from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from meta_research.deepfetch import CodexDeepFetchAdapter, DeepFetchUnavailable
from test_deepfetch_adapter import (
    PROTOTYPE_ACQUIRE, PROTOTYPE_FINAL, PROTOTYPE_EMPTY_FINAL,
    DurableSegmentSequenceRunner, RecordingAcquisitionClient, RecordingRunner, SequencedPrototypeRunner,
    _bind_acquisition, _execute, _request,
)


class ProvidedRunner(RecordingRunner):
    def __call__(self, argv, prompt, timeout):
        assert "web_evidence_gate=v1" not in prompt
        result = super().__call__(argv, prompt, timeout)
        output = Path(next(line.removeprefix("public_output_root=")
            for line in prompt.splitlines() if line.startswith("public_output_root=")))
        path = output / "papers.json"
        ledger = json.loads(path.read_text(encoding="utf-8"))
        ledger["run"]["dimensions_used"] = []
        ledger["run"]["active_search_elapsed_seconds"] = 0
        ledger["topic"]["search_concepts"] = []
        path.write_text(json.dumps(ledger), encoding="utf-8")
        return result


def test_provided_materials_finalize_without_fabricating_external_search(tmp_path):
    runner = ProvidedRunner(PROTOTYPE_EMPTY_FINAL, emit_web_evidence=False)
    adapter = CodexDeepFetchAdapter(tmp_path / "provider", process_runner=runner)
    request = replace(_request(), literature_access_mode="provided_only",
        scope={"goal": "Compare supplied evidence only", "literature_mode": "oa_only"})
    result = _execute(adapter, request)
    assert result.completion == "honest_empty"
    assert len(runner.calls) == 1
    assert result.web_evidence["search_event_count"] == 0
    assert result.web_evidence["fetch_event_count"] == 0
    assert result.papers_ledger["run"]["dimensions_used"] == []
    argv, prompt, _ = runner.calls[0]
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert "--ignore-user-config" in argv and "--ignore-rules" in argv
    assert 'web_search="disabled"' in argv
    assert "sandbox_workspace_write.network_access=false" in argv
    assert 'shell_environment_policy.inherit="none"' in argv
    assert "literature_access_mode=provided_only" in prompt


def test_missing_host_mode_cannot_start_a_provider_effect(tmp_path):
    runner = RecordingRunner(PROTOTYPE_EMPTY_FINAL)
    adapter = CodexDeepFetchAdapter(tmp_path, process_runner=runner)
    with pytest.raises(DeepFetchUnavailable, match="deepfetch_literature_access_mode_missing"):
        _execute(adapter, replace(_request(), literature_access_mode=None))
    assert runner.calls == []


def test_gate_gets_exact_scientific_input_and_substantive_turn_reuses_it(tmp_path):
    runner = RecordingRunner(PROTOTYPE_EMPTY_FINAL)
    adapter = CodexDeepFetchAdapter(tmp_path, process_runner=runner)
    scope = {"goal": "Distinguish cold transfer from warm transfer",
        "conditions": "Keep patient cohort and protocol unchanged",
        "creation_basis": {"basis_ref": "basis_exact", "expected_basis_hash": "a" * 64}}
    _execute(adapter, replace(_request(), scope=scope, quest_ref="quest-existing",
        creation_context_kind="manual_question_creation", literature_access_mode="oa_only"))
    first, second = [call[1] for call in runner.calls]
    assert "basis_exact" in first and scope["conditions"] in first
    assert "accepted_material_bindings=" in first
    assert "quest_ref=quest-existing" in first
    assert "This initialization has no Quest" not in first + second
    assert "scope=" not in second
    assert "# DeepFetch v4：文献证据梳理" in second
    assert "不得探测或评价机构访问可用性" in second
    assert runner.calls[1][0][-3:] == ["resume", "native-web-research-1", "-"]


def test_provided_materials_reject_claimed_external_web_success(tmp_path):
    runner = ProvidedRunner(PROTOTYPE_EMPTY_FINAL, emit_web_evidence=True)
    adapter = CodexDeepFetchAdapter(tmp_path, process_runner=runner)
    with pytest.raises(DeepFetchUnavailable, match="deepfetch_web_evidence_invalid"):
        _execute(adapter, replace(_request(), literature_access_mode="provided_only"))


