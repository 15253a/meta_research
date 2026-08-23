from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol, cast

from sqlalchemy import text

from meta_research.control_contract import (
    FORCE_FENCE_ACTIONS,
    QUESTION_ACTIONS,
    SWITCH_ACTIONS,
    signed_owner_preview,
    validate_control_payload,
)
from meta_research.database import Database
from meta_research.feed import DurableFeed
from meta_research.idea_contract import (
    IdeaContractError,
    evidence_reference_revision,
    literature_binding,
    validate_idea_context_pack,
)
from meta_research.plan_contract import (
    PlanContractError,
    validate_plan_context_pack,
)
from meta_research.owners._sqlite_snapshot import (
    OwnerSnapshotQuery,
    SQLiteOwnerSnapshot,
)
from meta_research.owners.common import (
    AcceptedIdeaSetBinding,
    AcceptedQuestionBinding,
    AcceptedQuestionBindingVerifier,
    AcceptanceReceipt,
    EvidenceRefVerifier,
    FormalPlanDecisionVerifier,
    IdeaOutcomeDecisionVerifier,
    LiteratureSnapshotVerifier,
    OwnerConflict,
    OwnerSnapshot,
    QuestReceiptVerifier,
    RootQuestionReceiptVerifier,
    RunCompletionReceiptVerifier,
    VerifiedStageRunRequestBinding,
    canonical_hash,
    canonical_json,
    decoded_object,
    new_ref,
)
from meta_research.owners.human_requests import (
    HumanRequestOwnerInterface,
    HumanRequestOwnerMixin,
    HumanResponseVerifier,
)
from meta_research.owners.research_graph import AcceptedQuestion, AcceptedQuest


AE_OWNER = "advancement_engine"
CYCLE_RECEIPT_KIND = "initial_cycle_activation"
STAGE_REQUEST_RECEIPT_KIND = "stage_run_request"
STAGE_COMMIT_RECEIPT_KIND = "stage_commit"
IDEA_STAGE = "idea"
PLAN_STAGE = "plan"
IDEA_SET_OUTCOME_KIND = "idea_set"
NO_VIABLE_CANDIDATE_OUTCOME_KIND = "no_viable_candidate"
FORMAL_PLAN_OUTCOME_KIND = "formal_plan"
COMPLETED_DISPOSITION = "completed"
SKIPPED_DISPOSITION = "skipped"
EXHAUSTED_DISPOSITION = "exhausted"
BASIS_DISPOSITIONS = {SKIPPED_DISPOSITION, EXHAUSTED_DISPOSITION}
STAGES = ("idea", "plan", "bundle", "reasoning")
NEXT_STAGE = {
    "idea": "plan",
    "plan": "bundle",
    "bundle": "reasoning",
}
COMPLETABLE_IDEA_OUTCOME_KINDS = {
    IDEA_SET_OUTCOME_KIND,
    NO_VIABLE_CANDIDATE_OUTCOME_KIND,
}
RECEIPT_SCHEMA = "meta-research/owner-acceptance-receipt/v1"


class RuntimeControlReceiptVerifier(Protocol):
    def verify_runtime_control_receipt(
        self,
        *,
        operation_ref: str,
        action: str,
        target: dict[str, object],
        receipt: dict[str, object],
    ) -> None: ...


class QuestionControlReceiptVerifier(Protocol):
    def verify_question_control_receipt(
        self,
        *,
        operation_ref: str,
        action: str,
        target: dict[str, object],
        receipt: dict[str, object],
    ) -> None: ...


class CurrentQuestionVerifier(Protocol):
    def verify_current_question(
        self,
        *,
        quest_ref: str,
        question_ref: str,
        question_receipt_ref: str,
        question_receipt_hash: str,
    ) -> None: ...


class StageDispositionBasisVerifier(Protocol):
    def verify_stage_disposition_basis(
        self,
        *,
        cycle_ref: str,
        quest_ref: str,
        question_ref: str,
        stage: str,
        epoch: int,
        disposition: str,
        basis_kind: str,
        basis_ref: str,
        receipt: AcceptanceReceipt,
    ) -> None: ...


@dataclass(frozen=True)
class ActivatedCycle:
    cycle_ref: str
    receipt: AcceptanceReceipt


@dataclass(frozen=True)
class StageRunRequest:
    request_ref: str
    cycle_ref: str
    stage: str
    epoch: int
    context_pack_ref: str
    context_pack_hash: str
    context_pack: dict[str, object]
    accepted_question: AcceptedQuestionBinding
    receipt: AcceptanceReceipt
    accepted_idea_set: AcceptedIdeaSetBinding | None = None


@dataclass(frozen=True)
class StageCommit:
    commit_ref: str
    request_ref: str | None
    cycle_ref: str
    stage: str
    epoch: int
    run_ref: str | None
    outcome_ref: str | None
    outcome_kind: str | None
    disposition: str
    run_completion_receipt: AcceptanceReceipt | None
    outcome_receipt: AcceptanceReceipt | None
    basis_kind: str | None
    basis_ref: str | None
    basis_receipt: AcceptanceReceipt | None
    receipt: AcceptanceReceipt


class AdvancementEngineInterface(HumanRequestOwnerInterface, Protocol):
    """Whole public Interface for Cycle, Stage, and Foreground authority."""

    def query_snapshot(self) -> OwnerSnapshot: ...

    def query_foreground(self, quest_ref: str) -> dict[str, object] | None: ...

    def query_active_foregrounds(
        self, *, stage: str | None = None
    ) -> tuple[dict[str, object], ...]: ...

    def query_foreground_control_by_intent(
        self, intent_id: str
    ) -> dict[str, object] | None: ...

    def query_recoverable_foreground_controls(
        self,
    ) -> tuple[dict[str, object], ...]: ...

    def preview_foreground_control(
        self, payload: dict[str, object]
    ) -> tuple[dict[str, object], int]: ...

    def prepare_foreground_control(
        self,
        *,
        intent_id: str,
        payload: dict[str, object],
        expected_revision: int,
        idempotency_key: str,
        target_question: AcceptedQuestion | None = None,
    ) -> dict[str, object]: ...

    def complete_foreground_control(
        self,
        *,
        operation_ref: str,
        runtime_receipt: dict[str, object],
        graph_receipt: dict[str, object] | None,
        idempotency_key: str,
    ) -> dict[str, object]: ...

    def abort_foreground_control(
        self, *, operation_ref: str, reason_code: str
    ) -> None: ...

    def preview_initial_cycle_activation(
        self,
        *,
        initialization_id: str,
        proposal_ref: str,
        proposal_hash: str,
    ) -> dict[str, object]: ...

    def query_initial_cycle(self, initialization_id: str) -> ActivatedCycle | None: ...

    def activate_initial_cycle(
        self,
        *,
        initialization_id: str,
        quest: AcceptedQuest,
        question: AcceptedQuestion,
    ) -> ActivatedCycle: ...

    def ensure_idea_stage_request(
        self,
        *,
        cycle_ref: str,
        accepted_question: AcceptedQuestionBinding,
        context_pack: dict[str, object],
        idempotency_key: str,
    ) -> StageRunRequest: ...

    def query_idea_stage_request(self, cycle_ref: str) -> StageRunRequest | None: ...

    def ensure_plan_stage_request(
        self,
        *,
        cycle_ref: str,
        accepted_question: AcceptedQuestionBinding,
        accepted_idea_set: AcceptedIdeaSetBinding,
        context_pack: dict[str, object],
        idempotency_key: str,
    ) -> StageRunRequest: ...

    def query_plan_stage_request(self, cycle_ref: str) -> StageRunRequest | None: ...

    def commit_stage_disposition(
        self,
        *,
        disposition: str,
        basis_kind: str,
        basis_ref: str,
        basis_receipt: AcceptanceReceipt,
        idempotency_key: str,
        request_ref: str | None = None,
        cycle_ref: str | None = None,
        stage: str | None = None,
        epoch: int | None = None,
        run_ref: str | None = None,
        run_completion_receipt: AcceptanceReceipt | None = None,
    ) -> StageCommit: ...

    def commit_idea_stage(
        self,
        *,
        request_ref: str,
        run_ref: str,
        outcome_ref: str,
        outcome_kind: str,
        run_completion_receipt: AcceptanceReceipt,
        outcome_receipt: AcceptanceReceipt,
        idempotency_key: str,
    ) -> StageCommit: ...

    def query_idea_stage_commit(self, request_ref: str) -> StageCommit | None: ...

    def commit_plan_stage(
        self,
        *,
        request_ref: str,
        run_ref: str,
        formal_plan_ref: str,
        run_completion_receipt: AcceptanceReceipt,
        formal_plan_receipt: AcceptanceReceipt,
        idempotency_key: str,
    ) -> StageCommit: ...

    def query_plan_stage_commit(self, request_ref: str) -> StageCommit | None: ...

    def verify_stage_run_request(
        self,
        *,
        request_ref: str,
        cycle_ref: str,
        epoch: int,
        context_pack_ref: str,
        context_pack_hash: str,
        receipt: AcceptanceReceipt,
    ) -> None: ...


_SNAPSHOT = OwnerSnapshotQuery(
    owner=AE_OWNER,
    statement=text(
        "SELECT revision, foreground_cycle_count, stage_request_count, "
        "stage_commit_count, human_request_count, control_operation_count, "
        "safe_point_count "
        "FROM advancement_engine_state WHERE singleton = 'owner'"
    ),
    fact_names=(
        "foreground_cycle_count",
        "stage_request_count",
        "stage_commit_count",
        "human_request_count",
        "control_operation_count",
        "safe_point_count",
    ),
)


