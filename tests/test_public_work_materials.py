from fastapi.testclient import TestClient

from meta_research.web import create_app
from test_root_workspace import _make
from test_public_human_reply_delivery import _login
from test_public_plan_stage import _confirm_direct_quest


def _source(client, path, description="Original source."):
    result = client.get("/api/v1/server-materials/inspect", params={"path": str(path), "description": description})
    assert result.status_code == 200, result.text
    return result.json()


def test_creation_reference_is_durable_unread_idempotent_and_really_readable(tmp_path):
    location = tmp_path / "runtime"
    source = tmp_path / "project"
    source.mkdir()
    (source / "README.txt").write_text("Old project, exact content.")
    with (source / "large.bin").open("wb") as file:
        file.truncate(2 * 1024**3)
    runtime = _make(location)
    try:
        with TestClient(create_app(runtime, base_url="http://testserver", control_key="control")) as client:
            headers = _login(client, runtime)
            opened = client.post("/api/v1/quest-initializations", json={}, headers={**headers, "Idempotency-Key": "creation-open"}).json()
            anchor = opened["initialization_id"]
            receiver = runtime.owners.human_collaboration.material_receiver("creation", anchor)
            before = runtime.owners.research_memory.query_snapshot()
            command = {"receiver": receiver, "selections": [_source(client, source)], "description": "Keep this exact explanation."}
            path = f"/api/v1/quest-initializations/{anchor}/material-references"
            result = client.post(path, json=command, headers=headers)
            assert result.status_code == 201, result.text
            receipt = result.json()
            reference = receipt["references"][0]
            assert (receipt["receiver"], reference["read_state"], reference["read_ranges"]) == (receiver, "not_read", [])
            assert client.get(f"/api/v1/quest-initializations/{anchor}").json()["work_materials"] == [receipt]
            assert client.post(path, json=command, headers=headers).json() == receipt
            conflict = client.post(path, json={**command, "description": "Changed."}, headers=headers)
            assert conflict.status_code == 409
            reference_ref = reference["reference_ref"]
            assert runtime.owners.research_memory.query_snapshot() == before
    finally:
        runtime.close()
    runtime = _make(location)
    try:
        with TestClient(create_app(runtime, base_url="http://testserver", control_key="control")) as client:
            _login(client, runtime)
            ref = client.get(f"/api/v1/work-materials/{reference_ref}").json()
            assert ref["receiver"] == receiver
            assert ref["read_state"] == "not_read"
            page = client.get(f"/api/v1/work-materials/{reference_ref}/discover").json()
            small = next(entry for entry in page["entries"] if entry["name"] == "README.txt")
            value = client.get(f"/api/v1/work-materials/{reference_ref}/read", params={"path": small["path"], "observation_ref": small["observation"]["observation_ref"], "max_bytes": 12}).json()
            assert (value["text"], value["bytes"], value["eof"]) == ("Old project,", 12, False)
            readback = client.get(f"/api/v1/work-materials/{reference_ref}").json()
            assert readback["read_state"] == "read"
            assert readback["read_ranges"][0]["bytes"] == 12
            assert readback["unexpanded"] is True
            assert runtime.owners.research_memory.query_snapshot() == before
    finally:
        runtime.close()


def test_manual_context_receives_before_seed_without_material_intake(tmp_path):
    runtime = _make(tmp_path / "runtime")
    try:
        confirmed = _confirm_direct_quest(runtime)
        quest = confirmed["receipts"]["quest_goal"]["subject_ref"]
        question = confirmed["receipts"]["question_identity"]["subject_ref"]
        before = runtime.owners.research_memory.query_snapshot()
        source = tmp_path / "notes.txt"
        source.write_text("Manual prior notes.")
        with TestClient(create_app(runtime, base_url="http://testserver", control_key="control")) as client:
            headers = _login(client, runtime)
            opened = client.post("/api/v1/manual-question-creations", json={"quest_ref": quest, "parent_question_ref": question}, headers={**headers, "Idempotency-Key": "manual-open"})
            assert opened.status_code == 201, opened.text
            view = opened.json()
            context = view["context_ref"]
            receiver = runtime.owners.human_collaboration.material_receiver("manual", context)
            result = client.post(f"/api/v1/manual-question-creations/{context}/material-references", json={"receiver": receiver, "selections": [_source(client, source)], "description": "Before Seed."}, headers=headers)
            assert result.status_code == 201, result.text
            readback = client.get(f"/api/v1/manual-question-creations/{context}").json()
            assert readback["seed"] is None
            assert readback["work_materials"][0]["receiver"] == {"kind": "manual", "context_ref": context, "quest_ref": quest, "parent_question_ref": question, "generation": view["generation"]}
            reference = result.json()["references"][0]
            value = runtime.owners.human_collaboration.read_creation_material(context_ref=context, reference_ref=reference["reference_ref"], path="", observation_ref=reference["source"]["observation"]["observation_ref"])
            assert value["text"] == "Manual prior notes."
            assert runtime.owners.research_memory.query_snapshot() == before
    finally:
        runtime.close()
