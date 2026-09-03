"""Remove the retired Micro execution schema.

Revision ID: 0042_remove_micro_experiment
Revises: 0041_human_request_lifecycle
Create Date: 2026-09-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0042_remove_micro_experiment"
down_revision = "0041_human_request_lifecycle"
branch_labels = None
depends_on = None


_MICRO_BACKED_TABLES = (
    "ar_target_execution_closures",
    "rg_target_protocol_aggregations",
    "rm_target_result_manifests",
    "rg_target_protected_execution_bindings",
    "ar_target_run_admissions",
    "rg_target_run_bindings",
)

_MICRO_ONLY_TABLES = (
    "ar_experiment_events",
    "ar_experiment_attempts",
    "ar_experiment_sessions",
    "ar_experiment_runs",
    "rg_experiment_idempotency",
    "rg_experiment_requests",
)

_MICRO_ONLY_STATE_COLUMNS = {
    "agent_runtime_state": (
        "experiment_run_count",
        "experiment_completed_run_count",
        "experiment_attempt_count",
        "experiment_session_count",
        "active_experiment_run_count",
        "target_execution_closure_count",
    ),
    "research_graph_state": (
        "target_protected_execution_count",
        "target_protocol_aggregation_count",
    ),
    "research_memory_state": ("target_result_manifest_count",),
}


def _provider_unit_checks_without_micro() -> tuple[sa.CheckConstraint, ...]:
    inspector = sa.inspect(op.get_bind())
    preserved = tuple(
        sa.CheckConstraint(
            constraint["sqltext"],
            name=constraint.get("name"),
        )
        for constraint in inspector.get_check_constraints("ar_provider_units")
        if "unit_kind IN" not in str(constraint["sqltext"])
    )
    return (
        *preserved,
        sa.CheckConstraint(
            "unit_kind IN ('idea_primary', 'idea_review', 'plan_primary', "
            "'plan_review', 'bundle_primary', 'bundle_review', 'deepfetch', "
            "'writing_primary', 'writing_review', 'reasoning_primary', "
            "'reasoning_review')",
            name="ck_ar_provider_units_unit_kind",
        ),
    )


def upgrade() -> None:
    for table in _MICRO_BACKED_TABLES:
        op.drop_table(table)
    for table in _MICRO_ONLY_TABLES:
        op.drop_table(table)

    with op.batch_alter_table(
        "ar_provider_units",
        recreate="always",
        table_args=_provider_unit_checks_without_micro(),
    ):
        pass

    for table, columns in _MICRO_ONLY_STATE_COLUMNS.items():
        for column in columns:
            op.drop_column(table, column)


def downgrade() -> None:
    raise RuntimeError("vNext production migrations are forward-only")
