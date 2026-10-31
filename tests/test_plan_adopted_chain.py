"""Real Owners retain a complete adopted set across an unarranged-gap handoff."""
from copy import deepcopy

from meta_research.owners.research_memory import AssetIntakeRequest
from meta_research.research_content import read_content
from meta_research.target_commit_evidence import TargetCommitEvidenceCatalog
from test_public_plan_stage import (
    _runtime, _confirm_direct_quest, _finish_idea_stage,
    _DeterministicIdeaSkill, _DeterministicPlanSkill,
)
from test_plan_generic_sources import _finish_plan_and_skipped_bundle


class _UnarrangedPlan(_DeterministicPlanSkill):
    def __init__(self):
        super().__init__(no_gap=False)
        self.selected = []

    def _document(self, request):
        document = super()._document(request)
        document["experiment_briefs"] = []
        document["bundle_disposition"] = "no_new_experiment_required"
        document["coverage"][0]["insufficiency"] = (
            "The materials establish possible methods, but this Cycle lacks the necessary "
            "data for the comparison. Its conclusion remains open; a later Cycle must "
            "obtain the data before scheduling that work."
        )
        document["source_bindings"]["selected_evidence_bindings"] = deepcopy(self.selected)
        return document


def test_complete_adoption_survives_skipped_bundle_without_source_readmission(tmp_path, monkeypatch):
    calls = []
    verify = TargetCommitEvidenceCatalog.verify_plan_evidence_catalog

    def counted(authority, **arguments):
        calls.append((arguments["require_current"], len(arguments["evidence_catalog"])))
        return verify(authority, **arguments)

    monkeypatch.setattr(TargetCommitEvidenceCatalog, "verify_plan_evidence_catalog", counted)
    plan = _UnarrangedPlan()
    runtime = _runtime(tmp_path / "unarranged", idea_skill=_DeterministicIdeaSkill(), plan_skill=plan)
    try:
        quest = _confirm_direct_quest(runtime)
        memory, graph = runtime.owners.research_memory, runtime.owners.research_graph
        for index in range(33):
            asset = memory.submit_asset_intake(AssetIntakeRequest(
                source_kind="text", custody_mode="managed", display_name=f"method-{index}",
                content=f"Candidate method {index}; evaluation remains unperformed.\n".encode()),
                idempotency_key=f"material-{index}").asset
            graph.accept_asset_role(binding=asset.as_binding(), role="quest_source_material",
                quest_ref=quest["quest_ref"], idempotency_key=f"role-{index}")
            plan.selected.append(read_content(graph, memory, quest_ref=quest["quest_ref"],
                source_ref=asset.version_ref, version_ref=asset.version_ref)["selected_evidence_binding"])
        _finish_idea_stage(runtime)
        _finish_plan_and_skipped_bundle(runtime)
        # The source graph is admitted once by its existing Owner at acceptance.
        assert calls == [(True, 33)]
        request = runtime.owners.advancement_engine.query_bundle_stage_request(quest["cycle_ref"])
        assert request.accepted_formal_plan.plan_document["source_bindings"]["selected_evidence_bindings"] == plan.selected
        assert request.accepted_formal_plan.plan_document["gap_set"] == ["rare-morphology-comparison"]
        assert runtime.bundle_stage.query_current()["stage_commit"]["disposition"] == "skipped"
        submission = runtime.plan_stage.query_current()["run"]["submission_ref"]
        assert memory.query_plan_document(submission).plan_document == request.accepted_formal_plan.plan_document
        assert graph.query_formal_plan_decision(submission).decision == "accepted"
        # Reasoning request creation consumes the same FormalPlan even with no Bundle run.
        runtime.reasoning_stage.process_once()
        reasoning = runtime.owners.advancement_engine.query_reasoning_stage_request(quest["cycle_ref"])
        assert reasoning is not None
        downstream = reasoning.context_pack["plan_evidence_input"]
        assert downstream["formal_plan_binding"] == request.accepted_formal_plan.as_dict()
        assert downstream["evidence_reuse_set"] == []
        assert calls == [(True, 33)]
    finally:
        runtime.close()
