from __future__ import annotations

import hashlib
import json
import secrets
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Callable, Literal

from meta_research.human_guidance import FrozenGuidanceBinding
from meta_research import __version__
from meta_research.owners.common import canonical_hash, new_ref
from meta_research.root_capabilities import ROOT_AGENT_KINDS, RootAgentKind
from meta_research.work_material_contract import RUNTIME_MATERIAL_OPERATION_IDS


MCP_PROTOCOL_VERSION = "2025-06-18"
ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS = (
    "human_request.open",
    "human_request.open.reconcile",
)
ROOT_AGENT_ACQUISITION_OPERATION_IDS = (
    "agent_runtime.acquisition.request",
    "agent_runtime.acquisition.request.reconcile",
)
ROOT_AGENT_DATASET_OPERATION_IDS = (
    "research_graph.datasets.page",
    "research_graph.datasets.read",
    "research_graph.datasets.register",
    "research_graph.datasets.register.reconcile",
    "research_graph.datasets.register_version",
    "research_graph.datasets.register_version.reconcile",
    "research_graph.datasets.reference",
    "research_graph.datasets.reference.reconcile",
    "research_graph.datasets.derive",
    "research_graph.datasets.derive.reconcile",
)
ROOT_AGENT_ENVIRONMENT_OPERATION_IDS = (
    "research_graph.environments.page",
    "research_graph.environments.read",
    "research_graph.environments.register",
    "research_graph.environments.register.reconcile",
    "research_graph.environments.reference",
    "research_graph.environments.reference.reconcile",
)
ROOT_AGENT_LITERATURE_OPERATION_IDS = (
    "research_memory.literature.register",
    "research_memory.literature.register.reconcile",
    "research_memory.literature.reference",
    "research_memory.literature.reference.reconcile",
    "research_memory.literature.read",
)
TOOL_DISCOVERY_OPERATION_IDS = (
    "tools.catalog.page",
    "tools.contracts.read",
)
ROOT_AGENT_COMMON_OPERATION_IDS = (
    *TOOL_DISCOVERY_OPERATION_IDS,
    *RUNTIME_MATERIAL_OPERATION_IDS,
    "research_memory.assets.page",
    "research_memory.assets.lifecycle",
    "research_memory.assets.current",
    "research_memory.assets.intake",
    "research_memory.assets.intake.reconcile",
    "research_memory.assets.retire",
    "research_memory.assets.retire.reconcile",
    "research_graph.questions.page",
    "research_memory.literature.page",
    "research_memory.content.read",
    "research_memory.creation_basis.read",
    "human_request.read",
    "research_graph.baselines.page",
    "research_graph.baselines.read",
    "research_graph.target_formal_results.read",
    "research_graph.formal_results.read",
    "research_graph.question_history.read",
    "research_graph.question_relations.read",
    "research_graph.artifact_roles.adjust",
    "research_graph.artifact_roles.adjust.reconcile",
    *ROOT_AGENT_DATASET_OPERATION_IDS,
    *ROOT_AGENT_ENVIRONMENT_OPERATION_IDS,
    *ROOT_AGENT_LITERATURE_OPERATION_IDS,
    *ROOT_AGENT_ACQUISITION_OPERATION_IDS,
    *ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS,
)


