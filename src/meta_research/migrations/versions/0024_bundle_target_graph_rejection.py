"""Add the Research Graph TargetPlan rejection ledger.

Revision ID: 0024_bundle_target_graph_rejection
Revises: 0023_bundle_exhaustion_proposal
Create Date: 2026-08-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0024_bundle_target_graph_rejection"
down_revision = "0023_bundle_exhaustion_proposal"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "research_graph_state",
        sa.Column(
            "target_graph_rejection_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_table(
        "rg_target_graph_rejections",
        sa.Column("rejection_ref", sa.String(96), primary_key=True),
        sa.Column("request_ref", sa.String(64), nullable=False),
        sa.Column("run_ref", sa.String(96), nullable=False),
        sa.Column("attempt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("fence_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("submission_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("context_pack_ref", sa.String(96), nullable=False),
        sa.Column("context_pack_hash", sa.String(64), nullable=False),
        sa.Column("formal_plan_ref", sa.String(96), nullable=False),
        sa.Column("plan_document_hash", sa.String(64), nullable=False),
        sa.Column("target_plan_json", sa.Text(), nullable=False),
        sa.Column("target_plan_hash", sa.String(64), nullable=False),
        sa.Column("execution_payload_hash", sa.String(64), nullable=False),
        sa.Column("execution_receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("execution_receipt_hash", sa.String(64), nullable=False),
        sa.Column("reason_code", sa.String(96), nullable=False),
        sa.Column("feedback_json", sa.Text(), nullable=False),
        sa.Column("feedback_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("rejected_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["request_ref"], ["ae_stage_run_requests.request_ref"]
        ),
        sa.ForeignKeyConstraint(["run_ref"], ["ar_stage_runs.run_ref"]),
        sa.ForeignKeyConstraint(["attempt_ref"], ["ar_stage_attempts.attempt_ref"]),
        sa.ForeignKeyConstraint(["fence_ref"], ["ar_execution_fences.fence_ref"]),
        sa.CheckConstraint("length(context_pack_hash) = 64"),
        sa.CheckConstraint("length(plan_document_hash) = 64"),
        sa.CheckConstraint("length(target_plan_hash) = 64"),
        sa.CheckConstraint("length(execution_payload_hash) = 64"),
        sa.CheckConstraint("length(execution_receipt_hash) = 64"),
        sa.CheckConstraint("length(feedback_hash) = 64"),
        sa.CheckConstraint("length(receipt_hash) = 64"),
        sa.CheckConstraint(
            "reason_code IN ('target_candidate_owner_proof_unverified')"
        ),
    )
    op.create_index(
        "ix_rg_target_graph_rejections_request_ref",
        "rg_target_graph_rejections",
        ["request_ref"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_rg_target_graph_rejections_request_ref",
        table_name="rg_target_graph_rejections",
    )
    op.drop_table("rg_target_graph_rejections")
    op.drop_column("research_graph_state", "target_graph_rejection_count")
