"""Source-reviewed MCP composition, without inventing deployment identities."""
from copy import deepcopy
from dataclasses import replace
import importlib
import json
from pathlib import Path

import pytest

from meta_research.idea_skill import (
    DEFAULT_PROVIDER_TRANSPORT_LIMITS, IdeaSkillUnavailable,
    _read_operation_invocation, _sealed_operation_invocation,
)
from meta_research.owners.common import canonical_hash
from meta_research.runtime_binding_compatibility import (
    bundle_bindings_compatible, reasoning_bindings_compatible,
)
from meta_research.system_mcp_binding_compatibility import (
    previous_system_mcp_binding, system_mcp_bindings_compatible,
)


FIXTURES = Path(__file__).parent / "fixtures"
MERGE = json.loads((FIXTURES / "system_mcp_test_merge_sources.json").read_text())
OLD_BEFORE = json.loads((FIXTURES / "system_mcp_binding_before.json").read_text())
OLD_AFTER = json.loads((FIXTURES / "system_mcp_binding_after.json").read_text())


def _with_sources(binding, revision):
    sources = {item.split("@sha256:")[0]: item for item in revision["sources"]}
    return replace(
        binding, instruction_set_hash=revision["instruction_set_hash"],
        resource_bindings=tuple(
            sources.get(item.split("@sha256:")[0], item)
            for item in binding.resource_bindings
        ),
    )


def _stage_compatible(left, right):
    if type(left).__name__ == "BundleRuntimeBinding":
        return bundle_bindings_compatible(left, right)
    if type(left).__name__ == "ReasoningRuntimeBinding":
        return reasoning_bindings_compatible(left, right)
    return system_mcp_bindings_compatible(left, right)


@pytest.fixture(params=range(len(MERGE["revisions"])))
def source_pair(request, tmp_path, monkeypatch):
    pair = MERGE["revisions"][request.param]
    revision = pair["after"]
    stage = revision["binding_type"].removesuffix("RuntimeBinding").lower()
    module = importlib.import_module("meta_research." + stage + "_skill")
    monkeypatch.setattr(module, "_codex_harness_manifest", lambda _path: (
        "test:harness", ("test:harness-artifact:unchanged",),
    ))
    adapter = getattr(module, "Codex" + stage.title() + "SkillAdapter")(tmp_path / stage)
    current = (adapter.runtime_binding(revision["profile"])
               if revision["profile"] else adapter.runtime_binding())
    # Use the production binding constructor and actual on-disk source hashes.
    # A later adapter/skill change must receive a new review, not inherit this.
    assert current.instruction_set_hash == revision["instruction_set_hash"]
    assert [item for item in current.resource_bindings
            if item.startswith("adapter-source:")] == revision["sources"]
    return pair, current


def test_merge_mcp_bridge_preserves_exact_test_binding(source_pair):
    pair, current = source_pair
    before = _with_sources(current, pair["before"])
    original = deepcopy((before.as_dict(), current.as_dict()))
    assert MERGE["capture_kind"] == "recomputed-source-identities-not-deployment-bindings"
    assert before != current
    assert canonical_hash(before.as_dict()) != canonical_hash(current.as_dict())
    assert previous_system_mcp_binding(current) == before
    assert system_mcp_bindings_compatible(before, current)
    assert system_mcp_bindings_compatible(current, before)
    assert _stage_compatible(before, current)
    assert _stage_compatible(current, before)
    assert (before.as_dict(), current.as_dict()) == original


def test_original_8769_source_bridge_remains_independent(source_pair):
    pair, template = source_pair
    kind, profile = pair["after"]["binding_type"], pair["after"]["profile"]
    def old_revision(records):
        return next(item for item in records
                    if item["binding_type"] == kind and item["profile"] == profile)
    # Synthetic shared transport fields isolate the old captured source bridge;
    # these are not claimed to be a full captured deployment binding.
    old_before = _with_sources(template, old_revision(OLD_BEFORE))
    old_after = _with_sources(template, old_revision(OLD_AFTER))
    original = deepcopy((old_before.as_dict(), old_after.as_dict()))
    assert previous_system_mcp_binding(old_after) == old_before
    assert system_mcp_bindings_compatible(old_before, old_after)
    assert system_mcp_bindings_compatible(old_after, old_before)
    assert _stage_compatible(old_before, old_after)
    assert not system_mcp_bindings_compatible(old_after, template)
    assert (old_before.as_dict(), old_after.as_dict()) == original


@pytest.mark.parametrize("field", (
    "model_ref", "harness_adapter_ref", "instruction_set_hash",
    "packaged_skill_bundle_hash", "mcp_bindings", "capability_bindings",
))
@pytest.mark.parametrize("side", ("before", "after"))
def test_merge_bridge_rejects_unreviewed_fields(source_pair, field, side):
    pair, after = source_pair
    before = _with_sources(after, pair["before"])
    subject, other = (before, after) if side == "before" else (after, before)
    value = getattr(subject, field)
    drift = replace(subject, **{
        field: value + ("unreviewed",) if isinstance(value, tuple) else "unreviewed:" + value,
    })
    assert not system_mcp_bindings_compatible(drift, other)
    assert not system_mcp_bindings_compatible(other, drift)


