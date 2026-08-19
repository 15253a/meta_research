"""Create the Greenfield local runtime.

Revision ID: 0001_greenfield
Revises:
Create Date: 2026-08-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0001_greenfield"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    installation = op.create_table(
        "installation_metadata",
        sa.Column("singleton", sa.String(length=16), primary_key=True),
        sa.Column("product", sa.String(length=64), nullable=False),
        sa.Column("schema_format", sa.Integer(), nullable=False),
    )
    op.bulk_insert(
        installation,
        [{"singleton": "installation", "product": "meta-research-vnext", "schema_format": 1}],
    )

    research_graph = op.create_table(
        "research_graph_state",
        sa.Column("singleton", sa.String(length=16), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("quest_count", sa.Integer(), nullable=False),
        sa.Column("question_count", sa.Integer(), nullable=False),
        sa.CheckConstraint("quest_count >= 0"),
        sa.CheckConstraint("question_count >= 0"),
    )
    op.bulk_insert(
        research_graph,
        [{"singleton": "owner", "revision": 0, "quest_count": 0, "question_count": 0}],
    )

    advancement = op.create_table(
        "advancement_engine_state",
        sa.Column("singleton", sa.String(length=16), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("foreground_cycle_count", sa.Integer(), nullable=False),
        sa.CheckConstraint("foreground_cycle_count >= 0"),
    )
    op.bulk_insert(
        advancement,
        [{"singleton": "owner", "revision": 0, "foreground_cycle_count": 0}],
    )

    memory = op.create_table(
        "research_memory_state",
        sa.Column("singleton", sa.String(length=16), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("asset_count", sa.Integer(), nullable=False),
        sa.Column("object_count", sa.Integer(), nullable=False),
        sa.CheckConstraint("asset_count >= 0"),
        sa.CheckConstraint("object_count >= 0"),
    )
    op.bulk_insert(
        memory,
        [{"singleton": "owner", "revision": 0, "asset_count": 0, "object_count": 0}],
    )

    runtime = op.create_table(
        "agent_runtime_state",
        sa.Column("singleton", sa.String(length=16), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("active_run_count", sa.Integer(), nullable=False),
        sa.CheckConstraint("active_run_count >= 0"),
    )
    op.bulk_insert(
        runtime,
        [{"singleton": "owner", "revision": 0, "active_run_count": 0}],
    )

    collaboration = op.create_table(
        "human_collaboration_state",
        sa.Column("singleton", sa.String(length=16), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("pending_intent_count", sa.Integer(), nullable=False),
        sa.Column("authorization_count", sa.Integer(), nullable=False),
        sa.CheckConstraint("pending_intent_count >= 0"),
        sa.CheckConstraint("authorization_count >= 0"),
    )
    op.bulk_insert(
        collaboration,
        [
            {
                "singleton": "owner",
                "revision": 0,
                "pending_intent_count": 0,
                "authorization_count": 0,
            }
        ],
    )

    op.create_table(
        "durable_feed",
        sa.Column("revision", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.Float(), nullable=False),
        sqlite_autoincrement=True,
    )
    projection = op.create_table(
        "projection_offsets",
        sa.Column("projection_name", sa.String(length=80), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
    )
    op.bulk_insert(
        projection,
        [{"projection_name": "public_snapshot", "revision": 0}],
    )

    op.create_table(
        "auth_bootstrap_grants",
        sa.Column("token_hash", sa.String(length=64), primary_key=True),
        sa.Column("grant_kind", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("consumed_at", sa.Float(), nullable=True),
    )
    op.create_index(
        "ix_auth_bootstrap_grants_active",
        "auth_bootstrap_grants",
        ["grant_kind", "expires_at", "consumed_at"],
    )
    op.create_table(
        "auth_sessions",
        sa.Column("session_hash", sa.String(length=64), primary_key=True),
        sa.Column("csrf_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("revoked_at", sa.Float(), nullable=True),
    )
    op.create_index(
        "ix_auth_sessions_active",
        "auth_sessions",
        ["expires_at", "revoked_at"],
    )


def downgrade() -> None:
    raise RuntimeError("vNext production migrations are forward-only")
