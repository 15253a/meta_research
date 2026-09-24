"""Execution compatibility without freezing Bundle's revisable stage guidance.

Historical binding equality, admission hashes and receipts remain exact. Only
execution gates normalize reviewed transport repairs and the two policy prose
resources. Models, capabilities, tools, executable artifacts and output schemas
are still compared exactly.
"""
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from meta_research.owners.agent_runtime import BundleRuntimeBinding, ReasoningRuntimeBinding


_REVIEWED_BUNDLE_TRANSPORT_REVISIONS = frozenset({
    (
        "267be1b0ce9594d094a5b88a7480b0111cbbbc0249d165909a4e2d6feb137853",
        "f383baed577536980e67174d63e27b2be92caefcb7502ad90f3e1c4b5e38e336",
        "4a397daf6c33153a06e578a1f165c7269e6cbdd0c155caeb8605883e6eeb60c5",
    ),
    (
        "01aac42316987ee04b9429f895f874d8f8afaf9d46431cca3633c5f6989aa2ae",
        "f383baed577536980e67174d63e27b2be92caefcb7502ad90f3e1c4b5e38e336",
        "080b6284e78bfc33487311ea07371aef8e56894bb82ea2d5df9bfc890f705c62",
    ),
    (
        "d72d2d310f8c5befcb03b5180b75ccfa93ca453895277b35df79ef711f2c6577",
        "2d25e8ad20c5f733957b4bb48c81acbbe001774d5dbe3f478a0be37f20b4fe1b",
        "080b6284e78bfc33487311ea07371aef8e56894bb82ea2d5df9bfc890f705c62",
    ),
    (
        "fe5b6acfaf30173e3b60f332c2cc2d81fc6bbfa6bc868f851cc7664e0177e277",
        "d547437cc7e8a6da5f03988f6f40fb909286296c3d3d7d227dd66a25d62bf441",
        "080b6284e78bfc33487311ea07371aef8e56894bb82ea2d5df9bfc890f705c62",
    ),
})
_BUNDLE_SOURCE = "adapter-source:meta_research.bundle_skill@sha256:"
_SUPERVISOR_SOURCE = "adapter-source:meta_research.provider_supervisor@sha256:"
_RECOVERY_SOURCE = "adapter-source:meta_research.bundle_dispatch_recovery@sha256:"
_POLICY_CONTRACT = "bundle-execution-contract:policy-refresh/v1"
_POLICY_PACKAGE = "package:meta_research.skills.bundle_stage/"
_POLICY_RESOURCES = ("SKILL.md", "references/contract.md")

# Complete bindings captured from the actual 8767 CLI and its copied transport
# key. Every prior operation binding is identical; the catalog adds only the
# four read-only protocol queries. No admission bytes or receipts are changed.
_REVIEWED_PROTOCOL_BUNDLE_BINDING_PAIRS = frozenset({
    frozenset({
        "d883f6795bda842f46d6a169d8271499802cc0b43d98efb799ca9eb8e0a88235",
        "3dd50f3b14fbf3908a89ee9a2681d817b21292da97e5764509a8c98e80fb570d",
    }),
})

# This adapter revision only adds the contract marker and the previously implicit
# dispatch-recovery source hash. Future prose edits need no hash-table updates;
# changing execution code still requires an explicit compatibility review.
_POLICY_REFRESH_ADAPTERS = frozenset({
    "065eb46009276165063eea84732015a53546bbdccfe96c9b85d46abd03708615",
})
_REVIEWED_SUPERVISORS = frozenset(
    revision[2] for revision in _REVIEWED_BUNDLE_TRANSPORT_REVISIONS
)
_REVIEWED_RECOVERY = "6918457b3fe36f01719e3323b86cd43839b928ede31c780023452aff239ee3c0"

