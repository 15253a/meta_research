"""Add immutable RM/RG acceptance facts for the Reasoning Stage.

Revision ID: 0030_reasoning_owner_acceptance
Revises: 0029_target_root_lifecycle
Create Date: 2026-08-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision = "0030_reasoning_owner_acceptance"
down_revision = "0029_target_root_lifecycle"
branch_labels = None
depends_on = None


def _counter(name: str) -> sa.Column:
    return sa.Column(name, sa.Integer(), nullable=False, server_default="0")


def _hash(name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"length({name}) = 64")


def _copy_rows(
    source: str,
    target: str,
    columns: Sequence[str],
    *,
    where: str | None = None,
) -> None:
    column_list = ", ".join(columns)
    predicate = "" if where is None else f" WHERE {where}"
    op.execute(
        f"INSERT INTO {target} ({column_list}) SELECT {column_list} FROM "
        f"{source}{predicate}"
    )


def _replace_provider_units(*, include_reasoning: bool) -> None:
    """Rebuild SQLite's shared provider-unit kind constraint losslessly."""

    source = "ar_provider_units"
    backup = "ar_provider_units_pre_reasoning"
    columns = (
        "unit_ref",
        "operation_ref",
        "run_ref",
        "attempt_ref",
        "fence_ref",
        "unit_kind",
        "status",
        "started_at",
        "completed_at",
    )
    unit_kinds = (
        "'idea_primary', 'idea_review', 'plan_primary', 'plan_review', "
        "'bundle_primary', 'bundle_review', 'deepfetch', 'experiment', "
        "'writing_primary', 'writing_review'"
    )
    if include_reasoning:
        unit_kinds += ", 'reasoning_primary', 'reasoning_review'"
    connection = op.get_bind()
    connection.exec_driver_sql("PRAGMA legacy_alter_table=ON")
    try:
        op.rename_table(source, backup)
        op.create_table(
            source,
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
            sa.CheckConstraint(f"unit_kind IN ({unit_kinds})"),
            sa.CheckConstraint(
                "status IN ('active', 'revocation_pending', 'completed', 'revoked')"
            ),
            sa.CheckConstraint(
                "(status IN ('active', 'revocation_pending') AND completed_at IS "
                "NULL) OR (status IN ('completed', 'revoked') AND completed_at "
                "IS NOT NULL)"
            ),
        )
        _copy_rows(
            backup,
            source,
            columns,
            where=(
                None
                if include_reasoning
                else "unit_kind NOT IN ('reasoning_primary', 'reasoning_review')"
            ),
        )
        op.drop_table(backup)
        op.create_index(
            "ix_ar_provider_units_active",
            source,
            ["run_ref", "status"],
        )
    finally:
        connection.exec_driver_sql("PRAGMA legacy_alter_table=OFF")