def test_provided_fulltext_reader_result_keeps_exact_acquired_bytes(tmp_path):
    class ProvidedSequenceRunner(SequencedPrototypeRunner):
        def __call__(self, argv, prompt, timeout):
            assert "web_evidence_gate=v1" not in prompt
            result = super().__call__(argv, prompt, timeout)
            output = Path(next(line.removeprefix("public_output_root=")
                for line in prompt.splitlines() if line.startswith("public_output_root=")))
            path = output / "papers.json"
            ledger = json.loads(path.read_text(encoding="utf-8"))
            ledger["run"]["dimensions_used"] = []
            ledger["run"]["active_search_elapsed_seconds"] = 0
            path.write_text(json.dumps(ledger), encoding="utf-8")
            return result

    runner = ProvidedSequenceRunner([PROTOTYPE_ACQUIRE, PROTOTYPE_FINAL])
    runner.emit_web_evidence = False
    acquisition = RecordingAcquisitionClient(tmp_path / "acquired")
    adapter = _bind_acquisition(CodexDeepFetchAdapter(tmp_path / "provider", process_runner=runner), acquisition)
    result = _execute(adapter, replace(_request(), literature_access_mode="provided_only"))
    assert result.completion == "complete"
    assert len(result.fulltexts) == 1
    assert result.papers_ledger["papers"]["doi:10.1000/example"]["reading"]["status"] == "complete"
    assert result.web_evidence["search_event_count"] == 0
    assert len(acquisition.calls) == 1
    assert len(runner.calls) == 2
    assert "scope=" not in runner.calls[1][1]
    assert "literature_access_mode=provided_only" in runner.calls[1][1]


def test_human_response_on_same_native_is_incremental(tmp_path):
    runner = RecordingRunner(PROTOTYPE_EMPTY_FINAL)
    adapter = CodexDeepFetchAdapter(tmp_path, process_runner=runner)
    result = _execute(adapter, replace(_request(),
        native_session_ref="native-web-research-1",
        human_request_resume={"effect_id": "help-original", "request_ref": "hr-original", "phase": "turn-0"}))
    assert result.completion == "honest_empty"
    assert len(runner.calls) == 1
    prompt = runner.calls[0][1]
    assert "human_request_effect_id=help-original" in prompt
    assert "human_request.open.reconcile" in prompt
    assert "scope=" not in prompt
    assert "# DeepFetch v4：文献证据梳理" in prompt
    context = Path(next(line.removeprefix("exact_scientific_input_path=")
        for line in prompt.splitlines() if line.startswith("exact_scientific_input_path=")))
    assert json.loads(context.read_text())["scope"] == _request().scope


@pytest.mark.parametrize("same_native", [True, False])
def test_resume_preserves_original_fulltext_and_acquisition_without_redo(tmp_path, same_native):
    class InterruptedReaderRunner(SequencedPrototypeRunner):
        def __init__(self):
            super().__init__([PROTOTYPE_ACQUIRE, PROTOTYPE_FINAL])
            self.interrupted = False

        def __call__(self, argv, prompt, timeout):
            if "existing_papers_path=" in prompt:
                self.emit_web_evidence = False  # Existing successful Gate remains evidence.
                self.output = PROTOTYPE_FINAL
                result = RecordingRunner.__call__(self, argv, prompt, timeout)
                if not same_native:
                    result.stdout = result.stdout.replace("native-web-research-1", "native-fresh-recovery")
                return result
            result = super().__call__(argv, prompt, timeout)
            if "phase=acquisition_result" in prompt and not self.interrupted:
                self.interrupted = True
                self.original_output = Path(next(line.removeprefix("public_output_root=")
                    for line in prompt.splitlines() if line.startswith("public_output_root=")))
                self.original_bytes = (self.original_output / "papers.json").read_bytes()
                raise DeepFetchUnavailable("deepfetch_human_request_wait", durable_outcome="pending",
                    native_session_ref="native-web-research-1")
            return result

    runner = InterruptedReaderRunner()
    acquisition = RecordingAcquisitionClient(tmp_path / "acquired")
    adapter = _bind_acquisition(CodexDeepFetchAdapter(tmp_path / "provider", process_runner=runner), acquisition)
    original = replace(_request(), job_ref=f"{_request().run_ref}:deepfetch:1")
    with pytest.raises(DeepFetchUnavailable, match="deepfetch_human_request_wait"):
        _execute(adapter, original)
    if not same_native:
        adapter.prepare_retry_execution(original, previous_job_ref=original.job_ref,
            previous_run_ref=original.run_ref, previous_runtime_binding=adapter.runtime_binding())
        assert (runner.original_output / "papers.json").read_bytes() == runner.original_bytes
    resumed = replace(original, job_ref=f"{original.run_ref}:deepfetch:2",
        native_session_ref="native-web-research-1" if same_native else None,
        previous_job_ref=original.job_ref,
        human_request_resume={"effect_id": "original-help", "request_ref": "original-hr",
            "phase": "turn-2"} if same_native else None)
    result = _execute(adapter, resumed)
    assert result.completion == "complete"
    assert len(acquisition.calls) == 1
    assert (runner.original_output / "papers.json").read_bytes() == runner.original_bytes
    assert f"public_output_root={runner.original_output}" in runner.calls[-1][1]
    assert ("# DeepFetch v4：文献证据梳理" in runner.calls[-1][1]) == (not same_native)
    assert ("scope=" in runner.calls[-1][1]) == (not same_native)
    assert result.native_session_ref == ("native-web-research-1" if same_native else "native-fresh-recovery")
    assert result.web_evidence["search_event_count"] > 0


