"""Exact runtime identities, with the current Bundle policy prose exception.

Only SKILL.md and contract.md can change within the same execution identity.
Historical deployments require their original installation; no release hash
registry rewrites or extends an existing Run's grants.
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


def _single_resource(binding: "BundleRuntimeBinding", prefix: str) -> str | None:
    matches = [entry for entry in binding.resource_bindings if entry.startswith(prefix)]
    return matches[0] if len(matches) == 1 else None


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
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
