from fastapi.testclient import TestClient

from meta_research.web import create_app
from test_root_workspace import _make
from test_public_human_reply_delivery import _login
from test_public_plan_stage import _confirm_direct_quest


def test_current_guidance_and_input_save_exact_receivers_atomically_and_replay_after_source_loss(tmp_path):
    from test_public_idea_stage import _DeterministicIdeaSkill, _runtime, _confirm_direct_quest as confirm
    observed = {}
    source = tmp_path / "current-notes.txt"
    source.write_text("Current operation notes.")
    class SubmitDuringIdea(_DeterministicIdeaSkill):
        def generate_draft(self, request):
            with TestClient(create_app(self.runtime, base_url="http://testserver", control_key="control")) as client:
                headers = _login(client, self.runtime)
                receiver_response = client.get("/api/v1/work-materials/receiver", params={"quest_ref": self.quest, "question_ref": request.question_ref})
                assert receiver_response.status_code == 200, receiver_response.text
                receiver = receiver_response.json()
                assert receiver["roots"][0]["work_ref"] == request.run_ref
                selection = _source(client, source)
                command = {"receiver": receiver, "selections": [selection], "description": "Preserve exact active receiving work."}
                stale = {**command, "receiver": {**receiver, "epoch": receiver["epoch"] + 1}}
                guide = {"scope_ref": "quest:" + self.quest, "text": "  Keep original text.\n", "strength": 4, "work_materials": command}
                denied = client.post("/api/v1/human-collaboration/guidance", json={**guide, "work_materials": stale}, headers={**headers, "Idempotency-Key": "stale-guide"})
                assert denied.status_code == 409, denied.text
                assert self.runtime.owners.human_collaboration.query_collaboration_projection(("quest:" + self.quest,))["soft_constraints"] == []
                accepted = client.post("/api/v1/human-collaboration/guidance", json=guide, headers={**headers, "Idempotency-Key": "current-guide"})
                assert accepted.status_code == 201, accepted.text
                assert accepted.json()["guidance"] == {"text": guide["text"], "strength": 4}
                assert accepted.json()["work_materials"][0]["receiver"] == receiver
                input_body = {"quest_ref": self.quest, "question_ref": request.question_ref, "text": "Original proactive input.", "work_materials": command}
                accepted_input = client.post("/api/v1/research-inputs", json=input_body, headers={**headers, "Idempotency-Key": "current-input"})
                assert accepted_input.status_code == 201, accepted_input.text
                input_value = accepted_input.json()
                readback = client.get(f"/api/v1/research-inputs/{input_value['input_ref']}", params={"quest_ref": self.quest})
                assert readback.status_code == 200, readback.text
                assert readback.json()["work_materials"][0]["receiver"] == receiver
                assert readback.json()["text"] == input_body["text"]
                source.unlink()
                assert client.post("/api/v1/human-collaboration/guidance", json=guide, headers={**headers, "Idempotency-Key": "current-guide"}).json() == accepted.json()
                assert client.post("/api/v1/research-inputs", json=input_body, headers={**headers, "Idempotency-Key": "current-input"}).json() == input_value
                observed["receiver"] = receiver
            return super().generate_draft(request)
    provider = SubmitDuringIdea()
    runtime = _runtime(tmp_path / "runtime", provider)
    provider.runtime = runtime
    try:
        provider.quest = confirm(runtime)["quest_ref"]
        before = runtime.owners.research_memory.query_snapshot()
        for _ in range(6):
            runtime.idea_stage.process_once()
            if observed:
                break
        assert observed
        assert runtime.owners.research_memory.query_snapshot() == before
    finally:
        runtime.close()


def test_pending_original_references_are_private_and_recover_without_duplication(tmp_path, monkeypatch):
    import pytest
    from sqlalchemy import text
    from meta_research.human_reply import ProvidedReply, ServerReference
    from meta_research.owners.common import OwnerConflict
    from test_public_human_reply_delivery import _open
    class ProcessCrash(BaseException):
        pass
    location = tmp_path / "runtime"
    source = tmp_path / "original.txt"
    source.write_text("Original remains in place.")
    runtime = _make(location)
    _, _, opened = _open(runtime)
    selection = runtime.root_workspaces.server_files.inspect(str(source))
    reply = ProvidedReply("Read original.", {}, (ServerReference(selection),))
    deliver = runtime.root_workspaces.deliver
    def crash_after_publication(*args, **kwargs):
        deliver(*args, **kwargs)
        raise ProcessCrash()
    monkeypatch.setattr(runtime.root_workspaces, "deliver", crash_after_publication)
    with pytest.raises(ProcessCrash):
        runtime.owners.human_collaboration.respond_to_human_request(opened["request_ref"], reply=reply, idempotency_key="recover-reference")
    with runtime._database.read() as connection:
        ref = connection.execute(text("SELECT reference_ref FROM hc_work_material_references")).scalar_one()
        assert connection.execute(text("SELECT state FROM hc_work_material_submissions")).scalar_one() == "pending"
    with pytest.raises(OwnerConflict, match="material_reference_not_found"):
        runtime.owners.human_collaboration.query_work_material(ref)
    assert runtime.owners.human_collaboration.query_work_materials("request", opened["request_ref"]) == []
    monkeypatch.setattr(runtime.root_workspaces, "deliver", deliver)
    runtime.close()
    runtime = _make(location)
    try:
        saved = runtime.owners.human_collaboration.query_work_material(ref)
        assert saved["read_state"] == "not_read"
        response = runtime.owners.human_collaboration.respond_to_human_request(opened["request_ref"], reply=reply, idempotency_key="recover-reference")
        assert response["delivery"]["work_materials"] == [{"reference_ref": ref}]
        with runtime._database.read() as connection:
            assert connection.execute(text("SELECT count(*) FROM hc_work_material_references")).scalar_one() == 1
            assert connection.execute(text("SELECT state FROM hc_work_material_submissions")).scalar_one() == "ready"
    finally:
        runtime.close()


