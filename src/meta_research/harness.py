from __future__ import annotations

import hashlib
import ipaddress
import re
import secrets
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import urlsplit

from meta_research.bundle_protocol import TargetWorkHandle
from meta_research.harness_adapters import (
    HARNESS_CAPABILITIES,
    HarnessAdapter,
    HarnessAdapterUnavailable,
    HarnessInvocation,
)
from meta_research.owners.agent_runtime_harness import (
    AgentRuntimeHarnessError,
    AgentRuntimeHarnessInterface,
    AgentRuntimeHarnessOperation,
    AgentRuntimeHarnessRetry,
    AgentRuntimeHarnessRetryLater,
    AgentRuntimeHarnessRun,
    AgentRuntimeTargetChildSession,
    TargetRootCompletionEvidence,
    TargetRootObservationPage,
)
from meta_research.owners.common import canonical_hash
from meta_research.provider_supervisor import provider_operation_ref
from meta_research.semantic_mcp import (
    McpConnection,
    ResidentMcpBinding,
    SemanticMcpError,
    SemanticMcpGateway,
)
from meta_research.semantic_owner_gateway import (
    BUNDLE_ROOT_SEMANTIC_OPERATION_IDS,
    TARGET_RUN_SEMANTIC_OPERATION_IDS,
)
from meta_research.target_run_runtime_contract import TargetCompletionHandoff


HarnessFamily = Literal["codex", "claude"]
TargetTurnPhase = Literal["implementation_review", "execution", "result_review"]
TARGET_ROOT_LIFECYCLE_PHASE = "target_root_lifecycle"
_AUTH_PROFILE_REF = re.compile(r"^harness-profile:[A-Za-z0-9][A-Za-z0-9._:-]*$")
_CONFORMANCE_TOKEN = re.compile(r"^[0-9a-f]{16}$")

FULL_CONFORMANCE_V1 = "meta-research/harness-full-conformance/v1"
FULL_CONFORMANCE_FAMILIES: tuple[HarnessFamily, ...] = ("codex", "claude")
FULL_CONFORMANCE_OPERATION_IDS = tuple(
    sorted(
        {
            "advancement_engine.snapshot.read",
            "agent_runtime.host_compute.observe",
            "agent_runtime.host_compute.reconcile",
            "agent_runtime.snapshot.read",
            "human_collaboration.snapshot.read",
            "research_graph.quest_receipt.verify",
            "research_graph.snapshot.read",
            "research_memory.snapshot.read",
            *BUNDLE_ROOT_SEMANTIC_OPERATION_IDS,
            *TARGET_RUN_SEMANTIC_OPERATION_IDS,
        }
    )
)
_FULL_CONFORMANCE_INITIAL_CAPABILITIES = tuple(
    capability for capability in HARNESS_CAPABILITIES if capability != "resume"
)
_FULL_CONFORMANCE_INITIAL_PROMPT = """\
Run the installed Harness full-conformance contract. Exercise every available
tool category through real provider operations: inspect the tool inventory; run
a bounded shell command; create, read, and update a file only in the assigned
workspace; call research_graph.snapshot.read through the configured Semantic
MCP server; invoke one installed Skill, plugin, and hook; spawn one real
subagent and wait for its terminal result; perform a live Web Search for the MCP
2025-06-18 specification and then Fetch the resulting official specification
page; and exercise the provider-native fork, steer, and interrupt controls.
Return normally only after the streamed evidence for those operations is
complete. Do not merely describe or self-report a capability.
"""
_FULL_CONFORMANCE_RESUME_PROMPT = """\
Resume this exact native Harness Session for the second full-conformance turn.
Perform one bounded shell operation and one Semantic MCP
research_graph.snapshot.read call, then return normally with streamed provider
evidence. Do not start a replacement Session and do not self-report capability.
"""


class HarnessAdmissionError(RuntimeError):
    def __init__(self, code: str, *, next_retry_at: float | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.next_retry_at = next_retry_at


class ResidentMcpScopeVerifier(Protocol):
    def verify_bundle_runtime_scope(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        root_session_ref: str,
        fence_ref: str,
        runtime_binding_hash: str,
    ) -> None: ...


class TargetWorkspaceResolver(Protocol):
    def resolve_target_workspace(
        self,
        *,
        target_ref: str,
        target_run_ref: str,
        root_session_ref: str,
        attempt_ref: str,
        fence_ref: str,
    ) -> tuple[str, Path]: ...


class HarnessOperationCanceller(Protocol):
    """Mechanical control for one already-started durable provider effect."""

    def cancel_operation(self, invocation_hash: str) -> bool: ...


@dataclass(frozen=True)
class HarnessProbeRequest:
    request_ref: str
    harness_family: HarnessFamily
    model_ref: str
    auth_profile_ref: str
    required_operation_ids: tuple[str, ...]
    required_capabilities: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "request_ref": self.request_ref,
            "harness_family": self.harness_family,
            "model_ref": self.model_ref,
            "auth_profile_ref": self.auth_profile_ref,
            "required_operation_ids": list(self.required_operation_ids),
            "required_capabilities": list(self.required_capabilities),
        }


@dataclass(frozen=True)
class TargetHarnessRequest(HarnessProbeRequest):
    """One independently admitted TargetRun root Harness request."""

    target_ref: str
    target_run_ref: str
    full_conformance_binding: dict[str, object]
    full_conformance_binding_hash: str
    target_scope_binding_hash: str

    def as_dict(self) -> dict[str, object]:
        return {
            **super().as_dict(),
            "target_ref": self.target_ref,
            "target_run_ref": self.target_run_ref,
            "full_conformance_binding": self.full_conformance_binding,
            "full_conformance_binding_hash": self.full_conformance_binding_hash,
            "target_scope_binding_hash": self.target_scope_binding_hash,
        }


@dataclass(frozen=True)
class HarnessProbeRun:
    request_ref: str
    run_ref: str
    attempt_ref: str
    attempt_generation: int
    root_session_ref: str
    native_session_ref: str | None
    fence_ref: str
    harness_family: HarnessFamily
    model_ref: str
    auth_profile_ref: str
    capability_binding_hash: str
    mcp_binding: ResidentMcpBinding
    status: str = "admitted"

    def as_public_dict(self) -> dict[str, object]:
        return {
            "request_ref": self.request_ref,
            "run_ref": self.run_ref,
            "attempt_ref": self.attempt_ref,
            "attempt_generation": self.attempt_generation,
            "root_session_ref": self.root_session_ref,
            "native_session_ref": self.native_session_ref,
            "fence_ref": self.fence_ref,
            "harness_family": self.harness_family,
            "model_ref": self.model_ref,
            "auth_profile_ref": self.auth_profile_ref,
            "capability_binding_hash": self.capability_binding_hash,
            "mcp_binding": self.mcp_binding.as_dict(),
            "status": self.status,
        }


@dataclass(frozen=True)
class HarnessAdmission:
    run: HarnessProbeRun
    connection: McpConnection


@dataclass(frozen=True)
class ResidentMcpChannel:
    """One ephemeral credential for an already Owner-admitted runtime scope."""

    connection: McpConnection
    binding: ResidentMcpBinding


@dataclass(frozen=True)
class _ResidentMcpScope:
    run_ref: str
    attempt_ref: str
    root_session_ref: str
    fence_ref: str
    capability_binding_hash: str


@dataclass(frozen=True)
class FullConformanceRequest:
    codex_model_ref: str
    codex_auth_profile_ref: str
    claude_model_ref: str
    claude_auth_profile_ref: str

    def provider_configuration(
        self, family: HarnessFamily
    ) -> tuple[str, str]:
        if family == "codex":
            return self.codex_model_ref, self.codex_auth_profile_ref
        return self.claude_model_ref, self.claude_auth_profile_ref


@dataclass(frozen=True)
class FullConformanceRunSet:
    conformance_ref: str
    contract_ref: str
    contract_hash: str
    runs: tuple[HarnessProbeRun, ...]

    def as_public_dict(self) -> dict[str, object]:
        return {
            "status": "admitted",
            "conformance_ref": self.conformance_ref,
            "contract_ref": self.contract_ref,
            "contract_hash": self.contract_hash,
            "runs": [
                {
                    "request_ref": run.request_ref,
                    "run_ref": run.run_ref,
                    "harness_family": run.harness_family,
                    "status": run.status,
                }
                for run in self.runs
            ],
        }


@dataclass(frozen=True)
class FullConformanceBinding:
    """Content-addressed summary of the current fixed Harness contract.

    This is a projection of Agent Runtime's accepted conformance Runs, not a
    second readiness authority.  Consumers freeze it into their own runtime
    admission and compare it with a freshly projected value before resuming a
    provider side effect.
    """

    contract_ref: str
    contract_hash: str
    conformance_ref: str
    semantic_mcp_catalog_hash: str
    semantic_mcp_operation_bindings_hash: str
    required_families: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    required_operation_ids: tuple[str, ...]
    profile_receipts: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "contract_ref": self.contract_ref,
            "contract_hash": self.contract_hash,
            "conformance_ref": self.conformance_ref,
            "semantic_mcp_catalog_hash": self.semantic_mcp_catalog_hash,
            "semantic_mcp_operation_bindings_hash": (
                self.semantic_mcp_operation_bindings_hash
            ),
            "required_families": list(self.required_families),
            "required_capabilities": list(self.required_capabilities),
            "required_operation_ids": list(self.required_operation_ids),
            "profile_receipts": list(self.profile_receipts),
        }

    @property
    def binding_hash(self) -> str:
        return canonical_hash(self.as_dict())


