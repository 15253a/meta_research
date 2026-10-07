import base64
import copy
import hashlib
import json

import pytest
from sqlalchemy import text

from meta_research.composition import build_production_runtime
from meta_research.creation_basis import (
    FirstQuestionSynthesisResult, InitializationUnderstandingResult, empty_understanding,
)
from meta_research.paths import prepare_data_root
from meta_research.research_content import read_content
from test_public_first_question_deepfetch import (
    DeterministicDeepFetchProvider, HonestEmptyDeepFetchProvider, RecordingAcquisitionProvider,
    DeterministicProbe, _authenticate, _deepfetch_draft, _write_headers,
)
from test_public_quest_initialization import QUESTION, _confirmation_payload
from test_public_plan_stage import _DeterministicIdeaSkill
from test_root_workspace import _context, _channel, _call, _accepted


class ExistingWorkAdapter:
    def bind_workspaces(self, workspaces):
        self.workspaces = workspaces

    def understand_initialization(self, request):
        understanding = empty_understanding()
        for entry in request.manifest["entries"]:
            is_note = entry["relative_path"] == "project/notes.md"
            witnesses = []
            if is_note:
                read = self.workspaces.read_initialization(request.initialization_id, request.root_session_ref,
                    workspace_ref=entry["workspace_ref"], path=entry["path"], expected_sha256=entry["sha256"])
                assert read["content"] == b"Room improves; cold worsens. More field work remains.\n"
                witnesses = [{"material_key": entry["material_key"], "offset": 0, "length": len(read["content"]),
                    "chunk_sha256": hashlib.sha256(read["content"]).hexdigest(), "location": "notes line 1"}]
                understanding["claims_and_conditions"] = [{"ref": "room-claim", "text": "Room improves and cold worsens.",
                    "kind": "reported_work", "conditions": ["Condition matters."], "sources": witnesses}]
                understanding["unfinished_questions"] = [{"ref": "unfinished", "text": "Which condition changes the calibration effect?",
                    "kind": "agent_inference", "conditions": [], "sources": witnesses}]
            understanding["coverage"].append({"material_key": entry["material_key"], "kind": "read" if is_note else "unread",
                "read_ranges": witnesses, "unread_description": "" if is_note else "Preserve this supplied source for later reading."})
            if entry["relative_path"] != "project/raw.log":
                understanding["selection"].append({"material_key": entry["material_key"], "reason": "Retain the key notes and measurements."})
        self.prepared_understanding = copy.deepcopy(understanding)
        return InitializationUnderstandingResult(understanding)

    def synthesize_first_question(self, request):
        binding = self.workspaces.bind_initialization(request.initialization_id, request.root_session_ref)
        basis = json.loads((binding.directory / request.context["relative_root"] / "basis.json").read_text())
        assert basis["understanding"]["claims_and_conditions"][0]["text"] == "Room improves and cold worsens."
        revision = None
        question = {**QUESTION, "title": "Continue condition-specific calibration", "background_context": "Room improves and cold worsens."}
        if request.literature_snapshot is not None:
            revised = copy.deepcopy(basis["understanding"])
            empty = request.literature_snapshot["completion"] == "honest_empty"
            corrections = []
            if not empty:
                revised["claims_and_conditions"][0]["text"] = "A literature result qualifies the interpretation of the old condition difference."
                key = revised["claims_and_conditions"][0]["sources"][0]["material_key"]
                corrections = [{"prior_statement_ref": "room-claim", "disposition": "qualified",
                    "explanation": "Retain the measured room/cold difference while checking the literature boundary.",
                    "original_sources": [key], "literature_sources": [{"paper_id": "doi:10.1000/example.one", "locator": "loc-1"}]}]
                question["background_context"] = revised["claims_and_conditions"][0]["text"]
            revision = {"understanding": revised, "corrections": corrections,
                "search_assessment": "No usable papers were found within the search limits." if empty else "One read paper qualifies the interpretation. A second paper lacks full text."}
        return FirstQuestionSynthesisResult(question, "test_creation", None, None, revision)


