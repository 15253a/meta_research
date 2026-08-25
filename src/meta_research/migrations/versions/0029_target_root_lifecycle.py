"""Add the root-owned Target lifecycle and final frozen RM handoff.

Revision ID: 0029_target_root_lifecycle
Revises: 0028_target_measurement_runtime
Create Date: 2026-08-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0029_target_root_lifecycle"
down_revision = "0028_target_measurement_runtime"
branch_labels = None
depends_on = None


def _hash(name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"length({name}) = 64")


def _counter(name: str) -> sa.Column:
    return sa.Column(name, sa.Integer(), nullable=False, server_default="0")


def upgrade() -> None:
    op.add_column(
        "agent_runtime_state", _counter("target_root_lifecycle_count")
    )
    op.add_column(
        "agent_runtime_state", _counter("target_root_completion_count")
    )
    op.add_column(
        "agent_runtime_state",
        _counter("target_root_completion_rejection_count"),
    )
    op.add_column(
        "research_memory_state",
        _counter("target_root_completion_manifest_count"),
    )
    op.add_column(
        "research_graph_state",
        _counter("target_root_measurement_count"),
    )

    # This is one outside edge around a freely iterating root Session.  It is
    # intentionally not a command/phase/checkpoint state machine and has no
    # dependency on the legacy execution-port preflight activation.
    op.create_table(
        "ar_target_root_lifecycles",
        sa.Column("lifecycle_ref", sa.String(96), primary_key=True),
        sa.Column("target_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("launch_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("target_run_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("root_session_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("target_attempt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("target_fence_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("initial_handle_json", sa.Text(), nullable=False),
        sa.Column("initial_handle_hash", sa.String(64), nullable=False),
        sa.Column("candidate_json", sa.Text(), nullable=False),
        sa.Column("candidate_hash", sa.String(64), nullable=False),
        sa.Column("formal_plan_json", sa.Text(), nullable=False),
        sa.Column("formal_plan_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("completion_ref", sa.String(96), nullable=True, unique=True),
        sa.Column("cancel_ref", sa.String(96), nullable=True, unique=True),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        sa.Column("cancel_requested_at", sa.Float(), nullable=True),
        sa.Column("cancelled_at", sa.Float(), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["target_ref"], ["rg_targets.target_ref"]),
        sa.ForeignKeyConstraint(["launch_ref"], ["ar_target_launches.launch_ref"]),
        sa.CheckConstraint(
            "status IN ('running', 'finalizing', 'completed', 'cancelled')"
        ),
        sa.CheckConstraint(
            "(status = 'running' AND completion_ref IS NULL AND "
            "cancelled_at IS NULL) OR (status = 'cancelled' AND "
            "completion_ref IS NULL AND cancel_ref IS NOT NULL AND "
            "cancel_requested_at IS NOT NULL AND cancelled_at IS NOT NULL) OR "
            "(status IN ('finalizing', 'completed') AND completion_ref IS NOT "
            "NULL AND cancel_ref IS NULL AND cancel_reason IS NULL AND "
            "cancel_requested_at IS NULL AND cancelled_at IS NULL)"
        ),
        sa.CheckConstraint(
            "(cancel_ref IS NULL AND cancel_reason IS NULL AND "
            "cancel_requested_at IS NULL) OR (cancel_ref IS NOT NULL AND "
            "cancel_requested_at IS NOT NULL)"
        ),
        *(
            _hash(name)
            for name in (
                "initial_handle_hash",
                "candidate_hash",
                "formal_plan_hash",
                "request_hash",
            )
        ),
    )

    # AR freezes each immutable completion generation before any artifact
    # becomes an RM acceptance.  A rejected generation is never rewritten;
    # its successor stays on the same TargetRun/Attempt/Fence and links the
    # exact rejection that woke the root Session.
    op.create_table(
        "ar_target_root_completions",
        sa.Column("completion_ref", sa.String(96), primary_key=True),
        sa.Column("target_ref", sa.String(96), nullable=False),
        sa.Column("target_run_ref", sa.String(96), nullable=False),
        sa.Column("target_attempt_ref", sa.String(96), nullable=False),
        sa.Column("target_fence_ref", sa.String(96), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("predecessor_completion_ref", sa.String(96), nullable=True),
        sa.Column("predecessor_rejection_ref", sa.String(96), nullable=True),
        sa.Column("handle_json", sa.Text(), nullable=False),
        sa.Column("handle_hash", sa.String(64), nullable=False),
        sa.Column("handoff_json", sa.Text(), nullable=False),
        sa.Column("handoff_hash", sa.String(64), nullable=False),
        sa.Column("harness_operation_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("evidence_ref", sa.String(128), nullable=False, unique=True),
        sa.Column("evidence_content_hash", sa.String(64), nullable=False),
        sa.Column("workspace_ref", sa.String(96), nullable=False),
        sa.Column("implementation_revision_ref", sa.String(96), nullable=True),
        sa.Column("implementation_tree_hash", sa.String(64), nullable=True),
        sa.Column("result_document_hash", sa.String(64), nullable=True),
        sa.Column("artifact_snapshot_hash", sa.String(64), nullable=True, unique=True),
        sa.Column("candidate_rejection_code", sa.String(128), nullable=True),
        sa.Column("candidate_rejection_feedback", sa.Text(), nullable=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["target_ref"], ["ar_target_root_lifecycles.target_ref"]
        ),
        sa.ForeignKeyConstraint(
            ["predecessor_completion_ref"],
            ["ar_target_root_completions.completion_ref"],
        ),
        sa.UniqueConstraint("target_ref", "generation"),
        sa.CheckConstraint("generation >= 1"),
        sa.CheckConstraint(
            "(generation = 1 AND predecessor_completion_ref IS NULL AND "
            "predecessor_rejection_ref IS NULL) OR (generation > 1 AND "
            "predecessor_completion_ref IS NOT NULL AND "
            "predecessor_rejection_ref IS NOT NULL)"
        ),
        sa.CheckConstraint(
            "(implementation_revision_ref IS NULL AND "
            "implementation_tree_hash IS NULL AND result_document_hash IS NULL "
            "AND artifact_snapshot_hash IS NULL AND candidate_rejection_code IS "
            "NOT NULL AND candidate_rejection_feedback IS NOT NULL) OR "
            "(implementation_revision_ref IS NOT NULL AND "
            "implementation_tree_hash IS NOT NULL AND result_document_hash IS "
            "NOT NULL AND artifact_snapshot_hash IS NOT NULL AND "
            "candidate_rejection_code IS NULL AND candidate_rejection_feedback "
            "IS NULL)"
        ),
        sa.CheckConstraint(
            "candidate_rejection_code IS NULL OR "
            "(length(candidate_rejection_code) > 0 AND "
            "length(candidate_rejection_code) <= 128)"
        ),
        sa.CheckConstraint(
            "candidate_rejection_feedback IS NULL OR "
            "(length(candidate_rejection_feedback) > 0 AND "
            "length(candidate_rejection_feedback) <= 16384)"
        ),
        *(
            _hash(name)
            for name in (
                "handle_hash",
                "handoff_hash",
                "evidence_content_hash",
                "implementation_tree_hash",
                "result_document_hash",
                "artifact_snapshot_hash",
                "payload_hash",
                "request_hash",
                "receipt_hash",
            )
        ),
    )

    # RM owns immutable managed copies of the exact bytes captured by AR's
    # completion.  Entries are a closed canonical list of RM Asset bindings;
    # RG can later consume this manifest without reopening the live workspace.
    op.create_table(
        "rm_target_root_completion_manifests",
        sa.Column("manifest_ref", sa.String(96), primary_key=True),
        sa.Column("completion_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("target_ref", sa.String(96), nullable=False),
        sa.Column("target_run_ref", sa.String(96), nullable=False),
        sa.Column("workspace_ref", sa.String(96), nullable=False),
        sa.Column("implementation_revision_ref", sa.String(96), nullable=False),
        sa.Column("implementation_tree_hash", sa.String(64), nullable=False),
        sa.Column("result_document_path", sa.String(1024), nullable=False),
        sa.Column("result_document_json", sa.Text(), nullable=False),
        sa.Column("result_document_hash", sa.String(64), nullable=False),
        sa.Column("artifact_snapshot_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("entries_json", sa.Text(), nullable=False),
        sa.Column("entries_hash", sa.String(64), nullable=False),
        sa.Column("completion_receipt_ref", sa.String(96), nullable=False),
        sa.Column("completion_receipt_hash", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["completion_ref"], ["ar_target_root_completions.completion_ref"]
        ),
        *(
            _hash(name)
            for name in (
                "implementation_tree_hash",
                "result_document_hash",
                "artifact_snapshot_hash",
                "entries_hash",
                "completion_receipt_hash",
                "payload_hash",
                "request_hash",
                "receipt_hash",
            )
        ),
    )

    # AR preserves the exact RM/RG rejection that caused a root wake.  This is
    # lineage, not a scientific phase: one rejected immutable completion gets
    # at most one issuer-backed rejection and any successor links it.
    op.create_table(
        "ar_target_root_completion_rejections",
        sa.Column("rejection_ref", sa.String(96), primary_key=True),
        sa.Column("completion_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("target_ref", sa.String(96), nullable=False),
        sa.Column("target_run_ref", sa.String(96), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("manifest_ref", sa.String(96), nullable=True),
        sa.Column("issuer", sa.String(32), nullable=False),
        sa.Column("code", sa.String(128), nullable=False),
        sa.Column("feedback", sa.Text(), nullable=False),
        sa.Column("issuer_receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("issuer_receipt_hash", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("rejected_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["completion_ref"], ["ar_target_root_completions.completion_ref"]
        ),
        sa.ForeignKeyConstraint(
            ["manifest_ref"],
            ["rm_target_root_completion_manifests.manifest_ref"],
        ),
        sa.UniqueConstraint("target_ref", "generation"),
        sa.CheckConstraint("generation >= 1"),
        sa.CheckConstraint("issuer IN ('research_memory', 'research_graph')"),
        sa.CheckConstraint("length(code) > 0 AND length(code) <= 128"),
        sa.CheckConstraint("length(feedback) > 0 AND length(feedback) <= 16384"),
        *(
            _hash(name)
            for name in (
                "issuer_receipt_hash",
                "payload_hash",
                "request_hash",
            )
        ),
    )

    # Both legacy and root Targets have an admitted launch, while only the
    # legacy execution-port path has ar_target_run_activations.  Rebuild the
    # two publication tables so a root terminal can use the existing Inbox.
    # Every legacy activation already FK-binds its own launch, so this widens
    # admissible publishers without weakening legacy referential integrity.
    fk_names = {
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"
    }
    with op.batch_alter_table(
        "ar_target_handoff_manifests",
        recreate="always",
        naming_convention=fk_names,
    ) as batch:
        batch.drop_constraint(
            "fk_ar_target_handoff_manifests_target_ref_ar_target_run_activations",
            type_="foreignkey",
        )
        batch.create_foreign_key(
            "fk_ar_target_handoff_manifests_target_ref_ar_target_launches",
            "ar_target_launches",
            ["target_ref"],
            ["target_ref"],
        )
    with op.batch_alter_table(
        "ar_target_work_notices",
        recreate="always",
        naming_convention=fk_names,
    ) as batch:
        batch.drop_constraint(
            "fk_ar_target_work_notices_target_ref_ar_target_run_activations",
            type_="foreignkey",
        )
        batch.create_foreign_key(
            "fk_ar_target_work_notices_target_ref_ar_target_launches",
            "ar_target_launches",
            ["target_ref"],
            ["target_ref"],
        )

    # RG admits one formal measurement/identity set from the issuer-verified
    # AR completion plus RM frozen manifest.  This table is not a Target phase
    # machine: it is the durable fact behind the sole final TargetCommit.
    op.create_table(
        "rg_target_root_measurements",
        sa.Column("measurement_ref", sa.String(96), primary_key=True),
        sa.Column("target_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("target_run_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("completion_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("manifest_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("authority_ref", sa.String(96), nullable=False),
        sa.Column("authority_hash", sa.String(64), nullable=False),
        sa.Column("variant_run_ref", sa.String(96), nullable=False, unique=True),
        sa.Column(
            "evaluation_attempt_ref", sa.String(96), nullable=False, unique=True
        ),
        sa.Column("metric_result_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("metrics_json", sa.Text(), nullable=False),
        sa.Column("metrics_hash", sa.String(64), nullable=False),
        sa.Column("checkpoint_refs_json", sa.Text(), nullable=False),
        sa.Column("checkpoint_refs_hash", sa.String(64), nullable=False),
        sa.Column("variant_input_binding_json", sa.Text(), nullable=False),
        sa.Column("variant_input_binding_hash", sa.String(64), nullable=False),
        sa.Column("evaluation_input_binding_json", sa.Text(), nullable=False),
        sa.Column("evaluation_input_binding_hash", sa.String(64), nullable=False),
        sa.Column("measurement_payload_json", sa.Text(), nullable=False),
        sa.Column("measurement_payload_hash", sa.String(64), nullable=False),
        sa.Column("accepted_measurement_json", sa.Text(), nullable=False),
        sa.Column("accepted_measurement_hash", sa.String(64), nullable=False),
        sa.Column("completion_payload_hash", sa.String(64), nullable=False),
        sa.Column("completion_receipt_ref", sa.String(96), nullable=False),
        sa.Column("completion_receipt_hash", sa.String(64), nullable=False),
        sa.Column("manifest_payload_hash", sa.String(64), nullable=False),
        sa.Column("manifest_receipt_ref", sa.String(96), nullable=False),
        sa.Column("manifest_receipt_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["target_ref"], ["rg_targets.target_ref"]),
        sa.ForeignKeyConstraint(
            ["completion_ref"], ["ar_target_root_completions.completion_ref"]
        ),
        sa.ForeignKeyConstraint(
            ["manifest_ref"],
            ["rm_target_root_completion_manifests.manifest_ref"],
        ),
        *(
            _hash(name)
            for name in (
                "authority_hash",
                "metrics_hash",
                "checkpoint_refs_hash",
                "variant_input_binding_hash",
                "evaluation_input_binding_hash",
                "measurement_payload_hash",
                "accepted_measurement_hash",
                "completion_payload_hash",
                "completion_receipt_hash",
                "manifest_payload_hash",
                "manifest_receipt_hash",
                "request_hash",
                "receipt_hash",
            )
        ),
    )


def downgrade() -> None:
    raise RuntimeError("vNext production migrations are forward-only")
