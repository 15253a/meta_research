from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

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
    HarnessAdapterUnavailable,
    HarnessInvocation,
    HarnessTurnEvidence,
)
from meta_research.owners.agent_runtime_harness import (
    TARGET_ROOT_RECOVERY_PENDING_CODE,
    TARGET_ROOT_RECOVERY_READY_CODE,
    AgentRuntimeHarnessError,
    AgentRuntimeHarnessOperation,
    AgentRuntimeHarnessRetry,
    AgentRuntimeHarnessRun,
    TargetRootCompletionEvidence,
    TargetRootObservationPage,
)
from meta_research.owners.common import canonical_hash
from meta_research.migration import upgrade_database
from meta_research.paths import prepare_data_root
from meta_research.runtime_protection import (
    InhibitorLease,
    RuntimeBoundaryRecorder,
    RuntimeEventLogger,
    RuntimeProtection,
    RuntimeProtectionUnavailable,
)
from meta_research.root_capabilities import (
    ROOT_AGENT_KINDS,
    RootAgentKind,
    root_capability_profile,
)
from meta_research.semantic_mcp import (
    McpConnection,
    ResidentMcpBinding,
    SemanticMcpError,
)
from meta_research.semantic_owner_gateway import (
    BUNDLE_ROOT_SEMANTIC_OPERATION_IDS,
    REASONING_ROOT_SEMANTIC_OPERATION_IDS,
    ROOT_AGENT_SEMANTIC_OPERATION_IDS,
    TARGET_ROOT_SEMANTIC_OPERATION_IDS,
)
from meta_research.target_run_runtime_contract import (
    TargetCompletionArtifact,
    TargetCompletionHandoff,
)


_TARGET_ROOT_CAPABILITIES = frozenset(
    {
        "shell",
        "file_access",
        "semantic_mcp",
        "skill",
        "subagent",
        "stream",
        "native_session",
    }
)
_TARGET_ROOT_REQUIRED_EVIDENCE = frozenset(
    {"shell", "file_access", "stream", "native_session"}
)
_TARGET_ROOT_OPTIONAL_CAPABILITIES = (
    _TARGET_ROOT_CAPABILITIES - _TARGET_ROOT_REQUIRED_EVIDENCE
)


class _TestPowerInhibitor:
    kind = "test_inhibitor"

    def __init__(self) -> None:
        self.active: set[str] = set()

    def acquire(self, *, holder_ref: str, reason: str) -> InhibitorLease:
        del reason
        self.active.add(holder_ref)
        return InhibitorLease(
            holder_ref=holder_ref,
            backend=self.kind,
            scope="sleep",
            acquired_at=1.0,
            native_holder_ref=f"native:{holder_ref}",
        )

    def is_confirmed(self, lease: InhibitorLease) -> bool:
        return lease.holder_ref in self.active

    def release(self, lease: InhibitorLease) -> None:
        self.active.discard(lease.holder_ref)


class _FailOncePowerInhibitor(_TestPowerInhibitor):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    def acquire(self, *, holder_ref: str, reason: str) -> InhibitorLease:
        if not self.failed:
            self.failed = True
            raise RuntimeProtectionUnavailable(
                "power_inhibitor_systemd_acquire_failed"
            )
        return super().acquire(holder_ref=holder_ref, reason=reason)


class _TargetRootAdapter:
    locked_version = "test-harness-v1"

    def __init__(
        self,
        family: str,
        *,
        fail_first_code: str | None = None,
        missing_capabilities: frozenset[str] = frozenset(),
        evidence_free_capability: str | None = None,
        target_root_timeout_seconds: float = 30 * 24 * 60 * 60,
    ) -> None:
        self.family = family
        self.fail_first_code = fail_first_code
        self.missing_capabilities = missing_capabilities
        self.evidence_free_capability = evidence_free_capability
        self.target_root_timeout_seconds = target_root_timeout_seconds
        self.invocations: list[HarnessInvocation] = []

    def provider_operation_timeout_seconds(self, *, target_root: bool) -> float:
        return self.target_root_timeout_seconds if target_root else 300.0

    def installation_profile(self) -> dict[str, object]:
        return {
            "harness_family": self.family,
            "locked_version": self.locked_version,
            "provider_version": self.locked_version,
            "status": "ready",
        }

    def invoke(self, invocation: HarnessInvocation) -> HarnessTurnEvidence:
        self.invocations.append(invocation)
        if self.fail_first_code is not None and len(self.invocations) == 1:
            raise HarnessAdapterUnavailable(self.fail_first_code)

        native_session_ref = invocation.native_session_ref or "native-target-root"
        available = set(_TARGET_ROOT_CAPABILITIES)
        if invocation.native_session_ref is not None:
            available.add("resume")
        available.difference_update(self.missing_capabilities)
        evidence_events = tuple(
            {
                "event_ref": (
                    f"evidence:{invocation.provider_operation_ref}:{capability}"
                ),
                "sequence": sequence,
                "kind": f"observed:{capability}",
            }
            for sequence, capability in enumerate(
                sorted(available - {self.evidence_free_capability}), start=1
            )
        )
        event_refs = {
            str(event["kind"]).removeprefix("observed:"): event["event_ref"]
            for event in evidence_events
        }
        capabilities = {
            capability: (
                {
                    "status": "available",
                    "evidence_refs": (
                        []
                        if capability == self.evidence_free_capability
                        else [event_refs[capability]]
                    ),
                }
                if capability in available
                else {
                    "status": "capability_unavailable",
                    "reason": {"code": "not_exercised"},
                    "evidence_refs": [],
                }
            )
            for capability in HARNESS_CAPABILITIES
        }
        used_capabilities = tuple(
            sorted(available - {self.evidence_free_capability})
        )
        usage_evidence_refs = {
            capability: (str(event_refs[capability]),)
            for capability in used_capabilities
        }
        return HarnessTurnEvidence(
            native_session_ref=native_session_ref,
            profile={
                "schema_ref": "meta-research/harness-capability-profile/v1",
                "harness_family": self.family,
                "locked_version": self.locked_version,
                "provider_version": self.locked_version,
                "native_session_ref": native_session_ref,
                "capabilities": capabilities,
                "root_capability_diagnostics": root_capability_profile(
                    "target"
                ).public_diagnostics(
                    entry_path=invocation.entry_path,
                    used_capabilities=used_capabilities,
                    authorized_operation_ids=(
                        invocation.authorized_operation_ids
                    ),
                    unavailable_capabilities={
                        capability: "harness_capability_unsupported"
                        for capability in self.missing_capabilities
                    },
                    usage_evidence_refs=usage_evidence_refs,
                ),
            },
            evidence_events=evidence_events,
            stream_hash=canonical_hash(evidence_events),
        )


class _Gateway:
    def __init__(self) -> None:
        self._counter = 0
        self._operation_ids_by_token: dict[str, tuple[str, ...]] = {}

    def required_bindings(
        self, operation_ids: tuple[str, ...]
    ) -> tuple[dict[str, object], ...]:
        return tuple(
            {"semantic_operation_id": operation_id}
            for operation_id in operation_ids
        )

    def query_status(self) -> dict[str, object]:
        return {"status": "ready", "catalog_hash": "a" * 64}

    def issue_channel(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        root_session_ref: str,
        fence_ref: str,
        capability_binding_hash: str,
        operation_ids: tuple[str, ...],
        root_kind: RootAgentKind | None,
        phase: str,
    ) -> tuple[McpConnection, ResidentMcpBinding]:
        del run_ref, attempt_ref, root_session_ref, fence_ref
        del capability_binding_hash
        self._counter += 1
        grant_ref = f"grant:{self._counter}"
        token = f"token:{self._counter}"
        self._operation_ids_by_token[token] = operation_ids
        return (
            McpConnection(token=token, grant_ref=grant_ref),
            ResidentMcpBinding(
                server_instance_ref="mcp-server:test",
                endpoint_ref="/mcp",
                catalog_revision=1,
                catalog_hash="a" * 64,
                health_receipt_ref="mcp-health:test",
                connection_grant_ref=grant_ref,
                operation_bindings=self.required_bindings(operation_ids),
                root_kind=root_kind,
                phase=phase,
            ),
        )

    def revoke_channel(self, token: str) -> None:
        self._operation_ids_by_token.pop(token, None)

    def dispatch(
        self, _token: str | None, message: object
    ) -> tuple[int, dict[str, object] | None]:
        assert isinstance(message, dict)
        operation_ids = self._operation_ids_by_token.get(_token or "")
        if operation_ids is None:
            return 401, {
                "error": {
                    "code": "mcp_channel_authentication_required",
                    "message": "A current scope-bound MCP channel is required.",
                }
            }
        if message.get("method") == "initialize":
            return 200, {
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "result": {"protocolVersion": "2025-06-18"},
            }
        if message.get("method") == "notifications/initialized":
            return 202, None
        if message.get("method") == "tools/list":
            return 200, {
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "result": {
                    "tools": [
                        {"name": operation_id}
                        for operation_id in operation_ids
                    ]
                },
            }
        if message.get("method") == "tools/call":
            params = message.get("params")
            operation_id = (
                params.get("name") if isinstance(params, dict) else None
            )
            if operation_id not in operation_ids:
                return 200, {
                    "jsonrpc": "2.0",
                    "id": message.get("id"),
                    "result": {
                        "content": [],
                        "structuredContent": {
                            "status": "capability_unavailable",
                            "code": "capability_unavailable",
                        },
                        "isError": True,
                    },
                }
            return 200, {
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "result": {
                    "content": [],
                    "structuredContent": {
                        "status": "ok",
                        "operation_id": operation_id,
                    },
                    "isError": False,
                },
            }
        return 400, None


