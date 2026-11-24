from alembic import op
import sqlalchemy as sa


revision = "0090_reasoning_content_retry"
down_revision = "0089_bundle_dispatch_batch"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    table = sa.Table("rm_reasoning_contents", sa.MetaData(), autoload_with=connection)
    removed = False
    for constraint in tuple(table.constraints):
        if (
            isinstance(constraint, sa.UniqueConstraint)
            and tuple(constraint.columns.keys()) == ("scientific_outcome_ref",)
        ):
            table.constraints.remove(constraint)
            removed = True
    if not removed:
        raise RuntimeError(
            "reasoning_content_scientific_outcome_constraint_unexpected"
        )

    connection.exec_driver_sql("PRAGMA legacy_alter_table=ON")
    try:
        with op.batch_alter_table(
            "rm_reasoning_contents", copy_from=table, recreate="always"
        ) as batch:
            batch.create_index(
                "ix_rm_reasoning_contents_scientific_outcome_ref",
                ["scientific_outcome_ref"],
            )
    finally:
        connection.exec_driver_sql("PRAGMA legacy_alter_table=OFF")


def downgrade():
    raise RuntimeError("production migrations are forward-only")
