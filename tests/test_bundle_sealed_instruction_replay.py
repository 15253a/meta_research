"""Sealed Bundle replay stays exact within one installed resource identity."""

from dataclasses import replace
import json
from pathlib import Path

import pytest

import meta_research.bundle_skill as bundle_skill
from meta_research.bundle_skill import BundleSkillRequest, BundleSkillUnavailable, CodexBundleSkillAdapter
from meta_research.owners.common import canonical_hash
from meta_research.runtime_binding_compatibility import bundle_bindings_compatible
from meta_research.runtime_conditions import split_runtime_prompt
from test_binding_compatibility import request as dispatch_request
from test_bundle_runtime_conditions_compatibility import _SignedDispatchRunner
from test_bundle_skill_adapter import _FullConformanceAuthority, _fake_codex, _inbox_checkpoint, _plan_document, _target_plan
from test_bundle_frozen_input_recovery_compatibility import FIXTURES, _binding
from test_public_guidance_delivery import _developer


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
    assert provider.runtime_binding() == frozen
    return provider, runner, authority, dispatch, result, directory, original


def test_completed_signed_operation_replays_without_rewriting_frozen_inputs(sealed_dispatch, tmp_path, monkeypatch):
    provider, runner, authority, dispatch, first, directory, original = sealed_dispatch

    assert provider.schedule_target(dispatch) == first
    assert len(runner.calls) == 1
    assert {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()} == original
    assert authority.issued[-1]["capability_binding_hash"] == canonical_hash(dispatch.runtime_binding.as_dict())

    next_request = replace(dispatch, job_ref="bundle-job:next", generation=2)
    assert provider.schedule_target(next_request).native_session_ref == dispatch.native_session_ref
    assert len(runner.calls) == 2
    assert runner.calls[1][0][-3:] == ["resume", dispatch.native_session_ref, "-"]
    assert authority.issued[-1]["capability_binding_hash"] == canonical_hash(dispatch.runtime_binding.as_dict())
    assert {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()} == original

    # A real installed Skill revision changes the common profile identity. It
    # does not authorize old roots to migrate their frozen runtime binding.
    source = Path(_developer(runner.calls[0][0]).split("Role skill source: ", 1)[1].splitlines()[0])
    original_read = Path.read_text

    def refreshed(path, *args, **kwargs):
        content = original_read(path, *args, **kwargs)
        return content + "\nReviewed current guidance.\n" if path == source else content

    monkeypatch.setattr(Path, "read_text", refreshed)
    current = provider.runtime_binding()
    assert not bundle_bindings_compatible(dispatch.runtime_binding, current)
    for old_request in (dispatch, replace(dispatch, job_ref="bundle-job:stale-next", generation=3)):
        with pytest.raises(BundleSkillUnavailable, match="^bundle_runtime_binding_drift$"):
            provider.schedule_target(old_request)
    assert len(runner.calls) == 2
    assert {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()} == original

    plan = _plan_document()
    position = {"cycle_ref": "cycle:current", "round": 1, "cycle_focus": "New authorized work", "focus_source": {"request_ref": "fixture-current"}}
    context = {
        "schema_ref": "meta-research/bundle-context-pack/v1", "cycle_ref": "cycle:current", "cycle_position": position,
        "accepted_question_binding": {"question_ref": "question:bundle-1"},
        "accepted_formal_plan_binding": {"formal_plan_ref": "formal-plan:bundle-1", "plan_document": plan,
            "plan_document_hash": canonical_hash(plan), "answer_contract_hash": "a" * 64},
    }
    class NewRootRunner(_SignedDispatchRunner):
        def __call__(self, *args, **kwargs):
            response = super().__call__(*args, **kwargs)
            response.stdout = response.stdout.replace("codex-bundle-primary:1", "codex-bundle-current:1")
            return response

    target_plan = _target_plan(plan, canonical_hash(context))
    target_plan["context_pack_ref"] = "context-pack:current"
    target_plan["source_bindings"]["context_pack_ref"] = "context-pack:current"
    delegate = NewRootRunner([{"target_plan": target_plan}])

    new_provider = CodexBundleSkillAdapter(tmp_path / "current-provider",
        executable=str(_fake_codex(tmp_path / "current-codex")), process_runner=delegate)
    new_authority = _FullConformanceAuthority()
    new_provider.bind_full_conformance_authority(new_authority)
    new_provider.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
    new_binding = new_provider.runtime_binding()
    new_request = BundleSkillRequest(
        stage_request_ref="stage-request:current", cycle_ref="cycle:current", run_ref="bundle-run:current",
        attempt_ref="bundle-attempt:current", fence_ref="bundle-fence:current", root_session_ref="ar-session:current",
        question_ref="question:bundle-1", formal_plan_ref="formal-plan:bundle-1",
        context_pack_ref="context-pack:current", context_pack_hash=canonical_hash(context), context_pack=context,
        plan_document=plan, runtime_binding=new_binding, job_ref="bundle-job:current",
        inbox_checkpoint=_inbox_checkpoint(run_ref="bundle-run:current", attempt_ref="bundle-attempt:current", fence_ref="bundle-fence:current"),
    )
    assert new_request.native_session_ref is None
    assert new_provider.generate_draft(new_request).primary_session_ref == "codex-bundle-current:1"
    assert len(delegate.calls) == 1 and "resume" not in delegate.calls[0][0]
    assert new_authority.issued[-1]["capability_binding_hash"] == canonical_hash(new_binding.as_dict())
    assert all("Reviewed current guidance." not in _developer(argv) and "Reviewed current guidance." not in prompt
               for argv, prompt, _ in runner.calls)
    developer = _developer(delegate.calls[0][0])
    assert source.read_text(encoding="utf-8").strip() in developer
    assert "Reviewed current guidance." in developer
    assert "Reviewed current guidance." not in delegate.calls[0][1]
    assert {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()} == original
    (tmp_path / "bundle-resource-identity-observation.json").write_text(json.dumps({
        "old_calls": runner.calls, "new_calls": delegate.calls,
        "old_binding": dispatch.runtime_binding.as_dict(), "current_binding": current.as_dict(),
        "new_root_binding": new_binding.as_dict(), "original_files_unchanged": True,
    }), encoding="utf-8")


def test_model_edit_applies_to_next_call_and_preserves_sealed_replay(sealed_dispatch, monkeypatch):
    provider, runner, _, dispatch, first, directory, original = sealed_dispatch
    selected = "gpt-5.6-sol"
    monkeypatch.setattr("meta_research.idea_skill.selected_stage_model", lambda *args, **kwargs: selected)
    assert provider.schedule_target(dispatch) == first
    assert len(runner.calls) == 1
    assert {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()} == original

    next_call = replace(dispatch, job_ref="bundle-job:changed-model", generation=2)
    provider.schedule_target(next_call)
    argv = runner.calls[-1][0]
    assert argv[argv.index("--model") + 1] == selected
    assert 'model_reasoning_effort="ultra"' in argv
    path = directory.parent.parent / canonical_hash({"job_ref": next_call.job_ref}) / "dispatch-2" / "invocation.json"
    payload = json.loads(path.read_text())["payload"]
    assert payload["model_ref"] == payload["root_capability_profile"]["model_ref"] == selected
    assert payload["root_capability_profile"]["effective_reasoning_effort"] == "max"


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
