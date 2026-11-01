"""Historical navigation through persisted Owners, with exact content readers."""
import pytest
from sqlalchemy import text

from meta_research.historical_research import discover_question_cycles, read_historical_stage_content
from meta_research.owners.common import OwnerConflict
from meta_research.research_content import read_content
from test_public_reasoning_stage import _reasoning_runtime, _DeterministicReasoningSkill, _confirm_deepfetch_quest
from test_public_plan_stage import _finish_idea_stage


def test_question_cycle_discovery_reads_exact_idea_and_scopes_history(tmp_path):
    runtime = _reasoning_runtime(tmp_path / "history", reasoning_skill=_DeterministicReasoningSkill())
    try:
        quest = _confirm_deepfetch_quest(runtime)
        _finish_idea_stage(runtime)
        graph, memory = runtime.owners.research_graph, runtime.owners.research_memory
        page = discover_question_cycles(graph, quest_ref=quest["quest_ref"], question_ref=quest["question_ref"])
        cycle, = page["items"]
        assert cycle["cycle_ref"] == quest["cycle_ref"]
        idea = next(item for item in cycle["stages"] if item["stage"] == "idea")
        assert idea["disposition"] == "completed"
        assert "closure" not in idea
        content = read_content(graph, memory, quest_ref=quest["quest_ref"], **idea["reader"])
        assert "why_plan_cannot_proceed" in content["text"]
        assert page["result_state"] == "matches" and page["scan_complete"]
        with pytest.raises(OwnerConflict, match="historical_question_not_visible"):
            discover_question_cycles(graph, quest_ref="foreign", question_ref=quest["question_ref"])
    finally:
        runtime.close()


from test_bundle_report_receipt_order import committed_targets
from test_bundle_report_read_snapshot import _accept_report
from test_public_reasoning_stage import _tick_reasoning
from meta_research.bundle_protocol import projection_plain_value


def _another_quest(runtime):
    from test_public_reasoning_stage import _deepfetch_draft
    human = runtime.owners.human_collaboration
    opened = human.create_quest({}, "history-unrelated-open")
    ref = opened["initialization_id"]
    probed = human.observe_host_compute(ref, ["GPU-deepfetch-1"], "history-unrelated-compute")
    revised = human.revise_quest_draft(ref, _deepfetch_draft(probed), probed["quest_draft"]["hash"],
                                      "history-unrelated-draft", probed["quest_draft"]["revision"])
    draft = revised["quest_draft"]
    human.prepare_acquisition_session(ref, draft["hash"], "history-unrelated-acquisition", draft["revision"])
    human.generate_question_proposal(ref, draft["hash"], "history-unrelated-proposal", draft["revision"])
    assert runtime.deepfetch.process_once()
    assert human.process_drafting_once()
    proposed = human.query_quest_creation(ref)
    args = {"quest_draft_revision": proposed["quest_draft"]["revision"],
            "quest_draft_hash": proposed["quest_draft"]["hash"],
            "proposal_ref": proposed["proposal"]["ref"], "proposal_hash": proposed["proposal"]["hash"]}
    preview = human.preview_confirmation(ref, **args, idempotency_key="history-unrelated-preview")
    human.confirm_quest(ref, **args, preview_ref=preview["confirmation_preview"]["ref"],
                        preview_hash=preview["confirmation_preview"]["hash"], idempotency_key="history-unrelated-confirm")
    for _ in range(16):
        result = human.query_quest_creation(ref)
        if result["status"] == "completed":
            return result
        assert human.reconcile_once(), result
    raise AssertionError(result)