class _WorkspaceResolver:
    def __init__(self, path: Path) -> None:
        self.path = path

    def resolve_target_workspace(self, **_scope: object) -> tuple[str, Path]:
        return "target-workspace:test", self.path


class _DiagnosticRecorder:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.records: list[dict[str, object]] = []

    def record(self, **values: object) -> object:
        if self.fail:
            raise RuntimeError("diagnostic store unavailable")
        self.records.append(values)
        return values


class _ResidentScopeVerifier:
    def __init__(self) -> None:
        self.current = True
        self.reconcile_current = False
        self.root_kinds: list[RootAgentKind] = []
        self.reconcile_root_kinds: list[RootAgentKind] = []

    def verify_root_agent_runtime_scope(
        self, *, root_kind: RootAgentKind, **_scope: object
    ) -> None:
        self.root_kinds.append(root_kind)
        if not self.current:
            raise RuntimeError("resident_scope_stale")
        return None

    def verify_root_agent_human_request_reconcile_scope(
        self, *, root_kind: RootAgentKind, **_scope: object
    ) -> None:
        self.reconcile_root_kinds.append(root_kind)
        if not self.reconcile_current:
            raise RuntimeError("resident_reconcile_scope_stale")


class _Owner:
    def __init__(self, request: TargetHarnessRequest) -> None:
        self.request = request
        self.run = AgentRuntimeHarnessRun(
            request_ref=request.request_ref,
            idempotency_key=request.request_ref,
            request=request.as_dict(),
            request_hash=canonical_hash(request.as_dict()),
            run_ref=request.target_run_ref,
            attempt_ref="target-attempt:test",
            attempt_generation=1,
            root_session_ref="target-root-session:test",
            native_session_ref=None,
            fence_ref="target-fence:test",
            harness_family=request.harness_family,
            model_ref=request.model_ref,
            auth_profile_ref=request.auth_profile_ref,
            capability_binding_hash="b" * 64,
            mcp_binding=None,
            status="admitted",
            failure_code=None,
            created_at=1.0,
            updated_at=1.0,
        )
        self.operations: list[AgentRuntimeHarnessOperation] = []
        self.profile: dict[str, object] | None = None
        self.completion_evidence: TargetRootCompletionEvidence | None = None
        self.completion_query_error: str | None = None
        self.observation_page: TargetRootObservationPage | None = None
        self.observation_query_error: str | None = None
        self.observation_query: tuple[str, str | None, int] | None = None
        self.recovery_transitions = 0

    def query_run(self, request_ref: str) -> AgentRuntimeHarnessRun | None:
        return self.run if request_ref == self.run.request_ref else None

    def channel_is_current(self, _token_hash: str) -> bool:
        return False

    def query_run_by_ref(self, run_ref: str) -> AgentRuntimeHarnessRun | None:
        return self.run if run_ref == self.run.run_ref else None

    def query_request(self, run_ref: str) -> dict[str, object]:
        assert run_ref == self.run.run_ref
        return self.request.as_dict()

    def activate_admission(self, **channel: object) -> AgentRuntimeHarnessRun:
        assert channel["run_ref"] == self.run.run_ref
        assert self.run.status == "admitting"
        self.run = replace(
            self.run,
            status="admitted",
            failure_code=(
                TARGET_ROOT_RECOVERY_READY_CODE
                if self.run.failure_code == TARGET_ROOT_RECOVERY_PENDING_CODE
                else self.run.failure_code
            ),
        )
        return self.run

    def replace_channel(self, **channel: object) -> AgentRuntimeHarnessRun:
        if self.run.failure_code == TARGET_ROOT_RECOVERY_PENDING_CODE:
            self.run = replace(
                self.run,
                failure_code=TARGET_ROOT_RECOVERY_READY_CODE,
            )
        self.run = replace(
            self.run,
            mcp_binding=channel["mcp_binding"],  # type: ignore[arg-type]
        )
        return self.run

    def reopen_failed_target_root(self, request_ref: str):
        assert request_ref == self.run.request_ref
        replay = (
            self.run.failure_code
            in {
                TARGET_ROOT_RECOVERY_PENDING_CODE,
                TARGET_ROOT_RECOVERY_READY_CODE,
            }
            and self.run.status in {"admitting", "admitted", "executed"}
        )
        if not replay:
            if self.run.status != "failed":
                raise AgentRuntimeHarnessError(
                    "target_root_failure_recovery_not_required"
                )
            if any(
                operation.status in {"running", "unknown_outcome"}
                for operation in self.operations
            ):
                raise AgentRuntimeHarnessError(
                    "target_root_failure_recovery_unsafe"
                )
            self.recovery_transitions += 1
            self.run = replace(
                self.run,
                status=(
                    "admitting"
                    if not self.operations
                    else "executed"
                    if self.run.native_session_ref is not None
                    else "admitted"
                ),
                failure_code=TARGET_ROOT_RECOVERY_PENDING_CODE,
            )
        return SimpleNamespace(
            run=self.run,
            reopened=not replay,
            operation_generation=(
                0 if not self.operations else self.operations[-1].generation
            ),
        )

    def next_operation_generation(self, run_ref: str) -> int:
        assert run_ref == self.run.run_ref
        return len(self.operations) + 1

    def latest_operation(
        self, run_ref: str
    ) -> AgentRuntimeHarnessOperation | None:
        assert run_ref == self.run.run_ref
        return self.operations[-1] if self.operations else None

    def start_operation(
        self,
        *,
        run_ref: str,
        operation_ref: str,
        generation: int,
        invocation_hash: str,
        resume: bool,
    ) -> None:
        assert run_ref == self.run.run_ref
        assert resume is (self.run.native_session_ref is not None)
        self.operations.append(
            AgentRuntimeHarnessOperation(
                operation_ref=operation_ref,
                run_ref=run_ref,
                harness_family=self.run.harness_family,
                generation=generation,
                invocation_hash=invocation_hash,
                status="running",
                outcome_code=None,
            )
        )
        self.run = replace(self.run, status="running", failure_code=None)

    def begin_reconciliation(self, operation_ref: str) -> int:
        operation = self.operations[-1]
        assert operation.operation_ref == operation_ref
        reconciliation_generation = operation.reconciliation_generation + 1
        self.operations[-1] = replace(
            operation,
            status="running",
            outcome_code=None,
            reconciliation_generation=reconciliation_generation,
        )
        return reconciliation_generation

    def record_operation_failure(
        self, operation_ref: str, code: str
    ) -> AgentRuntimeHarnessRetry | None:
        operation = self.operations[-1]
        assert operation.operation_ref == operation_ref
        unknown = code in {
            "provider_timeout",
            "provider_io_unavailable",
            "provider_outcome_unknown",
        } or code.startswith(("power_inhibitor_", "runtime_"))
        self.operations[-1] = replace(
            operation,
            status="unknown_outcome" if unknown else "failed",
            outcome_code=code,
        )
        self.run = replace(
            self.run,
            status="running" if unknown else "failed",
            failure_code=code,
        )
        if unknown:
            return None
        return AgentRuntimeHarnessRetry(
            request_ref=self.run.request_ref,
            target_ref=self.request.target_ref,
            target_run_ref=self.run.run_ref,
            operation_generation=operation.generation,
            failure_code=code,
            consecutive_failures=1,
            failed_at=1.0,
            next_retry_at=2.0,
        )

    def complete_operation(
        self,
        *,
        operation_ref: str,
        run_ref: str,
        native_session_ref: str,
        profile: dict[str, object],
        evidence_events: tuple[dict[str, object], ...],
    ) -> None:
        del evidence_events
        operation = self.operations[-1]
        assert operation.operation_ref == operation_ref
        assert run_ref == self.run.run_ref
        self.operations[-1] = replace(
            operation, status="executed", outcome_code=None
        )
        self.profile = profile
        self.run = replace(
            self.run,
            native_session_ref=native_session_ref,
            status="executed",
            failure_code=None,
        )

    def query_profile(self, run_ref: str) -> dict[str, object] | None:
        assert run_ref == self.run.run_ref
        return self.profile

    def query_status_records(
        self,
    ) -> tuple[
        tuple[AgentRuntimeHarnessRun, ...],
        tuple[AgentRuntimeHarnessOperation, ...],
    ]:
        return (self.run,), tuple(reversed(self.operations))

    def query_target_child_session(self, _operation_ref: str) -> None:
        return None

    def query_target_root_completion_evidence(
        self, target_ref: str
    ) -> TargetRootCompletionEvidence | None:
        assert target_ref == self.request.target_ref
        if self.completion_query_error is not None:
            raise AgentRuntimeHarnessError(self.completion_query_error)
        return self.completion_evidence

    def query_target_root_observations(
        self,
        target_ref: str,
        *,
        after_cursor: str | None = None,
        limit: int = 128,
    ) -> TargetRootObservationPage:
        assert target_ref == self.request.target_ref
        self.observation_query = (target_ref, after_cursor, limit)
        if self.observation_query_error is not None:
            raise AgentRuntimeHarnessError(self.observation_query_error)
        assert self.observation_page is not None
        return self.observation_page


