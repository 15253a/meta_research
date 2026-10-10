"""Execution compatibility without freezing Bundle's revisable stage guidance.

Historical binding equality, admission hashes and receipts remain exact. Only
execution gates normalize reviewed transport/conditions repairs and the two
policy prose resources. Models, capabilities, tools, executable artifacts and
output schemas are still compared exactly.
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
_SHARED_BUNDLE_SOURCE = "adapter-source:meta_research.idea_skill@sha256:"
_SUPERVISOR_SOURCE = "adapter-source:meta_research.provider_supervisor@sha256:"
_RECOVERY_SOURCE = "adapter-source:meta_research.bundle_dispatch_recovery@sha256:"
_POLICY_CONTRACT = "bundle-execution-contract:policy-refresh/v1"
_POLICY_PACKAGE = "package:meta_research.skills.bundle_stage/"
_POLICY_RESOURCES = ("SKILL.md", "references/contract.md")

# Exact reviewed shared adapter revisions: classify signed terminal account
# usage limits without changing tools, schemas, native identity or sealed files.
_REVIEWED_USAGE_LIMIT_SHARED_SOURCES = frozenset({
    "653e4374f2440a6b93ad40b520db472dd9c8b5b6dde80a707a59c380ff3e0a25",
    "e20a48af0c526466baa2303cd1e9d4f028399da4389faca8b62e13439036d699",
})

# 09e0ac7 adds current Quest conditions to new calls and retains the sealed
# conditions/prompt for durable replays. These exact aggregate instruction and
# two source hashes were checked against the admitted 8768 Bundle and release.
# This is execution-only: all remaining fields, resources, schemas and the
# per-deployment transport key must still be identical, with no receipt rewrite.
_REVIEWED_BUNDLE_CONDITIONS_REVISIONS = frozenset({
    (
        "67c1c3d4005ab5a73e8c0eaa7a45c9eee10bdacb8b0481dd819af6a50a4a02b4",
        "ffea3bacb7082e8cfbf7b3948780d6ded827c05811cdfab45245b3ea5ae820ec",
        "3c3b6e2cfd827981ec5cbd79c166b00f005849a9c9880d05d19cd9954255d5ad",
    ),
    (
        "9762d6d048f08186f950a010bd489f6a1a113b2511821698409629da3d391a96",
        "a68b18ac34873d2cb3357938bbdf23a5b9d7c3e54926b6806b396cff2ce0246e",
        "653e4374f2440a6b93ad40b520db472dd9c8b5b6dde80a707a59c380ff3e0a25",
    ),
})

# Complete bindings captured from the actual 8767 CLI and its copied transport
# key. Every prior operation binding is identical; the catalog adds only the
# four read-only protocol queries. No admission bytes or receipts are changed.
_REVIEWED_PROTOCOL_BUNDLE_BINDING_PAIRS = frozenset({
    frozenset({
        "d883f6795bda842f46d6a169d8271499802cc0b43d98efb799ca9eb8e0a88235",
        "3dd50f3b14fbf3908a89ee9a2681d817b21292da97e5764509a8c98e80fb570d",
    }),
})

# 2026-09-29: Bundle17's actual frozen binding and the candidate recomputed
# from the same deployment artifacts/key. Only the complete root profile hash
# changes; models, grants, MCP catalog, schemas and all other resources match.
# The profile differs only in the reviewed research instruction text. This
# exact pair does not authorize subsequent prompt, model or configuration edits.
_REVIEWED_ROOT_PROMPT_BUNDLE_BINDING_PAIRS = frozenset({
    frozenset({
        "9e877a6aae636809842c44f05b647120e3df7b509c98ca97a80f215a46124ae5",
        "fcbbf541fb19343e6c033d75ac9c69e612be290d51eadc7224010f1915d095b4",
    }),
})

# 2026-09-30: the active Bundle's exact frozen profile and the candidate with
# reviewed frozen-input/system-help guidance in owner-operations.md. Only that
# resource and its instruction/package aggregate hashes differ. Preserve every
# execution field, sealed prompt and historical admission identity; this pair
# does not make owner-operation prose generally revisable.
# Full profiles and original prose: fixtures/bundle_frozen_input_recovery_20260930/.
_REVIEWED_FROZEN_INPUT_RECOVERY_BUNDLE_BINDING_PAIRS = frozenset({
    frozenset({
        "43b138c196cae3a8e205c2b8b66fc2c9d6ce3b71bf041bede7283ad1e18f95ad",
        "a39e08787040ff56118d56a9d8a646a8f145122c22391f762de918fb7f11a718",
    }),
    # The final adapter restores only the original instruction prefix for an
    # existing sealed call; its full invocation and request remain unchanged.
    frozenset({
        "43b138c196cae3a8e205c2b8b66fc2c9d6ce3b71bf041bede7283ad1e18f95ad",
        "a72d15e5a107435726c06beccfbe4178c6dad3cabd8a9d826876a57c1fad8996",
    }),
})

# Directional, complete profile identities for historical reads only. Fixtures
# retain the full objects and their reviewed capability/model/config differences.
_QUEST_GOAL_GUIDANCE_ROOT_PROFILE_TRANSITIONS = frozenset({
    *(
        (before, "7095b640388dd7b4c32c1aff0538454578ca7658690922e8b57ccfb7a641771b")
        for before in (
            "15c6735fcb28a8e0a820952f49d37242d2efc345e2bc92236a12e8345b74b62e",
            "187457ec5bdc0bcd5d206e770f7c8f1f53e449c569f6beec68938ea79ba9e8de",
            "5bfe6cff4d3118caf38313803fa115bf962381ae4137984b48bc178d4aed3a85",
            "708321017aed3c4c4cf3085b00ed447c380306cfb45a71857263ab0cebec486d",
            "7f70bb11a077e60d07383ca75b4686675743c63e1938af2c2ebe9587e95c4bf0",
            "804c83eb1e28ce4ccec59e1a564a701af666eaba7e7ff6740418665da7cf21ff",
            "8f043025de4b27d9885426b44848fc4edad721ed0012c0102952c807fc2f14be",
            "b2830836c03bdeadf7be7bf824c273c010106dcd553ae7a6d73b2881cf7adf30",
            "c1f65540670572e9ee6211c004e6ad498400ede63f0b9da125843bd720ef77b9",
            "e31eebfad3e985d523b14ac2b49526291a0a661c3c7ed501dfa64fe982248370",
            "e8e691b757cb608562d9766f8c9507f1f2feb9ad71b43de92296f310832bde55",
        )
    ),
})

_REVIEWED_HISTORICAL_ROOT_PROFILE_TRANSITIONS = frozenset({
    *_QUEST_GOAL_GUIDANCE_ROOT_PROFILE_TRANSITIONS,
    # #183 adds current-root material processing guidance. Preserve only these
    # exact prior historical read identities; catalogs and execution bindings
    # remain exact. Full profiles: fixtures/material_processing_20261010/.
    *((before, "738a9ec21427f47674d5009e3e1c7ecfa5fd4c7e08edf669182d3899821d51fd") for before in (
        "15c6735fcb28a8e0a820952f49d37242d2efc345e2bc92236a12e8345b74b62e",
        "187457ec5bdc0bcd5d206e770f7c8f1f53e449c569f6beec68938ea79ba9e8de",
        "5bfe6cff4d3118caf38313803fa115bf962381ae4137984b48bc178d4aed3a85",
        "708321017aed3c4c4cf3085b00ed447c380306cfb45a71857263ab0cebec486d",
        "7095b640388dd7b4c32c1aff0538454578ca7658690922e8b57ccfb7a641771b",
        "7f70bb11a077e60d07383ca75b4686675743c63e1938af2c2ebe9587e95c4bf0",
        "804c83eb1e28ce4ccec59e1a564a701af666eaba7e7ff6740418665da7cf21ff",
        "8f043025de4b27d9885426b44848fc4edad721ed0012c0102952c807fc2f14be",
        "b2830836c03bdeadf7be7bf824c273c010106dcd553ae7a6d73b2881cf7adf30",
        "c1f65540670572e9ee6211c004e6ad498400ede63f0b9da125843bd720ef77b9",
        "e31eebfad3e985d523b14ac2b49526291a0a661c3c7ed501dfa64fe982248370",
        "e8e691b757cb608562d9766f8c9507f1f2feb9ad71b43de92296f310832bde55",
    )),
    # 2026-10-08 PR #202-#210 integration: this exact combined prompt retains
    # the baseline's historical read identities and the separately accepted
    # research-style, cleanup, help-handoff, reply and guidance prompt profiles.
    # These direct read transitions do not grant execution compatibility.
    # Complete identities: fixtures/batch_merge_20261008/profiles.json.
    ("804c83eb1e28ce4ccec59e1a564a701af666eaba7e7ff6740418665da7cf21ff", "c1f65540670572e9ee6211c004e6ad498400ede63f0b9da125843bd720ef77b9"),
    ("15c6735fcb28a8e0a820952f49d37242d2efc345e2bc92236a12e8345b74b62e", "c1f65540670572e9ee6211c004e6ad498400ede63f0b9da125843bd720ef77b9"),
    ("8f043025de4b27d9885426b44848fc4edad721ed0012c0102952c807fc2f14be", "c1f65540670572e9ee6211c004e6ad498400ede63f0b9da125843bd720ef77b9"),
    ("708321017aed3c4c4cf3085b00ed447c380306cfb45a71857263ab0cebec486d", "c1f65540670572e9ee6211c004e6ad498400ede63f0b9da125843bd720ef77b9"),
    ("b2830836c03bdeadf7be7bf824c273c010106dcd553ae7a6d73b2881cf7adf30", "c1f65540670572e9ee6211c004e6ad498400ede63f0b9da125843bd720ef77b9"),
    ("5bfe6cff4d3118caf38313803fa115bf962381ae4137984b48bc178d4aed3a85", "c1f65540670572e9ee6211c004e6ad498400ede63f0b9da125843bd720ef77b9"),
    ("187457ec5bdc0bcd5d206e770f7c8f1f53e449c569f6beec68938ea79ba9e8de", "c1f65540670572e9ee6211c004e6ad498400ede63f0b9da125843bd720ef77b9"),
    ("e8e691b757cb608562d9766f8c9507f1f2feb9ad71b43de92296f310832bde55", "c1f65540670572e9ee6211c004e6ad498400ede63f0b9da125843bd720ef77b9"),
    ("7f70bb11a077e60d07383ca75b4686675743c63e1938af2c2ebe9587e95c4bf0", "c1f65540670572e9ee6211c004e6ad498400ede63f0b9da125843bd720ef77b9"),
    ("e31eebfad3e985d523b14ac2b49526291a0a661c3c7ed501dfa64fe982248370", "c1f65540670572e9ee6211c004e6ad498400ede63f0b9da125843bd720ef77b9"),
    ("804c83eb1e28ce4ccec59e1a564a701af666eaba7e7ff6740418665da7cf21ff", "7f70bb11a077e60d07383ca75b4686675743c63e1938af2c2ebe9587e95c4bf0"),
    ("15c6735fcb28a8e0a820952f49d37242d2efc345e2bc92236a12e8345b74b62e", "7f70bb11a077e60d07383ca75b4686675743c63e1938af2c2ebe9587e95c4bf0"),
    ("8f043025de4b27d9885426b44848fc4edad721ed0012c0102952c807fc2f14be", "7f70bb11a077e60d07383ca75b4686675743c63e1938af2c2ebe9587e95c4bf0"),
    ("708321017aed3c4c4cf3085b00ed447c380306cfb45a71857263ab0cebec486d", "7f70bb11a077e60d07383ca75b4686675743c63e1938af2c2ebe9587e95c4bf0"),
    ("b2830836c03bdeadf7be7bf824c273c010106dcd553ae7a6d73b2881cf7adf30", "7f70bb11a077e60d07383ca75b4686675743c63e1938af2c2ebe9587e95c4bf0"),

    (
        "804c83eb1e28ce4ccec59e1a564a701af666eaba7e7ff6740418665da7cf21ff",
        "15c6735fcb28a8e0a820952f49d37242d2efc345e2bc92236a12e8345b74b62e",
    ),
    # 2026-09-29 Bundle candidate handoff: both deployed profiles differ from
    # this exact current profile only in research_system_prompt_hash, checked
    # for all nine root kinds. Keep both direct historical-read transitions;
    # this does not grant execution compatibility or rewrite signed records.
    # Full profile evidence: fixtures/root_prompt_bundle_handoff_20260929/.
    (
        "804c83eb1e28ce4ccec59e1a564a701af666eaba7e7ff6740418665da7cf21ff",
        "8f043025de4b27d9885426b44848fc4edad721ed0012c0102952c807fc2f14be",
    ),
    (
        "15c6735fcb28a8e0a820952f49d37242d2efc345e2bc92236a12e8345b74b62e",
        "8f043025de4b27d9885426b44848fc4edad721ed0012c0102952c807fc2f14be",
    ),
    # 2026-09-30 authorized model upgrade. The preceding current profile
    # changes only model_ref to gpt-6.1-sol; preserve all three exact historical
    # identities for reads, without granting execution or rewriting receipts.
    # Complete profile evidence: fixtures/model_upgrade_20260930/profiles.json.
    (
        "804c83eb1e28ce4ccec59e1a564a701af666eaba7e7ff6740418665da7cf21ff",
        "708321017aed3c4c4cf3085b00ed447c380306cfb45a71857263ab0cebec486d",
    ),
    (
        "15c6735fcb28a8e0a820952f49d37242d2efc345e2bc92236a12e8345b74b62e",
        "708321017aed3c4c4cf3085b00ed447c380306cfb45a71857263ab0cebec486d",
    ),
    (
        "8f043025de4b27d9885426b44848fc4edad721ed0012c0102952c807fc2f14be",
        "708321017aed3c4c4cf3085b00ed447c380306cfb45a71857263ab0cebec486d",
    ),
    (
        "804c83eb1e28ce4ccec59e1a564a701af666eaba7e7ff6740418665da7cf21ff",
        "b2830836c03bdeadf7be7bf824c273c010106dcd553ae7a6d73b2881cf7adf30",
    ),
    (
        "15c6735fcb28a8e0a820952f49d37242d2efc345e2bc92236a12e8345b74b62e",
        "b2830836c03bdeadf7be7bf824c273c010106dcd553ae7a6d73b2881cf7adf30",
    ),
    (
        "8f043025de4b27d9885426b44848fc4edad721ed0012c0102952c807fc2f14be",
        "b2830836c03bdeadf7be7bf824c273c010106dcd553ae7a6d73b2881cf7adf30",
    ),
    (
        "708321017aed3c4c4cf3085b00ed447c380306cfb45a71857263ab0cebec486d",
        "b2830836c03bdeadf7be7bf824c273c010106dcd553ae7a6d73b2881cf7adf30",
    ),
    ("804c83eb1e28ce4ccec59e1a564a701af666eaba7e7ff6740418665da7cf21ff", "e31eebfad3e985d523b14ac2b49526291a0a661c3c7ed501dfa64fe982248370"),
    ("15c6735fcb28a8e0a820952f49d37242d2efc345e2bc92236a12e8345b74b62e", "e31eebfad3e985d523b14ac2b49526291a0a661c3c7ed501dfa64fe982248370"),
    ("8f043025de4b27d9885426b44848fc4edad721ed0012c0102952c807fc2f14be", "e31eebfad3e985d523b14ac2b49526291a0a661c3c7ed501dfa64fe982248370"),
    ("708321017aed3c4c4cf3085b00ed447c380306cfb45a71857263ab0cebec486d", "e31eebfad3e985d523b14ac2b49526291a0a661c3c7ed501dfa64fe982248370"),
    ("b2830836c03bdeadf7be7bf824c273c010106dcd553ae7a6d73b2881cf7adf30", "e31eebfad3e985d523b14ac2b49526291a0a661c3c7ed501dfa64fe982248370"),

})


def reviewed_historical_root_profile_hashes(current_hash: str) -> frozenset[str]:
    """Historical identities reviewed for this exact current profile."""
    return frozenset(
        before for before, after in _REVIEWED_HISTORICAL_ROOT_PROFILE_TRANSITIONS
        if current_hash == after
    )

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
    shared = _single_resource(binding, _SHARED_BUNDLE_SOURCE)
    if shared is not None and shared[len(_SHARED_BUNDLE_SOURCE):] in _REVIEWED_USAGE_LIMIT_SHARED_SOURCES:
        replacements[shared] = _SHARED_BUNDLE_SOURCE + "reviewed-usage-limit-20260926"
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


def _reviewed_conditions_execution_value(binding: "BundleRuntimeBinding") -> dict[str, object] | None:
    source = _single_resource(binding, _BUNDLE_SOURCE)
    shared = _single_resource(binding, _SHARED_BUNDLE_SOURCE)
    contracts = [entry for entry in binding.resource_bindings if entry.startswith("bundle-execution-contract:")]
    if source is None or shared is None or contracts != [_POLICY_CONTRACT]:
        return None
    revision = (binding.instruction_set_hash, source[len(_BUNDLE_SOURCE):], shared[len(_SHARED_BUNDLE_SOURCE):])
    if revision not in _REVIEWED_BUNDLE_CONDITIONS_REVISIONS:
        return None
    value = binding.as_dict()
    value["instruction_set_hash"] = "reviewed-bundle-conditions-20260924"
    replacements = {
        source: _BUNDLE_SOURCE + "reviewed-conditions-20260924",
        shared: _SHARED_BUNDLE_SOURCE + "reviewed-conditions-20260924",
    }
    value["resource_bindings"] = [replacements.get(entry, entry) for entry in binding.resource_bindings]
    return value


def bundle_bindings_compatible(left: "BundleRuntimeBinding", right: "BundleRuntimeBinding") -> bool:
    from meta_research.owners.agent_runtime import BundleRuntimeBinding
    from meta_research.owners.common import canonical_hash

    if type(left) is not BundleRuntimeBinding or type(right) is not BundleRuntimeBinding:
        return False
    if left == right:
        return True
    before = _reviewed_conditions_execution_value(left)
    after = _reviewed_conditions_execution_value(right)
    if before is not None and after is not None and before == after:
        return True
    before = _same_execution_policy_value(left)
    after = _same_execution_policy_value(right)
    if before is not None and after is not None and before == after:
        return True
    pair = frozenset((canonical_hash(left.as_dict()), canonical_hash(right.as_dict())))
    if len(pair) == 2 and pair in _REVIEWED_FROZEN_INPUT_RECOVERY_BUNDLE_BINDING_PAIRS:
        return True
    if len(pair) == 2 and pair in _REVIEWED_ROOT_PROMPT_BUNDLE_BINDING_PAIRS:
        return True
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
    # 2026-09-28: only Reasoning SKILL.md and references/contract.md clarify
    # exact reference arrays and correction after truncated output. Full frozen
    # Run/candidate bindings are in tests/fixtures/reasoning_binding_compatibility/
    # reasoning-binding-20260928-{before,after}.json; all execution fields match.
    frozenset({
        "1c94cc1e0efa9ff348b1b4e7c8c653b44df119ac732230be948b4f900cf27a51",
        "d8bdbd84dbbed7f75f54819fa3f3c47d21af8a7ef90d4820c5b6af21b875f74a",
    }),
    # 2026-09-28 0651: bound five frozen reference arrays by their actual
    # request lengths and clarify copying prior outcome refs. The complete
    # reviewed pair includes the contract, adapter and all three generated
    # schema hashes; execution identity and frozen receipts remain unchanged.
    # Fixtures: reasoning-binding-20260928-0651-{before,after}.json.
    frozenset({
        "d8bdbd84dbbed7f75f54819fa3f3c47d21af8a7ef90d4820c5b6af21b875f74a",
        "c846926fd589eed5ea41608fedfabd33ff05b6302f01670fcb6e61d3df7f2243",
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
