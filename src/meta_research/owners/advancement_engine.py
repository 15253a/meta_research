from __future__ import annotations

from typing import Protocol

from sqlalchemy import text

from meta_research.database import Database
from meta_research.owners._sqlite_snapshot import (
    OwnerSnapshotQuery,
    SQLiteOwnerSnapshot,
)
from meta_research.owners.common import OwnerSnapshot


class AdvancementEngineInterface(Protocol):
    """Whole public Interface for Cycle, Stage, and Foreground authority."""

    def query_snapshot(self) -> OwnerSnapshot: ...


_SNAPSHOT = OwnerSnapshotQuery(
    owner="advancement_engine",
    statement=text(
        "SELECT revision, foreground_cycle_count "
        "FROM advancement_engine_state WHERE singleton = 'owner'"
    ),
    fact_names=("foreground_cycle_count",),
)


def create_advancement_engine_interface(
    database: Database,
) -> AdvancementEngineInterface:
    return SQLiteOwnerSnapshot(database, _SNAPSHOT)
