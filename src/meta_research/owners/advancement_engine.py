from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import text

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
COMPLETABLE_IDEA_OUTCOME_KINDS = {
    IDEA_SET_OUTCOME_KIND,
    NO_VIABLE_CANDIDATE_OUTCOME_KIND,
}
RECEIPT_SCHEMA = "meta-research/owner-acceptance-receipt/v1"


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
    request_ref: str
    cycle_ref: str
    stage: str
    epoch: int
    run_ref: str
    outcome_ref: str
    outcome_kind: str
    disposition: str
    run_completion_receipt: AcceptanceReceipt
    outcome_receipt: AcceptanceReceipt
    receipt: AcceptanceReceipt


class AdvancementEngineInterface(Protocol):
    """Whole public Interface for Cycle, Stage, and Foreground authority."""

    def query_snapshot(self) -> OwnerSnapshot: ...

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
        "stage_commit_count "
        "FROM advancement_engine_state WHERE singleton = 'owner'"
    ),
    fact_names=("foreground_cycle_count", "stage_request_count", "stage_commit_count"),
)


class SQLiteAdvancementEngine:
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
        self._stage_request_verifier = SQLiteAdvancementEngineReceiptVerifier(database)
        self._snapshot = SQLiteOwnerSnapshot(database, _SNAPSHOT)

    def query_snapshot(self) -> OwnerSnapshot:
        return self._snapshot.query_snapshot()

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
                    "activated_at": time.time(),
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

    def ensure_idea_stage_request(
        self,
        *,
        cycle_ref: str,
        accepted_question: AcceptedQuestionBinding,
        context_pack: dict[str, object],
        idempotency_key: str,
    ) -> StageRunRequest:
        _validate_idempotency_key(idempotency_key)
        context_pack_json = canonical_json(context_pack)
        context_pack_hash = canonical_hash(context_pack)
        epoch = 1
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
                    ":cycle_ref"
                ),
                {"cycle_ref": cycle_ref},
            ).first()
        if existing is not None:
            if existing.request_hash != request_hash:
                raise OwnerConflict("stage_run_request_conflict")
            with self._database.write() as connection:
                replay_ref = _ae_command_replay(
                    connection,
                    idempotency_key,
                    "ensure_idea_stage_request",
                    request_hash,
                )
                if replay_ref is None:
                    current = connection.execute(
                        text(
                            "SELECT * FROM ae_stage_run_requests WHERE cycle_ref = "
                            ":cycle_ref"
                        ),
                        {"cycle_ref": cycle_ref},
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
            replay_ref = _ae_command_replay(
                connection,
                idempotency_key,
                "ensure_idea_stage_request",
                request_hash,
            )
            if replay_ref is not None:
                result_ref = replay_ref
            else:
                existing = connection.execute(
                    text(
                        "SELECT * FROM ae_stage_run_requests WHERE cycle_ref = "
                        ":cycle_ref"
                    ),
                    {"cycle_ref": cycle_ref},
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
            row = connection.execute(
                text(
                    "SELECT * FROM ae_stage_run_requests WHERE cycle_ref = "
                    ":cycle_ref AND stage = 'idea'"
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
        epoch = 1
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
            evidence_by_ref = validate_plan_context_pack(
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
                    ":cycle_ref AND stage = 'plan'"
                ),
                {"cycle_ref": cycle_ref},
            ).first()
        if existing is not None:
            if existing.request_hash != request_hash:
                raise OwnerConflict("stage_run_request_conflict")
            with self._database.write() as connection:
                replay_ref = _ae_command_replay(
                    connection,
                    idempotency_key,
                    command_kind,
                    request_hash,
                )
                if replay_ref is None:
                    current = connection.execute(
                        text(
                            "SELECT * FROM ae_stage_run_requests WHERE cycle_ref = "
                            ":cycle_ref AND stage = 'plan'"
                        ),
                        {"cycle_ref": cycle_ref},
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
        version_refs = tuple(
            sorted(
                str(evidence["asset_version_ref"])
                for evidence in evidence_by_ref.values()
            )
        )
        self._verify_plan_evidence(
            accepted_question,
            evidence_catalog,
            expected_reference_revision=reference_revision,
        )
        if self._evidence_verifier is None:
            raise OwnerConflict("evidence_verifier_unavailable")

        with self._database.write() as connection:
            replay_ref = _ae_command_replay(
                connection,
                idempotency_key,
                command_kind,
                request_hash,
            )
            if replay_ref is not None:
                result_ref = replay_ref
            else:
                existing = connection.execute(
                    text(
                        "SELECT * FROM ae_stage_run_requests WHERE cycle_ref = "
                        ":cycle_ref AND stage = 'plan'"
                    ),
                    {"cycle_ref": cycle_ref},
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
                    self._evidence_verifier.assert_evidence_state(
                        quest_ref=accepted_question.quest_ref,
                        version_refs=version_refs,
                        expected_reference_revision=reference_revision,
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
            row = connection.execute(
                text(
                    "SELECT * FROM ae_stage_run_requests WHERE cycle_ref = "
                    ":cycle_ref AND stage = 'plan'"
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
    ) -> None:
        if self._evidence_verifier is None:
            raise OwnerConflict("evidence_verifier_unavailable")
        self._evidence_verifier.verify_plan_evidence_catalog(
            quest_ref=accepted_question.quest_ref,
            evidence_catalog=evidence_catalog,
            expected_reference_revision=expected_reference_revision,
            require_current=True,
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
                text("SELECT * FROM ae_initial_cycles WHERE cycle_ref = :cycle_ref"),
                {"cycle_ref": cycle_ref},
            ).first()
        if cycle is None or (
            cycle.initialization_id != accepted_question.initialization_id
            or cycle.quest_ref != accepted_question.quest_ref
            or cycle.question_ref != accepted_question.question_ref
            or cycle.question_receipt_ref
            != accepted_question.question_receipt.receipt_ref
            or cycle.question_receipt_hash
            != accepted_question.question_receipt.payload_hash
        ):
            raise OwnerConflict("stage_run_question_lineage_invalid")
        self._question_verifier.verify_root_question_receipt(
            initialization_id=accepted_question.initialization_id,
            quest_ref=accepted_question.quest_ref,
            question_ref=accepted_question.question_ref,
            receipt=accepted_question.question_receipt,
        )
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
        committed = self.query_idea_stage_commit(request_ref)
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

    def _stage_commit_from_row(self, row) -> StageCommit:
        committed = _stage_commit(row)
        if row.receipt_hash != _stage_commit_receipt_hash(row):
            raise OwnerConflict("stage_commit_receipt_invalid")
        if self._run_completion_verifier is not None:
            self._run_completion_verifier.verify_run_completion_receipt(
                request_ref=row.request_ref,
                run_ref=row.run_ref,
                attempt_ref=None,
                outcome_ref=row.outcome_ref,
                receipt=committed.run_completion_receipt,
            )
        if row.stage == IDEA_STAGE and self._outcome_verifier is not None:
            self._outcome_verifier.verify_idea_outcome_decision(
                request_ref=row.request_ref,
                submission_ref=None,
                decision="accepted",
                outcome_ref=row.outcome_ref,
                outcome_kind=row.outcome_kind,
                receipt=committed.outcome_receipt,
            )
        elif row.stage == PLAN_STAGE and self._formal_plan_verifier is not None:
            self._formal_plan_verifier.verify_formal_plan_decision(
                request_ref=row.request_ref,
                submission_ref=None,
                decision="accepted",
                formal_plan_ref=row.outcome_ref,
                receipt=committed.outcome_receipt,
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


def _question_binding(row) -> AcceptedQuestionBinding:
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


def _stage_request_bindings(row) -> dict[str, object]:
    return {
        **_question_binding_columns(_question_binding(row)),
        "cycle_ref": row.cycle_ref,
        "stage": row.stage,
        "epoch": int(row.epoch),
        "context_pack_ref": row.context_pack_ref,
        "context_pack_hash": row.context_pack_hash,
    }


def _stage_request_receipt_hash(row) -> str:
    return _receipt_hash(
        STAGE_REQUEST_RECEIPT_KIND, row.request_ref, _stage_request_bindings(row)
    )


def _verify_stage_request_integrity(row) -> dict[str, object]:
    try:
        context_pack = decoded_object(row.context_pack_json)
    except (TypeError, ValueError) as error:
        raise OwnerConflict("stage_run_request_invalid") from error
    binding = _question_binding(row)
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
        or row.receipt_hash != _stage_request_receipt_hash(row)
    ):
        raise OwnerConflict("stage_run_request_invalid")
    return context_pack


def _stage_request(row) -> StageRunRequest:
    context_pack = _verify_stage_request_integrity(row)
    return StageRunRequest(
        request_ref=row.request_ref,
        cycle_ref=row.cycle_ref,
        stage=row.stage,
        epoch=int(row.epoch),
        context_pack_ref=row.context_pack_ref,
        context_pack_hash=row.context_pack_hash,
        context_pack=context_pack,
        accepted_question=_question_binding(row),
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
    valid_kind = (
        row.stage == IDEA_STAGE and row.outcome_kind in COMPLETABLE_IDEA_OUTCOME_KINDS
    ) or (row.stage == PLAN_STAGE and row.outcome_kind == FORMAL_PLAN_OUTCOME_KIND)
    if not valid_kind or row.disposition != COMPLETED_DISPOSITION:
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
        run_completion_receipt=AcceptanceReceipt(
            issuer="agent_runtime",
            kind="run_execution_completed",
            receipt_ref=row.run_completion_receipt_ref,
            subject_ref=row.run_ref,
            payload_hash=row.run_completion_receipt_hash,
        ),
        outcome_receipt=AcceptanceReceipt(
            issuer="research_graph",
            kind=(
                "idea_outcome_accepted"
                if row.stage == IDEA_STAGE
                else "formal_plan_accepted"
            ),
            receipt_ref=row.outcome_receipt_ref,
            subject_ref=row.outcome_ref,
            payload_hash=row.outcome_receipt_hash,
        ),
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
    )
