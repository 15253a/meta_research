from pathlib import Path

import pytest

from meta_research.owners.common import OwnerConflict
from test_public_human_collaboration_ladder import (
    _DeterministicDraftingProvider,
    _confirm_direct_quest,
    _runtime,
)


def test_confirmation_binds_the_strength_shown_in_the_full_proposal(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path / "confirmed-guidance", _DeterministicDraftingProvider())
    try:
        human = runtime.owners.human_collaboration
        quest = _confirm_direct_quest(runtime)
        for _ in range(5):
            if not human.reconcile_once():
                break
        quest_ref = human.query_quest_creation(quest["initialization_id"])["quest_ref"]
        scope_ref = f"quest:{quest_ref}"
        proposal_value = {
            "proposal_kind": "soft_constraint",
            "text": "本次研究优先复核学习率；总目标不变。",
            "assistant_understanding": "优先复核学习率，保留当前总目标。",
            "applies_to": ["本 Quest 的学习率复核"],
            "semantic_scope": {"kind": "quest", "quest_ref": quest_ref},
            "strength": 4,
            "preserve_conditions": ["总目标不变"],
            "work_materials": None,
        }
        proposal = human.record_agent_proposal(scope_ref, proposal_value, "confirmed-guidance-proposal")
        with pytest.raises(OwnerConflict, match="guidance_confirmation_stale"):
            human.convert_agent_proposal_to_soft_constraint(
                proposal["proposal_ref"], expected_scope_ref=scope_ref,
                expected_proposal_hash=proposal["proposal_hash"],
                strength=5, idempotency_key="confirmed-guidance-wrong-strength",
            )
        converted = human.convert_agent_proposal_to_soft_constraint(
            proposal["proposal_ref"], expected_scope_ref=scope_ref,
            expected_proposal_hash=proposal["proposal_hash"],
            strength=4, idempotency_key="confirmed-guidance-convert",
        )
        assert converted["soft_constraint"]["guidance"] == proposal_value
        replay = human.convert_agent_proposal_to_soft_constraint(
            proposal["proposal_ref"], expected_scope_ref=scope_ref,
            expected_proposal_hash=proposal["proposal_hash"],
            strength=4, idempotency_key="confirmed-guidance-convert",
        )
        assert replay["soft_constraint"] == converted["soft_constraint"]
    finally:
        runtime.close()


def test_editing_a_proposal_invalidates_old_confirmation_and_late_edits(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path / "edited-guidance", _DeterministicDraftingProvider())
    try:
        human = runtime.owners.human_collaboration
        quest = _confirm_direct_quest(runtime)
        for _ in range(5):
            if not human.reconcile_once():
                break
        quest_ref = human.query_quest_creation(quest["initialization_id"])["quest_ref"]
        scope_ref = f"quest:{quest_ref}"
        original = {
            "proposal_kind": "soft_constraint", "text": "复核学习率，保留总目标。",
            "assistant_understanding": "只复核学习率。", "applies_to": ["本研究"],
            "semantic_scope": {"kind": "quest", "quest_ref": quest_ref},
            "strength": 3, "preserve_conditions": ["保留总目标"], "work_materials": None,
        }
        proposal = human.record_agent_proposal(scope_ref, original, "edit-guidance-record")
        edited = {**original, "assistant_understanding": "优先复核学习率，并保留总目标。", "strength": 5}
        revision = human.revise_agent_proposal(
            proposal["proposal_ref"], expected_scope_ref=scope_ref,
            expected_proposal_hash=proposal["proposal_hash"], proposal=edited,
            idempotency_key="edit-guidance-revision",
        )
        with pytest.raises(OwnerConflict, match="agent_proposal_stale"):
            human.convert_agent_proposal_to_soft_constraint(
                proposal["proposal_ref"], expected_scope_ref=scope_ref,
                expected_proposal_hash=proposal["proposal_hash"], strength=3,
                idempotency_key="edit-guidance-old-confirmation",
            )
        with pytest.raises(OwnerConflict, match="agent_proposal_stale"):
            human.revise_agent_proposal(
                proposal["proposal_ref"], expected_scope_ref=scope_ref,
                expected_proposal_hash=proposal["proposal_hash"], proposal=original,
                idempotency_key="edit-guidance-late-revision",
            )
        assert human.revise_agent_proposal(
            proposal["proposal_ref"], expected_scope_ref=scope_ref,
            expected_proposal_hash=proposal["proposal_hash"], proposal=edited,
            idempotency_key="edit-guidance-revision",
        ) == revision
        converted = human.convert_agent_proposal_to_soft_constraint(
            revision["proposal_ref"], expected_scope_ref=scope_ref,
            expected_proposal_hash=revision["proposal_hash"], strength=5,
            idempotency_key="edit-guidance-new-confirmation",
        )
        assert converted["soft_constraint"]["guidance"] == edited
    finally:
        runtime.close()


