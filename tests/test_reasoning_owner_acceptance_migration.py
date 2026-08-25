from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from test_reasoning_successor_context_migration import _migrate


@pytest.mark.parametrize(
    ("state_table", "counter"),
    (
        ("research_memory_state", "reasoning_content_count"),
        ("research_graph_state", "reasoning_outcome_count"),
    ),
)
def test_0030_downgrade_rejects_durable_reasoning_owner_facts(
    tmp_path: Path,
    state_table: str,
    counter: str,
) -> None:
    database = tmp_path / f"reasoning-owner-downgrade-{counter}.sqlite3"
    _migrate(database, "0030_reasoning_owner_acceptance")
    with sqlite3.connect(database) as connection:
        connection.execute(f"UPDATE {state_table} SET {counter} = 1")

    with pytest.raises(
        RuntimeError,
        match="cannot downgrade reasoning owner acceptance",
    ):
        _migrate(database, "0029_target_root_lifecycle", downgrade=True)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0030_reasoning_owner_acceptance",)
        assert connection.execute(
            f"SELECT {counter} FROM {state_table}"
        ).fetchone() == (1,)
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
