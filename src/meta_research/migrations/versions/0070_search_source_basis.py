from alembic import op
import sqlalchemy as sa


revision = "0070_search_source_basis"
down_revision = "0069_merge_goal_creation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("hc_deepfetch_requests") as batch:
        batch.drop_constraint("uq_hc_deepfetch_request_basis", type_="unique")
        batch.create_unique_constraint("uq_hc_deepfetch_request_basis", ["initialization_id", "draft_revision", "draft_hash", "scope_hash"])
    op.add_column("hc_quest_initializations", sa.Column("confirmed_search_basis_json", sa.Text(), nullable=True))


def downgrade() -> None:
    raise RuntimeError("vNext production migrations are forward-only")
