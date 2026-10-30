"""Exact scientific handoffs through the public presentation interface."""
from copy import deepcopy
import logging
from types import SimpleNamespace

import pytest

from meta_research.context_presentation import reasoning_handoff_reference, stage_context_view


def test_idea_receives_one_complete_predecessor_note_and_summary():
    note = "UNIQUE-NOTE:" + "必要失败边界" * 3000
    summary = {"claim": "UNIQUE-SUMMARY", "limitations": [str(i) for i in range(30)]}
    binding = {"cycle_ref": "old-cycle", "commit_ref": "commit", "outcome_ref": "outcome",
               "receipt": {}, "outcome_receipt": {}, "closure": {"notes": note, "scientific_summary": summary}}
    reference = reasoning_handoff_reference(binding)
    pack = {"prior_accepted_bindings": [reference]}
    view = stage_context_view("idea", pack, context_pack_ref="ctx", context_pack_hash="a" * 64)
    prior = view["sections"]["prior_accepted_bindings"][0]
    assert prior["handoff_notes"]["text"] == note
    assert prior["handoff_notes"]["truncated"] is False
    assert prior["scientific_summary"] == summary
    assert "current_handoff_notes" not in view


def test_reasoning_receives_complete_plan_reports_and_target_limits():
    plan = {"notes": "plan" * 10000, "experiment_briefs": [{"brief_key": str(i), "limitations": "x" * 4000} for i in range(35)]}
    targets = [{"target_ref": str(i), "target_spec": {"execution_contract": {"step": "x" * 5000}}, "result": {"limitations": "negative" * 1000}} for i in range(25)]
    pack = {"plan_evidence_input": {"kind": "accepted", "formal_plan_binding": {"plan_document": plan}, "evidence_reuse_closure": targets},
            "accepted_target_commit_closures": targets,
            "upstream_stage_closure": [{"stage": "bundle", "closure": {"bundle_report": {"failures": targets}, "notes": "late" * 10000}}]}
    before = deepcopy(pack)
    view = stage_context_view("reasoning", pack, context_pack_ref="ctx", context_pack_hash="b" * 64)
    for key in pack:
        assert view["sections"][key] == pack[key]
    assert pack == before


def test_disabled_projection_diagnostics_do_not_hash_the_reading_view(monkeypatch):
    import meta_research.context_presentation as presentation
    monkeypatch.setattr(presentation.LOGGER, "isEnabledFor", lambda level: False)
    monkeypatch.setattr(presentation.hashlib, "sha256", lambda *args: pytest.fail("observation-only hash ran"))
    view = stage_context_view("idea", {}, context_pack_ref="ctx", context_pack_hash="c" * 64)
    assert view["source_context_pack_hash"] == "c" * 64


def test_fresh_cycle_freezes_creation_focus_without_evidence_prequery(tmp_path, monkeypatch):
    from test_public_bundle_stage import _bundle_runtime
    from test_public_plan_stage import _confirm_direct_quest
    from meta_research.owners.common import OwnerConflict
    runtime = _bundle_runtime(tmp_path / "fresh-inputs")
    try:
        created = _confirm_direct_quest(runtime)
        engine = runtime.owners.advancement_engine
        cycle = engine.query_initial_cycle(created["initialization_id"])
        position = engine.query_cycle_position(cycle.cycle_ref)
        assert position["round"] == 1
        assert position["cycle_focus"] == "比较自监督和监督基线。"
        assert position["focus_source"]["kind"] == "confirmed_creation"
        assert position["focus_source"]["draft_hash"] == created["quest_draft"]["hash"]
        graph = runtime.owners.research_graph
        monkeypatch.setattr(graph, "query_evidence_reference_page", lambda *args, **kwargs: pytest.fail("fixed evidence prequery"))
        assert runtime.idea_stage.process_once()
        request = engine.query_idea_stage_request(cycle.cycle_ref)
        assert request.context_pack["cycle_position"] == position
        assert request.context_pack["evidence_page"] is None
        assert request.context_pack["accepted_evidence_refs"] == []
        forged = deepcopy(request.context_pack)
        forged["cycle_position"]["cycle_focus"] = "replace original intent"
        with pytest.raises(OwnerConflict, match="cycle_position_binding_invalid"):
            engine.ensure_idea_stage_request(cycle_ref=cycle.cycle_ref, accepted_question=request.accepted_question,
                                            context_pack=forged, idempotency_key="forged-focus")
    finally:
        runtime.close()


