from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import subprocess

import pytest
from sqlalchemy import text

from meta_research.bundle_protocol import projection_plain_value
from meta_research.composition import build_production_runtime
from meta_research.database import Database
from meta_research.feed import DurableFeed
from meta_research.harness import (
    TARGET_ROOT_LIFECYCLE_PHASE,
    HarnessAdmissionError,
    HarnessRuntime,
    TargetHarnessRequest,
)
from meta_research.harness_adapters import (
    HARNESS_CAPABILITIES,
    CodexHarnessAdapter,
)
from meta_research.migration import upgrade_database
from meta_research.owners.agent_runtime_harness import (
    TARGET_ROOT_RECOVERY_READY_CODE,
    AgentRuntimeHarnessError,
    AgentRuntimeHarnessRetryLater,
    SQLiteAgentRuntimeHarness,
)
from meta_research.owners.common import canonical_hash, canonical_json
from meta_research.paths import prepare_data_root
from meta_research.semantic_owner_gateway import TARGET_ROOT_SEMANTIC_OPERATION_IDS
from meta_research.semantic_mcp import (
    ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS,
    SemanticMcpError,
)
from test_harness_target_root import (
    _Gateway,
    _TargetRootAdapter,
)
import test_public_bundle_stage as bundle_fixtures
from test_root_human_request_lifecycle import _OperationBoundBundleSkill
from test_target_root_finalizer import _admit_independent_target_root
from test_target_run_owner import _records, _seed_admitted_launch


class _Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def time(self) -> float:
        return self.now


class _TargetHumanRequestRunner:
    """Codex process seam that uses the authenticated resident MCP channel."""

    def __init__(
        self,
        *,
        fail_first_after_open: bool = False,
        conflicting_session_after_open: bool = False,
    ) -> None:
        self.runtime = None
        self.fail_first_after_open = fail_first_after_open
        self.conflicting_session_after_open = conflicting_session_after_open
        self.provider_calls: list[tuple[list[str], str, dict[str, str]]] = []
        self.request_ref: str | None = None
        self.resolution: dict[str, object] | None = None

    def __call__(
        self,
        argv: list[str],
        prompt: str,
        timeout: float | None,
        environment: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        assert timeout is None or timeout > 0
        if "--version" in argv:
            return subprocess.CompletedProcess(
                argv, 0, "codex-cli 0.153.2\n", ""
            )
        if argv[-2:] == ["features", "list"]:
            return subprocess.CompletedProcess(
                argv,
                0,
                "\n".join(
                    f"{name} stable true"
                    for name in (
                        "hooks",
                        "multi_agent",
                        "plugins",
                        "remote_plugin",
                        "shell_tool",
                        "skill_search",
                        "unified_exec",
                    )
                )
                + "\n",
                "",
            )

        assert self.runtime is not None
        self.provider_calls.append((list(argv), prompt, dict(environment)))
        token = environment["META_RESEARCH_MCP_TOKEN"]
        if len(self.provider_calls) == 1:
            result = self._tool_call(
                token,
                operation_id=ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[0],
                arguments={
                    "effect_id": "target-provider-human-request",
                    "request_kind": "offline_action",
                    "obligation": "Choose the safe offline action for this Target.",
                    "business_purpose": "Resume this exact Target root task.",
                    "condition": {
                        "impact": "Only this Target task is paused.",
                        "safe_response": "Decline or defer without secrets.",
                    },
                    "acceptance_conditions": [
                        "The response is bound to this exact request."
                    ],
                },
                request_id=1,
            )
            self.request_ref = str(result["request_ref"])
        else:
            result = self._tool_call(
                token,
                operation_id=ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[1],
                arguments={"effect_id": "target-provider-human-request"},
                request_id=2,
            )
            resolution = result.get("resolution")
            assert isinstance(resolution, dict)
            self.resolution = resolution

        stream = [
            {
                "type": "thread.started",
                "thread_id": "codex-target-human-request-session",
            },
        ]
        if self.conflicting_session_after_open and len(self.provider_calls) == 1:
            stream.append(
                {
                    "type": "thread.started",
                    "thread_id": "codex-target-conflicting-session",
                }
            )
        stream.extend(
            (
            {
                "type": "item.completed",
                "item": {"type": "mcp_tool_call", "server": "meta_research"},
            },
            {"type": "turn.completed"},
            )
        )
        failed_after_open = (
            self.fail_first_after_open and len(self.provider_calls) == 1
        )
        return subprocess.CompletedProcess(
            argv,
            1 if failed_after_open else 0,
            "\n".join(json.dumps(item) for item in stream) + "\n",
            "provider exited after opening HumanRequest" if failed_after_open else "",
        )

    def _tool_call(
        self,
        token: str,
        *,
        operation_id: str,
        arguments: dict[str, object],
        request_id: int,
    ) -> dict[str, object]:
        status, payload, _session_id = self.runtime.harnesses.dispatch_mcp_http(
            token,
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": operation_id, "arguments": arguments},
            },
            mcp_session_id="codex-target-human-request-session",
        )
        assert status == 200
        assert payload is not None
        result = payload["result"]
        assert isinstance(result, dict)
        assert result["isError"] is False
        structured = result["structuredContent"]
        assert isinstance(structured, dict)
        return structured


