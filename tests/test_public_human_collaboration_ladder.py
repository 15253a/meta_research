from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import text

from meta_research.companion import CodexCompanionAdapter
from meta_research.composition import build_production_runtime
from meta_research.database import Database
from meta_research.feed import DurableFeed
from meta_research.migration import upgrade_database
from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.paths import prepare_data_root
from meta_research.owners.human_collaboration_ladder import (
    SQLiteHumanCollaborationLadder,
)
from meta_research.quest_drafting import (
    CodexDraftingAdapter,
    DraftingUnavailable,
    HostComputeDevice,
    HostComputeSnapshot,
    IntentTurnRequest,
    IntentTurnResult,
    ProposalDraftResult,
)
from meta_research.runtime_protection import (
    InhibitorLease,
    RuntimeProtectionUnavailable,
)
from meta_research.semantic_mcp import ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS


_QUESTION = {
    "title": "A bounded question",
    "unknown_statement": "What remains unknown?",
    "answer_shape": "A falsifiable answer",
    "applicability_scope": "This Quest",
    "background_context": "",
    "requirements_constraints": "",
}


class _DeterministicDraftingProvider:
    def __init__(
        self, agent_proposal: dict[str, object] | None = None
    ) -> None:
        self.intent_requests: list[IntentTurnRequest] = []
        self.agent_proposal = agent_proposal

    def draft(self, _request) -> ProposalDraftResult:
        return ProposalDraftResult(
            content=_QUESTION,
            adapter_kind="deterministic_test_adapter",
        )

    def reply(self, request: IntentTurnRequest) -> IntentTurnResult:
        self.intent_requests.append(request)
        return IntentTurnResult(
            reply=f"assistant:{request.message}",
            native_session_ref=request.native_session_ref or "native_companion_session",
            adapter_kind="deterministic_test_adapter",
            agent_proposal=self.agent_proposal,
        )


class _LifecycleDraftingProvider(_DeterministicDraftingProvider):
    def __init__(self) -> None:
        super().__init__()
        self.finished_jobs: list[str] = []

    def finish_job(self, job_ref: str) -> None:
        self.finished_jobs.append(job_ref)


class _CompanionRuntimeBinding:
    def as_dict(self) -> dict[str, object]:
        return {
            "schema_ref": "meta-research/test-companion-runtime-binding/v1",
            "provider_ref": "test/companion",
            "provider_version": "1",
        }


class _HumanRequestLifecycleDraftingProvider(_LifecycleDraftingProvider):
    def __init__(self) -> None:
        super().__init__()
        self.owner = None
        self.human_request: dict[str, object] | None = None
        self.reconciled_human_request: dict[str, object] | None = None

    def runtime_binding(self) -> _CompanionRuntimeBinding:
        return _CompanionRuntimeBinding()

    def reply(self, request: IntentTurnRequest) -> IntentTurnResult:
        self.intent_requests.append(request)
        assert self.owner is not None
        scope = request.root_runtime_scope
        assert scope is not None
        generation = cast(int, scope["generation"])
        target = {
            "schema_ref": "meta-research/root-agent-human-request-target/v1",
            "root": {
                "run_kind": "companion",
                "run_ref": scope["run_ref"],
                "attempt_ref": scope["attempt_ref"],
                "root_session_ref": scope["root_session_ref"],
                "fence_ref": scope["fence_ref"],
                "waiter_generation": generation,
            },
            "condition": {"operator_choice": "continue_without_optional_input"},
        }
        binding = {
            "quest_ref": scope["quest_ref"],
            "task_ref": scope["run_ref"],
            "root_session_ref": scope["root_session_ref"],
            "operation_id": ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[0],
            "attempt_ref": scope["attempt_ref"],
            "generation": generation,
            "request_owner": "agent_runtime",
            "root_kind": "companion",
            "phase": "companion-turn",
            "fence_ref": scope["fence_ref"],
            "runtime_binding_hash": scope["runtime_binding_hash"],
        }
        effect_key = "mcp-effect:" + canonical_hash(
            {
                "operation_binding": binding,
                "effect_id": "companion-needs-operator-choice",
            }
        )
        if self.human_request is None:
            self.human_request = self.owner.open_human_request_effect(
                effect_key=effect_key,
                effect_id="companion-needs-operator-choice",
                operation_binding=binding,
                predecessor_request_ref=None,
                request_kind="offline_action",
                obligation=(
                    "Choose whether this exact Companion turn should continue."
                ),
                business_purpose=(
                    "Resume only this exact Quest-bound Companion turn."
                ),
                target_assertion=target,
                acceptance_conditions=(
                    "The operator records an exact disposition.",
                ),
                direct_waiter={
                    "waiter_ref": f"root_run:{scope['run_ref']}",
                    "generation": generation,
                    "target_assertion": target,
                    "wait_scope": "local",
                    "other_blockers": [],
                },
                quest_ref=cast(str, scope["quest_ref"]),
            )
        else:
            self.reconciled_human_request = (
                self.owner.reconcile_human_request_effect(effect_key)
            )
        return IntentTurnResult(
            reply=f"assistant:{request.message}",
            native_session_ref=(
                request.native_session_ref or "native_companion_session"
            ),
            adapter_kind="deterministic_test_adapter",
        )


class _TogglePowerInhibitor:
    kind = "test_toggle_inhibitor"

    def __init__(self, *, available: bool) -> None:
        self.available = available
        self._active: set[str] = set()

    def acquire(self, *, holder_ref: str, reason: str) -> InhibitorLease:
        del reason
        if not self.available:
            raise RuntimeProtectionUnavailable(
                "power_inhibitor_acquisition_failed"
            )
        self._active.add(holder_ref)
        return InhibitorLease(
            holder_ref=holder_ref,
            backend=self.kind,
            scope="sleep",
            acquired_at=time.time(),
            native_holder_ref=f"test-native:{holder_ref}",
        )

    def is_confirmed(self, lease: InhibitorLease) -> bool:
        return lease.holder_ref in self._active

    def release(self, lease: InhibitorLease) -> None:
        self._active.discard(lease.holder_ref)


class _DeterministicProbe:
    def observe(self) -> HostComputeSnapshot:
        return HostComputeSnapshot(
            status="ready",
            observed_at=1720000000.0,
            devices=(
                HostComputeDevice(
                    uuid="GPU-hc-test",
                    name="Test GPU",
                    memory_total_mib=81920,
                ),
            ),
            adapter_kind="deterministic_test_probe",
        )


