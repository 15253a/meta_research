import json
import sys
from dataclasses import replace

import pytest

from meta_research.bundle_skill import CodexBundleSkillAdapter
from meta_research.deepfetch import CodexDeepFetchAdapter
from meta_research.idea_skill import CodexIdeaSkillAdapter, stage_provider
from meta_research.owners.common import canonical_hash
from meta_research.plan_skill import CodexPlanSkillAdapter
from meta_research.provider_supervisor import ensure_transport_key
from meta_research.reasoning_skill import CodexReasoningSkillAdapter
from meta_research.runtime_conditions import read_runtime_conditions, save_runtime_conditions
from test_runtime_conditions import root
from test_runtime_conditions_provider_prompts import _SignedRunner
from test_deepfetch_adapter import DurableSegmentSequenceRunner, _request, _deepfetch_web_evidence_gate_output_schema


@pytest.mark.parametrize("adapter_type,stage", [
    (CodexIdeaSkillAdapter, "idea"), (CodexPlanSkillAdapter, "plan"),
    (CodexBundleSkillAdapter, "bundle"), (CodexReasoningSkillAdapter, "reasoning"),
])
def test_saved_model_changes_preserve_existing_binding_and_signed_operation(root, adapter_type, stage):
    runner = _SignedRunner()
    workspace = root / (stage + "-provider")
    adapter = stage_provider(adapter_type(workspace, executable=sys.executable, process_runner=runner), quest_ref="quest-one")
    frozen_binding = adapter.runtime_binding()

    def call(provider, job):
        return provider._invoke_root_operation(
            operation_name="primary", prompt="Continue the exact research task.",
            schema={"type": "object", "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"], "additionalProperties": False},
            native_session_ref=None, job_ref=job, run_ref="run-one",
            attempt_ref=None, root_session_ref="session-one", fence_ref=None,
        )

    first = call(adapter, "frozen-one")
    directory = workspace / "provider-operations" / canonical_hash({"job_ref": "frozen-one"}) / "primary"
    original = {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}
    value = read_runtime_conditions(root, "quest-one")
    save_runtime_conditions(root, "quest-one", text=value["text"], expected_revision=value["revision"],
                            stage_models={**value["stage_models"], stage: "gpt-5.6-sol"})
    assert adapter.runtime_binding() == frozen_binding
    reconstructed = adapter_type(workspace, executable=sys.executable, process_runner=runner)
    existing = stage_provider(reconstructed, model_ref=frozen_binding.model_ref)
    assert existing.runtime_binding() == frozen_binding
    # Simulate loss of the derived completion checkpoint before an Owner
    # observes it. Recovery must verify the original signed exit and rebuild
    # that checkpoint, without sending a second physical provider call.
    (directory / "completed.json").unlink()
    assert call(existing, "frozen-one") == first
    assert len(runner.calls) == 1
    assert {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()} == original
    fresh = stage_provider(reconstructed, quest_ref="quest-one")
    assert fresh.runtime_binding().model_ref == "gpt-5.6-sol"
    call(fresh, "new-operation")
    assert len(runner.calls) == 2
    invocation = workspace / "provider-operations" / canonical_hash({"job_ref": "new-operation"}) / "primary" / "invocation.json"
    assert json.loads(invocation.read_text())["payload"]["model_ref"] == "gpt-5.6-sol"


def test_deepfetch_replays_saved_condition_source_and_refreshes_next_segment(root):
    workspace = root / "deepfetch-provider"
    value = read_runtime_conditions(root, "quest-one")
    selected = save_runtime_conditions(root, "quest-one", text=value["text"],
        research_style="focus", expected_revision=value["revision"])
    runner = DurableSegmentSequenceRunner(workspace, stopped_segments=0)
    adapter = CodexDeepFetchAdapter(workspace, process_runner=runner)
    request = replace(_request(), run_ref="deepfetch-manual", initialization_id="init-one",
                      runtime_binding=adapter.runtime_binding(), job_ref="deepfetch:persisted-conditions")
    _, key = ensure_transport_key(workspace)

    def call(provider, segment):
        return provider._run_durable_segment(
            directory=workspace / "provider-operations" / "conditions" / f"deepfetch-{segment}",
            segment_name=segment, job_ref=request.job_ref, request=request,
            prompt="web_evidence_gate=v1\nRead the evidence.",
            output_schema=_deepfetch_web_evidence_gate_output_schema(), timeout_seconds=None,
            native_session_ref=None if segment == "initial" else "native-many-durable-segments",
            transport_key=key,
        )

    first = call(adapter, "initial")
    directory = workspace / "provider-operations" / "conditions" / "deepfetch-initial"
    original = {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}
    assert "聚焦攻关" in (directory / "prompt.txt").read_text(encoding="utf-8")
    save_runtime_conditions(root, "quest-one", text=selected["text"], research_style="open",
                            expected_revision=selected["revision"])
    restarted = CodexDeepFetchAdapter(workspace, process_runner=runner)
    assert call(restarted, "initial") == first
    assert len(runner.calls) == 1
    assert {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()} == original
    call(restarted, "resume-1")
    assert len(runner.calls) == 2
    refreshed = directory.with_name("deepfetch-resume-1") / "prompt.txt"
    assert "开放探索" in refreshed.read_text(encoding="utf-8")
