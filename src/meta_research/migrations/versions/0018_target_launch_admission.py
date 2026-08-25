"""Add pre-execution Target launch admission and worker-owned frontier storage.

Revision ID: 0018_target_launch_admission
Revises: 0017_bundle_target_rolling
Create Date: 2026-08-24
"""

from __future__ import annotations

import hashlib
import json

import sqlalchemy as sa
from alembic import op


revision = "0018_target_launch_admission"
down_revision = "0017_bundle_target_rolling"
branch_labels = None
depends_on = None


def _hash(name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"length({name}) = 64")


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _spec_receipt_hash(row: dict[str, object]) -> str:
    bindings = {
        "target_ref": row["target_ref"],
        "graph_ref": row["graph_ref"],
        "target_acceptance_receipt_ref": row["target_receipt_ref"],
        "target_acceptance_receipt_hash": row["target_receipt_hash"],
    }
    return _canonical_hash(
        {
            "schema_ref": "meta-research/owner-acceptance-receipt/v1",
            "issuer": "research_graph",
            "kind": "target_spec_content_accepted",
            "subject_ref": row["spec_hash"],
            "bindings": bindings,
        }
    )


def upgrade() -> None:
    # Existing Target receipts are subject-bound to TargetRef.  The fixed
    # prototype separately requires a receipt whose actual subject is the
    # complete spec content hash, so never relabel the old receipt in a
    # ReceiptProof.  Backfill a distinct RG acceptance identity instead.
    op.create_table(
        "rg_target_spec_acceptances",
        sa.Column("acceptance_ref", sa.String(96), primary_key=True),
        sa.Column("target_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("graph_ref", sa.String(96), nullable=False),
        sa.Column("spec_content_hash_ref", sa.String(64), nullable=False),
        sa.Column("target_acceptance_receipt_ref", sa.String(96), nullable=False),
        sa.Column("target_acceptance_receipt_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["target_ref"], ["rg_targets.target_ref"]),
        sa.ForeignKeyConstraint(["graph_ref"], ["rg_target_graphs.graph_ref"]),
        _hash("spec_content_hash_ref"),
        _hash("target_acceptance_receipt_hash"),
        _hash("receipt_hash"),
    )
    connection = op.get_bind()
    targets = connection.execute(
        sa.text(
            "SELECT target_ref, graph_ref, spec_hash, receipt_ref AS "
            "target_receipt_ref, receipt_hash AS target_receipt_hash, "
            "accepted_at FROM rg_targets ORDER BY accepted_at, target_ref"
        )
    ).mappings()
    for target in targets:
        row = dict(target)
        identity = hashlib.sha256(str(row["target_ref"]).encode("utf-8")).hexdigest()
        connection.execute(
            sa.text(
                "INSERT INTO rg_target_spec_acceptances (acceptance_ref, "
                "target_ref, graph_ref, spec_content_hash_ref, "
                "target_acceptance_receipt_ref, "
                "target_acceptance_receipt_hash, receipt_ref, receipt_hash, "
                "accepted_at) VALUES (:acceptance_ref, :target_ref, :graph_ref, "
                ":spec_hash, :target_receipt_ref, :target_receipt_hash, "
                ":receipt_ref, :receipt_hash, :accepted_at)"
            ),
            {
                **row,
                "acceptance_ref": "target_spec_acceptance_" + identity[:32],
                "receipt_ref": "rg_target_spec_receipt_" + identity[:32],
                "receipt_hash": _spec_receipt_hash(row),
            },
        )

    # This is the admission boundary, not an Experiment run.  Admission may
    # allocate the distinct TargetRun identity, but it must not invent Harness
    # Session/Attempt/Fence identities.  Those reserved fields must remain
    # empty in this admission revision; a later worker migration can publish
    # execution state only after starting a real recoverable Harness Session.
    op.create_table(
        "ar_target_launches",
        sa.Column("launch_ref", sa.String(96), primary_key=True),
        sa.Column("operation_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("target_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("graph_ref", sa.String(96), nullable=False),
        sa.Column("stage_request_ref", sa.String(64), nullable=False),
        sa.Column("quest_ref", sa.String(96), nullable=False),
        sa.Column("target_spec_content_hash_ref", sa.String(256), nullable=False),
        sa.Column("target_spec_receipt_ref", sa.String(96), nullable=False),
        sa.Column("target_spec_receipt_subject_ref", sa.String(256), nullable=False),
        sa.Column("accepted_input_target_commit_refs_json", sa.Text(), nullable=False),
        sa.Column(
            "accepted_input_target_commit_refs_hash", sa.String(64), nullable=False
        ),
        sa.Column("accepted_input_asset_refs_json", sa.Text(), nullable=False),
        sa.Column("accepted_input_asset_refs_hash", sa.String(64), nullable=False),
        sa.Column("accepted_input_asset_proofs_json", sa.Text(), nullable=False),
        sa.Column("accepted_input_asset_proofs_hash", sa.String(64), nullable=False),
        sa.Column("recoverable_required", sa.Boolean(), nullable=False),
        sa.Column("target_run_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("root_session_ref", sa.String(96), nullable=True, unique=True),
        sa.Column("execution_attempt_ref", sa.String(96), nullable=True, unique=True),
        sa.Column("execution_fence_ref", sa.String(96), nullable=True, unique=True),
        sa.Column(
            "execution_input_binding_ref", sa.String(96), nullable=True, unique=True
        ),
        sa.Column(
            "execution_input_binding_receipt_ref",
            sa.String(96),
            nullable=True,
            unique=True,
        ),
        sa.Column("execution_input_binding_receipt_hash", sa.String(64), nullable=True),
        sa.Column("current_handle_json", sa.Text(), nullable=True),
        sa.Column("current_handle_hash", sa.String(64), nullable=True),
        sa.Column("dispatch_decision_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("dispatch_receipt_ref", sa.String(96), nullable=False),
        sa.Column("dispatch_receipt_hash", sa.String(64), nullable=False),
        sa.Column("human_request_ref", sa.String(96), nullable=True),
        sa.Column("human_waiter_ref", sa.String(128), nullable=True),
        sa.Column("human_waiter_generation", sa.Integer(), nullable=True),
        sa.Column("human_authorization_receipt_ref", sa.String(96), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("admitted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["target_ref"], ["rg_targets.target_ref"]),
        sa.ForeignKeyConstraint(["graph_ref"], ["rg_target_graphs.graph_ref"]),
        sa.ForeignKeyConstraint(
            ["stage_request_ref"], ["ae_stage_run_requests.request_ref"]
        ),
        sa.ForeignKeyConstraint(
            ["dispatch_decision_ref"], ["ar_bundle_dispatch_decisions.decision_ref"]
        ),
        sa.CheckConstraint("recoverable_required = 1"),
        sa.CheckConstraint("status = 'admitted'"),
        sa.CheckConstraint(
            "root_session_ref IS NULL AND execution_attempt_ref IS NULL "
            "AND execution_fence_ref IS NULL "
            "AND execution_input_binding_ref IS NULL "
            "AND execution_input_binding_receipt_ref IS NULL "
            "AND execution_input_binding_receipt_hash IS NULL "
            "AND current_handle_json IS NULL AND current_handle_hash IS NULL"
        ),
        sa.CheckConstraint(
            "(human_request_ref IS NULL AND human_waiter_ref IS NULL "
            "AND human_waiter_generation IS NULL "
            "AND human_authorization_receipt_ref IS NULL) OR "
            "(human_request_ref IS NOT NULL AND human_waiter_ref IS NOT NULL "
            "AND human_waiter_generation >= 1 "
            "AND human_authorization_receipt_ref IS NOT NULL)"
        ),
        *(
            _hash(name)
            for name in (
                "accepted_input_target_commit_refs_hash",
                "accepted_input_asset_refs_hash",
                "accepted_input_asset_proofs_hash",
                "execution_input_binding_receipt_hash",
                "current_handle_hash",
                "dispatch_receipt_hash",
                "request_hash",
                "receipt_hash",
            )
        ),
    )

    # The later TargetRun worker publishes this compact projection only after
    # a real recoverable Harness root Session exists.  Admission itself leaves
    # this table empty, so Bundle cannot mistake acceptance for execution.
    op.create_table(
        "ar_target_frontier_entries",
        sa.Column("target_ref", sa.String(96), primary_key=True),
        sa.Column("launch_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("target_spec_content_hash_ref", sa.String(256), nullable=False),
        sa.Column("target_spec_receipt_ref", sa.String(96), nullable=False),
        sa.Column("target_spec_receipt_subject_ref", sa.String(256), nullable=False),
        sa.Column("state_revision", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("current_handle_json", sa.Text(), nullable=False),
        sa.Column("current_handle_hash", sa.String(64), nullable=False),
        sa.Column("terminal_fact_ref", sa.String(96), nullable=True),
        sa.Column("currentness_known", sa.Boolean(), nullable=False),
        sa.Column("current", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["target_ref"], ["rg_targets.target_ref"]),
        sa.ForeignKeyConstraint(["launch_ref"], ["ar_target_launches.launch_ref"]),
        sa.CheckConstraint("state_revision >= 1"),
        sa.CheckConstraint("state = 'running'"),
        sa.CheckConstraint("terminal_fact_ref IS NULL"),
        sa.CheckConstraint("currentness_known = 1 AND current = 1"),
        _hash("current_handle_hash"),
    )


def downgrade() -> None:
    raise RuntimeError("vNext production migrations are forward-only")
