"""Add HC/AE autonomous lifecycle and human-sovereign Quest completion facts.

Revision ID: 0032_autonomous_completion
Revises: 0031_autonomous_question_owners
Create Date: 2026-08-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0032_autonomous_completion"
down_revision = "0031_autonomous_question_owners"
branch_labels = None
depends_on = None


def _hash(name: str, *, nullable: bool = False) -> sa.CheckConstraint:
    expression = (
        f"{name} IS NULL OR length({name}) = 64"
        if nullable
        else f"length({name}) = 64"
    )
    return sa.CheckConstraint(expression)


def _assert_downgrade_safe() -> None:
    connection = op.get_bind()
    for table in (
        "hc_autonomous_creation_contexts",
        "ae_autonomous_deepfetch_requests",
        "ae_autonomous_question_dispatches",
        "hc_quest_completion_contexts",
        "rg_quest_completion_acceptances",
        "ae_quest_endings",
    ):
        count = connection.execute(
            sa.text(f"SELECT COUNT(*) FROM {table}")
        ).scalar_one()
        if int(count):
            raise RuntimeError(
                "0032 downgrade blocked: autonomous/completion product facts exist"
            )


def upgrade() -> None:
    op.create_table(
        "hc_autonomous_creation_contexts",
        sa.Column("context_ref", sa.String(96), primary_key=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column(
            "reasoning_checkpoint_ref", sa.String(96), nullable=False, unique=True
        ),
        sa.Column("reasoning_checkpoint_hash", sa.String(64), nullable=False),
        sa.Column("source_outcome_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("source_json", sa.Text(), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("scientific_outcome_json", sa.Text(), nullable=False),
        sa.Column("scientific_outcome_hash", sa.String(64), nullable=False),
        sa.Column("autonomous_scope_json", sa.Text(), nullable=False),
        sa.Column("autonomous_scope_hash", sa.String(64), nullable=False),
        sa.Column("broad_authorization_json", sa.Text(), nullable=False),
        sa.Column("broad_authorization_hash", sa.String(64), nullable=False),
        sa.Column("context_receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("context_receipt_hash", sa.String(64), nullable=False),
        sa.Column("proposal_ref", sa.String(96), nullable=True, unique=True),
        sa.Column("proposal_json", sa.Text(), nullable=True),
        sa.Column("proposal_hash", sa.String(64), nullable=True),
        sa.Column("proposal_snapshot_ref", sa.String(96), nullable=True),
        sa.Column("proposal_request_hash", sa.String(64), nullable=True),
        sa.Column("proposal_receipt_ref", sa.String(96), nullable=True, unique=True),
        sa.Column("proposal_receipt_hash", sa.String(64), nullable=True),
        sa.Column("selected_content_ref", sa.String(96), nullable=True, unique=True),
        sa.Column("selected_content_hash", sa.String(64), nullable=True),
        sa.Column("selected_content_receipt_json", sa.Text(), nullable=True),
        sa.Column("selected_content_receipt_hash", sa.String(64), nullable=True),
        sa.Column("selection_request_hash", sa.String(64), nullable=True),
        sa.Column("selection_receipt_ref", sa.String(96), nullable=True, unique=True),
        sa.Column("selection_receipt_hash", sa.String(64), nullable=True),
        sa.Column("idempotency_key", sa.String(200), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.CheckConstraint("generation >= 1"),
        sa.CheckConstraint(
            "(proposal_ref IS NULL AND proposal_json IS NULL AND proposal_hash IS "
            "NULL AND proposal_snapshot_ref IS NULL AND proposal_request_hash IS "
            "NULL AND proposal_receipt_ref IS NULL AND proposal_receipt_hash IS "
            "NULL) OR (proposal_ref IS NOT NULL AND proposal_json IS NOT NULL AND "
            "length(proposal_hash) = 64 AND proposal_snapshot_ref IS NOT NULL AND "
            "length(proposal_request_hash) = 64 AND proposal_receipt_ref IS NOT "
            "NULL AND length(proposal_receipt_hash) = 64)"
        ),
        sa.CheckConstraint(
            "(selected_content_ref IS NULL AND selected_content_hash IS NULL AND "
            "selected_content_receipt_json IS NULL AND "
            "selected_content_receipt_hash IS NULL AND selection_request_hash IS "
            "NULL AND selection_receipt_ref IS NULL AND selection_receipt_hash IS "
            "NULL) OR (selected_content_ref IS NOT NULL AND "
            "length(selected_content_hash) = 64 AND "
            "selected_content_receipt_json IS NOT NULL AND "
            "length(selected_content_receipt_hash) = 64 AND "
            "length(selection_request_hash) = 64 AND selection_receipt_ref IS NOT "
            "NULL AND length(selection_receipt_hash) = 64)"
        ),
        *(
            _hash(name)
            for name in (
                "reasoning_checkpoint_hash",
                "source_hash",
                "scientific_outcome_hash",
                "autonomous_scope_hash",
                "broad_authorization_hash",
                "context_receipt_hash",
                "request_hash",
            )
        ),
    )

    op.create_table(
        "ae_autonomous_deepfetch_requests",
        sa.Column("request_ref", sa.String(96), primary_key=True),
        sa.Column("context_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("request_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reasoning_stage_run_request_ref", sa.String(96), nullable=False),
        sa.Column("cycle_ref", sa.String(96), nullable=False),
        sa.Column("quest_ref", sa.String(96), nullable=False),
        sa.Column("foreground_epoch", sa.Integer(), nullable=False),
        sa.Column("reasoning_checkpoint_ref", sa.String(96), nullable=False),
        sa.Column("reasoning_checkpoint_hash", sa.String(64), nullable=False),
        sa.Column("run_ref", sa.String(96), nullable=True),
        sa.Column("snapshot_ref", sa.String(96), nullable=True),
        sa.Column("failure_code", sa.String(96), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["context_ref"], ["hc_autonomous_creation_contexts.context_ref"]
        ),
        sa.CheckConstraint("foreground_epoch >= 1"),
        sa.CheckConstraint("status IN ('queued', 'succeeded', 'failed')"),
        sa.CheckConstraint(
            "(status = 'queued' AND run_ref IS NULL AND snapshot_ref IS NULL AND "
            "failure_code IS NULL) OR (status = 'succeeded' AND run_ref IS NOT "
            "NULL AND snapshot_ref IS NOT NULL AND failure_code IS NULL) OR "
            "(status = 'failed' AND snapshot_ref IS NULL AND failure_code IS NOT "
            "NULL)"
        ),
        _hash("reasoning_checkpoint_hash"),
        _hash("request_hash"),
        _hash("receipt_hash"),
    )
    op.create_index(
        "ix_ae_autonomous_deepfetch_queued",
        "ae_autonomous_deepfetch_requests",
        ["status", "created_at"],
    )

    op.create_table(
        "ae_autonomous_question_dispatches",
        sa.Column("dispatch_ref", sa.String(96), primary_key=True),
        sa.Column("context_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("reasoning_checkpoint_ref", sa.String(96), nullable=False),
        sa.Column("reasoning_checkpoint_hash", sa.String(64), nullable=False),
        sa.Column("reasoning_stage_run_request_ref", sa.String(96), nullable=False),
        sa.Column("foreground_epoch", sa.Integer(), nullable=False),
        sa.Column("content_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("selection_receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("selection_receipt_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("authorized_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["context_ref"], ["hc_autonomous_creation_contexts.context_ref"]
        ),
        sa.CheckConstraint("foreground_epoch >= 1"),
        *(
            _hash(name)
            for name in (
                "reasoning_checkpoint_hash",
                "content_hash",
                "selection_receipt_hash",
                "request_hash",
                "receipt_hash",
            )
        ),
    )

    op.create_table(
        "hc_quest_completion_contexts",
        sa.Column("context_ref", sa.String(96), primary_key=True),
        sa.Column("source_json", sa.Text(), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column(
            "candidate_completion_ref", sa.String(96), nullable=False, unique=True
        ),
        sa.Column("candidate_completion_json", sa.Text(), nullable=False),
        sa.Column("candidate_completion_hash", sa.String(64), nullable=False),
        sa.Column("quest_ref", sa.String(96), nullable=False),
        sa.Column("goal_revision_ref", sa.String(96), nullable=False),
        sa.Column("goal_revision_json", sa.Text(), nullable=False),
        sa.Column("goal_revision_hash", sa.String(64), nullable=False),
        sa.Column("preview_ref", sa.String(96), nullable=True, unique=True),
        sa.Column("preview_json", sa.Text(), nullable=True),
        sa.Column("preview_hash", sa.String(64), nullable=True),
        sa.Column("preview_request_hash", sa.String(64), nullable=True),
        sa.Column("preview_idempotency_key", sa.String(200), nullable=True, unique=True),
        sa.Column("decision", sa.String(16), nullable=True),
        sa.Column("decision_request_hash", sa.String(64), nullable=True),
        sa.Column("decision_idempotency_key", sa.String(200), nullable=True, unique=True),
        sa.Column("decision_receipt_ref", sa.String(96), nullable=True, unique=True),
        sa.Column("decision_receipt_hash", sa.String(64), nullable=True),
        sa.Column("decided_at", sa.Float(), nullable=True),
        sa.Column("idempotency_key", sa.String(200), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.CheckConstraint("decision IS NULL OR decision IN ('confirmed', 'rejected')"),
        sa.CheckConstraint(
            "(preview_ref IS NULL AND preview_json IS NULL AND preview_hash IS NULL "
            "AND preview_request_hash IS NULL AND preview_idempotency_key IS NULL) "
            "OR (preview_ref IS NOT NULL AND preview_json IS NOT NULL AND "
            "length(preview_hash) = 64 AND length(preview_request_hash) = 64 AND "
            "preview_idempotency_key IS NOT NULL)"
        ),
        sa.CheckConstraint(
            "(decision IS NULL AND decision_request_hash IS NULL AND "
            "decision_idempotency_key IS NULL AND decision_receipt_ref IS NULL AND "
            "decision_receipt_hash IS NULL AND decided_at IS NULL) OR (decision IS "
            "NOT NULL AND length(decision_request_hash) = 64 AND "
            "decision_idempotency_key IS NOT NULL AND decision_receipt_ref IS NOT "
            "NULL AND length(decision_receipt_hash) = 64 AND decided_at IS NOT NULL)"
        ),
        *(
            _hash(name)
            for name in (
                "source_hash",
                "candidate_completion_hash",
                "goal_revision_hash",
                "request_hash",
            )
        ),
    )

    op.create_table(
        "rg_quest_completion_acceptances",
        sa.Column("completion_ref", sa.String(96), primary_key=True),
        sa.Column("context_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("source_outcome_ref", sa.String(96), nullable=False, unique=True),
        sa.Column(
            "candidate_completion_ref", sa.String(96), nullable=False, unique=True
        ),
        sa.Column("candidate_completion_hash", sa.String(64), nullable=False),
        sa.Column("quest_ref", sa.String(96), nullable=False),
        sa.Column("goal_revision_ref", sa.String(96), nullable=False),
        sa.Column("goal_revision_hash", sa.String(64), nullable=False),
        sa.Column("human_preview_ref", sa.String(96), nullable=False),
        sa.Column("human_preview_hash", sa.String(64), nullable=False),
        sa.Column("human_receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("human_receipt_hash", sa.String(64), nullable=False),
        sa.Column(
            "reasoning_outcome_receipt_ref",
            sa.String(96),
            nullable=False,
            unique=True,
        ),
        sa.Column("reasoning_outcome_receipt_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        *(
            _hash(name)
            for name in (
                "candidate_completion_hash",
                "goal_revision_hash",
                "human_preview_hash",
                "human_receipt_hash",
                "reasoning_outcome_receipt_hash",
                "request_hash",
                "receipt_hash",
            )
        ),
    )

    op.create_table(
        "ae_quest_endings",
        sa.Column("transition_ref", sa.String(96), primary_key=True),
        sa.Column("quest_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("cycle_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("foreground_epoch", sa.Integer(), nullable=False),
        sa.Column(
            "reasoning_stage_run_request_ref",
            sa.String(96),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "candidate_completion_ref", sa.String(96), nullable=False, unique=True
        ),
        sa.Column("completion_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("completion_receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("completion_receipt_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("ended_at", sa.Float(), nullable=False),
        sa.CheckConstraint("foreground_epoch >= 1"),
        *(
            _hash(name)
            for name in (
                "completion_receipt_hash",
                "request_hash",
                "receipt_hash",
            )
        ),
    )


def downgrade() -> None:
    _assert_downgrade_safe()
    op.drop_table("ae_quest_endings")
    op.drop_table("rg_quest_completion_acceptances")
    op.drop_table("hc_quest_completion_contexts")
    op.drop_table("ae_autonomous_question_dispatches")
    op.drop_index(
        "ix_ae_autonomous_deepfetch_queued",
        table_name="ae_autonomous_deepfetch_requests",
    )
    op.drop_table("ae_autonomous_deepfetch_requests")
    op.drop_table("hc_autonomous_creation_contexts")
