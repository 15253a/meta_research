from alembic import op
import sqlalchemy as sa

revision = "0068_manual_creation_inputs"
down_revision = "0067_creation_basis_inputs"
branch_labels = None
depends_on = None


def upgrade():
    for name in ("creation_basis_ref", "creation_basis_hash", "proposal_input_identity_hash"):
        op.add_column("hc_manual_question_creations", sa.Column(name, sa.String(64), nullable=True))


def downgrade():
    raise RuntimeError("vNext production migrations are forward-only")
