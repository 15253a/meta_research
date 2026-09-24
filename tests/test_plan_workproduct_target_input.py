"""A real unmeasured WorkProduct can supply a later, still-open experiment."""
from dataclasses import replace
import hashlib
import subprocess
import sys
import time
import json

from sqlalchemy import text

from meta_research.owners.common import canonical_hash, canonical_json
from meta_research.plan_skill import _with_derived_answer_contract_hash
from meta_research.target_commit_evidence import TargetCommitEvidenceCatalog
from meta_research.target_run_finalizer import TargetRunFinalizer
import test_public_bundle_stage as fixtures
from test_public_first_question_deepfetch import (SnapshotAwareProposalDrafter,
    DeterministicDeepFetchProvider, DeterministicProbe)
from test_public_manual_question_lifecycle import DeterministicAcquisitionProvider
from test_public_reasoning_stage import (_confirm_deepfetch_quest,
    _MultiRunIdeaSkill, _MultiRunPlanSkill, _MultiRunReasoningSkill)
from test_research_notes_and_call_observations import _SystemEvidenceReader
from test_target_root_finalizer import _CurrentBindingBundleSkill, _root_finalizer_fixture
from test_two_cycle_actual_work import _ready_existing, _finish_stage


class _Plan(_MultiRunPlanSkill):
    def __init__(self):
        super().__init__(no_gap=False)
        self.entry = None

    def _document(self, request):
        document = super()._document(request)
        if self.entry is not None:
            discovery = next(item for item in request.context_pack["evidence_catalog_page"]["projections"]
                if item["evidence_ref"] == self.entry["evidence_ref"])
            assert discovery["research_summary"]["metric_count"] == 0
            assert discovery["research_summary"]["metric_names"] == []
            use = {"obligation_key": document["gap_set"][0], "evidence_ref": self.entry["evidence_ref"],
                "supported_claim": "The earlier run produced the retained material needed for a new comparison.",
                "support_boundary": "Work was executed; no evaluation or comparative conclusion is claimed.",
                "contributing_idea_refs": [request.accepted_idea_set["candidates"][0]["candidate_key"]]}
            document["coverage"][0]["evidence_uses"] = [use]
            document["evidence_reuse_set"] = [use]
            document["additional_evidence_bindings"] = [self.entry]
        return _with_derived_answer_contract_hash(document, request)


class _Bundle(_CurrentBindingBundleSkill):
    def __init__(self):
        super().__init__()
        self.input_ref = None

    def _target_plan(self, request):
        result = super()._target_plan(request)
        if self.input_ref is not None:
            result["initial_strategy_update"]["candidates"][0]["candidate"][
                "direct_accepted_input_asset_refs"] = [self.input_ref]
        return result

    def generate_draft(self, request):
        draft = super().generate_draft(request)
        return replace(draft, primary_session_ref=request.native_session_ref or
            "bundle-primary-" + canonical_hash(request.stage_request_ref)[:20])


class _Reasoning(_MultiRunReasoningSkill):
    def review_draft(self, request, draft):
        return replace(super().review_draft(request, draft),
            review_mode="advisory_unobserved", reviewer_agent_ref=None)