class _RecordingCompanionJobRunner:
    def __init__(self, external_calls: list[tuple[str, str]]) -> None:
        self.external_calls = external_calls

    def run_job(
        self,
        job_ref: str,
        argv: list[str],
        input_text: str,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        del input_text, timeout
        native_session_ref = f"native-companion-{len(self.external_calls) + 1}"
        self.external_calls.append((job_ref, native_session_ref))
        output_path = Path(argv[argv.index("--output-last-message") + 1])
        output_path.write_text(
            json.dumps(
                {
                    "reply": f"sealed companion reply {len(self.external_calls)}",
                    "agent_proposal": None,
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps(
                {"type": "thread.started", "thread_id": native_session_ref}
            ),
            stderr="",
        )


class _UnknownOutcomeCompanionJobRunner:
    def __init__(self, external_calls: list[str]) -> None:
        self.external_calls = external_calls

    def run_job(
        self,
        job_ref: str,
        argv: list[str],
        input_text: str,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        del argv, input_text, timeout
        self.external_calls.append(job_ref)
        raise OSError("provider transport ended after submission")


class _ResidentHumanRequestCompanionRunner:
    def __init__(self) -> None:
        self.harnesses = None
        self.calls: list[tuple[str, list[str]]] = []
        self.finished_jobs: list[str] = []
        self.opened: dict[str, object] | None = None
        self.reconciled: dict[str, object] | None = None

    def run_job(
        self,
        job_ref: str,
        argv: list[str],
        prompt: str,
        timeout: float | None,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del prompt, timeout
        assert self.harnesses is not None
        assert environment is not None
        token = environment["META_RESEARCH_MCP_TOKEN"]
        effect_id = "companion-spool-human-request"
        if self.opened is None:
            operation_id = ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[0]
            arguments = {
                "effect_id": effect_id,
                "request_kind": "offline_action",
                "obligation": "Choose how this exact Companion turn continues.",
                "business_purpose": "Resume only this Companion Session.",
                "condition": {
                    "impact": "Only this Companion turn is paused.",
                    "safe_response": "Defer and continue safely.",
                },
                "acceptance_conditions": [
                    "The response is bound to this exact waiter."
                ],
            }
        else:
            operation_id = ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[1]
            arguments = {"effect_id": effect_id}
        status, payload = self.harnesses.dispatch_mcp(
            token,
            {
                "jsonrpc": "2.0",
                "id": len(self.calls) + 1,
                "method": "tools/call",
                "params": {"name": operation_id, "arguments": arguments},
            },
        )
        assert status == 200
        assert payload is not None
        result = payload["result"]
        assert result["isError"] is False
        structured = result["structuredContent"]
        assert isinstance(structured, dict)
        if self.opened is None:
            self.opened = structured
            reply = "Waiting for the exact human response."
        else:
            self.reconciled = structured
            reply = "Continued after reading the exact human response."
        self.calls.append((job_ref, list(argv)))
        output_path = Path(argv[argv.index("--output-last-message") + 1])
        output_path.write_text(
            json.dumps({"reply": reply, "agent_proposal": None}),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps(
                {
                    "type": "thread.started",
                    "thread_id": "native-companion-human-request",
                }
            ),
            stderr="",
        )

    def finish_job(self, job_ref: str) -> None:
        self.finished_jobs.append(job_ref)


def _fake_codex(path: Path) -> Path:
    path.write_text("#!/bin/sh\nprintf 'codex-test 1\\n'\n", encoding="utf-8")
    path.chmod(0o700)
    return path


def _drafting_spool_files(workspace: Path, name: str) -> list[Path]:
    return [
        *workspace.glob(f"provider-operations/*/drafting/{name}"),
        *workspace.glob(f".provider-jobs/*/{name}"),
    ]


def _runtime(
    path: Path,
    provider: _DeterministicDraftingProvider,
    *,
    power_inhibitor: _TogglePowerInhibitor | None = None,
):
    return build_production_runtime(
        prepare_data_root(path),
        proposal_drafter=provider,
        intent_drafting_provider=provider,
        host_compute_probe=_DeterministicProbe(),
        power_inhibitor=power_inhibitor,
    )


def _runtime_with_companion_adapter(
    path: Path, adapter: CodexCompanionAdapter
):
    return build_production_runtime(
        prepare_data_root(path),
        proposal_drafter=_DeterministicDraftingProvider(),
        intent_drafting_provider=adapter,
        host_compute_probe=_DeterministicProbe(),
        power_inhibitor=_TogglePowerInhibitor(available=True),
        startup_power_probe=False,
        startup_harness_diagnostics=False,
    )


def test_companion_post_invoke_validation_preserves_native_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = CodexCompanionAdapter(
        tmp_path / "invalid-companion-result",
        executable=str(_fake_codex(tmp_path / "companion-codex")),
    )
    monkeypatch.setattr(
        adapter,
        "_invoke_optional_root_task_operation",
        lambda **_kwargs: (
            {"reply": "", "agent_proposal": None},
            "native-companion-invalid-result",
            "",
        ),
    )

    with pytest.raises(DraftingUnavailable) as raised:
        adapter.reply(
            IntentTurnRequest(
                initialization_id="quest:companion-invalid-result",
                draft_revision=0,
                draft_hash="1" * 64,
                draft={"interaction_kind": "conversation"},
                message="Continue after validation.",
                native_session_ref=None,
            )
        )

    assert raised.value.code == "codex_intent_reply_invalid"
    assert raised.value.native_session_ref == (
        "native-companion-invalid-result"
    )


def _confirm_direct_quest(runtime) -> dict[str, object]:
    human = runtime.owners.human_collaboration
    opened = human.create_quest({}, "hc-ladder-quest-open")
    probed = human.observe_host_compute(
        opened["initialization_id"],
        ["GPU-hc-test"],
        "hc-ladder-compute-probe",
    )
    draft = dict(probed["quest_draft"]["value"])
    draft.update(
        {
            "goal": "Answer a bounded research question.",
            "completion_criteria": "Produce a falsifiable answer with evidence.",
            "time_budget": "30d",
            "route": "direct",
            "literature": {
                "mode": "oa_only",
                "library_entry_url": "",
                "scope_exclusions": "",
                "accepted_material_bindings": [],
            },
            "background_and_initial_direction": "Start with public literature.",
        }
    )
    revised = human.revise_quest_draft(
        opened["initialization_id"],
        draft,
        probed["quest_draft"]["hash"],
        "hc-ladder-quest-draft",
        probed["quest_draft"]["revision"],
    )
    human.generate_question_proposal(
        opened["initialization_id"],
        revised["quest_draft"]["hash"],
        "hc-ladder-question-proposal",
        revised["quest_draft"]["revision"],
    )
    assert human.process_drafting_once()
    proposed = human.query_quest_creation(opened["initialization_id"])
    previewed = human.preview_confirmation(
        opened["initialization_id"],
        quest_draft_revision=proposed["quest_draft"]["revision"],
        quest_draft_hash=proposed["quest_draft"]["hash"],
        proposal_ref=proposed["proposal"]["ref"],
        proposal_hash=proposed["proposal"]["hash"],
        idempotency_key="hc-ladder-quest-preview",
    )
    confirmed = human.confirm_quest(
        opened["initialization_id"],
        quest_draft_revision=proposed["quest_draft"]["revision"],
        quest_draft_hash=proposed["quest_draft"]["hash"],
        proposal_ref=proposed["proposal"]["ref"],
        proposal_hash=proposed["proposal"]["hash"],
        preview_ref=previewed["confirmation_preview"]["ref"],
        preview_hash=previewed["confirmation_preview"]["hash"],
        idempotency_key="hc-ladder-quest-confirm",
    )
    assert confirmed["receipts"]["human_confirmation"]["status"] == "accepted"
    return confirmed


def _confirm_capability_command(
    human,
    *,
    scope_ref: str,
    capability: str,
    decision: str,
    capability_scope: dict[str, object],
    key: str,
) -> dict[str, object]:
    drafted = human.create_command_draft(
        scope_ref,
        {
            "command_kind": "capability_authorization",
            "payload": {
                "capability": capability,
                "decision": decision,
                "scope": capability_scope,
            },
        },
        f"{key}-draft",
    )
    preview = human.preview_command(
        drafted["intent_id"],
        drafted["draft_revision"],
        drafted["draft_hash"],
        f"{key}-preview",
    )["impact_preview"]
    return human.confirm_command(
        drafted["intent_id"],
        drafted["draft_revision"],
        drafted["draft_hash"],
        preview["preview_ref"],
        preview["preview_hash"],
        f"{key}-confirm",
    )


def test_conversation_proposal_constraint_and_authorization_are_distinct(
    tmp_path: Path,
) -> None:
    provider = _DeterministicDraftingProvider()
    runtime = _runtime(tmp_path / "interaction-ladder", provider)
    human = runtime.owners.human_collaboration
    scope_ref = "quest:quest_interaction_ladder"
    try:
        initial_authorization_count = human.query_snapshot().facts[
            "authorization_count"
        ]

        queued = human.send_companion_message(
            scope_ref,
            "Help me reason about a narrower scope.",
            "companion-conversation-1",
        )
        assert queued["interaction_kind"] == "conversation"
        assert queued["assistant_status"] == "queued"
        assert human.process_drafting_once()

        companion = human.query_companion(scope_ref)
        assert companion["scope_ref"] == scope_ref
        assert companion["turns"][-1]["message"] == (
            "Help me reason about a narrower scope."
        )
        assert companion["turns"][-1]["assistant_status"] == "completed"
        assert companion["turns"][-1]["assistant_content"].startswith("assistant:")
        assert human.query_active_guidance_bindings(scope_ref) == []
        assert human.query_snapshot().facts["authorization_count"] == (
            initial_authorization_count
        )

        proposal = human.record_agent_proposal(
            scope_ref,
            {
                "proposal_kind": "narrow_scope",
                "text": "Restrict the first pass to public literature.",
            },
            "agent-proposal-1",
        )
        assert proposal["status"] == "proposed"
        assert proposal["authoritative_effect"] is False
        assert human.query_active_guidance_bindings(scope_ref) == []

        constraint = human.record_soft_constraint(
            scope_ref,
            {
                "text": "Prefer public literature for the first pass.",
                "applies_to": ["idea"],
            },
            "soft-constraint-1",
        )
        assert constraint["status"] == "active"
        assert constraint["issuer"] == "human_collaboration"
        assert constraint["receipt_ref"]
        bindings = human.query_active_guidance_bindings(scope_ref)
        assert len(bindings) == 1
        assert bindings[0]["constraint_ref"] == constraint["constraint_ref"]
        assert bindings[0]["revision"] == constraint["revision"]
        assert bindings[0]["receipt_ref"] == constraint["receipt_ref"]
        assert human.query_snapshot().facts["authorization_count"] == (
            initial_authorization_count
        )

        withdrawn = human.withdraw_soft_constraint(
            constraint["constraint_ref"],
            constraint["revision"],
            "soft-constraint-withdraw-1",
        )
        assert withdrawn["status"] == "withdrawn"
        assert human.query_active_guidance_bindings(scope_ref) == []
    finally:
        runtime.close()


def test_companion_receives_current_request_context_and_may_emit_only_a_proposal(
    tmp_path: Path,
) -> None:
    provider = _DeterministicDraftingProvider(
        {
            "proposal_kind": "alternative_route",
            "text": "Prefer the OA-only route while institutional access is blocked.",
            "applies_to": ["literature_acquisition"],
        }
    )
    runtime = _runtime(tmp_path / "companion-request-context", provider)
    human = runtime.owners.human_collaboration
    owner = runtime.owners.agent_runtime
    try:
        request = owner.open_human_request(
            request_kind="library_reconnect",
            obligation="Reconnect the exact institution-backed browser profile.",
            business_purpose="Resume the blocked acquisition session.",
            target_assertion={"session_ref": "acquisition_session_context"},
            acceptance_conditions=("Owner preflight reports ready.",),
            direct_waiter={
                "waiter_ref": "acquisition_waiter_context",
                "generation": 1,
                "target_assertion": {"session_ref": "acquisition_session_context"},
                "wait_scope": "local",
                "other_blockers": ["provider_unavailable"],
            },
            idempotency_key="companion-context-human-request",
        )
        human.send_companion_message(
            cast(str, request["request_ref"]),
            "What exact fact is still missing?",
            "companion-context-message",
        )
        assert human.process_drafting_once()

        provider_request = provider.intent_requests[-1]
        context = provider_request.draft["current_context"]
        assert context["context_kind"] == "human_request"
        assert context["human_request"]["request_ref"] == request["request_ref"]
        assert context["human_request"]["revision"] == request["revision"]
        assert context["human_request"]["status"] == "open"
        assert context["human_request"]["direct_waiters"][0]["other_blockers"] == [
            "provider_unavailable"
        ]

        projection = human.query_collaboration_projection(
            (cast(str, request["request_ref"]),)
        )
        assert len(projection["agent_proposals"]) == 1
        proposal = projection["agent_proposals"][0]
        assert proposal["proposal"] == provider.agent_proposal
        assert proposal["status"] == "proposed"
        assert proposal["authoritative_effect"] is False
        assert projection["soft_constraints"] == []
        assert projection["commands"] == []
        assert projection["authorizations"] == []
    finally:
        runtime.close()


def test_companion_binds_an_exact_current_question_view_context_into_the_agent_turn(
    tmp_path: Path,
) -> None:
    provider = _DeterministicDraftingProvider()
    runtime = _runtime(tmp_path / "companion-question-context", provider)
    human = runtime.owners.human_collaboration
    try:
        _confirm_direct_quest(runtime)
        for _step in range(8):
            if not human.reconcile_once():
                break
        [question] = runtime.owners.research_graph.query_question_tree()
        lifecycle = runtime.owners.research_graph.query_question_lifecycle(
            question.question_ref
        )
        scope_ref = f"quest:{question.quest_ref}"
        view_context = {
            "kind": "question",
            "quest_ref": question.quest_ref,
            "question_ref": question.question_ref,
            "content_ref": question.content_ref,
            "content_hash": question.content_hash,
            "lifecycle_revision": lifecycle["revision"],
        }

        queued = human.send_companion_message(
            scope_ref,
            "What evidence is still missing for this Question?",
            "companion-question-context-message",
            view_context=view_context,
        )

        assert queued["view_context"] == view_context
        assert human.process_drafting_once()
        provider_request = provider.intent_requests[-1]
        assert provider_request.message == (
            "What evidence is still missing for this Question?"
        )
        assert provider_request.draft["current_context"] == {
            "schema_ref": "meta-research/companion-context/v1",
            "scope_ref": scope_ref,
            "context_kind": "question",
            "quest_ref": question.quest_ref,
            "view_context": view_context,
            "question": {
                "question_ref": question.question_ref,
                "quest_ref": question.quest_ref,
                "parent_question_ref": None,
                "content_ref": question.content_ref,
                "content_hash": question.content_hash,
                "schema_ref": question.schema_ref,
                "question_receipt_ref": question.receipt.receipt_ref,
                "content_receipt_ref": question.content_receipt.receipt_ref,
                "lifecycle_status": "active",
                "lifecycle_revision": lifecycle["revision"],
                "title": "A bounded question",
                "unknown_statement": "What remains unknown?",
            },
        }
        assert human.query_companion(scope_ref)["turns"][-1][
            "view_context"
        ] == view_context
        projected = human.query_collaboration_projection((scope_ref,))["messages"]
        assert [message["role"] for message in projected] == [
            "user",
            "assistant",
        ]
        assert [message["view_context"] for message in projected] == [
            view_context,
            view_context,
        ]
        with pytest.raises(OwnerConflict, match="idempotency_conflict"):
            human.send_companion_message(
                scope_ref,
                "What evidence is still missing for this Question?",
                "companion-question-context-message",
            )
    finally:
        runtime.close()


def test_companion_rejects_a_stale_question_view_context_before_queueing(
    tmp_path: Path,
) -> None:
    provider = _DeterministicDraftingProvider()
    runtime = _runtime(tmp_path / "companion-stale-question-context", provider)
    human = runtime.owners.human_collaboration
    try:
        _confirm_direct_quest(runtime)
        for _step in range(8):
            if not human.reconcile_once():
                break
        [question] = runtime.owners.research_graph.query_question_tree()
        lifecycle = runtime.owners.research_graph.query_question_lifecycle(
            question.question_ref
        )
        scope_ref = f"quest:{question.quest_ref}"

        with pytest.raises(
            OwnerConflict,
            match="companion_question_view_context_stale",
        ):
            human.send_companion_message(
                scope_ref,
                "Do not enqueue this stale Question selection.",
                "companion-stale-question-context-message",
                view_context={
                    "kind": "question",
                    "quest_ref": question.quest_ref,
                    "question_ref": question.question_ref,
                    "content_ref": question.content_ref,
                    "content_hash": question.content_hash,
                    "lifecycle_revision": int(lifecycle["revision"]) + 1,
                },
            )

        assert human.query_companion(scope_ref)["turns"] == []
        assert provider.intent_requests == []
    finally:
        runtime.close()


def test_companion_fails_closed_when_question_changes_after_queueing(
    tmp_path: Path,
) -> None:
    provider = _DeterministicDraftingProvider()
    runtime = _runtime(tmp_path / "companion-queued-question-drift", provider)
    human = runtime.owners.human_collaboration
    try:
        _confirm_direct_quest(runtime)
        for _step in range(8):
            if not human.reconcile_once():
                break
        [question] = runtime.owners.research_graph.query_question_tree()
        lifecycle = runtime.owners.research_graph.query_question_lifecycle(
            question.question_ref
        )
        scope_ref = f"quest:{question.quest_ref}"
        view_context = {
            "kind": "question",
            "quest_ref": question.quest_ref,
            "question_ref": question.question_ref,
            "content_ref": question.content_ref,
            "content_hash": question.content_hash,
            "lifecycle_revision": lifecycle["revision"],
        }
        queued = human.send_companion_message(
            scope_ref,
            "This must not reach drafting after the Question is pruned.",
            "companion-question-drift-message",
            view_context=view_context,
        )

        foreground = runtime.owners.advancement_engine.query_foreground(
            question.quest_ref
        )
        assert foreground is not None
        payload = {
            "action": "prune",
            "target": {
                "quest_ref": question.quest_ref,
                "cycle_ref": foreground["cycle_ref"],
                "question_ref": question.question_ref,
                "epoch": foreground["epoch"],
                "target_question_ref": question.question_ref,
            },
            "reason": "question_context_drift_test",
        }
        drafted = human.create_command_draft(
            scope_ref,
            {"command_kind": "research_control", "payload": payload},
            "companion-question-drift-control-draft",
        )
        previewed = human.preview_command(
            drafted["intent_id"],
            drafted["draft_revision"],
            drafted["draft_hash"],
            "companion-question-drift-control-preview",
        )
        preview = previewed["impact_preview"]
        assert preview is not None
        confirmed = human.confirm_command(
            drafted["intent_id"],
            drafted["draft_revision"],
            drafted["draft_hash"],
            preview["preview_ref"],
            preview["preview_hash"],
            "companion-question-drift-control-confirm",
        )
        confirmation = confirmed["confirmation_receipt"]
        assert confirmation is not None
        executed = human.execute_confirmed_command(
            confirmed["intent_id"],
            confirmation["receipt_ref"],
            "companion-question-drift-control-execute",
        )
        assert executed["executed"] is True
        assert runtime.owners.research_graph.query_question_lifecycle(
            question.question_ref
        )["status"] == "pruned"

        replayed = human.send_companion_message(
            scope_ref,
            "This must not reach drafting after the Question is pruned.",
            "companion-question-drift-message",
            view_context=view_context,
        )
        assert replayed["interaction_ref"] == queued["interaction_ref"]
        assert replayed["assistant_status"] == "queued"
        with pytest.raises(OwnerConflict, match="idempotency_conflict"):
            human.send_companion_message(
                scope_ref,
                "This must not reach drafting after the Question is pruned.",
                "companion-question-drift-message",
            )

        assert human.process_drafting_once()
        [turn] = human.query_companion(scope_ref)["turns"]
        assert turn["assistant_status"] == "failed"
        assert turn["reason"] == {
            "code": "companion_question_view_context_stale"
        }
        terminal_replay = human.send_companion_message(
            scope_ref,
            "This must not reach drafting after the Question is pruned.",
            "companion-question-drift-message",
            view_context=view_context,
        )
        assert terminal_replay["interaction_ref"] == queued["interaction_ref"]
        assert terminal_replay["assistant_status"] == "failed"
        assert provider.intent_requests == []
    finally:
        runtime.close()


def test_companion_question_view_context_survives_restart_before_drafting(
    tmp_path: Path,
) -> None:
    data_path = tmp_path / "companion-question-context-restart"
    first_provider = _DeterministicDraftingProvider()
    runtime = _runtime(data_path, first_provider)
    human = runtime.owners.human_collaboration
    _confirm_direct_quest(runtime)
    for _step in range(8):
        if not human.reconcile_once():
            break
    [question] = runtime.owners.research_graph.query_question_tree()
    lifecycle = runtime.owners.research_graph.query_question_lifecycle(
        question.question_ref
    )
    scope_ref = f"quest:{question.quest_ref}"
    view_context = {
        "kind": "question",
        "quest_ref": question.quest_ref,
        "question_ref": question.question_ref,
        "content_ref": question.content_ref,
        "content_hash": question.content_hash,
        "lifecycle_revision": lifecycle["revision"],
    }
    runtime.owners.human_collaboration.send_companion_message(
        scope_ref,
        "Recover this exact Question context after restart.",
        "companion-question-context-restart-message",
        view_context=view_context,
    )
    runtime.close()

    restarted_provider = _DeterministicDraftingProvider()
    restarted = _runtime(data_path, restarted_provider)
    try:
        [queued] = restarted.owners.human_collaboration.query_companion(scope_ref)[
            "turns"
        ]
        assert queued["assistant_status"] == "queued"
        assert queued["view_context"] == view_context

        assert restarted.owners.human_collaboration.process_drafting_once()
        [request] = restarted_provider.intent_requests
        assert request.draft["current_context"]["view_context"] == view_context
        [completed] = restarted.owners.human_collaboration.query_companion(
            scope_ref
        )["turns"]
        assert completed["assistant_status"] == "completed"
        assert completed["view_context"] == view_context
    finally:
        restarted.close()


def test_companion_durable_view_context_requires_a_resolver_when_consumed(
    tmp_path: Path,
) -> None:
    data_path = tmp_path / "companion-context-consumption-verifier-unavailable"
    first_provider = _DeterministicDraftingProvider()
    runtime = _runtime(data_path, first_provider)
    human = runtime.owners.human_collaboration
    _confirm_direct_quest(runtime)
    for _step in range(8):
        if not human.reconcile_once():
            break
    [question] = runtime.owners.research_graph.query_question_tree()
    lifecycle = runtime.owners.research_graph.query_question_lifecycle(
        question.question_ref
    )
    scope_ref = f"quest:{question.quest_ref}"
    view_context = {
        "kind": "question",
        "quest_ref": question.quest_ref,
        "question_ref": question.question_ref,
        "content_ref": question.content_ref,
        "content_hash": question.content_hash,
        "lifecycle_revision": lifecycle["revision"],
    }
    human.send_companion_message(
        scope_ref,
        "Fail closed if the Owner resolver is missing after restart.",
        "companion-context-consumption-verifier-unavailable",
        view_context=view_context,
    )
    runtime.close()

    database = Database(prepare_data_root(data_path).database)
    provider_without_resolver = _DeterministicDraftingProvider()
    ladder = SQLiteHumanCollaborationLadder(
        database,
        DurableFeed(database),
        provider_without_resolver,
    )
    try:
        assert ladder.process_drafting_once()
        [failed] = ladder.query_companion(scope_ref)["turns"]
        assert failed["assistant_status"] == "failed"
        assert failed["reason"] == {
            "code": "companion_view_context_verifier_unavailable"
        }
        assert failed["view_context"] == view_context
        assert provider_without_resolver.intent_requests == []
    finally:
        database.close()


def test_companion_view_context_requires_an_owner_resolver(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "companion-context-verifier-unavailable.sqlite3"
    upgrade_database(database_path)
    database = Database(database_path)
    provider = _DeterministicDraftingProvider()
    ladder = SQLiteHumanCollaborationLadder(
        database,
        DurableFeed(database),
        provider,
    )
    try:
        with pytest.raises(
            OwnerConflict,
            match="companion_view_context_verifier_unavailable",
        ):
            ladder.send_companion_message(
                "quest:unverified",
                "Never pass unverified browser context to a provider.",
                "companion-unverified-context-message",
                view_context={
                    "kind": "question",
                    "quest_ref": "unverified",
                    "question_ref": "question-unverified",
                    "content_ref": "content-unverified",
                    "content_hash": "a" * 64,
                    "lifecycle_revision": 1,
                },
            )
        assert ladder.query_companion("quest:unverified")["turns"] == []

        contextless = ladder.send_companion_message(
            "workspace",
            "Legacy contextless conversation remains available.",
            "companion-contextless-without-resolver",
        )
        assert contextless["assistant_status"] == "queued"
        assert ladder.process_drafting_once()
        assert len(provider.intent_requests) == 1
        [completed] = ladder.query_companion("workspace")["turns"]
        assert completed["assistant_status"] == "completed"
        assert "view_context" not in completed
    finally:
        database.close()


def test_companion_backlog_cannot_starve_quest_proposal_generation(
    tmp_path: Path,
) -> None:
    provider = _DeterministicDraftingProvider()
    runtime = _runtime(tmp_path / "companion-fair-scheduler", provider)
    human = runtime.owners.human_collaboration
    try:
        opened = human.create_quest({}, "fair-scheduler-quest-open")
        probed = human.observe_host_compute(
            opened["initialization_id"],
            ["GPU-hc-test"],
            "fair-scheduler-compute-probe",
        )
        draft = dict(probed["quest_draft"]["value"])
        draft.update(
            {
                "goal": "Answer a bounded scheduling question.",
                "completion_criteria": "Produce one falsifiable answer.",
                "time_budget": "7d",
                "route": "direct",
                "literature": {
                    "mode": "oa_only",
                    "library_entry_url": "",
                    "scope_exclusions": "",
                    "accepted_material_bindings": [],
                },
                "background_and_initial_direction": "Use public evidence.",
            }
        )
        revised = human.revise_quest_draft(
            opened["initialization_id"],
            draft,
            probed["quest_draft"]["hash"],
            "fair-scheduler-quest-draft",
            probed["quest_draft"]["revision"],
        )
        human.generate_question_proposal(
            opened["initialization_id"],
            revised["quest_draft"]["hash"],
            "fair-scheduler-proposal-request",
            revised["quest_draft"]["revision"],
        )
        scope_ref = "quest:quest_fair_scheduler"
        human.send_companion_message(
            scope_ref, "First queued chat.", "fair-scheduler-chat-1"
        )
        human.send_companion_message(
            scope_ref, "Second queued chat.", "fair-scheduler-chat-2"
        )

        assert human.process_drafting_once()
        assert human.query_companion(scope_ref)["turns"][0][
            "assistant_status"
        ] == "completed"
        assert human.process_drafting_once()
        creation = human.query_quest_creation(opened["initialization_id"])
        assert creation["proposal"] is not None
        assert human.query_companion(scope_ref)["turns"][1][
            "assistant_status"
        ] == "queued"
    finally:
        runtime.close()


def test_collaboration_projection_isolates_every_artifact_by_exact_scope(
    tmp_path: Path,
) -> None:
    provider = _DeterministicDraftingProvider()
    runtime = _runtime(tmp_path / "projection-scope-isolation", provider)
    human = runtime.owners.human_collaboration
    scope_a = "quest:quest_projection_scope_a"
    scope_b = "human_request:human_request_projection_scope_b"
    try:
        for suffix, scope_ref in (("a", scope_a), ("b", scope_b)):
            human.send_companion_message(
                scope_ref,
                f"Keep this conversation in scope {suffix}.",
                f"scope-{suffix}-message",
            )
            assert human.process_drafting_once()
            human.record_agent_proposal(
                scope_ref,
                {"proposal_kind": "scope_probe", "scope": suffix},
                f"scope-{suffix}-proposal",
            )
            human.record_soft_constraint(
                scope_ref,
                {"text": f"Only apply guidance {suffix} in its exact scope."},
                f"scope-{suffix}-constraint",
            )
            confirmation = _confirm_capability_command(
                human,
                scope_ref=scope_ref,
                capability="external_publish",
                decision="granted",
                capability_scope={"destination": f"https://{suffix}.example.invalid"},
                key=f"scope-{suffix}-command",
            )
            human.decide_capability_authorization(
                scope_ref,
                {
                    "capability": "external_publish",
                    "decision": "granted",
                    "scope": {
                        "destination": f"https://{suffix}.example.invalid"
                    },
                    "confirmation_receipt_ref": confirmation[
                        "confirmation_receipt"
                    ]["receipt_ref"],
                },
                f"scope-{suffix}-authorization",
            )

        projection = human.query_collaboration_projection((scope_a,))
        for collection in (
            "messages",
            "soft_constraints",
            "agent_proposals",
            "commands",
            "authorizations",
        ):
            assert projection[collection]
            assert {item["scope_ref"] for item in projection[collection]} == {
                scope_a
            }
    finally:
        runtime.close()


def test_companion_terminal_owner_commit_finishes_exact_runtime_responsibility(
    tmp_path: Path,
) -> None:
    provider = _LifecycleDraftingProvider()
    runtime = _runtime(tmp_path / "companion-runtime-terminal", provider)
    human = runtime.owners.human_collaboration
    scope_ref = "quest:quest_companion_runtime_terminal"
    try:
        queued = human.send_companion_message(
            scope_ref,
            "Finish the exact runtime responsibility after this Owner commit.",
            "companion-runtime-terminal-message",
        )
        assert human.process_drafting_once()

        assert len(provider.intent_requests) == 1
        request = provider.intent_requests[0]
        assert request.job_ref == queued["interaction_ref"]
        assert provider.finished_jobs == [queued["interaction_ref"]]
        settled = human.query_companion(scope_ref)["turns"][-1]
        assert settled["assistant_status"] == "completed"
        assert runtime.query_runtime_observability()["responsibilities"] == []
        with runtime._database.read() as connection:
            responsibility = connection.execute(
                text(
                    "SELECT responsibility_ref, operation_ref, status, boundary, "
                    "checkpoint_ref FROM ar_execution_responsibilities WHERE "
                    "owner_scope = 'human_collaboration' AND root_run_ref = "
                    ":scope_ref"
                ),
                {"scope_ref": scope_ref},
            ).one()
            receipt = connection.execute(
                text(
                    "SELECT boundary, checkpoint_ref, owner_evidence_ref FROM "
                    "ar_runtime_boundary_receipts WHERE responsibility_ref = "
                    ":responsibility_ref"
                ),
                {"responsibility_ref": responsibility.responsibility_ref},
            ).one()
        assert responsibility.operation_ref == queued["interaction_ref"]
        assert responsibility.status == "finished"
        assert responsibility.boundary == "terminal"
        assert responsibility.checkpoint_ref is None
        assert receipt.boundary == "terminal"
        assert receipt.checkpoint_ref is None
        assert receipt.owner_evidence_ref.startswith("companion_terminal_")
    finally:
        runtime.close()


def test_quest_bound_companion_turn_yields_and_resumes_through_exact_owner_scope(
    tmp_path: Path,
) -> None:
    provider = _HumanRequestLifecycleDraftingProvider()
    runtime = _runtime(tmp_path / "companion-root-human-request", provider)
    human = runtime.owners.human_collaboration
    owner = runtime.owners.agent_runtime
    provider.owner = owner
    try:
        confirmed = _confirm_direct_quest(runtime)
        assert human.reconcile_once()
        creation = human.query_quest_creation(confirmed["initialization_id"])
        quest_ref = cast(str, creation["quest_ref"])
        scope_ref = f"quest:{quest_ref}"
        queued = human.send_companion_message(
            scope_ref,
            "Yield this exact Companion turn for one optional human choice.",
            "companion-root-human-request-message",
        )

        assert not human.process_drafting_once()
        [first_request] = provider.intent_requests
        root_scope = first_request.root_runtime_scope
        assert root_scope is not None
        assert set(root_scope) == {
            "quest_ref",
            "run_ref",
            "attempt_ref",
            "root_session_ref",
            "fence_ref",
            "runtime_binding_hash",
            "generation",
        }
        assert root_scope["quest_ref"] == quest_ref
        assert root_scope["run_ref"] == queued["interaction_ref"]
        assert root_scope["generation"] == 1
        suspended = owner.query_managed_run(cast(str, root_scope["run_ref"]))
        assert suspended is not None and suspended["status"] == "suspended"
        [waiting] = human.query_companion(scope_ref)["turns"]
        assert waiting["assistant_status"] == "processing"
        assert waiting["assistant_content"] is None
        assert provider.finished_jobs.count(queued["interaction_ref"]) == 1

        assert provider.human_request is not None
        request_ref = cast(str, provider.human_request["request_ref"])
        human.respond_to_human_request(
            request_ref,
            decision="deferred",
            facts={},
            note="Continue without the optional input.",
            idempotency_key="companion-root-human-request-response",
        )
        resumed = owner.query_managed_run(cast(str, root_scope["run_ref"]))
        assert resumed is not None and resumed["status"] == "running"

        assert human.process_drafting_once()
        assert len(provider.intent_requests) == 2
        assert provider.intent_requests[1].native_session_ref == (
            "native_companion_session"
        )
        assert provider.intent_requests[1].job_ref != first_request.job_ref
        assert provider.intent_requests[1].draft["human_request_resume"][
            "status"
        ] == "response_committed"
        assert provider.intent_requests[1].root_runtime_scope == root_scope
        assert provider.reconciled_human_request is not None
        assert provider.reconciled_human_request["responses"][-1][
            "decision"
        ] == "deferred"
        requests = owner.query_human_requests(quest_ref=quest_ref)
        assert [item["request_ref"] for item in requests] == [request_ref]
        settled = owner.query_managed_run(cast(str, root_scope["run_ref"]))
        assert settled is not None and settled["status"] == "completed"
        replay = owner.complete_external_root_task_scope(
            root_kind="companion",
            root_runtime_scope=root_scope,
        )
        assert replay["status"] == "completed"
        assert replay["root_runtime_scope"] == root_scope
        [completed] = human.query_companion(scope_ref)["turns"]
        assert completed["assistant_status"] == "completed"
        assert provider.finished_jobs.count(queued["interaction_ref"]) == 1
        assert provider.finished_jobs.count(
            cast(str, provider.intent_requests[1].job_ref)
        ) == 1
    finally:
        runtime.close()


def test_quest_bound_request_context_preserves_companion_root_scope(
    tmp_path: Path,
) -> None:
    provider = _HumanRequestLifecycleDraftingProvider()
    runtime = _runtime(tmp_path / "companion-request-root-scope", provider)
    human = runtime.owners.human_collaboration
    owner = runtime.owners.agent_runtime
    provider.owner = owner
    try:
        confirmed = _confirm_direct_quest(runtime)
        assert human.reconcile_once()
        creation = human.query_quest_creation(confirmed["initialization_id"])
        quest_ref = cast(str, creation["quest_ref"])
        request = owner.open_human_request(
            request_kind="offline_action",
            obligation="Review the exact Quest-bound operator choice.",
            business_purpose="Keep the related Companion turn in this Quest.",
            target_assertion={"quest_ref": quest_ref},
            acceptance_conditions=("The operator records a decision.",),
            direct_waiter={
                "waiter_ref": "companion_request_context_waiter",
                "generation": 1,
                "target_assertion": {"quest_ref": quest_ref},
                "wait_scope": "local",
                "other_blockers": [],
            },
            idempotency_key="companion-request-root-scope-open",
            quest_ref=quest_ref,
        )
        queued = human.send_companion_message(
            cast(str, request["request_ref"]),
            "Help me evaluate this exact request.",
            "companion-request-root-scope-message",
        )

        assert not human.process_drafting_once()
        [first_request] = provider.intent_requests
        assert first_request.draft["current_context"]["quest_ref"] == quest_ref
        root_scope = first_request.root_runtime_scope
        assert root_scope is not None
        assert root_scope["quest_ref"] == quest_ref
        assert root_scope["run_ref"] == queued["interaction_ref"]

        assert provider.human_request is not None
        human.respond_to_human_request(
            cast(str, provider.human_request["request_ref"]),
            decision="deferred",
            facts={},
            note="Continue after reviewing the request context.",
            idempotency_key="companion-request-root-scope-response",
        )
        assert human.process_drafting_once()
        assert len(provider.intent_requests) == 2
        assert provider.intent_requests[1].root_runtime_scope == root_scope
        assert provider.intent_requests[1].native_session_ref == (
            "native_companion_session"
        )
    finally:
        runtime.close()


def test_companion_completion_recovery_finishes_the_continuation_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_path = tmp_path / "companion-continuation-completion-crash"
    provider = _HumanRequestLifecycleDraftingProvider()
    runtime = _runtime(data_path, provider)
    human = runtime.owners.human_collaboration
    owner = runtime.owners.agent_runtime
    provider.owner = owner
    try:
        confirmed = _confirm_direct_quest(runtime)
        assert human.reconcile_once()
        creation = human.query_quest_creation(confirmed["initialization_id"])
        scope_ref = f"quest:{cast(str, creation['quest_ref'])}"
        queued = human.send_companion_message(
            scope_ref,
            "Keep this continuation across the completion crash.",
            "companion-continuation-completion-crash-message",
        )
        assert not human.process_drafting_once()
        assert provider.human_request is not None
        human.respond_to_human_request(
            cast(str, provider.human_request["request_ref"]),
            decision="deferred",
            facts={},
            note="Continue after the exact response.",
            idempotency_key="companion-continuation-completion-response",
        )

        def _crash_before_scope_completion(**_kwargs: object) -> object:
            raise RuntimeError("simulated continuation completion crash")

        monkeypatch.setattr(
            owner,
            "complete_external_root_task_scope",
            _crash_before_scope_completion,
        )
        with pytest.raises(
            RuntimeError, match="simulated continuation completion crash"
        ):
            human.process_drafting_once()
        continuation_job_ref = cast(str, provider.intent_requests[1].job_ref)
        assert continuation_job_ref.startswith("companion_continuation_")
        assert provider.finished_jobs[-1] == queued["interaction_ref"]
        assert continuation_job_ref not in provider.finished_jobs
    finally:
        runtime.close()

    recovered_provider = _LifecycleDraftingProvider()
    restarted = _runtime(data_path, recovered_provider)
    try:
        assert restarted.owners.human_collaboration.process_drafting_once()
        assert recovered_provider.intent_requests == []
        assert recovered_provider.finished_jobs == [continuation_job_ref]
        assert not restarted.owners.human_collaboration.process_drafting_once()
    finally:
        restarted.close()


def test_codex_companion_resumes_human_request_in_one_new_durable_operation(
    tmp_path: Path,
) -> None:
    data_path = tmp_path / "companion-codex-human-request-data"
    provider_workspace = tmp_path / "companion-codex-human-request-provider"
    executable = str(_fake_codex(tmp_path / "companion-codex"))
    runner = _ResidentHumanRequestCompanionRunner()
    first_adapter = CodexCompanionAdapter(
        provider_workspace,
        executable=executable,
        process_runner=runner,  # type: ignore[arg-type]
    )
    runtime = _runtime_with_companion_adapter(data_path, first_adapter)
    runtime.configure_resident_mcp_endpoint("http://127.0.0.1:8766")
    runner.harnesses = runtime.harnesses
    human = runtime.owners.human_collaboration
    try:
        confirmed = _confirm_direct_quest(runtime)
        assert human.reconcile_once()
        creation = human.query_quest_creation(confirmed["initialization_id"])
        quest_ref = cast(str, creation["quest_ref"])
        scope_ref = f"quest:{quest_ref}"
        queued = human.send_companion_message(
            scope_ref,
            "Pause once, then continue in this exact Companion Session.",
            "companion-codex-human-request-message",
        )

        assert not human.process_drafting_once()
        assert runner.opened is not None
        assert len(runner.calls) == 1
        first_job_ref, first_argv = runner.calls[0]
        assert first_job_ref == queued["interaction_ref"]
        assert "resume" not in first_argv
        [waiting] = human.query_companion(scope_ref)["turns"]
        assert waiting["assistant_status"] == "processing"
        assert waiting["assistant_content"] is None
        request_ref = cast(str, runner.opened["request_ref"])
        human.respond_to_human_request(
            request_ref,
            decision="deferred",
            facts={"safe_route": "continue_without_optional_input"},
            note="Continue in the same Companion Session.",
            idempotency_key="companion-codex-human-request-response",
        )
        assert human.process_drafting_once()
        assert len(runner.calls) == 2
        continuation_job_ref, continuation_argv = runner.calls[1]
        assert continuation_job_ref != first_job_ref
        assert continuation_job_ref.startswith("companion_continuation_")
        resume_index = continuation_argv.index("resume")
        assert continuation_argv[resume_index + 1] == (
            "native-companion-human-request"
        )
        assert runner.reconciled is not None
        resolution = runner.reconciled["resolution"]
        assert isinstance(resolution, dict)
        assert isinstance(resolution["response_ref"], str)
        assert {
            key: value
            for key, value in resolution.items()
            if key != "response_ref"
        } == {
            "decision": "deferred",
            "facts": {"safe_route": "continue_without_optional_input"},
            "note": "Continue in the same Companion Session.",
            "disposition": "unsatisfied",
            "reason_code": "human_deferred_exact_obligation",
            "accepted_evidence_refs": [],
        }
        [completed] = human.query_companion(scope_ref)["turns"]
        assert completed["assistant_status"] == "completed"
        assert completed["assistant_content"] == (
            "Continued after reading the exact human response."
        )
        assert runner.finished_jobs == [first_job_ref, continuation_job_ref]
        assert not human.process_drafting_once()
        assert len(runner.calls) == 2
    finally:
        runtime.close()


@pytest.mark.parametrize("scope_committed_before_crash", [True, False])
def test_quest_bound_companion_completion_crash_does_not_repeat_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope_committed_before_crash: bool,
) -> None:
    data_path = tmp_path / (
        "companion-root-completion-crash-"
        f"{scope_committed_before_crash}"
    )
    first_provider = _LifecycleDraftingProvider()
    monkeypatch.setattr(
        first_provider,
        "runtime_binding",
        lambda: _CompanionRuntimeBinding(),
        raising=False,
    )
    runtime = _runtime(data_path, first_provider)
    human = runtime.owners.human_collaboration
    owner = runtime.owners.agent_runtime
    try:
        confirmed = _confirm_direct_quest(runtime)
        assert human.reconcile_once()
        creation = human.query_quest_creation(confirmed["initialization_id"])
        quest_ref = cast(str, creation["quest_ref"])
        scope_ref = f"quest:{quest_ref}"
        queued = human.send_companion_message(
            scope_ref,
            "Keep this exact Companion result across a completion crash.",
            "companion-root-completion-crash-message",
        )
        complete_scope = owner.complete_external_root_task_scope

        def complete_scope_then_crash(*, root_kind, root_runtime_scope):
            if scope_committed_before_crash:
                complete_scope(
                    root_kind=root_kind,
                    root_runtime_scope=root_runtime_scope,
                )
            raise RuntimeError("simulated crash after Companion scope completion")

        monkeypatch.setattr(
            owner,
            "complete_external_root_task_scope",
            complete_scope_then_crash,
        )
        with pytest.raises(
            RuntimeError,
            match="simulated crash after Companion scope completion",
        ):
            human.process_drafting_once()
        assert len(first_provider.intent_requests) == 1
        assert first_provider.intent_requests[0].job_ref == queued["interaction_ref"]
    finally:
        runtime.close()

    restarted_provider = _LifecycleDraftingProvider()
    monkeypatch.setattr(
        restarted_provider,
        "runtime_binding",
        lambda: _CompanionRuntimeBinding(),
        raising=False,
    )
    restarted = _runtime(data_path, restarted_provider)
    try:
        human = restarted.owners.human_collaboration
        [completed] = human.query_companion(scope_ref)["turns"]
        assert completed["interaction_ref"] == queued["interaction_ref"]
        assert completed["assistant_status"] == "completed"
        assert completed["assistant_content"] == (
            "assistant:Keep this exact Companion result across a completion crash."
        )
        assert human.process_drafting_once() is (
            not scope_committed_before_crash
        )
        assert not human.process_drafting_once()
        assert restarted_provider.intent_requests == []
        managed = restarted.owners.agent_runtime.query_managed_run(
            queued["interaction_ref"]
        )
        assert managed is not None and managed["status"] == "completed"
        assert restarted.query_runtime_observability()["responsibilities"] == []
    finally:
        restarted.close()


def test_companion_deferred_response_recovers_issuing_owner_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_path = tmp_path / "companion-root-response-crash-recovery"
    provider = _HumanRequestLifecycleDraftingProvider()
    runtime = _runtime(data_path, provider)
    human = runtime.owners.human_collaboration
    owner = runtime.owners.agent_runtime
    provider.owner = owner
    try:
        confirmed = _confirm_direct_quest(runtime)
        assert human.reconcile_once()
        creation = human.query_quest_creation(confirmed["initialization_id"])
        quest_ref = cast(str, creation["quest_ref"])
        scope_ref = f"quest:{quest_ref}"
        human.send_companion_message(
            scope_ref,
            "Yield this Companion turn across one response ACK-loss window.",
            "companion-root-response-crash-message",
        )

        assert not human.process_drafting_once()
        [first_request] = provider.intent_requests
        root_scope = first_request.root_runtime_scope
        assert root_scope is not None
        assert provider.human_request is not None
        request_ref = cast(str, provider.human_request["request_ref"])

        monkeypatch.setattr(
            human,
            "_reconcile_issuing_owner_human_request",
            lambda _request_ref: None,
        )
        response = human.respond_to_human_request(
            request_ref,
            decision="deferred",
            facts={},
            note="Continue without the optional input after restart.",
            idempotency_key="companion-root-response-crash-response",
        )
        persisted = owner.query_human_request(request_ref)
        assert persisted is not None and persisted["status"] == "open"
        assert [item["response_ref"] for item in persisted["responses"]] == [
            response["response_ref"]
        ]
        suspended = owner.query_managed_run(cast(str, root_scope["run_ref"]))
        assert suspended is not None and suspended["status"] == "suspended"
    finally:
        runtime.close()

    recovered_provider = _HumanRequestLifecycleDraftingProvider()
    restarted = _runtime(data_path, recovered_provider)
    recovered_provider.owner = restarted.owners.agent_runtime
    try:
        recovered = restarted.owners.agent_runtime.query_human_request(
            request_ref
        )
        assert recovered is not None and recovered["status"] == "unsatisfied"
        [waiter] = recovered["direct_waiters"]
        assert waiter["status"] == "consumed"
        managed = restarted.owners.agent_runtime.query_managed_run(
            cast(str, root_scope["run_ref"])
        )
        assert managed is not None and managed["status"] == "running"
    finally:
        restarted.close()


def test_companion_acquire_failure_is_terminal_no_effect_with_provider_zero_call(
    tmp_path: Path,
) -> None:
    provider = _LifecycleDraftingProvider()
    runtime = _runtime(
        tmp_path / "companion-runtime-acquire-failure",
        provider,
        power_inhibitor=_TogglePowerInhibitor(available=False),
    )
    human = runtime.owners.human_collaboration
    scope_ref = "quest:quest_companion_runtime_acquire_failure"
    try:
        queued = human.send_companion_message(
            scope_ref,
            "Never call the provider without a confirmed power hold.",
            "companion-runtime-acquire-failure-message",
        )
        assert human.process_drafting_once()

        assert provider.intent_requests == []
        assert provider.finished_jobs == []
        failed = human.query_companion(scope_ref)["turns"][-1]
        assert failed["interaction_ref"] == queued["interaction_ref"]
        assert failed["assistant_status"] == "failed"
        assert failed["reason"] == {
            "code": "power_inhibitor_acquisition_failed"
        }
        observability = runtime.query_runtime_observability()
        assert observability["responsibilities"] == []
        assert observability["durable_waiting"] == []
        with runtime._database.read() as connection:
            responsibility = connection.execute(
                text(
                    "SELECT responsibility_ref, operation_ref, status, boundary "
                    "FROM ar_execution_responsibilities WHERE owner_scope = "
                    "'human_collaboration' AND root_run_ref = :scope_ref"
                ),
                {"scope_ref": scope_ref},
            ).one()
            receipt = connection.execute(
                text(
                    "SELECT boundary, owner_evidence_ref FROM "
                    "ar_runtime_boundary_receipts WHERE responsibility_ref = "
                    ":responsibility_ref"
                ),
                {"responsibility_ref": responsibility.responsibility_ref},
            ).one()
        assert responsibility.operation_ref == queued["interaction_ref"]
        assert responsibility.status == "finished"
        assert responsibility.boundary == "terminal"
        assert receipt.boundary == "terminal"
        assert receipt.owner_evidence_ref.startswith("companion_terminal_")
    finally:
        runtime.close()


def test_companion_ack_loss_recovers_without_a_second_provider_job_or_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_path = tmp_path / "companion-ack-loss-data"
    provider_workspace = tmp_path / "companion-provider-spool"
    external_calls: list[tuple[str, str]] = []
    first_provider = CodexDraftingAdapter(
        provider_workspace,
        process_runner=_RecordingCompanionJobRunner(external_calls),
    )
    runtime = _runtime(data_path, first_provider)  # type: ignore[arg-type]
    human = runtime.owners.human_collaboration
    scope_ref = "quest:quest_companion_ack_loss"
    try:
        queued = human.send_companion_message(
            scope_ref,
            "Recover this exact provider reply after an Owner ACK loss.",
            "companion-ack-loss-message",
        )
        expected_job_ref = queued["interaction_ref"]

        def crash_before_owner_commit(*_args, **_kwargs) -> None:
            raise RuntimeError("simulated companion Owner ACK loss")

        monkeypatch.setattr(
            human._collaboration_ladder,
            "_finish_companion_turn",
            crash_before_owner_commit,
        )
        with pytest.raises(RuntimeError, match="simulated companion Owner ACK loss"):
            human.process_drafting_once()

        interrupted = human.query_companion(scope_ref)["turns"][-1]
        assert interrupted["assistant_status"] == "processing"
        assert interrupted["assistant_content"] is None
        assert external_calls == [(expected_job_ref, "native-companion-1")]
    finally:
        runtime.close()

    restarted_provider = CodexDraftingAdapter(
        provider_workspace,
        process_runner=_RecordingCompanionJobRunner(external_calls),
    )
    restarted = _runtime(data_path, restarted_provider)  # type: ignore[arg-type]
    try:
        human = restarted.owners.human_collaboration
        recovered = human.query_companion(scope_ref)["turns"][-1]
        assert recovered["interaction_ref"] == queued["interaction_ref"]
        assert recovered["assistant_status"] == "queued"
        assert human.process_drafting_once()

        # A durable sealed result may be consumed again, but the external
        # provider operation and its native thread identity are exactly-once.
        assert external_calls == [(expected_job_ref, "native-companion-1")]
        settled = human.query_companion(scope_ref)["turns"][-1]
        assert settled["interaction_ref"] == queued["interaction_ref"]
        assert settled["assistant_status"] == "completed"
        assert settled["assistant_content"] == "sealed companion reply 1"
        assert settled["authoritative_effect"] is False
        assert not human.process_drafting_once()
        assert restarted.query_runtime_observability()["responsibilities"] == []
        with restarted._database.read() as connection:
            boundaries = connection.execute(
                text(
                    "SELECT attempt_ref, operation_ref, status, boundary FROM "
                    "ar_execution_responsibilities WHERE owner_scope = "
                    "'human_collaboration' AND root_run_ref = :scope_ref ORDER BY "
                    "attempt_ref"
                ),
                {"scope_ref": scope_ref},
            ).all()
        assert len(boundaries) == 2
        assert {row.operation_ref for row in boundaries} == {expected_job_ref}
        assert {row.status for row in boundaries} == {"finished"}
        assert {row.boundary for row in boundaries} == {
            "permanent_fence",
            "terminal",
        }
    finally:
        restarted.close()


def test_companion_unknown_outcome_keeps_hold_and_spool_without_blind_replay(
    tmp_path: Path,
) -> None:
    data_path = tmp_path / "companion-unknown-outcome-data"
    provider_workspace = tmp_path / "companion-unknown-outcome-spool"
    external_calls: list[str] = []
    runtime = _runtime(
        data_path,
        CodexDraftingAdapter(
            provider_workspace,
            process_runner=_UnknownOutcomeCompanionJobRunner(external_calls),
        ),  # type: ignore[arg-type]
    )
    human = runtime.owners.human_collaboration
    scope_ref = "quest:quest_companion_unknown_outcome"
    try:
        queued = human.send_companion_message(
            scope_ref,
            "Do not replay this provider job when its outcome is unknown.",
            "companion-unknown-outcome-message",
        )
        expected_job_ref = queued["interaction_ref"]

        assert human.process_drafting_once()
        assert external_calls == [expected_job_ref]
        pending = human.query_companion(scope_ref)["turns"][-1]
        assert pending["assistant_status"] == "processing"
        assert pending["reason"] == {"code": "codex_cli_io_unavailable"}
        assert not human.process_drafting_once()
        assert external_calls == [expected_job_ref]
        observability = runtime.query_runtime_observability()
        assert len(observability["responsibilities"]) == 1
        assert observability["responsibilities"][0]["operation_ref"] == (
            expected_job_ref
        )
        assert len(_drafting_spool_files(provider_workspace, "invocation.json")) == 1
    finally:
        runtime.close()

    restarted = _runtime(
        data_path,
        CodexDraftingAdapter(
            provider_workspace,
            process_runner=_UnknownOutcomeCompanionJobRunner(external_calls),
        ),  # type: ignore[arg-type]
    )
    try:
        human = restarted.owners.human_collaboration
        assert human.process_drafting_once()
        assert external_calls == [expected_job_ref]
        pending = human.query_companion(scope_ref)["turns"][-1]
        assert pending["assistant_status"] == "processing"
        assert pending["reason"] == {"code": "codex_job_outcome_unknown"}
        assert not human.process_drafting_once()
        observability = restarted.query_runtime_observability()
        assert len(observability["responsibilities"]) == 2
        assert {
            item["operation_ref"] for item in observability["responsibilities"]
        } == {expected_job_ref}
        assert len(_drafting_spool_files(provider_workspace, "invocation.json")) == 1
    finally:
        restarted.close()


def test_companion_rejects_tampered_sealed_provider_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_path = tmp_path / "companion-tampered-spool-data"
    provider_workspace = tmp_path / "companion-tampered-spool"
    external_calls: list[tuple[str, str]] = []
    runtime = _runtime(
        data_path,
        CodexDraftingAdapter(
            provider_workspace,
            process_runner=_RecordingCompanionJobRunner(external_calls),
        ),  # type: ignore[arg-type]
    )
    human = runtime.owners.human_collaboration
    scope_ref = "quest:quest_companion_tampered_spool"
    try:
        human.send_companion_message(
            scope_ref,
            "Reject a sealed result whose content no longer matches its hash.",
            "companion-tampered-spool-message",
        )

        def crash_before_owner_commit(*_args, **_kwargs) -> None:
            raise RuntimeError("simulated Owner ACK loss before tamper")

        monkeypatch.setattr(
            human._collaboration_ladder,
            "_finish_companion_turn",
            crash_before_owner_commit,
        )
        with pytest.raises(RuntimeError, match="before tamper"):
            human.process_drafting_once()
    finally:
        runtime.close()

    result_paths = _drafting_spool_files(provider_workspace, "result.json")
    assert len(result_paths) == 1
    sealed = json.loads(result_paths[0].read_text(encoding="utf-8"))
    sealed["raw"] = {"reply": "forged sealed reply"}
    result_paths[0].write_text(json.dumps(sealed), encoding="utf-8")

    restarted = _runtime(
        data_path,
        CodexDraftingAdapter(
            provider_workspace,
            process_runner=_RecordingCompanionJobRunner(external_calls),
        ),  # type: ignore[arg-type]
    )
    try:
        human = restarted.owners.human_collaboration
        assert human.process_drafting_once()
        assert len(external_calls) == 1
        failed = human.query_companion(scope_ref)["turns"][-1]
        assert failed["assistant_status"] == "failed"
        assert failed["assistant_content"] is None
        assert failed["reason"] == {"code": "codex_job_spool_invalid"}
    finally:
        restarted.close()


def test_owner_atomically_converts_exact_proposal_to_soft_constraint(
    tmp_path: Path,
) -> None:
    provider = _DeterministicDraftingProvider()
    runtime = _runtime(tmp_path / "proposal-to-soft-constraint", provider)
    human = runtime.owners.human_collaboration
    scope_ref = "quest:quest_proposal_to_soft_constraint"
    proposal_value = {
        "proposal_kind": "narrow_scope",
        "text": "Restrict the first pass to public literature.",
        "applies_to": ["idea"],
    }
    try:
        proposal = human.record_agent_proposal(
            scope_ref,
            proposal_value,
            "proposal-to-constraint-record",
        )

        with pytest.raises(OwnerConflict, match="agent_proposal_stale"):
            human.convert_agent_proposal_to_soft_constraint(
                proposal["proposal_ref"],
                expected_scope_ref="quest:forged_scope",
                expected_proposal_hash=canonical_hash(proposal_value),
                idempotency_key="proposal-to-constraint-forged-scope",
            )
        unchanged = human.query_collaboration_projection(
            (scope_ref, "quest:forged_scope")
        )
        assert unchanged["agent_proposals"] == [proposal]
        assert unchanged["soft_constraints"] == []

        converted = human.convert_agent_proposal_to_soft_constraint(
            proposal["proposal_ref"],
            expected_scope_ref=scope_ref,
            expected_proposal_hash=canonical_hash(proposal_value),
            idempotency_key="proposal-to-constraint-convert",
        )
        assert set(converted) == {"proposal", "soft_constraint"}
        assert converted["proposal"]["proposal_ref"] == proposal["proposal_ref"]
        assert converted["proposal"]["scope_ref"] == scope_ref
        assert converted["proposal"]["proposal"] == proposal_value
        assert converted["proposal"]["status"] == "converted"
        assert converted["proposal"]["authoritative_effect"] is False
        constraint = converted["soft_constraint"]
        assert constraint["scope_ref"] == scope_ref
        assert constraint["guidance"] == proposal_value
        assert constraint["status"] == "active"
        assert constraint["source_proposal_ref"] == proposal["proposal_ref"]

        replay = human.convert_agent_proposal_to_soft_constraint(
            proposal["proposal_ref"],
            expected_scope_ref=scope_ref,
            expected_proposal_hash=canonical_hash(proposal_value),
            idempotency_key="proposal-to-constraint-convert",
        )
        assert replay["proposal"]["proposal_ref"] == proposal["proposal_ref"]
        assert replay["soft_constraint"]["constraint_ref"] == constraint[
            "constraint_ref"
        ]

        with pytest.raises(OwnerConflict, match="agent_proposal_stale"):
            human.convert_agent_proposal_to_soft_constraint(
                proposal["proposal_ref"],
                expected_scope_ref=scope_ref,
                expected_proposal_hash=canonical_hash(proposal_value),
                idempotency_key="proposal-to-constraint-duplicate",
            )
        persisted = human.query_collaboration_projection((scope_ref,))
        assert len(persisted["agent_proposals"]) == 1
        assert persisted["agent_proposals"][0]["status"] == "converted"
        assert persisted["soft_constraints"] == [constraint]
    finally:
        runtime.close()


def test_owner_atomically_converts_exact_proposal_to_command_draft(
    tmp_path: Path,
) -> None:
    provider = _DeterministicDraftingProvider()
    runtime = _runtime(tmp_path / "proposal-to-command-draft", provider)
    human = runtime.owners.human_collaboration
    scope_ref = "quest:quest_proposal_to_command_draft"
    command = {
        "command_kind": "capability_authorization",
        "payload": {
            "capability": "external_publish",
            "decision": "granted",
            "scope": {
                "destination": "https://example.invalid/publication",
                "asset_ref": "asset_from_agent_proposal",
            },
        },
    }
    try:
        proposal = human.record_agent_proposal(
            scope_ref,
            command,
            "proposal-to-command-record",
        )

        with pytest.raises(OwnerConflict, match="agent_proposal_stale"):
            human.convert_agent_proposal_to_command_draft(
                proposal["proposal_ref"],
                expected_scope_ref=scope_ref,
                expected_proposal_hash="f" * 64,
                idempotency_key="proposal-to-command-forged-hash",
            )
        unchanged = human.query_collaboration_projection((scope_ref,))
        assert unchanged["agent_proposals"] == [proposal]
        assert unchanged["commands"] == []

        converted = human.convert_agent_proposal_to_command_draft(
            proposal["proposal_ref"],
            expected_scope_ref=scope_ref,
            expected_proposal_hash=canonical_hash(command),
            idempotency_key="proposal-to-command-convert",
        )
        assert set(converted) == {"proposal", "command_draft"}
        assert converted["proposal"]["proposal_ref"] == proposal["proposal_ref"]
        assert converted["proposal"]["scope_ref"] == scope_ref
        assert converted["proposal"]["proposal"] == command
        assert converted["proposal"]["status"] == "converted"
        assert converted["proposal"]["authoritative_effect"] is False
        draft = converted["command_draft"]
        assert draft["scope_ref"] == scope_ref
        assert draft["draft"] == command
        assert draft["status"] == "draft"
        assert draft["executed"] is False
        assert draft["source_proposal_ref"] == proposal["proposal_ref"]

        replay = human.convert_agent_proposal_to_command_draft(
            proposal["proposal_ref"],
            expected_scope_ref=scope_ref,
            expected_proposal_hash=canonical_hash(command),
            idempotency_key="proposal-to-command-convert",
        )
        assert replay["proposal"]["proposal_ref"] == proposal["proposal_ref"]
        assert replay["command_draft"]["intent_id"] == draft["intent_id"]

        with pytest.raises(OwnerConflict, match="agent_proposal_stale"):
            human.convert_agent_proposal_to_command_draft(
                proposal["proposal_ref"],
                expected_scope_ref=scope_ref,
                expected_proposal_hash=canonical_hash(command),
                idempotency_key="proposal-to-command-duplicate",
            )
        persisted = human.query_collaboration_projection((scope_ref,))
        assert len(persisted["agent_proposals"]) == 1
        assert persisted["agent_proposals"][0]["status"] == "converted"
        assert len(persisted["commands"]) == 1
        assert persisted["commands"][0]["intent_id"] == draft["intent_id"]
        assert persisted["commands"][0]["source_proposal_ref"] == proposal[
            "proposal_ref"
        ]
    finally:
        runtime.close()


def test_all_human_collaboration_write_surfaces_reject_credentials(tmp_path) -> None:
    provider = _DeterministicDraftingProvider()
    runtime = _runtime(tmp_path / "collaboration-secret-rejection", provider)
    human = runtime.owners.human_collaboration
    scope_ref = "quest:quest_secret_rejection"
    try:
        with pytest.raises(OwnerConflict, match="companion_scope_required"):
            human.send_companion_message(
                "password=hunter2",
                "This message itself is safe.",
                "secret-scope-message",
            )
        with pytest.raises(OwnerConflict, match="idempotency_key_invalid"):
            human.send_companion_message(
                scope_ref,
                "This message itself is safe.",
                "password=hunter2",
            )
        with pytest.raises(OwnerConflict, match="idempotency_key_invalid"):
            human.create_quest({}, "password=hunter2")
        with pytest.raises(
            OwnerConflict, match="human_collaboration_secret_forbidden"
        ):
            human.send_companion_message(
                scope_ref,
                "token: ghp_examplecredential",
                "secret-companion-message",
            )
        with pytest.raises(
            OwnerConflict, match="human_collaboration_secret_forbidden"
        ):
            human.record_agent_proposal(
                scope_ref,
                {"proposal_kind": "narrow_scope", "client_secret": "value"},
                "secret-agent-proposal",
            )
        with pytest.raises(
            OwnerConflict, match="human_collaboration_secret_forbidden"
        ):
            human.record_soft_constraint(
                scope_ref,
                {
                    "text": "Use only public sources.",
                    "private_key": "-----BEGIN PRIVATE KEY-----",
                },
                "secret-soft-constraint",
            )
        with pytest.raises(
            OwnerConflict, match="human_collaboration_secret_forbidden"
        ):
            human.create_command_draft(
                scope_ref,
                {
                    "command_kind": "capability_authorization",
                    "payload": {
                        "capability": "network_access",
                        "decision": "granted",
                        "scope": {"token": "ghp_examplecredential"},
                    },
                },
                "secret-command-draft",
            )
        with runtime._database.read() as connection:
            assert connection.execute(
                text("SELECT COUNT(*) FROM hc_companion_sessions")
            ).scalar_one() == 0
            assert connection.execute(
                text("SELECT COUNT(*) FROM hc_companion_turns")
            ).scalar_one() == 0
    finally:
        runtime.close()


def test_companion_rejects_secret_provider_metadata_before_persistence(
    tmp_path: Path,
) -> None:
    class _SecretMetadataProvider(_DeterministicDraftingProvider):
        def reply(self, request) -> IntentTurnResult:
            self.intent_requests.append(request)
            return IntentTurnResult(
                reply="A safe visible reply.",
                native_session_ref="password=hunter2",
                adapter_kind="deterministic_test_adapter",
            )

    provider = _SecretMetadataProvider()
    runtime = _runtime(tmp_path / "secret-provider-metadata", provider)
    human = runtime.owners.human_collaboration
    scope_ref = "quest:quest_secret_provider_metadata"
    try:
        human.send_companion_message(
            scope_ref,
            "Explain the current status without changing it.",
            "secret-provider-metadata-message",
        )
        assert human.process_drafting_once()
        turn = human.query_companion(scope_ref)["turns"][0]
        assert turn["assistant_status"] == "failed"
        assert turn["assistant_content"] is None
        with runtime._database.read() as connection:
            assert connection.execute(
                text(
                    "SELECT native_session_ref FROM hc_companion_sessions WHERE "
                    "scope_ref = :scope_ref"
                ),
                {"scope_ref": scope_ref},
            ).scalar_one() is None
            assert connection.execute(
                text(
                    "SELECT adapter_kind FROM hc_companion_turns WHERE "
                    "session_ref = (SELECT session_ref FROM hc_companion_sessions "
                    "WHERE scope_ref = :scope_ref)"
                ),
                {"scope_ref": scope_ref},
            ).scalar_one() is None
    finally:
        runtime.close()


def test_command_revision_stales_owner_preview_and_confirmation_is_not_auth(
    tmp_path: Path,
) -> None:
    provider = _DeterministicDraftingProvider()
    runtime = _runtime(tmp_path / "command-ladder", provider)
    human = runtime.owners.human_collaboration
    scope_ref = "quest:quest_command_ladder"
    try:
        initial_authorization_count = human.query_snapshot().facts[
            "authorization_count"
        ]
        command = {
            "command_kind": "capability_authorization",
            "payload": {
                "capability": "external_publish",
                "decision": "granted",
                "scope": {
                    "destination": "https://example.invalid/publication",
                    "asset_ref": "asset_publication_1",
                },
            },
        }
        drafted = human.create_command_draft(
            scope_ref,
            command,
            "command-draft-create-1",
        )
        assert drafted["status"] == "draft"
        assert drafted["executed"] is False

        previewed = human.preview_command(
            drafted["intent_id"],
            drafted["draft_revision"],
            drafted["draft_hash"],
            "command-preview-1",
        )
        preview = previewed["impact_preview"]
        assert preview["status"] == "current"
        assert {item["source_owner"] for item in preview["owner_previews"]} == {
            "human_collaboration",
        }
        for item in preview["owner_previews"]:
            assert item["target_assertion"]["operation"] == (
                "decide_capability_authorization"
            )
            assert item["target_assertion"]["capability"] == "external_publish"
            assert item["will_happen"]
            assert item["will_not_happen"]
            assert item["stale_conditions"]
            assert item["digest"]

        revised_command = {
            **command,
            "payload": {
                **command["payload"],
                "scope": {
                    **command["payload"]["scope"],
                    "asset_ref": "asset_publication_2",
                },
            },
        }
        revised = human.revise_command_draft(
            drafted["intent_id"],
            drafted["draft_revision"],
            revised_command,
            "command-draft-revise-1",
        )
        assert revised["draft_revision"] == drafted["draft_revision"] + 1
        assert revised["draft_hash"] != drafted["draft_hash"]

        with pytest.raises(OwnerConflict, match="command_preview_stale"):
            human.confirm_command(
                drafted["intent_id"],
                drafted["draft_revision"],
                drafted["draft_hash"],
                preview["preview_ref"],
                preview["preview_hash"],
                "command-confirm-stale-1",
            )

        refreshed = human.preview_command(
            revised["intent_id"],
            revised["draft_revision"],
            revised["draft_hash"],
            "command-preview-2",
        )["impact_preview"]
        confirmed = human.confirm_command(
            revised["intent_id"],
            revised["draft_revision"],
            revised["draft_hash"],
            refreshed["preview_ref"],
            refreshed["preview_hash"],
            "command-confirm-1",
        )
        assert confirmed["confirmation_receipt"]["status"] == "accepted"
        assert confirmed["executed"] is False
        assert human.query_snapshot().facts["authorization_count"] == (
            initial_authorization_count
        )

        authorization = human.decide_capability_authorization(
            scope_ref,
            {
                "capability": "external_publish",
                "decision": "granted",
                "scope": {
                    "destination": "https://example.invalid/publication",
                    "asset_ref": "asset_publication_2",
                },
                "confirmation_receipt_ref": confirmed["confirmation_receipt"][
                    "receipt_ref"
                ],
            },
            "capability-authorization-1",
        )
        assert authorization["decision"] == "granted"
        assert authorization["authorization_ref"]
        assert authorization["receipt_ref"] != confirmed["confirmation_receipt"][
            "receipt_ref"
        ]
        assert human.query_snapshot().facts["authorization_count"] == (
            initial_authorization_count + 1
        )
    finally:
        runtime.close()


def test_late_confirmed_grant_cannot_override_newer_committed_revoke(
    tmp_path: Path,
) -> None:
    provider = _DeterministicDraftingProvider()
    runtime = _runtime(tmp_path / "late-confirmed-grant", provider)
    human = runtime.owners.human_collaboration
    scope_ref = "quest:quest_late_confirmed_grant"
    capability = "external_publish"
    capability_scope = {
        "destination": "https://example.invalid/publication",
        "asset_ref": "asset_publication_late_grant",
    }
    try:
        old_grant = _confirm_capability_command(
            human,
            scope_ref=scope_ref,
            capability=capability,
            decision="granted",
            capability_scope=capability_scope,
            key="old-grant",
        )
        newer_revoke = _confirm_capability_command(
            human,
            scope_ref=scope_ref,
            capability=capability,
            decision="revoked",
            capability_scope=capability_scope,
            key="newer-revoke",
        )

        revoke = human.decide_capability_authorization(
            scope_ref,
            {
                "capability": capability,
                "decision": "revoked",
                "scope": capability_scope,
                "confirmation_receipt_ref": newer_revoke["confirmation_receipt"][
                    "receipt_ref"
                ],
            },
            "newer-revoke-authorization",
        )
        assert revoke["decision"] == "revoked"
        assert revoke["is_current"] is True

        with pytest.raises(OwnerConflict, match="authorization_confirmation_stale"):
            human.decide_capability_authorization(
                scope_ref,
                {
                    "capability": capability,
                    "decision": "granted",
                    "scope": capability_scope,
                    "confirmation_receipt_ref": old_grant["confirmation_receipt"][
                        "receipt_ref"
                    ],
                },
                "old-grant-late-authorization",
            )

        current = human.query_command(newer_revoke["intent_id"])["authorization"]
        assert current["authorization_ref"] == revoke["authorization_ref"]
        assert current["decision"] == "revoked"
        assert current["is_current"] is True
        assert "authorization" not in human.query_command(old_grant["intent_id"])
    finally:
        runtime.close()


def test_authorization_head_cannot_be_rolled_back_by_mutable_current_flags(
    tmp_path: Path,
) -> None:
    provider = _DeterministicDraftingProvider()
    runtime = _runtime(tmp_path / "authorization-head-integrity", provider)
    human = runtime.owners.human_collaboration
    scope_ref = "quest:quest_authorization_head_integrity"
    requirement = {
        "capability": "external_publish",
        "scope": {"destination": "https://example.invalid/head-integrity"},
    }
    try:
        grant_confirmation = _confirm_capability_command(
            human,
            scope_ref=scope_ref,
            capability=cast(str, requirement["capability"]),
            decision="granted",
            capability_scope=cast(dict[str, object], requirement["scope"]),
            key="head-integrity-grant",
        )
        grant = human.decide_capability_authorization(
            scope_ref,
            {
                **requirement,
                "decision": "granted",
                "confirmation_receipt_ref": grant_confirmation[
                    "confirmation_receipt"
                ]["receipt_ref"],
            },
            "head-integrity-grant-authorization",
        )
        revoke_confirmation = _confirm_capability_command(
            human,
            scope_ref=scope_ref,
            capability=cast(str, requirement["capability"]),
            decision="revoked",
            capability_scope=cast(dict[str, object], requirement["scope"]),
            key="head-integrity-revoke",
        )
        revoke = human.decide_capability_authorization(
            scope_ref,
            {
                **requirement,
                "decision": "revoked",
                "confirmation_receipt_ref": revoke_confirmation[
                    "confirmation_receipt"
                ]["receipt_ref"],
            },
            "head-integrity-revoke-authorization",
        )
        assert grant["revision"] == 1
        assert revoke["revision"] == 2

        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE hc_capability_authorizations SET is_current = 0 WHERE "
                    "authorization_ref = :revoke_ref"
                ),
                {
                    "revoke_ref": revoke["authorization_ref"],
                },
            )
            connection.execute(
                text(
                    "UPDATE hc_capability_authorizations SET is_current = 1 WHERE "
                    "authorization_ref = :grant_ref"
                ),
                {"grant_ref": grant["authorization_ref"]},
            )

        with pytest.raises(
            OwnerConflict, match="capability_authorization_receipt_invalid"
        ):
            human._fact_verifier.verify_capability_authorization(
                requirement=requirement,
                receipt_ref=grant["receipt_ref"],
            )
        with pytest.raises(
            OwnerConflict, match="capability_authorization_receipt_invalid"
        ):
            human.query_command(grant_confirmation["intent_id"])
    finally:
        runtime.close()


def test_direct_authorization_rejects_tampered_confirmation_receipt(
    tmp_path: Path,
) -> None:
    provider = _DeterministicDraftingProvider()
    runtime = _runtime(tmp_path / "tampered-command-confirmation", provider)
    human = runtime.owners.human_collaboration
    scope_ref = "quest:quest_tampered_command_confirmation"
    capability_scope = {
        "destination": "https://example.invalid/publication",
        "asset_ref": "asset_publication_tampered_confirmation",
    }
    try:
        confirmed = _confirm_capability_command(
            human,
            scope_ref=scope_ref,
            capability="external_publish",
            decision="granted",
            capability_scope=capability_scope,
            key="tampered-confirmation",
        )
        confirmation_ref = confirmed["confirmation_receipt"]["receipt_ref"]
        authorization_count = human.query_snapshot().facts["authorization_count"]

        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE hc_command_confirmations SET receipt_hash = :receipt_hash "
                    "WHERE confirmation_ref = :confirmation_ref"
                ),
                {
                    "receipt_hash": "f" * 64,
                    "confirmation_ref": confirmation_ref,
                },
            )

        with pytest.raises(OwnerConflict, match="authorization_confirmation_invalid"):
            human.decide_capability_authorization(
                scope_ref,
                {
                    "capability": "external_publish",
                    "decision": "granted",
                    "scope": capability_scope,
                    "confirmation_receipt_ref": confirmation_ref,
                },
                "tampered-confirmation-authorization",
            )
        assert human.query_snapshot().facts["authorization_count"] == (
            authorization_count
        )
    finally:
        runtime.close()


def test_quest_confirmation_issues_independent_durable_broad_authorization(
    tmp_path: Path,
) -> None:
    provider = _DeterministicDraftingProvider()
    data_path = tmp_path / "broad-research-authorization"
    runtime = _runtime(data_path, provider)
    confirmed = _confirm_direct_quest(runtime)
    initialization_id = confirmed["initialization_id"]
    confirmation_receipt_ref = confirmed["receipts"]["human_confirmation"][
        "receipt_ref"
    ]
    human = runtime.owners.human_collaboration
    authorization_count_before = human.query_snapshot().facts[
        "authorization_count"
    ]
    assert human.reconcile_once()  # Research Graph accepts the Quest first.
    quest_accepted = human.query_quest_creation(initialization_id)
    quest_ref = quest_accepted["quest_ref"]
    assert quest_accepted["receipts"]["quest_goal"]["status"] == "accepted"
    assert quest_accepted["receipts"]["broad_research_authorization"][
        "status"
    ] == "not_attempted"
    assert human.query_broad_research_authorization(quest_ref) is None
    runtime.close()

    # The next durable boundary commits authorization independently. Simulate
    # an ACK loss by closing without reading its projected fact.
    restarted = _runtime(data_path, provider)
    try:
        human = restarted.owners.human_collaboration
        assert human.reconcile_once()
    finally:
        restarted.close()

    recovered_again = _runtime(data_path, provider)
    try:
        human = recovered_again.owners.human_collaboration
        authorization = human.query_broad_research_authorization(quest_ref)
        assert authorization is not None
        assert authorization["status"] == "granted"
        assert authorization["quest_ref"] == quest_ref
        assert authorization["authorization_kind"] == "broad_research"
        assert authorization["receipt_ref"] != confirmation_receipt_ref
        assert authorization["policy"][
            "ordinary_reversible_local_research"
        ] == "allowed_without_additional_confirmation"
        assert set(authorization["policy"]["requires_new_confirmation"]) == {
            "scope_expansion",
            "external_publish_or_send",
            "irreversible_operation",
            "delete_or_destruct_user_data",
            "high_risk_operation",
        }
        receipt_ref = authorization["receipt_ref"]
        assert human.query_snapshot().facts["authorization_count"] == (
            authorization_count_before + 1
        )
        for _step in range(8):
            if not human.reconcile_once():
                break
        completed = human.query_quest_creation(initialization_id)
        assert completed["status"] == "completed"
        assert completed["receipts"]["broad_research_authorization"][
            "receipt_ref"
        ] == receipt_ref
        assert human.query_snapshot().facts["authorization_count"] == (
            authorization_count_before + 1
        )
    finally:
        recovered_again.close()

    replayed_runtime = _runtime(data_path, provider)
    try:
        human = replayed_runtime.owners.human_collaboration
        replay = human.query_broad_research_authorization(quest_ref)
        assert replay is not None
        assert replay["receipt_ref"] == receipt_ref
        assert not human.reconcile_once()
        assert human.query_snapshot().facts["authorization_count"] == (
            authorization_count_before + 1
        )
    finally:
        replayed_runtime.close()


def test_exact_human_revoke_disables_durable_broad_research_authorization(
    tmp_path: Path,
) -> None:
    provider = _DeterministicDraftingProvider()
    data_path = tmp_path / "broad-research-revocation"
    runtime = _runtime(data_path, provider)
    human = runtime.owners.human_collaboration
    try:
        confirmed = _confirm_direct_quest(runtime)
        assert human.reconcile_once()
        quest_ref = human.query_quest_creation(confirmed["initialization_id"])[
            "quest_ref"
        ]
        assert human.reconcile_once()
        issued = human.query_broad_research_authorization(quest_ref)
        assert issued is not None
        assert issued["decision"] == "granted"
        for _step in range(8):
            if not human.reconcile_once():
                break
        assert human.query_quest_creation(confirmed["initialization_id"])[
            "status"
        ] == "completed"
        assert runtime.projection.query_snapshot()["human_collaboration"][
            "companion"
        ]["scope_ref"] == f"quest:{quest_ref}"
        pending_creation = human.create_quest({}, "second-quest-draft-open")
        assert pending_creation["initialization_id"] != confirmed["initialization_id"]
        assert runtime.projection.query_snapshot()["human_collaboration"][
            "companion"
        ]["scope_ref"] == f"quest:{quest_ref}"

        scope_ref = f"quest:{quest_ref}"
        override_scope = {"quest_ref": quest_ref}
        confirmed_revoke = _confirm_capability_command(
            human,
            scope_ref=scope_ref,
            capability="broad_research",
            decision="revoked",
            capability_scope=override_scope,
            key="broad-research-revoke",
        )
        revoked = human.decide_capability_authorization(
            scope_ref,
            {
                "capability": "broad_research",
                "decision": "revoked",
                "scope": override_scope,
                "confirmation_receipt_ref": confirmed_revoke[
                    "confirmation_receipt"
                ]["receipt_ref"],
            },
            "broad-research-revoke-authorization",
        )
        assert revoked["decision"] == "revoked"
        assert revoked["is_current"] is True
        effective = human.query_broad_research_authorization(quest_ref)
        assert effective is not None
        assert effective["decision"] == "granted"
        assert effective["receipt_ref"] == issued["receipt_ref"]
        assert effective["effective_decision"] == "revoked"
        assert effective["effective_authorization"]["receipt_ref"] == revoked[
            "receipt_ref"
        ]

        # The independently accepted Quest and issuance receipt remain historical
        # facts.  Revocation only closes the current capability gate; it must not
        # turn initialization into recovery or cause HC to re-issue the grant.
        creation = human.query_quest_creation(confirmed["initialization_id"])
        assert creation["status"] == "completed"
        assert creation["receipts"]["quest_goal"]["status"] == "accepted"
        assert creation["receipts"]["broad_research_authorization"][
            "status"
        ] == "accepted"
        assert creation["receipts"]["broad_research_authorization"][
            "effective_decision"
        ] == "revoked"
        assert not human.reconcile_once()
        snapshot = runtime.projection.query_snapshot()
        assert snapshot["human_collaboration"]["companion"][
            "scope_ref"
        ] == f"quest:{quest_ref}"
        projected_issuance = next(
            item
            for item in snapshot["human_collaboration"]["commands"][
                "authorizations"
            ]
            if item["authorization_kind"] == "broad_research"
        )
        assert projected_issuance["is_current"] is True
        assert projected_issuance["receipt_ref"] == issued["receipt_ref"]
        assert projected_issuance["effective_decision"] == "revoked"
        assert projected_issuance["effective_authorization"]["receipt_ref"] == (
            revoked["receipt_ref"]
        )
    finally:
        runtime.close()

    restarted = _runtime(data_path, provider)
    try:
        human = restarted.owners.human_collaboration
        effective = human.query_broad_research_authorization(quest_ref)
        assert effective is not None
        assert effective["receipt_ref"] == issued["receipt_ref"]
        assert effective["effective_decision"] == "revoked"
        creation = human.query_quest_creation(confirmed["initialization_id"])
        assert creation["status"] == "completed"
        assert creation["receipts"]["broad_research_authorization"][
            "effective_decision"
        ] == "revoked"
        projected = restarted.projection.query_snapshot()["human_collaboration"]
        projected_issuance = next(
            item
            for item in projected["commands"]["authorizations"]
            if item["authorization_kind"] == "broad_research"
        )
        assert projected_issuance["receipt_ref"] == issued["receipt_ref"]
        assert projected_issuance["effective_decision"] == "revoked"
        assert not human.reconcile_once()
    finally:
        restarted.close()
