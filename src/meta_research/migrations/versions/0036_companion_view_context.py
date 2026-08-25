"""Persist exact Companion view context on the durable agent turn.

Revision ID: 0036_companion_view_context
Revises: 0035_runtime_protection
Create Date: 2026-08-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0036_companion_view_context"
down_revision = "0035_runtime_protection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("hc_companion_turns") as batch:
        batch.add_column(sa.Column("view_context_json", sa.Text()))
        batch.add_column(sa.Column("view_context_hash", sa.String(64)))
        batch.create_check_constraint(
            "ck_hc_companion_turn_view_context_pair",
            "(view_context_json IS NULL AND view_context_hash IS NULL) OR "
            "(view_context_json IS NOT NULL AND length(view_context_hash) = 64)",
        )


def downgrade() -> None:
    with op.batch_alter_table("hc_companion_turns") as batch:
        batch.drop_constraint(
            "ck_hc_companion_turn_view_context_pair",
            type_="check",
        )
        batch.drop_column("view_context_hash")
        batch.drop_column("view_context_json")
