"""Add durable Idea Stage Owner state and signed acceptance records.

Revision ID: 0004_idea_stage
Revises: 0003_quest_direct_web
Create Date: 2026-08-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0004_idea_stage"
down_revision = "0003_quest_direct_web"
branch_labels = None
depends_on = None


def _counter(name: str) -> sa.Column:
    return sa.Column(name, sa.Integer(), nullable=False, server_default="0")


def _hash_check(name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"length({name}) = 64")


def upgrade() -> None:
    op.create_index(
        "ix_durable_feed_event_type_revision",
        "durable_feed",
        ["event_type", "revision"],
    )
    op.add_column("advancement_engine_state", _counter("stage_request_count"))
    op.add_column("advancement_engine_state", _counter("stage_commit_count"))
    op.add_column("agent_runtime_state", _counter("stage_run_count"))
    op.add_column("agent_runtime_state", _counter("completed_run_count"))
    op.add_column("agent_runtime_state", _counter("attempt_count"))
    op.add_column("agent_runtime_state", _counter("session_count"))
    op.add_column("research_memory_state", _counter("idea_content_count"))
    op.add_column("research_graph_state", _counter("idea_outcome_count"))
    op.add_column("research_graph_state", _counter("idea_rejection_count"))

    op.create_table(
        "ae_stage_run_requests",
        sa.Column("request_ref", sa.String(length=64), primary_key=True),
        sa.Column("cycle_ref", sa.String(length=64), nullable=False, unique=True),
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
        sa.CheckConstraint("stage = 'idea'"),
        sa.CheckConstraint("epoch >= 1"),
        _hash_check("content_hash"),
        _hash_check("content_receipt_hash"),
        _hash_check("question_receipt_hash"),
        _hash_check("context_pack_hash"),
        _hash_check("request_hash"),
        _hash_check("receipt_hash"),
    )
    op.create_table(
        "ae_stage_commands",
        sa.Column("idempotency_key", sa.String(length=128), primary_key=True),
        sa.Column("command_kind", sa.String(length=40), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("result_ref", sa.String(length=64), nullable=False),
        sa.Column("recorded_at", sa.Float(), nullable=False),
        _hash_check("request_hash"),
    )

    op.create_table(
        "ar_stage_runs",
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
        sa.Column("current_attempt_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("root_session_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("current_fence_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("completion_receipt_ref", sa.String(length=64), nullable=True, unique=True),
        sa.Column("completion_receipt_hash", sa.String(length=64), nullable=True),
        sa.Column("outcome_ref", sa.String(length=64), nullable=True, unique=True),
        sa.Column("admission_key", sa.String(length=128), nullable=False, unique=True),
        sa.Column("admission_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["request_ref"], ["ae_stage_run_requests.request_ref"]
        ),
        sa.CheckConstraint("stage = 'idea'"),
        sa.CheckConstraint("epoch >= 1"),
        sa.CheckConstraint("status IN ('running', 'awaiting_acceptance', 'completed')"),
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
    op.create_index("ix_ar_stage_runs_status", "ar_stage_runs", ["status", "updated_at"])

    op.create_table(
        "ar_stage_sessions",
        sa.Column("session_ref", sa.String(length=64), primary_key=True),
        sa.Column("run_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("native_session_ref", sa.String(length=256), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["run_ref"], ["ar_stage_runs.run_ref"]),
        sa.CheckConstraint("status IN ('active', 'completed')"),
    )
    op.create_index(
        "uq_ar_stage_sessions_native_session_ref",
        "ar_stage_sessions",
        ["native_session_ref"],
        unique=True,
        sqlite_where=sa.text("native_session_ref IS NOT NULL"),
    )
    op.create_table(
        "ar_stage_attempts",
        sa.Column("attempt_ref", sa.String(length=64), primary_key=True),
        sa.Column("run_ref", sa.String(length=64), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("root_session_ref", sa.String(length=64), nullable=False),
        sa.Column("fence_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("predecessor_attempt_ref", sa.String(length=64), nullable=True, unique=True),
        sa.Column("predecessor_outcome_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "predecessor_material_outcome_hash",
            sa.String(length=64),
            nullable=True,
        ),
        sa.Column(
            "predecessor_rejection_receipt_ref",
            sa.String(length=64),
            nullable=True,
        ),
        sa.Column(
            "predecessor_rejection_receipt_subject_ref",
            sa.String(length=64),
            nullable=True,
        ),
        sa.Column(
            "predecessor_rejection_receipt_hash",
            sa.String(length=64),
            nullable=True,
        ),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("primary_draft_json", sa.Text(), nullable=True),
        sa.Column("primary_draft_hash", sa.String(length=64), nullable=True),
        sa.Column("primary_adapter_kind", sa.String(length=64), nullable=True),
        sa.Column("primary_recorded_at", sa.Float(), nullable=True),
        sa.Column("submission_ref", sa.String(length=64), nullable=True, unique=True),
        sa.Column("payload_json", sa.Text(), nullable=True),
        sa.Column("payload_hash", sa.String(length=64), nullable=True),
        sa.Column("material_outcome_hash", sa.String(length=64), nullable=True),
        sa.Column("execution_receipt_ref", sa.String(length=64), nullable=True, unique=True),
        sa.Column("execution_receipt_hash", sa.String(length=64), nullable=True),
        sa.Column("decision_receipt_ref", sa.String(length=64), nullable=True),
        sa.Column("decision_receipt_subject_ref", sa.String(length=64), nullable=True),
        sa.Column("decision_receipt_hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("executed_at", sa.Float(), nullable=True),
        sa.Column("closed_at", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["run_ref"], ["ar_stage_runs.run_ref"]),
        sa.ForeignKeyConstraint(["root_session_ref"], ["ar_stage_sessions.session_ref"]),
        sa.ForeignKeyConstraint(["predecessor_attempt_ref"], ["ar_stage_attempts.attempt_ref"]),
        sa.UniqueConstraint("run_ref", "generation"),
        sa.CheckConstraint("generation >= 1"),
        sa.CheckConstraint("status IN ('running', 'executed', 'rejected', 'completed')"),
        _hash_check("material_outcome_hash"),
        _hash_check("predecessor_material_outcome_hash"),
        sa.CheckConstraint(
            "(primary_draft_json IS NULL AND primary_draft_hash IS NULL "
            "AND primary_adapter_kind IS NULL AND primary_recorded_at IS NULL) OR "
            "(primary_draft_json IS NOT NULL AND primary_draft_hash IS NOT NULL "
            "AND length(primary_draft_hash) = 64 "
            "AND primary_adapter_kind IS NOT NULL AND primary_recorded_at IS NOT NULL)"
        ),
        sa.CheckConstraint(
            "(status = 'running' AND submission_ref IS NULL AND payload_json IS NULL "
            "AND payload_hash IS NULL AND material_outcome_hash IS NULL "
            "AND execution_receipt_ref IS NULL "
            "AND execution_receipt_hash IS NULL AND executed_at IS NULL "
            "AND closed_at IS NULL) OR "
            "(status IN ('executed', 'rejected', 'completed') "
            "AND submission_ref IS NOT NULL AND payload_json IS NOT NULL "
            "AND payload_hash IS NOT NULL AND length(payload_hash) = 64 "
            "AND material_outcome_hash IS NOT NULL "
            "AND length(material_outcome_hash) = 64 "
            "AND execution_receipt_ref IS NOT NULL "
            "AND execution_receipt_hash IS NOT NULL "
            "AND length(execution_receipt_hash) = 64 AND executed_at IS NOT NULL)"
        ),
        sa.CheckConstraint(
            "(status IN ('running', 'executed') AND decision_receipt_ref IS NULL "
            "AND decision_receipt_subject_ref IS NULL "
            "AND decision_receipt_hash IS NULL AND closed_at IS NULL) OR "
            "(status IN ('rejected', 'completed') AND decision_receipt_ref IS NOT NULL "
            "AND decision_receipt_subject_ref IS NOT NULL "
            "AND decision_receipt_hash IS NOT NULL "
            "AND length(decision_receipt_hash) = 64 AND closed_at IS NOT NULL)"
        ),
        sa.CheckConstraint(
            "(predecessor_attempt_ref IS NULL "
            "AND predecessor_outcome_hash IS NULL "
            "AND predecessor_material_outcome_hash IS NULL "
            "AND predecessor_rejection_receipt_ref IS NULL "
            "AND predecessor_rejection_receipt_subject_ref IS NULL "
            "AND predecessor_rejection_receipt_hash IS NULL) OR "
            "(predecessor_attempt_ref IS NOT NULL AND status = 'running' "
            "AND predecessor_outcome_hash IS NULL "
            "AND predecessor_material_outcome_hash IS NULL "
            "AND predecessor_rejection_receipt_ref IS NULL "
            "AND predecessor_rejection_receipt_subject_ref IS NULL "
            "AND predecessor_rejection_receipt_hash IS NULL) OR "
            "(predecessor_attempt_ref IS NOT NULL "
            "AND status IN ('executed', 'rejected', 'completed') "
            "AND predecessor_outcome_hash IS NOT NULL "
            "AND length(predecessor_outcome_hash) = 64 "
            "AND predecessor_material_outcome_hash IS NOT NULL "
            "AND length(predecessor_material_outcome_hash) = 64 "
            "AND predecessor_rejection_receipt_ref IS NOT NULL "
            "AND predecessor_rejection_receipt_subject_ref IS NOT NULL "
            "AND predecessor_rejection_receipt_hash IS NOT NULL "
            "AND length(predecessor_rejection_receipt_hash) = 64)"
        ),
    )
    op.create_table(
        "ar_execution_fences",
        sa.Column("fence_ref", sa.String(length=64), primary_key=True),
        sa.Column("run_ref", sa.String(length=64), nullable=False),
        sa.Column("attempt_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("issued_at", sa.Float(), nullable=False),
        sa.Column("closed_at", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["run_ref"], ["ar_stage_runs.run_ref"]),
        sa.ForeignKeyConstraint(["attempt_ref"], ["ar_stage_attempts.attempt_ref"]),
        sa.UniqueConstraint("run_ref", "generation"),
        sa.CheckConstraint("generation >= 1"),
        sa.CheckConstraint("status IN ('current', 'submitted', 'rejected', 'completed')"),
        sa.CheckConstraint(
            "(status IN ('current', 'submitted') AND closed_at IS NULL) OR "
            "(status IN ('rejected', 'completed') AND closed_at IS NOT NULL)"
        ),
    )
    op.create_table(
        "ar_idea_provider_invocations",
        sa.Column("invocation_ref", sa.String(length=64), primary_key=True),
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
        sa.ForeignKeyConstraint(["attempt_ref"], ["ar_stage_attempts.attempt_ref"]),
        sa.ForeignKeyConstraint(["fence_ref"], ["ar_execution_fences.fence_ref"]),
        sa.UniqueConstraint("attempt_ref", "phase"),
        sa.CheckConstraint("phase IN ('primary', 'review')"),
        sa.CheckConstraint("status IN ('prepared', 'completed')"),
        _hash_check("request_hash"),
        _hash_check("runtime_binding_hash"),
        sa.CheckConstraint(
            "(status = 'prepared' AND response_hash IS NULL "
            "AND completed_at IS NULL) OR "
            "(status = 'completed' AND response_hash IS NOT NULL "
            "AND length(response_hash) = 64 AND completed_at IS NOT NULL)"
        ),
    )
    op.create_index(
        "ix_ar_idea_provider_invocations_status",
        "ar_idea_provider_invocations",
        ["status", "prepared_at"],
    )
    op.create_table(
        "ar_stage_commands",
        sa.Column("idempotency_key", sa.String(length=128), primary_key=True),
        sa.Column("command_kind", sa.String(length=40), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("result_ref", sa.String(length=64), nullable=False),
        sa.Column("recorded_at", sa.Float(), nullable=False),
        _hash_check("request_hash"),
    )

    op.create_table(
        "rm_idea_outcome_contents",
        sa.Column("content_ref", sa.String(length=64), primary_key=True),
        sa.Column("request_ref", sa.String(length=64), nullable=False),
        sa.Column("run_ref", sa.String(length=64), nullable=False),
        sa.Column("attempt_ref", sa.String(length=64), nullable=False),
        sa.Column("fence_ref", sa.String(length=64), nullable=False),
        sa.Column("submission_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("outcome_kind", sa.String(length=32), nullable=False),
        sa.Column("outcome_json", sa.Text(), nullable=False),
        sa.Column("outcome_hash", sa.String(length=64), nullable=False),
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
        sa.ForeignKeyConstraint(["request_ref"], ["ae_stage_run_requests.request_ref"]),
        sa.ForeignKeyConstraint(["run_ref"], ["ar_stage_runs.run_ref"]),
        sa.ForeignKeyConstraint(["attempt_ref"], ["ar_stage_attempts.attempt_ref"]),
        sa.ForeignKeyConstraint(["fence_ref"], ["ar_execution_fences.fence_ref"]),
        sa.ForeignKeyConstraint(
            ["submission_ref"], ["ar_stage_attempts.submission_ref"]
        ),
        sa.CheckConstraint("outcome_kind IN ('idea_set', 'no_viable_candidate')"),
        _hash_check("outcome_hash"),
        _hash_check("reviewed_draft_hash"),
        _hash_check("review_hash"),
        _hash_check("payload_hash"),
        _hash_check("execution_receipt_hash"),
        _hash_check("receipt_hash"),
    )
    op.create_index(
        "ix_rm_idea_outcome_contents_request",
        "rm_idea_outcome_contents",
        ["request_ref", "accepted_at"],
    )

    op.create_table(
        "rg_idea_outcome_decisions",
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
        sa.Column("question_receipt_ref", sa.String(length=64), nullable=False),
        sa.Column("question_receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("idea_content_ref", sa.String(length=64), nullable=False),
        sa.Column("idea_content_receipt_ref", sa.String(length=64), nullable=False),
        sa.Column("idea_content_receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("execution_receipt_ref", sa.String(length=64), nullable=False),
        sa.Column("execution_receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("outcome_kind", sa.String(length=32), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("outcome_hash", sa.String(length=64), nullable=False),
        sa.Column("reviewed_draft_hash", sa.String(length=64), nullable=False),
        sa.Column("review_hash", sa.String(length=64), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("outcome_ref", sa.String(length=64), nullable=True, unique=True),
        sa.Column("reason_code", sa.String(length=96), nullable=True),
        sa.Column("feedback_json", sa.Text(), nullable=False),
        sa.Column("feedback_hash", sa.String(length=64), nullable=False),
        sa.Column("receipt_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("decided_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["request_ref"], ["ae_stage_run_requests.request_ref"]),
        sa.ForeignKeyConstraint(["run_ref"], ["ar_stage_runs.run_ref"]),
        sa.ForeignKeyConstraint(["attempt_ref"], ["ar_stage_attempts.attempt_ref"]),
        sa.ForeignKeyConstraint(["fence_ref"], ["ar_execution_fences.fence_ref"]),
        sa.ForeignKeyConstraint(["quest_ref"], ["rg_quests.quest_ref"]),
        sa.ForeignKeyConstraint(["question_ref"], ["rg_questions.question_ref"]),
        sa.ForeignKeyConstraint(
            ["question_content_ref"], ["rm_formal_question_contents.content_ref"]
        ),
        sa.ForeignKeyConstraint(
            ["idea_content_ref"], ["rm_idea_outcome_contents.content_ref"]
        ),
        sa.ForeignKeyConstraint(
            ["submission_ref"], ["rm_idea_outcome_contents.submission_ref"]
        ),
        sa.CheckConstraint("outcome_kind IN ('idea_set', 'no_viable_candidate')"),
        sa.CheckConstraint("decision IN ('accepted', 'rejected')"),
        _hash_check("question_content_hash"),
        _hash_check("question_receipt_hash"),
        _hash_check("idea_content_receipt_hash"),
        _hash_check("execution_receipt_hash"),
        _hash_check("payload_hash"),
        _hash_check("outcome_hash"),
        _hash_check("reviewed_draft_hash"),
        _hash_check("review_hash"),
        _hash_check("receipt_hash"),
        _hash_check("feedback_hash"),
        sa.CheckConstraint(
            "(decision = 'accepted' AND outcome_ref IS NOT NULL "
            "AND reason_code IS NULL) OR "
            "(decision = 'rejected' AND outcome_ref IS NULL "
            "AND reason_code IS NOT NULL)"
        ),
    )
    op.create_index(
        "uq_rg_idea_outcome_one_accepted_per_request",
        "rg_idea_outcome_decisions",
        ["request_ref"],
        unique=True,
        sqlite_where=sa.text("decision = 'accepted'"),
    )
    op.create_index(
        "ix_rg_idea_outcome_request_decided",
        "rg_idea_outcome_decisions",
        ["request_ref", "decided_at"],
    )

    op.create_table(
        "ae_stage_commits",
        sa.Column("commit_ref", sa.String(length=64), primary_key=True),
        sa.Column("request_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("cycle_ref", sa.String(length=64), nullable=False),
        sa.Column("stage", sa.String(length=24), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("run_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("outcome_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("outcome_kind", sa.String(length=32), nullable=False),
        sa.Column("disposition", sa.String(length=16), nullable=False),
        sa.Column("run_completion_receipt_ref", sa.String(length=64), nullable=False),
        sa.Column("run_completion_receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("outcome_receipt_ref", sa.String(length=64), nullable=False),
        sa.Column("outcome_receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("receipt_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("committed_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["request_ref"], ["ae_stage_run_requests.request_ref"]),
        sa.ForeignKeyConstraint(["run_ref"], ["ar_stage_runs.run_ref"]),
        sa.ForeignKeyConstraint(["outcome_ref"], ["rg_idea_outcome_decisions.outcome_ref"]),
        sa.CheckConstraint("stage = 'idea'"),
        sa.CheckConstraint(
            "outcome_kind IN ('idea_set', 'no_viable_candidate')"
        ),
        sa.CheckConstraint("disposition = 'completed'"),
        sa.CheckConstraint("epoch >= 1"),
        _hash_check("run_completion_receipt_hash"),
        _hash_check("outcome_receipt_hash"),
        _hash_check("request_hash"),
        _hash_check("receipt_hash"),
    )


def downgrade() -> None:
    raise RuntimeError("vNext production migrations are forward-only")
