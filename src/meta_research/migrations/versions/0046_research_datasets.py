"""Add global Dataset semantics and exact links to existing RM AssetVersions.

Revision ID: 0046_research_datasets
Revises: 0045_plan_canonical_storage
"""
from alembic import op
import sqlalchemy as sa

revision = "0046_research_datasets"
down_revision = "0045_plan_canonical_storage"
branch_labels = None
depends_on = None


def _fact_columns():
    return [
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.CheckConstraint("length(payload_hash) = 64 AND length(receipt_hash) = 64"),
    ]


def upgrade():
    op.create_table("rg_datasets",
        sa.Column("dataset_ref", sa.String(96), primary_key=True),
        sa.Column("semantic_key", sa.Text(), nullable=False, unique=True),
        *_fact_columns())
    op.create_table("rg_dataset_versions",
        sa.Column("dataset_version_ref", sa.String(96), primary_key=True),
        sa.Column("dataset_ref", sa.String(96), nullable=False),
        sa.Column("version_label", sa.Text(), nullable=False),
        *_fact_columns(),
        sa.ForeignKeyConstraint(["dataset_ref"], ["rg_datasets.dataset_ref"]),
        sa.UniqueConstraint("dataset_ref", "version_label"))
    op.create_table("rg_dataset_version_assets",
        sa.Column("dataset_version_ref", sa.String(96), primary_key=True),
        sa.Column("asset_version_ref", sa.String(96), primary_key=True),
        sa.ForeignKeyConstraint(["dataset_version_ref"], ["rg_dataset_versions.dataset_version_ref"]),
        sa.ForeignKeyConstraint(["asset_version_ref"], ["rm_asset_versions.version_ref"]))
    op.create_index("ix_rg_dataset_assets_version", "rg_dataset_version_assets", ["asset_version_ref"])
    op.create_table("rg_dataset_references",
        sa.Column("dataset_reference_ref", sa.String(96), primary_key=True),
        sa.Column("dataset_version_ref", sa.String(96), nullable=False),
        sa.Column("question_ref", sa.String(96), nullable=False),
        *_fact_columns(),
        sa.ForeignKeyConstraint(["dataset_version_ref"], ["rg_dataset_versions.dataset_version_ref"]),
        sa.UniqueConstraint("payload_hash"))
    # Question has root/manual/autonomous issuer tables; RG verifies the union.
    op.create_index("ix_rg_dataset_references_question", "rg_dataset_references", ["question_ref"])
    op.create_table("rg_dataset_commands",
        sa.Column("idempotency_key", sa.String(128), primary_key=True),
        sa.Column("operation", sa.String(32), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("result_ref", sa.String(96), nullable=False),
        sa.Column("recorded_at", sa.Float(), nullable=False))


def downgrade():
    raise RuntimeError("vNext production migrations are forward-only")
