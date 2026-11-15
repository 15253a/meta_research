"""Locate current-protocol resource selections without scanning other records."""
from alembic import op
import sqlalchemy as sa

revision = "0088_resource_selection_index"
down_revision = "0087_bundle_strategy_revision"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("rg_resource_selections",
        sa.Column("selection_ref", sa.String(96), primary_key=True),
        sa.Column("resource_kind", sa.String(24), primary_key=True),
        sa.Column("resource_ref", sa.String(96), primary_key=True),
        sa.CheckConstraint("resource_kind IN ('environment', 'dataset_version')"))


def downgrade():
    raise RuntimeError("production migrations are forward-only")
