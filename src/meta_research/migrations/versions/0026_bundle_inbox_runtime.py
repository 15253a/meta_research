"""Scope the Bundle inbox and its acknowledged checkpoint per Bundle run.

Revision ID: 0026_bundle_inbox_runtime
Revises: 0025_target_generic_measurement
Create Date: 2026-08-24
"""

from __future__ import annotations

import time

import sqlalchemy as sa
from alembic import op


revision = "0026_bundle_inbox_runtime"
down_revision = "0025_target_generic_measurement"
branch_labels = None
depends_on = None


def _hash(name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"length({name}) = 64")


def upgrade() -> None:
    # Checkpoints are immutable AR facts.  They are created before the mutable
    # scope table so the scope can hold a real FK to its current checkpoint.
    op.create_table(
        "ar_bundle_inbox_checkpoints",
        sa.Column("checkpoint_ref", sa.String(96), primary_key=True),
        sa.Column("run_ref", sa.String(96), nullable=False),
        sa.Column("attempt_ref", sa.String(96), nullable=False),
        sa.Column("fence_ref", sa.String(96), nullable=False),
        sa.Column("checkpoint_revision", sa.Integer(), nullable=False),
        sa.Column("cursor", sa.Integer(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("batch_hash", sa.String(64), nullable=False),
        sa.Column("checkpoint_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["run_ref"], ["ar_stage_runs.run_ref"]),
        sa.ForeignKeyConstraint(["attempt_ref"], ["ar_stage_attempts.attempt_ref"]),
        sa.ForeignKeyConstraint(["fence_ref"], ["ar_execution_fences.fence_ref"]),
        sa.UniqueConstraint("run_ref", "checkpoint_revision"),
        sa.CheckConstraint("checkpoint_revision >= 1"),
        sa.CheckConstraint("cursor >= 0"),
        sa.CheckConstraint("generation >= 0"),
        *(
            _hash(name)
            for name in (
                "batch_hash",
                "checkpoint_hash",
                "request_hash",
                "receipt_hash",
            )
        ),
    )
    op.create_index(
        "ix_ar_bundle_inbox_checkpoints_run_ref",
        "ar_bundle_inbox_checkpoints",
        ["run_ref", "checkpoint_revision"],
    )

    op.create_table(
        "ar_bundle_inbox_scopes",
        sa.Column("run_ref", sa.String(96), primary_key=True),
        sa.Column("next_sequence", sa.Integer(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("wake_pending", sa.Boolean(), nullable=False),
        sa.Column("acknowledged_cursor", sa.Integer(), nullable=False),
        sa.Column("current_checkpoint_ref", sa.String(96), nullable=True, unique=True),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["run_ref"], ["ar_stage_runs.run_ref"]),
        sa.ForeignKeyConstraint(
            ["current_checkpoint_ref"],
            ["ar_bundle_inbox_checkpoints.checkpoint_ref"],
        ),
        sa.CheckConstraint("next_sequence >= 1"),
        sa.CheckConstraint("generation >= 0"),
        sa.CheckConstraint("acknowledged_cursor >= 0"),
        sa.CheckConstraint("acknowledged_cursor < next_sequence"),
    )

    op.create_table(
        "ar_bundle_inbox_entries",
        sa.Column("run_ref", sa.String(96), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("notice_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("published_generation", sa.Integer(), nullable=False),
        sa.Column("published_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("run_ref", "sequence"),
        sa.ForeignKeyConstraint(["run_ref"], ["ar_stage_runs.run_ref"]),
        sa.ForeignKeyConstraint(
            ["notice_ref"], ["ar_target_work_notices.notice_ref"]
        ),
        sa.CheckConstraint("sequence >= 1"),
        sa.CheckConstraint("published_generation >= 1"),
    )
    # Proposal and dispatch rows predate Inbox checkpoints.  Keep those
    # released tables intact and bind every new proposal-capable operation in
    # this exact companion ledger.  Legacy rows remain readable history but
    # cannot authorize a new append or Target launch without a binding.
    op.create_table(
        "ar_bundle_inbox_operation_checkpoints",
        sa.Column("operation_kind", sa.String(24), nullable=False),
        sa.Column("operation_ref", sa.String(96), nullable=False),
        sa.Column("checkpoint_ref", sa.String(96), nullable=False),
        sa.Column("checkpoint_hash", sa.String(64), nullable=False),
        sa.Column("binding_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("bound_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("operation_kind", "operation_ref"),
        sa.ForeignKeyConstraint(
            ["checkpoint_ref"], ["ar_bundle_inbox_checkpoints.checkpoint_ref"]
        ),
        sa.CheckConstraint("operation_kind IN ('target_proposal', 'dispatch')"),
        _hash("checkpoint_hash"),
        _hash("binding_hash"),
        _hash("receipt_hash"),
    )
    op.create_index(
        "ix_ar_bundle_inbox_operation_checkpoint_ref",
        "ar_bundle_inbox_operation_checkpoints",
        ["checkpoint_ref"],
    )

    # Existing 0019 notices were global.  Recover their authoritative Bundle
    # scope through Target launch -> dispatch and expose the whole recovered
    # prefix as pending.  No notice is silently acknowledged by the migration.
    connection = op.get_bind()
    now = time.time()
    notice_count = int(
        connection.execute(
            sa.text("SELECT COUNT(*) FROM ar_target_work_notices")
        ).scalar_one()
    )
    scoped_notice_count = int(
        connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM ar_target_work_notices notices JOIN "
                "ar_target_launches launches ON launches.target_ref = "
                "notices.target_ref JOIN ar_bundle_dispatch_decisions decisions "
                "ON decisions.decision_ref = launches.dispatch_decision_ref JOIN "
                "ar_stage_runs runs ON runs.run_ref = decisions.run_ref WHERE "
                "runs.stage = 'bundle'"
            )
        ).scalar_one()
    )
    if scoped_notice_count != notice_count:
        raise RuntimeError("legacy Bundle inbox notice has no authoritative run scope")
    bundle_runs = connection.execute(
        sa.text("SELECT run_ref FROM ar_stage_runs WHERE stage = 'bundle'")
    ).all()
    for run in bundle_runs:
        notice_rows = connection.execute(
            sa.text(
                "SELECT notices.notice_ref, notices.published_at FROM "
                "ar_target_work_notices notices JOIN ar_target_launches launches "
                "ON launches.target_ref = notices.target_ref JOIN "
                "ar_bundle_dispatch_decisions decisions ON "
                "decisions.decision_ref = launches.dispatch_decision_ref WHERE "
                "decisions.run_ref = :run_ref ORDER BY notices.sequence"
            ),
            {"run_ref": run.run_ref},
        ).all()
        generation = 1 if notice_rows else 0
        connection.execute(
            sa.text(
                "INSERT INTO ar_bundle_inbox_scopes (run_ref, next_sequence, "
                "generation, wake_pending, acknowledged_cursor, "
                "current_checkpoint_ref, updated_at) VALUES (:run_ref, "
                ":next_sequence, :generation, :wake_pending, 0, NULL, :updated_at)"
            ),
            {
                "run_ref": run.run_ref,
                "next_sequence": len(notice_rows) + 1,
                "generation": generation,
                "wake_pending": bool(notice_rows),
                "updated_at": now,
            },
        )
        for sequence, notice in enumerate(notice_rows, start=1):
            connection.execute(
                sa.text(
                    "INSERT INTO ar_bundle_inbox_entries (run_ref, sequence, "
                    "notice_ref, published_generation, published_at) VALUES "
                    "(:run_ref, :sequence, :notice_ref, 1, :published_at)"
                ),
                {
                    "run_ref": run.run_ref,
                    "sequence": sequence,
                    "notice_ref": notice.notice_ref,
                    "published_at": notice.published_at,
                },
            )


def downgrade() -> None:
    op.drop_index(
        "ix_ar_bundle_inbox_operation_checkpoint_ref",
        table_name="ar_bundle_inbox_operation_checkpoints",
    )
    op.drop_table("ar_bundle_inbox_operation_checkpoints")
    op.drop_table("ar_bundle_inbox_entries")
    op.drop_table("ar_bundle_inbox_scopes")
    op.drop_index(
        "ix_ar_bundle_inbox_checkpoints_run_ref",
        table_name="ar_bundle_inbox_checkpoints",
    )
    op.drop_table("ar_bundle_inbox_checkpoints")
