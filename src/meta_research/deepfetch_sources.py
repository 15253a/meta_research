from __future__ import annotations

import re

from meta_research.owners.common import OwnerConflict
from meta_research.search_sources import SearchSourceError, SearchSourceRegistry
from meta_research.semantic_mcp import SemanticCallContext, SemanticMcpError, SemanticOperation


SOURCE_ACTION_ID = "deepfetch_source_action"
SOURCE_LEDGER_SCHEMA = "deepfetch.papers.v4.1"


def source_operations(owner, registry: SearchSourceRegistry | None) -> tuple[SemanticOperation, ...]:
    return (SemanticOperation(
        semantic_operation_id=SOURCE_ACTION_ID,
        owning_module="agent_runtime",
        description="Use one supplemental source pinned to this DeepFetch Run and return a host receipt.",
        input_schema={
            "type": "object", "additionalProperties": False,
            "required": ["source_id", "operation"],
            "properties": {
                "source_id": {"type": "string", "minLength": 1},
                "operation": {"type": "string", "enum": ["api_search", "website_open", "mcp_tool_call"]},
                "query": {"type": "string", "maxLength": 2000},
                "url": {"type": "string", "maxLength": 4000},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                "tool_name": {"type": "string", "maxLength": 256},
                "arguments": {"type": "object"},
            },
        },
        output_schema={"type": "object"},
        access_mode="read",
        handler=lambda context, arguments: source_action(owner, registry, context, arguments),
    ),)


def source_action(owner, registry: SearchSourceRegistry | None, context: SemanticCallContext,
                  arguments: dict[str, object]) -> dict[str, object]:
    if context.root_kind != "deepfetch":
        raise SemanticMcpError("deepfetch_source_action_unauthorized")
    try:
        scope = owner.verify_root_agent_runtime_scope(
            root_kind=context.root_kind, run_ref=context.run_ref,
            attempt_ref=context.attempt_ref, root_session_ref=context.root_session_ref,
            fence_ref=context.fence_ref, runtime_binding_hash=context.capability_binding_hash,
        )
        if scope.get("literature_access_mode") == "provided_only":
            raise SemanticMcpError("deepfetch_source_action_provided_only")
        operation_ref = scope.get("provider_operation_ref")
        turn = re.fullmatch(r"turn-(\d+)", context.phase)
        if not isinstance(operation_ref, str) or not operation_ref or turn is None:
            raise SemanticMcpError("deepfetch_source_job_unbound")
        if registry is None:
            raise SemanticMcpError("deepfetch_search_source_runtime_missing")
        manifest = registry.manifest_for_run(context.run_ref)
        if manifest["runtime_binding_hash"] != context.capability_binding_hash:
            raise SemanticMcpError("deepfetch_source_manifest_binding_mismatch")
        return registry.act(manifest=manifest,
                            job_ref=f"{operation_ref}:v4-turn:{turn.group(1)}",
                            command=arguments)
    except (OwnerConflict, SearchSourceError) as error:
        raise SemanticMcpError(error.code) from error


def verify_ledger_origins(ledger: dict, registry: SearchSourceRegistry | None,
                          manifest: dict | None) -> None:
    if ledger.get("schema_version") != SOURCE_LEDGER_SCHEMA:
        if manifest is not None:
            raise ValueError("deepfetch_source_ledger_schema_required")
        return
    receipts = [] if registry is None or manifest is None else registry.receipts(manifest)
    for paper in ledger["papers"].values():
        version = paper.get("paper_version")
        origins = paper.get("discovery_origins")
        if not isinstance(version, str) or not version or not isinstance(origins, list):
            raise ValueError("deepfetch_source_provenance_invalid")
        verified = []
        for origin in origins:
            if not isinstance(origin, dict) or set(origin) != {"receipt_ref"}:
                raise ValueError("deepfetch_source_provenance_invalid")
            if registry is None or manifest is None:
                raise ValueError("deepfetch_source_manifest_missing")
            identity = paper["identity"]
            verified.append(registry.verify_discovery(
                manifest, origin["receipt_ref"], doi=identity["doi"],
                arxiv=identity["arxiv_id"], version=version,
            ))
        if (registry is not None and manifest is not None and version != "unverified"
                and (paper["identity"]["doi"] or paper["identity"]["arxiv_id"])):
            identity = paper["identity"]
            for receipt in receipts:
                if any(origin["receipt_ref"] == receipt["receipt_ref"] for origin in verified):
                    continue
                try:
                    provenance = registry.verify_discovery(
                        manifest, receipt["receipt_ref"], doi=identity["doi"],
                        arxiv=identity["arxiv_id"], version=version,
                    )
                except SearchSourceError as error:
                    if error.code != "source_discovery_not_in_receipt":
                        raise
                else:
                    verified.append(provenance)
        paper["discovery_origins"] = verified
