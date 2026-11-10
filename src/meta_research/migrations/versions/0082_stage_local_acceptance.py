"""Retain historical exhaustion review provenance without requiring a fake call."""
from alembic import op
import sqlalchemy as sa

revision = "0082_stage_local_acceptance"
down_revision = "0081_quest_research_time"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    checks = tuple(sa.CheckConstraint(item["sqltext"])
                   for item in sa.inspect(connection).get_check_constraints("ar_bundle_exhaustion_evidence")
                   if not item.get("name"))
    connection.exec_driver_sql("PRAGMA legacy_alter_table=ON")
    try:
        with op.batch_alter_table("ar_bundle_exhaustion_evidence", recreate="always", table_args=checks) as batch:
            batch.alter_column("review_invocation_ref", existing_type=sa.String(96), nullable=True)
    finally:
        connection.exec_driver_sql("PRAGMA legacy_alter_table=OFF")


def downgrade():
    raise RuntimeError("production migrations are forward-only")
