from alembic import op
import sqlalchemy as sa

revision = "0065_creation_material_operations"
down_revision = "0064_work_materials"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("hc_creation_material_operations",
        sa.Column("operation_ref", sa.String(160), primary_key=True),
        sa.Column("fence_ref", sa.String(96), nullable=False),
        sa.Column("binding_hash", sa.String(64), nullable=False),
        sa.Column("workspace_ref", sa.String(96), nullable=False),
        sa.Column("root_session_ref", sa.String(96), nullable=False),
        sa.Column("anchor_json", sa.Text, nullable=False),
        sa.Column("material_set_json", sa.Text, nullable=False),
        sa.Column("material_set_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("identity_json", sa.Text, nullable=True),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.CheckConstraint("state IN ('active','sealed','failed','unknown_outcome')"))


def downgrade():
    raise RuntimeError("vNext production migrations are forward-only")
