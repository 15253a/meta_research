"""Target results retain research facts without a mandatory overall verdict."""

import json
from dataclasses import replace

import pytest

from meta_research.bundle_protocol import projection_plain_value
from meta_research.owners.common import canonical_hash
from meta_research.target_run_finalizer import TargetRunFinalizer
from test_target_root_finalizer import _EvidenceReader, _root_finalizer_fixture


def test_unclassified_result_is_accepted_persisted_and_replayable(tmp_path):
    runtime, lifecycle, memory, authority, handle, workspace, evidence = (
        _root_finalizer_fixture(tmp_path)
    )
    try:
        result_path = workspace / "outputs/metrics.json"
        document = json.loads(result_path.read_bytes())
        document.pop("result_disposition")
        document["target_spec_binding"] = projection_plain_value(
            runtime.owners.research_graph.query_target_launch_request(handle.target_ref)
            .target_spec_binding
        )
        original = (json.dumps(document, indent=2) + "\n").encode()
        result_path.write_bytes(original)
        frozen_schema_hash = canonical_hash(
            authority.measurement_contract.result_schema.as_dict()
        )
        finalizer = TargetRunFinalizer(
            lifecycle=lifecycle,
            memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_EvidenceReader(evidence),
            measurement_authority=runtime.owners.research_graph,
            graph_authority=runtime.owners.research_graph,
        )

        accepted = finalizer.finalize(handle=handle, evidence=evidence)
        assert accepted.status == "completed", accepted
        manifest = memory.query(accepted.manifest_ref)
        assert manifest.result_document.as_dict() == document
        result_entry = next(entry for entry in manifest.entries if entry.role == "result")
        assert runtime.owners.research_memory.materialize_asset(
            result_entry.binding.version_ref
        ).content == original
        graph = runtime.owners.research_graph
        commit = next(
            item for item in graph.query_target_commits(authority.graph_ref)
            if item.commit_ref == accepted.target_commit_ref
        )
        assert commit.result_disposition is None
        terminal = graph.query_target_frontier_commit_transition(
            handle.target_ref
        ).canonical_terminal
        assert projection_plain_value(terminal)["metric_values"] == [1.0]
        graph.verify_bundle_report_target_commits(
            graph_ref=authority.graph_ref,
            closures=(terminal,),
            receipts=(commit.receipt,),
            head_receipt=graph.query_target_graph_head(authority.graph_ref).receipt,
        )
        assert canonical_hash(graph.query_target_measurement_domain_authority(
            handle.target_ref
        ).measurement_contract.result_schema.as_dict()) == frozen_schema_hash
        assert finalizer.finalize(handle=handle, evidence=evidence) == accepted
        assert graph.query_target_commits(authority.graph_ref) == (commit,)
    finally:
        runtime.close()


