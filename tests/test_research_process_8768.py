from types import SimpleNamespace as NS
from dataclasses import replace
from unittest.mock import Mock
import json

import pytest
from sqlalchemy import text

from meta_research.context_presentation import (
    CONTEXT_VIEW_MAX_BYTES, reasoning_handoff_reference, stage_context_view,
)
from meta_research.human_research_context import read_human_research_context
from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json
from meta_research.question_relations import question_relation_operations
from meta_research.semantic_mcp import SemanticMcpError


def _context(stage="reasoning"):
    return NS(root_kind=stage, run_ref="run", attempt_ref="attempt",
              root_session_ref="root", fence_ref="fence", capability_binding_hash="a"*64)


@pytest.mark.parametrize("stage", ["idea", "plan", "bundle", "target", "reasoning"])
def test_human_reply_reaches_any_entry_with_exact_content_and_scope(stage):
    response = {"response_ref": "response-1", "decision": "provided",
                "facts": {"judgment": "Try the subgroup before the broad question."},
                "note": "已尝试总体分析；导师建议保留不确定性。" * 1200}
    request = {"request_ref": "request-1", "quest_ref": "quest", "kind": "offline_action",
               "obligation": "请判断下一步投入", "business_purpose": "两次失败后的研究取舍",
               "status": "satisfied", "current": True, "responses": [response],
               "evaluation": {"response_refs": ["response-1"]}, "disposition": {"decision": "satisfied"}}
    inputs_page = {"items": [], "offset": 0, "next_offset": None}
    owner = NS(query_human_request=Mock(return_value=request),
               query_research_help_page=Mock(return_value={"items": [request], "next_cursor": None}),
               query_research_inputs=Mock(return_value=inputs_page))
    runtime = NS(verify_root_agent_runtime_scope=Mock(return_value={"quest_ref": "quest"}))
    preview = read_human_research_context(runtime, owner, _context(stage), {})
    assert json.loads(preview["text"])["research_inputs"] == inputs_page
    owner.query_research_inputs.assert_called_once_with(quest_ref="quest", offset=0, limit=12, query="")
    summary = json.loads(preview["text"])["items"][0]
    assert summary["latest_response"]["note"]["truncated"]
    args = {key: value for key, value in summary["latest_response"]["reader"].items() if key != "operation"}
    chunks = []
    while True:
        chunk = read_human_research_context(runtime, owner, _context(stage), args)
        chunks.append(chunk["text"])
        if chunk.get("next_offset") is None:
            break
        args["offset"] = chunk["next_offset"]
    exact = json.loads("".join(chunks))
    assert exact["response"] == response
    assert exact["response_content_hash"] == canonical_hash(response)
    assert exact["used_by_owner_evaluation"] and exact["owner_disposition"] == "satisfied"
    request["quest_ref"] = "foreign"
    with pytest.raises(SemanticMcpError, match="research_help_request_unbound"):
        read_human_research_context(runtime, owner, _context(stage), {**args, "offset": 0})
    request["quest_ref"] = "quest"
    with pytest.raises(SemanticMcpError, match="research_help_response_unbound"):
        read_human_research_context(runtime, owner, _context(stage), {**args, "response_ref": "foreign"})


@pytest.mark.parametrize("stage", ["idea", "plan", "bundle", "reasoning"])
def test_prior_handoff_visible_at_any_stage_and_full_note_stays_exact(stage):
    note = "继续原因：先检验局部假设；试过两种方法均失败；尚缺评估；使用asset_version_A；遵循response_1意见。" * 200
    binding = {"cycle_ref": "old-cycle", "commit_ref": "old-commit", "outcome_ref": "old-outcome",
               "receipt": {}, "outcome_receipt": {}, "closure": {"notes": note,
               "transition": {"source_quest_ref": "quest", "source_question_ref": "old-question"}}}
    pack = {"prior_accepted_bindings": [reasoning_handoff_reference(binding)],
            "large_proof": {str(i): "x"*2000 for i in range(100)}}
    original = canonical_hash(pack)
    view = stage_context_view(stage, pack, context_pack_ref="context", context_pack_hash=original)
    handoff = view["current_handoff_notes"][0]
    assert handoff["notes"]["text"].startswith("继续原因")
    assert handoff["notes"]["truncated"]
    assert handoff["reader"]["source"] == "predecessor_closure"
    assert handoff["reader"]["source_ref"] == "old-commit"
    assert view["human_guidance_reader"]["operation"] == "human_request.read"
    assert len(canonical_json(view).encode()) <= CONTEXT_VIEW_MAX_BYTES
    assert canonical_hash(pack) == original