class _OperationBoundAdvisoryBundleSkill(_OperationBoundBundleSkill):
    """Current deterministic Bundle seam with no fabricated child review."""

    def review_draft(self, request, draft):
        return replace(
            super().review_draft(request, draft),
            review_mode="advisory_unobserved",
            reviewer_agent_ref=None,
        )


@pytest.fixture
def immediate_target_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "meta_research.owners.agent_runtime_harness."
        "_TARGET_ROOT_RETRY_BASE_SECONDS",
        0.0,
    )


def _owner_with_failed_admission(
    path: Path,
) -> tuple[Database, SQLiteAgentRuntimeHarness, TargetHarnessRequest]:
    upgrade_database(path)
    _candidate, _formal_plan, handle, _preflight, launch_request = _records()
    _seed_admitted_launch(path, launch_request, handle)
    database = Database(path)
    owner = SQLiteAgentRuntimeHarness(database, DurableFeed(database))
    full_conformance = {"contract_ref": "test/full-conformance/v1"}
    request = TargetHarnessRequest(
        request_ref="target-harness-request:failed-recovery",
        harness_family="codex",
        model_ref="gpt-target-root",
        auth_profile_ref="harness-profile:target-root",
        required_operation_ids=TARGET_ROOT_SEMANTIC_OPERATION_IDS,
        required_capabilities=HARNESS_CAPABILITIES,
        target_ref=handle.target_ref,
        target_run_ref=handle.target_run_ref,
        full_conformance_binding=full_conformance,
        full_conformance_binding_hash=canonical_hash(full_conformance),
        target_scope_binding_hash="c" * 64,
        provider_operation_timeout_seconds=30 * 24 * 60 * 60,
    )
    reserved = owner.reserve_admission(
        request=request.as_dict(),
        idempotency_key=request.request_ref,
        request_hash=canonical_hash(request.as_dict()),
        capability_binding_hash="b" * 64,
        authoritative_run_ref=handle.target_run_ref,
    )
    owner.fail_admission(reserved.run_ref, "mcp_channel_unavailable")
    return database, owner, request


def _activate_channel(
    owner: SQLiteAgentRuntimeHarness,
    request: TargetHarnessRequest,
):
    run = owner.query_run(request.request_ref)
    assert run is not None
    operation_bindings = [
        {"semantic_operation_id": operation_id}
        for operation_id in request.required_operation_ids
    ]
    mcp_binding = {
        "server_instance_ref": "mcp-server:failed-recovery",
        "endpoint_ref": "/mcp",
        "catalog_revision": 1,
        "catalog_hash": "a" * 64,
        "health_receipt_ref": "mcp-health:failed-recovery",
        "connection_grant_ref": "grant:failed-recovery",
        "operation_bindings": operation_bindings,
    }
    scope = {
        "run_ref": run.run_ref,
        "attempt_ref": run.attempt_ref,
        "root_session_ref": run.root_session_ref,
        "fence_ref": run.fence_ref,
        "capability_binding_hash": run.capability_binding_hash,
        "operation_ids": list(request.required_operation_ids),
    }
    return owner.activate_admission(
        run_ref=run.run_ref,
        mcp_binding=mcp_binding,
        grant_ref="grant:failed-recovery",
        server_instance_ref="mcp-server:failed-recovery",
        token_hash="d" * 64,
        scope=scope,
    )


def _replace_channel(
    owner: SQLiteAgentRuntimeHarness,
    request: TargetHarnessRequest,
    *,
    suffix: str,
):
    run = owner.query_run(request.request_ref)
    assert run is not None
    grant_ref = f"grant:failed-recovery:{suffix}"
    server_ref = f"mcp-server:failed-recovery:{suffix}"
    mcp_binding = {
        "server_instance_ref": server_ref,
        "endpoint_ref": "/mcp",
        "catalog_revision": 1,
        "catalog_hash": "a" * 64,
        "health_receipt_ref": f"mcp-health:failed-recovery:{suffix}",
        "connection_grant_ref": grant_ref,
        "operation_bindings": [
            {"semantic_operation_id": operation_id}
            for operation_id in request.required_operation_ids
        ],
    }
    scope = {
        "run_ref": run.run_ref,
        "attempt_ref": run.attempt_ref,
        "root_session_ref": run.root_session_ref,
        "fence_ref": run.fence_ref,
        "capability_binding_hash": run.capability_binding_hash,
        "operation_ids": list(request.required_operation_ids),
    }
    return owner.replace_channel(
        run_ref=run.run_ref,
        mcp_binding=mcp_binding,
        grant_ref=grant_ref,
        server_instance_ref=server_ref,
        token_hash=canonical_hash({"suffix": suffix}),
        scope=scope,
    )


