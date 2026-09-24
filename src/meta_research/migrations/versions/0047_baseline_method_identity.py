"""Index explicitly named method versions without rewriting historical identities."""
from alembic import op
import sqlalchemy as sa

revision = "0047_baseline_method_identity"
down_revision = "0046_research_datasets"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "rg_baseline_method_versions",
        sa.Column("method_key", sa.Text(), primary_key=True),
        sa.Column("method_version", sa.Text(), primary_key=True),
        sa.Column("baseline_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("method_contract_json", sa.Text(), nullable=False),
        sa.Column("method_contract_hash", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["baseline_ref"], ["rg_experiment_baselines.baseline_ref"]),
        sa.CheckConstraint("length(method_key) > 0 AND length(method_version) > 0"),
        sa.CheckConstraint("length(method_contract_hash) = 64"),
    )
    # Equal content suggests a review candidate, never an automatic identity merge.
    op.create_index("ix_rg_baseline_method_content", "rg_baseline_method_versions", ["method_contract_hash"])
    # Keep the existing rejection/Attempt continuation lifecycle and all historic
    # receipt bytes; only these explicit method-input corrections are admitted.
    rejection_table = sa.Table("rg_target_graph_rejections", sa.MetaData(), autoload_with=op.get_bind())
    old_reason_constraints = [constraint for constraint in rejection_table.constraints
        if isinstance(constraint, sa.CheckConstraint) and str(constraint.sqltext)
        == "reason_code IN ('target_candidate_owner_proof_unverified')"]
    if len(old_reason_constraints) != 1:
        raise RuntimeError("baseline_identity_rejection_constraint_unexpected")
    rejection_table.constraints.remove(old_reason_constraints[0])
    reasons = (
        "target_candidate_owner_proof_unverified", "baseline_method_identity_required",
        "baseline_method_envelope_invalid", "baseline_reference_invalid", "baseline_reference_not_found",
        "baseline_method_key_required", "baseline_method_version_required", "baseline_method_contract_required",
        "baseline_method_version_conflict", "baseline_method_version_content_conflict",
    )
    with op.batch_alter_table("rg_target_graph_rejections", recreate="always", copy_from=rejection_table) as batch:
        batch.create_check_constraint("ck_rg_target_graph_rejection_reason",
            "reason_code IN (" + ", ".join(repr(reason) for reason in reasons) + ")")


def downgrade():
    raise RuntimeError("vNext production migrations are forward-only")