def test_unmeasured_workproduct_plan_selection_prepares_target_input_with_no_evaluation(tmp_path):
    plan, bundle = _Plan(), _Bundle()
    runtime = fixtures.build_production_runtime(fixtures.prepare_data_root(tmp_path / "workproduct"),
        proposal_drafter=SnapshotAwareProposalDrafter(),
        intent_drafting_provider=fixtures._DeterministicDraftingAdapter(),
        host_compute_probe=DeterministicProbe(), deepfetch_provider=DeterministicDeepFetchProvider(),
        acquisition_provider=DeterministicAcquisitionProvider(),
        idea_skill_provider=_MultiRunIdeaSkill(), plan_skill_provider=plan, bundle_skill_provider=bundle,
        reasoning_skill_provider=_Reasoning(entry_stage="plan"),
        harness_adapters=(fixtures._FullConformanceAdapter("codex"),),
        power_inhibitor=fixtures._TogglePowerInhibitor())
    try:
        quest = _confirm_deepfetch_quest(runtime)
        fixtures._finish_idea_stage(runtime)
        fixtures._finish_plan_stage(runtime)
        ready = _ready_existing(runtime)
        _, lifecycle, memory, source_authority, handle, workspace, prior = _root_finalizer_fixture(
            tmp_path, runtime=runtime, ready=ready)
        script = workspace / "implementation/train.py"
        script.write_text("print('actual retained work product')\n")
        output = subprocess.check_output([sys.executable, str(script)], cwd=workspace)
        (workspace / "outputs/data").mkdir()
        (workspace / "outputs/data/work.txt").write_bytes(output)
        (workspace / "outputs/result.json").write_text(canonical_json({
            "schema_ref": source_authority.measurement_contract.result_schema_ref,
            "metrics": {}, "result_disposition": "uncertain",
            "formal_runs": [{"run_key": "produced-material", "implementation_paths": ["implementation"],
                "checkpoint_paths": [], "artifact_paths": ["outputs/data/work.txt"], "evaluations": []}]}))
        workspace_ref, _ = runtime.target_run_authorities.agent_runtime.resolve_target_workspace(
            target_ref=handle.target_ref, target_run_ref=handle.target_run_ref,
            root_session_ref=handle.root_session_ref, attempt_ref=handle.execution_attempt_ref,
            fence_ref=handle.execution_fence_ref)
        summary = "Executed one material-producing script; no evaluation was performed."
        evidence = replace(prior, handoff=None, workspace_ref=workspace_ref, final_text=summary,
            final_text_sha256=hashlib.sha256(summary.encode()).hexdigest())
        finalizer = TargetRunFinalizer(lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_SystemEvidenceReader(), measurement_authority=runtime.owners.research_graph,
            graph_authority=runtime.owners.research_graph)
        completed = finalizer.finalize(handle=handle, evidence=evidence)
        assert completed.status == "completed"
        manifest = memory.query(completed.manifest_ref)
        retained = next(entry for entry in manifest.entries
            if entry.declared_relative_path == "outputs/data/work.txt")
        assert runtime.owners.research_memory.materialize_asset(retained.binding.version_ref).content == output
        assert output == b"actual retained work product\n"
        runtime.owners.agent_runtime.publish_target_root_completion(target_ref=handle.target_ref,
            completion_ref=completed.completion_ref, target_commit_ref=completed.target_commit_ref)
        graph = runtime.owners.research_graph
        cleanup = runtime.target_run_runtime.cleanup_completed_workspaces(dry_run=False, now=time.time()+90000)
        assert len(cleanup) == 1 and cleanup[0]["action"] == "removed" and cleanup[0]["bytes"] > 0, cleanup
        assert not workspace.exists()
        assert runtime.owners.research_memory.materialize_asset(retained.binding.version_ref).content == output
        facts = graph.query_target_formal_results(handle.target_ref)
        assert len(facts) == 1 and facts[0]["variant_run"]["status"] == "executed"
        assert facts[0]["evaluation_attempt"] is facts[0]["metric_result"] is None
        _finish_stage(runtime, "bundle")
        catalog = TargetCommitEvidenceCatalog(graph, runtime.owners.research_memory)
        _, entries = catalog.query_plan_evidence_catalog(quest_ref=quest["quest_ref"],
            target_commit_refs=(completed.target_commit_ref,))
        assert len(entries) == 1
        plan.entry = entries[0]
        bundle.input_ref = entries[0]["evidence_ref"]
        _finish_stage(runtime, "reasoning")
        next_cycle = runtime.owners.advancement_engine.query_foreground(quest["quest_ref"])["cycle_ref"]
        assert next_cycle != quest["cycle_ref"]
        _finish_stage(runtime, "plan")
        second_graph = None
        for _ in range(20):
            runtime.bundle_stage.process_once()
            request = runtime.owners.advancement_engine.query_bundle_stage_request(next_cycle)
            if request is not None:
                second_graph = graph.query_target_graph(request.request_ref)
                if second_graph is not None:
                    break
        assert second_graph is not None
        accepted = request.accepted_formal_plan
        assert accepted.plan_document["gap_set"] and accepted.plan_document["experiment_briefs"]
        leaves = graph.resolve_plan_evidence_reuse_leaves(quest_ref=quest["quest_ref"], accepted_formal_plan=accepted)
        assert len(leaves) == 1 and leaves[0].role == "WorkProduct"
        assert leaves[0].target_commit_ref == completed.target_commit_ref
        assert leaves[0].source_evaluation_attempt_ref is leaves[0].formal_measurement_acceptance_receipt is None
        target = second_graph.targets[0]
        graph.accept_formal_plan_content(formal_plan_ref=second_graph.formal_plan_ref,
            idempotency_key="workproduct-plan-content")
        graph.accept_target_formal_plan_projection(graph_ref=second_graph.graph_ref,
            idempotency_key="workproduct-plan-projection")
        graph.accept_target_candidate_projection(target_ref=target.target_ref,
            idempotency_key="workproduct-candidate-projection")
        authority = runtime.target_run_authorities.research_graph
        assert authority.prepare_input_assets(target.target_ref) is True
        source_asset = runtime.owners.research_memory.query_asset_version(entries[0]["asset_version_ref"]).as_binding()
        projection = authority.query_input_asset_projection(target_ref=target.target_ref, asset_ref=source_asset.asset_ref)
        assert projection.asset == source_asset
        assert runtime.target_run_authorities.research_memory.query_input_asset(
            target_ref=target.target_ref, asset_ref=source_asset.asset_ref) == (source_asset, projection.rm_proof_receipt)
        proof = authority.query_bundle_input_asset_proof(target_ref=target.target_ref, asset_ref=source_asset.asset_ref)
        assert proof.rm_acceptance_receipt.verified and proof.rg_role_receipt.verified
        assert authority.selected_evidence_target_commits(target.target_ref) == {
            completed.target_commit_ref: (entries[0]["evidence_ref"],)}
        assert authority.prepare_input_assets(target.target_ref) is False
        assert graph.query_target_formal_results(handle.target_ref) == facts
        print("T16_SUCCESSOR_ADOPTION " + json.dumps({"cleanup":cleanup,
            "source_commit":completed.target_commit_ref,"retained_version":retained.binding.version_ref,
            "source_cycle":quest["cycle_ref"],"successor_cycle":next_cycle,
            "accepted_plan":accepted.formal_plan_ref,"adopted_evidence":entries[0]["evidence_ref"],
            "successor_target":target.target_ref,"input_version":projection.asset.version_ref,
            "rm_proof_receipt":projection.rm_proof_receipt.receipt_ref,
            "rg_proof_receipt":projection.rg_proof_receipt.receipt_ref},sort_keys=True))
        with runtime._database.read() as connection:
            for table in ("rg_evaluation_attempts", "rg_metric_results"):
                assert connection.execute(text("SELECT COUNT(*) FROM " + table)).scalar_one() == 0
    finally:
        runtime.close()
