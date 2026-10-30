"""Freeze the actual Cycle focus and its source at activation."""
from alembic import op
import sqlalchemy as sa

revision = "0075_cycle_position"
down_revision = "0074_guidance_confirmation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ae_cycles", sa.Column("cycle_position_json", sa.Text(), nullable=True))


def downgrade() -> None:
    raise RuntimeError("production migrations are forward-only")
