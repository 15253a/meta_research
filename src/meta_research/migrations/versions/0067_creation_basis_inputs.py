from alembic import op
import sqlalchemy as sa

revision = "0067_creation_basis_inputs"
down_revision = "0066_creation_material_copies"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_index("uq_prepared_creation_basis", table_name="rm_creation_bases")
    op.add_column("rm_creation_bases", sa.Column("input_identity_hash", sa.String(64)))
    op.create_index("uq_prepared_creation_basis", "rm_creation_bases",
        ["initialization_id", "draft_revision", "draft_hash"], unique=True,
        sqlite_where=sa.text("kind='prepared' AND input_identity_hash IS NULL"))
    op.create_index("uq_prepared_creation_inputs", "rm_creation_bases",
        ["initialization_id", "draft_revision", "draft_hash", "input_identity_hash"],
        unique=True, sqlite_where=sa.text("kind='prepared' AND input_identity_hash IS NOT NULL"))
    op.create_table("root_creation_custody",
        sa.Column("custody_ref", sa.String(96), primary_key=True),
        sa.Column("operation_ref", sa.String(160), nullable=False),
        sa.Column("source_json", sa.Text, nullable=False),
        sa.Column("receipt_json", sa.Text, nullable=False))


def downgrade():
    raise RuntimeError("vNext production migrations are forward-only")
