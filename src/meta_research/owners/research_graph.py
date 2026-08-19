from __future__ import annotations

from typing import Protocol

from sqlalchemy import text

from meta_research.database import Database
from meta_research.owners._sqlite_snapshot import (
    OwnerSnapshotQuery,
    SQLiteOwnerSnapshot,
)
from meta_research.owners.common import OwnerSnapshot


class ResearchGraphInterface(Protocol):
    """Whole public Interface for authoritative research semantics."""

    def query_snapshot(self) -> OwnerSnapshot: ...


_SNAPSHOT = OwnerSnapshotQuery(
    owner="research_graph",
    statement=text(
        "SELECT revision, quest_count, question_count "
        "FROM research_graph_state WHERE singleton = 'owner'"
    ),
    fact_names=("quest_count", "question_count"),
)


def create_research_graph_interface(database: Database) -> ResearchGraphInterface:
    return SQLiteOwnerSnapshot(database, _SNAPSHOT)
