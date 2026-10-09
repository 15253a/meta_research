import hashlib
import os
from pathlib import Path
import stat

from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.owners.research_memory import AssetIntakeRequest
from meta_research.research_content import _quest
from meta_research.semantic_mcp import SemanticMcpError, SemanticOperation
from sqlalchemy import text


_TARGET_NOTE_SCHEMA = "meta-research/target-workspace-note/v1"
_TARGET_NOTE_PATHS = frozenset(
    {"outputs/analysis/research-note.md", "handoff/final-message.md"}
)
_TARGET_NOTE_RESERVED_PROVENANCE = frozenset(
    {
        "producer_kind",
        "target_ref",
        "target_run_ref",
        "workspace_ref",
        "relative_path",
        "source_content_hash",
        "source_locator",
    }
)


def asset_lifecycle_operations(graph, memory, agent_runtime, target_run_agent=None):
    string = {"type": "string", "minLength": 1, "maxLength": 16000}
    integer = {"type": "integer", "minimum": 0}

    def scope(context, version_ref=None, *, reconcile=False):
        if reconcile:
            verified = agent_runtime.verify_root_agent_human_request_reconcile_scope(
                root_kind=context.root_kind,
                run_ref=context.run_ref,
                attempt_ref=context.attempt_ref,
                root_session_ref=context.root_session_ref,
                fence_ref=context.fence_ref,
                runtime_binding_hash=context.capability_binding_hash,
            )
            quest_ref = verified.get("quest_ref")
            if not isinstance(quest_ref, str) or not quest_ref:
                raise OwnerConflict("content_quest_scope_required")
        else:
            quest_ref = _quest(agent_runtime, context)
        if version_ref is not None:
            graph.verify_asset_quest_scope(version_ref, quest_ref=quest_ref)
        return quest_ref

    def key(context, effect_id, action):
        return "mcp-asset:" + canonical_hash(
            {
                "run_ref": context.run_ref,
                "root_session_ref": context.root_session_ref,
                "root_kind": context.root_kind,
                "action": action,
                "effect_id": effect_id,
            }
        )

    def read(context, arguments, action):
        try:
            quest = scope(context)
            if action == "page":
                offset, limit = arguments.get("offset", 0), arguments.get("limit", 20)
                with memory._database.read() as connection:
                    refs = (
                        connection.execute(
                            text(
                                "SELECT a.asset_ref FROM rm_assets a WHERE (EXISTS (SELECT 1 FROM rg_asset_roles r WHERE r.asset_ref=a.asset_ref AND r.quest_ref=:quest) OR EXISTS (SELECT 1 FROM rm_asset_changes c WHERE c.asset_ref=a.asset_ref AND c.kind='initial' AND json_extract(c.payload_json,'$.origin_quest_ref')=:quest)) AND EXISTS (SELECT 1 FROM rm_asset_versions v WHERE v.asset_ref=a.asset_ref AND instr(lower(v.display_name),lower(:query))>0) ORDER BY a.created_at DESC,a.asset_ref LIMIT :limit OFFSET :offset"
                            ),
                            {
                                "quest": quest,
                                "query": arguments.get("query", ""),
                                "limit": limit + 1,
                                "offset": offset,
                            },
                        )
                        .scalars()
                        .all()
                    )
                items = []
                for asset_ref in refs[:limit]:
                    lifecycle = memory.query_asset_lifecycle(asset_ref)
                    version_ref = (
                        lifecycle["current_version_ref"]
                        or lifecycle["versions"][-1]["version_ref"]
                    )
                    scope(context, version_ref)
                    asset = memory.query_asset_version(version_ref)
                    lifecycle_state = next(
                        version["state"]
                        for version in lifecycle["versions"]
                        if version["version_ref"] == version_ref
                    )
                    items.append(
                        {
                            "asset": {
                                **asset.as_public_dict(),
                                "lifecycle_state": lifecycle_state,
                            },
                            "lifecycle": lifecycle,
                        }
                    )
                return {
                    "items": items,
                    "offset": offset,
                    "next_offset": offset + limit if len(refs) > limit else None,
                }
            version_ref = arguments["version_ref"]
            scope(context, version_ref)
            asset = memory.query_asset_version(version_ref)
            lifecycle = memory.query_asset_lifecycle(asset.asset_ref)
            lifecycle["reference_revision"] = graph.query_asset_reference_revision()
            if action == "lifecycle":
                return lifecycle
            current = memory.query_current_asset(asset.asset_ref)
            return {
                "lifecycle": lifecycle,
                "asset": None if current is None else current.as_public_dict(),
            }
        except OwnerConflict as error:
            raise SemanticMcpError(error.code, error.details) from error

    def effect(context, arguments, action, reconcile):
        try:
            quest = scope(context, reconcile=reconcile)
            effect_key = key(context, arguments["effect_id"], action)
            if action == "intake":
                values = dict(arguments["intake"])
                selector = values.pop("target_workspace_note", None)
                text_value = values.pop("text", None)
                if selector is not None:
                    if values.get("asset_ref") is not None or values.get("change") is not None:
                        raise OwnerConflict("target_note_selector_invalid")
                    if "provenance" in values:
                        raise OwnerConflict("target_note_provenance_reserved")
                    if target_run_agent is None:
                        raise OwnerConflict("target_note_intake_unavailable")
                    expected = _target_note_provenance(selector)
                    replay = memory.query_target_note_intake_effect_record(
                        effect_key
                    )
                    if replay is not None:
                        _verify_target_note_replay(
                            replay["binding"],
                            values=values,
                            quest_ref=quest,
                            provenance=expected,
                        )
                        result = replay["result"]
                        return {
                            "status": result["status"],
                            "result": result,
                        } if reconcile else result
                    if reconcile:
                        return {"status": "not_found", "result": None}
                    content = _read_target_note(
                        graph=graph,
                        target_run_agent=target_run_agent,
                        quest_ref=quest,
                        selector=selector,
                    )
                    values["content"] = content
                    values["provenance"] = expected
                else:
                    _reject_target_note_provenance(values.get("provenance"))
                    if not isinstance(text_value, str):
                        raise OwnerConflict("asset_content_required")
                    values["content"] = text_value.encode("utf-8")
                values["origin_quest_ref"] = quest
                values["effect_scope_required"] = True
                request = AssetIntakeRequest(**values)
                change = request.change
                if change is not None:
                    scope(context, change["predecessor_version_ref"])
                    for binding in change.get("evidence_bindings", []):
                        scope(context, binding["version_ref"])
                if reconcile:
                    result = memory.query_asset_intake_by_idempotency_key(
                        effect_key, request
                    )
                    return {
                        "status": "not_found" if result is None else result.status,
                        "result": None if result is None else result.as_public_dict(),
                    }

                def effect_scope():
                    if scope(context) != quest:
                        raise OwnerConflict("asset_quest_scope_invalid")
                    if selector is not None:
                        _verify_target_note_workspace(
                            graph=graph,
                            target_run_agent=target_run_agent,
                            quest_ref=quest,
                            provenance=expected,
                        )
                    if change is not None:
                        scope(context, change["predecessor_version_ref"])
                        for binding in change.get("evidence_bindings", []):
                            scope(context, binding["version_ref"])

                submit = (
                    memory.submit_target_note_intake
                    if selector is not None
                    else memory.submit_asset_intake
                )
                result = submit(
                    request, idempotency_key=effect_key, effect_scope=effect_scope
                )
                return result.as_public_dict()
            version_ref = arguments["version_ref"]
            scope(context, version_ref)
            payload = {
                name: value
                for name, value in arguments.items()
                if name not in {"effect_id", "version_ref"}
            }
            if reconcile:
                result = memory.query_retirement_by_idempotency_key(
                    version_ref, **payload, idempotency_key=effect_key
                )
                return {
                    "status": "not_found" if result is None else "accepted",
                    "result": result,
                }
            return memory.retire_asset_version(
                version_ref,
                **payload,
                idempotency_key=effect_key,
                effect_scope=lambda: scope(context, version_ref)
            )
        except OwnerConflict as error:
            raise SemanticMcpError(error.code, error.details) from error

    operations = []
    for action in ("page", "lifecycle", "current"):
        properties = (
            {
                "query": {"type": "string", "maxLength": 1024},
                "offset": integer,
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            }
            if action == "page"
            else {"version_ref": string}
        )
        operations.append(
            SemanticOperation(
                semantic_operation_id="research_memory.assets." + action,
                owning_module="research_memory",
                description="Discover and read this Quest's accepted asset lifecycle before registering material. Current selection is explicit. Exact historical content.read keeps original bytes and shows correction or retirement explanations.",
                input_schema={
                    "type": "object",
                    "properties": properties,
                    "required": [] if action == "page" else ["version_ref"],
                    "additionalProperties": False,
                },
                output_schema={"type": "object"},
                handler=lambda c, a, action=action: read(c, a, action),
            )
        )
    intake_schema = {
        "type": "object",
        "properties": {
            "source_kind": {"type": "string", "enum": ["text", "file"]},
            "custody_mode": {"type": "string", "enum": ["managed"]},
            "display_name": string,
            "media_type": string,
            "text": {"type": "string", "maxLength": 16000000},
            "target_workspace_note": {
                "type": "object",
                "properties": {
                    "target_ref": string,
                    "target_run_ref": string,
                    "workspace_ref": string,
                    "relative_path": {
                        "type": "string",
                        "enum": sorted(_TARGET_NOTE_PATHS),
                    },
                    "expected_content_hash": {
                        "type": "string",
                        "pattern": "^[0-9a-f]{64}$",
                    },
                },
                "required": [
                    "target_ref",
                    "target_run_ref",
                    "workspace_ref",
                    "relative_path",
                    "expected_content_hash",
                ],
                "additionalProperties": False,
            },
            "provenance": {"type": "object"},
            "asset_ref": string,
            "change": {"type": "object"},
        },
        "required": ["source_kind", "custody_mode", "display_name"],
        "additionalProperties": False,
        "oneOf": [
            {"required": ["text"], "not": {"required": ["target_workspace_note"]}},
            {
                "required": ["target_workspace_note"],
                "allOf": [
                    {"not": {"required": ["text"]}},
                    {"not": {"required": ["provenance"]}},
                ],
            },
        ],
    }
    binding_schema = {
        "type": "object",
        "properties": {
            "asset_ref": string,
            "version_ref": string,
            "content_hash": string,
            "manifest_hash": string,
            "receipt": {
                "type": "object",
                "properties": {
                    name: string
                    for name in (
                        "issuer",
                        "kind",
                        "receipt_ref",
                        "subject_ref",
                        "payload_hash",
                    )
                },
                "required": [
                    "issuer",
                    "kind",
                    "receipt_ref",
                    "subject_ref",
                    "payload_hash",
                ],
            },
        },
        "required": [
            "asset_ref",
            "version_ref",
            "content_hash",
            "manifest_hash",
            "receipt",
        ],
    }
    intake_schema["properties"]["change"] = {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["supplement", "substantive_change", "correction"],
            },
            "predecessor_version_ref": string,
            "expected_revision": integer,
            "explanation": string,
            "error": {
                **string,
                "description": "Required for correction. Describe the actual error.",
            },
            "scope": {
                **string,
                "description": "Required for correction. State the affected scientific scope.",
            },
            "evidence_bindings": {
                "type": "array",
                "minItems": 1,
                "maxItems": 32,
                "items": binding_schema,
                "description": "Required for correction. Use exact accepted RM bindings.",
            },
            "impact": {
                "type": "array",
                "maxItems": 256,
                "items": {
                    "type": "object",
                    "properties": {
                        "work_ref": string,
                        "judgment": {
                            "type": "string",
                            "enum": ["unaffected", "recheck", "redo", "unknown"],
                        },
                        "explanation": string,
                    },
                    "required": ["work_ref", "judgment", "explanation"],
                    "additionalProperties": False,
                },
            },
            "no_affected_work_explanation": {
                **string,
                "description": "Required for correction when impact is omitted or empty. Explain the checked scope and why no work is affected. If impact is uncertain, list the actual work with judgment unknown instead.",
            },
        },
        "required": [
            "kind",
            "predecessor_version_ref",
            "expected_revision",
            "explanation",
        ],
        "additionalProperties": False,
        "allOf": [
            {
                "if": {"properties": {"kind": {"const": "correction"}}},
                "then": {
                    "required": ["error", "scope", "evidence_bindings"],
                    "anyOf": [
                        {
                            "required": ["impact"],
                            "properties": {"impact": {"minItems": 1}},
                        },
                        {"required": ["no_affected_work_explanation"]},
                    ],
                },
            }
        ],
    }
    retirement = {
        "version_ref": string,
        "expected_revision": integer,
        "expected_reference_revision": integer,
        "explanation": string,
        **{
            name: {"type": "boolean"}
            for name in (
                "low_value",
                "obsolete",
                "incorrect",
                "impact_understood",
                "has_explanation_value",
            )
        },
    }
    for action, properties in (
        ("intake", {"intake": intake_schema}),
        ("retire", retirement),
    ):
        name = "research_memory.assets." + action
        properties = {
            **properties,
            "effect_id": {"type": "string", "minLength": 1, "maxLength": 128},
        }
        required = list(properties)
        for reconcile in (False, True):
            operations.append(
                SemanticOperation(
                    semantic_operation_id=name + (".reconcile" if reconcile else ""),
                    owning_module="research_memory",
                    access_mode="reconcile" if reconcile else "effect",
                    reconciliation_operation_id=None
                    if reconcile
                    else name + ".reconcile",
                    description="Accept an asset supplement, substantive change, or correction atomically with its exact predecessor, explanation and current selection. Correction requires error, exact evidence bindings, scope, and per-work recheck/redo/unaffected/unknown judgments; if no work is affected, record no_affected_work_explanation with the checked scope and reason. Retirement requires confirmed low value, obsolescence and error, understood impact and no explanatory value; fresh references and holds block it. Failure, negative results and goal changes never trigger retirement. All historical bytes are retained. Keep the same effect_id and exact payload when reconciling. A queued intake awaits an authorized caller: retry the same effect and payload with the current root scope; background workers cannot accept it without that scope.",
                    input_schema={
                        "type": "object",
                        "properties": properties,
                        "required": required,
                        "additionalProperties": False,
                    },
                    output_schema={"type": "object"},
                    handler=lambda c, a, action=action, reconcile=reconcile: effect(
                        c, a, action, reconcile
                    ),
                )
            )
    return tuple(operations)


