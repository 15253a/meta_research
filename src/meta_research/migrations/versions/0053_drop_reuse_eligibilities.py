"""Drop the retired five-tier reuse eligibility machinery.

Revision ID: 0053_drop_reuse_eligibilities
Revises: 0052_artifact_data_role
Create Date: 2026-09-22
"""

from alembic import op


revision = "0053_drop_reuse_eligibilities"
down_revision = "0052_artifact_data_role"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    connection.exec_driver_sql("DROP TABLE IF EXISTS rg_reuse_eligibilities")
    with op.batch_alter_table("research_graph_state") as statement:
        statement.drop_column("reuse_eligibility_count")


def downgrade():
    raise RuntimeError("production migrations are forward-only")
