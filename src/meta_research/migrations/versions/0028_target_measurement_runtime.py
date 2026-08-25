"""Bind generic Target terminals to native RG measurement identities.

Revision ID: 0028_target_measurement_runtime
Revises: 0027_target_measurement_domain
Create Date: 2026-08-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0028_target_measurement_runtime"
down_revision = "0027_target_measurement_domain"
branch_labels = None
depends_on = None


def _hash(name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"length({name}) = 64")


def upgrade() -> None:
    # The Harness run row owns the one pending Target recovery generation.
    # These fields freeze the complete old handle and recovery basis while AR
    # still exposes that handle as its frontier; feed events are audit only.
    for column in (
        sa.Column("pending_recovery_ref", sa.String(128), nullable=True),
        sa.Column("pending_recovery_old_handle_json", sa.Text(), nullable=True),
        sa.Column("pending_recovery_old_handle_hash", sa.String(64), nullable=True),
        sa.Column("pending_recovery_generation", sa.Integer(), nullable=True),
        sa.Column("pending_recovery_binding_hash", sa.String(64), nullable=True),
    ):
        op.add_column("ar_harness_runs", column)

    # Stop and recovery facts point at RG's accepted exact signed terminal.
    # Existing columns remain the compact Bundle projection, while these
    # issuer refs make every later history read independently re-verifiable.
    for table in ("ar_target_stop_decisions", "ar_target_run_recoveries"):
        op.add_column(
            table,
            sa.Column("generic_binding_ref", sa.String(96), nullable=True),
        )
        op.add_column(
            table,
            sa.Column(
                "generic_binding_receipt_ref", sa.String(96), nullable=True
            ),
        )
        op.add_column(
            table,
            sa.Column(
                "generic_binding_receipt_hash", sa.String(64), nullable=True
            ),
        )

    op.add_column(
        "ar_target_run_recoveries",
        sa.Column("successor_reservation_json", sa.Text(), nullable=True),
    )
    op.add_column(
        "ar_target_run_recoveries",
        sa.Column("successor_reservation_hash", sa.String(64), nullable=True),
    )

    op.add_column(
        "research_graph_state",
        sa.Column(
            "target_measurement_attempt_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "agent_runtime_state",
        sa.Column(
            "target_native_execution_closure_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )

    # One accepted post-terminal Target measurement candidate.  VariantRun,
    # EvaluationAttempt, their two input bindings, roles, and MetricResult stay
    # in the existing native RG tables; this row only binds their exact issuer
    # chain to the Target terminal and the Plan-bound 0027 authority.
    op.create_table(
        "rg_target_measurement_attempt_bindings",
        sa.Column("attempt_binding_ref", sa.String(96), primary_key=True),
        sa.Column("target_ref", sa.String(96), nullable=False),
        sa.Column("target_run_ref", sa.String(96), nullable=False),
        sa.Column("target_attempt_ref", sa.String(96), nullable=False),
        sa.Column("target_fence_ref", sa.String(96), nullable=False),
        sa.Column("authority_ref", sa.String(96), nullable=False),
        sa.Column("authority_hash", sa.String(64), nullable=False),
        sa.Column("generic_binding_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("manifest_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("variant_run_ref", sa.String(96), nullable=False),
        sa.Column("variant_run_disposition", sa.String(16), nullable=False),
        sa.Column("evaluation_attempt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("variant_input_binding_ref", sa.String(96), nullable=False),
        sa.Column("evaluation_input_binding_ref", sa.String(96), nullable=False),
        sa.Column("checkpoint_role_refs_json", sa.Text(), nullable=False),
        sa.Column("checkpoint_role_refs_hash", sa.String(64), nullable=False),
        sa.Column("result_role_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["target_ref"], ["rg_targets.target_ref"]),
        sa.ForeignKeyConstraint(
            ["authority_ref"],
            ["rg_target_measurement_domain_authorities.authority_ref"],
        ),
        sa.ForeignKeyConstraint(
            ["generic_binding_ref"],
            ["rg_target_generic_execution_bindings_v3.binding_ref"],
        ),
        sa.ForeignKeyConstraint(
            ["manifest_ref"],
            ["rm_target_generic_result_manifests.manifest_ref"],
        ),
        sa.ForeignKeyConstraint(
            ["variant_run_ref"], ["rg_variant_runs.variant_run_ref"]
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_attempt_ref"],
            ["rg_evaluation_attempts.evaluation_attempt_ref"],
        ),
        sa.ForeignKeyConstraint(
            ["variant_input_binding_ref"],
            ["rg_experiment_input_bindings.binding_ref"],
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_input_binding_ref"],
            ["rg_experiment_input_bindings.binding_ref"],
        ),
        sa.ForeignKeyConstraint(
            ["result_role_ref"], ["rg_experiment_asset_roles.role_ref"]
        ),
        sa.CheckConstraint(
            "variant_run_disposition IN ('created', 'reused')"
        ),
        sa.UniqueConstraint("target_run_ref", "target_attempt_ref"),
        *(
            _hash(name)
            for name in (
                "authority_hash",
                "checkpoint_role_refs_hash",
                "payload_hash",
                "request_hash",
                "receipt_hash",
            )
        ),
    )

    # AR closes an exact current Target attempt only after a fresh result
    # review over the native RG EvaluationAttempt/MetricResult and RM manifest.
    # There is intentionally no ExperimentRun, provider, or shadow measurement
    # identity in this table.
    op.create_table(
        "ar_target_native_execution_closures",
        sa.Column("closure_ref", sa.String(96), primary_key=True),
        sa.Column("target_ref", sa.String(96), nullable=False),
        sa.Column("target_run_ref", sa.String(96), nullable=False),
        sa.Column("target_attempt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("target_fence_ref", sa.String(96), nullable=False),
        sa.Column("generic_binding_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("manifest_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("attempt_binding_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("evaluation_attempt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("metric_result_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("result_review_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["target_ref"], ["ar_target_launches.target_ref"]),
        sa.ForeignKeyConstraint(
            ["generic_binding_ref"],
            ["rg_target_generic_execution_bindings_v3.binding_ref"],
        ),
        sa.ForeignKeyConstraint(
            ["manifest_ref"],
            ["rm_target_generic_result_manifests.manifest_ref"],
        ),
        sa.ForeignKeyConstraint(
            ["attempt_binding_ref"],
            ["rg_target_measurement_attempt_bindings.attempt_binding_ref"],
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_attempt_ref"],
            ["rg_evaluation_attempts.evaluation_attempt_ref"],
        ),
        sa.ForeignKeyConstraint(
            ["metric_result_ref"], ["rg_metric_results.metric_result_ref"]
        ),
        sa.ForeignKeyConstraint(
            ["result_review_ref"], ["ar_target_review_evidence.review_ref"]
        ),
        *(
            _hash(name)
            for name in ("payload_hash", "request_hash", "receipt_hash")
        ),
    )


def downgrade() -> None:
    raise RuntimeError("vNext production migrations are forward-only")
