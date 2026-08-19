from __future__ import annotations

from typing import Protocol

from sqlalchemy import text

from meta_research.database import Database
from meta_research.owners._sqlite_snapshot import (
    OwnerSnapshotQuery,
    SQLiteOwnerSnapshot,
)
from meta_research.owners.common import OwnerSnapshot


class AgentRuntimeInterface(Protocol):
    """Whole public Interface for Run, Attempt, Session, and Fence authority."""

    def query_snapshot(self) -> OwnerSnapshot: ...


_SNAPSHOT = OwnerSnapshotQuery(
    owner="agent_runtime",
    statement=text(
        "SELECT revision, active_run_count "
        "FROM agent_runtime_state WHERE singleton = 'owner'"
    ),
    fact_names=("active_run_count",),
)


def create_agent_runtime_interface(database: Database) -> AgentRuntimeInterface:
    return SQLiteOwnerSnapshot(database, _SNAPSHOT)
