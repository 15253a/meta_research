from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from meta_research.owners.common import canonical_hash


RootAgentKind = Literal[
    "deepfetch",
    "acquisition",
    "companion",
    "idea",
    "plan",
    "bundle",
    "target",
    "reasoning",
    "writing",
]

ROOT_AGENT_KINDS: tuple[RootAgentKind, ...] = (
    "deepfetch",
    "acquisition",
    "companion",
    "idea",
    "plan",
    "bundle",
    "target",
    "reasoning",
    "writing",
)

RootCapabilityEntryPath = Literal[
    "initial",
    "wake",
    "resume",
    "successor",
    "recovery",
]

ROOT_CAPABILITY_ENTRY_PATHS: tuple[RootCapabilityEntryPath, ...] = (
    "initial",
    "wake",
    "resume",
    "successor",
    "recovery",
)

ROOT_CAPABILITY_PROFILE_SCHEMA = "meta-research/root-capability-profile/v3"
ROOT_CAPABILITY_DIAGNOSTIC_SCHEMA = (
    "meta-research/root-capability-diagnostic/v1"
)
CODEX_FEATURE_INVENTORY_TIMEOUT_SECONDS = 2.0

# This is an availability floor, not a per-turn usage checklist.  A Root may
# choose none of these tools in a turn and still have a conforming launch.
ROOT_CAPABILITY_FLOOR = (
    "tool_inventory",
    "shell",
    "file_access",
    "semantic_mcp",
    "skill",
    "plugin",
    "hook",
    "subagent",
    "stream",
    "native_session",
    "fork",
    "steer",
    "interrupt",
    "resume",
    "web_search",
    "web_fetch",
)

_CODEX_ROOT_FEATURES = (
    "multi_agent",
    "plugins",
    "remote_plugin",
    "hooks",
)

CODEX_ROOT_FEATURE_INVENTORY_NAMES = (
    "hooks",
    "multi_agent",
    "plugins",
    "remote_plugin",
    "shell_tool",
    "skill_search",
    "unified_exec",
)

ROOT_ROLE_OPERATION_DELTAS: dict[RootAgentKind, tuple[str, ...]] = {
    "deepfetch": (),
    "acquisition": (),
    "companion": (),
    "idea": (),
    "plan": (),
    "bundle": (
        "advancement_engine.bundle_stage_run.observe",
        "advancement_engine.bundle_exhaustion.submit",
        "advancement_engine.bundle_exhaustion.reconcile",
        "agent_runtime.bundle_run_binding.observe",
        "research_memory.implementation_content.accept",
        "research_memory.implementation_content.accept.reconcile",
        "research_memory.implementation_content.read",
        "research_graph.reuse_eligibility.read",
        "research_graph.reuse_inputs.verify",
        "research_graph.target_launch_request.read",
        "agent_runtime.target_work.request",
        "agent_runtime.target_work.request.reconcile",
        "agent_runtime.target_frontier.read",
        "agent_runtime.bundle_inbox.read",
    ),
    "target": ("agent_runtime.target_run.observe",),
    "reasoning": (
        "advancement_engine.reasoning_stage_run.observe",
        "research_memory.reasoning_evidence.read",
        "research_graph.reasoning_context.read",
    ),
    "writing": (),
}


