"""Rotate the actual creation Companion intent Session, preserving its workspace."""

import sqlalchemy as sa
from alembic import op


revision = "0071_creation_companion_rotation"
down_revision = "0070_companion_native_rotation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("hc_intent_drafting_sessions", sa.Column(
        "native_session_generation", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("hc_intent_drafting_turns", sa.Column(
        "native_session_generation", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("hc_intent_drafting_turns", sa.Column("native_session_ref", sa.String(256)))
    op.add_column("hc_proposal_generation_attempts", sa.Column("native_session_generation", sa.Integer()))
    op.execute("UPDATE hc_proposal_generation_attempts SET native_session_generation=1")
    op.create_table("hc_intent_native_sessions",
        sa.Column("session_ref", sa.String(64), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("native_session_ref", sa.String(256)),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("session_ref", "generation"),
        sa.ForeignKeyConstraint(["session_ref"], ["hc_intent_drafting_sessions.session_ref"]),
        sa.UniqueConstraint("session_ref", "native_session_ref"),
        sa.CheckConstraint("generation>=1"))
    op.execute("INSERT INTO hc_intent_native_sessions (session_ref,generation,native_session_ref,created_at) "
        "SELECT session_ref,1,native_session_ref,created_at FROM hc_intent_drafting_sessions")
    op.execute("UPDATE hc_intent_drafting_turns SET native_session_ref="
        "json_extract(adapter_metadata_json,'$.native_session_ref') WHERE adapter_metadata_json IS NOT NULL")
    op.create_table("hc_intent_session_switches",
        sa.Column("switch_ref", sa.String(64), primary_key=True),
        sa.Column("session_ref", sa.String(64), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("previous_native_session_ref", sa.String(256)),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["session_ref", "generation"],
            ["hc_intent_native_sessions.session_ref", "hc_intent_native_sessions.generation"]),
        sa.UniqueConstraint("session_ref", "generation"))


def downgrade() -> None:
    raise RuntimeError("production migrations are forward-only")
