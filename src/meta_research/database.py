from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from sqlalchemy import Engine, URL, create_engine, event
from sqlalchemy.engine import Connection
from meta_research.query_timing import before_sql, after_sql


class Database:
    """Process-local access to the daemon's SQLite writer."""

    def __init__(self, path: Path) -> None:
        url = URL.create("sqlite+pysqlite", database=str(path))
        self._engine: Engine = create_engine(url, future=True)
        self._write_lock = threading.RLock()
        self._read_cut: ContextVar[Connection | None] = ContextVar(
            "database_read_snapshot", default=None
        )
        self._read_cache: ContextVar[dict | None] = ContextVar(
            "database_read_snapshot_cache", default=None
        )
        event.listen(self._engine, "connect", _configure_sqlite)
        event.listen(self._engine, "before_cursor_execute", before_sql)
        event.listen(self._engine, "after_cursor_execute", after_sql)

    @contextmanager
    def read(self) -> Iterator[Connection]:
        current = self._read_cut.get()
        if current is not None:
            yield current
            return
        with self._engine.connect() as connection:
            yield connection

    @contextmanager
    def read_snapshot(self) -> Iterator[Connection]:
        """Pin nested Owner reads to one SQLite WAL snapshot, without a writer lock."""
        current = self._read_cut.get()
        if current is not None:
            yield current
            return
        with self._engine.connect() as connection:
            # A leading SELECT does not start a transaction in pysqlite.
            connection.exec_driver_sql("PRAGMA query_only=ON")
            connection.exec_driver_sql("BEGIN")
            token = self._read_cut.set(connection)
            cache_token = self._read_cache.set({})
            try:
                yield connection
            finally:
                self._read_cache.reset(cache_token)
                self._read_cut.reset(token)
                connection.rollback()
                connection.exec_driver_sql("PRAGMA query_only=OFF")

    @contextmanager
    def write(self) -> Iterator[Connection]:
        if self._read_cut.get() is not None:
            raise RuntimeError("write_inside_read_snapshot")
        with self._write_lock, self._engine.begin() as connection:
            yield connection

    @contextmanager
    def fenced_write(self) -> Iterator[Connection]:
        """Acquire SQLite's writer before any issuer/currentness reads.

        Pysqlite's deferred transaction mode does not emit ``BEGIN`` for a
        leading ``SELECT``.  A recovery-sensitive Owner boundary that verifies
        a Fence and only then inserts would therefore leave a cross-process
        check-to-write window.  This narrow seam deliberately acquires the
        SQLite writer up front; ordinary writes keep their existing deferred
        behavior.
        """

        if self._read_cut.get() is not None:
            raise RuntimeError("write_inside_read_snapshot")
        with self._write_lock, self._engine.connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    def close(self) -> None:
        self._engine.dispose()


def _configure_sqlite(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=FULL")
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()
