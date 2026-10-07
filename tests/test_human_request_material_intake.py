import pytest

from meta_research.owners.research_memory import AssetIntakeRequest
from test_public_human_collaboration_web import _authenticated_client, _open_request, _runtime, _write_headers
from test_root_human_request_lifecycle import _bundle_root_channel, _initialized_child, _open_arguments, _runtime as _root_runtime, _structured, _tool_call


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_operation_bound_linked_reply_has_no_sync_async_or_replay_intake(tmp_path, monkeypatch, kind):
    source = tmp_path / "host-original"
    if kind == "directory":
        source.mkdir()
        for index in range(105):
            (source / f"{index}.txt").write_text("Exact host result.\n")
    else:
        source.write_text("Exact host result.\n")
    runtime = _root_runtime(tmp_path / "runtime")
    try:
        def forbidden(*args, **kwargs):
            pytest.fail("Human reply attempted ResearchMemory intake.")
        for method in ("submit_asset_intake", "linked_local_intake_mode"):
            monkeypatch.setattr(runtime.owners.research_memory, method, forbidden)
        _run, channel = _bundle_root_channel(runtime)
        client, auth = _authenticated_client(runtime)
        with client:
            agent_headers = _initialized_child(client, channel.connection.token)
            opened = _structured(_tool_call(client, agent_headers, operation_id="human_request.open",
                arguments=_open_arguments("reply-host", "external_material_api_access"), request_id=10))
            url = f"/api/v1/human-requests/{opened['request_ref']}/responses"
            headers = _write_headers(auth, "reply-host")
            body = {"decision": "provided", "facts": {"claim": "Human observations."}, "note": "Read the host original.",
                    "materials": [{"kind": "linked_local", "locator": str(source), "description": "Exact source."}]}
            before = runtime.owners.research_memory.query_snapshot()
            first = client.post(url, headers=headers, json=body)
            assert first.status_code == 201, first.text
            delivery = first.json()["delivery"]
            assert delivery["cycle_ref"] is not None
            assert delivery["root_kind"] == "bundle"
            assert delivery["linked_locators"] == [{"kind": "linked_local", "locator": str(source), "description": "Exact source.", "observed_kind": kind}]
            if kind == "directory":
                for child in source.iterdir():
                    child.unlink()
                source.rmdir()
            else:
                source.unlink()
            replay = client.post(url, headers=headers, json=body)
            assert replay.status_code == 201
            assert replay.json() == first.json()
            current = runtime.owners.human_collaboration.query_human_request(opened["request_ref"])
            assert current["status"] == "satisfied"
            assert len(current["responses"]) == 1
            assert current["direct_waiters"][0]["status"] == "consumed"
            successor = _structured(_tool_call(client, agent_headers, operation_id="human_request.open",
                arguments=_open_arguments("reply-successor", "external_material_api_access", predecessor_request_ref=opened["request_ref"]), request_id=11))
            assert successor["request_ref"] != opened["request_ref"]
            stale = client.post(url, headers=_write_headers(auth, "stale-reply"), json=body)
            assert stale.status_code == 409
            assert stale.json()["detail"]["code"] == "human_request_not_current"
            assert client.post(url, headers=headers, json=body).json() == first.json()
            assert runtime.owners.research_memory.query_snapshot() == before
    finally:
        runtime.close()


def test_unbound_material_reply_is_rejected_without_public_response_or_intake(tmp_path):
    runtime = _runtime(tmp_path / "runtime")
    try:
        request = _open_request(runtime.owners.agent_runtime, request_kind="offline_action", waiter_ref="unbound",
            wait_scope="local", other_blockers=(), idempotency_key="unbound-open")
        before = runtime.owners.research_memory.query_snapshot()
        client, auth = _authenticated_client(runtime)
        with client:
            response = client.post(f"/api/v1/human-requests/{request['request_ref']}/responses",
                headers=_write_headers(auth, "unbound-response"), json={"decision": "provided", "facts": {}, "note": "Material reply.",
                "materials": [{"kind": "upload", "relative_path": "file.txt", "media_type": "text/plain", "content_base64": "b2JzZXJ2YXRpb24="}]})
            assert response.status_code == 409
            assert response.json()["detail"]["code"] == "human_response_destination_unbound"
            assert runtime.owners.human_collaboration.query_human_request(request["request_ref"])["responses"] == []
            assert runtime.owners.research_memory.query_snapshot() == before
    finally:
        runtime.close()


def test_explicit_large_linked_asset_intake_remains_available(tmp_path, monkeypatch):
    import meta_research.owners.research_memory as module
    source = tmp_path / "explicit-directory"
    source.mkdir()
    (source / "one.txt").write_text("one\n")
    (source / "two.txt").write_text("two\n")
    monkeypatch.setattr(module, "MAX_ASSET_FILES", 1)
    runtime = _runtime(tmp_path / "runtime")
    try:
        memory = runtime.owners.research_memory
        queued = memory.submit_asset_intake(AssetIntakeRequest(source_kind="local_path", custody_mode="linked_local",
            display_name=source.name, source_locator=str(source), asynchronous=True), idempotency_key="explicit-intake")
        assert queued.status == "queued"
        assert memory.process_asset_intake_once()
        accepted = memory.query_asset_intake(queued.job_ref)
        assert accepted.status == "accepted"
        assert accepted.asset.byte_count == 8
        assert accepted.asset.custody_modes == ("linked_local",)
    finally:
        runtime.close()
