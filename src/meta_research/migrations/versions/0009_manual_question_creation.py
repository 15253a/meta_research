"""Add the independent follow-up ManualCreation context and Owner facts.

Revision ID: 0009_manual_question_creation
Revises: 0008_quest_acquisition_session
Create Date: 2026-08-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0009_manual_question_creation"
down_revision = "0008_quest_acquisition_session"
branch_labels = None
depends_on = None


def _hash(name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"{name} IS NULL OR length({name}) = 64")


def _counter(name: str) -> sa.Column:
    return sa.Column(name, sa.Integer(), nullable=False, server_default="0")


def upgrade() -> None:
    op.add_column("human_collaboration_state", _counter("manual_creation_count"))
    op.add_column(
        "human_collaboration_state", _counter("active_manual_creation_count")
    )
    op.add_column(
        "human_collaboration_state", _counter("confirmed_manual_seed_count")
    )
    # Literature custody is shared by both creation modes, while its identity is
    # owned by the creation context rather than by a Quest draft.  Keep the old
    # basis columns for the accepted Quest policy that was read by DeepFetch,
    # but replace the old one-snapshot-per-draft constraint with a context key.
    with op.batch_alter_table(
        "rm_literature_snapshots", recreate="always"
    ) as batch:
        batch.add_column(
            sa.Column("creation_context_kind", sa.String(length=48), nullable=True)
        )
        batch.add_column(
            sa.Column("creation_context_ref", sa.String(length=64), nullable=True)
        )
        batch.add_column(
            sa.Column("quest_ref", sa.String(length=64), nullable=True)
        )
        batch.drop_constraint(
            "uq_rm_literature_snapshot_basis", type_="unique"
        )
    op.execute(
        "UPDATE rm_literature_snapshots SET creation_context_kind = "
        "'quest_initialization', creation_context_ref = initialization_id "
        "WHERE creation_context_ref IS NULL"
    )
    op.create_index(
        "uq_rm_literature_snapshot_context",
        "rm_literature_snapshots",
        ["creation_context_kind", "creation_context_ref"],
        unique=True,
        sqlite_where=sa.text(
            "creation_context_kind = 'manual_question_creation'"
        ),
    )

    op.create_table(
        "hc_manual_question_creations",
        sa.Column("context_ref", sa.String(length=64), primary_key=True),
        sa.Column("quest_ref", sa.String(length=64), nullable=False),
        sa.Column("quest_initialization_id", sa.String(length=64), nullable=False),
        sa.Column("quest_receipt_ref", sa.String(length=64), nullable=False),
        sa.Column("quest_receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("parent_question_ref", sa.String(length=64), nullable=False),
        sa.Column("parent_question_receipt_ref", sa.String(length=64), nullable=False),
        sa.Column("parent_question_receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("creation_mode", sa.String(length=32), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("seed_ref", sa.String(length=64), nullable=True, unique=True),
        sa.Column("seed_json", sa.Text(), nullable=True),
        sa.Column("seed_hash", sa.String(length=64), nullable=True),
        sa.Column("seed_receipt_ref", sa.String(length=64), nullable=True, unique=True),
        sa.Column("seed_receipt_hash", sa.String(length=64), nullable=True),
        sa.Column("research_choice", sa.String(length=24), nullable=True),
        sa.Column("waiver_ref", sa.String(length=64), nullable=True, unique=True),
        sa.Column("waiver_hash", sa.String(length=64), nullable=True),
        sa.Column("waiver_receipt_ref", sa.String(length=64), nullable=True, unique=True),
        sa.Column("waiver_receipt_hash", sa.String(length=64), nullable=True),
        sa.Column("research_basis_hash", sa.String(length=64), nullable=True),
        sa.Column("deepfetch_request_ref", sa.String(length=64), nullable=True),
        sa.Column("deepfetch_run_ref", sa.String(length=64), nullable=True),
        sa.Column("literature_snapshot_ref", sa.String(length=64), nullable=True),
        sa.Column("literature_snapshot_hash", sa.String(length=64), nullable=True),
        sa.Column("deepfetch_failure_code", sa.String(length=96), nullable=True),
        sa.Column("proposal_revision", sa.Integer(), nullable=False),
        sa.Column("proposal_ref", sa.String(length=64), nullable=True, unique=True),
        sa.Column("proposal_json", sa.Text(), nullable=True),
        sa.Column("proposal_hash", sa.String(length=64), nullable=True),
        sa.Column("proposal_basis_hash", sa.String(length=64), nullable=True),
        sa.Column("confirmation_ref", sa.String(length=64), nullable=True, unique=True),
        sa.Column("confirmation_hash", sa.String(length=64), nullable=True),
        sa.Column("content_ref", sa.String(length=64), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("content_receipt_ref", sa.String(length=64), nullable=True),
        sa.Column("content_receipt_hash", sa.String(length=64), nullable=True),
        sa.Column("question_ref", sa.String(length=64), nullable=True),
        sa.Column("question_receipt_ref", sa.String(length=64), nullable=True),
        sa.Column("question_receipt_hash", sa.String(length=64), nullable=True),
        sa.Column("recovery_first_missing", sa.String(length=48), nullable=True),
        sa.Column("recovery_reason_code", sa.String(length=96), nullable=True),
        sa.Column("recovery_attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_retry_at", sa.Float(), nullable=True),
        sa.Column("terminal_decision", sa.String(length=16), nullable=True),
        sa.Column("cancel_receipt_ref", sa.String(length=64), nullable=True, unique=True),
        sa.Column("cancel_receipt_hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("completed_at", sa.Float(), nullable=True),
        sa.Column("cancelled_at", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(
            ["quest_initialization_id"],
            ["rg_quests.initialization_id"],
        ),
        _hash("quest_receipt_hash"),
        _hash("parent_question_receipt_hash"),
        _hash("seed_hash"),
        _hash("seed_receipt_hash"),
        _hash("waiver_hash"),
        _hash("waiver_receipt_hash"),
        _hash("research_basis_hash"),
        _hash("literature_snapshot_hash"),
        _hash("proposal_hash"),
        _hash("proposal_basis_hash"),
        _hash("confirmation_hash"),
        _hash("content_hash"),
        _hash("content_receipt_hash"),
        _hash("question_receipt_hash"),
        _hash("cancel_receipt_hash"),
        sa.CheckConstraint("creation_mode = 'ManualCreation'"),
        sa.CheckConstraint("generation >= 1"),
        sa.CheckConstraint("proposal_revision >= 0"),
        sa.CheckConstraint("recovery_attempt_count >= 0"),
        sa.CheckConstraint(
            "status IN ('draft', 'seed_confirmed', 'research_pending', "
            "'research_ready', 'confirmed', 'recovering', 'completed', 'cancelled')"
        ),
        sa.CheckConstraint(
            "research_choice IS NULL OR research_choice IN ('deepfetch', 'waiver')"
        ),
        sa.CheckConstraint(
            "terminal_decision IS NULL OR terminal_decision IN ('commit', 'cancel')"
        ),
        sa.CheckConstraint(
            "(seed_ref IS NULL AND seed_json IS NULL AND seed_hash IS NULL AND "
            "seed_receipt_ref IS NULL AND seed_receipt_hash IS NULL) OR "
            "(seed_ref IS NOT NULL AND seed_json IS NOT NULL AND seed_hash IS NOT NULL "
            "AND seed_receipt_ref IS NOT NULL AND seed_receipt_hash IS NOT NULL)"
        ),
        sa.CheckConstraint(
            "(waiver_ref IS NULL AND waiver_hash IS NULL AND "
            "waiver_receipt_ref IS NULL AND waiver_receipt_hash IS NULL) OR "
            "(waiver_ref IS NOT NULL AND waiver_hash IS NOT NULL AND "
            "waiver_receipt_ref IS NOT NULL AND waiver_receipt_hash IS NOT NULL)"
        ),
        sa.CheckConstraint(
            "(proposal_ref IS NULL AND proposal_json IS NULL AND proposal_hash IS NULL) "
            "OR (proposal_ref IS NOT NULL AND proposal_json IS NOT NULL AND "
            "proposal_hash IS NOT NULL)"
        ),
    )
    op.create_index(
        "ix_hc_manual_question_creations_quest",
        "hc_manual_question_creations",
        ["quest_ref", "updated_at"],
    )

    op.create_table(
        "hc_manual_drafting_sessions",
        sa.Column("session_ref", sa.String(length=64), primary_key=True),
        sa.Column("context_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("native_session_ref", sa.String(length=256), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["context_ref"], ["hc_manual_question_creations.context_ref"]
        ),
        sa.CheckConstraint("status IN ('open', 'closed')"),
    )
    op.create_table(
        "hc_manual_drafting_turns",
        sa.Column("turn_ref", sa.String(length=64), primary_key=True),
        sa.Column("session_ref", sa.String(length=64), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("basis_hash", sa.String(length=64), nullable=False),
        sa.Column("drafting_context_json", sa.Text(), nullable=False),
        sa.Column("drafting_context_hash", sa.String(length=64), nullable=False),
        sa.Column("user_content", sa.Text(), nullable=False),
        sa.Column("user_content_hash", sa.String(length=64), nullable=False),
        sa.Column("assistant_status", sa.String(length=16), nullable=False),
        sa.Column(
            "assistant_attempt_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("assistant_started_at", sa.Float(), nullable=True),
        sa.Column("assistant_content", sa.Text(), nullable=True),
        sa.Column("assistant_content_hash", sa.String(length=64), nullable=True),
        sa.Column("reason_code", sa.String(length=96), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("completed_at", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(
            ["session_ref"], ["hc_manual_drafting_sessions.session_ref"]
        ),
        sa.UniqueConstraint(
            "session_ref", "ordinal", name="uq_hc_manual_drafting_turn_ordinal"
        ),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_hc_manual_drafting_turn_idempotency"
        ),
        _hash("request_hash"),
        _hash("basis_hash"),
        _hash("drafting_context_hash"),
        _hash("user_content_hash"),
        _hash("assistant_content_hash"),
        sa.CheckConstraint("ordinal >= 1"),
        sa.CheckConstraint("assistant_attempt_count >= 0"),
        sa.CheckConstraint(
            "assistant_status IN "
            "('queued', 'running', 'completed', 'unavailable', 'failed')"
        ),
        sa.CheckConstraint(
            "(assistant_status = 'queued' AND assistant_attempt_count >= 0 AND "
            "assistant_started_at IS NULL AND assistant_content IS NULL AND "
            "assistant_content_hash IS NULL AND reason_code IS NULL AND "
            "completed_at IS NULL) OR "
            "(assistant_status = 'running' AND assistant_attempt_count >= 1 AND "
            "assistant_started_at IS NOT NULL AND assistant_content IS NULL AND "
            "assistant_content_hash IS NULL AND reason_code IS NULL AND "
            "completed_at IS NULL) OR "
            "(assistant_status = 'completed' AND assistant_content IS NOT NULL AND "
            "assistant_content_hash IS NOT NULL AND reason_code IS NULL AND "
            "completed_at IS NOT NULL) OR "
            "(assistant_status IN ('unavailable', 'failed') AND "
            "assistant_content IS NULL AND assistant_content_hash IS NULL AND "
            "reason_code IS NOT NULL AND completed_at IS NOT NULL)"
        ),
    )
    op.create_index(
        "ix_hc_manual_question_creations_status",
        "hc_manual_question_creations",
        ["status", "updated_at"],
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_hc_manual_question_creation_active_parent "
        "ON hc_manual_question_creations (quest_ref, parent_question_ref) "
        "WHERE status IN ('draft', 'seed_confirmed', 'research_pending', "
        "'research_ready', 'confirmed', 'recovering')"
    )

    op.create_table(
        "hc_manual_question_commands",
        sa.Column("idempotency_key", sa.String(length=128), primary_key=True),
        sa.Column("context_ref", sa.String(length=64), nullable=False),
        sa.Column("command_kind", sa.String(length=48), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("result_ref", sa.String(length=64), nullable=True),
        sa.Column("recorded_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["context_ref"], ["hc_manual_question_creations.context_ref"]
        ),
        _hash("request_hash"),
    )

    op.create_table(
        "hc_manual_deepfetch_requests",
        sa.Column("request_ref", sa.String(length=64), primary_key=True),
        sa.Column("context_ref", sa.String(length=64), nullable=False),
        sa.Column("initialization_id", sa.String(length=64), nullable=False),
        sa.Column("quest_ref", sa.String(length=64), nullable=False),
        sa.Column("parent_question_ref", sa.String(length=64), nullable=False),
        sa.Column("correlation_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("quest_draft_revision", sa.Integer(), nullable=False),
        sa.Column("quest_draft_hash", sa.String(length=64), nullable=False),
        sa.Column("context_basis_hash", sa.String(length=64), nullable=False),
        sa.Column("seed_hash", sa.String(length=64), nullable=False),
        sa.Column("scope_json", sa.Text(), nullable=False),
        sa.Column("scope_hash", sa.String(length=64), nullable=False),
        sa.Column("material_bindings_json", sa.Text(), nullable=False),
        sa.Column("material_bindings_hash", sa.String(length=64), nullable=False),
        sa.Column("resource_envelope_ref", sa.String(length=64), nullable=False),
        sa.Column("resource_envelope_hash", sa.String(length=64), nullable=False),
        sa.Column("acquisition_session_ref", sa.String(length=64), nullable=False),
        sa.Column("acquisition_config_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "acquisition_runtime_binding_hash", sa.String(length=64), nullable=False
        ),
        sa.Column("authorization_receipt_ref", sa.String(length=64), nullable=False),
        sa.Column("authorization_hash", sa.String(length=64), nullable=False),
        sa.Column("result_route", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("run_ref", sa.String(length=64), nullable=True),
        sa.Column("snapshot_ref", sa.String(length=64), nullable=True),
        sa.Column("failure_code", sa.String(length=96), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("completed_at", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(
            ["context_ref"], ["hc_manual_question_creations.context_ref"]
        ),
        _hash("seed_hash"),
        _hash("quest_draft_hash"),
        _hash("context_basis_hash"),
        _hash("scope_hash"),
        _hash("material_bindings_hash"),
        _hash("resource_envelope_hash"),
        _hash("acquisition_config_hash"),
        _hash("acquisition_runtime_binding_hash"),
        _hash("authorization_hash"),
        sa.CheckConstraint("generation >= 1"),
        sa.CheckConstraint("quest_draft_revision >= 1"),
        sa.CheckConstraint(
            "result_route = 'same_manual_question_creation_proposal'"
        ),
        sa.CheckConstraint("status IN ('queued', 'succeeded', 'failed', 'cancelled')"),
        sa.CheckConstraint(
            "(status = 'queued' AND snapshot_ref IS NULL AND failure_code IS NULL "
            "AND completed_at IS NULL) OR "
            "(status = 'succeeded' AND run_ref IS NOT NULL AND snapshot_ref IS NOT "
            "NULL AND failure_code IS NULL AND completed_at IS NOT NULL) OR "
            "(status IN ('failed', 'cancelled') AND snapshot_ref IS NULL AND "
            "failure_code IS NOT NULL AND completed_at IS NOT NULL)"
        ),
    )
    op.create_index(
        "ix_hc_manual_deepfetch_requests_queue",
        "hc_manual_deepfetch_requests",
        ["status", "created_at"],
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_hc_manual_deepfetch_request_active "
        "ON hc_manual_deepfetch_requests (context_ref) WHERE status = 'queued'"
    )

    op.create_table(
        "rm_manual_question_contents",
        sa.Column("content_ref", sa.String(length=64), primary_key=True),
        sa.Column("context_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("quest_ref", sa.String(length=64), nullable=False),
        sa.Column("parent_question_ref", sa.String(length=64), nullable=False),
        sa.Column("proposal_ref", sa.String(length=64), nullable=False),
        sa.Column("proposal_hash", sa.String(length=64), nullable=False),
        sa.Column("confirmation_ref", sa.String(length=64), nullable=False),
        sa.Column("confirmation_hash", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("schema_ref", sa.String(length=80), nullable=False),
        sa.Column("content_json", sa.Text(), nullable=False),
        sa.Column("object_path", sa.Text(), nullable=False),
        sa.Column("receipt_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        _hash("proposal_hash"),
        _hash("confirmation_hash"),
        _hash("content_hash"),
        _hash("receipt_hash"),
    )

    op.create_table(
        "rg_manual_questions",
        sa.Column("question_ref", sa.String(length=64), primary_key=True),
        sa.Column("context_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("quest_ref", sa.String(length=64), nullable=False),
        sa.Column("parent_question_ref", sa.String(length=64), nullable=False),
        sa.Column("parent_question_receipt_ref", sa.String(length=64), nullable=False),
        sa.Column("parent_question_receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("content_ref", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("schema_ref", sa.String(length=80), nullable=False),
        sa.Column("content_receipt_ref", sa.String(length=64), nullable=False),
        sa.Column("content_receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("proposal_ref", sa.String(length=64), nullable=False),
        sa.Column("proposal_hash", sa.String(length=64), nullable=False),
        sa.Column("confirmation_ref", sa.String(length=64), nullable=False),
        sa.Column("confirmation_hash", sa.String(length=64), nullable=False),
        sa.Column("receipt_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        _hash("parent_question_receipt_hash"),
        _hash("content_hash"),
        _hash("content_receipt_hash"),
        _hash("proposal_hash"),
        _hash("confirmation_hash"),
        _hash("receipt_hash"),
    )
    op.create_index(
        "ix_rg_manual_questions_parent",
        "rg_manual_questions",
        ["quest_ref", "parent_question_ref", "accepted_at"],
    )


def downgrade() -> None:
    raise RuntimeError("vNext production migrations are forward-only")