def _target_request(
    *, provider_operation_timeout_seconds: float | None = 7 * 24 * 60 * 60
) -> TargetHarnessRequest:
    full_conformance_binding = {"contract_ref": "test/full-conformance/v1"}
    return TargetHarnessRequest(
        request_ref="target-harness-request:test",
        harness_family="codex",
        model_ref="gpt-target-root",
        auth_profile_ref="harness-profile:test",
        required_operation_ids=TARGET_ROOT_SEMANTIC_OPERATION_IDS,
        required_capabilities=HARNESS_CAPABILITIES,
        target_ref="target:test",
        target_run_ref="target-run:test",
        full_conformance_binding=full_conformance_binding,
        full_conformance_binding_hash=canonical_hash(full_conformance_binding),
        target_scope_binding_hash="c" * 64,
        provider_operation_timeout_seconds=provider_operation_timeout_seconds,
    )


def _runtime(
    tmp_path: Path,
    *,
    fail_first_unknown: bool = False,
    fail_first_code: str | None = None,
    missing_capabilities: frozenset[str] = frozenset(),
    evidence_free_capability: str | None = None,
) -> tuple[HarnessRuntime, _Owner, _TargetRootAdapter]:
    request = _target_request()
    owner = _Owner(request)
    codex = _TargetRootAdapter(
        "codex",
        fail_first_code=(
            "provider_outcome_unknown"
            if fail_first_unknown
            else fail_first_code
        ),
        missing_capabilities=missing_capabilities,
        evidence_free_capability=evidence_free_capability,
    )
    runtime = HarnessRuntime(
        owner,
        _Gateway(),
        (codex,),
    )
    runtime.bind_target_workspace_resolver(_WorkspaceResolver(tmp_path))
    return runtime, owner, codex


def test_resident_root_channel_rejects_catalog_selected_by_the_caller(
    tmp_path: Path,
) -> None:
    runtime, owner, _adapter = _runtime(tmp_path)
    runtime.bind_resident_mcp_scope_verifier(_ResidentScopeVerifier())
    scope = {
        "run_ref": owner.run.run_ref,
        "attempt_ref": owner.run.attempt_ref,
        "root_session_ref": owner.run.root_session_ref,
        "fence_ref": owner.run.fence_ref,
        "capability_binding_hash": owner.run.capability_binding_hash,
        "root_kind": "bundle",
        "phase": "primary",
        "subject_policy": "operation_tree",
    }

    with pytest.raises(
        HarnessAdmissionError, match="mcp_channel_scope_invalid"
    ):
        runtime.issue_resident_mcp_channel(
            **scope,
            operation_ids=("research_graph.snapshot.read",),
        )

    channel = runtime.issue_resident_mcp_channel(
        **scope,
        operation_ids=BUNDLE_ROOT_SEMANTIC_OPERATION_IDS,
    )
    assert tuple(
        item["semantic_operation_id"]
        for item in channel.binding.operation_bindings
    ) == BUNDLE_ROOT_SEMANTIC_OPERATION_IDS

    with pytest.raises(
        HarnessAdmissionError, match="mcp_channel_scope_invalid"
    ):
        runtime.issue_resident_mcp_channel(
            **{**scope, "subject_policy": "review_tree"},
            operation_ids=("human_request.open",),
        )


@pytest.mark.parametrize("root_kind", ROOT_AGENT_KINDS)
def test_each_root_operation_tree_lists_its_server_selected_catalog_and_one_recovery(
    tmp_path: Path,
    root_kind: RootAgentKind,
) -> None:
    runtime, owner, _adapter = _runtime(tmp_path)
    verifier = _ResidentScopeVerifier()
    runtime.bind_resident_mcp_scope_verifier(verifier)
    expected_operation_ids = ROOT_AGENT_SEMANTIC_OPERATION_IDS[root_kind]

    channel = runtime.issue_resident_mcp_channel(
        run_ref=owner.run.run_ref,
        attempt_ref=owner.run.attempt_ref,
        root_session_ref=owner.run.root_session_ref,
        fence_ref=owner.run.fence_ref,
        capability_binding_hash=owner.run.capability_binding_hash,
        operation_ids=expected_operation_ids,
        root_kind=root_kind,
        phase="primary",
        subject_policy="operation_tree",
    )
    status, payload, _session_id = runtime.dispatch_mcp_http(
        channel.connection.token,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        mcp_session_id="authenticated-agent-session",
    )

    assert status == 200
    assert payload is not None
    assert tuple(
        item["name"] for item in payload["result"]["tools"]
    ) == expected_operation_ids
    assert channel.binding.root_kind == root_kind
    assert channel.binding.phase == "primary"
    expected_verifications = [root_kind, root_kind]

    # One representative recovery proves that the catalog is selected again
    # from root_kind after Attempt/Fence rotation, without constructing a
    # kind-by-identity authorization matrix.
    if root_kind == "acquisition":
        runtime.revoke_resident_mcp_channel(channel)
        recovered = runtime.issue_resident_mcp_channel(
            run_ref=owner.run.run_ref,
            attempt_ref="acquisition-attempt:recovery",
            root_session_ref=owner.run.root_session_ref,
            fence_ref="acquisition-fence:recovery",
            capability_binding_hash=owner.run.capability_binding_hash,
            operation_ids=expected_operation_ids,
            root_kind=root_kind,
            phase="recovery",
            subject_policy="operation_tree",
        )
        recovered_status, recovered_payload, _ = runtime.dispatch_mcp_http(
            recovered.connection.token,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            },
            mcp_session_id="recovered-agent-session",
        )

        assert recovered_status == 200
        assert recovered_payload is not None
        assert tuple(
            item["name"] for item in recovered_payload["result"]["tools"]
        ) == expected_operation_ids
        assert recovered.binding.phase == "recovery"
        assert recovered.connection.token != channel.connection.token
        expected_verifications.extend((root_kind, root_kind))

    assert verifier.root_kinds == expected_verifications


