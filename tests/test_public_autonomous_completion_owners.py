"""Public HC Owner contracts for autonomous creation and Quest completion.

These tests deliberately stay below the two coordinators.  They exercise only
the production-composed Human Collaboration interface and adjacent public
Owner queries; no private table or fixture-only persistence seam is used.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from meta_research.owners.common import (
    AcceptanceReceipt,
    OwnerConflict,
    canonical_hash,
)
from meta_research.reasoning_contract import (
    AUTONOMOUS_QUESTION_SCOPE_SCHEMA_REF,
    CANDIDATE_COMPLETION_SCHEMA_REF,
    SCIENTIFIC_OUTCOME_SCHEMA_REF,
)

from test_public_plan_stage import (
    _DeterministicIdeaSkill,
    _DeterministicPlanSkill,
    _confirm_direct_quest,
    _runtime,
)


_AUTONOMOUS_QUESTION = {
    "title": "跨设备的稀有形态保持边界",
    "unknown_statement": "尚不明确当前结论能否跨显微设备成立。",
    "answer_shape": "形成带反例与适用边界的跨设备比较结论。",
    "applicability_scope": "两个获准的低照度荧光显微设备域。",
    "background_context": "当前结论只覆盖一个设备域。",
    "requirements_constraints": "只复用已接纳证据并显式报告域偏移。",
}


def _owner_runtime(path: Path):
    return _runtime(
        path,
        idea_skill=_DeterministicIdeaSkill(),
        plan_skill=_DeterministicPlanSkill(no_gap=True),
    )


def _autonomous_inputs(runtime, quest: dict[str, object]):
    foreground = runtime.owners.advancement_engine.query_foreground(
        str(quest["quest_ref"])
    )
    goal_revision = runtime.owners.research_graph.query_current_quest_goal_revision(
        str(quest["quest_ref"])
    )
    authorization = (
        runtime.owners.human_collaboration.query_broad_research_authorization(
            str(quest["quest_ref"])
        )
    )
    assert foreground is not None
    assert goal_revision is not None
    assert authorization is not None and authorization["status"] == "granted"

    outcome = {
        "schema_ref": SCIENTIFIC_OUTCOME_SCHEMA_REF,
        "kind": "ScientificOutcomeCandidate",
        "outcome_ref": "scientific-outcome:autonomous-owner",
        "stage_run_request_ref": "stage-run-request:reasoning-owner",
        "cycle_ref": quest["cycle_ref"],
        "question_ref": quest["question_ref"],
        "quest_ref": quest["quest_ref"],
        "goal_revision_ref": goal_revision["goal_revision_ref"],
        "foreground_epoch": foreground["epoch"],
        "disposition": "insufficient_evidence",
        "claim": None,
        "evidence": [],
        "missing_evidence": ["缺少跨设备、同标注口径的正式比较结果。"],
        "uncertainty_basis": [],
        "is_authoritative": False,
    }
    scope = {
        "schema_ref": AUTONOMOUS_QUESTION_SCOPE_SCHEMA_REF,
        "kind": "AutonomousQuestionScope",
        "creation_mode": "AutonomousCreation",
        "mode": "new",
        "source_quest_ref": outcome["quest_ref"],
        "source_cycle_ref": outcome["cycle_ref"],
        "source_reasoning_stage_run_request_ref": outcome[
            "stage_run_request_ref"
        ],
        "source_scientific_outcome_ref": outcome["outcome_ref"],
        "source_question_ref": outcome["question_ref"],
        "source_foreground_epoch": outcome["foreground_epoch"],
        "question_blueprint": dict(_AUTONOMOUS_QUESTION),
        "parent_question_ref": None,
        "decomposition_basis_refs": [],
        "entry_stage": "idea",
        "typed_skip_basis_refs_by_stage": {},
        "is_authoritative": False,
    }
    checkpoint = {
        "schema_ref": "meta-research/reasoning-autonomous-checkpoint/v1",
        "scientific_outcome": outcome,
        "autonomous_scope": scope,
    }
    checkpoint_ref = "reasoning-autonomous-checkpoint:owner-public"
    checkpoint_hash = canonical_hash(checkpoint)
    source = {
        "quest_ref": outcome["quest_ref"],
        "cycle_ref": outcome["cycle_ref"],
        "reasoning_stage_run_request_ref": outcome["stage_run_request_ref"],
        "scientific_outcome_ref": outcome["outcome_ref"],
        "question_ref": outcome["question_ref"],
        "foreground_epoch": outcome["foreground_epoch"],
        "reasoning_checkpoint_ref": checkpoint_ref,
        "reasoning_checkpoint_hash": checkpoint_hash,
        "autonomous_scope_content_acceptance_receipt_ref": (
            "rm-receipt:autonomous-scope"
        ),
        "preliminary_scientific_acceptance_receipt_ref": (
            "rg-receipt:preliminary-science"
        ),
    }
    return {
        "source": source,
        "scientific_outcome": outcome,
        "reasoning_checkpoint_ref": checkpoint_ref,
        "reasoning_checkpoint_hash": checkpoint_hash,
        "autonomous_scope": scope,
        "autonomous_scope_hash": canonical_hash(scope),
        "broad_authorization": authorization,
    }


def _completion_inputs(runtime, quest: dict[str, object]):
    foreground = runtime.owners.advancement_engine.query_foreground(
        str(quest["quest_ref"])
    )
    goal_revision = runtime.owners.research_graph.query_current_quest_goal_revision(
        str(quest["quest_ref"])
    )
    assert foreground is not None
    assert goal_revision is not None
    source = {
        "quest_ref": quest["quest_ref"],
        "cycle_ref": quest["cycle_ref"],
        "reasoning_stage_run_request_ref": "stage-run-request:completion-owner",
        "scientific_outcome_ref": "scientific-outcome:completion-owner",
        "question_ref": quest["question_ref"],
        "foreground_epoch": foreground["epoch"],
    }
    candidate = {
        "schema_ref": CANDIDATE_COMPLETION_SCHEMA_REF,
        "kind": "CandidateCompletion",
        "source_quest_ref": source["quest_ref"],
        "source_cycle_ref": source["cycle_ref"],
        "source_reasoning_stage_run_request_ref": source[
            "reasoning_stage_run_request_ref"
        ],
        "source_scientific_outcome_ref": source["scientific_outcome_ref"],
        "source_question_ref": source["question_ref"],
        "source_foreground_epoch": source["foreground_epoch"],
        "current_quest_ref": source["quest_ref"],
        "current_goal_revision_ref": goal_revision["goal_revision_ref"],
        "completion_milestone_basis_refs": [
            "milestone:accepted:bounded-comparison"
        ],
        "rationale": "当前已接纳里程碑满足精确 Goal revision。",
        "is_authoritative": False,
    }
    return {
        "source": source,
        "candidate_completion": candidate,
        "candidate_completion_ref": "candidate-completion:owner-public",
        "candidate_completion_hash": canonical_hash(candidate),
        "goal_revision": goal_revision,
    }


def test_hc_autonomous_context_is_idempotent_restart_safe_and_receipted(
    tmp_path: Path,
) -> None:
    data_path = tmp_path / "hc-autonomous-owner"
    runtime = _owner_runtime(data_path)
    try:
        quest = _confirm_direct_quest(runtime)
        values = _autonomous_inputs(runtime, quest)
        human = runtime.owners.human_collaboration

        prepared = human.prepare_autonomous_creation(
            **values,
            idempotency_key="hc-autonomous-prepare",
        )
        assert prepared["checkpoint"] == {
            "ref": values["reasoning_checkpoint_ref"],
            "hash": values["reasoning_checkpoint_hash"],
        }
        assert prepared["source"] == values["source"]
        assert prepared["scientific_outcome"] == values["scientific_outcome"]
        assert prepared["scope"] == values["autonomous_scope"]
        assert prepared["scope_hash"] == values["autonomous_scope_hash"]
        assert prepared["broad_authorization"] == values["broad_authorization"]
        assert prepared["generation"] == 1
        assert prepared["proposal"] is None
        assert prepared["selection"] is None
        assert prepared["receipt"] == {
            "status": "accepted",
            "issuer": "human_collaboration",
            "kind": "autonomous_creation_context",
            "receipt_ref": prepared["receipt"]["receipt_ref"],
            "subject_ref": prepared["context_ref"],
            "payload_hash": prepared["receipt"]["payload_hash"],
        }
        assert len(prepared["receipt"]["payload_hash"]) == 64

        replayed = human.prepare_autonomous_creation(
            **values,
            idempotency_key="hc-autonomous-prepare",
        )
        assert replayed == prepared

        # Neither a guessed snapshot nor a correctly shaped but unverified RM
        # receipt may skip the proposal/content Owner facts.
        with pytest.raises(OwnerConflict) as unavailable_snapshot:
            human.form_autonomous_question_proposal(
                str(prepared["context_ref"]),
                literature_snapshot_ref="literature-snapshot:not-accepted",
                idempotency_key="hc-autonomous-proposal",
            )
        assert unavailable_snapshot.value.code == (
            "autonomous_literature_snapshot_invalid"
        )
        shaped_receipt = AcceptanceReceipt(
            issuer="research_memory",
            kind="autonomous_question_content_acceptance",
            receipt_ref="rm-receipt:not-accepted",
            subject_ref="autonomous-content:not-accepted",
            payload_hash=canonical_hash({"not": "accepted"}),
        )
        with pytest.raises(OwnerConflict) as proposal_missing:
            human.select_autonomous_question_content(
                str(prepared["context_ref"]),
                content_ref=shaped_receipt.subject_ref,
                content_hash=canonical_hash(_AUTONOMOUS_QUESTION),
                content_receipt=shaped_receipt,
                idempotency_key="hc-autonomous-selection",
            )
        assert proposal_missing.value.code == "autonomous_proposal_unavailable"
    finally:
        runtime.close()

    restarted = _owner_runtime(data_path)
    try:
        recovered = restarted.owners.human_collaboration.query_autonomous_creation(
            str(values["reasoning_checkpoint_ref"])
        )
        assert recovered == prepared
        assert (
            restarted.owners.human_collaboration.query_current_autonomous_creation()
            == prepared
        )
        assert (
            restarted.owners.human_collaboration.query_autonomous_creation_context(
                str(prepared["context_ref"])
            )
            == prepared
        )
        assert (
            restarted.owners.human_collaboration.query_autonomous_creation_contexts()
            == (prepared,)
        )
    finally:
        restarted.close()


def test_hc_quest_completion_preview_is_exact_idempotent_and_restart_safe(
    tmp_path: Path,
) -> None:
    data_path = tmp_path / "hc-quest-completion-owner"
    runtime = _owner_runtime(data_path)
    try:
        quest = _confirm_direct_quest(runtime)
        values = _completion_inputs(runtime, quest)
        human = runtime.owners.human_collaboration

        prepared = human.prepare_quest_completion(
            **values,
            idempotency_key="hc-completion-prepare",
        )
        assert prepared["source"] == values["source"]
        assert prepared["candidate_completion_ref"] == values[
            "candidate_completion_ref"
        ]
        assert prepared["candidate_completion_hash"] == values[
            "candidate_completion_hash"
        ]
        assert prepared["candidate_completion"] == values[
            "candidate_completion"
        ]
        assert prepared["goal_revision"] == values["goal_revision"]
        assert prepared["human_confirmation"] == {
            "preview": None,
            "decision": None,
        }
        assert human.prepare_quest_completion(
            **values,
            idempotency_key="hc-completion-prepare",
        ) == prepared
    finally:
        runtime.close()

    # Losing the prepare response and restarting must recover the same context,
    # not create a second CandidateCompletion workflow.
    preview_runtime = _owner_runtime(data_path)
    try:
        human = preview_runtime.owners.human_collaboration
        assert human.query_current_quest_completion() == prepared
        preview = human.preview_quest_completion(
            str(prepared["context_ref"]),
            idempotency_key="hc-completion-preview",
        )
        assert preview == {
            "candidate_completion_ref": values["candidate_completion_ref"],
            "candidate_completion_hash": values["candidate_completion_hash"],
            "quest_ref": quest["quest_ref"],
            "goal_revision_ref": values["goal_revision"]["goal_revision_ref"],
            "completion_milestone_basis_refs": values["candidate_completion"][
                "completion_milestone_basis_refs"
            ],
            "status": "current",
            "ref": preview["ref"],
            "hash": preview["hash"],
        }
        assert human.preview_quest_completion(
            str(prepared["context_ref"]),
            idempotency_key="hc-completion-preview",
        ) == preview
    finally:
        preview_runtime.close()

    # A second restart after preview exercises the ACK-loss boundary.  The
    # stale attempt is rejected without consuming that exact current preview.
    decision_runtime = _owner_runtime(data_path)
    try:
        human = decision_runtime.owners.human_collaboration
        recovered_preview = human.query_current_quest_completion()
        assert recovered_preview is not None
        assert recovered_preview["human_confirmation"] == {
            "preview": preview,
            "decision": None,
        }
        with pytest.raises(OwnerConflict) as stale:
            human.decide_quest_completion(
                preview_ref=str(preview["ref"]),
                preview_hash="0" * 64,
                decision="confirmed",
                idempotency_key="hc-completion-confirm",
            )
        assert stale.value.code == "quest_completion_preview_stale"
        assert human.query_current_quest_completion()["human_confirmation"] == {
            "preview": preview,
            "decision": None,
        }

        decision = human.decide_quest_completion(
            preview_ref=str(preview["ref"]),
            preview_hash=str(preview["hash"]),
            decision="confirmed",
            idempotency_key="hc-completion-confirm",
        )
        assert decision == {
            "decision": "confirmed",
            "receipt": {
                "status": "accepted",
                "issuer": "human_collaboration",
                "kind": "quest_completion_confirmation",
                "receipt_ref": decision["receipt"]["receipt_ref"],
                "subject_ref": preview["ref"],
                "payload_hash": decision["receipt"]["payload_hash"],
            },
        }
        assert len(decision["receipt"]["payload_hash"]) == 64
        assert human.decide_quest_completion(
            preview_ref=str(preview["ref"]),
            preview_hash=str(preview["hash"]),
            decision="confirmed",
            idempotency_key="hc-completion-confirm",
        ) == decision
        accepted = human.query_current_quest_completion()
        assert accepted is not None
        assert accepted["context_ref"] == prepared["context_ref"]
        assert accepted["human_confirmation"] == {
            "preview": preview,
            "decision": decision,
        }
    finally:
        decision_runtime.close()

    restarted = _owner_runtime(data_path)
    try:
        recovered = (
            restarted.owners.human_collaboration.query_current_quest_completion()
        )
        assert recovered == accepted
        assert (
            restarted.owners.human_collaboration.query_quest_completion_contexts()
            == (accepted,)
        )
        # HC confirmation is a collaboration receipt only: it cannot itself
        # end the Quest or move the AE foreground.
        foreground = restarted.owners.advancement_engine.query_foreground(
            str(quest["quest_ref"])
        )
        assert foreground is not None
        assert foreground["status"] == "active"
        assert foreground["cycle_ref"] == quest["cycle_ref"]
    finally:
        restarted.close()
