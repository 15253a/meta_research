"""Audit public Question discovery through exact retained research content."""
import hashlib
import json

from test_bundle_report_read_snapshot import _accept_report
from test_bundle_report_receipt_order import committed_targets
from test_dataset_effect_scope_recovery import _scope
from test_public_manual_question_lifecycle import _confirm_waived_manual_question
from test_public_plan_stage import _finish_idea_stage
from test_public_reasoning_stage import (
    _DeterministicReasoningSkill, _confirm_deepfetch_quest, _reasoning_runtime,
    _tick_reasoning,
)
from test_root_workspace import _accepted, _call, _channel, _external_context


def _companion(runtime):
    return _channel(runtime, _external_context(_scope(
        runtime, quest_ref="question-navigation-selected-elsewhere")))


def _arguments(descriptor):
    return descriptor.get("args", {key: value for key, value in descriptor.items()
                                    if key != "operation"})


def _follow(runtime, channel, descriptor, **extra):
    return _accepted(_call(runtime, channel,
        descriptor.get("operation", "research_memory.content.read"),
        **{**_arguments(descriptor), **extra}))


def _exact_json(runtime, channel, descriptor, quest, *, limit=4096):
    arguments = _arguments(descriptor)
    assert arguments["source_quest_ref"] == quest
    chunks, offset, digest = [], 0, None
    while True:
        page = _follow(runtime, channel, descriptor, offset=offset, limit=limit)
        assert page["encoding"] == "canonical-json-utf8-bytes"
        assert page["returned_bytes"] <= limit
        assert page["offset"] == offset
        assert page["source_ref"] == arguments["source_ref"]
        assert page["version_ref"] == arguments["version_ref"]
        assert digest is None or page["content_hash"] == digest
        digest = page["content_hash"]
        chunks.append(page["text"])
        if page["next_offset"] is None:
            break
        assert page["next_offset"] > offset
        offset = page["next_offset"]
    raw = "".join(chunks).encode()
    assert hashlib.sha256(raw).hexdigest() == digest
    return json.loads(raw)


def test_question_entry_reaches_idea_plan_and_bundle_exact_content(committed_targets):
    runtime, graph, run, report = _accept_report(committed_targets)
    owners = runtime.owners
    completion = owners.agent_runtime.complete_bundle_run(
        run_ref=run.run_ref, attempt_ref=run.attempt_ref, fence_ref=run.fence_ref,
        report_ref=report.report_ref, decision_receipt=report.receipt,
        idempotency_key="question-navigation-bundle-completion")
    owners.advancement_engine.commit_bundle_stage(
        request_ref=run.request_ref, run_ref=run.run_ref, bundle_report_ref=report.report_ref,
        run_completion_receipt=completion.receipt, bundle_report_receipt=report.receipt,
        idempotency_key="question-navigation-bundle-commit")
    request = owners.advancement_engine.query_bundle_stage_request(run.cycle_ref)
    quest = request.accepted_question.quest_ref
    channel = _companion(runtime)
    questions = _accepted(_call(runtime, channel, "research_graph.questions.page",
        source_quest_ref=quest, limit=1))
    item, = questions["items"]
    assert item["question_ref"] == request.accepted_question.question_ref
    assert _exact_json(runtime, channel, item["reader"], quest, limit=97)
    history = _follow(runtime, channel, item["history_reader"])
    stages = {stage["stage"]: stage for cycle in history["items"]
              for stage in cycle["stages"] if stage.get("content_state") == "available"}
    assert {"idea", "plan", "bundle"} <= set(stages)
    bodies = {}
    for name in ("idea", "plan", "bundle"):
        bodies[name] = _exact_json(runtime, channel, stages[name]["reader"], quest)
        if "basis_reader" in stages[name]:
            basis = _exact_json(runtime, channel, stages[name]["basis_reader"], quest)
            if "content_reader" in basis:
                assert _exact_json(runtime, channel, basis["content_reader"], quest)
    assert bodies["bundle"]["closure"]["bundle_report_ref"] == report.report_ref
    assert bodies["bundle"]["closure"]["bundle_report_hash"] == report.report_hash
    print("QUESTION_NAVIGATION idea+plan+bundle+source_selector+exact_byte_pages passed")