def test_operation_tree_channel_allows_root_reconnect_and_child_clients(
    tmp_path: Path,
) -> None:
    runtime, owner, _adapter = _runtime(tmp_path)
    runtime.bind_resident_mcp_scope_verifier(_ResidentScopeVerifier())
    channel = runtime.issue_resident_mcp_channel(
        run_ref=owner.run.run_ref,
        attempt_ref=owner.run.attempt_ref,
        root_session_ref=owner.run.root_session_ref,
        fence_ref=owner.run.fence_ref,
        capability_binding_hash=owner.run.capability_binding_hash,
        operation_ids=BUNDLE_ROOT_SEMANTIC_OPERATION_IDS,
        root_kind="bundle",
        phase="primary",
        subject_policy="operation_tree",
    )
    operation_id = BUNDLE_ROOT_SEMANTIC_OPERATION_IDS[0]

    def initialize_list_and_call(
        client_name: str, request_id: int
    ) -> tuple[str, tuple[str, ...], dict[str, object]]:
        status, response, session_id = runtime.dispatch_mcp_http(
            channel.connection.token,
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": client_name, "version": "1"},
                },
            },
            mcp_session_id=None,
        )
        assert status == 200
        assert response is not None
        assert isinstance(session_id, str) and session_id

        ack_status, ack_response, _ = runtime.dispatch_mcp_http(
            channel.connection.token,
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            },
            mcp_session_id=session_id,
        )
        assert ack_status == 202
        assert ack_response is None

        listed_status, listed, _ = runtime.dispatch_mcp_http(
            channel.connection.token,
            {
                "jsonrpc": "2.0",
                "id": request_id + 1,
                "method": "tools/list",
                "params": {},
            },
            mcp_session_id=session_id,
        )
        assert listed_status == 200
        assert listed is not None
        result = listed.get("result")
        assert isinstance(result, dict)
        tools = result.get("tools")
        assert isinstance(tools, list)
        catalog = tuple(
            item["name"] for item in tools if isinstance(item, dict)
        )

        called_status, called, _ = runtime.dispatch_mcp_http(
            channel.connection.token,
            {
                "jsonrpc": "2.0",
                "id": request_id + 2,
                "method": "tools/call",
                "params": {"name": operation_id, "arguments": {}},
            },
            mcp_session_id=session_id,
        )
        assert called_status == 200
        assert called is not None
        called_result = called.get("result")
        assert isinstance(called_result, dict)
        assert called_result["isError"] is False
        return session_id, catalog, called_result

    # A fresh transport in the same Root operation must retain a callable
    # catalog after reconnect, including initialize/initialized response loss.
    root = initialize_list_and_call("root-client", 10)
    reconnect = initialize_list_and_call("root-reconnect", 20)

    # Native child Sessions inherit and can call the same operation bearer.
    child = initialize_list_and_call("native-child", 30)

    assert len({root[0], reconnect[0], child[0]}) == 3
    assert root[1] == BUNDLE_ROOT_SEMANTIC_OPERATION_IDS
    assert reconnect[1] == root[1]
    assert child[1] == root[1]


def test_suspended_root_keeps_bearer_only_for_mcp_and_lost_ack_reconcile(
    tmp_path: Path,
) -> None:
    runtime, owner, _adapter = _runtime(tmp_path)
    verifier = _ResidentScopeVerifier()
    runtime.bind_resident_mcp_scope_verifier(verifier)
    channel = runtime.issue_resident_mcp_channel(
        run_ref=owner.run.run_ref,
        attempt_ref=owner.run.attempt_ref,
        root_session_ref=owner.run.root_session_ref,
        fence_ref=owner.run.fence_ref,
        capability_binding_hash=owner.run.capability_binding_hash,
        operation_ids=BUNDLE_ROOT_SEMANTIC_OPERATION_IDS,
        root_kind="bundle",
        phase="primary",
        subject_policy="operation_tree",
    )
    token = channel.connection.token
    ordinary_operation = next(
        operation_id
        for operation_id in BUNDLE_ROOT_SEMANTIC_OPERATION_IDS
        if not operation_id.startswith("human_request.")
    )

    verifier.current = False
    verifier.reconcile_current = True
    denied, _payload, _ = runtime.dispatch_mcp_http(
        token,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": ordinary_operation, "arguments": {}},
        },
        mcp_session_id="suspended-root",
    )
    assert denied == 401
    assert verifier.reconcile_root_kinds == []

    listed, _payload, _ = runtime.dispatch_mcp_http(
        token,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        mcp_session_id="suspended-root",
    )
    assert listed == 200
    assert verifier.reconcile_root_kinds == ["bundle"]

    reconciled, _payload, _ = runtime.dispatch_mcp_http(
        token,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "human_request.open.reconcile",
                "arguments": {},
            },
        },
        mcp_session_id="suspended-root",
    )
    assert reconciled == 200
    assert verifier.reconcile_root_kinds == ["bundle", "bundle"]

    verifier.current = True
    resumed, _payload, _ = runtime.dispatch_mcp_http(
        token,
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": ordinary_operation, "arguments": {}},
        },
        mcp_session_id="resumed-root",
    )
    assert resumed == 200


def test_suspended_root_can_reissue_exact_reconcile_channel_after_restart(
    tmp_path: Path,
) -> None:
    runtime, owner, _adapter = _runtime(tmp_path)
    verifier = _ResidentScopeVerifier()
    verifier.current = False
    verifier.reconcile_current = True
    runtime.bind_resident_mcp_scope_verifier(verifier)

    channel = runtime.issue_resident_mcp_channel(
        run_ref=owner.run.run_ref,
        attempt_ref=owner.run.attempt_ref,
        root_session_ref=owner.run.root_session_ref,
        fence_ref=owner.run.fence_ref,
        capability_binding_hash=owner.run.capability_binding_hash,
        operation_ids=BUNDLE_ROOT_SEMANTIC_OPERATION_IDS,
        root_kind="bundle",
        phase="primary",
        subject_policy="operation_tree",
    )
    ordinary_operation = next(
        operation_id
        for operation_id in BUNDLE_ROOT_SEMANTIC_OPERATION_IDS
        if not operation_id.startswith("human_request.")
    )

    denied, _payload, _ = runtime.dispatch_mcp_http(
        channel.connection.token,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": ordinary_operation, "arguments": {}},
        },
        mcp_session_id="restarted-root",
    )
    assert denied == 401

    listed, _payload, _ = runtime.dispatch_mcp_http(
        channel.connection.token,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        mcp_session_id="restarted-root",
    )
    assert listed == 200

    reconciled, _payload, _ = runtime.dispatch_mcp_http(
        channel.connection.token,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "human_request.open.reconcile",
                "arguments": {},
            },
        },
        mcp_session_id="restarted-root",
    )
    assert reconciled == 200
    assert verifier.root_kinds == ["bundle"] * 4
    assert verifier.reconcile_root_kinds == ["bundle", "bundle", "bundle"]


