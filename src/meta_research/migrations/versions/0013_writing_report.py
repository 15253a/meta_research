"""Add independent report Writing Run admission state.

Revision ID: 0013_writing_report
Revises: 0012_experiment_measurement
Create Date: 2026-08-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0013_writing_report"
down_revision = "0012_experiment_measurement"
branch_labels = None
depends_on = None


def _hash(name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"length({name}) = 64")


def upgrade() -> None:
    for name in (
        "writing_run_count",
        "writing_attempt_count",
        "writing_session_count",
        "active_writing_run_count",
    ):
        op.add_column(
            "agent_runtime_state",
            sa.Column(name, sa.Integer(), nullable=False, server_default="0"),
        )
    for name in (
        "writing_citation_decision_count",
        "writing_citation_rejection_count",
    ):
        op.add_column(
            "research_graph_state",
            sa.Column(name, sa.Integer(), nullable=False, server_default="0"),
        )

    op.create_table(
        "ar_writing_runs",
        sa.Column("run_ref", sa.String(length=96), primary_key=True),
        sa.Column("intent_id", sa.String(length=96), nullable=False, unique=True),
        sa.Column("quest_ref", sa.String(length=96), nullable=False),
        sa.Column("document_type", sa.String(length=24), nullable=False),
        sa.Column("intent_json", sa.Text(), nullable=False),
        sa.Column("intent_hash", sa.String(length=64), nullable=False),
        sa.Column("snapshot_ref", sa.String(length=96), nullable=False),
        sa.Column("snapshot_json", sa.Text(), nullable=False),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("confirmation_ref", sa.String(length=96), nullable=False, unique=True),
        sa.Column("confirmation_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("failure_code", sa.String(length=128), nullable=True),
        sa.Column("execution_budget_json", sa.Text(), nullable=False),
        sa.Column("execution_budget_hash", sa.String(length=64), nullable=False),
        sa.Column("output_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempt_ref", sa.String(length=96), nullable=False, unique=True),
        sa.Column("attempt_generation", sa.Integer(), nullable=False),
        sa.Column("root_session_ref", sa.String(length=96), nullable=False, unique=True),
        sa.Column("native_session_ref", sa.String(length=96), nullable=True, unique=True),
        sa.Column("fence_ref", sa.String(length=96), nullable=False, unique=True),
        sa.Column("predecessor_version_ref", sa.String(length=96), nullable=True),
        sa.Column("feedback_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column(
            "feedback_hash",
            sa.String(length=64),
            nullable=False,
            server_default="4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
        ),
        sa.Column("runtime_binding_json", sa.Text(), nullable=False),
        sa.Column("runtime_binding_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["intent_id"], ["hc_command_intents.intent_id"]),
        sa.ForeignKeyConstraint(["quest_ref"], ["rg_quests.quest_ref"]),
        sa.CheckConstraint("document_type = 'report'"),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'blocked', 'completed', 'cancelled')"
        ),
        sa.CheckConstraint("attempt_generation >= 1"),
        sa.CheckConstraint("output_bytes >= 0"),
        _hash("intent_hash"),
        _hash("snapshot_hash"),
        _hash("confirmation_hash"),
        _hash("runtime_binding_hash"),
        _hash("feedback_hash"),
        _hash("execution_budget_hash"),
    )
    op.create_index(
        "ix_ar_writing_runs_status_updated",
        "ar_writing_runs",
        ["status", "updated_at", "run_ref"],
    )
    op.create_table(
        "ar_writing_attempts",
        sa.Column("attempt_ref", sa.String(length=96), primary_key=True),
        sa.Column("run_ref", sa.String(length=96), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("root_session_ref", sa.String(length=96), nullable=False),
        sa.Column("native_session_ref", sa.String(length=96), nullable=True),
        sa.Column("fence_ref", sa.String(length=96), nullable=False, unique=True),
        sa.Column("provider_job_ref", sa.String(length=96), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("failure_code", sa.String(length=128), nullable=True),
        sa.Column("predecessor_version_ref", sa.String(length=96), nullable=True),
        sa.Column("predecessor_markdown_hash", sa.String(length=64), nullable=True),
        sa.Column("feedback_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column(
            "feedback_hash",
            sa.String(length=64),
            nullable=False,
            server_default="4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
        ),
        sa.Column("decision_status", sa.String(length=16), nullable=True),
        sa.Column("decision_receipt_json", sa.Text(), nullable=True),
        sa.Column("decision_receipt_hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("started_at", sa.Float(), nullable=True),
        sa.Column("completed_at", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["run_ref"], ["ar_writing_runs.run_ref"]),
        sa.UniqueConstraint("run_ref", "generation"),
        sa.CheckConstraint("generation >= 1"),
        sa.CheckConstraint(
            "status IN ('admitted', 'running', 'paused', 'completed', 'failed', 'retired')"
        ),
        sa.CheckConstraint("decision_status IS NULL OR decision_status IN ('accepted', 'rejected')"),
        _hash("feedback_hash"),
        sa.CheckConstraint(
            "predecessor_markdown_hash IS NULL OR length(predecessor_markdown_hash) = 64"
        ),
        sa.CheckConstraint(
            "decision_receipt_hash IS NULL OR length(decision_receipt_hash) = 64"
        ),
    )
    op.create_table(
        "ar_writing_checkpoints",
        sa.Column("checkpoint_ref", sa.String(length=96), primary_key=True),
        sa.Column("run_ref", sa.String(length=96), nullable=False),
        sa.Column("attempt_ref", sa.String(length=96), nullable=False, unique=True),
        sa.Column("fence_ref", sa.String(length=96), nullable=False),
        sa.Column("native_session_ref", sa.String(length=96), nullable=False),
        sa.Column("markdown", sa.Text(), nullable=False),
        sa.Column("markdown_hash", sa.String(length=64), nullable=False),
        sa.Column("citations_json", sa.Text(), nullable=False),
        sa.Column("citations_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["run_ref"], ["ar_writing_runs.run_ref"]),
        sa.ForeignKeyConstraint(["attempt_ref"], ["ar_writing_attempts.attempt_ref"]),
        _hash("markdown_hash"),
        _hash("citations_hash"),
    )
    op.create_table(
        "ar_writing_executions",
        sa.Column("execution_ref", sa.String(length=96), primary_key=True),
        sa.Column("run_ref", sa.String(length=96), nullable=False),
        sa.Column("attempt_ref", sa.String(length=96), nullable=False, unique=True),
        sa.Column("fence_ref", sa.String(length=96), nullable=False),
        sa.Column("reviewed_markdown", sa.Text(), nullable=False),
        sa.Column("reviewed_markdown_hash", sa.String(length=64), nullable=False),
        sa.Column("final_markdown", sa.Text(), nullable=False),
        sa.Column("final_markdown_hash", sa.String(length=64), nullable=False),
        sa.Column("citations_json", sa.Text(), nullable=False),
        sa.Column("citations_hash", sa.String(length=64), nullable=False),
        sa.Column("review_json", sa.Text(), nullable=False),
        sa.Column("review_hash", sa.String(length=64), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("receipt_ref", sa.String(length=96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("runtime_binding_hash", sa.String(length=64), nullable=False),
        sa.Column("completed_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["run_ref"], ["ar_writing_runs.run_ref"]),
        sa.ForeignKeyConstraint(["attempt_ref"], ["ar_writing_attempts.attempt_ref"]),
        _hash("reviewed_markdown_hash"),
        _hash("final_markdown_hash"),
        _hash("citations_hash"),
        _hash("review_hash"),
        _hash("payload_hash"),
        _hash("receipt_hash"),
        _hash("runtime_binding_hash"),
    )
    op.create_table(
        "ar_writing_commands",
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("command_kind", sa.String(length=48), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("result_ref", sa.String(length=96), nullable=False),
        sa.Column("result_attempt_ref", sa.String(length=96), nullable=True),
        sa.Column("recorded_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["result_ref"], ["ar_writing_runs.run_ref"]),
        sa.ForeignKeyConstraint(
            ["result_attempt_ref"], ["ar_writing_attempts.attempt_ref"]
        ),
        sa.PrimaryKeyConstraint("command_kind", "idempotency_key"),
        _hash("request_hash"),
    )
    op.create_table(
        "rg_writing_citation_decisions",
        sa.Column("decision_ref", sa.String(length=96), primary_key=True),
        sa.Column("run_ref", sa.String(length=96), nullable=False),
        sa.Column("attempt_ref", sa.String(length=96), nullable=False),
        sa.Column("fence_ref", sa.String(length=96), nullable=False),
        sa.Column("quest_ref", sa.String(length=96), nullable=False),
        sa.Column("snapshot_ref", sa.String(length=96), nullable=False),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("allowed_sources_json", sa.Text(), nullable=False),
        sa.Column("allowed_sources_hash", sa.String(length=64), nullable=False),
        sa.Column("asset_ref", sa.String(length=96), nullable=False),
        sa.Column("version_ref", sa.String(length=96), nullable=False, unique=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("asset_receipt_ref", sa.String(length=96), nullable=False),
        sa.Column("asset_receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("citations_json", sa.Text(), nullable=False),
        sa.Column("citations_hash", sa.String(length=64), nullable=False),
        sa.Column("final_markdown_hash", sa.String(length=64), nullable=False),
        sa.Column("execution_ref", sa.String(length=96), nullable=False),
        sa.Column("execution_receipt_ref", sa.String(length=96), nullable=False),
        sa.Column("execution_receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("feedback_json", sa.Text(), nullable=False),
        sa.Column("feedback_hash", sa.String(length=64), nullable=False),
        sa.Column("decision_hash", sa.String(length=64), nullable=False),
        sa.Column("receipt_ref", sa.String(length=96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("decided_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["run_ref"], ["ar_writing_runs.run_ref"]),
        sa.ForeignKeyConstraint(["attempt_ref"], ["ar_writing_attempts.attempt_ref"]),
        sa.ForeignKeyConstraint(["quest_ref"], ["rg_quests.quest_ref"]),
        sa.ForeignKeyConstraint(["version_ref"], ["rm_asset_versions.version_ref"]),
        sa.UniqueConstraint("run_ref", "attempt_ref"),
        sa.CheckConstraint("decision IN ('accepted', 'rejected')"),
        _hash("snapshot_hash"),
        _hash("allowed_sources_hash"),
        _hash("content_hash"),
        _hash("manifest_hash"),
        _hash("asset_receipt_hash"),
        _hash("citations_hash"),
        _hash("final_markdown_hash"),
        _hash("execution_receipt_hash"),
        _hash("feedback_hash"),
        _hash("decision_hash"),
        _hash("receipt_hash"),
    )


def downgrade() -> None:
    raise RuntimeError("vNext production migrations are forward-only")
