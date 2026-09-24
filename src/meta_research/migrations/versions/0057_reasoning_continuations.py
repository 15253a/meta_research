"""Keep native Reasoning continuations separate from the immutable draft checkpoint."""
from alembic import op
import sqlalchemy as sa
revision = "0057_reasoning_continuations"
down_revision = "0056_movable_artifact_roles"
branch_labels = None
depends_on = None

def upgrade():
    # Scientific acceptance follows the resumed native decision. The Owner
    # validates its receipt; a SQL literal for the old checkpoint kind would
    # incorrectly make the immutable pre-fetch draft an acceptance source.
    connection = op.get_bind()
    table = sa.Table("rm_reasoning_scientific_candidates", sa.MetaData(), autoload_with=connection)
    checks = [c for c in table.constraints if isinstance(c, sa.CheckConstraint)
              and "checkpoint_receipt_kind" in str(c.sqltext)]
    if len(checks) != 1:
        raise RuntimeError("reasoning_candidate_checkpoint_constraint_unexpected")
    table.constraints.remove(checks[0])
    with op.batch_alter_table(table.name, recreate="always", copy_from=table):
        pass
    op.add_column("ae_autonomous_deepfetch_requests", sa.Column("retry_decision_ref", sa.String(96), nullable=True))
    op.add_column("ar_stage_attempts", sa.Column("reasoning_continuations_json", sa.Text(), nullable=True))
    op.add_column("ar_stage_attempts", sa.Column("reasoning_continuations_hash", sa.String(64), nullable=True))
    op.add_column("ar_stage_attempts", sa.Column("reasoning_provider_rejections_json", sa.Text(), nullable=True))
    op.add_column("ar_stage_attempts", sa.Column("reasoning_provider_rejections_hash", sa.String(64), nullable=True))

def downgrade():
    raise RuntimeError("production migrations are forward-only")
