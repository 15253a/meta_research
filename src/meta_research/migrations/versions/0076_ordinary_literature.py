"""Ordinary literature index facts; content stays in existing RM assets."""
from alembic import op
import sqlalchemy as sa
revision = "0076_ordinary_literature"
down_revision = "0075_cycle_position"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("rg_literature_facts",
        sa.Column("ref", sa.String(96), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("quest_ref", sa.String(96), sa.ForeignKey("rg_quests.quest_ref"), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("kind", "payload_hash"))
    op.create_index("ix_rg_literature_quest", "rg_literature_facts", ["quest_ref", "kind"])
    op.create_table("rg_literature_commands",
        sa.Column("idempotency_key", sa.String(128), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("result_ref", sa.String(96), sa.ForeignKey("rg_literature_facts.ref"), nullable=False))


def downgrade():
    raise RuntimeError("production migrations are forward-only")
