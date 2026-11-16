from alembic import op
import sqlalchemy as sa

revision = "0089_bundle_dispatch_batch"
down_revision = "0088_resource_selection_index"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    if connection.exec_driver_sql(
        "SELECT 1 FROM ar_bundle_dispatch_decisions LIMIT 1"
    ).first() is not None:
        raise RuntimeError(
            "bundle_dispatch_batch_requires_new_data_root: "
            "retain the database with signed v1 dispatch decisions offline"
        )

    metadata = sa.MetaData()
    decisions = sa.Table("ar_bundle_dispatch_decisions", metadata, autoload_with=connection)
    launches = sa.Table("ar_target_launches", metadata, autoload_with=connection)
    for constraint in tuple(decisions.constraints):
        if (
            isinstance(constraint, sa.CheckConstraint)
            and "selected_target_ref" in str(constraint.sqltext)
        ) or (
            isinstance(constraint, sa.ForeignKeyConstraint)
            and tuple(constraint.columns.keys()) == ("selected_target_ref",)
        ):
            decisions.constraints.remove(constraint)
    for constraint in tuple(launches.constraints):
        if isinstance(constraint, sa.UniqueConstraint) and tuple(
            constraint.columns.keys()
        ) == ("dispatch_decision_ref",):
            launches.constraints.remove(constraint)

    connection.exec_driver_sql("PRAGMA legacy_alter_table=ON")
    try:
        with op.batch_alter_table(decisions.name, copy_from=decisions, recreate="always") as batch:
            batch.drop_column("selected_target_ref")
            batch.add_column(sa.Column("selected_target_refs_json", sa.Text(), nullable=False))
            batch.create_check_constraint(
                "ck_ar_bundle_dispatch_selection",
                "json_valid(selected_target_refs_json) "
                "AND json_type(selected_target_refs_json) = 'array' "
                "AND ((action = 'dispatch' AND json_array_length(selected_target_refs_json) BETWEEN 1 AND 64) "
                "OR (action IN ('wait', 'replan_required', 'revise_strategy') AND selected_target_refs_json = '[]'))",
            )
        with op.batch_alter_table(launches.name, copy_from=launches, recreate="always") as batch:
            batch.create_index("ix_ar_target_launches_dispatch_decision_ref", ["dispatch_decision_ref"])
    finally:
        connection.exec_driver_sql("PRAGMA legacy_alter_table=OFF")


def downgrade():
    raise RuntimeError("production migrations are forward-only")
