"""Allow a rejected host intake to resubmit unchanged scientific artifacts."""

from alembic import op
import sqlalchemy as sa


revision = "0078_target_host_intake_retry"
down_revision = "0077_stage_model"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    table = sa.Table("ar_target_root_completions", sa.MetaData(), autoload_with=connection)
    for constraint in tuple(table.constraints):
        if (isinstance(constraint, sa.UniqueConstraint)
                and list(constraint.columns.keys()) == ["artifact_snapshot_hash"]):
            table.constraints.remove(constraint)
    # Completion generation, operation, evidence and idempotency stay unique.
    # RM still permits only one accepted manifest for an artifact snapshot.
    connection.exec_driver_sql("PRAGMA legacy_alter_table=ON")
    try:
        with op.batch_alter_table(
            "ar_target_root_completions", copy_from=table, recreate="always"
        ) as batch:
            batch.create_index("ix_ar_target_root_completion_snapshot", ["artifact_snapshot_hash"])
    finally:
        connection.exec_driver_sql("PRAGMA legacy_alter_table=OFF")


def downgrade():
    raise RuntimeError("production migrations are forward-only")
