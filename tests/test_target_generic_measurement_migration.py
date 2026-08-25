from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic.operations import Operations

from meta_research.migration import upgrade_database
from test_plan_stage_migration import _upgrade_to_revision


def test_target_generic_measurement_migration_is_atomic_and_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "target-generic-measurement.sqlite3"
    _upgrade_to_revision(database, "0024_bundle_target_graph_rejection")
    original_create_table = Operations.create_table
    interrupted = False

    def interrupt_after_usage_table(self, table_name, *args, **kwargs):
        nonlocal interrupted
        created = original_create_table(self, table_name, *args, **kwargs)
        if (
            table_name == "rm_target_implementation_bundle_usages"
            and not interrupted
        ):
            interrupted = True
            raise OSError("injected Target generic migration interruption")
        return created

    monkeypatch.setattr(Operations, "create_table", interrupt_after_usage_table)
    with pytest.raises(
        OSError, match="Target generic migration interruption"
    ):
        upgrade_database(database)

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
        memory_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(research_memory_state)")
        }
        runtime_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(agent_runtime_state)")
        }
    assert "rm_target_implementation_bundles" not in tables
    assert "rm_target_implementation_bundle_usages" not in tables
    assert "target_implementation_bundle_count" not in memory_columns
    assert "target_run_workspace_count" not in runtime_columns

    monkeypatch.setattr(Operations, "create_table", original_create_table)
    _upgrade_to_revision(database, "0025_target_generic_measurement")
    _upgrade_to_revision(database, "0025_target_generic_measurement")

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0025_target_generic_measurement",)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        foreign_key_failures = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()
        integrity = connection.execute("PRAGMA quick_check").fetchone()
        unique_indexes: dict[str, set[tuple[str, ...]]] = {}
        for table in (
            "rm_target_implementation_bundles",
            "rg_target_generic_measurements",
            "ar_target_generic_execution_closures",
        ):
            unique_indexes[table] = {
                tuple(
                    column[2]
                    for column in connection.execute(
                        f"PRAGMA index_info('{index[1]}')"
                    )
                )
                for index in connection.execute(f"PRAGMA index_list('{table}')")
                if index[2]
            }
    assert {
        "ar_target_run_workspaces",
        "rm_target_implementation_bundles",
        "rm_target_implementation_bundle_usages",
        "ar_target_execution_eligibilities_v3",
        "rg_target_generic_execution_bindings_v3",
        "rm_target_generic_result_manifests",
        "rg_target_generic_measurements",
        "ar_target_generic_execution_closures",
    }.issubset(tables)
    assert ("bundle_content_hash",) not in unique_indexes[
        "rm_target_implementation_bundles"
    ]
    assert ("asset_ref",) not in unique_indexes[
        "rm_target_implementation_bundles"
    ]
    assert ("version_ref",) not in unique_indexes[
        "rm_target_implementation_bundles"
    ]
    assert ("target_ref",) not in unique_indexes[
        "rg_target_generic_measurements"
    ]
    assert ("target_ref",) not in unique_indexes[
        "ar_target_generic_execution_closures"
    ]
    assert ("target_attempt_ref",) in unique_indexes[
        "rg_target_generic_measurements"
    ]
    assert ("target_attempt_ref",) in unique_indexes[
        "ar_target_generic_execution_closures"
    ]
    assert foreign_key_failures == []
    assert integrity == ("ok",)

    # 0026 is independently deployable on top of this exact 0025 head.
    _upgrade_to_revision(database, "0026_bundle_inbox_runtime")
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0026_bundle_inbox_runtime",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
