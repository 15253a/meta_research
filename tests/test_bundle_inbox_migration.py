from __future__ import annotations

import sqlite3
from importlib.resources import files
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.operations import Operations
from sqlalchemy import URL, create_engine, event

from meta_research.migration import (
    _begin_migration_transaction,
    _configure_migration_sqlite,
    upgrade_database,
)


def _upgrade_to_revision(database: Path, revision: str) -> None:
    engine = create_engine(
        URL.create("sqlite+pysqlite", database=str(database)), future=True
    )
    event.listen(engine, "connect", _configure_migration_sqlite)
    event.listen(engine, "begin", _begin_migration_transaction)
    config = Config()
    config.set_main_option(
        "script_location", str(files("meta_research.migrations"))
    )
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, revision)
    finally:
        engine.dispose()


def test_bundle_inbox_runtime_migration_is_atomic_and_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "bundle-inbox-runtime.sqlite3"
    _upgrade_to_revision(database, "0025_target_generic_measurement")
    original_create_table = Operations.create_table
    failed_once = False

    def interrupt(self, table_name, *args, **kwargs):
        nonlocal failed_once
        created = original_create_table(self, table_name, *args, **kwargs)
        if table_name == "ar_bundle_inbox_entries" and not failed_once:
            failed_once = True
            raise OSError("injected Bundle inbox migration interruption")
        return created

    monkeypatch.setattr(Operations, "create_table", interrupt)
    with pytest.raises(OSError, match="Bundle inbox migration interruption"):
        upgrade_database(database)

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
    assert "ar_bundle_inbox_checkpoints" not in tables
    assert "ar_bundle_inbox_scopes" not in tables
    assert "ar_bundle_inbox_entries" not in tables
    assert "ar_bundle_inbox_operation_checkpoints" not in tables

    monkeypatch.setattr(Operations, "create_table", original_create_table)
    upgrade_database(database)
    upgrade_database(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0030_runtime_protection",)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        foreign_key_failures = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()
        quick_check = connection.execute("PRAGMA quick_check").fetchone()

    assert {
        "ar_bundle_inbox_checkpoints",
        "ar_bundle_inbox_scopes",
        "ar_bundle_inbox_entries",
        "ar_bundle_inbox_operation_checkpoints",
    } <= tables
    assert foreign_key_failures == []
    assert quick_check == ("ok",)
