from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, cast

from sqlalchemy import text

from meta_research.owners.common import OwnerConflict, canonical_hash, decoded_object


LOGGER = logging.getLogger(__name__)


def _rotate_by_key(
    items: list[Any], cursor: str | None, key_fn: Callable[[Any], str]
) -> list[Any]:
    if cursor is None or not items:
        return items
    index = next((idx for idx, item in enumerate(items) if key_fn(item) == cursor), None)
    if index is None:
        return items
    return items[index + 1 :] + items[: index + 1]


def _error_quest_ref(error: OwnerConflict) -> object | None:
    return error.details.get("quest_ref") if isinstance(error.details, dict) else None


class GoalWorkReconciler:
    """Apply at most one issuer-owned mechanical work effect per tick."""

    def __init__(self, research_graph, target_lifecycle, advancement_engine) -> None:
        self._research_graph = research_graph
        self._target_lifecycle = target_lifecycle
        self._advancement_engine = advancement_engine
        self._intent_cursor: str | None = None
        self._reassessment_cursor: str | None = None
        self._transient_error: str | None = None

    @property
    def transient_error(self) -> str | None:
        return self._transient_error

    def _apply_intent(
        self, intent_ref: str, decision_kind: str, quest_ref: object | None, *, update_cursor: bool,
    ) -> bool:
        try:
            if decision_kind == "stop":
                self._target_lifecycle.request_cancel_exact(intent_ref=intent_ref)
            else:
                self._target_lifecycle.block_admission_exact(intent_ref=intent_ref)
            if update_cursor:
                self._intent_cursor = intent_ref
            return True
        except OwnerConflict as error:
            LOGGER.warning(
                "goal_work_reconciler intent isolated quest_ref=%s intent_ref=%s error_code=%s",
                quest_ref if quest_ref is not None else _error_quest_ref(error),
                intent_ref,
                error.code,
            )
            self._transient_error = self._transient_error or error.code
            if update_cursor:
                self._intent_cursor = intent_ref
            return False

    def _process_intents(self) -> bool:
        database = getattr(self._research_graph, "_database", None)
        if database is None:
            intent = self._research_graph.next_unfulfilled_goal_work_intent()
            if intent is None:
                return False
            return self._apply_intent(
                intent["intent_ref"], intent["decision_kind"], intent.get("quest_ref"), update_cursor=False,
            )

        candidates: list[tuple[str, str, str, str]] = []
        with database.read() as connection:
            rows = connection.execute(
                text(
                    "SELECT i.*, r.quest_ref AS quest_ref FROM rg_goal_work_intents i "
                    "JOIN rg_quest_goal_revisions r ON r.revision_ref=i.revision_ref "
                    "JOIN rg_quest_goal_heads h ON h.quest_ref=r.quest_ref AND "
                    "h.current_revision_ref=i.revision_ref WHERE i.decision_kind "
                    "IN ('stop','do_not_start') ORDER BY i.created_at,i.intent_ref"
                )
            ).all()
            candidates_raw = _rotate_by_key(list(rows), self._intent_cursor, lambda row: row.intent_ref)
            for row in candidates_raw:
                try:
                    try:
                        decision = decoded_object(row.decision_json)
                        target_ref = cast(str, decision["work"]["target_ref"])
                    except (TypeError, ValueError, KeyError) as error:
                        raise OwnerConflict("goal_work_intent_invalid") from error
                    if canonical_hash(decision) != row.decision_hash:
                        raise OwnerConflict("goal_work_intent_invalid")
                    if row.decision_kind == "stop":
                        lifecycle = connection.execute(
                            text(
                                "SELECT status,cancel_goal_intent_ref FROM "
                                "ar_target_root_lifecycles WHERE target_ref=:target_ref"
                            ),
                            {"target_ref": target_ref},
                        ).first()
                        unfulfilled = (
                            lifecycle is not None
                            and lifecycle.status == "running"
                            and lifecycle.cancel_goal_intent_ref is None
                        )
                    else:
                        block = connection.execute(
                            text(
                                "SELECT 1 FROM ar_target_goal_admission_blocks WHERE "
                                "target_ref=:target_ref"
                            ),
                            {"target_ref": target_ref},
                        ).first()
                        activation = connection.execute(
                            text(
                                "SELECT 1 FROM ar_target_run_activations WHERE "
                                "target_ref=:target_ref"
                            ),
                            {"target_ref": target_ref},
                        ).first()
                        unfulfilled = block is None and activation is None
                    if unfulfilled:
                        candidates.append(
                            (str(row.intent_ref), str(row.decision_kind), str(row.quest_ref), target_ref)
                        )
                except OwnerConflict as error:
                    LOGGER.warning(
                        "goal_work_reconciler intent isolated quest_ref=%s intent_ref=%s error_code=%s",
                        row.quest_ref,
                        row.intent_ref,
                        error.code,
                    )
                    self._transient_error = self._transient_error or error.code
                    self._intent_cursor = str(row.intent_ref)

        for intent_ref, decision_kind, quest_ref, _target_ref in candidates:
            if self._apply_intent(intent_ref, decision_kind, quest_ref, update_cursor=True):
                return True
        return False

    def _process_reassessments(self) -> bool:
        database = getattr(self._advancement_engine, "_database", None)
        verifier = getattr(self._advancement_engine, "_reasoning_outcome_verifier", None)
        if database is None or verifier is None:
            try:
                reassessment = self._advancement_engine.query_goal_reasoning_reassessment()
            except OwnerConflict as error:
                LOGGER.warning(
                    "goal_work_reconciler reassessment isolated quest_ref=%s error_code=%s",
                    _error_quest_ref(error),
                    error.code,
                )
                self._transient_error = self._transient_error or error.code
                return False
            if reassessment is None:
                return False
            try:
                self._advancement_engine.ensure_goal_reasoning_reassessment(
                    quest_ref=reassessment["quest_ref"],
                    goal_revision_ref=reassessment["goal_revision_ref"],
                    idempotency_key=(
                        "goal-reasoning-reassessment-"
                        + str(reassessment["goal_revision_ref"])
                        + ":" + str(reassessment["source_epoch"])
                    ),
                )
                return True
            except OwnerConflict as error:
                LOGGER.warning(
                    "goal_work_reconciler reassessment isolated quest_ref=%s error_code=%s",
                    reassessment.get("quest_ref"),
                    error.code,
                )
                self._transient_error = self._transient_error or error.code
                return False

        with database.read() as connection:
            rows = connection.execute(
                text(
                    "SELECT h.quest_ref,h.cycle_ref,h.question_ref,h.epoch,"
                    "g.grant_ref,r.request_ref,r.context_pack_json,"
                    "r.context_pack_hash,c.commit_ref,c.closure_json,"
                    "c.closure_hash FROM ae_foreground_heads h JOIN "
                    "ae_foreground_grants g ON g.quest_ref=h.quest_ref AND "
                    "g.epoch=h.epoch JOIN ae_cycles y ON y.cycle_ref=h.cycle_ref "
                    "JOIN ae_stage_run_requests r ON r.cycle_ref=h.cycle_ref AND "
                    "r.stage='reasoning' AND r.epoch=h.epoch JOIN "
                    "ae_stage_commits c ON c.request_ref=r.request_ref WHERE "
                    "h.stage='reasoning' AND h.status='active' AND "
                    "EXISTS (SELECT 1 FROM rg_quest_goal_heads z WHERE "
                    "z.quest_ref=h.quest_ref AND z.status='open') AND "
                    "h.pending_operation_ref IS NULL AND g.status='active' AND "
                    "y.status='ongoing' AND c.disposition='completed' ORDER BY "
                    "h.updated_at,h.quest_ref"
                )
            ).all()

        rotated = _rotate_by_key(list(rows), self._reassessment_cursor, lambda row: row.quest_ref)
        for row in rotated:
            try:
                try:
                    context_pack = decoded_object(row.context_pack_json)
                    closure = decoded_object(row.closure_json)
                except (TypeError, ValueError) as error:
                    raise OwnerConflict("goal_reasoning_reassessment_invalid") from error
                if (
                    not isinstance(context_pack, dict)
                    or canonical_hash(context_pack) != row.context_pack_hash
                    or not isinstance(closure, dict)
                    or canonical_hash(closure) != row.closure_hash
                    or closure.get("transition_kind") != "candidate_completion"
                ):
                    raise OwnerConflict("goal_reasoning_reassessment_invalid")
                research_context = context_pack.get("research_context")
                if not isinstance(research_context, dict):
                    raise OwnerConflict("goal_reasoning_reassessment_invalid")
                current = verifier.query_current_quest_goal_revision(row.quest_ref)
                if current is None:
                    raise OwnerConflict("quest_goal_revision_unavailable")
                verifier.verify_current_quest_goal_revision(current)
                if research_context.get("goal_revision_ref") == current.get("goal_revision_ref"):
                    with database.read() as connection:
                        if not self._advancement_engine._completion_conditions_stale(
                            connection, closure, row.quest_ref
                        ):
                            continue
                goal_revision_ref = str(current["goal_revision_ref"])
                self._advancement_engine.ensure_goal_reasoning_reassessment(
                    quest_ref=row.quest_ref,
                    goal_revision_ref=goal_revision_ref,
                    idempotency_key=(
                        "goal-reasoning-reassessment-"
                        + goal_revision_ref
                        + ":" + str(int(row.epoch))
                    ),
                )
                self._reassessment_cursor = str(row.quest_ref)
                return True
            except OwnerConflict as error:
                LOGGER.warning(
                    "goal_work_reconciler reassessment isolated quest_ref=%s cycle_ref=%s error_code=%s",
                    row.quest_ref,
                    row.cycle_ref,
                    error.code,
                )
                self._transient_error = self._transient_error or error.code
                self._reassessment_cursor = str(row.quest_ref)
        return False

    def process_once(self) -> bool:
        self._transient_error = None
        if self._process_intents():
            return True
        return self._process_reassessments()
