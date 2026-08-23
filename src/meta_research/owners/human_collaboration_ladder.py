from __future__ import annotations

import json
import re
import time
from typing import Callable, cast

from sqlalchemy import text

from meta_research.control_contract import validate_control_payload
from meta_research.database import Database
from meta_research.feed import DurableFeed
from meta_research.owners.common import (
    AcceptanceReceipt,
    OwnerConflict,
    canonical_hash,
    canonical_json,
    decoded_object,
    new_ref,
)
from meta_research.owners.secret_detection import contains_secret
from meta_research.quest_drafting import (
    DraftingUnavailable,
    INTENT_MESSAGE_MAX_LENGTH,
    INTENT_REPLY_MAX_LENGTH,
    IntentDraftingProvider,
    IntentTurnRequest,
)


HC_OWNER = "human_collaboration"
GUIDANCE_RECEIPT_SCHEMA = "meta-research/soft-constraint-receipt/v1"
COMMAND_CONFIRMATION_SCHEMA = "meta-research/human-confirmation-receipt/v1"
AUTHORIZATION_RECEIPT_SCHEMA = "meta-research/capability-authorization-receipt/v1"

BROAD_RESEARCH_POLICY = {
    "schema_ref": "meta-research/trusted-local-quest-policy/v1",
    "ordinary_reversible_local_research": (
        "allowed_without_additional_confirmation"
    ),
    "requires_new_confirmation": [
        "scope_expansion",
        "external_publish_or_send",
        "irreversible_operation",
        "delete_or_destruct_user_data",
        "high_risk_operation",
    ],
    "exclusions": [
        "scope_expansion",
        "external_effect",
        "irreversible_effect",
        "destructive_effect",
        "high_risk_effect",
    ],
}
LEGACY_BROAD_RESEARCH_POLICY = {
    **BROAD_RESEARCH_POLICY,
    "schema_ref": "meta-research/trusted-local-broad/v1",
    "basis_kind": "legacy_implicit_quest_confirmation_policy",
}

_HARD_COMMAND_KINDS = {
    "scope_expansion",
    "external_publish",
    "external_send",
    "irreversible_operation",
    "delete_user_data",
    "high_risk_operation",
    "capability_expansion",
    "capability_authorization",
    "research_control",
}
_MAX_PENDING_COMPANION_TURNS = 64


def broad_research_target_assertion(
    *,
    initialization_id: str,
    draft: dict[str, object],
    resource_envelope: dict[str, object] | None,
) -> dict[str, object]:
    """Build the exact HC-owned policy assertion shown before confirmation."""

    return _broad_research_target_assertion(
        initialization_id=initialization_id,
        draft=draft,
        resource_envelope=resource_envelope,
        policy_template=BROAD_RESEARCH_POLICY,
        basis_kind="explicit_confirmation_preview",
    )


def legacy_broad_research_target_assertion(
    *,
    initialization_id: str,
    draft: dict[str, object],
    resource_envelope: dict[str, object] | None,
) -> dict[str, object]:
    """Represent the broad policy already implicit in pre-0009 confirmations."""

    return _broad_research_target_assertion(
        initialization_id=initialization_id,
        draft=draft,
        resource_envelope=resource_envelope,
        policy_template=LEGACY_BROAD_RESEARCH_POLICY,
        basis_kind="legacy_implicit_quest_confirmation_policy",
    )


def _broad_research_target_assertion(
    *,
    initialization_id: str,
    draft: dict[str, object],
    resource_envelope: dict[str, object] | None,
    policy_template: dict[str, object],
    basis_kind: str,
) -> dict[str, object]:

    envelope_binding = {
        "resource_envelope_ref": draft.get("resource_envelope_ref"),
        "resource_envelope_hash": draft.get("resource_envelope_hash"),
        "time_budget": draft.get("time_budget"),
        "hard_ceiling": (
            resource_envelope.get("hard_ceiling")
            if resource_envelope is not None
            else None
        ),
    }
    policy = {**policy_template, "resource_envelope": envelope_binding}
    assertion = {
        "owner": HC_OWNER,
        "operation": "issue_broad_research_authorization",
        "may_change": ["quest_capability_authorization"],
        "will_not_change": [
            "quest_identity",
            "owner_acceptances",
            "runtime_binding",
            "resource_hard_ceiling",
        ],
        "preconditions": [
            "exact_human_confirmation",
            "exact_quest_receipt",
        ],
        "risks": ["authorization_remains_missing_if_independent_commit_fails"],
        "stale_if": [
            "quest_draft_revision_changes",
            "default_policy_changes",
            "resource_hard_ceiling_changes",
        ],
        "bindings": {
            "initialization_id": initialization_id,
            "basis_kind": basis_kind,
            "policy": policy,
            "policy_hash": canonical_hash(policy),
            **envelope_binding,
        },
    }
    return {**assertion, "target_hash": canonical_hash(assertion)}