def _target_note_provenance(selector: object) -> dict[str, object]:
    required = {
        "target_ref",
        "target_run_ref",
        "workspace_ref",
        "relative_path",
        "expected_content_hash",
    }
    if not isinstance(selector, dict) or set(selector) != required:
        raise OwnerConflict("target_note_selector_invalid")
    if any(
        not isinstance(selector.get(name), str) or not selector[name]
        for name in required
    ):
        raise OwnerConflict("target_note_selector_invalid")
    if (
        selector["relative_path"] not in _TARGET_NOTE_PATHS
        or len(selector["expected_content_hash"]) != 64
        or any(
            character not in "0123456789abcdef"
            for character in selector["expected_content_hash"]
        )
    ):
        raise OwnerConflict("target_note_selector_invalid")
    return {
        "schema_ref": _TARGET_NOTE_SCHEMA,
        "producer_kind": "target_workspace",
        "target_ref": selector["target_ref"],
        "target_run_ref": selector["target_run_ref"],
        "workspace_ref": selector["workspace_ref"],
        "relative_path": selector["relative_path"],
        "source_content_hash": selector["expected_content_hash"],
        "source_locator": (
            "target-workspace://"
            + selector["workspace_ref"]
            + "/"
            + selector["relative_path"]
        ),
    }


