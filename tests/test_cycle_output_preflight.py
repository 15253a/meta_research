import hashlib
import json
from dataclasses import replace
from unittest.mock import Mock
from types import SimpleNamespace

import pytest

from meta_research.owners.common import OwnerConflict, canonical_json
from meta_research.reasoning_contract import ReasoningContractError, validate_scientific_outcome
from meta_research.target_run_finalizer import TargetRunFinalizer
from test_reasoning_contract import _scientific_outcome, _research_context, _literature_closure
from test_target_root_finalizer import _root_finalizer_fixture
from test_research_notes_and_call_observations import _SystemEvidenceReader


def test_implementation_file_declaration_rejects_before_copy_with_exact_field(tmp_path, monkeypatch):
    import meta_research.target_run_finalizer as module
    runtime, lifecycle, memory, _, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        document = json.loads((workspace / "outputs/metrics.json").read_bytes())
        document["formal_runs"][0]["implementation_paths"] = ["implementation/train.py"]
        (workspace / "outputs/result.json").write_text(canonical_json(document))
        workspace_ref, _ = runtime.target_run_authorities.agent_runtime.resolve_target_workspace(
            target_ref=handle.target_ref, target_run_ref=handle.target_run_ref,
            root_session_ref=handle.root_session_ref, attempt_ref=handle.execution_attempt_ref,
            fence_ref=handle.execution_fence_ref)
        final = "Completed scientific work remains retained."
        evidence = replace(evidence, handoff=None, workspace_ref=workspace_ref, final_text=final,
            final_text_sha256=hashlib.sha256(final.encode()).hexdigest())
        freeze = Mock(wraps=module._freeze_artifact)
        monkeypatch.setattr(module, "_freeze_artifact", freeze)
        result = TargetRunFinalizer(lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            measurement_authority=runtime.owners.research_graph,
            graph_authority=runtime.owners.research_graph,
            evidence_reader=_SystemEvidenceReader()).finalize(handle=handle, evidence=evidence)
        assert result.status == "revision_required"
        assert result.pending_code == "target_implementation_workspace_invalid"
        assert "$.artifacts[0].relative_path" in result.rejection_feedback
        assert "formal_runs[].implementation_paths" in result.rejection_feedback
        assert "implementation/train.py" in result.rejection_feedback
        assert "directory" in result.rejection_feedback
        freeze.assert_not_called()
        assert result.manifest_ref is None
        assert (workspace / "implementation/train.py").read_text() == "print('train')\n"
    finally:
        runtime.close()


def test_implementation_preflight_does_not_recheck_live_workspace_during_frozen_recovery(tmp_path, monkeypatch):
    import meta_research.target_run_finalizer as module
    runtime, lifecycle, memory, _, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)

    class UnavailableMemory:
        def __getattr__(self, name):
            return getattr(memory, name)

        def accept(self, **values):
            raise OwnerConflict("target_root_artifact_intake_unavailable")

    try:
        selected = workspace / "implementation/selected"
        selected.mkdir()
        (workspace / "implementation/train.py").rename(selected / "train.py")
        (selected / "payload.bin").write_bytes(b"".join(
            hashlib.sha256(str(index).encode()).digest() for index in range(128)
        ))
        document = json.loads((workspace / "outputs/metrics.json").read_bytes())
        document["formal_runs"][0]["implementation_paths"] = ["implementation/selected"]
        (workspace / "outputs/result.json").write_text(canonical_json(document))
        # Exercise the existing large-artifact custody path with small fixtures.
        monkeypatch.setattr(module, "MAX_ASSET_BYTES", 1024)
        workspace_ref, _ = runtime.target_run_authorities.agent_runtime.resolve_target_workspace(
            target_ref=handle.target_ref, target_run_ref=handle.target_run_ref,
            root_session_ref=handle.root_session_ref, attempt_ref=handle.execution_attempt_ref,
            fence_ref=handle.execution_fence_ref)
        final = "Original completed research."
        evidence = replace(evidence, handoff=None, workspace_ref=workspace_ref, final_text=final,
            final_text_sha256=hashlib.sha256(final.encode()).hexdigest())
        with pytest.raises(OwnerConflict, match="target_root_artifact_intake_unavailable"):
            TargetRunFinalizer(lifecycle=lifecycle, memory=UnavailableMemory(),
                workspace_resolver=runtime.target_run_authorities.agent_runtime,
                measurement_authority=runtime.owners.research_graph,
                evidence_reader=_SystemEvidenceReader()).finalize(handle=handle, evidence=evidence)
        completion = lifecycle.query_completion(handle.target_ref)
        assert completion is not None and completion.artifact_snapshot_hash is not None
        # The original evidence remains in the private frozen source store.
        # Later workspace contents cannot invalidate or replace that generation.
        selected.rename(workspace / "implementation/later-selected")
        selected.write_text("unrelated later file")
        replayed = TargetRunFinalizer(lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            measurement_authority=runtime.owners.research_graph,
            evidence_reader=_SystemEvidenceReader()).finalize(handle=handle, evidence=evidence)
        assert replayed.status == "rm_accepted"
        assert replayed.completion_ref == completion.completion_ref
        assert replayed.completion_generation == 1
        manifest = memory.query(replayed.manifest_ref)
        assert manifest.artifact_snapshot_hash == completion.artifact_snapshot_hash
        assert lifecycle.query_completion_rejection(completion.completion_ref) is None
    finally:
        runtime.close()


