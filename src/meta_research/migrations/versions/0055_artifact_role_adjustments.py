"""Record lightweight corrections of current artifact attribution.

Revision ID: 0055_artifact_role_adjustments
Revises: 0054_variant_run_implementation
Create Date: 2026-09-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0055_artifact_role_adjustments"
down_revision = "0054_variant_run_implementation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rg_experiment_asset_role_adjustments",
        sa.Column("adjustment_ref", sa.Text(), primary_key=True),
        sa.Column("role_ref", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
        sa.Column("from_subject_kind", sa.Text(), nullable=False),
        sa.Column("from_subject_ref", sa.Text(), nullable=False),
        sa.Column("to_subject_kind", sa.Text(), nullable=False),
        sa.Column("to_subject_ref", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.Text(), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.Column("receipt_ref", sa.Text(), nullable=False),
        sa.Column("receipt_hash", sa.Text(), nullable=False),
    )
    op.create_index(
        "ix_rg_asset_role_adjustments_role_ref",
        "rg_experiment_asset_role_adjustments",
        ["role_ref"],
    )


def downgrade() -> None:
    raise RuntimeError("production migrations are forward-only")
