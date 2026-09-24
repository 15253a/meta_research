"""Editable conditions affect new calls without rewriting sealed operations."""

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from meta_research.bundle_skill import CodexBundleSkillAdapter
from meta_research.deepfetch import CodexDeepFetchAdapter, DeepFetchUnavailable
from meta_research.idea_skill import CodexIdeaSkillAdapter, IdeaSkillUnavailable
from meta_research.owners.common import canonical_hash
from meta_research.plan_skill import CodexPlanSkillAdapter
from meta_research.provider_supervisor import (
    ensure_transport_key, read_supervisor_request, read_transport_key_for_operation,
    write_exit_receipt,
)
from meta_research.reasoning_skill import CodexReasoningSkillAdapter
from test_deepfetch_adapter import (
    DurableSegmentSequenceRunner, RecordingRunner, _deepfetch_web_evidence_gate_output_schema,
    _request,
)


class _SignedRunner:
    def __init__(self):
        self.calls = []

    def run_job(self, *args, **kwargs):
        raise AssertionError("use the signed durable boundary")

    def __call__(self, argv, prompt, timeout, environment=None):
        self.calls.append((argv, prompt))
        Path(argv[argv.index("--output-last-message") + 1]).write_text('{"ok":true}')
        return subprocess.CompletedProcess(
            argv, 0,
            '{"type":"thread.started","thread_id":"runtime-native"}\n'
            '{"type":"turn.completed","usage":{}}\n', "",
        )

    def run_durable_job(self, job_ref, argv, prompt, timeout, stdout_path,
                        pid_path, request_path, environment=None):
        result = self(argv, prompt, timeout, environment)
        stdout_path.write_text(result.stdout)
        _, key = read_transport_key_for_operation(request_path.parent)
        request = read_supervisor_request(request_path, key)
        write_exit_receipt(
            Path(request["receipt_path"]), key=key,
            invocation_hash=request["invocation_hash"],
            prompt_path=Path(request["prompt_path"]),
            schema_path=Path(request["schema_path"]), stdout_path=stdout_path,
            result_path=Path(request["result_path"]), returncode=0,
            input_bytes=len(prompt.encode("utf-8")),
        )
        return result


def _idea_call(adapter, *, job="job:initial", prompt="Research the actual question.", native=None):
    return adapter._invoke_root_operation(
        operation_name="primary", prompt=prompt,
        schema={"type":"object", "properties":{"ok":{"type":"boolean"}},
                "required":["ok"], "additionalProperties":False},
        native_session_ref=native, job_ref=job, run_ref="stage-run:current",
        attempt_ref=None, root_session_ref="session:current", fence_ref=None,
    )


@pytest.fixture
def conditions(monkeypatch):
    state = {"value":"Selected GPU: GPU-A; time budget: 7d", "scopes":[]}
    def render(workspace, **scope):
        state["scopes"].append(scope)
        return state["value"]
    for module in ("idea_skill", "deepfetch"):
        monkeypatch.setattr(f"meta_research.{module}.render_runtime_conditions", render, raising=False)
    return state


@pytest.mark.parametrize("adapter_type", (
    CodexIdeaSkillAdapter, CodexPlanSkillAdapter,
    CodexBundleSkillAdapter, CodexReasoningSkillAdapter,
))
def test_stage_conditions_freeze_per_operation_and_refresh_on_native_resume(
    tmp_path, conditions, adapter_type,
):
    runner = _SignedRunner()
    adapter = adapter_type(tmp_path, process_runner=runner)
    first = _idea_call(adapter)
    assert "GPU-A" in runner.calls[0][1]
    assert conditions["scopes"] == [{"run_ref":"stage-run:current"}]
    directory = tmp_path / "provider-operations" / canonical_hash({"job_ref":"job:initial"}) / "primary"
    sealed_before = {p.name:p.read_bytes() for p in directory.iterdir() if p.is_file()}
    conditions["value"] = "Selected GPU: GPU-B; time budget: 30d"
    assert _idea_call(adapter) == first
    assert len(runner.calls) == 1
    assert len(conditions["scopes"]) == 1
    assert {p.name:p.read_bytes() for p in directory.iterdir() if p.is_file()} == sealed_before
    _idea_call(adapter, job="job:next", native="runtime-native")
    assert "GPU-B" in runner.calls[1][1]
    assert runner.calls[1][0][-3:] == ["resume", "runtime-native", "-"]


