"""Add the independent formal-v3 TargetRun runtime receipt chain.

Revision ID: 0022_target_run_runtime
Revises: 0021_bundle_report_closure
Create Date: 2026-08-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0022_target_run_runtime"
down_revision = "0021_bundle_report_closure"
branch_labels = None
depends_on = None


def _hash(name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"length({name}) = 64")


def _counter(name: str) -> sa.Column:
    return sa.Column(name, sa.Integer(), nullable=False, server_default="0")


def upgrade() -> None:
    op.add_column("agent_runtime_state", _counter("target_review_count"))
    op.add_column(
        "agent_runtime_state", _counter("target_execution_eligibility_count")
    )
    op.add_column(
        "agent_runtime_state", _counter("target_execution_closure_count")
    )
    op.add_column(
        "research_graph_state", _counter("target_execution_input_binding_count")
    )
    op.add_column(
        "research_graph_state", _counter("target_protected_execution_count")
    )
    op.add_column(
        "research_graph_state",
        _counter("target_generic_execution_binding_count"),
    )
    op.add_column(
        "research_memory_state", _counter("target_result_manifest_count")
    )
    op.add_column(
        "research_memory_state", _counter("target_implementation_artifact_count")
    )
    op.add_column(
        "research_memory_state", _counter("target_input_asset_proof_count")
    )
    op.add_column(
        "research_graph_state", _counter("target_input_asset_role_proof_count")
    )
    op.add_column(
        "research_graph_state", _counter("target_formal_plan_projection_count")
    )
    op.add_column(
        "research_graph_state", _counter("target_candidate_projection_count")
    )
    op.add_column(
        "research_graph_state", _counter("target_protocol_aggregation_count")
    )

    # The 0021 source receipt owns the accepted PlanDocument hash.  TargetRun
    # needs the fixed canonical FormalPlan projection hash instead, so RG issues
    # a distinct receipt after re-verifying that source and the normalized
    # completion contract.  The two subjects are never relabelled.
    op.create_table(
        "rg_target_formal_plan_projections",
        sa.Column("graph_ref", sa.String(96), primary_key=True),
        sa.Column("formal_plan_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("plan_document_hash", sa.String(64), nullable=False),
        sa.Column("source_acceptance_receipt_ref", sa.String(96), nullable=False),
        sa.Column("source_acceptance_receipt_hash", sa.String(64), nullable=False),
        sa.Column("completion_contract_json", sa.Text(), nullable=False),
        sa.Column("completion_contract_hash", sa.String(64), nullable=False),
        sa.Column("briefs_json", sa.Text(), nullable=False),
        sa.Column("briefs_hash", sa.String(64), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["graph_ref"], ["rg_target_graphs.graph_ref"]),
        *(
            _hash(name)
            for name in (
                "plan_document_hash",
                "source_acceptance_receipt_hash",
                "completion_contract_hash",
                "briefs_hash",
                "content_hash",
                "request_hash",
                "receipt_hash",
            )
        ),
    )

    # The accepted formal Target wrapper remains the source fact.  The fixed
    # TargetRun prototype consumes the canonical TargetCandidate projection,
    # whose digest is a distinct receipt subject and may not be forged by
    # relabelling the source spec receipt.
    op.create_table(
        "rg_target_candidate_projections",
        sa.Column("target_ref", sa.String(96), primary_key=True),
        sa.Column("graph_ref", sa.String(96), nullable=False),
        sa.Column("source_spec_hash", sa.String(64), nullable=False),
        sa.Column("source_acceptance_receipt_ref", sa.String(96), nullable=False),
        sa.Column("source_acceptance_receipt_hash", sa.String(64), nullable=False),
        sa.Column("candidate_json", sa.Text(), nullable=False),
        sa.Column("candidate_hash", sa.String(64), nullable=False),
        sa.Column("projection_digest", sa.String(64), nullable=False, unique=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["target_ref"], ["rg_targets.target_ref"]),
        sa.ForeignKeyConstraint(["graph_ref"], ["rg_target_graphs.graph_ref"]),
        *(
            _hash(name)
            for name in (
                "source_spec_hash",
                "source_acceptance_receipt_hash",
                "candidate_hash",
                "projection_digest",
                "request_hash",
                "receipt_hash",
            )
        ),
    )

    # 0021 remains independently deployable and stores only its source
    # PlanDocument chain.  At the 0022 boundary the report becomes formal-v3
    # only by adding the second projection chain.  The columns stay nullable
    # solely so already accepted 0021 source-only rows survive upgrade without
    # fabricated receipts; all new formal-v3 writes require every field and
    # readers reject a legacy row for completion/StageCommit.
    with op.batch_alter_table("ar_bundle_reports", recreate="always") as batch:
        batch.add_column(
            sa.Column(
                "formal_plan_projection_digest", sa.String(64), nullable=True
            )
        )
        batch.add_column(
            sa.Column(
                "formal_plan_projection_receipt_ref",
                sa.String(96),
                nullable=True,
            )
        )
        batch.add_column(
            sa.Column(
                "formal_plan_projection_receipt_hash",
                sa.String(64),
                nullable=True,
            )
        )
        batch.add_column(
            sa.Column("completion_contract_hash", sa.String(64), nullable=True)
        )
        batch.add_column(
            sa.Column("formal_plan_briefs_hash", sa.String(64), nullable=True)
        )
        batch.create_foreign_key(
            "fk_ar_bundle_reports_formal_plan_projection_receipt",
            "rg_target_formal_plan_projections",
            ["formal_plan_projection_receipt_ref"],
            ["receipt_ref"],
        )
        for name in (
            "formal_plan_projection_digest",
            "formal_plan_projection_receipt_hash",
            "completion_contract_hash",
            "formal_plan_briefs_hash",
        ):
            batch.create_check_constraint(
                f"ck_ar_bundle_reports_{name}_sha256",
                f"length({name}) = 64",
            )

    # 0020 freezes the candidate's source/version metadata.  This separate RM
    # fact binds that metadata hash to an actually accepted immutable artifact
    # (including its content and manifest hashes), so a TargetRun cannot use an
    # opaque verification ref as a substitute for executable bytes.
    op.create_table(
        "rm_target_implementation_artifacts",
        sa.Column("implementation_revision_ref", sa.String(256), primary_key=True),
        sa.Column("metadata_content_hash_ref", sa.String(64), nullable=False, unique=True),
        sa.Column("asset_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("version_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("artifact_content_hash", sa.String(64), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["implementation_revision_ref"],
            ["rm_implementation_revision_contents.implementation_revision_ref"],
        ),
        sa.ForeignKeyConstraint(["version_ref"], ["rm_asset_versions.version_ref"]),
        *(
            _hash(name)
            for name in (
                "metadata_content_hash_ref",
                "artifact_content_hash",
                "artifact_manifest_hash",
                "asset_receipt_hash",
                "payload_hash",
                "request_hash",
                "receipt_hash",
            )
        ),
    )

    # Generic RM version and RG role receipts have different subjects.  These
    # issuer-owned projections directly bind the canonical asset_ref required
    # by TargetLaunchRequest/TargetWorkHandle without relabelling either source
    # receipt.
    op.create_table(
        "rm_target_input_asset_proofs",
        sa.Column("proof_ref", sa.String(96), primary_key=True),
        sa.Column("target_ref", sa.String(96), nullable=False),
        sa.Column("asset_ref", sa.String(96), nullable=False),
        sa.Column("version_ref", sa.String(96), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("source_receipt_ref", sa.String(96), nullable=False),
        sa.Column("source_receipt_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["version_ref"], ["rm_asset_versions.version_ref"]),
        sa.UniqueConstraint("target_ref", "asset_ref"),
        *(
            _hash(name)
            for name in (
                "content_hash",
                "manifest_hash",
                "source_receipt_hash",
                "request_hash",
                "receipt_hash",
            )
        ),
    )
    op.create_table(
        "rg_target_input_asset_role_proofs",
        sa.Column("proof_ref", sa.String(96), primary_key=True),
        sa.Column("target_ref", sa.String(96), nullable=False),
        sa.Column("asset_ref", sa.String(96), nullable=False),
        sa.Column("source_role_ref", sa.String(96), nullable=False),
        sa.Column("source_role_receipt_ref", sa.String(96), nullable=False),
        sa.Column("source_role_receipt_hash", sa.String(64), nullable=False),
        sa.Column("rm_proof_receipt_ref", sa.String(96), nullable=False),
        sa.Column("rm_proof_receipt_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["target_ref"], ["rg_targets.target_ref"]),
        sa.UniqueConstraint("target_ref", "asset_ref"),
        *(
            _hash(name)
            for name in (
                "source_role_receipt_hash",
                "rm_proof_receipt_hash",
                "request_hash",
                "receipt_hash",
            )
        ),
    )

    # A TargetRun root is a native Harness Typed Run whose Run identity is the
    # already admitted domain TargetRunRef.  ar_harness_runs remains the sole
    # Session/Attempt/Fence authority; this table only freezes the exact launch
    # and full-conformance binding consumed at admission.
    op.create_table(
        "ar_target_harness_admissions",
        sa.Column("target_run_ref", sa.String(96), primary_key=True),
        sa.Column("target_ref", sa.String(96), nullable=False),
        sa.Column("harness_request_ref", sa.String(256), nullable=False, unique=True),
        sa.Column("harness_family", sa.String(16), nullable=False),
        sa.Column("model_ref", sa.String(256), nullable=False),
        sa.Column("auth_profile_ref", sa.String(256), nullable=False),
        sa.Column("full_conformance_binding_json", sa.Text(), nullable=False),
        sa.Column("full_conformance_binding_hash", sa.String(64), nullable=False),
        sa.Column("target_scope_binding_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("admitted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["target_run_ref"], ["ar_harness_runs.run_ref"]
        ),
        sa.ForeignKeyConstraint(["target_ref"], ["ar_target_launches.target_ref"]),
        sa.CheckConstraint("harness_family IN ('codex', 'claude')"),
        *(
            _hash(name)
            for name in (
                "full_conformance_binding_hash",
                "target_scope_binding_hash",
                "request_hash",
            )
        ),
    )

    # AR allocates the canonical child Session before a Target review turn and
    # binds it to the provider-native parent/child identities only after real
    # spawn and terminal completion evidence has been persisted.
    op.create_table(
        "ar_target_harness_child_sessions",
        sa.Column("child_session_ref", sa.String(96), primary_key=True),
        sa.Column("target_run_ref", sa.String(96), nullable=False),
        sa.Column("review_kind", sa.String(16), nullable=False),
        sa.Column("harness_operation_ref", sa.String(256), nullable=False, unique=True),
        sa.Column("parent_root_session_ref", sa.String(96), nullable=False),
        sa.Column("native_parent_session_ref", sa.String(256), nullable=True),
        sa.Column("native_child_session_ref", sa.String(256), nullable=True, unique=True),
        sa.Column("spawn_evidence_ref", sa.String(256), nullable=True, unique=True),
        sa.Column("completion_evidence_ref", sa.String(256), nullable=True, unique=True),
        sa.Column("payload_hash", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reserved_at", sa.Float(), nullable=False),
        sa.Column("bound_at", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["target_run_ref"], ["ar_harness_runs.run_ref"]),
        sa.CheckConstraint("review_kind IN ('code', 'result')"),
        sa.CheckConstraint("status IN ('reserved', 'bound')"),
        sa.UniqueConstraint("target_run_ref", "review_kind", "harness_operation_ref"),
        _hash("payload_hash"),
    )

    # Review records are AR/Harness facts.  Their receipts bind the complete
    # canonical payload hash; the child Session and spawn/completion evidence
    # remain distinct from the TargetRun root and from every other reviewer.
    op.create_table(
        "ar_target_review_evidence",
        sa.Column("review_ref", sa.String(96), primary_key=True),
        sa.Column("target_ref", sa.String(96), nullable=False),
        sa.Column("target_run_ref", sa.String(96), nullable=False),
        sa.Column("review_kind", sa.String(16), nullable=False),
        sa.Column("subject_ref", sa.String(256), nullable=False),
        sa.Column("parent_session_ref", sa.String(256), nullable=False),
        sa.Column("reviewer_session_ref", sa.String(256), nullable=False, unique=True),
        sa.Column(
            "reviewer_spawn_evidence_ref", sa.String(256), nullable=False, unique=True
        ),
        sa.Column(
            "reviewer_completion_evidence_ref",
            sa.String(256),
            nullable=False,
            unique=True,
        ),
        sa.Column("harness_operation_ref", sa.String(256), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["target_ref"], ["ar_target_launches.target_ref"]),
        sa.ForeignKeyConstraint(
            ["target_run_ref"], ["ar_harness_runs.run_ref"]
        ),
        sa.ForeignKeyConstraint(
            ["harness_operation_ref"],
            ["ar_harness_provider_operations.operation_ref"],
        ),
        sa.ForeignKeyConstraint(
            ["reviewer_session_ref"],
            ["ar_target_harness_child_sessions.child_session_ref"],
        ),
        sa.CheckConstraint("review_kind IN ('code', 'result')"),
        sa.CheckConstraint("parent_session_ref != reviewer_session_ref"),
        sa.UniqueConstraint("target_run_ref", "review_kind", "subject_ref"),
        *(
            _hash(name)
            for name in ("payload_hash", "request_hash", "receipt_hash")
        ),
    )

    # Protected execution is eligible only after AR re-verifies the current
    # TargetRun handle, the activation's exact preflight, RM's accepted
    # implementation artifact (which binds real bytes), candidate-ready and
    # self-check Harness evidence, and the exact code-review acceptance.  The
    # generic execution port consumes this receipt; neither an RM intake nor a
    # self-consistent preflight can substitute for it.
    op.create_table(
        "ar_target_execution_eligibilities",
        sa.Column("eligibility_ref", sa.String(96), primary_key=True),
        sa.Column("target_ref", sa.String(96), nullable=False),
        sa.Column("target_run_ref", sa.String(96), nullable=False),
        sa.Column("target_attempt_ref", sa.String(96), nullable=False),
        sa.Column("target_fence_ref", sa.String(96), nullable=False),
        sa.Column("implementation_revision_ref", sa.String(256), nullable=False),
        sa.Column("implementation_artifact_receipt_ref", sa.String(96), nullable=False),
        sa.Column("implementation_artifact_receipt_hash", sa.String(64), nullable=False),
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
            ["rm_target_implementation_artifacts.implementation_revision_ref"],
        ),
        sa.ForeignKeyConstraint(
            ["implementation_artifact_receipt_ref"],
            ["rm_target_implementation_artifacts.receipt_ref"],
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
                "implementation_artifact_receipt_hash",
                "code_review_receipt_hash",
                "handle_hash",
                "preflight_hash",
                "payload_hash",
                "request_hash",
                "receipt_hash",
            )
        ),
    )

    # The input closure exists before Experiment admission and therefore before
    # any provider/RM/RG Experiment side effect.  Its receipt subject is the
    # binding_ref carried by TargetWorkHandle.
    op.create_table(
        "rg_target_execution_input_bindings",
        sa.Column("binding_ref", sa.String(96), primary_key=True),
        sa.Column("target_ref", sa.String(96), nullable=False),
        sa.Column("target_run_ref", sa.String(96), nullable=False),
        sa.Column("target_attempt_ref", sa.String(96), nullable=False),
        sa.Column("target_fence_ref", sa.String(96), nullable=False),
        sa.Column("target_spec_hash", sa.String(64), nullable=False),
        sa.Column("target_scope_binding_hash", sa.String(64), nullable=False),
        sa.Column("input_refs_json", sa.Text(), nullable=False),
        sa.Column("input_refs_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["target_ref"], ["rg_targets.target_ref"]),
        sa.UniqueConstraint("target_run_ref", "target_attempt_ref"),
        *(
            _hash(name)
            for name in (
                "target_spec_hash",
                "target_scope_binding_hash",
                "input_refs_hash",
                "request_hash",
                "receipt_hash",
            )
        ),
    )

    # This is the explicit bridge between the domain TargetRun and the
    # protected Experiment execution.  It never aliases either identity.
    op.create_table(
        "rg_target_protected_execution_bindings",
        sa.Column("binding_ref", sa.String(96), primary_key=True),
        sa.Column("target_ref", sa.String(96), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("target_run_ref", sa.String(96), nullable=False),
        sa.Column("target_attempt_ref", sa.String(96), nullable=False),
        sa.Column("target_fence_ref", sa.String(96), nullable=False),
        sa.Column("input_binding_ref", sa.String(96), nullable=False),
        sa.Column("experiment_run_ref", sa.String(96), nullable=False, unique=True),
        sa.Column(
            "experiment_attempt_ref", sa.String(96), nullable=False, unique=True
        ),
        sa.Column("experiment_fence_ref", sa.String(96), nullable=False, unique=True),
        sa.Column(
            "evaluation_attempt_ref", sa.String(96), nullable=False, unique=True
        ),
        sa.Column("execution_request_ref", sa.String(128), nullable=False),
        sa.Column("definition_hash", sa.String(64), nullable=False),
        sa.Column("experiment_request_receipt_ref", sa.String(96), nullable=False),
        sa.Column("experiment_request_receipt_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["target_ref"], ["rg_targets.target_ref"]),
        sa.ForeignKeyConstraint(
            ["input_binding_ref"], ["rg_target_execution_input_bindings.binding_ref"]
        ),
        sa.ForeignKeyConstraint(
            ["experiment_run_ref"], ["ar_experiment_runs.run_ref"]
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_attempt_ref"], ["rg_evaluation_attempts.evaluation_attempt_ref"]
        ),
        sa.UniqueConstraint("target_ref", "ordinal"),
        sa.UniqueConstraint("target_run_ref", "target_attempt_ref"),
        sa.CheckConstraint("ordinal >= 1"),
        *(
            _hash(name)
            for name in (
                "definition_hash",
                "experiment_request_receipt_hash",
                "request_hash",
                "receipt_hash",
            )
        ),
    )

    # Formal-v3 execution identity is the generic port's opaque operation, not
    # an Experiment Run.  RG accepts this immutable terminal binding only after
    # the port re-verifies its signed request/exit spool, AR re-verifies the
    # execution-eligible receipt, and the Target Fence is still current.
    op.create_table(
        "rg_target_generic_execution_bindings",
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
            ["input_binding_ref"], ["rg_target_execution_input_bindings.binding_ref"]
        ),
        sa.ForeignKeyConstraint(
            ["input_binding_receipt_ref"],
            ["rg_target_execution_input_bindings.receipt_ref"],
        ),
        sa.ForeignKeyConstraint(
            ["execution_eligibility_ref"],
            ["ar_target_execution_eligibilities.eligibility_ref"],
        ),
        sa.ForeignKeyConstraint(
            ["execution_eligibility_receipt_ref"],
            ["ar_target_execution_eligibilities.receipt_ref"],
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

    # RM accepts the exact typed result-asset manifest after verifying every
    # referenced AssetVersion and receipt.  A generic intake receipt is never
    # relabelled as this manifest receipt.
    op.create_table(
        "rm_target_result_manifests",
        sa.Column("manifest_ref", sa.String(96), primary_key=True),
        sa.Column("target_ref", sa.String(96), nullable=False),
        sa.Column("target_run_ref", sa.String(96), nullable=False),
        sa.Column("variant_run_ref", sa.String(96), nullable=False),
        sa.Column("evaluation_attempt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("metric_result_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("experiment_run_ref", sa.String(96), nullable=False),
        sa.Column("experiment_attempt_ref", sa.String(96), nullable=False),
        sa.Column("experiment_fence_ref", sa.String(96), nullable=False),
        sa.Column("roles_json", sa.Text(), nullable=False),
        sa.Column("roles_hash", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
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

    # Atomic protocol aggregation is derived from the immutable provider
    # result_content asset.  The caller cannot choose the ordered parts or the
    # rule that this RG receipt attests.
    op.create_table(
        "rg_target_protocol_aggregations",
        sa.Column("aggregation_ref", sa.String(96), primary_key=True),
        sa.Column("target_ref", sa.String(96), nullable=False),
        sa.Column("protected_binding_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("result_manifest_ref", sa.String(96), nullable=False, unique=True),
        sa.Column(
            "evaluation_attempt_ref", sa.String(96), nullable=False, unique=True
        ),
        sa.Column("result_content_version_ref", sa.String(96), nullable=False),
        sa.Column("result_content_hash", sa.String(64), nullable=False),
        sa.Column("protocol_version_ref", sa.String(256), nullable=False),
        sa.Column("part_keys_json", sa.Text(), nullable=False),
        sa.Column("part_keys_hash", sa.String(64), nullable=False),
        sa.Column("aggregation_rule_ref", sa.String(256), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["target_ref"], ["rg_targets.target_ref"]),
        sa.ForeignKeyConstraint(
            ["protected_binding_ref"],
            ["rg_target_protected_execution_bindings.binding_ref"],
        ),
        sa.ForeignKeyConstraint(
            ["result_manifest_ref"], ["rm_target_result_manifests.manifest_ref"]
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_attempt_ref"],
            ["rg_evaluation_attempts.evaluation_attempt_ref"],
        ),
        *(
            _hash(name)
            for name in (
                "result_content_hash",
                "part_keys_hash",
                "payload_hash",
                "request_hash",
                "receipt_hash",
            )
        ),
    )

    # AR issues this receipt only after independently re-verifying the
    # protected Experiment execution receipt, RM manifest, RG measurement and
    # Harness result review.  Its receipt subject is the TargetRun Attempt,
    # while the payload retains all distinct Experiment identities.
    op.create_table(
        "ar_target_execution_closures",
        sa.Column("closure_ref", sa.String(96), primary_key=True),
        sa.Column("target_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("target_run_ref", sa.String(96), nullable=False),
        sa.Column("target_attempt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("target_fence_ref", sa.String(96), nullable=False),
        sa.Column("protected_binding_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("experiment_run_ref", sa.String(96), nullable=False),
        sa.Column("experiment_attempt_ref", sa.String(96), nullable=False),
        sa.Column("experiment_fence_ref", sa.String(96), nullable=False),
        sa.Column("evaluation_attempt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("experiment_result_hash", sa.String(64), nullable=False),
        sa.Column("result_manifest_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("formal_metric_ref", sa.String(96), nullable=False, unique=True),
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
            ["protected_binding_ref"],
            ["rg_target_protected_execution_bindings.binding_ref"],
        ),
        sa.ForeignKeyConstraint(
            ["result_manifest_ref"], ["rm_target_result_manifests.manifest_ref"]
        ),
        sa.ForeignKeyConstraint(
            ["result_review_ref"], ["ar_target_review_evidence.review_ref"]
        ),
        *(
            _hash(name)
            for name in (
                "experiment_result_hash",
                "payload_hash",
                "request_hash",
                "receipt_hash",
            )
        ),
    )


def downgrade() -> None:
    raise RuntimeError("vNext production migrations are forward-only")
