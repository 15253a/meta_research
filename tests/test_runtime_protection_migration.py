from __future__ import annotations

import sqlite3
from importlib.resources import files
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import URL, create_engine, event, inspect

from meta_research.migration import (
    _begin_migration_transaction,
    _configure_migration_sqlite,
    upgrade_database,
)


RUNTIME_TABLES = {
    "ar_runtime_instances",
    "ar_power_inhibitor_epochs",
    "ar_execution_responsibilities",
    "ar_power_inhibitor_capabilities",
    "ar_runtime_boundary_receipts",
    "ar_runtime_interruptions",
    "ar_runtime_observability_identity",
    "ar_runtime_telemetry_state",
}


def _migration_config() -> Config:
    config = Config()
    config.set_main_option(
        "script_location",
        str(files("meta_research.migrations")),
    )
    return config


def _upgrade_to_revision(database: Path, revision: str) -> None:
    engine = create_engine(
        URL.create("sqlite+pysqlite", database=str(database)),
        future=True,
    )
    event.listen(engine, "connect", _configure_migration_sqlite)
    event.listen(engine, "begin", _begin_migration_transaction)
    config = _migration_config()
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, revision)
    finally:
        engine.dispose()


def _schema_snapshot(database: Path) -> tuple[tuple[object, ...], ...]:
    with sqlite3.connect(database) as connection:
        return tuple(
            connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall()
        )


def _foreign_keys(inspector, table_name: str) -> set[tuple[object, ...]]:
    return {
        (
            tuple(item["constrained_columns"]),
            item["referred_table"],
            tuple(item["referred_columns"]),
        )
        for item in inspector.get_foreign_keys(table_name)
    }


def _check_text(inspector, table_name: str) -> str:
    return " ".join(
        " ".join(str(item["sqltext"]).lower().split())
        for item in inspector.get_check_constraints(table_name)
    )


def test_runtime_protection_migration_is_atomic_retryable_and_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "runtime-protection-migration.sqlite3"
    _upgrade_to_revision(database, "0029_target_root_lifecycle")
    original_create_table = Operations.create_table
    failed_once = False

    def interrupt(self, table_name, *args, **kwargs):
        nonlocal failed_once
        created = original_create_table(self, table_name, *args, **kwargs)
        if table_name == "ar_execution_responsibilities" and not failed_once:
            failed_once = True
            raise OSError("injected runtime protection migration interruption")
        return created

    monkeypatch.setattr(Operations, "create_table", interrupt)
    with pytest.raises(
        OSError,
        match="runtime protection migration interruption",
    ):
        upgrade_database(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0029_target_root_lifecycle",)
        tables_after_failure = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        harness_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(ar_harness_provider_operations)"
            )
        }
    assert not RUNTIME_TABLES & tables_after_failure
    assert "reconciliation_generation" not in harness_columns

    monkeypatch.setattr(Operations, "create_table", original_create_table)
    upgrade_database(database)
    first_schema = _schema_snapshot(database)
    upgrade_database(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0038_deepfetch_binding_audit",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
    assert _schema_snapshot(database) == first_schema


def test_runtime_protection_migration_is_single_head_with_core_constraints(
    tmp_path: Path,
) -> None:
    config = _migration_config()
    assert ScriptDirectory.from_config(config).get_heads() == [
        "0038_deepfetch_binding_audit"
    ]

    database = tmp_path / "runtime-protection-schema.sqlite3"
    upgrade_database(database)
    engine = create_engine(f"sqlite:///{database}", future=True)
    try:
        inspector = inspect(engine)
        assert RUNTIME_TABLES <= set(inspector.get_table_names())
        harness_columns = {
            item["name"]: item
            for item in inspector.get_columns("ar_harness_provider_operations")
        }
        reconciliation_generation = harness_columns["reconciliation_generation"]
        assert reconciliation_generation["nullable"] is False
        assert str(reconciliation_generation["default"]).strip("'") == "0"

        assert _foreign_keys(inspector, "ar_power_inhibitor_epochs") == {
            (("incarnation_ref",), "ar_runtime_instances", ("incarnation_ref",))
        }
        assert _foreign_keys(inspector, "ar_execution_responsibilities") == {
            (("holder_ref",), "ar_power_inhibitor_epochs", ("holder_ref",)),
            (("incarnation_ref",), "ar_runtime_instances", ("incarnation_ref",)),
        }
        assert _foreign_keys(inspector, "ar_power_inhibitor_capabilities") == {
            (("incarnation_ref",), "ar_runtime_instances", ("incarnation_ref",))
        }
        assert _foreign_keys(inspector, "ar_runtime_boundary_receipts") == {
            (
                ("responsibility_ref",),
                "ar_execution_responsibilities",
                ("responsibility_ref",),
            )
        }
        assert _foreign_keys(inspector, "ar_runtime_interruptions") == {
            (
                ("responsibility_ref",),
                "ar_execution_responsibilities",
                ("responsibility_ref",),
            )
        }

        assert "active" in _check_text(inspector, "ar_runtime_instances")
        assert "release_pending" in _check_text(
            inspector, "ar_power_inhibitor_epochs"
        )
        responsibility_checks = _check_text(
            inspector, "ar_execution_responsibilities"
        )
        assert "permanent_fence" in responsibility_checks
        assert "finished_at is not null" in responsibility_checks
        assert "unavailable" in _check_text(
            inspector, "ar_power_inhibitor_capabilities"
        )
        boundary_checks = _check_text(
            inspector, "ar_runtime_boundary_receipts"
        )
        assert "checkpoint_ref is not null" in boundary_checks
        assert "length(evidence_hash) = 64" in boundary_checks
        assert "protected" in _check_text(
            inspector, "ar_runtime_interruptions"
        )
        assert "singleton = 'runtime'" in _check_text(
            inspector, "ar_runtime_observability_identity"
        )
        assert "revoked" in _check_text(
            inspector, "ar_runtime_telemetry_state"
        )
        assert "revocation_pending" in _check_text(
            inspector, "ar_runtime_telemetry_state"
        )
        assert {
            "singleton",
            "mode",
            "provider",
            "authorization_ref",
            "failure_code",
            "updated_at",
        } == {
            column["name"]
            for column in inspector.get_columns("ar_runtime_telemetry_state")
        }

        with engine.connect() as connection:
            assert connection.exec_driver_sql(
                "SELECT singleton, mode, provider, authorization_ref, "
                "failure_code, updated_at "
                "FROM ar_runtime_telemetry_state"
            ).one() == ("runtime", "disabled", None, None, None, 0.0)
            correlation = connection.exec_driver_sql(
                "SELECT singleton, correlation_ref "
                "FROM ar_runtime_observability_identity"
            ).one()
            assert correlation[0] == "runtime"
            assert str(correlation[1]).startswith("runtime_correlation_")
    finally:
        engine.dispose()