class HarnessRuntime:
    """One Typed Run interface shared by every supported native Harness."""

    def __init__(
        self,
        owner: AgentRuntimeHarnessInterface,
        gateway: SemanticMcpGateway,
        adapters: tuple[HarnessAdapter, ...],
        *,
        operation_canceller: HarnessOperationCanceller | None = None,
    ) -> None:
        self._owner = owner
        self._gateway = gateway
        self._adapters = {adapter.family: adapter for adapter in adapters}
        if set(self._adapters) != {"codex", "claude"}:
            raise HarnessAdmissionError("harness_adapter_catalog_invalid")
        try:
            self._full_conformance_operation_bindings = (
                self._gateway.required_bindings(
                    FULL_CONFORMANCE_OPERATION_IDS
                )
            )
        except SemanticMcpError as error:
            raise HarnessAdmissionError(error.code) from error
        gateway_status = self._gateway.query_status()
        catalog_hash = gateway_status.get("catalog_hash")
        if not isinstance(catalog_hash, str) or len(catalog_hash) != 64:
            raise HarnessAdmissionError("semantic_mcp_catalog_invalid")
        self._full_conformance_catalog_hash = catalog_hash
        self._full_conformance_contract_hash = canonical_hash(
            {
                "contract_ref": FULL_CONFORMANCE_V1,
                "harness_families": list(FULL_CONFORMANCE_FAMILIES),
                "locked_versions": {
                    family: self._adapters[family].locked_version
                    for family in FULL_CONFORMANCE_FAMILIES
                },
                "required_capabilities": list(HARNESS_CAPABILITIES),
                "required_operation_bindings": list(
                    self._full_conformance_operation_bindings
                ),
                "semantic_mcp_catalog_hash": catalog_hash,
                "turns": ["initial", "resume"],
            }
        )
        self._admissions: dict[str, tuple[str, HarnessAdmission]] = {}
        self._admissions_by_request: dict[str, HarnessAdmission] = {}
        self._resident_scope_verifier: ResidentMcpScopeVerifier | None = None
        self._target_workspace_resolver: TargetWorkspaceResolver | None = None
        self._operation_canceller = operation_canceller
        self._resident_channel_scopes: dict[str, _ResidentMcpScope] = {}
        self._recovered_target_requests: dict[str, int] = {}

    def bind_target_workspace_resolver(
        self, resolver: TargetWorkspaceResolver
    ) -> None:
        if (
            self._target_workspace_resolver is not None
            and self._target_workspace_resolver is not resolver
        ):
            raise HarnessAdmissionError("target_workspace_resolver_already_bound")
        self._target_workspace_resolver = resolver

    def bind_resident_mcp_scope_verifier(
        self,
        verifier: ResidentMcpScopeVerifier,
    ) -> None:
        if (
            self._resident_scope_verifier is not None
            and self._resident_scope_verifier is not verifier
        ):
            raise HarnessAdmissionError("mcp_scope_verifier_already_bound")
        self._resident_scope_verifier = verifier

    def issue_resident_mcp_channel(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        root_session_ref: str,
        fence_ref: str,
        capability_binding_hash: str,
        operation_ids: tuple[str, ...],
    ) -> ResidentMcpChannel:
        """Issue a scoped channel after the fixed Harness contract is ready.

        This method does not admit or invent a Run, Attempt, Session, or Fence;
        the caller must supply the exact identities already admitted by the
        owning runtime.  The bearer credential is intentionally ephemeral and
        is never placed in a runtime binding or durable provider spool.
        """

        self.require_full_conformance_binding()
        verifier = self._resident_scope_verifier
        if verifier is None:
            raise HarnessAdmissionError("mcp_scope_verifier_unavailable")
        try:
            verifier.verify_bundle_runtime_scope(
                run_ref=run_ref,
                attempt_ref=attempt_ref,
                root_session_ref=root_session_ref,
                fence_ref=fence_ref,
                runtime_binding_hash=capability_binding_hash,
            )
        except Exception as error:
            code = getattr(error, "code", "mcp_channel_scope_invalid")
            raise HarnessAdmissionError(str(code)) from error
        try:
            connection, binding = self._gateway.issue_channel(
                run_ref=run_ref,
                attempt_ref=attempt_ref,
                root_session_ref=root_session_ref,
                fence_ref=fence_ref,
                capability_binding_hash=capability_binding_hash,
                operation_ids=operation_ids,
            )
        except SemanticMcpError as error:
            raise HarnessAdmissionError(error.code) from error
        self._resident_channel_scopes[_token_hash(connection.token)] = (
            _ResidentMcpScope(
                run_ref=run_ref,
                attempt_ref=attempt_ref,
                root_session_ref=root_session_ref,
                fence_ref=fence_ref,
                capability_binding_hash=capability_binding_hash,
            )
        )
        return ResidentMcpChannel(connection=connection, binding=binding)

    def revoke_resident_mcp_channel(self, channel: ResidentMcpChannel) -> None:
        """Revoke the ephemeral credential after one provider operation."""

        if not isinstance(channel, ResidentMcpChannel):
            raise HarnessAdmissionError("mcp_channel_scope_invalid")
        self._resident_channel_scopes.pop(_token_hash(channel.connection.token), None)
        self._gateway.revoke_channel(channel.connection.token)

    def start_full_conformance(
        self, request: FullConformanceRequest
    ) -> FullConformanceRunSet:
        """Admit one fixed full-contract Run for each production Adapter."""

        token = secrets.token_hex(8)
        conformance_ref = f"hfc_{token}"
        probe_requests: list[HarnessProbeRequest] = []
        for family in FULL_CONFORMANCE_FAMILIES:
            model_ref, auth_profile_ref = request.provider_configuration(family)
            request_ref = self._full_conformance_request_ref(token, family)
            probe_request = HarnessProbeRequest(
                request_ref=request_ref,
                harness_family=family,
                model_ref=model_ref,
                auth_profile_ref=auth_profile_ref,
                required_operation_ids=FULL_CONFORMANCE_OPERATION_IDS,
                required_capabilities=HARNESS_CAPABILITIES,
            )
            self._validate_request(probe_request, request_ref)
            self._capability_binding_hash(probe_request)
            probe_requests.append(probe_request)
        runs: list[HarnessProbeRun] = []
        for probe_request in probe_requests:
            admission = self.admit_probe(
                probe_request,
                idempotency_key=probe_request.request_ref,
            )
            runs.append(admission.run)
        return FullConformanceRunSet(
            conformance_ref=conformance_ref,
            contract_ref=FULL_CONFORMANCE_V1,
            contract_hash=self._full_conformance_contract_hash,
            runs=tuple(runs),
        )

    def advance_full_conformance(self, *, mcp_base_url: str) -> bool:
        """Advance one durable turn of the newest fixed conformance set."""

        if not _loopback_http_url(mcp_base_url):
            raise HarnessAdmissionError("harness_probe_execution_invalid")
        current_ref, runs, operations = self._current_full_conformance()
        if current_ref is None:
            return False
        for family in FULL_CONFORMANCE_FAMILIES:
            run = runs.get(family)
            if run is None or run.status == "failed":
                continue
            operation = operations.get(run.run_ref)
            if run.status == "admitted":
                self._execute_probe_turn(
                    run.request_ref,
                    prompt=_FULL_CONFORMANCE_INITIAL_PROMPT,
                    mcp_base_url=mcp_base_url,
                    resume=False,
                    required_capabilities=(
                        _FULL_CONFORMANCE_INITIAL_CAPABILITIES
                    ),
                )
                return True
            if run.status == "running":
                if operation is None or operation.status != "unknown_outcome":
                    return False
                generation = operation.generation
                if generation not in {1, 2}:
                    raise HarnessAdmissionError(
                        "full_conformance_generation_invalid"
                    )
                self._reconcile_full_conformance_turn(
                    run.request_ref,
                    prompt=(
                        _FULL_CONFORMANCE_INITIAL_PROMPT
                        if generation == 1
                        else _FULL_CONFORMANCE_RESUME_PROMPT
                    ),
                    mcp_base_url=mcp_base_url,
                    required_capabilities=(
                        _FULL_CONFORMANCE_INITIAL_CAPABILITIES
                        if generation == 1
                        else HARNESS_CAPABILITIES
                    ),
                )
                return True
            if run.status != "executed":
                continue
            profile = self._profile_for_run(run.run_ref)
            if self._full_profile_is_ready(run, operation, profile):
                continue
            if operation is None or operation.generation != 1:
                return False
            self._execute_probe_turn(
                run.request_ref,
                prompt=_FULL_CONFORMANCE_RESUME_PROMPT,
                mcp_base_url=mcp_base_url,
                resume=True,
                required_capabilities=HARNESS_CAPABILITIES,
            )
            return True
        return False

    def admit_probe(
        self, request: HarnessProbeRequest, *, idempotency_key: str
    ) -> HarnessAdmission:
        return self._admit_request(
            request,
            idempotency_key=idempotency_key,
            authoritative_run_ref=None,
        )

    def admit_target_run(
        self,
        *,
        target_ref: str,
        target_run_ref: str,
        harness_family: HarnessFamily,
        model_ref: str,
        auth_profile_ref: str,
        target_scope_binding: dict[str, object],
    ) -> HarnessAdmission:
        """Admit an independent TargetRun root under the fixed Harness contract.

        Agent Runtime already owns ``target_run_ref``.  Harness consumes that
        exact identity and only creates its Session/Attempt/Fence children.
        """

        full_conformance = self.require_full_conformance_binding()
        target_scope_binding_hash = canonical_hash(target_scope_binding)
        request_material = {
            "target_ref": target_ref,
            "target_run_ref": target_run_ref,
            "harness_family": harness_family,
            "model_ref": model_ref,
            "auth_profile_ref": auth_profile_ref,
            "required_operation_ids": list(TARGET_RUN_SEMANTIC_OPERATION_IDS),
            "required_capabilities": list(HARNESS_CAPABILITIES),
            "full_conformance_binding_hash": full_conformance.binding_hash,
            "target_scope_binding_hash": target_scope_binding_hash,
        }
        request_ref = f"trh1:{canonical_hash(request_material)}"
        request = TargetHarnessRequest(
            request_ref=request_ref,
            harness_family=harness_family,
            model_ref=model_ref,
            auth_profile_ref=auth_profile_ref,
            required_operation_ids=TARGET_RUN_SEMANTIC_OPERATION_IDS,
            required_capabilities=HARNESS_CAPABILITIES,
            target_ref=target_ref,
            target_run_ref=target_run_ref,
            full_conformance_binding=full_conformance.as_dict(),
            full_conformance_binding_hash=full_conformance.binding_hash,
            target_scope_binding_hash=target_scope_binding_hash,
        )
        return self._admit_request(
            request,
            idempotency_key=request_ref,
            authoritative_run_ref=target_run_ref,
        )

    def admit_target_run_from_current_conformance(
        self,
        *,
        target_ref: str,
        target_run_ref: str,
        harness_family: HarnessFamily,
        target_scope_binding: dict[str, object],
    ) -> HarnessAdmission:
        """Admit a Target root with the exact current conformance profile.

        Model and authentication bindings are reread from the current complete
        conformance run for the requested family.  The Target runtime therefore
        cannot guess credentials or silently fall back to a fixture profile.
        """

        self.require_full_conformance_binding()
        _conformance_ref, runs, _operations = self._current_full_conformance()
        conformance_run = runs.get(harness_family)
        if conformance_run is None:
            raise HarnessAdmissionError(
                "bundle_harness_full_conformance_unavailable"
            )
        return self.admit_target_run(
            target_ref=target_ref,
            target_run_ref=target_run_ref,
            harness_family=harness_family,
            model_ref=conformance_run.model_ref,
            auth_profile_ref=conformance_run.auth_profile_ref,
            target_scope_binding=target_scope_binding,
        )

    def _admit_request(
        self,
        request: HarnessProbeRequest,
        *,
        idempotency_key: str,
        authoritative_run_ref: str | None,
    ) -> HarnessAdmission:
        self._validate_request(request, idempotency_key)
        request_hash = canonical_hash(request.as_dict())
        existing = self._admissions.get(idempotency_key)
        if existing is not None:
            if existing[0] != request_hash:
                raise HarnessAdmissionError("harness_admission_conflict")
            return existing[1]
        capability_binding_hash = self._capability_binding_hash(request)
        try:
            admission_arguments: dict[str, object] = {
                "request": request.as_dict(),
                "idempotency_key": idempotency_key,
                "request_hash": request_hash,
                "capability_binding_hash": capability_binding_hash,
            }
            if authoritative_run_ref is not None:
                admission_arguments["authoritative_run_ref"] = (
                    authoritative_run_ref
                )
            reserved = self._owner.reserve_admission(**admission_arguments)
        except AgentRuntimeHarnessError as error:
            raise HarnessAdmissionError(error.code) from error
        try:
            connection, mcp_binding = self._gateway.issue_channel(
                run_ref=reserved.run_ref,
                attempt_ref=reserved.attempt_ref,
                root_session_ref=reserved.root_session_ref,
                fence_ref=reserved.fence_ref,
                capability_binding_hash=capability_binding_hash,
                operation_ids=request.required_operation_ids,
            )
        except SemanticMcpError as error:
            retry = self._owner.fail_admission(reserved.run_ref, error.code)
            raise HarnessAdmissionError(
                error.code,
                next_retry_at=(
                    None if retry is None else retry.next_retry_at
                ),
            ) from error
        provisional_run = _probe_run_from_owner(reserved, mcp_binding)
        scope = _channel_scope(
            provisional_run, request.required_operation_ids
        )
        try:
            activated = self._owner.activate_admission(
                run_ref=reserved.run_ref,
                mcp_binding=mcp_binding.as_dict(),
                grant_ref=connection.grant_ref,
                server_instance_ref=mcp_binding.server_instance_ref,
                token_hash=_token_hash(connection.token),
                scope=scope,
            )
        except AgentRuntimeHarnessError as error:
            self._gateway.revoke_channel(connection.token)
            retry = self._owner.fail_admission(reserved.run_ref, error.code)
            raise HarnessAdmissionError(
                error.code,
                next_retry_at=(
                    None if retry is None else retry.next_retry_at
                ),
            ) from error
        admission = HarnessAdmission(
            run=_probe_run_from_owner(activated, mcp_binding),
            connection=connection,
        )
        self._admissions[idempotency_key] = (request_hash, admission)
        self._admissions_by_request[request.request_ref] = admission
        return admission

    def resume_target_run(self, target_run_ref: str) -> HarnessAdmission:
        """Resume only a Harness Run backed by a canonical Target admission."""

        try:
            record = self._owner.query_target_run_by_ref(target_run_ref)
        except AgentRuntimeHarnessError as error:
            raise HarnessAdmissionError(error.code) from error
        if record is None:
            raise HarnessAdmissionError("target_harness_run_not_found")
        admission = self.resume_probe(record.request_ref)
        request = self._request_for_run(target_run_ref)
        if not isinstance(request, TargetHarnessRequest):
            raise HarnessAdmissionError("target_harness_admission_integrity_invalid")
        return admission

    def reserve_target_review_session(
        self,
        target_run_ref: str,
        *,
        review_kind: Literal["code", "result"],
    ) -> AgentRuntimeTargetChildSession:
        """Allocate one domain child Session for the next real review turn."""

        try:
            return self._owner.reserve_target_child_session(
                target_run_ref=target_run_ref,
                review_kind=review_kind,
            )
        except AgentRuntimeHarnessError as error:
            raise HarnessAdmissionError(error.code) from error

    def resume_probe(self, request_ref: str) -> HarnessAdmission:
        if not request_ref or len(request_ref) > 96:
            raise HarnessAdmissionError("harness_probe_request_invalid")
        try:
            record = self._owner.query_run(request_ref)
        except AgentRuntimeHarnessError as error:
            raise HarnessAdmissionError(error.code) from error
        if record is None:
            raise HarnessAdmissionError("harness_run_not_found")
        if record.status not in {
            "admitting",
            "admitted",
            "running",
            "executed",
        }:
            raise HarnessAdmissionError("harness_run_not_resumable")
        request = self._request_from_value(
            record.request, record.idempotency_key, record
        )
        previous = self._admissions_by_request.get(request_ref)
        try:
            mcp_connection, mcp_binding = self._gateway.issue_channel(
                run_ref=record.run_ref,
                attempt_ref=record.attempt_ref,
                root_session_ref=record.root_session_ref,
                fence_ref=record.fence_ref,
                capability_binding_hash=record.capability_binding_hash,
                operation_ids=request.required_operation_ids,
            )
        except SemanticMcpError as error:
            raise HarnessAdmissionError(error.code) from error
        run = _probe_run_from_owner(record, mcp_binding)
        try:
            if record.status == "admitting":
                record = self._owner.activate_admission(
                    run_ref=run.run_ref,
                    mcp_binding=mcp_binding.as_dict(),
                    grant_ref=mcp_connection.grant_ref,
                    server_instance_ref=mcp_binding.server_instance_ref,
                    token_hash=_token_hash(mcp_connection.token),
                    scope=_channel_scope(run, request.required_operation_ids),
                )
                run = _probe_run_from_owner(record, mcp_binding)
            else:
                record = self._owner.replace_channel(
                    run_ref=run.run_ref,
                    mcp_binding=mcp_binding.as_dict(),
                    grant_ref=mcp_connection.grant_ref,
                    server_instance_ref=mcp_binding.server_instance_ref,
                    token_hash=_token_hash(mcp_connection.token),
                    scope=_channel_scope(run, request.required_operation_ids),
                )
                run = _probe_run_from_owner(record, mcp_binding)
        except AgentRuntimeHarnessError as error:
            self._gateway.revoke_channel(mcp_connection.token)
            raise HarnessAdmissionError(error.code) from error
        admission = HarnessAdmission(run=run, connection=mcp_connection)
        if previous is not None:
            self._gateway.revoke_channel(previous.connection.token)
        self._admissions[record.idempotency_key] = (
            record.request_hash,
            admission,
        )
        self._admissions_by_request[request_ref] = admission
        return admission

    def execute_probe(
        self,
        request_ref: str,
        *,
        prompt: str,
        mcp_base_url: str,
    ) -> HarnessProbeRun:
        return self._execute_probe_turn(
            request_ref,
            prompt=prompt,
            mcp_base_url=mcp_base_url,
            resume=False,
        )

    def execute_target_turn(
        self,
        request_ref: str,
        *,
        phase: TargetTurnPhase,
        prompt: str,
        mcp_base_url: str,
    ) -> HarnessProbeRun:
        self._require_target_request(request_ref)
        return self._execute_probe_turn(
            request_ref,
            prompt=prompt,
            mcp_base_url=mcp_base_url,
            resume=False,
            required_capabilities=_target_turn_capabilities(phase, resume=False),
        )

    def run_or_resume_target_root(
        self,
        request_ref: str,
        *,
        prompt: str,
        mcp_base_url: str,
    ) -> HarnessProbeRun:
        """Advance the one long-lived Target root Session.

        The caller supplies work, not lifecycle mechanics.  Harness chooses an
        initial turn, a native Session resume, or reconciliation of the exact
        uncertain provider operation from durable Agent Runtime state.
        """

        self._require_target_request(request_ref)
        try:
            record = self._owner.query_run(request_ref)
            operation = (
                None
                if record is None or record.status != "running"
                else self._owner.latest_operation(record.run_ref)
            )
        except AgentRuntimeHarnessError as error:
            raise HarnessAdmissionError(error.code) from error
        if record is None:
            raise HarnessAdmissionError("target_harness_run_not_found")

        if record.status == "running":
            if operation is None or operation.status != "unknown_outcome":
                raise HarnessAdmissionError("target_root_lifecycle_unavailable")
            return self._reconcile_probe_turn(
                request_ref,
                prompt=prompt,
                mcp_base_url=mcp_base_url,
                required_capabilities=_target_root_lifecycle_capabilities(
                    resume=record.native_session_ref is not None
                ),
            )

        if record.status in {"admitting", "admitted"}:
            resume = False
        elif record.status == "executed":
            resume = True
        else:
            raise HarnessAdmissionError("target_root_lifecycle_unavailable")

        required_capabilities = _target_root_lifecycle_capabilities(
            resume=resume
        )
        try:
            return self._execute_probe_turn(
                request_ref,
                prompt=prompt,
                mcp_base_url=mcp_base_url,
                resume=resume,
                required_capabilities=required_capabilities,
            )
        except HarnessAdmissionError as error:
            if error.code != "provider_outcome_unknown":
                raise
        return self._reconcile_probe_turn(
            request_ref,
            prompt=prompt,
            mcp_base_url=mcp_base_url,
            required_capabilities=required_capabilities,
        )

    def recover_failed_target_root(self, request_ref: str) -> HarnessAdmission:
        """Reopen one deterministic Target root failure and refresh its channel.

        Agent Runtime owns every safety and idempotency decision.  Harness only
        restores the ephemeral resident channel for the exact same
        Run/Session/Attempt/Fence so the next daemon tick can start a fresh or
        native-resume turn as selected by the persisted native Session fact.
        """

        self._require_target_request(request_ref)
        try:
            recovery = self._owner.reopen_failed_target_root(request_ref)
        except AgentRuntimeHarnessRetryLater as error:
            raise HarnessAdmissionError(
                error.code,
                next_retry_at=error.retry.next_retry_at,
            ) from error
        except AgentRuntimeHarnessError as error:
            raise HarnessAdmissionError(error.code) from error
        cached = self._admissions_by_request.get(request_ref)
        if (
            not recovery.reopened
            and self._recovered_target_requests.get(request_ref)
            == recovery.operation_generation
            and cached is not None
            and _same_harness_identity(cached.run, recovery.run)
            and cached.run.status == recovery.run.status
        ):
            return cached
        try:
            admission = self.resume_probe(request_ref)
        except HarnessAdmissionError as error:
            # The Owner has already revoked the durable grant and reopened the
            # run.  Never let a stale in-memory admission bypass the next
            # channel retry merely because the run is no longer ``failed``.
            self._drop_cached_target_admission(request_ref)
            raise HarnessAdmissionError(
                error.code,
                next_retry_at=(
                    error.next_retry_at
                    if error.next_retry_at is not None
                    else getattr(recovery, "next_retry_at", None)
                ),
            ) from error
        self._recovered_target_requests[request_ref] = (
            recovery.operation_generation
        )
        return admission

    def _drop_cached_target_admission(self, request_ref: str) -> None:
        previous = self._admissions_by_request.pop(request_ref, None)
        for idempotency_key, (_request_hash, admission) in tuple(
            self._admissions.items()
        ):
            if admission.run.request_ref == request_ref:
                self._admissions.pop(idempotency_key, None)
        self._recovered_target_requests.pop(request_ref, None)
        if previous is not None:
            self._gateway.revoke_channel(previous.connection.token)

    def cancel_target_root(self, request_ref: str) -> bool:
        """Stop only the current durable provider flight for one Target root."""

        self._require_target_request(request_ref)
        try:
            record = self._owner.query_run(request_ref)
            operation = (
                None if record is None else self._owner.latest_operation(record.run_ref)
            )
        except AgentRuntimeHarnessError as error:
            raise HarnessAdmissionError(error.code) from error
        if record is None:
            raise HarnessAdmissionError("target_harness_run_not_found")
        if operation is None or operation.status in {"executed", "failed"}:
            return True
        if record.status != "running" or operation.status not in {
            "running",
            "unknown_outcome",
        }:
            raise HarnessAdmissionError("target_root_cancel_state_invalid")
        canceller = self._operation_canceller
        if canceller is None:
            raise HarnessAdmissionError("target_root_cancel_unavailable")
        try:
            cancelled = canceller.cancel_operation(operation.invocation_hash)
        except Exception as error:
            code = getattr(error, "code", "target_root_cancel_unavailable")
            raise HarnessAdmissionError(str(code)) from error
        if type(cancelled) is not bool:
            raise HarnessAdmissionError("target_root_cancel_invalid")
        return cancelled

    def query_target_root_completion_evidence(
        self, target_ref: str
    ) -> TargetRootCompletionEvidence | None:
        """Return Owner-verified final root evidence without exposing profiles."""

        try:
            return self._owner.query_target_root_completion_evidence(target_ref)
        except AgentRuntimeHarnessError as error:
            raise HarnessAdmissionError(error.code) from error

    def verify_target_root_completion_evidence(
        self,
        *,
        handle: TargetWorkHandle,
        evidence: TargetRootCompletionEvidence,
        handoff: TargetCompletionHandoff,
    ) -> str:
        """Issuer-reverify one final envelope against the durable event ledger."""

        try:
            return self._owner.verify_target_root_completion_evidence(
                handle=handle,
                evidence=evidence,
                handoff=handoff,
            )
        except AgentRuntimeHarnessError as error:
            raise HarnessAdmissionError(error.code) from error

    def query_target_root_observations(
        self,
        target_ref: str,
        *,
        after_cursor: str | None = None,
        limit: int = 128,
    ) -> TargetRootObservationPage:
        """Return the Owner-redacted Target root stream for observers."""

        try:
            return self._owner.query_target_root_observations(
                target_ref,
                after_cursor=after_cursor,
                limit=limit,
            )
        except AgentRuntimeHarnessError as error:
            raise HarnessAdmissionError(error.code) from error

    def resume_probe_turn(
        self,
        request_ref: str,
        *,
        prompt: str,
        mcp_base_url: str,
    ) -> HarnessProbeRun:
        return self._execute_probe_turn(
            request_ref,
            prompt=prompt,
            mcp_base_url=mcp_base_url,
            resume=True,
        )

    def resume_target_turn(
        self,
        request_ref: str,
        *,
        phase: TargetTurnPhase,
        prompt: str,
        mcp_base_url: str,
    ) -> HarnessProbeRun:
        self._require_target_request(request_ref)
        return self._execute_probe_turn(
            request_ref,
            prompt=prompt,
            mcp_base_url=mcp_base_url,
            resume=True,
            required_capabilities=_target_turn_capabilities(phase, resume=True),
        )

    def reconcile_target_turn(
        self,
        request_ref: str,
        *,
        phase: TargetTurnPhase,
        prompt: str,
        mcp_base_url: str,
    ) -> HarnessProbeRun:
        self._require_target_request(request_ref)
        return self._reconcile_probe_turn(
            request_ref,
            prompt=prompt,
            mcp_base_url=mcp_base_url,
            required_capabilities=_target_turn_capabilities(
                phase,
                resume=self._operation_requires_resume(request_ref),
            ),
        )

    def _require_target_request(self, request_ref: str) -> TargetHarnessRequest:
        try:
            record = self._owner.query_run(request_ref)
        except AgentRuntimeHarnessError as error:
            raise HarnessAdmissionError(error.code) from error
        if record is None:
            raise HarnessAdmissionError("target_harness_run_not_found")
        request = self._request_from_value(
            record.request,
            record.idempotency_key,
            record,
        )
        if not isinstance(request, TargetHarnessRequest):
            raise HarnessAdmissionError("target_harness_request_required")
        return request

    def _target_workspace_for(
        self,
        admission: HarnessAdmission,
        request: HarnessProbeRequest,
    ) -> tuple[str | None, Path | None]:
        if not isinstance(request, TargetHarnessRequest):
            return None, None
        resolver = self._target_workspace_resolver
        if resolver is None:
            raise HarnessAdmissionError("target_run_workspace_unavailable")
        try:
            workspace_ref, path = resolver.resolve_target_workspace(
                target_ref=request.target_ref,
                target_run_ref=admission.run.run_ref,
                root_session_ref=admission.run.root_session_ref,
                attempt_ref=admission.run.attempt_ref,
                fence_ref=admission.run.fence_ref,
            )
        except Exception as error:
            raise HarnessAdmissionError("target_run_workspace_unavailable") from error
        if (
            not isinstance(workspace_ref, str)
            or not workspace_ref
            or not isinstance(path, Path)
            or not path.is_absolute()
            or not path.is_dir()
            or path.is_symlink()
        ):
            raise HarnessAdmissionError("target_run_workspace_unavailable")
        return workspace_ref, path

    def _operation_requires_resume(self, request_ref: str) -> bool:
        try:
            record = self._owner.query_run(request_ref)
            operation = (
                None if record is None else self._owner.latest_operation(record.run_ref)
            )
        except AgentRuntimeHarnessError as error:
            raise HarnessAdmissionError(error.code) from error
        if operation is None or operation.status != "unknown_outcome":
            raise HarnessAdmissionError("provider_reconciliation_not_required")
        if record is None:
            raise HarnessAdmissionError("harness_run_not_found")
        return record.native_session_ref is not None

    def _execute_probe_turn(
        self,
        request_ref: str,
        *,
        prompt: str,
        mcp_base_url: str,
        resume: bool,
        required_capabilities: tuple[str, ...] | None = None,
    ) -> HarnessProbeRun:
        if (
            not prompt
            or len(prompt.encode("utf-8")) > 256_000
            or not _loopback_http_url(mcp_base_url)
        ):
            raise HarnessAdmissionError("harness_probe_execution_invalid")
        admission = self._admissions_by_request.get(request_ref)
        if admission is None:
            admission = self.resume_probe(request_ref)
        if resume:
            if (
                admission.run.status != "executed"
                or admission.run.native_session_ref is None
            ):
                raise HarnessAdmissionError("native_session_resume_unavailable")
        elif (
            admission.run.status != "admitted"
            or admission.run.native_session_ref is not None
        ):
            raise HarnessAdmissionError("harness_initial_turn_unavailable")
        request = self._request_for_run(admission.run.run_ref)
        generation = self._next_operation_generation(admission.run.run_ref)
        if generation == 1 and resume:
            raise HarnessAdmissionError("harness_turn_generation_invalid")
        operation_ref = provider_operation_ref(
            admission.run.run_ref, "harness_turn", generation
        )
        workspace_ref, working_directory = self._target_workspace_for(
            admission, request
        )
        invocation_material = _turn_invocation_material(
            admission,
            request,
            prompt=prompt,
            mcp_base_url=mcp_base_url,
            operation_ref=operation_ref,
            generation=generation,
            resume=resume,
            workspace_ref=workspace_ref,
        )
        invocation_hash = canonical_hash(invocation_material)
        self._start_operation(
            admission.run,
            operation_ref,
            generation=generation,
            invocation_hash=invocation_hash,
            resume=resume,
        )
        return self._invoke_provider_turn(
            admission,
            request,
            prompt=prompt,
            mcp_base_url=mcp_base_url,
            operation_ref=operation_ref,
            resume=resume,
            required_capabilities=required_capabilities,
            workspace_ref=workspace_ref,
            working_directory=working_directory,
        )

    def reconcile_probe_turn(
        self,
        request_ref: str,
        *,
        prompt: str,
        mcp_base_url: str,
    ) -> HarnessProbeRun:
        return self._reconcile_probe_turn(
            request_ref,
            prompt=prompt,
            mcp_base_url=mcp_base_url,
            required_capabilities=None,
        )

    def _reconcile_full_conformance_turn(
        self,
        request_ref: str,
        *,
        prompt: str,
        mcp_base_url: str,
        required_capabilities: tuple[str, ...],
    ) -> HarnessProbeRun:
        return self._reconcile_probe_turn(
            request_ref,
            prompt=prompt,
            mcp_base_url=mcp_base_url,
            required_capabilities=required_capabilities,
        )

    def _reconcile_probe_turn(
        self,
        request_ref: str,
        *,
        prompt: str,
        mcp_base_url: str,
        required_capabilities: tuple[str, ...] | None,
    ) -> HarnessProbeRun:
        if (
            not prompt
            or len(prompt.encode("utf-8")) > 256_000
            or not _loopback_http_url(mcp_base_url)
        ):
            raise HarnessAdmissionError("harness_probe_execution_invalid")
        admission = self._admissions_by_request.get(request_ref)
        if admission is None:
            admission = self.resume_probe(request_ref)
        request = self._request_for_run(admission.run.run_ref)
        try:
            operation = self._owner.latest_operation(admission.run.run_ref)
        except AgentRuntimeHarnessError as error:
            raise HarnessAdmissionError(error.code) from error
        if operation is None or operation.status != "unknown_outcome":
            raise HarnessAdmissionError("provider_reconciliation_not_required")
        generation = operation.generation
        resume = admission.run.native_session_ref is not None
        if resume and admission.run.native_session_ref is None:
            raise HarnessAdmissionError("native_session_resume_unavailable")
        operation_ref = operation.operation_ref
        workspace_ref, working_directory = self._target_workspace_for(
            admission, request
        )
        invocation_hash = canonical_hash(
            _turn_invocation_material(
                admission,
                request,
                prompt=prompt,
                mcp_base_url=mcp_base_url,
                operation_ref=operation_ref,
                generation=generation,
                resume=resume,
                workspace_ref=workspace_ref,
            )
        )
        if invocation_hash != operation.invocation_hash:
            raise HarnessAdmissionError("harness_operation_conflict")
        self._begin_operation_reconciliation(operation_ref)
        return self._invoke_provider_turn(
            admission,
            request,
            prompt=prompt,
            mcp_base_url=mcp_base_url,
            operation_ref=operation_ref,
            resume=resume,
            reconciling=True,
            required_capabilities=required_capabilities,
            workspace_ref=workspace_ref,
            working_directory=working_directory,
        )

    def _invoke_provider_turn(
        self,
        admission: HarnessAdmission,
        request: HarnessProbeRequest,
        *,
        prompt: str,
        mcp_base_url: str,
        operation_ref: str,
        resume: bool,
        reconciling: bool = False,
        required_capabilities: tuple[str, ...] | None = None,
        workspace_ref: str | None = None,
        working_directory: Path | None = None,
    ) -> HarnessProbeRun:
        adapter = self._adapters[request.harness_family]
        try:
            result = adapter.invoke(
                HarnessInvocation(
                    harness_family=request.harness_family,
                    provider_operation_ref=operation_ref,
                    run_ref=admission.run.run_ref,
                    attempt_ref=admission.run.attempt_ref,
                    attempt_generation=admission.run.attempt_generation,
                    root_session_ref=admission.run.root_session_ref,
                    fence_ref=admission.run.fence_ref,
                    model_ref=request.model_ref,
                    prompt=prompt,
                    mcp_url=(
                        mcp_base_url.rstrip("/")
                        + admission.run.mcp_binding.endpoint_ref
                    ),
                    mcp_token=admission.connection.token,
                    native_session_ref=admission.run.native_session_ref,
                    target_workspace_ref=workspace_ref,
                    working_directory=(
                        None
                        if working_directory is None
                        else str(working_directory)
                    ),
                )
            )
        except HarnessAdapterUnavailable as error:
            code = error.code
            if reconciling and code in {
                "provider_unavailable",
                "provider_version_unavailable",
                "provider_version_drift",
                "provider_timeout",
                "provider_io_unavailable",
                "provider_outcome_unknown",
            }:
                code = "provider_outcome_unknown"
            retry = self._record_operation_failure(operation_ref, code)
            raise HarnessAdmissionError(
                code,
                next_retry_at=(
                    None if retry is None else retry.next_retry_at
                ),
            ) from error
        subagent_evidence = [
            {**item, "provider_operation_ref": operation_ref}
            for item in result.profile.get("subagent_evidence", [])
            if isinstance(item, dict)
        ]
        if isinstance(request, TargetHarnessRequest):
            try:
                reservation = self._owner.query_target_child_session(
                    operation_ref
                )
            except AgentRuntimeHarnessError as error:
                retry = self._record_operation_failure(
                    operation_ref, error.code
                )
                raise HarnessAdmissionError(
                    error.code,
                    next_retry_at=(
                        None if retry is None else retry.next_retry_at
                    ),
                ) from error
            if reservation is not None:
                normalized: list[dict[str, object]] = []
                for evidence in subagent_evidence:
                    payload = evidence.get("payload")
                    review = (
                        payload.get("review")
                        if isinstance(payload, dict)
                        else None
                    )
                    if (
                        not isinstance(payload, dict)
                        or payload.get("review_kind")
                        != reservation.review_kind
                        or not isinstance(review, dict)
                        or not isinstance(
                            evidence.get("spawn_evidence_ref"), str
                        )
                    ):
                        normalized.append(evidence)
                        continue
                    # Native child identifiers are transport evidence, never
                    # domain Session identities.  Harness owns the exact map
                    # from this operation's reserved domain child to the
                    # observed native parent/child and spawn event.
                    normalized_review = {
                        **review,
                        "review_parent_session_ref": (
                            reservation.parent_root_session_ref
                        ),
                        "reviewer_session_ref": reservation.child_session_ref,
                        "reviewer_spawn_evidence_ref": evidence[
                            "spawn_evidence_ref"
                        ],
                    }
                    normalized_payload = {
                        **payload,
                        "review": normalized_review,
                    }
                    normalized.append(
                        {
                            **evidence,
                            "payload": normalized_payload,
                            "payload_hash": canonical_hash(
                                normalized_payload
                            ),
                        }
                    )
                subagent_evidence = normalized
        turn_profile = {
            **result.profile,
            "run_ref": admission.run.run_ref,
            "attempt_ref": admission.run.attempt_ref,
            "attempt_generation": admission.run.attempt_generation,
            "root_session_ref": admission.run.root_session_ref,
            "fence_ref": admission.run.fence_ref,
            "provider_operation_ref": operation_ref,
            "provider_operation_refs": [operation_ref],
            "subagent_evidence": subagent_evidence,
            "provider_transport_receipts": (
                []
                if result.transport_receipt is None
                else [
                    {
                        "provider_operation_ref": operation_ref,
                        **result.transport_receipt,
                    }
                ]
            ),
            "status": "executed",
        }
        previous_profile = self._profile_for_run(admission.run.run_ref)
        profile = _merge_capability_profiles(
            previous_profile,
            turn_profile,
            operation_ref=operation_ref,
            resumed=resume,
        )
        required = (
            request.required_capabilities
            if required_capabilities is None
            else required_capabilities
        )
        missing = [
            capability
            for capability in required
            if not _capability_has_evidence(profile, capability)
        ]
        if missing:
            retry = self._record_operation_failure(
                operation_ref, "required_harness_capability_unavailable"
            )
            raise HarnessAdmissionError(
                "required_harness_capability_unavailable",
                next_retry_at=(
                    None if retry is None else retry.next_retry_at
                ),
            )
        self._complete_operation(
            operation_ref=operation_ref,
            run_ref=admission.run.run_ref,
            native_session_ref=result.native_session_ref,
            profile=profile,
            evidence_events=result.evidence_events,
        )
        completed = replace(
            admission.run,
            native_session_ref=result.native_session_ref,
            status="executed",
        )
        self._admissions_by_request[request.request_ref] = HarnessAdmission(
            run=completed, connection=admission.connection
        )
        return completed

    def query_capability_profiles(self) -> list[dict[str, object]]:
        try:
            return self._owner.query_profiles()
        except AgentRuntimeHarnessError as error:
            raise HarnessAdmissionError(error.code) from error

    def query_status(self) -> dict[str, object]:
        profiles = self.query_capability_profiles()
        try:
            rows, operation_rows = self._owner.query_status_records()
        except AgentRuntimeHarnessError as error:
            raise HarnessAdmissionError(error.code) from error
        profile_by_run_ref = {
            str(profile["run_ref"]): profile
            for profile in profiles
            if isinstance(profile.get("run_ref"), str)
        }
        latest_runs: dict[str, AgentRuntimeHarnessRun] = {}
        for row in rows:
            latest_runs.setdefault(row.harness_family, row)
        latest_operations_by_run: dict[str, AgentRuntimeHarnessOperation] = {}
        for row in operation_rows:
            latest_operations_by_run.setdefault(row.run_ref, row)
        conformance_ref, conformance_runs = self._full_conformance_group(rows)
        adapters = []
        for family in FULL_CONFORMANCE_FAMILIES:
            installation = self._adapters[family].installation_profile()
            conformance_run = conformance_runs.get(family)
            diagnostic_run = conformance_run or latest_runs.get(family)
            latest_operation = (
                None
                if diagnostic_run is None
                else latest_operations_by_run.get(diagnostic_run.run_ref)
            )
            profile = (
                None
                if conformance_run is None
                else profile_by_run_ref.get(conformance_run.run_ref)
            )
            installation_ready = installation.get("status") == "ready"
            adapter_ready = (
                installation_ready
                and conformance_run is not None
                and self._full_profile_is_ready(
                    conformance_run,
                    latest_operations_by_run.get(conformance_run.run_ref),
                    profile,
                )
            )
            missing_code = self._full_conformance_missing_code(
                conformance_ref=conformance_ref,
                run=conformance_run,
                diagnostic_run=diagnostic_run,
                profile=profile,
                operation=(
                    None
                    if conformance_run is None
                    else latest_operations_by_run.get(conformance_run.run_ref)
                ),
            )
            adapters.append(
                {
                    **installation,
                    "installation_status": installation.get("status"),
                    "status": (
                        "ready" if adapter_ready else "capability_unavailable"
                    ),
                    "capability_profile": profile,
                    "provider_operation": (
                        None
                        if latest_operation is None
                        else {
                            "operation_ref": latest_operation.operation_ref,
                            "generation": latest_operation.generation,
                            "status": latest_operation.status,
                            "outcome_code": (
                                latest_operation.outcome_code
                                if latest_operation.outcome_code is not None
                                else None
                            ),
                        }
                    ),
                    "missing_reason": (
                        None
                        if adapter_ready
                        else (
                            installation.get("reason")
                            if not installation_ready
                            else {"code": missing_code}
                        )
                    ),
                }
            )
        return {
            "status": (
                "ready"
                if all(item["status"] == "ready" for item in adapters)
                else "capability_unavailable"
            ),
            "conformance": {
                "contract_ref": FULL_CONFORMANCE_V1,
                "contract_hash": self._full_conformance_contract_hash,
                "conformance_ref": conformance_ref,
                "required_families": list(FULL_CONFORMANCE_FAMILIES),
                "required_capabilities": list(HARNESS_CAPABILITIES),
                "required_operation_ids": list(
                    FULL_CONFORMANCE_OPERATION_IDS
                ),
            },
            "gateway": self._gateway.query_status(),
            "adapters": adapters,
        }

    def require_full_conformance_binding(self) -> FullConformanceBinding:
        """Return the current complete conformance evidence or fail closed.

        ``query_status`` already owns the exact readiness decision.  This
        method deliberately consumes that public deep interface and only
        packages its ready/current evidence for downstream admissions.
        Diagnostic probes and an older ready set cannot satisfy this method
        once a newer full-conformance set has been admitted.
        """

        status = self.query_status()
        conformance = status.get("conformance")
        adapters = status.get("adapters")
        gateway = status.get("gateway")
        if (
            status.get("status") != "ready"
            or not isinstance(conformance, dict)
            or conformance.get("contract_ref") != FULL_CONFORMANCE_V1
            or conformance.get("contract_hash")
            != self._full_conformance_contract_hash
            or not isinstance(conformance.get("conformance_ref"), str)
            or conformance.get("required_families")
            != list(FULL_CONFORMANCE_FAMILIES)
            or conformance.get("required_capabilities")
            != list(HARNESS_CAPABILITIES)
            or conformance.get("required_operation_ids")
            != list(FULL_CONFORMANCE_OPERATION_IDS)
            or not isinstance(adapters, list)
            or len(adapters) != len(FULL_CONFORMANCE_FAMILIES)
            or not isinstance(gateway, dict)
            or gateway.get("status") != "ready"
            or gateway.get("catalog_hash")
            != self._full_conformance_catalog_hash
        ):
            raise HarnessAdmissionError(
                "bundle_harness_full_conformance_unavailable"
            )

        profile_receipts: list[str] = []
        for family, adapter_status in zip(
            FULL_CONFORMANCE_FAMILIES, adapters, strict=True
        ):
            if (
                not isinstance(adapter_status, dict)
                or adapter_status.get("harness_family") != family
                or adapter_status.get("status") != "ready"
                or adapter_status.get("locked_version")
                != self._adapters[family].locked_version
                or adapter_status.get("provider_version")
                != self._adapters[family].locked_version
            ):
                raise HarnessAdmissionError(
                    "bundle_harness_full_conformance_unavailable"
                )
            profile = adapter_status.get("capability_profile")
            if not isinstance(profile, dict):
                raise HarnessAdmissionError(
                    "bundle_harness_full_conformance_unavailable"
                )
            run_ref = profile.get("run_ref")
            if not isinstance(run_ref, str) or not run_ref:
                raise HarnessAdmissionError(
                    "bundle_harness_full_conformance_unavailable"
                )
            profile_receipts.append(
                "harness-artifact:full-conformance-profile:"
                f"{family}:{run_ref}@sha256:{canonical_hash(profile)}"
            )

        return FullConformanceBinding(
            contract_ref=FULL_CONFORMANCE_V1,
            contract_hash=self._full_conformance_contract_hash,
            conformance_ref=str(conformance["conformance_ref"]),
            semantic_mcp_catalog_hash=self._full_conformance_catalog_hash,
            semantic_mcp_operation_bindings_hash=canonical_hash(
                list(self._full_conformance_operation_bindings)
            ),
            required_families=tuple(FULL_CONFORMANCE_FAMILIES),
            required_capabilities=tuple(HARNESS_CAPABILITIES),
            required_operation_ids=tuple(FULL_CONFORMANCE_OPERATION_IDS),
            profile_receipts=tuple(profile_receipts),
        )

    def _current_full_conformance(
        self,
    ) -> tuple[
        str | None,
        dict[HarnessFamily, AgentRuntimeHarnessRun],
        dict[str, AgentRuntimeHarnessOperation],
    ]:
        try:
            rows, operation_rows = self._owner.query_status_records()
        except AgentRuntimeHarnessError as error:
            raise HarnessAdmissionError(error.code) from error
        conformance_ref, runs = self._full_conformance_group(rows)
        operations: dict[str, AgentRuntimeHarnessOperation] = {}
        for operation in operation_rows:
            operations.setdefault(operation.run_ref, operation)
        return conformance_ref, runs, operations

    def _full_conformance_group(
        self, rows: tuple[AgentRuntimeHarnessRun, ...]
    ) -> tuple[str | None, dict[HarnessFamily, AgentRuntimeHarnessRun]]:
        groups: dict[str, dict[HarnessFamily, AgentRuntimeHarnessRun]] = {}
        newest: dict[str, float] = {}
        for row in rows:
            identity = self._full_conformance_identity(row)
            if identity is None:
                continue
            conformance_ref, family = identity
            groups.setdefault(conformance_ref, {}).setdefault(family, row)
            newest[conformance_ref] = max(
                newest.get(conformance_ref, row.created_at), row.created_at
            )
        if not newest:
            return None, {}
        current_ref = max(newest, key=lambda ref: (newest[ref], ref))
        return current_ref, groups[current_ref]

    def _full_conformance_identity(
        self, row: AgentRuntimeHarnessRun
    ) -> tuple[str, HarnessFamily] | None:
        parts = row.request_ref.split(":")
        if len(parts) != 4 or parts[0] != "hfc1":
            return None
        _prefix, contract_hash, token, family_value = parts
        if (
            contract_hash != self._full_conformance_contract_hash
            or _CONFORMANCE_TOKEN.fullmatch(token) is None
            or family_value not in FULL_CONFORMANCE_FAMILIES
            or row.idempotency_key != row.request_ref
        ):
            return None
        family: HarnessFamily = (
            "codex" if family_value == "codex" else "claude"
        )
        request = self._request_from_value(
            row.request, row.idempotency_key, row
        )
        if (
            request.harness_family != family
            or request.required_capabilities != HARNESS_CAPABILITIES
            or request.required_operation_ids
            != FULL_CONFORMANCE_OPERATION_IDS
            or row.capability_binding_hash
            != self._capability_binding_hash(request)
        ):
            return None
        return f"hfc_{token}", family

    def _full_profile_is_ready(
        self,
        run: AgentRuntimeHarnessRun,
        operation: AgentRuntimeHarnessOperation | None,
        profile: dict[str, object] | None,
    ) -> bool:
        if (
            run.status != "executed"
            or operation is None
            or operation.status != "executed"
            or operation.generation < 2
            or profile is None
            or profile.get("run_ref") != run.run_ref
            or profile.get("attempt_ref") != run.attempt_ref
            or profile.get("root_session_ref") != run.root_session_ref
            or profile.get("fence_ref") != run.fence_ref
            or profile.get("native_session_ref") != run.native_session_ref
            or profile.get("harness_family") != run.harness_family
            or profile.get("locked_version")
            != self._adapters[run.harness_family].locked_version
            or profile.get("provider_version")
            != self._adapters[run.harness_family].locked_version
            or run.mcp_binding is None
            or run.mcp_binding.get("catalog_hash")
            != self._full_conformance_catalog_hash
            or run.mcp_binding.get("operation_bindings")
            != list(self._full_conformance_operation_bindings)
        ):
            return False
        operation_refs = profile.get("provider_operation_refs")
        if (
            not isinstance(operation_refs, list)
            or len(operation_refs) < 2
            or len(operation_refs) != len(set(operation_refs))
        ):
            return False
        capabilities = profile.get("capabilities")
        if not isinstance(capabilities, dict) or set(capabilities) != set(
            HARNESS_CAPABILITIES
        ):
            return False
        return all(
            isinstance(entry := capabilities.get(capability), dict)
            and entry.get("status") == "available"
            and isinstance(entry.get("evidence_refs"), list)
            and bool(entry["evidence_refs"])
            for capability in HARNESS_CAPABILITIES
        )

    def _full_conformance_missing_code(
        self,
        *,
        conformance_ref: str | None,
        run: AgentRuntimeHarnessRun | None,
        diagnostic_run: AgentRuntimeHarnessRun | None,
        profile: dict[str, object] | None,
        operation: AgentRuntimeHarnessOperation | None,
    ) -> str:
        if run is None:
            if conformance_ref is not None:
                return "full_conformance_family_missing"
            if diagnostic_run is not None and diagnostic_run.failure_code:
                return diagnostic_run.failure_code
            return "full_conformance_not_recorded"
        if run.failure_code is not None:
            return run.failure_code
        if run.status in {"admitting", "admitted", "running"}:
            return "full_conformance_pending"
        if not self._full_profile_is_ready(run, operation, profile):
            return "full_conformance_incomplete"
        return "full_conformance_incomplete"

    def dispatch_mcp(
        self, token: str | None, message: object
    ) -> tuple[int, dict[str, object] | None]:
        if token is None:
            return self._gateway.dispatch(None, message)
        token_hash = _token_hash(token)
        try:
            current = self._owner.channel_is_current(token_hash)
        except AgentRuntimeHarnessError as error:
            raise HarnessAdmissionError(error.code) from error
        resident_scope = self._resident_channel_scopes.get(token_hash)
        if resident_scope is not None:
            verifier = self._resident_scope_verifier
            if verifier is None:
                current = False
            else:
                try:
                    verifier.verify_bundle_runtime_scope(
                        run_ref=resident_scope.run_ref,
                        attempt_ref=resident_scope.attempt_ref,
                        root_session_ref=resident_scope.root_session_ref,
                        fence_ref=resident_scope.fence_ref,
                        runtime_binding_hash=(
                            resident_scope.capability_binding_hash
                        ),
                    )
                except Exception:
                    current = False
                else:
                    current = True
            if not current:
                self._resident_channel_scopes.pop(token_hash, None)
                self._gateway.revoke_channel(token)
        if not current:
            return 401, {
                "error": {
                    "code": "mcp_channel_authentication_required",
                    "message": "A current scope-bound MCP channel is required.",
                }
            }
        return self._gateway.dispatch(token, message)

    def _full_conformance_request_ref(
        self, token: str, family: HarnessFamily
    ) -> str:
        if _CONFORMANCE_TOKEN.fullmatch(token) is None:
            raise HarnessAdmissionError("full_conformance_identity_invalid")
        request_ref = (
            f"hfc1:{self._full_conformance_contract_hash}:{token}:{family}"
        )
        if len(request_ref) > 96:
            raise HarnessAdmissionError("full_conformance_identity_invalid")
        return request_ref

    def _capability_binding_hash(
        self, request: HarnessProbeRequest
    ) -> str:
        try:
            required_operation_bindings = self._gateway.required_bindings(
                request.required_operation_ids
            )
        except SemanticMcpError as error:
            raise HarnessAdmissionError(error.code) from error
        material: dict[str, object] = {
                "harness_family": request.harness_family,
                "model_ref": request.model_ref,
                "auth_profile_ref": request.auth_profile_ref,
                "required_operation_ids": list(request.required_operation_ids),
                "required_operation_bindings": list(
                    required_operation_bindings
                ),
                "required_capabilities": list(request.required_capabilities),
        }
        if isinstance(request, TargetHarnessRequest):
            material.update(
                {
                    "target_ref": request.target_ref,
                    "target_run_ref": request.target_run_ref,
                    "full_conformance_binding_hash": (
                        request.full_conformance_binding_hash
                    ),
                    "target_scope_binding_hash": request.target_scope_binding_hash,
                }
            )
        return canonical_hash(material)

    def _validate_request(
        self, request: HarnessProbeRequest, idempotency_key: str
    ) -> None:
        operations_valid = all(
            isinstance(item, str) and 0 < len(item) <= 128
            for item in request.required_operation_ids
        )
        capabilities_valid = all(
            isinstance(item, str)
            and item in HARNESS_CAPABILITIES
            for item in request.required_capabilities
        )
        if (
            not isinstance(request.harness_family, str)
            or request.harness_family not in {"codex", "claude"}
            or not isinstance(request.request_ref, str)
            or not request.request_ref
            or len(request.request_ref) > 96
            or not isinstance(request.model_ref, str)
            or not request.model_ref
            or len(request.model_ref) > 160
            or not isinstance(request.auth_profile_ref, str)
            or not request.auth_profile_ref
            or len(request.auth_profile_ref) > 160
            or _AUTH_PROFILE_REF.fullmatch(request.auth_profile_ref) is None
            or not isinstance(idempotency_key, str)
            or not idempotency_key
            or len(idempotency_key) > 128
            or not request.required_operation_ids
            or len(request.required_operation_ids) > 64
            or not operations_valid
            or len(request.required_operation_ids)
            != len(set(request.required_operation_ids))
            or not request.required_capabilities
            or len(request.required_capabilities) > len(HARNESS_CAPABILITIES)
            or not capabilities_valid
            or len(request.required_capabilities)
            != len(set(request.required_capabilities))
        ):
            raise HarnessAdmissionError("harness_probe_request_invalid")
        if isinstance(request, TargetHarnessRequest) and (
            not request.target_ref
            or len(request.target_ref) > 96
            or not request.target_run_ref
            or len(request.target_run_ref) > 96
            or canonical_hash(request.full_conformance_binding)
            != request.full_conformance_binding_hash
            or len(request.full_conformance_binding_hash) != 64
            or len(request.target_scope_binding_hash) != 64
            or request.required_operation_ids != TARGET_RUN_SEMANTIC_OPERATION_IDS
            or request.required_capabilities != HARNESS_CAPABILITIES
        ):
            raise HarnessAdmissionError("target_harness_request_invalid")

    def _begin_operation_reconciliation(self, operation_ref: str) -> None:
        try:
            self._owner.begin_reconciliation(operation_ref)
        except AgentRuntimeHarnessError as error:
            raise HarnessAdmissionError(error.code) from error

    def _next_operation_generation(self, run_ref: str) -> int:
        try:
            return self._owner.next_operation_generation(run_ref)
        except AgentRuntimeHarnessError as error:
            raise HarnessAdmissionError(error.code) from error

    def _profile_for_run(self, run_ref: str) -> dict[str, object] | None:
        try:
            return self._owner.query_profile(run_ref)
        except AgentRuntimeHarnessError as error:
            raise HarnessAdmissionError(error.code) from error

    def _start_operation(
        self,
        run: HarnessProbeRun,
        operation_ref: str,
        *,
        generation: int,
        invocation_hash: str,
        resume: bool,
    ) -> None:
        try:
            self._owner.start_operation(
                run_ref=run.run_ref,
                operation_ref=operation_ref,
                generation=generation,
                invocation_hash=invocation_hash,
                resume=resume,
            )
        except AgentRuntimeHarnessError as error:
            raise HarnessAdmissionError(error.code) from error

    def _record_operation_failure(
        self, operation_ref: str, code: str
    ) -> AgentRuntimeHarnessRetry | None:
        try:
            return self._owner.record_operation_failure(operation_ref, code)
        except AgentRuntimeHarnessError as error:
            raise HarnessAdmissionError(error.code) from error

    def _complete_operation(
        self,
        *,
        operation_ref: str,
        run_ref: str,
        native_session_ref: str,
        profile: dict[str, object],
        evidence_events: tuple[dict[str, object], ...],
    ) -> None:
        try:
            self._owner.complete_operation(
                operation_ref=operation_ref,
                run_ref=run_ref,
                native_session_ref=native_session_ref,
                profile=profile,
                evidence_events=evidence_events,
            )
        except AgentRuntimeHarnessError as error:
            raise HarnessAdmissionError(error.code) from error

    def _request_for_run(self, run_ref: str) -> HarnessProbeRequest:
        try:
            value = self._owner.query_request(run_ref)
            record = self._owner.query_run_by_ref(run_ref)
        except AgentRuntimeHarnessError as error:
            raise HarnessAdmissionError(error.code) from error
        if record is None:
            raise HarnessAdmissionError("harness_run_not_found")
        return self._request_from_value(value, record.idempotency_key, record)

    def _request_from_value(
        self,
        value: dict[str, object],
        idempotency_key: str,
        record: AgentRuntimeHarnessRun,
    ) -> HarnessProbeRequest:
        try:
            base = {
                "request_ref": str(value["request_ref"]),
                "harness_family": value["harness_family"],
                "model_ref": str(value["model_ref"]),
                "auth_profile_ref": str(value["auth_profile_ref"]),
                "required_operation_ids": tuple(value["required_operation_ids"]),
                "required_capabilities": tuple(value["required_capabilities"]),
            }
            target_keys = {
                "target_ref",
                "target_run_ref",
                "full_conformance_binding",
                "full_conformance_binding_hash",
                "target_scope_binding_hash",
            }
            present_target_keys = target_keys.intersection(value)
            if present_target_keys:
                if present_target_keys != target_keys:
                    raise HarnessAdmissionError("harness_request_corrupt")
                full_binding = value["full_conformance_binding"]
                if not isinstance(full_binding, dict):
                    raise HarnessAdmissionError("harness_request_corrupt")
                request: HarnessProbeRequest = TargetHarnessRequest(
                    **base,
                    target_ref=str(value["target_ref"]),
                    target_run_ref=str(value["target_run_ref"]),
                    full_conformance_binding=full_binding,
                    full_conformance_binding_hash=str(
                        value["full_conformance_binding_hash"]
                    ),
                    target_scope_binding_hash=str(
                        value["target_scope_binding_hash"]
                    ),
                )
            else:
                request = HarnessProbeRequest(**base)
        except (KeyError, TypeError) as error:
            raise HarnessAdmissionError("harness_request_corrupt") from error
        self._validate_request(request, idempotency_key)
        if (
            canonical_hash(value) != record.request_hash
            or request.request_ref != record.request_ref
            or request.harness_family != record.harness_family
            or request.model_ref != record.model_ref
            or request.auth_profile_ref != record.auth_profile_ref
            or (
                isinstance(request, TargetHarnessRequest)
                and request.target_run_ref != record.run_ref
            )
        ):
            raise HarnessAdmissionError("harness_request_corrupt")
        return request


