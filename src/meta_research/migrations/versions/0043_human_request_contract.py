"""Expand HumanRequest to the five-kind collaboration contract.

Revision ID: 0043_human_request_contract
Revises: 0042_remove_micro_experiment
Create Date: 2026-09-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0043_human_request_contract"
down_revision = "0042_remove_micro_experiment"
branch_labels = None
depends_on = None


_NAMING_CONVENTION = {
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
}


def _preserved_checks() -> tuple[sa.CheckConstraint, ...]:
    inspector = sa.inspect(op.get_bind())
    return tuple(
        sa.CheckConstraint(
            constraint["sqltext"],
            name=constraint.get("name"),
        )
        for constraint in inspector.get_check_constraints("owner_human_requests")
        if "kind IN" not in str(constraint["sqltext"])
    )


def upgrade() -> None:
    connection = op.get_bind()
    connection.exec_driver_sql("PRAGMA legacy_alter_table=ON")
    try:
        with op.batch_alter_table(
            "owner_human_requests",
            recreate="always",
            naming_convention=_NAMING_CONVENTION,
            table_args=(
                *_preserved_checks(),
                sa.CheckConstraint(
                    "kind IN ('library_reconnect', "
                    "'external_material_api_access', 'offline_action', "
                    "'capability_authorization', 'system_operation_help')",
                    name="ck_owner_human_requests_kind",
                ),
            ),
        ):
            pass
    finally:
        connection.exec_driver_sql("PRAGMA legacy_alter_table=OFF")


def downgrade() -> None:
    raise RuntimeError("vNext production migrations are forward-only")
