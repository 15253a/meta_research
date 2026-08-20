from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic.operations import Operations

from meta_research.migration import upgrade_database


def test_interrupted_sqlite_ddl_rolls_back_and_upgrade_can_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "interrupted.sqlite3"
    original_create_table = Operations.create_table
    failed_once = False

    def fail_during_quest_migration(self, table_name, *args, **kwargs):
        nonlocal failed_once
        if table_name == "hc_quest_initializations" and not failed_once:
            failed_once = True
            raise OSError("injected migration interruption")
        return original_create_table(self, table_name, *args, **kwargs)

    monkeypatch.setattr(Operations, "create_table", fail_during_quest_migration)
    with pytest.raises(OSError, match="injected migration interruption"):
        upgrade_database(database)

    with sqlite3.connect(database) as connection:
        tables_after_failure = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "research_memory_state" in tables_after_failure:
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(research_memory_state)")
            }
            assert "formal_content_count" not in columns
        assert "hc_quest_initializations" not in tables_after_failure

    monkeypatch.setattr(Operations, "create_table", original_create_table)
    upgrade_database(database)
    upgrade_database(database)

    with sqlite3.connect(database) as connection:
        version = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(research_memory_state)")
        }
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert version == ("0002_quest_initialization",)
    assert "formal_content_count" in columns
    assert "hc_quest_initializations" in tables