class SQLiteHumanCollaborationLadder:
    """HC's durable interaction ladder behind its single public Interface."""

    def __init__(
        self,
        database: Database,
        feed: DurableFeed,
        drafting_provider: IntentDraftingProvider,
        context_resolver: Callable[[str], dict[str, object]] | None = None,
        control_preview_resolver: Callable[
            [str, dict[str, object]],
            tuple[list[dict[str, object]], dict[str, int]],
        ]
        | None = None,
    ) -> None:
        self._database = database
        self._feed = feed
        self._drafting_provider = drafting_provider
        self._context_resolver = context_resolver
        self._control_preview_resolver = control_preview_resolver
        with self._database.write() as connection:
            recovered = connection.execute(
                text(
                    "UPDATE hc_companion_turns SET assistant_status = 'queued', "
                    "updated_at = :now WHERE assistant_status = 'processing'"
                ),
                {"now": time.time()},
            )
            if recovered.rowcount:
                connection.execute(
                    text(
                        "UPDATE human_collaboration_state SET "
                        "pending_companion_turn_count = (SELECT COUNT(*) FROM "
                        "hc_companion_turns WHERE assistant_status = 'queued'), "
                        "revision = revision + 1 WHERE "
                        "singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "human_collaboration.companion_turns_recovered",
                    {"recovered_turn_count": int(recovered.rowcount)},
                )

    def send_companion_message(
        self,
        scope_ref: str,
        message: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        scope_ref = _scope_ref(scope_ref, "companion_scope_required")
        message = _text(message, "companion_message_required", INTENT_MESSAGE_MAX_LENGTH)
        _reject_secret_content(message)
        _idempotency_key(idempotency_key)
        command_hash = canonical_hash(
            {
                "command": "send_companion_message",
                "scope_ref": scope_ref,
                "message": message,
            }
        )
        with self._database.write() as connection:
            existing = connection.execute(
                text(
                    "SELECT * FROM hc_companion_turns WHERE idempotency_key = "
                    ":idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
            if existing is not None:
                if existing.command_hash != command_hash:
                    raise OwnerConflict("idempotency_conflict")
                interaction_ref = existing.interaction_ref
            else:
                pending_count = int(
                    connection.execute(
                        text(
                            "SELECT COUNT(*) FROM hc_companion_turns WHERE "
                            "assistant_status IN ('queued', 'processing')"
                        )
                    ).scalar_one()
                )
                if pending_count >= _MAX_PENDING_COMPANION_TURNS:
                    raise OwnerConflict("companion_queue_full")
                session = connection.execute(
                    text(
                        "SELECT * FROM hc_companion_sessions WHERE scope_ref = :scope_ref"
                    ),
                    {"scope_ref": scope_ref},
                ).first()
                now = time.time()
                created_session = session is None
                if session is None:
                    session_ref = new_ref("companion_session")
                    connection.execute(
                        text(
                            "INSERT INTO hc_companion_sessions (session_ref, scope_ref, "
                            "status, created_at, updated_at) VALUES (:session_ref, "
                            ":scope_ref, 'open', :now, :now)"
                        ),
                        {"session_ref": session_ref, "scope_ref": scope_ref, "now": now},
                    )
                else:
                    if session.status != "open":
                        raise OwnerConflict("companion_session_closed")
                    session_ref = session.session_ref
                ordinal = int(
                    connection.execute(
                        text(
                            "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM "
                            "hc_companion_turns WHERE session_ref = :session_ref"
                        ),
                        {"session_ref": session_ref},
                    ).scalar_one()
                )
                interaction_ref = new_ref("interaction")
                connection.execute(
                    text(
                        "INSERT INTO hc_companion_turns (interaction_ref, session_ref, "
                        "ordinal, message, message_hash, assistant_status, "
                        "attempt_count, idempotency_key, command_hash, created_at, "
                        "updated_at) VALUES (:interaction_ref, :session_ref, :ordinal, "
                        ":message, :message_hash, 'queued', 0, :idempotency_key, "
                        ":command_hash, :now, :now)"
                    ),
                    {
                        "interaction_ref": interaction_ref,
                        "session_ref": session_ref,
                        "ordinal": ordinal,
                        "message": message,
                        "message_hash": canonical_hash(message),
                        "idempotency_key": idempotency_key,
                        "command_hash": command_hash,
                        "now": now,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE human_collaboration_state SET revision = revision + 1, "
                        "companion_session_count = companion_session_count + "
                        ":created_session, pending_companion_turn_count = "
                        "pending_companion_turn_count + 1 WHERE singleton = 'owner'"
                    ),
                    {"created_session": 1 if created_session else 0},
                )
                self._feed.record(
                    connection,
                    "human_collaboration.companion_message_queued",
                    {
                        "scope_ref": scope_ref,
                        "session_ref": session_ref,
                        "interaction_ref": interaction_ref,
                    },
                )
        return self._query_turn(interaction_ref)

    def process_drafting_once(self) -> bool:
        with self._database.write() as connection:
            row = connection.execute(
                text(
                    "SELECT turns.*, sessions.scope_ref, sessions.native_session_ref "
                    "FROM hc_companion_turns AS turns JOIN hc_companion_sessions AS "
                    "sessions ON sessions.session_ref = turns.session_ref WHERE "
                    "turns.assistant_status = 'queued' ORDER BY turns.created_at LIMIT 1"
                )
            ).first()
            if row is None:
                return False
            updated = connection.execute(
                text(
                    "UPDATE hc_companion_turns SET assistant_status = 'processing', "
                    "attempt_count = attempt_count + 1, updated_at = :now WHERE "
                    "interaction_ref = :interaction_ref AND assistant_status = 'queued'"
                ),
                {"interaction_ref": row.interaction_ref, "now": time.time()},
            )
            if not updated.rowcount:
                return False
        job_ref = f"{row.interaction_ref}:claim:1"
        try:
            context = (
                {
                    "schema_ref": "meta-research/companion-context/v1",
                    "scope_ref": row.scope_ref,
                }
                if self._context_resolver is None
                else self._context_resolver(row.scope_ref)
            )
            context = _document(context, "companion_context_invalid")
            _reject_secret_content(context)
            draft = {
                "interaction_kind": "conversation",
                "scope_ref": row.scope_ref,
                "authoritative_effect": False,
                "current_context": context,
            }
            request = IntentTurnRequest(
                initialization_id=row.scope_ref,
                draft_revision=0,
                draft_hash=canonical_hash(draft),
                draft=draft,
                message=row.message,
                native_session_ref=row.native_session_ref,
                job_ref=job_ref,
            )
            result = self._drafting_provider.reply(request)
            reply = _text(
                result.reply,
                "companion_reply_invalid",
                INTENT_REPLY_MAX_LENGTH,
            )
            _reject_secret_content(reply)
            native_session_ref = _text(
                result.native_session_ref,
                "companion_native_session_invalid",
                256,
            )
            adapter_kind = _text(
                result.adapter_kind, "companion_adapter_kind_invalid", 64
            )
            _reject_secret_content(
                {
                    "native_session_ref": native_session_ref,
                    "adapter_kind": adapter_kind,
                }
            )
            agent_proposal = getattr(result, "agent_proposal", None)
            if agent_proposal is not None:
                agent_proposal = _document(
                    agent_proposal, "agent_proposal_invalid"
                )
                _reject_secret_content(agent_proposal)
        except (DraftingUnavailable, OSError, OwnerConflict, ValueError) as error:
            reason_code = (
                error.code
                if isinstance(error, (DraftingUnavailable, OwnerConflict))
                else "companion_provider_unavailable"
            )
            with self._database.write() as connection:
                connection.execute(
                    text(
                        "UPDATE hc_companion_turns SET assistant_status = 'failed', "
                        "reason_code = :reason_code, updated_at = :now WHERE "
                        "interaction_ref = :interaction_ref AND assistant_status = "
                        "'processing'"
                    ),
                    {
                        "interaction_ref": row.interaction_ref,
                        "reason_code": reason_code,
                        "now": time.time(),
                    },
                )
                self._finish_companion_turn(
                    connection,
                    row.interaction_ref,
                    "human_collaboration.companion_reply_failed",
                )
            finish_job = getattr(self._drafting_provider, "finish_job", None)
            if callable(finish_job):
                finish_job(job_ref)
            return True
        with self._database.write() as connection:
            updated = connection.execute(
                text(
                    "UPDATE hc_companion_turns SET assistant_status = 'completed', "
                    "assistant_content = :reply, assistant_content_hash = "
                    ":reply_hash, adapter_kind = :adapter_kind, reason_code = NULL, "
                    "updated_at = :now WHERE interaction_ref = :interaction_ref AND "
                    "assistant_status = 'processing'"
                ),
                {
                    "interaction_ref": row.interaction_ref,
                    "reply": reply,
                    "reply_hash": canonical_hash(reply),
                    "adapter_kind": adapter_kind,
                    "now": time.time(),
                },
            )
            if not updated.rowcount:
                return False
            connection.execute(
                text(
                    "UPDATE hc_companion_sessions SET native_session_ref = "
                    ":native_session_ref, updated_at = :now WHERE session_ref = "
                    ":session_ref"
                ),
                {
                    "native_session_ref": native_session_ref,
                    "now": time.time(),
                    "session_ref": row.session_ref,
                },
            )
            self._finish_companion_turn(
                connection,
                row.interaction_ref,
                "human_collaboration.companion_reply_recorded",
            )
            if agent_proposal is not None:
                proposal_idempotency_key = (
                    f"companion-agent-proposal:{row.interaction_ref}"
                )
                proposal_command_hash = canonical_hash(
                    {
                        "command": "record_agent_proposal",
                        "scope_ref": row.scope_ref,
                        "proposal": agent_proposal,
                        "source_interaction_ref": row.interaction_ref,
                    }
                )
                replay = _collaboration_command(
                    connection,
                    proposal_idempotency_key,
                    "agent_proposal",
                    proposal_command_hash,
                )
                if replay is None:
                    proposal_ref = new_ref("agent_proposal")
                    now = time.time()
                    connection.execute(
                        text(
                            "INSERT INTO hc_agent_proposals (proposal_ref, scope_ref, "
                            "proposal_json, proposal_hash, status, idempotency_key, "
                            "command_hash, created_at) VALUES (:proposal_ref, "
                            ":scope_ref, :proposal_json, :proposal_hash, 'proposed', "
                            ":idempotency_key, :command_hash, :now)"
                        ),
                        {
                            "proposal_ref": proposal_ref,
                            "scope_ref": row.scope_ref,
                            "proposal_json": canonical_json(agent_proposal),
                            "proposal_hash": canonical_hash(agent_proposal),
                            "idempotency_key": proposal_idempotency_key,
                            "command_hash": proposal_command_hash,
                            "now": now,
                        },
                    )
                    _record_collaboration_command(
                        connection,
                        proposal_idempotency_key,
                        "agent_proposal",
                        proposal_command_hash,
                        proposal_ref,
                    )
                    self._feed.record(
                        connection,
                        "human_collaboration.agent_proposal_recorded",
                        {
                            "proposal_ref": proposal_ref,
                            "scope_ref": row.scope_ref,
                            "source_interaction_ref": row.interaction_ref,
                        },
                    )
        finish_job = getattr(self._drafting_provider, "finish_job", None)
        if callable(finish_job):
            finish_job(job_ref)
        return True

    def _finish_companion_turn(
        self, connection, interaction_ref: str, event_type: str
    ) -> None:
        connection.execute(
            text(
                "UPDATE human_collaboration_state SET revision = revision + 1, "
                "pending_companion_turn_count = CASE WHEN "
                "pending_companion_turn_count > 0 THEN pending_companion_turn_count - 1 "
                "ELSE 0 END WHERE singleton = 'owner'"
            )
        )
        self._feed.record(
            connection, event_type, {"interaction_ref": interaction_ref}
        )

    def query_companion(self, scope_ref: str) -> dict[str, object]:
        scope_ref = _scope_ref(scope_ref, "companion_scope_required")
        with self._database.read() as connection:
            session = connection.execute(
                text(
                    "SELECT * FROM hc_companion_sessions WHERE scope_ref = :scope_ref"
                ),
                {"scope_ref": scope_ref},
            ).first()
            turns = (
                []
                if session is None
                else connection.execute(
                    text(
                        "SELECT * FROM hc_companion_turns WHERE session_ref = "
                        ":session_ref ORDER BY ordinal"
                    ),
                    {"session_ref": session.session_ref},
                ).all()
            )
        return {
            "scope_ref": scope_ref,
            "session_ref": None if session is None else session.session_ref,
            "status": "ready" if session is None else session.status,
            "turns": [_public_turn(item) for item in turns],
        }

    def query_projection(
        self, scope_refs: tuple[str, ...]
    ) -> dict[str, list[dict[str, object]]]:
        """Return a bounded public view for the selected collaboration scopes."""

        if not scope_refs or len(scope_refs) > 101:
            raise OwnerConflict("collaboration_projection_scope_invalid")
        scopes = tuple(
            dict.fromkeys(
                _scope_ref(item, "collaboration_projection_scope_invalid")
                for item in scope_refs
            )
        )
        parameters = {f"scope_{index}": value for index, value in enumerate(scopes)}
        placeholders = ", ".join(f":scope_{index}" for index in range(len(scopes)))
        with self._database.read() as connection:
            proposal_refs = [
                row.proposal_ref
                for row in connection.execute(
                    text(
                        "SELECT proposal_ref FROM hc_agent_proposals WHERE scope_ref "
                        f"IN ({placeholders}) ORDER BY created_at, proposal_ref"
                    ),
                    parameters,
                ).all()
            ]
            constraint_refs = [
                row.constraint_ref
                for row in connection.execute(
                    text(
                        "SELECT constraint_ref FROM hc_soft_constraints WHERE "
                        f"scope_ref IN ({placeholders}) ORDER BY created_at, "
                        "constraint_ref"
                    ),
                    parameters,
                ).all()
            ]
            intent_ids = [
                row.intent_id
                for row in connection.execute(
                    text(
                        "SELECT intent_id FROM hc_command_intents WHERE scope_ref "
                        f"IN ({placeholders}) ORDER BY created_at DESC, "
                        "intent_id DESC LIMIT 100"
                    ),
                    parameters,
                ).all()
            ]
            authorization_refs = [
                row.authorization_ref
                for row in connection.execute(
                    text(
                        "SELECT authorization_ref FROM "
                        "hc_capability_authorizations WHERE is_current = 1 AND "
                        f"scope_ref IN ({placeholders}) ORDER BY created_at DESC, "
                        "authorization_ref DESC LIMIT 100"
                    ),
                    parameters,
                ).all()
            ]
        messages: list[dict[str, object]] = []
        for scope_ref in scopes:
            companion = self.query_companion(scope_ref)
            for turn in cast(list[dict[str, object]], companion["turns"]):
                interaction_ref = cast(str, turn["interaction_ref"])
                messages.append(
                    {
                        "message_ref": f"{interaction_ref}:user",
                        "scope_ref": scope_ref,
                        "role": "user",
                        "content": turn["message"],
                        "status": "completed",
                        "created_at": turn["created_at"],
                    }
                )
                assistant_status = cast(str, turn["assistant_status"])
                messages.append(
                    {
                        "message_ref": f"{interaction_ref}:assistant",
                        "scope_ref": scope_ref,
                        "role": "assistant",
                        "content": turn["assistant_content"] or "",
                        "status": assistant_status,
                        "created_at": turn["updated_at"],
                        "reason": turn["reason"],
                    }
                )
        return {
            "messages": messages,
            "soft_constraints": [
                self._query_soft_constraint(ref) for ref in constraint_refs
            ],
            "agent_proposals": [
                self._query_agent_proposal(ref) for ref in proposal_refs
            ],
            "commands": [self.query_command(intent_id) for intent_id in intent_ids],
            "authorizations": [
                self.query_authorization(ref) for ref in authorization_refs
            ],
        }

    def _query_turn(self, interaction_ref: str) -> dict[str, object]:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT turns.*, sessions.scope_ref FROM hc_companion_turns AS "
                    "turns JOIN hc_companion_sessions AS sessions ON "
                    "sessions.session_ref = turns.session_ref WHERE "
                    "turns.interaction_ref = :interaction_ref"
                ),
                {"interaction_ref": interaction_ref},
            ).first()
        if row is None:
            raise OwnerConflict("companion_interaction_not_found")
        return _public_turn(row)

    def record_agent_proposal(
        self,
        scope_ref: str,
        proposal: dict[str, object],
        idempotency_key: str,
    ) -> dict[str, object]:
        scope_ref = _scope_ref(scope_ref, "companion_scope_required")
        proposal = _document(proposal, "agent_proposal_invalid")
        _reject_secret_content(proposal)
        command_hash = canonical_hash(
            {"command": "record_agent_proposal", "scope_ref": scope_ref, "proposal": proposal}
        )
        _idempotency_key(idempotency_key)
        with self._database.write() as connection:
            replay = _collaboration_command(
                connection, idempotency_key, "agent_proposal", command_hash
            )
            if replay is None:
                proposal_ref = new_ref("agent_proposal")
                now = time.time()
                connection.execute(
                    text(
                        "INSERT INTO hc_agent_proposals (proposal_ref, scope_ref, "
                        "proposal_json, proposal_hash, status, idempotency_key, "
                        "command_hash, created_at) VALUES (:proposal_ref, :scope_ref, "
                        ":proposal_json, :proposal_hash, 'proposed', :idempotency_key, "
                        ":command_hash, :now)"
                    ),
                    {
                        "proposal_ref": proposal_ref,
                        "scope_ref": scope_ref,
                        "proposal_json": canonical_json(proposal),
                        "proposal_hash": canonical_hash(proposal),
                        "idempotency_key": idempotency_key,
                        "command_hash": command_hash,
                        "now": now,
                    },
                )
                _record_collaboration_command(
                    connection,
                    idempotency_key,
                    "agent_proposal",
                    command_hash,
                    proposal_ref,
                )
                _advance_hc(
                    connection,
                    self._feed,
                    "human_collaboration.agent_proposal_recorded",
                    {"proposal_ref": proposal_ref, "scope_ref": scope_ref},
                )
            else:
                proposal_ref = replay
        return self._query_agent_proposal(proposal_ref)

    def _query_agent_proposal(self, proposal_ref: str) -> dict[str, object]:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM hc_agent_proposals WHERE proposal_ref = :proposal_ref"
                ),
                {"proposal_ref": proposal_ref},
            ).first()
        if row is None:
            raise OwnerConflict("agent_proposal_not_found")
        proposal = decoded_object(row.proposal_json)
        if canonical_hash(proposal) != row.proposal_hash:
            raise OwnerConflict("agent_proposal_invalid")
        return {
            "proposal_ref": row.proposal_ref,
            "scope_ref": row.scope_ref,
            "proposal": proposal,
            "proposal_hash": row.proposal_hash,
            "status": row.status,
            "authoritative_effect": False,
            "created_at": float(row.created_at),
        }

    def convert_agent_proposal_to_soft_constraint(
        self,
        proposal_ref: str,
        *,
        expected_scope_ref: str,
        expected_proposal_hash: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        proposal_ref = _text(proposal_ref, "agent_proposal_stale", 64)
        expected_scope_ref = _scope_ref(
            expected_scope_ref, "agent_proposal_stale"
        )
        expected_proposal_hash = _expected_proposal_hash(
            expected_proposal_hash
        )
        _idempotency_key(idempotency_key)
        command_hash = canonical_hash(
            {
                "command": "convert_agent_proposal_to_soft_constraint",
                "proposal_ref": proposal_ref,
                "expected_scope_ref": expected_scope_ref,
                "expected_proposal_hash": expected_proposal_hash,
            }
        )
        with self._database.write() as connection:
            replay = _collaboration_command(
                connection,
                idempotency_key,
                "proposal_to_constraint",
                command_hash,
            )
            if replay is None:
                row, guidance = _proposal_for_conversion(
                    connection,
                    proposal_ref=proposal_ref,
                    expected_scope_ref=expected_scope_ref,
                    expected_proposal_hash=expected_proposal_hash,
                )
                text_value = guidance.get("text")
                if not isinstance(text_value, str) or not text_value.strip():
                    raise OwnerConflict("soft_constraint_text_required")
                _reject_secret_content(guidance)
                constraint_ref = new_ref("soft_constraint")
                receipt_ref = new_ref("hc_receipt")
                guidance_hash = canonical_hash(guidance)
                revision = 1
                receipt_hash = _guidance_receipt_hash(
                    constraint_ref,
                    expected_scope_ref,
                    revision,
                    guidance_hash,
                )
                now = time.time()
                connection.execute(
                    text(
                        "INSERT INTO hc_soft_constraints (constraint_ref, "
                        "scope_ref, source_proposal_ref, revision, guidance_json, "
                        "guidance_hash, status, receipt_ref, receipt_hash, "
                        "idempotency_key, created_at, updated_at) VALUES "
                        "(:constraint_ref, :scope_ref, :proposal_ref, 1, "
                        ":guidance_json, :guidance_hash, 'active', :receipt_ref, "
                        ":receipt_hash, :idempotency_key, :now, :now)"
                    ),
                    {
                        "constraint_ref": constraint_ref,
                        "scope_ref": expected_scope_ref,
                        "proposal_ref": proposal_ref,
                        "guidance_json": canonical_json(guidance),
                        "guidance_hash": guidance_hash,
                        "receipt_ref": receipt_ref,
                        "receipt_hash": receipt_hash,
                        "idempotency_key": idempotency_key,
                        "now": now,
                    },
                )
                _mark_proposal_converted(connection, row)
                _record_collaboration_command(
                    connection,
                    idempotency_key,
                    "proposal_to_constraint",
                    command_hash,
                    constraint_ref,
                )
                connection.execute(
                    text(
                        "UPDATE human_collaboration_state SET revision = "
                        "revision + 1, soft_constraint_count = "
                        "soft_constraint_count + 1 WHERE singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "human_collaboration.agent_proposal_converted",
                    {
                        "proposal_ref": proposal_ref,
                        "result_kind": "soft_constraint",
                        "result_ref": constraint_ref,
                    },
                )
            else:
                constraint_ref = replay
        proposal = self._query_agent_proposal(proposal_ref)
        constraint = self._query_soft_constraint(constraint_ref)
        if constraint.get("source_proposal_ref") != proposal_ref:
            raise OwnerConflict("agent_proposal_conversion_invalid")
        return {"proposal": proposal, "soft_constraint": constraint}

    def convert_agent_proposal_to_command_draft(
        self,
        proposal_ref: str,
        *,
        expected_scope_ref: str,
        expected_proposal_hash: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        proposal_ref = _text(proposal_ref, "agent_proposal_stale", 64)
        expected_scope_ref = _scope_ref(
            expected_scope_ref, "agent_proposal_stale"
        )
        expected_proposal_hash = _expected_proposal_hash(
            expected_proposal_hash
        )
        _idempotency_key(idempotency_key)
        command_hash = canonical_hash(
            {
                "command": "convert_agent_proposal_to_command_draft",
                "proposal_ref": proposal_ref,
                "expected_scope_ref": expected_scope_ref,
                "expected_proposal_hash": expected_proposal_hash,
            }
        )
        with self._database.write() as connection:
            replay = _collaboration_command(
                connection,
                idempotency_key,
                "proposal_to_command",
                command_hash,
            )
            if replay is None:
                row, proposal = _proposal_for_conversion(
                    connection,
                    proposal_ref=proposal_ref,
                    expected_scope_ref=expected_scope_ref,
                    expected_proposal_hash=expected_proposal_hash,
                )
                proposed_command = proposal.get("command", proposal)
                if not isinstance(proposed_command, dict):
                    raise OwnerConflict("command_draft_invalid")
                command = self._validate_command(proposed_command)
                intent_id = new_ref("intent")
                now = time.time()
                connection.execute(
                    text(
                        "INSERT INTO hc_command_intents (intent_id, scope_ref, "
                        "source_proposal_ref, current_revision, status, "
                        "created_at, updated_at) VALUES (:intent_id, :scope_ref, "
                        ":proposal_ref, 1, 'draft', :now, :now)"
                    ),
                    {
                        "intent_id": intent_id,
                        "scope_ref": expected_scope_ref,
                        "proposal_ref": proposal_ref,
                        "now": now,
                    },
                )
                self._insert_command_draft(connection, intent_id, 1, command, now)
                _mark_proposal_converted(connection, row)
                _record_collaboration_command(
                    connection,
                    idempotency_key,
                    "proposal_to_command",
                    command_hash,
                    intent_id,
                )
                _advance_hc(
                    connection,
                    self._feed,
                    "human_collaboration.agent_proposal_converted",
                    {
                        "proposal_ref": proposal_ref,
                        "result_kind": "command_draft",
                        "result_ref": intent_id,
                    },
                )
            else:
                intent_id = replay
        proposal = self._query_agent_proposal(proposal_ref)
        command_draft = self.query_command(intent_id)
        if command_draft.get("source_proposal_ref") != proposal_ref:
            raise OwnerConflict("agent_proposal_conversion_invalid")
        return {"proposal": proposal, "command_draft": command_draft}

    def record_soft_constraint(
        self,
        scope_ref: str,
        guidance: dict[str, object],
        idempotency_key: str,
    ) -> dict[str, object]:
        scope_ref = _scope_ref(scope_ref, "soft_constraint_scope_required")
        guidance = _document(guidance, "soft_constraint_invalid")
        text_value = guidance.get("text")
        if not isinstance(text_value, str) or not text_value.strip():
            raise OwnerConflict("soft_constraint_text_required")
        _reject_secret_content(guidance)
        _idempotency_key(idempotency_key)
        command_hash = canonical_hash(
            {"command": "record_soft_constraint", "scope_ref": scope_ref, "guidance": guidance}
        )
        with self._database.write() as connection:
            replay = _collaboration_command(
                connection, idempotency_key, "soft_constraint", command_hash
            )
            if replay is None:
                constraint_ref = new_ref("soft_constraint")
                receipt_ref = new_ref("hc_receipt")
                revision = 1
                receipt_hash = _guidance_receipt_hash(
                    constraint_ref, scope_ref, revision, canonical_hash(guidance)
                )
                now = time.time()
                connection.execute(
                    text(
                        "INSERT INTO hc_soft_constraints (constraint_ref, scope_ref, "
                        "revision, guidance_json, guidance_hash, status, receipt_ref, "
                        "receipt_hash, idempotency_key, created_at, updated_at) VALUES "
                        "(:constraint_ref, :scope_ref, :revision, :guidance_json, "
                        ":guidance_hash, 'active', :receipt_ref, :receipt_hash, "
                        ":idempotency_key, :now, :now)"
                    ),
                    {
                        "constraint_ref": constraint_ref,
                        "scope_ref": scope_ref,
                        "revision": revision,
                        "guidance_json": canonical_json(guidance),
                        "guidance_hash": canonical_hash(guidance),
                        "receipt_ref": receipt_ref,
                        "receipt_hash": receipt_hash,
                        "idempotency_key": idempotency_key,
                        "now": now,
                    },
                )
                _record_collaboration_command(
                    connection,
                    idempotency_key,
                    "soft_constraint",
                    command_hash,
                    constraint_ref,
                )
                connection.execute(
                    text(
                        "UPDATE human_collaboration_state SET revision = revision + 1, "
                        "soft_constraint_count = soft_constraint_count + 1 WHERE "
                        "singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "human_collaboration.soft_constraint_recorded",
                    {"constraint_ref": constraint_ref, "scope_ref": scope_ref},
                )
            else:
                constraint_ref = replay
        return self._query_soft_constraint(constraint_ref)

    def withdraw_soft_constraint(
        self,
        constraint_ref: str,
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, object]:
        _idempotency_key(idempotency_key)
        command_hash = canonical_hash(
            {
                "command": "withdraw_soft_constraint",
                "constraint_ref": constraint_ref,
                "expected_revision": expected_revision,
            }
        )
        with self._database.write() as connection:
            replay = _collaboration_command(
                connection, idempotency_key, "withdraw_constraint", command_hash
            )
            if replay is None:
                row = connection.execute(
                    text(
                        "SELECT * FROM hc_soft_constraints WHERE constraint_ref = "
                        ":constraint_ref"
                    ),
                    {"constraint_ref": constraint_ref},
                ).first()
                if row is None:
                    raise OwnerConflict("soft_constraint_not_found")
                if int(row.revision) != expected_revision or row.status != "active":
                    raise OwnerConflict("soft_constraint_stale")
                connection.execute(
                    text(
                        "UPDATE hc_soft_constraints SET status = 'withdrawn', "
                        "updated_at = :now WHERE constraint_ref = :constraint_ref"
                    ),
                    {"constraint_ref": constraint_ref, "now": time.time()},
                )
                _record_collaboration_command(
                    connection,
                    idempotency_key,
                    "withdraw_constraint",
                    command_hash,
                    constraint_ref,
                )
                _advance_hc(
                    connection,
                    self._feed,
                    "human_collaboration.soft_constraint_withdrawn",
                    {"constraint_ref": constraint_ref},
                )
            else:
                constraint_ref = replay
        return self._query_soft_constraint(constraint_ref)

    def query_active_guidance_bindings(
        self, scope_ref: str
    ) -> list[dict[str, object]]:
        with self._database.read() as connection:
            rows = connection.execute(
                text(
                    "SELECT * FROM hc_soft_constraints WHERE scope_ref = :scope_ref "
                    "AND status = 'active' ORDER BY scope_ref, constraint_ref, "
                    "revision"
                ),
                {"scope_ref": scope_ref},
            ).all()
        return [guidance_binding_from_row(row) for row in rows]

    def verify_guidance_binding(self, binding: dict[str, object]) -> None:
        constraint_ref = binding.get("constraint_ref")
        if not isinstance(constraint_ref, str):
            raise OwnerConflict("guidance_binding_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM hc_soft_constraints WHERE constraint_ref = "
                    ":constraint_ref AND status = 'active'"
                ),
                {"constraint_ref": constraint_ref},
            ).first()
        if row is None or binding != guidance_binding_from_row(row):
            raise OwnerConflict("guidance_binding_invalid")

    def _query_soft_constraint(self, constraint_ref: str) -> dict[str, object]:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM hc_soft_constraints WHERE constraint_ref = "
                    ":constraint_ref"
                ),
                {"constraint_ref": constraint_ref},
            ).first()
        if row is None:
            raise OwnerConflict("soft_constraint_not_found")
        guidance = decoded_object(row.guidance_json)
        if (
            canonical_hash(guidance) != row.guidance_hash
            or row.receipt_hash
            != _guidance_receipt_hash(
                row.constraint_ref,
                row.scope_ref,
                int(row.revision),
                row.guidance_hash,
            )
        ):
            raise OwnerConflict("soft_constraint_invalid")
        return {
            "constraint_ref": row.constraint_ref,
            "scope_ref": row.scope_ref,
            "source_proposal_ref": row.source_proposal_ref,
            "revision": int(row.revision),
            "guidance": guidance,
            "status": row.status,
            "issuer": HC_OWNER,
            "receipt_ref": row.receipt_ref,
            "receipt": AcceptanceReceipt(
                issuer=HC_OWNER,
                kind="soft_constraint",
                receipt_ref=row.receipt_ref,
                subject_ref=row.constraint_ref,
                payload_hash=row.receipt_hash,
            ).as_public_dict(),
            "created_at": float(row.created_at),
            "updated_at": float(row.updated_at),
        }

    def create_command_draft(
        self,
        scope_ref: str,
        command: dict[str, object],
        idempotency_key: str,
    ) -> dict[str, object]:
        scope_ref = _scope_ref(scope_ref, "command_scope_required")
        command = self._validate_command(command)
        _idempotency_key(idempotency_key)
        command_hash = canonical_hash(
            {"command": "create_command_draft", "scope_ref": scope_ref, "draft": command}
        )
        with self._database.write() as connection:
            replay = _collaboration_command(
                connection, idempotency_key, "command_create", command_hash
            )
            if replay is None:
                intent_id = new_ref("intent")
                now = time.time()
                connection.execute(
                    text(
                        "INSERT INTO hc_command_intents (intent_id, scope_ref, "
                        "current_revision, status, created_at, updated_at) VALUES "
                        "(:intent_id, :scope_ref, 1, 'draft', :now, :now)"
                    ),
                    {"intent_id": intent_id, "scope_ref": scope_ref, "now": now},
                )
                self._insert_command_draft(connection, intent_id, 1, command, now)
                _record_collaboration_command(
                    connection,
                    idempotency_key,
                    "command_create",
                    command_hash,
                    intent_id,
                )
                _advance_hc(
                    connection,
                    self._feed,
                    "human_collaboration.command_draft_created",
                    {"intent_id": intent_id, "scope_ref": scope_ref},
                )
            else:
                intent_id = replay
        return self.query_command(intent_id)

    def revise_command_draft(
        self,
        intent_id: str,
        expected_revision: int,
        command: dict[str, object],
        idempotency_key: str,
    ) -> dict[str, object]:
        command = self._validate_command(command)
        _idempotency_key(idempotency_key)
        command_hash = canonical_hash(
            {
                "command": "revise_command_draft",
                "intent_id": intent_id,
                "expected_revision": expected_revision,
                "draft": command,
            }
        )
        with self._database.write() as connection:
            replay = _collaboration_command(
                connection, idempotency_key, "command_revise", command_hash
            )
            if replay is None:
                intent = connection.execute(
                    text(
                        "SELECT * FROM hc_command_intents WHERE intent_id = :intent_id"
                    ),
                    {"intent_id": intent_id},
                ).first()
                if intent is None:
                    raise OwnerConflict("command_intent_not_found")
                if (
                    int(intent.current_revision) != expected_revision
                    or intent.status in {"confirmed", "cancelled"}
                ):
                    raise OwnerConflict("command_draft_stale")
                revision = expected_revision + 1
                now = time.time()
                self._insert_command_draft(connection, intent_id, revision, command, now)
                connection.execute(
                    text(
                        "UPDATE hc_command_intents SET current_revision = :revision, "
                        "status = 'draft', updated_at = :now WHERE intent_id = :intent_id"
                    ),
                    {"revision": revision, "now": now, "intent_id": intent_id},
                )
                connection.execute(
                    text(
                        "UPDATE hc_command_previews SET status = 'stale' WHERE "
                        "intent_id = :intent_id AND status = 'current'"
                    ),
                    {"intent_id": intent_id},
                )
                _record_collaboration_command(
                    connection,
                    idempotency_key,
                    "command_revise",
                    command_hash,
                    intent_id,
                )
                _advance_hc(
                    connection,
                    self._feed,
                    "human_collaboration.command_draft_revised",
                    {"intent_id": intent_id, "draft_revision": revision},
                )
        return self.query_command(intent_id)

    def _insert_command_draft(
        self, connection, intent_id: str, revision: int, command: dict[str, object], now: float
    ) -> None:
        connection.execute(
            text(
                "INSERT INTO hc_command_drafts (intent_id, draft_revision, draft_json, "
                "draft_hash, created_at) VALUES (:intent_id, :revision, :draft_json, "
                ":draft_hash, :now)"
            ),
            {
                "intent_id": intent_id,
                "revision": revision,
                "draft_json": canonical_json(command),
                "draft_hash": canonical_hash(command),
                "now": now,
            },
        )

    def preview_command(
        self,
        intent_id: str,
        draft_revision: int,
        draft_hash: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        _idempotency_key(idempotency_key)
        command_hash = canonical_hash(
            {
                "command": "preview_command",
                "intent_id": intent_id,
                "draft_revision": draft_revision,
                "draft_hash": draft_hash,
            }
        )
        intent, draft_row, draft = self._current_command(
            intent_id, draft_revision, draft_hash
        )
        payload = cast(dict[str, object], draft["payload"])
        if draft["command_kind"] == "research_control":
            if self._control_preview_resolver is None:
                raise OwnerConflict("research_control_unavailable")
            owner_previews, owner_revisions = self._control_preview_resolver(
                str(intent.scope_ref), payload
            )
            if (
                not owner_previews
                or set(owner_revisions)
                != {str(item.get("source_owner")) for item in owner_previews}
                or any(
                    item.get("digest")
                    != canonical_hash(
                        {key: value for key, value in item.items() if key != "digest"}
                    )
                    for item in owner_previews
                )
            ):
                raise OwnerConflict("command_owner_preview_invalid")
        else:
            capability = cast(str, payload["capability"])
            decision = cast(str, payload["decision"])
            scope = cast(dict[str, object], payload["scope"])
            with self._database.read() as connection:
                authorization_count = int(
                    connection.execute(
                        text(
                            "SELECT authorization_count FROM human_collaboration_state "
                            "WHERE singleton = 'owner'"
                        )
                    ).scalar_one()
                )
            target_assertion = {
                "owner": HC_OWNER,
                "operation": "decide_capability_authorization",
                "intent_id": intent_id,
                "capability": capability,
                "decision": decision,
                "scope_hash": canonical_hash(scope),
                "authorization_head": authorization_count,
            }
            owner_preview = {
                "source_owner": HC_OWNER,
                "target_assertion": target_assertion,
                "will_happen": [
                    f"record an independent {decision} decision for {capability}"
                ],
                "will_not_happen": [
                    "confirmation alone will not grant the capability",
                    "no domain effect, Owner acceptance, Run, or Stage transition occurs",
                    "the authorization cannot exceed the system hard ceiling",
                ],
                "risks": [
                    "a grant may permit the exact high-risk capability inside its narrow scope"
                ],
                "stale_conditions": [
                    "the command draft changes",
                    "the capability authorization head changes",
                ],
            }
            owner_previews = [
                {**owner_preview, "digest": canonical_hash(owner_preview)}
            ]
            owner_revisions = {"human_collaboration": authorization_count}
        with self._database.write() as connection:
            replay = _collaboration_command(
                connection, idempotency_key, "command_preview", command_hash
            )
            if replay is None:
                # Re-check after deterministic computation; no stale preview is persisted.
                current = connection.execute(
                    text(
                        "SELECT * FROM hc_command_intents WHERE intent_id = :intent_id"
                    ),
                    {"intent_id": intent_id},
                ).first()
                if current is None or int(current.current_revision) != draft_revision:
                    raise OwnerConflict("command_draft_stale")
                preview_ref = new_ref("impact_preview")
                preview_payload = {
                    "intent_id": intent_id,
                    "draft_revision": draft_revision,
                    "draft_hash": draft_hash,
                    "owner_previews": owner_previews,
                    "owner_revisions": owner_revisions,
                }
                preview_hash = canonical_hash(preview_payload)
                now = time.time()
                connection.execute(
                    text(
                        "UPDATE hc_command_previews SET status = 'stale' WHERE "
                        "intent_id = :intent_id AND status = 'current'"
                    ),
                    {"intent_id": intent_id},
                )
                connection.execute(
                    text(
                        "INSERT INTO hc_command_previews (preview_ref, intent_id, "
                        "draft_revision, draft_hash, owner_previews_json, "
                        "owner_previews_hash, owner_revisions_json, "
                        "owner_revisions_hash, preview_hash, status, created_at) VALUES "
                        "(:preview_ref, :intent_id, :draft_revision, :draft_hash, "
                        ":owner_previews_json, :owner_previews_hash, "
                        ":owner_revisions_json, :owner_revisions_hash, :preview_hash, "
                        "'current', :now)"
                    ),
                    {
                        "preview_ref": preview_ref,
                        "intent_id": intent_id,
                        "draft_revision": draft_revision,
                        "draft_hash": draft_hash,
                        "owner_previews_json": canonical_json(owner_previews),
                        "owner_previews_hash": canonical_hash(owner_previews),
                        "owner_revisions_json": canonical_json(owner_revisions),
                        "owner_revisions_hash": canonical_hash(owner_revisions),
                        "preview_hash": preview_hash,
                        "now": now,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE hc_command_intents SET status = 'previewed', "
                        "updated_at = :now WHERE intent_id = :intent_id"
                    ),
                    {"intent_id": intent_id, "now": now},
                )
                _record_collaboration_command(
                    connection,
                    idempotency_key,
                    "command_preview",
                    command_hash,
                    preview_ref,
                )
                _advance_hc(
                    connection,
                    self._feed,
                    "human_collaboration.command_preview_recorded",
                    {"intent_id": intent_id, "preview_ref": preview_ref},
                )
            else:
                preview_ref = replay
        return self.query_command(intent_id)

    def confirm_command(
        self,
        intent_id: str,
        draft_revision: int,
        draft_hash: str,
        preview_ref: str,
        preview_hash: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        _idempotency_key(idempotency_key)
        command_hash = canonical_hash(
            {
                "command": "confirm_command",
                "intent_id": intent_id,
                "draft_revision": draft_revision,
                "draft_hash": draft_hash,
                "preview_ref": preview_ref,
                "preview_hash": preview_hash,
            }
        )
        try:
            self._current_command(intent_id, draft_revision, draft_hash)
        except OwnerConflict as error:
            if error.code == "command_draft_stale":
                raise OwnerConflict("command_preview_stale") from error
            raise
        with self._database.write() as connection:
            replay = _collaboration_command(
                connection, idempotency_key, "command_confirm", command_hash
            )
            if replay is None:
                preview = connection.execute(
                    text(
                        "SELECT * FROM hc_command_previews WHERE preview_ref = "
                        ":preview_ref AND intent_id = :intent_id"
                    ),
                    {"preview_ref": preview_ref, "intent_id": intent_id},
                ).first()
                if (
                    preview is None
                    or preview.status != "current"
                    or int(preview.draft_revision) != draft_revision
                    or preview.draft_hash != draft_hash
                    or preview.preview_hash != preview_hash
                ):
                    raise OwnerConflict("command_preview_stale")
                owner_revisions = decoded_object(preview.owner_revisions_json)
                if canonical_hash(owner_revisions) != preview.owner_revisions_hash:
                    raise OwnerConflict("command_preview_stale")
                if owner_revisions != _current_owner_revisions(
                    connection, tuple(owner_revisions)
                ):
                    raise OwnerConflict("command_preview_stale")
                confirmation_ref = new_ref("human_confirmation")
                receipt_hash = canonical_hash(
                    {
                        "schema_ref": COMMAND_CONFIRMATION_SCHEMA,
                        "issuer": HC_OWNER,
                        "intent_id": intent_id,
                        "draft_revision": draft_revision,
                        "draft_hash": draft_hash,
                        "preview_ref": preview_ref,
                        "preview_hash": preview_hash,
                    }
                )
                now = time.time()
                connection.execute(
                    text(
                        "INSERT INTO hc_command_confirmations (confirmation_ref, "
                        "intent_id, draft_revision, draft_hash, preview_ref, "
                        "preview_hash, receipt_hash, created_at) VALUES "
                        "(:confirmation_ref, :intent_id, :draft_revision, "
                        ":draft_hash, :preview_ref, :preview_hash, :receipt_hash, :now)"
                    ),
                    {
                        "confirmation_ref": confirmation_ref,
                        "intent_id": intent_id,
                        "draft_revision": draft_revision,
                        "draft_hash": draft_hash,
                        "preview_ref": preview_ref,
                        "preview_hash": preview_hash,
                        "receipt_hash": receipt_hash,
                        "now": now,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE hc_command_previews SET status = 'consumed' WHERE "
                        "preview_ref = :preview_ref"
                    ),
                    {"preview_ref": preview_ref},
                )
                connection.execute(
                    text(
                        "UPDATE hc_command_intents SET status = 'confirmed', "
                        "updated_at = :now WHERE intent_id = :intent_id"
                    ),
                    {"intent_id": intent_id, "now": now},
                )
                _record_collaboration_command(
                    connection,
                    idempotency_key,
                    "command_confirm",
                    command_hash,
                    confirmation_ref,
                )
                _advance_hc(
                    connection,
                    self._feed,
                    "human_collaboration.command_confirmed",
                    {"intent_id": intent_id, "confirmation_ref": confirmation_ref},
                )
        return self.query_command(intent_id)

    def _current_command(
        self, intent_id: str, revision: int, draft_hash: str
    ):
        with self._database.read() as connection:
            intent = connection.execute(
                text("SELECT * FROM hc_command_intents WHERE intent_id = :intent_id"),
                {"intent_id": intent_id},
            ).first()
            draft = connection.execute(
                text(
                    "SELECT * FROM hc_command_drafts WHERE intent_id = :intent_id "
                    "AND draft_revision = :revision"
                ),
                {"intent_id": intent_id, "revision": revision},
            ).first()
        if (
            intent is None
            or draft is None
            or int(intent.current_revision) != revision
            or draft.draft_hash != draft_hash
        ):
            raise OwnerConflict("command_draft_stale")
        document = decoded_object(draft.draft_json)
        if canonical_hash(document) != draft.draft_hash:
            raise OwnerConflict("command_draft_invalid")
        return intent, draft, document

    def query_command(self, intent_id: str) -> dict[str, object]:
        with self._database.read() as connection:
            intent = connection.execute(
                text("SELECT * FROM hc_command_intents WHERE intent_id = :intent_id"),
                {"intent_id": intent_id},
            ).first()
            if intent is None:
                raise OwnerConflict("command_intent_not_found")
            draft = connection.execute(
                text(
                    "SELECT * FROM hc_command_drafts WHERE intent_id = :intent_id "
                    "AND draft_revision = :revision"
                ),
                {"intent_id": intent_id, "revision": intent.current_revision},
            ).first()
            preview = connection.execute(
                text(
                    "SELECT * FROM hc_command_previews WHERE intent_id = :intent_id "
                    "ORDER BY created_at DESC LIMIT 1"
                ),
                {"intent_id": intent_id},
            ).first()
            confirmation = connection.execute(
                text(
                    "SELECT * FROM hc_command_confirmations WHERE intent_id = :intent_id"
                ),
                {"intent_id": intent_id},
            ).first()
            authorization_ref = (
                None
                if confirmation is None
                else connection.execute(
                    text(
                        "SELECT authorization_ref FROM "
                        "hc_capability_authorizations WHERE "
                        "basis_confirmation_ref = :confirmation_ref AND "
                        "is_current = 1 ORDER BY created_at DESC LIMIT 1"
                    ),
                    {"confirmation_ref": confirmation.confirmation_ref},
                ).scalar_one_or_none()
            )
            execution = connection.execute(
                text(
                    "SELECT * FROM hc_command_executions WHERE intent_id = "
                    ":intent_id"
                ),
                {"intent_id": intent_id},
            ).first()
        document = decoded_object(draft.draft_json)
        if canonical_hash(document) != draft.draft_hash:
            raise OwnerConflict("command_draft_invalid")
        result: dict[str, object] = {
            "intent_id": intent.intent_id,
            "scope_ref": intent.scope_ref,
            "source_proposal_ref": intent.source_proposal_ref,
            "status": intent.status,
            "draft_revision": int(draft.draft_revision),
            "draft_hash": draft.draft_hash,
            "draft": document,
            "executed": execution is not None,
            "impact_preview": None,
            "confirmation_receipt": None,
        }
        if preview is not None:
            owner_previews = json.loads(preview.owner_previews_json)
            owner_revisions = decoded_object(preview.owner_revisions_json)
            if (
                canonical_hash(owner_previews) != preview.owner_previews_hash
                or canonical_hash(owner_revisions) != preview.owner_revisions_hash
                or canonical_hash(
                    {
                        "intent_id": intent_id,
                        "draft_revision": int(preview.draft_revision),
                        "draft_hash": preview.draft_hash,
                        "owner_previews": owner_previews,
                        "owner_revisions": owner_revisions,
                    }
                )
                != preview.preview_hash
            ):
                raise OwnerConflict("command_preview_invalid")
            result["impact_preview"] = {
                "preview_ref": preview.preview_ref,
                "preview_hash": preview.preview_hash,
                "draft_revision": int(preview.draft_revision),
                "draft_hash": preview.draft_hash,
                "owner_previews": owner_previews,
                "owner_revisions": owner_revisions,
                "status": preview.status,
            }
        if confirmation is not None:
            expected_hash = canonical_hash(
                {
                    "schema_ref": COMMAND_CONFIRMATION_SCHEMA,
                    "issuer": HC_OWNER,
                    "intent_id": intent_id,
                    "draft_revision": int(confirmation.draft_revision),
                    "draft_hash": confirmation.draft_hash,
                    "preview_ref": confirmation.preview_ref,
                    "preview_hash": confirmation.preview_hash,
                }
            )
            if expected_hash != confirmation.receipt_hash:
                raise OwnerConflict("command_confirmation_invalid")
            result["confirmation_receipt"] = {
                **AcceptanceReceipt(
                    issuer=HC_OWNER,
                    kind="human_confirmation",
                    receipt_ref=confirmation.confirmation_ref,
                    subject_ref=intent_id,
                    payload_hash=confirmation.receipt_hash,
                ).as_public_dict(),
                "status": "accepted",
            }
        if authorization_ref is not None:
            result["authorization"] = self.query_authorization(
                str(authorization_ref)
            )
        if execution is not None:
            owner_receipts = json.loads(execution.owner_receipts_json)
            expected_execution_receipt_hash = canonical_hash(
                {
                    "issuer": HC_OWNER,
                    "kind": "confirmed_command_execution",
                    "subject_ref": execution.execution_ref,
                    "intent_id": execution.intent_id,
                    "confirmation_receipt_ref": execution.confirmation_ref,
                    "command_hash": execution.command_hash,
                    "owner_receipts_hash": execution.owner_receipts_hash,
                }
            )
            if (
                canonical_json(owner_receipts) != execution.owner_receipts_json
                or canonical_hash(owner_receipts) != execution.owner_receipts_hash
                or execution.receipt_hash != expected_execution_receipt_hash
            ):
                raise OwnerConflict("command_execution_receipt_invalid")
            result["control_execution"] = {
                "execution_ref": execution.execution_ref,
                "status": execution.status,
                "owner_receipts": owner_receipts,
                "receipt_ref": execution.receipt_ref,
                "receipt_hash": execution.receipt_hash,
            }
        return result

    def _validate_command(self, value: dict[str, object]) -> dict[str, object]:
        command = _document(value, "command_draft_invalid")
        if set(command) != {"command_kind", "payload"}:
            raise OwnerConflict("command_draft_invalid")
        if command["command_kind"] not in _HARD_COMMAND_KINDS:
            raise OwnerConflict("command_kind_not_confirmation_gated")
        if command["command_kind"] == "research_control":
            command["payload"] = validate_control_payload(command["payload"])
            _reject_secret_content(command)
            return command
        if command["command_kind"] != "capability_authorization":
            raise OwnerConflict("command_target_unavailable")
        payload = command["payload"]
        if not isinstance(payload, dict) or set(payload) != {
            "capability",
            "decision",
            "scope",
        }:
            raise OwnerConflict("command_payload_invalid")
        _text(payload["capability"], "capability_required", 64)
        if payload["decision"] not in {"granted", "denied", "revoked"}:
            raise OwnerConflict("authorization_decision_invalid")
        _document(payload["scope"], "authorization_scope_invalid")
        _reject_secret_content(command)
        return command

    def decide_capability_authorization(
        self,
        scope_ref: str,
        decision: dict[str, object],
        idempotency_key: str,
    ) -> dict[str, object]:
        scope_ref = _scope_ref(scope_ref, "authorization_scope_required")
        decision = _document(decision, "authorization_decision_invalid")
        if set(decision) != {
            "capability",
            "decision",
            "scope",
            "confirmation_receipt_ref",
        }:
            raise OwnerConflict("authorization_decision_invalid")
        capability = _text(decision["capability"], "capability_required", 64)
        outcome = decision["decision"]
        if outcome not in {"granted", "denied", "revoked"}:
            raise OwnerConflict("authorization_decision_invalid")
        scope = _document(decision["scope"], "authorization_scope_invalid")
        confirmation_ref = _text(
            decision["confirmation_receipt_ref"],
            "authorization_confirmation_required",
            64,
        )
        with self._database.read() as connection:
            confirmation = connection.execute(
                text(
                    "SELECT confirmations.*, drafts.draft_json, drafts.draft_hash "
                    "AS stored_draft_hash, intents.scope_ref AS intent_scope_ref "
                    ", previews.owner_previews_json, "
                    "previews.owner_previews_hash, previews.owner_revisions_json, "
                    "previews.owner_revisions_hash, previews.preview_hash AS "
                    "stored_preview_hash "
                    "FROM hc_command_confirmations AS "
                    "confirmations JOIN hc_command_drafts AS drafts ON "
                    "drafts.intent_id = confirmations.intent_id AND "
                    "drafts.draft_revision = confirmations.draft_revision JOIN "
                    "hc_command_intents AS intents ON intents.intent_id = "
                    "confirmations.intent_id JOIN hc_command_previews AS previews "
                    "ON previews.preview_ref = confirmations.preview_ref WHERE "
                    "confirmations.confirmation_ref = :confirmation_ref"
                ),
                {"confirmation_ref": confirmation_ref},
            ).first()
        if confirmation is None:
            raise OwnerConflict("authorization_confirmation_invalid")
        confirmed_draft = decoded_object(confirmation.draft_json)
        try:
            owner_previews = json.loads(confirmation.owner_previews_json)
            owner_revisions = decoded_object(confirmation.owner_revisions_json)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise OwnerConflict("authorization_confirmation_invalid") from error
        matching_previews = [
            preview
            for preview in owner_previews
            if isinstance(preview, dict)
            and preview.get("source_owner") == HC_OWNER
            and isinstance(preview.get("target_assertion"), dict)
        ] if isinstance(owner_previews, list) else []
        target_assertion = (
            matching_previews[0].get("target_assertion")
            if len(matching_previews) == 1
            else None
        )
        expected_authorization_head = (
            target_assertion.get("authorization_head")
            if isinstance(target_assertion, dict)
            else None
        )
        expected_preview_hash = canonical_hash(
            {
                "intent_id": confirmation.intent_id,
                "draft_revision": int(confirmation.draft_revision),
                "draft_hash": confirmation.draft_hash,
                "owner_previews": owner_previews,
                "owner_revisions": owner_revisions,
            }
        )
        expected_confirmation_hash = canonical_hash(
            {
                "schema_ref": COMMAND_CONFIRMATION_SCHEMA,
                "issuer": HC_OWNER,
                "intent_id": confirmation.intent_id,
                "draft_revision": int(confirmation.draft_revision),
                "draft_hash": confirmation.draft_hash,
                "preview_ref": confirmation.preview_ref,
                "preview_hash": confirmation.preview_hash,
            }
        )
        if (
            canonical_hash(confirmed_draft) != confirmation.stored_draft_hash
            or confirmation.draft_hash != confirmation.stored_draft_hash
            or confirmation.intent_scope_ref != scope_ref
            or canonical_hash(owner_previews) != confirmation.owner_previews_hash
            or canonical_hash(owner_revisions) != confirmation.owner_revisions_hash
            or confirmation.preview_hash != confirmation.stored_preview_hash
            or confirmation.preview_hash != expected_preview_hash
            or confirmation.receipt_hash != expected_confirmation_hash
            or len(matching_previews) != 1
            or matching_previews[0].get("digest")
            != canonical_hash(
                {
                    key: value
                    for key, value in matching_previews[0].items()
                    if key != "digest"
                }
            )
            or confirmed_draft.get("command_kind") != "capability_authorization"
            or confirmed_draft.get("payload")
            != {
                "capability": capability,
                "decision": outcome,
                "scope": scope,
            }
            or not isinstance(expected_authorization_head, int)
            or isinstance(expected_authorization_head, bool)
            or owner_revisions
            != {"human_collaboration": expected_authorization_head}
            or target_assertion
            != {
                "owner": HC_OWNER,
                "operation": "decide_capability_authorization",
                "intent_id": confirmation.intent_id,
                "capability": capability,
                "decision": outcome,
                "scope_hash": canonical_hash(scope),
                "authorization_head": expected_authorization_head,
            }
        ):
            raise OwnerConflict("authorization_confirmation_invalid")
        requirement = {"capability": capability, "scope": scope}
        policy = {
            "schema_ref": "meta-research/narrow-capability-policy/v1",
            "capability": capability,
            "scope": scope,
            "decision": outcome,
        }
        return self._record_authorization(
            scope_ref=scope_ref,
            authorization_kind="capability",
            capability=capability,
            decision=cast(str, outcome),
            requirement=requirement,
            policy=policy,
            target_assertion=cast(dict[str, object], target_assertion),
            initialization_id=None,
            basis_preview_ref=confirmation.preview_ref,
            basis_preview_hash=confirmation.preview_hash,
            basis_confirmation_ref=confirmation_ref,
            basis_confirmation_hash=confirmation.receipt_hash,
            quest_ref=None,
            quest_receipt_ref=None,
            quest_receipt_hash=None,
            expected_authorization_head=expected_authorization_head,
            idempotency_key=idempotency_key,
        )

    def ensure_broad_research_authorization(
        self,
        *,
        quest_ref: str,
        initialization_id: str,
        target_assertion: dict[str, object],
        preview_ref: str,
        preview_hash: str,
        confirmation_receipt_ref: str,
        confirmation_receipt_hash: str,
        quest_receipt: AcceptanceReceipt,
    ) -> dict[str, object]:
        target_assertion = _document(
            target_assertion, "broad_research_authorization_basis_invalid"
        )
        unsigned_assertion = {
            key: value for key, value in target_assertion.items() if key != "target_hash"
        }
        bindings = target_assertion.get("bindings")
        if (
            target_assertion.get("owner") != HC_OWNER
            or target_assertion.get("operation")
            != "issue_broad_research_authorization"
            or target_assertion.get("target_hash")
            != canonical_hash(unsigned_assertion)
            or not isinstance(bindings, dict)
            or bindings.get("initialization_id") != initialization_id
        ):
            raise OwnerConflict("broad_research_authorization_basis_invalid")
        policy = bindings.get("policy")
        if (
            not isinstance(policy, dict)
            or bindings.get("policy_hash") != canonical_hash(policy)
            or policy.get("schema_ref")
            not in {
                BROAD_RESEARCH_POLICY["schema_ref"],
                LEGACY_BROAD_RESEARCH_POLICY["schema_ref"],
            }
        ):
            raise OwnerConflict("broad_research_authorization_basis_invalid")
        requirement = {
            "quest_ref": quest_ref,
            "initialization_id": initialization_id,
            "target_assertion_hash": target_assertion["target_hash"],
            "policy_hash": bindings["policy_hash"],
            "resource_envelope_ref": bindings.get("resource_envelope_ref"),
            "resource_envelope_hash": bindings.get("resource_envelope_hash"),
            "resource_hard_ceiling": bindings.get("hard_ceiling"),
        }
        return self._record_authorization(
            scope_ref=f"quest:{quest_ref}",
            authorization_kind="broad_research",
            capability=None,
            decision="granted",
            requirement=requirement,
            policy=cast(dict[str, object], policy),
            target_assertion=target_assertion,
            initialization_id=initialization_id,
            basis_preview_ref=preview_ref,
            basis_preview_hash=preview_hash,
            basis_confirmation_ref=confirmation_receipt_ref,
            basis_confirmation_hash=confirmation_receipt_hash,
            quest_ref=quest_ref,
            quest_receipt_ref=quest_receipt.receipt_ref,
            quest_receipt_hash=quest_receipt.payload_hash,
            expected_authorization_head=None,
            idempotency_key=f"broad-research:{quest_ref}",
        )

    def _record_authorization(
        self,
        *,
        scope_ref: str,
        authorization_kind: str,
        capability: str | None,
        decision: str,
        requirement: dict[str, object],
        policy: dict[str, object],
        target_assertion: dict[str, object],
        initialization_id: str | None,
        basis_preview_ref: str | None,
        basis_preview_hash: str | None,
        basis_confirmation_ref: str,
        basis_confirmation_hash: str,
        quest_ref: str | None,
        quest_receipt_ref: str | None,
        quest_receipt_hash: str | None,
        expected_authorization_head: int | None,
        idempotency_key: str,
    ) -> dict[str, object]:
        _idempotency_key(idempotency_key)
        command_hash = canonical_hash(
            {
                "command": "record_authorization",
                "scope_ref": scope_ref,
                "authorization_kind": authorization_kind,
                "capability": capability,
                "decision": decision,
                "requirement": requirement,
                "policy": policy,
                "target_assertion": target_assertion,
                "initialization_id": initialization_id,
                "basis_preview_ref": basis_preview_ref,
                "basis_preview_hash": basis_preview_hash,
                "basis_confirmation_ref": basis_confirmation_ref,
                "basis_confirmation_hash": basis_confirmation_hash,
                "quest_ref": quest_ref,
                "quest_receipt_ref": quest_receipt_ref,
                "quest_receipt_hash": quest_receipt_hash,
            }
        )
        target_assertion_hash = canonical_hash(target_assertion)
        requirement_hash = canonical_hash(requirement)
        policy_hash = canonical_hash(policy)
        policy_schema_ref = policy.get("schema_ref")
        if not isinstance(policy_schema_ref, str) or not policy_schema_ref:
            raise OwnerConflict("capability_authorization_policy_invalid")
        with self._database.write() as connection:
            existing = connection.execute(
                text(
                    "SELECT * FROM hc_capability_authorizations WHERE "
                    "idempotency_key = :idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
            if existing is not None:
                if existing.receipt_hash != _authorization_receipt_hash(
                    authorization_ref=existing.authorization_ref,
                    scope_ref=scope_ref,
                    authorization_kind=authorization_kind,
                    revision=int(existing.revision),
                    initialization_id=initialization_id,
                    capability=capability,
                    decision=decision,
                    target_assertion_hash=target_assertion_hash,
                    requirement_hash=requirement_hash,
                    policy_schema_ref=policy_schema_ref,
                    policy_hash=policy_hash,
                    basis_preview_ref=basis_preview_ref,
                    basis_preview_hash=basis_preview_hash,
                    basis_confirmation_ref=basis_confirmation_ref,
                    basis_confirmation_hash=basis_confirmation_hash,
                    quest_ref=quest_ref,
                    quest_receipt_ref=quest_receipt_ref,
                    quest_receipt_hash=quest_receipt_hash,
                ):
                    raise OwnerConflict("idempotency_conflict")
                authorization_ref = existing.authorization_ref
            else:
                if expected_authorization_head is not None:
                    current_authorization_head = int(
                        connection.execute(
                            text(
                                "SELECT authorization_count FROM "
                                "human_collaboration_state WHERE singleton = 'owner'"
                            )
                        ).scalar_one()
                    )
                    if current_authorization_head != expected_authorization_head:
                        raise OwnerConflict("authorization_confirmation_stale")
                if authorization_kind == "capability":
                    latest_revision = connection.execute(
                        text(
                            "SELECT COALESCE(MAX(revision), 0) FROM "
                            "hc_capability_authorizations WHERE authorization_kind = "
                            "'capability' AND scope_ref = :scope_ref AND capability = "
                            ":capability"
                        ),
                        {"scope_ref": scope_ref, "capability": capability},
                    ).scalar_one()
                else:
                    latest_revision = connection.execute(
                        text(
                            "SELECT COALESCE(MAX(revision), 0) FROM "
                            "hc_capability_authorizations WHERE authorization_kind = "
                            "'broad_research' AND quest_ref = :quest_ref"
                        ),
                        {"quest_ref": quest_ref},
                    ).scalar_one()
                revision = int(latest_revision) + 1
                authorization_ref = new_ref("authorization")
                receipt_ref = new_ref("hc_receipt")
                receipt_hash = _authorization_receipt_hash(
                    authorization_ref=authorization_ref,
                    scope_ref=scope_ref,
                    authorization_kind=authorization_kind,
                    revision=revision,
                    initialization_id=initialization_id,
                    capability=capability,
                    decision=decision,
                    target_assertion_hash=target_assertion_hash,
                    requirement_hash=requirement_hash,
                    policy_schema_ref=policy_schema_ref,
                    policy_hash=policy_hash,
                    basis_preview_ref=basis_preview_ref,
                    basis_preview_hash=basis_preview_hash,
                    basis_confirmation_ref=basis_confirmation_ref,
                    basis_confirmation_hash=basis_confirmation_hash,
                    quest_ref=quest_ref,
                    quest_receipt_ref=quest_receipt_ref,
                    quest_receipt_hash=quest_receipt_hash,
                )
                now = time.time()
                if authorization_kind == "capability":
                    connection.execute(
                        text(
                            "UPDATE hc_capability_authorizations SET is_current = 0 "
                            "WHERE authorization_kind = 'capability' AND scope_ref = "
                            ":scope_ref AND capability = :capability AND is_current = 1"
                        ),
                        {"scope_ref": scope_ref, "capability": capability},
                    )
                connection.execute(
                    text(
                        "INSERT INTO hc_capability_authorizations "
                        "(authorization_ref, scope_ref, authorization_kind, revision, "
                        "initialization_id, capability, decision, status, "
                        "target_assertion_json, target_assertion_hash, "
                        "requirement_json, requirement_hash, policy_schema_ref, "
                        "policy_json, policy_hash, basis_preview_ref, "
                        "basis_preview_hash, basis_confirmation_ref, "
                        "basis_confirmation_hash, quest_ref, quest_receipt_ref, "
                        "quest_receipt_hash, is_current, receipt_ref, receipt_hash, "
                        "idempotency_key, created_at) VALUES (:authorization_ref, "
                        ":scope_ref, :authorization_kind, :revision, "
                        ":initialization_id, "
                        ":capability, :decision, :status, :target_assertion_json, "
                        ":target_assertion_hash, :requirement_json, "
                        ":requirement_hash, :policy_schema_ref, :policy_json, "
                        ":policy_hash, :basis_preview_ref, :basis_preview_hash, "
                        ":basis_confirmation_ref, :basis_confirmation_hash, "
                        ":quest_ref, :quest_receipt_ref, :quest_receipt_hash, 1, "
                        ":receipt_ref, :receipt_hash, :idempotency_key, :now)"
                    ),
                    {
                        "authorization_ref": authorization_ref,
                        "scope_ref": scope_ref,
                        "authorization_kind": authorization_kind,
                        "revision": revision,
                        "initialization_id": initialization_id,
                        "capability": capability,
                        "decision": decision,
                        "status": decision,
                        "target_assertion_json": canonical_json(target_assertion),
                        "target_assertion_hash": target_assertion_hash,
                        "requirement_json": canonical_json(requirement),
                        "requirement_hash": requirement_hash,
                        "policy_schema_ref": policy_schema_ref,
                        "policy_json": canonical_json(policy),
                        "policy_hash": policy_hash,
                        "basis_preview_ref": basis_preview_ref,
                        "basis_preview_hash": basis_preview_hash,
                        "basis_confirmation_ref": basis_confirmation_ref,
                        "basis_confirmation_hash": basis_confirmation_hash,
                        "quest_ref": quest_ref,
                        "quest_receipt_ref": quest_receipt_ref,
                        "quest_receipt_hash": quest_receipt_hash,
                        "receipt_ref": receipt_ref,
                        "receipt_hash": receipt_hash,
                        "idempotency_key": idempotency_key,
                        "now": now,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE human_collaboration_state SET revision = revision + 1, "
                        "authorization_count = authorization_count + 1 WHERE "
                        "singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "human_collaboration.capability_authorization_recorded",
                    {
                        "authorization_ref": authorization_ref,
                        "authorization_kind": authorization_kind,
                        "scope_ref": scope_ref,
                        "decision": decision,
                    },
                )
        return self.query_authorization(authorization_ref)

    def query_broad_research_authorization(
        self, quest_ref: str
    ) -> dict[str, object] | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT authorization_ref FROM hc_capability_authorizations WHERE "
                    "authorization_kind = 'broad_research' AND quest_ref = "
                    ":quest_ref ORDER BY revision DESC LIMIT 1"
                ),
                {"quest_ref": quest_ref},
            ).first()
        return None if row is None else self.query_authorization(row.authorization_ref)

    def query_authorization(self, authorization_ref: str) -> dict[str, object]:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM hc_capability_authorizations WHERE "
                    "authorization_ref = :authorization_ref"
                ),
                {"authorization_ref": authorization_ref},
            ).first()
            if row is not None:
                verify_authorization_currentness(connection, row)
        if row is None:
            raise OwnerConflict("capability_authorization_not_found")
        return public_authorization_from_row(row)


def verify_authorization_currentness(connection, row) -> None:
    """Verify the receipt-bound monotonic head instead of trusting a flag."""

    if row.authorization_kind == "capability":
        statement = text(
            "SELECT authorization_ref, revision, is_current FROM "
            "hc_capability_authorizations WHERE authorization_kind = "
            "'capability' AND scope_ref = :scope_ref AND capability = :capability "
            "ORDER BY revision, authorization_ref"
        )
        parameters = {"scope_ref": row.scope_ref, "capability": row.capability}
    elif row.authorization_kind == "broad_research":
        statement = text(
            "SELECT authorization_ref, revision, is_current FROM "
            "hc_capability_authorizations WHERE authorization_kind = "
            "'broad_research' AND quest_ref = :quest_ref ORDER BY revision, "
            "authorization_ref"
        )
        parameters = {"quest_ref": row.quest_ref}
    else:
        raise OwnerConflict("capability_authorization_receipt_invalid")
    revisions = connection.execute(statement, parameters).all()
    current_refs = [
        item.authorization_ref for item in revisions if bool(item.is_current)
    ]
    if (
        [int(item.revision) for item in revisions]
        != list(range(1, len(revisions) + 1))
        or not revisions
        or current_refs != [revisions[-1].authorization_ref]
        or bool(row.is_current)
        != (row.authorization_ref == revisions[-1].authorization_ref)
    ):
        raise OwnerConflict("capability_authorization_receipt_invalid")


def public_authorization_from_row(row) -> dict[str, object]:
    """Verify and project one immutable HC authorization record."""

    try:
        target_assertion = decoded_object(row.target_assertion_json)
        requirement = decoded_object(row.requirement_json)
        policy = decoded_object(row.policy_json)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise OwnerConflict("capability_authorization_receipt_invalid") from error
    expected = _authorization_receipt_hash(
        authorization_ref=row.authorization_ref,
        scope_ref=row.scope_ref,
        authorization_kind=row.authorization_kind,
        revision=int(row.revision),
        initialization_id=row.initialization_id,
        capability=row.capability,
        decision=row.decision,
        target_assertion_hash=row.target_assertion_hash,
        requirement_hash=row.requirement_hash,
        policy_schema_ref=row.policy_schema_ref,
        policy_hash=row.policy_hash,
        basis_preview_ref=row.basis_preview_ref,
        basis_preview_hash=row.basis_preview_hash,
        basis_confirmation_ref=row.basis_confirmation_ref,
        basis_confirmation_hash=row.basis_confirmation_hash,
        quest_ref=row.quest_ref,
        quest_receipt_ref=row.quest_receipt_ref,
        quest_receipt_hash=row.quest_receipt_hash,
    )
    if (
        canonical_hash(target_assertion) != row.target_assertion_hash
        or canonical_hash(requirement) != row.requirement_hash
        or canonical_hash(policy) != row.policy_hash
        or policy.get("schema_ref") != row.policy_schema_ref
        or row.status != row.decision
        or expected != row.receipt_hash
    ):
        raise OwnerConflict("capability_authorization_receipt_invalid")
    return {
        "authorization_ref": row.authorization_ref,
        "scope_ref": row.scope_ref,
        "authorization_kind": row.authorization_kind,
        "revision": int(row.revision),
        "initialization_id": row.initialization_id,
        "capability": row.capability,
        "decision": row.decision,
        "status": row.status,
        "requirement": requirement,
        "target_assertion": target_assertion,
        "policy": policy,
        "policy_hash": row.policy_hash,
        "basis_preview_ref": row.basis_preview_ref,
        "basis_preview_hash": row.basis_preview_hash,
        "confirmation_receipt_ref": row.basis_confirmation_ref,
        "confirmation_receipt_hash": row.basis_confirmation_hash,
        "quest_ref": row.quest_ref,
        "quest_receipt_ref": row.quest_receipt_ref,
        "quest_receipt_hash": row.quest_receipt_hash,
        "is_current": bool(row.is_current),
        "receipt_ref": row.receipt_ref,
        "receipt": AcceptanceReceipt(
            issuer=HC_OWNER,
            kind=(
                "broad_research_authorization"
                if row.authorization_kind == "broad_research"
                else "capability_authorization"
            ),
            receipt_ref=row.receipt_ref,
            subject_ref=row.authorization_ref,
            payload_hash=row.receipt_hash,
        ).as_public_dict(),
        "created_at": float(row.created_at),
    }


def _public_turn(row) -> dict[str, object]:
    if canonical_hash(row.message) != row.message_hash:
        raise OwnerConflict("companion_interaction_invalid")
    if row.assistant_content is not None and (
        canonical_hash(row.assistant_content) != row.assistant_content_hash
    ):
        raise OwnerConflict("companion_interaction_invalid")
    return {
        "interaction_ref": row.interaction_ref,
        "interaction_kind": "conversation",
        "scope_ref": getattr(row, "scope_ref", None),
        "ordinal": int(row.ordinal),
        "message": row.message,
        "assistant_status": row.assistant_status,
        "assistant_content": row.assistant_content,
        "adapter_kind": row.adapter_kind,
        "reason": None if row.reason_code is None else {"code": row.reason_code},
        "authoritative_effect": False,
        "created_at": float(row.created_at),
        "updated_at": float(row.updated_at),
    }


def _guidance_receipt_hash(
    constraint_ref: str, scope_ref: str, revision: int, guidance_hash: str
) -> str:
    return canonical_hash(
        {
            "schema_ref": GUIDANCE_RECEIPT_SCHEMA,
            "issuer": HC_OWNER,
            "constraint_ref": constraint_ref,
            "scope_ref": scope_ref,
            "revision": revision,
            "guidance_hash": guidance_hash,
        }
    )


def guidance_binding_from_row(row) -> dict[str, object]:
    try:
        guidance = decoded_object(row.guidance_json)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise OwnerConflict("guidance_binding_invalid") from error
    if (
        canonical_hash(guidance) != row.guidance_hash
        or row.receipt_hash
        != _guidance_receipt_hash(
            row.constraint_ref,
            row.scope_ref,
            int(row.revision),
            row.guidance_hash,
        )
    ):
        raise OwnerConflict("guidance_binding_invalid")
    return {
        "schema_ref": "meta-research/active-guidance-binding/v1",
        "issuer": HC_OWNER,
        "constraint_ref": row.constraint_ref,
        "scope_ref": row.scope_ref,
        "revision": int(row.revision),
        "guidance": guidance,
        "guidance_hash": row.guidance_hash,
        "receipt_ref": row.receipt_ref,
        "receipt_hash": row.receipt_hash,
    }


def _authorization_receipt_hash(
    *,
    authorization_ref: str,
    scope_ref: str,
    authorization_kind: str,
    revision: int,
    initialization_id: str | None,
    capability: str | None,
    decision: str,
    target_assertion_hash: str,
    requirement_hash: str,
    policy_schema_ref: str,
    policy_hash: str,
    basis_preview_ref: str | None,
    basis_preview_hash: str | None,
    basis_confirmation_ref: str,
    basis_confirmation_hash: str,
    quest_ref: str | None,
    quest_receipt_ref: str | None,
    quest_receipt_hash: str | None,
) -> str:
    return canonical_hash(
        {
            "schema_ref": AUTHORIZATION_RECEIPT_SCHEMA,
            "issuer": HC_OWNER,
            "authorization_ref": authorization_ref,
            "scope_ref": scope_ref,
            "authorization_kind": authorization_kind,
            "revision": revision,
            "initialization_id": initialization_id,
            "capability": capability,
            "decision": decision,
            "status": decision,
            "target_assertion_hash": target_assertion_hash,
            "requirement_hash": requirement_hash,
            "policy_schema_ref": policy_schema_ref,
            "policy_hash": policy_hash,
            "basis_preview_ref": basis_preview_ref,
            "basis_preview_hash": basis_preview_hash,
            "basis_confirmation_ref": basis_confirmation_ref,
            "basis_confirmation_hash": basis_confirmation_hash,
            "quest_ref": quest_ref,
            "quest_receipt_ref": quest_receipt_ref,
            "quest_receipt_hash": quest_receipt_hash,
        }
    )


def _expected_proposal_hash(value: str) -> str:
    value = _text(value, "agent_proposal_stale", 64)
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise OwnerConflict("agent_proposal_stale")
    return value


def _proposal_for_conversion(
    connection,
    *,
    proposal_ref: str,
    expected_scope_ref: str,
    expected_proposal_hash: str,
):
    row = connection.execute(
        text(
            "SELECT * FROM hc_agent_proposals WHERE proposal_ref = :proposal_ref"
        ),
        {"proposal_ref": proposal_ref},
    ).first()
    if row is None:
        raise OwnerConflict("agent_proposal_stale")
    try:
        proposal = decoded_object(row.proposal_json)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise OwnerConflict("agent_proposal_invalid") from error
    if canonical_hash(proposal) != row.proposal_hash:
        raise OwnerConflict("agent_proposal_invalid")
    if (
        row.status != "proposed"
        or row.scope_ref != expected_scope_ref
        or row.proposal_hash != expected_proposal_hash
    ):
        raise OwnerConflict("agent_proposal_stale")
    return row, proposal


def _mark_proposal_converted(connection, row) -> None:
    updated = connection.execute(
        text(
            "UPDATE hc_agent_proposals SET status = 'converted' WHERE "
            "proposal_ref = :proposal_ref AND status = 'proposed'"
        ),
        {"proposal_ref": row.proposal_ref},
    )
    if updated.rowcount != 1:
        raise OwnerConflict("agent_proposal_stale")


def _collaboration_command(
    connection, idempotency_key: str, command_kind: str, request_hash: str
) -> str | None:
    row = connection.execute(
        text(
            "SELECT * FROM hc_collaboration_commands WHERE idempotency_key = "
            ":idempotency_key"
        ),
        {"idempotency_key": idempotency_key},
    ).first()
    if row is None:
        return None
    if row.command_kind != command_kind or row.request_hash != request_hash:
        raise OwnerConflict("idempotency_conflict")
    return cast(str, row.result_ref)


def _record_collaboration_command(
    connection,
    idempotency_key: str,
    command_kind: str,
    request_hash: str,
    result_ref: str,
) -> None:
    connection.execute(
        text(
            "INSERT INTO hc_collaboration_commands (idempotency_key, command_kind, "
            "request_hash, result_ref, recorded_at) VALUES (:idempotency_key, "
            ":command_kind, :request_hash, :result_ref, :recorded_at)"
        ),
        {
            "idempotency_key": idempotency_key,
            "command_kind": command_kind,
            "request_hash": request_hash,
            "result_ref": result_ref,
            "recorded_at": time.time(),
        },
    )


def _advance_hc(
    connection,
    feed: DurableFeed,
    event_type: str,
    payload: dict[str, object],
) -> None:
    connection.execute(
        text(
            "UPDATE human_collaboration_state SET revision = revision + 1 WHERE "
            "singleton = 'owner'"
        )
    )
    feed.record(connection, event_type, payload)


def _idempotency_key(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or contains_secret(value)
    ):
        raise OwnerConflict("idempotency_key_invalid")


def _current_owner_revisions(
    connection, owners: tuple[str, ...]
) -> dict[str, int]:
    fields = {
        # Capability previews historically bind the authorization head rather
        # than unrelated Companion/command activity on HC's global revision.
        "human_collaboration": ("human_collaboration_state", "authorization_count"),
        "advancement_engine": ("advancement_engine_state", "revision"),
        "agent_runtime": ("agent_runtime_state", "revision"),
        "research_graph": ("research_graph_state", "revision"),
        "research_memory": ("research_memory_state", "revision"),
    }
    if any(owner not in fields for owner in owners):
        raise OwnerConflict("command_owner_preview_invalid")
    return {
        owner: int(
            connection.execute(
                text(
                    f"SELECT {fields[owner][1]} FROM {fields[owner][0]} "
                    "WHERE singleton = 'owner'"
                )
            ).scalar_one()
        )
        for owner in owners
    }


def _scope_ref(value: object, code: str) -> str:
    normalized = _text(value, code, 128)
    if contains_secret(normalized) or re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:/-]*", normalized
    ) is None:
        raise OwnerConflict(code)
    return normalized


def _text(value: object, code: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise OwnerConflict(code)
    return value.strip()


def _document(value: object, code: str) -> dict[str, object]:
    if not isinstance(value, dict) or not value:
        raise OwnerConflict(code)
    try:
        encoded = canonical_json(value)
        if len(encoded.encode("utf-8")) > 64 * 1024:
            raise ValueError("too large")
        return cast(dict[str, object], json.loads(encoded))
    except (TypeError, ValueError) as error:
        raise OwnerConflict(code) from error


def _reject_secret_content(value: object) -> None:
    if contains_secret(value):
        raise OwnerConflict("human_collaboration_secret_forbidden")
