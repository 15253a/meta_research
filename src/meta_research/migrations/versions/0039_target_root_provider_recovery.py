"""Add native Target-root provider-ceiling recovery history.

Revision ID: 0039_target_root_provider_recovery
Revises: 0038_deepfetch_binding_audit
Create Date: 2026-08-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0039_target_root_provider_recovery"
down_revision = "0038_deepfetch_binding_audit"
branch_labels = None
depends_on = None


def _hash(name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"length({name}) = 64")


def upgrade() -> None:
    # Root-owned Targets deliberately do not create the legacy preflight
    # activation.  Their successor history therefore has its own FK root and
    # cannot be mixed with ar_target_run_recoveries.
    op.create_table(
        "ar_target_root_handle_history",
        sa.Column("target_ref", sa.String(96), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("target_run_ref", sa.String(96), nullable=False),
        sa.Column("root_session_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("execution_attempt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("execution_fence_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("handle_json", sa.Text(), nullable=False),
        sa.Column("handle_hash", sa.String(64), nullable=False),
        sa.Column("recorded_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["target_ref"], ["ar_target_root_lifecycles.target_ref"]
        ),
        sa.PrimaryKeyConstraint("target_ref", "ordinal"),
        sa.UniqueConstraint("target_ref", "target_run_ref", "ordinal"),
        sa.CheckConstraint("ordinal >= 1"),
        _hash("handle_hash"),
    )
    op.create_table(
        "ar_target_root_provider_recoveries",
        sa.Column("transition_ref", sa.String(96), primary_key=True),
        sa.Column("recovery_ref", sa.String(128), nullable=False, unique=True),
        sa.Column("target_ref", sa.String(96), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("blocker_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("old_execution_attempt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("new_execution_attempt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("retired_workspace_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("failed_provider_operation_ref", sa.String(128), nullable=False, unique=True),
        sa.Column("failure_code", sa.String(96), nullable=False),
        sa.Column("blocker_json", sa.Text(), nullable=False),
        sa.Column("blocker_hash", sa.String(64), nullable=False),
        sa.Column("transport_receipt_json", sa.Text(), nullable=False),
        sa.Column("transport_receipt_hash", sa.String(64), nullable=False),
        sa.Column("provider_evidence_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("provider_evidence_json", sa.Text(), nullable=False),
        sa.Column("provider_evidence_json_hash", sa.String(64), nullable=False),
        sa.Column("provider_evidence_hash", sa.String(64), nullable=False),
        sa.Column("successor_reservation_json", sa.Text(), nullable=False),
        sa.Column("successor_reservation_hash", sa.String(64), nullable=False),
        sa.Column("recovery_evidence_refs_json", sa.Text(), nullable=False),
        sa.Column("recovery_evidence_refs_hash", sa.String(64), nullable=False),
        # These three columns make the history-reader branch explicit.  Native
        # root recovery never claims a generic execution binding.
        sa.Column("generic_binding_ref", sa.String(96), nullable=True),
        sa.Column("generic_binding_receipt_ref", sa.String(96), nullable=True),
        sa.Column("generic_binding_receipt_hash", sa.String(64), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("recovered_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["target_ref"], ["ar_target_root_lifecycles.target_ref"]
        ),
        sa.ForeignKeyConstraint(
            ["failed_provider_operation_ref"],
            ["ar_harness_provider_operations.operation_ref"],
        ),
        sa.ForeignKeyConstraint(
            ["retired_workspace_ref"],
            ["ar_target_run_workspaces.workspace_ref"],
        ),
        sa.UniqueConstraint("target_ref", "ordinal"),
        sa.CheckConstraint("ordinal >= 1"),
        sa.CheckConstraint(
            "failure_code IN ('provider_timeout', 'provider_output_limit', "
            "'provider_descendant_process')"
        ),
        sa.CheckConstraint(
            "generic_binding_ref IS NULL AND "
            "generic_binding_receipt_ref IS NULL AND "
            "generic_binding_receipt_hash IS NULL"
        ),
        *(
            _hash(name)
            for name in (
                "blocker_hash",
                "transport_receipt_hash",
                "provider_evidence_json_hash",
                "provider_evidence_hash",
                "successor_reservation_hash",
                "recovery_evidence_refs_hash",
                "request_hash",
            )
        ),
    )
    # Filesystem effects cannot share SQLite's transaction.  A recovered
    # workspace therefore remains invisible until this append-only receipt is
    # inserted after an exact, stable copy of its retired predecessor.  There
    # is deliberately no mutable ``complete`` flag that a partial copy could
    # accidentally inherit across restart.
    op.create_table(
        "ar_target_root_workspace_continuities",
        sa.Column("continuity_ref", sa.String(96), primary_key=True),
        sa.Column("transition_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("target_ref", sa.String(96), nullable=False),
        sa.Column(
            "predecessor_workspace_ref",
            sa.String(96),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "successor_workspace_ref",
            sa.String(96),
            nullable=False,
            unique=True,
        ),
        sa.Column("manifest_json", sa.Text(), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["transition_ref"],
            ["ar_target_root_provider_recoveries.transition_ref"],
        ),
        sa.ForeignKeyConstraint(
            ["target_ref"], ["ar_target_root_lifecycles.target_ref"]
        ),
        sa.ForeignKeyConstraint(
            ["predecessor_workspace_ref"],
            ["ar_target_run_workspaces.workspace_ref"],
        ),
        sa.ForeignKeyConstraint(
            ["successor_workspace_ref"],
            ["ar_target_run_workspaces.workspace_ref"],
        ),
        sa.CheckConstraint(
            "predecessor_workspace_ref <> successor_workspace_ref"
        ),
        *(
            _hash(name)
            for name in (
                "manifest_hash",
                "payload_hash",
                "request_hash",
                "receipt_hash",
            )
        ),
    )
    op.create_table(
        "ar_target_root_retired_identities",
        sa.Column("identity_ref", sa.String(96), primary_key=True),
        sa.Column("identity_kind", sa.String(24), nullable=False),
        sa.Column("target_ref", sa.String(96), nullable=False),
        sa.Column("transition_ref", sa.String(96), nullable=False),
        sa.Column("retired_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["target_ref"], ["ar_target_root_lifecycles.target_ref"]
        ),
        sa.ForeignKeyConstraint(
            ["transition_ref"],
            ["ar_target_root_provider_recoveries.transition_ref"],
        ),
        sa.CheckConstraint(
            "identity_kind IN ('root_session', 'execution_attempt', "
            "'execution_fence')"
        ),
    )

    # Existing root lifecycles have a complete immutable initial handle.  Make
    # it the first history row so upgrades preserve their exact identity.
    op.execute(
        sa.text(
            "INSERT INTO ar_target_root_handle_history (target_ref, ordinal, "
            "target_run_ref, root_session_ref, execution_attempt_ref, "
            "execution_fence_ref, handle_json, handle_hash, recorded_at) "
            "SELECT target_ref, 1, target_run_ref, root_session_ref, "
            "target_attempt_ref, target_fence_ref, initial_handle_json, "
            "initial_handle_hash, created_at FROM ar_target_root_lifecycles"
        )
    )


def downgrade() -> None:
    raise RuntimeError("vNext production migrations are forward-only")
