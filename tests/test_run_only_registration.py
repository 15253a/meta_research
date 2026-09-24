import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from meta_research.bundle_protocol import AcceptedInputAssetProof, ReceiptProof
from meta_research.owners.common import OwnerConflict, canonical_json
from meta_research.run_only_registration import _source_inputs, _unassessed_inventory
from meta_research.target_run_finalizer import TargetRunFinalizer
from test_target_root_finalizer import _EvidenceReader, _root_finalizer_fixture


def _finalizer(runtime, lifecycle, memory, evidence):
    return TargetRunFinalizer(lifecycle=lifecycle, memory=memory,
        workspace_resolver=runtime.target_run_authorities.agent_runtime,
        evidence_reader=_EvidenceReader(evidence),
        measurement_authority=runtime.owners.research_graph,
        graph_authority=runtime.owners.research_graph)


def _declare(workspace, evidence):
    path = workspace / "outputs/metrics.json"
    document = json.loads(path.read_text())
    paths = [artifact.relative_path for artifact in evidence.handoff.artifacts if artifact.role == "checkpoint"]
    document["formal_runs"] = [
        {"run_key": "actual-a", "status": "executed", "checkpoint_paths": paths,
         "evaluations": [{"attempt_key": "missing-prerequisite", "status": "blocked"}]},
        {"run_key": "actual-b", "status": "executed", "checkpoint_paths": [], "evaluations": []},
        {"run_key": "never-executed", "status": "cancelled"},
    ]
    path.write_text(canonical_json(document))
    return path, document


def test_unassessed_runs_are_accepted_replayable_and_do_not_invent_assessments(tmp_path):
    runtime, lifecycle, memory, authority, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        path, document = _declare(workspace, evidence)
        document["metrics"] = {}
        path.write_text(canonical_json(document))
        finalizer = _finalizer(runtime, lifecycle, memory, evidence)
        completed = finalizer.finalize(handle=handle, evidence=evidence)
        assert completed.status == "completed"
        assert completed.pending_code is None and completed.target_commit_ref
        published = runtime.owners.agent_runtime.publish_target_root_completion(
            target_ref=handle.target_ref, completion_ref=completed.completion_ref,
            target_commit_ref=completed.target_commit_ref)
        assert published.terminal.evaluation_attempt_ref is None
        assert published.terminal.metric_result_ref is None
        assert published.terminal.evaluation_attempt_input_binding is None
        assert published.terminal.rg_formal_measurement_receipt is None
        assert lifecycle.query(handle.target_ref).status == "completed"
        graph = runtime.owners.research_graph
        facts = graph.query_target_formal_results(handle.target_ref)
        assert len(facts) == 2
        assert {item["run_key"] for item in facts} == {"actual-a", "actual-b"}
        assert all(item["variant_run"]["status"] == "executed" for item in facts)
        assert all(item["evaluation_attempt"] is None and item["metric_result"] is None for item in facts)
        assert all(item["variant_run"]["inputs"]["manifest_ref"] == completed.manifest_ref for item in facts)
        assert finalizer.finalize(handle=handle, evidence=evidence) == completed
        assert graph.query_target_formal_results(handle.target_ref) == facts
        with runtime._database.read() as connection:
            for table in ("rg_evaluation_attempts", "rg_metric_results", "rg_target_root_unassessed_runs"):
                assert connection.execute(text("SELECT count(*) FROM " + table)).scalar_one() == 0
            assert connection.execute(text("SELECT count(*) FROM rg_variant_runs")).scalar_one() == 2
            assert connection.execute(text("SELECT count(*) FROM rg_target_commits")).scalar_one() == 1
            stored = connection.exec_driver_sql('SELECT r.evaluation_attempt_ref,r.metric_result_ref,r.evaluation_input_binding_json,c.closure_json '
                'FROM rg_target_root_measurements r JOIN rg_target_commits c USING (target_ref)').first()
            assert stored.evaluation_attempt_ref is stored.metric_result_ref is None
            assert json.loads(stored.evaluation_input_binding_json) is None
            closure = json.loads(stored.closure_json)
            root = closure['root_measurement']
            assert root['evaluation_attempt_ref'] is root['metric_result_ref'] is None
            assert root['receipt']['kind'] == 'target_root_work_accepted'
            assert root['receipt']['subject_ref'] == root['measurement_ref']
        # Published execution remains immutable; later assessment belongs to a
        # new work unit reusing these exact Run refs (covered by cross-target test).
        path.write_text("changed after publication")
        assert finalizer.finalize(handle=handle, evidence=evidence) == completed
        assert graph.query_target_formal_results(handle.target_ref) == facts
    finally:
        runtime.close()


def test_unassessed_run_query_rechecks_original_binding_and_rolls_back_invalid_inventory(tmp_path):
    runtime, lifecycle, memory, _, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        path, document = _declare(workspace, evidence)
        document["formal_runs"][1]["variant_ref"] = "missing-variant"
        path.write_text(canonical_json(document))
        with pytest.raises(OwnerConflict, match="variant_definition_invalid"):
            _finalizer(runtime, lifecycle, memory, evidence).finalize(handle=handle, evidence=evidence)
        with runtime._database.read() as connection:
            assert connection.execute(text("SELECT count(*) FROM rg_variant_runs")).scalar_one() == 0
            assert connection.execute(text("SELECT count(*) FROM rg_experiment_input_bindings")).scalar_one() == 0
        # The rejected registration is atomic, despite the first valid execution.
    finally:
        runtime.close()


