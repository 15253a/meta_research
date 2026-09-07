"""Remove unused storage and index accepted Reasoning by its source scope.

Revision ID: 0044_backend_storage_cleanup
Revises: 0043_human_request_contract
Create Date: 2026-09-05
"""

from __future__ import annotations

from alembic import op


revision = "0044_backend_storage_cleanup"
down_revision = "0043_human_request_contract"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Each key is already covered by an identical UNIQUE constraint index.
    for index, table in (
        ("ix_rm_question_literature_revisions_current", "rm_question_literature_revisions"),
        ("ix_rg_target_graph_appends_graph_generation", "rg_target_graph_appends"),
        ("ix_rg_target_measurement_domain_authorities_graph", "rg_target_measurement_domain_authorities"),
    ):
        op.drop_index(index, table_name=table)

    # The v3 table owns current execution bindings; nothing references this
    # retired table in the preceding head schema or in production readers.
    op.drop_table("rg_target_generic_execution_bindings")

    # Both accepted transition kinds carry the Owner-validated source scope.
    # Index those immutable fields without introducing another stored copy.
    op.execute(
        "CREATE INDEX ix_rg_reasoning_outcomes_source_scope ON "
        "rg_reasoning_outcome_decisions ("
        "json_extract(transition_json, '$.source_quest_ref'), "
        "json_extract(transition_json, '$.source_question_ref'), "
        "decided_at, outcome_ref) WHERE decision = 'accepted'"
    )

    # object_count means unique registered content objects, not every file
    # below the data root. Literature snapshots have their own counter.
    op.execute(
        "UPDATE research_memory_state SET object_count = "
        "(SELECT COUNT(*) FROM rm_managed_objects), revision = revision + 1 "
        "WHERE object_count != (SELECT COUNT(*) FROM rm_managed_objects)"
    )


def downgrade() -> None:
    raise RuntimeError("vNext production migrations are forward-only")