@pytest.mark.parametrize("damage,field", [("unbound", "ref"), ("kind", "kind")])
def test_evidence_reference_level_reports_exact_field_without_changing_owner_code(damage, field):
    outcome = _scientific_outcome()
    if damage == "unbound":
        outcome["evidence"][0]["ref"] = "catalog-container-not-selected-leaf"
    else:
        outcome["evidence"][0]["kind"] = "MetricResult"
    with pytest.raises(ReasoningContractError) as caught:
        validate_scientific_outcome(outcome, frozen_evidence_closure=_literature_closure(),
            frozen_research_context=_research_context())
    assert str(caught.value) == "scientific_outcome_evidence_invalid"
    assert f"$.scientific_outcome.evidence[0].{field}" in caught.value.feedback


def test_adapter_preflight_keeps_raw_candidate_and_routes_field_feedback():
    from meta_research.reasoning_skill import CodexReasoningSkillAdapter
    from meta_research.reasoning_stage import ReasoningStageWorker
    from test_reasoning_skill_adapter import _request, _stage_output
    request = _request()
    output = _stage_output()
    output["scientific_outcome"]["evidence"][0]["ref"] = "catalog-container"
    before = canonical_json(output)
    adapter = object.__new__(CodexReasoningSkillAdapter)
    adapter._sealed_result_failure = Mock(return_value=RuntimeError("sealed-candidate"))
    with pytest.raises(RuntimeError, match="sealed-candidate"):
        adapter._assemble_provider_output(request, output, operation_name="primary", native_session_ref="native")
    failure = adapter._sealed_result_failure.call_args.kwargs
    assert failure["failure_code"] == "reasoning_primary_result_contract_invalid"
    assert failure["detail_code"].startswith("scientific_outcome_evidence_invalid:")
    assert "$.scientific_outcome.evidence[0].ref" in failure["detail_code"]
    assert canonical_json(output) == before
    assert failure["rejected_candidate"] is output
    worker = object.__new__(ReasoningStageWorker)
    worker._agent_runtime = SimpleNamespace(reject_stage_completion_candidate=Mock())
    worker._reject_completion_candidate(unit_ref="unit",
        run=SimpleNamespace(run_ref="run", attempt_ref="attempt", fence_ref="fence"),
        operation_name="primary", native_session_ref="native", candidate=output,
        failure_code=failure["failure_code"], detail_code=failure["detail_code"])
    rejected = worker._agent_runtime.reject_stage_completion_candidate.call_args.kwargs
    assert rejected["detail_code"] == "scientific_outcome_evidence_invalid"
    assert "$.scientific_outcome.evidence[0].ref" in rejected["feedback"][0]
