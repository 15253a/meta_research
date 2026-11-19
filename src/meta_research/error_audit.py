"""Durable, idempotent audit storage for unified error facts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

from sqlalchemy import text
from sqlalchemy.engine import Connection

from meta_research.database import Database
from meta_research.error_contract import ErrorContract
from meta_research.feed import DurableFeed


ERROR_AUDIT_EVENT_TYPE = "system.error_contract"
ERROR_AUDIT_SCHEMA_REF = "meta-research/error-audit/v1"


class ErrorAuditError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ErrorAuditConflict(ErrorAuditError):
    """The same audit identity was reused for a different error fact."""


@dataclass(frozen=True)
class ErrorAuditRecord:
    revision: int
    audit_ref: str
    correlation_ref: str | None
    payload_hash: str
    payload: dict[str, Any]

    def as_public_dict(self) -> dict[str, Any]:
        return {"revision": self.revision, **self.payload}


class ErrorAuditStore:
    """Append-only error audit backed by the existing DurableFeed."""

    def __init__(self, database: Database, feed: DurableFeed | None = None) -> None:
        self._database = database
        self._feed = feed or DurableFeed(database)

    def record(self, contract: ErrorContract) -> ErrorAuditRecord:
        with self._database.fenced_write() as connection:
            return self.record_in_transaction(connection, contract)

    def record_in_transaction(
        self, connection: Connection, contract: ErrorContract
    ) -> ErrorAuditRecord:
        payload = _payload_for(contract)
        existing = self._find_in_connection(connection, contract.audit_ref)
        if existing is not None:
            if existing.payload_hash != payload["payload_hash"]:
                raise ErrorAuditConflict("error_audit_idempotency_conflict")
            return existing
        revision = self._feed.record(connection, ERROR_AUDIT_EVENT_TYPE, payload)
        return _record_from_payload(revision, payload)

    def query(self, audit_ref: str) -> ErrorAuditRecord | None:
        if not isinstance(audit_ref, str) or not audit_ref:
            raise ErrorAuditError("error_audit_identity_invalid")
        for event in self._feed.read_event_type(ERROR_AUDIT_EVENT_TYPE):
            if event.payload.get("audit_ref") != audit_ref:
                continue
            return _record_from_payload(event.revision, event.payload)
        return None

    def query_correlation(self, correlation_ref: str) -> tuple[ErrorAuditRecord, ...]:
        if not isinstance(correlation_ref, str) or not correlation_ref:
            raise ErrorAuditError("error_audit_correlation_invalid")
        return tuple(
            _record_from_payload(event.revision, event.payload)
            for event in self._feed.read_event_type(ERROR_AUDIT_EVENT_TYPE)
            if event.payload.get("correlation_ref") == correlation_ref
        )

    def _find_in_connection(
        self, connection: Connection, audit_ref: str | None
    ) -> ErrorAuditRecord | None:
        if not isinstance(audit_ref, str) or not audit_ref:
            raise ErrorAuditError("error_audit_identity_invalid")
        rows = connection.execute(
            text(
                "SELECT revision, payload_json FROM durable_feed "
                "WHERE event_type = :event_type ORDER BY revision"
            ),
            {"event_type": ERROR_AUDIT_EVENT_TYPE},
        ).all()
        for row in rows:
            try:
                payload = json.loads(row.payload_json)
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise ErrorAuditError("error_audit_record_invalid") from error
            if isinstance(payload, dict) and payload.get("audit_ref") == audit_ref:
                return _record_from_payload(int(row.revision), payload)
        return None


def _payload_for(contract: ErrorContract) -> dict[str, Any]:
    if not isinstance(contract, ErrorContract):
        raise ErrorAuditError("error_audit_contract_invalid")
    public = contract.as_public_dict()
    details = public["details"]
    payload_without_hash: dict[str, Any] = {
        "schema_ref": ERROR_AUDIT_SCHEMA_REF,
        "event_type": ERROR_AUDIT_EVENT_TYPE,
        "audit_ref": contract.audit_ref,
        "correlation_ref": contract.correlation_ref,
        "operation_id": contract.operation_id,
        "operation_ref": contract.operation_ref,
        "effect_id": contract.effect_id,
        "phase": contract.phase,
        "effect_status": contract.effect_status.value,
        "route": contract.category,
        "stable_code": details["stable_code"],
        "legacy_code": details["legacy_code"],
        "contract": public,
    }
    payload_without_hash["payload_hash"] = _payload_hash(payload_without_hash)
    return payload_without_hash


def _payload_hash(payload: Mapping[str, Any]) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _record_from_payload(revision: int, payload: Mapping[str, Any]) -> ErrorAuditRecord:
    required = {
        "schema_ref", "event_type", "audit_ref", "correlation_ref",
        "operation_id", "operation_ref", "effect_id", "phase", "effect_status",
        "route", "stable_code", "legacy_code", "contract", "payload_hash",
    }
    if (
        set(payload) != required
        or payload.get("schema_ref") != ERROR_AUDIT_SCHEMA_REF
        or payload.get("event_type") != ERROR_AUDIT_EVENT_TYPE
        or not isinstance(payload.get("audit_ref"), str)
        or not isinstance(payload.get("contract"), dict)
        or not isinstance(payload.get("payload_hash"), str)
        or len(payload["payload_hash"]) != 64
        or _payload_hash({key: payload[key] for key in required if key != "payload_hash"})
        != payload["payload_hash"]
    ):
        raise ErrorAuditError("error_audit_record_invalid")
    return ErrorAuditRecord(
        revision=revision,
        audit_ref=payload["audit_ref"],
        correlation_ref=(
            payload["correlation_ref"]
            if isinstance(payload.get("correlation_ref"), str)
            else None
        ),
        payload_hash=payload["payload_hash"],
        payload=dict(payload),
    )


__all__ = [
    "ERROR_AUDIT_EVENT_TYPE",
    "ERROR_AUDIT_SCHEMA_REF",
    "ErrorAuditConflict",
    "ErrorAuditError",
    "ErrorAuditRecord",
    "ErrorAuditStore",
]
