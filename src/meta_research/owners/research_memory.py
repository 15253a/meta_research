from __future__ import annotations

from pathlib import Path
from typing import Protocol

from sqlalchemy import text

from meta_research.database import Database
from meta_research.owners._sqlite_snapshot import (
    OwnerSnapshotQuery,
    SQLiteOwnerSnapshot,
)
from meta_research.owners.common import OwnerSnapshot


class ResearchMemoryInterface(Protocol):
    """Whole public Interface for immutable asset identity and custody."""

    def query_snapshot(self) -> OwnerSnapshot: ...


_SNAPSHOT = OwnerSnapshotQuery(
    owner="research_memory",
    statement=text(
        "SELECT revision, asset_count, object_count "
        "FROM research_memory_state WHERE singleton = 'owner'"
    ),
    fact_names=("asset_count", "object_count"),
)


def create_research_memory_interface(
    database: Database, object_store: Path
) -> ResearchMemoryInterface:
    return SQLiteOwnerSnapshot(
        database,
        _SNAPSHOT,
        additional_facts=lambda: {
            "managed_store_available": object_store.is_dir()
        },
    )
