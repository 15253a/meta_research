from __future__ import annotations

import sqlite3
from importlib.resources import files
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import URL, create_engine, event

from meta_research.migration import (
    _begin_migration_transaction,
    _configure_migration_sqlite,
    upgrade_database,
)


def _migrate(database: Path, revision: str, *, downgrade: bool = False) -> None:
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
            if downgrade:
                command.downgrade(config, revision)
            else:
                command.upgrade(config, revision)
    finally:
        engine.dispose()


def _cycle_columns(database: Path) -> set[str]:
    with sqlite3.connect(database) as connection:
        return {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(ae_cycles)")
        }


def _selection_fact_foreign_keys(database: Path) -> list[tuple[object, ...]]:
    with sqlite3.connect(database) as connection:
        return connection.execute(
            "PRAGMA foreign_key_list(rg_question_selection_facts)"
        ).fetchall()


def _selection_fact_unique_columns(database: Path) -> set[tuple[str, ...]]:
    with sqlite3.connect(database) as connection:
        indexes = connection.execute(
            "PRAGMA index_list(rg_question_selection_facts)"
        ).fetchall()
        return {
            tuple(
                str(column[2])
                for column in connection.execute(
                    f"PRAGMA index_info({index[1]})"
                ).fetchall()
            )
            for index in indexes
            if bool(index[2])
        }


def _reasoning_decision_columns(database: Path) -> set[str]:
    with sqlite3.connect(database) as connection:
        return {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(rg_reasoning_outcome_decisions)"
            )
        }


def test_0033_reasoning_successor_context_round_trips_from_0032(
    tmp_path: Path,
) -> None:
    database = tmp_path / "reasoning-successor-context.sqlite3"
    _migrate(database, "0032_autonomous_completion")
    assert {
        "idea_context_pack_json",
        "idea_context_pack_hash",
    }.isdisjoint(_cycle_columns(database))
    assert _selection_fact_foreign_keys(database)[0][2] == (
        "rg_autonomous_questions"
    )

    _migrate(database, "0033_reasoning_successor_context")
    assert {
        "idea_context_pack_json",
        "idea_context_pack_hash",
    } <= _cycle_columns(database)
    assert _selection_fact_foreign_keys(database) == []
    assert (
        "question_ref",
        "fact_kind",
        "graph_revision_ref",
    ) in _selection_fact_unique_columns(database)
    assert {
        "target_aggregate_json",
        "target_aggregate_hash",
    } <= _reasoning_decision_columns(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0033_reasoning_successor_context",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)

    _migrate(database, "0032_autonomous_completion", downgrade=True)
    assert {
        "idea_context_pack_json",
        "idea_context_pack_hash",
    }.isdisjoint(_cycle_columns(database))
    assert _selection_fact_foreign_keys(database)[0][2] == (
        "rg_autonomous_questions"
    )
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0032_autonomous_completion",)

    upgrade_database(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0038_deepfetch_binding_audit",)
        assert connection.execute(
            "PRAGMA foreign_key_list(rg_question_selection_facts)"
        ).fetchall() == []
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)


def test_0033_downgrade_rejects_multiple_selection_fact_revisions(
    tmp_path: Path,
) -> None:
    database = tmp_path / "reasoning-selection-fact-versions.sqlite3"
    _migrate(database, "0033_reasoning_successor_context")
    with sqlite3.connect(database) as connection:
        for index in (1, 2):
            connection.execute(
                "INSERT INTO rg_question_selection_facts (fact_ref, "
                "question_ref, quest_ref, fact_kind, fact_value, is_current, "
                "graph_revision_ref, receipt_ref, receipt_hash, accepted_at) "
                "VALUES (?, 'question:versioned', 'quest:versioned', "
                "'GraphPresenceFact', 'present', 1, ?, ?, ?, ?)",
                (
                    f"fact:versioned:{index}",
                    f"graph-revision:versioned:{index}",
                    f"receipt:versioned:{index}",
                    str(index) * 64,
                    float(index),
                ),
            )

    with pytest.raises(
        RuntimeError,
        match="versioned Question selection facts exist",
    ):
        _migrate(database, "0032_autonomous_completion", downgrade=True)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0033_reasoning_successor_context",)
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)


def test_0031_downgrade_rejects_nonzero_autonomous_owner_counters(
    tmp_path: Path,
) -> None:
    database = tmp_path / "autonomous-owner-counter.sqlite3"
    _migrate(database, "0031_autonomous_question_owners")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE research_memory_state SET "
            "autonomous_question_content_count = 1 WHERE singleton = 'owner'"
        )

    with pytest.raises(
        RuntimeError,
        match="autonomous Question Owner facts exist",
    ):
        _migrate(database, "0030_reasoning_owner_acceptance", downgrade=True)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0031_autonomous_question_owners",)
        assert connection.execute(
            "SELECT autonomous_question_content_count FROM "
            "research_memory_state WHERE singleton = 'owner'"
        ).fetchone() == (1,)
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