@pytest.mark.parametrize("damage", ("base_prompt", "stored_conditions", "missing_prompt", "seal"))
def test_stage_conditions_do_not_bypass_frozen_task_or_signature(tmp_path, conditions, damage):
    runner = _SignedRunner()
    adapter = CodexIdeaSkillAdapter(tmp_path, process_runner=runner)
    _idea_call(adapter)
    directory = tmp_path / "provider-operations" / canonical_hash({"job_ref":"job:initial"}) / "primary"
    prompt_path = directory / "prompt.txt"
    changed_prompt = "Different research task." if damage == "base_prompt" else "Research the actual question."
    if damage == "stored_conditions":
        prompt_path.write_text(prompt_path.read_text().replace("GPU-A", "GPU-X"))
    elif damage == "missing_prompt":
        prompt_path.unlink()
    elif damage == "seal":
        invocation_path = directory / "invocation.json"
        invocation = json.loads(invocation_path.read_text())
        invocation["seal"] = "0" * 64
        invocation_path.write_text(json.dumps(invocation))
    with pytest.raises(IdeaSkillUnavailable):
        _idea_call(adapter, prompt=changed_prompt)
    assert len(runner.calls) == 1


def test_legacy_stage_prompt_stays_unchanged_when_new_settings_appear(tmp_path, conditions):
    conditions["value"] = ""
    runner = _SignedRunner()
    adapter = CodexIdeaSkillAdapter(tmp_path, process_runner=runner)
    first = _idea_call(adapter)
    assert runner.calls[0][1] == "Research the actual question."
    conditions["value"] = "Selected GPU: GPU-B"
    assert _idea_call(adapter) == first
    assert len(runner.calls) == 1


@pytest.mark.parametrize("native", (None, "runtime-native"))
def test_nondurable_stage_call_receives_current_conditions(tmp_path, conditions, native):
    runner = _SignedRunner()
    adapter = CodexIdeaSkillAdapter(tmp_path, process_runner=runner)
    _idea_call(adapter, job=None, native=native)
    assert "GPU-A" in runner.calls[0][1]


def _deepfetch_setup(tmp_path):
    runner = DurableSegmentSequenceRunner(tmp_path, stopped_segments=0)
    adapter = CodexDeepFetchAdapter(tmp_path, process_runner=runner)
    request = replace(_request(), runtime_binding=adapter.runtime_binding(), job_ref="deepfetch:conditions")
    _, key = ensure_transport_key(tmp_path)
    def call(segment="initial", prompt="web_evidence_gate=v1\nRead the evidence."):
        return adapter._run_durable_segment(
            directory=tmp_path / "provider-operations" / "conditions" / f"deepfetch-{segment}",
            segment_name=segment, job_ref=request.job_ref, request=request, prompt=prompt,
            output_schema=_deepfetch_web_evidence_gate_output_schema(), timeout_seconds=None,
            native_session_ref=None if segment == "initial" else "native-many-durable-segments",
            transport_key=key,
        )
    return runner, call