def _reject_target_note_provenance(value: object) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise OwnerConflict("asset_provenance_invalid")
    if (
        value.get("schema_ref") == _TARGET_NOTE_SCHEMA
        or bool(set(value) & _TARGET_NOTE_RESERVED_PROVENANCE)
    ):
        raise OwnerConflict("target_note_provenance_reserved")


def _verify_target_note_replay(
    binding: object,
    *,
    values: dict[str, object],
    quest_ref: str,
    provenance: dict[str, object],
) -> None:
    expected_selector = {
        "target_ref": provenance["target_ref"],
        "target_run_ref": provenance["target_run_ref"],
        "workspace_ref": provenance["workspace_ref"],
        "relative_path": provenance["relative_path"],
        "expected_content_hash": provenance["source_content_hash"],
    }
    expected_intake = {
        "source_kind": values.get("source_kind"),
        "custody_mode": values.get("custody_mode"),
        "display_name": str(values.get("display_name", "")).strip(),
        "media_type": str(
            values.get("media_type", "application/octet-stream")
        ).strip(),
        "provenance": provenance,
        "asset_ref": values.get("asset_ref"),
        "asynchronous": False,
        "origin_quest_ref": quest_ref,
        "effect_scope_required": True,
    }
    if binding != {
        "schema_ref": "meta-research/target-note-intake-effect/v1",
        "quest_ref": quest_ref,
        "selector": expected_selector,
        "intake": expected_intake,
    }:
        raise OwnerConflict("asset_intake_idempotency_conflict")