def test_operation_tree_bearers_keep_exact_catalog_and_revocation_boundaries(
    tmp_path: Path,
) -> None:
    runtime, owner, _adapter = _runtime(tmp_path)
    verifier = _ResidentScopeVerifier()
    runtime.bind_resident_mcp_scope_verifier(verifier)
    common_scope = {
        "run_ref": owner.run.run_ref,
        "attempt_ref": owner.run.attempt_ref,
        "root_session_ref": owner.run.root_session_ref,
        "fence_ref": owner.run.fence_ref,
        "capability_binding_hash": owner.run.capability_binding_hash,
        "phase": "primary",
        "subject_policy": "operation_tree",
    }
    bundle = runtime.issue_resident_mcp_channel(
        **common_scope,
        operation_ids=BUNDLE_ROOT_SEMANTIC_OPERATION_IDS,
        root_kind="bundle",
    )
    reasoning = runtime.issue_resident_mcp_channel(
        **common_scope,
        operation_ids=REASONING_ROOT_SEMANTIC_OPERATION_IDS,
        root_kind="reasoning",
    )

    def list_operations(token: str, request_id: int) -> tuple[str, ...]:
        status, payload, _ = runtime.dispatch_mcp_http(
            token,
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/list",
                "params": {},
            },
            mcp_session_id="client-session",
        )
        assert status == 200
        assert payload is not None
        result = payload.get("result")
        assert isinstance(result, dict)
        tools = result.get("tools")
        assert isinstance(tools, list)
        return tuple(
            item["name"] for item in tools if isinstance(item, dict)
        )

    assert list_operations(bundle.connection.token, 1) == (
        BUNDLE_ROOT_SEMANTIC_OPERATION_IDS
    )
    assert list_operations(reasoning.connection.token, 2) == (
        REASONING_ROOT_SEMANTIC_OPERATION_IDS
    )

    bundle_only = next(
        item
        for item in BUNDLE_ROOT_SEMANTIC_OPERATION_IDS
        if item not in REASONING_ROOT_SEMANTIC_OPERATION_IDS
    )
    reasoning_only = next(
        item
        for item in REASONING_ROOT_SEMANTIC_OPERATION_IDS
        if item not in BUNDLE_ROOT_SEMANTIC_OPERATION_IDS
    )
    for request_id, token, forbidden_operation in (
        (3, bundle.connection.token, reasoning_only),
        (4, reasoning.connection.token, bundle_only),
    ):
        status, payload, _ = runtime.dispatch_mcp_http(
            token,
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": forbidden_operation, "arguments": {}},
            },
            mcp_session_id="client-session",
        )
        assert status == 200
        assert payload is not None
        result = payload.get("result")
        assert isinstance(result, dict)
        assert result["isError"] is True
        assert result["structuredContent"] == {
            "status": "capability_unavailable",
            "code": "capability_unavailable",
        }

    runtime.revoke_resident_mcp_channel(bundle)
    revoked_status, revoked_payload, _ = runtime.dispatch_mcp_http(
        bundle.connection.token,
        {"jsonrpc": "2.0", "id": 5, "method": "tools/list", "params": {}},
        mcp_session_id="client-session",
    )
    assert revoked_status == 401
    assert revoked_payload is not None
    assert revoked_payload["error"] == {
        "code": "mcp_channel_authentication_required",
        "message": "A current scope-bound MCP channel is required.",
    }

    verifier.current = False
    stale_status, stale_payload, _ = runtime.dispatch_mcp_http(
        reasoning.connection.token,
        {"jsonrpc": "2.0", "id": 6, "method": "tools/list", "params": {}},
        mcp_session_id="client-session",
    )
    assert stale_status == 401
    assert stale_payload is not None
    assert stale_payload["error"] == {
        "code": "mcp_channel_authentication_required",
        "message": "A current scope-bound MCP channel is required.",
    }


def test_failed_initial_root_turn_recovers_same_handle_then_runs_next_generation(
    tmp_path: Path,
) -> None:
    runtime, owner, adapter = _runtime(
        tmp_path,
        fail_first_code="provider_process_failed",
    )
    prompt = "Own implementation through final training completion."
    identity = (
        owner.run.run_ref,
        owner.run.root_session_ref,
        owner.run.attempt_ref,
        owner.run.fence_ref,
        owner.run.attempt_generation,
    )

    with pytest.raises(
        HarnessAdmissionError, match="provider_process_failed"
    ) as failed_turn:
        runtime.run_or_resume_target_root(
            owner.run.request_ref,
            prompt=prompt,
            mcp_base_url="http://127.0.0.1:8765",
        )
    assert failed_turn.value.next_retry_at == 2.0
    assert owner.run.status == "failed"
    assert owner.operations[0].status == "failed"

    recovered = runtime.recover_failed_target_root(owner.run.request_ref)
    replayed = runtime.recover_failed_target_root(owner.run.request_ref)
    completed = runtime.run_or_resume_target_root(
        owner.run.request_ref,
        prompt=prompt,
        mcp_base_url="http://127.0.0.1:8765",
    )

    assert recovered.run.status == "admitted"
    assert replayed.run == recovered.run
    assert owner.recovery_transitions == 1
    assert completed.status == "executed"
    assert [operation.generation for operation in owner.operations] == [1, 2]
    assert [operation.status for operation in owner.operations] == [
        "failed",
        "executed",
    ]
    assert [invocation.native_session_ref for invocation in adapter.invocations] == [
        None,
        None,
    ]
    assert {invocation.working_directory for invocation in adapter.invocations} == {
        str(tmp_path)
    }
    assert (
        owner.run.run_ref,
        owner.run.root_session_ref,
        owner.run.attempt_ref,
        owner.run.fence_ref,
        owner.run.attempt_generation,
    ) == identity


def test_failed_target_admission_recovery_refreshes_both_runtime_caches_once(
    tmp_path: Path,
) -> None:
    request = _target_request()
    owner = _Owner(request)
    owner.run = replace(
        owner.run,
        status="failed",
        failure_code="mcp_channel_unavailable",
    )
    gateway = _Gateway()
    runtime = HarnessRuntime(
        owner,
        gateway,
        (_TargetRootAdapter("codex"), _TargetRootAdapter("claude")),
    )
    runtime.bind_target_workspace_resolver(_WorkspaceResolver(tmp_path))

    recovered = runtime.recover_failed_target_root(request.request_ref)
    replayed = runtime.recover_failed_target_root(request.request_ref)

    assert recovered.run.status == "admitted"
    assert replayed is recovered
    assert gateway._counter == 1
    assert owner.recovery_transitions == 1
    completed = runtime.run_or_resume_target_root(
        request.request_ref,
        prompt="Own implementation through final training completion.",
        mcp_base_url="http://127.0.0.1:8765",
    )
    assert completed.status == "executed"
    assert [operation.generation for operation in owner.operations] == [1]


def test_failed_resume_turn_recovers_after_restart_in_same_native_session(
    tmp_path: Path,
) -> None:
    runtime, owner, initial_adapter = _runtime(tmp_path)
    prompt = "Own implementation through final training completion."
    initial = runtime.run_or_resume_target_root(
        owner.run.request_ref,
        prompt=prompt,
        mcp_base_url="http://127.0.0.1:8765",
    )
    identity = (
        owner.run.run_ref,
        owner.run.root_session_ref,
        owner.run.attempt_ref,
        owner.run.fence_ref,
        owner.run.attempt_generation,
    )
    failing_adapter = _TargetRootAdapter(
        "codex", fail_first_code="provider_process_failed"
    )
    failing = HarnessRuntime(
        owner,
        _Gateway(),
        (failing_adapter, _TargetRootAdapter("claude")),
    )
    failing.bind_target_workspace_resolver(_WorkspaceResolver(tmp_path))

    with pytest.raises(HarnessAdmissionError, match="provider_process_failed"):
        failing.run_or_resume_target_root(
            owner.run.request_ref,
            prompt="Continue the same Target root lifecycle.",
            mcp_base_url="http://127.0.0.1:8765",
        )
    assert owner.run.status == "failed"
    assert owner.operations[-1].status == "failed"

    continuation_adapter = _TargetRootAdapter("codex")
    restarted = HarnessRuntime(
        owner,
        _Gateway(),
        (continuation_adapter, _TargetRootAdapter("claude")),
    )
    restarted.bind_target_workspace_resolver(_WorkspaceResolver(tmp_path))
    recovered = restarted.recover_failed_target_root(owner.run.request_ref)
    completed = restarted.run_or_resume_target_root(
        owner.run.request_ref,
        prompt="Continue the same Target root lifecycle.",
        mcp_base_url="http://127.0.0.1:8765",
    )

    assert recovered.run.status == "executed"
    assert completed.native_session_ref == initial.native_session_ref
    assert initial_adapter.invocations[0].native_session_ref is None
    assert failing_adapter.invocations[0].native_session_ref == (
        initial.native_session_ref
    )
    assert continuation_adapter.invocations[0].native_session_ref == (
        initial.native_session_ref
    )
    assert [operation.generation for operation in owner.operations] == [1, 2, 3]
    assert [operation.status for operation in owner.operations] == [
        "executed",
        "failed",
        "executed",
    ]
    assert (
        owner.run.run_ref,
        owner.run.root_session_ref,
        owner.run.attempt_ref,
        owner.run.fence_ref,
        owner.run.attempt_generation,
    ) == identity
    assert {
        invocation.working_directory
        for invocation in (
            *initial_adapter.invocations,
            *failing_adapter.invocations,
            *continuation_adapter.invocations,
        )
    } == {str(tmp_path)}


