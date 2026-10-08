import sqlalchemy as sa
from alembic import op

revision = "0062_human_reply_delivery"
down_revision = "0061_target_result_optional_disposition"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("hc_reply_deliveries",
        sa.Column("delivery_ref", sa.String(96), primary_key=True),
        sa.Column("response_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("request_ref", sa.String(96), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("command_hash", sa.String(64), nullable=False),
        sa.Column("command_json", sa.Text(), nullable=False),
        sa.Column("destination_json", sa.Text(), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("receipt_json", sa.Text()),
        sa.Column("failure_code", sa.String(128)),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.CheckConstraint("state IN ('pending', 'ready', 'aborted')"))


def downgrade() -> None:
    raise RuntimeError("production migrations are forward-only")
