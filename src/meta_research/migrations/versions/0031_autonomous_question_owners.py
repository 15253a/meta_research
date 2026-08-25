"""Add AutonomousCreation RM content and RG Question facts.

Revision ID: 0031_autonomous_question_owners
Revises: 0030_reasoning_owner_acceptance
Create Date: 2026-08-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0031_autonomous_question_owners"
down_revision = "0030_reasoning_owner_acceptance"
branch_labels = None
depends_on = None


def _counter(name: str) -> sa.Column:
    return sa.Column(name, sa.Integer(), nullable=False, server_default="0")


def _hash(name: str, *, nullable: bool = False) -> sa.CheckConstraint:
    if nullable:
        return sa.CheckConstraint(f"{name} IS NULL OR length({name}) = 64")
    return sa.CheckConstraint(f"length({name}) = 64")


def _assert_downgrade_safe() -> None:
    connection = op.get_bind()
    snapshot_count = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM rm_literature_snapshots WHERE "
            "creation_context_kind = 'autonomous_question_creation'"
        )
    ).scalar_one()
    owner_fact_count = sum(
        int(
            connection.execute(
                sa.text(f"SELECT COUNT(*) FROM {table_name}")
            ).scalar_one()
        )
        for table_name in (
            "rm_autonomous_question_contents",
            "rg_autonomous_questions",
            "rg_question_anchors",
            "rg_question_selection_facts",
        )
    )
    memory_counter = connection.execute(
        sa.text(
            "SELECT autonomous_question_content_count FROM "
            "research_memory_state WHERE singleton = 'owner'"
        )
    ).scalar_one()
    graph_counters = connection.execute(
        sa.text(
            "SELECT autonomous_question_count, question_anchor_count, "
            "graph_presence_fact_count, question_research_state_fact_count "
            "FROM research_graph_state WHERE singleton = 'owner'"
        )
    ).one()
    if (
        int(snapshot_count) != 0
        or owner_fact_count != 0
        or int(memory_counter) != 0
        or any(int(value) != 0 for value in graph_counters)
    ):
        raise RuntimeError(
            "0031 downgrade blocked: autonomous Question Owner facts exist"
        )


def upgrade() -> None:
    op.add_column(
        "rm_literature_snapshots",
        sa.Column("context_generation", sa.Integer(), nullable=True),
    )
    op.add_column(
        "rm_literature_snapshots",
        sa.Column("context_basis_hash", sa.String(64), nullable=True),
    )
    with op.batch_alter_table(
        "rm_literature_snapshots", recreate="always"
    ) as batch:
        batch.create_check_constraint(
            "ck_rm_literature_snapshots_autonomous_context",
            "(creation_context_kind != 'autonomous_question_creation' AND "
            "context_generation IS NULL AND context_basis_hash IS NULL) OR "
            "(creation_context_kind = 'autonomous_question_creation' AND "
            "creation_context_ref IS NOT NULL AND quest_ref IS NOT NULL AND "
            "context_generation >= 1 AND length(context_basis_hash) = 64)",
        )
    op.create_index(
        "uq_rm_literature_snapshot_autonomous_context",
        "rm_literature_snapshots",
        ["creation_context_kind", "creation_context_ref", "context_generation"],
        unique=True,
        sqlite_where=sa.text(
            "creation_context_kind = 'autonomous_question_creation'"
        ),
    )

    op.add_column(
        "research_memory_state", _counter("autonomous_question_content_count")
    )
    for name in (
        "autonomous_question_count",
        "question_anchor_count",
        "graph_presence_fact_count",
        "question_research_state_fact_count",
    ):
        op.add_column("research_graph_state", _counter(name))

    op.create_table(
        "rm_autonomous_question_contents",
        sa.Column("content_ref", sa.String(96), primary_key=True),
        sa.Column("context_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("reasoning_checkpoint_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("reasoning_checkpoint_hash", sa.String(64), nullable=False),
        sa.Column("source_scientific_outcome_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("source_candidate_content_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("source_candidate_receipt_ref", sa.String(96), nullable=False),
        sa.Column("source_candidate_receipt_hash", sa.String(64), nullable=False),
        sa.Column("source_scientific_receipt_ref", sa.String(96), nullable=False),
        sa.Column("source_scientific_receipt_hash", sa.String(64), nullable=False),
        sa.Column("source_stage_request_ref", sa.String(96), nullable=False),
        sa.Column("source_cycle_ref", sa.String(96), nullable=False),
        sa.Column("source_foreground_epoch", sa.Integer(), nullable=False),
        sa.Column("source_quest_ref", sa.String(96), nullable=False),
        sa.Column("source_question_ref", sa.String(96), nullable=False),
        sa.Column("autonomous_scope_hash", sa.String(64), nullable=False),
        sa.Column("literature_snapshot_ref", sa.String(96), nullable=False),
        sa.Column("literature_snapshot_hash", sa.String(64), nullable=False),
        sa.Column("literature_snapshot_receipt_ref", sa.String(96), nullable=False),
        sa.Column("literature_snapshot_receipt_hash", sa.String(64), nullable=False),
        sa.Column("proposal_json", sa.Text(), nullable=False),
        sa.Column("proposal_hash", sa.String(64), nullable=False),
        sa.Column("question_json", sa.Text(), nullable=False),
        sa.Column("question_hash", sa.String(64), nullable=False),
        sa.Column("schema_ref", sa.String(96), nullable=False),
        sa.Column("object_path", sa.String(512), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_candidate_content_ref"],
            ["rm_reasoning_scientific_candidates.content_ref"],
        ),
        sa.ForeignKeyConstraint(
            ["literature_snapshot_ref"],
            ["rm_literature_snapshots.snapshot_ref"],
        ),
        sa.CheckConstraint("source_foreground_epoch >= 1"),
        sa.CheckConstraint(
            "schema_ref = 'meta-research/formal-question-content/v1'"
        ),
        *(
            _hash(name)
            for name in (
                "source_candidate_receipt_hash",
                "source_scientific_receipt_hash",
                "reasoning_checkpoint_hash",
                "autonomous_scope_hash",
                "literature_snapshot_hash",
                "literature_snapshot_receipt_hash",
                "proposal_hash",
                "question_hash",
                "request_hash",
                "receipt_hash",
            )
        ),
    )

    op.create_table(
        "rg_autonomous_questions",
        sa.Column("question_ref", sa.String(96), primary_key=True),
        sa.Column("initialization_id", sa.String(96), nullable=False),
        sa.Column("quest_ref", sa.String(96), nullable=False),
        sa.Column("parent_question_ref", sa.String(96), nullable=True),
        sa.Column("context_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("reasoning_checkpoint_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("reasoning_checkpoint_hash", sa.String(64), nullable=False),
        sa.Column("source_scientific_outcome_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("source_stage_request_ref", sa.String(96), nullable=False),
        sa.Column("source_cycle_ref", sa.String(96), nullable=False),
        sa.Column("source_foreground_epoch", sa.Integer(), nullable=False),
        sa.Column("literature_snapshot_ref", sa.String(96), nullable=False),
        sa.Column("content_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("schema_ref", sa.String(96), nullable=False),
        sa.Column("content_receipt_ref", sa.String(96), nullable=False),
        sa.Column("content_receipt_hash", sa.String(64), nullable=False),
        sa.Column("dispatch_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("dispatch_receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("dispatch_receipt_hash", sa.String(64), nullable=False),
        sa.Column("graph_revision_ref", sa.String(96), nullable=False),
        sa.Column("graph_revision_number", sa.Integer(), nullable=False),
        sa.Column("entry_stage", sa.String(16), nullable=False),
        sa.Column("typed_skip_basis_refs_json", sa.Text(), nullable=False),
        sa.Column("typed_skip_basis_refs_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("aggregate_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("aggregate_receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("aggregate_receipt_hash", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["quest_ref"], ["rg_quests.quest_ref"]),
        sa.ForeignKeyConstraint(
            ["content_ref"], ["rm_autonomous_question_contents.content_ref"]
        ),
        sa.CheckConstraint(
            "graph_revision_number >= 1 AND source_foreground_epoch >= 1"
        ),
        *(
            _hash(name)
            for name in (
                "content_hash",
                "reasoning_checkpoint_hash",
                "content_receipt_hash",
                "dispatch_receipt_hash",
                "request_hash",
                "receipt_hash",
                "aggregate_receipt_hash",
                "typed_skip_basis_refs_hash",
            )
        ),
    )
    op.create_index(
        "ix_rg_autonomous_questions_quest_ref",
        "rg_autonomous_questions",
        ["quest_ref"],
    )

    op.create_table(
        "rg_question_anchors",
        sa.Column("anchor_ref", sa.String(96), primary_key=True),
        sa.Column("question_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("quest_ref", sa.String(96), nullable=False),
        sa.Column("content_ref", sa.String(96), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("graph_revision_ref", sa.String(96), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["question_ref"], ["rg_autonomous_questions.question_ref"]
        ),
        _hash("content_hash"),
        _hash("receipt_hash"),
    )

    op.create_table(
        "rg_question_selection_facts",
        sa.Column("fact_ref", sa.String(96), primary_key=True),
        sa.Column("question_ref", sa.String(96), nullable=False),
        sa.Column("quest_ref", sa.String(96), nullable=False),
        sa.Column("fact_kind", sa.String(48), nullable=False),
        sa.Column("fact_value", sa.String(16), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("graph_revision_ref", sa.String(96), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["question_ref"], ["rg_autonomous_questions.question_ref"]
        ),
        sa.UniqueConstraint("question_ref", "fact_kind"),
        sa.CheckConstraint(
            "(fact_kind = 'GraphPresenceFact' AND fact_value = 'present') OR "
            "(fact_kind = 'QuestionResearchStateFact' AND fact_value = 'open')"
        ),
        _hash("receipt_hash"),
    )


def downgrade() -> None:
    _assert_downgrade_safe()
    op.drop_table("rg_question_selection_facts")
    op.drop_table("rg_question_anchors")
    op.drop_index(
        "ix_rg_autonomous_questions_quest_ref",
        table_name="rg_autonomous_questions",
    )
    op.drop_table("rg_autonomous_questions")
    op.drop_table("rm_autonomous_question_contents")
    for name in (
        "question_research_state_fact_count",
        "graph_presence_fact_count",
        "question_anchor_count",
        "autonomous_question_count",
    ):
        op.drop_column("research_graph_state", name)
    op.drop_column("research_memory_state", "autonomous_question_content_count")
    op.drop_index(
        "uq_rm_literature_snapshot_autonomous_context",
        table_name="rm_literature_snapshots",
    )
    with op.batch_alter_table(
        "rm_literature_snapshots", recreate="always"
    ) as batch:
        batch.drop_constraint(
            "ck_rm_literature_snapshots_autonomous_context",
            type_="check",
        )
        batch.drop_column("context_basis_hash")
        batch.drop_column("context_generation")
