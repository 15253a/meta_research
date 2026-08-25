from __future__ import annotations

import sqlite3
from pathlib import Path

from meta_research.migration import upgrade_database
from meta_research.paths import prepare_data_root
from test_migration_recovery import _upgrade_to_revision


_HARNESS_TABLES = {
    "ar_harness_runs",
    "ar_mcp_channel_grants",
    "ar_harness_provider_operations",
    "ar_harness_evidence_events",
}


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def test_existing_0015_data_upgrades_to_one_durable_harness_run_model(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "upgrade-from-0015")
    _upgrade_to_revision(data_root.database, "0015_writing_report")
    with sqlite3.connect(data_root.database) as connection:
        before_owner_columns = tuple(
            row[1]
            for row in connection.execute("PRAGMA table_info(agent_runtime_state)")
        )
        before_owner_rows = connection.execute(
            f"SELECT {', '.join(before_owner_columns)} FROM agent_runtime_state "
            "ORDER BY singleton"
        ).fetchall()
        before_experiment_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name LIKE '%experiment%'"
            )
        }

    upgrade_database(data_root.database)
    upgrade_database(data_root.database)

    with sqlite3.connect(data_root.database) as connection:
        version = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        after_owner_rows = connection.execute(
            f"SELECT {', '.join(before_owner_columns)} FROM agent_runtime_state "
            "ORDER BY singleton"
        ).fetchall()
        after_experiment_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name LIKE '%experiment%'"
            )
        }
        foreign_key_failures = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()

        assert version == ("0035_runtime_protection",)
        assert _HARNESS_TABLES <= tables
        assert "ar_harness_sessions" not in tables
        assert "ar_harness_attempts" not in tables
        assert {
            "run_ref",
            "attempt_ref",
            "attempt_generation",
            "root_session_ref",
            "native_session_ref",
            "fence_ref",
            "capability_binding_hash",
            "mcp_binding_hash",
            "profile_hash",
        } <= _columns(connection, "ar_harness_runs")
        assert {
            "token_hash",
            "scope_hash",
            "status",
            "revoked_at",
        } <= _columns(connection, "ar_mcp_channel_grants")

    assert after_owner_rows == before_owner_rows
    assert after_experiment_tables == before_experiment_tables
    assert foreign_key_failures == []
    assert integrity == ("ok",)