class SemanticMcpError(RuntimeError):
    def __init__(self, code: str, details: dict[str, object] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.details = details


@dataclass(frozen=True)
class SemanticCallContext:
    run_ref: str
    attempt_ref: str
    root_session_ref: str
    fence_ref: str
    capability_binding_hash: str
    root_kind: RootAgentKind | None
    phase: str
    operation_id: str
    guidance_binding: FrozenGuidanceBinding | None = None
    authorized_operation_ids: tuple[str, ...] = ()
    channel_grant_ref: str = ""

    def effect_key(self, effect_id: str) -> str:
        if not effect_id or len(effect_id) > 128:
            raise SemanticMcpError("semantic_effect_id_invalid")
        operation_id = self.operation_id
        for suffix in (".reconcile", ".observe", ".submit"):
            operation_id = operation_id.removesuffix(suffix)
        return "mcp-effect:" + canonical_hash(
            {
                "run_ref": self.run_ref,
                "attempt_ref": self.attempt_ref,
                "root_session_ref": self.root_session_ref,
                "fence_ref": self.fence_ref,
                "capability_binding_hash": self.capability_binding_hash,
                "root_kind": self.root_kind,
                "phase": self.phase,
                "operation_id": operation_id,
                "effect_id": effect_id,
            }
        )

    def dataset_effect_key(self, effect_id: str) -> str:
        """Keep a Dataset effect discoverable after a technical Root recovery."""
        return self._research_index_effect_key(effect_id, "datasets", ROOT_AGENT_DATASET_OPERATION_IDS)

    def environment_effect_key(self, effect_id: str) -> str:
        """Keep an Environment effect discoverable after a technical Root recovery."""
        return self._research_index_effect_key(effect_id, "environments", ROOT_AGENT_ENVIRONMENT_OPERATION_IDS)

    def _research_index_effect_key(self, effect_id: str, family_name: str, operations: tuple[str, ...]) -> str:
        if not isinstance(effect_id, str) or not effect_id or len(effect_id) > 128:
            raise SemanticMcpError("semantic_effect_id_invalid")
        family = self.operation_id.removesuffix(".reconcile")
        if family + ".reconcile" not in operations:
            prefix = "dataset" if family_name == "datasets" else "environment"
            raise SemanticMcpError(prefix + "_effect_operation_invalid")
        return "mcp-effect:" + canonical_hash({
            "run_ref": self.run_ref, "root_session_ref": self.root_session_ref,
            "root_kind": self.root_kind, "phase": self.phase,
            "operation_family": family, "effect_id": effect_id,
        })

    def human_request_effect_key(self, effect_id: str) -> str:
        """Bind one HumanRequest effect to the logical task across recovery Attempts."""

        if not effect_id or len(effect_id) > 128:
            raise SemanticMcpError("semantic_effect_id_invalid")
        return "mcp-effect:" + canonical_hash(
            {
                "run_ref": self.run_ref,
                "root_session_ref": self.root_session_ref,
                "root_kind": self.root_kind,
                "phase": self.phase,
                "operation_family": ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[0],
                "effect_id": effect_id,
            }
        )

    def acquisition_effect_key(self, effect_id: str) -> str:
        """Bind one Acquisition request to the logical task across recovery."""

        if not effect_id or len(effect_id) > 128:
            raise SemanticMcpError("semantic_effect_id_invalid")
        return "mcp-effect:" + canonical_hash(
            {
                "run_ref": self.run_ref,
                "root_session_ref": self.root_session_ref,
                "root_kind": self.root_kind,
                "phase": self.phase,
                "operation_family": ROOT_AGENT_ACQUISITION_OPERATION_IDS[0],
                "effect_id": effect_id,
            }
        )


@dataclass(frozen=True)
class SemanticOperation:
    semantic_operation_id: str
    owning_module: str
    description: str
    handler: Callable[
        [SemanticCallContext, dict[str, object]], dict[str, object]
    ]
    input_schema: dict[str, object]
    output_schema: dict[str, object]
    access_mode: Literal["read", "effect", "reconcile", "verify"] = "read"
    reconciliation_operation_id: str | None = None
    operation_contract_version: str = "v1"
    unavailable_reason: Callable[[SemanticCallContext], str | None] | None = None
    return_description: str = ""
    error_descriptions: dict[str, str] = field(default_factory=dict)

    def tool(self) -> dict[str, object]:
        return {
            "name": self.semantic_operation_id,
            "title": self.semantic_operation_id,
            "description": self.description,
            "inputSchema": deepcopy(self.input_schema),
            "outputSchema": deepcopy(self.output_schema),
        }

    def binding(self) -> dict[str, object]:
        return {
            "semantic_operation_id": self.semantic_operation_id,
            "operation_contract_version": self.operation_contract_version,
            "owning_module": self.owning_module,
            "access_mode": self.access_mode,
            "input_schema_hash": canonical_hash(self.input_schema),
            "output_schema_hash": canonical_hash(self.output_schema),
            "reconciliation_operation_id": self.reconciliation_operation_id,
            "discovered_tool_name": self.semantic_operation_id,
            "contract_documentation_hash": canonical_hash({
                "description": self.description, "returns": self.return_description,
                "errors": self.error_descriptions}),
        }


@dataclass(frozen=True)
class McpConnection:
    token: str
    grant_ref: str


@dataclass(frozen=True)
class ResidentMcpBinding:
    server_instance_ref: str
    endpoint_ref: str
    catalog_revision: int
    catalog_hash: str
    health_receipt_ref: str
    connection_grant_ref: str
    operation_bindings: tuple[dict[str, object], ...]
    root_kind: RootAgentKind | None
    phase: str
    deployment_profile: str = "local_resident_streamable_http"
    transport: str = "streamable_http"
    protocol_version: str = MCP_PROTOCOL_VERSION
    guidance_binding: FrozenGuidanceBinding | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "server_instance_ref": self.server_instance_ref,
            "deployment_profile": self.deployment_profile,
            "endpoint_ref": self.endpoint_ref,
            "transport": self.transport,
            "protocol_version": self.protocol_version,
            "catalog_revision": self.catalog_revision,
            "catalog_hash": self.catalog_hash,
            "health_receipt_ref": self.health_receipt_ref,
            "operation_bindings": list(self.operation_bindings),
            "connection_grant_ref": self.connection_grant_ref,
            "root_kind": self.root_kind,
            "phase": self.phase,
            **({} if self.guidance_binding is None else {"guidance_binding": self.guidance_binding.as_dict()}),
        }


