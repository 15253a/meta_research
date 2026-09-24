"""A reused FormalPlan can support independently accepted research Cycles."""
from dataclasses import replace

from meta_research.owners.common import canonical_hash
from test_public_bundle_stage import (
    _AcceptingTargetCandidateProofVerifier,
    _NonDispatchingBundleSkill,
)
from test_public_plan_stage import _DeterministicIdeaSkill, _finish_idea_stage
from test_public_autonomous_creation import _ReadyAutonomousAcquisitionProvider
from test_public_reasoning_stage import (
    _AcceptedAssetRouteReasoningSkill,
    _MultiRunPlanSkill,
    _confirm_deepfetch_quest,
    _finish_plan_and_bundle,
    _reasoning_runtime,
    _tick_reasoning,
)


class _ReplanningGraphBundleSkill(_NonDispatchingBundleSkill):
    def __init__(self):
        super().__init__("replan_required")

    def generate_draft(self, request):
        draft = super().generate_draft(request)
        return replace(draft, primary_session_ref=request.native_session_ref or
            "bundle-reused-plan-" + canonical_hash({"request_ref": request.stage_request_ref})[:20])


class _ReusingPlanReasoningSkill(_AcceptedAssetRouteReasoningSkill):
    def review_draft(self, request, draft):
        return replace(super().review_draft(request, draft),
            review_mode="advisory_unobserved", reviewer_agent_ref=None)


def _replay_graph(runtime, graph):
    run = runtime.owners.agent_runtime.query_bundle_stage_run(graph.request_ref)
    assert run is not None and run.execution is not None
    return runtime.owners.research_graph.accept_target_graph(
        request_ref=run.request_ref, run_ref=run.run_ref,
        attempt_ref=run.attempt_ref, fence_ref=run.fence_ref,
        submission_ref=run.execution.submission_ref,
        context_pack_ref=graph.context_pack_ref,
        target_plan=run.execution.outcome,
        target_plan_hash=run.execution.material_outcome_hash,
        execution_payload_hash=run.execution.payload_hash,
        execution_receipt=run.execution.receipt,
    )


def test_historical_reused_plan_cycles_keep_distinct_graphs_and_projection_receipts(tmp_path, monkeypatch):
    from meta_research import reasoning_contract
    monkeypatch.setattr(reasoning_contract, "REASONING_SUCCESSOR_ENTRY_STAGES",
                        ("idea", "plan", "bundle", "reasoning"))
    runtime = _reasoning_runtime(
        tmp_path / "reused-plan-graphs",
        reasoning_skill=_ReusingPlanReasoningSkill(entry_stage="bundle"),
        idea_skill=_DeterministicIdeaSkill(),
        plan_skill=_MultiRunPlanSkill(no_gap=False),
        bundle_skill=_ReplanningGraphBundleSkill(),
        acquisition_provider=_ReadyAutonomousAcquisitionProvider(),
    )
    # The existing deterministic fixture replaces only external source proof
    # checks; AE/AR/RG create and verify every accepted research fact normally.
    runtime.owners.research_graph._target_candidate_proof_verifier = (
        _AcceptingTargetCandidateProofVerifier()
    )
    try:
        quest = _confirm_deepfetch_quest(runtime)
        _finish_idea_stage(runtime)
        _finish_plan_and_bundle(runtime)
        ae, rg = runtime.owners.advancement_engine, runtime.owners.research_graph
        first_request = ae.query_bundle_stage_request(str(quest["cycle_ref"]))
        assert first_request is not None
        first_graph = rg.query_target_graph(first_request.request_ref)
        assert first_graph is not None and first_graph.targets
        first_projection = rg.query_target_formal_plan_projection(graph_ref=first_graph.graph_ref)
        assert first_projection is not None

        for _ in range(12):
            reasoning = _tick_reasoning(runtime)
            if reasoning["stage_commit"] is not None:
                break
        else:
            raise AssertionError("Reasoning did not accept the reused Plan route")
        successor = ae.query_foreground(str(quest["quest_ref"]))
        assert successor is not None and successor["stage"] == "bundle"
        assert successor["cycle_ref"] != first_request.cycle_ref

        second_graph = second_request = None
        for _ in range(16):
            second_request = ae.query_bundle_stage_request(str(successor["cycle_ref"]))
            if second_request is not None:
                second_graph = rg.query_target_graph(second_request.request_ref)
                if second_graph is not None:
                    break
            # Before the fresh-schema fix this real Owner INSERT fails with
            # UNIQUE constraint failed: rg_target_graphs.formal_plan_ref.
            assert runtime.bundle_stage.process_once()
        assert second_request is not None and second_graph is not None
        assert second_request.accepted_formal_plan == first_request.accepted_formal_plan
        assert second_graph.cycle_ref == second_request.cycle_ref
        assert second_graph.formal_plan_ref == first_graph.formal_plan_ref
        assert second_graph.graph_ref != first_graph.graph_ref
        assert second_graph.run_ref != first_graph.run_ref
        assert {item.target_ref for item in second_graph.targets}.isdisjoint(
            item.target_ref for item in first_graph.targets)

        monkeypatch.setattr(reasoning_contract, "REASONING_SUCCESSOR_ENTRY_STAGES",
                            ("idea", "plan", "reasoning"))
        second_projection = rg.accept_target_formal_plan_projection(
            graph_ref=second_graph.graph_ref, idempotency_key="second-cycle-plan-projection")
        assert second_projection.projection_digest == first_projection.projection_digest
        assert second_projection.receipt != first_projection.receipt
        assert rg.accept_target_formal_plan_projection(
            graph_ref=second_graph.graph_ref, idempotency_key="second-cycle-plan-projection") == second_projection
        assert _replay_graph(runtime, second_graph) == second_graph
        assert _replay_graph(runtime, second_graph) == second_graph
        assert rg.query_target_graph(first_request.request_ref) == first_graph
        assert rg.query_target_formal_plan_projection(graph_ref=first_graph.graph_ref) == first_projection
        assert rg.query_target_graph(second_request.request_ref) == second_graph
        assert rg.query_target_formal_plan_projection(graph_ref=second_graph.graph_ref) == second_projection
    finally:
        runtime.close()