@dataclass(frozen=True)
class RootCapabilityProfile:
    root_kind: RootAgentKind
    capabilities: tuple[str, ...] = ROOT_CAPABILITY_FLOOR
    multi_agent_enabled: bool = True
    web_search_mode: Literal["live"] = "live"

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_ref": ROOT_CAPABILITY_PROFILE_SCHEMA,
            "capabilities": list(self.capabilities),
            "enabled_codex_features": list(_CODEX_ROOT_FEATURES),
            "multi_agent_enabled": self.multi_agent_enabled,
            "web_search_mode": self.web_search_mode,
        }

    @property
    def digest(self) -> str:
        return canonical_hash(self.as_dict())

    def codex_arguments(
        self,
        *,
        entry_path: RootCapabilityEntryPath = "initial",
    ) -> tuple[str, ...]:
        """Return the common real CLI switches owned by this profile.

        The entry path is validated but deliberately does not alter the
        capability configuration.  Role and lifecycle callers may add resource
        or side-effect authorization overlays, never remove ordinary tools.
        """

        if entry_path not in ROOT_CAPABILITY_ENTRY_PATHS:
            raise ValueError("root_capability_entry_path_invalid")
        return tuple(
            value
            for feature in _CODEX_ROOT_FEATURES
            for value in ("--enable", feature)
        ) + (
            "--config",
            'web_search="live"',
        )

    def runtime_bindings(self) -> tuple[str, ...]:
        return (
            "root-capability-floor:v3",
            "root-capability-profile:sha256:" + self.digest,
            "codex-config:features.hooks=true",
            "codex-config:features.multi_agent=true",
            "codex-config:features.plugins=true",
            "codex-config:features.remote_plugin=true",
            "codex-config:web_search=live",
            "file-access-enabled",
            "hooks-enabled",
            "native-session-controls-enabled",
            "native-subagent-enabled",
            "plugins-enabled",
            "semantic-mcp-configurable",
            "shell-tool-enabled",
            "skills-enabled",
            "stream-events-enabled",
            "tool-inventory-enabled",
            "web-fetch-enabled",
            # Retain the established binding vocabulary while all consumers
            # migrate to the canonical profile hash.
            "web-fetch-live",
            "web-search-live",
        )

    def public_diagnostics(
        self,
        *,
        entry_path: RootCapabilityEntryPath = "initial",
        available_capabilities: tuple[str, ...] = (),
        used_capabilities: tuple[str, ...] = (),
        usage_observed: bool = True,
        authorized_operation_ids: tuple[str, ...] = (),
        unavailable_capabilities: Mapping[str, str] | None = None,
        usage_evidence_refs: Mapping[str, tuple[str, ...]] | None = None,
        tool_inventory_evidence_refs: tuple[str, ...] = (),
        tool_inventory_names: tuple[str, ...] = (),
        provider_feature_inventory: Mapping[str, bool] | None = None,
        provider_feature_inventory_evidence_refs: tuple[str, ...] = (),
    ) -> dict[str, object]:
        """Expose observed availability, turn usage, and grants independently.

        A configured profile is not evidence that a provider actually exposed
        every facility.  Callers therefore supply positive inventory/use
        evidence and explicit unsupported facts; everything else remains
        ``availability_not_observed`` instead of being promoted to available.
        """

        unavailable = dict(unavailable_capabilities or {})
        evidence = dict(usage_evidence_refs or {})
        provider_features = dict(provider_feature_inventory or {})
        capability_set = set(self.capabilities)
        if (
            entry_path not in ROOT_CAPABILITY_ENTRY_PATHS
            or len(available_capabilities)
            != len(set(available_capabilities))
            or not set(available_capabilities) <= capability_set
            or len(used_capabilities) != len(set(used_capabilities))
            or not set(used_capabilities) <= capability_set
            or type(usage_observed) is not bool
            or not set(unavailable) <= capability_set
            or bool(set(available_capabilities) & set(unavailable))
            or bool(set(used_capabilities) & set(unavailable))
            or any(not code or len(code) > 128 for code in unavailable.values())
            or not set(evidence) <= capability_set
            or any(
                not isinstance(refs, tuple)
                or len(refs) != len(set(refs))
                or any(not ref or len(ref) > 512 for ref in refs)
                for refs in evidence.values()
            )
            or len(authorized_operation_ids)
            != len(set(authorized_operation_ids))
            or any(
                not operation_id or len(operation_id) > 256
                for operation_id in authorized_operation_ids
            )
            or len(tool_inventory_evidence_refs)
            != len(set(tool_inventory_evidence_refs))
            or any(
                not ref or len(ref) > 512
                for ref in tool_inventory_evidence_refs
            )
            or len(tool_inventory_names) != len(set(tool_inventory_names))
            or any(
                not name or len(name) > 256 for name in tool_inventory_names
            )
            or any(
                not isinstance(name, str)
                or not name
                or len(name) > 128
                or type(enabled) is not bool
                for name, enabled in provider_features.items()
            )
            or len(provider_feature_inventory_evidence_refs)
            != len(set(provider_feature_inventory_evidence_refs))
            or bool(provider_feature_inventory_evidence_refs)
            != bool(provider_features)
            or any(
                not ref or len(ref) > 512
                for ref in provider_feature_inventory_evidence_refs
            )
        ):
            raise ValueError("root_capability_diagnostic_invalid")
        used = set(used_capabilities)
        available = set(available_capabilities) | used
        return {
            "schema_ref": ROOT_CAPABILITY_DIAGNOSTIC_SCHEMA,
            "root_kind": self.root_kind,
            "entry_path": entry_path,
            "capability_profile_hash": self.digest,
            "availability": {
                capability: (
                    {
                        "status": "capability_unavailable",
                        "reason": {"code": unavailable[capability]},
                    }
                    if capability in unavailable
                    else (
                        {"status": "available"}
                        if capability in available
                        else {"status": "availability_not_observed"}
                    )
                )
                for capability in self.capabilities
            },
            "usage": {
                capability: {
                    "status": (
                        "used"
                        if capability in used
                        else (
                            "not_used"
                            if usage_observed
                            else "usage_not_observed"
                        )
                    ),
                    "evidence_refs": list(evidence.get(capability, ())),
                }
                for capability in self.capabilities
            },
            "side_effect_authorization": {
                "status": "operation_local",
                "operation_ids": list(authorized_operation_ids),
            },
            "tool_inventory": {
                "status": (
                    "observed"
                    if tool_inventory_evidence_refs or tool_inventory_names
                    else "not_reported"
                ),
                "evidence_refs": list(tool_inventory_evidence_refs),
                "names": list(tool_inventory_names),
            },
            # Codex 0.147 exec JSONL does not expose the model-visible tool
            # list. Keep its real, separately probed feature inventory apart
            # from both tool inventory and turn usage instead of fabricating a
            # ``thread.started.tools`` event.
            "provider_feature_inventory": {
                "status": "observed" if provider_features else "not_reported",
                "evidence_refs": list(
                    provider_feature_inventory_evidence_refs
                ),
                "features": provider_features,
            },
        }


