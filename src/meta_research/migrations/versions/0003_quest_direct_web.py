"""Add the durable production Web workflow around direct Quest initialization.

Revision ID: 0003_quest_direct_web
Revises: 0002_quest_initialization
Create Date: 2026-08-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0003_quest_direct_web"
down_revision = "0002_quest_initialization"
branch_labels = None
depends_on = None


LEGACY_DRAFT_SCHEMA = "meta-research/quest-initialization-draft/v1"
LEGACY_PROPOSAL_SCHEMA = "meta-research/question-proposal/v1"


def upgrade() -> None:
    connection = op.get_bind()
    active_count = connection.execute(
        sa.text(
            "SELECT count(*) FROM hc_quest_initializations "
            "WHERE status IN ('draft', 'proposal_ready', 'confirmed')"
        )
    ).scalar_one()
    if int(active_count) > 1:
        raise RuntimeError(
            "cannot enforce one active Quest initialization: multiple nonterminal "
            "initializations already exist"
        )

    op.add_column(
        "hc_quest_initializations",
        sa.Column(
            "draft_schema_ref",
            sa.String(length=96),
            nullable=False,
            server_default=LEGACY_DRAFT_SCHEMA,
        ),
    )
    op.add_column(
        "hc_quest_draft_revisions",
        sa.Column(
            "draft_schema_ref",
            sa.String(length=96),
            nullable=False,
            server_default=LEGACY_DRAFT_SCHEMA,
        ),
    )
    op.add_column(
        "hc_question_proposals",
        sa.Column(
            "schema_ref",
            sa.String(length=96),
            nullable=False,
            server_default=LEGACY_PROPOSAL_SCHEMA,
        ),
    )

    op.create_table(
        "ar_host_capability_snapshots",
        sa.Column("snapshot_ref", sa.String(length=64), primary_key=True),
        sa.Column(
            "idempotency_key", sa.String(length=128), nullable=False, unique=True
        ),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("adapter_kind", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("capabilities_json", sa.Text(), nullable=False),
        sa.Column("capabilities_hash", sa.String(length=64), nullable=False),
        sa.Column("reason_code", sa.String(length=96), nullable=True),
        sa.Column("observed_at", sa.Float(), nullable=False),
        sa.CheckConstraint("status IN ('ready', 'unavailable')"),
        sa.CheckConstraint("length(capabilities_hash) = 64"),
        sa.CheckConstraint("length(request_hash) = 64"),
        sa.CheckConstraint(
            "(status = 'ready' AND reason_code IS NULL) OR "
            "(status = 'unavailable' AND reason_code IS NOT NULL)"
        ),
    )
    op.create_index(
        "ix_ar_host_capability_snapshots_observed",
        "ar_host_capability_snapshots",
        ["observed_at"],
    )
    op.create_table(
        "ar_host_compute_observation_claims",
        sa.Column("idempotency_key", sa.String(length=128), primary_key=True),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("claim_token", sa.String(length=64), nullable=False, unique=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("claimed_at", sa.Float(), nullable=False),
        sa.Column("lease_expires_at", sa.Float(), nullable=False),
        sa.CheckConstraint("length(request_hash) = 64"),
        sa.CheckConstraint("attempt_count >= 1"),
        sa.CheckConstraint("lease_expires_at > claimed_at"),
    )
    op.create_index(
        "ix_ar_host_compute_observation_claims_lease",
        "ar_host_compute_observation_claims",
        ["lease_expires_at"],
    )

    op.create_table(
        "hc_resource_envelopes",
        sa.Column("envelope_ref", sa.String(length=64), primary_key=True),
        sa.Column("initialization_id", sa.String(length=64), nullable=False),
        sa.Column("draft_revision", sa.Integer(), nullable=False),
        sa.Column("draft_hash", sa.String(length=64), nullable=False),
        sa.Column("host_snapshot_ref", sa.String(length=64), nullable=False),
        sa.Column("host_snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("envelope_json", sa.Text(), nullable=False),
        sa.Column("envelope_hash", sa.String(length=64), nullable=False),
        sa.Column("recorded_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["initialization_id", "draft_revision"],
            [
                "hc_quest_draft_revisions.initialization_id",
                "hc_quest_draft_revisions.revision",
            ],
        ),
        sa.UniqueConstraint("initialization_id", "draft_revision"),
        sa.CheckConstraint("draft_revision >= 1"),
        sa.CheckConstraint("length(draft_hash) = 64"),
        sa.CheckConstraint("length(host_snapshot_hash) = 64"),
        sa.CheckConstraint("length(envelope_hash) = 64"),
    )

    op.create_table(
        "hc_intent_drafting_sessions",
        sa.Column("session_ref", sa.String(length=64), primary_key=True),
        sa.Column(
            "initialization_id", sa.String(length=64), nullable=False, unique=True
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["initialization_id"],
            ["hc_quest_initializations.initialization_id"],
        ),
        sa.CheckConstraint("status IN ('open', 'closed')"),
    )
    op.create_table(
        "hc_intent_drafting_turns",
        sa.Column("turn_ref", sa.String(length=64), primary_key=True),
        sa.Column("session_ref", sa.String(length=64), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column(
            "idempotency_key", sa.String(length=128), nullable=False, unique=True
        ),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("basis_revision", sa.Integer(), nullable=False),
        sa.Column("basis_hash", sa.String(length=64), nullable=False),
        sa.Column("user_content", sa.Text(), nullable=False),
        sa.Column("user_content_hash", sa.String(length=64), nullable=False),
        sa.Column("assistant_status", sa.String(length=24), nullable=False),
        sa.Column(
            "assistant_attempt_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("assistant_content", sa.Text(), nullable=True),
        sa.Column("assistant_content_hash", sa.String(length=64), nullable=True),
        sa.Column("adapter_metadata_json", sa.Text(), nullable=True),
        sa.Column("adapter_metadata_hash", sa.String(length=64), nullable=True),
        sa.Column("reason_code", sa.String(length=96), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("assistant_started_at", sa.Float(), nullable=True),
        sa.Column("completed_at", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(
            ["session_ref"], ["hc_intent_drafting_sessions.session_ref"]
        ),
        sa.UniqueConstraint("session_ref", "ordinal"),
        sa.CheckConstraint("ordinal >= 1"),
        sa.CheckConstraint("basis_revision >= 1"),
        sa.CheckConstraint("assistant_attempt_count >= 0"),
        sa.CheckConstraint("length(request_hash) = 64"),
        sa.CheckConstraint("length(basis_hash) = 64"),
        sa.CheckConstraint("length(user_content_hash) = 64"),
        sa.CheckConstraint("length(trim(user_content)) > 0"),
        sa.CheckConstraint(
            "assistant_status IN "
            "('queued', 'running', 'completed', 'unavailable', 'failed')"
        ),
        sa.CheckConstraint(
            "(assistant_content IS NULL AND assistant_content_hash IS NULL) OR "
            "(assistant_content IS NOT NULL AND assistant_content_hash IS NOT NULL "
            "AND length(assistant_content_hash) = 64)"
        ),
        sa.CheckConstraint(
            "(adapter_metadata_json IS NULL AND adapter_metadata_hash IS NULL) OR "
            "(adapter_metadata_json IS NOT NULL AND adapter_metadata_hash IS NOT NULL "
            "AND length(adapter_metadata_hash) = 64)"
        ),
        sa.CheckConstraint(
            "(assistant_status IN ('queued', 'running') "
            "AND assistant_content IS NULL AND reason_code IS NULL "
            "AND completed_at IS NULL) OR "
            "(assistant_status = 'completed' AND assistant_content IS NOT NULL "
            "AND reason_code IS NULL AND completed_at IS NOT NULL) OR "
            "(assistant_status IN ('unavailable', 'failed') "
            "AND assistant_content IS NULL AND reason_code IS NOT NULL "
            "AND completed_at IS NOT NULL)"
        ),
    )

    op.create_table(
        "hc_proposal_generation_attempts",
        sa.Column("generation_ref", sa.String(length=64), primary_key=True),
        sa.Column("initialization_id", sa.String(length=64), nullable=False),
        sa.Column(
            "idempotency_key", sa.String(length=128), nullable=False, unique=True
        ),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("route", sa.String(length=16), nullable=False),
        sa.Column("basis_revision", sa.Integer(), nullable=False),
        sa.Column("basis_hash", sa.String(length=64), nullable=False),
        sa.Column("starting_proposal_revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("adapter_kind", sa.String(length=80), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("proposal_ref", sa.String(length=64), nullable=True),
        sa.Column("proposal_hash", sa.String(length=64), nullable=True),
        sa.Column("failure_code", sa.String(length=96), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("started_at", sa.Float(), nullable=True),
        sa.Column("completed_at", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(
            ["initialization_id", "basis_revision"],
            [
                "hc_quest_draft_revisions.initialization_id",
                "hc_quest_draft_revisions.revision",
            ],
        ),
        sa.ForeignKeyConstraint(
            ["proposal_ref"], ["hc_question_proposals.proposal_ref"]
        ),
        sa.CheckConstraint("route IN ('direct', 'deepfetch')"),
        sa.CheckConstraint("basis_revision >= 1"),
        sa.CheckConstraint("starting_proposal_revision >= 0"),
        sa.CheckConstraint("attempt_count >= 0"),
        sa.CheckConstraint("length(request_hash) = 64"),
        sa.CheckConstraint("length(basis_hash) = 64"),
        sa.CheckConstraint(
            "status IN "
            "('queued', 'running', 'succeeded', 'capability_unavailable', 'failed')"
        ),
        sa.CheckConstraint(
            "(proposal_ref IS NULL AND proposal_hash IS NULL) OR "
            "(proposal_ref IS NOT NULL AND proposal_hash IS NOT NULL "
            "AND length(proposal_hash) = 64)"
        ),
        sa.CheckConstraint(
            "(status = 'queued' AND proposal_ref IS NULL AND failure_code IS NULL "
            "AND started_at IS NULL AND completed_at IS NULL) OR "
            "(status = 'running' AND proposal_ref IS NULL AND failure_code IS NULL "
            "AND started_at IS NOT NULL AND completed_at IS NULL) OR "
            "(status = 'succeeded' AND proposal_ref IS NOT NULL "
            "AND failure_code IS NULL AND started_at IS NOT NULL "
            "AND completed_at IS NOT NULL) OR "
            "(status IN ('capability_unavailable', 'failed') "
            "AND proposal_ref IS NULL AND failure_code IS NOT NULL "
            "AND completed_at IS NOT NULL)"
        ),
    )
    op.create_index(
        "ix_hc_proposal_generation_attempts_queue",
        "hc_proposal_generation_attempts",
        ["status", "created_at"],
    )

    op.create_table(
        "hc_confirmation_preview_bindings",
        sa.Column("preview_ref", sa.String(length=64), primary_key=True),
        sa.Column("schema_ref", sa.String(length=96), nullable=False),
        sa.Column("resource_envelope_ref", sa.String(length=64), nullable=False),
        sa.Column("resource_envelope_hash", sa.String(length=64), nullable=False),
        sa.Column("owner_revisions_json", sa.Text(), nullable=False),
        sa.Column("owner_revisions_hash", sa.String(length=64), nullable=False),
        sa.Column("feed_revision", sa.Integer(), nullable=False),
        sa.Column("summary_json", sa.Text(), nullable=False),
        sa.Column("summary_hash", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["preview_ref"], ["hc_confirmation_previews.preview_ref"]
        ),
        sa.ForeignKeyConstraint(
            ["resource_envelope_ref"], ["hc_resource_envelopes.envelope_ref"]
        ),
        sa.CheckConstraint("length(resource_envelope_hash) = 64"),
        sa.CheckConstraint("length(owner_revisions_hash) = 64"),
        sa.CheckConstraint("length(summary_hash) = 64"),
        sa.CheckConstraint("feed_revision >= 0"),
    )

    op.create_table(
        "hc_reconciliation_checkpoints",
        sa.Column("initialization_id", sa.String(length=64), primary_key=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("first_missing_step", sa.String(length=32), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(length=96), nullable=True),
        sa.Column("next_retry_at", sa.Float(), nullable=True),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["initialization_id"],
            ["hc_quest_initializations.initialization_id"],
        ),
        sa.CheckConstraint("state IN ('idle', 'partial', 'recovering', 'completed')"),
        sa.CheckConstraint(
            "first_missing_step IS NULL OR first_missing_step IN "
            "('quest_goal', 'question_content', 'question_identity', "
            "'cycle_activation')"
        ),
        sa.CheckConstraint("attempt_count >= 0"),
        sa.CheckConstraint(
            "(state IN ('idle', 'completed') AND first_missing_step IS NULL "
            "AND reason_code IS NULL AND next_retry_at IS NULL) OR "
            "(state IN ('partial', 'recovering') "
            "AND first_missing_step IS NOT NULL AND reason_code IS NOT NULL)"
        ),
    )
    op.create_index(
        "ix_hc_reconciliation_checkpoints_due",
        "hc_reconciliation_checkpoints",
        ["state", "next_retry_at"],
    )
    op.create_table(
        "hc_reconciliation_attempts",
        sa.Column("attempt_ref", sa.String(length=64), primary_key=True),
        sa.Column("initialization_id", sa.String(length=64), nullable=False),
        sa.Column("step", sa.String(length=32), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(length=24), nullable=False),
        sa.Column("reason_code", sa.String(length=96), nullable=True),
        sa.Column("started_at", sa.Float(), nullable=False),
        sa.Column("finished_at", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(
            ["initialization_id"],
            ["hc_quest_initializations.initialization_id"],
        ),
        sa.UniqueConstraint("initialization_id", "step", "attempt_number"),
        sa.CheckConstraint(
            "step IN ('quest_goal', 'question_content', 'question_identity', "
            "'cycle_activation')"
        ),
        sa.CheckConstraint("attempt_number >= 1"),
        sa.CheckConstraint(
            "outcome IN "
            "('started', 'accepted', 'transient_failure', 'rejected', 'stale')"
        ),
        sa.CheckConstraint(
            "(outcome = 'started' AND reason_code IS NULL AND finished_at IS NULL) OR "
            "(outcome = 'accepted' AND reason_code IS NULL "
            "AND finished_at IS NOT NULL) OR "
            "(outcome IN ('transient_failure', 'rejected', 'stale') "
            "AND reason_code IS NOT NULL AND finished_at IS NOT NULL)"
        ),
    )

    op.execute(
        sa.text(
            "CREATE UNIQUE INDEX uq_hc_quest_initializations_one_nonterminal "
            "ON hc_quest_initializations ((1)) "
            "WHERE status IN ('draft', 'proposal_ready', 'confirmed')"
        )
    )


def downgrade() -> None:
    raise RuntimeError("vNext production migrations are forward-only")