@dataclass(frozen=True)
class _ChannelGrant:
    grant_ref: str
    run_ref: str
    attempt_ref: str
    root_session_ref: str
    fence_ref: str
    capability_binding_hash: str
    operation_ids: tuple[str, ...]
    root_kind: RootAgentKind | None
    phase: str
    guidance_binding: FrozenGuidanceBinding | None = None


class SemanticMcpGateway:
    """A narrow MCP facade over registered high-level Owner operations.

    Construction registers an owned contract snapshot. Changes to a caller's
    schema dictionaries take effect only in a new gateway registration, with
    its own server and grants. Handlers and availability checks remain live.
    """

    def __init__(self, operations: tuple[SemanticOperation, ...]) -> None:
        if not operations:
            raise SemanticMcpError("semantic_operation_catalog_empty")
        ordered = tuple(sorted((replace(item,
            input_schema=deepcopy(item.input_schema),
            output_schema=deepcopy(item.output_schema),
            error_descriptions=deepcopy(item.error_descriptions))
            for item in (*operations, *self._discovery_operations())),
            key=lambda item: item.semantic_operation_id))
        if len({item.semantic_operation_id for item in ordered}) != len(ordered):
            raise SemanticMcpError("semantic_operation_id_duplicate")
        self._operations = {item.semantic_operation_id: item for item in ordered}
        self._validate_catalog(ordered)
        self._catalog_revision = 1
        self._operation_bindings = {item.semantic_operation_id: item.binding() for item in ordered}
        self._catalog_hash = canonical_hash(list(self._operation_bindings.values()))
        self._server_instance_ref = new_ref("mcp_server")
        self._health_receipt_ref = new_ref("mcp_health")
        self._grants: dict[str, _ChannelGrant] = {}

    @property
    def operation_ids(self) -> tuple[str, ...]:
        return tuple(self._operations)

    def query_status(self) -> dict[str, object]:
        health_payload = {
            "server_instance_ref": self._server_instance_ref,
            "deployment_profile": "local_resident_streamable_http",
            "transport": "streamable_http",
            "protocol_version": MCP_PROTOCOL_VERSION,
            "catalog_revision": self._catalog_revision,
            "catalog_hash": self._catalog_hash,
        }
        return {
            **health_payload,
            "status": "ready",
            "operation_ids": list(self.operation_ids),
            "health_receipt": {
                "issuer": "semantic_mcp_gateway",
                "kind": "resident_health",
                "receipt_ref": self._health_receipt_ref,
                "subject_ref": self._server_instance_ref,
                "payload_hash": canonical_hash(health_payload),
            },
        }

    def required_bindings(
        self, operation_ids: tuple[str, ...]
    ) -> tuple[dict[str, object], ...]:
        if (
            not operation_ids
            or len(operation_ids) != len(set(operation_ids))
            or any(item not in self._operations for item in operation_ids)
        ):
            raise SemanticMcpError("required_semantic_operation_unavailable")
        selected = set(operation_ids)
        if any(
            operation.access_mode == "effect"
            and operation.reconciliation_operation_id not in selected
            for operation in (self._operations[item] for item in operation_ids)
        ):
            raise SemanticMcpError(
                "required_semantic_reconciliation_unavailable"
            )
        return tuple(dict(self._operation_bindings[item]) for item in operation_ids)

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
        guidance_binding: FrozenGuidanceBinding | None = None,
    ) -> tuple[McpConnection, ResidentMcpBinding]:
        if (
            not run_ref
            or not attempt_ref
            or not root_session_ref
            or not fence_ref
            or len(capability_binding_hash) != 64
            or not operation_ids
            or len(operation_ids) != len(set(operation_ids))
            or (root_kind is not None and root_kind not in ROOT_AGENT_KINDS)
            or not isinstance(phase, str)
            or not phase
            or len(phase) > 128
        ):
            raise SemanticMcpError("mcp_channel_scope_invalid")
        bindings = self.required_bindings(operation_ids)
        token = secrets.token_urlsafe(32)
        grant_ref = new_ref("mcp_grant")
        grant = _ChannelGrant(
            grant_ref=grant_ref,
            run_ref=run_ref,
            attempt_ref=attempt_ref,
            root_session_ref=root_session_ref,
            fence_ref=fence_ref,
            capability_binding_hash=capability_binding_hash,
            operation_ids=operation_ids,
            root_kind=root_kind,
            phase=phase,
            guidance_binding=guidance_binding,
        )
        self._grants[_token_hash(token)] = grant
        return (
            McpConnection(token=token, grant_ref=grant_ref),
            ResidentMcpBinding(
                server_instance_ref=self._server_instance_ref,
                endpoint_ref="/mcp",
                catalog_revision=self._catalog_revision,
                catalog_hash=self._catalog_hash,
                health_receipt_ref=self._health_receipt_ref,
                connection_grant_ref=grant_ref,
                operation_bindings=bindings,
                root_kind=root_kind,
                phase=phase,
                guidance_binding=guidance_binding,
            ),
        )

    def revoke_channel(self, token: str) -> None:
        self._grants.pop(_token_hash(token), None)

    def dispatch(
        self, token: str | None, message: object, *,
        allowed_operation_ids: tuple[str, ...] | None = None,
    ) -> tuple[int, dict[str, object] | None]:
        grant = self._grants.get(_token_hash(token or ""))
        if grant is None:
            return 401, {
                "error": {
                    "code": "mcp_channel_authentication_required",
                    "message": "A current scope-bound MCP channel is required.",
                }
            }
        if not isinstance(message, dict):
            return 400, _jsonrpc_error(None, -32600, "invalid_request")
        request_id = message.get("id")
        if message.get("jsonrpc") != "2.0" or not isinstance(
            message.get("method"), str
        ):
            return 400, _jsonrpc_error(request_id, -32600, "invalid_request")
        method = message["method"]
        operation_ids = tuple(item for item in grant.operation_ids
                              if allowed_operation_ids is None or item in allowed_operation_ids)
        context = SemanticCallContext(
            run_ref=grant.run_ref, attempt_ref=grant.attempt_ref,
            root_session_ref=grant.root_session_ref, fence_ref=grant.fence_ref,
            capability_binding_hash=grant.capability_binding_hash, root_kind=grant.root_kind,
            phase=grant.phase, operation_id="", guidance_binding=grant.guidance_binding,
            authorized_operation_ids=operation_ids, channel_grant_ref=grant.grant_ref)
        if method == "notifications/initialized":
            return 202, None
        if method == "initialize":
            return 200, {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {
                        "name": "meta-research-semantic-gateway",
                        "version": __version__,
                    },
                    "instructions": (
                        "通过本服务的高层语义操作读取或改变 Owner 权威状态；其他研究工作按当前授予的原生工具和阶段规则进行。"
                        "以对应 Owner 验证的接纳事实与真实回执确认结果；工具可见或普通执行成功不扩大授权，也不替代领域接纳。"
                        "工具调用失败以 isError=true 和 structuredContent.code 为准，可选 details 保留具体诊断与原操作对账信息；正常 Owner status 仍是业务状态。"
                        "按需使用 tools.catalog.page 与 tools.contracts.read；已知 reader 可直接调用，无需先发现。"
                        "capability_unavailable 表示操作未注册、未获授或当前服务不可用；content_version_unbound 表示本次精确版本在当前范围未绑定，不表示全库不存在。"
                    ),
                },
            }
        if method == "ping":
            return 200, {"jsonrpc": "2.0", "id": request_id, "result": {}}
        if method == "tools/list":
            return 200, {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "tools": [
                        self._operations[item].tool() for item in operation_ids
                        if self._unavailable_reason(item, context) is None
                    ]
                },
            }
        if method != "tools/call":
            return 200, _jsonrpc_error(request_id, -32601, "method_not_found")
        params = message.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return 200, _jsonrpc_error(request_id, -32602, "invalid_params")
        operation_id = params["name"]
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            return 200, _jsonrpc_error(request_id, -32602, "invalid_params")
        operation = self._operations.get(operation_id)
        if operation is None or operation_id not in operation_ids:
            return 200, {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": _tool_error("capability_unavailable"),
            }
        unavailable = self._unavailable_reason(operation_id, context)
        if unavailable is not None:
            return 200, {"jsonrpc": "2.0", "id": request_id,
                         "result": _tool_error("capability_unavailable", {"reason": unavailable})}
        problem = _schema_problem(arguments, operation.input_schema)
        if problem is not None and not _matches_bound_target_human_request(
            operation_id, arguments
        ):
            return 200, {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": _tool_error("semantic_input_schema_mismatch", {
                    **problem, "operation_id": operation_id,
                    "category": "arguments", "execution_stage": "input_validation",
                    "call_execution": "not_started",
                    "message": "Input violates this field constraint; this call did not run.",
                    "correction": "Fix the indicated field using expected. Reconcile any earlier uncertain effect unchanged before modifying its submission.",
                }),
            }
        try:
            context = replace(context, operation_id=operation_id)
            result = operation.handler(context, arguments)
        except SemanticMcpError as error:
            details = dict(error.details or {})
            if error.code in operation.error_descriptions:
                details.setdefault("message", operation.error_descriptions[error.code])
            if operation.access_mode in {"effect", "reconcile"}:
                # The wrapper cannot infer transaction outcome from an Owner code.
                # Keep any more precise phase/outcome supplied by that Owner.
                recovery = _post_handler_failure(operation, arguments, "handler")
                for key in ("operation_id", "execution_stage", "effect_status",
                            "effect_id", "reconciliation_operation_id", "correction"):
                    if key in recovery:
                        details.setdefault(key, recovery[key])
            return 200, {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": _tool_error(error.code, details or None),
            }
        except Exception:
            return 200, {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": _tool_error("semantic_operation_failed",
                    _post_handler_failure(operation, arguments, "handler")),
            }
        output_problem = _schema_problem(result, operation.output_schema)
        if output_problem is not None:
            return 200, {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": _tool_error("semantic_output_schema_mismatch", {
                    **_post_handler_failure(operation, arguments, "output_validation"),
                    **output_problem}),
            }
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        return 200, {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [{"type": "text", "text": encoded}],
                "structuredContent": result,
                "isError": False,
            },
        }

    def _unavailable_reason(self, operation_id: str, context: SemanticCallContext) -> str | None:
        operation = self._operations.get(operation_id)
        if operation is None:
            return "not_registered"
        if operation_id not in context.authorized_operation_ids:
            return "not_granted"
        if operation.unavailable_reason is not None:
            try:
                return operation.unavailable_reason(replace(context, operation_id=operation_id))
            except Exception:
                return "availability_check_failed"
        return None

    def _discovery_operations(self) -> tuple[SemanticOperation, ...]:
        return (
            SemanticOperation(
                "tools.catalog.page", "harness",
                "Page currently granted, available semantic operations. Use query or owning_module to narrow the list. "
                "Continue using next_offset and catalog_hash from this page; tool_catalog_changed requires restarting at offset 0. "
                "Discovery grants no permission and is never required before a known reader call.",
                self._catalog_page,
                {"type": "object", "additionalProperties": False, "properties": {
                    "offset": {"type": "integer", "minimum": 0, "default": 0,
                               "description": "Entry offset. A nonzero offset requires this filtered page's catalog_hash."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20,
                              "description": "Maximum operations returned per page."},
                    "query": {"type": "string", "maxLength": 1024, "default": "",
                              "description": "Case-insensitive substring of operation id, owner or description."},
                    "owning_module": {"type": "string", "minLength": 1, "maxLength": 256,
                                      "description": "Exact registered owning_module; omitted means all granted owners."},
                    "catalog_hash": {"type": "string", "minLength": 64, "maxLength": 64,
                                     "description": "Hash from the preceding page, bound to this grant, filters and available contracts; never an authorization."},
                }},
                {"type": "object", "required": ["items", "offset", "next_offset", "total", "catalog_hash"],
                 "properties": {"items": {"type": "array", "items": {"type": "object"}},
                    "offset": {"type": "integer"}, "next_offset": {"type": ["integer", "null"]},
                    "total": {"type": "integer"}, "catalog_hash": {"type": "string"}}},
                return_description="items are compact summaries ordered by operation_id. next_offset=null ends this filtered catalog; total counts available operations only.",
                error_descriptions={"tool_catalog_changed": "A continuation omitted its hash or the grant, filters or available contracts changed. Restart from offset 0."}),
            SemanticOperation(
                "tools.contracts.read", "harness",
                "Read the live schema, return and error contract for one exact currently granted operation_id. "
                "Unknown, ungranted or unavailable operations return capability_unavailable with a reason. "
                "The contract is descriptive; tools/call rechecks current authority and service availability.",
                self._contract_read,
                {"type": "object", "additionalProperties": False, "required": ["operation_id"],
                 "properties": {"operation_id": {"type": "string", "minLength": 1, "maxLength": 256}}},
                {"type": "object", "required": ["operation_id", "name", "inputSchema", "outputSchema", "returns", "errors"]},
                return_description="Returns the same live tool schema and description as tools/list plus operation ownership, mode, reconciliation, return and error documentation.",
                error_descriptions={"capability_unavailable": "details.reason distinguishes not_registered, not_granted and the current service's unavailable reason; no hidden contract is returned."}),
        )

    def _catalog_page(self, context: SemanticCallContext, arguments: dict[str, object]) -> dict[str, object]:
        query = str(arguments.get("query", "")).casefold()
        module = arguments.get("owning_module")
        operations = [operation for name, operation in self._operations.items()
                      if self._unavailable_reason(name, context) is None
                      and (module is None or operation.owning_module == module)
                      and query in (name + " " + operation.owning_module + " " + operation.description).casefold()]
        catalog_hash = canonical_hash({"grant": context.channel_grant_ref, "query": query, "owning_module": module,
            "registered_catalog": self._catalog_hash,
            "operations": [operation.semantic_operation_id for operation in operations]})
        offset, limit = int(arguments.get("offset", 0)), int(arguments.get("limit", 20))
        if offset < 0 or not 1 <= limit <= 100:
            raise SemanticMcpError("semantic_input_schema_mismatch")
        expected = arguments.get("catalog_hash")
        if (offset > 0 and expected is None) or (expected is not None and expected != catalog_hash):
            raise SemanticMcpError("tool_catalog_changed", {"restart_offset": 0})
        return {"items": [{"operation_id": operation.semantic_operation_id,
                           "owning_module": operation.owning_module,
                           "access_mode": operation.access_mode,
                           "summary": operation.description.split(". ", 1)[0]}
                          for operation in operations[offset:offset + limit]],
                "offset": offset, "next_offset": offset + limit if offset + limit < len(operations) else None,
                "total": len(operations), "catalog_hash": catalog_hash}

    def _contract_read(self, context: SemanticCallContext, arguments: dict[str, object]) -> dict[str, object]:
        operation_id = str(arguments["operation_id"])
        reason = self._unavailable_reason(operation_id, context)
        if reason is not None:
            raise SemanticMcpError("capability_unavailable", {"reason": reason})
        operation = self._operations[operation_id]
        return {**operation.tool(), "operation_id": operation_id,
                "owning_module": operation.owning_module, "access_mode": operation.access_mode,
                "operation_contract_version": operation.operation_contract_version,
                "reconciliation_operation_id": operation.reconciliation_operation_id,
                "returns": "Success: isError=false; structuredContent matches outputSchema. content contains the same JSON. "
                           "Operation-specific pagination, units and acceptance semantics are in description and schemas. "
                           + operation.return_description,
                "errors": {"description": "Failure: isError=true; text contains the same JSON code/details as structuredContent. "
                           "Owner codes/details remain intact; normal Owner status is business state, not a transport failure. "
                           "A revoked or stale channel returns HTTP 401 mcp_channel_authentication_required. "
                           "Input diagnostics identify path, requirement and expected without echoing submitted values. "
                           "For an uncertain effect result, use reconciliation_operation_id with the original unchanged submission and effect identity before any new write. "
                           "Handler failures do not establish whether an effect committed. Host/tool contract defects require host repair, not changing research conclusions.",
                           "operation_codes": dict(operation.error_descriptions),
                           "gateway_codes": ["capability_unavailable", "semantic_input_schema_mismatch",
                                             "semantic_output_schema_mismatch", "semantic_operation_failed"]}}

    def _validate_catalog(
        self, operations: tuple[SemanticOperation, ...]
    ) -> None:
        for operation in operations:
            if (
                not operation.semantic_operation_id
                or not operation.owning_module
                or not operation.description
                or not isinstance(operation.return_description, str)
                or not isinstance(operation.error_descriptions, dict)
                or any(not isinstance(code, str) or not code or not isinstance(description, str)
                       or not description for code, description in operation.error_descriptions.items())
                or (operation.unavailable_reason is not None and not callable(operation.unavailable_reason))
                or not _valid_schema_contract(operation.input_schema)
                or not _valid_schema_contract(operation.output_schema)
                or operation.input_schema.get("type") != "object"
                or operation.output_schema.get("type") != "object"
                or operation.access_mode
                not in {"read", "effect", "reconcile", "verify"}
            ):
                raise SemanticMcpError("semantic_operation_schema_invalid")
            if operation.access_mode == "effect":
                reconciliation_id = operation.reconciliation_operation_id
                reconciliation = self._operations.get(reconciliation_id or "")
                if (
                    reconciliation is None
                    or reconciliation.access_mode != "reconcile"
                    or reconciliation.owning_module != operation.owning_module
                ):
                    raise SemanticMcpError(
                        "semantic_effect_reconciliation_required"
                    )
            elif operation.reconciliation_operation_id is not None:
                raise SemanticMcpError("semantic_operation_schema_invalid")


