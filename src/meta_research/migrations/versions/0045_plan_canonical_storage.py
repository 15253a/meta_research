"""Keep the canonical Plan payload in object storage and one SQL projection.

Revision ID: 0045_plan_canonical_storage
Revises: 0044_backend_storage_cleanup
Create Date: 2026-09-05
"""

from __future__ import annotations

from alembic import op


revision = "0045_plan_canonical_storage"
down_revision = "0044_backend_storage_cleanup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # These copies have no schema dependencies. Native SQLite DROP COLUMN
    # preserves the referenced table, its retained constraints, and all inbound
    # foreign keys. The migration runner wraps all three statements and the
    # Alembic revision update in one explicit transaction.
    # plan_document_json remains a hash-bound projection for actual RG SQL
    # queries; hashes, receipts, object paths, and immutable refs stay intact.
    for column in ("payload_json", "reviewed_draft_json", "review_json"):
        op.execute(f'ALTER TABLE rm_plan_documents DROP COLUMN "{column}"')


def downgrade() -> None:
    raise RuntimeError("vNext production migrations are forward-only")
