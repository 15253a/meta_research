from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, URL, create_engine, event
from sqlalchemy.engine import Connection


class Database:
    """Process-local access to the daemon's SQLite writer."""

    def __init__(self, path: Path) -> None:
        url = URL.create("sqlite+pysqlite", database=str(path))
        self._engine: Engine = create_engine(url, future=True)
        self._write_lock = threading.RLock()
        event.listen(self._engine, "connect", _configure_sqlite)

    @contextmanager
    def read(self) -> Iterator[Connection]:
        with self._engine.connect() as connection:
            yield connection

    @contextmanager
    def write(self) -> Iterator[Connection]:
        with self._write_lock, self._engine.begin() as connection:
            yield connection

    def close(self) -> None:
        self._engine.dispose()


def _configure_sqlite(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=FULL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()
