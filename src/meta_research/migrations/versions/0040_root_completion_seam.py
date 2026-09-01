"""Close the durable Root completion and successor seam.

Revision ID: 0040_root_completion_seam
Revises: 0039_target_root_provider_recovery
Create Date: 2026-09-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0040_root_completion_seam"
down_revision = "0039_target_root_provider_recovery"
branch_labels = None
depends_on = None


def _hash(name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"length({name}) = 64")


def upgrade() -> None:
    # #137 made the persistent Companion session the parent of proposal forks.
    # Keep that native identity nullable until the first provider turn binds it.
    op.add_column(
        "hc_intent_drafting_sessions",
        sa.Column("native_session_ref", sa.String(length=512), nullable=True),
    )
    op.create_index(
        "uq_hc_intent_drafting_sessions_native_session_ref",
        "hc_intent_drafting_sessions",
        ["native_session_ref"],
        unique=True,
        sqlite_where=sa.text("native_session_ref IS NOT NULL"),
    )

    # A provider result can be complete while its structured candidate is not
    # acceptable.  Keep that semantic fact separate from runtime ceilings and
    # reuse the existing Attempt replacement row for the successor relation.
    op.create_table(
        "ar_stage_completion_rejections",
        sa.Column("rejection_ref", sa.String(96), primary_key=True),
        sa.Column("request_ref", sa.String(96), nullable=False),
        sa.Column("run_ref", sa.String(96), nullable=False),
        sa.Column("stage", sa.String(24), nullable=False),
        sa.Column("attempt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("fence_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("root_session_ref", sa.String(96), nullable=False),
        sa.Column(
            "provider_invocation_ref", sa.String(96), nullable=False, unique=True
        ),
        sa.Column("provider_operation_ref", sa.String(128), nullable=False),
        sa.Column("candidate_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("candidate_json", sa.Text(), nullable=False),
        sa.Column("candidate_hash", sa.String(64), nullable=False),
        sa.Column("reason_code", sa.String(96), nullable=False),
        sa.Column("detail_code", sa.String(128), nullable=False),
        sa.Column("feedback_json", sa.Text(), nullable=False),
        sa.Column("feedback_hash", sa.String(64), nullable=False),
        sa.Column("known_facts_json", sa.Text(), nullable=False),
        sa.Column("known_facts_hash", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("rejected_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["request_ref"], ["ae_stage_run_requests.request_ref"]
        ),
        sa.ForeignKeyConstraint(["run_ref"], ["ar_stage_runs.run_ref"]),
        sa.ForeignKeyConstraint(
            ["attempt_ref"], ["ar_stage_attempts.attempt_ref"]
        ),
        sa.ForeignKeyConstraint(["fence_ref"], ["ar_execution_fences.fence_ref"]),
        sa.ForeignKeyConstraint(
            ["root_session_ref"], ["ar_stage_sessions.session_ref"]
        ),
        sa.ForeignKeyConstraint(
            ["provider_invocation_ref"],
            ["ar_stage_provider_invocations.invocation_ref"],
        ),
        sa.CheckConstraint("stage IN ('idea', 'plan', 'bundle', 'reasoning')"),
        sa.CheckConstraint("length(reason_code) > 0 AND length(reason_code) <= 96"),
        sa.CheckConstraint("length(detail_code) > 0 AND length(detail_code) <= 128"),
        *(
            _hash(name)
            for name in (
                "candidate_hash",
                "feedback_hash",
                "known_facts_hash",
                "payload_hash",
                "receipt_hash",
            )
        ),
    )


def downgrade() -> None:
    raise RuntimeError("vNext production migrations are forward-only")
