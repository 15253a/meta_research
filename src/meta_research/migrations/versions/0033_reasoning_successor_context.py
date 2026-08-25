"""Persist Reasoning successor ContextPacks and generic selection facts.

Revision ID: 0033_reasoning_successor_context
Revises: 0032_autonomous_completion
Create Date: 2026-08-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0033_reasoning_successor_context"
down_revision = "0032_autonomous_completion"
branch_labels = None
depends_on = None


_SELECTION_COLUMNS = (
    "fact_ref",
    "question_ref",
    "quest_ref",
    "fact_kind",
    "fact_value",
    "is_current",
    "graph_revision_ref",
    "receipt_ref",
    "receipt_hash",
    "accepted_at",
)


def _create_selection_facts_table(
    *, autonomous_only: bool, versioned: bool
) -> None:
    constraints: list[sa.SchemaItem] = [
        sa.UniqueConstraint(
            "question_ref",
            "fact_kind",
            *("graph_revision_ref",) if versioned else (),
        ),
        sa.CheckConstraint(
            "(fact_kind = 'GraphPresenceFact' AND fact_value = 'present') OR "
            "(fact_kind = 'QuestionResearchStateFact' AND fact_value = 'open')"
        ),
        sa.CheckConstraint("length(receipt_hash) = 64"),
    ]
    if autonomous_only:
        constraints.append(
            sa.ForeignKeyConstraint(
                ["question_ref"], ["rg_autonomous_questions.question_ref"]
            )
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
        *constraints,
    )


def _rebuild_selection_facts(
    *, autonomous_only: bool, versioned: bool
) -> None:
    archived = "rg_question_selection_facts_0033_old"
    op.rename_table("rg_question_selection_facts", archived)
    _create_selection_facts_table(
        autonomous_only=autonomous_only,
        versioned=versioned,
    )
    columns = ", ".join(_SELECTION_COLUMNS)
    op.execute(
        sa.text(
            f"INSERT INTO rg_question_selection_facts ({columns}) "
            f"SELECT {columns} FROM {archived}"
        )
    )
    op.drop_table(archived)


def upgrade() -> None:
    # 0031 accidentally constrained issuer-owned selection facts to only
    # Autonomous Questions.  Reasoning also needs the exact current root/manual
    # source Question to carry independent present/open receipts.
    _rebuild_selection_facts(autonomous_only=False, versioned=True)
    with op.batch_alter_table("rg_reasoning_outcome_decisions") as batch:
        batch.add_column(sa.Column("target_aggregate_json", sa.Text()))
        batch.add_column(sa.Column("target_aggregate_hash", sa.String(64)))
        batch.create_check_constraint(
            "ck_rg_reasoning_outcome_target_aggregate_pair",
            "(target_aggregate_json IS NULL AND target_aggregate_hash IS NULL) OR "
            "(target_aggregate_json IS NOT NULL AND "
            "length(target_aggregate_hash) = 64)",
        )
    with op.batch_alter_table("ae_cycles") as batch:
        batch.add_column(sa.Column("idea_context_pack_json", sa.Text()))
        batch.add_column(
            sa.Column("idea_context_pack_hash", sa.String(64))
        )
        batch.create_check_constraint(
            "ck_ae_cycles_idea_context_pack_pair",
            "(idea_context_pack_json IS NULL AND idea_context_pack_hash IS NULL) OR "
            "(idea_context_pack_json IS NOT NULL AND "
            "length(idea_context_pack_hash) = 64)",
        )


def downgrade() -> None:
    connection = op.get_bind()
    accepted_targets = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM rg_reasoning_outcome_decisions WHERE "
            "target_aggregate_json IS NOT NULL"
        )
    ).scalar_one()
    if int(accepted_targets):
        raise RuntimeError(
            "0033 downgrade blocked: Reasoning target aggregates exist"
        )
    count = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM ae_cycles WHERE idea_context_pack_json IS NOT NULL"
        )
    ).scalar_one()
    if int(count):
        raise RuntimeError(
            "0033 downgrade blocked: autonomous successor context facts exist"
        )
    versioned_facts = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM (SELECT question_ref, fact_kind FROM "
            "rg_question_selection_facts GROUP BY question_ref, fact_kind "
            "HAVING COUNT(DISTINCT graph_revision_ref) > 1)"
        )
    ).scalar_one()
    if int(versioned_facts):
        raise RuntimeError(
            "0033 downgrade blocked: versioned Question selection facts exist"
        )
    non_autonomous = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM rg_question_selection_facts facts LEFT JOIN "
            "rg_autonomous_questions questions ON questions.question_ref = "
            "facts.question_ref WHERE questions.question_ref IS NULL"
        )
    ).scalar_one()
    if int(non_autonomous):
        raise RuntimeError(
            "0033 downgrade blocked: source-current Question selection facts exist"
        )
    with op.batch_alter_table("ae_cycles") as batch:
        batch.drop_constraint(
            "ck_ae_cycles_idea_context_pack_pair",
            type_="check",
        )
        batch.drop_column("idea_context_pack_hash")
        batch.drop_column("idea_context_pack_json")
    with op.batch_alter_table("rg_reasoning_outcome_decisions") as batch:
        batch.drop_constraint(
            "ck_rg_reasoning_outcome_target_aggregate_pair",
            type_="check",
        )
        batch.drop_column("target_aggregate_hash")
        batch.drop_column("target_aggregate_json")
    _rebuild_selection_facts(autonomous_only=True, versioned=False)
