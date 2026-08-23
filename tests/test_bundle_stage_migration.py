from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic.operations import Operations

from meta_research.migration import upgrade_database
from test_plan_stage_migration import _upgrade_to_revision


def test_interrupted_bundle_migration_rolls_back_and_restarts_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "bundle-migration.sqlite3"
    _upgrade_to_revision(database, "0012_experiment_measurement")
    original_create_table = Operations.create_table
    failed_once = False

    def fail_after_target_table(self, table_name, *args, **kwargs):
        nonlocal failed_once
        created = original_create_table(self, table_name, *args, **kwargs)
        if table_name == "rg_targets" and not failed_once:
            failed_once = True
            raise OSError("injected bundle migration interruption")
        return created

    monkeypatch.setattr(Operations, "create_table", fail_after_target_table)
    with pytest.raises(OSError, match="bundle migration interruption"):
        upgrade_database(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0012_experiment_measurement",)
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
        experiment_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(ar_experiment_runs)")
        }
        request_ddl = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND "
            "name = 'ae_stage_run_requests'"
        ).fetchone()[0]
    assert "rg_target_graphs" not in tables
    assert "rg_targets" not in tables
    assert "ae_stage_run_requests_pre_bundle" not in tables
    assert "target_graph_count" not in graph_columns
    assert "bundle_target_ref" not in experiment_columns
    assert "'bundle'" not in request_ddl

    monkeypatch.setattr(Operations, "create_table", original_create_table)
    _upgrade_to_revision(database, "0013_bundle_target_dag")
    _upgrade_to_revision(database, "0013_bundle_target_dag")

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0013_bundle_target_dag",)
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
        experiment_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(ar_experiment_runs)")
        }
        request_ddl = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND "
            "name = 'ae_stage_run_requests'"
        ).fetchone()[0]
        foreign_key_failures = connection.execute("PRAGMA foreign_key_check").fetchall()
        integrity = connection.execute("PRAGMA quick_check").fetchone()
    assert {
        "ar_bundle_dispatch_decisions",
        "ar_target_run_admissions",
        "rg_target_graphs",
        "rg_targets",
        "rg_target_run_bindings",
        "rg_target_commits",
    } <= tables
    assert {
        "target_graph_count",
        "target_count",
        "target_commit_count",
    } <= graph_columns
    assert "bundle_target_ref" in experiment_columns
    assert "'bundle'" in request_ddl
    assert foreign_key_failures == []
    assert integrity == ("ok",)
