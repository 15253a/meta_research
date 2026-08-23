from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import URL, create_engine, event

from meta_research.database import _configure_sqlite


def _configure_migration_sqlite(dbapi_connection, connection_record) -> None:
    _configure_sqlite(dbapi_connection, connection_record)
    # SQLite rewrites referenced table names during ALTER TABLE and enforces
    # parent drops while a schema-rebuild migration is still in flight.  The
    # migration connection therefore performs DDL with FK enforcement off;
    # every normal Database connection re-enables it via _configure_sqlite.
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=OFF")
    cursor.close()
    # Python 3.11's sqlite3 legacy mode otherwise autocommits DDL before
    # Alembic can roll the revision back.
    dbapi_connection.isolation_level = None


def _begin_migration_transaction(connection) -> None:
    connection.exec_driver_sql("BEGIN IMMEDIATE")


def upgrade_database(path: Path) -> None:
    url = URL.create("sqlite+pysqlite", database=str(path))
    engine = create_engine(url, future=True)
    event.listen(engine, "connect", _configure_migration_sqlite)
    event.listen(engine, "begin", _begin_migration_transaction)
    config = Config()
    config.set_main_option(
        "script_location", str(files("meta_research.migrations"))
    )
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
    finally:
        engine.dispose()