def _verify_target_note_workspace(
    *, graph, target_run_agent, quest_ref: str, provenance: dict[str, object]
) -> Path:
    try:
        graph.verify_target_quest_scope(
            str(provenance["target_ref"]), quest_ref=quest_ref
        )
        workspace, workspace_path = target_run_agent.read_target_workspace_location(
            provenance["target_run_ref"]
        )
    except OwnerConflict as error:
        raise OwnerConflict("target_note_workspace_invalid") from error
    if (
        workspace.target_ref != provenance["target_ref"]
        or workspace.target_run_ref != provenance["target_run_ref"]
        or workspace.workspace_ref != provenance["workspace_ref"]
    ):
        raise OwnerConflict("target_note_workspace_invalid")
    try:
        path = Path(workspace_path)
        if stat.S_ISLNK(path.lstat().st_mode):
            raise OwnerConflict("target_note_symlink_forbidden")
        return path.resolve(strict=True)
    except OwnerConflict:
        raise
    except OSError as error:
        raise OwnerConflict("target_note_workspace_invalid") from error


def _read_target_note(*, graph, target_run_agent, quest_ref: str, selector: object) -> bytes:
    provenance = _target_note_provenance(selector)
    root = _verify_target_note_workspace(
        graph=graph,
        target_run_agent=target_run_agent,
        quest_ref=quest_ref,
        provenance=provenance,
    )
    relative = Path(str(provenance["relative_path"]))
    path = root.joinpath(relative)
    current = root
    for part in relative.parts:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except OSError as error:
            raise OwnerConflict("target_note_source_unavailable") from error
        if stat.S_ISLNK(mode):
            raise OwnerConflict("target_note_symlink_forbidden")
    resolved = path.resolve(strict=True)
    if os.path.commonpath((str(root), str(resolved))) != str(root):
        raise OwnerConflict("target_note_path_invalid")
    try:
        with path.open("rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > 16_000_000:
                raise OwnerConflict("target_note_source_invalid")
            content = source.read(16_000_001)
            after = os.fstat(source.fileno())
    except OwnerConflict:
        raise
    except OSError as error:
        raise OwnerConflict("target_note_source_unavailable") from error
    if (
        len(content) > 16_000_000
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    ):
        raise OwnerConflict("target_note_source_changed")
    try:
        content.decode("utf-8")
    except UnicodeError as error:
        raise OwnerConflict("target_note_utf8_required") from error
    if hashlib.sha256(content).hexdigest() != provenance["source_content_hash"]:
        raise OwnerConflict("target_note_hash_mismatch")
    return content
