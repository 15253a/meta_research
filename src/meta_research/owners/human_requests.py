from __future__ import annotations

import json
import math
import time
from typing import Protocol, cast

from sqlalchemy import text
from sqlalchemy.engine import Connection

from meta_research.database import Database
from meta_research.feed import DurableFeed
from meta_research.owners.common import (
    AcceptanceReceipt,
    OwnerConflict,
    canonical_hash,
    canonical_json,
    decoded_object,
    new_ref,
)
from meta_research.owners.secret_detection import contains_secret


HUMAN_REQUEST_KINDS = {
    "library_reconnect",
    "external_material_api_access",
    "offline_action",
    "capability_authorization",
}
HUMAN_EVALUATIONS = {"satisfied", "needs_input", "declined", "stale"}
HUMAN_DISPOSITIONS = {
    "satisfied",
    "declined",
    "withdrawn",
    "expired",
    "superseded",
}
HUMAN_RESPONSE_DECISIONS = {"provided", "declined", "deferred"}
HUMAN_REQUEST_RECEIPT_SCHEMA = "meta-research/human-request-disposition/v1"
HUMAN_REQUEST_RESUME_CONSUMPTION_RECEIPT_SCHEMA = (
    "meta-research/human-request-resume-consumption/v1"
)

_OWNER_STATE_TABLES = {
    "research_graph": "research_graph_state",
    "research_memory": "research_memory_state",
    "agent_runtime": "agent_runtime_state",
    "advancement_engine": "advancement_engine_state",
}


class HumanResponseVerifier(Protocol):
    """Narrow HC authority consumed by blocked Owners."""

    def query_human_responses(
        self, request_ref: str
    ) -> tuple[dict[str, object], ...]: ...

    def verify_human_response(
        self, *, request_ref: str, response_ref: str
    ) -> dict[str, object]: ...

    def verify_capability_authorization(
        self,
        *,
        requirement: dict[str, object],
        receipt_ref: str,
    ) -> None: ...

    def verify_broad_research_authorization(
        self, *, quest_ref: str
    ) -> dict[str, object]: ...

    def verify_guidance_snapshot(
        self,
        *,
        scope_ref: str,
        bindings: list[dict[str, object]],
    ) -> None: ...


class HumanRequestOwnerInterface(Protocol):
    """The HumanRequest portion of each State Owner's whole Interface."""

    def open_human_request(
        self,
        *,
        request_kind: str,
        obligation: str,
        business_purpose: str,
        target_assertion: dict[str, object],
        acceptance_conditions: tuple[str, ...],
        direct_waiter: dict[str, object],
        idempotency_key: str,
        quest_ref: str | None = None,
        required_authorization: dict[str, object] | None = None,
        expires_at: float | None = None,
    ) -> dict[str, object]: ...

    def revise_human_request(
        self,
        request_ref: str,
        *,
        expected_revision: int,
        obligation: str,
        target_assertion: dict[str, object],
        acceptance_conditions: tuple[str, ...],
        direct_waiters: tuple[dict[str, object], ...],
        idempotency_key: str,
        required_authorization: dict[str, object] | None = None,
        expires_at: float | None = None,
    ) -> dict[str, object]: ...

    def evaluate_human_request(
        self,
        request_ref: str,
        *,
        response_refs: tuple[str, ...],
        decision: str,
        reason_code: str,
        accepted_evidence_refs: tuple[str, ...],
        idempotency_key: str,
    ) -> dict[str, object]: ...

    def validate_human_request_waiter(
        self,
        request_ref: str,
        *,
        waiter_ref: str,
        generation: int,
        target_assertion: dict[str, object],
        other_blockers: tuple[str, ...],
        idempotency_key: str,
        authorization_receipt_ref: str | None = None,
    ) -> dict[str, object]: ...

    def query_human_request(
        self, request_ref: str
    ) -> dict[str, object] | None: ...

    def query_human_requests(
        self,
        *,
        quest_ref: str | None = None,
        include_history: bool = False,
    ) -> tuple[dict[str, object], ...]: ...



class HumanRequestOwnerMixin:
    """Delegates the HumanRequest slice without exposing its persistence shape."""

    _human_request_owner: SQLiteHumanRequestOwner

    def _configure_human_request_owner(
        self,
        database: Database,
        feed: DurableFeed,
        issuer: str,
        response_verifier: HumanResponseVerifier | None,
    ) -> None:
        self._human_request_owner = SQLiteHumanRequestOwner(
            database,
            feed,
            issuer,
            response_verifier,
        )

    def open_human_request(self, **values) -> dict[str, object]:
        return self._human_request_owner.open_human_request(**values)

    def revise_human_request(self, request_ref: str, **values) -> dict[str, object]:
        return self._human_request_owner.revise_human_request(request_ref, **values)

    def evaluate_human_request(
        self, request_ref: str, **values
    ) -> dict[str, object]:
        return self._human_request_owner.evaluate_human_request(request_ref, **values)

    def validate_human_request_waiter(
        self, request_ref: str, **values
    ) -> dict[str, object]:
        return self._human_request_owner.validate_human_request_waiter(
            request_ref, **values
        )

    def _consume_human_request_waiter(self, connection: Connection, **values):
        return self._human_request_owner.consume_released_waiter(
            connection, **values
        )

    def query_human_request(
        self, request_ref: str
    ) -> dict[str, object] | None:
        return self._human_request_owner.query_human_request(request_ref)

    def query_human_requests(self, **values) -> tuple[dict[str, object], ...]:
        return self._human_request_owner.query_human_requests(**values)



