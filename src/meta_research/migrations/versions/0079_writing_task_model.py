"""Bind ordinary Writing turns to their user's model selection."""
from alembic import op
import sqlalchemy as sa

revision = "0079_writing_task_model"
down_revision = "0078_target_host_intake_retry"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("hc_companion_turns", sa.Column("writing_model_json", sa.Text(), nullable=True))


def downgrade():
    raise RuntimeError("production migrations are forward-only")
