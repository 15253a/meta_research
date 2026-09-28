"""Frozen completion dependencies expose exact accepted output versions only."""
import pytest
from sqlalchemy import text

from meta_research.formal_entities import verified_target_input_asset_refs
from meta_research.owners.common import OwnerConflict, canonical_json
from meta_research.target_run_finalizer import TargetRunFinalizer
from test_plan_asset_target_input import _asset, _origin
from test_research_notes_and_call_observations import _SystemEvidenceReader
from test_target_formal_input_versions import _admitted_input_root


def test_ownerless_history_does_not_invent_dependency_asset_versions():
    assert verified_target_input_asset_refs(None, target_ref="target_history",
        proofs=[], target_commit_refs=("target_commit_history",)) == set()
    with pytest.raises(OwnerConflict, match="target_launch_asset_proof_verifier_unavailable"):
        verified_target_input_asset_refs(None, target_ref="target_history",
            proofs=[{"asset_ref": "asset_requires_owner"}], target_commit_refs=())


@pytest.mark.parametrize("assessed", [True, False], ids=["evaluated", "workproduct"])
def test_frozen_commit_artifact_versions_keep_exact_owner_custody(tmp_path, assessed):
    runtime, lifecycle, memory, handle, workspace, evidence, selected, _ = _admitted_input_root(tmp_path)
    try:
        graph = runtime.owners.research_graph
        authority = graph.query_target_measurement_domain_authority(handle.target_ref)
        metrics = {key: 1.0 for key in authority.measurement_contract.protocol_version.required_metric_keys}
        document = {"schema_ref": authority.measurement_contract.result_schema_ref,
            "metrics": metrics if assessed else {}, "result_disposition": "positive" if assessed else "uncertain",
            "formal_runs": [{"run_key": "upstream", "implementation_paths": ["implementation"],
                "input_refs": [selected.version_ref], "checkpoint_paths": [], "artifact_paths": [],
                "evaluations": [{"attempt_key": "upstream-check", "metrics": metrics}] if assessed else []}]}
        (workspace / "outputs/result.json").write_text(canonical_json(document))
        finalized = TargetRunFinalizer(lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_SystemEvidenceReader(), measurement_authority=graph,
            graph_authority=graph).finalize(handle=handle, evidence=evidence)
        assert finalized.status == "completed"
        manifest = memory.query(finalized.manifest_ref)
        produced = next(entry.binding for entry in manifest.entries if entry.role == "result")
        quest = graph.query_target_graph(authority.stage_request_ref).quest_ref
        # New versions and unrelated assets may be valid in this Quest without
        # belonging to the exact immutable manifest selected by the caller.
        newer = _asset(runtime, "new-output-version", asset_ref=produced.asset_ref)
        other = _asset(runtime, "unselected-same-quest-output")
        for key, binding in (("new-output", newer), ("other-output", other)):
            _origin(runtime, binding, quest, key)
        refs = verified_target_input_asset_refs(graph, target_ref=handle.target_ref,
            proofs=[], target_commit_refs=(finalized.target_commit_ref,))
        assert produced.version_ref in refs and produced.asset_ref in refs
        assert newer.version_ref not in refs and other.version_ref not in refs
        assert verified_target_input_asset_refs(graph, target_ref=handle.target_ref,
            proofs=[], target_commit_refs=()) == set()
        with pytest.raises(OwnerConflict, match="target_input_dependency_commit_invalid"):
            verified_target_input_asset_refs(graph, target_ref=handle.target_ref,
                proofs=[], target_commit_refs=("target_commit_unknown",))
        with pytest.raises(OwnerConflict, match="target_input_reference_scope_invalid"):
            graph.query_target_commit_input_asset_bindings(target_ref=handle.target_ref,
                quest_ref="foreign_quest", target_commit_refs=(finalized.target_commit_ref,))
        with pytest.raises(OwnerConflict, match="target_input_dependency_commit_invalid"):
            graph.query_target_commit_input_asset_bindings(quest_ref="foreign_quest",
                target_commit_refs=(finalized.target_commit_ref,))
        # Accepted custody is checked again, not inferred from object existence.
        with runtime._database.write() as connection:
            connection.execute(text("UPDATE rm_target_root_completion_manifests SET receipt_hash=:hash "
                "WHERE manifest_ref=:ref"), {"hash": "0" * 64, "ref": finalized.manifest_ref})
        with pytest.raises(OwnerConflict):
            verified_target_input_asset_refs(graph, target_ref=handle.target_ref,
                proofs=[], target_commit_refs=(finalized.target_commit_ref,))
    finally:
        runtime.close()