def _valid_schema_contract(schema: object) -> bool:
    if not isinstance(schema, dict):
        return False
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        return bool(schema_type) and all(isinstance(member, str)
            and _valid_schema_contract({**schema, "type": member}) for member in schema_type)
    if schema_type not in {
        "object",
        "array",
        "string",
        "integer",
        "number",
        "boolean",
        "null",
    }:
        return False
    enum = schema.get("enum")
    if enum is not None and (not isinstance(enum, list) or not enum):
        return False
    for key in ("minLength", "maxLength"):
        value = schema.get(key)
        if key in schema and (
            not isinstance(value, int) or isinstance(value, bool) or value < 0
        ):
            return False
    minimum_length = schema.get("minLength")
    maximum_length = schema.get("maxLength")
    if (
        isinstance(minimum_length, int)
        and isinstance(maximum_length, int)
        and minimum_length > maximum_length
    ):
        return False
    for key in ("minimum", "maximum"):
        if key in schema and (not isinstance(schema[key], (int, float)) or isinstance(schema[key], bool)):
            return False
    if "minimum" in schema and "maximum" in schema and schema["minimum"] > schema["maximum"]:
        return False
    if schema_type == "object":
        required = schema.get("required", [])
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        return (
            isinstance(required, list)
            and all(isinstance(item, str) and item for item in required)
            and len(required) == len(set(required))
            and isinstance(properties, dict)
            and all(
                isinstance(name, str)
                and name
                and _valid_schema_contract(property_schema)
                for name, property_schema in properties.items()
            )
            and isinstance(additional, bool)
        )
    if schema_type == "array":
        return "items" not in schema or _valid_schema_contract(schema["items"])
    return True


