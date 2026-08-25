from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic.operations import Operations

from meta_research.database import Database
from meta_research.migration import upgrade_database
from meta_research.owners.agent_runtime import (
    BUNDLE_REPORT_RECEIPT_KIND,
    SQLiteAgentRuntimeReceiptVerifier,
)
from meta_research.owners.common import AcceptanceReceipt, OwnerConflict
from test_plan_stage_migration import _upgrade_to_revision


class _UnusedBundleReportEvidenceVerifier:
    """The legacy projection gate must fail before any evidence callback."""

    def __getattr__(self, _name: str):
        raise AssertionError("legacy report reached evidence verification")


def test_interrupted_bundle_migration_rolls_back_and_restarts_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "bundle-migration.sqlite3"
    _upgrade_to_revision(database, "0012_experiment_measurement")
    original_create_table = Operations.create_table
    failed_once = False

    def fail_after_target_table(self, table_name, *args, **kwargs):
        nonlocal failed_once
        created = original_create_table(self, table_name, *args, **kwargs)
        if table_name == "rg_targets" and not failed_once:
            failed_once = True
            raise OSError("injected bundle migration interruption")
        return created

    monkeypatch.setattr(Operations, "create_table", fail_after_target_table)
    with pytest.raises(OSError, match="bundle migration interruption"):
        upgrade_database(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0012_experiment_measurement",)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        graph_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(research_graph_state)")
        }
        experiment_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(ar_experiment_runs)")
        }
        request_ddl = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND "
            "name = 'ae_stage_run_requests'"
        ).fetchone()[0]
    assert "rg_target_graphs" not in tables
    assert "rg_targets" not in tables
    assert "ae_stage_run_requests_pre_bundle" not in tables
    assert "target_graph_count" not in graph_columns
    assert "bundle_target_ref" not in experiment_columns
    assert "'bundle'" not in request_ddl

    monkeypatch.setattr(Operations, "create_table", original_create_table)
    _upgrade_to_revision(database, "0013_bundle_target_dag")
    _upgrade_to_revision(database, "0013_bundle_target_dag")

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0013_bundle_target_dag",)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        graph_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(research_graph_state)")
        }
        experiment_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(ar_experiment_runs)")
        }
        request_ddl = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND "
            "name = 'ae_stage_run_requests'"
        ).fetchone()[0]
        foreign_key_failures = connection.execute("PRAGMA foreign_key_check").fetchall()
        integrity = connection.execute("PRAGMA quick_check").fetchone()
    assert {
        "ar_bundle_dispatch_decisions",
        "ar_target_run_admissions",
        "rg_target_graphs",
        "rg_targets",
        "rg_target_run_bindings",
        "rg_target_commits",
    } <= tables
    assert {
        "target_graph_count",
        "target_count",
        "target_commit_count",
    } <= graph_columns
    assert "bundle_target_ref" in experiment_columns
    assert "'bundle'" in request_ddl
    assert foreign_key_failures == []
    assert integrity == ("ok",)


def test_interrupted_rolling_target_migration_is_atomic_and_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "bundle-rolling-migration.sqlite3"
    _upgrade_to_revision(database, "0016_semantic_mcp_harness")
    original_create_table = Operations.create_table
    failed_once = False

    def fail_after_append_table(self, table_name, *args, **kwargs):
        nonlocal failed_once
        created = original_create_table(self, table_name, *args, **kwargs)
        if table_name == "rg_target_graph_appends" and not failed_once:
            failed_once = True
            raise OSError("injected rolling target migration interruption")
        return created

    monkeypatch.setattr(Operations, "create_table", fail_after_append_table)
    with pytest.raises(OSError, match="rolling target migration interruption"):
        upgrade_database(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0016_semantic_mcp_harness",)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        target_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(rg_targets)")
        }
    assert "ar_bundle_target_proposals" not in tables
    assert "rg_target_graph_appends" not in tables
    assert "append_ref" not in target_columns

    monkeypatch.setattr(Operations, "create_table", original_create_table)
    upgrade_database(database)
    upgrade_database(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0030_writing_delivery",)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        target_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(rg_targets)")
        }
        foreign_key_failures = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()
        integrity = connection.execute("PRAGMA quick_check").fetchone()
    assert {"ar_bundle_target_proposals", "rg_target_graph_appends"} <= tables
    assert "append_ref" in target_columns
    assert foreign_key_failures == []
    assert integrity == ("ok",)


