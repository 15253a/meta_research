"""An accepted generic Plan source becomes an exact, authenticated Target input."""
import pytest

from meta_research.owners.common import OwnerConflict
from meta_research.owners.research_memory import AssetIntakeRequest
from meta_research.plan_skill import _with_derived_answer_contract_hash
import test_public_bundle_stage as fixtures
from test_target_root_finalizer import _CurrentBindingBundleSkill
from test_public_research_asset_roles import _accepted_quest


class _AssetGapPlan(fixtures._DeterministicPlanSkill):
    def __init__(self):
        super().__init__(no_gap=False)
        self.source_ref = None

    def _document(self, request):
        document = super()._document(request)
        entry = {"schema_ref": "meta-research/evidence-source-ref/v1",
            "evidence_ref": self.source_ref, "source_kind": "AssetVersion", "source_ref": self.source_ref}
        use = {"obligation_key": document["gap_set"][0], "evidence_ref": self.source_ref,
            "supported_claim": "The supplied material fixes the observed input for a new comparison.",
            "support_boundary": "Input observation only; comparative performance remains an open gap.",
            "contributing_idea_refs": [request.accepted_idea_set["candidates"][0]["candidate_key"]]}
        document["coverage"][0]["evidence_uses"] = [use]
        document["evidence_reuse_set"] = [use]
        document["additional_evidence_bindings"] = [entry]
        return _with_derived_answer_contract_hash(document, request)


class _InputBundle(_CurrentBindingBundleSkill):
    def __init__(self):
        super().__init__()
        self.source_ref = None

    def _target_plan(self, request):
        document = super()._target_plan(request)
        document["initial_strategy_update"]["candidates"][0]["candidate"][
            "direct_accepted_input_asset_refs"] = [self.source_ref]
        return document


def _runtime(path):
    plan, bundle = _AssetGapPlan(), _InputBundle()
    drafting = fixtures._DeterministicDraftingAdapter()
    runtime = fixtures.build_production_runtime(fixtures.prepare_data_root(path),
        proposal_drafter=drafting, intent_drafting_provider=drafting,
        host_compute_probe=fixtures._DeterministicProbe(),
        idea_skill_provider=fixtures._DeterministicIdeaSkill(), plan_skill_provider=plan,
        bundle_skill_provider=bundle, harness_adapters=(fixtures._FullConformanceAdapter("codex"),),
        power_inhibitor=fixtures._TogglePowerInhibitor())
    return runtime, plan, bundle


def _asset(runtime, key, *, asset_ref=None):
    return runtime.owners.research_memory.submit_asset_intake(AssetIntakeRequest(
        source_kind="text", custody_mode="managed", display_name=key, asset_ref=asset_ref,
        content=(key + ": observed input bytes\n").encode()), idempotency_key=key).asset.as_binding()


def _origin(runtime, binding, quest_ref, key):
    return runtime.owners.research_graph.accept_asset_role(binding=binding,
        role="quest_source_material", quest_ref=quest_ref, idempotency_key=key)


def _accepted_target(runtime, quest):
    fixtures._finish_idea_stage(runtime)
    fixtures._finish_plan_stage(runtime)
    graph = runtime.owners.research_graph
    accepted_graph = None
    for _ in range(16):
        runtime.bundle_stage.process_once()
        request = runtime.bundle_stage.query_current()["stage_run_request"]
        if request is not None:
            accepted_graph = graph.query_target_graph(request["request_ref"])
            if accepted_graph is not None:
                break
    assert accepted_graph is not None, runtime.bundle_stage.query_current()
    request = runtime.owners.advancement_engine.query_bundle_stage_request(quest["cycle_ref"])
    plan = request.accepted_formal_plan
    assert plan.plan_document["gap_set"] and plan.plan_document["experiment_briefs"]
    assert plan.plan_document["bundle_disposition"] == "experiments_required"
    submission = runtime.plan_stage.query_current()["run"]["submission_ref"]
    assert runtime.owners.research_memory.query_plan_document(submission).plan_document == plan.plan_document
    leaves = graph.resolve_plan_evidence_reuse_leaves(
        quest_ref=quest["quest_ref"], accepted_formal_plan=plan)
    assert len(leaves) == 1 and leaves[0].role == "AssetVersion"
    target = accepted_graph.targets[0]
    graph.accept_formal_plan_content(formal_plan_ref=accepted_graph.formal_plan_ref,
        idempotency_key="asset-plan-content")
    graph.accept_target_formal_plan_projection(graph_ref=accepted_graph.graph_ref,
        idempotency_key="asset-plan-projection")
    graph.accept_target_candidate_projection(target_ref=target.target_ref,
        idempotency_key="asset-candidate-projection")
    return target, plan


