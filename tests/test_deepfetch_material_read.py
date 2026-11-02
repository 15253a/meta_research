"""Real HC/AR admission and resident MCP accepted-input boundaries."""
import base64
import json
from dataclasses import replace
from pathlib import Path

import pytest

from meta_research.deepfetch import CodexDeepFetchAdapter, DeepFetchUnavailable
from meta_research.harness import HarnessAdmissionError
from meta_research.owners.research_memory import AssetIntakeRequest
from test_public_manual_question_lifecycle import (
    _build_runtime, DeterministicAcquisitionProvider, DeterministicManualDeepFetchProvider,
)
from test_root_workspace import _context, _channel, _call, _accepted


class InspectAcceptedInputs(CodexDeepFetchAdapter):
    def runtime_binding(self):
        return DeterministicManualDeepFetchProvider().runtime_binding()

    def execute(self, request):
        self.request = request
        self.inspect(request)
        self.inspected = True
        raise DeepFetchUnavailable("material_boundary_inspected")


def _accept(memory, content, key, **kwargs):
    result = memory.submit_asset_intake(AssetIntakeRequest(source_kind="file",
        custody_mode="managed", display_name="provided.pdf", media_type="application/pdf",
        content=content, **kwargs), idempotency_key=key)
    assert result.status == "accepted", result.failure_code
    return result.asset


def _queue(runtime, binding, key="provided"):
    human = runtime.owners.human_collaboration
    opened = human.create_quest({}, key + "-open")
    observed = human.observe_host_compute(opened["initialization_id"],
        ["GPU-manual-lifecycle"], key + "-compute")
    draft = dict(observed["quest_draft"]["value"])
    draft.update(goal="Read the accepted evidence.", completion_criteria="Preserve its limits.",
        time_budget="30d", route="deepfetch", background_and_initial_direction="Use supplied evidence.",
        literature={"mode": "provided_only", "library_entry_url": "", "scope_exclusions": "",
                    "accepted_material_bindings": [binding]})
    saved = human.revise_quest_draft(opened["initialization_id"], draft,
        observed["quest_draft"]["hash"], key + "-draft", observed["quest_draft"]["revision"])
    revision = saved["quest_draft"]
    human.prepare_acquisition_session(opened["initialization_id"], revision["hash"],
        key + "-acquisition", revision["revision"])
    human.generate_question_proposal(opened["initialization_id"], revision["hash"],
        key + "-proposal", revision["revision"])
    return human.query_next_deepfetch_request()


