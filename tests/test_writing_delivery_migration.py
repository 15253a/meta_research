from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic.operations import Operations

from test_migration_recovery import _upgrade_to_revision


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _seed_legacy_report(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO hc_command_intents (intent_id, scope_ref, current_revision, "
        "status, created_at, updated_at) VALUES ('legacy-report-intent', "
        "'legacy-writing-scope', 1, 'confirmed', 8.0, 9.0)"
    )
    connection.execute(
        "INSERT INTO rg_quests (quest_ref, initialization_id, draft_revision, "
        "draft_hash, proposal_ref, proposal_hash, preview_ref, preview_hash, "
        "goal_json, confirmation_ref, confirmation_hash, receipt_ref, "
        "receipt_hash, accepted_at) VALUES ('legacy-quest', 'legacy-init', 1, ?, "
        "'legacy-proposal', ?, 'legacy-preview', ?, '{}', 'legacy-confirm', ?, "
        "'legacy-quest-receipt', ?, 7.0)",
        tuple(str(index) * 64 for index in range(1, 6)),
    )
    connection.execute(
        "INSERT INTO ar_writing_runs (run_ref, intent_id, quest_ref, document_type, "
        "intent_json, intent_hash, snapshot_ref, snapshot_json, snapshot_hash, "
        "confirmation_ref, confirmation_hash, status, failure_code, "
        "execution_budget_json, execution_budget_hash, output_bytes, attempt_ref, "
        "attempt_generation, root_session_ref, native_session_ref, fence_ref, "
        "predecessor_version_ref, feedback_json, feedback_hash, "
        "runtime_binding_json, runtime_binding_hash, created_at, updated_at) VALUES "
        "('legacy-report-run', 'legacy-report-intent', 'legacy-quest', 'report', "
        "'{}', ?, 'legacy-snapshot', '{}', ?, 'legacy-confirmation', ?, 'active', "
        "NULL, '{}', ?, 0, 'legacy-attempt', 1, 'legacy-root', NULL, "
        "'legacy-fence', NULL, '[]', ?, '{}', ?, 10.0, 11.0)",
        ("1" * 64, "2" * 64, "3" * 64, "4" * 64, "5" * 64, "6" * 64),
    )
    connection.commit()


def test_0030_preserves_report_rows_and_widens_document_type(tmp_path: Path) -> None:
    path = tmp_path / "writing-delivery.sqlite3"
    _upgrade_to_revision(path, "0029_target_root_lifecycle")
    with sqlite3.connect(path) as connection:
        _seed_legacy_report(connection)

    _upgrade_to_revision(path, "0034_writing_delivery")
    _upgrade_to_revision(path, "0034_writing_delivery")

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0034_writing_delivery",)
        assert {
            "ar_writing_delivery_operations",
            "ar_writing_delivery_observations",
            "ar_writing_delivery_receipts",
        } <= _tables(connection)
        assert connection.execute(
            "SELECT run_ref, intent_id, document_type, intent_hash, snapshot_hash, "
            "confirmation_hash, runtime_binding_hash, created_at, updated_at FROM "
            "ar_writing_runs WHERE run_ref = 'legacy-report-run'"
        ).fetchone() == (
            "legacy-report-run",
            "legacy-report-intent",
            "report",
            "1" * 64,
            "2" * 64,
            "3" * 64,
            "6" * 64,
            10.0,
            11.0,
        )
        for document_type in ("paper", "presentation"):
            connection.execute(
                "UPDATE ar_writing_runs SET document_type = ? WHERE run_ref = "
                "'legacy-report-run'",
                (document_type,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE ar_writing_runs SET document_type = 'pdf-extension' WHERE "
                "run_ref = 'legacy-report-run'"
            )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)


def test_interrupted_0030_rolls_back_and_retry_is_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "interrupted-writing-delivery.sqlite3"
    _upgrade_to_revision(path, "0029_target_root_lifecycle")
    with sqlite3.connect(path) as connection:
        _seed_legacy_report(connection)

    original_create_table = Operations.create_table
    failed_once = False

    def fail_during_delivery(self, table_name, *args, **kwargs):
        nonlocal failed_once
        if table_name == "ar_writing_delivery_operations" and not failed_once:
            failed_once = True
            raise OSError("injected 0030 migration interruption")
        return original_create_table(self, table_name, *args, **kwargs)

    monkeypatch.setattr(Operations, "create_table", fail_during_delivery)
    with pytest.raises(OSError, match="injected 0030 migration interruption"):
        _upgrade_to_revision(path, "0034_writing_delivery")

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0029_target_root_lifecycle",)
        assert connection.execute(
            "SELECT document_type FROM ar_writing_runs WHERE run_ref = "
            "'legacy-report-run'"
        ).fetchone() == ("report",)
        assert not {
            "ar_writing_delivery_operations",
            "ar_writing_delivery_observations",
            "ar_writing_delivery_receipts",
        } & _tables(connection)

    monkeypatch.setattr(Operations, "create_table", original_create_table)
    _upgrade_to_revision(path, "0034_writing_delivery")
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0034_writing_delivery",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
