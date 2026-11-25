"""Scope Datasets by origin_quest_ref and support typed multi-stage Question relations."""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0092_dataset_origin_quest_and_question_relations"
down_revision = "0091_part4_goal_refs_and_hot_indexes"
branch_labels = None
depends_on = None


def _drop_dataset_semantic_key_uniqueness(table: sa.Table) -> None:
    removed = False
    for constraint in tuple(table.constraints):
        if (
            isinstance(constraint, sa.UniqueConstraint)
            and tuple(constraint.columns.keys()) == ("semantic_key",)
        ):
            table.constraints.remove(constraint)
            removed = True
    for index in tuple(table.indexes):
        if index.unique and tuple(index.columns.keys()) == ("semantic_key",):
            table.indexes.remove(index)
            removed = True
    if not removed:
        raise RuntimeError("dataset_semantic_key_constraint_unexpected")


def _drop_relation_pair_check(table: sa.Table) -> None:
    checks = [
        constraint
        for constraint in table.constraints
        if isinstance(constraint, sa.CheckConstraint)
        and "left_question_ref < right_question_ref" in str(constraint.sqltext)
    ]
    if len(checks) != 1:
        raise RuntimeError("question_relation_pair_constraint_unexpected")
    table.constraints.remove(checks[0])


def upgrade() -> None:
    connection = op.get_bind()
    metadata = sa.MetaData()
    datasets_table = sa.Table("rg_datasets", metadata, autoload_with=connection)
    versions_table = sa.Table("rg_dataset_versions", metadata, autoload_with=connection)
    relations_table = sa.Table("rg_question_relations", metadata, autoload_with=connection)

    _drop_dataset_semantic_key_uniqueness(datasets_table)
    _drop_relation_pair_check(relations_table)
    relation_index_exists = any(
        index.name == "ix_rg_question_relations_quest"
        for index in relations_table.indexes
    )

    connection.exec_driver_sql("PRAGMA legacy_alter_table=ON")
    try:
        with op.batch_alter_table(
            "rg_datasets", copy_from=datasets_table, recreate="always"
        ) as batch:
            batch.add_column(sa.Column("origin_quest_ref", sa.Text(), nullable=True))
            batch.create_foreign_key(
                "fk_rg_datasets_origin_quest_ref",
                "rg_quests",
                ["origin_quest_ref"],
                ["quest_ref"],
            )
            batch.create_index(
                "uq_rg_datasets_payload_hash", ["payload_hash"], unique=True
            )
            batch.create_index("ix_rg_datasets_semantic_key", ["semantic_key"])
            batch.create_index("ix_rg_datasets_origin_quest_ref", ["origin_quest_ref"])

        with op.batch_alter_table(
            "rg_dataset_versions", copy_from=versions_table, recreate="always"
        ) as batch:
            batch.add_column(sa.Column("origin_quest_ref", sa.Text(), nullable=True))
            batch.create_foreign_key(
                "fk_rg_dataset_versions_origin_quest_ref",
                "rg_quests",
                ["origin_quest_ref"],
                ["quest_ref"],
            )
            batch.create_index(
                "ix_rg_dataset_versions_origin_quest_ref", ["origin_quest_ref"]
            )

        with op.batch_alter_table(
            "rg_question_relations", copy_from=relations_table, recreate="always"
        ) as batch:
            batch.add_column(
                sa.Column(
                    "relation_kind",
                    sa.String(32),
                    nullable=False,
                    server_default="related",
                )
            )
            batch.create_check_constraint(
                "ck_rg_question_relations_kind",
                "relation_kind IN ('related', 'overlaps', 'competes', "
                "'depends_on', 'subquestion_of', 'refines', 'supersedes')",
            )
            batch.create_check_constraint(
                "ck_rg_question_relations_pair",
                "left_question_ref <> right_question_ref AND ("
                "relation_kind NOT IN ('related', 'overlaps', 'competes') "
                "OR left_question_ref < right_question_ref)",
            )
            if not relation_index_exists:
                batch.create_index(
                    "ix_rg_question_relations_quest",
                    ["quest_ref", "created_at", "relation_ref"],
                )
    finally:
        connection.exec_driver_sql("PRAGMA legacy_alter_table=OFF")


def downgrade() -> None:
    raise RuntimeError("production migrations are forward-only")
