"""Link historical final explanations to existing immutable RM assets."""
import sqlalchemy as sa
from alembic import op

revision = "0049_research_note_assets"
down_revision = "0048_root_formal_entities"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("rm_target_research_notes",
        sa.Column("note_ref", sa.String(96), primary_key=True),
        sa.Column("target_ref", sa.String(96), nullable=False),
        sa.Column("completion_ref", sa.String(96), nullable=False),
        sa.Column("manifest_ref", sa.String(96), nullable=False),
        sa.Column("version_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("source_evidence_ref", sa.String(256), nullable=False),
        sa.Column("source_evidence_hash", sa.String(64), nullable=False),
        sa.Column("note_json", sa.Text(), nullable=False),
        sa.Column("note_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("completion_ref", "source_evidence_hash"),
        *[sa.ForeignKeyConstraint([column], [table + "." + key]) for column, table, key in (
            ("target_ref", "rg_targets", "target_ref"),
            ("completion_ref", "ar_target_root_completions", "completion_ref"),
            ("manifest_ref", "rm_target_root_completion_manifests", "manifest_ref"),
            ("version_ref", "rm_asset_versions", "version_ref"))])
    op.create_index("ix_rm_target_research_notes_target", "rm_target_research_notes",
                    ["target_ref", "accepted_at"])


def downgrade():
    raise RuntimeError("production migrations are forward-only")