def test_owner_handoff_keeps_full_original_scientific_summary_and_separate_notes(tmp_path, monkeypatch):
    import test_public_reasoning_owners as fixtures
    from meta_research.owners.common import canonical_hash
    database, memory, _, graph, receipts = fixtures._owners(tmp_path)
    original = memory.accept_reasoning_content
    note = "Only in original notes:" + "失败与缺测限制" * 3000
    def accept_with_notes(**values):
        values["outcome"]["scientific_outcome"]["notes"] = note
        values["reviewed_draft"] = deepcopy(values["outcome"])
        values["review"] = fixtures._review(values["outcome"])
        return original(**values)
    monkeypatch.setattr(memory, "accept_reasoning_content", accept_with_notes)
    try:
        content = fixtures._accept_real_root_next_cycle_content(memory, graph,
                    submission_ref="reasoning-submission:exact-summary", outcome_ref="scientific-outcome:exact-summary")
        accepted = graph.decide_reasoning_outcome(content=content)
        assert accepted.decision == "accepted"
        closure = receipts.query_reasoning_transition_binding(accepted.outcome_ref, accepted.receipt)
        assert closure["notes"] == note
        expected = {key: value for key, value in content.scientific_outcome.items() if key != "notes"}
        assert closure["scientific_summary"] == expected
        assert "notes" not in closure["scientific_summary"]
    finally:
        database.close()


@pytest.mark.parametrize("focus", [None, "", " \n"])
def test_next_cycle_requires_its_own_explicit_focus(focus):
    from test_reasoning_contract import _scientific_outcome, _next_cycle_proposal
    from meta_research.reasoning_contract import validate_reasoning_transition, ReasoningContractError
    next_cycle = _next_cycle_proposal()
    if focus is None:
        next_cycle.pop("cycle_focus", None)
    else:
        next_cycle["cycle_focus"] = focus
    with pytest.raises(ReasoningContractError):
        validate_reasoning_transition(_scientific_outcome(), next_cycle=next_cycle)


def test_stage_position_prefix_uses_frozen_focus_and_rejects_foreign_cycle():
    import json
    from meta_research.context_presentation import stage_position_prompt
    focus = "保留首根原文 \n验证失败而不是重复下载。"
    position = {"cycle_ref": "cycle", "round": 3, "cycle_focus": focus,
                "focus_source": {"outcome_ref": "previous-result", "transition_hash": "d" * 64}}
    request = SimpleNamespace(context_pack={"cycle_ref": "cycle", "cycle_position": position})
    prefix = stage_position_prompt(request, "idea")
    assert prefix.startswith("cycle_position=")
    assert json.loads(prefix.split("=", 1)[1])["cycle_focus"] == focus
    request.context_pack["cycle_ref"] = "foreign"
    with pytest.raises(ValueError, match="cycle_position_invalid"):
        stage_position_prompt(request, "idea")


@pytest.mark.parametrize("reason", [None, "", "   "])
def test_formal_control_entries_reject_missing_or_blank_focus_as_owner_conflict(tmp_path, reason):
    from test_public_bundle_stage import _bundle_runtime
    from meta_research.owners.common import OwnerConflict
    runtime = _bundle_runtime(tmp_path / "invalid-control-focus")
    try:
        engine = runtime.owners.advancement_engine
        payload = {"action": "normal_switch", "target": {
            "quest_ref": "quest", "question_ref": "question", "cycle_ref": "cycle",
            "epoch": 1, "target_question_ref": "next-question"}, "reason": reason}
        if reason is None:
            payload.pop("reason")
        for action in (lambda: engine.preview_foreground_control(payload),
                       lambda: engine.prepare_foreground_control(intent_id="intent:bad-focus", payload=payload,
                           expected_revision=0, idempotency_key="bad-focus")):
            with pytest.raises(OwnerConflict, match="research_control_(payload|reason)_invalid"):
                action()
    finally:
        runtime.close()
