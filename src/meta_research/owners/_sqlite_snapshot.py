from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.sql.elements import TextClause

from meta_research.database import Database
from meta_research.owners.common import OwnerFact, OwnerSnapshot


@dataclass(frozen=True)
class OwnerSnapshotQuery:
    owner: str
    statement: TextClause
    fact_names: tuple[str, ...]


class SQLiteOwnerSnapshot:
    """Private persistence Adapter shared by the five semantic Interfaces."""

    def __init__(
        self,
        database: Database,
        query: OwnerSnapshotQuery,
    ) -> None:
        self._database = database
        self._query = query

    def query_snapshot(self) -> OwnerSnapshot:
        with self._database.read() as connection:
            row = connection.execute(self._query.statement).one()._mapping
        facts: dict[str, OwnerFact] = {
            name: row[name] for name in self._query.fact_names
        }
        return OwnerSnapshot(
            owner=self._query.owner,
            revision=int(row["revision"]),
            facts=facts,
        )