def _owner_with_active_root(
    path: Path,
) -> tuple[Database, SQLiteAgentRuntimeHarness, TargetHarnessRequest, object]:
    upgrade_database(path)
    candidate, formal_plan, base_handle, _preflight, launch_request = _records()
    _seed_admitted_launch(path, launch_request, base_handle)
    database = Database(path)
    owner = SQLiteAgentRuntimeHarness(database, DurableFeed(database))
    full_conformance = {"contract_ref": "test/full-conformance/v1"}
    request = TargetHarnessRequest(
        request_ref="target-harness-request:active-failed-recovery",
        harness_family="codex",
        model_ref="gpt-target-root",
        auth_profile_ref="harness-profile:target-root",
        required_operation_ids=TARGET_ROOT_SEMANTIC_OPERATION_IDS,
        required_capabilities=HARNESS_CAPABILITIES,
        target_ref=base_handle.target_ref,
        target_run_ref=base_handle.target_run_ref,
        full_conformance_binding=full_conformance,
        full_conformance_binding_hash=canonical_hash(full_conformance),
        target_scope_binding_hash="c" * 64,
        provider_operation_timeout_seconds=30 * 24 * 60 * 60,
    )
    reserved = owner.reserve_admission(
        request=request.as_dict(),
        idempotency_key=request.request_ref,
        request_hash=canonical_hash(request.as_dict()),
        capability_binding_hash="b" * 64,
        authoritative_run_ref=base_handle.target_run_ref,
    )
    admitted = _activate_channel(owner, request)
    assert admitted.run_ref == reserved.run_ref
    handle = replace(
        base_handle,
        root_session_ref=admitted.root_session_ref,
        execution_attempt_ref=admitted.attempt_ref,
        execution_fence_ref=admitted.fence_ref,
    )
    handle_value = projection_plain_value(handle)
    candidate_value = projection_plain_value(candidate)
    formal_plan_value = projection_plain_value(formal_plan)
    with database.fenced_write() as connection:
        connection.execute(
            text(
                "INSERT INTO ar_target_root_lifecycles (lifecycle_ref, "
                "target_ref, launch_ref, target_run_ref, root_session_ref, "
                "target_attempt_ref, target_fence_ref, initial_handle_json, "
                "initial_handle_hash, candidate_json, candidate_hash, "
                "formal_plan_json, formal_plan_hash, status, completion_ref, "
                "idempotency_key, request_hash, created_at, updated_at) VALUES "
                "('target-root-lifecycle:failed-recovery', :target_ref, "
                "'target_launch_1', :target_run_ref, :root_session_ref, "
                ":attempt_ref, :fence_ref, :handle_json, :handle_hash, "
                ":candidate_json, :candidate_hash, :formal_plan_json, "
                ":formal_plan_hash, 'running', NULL, "
                "'target-root-lifecycle:failed-recovery', :request_hash, "
                "1.0, 1.0)"
            ),
            {
                "target_ref": handle.target_ref,
                "target_run_ref": handle.target_run_ref,
                "root_session_ref": handle.root_session_ref,
                "attempt_ref": handle.execution_attempt_ref,
                "fence_ref": handle.execution_fence_ref,
                "handle_json": canonical_json(handle_value),
                "handle_hash": canonical_hash(handle_value),
                "candidate_json": canonical_json(candidate_value),
                "candidate_hash": canonical_hash(candidate_value),
                "formal_plan_json": canonical_json(formal_plan_value),
                "formal_plan_hash": canonical_hash(formal_plan_value),
                "request_hash": "e" * 64,
            },
        )
        connection.execute(
            text(
                "INSERT INTO ar_target_frontier_entries (target_ref, "
                "launch_ref, target_spec_content_hash_ref, "
                "target_spec_receipt_ref, target_spec_receipt_subject_ref, "
                "state_revision, state, current_handle_json, "
                "current_handle_hash, terminal_fact_ref, currentness_known, "
                "current, updated_at) VALUES (:target_ref, 'target_launch_1', "
                ":spec_hash, :receipt_ref, :receipt_subject_ref, 1, 'running', "
                ":handle_json, :handle_hash, NULL, 1, 1, 1.0)"
            ),
            {
                "target_ref": handle.target_ref,
                "spec_hash": launch_request.target_spec_binding.content_hash_ref,
                "receipt_ref": (
                    launch_request.target_spec_acceptance_receipt.receipt_ref
                ),
                "receipt_subject_ref": (
                    launch_request.target_spec_acceptance_receipt.subject_ref
                ),
                "handle_json": canonical_json(handle_value),
                "handle_hash": canonical_hash(handle_value),
            },
        )
    return database, owner, request, handle