def _replace_stage_commits(*, include_reasoning_closure: bool) -> None:
    """Extend the completed-stage closure invariant for Reasoning commits."""

    source = "ae_stage_commits"
    backup = "ae_stage_commits_pre_reasoning"
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
        "closure_json",
        "closure_hash",
        "basis_kind",
        "basis_ref",
        "basis_receipt_issuer",
        "basis_receipt_kind",
        "basis_receipt_subject_ref",
        "basis_receipt_ref",
        "basis_receipt_hash",
        "idempotency_key",
        "request_hash",
        "receipt_ref",
        "receipt_hash",
        "committed_at",
    )
    completed_without_closure = (
        "(stage NOT IN ('bundle', 'reasoning')"
        if include_reasoning_closure
        else "(stage != 'bundle'"
    )
    completed_without_closure += (
        " AND disposition = 'completed' AND request_ref IS NOT NULL AND "
        "run_ref IS NOT NULL AND outcome_ref IS NOT NULL AND outcome_kind IS "
        "NOT NULL AND run_completion_receipt_ref IS NOT NULL AND "
        "length(run_completion_receipt_hash) = 64 AND outcome_receipt_ref IS "
        "NOT NULL AND length(outcome_receipt_hash) = 64 AND closure_json IS "
        "NULL AND closure_hash IS NULL AND basis_kind IS NULL AND basis_ref IS "
        "NULL AND basis_receipt_issuer IS NULL AND basis_receipt_kind IS NULL "
        "AND basis_receipt_subject_ref IS NULL AND basis_receipt_ref IS NULL "
        "AND basis_receipt_hash IS NULL)"
    )
    reasoning_completed = (
        " OR (stage = 'reasoning' AND disposition = 'completed' AND request_ref "
        "IS NOT NULL AND run_ref IS NOT NULL AND outcome_ref IS NOT NULL AND "
        "outcome_kind = 'reasoning_outcome' AND run_completion_receipt_ref IS "
        "NOT NULL AND length(run_completion_receipt_hash) = 64 AND "
        "outcome_receipt_ref IS NOT NULL AND length(outcome_receipt_hash) = 64 "
        "AND closure_json IS NOT NULL AND length(closure_hash) = 64 AND "
        "basis_kind IS NULL AND basis_ref IS NULL AND basis_receipt_issuer IS "
        "NULL AND basis_receipt_kind IS NULL AND basis_receipt_subject_ref IS "
        "NULL AND basis_receipt_ref IS NULL AND basis_receipt_hash IS NULL)"
        if include_reasoning_closure
        else ""
    )
    state_constraint = completed_without_closure + reasoning_completed + (
        " OR (stage = 'bundle' AND disposition = 'completed' AND request_ref IS "
        "NOT NULL AND run_ref IS NOT NULL AND outcome_ref IS NOT NULL AND "
        "outcome_kind IN ('target_graph', 'bundle_report') AND "
        "run_completion_receipt_ref IS NOT NULL AND "
        "length(run_completion_receipt_hash) = 64 AND outcome_receipt_ref IS "
        "NOT NULL AND length(outcome_receipt_hash) = 64 AND closure_json IS NOT "
        "NULL AND length(closure_hash) = 64 AND basis_kind IS NULL AND "
        "basis_ref IS NULL AND basis_receipt_issuer IS NULL AND "
        "basis_receipt_kind IS NULL AND basis_receipt_subject_ref IS NULL AND "
        "basis_receipt_ref IS NULL AND basis_receipt_hash IS NULL) OR "
        "(stage = 'bundle' AND disposition = 'skipped' AND request_ref IS NOT "
        "NULL AND run_ref IS NULL AND outcome_ref IS NOT NULL AND outcome_kind "
        "= 'bundle_skip' AND run_completion_receipt_ref IS NULL AND "
        "run_completion_receipt_hash IS NULL AND outcome_receipt_ref IS NOT "
        "NULL AND length(outcome_receipt_hash) = 64 AND closure_json IS NOT NULL "
        "AND length(closure_hash) = 64 AND basis_kind IS NULL AND basis_ref IS "
        "NULL AND basis_receipt_issuer IS NULL AND basis_receipt_kind IS NULL "
        "AND basis_receipt_subject_ref IS NULL AND basis_receipt_ref IS NULL AND "
        "basis_receipt_hash IS NULL) OR (disposition = 'skipped' AND request_ref "
        "IS NULL AND run_ref IS NULL AND outcome_ref IS NULL AND outcome_kind IS "
        "NULL AND run_completion_receipt_ref IS NULL AND "
        "run_completion_receipt_hash IS NULL AND outcome_receipt_ref IS NULL "
        "AND outcome_receipt_hash IS NULL AND closure_json IS NULL AND "
        "closure_hash IS NULL AND basis_kind IS NOT NULL AND basis_ref IS NOT "
        "NULL AND basis_receipt_issuer IS NOT NULL AND basis_receipt_kind IS NOT "
        "NULL AND basis_receipt_subject_ref IS NOT NULL AND basis_receipt_ref IS "
        "NOT NULL AND length(basis_receipt_hash) = 64) OR (disposition = "
        "'exhausted' AND request_ref IS NOT NULL AND run_ref IS NOT NULL AND "
        "outcome_ref IS NULL AND outcome_kind IS NULL AND "
        "run_completion_receipt_ref IS NOT NULL AND "
        "length(run_completion_receipt_hash) = 64 AND outcome_receipt_ref IS "
        "NULL AND outcome_receipt_hash IS NULL AND closure_json IS NULL AND "
        "closure_hash IS NULL AND basis_kind IS NOT NULL AND basis_ref IS NOT "
        "NULL AND basis_receipt_issuer IS NOT NULL AND basis_receipt_kind IS NOT "
        "NULL AND basis_receipt_subject_ref IS NOT NULL AND basis_receipt_ref IS "
        "NOT NULL AND length(basis_receipt_hash) = 64)"
    )
    connection = op.get_bind()
    connection.exec_driver_sql("PRAGMA legacy_alter_table=ON")
    try:
        op.rename_table(source, backup)
        op.create_table(
            source,
            sa.Column("commit_ref", sa.String(64), primary_key=True),
            sa.Column("request_ref", sa.String(64), nullable=True, unique=True),
            sa.Column("cycle_ref", sa.String(64), nullable=False),
            sa.Column("stage", sa.String(24), nullable=False),
            sa.Column("epoch", sa.Integer(), nullable=False),
            sa.Column("run_ref", sa.String(96), nullable=True, unique=True),
            sa.Column("outcome_ref", sa.String(96), nullable=True),
            sa.Column("outcome_kind", sa.String(64), nullable=True),
            sa.Column("disposition", sa.String(16), nullable=False),
            sa.Column(
                "run_completion_receipt_ref", sa.String(96), nullable=True
            ),
            sa.Column(
                "run_completion_receipt_hash", sa.String(64), nullable=True
            ),
            sa.Column("outcome_receipt_ref", sa.String(96), nullable=True),
            sa.Column("outcome_receipt_hash", sa.String(64), nullable=True),
            sa.Column("closure_json", sa.Text(), nullable=True),
            sa.Column("closure_hash", sa.String(64), nullable=True),
            sa.Column("basis_kind", sa.String(64), nullable=True),
            sa.Column("basis_ref", sa.String(96), nullable=True),
            sa.Column("basis_receipt_issuer", sa.String(64), nullable=True),
            sa.Column("basis_receipt_kind", sa.String(96), nullable=True),
            sa.Column("basis_receipt_subject_ref", sa.String(96), nullable=True),
            sa.Column("basis_receipt_ref", sa.String(96), nullable=True),
            sa.Column("basis_receipt_hash", sa.String(64), nullable=True),
            sa.Column(
                "idempotency_key", sa.String(128), nullable=False, unique=True
            ),
            sa.Column("request_hash", sa.String(64), nullable=False),
            sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
            sa.Column("receipt_hash", sa.String(64), nullable=False),
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
            sa.CheckConstraint(state_constraint),
            _hash("request_hash"),
            _hash("receipt_hash"),
        )
        _copy_rows(backup, source, columns)
        op.drop_table(backup)
    finally:
        connection.exec_driver_sql("PRAGMA legacy_alter_table=OFF")


