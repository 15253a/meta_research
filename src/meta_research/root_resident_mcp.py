from __future__ import annotations

import os
import hashlib
from pathlib import Path
import threading
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from meta_research.human_guidance import FrozenGuidanceBinding
from meta_research.owners.common import canonical_hash
from meta_research.root_capabilities import RootAgentKind, root_operation_catalog
from meta_research.semantic_mcp import ROOT_AGENT_COMMON_OPERATION_IDS, SemanticCallContext, SemanticMcpError
from meta_research.root_workspace import RootWorkspaces, WorkspaceBinding


_OPERATION_BINDING_CONTRACT = "meta-research/harness-operation-binding/v1"
_MCP_BINDING_PREFIX = "harness-operation-binding:semantic-mcp-"
_OPERATION_BINDING_RESOURCE_PREFIX = "harness-artifact:operation-binding-"


class RootResidentMcpError(RuntimeError):
    def __init__(self, code: str, details: dict[str, object] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.details = details


class _OperationBinding(Protocol):
    contract_ref: str
    contract_hash: str
    conformance_ref: str
    semantic_mcp_catalog_hash: str
    semantic_mcp_operation_bindings_hash: str
    required_families: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    required_operation_ids: tuple[str, ...]
    profile_receipts: tuple[str, ...]

    def as_dict(self) -> dict[str, object]: ...


class _McpConnection(Protocol):
    token: str
    grant_ref: str


class _ResidentMcpBinding(Protocol):
    endpoint_ref: str
    catalog_hash: str
    connection_grant_ref: str
    operation_bindings: tuple[dict[str, object], ...]
    root_kind: RootAgentKind | None
    phase: str
    guidance_binding: FrozenGuidanceBinding | None


class _ResidentMcpChannel(Protocol):
    connection: _McpConnection
    binding: _ResidentMcpBinding


class RootResidentMcpAuthority(Protocol):
    def require_operation_binding(
        self,
        *,
        harness_family: str,
        required_operation_ids: tuple[str, ...],
        required_capabilities: tuple[str, ...],
    ) -> _OperationBinding: ...

    def issue_resident_mcp_channel(
        self,
        *,
        root_kind: RootAgentKind,
        phase: str,
        subject_policy: str,
        run_ref: str,
        attempt_ref: str,
        root_session_ref: str,
        fence_ref: str,
        capability_binding_hash: str,
        operation_ids: tuple[str, ...],
        guidance_binding: FrozenGuidanceBinding | None = None,
    ) -> _ResidentMcpChannel: ...

    def revoke_resident_mcp_channel(
        self, channel: _ResidentMcpChannel
    ) -> None: ...

    def dispatch_mcp(
        self, token: str | None, message: object
    ) -> tuple[int, dict[str, object] | None]: ...


@dataclass(frozen=True)
class RootResidentMcpRuntimeFacts:
    mcp_bindings: tuple[str, ...]
    capability_bindings: tuple[str, ...]
    resource_bindings: tuple[str, ...]


@dataclass(frozen=True)
class RootResidentMcpAccess:
    url: str
    token: str
    scope_binding_hash: str
    operation_ids: tuple[str, ...]
    workspace_binding: WorkspaceBinding | None = None
    operation_bindings: tuple[dict[str, object], ...] = ()


RootResidentMcpChannelKey = tuple[str, str, str, str]
_ChannelScope = tuple[str, str, str]


class RootResidentMcpChannels:
    """Ephemeral operation-tree channels for one Root adapter kind."""

    def __init__(self, root_kind: RootAgentKind) -> None:
        self._root_kind = root_kind
        self._operation_ids = root_operation_catalog(
            root_kind,
            common_operation_ids=ROOT_AGENT_COMMON_OPERATION_IDS,
        )
        self._workspaces: RootWorkspaces | None = None
        self._authority: RootResidentMcpAuthority | None = None
        self._base_url: str | None = None
        self._channels: dict[_ChannelScope, _ResidentMcpChannel] = {}
        self._channel_users: dict[_ChannelScope, int] = {}
        self._lock = threading.RLock()

    @property
    def operation_ids(self) -> tuple[str, ...]:
        return self._operation_ids

    @property
    def enabled(self) -> bool:
        return self._authority is not None or self._base_url is not None

    @property
    def endpoint_url(self) -> str | None:
        return None if self._base_url is None else self._base_url + "/mcp"

    def bind_workspaces(self, workspaces: RootWorkspaces) -> None:
        if self._workspaces is not None and self._workspaces is not workspaces:
            raise RootResidentMcpError("workspace_service_conflict")
        self._workspaces = workspaces

    def bind_authority(self, authority: RootResidentMcpAuthority) -> None:
        if self._authority is not None and self._authority is not authority:
            raise RootResidentMcpError("semantic_mcp_authority_conflict")
        self._authority = authority

    def configure_endpoint(self, base_url: str) -> None:
        if not isinstance(base_url, str):
            raise RootResidentMcpError("semantic_mcp_endpoint_invalid")
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise RootResidentMcpError("semantic_mcp_endpoint_invalid")
        normalized = base_url.rstrip("/")
        if self._base_url is not None and self._base_url != normalized:
            raise RootResidentMcpError("semantic_mcp_endpoint_conflict")
        self._base_url = normalized

    def runtime_facts(self) -> RootResidentMcpRuntimeFacts:
        if self._authority is None:
            return RootResidentMcpRuntimeFacts((), (), ())
        binding = self._require_operation_binding()
        return RootResidentMcpRuntimeFacts(
            mcp_bindings=(
                _MCP_BINDING_PREFIX
                + "catalog@sha256:"
                + binding.semantic_mcp_catalog_hash,
                _MCP_BINDING_PREFIX
                + "operation-bindings@sha256:"
                + binding.semantic_mcp_operation_bindings_hash,
            ),
            capability_bindings=(
                "harness-operation-binding-v1",
                "semantic-mcp-resident",
            ),
            resource_bindings=(
                "adapter-source:meta_research.root_workspace@sha256:" + hashlib.sha256(
                    Path(__file__).with_name("root_workspace.py").read_bytes()).hexdigest(),
                _OPERATION_BINDING_RESOURCE_PREFIX
                + "contract:"
                + binding.contract_ref
                + "@sha256:"
                + binding.contract_hash,
                _OPERATION_BINDING_RESOURCE_PREFIX
                + "set:"
                + binding.conformance_ref
                + "@sha256:"
                + canonical_hash(binding.as_dict()),
                *binding.profile_receipts,
            ),
        )

    def acquire(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        root_session_ref: str,
        fence_ref: str,
        capability_binding_hash: str,
        phase: str,
        job_ref: str | None,
        guidance_binding: FrozenGuidanceBinding | None = None,
        revoke_on_failure: bool = False,
    ) -> tuple[RootResidentMcpChannelKey, RootResidentMcpAccess]:
        authority = self._authority
        base_url = self._base_url
        if authority is None or base_url is None:
            raise RootResidentMcpError("semantic_mcp_unavailable")
        binding = self._require_operation_binding()
        exact_scope_hash = canonical_hash(
            {
                "run_ref": run_ref,
                "attempt_ref": attempt_ref,
                "root_session_ref": root_session_ref,
                "fence_ref": fence_ref,
                "capability_binding_hash": capability_binding_hash,
                "root_kind": self._root_kind,
                "phase": phase,
                **({} if guidance_binding is None else {"guidance_binding": guidance_binding.as_dict()}),
            }
        )
        key = (job_ref or run_ref, phase, exact_scope_hash)
        with self._lock:
            channel = self._channels.get(key)
            if channel is None:
                try:
                    channel = authority.issue_resident_mcp_channel(
                        root_kind=self._root_kind,
                        phase=phase,
                        subject_policy="operation_tree",
                        run_ref=run_ref,
                        attempt_ref=attempt_ref,
                        root_session_ref=root_session_ref,
                        fence_ref=fence_ref,
                        capability_binding_hash=capability_binding_hash,
                        operation_ids=self._operation_ids,
                        **({} if guidance_binding is None else {"guidance_binding": guidance_binding}),
                    )
                except Exception as error:
                    raise RootResidentMcpError(
                        str(
                            getattr(
                                error, "code", "semantic_mcp_unavailable"
                            )
                        )
                    ) from error
                self._channels[key] = channel
            self._channel_users[key] = self._channel_users.get(key, 0) + 1
            try:
                endpoint = urlsplit(channel.binding.endpoint_ref)
                observed_operation_ids = tuple(
                    item.get("semantic_operation_id")
                    for item in channel.binding.operation_bindings
                )
                if (
                    not channel.connection.token
                    or channel.connection.grant_ref
                    != channel.binding.connection_grant_ref
                    or channel.binding.catalog_hash
                    != binding.semantic_mcp_catalog_hash
                    or canonical_hash(list(channel.binding.operation_bindings))
                    != binding.semantic_mcp_operation_bindings_hash
                    or observed_operation_ids != self._operation_ids
                    or channel.binding.root_kind != self._root_kind
                    or channel.binding.phase != phase
                    or getattr(channel.binding, "guidance_binding", None) != guidance_binding
                    or endpoint.scheme
                    or endpoint.netloc
                    or endpoint.query
                    or endpoint.fragment
                    or not endpoint.path.startswith("/")
                    or endpoint.path.startswith("//")
                ):
                    raise RootResidentMcpError("semantic_mcp_channel_invalid")
                scope_binding_hash = canonical_hash(
                    {
                        "catalog_hash": channel.binding.catalog_hash,
                        "operation_bindings": list(
                            channel.binding.operation_bindings
                        ),
                        "root_kind": channel.binding.root_kind,
                        "phase": channel.binding.phase,
                        "subject_policy": "operation_tree",
                        **({} if guidance_binding is None else {"guidance_binding": guidance_binding.as_dict()}),
                    }
                )
                return (*key, channel.connection.grant_ref), RootResidentMcpAccess(
                    url=base_url + endpoint.path,
                    token=channel.connection.token,
                    scope_binding_hash=scope_binding_hash,
                    operation_ids=self._operation_ids,
                    operation_bindings=channel.binding.operation_bindings,
                    workspace_binding=(None if self._workspaces is None else self._workspaces.bind_runtime(
                        SemanticCallContext(run_ref, attempt_ref, root_session_ref, fence_ref,
                            capability_binding_hash, self._root_kind, phase, "", guidance_binding))),
                )
            except SemanticMcpError as error:
                self._release((*key, channel.connection.grant_ref), immediate=revoke_on_failure)
                raise RootResidentMcpError(error.code) from error
            except Exception:
                self._release((*key, channel.connection.grant_ref), immediate=revoke_on_failure)
                raise

    def release(self, key: RootResidentMcpChannelKey) -> None:
        """Release one user of this exact grant generation."""
        self._release(key, immediate=False)

    def revoke(self, key: RootResidentMcpChannelKey) -> None:
        """End this physical call's grant now, including concurrent users.

        Bundle and Reasoning have always revoked at the first terminal call
        boundary. A late release from another user cannot revoke a newer grant
        issued for the same scope while that old user was finishing.
        """
        self._release(key, immediate=True)

    def _release(self, key: RootResidentMcpChannelKey, *, immediate: bool) -> None:
        scope = key[:3]
        with self._lock:
            channel = self._channels.get(scope)
            if channel is None or channel.connection.grant_ref != key[3]:
                return
            users = self._channel_users.get(scope, 0)
            if not immediate and users > 1:
                self._channel_users[scope] = users - 1
                return
            self._channel_users.pop(scope, None)
            channel = self._channels.pop(scope, None)
            authority = self._authority
        if channel is None or authority is None:
            return
        try:
            authority.revoke_resident_mcp_channel(channel)
        except Exception as error:
            raise RootResidentMcpError(
                str(getattr(error, "code", "semantic_mcp_revoke_failed"))
            ) from error

    def call_operation(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        root_session_ref: str,
        fence_ref: str,
        capability_binding_hash: str,
        phase: str,
        job_ref: str | None,
        operation_id: str,
        arguments: dict[str, object],
    ) -> dict[str, object]:
        """Call one operation through the same exact resident bearer as Agents."""

        if operation_id not in self._operation_ids:
            raise RootResidentMcpError("semantic_mcp_operation_unavailable")
        authority = self._authority
        if authority is None:
            raise RootResidentMcpError("semantic_mcp_unavailable")
        key, access = self.acquire(
            run_ref=run_ref,
            attempt_ref=attempt_ref,
            root_session_ref=root_session_ref,
            fence_ref=fence_ref,
            capability_binding_hash=capability_binding_hash,
            phase=phase,
            job_ref=job_ref,
        )
        try:
            status, payload = authority.dispatch_mcp(
                access.token,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": operation_id,
                        "arguments": arguments,
                    },
                },
            )
            result = payload.get("result") if isinstance(payload, dict) else None
            structured = (
                result.get("structuredContent")
                if isinstance(result, dict)
                else None
            )
            if (
                status != 200
                or not isinstance(result, dict)
                or not isinstance(structured, dict)
            ):
                details: dict[str, object] = {"operation_id": operation_id, "http_status": status}
                effect_id = arguments.get("effect_id") if isinstance(arguments, dict) else None
                if isinstance(effect_id, str) and 0 < len(effect_id) <= 128:
                    details["effect_id"] = effect_id
                if status in {401, 403}:
                    raise RootResidentMcpError("mcp_channel_authentication_required", {
                        **details, "category": "authorization",
                        "message": "This channel is not authorized.",
                        "correction": "Ask the host to restore the current scoped grant; do not bypass authorization.",
                    })
                protocol_error = payload.get("error") if isinstance(payload, dict) else None
                protocol_code = protocol_error.get("code") if isinstance(protocol_error, dict) else None
                unavailable = status == 429 or status >= 500
                if type(protocol_code) is int:
                    details["protocol_code"] = protocol_code
                if not unavailable and type(protocol_code) is int and protocol_code in {-32700, -32600, -32601, -32602}:
                    raise RootResidentMcpError("semantic_mcp_protocol_error", {
                        **details, "category": "host_contract",
                        "protocol_code": protocol_code,
                        "message": "The host's JSON-RPC request was rejected.",
                        "correction": "Repair the host request envelope, not the research submission.",
                    })
                details.update({
                    "category": ("temporarily_unavailable" if unavailable else
                                 "host_failure" if type(protocol_code) is int else "host_contract"),
                    "message": "No usable operation receipt was returned.",
                    "effect_state": "unknown",
                    "correction": "Restore service/host access, then reconcile the original unchanged operation before resubmitting.",
                })
                raise RootResidentMcpError(
                    "semantic_mcp_unavailable" if unavailable else
                    "semantic_mcp_protocol_error" if type(protocol_code) is int else "semantic_mcp_response_invalid", details)
            if result.get("isError") is True:
                code = structured.get("code")
                raise RootResidentMcpError(
                    code if isinstance(code, str) else "semantic_mcp_call_failed",
                    structured.get("details") if isinstance(structured.get("details"), dict) else None,
                )
            return structured
        finally:
            self.release(key)

    def release_job(self, job_ref: str, *, include_children: bool = False) -> None:
        with self._lock:
            selected = tuple(
                key
                for key in self._channels
                if key[0] == job_ref
                or (include_children and key[0].startswith(job_ref + ":"))
            )
            channels = tuple(self._channels.pop(key) for key in selected)
            for key in selected:
                self._channel_users.pop(key, None)
            authority = self._authority
        if authority is None:
            return
        for channel in channels:
            try:
                authority.revoke_resident_mcp_channel(channel)
            except Exception as error:
                raise RootResidentMcpError(
                    str(getattr(error, "code", "semantic_mcp_revoke_failed"))
                ) from error

    def release_all(self) -> None:
        with self._lock:
            channels = tuple(self._channels.values())
            self._channels.clear()
            self._channel_users.clear()
            authority = self._authority
        if authority is None:
            return
        for channel in channels:
            try:
                authority.revoke_resident_mcp_channel(channel)
            except Exception as error:
                raise RootResidentMcpError(
                    str(getattr(error, "code", "semantic_mcp_revoke_failed"))
                ) from error

    def _require_operation_binding(self) -> _OperationBinding:
        authority = self._authority
        if authority is None:
            raise RootResidentMcpError("semantic_mcp_unavailable")
        try:
            binding = authority.require_operation_binding(
                harness_family="codex",
                required_operation_ids=self._operation_ids,
                required_capabilities=("semantic_mcp",),
            )
        except Exception as error:
            raise RootResidentMcpError(
                str(getattr(error, "code", "semantic_mcp_unavailable"))
            ) from error
        if (
            binding.contract_ref != _OPERATION_BINDING_CONTRACT
            or binding.required_families != ("codex",)
            or binding.required_capabilities != ("semantic_mcp",)
            or binding.required_operation_ids != self._operation_ids
        ):
            raise RootResidentMcpError("semantic_mcp_operation_binding_invalid")
        return binding


def semantic_mcp_environment(token: str) -> dict[str, str]:
    configured: list[str] = []
    for name in ("NO_PROXY", "no_proxy"):
        value = os.environ.get(name, "")
        configured.extend(
            item.strip() for item in value.split(",") if item.strip()
        )
    for loopback in ("127.0.0.1", "localhost", "::1"):
        if loopback not in configured:
            configured.append(loopback)
    no_proxy = ",".join(configured)
    return {
        "META_RESEARCH_MCP_TOKEN": token,
        "NO_PROXY": no_proxy,
        "no_proxy": no_proxy,
    }


__all__ = [
    "RootResidentMcpAccess",
    "RootResidentMcpAuthority",
    "RootResidentMcpChannelKey",
    "RootResidentMcpChannels",
    "RootResidentMcpError",
    "RootResidentMcpRuntimeFacts",
    "semantic_mcp_environment",
]