def test_recovery_preserves_human_request_suspension_without_session_checkpoint(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "suspended-target-restart")
    database, _owner, request, _handle = _owner_with_active_root(
        data_root.database
    )
    database.close()
    runtime = build_production_runtime(data_root)
    agent_runtime = runtime.owners.agent_runtime
    harness_owner = agent_runtime.harness_runs
    run = harness_owner.query_run(request.request_ref)
    assert run is not None
    operation_ref = f"{run.run_ref}:harness_turn:1"
    harness_owner.start_operation(
        run_ref=run.run_ref,
        operation_ref=operation_ref,
        generation=1,
        invocation_hash="9" * 64,
        resume=False,
    )
    binding = {
        "quest_ref": "quest_1",
        "task_ref": run.run_ref,
        "root_session_ref": run.root_session_ref,
        "operation_id": ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[0],
        "attempt_ref": run.attempt_ref,
        "generation": run.attempt_generation,
        "request_owner": "agent_runtime",
        "root_kind": "target",
        "phase": TARGET_ROOT_LIFECYCLE_PHASE,
        "fence_ref": run.fence_ref,
        "runtime_binding_hash": run.capability_binding_hash,
    }
    target = {
        "schema_ref": "meta-research/root-agent-human-request-target/v1",
        "root": {
            "run_kind": "target",
            "run_ref": run.run_ref,
            "attempt_ref": run.attempt_ref,
            "root_session_ref": run.root_session_ref,
            "fence_ref": run.fence_ref,
            "waiter_generation": run.attempt_generation,
        },
        "condition": {"route": "literature_access"},
    }
    try:
        human_request = agent_runtime.open_human_request_effect(
            effect_key="mcp-effect:target-restart-human-request",
            effect_id="target-restart-human-request",
            operation_binding=binding,
            predecessor_request_ref=None,
            request_kind="library_reconnect",
            obligation="Select a safe literature route for this Target task.",
            business_purpose="Resume the exact suspended Target task.",
            target_assertion=target,
            acceptance_conditions=("A safe current route is selected.",),
            direct_waiter={
                "waiter_ref": f"root_run:{run.run_ref}",
                "generation": run.attempt_generation,
                "target_assertion": target,
                "wait_scope": "local",
                "other_blockers": [],
            },
            quest_ref="quest_1",
        )
        assert harness_owner.query_run(request.request_ref).status == "suspended"

        restarted_database = Database(data_root.database)
        restarted_owner = SQLiteAgentRuntimeHarness(
            restarted_database,
            DurableFeed(restarted_database),
        )
        try:
            recovered = restarted_owner.query_run(request.request_ref)
            recovered_operation = restarted_owner.latest_operation(run.run_ref)
            assert recovered is not None and recovered.status == "suspended"
            assert recovered_operation is not None
            assert recovered_operation.status == "unknown_outcome"
        finally:
            restarted_database.close()

        runtime.owners.human_collaboration.respond_to_human_request(
            str(human_request["request_ref"]),
            decision="provided",
            facts={"route": "oa_only"},
            note="Continue with lawful open-access sources.",
            idempotency_key="target-restart-human-response",
        )
        disposed = agent_runtime.query_human_request(
            str(human_request["request_ref"])
        )
        assert disposed is not None
        assert disposed["status"] == "open"
        assert disposed["direct_waiters"][0]["status"] == "blocked"
        assert harness_owner.query_run(request.request_ref).status == "suspended"
    finally:
        runtime.close()


