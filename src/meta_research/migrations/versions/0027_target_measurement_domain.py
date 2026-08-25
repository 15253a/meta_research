"""Accept Plan-bound Target measurement domain identities at graph admission.

Revision ID: 0027_target_measurement_domain
Revises: 0026_bundle_inbox_runtime
Create Date: 2026-08-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0027_target_measurement_domain"
down_revision = "0026_bundle_inbox_runtime"
branch_labels = None
depends_on = None


def _hash(name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"length({name}) = 64")


def upgrade() -> None:
    op.add_column(
        "research_graph_state",
        sa.Column(
            "target_measurement_domain_authority_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )

    # This is the immutable bridge from a complete formal Target spec to the
    # five pre-execution measurement identities already owned by RG.  It is
    # deliberately not an execution request: graph admission must not create
    # a VariantRun, EvaluationAttempt, provider request, or Agent Runtime row.
    op.create_table(
        "rg_target_measurement_domain_authorities",
        sa.Column("authority_ref", sa.String(96), primary_key=True),
        sa.Column("authority_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("target_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("graph_ref", sa.String(96), nullable=False),
        sa.Column("graph_generation", sa.Integer(), nullable=False),
        sa.Column("graph_acceptance_receipt_ref", sa.String(96), nullable=False),
        sa.Column("graph_acceptance_receipt_hash", sa.String(64), nullable=False),
        sa.Column("append_ref", sa.String(96), nullable=True),
        sa.Column("predecessor_head_receipt_ref", sa.String(96), nullable=True),
        sa.Column("predecessor_head_receipt_hash", sa.String(64), nullable=True),
        sa.Column("proposal_ref", sa.String(96), nullable=True),
        sa.Column("proposal_hash", sa.String(64), nullable=True),
        sa.Column("proposal_receipt_ref", sa.String(96), nullable=True),
        sa.Column("proposal_receipt_hash", sa.String(64), nullable=True),
        sa.Column("formal_plan_ref", sa.String(96), nullable=False),
        sa.Column("stage_request_ref", sa.String(96), nullable=False),
        sa.Column("plan_content_ref", sa.String(96), nullable=False),
        sa.Column("plan_document_hash", sa.String(64), nullable=False),
        sa.Column("answer_contract_hash", sa.String(64), nullable=False),
        sa.Column("accepted_formal_plan_binding_hash", sa.String(64), nullable=False),
        sa.Column("plan_content_receipt_ref", sa.String(96), nullable=False),
        sa.Column("plan_content_receipt_hash", sa.String(64), nullable=False),
        sa.Column("formal_plan_receipt_ref", sa.String(96), nullable=False),
        sa.Column("formal_plan_receipt_hash", sa.String(64), nullable=False),
        sa.Column("stage_commit_ref", sa.String(96), nullable=False),
        sa.Column("stage_commit_receipt_ref", sa.String(96), nullable=False),
        sa.Column("stage_commit_receipt_hash", sa.String(64), nullable=False),
        sa.Column("completion_contract_json", sa.Text(), nullable=False),
        sa.Column("completion_contract_hash", sa.String(64), nullable=False),
        sa.Column("formal_plan_projection_digest", sa.String(64), nullable=False),
        sa.Column("target_plan_hash", sa.String(64), nullable=False),
        sa.Column("target_key", sa.String(128), nullable=False),
        sa.Column("target_ordinal", sa.Integer(), nullable=False),
        sa.Column("target_spec_hash", sa.String(64), nullable=False),
        sa.Column("target_receipt_ref", sa.String(96), nullable=False),
        sa.Column("target_receipt_hash", sa.String(64), nullable=False),
        sa.Column("target_spec_acceptance_ref", sa.String(96), nullable=False),
        sa.Column("target_spec_receipt_ref", sa.String(96), nullable=False),
        sa.Column("target_spec_receipt_hash", sa.String(64), nullable=False),
        sa.Column("measurement_contract_json", sa.Text(), nullable=False),
        sa.Column("measurement_contract_hash", sa.String(64), nullable=False),
        sa.Column("experiment_keys_json", sa.Text(), nullable=False),
        sa.Column("experiment_keys_hash", sa.String(64), nullable=False),
        sa.Column("measurement_unit_key", sa.String(256), nullable=False),
        sa.Column("baseline_ref", sa.String(96), nullable=False),
        sa.Column("variant_ref", sa.String(96), nullable=False),
        sa.Column("evaluation_protocol_ref", sa.String(96), nullable=False),
        sa.Column("protocol_version_ref", sa.String(96), nullable=False),
        sa.Column("evaluation_ref", sa.String(96), nullable=False),
        sa.Column("native_identity_set_hash", sa.String(64), nullable=False),
        sa.Column("aggregation_evidence_ref", sa.String(96), nullable=True),
        sa.Column("aggregation_content_json", sa.Text(), nullable=True),
        sa.Column("aggregation_content_hash", sa.String(64), nullable=True),
        sa.Column("aggregation_part_keys_json", sa.Text(), nullable=True),
        sa.Column("aggregation_part_keys_hash", sa.String(64), nullable=True),
        sa.Column("aggregation_rule_ref", sa.String(256), nullable=True),
        sa.Column("aggregation_receipt_ref", sa.String(96), nullable=True),
        sa.Column("aggregation_receipt_hash", sa.String(64), nullable=True),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["target_ref"], ["rg_targets.target_ref"]),
        sa.ForeignKeyConstraint(["graph_ref"], ["rg_target_graphs.graph_ref"]),
        sa.ForeignKeyConstraint(
            ["stage_request_ref"], ["ae_stage_run_requests.request_ref"]
        ),
        sa.ForeignKeyConstraint(
            ["plan_content_ref"], ["rm_plan_documents.content_ref"]
        ),
        sa.ForeignKeyConstraint(
            ["stage_commit_ref"], ["ae_stage_commits.commit_ref"]
        ),
        sa.ForeignKeyConstraint(
            ["target_spec_acceptance_ref"],
            ["rg_target_spec_acceptances.acceptance_ref"],
        ),
        sa.ForeignKeyConstraint(
            ["append_ref"], ["rg_target_graph_appends.append_ref"]
        ),
        sa.ForeignKeyConstraint(
            ["baseline_ref"], ["rg_experiment_baselines.baseline_ref"]
        ),
        sa.ForeignKeyConstraint(
            ["variant_ref"], ["rg_experiment_variants.variant_ref"]
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_protocol_ref"],
            ["rg_evaluation_protocols.evaluation_protocol_ref"],
        ),
        sa.ForeignKeyConstraint(
            ["protocol_version_ref"],
            ["rg_protocol_versions.protocol_version_ref"],
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_ref"], ["rg_evaluations.evaluation_ref"]
        ),
        sa.UniqueConstraint("graph_ref", "target_ordinal"),
        sa.CheckConstraint("graph_generation >= 0"),
        sa.CheckConstraint("target_ordinal >= 0"),
        sa.CheckConstraint(
            "(aggregation_evidence_ref IS NULL AND aggregation_content_json IS "
            "NULL AND aggregation_content_hash IS NULL AND "
            "aggregation_part_keys_json IS NULL AND aggregation_part_keys_hash "
            "IS NULL AND aggregation_rule_ref IS NULL AND "
            "aggregation_receipt_ref IS NULL AND aggregation_receipt_hash IS "
            "NULL) OR (aggregation_evidence_ref IS NOT NULL AND "
            "aggregation_content_json IS NOT NULL AND aggregation_content_hash "
            "IS NOT NULL AND aggregation_part_keys_json IS NOT NULL AND "
            "aggregation_part_keys_hash IS NOT NULL AND aggregation_rule_ref IS "
            "NOT NULL AND aggregation_receipt_ref IS NOT NULL AND "
            "aggregation_receipt_hash IS NOT NULL)"
        ),
        sa.CheckConstraint(
            "(graph_generation = 0 AND append_ref IS NULL AND "
            "predecessor_head_receipt_ref IS NULL AND "
            "predecessor_head_receipt_hash IS NULL AND proposal_ref IS NULL "
            "AND proposal_hash IS NULL AND proposal_receipt_ref IS NULL AND "
            "proposal_receipt_hash IS NULL) OR (graph_generation >= 1 AND "
            "append_ref IS NOT NULL AND predecessor_head_receipt_ref IS NOT "
            "NULL AND predecessor_head_receipt_hash IS NOT NULL AND "
            "proposal_ref IS NOT NULL AND proposal_hash IS NOT NULL AND "
            "proposal_receipt_ref IS NOT NULL AND proposal_receipt_hash IS "
            "NOT NULL)"
        ),
        *(
            _hash(name)
            for name in (
                "authority_hash",
                "graph_acceptance_receipt_hash",
                "predecessor_head_receipt_hash",
                "proposal_hash",
                "proposal_receipt_hash",
                "plan_document_hash",
                "answer_contract_hash",
                "accepted_formal_plan_binding_hash",
                "plan_content_receipt_hash",
                "formal_plan_receipt_hash",
                "stage_commit_receipt_hash",
                "completion_contract_hash",
                "formal_plan_projection_digest",
                "target_plan_hash",
                "target_spec_hash",
                "target_receipt_hash",
                "target_spec_receipt_hash",
                "measurement_contract_hash",
                "experiment_keys_hash",
                "native_identity_set_hash",
                "aggregation_content_hash",
                "aggregation_part_keys_hash",
                "aggregation_receipt_hash",
                "receipt_hash",
            )
        ),
    )
    op.create_index(
        "ix_rg_target_measurement_domain_authorities_graph",
        "rg_target_measurement_domain_authorities",
        ["graph_ref", "target_ordinal"],
    )


def downgrade() -> None:
    raise RuntimeError("vNext production migrations are forward-only")
