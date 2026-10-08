import sqlalchemy as sa
from alembic import op

revision = "0064_work_materials"
down_revision = "0063_merge_reply_guidance_creation"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("hc_work_material_submissions",
        sa.Column("submission_ref", sa.String(96), primary_key=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("command_hash", sa.String(64), nullable=False),
        sa.Column("command_json", sa.Text(), nullable=False),
        sa.Column("receiver_json", sa.Text(), nullable=False),
        sa.Column("anchor_kind", sa.String(32), nullable=False),
        sa.Column("anchor_ref", sa.String(128), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.CheckConstraint("state IN ('pending', 'ready', 'aborted')"))
    op.create_index("hc_material_anchor", "hc_work_material_submissions", ["anchor_kind", "anchor_ref"])
    op.create_table("hc_work_material_references",
        sa.Column("reference_ref", sa.String(96), primary_key=True),
        sa.Column("submission_ref", sa.String(96), sa.ForeignKey("hc_work_material_submissions.submission_ref"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("selection_json", sa.Text(), nullable=False),
        sa.Column("selection_hash", sa.String(64), nullable=False),
        sa.UniqueConstraint("submission_ref", "ordinal"))
    op.create_table("hc_work_material_roots",
        sa.Column("submission_ref", sa.String(96), sa.ForeignKey("hc_work_material_submissions.submission_ref"), primary_key=True),
        sa.Column("workspace_ref", sa.String(128), primary_key=True))
    op.create_index("hc_material_workspace", "hc_work_material_roots", ["workspace_ref"])
    op.create_table("hc_work_material_accesses",
        sa.Column("access_ref", sa.String(96), primary_key=True),
        sa.Column("reference_ref", sa.String(96), sa.ForeignKey("hc_work_material_references.reference_ref"), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("actor", sa.String(256), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False))
    op.create_index("hc_material_access_reference", "hc_work_material_accesses", ["reference_ref", "created_at"])


def downgrade():
    raise RuntimeError("production migrations are forward-only")