def test_unassessed_run_query_detects_tampered_native_binding(tmp_path):
    runtime, lifecycle, memory, _, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        _declare(workspace, evidence)
        _finalizer(runtime, lifecycle, memory, evidence).finalize(handle=handle, evidence=evidence)
        graph = runtime.owners.research_graph
        facts = graph.query_target_formal_results(handle.target_ref)
        ref = facts[0]["variant_run"]["input_binding_ref"]
        with runtime._database.write() as connection:
            connection.execute(text("UPDATE rg_experiment_input_bindings SET inputs_hash=:bad WHERE binding_ref=:ref"),
                               {"bad": "0" * 64, "ref": ref})
        with pytest.raises(OwnerConflict, match="integrity_invalid|experiment_input_binding_invalid"):
            graph.query_target_formal_results(handle.target_ref)
    finally:
        runtime.close()


@pytest.mark.parametrize("problem", ["input_refs", "wrong_subject"])
def test_reusing_a_run_rejects_changed_inputs_or_another_runs_valid_binding(tmp_path, problem):
    from meta_research.formal_entities import register_root_entities
    from test_reused_evaluation_source import _source_packet
    runtime, lifecycle, memory, _, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        _declare(workspace, evidence)
        _finalizer(runtime, lifecycle, memory, evidence).finalize(handle=handle, evidence=evidence)
        graph = runtime.owners.research_graph
        facts = {item["run_key"]: item for item in graph.query_target_formal_results(handle.target_ref)}
        packet = _source_packet(runtime, handle.target_ref)
        for item in packet["work_items"]:
            item["reuse_evaluation_attempt"] = False
        if problem == "input_refs":
            packet["work_items"][0]["input_refs"] = []
        else:
            with runtime._database.write() as connection:
                connection.execute(text("UPDATE rg_variant_runs SET input_binding_ref='test-swap' WHERE variant_run_ref=:run"),
                    {"run": facts["actual-a"]["variant_run_ref"]})
                connection.execute(text("UPDATE rg_variant_runs SET input_binding_ref=:binding WHERE variant_run_ref=:run"),
                    {"binding": facts["actual-a"]["variant_run"]["input_binding_ref"], "run": facts["actual-b"]["variant_run_ref"]})
                connection.execute(text("UPDATE rg_variant_runs SET input_binding_ref=:binding WHERE variant_run_ref=:run"),
                    {"binding": facts["actual-b"]["variant_run"]["input_binding_ref"], "run": facts["actual-a"]["variant_run_ref"]})
        with pytest.raises(OwnerConflict, match="reused_run"):
            with runtime._database.read() as connection:
                register_root_entities(connection, **packet, source_owner=graph, verify_only=True)
        with runtime._database.read() as connection:
            assert connection.execute(text("SELECT count(*) FROM rg_variant_runs")).scalar_one() == 2
            assert connection.execute(text("SELECT count(*) FROM rg_target_commits")).scalar_one() == 1
            for table in ("rg_evaluation_attempts", "rg_metric_results"):
                assert connection.execute(text("SELECT count(*) FROM " + table)).scalar_one() == 0
    finally:
        runtime.close()


def test_explicit_nonexecution_never_becomes_an_unassessed_run():
    document = {"formal_runs": [{"run_key": "claimed-run", "status": "executed", "evaluations": []}],
        "execution_hierarchy": {"variant_run": {"status": "not_instantiated"},
                                "evaluation_attempt": {"status": "not_started"}},
        "input_admission": {"variant_run_count": 0, "evaluation_attempt_count": 0,
                            "training_process_started": False, "evaluation_process_started": False}}
    assert _unassessed_inventory(document) is None
    assert _unassessed_inventory({"formal_runs": [{"run_key": "cancelled", "status": "cancelled"}]}) is None


def test_exact_asset_version_can_be_selected_from_accepted_receipt():
    proof = AcceptedInputAssetProof(asset_ref="asset", rm_acceptance_receipt=ReceiptProof(
        receipt_ref="rm-receipt", subject_ref="asset-version-7", verified=True,
        currentness_known=True, current=True), rg_role_receipt=ReceiptProof(
        receipt_ref="rg-role", subject_ref="role", verified=True, currentness_known=True, current=True))
    receipt = SimpleNamespace(as_public_dict=lambda: {"receipt_ref": "receipt"})
    completion = SimpleNamespace(handle=SimpleNamespace(accepted_input_asset_proofs=(proof,),
        accepted_input_target_commit_refs=("upstream",), target_run_ref="target-run"),
        completion_ref="completion", payload_hash="completion-hash", receipt=receipt)
    manifest = SimpleNamespace(implementation_revision_ref="implementation", implementation_tree_hash="tree-hash",
        manifest_ref="manifest", payload_hash="manifest-hash", receipt=receipt,
        entries=(SimpleNamespace(as_dict=lambda: {'role': 'implementation', 'tree_hash': 'tree-hash',
            'declared_relative_path': 'implementation'}),),
        result_document=SimpleNamespace(as_dict=lambda: {'formal_runs': [{'run_key': 'actual'}]}))
    target = SimpleNamespace(target_ref="target", spec_hash="spec-hash")
    authority = SimpleNamespace(authority_ref="authority", authority_hash="authority-hash")
    selected = _source_inputs(completion, manifest, target, authority,
        {"run_key": "actual", "input_refs": ["asset-version-7", "upstream"]})
    assert selected["input_refs"] == ["asset-version-7", "upstream"]
    with pytest.raises(OwnerConflict, match="input_reference_invalid"):
        _source_inputs(completion, manifest, target, authority,
            {"run_key": "actual", "input_refs": ["unadmitted-version"]})