def test_guidance_web_requires_a_complete_confirmed_proposal(tmp_path: Path) -> None:
    from test_public_human_collaboration_web import (
        _authenticated_client, _confirm_direct_quest as confirm_web_quest,
        _runtime as web_runtime, _write_headers,
    )
    runtime = web_runtime(tmp_path / "guidance-web-confirmation")
    quest = confirm_web_quest(runtime)
    scope_ref = f"quest:{quest['quest_ref']}"
    client, auth = _authenticated_client(runtime)
    try:
        with client:
            response = client.post("/api/v1/human-collaboration/guidance",
                headers=_write_headers(auth, "unconfirmed-guidance"),
                json={"scope_ref": scope_ref, "text": "改变研究方向", "strength": 5})
            assert response.status_code == 409
            assert response.json()["detail"]["code"] == "guidance_confirmation_required"
            incomplete = runtime.owners.human_collaboration.record_agent_proposal(
                scope_ref, {"proposal_kind": "soft_constraint", "text": "只改学习率"},
                "incomplete-guidance-proposal")
            response = client.post(
                f"/api/v1/human-collaboration/agent-proposals/{incomplete['proposal_ref']}/soft-constraint",
                headers=_write_headers(auth, "incomplete-guidance-confirmation"),
                json={"expected_scope_ref": scope_ref,
                      "expected_proposal_hash": incomplete["proposal_hash"], "strength": 5})
            assert response.status_code == 409
            assert response.json()["detail"]["code"] == "guidance_confirmation_required"
            assert runtime.owners.human_collaboration.query_collaboration_projection(
                (scope_ref,))["soft_constraints"] == []
    finally:
        runtime.close()


def test_companion_keeps_human_words_and_selected_strength_until_confirmation(tmp_path: Path) -> None:
    provider = _DeterministicDraftingProvider()
    runtime = _runtime(tmp_path / "companion-guidance-intent", provider)
    try:
        human = runtime.owners.human_collaboration
        quest = _confirm_direct_quest(runtime)
        for _ in range(5):
            if not human.reconcile_once():
                break
        quest_ref = human.query_quest_creation(quest["initialization_id"])["quest_ref"]
        scope_ref = f"quest:{quest_ref}"
        human.send_companion_message(scope_ref, "解释一下学习率的作用。", "explain-learning-rate")
        assert human.process_drafting_once()
        assert human.query_collaboration_projection((scope_ref,))["agent_proposals"] == []
        original = "  本次实验严格只换学习率，总目标不变。  "
        provider.agent_proposal = {
            "proposal_kind": "soft_constraint", "text": "助手不准确的概括",
            "assistant_understanding": "仅更换学习率，保留总目标。",
            "applies_to": ["当前研究的学习率复核"],
            "semantic_scope": {"kind": "quest", "quest_ref": quest_ref},
            "strength": 3, "preserve_conditions": ["总目标不变"], "work_materials": None,
        }
        queued = human.send_companion_message(scope_ref, original, "selected-strength-guidance",
            guidance_options={"strength": 5, "work_materials": None})
        assert human.process_drafting_once()
        projection = human.query_collaboration_projection((scope_ref,))
        proposal, = projection["agent_proposals"]
        assert {name: proposal["proposal"][name] for name in ("text", "strength", "work_materials")} == {
            "text": original, "strength": 5, "work_materials": None}
        assert projection["soft_constraints"] == []
        assert human.query_companion(scope_ref)["turns"][-1]["guidance_options"] == {
            "strength": 5, "work_materials": None}
        assert provider.intent_requests[-1].draft["selected_guidance"] == {
            "strength": 5, "work_materials": None}
    finally:
        runtime.close()


def test_companion_provider_returns_an_understood_target_scope_for_human_review(tmp_path: Path) -> None:
    import json
    import subprocess
    from meta_research.companion import CodexCompanionAdapter
    from meta_research.quest_drafting import IntentTurnRequest

    proposal = {
        "proposal_kind": "soft_constraint", "text": "本次实验严格只换学习率，总目标不变。",
        "assistant_understanding": "仅更换本实验的学习率，保持总目标及其他实验。",
        "applies_to": ["当前目标实验"], "strength": 5,
        "semantic_scope": {"kind": "target", "quest_ref": "quest_233", "question_ref": "question_233",
                           "cycle_ref": "cycle_233", "target_ref": "target_233"},
        "preserve_conditions": ["总目标不变"],
    }

    def provider_process(argv, prompt, timeout):
        del timeout
        Path(argv[argv.index("--output-last-message") + 1]).write_text(
            json.dumps({"reply": "请确认上述理解、当前实验范围与力度。", "agent_proposal": proposal}),
            encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"type": "thread.started",
            "thread_id": "native-guidance-confirmation"}), stderr="")

    adapter = CodexCompanionAdapter(tmp_path / "companion-provider", process_runner=provider_process)
    result = adapter.reply(IntentTurnRequest(
        initialization_id="quest:quest_233", draft_revision=0, draft_hash="1" * 64,
        draft={"interaction_kind": "conversation", "selected_guidance": {"strength": 5, "work_materials": None}},
        message=proposal["text"], native_session_ref=None))
    assert result.agent_proposal == proposal


def test_companion_reads_actual_target_identities_before_proposing_experiment_scope(tmp_path: Path) -> None:
    from test_quest_goal_concurrent_roots import _ParallelBundle, _two_admitted_roots
    from test_target_root_finalizer import _current_bundle_runtime

    provider = _DeterministicDraftingProvider()
    runtime = _current_bundle_runtime(tmp_path / "companion-target-context",
        bundle_skill=_ParallelBundle(), intent_drafting_provider=provider)
    try:
        quest_ref, _, targets = _two_admitted_roots(runtime)
        human = runtime.owners.human_collaboration
        human.send_companion_message("quest:" + quest_ref, "当前有哪些实验？", "companion-target-context")
        assert human.process_drafting_once()
        context = provider.intent_requests[-1].draft["current_context"]
        actual = context["activity"]["research_targets"]
        assert {item["target_ref"] for item in actual} == targets
        assert all(item["quest_ref"] == quest_ref
            and item["question_ref"] == context["current_question"]["question_ref"]
            and item["cycle_ref"] == context["activity"]["foreground"]["cycle_ref"]
            for item in actual)
    finally:
        runtime.close()
