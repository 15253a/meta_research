"""Add append-only rolling Bundle Target heads.

Revision ID: 0017_bundle_target_rolling
Revises: 0016_semantic_mcp_harness
Create Date: 2026-08-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0017_bundle_target_rolling"
down_revision = "0016_semantic_mcp_harness"
branch_labels = None
depends_on = None


def _hash(name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"length({name}) = 64")


def upgrade() -> None:
    # AR records what the current Bundle Session proposed.  This is deliberately
    # separate from RG acceptance: a proposal is not a Target or a graph head.
    op.create_table(
        "ar_bundle_target_proposals",
        sa.Column("proposal_ref", sa.String(96), primary_key=True),
        sa.Column("run_ref", sa.String(64), nullable=False),
        sa.Column("attempt_ref", sa.String(64), nullable=False),
        sa.Column("fence_ref", sa.String(64), nullable=False),
        sa.Column("native_session_ref", sa.String(128), nullable=False),
        sa.Column("graph_ref", sa.String(96), nullable=False),
        sa.Column("proposal_sequence", sa.Integer(), nullable=False),
        sa.Column("base_generation", sa.Integer(), nullable=False),
        sa.Column("base_head_receipt_ref", sa.String(96), nullable=False),
        sa.Column("base_head_receipt_hash", sa.String(64), nullable=False),
        sa.Column("proposal_json", sa.Text(), nullable=False),
        sa.Column("proposal_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["run_ref"], ["ar_stage_runs.run_ref"]),
        sa.ForeignKeyConstraint(["attempt_ref"], ["ar_stage_attempts.attempt_ref"]),
        sa.ForeignKeyConstraint(["fence_ref"], ["ar_execution_fences.fence_ref"]),
        sa.ForeignKeyConstraint(["graph_ref"], ["rg_target_graphs.graph_ref"]),
        sa.UniqueConstraint("run_ref", "proposal_sequence"),
        sa.CheckConstraint("proposal_sequence >= 1"),
        sa.CheckConstraint("base_generation >= 0"),
        _hash("base_head_receipt_hash"),
        _hash("proposal_hash"),
        _hash("request_hash"),
        _hash("receipt_hash"),
    )
    op.create_index(
        "ix_ar_bundle_target_proposals_graph_sequence",
        "ar_bundle_target_proposals",
        ["graph_ref", "proposal_sequence"],
    )

    # Each RG row is an immutable compare-and-swap successor to the previous
    # head.  The latest generation is derived; there is no mutable head table.
    op.create_table(
        "rg_target_graph_appends",
        sa.Column("append_ref", sa.String(96), primary_key=True),
        sa.Column("graph_ref", sa.String(96), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("predecessor_head_receipt_ref", sa.String(96), nullable=False),
        sa.Column("predecessor_head_receipt_hash", sa.String(64), nullable=False),
        sa.Column("proposal_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("proposal_hash", sa.String(64), nullable=False),
        sa.Column("proposal_receipt_ref", sa.String(96), nullable=False),
        sa.Column("proposal_receipt_hash", sa.String(64), nullable=False),
        sa.Column("target_refs_json", sa.Text(), nullable=False),
        sa.Column("target_set_hash", sa.String(64), nullable=False),
        sa.Column("coverage_hash", sa.String(64), nullable=False),
        sa.Column("strategy_complete", sa.Boolean(), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["graph_ref"], ["rg_target_graphs.graph_ref"]),
        sa.ForeignKeyConstraint(
            ["proposal_ref"], ["ar_bundle_target_proposals.proposal_ref"]
        ),
        sa.UniqueConstraint("graph_ref", "generation"),
        sa.UniqueConstraint("graph_ref", "predecessor_head_receipt_ref"),
        sa.CheckConstraint("generation >= 1"),
        _hash("predecessor_head_receipt_hash"),
        _hash("proposal_hash"),
        _hash("proposal_receipt_hash"),
        _hash("target_set_hash"),
        _hash("coverage_hash"),
        _hash("receipt_hash"),
    )
    op.create_index(
        "ix_rg_target_graph_appends_graph_generation",
        "rg_target_graph_appends",
        ["graph_ref", "generation"],
    )

    # NULL identifies generation-0 Targets and preserves their receipt bytes.
    # SQLite cannot safely add a new FK to this existing table without a full
    # rebuild.  RG's receipt verifier therefore enforces the relationship in
    # both directions (graph, append_ref, target_refs, and contiguous ordinal).
    op.add_column(
        "rg_targets",
        sa.Column("append_ref", sa.String(96), nullable=True),
    )
    op.create_index("ix_rg_targets_append_ref", "rg_targets", ["append_ref"])


def downgrade() -> None:
    raise RuntimeError("vNext production migrations are forward-only")
