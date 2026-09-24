"""Index reusable environments and their existing research/content sources."""
from alembic import op
import sqlalchemy as sa

revision = "0059_research_environments"
down_revision = "0058_human_research_inputs"
branch_labels = None
depends_on = None


def _fact_columns():
    return [
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
    ]


def upgrade():
    op.create_table("rg_environments",
        sa.Column("environment_ref", sa.String(96), primary_key=True),
        sa.Column("semantic_key", sa.Text(), nullable=False),
        sa.Column("origin_quest_ref", sa.Text(), sa.ForeignKey("rg_quests.quest_ref")),
        *_fact_columns())
    op.create_index("ix_rg_environments_semantic_key", "rg_environments", ["semantic_key"])
    op.create_table("rg_environment_assets",
        sa.Column("environment_ref", sa.String(96), sa.ForeignKey("rg_environments.environment_ref"), primary_key=True),
        sa.Column("asset_version_ref", sa.String(96), sa.ForeignKey("rm_asset_versions.version_ref"), primary_key=True))
    op.create_index("ix_rg_environment_assets_version", "rg_environment_assets", ["asset_version_ref"])
    op.create_table("rg_environment_references",
        sa.Column("environment_reference_ref", sa.String(96), primary_key=True),
        sa.Column("environment_ref", sa.String(96), sa.ForeignKey("rg_environments.environment_ref"), nullable=False),
        sa.Column("question_ref", sa.String(96), sa.ForeignKey("rg_question_lifecycle.question_ref"), nullable=False),
        *_fact_columns())
    op.create_index("ix_rg_environment_references_question", "rg_environment_references", ["question_ref"])
    op.create_index("ix_rg_environment_references_environment", "rg_environment_references", ["environment_ref"])
    op.create_table("rg_environment_commands",
        sa.Column("idempotency_key", sa.String(128), primary_key=True),
        sa.Column("operation", sa.String(32), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("result_ref", sa.String(96), nullable=False),
        sa.Column("recorded_at", sa.Float(), nullable=False))


def downgrade():
    raise RuntimeError("production migrations are forward-only")
