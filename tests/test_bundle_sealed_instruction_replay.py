"""A reviewed instruction refresh replays only the original sealed Bundle call."""

from dataclasses import replace
import json
from pathlib import Path

import pytest

import meta_research.bundle_skill as bundle_skill
from meta_research.bundle_skill import BundleSkillUnavailable, CodexBundleSkillAdapter
from meta_research.owners.common import canonical_hash
from meta_research.runtime_binding_compatibility import bundle_bindings_compatible
from meta_research.runtime_conditions import split_runtime_prompt
from test_binding_compatibility import request as dispatch_request
from test_bundle_runtime_conditions_compatibility import _SignedDispatchRunner
from test_bundle_skill_adapter import _FullConformanceAuthority, _fake_codex
from test_bundle_frozen_input_recovery_compatibility import FIXTURES, _binding


@pytest.fixture(params=["plain", "envelope"])
def sealed_dispatch(tmp_path, monkeypatch, request):
    monkeypatch.setattr("meta_research.idea_skill.render_runtime_conditions", lambda workspace, **scope: "Frozen test runtime conditions.")
    if request.param == "envelope":
        dispatch_schema = bundle_skill._dispatch_schema
        monkeypatch.setattr(bundle_skill, "_dispatch_schema", lambda frontier: {"oneOf": [dispatch_schema(frontier)]})
    output = {"action": "dispatch", "selected_target_ref": "target:followup", "rationale": "Continue."}
    runner = _SignedDispatchRunner([output, output])
    provider = CodexBundleSkillAdapter(
        tmp_path / "provider", executable=str(_fake_codex(tmp_path / "codex")), process_runner=runner,
    )
    authority = _FullConformanceAuthority()
    provider.bind_full_conformance_authority(authority)
    provider.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
    frozen = provider.runtime_binding()
    dispatch = replace(dispatch_request(frozen), job_ref="bundle-job:sealed")
    result = provider.schedule_target(dispatch)
    directory = tmp_path / "provider" / "provider-operations" / canonical_hash({"job_ref": dispatch.job_ref}) / "dispatch-1"
    original = {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}
    resources = bundle_skill._bundle_skill_resources()
    refreshed = {**resources, "SKILL.md": resources["SKILL.md"] + "\nReviewed current guidance.\n"}
    monkeypatch.setattr(bundle_skill, "_bundle_skill_resources", lambda: refreshed)
    assert bundle_bindings_compatible(frozen, provider.runtime_binding())
    return provider, runner, authority, dispatch, result, directory, original


def test_completed_signed_operation_replays_without_rewriting_frozen_inputs(sealed_dispatch):
    provider, runner, authority, dispatch, first, directory, original = sealed_dispatch

    assert provider.schedule_target(dispatch) == first
    assert len(runner.calls) == 1
    assert {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()} == original
    assert authority.issued[-1]["capability_binding_hash"] == canonical_hash(dispatch.runtime_binding.as_dict())

    provider.schedule_target(replace(dispatch, job_ref="bundle-job:next", generation=2))
    assert len(runner.calls) == 2
    assert "Reviewed current guidance." not in runner.calls[0][1]
    assert "Reviewed current guidance." in runner.calls[1][1]


@pytest.mark.parametrize("mutation", ["request", "native", "invocation", "seal"])
def test_instruction_replay_keeps_request_identity_and_signed_invocation_exact(sealed_dispatch, mutation):
    provider, runner, _, dispatch, _, directory, original = sealed_dispatch
    if mutation == "request":
        dispatch = replace(dispatch, state={**dispatch.state, "target_commit_refs": ["target-commit:changed"]})
    elif mutation == "native":
        dispatch = replace(dispatch, native_session_ref="native-session:changed")
    else:
        path = directory / "invocation.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        if mutation == "seal":
            value["seal"] = "f" * 64
        else:
            value["payload"]["model_ref"] = "unreviewed-model"
        path.write_text(json.dumps(value), encoding="utf-8")
        original["invocation.json"] = path.read_bytes()

    expected = "codex_operation_spool_invalid" if mutation == "request" else "codex_operation_identity_conflict"
    with pytest.raises(BundleSkillUnavailable, match=expected):
        provider.schedule_target(dispatch)
    assert len(runner.calls) == 1
    assert {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()} == original


def _actual_sealed_prompt():
    path = Path(__file__).parent / "fixtures" / "bundle_sealed_prompt_20260930" / "prompt.txt"
    sealed = path.read_text(encoding="utf-8")
    _, body = split_runtime_prompt(sealed)
    resources = bundle_skill._bundle_skill_resources()
    resources["references/owner-operations.md"] = (FIXTURES / "owner-operations-before.md").read_text(encoding="utf-8")
    old_instructions = "\n\n".join(f"<!-- bundled resource: {name} -->\n{content}" for name, content in resources.items())
    assert body.startswith(old_instructions)
    assert canonical_hash(resources) == _binding("before").packaged_skill_bundle_hash
    current = bundle_skill._bundle_skill_instructions() + body[len(old_instructions):]
    return sealed, body, current


def test_actual_generation_two_prompt_restores_only_frozen_resource_prefix():
    sealed, body, current = _actual_sealed_prompt()
    assert current != body
    assert bundle_skill._restore_frozen_bundle_instruction_prefix(current, sealed, _binding("before")) == body


@pytest.mark.parametrize("mutation", ["request", "resource", "marker", "package_hash", "resource_hash", "duplicate_resource"])
def test_actual_generation_two_rejects_changed_tail_or_unbound_resource_prefix(mutation):
    sealed, _, current = _actual_sealed_prompt()
    frozen = _binding("before")
    if mutation == "request":
        current += "\nChanged frozen request."
    elif mutation == "resource":
        sealed = sealed.replace("<!-- bundled resource: SKILL.md -->\n", "<!-- bundled resource: SKILL.md -->\nUnreviewed text.\n", 1)
    elif mutation == "marker":
        sealed = sealed.replace("<!-- bundled resource: SKILL.md -->", "<!-- bundled resource: other.md -->", 1)
    elif mutation == "package_hash":
        frozen = replace(frozen, packaged_skill_bundle_hash="f" * 64)
    else:
        entry = next(entry for entry in frozen.resource_bindings if entry.startswith("package:meta_research.skills.bundle_stage/SKILL.md@sha256:"))
        frozen = replace(frozen, resource_bindings=(
            frozen.resource_bindings + (entry,) if mutation == "duplicate_resource"
            else tuple(entry.rsplit(":", 1)[0] + ":" + "f" * 64 if value == entry else value for value in frozen.resource_bindings)
        ))
    with pytest.raises(BundleSkillUnavailable, match="codex_operation_spool_invalid"):
        bundle_skill._restore_frozen_bundle_instruction_prefix(current, sealed, frozen)
