from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic.script import ScriptDirectory

from meta_research.migration import upgrade_database
from test_goal_creation_migration_merge import _seed_existing_creation
from test_quest_goal_evolution_migration import _migration_config, _upgrade_to_revision


MERGED_REVISION = "0073_merge_search_source"
STARTING_REVISIONS = (
    "0069_merge_goal_creation",
    "0070_material_processing",
    "0071_creation_companion_rotation",
    "0070_search_source_basis",
)
EXISTING_TABLES = (
    "rg_quests", "rm_creation_bases", "rm_question_creation_bases",
    "hc_manual_question_creations", "hc_quest_initializations",
    "hc_quest_draft_revisions", "hc_companion_sessions", "hc_companion_turns",
    "hc_intent_drafting_sessions", "hc_intent_drafting_turns",
    "hc_proposal_generation_attempts", "hc_deepfetch_requests",
)


def _insert(connection: sqlite3.Connection, table: str, values: dict) -> None:
    columns = ",".join(values)
    placeholders = ",".join("?" for _ in values)
    connection.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", tuple(values.values()),
    )


def _insert_search_request(
    connection: sqlite3.Connection, suffix: str, scope_hash: str,
    initialization_id: str = "initialization-existing",
) -> None:
    _insert(connection, "hc_deepfetch_requests", {
        "request_ref": f"search-{suffix}", "initialization_id": initialization_id,
        "correlation_ref": f"correlation-{suffix}", "draft_revision": 3,
        "draft_hash": "a" * 64, "scope_json": '{"sources":["preserved-source"]}',
        "scope_hash": scope_hash, "material_bindings_json": "[]",
        "material_bindings_hash": "b" * 64, "resource_envelope_ref": "envelope-existing",
        "resource_envelope_hash": "c" * 64,
        "result_route": "same_quest_initialization_proposal",
        "authorization_receipt_ref": f"authorization-{suffix}",
        "authorization_hash": "d" * 64, "status": "queued",
        "created_at": 10.0, "updated_at": 10.0,
    })


def _seed_existing_sessions(database: Path) -> None:
    _seed_existing_creation(database)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        _insert(connection, "hc_quest_initializations", {
            "initialization_id": "initialization-existing", "status": "draft",
            "draft_revision": 3, "draft_json": '{"goal":"Preserve this research."}',
            "draft_hash": "a" * 64, "proposal_revision": 0,
            "created_at": 10.0, "updated_at": 10.0,
        })
        _insert(connection, "hc_quest_draft_revisions", {
            "initialization_id": "initialization-existing", "revision": 3,
            "draft_json": '{"goal":"Preserve this research."}',
            "draft_hash": "a" * 64, "recorded_at": 10.0,
        })
        _insert(connection, "hc_companion_sessions", {
            "session_ref": "companion-existing", "scope_ref": "quest-existing",
            "native_session_ref": "native-companion-existing", "status": "open",
            "created_at": 10.0, "updated_at": 10.0,
        })
        _insert(connection, "hc_companion_turns", {
            "interaction_ref": "companion-turn-existing", "session_ref": "companion-existing",
            "ordinal": 1, "message": "Keep the research workspace.", "message_hash": "b" * 64,
            "assistant_status": "queued", "attempt_count": 0,
            "idempotency_key": "companion-turn-key", "command_hash": "c" * 64,
            "created_at": 10.0, "updated_at": 10.0,
        })
        _insert(connection, "hc_intent_drafting_sessions", {
            "session_ref": "intent-existing", "initialization_id": "initialization-existing",
            "native_session_ref": "native-intent-existing", "status": "open",
            "created_at": 10.0, "updated_at": 10.0,
        })
        _insert(connection, "hc_intent_drafting_turns", {
            "turn_ref": "intent-turn-existing", "session_ref": "intent-existing",
            "ordinal": 1, "idempotency_key": "intent-turn-key", "request_hash": "b" * 64,
            "basis_revision": 3, "basis_hash": "a" * 64,
            "user_content": "Preserve the accepted goal.", "user_content_hash": "c" * 64,
            "assistant_status": "queued", "created_at": 10.0,
            "adapter_metadata_json": '{"native_session_ref":"native-intent-existing"}',
            "adapter_metadata_hash": "d" * 64,
        })
        _insert(connection, "hc_proposal_generation_attempts", {
            "generation_ref": "proposal-attempt-existing",
            "initialization_id": "initialization-existing", "idempotency_key": "proposal-key",
            "request_hash": "b" * 64, "route": "direct", "basis_revision": 3,
            "basis_hash": "a" * 64, "starting_proposal_revision": 0,
            "status": "queued", "adapter_kind": "existing-adapter", "attempt_count": 0,
            "created_at": 10.0,
        })
        _insert_search_request(connection, "existing", "e" * 64)


