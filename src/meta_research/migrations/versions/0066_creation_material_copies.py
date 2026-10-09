from alembic import op
import sqlalchemy as sa

revision = "0066_creation_material_copies"
down_revision = "0065_creation_material_operations"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("hc_creation_material_copies",
        sa.Column("effect_key", sa.String(96), primary_key=True),
        sa.Column("operation_ref", sa.String(160), nullable=False),
        sa.Column("fence_ref", sa.String(96), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("receipt_json", sa.Text, nullable=True),
        sa.CheckConstraint("state IN ('pending','ready','failed')"))


def downgrade():
    raise RuntimeError("vNext production migrations are forward-only")
