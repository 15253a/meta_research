"""Hot-update compatible runtime bindings, preserving core execution identity.

Hot-updatable SKILL.md, prompt instructions, package resources, root capability
profile hashes, and python patch adapter-source hashes may update cleanly
across restarts while core identity (model, harness, schema, MCP bindings,
output schemas, seal keys, and non-hot-updatable capabilities) remains strict.
"""
from typing import TYPE_CHECKING

from meta_research.owners.common import canonical_hash

if TYPE_CHECKING:
    from meta_research.deepfetch import DeepFetchRuntimeBinding

_MCP_BINDING_PREFIX = "harness-operation-binding:semantic-mcp-"
_OPERATION_BINDING_RESOURCE_PREFIX = "harness-artifact:operation-binding-"


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _binding_hashes(values: tuple[str, ...]) -> tuple[str, str] | None:
    selected: dict[str, str] = {}
    for value in values:
        if not value.startswith(_MCP_BINDING_PREFIX) or "@sha256:" not in value:
            continue
        name, digest = value.rsplit("@sha256:", 1)
        if name not in {
            _MCP_BINDING_PREFIX + "catalog",
            _MCP_BINDING_PREFIX + "operation-bindings",
        } or not _is_sha256(digest) or name in selected:
            return None
        selected[name] = value
    if set(selected) != {
        _MCP_BINDING_PREFIX + "catalog",
        _MCP_BINDING_PREFIX + "operation-bindings",
    }:
        return None
    return (
        selected[_MCP_BINDING_PREFIX + "catalog"],
        selected[_MCP_BINDING_PREFIX + "operation-bindings"],
    )


def _normalize_adapter_source_binding(entry: str) -> str:
    if entry.startswith("adapter-source:meta_research.") and "@sha256:" in entry:
        prefix, digest = entry.rsplit("@sha256:", 1)
        if _is_sha256(digest):
            return prefix + "@sha256:hot-update"
    return entry


def _normalize_resource_binding(entry: str) -> str:
    if entry.startswith("package:meta_research.skills") and "@sha256:" in entry:
        prefix, digest = entry.rsplit("@sha256:", 1)
        if _is_sha256(digest):
            return prefix + "@sha256:hot-update"
    return _normalize_adapter_source_binding(entry)


def _normalize_capability_binding(entry: str) -> str:
    if entry.startswith("root-capability-profile:sha256:"):
        digest = entry.removeprefix("root-capability-profile:sha256:")
        if _is_sha256(digest):
            return "root-capability-profile:sha256:hot-update"
    if entry.startswith("deepfetch-v4-skill-bundle-sha256:"):
        digest = entry.removeprefix("deepfetch-v4-skill-bundle-sha256:")
        if _is_sha256(digest):
            return "deepfetch-v4-skill-bundle-sha256:hot-update"
    return _normalize_adapter_source_binding(entry)


def _normalized_stage_binding(binding) -> dict[str, object] | None:
    if (
        not isinstance(getattr(binding, "packaged_skill_bundle_hash", None), str)
        or not binding.packaged_skill_bundle_hash
        or not isinstance(getattr(binding, "instruction_set_hash", None), str)
        or not binding.instruction_set_hash
    ):
        return None
    value = binding.as_dict()
    value["packaged_skill_bundle_hash"] = "hot-update"
    value["instruction_set_hash"] = "hot-update"
    value["capability_bindings"] = [
        _normalize_capability_binding(entry) for entry in binding.capability_bindings
    ]
    value["resource_bindings"] = [
        _normalize_resource_binding(entry) for entry in binding.resource_bindings
    ]
    return value


def _stage_bindings_compatible(expected_type: type, left, right) -> bool:
    if type(left) is not expected_type or type(right) is not expected_type:
        return False
    if left == right:
        return True
    before = _normalized_stage_binding(left)
    after = _normalized_stage_binding(right)
    return before is not None and after is not None and before == after


