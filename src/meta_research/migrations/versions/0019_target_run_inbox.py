"""Add durable TargetRun lifecycle, handoff manifests, and Bundle Inbox.

Revision ID: 0019_target_run_inbox
Revises: 0018_target_launch_admission
Create Date: 2026-08-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0019_target_run_inbox"
down_revision = "0018_target_launch_admission"
branch_labels = None
depends_on = None


def _hash(name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"length({name}) = 64")


def upgrade() -> None:
    # 0018 deliberately admitted no worker and therefore allowed only an empty
    # or running frontier.  Preserve any future running projection while
    # widening the same authoritative table to the fixed running/terminal
    # lifecycle; do not introduce a second competing frontier.
    op.rename_table("ar_target_frontier_entries", "ar_target_frontier_entries_0018")
    op.create_table(
        "ar_target_frontier_entries",
        sa.Column("target_ref", sa.String(96), primary_key=True),
        sa.Column("launch_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("target_spec_content_hash_ref", sa.String(256), nullable=False),
        sa.Column("target_spec_receipt_ref", sa.String(96), nullable=False),
        sa.Column("target_spec_receipt_subject_ref", sa.String(256), nullable=False),
        sa.Column("state_revision", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("current_handle_json", sa.Text(), nullable=False),
        sa.Column("current_handle_hash", sa.String(64), nullable=False),
        sa.Column("terminal_fact_ref", sa.String(96), nullable=True),
        sa.Column("currentness_known", sa.Boolean(), nullable=False),
        sa.Column("current", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["target_ref"], ["rg_targets.target_ref"]),
        sa.ForeignKeyConstraint(["launch_ref"], ["ar_target_launches.launch_ref"]),
        sa.CheckConstraint("state_revision >= 1"),
        sa.CheckConstraint("state IN ('running', 'terminal')"),
        sa.CheckConstraint(
            "(state = 'running' AND terminal_fact_ref IS NULL) OR "
            "(state = 'terminal' AND terminal_fact_ref IS NOT NULL)"
        ),
        sa.CheckConstraint("currentness_known = 1 AND current = 1"),
        _hash("current_handle_hash"),
    )
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "INSERT INTO ar_target_frontier_entries SELECT * FROM "
            "ar_target_frontier_entries_0018"
        )
    )
    op.drop_table("ar_target_frontier_entries_0018")

    # The launch acknowledgement stays opaque and immutable.  Activation is a
    # separate durable fact because a valid preflight and real Harness-owned
    # Session/Attempt/Fence must all exist before protected execution starts.
    op.create_table(
        "ar_target_run_activations",
        sa.Column("activation_ref", sa.String(96), primary_key=True),
        sa.Column("target_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("launch_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("target_run_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("root_session_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("execution_attempt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("execution_fence_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("initial_handle_json", sa.Text(), nullable=False),
        sa.Column("initial_handle_hash", sa.String(64), nullable=False),
        sa.Column("candidate_json", sa.Text(), nullable=False),
        sa.Column("candidate_hash", sa.String(64), nullable=False),
        sa.Column("formal_plan_json", sa.Text(), nullable=False),
        sa.Column("formal_plan_hash", sa.String(64), nullable=False),
        sa.Column("initial_preflight_json", sa.Text(), nullable=False),
        sa.Column("initial_preflight_hash", sa.String(64), nullable=False),
        sa.Column("initial_review_scope_json", sa.Text(), nullable=False),
        sa.Column("initial_review_scope_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("activated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["target_ref"], ["rg_targets.target_ref"]),
        sa.ForeignKeyConstraint(["launch_ref"], ["ar_target_launches.launch_ref"]),
        *(
            _hash(name)
            for name in (
                "initial_handle_hash",
                "candidate_hash",
                "formal_plan_hash",
                "initial_preflight_hash",
                "initial_review_scope_hash",
                "request_hash",
            )
        ),
    )

    # Append-only handle and preflight histories make recovery and restart
    # reconstruction independent of process memory or a parent transcript.
    op.create_table(
        "ar_target_run_handles",
        sa.Column("target_ref", sa.String(96), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("target_run_ref", sa.String(96), nullable=False),
        sa.Column("root_session_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("execution_attempt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("execution_fence_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("handle_json", sa.Text(), nullable=False),
        sa.Column("handle_hash", sa.String(64), nullable=False),
        sa.Column("recorded_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["target_ref"], ["ar_target_run_activations.target_ref"]),
        sa.PrimaryKeyConstraint("target_ref", "ordinal"),
        sa.UniqueConstraint("target_ref", "target_run_ref", "ordinal"),
        sa.CheckConstraint("ordinal >= 1"),
        _hash("handle_hash"),
    )
    op.create_table(
        "ar_target_run_identities",
        sa.Column("target_run_ref", sa.String(96), primary_key=True),
        sa.Column("target_ref", sa.String(96), nullable=False),
        sa.Column("first_handle_ordinal", sa.Integer(), nullable=False),
        sa.Column("recorded_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["target_ref"], ["ar_target_run_activations.target_ref"]
        ),
        sa.CheckConstraint("first_handle_ordinal >= 1"),
    )
    op.create_table(
        "ar_target_run_preflights",
        sa.Column("target_ref", sa.String(96), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("implementation_revision_ref", sa.String(96), nullable=False),
        sa.Column("preflight_json", sa.Text(), nullable=False),
        sa.Column("preflight_hash", sa.String(64), nullable=False),
        sa.Column("review_scope_json", sa.Text(), nullable=False),
        sa.Column("review_scope_hash", sa.String(64), nullable=False),
        sa.Column("recorded_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["target_ref"], ["ar_target_run_activations.target_ref"]),
        sa.PrimaryKeyConstraint("target_ref", "ordinal"),
        sa.UniqueConstraint("target_ref", "implementation_revision_ref"),
        sa.CheckConstraint("ordinal >= 1"),
        _hash("preflight_hash"),
        _hash("review_scope_hash"),
    )
    op.create_table(
        "ar_target_monitor_states",
        sa.Column("target_ref", sa.String(96), primary_key=True),
        sa.Column("target_run_ref", sa.String(96), nullable=False),
        sa.Column("execution_attempt_ref", sa.String(96), nullable=False),
        sa.Column("execution_fence_ref", sa.String(96), nullable=False),
        sa.Column("snapshot_required", sa.Boolean(), nullable=False),
        sa.Column("cursor", sa.Integer(), nullable=True),
        sa.Column("status_revision", sa.Integer(), nullable=True),
        sa.Column("checkpoint_revision", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["target_ref"], ["ar_target_run_activations.target_ref"]),
        sa.CheckConstraint("checkpoint_revision >= 1"),
        sa.CheckConstraint(
            "(snapshot_required = 1 AND cursor IS NULL AND status_revision IS NULL) OR "
            "(snapshot_required = 0 AND cursor IS NOT NULL AND cursor >= 0 AND "
            "status_revision IS NOT NULL AND status_revision >= 0)"
        ),
    )
    op.create_table(
        "ar_target_stop_decisions",
        sa.Column("decision_ref", sa.String(96), primary_key=True),
        sa.Column("target_ref", sa.String(96), nullable=False),
        sa.Column("target_run_ref", sa.String(96), nullable=False),
        sa.Column("execution_attempt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("stop_json", sa.Text(), nullable=False),
        sa.Column("stop_hash", sa.String(64), nullable=False),
        sa.Column("recorded_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["target_ref"], ["ar_target_run_activations.target_ref"]),
        _hash("stop_hash"),
    )
    op.create_table(
        "ar_target_monitor_commands",
        sa.Column("idempotency_key", sa.String(128), primary_key=True),
        sa.Column("target_ref", sa.String(96), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("cursor", sa.Integer(), nullable=False),
        sa.Column("status_revision", sa.Integer(), nullable=False),
        sa.Column("recorded_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["target_ref"], ["ar_target_run_activations.target_ref"]),
        sa.CheckConstraint("cursor >= 0 AND status_revision >= 0"),
        _hash("request_hash"),
    )

    op.create_table(
        "ar_target_run_recoveries",
        sa.Column("transition_ref", sa.String(96), primary_key=True),
        sa.Column("target_ref", sa.String(96), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("blocker_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("old_execution_attempt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("new_execution_attempt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("blocker_json", sa.Text(), nullable=False),
        sa.Column("blocker_hash", sa.String(64), nullable=False),
        sa.Column("recovery_evidence_refs_json", sa.Text(), nullable=False),
        sa.Column("recovery_evidence_refs_hash", sa.String(64), nullable=False),
        sa.Column("replacement_preflight_ordinal", sa.Integer(), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("recovered_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["target_ref"], ["ar_target_run_activations.target_ref"]),
        sa.UniqueConstraint("target_ref", "ordinal"),
        sa.CheckConstraint("ordinal >= 1"),
        *(
            _hash(name)
            for name in (
                "blocker_hash",
                "recovery_evidence_refs_hash",
                "request_hash",
            )
        ),
    )
    op.create_table(
        "ar_target_retired_identities",
        sa.Column("identity_ref", sa.String(96), primary_key=True),
        sa.Column("identity_kind", sa.String(24), nullable=False),
        sa.Column("target_ref", sa.String(96), nullable=False),
        sa.Column("transition_ref", sa.String(96), nullable=False),
        sa.Column("retired_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["target_ref"], ["ar_target_run_activations.target_ref"]),
        sa.ForeignKeyConstraint(["transition_ref"], ["ar_target_run_recoveries.transition_ref"]),
        sa.CheckConstraint(
            "identity_kind IN ('target_run', 'root_session', "
            "'execution_attempt', 'execution_fence')"
        ),
    )

    op.create_table(
        "ar_target_handoff_manifests",
        sa.Column("manifest_ref", sa.String(96), primary_key=True),
        sa.Column("target_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("handoff_json", sa.Text(), nullable=False),
        sa.Column("handoff_hash", sa.String(64), nullable=False),
        sa.Column("semantic_barrier_fact_ref", sa.String(96), nullable=True, unique=True),
        sa.Column("published_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["target_ref"], ["ar_target_run_activations.target_ref"]),
        _hash("handoff_hash"),
    )
    op.create_table(
        "ar_target_work_notices",
        sa.Column("notice_ref", sa.String(96), primary_key=True),
        sa.Column("sequence", sa.Integer(), nullable=False, unique=True),
        sa.Column("terminal_transition_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("target_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("manifest_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("notice_json", sa.Text(), nullable=False),
        sa.Column("notice_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("published_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["target_ref"], ["ar_target_run_activations.target_ref"]),
        sa.ForeignKeyConstraint(["manifest_ref"], ["ar_target_handoff_manifests.manifest_ref"]),
        sa.CheckConstraint("sequence >= 1"),
        sa.CheckConstraint(
            "kind IN ('target_completed', 'coordination_required', "
            "'semantic_change_required')"
        ),
        _hash("notice_hash"),
        _hash("request_hash"),
    )
    op.create_table(
        "ar_bundle_inbox_state",
        sa.Column("singleton", sa.String(16), primary_key=True),
        sa.Column("next_sequence", sa.Integer(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("wake_pending", sa.Boolean(), nullable=False),
        sa.CheckConstraint("singleton = 'bundle'"),
        sa.CheckConstraint("next_sequence >= 1"),
        sa.CheckConstraint("generation >= 0"),
    )
    connection.execute(
        sa.text(
            "INSERT INTO ar_bundle_inbox_state (singleton, next_sequence, "
            "generation, wake_pending) VALUES ('bundle', 1, 0, 0)"
        )
    )


def downgrade() -> None:
    raise RuntimeError("vNext production migrations are forward-only")