def test_stage_bridge_rejects_changed_output_schemas(source_pair):
    pair, after = source_pair
    before = _with_sources(after, pair["before"])
    schemas = [item for item in before.resource_bindings if item.startswith("output-schema:")]
    assert schemas
    for schema in schemas:
        drift = replace(before, resource_bindings=tuple(
            item + ":unreviewed" if item == schema else item
            for item in before.resource_bindings
        ))
        assert not _stage_compatible(drift, after)
        assert not _stage_compatible(after, drift)
    if hasattr(before, "schema_ref"):
        drift = replace(before, schema_ref="unreviewed:" + before.schema_ref)
        assert not _stage_compatible(drift, after)
        assert not _stage_compatible(after, drift)


@pytest.mark.parametrize("side", ("before", "after"))
def test_merge_bridge_keeps_every_resource_exact(source_pair, side):
    pair, after = source_pair
    before = _with_sources(after, pair["before"])
    subject, other = (before, after) if side == "before" else (after, before)
    for index, entry in enumerate(subject.resource_bindings):
        original = subject.resource_bindings
        for resources in (
            original[:index] + (entry + ":unreviewed",) + original[index + 1:],
            original[:index] + original[index + 1:],
            original[:index] + (entry,) + original[index:],
            tuple(reversed(original)),
        ):
            drift = replace(subject, resource_bindings=resources)
            assert not system_mcp_bindings_compatible(drift, other)
            assert not system_mcp_bindings_compatible(other, drift)


def test_source_bridge_cannot_authorize_a_historical_root_profile(source_pair):
    pair, after = source_pair
    before = _with_sources(after, pair["before"])
    historical = json.loads((FIXTURES / "root_prompt_20260929" / "before.json").read_text())
    before = replace(before, capability_bindings=tuple(
        "root-capability-profile:sha256:" + historical["root_profile_hash"]
        if item.startswith("root-capability-profile:sha256:") else item
        for item in before.capability_bindings
    ))
    assert not _stage_compatible(before, after)
    assert not _stage_compatible(after, before)


def test_bundle_usage_limit_repair_stays_within_its_reviewed_transport(tmp_path, monkeypatch):
    from meta_research import bundle_skill
    monkeypatch.setattr(bundle_skill, "_codex_harness_manifest", lambda _path: (
        "test:harness", ("test:harness-artifact:unchanged",),
    ))
    current = bundle_skill.CodexBundleSkillAdapter(tmp_path / "bundle").runtime_binding()
    prefix = "adapter-source:meta_research.idea_skill@sha256:"

    def shared_revision(binding, digest):
        return replace(binding, instruction_set_hash="a" * 64, resource_bindings=tuple(
            prefix + digest if item.startswith(prefix) else item
            for item in binding.resource_bindings
        ))

    mcp_without_quota = shared_revision(current, "63518da94839dcf74332db7280caaffa9104dffbe2aa64b102dab9eb12103f44")
    pre_mcp = previous_system_mcp_binding(current)
    assert pre_mcp is not None
    original_without_quota = shared_revision(pre_mcp, "653e4374f2440a6b93ad40b520db472dd9c8b5b6dde80a707a59c380ff3e0a25")
    for before in (mcp_without_quota, original_without_quota):
        original = deepcopy(before.as_dict())
        assert bundle_bindings_compatible(before, current)
        assert bundle_bindings_compatible(current, before)
        assert before.as_dict() == original
        assert not bundle_bindings_compatible(replace(before, model_ref="other"), current)
        assert not bundle_bindings_compatible(shared_revision(before, "f" * 64), current)

    mixed_transport = shared_revision(current, "653e4374f2440a6b93ad40b520db472dd9c8b5b6dde80a707a59c380ff3e0a25")
    assert not bundle_bindings_compatible(mixed_transport, current)
    assert not bundle_bindings_compatible(current, mixed_transport)


def test_frozen_invocation_replay_keeps_identity_and_rejects_new_profile_or_schema(tmp_path):
    from meta_research.idea_skill import _CODEX_PROVIDER_OPERATION_SCHEMA
    historical = json.loads((FIXTURES / "root_prompt_20260929" / "before.json").read_text())
    current = json.loads((FIXTURES / "root_prompt_bundle_handoff_20260929" / "profiles.json").read_text())
    key = b"test-only-operation-seal-key"
    base = {
        "schema_ref": _CODEX_PROVIDER_OPERATION_SCHEMA,
        "job_ref": "fixture:job", "operation_name": "primary",
        "prompt_hash": canonical_hash("frozen prompt"),
        "output_schema_hash": canonical_hash({"type": "object"}),
        "native_session_ref": None, "model_ref": historical["runtime_binding"]["model_ref"],
        "root_capability_profile": historical["root_profile"],
        "root_capability_profile_hash": historical["root_profile_hash"],
        "mcp_url": None, "mcp_scope_binding_hash": None,
        **DEFAULT_PROVIDER_TRANSPORT_LIMITS.as_dict(),
    }
    payload = {**base, "transport_mode": "durable_supervisor"}
    path = tmp_path / "invocation.json"
    path.write_text(_sealed_operation_invocation(payload, key), encoding="utf-8")
    original = path.read_bytes()
    assert _read_operation_invocation(path, key=key, expected_base=base) == payload
    for update in (
        {"root_capability_profile": current["current_profile"],
         "root_capability_profile_hash": current["current_profile_hash"]},
        {"output_schema_hash": canonical_hash({"type": "string"})},
        {"prompt_hash": canonical_hash("new prompt")},
    ):
        with pytest.raises(IdeaSkillUnavailable, match="codex_operation_identity_conflict"):
            _read_operation_invocation(path, key=key, expected_base={**base, **update})
    assert path.read_bytes() == original
