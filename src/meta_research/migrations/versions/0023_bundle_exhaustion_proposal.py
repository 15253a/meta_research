"""Add AR exhaustion evidence and the AE ExhaustionProposal ledger.

Revision ID: 0023_bundle_exhaustion_proposal
Revises: 0022_target_run_runtime
Create Date: 2026-08-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0023_bundle_exhaustion_proposal"
down_revision = "0022_target_run_runtime"
branch_labels = None
depends_on = None


def _hash(name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"length({name}) = 64")


def _replace_stage_attempts_for_exhaustion_completion() -> None:
    """Permit the fixed no-submission exhaustion completion shape.

    A normal accepted stage attempt still requires an executed submission.  An
    accepted ExhaustionProposal is deliberately different: AR completes the
    current Bundle Attempt from the reviewed evidence and AE receipt without
    inventing a TargetPlan submission or execution receipt.
    """

    replacement = "ar_stage_attempts_exhaustion_v1"
    op.create_table(
        replacement,
        sa.Column("attempt_ref", sa.String(64), primary_key=True),
        sa.Column("run_ref", sa.String(64), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("root_session_ref", sa.String(64), nullable=False),
        sa.Column("fence_ref", sa.String(64), nullable=False, unique=True),
        sa.Column("predecessor_attempt_ref", sa.String(64), nullable=True, unique=True),
        sa.Column("predecessor_outcome_hash", sa.String(64), nullable=True),
        sa.Column("predecessor_material_outcome_hash", sa.String(64), nullable=True),
        sa.Column("predecessor_rejection_receipt_ref", sa.String(64), nullable=True),
        sa.Column(
            "predecessor_rejection_receipt_subject_ref",
            sa.String(64),
            nullable=True,
        ),
        sa.Column("predecessor_rejection_receipt_hash", sa.String(64), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("primary_draft_json", sa.Text(), nullable=True),
        sa.Column("primary_draft_hash", sa.String(64), nullable=True),
        sa.Column("primary_adapter_kind", sa.String(64), nullable=True),
        sa.Column("primary_recorded_at", sa.Float(), nullable=True),
        sa.Column("submission_ref", sa.String(64), nullable=True, unique=True),
        sa.Column("payload_json", sa.Text(), nullable=True),
        sa.Column("payload_hash", sa.String(64), nullable=True),
        sa.Column("material_outcome_hash", sa.String(64), nullable=True),
        sa.Column("execution_receipt_ref", sa.String(64), nullable=True, unique=True),
        sa.Column("execution_receipt_hash", sa.String(64), nullable=True),
        sa.Column("decision_receipt_ref", sa.String(64), nullable=True),
        sa.Column("decision_receipt_subject_ref", sa.String(64), nullable=True),
        sa.Column("decision_receipt_hash", sa.String(64), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("executed_at", sa.Float(), nullable=True),
        sa.Column("closed_at", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["run_ref"], ["ar_stage_runs.run_ref"]),
        sa.ForeignKeyConstraint(
            ["root_session_ref"], ["ar_stage_sessions.session_ref"]
        ),
        sa.ForeignKeyConstraint(
            ["predecessor_attempt_ref"], [f"{replacement}.attempt_ref"]
        ),
        sa.UniqueConstraint("run_ref", "generation"),
        sa.CheckConstraint("generation >= 1"),
        sa.CheckConstraint(
            "status IN ('running', 'executed', 'rejected', 'completed')"
        ),
        _hash("material_outcome_hash"),
        _hash("predecessor_material_outcome_hash"),
        sa.CheckConstraint(
            "(primary_draft_json IS NULL AND primary_draft_hash IS NULL "
            "AND primary_adapter_kind IS NULL AND primary_recorded_at IS NULL) OR "
            "(primary_draft_json IS NOT NULL AND primary_draft_hash IS NOT NULL "
            "AND length(primary_draft_hash) = 64 AND primary_adapter_kind IS NOT NULL "
            "AND primary_recorded_at IS NOT NULL)"
        ),
        sa.CheckConstraint(
            "(status = 'running' AND submission_ref IS NULL AND payload_json IS NULL "
            "AND payload_hash IS NULL AND material_outcome_hash IS NULL "
            "AND execution_receipt_ref IS NULL AND execution_receipt_hash IS NULL "
            "AND executed_at IS NULL AND closed_at IS NULL) OR "
            "(status IN ('executed', 'rejected', 'completed') "
            "AND submission_ref IS NOT NULL AND payload_json IS NOT NULL "
            "AND payload_hash IS NOT NULL AND length(payload_hash) = 64 "
            "AND material_outcome_hash IS NOT NULL "
            "AND length(material_outcome_hash) = 64 "
            "AND execution_receipt_ref IS NOT NULL "
            "AND execution_receipt_hash IS NOT NULL "
            "AND length(execution_receipt_hash) = 64 AND executed_at IS NOT NULL) OR "
            "(status = 'completed' AND submission_ref IS NULL AND payload_json IS NULL "
            "AND payload_hash IS NULL AND material_outcome_hash IS NULL "
            "AND execution_receipt_ref IS NULL AND execution_receipt_hash IS NULL "
            "AND executed_at IS NULL)"
        ),
        sa.CheckConstraint(
            "(status IN ('running', 'executed') AND decision_receipt_ref IS NULL "
            "AND decision_receipt_subject_ref IS NULL AND decision_receipt_hash IS NULL "
            "AND closed_at IS NULL) OR "
            "(status IN ('rejected', 'completed') AND decision_receipt_ref IS NOT NULL "
            "AND decision_receipt_subject_ref IS NOT NULL "
            "AND decision_receipt_hash IS NOT NULL "
            "AND length(decision_receipt_hash) = 64 AND closed_at IS NOT NULL)"
        ),
        sa.CheckConstraint(
            "(predecessor_attempt_ref IS NULL AND predecessor_outcome_hash IS NULL "
            "AND predecessor_material_outcome_hash IS NULL "
            "AND predecessor_rejection_receipt_ref IS NULL "
            "AND predecessor_rejection_receipt_subject_ref IS NULL "
            "AND predecessor_rejection_receipt_hash IS NULL) OR "
            "(predecessor_attempt_ref IS NOT NULL "
            "AND status IN ('running', 'completed') "
            "AND predecessor_outcome_hash IS NULL "
            "AND predecessor_material_outcome_hash IS NULL "
            "AND predecessor_rejection_receipt_ref IS NULL "
            "AND predecessor_rejection_receipt_subject_ref IS NULL "
            "AND predecessor_rejection_receipt_hash IS NULL) OR "
            "(predecessor_attempt_ref IS NOT NULL "
            "AND status IN ('executed', 'rejected', 'completed') "
            "AND predecessor_outcome_hash IS NOT NULL "
            "AND length(predecessor_outcome_hash) = 64 "
            "AND predecessor_material_outcome_hash IS NOT NULL "
            "AND length(predecessor_material_outcome_hash) = 64 "
            "AND predecessor_rejection_receipt_ref IS NOT NULL "
            "AND predecessor_rejection_receipt_subject_ref IS NOT NULL "
            "AND predecessor_rejection_receipt_hash IS NOT NULL "
            "AND length(predecessor_rejection_receipt_hash) = 64)"
        ),
    )
    columns = (
        "attempt_ref, run_ref, generation, root_session_ref, fence_ref, "
        "predecessor_attempt_ref, predecessor_outcome_hash, "
        "predecessor_material_outcome_hash, predecessor_rejection_receipt_ref, "
        "predecessor_rejection_receipt_subject_ref, "
        "predecessor_rejection_receipt_hash, status, primary_draft_json, "
        "primary_draft_hash, primary_adapter_kind, primary_recorded_at, "
        "submission_ref, payload_json, payload_hash, material_outcome_hash, "
        "execution_receipt_ref, execution_receipt_hash, decision_receipt_ref, "
        "decision_receipt_subject_ref, decision_receipt_hash, created_at, "
        "executed_at, closed_at"
    )
    op.execute(
        sa.text(
            f"INSERT INTO {replacement} ({columns}) "
            f"SELECT {columns} FROM ar_stage_attempts"
        )
    )
    op.drop_table("ar_stage_attempts")
    op.rename_table(replacement, "ar_stage_attempts")


def upgrade() -> None:
    _replace_stage_attempts_for_exhaustion_completion()
    op.add_column(
        "agent_runtime_state",
        sa.Column(
            "bundle_exhaustion_evidence_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "advancement_engine_state",
        sa.Column(
            "bundle_exhaustion_proposal_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "advancement_engine_state",
        sa.Column(
            "bundle_exhaustion_decision_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )

    # AR freezes the exact reviewed exploration inventory.  The per-record
    # receipts prove immutable provenance/content; they do not decide the
    # semantic exhaustion outcome, which remains AE-owned below.
    op.create_table(
        "ar_bundle_exhaustion_evidence",
        sa.Column("evidence_ref", sa.String(96), primary_key=True),
        sa.Column("evidence_identity", sa.String(128), nullable=False, unique=True),
        sa.Column("request_ref", sa.String(64), nullable=False),
        sa.Column("request_receipt_ref", sa.String(96), nullable=False),
        sa.Column("request_receipt_hash", sa.String(64), nullable=False),
        sa.Column("cycle_ref", sa.String(64), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("run_ref", sa.String(96), nullable=False),
        sa.Column("attempt_ref", sa.String(96), nullable=False),
        sa.Column("root_session_ref", sa.String(96), nullable=False),
        sa.Column("fence_ref", sa.String(96), nullable=False),
        sa.Column("context_pack_ref", sa.String(96), nullable=False),
        sa.Column("context_pack_hash", sa.String(64), nullable=False),
        sa.Column("formal_plan_ref", sa.String(96), nullable=False),
        sa.Column("formal_plan_content_hash", sa.String(64), nullable=False),
        sa.Column("native_session_ref", sa.String(128), nullable=False),
        sa.Column("primary_invocation_ref", sa.String(96), nullable=False),
        sa.Column("primary_response_hash", sa.String(64), nullable=False),
        sa.Column("primary_assessment_hash", sa.String(64), nullable=False),
        sa.Column("review_invocation_ref", sa.String(96), nullable=False),
        sa.Column("review_response_hash", sa.String(64), nullable=False),
        sa.Column("reviewer_agent_ref", sa.String(128), nullable=False),
        sa.Column("completion_contract_hash", sa.String(64), nullable=False),
        sa.Column("evidence_json", sa.Text(), nullable=False),
        sa.Column("evidence_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["request_ref"], ["ae_stage_run_requests.request_ref"]
        ),
        sa.ForeignKeyConstraint(["run_ref"], ["ar_stage_runs.run_ref"]),
        sa.ForeignKeyConstraint(["attempt_ref"], ["ar_stage_attempts.attempt_ref"]),
        sa.ForeignKeyConstraint(
            ["root_session_ref"], ["ar_stage_sessions.session_ref"]
        ),
        sa.ForeignKeyConstraint(["fence_ref"], ["ar_execution_fences.fence_ref"]),
        sa.ForeignKeyConstraint(
            ["primary_invocation_ref"],
            ["ar_stage_provider_invocations.invocation_ref"],
        ),
        sa.ForeignKeyConstraint(
            ["review_invocation_ref"],
            ["ar_stage_provider_invocations.invocation_ref"],
        ),
        sa.CheckConstraint("epoch >= 1"),
        *(
            _hash(name)
            for name in (
                "request_receipt_hash",
                "context_pack_hash",
                "formal_plan_content_hash",
                "primary_response_hash",
                "primary_assessment_hash",
                "review_response_hash",
                "completion_contract_hash",
                "evidence_hash",
                "request_hash",
                "receipt_hash",
            )
        ),
    )
    op.create_index(
        "ix_ar_bundle_exhaustion_evidence_run",
        "ar_bundle_exhaustion_evidence",
        ["run_ref", "accepted_at"],
    )
    op.create_table(
        "ar_bundle_exhaustion_evidence_records",
        sa.Column("evidence_ref", sa.String(96), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("record_ref", sa.String(256), nullable=False, unique=True),
        sa.Column("record_json", sa.Text(), nullable=False),
        sa.Column("record_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(
            ["evidence_ref"], ["ar_bundle_exhaustion_evidence.evidence_ref"]
        ),
        sa.PrimaryKeyConstraint("evidence_ref", "ordinal"),
        sa.CheckConstraint("ordinal >= 1"),
        _hash("record_hash"),
        _hash("receipt_hash"),
    )

    # The Bundle root Agent authors this immutable, non-authoritative value.
    # It deliberately remains distinct from both BundleReport and StageCommit.
    op.create_table(
        "ae_bundle_exhaustion_proposals",
        sa.Column("proposal_ref", sa.String(96), primary_key=True),
        sa.Column("proposal_identity", sa.String(128), nullable=False, unique=True),
        sa.Column("request_ref", sa.String(64), nullable=False),
        sa.Column("request_receipt_ref", sa.String(96), nullable=False),
        sa.Column("request_receipt_hash", sa.String(64), nullable=False),
        sa.Column("cycle_ref", sa.String(64), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("run_ref", sa.String(96), nullable=False),
        sa.Column("attempt_ref", sa.String(96), nullable=False),
        sa.Column("root_session_ref", sa.String(96), nullable=False),
        sa.Column("fence_ref", sa.String(96), nullable=False),
        sa.Column("context_pack_ref", sa.String(96), nullable=False),
        sa.Column("context_pack_hash", sa.String(64), nullable=False),
        sa.Column("formal_plan_ref", sa.String(96), nullable=False),
        sa.Column("formal_plan_content_hash", sa.String(64), nullable=False),
        sa.Column("formal_plan_content_receipt_ref", sa.String(96), nullable=False),
        sa.Column("formal_plan_content_receipt_hash", sa.String(64), nullable=False),
        sa.Column("evidence_ref", sa.String(96), nullable=False),
        sa.Column("evidence_hash", sa.String(64), nullable=False),
        sa.Column("evidence_receipt_ref", sa.String(96), nullable=False),
        sa.Column("evidence_receipt_hash", sa.String(64), nullable=False),
        sa.Column("proposal_json", sa.Text(), nullable=False),
        sa.Column("proposal_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("authoritative", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["request_ref"], ["ae_stage_run_requests.request_ref"]
        ),
        sa.ForeignKeyConstraint(["run_ref"], ["ar_stage_runs.run_ref"]),
        sa.ForeignKeyConstraint(["attempt_ref"], ["ar_stage_attempts.attempt_ref"]),
        sa.ForeignKeyConstraint(
            ["root_session_ref"], ["ar_stage_sessions.session_ref"]
        ),
        sa.ForeignKeyConstraint(["fence_ref"], ["ar_execution_fences.fence_ref"]),
        sa.ForeignKeyConstraint(
            ["formal_plan_content_receipt_ref"],
            ["rg_formal_plan_content_acceptances.receipt_ref"],
        ),
        sa.ForeignKeyConstraint(
            ["evidence_ref"], ["ar_bundle_exhaustion_evidence.evidence_ref"]
        ),
        sa.ForeignKeyConstraint(
            ["evidence_receipt_ref"],
            ["ar_bundle_exhaustion_evidence.receipt_ref"],
        ),
        sa.UniqueConstraint("request_ref", "proposal_identity"),
        sa.CheckConstraint("epoch >= 1"),
        sa.CheckConstraint("authoritative = 0"),
        *(
            _hash(name)
            for name in (
                "request_receipt_hash",
                "context_pack_hash",
                "formal_plan_content_hash",
                "formal_plan_content_receipt_hash",
                "evidence_hash",
                "evidence_receipt_hash",
                "proposal_hash",
            )
        ),
    )

    # Submission/reconciliation share this stable operation identity.  Current
    # status may advance only by appending a decision below and then pointing
    # at that exact immutable decision.
    op.create_table(
        "ae_bundle_exhaustion_operations",
        sa.Column("operation_ref", sa.String(96), primary_key=True),
        sa.Column("proposal_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("proposal_identity", sa.String(128), nullable=False, unique=True),
        sa.Column("proposal_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("current_decision_ref", sa.String(96), nullable=True, unique=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["proposal_ref"], ["ae_bundle_exhaustion_proposals.proposal_ref"]
        ),
        sa.CheckConstraint(
            "status IN ('accepted', 'rejected', 'stale', 'needs_input', "
            "'outcome_unknown', 'technical_blocker')"
        ),
        _hash("proposal_hash"),
        _hash("request_hash"),
    )

    op.create_table(
        "ae_bundle_exhaustion_decisions",
        sa.Column("decision_ref", sa.String(96), primary_key=True),
        sa.Column("operation_ref", sa.String(96), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("feedback_json", sa.Text(), nullable=False),
        sa.Column("feedback_hash", sa.String(64), nullable=False),
        sa.Column("human_request_ref", sa.String(96), nullable=True),
        sa.Column("blocker_ref", sa.String(256), nullable=True),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_kind", sa.String(96), nullable=False),
        sa.Column("receipt_subject_ref", sa.String(96), nullable=False),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("decided_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["operation_ref"], ["ae_bundle_exhaustion_operations.operation_ref"]
        ),
        sa.UniqueConstraint("operation_ref", "ordinal"),
        sa.CheckConstraint("ordinal >= 1"),
        sa.CheckConstraint(
            "status IN ('accepted', 'rejected', 'stale', 'needs_input', "
            "'outcome_unknown', 'technical_blocker')"
        ),
        sa.CheckConstraint(
            "(status = 'needs_input' AND human_request_ref IS NOT NULL AND "
            "blocker_ref IS NULL) OR (status = 'technical_blocker' AND "
            "blocker_ref IS NOT NULL AND human_request_ref IS NULL) OR "
            "(status NOT IN ('needs_input', 'technical_blocker') AND "
            "human_request_ref IS NULL AND blocker_ref IS NULL)"
        ),
        _hash("feedback_hash"),
        _hash("receipt_hash"),
    )
    op.create_index(
        "ix_ae_bundle_exhaustion_decisions_operation",
        "ae_bundle_exhaustion_decisions",
        ["operation_ref", "ordinal"],
    )


def downgrade() -> None:
    raise RuntimeError("vNext production migrations are forward-only")