def test_recovery_replays_temporary_channel_failure_without_stale_cache(
    tmp_path: Path,
) -> None:
    request = _target_request()
    owner = _Owner(request)

    class FlakyGateway(_Gateway):
        issue_attempts = 0

        def issue_channel(self, **values: object):
            self.issue_attempts += 1
            if self.issue_attempts == 2:
                raise SemanticMcpError("mcp_channel_temporarily_unavailable")
            return super().issue_channel(**values)

    gateway = FlakyGateway()
    adapter = _TargetRootAdapter(
        "codex", fail_first_code="provider_process_failed"
    )
    runtime = HarnessRuntime(
        owner,
        gateway,
        (adapter, _TargetRootAdapter("claude")),
    )
    runtime.bind_target_workspace_resolver(_WorkspaceResolver(tmp_path))
    prompt = "Own implementation through final training completion."

    with pytest.raises(HarnessAdmissionError, match="provider_process_failed"):
        runtime.run_or_resume_target_root(
            request.request_ref,
            prompt=prompt,
            mcp_base_url="http://127.0.0.1:8765",
        )
    with pytest.raises(
        HarnessAdmissionError,
        match="mcp_channel_temporarily_unavailable",
    ):
        runtime.recover_failed_target_root(request.request_ref)
    assert owner.run.status == "admitted"
    assert owner.run.failure_code == TARGET_ROOT_RECOVERY_PENDING_CODE

    recovered = runtime.recover_failed_target_root(request.request_ref)
    assert owner.run.failure_code == TARGET_ROOT_RECOVERY_READY_CODE
    completed = runtime.run_or_resume_target_root(
        request.request_ref,
        prompt=prompt,
        mcp_base_url="http://127.0.0.1:8765",
    )

    assert recovered.run.status == "admitted"
    assert owner.run.failure_code is None
    assert completed.status == "executed"
    assert gateway.issue_attempts == 3
    assert owner.recovery_transitions == 1
    assert [operation.generation for operation in owner.operations] == [1, 2]
    assert [operation.status for operation in owner.operations] == [
        "failed",
        "executed",
    ]


def test_target_root_lifecycle_chooses_first_then_resume_in_one_native_session(
    tmp_path: Path,
) -> None:
    runtime, owner, adapter = _runtime(tmp_path)

    first = runtime.run_or_resume_target_root(
        "target-harness-request:test",
        prompt="Own implementation through final training completion.",
        mcp_base_url="http://127.0.0.1:8765",
    )
    resumed = runtime.run_or_resume_target_root(
        "target-harness-request:test",
        prompt="Continue the same Target root lifecycle.",
        mcp_base_url="http://127.0.0.1:8765",
    )

    assert TARGET_ROOT_LIFECYCLE_PHASE == "target_root_lifecycle"
    assert first.native_session_ref == "native-target-root"
    assert resumed.native_session_ref == first.native_session_ref
    assert [item.native_session_ref for item in adapter.invocations] == [
        None,
        "native-target-root",
    ]
    assert [item.mcp_token for item in adapter.invocations] == [
        "token:1",
        "token:1",
    ]
    assert [item.generation for item in owner.operations] == [1, 2]
    assert all(item.status == "executed" for item in owner.operations)


@pytest.mark.parametrize("sink_fails", [False, True])
def test_target_root_publishes_diagnostics_without_gating_owner_completion(
    tmp_path: Path,
    sink_fails: bool,
) -> None:
    request = _target_request()
    owner = _Owner(request)
    adapter = _TargetRootAdapter("codex")
    recorder = _DiagnosticRecorder(fail=sink_fails)
    runtime = HarnessRuntime(
        owner,
        _Gateway(),
        (adapter,),
        root_operation_diagnostic_recorder=recorder,  # type: ignore[arg-type]
    )
    runtime.bind_target_workspace_resolver(_WorkspaceResolver(tmp_path))

    completed = runtime.run_or_resume_target_root(
        request.request_ref,
        prompt="Own this exact Target operation.",
        mcp_base_url="http://127.0.0.1:8765",
    )

    assert completed.status == "executed"
    assert owner.operations[-1].status == "executed"
    if sink_fails:
        assert recorder.records == []
        return
    assert len(recorder.records) == 1
    recorded = recorder.records[0]
    assert recorded["root_kind"] == "target"
    diagnostics = recorded["diagnostics"]
    assert isinstance(diagnostics, dict)
    assert diagnostics["side_effect_authorization"]["operation_ids"] == list(
        TARGET_ROOT_SEMANTIC_OPERATION_IDS
    )
    assert diagnostics["usage"]["shell"]["status"] == "used"


def test_target_root_lifecycle_reconciles_unknown_outcome_inside_one_call(
    tmp_path: Path,
) -> None:
    runtime, owner, adapter = _runtime(tmp_path, fail_first_unknown=True)

    completed = runtime.run_or_resume_target_root(
        "target-harness-request:test",
        prompt="Own implementation through final training completion.",
        mcp_base_url="http://127.0.0.1:8765",
    )

    assert completed.status == "executed"
    assert len(owner.operations) == 1
    assert len(adapter.invocations) == 2
    assert (
        adapter.invocations[0].provider_operation_ref
        == adapter.invocations[1].provider_operation_ref
    )
    assert all(item.native_session_ref is None for item in adapter.invocations)


def test_target_root_lifecycle_resumes_native_session_after_runtime_restart(
    tmp_path: Path,
) -> None:
    runtime, owner, _adapter = _runtime(tmp_path)
    initial = runtime.run_or_resume_target_root(
        "target-harness-request:test",
        prompt="Own implementation through final training completion.",
        mcp_base_url="http://127.0.0.1:8765",
    )
    continuation_adapter = _TargetRootAdapter("codex")
    restarted = HarnessRuntime(
        owner,
        _Gateway(),
        (continuation_adapter, _TargetRootAdapter("claude")),
    )
    restarted.bind_target_workspace_resolver(_WorkspaceResolver(tmp_path))

    resumed = restarted.run_or_resume_target_root(
        "target-harness-request:test",
        prompt="Continue the same Target root lifecycle.",
        mcp_base_url="http://127.0.0.1:8765",
    )

    assert resumed.native_session_ref == initial.native_session_ref
    assert continuation_adapter.invocations[0].native_session_ref == (
        "native-target-root"
    )
    assert [item.generation for item in owner.operations] == [1, 2]


def test_target_root_lifecycle_reconciles_durable_unknown_after_restart(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "runtime-protection")
    upgrade_database(data_root.database)
    database = Database(data_root.database)
    feed = DurableFeed(database)
    feed.ensure_initialized()
    protection = RuntimeProtection(
        database=database,
        feed=feed,
        inhibitor=_TestPowerInhibitor(),
        event_logger=RuntimeEventLogger(data_root.logs / "runtime.jsonl"),
    )
    boundary_recorder = RuntimeBoundaryRecorder(database)
    request = _target_request()
    owner = _Owner(request)
    interrupted_adapter = _TargetRootAdapter(
        "codex", fail_first_code="provider_timeout"
    )
    interrupted = HarnessRuntime(
        owner,
        _Gateway(),
        (interrupted_adapter, _TargetRootAdapter("claude")),
        runtime_protection=protection,
        runtime_boundary_recorder=boundary_recorder,
    )
    interrupted.bind_target_workspace_resolver(_WorkspaceResolver(tmp_path))
    prompt = "Own implementation through final training completion."

    with pytest.raises(HarnessAdmissionError, match="provider_timeout"):
        interrupted.run_or_resume_target_root(
            request.request_ref,
            prompt=prompt,
            mcp_base_url="http://127.0.0.1:8765",
        )
    operation_ref = interrupted_adapter.invocations[0].provider_operation_ref
    assert owner.run.status == "running"
    assert owner.operations[0].status == "unknown_outcome"
    assert protection.query_evidence()["inhibitor"]["active_count"] == 1

    recovery_adapter = _TargetRootAdapter("codex")
    recovered = HarnessRuntime(
        owner,
        _Gateway(),
        (recovery_adapter, _TargetRootAdapter("claude")),
        runtime_protection=protection,
        runtime_boundary_recorder=boundary_recorder,
    )
    recovered.bind_target_workspace_resolver(_WorkspaceResolver(tmp_path))
    completed = recovered.run_or_resume_target_root(
        request.request_ref,
        prompt=prompt,
        mcp_base_url="http://127.0.0.1:8765",
    )

    assert completed.status == "executed"
    assert len(owner.operations) == 1
    assert recovery_adapter.invocations[0].provider_operation_ref == operation_ref
    assert protection.query_evidence()["inhibitor"]["active_count"] == 0
    protection.close()
    database.close()


