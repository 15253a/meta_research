import base64
import json

import pytest
from fastapi.testclient import TestClient

from meta_research.web import create_app
from test_root_workspace import _make, _external_context, _channel, _call, _accepted
from test_dataset_effect_scope_recovery import _scope
from test_root_human_request_lifecycle import _open_arguments


def _open(runtime, kind="offline_action"):
    scope = _scope(runtime, run_ref="reply-work", root_ref="reply-root")
    context = _external_context(scope)
    channel = _channel(runtime, context)
    opened = _accepted(_call(runtime, channel, "human_request.open",
                            **_open_arguments("reply-observations", kind)))
    return scope, context, opened


def _login(client, runtime):
    token = runtime.authentication.issue_bootstrap_token()
    result = client.post("/auth/bootstrap", json={"token": token},
                         headers={"Origin": "http://testserver"})
    assert result.status_code == 200
    return {"Origin": "http://testserver", "Idempotency-Key": "reply-submit",
            "X-CSRF-Token": result.json()["csrf_token"]}


def test_explicit_upload_and_linked_directory_deliver_exact_original_root_without_rm(tmp_path):
    runtime = _make(tmp_path / "runtime")
    try:
        scope, context, opened = _open(runtime)
        other = _external_context(_scope(runtime, run_ref="other-work", root_ref="other-root"))
        other_location = runtime.root_workspaces.bind_runtime(other).location
        original = tmp_path / "observations"
        original.mkdir()
        (original / "raw.txt").write_text("Original host observations.")
        before = runtime.owners.research_memory.query_snapshot()
        body = {"decision": "provided", "facts": {"temperature": 17},
                "note": "Exact reply. Read the uploaded folder and host originals.",
                "materials": [{"kind": "upload", "relative_path": "folder/run.csv",
                               "media_type": "text/csv", "content_base64": base64.b64encode(b"x,y\n1,7\n").decode()},
                              {"kind": "linked_local", "locator": str(original),
                               "description": "Raw observations in place."}]}
        with TestClient(create_app(runtime, base_url="http://testserver", control_key="control-key")) as client:
            headers = _login(client, runtime)
            result = client.post(f"/api/v1/human-requests/{opened['request_ref']}/responses", headers=headers, json=body)
            assert result.status_code == 201, result.text
            response = result.json()
            delivery = response["delivery"]
            assert (delivery["human_request_ref"], delivery["root_session_ref"], delivery["work_ref"], delivery["cycle_ref"]) == (opened["request_ref"], "reply-root", "reply-work", None)
            assert delivery["linked_locators"] == [{"kind": "linked_local", "locator": str(original), "description": "Raw observations in place.", "observed_kind": "directory"}]
            assert client.post(f"/api/v1/human-requests/{opened['request_ref']}/responses", headers=headers, json=body).json() == response
            body["note"] = "Changed after lost ACK."
            assert client.post(f"/api/v1/human-requests/{opened['request_ref']}/responses", headers=headers, json=body).status_code == 409
        reader = delivery["reply_reader"]
        value = _accepted(_call(runtime, _channel(runtime, context), "research_workspace.read", **reader))
        envelope = json.loads(value["text"])
        assert envelope["note"] == "Exact reply. Read the uploaded folder and host originals."
        assert envelope["facts"] == {"temperature": 17}
        upload = delivery["uploaded_readers"][0]
        assert _accepted(_call(runtime, _channel(runtime, context), "research_workspace.read", **upload))["text"] == "x,y\n1,7\n"
        request = runtime.owners.human_collaboration.query_human_request(opened["request_ref"])
        assert len(request["responses"]) == 1
        assert request["responses"][0]["delivery"] == delivery
        assert request["direct_waiters"][0]["status"] == "consumed"
        assert runtime.owners.research_memory.query_snapshot() == before
        assert (original / "raw.txt").read_text() == "Original host observations."
        assert list(other_location.directory.iterdir()) == []
    finally:
        runtime.close()


@pytest.mark.parametrize("boundary", ["before_publication", "after_publication", "after_commit"])
def test_pending_reply_is_private_and_crash_recovery_resumes_library_material(tmp_path, monkeypatch, boundary):
    from meta_research.human_reply import ProvidedReply, Upload
    from meta_research.owners.common import OwnerConflict
    from sqlalchemy import text

    class ProcessCrash(BaseException):
        pass

    path = tmp_path / "runtime"
    runtime = _make(path)
    scope, context, opened = _open(runtime, "library_reconnect")
    reply = ProvidedReply("Read this working file.", {"route": "provided_material"},
                          (Upload("paper.txt", "text/plain", b"Exact lawful fulltext.\n"),))
    before = runtime.owners.research_memory.query_snapshot()
    original = runtime.root_workspaces.deliver
    reconcile = runtime.owners.human_collaboration._reconcile_issuing_owner_human_request

    def interrupt_publication(*args, **kwargs):
        if boundary == "before_publication":
            raise ProcessCrash()
        original(*args, **kwargs)
        raise ProcessCrash()

    def interrupt_reconcile(*args, **kwargs):
        raise ProcessCrash()

    if boundary == "after_commit":
        monkeypatch.setattr(runtime.owners.human_collaboration, "_reconcile_issuing_owner_human_request", interrupt_reconcile)
    else:
        monkeypatch.setattr(runtime.root_workspaces, "deliver", interrupt_publication)
    with pytest.raises(ProcessCrash):
        runtime.owners.human_collaboration.respond_to_human_request(opened["request_ref"], reply=reply, idempotency_key="crash-submit")
    request = runtime.owners.human_collaboration.query_human_request(opened["request_ref"])
    assert request["status"] == "open"
    assert request["direct_waiters"][0]["status"] == "blocked"
    if boundary != "after_commit":
        assert request["responses"] == []
        with runtime._database.read() as connection:
            row = connection.execute(text("SELECT response_ref FROM hc_reply_deliveries")).one()
        with pytest.raises(OwnerConflict, match="human_response_receipt_invalid"):
            runtime.owners.human_collaboration._fact_verifier.verify_human_response(
                request_ref=opened["request_ref"], response_ref=row.response_ref)
    monkeypatch.setattr(runtime.root_workspaces, "deliver", original)
    monkeypatch.setattr(runtime.owners.human_collaboration, "_reconcile_issuing_owner_human_request", reconcile)
    runtime.close()
    runtime = _make(path)
    try:
        response = runtime.owners.human_collaboration.respond_to_human_request(opened["request_ref"], reply=reply, idempotency_key="crash-submit")
        request = runtime.owners.human_collaboration.query_human_request(opened["request_ref"])
        assert request["status"] == "satisfied"
        assert request["direct_waiters"][0]["status"] == "consumed"
        assert len(request["responses"]) == 1
        reconciled = _accepted(_call(runtime, _channel(runtime, context), "human_request.open.reconcile", effect_id="reply-observations"))
        assert reconciled["resolution"]["delivery"] == response["delivery"]
        assert reconciled["resolution"]["reason_code"] == "workspace_material_delivery_verified"
        exact = _accepted(_call(runtime, _channel(runtime, context), "human_request.read",
                               request_ref=opened["request_ref"], response_ref=response["response_ref"], limit=16384))
        assert json.loads(exact["text"])["response"]["delivery"] == response["delivery"]
        value = _accepted(_call(runtime, _channel(runtime, context), "research_workspace.read", **response["delivery"]["uploaded_readers"][0]))
        assert value["text"] == "Exact lawful fulltext.\n"
        assert value["root_session_ref"] == "reply-root"
        assert runtime.owners.research_memory.query_snapshot() == before
    finally:
        runtime.close()