@pytest.mark.parametrize("human_gate_resume", [False, True])
def test_signed_final_ack_loss_reconciles_twice_without_provider_replay(tmp_path, human_gate_resume):
    workspace = tmp_path / "provider"
    runner = DurableSegmentSequenceRunner(workspace, stopped_segments=0)
    adapter = _bind_acquisition(CodexDeepFetchAdapter(workspace, process_runner=runner),
        RecordingAcquisitionClient(tmp_path / "acquired"))
    request = replace(_request(), runtime_binding=adapter.runtime_binding(), job_ref="signed-final-recovery")
    if human_gate_resume:
        request = replace(request, native_session_ref="native-many-durable-segments",
            human_request_resume={"effect_id": "gate-help", "request_ref": "gate-hr", "phase": "turn-0"})
    first = adapter.execute(request)
    checkpoint_path = next((workspace / "runs").glob("*/private/protocol.json"))
    checkpoint = json.loads(checkpoint_path.read_text())
    public = checkpoint_path.parent.parent / "public"
    checkpoint.update(phase="ready_for_turn", next_turn_number=checkpoint["next_turn_number"] - 1,
        evidence_parts=checkpoint["evidence_parts"][:-1], turn_sources=checkpoint["turn_sources"][:-1],
        final_envelope=None, next_prompt=f"public_output_root={public}\nContinue work")
    checkpoint_path.write_text(json.dumps(checkpoint))
    before = tuple(runner.calls)
    recovered = adapter.execute(replace(request, reconcile_only=True))
    again = adapter.execute(replace(request, reconcile_only=True))
    assert recovered.summary == again.summary == first.summary
    assert tuple(runner.calls) == before
    sealed = json.loads(checkpoint_path.read_text())
    assert len(sealed["turn_sources"]) == len(sealed["evidence_parts"]) == sealed["next_turn_number"]


@pytest.mark.parametrize("same_native", [True, False])
def test_signed_resume_reconciles_prior_resident_mcp_phases(tmp_path, same_native):
    workspace = tmp_path / "provider"

    class InterruptedDurableRunner(DurableSegmentSequenceRunner):
        def run_durable_job(self, *args, **kwargs):
            if len(self.calls) == 1 and not getattr(self, "interrupted", False):
                self.interrupted = True
                raise DeepFetchUnavailable("deepfetch_human_request_wait", durable_outcome="pending",
                    native_session_ref="native-many-durable-segments")
            return super().run_durable_job(*args, **kwargs)

    runner = InterruptedDurableRunner(workspace, stopped_segments=0)
    authority = RecordingAcquisitionClient(tmp_path / "acquired")
    adapter = _bind_acquisition(CodexDeepFetchAdapter(workspace, process_runner=runner), authority)
    original = replace(_request(), runtime_binding=adapter.runtime_binding(), job_ref="before-hr")
    with pytest.raises(DeepFetchUnavailable, match="deepfetch_human_request_wait"):
        adapter.execute(original)
    if not same_native:
        runner.thread_ref = "native-fresh-recovery"
    resumed = replace(original, job_ref="after-hr",
        native_session_ref="native-many-durable-segments" if same_native else None,
        previous_job_ref=original.job_ref,
        human_request_resume={"phase": "turn-1", "effect_id": "help", "request_ref": "hr"} if same_native else None)
    first = adapter.execute(resumed)
    if not same_native:
        checkpoint_path = next(path for path in (workspace / "runs").glob("*/private/protocol.json")
            if json.loads(path.read_text())["phase"] == "finalized")
        checkpoint = json.loads(checkpoint_path.read_text())
        checkpoint.update(phase="ready_for_turn", next_turn_number=1,
            evidence_parts=checkpoint["evidence_parts"][:-1], turn_sources=checkpoint["turn_sources"][:-1],
            final_envelope=None, next_prompt="Continue exact prior work")
        checkpoint_path.write_text(json.dumps(checkpoint))
    before = tuple(runner.calls)
    issued_before = len(authority.issued)
    revoked_before = len(authority.revoked)
    recovered = adapter.execute(replace(resumed, reconcile_only=True))
    again = adapter.execute(replace(resumed, reconcile_only=True))
    assert recovered.summary == again.summary == first.summary
    assert tuple(runner.calls) == before
    assert {scope["phase"] for scope in authority.issued} == {"turn-0", "turn-1"}
    assert len(authority.revoked) - revoked_before == len(authority.issued) - issued_before
