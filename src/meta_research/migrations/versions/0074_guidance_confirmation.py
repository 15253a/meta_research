"""Bind the human's selected guidance options to each Companion turn."""

from alembic import op
import sqlalchemy as sa

revision = "0074_guidance_confirmation"
down_revision = "0073_merge_search_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("hc_companion_turns", sa.Column("guidance_options_json", sa.Text(), nullable=True))
    op.add_column("hc_companion_turns", sa.Column("guidance_options_hash", sa.String(64), nullable=True))


def downgrade() -> None:
    raise RuntimeError("production migrations are forward-only")
