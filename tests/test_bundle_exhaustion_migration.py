from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic.operations import Operations

from meta_research.migration import upgrade_database
from test_plan_stage_migration import _upgrade_to_revision


def test_target_graph_rejection_ledger_migration_is_atomic_and_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "bundle-target-graph-rejection.sqlite3"
    _upgrade_to_revision(database, "0023_bundle_exhaustion_proposal")
    original_create_table = Operations.create_table
    interrupted = False

    def interrupt_after_rejection_table(self, table_name, *args, **kwargs):
        nonlocal interrupted
        created = original_create_table(self, table_name, *args, **kwargs)
        if table_name == "rg_target_graph_rejections" and not interrupted:
            interrupted = True
            raise OSError("injected TargetGraph rejection migration interruption")
        return created

    monkeypatch.setattr(
        Operations,
        "create_table",
        interrupt_after_rejection_table,
    )
    with pytest.raises(OSError, match="TargetGraph rejection migration interruption"):
        upgrade_database(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0023_bundle_exhaustion_proposal",)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        graph_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(research_graph_state)")
        }
    assert "rg_target_graph_rejections" not in tables
    assert "target_graph_rejection_count" not in graph_columns

    monkeypatch.setattr(Operations, "create_table", original_create_table)
    _upgrade_to_revision(database, "0024_bundle_target_graph_rejection")
    _upgrade_to_revision(database, "0024_bundle_target_graph_rejection")

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0024_bundle_target_graph_rejection",)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        graph_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(research_graph_state)")
        }
        foreign_key_failures = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()
        integrity = connection.execute("PRAGMA quick_check").fetchone()
    assert "rg_target_graph_rejections" in tables
    assert "target_graph_rejection_count" in graph_columns
    assert foreign_key_failures == []
    assert integrity == ("ok",)