@pytest.mark.parametrize("field", ["target_ref", "root_session_ref", "target_spec_binding"])
def test_conflicting_result_identity_can_be_corrected_on_the_same_work(tmp_path, field):
    runtime, lifecycle, memory, authority, handle, workspace, evidence = (
        _root_finalizer_fixture(tmp_path)
    )
    try:
        result_path = workspace / "outputs/metrics.json"
        document = json.loads(result_path.read_bytes())
        document.pop("result_disposition")
        document[field] = "another-identity"
        rejected_bytes = json.dumps(document).encode()
        result_path.write_bytes(rejected_bytes)
        finalizer = TargetRunFinalizer(
            lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_EvidenceReader(evidence),
            measurement_authority=runtime.owners.research_graph,
            graph_authority=runtime.owners.research_graph,
        )

        rejected = finalizer.finalize(handle=handle, evidence=evidence)
        assert rejected.status == "revision_required", rejected
        assert rejected.rejection_issuer == "research_graph"
        assert f"$.{field}" in rejected.rejection_feedback
        assert "another-identity" in rejected.rejection_feedback
        expected_identity = (
            runtime.owners.research_graph.query_target_launch_request(handle.target_ref)
            .target_spec_binding.content_hash_ref
            if field == "target_spec_binding" else getattr(handle, field)
        )
        assert expected_identity in rejected.rejection_feedback
        assert runtime.owners.research_graph.query_target_commits(authority.graph_ref) == ()

        document.pop(field)
        corrected_bytes = json.dumps(document).encode()
        result_path.write_bytes(corrected_bytes)
        revised_evidence = replace(
            evidence,
            operation_ref="harness_target_identity_corrected_final_turn",
            operation_generation=evidence.operation_generation + 1,
            evidence_ref="harness_evidence_target_identity_corrected_final_turn",
            evidence_sequence=evidence.evidence_sequence + 1,
            handoff=replace(evidence.handoff, summary="Corrected the source identity."),
            observed_at=evidence.observed_at + 1,
        )
        revised = TargetRunFinalizer(
            lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_EvidenceReader(revised_evidence),
            measurement_authority=runtime.owners.research_graph,
            graph_authority=runtime.owners.research_graph,
        ).finalize(handle=handle, evidence=revised_evidence)
        assert revised.status == "completed", revised
        assert revised.completion_generation == 2
        commit, = runtime.owners.research_graph.query_target_commits(authority.graph_ref)
        assert commit.target_ref == handle.target_ref
        assert commit.target_run_ref == handle.target_run_ref
        assert commit.closure["root_measurement"]["metrics"] == {"metric:effect": 1.0}
        old_manifest = memory.query(rejected.manifest_ref)
        old_entry = next(entry for entry in old_manifest.entries if entry.role == "result")
        assert runtime.owners.research_memory.materialize_asset(
            old_entry.binding.version_ref
        ).content == rejected_bytes
        revised_completion = lifecycle.query_completion(handle.target_ref)
        assert revised_completion.predecessor_completion_ref == rejected.completion_ref
    finally:
        runtime.close()


def test_frozen_available_input_is_not_inferred_as_adopted_by_new_work(tmp_path):
    from test_research_notes_and_call_observations import _SystemEvidenceReader
    from test_target_formal_input_versions import _admitted_input_root

    runtime, lifecycle, memory, handle, workspace, evidence, selected, newer = (
        _admitted_input_root(tmp_path)
    )
    try:
        graph = runtime.owners.research_graph
        document = {
            "metrics": {"metric:effect": 1.0},
            "formal_runs": [{
                "run_key": "observed",
                "evaluations": [{"attempt_key": "measured", "metrics": {"metric:effect": 1.0}}],
            }],
        }
        (workspace / "outputs/result.json").write_text(json.dumps(document), encoding="utf-8")
        finalizer = TargetRunFinalizer(
            lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_SystemEvidenceReader(),
            measurement_authority=graph, graph_authority=graph,
        )
        accepted = finalizer.finalize(handle=handle, evidence=evidence)
        assert accepted.status == "completed", accepted
        fact, = graph.query_target_formal_results(handle.target_ref)
        inputs = fact["variant_run"]["inputs"]
        assert inputs["input_refs"] == []
        assert inputs["input_selection_recorded"] is False
        proof, = inputs["accepted_input_asset_proofs"]
        assert proof["asset_ref"] == selected.asset_ref
        admitted = runtime.target_run_authorities.research_graph.query_input_asset_projection(
            target_ref=handle.target_ref, asset_ref=proof["asset_ref"]
        ).asset
        assert admitted.version_ref == selected.version_ref
        assert admitted.version_ref != newer.version_ref
        commit, = graph.query_target_commits(graph.query_target_measurement_domain_authority(
            handle.target_ref
        ).graph_ref)
        input_refs = commit.closure["accepted_measurement"]["variant_run_input_binding"]["input_refs"]
        assert selected.asset_ref not in input_refs
        assert selected.version_ref not in input_refs
        assert finalizer.finalize(handle=handle, evidence=evidence) == accepted
        assert graph.query_target_formal_results(handle.target_ref) == (fact,)
    finally:
        runtime.close()