def _seed_branch_data(database: Path, revision: str) -> tuple[str, ...]:
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        if revision == "0070_material_processing":
            _insert(connection, "hc_material_processing_effects", {
                "effect_key": "material-effect-existing", "action": "retain",
                "root_kind": "companion", "run_ref": "run-existing",
                "root_session_ref": "companion-existing", "quest_ref": "quest-existing",
                "reference_ref": "material-existing", "command_hash": "f" * 64,
                "command_json": '{"purpose":"Keep useful evidence."}',
                "state": "completed", "result_json": '{"retained":true}', "created_at": 11.0,
            })
            return ("hc_material_processing_effects",)
        if revision == "0071_creation_companion_rotation":
            tables = []
            for prefix, session_ref in (
                ("companion", "companion-existing"), ("intent", "intent-existing"),
            ):
                native_table = f"hc_{prefix}_native_sessions"
                switch_table = f"hc_{prefix}_session_switches"
                _insert(connection, native_table, {
                    "session_ref": session_ref, "generation": 2,
                    "native_session_ref": f"native-{prefix}-second", "created_at": 11.0,
                })
                switch = {
                    "switch_ref": f"switch-{prefix}-existing", "session_ref": session_ref,
                    "generation": 2, "previous_native_session_ref": f"native-{prefix}-existing",
                    "created_at": 11.0,
                }
                if prefix == "companion":
                    switch["scope_ref"] = "quest-existing"
                _insert(connection, switch_table, switch)
                tables.extend((native_table, switch_table))
            return tuple(tables)
        if revision == "0070_search_source_basis":
            _insert_search_request(connection, "second", "f" * 64)
            connection.execute(
                "UPDATE hc_quest_initializations SET confirmed_search_basis_json=? "
                "WHERE initialization_id='initialization-existing'",
                ('{"sources":["confirmed-existing-source"]}',),
            )
    return ()


def _snapshot(database: Path, tables: tuple[str, ...]) -> dict:
    with sqlite3.connect(database) as connection:
        return {
            table: (
                tuple(row[1] for row in connection.execute(f"PRAGMA table_info({table})")),
                connection.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall(),
            )
            for table in tables
        }


@pytest.mark.parametrize("starting_revision", STARTING_REVISIONS)
def test_material_companion_search_merge_preserves_each_branch_and_constraints(
    tmp_path: Path, starting_revision: str,
) -> None:
    database = tmp_path / "material-companion-search.sqlite3"
    _upgrade_to_revision(database, "0069_merge_goal_creation")
    _seed_existing_sessions(database)
    if starting_revision != "0069_merge_goal_creation":
        _upgrade_to_revision(database, starting_revision)
    branch_tables = _seed_branch_data(database, starting_revision)
    existing_rows = _snapshot(database, EXISTING_TABLES + branch_tables)

    upgrade_database(database)
    first_schema = _schema_snapshot(database)
    upgrade_database(database)

    assert ScriptDirectory.from_config(_migration_config()).get_heads() == [MERGED_REVISION]
    assert _schema_snapshot(database) == first_schema
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        assert connection.execute("SELECT version_num FROM alembic_version").fetchall() == [
            (MERGED_REVISION,),
        ]
        for table, (columns, original) in existing_rows.items():
            actual = connection.execute(
                f"SELECT {','.join(columns)} FROM {table} ORDER BY 1,2",
            ).fetchall()
            assert actual == original
        for prefix, session_ref in (
            ("companion", "companion-existing"), ("intent", "intent-existing"),
        ):
            assert connection.execute(
                f"SELECT generation,native_session_ref FROM hc_{prefix}_native_sessions "
                "WHERE session_ref=? AND generation=1", (session_ref,),
            ).fetchone() == (1, f"native-{prefix}-existing")
            turn_table = "hc_companion_turns" if prefix == "companion" else "hc_intent_drafting_turns"
            assert connection.execute(
                f"SELECT native_session_generation,native_session_ref FROM {turn_table}",
            ).fetchone() == (1, f"native-{prefix}-existing")
            native_insert = (
                f"INSERT INTO hc_{prefix}_native_sessions "
                "(session_ref,generation,native_session_ref,created_at) VALUES (?,?,?,12.0)"
            )
            with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
                connection.execute(native_insert, (session_ref, 3, f"native-{prefix}-existing"))
            with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
                connection.execute(native_insert, (session_ref, 0, "invalid-generation"))
            with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
                connection.execute(native_insert, ("session-missing", 1, "missing-parent"))
            switch = {
                "switch_ref": f"switch-{prefix}-missing", "session_ref": session_ref,
                "generation": 99, "created_at": 12.0,
            }
            if prefix == "companion":
                switch["scope_ref"] = "quest-existing"
            with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
                _insert(connection, f"hc_{prefix}_session_switches", switch)
        assert connection.execute(
            "SELECT native_session_generation FROM hc_proposal_generation_attempts",
        ).fetchone() == (1,)

        _insert_search_request(connection, "different-scope", "9" * 64)
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
            _insert_search_request(connection, "duplicate-scope", "e" * 64)
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
            _insert_search_request(connection, "missing-initialization", "8" * 64, "missing")
        assert {row[1] for row in connection.execute(
            "PRAGMA index_list(hc_material_processing_effects)",
        )} >= {"hc_material_treatment_reference", "hc_material_treatment_quest"}
        _insert(connection, "hc_material_processing_effects", {
            "effect_key": "material-key-check", "action": "retain", "root_kind": "companion",
            "run_ref": "run-existing", "root_session_ref": "companion-existing",
            "command_hash": "a" * 64, "command_json": "{}", "state": "completed",
            "created_at": 12.0,
        })
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
            connection.execute(
                "INSERT INTO hc_material_processing_effects "
                "SELECT * FROM hc_material_processing_effects WHERE effect_key='material-key-check'",
            )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def _schema_snapshot(database: Path) -> list[tuple]:
    with sqlite3.connect(database) as connection:
        return connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name",
        ).fetchall()