def _assert_reasoning_owner_downgrade_safe() -> None:
    connection = op.get_bind()
    destructive_counts = (
        connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM ae_stage_commits WHERE stage = 'reasoning'"
            )
        ).scalar_one(),
        connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM ar_stage_attempts WHERE "
                "reasoning_checkpoint_ref IS NOT NULL"
            )
        ).scalar_one(),
        connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM ar_provider_units WHERE unit_kind IN "
                "('reasoning_primary', 'reasoning_review')"
            )
        ).scalar_one(),
        *(
            connection.execute(
                sa.text(f"SELECT COUNT(*) FROM {table_name}")
            ).scalar_one()
            for table_name in (
                "rm_question_literature_revisions",
                "rm_reasoning_scientific_candidates",
                "rm_reasoning_contents",
                "rg_reasoning_scientific_decisions",
                "rg_reasoning_outcome_decisions",
            )
        ),
        connection.execute(
            sa.text(
                "SELECT COALESCE(SUM(question_literature_revision_count + "
                "reasoning_content_count + reasoning_scientific_candidate_count), "
                "0) FROM research_memory_state"
            )
        ).scalar_one(),
        connection.execute(
            sa.text(
                "SELECT COALESCE(SUM(reasoning_outcome_count + "
                "reasoning_rejection_count + reasoning_scientific_outcome_count + "
                "reasoning_scientific_rejection_count), 0) FROM "
                "research_graph_state"
            )
        ).scalar_one(),
    )
    if any(destructive_counts):
        raise RuntimeError(
            "cannot downgrade reasoning owner acceptance while Reasoning "
            "or QuestionLiterature vNext facts exist"
        )


