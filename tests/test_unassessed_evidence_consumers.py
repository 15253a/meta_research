"""Real work stays readable without becoming a nonexistent scientific metric."""
import json

import pytest
from sqlalchemy import text

from meta_research.owners.common import OwnerConflict, canonical_json
from meta_research.owners.research_memory import _frozen_reasoning_evidence_closure
from meta_research.target_commit_evidence import (
    TargetCommitEvidenceCatalog,
    target_commit_evidence_document,
    target_commit_evidence_provenance,
    target_commit_metric_result,
)
from test_root_formal_entities import _accept
from test_target_root_finalizer import _root_finalizer_fixture


@pytest.mark.parametrize("assessment", [None, "failed", "executed"])
def test_owner_work_publication_and_reasoning_only_cite_real_metrics(tmp_path, assessment):
    runtime, lifecycle, memory, authority, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        path = workspace / "outputs/metrics.json"
        document = json.loads(path.read_text())
        metrics = document["metrics"]
        evaluations = [] if assessment is None else [{
            "attempt_key": "assessment", "status": assessment,
            **({"metrics": metrics} if assessment == "executed" else {}),
        }]
        document["formal_runs"] = [{"run_key": "actual-work", "evaluations": evaluations}]
        document["metrics"] = metrics if assessment == "executed" else {}
        document["result_disposition"] = "uncertain"
        path.write_text(canonical_json(document))
        accepted, manifest = _accept(runtime, lifecycle, memory, handle, evidence)
        graph = runtime.owners.research_graph
        commit = next(item for item in graph.query_target_commits(authority.graph_ref)
                      if item.commit_ref == accepted.target_commit_ref)
        graph_record = graph.query_target_graph(authority.stage_request_ref)
        measured = assessment == "executed"

        # Publication must remain retryable for an execution, failed assessment,
        # or measured result. It preserves the exact result source in each case.
        runtime.bundle_stage._publish_target_commit_evidence(quest_ref=graph_record.quest_ref, commit=commit)
        runtime.bundle_stage._publish_target_commit_evidence(quest_ref=graph_record.quest_ref, commit=commit)
        result_entry = next(entry for entry in manifest.entries if entry.role == "result")
        assert runtime.owners.research_memory.materialize_asset(result_entry.binding.version_ref).content == path.read_bytes()
        projected = target_commit_evidence_document(commit)
        assert projected["result_content"] == commit.closure["result_content"]
        assert (projected["metric_result"] is not None) == measured
        assert (target_commit_metric_result(commit) is not None) == measured
        provenance = target_commit_evidence_provenance(commit)
        assert ("metric_result" in provenance["capabilities"]) == measured
        assert None not in provenance["provenance_closure_refs"]
        published = [runtime.owners.research_memory.query_asset_version(role.version_ref)
                     for role in graph.query_asset_roles(quest_ref=graph_record.quest_ref, role="evidence")]
        published = [asset for asset in published if asset.provenance.get("target_commit_root_ref") == commit.commit_ref]
        assert len(published) == 1
        assert json.loads(runtime.owners.research_memory.materialize_asset(published[0].version_ref).content) == projected

        catalog = TargetCommitEvidenceCatalog(graph, runtime.owners.research_memory)
        _, entries = catalog.query_plan_evidence_catalog(quest_ref=graph_record.quest_ref,
            target_commit_refs=(commit.commit_ref,))
        # Every accepted TargetCommit is discoverable evidence (ADR 0005);
        # measured work carries its metric leaf, unmeasured work a WorkProduct
        # leaf, with no measurement manufactured for it.
        assert len(entries) == 1
        leaves = catalog.resolve_reasoning_target_evidence_leaves(quest_ref=graph_record.quest_ref,
            target_commit_refs=(commit.commit_ref,))
        assert len(leaves) == 1
        assert all(leaf.role == ("MetricResult" if measured else "WorkProduct") for leaf in leaves)
        if not measured:
            work = leaves[0]
            assert work.evidence_item_ref == commit.commit_ref
            assert work.source_subject_kind == "VariantRun"
            assert work.formal_measurement_acceptance_receipt is None
        assert graph.resolve_reasoning_target_evidence_leaves(quest_ref=graph_record.quest_ref,
            target_commit_refs=(commit.commit_ref,)) == leaves
        with pytest.raises(OwnerConflict, match="reasoning_target_evidence_closure_invalid"):
            catalog.resolve_reasoning_target_evidence_leaves(quest_ref=graph_record.quest_ref,
                target_commit_refs=("target-commit-does-not-exist",))

        # The same Owner-issued closure reaches RM's scientific citation gate.
        # It remains in the frozen input even when there is no MetricResult leaf.
        terminal = commit.closure["accepted_measurement"]
        if not measured:
            assert terminal["metric_result_ref"] is None
            assert terminal["rg_formal_measurement_receipt"] is None
            assert commit.closure["root_measurement"]["metric_result_ref"] is None
        if assessment is None:
            assert terminal["evaluation_attempt_ref"] is None
            assert terminal["evaluation_attempt_input_binding"] is None
        context = {"question_literature_input": {"kind": "none"},
                   "plan_evidence_input": {"kind": "none", "basis_stage_commit_refs": []},
                   "accepted_target_commit_closures": [terminal]}
        frozen = _frozen_reasoning_evidence_closure(context, revision_verifier=None)
        assert len(frozen) == 1
        if measured:
            assert frozen[0]["kind"] == "MetricResult"
            assert frozen[0]["ref"] == terminal["metric_result_ref"]
        else:
            assert frozen[0]["kind"] == "WorkProduct"
            assert frozen[0]["ref"] == commit.commit_ref
            assert frozen[0]["owner_acceptance_receipt_ref"] == commit.receipt.receipt_ref
        assert context["accepted_target_commit_closures"] == [terminal]
        facts = graph.query_target_formal_results(handle.target_ref)
        assert (facts[0]["evaluation_attempt"] is not None) == (assessment is not None)
        assert (facts[0]["metric_result"] is not None) == measured
        if assessment == "failed":
            assert facts[0]["evaluation_attempt"]["status"] == "failed"
        # A no-measurement flag is not itself authority: the Commit must still
        # pass the Owner's original content/receipt verification before skipping.
        tampered = json.loads(canonical_json(commit.closure))
        tampered["accepted_measurement"]["formal_measurement_accepted"] = False
        tampered["invented_note"] = "not in the accepted content"
        with runtime._database.write() as connection:
            connection.execute(text("UPDATE rg_target_commits SET closure_json=:value WHERE commit_ref=:ref"),
                               {"value": canonical_json(tampered), "ref": commit.commit_ref})
        with pytest.raises(OwnerConflict, match="target_commit_invalid"):
            catalog.resolve_reasoning_target_evidence_leaves(quest_ref=graph_record.quest_ref,
                target_commit_refs=(commit.commit_ref,))
    finally:
        runtime.close()
