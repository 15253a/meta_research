"""Add issuer facts after generic Target execution.

Revision ID: 0025_target_generic_measurement
Revises: 0024_bundle_target_graph_rejection
Create Date: 2026-08-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0025_target_generic_measurement"
down_revision = "0024_bundle_target_graph_rejection"
branch_labels = None
depends_on = None


def _hash(name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"length({name}) = 64")


def _counter(name: str) -> sa.Column:
    return sa.Column(name, sa.Integer(), nullable=False, server_default="0")


def upgrade() -> None:
    # 0013 coupled the TargetCommit projection to the example-era Experiment
    # Run/EvaluationAttempt tables.  Formal-v3 now accepts either the legacy
    # diagnostic closure or the independent generic Target measurement chain,
    # so these two foreign keys must not force an identity alias.  The Target
    # FK and every legacy row remain intact; application verification chooses
    # and rechecks the exact issuer chain from closure schema v3.
    legacy_fk_names = {
        "fk_rg_target_commits_target_run_ref_ar_experiment_runs",
        "fk_rg_target_commits_evaluation_attempt_ref_rg_evaluation_attempts",
    }
    with op.batch_alter_table(
        "rg_target_commits",
        recreate="always",
        naming_convention={
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"
        },
    ) as batch:
        for constraint_name in legacy_fk_names:
            batch.drop_constraint(constraint_name, type_="foreignkey")

    op.add_column(
        "research_memory_state",
        _counter("target_implementation_bundle_count"),
    )
    op.add_column(
        "research_memory_state",
        _counter("target_implementation_bundle_usage_count"),
    )
    op.add_column(
        "research_memory_state",
        _counter("target_generic_result_manifest_count"),
    )
    op.add_column(
        "research_graph_state",
        _counter("target_generic_measurement_count"),
    )
    op.add_column(
        "agent_runtime_state",
        _counter("target_generic_execution_closure_count"),
    )
    op.add_column(
        "agent_runtime_state",
        _counter("target_run_workspace_count"),
    )
    # 0022 review rows remain readable.  New code-review acceptances require
    # this fixed-contract review+scope subject while ``payload_hash`` freezes
    # the larger Owner payload including candidate-ready/self-check evidence.
    op.add_column(
        "ar_target_review_evidence",
        sa.Column("evidence_content_hash", sa.String(64), nullable=True),
    )

    # The native Target root never receives an arbitrary host path.  AR owns
    # one opaque private workspace lease per Session/Attempt/Fence and the
    # Harness resolver maps only that lease to the deployment root.
    op.create_table(
        "ar_target_run_workspaces",
        sa.Column("workspace_ref", sa.String(96), primary_key=True),
        sa.Column("target_ref", sa.String(96), nullable=False),
        sa.Column("target_run_ref", sa.String(96), nullable=False),
        sa.Column("root_session_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("target_attempt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("target_fence_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("root_name", sa.String(64), nullable=False, unique=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["target_ref"], ["ar_target_launches.target_ref"]),
        sa.UniqueConstraint("target_run_ref", "ordinal"),
        sa.CheckConstraint("ordinal >= 1"),
        sa.CheckConstraint("status IN ('active', 'retired')"),
        *(
            _hash(name)
            for name in (
                "root_name",
                "payload_hash",
                "request_hash",
                "receipt_hash",
            )
        ),
    )

    # The implementation revision and its actual immutable code bundle are
    # global RM facts.  A revision may be reused by any number of Targets; the
    # Target-specific provenance/current-scope fact lives in the append-only
    # usage table below and is never relabelled as the bundle receipt.
    op.create_table(
        "rm_target_implementation_bundles",
        sa.Column("implementation_revision_ref", sa.String(256), primary_key=True),
        sa.Column("bundle_content_hash", sa.String(64), nullable=False),
        sa.Column("asset_ref", sa.String(96), nullable=False),
        sa.Column("version_ref", sa.String(96), nullable=False),
        sa.Column("artifact_manifest_hash", sa.String(64), nullable=False),
        sa.Column("asset_receipt_ref", sa.String(96), nullable=False),
        sa.Column("asset_receipt_hash", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["version_ref"], ["rm_asset_versions.version_ref"]),
        *(
            _hash(name)
            for name in (
                "bundle_content_hash",
                "artifact_manifest_hash",
                "asset_receipt_hash",
                "payload_hash",
                "request_hash",
                "receipt_hash",
            )
        ),
    )

    op.create_table(
        "rm_target_implementation_bundle_usages",
        sa.Column("usage_ref", sa.String(96), primary_key=True),
        sa.Column("target_ref", sa.String(96), nullable=False),
        sa.Column("implementation_revision_ref", sa.String(256), nullable=False),
        sa.Column("origin_kind", sa.String(24), nullable=False),
        sa.Column("revision_authority_receipt_ref", sa.String(96), nullable=False),
        sa.Column("revision_authority_receipt_hash", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["target_ref"], ["rg_targets.target_ref"]),
        sa.ForeignKeyConstraint(
            ["implementation_revision_ref"],
            ["rm_target_implementation_bundles.implementation_revision_ref"],
        ),
        sa.UniqueConstraint("target_ref", "implementation_revision_ref"),
        sa.CheckConstraint(
            "origin_kind IN ('reused', 'greenfield', 'recovery')"
        ),
        *(
            _hash(name)
            for name in (
                "revision_authority_receipt_hash",
                "payload_hash",
                "request_hash",
                "receipt_hash",
            )
        ),
    )

    # Formal-v3 eligibility references the actual bundle receipt.  The 0022
    # table retains its FK to metadata-backed legacy artifacts and is never
    # repurposed or relabelled.
    op.create_table(
        "ar_target_execution_eligibilities_v3",
        sa.Column("eligibility_ref", sa.String(96), primary_key=True),
        sa.Column("target_ref", sa.String(96), nullable=False),
        sa.Column("target_run_ref", sa.String(96), nullable=False),
        sa.Column("target_attempt_ref", sa.String(96), nullable=False),
        sa.Column("target_fence_ref", sa.String(96), nullable=False),
        sa.Column("implementation_revision_ref", sa.String(256), nullable=False),
        sa.Column("implementation_bundle_receipt_ref", sa.String(96), nullable=False),
        sa.Column("implementation_bundle_receipt_hash", sa.String(64), nullable=False),
        sa.Column("implementation_bundle_usage_ref", sa.String(96), nullable=False),
        sa.Column("implementation_usage_receipt_ref", sa.String(96), nullable=False),
        sa.Column("implementation_usage_receipt_hash", sa.String(64), nullable=False),
        sa.Column("code_review_receipt_ref", sa.String(96), nullable=True),
        sa.Column("code_review_receipt_hash", sa.String(64), nullable=True),
        sa.Column("harness_operation_ref", sa.String(256), nullable=False),
        sa.Column("handle_json", sa.Text(), nullable=False),
        sa.Column("handle_hash", sa.String(64), nullable=False),
        sa.Column("preflight_json", sa.Text(), nullable=False),
        sa.Column("preflight_hash", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["target_ref"], ["ar_target_run_activations.target_ref"]
        ),
        sa.ForeignKeyConstraint(
            ["implementation_revision_ref"],
            ["rm_target_implementation_bundles.implementation_revision_ref"],
        ),
        sa.ForeignKeyConstraint(
            ["implementation_bundle_receipt_ref"],
            ["rm_target_implementation_bundles.receipt_ref"],
        ),
        sa.ForeignKeyConstraint(
            ["implementation_bundle_usage_ref"],
            ["rm_target_implementation_bundle_usages.usage_ref"],
        ),
        sa.ForeignKeyConstraint(
            ["implementation_usage_receipt_ref"],
            ["rm_target_implementation_bundle_usages.receipt_ref"],
        ),
        sa.UniqueConstraint(
            "target_run_ref", "target_attempt_ref", "implementation_revision_ref"
        ),
        sa.CheckConstraint(
            "(code_review_receipt_ref IS NULL) = "
            "(code_review_receipt_hash IS NULL)"
        ),
        *(
            _hash(name)
            for name in (
                "implementation_bundle_receipt_hash",
                "implementation_usage_receipt_hash",
                "code_review_receipt_hash",
                "handle_hash",
                "preflight_hash",
                "payload_hash",
                "request_hash",
                "receipt_hash",
            )
        ),
    )

    # The 0022 generic binding points at the metadata-backed legacy
    # eligibility table.  Formal-v3 therefore gets its own append-only binding
    # table instead of silently repurposing that foreign key.
    op.create_table(
        "rg_target_generic_execution_bindings_v3",
        sa.Column("binding_ref", sa.String(96), primary_key=True),
        sa.Column("target_ref", sa.String(96), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("target_run_ref", sa.String(96), nullable=False),
        sa.Column("target_attempt_ref", sa.String(96), nullable=False),
        sa.Column("target_fence_ref", sa.String(96), nullable=False),
        sa.Column("input_binding_ref", sa.String(96), nullable=False),
        sa.Column("input_binding_receipt_ref", sa.String(96), nullable=False),
        sa.Column("input_binding_receipt_hash", sa.String(64), nullable=False),
        sa.Column("execution_eligibility_ref", sa.String(96), nullable=False),
        sa.Column(
            "execution_eligibility_receipt_ref", sa.String(96), nullable=False
        ),
        sa.Column(
            "execution_eligibility_receipt_hash", sa.String(64), nullable=False
        ),
        sa.Column("operation_handle", sa.String(192), nullable=False, unique=True),
        sa.Column("execution_request_ref", sa.String(256), nullable=False),
        sa.Column("operation_request_json", sa.Text(), nullable=False),
        sa.Column("operation_request_hash", sa.String(64), nullable=False),
        sa.Column("command_spec_hash", sa.String(64), nullable=False),
        sa.Column("terminal_status", sa.String(32), nullable=False),
        sa.Column("exit_receipt_ref", sa.String(192), nullable=False, unique=True),
        sa.Column("exit_receipt_json", sa.Text(), nullable=False),
        sa.Column("exit_receipt_hash", sa.String(64), nullable=False),
        sa.Column("process_tree_drained", sa.Boolean(), nullable=False),
        sa.Column("currentness_known", sa.Boolean(), nullable=False),
        sa.Column("current", sa.Boolean(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["target_ref"], ["rg_targets.target_ref"]),
        sa.ForeignKeyConstraint(
            ["input_binding_ref"],
            ["rg_target_execution_input_bindings.binding_ref"],
        ),
        sa.ForeignKeyConstraint(
            ["input_binding_receipt_ref"],
            ["rg_target_execution_input_bindings.receipt_ref"],
        ),
        sa.ForeignKeyConstraint(
            ["execution_eligibility_ref"],
            ["ar_target_execution_eligibilities_v3.eligibility_ref"],
        ),
        sa.ForeignKeyConstraint(
            ["execution_eligibility_receipt_ref"],
            ["ar_target_execution_eligibilities_v3.receipt_ref"],
        ),
        sa.UniqueConstraint("target_ref", "ordinal"),
        sa.UniqueConstraint("target_run_ref", "target_attempt_ref"),
        sa.CheckConstraint("ordinal >= 1"),
        sa.CheckConstraint(
            "terminal_status IN ('succeeded', 'failed', 'stopped', 'timed_out')"
        ),
        sa.CheckConstraint("process_tree_drained = 1"),
        sa.CheckConstraint("currentness_known = 1"),
        sa.CheckConstraint("current = 1"),
        *(
            _hash(name)
            for name in (
                "input_binding_receipt_hash",
                "execution_eligibility_receipt_hash",
                "operation_request_hash",
                "command_spec_hash",
                "exit_receipt_hash",
                "payload_hash",
                "request_hash",
                "receipt_hash",
            )
        ),
    )

    # RM accepts the typed assets produced after one issuer-verified generic
    # operation.  It does not mint or reuse Experiment identities.
    op.create_table(
        "rm_target_generic_result_manifests",
        sa.Column("manifest_ref", sa.String(96), primary_key=True),
        sa.Column("target_ref", sa.String(96), nullable=False),
        sa.Column("target_run_ref", sa.String(96), nullable=False),
        sa.Column("target_attempt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("target_fence_ref", sa.String(96), nullable=False),
        sa.Column("generic_binding_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("operation_handle", sa.String(192), nullable=False, unique=True),
        sa.Column("roles_json", sa.Text(), nullable=False),
        sa.Column("roles_hash", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["generic_binding_ref"],
            ["rg_target_generic_execution_bindings_v3.binding_ref"],
        ),
        *(
            _hash(name)
            for name in (
                "roles_hash",
                "payload_hash",
                "request_hash",
                "receipt_hash",
            )
        ),
    )

    # RG assigns post-operation measurement identities only after re-reading
    # the accepted result bytes.  These are domain facts, not execution
    # identities and not aliases for TargetRun/Attempt/Fence.
    op.create_table(
        "rg_target_generic_measurements",
        sa.Column("measurement_ref", sa.String(96), primary_key=True),
        sa.Column("target_ref", sa.String(96), nullable=False),
        sa.Column("target_run_ref", sa.String(96), nullable=False),
        sa.Column("target_attempt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("target_fence_ref", sa.String(96), nullable=False),
        sa.Column("generic_binding_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("manifest_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("variant_run_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("evaluation_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("protocol_version_ref", sa.String(256), nullable=False),
        sa.Column("evaluation_attempt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("metric_result_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("experiment_keys_json", sa.Text(), nullable=False),
        sa.Column("experiment_keys_hash", sa.String(64), nullable=False),
        sa.Column("measurement_unit_key", sa.String(256), nullable=False),
        sa.Column("metrics_json", sa.Text(), nullable=False),
        sa.Column("metrics_hash", sa.String(64), nullable=False),
        sa.Column("variant_input_binding_json", sa.Text(), nullable=False),
        sa.Column("variant_input_binding_hash", sa.String(64), nullable=False),
        sa.Column("evaluation_input_binding_json", sa.Text(), nullable=False),
        sa.Column("evaluation_input_binding_hash", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["target_ref"], ["rg_targets.target_ref"]),
        sa.ForeignKeyConstraint(
            ["generic_binding_ref"],
            ["rg_target_generic_execution_bindings_v3.binding_ref"],
        ),
        sa.ForeignKeyConstraint(
            ["manifest_ref"],
            ["rm_target_generic_result_manifests.manifest_ref"],
        ),
        *(
            _hash(name)
            for name in (
                "experiment_keys_hash",
                "metrics_hash",
                "variant_input_binding_hash",
                "evaluation_input_binding_hash",
                "payload_hash",
                "request_hash",
                "receipt_hash",
            )
        ),
    )

    # AR closes the Target attempt only after the independent result review
    # and the RM/RG chains above are re-verified.  The legacy Experiment-backed
    # closure table remains readable but formal-v3 never writes it.
    op.create_table(
        "ar_target_generic_execution_closures",
        sa.Column("closure_ref", sa.String(96), primary_key=True),
        sa.Column("target_ref", sa.String(96), nullable=False),
        sa.Column("target_run_ref", sa.String(96), nullable=False),
        sa.Column("target_attempt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("target_fence_ref", sa.String(96), nullable=False),
        sa.Column("generic_binding_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("manifest_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("measurement_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("result_review_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["generic_binding_ref"],
            ["rg_target_generic_execution_bindings_v3.binding_ref"],
        ),
        sa.ForeignKeyConstraint(
            ["manifest_ref"],
            ["rm_target_generic_result_manifests.manifest_ref"],
        ),
        sa.ForeignKeyConstraint(
            ["measurement_ref"],
            ["rg_target_generic_measurements.measurement_ref"],
        ),
        sa.ForeignKeyConstraint(
            ["result_review_ref"],
            ["ar_target_review_evidence.review_ref"],
        ),
        *(
            _hash(name)
            for name in (
                "payload_hash",
                "request_hash",
                "receipt_hash",
            )
        ),
    )


def downgrade() -> None:
    raise RuntimeError("vNext production migrations are forward-only")
