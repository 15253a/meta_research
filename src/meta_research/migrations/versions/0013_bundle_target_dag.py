"""Add Bundle Stage, Target DAG, and TargetCommit authorities.

Revision ID: 0013_bundle_target_dag
Revises: 0012_experiment_measurement
Create Date: 2026-08-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision = "0013_bundle_target_dag"
down_revision = "0012_experiment_measurement"
branch_labels = None
depends_on = None


def _hash(name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"length({name}) = 64")


def _copy(source: str, target: str, columns: Sequence[str]) -> None:
    values = ", ".join(columns)
    op.get_bind().execute(
        sa.text(f"INSERT INTO {target} ({values}) SELECT {values} FROM {source}")
    )


def _replace_stage_requests() -> None:
    source = "ae_stage_run_requests"
    backup = "ae_stage_run_requests_pre_bundle"
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
        sa.Column("request_ref", sa.String(64), primary_key=True),
        sa.Column("cycle_ref", sa.String(64), nullable=False),
        sa.Column("stage", sa.String(24), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("initialization_id", sa.String(64), nullable=False),
        sa.Column("quest_ref", sa.String(64), nullable=False),
        sa.Column("question_ref", sa.String(64), nullable=False),
        sa.Column("content_ref", sa.String(64), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("schema_ref", sa.String(96), nullable=False),
        sa.Column("content_receipt_ref", sa.String(64), nullable=False),
        sa.Column("content_receipt_hash", sa.String(64), nullable=False),
        sa.Column("question_receipt_ref", sa.String(64), nullable=False),
        sa.Column("question_receipt_hash", sa.String(64), nullable=False),
        sa.Column("context_pack_ref", sa.String(64), nullable=False, unique=True),
        sa.Column("context_pack_json", sa.Text(), nullable=False),
        sa.Column("context_pack_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(64), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["cycle_ref"], ["ae_initial_cycles.cycle_ref"]),
        sa.ForeignKeyConstraint(["question_ref"], ["rg_questions.question_ref"]),
        sa.ForeignKeyConstraint(
            ["content_ref"], ["rm_formal_question_contents.content_ref"]
        ),
        sa.UniqueConstraint("cycle_ref", "stage"),
        sa.CheckConstraint("stage IN ('idea', 'plan', 'bundle')"),
        sa.CheckConstraint("epoch >= 1"),
        *(
            _hash(name)
            for name in (
                "content_hash",
                "content_receipt_hash",
                "question_receipt_hash",
                "context_pack_hash",
                "request_hash",
                "receipt_hash",
            )
        ),
    )
    _copy(backup, source, columns)
    op.drop_table(backup)


def _replace_stage_runs() -> None:
    source = "ar_stage_runs"
    backup = "ar_stage_runs_pre_bundle"
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
        sa.Column("run_ref", sa.String(64), primary_key=True),
        sa.Column("request_ref", sa.String(64), nullable=False, unique=True),
        sa.Column("cycle_ref", sa.String(64), nullable=False),
        sa.Column("stage", sa.String(24), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("context_pack_ref", sa.String(64), nullable=False),
        sa.Column("context_pack_hash", sa.String(64), nullable=False),
        sa.Column("runtime_binding_json", sa.Text(), nullable=False),
        sa.Column("runtime_binding_hash", sa.String(64), nullable=False),
        sa.Column("request_receipt_ref", sa.String(64), nullable=False),
        sa.Column("request_receipt_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("current_attempt_ref", sa.String(64), nullable=False, unique=True),
        sa.Column("root_session_ref", sa.String(64), nullable=False, unique=True),
        sa.Column("current_fence_ref", sa.String(64), nullable=False, unique=True),
        sa.Column("completion_receipt_ref", sa.String(64), nullable=True, unique=True),
        sa.Column("completion_receipt_hash", sa.String(64), nullable=True),
        sa.Column("outcome_ref", sa.String(96), nullable=True, unique=True),
        sa.Column("admission_key", sa.String(128), nullable=False, unique=True),
        sa.Column("admission_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["request_ref"], ["ae_stage_run_requests.request_ref"]),
        sa.CheckConstraint("stage IN ('idea', 'plan', 'bundle')"),
        sa.CheckConstraint("epoch >= 1"),
        sa.CheckConstraint("status IN ('running', 'awaiting_acceptance', 'completed')"),
        *(
            _hash(name)
            for name in (
                "context_pack_hash",
                "runtime_binding_hash",
                "request_receipt_hash",
                "admission_hash",
            )
        ),
        sa.CheckConstraint(
            "(status != 'completed' AND completion_receipt_ref IS NULL "
            "AND completion_receipt_hash IS NULL AND outcome_ref IS NULL) OR "
            "(status = 'completed' AND completion_receipt_ref IS NOT NULL "
            "AND completion_receipt_hash IS NOT NULL "
            "AND length(completion_receipt_hash) = 64 AND outcome_ref IS NOT NULL)"
        ),
    )
    _copy(backup, source, columns)
    op.drop_table(backup)
    op.create_index("ix_ar_stage_runs_status", source, ["status", "updated_at"])


def _replace_stage_commits() -> None:
    source = "ae_stage_commits"
    backup = "ae_stage_commits_pre_bundle"
    old_columns = (
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
        sa.Column("commit_ref", sa.String(64), primary_key=True),
        sa.Column("request_ref", sa.String(64), nullable=False, unique=True),
        sa.Column("cycle_ref", sa.String(64), nullable=False),
        sa.Column("stage", sa.String(24), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("run_ref", sa.String(64), nullable=True, unique=True),
        sa.Column("outcome_ref", sa.String(96), nullable=False),
        sa.Column("outcome_kind", sa.String(32), nullable=False),
        sa.Column("disposition", sa.String(16), nullable=False),
        sa.Column("run_completion_receipt_ref", sa.String(96), nullable=True),
        sa.Column("run_completion_receipt_hash", sa.String(64), nullable=True),
        sa.Column("outcome_receipt_ref", sa.String(96), nullable=False),
        sa.Column("outcome_receipt_hash", sa.String(64), nullable=False),
        sa.Column("closure_json", sa.Text(), nullable=True),
        sa.Column("closure_hash", sa.String(64), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("committed_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["request_ref"], ["ae_stage_run_requests.request_ref"]),
        sa.ForeignKeyConstraint(["run_ref"], ["ar_stage_runs.run_ref"]),
        # One accepted FormalPlan is both the Plan outcome and the exact
        # no-op Bundle outcome when its GapSet is empty.  Identity is therefore
        # unique inside a Stage, not globally across the Stage pipeline.
        sa.UniqueConstraint("stage", "outcome_ref"),
        sa.CheckConstraint("stage IN ('idea', 'plan', 'bundle')"),
        sa.CheckConstraint(
            "outcome_kind IN ('idea_set', 'no_viable_candidate', "
            "'formal_plan', 'target_graph', 'bundle_skip')"
        ),
        sa.CheckConstraint("disposition IN ('completed', 'skipped')"),
        sa.CheckConstraint("epoch >= 1"),
        _hash("outcome_receipt_hash"),
        _hash("request_hash"),
        _hash("receipt_hash"),
        sa.CheckConstraint(
            "(stage != 'bundle' AND disposition = 'completed' "
            "AND run_ref IS NOT NULL AND run_completion_receipt_ref IS NOT NULL "
            "AND run_completion_receipt_hash IS NOT NULL "
            "AND length(run_completion_receipt_hash) = 64 "
            "AND closure_json IS NULL AND closure_hash IS NULL) OR "
            "(stage = 'bundle' AND disposition = 'completed' "
            "AND outcome_kind = 'target_graph' AND run_ref IS NOT NULL "
            "AND run_completion_receipt_ref IS NOT NULL "
            "AND run_completion_receipt_hash IS NOT NULL "
            "AND length(run_completion_receipt_hash) = 64 "
            "AND closure_json IS NOT NULL AND closure_hash IS NOT NULL "
            "AND length(closure_hash) = 64) OR "
            "(stage = 'bundle' AND disposition = 'skipped' "
            "AND outcome_kind = 'bundle_skip' AND run_ref IS NULL "
            "AND run_completion_receipt_ref IS NULL "
            "AND run_completion_receipt_hash IS NULL "
            "AND closure_json IS NOT NULL AND closure_hash IS NOT NULL "
            "AND length(closure_hash) = 64)"
        ),
    )
    _copy(backup, source, old_columns)
    op.drop_table(backup)


def _rebuild_stage_authorities() -> None:
    connection = op.get_bind()
    connection.exec_driver_sql("PRAGMA legacy_alter_table=ON")
    try:
        _replace_stage_requests()
        _replace_stage_runs()
        _replace_stage_commits()
    finally:
        connection.exec_driver_sql("PRAGMA legacy_alter_table=OFF")


def _create_target_authorities() -> None:
    for name in ("target_graph_count", "target_count", "target_commit_count"):
        op.add_column(
            "research_graph_state",
            sa.Column(name, sa.Integer(), nullable=False, server_default="0"),
        )
    op.create_table(
        "rg_target_graphs",
        sa.Column("graph_ref", sa.String(96), primary_key=True),
        sa.Column("request_ref", sa.String(64), nullable=False, unique=True),
        sa.Column("run_ref", sa.String(64), nullable=False, unique=True),
        sa.Column("attempt_ref", sa.String(64), nullable=False),
        sa.Column("fence_ref", sa.String(64), nullable=False),
        sa.Column("submission_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("cycle_ref", sa.String(64), nullable=False),
        sa.Column("quest_ref", sa.String(96), nullable=False),
        sa.Column("formal_plan_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("plan_content_ref", sa.String(64), nullable=False),
        sa.Column("plan_document_hash", sa.String(64), nullable=False),
        sa.Column("context_pack_ref", sa.String(64), nullable=False),
        sa.Column("context_pack_hash", sa.String(64), nullable=False),
        sa.Column("target_plan_json", sa.Text(), nullable=False),
        sa.Column("target_plan_hash", sa.String(64), nullable=False),
        sa.Column("execution_receipt_ref", sa.String(96), nullable=False),
        sa.Column("execution_receipt_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["request_ref"], ["ae_stage_run_requests.request_ref"]),
        sa.ForeignKeyConstraint(["run_ref"], ["ar_stage_runs.run_ref"]),
        sa.ForeignKeyConstraint(["attempt_ref"], ["ar_stage_attempts.attempt_ref"]),
        sa.ForeignKeyConstraint(["fence_ref"], ["ar_execution_fences.fence_ref"]),
        sa.ForeignKeyConstraint(["quest_ref"], ["rg_quests.quest_ref"]),
        sa.ForeignKeyConstraint(
            ["plan_content_ref"], ["rm_plan_documents.content_ref"]
        ),
        *(
            _hash(name)
            for name in (
                "plan_document_hash",
                "context_pack_hash",
                "target_plan_hash",
                "execution_receipt_hash",
                "receipt_hash",
            )
        ),
    )
    op.create_table(
        "rg_targets",
        sa.Column("target_ref", sa.String(96), primary_key=True),
        sa.Column("graph_ref", sa.String(96), nullable=False),
        sa.Column("target_key", sa.String(128), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("spec_json", sa.Text(), nullable=False),
        sa.Column("spec_hash", sa.String(64), nullable=False),
        sa.Column("dependency_refs_json", sa.Text(), nullable=False),
        sa.Column("dependency_refs_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["graph_ref"], ["rg_target_graphs.graph_ref"]),
        sa.UniqueConstraint("graph_ref", "target_key"),
        sa.UniqueConstraint("graph_ref", "ordinal"),
        sa.CheckConstraint("ordinal >= 0"),
        _hash("spec_hash"),
        _hash("dependency_refs_hash"),
        _hash("receipt_hash"),
    )
    op.create_table(
        "rg_target_run_bindings",
        sa.Column("binding_ref", sa.String(96), primary_key=True),
        sa.Column("target_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("target_run_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("evaluation_attempt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("execution_request_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("definition_hash", sa.String(64), nullable=False),
        sa.Column("admission_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("admission_receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("admission_receipt_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("bound_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["target_ref"], ["rg_targets.target_ref"]),
        sa.ForeignKeyConstraint(
            ["evaluation_attempt_ref"],
            ["rg_evaluation_attempts.evaluation_attempt_ref"],
        ),
        sa.ForeignKeyConstraint(
            ["execution_request_ref"], ["rg_experiment_requests.execution_request_ref"]
        ),
        sa.ForeignKeyConstraint(["target_run_ref"], ["ar_experiment_runs.run_ref"]),
        _hash("definition_hash"),
        _hash("admission_receipt_hash"),
        _hash("receipt_hash"),
    )
    op.create_table(
        "rg_target_commits",
        sa.Column("commit_ref", sa.String(96), primary_key=True),
        sa.Column("target_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("target_run_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("evaluation_attempt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("target_spec_hash", sa.String(64), nullable=False),
        sa.Column("closure_json", sa.Text(), nullable=False),
        sa.Column("closure_hash", sa.String(64), nullable=False),
        sa.Column("result_disposition", sa.String(24), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("committed_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["target_ref"], ["rg_targets.target_ref"]),
        sa.ForeignKeyConstraint(["target_run_ref"], ["ar_experiment_runs.run_ref"]),
        sa.ForeignKeyConstraint(
            ["evaluation_attempt_ref"],
            ["rg_evaluation_attempts.evaluation_attempt_ref"],
        ),
        sa.CheckConstraint(
            "result_disposition IN ('positive', 'negative', 'zero', "
            "'nonsignificant', 'denied', 'uncertain')"
        ),
        _hash("target_spec_hash"),
        _hash("closure_hash"),
        _hash("receipt_hash"),
    )
    op.create_table(
        "ar_target_run_admissions",
        sa.Column("admission_ref", sa.String(96), primary_key=True),
        sa.Column("target_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("target_spec_hash", sa.String(64), nullable=False),
        sa.Column("graph_ref", sa.String(96), nullable=False),
        sa.Column("stage_request_ref", sa.String(64), nullable=False),
        sa.Column("quest_ref", sa.String(96), nullable=False),
        sa.Column("target_run_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("evaluation_attempt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("execution_request_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("definition_hash", sa.String(64), nullable=False),
        sa.Column("human_request_ref", sa.String(96), nullable=True),
        sa.Column("human_waiter_ref", sa.String(128), nullable=True),
        sa.Column("human_waiter_generation", sa.Integer(), nullable=True),
        sa.Column("human_authorization_receipt_ref", sa.String(96), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("admitted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["target_ref"], ["rg_targets.target_ref"]),
        sa.ForeignKeyConstraint(["graph_ref"], ["rg_target_graphs.graph_ref"]),
        sa.ForeignKeyConstraint(
            ["stage_request_ref"], ["ae_stage_run_requests.request_ref"]
        ),
        sa.ForeignKeyConstraint(["target_run_ref"], ["ar_experiment_runs.run_ref"]),
        sa.ForeignKeyConstraint(
            ["evaluation_attempt_ref"],
            ["rg_evaluation_attempts.evaluation_attempt_ref"],
        ),
        sa.ForeignKeyConstraint(
            ["execution_request_ref"], ["rg_experiment_requests.execution_request_ref"]
        ),
        sa.CheckConstraint(
            "(human_request_ref IS NULL AND human_waiter_ref IS NULL "
            "AND human_waiter_generation IS NULL "
            "AND human_authorization_receipt_ref IS NULL) OR "
            "(human_request_ref IS NOT NULL AND human_waiter_ref IS NOT NULL "
            "AND human_waiter_generation >= 1 "
            "AND human_authorization_receipt_ref IS NOT NULL)"
        ),
        _hash("target_spec_hash"),
        _hash("definition_hash"),
        _hash("request_hash"),
        _hash("receipt_hash"),
    )
    op.create_table(
        "ar_bundle_dispatch_decisions",
        sa.Column("decision_ref", sa.String(96), primary_key=True),
        sa.Column("run_ref", sa.String(64), nullable=False),
        sa.Column("attempt_ref", sa.String(64), nullable=False),
        sa.Column("fence_ref", sa.String(64), nullable=False),
        sa.Column("native_session_ref", sa.String(128), nullable=False),
        sa.Column("graph_ref", sa.String(96), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("frontier_json", sa.Text(), nullable=False),
        sa.Column("frontier_hash", sa.String(64), nullable=False),
        sa.Column("state_json", sa.Text(), nullable=False),
        sa.Column("state_hash", sa.String(64), nullable=False),
        sa.Column("action", sa.String(24), nullable=False),
        sa.Column("selected_target_ref", sa.String(96), nullable=True),
        sa.Column("rationale", sa.String(512), nullable=False),
        sa.Column("decision_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["run_ref"], ["ar_stage_runs.run_ref"]),
        sa.ForeignKeyConstraint(["attempt_ref"], ["ar_stage_attempts.attempt_ref"]),
        sa.ForeignKeyConstraint(["fence_ref"], ["ar_execution_fences.fence_ref"]),
        sa.ForeignKeyConstraint(["graph_ref"], ["rg_target_graphs.graph_ref"]),
        sa.ForeignKeyConstraint(["selected_target_ref"], ["rg_targets.target_ref"]),
        sa.UniqueConstraint("run_ref", "generation"),
        sa.CheckConstraint("generation >= 1"),
        sa.CheckConstraint(
            "(action = 'dispatch' AND selected_target_ref IS NOT NULL) OR "
            "(action IN ('wait', 'replan_required') "
            "AND selected_target_ref IS NULL)"
        ),
        _hash("frontier_hash"),
        _hash("state_hash"),
        _hash("decision_hash"),
        _hash("request_hash"),
        _hash("receipt_hash"),
    )


def upgrade() -> None:
    _rebuild_stage_authorities()
    _create_target_authorities()


def downgrade() -> None:
    raise RuntimeError("vNext production migrations are forward-only")
