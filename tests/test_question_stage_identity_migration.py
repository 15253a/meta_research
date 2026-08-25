from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import meta_research.composition as composition
from meta_research.paths import prepare_data_root
from test_migration_recovery import _upgrade_to_revision
from test_public_plan_stage import (
    _DeterministicIdeaSkill,
    _DeterministicPlanSkill,
    _confirm_direct_quest,
    _finish_idea_stage,
    _runtime,
)


_TABLES = (
    "rg_idea_outcome_decisions",
    "rm_plan_documents",
    "rg_formal_plan_decisions",
)
_REMOVED_QUESTION_FOREIGN_KEYS = {
    ("question_ref", "rg_questions", "question_ref"),
    (
        "question_content_ref",
        "rm_formal_question_contents",
        "content_ref",
    ),
}


def _normalized_sql(value: str | None) -> str | None:
    return None if value is None else " ".join(value.split())


def _check_clauses(create_sql: str) -> tuple[str, ...]:
    clauses: list[str] = []
    for match in re.finditer(r"\bCHECK\s*\(", create_sql, flags=re.IGNORECASE):
        start = create_sql.find("(", match.start())
        depth = 0
        for index in range(start, len(create_sql)):
            character = create_sql[index]
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0:
                    clauses.append(_normalized_sql(create_sql[start + 1 : index]) or "")
                    break
        else:
            raise AssertionError("unterminated CHECK constraint")
    return tuple(sorted(clauses))


def _table_contract(
    connection: sqlite3.Connection, table_name: str
) -> dict[str, object]:
    create_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()[0]
    indexes: list[tuple[object, ...]] = []
    for _seq, name, unique, origin, partial in connection.execute(
        f"PRAGMA index_list({table_name})"
    ):
        columns = tuple(
            row[2] for row in connection.execute(f'PRAGMA index_info("{name}")')
        )
        sql_row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
            (name,),
        ).fetchone()
        indexes.append(
            (
                (
                    "<automatic-unique>"
                    if name.startswith("sqlite_autoindex_")
                    else name
                ),
                unique,
                origin,
                partial,
                columns,
                _normalized_sql(None if sql_row is None else sql_row[0]),
            )
        )
    return {
        "columns": tuple(connection.execute(f"PRAGMA table_xinfo({table_name})")),
        "foreign_keys": {
            (row[3], row[2], row[4])
            for row in connection.execute(f"PRAGMA foreign_key_list({table_name})")
        },
        "indexes": tuple(sorted(indexes)),
        "checks": _check_clauses(create_sql),
        "triggers": tuple(
            connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' "
                "AND tbl_name = ? ORDER BY name",
                (table_name,),
            )
        ),
        "rows": tuple(connection.execute(f"SELECT * FROM {table_name} ORDER BY 1")),
    }


def _drive_plan_to_commit(runtime) -> dict[str, object]:
    for _step in range(12):
        current = runtime.plan_stage.query_current()
        if current["stage_commit"] is not None:
            return current
        assert runtime.plan_stage.process_once()
    raise AssertionError("Plan Stage did not reach StageCommit")


def test_0036_root_stage_rows_survive_polymorphic_question_upgrade(
    tmp_path: Path,
    monkeypatch,
) -> None:
    data_path = tmp_path / "question-stage-identity"
    data_root = prepare_data_root(data_path)
    _upgrade_to_revision(data_root.database, "0036_companion_view_context")
    monkeypatch.setattr(composition, "upgrade_database", lambda _database: None)

    runtime = _runtime(
        data_path,
        idea_skill=_DeterministicIdeaSkill(),
        plan_skill=_DeterministicPlanSkill(no_gap=False),
    )
    try:
        quest = _confirm_direct_quest(runtime)
        assert _finish_idea_stage(runtime)["stage_commit"]["outcome_kind"] == (
            "IdeaSet"
        )
        plan = _drive_plan_to_commit(runtime)
        assert plan["stage_commit"]["outcome_kind"] == "FormalPlan"
        submission_ref = plan["run"]["submission_ref"]
        cycle_ref = quest["cycle_ref"]
    finally:
        runtime.close()

    with sqlite3.connect(data_root.database) as connection:
        before = {
            table_name: _table_contract(connection, table_name)
            for table_name in _TABLES
        }
    assert all(len(contract["rows"]) == 1 for contract in before.values())

    _upgrade_to_revision(data_root.database, "0037_question_stage_identity")
    _upgrade_to_revision(data_root.database, "0037_question_stage_identity")

    # A fresh SQLite connection models daemon restart and also catches stale
    # schema state retained by a prior connection.
    with sqlite3.connect(data_root.database) as connection:
        version = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()
        after = {
            table_name: _table_contract(connection, table_name)
            for table_name in _TABLES
        }
        foreign_key_failures = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()
        quick_check = connection.execute("PRAGMA quick_check").fetchone()
        scratch_tables = tuple(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
            if row[0].startswith("_alembic_tmp_")
        )

    assert version == ("0037_question_stage_identity",)
    assert foreign_key_failures == []
    assert quick_check == ("ok",)
    assert scratch_tables == ()
    for table_name in _TABLES:
        assert after[table_name]["columns"] == before[table_name]["columns"]
        assert after[table_name]["checks"] == before[table_name]["checks"]
        assert after[table_name]["indexes"] == before[table_name]["indexes"]
        assert after[table_name]["triggers"] == before[table_name]["triggers"]
        assert after[table_name]["rows"] == before[table_name]["rows"]
        assert after[table_name]["foreign_keys"] == (
            before[table_name]["foreign_keys"]
            - _REMOVED_QUESTION_FOREIGN_KEYS
        )

    restarted = _runtime(
        data_path,
        idea_skill=_DeterministicIdeaSkill(),
        plan_skill=_DeterministicPlanSkill(no_gap=False),
    )
    try:
        request = restarted.owners.advancement_engine.query_plan_stage_request(
            cycle_ref
        )
        content = restarted.owners.research_memory.query_plan_document(
            submission_ref
        )
        decision = restarted.owners.research_graph.query_formal_plan_decision(
            submission_ref
        )
        assert request is not None
        assert request.accepted_question.question_receipt.kind == (
            "root_question_acceptance"
        )
        assert content is not None
        assert decision is not None
        assert decision.formal_plan_ref is not None
    finally:
        restarted.close()