# Old bindings lack the policy contract marker. Migrate only the exact deployed
# package whose instructions were reviewed together with the revisions above.
_LEGACY_POLICY_PACKAGE_HASH = "4d2743769cf8549b776c1c2c119c3dfea78957095d6d7e016870cf2ad2df5f90"
_LEGACY_POLICY_RESOURCES = {
    "SKILL.md": "e7d96919184e6b7eb8ecf39ef3701c32247a261b92c50ee84a6c0792c629550a",
    "references/contract.md": "95be373445b671cdf5a0e39eb7f57cc797e94aea06e7cd8919e066681f0eabd8",
    "references/owner-operations.md": "0a7f197d9c810f40565840bb25caffac875b88f150c94c1c2b86e7fd57701b0a",
}


def _single_resource(binding: "BundleRuntimeBinding", prefix: str) -> str | None:
    matches = [entry for entry in binding.resource_bindings if entry.startswith(prefix)]
    return matches[0] if len(matches) == 1 else None


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _reviewed_execution_value(binding: "BundleRuntimeBinding") -> dict[str, object] | None:
    """Keep the previously deployed transport-only compatibility unchanged."""
    source = _single_resource(binding, _BUNDLE_SOURCE)
    supervisor = _single_resource(binding, _SUPERVISOR_SOURCE)
    if source is None or supervisor is None:
        return None
    revision = (binding.instruction_set_hash, source[len(_BUNDLE_SOURCE):], supervisor[len(_SUPERVISOR_SOURCE):])
    if revision not in _REVIEWED_BUNDLE_TRANSPORT_REVISIONS:
        return None
    value = binding.as_dict()
    value["instruction_set_hash"] = "reviewed-bundle-transport-20260906"
    replacements = {source: _BUNDLE_SOURCE + "reviewed-20260906", supervisor: _SUPERVISOR_SOURCE + "reviewed-20260906"}
    value["resource_bindings"] = [replacements.get(entry, entry) for entry in binding.resource_bindings]
    return value


def _policy_execution_value(binding: "BundleRuntimeBinding") -> dict[str, object] | None:
    source = _single_resource(binding, _BUNDLE_SOURCE)
    supervisor = _single_resource(binding, _SUPERVISOR_SOURCE)
    if source is None or supervisor is None:
        return None
    if not _is_sha256(binding.instruction_set_hash) or not _is_sha256(binding.packaged_skill_bundle_hash):
        return None
    contracts = [entry for entry in binding.resource_bindings if entry.startswith("bundle-execution-contract:")]
    recovery = _single_resource(binding, _RECOVERY_SOURCE)
    if contracts:
        if (
            contracts != [_POLICY_CONTRACT]
            or source[len(_BUNDLE_SOURCE):] not in _POLICY_REFRESH_ADAPTERS
            or supervisor[len(_SUPERVISOR_SOURCE):] not in _REVIEWED_SUPERVISORS
            or recovery != _RECOVERY_SOURCE + _REVIEWED_RECOVERY
        ):
            return None
    else:
        revision = (binding.instruction_set_hash, source[len(_BUNDLE_SOURCE):], supervisor[len(_SUPERVISOR_SOURCE):])
        if (
            revision not in _REVIEWED_BUNDLE_TRANSPORT_REVISIONS
            or binding.packaged_skill_bundle_hash != _LEGACY_POLICY_PACKAGE_HASH
            or recovery is not None
        ):
            return None
        package_entries = [entry for entry in binding.resource_bindings if entry.startswith(_POLICY_PACKAGE)]
        expected = [f"{_POLICY_PACKAGE}{name}@sha256:{digest}" for name, digest in _LEGACY_POLICY_RESOURCES.items()]
        if package_entries != expected:
            return None

    replacements = {
        source: _BUNDLE_SOURCE + "reviewed-20260907",
        supervisor: _SUPERVISOR_SOURCE + "reviewed-20260907",
    }
    for name in _POLICY_RESOURCES:
        prefix = f"{_POLICY_PACKAGE}{name}@sha256:"
        entry = _single_resource(binding, prefix)
        if entry is None or not _is_sha256(entry[len(prefix):]):
            return None
        replacements[entry] = prefix + "revisable-policy-v1"
    value = binding.as_dict()
    value["instruction_set_hash"] = "bundle-stage-revisable-policy/v1"
    value["packaged_skill_bundle_hash"] = "bundle-stage-revisable-policy/v1"
    value["resource_bindings"] = [
        replacements.get(entry, entry)
        for entry in binding.resource_bindings
        if entry != _POLICY_CONTRACT and entry != recovery
    ]
    return value


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
    # historical transport exceptions below are a separate compatibility path.
    value["resource_bindings"] = [
        replacements.get(entry, entry) for entry in binding.resource_bindings
    ]
    return value