@pytest.mark.parametrize(
    (
        "fail_first_after_open",
        "restart_after_response",
        "conflicting_session_after_open",
        "resumable",
    ),
    (
        (False, False, False, True),
        (False, True, False, True),
        (True, True, False, True),
        (False, False, True, False),
    ),
    ids=(
        "normal-same-process",
        "normal-restart",
        "nonzero-after-open-restart",
        "conflicting-session-after-open",
    ),
)
def test_target_provider_return_checkpoints_session_then_resumes_once(
    tmp_path: Path,
    fail_first_after_open: bool,
    restart_after_response: bool,
    conflicting_session_after_open: bool,
    resumable: bool,
) -> None:
    data_root = prepare_data_root(tmp_path / "target-provider-human-request")
    runner = _TargetHumanRequestRunner(
        fail_first_after_open=fail_first_after_open,
        conflicting_session_after_open=conflicting_session_after_open,
    )
    codex_workspace = data_root.run / "target-human-request-harness"
    drafting = bundle_fixtures._DeterministicDraftingAdapter()
    bundle_provider = _OperationBoundAdvisoryBundleSkill()
    setup_runtime = build_production_runtime(
        data_root,
        proposal_drafter=drafting,
        intent_drafting_provider=drafting,
        host_compute_probe=bundle_fixtures._DeterministicProbe(),
        idea_skill_provider=bundle_fixtures._DeterministicIdeaSkill(),
        plan_skill_provider=bundle_fixtures._DeterministicPlanSkill(
            no_gap=False
        ),
        bundle_skill_provider=bundle_provider,
        harness_adapters=(
            bundle_fixtures._FullConformanceAdapter("codex"),
            bundle_fixtures._FullConformanceAdapter("claude"),
        ),
        power_inhibitor=bundle_fixtures._TogglePowerInhibitor(),
        startup_power_probe=False,
        startup_harness_diagnostics=False,
    )
    bundle_provider.bind(setup_runtime.harnesses)
    setup_runtime.owners.research_graph._target_candidate_proof_verifier = (  # type: ignore[attr-defined]
        bundle_fixtures._AcceptingTargetCandidateProofVerifier()
    )
    setup_runtime.harnesses.start_full_conformance(
        bundle_fixtures._full_request()
    )
    for _turn in range(4):
        if setup_runtime.harnesses.query_status()["status"] == "ready":
            break
        assert setup_runtime.harnesses.advance_full_conformance(
            mcp_base_url="http://127.0.0.1:8999"
        )
    assert setup_runtime.harnesses.query_status()["status"] == "ready"
    _target, _candidate, _formal_plan, admission, _handle = (
        _admit_independent_target_root(setup_runtime)
    )
    request_ref = admission.run.request_ref
    target_run_ref = admission.run.run_ref
    setup_runtime.close()

    runtime = build_production_runtime(
        data_root,
        harness_adapters=(
            CodexHarnessAdapter(
                codex_workspace,
                runner=runner,
                target_root_timeout_seconds=30 * 24 * 60 * 60,
            ),
            bundle_fixtures._FullConformanceAdapter("claude"),
        ),
        power_inhibitor=bundle_fixtures._TogglePowerInhibitor(),
        startup_power_probe=False,
        startup_harness_diagnostics=False,
    )
    runner.runtime = runtime
    prompt = "Continue the exact Target root task through completion."
    first_flow_completed = False
    try:
        with pytest.raises(HarnessAdmissionError, match="runtime_run_suspended"):
            runtime.harnesses.run_or_resume_target_root(
                request_ref,
                prompt=prompt,
                mcp_base_url="http://127.0.0.1:8999",
            )

        suspended = runtime.owners.agent_runtime.harness_runs.query_run(
            request_ref
        )
        first_operation = (
            runtime.owners.agent_runtime.harness_runs.latest_operation(
                target_run_ref
            )
        )
        assert suspended is not None
        assert suspended.status == "suspended"
        assert suspended.native_session_ref == (
            "codex-target-human-request-session" if resumable else None
        )
        assert suspended.failure_code == (
            "human_request_wait" if resumable else None
        )
        assert first_operation is not None
        assert (first_operation.status, first_operation.outcome_code) == (
            ("executed", "human_request_wait")
            if resumable
            else ("running", None)
        )
        assert (
            runtime.owners.agent_runtime.harness_runs.query_profile(
                target_run_ref
            )
            is None
        )
        assert runner.request_ref is not None

        response = runtime.owners.human_collaboration.respond_to_human_request(
            runner.request_ref,
            decision="deferred",
            facts={"safe_route": "continue_without_offline_action"},
            note="Continue with the safe in-process alternative.",
            idempotency_key="target-provider-human-response",
        )
        resumed = runtime.owners.agent_runtime.harness_runs.query_run(
            request_ref
        )
        assert resumed is not None
        if not resumable:
            assert resumed.status == "suspended"
            waiting = runtime.owners.agent_runtime.query_human_request(
                runner.request_ref
            )
            assert waiting is not None and waiting["status"] == "open"
            assert waiting["direct_waiters"][0]["status"] == "blocked"
            assert len(runner.provider_calls) == 1
            return
        assert resumed.status == "running"
        first_flow_completed = True
    finally:
        if restart_after_response or not first_flow_completed:
            runtime.close()

    restarted = (
        build_production_runtime(
            data_root,
            harness_adapters=(
                CodexHarnessAdapter(
                    codex_workspace,
                    runner=runner,
                    target_root_timeout_seconds=30 * 24 * 60 * 60,
                ),
                bundle_fixtures._FullConformanceAdapter("claude"),
            ),
            power_inhibitor=bundle_fixtures._TogglePowerInhibitor(),
            startup_power_probe=False,
            startup_harness_diagnostics=False,
        )
        if restart_after_response
        else runtime
    )
    runner.runtime = restarted
    try:
        completed = restarted.harnesses.run_or_resume_target_root(
            request_ref,
            prompt=prompt,
            mcp_base_url="http://127.0.0.1:8999",
        )
        replayed_response = (
            restarted.owners.human_collaboration.respond_to_human_request(
                runner.request_ref,
                decision="deferred",
                facts={"safe_route": "continue_without_offline_action"},
                note="Continue with the safe in-process alternative.",
                idempotency_key="target-provider-human-response",
            )
        )

        operations = tuple(
            operation
            for operation in restarted.owners.agent_runtime.harness_runs.query_status_records()[
                1
            ]
            if operation.run_ref == target_run_ref
        )
        assert completed.status == "executed"
        assert completed.native_session_ref == (
            "codex-target-human-request-session"
        )
        assert replayed_response == response
        assert len(runner.provider_calls) == 2
        first_argv, _first_prompt, first_environment = runner.provider_calls[0]
        second_argv, second_prompt, second_environment = runner.provider_calls[1]
        assert first_argv[-1] == "-"
        assert second_argv[-3:] == [
            "resume",
            "codex-target-human-request-session",
            "-",
        ]
        assert "HumanRequest" in second_prompt
        assert first_environment["META_RESEARCH_PROVIDER_OPERATION_REF"] != (
            second_environment["META_RESEARCH_PROVIDER_OPERATION_REF"]
        )
        assert [item.generation for item in reversed(operations)] == [1, 2]
        assert [item.status for item in reversed(operations)] == [
            "executed",
            "executed",
        ]
        assert runner.resolution == {
            "response_ref": response["response_ref"],
            "decision": "deferred",
            "facts": {"safe_route": "continue_without_offline_action"},
            "note": "Continue with the safe in-process alternative.",
            "disposition": "unsatisfied",
            "reason_code": "human_deferred_exact_obligation",
            "accepted_evidence_refs": [],
        }
        profile = restarted.owners.agent_runtime.harness_runs.query_profile(
            target_run_ref
        )
        assert profile is not None
        assert profile["provider_operation_refs"] == [
            second_environment["META_RESEARCH_PROVIDER_OPERATION_REF"]
        ]
    finally:
        restarted.close()


def test_owner_reopens_failed_target_admission_once_without_rotating_identity(
    tmp_path: Path,
    immediate_target_retry: None,
) -> None:
    database, owner, request = _owner_with_failed_admission(
        tmp_path / "failed-target-admission.sqlite3"
    )
    try:
        failed = owner.query_run(request.request_ref)
        assert failed is not None and failed.status == "failed"
        identity = (
            failed.run_ref,
            failed.root_session_ref,
            failed.attempt_ref,
            failed.fence_ref,
            failed.attempt_generation,
        )

        recovered = owner.reopen_failed_target_root(request.request_ref)
        replayed = owner.reopen_failed_target_root(request.request_ref)

        assert recovered.reopened is True
        assert replayed.reopened is False
        assert recovered.run.status == "admitting"
        assert replayed.run.status == recovered.run.status
        assert replayed.run.failure_code == recovered.run.failure_code
        assert (
            recovered.run.run_ref,
            recovered.run.root_session_ref,
            recovered.run.attempt_ref,
            recovered.run.fence_ref,
            recovered.run.attempt_generation,
        ) == identity
        assert owner.latest_operation(recovered.run.run_ref) is None
        events = DurableFeed(database).read_event_type(
            "agent_runtime.target_root_failure_recovered"
        )
        assert len(events) == 1
    finally:
        database.close()