@pytest.mark.parametrize("inspection", ["reader", "native_files", "native_symlink"])
def test_current_initialization_reads_and_delivers_only_frozen_materials(tmp_path, inspection):
    provider = InspectAcceptedInputs(tmp_path / "provider")
    runtime = _build_runtime(tmp_path / "runtime", deepfetch_provider=provider,
        acquisition_provider=DeterministicAcquisitionProvider())
    memory = runtime.owners.research_memory
    original = b"%PDF-1.4\n" + bytes(range(256)) * 800
    try:
        selected = _accept(memory, original, "selected")
        foreign = _accept(memory, b"%PDF-1.4\nforeign", "foreign")
        issued = _queue(runtime, selected.as_binding().as_dict())
        newer = _accept(memory, b"%PDF-1.4\nunselected revision", "newer",
            asset_ref=selected.asset_ref, change={"kind": "supplement",
                "predecessor_version_ref": selected.version_ref, "expected_revision": 1,
                "explanation": "Concurrent revision is not this request's material."})
        before = memory.query_snapshot()

        def inspect(request):
            context = _context(request, "deepfetch")
            channel = _channel(runtime, context)
            if inspection == "reader":
                value = _accepted(_call(runtime, channel, "research_memory.content.read",
                    source_ref=selected.version_ref, version_ref=selected.version_ref, offset=8, limit=257))
                assert value["encoding"] == "base64"
                assert base64.b64decode(value["text"]) == original[8:265]
                assert value["asset_binding"] == selected.as_binding().as_dict()
                for source, version in [(selected.asset_ref, selected.version_ref),
                        (foreign.version_ref, foreign.version_ref), (newer.version_ref, newer.version_ref)]:
                    denied = _call(runtime, channel, "research_memory.content.read",
                        source_ref=source, version_ref=version)
                    assert denied["isError"]
                    assert denied["structuredContent"]["code"] == "content_source_unbound"
                denied = _call(runtime, channel, "research_memory.content.read",
                    source_ref=selected.version_ref, version_ref=selected.version_ref,
                    source_quest_ref="foreign-quest")
                assert denied["structuredContent"]["code"] == "content_quest_not_authorized"
                for bad in (replace(context, fence_ref="old-fence"),
                        replace(context, attempt_ref="old-attempt"), replace(context, root_kind="companion"),
                        replace(context, capability_binding_hash="f" * 64)):
                    with pytest.raises(HarnessAdmissionError, match="root_agent_human_request_scope_stale"):
                        _channel(runtime, bad)
                denied = _call(runtime, channel, "research_memory.literature.page")
                assert denied["structuredContent"]["code"] == "content_quest_scope_required"
            else:
                # Existing sessions may already have the original exact input
                # document. Adding native inputs must preserve that evidence.
                old_reference = provider._context_reference(request, materials=[])
                old_path = Path(next(line.split("=", 1)[1] for line in old_reference.splitlines()
                    if line.startswith("exact_scientific_input_path=")))
                old_bytes = old_path.read_bytes()
                prompt = provider._scientific_input(request)
                reference = Path(next(line.split("=", 1)[1] for line in prompt.splitlines()
                    if line.startswith("exact_scientific_input_path=")))
                materials = json.loads(reference.read_text())["provided_materials"]
                material, = materials
                path = Path(material["path"])
                assert path.is_relative_to(provider.research_workspace_path(
                    request.job_ref, context.capability_binding_hash))
                assert path.read_bytes() == original
                assert material["binding"] == selected.as_binding().as_dict()
                assert material["reader"] == {"operation": "research_memory.content.read", "args": {
                    "source_ref": selected.version_ref, "version_ref": selected.version_ref}}
                assert str(path) in prompt
                assert old_path.read_bytes() == old_bytes
                assert provider._scientific_input(request) == prompt
                # The same native root's subsequent protocol turns reuse the
                # input view; a new execution workspace gets its own view.
                assert provider._scientific_input(replace(request,
                    job_ref=request.job_ref + ":v4-turn:1")) == prompt
                resumed = replace(request, job_ref=request.job_ref + ":resumed")
                resumed_reference = provider._context_reference(resumed)
                assert resumed_reference != provider._context_reference(request)
                resumed_path = Path(next(line.split("=", 1)[1] for line in resumed_reference.splitlines()
                    if line.startswith("exact_scientific_input_path=")))
                resumed_material, = json.loads(resumed_path.read_text())["provided_materials"]
                assert Path(resumed_material["path"]).read_bytes() == original
                assert resumed_material["binding"] == material["binding"]
                assert reference.read_text() != resumed_path.read_text()
                with pytest.raises(DeepFetchUnavailable):
                    provider._scientific_input(replace(request, accepted_material_bindings=(foreign.as_binding().as_dict(),)))
                with pytest.raises(DeepFetchUnavailable):
                    provider._scientific_input(replace(request, request_ref="other-request"))
                if inspection == "native_symlink":
                    target = tmp_path / "outside.pdf"
                    target.write_bytes(original)
                    path.unlink()
                    path.symlink_to(target)
                    code = "deepfetch_input_path_invalid"
                else:
                    path.write_bytes(b"changed")
                    code = "deepfetch_input_changed"
                with pytest.raises(DeepFetchUnavailable, match=code):
                    provider._scientific_input(request)
                assert path.read_bytes() == (original if inspection == "native_symlink" else b"changed")
            assert memory.query_snapshot() == before

        provider.inspect = inspect
        with pytest.raises(DeepFetchUnavailable, match="material_boundary_inspected"):
            runtime.owners.agent_runtime.execute_deepfetch(issued, provider)
        assert provider.inspected
    finally:
        runtime.close()
