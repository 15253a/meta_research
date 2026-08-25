from __future__ import annotations

from pathlib import Path
import threading

from sqlalchemy import text

from meta_research.database import Database


def test_fenced_write_acquires_sqlite_writer_before_currentness_read(
    tmp_path: Path,
) -> None:
    path = tmp_path / "fenced-write.sqlite3"
    current = Database(path)
    recovery = Database(path)
    try:
        with current.write() as connection:
            connection.execute(
                text(
                    "CREATE TABLE frontier (singleton INTEGER PRIMARY KEY, "
                    "generation INTEGER NOT NULL)"
                )
            )
            connection.execute(
                text(
                    "CREATE TABLE effects (generation INTEGER NOT NULL)"
                )
            )
            connection.execute(
                text("INSERT INTO frontier VALUES (1, 1)")
            )

        recovery_attempted = threading.Event()
        recovery_committed = threading.Event()

        def recover() -> None:
            with recovery.write() as connection:
                recovery_attempted.set()
                connection.execute(
                    text(
                        "UPDATE frontier SET generation = 2 WHERE singleton = 1"
                    )
                )
            recovery_committed.set()

        worker = threading.Thread(target=recover, daemon=True)
        with current.fenced_write() as connection:
            generation = connection.execute(
                text("SELECT generation FROM frontier WHERE singleton = 1")
            ).scalar_one()
            assert generation == 1
            worker.start()
            assert recovery_attempted.wait(timeout=1.0)
            # The second Database reached its UPDATE but cannot commit a
            # successor while this Fence's issuer read and effect are open.
            assert not recovery_committed.wait(timeout=0.25)
            connection.execute(
                text("INSERT INTO effects VALUES (:generation)"),
                {"generation": generation},
            )

        worker.join(timeout=2.0)
        assert not worker.is_alive()
        assert recovery_committed.is_set()
        with current.read() as connection:
            assert connection.execute(
                text("SELECT generation FROM frontier WHERE singleton = 1")
            ).scalar_one() == 2
            assert connection.execute(
                text("SELECT generation FROM effects")
            ).scalars().all() == [1]
    finally:
        recovery.close()
        current.close()