def root_capability_profile(root_kind: RootAgentKind) -> RootCapabilityProfile:
    if root_kind not in ROOT_AGENT_KINDS:
        raise ValueError("root_agent_kind_invalid")
    return RootCapabilityProfile(root_kind=root_kind)


def parse_codex_feature_inventory(value: str) -> dict[str, bool] | None:
    """Parse the stable human-readable ``codex features list`` surface."""

    observed: dict[str, bool] = {}
    for raw_line in value.splitlines():
        fields = raw_line.split()
        if len(fields) < 3 or fields[-1] not in {"true", "false"}:
            continue
        name = fields[0]
        if name in CODEX_ROOT_FEATURE_INVENTORY_NAMES:
            observed[name] = fields[-1] == "true"
    if set(observed) != set(CODEX_ROOT_FEATURE_INVENTORY_NAMES):
        return None
    return {
        name: observed[name] for name in CODEX_ROOT_FEATURE_INVENTORY_NAMES
    }


def capabilities_from_codex_feature_inventory(
    features: Mapping[str, bool],
) -> tuple[set[str], dict[str, str]]:
    """Project provider feature facts without calling them tool inventory."""

    if not features:
        return set(), {}
    if set(features) != set(CODEX_ROOT_FEATURE_INVENTORY_NAMES) or any(
        type(enabled) is not bool for enabled in features.values()
    ):
        raise ValueError("codex_feature_inventory_invalid")
    # ``skill_search`` describes discovery, not whether the selected Skill
    # package itself reached the model-visible prompt. Preserve that fact in
    # the provider inventory but do not overclaim the broader ``skill`` floor.
    checks = {
        "shell": features["shell_tool"] or features["unified_exec"],
        "file_access": features["shell_tool"] or features["unified_exec"],
        "plugin": features["plugins"] and features["remote_plugin"],
        "hook": features["hooks"],
        "subagent": features["multi_agent"],
    }
    return (
        {capability for capability, enabled in checks.items() if enabled},
        {
            capability: f"codex_feature_{capability}_disabled"
            for capability, enabled in checks.items()
            if not enabled
        },
    )


def codex_feature_inventory_evidence_ref(
    *,
    profile: RootCapabilityProfile,
    entry_path: RootCapabilityEntryPath,
    provider_version: str,
    features: Mapping[str, bool],
) -> str:
    if entry_path not in ROOT_CAPABILITY_ENTRY_PATHS or not provider_version:
        raise ValueError("codex_feature_inventory_invalid")
    capabilities_from_codex_feature_inventory(features)
    return "harness_provider_feature_inventory:" + canonical_hash(
        {
            "provider_version": provider_version,
            "capability_profile_hash": profile.digest,
            "entry_path": entry_path,
            "features": dict(features),
        }
    )


