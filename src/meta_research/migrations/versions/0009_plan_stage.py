"""Deepen the shared Stage authorities for the production Plan Stage.

Revision ID: 0009_plan_stage
Revises: 0008_quest_acquisition_session
Create Date: 2026-08-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision = "0009_plan_stage"
down_revision = "0008_quest_acquisition_session"
branch_labels = None
depends_on = None


def _counter(name: str) -> sa.Column:
    return sa.Column(name, sa.Integer(), nullable=False, server_default="0")


def _hash_check(name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"length({name}) = 64")


def _copy_rows(source: str, target: str, columns: Sequence[str]) -> None:
    column_list = ", ".join(columns)
    op.get_bind().execute(
        sa.text(
            f"INSERT INTO {target} ({column_list}) "
            f"SELECT {column_list} FROM {source}"
        )
    )


def _replace_stage_run_requests() -> None:
    source = "ae_stage_run_requests"
    backup = "ae_stage_run_requests_pre_plan"
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
        sa.Column("context_pack_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("context_pack_json", sa.Text(), nullable=False),
        sa.Column("context_pack_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("receipt_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["cycle_ref"], ["ae_initial_cycles.cycle_ref"]),
        sa.ForeignKeyConstraint(["question_ref"], ["rg_questions.question_ref"]),
        sa.ForeignKeyConstraint(
            ["content_ref"], ["rm_formal_question_contents.content_ref"]
        ),
        sa.UniqueConstraint("cycle_ref", "stage"),
        sa.CheckConstraint("stage IN ('idea', 'plan')"),
        sa.CheckConstraint("epoch >= 1"),
        _hash_check("content_hash"),
        _hash_check("content_receipt_hash"),
        _hash_check("question_receipt_hash"),
        _hash_check("context_pack_hash"),
        _hash_check("request_hash"),
        _hash_check("receipt_hash"),
    )
    _copy_rows(backup, source, columns)
    op.drop_table(backup)


def _replace_stage_runs() -> None:
    source = "ar_stage_runs"
    backup = "ar_stage_runs_pre_plan"
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
        sa.Column("outcome_ref", sa.String(length=64), nullable=True, unique=True),
        sa.Column("admission_key", sa.String(length=128), nullable=False, unique=True),
        sa.Column("admission_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["request_ref"], ["ae_stage_run_requests.request_ref"]
        ),
        sa.CheckConstraint("stage IN ('idea', 'plan')"),
        sa.CheckConstraint("epoch >= 1"),
        sa.CheckConstraint(
            "status IN ('running', 'awaiting_acceptance', 'completed')"
        ),
        _hash_check("context_pack_hash"),
        _hash_check("runtime_binding_hash"),
        _hash_check("request_receipt_hash"),
        _hash_check("admission_hash"),
        sa.CheckConstraint(
            "(status != 'completed' AND completion_receipt_ref IS NULL "
            "AND completion_receipt_hash IS NULL AND outcome_ref IS NULL) OR "
            "(status = 'completed' AND completion_receipt_ref IS NOT NULL "
            "AND completion_receipt_hash IS NOT NULL "
            "AND length(completion_receipt_hash) = 64 AND outcome_ref IS NOT NULL)"
        ),
    )
    _copy_rows(backup, source, columns)
    op.drop_table(backup)
    op.create_index(
        "ix_ar_stage_runs_status", source, ["status", "updated_at"]
    )


def _rename_stage_provider_invocations() -> None:
    old_table = "ar_idea_provider_invocations"
    new_table = "ar_stage_provider_invocations"
    op.drop_index(
        "ix_ar_idea_provider_invocations_status", table_name=old_table
    )
    op.rename_table(old_table, new_table)
    op.create_index(
        "ix_ar_stage_provider_invocations_status",
        new_table,
        ["status", "prepared_at"],
    )


def _replace_stage_commits() -> None:
    source = "ae_stage_commits"
    backup = "ae_stage_commits_pre_plan"
    columns = (
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
        "idempotency_key",
        "request_hash",
        "receipt_ref",
        "receipt_hash",
        "committed_at",
    )
    op.rename_table(source, backup)
    op.create_table(
        source,
        sa.Column("commit_ref", sa.String(length=64), primary_key=True),
        sa.Column("request_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("cycle_ref", sa.String(length=64), nullable=False),
        sa.Column("stage", sa.String(length=24), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("run_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("outcome_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("outcome_kind", sa.String(length=32), nullable=False),
        sa.Column("disposition", sa.String(length=16), nullable=False),
        sa.Column(
            "run_completion_receipt_ref", sa.String(length=64), nullable=False
        ),
        sa.Column(
            "run_completion_receipt_hash", sa.String(length=64), nullable=False
        ),
        sa.Column("outcome_receipt_ref", sa.String(length=64), nullable=False),
        sa.Column("outcome_receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("receipt_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("committed_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["request_ref"], ["ae_stage_run_requests.request_ref"]
        ),
        sa.ForeignKeyConstraint(["run_ref"], ["ar_stage_runs.run_ref"]),
        sa.CheckConstraint("stage IN ('idea', 'plan')"),
        sa.CheckConstraint(
            "outcome_kind IN ('idea_set', 'no_viable_candidate', 'formal_plan')"
        ),
        sa.CheckConstraint("disposition = 'completed'"),
        sa.CheckConstraint("epoch >= 1"),
        _hash_check("run_completion_receipt_hash"),
        _hash_check("outcome_receipt_hash"),
        _hash_check("request_hash"),
        _hash_check("receipt_hash"),
    )
    _copy_rows(backup, source, columns)
    op.drop_table(backup)


def _rebuild_shared_stage_authorities() -> None:
    connection = op.get_bind()
    # Keep dependent child-table foreign keys pointing at the stable public
    # names while SQLite rebuilds only the three authorities whose checks
    # actually change.  Rebuilding every child would reject pre-existing
    # legacy/orphan rows that earlier forward-only migrations intentionally
    # preserve, even though those rows are unrelated to Plan.
    connection.exec_driver_sql("PRAGMA legacy_alter_table=ON")
    try:
        _replace_stage_run_requests()
        _replace_stage_runs()
        _rename_stage_provider_invocations()
        _replace_stage_commits()
    finally:
        connection.exec_driver_sql("PRAGMA legacy_alter_table=OFF")


def _create_plan_document_table() -> None:
    op.create_table(
        "rm_plan_documents",
        sa.Column("content_ref", sa.String(length=64), primary_key=True),
        sa.Column("request_ref", sa.String(length=64), nullable=False),
        sa.Column("run_ref", sa.String(length=64), nullable=False),
        sa.Column("attempt_ref", sa.String(length=64), nullable=False),
        sa.Column("fence_ref", sa.String(length=64), nullable=False),
        sa.Column("submission_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("initialization_id", sa.String(length=64), nullable=False),
        sa.Column("quest_ref", sa.String(length=64), nullable=False),
        sa.Column("question_ref", sa.String(length=64), nullable=False),
        sa.Column("context_pack_ref", sa.String(length=64), nullable=False),
        sa.Column("question_content_ref", sa.String(length=64), nullable=False),
        sa.Column("question_content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "question_content_receipt_ref", sa.String(length=64), nullable=False
        ),
        sa.Column(
            "question_content_receipt_hash", sa.String(length=64), nullable=False
        ),
        sa.Column("question_receipt_ref", sa.String(length=64), nullable=False),
        sa.Column("question_receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("idea_outcome_ref", sa.String(length=64), nullable=False),
        sa.Column("idea_outcome_receipt_ref", sa.String(length=64), nullable=False),
        sa.Column("idea_outcome_receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("idea_content_ref", sa.String(length=64), nullable=False),
        sa.Column("idea_content_hash", sa.String(length=64), nullable=False),
        sa.Column("idea_content_receipt_ref", sa.String(length=64), nullable=False),
        sa.Column("idea_content_receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("idea_stage_commit_ref", sa.String(length=64), nullable=False),
        sa.Column(
            "idea_stage_commit_receipt_ref", sa.String(length=64), nullable=False
        ),
        sa.Column(
            "idea_stage_commit_receipt_hash", sa.String(length=64), nullable=False
        ),
        sa.Column("plan_document_json", sa.Text(), nullable=False),
        sa.Column("plan_document_hash", sa.String(length=64), nullable=False),
        sa.Column("answer_contract_hash", sa.String(length=64), nullable=False),
        sa.Column("reviewed_draft_json", sa.Text(), nullable=False),
        sa.Column("reviewed_draft_hash", sa.String(length=64), nullable=False),
        sa.Column("review_json", sa.Text(), nullable=False),
        sa.Column("review_hash", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("object_path", sa.Text(), nullable=False),
        sa.Column("execution_receipt_ref", sa.String(length=64), nullable=False),
        sa.Column("execution_receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("receipt_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["request_ref"], ["ae_stage_run_requests.request_ref"]
        ),
        sa.ForeignKeyConstraint(["run_ref"], ["ar_stage_runs.run_ref"]),
        sa.ForeignKeyConstraint(["attempt_ref"], ["ar_stage_attempts.attempt_ref"]),
        sa.ForeignKeyConstraint(["fence_ref"], ["ar_execution_fences.fence_ref"]),
        sa.ForeignKeyConstraint(
            ["submission_ref"], ["ar_stage_attempts.submission_ref"]
        ),
        sa.ForeignKeyConstraint(["question_ref"], ["rg_questions.question_ref"]),
        sa.ForeignKeyConstraint(
            ["question_content_ref"], ["rm_formal_question_contents.content_ref"]
        ),
        sa.ForeignKeyConstraint(
            ["idea_outcome_ref"], ["rg_idea_outcome_decisions.outcome_ref"]
        ),
        sa.ForeignKeyConstraint(
            ["idea_content_ref"], ["rm_idea_outcome_contents.content_ref"]
        ),
        sa.ForeignKeyConstraint(
            ["idea_stage_commit_ref"], ["ae_stage_commits.commit_ref"]
        ),
        _hash_check("question_content_hash"),
        _hash_check("question_content_receipt_hash"),
        _hash_check("question_receipt_hash"),
        _hash_check("idea_outcome_receipt_hash"),
        _hash_check("idea_content_hash"),
        _hash_check("idea_content_receipt_hash"),
        _hash_check("idea_stage_commit_receipt_hash"),
        _hash_check("plan_document_hash"),
        _hash_check("answer_contract_hash"),
        _hash_check("reviewed_draft_hash"),
        _hash_check("review_hash"),
        _hash_check("payload_hash"),
        _hash_check("execution_receipt_hash"),
        _hash_check("receipt_hash"),
    )
    op.create_index(
        "ix_rm_plan_documents_request",
        "rm_plan_documents",
        ["request_ref", "accepted_at"],
    )


def _create_formal_plan_decision_table() -> None:
    op.create_table(
        "rg_formal_plan_decisions",
        sa.Column("decision_ref", sa.String(length=64), primary_key=True),
        sa.Column("request_ref", sa.String(length=64), nullable=False),
        sa.Column("submission_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("run_ref", sa.String(length=64), nullable=False),
        sa.Column("attempt_ref", sa.String(length=64), nullable=False),
        sa.Column("fence_ref", sa.String(length=64), nullable=False),
        sa.Column("initialization_id", sa.String(length=64), nullable=False),
        sa.Column("quest_ref", sa.String(length=64), nullable=False),
        sa.Column("question_ref", sa.String(length=64), nullable=False),
        sa.Column("context_pack_ref", sa.String(length=64), nullable=False),
        sa.Column("question_content_ref", sa.String(length=64), nullable=False),
        sa.Column("question_content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "question_content_receipt_ref", sa.String(length=64), nullable=False
        ),
        sa.Column(
            "question_content_receipt_hash", sa.String(length=64), nullable=False
        ),
        sa.Column("question_receipt_ref", sa.String(length=64), nullable=False),
        sa.Column("question_receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("idea_outcome_ref", sa.String(length=64), nullable=False),
        sa.Column("idea_outcome_receipt_ref", sa.String(length=64), nullable=False),
        sa.Column("idea_outcome_receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("idea_content_ref", sa.String(length=64), nullable=False),
        sa.Column("idea_content_hash", sa.String(length=64), nullable=False),
        sa.Column("idea_content_receipt_ref", sa.String(length=64), nullable=False),
        sa.Column("idea_content_receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("idea_stage_commit_ref", sa.String(length=64), nullable=False),
        sa.Column(
            "idea_stage_commit_receipt_ref", sa.String(length=64), nullable=False
        ),
        sa.Column(
            "idea_stage_commit_receipt_hash", sa.String(length=64), nullable=False
        ),
        sa.Column("plan_content_ref", sa.String(length=64), nullable=False),
        sa.Column("plan_content_receipt_ref", sa.String(length=64), nullable=False),
        sa.Column("plan_content_receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("execution_receipt_ref", sa.String(length=64), nullable=False),
        sa.Column("execution_receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("plan_document_hash", sa.String(length=64), nullable=False),
        sa.Column("answer_contract_hash", sa.String(length=64), nullable=False),
        sa.Column("reviewed_draft_hash", sa.String(length=64), nullable=False),
        sa.Column("review_hash", sa.String(length=64), nullable=False),
        sa.Column("bundle_disposition", sa.String(length=40), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("formal_plan_ref", sa.String(length=64), nullable=True, unique=True),
        sa.Column("reason_code", sa.String(length=96), nullable=True),
        sa.Column("feedback_json", sa.Text(), nullable=False),
        sa.Column("feedback_hash", sa.String(length=64), nullable=False),
        sa.Column("receipt_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("decided_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["request_ref"], ["ae_stage_run_requests.request_ref"]
        ),
        sa.ForeignKeyConstraint(["run_ref"], ["ar_stage_runs.run_ref"]),
        sa.ForeignKeyConstraint(["attempt_ref"], ["ar_stage_attempts.attempt_ref"]),
        sa.ForeignKeyConstraint(["fence_ref"], ["ar_execution_fences.fence_ref"]),
        sa.ForeignKeyConstraint(["quest_ref"], ["rg_quests.quest_ref"]),
        sa.ForeignKeyConstraint(["question_ref"], ["rg_questions.question_ref"]),
        sa.ForeignKeyConstraint(
            ["question_content_ref"], ["rm_formal_question_contents.content_ref"]
        ),
        sa.ForeignKeyConstraint(
            ["idea_outcome_ref"], ["rg_idea_outcome_decisions.outcome_ref"]
        ),
        sa.ForeignKeyConstraint(
            ["idea_content_ref"], ["rm_idea_outcome_contents.content_ref"]
        ),
        sa.ForeignKeyConstraint(
            ["idea_stage_commit_ref"], ["ae_stage_commits.commit_ref"]
        ),
        sa.ForeignKeyConstraint(
            ["plan_content_ref"], ["rm_plan_documents.content_ref"]
        ),
        sa.ForeignKeyConstraint(
            ["submission_ref"], ["rm_plan_documents.submission_ref"]
        ),
        sa.CheckConstraint("decision IN ('accepted', 'rejected')"),
        sa.CheckConstraint(
            "bundle_disposition IN "
            "('experiments_required', 'no_new_experiment_required')"
        ),
        _hash_check("question_content_hash"),
        _hash_check("question_content_receipt_hash"),
        _hash_check("question_receipt_hash"),
        _hash_check("idea_outcome_receipt_hash"),
        _hash_check("idea_content_hash"),
        _hash_check("idea_content_receipt_hash"),
        _hash_check("idea_stage_commit_receipt_hash"),
        _hash_check("plan_content_receipt_hash"),
        _hash_check("execution_receipt_hash"),
        _hash_check("payload_hash"),
        _hash_check("plan_document_hash"),
        _hash_check("answer_contract_hash"),
        _hash_check("reviewed_draft_hash"),
        _hash_check("review_hash"),
        _hash_check("feedback_hash"),
        _hash_check("receipt_hash"),
        sa.CheckConstraint(
            "(decision = 'accepted' AND formal_plan_ref IS NOT NULL "
            "AND reason_code IS NULL) OR "
            "(decision = 'rejected' AND formal_plan_ref IS NULL "
            "AND reason_code IS NOT NULL)"
        ),
    )
    op.create_index(
        "uq_rg_formal_plan_one_accepted_per_request",
        "rg_formal_plan_decisions",
        ["request_ref"],
        unique=True,
        sqlite_where=sa.text("decision = 'accepted'"),
    )
    op.create_index(
        "ix_rg_formal_plan_request_decided",
        "rg_formal_plan_decisions",
        ["request_ref", "decided_at"],
    )


def upgrade() -> None:
    op.add_column("research_memory_state", _counter("plan_content_count"))
    op.add_column("research_graph_state", _counter("formal_plan_count"))
    op.add_column("research_graph_state", _counter("plan_rejection_count"))

    _rebuild_shared_stage_authorities()
    _create_plan_document_table()
    _create_formal_plan_decision_table()


def downgrade() -> None:
    raise RuntimeError("vNext production migrations are forward-only")
