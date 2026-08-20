"""Add the Quest-initialization write models owned by HC, RG, RM, and AE.

Revision ID: 0002_quest_initialization
Revises: 0001_greenfield
Create Date: 2026-08-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0002_quest_initialization"
down_revision = "0001_greenfield"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "research_memory_state",
        sa.Column(
            "formal_content_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )

    op.create_table(
        "hc_quest_initializations",
        sa.Column("initialization_id", sa.String(length=64), primary_key=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("draft_revision", sa.Integer(), nullable=False),
        sa.Column("draft_json", sa.Text(), nullable=False),
        sa.Column("draft_hash", sa.String(length=64), nullable=False),
        sa.Column("proposal_revision", sa.Integer(), nullable=False),
        sa.Column("proposal_ref", sa.String(length=64), nullable=True, unique=True),
        sa.Column("proposal_json", sa.Text(), nullable=True),
        sa.Column("proposal_hash", sa.String(length=64), nullable=True),
        sa.Column("proposal_basis_revision", sa.Integer(), nullable=True),
        sa.Column("proposal_basis_hash", sa.String(length=64), nullable=True),
        sa.Column("preview_ref", sa.String(length=64), nullable=True, unique=True),
        sa.Column("preview_hash", sa.String(length=64), nullable=True),
        sa.Column("preview_json", sa.Text(), nullable=True),
        sa.Column("preview_basis_revision", sa.Integer(), nullable=True),
        sa.Column("preview_basis_hash", sa.String(length=64), nullable=True),
        sa.Column("preview_proposal_ref", sa.String(length=64), nullable=True),
        sa.Column("preview_proposal_hash", sa.String(length=64), nullable=True),
        sa.Column("confirmation_ref", sa.String(length=64), nullable=True, unique=True),
        sa.Column("confirmation_hash", sa.String(length=64), nullable=True),
        sa.Column("confirmed_draft_revision", sa.Integer(), nullable=True),
        sa.Column("confirmed_draft_hash", sa.String(length=64), nullable=True),
        sa.Column("confirmed_proposal_ref", sa.String(length=64), nullable=True),
        sa.Column("confirmed_proposal_hash", sa.String(length=64), nullable=True),
        sa.Column("confirmed_preview_ref", sa.String(length=64), nullable=True),
        sa.Column("confirmed_preview_hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft', 'proposal_ready', 'confirmed', 'completed', 'cancelled')"
        ),
        sa.CheckConstraint("draft_revision >= 1"),
        sa.CheckConstraint("proposal_revision >= 0"),
    )
    op.create_index(
        "ix_hc_quest_initializations_status",
        "hc_quest_initializations",
        ["status", "updated_at"],
    )
    op.create_table(
        "hc_quest_draft_revisions",
        sa.Column("initialization_id", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("draft_json", sa.Text(), nullable=False),
        sa.Column("draft_hash", sa.String(length=64), nullable=False),
        sa.Column("recorded_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("initialization_id", "revision"),
        sa.ForeignKeyConstraint(
            ["initialization_id"],
            ["hc_quest_initializations.initialization_id"],
        ),
    )
    op.create_table(
        "hc_question_proposals",
        sa.Column("proposal_ref", sa.String(length=64), primary_key=True),
        sa.Column("initialization_id", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("basis_revision", sa.Integer(), nullable=False),
        sa.Column("basis_hash", sa.String(length=64), nullable=False),
        sa.Column("content_json", sa.Text(), nullable=False),
        sa.Column("proposal_hash", sa.String(length=64), nullable=False),
        sa.Column("recorded_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("initialization_id", "revision"),
        sa.ForeignKeyConstraint(
            ["initialization_id"],
            ["hc_quest_initializations.initialization_id"],
        ),
    )
    op.create_table(
        "hc_confirmation_previews",
        sa.Column("preview_ref", sa.String(length=64), primary_key=True),
        sa.Column("initialization_id", sa.String(length=64), nullable=False),
        sa.Column("basis_revision", sa.Integer(), nullable=False),
        sa.Column("basis_hash", sa.String(length=64), nullable=False),
        sa.Column("proposal_ref", sa.String(length=64), nullable=False),
        sa.Column("proposal_hash", sa.String(length=64), nullable=False),
        sa.Column("assertions_json", sa.Text(), nullable=False),
        sa.Column("assertions_hash", sa.String(length=64), nullable=False),
        sa.Column("preview_hash", sa.String(length=64), nullable=False),
        sa.Column("recorded_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("initialization_id", "preview_ref"),
        sa.ForeignKeyConstraint(
            ["initialization_id"],
            ["hc_quest_initializations.initialization_id"],
        ),
    )
    op.create_table(
        "hc_confirmation_attempts",
        sa.Column("attempt_ref", sa.String(length=64), primary_key=True),
        sa.Column("initialization_id", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("request_json", sa.Text(), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("reason_code", sa.String(length=96), nullable=True),
        sa.Column("attempted_at", sa.Float(), nullable=False),
        sa.Column("superseded_at", sa.Float(), nullable=True),
        sa.CheckConstraint("decision IN ('stale', 'rejected')"),
        sa.ForeignKeyConstraint(
            ["initialization_id"],
            ["hc_quest_initializations.initialization_id"],
        ),
    )
    op.create_table(
        "hc_quest_initialization_commands",
        sa.Column("idempotency_key", sa.String(length=128), primary_key=True),
        sa.Column("initialization_id", sa.String(length=64), nullable=False),
        sa.Column("command_kind", sa.String(length=32), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("result_ref", sa.String(length=64), nullable=True),
        sa.Column("recorded_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["initialization_id"],
            ["hc_quest_initializations.initialization_id"],
        ),
    )
    op.create_table(
        "hc_quest_dispatch_failures",
        sa.Column("initialization_id", sa.String(length=64), primary_key=True),
        sa.Column("layer", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason_code", sa.String(length=96), nullable=False),
        sa.Column("observed_at", sa.Float(), nullable=False),
        sa.CheckConstraint("status IN ('rejected', 'stale')"),
        sa.ForeignKeyConstraint(
            ["initialization_id"],
            ["hc_quest_initializations.initialization_id"],
        ),
    )

    op.create_table(
        "rm_formal_question_contents",
        sa.Column("content_ref", sa.String(length=64), primary_key=True),
        sa.Column("initialization_id", sa.String(length=64), nullable=False, unique=True),
        sa.Column("quest_ref", sa.String(length=64), nullable=False),
        sa.Column("quest_receipt_ref", sa.String(length=64), nullable=False),
        sa.Column("quest_receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("proposal_ref", sa.String(length=64), nullable=False),
        sa.Column("proposal_hash", sa.String(length=64), nullable=False),
        sa.Column("confirmation_ref", sa.String(length=64), nullable=False),
        sa.Column("confirmation_hash", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("schema_ref", sa.String(length=80), nullable=False),
        sa.Column("content_json", sa.Text(), nullable=False),
        sa.Column("object_path", sa.Text(), nullable=False),
        sa.Column("receipt_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
    )

    op.create_table(
        "rg_quests",
        sa.Column("quest_ref", sa.String(length=64), primary_key=True),
        sa.Column("initialization_id", sa.String(length=64), nullable=False, unique=True),
        sa.Column("draft_revision", sa.Integer(), nullable=False),
        sa.Column("draft_hash", sa.String(length=64), nullable=False),
        sa.Column("proposal_ref", sa.String(length=64), nullable=False),
        sa.Column("proposal_hash", sa.String(length=64), nullable=False),
        sa.Column("preview_ref", sa.String(length=64), nullable=False),
        sa.Column("preview_hash", sa.String(length=64), nullable=False),
        sa.Column("goal_json", sa.Text(), nullable=False),
        sa.Column("confirmation_ref", sa.String(length=64), nullable=False),
        sa.Column("confirmation_hash", sa.String(length=64), nullable=False),
        sa.Column("receipt_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
    )
    op.create_table(
        "rg_questions",
        sa.Column("question_ref", sa.String(length=64), primary_key=True),
        sa.Column("initialization_id", sa.String(length=64), nullable=False, unique=True),
        sa.Column("quest_ref", sa.String(length=64), nullable=False),
        sa.Column("content_ref", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("schema_ref", sa.String(length=80), nullable=False),
        sa.Column("quest_receipt_ref", sa.String(length=64), nullable=False),
        sa.Column("quest_receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("content_receipt_ref", sa.String(length=64), nullable=False),
        sa.Column("content_receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("confirmation_ref", sa.String(length=64), nullable=False),
        sa.Column("receipt_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["quest_ref"], ["rg_quests.quest_ref"]),
    )

    op.create_table(
        "ae_initial_cycles",
        sa.Column("cycle_ref", sa.String(length=64), primary_key=True),
        sa.Column("initialization_id", sa.String(length=64), nullable=False, unique=True),
        sa.Column("quest_ref", sa.String(length=64), nullable=False),
        sa.Column("question_ref", sa.String(length=64), nullable=False),
        sa.Column("question_receipt_ref", sa.String(length=64), nullable=False),
        sa.Column("question_receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("quest_receipt_ref", sa.String(length=64), nullable=False),
        sa.Column("quest_receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("receipt_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("activated_at", sa.Float(), nullable=False),
    )


def downgrade() -> None:
    raise RuntimeError("vNext production migrations are forward-only")
