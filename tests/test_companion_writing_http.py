from io import BytesIO
import json
from pathlib import Path

from docx import Document
from pptx import Presentation
import pytest

from meta_research.companion_writing import WritingRequest, writing_message
from meta_research.owners.common import OwnerConflict
from meta_research.owners.research_memory import AssetIntakeRequest

from test_public_manual_question_lifecycle import _build_runtime, _accept_root_question, DeterministicDraftingAdapter
from test_public_human_collaboration_web import _authenticated_client


def test_assistant_files_read_only_the_requested_page(tmp_path, monkeypatch):
    import meta_research.root_workspace as module
    runtime = _build_runtime(tmp_path / "runtime")
    client, headers = _authenticated_client(runtime)
    try:
        quest_ref, _ = _accept_root_question(runtime, "paged-files")
        scope = "quest:" + quest_ref
        session = runtime.owners.human_collaboration.query_companion(scope)
        assert session["session_ref"] is not None
        workspace = runtime.root_workspaces.bind_companion_session(scope, session["session_ref"]).directory
        for index in range(8):
            (workspace / f"result-{index}.txt").write_text(str(index))
        reads = []
        original = module._read_bytes
        def read(root, path):
            reads.append(path)
            return original(root, path)
        monkeypatch.setattr(module, "_read_bytes", read)
        # A later subtree is beyond the page and must not be walked at all.
        later = workspace / "z-later"
        later.mkdir()
        for index in range(8):
            (later / f"ignored-{index}.txt").write_text("not requested")
        monkeypatch.setattr(module, "_MAX_SCAN_FILES", 7)
        response = client.get("/api/v1/companion/files", params={
            "scope_ref": scope, "offset": 2, "limit": 2}, headers=headers)
        assert response.status_code == 200, response.text
        assert reads == ["result-2.txt", "result-3.txt"]
        assert response.json()["next_offset"] == 4
        def disappearing(root, path):
            if path == "result-0.txt":
                raise module.SemanticMcpError("workspace_file_unsafe")
            return original(root, path)
        monkeypatch.setattr(module, "_read_bytes", disappearing)
        first = client.get("/api/v1/companion/files", params={
            "scope_ref": scope, "limit": 2}, headers=headers).json()
        assert [item["path"] for item in first["files"]] == ["result-1.txt", "result-2.txt"]
        assert first["next_offset"] == 3
        second = client.get("/api/v1/companion/files", params={
            "scope_ref": scope, "offset": first["next_offset"], "limit": 2}, headers=headers).json()
        assert [item["path"] for item in second["files"]] == ["result-3.txt", "result-4.txt"]
    finally:
        client.close()
        runtime.close()


def test_writing_enters_new_ordinary_conversation_once_and_keeps_editable_files(tmp_path):
    provider = DeterministicDraftingAdapter()
    runtime = _build_runtime(tmp_path / "runtime", drafting=provider)
    client, headers = _authenticated_client(runtime)
    try:
        quest_ref, _ = _accept_root_question(runtime, "writing")
        scope = "quest:" + quest_ref
        human = runtime.owners.human_collaboration
        human.send_companion_message(scope, "Previous discussion", "prior-chat")
        assert human.process_drafting_once()
        before = human.query_companion(scope)
        workspace = runtime.root_workspaces.bind_companion_session(scope, before["session_ref"])
        doc = Document()
        doc.add_heading("Selected draft", 0)
        doc.add_paragraph("Existing evidence supports only the bounded result.")
        doc.save(workspace.directory / "draft-v1.docx")
        foreground = runtime.owners.advancement_engine.query_foreground(quest_ref)
        body = {"scope_ref": scope, "message": "Only revise the conclusion.", "writing_request": {
            "document_type": "report", "include_presentation": True, "task_scope": "revision",
            "requirements": "Only revise the conclusion. Keep the negative result and cite the evidence.",
            "model": {"kind": "current"}, "materials": []}}
        selected_headers = {**headers, "Idempotency-Key": "writing-selected-task"}
        response = client.post("/api/v1/companion/messages", json=body, headers=selected_headers)
        assert response.status_code == 202, response.text
        queued = response.json()
        after = human.query_companion(scope)
        assert after["session_ref"] == before["session_ref"]
        assert after["native_session_generation"] == before["native_session_generation"] + 1
        replay = client.post("/api/v1/companion/messages", json=body, headers=selected_headers)
        assert replay.status_code == 202, replay.text
        assert replay.json()["interaction_ref"] == queued["interaction_ref"]
        assert human.query_companion(scope)["native_session_generation"] == after["native_session_generation"]
        altered = {**body, "writing_request": {**body["writing_request"], "requirements": "Different task"}}
        assert client.post("/api/v1/companion/messages", json=altered, headers=selected_headers).status_code == 409
        assert human.process_drafting_once()
        request = provider.intent_requests[-1]
        assert request.native_session_ref is None
        assert "no_accepted_scientific_result" in request.message
        assert "Keep the negative result" in request.message
        assert runtime.owners.advancement_engine.query_foreground(quest_ref) == foreground
        assert not hasattr(runtime, "writing")
        listing = client.get("/api/v1/companion/files", params={"scope_ref": scope}, headers=headers)
        assert listing.status_code == 200, listing.text
        file = next(item for item in listing.json()["files"] if item["path"] == "draft-v1.docx")
        download = client.get(file["download_url"], headers=headers)
        assert download.status_code == 200
        opened = Document(BytesIO(download.content))
        assert "Existing evidence" in opened.paragraphs[1].text
        assert download.headers["content-disposition"].startswith("attachment;")
        doc.add_paragraph("A concurrent revision.")
        doc.save(workspace.directory / "draft-v1.docx")
        assert client.get(file["download_url"], headers=headers).status_code in {400, 409}
        assert client.get("/api/v1/writing").status_code == 404
    finally:
        client.close()
        runtime.close()