def _merge_capability_profiles(
    previous: dict[str, object] | None,
    current: dict[str, object],
    *,
    operation_ref: str,
    resumed: bool,
) -> dict[str, object]:
    current_capabilities = current.get("capabilities")
    if not isinstance(current_capabilities, dict):
        raise HarnessAdmissionError("harness_profile_invalid")
    if previous is None:
        if resumed:
            raise HarnessAdmissionError("native_session_resume_unavailable")
        return current

    for identity_field in (
        "schema_ref",
        "harness_family",
        "locked_version",
        "provider_version",
        "native_session_ref",
        "run_ref",
        "attempt_ref",
        "attempt_generation",
        "root_session_ref",
        "fence_ref",
    ):
        if previous.get(identity_field) != current.get(identity_field):
            raise HarnessAdmissionError("harness_profile_identity_conflict")
    previous_capabilities = previous.get("capabilities")
    if not isinstance(previous_capabilities, dict):
        raise HarnessAdmissionError("harness_profile_corrupt")

    merged_capabilities: dict[str, object] = {}
    for capability in sorted(
        set(previous_capabilities) | set(current_capabilities)
    ):
        before = previous_capabilities.get(capability)
        after = current_capabilities.get(capability)
        if not isinstance(before, dict) or not isinstance(after, dict):
            raise HarnessAdmissionError("harness_profile_invalid")
        evidence_refs = list(
            dict.fromkeys(
                [
                    item
                    for entry in (before, after)
                    for item in entry.get("evidence_refs", [])
                    if isinstance(item, str)
                ]
            )
        )
        if before.get("status") == "available" or after.get("status") == "available":
            merged_capabilities[capability] = {
                "status": "available",
                "evidence_refs": evidence_refs,
            }
        else:
            reason = after.get("reason", before.get("reason"))
            merged_capabilities[capability] = {
                "status": "capability_unavailable",
                "reason": (
                    reason
                    if isinstance(reason, dict)
                    else {"code": "probe_evidence_missing"}
                ),
                "evidence_refs": evidence_refs,
            }

    if resumed:
        continuation_refs: list[str] = []
        for capability in ("native_session", "stream"):
            entry = current_capabilities.get(capability)
            if isinstance(entry, dict) and entry.get("status") == "available":
                continuation_refs.extend(
                    item
                    for item in entry.get("evidence_refs", [])
                    if isinstance(item, str)
                )
        if not continuation_refs:
            raise HarnessAdmissionError("native_session_resume_unavailable")
        merged_capabilities["resume"] = {
            "status": "available",
            "evidence_refs": list(dict.fromkeys(continuation_refs)),
        }

    previous_operation_refs = previous.get("provider_operation_refs")
    if not isinstance(previous_operation_refs, list) or not all(
        isinstance(item, str) for item in previous_operation_refs
    ):
        raise HarnessAdmissionError("harness_profile_corrupt")
    previous_transport_receipts = previous.get("provider_transport_receipts")
    current_transport_receipts = current.get("provider_transport_receipts")
    if not isinstance(previous_transport_receipts, list) or not isinstance(
        current_transport_receipts, list
    ):
        raise HarnessAdmissionError("harness_profile_corrupt")
    previous_subagent_evidence = previous.get("subagent_evidence", [])
    current_subagent_evidence = current.get("subagent_evidence", [])
    if not isinstance(previous_subagent_evidence, list) or not isinstance(
        current_subagent_evidence, list
    ):
        raise HarnessAdmissionError("harness_profile_corrupt")
    return {
        **current,
        "capabilities": merged_capabilities,
        "provider_operation_ref": operation_ref,
        "provider_operation_refs": list(
            dict.fromkeys([*previous_operation_refs, operation_ref])
        ),
        "provider_transport_receipts": [
            *previous_transport_receipts,
            *current_transport_receipts,
        ],
        "subagent_evidence": [
            *previous_subagent_evidence,
            *current_subagent_evidence,
        ],
    }


