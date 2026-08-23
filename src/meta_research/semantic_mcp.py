from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Callable, Literal

from meta_research import __version__
from meta_research.owners.common import canonical_hash, new_ref


MCP_PROTOCOL_VERSION = "2025-06-18"


class SemanticMcpError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class SemanticCallContext:
    run_ref: str
    attempt_ref: str
    root_session_ref: str
    fence_ref: str
    capability_binding_hash: str

    def effect_key(self, effect_id: str) -> str:
        if not effect_id or len(effect_id) > 128:
            raise SemanticMcpError("semantic_effect_id_invalid")
        return "mcp-effect:" + canonical_hash(
            {
                "run_ref": self.run_ref,
                "attempt_ref": self.attempt_ref,
                "fence_ref": self.fence_ref,
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

    def tool(self) -> dict[str, object]:
        return {
            "name": self.semantic_operation_id,
            "title": self.semantic_operation_id,
            "description": self.description,
            "inputSchema": self.input_schema,
            "outputSchema": self.output_schema,
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
    deployment_profile: str = "local_resident_streamable_http"
    transport: str = "streamable_http"
    protocol_version: str = MCP_PROTOCOL_VERSION

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


class SemanticMcpGateway:
    """A narrow MCP facade over registered high-level Owner operations."""

    def __init__(self, operations: tuple[SemanticOperation, ...]) -> None:
        if not operations:
            raise SemanticMcpError("semantic_operation_catalog_empty")
        ordered = tuple(sorted(operations, key=lambda item: item.semantic_operation_id))
        if len({item.semantic_operation_id for item in ordered}) != len(ordered):
            raise SemanticMcpError("semantic_operation_id_duplicate")
        self._operations = {item.semantic_operation_id: item for item in ordered}
        self._validate_catalog(ordered)
        self._catalog_revision = 1
        self._catalog_hash = canonical_hash([item.binding() for item in ordered])
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
        return tuple(self._operations[item].binding() for item in operation_ids)

    def issue_channel(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        root_session_ref: str,
        fence_ref: str,
        capability_binding_hash: str,
        operation_ids: tuple[str, ...],
    ) -> tuple[McpConnection, ResidentMcpBinding]:
        if (
            not run_ref
            or not attempt_ref
            or not root_session_ref
            or not fence_ref
            or len(capability_binding_hash) != 64
            or not operation_ids
            or len(operation_ids) != len(set(operation_ids))
        ):
            raise SemanticMcpError("mcp_channel_scope_invalid")
        self.required_bindings(operation_ids)
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
        )
        self._grants[_token_hash(token)] = grant
        bindings = tuple(self._operations[item].binding() for item in operation_ids)
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
            ),
        )

    def revoke_channel(self, token: str) -> None:
        self._grants.pop(_token_hash(token), None)

    def dispatch(
        self, token: str | None, message: object
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
                        "Use only high-level semantic operations. Tool discovery does "
                        "not grant authority, and tool results are not Owner acceptance."
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
                        self._operations[item].tool() for item in grant.operation_ids
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
        if operation is None or operation_id not in grant.operation_ids:
            return 200, {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": _tool_error("capability_unavailable"),
            }
        if not _matches_schema(arguments, operation.input_schema):
            return 200, {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": _tool_error("semantic_input_schema_mismatch"),
            }
        try:
            context = SemanticCallContext(
                run_ref=grant.run_ref,
                attempt_ref=grant.attempt_ref,
                root_session_ref=grant.root_session_ref,
                fence_ref=grant.fence_ref,
                capability_binding_hash=grant.capability_binding_hash,
            )
            result = operation.handler(context, arguments)
        except SemanticMcpError as error:
            return 200, {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": _tool_error(error.code),
            }
        except Exception:
            return 200, {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": _tool_error("semantic_operation_failed"),
            }
        if not isinstance(result, dict) or not _matches_schema(
            result, operation.output_schema
        ):
            return 200, {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": _tool_error("semantic_output_schema_mismatch"),
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

    def _validate_catalog(
        self, operations: tuple[SemanticOperation, ...]
    ) -> None:
        for operation in operations:
            if (
                not operation.semantic_operation_id
                or not operation.owning_module
                or not operation.description
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
        if value is not None and (
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
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        return False
    schema_type = schema["type"]
    if schema_type == "object":
        if not isinstance(value, dict):
            return False
        required = schema.get("required", [])
        properties = schema.get("properties", {})
        if not isinstance(required, list) or not isinstance(properties, dict):
            return False
        if any(item not in value for item in required):
            return False
        if schema.get("additionalProperties") is False and any(
            item not in properties for item in value
        ):
            return False
        return all(
            name not in value
            or not isinstance(property_schema, dict)
            or _matches_schema(value[name], property_schema)
            for name, property_schema in properties.items()
        )
    if schema_type == "array":
        if not isinstance(value, list):
            return False
        items = schema.get("items")
        return not isinstance(items, dict) or all(
            _matches_schema(item, items) for item in value
        )
    if schema_type == "string":
        return (
            isinstance(value, str)
            and len(value) >= int(schema.get("minLength", 0))
            and (
                "maxLength" not in schema
                or len(value) <= int(schema["maxLength"])
            )
        )
    if schema_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if schema_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if schema_type == "boolean":
        return isinstance(value, bool)
    return value is None


def _jsonrpc_error(
    request_id: object, code: int, message: str
) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _tool_error(code: str) -> dict[str, object]:
    return {
        "content": [{"type": "text", "text": code}],
        "structuredContent": {"status": "capability_unavailable", "code": code},
        "isError": True,
    }


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
