"""Add typed Writing documents and the AR external-delivery ledger.

Revision ID: 0034_writing_delivery
Revises: 0033_reasoning_successor_context
Create Date: 2026-08-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision = "0034_writing_delivery"
down_revision = "0033_reasoning_successor_context"
branch_labels = None
depends_on = None


def _hash(name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"length({name}) = 64")


def _copy_rows(source: str, target: str, columns: Sequence[str]) -> None:
    names = ", ".join(columns)
    op.execute(f"INSERT INTO {target} ({names}) SELECT {names} FROM {source}")


def _replace_writing_runs() -> None:
    """Widen the original report-only check without changing any row bytes."""

    source = "ar_writing_runs"
    backup = "ar_writing_runs_pre_delivery"
    columns = (
        "run_ref",
        "intent_id",
        "quest_ref",
        "document_type",
        "intent_json",
        "intent_hash",
        "snapshot_ref",
        "snapshot_json",
        "snapshot_hash",
        "confirmation_ref",
        "confirmation_hash",
        "status",
        "failure_code",
        "execution_budget_json",
        "execution_budget_hash",
        "output_bytes",
        "attempt_ref",
        "attempt_generation",
        "root_session_ref",
        "native_session_ref",
        "fence_ref",
        "predecessor_version_ref",
        "feedback_json",
        "feedback_hash",
        "runtime_binding_json",
        "runtime_binding_hash",
        "created_at",
        "updated_at",
    )
    connection = op.get_bind()
    connection.exec_driver_sql("PRAGMA legacy_alter_table=ON")
    try:
        op.rename_table(source, backup)
        op.create_table(
            source,
            sa.Column("run_ref", sa.String(96), primary_key=True),
            sa.Column("intent_id", sa.String(96), nullable=False, unique=True),
            sa.Column("quest_ref", sa.String(96), nullable=False),
            sa.Column("document_type", sa.String(24), nullable=False),
            sa.Column("intent_json", sa.Text(), nullable=False),
            sa.Column("intent_hash", sa.String(64), nullable=False),
            sa.Column("snapshot_ref", sa.String(96), nullable=False),
            sa.Column("snapshot_json", sa.Text(), nullable=False),
            sa.Column("snapshot_hash", sa.String(64), nullable=False),
            sa.Column("confirmation_ref", sa.String(96), nullable=False, unique=True),
            sa.Column("confirmation_hash", sa.String(64), nullable=False),
            sa.Column("status", sa.String(24), nullable=False),
            sa.Column("failure_code", sa.String(128), nullable=True),
            sa.Column("execution_budget_json", sa.Text(), nullable=False),
            sa.Column("execution_budget_hash", sa.String(64), nullable=False),
            sa.Column("output_bytes", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("attempt_ref", sa.String(96), nullable=False, unique=True),
            sa.Column("attempt_generation", sa.Integer(), nullable=False),
            sa.Column("root_session_ref", sa.String(96), nullable=False, unique=True),
            sa.Column("native_session_ref", sa.String(96), nullable=True, unique=True),
            sa.Column("fence_ref", sa.String(96), nullable=False, unique=True),
            sa.Column("predecessor_version_ref", sa.String(96), nullable=True),
            sa.Column("feedback_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column(
                "feedback_hash",
                sa.String(64),
                nullable=False,
                server_default="4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
            ),
            sa.Column("runtime_binding_json", sa.Text(), nullable=False),
            sa.Column("runtime_binding_hash", sa.String(64), nullable=False),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.Column("updated_at", sa.Float(), nullable=False),
            sa.ForeignKeyConstraint(["intent_id"], ["hc_command_intents.intent_id"]),
            sa.ForeignKeyConstraint(["quest_ref"], ["rg_quests.quest_ref"]),
            sa.CheckConstraint(
                "document_type IN ('report', 'paper', 'presentation')"
            ),
            sa.CheckConstraint(
                "status IN ('active', 'paused', 'blocked', 'completed', 'cancelled')"
            ),
            sa.CheckConstraint("attempt_generation >= 1"),
            sa.CheckConstraint("output_bytes >= 0"),
            *(
                _hash(name)
                for name in (
                    "intent_hash",
                    "snapshot_hash",
                    "confirmation_hash",
                    "execution_budget_hash",
                    "feedback_hash",
                    "runtime_binding_hash",
                )
            ),
        )
        _copy_rows(backup, source, columns)
        op.drop_table(backup)
        op.create_index(
            "ix_ar_writing_runs_status_updated",
            source,
            ["status", "updated_at", "run_ref"],
        )
    finally:
        connection.exec_driver_sql("PRAGMA legacy_alter_table=OFF")


def upgrade() -> None:
    _replace_writing_runs()
    for name in (
        "writing_delivery_operation_count",
        "writing_delivery_completed_count",
        "writing_delivery_reconciliation_count",
    ):
        op.add_column(
            "agent_runtime_state",
            sa.Column(name, sa.Integer(), nullable=False, server_default="0"),
        )

    op.create_table(
        "ar_writing_delivery_operations",
        sa.Column("operation_ref", sa.String(96), primary_key=True),
        sa.Column("request_nonce", sa.String(128), nullable=False),
        sa.Column("run_ref", sa.String(96), nullable=False),
        sa.Column("document_type", sa.String(24), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("provider_ref", sa.String(128), nullable=False),
        sa.Column("provider_operation_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("target_json", sa.Text(), nullable=False),
        sa.Column("target_hash", sa.String(64), nullable=False),
        sa.Column("asset_ref", sa.String(128), nullable=False),
        sa.Column("version_ref", sa.String(128), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("citation_decision_ref", sa.String(128), nullable=False),
        sa.Column("renderer_version_ref", sa.String(128), nullable=False),
        sa.Column("renderer_artifact_sha256", sa.String(64), nullable=False),
        sa.Column("intent_id", sa.String(96), nullable=False),
        sa.Column("draft_revision", sa.Integer(), nullable=False),
        sa.Column("draft_hash", sa.String(64), nullable=False),
        sa.Column("preview_ref", sa.String(96), nullable=False),
        sa.Column("preview_hash", sa.String(64), nullable=False),
        sa.Column("confirmation_json", sa.Text(), nullable=False),
        sa.Column("confirmation_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("provider_request_hash", sa.String(64), nullable=True),
        sa.Column("operation_receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("execution_receipt_ref", sa.String(96), nullable=True, unique=True),
        sa.Column("reconciliation_receipt_ref", sa.String(96), nullable=True, unique=True),
        sa.Column("failure_code", sa.String(128), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("completed_at", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["run_ref"], ["ar_writing_runs.run_ref"]),
        sa.CheckConstraint(
            "document_type IN ('report', 'paper', 'presentation')"
        ),
        sa.CheckConstraint(
            "action IN ('publish', 'overwrite', 'delete', 'send', 'submit')"
        ),
        sa.CheckConstraint(
            "status IN ('admitted', 'executing', 'partial', 'outcome_unknown', 'completed')"
        ),
        sa.CheckConstraint("draft_revision >= 1"),
        sa.CheckConstraint("attempt_count >= 0"),
        sa.CheckConstraint(
            "(status = 'completed' AND completed_at IS NOT NULL) OR "
            "(status != 'completed' AND completed_at IS NULL)"
        ),
        *(
            _hash(name)
            for name in (
                "payload_hash",
                "target_hash",
                "content_hash",
                "renderer_artifact_sha256",
                "draft_hash",
                "preview_hash",
                "confirmation_hash",
                "request_hash",
                "provider_request_hash",
            )
        ),
    )
    op.create_index(
        "ix_ar_writing_delivery_status_updated",
        "ar_writing_delivery_operations",
        ["status", "updated_at", "operation_ref"],
    )
    op.create_index(
        "ix_ar_writing_delivery_run_created",
        "ar_writing_delivery_operations",
        ["run_ref", "created_at", "operation_ref"],
    )

    op.create_table(
        "ar_writing_delivery_observations",
        sa.Column("observation_ref", sa.String(96), primary_key=True),
        sa.Column("operation_ref", sa.String(96), nullable=False),
        sa.Column("provider_ref", sa.String(128), nullable=False),
        sa.Column("provider_operation_ref", sa.String(96), nullable=False),
        sa.Column("outcome", sa.String(24), nullable=False),
        sa.Column("observation_json", sa.Text(), nullable=False),
        sa.Column("observation_hash", sa.String(64), nullable=False),
        sa.Column("semantic_hash", sa.String(64), nullable=False),
        sa.Column("observed_at", sa.Float(), nullable=False),
        sa.Column("recorded_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["operation_ref"], ["ar_writing_delivery_operations.operation_ref"]
        ),
        sa.UniqueConstraint("operation_ref", "observation_hash"),
        sa.UniqueConstraint("operation_ref", "semantic_hash"),
        sa.CheckConstraint(
            "outcome IN ('completed', 'not_found', 'partial', 'outcome_unknown')"
        ),
        _hash("observation_hash"),
        _hash("semantic_hash"),
    )

    op.create_table(
        "ar_writing_delivery_receipts",
        sa.Column("receipt_ref", sa.String(96), primary_key=True),
        sa.Column("operation_ref", sa.String(96), nullable=False),
        sa.Column("receipt_role", sa.String(24), nullable=False),
        sa.Column("receipt_kind", sa.String(64), nullable=False),
        sa.Column("subject_ref", sa.String(96), nullable=False),
        sa.Column("fact_json", sa.Text(), nullable=False),
        sa.Column("fact_hash", sa.String(64), nullable=False),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("recorded_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["operation_ref"], ["ar_writing_delivery_operations.operation_ref"]
        ),
        sa.CheckConstraint(
            "receipt_role IN ('operation', 'execution', 'reconciliation')"
        ),
        _hash("fact_hash"),
        _hash("receipt_hash"),
    )


def downgrade() -> None:
    raise RuntimeError("vNext production migrations are forward-only")
