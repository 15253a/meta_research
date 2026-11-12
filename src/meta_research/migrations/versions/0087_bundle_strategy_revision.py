"""Admit an explicit return to Bundle strategy planning without changing waits."""
from alembic import op
import sqlalchemy as sa

revision = "0087_bundle_strategy_revision"
down_revision = "0086_bundle_runtime_refresh"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    table = sa.Table("ar_bundle_dispatch_decisions", sa.MetaData(), autoload_with=connection)
    checks = [item for item in table.constraints if isinstance(item, sa.CheckConstraint)
              and "action IN ('wait', 'replan_required')" in str(item.sqltext)]
    if len(checks) != 1:
        raise RuntimeError("bundle_dispatch_action_constraint_unexpected")
    old = checks[0]
    table.constraints.remove(old)
    table.append_constraint(sa.CheckConstraint(str(old.sqltext).replace(
        "action IN ('wait', 'replan_required')", "action IN ('wait', 'replan_required', 'revise_strategy')"),
        name=old.name))
    connection.exec_driver_sql("PRAGMA legacy_alter_table=ON")
    try:
        with op.batch_alter_table(table.name, copy_from=table, recreate="always"):
            pass
    finally:
        connection.exec_driver_sql("PRAGMA legacy_alter_table=OFF")


def downgrade():
    raise RuntimeError("production migrations are forward-only")
