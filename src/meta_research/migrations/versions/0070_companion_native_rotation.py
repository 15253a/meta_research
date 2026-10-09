"""Keep Companion workspaces stable while rotating native conversations."""

import sqlalchemy as sa
from alembic import op


revision = "0070_companion_native_rotation"
down_revision = "0069_merge_goal_creation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("hc_companion_sessions", sa.Column(
        "native_session_generation", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("hc_companion_turns", sa.Column(
        "native_session_generation", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("hc_companion_turns", sa.Column("native_session_ref", sa.String(256)))
    op.create_table(
        "hc_companion_native_sessions",
        sa.Column("session_ref", sa.String(64), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("native_session_ref", sa.String(256)),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("session_ref", "generation"),
        sa.ForeignKeyConstraint(["session_ref"], ["hc_companion_sessions.session_ref"]),
        sa.UniqueConstraint("session_ref", "native_session_ref"),
        sa.CheckConstraint("generation >= 1"),
    )
    op.execute("INSERT INTO hc_companion_native_sessions "
        "(session_ref,generation,native_session_ref,created_at) "
        "SELECT session_ref,1,native_session_ref,created_at FROM hc_companion_sessions")
    op.execute("UPDATE hc_companion_turns SET native_session_ref=(SELECT native_session_ref "
        "FROM hc_companion_sessions s WHERE s.session_ref=hc_companion_turns.session_ref)")
    op.create_table(
        "hc_companion_session_switches",
        sa.Column("switch_ref", sa.String(64), primary_key=True),
        sa.Column("scope_ref", sa.String(128), nullable=False),
        sa.Column("session_ref", sa.String(64), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("previous_native_session_ref", sa.String(256)),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["session_ref", "generation"],
            ["hc_companion_native_sessions.session_ref", "hc_companion_native_sessions.generation"]),
        sa.UniqueConstraint("session_ref", "generation"),
    )


def downgrade() -> None:
    raise RuntimeError("production migrations are forward-only")
