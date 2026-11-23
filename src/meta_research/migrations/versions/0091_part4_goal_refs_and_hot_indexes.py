from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from meta_research.owners.common import canonical_hash


revision = "0091_part4_goal_refs_and_hot_indexes"
down_revision = "0090_reasoning_content_retry"
branch_labels = None
depends_on = None


def _table_columns(connection, table_name: str) -> set[str]:
    return {
        str(row["name"])
        for row in connection.exec_driver_sql(
            f"PRAGMA table_info({table_name})"
        ).mappings()
    }


def _existing_indexes(connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    }


def _add_column_if_missing(
    connection, table_name: str, column: sa.Column
) -> None:
    if column.name not in _table_columns(connection, table_name):
        op.add_column(table_name, column)


def _create_index_if_missing(
    existing_indexes: set[str],
    index_name: str,
    table_name: str,
    columns: list[str],
    *,
    unique: bool = False,
) -> None:
    if index_name not in existing_indexes:
        op.create_index(index_name, table_name, columns, unique=unique)
        existing_indexes.add(index_name)


def upgrade() -> None:
    connection = op.get_bind()

    _add_column_if_missing(
        connection,
        "rg_quests",
        sa.Column("initial_goal_revision_ref", sa.String(96), nullable=True),
    )
    rows = connection.execute(
        sa.text(
            "SELECT quest_ref, draft_revision, draft_hash FROM rg_quests "
            "WHERE initial_goal_revision_ref IS NULL"
        )
    ).mappings()
    for row in rows:
        revision_ref = "quest_goal_revision_" + canonical_hash(
            {
                "quest_ref": row["quest_ref"],
                "draft_revision": int(row["draft_revision"]),
                "draft_hash": row["draft_hash"],
            }
        )[:32]
        connection.execute(
            sa.text(
                "UPDATE rg_quests SET initial_goal_revision_ref = :revision_ref "
                "WHERE quest_ref = :quest_ref"
            ),
            {"quest_ref": row["quest_ref"], "revision_ref": revision_ref},
        )

    _add_column_if_missing(
        connection,
        "ar_target_launches",
        sa.Column("goal_revision_ref", sa.String(96), nullable=True),
    )
    _add_column_if_missing(
        connection,
        "rg_target_graph_appends",
        sa.Column("goal_revision_ref", sa.String(96), nullable=True),
    )

    existing_indexes = _existing_indexes(connection)
    _create_index_if_missing(
        existing_indexes,
        "uq_rg_quests_initial_goal_revision_ref",
        "rg_quests",
        ["initial_goal_revision_ref"],
        unique=True,
    )
    _create_index_if_missing(
        existing_indexes,
        "ix_ar_target_launches_goal_revision_ref",
        "ar_target_launches",
        ["goal_revision_ref"],
    )
    _create_index_if_missing(
        existing_indexes,
        "ix_ae_foreground_heads_cycle_status",
        "ae_foreground_heads",
        ["cycle_ref", "status"],
    )
    _create_index_if_missing(
        existing_indexes,
        "ix_rg_question_lifecycle_quest_status",
        "rg_question_lifecycle",
        ["quest_ref", "status"],
    )
    _create_index_if_missing(
        existing_indexes,
        "ix_rg_target_graphs_quest_ref",
        "rg_target_graphs",
        ["quest_ref"],
    )
    _create_index_if_missing(
        existing_indexes,
        "ix_ar_target_run_workspaces_target_status",
        "ar_target_run_workspaces",
        ["target_ref", "status", "ordinal"],
    )
    _create_index_if_missing(
        existing_indexes,
        "ix_hc_guidance_deliveries_constraint",
        "hc_guidance_deliveries",
        ["constraint_ref", "created_at", "delivery_ref"],
    )
    _create_index_if_missing(
        existing_indexes,
        "ix_hc_guidance_effects_delivery_kind",
        "hc_guidance_effects",
        ["delivery_ref", "kind"],
    )
    _create_index_if_missing(
        existing_indexes,
        "ix_hc_guidance_read_pages_delivery",
        "hc_guidance_read_pages",
        ["delivery_ref", "start_offset"],
    )
    _create_index_if_missing(
        existing_indexes,
        "ix_ar_target_frontier_entries_state",
        "ar_target_frontier_entries",
        ["state", "target_ref"],
    )
    _create_index_if_missing(
        existing_indexes,
        "ix_rg_goal_work_intents_kind_created",
        "rg_goal_work_intents",
        ["decision_kind", "created_at", "intent_ref"],
    )


def downgrade() -> None:
    raise RuntimeError("production migrations are forward-only")