def codex_feature_diagnostics(
    *,
    profile: RootCapabilityProfile,
    entry_path: RootCapabilityEntryPath,
    provider_version: str,
    feature_output: str | None,
    authorized_operation_ids: tuple[str, ...] = (),
    semantic_mcp_available: bool = False,
) -> dict[str, object]:
    """Build truthful pre-turn diagnostics from a real provider feature probe."""

    features = (
        None
        if feature_output is None
        else parse_codex_feature_inventory(feature_output)
    )
    if features is None:
        return profile.public_diagnostics(
            entry_path=entry_path,
            usage_observed=False,
            authorized_operation_ids=authorized_operation_ids,
        )
    available, unavailable = capabilities_from_codex_feature_inventory(
        features
    )
    if semantic_mcp_available:
        available.add("semantic_mcp")
    evidence_ref = codex_feature_inventory_evidence_ref(
        profile=profile,
        entry_path=entry_path,
        provider_version=provider_version,
        features=features,
    )
    return profile.public_diagnostics(
        entry_path=entry_path,
        available_capabilities=tuple(
            capability
            for capability in profile.capabilities
            if capability in available
        ),
        authorized_operation_ids=authorized_operation_ids,
        usage_observed=False,
        unavailable_capabilities=unavailable,
        provider_feature_inventory=features,
        provider_feature_inventory_evidence_refs=(evidence_ref,),
    )


def codex_feature_diagnostics_match(
    value: object,
    *,
    profile: RootCapabilityProfile,
    entry_path: RootCapabilityEntryPath,
    provider_version: str,
    authorized_operation_ids: tuple[str, ...] = (),
    semantic_mcp_available: bool = False,
) -> bool:
    """Revalidate sealed diagnostics without rerunning the provider probe."""

    if not isinstance(value, dict):
        return False
    inventory = value.get("provider_feature_inventory")
    if not isinstance(inventory, dict):
        return False
    status = inventory.get("status")
    if status == "not_reported":
        expected = codex_feature_diagnostics(
            profile=profile,
            entry_path=entry_path,
            provider_version=provider_version,
            feature_output=None,
            authorized_operation_ids=authorized_operation_ids,
            semantic_mcp_available=semantic_mcp_available,
        )
        return value == expected
    features = inventory.get("features")
    if status != "observed" or not isinstance(features, dict):
        return False
    try:
        available, unavailable = capabilities_from_codex_feature_inventory(
            features
        )
        if semantic_mcp_available:
            available.add("semantic_mcp")
        evidence_ref = codex_feature_inventory_evidence_ref(
            profile=profile,
            entry_path=entry_path,
            provider_version=provider_version,
            features=features,
        )
        expected = profile.public_diagnostics(
            entry_path=entry_path,
            available_capabilities=tuple(
                capability
                for capability in profile.capabilities
                if capability in available
            ),
            authorized_operation_ids=authorized_operation_ids,
            usage_observed=False,
            unavailable_capabilities=unavailable,
            provider_feature_inventory=features,
            provider_feature_inventory_evidence_refs=(evidence_ref,),
        )
    except ValueError:
        return False
    return value == expected


