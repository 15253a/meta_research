"""Exact runtime identities, with explicit, recorded refresh seams.

Only SKILL.md and contract.md can change within the same execution identity.
Historical deployments require their original installation unless an Owner
creates a new stage request at a fenced suspend/rebind boundary.  That seam
may adopt only a current Semantic MCP catalog and operation-binding hash.
It does not authorize arbitrary resource, model, or capability changes.
"""
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from meta_research.owners.agent_runtime import BundleRuntimeBinding, ReasoningRuntimeBinding

_BUNDLE_SOURCE = "adapter-source:meta_research.bundle_skill@sha256:"
_SUPERVISOR_SOURCE = "adapter-source:meta_research.provider_supervisor@sha256:"
_RECOVERY_SOURCE = "adapter-source:meta_research.bundle_dispatch_recovery@sha256:"
_POLICY_CONTRACT = "bundle-execution-contract:policy-refresh/v1"
_POLICY_PACKAGE = "package:meta_research.skills.bundle_stage/"
_POLICY_RESOURCES = ("SKILL.md", "references/contract.md")
_MCP_BINDING_PREFIX = "harness-operation-binding:semantic-mcp-"
_OPERATION_BINDING_RESOURCE_PREFIX = "harness-artifact:operation-binding-"


def _single_resource(binding: "BundleRuntimeBinding", prefix: str) -> str | None:
    matches = [entry for entry in binding.resource_bindings if entry.startswith(prefix)]
    return matches[0] if len(matches) == 1 else None


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


def stage_bindings_mcp_rebind_compatible(left, right) -> bool:
    """Allow only a current MCP catalog refresh at a fenced stage rebind.

    A new Owner stage request is the authorization for this comparison.  The
    persisted binding remains exact until that request is accepted.  Every
    execution identity, capability and non-MCP resource stays byte-for-byte
    equal.  The only replaceable entries are the resident MCP hashes and the
    operation-binding contract/set artifacts they describe.
    """

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
            value
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
    # The operation contract and set artifacts are derived from the same
    # current authority as the two MCP hashes.  Require both artifact values
    # to move with those hashes so a caller cannot smuggle an unrelated valid
    # digest through this migration seam.
    if (
        len(left_operation_resources) != 2
        or len(right_operation_resources) != 2
        or left_operation_resources == right_operation_resources
    ):
        return False

    return (
        left.packaged_skill_bundle_hash == right.packaged_skill_bundle_hash
        and left.instruction_set_hash == right.instruction_set_hash
        and left.model_ref == right.model_ref
        and left.harness_adapter_ref == right.harness_adapter_ref
        and left.capability_bindings == right.capability_bindings
        and stable_resources(left.resource_bindings)
        == stable_resources(right.resource_bindings)
    )


def _same_execution_policy_value(binding: "BundleRuntimeBinding") -> dict[str, object] | None:
    """Compare prose refreshes without registering each unchanged code revision."""
    contracts = [entry for entry in binding.resource_bindings if entry.startswith("bundle-execution-contract:")]
    if contracts != [_POLICY_CONTRACT]:
        return None
    if not _is_sha256(binding.instruction_set_hash) or not _is_sha256(binding.packaged_skill_bundle_hash):
        return None
    for prefix in (_BUNDLE_SOURCE, _SUPERVISOR_SOURCE, _RECOVERY_SOURCE):
        entry = _single_resource(binding, prefix)
        if entry is None or not _is_sha256(entry[len(prefix):]):
            return None
    replacements = {}
    for name in _POLICY_RESOURCES:
        prefix = f"{_POLICY_PACKAGE}{name}@sha256:"
        entry = _single_resource(binding, prefix)
        if entry is None or not _is_sha256(entry[len(prefix):]):
            return None
        replacements[entry] = prefix + "revisable-policy-v1"
    value = binding.as_dict()
    value["instruction_set_hash"] = "bundle-stage-revisable-policy/v1"
    value["packaged_skill_bundle_hash"] = "bundle-stage-revisable-policy/v1"
    # Executable identities and every non-policy resource remain exact. The
    # current policy exception does not authorize deployment migrations.
    value["resource_bindings"] = [
        replacements.get(entry, entry) for entry in binding.resource_bindings
    ]
    return value


def bundle_bindings_compatible(left: "BundleRuntimeBinding", right: "BundleRuntimeBinding") -> bool:
    from meta_research.owners.agent_runtime import BundleRuntimeBinding

    if type(left) is not BundleRuntimeBinding or type(right) is not BundleRuntimeBinding:
        return False
    if left == right:
        return True
    before = _same_execution_policy_value(left)
    after = _same_execution_policy_value(right)
    return before is not None and after is not None and before == after


def reasoning_bindings_compatible(
    left: "ReasoningRuntimeBinding", right: "ReasoningRuntimeBinding"
) -> bool:
    from meta_research.owners.agent_runtime import ReasoningRuntimeBinding

    return type(left) is ReasoningRuntimeBinding and type(right) is ReasoningRuntimeBinding and left == right
