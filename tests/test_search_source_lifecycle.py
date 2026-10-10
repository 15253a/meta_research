from __future__ import annotations

from meta_research.search_sources import QuestScope
from test_public_autonomous_creation import (
    _AutonomousReasoningSkill,
    _ReadyAutonomousAcquisitionProvider,
    _reach_autonomous_checkpoint,
    _tick_autonomous,
)
from test_public_manual_question_lifecycle import (
    DeterministicAcquisitionProvider,
    DeterministicManualDeepFetchProvider,
    _accept_root_question,
    _build_runtime,
    _open_and_confirm_seed,
)
from test_public_plan_stage import _DeterministicDraftingAdapter, _DeterministicProbe
from test_public_reasoning_stage import _reasoning_runtime


def test_manual_run_uses_quest_selection_and_keeps_issued_versions(tmp_path):
    provider = DeterministicManualDeepFetchProvider()
    runtime = _build_runtime(tmp_path / "manual", deepfetch_provider=provider,
        acquisition_provider=DeterministicAcquisitionProvider())
    try:
        quest_ref, parent_ref = _accept_root_question(runtime, "manual-sources")
        source = runtime.search_sources.save({"kind": "website", "name": "Selected pages", "url": "https://arxiv.org"})
        selected = runtime.search_sources.select(QuestScope(quest_ref),
            allowed_source_ids=(source["source_id"],),
            expected_revision=runtime.search_sources.selection(QuestScope(quest_ref))["revision"])
        human = runtime.owners.human_collaboration
        seeded = _open_and_confirm_seed(human, quest_ref=quest_ref,
            parent_question_ref=parent_ref, key_prefix="manual-sources", deepfetch_preference="use")
        queued = human.start_manual_creation_deepfetch(seeded["context_ref"],
            expected_seed_ref=seeded["seed"]["ref"], expected_seed_hash=seeded["seed"]["hash"],
            idempotency_key="manual-sources-start")
        request_ref = queued["research_path"]["deepfetch"]["request_ref"]
        issued = human.query_deepfetch_request(request_ref)
        expected = [{"source_id": source["source_id"], "source_version": 1}]
        assert issued.scope["search_source_basis"]["sources"] == expected
        runtime.search_sources.save({"kind": "website", "name": "Later pages", "url": "https://arxiv.org"},
            source_id=source["source_id"], expected_version=1)
        runtime.search_sources.select(QuestScope(quest_ref), allowed_source_ids=(), expected_revision=selected["revision"])
        assert human.query_deepfetch_request(request_ref).scope == issued.scope
        assert runtime.deepfetch.process_once()
        assert len(provider.requests) == 1
        request = provider.requests[0]
        assert request.quest_ref == quest_ref
        assert request.creation_context_kind == "manual_question_creation"
        assert request.creation_context_ref == seeded["context_ref"]
        assert request.scope["search_source_basis"]["sources"] == expected
        assert request.literature_access_mode == runtime.owners.agent_runtime.query_acquisition_session(
            session_ref=issued.acquisition_session_ref).mode
        assert human.query_manual_question_creation(seeded["context_ref"])["research_path"]["status"] == "ready"
    finally:
        runtime.close()


def test_autonomous_reissue_retains_quest_sources_after_configuration_changes(tmp_path):
    runtime = _reasoning_runtime(tmp_path / "autonomous",
        reasoning_skill=_AutonomousReasoningSkill(require_source_literature=False),
        acquisition_provider=_ReadyAutonomousAcquisitionProvider(),
        host_compute_probe=_DeterministicProbe(), proposal_drafter=_DeterministicDraftingAdapter())
    try:
        quest, _, checkpoint = _reach_autonomous_checkpoint(runtime, direct_quest=True)
        quest_ref = str(quest["quest_ref"])
        source = runtime.search_sources.save({"kind": "website", "name": "Selected pages", "url": "https://arxiv.org"})
        selected = runtime.search_sources.select(QuestScope(quest_ref),
            allowed_source_ids=(source["source_id"],),
            expected_revision=runtime.search_sources.selection(QuestScope(quest_ref))["revision"])
        started = runtime.autonomous_creation.start(
            reasoning_checkpoint_ref=str(checkpoint["checkpoint_ref"]),
            source_scientific_outcome_ref=str(checkpoint["scientific_outcome"]["outcome_ref"]),
            idempotency_key="autonomous-sources-start")
        assert _tick_autonomous(runtime, "agent_runtime")["deepfetch"]["status"] == "not_started"
        assert _tick_autonomous(runtime, "advancement_engine")["deepfetch"]["status"] == "queued"
        engine = runtime.owners.advancement_engine
        issued = engine.query_autonomous_deepfetch_request(str(started["context_ref"]))
        expected = [{"source_id": source["source_id"], "source_version": 1}]
        assert issued.scope["search_source_basis"]["scope"] == {"kind": "quest", "quest_ref": quest_ref}
        assert issued.scope["search_source_basis"]["sources"] == expected
        runtime.search_sources.save({"kind": "website", "name": "Later pages", "url": "https://arxiv.org"},
            source_id=source["source_id"], expected_version=1)
        runtime.search_sources.select(QuestScope(quest_ref), allowed_source_ids=(), expected_revision=selected["revision"])
        session = runtime.owners.agent_runtime.query_acquisition_session(session_ref=issued.acquisition_session_ref)
        retried = engine.issue_autonomous_deepfetch_request(
            context=runtime.owners.human_collaboration.query_autonomous_creation_context(str(started["context_ref"])),
            acquisition_session=session, idempotency_key="autonomous-sources-reissue")
        assert retried.request_ref == issued.request_ref
        assert retried.scope == issued.scope
    finally:
        runtime.close()
