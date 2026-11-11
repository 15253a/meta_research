from alembic import op
import sqlalchemy as sa

revision = "0083_reasoning_primary_source"
down_revision = "0082_stage_local_acceptance"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("ar_stage_attempts", sa.Column("primary_provider_unit_ref", sa.String(96), nullable=True))


def downgrade():
    raise RuntimeError("production migrations are forward-only")
