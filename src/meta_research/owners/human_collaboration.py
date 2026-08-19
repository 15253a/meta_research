from __future__ import annotations

from typing import Protocol

from sqlalchemy import text

from meta_research.database import Database
from meta_research.owners._sqlite_snapshot import (
    OwnerSnapshotQuery,
    SQLiteOwnerSnapshot,
)
from meta_research.owners.common import OwnerSnapshot


class HumanCollaborationInterface(Protocol):
    """Whole public Interface for intent, confirmation, and authorization facts."""

    def query_snapshot(self) -> OwnerSnapshot: ...


_SNAPSHOT = OwnerSnapshotQuery(
    owner="human_collaboration",
    statement=text(
        "SELECT revision, pending_intent_count, authorization_count "
        "FROM human_collaboration_state WHERE singleton = 'owner'"
    ),
    fact_names=("pending_intent_count", "authorization_count"),
)


def create_human_collaboration_interface(
    database: Database,
) -> HumanCollaborationInterface:
    return SQLiteOwnerSnapshot(database, _SNAPSHOT)
