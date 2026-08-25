"""Add issuer-owned Bundle reuse content and eligibility proofs.

Revision ID: 0020_bundle_reuse_proofs
Revises: 0019_target_run_inbox
Create Date: 2026-08-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0020_bundle_reuse_proofs"
down_revision = "0019_target_run_inbox"
branch_labels = None
depends_on = None


def _hash(name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"length({name}) = 64")


def _counter(name: str) -> sa.Column:
    return sa.Column(name, sa.Integer(), nullable=False, server_default="0")


def upgrade() -> None:
    op.add_column(
        "research_memory_state", _counter("implementation_revision_count")
    )
    op.add_column("research_graph_state", _counter("reuse_eligibility_count"))

    # One row is both the exact source/version verification fact and the
    # immutable Implementation Revision content admitted by RM.  The two
    # receipts remain distinct because their required subjects are distinct.
    op.create_table(
        "rm_implementation_revision_contents",
        sa.Column("implementation_revision_ref", sa.String(256), primary_key=True),
        sa.Column("source_ref", sa.String(256), nullable=False),
        sa.Column("exact_version_ref", sa.String(256), nullable=False, unique=True),
        sa.Column("license_ref", sa.String(256), nullable=True),
        sa.Column("source_content_hash_ref", sa.String(64), nullable=True),
        sa.Column("patch_ref", sa.String(256), nullable=True),
        sa.Column("verification_evidence_ref", sa.String(256), nullable=False),
        sa.Column("content_json", sa.Text(), nullable=False),
        sa.Column("content_hash_ref", sa.String(64), nullable=False, unique=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("source_receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("source_receipt_hash", sa.String(64), nullable=False),
        sa.Column("content_receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("content_receipt_hash", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("source_ref", "exact_version_ref"),
        *(
            _hash(name)
            for name in (
                "content_hash_ref",
                "request_hash",
                "source_receipt_hash",
                "content_receipt_hash",
            )
        ),
    )

    # Eligibility is an RG fact, separate from source/version verification.
    # Its canonical payload is anchored to an already accepted TargetCommit;
    # the receipt subject is the payload hash, never the stable eligibility ref.
    op.create_table(
        "rg_reuse_eligibilities",
        sa.Column("eligibility_ref", sa.String(96), primary_key=True),
        sa.Column("tier", sa.String(32), nullable=False),
        sa.Column("target_commit_ref", sa.String(96), nullable=False),
        sa.Column("source_ref", sa.String(256), nullable=False),
        sa.Column("exact_version_ref", sa.String(256), nullable=False),
        sa.Column("implementation_revision_ref", sa.String(256), nullable=False),
        sa.Column("implementation_content_hash_ref", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["target_commit_ref"], ["rg_target_commits.commit_ref"]
        ),
        sa.UniqueConstraint(
            "tier",
            "target_commit_ref",
            "source_ref",
            "exact_version_ref",
            "implementation_revision_ref",
        ),
        sa.CheckConstraint(
            "tier IN ('accepted-local', 'related-history', "
            "'global-baseline-pool')"
        ),
        *(
            _hash(name)
            for name in (
                "implementation_content_hash_ref",
                "payload_hash",
                "request_hash",
                "receipt_hash",
            )
        ),
    )


def downgrade() -> None:
    raise RuntimeError("vNext production migrations are forward-only")
