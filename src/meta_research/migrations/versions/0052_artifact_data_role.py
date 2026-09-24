"""Allow exact data products to belong to their actual execution or assessment."""
from alembic import op
import sqlalchemy as sa

revision = "0052_artifact_data_role"
down_revision = "0051_dataset_derivations"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    before_violations = set(map(tuple, connection.exec_driver_sql("PRAGMA foreign_key_check")))
    table = sa.Table("rg_experiment_asset_roles", sa.MetaData(), autoload_with=connection)
    role_checks = [constraint for constraint in table.constraints
                   if isinstance(constraint, sa.CheckConstraint)
                   and "'log_asset'" in str(constraint.sqltext)]
    if len(role_checks) != 2:
        raise RuntimeError("experiment_asset_role_constraints_unexpected")
    for constraint in role_checks:
        table.constraints.remove(constraint)
    table.append_constraint(sa.CheckConstraint(
        "(role = 'checkpoint_artifact' AND subject_kind = 'variant_run') OR "
        "(role IN ('log_asset', 'analysis_asset', 'data_asset') "
        "AND subject_kind IN ('variant_run', 'evaluation_attempt')) OR "
        "(role = 'result_content' AND subject_kind = 'evaluation_attempt')",
        name="ck_experiment_asset_role_subject"))
    table.append_constraint(sa.CheckConstraint(
        "role IN ('checkpoint_artifact', 'log_asset', 'analysis_asset', 'data_asset', 'result_content')",
        name="ck_experiment_asset_role_kind"))
    with op.batch_alter_table(table.name, recreate="always", copy_from=table):
        pass
    if set(map(tuple, connection.exec_driver_sql("PRAGMA foreign_key_check"))) - before_violations:
        raise RuntimeError("experiment_asset_role_migration_foreign_key_violation")


def downgrade():
    raise RuntimeError("production migrations are forward-only")