def stage_bindings_mcp_rebind_compatible(left, right) -> bool:
    """Allow semantic MCP catalog refresh (and hot-updatable prompt/source hashes) at fenced rebind."""

    if type(left) is not type(right):
        return False
    if left == right:
        return True
    if left.schema_ref != right.schema_ref:
        return False
    left_mcp = _binding_hashes(left.mcp_bindings)
    right_mcp = _binding_hashes(right.mcp_bindings)
    if left_mcp is None or right_mcp is None or left_mcp == right_mcp:
        return False

    def stable_resources(values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(
            _normalize_resource_binding(value)
            for value in values
            if not value.startswith(_OPERATION_BINDING_RESOURCE_PREFIX)
        )

    def operation_resources(values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(
            sorted(
                value
                for value in values
                if value.startswith(_OPERATION_BINDING_RESOURCE_PREFIX)
            )
        )

    left_operation_resources = operation_resources(left.resource_bindings)
    right_operation_resources = operation_resources(right.resource_bindings)
    if (
        len(left_operation_resources) != 2
        or len(right_operation_resources) != 2
        or left_operation_resources == right_operation_resources
    ):
        return False

    return (
        bool(left.packaged_skill_bundle_hash)
        and bool(right.packaged_skill_bundle_hash)
        and bool(left.instruction_set_hash)
        and bool(right.instruction_set_hash)
        and left.model_ref == right.model_ref
        and left.harness_adapter_ref == right.harness_adapter_ref
        and tuple(_normalize_capability_binding(v) for v in left.capability_bindings)
        == tuple(_normalize_capability_binding(v) for v in right.capability_bindings)
        and stable_resources(left.resource_bindings)
        == stable_resources(right.resource_bindings)
    )


def idea_bindings_compatible(left, right) -> bool:
    from meta_research.owners.agent_runtime import IdeaRuntimeBinding

    return _stage_bindings_compatible(IdeaRuntimeBinding, left, right)


def plan_bindings_compatible(left, right) -> bool:
    from meta_research.owners.agent_runtime import PlanRuntimeBinding

    return _stage_bindings_compatible(PlanRuntimeBinding, left, right)


def bundle_bindings_compatible(left, right) -> bool:
    from meta_research.owners.agent_runtime import BundleRuntimeBinding

    return _stage_bindings_compatible(BundleRuntimeBinding, left, right)


def reasoning_bindings_compatible(left, right) -> bool:
    from meta_research.owners.agent_runtime import ReasoningRuntimeBinding

    return _stage_bindings_compatible(ReasoningRuntimeBinding, left, right)


def deepfetch_bindings_compatible(
    left: "DeepFetchRuntimeBinding", right: "DeepFetchRuntimeBinding"
) -> bool:
    from meta_research.deepfetch import DeepFetchRuntimeBinding

    if type(left) is not DeepFetchRuntimeBinding or type(right) is not DeepFetchRuntimeBinding:
        return False
    if left == right:
        return True
    return (
        left.provider_ref == right.provider_ref
        and left.provider_version == right.provider_version
        and left.model_ref == right.model_ref
        and left.harness_ref == right.harness_ref
        and tuple(_normalize_capability_binding(entry) for entry in left.capability_bindings)
        == tuple(_normalize_capability_binding(entry) for entry in right.capability_bindings)
    )


def root_capability_profile_compatible(
    persisted_profile: object,
    persisted_hash: object,
    expected_profile: dict[str, object],
) -> bool:
    if (
        not isinstance(persisted_profile, dict)
        or not _is_sha256(persisted_hash)
        or canonical_hash(persisted_profile) != persisted_hash
    ):
        return False
    if persisted_profile == expected_profile:
        return True
    if set(persisted_profile) != set(expected_profile):
        return False
    if not _is_sha256(persisted_profile.get("research_system_prompt_hash")):
        return False
    return all(
        persisted_profile[key] == expected_profile[key]
        for key in expected_profile
        if key != "research_system_prompt_hash"
    )
