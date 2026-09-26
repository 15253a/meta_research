"""System MCP management routes; authentication is the application's middleware."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from meta_research.root_capabilities import ROOT_AGENT_KINDS
from meta_research.paths import DataRoot
from meta_research.codex_runtime import CODEX_LOCKED_VERSION
from meta_research.provider_supervisor import protected_subprocess_environment
from meta_research.system_mcp_probe import check_system_mcp
from meta_research.system_mcp import (
    SystemMcpRegistry, SystemMcpValidationError, SystemMcpConflictError, SystemMcpLoadError,
)


class RevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0, strict=True)


class SaveServerRequest(RevisionRequest):
    config: dict[str, object]


def system_mcp_router(registry: SystemMcpRegistry, data_root: DataRoot) -> APIRouter:
    router = APIRouter(prefix="/api/v1/system/mcp")

    def present(state: dict) -> dict:
        checks = {item["server_id"]: item for item in registry.check_status()}
        return {**state, "root_kinds": list(ROOT_AGENT_KINDS),
                "backend": "codex", "connection_check": "supported",
                "operations": list(reversed(registry.load_status())),
                "servers": [{**server, "connection_status": checks.get(server["server_id"], {"status": "unknown",
                    "revision": server["revision"], "checked_at": None})}
                    for server in state["servers"]],
                "internal_server": {"server_id": "meta_research", "read_only": True}}

    def perform(operation) -> dict:
        try:
            return present(operation())
        except SystemMcpConflictError:
            raise HTTPException(409, detail={"code": "system_mcp_revision_conflict"}) from None
        except SystemMcpValidationError as error:
            raise HTTPException(422, detail={"code": "system_mcp_config_invalid",
                                             "message": str(error)}) from None
        except SystemMcpLoadError:
            raise HTTPException(503, detail={"code": "system_mcp_registry_unavailable"}) from None

    @router.get("")
    def read() -> dict:
        return perform(registry.read)

    @router.post("", status_code=201)
    def create(request: SaveServerRequest) -> dict:
        return perform(lambda: registry.create(request.config, expected_revision=request.expected_revision))

    @router.put("/{server_id}")
    def update(server_id: str, request: SaveServerRequest) -> dict:
        return perform(lambda: registry.update(server_id, request.config, expected_revision=request.expected_revision))

    @router.delete("/{server_id}")
    def delete(server_id: str, request: RevisionRequest) -> dict:
        return perform(lambda: registry.delete(server_id, expected_revision=request.expected_revision))

    @router.post("/{server_id}/check")
    def check(server_id: str, request: RevisionRequest) -> dict:
        try:
            return check_system_mcp(registry, server_id, expected_revision=request.expected_revision,
                executable=str(data_root.validated_codex_cli_executable(CODEX_LOCKED_VERSION)),
                environment=protected_subprocess_environment(protected=data_root.codex_environment))
        except SystemMcpConflictError as error:
            raise HTTPException(409, detail={"code": str(error)}) from None
        except SystemMcpValidationError as error:
            raise HTTPException(422, detail={"code": str(error)}) from None
        except SystemMcpLoadError:
            raise HTTPException(503, detail={"code": "system_mcp_registry_unavailable"}) from None

    return router
