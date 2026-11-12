"""Retain exact prior identities when a stopped Bundle adopts new guidance."""
from alembic import op
import sqlalchemy as sa

revision = "0086_bundle_runtime_refresh"
down_revision = "0085_stable_target_workspaces"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("ar_bundle_runtime_refreshes",
        sa.Column("refresh_ref", sa.Text(), primary_key=True),
        sa.Column("run_ref", sa.Text(), nullable=False),
        sa.Column("retired_attempt_ref", sa.Text(), nullable=False, unique=True),
        sa.Column("replacement_attempt_ref", sa.Text(), nullable=False, unique=True),
        sa.Column("retired_generation", sa.Integer(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
    )


def downgrade():
    raise RuntimeError("production migrations are forward-only")
