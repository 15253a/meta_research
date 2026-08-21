from __future__ import annotations

import json
import time
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Connection

from meta_research import __version__
from meta_research.database import Database


@dataclass(frozen=True)
class DurableEvent:
    revision: int
    event_type: str
    payload: dict[str, object]


@dataclass(frozen=True)
class FeedPage:
    events: tuple[DurableEvent, ...]
    current_revision: int
    revision_gap: bool


@dataclass(frozen=True)
class FeedReadiness:
    database_ready: bool
    current_revision: int
    projection_revision: int


class DurableFeed:
    """Projection transport and cursor storage; never an authoritative Owner."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def ensure_initialized(self) -> int:
        with self._database.write() as connection:
            row = connection.execute(
                text(
                    "SELECT revision FROM durable_feed "
                    "WHERE event_type = 'system.ready' ORDER BY revision LIMIT 1"
                )
            ).first()
            if row is None:
                result = connection.execute(
                    text(
                        "INSERT INTO durable_feed (event_type, payload_json, recorded_at) "
                        "VALUES ('system.ready', :payload, :recorded_at)"
                    ),
                    {
                        "payload": json.dumps(
                            {"product_version": __version__}, separators=(",", ":")
                        ),
                        "recorded_at": time.time(),
                    },
                )
                revision = int(result.lastrowid)
            else:
                revision = int(row.revision)
            current = int(
                connection.execute(
                    text("SELECT COALESCE(MAX(revision), 0) FROM durable_feed")
                ).scalar_one()
            )
            connection.execute(
                text(
                    "UPDATE projection_offsets SET revision = :revision "
                    "WHERE projection_name = 'public_snapshot'"
                ),
                {"revision": current},
            )
        return revision

    def current_revision(self) -> int:
        with self._database.read() as connection:
            return int(
                connection.execute(
                    text("SELECT COALESCE(MAX(revision), 0) FROM durable_feed")
                ).scalar_one()
            )

    def record(
        self,
        connection: Connection,
        event_type: str,
        payload: dict[str, object],
    ) -> int:
        """Append an Owner event inside the caller's authoritative transaction."""
        result = connection.execute(
            text(
                "INSERT INTO durable_feed (event_type, payload_json, recorded_at) "
                "VALUES (:event_type, :payload, :recorded_at)"
            ),
            {
                "event_type": event_type,
                "payload": json.dumps(payload, sort_keys=True, separators=(",", ":")),
                "recorded_at": time.time(),
            },
        )
        revision = int(result.lastrowid)
        connection.execute(
            text(
                "UPDATE projection_offsets SET revision = :revision "
                "WHERE projection_name = 'public_snapshot'"
            ),
            {"revision": revision},
        )
        return revision

    def query_readiness(self) -> FeedReadiness:
        with self._database.read() as connection:
            database_ready = connection.execute(text("SELECT 1")).scalar_one() == 1
            current_revision = int(
                connection.execute(
                    text("SELECT COALESCE(MAX(revision), 0) FROM durable_feed")
                ).scalar_one()
            )
            projection_revision = int(
                connection.execute(
                    text(
                        "SELECT revision FROM projection_offsets "
                        "WHERE projection_name = 'public_snapshot'"
                    )
                ).scalar_one()
            )
        return FeedReadiness(
            database_ready=database_ready,
            current_revision=current_revision,
            projection_revision=projection_revision,
        )

    def read_after(self, last_revision: int, *, limit: int = 100) -> FeedPage:
        with self._database.read() as connection:
            bounds = connection.execute(
                text(
                    "SELECT COALESCE(MIN(revision), 0) AS minimum, "
                    "COALESCE(MAX(revision), 0) AS maximum FROM durable_feed"
                )
            ).one()
            minimum = int(bounds.minimum)
            maximum = int(bounds.maximum)
            gap = last_revision > maximum or (
                minimum > 0 and last_revision < minimum - 1
            )
            if gap:
                return FeedPage((), maximum, True)
            rows = connection.execute(
                text(
                    "SELECT revision, event_type, payload_json FROM durable_feed "
                    "WHERE revision > :last_revision ORDER BY revision LIMIT :limit"
                ),
                {"last_revision": last_revision, "limit": limit},
            ).all()
        return FeedPage(
            tuple(
                DurableEvent(
                    revision=int(row.revision),
                    event_type=row.event_type,
                    payload=json.loads(row.payload_json),
                )
                for row in rows
            ),
            maximum,
            False,
        )

    def read_event_type(self, event_type: str) -> tuple[DurableEvent, ...]:
        """Read one event family as a routing index, never as authority.

        Application workers use this narrow query to discover aggregate refs and
        then revalidate every returned identity through its State Owner.  It
        avoids replaying unrelated history on every idle worker poll.
        """

        with self._database.read() as connection:
            rows = connection.execute(
                text(
                    "SELECT revision, event_type, payload_json FROM durable_feed "
                    "WHERE event_type = :event_type ORDER BY revision"
                ),
                {"event_type": event_type},
            ).all()
        return tuple(
            DurableEvent(
                revision=int(row.revision),
                event_type=row.event_type,
                payload=json.loads(row.payload_json),
            )
            for row in rows
        )