class SQLiteAdvancementEngine(HumanRequestOwnerMixin):
    def __init__(
        self,
        database: Database,
        feed: DurableFeed,
        quest_verifier: QuestReceiptVerifier,
        question_verifier: RootQuestionReceiptVerifier,
        accepted_question_verifier: AcceptedQuestionBindingVerifier | None = None,
        evidence_verifier: EvidenceRefVerifier | None = None,
        run_completion_verifier: RunCompletionReceiptVerifier | None = None,
        outcome_verifier: IdeaOutcomeDecisionVerifier | None = None,
        formal_plan_verifier: FormalPlanDecisionVerifier | None = None,
        literature_snapshot_verifier: LiteratureSnapshotVerifier | None = None,
        human_response_verifier: HumanResponseVerifier | None = None,
        runtime_control_verifier: RuntimeControlReceiptVerifier | None = None,
        question_control_verifier: QuestionControlReceiptVerifier | None = None,
        stage_disposition_basis_verifier: StageDispositionBasisVerifier
        | None = None,
        current_question_verifier: CurrentQuestionVerifier | None = None,
    ) -> None:
        self._database = database
        self._feed = feed
        self._quest_verifier = quest_verifier
        self._question_verifier = question_verifier
        self._accepted_question_verifier = accepted_question_verifier
        self._evidence_verifier = evidence_verifier
        self._run_completion_verifier = run_completion_verifier
        self._outcome_verifier = outcome_verifier
        self._formal_plan_verifier = formal_plan_verifier
        self._literature_snapshot_verifier = literature_snapshot_verifier
        self._authorization_verifier = human_response_verifier
        self._runtime_control_verifier = runtime_control_verifier
        self._question_control_verifier = question_control_verifier
        self._stage_disposition_basis_verifier = stage_disposition_basis_verifier
        self._current_question_verifier = current_question_verifier
        self._configure_human_request_owner(
            database, feed, AE_OWNER, human_response_verifier
        )
        self._stage_request_verifier = SQLiteAdvancementEngineReceiptVerifier(database)
        self._snapshot = SQLiteOwnerSnapshot(database, _SNAPSHOT)

    def query_snapshot(self) -> OwnerSnapshot:
        return self._snapshot.query_snapshot()

    def query_foreground(self, quest_ref: str) -> dict[str, object] | None:
        _control_ref(quest_ref, "quest_ref")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT h.*, g.grant_ref, g.status AS grant_status, "
                    "g.safe_point_ref, s.revision AS owner_revision FROM "
                    "ae_foreground_heads h JOIN ae_foreground_grants g ON "
                    "g.quest_ref = h.quest_ref AND g.epoch = h.epoch JOIN "
                    "advancement_engine_state s ON s.singleton = 'owner' WHERE "
                    "h.quest_ref = :quest_ref"
                ),
                {"quest_ref": quest_ref},
            ).first()
        if row is None:
            return None
        if row.grant_status not in {
            "active",
            "suspended",
            "revoked",
            "completed",
            "cancelled",
            "abandoned",
            "pruned",
        }:
            raise OwnerConflict("foreground_grant_invalid")
        return {
            "quest_ref": row.quest_ref,
            "cycle_ref": row.cycle_ref,
            "question_ref": row.question_ref,
            "stage": row.stage,
            "epoch": int(row.epoch),
            "status": row.status,
            "grant_ref": row.grant_ref,
            "grant_status": row.grant_status,
            "safe_point_ref": row.safe_point_ref,
            "pending_operation_ref": row.pending_operation_ref,
            "owner_revision": int(row.owner_revision),
        }

    def query_active_foregrounds(
        self, *, stage: str | None = None
    ) -> tuple[dict[str, object], ...]:
        if stage is not None and stage not in STAGES:
            raise OwnerConflict("foreground_stage_invalid")
        with self._database.read() as connection:
            rows = connection.execute(
                text(
                    "SELECT h.*, g.grant_ref, g.status AS grant_status, "
                    "g.safe_point_ref, s.revision AS owner_revision FROM "
                    "ae_foreground_heads h JOIN ae_foreground_grants g ON "
                    "g.quest_ref = h.quest_ref AND g.epoch = h.epoch JOIN ae_cycles c "
                    "ON c.cycle_ref = h.cycle_ref JOIN advancement_engine_state s ON "
                    "s.singleton = 'owner' WHERE h.status = 'active' AND "
                    "g.status = 'active' AND c.status = 'ongoing' AND "
                    "(:stage IS NULL OR h.stage = :stage) ORDER BY h.updated_at, "
                    "h.quest_ref"
                ),
                {"stage": stage},
            ).all()
        return tuple(_foreground_query_document(row) for row in rows)

    def query_foreground_control_by_intent(
        self, intent_id: str
    ) -> dict[str, object] | None:
        _control_ref(intent_id, "intent_id")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ae_control_operations WHERE intent_id = "
                    ":intent_id"
                ),
                {"intent_id": intent_id},
            ).first()
        return None if row is None else self._control_operation_from_row(row)

    def query_recoverable_foreground_controls(
        self,
    ) -> tuple[dict[str, object], ...]:
        with self._database.read() as connection:
            rows = connection.execute(
                text(
                    "SELECT * FROM ae_control_operations WHERE status != 'aborted' "
                    "ORDER BY created_at, intent_id"
                )
            ).all()
        return tuple(self._control_operation_from_row(row) for row in rows)

    def preview_foreground_control(
        self, payload: dict[str, object]
    ) -> tuple[dict[str, object], int]:
        control = validate_control_payload(payload)
        action = cast(str, control["action"])
        target = cast(dict[str, object], control["target"])
        foreground = self.query_foreground(cast(str, target["quest_ref"]))
        if foreground is None:
            raise OwnerConflict("foreground_cycle_not_found")
        _assert_foreground_target(foreground, target)
        _assert_foreground_action(
            action,
            foreground,
            allow_pending_normal_override=(
                action == "forced_switch"
                and self._is_pending_normal_handoff(
                    cast(str | None, foreground.get("pending_operation_ref"))
                )
            ),
        )
        revision = cast(int, foreground["owner_revision"])
        target_question_ref = target.get("target_question_ref")
        assertion = {
            "owner": AE_OWNER,
            "operation": "control_foreground",
            "action": action,
            "quest_ref": foreground["quest_ref"],
            "source_cycle_ref": foreground["cycle_ref"],
            "source_epoch": foreground["epoch"],
            "source_status": foreground["status"],
            "source_stage": foreground["stage"],
            "target_question_ref": target_question_ref,
            "target_cycle_ref": (
                self._latest_recoverable_cycle_ref(
                    quest_ref=cast(str, foreground["quest_ref"]),
                    question_ref=cast(str, target_question_ref),
                )
                if action in SWITCH_ACTIONS
                else None
            ),
            "owner_revision": revision,
        }
        if action == "pause":
            will_happen = [
                "停止签发新的 Stage 工作",
                "在途 Run 到达 durable Safe Point 后进入 suspended",
            ]
        elif action == "resume":
            will_happen = [
                "从 durable Safe Point 恢复当前 Foreground Cycle",
                "保持可恢复 Run 的逻辑身份与 root Session",
            ]
        elif action in SWITCH_ACTIONS:
            will_happen = [
                "旧 Foreground Epoch 被撤销",
                "目标 Question 获得唯一的新 Foreground Grant/Epoch",
            ]
        elif action in {"cancel", "abandon"}:
            will_happen = [
                "当前 Foreground Epoch 被撤销",
                "相关 Run 先逻辑终止，再异步清理外部资源",
            ]
        else:
            will_happen = [
                "问题树生命周期意图交给 Research Graph",
                "若命中前台 Question，则 Foreground Epoch 先被保护性撤销",
            ]
        preview = signed_owner_preview(
            source_owner=AE_OWNER,
            target_assertion=assertion,
            will_happen=will_happen,
            will_not_happen=[
                "不会改写 Question、Research Asset 或 Owner acceptance",
                "技术失败不会被写成 Stage Completed/Skipped/Exhausted",
                "短生命周期 Harness 子 Agent 不会成为独立 Foreground Cycle",
            ],
            risks=[
                "强制路径可能留下待异步清理的外部进程",
                "任何 Owner revision 变化都会使本 Preview 陈旧",
            ],
            stale_conditions=[
                "Foreground Cycle、Epoch 或 Stage 改变",
                "Advancement Engine owner revision 改变",
            ],
        )
        return preview, revision

    def prepare_foreground_control(
        self,
        *,
        intent_id: str,
        payload: dict[str, object],
        expected_revision: int,
        idempotency_key: str,
        target_question: AcceptedQuestion | None = None,
    ) -> dict[str, object]:
        _control_ref(intent_id, "intent_id")
        _validate_idempotency_key(idempotency_key)
        control = validate_control_payload(payload)
        action = cast(str, control["action"])
        target = cast(dict[str, object], control["target"])
        target_question_ref = cast(str | None, target.get("target_question_ref"))
        if action in QUESTION_ACTIONS or action == "resume":
            verified_question_ref = (
                target_question_ref
                if action in QUESTION_ACTIONS
                else target.get("question_ref")
            )
            if (
                target_question is None
                or target_question.question_ref != verified_question_ref
            ):
                raise OwnerConflict("research_control_question_target_invalid")
            if target_question.quest_ref != target["quest_ref"]:
                raise OwnerConflict("research_control_question_target_invalid")
            if self._accepted_question_verifier is None:
                raise OwnerConflict("accepted_question_verifier_unavailable")
            self._accepted_question_verifier.verify_accepted_question_binding(
                target_question.as_binding()
            )
        elif target_question is not None:
            raise OwnerConflict("research_control_question_target_invalid")
        command_hash = canonical_hash(
            {
                "command": "prepare_foreground_control",
                "intent_id": intent_id,
                "payload": control,
                "expected_revision": expected_revision,
            }
        )
        now = time.time()
        with self._database.write() as connection:
            replay = connection.execute(
                text(
                    "SELECT * FROM ae_control_operations WHERE idempotency_key = "
                    ":idempotency_key OR intent_id = :intent_id"
                ),
                {"idempotency_key": idempotency_key, "intent_id": intent_id},
            ).first()
            if replay is not None:
                if replay.command_hash != command_hash:
                    raise OwnerConflict("idempotency_conflict")
                if replay.status == "aborted":
                    raise OwnerConflict("foreground_control_repreview_required")
                operation_ref = replay.operation_ref
            else:
                owner_revision = int(
                    connection.execute(
                        text(
                            "SELECT revision FROM advancement_engine_state WHERE "
                            "singleton = 'owner'"
                        )
                    ).scalar_one()
                )
                head = connection.execute(
                    text(
                        "SELECT * FROM ae_foreground_heads WHERE quest_ref = "
                        ":quest_ref"
                    ),
                    {"quest_ref": target["quest_ref"]},
                ).first()
                if owner_revision != expected_revision:
                    raise OwnerConflict("command_preview_stale")
                if head is None:
                    raise OwnerConflict("foreground_cycle_not_found")
                _assert_foreground_target(_foreground_row_dict(head), target)
                pending_normal = (
                    None
                    if head.pending_operation_ref is None
                    else connection.execute(
                        text(
                            "SELECT * FROM ae_control_operations WHERE "
                            "operation_ref = :operation_ref"
                        ),
                        {"operation_ref": head.pending_operation_ref},
                    ).first()
                )
                override_pending_normal = (
                    action == "forced_switch"
                    and pending_normal is not None
                    and pending_normal.action == "normal_switch"
                    and pending_normal.status == "handoff_pending"
                )
                _assert_foreground_action(
                    action,
                    _foreground_row_dict(head),
                    allow_pending_normal_override=override_pending_normal,
                )
                if override_pending_normal:
                    connection.execute(
                        text(
                            "UPDATE ae_control_operations SET status = 'aborted', "
                            "abort_reason_code = 'forced_switch_override', "
                            "updated_at = :now WHERE operation_ref = :operation_ref "
                            "AND status = 'handoff_pending'"
                        ),
                        {
                            "now": now,
                            "operation_ref": head.pending_operation_ref,
                        },
                    )
                    self._feed.record(
                        connection,
                        "advancement_engine.normal_handoff_overridden",
                        {
                            "operation_ref": head.pending_operation_ref,
                            "replacement_intent_id": intent_id,
                            "quest_ref": head.quest_ref,
                        },
                    )
                target_cycle_ref = None
                if action in SWITCH_ACTIONS:
                    assert target_question is not None
                    if target_question.question_ref == head.question_ref:
                        raise OwnerConflict("foreground_switch_target_current")
                    cycle = connection.execute(
                        text(
                            "SELECT * FROM ae_cycles WHERE question_ref = "
                            ":question_ref AND quest_ref = :quest_ref AND status = "
                            "'ongoing' ORDER BY created_at DESC LIMIT 1"
                        ),
                        {
                            "question_ref": target_question.question_ref,
                            "quest_ref": target_question.quest_ref,
                        },
                    ).first()
                    if cycle is None:
                        target_cycle_ref = new_ref("cycle")
                    elif (
                        cycle.quest_ref != target_question.quest_ref
                        or cycle.question_receipt_ref
                        != target_question.receipt.receipt_ref
                        or cycle.question_receipt_hash
                        != target_question.receipt.payload_hash
                    ):
                        raise OwnerConflict("research_cycle_target_conflict")
                    else:
                        target_cycle_ref = cycle.cycle_ref
                operation_ref = (
                    "ae_control_"
                    + canonical_hash({"intent_id": intent_id})[:48]
                )
                connection.execute(
                    text(
                        "INSERT INTO ae_control_operations (operation_ref, intent_id, "
                        "idempotency_key, action, quest_ref, source_cycle_ref, "
                        "source_epoch, source_stage, target_question_ref, "
                        "target_cycle_ref, "
                        "target_question_receipt_ref, target_question_receipt_hash, "
                        "command_json, command_hash, expected_revision, status, "
                        "created_at, updated_at) VALUES (:operation_ref, :intent_id, "
                        ":idempotency_key, :action, :quest_ref, :source_cycle_ref, "
                        ":source_epoch, :source_stage, :target_question_ref, "
                        ":target_cycle_ref, "
                        ":target_question_receipt_ref, :target_question_receipt_hash, "
                        ":command_json, :command_hash, :expected_revision, 'prepared', "
                        ":now, :now)"
                    ),
                    {
                        "operation_ref": operation_ref,
                        "intent_id": intent_id,
                        "idempotency_key": idempotency_key,
                        "action": action,
                        "quest_ref": head.quest_ref,
                        "source_cycle_ref": head.cycle_ref,
                        "source_epoch": int(head.epoch),
                        "source_stage": head.stage,
                        "target_question_ref": target_question_ref,
                        "target_cycle_ref": target_cycle_ref,
                        "target_question_receipt_ref": (
                            None
                            if target_question is None
                            else target_question.receipt.receipt_ref
                        ),
                        "target_question_receipt_hash": (
                            None
                            if target_question is None
                            else target_question.receipt.payload_hash
                        ),
                        "command_json": canonical_json(control),
                        "command_hash": command_hash,
                        "expected_revision": expected_revision,
                        "now": now,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE ae_foreground_heads SET pending_operation_ref = "
                        ":operation_ref, updated_at = :now "
                        "WHERE quest_ref = :quest_ref"
                    ),
                    {
                        "operation_ref": operation_ref,
                        "now": now,
                        "quest_ref": head.quest_ref,
                    },
                )
                self._feed.record(
                    connection,
                    "advancement_engine.foreground_control_prepared",
                    {
                        "operation_ref": operation_ref,
                        "intent_id": intent_id,
                        "action": action,
                        "quest_ref": head.quest_ref,
                        "cycle_ref": head.cycle_ref,
                        "epoch": int(head.epoch),
                    },
                )
        return self._query_control_operation(operation_ref)

    def _is_pending_normal_handoff(self, operation_ref: str | None) -> bool:
        if operation_ref is None:
            return False
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT action, status FROM ae_control_operations WHERE "
                    "operation_ref = :operation_ref"
                ),
                {"operation_ref": operation_ref},
            ).first()
        return bool(
            row is not None
            and row.action == "normal_switch"
            and row.status == "handoff_pending"
        )

    def abort_foreground_control(
        self, *, operation_ref: str, reason_code: str
    ) -> None:
        _control_ref(operation_ref, "operation_ref")
        if not isinstance(reason_code, str) or not reason_code or len(reason_code) > 96:
            raise OwnerConflict("foreground_control_abort_reason_invalid")
        now = time.time()
        with self._database.write() as connection:
            operation = connection.execute(
                text(
                    "SELECT * FROM ae_control_operations WHERE operation_ref = "
                    ":operation_ref"
                ),
                {"operation_ref": operation_ref},
            ).first()
            if operation is None:
                return
            if operation.status == "completed":
                raise OwnerConflict("foreground_control_already_completed")
            if operation.status == "aborted":
                return
            connection.execute(
                text(
                    "UPDATE ae_control_operations SET status = 'aborted', "
                    "abort_reason_code = :reason_code, updated_at = :now WHERE "
                    "operation_ref = :operation_ref"
                ),
                {
                    "now": now,
                    "operation_ref": operation_ref,
                    "reason_code": reason_code,
                },
            )
            connection.execute(
                text(
                    "UPDATE ae_foreground_heads SET pending_operation_ref = NULL, "
                    "updated_at = :now WHERE quest_ref = :quest_ref AND "
                    "pending_operation_ref = :operation_ref"
                ),
                {
                    "now": now,
                    "quest_ref": operation.quest_ref,
                    "operation_ref": operation_ref,
                },
            )
            self._feed.record(
                connection,
                "advancement_engine.foreground_control_aborted",
                {
                    "operation_ref": operation_ref,
                    "reason_code": reason_code,
                    "quest_ref": operation.quest_ref,
                },
            )

    def complete_foreground_control(
        self,
        *,
        operation_ref: str,
        runtime_receipt: dict[str, object],
        graph_receipt: dict[str, object] | None,
        idempotency_key: str,
    ) -> dict[str, object]:
        _control_ref(operation_ref, "operation_ref")
        _validate_idempotency_key(idempotency_key)
        now = time.time()
        runtime_hash = canonical_hash(runtime_receipt)
        graph_hash = None if graph_receipt is None else canonical_hash(graph_receipt)
        with self._database.write() as connection:
            operation = connection.execute(
                text(
                    "SELECT * FROM ae_control_operations WHERE operation_ref = "
                    ":operation_ref"
                ),
                {"operation_ref": operation_ref},
            ).first()
            if operation is None:
                raise OwnerConflict("foreground_control_operation_not_found")
            if operation.status == "aborted":
                raise OwnerConflict("foreground_control_repreview_required")
            try:
                control = decoded_object(operation.command_json)
            except (TypeError, ValueError) as error:
                raise OwnerConflict("foreground_control_operation_invalid") from error
            target = cast(dict[str, object], control.get("target"))
            action = str(operation.action)
            if self._runtime_control_verifier is None:
                raise OwnerConflict("runtime_control_verifier_unavailable")
            self._runtime_control_verifier.verify_runtime_control_receipt(
                operation_ref=operation_ref,
                action=action,
                target=target,
                receipt=runtime_receipt,
            )
            if action in {"prune", "restore"}:
                if graph_receipt is None or self._question_control_verifier is None:
                    raise OwnerConflict("question_control_receipt_invalid")
                self._question_control_verifier.verify_question_control_receipt(
                    operation_ref=operation_ref,
                    action=action,
                    target=target,
                    receipt=graph_receipt,
                )
                affected_refs = graph_receipt.get("affected_question_refs")
                if not isinstance(affected_refs, list) or not all(
                    isinstance(item, str) and item for item in affected_refs
                ):
                    raise OwnerConflict("question_control_receipt_invalid")
            elif graph_receipt is not None:
                raise OwnerConflict("question_control_receipt_unexpected")
            else:
                affected_refs = []
            if operation.status in {"completed", "handoff_pending"}:
                if (
                    operation.runtime_receipt_hash != runtime_hash
                    or operation.graph_receipt_hash != graph_hash
                ):
                    raise OwnerConflict("idempotency_conflict")
                if operation.status == "completed":
                    return self._control_operation_from_row(operation)
            head = connection.execute(
                text(
                    "SELECT * FROM ae_foreground_heads WHERE quest_ref = :quest_ref"
                ),
                {"quest_ref": operation.quest_ref},
            ).first()
            if head is None or head.pending_operation_ref != operation_ref:
                raise OwnerConflict("foreground_control_operation_stale")
            if action == "normal_switch":
                if runtime_receipt.get("source_stage") != operation.source_stage:
                    raise OwnerConflict("runtime_control_receipt_invalid")
                if operation.source_stage == "bundle":
                    affected_runs = runtime_receipt.get("affected_runs")
                    if not isinstance(affected_runs, list) or any(
                        not isinstance(item, dict)
                        or item.get("status")
                        not in {"suspended", "completed", "terminated"}
                        or not isinstance(item.get("safe_point_ref"), str)
                        for item in affected_runs
                    ):
                        raise OwnerConflict("runtime_quiescence_receipt_invalid")
            safe_points = runtime_receipt.get("safe_points")
            safe_point_ref = (
                safe_points[0].get("safe_point_ref")
                if isinstance(safe_points, list)
                and safe_points
                and isinstance(safe_points[0], dict)
                else None
            )
            if action == "normal_switch" and self._normal_handoff_requires_commit(
                connection, operation, head
            ):
                connection.execute(
                    text(
                        "UPDATE ae_control_operations SET status = "
                        "'handoff_pending', runtime_receipt_json = :runtime_json, "
                        "runtime_receipt_hash = :runtime_hash, graph_receipt_json = "
                        "NULL, graph_receipt_hash = NULL, safe_point_ref = "
                        ":safe_point_ref, updated_at = :now WHERE operation_ref = "
                        ":operation_ref"
                    ),
                    {
                        "runtime_json": canonical_json(runtime_receipt),
                        "runtime_hash": runtime_hash,
                        "safe_point_ref": safe_point_ref,
                        "now": now,
                        "operation_ref": operation_ref,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE advancement_engine_state SET revision = revision + 1 "
                        "WHERE singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "advancement_engine.normal_handoff_pending",
                    {
                        "operation_ref": operation_ref,
                        "quest_ref": operation.quest_ref,
                        "source_cycle_ref": operation.source_cycle_ref,
                        "source_epoch": int(operation.source_epoch),
                        "target_cycle_ref": operation.target_cycle_ref,
                    },
                )
                pending = connection.execute(
                    text(
                        "SELECT * FROM ae_control_operations WHERE operation_ref = "
                        ":operation_ref"
                    ),
                    {"operation_ref": operation_ref},
                ).one()
                return self._control_operation_from_row(pending)
            if action in SWITCH_ACTIONS:
                if self._current_question_verifier is None:
                    raise OwnerConflict("current_question_verifier_unavailable")
                try:
                    self._current_question_verifier.verify_current_question(
                        quest_ref=str(operation.quest_ref),
                        question_ref=str(operation.target_question_ref),
                        question_receipt_ref=str(
                            operation.target_question_receipt_ref
                        ),
                        question_receipt_hash=str(
                            operation.target_question_receipt_hash
                        ),
                    )
                except OwnerConflict:
                    connection.execute(
                        text(
                            "UPDATE ae_control_operations SET status = 'aborted', "
                            "abort_reason_code = 'switch_target_invalidated', "
                            "runtime_receipt_json = :runtime_json, "
                            "runtime_receipt_hash = :runtime_hash, updated_at = "
                            ":now WHERE operation_ref = :operation_ref"
                        ),
                        {
                            "runtime_json": canonical_json(runtime_receipt),
                            "runtime_hash": runtime_hash,
                            "now": now,
                            "operation_ref": operation_ref,
                        },
                    )
                    connection.execute(
                        text(
                            "UPDATE ae_foreground_heads SET pending_operation_ref = "
                            "NULL, updated_at = :now WHERE quest_ref = :quest_ref "
                            "AND pending_operation_ref = :operation_ref"
                        ),
                        {
                            "now": now,
                            "quest_ref": operation.quest_ref,
                            "operation_ref": operation_ref,
                        },
                    )
                    connection.execute(
                        text(
                            "UPDATE advancement_engine_state SET revision = "
                            "revision + 1 WHERE singleton = 'owner'"
                        )
                    )
                    self._feed.record(
                        connection,
                        "advancement_engine.foreground_switch_target_invalidated",
                        {
                            "operation_ref": operation_ref,
                            "quest_ref": operation.quest_ref,
                            "source_cycle_ref": operation.source_cycle_ref,
                            "target_question_ref": operation.target_question_ref,
                        },
                    )
                    aborted = connection.execute(
                        text(
                            "SELECT * FROM ae_control_operations WHERE "
                            "operation_ref = :operation_ref"
                        ),
                        {"operation_ref": operation_ref},
                    ).one()
                    return self._control_operation_from_row(aborted)
            if action == "pause":
                next_status = "suspended"
                connection.execute(
                    text(
                        "UPDATE ae_cycles SET suspension_reason = 'human_paused', "
                        "updated_at = :now WHERE cycle_ref = :cycle_ref AND status = "
                        "'ongoing'"
                    ),
                    {"now": now, "cycle_ref": operation.source_cycle_ref},
                )
                connection.execute(
                    text(
                        "UPDATE ae_foreground_grants SET status = 'suspended', "
                        "safe_point_ref = :safe_point_ref WHERE quest_ref = "
                        ":quest_ref AND epoch = :epoch"
                    ),
                    {
                        "safe_point_ref": safe_point_ref,
                        "quest_ref": operation.quest_ref,
                        "epoch": int(operation.source_epoch),
                    },
                )
            elif action == "resume":
                next_status = "active"
                current_grant = connection.execute(
                    text(
                        "SELECT * FROM ae_foreground_grants WHERE quest_ref = "
                        ":quest_ref AND epoch = :epoch"
                    ),
                    {
                        "quest_ref": operation.quest_ref,
                        "epoch": int(operation.source_epoch),
                    },
                ).one()
                cycle_reason = connection.execute(
                    text(
                        "SELECT suspension_reason FROM ae_cycles WHERE cycle_ref = "
                        ":cycle_ref AND status = 'ongoing'"
                    ),
                    {"cycle_ref": operation.source_cycle_ref},
                ).scalar_one()
                connection.execute(
                    text(
                        "UPDATE ae_cycles SET suspension_reason = NULL, updated_at = "
                        ":now WHERE cycle_ref = :cycle_ref AND status = 'ongoing'"
                    ),
                    {"now": now, "cycle_ref": operation.source_cycle_ref},
                )
                if (
                    current_grant.status == "revoked"
                    and cycle_reason == "human_cancelled"
                ):
                    next_epoch = int(operation.source_epoch) + 1
                    connection.execute(
                        text(
                            "INSERT INTO ae_foreground_grants (grant_ref, quest_ref, "
                            "cycle_ref, question_ref, stage, epoch, status, "
                            "predecessor_grant_ref, safe_point_ref, granted_at, "
                            "revoked_at) VALUES (:grant_ref, :quest_ref, :cycle_ref, "
                            ":question_ref, :stage, :epoch, 'active', :predecessor, "
                            "NULL, :now, NULL)"
                        ),
                        {
                            "grant_ref": new_ref("foreground_grant"),
                            "quest_ref": operation.quest_ref,
                            "cycle_ref": operation.source_cycle_ref,
                            "question_ref": head.question_ref,
                            "stage": head.stage,
                            "epoch": next_epoch,
                            "predecessor": current_grant.grant_ref,
                            "now": now,
                        },
                    )
                    connection.execute(
                        text(
                            "UPDATE ae_foreground_heads SET epoch = :epoch WHERE "
                            "quest_ref = :quest_ref"
                        ),
                        {"epoch": next_epoch, "quest_ref": operation.quest_ref},
                    )
                else:
                    connection.execute(
                        text(
                            "UPDATE ae_foreground_grants SET status = 'active' WHERE "
                            "quest_ref = :quest_ref AND epoch = :epoch AND status = "
                            "'suspended'"
                        ),
                        {
                            "quest_ref": operation.quest_ref,
                            "epoch": int(operation.source_epoch),
                        },
                    )
            elif action in SWITCH_ACTIONS:
                connection.execute(
                    text(
                        "UPDATE ae_foreground_grants SET status = CASE WHEN status = "
                        "'completed' THEN 'completed' ELSE 'revoked' END, "
                        "safe_point_ref = :safe_point_ref, revoked_at = COALESCE"
                        "(revoked_at, :now) WHERE quest_ref = :quest_ref AND "
                        "epoch = :epoch"
                    ),
                    {
                        "safe_point_ref": safe_point_ref,
                        "now": now,
                        "quest_ref": operation.quest_ref,
                        "epoch": int(operation.source_epoch),
                    },
                )
                prior_grant = connection.execute(
                    text(
                        "SELECT grant_ref FROM ae_foreground_grants WHERE "
                        "quest_ref = :quest_ref AND epoch = :epoch"
                    ),
                    {
                        "quest_ref": operation.quest_ref,
                        "epoch": int(operation.source_epoch),
                    },
                ).scalar_one()
                next_epoch = int(operation.source_epoch) + 1
                grant_ref = new_ref("foreground_grant")
                target_cycle = connection.execute(
                    text(
                        "SELECT * FROM ae_cycles WHERE cycle_ref = :cycle_ref AND "
                        "status = 'ongoing'"
                    ),
                    {"cycle_ref": operation.target_cycle_ref},
                ).first()
                if target_cycle is None:
                    predecessor = connection.execute(
                        text(
                            "SELECT cycle_ref FROM ae_cycles WHERE quest_ref = "
                            ":quest_ref AND question_ref = :question_ref ORDER BY "
                            "created_at DESC LIMIT 1"
                        ),
                        {
                            "quest_ref": operation.quest_ref,
                            "question_ref": operation.target_question_ref,
                        },
                    ).scalar_one_or_none()
                    connection.execute(
                        text(
                            "INSERT INTO ae_cycles (cycle_ref, quest_ref, "
                            "question_ref, question_receipt_ref, "
                            "question_receipt_hash, stage, status, "
                            "predecessor_cycle_ref, created_at, updated_at) VALUES "
                            "(:cycle_ref, :quest_ref, :question_ref, :receipt_ref, "
                            ":receipt_hash, 'idea', 'ongoing', :predecessor, :now, "
                            ":now)"
                        ),
                        {
                            "cycle_ref": operation.target_cycle_ref,
                            "quest_ref": operation.quest_ref,
                            "question_ref": operation.target_question_ref,
                            "receipt_ref": operation.target_question_receipt_ref,
                            "receipt_hash": operation.target_question_receipt_hash,
                            "predecessor": predecessor,
                            "now": now,
                        },
                    )
                    if predecessor is not None:
                        connection.execute(
                            text(
                                "UPDATE ae_cycles SET successor_cycle_ref = "
                                ":successor, updated_at = :now WHERE cycle_ref = "
                                ":predecessor AND successor_cycle_ref IS NULL"
                            ),
                            {
                                "successor": operation.target_cycle_ref,
                                "predecessor": predecessor,
                                "now": now,
                            },
                        )
                    target_cycle = connection.execute(
                        text(
                            "SELECT * FROM ae_cycles WHERE cycle_ref = :cycle_ref"
                        ),
                        {"cycle_ref": operation.target_cycle_ref},
                    ).one()
                target_stage = str(target_cycle.stage)
                connection.execute(
                    text(
                        "UPDATE ae_cycles SET suspension_reason = "
                        "'foreground_switched', updated_at = :now WHERE cycle_ref = "
                        ":source_cycle_ref AND status = 'ongoing'"
                    ),
                    {
                        "now": now,
                        "source_cycle_ref": operation.source_cycle_ref,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE ae_cycles SET suspension_reason = NULL, updated_at = "
                        ":now WHERE cycle_ref = :target_cycle_ref AND status = "
                        "'ongoing'"
                    ),
                    {"now": now, "target_cycle_ref": operation.target_cycle_ref},
                )
                connection.execute(
                    text(
                        "INSERT INTO ae_foreground_grants (grant_ref, quest_ref, "
                        "cycle_ref, question_ref, stage, epoch, status, "
                        "predecessor_grant_ref, safe_point_ref, granted_at, "
                        "revoked_at) VALUES (:grant_ref, :quest_ref, :cycle_ref, "
                        ":question_ref, :stage, :epoch, 'active', :predecessor, "
                        "NULL, :now, NULL)"
                    ),
                    {
                        "grant_ref": grant_ref,
                        "quest_ref": operation.quest_ref,
                        "cycle_ref": operation.target_cycle_ref,
                        "question_ref": operation.target_question_ref,
                        "stage": target_stage,
                        "epoch": next_epoch,
                        "predecessor": prior_grant,
                        "now": now,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE ae_foreground_heads SET cycle_ref = :cycle_ref, "
                        "question_ref = :question_ref, stage = :stage, epoch = "
                        ":epoch WHERE quest_ref = :quest_ref"
                    ),
                    {
                        "cycle_ref": operation.target_cycle_ref,
                        "question_ref": operation.target_question_ref,
                        "stage": target_stage,
                        "epoch": next_epoch,
                        "quest_ref": operation.quest_ref,
                    },
                )
                next_status = "active"
            elif action == "cancel":
                # Cancel terminates current technical Runs but leaves the Research
                # Cycle recoverable.  Resume signs a new Epoch/Grant and therefore
                # never reopens any terminal Run identity.
                next_status = "suspended"
                connection.execute(
                    text(
                        "UPDATE ae_foreground_grants SET status = 'revoked', "
                        "safe_point_ref = :safe_point_ref, revoked_at = COALESCE"
                        "(revoked_at, :now) WHERE quest_ref = :quest_ref AND epoch = "
                        ":epoch"
                    ),
                    {
                        "safe_point_ref": safe_point_ref,
                        "now": now,
                        "quest_ref": operation.quest_ref,
                        "epoch": int(operation.source_epoch),
                    },
                )
                connection.execute(
                    text(
                        "UPDATE ae_cycles SET suspension_reason = 'human_cancelled', "
                        "updated_at = :now WHERE cycle_ref = :cycle_ref AND status = "
                        "'ongoing'"
                    ),
                    {"now": now, "cycle_ref": operation.source_cycle_ref},
                )
            elif action == "abandon":
                next_status = "abandoned"
                connection.execute(
                    text(
                        "UPDATE ae_foreground_grants SET status = 'abandoned', "
                        "safe_point_ref = :safe_point_ref, revoked_at = COALESCE"
                        "(revoked_at, :now) WHERE quest_ref = :quest_ref AND epoch = "
                        ":epoch"
                    ),
                    {
                        "safe_point_ref": safe_point_ref,
                        "now": now,
                        "quest_ref": operation.quest_ref,
                        "epoch": int(operation.source_epoch),
                    },
                )
                connection.execute(
                    text(
                        "UPDATE ae_cycles SET status = 'abandoned', updated_at = "
                        ":now WHERE cycle_ref = :cycle_ref"
                    ),
                    {"now": now, "cycle_ref": operation.source_cycle_ref},
                )
            elif action == "prune":
                foreground_affected = head.question_ref in affected_refs
                next_status = (
                    "suspended"
                    if foreground_affected
                    else head.status
                )
                if foreground_affected:
                    connection.execute(
                        text(
                            "UPDATE ae_foreground_grants SET status = 'suspended', "
                            "safe_point_ref = :safe_point_ref WHERE quest_ref = "
                            ":quest_ref AND epoch = :epoch"
                        ),
                        {
                            "safe_point_ref": safe_point_ref,
                            "now": now,
                            "quest_ref": operation.quest_ref,
                            "epoch": int(operation.source_epoch),
                        },
                    )
                    connection.execute(
                        text(
                            "UPDATE ae_cycles SET suspension_reason = "
                            "'question_pruned', updated_at = :now WHERE cycle_ref = "
                            ":cycle_ref AND status = 'ongoing'"
                        ),
                        {"now": now, "cycle_ref": head.cycle_ref},
                    )
            else:  # restore
                foreground_affected = head.question_ref in affected_refs
                next_status = (
                    "suspended"
                    if foreground_affected
                    and head.status == "suspended"
                    else head.status
                )
                if foreground_affected:
                    connection.execute(
                        text(
                            "UPDATE ae_cycles SET suspension_reason = "
                            "'question_restored_revalidation_required', updated_at = "
                            ":now WHERE cycle_ref = :cycle_ref AND status = 'ongoing'"
                        ),
                        {"now": now, "cycle_ref": head.cycle_ref},
                    )
                    connection.execute(
                        text(
                            "UPDATE ae_foreground_grants SET status = 'suspended' "
                            "WHERE quest_ref = :quest_ref AND epoch = :epoch"
                        ),
                        {"quest_ref": head.quest_ref, "epoch": int(head.epoch)},
                    )
            connection.execute(
                text(
                    "UPDATE ae_foreground_heads SET status = :status, "
                    "pending_operation_ref = NULL, updated_at = :now WHERE "
                    "quest_ref = :quest_ref"
                ),
                {"status": next_status, "now": now, "quest_ref": operation.quest_ref},
            )
            result = {
                "operation_ref": operation_ref,
                "action": action,
                "quest_ref": operation.quest_ref,
                "source_cycle_ref": operation.source_cycle_ref,
                "source_epoch": int(operation.source_epoch),
                "status": "completed",
                "safe_point_ref": safe_point_ref,
                "target_cycle_ref": operation.target_cycle_ref,
                "target_question_ref": operation.target_question_ref,
            }
            result_hash = canonical_hash(result)
            receipt_ref = new_ref("ae_control_receipt")
            receipt_hash = canonical_hash(
                {
                    "issuer": AE_OWNER,
                    "kind": "foreground_control",
                    "subject_ref": operation_ref,
                    "result_hash": result_hash,
                    "runtime_receipt_hash": runtime_hash,
                    "graph_receipt_hash": graph_hash,
                }
            )
            connection.execute(
                text(
                    "UPDATE ae_control_operations SET status = 'completed', "
                    "runtime_receipt_json = :runtime_json, runtime_receipt_hash = "
                    ":runtime_hash, graph_receipt_json = :graph_json, "
                    "graph_receipt_hash = :graph_hash, safe_point_ref = "
                    ":safe_point_ref, result_json = :result_json, result_hash = "
                    ":result_hash, receipt_ref = :receipt_ref, receipt_hash = "
                    ":receipt_hash, updated_at = :now WHERE operation_ref = "
                    ":operation_ref"
                ),
                {
                    "runtime_json": canonical_json(runtime_receipt),
                    "runtime_hash": runtime_hash,
                    "graph_json": (
                        None if graph_receipt is None else canonical_json(graph_receipt)
                    ),
                    "graph_hash": graph_hash,
                    "safe_point_ref": safe_point_ref,
                    "result_json": canonical_json(result),
                    "result_hash": result_hash,
                    "receipt_ref": receipt_ref,
                    "receipt_hash": receipt_hash,
                    "now": now,
                    "operation_ref": operation_ref,
                },
            )
            safe_point_count = len(safe_points) if isinstance(safe_points, list) else 0
            connection.execute(
                text(
                    "UPDATE advancement_engine_state SET revision = revision + 1, "
                    "control_operation_count = control_operation_count + 1, "
                    "safe_point_count = safe_point_count + :safe_point_count WHERE "
                    "singleton = 'owner'"
                ),
                {"safe_point_count": safe_point_count},
            )
            self._feed.record(
                connection,
                "advancement_engine.foreground_control_completed",
                result,
            )
        return self._query_control_operation(operation_ref)

    def _normal_handoff_requires_commit(self, connection, operation, head) -> bool:
        if (
            head.cycle_ref != operation.source_cycle_ref
            or int(head.epoch) != int(operation.source_epoch)
            or head.status not in {"active", "completed"}
        ):
            raise OwnerConflict("foreground_control_operation_stale")
        source_stage = str(operation.source_stage)
        if source_stage == "bundle":
            # Bundle handoff freezes the Target Run set in AR and waits for those
            # exact units to acknowledge Safe Points.  It does not fabricate or
            # require a whole-Bundle StageCommit merely to switch foreground.
            return False
        request = connection.execute(
            text(
                "SELECT request_ref FROM ae_stage_run_requests WHERE cycle_ref = "
                ":cycle_ref AND stage = :stage AND epoch = :epoch"
            ),
            {
                "cycle_ref": operation.source_cycle_ref,
                "stage": source_stage,
                "epoch": int(operation.source_epoch),
            },
        ).first()
        committed = connection.execute(
            text(
                "SELECT commit_ref FROM ae_stage_commits WHERE cycle_ref = "
                ":cycle_ref AND stage = :stage AND epoch = :epoch"
            ),
            {
                "cycle_ref": operation.source_cycle_ref,
                "stage": source_stage,
                "epoch": int(operation.source_epoch),
            },
        ).first()
        if head.status == "completed":
            # A terminal Reasoning StageCommit atomically completes the source
            # Cycle/Grant before the pending handoff callback runs.  That is the
            # required normal boundary, not a stale control operation.
            if source_stage != "reasoning" or committed is None:
                raise OwnerConflict("foreground_control_operation_stale")
            return False
        if request is None:
            return False
        return committed is None

    def _query_control_operation(self, operation_ref: str) -> dict[str, object]:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ae_control_operations WHERE operation_ref = "
                    ":operation_ref"
                ),
                {"operation_ref": operation_ref},
            ).first()
        if row is None:
            raise OwnerConflict("foreground_control_operation_not_found")
        return self._control_operation_from_row(row)

    def _latest_recoverable_cycle_ref(
        self, *, quest_ref: str, question_ref: str
    ) -> str | None:
        """Return the current resumable Cycle without inventing an identity.

        A Question may own a serial chain of Cycles.  Preview therefore reports
        the latest ongoing Cycle when one exists; prepare creates a successor
        only when resume validation cannot select one.
        """

        with self._database.read() as connection:
            return cast(
                str | None,
                connection.execute(
                    text(
                        "SELECT cycle_ref FROM ae_cycles WHERE quest_ref = "
                        ":quest_ref AND question_ref = :question_ref AND status = "
                        "'ongoing' ORDER BY created_at DESC LIMIT 1"
                    ),
                    {"quest_ref": quest_ref, "question_ref": question_ref},
                ).scalar_one_or_none(),
            )

    def _control_operation_from_row(self, row) -> dict[str, object]:
        result = {
            "operation_ref": row.operation_ref,
            "intent_id": row.intent_id,
            "action": row.action,
            "quest_ref": row.quest_ref,
            "source_cycle_ref": row.source_cycle_ref,
            "source_epoch": int(row.source_epoch),
            "source_stage": row.source_stage,
            "target_question_ref": row.target_question_ref,
            "target_cycle_ref": row.target_cycle_ref,
            "status": row.status,
        }
        if row.status == "completed":
            document = decoded_object(row.result_json)
            if canonical_hash(document) != row.result_hash:
                raise OwnerConflict("foreground_control_receipt_invalid")
            result.update(document)
            result["receipt"] = AcceptanceReceipt(
                issuer=AE_OWNER,
                kind="foreground_control",
                receipt_ref=row.receipt_ref,
                subject_ref=row.operation_ref,
                payload_hash=row.receipt_hash,
            ).as_public_dict()
        elif row.status == "aborted":
            if not isinstance(row.abort_reason_code, str) or not row.abort_reason_code:
                raise OwnerConflict("foreground_control_operation_invalid")
            result["abort_reason_code"] = row.abort_reason_code
        return result

    def preview_initial_cycle_activation(
        self,
        *,
        initialization_id: str,
        proposal_ref: str,
        proposal_hash: str,
    ) -> dict[str, object]:
        assertion = {
            "owner": AE_OWNER,
            "operation": "activate_initial_cycle",
            "may_change": ["research_cycle", "foreground_cycle"],
            "will_not_change": ["quest_goal", "question_content", "question_identity"],
            "preconditions": ["exact_quest_receipt", "exact_root_question_receipt"],
            "risks": ["cycle_remains_not_attempted_if_question_receipt_is_stale"],
            "stale_if": ["quest_receipt_changes", "root_question_receipt_changes"],
            "bindings": {
                "initialization_id": initialization_id,
                "proposal_ref": proposal_ref,
                "proposal_hash": proposal_hash,
            },
        }
        return {**assertion, "target_hash": canonical_hash(assertion)}

    def query_initial_cycle(self, initialization_id: str) -> ActivatedCycle | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ae_initial_cycles WHERE initialization_id = "
                    ":initialization_id"
                ),
                {"initialization_id": initialization_id},
            ).first()
        if row is None:
            return None
        if row.receipt_hash != _cycle_receipt_hash(row):
            raise OwnerConflict("cycle_receipt_invalid")
        self._question_verifier.verify_root_question_receipt(
            initialization_id=initialization_id,
            quest_ref=row.quest_ref,
            question_ref=row.question_ref,
            receipt=AcceptanceReceipt(
                issuer="research_graph",
                kind="root_question_acceptance",
                receipt_ref=row.question_receipt_ref,
                subject_ref=row.question_ref,
                payload_hash=row.question_receipt_hash,
            ),
        )
        return _activated_cycle(row)

    def activate_initial_cycle(
        self,
        *,
        initialization_id: str,
        quest: AcceptedQuest,
        question: AcceptedQuestion,
    ) -> ActivatedCycle:
        self._quest_verifier.verify_quest_receipt(
            initialization_id=initialization_id,
            quest_ref=quest.quest_ref,
            proposal_ref=quest.proposal_ref,
            proposal_hash=quest.proposal_hash,
            confirmation_ref=quest.confirmation.receipt_ref,
            receipt=quest.receipt,
        )
        self._question_verifier.verify_root_question_receipt(
            initialization_id=initialization_id,
            quest_ref=quest.quest_ref,
            question_ref=question.question_ref,
            receipt=question.receipt,
        )
        if (
            question.initialization_id != initialization_id
            or question.quest_ref != quest.quest_ref
            or question.confirmation_ref != quest.confirmation.receipt_ref
        ):
            raise OwnerConflict("initial_cycle_lineage_invalid")
        bindings = {
            "initialization_id": initialization_id,
            "quest_ref": quest.quest_ref,
            "question_ref": question.question_ref,
            "question_receipt_ref": question.receipt.receipt_ref,
            "question_receipt_hash": question.receipt.payload_hash,
            "quest_receipt_ref": quest.receipt.receipt_ref,
            "quest_receipt_hash": quest.receipt.payload_hash,
        }
        with self._database.write() as connection:
            existing = connection.execute(
                text(
                    "SELECT * FROM ae_initial_cycles WHERE initialization_id = "
                    ":initialization_id"
                ),
                {"initialization_id": initialization_id},
            ).first()
            if existing is not None:
                if any(getattr(existing, key) != value for key, value in bindings.items()) or (
                    existing.receipt_hash != _cycle_receipt_hash(existing)
                ):
                    raise OwnerConflict("cycle_activation_conflict")
                return _activated_cycle(existing)

            cycle_ref = new_ref("cycle")
            receipt_ref = new_ref("ae_cycle_receipt")
            receipt_hash = _receipt_hash(CYCLE_RECEIPT_KIND, cycle_ref, bindings)
            activated_at = time.time()
            connection.execute(
                text(
                    "INSERT INTO ae_initial_cycles (cycle_ref, initialization_id, "
                    "quest_ref, question_ref, question_receipt_ref, "
                    "question_receipt_hash, quest_receipt_ref, quest_receipt_hash, "
                    "receipt_ref, receipt_hash, activated_at) VALUES (:cycle_ref, "
                    ":initialization_id, :quest_ref, :question_ref, "
                    ":question_receipt_ref, :question_receipt_hash, "
                    ":quest_receipt_ref, :quest_receipt_hash, :receipt_ref, "
                    ":receipt_hash, :activated_at)"
                ),
                {
                    **bindings,
                    "cycle_ref": cycle_ref,
                    "receipt_ref": receipt_ref,
                    "receipt_hash": receipt_hash,
                    "activated_at": activated_at,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO ae_cycles (cycle_ref, quest_ref, question_ref, "
                    "question_receipt_ref, question_receipt_hash, stage, status, "
                    "created_at, updated_at) VALUES (:cycle_ref, :quest_ref, "
                    ":question_ref, :question_receipt_ref, :question_receipt_hash, "
                    "'idea', 'ongoing', :activated_at, :activated_at)"
                ),
                {
                    "cycle_ref": cycle_ref,
                    "quest_ref": quest.quest_ref,
                    "question_ref": question.question_ref,
                    "question_receipt_ref": question.receipt.receipt_ref,
                    "question_receipt_hash": question.receipt.payload_hash,
                    "activated_at": activated_at,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO ae_foreground_heads (quest_ref, cycle_ref, "
                    "question_ref, stage, epoch, status, pending_operation_ref, "
                    "updated_at) VALUES (:quest_ref, :cycle_ref, :question_ref, "
                    "'idea', 1, 'active', NULL, :activated_at)"
                ),
                {
                    "quest_ref": quest.quest_ref,
                    "cycle_ref": cycle_ref,
                    "question_ref": question.question_ref,
                    "activated_at": activated_at,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO ae_foreground_grants (grant_ref, quest_ref, "
                    "cycle_ref, question_ref, stage, epoch, status, "
                    "predecessor_grant_ref, safe_point_ref, granted_at, revoked_at) "
                    "VALUES (:grant_ref, :quest_ref, :cycle_ref, :question_ref, "
                    "'idea', 1, 'active', NULL, NULL, :activated_at, NULL)"
                ),
                {
                    "grant_ref": new_ref("foreground_grant"),
                    "quest_ref": quest.quest_ref,
                    "cycle_ref": cycle_ref,
                    "question_ref": question.question_ref,
                    "activated_at": activated_at,
                },
            )
            connection.execute(
                text(
                    "UPDATE advancement_engine_state SET revision = revision + 1, "
                    "foreground_cycle_count = foreground_cycle_count + 1 "
                    "WHERE singleton = 'owner'"
                )
            )
            self._feed.record(
                connection,
                "advancement_engine.initial_cycle_activated",
                {
                    "initialization_id": initialization_id,
                    "quest_ref": quest.quest_ref,
                    "question_ref": question.question_ref,
                    "cycle_ref": cycle_ref,
                    "receipt_ref": receipt_ref,
                },
            )
        activated = self.query_initial_cycle(initialization_id)
        if activated is None:
            raise OwnerConflict("cycle_receipt_missing_after_commit")
        return activated

    def _current_stage_epoch(
        self, cycle_ref: str, quest_ref: str, expected_stage: str
    ) -> int:
        with self._database.read() as connection:
            head = connection.execute(
                text(
                    "SELECT * FROM ae_foreground_heads WHERE quest_ref = :quest_ref"
                ),
                {"quest_ref": quest_ref},
            ).first()
        if head is None or head.cycle_ref != cycle_ref:
            raise OwnerConflict("stage_request_epoch_revoked")
        if head.status != "active":
            raise OwnerConflict("foreground_cycle_not_active")
        if head.pending_operation_ref is not None:
            raise OwnerConflict("stage_run_handoff_pending")
        if head.stage != expected_stage:
            raise OwnerConflict("stage_request_not_current")
        return int(head.epoch)

    @staticmethod
    def _assert_stage_head_current(
        connection,
        *,
        cycle_ref: str,
        quest_ref: str,
        stage: str,
        epoch: int,
    ) -> None:
        head = connection.execute(
            text(
                "SELECT * FROM ae_foreground_heads WHERE quest_ref = :quest_ref"
            ),
            {"quest_ref": quest_ref},
        ).first()
        if head is None or head.cycle_ref != cycle_ref or int(head.epoch) != epoch:
            raise OwnerConflict("stage_request_epoch_revoked")
        if head.status != "active":
            raise OwnerConflict("foreground_cycle_not_active")
        if head.pending_operation_ref is not None:
            raise OwnerConflict("stage_run_handoff_pending")
        if head.stage != stage:
            raise OwnerConflict("stage_request_not_current")

    def ensure_idea_stage_request(
        self,
        *,
        cycle_ref: str,
        accepted_question: AcceptedQuestionBinding,
        context_pack: dict[str, object],
        idempotency_key: str,
    ) -> StageRunRequest:
        _validate_idempotency_key(idempotency_key)
        if self._authorization_verifier is None:
            raise OwnerConflict("broad_research_authorization_verifier_unavailable")
        self._authorization_verifier.verify_broad_research_authorization(
            quest_ref=accepted_question.quest_ref
        )
        context_pack_json = canonical_json(context_pack)
        context_pack_hash = canonical_hash(context_pack)
        epoch = self._current_stage_epoch(
            cycle_ref, accepted_question.quest_ref, IDEA_STAGE
        )
        request_input = {
            "command": "ensure_idea_stage_request",
            "cycle_ref": cycle_ref,
            "stage": IDEA_STAGE,
            "epoch": epoch,
            "accepted_question": accepted_question.as_dict(),
            "context_pack_hash": context_pack_hash,
        }
        request_hash = canonical_hash(request_input)
        replay_ref = _query_ae_command(
            self._database,
            idempotency_key,
            "ensure_idea_stage_request",
            request_hash,
        )
        if replay_ref is not None:
            return self._query_stage_request_ref(replay_ref)
        try:
            evidence_refs = validate_idea_context_pack(
                context_pack,
                cycle_ref=cycle_ref,
                accepted_question_binding=accepted_question.as_dict(),
            )
        except IdeaContractError as error:
            raise OwnerConflict(str(error)) from error
        self._verify_cycle_question(cycle_ref, accepted_question)
        self._verify_context_literature(accepted_question, context_pack)

        # Natural-key replay is a historical receipt lookup. It must not
        # pursue today's Evidence set or current custody.
        with self._database.read() as connection:
            existing = connection.execute(
                text(
                    "SELECT * FROM ae_stage_run_requests WHERE cycle_ref = "
                    ":cycle_ref AND stage = 'idea' AND epoch = :epoch"
                ),
                {"cycle_ref": cycle_ref, "epoch": epoch},
            ).first()
        if existing is not None:
            if existing.request_hash != request_hash:
                raise OwnerConflict("stage_run_request_conflict")
            with self._database.write() as connection:
                connection.execute(
                    text(
                        "UPDATE advancement_engine_state SET revision = revision "
                        "WHERE singleton = 'owner'"
                    )
                )
                replay_ref = _ae_command_replay(
                    connection,
                    idempotency_key,
                    "ensure_idea_stage_request",
                    request_hash,
                )
                if replay_ref is None:
                    self._assert_stage_head_current(
                        connection,
                        cycle_ref=cycle_ref,
                        quest_ref=accepted_question.quest_ref,
                        stage=IDEA_STAGE,
                        epoch=epoch,
                    )
                    current = connection.execute(
                        text(
                            "SELECT * FROM ae_stage_run_requests WHERE cycle_ref = "
                            ":cycle_ref AND stage = 'idea' AND epoch = :epoch"
                        ),
                        {"cycle_ref": cycle_ref, "epoch": epoch},
                    ).first()
                    if current is None or current.request_hash != request_hash:
                        raise OwnerConflict("stage_run_request_conflict")
                    replay_ref = current.request_ref
                    _record_ae_command(
                        connection,
                        idempotency_key,
                        "ensure_idea_stage_request",
                        request_hash,
                        replay_ref,
                    )
            return self._query_stage_request_ref(replay_ref)

        # Current custody verification can involve bounded file hashing. Keep
        # it outside the process-wide SQLite writer lock, then close the race
        # with a cheap per-Quest Evidence CAS inside the transaction.
        self._verify_context_evidence(
            accepted_question,
            context_pack,
            evidence_refs,
            require_current=True,
        )
        try:
            reference_revision = evidence_reference_revision(context_pack)
        except IdeaContractError as error:
            raise OwnerConflict(str(error)) from error
        if reference_revision is None or self._evidence_verifier is None:
            raise OwnerConflict("evidence_verifier_unavailable")

        result_ref: str
        with self._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE advancement_engine_state SET revision = revision WHERE "
                    "singleton = 'owner'"
                )
            )
            replay_ref = _ae_command_replay(
                connection,
                idempotency_key,
                "ensure_idea_stage_request",
                request_hash,
            )
            if replay_ref is not None:
                result_ref = replay_ref
            else:
                self._assert_stage_head_current(
                    connection,
                    cycle_ref=cycle_ref,
                    quest_ref=accepted_question.quest_ref,
                    stage=IDEA_STAGE,
                    epoch=epoch,
                )
                existing = connection.execute(
                    text(
                        "SELECT * FROM ae_stage_run_requests WHERE cycle_ref = "
                        ":cycle_ref AND stage = 'idea' AND epoch = :epoch"
                    ),
                    {"cycle_ref": cycle_ref, "epoch": epoch},
                ).first()
                if existing is not None:
                    if existing.request_hash != request_hash:
                        raise OwnerConflict("stage_run_request_conflict")
                    result_ref = existing.request_ref
                    _record_ae_command(
                        connection,
                        idempotency_key,
                        "ensure_idea_stage_request",
                        request_hash,
                        result_ref,
                    )
                else:
                    self._evidence_verifier.assert_evidence_state(
                        quest_ref=accepted_question.quest_ref,
                        version_refs=tuple(sorted(evidence_refs)),
                        expected_reference_revision=reference_revision,
                    )
                    guidance_bindings = context_pack.get(
                        "active_guidance_bindings"
                    )
                    if not isinstance(guidance_bindings, list):
                        raise OwnerConflict(
                            "idea_context_guidance_bindings_invalid"
                        )
                    self._authorization_verifier.verify_guidance_snapshot(
                        scope_ref=f"quest:{accepted_question.quest_ref}",
                        bindings=cast(
                            list[dict[str, object]], guidance_bindings
                        ),
                    )
                    request_ref = new_ref("stage_request")
                    context_pack_ref = new_ref("context_pack")
                    receipt_ref = new_ref("ae_stage_request_receipt")
                    bindings = {
                        **_question_binding_columns(accepted_question),
                        "cycle_ref": cycle_ref,
                        "stage": IDEA_STAGE,
                        "epoch": epoch,
                        "context_pack_ref": context_pack_ref,
                        "context_pack_hash": context_pack_hash,
                    }
                    receipt_hash = _receipt_hash(
                        STAGE_REQUEST_RECEIPT_KIND, request_ref, bindings
                    )
                    connection.execute(
                        text(
                            "INSERT INTO ae_stage_run_requests (request_ref, "
                            "cycle_ref, stage, epoch, initialization_id, quest_ref, "
                            "question_ref, content_ref, content_hash, schema_ref, "
                            "content_receipt_ref, content_receipt_hash, "
                            "question_receipt_ref, question_receipt_hash, "
                            "context_pack_ref, context_pack_json, context_pack_hash, "
                            "idempotency_key, request_hash, receipt_ref, receipt_hash, "
                            "created_at) VALUES (:request_ref, :cycle_ref, :stage, "
                            ":epoch, :initialization_id, :quest_ref, :question_ref, "
                            ":content_ref, :content_hash, :schema_ref, "
                            ":content_receipt_ref, :content_receipt_hash, "
                            ":question_receipt_ref, :question_receipt_hash, "
                            ":context_pack_ref, :context_pack_json, "
                            ":context_pack_hash, :idempotency_key, :request_hash, "
                            ":receipt_ref, :receipt_hash, :created_at)"
                        ),
                        {
                            **bindings,
                            "request_ref": request_ref,
                            "context_pack_json": context_pack_json,
                            "idempotency_key": idempotency_key,
                            "request_hash": request_hash,
                            "receipt_ref": receipt_ref,
                            "receipt_hash": receipt_hash,
                            "created_at": time.time(),
                        },
                    )
                    _record_ae_command(
                        connection,
                        idempotency_key,
                        "ensure_idea_stage_request",
                        request_hash,
                        request_ref,
                    )
                    connection.execute(
                        text(
                            "UPDATE advancement_engine_state SET revision = "
                            "revision + 1, stage_request_count = "
                            "stage_request_count + 1 WHERE singleton = 'owner'"
                        )
                    )
                    self._feed.record(
                        connection,
                        "advancement_engine.stage_run_requested",
                        {
                            "request_ref": request_ref,
                            "cycle_ref": cycle_ref,
                            "stage": IDEA_STAGE,
                            "epoch": epoch,
                            "context_pack_ref": context_pack_ref,
                            "context_pack_hash": context_pack_hash,
                            "receipt_ref": receipt_ref,
                        },
                    )
                    result_ref = request_ref
        return self._query_stage_request_ref(result_ref)

    def query_idea_stage_request(self, cycle_ref: str) -> StageRunRequest | None:
        with self._database.read() as connection:
            head = connection.execute(
                text(
                    "SELECT * FROM ae_foreground_heads WHERE cycle_ref = :cycle_ref "
                    "AND status = 'active'"
                ),
                {"cycle_ref": cycle_ref},
            ).first()
            if head is None:
                row = None
            elif head.stage == IDEA_STAGE:
                row = connection.execute(
                    text(
                        "SELECT * FROM ae_stage_run_requests WHERE cycle_ref = "
                        ":cycle_ref AND stage = 'idea' AND epoch = :epoch"
                    ),
                    {"cycle_ref": cycle_ref, "epoch": int(head.epoch)},
                ).first()
            else:
                row = connection.execute(
                    text(
                        "SELECT requests.* FROM ae_stage_run_requests requests JOIN "
                        "ae_stage_commits commits ON commits.request_ref = "
                        "requests.request_ref WHERE requests.cycle_ref = :cycle_ref "
                        "AND requests.stage = 'idea' ORDER BY requests.epoch DESC "
                        "LIMIT 1"
                    ),
                    {"cycle_ref": cycle_ref},
                ).first()
        if row is None:
            return None
        return self._stage_request_from_row(row)

    def ensure_plan_stage_request(
        self,
        *,
        cycle_ref: str,
        accepted_question: AcceptedQuestionBinding,
        accepted_idea_set: AcceptedIdeaSetBinding,
        context_pack: dict[str, object],
        idempotency_key: str,
    ) -> StageRunRequest:
        """Freeze the accepted Question, IdeaSet closure, and Evidence snapshot."""

        _validate_idempotency_key(idempotency_key)
        context_pack_json = canonical_json(context_pack)
        context_pack_hash = canonical_hash(context_pack)
        epoch = self._current_stage_epoch(
            cycle_ref, accepted_question.quest_ref, PLAN_STAGE
        )
        command_kind = "ensure_plan_stage_request"
        request_input = {
            "command": command_kind,
            "cycle_ref": cycle_ref,
            "stage": PLAN_STAGE,
            "epoch": epoch,
            "accepted_question": accepted_question.as_dict(),
            "accepted_idea_set": accepted_idea_set.as_dict(),
            "context_pack_hash": context_pack_hash,
        }
        request_hash = canonical_hash(request_input)
        replay_ref = _query_ae_command(
            self._database,
            idempotency_key,
            command_kind,
            request_hash,
        )
        if replay_ref is not None:
            return self._query_stage_request_ref(replay_ref)
        try:
            validate_plan_context_pack(
                context_pack,
                cycle_ref=cycle_ref,
                accepted_question_binding=accepted_question.as_dict(),
            )
        except PlanContractError as error:
            raise OwnerConflict(str(error)) from error
        if context_pack.get("accepted_idea_set_binding") != accepted_idea_set.as_dict():
            raise OwnerConflict("plan_idea_set_binding_invalid")
        self._verify_cycle_question(cycle_ref, accepted_question)
        self._verify_plan_idea_set(cycle_ref, accepted_idea_set)

        with self._database.read() as connection:
            existing = connection.execute(
                text(
                    "SELECT * FROM ae_stage_run_requests WHERE cycle_ref = "
                    ":cycle_ref AND stage = 'plan' AND epoch = :epoch"
                ),
                {"cycle_ref": cycle_ref, "epoch": epoch},
            ).first()
        if existing is not None:
            if existing.request_hash != request_hash:
                raise OwnerConflict("stage_run_request_conflict")
            with self._database.write() as connection:
                connection.execute(
                    text(
                        "UPDATE advancement_engine_state SET revision = revision "
                        "WHERE singleton = 'owner'"
                    )
                )
                replay_ref = _ae_command_replay(
                    connection,
                    idempotency_key,
                    command_kind,
                    request_hash,
                )
                if replay_ref is None:
                    self._assert_stage_head_current(
                        connection,
                        cycle_ref=cycle_ref,
                        quest_ref=accepted_question.quest_ref,
                        stage=PLAN_STAGE,
                        epoch=epoch,
                    )
                    current = connection.execute(
                        text(
                            "SELECT * FROM ae_stage_run_requests WHERE cycle_ref = "
                            ":cycle_ref AND stage = 'plan' AND epoch = :epoch"
                        ),
                        {"cycle_ref": cycle_ref, "epoch": epoch},
                    ).first()
                    if current is None or current.request_hash != request_hash:
                        raise OwnerConflict("stage_run_request_conflict")
                    replay_ref = current.request_ref
                    _record_ae_command(
                        connection,
                        idempotency_key,
                        command_kind,
                        request_hash,
                        replay_ref,
                    )
            return self._query_stage_request_ref(replay_ref)

        reference_revision = context_pack["evidence_reference_revision"]
        assert isinstance(reference_revision, int)
        evidence_catalog = context_pack["evidence_catalog"]
        assert isinstance(evidence_catalog, list)
        self._verify_plan_evidence(
            accepted_question,
            evidence_catalog,
            expected_reference_revision=reference_revision,
        )
        if self._evidence_verifier is None:
            raise OwnerConflict("evidence_verifier_unavailable")

        with self._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE advancement_engine_state SET revision = revision WHERE "
                    "singleton = 'owner'"
                )
            )
            replay_ref = _ae_command_replay(
                connection,
                idempotency_key,
                command_kind,
                request_hash,
            )
            if replay_ref is not None:
                result_ref = replay_ref
            else:
                self._assert_stage_head_current(
                    connection,
                    cycle_ref=cycle_ref,
                    quest_ref=accepted_question.quest_ref,
                    stage=PLAN_STAGE,
                    epoch=epoch,
                )
                existing = connection.execute(
                    text(
                        "SELECT * FROM ae_stage_run_requests WHERE cycle_ref = "
                        ":cycle_ref AND stage = 'plan' AND epoch = :epoch"
                    ),
                    {"cycle_ref": cycle_ref, "epoch": epoch},
                ).first()
                if existing is not None:
                    if existing.request_hash != request_hash:
                        raise OwnerConflict("stage_run_request_conflict")
                    result_ref = existing.request_ref
                    _record_ae_command(
                        connection,
                        idempotency_key,
                        command_kind,
                        request_hash,
                        result_ref,
                    )
                else:
                    self._evidence_verifier.verify_plan_evidence_catalog(
                        quest_ref=accepted_question.quest_ref,
                        evidence_catalog=evidence_catalog,
                        expected_reference_revision=reference_revision,
                        require_current=True,
                        require_complete=True,
                    )
                    request_ref = new_ref("stage_request")
                    context_pack_ref = new_ref("context_pack")
                    receipt_ref = new_ref("ae_stage_request_receipt")
                    bindings = {
                        **_question_binding_columns(accepted_question),
                        "cycle_ref": cycle_ref,
                        "stage": PLAN_STAGE,
                        "epoch": epoch,
                        "context_pack_ref": context_pack_ref,
                        "context_pack_hash": context_pack_hash,
                    }
                    receipt_hash = _receipt_hash(
                        STAGE_REQUEST_RECEIPT_KIND,
                        request_ref,
                        bindings,
                    )
                    connection.execute(
                        text(
                            "INSERT INTO ae_stage_run_requests (request_ref, "
                            "cycle_ref, stage, epoch, initialization_id, quest_ref, "
                            "question_ref, content_ref, content_hash, schema_ref, "
                            "content_receipt_ref, content_receipt_hash, "
                            "question_receipt_ref, question_receipt_hash, "
                            "context_pack_ref, context_pack_json, context_pack_hash, "
                            "idempotency_key, request_hash, receipt_ref, receipt_hash, "
                            "created_at) VALUES (:request_ref, :cycle_ref, :stage, "
                            ":epoch, :initialization_id, :quest_ref, :question_ref, "
                            ":content_ref, :content_hash, :schema_ref, "
                            ":content_receipt_ref, :content_receipt_hash, "
                            ":question_receipt_ref, :question_receipt_hash, "
                            ":context_pack_ref, :context_pack_json, "
                            ":context_pack_hash, :idempotency_key, :request_hash, "
                            ":receipt_ref, :receipt_hash, :created_at)"
                        ),
                        {
                            **bindings,
                            "request_ref": request_ref,
                            "context_pack_json": context_pack_json,
                            "idempotency_key": idempotency_key,
                            "request_hash": request_hash,
                            "receipt_ref": receipt_ref,
                            "receipt_hash": receipt_hash,
                            "created_at": time.time(),
                        },
                    )
                    _record_ae_command(
                        connection,
                        idempotency_key,
                        command_kind,
                        request_hash,
                        request_ref,
                    )
                    connection.execute(
                        text(
                            "UPDATE advancement_engine_state SET revision = "
                            "revision + 1, stage_request_count = "
                            "stage_request_count + 1 WHERE singleton = 'owner'"
                        )
                    )
                    self._feed.record(
                        connection,
                        "advancement_engine.stage_run_requested",
                        {
                            "request_ref": request_ref,
                            "cycle_ref": cycle_ref,
                            "stage": PLAN_STAGE,
                            "epoch": epoch,
                            "context_pack_ref": context_pack_ref,
                            "context_pack_hash": context_pack_hash,
                            "idea_set_ref": accepted_idea_set.outcome_ref,
                            "receipt_ref": receipt_ref,
                        },
                    )
                    result_ref = request_ref
        return self._query_stage_request_ref(result_ref)

    def query_plan_stage_request(self, cycle_ref: str) -> StageRunRequest | None:
        with self._database.read() as connection:
            head = connection.execute(
                text(
                    "SELECT * FROM ae_foreground_heads WHERE cycle_ref = :cycle_ref "
                    "AND status = 'active'"
                ),
                {"cycle_ref": cycle_ref},
            ).first()
            if head is None:
                row = None
            elif head.stage == PLAN_STAGE:
                row = connection.execute(
                    text(
                        "SELECT * FROM ae_stage_run_requests WHERE cycle_ref = "
                        ":cycle_ref AND stage = 'plan' AND epoch = :epoch"
                    ),
                    {"cycle_ref": cycle_ref, "epoch": int(head.epoch)},
                ).first()
            else:
                row = connection.execute(
                    text(
                        "SELECT requests.* FROM ae_stage_run_requests requests JOIN "
                        "ae_stage_commits commits ON commits.request_ref = "
                        "requests.request_ref WHERE requests.cycle_ref = :cycle_ref "
                        "AND requests.stage = 'plan' ORDER BY requests.epoch DESC "
                        "LIMIT 1"
                    ),
                    {"cycle_ref": cycle_ref},
                ).first()
        if row is None:
            return None
        return self._stage_request_from_row(row)

    def _query_stage_request_ref(self, request_ref: str) -> StageRunRequest:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ae_stage_run_requests WHERE request_ref = "
                    ":request_ref"
                ),
                {"request_ref": request_ref},
            ).first()
        if row is None:
            raise OwnerConflict("stage_command_result_missing")
        return self._stage_request_from_row(row)

    def _stage_request_from_row(self, row) -> StageRunRequest:
        requested = _stage_request(row)
        self._verify_cycle_question(row.cycle_ref, requested.accepted_question)
        if requested.stage == IDEA_STAGE:
            try:
                evidence_refs = validate_idea_context_pack(
                    requested.context_pack,
                    cycle_ref=requested.cycle_ref,
                    accepted_question_binding=requested.accepted_question.as_dict(),
                )
            except IdeaContractError as error:
                raise OwnerConflict(str(error)) from error
            self._verify_context_evidence(
                requested.accepted_question,
                requested.context_pack,
                evidence_refs,
                require_current=False,
            )
            self._verify_context_literature(
                requested.accepted_question, requested.context_pack
            )
        elif requested.stage == PLAN_STAGE and requested.accepted_idea_set is not None:
            self._verify_plan_idea_set(
                requested.cycle_ref,
                requested.accepted_idea_set,
            )
            evidence_by_ref = validate_plan_context_pack(
                requested.context_pack,
                cycle_ref=requested.cycle_ref,
                accepted_question_binding=requested.accepted_question.as_dict(),
            )
            self._verify_plan_evidence(
                requested.accepted_question,
                list(evidence_by_ref.values()),
                expected_reference_revision=int(
                    requested.context_pack["evidence_reference_revision"]
                ),
                require_current=False,
                require_complete=False,
            )
        else:
            raise OwnerConflict("stage_run_request_invalid")
        self._stage_request_verifier.verify_stage_run_request(
            request_ref=requested.request_ref,
            cycle_ref=requested.cycle_ref,
            epoch=requested.epoch,
            context_pack_ref=requested.context_pack_ref,
            context_pack_hash=requested.context_pack_hash,
            receipt=requested.receipt,
        )
        return requested

    def _verify_plan_idea_set(
        self,
        cycle_ref: str,
        binding: AcceptedIdeaSetBinding,
    ) -> None:
        if self._outcome_verifier is None:
            raise OwnerConflict("plan_idea_set_verifier_unavailable")
        self._outcome_verifier.verify_accepted_idea_set_binding(binding)
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ae_stage_commits WHERE commit_ref = :commit_ref"
                ),
                {"commit_ref": binding.stage_commit_ref},
            ).first()
        if row is None:
            raise OwnerConflict("plan_idea_set_stage_commit_invalid")
        commit = self._stage_commit_from_row(row)
        if (
            commit.cycle_ref != cycle_ref
            or commit.stage != IDEA_STAGE
            or commit.outcome_kind != IDEA_SET_OUTCOME_KIND
            or commit.outcome_ref != binding.outcome_ref
            or commit.outcome_receipt != binding.outcome_receipt
            or commit.receipt != binding.stage_commit_receipt
        ):
            raise OwnerConflict("plan_idea_set_stage_commit_invalid")

    def _verify_plan_evidence(
        self,
        accepted_question: AcceptedQuestionBinding,
        evidence_catalog: list[dict[str, object]],
        *,
        expected_reference_revision: int,
        require_current: bool = True,
        require_complete: bool = True,
    ) -> None:
        if self._evidence_verifier is None:
            raise OwnerConflict("evidence_verifier_unavailable")
        self._evidence_verifier.verify_plan_evidence_catalog(
            quest_ref=accepted_question.quest_ref,
            evidence_catalog=evidence_catalog,
            expected_reference_revision=expected_reference_revision,
            require_current=require_current,
            require_complete=require_complete,
        )

    def _verify_context_evidence(
        self,
        accepted_question: AcceptedQuestionBinding,
        context_pack: dict[str, object],
        evidence_refs: set[str],
        *,
        require_current: bool,
    ) -> None:
        try:
            reference_revision = evidence_reference_revision(context_pack)
        except IdeaContractError as error:
            raise OwnerConflict(str(error)) from error
        if require_current and reference_revision is None:
            raise OwnerConflict("idea_context_pack_invalid")
        if not require_current and not evidence_refs:
            return
        if self._evidence_verifier is None:
            raise OwnerConflict("evidence_verifier_unavailable")
        self._evidence_verifier.verify_evidence_refs(
            quest_ref=accepted_question.quest_ref,
            version_refs=tuple(sorted(evidence_refs)),
            expected_reference_revision=(
                reference_revision if require_current else None
            ),
        )

    def _verify_context_literature(
        self,
        accepted_question: AcceptedQuestionBinding,
        context_pack: dict[str, object],
    ) -> None:
        try:
            binding = literature_binding(context_pack)
        except IdeaContractError as error:
            raise OwnerConflict(str(error)) from error
        if binding is None:
            return
        if self._literature_snapshot_verifier is None:
            raise OwnerConflict("literature_snapshot_verifier_unavailable")
        receipt_value = binding["receipt"]
        assert isinstance(receipt_value, dict)
        if binding["initialization_id"] != accepted_question.initialization_id:
            raise OwnerConflict("literature_snapshot_binding_invalid")
        receipt = AcceptanceReceipt(
            issuer=str(receipt_value["issuer"]),
            kind=str(receipt_value["kind"]),
            receipt_ref=str(receipt_value["receipt_ref"]),
            subject_ref=str(receipt_value["subject_ref"]),
            payload_hash=str(receipt_value["payload_hash"]),
        )
        self._literature_snapshot_verifier.verify_literature_snapshot_binding(
            snapshot_ref=str(binding["snapshot_ref"]),
            snapshot_hash=str(binding["snapshot_hash"]),
            initialization_id=str(binding["initialization_id"]),
            draft_revision=int(binding["draft_revision"]),
            draft_hash=str(binding["draft_hash"]),
            receipt=receipt,
        )

    def _verify_cycle_question(
        self, cycle_ref: str, accepted_question: AcceptedQuestionBinding
    ) -> None:
        if self._accepted_question_verifier is None:
            raise OwnerConflict("accepted_question_verifier_unavailable")
        with self._database.read() as connection:
            cycle = connection.execute(
                text("SELECT * FROM ae_cycles WHERE cycle_ref = :cycle_ref"),
                {"cycle_ref": cycle_ref},
            ).first()
        if cycle is None or (
            cycle.quest_ref != accepted_question.quest_ref
            or cycle.question_ref != accepted_question.question_ref
            or cycle.question_receipt_ref
            != accepted_question.question_receipt.receipt_ref
            or cycle.question_receipt_hash
            != accepted_question.question_receipt.payload_hash
        ):
            raise OwnerConflict("stage_run_question_lineage_invalid")
        self._accepted_question_verifier.verify_accepted_question_binding(
            accepted_question
        )

    def commit_idea_stage(
        self,
        *,
        request_ref: str,
        run_ref: str,
        outcome_ref: str,
        outcome_kind: str,
        run_completion_receipt: AcceptanceReceipt,
        outcome_receipt: AcceptanceReceipt,
        idempotency_key: str,
    ) -> StageCommit:
        _validate_idempotency_key(idempotency_key)
        command_input = {
            "command": "commit_idea_stage",
            "request_ref": request_ref,
            "run_ref": run_ref,
            "outcome_ref": outcome_ref,
            "outcome_kind": outcome_kind,
            "disposition": COMPLETED_DISPOSITION,
            "run_completion_receipt": run_completion_receipt.as_public_dict(),
            "outcome_receipt": outcome_receipt.as_public_dict(),
        }
        command_hash = canonical_hash(command_input)
        _query_ae_command(
            self._database,
            idempotency_key,
            "commit_idea_stage",
            command_hash,
        )
        if outcome_kind not in COMPLETABLE_IDEA_OUTCOME_KINDS:
            raise OwnerConflict("idea_stage_outcome_not_committable")
        request = self._query_stage_request_by_ref(request_ref)
        if request.stage != IDEA_STAGE:
            raise OwnerConflict("idea_stage_request_invalid")
        if self.query_idea_stage_commit(request_ref) is None:
            self._assert_stage_request_current(request)
        if self._run_completion_verifier is None or self._outcome_verifier is None:
            raise OwnerConflict("idea_stage_verifier_unavailable")
        self._run_completion_verifier.verify_run_completion_receipt(
            request_ref=request_ref,
            run_ref=run_ref,
            attempt_ref=None,
            outcome_ref=outcome_ref,
            receipt=run_completion_receipt,
        )
        self._outcome_verifier.verify_idea_outcome_decision(
            request_ref=request_ref,
            submission_ref=None,
            decision="accepted",
            outcome_ref=outcome_ref,
            outcome_kind=outcome_kind,
            receipt=outcome_receipt,
        )
        with self._database.write() as connection:
            replay_ref = _ae_command_replay(
                connection,
                idempotency_key,
                "commit_idea_stage",
                command_hash,
            )
            if replay_ref is not None:
                replay = connection.execute(
                    text(
                        "SELECT * FROM ae_stage_commits WHERE commit_ref = "
                        ":commit_ref"
                    ),
                    {"commit_ref": replay_ref},
                ).first()
                if replay is None:
                    raise OwnerConflict("stage_command_result_missing")
                return self._stage_commit_from_row(replay)

            existing = connection.execute(
                text("SELECT * FROM ae_stage_commits WHERE request_ref = :request_ref"),
                {"request_ref": request_ref},
            ).first()
            if existing is not None:
                if existing.request_hash != command_hash:
                    raise OwnerConflict("stage_commit_conflict")
                _record_ae_command(
                    connection,
                    idempotency_key,
                    "commit_idea_stage",
                    command_hash,
                    existing.commit_ref,
                )
                return self._stage_commit_from_row(existing)

            commit_ref = new_ref("stage_commit")
            receipt_ref = new_ref("ae_stage_commit_receipt")
            bindings = {
                "request_ref": request_ref,
                "cycle_ref": request.cycle_ref,
                "stage": request.stage,
                "epoch": request.epoch,
                "run_ref": run_ref,
                "outcome_ref": outcome_ref,
                "outcome_kind": outcome_kind,
                "disposition": COMPLETED_DISPOSITION,
                "run_completion_receipt_ref": run_completion_receipt.receipt_ref,
                "run_completion_receipt_hash": run_completion_receipt.payload_hash,
                "outcome_receipt_ref": outcome_receipt.receipt_ref,
                "outcome_receipt_hash": outcome_receipt.payload_hash,
            }
            receipt_hash = _receipt_hash(STAGE_COMMIT_RECEIPT_KIND, commit_ref, bindings)
            connection.execute(
                text(
                    "INSERT INTO ae_stage_commits (commit_ref, request_ref, "
                    "cycle_ref, stage, epoch, run_ref, outcome_ref, "
                    "outcome_kind, disposition, "
                    "run_completion_receipt_ref, run_completion_receipt_hash, "
                    "outcome_receipt_ref, outcome_receipt_hash, idempotency_key, "
                    "request_hash, receipt_ref, receipt_hash, committed_at) VALUES "
                    "(:commit_ref, :request_ref, :cycle_ref, :stage, :epoch, "
                    ":run_ref, :outcome_ref, :outcome_kind, :disposition, "
                    ":run_completion_receipt_ref, "
                    ":run_completion_receipt_hash, :outcome_receipt_ref, "
                    ":outcome_receipt_hash, :idempotency_key, :request_hash, "
                    ":receipt_ref, :receipt_hash, :committed_at)"
                ),
                {
                    **bindings,
                    "commit_ref": commit_ref,
                    "idempotency_key": idempotency_key,
                    "request_hash": command_hash,
                    "receipt_ref": receipt_ref,
                    "receipt_hash": receipt_hash,
                    "committed_at": time.time(),
                },
            )
            _record_ae_command(
                connection,
                idempotency_key,
                "commit_idea_stage",
                command_hash,
                commit_ref,
            )
            self._advance_cycle_after_stage_commit(
                connection,
                cycle_ref=request.cycle_ref,
                quest_ref=request.accepted_question.quest_ref,
                stage=request.stage,
                epoch=request.epoch,
                disposition=COMPLETED_DISPOSITION,
                outcome_kind=outcome_kind,
            )
            connection.execute(
                text(
                    "UPDATE advancement_engine_state SET revision = revision + 1, "
                    "stage_commit_count = stage_commit_count + 1 "
                    "WHERE singleton = 'owner'"
                )
            )
            self._feed.record(
                connection,
                "advancement_engine.stage_committed",
                {
                    "commit_ref": commit_ref,
                    "request_ref": request_ref,
                    "run_ref": run_ref,
                    "outcome_ref": outcome_ref,
                    "outcome_kind": outcome_kind,
                    "disposition": COMPLETED_DISPOSITION,
                    "stage": request.stage,
                    "epoch": request.epoch,
                    "receipt_ref": receipt_ref,
                },
            )
        self._resume_normal_handoff_after_commit(request.cycle_ref)
        committed = self.query_idea_stage_commit(request_ref)
        if committed is None:
            raise OwnerConflict("stage_commit_missing_after_commit")
        return committed

    def commit_stage_disposition(
        self,
        *,
        disposition: str,
        basis_kind: str,
        basis_ref: str,
        basis_receipt: AcceptanceReceipt,
        idempotency_key: str,
        request_ref: str | None = None,
        cycle_ref: str | None = None,
        stage: str | None = None,
        epoch: int | None = None,
        run_ref: str | None = None,
        run_completion_receipt: AcceptanceReceipt | None = None,
    ) -> StageCommit:
        """Commit a verifier-owned Skipped or execution-backed Exhausted fact.

        Skipped has no fake execution. Exhausted proves a current completed Run
        plus a reviewed domain basis, but never impersonates Completed.
        """

        _validate_idempotency_key(idempotency_key)
        if disposition not in BASIS_DISPOSITIONS:
            raise OwnerConflict("stage_commit_disposition_invalid")
        if not basis_kind or not basis_ref:
            raise OwnerConflict("stage_commit_basis_invalid")
        if disposition == EXHAUSTED_DISPOSITION:
            if (
                request_ref is None
                or run_ref is None
                or run_completion_receipt is None
            ):
                raise OwnerConflict("stage_disposition_execution_required")
        elif (
            request_ref is not None
            or run_ref is not None
            or run_completion_receipt is not None
        ):
            raise OwnerConflict("stage_disposition_execution_unexpected")
        request = (
            None
            if request_ref is None
            else self._query_stage_request_by_ref(request_ref)
        )
        if request is not None:
            if any(
                provided is not None and provided != actual
                for provided, actual in (
                    (cycle_ref, request.cycle_ref),
                    (stage, request.stage),
                    (epoch, request.epoch),
                )
            ):
                raise OwnerConflict("stage_commit_position_invalid")
            cycle_ref = request.cycle_ref
            stage = request.stage
            epoch = request.epoch
            quest_ref = request.accepted_question.quest_ref
            question_ref = request.accepted_question.question_ref
        else:
            if (
                not isinstance(cycle_ref, str)
                or not cycle_ref
                or stage not in STAGES
                or not isinstance(epoch, int)
                or isinstance(epoch, bool)
                or epoch < 1
            ):
                raise OwnerConflict("stage_commit_position_invalid")
            with self._database.read() as connection:
                cycle = connection.execute(
                    text(
                        "SELECT quest_ref, question_ref, status FROM ae_cycles "
                        "WHERE cycle_ref = :cycle_ref"
                    ),
                    {"cycle_ref": cycle_ref},
                ).first()
                head = connection.execute(
                    text(
                        "SELECT * FROM ae_foreground_heads WHERE cycle_ref = "
                        ":cycle_ref"
                    ),
                    {"cycle_ref": cycle_ref},
                ).first()
                existing_position = connection.execute(
                    text(
                        "SELECT commit_ref FROM ae_stage_commits WHERE cycle_ref = "
                        ":cycle_ref AND stage = :stage AND epoch = :epoch"
                    ),
                    {"cycle_ref": cycle_ref, "stage": stage, "epoch": epoch},
                ).first()
                started = connection.execute(
                    text(
                        "SELECT request_ref FROM ae_stage_run_requests WHERE "
                        "cycle_ref = :cycle_ref AND stage = :stage AND epoch = "
                        ":epoch"
                    ),
                    {"cycle_ref": cycle_ref, "stage": stage, "epoch": epoch},
                ).first()
            if cycle is None:
                raise OwnerConflict("stage_commit_position_invalid")
            if existing_position is None and (
                head is None
                or head.stage != stage
                or int(head.epoch) != epoch
                or head.status != "active"
                or head.pending_operation_ref is not None
                or cycle.status != "ongoing"
            ):
                raise OwnerConflict("stage_request_epoch_revoked")
            if (
                existing_position is None
                and disposition == SKIPPED_DISPOSITION
                and started is not None
            ):
                raise OwnerConflict("stage_disposition_execution_already_started")
            quest_ref = str(cycle.quest_ref)
            question_ref = str(cycle.question_ref)
        assert cycle_ref is not None and stage is not None and epoch is not None
        if stage == "reasoning":
            raise OwnerConflict("reasoning_stage_disposition_requires_completion")
        existing_commit = self._query_stage_commit_position(
            cycle_ref=cycle_ref, stage=stage, epoch=epoch
        )
        if existing_commit is None and request is not None:
            self._assert_stage_request_current(request)
        if disposition == EXHAUSTED_DISPOSITION:
            assert request is not None
            assert run_ref is not None
            assert run_completion_receipt is not None
            if self._run_completion_verifier is None:
                raise OwnerConflict("run_completion_verifier_unavailable")
            self._run_completion_verifier.verify_run_completion_receipt(
                request_ref=request.request_ref,
                run_ref=run_ref,
                attempt_ref=None,
                outcome_ref=basis_ref,
                receipt=run_completion_receipt,
            )
        if self._stage_disposition_basis_verifier is None:
            raise OwnerConflict("stage_disposition_basis_verifier_unavailable")
        self._stage_disposition_basis_verifier.verify_stage_disposition_basis(
            cycle_ref=cycle_ref,
            quest_ref=quest_ref,
            question_ref=question_ref,
            stage=stage,
            epoch=epoch,
            disposition=disposition,
            basis_kind=basis_kind,
            basis_ref=basis_ref,
            receipt=basis_receipt,
        )
        command_kind = "commit_stage_disposition"
        command_input = {
            "command": command_kind,
            "request_ref": request_ref,
            "cycle_ref": cycle_ref,
            "stage": stage,
            "epoch": epoch,
            "disposition": disposition,
            "basis_kind": basis_kind,
            "basis_ref": basis_ref,
            "basis_receipt": basis_receipt.as_public_dict(),
            "run_ref": run_ref,
            "run_completion_receipt": (
                None
                if run_completion_receipt is None
                else run_completion_receipt.as_public_dict()
            ),
        }
        command_hash = canonical_hash(command_input)
        _query_ae_command(
            self._database,
            idempotency_key,
            command_kind,
            command_hash,
        )
        with self._database.write() as connection:
            replay_ref = _ae_command_replay(
                connection,
                idempotency_key,
                command_kind,
                command_hash,
            )
            if replay_ref is not None:
                replay = connection.execute(
                    text(
                        "SELECT * FROM ae_stage_commits WHERE commit_ref = "
                        ":commit_ref"
                    ),
                    {"commit_ref": replay_ref},
                ).first()
                if replay is None:
                    raise OwnerConflict("stage_command_result_missing")
                return self._stage_commit_from_row(replay)
            existing = connection.execute(
                text(
                    "SELECT * FROM ae_stage_commits WHERE cycle_ref = :cycle_ref AND "
                    "stage = :stage AND epoch = :epoch"
                ),
                {"cycle_ref": cycle_ref, "stage": stage, "epoch": epoch},
            ).first()
            if existing is not None:
                if existing.request_hash != command_hash:
                    raise OwnerConflict("stage_commit_conflict")
                _record_ae_command(
                    connection,
                    idempotency_key,
                    command_kind,
                    command_hash,
                    existing.commit_ref,
                )
                return self._stage_commit_from_row(existing)

            if disposition == SKIPPED_DISPOSITION:
                started = connection.execute(
                    text(
                        "SELECT request_ref FROM ae_stage_run_requests WHERE "
                        "cycle_ref = :cycle_ref AND stage = :stage AND epoch = "
                        ":epoch"
                    ),
                    {"cycle_ref": cycle_ref, "stage": stage, "epoch": epoch},
                ).first()
                if started is not None:
                    raise OwnerConflict(
                        "stage_disposition_execution_already_started"
                    )

            commit_ref = new_ref("stage_commit")
            receipt_ref = new_ref("ae_stage_commit_receipt")
            bindings = {
                "request_ref": request_ref,
                "cycle_ref": cycle_ref,
                "stage": stage,
                "epoch": epoch,
                "disposition": disposition,
                **(
                    {}
                    if disposition == SKIPPED_DISPOSITION
                    else {
                        "run_ref": run_ref,
                        "run_completion_receipt_ref": (
                            run_completion_receipt.receipt_ref
                            if run_completion_receipt is not None
                            else None
                        ),
                        "run_completion_receipt_hash": (
                            run_completion_receipt.payload_hash
                            if run_completion_receipt is not None
                            else None
                        ),
                    }
                ),
                "basis_kind": basis_kind,
                "basis_ref": basis_ref,
                "basis_receipt_issuer": basis_receipt.issuer,
                "basis_receipt_kind": basis_receipt.kind,
                "basis_receipt_subject_ref": basis_receipt.subject_ref,
                "basis_receipt_ref": basis_receipt.receipt_ref,
                "basis_receipt_hash": basis_receipt.payload_hash,
            }
            receipt_hash = _receipt_hash(
                STAGE_COMMIT_RECEIPT_KIND,
                commit_ref,
                bindings,
            )
            connection.execute(
                text(
                    "INSERT INTO ae_stage_commits (commit_ref, request_ref, "
                    "cycle_ref, stage, epoch, run_ref, outcome_ref, outcome_kind, "
                    "disposition, run_completion_receipt_ref, "
                    "run_completion_receipt_hash, outcome_receipt_ref, "
                    "outcome_receipt_hash, basis_kind, basis_ref, "
                    "basis_receipt_issuer, basis_receipt_kind, "
                    "basis_receipt_subject_ref, basis_receipt_ref, "
                    "basis_receipt_hash, idempotency_key, request_hash, "
                    "receipt_ref, receipt_hash, committed_at) VALUES "
                    "(:commit_ref, :request_ref, :cycle_ref, :stage, :epoch, "
                    ":run_ref, NULL, NULL, :disposition, "
                    ":run_completion_receipt_ref, "
                    ":run_completion_receipt_hash, NULL, NULL, "
                    ":basis_kind, :basis_ref, :basis_receipt_issuer, "
                    ":basis_receipt_kind, :basis_receipt_subject_ref, "
                    ":basis_receipt_ref, :basis_receipt_hash, :idempotency_key, "
                    ":request_hash, :receipt_ref, :receipt_hash, :committed_at)"
                ),
                {
                    **bindings,
                    "commit_ref": commit_ref,
                    "run_ref": run_ref,
                    "run_completion_receipt_ref": (
                        None
                        if run_completion_receipt is None
                        else run_completion_receipt.receipt_ref
                    ),
                    "run_completion_receipt_hash": (
                        None
                        if run_completion_receipt is None
                        else run_completion_receipt.payload_hash
                    ),
                    "idempotency_key": idempotency_key,
                    "request_hash": command_hash,
                    "receipt_ref": receipt_ref,
                    "receipt_hash": receipt_hash,
                    "committed_at": time.time(),
                },
            )
            _record_ae_command(
                connection,
                idempotency_key,
                command_kind,
                command_hash,
                commit_ref,
            )
            self._advance_cycle_after_stage_commit(
                connection,
                cycle_ref=cycle_ref,
                quest_ref=quest_ref,
                stage=stage,
                epoch=epoch,
                disposition=disposition,
            )
            connection.execute(
                text(
                    "UPDATE advancement_engine_state SET revision = revision + 1, "
                    "stage_commit_count = stage_commit_count + 1 "
                    "WHERE singleton = 'owner'"
                )
            )
            self._feed.record(
                connection,
                "advancement_engine.stage_committed",
                {
                    "commit_ref": commit_ref,
                    "request_ref": request_ref,
                    "disposition": disposition,
                    "basis_kind": basis_kind,
                    "basis_ref": basis_ref,
                    "stage": stage,
                    "epoch": epoch,
                    "receipt_ref": receipt_ref,
                },
            )
        self._resume_normal_handoff_after_commit(cycle_ref)
        committed = self._query_stage_commit_position(
            cycle_ref=cycle_ref, stage=stage, epoch=epoch
        )
        if committed is None:
            raise OwnerConflict("stage_commit_missing_after_commit")
        return committed

    def query_idea_stage_commit(self, request_ref: str) -> StageCommit | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ae_stage_commits WHERE request_ref = "
                    ":request_ref AND stage = 'idea'"
                ),
                {"request_ref": request_ref},
            ).first()
        if row is None:
            return None
        return self._stage_commit_from_row(row)

    def commit_plan_stage(
        self,
        *,
        request_ref: str,
        run_ref: str,
        formal_plan_ref: str,
        run_completion_receipt: AcceptanceReceipt,
        formal_plan_receipt: AcceptanceReceipt,
        idempotency_key: str,
    ) -> StageCommit:
        _validate_idempotency_key(idempotency_key)
        command_kind = "commit_plan_stage"
        command_input = {
            "command": command_kind,
            "request_ref": request_ref,
            "run_ref": run_ref,
            "outcome_ref": formal_plan_ref,
            "outcome_kind": FORMAL_PLAN_OUTCOME_KIND,
            "disposition": COMPLETED_DISPOSITION,
            "run_completion_receipt": run_completion_receipt.as_public_dict(),
            "outcome_receipt": formal_plan_receipt.as_public_dict(),
        }
        command_hash = canonical_hash(command_input)
        _query_ae_command(
            self._database,
            idempotency_key,
            command_kind,
            command_hash,
        )
        request = self._query_stage_request_by_ref(request_ref)
        if request.stage != PLAN_STAGE:
            raise OwnerConflict("plan_stage_request_invalid")
        if self.query_plan_stage_commit(request_ref) is None:
            self._assert_stage_request_current(request)
        if (
            self._run_completion_verifier is None
            or self._formal_plan_verifier is None
        ):
            raise OwnerConflict("plan_stage_verifier_unavailable")
        self._run_completion_verifier.verify_run_completion_receipt(
            request_ref=request_ref,
            run_ref=run_ref,
            attempt_ref=None,
            outcome_ref=formal_plan_ref,
            receipt=run_completion_receipt,
        )
        self._formal_plan_verifier.verify_formal_plan_decision(
            request_ref=request_ref,
            submission_ref=None,
            decision="accepted",
            formal_plan_ref=formal_plan_ref,
            receipt=formal_plan_receipt,
        )
        with self._database.write() as connection:
            replay_ref = _ae_command_replay(
                connection,
                idempotency_key,
                command_kind,
                command_hash,
            )
            if replay_ref is not None:
                replay = connection.execute(
                    text(
                        "SELECT * FROM ae_stage_commits WHERE commit_ref = "
                        ":commit_ref"
                    ),
                    {"commit_ref": replay_ref},
                ).first()
                if replay is None:
                    raise OwnerConflict("stage_command_result_missing")
                return self._stage_commit_from_row(replay)
            existing = connection.execute(
                text("SELECT * FROM ae_stage_commits WHERE request_ref = :request_ref"),
                {"request_ref": request_ref},
            ).first()
            if existing is not None:
                if existing.request_hash != command_hash:
                    raise OwnerConflict("stage_commit_conflict")
                _record_ae_command(
                    connection,
                    idempotency_key,
                    command_kind,
                    command_hash,
                    existing.commit_ref,
                )
                return self._stage_commit_from_row(existing)

            commit_ref = new_ref("stage_commit")
            receipt_ref = new_ref("ae_stage_commit_receipt")
            bindings = {
                "request_ref": request_ref,
                "cycle_ref": request.cycle_ref,
                "stage": request.stage,
                "epoch": request.epoch,
                "run_ref": run_ref,
                "outcome_ref": formal_plan_ref,
                "outcome_kind": FORMAL_PLAN_OUTCOME_KIND,
                "disposition": COMPLETED_DISPOSITION,
                "run_completion_receipt_ref": run_completion_receipt.receipt_ref,
                "run_completion_receipt_hash": run_completion_receipt.payload_hash,
                "outcome_receipt_ref": formal_plan_receipt.receipt_ref,
                "outcome_receipt_hash": formal_plan_receipt.payload_hash,
            }
            receipt_hash = _receipt_hash(
                STAGE_COMMIT_RECEIPT_KIND,
                commit_ref,
                bindings,
            )
            connection.execute(
                text(
                    "INSERT INTO ae_stage_commits (commit_ref, request_ref, "
                    "cycle_ref, stage, epoch, run_ref, outcome_ref, outcome_kind, "
                    "disposition, run_completion_receipt_ref, "
                    "run_completion_receipt_hash, outcome_receipt_ref, "
                    "outcome_receipt_hash, idempotency_key, request_hash, "
                    "receipt_ref, receipt_hash, committed_at) VALUES "
                    "(:commit_ref, :request_ref, :cycle_ref, :stage, :epoch, "
                    ":run_ref, :outcome_ref, :outcome_kind, :disposition, "
                    ":run_completion_receipt_ref, :run_completion_receipt_hash, "
                    ":outcome_receipt_ref, :outcome_receipt_hash, "
                    ":idempotency_key, :request_hash, :receipt_ref, :receipt_hash, "
                    ":committed_at)"
                ),
                {
                    **bindings,
                    "commit_ref": commit_ref,
                    "idempotency_key": idempotency_key,
                    "request_hash": command_hash,
                    "receipt_ref": receipt_ref,
                    "receipt_hash": receipt_hash,
                    "committed_at": time.time(),
                },
            )
            _record_ae_command(
                connection,
                idempotency_key,
                command_kind,
                command_hash,
                commit_ref,
            )
            self._advance_cycle_after_stage_commit(
                connection,
                cycle_ref=request.cycle_ref,
                quest_ref=request.accepted_question.quest_ref,
                stage=request.stage,
                epoch=request.epoch,
                disposition=COMPLETED_DISPOSITION,
            )
            connection.execute(
                text(
                    "UPDATE advancement_engine_state SET revision = revision + 1, "
                    "stage_commit_count = stage_commit_count + 1 "
                    "WHERE singleton = 'owner'"
                )
            )
            self._feed.record(
                connection,
                "advancement_engine.stage_committed",
                {
                    "commit_ref": commit_ref,
                    "request_ref": request_ref,
                    "run_ref": run_ref,
                    "outcome_ref": formal_plan_ref,
                    "outcome_kind": FORMAL_PLAN_OUTCOME_KIND,
                    "disposition": COMPLETED_DISPOSITION,
                    "stage": PLAN_STAGE,
                    "epoch": request.epoch,
                    "receipt_ref": receipt_ref,
                },
            )
        self._resume_normal_handoff_after_commit(request.cycle_ref)
        committed = self.query_plan_stage_commit(request_ref)
        if committed is None:
            raise OwnerConflict("stage_commit_missing_after_commit")
        return committed

    def query_plan_stage_commit(self, request_ref: str) -> StageCommit | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ae_stage_commits WHERE request_ref = "
                    ":request_ref AND stage = 'plan'"
                ),
                {"request_ref": request_ref},
            ).first()
        if row is None:
            return None
        return self._stage_commit_from_row(row)

    def _query_stage_commit(self, request_ref: str) -> StageCommit | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ae_stage_commits WHERE request_ref = :request_ref"
                ),
                {"request_ref": request_ref},
            ).first()
        if row is None:
            return None
        return self._stage_commit_from_row(row)

    def _query_stage_commit_position(
        self, *, cycle_ref: str, stage: str, epoch: int
    ) -> StageCommit | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ae_stage_commits WHERE cycle_ref = :cycle_ref AND "
                    "stage = :stage AND epoch = :epoch"
                ),
                {"cycle_ref": cycle_ref, "stage": stage, "epoch": epoch},
            ).first()
        if row is None:
            return None
        return self._stage_commit_from_row(row)

    def _resume_normal_handoff_after_commit(self, cycle_ref: str) -> None:
        with self._database.read() as connection:
            operation = connection.execute(
                text(
                    "SELECT * FROM ae_control_operations WHERE source_cycle_ref = "
                    ":cycle_ref AND action = 'normal_switch' AND status = "
                    "'handoff_pending' ORDER BY created_at LIMIT 1"
                ),
                {"cycle_ref": cycle_ref},
            ).first()
        if operation is None:
            return
        runtime_receipt = decoded_object(operation.runtime_receipt_json)
        self.complete_foreground_control(
            operation_ref=operation.operation_ref,
            runtime_receipt=runtime_receipt,
            graph_receipt=None,
            idempotency_key=(
                "normal-handoff-complete-"
                + canonical_hash({"operation_ref": operation.operation_ref})[:48]
            ),
        )

    def _advance_cycle_after_stage_commit(
        self,
        connection,
        *,
        cycle_ref: str,
        quest_ref: str,
        stage: str,
        epoch: int,
        disposition: str,
        outcome_kind: str | None = None,
    ) -> None:
        now = time.time()
        next_stage = (
            "reasoning"
            if disposition == EXHAUSTED_DISPOSITION
            or (
                stage == IDEA_STAGE
                and disposition == COMPLETED_DISPOSITION
                and outcome_kind == NO_VIABLE_CANDIDATE_OUTCOME_KIND
            )
            else NEXT_STAGE.get(stage)
        )
        if next_stage is None:
            advanced = connection.execute(
                text(
                    "UPDATE ae_foreground_heads SET status = 'completed', "
                    "updated_at = :now WHERE quest_ref = :quest_ref AND cycle_ref = "
                    ":cycle_ref AND epoch = :epoch AND stage = :stage AND status = "
                    "'active'"
                ),
                {
                    "now": now,
                    "quest_ref": quest_ref,
                    "cycle_ref": cycle_ref,
                    "epoch": epoch,
                    "stage": stage,
                },
            )
            if advanced.rowcount != 1:
                raise OwnerConflict("stage_request_epoch_revoked")
            connection.execute(
                text(
                    "UPDATE ae_foreground_grants SET status = 'completed', "
                    "revoked_at = COALESCE(revoked_at, :now) WHERE quest_ref = "
                    ":quest_ref AND cycle_ref = :cycle_ref AND epoch = :epoch AND "
                    "status = 'active'"
                ),
                {
                    "now": now,
                    "quest_ref": quest_ref,
                    "cycle_ref": cycle_ref,
                    "epoch": epoch,
                },
            )
            connection.execute(
                text(
                    "UPDATE ae_cycles SET status = 'completed', suspension_reason = "
                    "NULL, updated_at = :now WHERE cycle_ref = :cycle_ref AND status "
                    "= 'ongoing'"
                ),
                {"now": now, "cycle_ref": cycle_ref},
            )
            return

        advanced = connection.execute(
            text(
                "UPDATE ae_foreground_heads SET stage = :next_stage, updated_at = "
                ":now WHERE quest_ref = :quest_ref AND cycle_ref = :cycle_ref AND "
                "epoch = :epoch AND stage = :stage AND status = 'active'"
            ),
            {
                "next_stage": next_stage,
                "now": now,
                "quest_ref": quest_ref,
                "cycle_ref": cycle_ref,
                "epoch": epoch,
                "stage": stage,
            },
        )
        if advanced.rowcount != 1:
            raise OwnerConflict("stage_request_epoch_revoked")
        connection.execute(
            text(
                "UPDATE ae_foreground_grants SET stage = :next_stage WHERE "
                "quest_ref = :quest_ref AND cycle_ref = :cycle_ref AND epoch = "
                ":epoch AND status = 'active'"
            ),
            {
                "next_stage": next_stage,
                "quest_ref": quest_ref,
                "cycle_ref": cycle_ref,
                "epoch": epoch,
            },
        )
        connection.execute(
            text(
                "UPDATE ae_cycles SET stage = :next_stage, suspension_reason = NULL, "
                "updated_at = :now WHERE cycle_ref = :cycle_ref AND status = "
                "'ongoing'"
            ),
            {
                "next_stage": next_stage,
                "now": now,
                "cycle_ref": cycle_ref,
            },
        )

    def _stage_commit_from_row(self, row) -> StageCommit:
        committed = _stage_commit(row)
        if row.receipt_hash != _stage_commit_receipt_hash(row):
            raise OwnerConflict("stage_commit_receipt_invalid")
        if (
            row.disposition in {COMPLETED_DISPOSITION, EXHAUSTED_DISPOSITION}
            and self._run_completion_verifier is not None
        ):
            self._run_completion_verifier.verify_run_completion_receipt(
                request_ref=row.request_ref,
                run_ref=row.run_ref,
                attempt_ref=None,
                outcome_ref=(
                    row.outcome_ref
                    if row.disposition == COMPLETED_DISPOSITION
                    else row.basis_ref
                ),
                receipt=committed.run_completion_receipt,
            )
        if (
            row.disposition == COMPLETED_DISPOSITION
            and row.stage == IDEA_STAGE
            and self._outcome_verifier is not None
        ):
            self._outcome_verifier.verify_idea_outcome_decision(
                request_ref=row.request_ref,
                submission_ref=None,
                decision="accepted",
                outcome_ref=row.outcome_ref,
                outcome_kind=row.outcome_kind,
                receipt=committed.outcome_receipt,
            )
        elif (
            row.disposition == COMPLETED_DISPOSITION
            and row.stage == PLAN_STAGE
            and self._formal_plan_verifier is not None
        ):
            self._formal_plan_verifier.verify_formal_plan_decision(
                request_ref=row.request_ref,
                submission_ref=None,
                decision="accepted",
                formal_plan_ref=row.outcome_ref,
                receipt=committed.outcome_receipt,
            )
        elif row.disposition in BASIS_DISPOSITIONS:
            if self._stage_disposition_basis_verifier is None:
                raise OwnerConflict("stage_disposition_basis_verifier_unavailable")
            if committed.basis_receipt is None:
                raise OwnerConflict("stage_commit_basis_invalid")
            with self._database.read() as connection:
                cycle = connection.execute(
                    text(
                        "SELECT quest_ref, question_ref FROM ae_cycles WHERE "
                        "cycle_ref = :cycle_ref"
                    ),
                    {"cycle_ref": row.cycle_ref},
                ).first()
            if cycle is None:
                raise OwnerConflict("stage_commit_basis_invalid")
            self._stage_disposition_basis_verifier.verify_stage_disposition_basis(
                cycle_ref=row.cycle_ref,
                quest_ref=cycle.quest_ref,
                question_ref=cycle.question_ref,
                stage=row.stage,
                epoch=int(row.epoch),
                disposition=row.disposition,
                basis_kind=row.basis_kind,
                basis_ref=row.basis_ref,
                receipt=committed.basis_receipt,
            )
        return committed

    def _query_stage_request_by_ref(self, request_ref: str) -> StageRunRequest:
        with self._database.read() as connection:
            row = connection.execute(
                text("SELECT * FROM ae_stage_run_requests WHERE request_ref = :request_ref"),
                {"request_ref": request_ref},
            ).first()
        if row is None:
            raise OwnerConflict("stage_run_request_not_found")
        return self._stage_request_from_row(row)

    def _assert_stage_request_current(self, request: StageRunRequest) -> None:
        with self._database.read() as connection:
            head = connection.execute(
                text(
                    "SELECT head.*, operation.action AS pending_action FROM "
                    "ae_foreground_heads head LEFT JOIN ae_control_operations "
                    "operation ON operation.operation_ref = "
                    "head.pending_operation_ref WHERE head.cycle_ref = :cycle_ref"
                ),
                {"cycle_ref": request.cycle_ref},
            ).first()
        if head is None or (
            int(head.epoch) != request.epoch
            or head.stage != request.stage
            or head.status != "active"
            or (
                head.pending_operation_ref is not None
                and head.pending_action != "normal_switch"
            )
        ):
            raise OwnerConflict("stage_request_epoch_revoked")

    def verify_stage_run_request(self, **values) -> None:
        self._stage_request_verifier.verify_stage_run_request(**values)