def test_selected_binary_materials_are_readable_exact_versions_and_replay_preserves_edits(tmp_path, monkeypatch):
    provider = DeterministicDraftingAdapter()
    runtime = _build_runtime(tmp_path / "runtime", drafting=provider)
    client, headers = _authenticated_client(runtime)
    try:
        quest_ref, _ = _accept_root_question(runtime, "writing-inputs")
        scope = "quest:" + quest_ref
        human, memory = runtime.owners.human_collaboration, runtime.owners.research_memory
        doc = Document()
        doc.add_paragraph("Selected draft with the negative result.")
        draft_path = tmp_path / "draft.docx"
        doc.save(draft_path)
        slides = Presentation()
        slides.slides.add_slide(slides.slide_layouts[0]).shapes.title.text = "Selected slide template"
        slides_path = tmp_path / "template.pptx"
        slides.save(slides_path)
        csv_path = tmp_path / "data.csv"
        csv_path.write_bytes(b"condition,value\ncontrol,2\n")

        def accept(path, key, **values):
            result = memory.submit_asset_intake(AssetIntakeRequest(source_kind="file",
                custody_mode="managed", display_name=path.name, content=path.read_bytes(), **values),
                idempotency_key=key)
            assert result.status == "accepted", result.failure_code
            return result.asset

        originals = [accept(draft_path, "selected-draft"), accept(slides_path, "selected-slides"),
            accept(csv_path, "selected-data")]
        original_bytes = [path.read_bytes() for path in (draft_path, slides_path, csv_path)]
        body = {"scope_ref": scope, "message": "Revise the selected draft using the selected template.",
            "writing_request": {"requirements": "Keep the negative result.", "materials": [
                {"version_ref": asset.version_ref, "content_hash": asset.content_hash, "role": role}
                for asset, role in zip(originals, ("draft", "presentation_template", "source"))]}}
        newer = []
        export = memory.export_asset

        def export_while_current_changes(version_ref, destination):
            if not newer:
                doc.add_paragraph("A concurrent version that was not selected.")
                doc.save(draft_path)
                newer.append(accept(draft_path, "concurrent-draft", asset_ref=originals[0].asset_ref,
                    change={"kind": "supplement", "predecessor_version_ref": originals[0].version_ref,
                        "expected_revision": 1, "explanation": "Adds a new, unselected revision."}))
            return export(version_ref, destination)

        monkeypatch.setattr(memory, "export_asset", export_while_current_changes)
        key = "selected-binary-writing"
        selected_headers = {**headers, "Idempotency-Key": key}
        response = client.post("/api/v1/companion/messages", json=body, headers=selected_headers)
        assert response.status_code == 202, response.text
        assert human.process_drafting_once()
        request = provider.intent_requests[-1]
        task, _ = json.JSONDecoder().raw_decode(request.message.split("写作任务（普通助手会话）：\n", 1)[1])
        session = human.query_companion(scope)
        workspace = runtime.root_workspaces.bind_companion_session(scope, session["session_ref"]).directory
        assert memory.query_current_asset(originals[0].asset_ref).version_ref == newer[0].version_ref
        paths = [Path(item["path"]) for item in task["materials"]]
        for item, asset, path, expected in zip(task["materials"], originals, paths, original_bytes):
            assert path.is_relative_to(workspace)
            assert path.read_bytes() == expected
            assert item["version_ref"] == asset.version_ref
            assert item["manifest_hash"] == asset.manifest_hash
            assert item["source_receipt"] == asset.receipt.as_public_dict()
        assert [p.text for p in Document(paths[0]).paragraphs] == ["Selected draft with the negative result."]
        assert Presentation(paths[1]).slides[0].shapes.title.text == "Selected slide template"
        assert paths[2].read_text() == "condition,value\ncontrol,2\n"

        # Preparation may retry after export but before queue persistence.
        retry = writing_message(WritingRequest.model_validate(body["writing_request"]), body["message"],
            memory, workspace_root=workspace, input_key=key)
        retry_task, _ = json.JSONDecoder().raw_decode(retry.split("写作任务（普通助手会话）：\n", 1)[1])
        assert [item["path"] for item in retry_task["materials"]] == [str(path) for path in paths]
        paths[0].write_bytes(b"An edited copy must never be overwritten.")
        with pytest.raises(OwnerConflict, match="writing_input_changed"):
            writing_message(WritingRequest.model_validate(body["writing_request"]), body["message"],
                memory, workspace_root=workspace, input_key=key)
        replay = client.post("/api/v1/companion/messages", json=body, headers=selected_headers)
        assert replay.status_code == 202, replay.text
        assert replay.json()["interaction_ref"] == response.json()["interaction_ref"]
        assert paths[0].read_bytes() == b"An edited copy must never be overwritten."
        assert human.query_companion(scope)["native_session_generation"] == session["native_session_generation"]
    finally:
        client.close()
        runtime.close()
