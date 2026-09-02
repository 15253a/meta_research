"""Close the operation-scoped HumanRequest lifecycle.

Revision ID: 0041_human_request_lifecycle
Revises: 0040_root_completion_seam
Create Date: 2026-09-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0041_human_request_lifecycle"
down_revision = "0040_root_completion_seam"
branch_labels = None
depends_on = None


_NAMING_CONVENTION = {
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
}


def _preserved_checks(table_name: str, *, replaced_fragment: str):
    inspector = sa.inspect(op.get_bind())
    return tuple(
        sa.CheckConstraint(
            constraint["sqltext"],
            name=constraint.get("name"),
        )
        for constraint in inspector.get_check_constraints(table_name)
        if replaced_fragment not in str(constraint["sqltext"])
    )


def upgrade() -> None:
    connection = op.get_bind()
    # SQLite batch recreation must retain inbound foreign keys on the stable
    # public table names while the three old unnamed CHECKs are replaced.
    connection.exec_driver_sql("PRAGMA legacy_alter_table=ON")
    try:
        with op.batch_alter_table(
            "owner_human_requests",
            recreate="always",
            naming_convention=_NAMING_CONVENTION,
            table_args=(
                *_preserved_checks(
                    "owner_human_requests", replaced_fragment="status IN"
                ),
                sa.CheckConstraint(
                    "status IN ('open', 'satisfied', 'unsatisfied', 'declined', "
                    "'withdrawn', 'expired', 'superseded')",
                    name="ck_owner_human_requests_status",
                ),
            ),
        ) as batch:
            batch.add_column(
                sa.Column(
                    "predecessor_request_ref",
                    sa.String(length=96),
                    nullable=True,
                )
            )
            batch.create_foreign_key(
                "fk_owner_human_requests_predecessor",
                "owner_human_requests",
                ["predecessor_request_ref"],
                ["request_ref"],
            )

        # Existing rN revisions already have an unambiguous predecessor.  This
        # backfill exposes their lineage without changing their terminal facts.
        op.execute(
            "UPDATE owner_human_requests SET predecessor_request_ref = ("
            "SELECT predecessor.request_ref FROM owner_human_requests AS predecessor "
            "WHERE predecessor.issuer = owner_human_requests.issuer AND "
            "predecessor.request_id = owner_human_requests.request_id AND "
            "predecessor.revision = owner_human_requests.revision - 1) "
            "WHERE revision > 1"
        )
        op.create_index(
            "uq_owner_human_requests_predecessor",
            "owner_human_requests",
            ["predecessor_request_ref"],
            unique=True,
            sqlite_where=sa.text("predecessor_request_ref IS NOT NULL"),
        )

        with op.batch_alter_table(
            "owner_human_request_evaluations",
            recreate="always",
            naming_convention=_NAMING_CONVENTION,
            table_args=(
                *_preserved_checks(
                    "owner_human_request_evaluations",
                    replaced_fragment="decision IN",
                ),
                sa.CheckConstraint(
                    "decision IN ('satisfied', 'unsatisfied', 'needs_input', "
                    "'declined', 'stale')",
                    name="ck_owner_human_request_evaluations_decision",
                ),
            ),
        ):
            pass

        with op.batch_alter_table(
            "owner_human_request_dispositions",
            recreate="always",
            naming_convention=_NAMING_CONVENTION,
            table_args=(
                *_preserved_checks(
                    "owner_human_request_dispositions",
                    replaced_fragment="decision IN",
                ),
                sa.CheckConstraint(
                    "decision IN ('satisfied', 'unsatisfied', 'declined', "
                    "'withdrawn', 'expired', 'superseded')",
                    name="ck_owner_human_request_dispositions_decision",
                ),
            ),
        ):
            pass

        with op.batch_alter_table(
            "ar_harness_runs",
            recreate="always",
            naming_convention=_NAMING_CONVENTION,
            table_args=(
                *_preserved_checks(
                    "ar_harness_runs", replaced_fragment="status IN"
                ),
                sa.CheckConstraint(
                    "status IN ('admitting', 'admitted', 'running', "
                    "'suspended', 'executed', 'failed', 'revoked')",
                    name="ck_ar_harness_runs_status",
                ),
            ),
        ):
            pass

        # Acquisition and Companion have real provider tasks but no dedicated
        # AR run table.  These nullable columns let their narrow registration
        # seam use the existing runtime control row without rewriting legacy
        # controls owned by the built-in Root runtimes.
        op.add_column(
            "ar_run_controls",
            sa.Column("runtime_binding_hash", sa.String(length=64), nullable=True),
        )
        op.add_column(
            "ar_run_controls",
            sa.Column("attempt_generation", sa.Integer(), nullable=True),
        )
    finally:
        connection.exec_driver_sql("PRAGMA legacy_alter_table=OFF")

    op.create_table(
        "owner_human_request_open_effects",
        sa.Column("issuer", sa.String(length=32), nullable=False),
        sa.Column("effect_key", sa.String(length=128), nullable=False),
        sa.Column("effect_id", sa.String(length=128), nullable=False),
        sa.Column("request_ref", sa.String(length=96), nullable=False, unique=True),
        sa.Column("waiter_ref", sa.String(length=128), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("operation_binding_json", sa.Text(), nullable=False),
        sa.Column("operation_binding_hash", sa.String(length=64), nullable=False),
        sa.Column("yield_fact_json", sa.Text(), nullable=False),
        sa.Column("yield_fact_hash", sa.String(length=64), nullable=False),
        sa.Column("receipt_ref", sa.String(length=96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("issuer", "effect_key"),
        sa.ForeignKeyConstraint(
            ["request_ref"], ["owner_human_requests.request_ref"]
        ),
        sa.ForeignKeyConstraint(
            ["request_ref", "waiter_ref"],
            [
                "owner_human_request_waiters.request_ref",
                "owner_human_request_waiters.waiter_ref",
            ],
        ),
        sa.CheckConstraint("generation >= 1"),
        sa.CheckConstraint("length(operation_binding_hash) = 64"),
        sa.CheckConstraint("length(yield_fact_hash) = 64"),
        sa.CheckConstraint("length(receipt_hash) = 64"),
    )
    op.create_table(
        "hc_human_request_response_rejections",
        sa.Column("rejection_ref", sa.String(length=64), primary_key=True),
        sa.Column("request_ref", sa.String(length=96), nullable=False),
        sa.Column("issuer", sa.String(length=32), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("request_revision", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("request_identity_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("receipt_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["request_ref"], ["owner_human_requests.request_ref"]
        ),
        sa.CheckConstraint("request_revision >= 1"),
        sa.CheckConstraint(
            "reason_code = 'human_response_secret_forbidden'"
        ),
        sa.CheckConstraint("length(request_identity_hash) = 64"),
        sa.CheckConstraint("length(idempotency_hash) = 64"),
        sa.CheckConstraint("length(receipt_hash) = 64"),
    )
    op.create_index(
        "ix_hc_human_request_response_rejections_request",
        "hc_human_request_response_rejections",
        ["request_ref", "created_at"],
    )


def downgrade() -> None:
    raise RuntimeError("vNext production migrations are forward-only")
