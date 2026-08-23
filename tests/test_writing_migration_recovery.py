from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic.operations import Operations

from meta_research.migration import upgrade_database
from test_migration_recovery import _downgrade_to_revision, _upgrade_to_revision


_AR_COUNTERS = {
    "writing_run_count",
    "writing_attempt_count",
    "writing_session_count",
    "active_writing_run_count",
}
_RG_COUNTERS = {
    "writing_citation_decision_count",
    "writing_citation_rejection_count",
}
_TABLES = {
    "ar_writing_runs",
    "ar_writing_attempts",
    "ar_writing_checkpoints",
    "ar_writing_executions",
    "ar_writing_commands",
    "rg_writing_citation_decisions",
}


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def test_interrupted_0015_rolls_back_then_retries_without_losing_0014_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "interrupted-writing.sqlite3"
    _upgrade_to_revision(database, "0014_advancement_runtime_control")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO ar_run_controls (run_ref, run_kind, quest_ref, cycle_ref, "
            "epoch, status, attempt_ref, root_session_ref, fence_ref, "
            "control_revision, safe_point_ref, terminal_reason, cleanup_status, "
            "updated_at) VALUES ('migration-provider-run', 'experiment', NULL, "
            "NULL, NULL, 'running', 'migration-attempt', 'migration-root', "
            "'migration-fence', 1, NULL, NULL, 'none', 129.0)"
        )
        connection.execute(
            "INSERT INTO ar_provider_units (unit_ref, operation_ref, run_ref, "
            "attempt_ref, fence_ref, unit_kind, status, started_at, completed_at) "
            "VALUES ('migration-provider-unit', 'migration-operation', "
            "'migration-provider-run', 'migration-attempt', 'migration-fence', "
            "'experiment', 'active', 129.0, NULL)"
        )
        connection.execute(
            "INSERT INTO durable_feed "
            "(revision, event_type, payload_json, recorded_at) "
            "VALUES (1, 'before.0015', '{}', 130.0)"
        )
        connection.commit()

    original_create_table = Operations.create_table
    failed_once = False

    def fail_during_writing_migration(self, table_name, *args, **kwargs):
        nonlocal failed_once
        if table_name == "ar_writing_runs" and not failed_once:
            failed_once = True
            raise OSError("injected 0015 migration interruption")
        return original_create_table(self, table_name, *args, **kwargs)

    monkeypatch.setattr(Operations, "create_table", fail_during_writing_migration)
    with pytest.raises(OSError, match="injected 0015 migration interruption"):
        _upgrade_to_revision(database, "0015_writing_report")

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0014_advancement_runtime_control",)
        assert not (_AR_COUNTERS & _columns(connection, "agent_runtime_state"))
        assert not (_RG_COUNTERS & _columns(connection, "research_graph_state"))
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert not (_TABLES & tables)
        assert connection.execute(
            "SELECT event_type, payload_json, recorded_at FROM durable_feed "
            "WHERE revision = 1"
        ).fetchone() == ("before.0015", "{}", 130.0)

    monkeypatch.setattr(Operations, "create_table", original_create_table)
    _upgrade_to_revision(database, "0015_writing_report")
    _upgrade_to_revision(database, "0015_writing_report")

    with sqlite3.connect(database) as connection:
        version = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()
        ar_columns = _columns(connection, "agent_runtime_state")
        rg_columns = _columns(connection, "research_graph_state")
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        ar_values = connection.execute(
            f"SELECT {', '.join(sorted(_AR_COUNTERS))} FROM agent_runtime_state "
            "WHERE singleton = 'owner'"
        ).fetchone()
        rg_values = connection.execute(
            f"SELECT {', '.join(sorted(_RG_COUNTERS))} FROM research_graph_state "
            "WHERE singleton = 'owner'"
        ).fetchone()
        preserved = connection.execute(
            "SELECT event_type, payload_json, recorded_at FROM durable_feed "
            "WHERE revision = 1"
        ).fetchone()
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        integrity = connection.execute("PRAGMA quick_check").fetchone()
        provider_unit = connection.execute(
            "SELECT operation_ref, run_ref, attempt_ref, fence_ref, unit_kind, "
            "status, started_at, completed_at FROM ar_provider_units WHERE "
            "unit_ref = 'migration-provider-unit'"
        ).fetchone()
        connection.execute(
            "INSERT INTO ar_provider_units (unit_ref, operation_ref, run_ref, "
            "attempt_ref, fence_ref, unit_kind, status, started_at, completed_at) "
            "VALUES ('migration-writing-primary', 'migration-writing-operation', "
            "'migration-provider-run', 'migration-writing-attempt', "
            "'migration-writing-fence', 'writing_primary', 'active', 131.0, NULL)"
        )
        connection.execute(
            "INSERT INTO ar_provider_units (unit_ref, operation_ref, run_ref, "
            "attempt_ref, fence_ref, unit_kind, status, started_at, completed_at) "
            "VALUES ('migration-writing-review', 'migration-writing-operation', "
            "'migration-provider-run', 'migration-writing-attempt', "
            "'migration-writing-fence', 'writing_review', 'completed', 131.0, 132.0)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO ar_provider_units (unit_ref, operation_ref, run_ref, "
                "unit_kind, status, started_at) VALUES ('migration-invalid-unit', "
                "'migration-invalid-operation', 'migration-provider-run', "
                "'writing_unknown', 'active', 133.0)"
            )

    assert version == ("0015_writing_report",)
    assert _AR_COUNTERS <= ar_columns
    assert _RG_COUNTERS <= rg_columns
    assert _TABLES <= tables
    assert ar_values == (0,) * len(_AR_COUNTERS)
    assert rg_values == (0,) * len(_RG_COUNTERS)
    assert preserved == ("before.0015", "{}", 130.0)
    assert foreign_keys == []
    assert integrity == ("ok",)
    assert provider_unit == (
        "migration-operation",
        "migration-provider-run",
        "migration-attempt",
        "migration-fence",
        "experiment",
        "active",
        129.0,
        None,
    )


def test_0015_is_forward_only(tmp_path: Path) -> None:
    database = tmp_path / "forward-only-writing.sqlite3"
    _upgrade_to_revision(database, "0015_writing_report")

    with pytest.raises(RuntimeError, match="forward-only"):
        _downgrade_to_revision(database, "0014_advancement_runtime_control")

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0015_writing_report",)
