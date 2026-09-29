"""Accepted Reasoning history is exact text, not a replay of execution inputs."""
from dataclasses import replace
from unittest.mock import Mock

import pytest
from sqlalchemy import text

from meta_research.composition import build_production_runtime
from meta_research.owners.common import OwnerConflict, canonical_json
from meta_research.paths import prepare_data_root
from meta_research.research_overview import ResearchOverviewReader
from meta_research.writing_snapshot import WritingResearchSnapshotReader
from test_bundle_history_display import _changed
from test_research_overview import (
    CurrentReasoningSkill, DeterministicDeepFetchProvider, DeterministicProbe,
    SnapshotAwareProposalDrafter, RecordingAcquisitionProvider,
    _DeterministicDraftingAdapter, _DeterministicIdeaSkill,
    _DeterministicPlanSkill, _confirm_deepfetch_quest, _finish_idea_stage,
    _tick_reasoning,
)


@pytest.fixture
def accepted_reasoning(tmp_path):
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "reasoning-history"),
        proposal_drafter=SnapshotAwareProposalDrafter(),
        intent_drafting_provider=_DeterministicDraftingAdapter(),
        host_compute_probe=DeterministicProbe(),
        deepfetch_provider=DeterministicDeepFetchProvider(),
        acquisition_provider=RecordingAcquisitionProvider(),
        idea_skill_provider=_DeterministicIdeaSkill(no_viable=True),
        plan_skill_provider=_DeterministicPlanSkill(no_gap=False),
        reasoning_skill_provider=CurrentReasoningSkill(),
    )
    try:
        quest = _confirm_deepfetch_quest(runtime)
        _finish_idea_stage(runtime)
        for _ in range(14):
            view = _tick_reasoning(runtime)
            if view["stage_commit"] is not None:
                break
        else:
            raise AssertionError(runtime.reasoning_stage.transient_error)
        owner = runtime.owners.advancement_engine
        history = owner.query_quest_stage_history_display(quest["quest_ref"])
        cycle = next(item for item in history if item.cycle_ref == quest["cycle_ref"])
        commit = next(item for item in cycle.commits if item.stage == "reasoning")
        request = next(item for item in cycle.requests if item.request_ref == commit.request_ref)
        yield runtime, quest, cycle, request, commit
    finally:
        runtime.close()


def test_reasoning_history_keeps_text_without_replaying_execution(accepted_reasoning, monkeypatch):
    runtime, quest, cycle, request, commit = accepted_reasoning
    owners, database = runtime.owners, runtime._database
    readers = (owners.research_graph, owners.advancement_engine,
               owners.research_memory, owners.agent_runtime)
    overview, writing = ResearchOverviewReader(*readers), WritingResearchSnapshotReader(*readers)
    with database.read_snapshot():
        expected = overview._query_once(quest["quest_ref"])
        original = writing._reasoning_stage_value(request, commit)["result"]
    strict = Mock(side_effect=OwnerConflict("test_execution_proof_unavailable"))
    with monkeypatch.context() as patch:
        patch.setattr(owners.research_memory._receipt_verifier,
                      "verify_reasoning_content_receipt", strict)
        write = Mock(side_effect=AssertionError("display_performed_owner_write"))
        patch.setattr(database, "write", write)
        with database.read_snapshot():
            actual = overview._query_once(quest["quest_ref"])
            assert actual == expected
            artifact = overview._artifact(quest["quest_ref"], cycle, request, commit)
            assert artifact["content"] == original["outcome"]["scientific_outcome"]
            assert artifact["source"]["content_hash"] == original["content_hash"]
        strict.assert_not_called()
        write.assert_not_called()
        with pytest.raises(OwnerConflict, match="test_execution_proof_unavailable"):
            writing._reasoning_stage_value(request, commit)
    assert database._read_cut.get() is None and database._read_cache.get() is None


def test_reasoning_history_rejects_corrupt_text_receipts_and_scope(accepted_reasoning):
    runtime, quest, cycle, request, commit = accepted_reasoning
    owners, database = runtime.owners, runtime._database
    overview = ResearchOverviewReader(owners.research_graph, owners.advancement_engine,
                                      owners.research_memory, owners.agent_runtime)
    run = owners.agent_runtime.query_reasoning_stage_run(request.request_ref)
    content = owners.research_memory.query_reasoning_content(run.execution.submission_ref)
    decision = owners.research_graph.query_reasoning_outcome_decision(run.execution.submission_ref)
    altered = dict(content.scientific_outcome, claim="An altered historical claim.")
    for table, key, ref, field, value in (
        ("rm_reasoning_contents", "content_ref", content.content_ref, "scientific_outcome_json", canonical_json(altered)),
        ("rm_reasoning_contents", "content_ref", content.content_ref, "payload_hash", "0" * 64),
        ("rm_reasoning_contents", "content_ref", content.content_ref, "receipt_hash", "0" * 64),
        ("rg_reasoning_outcome_decisions", "decision_ref", decision.decision_ref, "receipt_hash", "0" * 64),
        ("ar_stage_runs", "run_ref", run.run_ref, "epoch", request.epoch + 1),
        ("ar_stage_runs", "run_ref", run.run_ref, "completion_receipt_hash", "0" * 64),
    ):
        with _changed(database, table, key, ref, field, value), database.read_snapshot():
            artifact = overview._artifact(quest["quest_ref"], cycle, request, commit)
            assert artifact["status"] == "unavailable", (table, field)
            assert artifact["content"] is None
    for bad_request, bad_commit in (
        (replace(request, epoch=request.epoch + 1), commit),
        (replace(request, context_pack_hash="0" * 64), commit),
        (request, replace(commit, run_ref="another-run")),
        (request, replace(commit, outcome_ref="another-outcome")),
        (request, replace(commit, outcome_receipt=replace(commit.outcome_receipt, payload_hash="0" * 64))),
        (request, replace(commit, run_completion_receipt=replace(commit.run_completion_receipt, payload_hash="0" * 64))),
    ):
        with database.read_snapshot():
            artifact = overview._artifact(quest["quest_ref"], cycle, bad_request, bad_commit)
        assert artifact["status"] == "unavailable"
        assert artifact["content"] is None
    with database.read_snapshot():
        artifact = overview._artifact("another-quest", cycle, request, commit)
    assert artifact["status"] == "unavailable" and artifact["content"] is None
    # The history shortcut still reads the real immutable source bytes.
    # This path belongs to this test's isolated runtime, never a live store.
    with database.read() as connection:
        object_path = connection.execute(text(
            "SELECT object_path FROM rm_reasoning_contents WHERE content_ref=:ref"
        ), {"ref": content.content_ref}).scalar_one()
    original_path = runtime.data_root.objects / object_path
    original_bytes = original_path.read_bytes()
    try:
        original_path.write_bytes(b"corrupted historical text")
        with database.read_snapshot():
            artifact = overview._artifact(quest["quest_ref"], cycle, request, commit)
        assert artifact["status"] == "unavailable" and artifact["content"] is None
    finally:
        original_path.write_bytes(original_bytes)
    with database.read_snapshot():
        assert overview._artifact(quest["quest_ref"], cycle, request, commit)["status"] == "accepted"