def _probe_run_from_owner(
    record: AgentRuntimeHarnessRun,
    mcp_binding: ResidentMcpBinding,
) -> HarnessProbeRun:
    if record.harness_family not in {"codex", "claude"}:
        raise HarnessAdmissionError("harness_request_corrupt")
    return HarnessProbeRun(
        request_ref=record.request_ref,
        run_ref=record.run_ref,
        attempt_ref=record.attempt_ref,
        attempt_generation=record.attempt_generation,
        root_session_ref=record.root_session_ref,
        native_session_ref=record.native_session_ref,
        fence_ref=record.fence_ref,
        harness_family=record.harness_family,
        model_ref=record.model_ref,
        auth_profile_ref=record.auth_profile_ref,
        capability_binding_hash=record.capability_binding_hash,
        mcp_binding=mcp_binding,
        status=record.status,
    )


def _same_harness_identity(
    projected: HarnessProbeRun,
    owner: AgentRuntimeHarnessRun,
) -> bool:
    return (
        projected.request_ref,
        projected.run_ref,
        projected.attempt_ref,
        projected.attempt_generation,
        projected.root_session_ref,
        projected.native_session_ref,
        projected.fence_ref,
    ) == (
        owner.request_ref,
        owner.run_ref,
        owner.attempt_ref,
        owner.attempt_generation,
        owner.root_session_ref,
        owner.native_session_ref,
        owner.fence_ref,
    )