def _matches_schema(value: object, schema: dict[str, object]) -> bool:
    return _schema_problem(value, schema) is None


def _schema_problem(
    value: object, schema: dict[str, object], path: tuple[str | int, ...] = (),
) -> dict[str, object] | None:
    """Explain the first failed contract check without echoing submitted values."""
    def problem(requirement: str, expected: object, at=path):
        return {"path": list(at), "requirement": requirement, "expected": expected}

    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        return problem("enum", enum)
    schema_type = schema["type"]
    if isinstance(schema_type, list):
        if any(_matches_schema(value, {**schema, "type": member}) for member in schema_type):
            return None
        # Preserve the actual constraint when the value has an allowed type.
        for member in schema_type:
            candidate = _schema_problem(value, {**schema, "type": member}, path)
            if candidate is not None and candidate["requirement"] != "type":
                return candidate
        return problem("type", schema_type)
    if schema_type == "object":
        if not isinstance(value, dict):
            return problem("type", "object")
        properties = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                return problem("required", "present", (*path, name))
        if schema.get("additionalProperties") is False and any(name not in properties for name in value):
            # Unknown property names can themselves contain secrets; show allowed names.
            return problem("additionalProperties", list(properties))
        for name, child in properties.items():
            if name in value:
                failure = _schema_problem(value[name], child, (*path, name))
                if failure is not None:
                    return failure
        return None
    if schema_type == "array":
        if not isinstance(value, list):
            return problem("type", "array")
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                failure = _schema_problem(item, items, (*path, index))
                if failure is not None:
                    return failure
        return None
    if schema_type == "string":
        if not isinstance(value, str):
            return problem("type", "string")
        for key, failed in (("minLength", len(value) < schema.get("minLength", 0)),
                            ("maxLength", len(value) > schema.get("maxLength", len(value)))):
            if failed:
                return problem(key, schema[key])
        return None
    if schema_type in {"integer", "number"}:
        kinds = (int,) if schema_type == "integer" else (int, float)
        if not isinstance(value, kinds) or isinstance(value, bool):
            return problem("type", schema_type)
        for key, failed in (("minimum", value < schema.get("minimum", value)),
                            ("maximum", value > schema.get("maximum", value))):
            if failed:
                return problem(key, schema[key])
        return None
    if schema_type == "boolean":
        return None if isinstance(value, bool) else problem("type", "boolean")
    return None if value is None else problem("type", "null")


