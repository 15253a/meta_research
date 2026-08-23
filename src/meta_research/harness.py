from __future__ import annotations

import hashlib
import ipaddress
import re
from dataclasses import dataclass, replace
from typing import Literal
from urllib.parse import urlsplit

from meta_research.harness_adapters import (
    HARNESS_CAPABILITIES,
    HarnessAdapter,
    HarnessAdapterUnavailable,
    HarnessInvocation,
)
from meta_research.owners.agent_runtime_harness import (
    AgentRuntimeHarnessError,
    AgentRuntimeHarnessInterface,
    AgentRuntimeHarnessRun,
)
from meta_research.owners.common import canonical_hash
from meta_research.provider_supervisor import provider_operation_ref
from meta_research.semantic_mcp import (
    McpConnection,
    ResidentMcpBinding,
    SemanticMcpError,
    SemanticMcpGateway,
)


HarnessFamily = Literal["codex", "claude"]
_AUTH_PROFILE_REF = re.compile(r"^harness-profile:[A-Za-z0-9][A-Za-z0-9._:-]*$")


class HarnessAdmissionError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


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


class HarnessRuntime:
    """One Typed Run interface shared by every supported native Harness."""

    def __init__(
        self,
        owner: AgentRuntimeHarnessInterface,
        gateway: SemanticMcpGateway,
        adapters: tuple[HarnessAdapter, ...],
    ) -> None:
        self._owner = owner
        self._gateway = gateway
        self._adapters = {adapter.family: adapter for adapter in adapters}
        if set(self._adapters) != {"codex", "claude"}:
            raise HarnessAdmissionError("harness_adapter_catalog_invalid")
        self._admissions: dict[str, tuple[str, HarnessAdmission]] = {}
        self._admissions_by_request: dict[str, HarnessAdmission] = {}

    def admit_probe(
        self, request: HarnessProbeRequest, *, idempotency_key: str
    ) -> HarnessAdmission:
        self._validate_request(request, idempotency_key)
        request_hash = canonical_hash(request.as_dict())
        existing = self._admissions.get(idempotency_key)
        if existing is not None:
            if existing[0] != request_hash:
                raise HarnessAdmissionError("harness_admission_conflict")
            return existing[1]
        try:
            required_operation_bindings = self._gateway.required_bindings(
                request.required_operation_ids
            )
        except SemanticMcpError as error:
            raise HarnessAdmissionError(error.code) from error
        capability_binding_hash = canonical_hash(
            {
                "harness_family": request.harness_family,
                "model_ref": request.model_ref,
                "auth_profile_ref": request.auth_profile_ref,
                "required_operation_ids": list(request.required_operation_ids),
                "required_operation_bindings": list(
                    required_operation_bindings
                ),
                "required_capabilities": list(request.required_capabilities),
            }
        )
        try:
            reserved = self._owner.reserve_admission(
                request=request.as_dict(),
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                capability_binding_hash=capability_binding_hash,
            )
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
            self._owner.fail_admission(reserved.run_ref, error.code)
            raise HarnessAdmissionError(error.code) from error
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
            self._owner.fail_admission(reserved.run_ref, error.code)
            raise HarnessAdmissionError(error.code) from error
        admission = HarnessAdmission(
            run=_probe_run_from_owner(activated, mcp_binding),
            connection=connection,
        )
        self._admissions[idempotency_key] = (request_hash, admission)
        self._admissions_by_request[request.request_ref] = admission
        return admission

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
                self._owner.replace_channel(
                    run_ref=run.run_ref,
                    mcp_binding=mcp_binding.as_dict(),
                    grant_ref=mcp_connection.grant_ref,
                    server_instance_ref=mcp_binding.server_instance_ref,
                    token_hash=_token_hash(mcp_connection.token),
                    scope=_channel_scope(run, request.required_operation_ids),
                )
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

    def _execute_probe_turn(
        self,
        request_ref: str,
        *,
        prompt: str,
        mcp_base_url: str,
        resume: bool,
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
        if (generation == 1) == resume:
            raise HarnessAdmissionError("harness_turn_generation_invalid")
        operation_ref = provider_operation_ref(
            admission.run.run_ref, "harness_turn", generation
        )
        invocation_material = _turn_invocation_material(
            admission,
            request,
            prompt=prompt,
            mcp_base_url=mcp_base_url,
            operation_ref=operation_ref,
            generation=generation,
            resume=resume,
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
        )

    def reconcile_probe_turn(
        self,
        request_ref: str,
        *,
        prompt: str,
        mcp_base_url: str,
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
        resume = generation > 1
        if resume and admission.run.native_session_ref is None:
            raise HarnessAdmissionError("native_session_resume_unavailable")
        operation_ref = operation.operation_ref
        invocation_hash = canonical_hash(
            _turn_invocation_material(
                admission,
                request,
                prompt=prompt,
                mcp_base_url=mcp_base_url,
                operation_ref=operation_ref,
                generation=generation,
                resume=resume,
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
            self._record_operation_failure(operation_ref, code)
            raise HarnessAdmissionError(code) from error
        turn_profile = {
            **result.profile,
            "run_ref": admission.run.run_ref,
            "attempt_ref": admission.run.attempt_ref,
            "attempt_generation": admission.run.attempt_generation,
            "root_session_ref": admission.run.root_session_ref,
            "fence_ref": admission.run.fence_ref,
            "provider_operation_ref": operation_ref,
            "provider_operation_refs": [operation_ref],
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
        missing = [
            capability
            for capability in request.required_capabilities
            if not isinstance(profile.get("capabilities"), dict)
            or profile["capabilities"].get(capability, {}).get("status")
            != "available"
        ]
        if missing:
            self._record_operation_failure(
                operation_ref, "required_harness_capability_unavailable"
            )
            raise HarnessAdmissionError(
                "required_harness_capability_unavailable"
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
        latest = {
            str(profile["harness_family"]): profile for profile in profiles
        }
        try:
            rows, operation_rows = self._owner.query_status_records()
        except AgentRuntimeHarnessError as error:
            raise HarnessAdmissionError(error.code) from error
        latest_runs = {}
        for row in rows:
            latest_runs.setdefault(row.harness_family, row)
        latest_operations = {}
        for row in operation_rows:
            latest_operations.setdefault(row.harness_family, row)
        adapters = []
        for family in ("codex", "claude"):
            installation = self._adapters[family].installation_profile()
            latest_run = latest_runs.get(family)
            latest_operation = latest_operations.get(family)
            profile = latest.get(family)
            if (
                latest_run is not None
                and latest_operation is not None
                and latest_operation.run_ref != latest_run.run_ref
            ):
                latest_operation = None
            if (
                latest_run is not None
                and profile is not None
                and (
                    profile.get("run_ref") != latest_run.run_ref
                    or latest_run.status != "executed"
                    or latest_operation is None
                    or latest_operation.status != "executed"
                )
            ):
                profile = None
            missing_code = (
                latest_run.failure_code
                if latest_run is not None
                and latest_run.failure_code is not None
                else "conformance_probe_not_recorded"
            )
            installation_ready = installation.get("status") == "ready"
            adapter_ready = (
                installation_ready
                and profile is not None
                and latest_run is not None
                and latest_run.status == "executed"
                and latest_operation is not None
                and latest_operation.status == "executed"
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
            "gateway": self._gateway.query_status(),
            "adapters": adapters,
        }

    def dispatch_mcp(
        self, token: str | None, message: object
    ) -> tuple[int, dict[str, object] | None]:
        if token is None:
            return self._gateway.dispatch(None, message)
        try:
            current = self._owner.channel_is_current(_token_hash(token))
        except AgentRuntimeHarnessError as error:
            raise HarnessAdmissionError(error.code) from error
        if not current:
            return 401, {
                "error": {
                    "code": "mcp_channel_authentication_required",
                    "message": "A current scope-bound MCP channel is required.",
                }
            }
        return self._gateway.dispatch(token, message)

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
    ) -> None:
        try:
            self._owner.record_operation_failure(operation_ref, code)
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
            request = HarnessProbeRequest(
                request_ref=str(value["request_ref"]),
                harness_family=value["harness_family"],
                model_ref=str(value["model_ref"]),
                auth_profile_ref=str(value["auth_profile_ref"]),
                required_operation_ids=tuple(value["required_operation_ids"]),
                required_capabilities=tuple(value["required_capabilities"]),
            )
        except (KeyError, TypeError) as error:
            raise HarnessAdmissionError("harness_request_corrupt") from error
        self._validate_request(request, idempotency_key)
        if (
            canonical_hash(value) != record.request_hash
            or request.request_ref != record.request_ref
            or request.harness_family != record.harness_family
            or request.model_ref != record.model_ref
            or request.auth_profile_ref != record.auth_profile_ref
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


def _turn_invocation_material(
    admission: HarnessAdmission,
    request: HarnessProbeRequest,
    *,
    prompt: str,
    mcp_base_url: str,
    operation_ref: str,
    generation: int,
    resume: bool,
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
