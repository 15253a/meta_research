"""Allow Stage artifacts to bind every accepted Question identity.

Revision ID: 0037_question_stage_identity
Revises: 0036_companion_view_context
Create Date: 2026-08-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0037_question_stage_identity"
down_revision = "0036_companion_view_context"
branch_labels = None
depends_on = None


_NAMING_CONVENTION = {
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
}

_QUESTION_STAGE_TABLES = (
    "rg_idea_outcome_decisions",
    "rm_plan_documents",
    "rg_formal_plan_decisions",
)


def _remove_root_only_question_foreign_keys(table_name: str) -> None:
    """Retain the immutable refs while moving validation to Owner receipts.

    Question is a polymorphic domain identity: root, ManualCreation, and
    AutonomousCreation records live in distinct RG/RM authority tables.  A
    single-table SQLite foreign key therefore rejects two valid Question
    variants.  The Stage Owners already verify the exact accepted Question
    binding and its issuer receipts before accepting any of these rows.
    """

    # SQLite/Alembic batch mode does not implicitly reproduce unnamed CHECK
    # constraints.  Reflect them into ``table_args`` so the rebuild changes
    # exactly the two invalid FKs and nothing else in the table contract.
    inspector = sa.inspect(op.get_bind())
    check_constraints = tuple(
        sa.CheckConstraint(
            constraint["sqltext"],
            name=constraint.get("name"),
        )
        for constraint in inspector.get_check_constraints(table_name)
    )
    with op.batch_alter_table(
        table_name,
        recreate="always",
        naming_convention=_NAMING_CONVENTION,
        table_args=check_constraints,
    ) as batch:
        batch.drop_constraint(
            f"fk_{table_name}_question_ref_rg_questions",
            type_="foreignkey",
        )
        batch.drop_constraint(
            f"fk_{table_name}_question_content_ref_rm_formal_question_contents",
            type_="foreignkey",
        )


def upgrade() -> None:
    connection = op.get_bind()
    # Keep inbound foreign keys attached to the stable public table names while
    # SQLite recreates these authorities.
    connection.exec_driver_sql("PRAGMA legacy_alter_table=ON")
    try:
        for table_name in _QUESTION_STAGE_TABLES:
            _remove_root_only_question_foreign_keys(table_name)
    finally:
        connection.exec_driver_sql("PRAGMA legacy_alter_table=OFF")


def downgrade() -> None:
    raise RuntimeError("vNext production migrations are forward-only")
