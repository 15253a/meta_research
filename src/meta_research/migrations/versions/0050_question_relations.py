"""Keep related Questions and their Reasoning source as simple research facts."""
import sqlalchemy as sa
from alembic import op

revision = "0050_question_relations"
down_revision = "0049_research_note_assets"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("rg_question_relations",
        sa.Column("relation_ref", sa.String(96), primary_key=True),
        sa.Column("quest_ref", sa.String(96), nullable=False),
        sa.Column("left_question_ref", sa.String(96), nullable=False),
        sa.Column("right_question_ref", sa.String(96), nullable=False),
        sa.Column("source_run_ref", sa.String(96), nullable=False),
        sa.Column("source_request_ref", sa.String(96), nullable=False),
        sa.Column("effect_id", sa.String(128), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("source_run_ref", "effect_id"),
        sa.CheckConstraint("left_question_ref < right_question_ref"),
        sa.CheckConstraint("length(content_hash) = 64"),
        sa.ForeignKeyConstraint(["quest_ref"], ["rg_quests.quest_ref"]),
        sa.ForeignKeyConstraint(["left_question_ref"], ["rg_question_lifecycle.question_ref"]),
        sa.ForeignKeyConstraint(["right_question_ref"], ["rg_question_lifecycle.question_ref"]),
        sa.ForeignKeyConstraint(["source_run_ref"], ["ar_stage_runs.run_ref"]),
        sa.ForeignKeyConstraint(["source_request_ref"], ["ae_stage_run_requests.request_ref"]))
    op.create_index("ix_rg_question_relations_quest", "rg_question_relations",
                    ["quest_ref", "created_at", "relation_ref"])


def downgrade():
    raise RuntimeError("production migrations are forward-only")