def validate_root_capability_diagnostics(
    value: object,
    *,
    root_kind: RootAgentKind | None = None,
) -> dict[str, object]:
    """Validate one diagnostic by reconstructing the canonical projection.

    Diagnostics are intentionally independent from provider/domain results, so
    the public sidecar needs its own strict validation boundary.  Rebuilding
    the document through ``public_diagnostics`` keeps availability, usage, and
    operation authorization separate instead of accepting an arbitrary dict.
    """

    if not isinstance(value, dict):
        raise ValueError("root_capability_diagnostic_invalid")
    observed_kind = value.get("root_kind")
    if (
        observed_kind not in ROOT_AGENT_KINDS
        or root_kind is not None
        and observed_kind != root_kind
        or set(value)
        != {
            "schema_ref",
            "root_kind",
            "entry_path",
            "capability_profile_hash",
            "availability",
            "usage",
            "side_effect_authorization",
            "tool_inventory",
            "provider_feature_inventory",
        }
    ):
        raise ValueError("root_capability_diagnostic_invalid")
    profile = root_capability_profile(observed_kind)
    entry_path = value.get("entry_path")
    availability = value.get("availability")
    usage = value.get("usage")
    authorization = value.get("side_effect_authorization")
    tool_inventory = value.get("tool_inventory")
    provider_inventory = value.get("provider_feature_inventory")
    if (
        value.get("schema_ref") != ROOT_CAPABILITY_DIAGNOSTIC_SCHEMA
        or value.get("capability_profile_hash") != profile.digest
        or entry_path not in ROOT_CAPABILITY_ENTRY_PATHS
        or not isinstance(availability, dict)
        or set(availability) != set(profile.capabilities)
        or not isinstance(usage, dict)
        or set(usage) != set(profile.capabilities)
        or not isinstance(authorization, dict)
        or set(authorization) != {"status", "operation_ids"}
        or authorization.get("status") != "operation_local"
        or not isinstance(authorization.get("operation_ids"), list)
        or not isinstance(tool_inventory, dict)
        or set(tool_inventory) != {"status", "evidence_refs", "names"}
        or tool_inventory.get("status") not in {"observed", "not_reported"}
        or not isinstance(tool_inventory.get("evidence_refs"), list)
        or not isinstance(tool_inventory.get("names"), list)
        or not isinstance(provider_inventory, dict)
        or set(provider_inventory)
        != {"status", "evidence_refs", "features"}
        or provider_inventory.get("status") not in {"observed", "not_reported"}
        or not isinstance(provider_inventory.get("evidence_refs"), list)
        or not isinstance(provider_inventory.get("features"), dict)
    ):
        raise ValueError("root_capability_diagnostic_invalid")

    available_capabilities: list[str] = []
    unavailable_capabilities: dict[str, str] = {}
    used_capabilities: list[str] = []
    usage_evidence_refs: dict[str, tuple[str, ...]] = {}
    usage_observed: bool | None = None
    for capability in profile.capabilities:
        availability_item = availability.get(capability)
        usage_item = usage.get(capability)
        if not isinstance(availability_item, dict) or not isinstance(
            usage_item, dict
        ):
            raise ValueError("root_capability_diagnostic_invalid")
        availability_status = availability_item.get("status")
        if availability_status == "available":
            if set(availability_item) != {"status"}:
                raise ValueError("root_capability_diagnostic_invalid")
            available_capabilities.append(capability)
        elif availability_status == "availability_not_observed":
            if set(availability_item) != {"status"}:
                raise ValueError("root_capability_diagnostic_invalid")
        elif availability_status == "capability_unavailable":
            reason = availability_item.get("reason")
            if (
                set(availability_item) != {"status", "reason"}
                or not isinstance(reason, dict)
                or set(reason) != {"code"}
                or not isinstance(reason.get("code"), str)
            ):
                raise ValueError("root_capability_diagnostic_invalid")
            unavailable_capabilities[capability] = reason["code"]
        else:
            raise ValueError("root_capability_diagnostic_invalid")

        if set(usage_item) != {"status", "evidence_refs"} or not isinstance(
            usage_item.get("evidence_refs"), list
        ):
            raise ValueError("root_capability_diagnostic_invalid")
        usage_status = usage_item.get("status")
        if usage_status == "used":
            used_capabilities.append(capability)
        elif usage_status == "not_used":
            if usage_observed is False:
                raise ValueError("root_capability_diagnostic_invalid")
            usage_observed = True
        elif usage_status == "usage_not_observed":
            if usage_observed is True:
                raise ValueError("root_capability_diagnostic_invalid")
            usage_observed = False
        else:
            raise ValueError("root_capability_diagnostic_invalid")
        usage_evidence_refs[capability] = tuple(usage_item["evidence_refs"])

    if usage_observed is None:
        usage_observed = True
    operation_ids = tuple(authorization["operation_ids"])
    inventory_evidence_refs = tuple(tool_inventory["evidence_refs"])
    inventory_names = tuple(tool_inventory["names"])
    provider_evidence_refs = tuple(provider_inventory["evidence_refs"])
    provider_features = provider_inventory["features"]
    try:
        expected = profile.public_diagnostics(
            entry_path=entry_path,
            available_capabilities=tuple(available_capabilities),
            used_capabilities=tuple(used_capabilities),
            usage_observed=usage_observed,
            authorized_operation_ids=operation_ids,
            unavailable_capabilities=unavailable_capabilities,
            usage_evidence_refs=usage_evidence_refs,
            tool_inventory_evidence_refs=inventory_evidence_refs,
            tool_inventory_names=inventory_names,
            provider_feature_inventory=provider_features,
            provider_feature_inventory_evidence_refs=provider_evidence_refs,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("root_capability_diagnostic_invalid") from error
    if value != expected:
        raise ValueError("root_capability_diagnostic_invalid")
    return expected


def project_codex_post_turn_diagnostics(
    pre_turn: object,
    stdout: str,
    *,
    semantic_mcp_available: bool = False,
) -> dict[str, object]:
    """Merge a sealed pre-turn probe with actual Codex JSONL turn usage.

    Codex 0.147 does not report a complete model-visible tool inventory.  This
    projector therefore preserves ``tool_inventory=not_reported`` while still
    publishing what the same provider Session demonstrably used.  A successful
    required MCP startup is availability evidence, never evidence of a call.
    """

    diagnostic = validate_root_capability_diagnostics(pre_turn)
    root_kind = diagnostic["root_kind"]
    profile = root_capability_profile(root_kind)
    entry_path = diagnostic["entry_path"]
    availability = diagnostic["availability"]
    authorization = diagnostic["side_effect_authorization"]
    tool_inventory = diagnostic["tool_inventory"]
    provider_inventory = diagnostic["provider_feature_inventory"]
    assert isinstance(availability, dict)
    assert isinstance(authorization, dict)
    assert isinstance(tool_inventory, dict)
    assert isinstance(provider_inventory, dict)

    available = {
        capability
        for capability, item in availability.items()
        if isinstance(item, dict) and item.get("status") == "available"
    }
    unavailable = {
        capability: str(item["reason"]["code"])
        for capability, item in availability.items()
        if isinstance(item, dict)
        and item.get("status") == "capability_unavailable"
        and isinstance(item.get("reason"), dict)
    }
    used: set[str] = set()
    evidence: dict[str, list[str]] = {
        capability: [] for capability in profile.capabilities
    }
    event_refs: list[str] = []
    terminal = False
    native_session_refs: list[str] = []
    for raw_line in stdout.splitlines():
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            continue
        event_ref = "codex_turn_event:" + canonical_hash(event)
        event_refs.append(event_ref)
        event_type = event["type"]
        capabilities = _codex_event_capabilities(event)
        if event_type == "thread.started":
            thread_id = event.get("thread_id")
            if isinstance(thread_id, str) and thread_id:
                capabilities.add("native_session")
                native_session_refs.append(thread_id)
        if event_type == "turn.completed":
            terminal = True
        for capability in capabilities:
            if capability in evidence:
                used.add(capability)
                evidence[capability].append(event_ref)

    if terminal and len(event_refs) >= 2:
        used.add("stream")
        evidence["stream"].extend((event_refs[0], event_refs[-1]))
    if terminal and entry_path != "initial" and native_session_refs:
        used.add("resume")
        evidence["resume"].append(
            "codex_resume_entry:" + canonical_hash(
                {
                    "entry_path": entry_path,
                    "native_session_ref": native_session_refs[-1],
                }
            )
        )
    available.update(used)
    # Callers invoke this projector only after the provider operation returns.
    # A required MCP server therefore initialized successfully even if the
    # lossy JSONL omitted its terminal marker. This is availability, not use.
    if semantic_mcp_available:
        available.add("semantic_mcp")
    if entry_path != "initial":
        available.add("resume")
    unavailable = {
        capability: code
        for capability, code in unavailable.items()
        if capability not in available
    }
    return profile.public_diagnostics(
        entry_path=entry_path,
        available_capabilities=tuple(
            capability
            for capability in profile.capabilities
            if capability in available
        ),
        used_capabilities=tuple(
            capability for capability in profile.capabilities if capability in used
        ),
        usage_observed=terminal,
        authorized_operation_ids=tuple(authorization["operation_ids"]),
        unavailable_capabilities=unavailable,
        usage_evidence_refs={
            capability: tuple(dict.fromkeys(refs))
            for capability, refs in evidence.items()
        },
        tool_inventory_evidence_refs=tuple(tool_inventory["evidence_refs"]),
        tool_inventory_names=tuple(tool_inventory["names"]),
        provider_feature_inventory=provider_inventory["features"],
        provider_feature_inventory_evidence_refs=tuple(
            provider_inventory["evidence_refs"]
        ),
    )


def _codex_event_capabilities(event: Mapping[str, object]) -> set[str]:
    event_type = event.get("type")
    lifecycle = {
        "thread.forked": "fork",
        "turn.steered": "steer",
        "turn.interrupted": "interrupt",
        "thread.resumed": "resume",
    }
    capabilities = (
        {lifecycle[event_type]}
        if isinstance(event_type, str) and event_type in lifecycle
        else set()
    )
    if event_type != "item.completed":
        return capabilities
    item = event.get("item")
    if not isinstance(item, dict):
        return capabilities
    item_type = str(item.get("type", "")).casefold()
    tool = str(item.get("tool", "")).casefold()
    server = str(item.get("server", "")).casefold()
    if item_type in {"command_execution", "bash", "shell"} or tool in {
        "bash",
        "shell",
        "exec_command",
    }:
        capabilities.add("shell")
    if item_type in {"file_change", "read", "write", "edit"} or tool in {
        "read",
        "write",
        "edit",
        "apply_patch",
    }:
        capabilities.add("file_access")
    if (
        item_type == "mcp_tool_call"
        and server == "meta_research"
        or item_type.startswith("mcp__meta_research__")
        or tool.startswith("mcp__meta_research__")
    ):
        capabilities.add("semantic_mcp")
    if item_type == "skill" or tool == "skill":
        capabilities.add("skill")
    if (
        item_type == "plugin"
        or tool == "plugin"
        or item_type.startswith("plugin__")
        or tool.startswith("plugin__")
    ):
        capabilities.add("plugin")
    if item_type == "hook" or tool == "hook":
        capabilities.add("hook")
    if item_type == "collab_tool_call" or tool in {
        "spawn_agent",
        "wait",
        "send_message",
        "followup_task",
        "interrupt_agent",
    }:
        capabilities.add("subagent")
    if item_type in {"web_search", "websearch"}:
        action = item.get("action")
        action_type = (
            str(action.get("type", "")).casefold()
            if isinstance(action, dict)
            else ""
        )
        query = item.get("query")
        if action_type == "search":
            capabilities.add("web_search")
        elif action_type in {"open", "open_page", "fetch"} or (
            action_type == "other"
            and isinstance(query, str)
            and (not query or query.startswith(("http://", "https://")))
        ):
            capabilities.add("web_fetch")
    elif item_type in {"web_fetch", "webfetch"} or tool in {
        "web_fetch",
        "webfetch",
    }:
        capabilities.add("web_fetch")
    return capabilities


def merge_root_capability_bindings(
    existing: tuple[str, ...], root_kind: RootAgentKind
) -> tuple[str, ...]:
    """Append the canonical floor without changing adapter-specific ordering."""

    merged = list(existing)
    for binding in root_capability_profile(root_kind).runtime_bindings():
        if binding not in merged:
            merged.append(binding)
    return tuple(merged)


def root_operation_catalog(
    root_kind: RootAgentKind,
    *,
    common_operation_ids: tuple[str, ...],
) -> tuple[str, ...]:
    """Compose a Root grant from the common catalog and one role delta.

    Registration and handler availability remain the live gateway's concern.
    Keeping the common tuple as an explicit input lets later effect tickets
    activate their operations without duplicating the role-specific catalog.
    """

    if root_kind not in ROOT_AGENT_KINDS:
        raise ValueError("root_agent_kind_invalid")
    if (
        not common_operation_ids
        or len(common_operation_ids) != len(set(common_operation_ids))
        or any(
            not operation_id or len(operation_id) > 256
            for operation_id in common_operation_ids
        )
    ):
        raise ValueError("root_common_operation_catalog_invalid")
    delta = ROOT_ROLE_OPERATION_DELTAS[root_kind]
    overlap = set(common_operation_ids) & set(delta)
    if overlap:
        raise ValueError("root_operation_catalog_overlap")
    # Preserve the established role-specific ordering while giving every
    # caller one common suffix seam to extend as effects are registered.
    return (*delta, *common_operation_ids)


__all__ = [
    "ROOT_AGENT_KINDS",
    "ROOT_CAPABILITY_DIAGNOSTIC_SCHEMA",
    "CODEX_FEATURE_INVENTORY_TIMEOUT_SECONDS",
    "ROOT_CAPABILITY_ENTRY_PATHS",
    "ROOT_CAPABILITY_FLOOR",
    "ROOT_CAPABILITY_PROFILE_SCHEMA",
    "ROOT_ROLE_OPERATION_DELTAS",
    "RootAgentKind",
    "RootCapabilityEntryPath",
    "RootCapabilityProfile",
    "merge_root_capability_bindings",
    "root_capability_profile",
    "root_operation_catalog",
]
