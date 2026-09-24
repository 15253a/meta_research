"""Keep immutable role ordinals while allowing corrected current attribution."""
from alembic import op
import sqlalchemy as sa

revision = "0056_movable_artifact_roles"
down_revision = "0055_artifact_role_adjustments"
branch_labels = None
depends_on = None

def upgrade():
    connection = op.get_bind()
    before = set(map(tuple, connection.exec_driver_sql("PRAGMA foreign_key_check")))
    table = sa.Table("rg_experiment_asset_roles", sa.MetaData(), autoload_with=connection)
    constraints = [c for c in table.constraints if isinstance(c, sa.UniqueConstraint)
                   and list(c.columns.keys()) == ["subject_kind", "subject_ref", "role", "ordinal"]]
    if len(constraints) != 1:
        raise RuntimeError("experiment_asset_role_constraints_unexpected")
    table.constraints.remove(constraints[0])
    with op.batch_alter_table(table.name, recreate="always", copy_from=table):
        pass
    op.create_index("ix_rg_asset_roles_current_subject", table.name,
                    ["subject_kind", "subject_ref", "role", "ordinal", "accepted_at", "role_ref"])
    # Several Targets can explicitly adopt the same accepted Evaluation.
    # The Evaluation identity remains unique in its native table, while each
    # TargetCommit is a separate contextual handoff.
    commits = sa.Table("rg_target_commits", sa.MetaData(), autoload_with=connection)
    constraints = [c for c in commits.constraints if isinstance(c, sa.UniqueConstraint)
                   and list(c.columns.keys()) == ["evaluation_attempt_ref"]]
    if len(constraints) != 1:
        raise RuntimeError("target_commit_evaluation_constraint_unexpected")
    commits.constraints.remove(constraints[0])
    with op.batch_alter_table(commits.name, recreate="always", copy_from=commits):
        pass
    op.create_index("ix_rg_commits_evaluation_attempt", commits.name, ["evaluation_attempt_ref"])
    if set(map(tuple, connection.exec_driver_sql("PRAGMA foreign_key_check"))) - before:
        raise RuntimeError("experiment_asset_role_migration_foreign_key_violation")

def downgrade():
    raise RuntimeError("production migrations are forward-only")