def _add_reasoning_checkpoint_columns() -> None:
    all_null = " AND ".join(
        f"{name} IS NULL"
        for name in (
            "reasoning_checkpoint_ref",
            "reasoning_checkpoint_json",
            "reasoning_checkpoint_hash",
            "reasoning_checkpoint_review_json",
            "reasoning_checkpoint_review_hash",
            "reasoning_checkpoint_receipt_ref",
            "reasoning_checkpoint_receipt_hash",
            "reasoning_checkpoint_recorded_at",
        )
    )
    all_present = " AND ".join(
        f"{name} IS NOT NULL"
        for name in (
            "reasoning_checkpoint_ref",
            "reasoning_checkpoint_json",
            "reasoning_checkpoint_hash",
            "reasoning_checkpoint_review_json",
            "reasoning_checkpoint_review_hash",
            "reasoning_checkpoint_receipt_ref",
            "reasoning_checkpoint_receipt_hash",
            "reasoning_checkpoint_recorded_at",
        )
    )
    with op.batch_alter_table("ar_stage_attempts", recreate="always") as batch:
        batch.add_column(
            sa.Column("reasoning_checkpoint_ref", sa.String(96), nullable=True)
        )
        batch.add_column(
            sa.Column("reasoning_checkpoint_json", sa.Text(), nullable=True)
        )
        batch.add_column(
            sa.Column("reasoning_checkpoint_hash", sa.String(64), nullable=True)
        )
        batch.add_column(
            sa.Column(
                "reasoning_checkpoint_review_json", sa.Text(), nullable=True
            )
        )
        batch.add_column(
            sa.Column(
                "reasoning_checkpoint_review_hash", sa.String(64), nullable=True
            )
        )
        batch.add_column(
            sa.Column(
                "reasoning_checkpoint_receipt_ref", sa.String(96), nullable=True
            )
        )
        batch.add_column(
            sa.Column(
                "reasoning_checkpoint_receipt_hash", sa.String(64), nullable=True
            )
        )
        batch.add_column(
            sa.Column(
                "reasoning_checkpoint_recorded_at", sa.Float(), nullable=True
            )
        )
        batch.create_check_constraint(
            "ck_ar_stage_attempts_reasoning_checkpoint_all_or_none",
            f"({all_null}) OR ({all_present} AND "
            "length(reasoning_checkpoint_hash) = 64 AND "
            "length(reasoning_checkpoint_review_hash) = 64 AND "
            "length(reasoning_checkpoint_receipt_hash) = 64)",
        )
    op.create_index(
        "uq_ar_stage_attempts_reasoning_checkpoint_ref",
        "ar_stage_attempts",
        ["reasoning_checkpoint_ref"],
        unique=True,
        sqlite_where=sa.text("reasoning_checkpoint_ref IS NOT NULL"),
    )
    op.create_index(
        "uq_ar_stage_attempts_reasoning_checkpoint_receipt_ref",
        "ar_stage_attempts",
        ["reasoning_checkpoint_receipt_ref"],
        unique=True,
        sqlite_where=sa.text("reasoning_checkpoint_receipt_ref IS NOT NULL"),
    )


def _drop_reasoning_checkpoint_columns() -> None:
    op.drop_index(
        "uq_ar_stage_attempts_reasoning_checkpoint_receipt_ref",
        table_name="ar_stage_attempts",
    )
    op.drop_index(
        "uq_ar_stage_attempts_reasoning_checkpoint_ref",
        table_name="ar_stage_attempts",
    )
    with op.batch_alter_table("ar_stage_attempts", recreate="always") as batch:
        batch.drop_constraint(
            "ck_ar_stage_attempts_reasoning_checkpoint_all_or_none",
            type_="check",
        )
        for name in (
            "reasoning_checkpoint_recorded_at",
            "reasoning_checkpoint_receipt_hash",
            "reasoning_checkpoint_receipt_ref",
            "reasoning_checkpoint_review_hash",
            "reasoning_checkpoint_review_json",
            "reasoning_checkpoint_hash",
            "reasoning_checkpoint_json",
            "reasoning_checkpoint_ref",
        ):
            batch.drop_column(name)


