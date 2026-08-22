"""Add durable first-question DeepFetch, Typed Runs, and LiteratureSnapshot.

Revision ID: 0007_first_question_deepfetch
Revises: 0006_research_asset_recovery
Create Date: 2026-08-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0007_first_question_deepfetch"
down_revision = "0006_research_asset_recovery"
branch_labels = None
depends_on = None


def _counter(name: str) -> sa.Column:
    return sa.Column(
        name,
        sa.Integer(),
        nullable=False,
        server_default="0",
    )


def _hash(name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"length({name}) = 64")


def upgrade() -> None:
    op.add_column("agent_runtime_state", _counter("deepfetch_run_count"))
    op.add_column("agent_runtime_state", _counter("deepfetch_completed_run_count"))
    op.add_column("agent_runtime_state", _counter("deepfetch_attempt_count"))
    op.add_column("agent_runtime_state", _counter("deepfetch_session_count"))
    op.add_column("research_memory_state", _counter("literature_snapshot_count"))
    op.add_column(
        "hc_proposal_generation_attempts",
        sa.Column("literature_snapshot_ref", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "hc_question_proposals",
        sa.Column("literature_snapshot_ref", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "hc_question_proposals",
        sa.Column("literature_snapshot_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "hc_question_proposals",
        sa.Column("binding_schema_ref", sa.String(length=80), nullable=True),
    )
    op.add_column(
        "hc_confirmation_preview_bindings",
        sa.Column("literature_snapshot_ref", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "hc_confirmation_preview_bindings",
        sa.Column("literature_snapshot_hash", sa.String(length=64), nullable=True),
    )

    op.create_table(
        "hc_deepfetch_requests",
        sa.Column("request_ref", sa.String(length=64), primary_key=True),
        sa.Column("initialization_id", sa.String(length=64), nullable=False),
        sa.Column("correlation_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("draft_revision", sa.Integer(), nullable=False),
        sa.Column("draft_hash", sa.String(length=64), nullable=False),
        sa.Column("scope_json", sa.Text(), nullable=False),
        sa.Column("scope_hash", sa.String(length=64), nullable=False),
        sa.Column("material_bindings_json", sa.Text(), nullable=False),
        sa.Column("material_bindings_hash", sa.String(length=64), nullable=False),
        sa.Column("resource_envelope_ref", sa.String(length=64), nullable=False),
        sa.Column("resource_envelope_hash", sa.String(length=64), nullable=False),
        sa.Column("result_route", sa.String(length=48), nullable=False),
        sa.Column(
            "authorization_receipt_ref",
            sa.String(length=64),
            nullable=False,
            unique=True,
        ),
        sa.Column("authorization_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("run_ref", sa.String(length=64), nullable=True),
        sa.Column("snapshot_ref", sa.String(length=64), nullable=True),
        sa.Column("failure_code", sa.String(length=96), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("completed_at", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(
            ["initialization_id"],
            ["hc_quest_initializations.initialization_id"],
        ),
        sa.ForeignKeyConstraint(
            ["initialization_id", "draft_revision"],
            [
                "hc_quest_draft_revisions.initialization_id",
                "hc_quest_draft_revisions.revision",
            ],
        ),
        sa.UniqueConstraint(
            "initialization_id",
            "draft_revision",
            "draft_hash",
            name="uq_hc_deepfetch_request_basis",
        ),
        sa.CheckConstraint("draft_revision >= 1"),
        _hash("draft_hash"),
        _hash("scope_hash"),
        _hash("material_bindings_hash"),
        _hash("resource_envelope_hash"),
        _hash("authorization_hash"),
        sa.CheckConstraint("result_route = 'same_quest_initialization_proposal'"),
        sa.CheckConstraint("status IN ('queued', 'succeeded', 'failed', 'cancelled')"),
        sa.CheckConstraint(
            "(status = 'queued' AND snapshot_ref IS NULL AND failure_code IS NULL "
            "AND completed_at IS NULL) OR "
            "(status = 'succeeded' AND run_ref IS NOT NULL AND snapshot_ref IS NOT NULL "
            "AND failure_code IS NULL AND completed_at IS NOT NULL) OR "
            "(status IN ('failed', 'cancelled') AND snapshot_ref IS NULL "
            "AND failure_code IS NOT NULL AND completed_at IS NOT NULL)"
        ),
    )
    op.create_index(
        "ix_hc_deepfetch_requests_queue",
        "hc_deepfetch_requests",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_hc_deepfetch_requests_initialization",
        "hc_deepfetch_requests",
        ["initialization_id", "draft_revision"],
    )

    op.create_table(
        "ar_deepfetch_runs",
        sa.Column("run_ref", sa.String(length=64), primary_key=True),
        sa.Column("request_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("correlation_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("runtime_binding_json", sa.Text(), nullable=False),
        sa.Column("runtime_binding_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("current_attempt_ref", sa.String(length=64), nullable=True),
        sa.Column("attempt_generation", sa.Integer(), nullable=False),
        sa.Column(
            "provider_operation_ref", sa.String(length=128), nullable=False, unique=True
        ),
        sa.Column("provider_operation_generation", sa.Integer(), nullable=False),
        sa.Column(
            "provider_operation_retry_permitted",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "reconciliation_attempt_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("next_reconcile_at", sa.Float(), nullable=True),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("result_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "execution_receipt_ref", sa.String(length=64), nullable=True, unique=True
        ),
        sa.Column("execution_receipt_hash", sa.String(length=64), nullable=True),
        sa.Column("failure_code", sa.String(length=96), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("completed_at", sa.Float(), nullable=True),
        _hash("request_hash"),
        _hash("runtime_binding_hash"),
        sa.CheckConstraint("attempt_generation >= 0"),
        sa.CheckConstraint("provider_operation_generation >= 1"),
        sa.CheckConstraint("provider_operation_retry_permitted IN (0, 1)"),
        sa.CheckConstraint("reconciliation_attempt_count >= 0"),
        sa.CheckConstraint(
            "provider_operation_retry_permitted = 0 OR status = 'failed'"
        ),
        sa.CheckConstraint(
            "next_reconcile_at IS NULL OR "
            "(status = 'admitted' AND reconciliation_attempt_count > 0)"
        ),
        sa.CheckConstraint(
            "status IN ('admitted', 'running', 'executed', 'failed', 'cancelled')"
        ),
        sa.CheckConstraint(
            "(result_json IS NULL AND result_hash IS NULL) OR "
            "(result_json IS NOT NULL AND result_hash IS NOT NULL "
            "AND length(result_hash) = 64)"
        ),
        sa.CheckConstraint(
            "(execution_receipt_ref IS NULL AND execution_receipt_hash IS NULL) OR "
            "(execution_receipt_ref IS NOT NULL AND execution_receipt_hash IS NOT NULL "
            "AND length(execution_receipt_hash) = 64)"
        ),
        sa.CheckConstraint(
            "(status IN ('admitted', 'running') AND result_json IS NULL "
            "AND execution_receipt_ref IS NULL AND completed_at IS NULL) OR "
            "(status = 'executed' AND result_json IS NOT NULL "
            "AND execution_receipt_ref IS NOT NULL AND failure_code IS NULL "
            "AND completed_at IS NOT NULL) OR "
            "(status IN ('failed', 'cancelled') AND result_json IS NULL "
            "AND execution_receipt_ref IS NULL AND failure_code IS NOT NULL "
            "AND completed_at IS NOT NULL)"
        ),
    )
    op.create_index(
        "ix_ar_deepfetch_runs_status",
        "ar_deepfetch_runs",
        ["status", "updated_at"],
    )
    op.create_table(
        "ar_deepfetch_sessions",
        sa.Column("root_session_ref", sa.String(length=64), primary_key=True),
        sa.Column("run_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("native_session_ref", sa.String(length=512), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["run_ref"], ["ar_deepfetch_runs.run_ref"]),
        sa.CheckConstraint("status IN ('open', 'completed', 'cancelled')"),
    )
    op.create_table(
        "ar_deepfetch_attempts",
        sa.Column("attempt_ref", sa.String(length=64), primary_key=True),
        sa.Column("run_ref", sa.String(length=64), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("root_session_ref", sa.String(length=64), nullable=False),
        sa.Column("fence_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("result_hash", sa.String(length=64), nullable=True),
        sa.Column("failure_code", sa.String(length=96), nullable=True),
        sa.Column("started_at", sa.Float(), nullable=False),
        sa.Column("completed_at", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["run_ref"], ["ar_deepfetch_runs.run_ref"]),
        sa.ForeignKeyConstraint(
            ["root_session_ref"], ["ar_deepfetch_sessions.root_session_ref"]
        ),
        sa.UniqueConstraint("run_ref", "generation"),
        sa.CheckConstraint("generation >= 1"),
        sa.CheckConstraint(
            "status IN ('running', 'executed', 'failed', 'superseded', 'cancelled')"
        ),
        sa.CheckConstraint(
            "(status = 'running' AND result_hash IS NULL AND failure_code IS NULL "
            "AND completed_at IS NULL) OR "
            "(status = 'executed' AND result_hash IS NOT NULL "
            "AND length(result_hash) = 64 AND failure_code IS NULL "
            "AND completed_at IS NOT NULL) OR "
            "(status IN ('failed', 'superseded', 'cancelled') "
            "AND result_hash IS NULL AND failure_code IS NOT NULL "
            "AND completed_at IS NOT NULL)"
        ),
    )
    op.create_index(
        "ix_ar_deepfetch_attempts_run",
        "ar_deepfetch_attempts",
        ["run_ref", "generation"],
    )

    op.create_table(
        "rm_literature_snapshots",
        sa.Column("snapshot_ref", sa.String(length=64), primary_key=True),
        sa.Column("request_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("initialization_id", sa.String(length=64), nullable=False),
        sa.Column("draft_revision", sa.Integer(), nullable=False),
        sa.Column("draft_hash", sa.String(length=64), nullable=False),
        sa.Column("scope_hash", sa.String(length=64), nullable=False),
        sa.Column("run_ref", sa.String(length=64), nullable=False),
        sa.Column("attempt_ref", sa.String(length=64), nullable=False),
        sa.Column("fence_ref", sa.String(length=64), nullable=False),
        sa.Column("result_hash", sa.String(length=64), nullable=False),
        sa.Column("execution_receipt_ref", sa.String(length=64), nullable=False),
        sa.Column("execution_receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("completion", sa.String(length=24), nullable=False),
        sa.Column("summary_ref", sa.String(length=64), nullable=False),
        sa.Column("summary_hash", sa.String(length=64), nullable=False),
        sa.Column("summary_object_path", sa.Text(), nullable=False),
        sa.Column("papers_ref", sa.String(length=64), nullable=False),
        sa.Column("papers_hash", sa.String(length=64), nullable=False),
        sa.Column("papers_object_path", sa.Text(), nullable=False),
        sa.Column("fulltexts_ref", sa.String(length=64), nullable=False),
        sa.Column("fulltexts_hash", sa.String(length=64), nullable=False),
        sa.Column("fulltexts_object_path", sa.Text(), nullable=False),
        sa.Column("limitations_json", sa.Text(), nullable=False),
        sa.Column("limitations_hash", sa.String(length=64), nullable=False),
        sa.Column("web_evidence_json", sa.Text(), nullable=False),
        sa.Column("web_evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("receipt_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.UniqueConstraint(
            "initialization_id",
            "draft_revision",
            "draft_hash",
            name="uq_rm_literature_snapshot_basis",
        ),
        sa.CheckConstraint("draft_revision >= 1"),
        _hash("draft_hash"),
        _hash("scope_hash"),
        _hash("result_hash"),
        _hash("execution_receipt_hash"),
        _hash("summary_hash"),
        _hash("papers_hash"),
        _hash("fulltexts_hash"),
        _hash("limitations_hash"),
        _hash("web_evidence_hash"),
        _hash("snapshot_hash"),
        _hash("receipt_hash"),
        sa.CheckConstraint("completion IN ('complete', 'limited', 'honest_empty')"),
    )
    op.create_index(
        "ix_rm_literature_snapshots_initialization",
        "rm_literature_snapshots",
        ["initialization_id", "draft_revision"],
    )


def downgrade() -> None:
    raise RuntimeError("vNext production migrations are forward-only")