def test_owner_replays_lost_recovery_ack_after_process_restart(
    tmp_path: Path,
    immediate_target_retry: None,
) -> None:
    path = tmp_path / "failed-target-admission-restart.sqlite3"
    database, owner, request = _owner_with_failed_admission(path)
    recovered = owner.reopen_failed_target_root(request.request_ref)
    assert recovered.reopened is True
    database.close()

    restarted_database = Database(path)
    restarted_owner = SQLiteAgentRuntimeHarness(
        restarted_database,
        DurableFeed(restarted_database),
    )
    try:
        replayed = restarted_owner.reopen_failed_target_root(request.request_ref)

        assert replayed.reopened is False
        assert replayed.run.status == "admitting"
        assert replayed.run.failure_code == "target_root_recovery_pending"
        assert len(
            DurableFeed(restarted_database).read_event_type(
                "agent_runtime.target_root_failure_recovered"
            )
        ) == 1
    finally:
        restarted_database.close()


@pytest.mark.parametrize("prior_success", (False, True))
def test_owner_reopens_only_drained_failed_turn_and_preserves_operation_ledger(
    tmp_path: Path,
    prior_success: bool,
    immediate_target_retry: None,
) -> None:
    database, owner, request, handle = _owner_with_active_root(
        tmp_path / f"failed-target-turn-{prior_success}.sqlite3"
    )
    try:
        run = owner.query_run(request.request_ref)
        assert run is not None
        if prior_success:
            first_ref = f"{run.run_ref}:harness_turn:1"
            owner.start_operation(
                run_ref=run.run_ref,
                operation_ref=first_ref,
                generation=1,
                invocation_hash="1" * 64,
                resume=False,
            )
            owner.complete_operation(
                operation_ref=first_ref,
                run_ref=run.run_ref,
                native_session_ref="native-target-root:stable",
                profile={},
                evidence_events=(),
            )
            run = owner.query_run(request.request_ref)
            assert run is not None and run.status == "executed"
        failed_generation = 2 if prior_success else 1
        failed_ref = f"{run.run_ref}:harness_turn:{failed_generation}"
        owner.start_operation(
            run_ref=run.run_ref,
            operation_ref=failed_ref,
            generation=failed_generation,
            invocation_hash="2" * 64,
            resume=prior_success,
        )
        owner.record_operation_failure(failed_ref, "provider_process_failed")
        identity = (
            run.run_ref,
            run.root_session_ref,
            run.attempt_ref,
            run.fence_ref,
            run.attempt_generation,
        )

        recovered = owner.reopen_failed_target_root(request.request_ref)
        replayed = owner.reopen_failed_target_root(request.request_ref)

        assert recovered.reopened is True
        assert replayed.reopened is False
        assert recovered.run.status == ("executed" if prior_success else "admitted")
        assert recovered.run.native_session_ref == (
            "native-target-root:stable" if prior_success else None
        )
        assert (
            recovered.run.run_ref,
            recovered.run.root_session_ref,
            recovered.run.attempt_ref,
            recovered.run.fence_ref,
            recovered.run.attempt_generation,
        ) == identity
        assert owner.channel_is_current("d" * 64) is False
        assert owner.next_operation_generation(run.run_ref) == failed_generation + 1
        failed = owner.latest_operation(run.run_ref)
        assert failed is not None
        assert (failed.operation_ref, failed.status, failed.outcome_code) == (
            failed_ref,
            "failed",
            "provider_process_failed",
        )
        with pytest.raises(
            AgentRuntimeHarnessError,
            match="harness_operation_state_conflict",
        ):
            owner.complete_operation(
                operation_ref=failed_ref,
                run_ref=run.run_ref,
                native_session_ref="native-target-root:forged-late-completion",
                profile={},
                evidence_events=(),
            )
        events = DurableFeed(database).read_event_type(
            "agent_runtime.target_root_failure_recovered"
        )
        assert len(events) == 1
        assert handle.target_run_ref == recovered.run.run_ref
    finally:
        database.close()


def test_owner_never_reopens_unknown_provider_outcome(tmp_path: Path) -> None:
    database, owner, request, _handle = _owner_with_active_root(
        tmp_path / "unknown-target-turn.sqlite3"
    )
    try:
        run = owner.query_run(request.request_ref)
        assert run is not None
        operation_ref = f"{run.run_ref}:harness_turn:1"
        owner.start_operation(
            run_ref=run.run_ref,
            operation_ref=operation_ref,
            generation=1,
            invocation_hash="3" * 64,
            resume=False,
        )
        owner.record_operation_failure(operation_ref, "provider_timeout")

        with pytest.raises(
            AgentRuntimeHarnessError,
            match="target_root_failure_recovery_unsafe",
        ):
            owner.reopen_failed_target_root(request.request_ref)

        current = owner.query_run(request.request_ref)
        operation = owner.latest_operation(run.run_ref)
        assert current is not None and current.status == "running"
        assert operation is not None and operation.status == "unknown_outcome"
        assert DurableFeed(database).read_event_type(
            "agent_runtime.target_root_failure_recovered"
        ) == ()
    finally:
        database.close()


