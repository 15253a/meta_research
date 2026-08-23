from __future__ import annotations

from typing import cast

from meta_research.owners.common import OwnerConflict, canonical_hash


CONTROL_ACTIONS = frozenset(
    {
        "pause",
        "resume",
        "normal_switch",
        "forced_switch",
        "cancel",
        "abandon",
        "prune",
        "restore",
    }
)
SWITCH_ACTIONS = frozenset({"normal_switch", "forced_switch"})
QUESTION_ACTIONS = frozenset({*SWITCH_ACTIONS, "prune", "restore"})
FORCE_FENCE_ACTIONS = frozenset({"forced_switch", "cancel", "abandon", "prune"})
TERMINAL_ACTIONS = frozenset({"cancel", "abandon"})
CONTROL_TARGET_SCOPES = frozenset({"cycle", "stage", "run"})
RUN_ACTIONS = frozenset({"pause", "resume", "cancel"})
STAGE_ACTIONS = frozenset({"pause", "resume", "cancel"})


def validate_control_payload(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {"action", "target", "reason"}:
        raise OwnerConflict("research_control_payload_invalid")
    action = value.get("action")
    target = value.get("target")
    reason = value.get("reason")
    if action not in CONTROL_ACTIONS or not isinstance(target, dict):
        raise OwnerConflict("research_control_payload_invalid")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 512:
        raise OwnerConflict("research_control_reason_invalid")
    required = {"quest_ref", "cycle_ref", "question_ref", "epoch"}
    allowed = required | {
        "target_scope",
        "run_ref",
        "target_question_ref",
        "prune_record_ref",
    }
    if not required.issubset(target) or not set(target).issubset(allowed):
        raise OwnerConflict("research_control_target_invalid")
    for field in ("quest_ref", "cycle_ref", "question_ref"):
        item = target.get(field)
        if not isinstance(item, str) or not item or len(item) > 96:
            raise OwnerConflict("research_control_target_invalid")
    epoch = target.get("epoch")
    if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1:
        raise OwnerConflict("research_control_target_invalid")
    target_scope = target.get("target_scope", "cycle")
    if target_scope not in CONTROL_TARGET_SCOPES:
        raise OwnerConflict("research_control_target_invalid")
    run_ref = target.get("run_ref")
    if target_scope == "run":
        if (
            action not in RUN_ACTIONS
            or not isinstance(run_ref, str)
            or not run_ref
            or len(run_ref) > 96
        ):
            raise OwnerConflict("research_control_run_target_invalid")
    elif run_ref is not None:
        raise OwnerConflict("research_control_target_invalid")
    if target_scope == "stage" and action not in STAGE_ACTIONS:
        raise OwnerConflict("research_control_stage_action_invalid")
    if action in QUESTION_ACTIONS and target_scope != "cycle":
        raise OwnerConflict("research_control_target_invalid")
    target_question_ref = target.get("target_question_ref")
    prune_record_ref = target.get("prune_record_ref")
    if action in QUESTION_ACTIONS:
        if (
            not isinstance(target_question_ref, str)
            or not target_question_ref
            or len(target_question_ref) > 96
        ):
            raise OwnerConflict("research_control_question_target_required")
    elif target_question_ref is not None:
        raise OwnerConflict("research_control_target_invalid")
    if action == "restore":
        if (
            not isinstance(prune_record_ref, str)
            or not prune_record_ref
            or len(prune_record_ref) > 96
        ):
            raise OwnerConflict("research_control_prune_record_required")
    elif prune_record_ref is not None:
        raise OwnerConflict("research_control_target_invalid")
    return {
        "action": cast(str, action),
        "target": {
            "quest_ref": str(target["quest_ref"]),
            "cycle_ref": str(target["cycle_ref"]),
            "question_ref": str(target["question_ref"]),
            "epoch": int(target["epoch"]),
            "target_scope": str(target_scope),
            **({"run_ref": str(run_ref)} if run_ref is not None else {}),
            **(
                {"target_question_ref": str(target_question_ref)}
                if target_question_ref is not None
                else {}
            ),
            **(
                {"prune_record_ref": str(prune_record_ref)}
                if prune_record_ref is not None
                else {}
            ),
        },
        "reason": reason.strip(),
    }


def signed_owner_preview(
    *,
    source_owner: str,
    target_assertion: dict[str, object],
    will_happen: list[str],
    will_not_happen: list[str],
    risks: list[str],
    stale_conditions: list[str],
) -> dict[str, object]:
    preview = {
        "source_owner": source_owner,
        "target_assertion": target_assertion,
        "will_happen": will_happen,
        "will_not_happen": will_not_happen,
        "risks": risks,
        "stale_conditions": stale_conditions,
    }
    return {**preview, "digest": canonical_hash(preview)}