def _post_handler_failure(
    operation: SemanticOperation, arguments: dict[str, object], stage: str,
) -> dict[str, object]:
    details: dict[str, object] = {
        "operation_id": operation.semantic_operation_id,
        "category": "tool_contract" if stage == "output_validation" else "host_failure",
        "execution_stage": stage,
        "message": ("Handler output violates its contract."
                    if stage == "output_validation" else "Handler failed without a valid receipt."),
        "correction": "Report this operation to the tool owner; do not alter research facts to bypass it.",
    }
    if operation.access_mode == "effect":
        details.update({
            "effect_status": "unknown",
            "reconciliation_operation_id": operation.reconciliation_operation_id,
            "correction": "Reconcile the original unchanged submission and effect first. Preserve accepted work; do not resubmit while the outcome is unknown.",
        })
        if isinstance(arguments.get("effect_id"), str):
            details["effect_id"] = arguments["effect_id"]
    elif operation.access_mode == "reconcile":
        details.update({"effect_status": "unknown",
                        "correction": "Restore reconciliation first. Keep the original identity and submission; the outcome is still unknown."})
        if isinstance(arguments.get("effect_id"), str):
            details["effect_id"] = arguments["effect_id"]
    return details


def _matches_bound_target_human_request(
    operation_id: str, arguments: dict[str, object]
) -> bool:
    """Admit the exact Bundle extension while preserving the frozen catalog."""

    condition = arguments.get("condition")
    authorization = arguments.get("required_authorization")
    return (
        operation_id in ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS
        and isinstance(condition, dict)
        and condition.get("schema_ref")
        == "meta-research/root-agent-human-request-target/v1"
        and isinstance(authorization, dict)
        and authorization.get("capability") == "execute_high_risk_target"
    )


def _jsonrpc_error(
    request_id: object, code: int, message: str
) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _tool_error(code: str, details: dict[str, object] | None = None) -> dict[str, object]:
    payload = {"code": code, **({"details": details} if details is not None else {})}
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))}],
        "structuredContent": payload,
        "isError": True,
    }


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
