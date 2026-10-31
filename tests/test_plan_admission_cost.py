"""Consumers reuse accepted Plan identities without repeating source admission."""
from meta_research.owners.research_memory import AssetIntakeRequest
from meta_research.target_commit_evidence import TargetCommitEvidenceCatalog
from test_plan_generic_sources import _GenericPlan, _finish_plan_and_skipped_bundle
from test_public_plan_stage import _runtime, _confirm_direct_quest, _finish_idea_stage, _DeterministicIdeaSkill


def test_accepted_plan_consumers_admit_the_source_only_at_acceptance(tmp_path, monkeypatch):
    calls = []
    original = TargetCommitEvidenceCatalog.verify_plan_evidence_catalog

    def counted(authority, **arguments):
        if arguments["evidence_catalog"]:
            calls.append((arguments.get("require_current", True), len(arguments["evidence_catalog"])))
        return original(authority, **arguments)

    monkeypatch.setattr(TargetCommitEvidenceCatalog, "verify_plan_evidence_catalog", counted)
    plan = _GenericPlan("AssetVersion")
    runtime = _runtime(tmp_path / "one-source", idea_skill=_DeterministicIdeaSkill(), plan_skill=plan)
    plan.runtime = runtime
    try:
        quest = _confirm_direct_quest(runtime)
        graph, memory = runtime.owners.research_graph, runtime.owners.research_memory
        asset = memory.submit_asset_intake(AssetIntakeRequest(
            source_kind="text", custody_mode="managed", display_name="Plan fixture observation",
            content=b"Test observation: visible only under stated lighting.\n"),
            idempotency_key="cost-source").asset
        graph.accept_asset_role(binding=asset.as_binding(), role="quest_source_material",
            quest_ref=quest["quest_ref"], idempotency_key="cost-scope")
        _finish_idea_stage(runtime)
        _finish_plan_and_skipped_bundle(runtime)
        submission = runtime.plan_stage.query_current()["run"]["submission_ref"]
        for _ in range(3):
            accepted = runtime.owners.advancement_engine.query_bundle_stage_request(quest["cycle_ref"]).accepted_formal_plan
            assert memory.query_plan_document(submission).plan_document == accepted.plan_document
            assert graph.query_formal_plan_decision(submission).decision == "accepted"
            leaves = graph.resolve_plan_evidence_reuse_leaves(
                quest_ref=quest["quest_ref"], accepted_formal_plan=accepted)
            assert len(leaves) == 1 and leaves[0].asset_version_ref == asset.version_ref
        assert calls == [(True, 1)], calls
    finally:
        runtime.close()
