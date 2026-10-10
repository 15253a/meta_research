import sqlalchemy as sa
from alembic import op

revision = "0070_material_processing"
down_revision = "0069_merge_goal_creation"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("hc_material_processing_effects",
        sa.Column("effect_key", sa.String(96), primary_key=True),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("root_kind", sa.String(32), nullable=False),
        sa.Column("run_ref", sa.String(128), nullable=False),
        sa.Column("root_session_ref", sa.String(128), nullable=False),
        sa.Column("quest_ref", sa.String(128)),
        sa.Column("reference_ref", sa.String(96)),
        sa.Column("command_hash", sa.String(64), nullable=False),
        sa.Column("command_json", sa.Text(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("result_json", sa.Text()),
        sa.Column("created_at", sa.Float(), nullable=False))
    op.create_index("hc_material_treatment_reference", "hc_material_processing_effects", ["reference_ref", "action", "created_at"])
    op.create_index("hc_material_treatment_quest", "hc_material_processing_effects", ["quest_ref", "action", "created_at"])


def downgrade():
    raise RuntimeError("production migrations are forward-only")
