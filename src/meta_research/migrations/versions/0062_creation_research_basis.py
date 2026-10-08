from alembic import op
import sqlalchemy as sa

revision = "0062_creation_research_basis"
down_revision = "0061_target_result_optional_disposition"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("rm_creation_bases",
        sa.Column("basis_ref", sa.String(64), primary_key=True),
        sa.Column("basis_hash", sa.String(64), nullable=False),
        sa.Column("initialization_id", sa.String(64), nullable=False),
        sa.Column("draft_revision", sa.Integer, nullable=False),
        sa.Column("draft_hash", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("body_json", sa.Text, nullable=False),
        sa.CheckConstraint("kind IN ('prepared','literature_revised')"))
    op.create_index("uq_prepared_creation_basis", "rm_creation_bases",
        ["initialization_id", "draft_revision", "draft_hash"], unique=True,
        sqlite_where=sa.text("kind='prepared'"))
    op.create_table("rm_question_creation_bases",
        sa.Column("question_ref", sa.String(64), primary_key=True),
        sa.Column("quest_ref", sa.String(64), nullable=False),
        sa.Column("basis_ref", sa.String(64), nullable=False),
        sa.Column("binding_json", sa.Text, nullable=False),
        sa.ForeignKeyConstraint(["basis_ref"], ["rm_creation_bases.basis_ref"]))
    for name in ("creation_basis_ref", "creation_basis_hash"):
        op.add_column("hc_question_proposals", sa.Column(name, sa.String(64), nullable=True))


def downgrade():
    raise RuntimeError("vNext production migrations are forward-only")
