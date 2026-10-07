"""Accept Target results without a mandatory overall classification."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0061_target_result_optional_disposition"
down_revision = "0060_asset_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    table = sa.Table("rg_target_commits", sa.MetaData(), autoload_with=connection)
    for constraint in tuple(table.constraints):
        if (isinstance(constraint, sa.CheckConstraint)
                and "result_disposition IN" in str(constraint.sqltext)):
            table.constraints.remove(constraint)
    # Rebuild only the constraint/nullable definition. Existing result bytes,
    # closures, receipts and all source/version identities are copied exactly.
    connection.exec_driver_sql("PRAGMA legacy_alter_table=ON")
    try:
        with op.batch_alter_table(
            "rg_target_commits", copy_from=table, recreate="always"
        ) as batch:
            batch.alter_column(
                "result_disposition", existing_type=sa.String(24),
                type_=sa.String(256), nullable=True,
            )
    finally:
        connection.exec_driver_sql("PRAGMA legacy_alter_table=OFF")


def downgrade() -> None:
    raise RuntimeError("production migrations are forward-only")
