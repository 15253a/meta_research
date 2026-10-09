from __future__ import annotations

import json
import sqlite3
from importlib.resources import files
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import URL, create_engine, event, inspect

from meta_research.migration import (
    _begin_migration_transaction,
    _configure_migration_sqlite,
    upgrade_database,
)
from meta_research.owners.common import canonical_hash


def _migration_config() -> Config:
    config = Config()
    config.set_main_option("script_location", str(files("meta_research.migrations")))
    return config


def _upgrade_to_revision(database: Path, revision: str) -> None:
    engine = create_engine(URL.create("sqlite+pysqlite", database=str(database)), future=True)
    event.listen(engine, "connect", _configure_migration_sqlite)
    event.listen(engine, "begin", _begin_migration_transaction)
    config = _migration_config()
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, revision)
    finally:
        engine.dispose()


def test_quest_goal_migration_seeds_only_heads_and_preserves_source_rows(tmp_path: Path) -> None:
    database = tmp_path / "quest-goal.sqlite3"
    _upgrade_to_revision(database, "0064_work_materials")
    goal = {"goal": "keep the accepted goal byte-identical"}
    encoded = json.dumps(goal, ensure_ascii=False, separators=(",", ":"))
    draft_hash = canonical_hash(goal)
    values = (
        "quest-existing",
        "initialization-existing",
        3,
        draft_hash,
        "proposal",
        "a" * 64,
        "preview",
        "b" * 64,
        encoded,
        "confirmation",
        "c" * 64,
        "quest-receipt",
        "d" * 64,
        10.0,
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO rg_quests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            values,
        )
        before = connection.execute(
            "SELECT * FROM rg_quests WHERE quest_ref='quest-existing'"
        ).fetchone()

    upgrade_database(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT * FROM rg_quests WHERE quest_ref='quest-existing'"
        ).fetchone() == before
        expected_ref = "quest_goal_revision_" + canonical_hash(
            {
                "quest_ref": "quest-existing",
                "draft_revision": 3,
                "draft_hash": draft_hash,
            }
        )[:32]
        assert connection.execute(
            "SELECT current_revision_ref,current_sequence,status,completion_acceptance_ref "
            "FROM rg_quest_goal_heads WHERE quest_ref='quest-existing'"
        ).fetchone() == (expected_ref, 0, "open", None)
        assert connection.execute("SELECT COUNT(*) FROM rg_quest_goal_revisions").fetchone() == (0,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    engine = create_engine(f"sqlite:///{database}", future=True)
    try:
        inspector = inspect(engine)
        assert {
            "hc_runtime_condition_versions",
            "hc_runtime_condition_heads",
            "rg_quest_goal_heads",
            "rg_quest_goal_revisions",
            "rg_goal_revision_conditions",
            "rg_goal_evolution_effects",
            "rg_goal_work_intents",
        } <= set(inspector.get_table_names())
    finally:
        engine.dispose()