def test_oversized_predecessor_excerpts_keep_bounded_view_and_exact_reader():
    pack = {"prior_accepted_bindings": [{"commit_ref": f"commit-{i}", "outcome_ref": f"outcome-{i}",
        "handoff_notes": {"text": "研究未决" * 200000, "source_utf8_bytes": 2400000, "truncated": False},
        "scientific_summary": {"claim": "研究范围" * 200000}} for i in range(3)]}
    original = canonical_hash(pack)
    view = stage_context_view("plan", pack, context_pack_ref="context", context_pack_hash=original)
    assert len(canonical_json(view).encode()) <= CONTEXT_VIEW_MAX_BYTES
    assert all(note["notes"]["truncated"] and note["scientific_summary"]["truncated"]
               for note in view["current_handoff_notes"])
    assert all(note["reader"]["source"] == "predecessor_closure" for note in view["current_handoff_notes"])
    assert canonical_hash(pack) == original


def test_related_questions_and_answered_human_help_use_real_owners(tmp_path):
    from test_public_reasoning_stage import (
        _reasoning_runtime, _DeterministicReasoningSkill, _confirm_deepfetch_quest,
        _finish_idea_stage, _tick_reasoning,
    )
    from test_public_manual_question_lifecycle import _confirm_waived_manual_question
    from test_public_first_question_deepfetch import RecordingAcquisitionProvider
    class NotesReasoning(_DeterministicReasoningSkill):
        human_response_ref = None

        def _result_parts(self, request):
            outcome, transition = super()._result_parts(request)
            outcome["notes"] = f"总体问题未解决；保留失败观察，先检验局部机制；遵循 {self.human_response_ref}。"
            return outcome, transition

        def review_draft(self, request, draft):
            return replace(super().review_draft(request, draft),
                           review_mode="advisory_unobserved", reviewer_agent_ref=None)

    skill = NotesReasoning()
    runtime = _reasoning_runtime(tmp_path / "research-process", reasoning_skill=skill,
                                 acquisition_provider=RecordingAcquisitionProvider())
    try:
        quest = _confirm_deepfetch_quest(runtime)
        human = runtime.owners.human_collaboration
        seeded = _confirm_waived_manual_question(human, quest_ref=quest["quest_ref"],
            parent_question_ref=quest["question_ref"], key_prefix="related-question")
        for _ in range(10):
            manual = human.query_manual_question_creation(seeded["context_ref"])
            if manual["status"] == "completed":
                break
            assert human.reconcile_once()
        assert manual["status"] == "completed"
        child = runtime.owners.research_graph.query_question_tree(quest_ref=quest["quest_ref"])[-1]
        _finish_idea_stage(runtime)
        _tick_reasoning(runtime)
        current = _tick_reasoning(runtime)
        run = current["run"]
        graph = runtime.owners.research_graph
        args = dict(quest_ref=quest["quest_ref"], source_run_ref=run["run_ref"], effect_id="related-1",
            left_question_ref=quest["question_ref"], right_question_ref=child.question_ref,
            notes="两个问题都依赖同一未决机制，先做局部调查。")
        relation = graph.record_question_relation(**args)
        assert relation["kind"] == "related"
        assert graph.record_question_relation(**{**args, "left_question_ref": child.question_ref,
            "right_question_ref": quest["question_ref"]}) == relation
        with pytest.raises(OwnerConflict, match="question_relation_identity_conflict"):
            graph.record_question_relation(**{**args, "notes": "silently changed"})
        with pytest.raises(OwnerConflict, match="question_relation_question_unbound"):
            graph.record_question_relation(**{**args, "effect_id": "missing-question", "right_question_ref": "foreign-question"})
        assert graph.query_question_relations(quest_ref=quest["quest_ref"], question_ref=child.question_ref)["items"] == [relation]
        with runtime._database.write() as connection:
            idea_request = connection.execute(text("SELECT request_ref FROM ar_stage_runs WHERE stage='idea' LIMIT 1")).scalar_one()
            connection.execute(text("UPDATE rg_question_relations SET source_request_ref=:ref WHERE relation_ref=:relation"),
                {"ref": idea_request, "relation": relation["relation_ref"]})
        with pytest.raises(OwnerConflict, match="question_relation_content_invalid"):
            graph.query_question_relations(quest_ref=quest["quest_ref"])
        with runtime._database.write() as connection:
            connection.execute(text("UPDATE rg_question_relations SET source_request_ref=:ref WHERE relation_ref=:relation"),
                {"ref": relation["source_request_ref"], "relation": relation["relation_ref"]})
        context = NS(root_kind="reasoning", run_ref=run["run_ref"], attempt_ref=run["attempt_ref"],
            root_session_ref=run["root_session_ref"], fence_ref=run["fence_ref"],
            capability_binding_hash=run["runtime_binding_hash"])
        ops = question_relation_operations(research_graph=graph, agent_runtime=runtime.owners.agent_runtime)
        assert ops[2].handler(context, {"effect_id": "related-1"})["relation"] == relation
        with pytest.raises(SemanticMcpError, match="question_relation_scope_invalid"):
            ops[0].handler(_context("target"), {})

        for i in range(15):
            request = runtime.owners.agent_runtime.open_human_request(
                request_kind="offline_action", obligation=f"判断研究方向 {i}",
                business_purpose="已尝试两种分析而没有新认识，需导师帮助取舍。",
                target_assertion={"question_ref": child.question_ref},
                acceptance_conditions=("提供具体判断",), direct_waiter={
                    "waiter_ref": f"advice-{i}", "generation": 1,
                    "target_assertion": {"question_ref": child.question_ref},
                    "wait_scope": "local", "other_blockers": []},
                quest_ref=quest["quest_ref"], idempotency_key=f"advice-{i}")
        response = human.respond_to_human_request(request["request_ref"], decision="provided",
            facts={"advice": "先检验局部机制"}, note="保留失败观察，允许评价稍后继续。",
            idempotency_key="mentor-response")
        skill.human_response_ref = response["response_ref"]
        first = human.query_research_help_page(quest_ref=quest["quest_ref"])
        assert len(first["items"]) == 12 and first["next_cursor"]
        assert first["items"][0]["responses"][-1]["response_ref"] == response["response_ref"]
        second = human.query_research_help_page(quest_ref=quest["quest_ref"], cursor=first["next_cursor"])
        assert len(second["items"]) == 3 and second["next_cursor"] is None
        assert not {item["request_ref"] for item in first["items"]} & {item["request_ref"] for item in second["items"]}
        source_request = current["stage_run_request"]["request_ref"]
        ae = runtime.owners.advancement_engine
        for _ in range(16):
            if ae.query_reasoning_stage_commit(source_request) is not None:
                break
            assert runtime.reasoning_stage.process_once(), runtime.reasoning_stage.transient_error
        commit = ae.query_reasoning_stage_commit(source_request)
        assert commit is not None, runtime.reasoning_stage.query_current()
        successor = ae.query_foreground(quest["quest_ref"])
        assert successor["cycle_ref"] != quest["cycle_ref"]
        assert successor["question_ref"] == quest["question_ref"]
        handoff = ae.query_reasoning_successor_context(successor["cycle_ref"])
        pack = handoff["idea_context_pack"]
        assert pack["schema_ref"].endswith("/v4")
        assert response["response_ref"] in pack["prior_accepted_bindings"][0]["handoff_notes"]["text"]
        from meta_research.idea_contract import validate_idea_context_pack
        validate_idea_context_pack(pack, cycle_ref=successor["cycle_ref"],
                                  accepted_question_binding=pack["accepted_question_binding"])
        with runtime._database.read() as connection:
            assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    finally:
        runtime.close()