class ReadingIdea(_DeterministicIdeaSkill):
    def generate_draft(self, request):
        context = _context(request, "idea")
        channel = _channel(self.runtime, context)
        self.understanding = _accepted(_call(self.runtime, channel, "research_memory.stage_context.read",
            context_pack_ref=request.context_pack_ref, source="creation_understanding", path=[], offset=0, limit=8192))
        sources = _accepted(_call(self.runtime, channel, "research_memory.stage_context.read",
            context_pack_ref=request.context_pack_ref, source="creation_sources", path=[], offset=0, limit=8192))
        self.sources = sources
        basis = self.runtime.owners.research_memory.creation_bases.for_question(request.question_ref)
        csv = next(source for source in basis["sources"] if source["relative_path"] == "project/data/results.csv")
        self.csv = _accepted(_call(self.runtime, channel, "research_memory.content.read",
            source_ref=csv["binding"]["version_ref"], version_ref=csv["binding"]["version_ref"], offset=0, limit=65536))
        unselected = next(source for source in basis["sources"] if source["relative_path"] == "project/raw.log")
        self.log = _accepted(_call(self.runtime, channel, "research_memory.creation_basis.read", basis_ref=basis["basis_ref"],
            expected_basis_hash=basis["basis_hash"], view="source", material_key=unselected["material_key"], offset=0, limit=65536))
        return super().generate_draft(request)


def _count(runtime, table):
    with runtime._database.read() as connection:
        return connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()


def _ready(runtime, client, headers, route):
    opened = client.post("/api/v1/quest-initializations", headers=_write_headers(headers, "open"), json={}).json()
    initialization = opened["initialization_id"]
    files = [("project/notes.md", b"Room improves; cold worsens. More field work remains.\n"),
        ("project/data/results.csv", b"condition,mae\nroom,0.3\ncold,1.85\n"), ("project/raw.log", b"Unread device observation.\n")]
    payload = {"expected_draft_revision": opened["quest_draft"]["revision"], "expected_draft_hash": opened["quest_draft"]["hash"],
        "submission_ref": "ordinary-folder", "folder": True, "complete": True,
        "files": [{"relative_path": path, "content_base64": base64.b64encode(body).decode()} for path, body in files]}
    response = client.post(f"/api/v1/quest-initializations/{initialization}/material-deliveries", headers=_write_headers(headers, "deliver"), json=payload)
    assert response.status_code == 200, response.json()
    delivered = response.json()
    assert _count(runtime, "rm_asset_versions") == 0
    assert delivered["creation_basis"] is None
    assert client.post(f"/api/v1/quest-initializations/{initialization}/material-deliveries", headers=_write_headers(headers, "deliver"), json=payload).json()["quest_draft"] == delivered["quest_draft"]
    probed = client.post(f"/api/v1/quest-initializations/{initialization}/compute-probe", headers=_write_headers(headers, "compute"), json={"selected_device_uuids": ["GPU-deepfetch-1"]}).json()
    draft = _deepfetch_draft(probed)
    draft["route"] = route
    saved = client.put(f"/api/v1/quest-initializations/{initialization}/draft", headers=_write_headers(headers, "draft"),
        json={"expected_draft_revision": probed["quest_draft"]["revision"], "expected_draft_hash": probed["quest_draft"]["hash"], "draft": draft})
    assert saved.status_code == 200, saved.json()
    view = saved.json()
    key = {"expected_draft_revision": view["quest_draft"]["revision"], "expected_draft_hash": view["quest_draft"]["hash"]}
    if route == "deepfetch":
        response = client.post(f"/api/v1/quest-initializations/{initialization}/acquisition-session", headers=_write_headers(headers, "acquisition"), json=key)
        assert response.status_code == 200, response.json()
    queued = client.post(f"/api/v1/quest-initializations/{initialization}/proposal-generations", headers=_write_headers(headers, "generate"), json=key)
    assert queued.status_code == 202, queued.json()
    if route == "deepfetch":
        assert runtime.deepfetch.process_once()
        researched = client.get(f"/api/v1/quest-initializations/{initialization}").json()
        assert researched["deepfetch"]["status"] == "succeeded", researched["deepfetch"]
        assert researched["creation_basis"]["kind"] == "prepared"
        assert _count(runtime, "rg_questions") == 0
    assert runtime.owners.human_collaboration.process_drafting_once()
    ready = client.get(f"/api/v1/quest-initializations/{initialization}").json()
    assert ready["proposal_generation"]["status"] == "succeeded", ready["proposal_generation"]
    assert _count(runtime, "rm_asset_versions") == 2
    return ready