def bundle_bindings_compatible(left: "BundleRuntimeBinding", right: "BundleRuntimeBinding") -> bool:
    from meta_research.owners.agent_runtime import BundleRuntimeBinding
    from meta_research.owners.common import canonical_hash

    if type(left) is not BundleRuntimeBinding or type(right) is not BundleRuntimeBinding:
        return False
    if left == right:
        return True
    before = _same_execution_policy_value(left)
    after = _same_execution_policy_value(right)
    if before is not None and after is not None and before == after:
        return True
    pair = frozenset((canonical_hash(left.as_dict()), canonical_hash(right.as_dict())))
    if len(pair) == 2 and pair in _REVIEWED_PROTOCOL_BUNDLE_BINDING_PAIRS:
        return True
    before = _reviewed_execution_value(left)
    after = _reviewed_execution_value(right)
    if before is not None and after is not None and before == after:
        return True
    before = _policy_execution_value(left)
    after = _policy_execution_value(right)
    return before is not None and after is not None and before == after


# Only this deployment's complete, independently reviewed before/after binding
# pair may bridge the Reasoning submit-recovery repair. This is execution-only:
# admission serialization, frozen Run/Attempt hashes and receipts stay exact.
# Audited artifacts: reasoning-submit-recovery-20260910/reasoning-binding-{before,after}.json.
_REVIEWED_REASONING_BINDING_PAIRS: frozenset[frozenset[str]] = frozenset({
    frozenset({
        "0d926bdb5d90952791279630d8d5e6d6410df41c9b7c3c0424eac7cee655858f",
        "70ac6ea577199bccbc9b837a8aec4557aa2a03f9d60d0b73b55e1d2691b89868",
    }),
})


def target_request_catalog_compatible(
    operation_ids: tuple[str, ...], binding: dict[str, object], current_operation_ids: tuple[str, ...]
) -> bool:
    """Read an exact pre-protocol Target request without expanding its grants.

    The old request hash, Attempt, Fence and capability binding are still checked
    by Harness. This exception only recognizes the deployed 2026-09-13 catalog
    before the four read-only protocol discovery operations were introduced.
    """
    from meta_research.owners.common import canonical_hash

    if operation_ids == current_operation_ids:
        return True
    additions = {
        "research_graph.baselines.page", "research_graph.baselines.read",
        "research_graph.target_formal_results.read",
        "research_memory.research_notes.read",
    }
    return (
        operation_ids == tuple(item for item in current_operation_ids if item not in additions)
        and binding.get("required_operation_ids") == list(operation_ids)
        and canonical_hash(list(operation_ids)) == "43ff852078ed99ba3fb9af60bca0f0bd7119bb7cc64e71c9e133a902d3daf0f3"
        and canonical_hash(binding) == "d320026b933aefc085369bc36e055934f83ebdc8fe81a37297255b39e14ba7e6"
    )


def reasoning_bindings_compatible(
    left: "ReasoningRuntimeBinding", right: "ReasoningRuntimeBinding"
) -> bool:
    """Allow exact equality or one complete reviewed deployment binding pair."""
    from meta_research.owners.agent_runtime import ReasoningRuntimeBinding
    from meta_research.owners.common import canonical_hash

    if type(left) is not ReasoningRuntimeBinding or type(right) is not ReasoningRuntimeBinding:
        return False
    if left == right:
        return True
    pair = frozenset((canonical_hash(left.as_dict()), canonical_hash(right.as_dict())))
    return len(pair) == 2 and pair in _REVIEWED_REASONING_BINDING_PAIRS
