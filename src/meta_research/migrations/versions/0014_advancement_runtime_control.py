"""Add durable Advancement Engine and Agent Runtime control recovery.

Revision ID: 0014_advancement_runtime_control
Revises: 0013_bundle_target_dag
Create Date: 2026-08-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision = "0014_advancement_runtime_control"
down_revision = "0013_bundle_target_dag"
branch_labels = None
depends_on = None


def _counter(table: str, name: str) -> None:
    op.add_column(
        table,
        sa.Column(name, sa.Integer(), nullable=False, server_default="0"),
    )


def _hash(name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"length({name}) = 64")


def _copy_rows(source: str, target: str, columns: Sequence[str]) -> None:
    column_list = ", ".join(columns)
    op.execute(
        f"INSERT INTO {target} ({column_list}) SELECT {column_list} FROM {source}"
    )


def _replace_stage_provider_invocations() -> None:
    """Seal the durable provider operation identity after its legacy backfill."""

    source = "ar_stage_provider_invocations"
    backup = "ar_stage_provider_invocations_pre_control"
    columns = (
        "invocation_ref",
        "operation_ref",
        "run_ref",
        "attempt_ref",
        "fence_ref",
        "phase",
        "request_hash",
        "runtime_binding_hash",
        "status",
        "response_hash",
        "prepared_at",
        "completed_at",
    )
    connection = op.get_bind()
    connection.exec_driver_sql("PRAGMA legacy_alter_table=ON")
    try:
        op.rename_table(source, backup)
        op.create_table(
            source,
            sa.Column("invocation_ref", sa.String(length=64), primary_key=True),
            sa.Column("operation_ref", sa.String(length=96), nullable=False),
            sa.Column("run_ref", sa.String(length=64), nullable=False),
            sa.Column("attempt_ref", sa.String(length=64), nullable=False),
            sa.Column("fence_ref", sa.String(length=64), nullable=False),
            sa.Column("phase", sa.String(length=16), nullable=False),
            sa.Column("request_hash", sa.String(length=64), nullable=False),
            sa.Column("runtime_binding_hash", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("response_hash", sa.String(length=64), nullable=True),
            sa.Column("prepared_at", sa.Float(), nullable=False),
            sa.Column("completed_at", sa.Float(), nullable=True),
            sa.ForeignKeyConstraint(["run_ref"], ["ar_stage_runs.run_ref"]),
            sa.ForeignKeyConstraint(
                ["attempt_ref"], ["ar_stage_attempts.attempt_ref"]
            ),
            sa.ForeignKeyConstraint(
                ["fence_ref"], ["ar_execution_fences.fence_ref"]
            ),
            sa.UniqueConstraint("attempt_ref", "phase"),
            sa.CheckConstraint("phase IN ('primary', 'review')"),
            sa.CheckConstraint("status IN ('prepared', 'completed')"),
            _hash("request_hash"),
            _hash("runtime_binding_hash"),
            sa.CheckConstraint(
                "(status = 'prepared' AND response_hash IS NULL "
                "AND completed_at IS NULL) OR "
                "(status = 'completed' AND response_hash IS NOT NULL "
                "AND length(response_hash) = 64 AND completed_at IS NOT NULL)"
            ),
        )
        _copy_rows(backup, source, columns)
        op.drop_table(backup)
        op.create_index(
            "ix_ar_stage_provider_invocations_status",
            source,
            ["status", "prepared_at"],
        )
        op.create_index(
            "ix_ar_stage_provider_invocations_operation",
            source,
            ["operation_ref", "phase"],
        )
    finally:
        connection.exec_driver_sql("PRAGMA legacy_alter_table=OFF")


def _replace_stage_run_requests() -> None:
    source = "ae_stage_run_requests"
    backup = "ae_stage_run_requests_pre_control"
    columns = (
        "request_ref",
        "cycle_ref",
        "stage",
        "epoch",
        "initialization_id",
        "quest_ref",
        "question_ref",
        "content_ref",
        "content_hash",
        "schema_ref",
        "content_receipt_ref",
        "content_receipt_hash",
        "question_receipt_ref",
        "question_receipt_hash",
        "context_pack_ref",
        "context_pack_json",
        "context_pack_hash",
        "idempotency_key",
        "request_hash",
        "receipt_ref",
        "receipt_hash",
        "created_at",
    )
    connection = op.get_bind()
    connection.exec_driver_sql("PRAGMA legacy_alter_table=ON")
    try:
        op.rename_table(source, backup)
        op.create_table(
            source,
            sa.Column("request_ref", sa.String(length=64), primary_key=True),
            sa.Column("cycle_ref", sa.String(length=64), nullable=False),
            sa.Column("stage", sa.String(length=24), nullable=False),
            sa.Column("epoch", sa.Integer(), nullable=False),
            sa.Column("initialization_id", sa.String(length=64), nullable=False),
            sa.Column("quest_ref", sa.String(length=64), nullable=False),
            sa.Column("question_ref", sa.String(length=64), nullable=False),
            sa.Column("content_ref", sa.String(length=64), nullable=False),
            sa.Column("content_hash", sa.String(length=64), nullable=False),
            sa.Column("schema_ref", sa.String(length=96), nullable=False),
            sa.Column("content_receipt_ref", sa.String(length=64), nullable=False),
            sa.Column("content_receipt_hash", sa.String(length=64), nullable=False),
            sa.Column("question_receipt_ref", sa.String(length=64), nullable=False),
            sa.Column("question_receipt_hash", sa.String(length=64), nullable=False),
            sa.Column(
                "context_pack_ref", sa.String(length=64), nullable=False, unique=True
            ),
            sa.Column("context_pack_json", sa.Text(), nullable=False),
            sa.Column("context_pack_hash", sa.String(length=64), nullable=False),
            sa.Column(
                "idempotency_key", sa.String(length=128), nullable=False, unique=True
            ),
            sa.Column("request_hash", sa.String(length=64), nullable=False),
            sa.Column("receipt_ref", sa.String(length=64), nullable=False, unique=True),
            sa.Column("receipt_hash", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.ForeignKeyConstraint(["cycle_ref"], ["ae_cycles.cycle_ref"]),
            sa.UniqueConstraint("cycle_ref", "stage", "epoch"),
            sa.CheckConstraint("stage IN ('idea', 'plan', 'bundle', 'reasoning')"),
            sa.CheckConstraint("epoch >= 1"),
            _hash("content_hash"),
            _hash("content_receipt_hash"),
            _hash("question_receipt_hash"),
            _hash("context_pack_hash"),
            _hash("request_hash"),
            _hash("receipt_hash"),
        )
        _copy_rows(backup, source, columns)
        op.drop_table(backup)
    finally:
        connection.exec_driver_sql("PRAGMA legacy_alter_table=OFF")


def _replace_stage_commits() -> None:
    source = "ae_stage_commits"
    backup = "ae_stage_commits_pre_control"
    legacy_columns = (
        "commit_ref",
        "request_ref",
        "cycle_ref",
        "stage",
        "epoch",
        "run_ref",
        "outcome_ref",
        "outcome_kind",
        "disposition",
        "run_completion_receipt_ref",
        "run_completion_receipt_hash",
        "outcome_receipt_ref",
        "outcome_receipt_hash",
        "closure_json",
        "closure_hash",
        "idempotency_key",
        "request_hash",
        "receipt_ref",
        "receipt_hash",
        "committed_at",
    )
    connection = op.get_bind()
    connection.exec_driver_sql("PRAGMA legacy_alter_table=ON")
    try:
        op.rename_table(source, backup)
        op.create_table(
            source,
            sa.Column("commit_ref", sa.String(length=64), primary_key=True),
            sa.Column("request_ref", sa.String(length=64), nullable=True, unique=True),
            sa.Column("cycle_ref", sa.String(length=64), nullable=False),
            sa.Column("stage", sa.String(length=24), nullable=False),
            sa.Column("epoch", sa.Integer(), nullable=False),
            sa.Column("run_ref", sa.String(length=96), nullable=True, unique=True),
            sa.Column("outcome_ref", sa.String(length=96), nullable=True),
            sa.Column("outcome_kind", sa.String(length=64), nullable=True),
            sa.Column("disposition", sa.String(length=16), nullable=False),
            sa.Column(
                "run_completion_receipt_ref", sa.String(length=96), nullable=True
            ),
            sa.Column(
                "run_completion_receipt_hash", sa.String(length=64), nullable=True
            ),
            sa.Column("outcome_receipt_ref", sa.String(length=96), nullable=True),
            sa.Column("outcome_receipt_hash", sa.String(length=64), nullable=True),
            sa.Column("closure_json", sa.Text(), nullable=True),
            sa.Column("closure_hash", sa.String(length=64), nullable=True),
            sa.Column("basis_kind", sa.String(length=64), nullable=True),
            sa.Column("basis_ref", sa.String(length=96), nullable=True),
            sa.Column("basis_receipt_issuer", sa.String(length=64), nullable=True),
            sa.Column("basis_receipt_kind", sa.String(length=96), nullable=True),
            sa.Column("basis_receipt_subject_ref", sa.String(length=96), nullable=True),
            sa.Column("basis_receipt_ref", sa.String(length=96), nullable=True),
            sa.Column("basis_receipt_hash", sa.String(length=64), nullable=True),
            sa.Column(
                "idempotency_key", sa.String(length=128), nullable=False, unique=True
            ),
            sa.Column("request_hash", sa.String(length=64), nullable=False),
            sa.Column("receipt_ref", sa.String(length=96), nullable=False, unique=True),
            sa.Column("receipt_hash", sa.String(length=64), nullable=False),
            sa.Column("committed_at", sa.Float(), nullable=False),
            sa.ForeignKeyConstraint(
                ["request_ref"], ["ae_stage_run_requests.request_ref"]
            ),
            sa.ForeignKeyConstraint(["run_ref"], ["ar_stage_runs.run_ref"]),
            sa.UniqueConstraint("stage", "outcome_ref"),
            sa.UniqueConstraint("cycle_ref", "stage", "epoch"),
            sa.CheckConstraint("stage IN ('idea', 'plan', 'bundle', 'reasoning')"),
            sa.CheckConstraint(
                "disposition IN ('completed', 'skipped', 'exhausted')"
            ),
            sa.CheckConstraint("epoch >= 1"),
            sa.CheckConstraint(
                "(stage != 'bundle' AND disposition = 'completed' AND "
                "request_ref IS NOT NULL AND run_ref IS NOT NULL AND outcome_ref "
                "IS NOT NULL AND outcome_kind IS NOT NULL AND "
                "run_completion_receipt_ref IS NOT NULL AND "
                "length(run_completion_receipt_hash) = 64 AND "
                "outcome_receipt_ref IS NOT NULL AND length(outcome_receipt_hash) "
                "= 64 AND closure_json IS NULL AND closure_hash IS NULL AND "
                "basis_kind IS NULL AND "
                "basis_ref IS NULL AND basis_receipt_issuer IS NULL AND "
                "basis_receipt_kind IS NULL AND basis_receipt_subject_ref IS NULL "
                "AND basis_receipt_ref IS NULL AND basis_receipt_hash IS NULL) OR "
                "(stage = 'bundle' AND disposition = 'completed' AND request_ref "
                "IS NOT NULL AND run_ref IS NOT NULL AND outcome_ref IS NOT NULL "
                "AND outcome_kind = 'target_graph' AND run_completion_receipt_ref "
                "IS NOT NULL AND length(run_completion_receipt_hash) = 64 AND "
                "outcome_receipt_ref IS NOT NULL AND length(outcome_receipt_hash) "
                "= 64 AND closure_json IS NOT NULL AND length(closure_hash) = 64 "
                "AND basis_kind IS NULL AND basis_ref IS NULL AND "
                "basis_receipt_issuer IS NULL AND basis_receipt_kind IS NULL AND "
                "basis_receipt_subject_ref IS NULL AND basis_receipt_ref IS NULL "
                "AND basis_receipt_hash IS NULL) OR (stage = 'bundle' AND "
                "disposition = 'skipped' AND request_ref IS NOT NULL AND run_ref "
                "IS NULL AND outcome_ref IS NOT NULL AND outcome_kind = "
                "'bundle_skip' AND run_completion_receipt_ref IS NULL AND "
                "run_completion_receipt_hash IS NULL AND outcome_receipt_ref IS "
                "NOT NULL AND length(outcome_receipt_hash) = 64 AND closure_json "
                "IS NOT NULL AND length(closure_hash) = 64 AND basis_kind IS NULL "
                "AND basis_ref IS NULL AND basis_receipt_issuer IS NULL AND "
                "basis_receipt_kind IS NULL AND basis_receipt_subject_ref IS NULL "
                "AND basis_receipt_ref IS NULL AND basis_receipt_hash IS NULL) OR "
                "(disposition = 'skipped' AND request_ref IS NULL AND run_ref IS "
                "NULL AND outcome_ref IS NULL AND "
                "outcome_kind IS NULL AND run_completion_receipt_ref IS NULL AND "
                "run_completion_receipt_hash IS NULL AND outcome_receipt_ref IS "
                "NULL AND outcome_receipt_hash IS NULL AND closure_json IS NULL "
                "AND closure_hash IS NULL AND basis_kind IS NOT NULL "
                "AND basis_ref IS NOT NULL AND basis_receipt_issuer IS NOT NULL AND "
                "basis_receipt_kind IS NOT NULL AND basis_receipt_subject_ref IS NOT "
                "NULL AND basis_receipt_ref IS NOT NULL AND "
                "length(basis_receipt_hash) = 64) OR (disposition = 'exhausted' "
                "AND request_ref IS NOT NULL AND run_ref IS NOT NULL AND "
                "outcome_ref IS NULL AND outcome_kind IS NULL AND "
                "run_completion_receipt_ref IS NOT NULL AND "
                "length(run_completion_receipt_hash) = 64 AND "
                "outcome_receipt_ref IS NULL AND outcome_receipt_hash IS NULL AND "
                "closure_json IS NULL AND closure_hash IS NULL AND "
                "basis_kind IS NOT NULL AND basis_ref IS NOT NULL AND "
                "basis_receipt_issuer IS NOT NULL AND basis_receipt_kind IS NOT "
                "NULL AND basis_receipt_subject_ref IS NOT NULL AND "
                "basis_receipt_ref IS NOT NULL AND length(basis_receipt_hash) = 64)"
            ),
            _hash("request_hash"),
            _hash("receipt_hash"),
        )
        column_list = ", ".join(legacy_columns)
        op.execute(
            f"INSERT INTO {source} ({column_list}) SELECT {column_list} FROM {backup}"
        )
        op.drop_table(backup)
    finally:
        connection.exec_driver_sql("PRAGMA legacy_alter_table=OFF")


def _replace_stage_runs() -> None:
    source = "ar_stage_runs"
    backup = "ar_stage_runs_pre_control"
    columns = (
        "run_ref",
        "request_ref",
        "cycle_ref",
        "stage",
        "epoch",
        "context_pack_ref",
        "context_pack_hash",
        "runtime_binding_json",
        "runtime_binding_hash",
        "request_receipt_ref",
        "request_receipt_hash",
        "status",
        "current_attempt_ref",
        "root_session_ref",
        "current_fence_ref",
        "completion_receipt_ref",
        "completion_receipt_hash",
        "outcome_ref",
        "admission_key",
        "admission_hash",
        "created_at",
        "updated_at",
    )
    connection = op.get_bind()
    connection.exec_driver_sql("PRAGMA legacy_alter_table=ON")
    try:
        op.drop_index("ix_ar_stage_runs_status", table_name=source)
        op.rename_table(source, backup)
        op.create_table(
            source,
            sa.Column("run_ref", sa.String(length=64), primary_key=True),
            sa.Column("request_ref", sa.String(length=64), nullable=False, unique=True),
            sa.Column("cycle_ref", sa.String(length=64), nullable=False),
            sa.Column("stage", sa.String(length=24), nullable=False),
            sa.Column("epoch", sa.Integer(), nullable=False),
            sa.Column("context_pack_ref", sa.String(length=64), nullable=False),
            sa.Column("context_pack_hash", sa.String(length=64), nullable=False),
            sa.Column("runtime_binding_json", sa.Text(), nullable=False),
            sa.Column("runtime_binding_hash", sa.String(length=64), nullable=False),
            sa.Column("request_receipt_ref", sa.String(length=64), nullable=False),
            sa.Column("request_receipt_hash", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=24), nullable=False),
            sa.Column(
                "current_attempt_ref", sa.String(length=64), nullable=False, unique=True
            ),
            sa.Column(
                "root_session_ref", sa.String(length=64), nullable=False, unique=True
            ),
            sa.Column(
                "current_fence_ref", sa.String(length=64), nullable=False, unique=True
            ),
            sa.Column(
                "completion_receipt_ref", sa.String(length=64), nullable=True, unique=True
            ),
            sa.Column("completion_receipt_hash", sa.String(length=64), nullable=True),
            sa.Column("outcome_ref", sa.String(length=96), nullable=True, unique=True),
            sa.Column(
                "admission_key", sa.String(length=128), nullable=False, unique=True
            ),
            sa.Column("admission_hash", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.Column("updated_at", sa.Float(), nullable=False),
            sa.ForeignKeyConstraint(
                ["request_ref"], ["ae_stage_run_requests.request_ref"]
            ),
            sa.CheckConstraint("stage IN ('idea', 'plan', 'bundle', 'reasoning')"),
            sa.CheckConstraint("epoch >= 1"),
            sa.CheckConstraint(
                "status IN ('running', 'awaiting_acceptance', 'completed')"
            ),
            _hash("context_pack_hash"),
            _hash("runtime_binding_hash"),
            _hash("request_receipt_hash"),
            _hash("admission_hash"),
            sa.CheckConstraint(
                "(status != 'completed' AND completion_receipt_ref IS NULL "
                "AND completion_receipt_hash IS NULL AND outcome_ref IS NULL) OR "
                "(status = 'completed' AND completion_receipt_ref IS NOT NULL "
                "AND length(completion_receipt_hash) = 64 AND outcome_ref IS NOT NULL)"
            ),
        )
        _copy_rows(backup, source, columns)
        op.drop_table(backup)
        op.create_index(
            "ix_ar_stage_runs_status", source, ["status", "updated_at"]
        )
    finally:
        connection.exec_driver_sql("PRAGMA legacy_alter_table=OFF")


def upgrade() -> None:
    _counter("advancement_engine_state", "control_operation_count")
    _counter("advancement_engine_state", "safe_point_count")
    _counter("agent_runtime_state", "control_operation_count")
    _counter("agent_runtime_state", "safe_point_count")
    _counter("agent_runtime_state", "fenced_attempt_count")
    _counter("research_graph_state", "question_prune_count")
    _counter("human_collaboration_state", "command_execution_count")

    # A provider invocation is Attempt/Fence-specific, while its external
    # operation identity survives a technical replacement. Existing responses
    # were keyed by invocation_ref, so that is the exact backfill identity.
    op.add_column(
        "ar_stage_provider_invocations",
        sa.Column("operation_ref", sa.String(length=96), nullable=True),
    )
    op.execute(
        "UPDATE ar_stage_provider_invocations SET operation_ref = invocation_ref "
        "WHERE operation_ref IS NULL"
    )
    _replace_stage_provider_invocations()

    op.create_table(
        "ae_cycles",
        sa.Column("cycle_ref", sa.String(length=64), primary_key=True),
        sa.Column("quest_ref", sa.String(length=64), nullable=False),
        sa.Column("question_ref", sa.String(length=64), nullable=False),
        sa.Column("question_receipt_ref", sa.String(length=96), nullable=False),
        sa.Column("question_receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("stage", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("predecessor_cycle_ref", sa.String(length=64), nullable=True),
        sa.Column("successor_cycle_ref", sa.String(length=64), nullable=True),
        sa.Column("suspension_reason", sa.String(length=96), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["predecessor_cycle_ref"], ["ae_cycles.cycle_ref"]),
        sa.ForeignKeyConstraint(["successor_cycle_ref"], ["ae_cycles.cycle_ref"]),
        sa.CheckConstraint("stage IN ('idea', 'plan', 'bundle', 'reasoning')"),
        sa.CheckConstraint("status IN ('ongoing', 'completed', 'abandoned')"),
        _hash("question_receipt_hash"),
    )
    op.create_index("ix_ae_cycles_quest", "ae_cycles", ["quest_ref", "status"])
    op.execute(
        "INSERT INTO ae_cycles (cycle_ref, quest_ref, question_ref, "
        "question_receipt_ref, question_receipt_hash, stage, status, created_at, "
        "updated_at) "
        "SELECT cycle_ref, quest_ref, question_ref, question_receipt_ref, "
        "question_receipt_hash, CASE WHEN EXISTS (SELECT 1 FROM ae_stage_commits "
        "commits WHERE commits.cycle_ref = ae_initial_cycles.cycle_ref AND "
        "commits.stage = 'bundle') THEN 'reasoning' WHEN EXISTS (SELECT 1 FROM "
        "ae_stage_commits commits WHERE commits.cycle_ref = "
        "ae_initial_cycles.cycle_ref AND commits.stage = 'plan') THEN 'bundle' "
        "WHEN EXISTS (SELECT 1 FROM ae_stage_commits commits WHERE "
        "commits.cycle_ref = ae_initial_cycles.cycle_ref AND commits.stage = "
        "'idea' AND commits.outcome_kind = 'no_viable_candidate') THEN "
        "'reasoning' WHEN EXISTS (SELECT 1 FROM "
        "ae_stage_commits commits WHERE commits.cycle_ref = "
        "ae_initial_cycles.cycle_ref AND commits.stage = 'idea') THEN 'plan' "
        "ELSE 'idea' END, 'ongoing', activated_at, activated_at "
        "FROM ae_initial_cycles"
    )
    _replace_stage_run_requests()
    _replace_stage_runs()
    _replace_stage_commits()

    op.create_table(
        "ae_foreground_heads",
        sa.Column("quest_ref", sa.String(length=64), primary_key=True),
        sa.Column("cycle_ref", sa.String(length=64), nullable=False),
        sa.Column("question_ref", sa.String(length=64), nullable=False),
        sa.Column("stage", sa.String(length=24), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("pending_operation_ref", sa.String(length=96), nullable=True),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["cycle_ref"], ["ae_cycles.cycle_ref"]),
        sa.CheckConstraint("epoch >= 1"),
        sa.CheckConstraint(
            "status IN ('active', 'pause_pending', 'suspended', 'switching', "
            "'completed', 'cancelled', 'abandoned', 'pruned')"
        ),
    )
    op.execute(
        "INSERT INTO ae_foreground_heads (quest_ref, cycle_ref, question_ref, "
        "stage, epoch, status, pending_operation_ref, updated_at) "
        "SELECT initial.quest_ref, initial.cycle_ref, initial.question_ref, CASE "
        "WHEN EXISTS (SELECT 1 FROM ae_stage_commits commits WHERE "
        "commits.cycle_ref = initial.cycle_ref AND commits.stage = 'bundle') THEN "
        "'reasoning' WHEN EXISTS (SELECT 1 FROM ae_stage_commits commits WHERE "
        "commits.cycle_ref = initial.cycle_ref AND commits.stage = 'plan') THEN "
        "'bundle' WHEN EXISTS (SELECT 1 FROM ae_stage_commits commits WHERE "
        "commits.cycle_ref = initial.cycle_ref AND commits.stage = 'idea' AND "
        "commits.outcome_kind = 'no_viable_candidate') THEN 'reasoning' WHEN "
        "EXISTS (SELECT 1 FROM ae_stage_commits commits WHERE "
        "commits.cycle_ref = initial.cycle_ref AND commits.stage = 'idea') THEN "
        "'plan' ELSE 'idea' END, 1, 'active', NULL, initial.activated_at FROM "
        "ae_initial_cycles initial"
    )

    op.create_table(
        "ae_foreground_grants",
        sa.Column("grant_ref", sa.String(length=96), primary_key=True),
        sa.Column("quest_ref", sa.String(length=64), nullable=False),
        sa.Column("cycle_ref", sa.String(length=64), nullable=False),
        sa.Column("question_ref", sa.String(length=64), nullable=False),
        sa.Column("stage", sa.String(length=24), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("predecessor_grant_ref", sa.String(length=96), nullable=True),
        sa.Column("safe_point_ref", sa.String(length=96), nullable=True),
        sa.Column("granted_at", sa.Float(), nullable=False),
        sa.Column("revoked_at", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["cycle_ref"], ["ae_cycles.cycle_ref"]),
        sa.ForeignKeyConstraint(
            ["predecessor_grant_ref"], ["ae_foreground_grants.grant_ref"]
        ),
        sa.UniqueConstraint("quest_ref", "epoch"),
        sa.CheckConstraint("epoch >= 1"),
        sa.CheckConstraint(
            "status IN ('active', 'suspended', 'revoked', 'completed', "
            "'cancelled', 'abandoned', 'pruned')"
        ),
    )
    op.create_index(
        "ix_ae_foreground_grants_quest",
        "ae_foreground_grants",
        ["quest_ref", "epoch"],
    )
    op.execute(
        "INSERT INTO ae_foreground_grants (grant_ref, quest_ref, cycle_ref, "
        "question_ref, stage, epoch, status, predecessor_grant_ref, "
        "safe_point_ref, granted_at, revoked_at) SELECT 'foreground_grant:' || "
        "initial.cycle_ref, initial.quest_ref, initial.cycle_ref, "
        "initial.question_ref, CASE WHEN EXISTS (SELECT 1 FROM ae_stage_commits "
        "commits WHERE commits.cycle_ref = initial.cycle_ref AND commits.stage = "
        "'bundle') THEN 'reasoning' WHEN EXISTS (SELECT 1 FROM ae_stage_commits "
        "commits WHERE commits.cycle_ref = initial.cycle_ref AND commits.stage = "
        "'plan') THEN 'bundle' WHEN EXISTS (SELECT 1 FROM ae_stage_commits commits "
        "WHERE commits.cycle_ref = initial.cycle_ref AND commits.stage = 'idea' "
        "AND commits.outcome_kind = 'no_viable_candidate') THEN 'reasoning' WHEN "
        "EXISTS (SELECT 1 FROM ae_stage_commits commits "
        "WHERE commits.cycle_ref = initial.cycle_ref AND commits.stage = 'idea') "
        "THEN 'plan' ELSE 'idea' END, 1, 'active', NULL, NULL, "
        "initial.activated_at, NULL FROM ae_initial_cycles initial"
    )

    op.create_table(
        "ae_control_operations",
        sa.Column("operation_ref", sa.String(length=96), primary_key=True),
        sa.Column("intent_id", sa.String(length=96), nullable=False, unique=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False, unique=True),
        sa.Column("action", sa.String(length=24), nullable=False),
        sa.Column("quest_ref", sa.String(length=64), nullable=False),
        sa.Column("source_cycle_ref", sa.String(length=64), nullable=False),
        sa.Column("source_epoch", sa.Integer(), nullable=False),
        sa.Column("source_stage", sa.String(length=24), nullable=False),
        sa.Column("target_question_ref", sa.String(length=64), nullable=True),
        sa.Column("target_cycle_ref", sa.String(length=64), nullable=True),
        sa.Column("target_question_receipt_ref", sa.String(length=96), nullable=True),
        sa.Column("target_question_receipt_hash", sa.String(length=64), nullable=True),
        sa.Column("command_json", sa.Text(), nullable=False),
        sa.Column("command_hash", sa.String(length=64), nullable=False),
        sa.Column("expected_revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("abort_reason_code", sa.String(length=96), nullable=True),
        sa.Column("runtime_receipt_json", sa.Text(), nullable=True),
        sa.Column("runtime_receipt_hash", sa.String(length=64), nullable=True),
        sa.Column("graph_receipt_json", sa.Text(), nullable=True),
        sa.Column("graph_receipt_hash", sa.String(length=64), nullable=True),
        sa.Column("safe_point_ref", sa.String(length=96), nullable=True),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("result_hash", sa.String(length=64), nullable=True),
        sa.Column("receipt_ref", sa.String(length=96), nullable=True, unique=True),
        sa.Column("receipt_hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.CheckConstraint("source_epoch >= 1"),
        sa.CheckConstraint("source_stage IN ('idea', 'plan', 'bundle', 'reasoning')"),
        sa.CheckConstraint(
            "action IN ('pause', 'resume', 'normal_switch', 'forced_switch', "
            "'cancel', 'abandon', 'prune', 'restore')"
        ),
        sa.CheckConstraint("status IN ('prepared', 'handoff_pending', 'completed', 'aborted')"),
        sa.CheckConstraint(
            "(status = 'aborted' AND abort_reason_code IS NOT NULL) OR "
            "(status != 'aborted' AND abort_reason_code IS NULL)"
        ),
        _hash("command_hash"),
        sa.CheckConstraint(
            "(target_question_receipt_hash IS NULL) OR "
            "length(target_question_receipt_hash) = 64"
        ),
        sa.CheckConstraint(
            "(status IN ('prepared', 'handoff_pending', 'aborted') AND result_json IS NULL AND result_hash IS NULL "
            "AND receipt_ref IS NULL AND receipt_hash IS NULL) OR "
            "(status = 'completed' AND result_json IS NOT NULL AND "
            "length(result_hash) = 64 AND receipt_ref IS NOT NULL AND "
            "length(receipt_hash) = 64)"
        ),
    )

    op.create_table(
        "ar_run_controls",
        sa.Column("run_ref", sa.String(length=96), primary_key=True),
        sa.Column("run_kind", sa.String(length=32), nullable=False),
        sa.Column("quest_ref", sa.String(length=96), nullable=True),
        sa.Column("cycle_ref", sa.String(length=64), nullable=True),
        sa.Column("epoch", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=28), nullable=False),
        sa.Column("attempt_ref", sa.String(length=96), nullable=True),
        sa.Column("root_session_ref", sa.String(length=96), nullable=True),
        sa.Column("fence_ref", sa.String(length=96), nullable=True),
        sa.Column("control_revision", sa.Integer(), nullable=False),
        sa.Column("safe_point_ref", sa.String(length=96), nullable=True),
        sa.Column("terminal_reason", sa.String(length=96), nullable=True),
        sa.Column("cleanup_status", sa.String(length=16), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.CheckConstraint("epoch IS NULL OR epoch >= 1"),
        sa.CheckConstraint("control_revision >= 1"),
        sa.CheckConstraint(
            "status IN ('running', 'suspended', 'suspended_fenced', "
            "'reconciliation_required', 'terminated', 'completed')"
        ),
        sa.CheckConstraint("cleanup_status IN ('none', 'pending', 'completed')"),
    )
    op.create_index(
        "ix_ar_run_controls_scope",
        "ar_run_controls",
        ["quest_ref", "cycle_ref", "status"],
    )
    op.execute(
        "INSERT INTO ar_run_controls (run_ref, run_kind, quest_ref, cycle_ref, "
        "epoch, status, attempt_ref, root_session_ref, fence_ref, control_revision, "
        "safe_point_ref, terminal_reason, cleanup_status, updated_at) SELECT "
        "r.run_ref, r.stage || '_stage', q.quest_ref, r.cycle_ref, r.epoch, "
        "CASE WHEN r.status = 'completed' THEN 'completed' ELSE 'running' END, "
        "r.current_attempt_ref, r.root_session_ref, r.current_fence_ref, 1, NULL, "
        "NULL, 'none', r.updated_at FROM ar_stage_runs r JOIN "
        "ae_stage_run_requests q ON q.request_ref = r.request_ref"
    )
    op.execute(
        "INSERT INTO ar_run_controls (run_ref, run_kind, quest_ref, cycle_ref, "
        "epoch, status, attempt_ref, root_session_ref, fence_ref, control_revision, "
        "safe_point_ref, terminal_reason, cleanup_status, updated_at) SELECT "
        "r.run_ref, 'deepfetch', m.quest_ref, NULL, NULL, CASE WHEN r.status = "
        "'executed' THEN 'completed' WHEN r.status IN ('failed', 'cancelled') THEN "
        "'terminated' ELSE 'running' END, r.current_attempt_ref, s.root_session_ref, "
        "a.fence_ref, 1, NULL, r.failure_code, 'none', r.updated_at FROM "
        "ar_deepfetch_runs r JOIN hc_manual_deepfetch_requests m ON "
        "m.request_ref = r.request_ref JOIN ar_deepfetch_sessions s ON "
        "s.run_ref = r.run_ref LEFT "
        "JOIN ar_deepfetch_attempts a ON a.attempt_ref = r.current_attempt_ref "
        "WHERE m.quest_ref IS NOT NULL"
    )
    op.execute(
        "INSERT INTO ar_run_controls (run_ref, run_kind, quest_ref, cycle_ref, "
        "epoch, status, attempt_ref, root_session_ref, fence_ref, control_revision, "
        "safe_point_ref, terminal_reason, cleanup_status, updated_at) SELECT "
        "r.run_ref, 'deepfetch', NULL, NULL, NULL, CASE WHEN r.status = 'executed' "
        "THEN 'completed' WHEN r.status IN ('failed', 'cancelled') THEN 'terminated' "
        "ELSE 'running' END, r.current_attempt_ref, s.root_session_ref, a.fence_ref, "
        "1, NULL, r.failure_code, 'none', r.updated_at FROM ar_deepfetch_runs r JOIN "
        "ar_deepfetch_sessions s ON s.run_ref = r.run_ref LEFT JOIN "
        "ar_deepfetch_attempts a ON a.attempt_ref = r.current_attempt_ref WHERE NOT "
        "EXISTS (SELECT 1 FROM ar_run_controls controls WHERE controls.run_ref = "
        "r.run_ref)"
    )
    op.execute(
        "INSERT INTO ar_run_controls (run_ref, run_kind, quest_ref, cycle_ref, "
        "epoch, status, attempt_ref, root_session_ref, fence_ref, control_revision, "
        "safe_point_ref, terminal_reason, cleanup_status, updated_at) SELECT "
        "run_ref, 'experiment', quest_ref, NULL, NULL, CASE WHEN status = 'executed' "
        "THEN 'completed' WHEN status = 'failed' THEN 'terminated' ELSE 'running' "
        "END, attempt_ref, root_session_ref, fence_ref, 1, NULL, failure_code, "
        "'none', updated_at FROM ar_experiment_runs"
    )

    op.create_table(
        "ar_provider_units",
        sa.Column("unit_ref", sa.String(length=96), primary_key=True),
        sa.Column("operation_ref", sa.String(length=128), nullable=False),
        sa.Column("run_ref", sa.String(length=96), nullable=False),
        sa.Column("attempt_ref", sa.String(length=96), nullable=True),
        sa.Column("fence_ref", sa.String(length=96), nullable=True),
        sa.Column("unit_kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.Float(), nullable=False),
        sa.Column("completed_at", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["run_ref"], ["ar_run_controls.run_ref"]),
        sa.CheckConstraint(
            "unit_kind IN ('idea_primary', 'idea_review', 'plan_primary', "
            "'plan_review', 'bundle_primary', 'bundle_review', 'deepfetch', "
            "'experiment')"
        ),
        sa.CheckConstraint(
            "status IN ('active', 'revocation_pending', 'completed', 'revoked')"
        ),
        sa.CheckConstraint(
            "(status IN ('active', 'revocation_pending') AND completed_at IS NULL) OR "
            "(status IN ('completed', 'revoked') AND completed_at IS NOT NULL)"
        ),
    )
    op.create_index(
        "ix_ar_provider_units_active",
        "ar_provider_units",
        ["run_ref", "status"],
    )
    op.create_table(
        "ar_stage_attempt_replacements",
        sa.Column(
            "replacement_attempt_ref", sa.String(length=96), primary_key=True
        ),
        sa.Column("run_ref", sa.String(length=96), nullable=False),
        sa.Column("retired_attempt_ref", sa.String(length=96), nullable=False),
        sa.Column("reason_code", sa.String(length=96), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["replacement_attempt_ref"], ["ar_stage_attempts.attempt_ref"]
        ),
        sa.ForeignKeyConstraint(
            ["retired_attempt_ref"], ["ar_stage_attempts.attempt_ref"]
        ),
        sa.UniqueConstraint("retired_attempt_ref"),
    )
    op.create_table(
        "ar_stage_run_rebindings",
        sa.Column("rebind_ref", sa.String(length=96), primary_key=True),
        sa.Column("run_ref", sa.String(length=96), nullable=False),
        sa.Column("cycle_ref", sa.String(length=64), nullable=False),
        sa.Column("stage", sa.String(length=24), nullable=False),
        sa.Column("old_request_ref", sa.String(length=96), nullable=False),
        sa.Column("old_epoch", sa.Integer(), nullable=False),
        sa.Column("new_request_ref", sa.String(length=96), nullable=False, unique=True),
        sa.Column("new_epoch", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["run_ref"], ["ar_stage_runs.run_ref"]),
        sa.CheckConstraint("stage IN ('idea', 'plan', 'bundle', 'reasoning')"),
        sa.CheckConstraint("old_epoch >= 1 AND new_epoch > old_epoch"),
    )

    op.create_table(
        "ar_safe_points",
        sa.Column("safe_point_ref", sa.String(length=96), primary_key=True),
        sa.Column("operation_ref", sa.String(length=96), nullable=False),
        sa.Column("run_ref", sa.String(length=96), nullable=False),
        sa.Column("attempt_ref", sa.String(length=96), nullable=True),
        sa.Column("root_session_ref", sa.String(length=96), nullable=True),
        sa.Column("fence_ref", sa.String(length=96), nullable=True),
        sa.Column("checkpoint_json", sa.Text(), nullable=False),
        sa.Column("checkpoint_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["run_ref"], ["ar_run_controls.run_ref"]),
        sa.UniqueConstraint("operation_ref", "run_ref"),
        _hash("checkpoint_hash"),
    )
    op.create_table(
        "ar_fence_revocations",
        sa.Column("fence_ref", sa.String(length=96), primary_key=True),
        sa.Column("operation_ref", sa.String(length=96), nullable=False),
        sa.Column("run_ref", sa.String(length=96), nullable=False),
        sa.Column("attempt_ref", sa.String(length=96), nullable=True),
        sa.Column("reason_code", sa.String(length=96), nullable=False),
        sa.Column("revoked_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["run_ref"], ["ar_run_controls.run_ref"]),
    )
    op.create_table(
        "ar_control_operations",
        sa.Column("operation_ref", sa.String(length=96), primary_key=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False, unique=True),
        sa.Column("action", sa.String(length=24), nullable=False),
        sa.Column("quest_ref", sa.String(length=96), nullable=False),
        sa.Column("cycle_ref", sa.String(length=64), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("target_scope", sa.String(length=16), nullable=False),
        sa.Column("run_ref", sa.String(length=96), nullable=True),
        sa.Column("source_stage", sa.String(length=24), nullable=True),
        sa.Column("affected_question_refs_json", sa.Text(), nullable=False),
        sa.Column("affected_question_refs_hash", sa.String(length=64), nullable=False),
        sa.Column("command_hash", sa.String(length=64), nullable=False),
        sa.Column("expected_revision", sa.Integer(), nullable=False),
        sa.Column("affected_runs_json", sa.Text(), nullable=False),
        sa.Column("affected_runs_hash", sa.String(length=64), nullable=False),
        sa.Column("safe_points_json", sa.Text(), nullable=False),
        sa.Column("safe_points_hash", sa.String(length=64), nullable=False),
        sa.Column("receipt_ref", sa.String(length=96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.CheckConstraint("epoch >= 1"),
        sa.CheckConstraint("target_scope IN ('cycle', 'stage', 'run')"),
        sa.CheckConstraint(
            "source_stage IS NULL OR source_stage IN "
            "('idea', 'plan', 'bundle', 'reasoning')"
        ),
        sa.CheckConstraint(
            "(target_scope = 'run' AND run_ref IS NOT NULL) OR "
            "(target_scope != 'run' AND run_ref IS NULL)"
        ),
        _hash("command_hash"),
        _hash("affected_question_refs_hash"),
        _hash("affected_runs_hash"),
        _hash("safe_points_hash"),
        _hash("receipt_hash"),
    )
    op.create_table(
        "ar_control_reservations",
        sa.Column("operation_ref", sa.String(length=96), primary_key=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False, unique=True),
        sa.Column("action", sa.String(length=24), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("expected_revision", sa.Integer(), nullable=False),
        sa.Column("source_stage", sa.String(length=24), nullable=True),
        sa.Column("affected_question_refs_json", sa.Text(), nullable=False),
        sa.Column("affected_question_refs_hash", sa.String(length=64), nullable=False),
        sa.Column("affected_runs_json", sa.Text(), nullable=False),
        sa.Column("affected_runs_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.CheckConstraint("status IN ('prepared', 'applied', 'aborted')"),
        sa.CheckConstraint(
            "source_stage IS NULL OR source_stage IN "
            "('idea', 'plan', 'bundle', 'reasoning')"
        ),
        _hash("payload_hash"),
        _hash("affected_question_refs_hash"),
        _hash("affected_runs_hash"),
    )
    op.create_table(
        "ar_control_compensations",
        sa.Column("operation_ref", sa.String(length=96), primary_key=True),
        sa.Column("reason_code", sa.String(length=96), nullable=False),
        sa.Column("affected_runs_json", sa.Text(), nullable=False),
        sa.Column("affected_runs_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        _hash("affected_runs_hash"),
    )

    op.create_table(
        "rg_graph_heads",
        sa.Column("quest_ref", sa.String(length=64), primary_key=True),
        sa.Column("graph_version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.CheckConstraint("graph_version >= 0"),
    )
    op.execute(
        "INSERT INTO rg_graph_heads (quest_ref, graph_version, updated_at) "
        "SELECT quests.quest_ref, COALESCE((SELECT COUNT(*) FROM (SELECT "
        "question_ref, quest_ref FROM rg_questions UNION ALL SELECT question_ref, "
        "quest_ref FROM rg_manual_questions) questions WHERE questions.quest_ref = "
        "quests.quest_ref), 0), quests.accepted_at FROM rg_quests quests"
    )

    op.create_table(
        "rg_question_lifecycle",
        sa.Column("question_ref", sa.String(length=64), primary_key=True),
        sa.Column("quest_ref", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.CheckConstraint("status IN ('active', 'pruned')"),
        sa.CheckConstraint("revision >= 1"),
    )
    op.execute(
        "INSERT INTO rg_question_lifecycle (question_ref, quest_ref, status, "
        "revision, updated_at) SELECT question_ref, quest_ref, 'active', 1, "
        "accepted_at FROM rg_questions"
    )
    op.execute(
        "INSERT INTO rg_question_lifecycle (question_ref, quest_ref, status, "
        "revision, updated_at) SELECT question_ref, quest_ref, 'active', 1, "
        "accepted_at FROM rg_manual_questions"
    )
    op.create_table(
        "rg_question_lifecycle_commands",
        sa.Column("idempotency_key", sa.String(length=128), primary_key=True),
        sa.Column("operation_ref", sa.String(length=96), nullable=False, unique=True),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("question_ref", sa.String(length=64), nullable=False),
        sa.Column("record_ref", sa.String(length=96), nullable=False, unique=True),
        sa.Column("prune_record_ref", sa.String(length=96), nullable=True),
        sa.Column("base_version", sa.Integer(), nullable=False),
        sa.Column("committed_version", sa.Integer(), nullable=False),
        sa.Column("runtime_receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("affected_refs_json", sa.Text(), nullable=False),
        sa.Column("affected_refs_hash", sa.String(length=64), nullable=False),
        sa.Column("receipt_ref", sa.String(length=96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("recorded_at", sa.Float(), nullable=False),
        sa.CheckConstraint("action IN ('prune', 'restore')"),
        sa.CheckConstraint("base_version >= 0"),
        sa.CheckConstraint("committed_version = base_version + 1"),
        sa.CheckConstraint(
            "(action = 'prune' AND prune_record_ref IS NULL) OR "
            "(action = 'restore' AND prune_record_ref IS NOT NULL)"
        ),
        _hash("runtime_receipt_hash"),
        _hash("request_hash"),
        _hash("affected_refs_hash"),
        _hash("receipt_hash"),
    )
    op.create_table(
        "rg_question_control_reservations",
        sa.Column("operation_ref", sa.String(length=96), primary_key=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False, unique=True),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("expected_revision", sa.Integer(), nullable=False),
        sa.Column("graph_version", sa.Integer(), nullable=False),
        sa.Column("affected_refs_json", sa.Text(), nullable=False),
        sa.Column("affected_refs_hash", sa.String(length=64), nullable=False),
        sa.Column("lifecycle_json", sa.Text(), nullable=False),
        sa.Column("lifecycle_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.CheckConstraint("action IN ('prune', 'restore')"),
        sa.CheckConstraint("graph_version >= 0"),
        sa.CheckConstraint("status IN ('prepared', 'applied', 'aborted')"),
        _hash("payload_hash"),
        _hash("affected_refs_hash"),
        _hash("lifecycle_hash"),
    )

    op.create_table(
        "rg_prune_records",
        sa.Column("prune_record_ref", sa.String(length=96), primary_key=True),
        sa.Column("operation_ref", sa.String(length=96), nullable=False, unique=True),
        sa.Column("quest_ref", sa.String(length=64), nullable=False),
        sa.Column("root_question_ref", sa.String(length=64), nullable=False),
        sa.Column("base_version", sa.Integer(), nullable=False),
        sa.Column("committed_version", sa.Integer(), nullable=False),
        sa.Column("affected_refs_json", sa.Text(), nullable=False),
        sa.Column("affected_refs_hash", sa.String(length=64), nullable=False),
        sa.Column("runtime_receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("receipt_ref", sa.String(length=96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.CheckConstraint("base_version >= 0"),
        sa.CheckConstraint("committed_version = base_version + 1"),
        _hash("affected_refs_hash"),
        _hash("runtime_receipt_hash"),
        _hash("receipt_hash"),
    )
    op.create_table(
        "rg_restore_records",
        sa.Column("restore_record_ref", sa.String(length=96), primary_key=True),
        sa.Column("operation_ref", sa.String(length=96), nullable=False, unique=True),
        sa.Column("prune_record_ref", sa.String(length=96), nullable=False),
        sa.Column("quest_ref", sa.String(length=64), nullable=False),
        sa.Column("root_question_ref", sa.String(length=64), nullable=False),
        sa.Column("base_version", sa.Integer(), nullable=False),
        sa.Column("committed_version", sa.Integer(), nullable=False),
        sa.Column("affected_refs_json", sa.Text(), nullable=False),
        sa.Column("affected_refs_hash", sa.String(length=64), nullable=False),
        sa.Column("receipt_ref", sa.String(length=96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["prune_record_ref"], ["rg_prune_records.prune_record_ref"]
        ),
        sa.CheckConstraint("base_version >= 0"),
        sa.CheckConstraint("committed_version = base_version + 1"),
        _hash("affected_refs_hash"),
        _hash("receipt_hash"),
    )

    op.create_table(
        "hc_command_executions",
        sa.Column("execution_ref", sa.String(length=96), primary_key=True),
        sa.Column("intent_id", sa.String(length=96), nullable=False, unique=True),
        sa.Column("confirmation_ref", sa.String(length=96), nullable=False, unique=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False, unique=True),
        sa.Column("command_hash", sa.String(length=64), nullable=False),
        sa.Column("owner_receipts_json", sa.Text(), nullable=False),
        sa.Column("owner_receipts_hash", sa.String(length=64), nullable=False),
        sa.Column("receipt_ref", sa.String(length=96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.CheckConstraint("status = 'completed'"),
        _hash("command_hash"),
        _hash("owner_receipts_hash"),
        _hash("receipt_hash"),
    )
    op.create_table(
        "hc_control_sagas",
        sa.Column("intent_id", sa.String(length=96), primary_key=True),
        sa.Column("confirmation_ref", sa.String(length=96), nullable=False, unique=True),
        sa.Column("operation_ref", sa.String(length=96), nullable=False, unique=True),
        sa.Column("action", sa.String(length=24), nullable=False),
        sa.Column("target_scope", sa.String(length=16), nullable=False),
        sa.Column("command_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("runtime_receipt_json", sa.Text(), nullable=True),
        sa.Column("runtime_receipt_hash", sa.String(length=64), nullable=True),
        sa.Column("graph_receipt_json", sa.Text(), nullable=True),
        sa.Column("graph_receipt_hash", sa.String(length=64), nullable=True),
        sa.Column("advancement_receipt_json", sa.Text(), nullable=True),
        sa.Column("advancement_receipt_hash", sa.String(length=64), nullable=True),
        sa.Column("last_error", sa.String(length=96), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.CheckConstraint("target_scope IN ('cycle', 'stage', 'run')"),
        sa.CheckConstraint(
            "status IN ('preparing', 'prepared', 'runtime_applied', "
            "'graph_applied', 'advancement_applied', 'compensated', "
            "'completed', 'aborted')"
        ),
        _hash("command_hash"),
        sa.CheckConstraint(
            "runtime_receipt_hash IS NULL OR length(runtime_receipt_hash) = 64"
        ),
        sa.CheckConstraint(
            "graph_receipt_hash IS NULL OR length(graph_receipt_hash) = 64"
        ),
        sa.CheckConstraint(
            "advancement_receipt_hash IS NULL OR "
            "length(advancement_receipt_hash) = 64"
        ),
    )


def downgrade() -> None:
    raise RuntimeError("vNext production migrations are forward-only")
