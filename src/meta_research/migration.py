from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import URL, create_engine, event

from meta_research.database import _configure_sqlite


def upgrade_database(path: Path) -> None:
    url = URL.create("sqlite+pysqlite", database=str(path))
    engine = create_engine(url, future=True)
    event.listen(engine, "connect", _configure_sqlite)
    config = Config()
    config.set_main_option(
        "script_location", str(files("meta_research.migrations"))
    )
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    engine.dispose()
