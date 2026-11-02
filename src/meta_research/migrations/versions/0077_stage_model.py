"""Freeze the model with the Target provider operation for reconciliation."""
from alembic import op
import sqlalchemy as sa

revision = "0077_stage_model"
down_revision = "0076_ordinary_literature"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("ar_harness_provider_operations", sa.Column("model_ref", sa.String(160)))


def downgrade():
    op.drop_column("ar_harness_provider_operations", "model_ref")