def test_question_entry_reaches_reasoning_and_related_question_content_with_continuation(tmp_path):
    runtime = _reasoning_runtime(tmp_path / "question-navigation",
                                 reasoning_skill=_DeterministicReasoningSkill())
    try:
        created = _confirm_deepfetch_quest(runtime)
        quest, question = created["quest_ref"], created["question_ref"]
        _finish_idea_stage(runtime)
        for _ in range(12):
            reasoning = _tick_reasoning(runtime)
            if reasoning["stage_commit"] is not None:
                break
        assert reasoning["stage_commit"] is not None
        channel = _companion(runtime)
        index = _accepted(_call(runtime, channel, "research_graph.questions.page", source_quest_ref=quest))
        item, = index["items"]
        first = _follow(runtime, channel, item["history_reader"], limit=1)
        assert first["next_offset"] == 1
        second = _follow(runtime, channel, item["history_reader"], limit=1,
            offset=first["next_offset"], query_revision=first["query_revision"])
        assert second["next_offset"] is None
        stage = next(stage for cycle in second["items"] for stage in cycle["stages"]
                     if stage["stage"] == "reasoning")
        assert _exact_json(runtime, channel, stage["reader"], quest)
        no_revision = _call(runtime, channel, "research_graph.question_history.read",
            source_quest_ref=quest, question_ref=question, offset=1, limit=1)
        assert no_revision["isError"]
        assert no_revision["structuredContent"]["code"] == "research_query_revision_required"
        assert runtime.idea_stage.process_once()
        changed = _call(runtime, channel, "research_graph.question_history.read", source_quest_ref=quest,
            question_ref=question, limit=1, offset=first["next_offset"], query_revision=first["query_revision"])
        assert changed["isError"] and changed["structuredContent"]["code"] == "research_query_changed"

        human, graph = runtime.owners.human_collaboration, runtime.owners.research_graph
        _confirm_waived_manual_question(human, quest_ref=quest, parent_question_ref=question,
                                       key_prefix="navigation-related")
        for _ in range(8):
            if not human.reconcile_once():
                break
        child, = (entry for entry in graph.query_question_tree(quest) if entry.question_ref != question)
        commit = runtime.owners.advancement_engine.query_reasoning_stage_commit(
            reasoning["stage_run_request"]["request_ref"])
        for i in range(13):
            graph.record_question_relation(quest_ref=quest, source_run_ref=commit.run_ref,
                effect_id=f"navigation-relation-{i}", left_question_ref=question,
                right_question_ref=child.question_ref, notes=f"Exact relation observation {i}")
        relation_page = _accepted(_call(runtime, channel, "research_graph.question_relations.read",
            source_quest_ref=quest, question_ref=question))
        relation_tail = _accepted(_call(runtime, channel, "research_graph.question_relations.read",
            source_quest_ref=quest, question_ref=question, offset=relation_page["next_offset"],
            query_revision=relation_page["query_revision"]))
        relations = relation_page["items"] + relation_tail["items"]
        assert len(relations) == 13 and relation_tail["next_offset"] is None
        assert {entry["notes"] for entry in relations} == {f"Exact relation observation {i}" for i in range(13)}
        question_page = _accepted(_call(runtime, channel, "research_graph.questions.page", source_quest_ref=quest, limit=1))
        question_tail = _accepted(_call(runtime, channel, "research_graph.questions.page", source_quest_ref=quest,
            limit=1, offset=question_page["next_offset"], query_revision=question_page["query_revision"]))
        related_entries = question_page["items"] + question_tail["items"]
        assert {entry["question_ref"] for entry in related_entries} == {question, child.question_ref}
        assert question_tail["next_offset"] is None
        for entry in related_entries:
            assert _exact_json(runtime, channel, entry["reader"], quest)
            assert _follow(runtime, channel, entry["history_reader"])["status"] == "accepted"
        denied = _call(runtime, channel, "research_graph.question_history.read", question_ref=question)
        assert _accepted(denied)["status"] == "not_visible"
        print("QUESTION_NAVIGATION reasoning+related_questions+three_index_continuations+scope passed")
    finally:
        runtime.close()
