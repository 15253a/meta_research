"""Add experiment identity, execution, asset-role, and measurement state.

Revision ID: 0009_experiment_measurement
Revises: 0008_quest_acquisition_session
Create Date: 2026-08-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0009_experiment_measurement"
down_revision = "0008_quest_acquisition_session"
branch_labels = None
depends_on = None


def _counter(table: str, name: str) -> None:
    op.add_column(
        table,
        sa.Column(name, sa.Integer(), nullable=False, server_default="0"),
    )


def _hash(name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"length({name}) = 64")


def upgrade() -> None:
    for name in (
        "experiment_baseline_count",
        "experiment_variant_count",
        "evaluation_protocol_count",
        "protocol_version_count",
        "evaluation_count",
        "variant_run_count",
        "evaluation_attempt_count",
        "experiment_input_binding_count",
        "experiment_asset_role_count",
        "formal_measurement_count",
    ):
        _counter("research_graph_state", name)
    for name in (
        "experiment_run_count",
        "experiment_completed_run_count",
        "experiment_attempt_count",
        "experiment_session_count",
        "active_experiment_run_count",
    ):
        _counter("agent_runtime_state", name)

    op.create_table(
        "rg_experiment_baselines",
        sa.Column("baseline_ref", sa.String(length=96), primary_key=True),
        sa.Column("quest_ref", sa.String(length=96), nullable=False),
        sa.Column("forward_contract_json", sa.Text(), nullable=False),
        sa.Column("forward_contract_hash", sa.String(length=64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["quest_ref"], ["rg_quests.quest_ref"]),
        sa.UniqueConstraint("forward_contract_hash"),
        _hash("forward_contract_hash"),
    )
    op.create_table(
        "rg_experiment_variants",
        sa.Column("variant_ref", sa.String(length=96), primary_key=True),
        sa.Column("baseline_ref", sa.String(length=96), nullable=False),
        sa.Column("recipe_json", sa.Text(), nullable=False),
        sa.Column("recipe_hash", sa.String(length=64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["baseline_ref"], ["rg_experiment_baselines.baseline_ref"]
        ),
        sa.UniqueConstraint("baseline_ref", "recipe_hash"),
        _hash("recipe_hash"),
    )
    op.create_table(
        "rg_evaluation_protocols",
        sa.Column("evaluation_protocol_ref", sa.String(length=96), primary_key=True),
        sa.Column("quest_ref", sa.String(length=96), nullable=False),
        sa.Column("lineage_json", sa.Text(), nullable=False),
        sa.Column("lineage_hash", sa.String(length=64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["quest_ref"], ["rg_quests.quest_ref"]),
        sa.UniqueConstraint("lineage_hash"),
        _hash("lineage_hash"),
    )
    op.create_table(
        "rg_protocol_versions",
        sa.Column("protocol_version_ref", sa.String(length=96), primary_key=True),
        sa.Column("evaluation_protocol_ref", sa.String(length=96), nullable=False),
        sa.Column("protocol_json", sa.Text(), nullable=False),
        sa.Column("protocol_hash", sa.String(length=64), nullable=False),
        sa.Column("required_metrics_json", sa.Text(), nullable=False),
        sa.Column("required_metrics_hash", sa.String(length=64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["evaluation_protocol_ref"],
            ["rg_evaluation_protocols.evaluation_protocol_ref"],
        ),
        sa.UniqueConstraint("evaluation_protocol_ref", "protocol_hash"),
        _hash("protocol_hash"),
        _hash("required_metrics_hash"),
    )
    op.create_table(
        "rg_evaluations",
        sa.Column("evaluation_ref", sa.String(length=96), primary_key=True),
        sa.Column("variant_ref", sa.String(length=96), nullable=False),
        sa.Column("protocol_version_ref", sa.String(length=96), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["variant_ref"], ["rg_experiment_variants.variant_ref"]
        ),
        sa.ForeignKeyConstraint(
            ["protocol_version_ref"],
            ["rg_protocol_versions.protocol_version_ref"],
        ),
        sa.UniqueConstraint("variant_ref", "protocol_version_ref"),
    )
    op.create_table(
        "rg_variant_runs",
        sa.Column("variant_run_ref", sa.String(length=96), primary_key=True),
        sa.Column("variant_ref", sa.String(length=96), nullable=False),
        sa.Column("input_binding_ref", sa.String(length=96), nullable=False, unique=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["variant_ref"], ["rg_experiment_variants.variant_ref"]
        ),
        sa.CheckConstraint("status IN ('planned', 'executed', 'failed')"),
    )
    op.create_table(
        "rg_evaluation_attempts",
        sa.Column("evaluation_attempt_ref", sa.String(length=96), primary_key=True),
        sa.Column("evaluation_ref", sa.String(length=96), nullable=False),
        sa.Column("variant_run_ref", sa.String(length=96), nullable=False),
        sa.Column("input_binding_ref", sa.String(length=96), nullable=False, unique=True),
        sa.Column("checkpoint_role_refs_json", sa.Text(), nullable=False),
        sa.Column("checkpoint_role_refs_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("formal_rejection_code", sa.String(length=96), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["evaluation_ref"], ["rg_evaluations.evaluation_ref"]
        ),
        sa.ForeignKeyConstraint(
            ["variant_run_ref"], ["rg_variant_runs.variant_run_ref"]
        ),
        sa.CheckConstraint(
            "status IN ('planned', 'assets_partial', 'assets_accepted', "
            "'measurement_accepted', 'measurement_rejected', 'failed')"
        ),
        sa.CheckConstraint(
            "(status = 'measurement_rejected' AND formal_rejection_code IS NOT "
            "NULL) OR (status != 'measurement_rejected' AND "
            "formal_rejection_code IS NULL)"
        ),
        _hash("checkpoint_role_refs_hash"),
    )
    op.create_table(
        "rg_experiment_input_bindings",
        sa.Column("binding_ref", sa.String(length=96), primary_key=True),
        sa.Column("subject_kind", sa.String(length=32), nullable=False),
        sa.Column("subject_ref", sa.String(length=96), nullable=False, unique=True),
        sa.Column("inputs_json", sa.Text(), nullable=False),
        sa.Column("inputs_hash", sa.String(length=64), nullable=False),
        sa.Column("receipt_ref", sa.String(length=96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.CheckConstraint(
            "subject_kind IN ('variant_run', 'evaluation_attempt')"
        ),
        _hash("inputs_hash"),
        _hash("receipt_hash"),
    )
    op.create_table(
        "rg_experiment_requests",
        sa.Column("execution_request_ref", sa.String(length=96), primary_key=True),
        sa.Column("intent_json", sa.Text(), nullable=False),
        sa.Column("intent_hash", sa.String(length=64), nullable=False),
        sa.Column("definition_json", sa.Text(), nullable=False),
        sa.Column("definition_hash", sa.String(length=64), nullable=False),
        sa.Column("definition_asset_ref", sa.String(length=96), nullable=False),
        sa.Column("definition_version_ref", sa.String(length=96), nullable=False),
        sa.Column("definition_manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("definition_receipt_ref", sa.String(length=96), nullable=False),
        sa.Column("definition_receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("implementation_asset_ref", sa.String(length=96), nullable=False),
        sa.Column("implementation_version_ref", sa.String(length=96), nullable=False),
        sa.Column("implementation_content_hash", sa.String(length=64), nullable=False),
        sa.Column("implementation_manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("implementation_receipt_ref", sa.String(length=96), nullable=False),
        sa.Column("implementation_receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("request_receipt_ref", sa.String(length=96), nullable=False, unique=True),
        sa.Column("request_receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("quest_ref", sa.String(length=96), nullable=False),
        sa.Column("variant_run_ref", sa.String(length=96), nullable=False),
        sa.Column(
            "evaluation_attempt_ref", sa.String(length=96), nullable=False, unique=True
        ),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["quest_ref"], ["rg_quests.quest_ref"]),
        sa.ForeignKeyConstraint(
            ["variant_run_ref"], ["rg_variant_runs.variant_run_ref"]
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_attempt_ref"],
            ["rg_evaluation_attempts.evaluation_attempt_ref"],
        ),
        _hash("intent_hash"),
        _hash("definition_hash"),
        _hash("definition_manifest_hash"),
        _hash("definition_receipt_hash"),
        _hash("implementation_content_hash"),
        _hash("implementation_manifest_hash"),
        _hash("implementation_receipt_hash"),
        _hash("request_receipt_hash"),
    )
    op.create_index(
        "ix_rg_experiment_requests_created",
        "rg_experiment_requests",
        ["created_at", "evaluation_attempt_ref"],
    )
    op.create_table(
        "rg_experiment_idempotency",
        sa.Column("idempotency_key", sa.String(length=128), primary_key=True),
        sa.Column("execution_request_ref", sa.String(length=96), nullable=False),
        sa.Column("intent_hash", sa.String(length=64), nullable=False),
        sa.Column("recorded_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["execution_request_ref"],
            ["rg_experiment_requests.execution_request_ref"],
        ),
        _hash("intent_hash"),
    )
    op.create_table(
        "rg_experiment_asset_roles",
        sa.Column("role_ref", sa.String(length=96), primary_key=True),
        sa.Column("subject_kind", sa.String(length=32), nullable=False),
        sa.Column("subject_ref", sa.String(length=96), nullable=False),
        sa.Column("role", sa.String(length=40), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("asset_ref", sa.String(length=96), nullable=False),
        sa.Column("version_ref", sa.String(length=96), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("asset_receipt_ref", sa.String(length=96), nullable=False),
        sa.Column("asset_receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("receipt_ref", sa.String(length=96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("subject_kind", "subject_ref", "role", "ordinal"),
        sa.CheckConstraint("ordinal >= 0"),
        sa.CheckConstraint(
            "(role = 'checkpoint_artifact' AND subject_kind = 'variant_run') OR "
            "(role != 'checkpoint_artifact' AND subject_kind = 'evaluation_attempt')"
        ),
        sa.CheckConstraint(
            "role IN ('checkpoint_artifact', 'log_asset', 'analysis_asset', "
            "'result_content')"
        ),
        _hash("content_hash"),
        _hash("manifest_hash"),
        _hash("asset_receipt_hash"),
        _hash("receipt_hash"),
    )
    op.create_table(
        "rg_evaluation_attempt_checkpoints",
        sa.Column("evaluation_attempt_ref", sa.String(length=96), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("checkpoint_role_ref", sa.String(length=96), nullable=False),
        sa.ForeignKeyConstraint(
            ["evaluation_attempt_ref"],
            ["rg_evaluation_attempts.evaluation_attempt_ref"],
        ),
        sa.ForeignKeyConstraint(
            ["checkpoint_role_ref"], ["rg_experiment_asset_roles.role_ref"]
        ),
        sa.PrimaryKeyConstraint("evaluation_attempt_ref", "ordinal"),
        sa.UniqueConstraint("evaluation_attempt_ref", "checkpoint_role_ref"),
        sa.CheckConstraint("ordinal >= 0"),
    )
    op.create_table(
        "rg_metric_results",
        sa.Column("metric_result_ref", sa.String(length=96), primary_key=True),
        sa.Column(
            "evaluation_attempt_ref", sa.String(length=96), nullable=False, unique=True
        ),
        sa.Column("result_role_ref", sa.String(length=96), nullable=False, unique=True),
        sa.Column("metrics_json", sa.Text(), nullable=False),
        sa.Column("metrics_hash", sa.String(length=64), nullable=False),
        sa.Column("required_metrics_hash", sa.String(length=64), nullable=False),
        sa.Column("run_ref", sa.String(length=96), nullable=False),
        sa.Column("execution_attempt_ref", sa.String(length=96), nullable=False),
        sa.Column("fence_ref", sa.String(length=96), nullable=False),
        sa.Column("execution_result_hash", sa.String(length=64), nullable=False),
        sa.Column("execution_receipt_ref", sa.String(length=96), nullable=False),
        sa.Column("execution_receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("receipt_ref", sa.String(length=96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["evaluation_attempt_ref"],
            ["rg_evaluation_attempts.evaluation_attempt_ref"],
        ),
        sa.ForeignKeyConstraint(
            ["result_role_ref"], ["rg_experiment_asset_roles.role_ref"]
        ),
        _hash("metrics_hash"),
        _hash("required_metrics_hash"),
        _hash("execution_result_hash"),
        _hash("execution_receipt_hash"),
        _hash("receipt_hash"),
    )

    op.create_table(
        "ar_experiment_runs",
        sa.Column("run_ref", sa.String(length=96), primary_key=True),
        sa.Column("execution_request_ref", sa.String(length=96), nullable=False, unique=True),
        sa.Column("quest_ref", sa.String(length=96), nullable=False),
        sa.Column("definition_hash", sa.String(length=64), nullable=False),
        sa.Column("execution_request_receipt_ref", sa.String(length=96), nullable=False),
        sa.Column("execution_request_receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("implementation_asset_ref", sa.String(length=96), nullable=False),
        sa.Column("implementation_version_ref", sa.String(length=96), nullable=False),
        sa.Column("implementation_content_hash", sa.String(length=64), nullable=False),
        sa.Column("implementation_manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("implementation_receipt_ref", sa.String(length=96), nullable=False),
        sa.Column("implementation_receipt_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "evaluation_attempt_ref", sa.String(length=96), nullable=False, unique=True
        ),
        sa.Column("variant_run_ref", sa.String(length=96), nullable=False),
        sa.Column("variant_input_binding_ref", sa.String(length=96), nullable=False),
        sa.Column("variant_input_hash", sa.String(length=64), nullable=False),
        sa.Column("variant_input_receipt_ref", sa.String(length=96), nullable=False),
        sa.Column("variant_input_receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("measurement_input_binding_ref", sa.String(length=96), nullable=False),
        sa.Column("measurement_input_hash", sa.String(length=64), nullable=False),
        sa.Column("measurement_input_receipt_ref", sa.String(length=96), nullable=False),
        sa.Column("measurement_input_receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("provider_operation_ref", sa.String(length=128), nullable=False, unique=True),
        sa.Column("provider_operation_generation", sa.Integer(), nullable=False),
        sa.Column(
            "provider_operation_retry_permitted",
            sa.Boolean(),
            nullable=False,
        ),
        sa.Column("attempt_ref", sa.String(length=96), nullable=False, unique=True),
        sa.Column("attempt_generation", sa.Integer(), nullable=False),
        sa.Column(
            "root_session_ref", sa.String(length=96), nullable=False, unique=True
        ),
        sa.Column("fence_ref", sa.String(length=96), nullable=False, unique=True),
        sa.Column("runtime_binding_json", sa.Text(), nullable=False),
        sa.Column("runtime_binding_hash", sa.String(length=64), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("result_hash", sa.String(length=64), nullable=True),
        sa.Column("failure_code", sa.String(length=96), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("completed_at", sa.Float(), nullable=True),
        sa.CheckConstraint(
            "status IN ('admitted', 'running', 'executed', 'failed')"
        ),
        sa.CheckConstraint("attempt_generation >= 1"),
        sa.CheckConstraint("provider_operation_generation >= 1"),
        sa.CheckConstraint(
            "(status = 'executed' AND result_json IS NOT NULL AND result_hash IS NOT NULL "
            "AND failure_code IS NULL AND completed_at IS NOT NULL) OR "
            "(status = 'failed' AND result_json IS NULL AND result_hash IS NULL "
            "AND failure_code IS NOT NULL AND completed_at IS NOT NULL) OR "
            "(status IN ('admitted', 'running') AND result_json IS NULL "
            "AND result_hash IS NULL AND failure_code IS NULL AND completed_at IS NULL)"
        ),
        _hash("variant_input_hash"),
        _hash("definition_hash"),
        _hash("execution_request_receipt_hash"),
        _hash("implementation_content_hash"),
        _hash("implementation_manifest_hash"),
        _hash("implementation_receipt_hash"),
        _hash("variant_input_receipt_hash"),
        _hash("measurement_input_hash"),
        _hash("measurement_input_receipt_hash"),
        _hash("runtime_binding_hash"),
        sa.CheckConstraint("result_hash IS NULL OR length(result_hash) = 64"),
    )
    op.create_index(
        "ix_ar_experiment_runs_status",
        "ar_experiment_runs",
        ["status", "updated_at", "run_ref"],
    )
    op.create_table(
        "ar_experiment_sessions",
        sa.Column("root_session_ref", sa.String(length=96), primary_key=True),
        sa.Column("run_ref", sa.String(length=96), nullable=False, unique=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["run_ref"], ["ar_experiment_runs.run_ref"]),
        sa.CheckConstraint("status IN ('open', 'closed')"),
    )
    op.create_table(
        "ar_experiment_attempts",
        sa.Column("attempt_ref", sa.String(length=96), primary_key=True),
        sa.Column("run_ref", sa.String(length=96), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("root_session_ref", sa.String(length=96), nullable=False),
        sa.Column("fence_ref", sa.String(length=96), nullable=False, unique=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("retired_reason", sa.String(length=96), nullable=True),
        sa.Column("receipt_ref", sa.String(length=96), nullable=True, unique=True),
        sa.Column("receipt_hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("started_at", sa.Float(), nullable=True),
        sa.Column("completed_at", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["run_ref"], ["ar_experiment_runs.run_ref"]),
        sa.ForeignKeyConstraint(
            ["root_session_ref"],
            ["ar_experiment_sessions.root_session_ref"],
        ),
        sa.UniqueConstraint("run_ref", "generation"),
        sa.CheckConstraint("generation >= 1"),
        sa.CheckConstraint(
            "status IN ('admitted', 'running', 'executed', 'failed', 'retired')"
        ),
        sa.CheckConstraint(
            "(receipt_ref IS NULL AND receipt_hash IS NULL) OR "
            "(receipt_ref IS NOT NULL AND receipt_hash IS NOT NULL "
            "AND length(receipt_hash) = 64)"
        ),
    )
    op.create_table(
        "ar_experiment_events",
        sa.Column("event_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_ref", sa.String(length=96), nullable=False, unique=True),
        sa.Column("run_ref", sa.String(length=96), nullable=False),
        sa.Column("attempt_ref", sa.String(length=96), nullable=False),
        sa.Column("fence_ref", sa.String(length=96), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("observed_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["run_ref"], ["ar_experiment_runs.run_ref"]),
        sa.ForeignKeyConstraint(
            ["attempt_ref"], ["ar_experiment_attempts.attempt_ref"]
        ),
        sa.UniqueConstraint("run_ref", "attempt_ref", "sequence"),
        sa.CheckConstraint("sequence >= 1"),
        sa.CheckConstraint("kind IN ('stdout', 'telemetry', 'status')"),
        _hash("payload_hash"),
        sqlite_autoincrement=True,
    )


def downgrade() -> None:
    raise RuntimeError("vNext production migrations are forward-only")
