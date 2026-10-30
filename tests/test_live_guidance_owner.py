from __future__ import annotations

from dataclasses import replace

import pytest

from meta_research.owners.common import OwnerConflict
from test_guidance_scope_alignment import _confirm_guidance
from test_human_guidance_inboxes import _InboxIdea, _run_idea, _finish


def test_late_confirmed_guidance_is_readable_and_treatable_in_the_original_operation(tmp_path):
    observed = {}

    def primary(human, operation):
        original = human.freeze_operation_guidance(operation)
        assert original.deliveries == ()
        quest = original.binding.quest_ref
        first = _confirm_guidance(provider.runtime, quest,
            {"kind": "quest", "quest_ref": quest}, key="live-first",
            text="Preserve the negative calibration result.")
        second = _confirm_guidance(provider.runtime, quest,
            {"kind": "quest", "quest_ref": quest}, key="live-independent",
            text="Compare every available device separately.")
        messages = human.prepare_live_guidance(scope=operation.scope, binding=original.binding)
        by_ref = {message["change"]["guide"]["constraint_ref"]: message for message in messages}
        assert set(by_ref) == {first["constraint_ref"], second["constraint_ref"]}
        for confirmed in (first, second):
            message = by_ref[confirmed["constraint_ref"]]
            assert message["change"]["status"] == "active"
            delivery, = message["deliveries"]
            assert delivery["guide"]["guidance"]["text"] == confirmed["guidance"]["text"]
            assert delivery["scope_confirmation"] == "confirmed"
            receipt = human.feedback_operation_guidance(scope=operation.scope,
                binding=original.binding, delivery_ref=delivery["delivery_ref"],
                effect_id="live-feedback-" + confirmed["constraint_ref"],
                understanding=confirmed["guidance"]["text"], changes="Keep the confirmed condition.",
                continuing_work="Continue the current research.", reasons="The guidance applies to this Quest.",
                disposition="applied", goal_impact="none")
            assert receipt["guide_ref"]["receipt_hash"] == confirmed["receipt"]["payload_hash"]
            exact = human.read_operation_guidance(scope=operation.scope, binding=original.binding,
                delivery_ref=delivery["delivery_ref"], effect_id="live-read-" + confirmed["constraint_ref"])
            assert exact["original_text"] == confirmed["guidance"]["text"]
        inbox = human.read_operation_guidance(scope=operation.scope, binding=original.binding)
        assert {item["constraint_ref"] for item in inbox["deliveries"]} == set(by_ref)
        assert human.recover_operation_guidance(original.binding.identity) == original
        assert human.prepare_live_guidance(scope=operation.scope, binding=original.binding) == messages

        human.withdraw_soft_constraint(first["constraint_ref"], first["revision"], "withdraw-live-first")
        after_withdrawal = human.prepare_live_guidance(scope=operation.scope, binding=original.binding)
        statuses = {message["change"]["guide"]["constraint_ref"]: message["change"]["status"]
            for message in after_withdrawal}
        assert statuses == {first["constraint_ref"]: "withdrawn", second["constraint_ref"]: "active"}
        current = human.read_operation_guidance(scope=operation.scope, binding=original.binding)
        current_by_ref = {item["constraint_ref"]: item for item in current["deliveries"]}
        withdrawn = current_by_ref[first["constraint_ref"]]
        assert withdrawn["guidance_status"] == "withdrawn"
        assert withdrawn["applies_to_work"] is False
        assert withdrawn["needs_treatment"] is False
        assert current_by_ref[second["constraint_ref"]]["guidance_status"] == "active"
        with pytest.raises(OwnerConflict, match="guidance_outside_confirmed_scope"):
            human.feedback_operation_guidance(scope=operation.scope, binding=original.binding,
                delivery_ref=withdrawn["delivery_ref"], effect_id="must-not-reapply-withdrawn",
                understanding="The user withdrew this condition.", changes="Keep its history only.",
                continuing_work="Continue the unaffected work.", reasons="Withdrawal changes applicability.",
                disposition="applied", goal_impact="none")
        assert human.recover_operation_guidance(original.binding.identity) == original
        observed["completed"] = True

    provider = _InboxIdea(primary, lambda *_: None)
    runtime, _ = _run_idea(tmp_path, provider)
    try:
        _finish(runtime)
        assert observed["completed"] is True
    finally:
        runtime.close()


def test_live_guidance_rechecks_current_fence_and_rejects_a_finished_root(tmp_path):
    observed = {}

    def primary(human, operation):
        cut = human.freeze_operation_guidance(operation)
        _confirm_guidance(provider.runtime, cut.binding.quest_ref,
            {"kind": "quest", "quest_ref": cut.binding.quest_ref},
            key="live-fenced", text="Keep the accepted evidence.")
        with pytest.raises(OwnerConflict, match="root_agent_human_request_scope_stale"):
            human.prepare_live_guidance(scope=replace(operation.scope, fence_ref="old-fence"),
                binding=cut.binding)
        assert human.prepare_live_guidance(scope=operation.scope, binding=cut.binding)
        observed.update(scope=operation.scope, binding=cut.binding)

    provider = _InboxIdea(primary, lambda *_: None)
    runtime, _ = _run_idea(tmp_path, provider)
    try:
        _finish(runtime)
        with pytest.raises(OwnerConflict, match="idea_runtime_scope_stale"):
            runtime.owners.human_collaboration.prepare_live_guidance(**observed)
    finally:
        runtime.close()


def test_live_guidance_does_not_promote_unconfirmed_scope_to_formal_input(tmp_path):
    observed = {}

    def primary(human, operation):
        cut = human.freeze_operation_guidance(operation)
        assert cut.deliveries == ()
        quest = cut.binding.quest_ref
        unconfirmed = human.submit_human_guidance(quest_ref=quest,
            original_text="A condition whose scope has not been confirmed.",
            idempotency_key="unconfirmed-live-scope")
        assert "semantic_scope" not in unconfirmed["guidance"]
        confirmed = _confirm_guidance(provider.runtime, quest,
            {"kind": "quest", "quest_ref": quest}, key="confirmed-live-scope",
            text="Keep the formally confirmed calibration condition.")

        messages = human.prepare_live_guidance(scope=operation.scope, binding=cut.binding)
        assert [item["change"]["guide"]["constraint_ref"] for item in messages] == [
            confirmed["constraint_ref"]]
        delivery, = messages[0]["deliveries"]
        assert delivery["scope_confirmation"] == "confirmed"
        assert human.query_guidance_deliveries(unconfirmed["constraint_ref"]) == []
        assert human.recover_operation_guidance(cut.binding.identity) == cut
        observed["completed"] = True

    provider = _InboxIdea(primary, lambda *_: None)
    runtime, _ = _run_idea(tmp_path, provider)
    try:
        _finish(runtime)
        assert observed["completed"] is True
    finally:
        runtime.close()
