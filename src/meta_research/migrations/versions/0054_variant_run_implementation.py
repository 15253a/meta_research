"""Carry each VariantRun's implementation revision ref on its own row.

Revision ID: 0054_variant_run_implementation
Revises: 0053_drop_reuse_eligibilities
Create Date: 2026-09-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0054_variant_run_implementation"
down_revision = "0053_drop_reuse_eligibilities"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Fresh databases have no rows to carry over; new runs write the column.
    op.add_column(
        "rg_variant_runs",
        sa.Column("implementation_revision_ref", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    raise RuntimeError("production migrations are forward-only")
