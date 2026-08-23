"""Add durable Harness admissions and scope-bound MCP channel grants.

Revision ID: 0013_semantic_mcp_harness
Revises: 0012_experiment_measurement
Create Date: 2026-08-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0013_semantic_mcp_harness"
down_revision = "0012_experiment_measurement"
branch_labels = None
depends_on = None


def _hash(name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"length({name}) = 64")


def upgrade() -> None:
    # This is an Agent Runtime-owned Typed Run extension, not a Harness owner.
    # The root Session deliberately lives on the Run row: this feature must
    # not introduce a parallel Session table or Session authority.
    op.create_table(
        "ar_harness_runs",
        sa.Column("request_ref", sa.String(length=96), primary_key=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False, unique=True),
        sa.Column("request_json", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("run_ref", sa.String(length=96), nullable=False, unique=True),
        sa.Column("attempt_ref", sa.String(length=96), nullable=False, unique=True),
        sa.Column("attempt_generation", sa.Integer(), nullable=False),
        sa.Column("root_session_ref", sa.String(length=96), nullable=False, unique=True),
        sa.Column("native_session_ref", sa.String(length=160), nullable=True),
        sa.Column("fence_ref", sa.String(length=96), nullable=False, unique=True),
        sa.Column("harness_family", sa.String(length=16), nullable=False),
        sa.Column("model_ref", sa.String(length=160), nullable=False),
        sa.Column("auth_profile_ref", sa.String(length=160), nullable=False),
        sa.Column("capability_binding_hash", sa.String(length=64), nullable=False),
        sa.Column("mcp_binding_json", sa.Text(), nullable=True),
        sa.Column("mcp_binding_hash", sa.String(length=64), nullable=True),
        sa.Column("profile_json", sa.Text(), nullable=True),
        sa.Column("profile_hash", sa.String(length=64), nullable=True),
        sa.Column("failure_code", sa.String(length=96), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("completed_at", sa.Float(), nullable=True),
        sa.CheckConstraint("harness_family IN ('codex', 'claude')"),
        sa.CheckConstraint("attempt_generation >= 1"),
        sa.CheckConstraint(
            "status IN ('admitting', 'admitted', 'running', 'executed', "
            "'failed', 'revoked')"
        ),
        _hash("request_hash"),
        _hash("capability_binding_hash"),
        sa.CheckConstraint(
            "(mcp_binding_json IS NULL AND mcp_binding_hash IS NULL) OR "
            "(mcp_binding_json IS NOT NULL AND mcp_binding_hash IS NOT NULL "
            "AND length(mcp_binding_hash) = 64)"
        ),
        sa.CheckConstraint(
            "(profile_json IS NULL AND profile_hash IS NULL) OR "
            "(profile_json IS NOT NULL AND profile_hash IS NOT NULL "
            "AND length(profile_hash) = 64)"
        ),
    )
    op.create_index(
        "ix_ar_harness_runs_status",
        "ar_harness_runs",
        ["status", "updated_at", "run_ref"],
    )
    op.create_table(
        "ar_mcp_channel_grants",
        sa.Column("grant_ref", sa.String(length=96), primary_key=True),
        sa.Column("run_ref", sa.String(length=96), nullable=False),
        sa.Column("server_instance_ref", sa.String(length=96), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("scope_json", sa.Text(), nullable=False),
        sa.Column("scope_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("issued_at", sa.Float(), nullable=False),
        sa.Column("revoked_at", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["run_ref"], ["ar_harness_runs.run_ref"]),
        sa.CheckConstraint("status IN ('current', 'revoked')"),
        sa.CheckConstraint(
            "(status = 'current' AND revoked_at IS NULL) OR "
            "(status = 'revoked' AND revoked_at IS NOT NULL)"
        ),
        _hash("token_hash"),
        _hash("scope_hash"),
    )
    op.create_index(
        "ix_ar_mcp_channel_grants_run_status",
        "ar_mcp_channel_grants",
        ["run_ref", "status", "issued_at"],
    )
    op.create_table(
        "ar_harness_provider_operations",
        sa.Column("operation_ref", sa.String(length=128), primary_key=True),
        sa.Column("run_ref", sa.String(length=96), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("invocation_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("outcome_code", sa.String(length=96), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("completed_at", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["run_ref"], ["ar_harness_runs.run_ref"]),
        sa.UniqueConstraint("run_ref", "generation"),
        sa.CheckConstraint("generation >= 1"),
        sa.CheckConstraint(
            "status IN ('running', 'executed', 'failed', 'unknown_outcome')"
        ),
        _hash("invocation_hash"),
    )
    op.create_table(
        "ar_harness_evidence_events",
        sa.Column("event_ref", sa.String(length=96), primary_key=True),
        sa.Column("operation_ref", sa.String(length=128), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("summary_json", sa.Text(), nullable=False),
        sa.Column("summary_hash", sa.String(length=64), nullable=False),
        sa.Column("recorded_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["operation_ref"], ["ar_harness_provider_operations.operation_ref"]
        ),
        sa.UniqueConstraint("operation_ref", "sequence"),
        sa.CheckConstraint("sequence >= 1"),
        _hash("summary_hash"),
    )


def downgrade() -> None:
    raise RuntimeError("vNext production migrations are forward-only")
