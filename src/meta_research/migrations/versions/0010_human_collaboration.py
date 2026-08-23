"""Add Human Collaboration and Owner-owned HumanRequest facts.

Revision ID: 0010_human_collaboration
Revises: 0009_manual_question_creation
Create Date: 2026-08-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0010_human_collaboration"
down_revision = "0009_manual_question_creation"
branch_labels = None
depends_on = None


def _hash(name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"length({name}) = 64")


def _counter(name: str) -> sa.Column:
    return sa.Column(name, sa.Integer(), nullable=False, server_default="0")


_PRE_0009_RECONCILIATION_STEPS = (
    "quest_goal",
    "quest_source_material",
    "question_content",
    "question_identity",
    "cycle_activation",
)
_HC_RECONCILIATION_STEPS = (
    "quest_goal",
    "broad_research_authorization",
    "acquisition_session",
    "quest_source_material",
    "question_content",
    "question_identity",
    "cycle_activation",
)


def _step_expression(column: str, steps: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{step}'" for step in steps)
    return f"{column} IN ({quoted})"


def _step_check(column: str, steps: tuple[str, ...]) -> sa.CheckConstraint:
    return sa.CheckConstraint(_step_expression(column, steps))


def upgrade() -> None:
    _extend_hc_reconciliation_steps()
    for table in (
        "research_graph_state",
        "research_memory_state",
        "agent_runtime_state",
        "advancement_engine_state",
    ):
        op.add_column(table, _counter("human_request_count"))
    op.add_column(
        "human_collaboration_state", _counter("human_response_count")
    )
    op.add_column(
        "human_collaboration_state", _counter("companion_session_count")
    )
    op.add_column(
        "human_collaboration_state", _counter("pending_companion_turn_count")
    )
    op.add_column(
        "human_collaboration_state", _counter("soft_constraint_count")
    )

    op.create_table(
        "owner_human_requests",
        sa.Column("request_ref", sa.String(length=96), primary_key=True),
        sa.Column("issuer", sa.String(length=32), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("quest_ref", sa.String(length=64), nullable=True),
        sa.Column("kind", sa.String(length=48), nullable=False),
        sa.Column("obligation", sa.Text(), nullable=False),
        sa.Column("business_purpose", sa.Text(), nullable=False),
        sa.Column("target_assertion_json", sa.Text(), nullable=False),
        sa.Column("target_assertion_hash", sa.String(length=64), nullable=False),
        sa.Column("acceptance_conditions_json", sa.Text(), nullable=False),
        sa.Column("acceptance_conditions_hash", sa.String(length=64), nullable=False),
        sa.Column("required_authorization_json", sa.Text(), nullable=True),
        sa.Column("required_authorization_hash", sa.String(length=64), nullable=True),
        sa.Column("expires_at", sa.Float(), nullable=True),
        sa.Column("identity_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("is_current", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("issuer", "request_id", "revision"),
        _hash("target_assertion_hash"),
        _hash("acceptance_conditions_hash"),
        _hash("identity_hash"),
        sa.CheckConstraint("revision >= 1"),
        sa.CheckConstraint("is_current IN (0, 1)"),
        sa.CheckConstraint(
            "kind IN ('library_reconnect', 'external_material_api_access', "
            "'offline_action', 'capability_authorization')"
        ),
        sa.CheckConstraint(
            "status IN ('open', 'satisfied', 'declined', 'withdrawn', "
            "'expired', 'superseded')"
        ),
        sa.CheckConstraint(
            "(required_authorization_json IS NULL AND "
            "required_authorization_hash IS NULL) OR "
            "(required_authorization_json IS NOT NULL AND "
            "required_authorization_hash IS NOT NULL AND "
            "length(required_authorization_hash) = 64)"
        ),
    )
    op.create_index(
        "ix_owner_human_requests_identity",
        "owner_human_requests",
        ["issuer", "identity_hash", "is_current", "status"],
    )
    op.create_index(
        "ix_owner_human_requests_current",
        "owner_human_requests",
        ["issuer", "is_current", "updated_at"],
    )

    op.create_table(
        "owner_human_request_waiters",
        sa.Column("request_ref", sa.String(length=96), nullable=False),
        sa.Column("waiter_ref", sa.String(length=128), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("target_assertion_json", sa.Text(), nullable=False),
        sa.Column("target_assertion_hash", sa.String(length=64), nullable=False),
        sa.Column("wait_scope", sa.String(length=16), nullable=False),
        sa.Column("other_blockers_json", sa.Text(), nullable=False),
        sa.Column("other_blockers_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["request_ref"], ["owner_human_requests.request_ref"]
        ),
        sa.PrimaryKeyConstraint("request_ref", "waiter_ref"),
        _hash("target_assertion_hash"),
        _hash("other_blockers_hash"),
        sa.CheckConstraint("generation >= 1"),
        sa.CheckConstraint("wait_scope IN ('local', 'quest')"),
        sa.CheckConstraint("status IN ('blocked', 'released', 'cancelled', 'consumed')"),
    )

    op.create_table(
        "owner_human_request_evaluations",
        sa.Column("evaluation_ref", sa.String(length=64), primary_key=True),
        sa.Column("request_ref", sa.String(length=96), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("decision", sa.String(length=24), nullable=False),
        sa.Column("response_refs_json", sa.Text(), nullable=False),
        sa.Column("response_refs_hash", sa.String(length=64), nullable=False),
        sa.Column("evidence_refs_json", sa.Text(), nullable=False),
        sa.Column("evidence_refs_hash", sa.String(length=64), nullable=False),
        sa.Column("reason_code", sa.String(length=96), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["request_ref"], ["owner_human_requests.request_ref"]
        ),
        sa.UniqueConstraint("request_ref", "sequence"),
        _hash("response_refs_hash"),
        _hash("evidence_refs_hash"),
        sa.CheckConstraint("sequence >= 1"),
        sa.CheckConstraint(
            "decision IN ('satisfied', 'needs_input', 'declined', 'stale')"
        ),
    )

    op.create_table(
        "owner_human_request_dispositions",
        sa.Column("disposition_ref", sa.String(length=64), primary_key=True),
        sa.Column("request_ref", sa.String(length=96), nullable=False, unique=True),
        sa.Column("decision", sa.String(length=24), nullable=False),
        sa.Column("evaluation_ref", sa.String(length=64), nullable=True),
        sa.Column("receipt_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["request_ref"], ["owner_human_requests.request_ref"]
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_ref"], ["owner_human_request_evaluations.evaluation_ref"]
        ),
        _hash("receipt_hash"),
        sa.CheckConstraint(
            "decision IN ('satisfied', 'declined', 'withdrawn', 'expired', "
            "'superseded')"
        ),
    )

    op.create_table(
        "owner_human_request_resume_validations",
        sa.Column("validation_ref", sa.String(length=64), primary_key=True),
        sa.Column("request_ref", sa.String(length=96), nullable=False),
        sa.Column("waiter_ref", sa.String(length=128), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("target_assertion_hash", sa.String(length=64), nullable=False),
        sa.Column("authorization_receipt_ref", sa.String(length=64), nullable=True),
        sa.Column("other_blockers_json", sa.Text(), nullable=False),
        sa.Column("other_blockers_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason_code", sa.String(length=96), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["request_ref", "waiter_ref"],
            ["owner_human_request_waiters.request_ref", "owner_human_request_waiters.waiter_ref"],
        ),
        _hash("target_assertion_hash"),
        _hash("other_blockers_hash"),
        sa.CheckConstraint("generation >= 1"),
        sa.CheckConstraint("status IN ('blocked', 'released')"),
    )
    op.create_index(
        "ix_owner_human_request_resume_waiter",
        "owner_human_request_resume_validations",
        ["request_ref", "waiter_ref", "created_at"],
    )

    op.create_table(
        "owner_human_request_resume_consumptions",
        sa.Column("consumption_ref", sa.String(length=64), primary_key=True),
        sa.Column("request_ref", sa.String(length=96), nullable=False),
        sa.Column("waiter_ref", sa.String(length=128), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("validation_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("work_ref", sa.String(length=128), nullable=False),
        sa.Column("work_hash", sa.String(length=64), nullable=False),
        sa.Column("receipt_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["request_ref", "waiter_ref"],
            ["owner_human_request_waiters.request_ref", "owner_human_request_waiters.waiter_ref"],
        ),
        sa.ForeignKeyConstraint(
            ["validation_ref"],
            ["owner_human_request_resume_validations.validation_ref"],
        ),
        sa.UniqueConstraint("request_ref", "waiter_ref", "generation"),
        _hash("work_hash"),
        _hash("receipt_hash"),
        sa.CheckConstraint("generation >= 1"),
        sa.CheckConstraint("consumption_ref != receipt_ref"),
    )

    op.create_table(
        "owner_human_request_commands",
        sa.Column("issuer", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("command_kind", sa.String(length=32), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("result_ref", sa.String(length=96), nullable=False),
        sa.Column("recorded_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("issuer", "idempotency_key"),
        _hash("request_hash"),
    )

    op.create_table(
        "hc_human_request_responses",
        sa.Column("response_ref", sa.String(length=64), primary_key=True),
        sa.Column("request_ref", sa.String(length=96), nullable=False),
        sa.Column("issuer", sa.String(length=32), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("request_revision", sa.Integer(), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("facts_json", sa.Text(), nullable=False),
        sa.Column("facts_hash", sa.String(length=64), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("receipt_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False, unique=True),
        sa.Column("command_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        _hash("facts_hash"),
        _hash("receipt_hash"),
        _hash("command_hash"),
        sa.CheckConstraint("request_revision >= 1"),
        sa.CheckConstraint("decision IN ('provided', 'declined', 'deferred')"),
    )
    op.create_index(
        "ix_hc_human_request_responses_request",
        "hc_human_request_responses",
        ["request_ref", "created_at"],
    )

    op.create_table(
        "hc_companion_sessions",
        sa.Column("session_ref", sa.String(length=64), primary_key=True),
        sa.Column("scope_ref", sa.String(length=128), nullable=False, unique=True),
        sa.Column("native_session_ref", sa.String(length=256), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.CheckConstraint("status IN ('open', 'closed')"),
    )
    op.create_table(
        "hc_companion_turns",
        sa.Column("interaction_ref", sa.String(length=64), primary_key=True),
        sa.Column("session_ref", sa.String(length=64), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("message_hash", sa.String(length=64), nullable=False),
        sa.Column("assistant_status", sa.String(length=20), nullable=False),
        sa.Column("assistant_content", sa.Text(), nullable=True),
        sa.Column("assistant_content_hash", sa.String(length=64), nullable=True),
        sa.Column("adapter_kind", sa.String(length=64), nullable=True),
        sa.Column("reason_code", sa.String(length=96), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False, unique=True),
        sa.Column("command_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_ref"], ["hc_companion_sessions.session_ref"]
        ),
        sa.UniqueConstraint("session_ref", "ordinal"),
        _hash("message_hash"),
        _hash("command_hash"),
        sa.CheckConstraint("ordinal >= 1"),
        sa.CheckConstraint("attempt_count >= 0"),
        sa.CheckConstraint(
            "assistant_status IN ('queued', 'processing', 'completed', 'failed')"
        ),
        sa.CheckConstraint(
            "(assistant_status IN ('queued', 'processing') AND "
            "assistant_content IS NULL AND assistant_content_hash IS NULL) OR "
            "(assistant_status = 'completed' AND assistant_content IS NOT NULL "
            "AND assistant_content_hash IS NOT NULL AND "
            "length(assistant_content_hash) = 64 AND reason_code IS NULL) OR "
            "(assistant_status = 'failed' AND assistant_content IS NULL AND "
            "assistant_content_hash IS NULL AND reason_code IS NOT NULL)"
        ),
    )
    op.create_index(
        "ix_hc_companion_turns_pending",
        "hc_companion_turns",
        ["assistant_status", "created_at"],
    )

    op.create_table(
        "hc_agent_proposals",
        sa.Column("proposal_ref", sa.String(length=64), primary_key=True),
        sa.Column("scope_ref", sa.String(length=128), nullable=False),
        sa.Column("proposal_json", sa.Text(), nullable=False),
        sa.Column("proposal_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False, unique=True),
        sa.Column("command_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        _hash("proposal_hash"),
        _hash("command_hash"),
        sa.CheckConstraint("status IN ('proposed', 'converted', 'dismissed')"),
    )

    op.create_table(
        "hc_soft_constraints",
        sa.Column("constraint_ref", sa.String(length=64), primary_key=True),
        sa.Column("scope_ref", sa.String(length=128), nullable=False),
        sa.Column("source_proposal_ref", sa.String(length=64), nullable=True, unique=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("guidance_json", sa.Text(), nullable=False),
        sa.Column("guidance_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("receipt_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "idempotency_key", sa.String(length=128), nullable=False, unique=True
        ),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_proposal_ref"], ["hc_agent_proposals.proposal_ref"]
        ),
        _hash("guidance_hash"),
        _hash("receipt_hash"),
        sa.CheckConstraint("revision >= 1"),
        sa.CheckConstraint(
            "status IN ('active', 'withdrawn', 'expired', 'superseded')"
        ),
    )
    op.create_index(
        "ix_hc_soft_constraints_scope",
        "hc_soft_constraints",
        ["scope_ref", "status", "created_at"],
    )

    op.create_table(
        "hc_command_intents",
        sa.Column("intent_id", sa.String(length=64), primary_key=True),
        sa.Column("scope_ref", sa.String(length=128), nullable=False),
        sa.Column("source_proposal_ref", sa.String(length=64), nullable=True, unique=True),
        sa.Column("current_revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_proposal_ref"], ["hc_agent_proposals.proposal_ref"]
        ),
        sa.CheckConstraint("current_revision >= 1"),
        sa.CheckConstraint("status IN ('draft', 'previewed', 'confirmed', 'cancelled')"),
    )
    op.create_table(
        "hc_command_drafts",
        sa.Column("intent_id", sa.String(length=64), nullable=False),
        sa.Column("draft_revision", sa.Integer(), nullable=False),
        sa.Column("draft_json", sa.Text(), nullable=False),
        sa.Column("draft_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["intent_id"], ["hc_command_intents.intent_id"]),
        sa.PrimaryKeyConstraint("intent_id", "draft_revision"),
        _hash("draft_hash"),
        sa.CheckConstraint("draft_revision >= 1"),
    )
    op.create_table(
        "hc_command_previews",
        sa.Column("preview_ref", sa.String(length=64), primary_key=True),
        sa.Column("intent_id", sa.String(length=64), nullable=False),
        sa.Column("draft_revision", sa.Integer(), nullable=False),
        sa.Column("draft_hash", sa.String(length=64), nullable=False),
        sa.Column("owner_previews_json", sa.Text(), nullable=False),
        sa.Column("owner_previews_hash", sa.String(length=64), nullable=False),
        sa.Column("owner_revisions_json", sa.Text(), nullable=False),
        sa.Column("owner_revisions_hash", sa.String(length=64), nullable=False),
        sa.Column("preview_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["intent_id"], ["hc_command_intents.intent_id"]),
        _hash("draft_hash"),
        _hash("owner_previews_hash"),
        _hash("owner_revisions_hash"),
        _hash("preview_hash"),
        sa.CheckConstraint("draft_revision >= 1"),
        sa.CheckConstraint("status IN ('current', 'stale', 'consumed')"),
    )
    op.create_index(
        "ix_hc_command_previews_intent",
        "hc_command_previews",
        ["intent_id", "created_at"],
    )
    op.create_table(
        "hc_command_confirmations",
        sa.Column("confirmation_ref", sa.String(length=64), primary_key=True),
        sa.Column("intent_id", sa.String(length=64), nullable=False, unique=True),
        sa.Column("draft_revision", sa.Integer(), nullable=False),
        sa.Column("draft_hash", sa.String(length=64), nullable=False),
        sa.Column("preview_ref", sa.String(length=64), nullable=False),
        sa.Column("preview_hash", sa.String(length=64), nullable=False),
        sa.Column("receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["intent_id"], ["hc_command_intents.intent_id"]),
        sa.ForeignKeyConstraint(["preview_ref"], ["hc_command_previews.preview_ref"]),
        _hash("draft_hash"),
        _hash("preview_hash"),
        _hash("receipt_hash"),
        sa.CheckConstraint("draft_revision >= 1"),
    )
    op.create_table(
        "hc_collaboration_commands",
        sa.Column("idempotency_key", sa.String(length=128), primary_key=True),
        sa.Column("command_kind", sa.String(length=32), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("result_ref", sa.String(length=96), nullable=False),
        sa.Column("recorded_at", sa.Float(), nullable=False),
        _hash("request_hash"),
    )

    op.create_table(
        "hc_legacy_broad_authorization_bases",
        sa.Column("initialization_id", sa.String(length=64), primary_key=True),
        sa.Column("preview_ref", sa.String(length=64), nullable=False),
        sa.Column("preview_hash", sa.String(length=64), nullable=False),
        sa.Column("confirmation_ref", sa.String(length=64), nullable=False),
        sa.Column("confirmation_hash", sa.String(length=64), nullable=False),
        sa.Column("basis_kind", sa.String(length=64), nullable=False),
        sa.Column("policy_schema_ref", sa.String(length=96), nullable=False),
        sa.Column("registered_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["initialization_id"], ["hc_quest_initializations.initialization_id"]
        ),
        sa.UniqueConstraint("preview_ref"),
        sa.UniqueConstraint("confirmation_ref"),
        _hash("preview_hash"),
        _hash("confirmation_hash"),
        sa.CheckConstraint(
            "basis_kind = 'legacy_implicit_quest_confirmation_policy'"
        ),
        sa.CheckConstraint(
            "policy_schema_ref = 'meta-research/trusted-local-broad/v1'"
        ),
    )
    _register_legacy_broad_authorization_bases()

    op.create_table(
        "ar_acquisition_resume_routes",
        sa.Column("route_ref", sa.String(length=64), primary_key=True),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("session_ref", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("paper_id", sa.String(length=128), nullable=False),
        sa.Column("item_hash", sa.String(length=64), nullable=False),
        sa.Column("human_request_ref", sa.String(length=96), nullable=False),
        sa.Column("response_ref", sa.String(length=64), nullable=False),
        sa.Column("evaluation_ref", sa.String(length=64), nullable=False),
        sa.Column("consumption_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column(
            "consumption_receipt_ref", sa.String(length=64), nullable=False, unique=True
        ),
        sa.Column("consumption_receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("effective_mode", sa.String(length=24), nullable=False),
        sa.Column("route_json", sa.Text(), nullable=False),
        sa.Column("route_hash", sa.String(length=64), nullable=False),
        sa.Column("receipt_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["request_id"], ["ar_acquisition_requests.request_id"]
        ),
        sa.ForeignKeyConstraint(
            ["human_request_ref"], ["owner_human_requests.request_ref"]
        ),
        sa.ForeignKeyConstraint(
            ["response_ref"], ["hc_human_request_responses.response_ref"]
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_ref"],
            ["owner_human_request_evaluations.evaluation_ref"],
        ),
        sa.ForeignKeyConstraint(
            ["consumption_ref"],
            ["owner_human_request_resume_consumptions.consumption_ref"],
        ),
        sa.UniqueConstraint("request_id", "attempt_no"),
        sa.CheckConstraint("attempt_no >= 2"),
        _hash("request_hash"),
        _hash("item_hash"),
        _hash("consumption_receipt_hash"),
        _hash("route_hash"),
        _hash("receipt_hash"),
        sa.CheckConstraint(
            "effective_mode IN ('oa_then_institution', 'oa_only', 'provided_only')"
        ),
        sa.CheckConstraint("receipt_ref != consumption_receipt_ref"),
    )

    op.create_table(
        "hc_capability_authorizations",
        sa.Column("authorization_ref", sa.String(length=64), primary_key=True),
        sa.Column("scope_ref", sa.String(length=128), nullable=False),
        sa.Column("authorization_kind", sa.String(length=24), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("initialization_id", sa.String(length=64), nullable=True),
        sa.Column("capability", sa.String(length=64), nullable=True),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("target_assertion_json", sa.Text(), nullable=False),
        sa.Column("target_assertion_hash", sa.String(length=64), nullable=False),
        sa.Column("requirement_json", sa.Text(), nullable=False),
        sa.Column("requirement_hash", sa.String(length=64), nullable=False),
        sa.Column("policy_schema_ref", sa.String(length=96), nullable=False),
        sa.Column("policy_json", sa.Text(), nullable=False),
        sa.Column("policy_hash", sa.String(length=64), nullable=False),
        sa.Column("basis_preview_ref", sa.String(length=64), nullable=True),
        sa.Column("basis_preview_hash", sa.String(length=64), nullable=True),
        sa.Column("basis_confirmation_ref", sa.String(length=64), nullable=False),
        sa.Column("basis_confirmation_hash", sa.String(length=64), nullable=False),
        sa.Column("quest_ref", sa.String(length=64), nullable=True),
        sa.Column("quest_receipt_ref", sa.String(length=64), nullable=True),
        sa.Column("quest_receipt_hash", sa.String(length=64), nullable=True),
        sa.Column("is_current", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("receipt_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False, unique=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.UniqueConstraint(
            "authorization_kind", "scope_ref", "capability", "revision"
        ),
        _hash("target_assertion_hash"),
        _hash("requirement_hash"),
        _hash("policy_hash"),
        _hash("basis_confirmation_hash"),
        _hash("receipt_hash"),
        sa.CheckConstraint("revision >= 1"),
        sa.CheckConstraint("is_current IN (0, 1)"),
        sa.CheckConstraint("length(policy_schema_ref) > 0"),
        sa.CheckConstraint(
            "authorization_ref != receipt_ref AND "
            "receipt_ref != basis_confirmation_ref"
        ),
        sa.CheckConstraint(
            "quest_receipt_ref IS NULL OR receipt_ref != quest_receipt_ref"
        ),
        sa.CheckConstraint(
            "authorization_kind IN ('broad_research', 'capability')"
        ),
        sa.CheckConstraint("decision IN ('granted', 'denied', 'revoked')"),
        sa.CheckConstraint("status IN ('granted', 'denied', 'revoked')"),
        sa.CheckConstraint("status = decision"),
        sa.CheckConstraint(
            "(basis_preview_ref IS NULL AND basis_preview_hash IS NULL) OR "
            "(basis_preview_ref IS NOT NULL AND basis_preview_hash IS NOT NULL "
            "AND length(basis_preview_hash) = 64)"
        ),
        sa.CheckConstraint(
            "(quest_receipt_ref IS NULL AND quest_receipt_hash IS NULL) OR "
            "(quest_receipt_ref IS NOT NULL AND quest_receipt_hash IS NOT NULL "
            "AND length(quest_receipt_hash) = 64)"
        ),
        sa.CheckConstraint(
            "(authorization_kind = 'broad_research' AND capability IS NULL "
            "AND initialization_id IS NOT NULL AND quest_ref IS NOT NULL "
            "AND basis_preview_ref IS NOT NULL AND basis_preview_hash IS NOT NULL "
            "AND quest_receipt_ref IS NOT NULL AND quest_receipt_hash IS NOT NULL) OR "
            "(authorization_kind = 'capability' AND capability IS NOT NULL)"
        ),
    )
    op.create_index(
        "ix_hc_capability_authorizations_scope",
        "hc_capability_authorizations",
        ["scope_ref", "capability", "created_at"],
    )
    op.create_index(
        "uq_hc_broad_authorization_current_quest",
        "hc_capability_authorizations",
        ["quest_ref"],
        unique=True,
        sqlite_where=sa.text(
            "authorization_kind = 'broad_research' AND is_current = 1"
        ),
    )
    op.create_index(
        "uq_hc_capability_authorization_current_scope",
        "hc_capability_authorizations",
        ["scope_ref", "capability"],
        unique=True,
        sqlite_where=sa.text(
            "authorization_kind = 'capability' AND is_current = 1"
        ),
    )
    _mark_completed_authorizations_missing()


def downgrade() -> None:
    _restore_hc_reconciliation_steps()
    op.drop_table("ar_acquisition_resume_routes")
    op.drop_index(
        "uq_hc_capability_authorization_current_scope",
        table_name="hc_capability_authorizations",
    )
    op.drop_index(
        "uq_hc_broad_authorization_current_quest",
        table_name="hc_capability_authorizations",
    )
    op.drop_index(
        "ix_hc_capability_authorizations_scope",
        table_name="hc_capability_authorizations",
    )
    op.drop_table("hc_capability_authorizations")
    op.drop_table("hc_legacy_broad_authorization_bases")
    op.drop_table("hc_collaboration_commands")
    op.drop_table("hc_command_confirmations")
    op.drop_index(
        "ix_hc_command_previews_intent", table_name="hc_command_previews"
    )
    op.drop_table("hc_command_previews")
    op.drop_table("hc_command_drafts")
    op.drop_table("hc_command_intents")
    op.drop_index(
        "ix_hc_soft_constraints_scope", table_name="hc_soft_constraints"
    )
    op.drop_table("hc_soft_constraints")
    op.drop_table("hc_agent_proposals")
    op.drop_index(
        "ix_hc_companion_turns_pending", table_name="hc_companion_turns"
    )
    op.drop_table("hc_companion_turns")
    op.drop_table("hc_companion_sessions")
    op.drop_index(
        "ix_hc_human_request_responses_request",
        table_name="hc_human_request_responses",
    )
    op.drop_table("hc_human_request_responses")
    op.drop_table("owner_human_request_commands")
    op.drop_table("owner_human_request_resume_consumptions")
    op.drop_index(
        "ix_owner_human_request_resume_waiter",
        table_name="owner_human_request_resume_validations",
    )
    op.drop_table("owner_human_request_resume_validations")
    op.drop_table("owner_human_request_dispositions")
    op.drop_table("owner_human_request_evaluations")
    op.drop_table("owner_human_request_waiters")
    op.drop_index(
        "ix_owner_human_requests_current", table_name="owner_human_requests"
    )
    op.drop_index(
        "ix_owner_human_requests_identity", table_name="owner_human_requests"
    )
    op.drop_table("owner_human_requests")
    for column in (
        "soft_constraint_count",
        "pending_companion_turn_count",
        "companion_session_count",
        "human_response_count",
    ):
        op.drop_column("human_collaboration_state", column)
    for table in (
        "advancement_engine_state",
        "agent_runtime_state",
        "research_memory_state",
        "research_graph_state",
    ):
        op.drop_column(table, "human_request_count")


def _create_reconciliation_checkpoints(steps: tuple[str, ...]) -> None:
    op.create_table(
        "hc_reconciliation_checkpoints",
        sa.Column("initialization_id", sa.String(length=64), primary_key=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("first_missing_step", sa.String(length=32), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(length=96), nullable=True),
        sa.Column("next_retry_at", sa.Float(), nullable=True),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["initialization_id"],
            ["hc_quest_initializations.initialization_id"],
        ),
        sa.CheckConstraint("state IN ('idle', 'partial', 'recovering', 'completed')"),
        sa.CheckConstraint(
            "first_missing_step IS NULL OR "
            + _step_expression("first_missing_step", steps)
        ),
        sa.CheckConstraint("attempt_count >= 0"),
        sa.CheckConstraint(
            "(state IN ('idle', 'completed') AND first_missing_step IS NULL "
            "AND reason_code IS NULL AND next_retry_at IS NULL) OR "
            "(state IN ('partial', 'recovering') AND first_missing_step IS NOT "
            "NULL AND reason_code IS NOT NULL)"
        ),
    )


def _create_reconciliation_attempts(steps: tuple[str, ...]) -> None:
    op.create_table(
        "hc_reconciliation_attempts",
        sa.Column("attempt_ref", sa.String(length=64), primary_key=True),
        sa.Column("initialization_id", sa.String(length=64), nullable=False),
        sa.Column("step", sa.String(length=32), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(length=24), nullable=False),
        sa.Column("reason_code", sa.String(length=96), nullable=True),
        sa.Column("started_at", sa.Float(), nullable=False),
        sa.Column("finished_at", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(
            ["initialization_id"],
            ["hc_quest_initializations.initialization_id"],
        ),
        sa.UniqueConstraint("initialization_id", "step", "attempt_number"),
        _step_check("step", steps),
        sa.CheckConstraint("attempt_number >= 1"),
        sa.CheckConstraint(
            "outcome IN ('started', 'accepted', 'transient_failure', 'rejected', "
            "'stale')"
        ),
        sa.CheckConstraint(
            "(outcome = 'started' AND reason_code IS NULL AND finished_at IS NULL) "
            "OR (outcome = 'accepted' AND reason_code IS NULL AND finished_at IS "
            "NOT NULL) OR (outcome IN ('transient_failure', 'rejected', 'stale') "
            "AND reason_code IS NOT NULL AND finished_at IS NOT NULL)"
        ),
    )


def _extend_hc_reconciliation_steps() -> None:
    connection = op.get_bind()
    op.rename_table(
        "hc_reconciliation_checkpoints",
        "hc_reconciliation_checkpoints_pre_human_collaboration",
    )
    _create_reconciliation_checkpoints(_HC_RECONCILIATION_STEPS)
    connection.execute(
        sa.text(
            "INSERT INTO hc_reconciliation_checkpoints SELECT * FROM "
            "hc_reconciliation_checkpoints_pre_human_collaboration"
        )
    )
    op.drop_table("hc_reconciliation_checkpoints_pre_human_collaboration")
    op.create_index(
        "ix_hc_reconciliation_checkpoints_due",
        "hc_reconciliation_checkpoints",
        ["state", "next_retry_at"],
    )

    op.rename_table(
        "hc_reconciliation_attempts",
        "hc_reconciliation_attempts_pre_human_collaboration",
    )
    _create_reconciliation_attempts(_HC_RECONCILIATION_STEPS)
    connection.execute(
        sa.text(
            "INSERT INTO hc_reconciliation_attempts SELECT * FROM "
            "hc_reconciliation_attempts_pre_human_collaboration"
        )
    )
    op.drop_table("hc_reconciliation_attempts_pre_human_collaboration")


def _mark_completed_authorizations_missing() -> None:
    """Schedule recovery without manufacturing a historical authorization fact."""

    connection = op.get_bind()
    connection.execute(
        sa.text(
            "INSERT INTO hc_reconciliation_checkpoints "
            "(initialization_id, state, first_missing_step, attempt_count, "
            "reason_code, next_retry_at, updated_at) SELECT initialization_id, "
            "'partial', 'broad_research_authorization', 0, "
            "'broad_research_authorization_missing', 0, updated_at FROM "
            "hc_quest_initializations WHERE status = 'completed' "
            "ON CONFLICT(initialization_id) DO UPDATE SET state = 'partial', "
            "first_missing_step = 'broad_research_authorization', reason_code = "
            "'broad_research_authorization_missing', next_retry_at = 0"
        )
    )


def _register_legacy_broad_authorization_bases() -> None:
    """Record which pre-0009 confirmations may use the compatibility policy."""

    connection = op.get_bind()
    connection.execute(
        sa.text(
            "INSERT INTO hc_legacy_broad_authorization_bases "
            "(initialization_id, preview_ref, preview_hash, confirmation_ref, "
            "confirmation_hash, basis_kind, policy_schema_ref, registered_at) "
            "SELECT initialization_id, confirmed_preview_ref, "
            "confirmed_preview_hash, confirmation_ref, confirmation_hash, "
            "'legacy_implicit_quest_confirmation_policy', "
            "'meta-research/trusted-local-broad/v1', "
            "CAST(strftime('%s', 'now') AS REAL) FROM hc_quest_initializations "
            "WHERE confirmation_ref IS NOT NULL AND confirmation_hash IS NOT NULL "
            "AND confirmed_preview_ref IS NOT NULL AND confirmed_preview_hash IS "
            "NOT NULL"
        )
    )


def _restore_hc_reconciliation_steps() -> None:
    """Restore the pre-0009 checks while retaining representable recovery history."""

    connection = op.get_bind()
    op.rename_table(
        "hc_reconciliation_checkpoints",
        "hc_reconciliation_checkpoints_with_human_collaboration",
    )
    _create_reconciliation_checkpoints(_PRE_0009_RECONCILIATION_STEPS)
    connection.execute(
        sa.text(
            "INSERT INTO hc_reconciliation_checkpoints "
            "(initialization_id, state, first_missing_step, attempt_count, "
            "reason_code, next_retry_at, updated_at) SELECT checkpoints.initialization_id, "
            "CASE WHEN checkpoints.first_missing_step IN "
            "('broad_research_authorization', 'acquisition_session') THEN "
            "CASE WHEN initializations.status = 'completed' THEN 'completed' ELSE "
            "'idle' END ELSE checkpoints.state END, CASE WHEN "
            "checkpoints.first_missing_step IN ('broad_research_authorization', "
            "'acquisition_session') THEN NULL ELSE checkpoints.first_missing_step END, "
            "checkpoints.attempt_count, CASE WHEN checkpoints.first_missing_step IN "
            "('broad_research_authorization', 'acquisition_session') THEN NULL ELSE "
            "checkpoints.reason_code END, CASE WHEN checkpoints.first_missing_step IN "
            "('broad_research_authorization', 'acquisition_session') THEN NULL ELSE "
            "checkpoints.next_retry_at END, checkpoints.updated_at FROM "
            "hc_reconciliation_checkpoints_with_human_collaboration AS checkpoints "
            "JOIN hc_quest_initializations AS initializations ON "
            "initializations.initialization_id = checkpoints.initialization_id"
        )
    )
    op.drop_table("hc_reconciliation_checkpoints_with_human_collaboration")
    op.create_index(
        "ix_hc_reconciliation_checkpoints_due",
        "hc_reconciliation_checkpoints",
        ["state", "next_retry_at"],
    )

    op.rename_table(
        "hc_reconciliation_attempts",
        "hc_reconciliation_attempts_with_human_collaboration",
    )
    _create_reconciliation_attempts(_PRE_0009_RECONCILIATION_STEPS)
    connection.execute(
        sa.text(
            "INSERT INTO hc_reconciliation_attempts SELECT * FROM "
            "hc_reconciliation_attempts_with_human_collaboration WHERE step NOT IN "
            "('broad_research_authorization', 'acquisition_session')"
        )
    )
    op.drop_table("hc_reconciliation_attempts_with_human_collaboration")