@pytest.mark.parametrize("route,empty", [("direct", False), ("deepfetch", False), ("deepfetch", True)])
def test_existing_folder_is_read_selectively_then_confirmed_and_read_by_idea(tmp_path, route, empty):
    adapter = ExistingWorkAdapter()
    idea = ReadingIdea()
    provider = HonestEmptyDeepFetchProvider() if empty else DeterministicDeepFetchProvider()
    runtime = build_production_runtime(prepare_data_root(tmp_path / "data"), proposal_drafter=adapter,
        host_compute_probe=DeterministicProbe(), deepfetch_provider=provider,
        acquisition_provider=RecordingAcquisitionProvider(), idea_skill_provider=idea)
    idea.runtime = runtime
    client, headers = _authenticate(runtime)
    try:
        ready = _ready(runtime, client, headers, route)
        basis = ready["creation_basis"]
        assert basis["kind"] == ("prepared" if route == "direct" else "literature_revised")
        if route == "deepfetch":
            assert provider.requests[0].scope["existing_work_retrieval"]["claims_and_conditions"][0]["text"] == "Room improves and cold worsens."
            assert basis["predecessor"]["basis_ref"] == provider.requests[0].scope["creation_basis"]["basis_ref"]
            assert basis["corrections"] == [] if empty else basis["corrections"][0]["disposition"] == "qualified"
        changed = {**ready["proposal"]["content"], "title": "Human edited continuation"}
        edit = client.put(f"/api/v1/quest-initializations/{ready['initialization_id']}/proposal", headers=_write_headers(headers, "edit"), json={
            "expected_draft_revision": ready["quest_draft"]["revision"], "expected_draft_hash": ready["quest_draft"]["hash"],
            "expected_proposal_ref": ready["proposal"]["ref"], "expected_proposal_hash": ready["proposal"]["hash"], "content": changed})
        assert edit.status_code == 200, edit.json()
        ready = edit.json()
        assert ready["creation_basis"]["basis_hash"] == basis["basis_hash"]
        confirmed = client.post(f"/api/v1/quest-initializations/{ready['initialization_id']}/confirmation", headers=_write_headers(headers, "confirm"), json=_confirmation_payload(ready))
        assert confirmed.status_code == 202, confirmed.json()
        for _ in range(20):
            runtime.owners.human_collaboration.reconcile_once()
            completed = client.get(f"/api/v1/quest-initializations/{ready['initialization_id']}").json()
            if completed["status"] == "completed":
                break
        assert completed["status"] == "completed", completed.get("recovery")
        memory = runtime.owners.research_memory
        exact = memory.creation_bases.for_question(completed["question_ref"], completed["quest_ref"])
        assert exact["basis_hash"] == basis["basis_hash"]
        assert _count(runtime, "ar_target_run_activations") == 0
        question = read_content(runtime.owners.research_graph, memory, quest_ref=completed["quest_ref"],
            source_ref=completed["question_ref"], version_ref=runtime.owners.research_graph.query_question(ready["initialization_id"]).content_ref)
        assert "Human edited continuation" in json.dumps(question)
        runtime.idea_stage.start("start-idea")
        for _ in range(6):
            runtime.idea_stage.process_once()
            if hasattr(idea, "csv"):
                break
        assert idea.csv["text"] == "condition,mae\nroom,0.3\ncold,1.85\n"
        assert idea.log["text"] == "Unread device observation.\n"
        assert "Room improves" in json.dumps(idea.understanding) if route == "direct" or empty else "qualifies" in json.dumps(idea.understanding)
    finally:
        client.close()
        runtime.close()
