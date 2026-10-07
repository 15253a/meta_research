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


class PreparedBasisReadingDeepFetch(DeterministicDeepFetchProvider):
    def __init__(self, empty=False):
        super().__init__()
        self.empty = empty

    def execute(self, request):
        context = _context(request, "deepfetch")
        channel = _channel(self.runtime, context)
        binding = request.scope["creation_basis"]
        accepted = _accepted(_call(self.runtime, channel, "research_memory.creation_basis.read",
            basis_ref=binding["basis_ref"], expected_basis_hash=binding["basis_hash"], view="understanding", limit=16384))
        assert "Room improves" in json.dumps(accepted)
        rejected = _call(self.runtime, channel, "research_memory.creation_basis.read",
            basis_ref=binding["basis_ref"], expected_basis_hash="0" * 64, view="understanding")
        assert rejected["isError"]
        assert _count(self.runtime, "rg_questions") == 0
        self.requests.append(request)
        return HonestEmptyDeepFetchProvider().execute(request) if self.empty else self.result()


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
    provider = PreparedBasisReadingDeepFetch(empty)
    runtime = build_production_runtime(prepare_data_root(tmp_path / "data"), proposal_drafter=adapter,
        host_compute_probe=DeterministicProbe(), deepfetch_provider=provider,
        acquisition_provider=RecordingAcquisitionProvider(), idea_skill_provider=idea)
    idea.runtime = runtime
    provider.runtime = runtime
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


def test_workspace_path_capture_preserves_one_submission_and_rejects_unsafe_bytes(tmp_path):
    runtime = build_production_runtime(prepare_data_root(tmp_path / "data"), proposal_drafter=ExistingWorkAdapter())
    client, headers = _authenticate(runtime)
    try:
        source = tmp_path / "project"
        (source / "nested").mkdir(parents=True)
        for index in range(101):
            (source / "nested" / f"{index}.txt").write_text(f"measurement {index}")
        opened = client.post("/api/v1/quest-initializations", headers=_write_headers(headers, "open-path"), json={}).json()
        endpoint = f"/api/v1/quest-initializations/{opened['initialization_id']}"
        payload = {"expected_draft_revision": opened["quest_draft"]["revision"], "expected_draft_hash": opened["quest_draft"]["hash"],
            "submission_ref": "whole-folder", "locator": str(source)}
        captured = client.post(endpoint + "/material-paths", headers=_write_headers(headers, "capture-path"), json=payload)
        assert captured.status_code == 200, captured.json()
        view = captured.json()
        manifest = view["quest_draft"]["value"]["material_manifest"]
        assert len(manifest["entries"]) == 101
        assert len(manifest["submissions"]) == 1
        assert all(entry["origin"]["kind"] == "server_path" and entry["relative_path"].startswith("nested/") for entry in manifest["entries"])
        assert _count(runtime, "rm_asset_versions") == 0
        replay = client.post(endpoint + "/material-paths", headers=_write_headers(headers, "capture-path"), json=payload)
        assert replay.status_code == 200 and replay.json()["quest_draft"]["hash"] == view["quest_draft"]["hash"]
        key = {"expected_draft_revision": view["quest_draft"]["revision"], "expected_draft_hash": view["quest_draft"]["hash"]}
        invalid = client.post(endpoint + "/material-deliveries", headers=_write_headers(headers, "bad-content"), json={
            **key, "submission_ref": "invalid", "files": [{"relative_path": "bad.txt", "content_base64": "%%%"}]})
        assert invalid.status_code == 409 and invalid.json()["detail"]["code"] == "creation_material_content_invalid"
        unsafe = client.post(endpoint + "/material-deliveries", headers=_write_headers(headers, "bad-path"), json={
            **key, "submission_ref": "unsafe", "files": [{"relative_path": "../escape.txt", "content_base64": base64.b64encode(b"outside").decode()}]})
        assert unsafe.status_code == 409
        assert not (tmp_path / "escape.txt").exists()
    finally:
        client.close()
        runtime.close()


