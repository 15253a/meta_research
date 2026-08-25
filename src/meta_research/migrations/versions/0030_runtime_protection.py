"""Add durable long-running responsibility and observability facts.

Revision ID: 0030_runtime_protection
Revises: 0029_target_root_lifecycle
Create Date: 2026-08-25
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op


revision = "0030_runtime_protection"
down_revision = "0029_target_root_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A reconciliation call is itself an effectful provider turn.  Persist its
    # generation so repeated unknown outcomes never reuse a responsibility
    # identity after a daemon incarnation change.
    op.add_column(
        "ar_harness_provider_operations",
        sa.Column(
            "reconciliation_generation",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )

    op.create_table(
        "ar_runtime_instances",
        sa.Column("incarnation_ref", sa.String(96), primary_key=True),
        sa.Column("boot_identity_hash", sa.String(64), nullable=False),
        sa.Column("process_identity_hash", sa.String(64), nullable=False),
        sa.Column("platform_kind", sa.String(48), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("reason_code", sa.String(96), nullable=True),
        sa.Column("started_at", sa.Float(), nullable=False),
        sa.Column("stopped_at", sa.Float(), nullable=True),
        sa.CheckConstraint("length(boot_identity_hash) = 64"),
        sa.CheckConstraint("length(process_identity_hash) = 64"),
        sa.CheckConstraint("status IN ('active', 'stopped', 'interrupted')"),
        sa.CheckConstraint(
            "(status = 'active' AND reason_code IS NULL AND stopped_at IS NULL) "
            "OR (status = 'stopped' AND stopped_at IS NOT NULL) OR "
            "(status = 'interrupted' AND reason_code IS NOT NULL AND "
            "stopped_at IS NOT NULL)"
        ),
    )

    # One epoch is the real OS-level hold shared by any number of attributable
    # responsibilities.  The responsibility rows, rather than a mutable naked
    # counter, are the source of truth for whether the epoch may be released.
    op.create_table(
        "ar_power_inhibitor_epochs",
        sa.Column("holder_ref", sa.String(96), primary_key=True),
        sa.Column("incarnation_ref", sa.String(96), nullable=False),
        sa.Column("backend", sa.String(64), nullable=False),
        sa.Column("scope", sa.String(64), nullable=False),
        sa.Column("native_holder_ref", sa.String(128), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("failure_code", sa.String(96), nullable=True),
        sa.Column("acquired_at", sa.Float(), nullable=True),
        sa.Column("released_at", sa.Float(), nullable=True),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["incarnation_ref"], ["ar_runtime_instances.incarnation_ref"]
        ),
        sa.CheckConstraint(
            "status IN ('acquiring', 'active', 'releasing', 'released', "
            "'failed', 'lost', 'release_pending')"
        ),
        sa.CheckConstraint(
            "(status = 'active' AND native_holder_ref IS NOT NULL AND "
            "acquired_at IS NOT NULL AND failure_code IS NULL AND "
            "released_at IS NULL) OR "
            "(status IN ('acquiring', 'releasing') AND failure_code IS NULL "
            "AND released_at IS NULL) OR "
            "(status IN ('failed', 'lost', 'release_pending') AND failure_code IS NOT NULL AND "
            "released_at IS NULL) OR "
            "(status = 'released' AND released_at IS NOT NULL)"
        ),
    )

    op.create_table(
        "ar_execution_responsibilities",
        sa.Column("responsibility_ref", sa.String(96), primary_key=True),
        sa.Column("incarnation_ref", sa.String(96), nullable=False),
        sa.Column("correlation_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("owner_scope", sa.String(48), nullable=False),
        sa.Column("root_run_ref", sa.String(128), nullable=False),
        sa.Column("attempt_ref", sa.String(128), nullable=True),
        sa.Column("fence_ref", sa.String(128), nullable=True),
        sa.Column("operation_ref", sa.String(128), nullable=False),
        sa.Column("effect_kind", sa.String(48), nullable=False),
        sa.Column("holder_ref", sa.String(96), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("boundary", sa.String(24), nullable=True),
        sa.Column("checkpoint_ref", sa.String(128), nullable=True),
        sa.Column("reason_code", sa.String(96), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("finished_at", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(
            ["holder_ref"], ["ar_power_inhibitor_epochs.holder_ref"]
        ),
        sa.ForeignKeyConstraint(
            ["incarnation_ref"], ["ar_runtime_instances.incarnation_ref"]
        ),
        sa.CheckConstraint(
            "status IN ('acquiring', 'active', 'waiting', 'interrupted', "
            "'finished')"
        ),
        sa.CheckConstraint(
            "boundary IS NULL OR boundary IN ('checkpoint', "
            "'permanent_fence', 'terminal')"
        ),
        sa.CheckConstraint(
            "(status = 'active' AND holder_ref IS NOT NULL AND "
            "reason_code IS NULL AND finished_at IS NULL) OR "
            "(status = 'acquiring' AND reason_code IS NULL AND "
            "finished_at IS NULL) OR "
            "(status IN ('waiting', 'interrupted') AND reason_code IS NOT NULL "
            "AND finished_at IS NULL) OR "
            "(status = 'finished' AND boundary IS NOT NULL AND "
            "finished_at IS NOT NULL)"
        ),
    )
    op.create_index(
        "ix_ar_execution_responsibilities_active",
        "ar_execution_responsibilities",
        ["status", "holder_ref"],
    )
    op.create_index(
        "ix_ar_execution_responsibilities_operation",
        "ar_execution_responsibilities",
        ["operation_ref", "created_at"],
    )

    # Capability is a diagnostic fact, not an Owner responsibility or a fake
    # receipt.  A row is scoped to one daemon incarnation while holder_ref keeps
    # an interrupted transient probe recoverable through the ordinary epoch
    # ledger.
    op.create_table(
        "ar_power_inhibitor_capabilities",
        sa.Column("incarnation_ref", sa.String(96), primary_key=True),
        sa.Column("holder_ref", sa.String(96), nullable=True),
        sa.Column("backend", sa.String(64), nullable=False),
        sa.Column("scope", sa.String(64), nullable=False),
        sa.Column("probe_status", sa.String(24), nullable=False),
        sa.Column("failure_code", sa.String(96), nullable=True),
        sa.Column("probed_at", sa.Float(), nullable=True),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["incarnation_ref"], ["ar_runtime_instances.incarnation_ref"]
        ),
        sa.CheckConstraint(
            "probe_status IN ('unprobed', 'probing', 'ready', 'unavailable')"
        ),
        sa.CheckConstraint(
            "(probe_status = 'unprobed' AND holder_ref IS NULL AND "
            "failure_code IS NULL AND probed_at IS NULL) OR "
            "(probe_status = 'probing' AND holder_ref IS NOT NULL AND "
            "failure_code IS NULL AND probed_at IS NULL) OR "
            "(probe_status = 'ready' AND holder_ref IS NOT NULL AND "
            "failure_code IS NULL AND probed_at IS NOT NULL) OR "
            "(probe_status = 'unavailable' AND holder_ref IS NOT NULL AND "
            "failure_code IS NOT NULL AND probed_at IS NOT NULL)"
        ),
    )

    op.create_table(
        "ar_runtime_boundary_receipts",
        sa.Column("responsibility_ref", sa.String(96), primary_key=True),
        sa.Column("owner_scope", sa.String(48), nullable=False),
        sa.Column("root_run_ref", sa.String(128), nullable=False),
        sa.Column("attempt_ref", sa.String(128), nullable=True),
        sa.Column("fence_ref", sa.String(128), nullable=True),
        sa.Column("operation_ref", sa.String(128), nullable=False),
        sa.Column("boundary", sa.String(24), nullable=False),
        sa.Column("checkpoint_ref", sa.String(128), nullable=True),
        sa.Column("owner_evidence_ref", sa.String(128), nullable=False),
        sa.Column("evidence_hash", sa.String(64), nullable=False),
        sa.Column("recorded_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["responsibility_ref"],
            ["ar_execution_responsibilities.responsibility_ref"],
        ),
        sa.CheckConstraint(
            "boundary IN ('checkpoint', 'permanent_fence', 'terminal')"
        ),
        sa.CheckConstraint(
            "(boundary = 'checkpoint' AND checkpoint_ref IS NOT NULL) OR "
            "(boundary IN ('permanent_fence', 'terminal') AND "
            "checkpoint_ref IS NULL)"
        ),
        sa.CheckConstraint("length(evidence_hash) = 64"),
    )

    op.create_table(
        "ar_runtime_interruptions",
        sa.Column("interruption_ref", sa.String(96), primary_key=True),
        sa.Column("responsibility_ref", sa.String(96), nullable=False),
        sa.Column("interruption_kind", sa.String(48), nullable=False),
        sa.Column("reason_code", sa.String(96), nullable=False),
        sa.Column("old_attempt_ref", sa.String(128), nullable=True),
        sa.Column("old_fence_ref", sa.String(128), nullable=True),
        sa.Column("operation_ref", sa.String(128), nullable=False),
        sa.Column("checkpoint_ref", sa.String(128), nullable=True),
        sa.Column("evidence_ref", sa.String(128), nullable=False),
        sa.Column("first_missing_boundary", sa.String(64), nullable=False),
        sa.Column("reconciliation_status", sa.String(24), nullable=False),
        sa.Column("recorded_at", sa.Float(), nullable=False),
        sa.Column("reconciled_at", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(
            ["responsibility_ref"],
            ["ar_execution_responsibilities.responsibility_ref"],
        ),
        sa.CheckConstraint(
            "reconciliation_status IN ('required', 'protected', 'completed', "
            "'terminal')"
        ),
    )

    op.create_table(
        "ar_runtime_observability_identity",
        sa.Column("singleton", sa.String(16), primary_key=True),
        sa.Column("correlation_ref", sa.String(96), nullable=False, unique=True),
        sa.CheckConstraint("singleton = 'runtime'"),
    )
    op.execute(
        "INSERT INTO ar_runtime_observability_identity "
        "(singleton, correlation_ref) VALUES ('runtime', "
        f"'runtime_correlation_{uuid.uuid4().hex}')"
    )

    op.create_table(
        "ar_runtime_telemetry_state",
        sa.Column("singleton", sa.String(16), primary_key=True),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("provider", sa.String(48), nullable=True),
        sa.Column("authorization_ref", sa.String(96), nullable=True),
        sa.Column("failure_code", sa.String(96), nullable=True),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.CheckConstraint("singleton = 'runtime'"),
        sa.CheckConstraint(
            "mode IN ('disabled', 'active', 'revocation_pending', 'revoked')"
        ),
        sa.CheckConstraint(
            "(mode = 'active' AND provider IS NOT NULL AND "
            "authorization_ref IS NOT NULL) OR "
            "(mode = 'revocation_pending' AND provider IS NOT NULL AND "
            "authorization_ref IS NOT NULL) OR "
            "(mode = 'revoked' AND provider IS NULL AND "
            "authorization_ref IS NOT NULL) OR "
            "(mode = 'disabled' AND provider IS NULL AND "
            "authorization_ref IS NULL)"
        ),
    )
    op.execute(
        "INSERT INTO ar_runtime_telemetry_state "
        "(singleton, mode, provider, authorization_ref, failure_code, updated_at) "
        "VALUES ('runtime', 'disabled', NULL, NULL, NULL, 0)"
    )


def downgrade() -> None:
    raise RuntimeError("vNext production migrations are forward-only")
