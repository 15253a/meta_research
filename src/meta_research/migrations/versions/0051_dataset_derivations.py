"""Record data provenance between exact, existing Dataset versions."""
from alembic import op
import sqlalchemy as sa

revision = "0051_dataset_derivations"
down_revision = "0050_question_relations"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("rg_dataset_derivations",
        sa.Column("dataset_derivation_ref", sa.String(96), primary_key=True),
        sa.Column("source_dataset_version_ref", sa.String(96), nullable=False),
        sa.Column("derived_dataset_version_ref", sa.String(96), nullable=False),
        sa.Column("question_ref", sa.String(96), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["source_dataset_version_ref"], ["rg_dataset_versions.dataset_version_ref"]),
        sa.ForeignKeyConstraint(["derived_dataset_version_ref"], ["rg_dataset_versions.dataset_version_ref"]),
        sa.CheckConstraint("source_dataset_version_ref != derived_dataset_version_ref"),
        sa.CheckConstraint("length(payload_hash) = 64 AND length(receipt_hash) = 64"))
    # Questions have several issuer tables; the Owner verifies their union.
    for side in ("source", "derived"):
        op.create_index("ix_rg_dataset_derivations_" + side, "rg_dataset_derivations",
                        [side + "_dataset_version_ref", "accepted_at", "dataset_derivation_ref"])


def downgrade():
    raise RuntimeError("production migrations are forward-only")
