from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest

from meta_research.owners.common import OwnerConflict
from meta_research.quest_goal import ResearchRoot, parse_evolution_decision
from test_guidance_scope_alignment import _confirm_guidance
from test_human_guidance_inboxes import _InboxIdea, _run_idea, _finish


@pytest.mark.parametrize("late", [False, True], ids=["initial-cut", "live-supplement"])
def test_goal_evolution_uses_exact_confirmed_guidance_without_read_receipt(tmp_path, late):
    observed = {}

    def primary(human, operation):
        cut = human.freeze_operation_guidance(operation)
        if late:
            _confirm_guidance(provider.runtime, cut.binding.quest_ref,
                {"kind": "quest", "quest_ref": cut.binding.quest_ref},
                strength=2, text="Compare calibrated devices and preserve calibration evidence.")
            message, = human.prepare_live_guidance(scope=operation.scope, binding=cut.binding)
            delivery, = message["deliveries"]
        else:
            frozen_delivery, = cut.deliveries
            delivery = frozen_delivery.as_dict()
        guide = delivery["guide"]
        scope = operation.scope
        author = ResearchRoot(scope.root_kind, scope.run_ref, scope.attempt_ref,
            scope.root_session_ref, scope.fence_ref, scope.runtime_binding_hash,
            cut.binding.identity.operation_ref)
        direction = cut.direction_cut
        decision = {
            "expected_revision": direction["goal"]["goal_revision_ref"],
            "conditions_basis": direction["conditions"]["basis_ref"],
            "work_basis": direction["work"]["basis_ref"],
            "cause": {"kind": "human_guidance", "delivery_ref": delivery["delivery_ref"],
                "guide_ref": {key: guide[key] for key in (
                    "constraint_ref", "revision", "guidance_hash", "receipt_ref", "receipt_hash")}},
            "replacement": {"goal": "Compare calibrated devices.",
                "completion_criteria": "Report reproducible device calibration evidence."},
            "criteria_review": "Review completion criteria together with the confirmed direction.",
            "judgment": "The confirmed Quest guidance changes the overall comparison.",
            "conditions": {
                "runtime_conditions_ref": direction["conditions"]["runtime_conditions"]["revision"],
                "assessments": [{"condition_ref": item["condition_ref"],
                    "disposition": "preserved", "explanation": "Keep the sourced condition."}
                    for item in direction["conditions"]["enduring"]],
                "newly_identified": [{"delivery_ref": delivery["delivery_ref"],
                    "start": 0, "end": len(guide["guidance"]["text"]),
                    "text": guide["guidance"]["text"], "meaning": "Preserve calibration evidence."}],
            },
            "arrangements": {"decisions": [{"kind": "continue", "work": item,
                "reason": "Continue the useful work under its frozen contract."}
                for item in direction["work"]["items"]]},
            "following_direction": "Continue useful work and preserve calibration evidence.",
        }
        graph = provider.runtime.owners.research_graph
        wrong_guide = deepcopy(decision)
        wrong_guide["cause"]["guide_ref"]["guidance_hash"] = "0" * 64
        with pytest.raises(OwnerConflict, match="goal_guidance_cause_unbound"):
            graph.evolve_quest_goal(author=author, quest_ref=cut.binding.quest_ref,
                direction_cut=direction, effect_id="wrong-guide",
                decision=parse_evolution_decision(wrong_guide))
        with pytest.raises(OwnerConflict, match="goal_guidance_cause_unbound"):
            graph.evolve_quest_goal(author=replace(author, operation_ref="another-operation"),
                quest_ref=cut.binding.quest_ref, direction_cut=direction,
                effect_id="wrong-operation", decision=parse_evolution_decision(decision))
        wrong_clause = deepcopy(decision)
        wrong_clause["conditions"]["newly_identified"][0]["text"] = "Unconfirmed condition."
        with pytest.raises(OwnerConflict, match="goal_condition_source_invalid"):
            graph.evolve_quest_goal(author=author, quest_ref=cut.binding.quest_ref,
                direction_cut=direction, effect_id="wrong-condition",
                decision=parse_evolution_decision(wrong_clause))
        result = graph.evolve_quest_goal(
            author=author, quest_ref=cut.binding.quest_ref, direction_cut=direction,
            effect_id="direct-goal", decision=parse_evolution_decision(decision))
        assert result["status"] == "accepted"
        assert result["goal"]["goal"]["goal"] == "Compare calibrated devices."
        delivery_record, = human.query_guidance_deliveries(guide["constraint_ref"])
        assert delivery_record["read_at"] is None
        observed["accepted"] = result

    provider = _InboxIdea(primary, lambda *_: None)
    runtime, quest = _run_idea(tmp_path, provider)
    try:
        if not late:
            _confirm_guidance(runtime, quest, {"kind": "quest", "quest_ref": quest},
                strength=2, text="Compare calibrated devices and preserve calibration evidence.")
        _finish(runtime)
        assert observed["accepted"]["goal"]["sequence"] == 1
    finally:
        runtime.close()