def test_target_root_reconciliation_keeps_the_admitted_operation_timeout(
    tmp_path: Path,
) -> None:
    admitted_timeout_seconds = 7 * 24 * 60 * 60
    request = _target_request(
        provider_operation_timeout_seconds=admitted_timeout_seconds
    )
    owner = _Owner(request)
    interrupted_adapter = _TargetRootAdapter(
        "codex",
        fail_first_code="provider_timeout",
        target_root_timeout_seconds=admitted_timeout_seconds,
    )
    interrupted = HarnessRuntime(
        owner,
        _Gateway(),
        (interrupted_adapter, _TargetRootAdapter("claude")),
    )
    interrupted.bind_target_workspace_resolver(_WorkspaceResolver(tmp_path))
    prompt = "Own implementation through final training completion."

    with pytest.raises(
        HarnessAdmissionError, match="provider_timeout"
    ):
        interrupted.run_or_resume_target_root(
            request.request_ref,
            prompt=prompt,
            mcp_base_url="http://127.0.0.1:8765",
        )
    original_operation = owner.operations[0]

    restarted_adapter = _TargetRootAdapter(
        "codex", target_root_timeout_seconds=60.0
    )
    restarted = HarnessRuntime(
        owner,
        _Gateway(),
        (restarted_adapter, _TargetRootAdapter("claude")),
    )
    restarted.bind_target_workspace_resolver(_WorkspaceResolver(tmp_path))
    completed = restarted.run_or_resume_target_root(
        request.request_ref,
        prompt=prompt,
        mcp_base_url="http://127.0.0.1:8765",
    )

    assert completed.status == "executed"
    assert len(owner.operations) == 1
    assert owner.operations[0].operation_ref == original_operation.operation_ref
    assert owner.operations[0].invocation_hash == original_operation.invocation_hash
    assert interrupted_adapter.invocations[0].provider_operation_timeout_seconds == (
        admitted_timeout_seconds
    )
    assert restarted_adapter.invocations[0].provider_operation_timeout_seconds == (
        admitted_timeout_seconds
    )

    changed_request = replace(
        request, provider_operation_timeout_seconds=60.0
    )
    changed_owner = _Owner(changed_request)
    changed_adapter = _TargetRootAdapter(
        "codex",
        fail_first_code="provider_timeout",
        target_root_timeout_seconds=60.0,
    )
    changed_runtime = HarnessRuntime(
        changed_owner,
        _Gateway(),
        (changed_adapter, _TargetRootAdapter("claude")),
    )
    changed_runtime.bind_target_workspace_resolver(
        _WorkspaceResolver(tmp_path)
    )
    with pytest.raises(HarnessAdmissionError, match="provider_timeout"):
        changed_runtime.run_or_resume_target_root(
            changed_request.request_ref,
            prompt=prompt,
            mcp_base_url="http://127.0.0.1:8765",
        )
    assert changed_owner.operations[0].operation_ref == (
        original_operation.operation_ref
    )
    assert changed_owner.operations[0].invocation_hash != (
        original_operation.invocation_hash
    )


def test_legacy_target_request_without_frozen_timeout_fails_closed(
    tmp_path: Path,
) -> None:
    request = _target_request(provider_operation_timeout_seconds=None)
    owner = _Owner(request)
    restarted_adapter = _TargetRootAdapter(
        "codex", target_root_timeout_seconds=60.0
    )
    runtime = HarnessRuntime(
        owner,
        _Gateway(),
        (restarted_adapter, _TargetRootAdapter("claude")),
    )
    runtime.bind_target_workspace_resolver(_WorkspaceResolver(tmp_path))

    with pytest.raises(
        HarnessAdmissionError,
        match="target_harness_runtime_binding_unavailable",
    ):
        runtime.run_or_resume_target_root(
            request.request_ref,
            prompt="Own implementation through final training completion.",
            mcp_base_url="http://127.0.0.1:8765",
        )

    assert "provider_operation_timeout_seconds" not in owner.run.request
    assert restarted_adapter.invocations == []


def test_target_root_repeated_unknown_reconciliation_keeps_one_operation_and_hold(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "runtime-protection-repeated")
    upgrade_database(data_root.database)
    database = Database(data_root.database)
    feed = DurableFeed(database)
    feed.ensure_initialized()
    inhibitor = _TestPowerInhibitor()
    request = _target_request()
    owner = _Owner(request)
    prompt = "Own implementation through final training completion."

    class TwiceUnknownAdapter(_TargetRootAdapter):
        def invoke(self, invocation: HarnessInvocation) -> HarnessTurnEvidence:
            if len(self.invocations) < 3:
                self.invocations.append(invocation)
                raise HarnessAdapterUnavailable("provider_outcome_unknown")
            return super().invoke(invocation)

    adapter = TwiceUnknownAdapter("codex")

    def build_restarted() -> tuple[RuntimeProtection, HarnessRuntime]:
        protection = RuntimeProtection(
            database=database,
            feed=feed,
            inhibitor=inhibitor,
            event_logger=RuntimeEventLogger(data_root.logs / "runtime.jsonl"),
        )
        runtime = HarnessRuntime(
            owner,
            _Gateway(),
            (adapter, _TargetRootAdapter("claude")),
            runtime_protection=protection,
            runtime_boundary_recorder=RuntimeBoundaryRecorder(database),
        )
        runtime.bind_target_workspace_resolver(_WorkspaceResolver(tmp_path))
        return protection, runtime

    protection, runtime = build_restarted()
    with pytest.raises(HarnessAdmissionError, match="provider_outcome_unknown"):
        runtime.run_or_resume_target_root(
            request.request_ref,
            prompt=prompt,
            mcp_base_url="http://127.0.0.1:8765",
        )
    operation_ref = adapter.invocations[0].provider_operation_ref
    protection.close()

    protection, runtime = build_restarted()
    with pytest.raises(HarnessAdmissionError, match="provider_outcome_unknown"):
        runtime.run_or_resume_target_root(
            request.request_ref,
            prompt=prompt,
            mcp_base_url="http://127.0.0.1:8765",
        )
    assert protection.query_evidence()["inhibitor"]["active_count"] == 3
    protection.close()

    protection, runtime = build_restarted()
    completed = runtime.run_or_resume_target_root(
        request.request_ref,
        prompt=prompt,
        mcp_base_url="http://127.0.0.1:8765",
    )

    assert completed.status == "executed"
    assert len(owner.operations) == 1
    assert [item.provider_operation_ref for item in adapter.invocations] == [
        operation_ref,
        operation_ref,
        operation_ref,
        operation_ref,
    ]
    evidence = protection.query_evidence()
    assert evidence["inhibitor"]["active_count"] == 0
    assert evidence["responsibilities"] == []
    protection.close()
    database.close()


def test_target_root_restart_replays_owner_commit_before_runtime_finish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = prepare_data_root(tmp_path / "runtime-protection-owner-commit")
    upgrade_database(data_root.database)
    database = Database(data_root.database)
    feed = DurableFeed(database)
    feed.ensure_initialized()
    inhibitor = _TestPowerInhibitor()
    request = _target_request()
    owner = _Owner(request)
    protection = RuntimeProtection(
        database=database,
        feed=feed,
        inhibitor=inhibitor,
        event_logger=RuntimeEventLogger(data_root.logs / "runtime.jsonl"),
    )
    interrupted = HarnessRuntime(
        owner,
        _Gateway(),
        (_TargetRootAdapter("codex"), _TargetRootAdapter("claude")),
        runtime_protection=protection,
        runtime_boundary_recorder=RuntimeBoundaryRecorder(database),
    )
    interrupted.bind_target_workspace_resolver(_WorkspaceResolver(tmp_path))
    monkeypatch.setattr(interrupted, "_finish_runtime_effect", lambda *_a, **_k: None)

    completed = interrupted.run_or_resume_target_root(
        request.request_ref,
        prompt="Own implementation through final training completion.",
        mcp_base_url="http://127.0.0.1:8765",
    )
    assert completed.status == "executed"
    assert protection.query_evidence()["inhibitor"]["active_count"] == 1
    protection.close()

    restarted_protection = RuntimeProtection(
        database=database,
        feed=feed,
        inhibitor=inhibitor,
        event_logger=RuntimeEventLogger(data_root.logs / "runtime.jsonl"),
    )
    HarnessRuntime(
        owner,
        _Gateway(),
        (_TargetRootAdapter("codex"), _TargetRootAdapter("claude")),
        runtime_protection=restarted_protection,
        runtime_boundary_recorder=RuntimeBoundaryRecorder(database),
    )

    evidence = restarted_protection.query_evidence()
    assert evidence["inhibitor"]["active_count"] == 0
    assert evidence["responsibilities"] == []
    restarted_protection.close()
    database.close()