def test_gap_plan_selected_asset_version_prepares_real_target_proofs_without_drift(tmp_path):
    runtime, plan, bundle = _runtime(tmp_path / "selected-asset")
    try:
        quest = fixtures._confirm_direct_quest(runtime)
        selected = _asset(runtime, "selected-original")
        role = _origin(runtime, selected, quest["quest_ref"], "selected-origin")
        plan.source_ref = bundle.source_ref = selected.version_ref
        target, accepted = _accepted_target(runtime, quest)
        assert accepted.plan_document["source_bindings"]["selected_evidence_catalog"][0]["source_ref"] == selected.version_ref
        assert target.spec["candidate"]["direct_accepted_input_asset_refs"] == [selected.version_ref]
        newer = _asset(runtime, "newer-same-asset", asset_ref=selected.asset_ref)
        _origin(runtime, newer, quest["quest_ref"], "newer-origin")
        authority = runtime.target_run_authorities.research_graph
        memory = runtime.target_run_authorities.research_memory
        assert memory.query_input_asset(target_ref=target.target_ref, asset_ref=selected.asset_ref) is None
        assert authority.prepare_input_assets(target.target_ref) is True
        projection = authority.query_input_asset_projection(target_ref=target.target_ref, asset_ref=selected.asset_ref)
        assert projection.asset == selected and projection.asset != newer
        roles = runtime.owners.research_graph.query_asset_roles(
            quest_ref=quest["quest_ref"], version_refs=(selected.version_ref,))
        assert role in roles
        input_role = next(item for item in roles if item.role_ref == projection.source_role_ref)
        assert input_role.asset_binding() == selected
        assert projection.source_role_receipt == input_role.receipt
        assert memory.query_input_asset(target_ref=target.target_ref, asset_ref=selected.asset_ref) == (
            selected, projection.rm_proof_receipt)
        proof = authority.query_bundle_input_asset_proof(target_ref=target.target_ref, asset_ref=selected.asset_ref)
        assert proof.rm_acceptance_receipt.verified and proof.rg_role_receipt.verified
        assert authority.resolve_input_asset_ref(target_ref=target.target_ref, input_ref=selected.version_ref) == selected.asset_ref
        assert runtime.owners.research_memory.materialize_asset(selected.version_ref).content == b"selected-original: observed input bytes\n"
        assert authority.prepare_input_assets(target.target_ref) is False
        assert authority.query_input_asset_projection(target_ref=target.target_ref, asset_ref=selected.asset_ref) == projection
    finally:
        runtime.close()


@pytest.mark.parametrize("kind", ["unselected", "foreign_quest", "no_origin"])
def test_target_cannot_add_an_asset_outside_the_formal_plan_selection(tmp_path, kind):
    runtime, plan, bundle = _runtime(tmp_path / kind)
    try:
        quest = fixtures._confirm_direct_quest(runtime)
        foreign = _accepted_quest(runtime) if kind == "foreign_quest" else None
        selected = _asset(runtime, "selected")
        _origin(runtime, selected, quest["quest_ref"], "selected-origin")
        rejected = _asset(runtime, "rejected")
        if kind == "unselected":
            _origin(runtime, rejected, quest["quest_ref"], "unselected-origin")
        elif kind == "foreign_quest":
            _origin(runtime, rejected, foreign.quest_ref, "foreign-origin")
        plan.source_ref, bundle.source_ref = selected.version_ref, rejected.version_ref
        target, _ = _accepted_target(runtime, quest)
        authority = runtime.target_run_authorities.research_graph
        with pytest.raises(OwnerConflict, match="target_input_asset_version_not_selected"):
            authority.prepare_input_assets(target.target_ref)
        assert authority.query_input_asset_projection(target_ref=target.target_ref, asset_ref=rejected.asset_ref) is None
        assert runtime.target_run_authorities.research_memory.query_input_asset(
            target_ref=target.target_ref, asset_ref=rejected.asset_ref) is None
    finally:
        runtime.close()


@pytest.mark.parametrize("kind", ["foreign_quest", "no_origin"])
def test_formal_plan_cannot_adopt_an_asset_without_same_quest_origin(tmp_path, kind):
    runtime, plan, bundle = _runtime(tmp_path / ("invalid-plan-" + kind))
    try:
        quest = fixtures._confirm_direct_quest(runtime)
        foreign = _accepted_quest(runtime) if kind == "foreign_quest" else None
        source = _asset(runtime, "unavailable-plan-source")
        if foreign is not None:
            _origin(runtime, source, foreign.quest_ref, "foreign-plan-origin")
        plan.source_ref = bundle.source_ref = source.version_ref
        fixtures._finish_idea_stage(runtime)
        with pytest.raises(OwnerConflict, match="asset_quest_scope_invalid"):
            for _ in range(16):
                assert runtime.plan_stage.process_once()
        current = runtime.plan_stage.query_current()
        assert current["stage_commit"] is None
        assert current["plan_acceptance"]["domain"]["status"] != "accepted"
        assert runtime.owners.research_graph.query_snapshot().facts["formal_plan_count"] == 0
        assert runtime.owners.advancement_engine.query_bundle_stage_request(quest["cycle_ref"]) is None
    finally:
        runtime.close()