class SQLiteAdvancementEngineReceiptVerifier:
    """Narrow AE issuer verifier used by Agent Runtime."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def verify_stage_run_request(
        self,
        *,
        request_ref: str,
        cycle_ref: str,
        epoch: int,
        context_pack_ref: str,
        context_pack_hash: str,
        receipt: AcceptanceReceipt,
    ) -> None:
        if (
            receipt.issuer != AE_OWNER
            or receipt.kind != STAGE_REQUEST_RECEIPT_KIND
            or receipt.subject_ref != request_ref
        ):
            raise OwnerConflict("stage_run_request_receipt_issuer_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ae_stage_run_requests WHERE request_ref = "
                    ":request_ref"
                ),
                {"request_ref": request_ref},
            ).first()
        if row is None or (
            row.cycle_ref != cycle_ref
            or int(row.epoch) != epoch
            or row.context_pack_ref != context_pack_ref
            or row.context_pack_hash != context_pack_hash
            or row.receipt_ref != receipt.receipt_ref
            or row.receipt_hash != receipt.payload_hash
        ):
            raise OwnerConflict("stage_run_request_receipt_invalid")
        _verify_stage_request_integrity(row)

    def verify_current_stage_run_request(
        self,
        *,
        request_ref: str,
        cycle_ref: str,
        epoch: int,
        context_pack_ref: str,
        context_pack_hash: str,
        receipt: AcceptanceReceipt,
    ) -> None:
        """Verify immutable issuance plus the current Foreground Grant.

        Historical consumers use ``verify_stage_run_request``. Admission is a
        new effect, so AR uses this stricter seam and cannot revive a request
        whose Cycle/Epoch has since lost its grant.
        """

        self.verify_stage_run_request(
            request_ref=request_ref,
            cycle_ref=cycle_ref,
            epoch=epoch,
            context_pack_ref=context_pack_ref,
            context_pack_hash=context_pack_hash,
            receipt=receipt,
        )
        with self._database.read() as connection:
            current = connection.execute(
                text(
                    "SELECT h.pending_operation_ref, h.status AS head_status, "
                    "g.status AS grant_status, c.status AS cycle_status, "
                    "operation.action AS pending_action FROM ae_stage_run_requests r "
                    "JOIN ae_foreground_heads h ON h.cycle_ref = r.cycle_ref AND "
                    "h.stage = r.stage AND h.epoch = r.epoch JOIN "
                    "ae_foreground_grants g ON g.quest_ref = h.quest_ref AND "
                    "g.cycle_ref = h.cycle_ref AND g.epoch = h.epoch JOIN ae_cycles c "
                    "ON c.cycle_ref = h.cycle_ref LEFT JOIN ae_control_operations "
                    "operation ON operation.operation_ref = h.pending_operation_ref "
                    "WHERE r.request_ref = :request_ref"
                ),
                {"request_ref": request_ref},
            ).first()
        if current is None or (
            current.head_status != "active"
            or current.grant_status != "active"
            or current.cycle_status != "ongoing"
            or (
                current.pending_operation_ref is not None
                and current.pending_action != "normal_switch"
            )
        ):
            raise OwnerConflict("stage_run_request_not_current")

    def verify_idea_stage_request_binding(
        self,
        *,
        request_ref: str,
        accepted_question: AcceptedQuestionBinding,
        context_pack_ref: str,
    ) -> VerifiedStageRunRequestBinding:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ae_stage_run_requests WHERE request_ref = "
                    ":request_ref"
                ),
                {"request_ref": request_ref},
            ).first()
        if row is None:
            raise OwnerConflict("stage_run_request_receipt_invalid")
        requested = _stage_request(row)
        if (
            requested.accepted_question != accepted_question
            or requested.context_pack_ref != context_pack_ref
        ):
            raise OwnerConflict("stage_run_request_binding_invalid")
        self.verify_stage_run_request(
            request_ref=requested.request_ref,
            cycle_ref=requested.cycle_ref,
            epoch=requested.epoch,
            context_pack_ref=requested.context_pack_ref,
            context_pack_hash=requested.context_pack_hash,
            receipt=requested.receipt,
        )
        return VerifiedStageRunRequestBinding(
            request_ref=requested.request_ref,
            cycle_ref=requested.cycle_ref,
            epoch=requested.epoch,
            accepted_question=requested.accepted_question,
            context_pack_ref=requested.context_pack_ref,
            context_pack_hash=requested.context_pack_hash,
            context_pack=requested.context_pack,
            receipt=requested.receipt,
        )

    def verify_plan_stage_request_binding(
        self,
        *,
        request_ref: str,
        accepted_question: AcceptedQuestionBinding,
        accepted_idea_set: AcceptedIdeaSetBinding,
        context_pack_ref: str,
    ) -> VerifiedStageRunRequestBinding:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ae_stage_run_requests WHERE request_ref = "
                    ":request_ref AND stage = 'plan'"
                ),
                {"request_ref": request_ref},
            ).first()
        if row is None:
            raise OwnerConflict("stage_run_request_receipt_invalid")
        requested = _stage_request(row)
        if (
            requested.accepted_question != accepted_question
            or requested.accepted_idea_set != accepted_idea_set
            or requested.context_pack_ref != context_pack_ref
        ):
            raise OwnerConflict("stage_run_request_binding_invalid")
        self.verify_stage_run_request(
            request_ref=requested.request_ref,
            cycle_ref=requested.cycle_ref,
            epoch=requested.epoch,
            context_pack_ref=requested.context_pack_ref,
            context_pack_hash=requested.context_pack_hash,
            receipt=requested.receipt,
        )
        return VerifiedStageRunRequestBinding(
            request_ref=requested.request_ref,
            cycle_ref=requested.cycle_ref,
            epoch=requested.epoch,
            accepted_question=requested.accepted_question,
            accepted_idea_set=requested.accepted_idea_set,
            context_pack_ref=requested.context_pack_ref,
            context_pack_hash=requested.context_pack_hash,
            context_pack=requested.context_pack,
            receipt=requested.receipt,
        )

    def query_verified_plan_stage_request(
        self,
        *,
        request_ref: str,
        context_pack_ref: str,
    ) -> VerifiedStageRunRequestBinding:
        """Return an issuer-verified Plan request without exposing AE storage.

        Downstream Owners use this read seam to compare their independently
        persisted closure.  They must never inspect ``ae_stage_run_requests``
        directly or treat a caller-supplied copy as AE truth.
        """

        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ae_stage_run_requests WHERE request_ref = "
                    ":request_ref AND stage = 'plan'"
                ),
                {"request_ref": request_ref},
            ).first()
        if row is None:
            raise OwnerConflict("stage_run_request_receipt_invalid")
        requested = _stage_request(row)
        if (
            requested.context_pack_ref != context_pack_ref
            or requested.accepted_idea_set is None
        ):
            raise OwnerConflict("stage_run_request_binding_invalid")
        self.verify_stage_run_request(
            request_ref=requested.request_ref,
            cycle_ref=requested.cycle_ref,
            epoch=requested.epoch,
            context_pack_ref=requested.context_pack_ref,
            context_pack_hash=requested.context_pack_hash,
            receipt=requested.receipt,
        )
        return VerifiedStageRunRequestBinding(
            request_ref=requested.request_ref,
            cycle_ref=requested.cycle_ref,
            epoch=requested.epoch,
            accepted_question=requested.accepted_question,
            accepted_idea_set=requested.accepted_idea_set,
            context_pack_ref=requested.context_pack_ref,
            context_pack_hash=requested.context_pack_hash,
            context_pack=requested.context_pack,
            receipt=requested.receipt,
        )


def _control_ref(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise OwnerConflict(f"{field}_invalid")
    return value


def _question_cycle_ref(question_ref: str) -> str:
    return f"cycle_question_{canonical_hash({'question_ref': question_ref})[:40]}"


def _foreground_row_dict(row) -> dict[str, object]:
    return {
        "quest_ref": row.quest_ref,
        "cycle_ref": row.cycle_ref,
        "question_ref": row.question_ref,
        "stage": row.stage,
        "epoch": int(row.epoch),
        "status": row.status,
        "pending_operation_ref": row.pending_operation_ref,
    }


def _foreground_query_document(row) -> dict[str, object]:
    if row.grant_status not in {
        "active",
        "suspended",
        "revoked",
        "completed",
        "cancelled",
        "abandoned",
        "pruned",
    }:
        raise OwnerConflict("foreground_grant_invalid")
    return {
        **_foreground_row_dict(row),
        "grant_ref": row.grant_ref,
        "grant_status": row.grant_status,
        "safe_point_ref": row.safe_point_ref,
        "owner_revision": int(row.owner_revision),
    }


def _assert_foreground_target(
    foreground: dict[str, object], target: dict[str, object]
) -> None:
    if (
        foreground.get("quest_ref") != target.get("quest_ref")
        or foreground.get("cycle_ref") != target.get("cycle_ref")
        or foreground.get("question_ref") != target.get("question_ref")
        or foreground.get("epoch") != target.get("epoch")
    ):
        raise OwnerConflict("research_control_target_stale")


def _assert_foreground_action(
    action: str,
    foreground: dict[str, object],
    *,
    allow_pending_normal_override: bool = False,
) -> None:
    status = foreground.get("status")
    pending = foreground.get("pending_operation_ref")
    if pending is not None and not allow_pending_normal_override:
        raise OwnerConflict("foreground_control_in_progress")
    allowed = {
        "pause": {"active"},
        "resume": {"suspended"},
        "normal_switch": {"active"},
        "forced_switch": {"active", "suspended"},
        "cancel": {"active", "suspended"},
        "abandon": {"active", "suspended", "cancelled"},
        "prune": {"active", "suspended", "pruned"},
        "restore": {"active", "suspended", "pruned"},
    }
    if status not in allowed[action]:
        if status in {"cancelled", "abandoned"}:
            raise OwnerConflict("foreground_cycle_terminal")
        raise OwnerConflict("foreground_control_transition_invalid")


def _receipt_hash(kind: str, subject_ref: str, bindings: dict[str, object]) -> str:
    return canonical_hash(
        {
            "schema_ref": RECEIPT_SCHEMA,
            "issuer": AE_OWNER,
            "kind": kind,
            "subject_ref": subject_ref,
            "bindings": bindings,
        }
    )


def _cycle_receipt_hash(row) -> str:
    return _receipt_hash(
        CYCLE_RECEIPT_KIND,
        row.cycle_ref,
        {
            "initialization_id": row.initialization_id,
            "quest_ref": row.quest_ref,
            "question_ref": row.question_ref,
            "question_receipt_ref": row.question_receipt_ref,
            "question_receipt_hash": row.question_receipt_hash,
            "quest_receipt_ref": row.quest_receipt_ref,
            "quest_receipt_hash": row.quest_receipt_hash,
        },
    )


def _activated_cycle(row) -> ActivatedCycle:
    return ActivatedCycle(
        row.cycle_ref,
        AcceptanceReceipt(
            issuer=AE_OWNER,
            kind=CYCLE_RECEIPT_KIND,
            receipt_ref=row.receipt_ref,
            subject_ref=row.cycle_ref,
            payload_hash=row.receipt_hash,
        ),
    )


def _question_binding_columns(binding: AcceptedQuestionBinding) -> dict[str, object]:
    return {
        "initialization_id": binding.initialization_id,
        "quest_ref": binding.quest_ref,
        "question_ref": binding.question_ref,
        "content_ref": binding.content_ref,
        "content_hash": binding.content_hash,
        "schema_ref": binding.schema_ref,
        "content_receipt_ref": binding.content_receipt.receipt_ref,
        "content_receipt_hash": binding.content_receipt.payload_hash,
        "question_receipt_ref": binding.question_receipt.receipt_ref,
        "question_receipt_hash": binding.question_receipt.payload_hash,
    }


def _question_binding(
    row, context_pack: dict[str, object] | None = None
) -> AcceptedQuestionBinding:
    if context_pack is not None:
        value = context_pack.get("accepted_question_binding")
        if not isinstance(value, dict):
            raise OwnerConflict("stage_run_request_invalid")
        try:
            binding = AcceptedQuestionBinding(
                initialization_id=str(value["initialization_id"]),
                quest_ref=str(value["quest_ref"]),
                question_ref=str(value["question_ref"]),
                content_ref=str(value["content_ref"]),
                content_hash=str(value["content_hash"]),
                schema_ref=str(value["schema_ref"]),
                content_receipt=_receipt_from_public(value["content_receipt"]),
                question_receipt=_receipt_from_public(value["question_receipt"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise OwnerConflict("stage_run_request_invalid") from error
        expected_columns = _question_binding_columns(binding)
        if any(getattr(row, name) != expected for name, expected in expected_columns.items()):
            raise OwnerConflict("stage_run_request_invalid")
        return binding
    return AcceptedQuestionBinding(
        initialization_id=row.initialization_id,
        quest_ref=row.quest_ref,
        question_ref=row.question_ref,
        content_ref=row.content_ref,
        content_hash=row.content_hash,
        schema_ref=row.schema_ref,
        content_receipt=AcceptanceReceipt(
            issuer="research_memory",
            kind="question_content_acceptance",
            receipt_ref=row.content_receipt_ref,
            subject_ref=row.content_ref,
            payload_hash=row.content_receipt_hash,
        ),
        question_receipt=AcceptanceReceipt(
            issuer="research_graph",
            kind="root_question_acceptance",
            receipt_ref=row.question_receipt_ref,
            subject_ref=row.question_ref,
            payload_hash=row.question_receipt_hash,
        ),
    )


def _stage_request_bindings(
    row, binding: AcceptedQuestionBinding | None = None
) -> dict[str, object]:
    return {
        **_question_binding_columns(binding or _question_binding(row)),
        "cycle_ref": row.cycle_ref,
        "stage": row.stage,
        "epoch": int(row.epoch),
        "context_pack_ref": row.context_pack_ref,
        "context_pack_hash": row.context_pack_hash,
    }


def _stage_request_receipt_hash(
    row, binding: AcceptedQuestionBinding | None = None
) -> str:
    return _receipt_hash(
        STAGE_REQUEST_RECEIPT_KIND,
        row.request_ref,
        _stage_request_bindings(row, binding),
    )


def _verify_stage_request_integrity(
    row,
) -> tuple[dict[str, object], AcceptedQuestionBinding]:
    try:
        context_pack = decoded_object(row.context_pack_json)
    except (TypeError, ValueError) as error:
        raise OwnerConflict("stage_run_request_invalid") from error
    binding = _question_binding(row, context_pack)
    try:
        if row.stage == IDEA_STAGE:
            validate_idea_context_pack(
                context_pack,
                cycle_ref=row.cycle_ref,
                accepted_question_binding=binding.as_dict(),
            )
        elif row.stage == PLAN_STAGE:
            validate_plan_context_pack(
                context_pack,
                cycle_ref=row.cycle_ref,
                accepted_question_binding=binding.as_dict(),
            )
        else:
            raise OwnerConflict("stage_run_request_invalid")
    except (IdeaContractError, PlanContractError) as error:
        raise OwnerConflict(str(error)) from error
    accepted_idea_set = (
        None
        if row.stage == IDEA_STAGE
        else _idea_set_binding_from_context(context_pack)
    )
    command = (
        "ensure_idea_stage_request"
        if row.stage == IDEA_STAGE
        else "ensure_plan_stage_request"
    )
    expected_request_hash = canonical_hash(
        {
            "command": command,
            "cycle_ref": row.cycle_ref,
            "stage": row.stage,
            "epoch": int(row.epoch),
            "accepted_question": binding.as_dict(),
            **(
                {}
                if accepted_idea_set is None
                else {"accepted_idea_set": accepted_idea_set.as_dict()}
            ),
            "context_pack_hash": row.context_pack_hash,
        }
    )
    if (
        row.stage not in {IDEA_STAGE, PLAN_STAGE}
        or int(row.epoch) < 1
        or canonical_hash(context_pack) != row.context_pack_hash
        or canonical_json(context_pack) != row.context_pack_json
        or row.request_hash != expected_request_hash
        or row.receipt_hash != _stage_request_receipt_hash(row, binding)
    ):
        raise OwnerConflict("stage_run_request_invalid")
    return context_pack, binding


def _stage_request(row) -> StageRunRequest:
    context_pack, accepted_question = _verify_stage_request_integrity(row)
    return StageRunRequest(
        request_ref=row.request_ref,
        cycle_ref=row.cycle_ref,
        stage=row.stage,
        epoch=int(row.epoch),
        context_pack_ref=row.context_pack_ref,
        context_pack_hash=row.context_pack_hash,
        context_pack=context_pack,
        accepted_question=accepted_question,
        receipt=AcceptanceReceipt(
            issuer=AE_OWNER,
            kind=STAGE_REQUEST_RECEIPT_KIND,
            receipt_ref=row.receipt_ref,
            subject_ref=row.request_ref,
            payload_hash=row.receipt_hash,
        ),
        accepted_idea_set=(
            None
            if row.stage == IDEA_STAGE
            else _idea_set_binding_from_context(context_pack)
        ),
    )


def _idea_set_binding_from_context(
    context_pack: dict[str, object],
) -> AcceptedIdeaSetBinding:
    value = context_pack.get("accepted_idea_set_binding")
    if not isinstance(value, dict):
        raise OwnerConflict("plan_idea_set_binding_invalid")
    try:
        content_receipt = _receipt_from_public(value["content_receipt"])
        outcome_receipt = _receipt_from_public(value["outcome_receipt"])
        stage_commit_receipt = _receipt_from_public(value["stage_commit_receipt"])
        idea_set = value["idea_set"]
        if not isinstance(idea_set, dict):
            raise TypeError("idea_set")
        return AcceptedIdeaSetBinding(
            outcome_ref=str(value["outcome_ref"]),
            outcome_kind=str(value["outcome_kind"]),
            content_ref=str(value["content_ref"]),
            payload_hash=str(value["payload_hash"]),
            outcome_hash=str(value["outcome_hash"]),
            content_receipt=content_receipt,
            outcome_receipt=outcome_receipt,
            stage_commit_ref=str(value["stage_commit_ref"]),
            stage_commit_receipt=stage_commit_receipt,
            idea_set=idea_set,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise OwnerConflict("plan_idea_set_binding_invalid") from error


def _receipt_from_public(value: object) -> AcceptanceReceipt:
    if not isinstance(value, dict) or value.get("status") != "accepted":
        raise TypeError("receipt")
    return AcceptanceReceipt(
        issuer=str(value["issuer"]),
        kind=str(value["kind"]),
        receipt_ref=str(value["receipt_ref"]),
        subject_ref=str(value["subject_ref"]),
        payload_hash=str(value["payload_hash"]),
    )


def _stage_commit_bindings(row) -> dict[str, object]:
    common = {
        "request_ref": row.request_ref,
        "cycle_ref": row.cycle_ref,
        "stage": row.stage,
        "epoch": int(row.epoch),
        "disposition": row.disposition,
    }
    if row.disposition == SKIPPED_DISPOSITION:
        return {
            **common,
            "basis_kind": row.basis_kind,
            "basis_ref": row.basis_ref,
            "basis_receipt_issuer": row.basis_receipt_issuer,
            "basis_receipt_kind": row.basis_receipt_kind,
            "basis_receipt_subject_ref": row.basis_receipt_subject_ref,
            "basis_receipt_ref": row.basis_receipt_ref,
            "basis_receipt_hash": row.basis_receipt_hash,
        }
    if row.disposition == EXHAUSTED_DISPOSITION:
        return {
            **common,
            "run_ref": row.run_ref,
            "run_completion_receipt_ref": row.run_completion_receipt_ref,
            "run_completion_receipt_hash": row.run_completion_receipt_hash,
            "basis_kind": row.basis_kind,
            "basis_ref": row.basis_ref,
            "basis_receipt_issuer": row.basis_receipt_issuer,
            "basis_receipt_kind": row.basis_receipt_kind,
            "basis_receipt_subject_ref": row.basis_receipt_subject_ref,
            "basis_receipt_ref": row.basis_receipt_ref,
            "basis_receipt_hash": row.basis_receipt_hash,
        }
    return {
        "request_ref": row.request_ref,
        "cycle_ref": row.cycle_ref,
        "stage": row.stage,
        "epoch": int(row.epoch),
        "run_ref": row.run_ref,
        "outcome_ref": row.outcome_ref,
        "outcome_kind": row.outcome_kind,
        "disposition": row.disposition,
        "run_completion_receipt_ref": row.run_completion_receipt_ref,
        "run_completion_receipt_hash": row.run_completion_receipt_hash,
        "outcome_receipt_ref": row.outcome_receipt_ref,
        "outcome_receipt_hash": row.outcome_receipt_hash,
    }


def _stage_commit_receipt_hash(row) -> str:
    return _receipt_hash(
        STAGE_COMMIT_RECEIPT_KIND, row.commit_ref, _stage_commit_bindings(row)
    )


def _stage_commit(row) -> StageCommit:
    if row.stage not in STAGES:
        raise OwnerConflict("stage_commit_disposition_invalid")
    if row.disposition == COMPLETED_DISPOSITION:
        valid_kind = (
            row.stage == IDEA_STAGE
            and row.outcome_kind in COMPLETABLE_IDEA_OUTCOME_KINDS
        ) or (
            row.stage == PLAN_STAGE and row.outcome_kind == FORMAL_PLAN_OUTCOME_KIND
        ) or (
            row.stage in {"bundle", "reasoning"}
            and isinstance(row.outcome_kind, str)
            and bool(row.outcome_kind)
        )
        if (
            not valid_kind
            or not row.request_ref
            or not row.run_ref
            or not row.outcome_ref
            or not row.run_completion_receipt_ref
            or not row.run_completion_receipt_hash
            or not row.outcome_receipt_ref
            or not row.outcome_receipt_hash
        ):
            raise OwnerConflict("stage_commit_disposition_invalid")
        run_completion_receipt = AcceptanceReceipt(
            issuer="agent_runtime",
            kind="run_execution_completed",
            receipt_ref=row.run_completion_receipt_ref,
            subject_ref=row.run_ref,
            payload_hash=row.run_completion_receipt_hash,
        )
        outcome_receipt = AcceptanceReceipt(
            issuer="research_graph",
            kind=(
                "idea_outcome_accepted"
                if row.stage == IDEA_STAGE
                else (
                    "formal_plan_accepted"
                    if row.stage == PLAN_STAGE
                    else f"{row.outcome_kind}_accepted"
                )
            ),
            receipt_ref=row.outcome_receipt_ref,
            subject_ref=row.outcome_ref,
            payload_hash=row.outcome_receipt_hash,
        )
        basis_receipt = None
    elif row.disposition == SKIPPED_DISPOSITION:
        if (
            row.request_ref is not None
            or row.run_ref is not None
            or row.run_completion_receipt_ref is not None
            or row.run_completion_receipt_hash is not None
            or row.outcome_ref is not None
            or row.outcome_kind is not None
            or not row.basis_kind
            or not row.basis_ref
            or not row.basis_receipt_issuer
            or not row.basis_receipt_kind
            or not row.basis_receipt_subject_ref
            or not row.basis_receipt_ref
            or not row.basis_receipt_hash
        ):
            raise OwnerConflict("stage_commit_disposition_invalid")
        run_completion_receipt = None
        outcome_receipt = None
        basis_receipt = AcceptanceReceipt(
            issuer=row.basis_receipt_issuer,
            kind=row.basis_receipt_kind,
            receipt_ref=row.basis_receipt_ref,
            subject_ref=row.basis_receipt_subject_ref,
            payload_hash=row.basis_receipt_hash,
        )
    elif row.disposition == EXHAUSTED_DISPOSITION:
        if (
            not row.request_ref
            or not row.run_ref
            or not row.run_completion_receipt_ref
            or not row.run_completion_receipt_hash
            or row.outcome_ref is not None
            or row.outcome_kind is not None
            or row.outcome_receipt_ref is not None
            or row.outcome_receipt_hash is not None
            or not row.basis_kind
            or not row.basis_ref
            or not row.basis_receipt_issuer
            or not row.basis_receipt_kind
            or not row.basis_receipt_subject_ref
            or not row.basis_receipt_ref
            or not row.basis_receipt_hash
        ):
            raise OwnerConflict("stage_commit_disposition_invalid")
        run_completion_receipt = AcceptanceReceipt(
            issuer="agent_runtime",
            kind="run_execution_completed",
            receipt_ref=row.run_completion_receipt_ref,
            subject_ref=row.run_ref,
            payload_hash=row.run_completion_receipt_hash,
        )
        outcome_receipt = None
        basis_receipt = AcceptanceReceipt(
            issuer=row.basis_receipt_issuer,
            kind=row.basis_receipt_kind,
            receipt_ref=row.basis_receipt_ref,
            subject_ref=row.basis_receipt_subject_ref,
            payload_hash=row.basis_receipt_hash,
        )
    else:
        raise OwnerConflict("stage_commit_disposition_invalid")
    return StageCommit(
        commit_ref=row.commit_ref,
        request_ref=row.request_ref,
        cycle_ref=row.cycle_ref,
        stage=row.stage,
        epoch=int(row.epoch),
        run_ref=row.run_ref,
        outcome_ref=row.outcome_ref,
        outcome_kind=row.outcome_kind,
        disposition=row.disposition,
        run_completion_receipt=run_completion_receipt,
        outcome_receipt=outcome_receipt,
        basis_kind=row.basis_kind,
        basis_ref=row.basis_ref,
        basis_receipt=basis_receipt,
        receipt=AcceptanceReceipt(
            issuer=AE_OWNER,
            kind=STAGE_COMMIT_RECEIPT_KIND,
            receipt_ref=row.receipt_ref,
            subject_ref=row.commit_ref,
            payload_hash=row.receipt_hash,
        ),
    )


def _ae_command_replay(
    connection,
    idempotency_key: str,
    command_kind: str,
    request_hash: str,
) -> str | None:
    row = connection.execute(
        text(
            "SELECT * FROM ae_stage_commands WHERE idempotency_key = "
            ":idempotency_key"
        ),
        {"idempotency_key": idempotency_key},
    ).first()
    if row is None:
        return None
    if row.command_kind != command_kind or row.request_hash != request_hash:
        raise OwnerConflict("idempotency_conflict")
    return row.result_ref


def _query_ae_command(
    database: Database,
    idempotency_key: str,
    command_kind: str,
    request_hash: str,
) -> str | None:
    with database.read() as connection:
        return _ae_command_replay(
            connection,
            idempotency_key,
            command_kind,
            request_hash,
        )


def _record_ae_command(
    connection,
    idempotency_key: str,
    command_kind: str,
    request_hash: str,
    result_ref: str,
) -> None:
    connection.execute(
        text(
            "INSERT INTO ae_stage_commands (idempotency_key, command_kind, "
            "request_hash, result_ref, recorded_at) VALUES (:idempotency_key, "
            ":command_kind, :request_hash, :result_ref, :recorded_at)"
        ),
        {
            "idempotency_key": idempotency_key,
            "command_kind": command_kind,
            "request_hash": request_hash,
            "result_ref": result_ref,
            "recorded_at": time.time(),
        },
    )


def _validate_idempotency_key(value: str) -> None:
    if not value or len(value) > 128:
        raise OwnerConflict("idempotency_key_invalid")


def create_advancement_engine_receipt_verifier(
    database: Database,
) -> SQLiteAdvancementEngineReceiptVerifier:
    return SQLiteAdvancementEngineReceiptVerifier(database)


def create_advancement_engine_interface(
    database: Database,
    feed: DurableFeed,
    quest_verifier: QuestReceiptVerifier,
    question_verifier: RootQuestionReceiptVerifier,
    accepted_question_verifier: AcceptedQuestionBindingVerifier | None = None,
    evidence_verifier: EvidenceRefVerifier | None = None,
    run_completion_verifier: RunCompletionReceiptVerifier | None = None,
    outcome_verifier: IdeaOutcomeDecisionVerifier | None = None,
    formal_plan_verifier: FormalPlanDecisionVerifier | None = None,
    literature_snapshot_verifier: LiteratureSnapshotVerifier | None = None,
    human_response_verifier: HumanResponseVerifier | None = None,
    runtime_control_verifier: RuntimeControlReceiptVerifier | None = None,
    question_control_verifier: QuestionControlReceiptVerifier | None = None,
    stage_disposition_basis_verifier: StageDispositionBasisVerifier | None = None,
    current_question_verifier: CurrentQuestionVerifier | None = None,
) -> AdvancementEngineInterface:
    return SQLiteAdvancementEngine(
        database,
        feed,
        quest_verifier,
        question_verifier,
        accepted_question_verifier,
        evidence_verifier,
        run_completion_verifier,
        outcome_verifier,
        formal_plan_verifier,
        literature_snapshot_verifier,
        human_response_verifier,
        runtime_control_verifier,
        question_control_verifier,
        stage_disposition_basis_verifier,
        current_question_verifier,
    )