def test_deepfetch_conditions_freeze_per_segment_and_refresh_on_resume(tmp_path, conditions):
    runner, call = _deepfetch_setup(tmp_path)
    first = call()
    directory = tmp_path / "provider-operations" / "conditions" / "deepfetch-initial"
    assert "GPU-A" in (directory / "prompt.txt").read_text()
    assert conditions["scopes"] == [{"run_ref":"deepfetch_run_1", "initialization_id":"quest_init_1"}]
    sealed_before = {p.name:p.read_bytes() for p in directory.iterdir() if p.is_file()}
    conditions["value"] = "Selected GPU: GPU-B; time budget: 30d"
    assert call() == first
    assert len(runner.calls) == 1
    assert len(conditions["scopes"]) == 1
    assert {p.name:p.read_bytes() for p in directory.iterdir() if p.is_file()} == sealed_before
    call("resume-1")
    assert "GPU-B" in (directory.with_name("deepfetch-resume-1") / "prompt.txt").read_text()
    assert len(runner.calls) == 2


@pytest.mark.parametrize("damage", ("base_prompt", "stored_conditions", "missing_prompt", "seal"))
def test_deepfetch_conditions_do_not_bypass_task_or_signature(tmp_path, conditions, damage):
    runner, call = _deepfetch_setup(tmp_path)
    call()
    directory = tmp_path / "provider-operations" / "conditions" / "deepfetch-initial"
    prompt_path = directory / "prompt.txt"
    prompt = "Different question." if damage == "base_prompt" else "web_evidence_gate=v1\nRead the evidence."
    if damage == "stored_conditions":
        prompt_path.write_text(prompt_path.read_text().replace("GPU-A", "GPU-X"))
    elif damage == "missing_prompt":
        prompt_path.unlink()
    elif damage == "seal":
        invocation_path = directory / "invocation.json"
        invocation = json.loads(invocation_path.read_text())
        invocation["seal"] = "0" * 64
        invocation_path.write_text(json.dumps(invocation))
    with pytest.raises(DeepFetchUnavailable):
        call(prompt=prompt)
    assert len(runner.calls) == 1


def test_legacy_deepfetch_prompt_stays_frozen_when_settings_appear(tmp_path, conditions):
    conditions["value"] = ""
    runner, call = _deepfetch_setup(tmp_path)
    first = call()
    conditions["value"] = "Selected GPU: GPU-B"
    assert call() == first
    assert len(runner.calls) == 1
    directory = tmp_path / "provider-operations" / "conditions" / "deepfetch-initial"
    assert (directory / "prompt.txt").read_text() == "web_evidence_gate=v1\nRead the evidence."


@pytest.mark.parametrize("native", (None, "native-web-research-1"))
def test_nondurable_deepfetch_receives_current_conditions(tmp_path, conditions, native):
    runner = RecordingRunner({"status":"web_evidence_ready"})
    adapter = CodexDeepFetchAdapter(tmp_path, process_runner=runner)
    request = replace(_request(), native_session_ref=native)
    adapter._invoke_with_access(
        request, "web_evidence_gate=v1\nRead the evidence.",
        output_schema=_deepfetch_web_evidence_gate_output_schema(),
        timeout_seconds=None, access=None,
    )
    assert "GPU-A" in runner.calls[0][1]


def test_deepfetch_automatic_resume_reads_new_conditions_per_segment(tmp_path, conditions):
    class EditingRunner(DurableSegmentSequenceRunner):
        def run_durable_job(self, *args, **kwargs):
            result = super().run_durable_job(*args, **kwargs)
            conditions["value"] = "Selected GPU: GPU-B"
            return result
    runner = EditingRunner(tmp_path, stopped_segments=1)
    adapter = CodexDeepFetchAdapter(tmp_path, process_runner=runner)
    request = replace(_request(), job_ref="deepfetch:automatic", runtime_binding=adapter.runtime_binding())
    adapter._invoke_with_access(
        request, "web_evidence_gate=v1\nRead the evidence.",
        output_schema=_deepfetch_web_evidence_gate_output_schema(),
        timeout_seconds=None, access=None,
    )
    first = next(tmp_path.glob("provider-operations/*/deepfetch-initial/prompt.txt"))
    resumed = next(tmp_path.glob("provider-operations/*/deepfetch-resume-1/prompt.txt"))
    assert "GPU-A" in first.read_text()
    assert "GPU-B" in resumed.read_text()
    assert len(runner.calls) == 2