def test_target_root_power_wait_never_calls_provider_and_can_reconcile(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "runtime-protection-power-wait")
    upgrade_database(data_root.database)
    database = Database(data_root.database)
    feed = DurableFeed(database)
    feed.ensure_initialized()
    request = _target_request()
    owner = _Owner(request)
    adapter = _TargetRootAdapter("codex")
    protection = RuntimeProtection(
        database=database,
        feed=feed,
        inhibitor=_FailOncePowerInhibitor(),
        event_logger=RuntimeEventLogger(data_root.logs / "runtime.jsonl"),
    )
    runtime = HarnessRuntime(
        owner,
        _Gateway(),
        (adapter, _TargetRootAdapter("claude")),
        runtime_protection=protection,
        runtime_boundary_recorder=RuntimeBoundaryRecorder(database),
    )
    runtime.bind_target_workspace_resolver(_WorkspaceResolver(tmp_path))
    prompt = "Own implementation through final training completion."

    with pytest.raises(
        HarnessAdmissionError, match="power_inhibitor_systemd_acquire_failed"
    ):
        runtime.run_or_resume_target_root(
            request.request_ref,
            prompt=prompt,
            mcp_base_url="http://127.0.0.1:8765",
        )
    assert adapter.invocations == []
    assert owner.run.status == "running"
    assert owner.operations[0].status == "unknown_outcome"
    assert protection.query_evidence()["durable_waiting"]

    completed = runtime.run_or_resume_target_root(
        request.request_ref,
        prompt=prompt,
        mcp_base_url="http://127.0.0.1:8765",
    )

    assert completed.status == "executed"
    assert len(adapter.invocations) == 1
    evidence = protection.query_evidence()
    assert evidence["responsibilities"] == []
    assert evidence["inhibitor"]["active_count"] == 0
    protection.close()
    database.close()


def test_target_root_cancel_delegates_exact_current_durable_operation() -> None:
    request = _target_request()
    owner = _Owner(request)

    class Canceller:
        calls: list[str] = []
        results = iter((False, True))

        def cancel_operation(self, invocation_hash: str) -> bool:
            self.calls.append(invocation_hash)
            return next(self.results)

    canceller = Canceller()
    runtime = HarnessRuntime(
        owner,
        _Gateway(),
        (_TargetRootAdapter("codex"), _TargetRootAdapter("claude")),
        operation_canceller=canceller,
    )
    original_identity = (
        owner.run.run_ref,
        owner.run.attempt_ref,
        owner.run.root_session_ref,
        owner.run.fence_ref,
        owner.run.attempt_generation,
    )
    owner.start_operation(
        run_ref=owner.run.run_ref,
        operation_ref="target-run:test:harness_turn:1",
        generation=1,
        invocation_hash="d" * 64,
        resume=False,
    )

    assert runtime.cancel_target_root(request.request_ref) is False
    assert runtime.cancel_target_root(request.request_ref) is True
    assert canceller.calls == ["d" * 64, "d" * 64]
    assert (
        owner.run.run_ref,
        owner.run.attempt_ref,
        owner.run.root_session_ref,
        owner.run.fence_ref,
        owner.run.attempt_generation,
    ) == original_identity
    assert len(owner.operations) == 1


def test_harness_exposes_typed_owner_completion_without_profile_parsing(
    tmp_path: Path,
) -> None:
    runtime, owner, _adapter = _runtime(tmp_path)
    handoff = TargetCompletionHandoff(
        schema_ref="meta-research/target-completion-handoff/v1",
        target_ref="target:test",
        target_run_ref="target-run:test",
        status="completed",
        artifacts=(
            TargetCompletionArtifact(
                role="result", relative_path="outputs/result.json"
            ),
        ),
        result_document_path="outputs/result.json",
        summary="Final training completed.",
    )
    evidence = TargetRootCompletionEvidence(
        target_ref="target:test",
        target_run_ref="target-run:test",
        attempt_ref="target-attempt:test",
        attempt_generation=1,
        root_session_ref="target-root-session:test",
        native_session_ref="native-target-root",
        fence_ref="target-fence:test",
        operation_ref="harness-operation:test",
        operation_generation=3,
        evidence_ref="harness-evidence:test",
        evidence_sequence=7,
        handoff=handoff,
        observed_at=2.0,
    )
    owner.completion_evidence = evidence

    assert runtime.query_target_root_completion_evidence("target:test") is evidence

    owner.completion_query_error = "target_root_completion_evidence_invalid"
    with pytest.raises(
        HarnessAdmissionError,
        match="target_root_completion_evidence_invalid",
    ):
        runtime.query_target_root_completion_evidence("target:test")


def test_harness_exposes_paginated_root_observations_without_owner_leakage(
    tmp_path: Path,
) -> None:
    runtime, owner, _adapter = _runtime(tmp_path)
    page = TargetRootObservationPage(
        target_ref="target:test",
        target_run_ref="target-run:test",
        attempt_ref="target-attempt:test",
        attempt_generation=1,
        root_session_ref="target-root-session:test",
        native_session_ref="native-target-root",
        fence_ref="target-fence:test",
        stream_ref="target-root-stream:test",
        status="running",
        items=(),
        next_cursor="cursor:9",
        head_cursor="cursor:9",
        has_more=False,
    )
    owner.observation_page = page

    assert runtime.query_target_root_observations(
        "target:test", after_cursor="cursor:4", limit=5
    ) is page
    assert owner.observation_query == ("target:test", "cursor:4", 5)

    owner.observation_query_error = "target_root_observation_cursor_invalid"
    with pytest.raises(
        HarnessAdmissionError,
        match="target_root_observation_cursor_invalid",
    ):
        runtime.query_target_root_observations("target:test")


def test_target_root_lifecycle_does_not_require_unused_optional_capabilities(
    tmp_path: Path,
) -> None:
    runtime, owner, adapter = _runtime(
        tmp_path,
        missing_capabilities=_TARGET_ROOT_OPTIONAL_CAPABILITIES,
    )

    first = runtime.run_or_resume_target_root(
        "target-harness-request:test",
        prompt="Implement and train using the native workspace.",
        mcp_base_url="http://127.0.0.1:8765",
    )
    resumed = runtime.run_or_resume_target_root(
        "target-harness-request:test",
        prompt="Continue in the same native session.",
        mcp_base_url="http://127.0.0.1:8765",
    )

    assert first.status == "executed"
    assert resumed.native_session_ref == first.native_session_ref
    assert [item.native_session_ref for item in adapter.invocations] == [
        None,
        "native-target-root",
    ]
    assert all(item.status == "executed" for item in owner.operations)


@pytest.mark.parametrize(
    "capability", sorted(_TARGET_ROOT_REQUIRED_EVIDENCE)
)
def test_target_root_lifecycle_fails_when_required_capability_is_unavailable(
    tmp_path: Path, capability: str
) -> None:
    runtime, _owner, _adapter = _runtime(
        tmp_path, missing_capabilities=frozenset({capability})
    )

    with pytest.raises(
        HarnessAdmissionError,
        match="required_harness_capability_unavailable",
    ):
        runtime.run_or_resume_target_root(
            "target-harness-request:test",
            prompt="Own implementation through final training completion.",
            mcp_base_url="http://127.0.0.1:8765",
        )


@pytest.mark.parametrize(
    "capability", sorted(_TARGET_ROOT_REQUIRED_EVIDENCE)
)
def test_target_root_lifecycle_does_not_require_unused_capability_evidence(
    tmp_path: Path, capability: str
) -> None:
    runtime, _owner, _adapter = _runtime(
        tmp_path, evidence_free_capability=capability
    )

    result = runtime.run_or_resume_target_root(
        "target-harness-request:test",
        prompt="Own implementation through final training completion.",
        mcp_base_url="http://127.0.0.1:8765",
    )

    assert result.status == "executed"
