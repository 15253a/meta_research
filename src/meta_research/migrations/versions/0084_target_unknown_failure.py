"""Record ended Target work without asserting an unobserved process stop."""

from alembic import op
import sqlalchemy as sa

revision = "0084_target_unknown_failure"
down_revision = "0083_reasoning_primary_source"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    table = sa.Table("ar_target_root_lifecycles", sa.MetaData(), autoload_with=connection)
    for constraint in tuple(table.constraints):
        if not isinstance(constraint, sa.CheckConstraint):
            continue
        expression = str(constraint.sqltext)
        if expression == "status IN ('running', 'finalizing', 'completed', 'cancelled')":
            table.constraints.remove(constraint)
            table.append_constraint(sa.CheckConstraint(
                "status IN ('running', 'finalizing', 'completed', 'cancelled', 'failed')"))
        elif "status = 'running' AND completion_ref IS NULL" in expression:
            table.constraints.remove(constraint)
            table.append_constraint(sa.CheckConstraint(
                f"({expression}) OR (status='failed' AND completion_ref IS NULL "
                "AND cancel_ref IS NOT NULL AND cancel_requested_at IS NOT NULL "
                "AND cancelled_at IS NULL)"))
    connection.exec_driver_sql("PRAGMA legacy_alter_table=ON")
    try:
        with op.batch_alter_table("ar_target_root_lifecycles", copy_from=table,
                                  recreate="always"):
            pass
    finally:
        connection.exec_driver_sql("PRAGMA legacy_alter_table=OFF")


def downgrade():
    raise RuntimeError("production migrations are forward-only")