def test_owner_rate_limits_initial_target_admission_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock(90.0)
    monkeypatch.setattr(
        "meta_research.owners.agent_runtime_harness.time.time",
        clock.time,
    )
    database, owner, request = _owner_with_failed_admission(
        tmp_path / "initial-target-admission-backoff.sqlite3"
    )
    try:
        with pytest.raises(AgentRuntimeHarnessRetryLater) as wait:
            owner.reopen_failed_target_root(request.request_ref)
        assert wait.value.retry.operation_generation == 0
        assert wait.value.retry.failure_code == "mcp_channel_unavailable"
        assert wait.value.retry.consecutive_failures == 1
        assert wait.value.retry.next_retry_at == 91.0
        assert owner.latest_operation(request.target_run_ref) is None

        clock.now = 91.0
        recovered = owner.reopen_failed_target_root(request.request_ref)
        assert recovered.reopened is True
        assert recovered.run.status == "admitting"
        assert recovered.next_retry_at == 92.0
    finally:
        database.close()


def test_owner_durably_rate_limits_failed_root_and_pending_channel_replays(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock(100.0)
    monkeypatch.setattr(
        "meta_research.owners.agent_runtime_harness.time.time",
        clock.time,
    )
    path = tmp_path / "target-root-durable-backoff.sqlite3"
    database, owner, request, _handle = _owner_with_active_root(path)
    run = owner.query_run(request.request_ref)
    assert run is not None
    operation_ref = f"{run.run_ref}:harness_turn:1"
    owner.start_operation(
        run_ref=run.run_ref,
        operation_ref=operation_ref,
        generation=1,
        invocation_hash="4" * 64,
        resume=False,
    )
    owner.record_operation_failure(operation_ref, "provider_process_failed")
    baseline_feed = DurableFeed(database).current_revision()

    for now in (100.0, 100.2, 100.8):
        clock.now = now
        with pytest.raises(AgentRuntimeHarnessRetryLater) as caught:
            owner.reopen_failed_target_root(request.request_ref)
        assert caught.value.retry.operation_generation == 1
        assert caught.value.retry.failure_code == "provider_process_failed"
        assert caught.value.retry.consecutive_failures == 1
        assert caught.value.retry.next_retry_at == 101.0
        assert DurableFeed(database).current_revision() == baseline_feed
        assert owner.next_operation_generation(run.run_ref) == 2
    database.close()

    restarted_database = Database(path)
    restarted_owner = SQLiteAgentRuntimeHarness(
        restarted_database,
        DurableFeed(restarted_database),
    )
    try:
        with pytest.raises(AgentRuntimeHarnessRetryLater) as restarted_wait:
            restarted_owner.reopen_failed_target_root(request.request_ref)
        assert restarted_wait.value.retry.next_retry_at == 101.0
        assert DurableFeed(restarted_database).current_revision() == baseline_feed

        clock.now = 101.0
        recovered = restarted_owner.reopen_failed_target_root(
            request.request_ref
        )
        assert recovered.reopened is True
        recovery_feed = DurableFeed(restarted_database).current_revision()

        for now in (101.0, 101.2, 101.8):
            clock.now = now
            with pytest.raises(AgentRuntimeHarnessRetryLater) as pending_wait:
                restarted_owner.reopen_failed_target_root(request.request_ref)
            assert pending_wait.value.retry.next_retry_at == 102.0
            assert (
                DurableFeed(restarted_database).current_revision()
                == recovery_feed
            )
            assert restarted_owner.next_operation_generation(run.run_ref) == 2

        restarted_database.close()
        replay_database = Database(path)
        replay_owner = SQLiteAgentRuntimeHarness(
            replay_database,
            DurableFeed(replay_database),
        )
        try:
            with pytest.raises(AgentRuntimeHarnessRetryLater) as replay_wait:
                replay_owner.reopen_failed_target_root(request.request_ref)
            assert replay_wait.value.retry.next_retry_at == 102.0

            clock.now = 102.0
            leased = replay_owner.reopen_failed_target_root(
                request.request_ref
            )
            assert leased.reopened is False
            assert leased.run.updated_at == 102.0
            with pytest.raises(AgentRuntimeHarnessRetryLater) as next_wait:
                replay_owner.reopen_failed_target_root(request.request_ref)
            assert next_wait.value.retry.next_retry_at == 103.0
            assert replay_owner.next_operation_generation(run.run_ref) == 2
        finally:
            replay_database.close()
    finally:
        restarted_database.close()


def test_target_retry_streak_spans_failure_codes_and_success_resets_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock(200.0)
    monkeypatch.setattr(
        "meta_research.owners.agent_runtime_harness.time.time",
        clock.time,
    )
    database, owner, request, _handle = _owner_with_active_root(
        tmp_path / "target-root-retry-streak.sqlite3"
    )
    try:
        run = owner.query_run(request.request_ref)
        assert run is not None
        first_ref = f"{run.run_ref}:harness_turn:1"
        owner.start_operation(
            run_ref=run.run_ref,
            operation_ref=first_ref,
            generation=1,
            invocation_hash="5" * 64,
            resume=False,
        )
        first_retry = owner.record_operation_failure(
            first_ref, "provider_process_failed"
        )
        assert first_retry is not None
        assert first_retry.consecutive_failures == 1
        assert first_retry.next_retry_at == 201.0

        clock.now = 201.0
        owner.reopen_failed_target_root(request.request_ref)
        _replace_channel(owner, request, suffix="second")
        second_ref = f"{run.run_ref}:harness_turn:2"
        owner.start_operation(
            run_ref=run.run_ref,
            operation_ref=second_ref,
            generation=2,
            invocation_hash="6" * 64,
            resume=False,
        )
        second_retry = owner.record_operation_failure(
            second_ref, "required_harness_capability_unavailable"
        )
        assert second_retry is not None
        assert second_retry.failure_code == (
            "required_harness_capability_unavailable"
        )
        assert second_retry.consecutive_failures == 2
        assert second_retry.next_retry_at == 203.0

        clock.now = 203.0
        owner.reopen_failed_target_root(request.request_ref)
        _replace_channel(owner, request, suffix="success")
        third_ref = f"{run.run_ref}:harness_turn:3"
        owner.start_operation(
            run_ref=run.run_ref,
            operation_ref=third_ref,
            generation=3,
            invocation_hash="7" * 64,
            resume=False,
        )
        owner.complete_operation(
            operation_ref=third_ref,
            run_ref=run.run_ref,
            native_session_ref="native-target-root:after-retry",
            profile={},
            evidence_events=(),
        )

        clock.now = 204.0
        fourth_ref = f"{run.run_ref}:harness_turn:4"
        owner.start_operation(
            run_ref=run.run_ref,
            operation_ref=fourth_ref,
            generation=4,
            invocation_hash="8" * 64,
            resume=True,
        )
        reset_retry = owner.record_operation_failure(
            fourth_ref, "provider_process_failed"
        )
        assert reset_retry is not None
        assert reset_retry.consecutive_failures == 1
        assert reset_retry.next_retry_at == 205.0
    finally:
        database.close()


def test_harness_runtime_rate_limits_channel_attempts_across_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock(500.0)
    monkeypatch.setattr(
        "meta_research.owners.agent_runtime_harness.time.time",
        clock.time,
    )
    path = tmp_path / "target-root-channel-retry-runtime.sqlite3"
    database, owner, request, _handle = _owner_with_active_root(path)
    run = owner.query_run(request.request_ref)
    assert run is not None
    operation_ref = f"{run.run_ref}:harness_turn:1"
    owner.start_operation(
        run_ref=run.run_ref,
        operation_ref=operation_ref,
        generation=1,
        invocation_hash="9" * 64,
        resume=False,
    )
    owner.record_operation_failure(operation_ref, "provider_process_failed")

    class FlakyGateway(_Gateway):
        attempts = 0
        unavailable = True

        def issue_channel(self, **values: object):
            self.attempts += 1
            if self.unavailable:
                raise SemanticMcpError("mcp_channel_temporarily_unavailable")
            return super().issue_channel(**values)

    gateway = FlakyGateway()
    runtime = HarnessRuntime(
        owner,
        gateway,
        (_TargetRootAdapter("codex"), _TargetRootAdapter("claude")),
    )

    with pytest.raises(HarnessAdmissionError) as initial_wait:
        runtime.recover_failed_target_root(request.request_ref)
    assert initial_wait.value.next_retry_at == 501.0
    assert gateway.attempts == 0

    clock.now = 501.0
    with pytest.raises(
        HarnessAdmissionError,
        match="mcp_channel_temporarily_unavailable",
    ) as channel_failure:
        runtime.recover_failed_target_root(request.request_ref)
    assert channel_failure.value.next_retry_at == 502.0
    assert gateway.attempts == 1
    for now in (501.2, 501.8):
        clock.now = now
        with pytest.raises(HarnessAdmissionError) as pending_wait:
            runtime.recover_failed_target_root(request.request_ref)
        assert pending_wait.value.next_retry_at == 502.0
        assert gateway.attempts == 1
        assert owner.next_operation_generation(run.run_ref) == 2
    database.close()

    restarted_database = Database(path)
    restarted_owner = SQLiteAgentRuntimeHarness(
        restarted_database,
        DurableFeed(restarted_database),
    )
    restarted_gateway = FlakyGateway()
    restarted = HarnessRuntime(
        restarted_owner,
        restarted_gateway,
        (_TargetRootAdapter("codex"), _TargetRootAdapter("claude")),
    )
    try:
        with pytest.raises(HarnessAdmissionError) as restarted_wait:
            restarted.recover_failed_target_root(request.request_ref)
        assert restarted_wait.value.next_retry_at == 502.0
        assert restarted_gateway.attempts == 0

        clock.now = 502.0
        with pytest.raises(HarnessAdmissionError) as second_channel_failure:
            restarted.recover_failed_target_root(request.request_ref)
        assert second_channel_failure.value.next_retry_at == 503.0
        assert restarted_gateway.attempts == 1

        clock.now = 503.0
        restarted_gateway.unavailable = False
        restarted.recover_failed_target_root(request.request_ref)
        recovered = restarted_owner.query_run(request.request_ref)
        assert recovered is not None
        assert recovered.failure_code == TARGET_ROOT_RECOVERY_READY_CODE
        assert restarted_gateway.attempts == 2
        assert restarted_owner.next_operation_generation(run.run_ref) == 2
    finally:
        restarted_database.close()
