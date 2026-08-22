"""Add the Quest-level Nature Downloader acquisition session.

Revision ID: 0008_quest_acquisition_session
Revises: 0007_first_question_deepfetch
Create Date: 2026-08-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0008_quest_acquisition_session"
down_revision = "0007_first_question_deepfetch"
branch_labels = None
depends_on = None


def _counter(name: str) -> sa.Column:
    return sa.Column(name, sa.Integer(), nullable=False, server_default="0")


def _hash(name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"length({name}) = 64")


def upgrade() -> None:
    op.add_column("agent_runtime_state", _counter("acquisition_session_count"))
    op.add_column("agent_runtime_state", _counter("acquisition_request_count"))
    op.add_column(
        "agent_runtime_state", _counter("acquisition_active_slot_count")
    )
    op.add_column(
        "hc_deepfetch_requests",
        sa.Column("acquisition_session_ref", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "hc_deepfetch_requests",
        sa.Column("acquisition_config_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "hc_deepfetch_requests",
        sa.Column(
            "acquisition_runtime_binding_hash",
            sa.String(length=64),
            nullable=True,
        ),
    )

    op.create_table(
        "ar_acquisition_sessions",
        sa.Column("session_ref", sa.String(length=64), primary_key=True),
        sa.Column(
            "initialization_id", sa.String(length=64), nullable=False, unique=True
        ),
        sa.Column("quest_ref", sa.String(length=64), nullable=True, unique=True),
        sa.Column("config_json", sa.Text(), nullable=False),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("runtime_binding_json", sa.Text(), nullable=False),
        sa.Column("runtime_binding_hash", sa.String(length=64), nullable=False),
        sa.Column("browser_context_ref", sa.String(length=512), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("preflight_generation", sa.Integer(), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("current_request_id", sa.String(length=128), nullable=True),
        sa.Column("slot_held", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reason_code", sa.String(length=96), nullable=True),
        sa.Column("evidence_json", sa.Text(), nullable=True),
        sa.Column("evidence_hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("last_ready_at", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(
            ["initialization_id"],
            ["hc_quest_initializations.initialization_id"],
        ),
        _hash("config_hash"),
        _hash("runtime_binding_hash"),
        sa.CheckConstraint("preflight_generation >= 1"),
        sa.CheckConstraint("request_count >= 0"),
        sa.CheckConstraint("slot_held IN (0, 1)"),
        sa.CheckConstraint(
            "mode IN ('oa_then_institution', 'oa_only', 'provided_only')"
        ),
        sa.CheckConstraint(
            "status IN ('probing', 'ready', 'waiting_user', 'unavailable', "
            "'acquiring', 'cancelled')"
        ),
        sa.CheckConstraint(
            "(evidence_json IS NULL AND evidence_hash IS NULL) OR "
            "(evidence_json IS NOT NULL AND evidence_hash IS NOT NULL "
            "AND length(evidence_hash) = 64)"
        ),
        sa.CheckConstraint(
            "(status IN ('probing', 'acquiring') AND slot_held = 1) OR "
            "(status NOT IN ('probing', 'acquiring') AND slot_held = 0)"
        ),
    )
    op.create_index(
        "ix_ar_acquisition_sessions_status",
        "ar_acquisition_sessions",
        ["status", "updated_at"],
    )

    op.create_table(
        "ar_acquisition_requests",
        sa.Column("request_id", sa.String(length=128), primary_key=True),
        sa.Column("session_ref", sa.String(length=64), nullable=False),
        sa.Column("request_json", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("route_policy", sa.String(length=48), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("results_json", sa.Text(), nullable=True),
        sa.Column("results_hash", sa.String(length=64), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("completed_at", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(
            ["session_ref"], ["ar_acquisition_sessions.session_ref"]
        ),
        _hash("request_hash"),
        sa.CheckConstraint("attempt_count >= 1"),
        sa.CheckConstraint(
            "route_policy = 'oa_first_then_institution'"
        ),
        sa.CheckConstraint(
            "status IN ('running', 'obtained', 'partial', 'waiting_user', "
            "'missing', 'cancelled')"
        ),
        sa.CheckConstraint(
            "(status = 'running' AND completed_at IS NULL AND "
            "((results_json IS NULL AND results_hash IS NULL) OR "
            "(results_json IS NOT NULL AND results_hash IS NOT NULL AND "
            "length(results_hash) = 64))) OR "
            "(status != 'running' AND results_json IS NOT NULL AND "
            "results_hash IS NOT NULL AND length(results_hash) = 64 AND "
            "completed_at IS NOT NULL)"
        ),
    )
    op.create_index(
        "ix_ar_acquisition_requests_session",
        "ar_acquisition_requests",
        ["session_ref", "created_at"],
    )


def downgrade() -> None:
    raise RuntimeError("vNext production migrations are forward-only")
