"""Audit the runtime binding and native session of every DeepFetch Attempt.

Revision ID: 0038_deepfetch_binding_audit
Revises: 0037_question_stage_identity
Create Date: 2026-08-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0038_deepfetch_binding_audit"
down_revision = "0037_question_stage_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ar_deepfetch_attempts",
        sa.Column("runtime_binding_json", sa.Text(), nullable=True),
    )
    op.add_column(
        "ar_deepfetch_attempts",
        sa.Column("runtime_binding_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "ar_deepfetch_attempts",
        sa.Column("native_session_ref", sa.String(length=512), nullable=True),
    )
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "UPDATE ar_deepfetch_attempts SET "
            "runtime_binding_json = (SELECT r.runtime_binding_json FROM "
            "ar_deepfetch_runs r WHERE r.run_ref = ar_deepfetch_attempts.run_ref), "
            "runtime_binding_hash = (SELECT r.runtime_binding_hash FROM "
            "ar_deepfetch_runs r WHERE r.run_ref = ar_deepfetch_attempts.run_ref), "
            "native_session_ref = (SELECT s.native_session_ref FROM "
            "ar_deepfetch_sessions s WHERE "
            "s.run_ref = ar_deepfetch_attempts.run_ref)"
        )
    )

    inspector = sa.inspect(connection)
    preserved_checks = tuple(
        sa.CheckConstraint(
            constraint["sqltext"],
            name=constraint.get("name"),
        )
        for constraint in inspector.get_check_constraints("ar_deepfetch_attempts")
    )
    connection.exec_driver_sql("PRAGMA legacy_alter_table=ON")
    try:
        with op.batch_alter_table(
            "ar_deepfetch_attempts",
            recreate="always",
            table_args=(
                *preserved_checks,
                sa.CheckConstraint(
                    "length(runtime_binding_hash) = 64",
                    name="ck_ar_deepfetch_attempts_runtime_binding_hash",
                ),
            ),
        ) as batch:
            batch.alter_column(
                "runtime_binding_json",
                existing_type=sa.Text(),
                nullable=False,
            )
            batch.alter_column(
                "runtime_binding_hash",
                existing_type=sa.String(length=64),
                nullable=False,
            )
    finally:
        connection.exec_driver_sql("PRAGMA legacy_alter_table=OFF")


def downgrade() -> None:
    raise RuntimeError("vNext production migrations are forward-only")
