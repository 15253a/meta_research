"""Keep the human Writing request alongside its complete model input."""
from alembic import op
import sqlalchemy as sa

revision = "0080_writing_display_message"
down_revision = "0079_writing_task_model"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("hc_companion_turns", sa.Column("display_message", sa.Text(), nullable=True))


def downgrade():
    raise RuntimeError("production migrations are forward-only")
