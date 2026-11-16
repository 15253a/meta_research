import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest
from alembic.operations import Operations
from migration_support import schema_head

from meta_research.migration import upgrade_database
from meta_research.owners.common import canonical_hash
from test_bundle_inbox_migration import _upgrade_to_revision


V1_REVISION = "0088_resource_selection_index"


def _schema(connection: sqlite3.Connection) -> list[tuple]:
    return connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY name"
    ).fetchall()


def test_fresh_bundle_dispatch_schema_allows_shared_decision_ref(tmp_path: Path) -> None:
    database = tmp_path / "empty-bundle-dispatch.sqlite3"
    upgrade_database(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            schema_head(),
        )
        columns = {
            row[1]: row
            for row in connection.execute("PRAGMA table_info(ar_bundle_dispatch_decisions)")
        }
        assert "selected_target_ref" not in columns
        assert columns["selected_target_refs_json"][3] == 1
        index_constraints = {
            (
                tuple(
                    column[2]
                    for column in connection.execute(
                        "SELECT * FROM pragma_index_info(?)", (name,)
                    )
                ),
                bool(unique),
            )
            for _, name, unique, _, _ in connection.execute("PRAGMA index_list(ar_target_launches)")
        }
        assert (("dispatch_decision_ref",), False) in index_constraints
        assert (("dispatch_decision_ref",), True) not in index_constraints
        assert (("target_ref",), True) in index_constraints
        assert (("idempotency_key",), True) in index_constraints
        assert any(
            row[2:5] == ("ar_bundle_dispatch_decisions", "dispatch_decision_ref", "decision_ref")
            for row in connection.execute("PRAGMA foreign_key_list(ar_target_launches)")
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)


def test_bundle_dispatch_batch_upgrade_preserves_v1_row_and_receipt_bytes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "signed-bundle-dispatch.sqlite3"
    _upgrade_to_revision(database, V1_REVISION)
    payload = {
        "schema_ref": "meta-research/bundle-dispatch-decision/v1",
        "run_ref": "old-run",
        "attempt_ref": "old-attempt",
        "fence_ref": "old-fence",
        "native_session_ref": "old-native",
        "graph_ref": "old-graph",
        "generation": 1,
        "frontier_hash": canonical_hash([]),
        "state_hash": canonical_hash({}),
        "action": "wait",
        "selected_target_ref": None,
        "rationale": "Wait for an accepted upstream outcome.",
    }
    decision_hash = canonical_hash(payload)
    receipt_hash = canonical_hash(
        {
            "schema_ref": "meta-research/owner-acceptance-receipt/v1",
            "issuer": "agent_runtime",
            "kind": "bundle_target_dispatch",
            "subject_ref": "old-decision",
            "bindings": {**payload, "decision_hash": decision_hash},
        }
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO ar_bundle_dispatch_decisions (
                decision_ref, run_ref, attempt_ref, fence_ref, native_session_ref,
                graph_ref, generation, frontier_json, frontier_hash, state_json,
                state_hash, action, selected_target_ref, rationale, decision_hash,
                idempotency_key, request_hash, receipt_ref, receipt_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "old-decision", "old-run", "old-attempt", "old-fence", "old-native",
                "old-graph", 1, "[]", payload["frontier_hash"], "{}", payload["state_hash"],
                "wait", None, payload["rationale"], decision_hash, "old-key",
                canonical_hash({"command": "record_bundle_dispatch_decision"}),
                "old-receipt", receipt_hash, 1.0,
            ),
        )
        schema = _schema(connection)
        connection.text_factory = bytes
        signed_decision = connection.execute("SELECT * FROM ar_bundle_dispatch_decisions").fetchone()

    with pytest.raises(RuntimeError, match="bundle_dispatch_batch_requires_new_data_root"):
        upgrade_database(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            V1_REVISION,
        )
        assert _schema(connection) == schema
        connection.text_factory = bytes
        assert connection.execute("SELECT * FROM ar_bundle_dispatch_decisions").fetchone() == signed_decision


def test_interrupted_bundle_dispatch_batch_upgrade_rolls_back_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "interrupted-bundle-dispatch.sqlite3"
    _upgrade_to_revision(database, V1_REVISION)
    with sqlite3.connect(database) as connection:
        original_schema = _schema(connection)
    original_batch_alter_table = Operations.batch_alter_table

    @contextmanager
    def interrupt_after_launch_rebuild(self, table_name, *args, **kwargs):
        with original_batch_alter_table(self, table_name, *args, **kwargs) as batch:
            yield batch
        if table_name == "ar_target_launches":
            raise OSError("injected Bundle dispatch batch migration interruption")

    with monkeypatch.context() as patch:
        patch.setattr(Operations, "batch_alter_table", interrupt_after_launch_rebuild)
        with pytest.raises(OSError, match="Bundle dispatch batch migration interruption"):
            upgrade_database(database)

    with sqlite3.connect(database) as connection:
        assert _schema(connection) == original_schema
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            V1_REVISION,
        )

    upgrade_database(database)
    with sqlite3.connect(database) as connection:
        upgraded_schema = _schema(connection)
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            schema_head(),
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    upgrade_database(database)
    with sqlite3.connect(database) as connection:
        assert _schema(connection) == upgraded_schema