def test_draft_change_keeps_generation_basis_distinct_from_explicit_human_review(tmp_path):
    runtime = build_production_runtime(prepare_data_root(tmp_path / "data"), proposal_drafter=ExistingWorkAdapter(), host_compute_probe=DeterministicProbe())
    client, headers = _authenticate(runtime)
    try:
        ready = _ready(runtime, client, headers, "direct")
        generated = ready["creation_basis"]
        assert generated["human_reviewed_draft"] is None
        changed = {**ready["quest_draft"]["value"], "goal": "Narrow the follow-up to cold conditions"}
        del changed["material_manifest"]
        response = client.put(f"/api/v1/quest-initializations/{ready['initialization_id']}/draft", headers=_write_headers(headers, "change-goal"), json={
            "expected_draft_revision": ready["quest_draft"]["revision"], "expected_draft_hash": ready["quest_draft"]["hash"], "draft": changed})
        assert response.status_code == 200, response.json()
        stale = response.json()
        assert stale["proposal"]["status"] == "stale"
        assert stale["creation_basis"]["freshness"] == "stale"
        assert stale["creation_basis"]["basis_hash"] == generated["basis_hash"]
        assert len(stale["quest_draft"]["value"]["material_manifest"]["entries"]) == 3
        edit = client.put(f"/api/v1/quest-initializations/{ready['initialization_id']}/proposal", headers=_write_headers(headers, "human-review"), json={
            "expected_draft_revision": stale["quest_draft"]["revision"], "expected_draft_hash": stale["quest_draft"]["hash"],
            "expected_proposal_ref": stale["proposal"]["ref"], "expected_proposal_hash": stale["proposal"]["hash"],
            "explicit_review": True, "content": {**stale["proposal"]["content"], "title": "Reviewed under the narrowed draft"}})
        assert edit.status_code == 200, edit.json()
        reviewed = edit.json()
        assert reviewed["proposal"]["status"] == "current"
        assert reviewed["creation_basis"]["freshness"] == "stale"
        assert reviewed["creation_basis"]["human_reviewed_draft"]["hash"] == reviewed["quest_draft"]["hash"]
        assert reviewed["creation_basis"]["draft"]["hash"] != reviewed["quest_draft"]["hash"]
        source = next(source for source in generated["sources"] if source["binding"] is None)
        binding = runtime.owners.human_collaboration._creation_workspaces.bind_initialization(ready["initialization_id"], ready["intent_session"]["ref"])
        (binding.directory / source["path"]).write_bytes(b"Changed external capture")
        from meta_research.semantic_mcp import SemanticMcpError
        with pytest.raises(SemanticMcpError):
            runtime.owners.research_memory.creation_bases.read_source(generated, source["material_key"])
    finally:
        client.close()
        runtime.close()


@pytest.mark.parametrize("literature", [False, True])
def test_production_synthesis_prompt_and_spool_bind_actual_loaded_skill_and_fresh_child(tmp_path, literature):
    from meta_research.companion import CodexCompanionAdapter
    from meta_research.creation_basis import FirstQuestionSynthesisRequest, first_creation_instructions
    from test_quest_drafting_adapters import RecordingRunner
    revision = {"understanding": empty_understanding(), "corrections": [], "search_assessment": "Honest empty search preserves the prior."} if literature else None
    class JobRunner(RecordingRunner):
        def run_job(self, job_ref, argv, input_text, timeout, **kwargs):
            return self(argv, input_text, timeout)

    runner = JobRunner({"proposal_fork_native_session_ref": "fresh-child", "result": {"content": QUESTION, "revision": revision}}, thread_id="persistent-parent")
    adapter = CodexCompanionAdapter(tmp_path / "transport", process_runner=runner)
    basis = {"basis_ref": "creation_basis_test", "basis_hash": "b" * 64, "kind": "prepared"}
    result = adapter.synthesize_first_question(FirstQuestionSynthesisRequest("init", 3, "a" * 64,
        {"goal": "Continue existing work"}, "first-generation", "creation-root", "persistent-parent", basis,
        {"relative_root": "sealed/cache", "context_hash": "c" * 64, "manifest": []}, {"snapshot_ref": "exact-empty"} if literature else None))
    assert result.companion_native_session_ref == "persistent-parent"
    assert result.proposal_fork_native_session_ref == "fresh-child"
    assert result.revision == revision
    argv, prompt, _ = runner.calls[0]
    assert "resume" in argv and "persistent-parent" in argv
    assert "spawn_agent exactly once with fork_turns=all" in prompt
    assert first_creation_instructions()["bundle_hash"] in prompt
    assert "creation_basis_test" in prompt and "sealed/cache" in prompt
    schema = runner.schemas[0]
    assert set(schema["properties"]["result"]["properties"]["content"]["properties"]) == set(QUESTION)
    assert schema["properties"]["result"]["properties"]["revision"]["type"] == ("object" if literature else "null")
    spool = next((tmp_path / "transport" / "provider-operations").rglob("invocation.json"))
    assert "first-question-synthesis" in spool.read_text()


def test_production_deepfetch_prompt_loads_creation_basis_without_replacing_v4_workflow(tmp_path):
    from types import SimpleNamespace
    from meta_research.deepfetch import CodexDeepFetchAdapter, _deepfetch_skill_root, _deepfetch_skill_bundle_hash
    adapter = object.__new__(CodexDeepFetchAdapter)
    adapter._skill_root = _deepfetch_skill_root()
    scope = {"creation_basis": {"basis_ref": "precise-prior", "basis_hash": "a" * 64, "kind": "prepared"},
        "existing_work_retrieval": {"claims_and_conditions": [{"text": "Cold-condition result differs."}]}, "literature_mode": "oa_only"}
    request = SimpleNamespace(scope=scope, request_ref="request", draft_revision=4, draft_hash="b" * 64, accepted_material_bindings=())
    prompt = adapter._initial_prompt(request, public_root=tmp_path / "public", private_root=tmp_path / "private")
    assert "precise-prior" in prompt and "Cold-condition result differs." in prompt
    assert "research_memory.creation_basis.read" in prompt
    assert all(value in prompt for value in ("Radar", "Ledger", "Acquisition", "independent Readers", "Synthesis"))
    assert len(_deepfetch_skill_bundle_hash(adapter._skill_root)) == 64
    skill = (adapter._skill_root / "SKILL.md").read_text()
    assert "references/creation-basis.md" in skill
    assert (adapter._skill_root / "references/creation-basis.md").is_file()
