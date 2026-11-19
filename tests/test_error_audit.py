from __future__ import annotations

from dataclasses import replace

import pytest

from meta_research.database import Database
from meta_research.error_audit import ErrorAuditConflict, ErrorAuditStore
from meta_research.error_contract import error_contract_from_legacy
from meta_research.feed import DurableFeed


def _contract():
    return error_contract_from_legacy(
        "semantic_operation_failed",
        {"effect_id": "effect-1"},
        operation_id="agent_runtime.target_work.request",
        operation_ref="operation-1",
        effect=True,
        correlation_ref="correlation-1",
    )


def _database(path):
    database = Database(path)
    with database.write() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE durable_feed (revision INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at REAL NOT NULL)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE projection_offsets (projection_name TEXT PRIMARY KEY, revision INTEGER NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO projection_offsets (projection_name, revision) VALUES ('public_snapshot', 0)"
        )
    return database


def test_error_audit_is_idempotent_and_survives_restart(tmp_path) -> None:
    path = tmp_path / "audit.sqlite3"
    first_database = _database(path)
    first_feed = DurableFeed(first_database)
    first_feed.ensure_initialized()
    first_store = ErrorAuditStore(first_database, first_feed)

    first = first_store.record(_contract())
    replay = first_store.record(_contract())

    restarted_database = Database(path)
    restarted_store = ErrorAuditStore(restarted_database, DurableFeed(restarted_database))
    observed = restarted_store.query(first.audit_ref)

    assert replay == first
    assert observed == first
    assert restarted_store.query_correlation("correlation-1") == (first,)
    first_database.close()
    restarted_database.close()


def test_error_audit_rejects_identity_reuse_with_changed_payload(tmp_path) -> None:
    path = tmp_path / "audit-conflict.sqlite3"
    database = _database(path)
    feed = DurableFeed(database)
    feed.ensure_initialized()
    store = ErrorAuditStore(database, feed)
    first = _contract()
    store.record(first)

    changed = replace(first, phase="output_validation")
    with pytest.raises(ErrorAuditConflict, match="error_audit_idempotency_conflict"):
        store.record(changed)
    database.close()


def test_field_diagnostics_keep_distinct_receipts_and_replay_old_fact(tmp_path) -> None:
    def diagnostic(field, expected, **details):
        return error_contract_from_legacy(
            "semantic_input_schema_mismatch",
            {"field_path": [field], "expected": expected, **details},
            phase="input_validation",
            operation_id="research_memory.plan_evidence.read",
            correlation_ref="plan-session",
        )

    path = tmp_path / "field-errors.sqlite3"
    database = _database(path)
    store = ErrorAuditStore(database)
    legacy_ref = "error:b3f66188d7ca999efe7fa86c3b38a04b"
    historical = store.record(diagnostic("target_commit_ref", "present", audit_ref=legacy_ref))
    facts = [
        diagnostic("target_commit_ref", "present"),
        diagnostic("asset_version_ref", "present"),
        diagnostic("asset_version_ref", "present", operator_required=True),
        diagnostic("role_ref", {"reader": {"input": {"kind": "artifact"}}}),
        diagnostic("role_ref", {"reader": {"input": {"kind": "metadata"}}}),
        diagnostic("target_commit_ref", "present", audit_ref="error:caller-assigned"),
    ]
    recorded = [store.record(fact) for fact in facts]
    assert recorded[0] == historical
    assert recorded[-1].audit_ref == "error:caller-assigned"
    assert len({item.audit_ref for item in recorded}) == len(facts)
    database.close()

    reopened = Database(path)
    try:
        store = ErrorAuditStore(reopened)
        assert [store.record(fact) for fact in facts] == recorded
        assert store.query(legacy_ref) == historical
        assert store.query_correlation("plan-session") == tuple(recorded)
    finally:
        reopened.close()

