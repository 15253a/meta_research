"""Add FormalPlan content acceptance and durable BundleReport closure.

Revision ID: 0021_bundle_report_closure
Revises: 0020_bundle_reuse_proofs
Create Date: 2026-08-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0021_bundle_report_closure"
down_revision = "0020_bundle_reuse_proofs"
branch_labels = None
depends_on = None


def _hash(name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"length({name}) = 64")


def _counter(name: str) -> sa.Column:
    return sa.Column(name, sa.Integer(), nullable=False, server_default="0")


def _replace_stage_commits() -> None:
    """Permit new BundleReport outcomes while retaining legacy rows read-only."""

    source = "ae_stage_commits"
    backup = "ae_stage_commits_pre_bundle_report"
    columns = (
        "commit_ref",
        "request_ref",
        "cycle_ref",
        "stage",
        "epoch",
        "run_ref",
        "outcome_ref",
        "outcome_kind",
        "disposition",
        "run_completion_receipt_ref",
        "run_completion_receipt_hash",
        "outcome_receipt_ref",
        "outcome_receipt_hash",
        "closure_json",
        "closure_hash",
        "basis_kind",
        "basis_ref",
        "basis_receipt_issuer",
        "basis_receipt_kind",
        "basis_receipt_subject_ref",
        "basis_receipt_ref",
        "basis_receipt_hash",
        "idempotency_key",
        "request_hash",
        "receipt_ref",
        "receipt_hash",
        "committed_at",
    )
    connection = op.get_bind()
    connection.exec_driver_sql("PRAGMA legacy_alter_table=ON")
    try:
        op.rename_table(source, backup)
        op.create_table(
            source,
            sa.Column("commit_ref", sa.String(64), primary_key=True),
            sa.Column("request_ref", sa.String(64), nullable=True, unique=True),
            sa.Column("cycle_ref", sa.String(64), nullable=False),
            sa.Column("stage", sa.String(24), nullable=False),
            sa.Column("epoch", sa.Integer(), nullable=False),
            sa.Column("run_ref", sa.String(96), nullable=True, unique=True),
            sa.Column("outcome_ref", sa.String(96), nullable=True),
            sa.Column("outcome_kind", sa.String(64), nullable=True),
            sa.Column("disposition", sa.String(16), nullable=False),
            sa.Column("run_completion_receipt_ref", sa.String(96), nullable=True),
            sa.Column("run_completion_receipt_hash", sa.String(64), nullable=True),
            sa.Column("outcome_receipt_ref", sa.String(96), nullable=True),
            sa.Column("outcome_receipt_hash", sa.String(64), nullable=True),
            sa.Column("closure_json", sa.Text(), nullable=True),
            sa.Column("closure_hash", sa.String(64), nullable=True),
            sa.Column("basis_kind", sa.String(64), nullable=True),
            sa.Column("basis_ref", sa.String(96), nullable=True),
            sa.Column("basis_receipt_issuer", sa.String(64), nullable=True),
            sa.Column("basis_receipt_kind", sa.String(96), nullable=True),
            sa.Column("basis_receipt_subject_ref", sa.String(96), nullable=True),
            sa.Column("basis_receipt_ref", sa.String(96), nullable=True),
            sa.Column("basis_receipt_hash", sa.String(64), nullable=True),
            sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
            sa.Column("request_hash", sa.String(64), nullable=False),
            sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
            sa.Column("receipt_hash", sa.String(64), nullable=False),
            sa.Column("committed_at", sa.Float(), nullable=False),
            sa.ForeignKeyConstraint(
                ["request_ref"], ["ae_stage_run_requests.request_ref"]
            ),
            sa.ForeignKeyConstraint(["run_ref"], ["ar_stage_runs.run_ref"]),
            sa.UniqueConstraint("stage", "outcome_ref"),
            sa.UniqueConstraint("cycle_ref", "stage", "epoch"),
            sa.CheckConstraint("stage IN ('idea', 'plan', 'bundle', 'reasoning')"),
            sa.CheckConstraint(
                "disposition IN ('completed', 'skipped', 'exhausted')"
            ),
            sa.CheckConstraint("epoch >= 1"),
            sa.CheckConstraint(
                "(stage != 'bundle' AND disposition = 'completed' AND "
                "request_ref IS NOT NULL AND run_ref IS NOT NULL AND outcome_ref "
                "IS NOT NULL AND outcome_kind IS NOT NULL AND "
                "run_completion_receipt_ref IS NOT NULL AND "
                "length(run_completion_receipt_hash) = 64 AND "
                "outcome_receipt_ref IS NOT NULL AND length(outcome_receipt_hash) "
                "= 64 AND closure_json IS NULL AND closure_hash IS NULL AND "
                "basis_kind IS NULL AND basis_ref IS NULL AND "
                "basis_receipt_issuer IS NULL AND basis_receipt_kind IS NULL AND "
                "basis_receipt_subject_ref IS NULL AND basis_receipt_ref IS NULL "
                "AND basis_receipt_hash IS NULL) OR "
                "(stage = 'bundle' AND disposition = 'completed' AND request_ref "
                "IS NOT NULL AND run_ref IS NOT NULL AND outcome_ref IS NOT NULL "
                "AND outcome_kind IN ('target_graph', 'bundle_report') AND "
                "run_completion_receipt_ref IS NOT NULL AND "
                "length(run_completion_receipt_hash) = 64 AND outcome_receipt_ref "
                "IS NOT NULL AND length(outcome_receipt_hash) = 64 AND "
                "closure_json IS NOT NULL AND length(closure_hash) = 64 AND "
                "basis_kind IS NULL AND basis_ref IS NULL AND "
                "basis_receipt_issuer IS NULL AND basis_receipt_kind IS NULL AND "
                "basis_receipt_subject_ref IS NULL AND basis_receipt_ref IS NULL "
                "AND basis_receipt_hash IS NULL) OR "
                "(stage = 'bundle' AND disposition = 'skipped' AND request_ref IS "
                "NOT NULL AND run_ref IS NULL AND outcome_ref IS NOT NULL AND "
                "outcome_kind = 'bundle_skip' AND run_completion_receipt_ref IS "
                "NULL AND run_completion_receipt_hash IS NULL AND "
                "outcome_receipt_ref IS NOT NULL AND length(outcome_receipt_hash) "
                "= 64 AND closure_json IS NOT NULL AND length(closure_hash) = 64 "
                "AND basis_kind IS NULL AND basis_ref IS NULL AND "
                "basis_receipt_issuer IS NULL AND basis_receipt_kind IS NULL AND "
                "basis_receipt_subject_ref IS NULL AND basis_receipt_ref IS NULL "
                "AND basis_receipt_hash IS NULL) OR "
                "(disposition = 'skipped' AND request_ref IS NULL AND run_ref IS "
                "NULL AND outcome_ref IS NULL AND outcome_kind IS NULL AND "
                "run_completion_receipt_ref IS NULL AND "
                "run_completion_receipt_hash IS NULL AND outcome_receipt_ref IS "
                "NULL AND outcome_receipt_hash IS NULL AND closure_json IS NULL "
                "AND closure_hash IS NULL AND basis_kind IS NOT NULL AND "
                "basis_ref IS NOT NULL AND basis_receipt_issuer IS NOT NULL AND "
                "basis_receipt_kind IS NOT NULL AND basis_receipt_subject_ref IS "
                "NOT NULL AND basis_receipt_ref IS NOT NULL AND "
                "length(basis_receipt_hash) = 64) OR "
                "(disposition = 'exhausted' AND request_ref IS NOT NULL AND "
                "run_ref IS NOT NULL AND outcome_ref IS NULL AND outcome_kind IS "
                "NULL AND run_completion_receipt_ref IS NOT NULL AND "
                "length(run_completion_receipt_hash) = 64 AND "
                "outcome_receipt_ref IS NULL AND outcome_receipt_hash IS NULL AND "
                "closure_json IS NULL AND closure_hash IS NULL AND basis_kind IS "
                "NOT NULL AND basis_ref IS NOT NULL AND basis_receipt_issuer IS "
                "NOT NULL AND basis_receipt_kind IS NOT NULL AND "
                "basis_receipt_subject_ref IS NOT NULL AND basis_receipt_ref IS "
                "NOT NULL AND length(basis_receipt_hash) = 64)"
            ),
            _hash("request_hash"),
            _hash("receipt_hash"),
        )
        column_list = ", ".join(columns)
        op.execute(
            f"INSERT INTO {source} ({column_list}) "
            f"SELECT {column_list} FROM {backup}"
        )
        op.drop_table(backup)
    finally:
        connection.exec_driver_sql("PRAGMA legacy_alter_table=OFF")


def upgrade() -> None:
    op.add_column(
        "research_graph_state", _counter("formal_plan_content_acceptance_count")
    )
    op.add_column("agent_runtime_state", _counter("bundle_report_count"))
    op.add_column(
        "agent_runtime_state", _counter("bundle_replan_retirement_count")
    )
    op.add_column(
        "advancement_engine_state", _counter("bundle_report_disposition_count")
    )
    op.add_column(
        "advancement_engine_state", _counter("bundle_replan_activation_count")
    )

    # This receipt is intentionally distinct from both the RM content_ref
    # receipt and the RG formal_plan_ref decision receipt.  Its subject is the
    # exact canonical PlanDocument hash required by the fixed prototype.
    op.create_table(
        "rg_formal_plan_content_acceptances",
        sa.Column("acceptance_ref", sa.String(96), primary_key=True),
        sa.Column("formal_plan_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("decision_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("request_ref", sa.String(64), nullable=False),
        sa.Column("submission_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("plan_content_ref", sa.String(64), nullable=False, unique=True),
        sa.Column("plan_document_hash", sa.String(64), nullable=False),
        sa.Column("plan_content_receipt_ref", sa.String(96), nullable=False),
        sa.Column("plan_content_receipt_hash", sa.String(64), nullable=False),
        sa.Column("formal_plan_receipt_ref", sa.String(96), nullable=False),
        sa.Column("formal_plan_receipt_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["decision_ref"], ["rg_formal_plan_decisions.decision_ref"]
        ),
        sa.ForeignKeyConstraint(
            ["plan_content_ref"], ["rm_plan_documents.content_ref"]
        ),
        *(
            _hash(name)
            for name in (
                "plan_document_hash",
                "plan_content_receipt_hash",
                "formal_plan_receipt_hash",
                "request_hash",
                "receipt_hash",
            )
        ),
    )

    op.create_table(
        "ar_bundle_reports",
        sa.Column("report_ref", sa.String(96), primary_key=True),
        sa.Column("run_ref", sa.String(96), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("request_ref", sa.String(64), nullable=False),
        sa.Column("attempt_ref", sa.String(96), nullable=False),
        sa.Column("fence_ref", sa.String(96), nullable=False),
        sa.Column("formal_plan_ref", sa.String(96), nullable=False),
        sa.Column("plan_document_hash", sa.String(64), nullable=False),
        sa.Column("formal_plan_content_receipt_ref", sa.String(96), nullable=False),
        sa.Column("formal_plan_content_receipt_hash", sa.String(64), nullable=False),
        sa.Column("target_graph_ref", sa.String(96), nullable=False),
        sa.Column("target_graph_generation", sa.Integer(), nullable=False),
        sa.Column("target_set_hash", sa.String(64), nullable=False),
        sa.Column("coverage_hash", sa.String(64), nullable=False),
        sa.Column("target_graph_receipt_ref", sa.String(96), nullable=False),
        sa.Column("target_graph_receipt_hash", sa.String(64), nullable=False),
        sa.Column("target_refs_json", sa.Text(), nullable=False),
        sa.Column("target_refs_hash", sa.String(64), nullable=False),
        sa.Column("notice_refs_json", sa.Text(), nullable=False),
        sa.Column("notice_refs_hash", sa.String(64), nullable=False),
        sa.Column("handoff_manifest_refs_json", sa.Text(), nullable=False),
        sa.Column("handoff_manifest_refs_hash", sa.String(64), nullable=False),
        sa.Column("target_commit_receipts_json", sa.Text(), nullable=False),
        sa.Column("target_commit_receipts_hash", sa.String(64), nullable=False),
        sa.Column("report_json", sa.Text(), nullable=False),
        sa.Column("report_hash", sa.String(64), nullable=False),
        sa.Column("disposition", sa.String(24), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["run_ref"], ["ar_stage_runs.run_ref"]),
        sa.ForeignKeyConstraint(["attempt_ref"], ["ar_stage_attempts.attempt_ref"]),
        sa.ForeignKeyConstraint(["fence_ref"], ["ar_execution_fences.fence_ref"]),
        sa.ForeignKeyConstraint(
            ["formal_plan_content_receipt_ref"],
            ["rg_formal_plan_content_acceptances.receipt_ref"],
        ),
        sa.ForeignKeyConstraint(["target_graph_ref"], ["rg_target_graphs.graph_ref"]),
        sa.UniqueConstraint("run_ref", "ordinal"),
        sa.UniqueConstraint("run_ref", "report_hash"),
        sa.CheckConstraint("ordinal >= 1"),
        sa.CheckConstraint("target_graph_generation >= 0"),
        sa.CheckConstraint(
            "disposition IN ('realized', 'blocked', 'replan_required')"
        ),
        *(
            _hash(name)
            for name in (
                "plan_document_hash",
                "formal_plan_content_receipt_hash",
                "target_set_hash",
                "coverage_hash",
                "target_graph_receipt_hash",
                "target_refs_hash",
                "notice_refs_hash",
                "handoff_manifest_refs_hash",
                "target_commit_receipts_hash",
                "report_hash",
                "request_hash",
                "receipt_hash",
            )
        ),
    )
    op.create_index(
        "ix_ar_bundle_reports_run", "ar_bundle_reports", ["run_ref", "ordinal"]
    )

    # A blocked/replan report is durable AE input, but never a StageCommit and
    # never advances the foreground Stage.
    op.create_table(
        "ae_bundle_report_dispositions",
        sa.Column("disposition_ref", sa.String(96), primary_key=True),
        sa.Column("request_ref", sa.String(64), nullable=False),
        sa.Column("cycle_ref", sa.String(64), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("run_ref", sa.String(96), nullable=False),
        sa.Column("report_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("report_hash", sa.String(64), nullable=False),
        sa.Column("disposition", sa.String(24), nullable=False),
        sa.Column("report_receipt_ref", sa.String(96), nullable=False),
        sa.Column("report_receipt_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("recorded_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["request_ref"], ["ae_stage_run_requests.request_ref"]
        ),
        sa.ForeignKeyConstraint(["run_ref"], ["ar_stage_runs.run_ref"]),
        sa.ForeignKeyConstraint(["report_ref"], ["ar_bundle_reports.report_ref"]),
        sa.CheckConstraint("epoch >= 1"),
        sa.CheckConstraint("disposition IN ('blocked', 'replan_required')"),
        *(
            _hash(name)
            for name in (
                "report_hash",
                "report_receipt_hash",
                "request_hash",
                "receipt_hash",
            )
        ),
    )

    # Replan is a three-Owner-boundary handoff.  AE first records the typed
    # disposition, AR then retires the exact old Run/Attempt/Fence, and only a
    # second AE receipt may activate Plan at epoch+1.  These two immutable facts
    # make every crash point restart-reconcilable without AE writing AR state.
    op.create_table(
        "ar_bundle_replan_retirements",
        sa.Column("retirement_ref", sa.String(96), primary_key=True),
        sa.Column("disposition_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("request_ref", sa.String(64), nullable=False),
        sa.Column("run_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("attempt_ref", sa.String(96), nullable=False),
        sa.Column("fence_ref", sa.String(96), nullable=False),
        sa.Column("run_identity_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("report_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("report_hash", sa.String(64), nullable=False),
        sa.Column("disposition_receipt_ref", sa.String(96), nullable=False),
        sa.Column("disposition_receipt_hash", sa.String(64), nullable=False),
        sa.Column("control_operation_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("control_receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("control_receipt_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("retired_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["disposition_ref"], ["ae_bundle_report_dispositions.disposition_ref"]
        ),
        sa.ForeignKeyConstraint(
            ["request_ref"], ["ae_stage_run_requests.request_ref"]
        ),
        sa.ForeignKeyConstraint(["run_ref"], ["ar_stage_runs.run_ref"]),
        sa.ForeignKeyConstraint(["attempt_ref"], ["ar_stage_attempts.attempt_ref"]),
        sa.ForeignKeyConstraint(["fence_ref"], ["ar_execution_fences.fence_ref"]),
        sa.ForeignKeyConstraint(["report_ref"], ["ar_bundle_reports.report_ref"]),
        sa.ForeignKeyConstraint(
            ["control_operation_ref"], ["ar_control_operations.operation_ref"]
        ),
        *(
            _hash(name)
            for name in (
                "run_identity_hash",
                "report_hash",
                "disposition_receipt_hash",
                "control_receipt_hash",
                "request_hash",
                "receipt_hash",
            )
        ),
    )

    op.create_table(
        "ae_bundle_replan_activations",
        sa.Column("activation_ref", sa.String(96), primary_key=True),
        sa.Column("disposition_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("retirement_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("request_ref", sa.String(64), nullable=False),
        sa.Column("cycle_ref", sa.String(64), nullable=False),
        sa.Column("source_epoch", sa.Integer(), nullable=False),
        sa.Column("next_epoch", sa.Integer(), nullable=False),
        sa.Column("run_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("report_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("run_identity_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("retirement_receipt_ref", sa.String(96), nullable=False),
        sa.Column("retirement_receipt_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("activated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["disposition_ref"], ["ae_bundle_report_dispositions.disposition_ref"]
        ),
        sa.ForeignKeyConstraint(
            ["retirement_ref"], ["ar_bundle_replan_retirements.retirement_ref"]
        ),
        sa.ForeignKeyConstraint(
            ["request_ref"], ["ae_stage_run_requests.request_ref"]
        ),
        *(
            _hash(name)
            for name in (
                "run_identity_hash",
                "retirement_receipt_hash",
                "request_hash",
                "receipt_hash",
            )
        ),
        sa.CheckConstraint("source_epoch >= 1"),
        sa.CheckConstraint("next_epoch = source_epoch + 1"),
    )

    _replace_stage_commits()


def downgrade() -> None:
    raise RuntimeError("vNext production migrations are forward-only")
