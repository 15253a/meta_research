"""0085 storage invariants; full old Owner-chain upgrade has a separate Linux probe."""

import sqlite3

import pytest

from meta_research.migration import upgrade_database
from meta_research.owners.common import canonical_hash, canonical_json
from migration_support import schema_head
from test_migration_recovery import _upgrade_to_revision


def _reservation(ordinal=1, status="active"):
    """Storage-only fixture, deliberately not a fabricated Harness authority."""
    ref = f"issue296-migration-workspace-{ordinal}"
    payload = {
        "target_ref": "issue296-migration-target", "target_run_ref": "issue296-migration-run",
        "root_session_ref": f"issue296-origin-root-{ordinal}",
        "target_attempt_ref": f"issue296-origin-attempt-{ordinal}",
        "target_fence_ref": f"issue296-origin-fence-{ordinal}",
        "implementation_relative_path": "implementation", "inputs_relative_path": "inputs",
    }
    root_name = canonical_hash({"workspace_ref": ref})
    receipt_ref = f"issue296-reservation-receipt-{ordinal}"
    return {
        "workspace_ref": ref, **{key: payload[key] for key in (
            "target_ref", "target_run_ref", "root_session_ref", "target_attempt_ref", "target_fence_ref")},
        "ordinal": ordinal, "root_name": root_name, "status": status,
        "payload_json": canonical_json(payload), "payload_hash": canonical_hash(payload),
        "idempotency_key": f"issue296-reserve-{ordinal}",
        "request_hash": canonical_hash({"command": "reserve_target_run_workspace", **payload}),
        "receipt_ref": receipt_ref,
        "receipt_hash": canonical_hash({"issuer": "agent_runtime", "kind": "target_run_workspace_reserved",
            "receipt_ref": receipt_ref, "subject_ref": ref,
            "bindings": {**payload, "workspace_ref": ref, "ordinal": ordinal,
                "root_name": root_name, "payload_hash": canonical_hash(payload)}}),
        "created_at": 296.0,
    }


def _insert(connection, row):
    connection.execute("INSERT INTO ar_target_run_workspaces (" + ",".join(row) + ") VALUES ("
                       + ",".join(":" + key for key in row) + ")", row)


def test_upgrade_removes_copy_protocol_and_preserves_original_receipt_bytes(tmp_path):
    path = tmp_path / "issue296-reservation.sqlite3"
    _upgrade_to_revision(path, "0084_target_unknown_failure")
    with sqlite3.connect(path) as connection:
        _insert(connection, _reservation())
        before = connection.execute("SELECT * FROM ar_target_run_workspaces").fetchall()
        owner = connection.execute("SELECT * FROM agent_runtime_state").fetchall()
    upgrade_database(path)
    upgrade_database(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT * FROM ar_target_run_workspaces").fetchall() == before
        assert connection.execute("SELECT * FROM agent_runtime_state").fetchall() == owner
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (schema_head(),)
        assert not connection.execute("SELECT name FROM sqlite_master WHERE name='ar_target_root_workspace_continuities'").fetchall()
        columns = {row[1] for row in connection.execute("PRAGMA table_info(ar_target_root_provider_recoveries)")}
        assert "retired_workspace_ref" not in columns
        assert {"failed_provider_operation_ref", "transport_receipt_hash", "provider_evidence_hash",
                "successor_reservation_hash", "recovery_evidence_refs_hash"} <= columns
        with pytest.raises(sqlite3.IntegrityError, match="ar_target_run_workspaces.target_run_ref"):
            _insert(connection, _reservation(2))
        # Retained historical directory facts are still legal storage; they are
        # never selected as the new active execution directory.
        _insert(connection, _reservation(2, "retired"))


@pytest.mark.parametrize("rows", (
    (_reservation(1, "retired"),),
    (_reservation(1), _reservation(2)),
    (_reservation(1, "retired"), _reservation(2)),
    (_reservation(2),),
))
def test_upgrade_rejects_unfinished_or_unproved_old_copy_atomically(tmp_path, rows):
    path = tmp_path / "issue296-unfinished-copy.sqlite3"
    _upgrade_to_revision(path, "0084_target_unknown_failure")
    with sqlite3.connect(path) as connection:
        for row in rows:
            _insert(connection, row)
        before = connection.execute("SELECT * FROM ar_target_run_workspaces ORDER BY ordinal").fetchall()
    with pytest.raises(RuntimeError, match="target_workspace_migration_incomplete"):
        upgrade_database(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT * FROM ar_target_run_workspaces ORDER BY ordinal").fetchall() == before
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0084_target_unknown_failure",)
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='ar_target_root_workspace_continuities'").fetchone()
        assert not connection.execute("SELECT name FROM sqlite_master WHERE name='uq_target_run_active_workspace'").fetchall()