def test_cycle_continuation_is_question_scoped_and_detects_new_stage(tmp_path):
    runtime = _reasoning_runtime(tmp_path / "continuation", reasoning_skill=_DeterministicReasoningSkill())
    try:
        quest = _confirm_deepfetch_quest(runtime)
        _finish_idea_stage(runtime)
        for _ in range(12):
            reasoning = _tick_reasoning(runtime)
            if reasoning["stage_commit"] is not None:
                break
        assert reasoning["stage_commit"] is not None
        graph = runtime.owners.research_graph
        args = {"quest_ref": quest["quest_ref"], "question_ref": quest["question_ref"], "limit": 1}
        first = discover_question_cycles(graph, **args)
        assert first["next_offset"] == 1 and not first["scan_complete"]
        _another_quest(runtime)
        second = discover_question_cycles(graph, **args, offset=1, query_revision=first["query_revision"])
        assert second["items"][0]["cycle_ref"] == quest["cycle_ref"]
        assert second["scan_complete"] and second["query_revision"] == first["query_revision"]
        outcomes = graph.query_question_research_history(quest_ref=quest["quest_ref"], question_ref=quest["question_ref"], limit=1)
        exhausted = graph.query_question_research_history(quest_ref=quest["quest_ref"], question_ref=quest["question_ref"],
                                                          offset=1, limit=1, query_revision=outcomes["query_revision"])
        assert exhausted["items"] == [] and exhausted["result_state"] == "exhausted"
        with pytest.raises(OwnerConflict, match="research_query_revision_required"):
            graph.query_question_research_history(quest_ref=quest["quest_ref"], question_ref=quest["question_ref"], offset=1)
        historical_reasoning = next(stage for stage in second["items"][0]["stages"] if stage["stage"] == "reasoning")
        scientific = read_content(graph, runtime.owners.research_memory,
                                  quest_ref=quest["quest_ref"], **historical_reasoning["reader"])
        assert scientific["selected_evidence_binding"]["source_kind"] == "ScientificOutcome"
        with pytest.raises(OwnerConflict, match="research_query_revision_required"):
            discover_question_cycles(graph, **args, offset=1)
        # Request publication changes this Question's history, even before the
        # subsequent stage has produced any accepted content.
        assert runtime.idea_stage.process_once()
        with pytest.raises(OwnerConflict, match="research_query_changed"):
            discover_question_cycles(graph, **args, offset=1, query_revision=first["query_revision"])
        from test_public_manual_question_lifecycle import _confirm_waived_manual_question
        human = runtime.owners.human_collaboration
        _confirm_waived_manual_question(human, quest_ref=quest["quest_ref"],
                                       parent_question_ref=quest["question_ref"], key_prefix="related-child")
        for _ in range(8):
            if not human.reconcile_once():
                break
        child = next(q for q in graph.query_question_tree(quest["quest_ref"])
                     if q.question_ref != quest["question_ref"])
        commit = runtime.owners.advancement_engine.query_reasoning_stage_commit(
            reasoning["stage_run_request"]["request_ref"])
        for i in range(13):
            graph.record_question_relation(quest_ref=quest["quest_ref"], source_run_ref=commit.run_ref,
                effect_id=f"relation-{i}", left_question_ref=quest["question_ref"],
                right_question_ref=child.question_ref, notes=f"Research connection {i}")
        relations = graph.query_question_relations(quest_ref=quest["quest_ref"], question_ref=quest["question_ref"])
        tail = graph.query_question_relations(quest_ref=quest["quest_ref"], question_ref=quest["question_ref"],
            offset=relations["next_offset"], query_revision=relations["query_revision"])
        assert len(relations["items"]) == 12 and len(tail["items"]) == 1
        with runtime._database.write() as connection:
            connection.execute(text("DELETE FROM rg_question_relations WHERE relation_ref=:ref"),
                               {"ref": relations["items"][0]["relation_ref"]})
        with pytest.raises(OwnerConflict, match="research_query_changed"):
            graph.query_question_relations(quest_ref=quest["quest_ref"], question_ref=quest["question_ref"],
                offset=12, query_revision=relations["query_revision"])
    finally:
        runtime.close()


def test_historical_bundle_reads_ae_closure_and_rejects_drift(committed_targets):
    runtime, graph, run, accepted = _accept_report(committed_targets)
    owners = runtime.owners
    completion = owners.agent_runtime.complete_bundle_run(
        run_ref=run.run_ref, attempt_ref=run.attempt_ref, fence_ref=run.fence_ref,
        report_ref=accepted.report_ref, decision_receipt=accepted.receipt,
        idempotency_key="history-complete-bundle")
    commit = owners.advancement_engine.commit_bundle_stage(
        request_ref=run.request_ref, run_ref=run.run_ref, bundle_report_ref=accepted.report_ref,
        run_completion_receipt=completion.receipt, bundle_report_receipt=accepted.receipt,
        idempotency_key="history-commit-bundle")
    request = owners.advancement_engine.query_bundle_stage_request(run.cycle_ref)
    quest = request.accepted_question.quest_ref
    page = discover_question_cycles(owners.research_graph, quest_ref=quest,
                                    question_ref=request.accepted_question.question_ref)
    item = next(stage for cycle in page["items"] for stage in cycle["stages"] if stage["stage"] == "bundle")
    result = read_historical_stage_content(owners.research_memory, quest_ref=quest, **item["reader"])
    assert result["closure"]["bundle_report_ref"] == accepted.report_ref
    assert result["closure"]["bundle_report"] == projection_plain_value(accepted.report)
    assert result["closure"]["bundle_report_hash"] == accepted.report_hash
    import json
    chunks, offset = [], 0
    while True:
        accurate = read_content(owners.research_graph, owners.research_memory, quest_ref=quest,
                                **item["reader"], offset=offset)
        chunks.append(accurate["text"])
        if accurate["next_offset"] is None:
            break
        offset = accurate["next_offset"]
    assert json.loads("".join(chunks))["closure"]["bundle_report_ref"] == accepted.report_ref
    with pytest.raises(OwnerConflict, match="content_source_unbound"):
        read_historical_stage_content(owners.research_memory, quest_ref="foreign", **item["reader"])
    with pytest.raises(OwnerConflict, match="content_version_unbound"):
        read_historical_stage_content(owners.research_memory, quest_ref=quest,
                                      source_ref=item["reader"]["source_ref"], version_ref="0" * 64)
    with runtime._database.write() as connection:
        connection.execute(text("UPDATE ae_stage_commits SET closure_json='{}' WHERE commit_ref=:ref"),
                           {"ref": item["commit_ref"]})
    with pytest.raises(OwnerConflict, match="content_drifted"):
        read_historical_stage_content(owners.research_memory, quest_ref=quest, **item["reader"])