def test_original_request_reference_is_readable_through_actual_root_and_child_mcp(tmp_path):
    import json
    import socket
    import subprocess
    import sys
    import threading
    import time
    import uvicorn
    from test_public_human_reply_delivery import _open
    from test_root_workspace import _channel, _call, _accepted, _external_context
    from test_dataset_effect_scope_recovery import _scope
    runtime = _make(tmp_path / "runtime")
    server = None
    try:
        scope, context, opened = _open(runtime)
        original = tmp_path / "observations"
        original.mkdir()
        (original / "small.txt").write_text("Original observation, 17 degrees.")
        before = runtime.owners.research_memory.query_snapshot()
        app = create_app(runtime, base_url="http://testserver", control_key="control")
        with TestClient(app) as client:
            headers = _login(client, runtime)
            body = {"decision": "provided", "facts": {}, "note": "Read this original directory.",
                    "materials": [{"kind": "server_reference", "selection": _source(client, original)}]}
            result = client.post(f"/api/v1/human-requests/{opened['request_ref']}/responses", json=body, headers=headers)
            assert result.status_code == 201, result.text
            response = result.json()
            assert client.post(f"/api/v1/human-requests/{opened['request_ref']}/responses", json=body, headers=headers).json() == response
            reference_ref = response["delivery"]["work_materials"][0]["reference_ref"]
            saved = client.get(f"/api/v1/work-materials/{reference_ref}").json()
            assert (saved["receiver"]["kind"], saved["receiver"]["request_ref"], saved["receiver"]["roots"][0]["work_ref"]) == ("request", opened["request_ref"], scope["run_ref"])
            assert saved["read_state"] == "not_read"
        channel = _channel(runtime, context)
        status, catalog, _ = runtime.harnesses.dispatch_mcp_http(channel.connection.token,
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, mcp_session_id=None)
        assert status == 200
        assert {"research_workspace.materials.discover", "research_workspace.materials.read"}.issubset({item["name"] for item in catalog["result"]["tools"]})
        listed = _accepted(_call(runtime, channel, "research_workspace.materials.discover"))
        assert [item["reference_ref"] for item in listed["references"]] == [reference_ref]
        page = _accepted(_call(runtime, channel, "research_workspace.materials.discover", reference_ref=reference_ref))
        entry = page["entries"][0]
        child_arguments = {"reference_ref": reference_ref, "path": entry["path"], "observation_ref": entry["observation"]["observation_ref"], "max_bytes": 20}
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
        thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
        thread.start()
        for _ in range(100):
            if server.started:
                break
            time.sleep(.02)
        assert server.started
        child = """
import json, sys, urllib.request
url, token, arguments = sys.argv[1:]
def call(payload):
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={
        'Host': 'testserver', 'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json',
        'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': '2025-06-18'})
    return json.loads(urllib.request.urlopen(request, timeout=10).read())
catalog = call({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
assert {'research_workspace.materials.discover', 'research_workspace.materials.read'}.issubset(
    {item['name'] for item in catalog['result']['tools']})
print(json.dumps(call({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
    'params': {'name': 'research_workspace.materials.read', 'arguments': json.loads(arguments)}})))
"""
        output = subprocess.check_output([sys.executable, "-c", child, f"http://127.0.0.1:{sock.getsockname()[1]}/mcp", channel.connection.token, json.dumps(child_arguments)], text=True, timeout=20)
        value = json.loads(output)["result"]["structuredContent"]
        assert (value["text"], value["bytes"]) == ("Original observation", 20)
        server.should_exit = True
        thread.join(5)
        assert not thread.is_alive()
        foreign = _external_context(_scope(runtime, run_ref="foreign-work", root_ref="foreign-root"))
        denied = _call(runtime, _channel(runtime, foreign), "research_workspace.materials.read", **child_arguments)
        assert denied["structuredContent"]["code"] == "material_not_visible"
        assert runtime.owners.human_collaboration.query_work_material(reference_ref)["read_ranges"][0]["bytes"] == 20
        assert runtime.owners.research_memory.query_snapshot() == before
    finally:
        if server is not None:
            server.should_exit = True
        runtime.close()


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
            moved = source.with_name("temporarily-moved")
            source.rename(moved)
            assert client.get(f"/api/v1/work-materials/{reference_ref}/discover").status_code == 409
            assert client.get(f"/api/v1/work-materials/{reference_ref}").json()["availability"] == "material_missing"
            moved.rename(source)
            page = client.get(f"/api/v1/work-materials/{reference_ref}/discover").json()
            restored = client.get(f"/api/v1/work-materials/{reference_ref}").json()
            assert (restored["availability"], restored["read_state"]) == ("available", "not_read")
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
            reopened = client.get("/api/v1/manual-question-creations/current", params={"quest_ref": quest, "parent_question_ref": question})
            assert reopened.status_code == 200, reopened.text
            assert reopened.json()["work_materials"] == readback["work_materials"]
            reference = result.json()["references"][0]
            value = runtime.owners.human_collaboration.read_creation_material(context_ref=context, reference_ref=reference["reference_ref"], path="", observation_ref=reference["source"]["observation"]["observation_ref"])
            assert value["text"] == "Manual prior notes."
            assert runtime.owners.research_memory.query_snapshot() == before
    finally:
        runtime.close()
