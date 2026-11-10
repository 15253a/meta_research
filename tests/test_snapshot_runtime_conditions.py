from pathlib import Path

from sqlalchemy import text

from test_public_plan_stage import (
    _DeterministicIdeaSkill,
    _DeterministicPlanSkill,
    _confirm_direct_quest,
    _runtime,
)


def test_first_snapshot_reads_conditions_materialized_at_quest_acceptance(tmp_path: Path) -> None:
    runtime = _runtime(
        tmp_path / "first-snapshot",
        idea_skill=_DeterministicIdeaSkill(),
        plan_skill=_DeterministicPlanSkill(no_gap=False),
    )
    try:
        completed = _confirm_direct_quest(runtime)
        quest_ref = completed["quest_ref"]
        with runtime._database.read() as connection:
            assert connection.execute(
                text("SELECT scope_ref FROM hc_runtime_condition_heads WHERE scope_ref=:quest"),
                {"quest": quest_ref},
            ).first() is not None

        snapshot = runtime.projection.query_snapshot(include_stages=False)
        current = snapshot["research_space"]["current_quest"]
        assert current["status"] == "ready"
        assert current["quest_ref"] == quest_ref
        conditions = current["conditions"]["runtime_conditions"]
        assert conditions["quest_ref"] == quest_ref
        assert conditions["text"].strip()
        assert runtime.projection.query_snapshot(include_stages=False)[
            "research_space"
        ]["current_quest"]["conditions"]["runtime_conditions"] == conditions
        with runtime._database.read() as connection:
            assert connection.execute(
                text("SELECT current_revision FROM hc_runtime_condition_heads WHERE scope_ref=:quest"),
                {"quest": quest_ref},
            ).scalar_one() == conditions["revision"]
            assert connection.execute(
                text("SELECT COUNT(*) FROM hc_runtime_condition_versions WHERE scope_ref=:quest"),
                {"quest": quest_ref},
            ).scalar_one() == 1
    finally:
        runtime.close()


def test_projection_condition_reads_have_no_extra_database_or_writes(tmp_path, monkeypatch):
    import sqlite3
    from meta_research.database import Database
    runtime = _runtime(tmp_path / "snapshot-cost", idea_skill=_DeterministicIdeaSkill(),
                       plan_skill=_DeterministicPlanSkill(no_gap=False))
    try:
        _confirm_direct_quest(runtime)
        constructions = []
        sqlite_connects = []
        statements = []
        original_init = Database.__init__
        original_connect = sqlite3.connect
        def initialize(self, *args, **kwargs):
            constructions.append(str(args[0]))
            original_init(self, *args, **kwargs)
        def connect(*args, **kwargs):
            sqlite_connects.append(str(args[0]))
            return original_connect(*args, **kwargs)
        from sqlalchemy import event
        def sql(_connection, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)
        monkeypatch.setattr(Database, '__init__', initialize)
        monkeypatch.setattr(sqlite3, 'connect', connect)
        event.listen(runtime._database._engine, 'before_cursor_execute', sql)
        for observation in ('first', 'stable'):
            constructions.clear(); sqlite_connects.clear(); statements.clear()
            runtime.projection.query_snapshot(include_stages=False)
            writes = [statement for statement in statements if statement.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE', 'BEGIN IMMEDIATE'))]
            print({'observation': observation, 'database_constructions': len(constructions),
                   'raw_sqlite_connects': len(sqlite_connects), 'write_statements': len(writes)})
            assert not constructions and not sqlite_connects and not writes
    finally:
        runtime.close()