def _turn_invocation_material(
    admission: HarnessAdmission,
    request: HarnessProbeRequest,
    *,
    prompt: str,
    mcp_base_url: str,
    operation_ref: str,
    generation: int,
    resume: bool,
    workspace_ref: str | None,
) -> dict[str, object]:
    return {
        "schema_ref": "meta-research/harness-invocation/v1",
        "operation_ref": operation_ref,
        "generation": generation,
        "resume": resume,
        "run_ref": admission.run.run_ref,
        "attempt_ref": admission.run.attempt_ref,
        "attempt_generation": admission.run.attempt_generation,
        "root_session_ref": admission.run.root_session_ref,
        "fence_ref": admission.run.fence_ref,
        "harness_family": request.harness_family,
        "model_ref": request.model_ref,
        "prompt_hash": canonical_hash(prompt),
        "native_session_ref": admission.run.native_session_ref,
        "mcp_url": (
            mcp_base_url.rstrip("/") + admission.run.mcp_binding.endpoint_ref
        ),
        "capability_binding_hash": admission.run.capability_binding_hash,
        "target_workspace_ref": workspace_ref,
    }


def _channel_scope(
    run: HarnessProbeRun, operation_ids: tuple[str, ...]
) -> dict[str, object]:
    return {
        "run_ref": run.run_ref,
        "attempt_ref": run.attempt_ref,
        "root_session_ref": run.root_session_ref,
        "fence_ref": run.fence_ref,
        "capability_binding_hash": run.capability_binding_hash,
        "operation_ids": list(operation_ids),
    }


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _loopback_http_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        return (
            parsed.scheme == "http"
            and parsed.path in {"", "/"}
            and not parsed.query
            and not parsed.fragment
            and host is not None
            and (host == "localhost" or ipaddress.ip_address(host).is_loopback)
        )
    except ValueError:
        return False