def test_0021_source_only_report_survives_0022_but_cannot_authorize_completion(
    tmp_path: Path,
) -> None:
    database = tmp_path / "bundle-report-0021-to-0022.sqlite3"
    _upgrade_to_revision(database, "0021_bundle_report_closure")
    digest = "0" * 64
    with sqlite3.connect(database) as connection:
        # These immediate parents make the report row itself FK-valid.  Their
        # deeper lineage is intentionally irrelevant to this migration test.
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "INSERT INTO ar_stage_runs (run_ref, request_ref, cycle_ref, "
            "stage, epoch, context_pack_ref, context_pack_hash, "
            "runtime_binding_json, runtime_binding_hash, request_receipt_ref, "
            "request_receipt_hash, status, current_attempt_ref, "
            "root_session_ref, current_fence_ref, admission_key, "
            "admission_hash, created_at, updated_at) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-bundle-run",
                "legacy-bundle-request",
                "legacy-cycle",
                "bundle",
                1,
                "legacy-context",
                digest,
                "{}",
                digest,
                "legacy-request-receipt",
                digest,
                "running",
                "legacy-bundle-attempt",
                "legacy-bundle-session",
                "legacy-bundle-fence",
                "legacy-bundle-admission",
                digest,
                1.0,
                1.0,
            ),
        )
        connection.execute(
            "INSERT INTO ar_stage_attempts (attempt_ref, run_ref, generation, "
            "root_session_ref, fence_ref, status, created_at) VALUES "
            "(?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-bundle-attempt",
                "legacy-bundle-run",
                1,
                "legacy-bundle-session",
                "legacy-bundle-fence",
                "running",
                1.0,
            ),
        )
        connection.execute(
            "INSERT INTO ar_execution_fences (fence_ref, run_ref, "
            "attempt_ref, generation, status, issued_at) VALUES "
            "(?, ?, ?, ?, ?, ?)",
            (
                "legacy-bundle-fence",
                "legacy-bundle-run",
                "legacy-bundle-attempt",
                1,
                "current",
                1.0,
            ),
        )
        connection.execute(
            "INSERT INTO rg_formal_plan_content_acceptances "
            "(acceptance_ref, formal_plan_ref, decision_ref, request_ref, "
            "submission_ref, plan_content_ref, plan_document_hash, "
            "plan_content_receipt_ref, plan_content_receipt_hash, "
            "formal_plan_receipt_ref, formal_plan_receipt_hash, "
            "idempotency_key, request_hash, receipt_ref, receipt_hash, "
            "accepted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-plan-acceptance",
                "legacy-formal-plan",
                "legacy-plan-decision",
                "legacy-bundle-request",
                "legacy-plan-submission",
                "legacy-plan-content",
                digest,
                "legacy-plan-content-receipt",
                digest,
                "legacy-formal-plan-receipt",
                digest,
                "legacy-plan-accept",
                digest,
                "legacy-plan-projection-source-receipt",
                digest,
                1.0,
            ),
        )
        connection.execute(
            "INSERT INTO rg_target_graphs (graph_ref, request_ref, run_ref, "
            "attempt_ref, fence_ref, submission_ref, cycle_ref, quest_ref, "
            "formal_plan_ref, plan_content_ref, plan_document_hash, "
            "context_pack_ref, context_pack_hash, target_plan_json, "
            "target_plan_hash, execution_receipt_ref, execution_receipt_hash, "
            "receipt_ref, receipt_hash, accepted_at) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-target-graph",
                "legacy-bundle-request",
                "legacy-bundle-run",
                "legacy-bundle-attempt",
                "legacy-bundle-fence",
                "legacy-target-submission",
                "legacy-cycle",
                "legacy-quest",
                "legacy-formal-plan",
                "legacy-plan-content",
                digest,
                "legacy-context",
                digest,
                "{}",
                digest,
                "legacy-target-execution-receipt",
                digest,
                "legacy-target-graph-receipt",
                digest,
                1.0,
            ),
        )
        connection.execute(
            "INSERT INTO ar_bundle_reports VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-bundle-report",
                "legacy-bundle-run",
                1,
                "legacy-bundle-request",
                "legacy-bundle-attempt",
                "legacy-bundle-fence",
                "legacy-formal-plan",
                digest,
                "legacy-plan-projection-source-receipt",
                digest,
                "legacy-target-graph",
                0,
                digest,
                digest,
                "legacy-target-graph-receipt",
                digest,
                "[]",
                digest,
                "[]",
                digest,
                "[]",
                digest,
                "[]",
                digest,
                "{}",
                digest,
                "blocked",
                "legacy-bundle-report-key",
                digest,
                "legacy-bundle-report-receipt",
                digest,
                1.0,
            ),
        )
        connection.commit()

    _upgrade_to_revision(database, "0022_target_run_runtime")
    with sqlite3.connect(database) as connection:
        projection = connection.execute(
            "SELECT formal_plan_projection_digest, "
            "formal_plan_projection_receipt_ref, "
            "formal_plan_projection_receipt_hash, completion_contract_hash, "
            "formal_plan_briefs_hash FROM ar_bundle_reports WHERE "
            "report_ref = ?",
            ("legacy-bundle-report",),
        ).fetchone()
        assert projection == (None, None, None, None, None)
        assert connection.execute(
            "PRAGMA foreign_key_check('ar_bundle_reports')"
        ).fetchall() == []
        assert connection.execute(
            "SELECT COUNT(*) FROM ae_stage_commits WHERE outcome_ref = ?",
            ("legacy-bundle-report",),
        ).fetchone() == (0,)

    owner_database = Database(database)
    try:
        verifier = SQLiteAgentRuntimeReceiptVerifier(
            owner_database,
            bundle_report_evidence_verifier=(
                _UnusedBundleReportEvidenceVerifier()  # type: ignore[arg-type]
            ),
        )
        with pytest.raises(
            OwnerConflict,
            match="bundle_report_formal_plan_projection_required",
        ):
            verifier.verify_bundle_report_receipt(
                report_ref="legacy-bundle-report",
                receipt=AcceptanceReceipt(
                    issuer="agent_runtime",
                    kind=BUNDLE_REPORT_RECEIPT_KIND,
                    receipt_ref="legacy-bundle-report-receipt",
                    subject_ref="legacy-bundle-report",
                    payload_hash=digest,
                ),
            )
    finally:
        owner_database.close()