def upgrade() -> None:
    _add_reasoning_checkpoint_columns()
    _replace_provider_units(include_reasoning=True)
    _replace_stage_commits(include_reasoning_closure=True)
    op.add_column(
        "research_memory_state",
        _counter("question_literature_revision_count"),
    )
    op.add_column(
        "research_memory_state",
        _counter("reasoning_content_count"),
    )
    op.add_column(
        "research_memory_state",
        _counter("reasoning_scientific_candidate_count"),
    )
    op.add_column(
        "research_graph_state",
        _counter("reasoning_outcome_count"),
    )
    op.add_column(
        "research_graph_state",
        _counter("reasoning_rejection_count"),
    )
    op.add_column(
        "research_graph_state",
        _counter("reasoning_scientific_outcome_count"),
    )
    op.add_column(
        "research_graph_state",
        _counter("reasoning_scientific_rejection_count"),
    )

    # A QuestionLiteratureRevision is not a relabelled LiteratureSnapshot.  It
    # freezes the exact record-level evidence surface for one accepted
    # Question and retains the source snapshot receipt as provenance.
    op.create_table(
        "rm_question_literature_revisions",
        sa.Column("revision_ref", sa.String(96), primary_key=True),
        sa.Column("question_ref", sa.String(96), nullable=False),
        sa.Column("quest_ref", sa.String(96), nullable=False),
        sa.Column("question_content_ref", sa.String(96), nullable=False),
        sa.Column("question_content_hash", sa.String(64), nullable=False),
        sa.Column("question_receipt_ref", sa.String(96), nullable=False),
        sa.Column("question_receipt_hash", sa.String(64), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("source_snapshot_ref", sa.String(96), nullable=False),
        sa.Column("source_snapshot_hash", sa.String(64), nullable=False),
        sa.Column("source_snapshot_receipt_ref", sa.String(96), nullable=False),
        sa.Column("source_snapshot_receipt_hash", sa.String(64), nullable=False),
        sa.Column("records_json", sa.Text(), nullable=False),
        sa.Column("records_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_snapshot_ref"], ["rm_literature_snapshots.snapshot_ref"]
        ),
        sa.UniqueConstraint("question_ref", "revision_number"),
        sa.CheckConstraint("revision_number >= 1"),
        *(
            _hash(name)
            for name in (
                "question_content_hash",
                "question_receipt_hash",
                "source_snapshot_hash",
                "source_snapshot_receipt_hash",
                "records_hash",
                "request_hash",
                "receipt_hash",
            )
        ),
    )
    op.create_index(
        "ix_rm_question_literature_revisions_current",
        "rm_question_literature_revisions",
        ["question_ref", "revision_number"],
    )

    op.create_table(
        "rm_reasoning_scientific_candidates",
        sa.Column("content_ref", sa.String(96), primary_key=True),
        sa.Column("request_ref", sa.String(96), nullable=False),
        sa.Column("cycle_ref", sa.String(96), nullable=False),
        sa.Column("foreground_epoch", sa.Integer(), nullable=False),
        sa.Column("context_pack_ref", sa.String(96), nullable=False),
        sa.Column("context_pack_json", sa.Text(), nullable=False),
        sa.Column("context_pack_hash", sa.String(64), nullable=False),
        sa.Column("stage_request_receipt_ref", sa.String(96), nullable=False),
        sa.Column("stage_request_receipt_hash", sa.String(64), nullable=False),
        sa.Column("run_ref", sa.String(96), nullable=False),
        sa.Column("attempt_ref", sa.String(96), nullable=False),
        sa.Column("fence_ref", sa.String(96), nullable=False),
        sa.Column("submission_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("checkpoint_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("checkpoint_json", sa.Text(), nullable=False),
        sa.Column("checkpoint_hash", sa.String(64), nullable=False),
        sa.Column("scientific_outcome_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("scientific_outcome_json", sa.Text(), nullable=False),
        sa.Column("outcome_hash", sa.String(64), nullable=False),
        sa.Column("scientific_disposition", sa.String(32), nullable=False),
        sa.Column("autonomous_scope_json", sa.Text(), nullable=False),
        sa.Column("autonomous_scope_hash", sa.String(64), nullable=False),
        sa.Column("evidence_closure_json", sa.Text(), nullable=False),
        sa.Column("evidence_closure_hash", sa.String(64), nullable=False),
        sa.Column("review_json", sa.Text(), nullable=False),
        sa.Column("reviewed_draft_hash", sa.String(64), nullable=False),
        sa.Column("review_hash", sa.String(64), nullable=False),
        sa.Column("object_path", sa.String(512), nullable=False),
        sa.Column("checkpoint_receipt_kind", sa.String(64), nullable=False),
        sa.Column("checkpoint_receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("checkpoint_receipt_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.CheckConstraint("foreground_epoch >= 1"),
        sa.CheckConstraint(
            "scientific_disposition IN ('affirmed', 'denied', 'uncertain', "
            "'insufficient_evidence')"
        ),
        sa.CheckConstraint(
            "checkpoint_receipt_kind = 'reasoning_autonomous_checkpoint'"
        ),
        *(
            _hash(name)
            for name in (
                "context_pack_hash",
                "stage_request_receipt_hash",
                "checkpoint_hash",
                "outcome_hash",
                "autonomous_scope_hash",
                "evidence_closure_hash",
                "reviewed_draft_hash",
                "review_hash",
                "checkpoint_receipt_hash",
                "receipt_hash",
            )
        ),
    )
    op.create_index(
        "ix_rm_reasoning_scientific_candidates_request_ref",
        "rm_reasoning_scientific_candidates",
        ["request_ref"],
    )

    op.create_table(
        "rm_reasoning_contents",
        sa.Column("content_ref", sa.String(96), primary_key=True),
        sa.Column("request_ref", sa.String(96), nullable=False),
        sa.Column("cycle_ref", sa.String(96), nullable=False),
        sa.Column("foreground_epoch", sa.Integer(), nullable=False),
        sa.Column("context_pack_ref", sa.String(96), nullable=False),
        sa.Column("context_pack_json", sa.Text(), nullable=False),
        sa.Column("context_pack_hash", sa.String(64), nullable=False),
        sa.Column("stage_request_receipt_ref", sa.String(96), nullable=False),
        sa.Column("stage_request_receipt_hash", sa.String(64), nullable=False),
        sa.Column("run_ref", sa.String(96), nullable=False),
        sa.Column("attempt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("fence_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("submission_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("outcome_json", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("scientific_outcome_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("scientific_outcome_json", sa.Text(), nullable=False),
        sa.Column("outcome_hash", sa.String(64), nullable=False),
        sa.Column("scientific_disposition", sa.String(32), nullable=False),
        sa.Column("transition_kind", sa.String(32), nullable=False),
        sa.Column("transition_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("transition_json", sa.Text(), nullable=False),
        sa.Column("transition_hash", sa.String(64), nullable=False),
        sa.Column("evidence_closure_json", sa.Text(), nullable=False),
        sa.Column("evidence_closure_hash", sa.String(64), nullable=False),
        sa.Column("reviewed_draft_json", sa.Text(), nullable=False),
        sa.Column("reviewed_draft_hash", sa.String(64), nullable=False),
        sa.Column("review_json", sa.Text(), nullable=False),
        sa.Column("review_hash", sa.String(64), nullable=False),
        sa.Column("scientific_candidate_content_ref", sa.String(96), nullable=True),
        sa.Column(
            "scientific_candidate_content_receipt_ref",
            sa.String(96),
            nullable=True,
        ),
        sa.Column(
            "scientific_candidate_content_receipt_hash",
            sa.String(64),
            nullable=True,
        ),
        sa.Column(
            "scientific_candidate_domain_receipt_ref",
            sa.String(96),
            nullable=True,
        ),
        sa.Column(
            "scientific_candidate_domain_receipt_hash",
            sa.String(64),
            nullable=True,
        ),
        sa.Column("object_path", sa.String(512), nullable=False),
        sa.Column("execution_receipt_kind", sa.String(64), nullable=False),
        sa.Column("execution_receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("execution_receipt_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["scientific_candidate_content_ref"],
            ["rm_reasoning_scientific_candidates.content_ref"],
        ),
        sa.CheckConstraint("foreground_epoch >= 1"),
        sa.CheckConstraint(
            "scientific_disposition IN ('affirmed', 'denied', 'uncertain', "
            "'insufficient_evidence')"
        ),
        sa.CheckConstraint(
            "transition_kind IN ('next_cycle_proposal', 'candidate_completion')"
        ),
        sa.CheckConstraint(
            "execution_receipt_kind = 'reasoning_attempt_execution'"
        ),
        sa.CheckConstraint(
            "(scientific_candidate_content_ref IS NULL AND "
            "scientific_candidate_content_receipt_ref IS NULL AND "
            "scientific_candidate_content_receipt_hash IS NULL AND "
            "scientific_candidate_domain_receipt_ref IS NULL AND "
            "scientific_candidate_domain_receipt_hash IS NULL) OR "
            "(scientific_candidate_content_ref IS NOT NULL AND "
            "scientific_candidate_content_receipt_ref IS NOT NULL AND "
            "length(scientific_candidate_content_receipt_hash) = 64 AND "
            "scientific_candidate_domain_receipt_ref IS NOT NULL AND "
            "length(scientific_candidate_domain_receipt_hash) = 64)"
        ),
        *(
            _hash(name)
            for name in (
                "context_pack_hash",
                "stage_request_receipt_hash",
                "payload_hash",
                "outcome_hash",
                "transition_hash",
                "evidence_closure_hash",
                "reviewed_draft_hash",
                "review_hash",
                "execution_receipt_hash",
                "receipt_hash",
            )
        ),
    )
    op.create_index(
        "ix_rm_reasoning_contents_request_ref",
        "rm_reasoning_contents",
        ["request_ref"],
    )

    op.create_table(
        "rg_reasoning_scientific_decisions",
        sa.Column("decision_ref", sa.String(96), primary_key=True),
        sa.Column("request_ref", sa.String(96), nullable=False),
        sa.Column("submission_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("run_ref", sa.String(96), nullable=False),
        sa.Column("attempt_ref", sa.String(96), nullable=False),
        sa.Column("fence_ref", sa.String(96), nullable=False),
        sa.Column("checkpoint_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("reasoning_content_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("reasoning_content_receipt_ref", sa.String(96), nullable=False),
        sa.Column("reasoning_content_receipt_hash", sa.String(64), nullable=False),
        sa.Column("checkpoint_hash", sa.String(64), nullable=False),
        sa.Column("scientific_outcome_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("outcome_hash", sa.String(64), nullable=False),
        sa.Column("scientific_disposition", sa.String(32), nullable=False),
        sa.Column("autonomous_scope_hash", sa.String(64), nullable=False),
        sa.Column("review_hash", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("outcome_ref", sa.String(96), nullable=True),
        sa.Column("reason_code", sa.String(128), nullable=True),
        sa.Column("feedback_json", sa.Text(), nullable=False),
        sa.Column("feedback_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("decided_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["reasoning_content_ref"],
            ["rm_reasoning_scientific_candidates.content_ref"],
        ),
        sa.CheckConstraint("decision IN ('accepted', 'rejected')"),
        sa.CheckConstraint(
            "scientific_disposition IN ('affirmed', 'denied', 'uncertain', "
            "'insufficient_evidence')"
        ),
        sa.CheckConstraint(
            "(decision = 'accepted' AND outcome_ref = scientific_outcome_ref "
            "AND reason_code IS NULL) OR (decision = 'rejected' AND "
            "outcome_ref IS NULL AND reason_code IS NOT NULL)"
        ),
        *(
            _hash(name)
            for name in (
                "reasoning_content_receipt_hash",
                "checkpoint_hash",
                "outcome_hash",
                "autonomous_scope_hash",
                "review_hash",
                "feedback_hash",
                "receipt_hash",
            )
        ),
    )
    op.create_index(
        "ix_rg_reasoning_scientific_decisions_request_ref",
        "rg_reasoning_scientific_decisions",
        ["request_ref"],
    )

    op.create_table(
        "rg_reasoning_outcome_decisions",
        sa.Column("decision_ref", sa.String(96), primary_key=True),
        sa.Column("request_ref", sa.String(96), nullable=False),
        sa.Column("submission_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("run_ref", sa.String(96), nullable=False),
        sa.Column("attempt_ref", sa.String(96), nullable=False),
        sa.Column("fence_ref", sa.String(96), nullable=False),
        sa.Column("reasoning_content_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("reasoning_content_receipt_ref", sa.String(96), nullable=False),
        sa.Column("reasoning_content_receipt_hash", sa.String(64), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("scientific_outcome_ref", sa.String(96), nullable=False),
        sa.Column("outcome_hash", sa.String(64), nullable=False),
        sa.Column("scientific_disposition", sa.String(32), nullable=False),
        sa.Column("transition_kind", sa.String(32), nullable=False),
        sa.Column("transition_ref", sa.String(96), nullable=False),
        sa.Column("transition_json", sa.Text(), nullable=False),
        sa.Column("transition_hash", sa.String(64), nullable=False),
        sa.Column("reviewed_draft_hash", sa.String(64), nullable=False),
        sa.Column("review_hash", sa.String(64), nullable=False),
        sa.Column("scientific_candidate_content_ref", sa.String(96), nullable=True),
        sa.Column(
            "scientific_candidate_content_receipt_ref",
            sa.String(96),
            nullable=True,
        ),
        sa.Column(
            "scientific_candidate_content_receipt_hash",
            sa.String(64),
            nullable=True,
        ),
        sa.Column(
            "scientific_candidate_domain_receipt_ref",
            sa.String(96),
            nullable=True,
        ),
        sa.Column(
            "scientific_candidate_domain_receipt_hash",
            sa.String(64),
            nullable=True,
        ),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("outcome_ref", sa.String(96), nullable=True),
        sa.Column("reason_code", sa.String(128), nullable=True),
        sa.Column("feedback_json", sa.Text(), nullable=False),
        sa.Column("feedback_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("decided_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["reasoning_content_ref"], ["rm_reasoning_contents.content_ref"]
        ),
        sa.ForeignKeyConstraint(
            ["scientific_candidate_content_ref"],
            ["rm_reasoning_scientific_candidates.content_ref"],
        ),
        sa.CheckConstraint("decision IN ('accepted', 'rejected')"),
        sa.CheckConstraint(
            "scientific_disposition IN ('affirmed', 'denied', 'uncertain', "
            "'insufficient_evidence')"
        ),
        sa.CheckConstraint(
            "transition_kind IN ('next_cycle_proposal', 'candidate_completion')"
        ),
        sa.CheckConstraint(
            "(decision = 'accepted' AND outcome_ref = scientific_outcome_ref "
            "AND reason_code IS NULL) OR (decision = 'rejected' AND "
            "outcome_ref IS NULL AND reason_code IS NOT NULL)"
        ),
        sa.CheckConstraint(
            "(scientific_candidate_content_ref IS NULL AND "
            "scientific_candidate_content_receipt_ref IS NULL AND "
            "scientific_candidate_content_receipt_hash IS NULL AND "
            "scientific_candidate_domain_receipt_ref IS NULL AND "
            "scientific_candidate_domain_receipt_hash IS NULL) OR "
            "(scientific_candidate_content_ref IS NOT NULL AND "
            "scientific_candidate_content_receipt_ref IS NOT NULL AND "
            "length(scientific_candidate_content_receipt_hash) = 64 AND "
            "scientific_candidate_domain_receipt_ref IS NOT NULL AND "
            "length(scientific_candidate_domain_receipt_hash) = 64)"
        ),
        *(
            _hash(name)
            for name in (
                "reasoning_content_receipt_hash",
                "payload_hash",
                "outcome_hash",
                "transition_hash",
                "reviewed_draft_hash",
                "review_hash",
                "feedback_hash",
                "receipt_hash",
            )
        ),
    )
    op.create_index(
        "ix_rg_reasoning_outcome_decisions_request_ref",
        "rg_reasoning_outcome_decisions",
        ["request_ref"],
    )
    op.create_index(
        "uq_rg_reasoning_outcome_decisions_one_accepted",
        "rg_reasoning_outcome_decisions",
        ["request_ref"],
        unique=True,
        sqlite_where=sa.text("decision = 'accepted'"),
    )


def downgrade() -> None:
    _assert_reasoning_owner_downgrade_safe()
    op.drop_index(
        "uq_rg_reasoning_outcome_decisions_one_accepted",
        table_name="rg_reasoning_outcome_decisions",
    )
    op.drop_index(
        "ix_rg_reasoning_outcome_decisions_request_ref",
        table_name="rg_reasoning_outcome_decisions",
    )
    op.drop_table("rg_reasoning_outcome_decisions")
    op.drop_index(
        "ix_rg_reasoning_scientific_decisions_request_ref",
        table_name="rg_reasoning_scientific_decisions",
    )
    op.drop_table("rg_reasoning_scientific_decisions")
    op.drop_index(
        "ix_rm_reasoning_contents_request_ref",
        table_name="rm_reasoning_contents",
    )
    op.drop_table("rm_reasoning_contents")
    op.drop_index(
        "ix_rm_reasoning_scientific_candidates_request_ref",
        table_name="rm_reasoning_scientific_candidates",
    )
    op.drop_table("rm_reasoning_scientific_candidates")
    op.drop_index(
        "ix_rm_question_literature_revisions_current",
        table_name="rm_question_literature_revisions",
    )
    op.drop_table("rm_question_literature_revisions")
    op.drop_column(
        "research_graph_state", "reasoning_scientific_rejection_count"
    )
    op.drop_column(
        "research_graph_state", "reasoning_scientific_outcome_count"
    )
    op.drop_column("research_graph_state", "reasoning_rejection_count")
    op.drop_column("research_graph_state", "reasoning_outcome_count")
    op.drop_column(
        "research_memory_state", "reasoning_scientific_candidate_count"
    )
    op.drop_column("research_memory_state", "reasoning_content_count")
    op.drop_column(
        "research_memory_state",
        "question_literature_revision_count",
    )
    _replace_stage_commits(include_reasoning_closure=False)
    _replace_provider_units(include_reasoning=False)
    _drop_reasoning_checkpoint_columns()
