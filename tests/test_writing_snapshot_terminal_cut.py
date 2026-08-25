from __future__ import annotations

import threading
from pathlib import Path

import pytest
from sqlalchemy import text

from meta_research.database import Database
from meta_research.owners.common import OwnerConflict
from meta_research.owners.research_graph import (
    SQLiteResearchGraph,
    WritingExperimentTerminalCut,
)


def _graph_with_terminal_tables(path: Path) -> tuple[Database, SQLiteResearchGraph]:
    database = Database(path)
    with database.write() as connection:
        connection.execute(
            text(
                "CREATE TABLE rg_experiment_requests ("
                "evaluation_attempt_ref TEXT PRIMARY KEY, quest_ref TEXT NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE rg_evaluation_attempts ("
                "evaluation_attempt_ref TEXT PRIMARY KEY, status TEXT NOT NULL, "
                "formal_rejection_code TEXT, updated_at FLOAT NOT NULL)"
            )
        )
    graph = object.__new__(SQLiteResearchGraph)
    graph._database = database
    return database, graph


def _append_attempt(
    database: Database,
    *,
    evaluation_attempt_ref: str,
    quest_ref: str,
    status: str,
    updated_at: float,
    rejection_code: str | None = None,
) -> None:
    with database.write() as connection:
        connection.execute(
            text(
                "INSERT INTO rg_experiment_requests "
                "(evaluation_attempt_ref, quest_ref) VALUES "
                "(:evaluation_attempt_ref, :quest_ref)"
            ),
            {
                "evaluation_attempt_ref": evaluation_attempt_ref,
                "quest_ref": quest_ref,
            },
        )
        connection.execute(
            text(
                "INSERT INTO rg_evaluation_attempts "
                "(evaluation_attempt_ref, status, formal_rejection_code, "
                "updated_at) VALUES (:evaluation_attempt_ref, :status, "
                ":formal_rejection_code, :updated_at)"
            ),
            {
                "evaluation_attempt_ref": evaluation_attempt_ref,
                "status": status,
                "formal_rejection_code": rejection_code,
                "updated_at": updated_at,
            },
        )


def test_terminal_cut_is_quest_scoped_and_excludes_live_attempts_while_tail_grows(
    tmp_path: Path,
) -> None:
    database, graph = _graph_with_terminal_tables(tmp_path / "terminal-cut.sqlite3")
    stop = threading.Event()
    writer: threading.Thread | None = None
    try:
        _append_attempt(
            database,
            evaluation_attempt_ref="attempt:focus:accepted",
            quest_ref="quest:focus",
            status="measurement_accepted",
            updated_at=1.0,
        )
        _append_attempt(
            database,
            evaluation_attempt_ref="attempt:focus:live",
            quest_ref="quest:focus",
            status="assets_accepted",
            updated_at=2.0,
        )
        _append_attempt(
            database,
            evaluation_attempt_ref="attempt:other:rejected",
            quest_ref="quest:other",
            status="measurement_rejected",
            rejection_code="formal_measurement_other",
            updated_at=3.0,
        )

        started = threading.Event()

        def append_irrelevant_tail() -> None:
            started.set()
            index = 0
            while not stop.is_set() and index < 200:
                _append_attempt(
                    database,
                    evaluation_attempt_ref=f"attempt:tail:{index:04d}",
                    quest_ref=("quest:other" if index % 2 else "quest:focus"),
                    status=(
                        "measurement_rejected"
                        if index % 2
                        else "assets_accepted"
                    ),
                    rejection_code=(
                        "formal_measurement_irrelevant" if index % 2 else None
                    ),
                    updated_at=10.0 + index,
                )
                index += 1

        writer = threading.Thread(target=append_irrelevant_tail, daemon=True)
        writer.start()
        assert started.wait(timeout=2)

        cut = graph.query_writing_experiment_terminal_cut("quest:focus")

        assert isinstance(cut, WritingExperimentTerminalCut)
        assert cut.quest_ref == "quest:focus"
        assert tuple(
            fact.evaluation_attempt_ref for fact in cut.facts
        ) == ("attempt:focus:accepted",)
        assert cut.facts[0].formal_measurement_status == "accepted"
        assert cut.facts[0].formal_rejection_code is None
    finally:
        stop.set()
        if writer is not None:
            writer.join(timeout=5)
        database.close()


def test_terminal_cut_is_closed_and_next_capture_observes_new_terminal_fact(
    tmp_path: Path,
) -> None:
    database, graph = _graph_with_terminal_tables(tmp_path / "next-cut.sqlite3")
    try:
        _append_attempt(
            database,
            evaluation_attempt_ref="attempt:first",
            quest_ref="quest:focus",
            status="measurement_accepted",
            updated_at=1.0,
        )
        first = graph.query_writing_experiment_terminal_cut("quest:focus")

        _append_attempt(
            database,
            evaluation_attempt_ref="attempt:second",
            quest_ref="quest:focus",
            status="measurement_rejected",
            rejection_code="formal_measurement_invalid_result",
            updated_at=2.0,
        )
        second = graph.query_writing_experiment_terminal_cut("quest:focus")

        assert tuple(item.evaluation_attempt_ref for item in first.facts) == (
            "attempt:first",
        )
        assert tuple(item.evaluation_attempt_ref for item in second.facts) == (
            "attempt:first",
            "attempt:second",
        )
        assert second.facts[-1].formal_measurement_status == "rejected"
        assert second.facts[-1].formal_rejection_code == (
            "formal_measurement_invalid_result"
        )
    finally:
        database.close()


def test_terminal_cut_rejects_more_than_4096_facts(tmp_path: Path) -> None:
    database, graph = _graph_with_terminal_tables(tmp_path / "bounded-cut.sqlite3")
    try:
        with database.write() as connection:
            connection.execute(
                text(
                    "WITH RECURSIVE values_4097(value) AS ("
                    "SELECT 1 UNION ALL SELECT value + 1 FROM values_4097 "
                    "WHERE value < 4097) INSERT INTO rg_experiment_requests "
                    "(evaluation_attempt_ref, quest_ref) SELECT "
                    "printf('attempt:%04d', value), 'quest:bounded' "
                    "FROM values_4097"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO rg_evaluation_attempts "
                    "(evaluation_attempt_ref, status, formal_rejection_code, "
                    "updated_at) SELECT evaluation_attempt_ref, "
                    "'measurement_rejected', "
                    "'formal_measurement_bounded_fixture', rowid "
                    "FROM rg_experiment_requests"
                )
            )

        with pytest.raises(
            OwnerConflict,
            match="writing_snapshot_experiment_limit_exceeded",
        ):
            graph.query_writing_experiment_terminal_cut("quest:bounded")
    finally:
        database.close()