class SQLiteHumanRequestOwner:
    """Shared mechanism used only behind a concrete issuing Owner Interface."""

    def __init__(
        self,
        database: Database,
        feed: DurableFeed,
        issuer: str,
        response_verifier: HumanResponseVerifier | None,
    ) -> None:
        try:
            self._state_table = _OWNER_STATE_TABLES[issuer]
        except KeyError as error:
            raise ValueError("human_request_issuer_invalid") from error
        self._database = database
        self._feed = feed
        self._issuer = issuer
        self._response_verifier = response_verifier

    def open_human_request(
        self,
        *,
        request_kind: str,
        obligation: str,
        business_purpose: str,
        target_assertion: dict[str, object],
        acceptance_conditions: tuple[str, ...],
        direct_waiter: dict[str, object],
        idempotency_key: str,
        quest_ref: str | None = None,
        required_authorization: dict[str, object] | None = None,
        expires_at: float | None = None,
    ) -> dict[str, object]:
        contract = _validate_contract(
            request_kind=request_kind,
            obligation=obligation,
            business_purpose=business_purpose,
            target_assertion=target_assertion,
            acceptance_conditions=acceptance_conditions,
            quest_ref=quest_ref,
            required_authorization=required_authorization,
            expires_at=expires_at,
        )
        waiter = _validate_waiter(direct_waiter)
        command = {
            "command": "open_human_request",
            "contract": contract,
            "direct_waiter": waiter,
        }
        request_hash = canonical_hash(command)
        _validate_idempotency_key(idempotency_key)
        with self._database.write() as connection:
            replay = _command_replay(
                connection,
                self._issuer,
                idempotency_key,
                "open",
                request_hash,
            )
            if replay is not None:
                request_ref = replay
            else:
                identity_hash = canonical_hash(contract)
                row = connection.execute(
                    text(
                        "SELECT * FROM owner_human_requests WHERE issuer = :issuer "
                        "AND identity_hash = :identity_hash AND is_current = 1 "
                        "AND status = 'open' ORDER BY created_at LIMIT 1"
                    ),
                    {"issuer": self._issuer, "identity_hash": identity_hash},
                ).first()
                now = time.time()
                created = row is None
                if row is None:
                    request_id = new_ref("human_request")
                    request_ref = f"{request_id}:r1"
                    _insert_request(
                        connection,
                        issuer=self._issuer,
                        request_id=request_id,
                        request_ref=request_ref,
                        revision=1,
                        contract=contract,
                        identity_hash=identity_hash,
                        now=now,
                    )
                else:
                    request_ref = row.request_ref
                waiter_added = _insert_waiter(
                    connection, request_ref=request_ref, waiter=waiter, now=now
                )
                _record_command(
                    connection,
                    self._issuer,
                    idempotency_key,
                    "open",
                    request_hash,
                    request_ref,
                )
                if created or waiter_added:
                    connection.execute(
                        text(
                            f"UPDATE {self._state_table} SET revision = revision + 1, "
                            "human_request_count = human_request_count + :created "
                            "WHERE singleton = 'owner'"
                        ),
                        {"created": 1 if created else 0},
                    )
                    self._feed.record(
                        connection,
                        f"{self._issuer}.human_request_opened",
                        {
                            "request_ref": request_ref,
                            "kind": request_kind,
                            "waiter_ref": waiter["waiter_ref"],
                            "reused": not created,
                        },
                    )
        result = self.query_human_request(request_ref)
        if result is None:
            raise OwnerConflict("human_request_not_found")
        return result

    def consume_released_waiter(
        self,
        connection: Connection,
        *,
        request_ref: str,
        waiter_ref: str,
        generation: int,
        work_ref: str,
        work_hash: str,
    ) -> dict[str, object]:
        """Atomically bind one released validation to the Owner work it starts."""

        waiter_ref = _bounded_text(
            waiter_ref, "human_request_waiter_ref_invalid", 128
        )
        work_ref = _bounded_text(work_ref, "human_request_work_ref_invalid", 128)
        if (
            not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < 1
            or not isinstance(work_hash, str)
            or len(work_hash) != 64
            or contains_secret({"work_ref": work_ref, "work_hash": work_hash})
        ):
            raise OwnerConflict("human_request_resume_consumption_invalid")
        existing = connection.execute(
            text(
                "SELECT * FROM owner_human_request_resume_consumptions WHERE "
                "request_ref = :request_ref AND waiter_ref = :waiter_ref AND "
                "generation = :generation"
            ),
            {
                "request_ref": request_ref,
                "waiter_ref": waiter_ref,
                "generation": generation,
            },
        ).first()
        if existing is not None:
            if existing.work_ref != work_ref or existing.work_hash != work_hash:
                raise OwnerConflict("human_request_resume_already_consumed")
            return _public_consumption(existing, self._issuer)

        request = _request_row(connection, self._issuer, request_ref)
        disposition = connection.execute(
            text(
                "SELECT * FROM owner_human_request_dispositions WHERE "
                "request_ref = :request_ref"
            ),
            {"request_ref": request_ref},
        ).first()
        if request is None or disposition is None:
            raise OwnerConflict("human_request_resume_not_released")
        _verify_request_state_integrity(connection, request, disposition)
        _verify_disposition_integrity(
            connection,
            issuer=self._issuer,
            request=request,
            disposition=disposition,
            response_verifier=self._response_verifier,
        )
        waiter = connection.execute(
            text(
                "SELECT * FROM owner_human_request_waiters WHERE request_ref = "
                ":request_ref AND waiter_ref = :waiter_ref"
            ),
            {"request_ref": request_ref, "waiter_ref": waiter_ref},
        ).first()
        validation = connection.execute(
            text(
                "SELECT * FROM owner_human_request_resume_validations WHERE "
                "request_ref = :request_ref AND waiter_ref = :waiter_ref ORDER BY "
                "created_at DESC, validation_ref DESC LIMIT 1"
            ),
            {"request_ref": request_ref, "waiter_ref": waiter_ref},
        ).first()
        if (
            request.status != "satisfied"
            or not bool(request.is_current)
            or disposition.decision != "satisfied"
            or waiter is None
            or waiter.status != "released"
            or int(waiter.generation) != generation
            or validation is None
            or validation.status != "released"
            or int(validation.generation) != generation
            or validation.target_assertion_hash != waiter.target_assertion_hash
        ):
            raise OwnerConflict("human_request_resume_not_released")
        consumption_ref = new_ref("resume_consumption")
        receipt_ref = new_ref("owner_receipt")
        receipt_hash = _resume_consumption_receipt_hash(
            issuer=self._issuer,
            consumption_ref=consumption_ref,
            request_ref=request_ref,
            waiter_ref=waiter_ref,
            generation=generation,
            validation_ref=validation.validation_ref,
            work_ref=work_ref,
            work_hash=work_hash,
        )
        now = time.time()
        connection.execute(
            text(
                "INSERT INTO owner_human_request_resume_consumptions "
                "(consumption_ref, request_ref, waiter_ref, generation, "
                "validation_ref, work_ref, work_hash, receipt_ref, receipt_hash, "
                "created_at) VALUES (:consumption_ref, :request_ref, :waiter_ref, "
                ":generation, :validation_ref, :work_ref, :work_hash, :receipt_ref, "
                ":receipt_hash, :now)"
            ),
            {
                "consumption_ref": consumption_ref,
                "request_ref": request_ref,
                "waiter_ref": waiter_ref,
                "generation": generation,
                "validation_ref": validation.validation_ref,
                "work_ref": work_ref,
                "work_hash": work_hash,
                "receipt_ref": receipt_ref,
                "receipt_hash": receipt_hash,
                "now": now,
            },
        )
        updated = connection.execute(
            text(
                "UPDATE owner_human_request_waiters SET status = 'consumed', "
                "updated_at = :now WHERE request_ref = :request_ref AND "
                "waiter_ref = :waiter_ref AND status = 'released'"
            ),
            {"now": now, "request_ref": request_ref, "waiter_ref": waiter_ref},
        )
        if updated.rowcount != 1:
            raise OwnerConflict("human_request_resume_not_released")
        connection.execute(
            text(
                f"UPDATE {self._state_table} SET revision = revision + 1 "
                "WHERE singleton = 'owner'"
            )
        )
        self._feed.record(
            connection,
            f"{self._issuer}.human_request_resume_consumed",
            {
                "request_ref": request_ref,
                "waiter_ref": waiter_ref,
                "validation_ref": validation.validation_ref,
                "consumption_ref": consumption_ref,
                "work_ref": work_ref,
            },
        )
        row = connection.execute(
            text(
                "SELECT * FROM owner_human_request_resume_consumptions WHERE "
                "consumption_ref = :consumption_ref"
            ),
            {"consumption_ref": consumption_ref},
        ).one()
        return _public_consumption(row, self._issuer)

    def revise_human_request(
        self,
        request_ref: str,
        *,
        expected_revision: int,
        obligation: str,
        target_assertion: dict[str, object],
        acceptance_conditions: tuple[str, ...],
        direct_waiters: tuple[dict[str, object], ...],
        idempotency_key: str,
        required_authorization: dict[str, object] | None = None,
        expires_at: float | None = None,
    ) -> dict[str, object]:
        _validate_idempotency_key(idempotency_key)
        self._materialize_expiration_if_due(request_ref)
        with self._database.read() as connection:
            current = _request_row(connection, self._issuer, request_ref)
        if current is None:
            raise OwnerConflict("human_request_not_found")
        contract = _validate_contract(
            request_kind=current.kind,
            obligation=obligation,
            business_purpose=current.business_purpose,
            target_assertion=target_assertion,
            acceptance_conditions=acceptance_conditions,
            quest_ref=current.quest_ref,
            required_authorization=required_authorization,
            expires_at=expires_at,
        )
        waiters = tuple(_validate_waiter(item) for item in direct_waiters)
        if not waiters:
            raise OwnerConflict("human_request_direct_waiter_required")
        if len({cast(str, item["waiter_ref"]) for item in waiters}) != len(waiters):
            raise OwnerConflict("human_request_waiter_duplicate")
        request_hash = canonical_hash(
            {
                "command": "revise_human_request",
                "request_ref": request_ref,
                "expected_revision": expected_revision,
                "contract": contract,
                "direct_waiters": list(waiters),
            }
        )
        with self._database.write() as connection:
            replay = _command_replay(
                connection,
                self._issuer,
                idempotency_key,
                "revise",
                request_hash,
            )
            if replay is not None:
                successor_ref = replay
            else:
                row = _request_row(connection, self._issuer, request_ref)
                if row is None:
                    raise OwnerConflict("human_request_not_found")
                disposition = connection.execute(
                    text(
                        "SELECT * FROM owner_human_request_dispositions WHERE "
                        "request_ref = :request_ref"
                    ),
                    {"request_ref": request_ref},
                ).first()
                _verify_request_state_integrity(connection, row, disposition)
                if (
                    int(row.revision) != expected_revision
                    or not bool(row.is_current)
                    or row.status != "open"
                ):
                    raise OwnerConflict("human_request_revision_stale")
                if canonical_hash(contract) == row.identity_hash:
                    raise OwnerConflict("human_request_revision_unchanged")
                now = time.time()
                successor_revision = expected_revision + 1
                successor_ref = f"{row.request_id}:r{successor_revision}"
                connection.execute(
                    text(
                        "UPDATE owner_human_requests SET status = 'superseded', "
                        "is_current = 0, updated_at = :now WHERE request_ref = :request_ref"
                    ),
                    {"now": now, "request_ref": request_ref},
                )
                _insert_disposition(
                    connection,
                    issuer=self._issuer,
                    request_ref=request_ref,
                    decision="superseded",
                    evaluation_ref=None,
                    now=now,
                )
                _insert_request(
                    connection,
                    issuer=self._issuer,
                    request_id=row.request_id,
                    request_ref=successor_ref,
                    revision=successor_revision,
                    contract=contract,
                    identity_hash=canonical_hash(contract),
                    now=now,
                )
                for waiter in waiters:
                    _insert_waiter(
                        connection,
                        request_ref=successor_ref,
                        waiter=waiter,
                        now=now,
                    )
                _record_command(
                    connection,
                    self._issuer,
                    idempotency_key,
                    "revise",
                    request_hash,
                    successor_ref,
                )
                connection.execute(
                    text(
                        f"UPDATE {self._state_table} SET revision = revision + 1 "
                        "WHERE singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    f"{self._issuer}.human_request_revised",
                    {
                        "request_ref": successor_ref,
                        "supersedes": request_ref,
                        "revision": successor_revision,
                    },
                )
        result = self.query_human_request(successor_ref)
        if result is None:
            raise OwnerConflict("human_request_not_found")
        return result

    def evaluate_human_request(
        self,
        request_ref: str,
        *,
        response_refs: tuple[str, ...],
        decision: str,
        reason_code: str,
        accepted_evidence_refs: tuple[str, ...],
        idempotency_key: str,
    ) -> dict[str, object]:
        _validate_idempotency_key(idempotency_key)
        self._materialize_expiration_if_due(request_ref)
        if decision not in HUMAN_EVALUATIONS:
            raise OwnerConflict("human_request_evaluation_invalid")
        reason_code = _bounded_text(
            reason_code, "human_request_evaluation_reason_required", 96
        )
        response_refs = _unique_refs(response_refs, "human_response_ref_invalid")
        evidence_refs = _unique_refs(
            accepted_evidence_refs, "human_evidence_ref_invalid"
        )
        if contains_secret(
            {
                "reason_code": reason_code,
                "response_refs": response_refs,
                "accepted_evidence_refs": evidence_refs,
            }
        ):
            raise OwnerConflict("human_request_secret_forbidden")
        responses: tuple[dict[str, object], ...] = ()
        if response_refs:
            verifier = self._require_response_verifier()
            responses = tuple(
                verifier.verify_human_response(
                    request_ref=request_ref, response_ref=response_ref
                )
                for response_ref in response_refs
            )
        if decision == "satisfied" and (
            not responses
            or any(item["decision"] != "provided" for item in responses)
        ):
            raise OwnerConflict("human_request_satisfaction_evidence_invalid")
        if decision == "declined" and not any(
            item["decision"] == "declined" for item in responses
        ):
            raise OwnerConflict("human_request_decline_response_required")
        command_hash = canonical_hash(
            {
                "command": "evaluate_human_request",
                "request_ref": request_ref,
                "response_refs": list(response_refs),
                "decision": decision,
                "reason_code": reason_code,
                "accepted_evidence_refs": list(evidence_refs),
            }
        )
        with self._database.write() as connection:
            replay = _command_replay(
                connection,
                self._issuer,
                idempotency_key,
                "evaluate",
                command_hash,
            )
            if replay is None:
                row = _request_row(connection, self._issuer, request_ref)
                if row is None:
                    raise OwnerConflict("human_request_not_found")
                disposition = connection.execute(
                    text(
                        "SELECT * FROM owner_human_request_dispositions WHERE "
                        "request_ref = :request_ref"
                    ),
                    {"request_ref": request_ref},
                ).first()
                _verify_request_state_integrity(connection, row, disposition)
                if not bool(row.is_current) or row.status != "open":
                    raise OwnerConflict("human_request_not_current")
                sequence = int(
                    connection.execute(
                        text(
                            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM "
                            "owner_human_request_evaluations WHERE request_ref = "
                            ":request_ref"
                        ),
                        {"request_ref": request_ref},
                    ).scalar_one()
                )
                evaluation_ref = new_ref("human_evaluation")
                now = time.time()
                connection.execute(
                    text(
                        "INSERT INTO owner_human_request_evaluations "
                        "(evaluation_ref, request_ref, sequence, decision, "
                        "response_refs_json, response_refs_hash, evidence_refs_json, "
                        "evidence_refs_hash, reason_code, created_at) VALUES "
                        "(:evaluation_ref, :request_ref, :sequence, :decision, "
                        ":response_refs_json, :response_refs_hash, "
                        ":evidence_refs_json, :evidence_refs_hash, :reason_code, :now)"
                    ),
                    {
                        "evaluation_ref": evaluation_ref,
                        "request_ref": request_ref,
                        "sequence": sequence,
                        "decision": decision,
                        "response_refs_json": canonical_json(list(response_refs)),
                        "response_refs_hash": canonical_hash(list(response_refs)),
                        "evidence_refs_json": canonical_json(list(evidence_refs)),
                        "evidence_refs_hash": canonical_hash(list(evidence_refs)),
                        "reason_code": reason_code,
                        "now": now,
                    },
                )
                if decision in {"satisfied", "declined"}:
                    _insert_disposition(
                        connection,
                        issuer=self._issuer,
                        request_ref=request_ref,
                        decision=decision,
                        evaluation_ref=evaluation_ref,
                        now=now,
                    )
                    connection.execute(
                        text(
                            "UPDATE owner_human_requests SET status = :status, "
                            "updated_at = :now WHERE request_ref = :request_ref"
                        ),
                        {
                            "status": decision,
                            "now": now,
                            "request_ref": request_ref,
                        },
                    )
                else:
                    connection.execute(
                        text(
                            "UPDATE owner_human_requests SET updated_at = :now "
                            "WHERE request_ref = :request_ref"
                        ),
                        {"now": now, "request_ref": request_ref},
                    )
                _record_command(
                    connection,
                    self._issuer,
                    idempotency_key,
                    "evaluate",
                    command_hash,
                    request_ref,
                )
                connection.execute(
                    text(
                        f"UPDATE {self._state_table} SET revision = revision + 1 "
                        "WHERE singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    f"{self._issuer}.human_request_evaluated",
                    {
                        "request_ref": request_ref,
                        "evaluation_ref": evaluation_ref,
                        "decision": decision,
                    },
                )
        result = self.query_human_request(request_ref)
        if result is None:
            raise OwnerConflict("human_request_not_found")
        return result

    def validate_human_request_waiter(
        self,
        request_ref: str,
        *,
        waiter_ref: str,
        generation: int,
        target_assertion: dict[str, object],
        other_blockers: tuple[str, ...],
        idempotency_key: str,
        authorization_receipt_ref: str | None = None,
    ) -> dict[str, object]:
        _validate_idempotency_key(idempotency_key)
        self._materialize_expiration_if_due(request_ref)
        waiter_ref = _bounded_text(
            waiter_ref, "human_request_waiter_ref_invalid", 128
        )
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
            raise OwnerConflict("human_request_waiter_generation_invalid")
        target_assertion = _object(target_assertion, "target_assertion_invalid")
        blockers = _unique_refs(other_blockers, "human_request_blocker_invalid")
        if contains_secret(
            {
                "target_assertion": target_assertion,
                "other_blockers": blockers,
                "authorization_receipt_ref": authorization_receipt_ref,
            }
        ):
            raise OwnerConflict("human_request_secret_forbidden")
        command_hash = canonical_hash(
            {
                "command": "validate_human_request_waiter",
                "request_ref": request_ref,
                "waiter_ref": waiter_ref,
                "generation": generation,
                "target_assertion": target_assertion,
                "other_blockers": list(blockers),
                "authorization_receipt_ref": authorization_receipt_ref,
            }
        )
        with self._database.write() as connection:
            replay = _command_replay(
                connection,
                self._issuer,
                idempotency_key,
                "resume_validate",
                command_hash,
            )
            if replay is not None:
                validation_ref = replay
            else:
                request = _request_row(connection, self._issuer, request_ref)
                if request is None:
                    raise OwnerConflict("human_request_not_found")
                disposition = connection.execute(
                    text(
                        "SELECT * FROM owner_human_request_dispositions WHERE "
                        "request_ref = :request_ref"
                    ),
                    {"request_ref": request_ref},
                ).first()
                _verify_request_state_integrity(connection, request, disposition)
                waiter = connection.execute(
                    text(
                        "SELECT * FROM owner_human_request_waiters WHERE "
                        "request_ref = :request_ref AND waiter_ref = :waiter_ref"
                    ),
                    {"request_ref": request_ref, "waiter_ref": waiter_ref},
                ).first()
                if waiter is None:
                    raise OwnerConflict("human_request_waiter_not_found")
                # This method is an issuing-Owner command.  Its blocker set is
                # the Owner's current observation for the exact waiter
                # generation; the creation-time set is historical context and
                # must not make a recovered technical blocker permanent.
                _verified_waiter_blockers(waiter)
                effective_blockers = blockers
                if disposition is not None:
                    _verify_disposition_integrity(
                        connection,
                        issuer=self._issuer,
                        request=request,
                        disposition=disposition,
                        response_verifier=self._response_verifier,
                    )
                reason_code: str | None = None
                status = "released"
                if (
                    disposition is None
                    or disposition.decision != "satisfied"
                    or request.status != "satisfied"
                    or not bool(request.is_current)
                ):
                    status = "blocked"
                    reason_code = "satisfied_disposition_required"
                elif waiter.status != "blocked":
                    status = "blocked"
                    reason_code = "waiter_not_resumable"
                elif int(waiter.generation) != generation:
                    status = "blocked"
                    reason_code = "waiter_generation_stale"
                elif waiter.target_assertion_hash != canonical_hash(target_assertion):
                    status = "blocked"
                    reason_code = "target_assertion_stale"
                elif effective_blockers:
                    status = "blocked"
                    reason_code = "other_blockers_present"
                else:
                    requirement = (
                        None
                        if request.required_authorization_json is None
                        else decoded_object(request.required_authorization_json)
                    )
                    if requirement is not None:
                        if authorization_receipt_ref is None:
                            status = "blocked"
                            reason_code = "authorization_receipt_required"
                        else:
                            try:
                                self._require_response_verifier().verify_capability_authorization(
                                    requirement=requirement,
                                    receipt_ref=authorization_receipt_ref,
                                )
                            except OwnerConflict:
                                status = "blocked"
                                reason_code = "authorization_receipt_invalid"
                validation_ref = new_ref("resume_validation")
                now = time.time()
                connection.execute(
                    text(
                        "INSERT INTO owner_human_request_resume_validations "
                        "(validation_ref, request_ref, waiter_ref, generation, "
                        "target_assertion_hash, authorization_receipt_ref, "
                        "other_blockers_json, other_blockers_hash, status, "
                        "reason_code, created_at) VALUES (:validation_ref, "
                        ":request_ref, :waiter_ref, :generation, "
                        ":target_assertion_hash, :authorization_receipt_ref, "
                        ":other_blockers_json, :other_blockers_hash, :status, "
                        ":reason_code, :now)"
                    ),
                    {
                        "validation_ref": validation_ref,
                        "request_ref": request_ref,
                        "waiter_ref": waiter_ref,
                        "generation": generation,
                        "target_assertion_hash": canonical_hash(target_assertion),
                        "authorization_receipt_ref": authorization_receipt_ref,
                        "other_blockers_json": canonical_json(
                            list(effective_blockers)
                        ),
                        "other_blockers_hash": canonical_hash(
                            list(effective_blockers)
                        ),
                        "status": status,
                        "reason_code": reason_code,
                        "now": now,
                    },
                )
                if (
                    waiter.status == "blocked"
                    and int(waiter.generation) == generation
                    and waiter.target_assertion_hash
                    == canonical_hash(target_assertion)
                ):
                    connection.execute(
                        text(
                            "UPDATE owner_human_request_waiters SET status = :status, "
                            "other_blockers_json = :blockers_json, "
                            "other_blockers_hash = :blockers_hash, updated_at = :now "
                            "WHERE request_ref = "
                            ":request_ref AND waiter_ref = :waiter_ref"
                        ),
                        {
                            "status": status,
                            "blockers_json": canonical_json(list(effective_blockers)),
                            "blockers_hash": canonical_hash(list(effective_blockers)),
                            "now": now,
                            "request_ref": request_ref,
                            "waiter_ref": waiter_ref,
                        },
                    )
                _record_command(
                    connection,
                    self._issuer,
                    idempotency_key,
                    "resume_validate",
                    command_hash,
                    validation_ref,
                )
                connection.execute(
                    text(
                        f"UPDATE {self._state_table} SET revision = revision + 1 "
                        "WHERE singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    f"{self._issuer}.human_request_resume_validated",
                    {
                        "request_ref": request_ref,
                        "waiter_ref": waiter_ref,
                        "validation_ref": validation_ref,
                        "status": status,
                    },
                )
        result = self._query_validation(validation_ref)
        if result is None:
            raise OwnerConflict("human_request_resume_validation_invalid")
        return result

    def query_human_request(
        self, request_ref: str
    ) -> dict[str, object] | None:
        self._materialize_expiration_if_due(request_ref)
        with self._database.read() as connection:
            row = _request_row(connection, self._issuer, request_ref)
            if row is None:
                return None
            waiters = connection.execute(
                text(
                    "SELECT * FROM owner_human_request_waiters WHERE request_ref = "
                    ":request_ref ORDER BY created_at, waiter_ref"
                ),
                {"request_ref": request_ref},
            ).all()
            evaluation = connection.execute(
                text(
                    "SELECT * FROM owner_human_request_evaluations WHERE "
                    "request_ref = :request_ref ORDER BY sequence DESC LIMIT 1"
                ),
                {"request_ref": request_ref},
            ).first()
            disposition = connection.execute(
                text(
                    "SELECT * FROM owner_human_request_dispositions WHERE "
                    "request_ref = :request_ref"
                ),
                {"request_ref": request_ref},
            ).first()
            _verify_request_state_integrity(connection, row, disposition)
            if disposition is not None:
                _verify_disposition_integrity(
                    connection,
                    issuer=self._issuer,
                    request=row,
                    disposition=disposition,
                    response_verifier=self._response_verifier,
                )
            validations = connection.execute(
                text(
                    "SELECT * FROM owner_human_request_resume_validations WHERE "
                    "request_ref = :request_ref ORDER BY created_at, validation_ref"
                ),
                {"request_ref": request_ref},
            ).all()
            consumptions = connection.execute(
                text(
                    "SELECT * FROM owner_human_request_resume_consumptions WHERE "
                    "request_ref = :request_ref ORDER BY created_at, "
                    "consumption_ref"
                ),
                {"request_ref": request_ref},
            ).all()
        latest_validations = {item.waiter_ref: item for item in validations}
        latest_consumptions = {item.waiter_ref: item for item in consumptions}
        responses = (
            ()
            if self._response_verifier is None
            else self._response_verifier.query_human_responses(request_ref)
        )
        return _public_request(
            row,
            waiters,
            evaluation,
            disposition,
            latest_validations,
            latest_consumptions,
            responses,
        )

    def query_human_requests(
        self,
        *,
        quest_ref: str | None = None,
        include_history: bool = False,
    ) -> tuple[dict[str, object], ...]:
        clauses = ["issuer = :issuer"]
        parameters: dict[str, object] = {"issuer": self._issuer}
        if not include_history:
            clauses.append("is_current = 1")
        if quest_ref is not None:
            clauses.append("quest_ref = :quest_ref")
            parameters["quest_ref"] = quest_ref
        with self._database.read() as connection:
            refs = tuple(
                row.request_ref
                for row in connection.execute(
                    text(
                        "SELECT request_ref FROM owner_human_requests WHERE "
                        + " AND ".join(clauses)
                        + " ORDER BY created_at, request_ref"
                    ),
                    parameters,
                ).all()
            )
        results = tuple(self.query_human_request(ref) for ref in refs)
        return tuple(item for item in results if item is not None)

    def _materialize_expiration_if_due(self, request_ref: str) -> None:
        now = time.time()
        with self._database.read() as connection:
            candidate = _request_row(connection, self._issuer, request_ref)
        if (
            candidate is None
            or not bool(candidate.is_current)
            or candidate.status != "open"
            or candidate.expires_at is None
            or float(candidate.expires_at) > now
        ):
            return
        with self._database.write() as connection:
            row = _request_row(connection, self._issuer, request_ref)
            now = time.time()
            if (
                row is None
                or not bool(row.is_current)
                or row.status != "open"
                or row.expires_at is None
                or float(row.expires_at) > now
            ):
                return
            connection.execute(
                text(
                    "UPDATE owner_human_requests SET status = 'expired', "
                    "updated_at = :now WHERE request_ref = :request_ref"
                ),
                {"now": now, "request_ref": request_ref},
            )
            connection.execute(
                text(
                    "UPDATE owner_human_request_waiters SET status = 'cancelled', "
                    "updated_at = :now WHERE request_ref = :request_ref AND "
                    "status = 'blocked'"
                ),
                {"now": now, "request_ref": request_ref},
            )
            _insert_disposition(
                connection,
                issuer=self._issuer,
                request_ref=request_ref,
                decision="expired",
                evaluation_ref=None,
                now=now,
            )
            connection.execute(
                text(
                    f"UPDATE {self._state_table} SET revision = revision + 1 "
                    "WHERE singleton = 'owner'"
                )
            )
            self._feed.record(
                connection,
                f"{self._issuer}.human_request_expired",
                {"request_ref": request_ref, "expires_at": float(row.expires_at)},
            )


    def _query_validation(self, validation_ref: str) -> dict[str, object] | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT validations.* FROM owner_human_request_resume_validations "
                    "AS validations JOIN owner_human_requests AS requests ON "
                    "requests.request_ref = validations.request_ref WHERE "
                    "validations.validation_ref = :validation_ref AND "
                    "requests.issuer = :issuer"
                ),
                {"validation_ref": validation_ref, "issuer": self._issuer},
            ).first()
        return None if row is None else _public_validation(row)

    def _require_response_verifier(self) -> HumanResponseVerifier:
        if self._response_verifier is None:
            raise OwnerConflict("human_collaboration_verifier_unavailable")
        return self._response_verifier


def _validate_contract(
    *,
    request_kind: str,
    obligation: str,
    business_purpose: str,
    target_assertion: dict[str, object],
    acceptance_conditions: tuple[str, ...],
    quest_ref: str | None,
    required_authorization: dict[str, object] | None,
    expires_at: float | None,
) -> dict[str, object]:
    if request_kind not in HUMAN_REQUEST_KINDS:
        raise OwnerConflict("human_request_kind_invalid")
    obligation = _bounded_text(obligation, "human_request_obligation_required", 8000)
    business_purpose = _bounded_text(
        business_purpose, "human_request_business_purpose_required", 4000
    )
    target_assertion = _object(target_assertion, "target_assertion_invalid")
    conditions = _unique_texts(
        acceptance_conditions,
        "human_request_acceptance_conditions_invalid",
        maximum_items=32,
        maximum_length=2000,
    )
    if not conditions:
        raise OwnerConflict("human_request_acceptance_conditions_required")
    if quest_ref is not None:
        quest_ref = _bounded_text(quest_ref, "quest_ref_invalid", 64)
    if required_authorization is not None:
        required_authorization = _object(
            required_authorization, "human_request_authorization_invalid"
        )
    if expires_at is not None and (
        not isinstance(expires_at, (int, float))
        or isinstance(expires_at, bool)
        or not math.isfinite(float(expires_at))
        or float(expires_at) <= time.time()
    ):
        raise OwnerConflict("human_request_expiry_invalid")
    contract = {
        "quest_ref": quest_ref,
        "kind": request_kind,
        "obligation": obligation,
        "business_purpose": business_purpose,
        "target_assertion": target_assertion,
        "acceptance_conditions": list(conditions),
        "required_authorization": required_authorization,
        "expires_at": expires_at,
    }
    if contains_secret(contract):
        raise OwnerConflict("human_request_secret_forbidden")
    return contract


def _validate_waiter(value: dict[str, object]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "waiter_ref",
        "generation",
        "target_assertion",
        "wait_scope",
        "other_blockers",
    }:
        raise OwnerConflict("human_request_waiter_invalid")
    waiter_ref = _bounded_text(
        value["waiter_ref"], "human_request_waiter_ref_invalid", 128
    )
    generation = value["generation"]
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise OwnerConflict("human_request_waiter_generation_invalid")
    target_assertion = _object(
        value["target_assertion"], "human_request_waiter_target_invalid"
    )
    wait_scope = value["wait_scope"]
    if wait_scope not in {"local", "quest"}:
        raise OwnerConflict("human_request_wait_scope_invalid")
    blockers = value["other_blockers"]
    if not isinstance(blockers, (list, tuple)):
        raise OwnerConflict("human_request_blockers_invalid")
    waiter = {
        "waiter_ref": waiter_ref,
        "generation": generation,
        "target_assertion": target_assertion,
        "wait_scope": wait_scope,
        "other_blockers": list(
            _unique_refs(tuple(blockers), "human_request_blocker_invalid")
        ),
    }
    if contains_secret(waiter):
        raise OwnerConflict("human_request_secret_forbidden")
    return waiter


def _insert_request(
    connection,
    *,
    issuer: str,
    request_id: str,
    request_ref: str,
    revision: int,
    contract: dict[str, object],
    identity_hash: str,
    now: float,
) -> None:
    target = cast(dict[str, object], contract["target_assertion"])
    conditions = cast(list[str], contract["acceptance_conditions"])
    authorization = cast(
        dict[str, object] | None, contract["required_authorization"]
    )
    connection.execute(
        text(
            "INSERT INTO owner_human_requests (request_ref, issuer, request_id, "
            "revision, quest_ref, kind, obligation, business_purpose, "
            "target_assertion_json, target_assertion_hash, "
            "acceptance_conditions_json, acceptance_conditions_hash, "
            "required_authorization_json, required_authorization_hash, expires_at, "
            "identity_hash, status, is_current, created_at, updated_at) VALUES "
            "(:request_ref, :issuer, :request_id, :revision, :quest_ref, :kind, "
            ":obligation, :business_purpose, :target_json, :target_hash, "
            ":conditions_json, :conditions_hash, :authorization_json, "
            ":authorization_hash, :expires_at, :identity_hash, 'open', 1, :now, :now)"
        ),
        {
            "request_ref": request_ref,
            "issuer": issuer,
            "request_id": request_id,
            "revision": revision,
            "quest_ref": contract["quest_ref"],
            "kind": contract["kind"],
            "obligation": contract["obligation"],
            "business_purpose": contract["business_purpose"],
            "target_json": canonical_json(target),
            "target_hash": canonical_hash(target),
            "conditions_json": canonical_json(conditions),
            "conditions_hash": canonical_hash(conditions),
            "authorization_json": (
                None if authorization is None else canonical_json(authorization)
            ),
            "authorization_hash": (
                None if authorization is None else canonical_hash(authorization)
            ),
            "expires_at": contract["expires_at"],
            "identity_hash": identity_hash,
            "now": now,
        },
    )


def _insert_waiter(
    connection,
    *,
    request_ref: str,
    waiter: dict[str, object],
    now: float,
) -> bool:
    existing = connection.execute(
        text(
            "SELECT * FROM owner_human_request_waiters WHERE request_ref = "
            ":request_ref AND waiter_ref = :waiter_ref"
        ),
        {"request_ref": request_ref, "waiter_ref": waiter["waiter_ref"]},
    ).first()
    if existing is not None:
        if (
            int(existing.generation) != waiter["generation"]
            or existing.target_assertion_hash
            != canonical_hash(waiter["target_assertion"])
            or existing.wait_scope != waiter["wait_scope"]
            or existing.other_blockers_hash
            != canonical_hash(waiter["other_blockers"])
        ):
            raise OwnerConflict("human_request_waiter_conflict")
        return False
    connection.execute(
        text(
            "INSERT INTO owner_human_request_waiters (request_ref, waiter_ref, "
            "generation, target_assertion_json, target_assertion_hash, wait_scope, "
            "other_blockers_json, other_blockers_hash, status, created_at, updated_at) "
            "VALUES (:request_ref, :waiter_ref, :generation, :target_json, "
            ":target_hash, :wait_scope, :blockers_json, :blockers_hash, "
            "'blocked', :now, :now)"
        ),
        {
            "request_ref": request_ref,
            "waiter_ref": waiter["waiter_ref"],
            "generation": waiter["generation"],
            "target_json": canonical_json(waiter["target_assertion"]),
            "target_hash": canonical_hash(waiter["target_assertion"]),
            "wait_scope": waiter["wait_scope"],
            "blockers_json": canonical_json(waiter["other_blockers"]),
            "blockers_hash": canonical_hash(waiter["other_blockers"]),
            "now": now,
        },
    )
    return True


def _insert_disposition(
    connection,
    *,
    issuer: str,
    request_ref: str,
    decision: str,
    evaluation_ref: str | None,
    now: float,
) -> None:
    if decision not in HUMAN_DISPOSITIONS:
        raise OwnerConflict("human_request_disposition_invalid")
    disposition_ref = new_ref("human_disposition")
    receipt_ref = new_ref("owner_receipt")
    receipt_hash = canonical_hash(
        {
            "schema_ref": HUMAN_REQUEST_RECEIPT_SCHEMA,
            "issuer": issuer,
            "request_ref": request_ref,
            "decision": decision,
            "evaluation_ref": evaluation_ref,
        }
    )
    connection.execute(
        text(
            "INSERT INTO owner_human_request_dispositions (disposition_ref, "
            "request_ref, decision, evaluation_ref, receipt_ref, receipt_hash, "
            "created_at) VALUES (:disposition_ref, :request_ref, :decision, "
            ":evaluation_ref, :receipt_ref, :receipt_hash, :now)"
        ),
        {
            "disposition_ref": disposition_ref,
            "request_ref": request_ref,
            "decision": decision,
            "evaluation_ref": evaluation_ref,
            "receipt_ref": receipt_ref,
            "receipt_hash": receipt_hash,
            "now": now,
        },
    )


def _verified_waiter_blockers(waiter) -> tuple[str, ...]:
    try:
        blockers = json.loads(waiter.other_blockers_json)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise OwnerConflict("human_request_waiter_invalid") from error
    if (
        not isinstance(blockers, list)
        or any(not isinstance(item, str) for item in blockers)
        or canonical_hash(blockers) != waiter.other_blockers_hash
    ):
        raise OwnerConflict("human_request_waiter_invalid")
    return tuple(blockers)


def _verify_disposition_integrity(
    connection,
    *,
    issuer: str,
    request,
    disposition,
    response_verifier: HumanResponseVerifier | None,
) -> None:
    expected_receipt_hash = canonical_hash(
        {
            "schema_ref": HUMAN_REQUEST_RECEIPT_SCHEMA,
            "issuer": issuer,
            "request_ref": request.request_ref,
            "decision": disposition.decision,
            "evaluation_ref": disposition.evaluation_ref,
        }
    )
    if disposition.receipt_hash != expected_receipt_hash:
        raise OwnerConflict("human_request_disposition_invalid")
    if disposition.decision not in {"satisfied", "declined"}:
        if disposition.evaluation_ref is not None:
            raise OwnerConflict("human_request_disposition_invalid")
        return
    if disposition.evaluation_ref is None:
        raise OwnerConflict("human_request_disposition_invalid")
    evaluation = connection.execute(
        text(
            "SELECT * FROM owner_human_request_evaluations WHERE "
            "evaluation_ref = :evaluation_ref AND request_ref = :request_ref"
        ),
        {
            "evaluation_ref": disposition.evaluation_ref,
            "request_ref": request.request_ref,
        },
    ).first()
    latest_evaluation_ref = connection.execute(
        text(
            "SELECT evaluation_ref FROM owner_human_request_evaluations WHERE "
            "request_ref = :request_ref ORDER BY sequence DESC LIMIT 1"
        ),
        {"request_ref": request.request_ref},
    ).scalar_one_or_none()
    if (
        evaluation is None
        or latest_evaluation_ref != disposition.evaluation_ref
        or evaluation.decision != disposition.decision
    ):
        raise OwnerConflict("human_request_disposition_invalid")
    try:
        response_refs = json.loads(evaluation.response_refs_json)
        evidence_refs = json.loads(evaluation.evidence_refs_json)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise OwnerConflict("human_request_evaluation_invalid") from error
    if (
        not isinstance(response_refs, list)
        or not isinstance(evidence_refs, list)
        or canonical_hash(response_refs) != evaluation.response_refs_hash
        or canonical_hash(evidence_refs) != evaluation.evidence_refs_hash
    ):
        raise OwnerConflict("human_request_evaluation_invalid")
    if response_verifier is None:
        raise OwnerConflict("human_collaboration_verifier_unavailable")
    try:
        responses = tuple(
            response_verifier.verify_human_response(
                request_ref=request.request_ref,
                response_ref=response_ref,
            )
            for response_ref in response_refs
        )
    except OwnerConflict as error:
        raise OwnerConflict("human_request_evaluation_invalid") from error
    if (
        not responses
        or (
            disposition.decision == "satisfied"
            and any(item.get("decision") != "provided" for item in responses)
        )
        or (
            disposition.decision == "declined"
            and not any(item.get("decision") == "declined" for item in responses)
        )
    ):
        raise OwnerConflict("human_request_evaluation_invalid")


def _request_row(connection, issuer: str, request_ref: str):
    return connection.execute(
        text(
            "SELECT * FROM owner_human_requests WHERE request_ref = :request_ref "
            "AND issuer = :issuer"
        ),
        {"request_ref": request_ref, "issuer": issuer},
    ).first()


def verify_human_request_response_target(
    connection: Connection,
    *,
    request_ref: str,
    issuer: str,
    request_id: str,
    revision: int,
) -> None:
    """CAS the exact issuing-Owner request in the HC response write snapshot."""

    row = _request_row(connection, issuer, request_ref)
    disposition = (
        None
        if row is None
        else connection.execute(
            text(
                "SELECT * FROM owner_human_request_dispositions WHERE "
                "request_ref = :request_ref"
            ),
            {"request_ref": request_ref},
        ).first()
    )
    if row is not None:
        _verify_request_state_integrity(connection, row, disposition)
    if (
        row is None
        or row.request_id != request_id
        or int(row.revision) != revision
        or not bool(row.is_current)
        or row.status != "open"
        or (
            row.expires_at is not None
            and float(row.expires_at) <= time.time()
        )
    ):
        raise OwnerConflict("human_request_not_current")
    # Reuse the Owner's artifact verifier so a structurally current but corrupt
    # request can never become the basis of a Human Collaboration receipt.
    _public_request(row, (), None, None, {}, {}, ())


def _verify_request_state_integrity(connection: Connection, row, disposition) -> None:
    revisions = connection.execute(
        text(
            "SELECT request_ref, revision, is_current FROM owner_human_requests "
            "WHERE issuer = :issuer AND request_id = :request_id ORDER BY "
            "revision DESC, request_ref DESC"
        ),
        {"issuer": row.issuer, "request_id": row.request_id},
    ).all()
    current_refs = [item.request_ref for item in revisions if bool(item.is_current)]
    if (
        not revisions
        or current_refs != [revisions[0].request_ref]
        or bool(row.is_current) != (row.request_ref == revisions[0].request_ref)
        or (row.status == "open" and disposition is not None)
        or (
            row.status in HUMAN_DISPOSITIONS
            and (
                disposition is None
                or disposition.decision != row.status
            )
        )
        or (
            not bool(row.is_current)
            and row.status != "superseded"
        )
    ):
        raise OwnerConflict("human_request_artifact_invalid")


def _public_request(
    row,
    waiters,
    evaluation,
    disposition,
    validations,
    consumptions,
    responses: tuple[dict[str, object], ...],
) -> dict[str, object]:
    target = decoded_object(row.target_assertion_json)
    conditions = json.loads(row.acceptance_conditions_json)
    authorization = (
        None
        if row.required_authorization_json is None
        else decoded_object(row.required_authorization_json)
    )
    if (
        canonical_hash(target) != row.target_assertion_hash
        or not isinstance(conditions, list)
        or canonical_hash(conditions) != row.acceptance_conditions_hash
        or (
            authorization is not None
            and canonical_hash(authorization) != row.required_authorization_hash
        )
    ):
        raise OwnerConflict("human_request_artifact_invalid")
    contract = {
        "quest_ref": row.quest_ref,
        "kind": row.kind,
        "obligation": row.obligation,
        "business_purpose": row.business_purpose,
        "target_assertion": target,
        "acceptance_conditions": conditions,
        "required_authorization": authorization,
        "expires_at": row.expires_at,
    }
    if contains_secret(contract):
        raise OwnerConflict("human_request_secret_forbidden")
    if canonical_hash(contract) != row.identity_hash:
        raise OwnerConflict("human_request_artifact_invalid")
    public_waiters = []
    for waiter in waiters:
        waiter_target = decoded_object(waiter.target_assertion_json)
        blockers = json.loads(waiter.other_blockers_json)
        if (
            canonical_hash(waiter_target) != waiter.target_assertion_hash
            or not isinstance(blockers, list)
            or canonical_hash(blockers) != waiter.other_blockers_hash
        ):
            raise OwnerConflict("human_request_waiter_invalid")
        if contains_secret(
            {
                "target_assertion": waiter_target,
                "other_blockers": blockers,
            }
        ):
            raise OwnerConflict("human_request_secret_forbidden")
        latest = validations.get(waiter.waiter_ref)
        consumption = consumptions.get(waiter.waiter_ref)
        if (
            (waiter.status == "consumed") != (consumption is not None)
            or consumption is not None
            and (
                latest is None
                or consumption.validation_ref != latest.validation_ref
                or int(consumption.generation) != int(waiter.generation)
            )
        ):
            raise OwnerConflict("human_request_resume_consumption_invalid")
        public_consumption = (
            None
            if consumption is None
            else _public_consumption(consumption, row.issuer)
        )
        public_waiters.append(
            {
                "waiter_ref": waiter.waiter_ref,
                "generation": int(waiter.generation),
                "target_assertion": waiter_target,
                "wait_scope": waiter.wait_scope,
                "other_blockers": blockers,
                "status": waiter.status,
                "resume_validation": (
                    None
                    if latest is None
                    else _public_validation(latest, public_consumption)
                ),
            }
        )
    public_evaluation = None
    if evaluation is not None:
        response_refs = json.loads(evaluation.response_refs_json)
        evidence_refs = json.loads(evaluation.evidence_refs_json)
        if (
            canonical_hash(response_refs) != evaluation.response_refs_hash
            or canonical_hash(evidence_refs) != evaluation.evidence_refs_hash
        ):
            raise OwnerConflict("human_request_evaluation_invalid")
        if contains_secret(
            {
                "response_refs": response_refs,
                "accepted_evidence_refs": evidence_refs,
                "reason_code": evaluation.reason_code,
            }
        ):
            raise OwnerConflict("human_request_secret_forbidden")
        public_evaluation = {
            "evaluation_ref": evaluation.evaluation_ref,
            "sequence": int(evaluation.sequence),
            "decision": evaluation.decision,
            "response_refs": response_refs,
            "accepted_evidence_refs": evidence_refs,
            "reason": {"code": evaluation.reason_code},
            "created_at": float(evaluation.created_at),
        }
    public_disposition = None
    if disposition is not None:
        expected_hash = canonical_hash(
            {
                "schema_ref": HUMAN_REQUEST_RECEIPT_SCHEMA,
                "issuer": row.issuer,
                "request_ref": row.request_ref,
                "decision": disposition.decision,
                "evaluation_ref": disposition.evaluation_ref,
            }
        )
        if expected_hash != disposition.receipt_hash:
            raise OwnerConflict("human_request_disposition_invalid")
        public_disposition = {
            "disposition_ref": disposition.disposition_ref,
            "decision": disposition.decision,
            "evaluation_ref": disposition.evaluation_ref,
            "receipt": AcceptanceReceipt(
                issuer=row.issuer,
                kind="human_request_disposition",
                receipt_ref=disposition.receipt_ref,
                subject_ref=row.request_ref,
                payload_hash=disposition.receipt_hash,
            ).as_public_dict(),
            "created_at": float(disposition.created_at),
        }
    return {
        "request_ref": row.request_ref,
        "issuer": row.issuer,
        "request_id": row.request_id,
        "revision": int(row.revision),
        "quest_ref": row.quest_ref,
        "kind": row.kind,
        "obligation": row.obligation,
        "business_purpose": row.business_purpose,
        "target_assertion": target,
        "acceptance_conditions": conditions,
        "required_authorization": authorization,
        "expires_at": row.expires_at,
        "status": row.status,
        "current": bool(row.is_current),
        "direct_waiters": public_waiters,
        "responses": list(responses),
        "evaluation": public_evaluation,
        "disposition": public_disposition,
        "created_at": float(row.created_at),
        "updated_at": float(row.updated_at),
    }


def _public_validation(
    row, consumption: dict[str, object] | None = None
) -> dict[str, object]:
    blockers = json.loads(row.other_blockers_json)
    if canonical_hash(blockers) != row.other_blockers_hash:
        raise OwnerConflict("human_request_resume_validation_invalid")
    return {
        "validation_ref": row.validation_ref,
        "request_ref": row.request_ref,
        "waiter_ref": row.waiter_ref,
        "generation": int(row.generation),
        "target_assertion_hash": row.target_assertion_hash,
        "authorization_receipt_ref": row.authorization_receipt_ref,
        "other_blockers": blockers,
        "status": row.status,
        "reason": (
            None if row.reason_code is None else {"code": row.reason_code}
        ),
        "started_work": consumption is not None,
        "consumption": consumption,
        "created_at": float(row.created_at),
    }


def _resume_consumption_receipt_hash(
    *,
    issuer: str,
    consumption_ref: str,
    request_ref: str,
    waiter_ref: str,
    generation: int,
    validation_ref: str,
    work_ref: str,
    work_hash: str,
) -> str:
    return canonical_hash(
        {
            "schema_ref": HUMAN_REQUEST_RESUME_CONSUMPTION_RECEIPT_SCHEMA,
            "issuer": issuer,
            "consumption_ref": consumption_ref,
            "request_ref": request_ref,
            "waiter_ref": waiter_ref,
            "generation": generation,
            "validation_ref": validation_ref,
            "work_ref": work_ref,
            "work_hash": work_hash,
        }
    )


def _public_consumption(row, issuer: str) -> dict[str, object]:
    expected_hash = _resume_consumption_receipt_hash(
        issuer=issuer,
        consumption_ref=row.consumption_ref,
        request_ref=row.request_ref,
        waiter_ref=row.waiter_ref,
        generation=int(row.generation),
        validation_ref=row.validation_ref,
        work_ref=row.work_ref,
        work_hash=row.work_hash,
    )
    if row.receipt_hash != expected_hash:
        raise OwnerConflict("human_request_resume_consumption_invalid")
    return {
        "consumption_ref": row.consumption_ref,
        "request_ref": row.request_ref,
        "waiter_ref": row.waiter_ref,
        "generation": int(row.generation),
        "validation_ref": row.validation_ref,
        "work_ref": row.work_ref,
        "work_hash": row.work_hash,
        "receipt": AcceptanceReceipt(
            issuer=issuer,
            kind="human_request_resume_consumption",
            receipt_ref=row.receipt_ref,
            subject_ref=row.consumption_ref,
            payload_hash=row.receipt_hash,
        ).as_public_dict(),
        "created_at": float(row.created_at),
    }


def _command_replay(
    connection,
    issuer: str,
    idempotency_key: str,
    command_kind: str,
    request_hash: str,
) -> str | None:
    row = connection.execute(
        text(
            "SELECT * FROM owner_human_request_commands WHERE issuer = :issuer "
            "AND idempotency_key = :idempotency_key"
        ),
        {"issuer": issuer, "idempotency_key": idempotency_key},
    ).first()
    if row is None:
        return None
    if row.command_kind != command_kind or row.request_hash != request_hash:
        raise OwnerConflict("idempotency_conflict")
    return cast(str, row.result_ref)


def _record_command(
    connection,
    issuer: str,
    idempotency_key: str,
    command_kind: str,
    request_hash: str,
    result_ref: str,
) -> None:
    connection.execute(
        text(
            "INSERT INTO owner_human_request_commands (issuer, idempotency_key, "
            "command_kind, request_hash, result_ref, recorded_at) VALUES "
            "(:issuer, :idempotency_key, :command_kind, :request_hash, "
            ":result_ref, :recorded_at)"
        ),
        {
            "issuer": issuer,
            "idempotency_key": idempotency_key,
            "command_kind": command_kind,
            "request_hash": request_hash,
            "result_ref": result_ref,
            "recorded_at": time.time(),
        },
    )


def _validate_idempotency_key(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or contains_secret(value)
    ):
        raise OwnerConflict("idempotency_key_invalid")


def _bounded_text(value: object, code: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise OwnerConflict(code)
    return value.strip()


def _object(value: object, code: str) -> dict[str, object]:
    if not isinstance(value, dict) or not value:
        raise OwnerConflict(code)
    try:
        encoded = canonical_json(value)
    except (TypeError, ValueError) as error:
        raise OwnerConflict(code) from error
    if len(encoded.encode("utf-8")) > 64 * 1024:
        raise OwnerConflict(code)
    return cast(dict[str, object], json.loads(encoded))


def _unique_texts(
    values: tuple[str, ...],
    code: str,
    *,
    maximum_items: int,
    maximum_length: int,
) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > maximum_items:
        raise OwnerConflict(code)
    normalized = tuple(_bounded_text(item, code, maximum_length) for item in values)
    if len(set(normalized)) != len(normalized):
        raise OwnerConflict(code)
    return normalized


def _unique_refs(values: tuple[str, ...], code: str) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > 100:
        raise OwnerConflict(code)
    normalized = tuple(_bounded_text(item, code, 128) for item in values)
    if len(set(normalized)) != len(normalized):
        raise OwnerConflict(code)
    return normalized