def _target_turn_capabilities(
    phase: TargetTurnPhase, *, resume: bool
) -> tuple[str, ...]:
    if phase == "implementation_review":
        required = (
            "native_session",
            "stream",
            "shell",
            "file_access",
            "semantic_mcp",
            "skill",
            "subagent",
        )
    elif phase == "execution":
        # Retired v1 phase compatibility only. Production Target work uses the
        # single root lifecycle and runs directly in its native Session.
        required = (
            "native_session",
            "stream",
            "semantic_mcp",
        )
    elif phase == "result_review":
        required = (
            "native_session",
            "stream",
            "semantic_mcp",
            "subagent",
        )
    else:
        raise HarnessAdmissionError("target_turn_phase_invalid")
    return (*required, "resume") if resume else required


def _target_root_lifecycle_capabilities(*, resume: bool) -> tuple[str, ...]:
    # Admission has already proved that the configured Harness exposes the
    # complete capability contract.  A root turn is free to choose which of
    # those optional facilities its work actually needs; completion therefore
    # requires only the native streamed workspace execution seam.  In
    # particular, Semantic MCP, Skills, and subagents must not become phase
    # rituals that every implementation/training iteration has to exercise.
    required = (
        "shell",
        "file_access",
        "stream",
        "native_session",
    )
    return (*required, "resume") if resume else required


def _capability_has_evidence(
    profile: dict[str, object], capability: str
) -> bool:
    capabilities = profile.get("capabilities")
    if not isinstance(capabilities, dict):
        return False
    entry = capabilities.get(capability)
    if not isinstance(entry, dict) or entry.get("status") != "available":
        return False
    evidence_refs = entry.get("evidence_refs")
    return isinstance(evidence_refs, list) and any(
        isinstance(item, str) and bool(item) for item in evidence_refs
    )
