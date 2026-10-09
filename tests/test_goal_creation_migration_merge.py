from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic.script import ScriptDirectory

from meta_research.migration import upgrade_database
from test_quest_goal_evolution_migration import _migration_config, _upgrade_to_revision


LATEST_REVISION = "0071_creation_companion_rotation"


def _seed_existing_creation(database: Path) -> dict[str, tuple]:
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO rg_quests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "quest-existing", "initialization-existing", 3, "a" * 64,
                "proposal-existing", "b" * 64, "preview-existing", "c" * 64,
                '{"goal":"Preserve the accepted goal."}', "confirmation-existing",
                "d" * 64, "quest-receipt", "e" * 64, 10.0,
            ),
        )
        connection.execute(
            "INSERT INTO rm_creation_bases "
            "(basis_ref,basis_hash,initialization_id,draft_revision,draft_hash,kind,body_json) "
            "VALUES ('basis-existing',?,'initialization-existing',3,?,'prepared',?)",
            ("f" * 64, "a" * 64, '{"sources":["keep-existing-source"]}'),
        )
        connection.execute(
            "INSERT INTO rm_question_creation_bases VALUES (?,?,?,?)",
            ("question-existing", "quest-existing", "basis-existing", '{"accepted":true}'),
        )
        connection.execute(
            "INSERT INTO hc_manual_question_creations "
            "(context_ref,quest_ref,quest_initialization_id,quest_receipt_ref,"
            "quest_receipt_hash,parent_question_ref,parent_question_receipt_ref,"
            "parent_question_receipt_hash,creation_mode,generation,status,"
            "proposal_revision,recovery_attempt_count,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "manual-existing", "quest-existing", "initialization-existing",
                "quest-receipt", "e" * 64, "question-existing", "question-receipt",
                "1" * 64, "ManualCreation", 1, "draft", 0, 0, 11.0, 11.0,
            ),
        )
        return {
            table: connection.execute(f"SELECT * FROM {table}").fetchone()
            for table in (
                "rg_quests", "rm_creation_bases", "rm_question_creation_bases",
                "hc_manual_question_creations",
            )
        }


@pytest.mark.parametrize(
    "starting_revision",
    ["0064_work_materials", "0065_quest_goal_evolution", "0068_manual_creation_inputs"],
)
def test_goal_creation_merge_preserves_existing_data_from_each_head(
    tmp_path: Path, starting_revision: str,
) -> None:
    database = tmp_path / "existing-creation.sqlite3"
    _upgrade_to_revision(database, "0064_work_materials")
    original_rows = _seed_existing_creation(database)
    if starting_revision != "0064_work_materials":
        _upgrade_to_revision(database, starting_revision)

    upgrade_database(database)
    upgrade_database(database)

    assert ScriptDirectory.from_config(_migration_config()).get_heads() == [LATEST_REVISION]
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        assert connection.execute("SELECT version_num FROM alembic_version").fetchall() == [
            (LATEST_REVISION,),
        ]
        for table, original in original_rows.items():
            actual = connection.execute(f"SELECT * FROM {table}").fetchone()
            assert actual[:len(original)] == original
        assert connection.execute(
            "SELECT input_identity_hash FROM rm_creation_bases WHERE basis_ref='basis-existing'"
        ).fetchone() == (None,)
        assert connection.execute(
            "SELECT creation_basis_ref,creation_basis_hash,proposal_input_identity_hash "
            "FROM hc_manual_question_creations WHERE context_ref='manual-existing'"
        ).fetchone() == (None, None, None)
        assert connection.execute(
            "SELECT current_sequence,status,completion_acceptance_ref "
            "FROM rg_quest_goal_heads WHERE quest_ref='quest-existing'"
        ).fetchone() == (0, "open", None)
        assert connection.execute("SELECT COUNT(*) FROM rg_quest_goal_revisions").fetchone() == (0,)
        tables = {
            row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {
            "hc_creation_material_operations", "hc_creation_material_copies",
            "root_creation_custody", "rg_quest_goal_heads", "rg_goal_work_intents",
            "hc_runtime_condition_heads",
        } <= tables

        insert_basis = (
            "INSERT INTO rm_creation_bases "
            "(basis_ref,basis_hash,initialization_id,draft_revision,draft_hash,kind,"
            "body_json,input_identity_hash) VALUES (?,?,'initialization-existing',3,?,"
            "'prepared','{}',?)"
        )
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
            connection.execute(insert_basis, ("duplicate-legacy", "2" * 64, "a" * 64, None))
        for suffix, identity in (("one", "3" * 64), ("two", "4" * 64)):
            connection.execute(insert_basis, (f"basis-{suffix}", "5" * 64, "a" * 64, identity))
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
            connection.execute(insert_basis, ("duplicate-input", "6" * 64, "a" * 64, "3" * 64))
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
            connection.execute(
                "INSERT INTO rm_question_creation_bases VALUES (?,?,?,?)",
                ("question-unbound", "quest-existing", "basis-missing", "{}"),
            )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