def test_missing_metric_feedback_identifies_the_affected_actual_evaluation(tmp_path):
    runtime, lifecycle, memory, _authority, handle, workspace, evidence = (
        _root_finalizer_fixture(tmp_path)
    )
    try:
        result_path = workspace / "outputs/metrics.json"
        document = json.loads(result_path.read_bytes())
        document.pop("result_disposition")
        document["metrics"] = {"metric:unregistered": 999.0}
        document["formal_runs"][0]["evaluations"][0]["metrics"] = document["metrics"]
        result_path.write_text(json.dumps(document), encoding="utf-8")
        rejected = TargetRunFinalizer(
            lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_EvidenceReader(evidence),
            measurement_authority=runtime.owners.research_graph,
            graph_authority=runtime.owners.research_graph,
        ).finalize(handle=handle, evidence=evidence)

        assert rejected.status == "revision_required", rejected
        feedback = rejected.rejection_feedback
        assert "metric:effect" in feedback
        assert "metric:unregistered" in feedback
        assert "Run 'primary'" in feedback
        assert "Evaluation 'primary'" in feedback
        assert "preserve" in feedback.lower()
    finally:
        runtime.close()


def test_secondary_metric_conflict_can_be_revised_without_repeating_valid_work(tmp_path):
    runtime, lifecycle, memory, authority, handle, workspace, evidence = (
        _root_finalizer_fixture(tmp_path)
    )
    try:
        result_path = workspace / "outputs/metrics.json"
        document = json.loads(result_path.read_bytes())
        document.pop("result_disposition")
        document["formal_runs"][0]["evaluations"].append({
            "attempt_key": "secondary-bad", "metrics": {"metric:unregistered": 999.0},
        })
        rejected_bytes = json.dumps(document).encode()
        result_path.write_bytes(rejected_bytes)
        finalizer = TargetRunFinalizer(
            lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_EvidenceReader(evidence),
            measurement_authority=runtime.owners.research_graph,
            graph_authority=runtime.owners.research_graph,
        )

        rejected = finalizer.finalize(handle=handle, evidence=evidence)
        assert rejected.status == "revision_required", rejected
        assert rejected.pending_code == "target_formal_metric_definition_invalid"
        feedback = rejected.rejection_feedback
        assert "Run 'primary'" in feedback
        assert "Evaluation 'secondary-bad'" in feedback
        assert "metric:effect" in feedback
        assert "metric:unregistered" in feedback
        assert authority.identities.protocol_version_ref in feedback
        graph = runtime.owners.research_graph
        assert graph.query_target_commits(authority.graph_ref) == ()
        assert graph.query_target_formal_results(handle.target_ref) == ()

        document["formal_runs"][0]["evaluations"][1]["metrics"] = {"metric:effect": 2.0}
        result_path.write_text(json.dumps(document), encoding="utf-8")
        revised_evidence = replace(
            evidence,
            operation_ref="harness_secondary_metric_corrected_final_turn",
            operation_generation=evidence.operation_generation + 1,
            evidence_ref="harness_evidence_secondary_metric_corrected_final_turn",
            evidence_sequence=evidence.evidence_sequence + 1,
            handoff=replace(evidence.handoff, summary="Corrected only the secondary metrics."),
            observed_at=evidence.observed_at + 1,
        )
        revised_finalizer = TargetRunFinalizer(
            lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_EvidenceReader(revised_evidence),
            measurement_authority=graph, graph_authority=graph,
        )
        accepted = revised_finalizer.finalize(handle=handle, evidence=revised_evidence)
        assert accepted.status == "completed", accepted
        assert accepted.completion_generation == 2
        commit, = graph.query_target_commits(authority.graph_ref)
        assert commit.target_run_ref == handle.target_run_ref
        facts = graph.query_target_formal_results(handle.target_ref)
        assert {item["attempt_key"]: item["metric_result"]["metrics"] for item in facts} == {
            "primary": {"metric:effect": 1.0}, "secondary-bad": {"metric:effect": 2.0},
        }
        assert len({item["variant_run_ref"] for item in facts}) == 1
        rejected_manifest = memory.query(rejected.manifest_ref)
        rejected_entry = next(entry for entry in rejected_manifest.entries if entry.role == "result")
        assert runtime.owners.research_memory.materialize_asset(
            rejected_entry.binding.version_ref
        ).content == rejected_bytes
        accepted_manifest = memory.query(accepted.manifest_ref)
        for role in ("implementation", "log"):
            original = next(entry for entry in rejected_manifest.entries if entry.role == role)
            corrected = next(entry for entry in accepted_manifest.entries if entry.role == role)
            assert original.content_hash == corrected.content_hash
        assert revised_finalizer.finalize(handle=handle, evidence=revised_evidence) == accepted
        assert graph.query_target_formal_results(handle.target_ref) == facts
    finally:
        runtime.close()
