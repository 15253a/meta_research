"""Task selection, restricted credentials and observable protocol traffic."""
import json
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from threading import Thread

import pytest

from meta_research.companion_writing import WritingRequest, writing_message
from meta_research.writing_content import generate


@contextmanager
def observed_endpoint(response, *, status=200, after_request=None):
    """A real HTTP peer; records only synthetic test inputs in memory."""
    observed = []

    class Endpoint(BaseHTTPRequestHandler):
        def do_POST(self):
            observed.append({"path": self.path, "headers": dict(self.headers),
                "body": json.loads(self.rfile.read(int(self.headers["Content-Length"])))})
            if after_request is not None:
                after_request()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            if 300 <= status < 400:
                self.send_header("Location", "/unselected")
            self.end_headers()
            self.wfile.write(response if isinstance(response, bytes) else json.dumps(response).encode())

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Endpoint) as server:
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/selected", observed
        finally:
            server.shutdown()
            worker.join()


def restricted_environment(tmp_path, endpoint, **scope):
    private = tmp_path / "credentials.json"
    private.write_text(json.dumps({"writing-key": {"api_key_env": "WRITER_KEY",
        "endpoints": [endpoint], **scope}}))
    return {"META_RESEARCH_CONTENT_CREDENTIALS": str(private), "WRITER_KEY": "private-test-key"}


def task_selection(endpoint, protocol="openai_chat_completions", **values):
    return {"kind": "task_api", "endpoint": endpoint, "model": "chosen",
        "protocol": protocol, "api_key_ref": "writing-key", **values}


@pytest.mark.parametrize("protocol, response", [
    ("openai_chat_completions", {"choices": [{"finish_reason": "stop", "message": {"content": "One observed failure."}}]}),
    ("openai_responses", {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "One observed failure."}]}]}),
    ("anthropic_messages", {"type": "message", "stop_reason": "end_turn", "content": [{"type": "text", "text": "One observed failure."}]}),
])
def test_task_protocol_makes_one_observable_request_and_returns_text(tmp_path, protocol, response):
    with observed_endpoint(response) as (url, calls):
        selection = task_selection(url, protocol, instructions="Retain the negative result.")
        content = generate(selection, {"messages": [{"role": "user", "content": "Only this authorized excerpt."}]},
            environment=restricted_environment(tmp_path, url, models=["chosen"], quest_refs=["quest-one"]),
            quest_ref="quest-one")
    assert content == "One observed failure."
    assert len(calls) == 1
    call = calls[0]
    assert call["path"] == "/selected"
    assert call["body"]["model"] == "chosen"
    headers = {key.lower(): value for key, value in call["headers"].items()}
    if protocol == "anthropic_messages":
        assert headers["x-api-key"] == "private-test-key"
        assert headers["anthropic-version"] == "2023-06-01"
        assert "authorization" not in headers
        assert call["body"]["system"] == "Retain the negative result."
        assert call["body"]["messages"] == [{"role": "user", "content": "Only this authorized excerpt."}]
        assert call["body"]["max_tokens"] > 0
    else:
        assert headers["authorization"] == "Bearer private-test-key"
        key = "input" if protocol == "openai_responses" else "messages"
        assert call["body"][key] == [{"role": "system", "content": "Retain the negative result."},
            {"role": "user", "content": "Only this authorized excerpt."}]
    assert "private-test-key" not in json.dumps(call["body"])
    assert "private-test-key" not in content


def test_task_api_reaches_selected_protocol_without_model_preconfiguration(tmp_path):
    selection = {"kind": "task_api", "endpoint": "https://writer.example/v1/responses",
                 "model": "chosen", "protocol": "openai_responses", "api_key_ref": "writing-key",
                 "instructions": "Keep the negative result."}
    task = WritingRequest(requirements="Revise the paragraph", model=selection)
    assert selection.items() <= json.loads(writing_message(task, "Revise", None).split(
        "写作任务（普通助手会话）：\n")[1].split("\n使用共用")[0])["model"].items()
    private = tmp_path / "credentials.json"
    private.write_text(json.dumps({"writing-key": {"api_key_env": "WRITER_KEY",
        "endpoints": [selection["endpoint"]]}}))
    calls = []
    def endpoint(call, *, timeout):
        calls.append(call)
        return BytesIO(b'{"status":"completed","output":[{"type":"message","content":[{"type":"output_text","text":"Bounded evidence."}]}]}')
    result = generate(selection, {"messages": [{"role": "user", "content": "One failed case."}]},
        environment={"META_RESEARCH_CONTENT_CREDENTIALS": str(private), "WRITER_KEY": "private-test-key"},
        opener=endpoint)
    assert result == "Bounded evidence."
    assert json.loads(calls[0].data) == {"model": "chosen", "input": [
        {"role": "system", "content": "Keep the negative result."},
        {"role": "user", "content": "One failed case."}]}
    assert "private-test-key" not in calls[0].data.decode()
    assert calls[0].get_header("Authorization") == "Bearer private-test-key"


@pytest.mark.parametrize("kind", ["configured_api", "task_api"])
def test_existing_explicit_http_endpoint_contract_is_preserved(tmp_path, kind):
    url = "http://writer.internal/v1/chat/completions"
    env = restricted_environment(tmp_path, url)
    selected = task_selection(url)
    if kind == "configured_api":
        config = tmp_path / "models.json"
        config.write_text(json.dumps({"selected": {"endpoint": url, "model": "chosen", "api_key_env": "WRITER_KEY"}}))
        env["META_RESEARCH_CONTENT_MODELS"] = str(config)
        selected = "selected"
    calls = []
    def endpoint(call, *, timeout):
        calls.append(call.full_url)
        return BytesIO(b'{"choices":[{"message":{"content":"Bounded result."}}]}')
    assert generate(selected, {"messages": [{"role": "user", "content": "Evidence."}]},
        environment=env, opener=endpoint) == "Bounded result."
    assert calls == [url]


@pytest.mark.parametrize("status, reason", [(401, "authentication_failed"),
    (403, "authorization_denied"), (429, "rate_limited"), (503, "service_unavailable"),
    (408, "timeout"), (504, "timeout"), (302, "endpoint_redirect_rejected")])
def test_task_http_failure_is_distinguishable_safe_and_not_retried(tmp_path, status, reason):
    with observed_endpoint(b"private-test-key private-evidence", status=status) as (url, calls):
        with pytest.raises(ValueError, match="content_model_call_failed") as caught:
            generate(task_selection(url), {"messages": [{"role": "user", "content": "private-evidence"}]},
                environment=restricted_environment(tmp_path, url))
    assert len(calls) == 1
    details = caught.value.details
    assert details["reason_code"] == reason
    assert details["http_status"] == status
    assert details["correction"]
    assert "private-test-key" not in str(caught.value) + json.dumps(details)
    assert "private-evidence" not in str(caught.value) + json.dumps(details)


@pytest.mark.parametrize("protocol, response", [
    ("openai_chat_completions", b"not-json private-test-key"),
    ("openai_chat_completions", {"choices": []}),
    ("openai_chat_completions", {"choices": [{"finish_reason": "length", "message": {"content": "Truncated evidence"}}]}),
    ("openai_chat_completions", {"choices": [{"message": {"content": "Refused", "refusal": "private-evidence"}}]}),
    ("openai_responses", {"status": "incomplete", "output": [{"type": "message", "content": [{"type": "output_text", "text": "Partial"}]}]}),
    ("openai_responses", {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "private-evidence"}]}]}),
    ("openai_responses", {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": " "}]}]}),
    ("anthropic_messages", {"stop_reason": "max_tokens", "content": [{"type": "text", "text": "Partial"}]}),
    ("anthropic_messages", {"stop_reason": "end_turn", "content": [{"type": "tool_use", "name": "unrequested-tool"}]}),
    ("anthropic_messages", {"stop_reason": "end_turn", "content": []}),
    ("anthropic_messages", {"stop_reason": "end_turn", "content": [None]}),
])
def test_task_refusal_partial_and_bad_protocol_response_never_becomes_delivered_text(tmp_path, protocol, response):
    with observed_endpoint(response) as (url, calls):
        with pytest.raises(ValueError, match="content_model_call_failed") as caught:
            generate(task_selection(url, protocol), {"messages": [{"role": "user", "content": "private-evidence"}]},
                environment=restricted_environment(tmp_path, url))
    assert len(calls) == 1
    assert caught.value.details["reason_code"] == "invalid_response"
    assert caught.value.details["category"] == "tool_contract"
    assert "private-test-key" not in json.dumps(caught.value.details)
    assert "private-evidence" not in json.dumps(caught.value.details)


@pytest.mark.parametrize("changed", [{"endpoint": "https://unselected.example/write"},
    {"model": "unselected-model"}, {"quest_ref": "quest-other"}, {"quest_ref": None}])
def test_restricted_credential_cannot_leave_its_exact_destination_model_or_quest(tmp_path, changed):
    with observed_endpoint({}) as (url, calls):
        selection = task_selection(url)
        selection.update({key: value for key, value in changed.items() if key != "quest_ref"})
        with pytest.raises(ValueError, match="content_model_credential_scope_denied"):
            generate(selection, {"messages": [{"role": "user", "content": "selected evidence"}]},
                environment=restricted_environment(tmp_path, url, models=["chosen"], quest_refs=["quest-one"]),
                quest_ref=changed.get("quest_ref", "quest-one"))
    assert calls == []


@pytest.mark.parametrize("kind", ["missing_registry", "missing_reference", "missing_environment"])
def test_missing_task_credential_fails_before_call_and_keeps_selection(tmp_path, kind):
    with observed_endpoint({}) as (url, calls):
        env = restricted_environment(tmp_path, url)
        selection = task_selection(url)
        if kind == "missing_registry":
            env.pop("META_RESEARCH_CONTENT_CREDENTIALS")
        elif kind == "missing_reference":
            selection["api_key_ref"] = "not-configured"
        else:
            env.pop("WRITER_KEY")
        with pytest.raises(ValueError, match="content_model_credential_unavailable"):
            generate(selection, {"messages": [{"role": "user", "content": "selected evidence"}]}, environment=env)
    assert calls == []
    assert selection["model"] == "chosen"


def test_private_credential_http_requires_auth_csrf_and_never_echoes_invalid_secrets(tmp_path):
    from fastapi.testclient import TestClient
    from meta_research.web import create_app
    from test_public_manual_question_lifecycle import _build_runtime, _accept_root_question
    from test_public_human_collaboration_web import _authenticated_client

    runtime = _build_runtime(tmp_path / "runtime")
    client, headers = _authenticated_client(runtime)
    try:
        quest, _ = _accept_root_question(runtime, "credential-http")
        url = "/api/v1/companion/content-credentials"
        body = {"scope_ref": "quest:" + quest, "endpoint": "https://writer.example/v1/responses",
            "model": "chosen", "api_key": "private-test-key"}
        with TestClient(create_app(runtime, base_url="http://testserver", control_key="control-secret"),
                base_url="http://testserver") as anonymous:
            denied = anonymous.post(url, json=body, headers={"Origin": "http://testserver"})
            assert denied.status_code == 401
        denied = client.post(url, json=body, headers={"Origin": "http://testserver"})
        assert denied.status_code == 403
        assert denied.json()["detail"]["code"] == "csrf_invalid"
        for invalid in ({**body, "api_key": "private-test-key\r\nInjected: value"},
                {**body, "endpoint": "https://private-test-key@writer.example/path"},
                {**body, "api_key": {"secret": "private-test-key"}},
                {**body, "extra": "private-test-key"}, ["private-test-key"]):
            response = client.post(url, json=invalid, headers=headers)
            assert response.status_code == 422, response.text
            assert "private-test-key" not in response.text
        valid = client.post(url, json=body, headers=headers)
        assert valid.status_code == 201, valid.text
        assert set(valid.json()) == {"api_key_ref"}
        assert "private-test-key" not in valid.text
        listing = client.get("/api/v1/companion/files", params={"scope_ref": "quest:" + quest}, headers=headers)
        assert listing.status_code == 200
        assert "private-test-key" not in listing.text
        assert "writing-credentials" not in listing.text
        # A reference is usable only by the private daemon store, never a file download.
        assert client.get(url, headers=headers).status_code == 405
    finally:
        client.close()
        runtime.close()


def test_http_task_selection_private_credential_and_actual_mcp_survive_restart_and_followup(tmp_path):
    from types import SimpleNamespace
    from meta_research.quest_drafting import IntentTurnResult
    from test_public_manual_question_lifecycle import _build_runtime, _accept_root_question, DeterministicDraftingAdapter
    from test_public_human_collaboration_web import _authenticated_client
    from test_root_workspace import _channel, _external_context, _call, _accepted

    class Provider(DeterministicDraftingAdapter):
        def __init__(self):
            super().__init__()
            self.runtime = None
            self.observations = []

        def runtime_binding(self):
            return SimpleNamespace(as_dict=lambda: {"provider_ref": "test/task-writing", "provider_version": "1"})

        def reply(self, request):
            self.intent_requests.append(request)
            chosen = self.runtime.owners.human_collaboration.query_companion_writing_model(request.root_runtime_scope["run_ref"])
            if chosen is not None:
                channel = _channel(self.runtime, _external_context(request.root_runtime_scope))
                mismatch = _call(self.runtime, channel, "assistant.content.generate",
                        model={**{key: value for key, value in chosen.items() if value is not None}, "model": "unselected-model"},
                    messages=[{"role": "user", "content": "Only selected evidence."}])
                assert mismatch.get("isError")
                assert "content_model_selection_mismatch" in str(mismatch)
                result = _accepted(_call(self.runtime, channel, "assistant.content.generate",
                    messages=[{"role": "user", "content": "Only selected evidence."}]))
                self.observations.append(result)
            return IntentTurnResult("The chosen model returned the requested bounded text.",
                request.native_session_ref or "task-writing-native", "test_task_writing")

    with observed_endpoint({"status": "completed", "output": [{"type": "message",
            "content": [{"type": "output_text", "text": "A bounded result, not a finished file."}]}]}) as (url, calls):
        provider = Provider()
        data_root = tmp_path / "runtime"
        runtime = _build_runtime(data_root, drafting=provider)
        provider.runtime = runtime
        client, headers = _authenticated_client(runtime)
        try:
            quest, _ = _accept_root_question(runtime, "writer-binding")
            scope = "quest:" + quest
            private = client.post("/api/v1/companion/content-credentials", json={"scope_ref": scope,
                "endpoint": url, "model": "chosen", "api_key": "private-test-key"}, headers=headers)
            assert private.status_code == 201, private.text
            selected = task_selection(url, "openai_responses", api_key_ref=private.json()["api_key_ref"])
            body = {"scope_ref": scope, "message": "Write a bounded paragraph.", "writing_request": {
                "requirements": "Preserve limitations.", "task_scope": "outline", "model": selected}}
            response = client.post("/api/v1/companion/messages", json=body,
                headers={**headers, "Idempotency-Key": "bound-writing"})
            assert response.status_code == 202, response.text
            original = response.json()["interaction_ref"]
            human = runtime.owners.human_collaboration
            assert selected.items() <= human.query_companion_writing_model(original).items()
            replay = client.post("/api/v1/companion/messages", json=body,
                headers={**headers, "Idempotency-Key": "bound-writing"})
            assert replay.json()["interaction_ref"] == original
            assert human.process_drafting_once()
            assert len(calls) == 1
            assert calls[0]["body"] == {"model": "chosen", "input": [{"role": "user", "content": "Only selected evidence."}]}
            assert "private-test-key" not in provider.intent_requests[-1].message
            assert "private-test-key" not in json.dumps(human.query_companion(scope))
            rotate = {"scope_ref": scope, "endpoint": url, "model": "chosen",
                "api_key": "rotated-private-key", "api_key_ref": selected["api_key_ref"]}
            refused = client.post("/api/v1/companion/content-credentials",
                json={**rotate, "endpoint": "https://unselected.example/content"}, headers=headers)
            assert refused.status_code == 422
            repaired = client.post("/api/v1/companion/content-credentials", json=rotate, headers=headers)
            assert repaired.status_code == 201, repaired.text
            assert repaired.json() == private.json()
            assert "rotated-private-key" not in repaired.text
            followup = human.send_companion_message(scope, "Revise only the final sentence.", "bound-followup")
            assert selected.items() <= human.query_companion_writing_model(followup["interaction_ref"]).items()
            assert human.process_drafting_once()
            assert len(calls) == 2
            assert calls[-1]["headers"]["Authorization"] == "Bearer rotated-private-key"
            assert provider.intent_requests[-1].native_session_ref == "task-writing-native"
        finally:
            client.close()
            runtime.close()
        recovered = Provider()
        runtime = _build_runtime(data_root, drafting=recovered)
        recovered.runtime = runtime
        try:
            human = runtime.owners.human_collaboration
            assert selected.items() <= human.query_companion_writing_model(original).items()
            continued = human.send_companion_message(scope, "Continue with the same selected model.", "after-restart")
            assert selected.items() <= human.query_companion_writing_model(continued["interaction_ref"]).items()
            assert human.process_drafting_once()
            assert len(calls) == 3
            assert calls[-1]["headers"]["Authorization"] == "Bearer rotated-private-key"
            assert recovered.intent_requests[-1].native_session_ref == "task-writing-native"
            assert recovered.observations[-1]["content"] == "A bounded result, not a finished file."
            assert recovered.observations[-1]["model"] == selected
            human.start_new_companion_session(scope, "fresh-conversation")
            fresh = human.send_companion_message(scope, "Explain the existing files.", "fresh-message")
            assert human.query_companion_writing_model(fresh["interaction_ref"]) is None
            assert human.process_drafting_once()
            assert len(calls) == 3
            assert recovered.intent_requests[-1].native_session_ref is None
        finally:
            runtime.close()


def test_bound_current_model_cannot_silently_call_an_external_model(tmp_path, monkeypatch):
    from test_public_manual_question_lifecycle import _build_runtime, _accept_root_question
    from test_dataset_effect_scope_recovery import _scope
    from test_root_workspace import _channel, _external_context, _call
    runtime = _build_runtime(tmp_path / "runtime")
    try:
        quest, _ = _accept_root_question(runtime, "current-choice")
        human = runtime.owners.human_collaboration
        turn = human.send_companion_message("quest:" + quest, "Write with the current model.",
            "current-choice-message", writing_request={"requirements": "Keep the limitations.", "model": {"kind": "current"}})
        scope = _scope(runtime, quest_ref=quest, run_ref=turn["interaction_ref"],
            root_ref=human.query_companion("quest:" + quest)["session_ref"])
        channel = _channel(runtime, _external_context(scope))
        with observed_endpoint({}) as (url, calls):
            for key, value in restricted_environment(tmp_path, url).items():
                monkeypatch.setenv(key, value)
            response = _call(runtime, channel, "assistant.content.generate", model=task_selection(url),
                messages=[{"role": "user", "content": "A task the current model should write."}])
            assert response.get("isError")
            assert "content_model_selection_mismatch" in str(response)
        assert calls == []
        assert human.query_companion_writing_model(turn["interaction_ref"]) == {"kind": "current"}
    finally:
        runtime.close()


def test_missing_private_credential_can_be_restored_only_for_the_original_task(tmp_path):
    from test_public_manual_question_lifecycle import _build_runtime, _accept_root_question
    from test_public_human_collaboration_web import _authenticated_client
    runtime = _build_runtime(tmp_path / "runtime")
    client, headers = _authenticated_client(runtime)
    try:
        quest, _ = _accept_root_question(runtime, "restore-writing-key")
        scope = "quest:" + quest
        route = "/api/v1/companion/content-credentials"
        body = {"scope_ref": scope, "endpoint": "https://writer.example/responses",
                "model": "chosen", "api_key": "old-private-key"}
        reference = client.post(route, json=body, headers=headers).json()["api_key_ref"]
        task = {"kind": "task_api", "endpoint": body["endpoint"], "model": "chosen",
                "protocol": "openai_responses", "api_key_ref": reference}
        turn = runtime.owners.human_collaboration.send_companion_message(scope, "Write a paragraph.",
            "restore-writing-task", writing_request={"requirements": "Preserve limits.", "model": task})
        (runtime.data_root.root / "writing-credentials" / (reference + ".json")).unlink()
        repair = {**body, "api_key": "new-private-key", "api_key_ref": reference}
        wrong = client.post(route, json={**repair, "endpoint": "https://unselected.example/path"}, headers=headers)
        assert wrong.status_code == 422
        restored = client.post(route, json=repair, headers=headers)
        assert restored.status_code == 201, restored.text
        assert restored.json() == {"api_key_ref": reference}
        assert runtime.owners.human_collaboration.query_companion_writing_model(turn["interaction_ref"]) == task
        assert "new-private-key" not in restored.text
    finally:
        client.close()
        runtime.close()


@pytest.mark.parametrize("retire_at", ["before_request", "before_response"])
def test_retired_root_cannot_call_or_deliver_a_content_result(tmp_path, monkeypatch, retire_at):
    from test_public_manual_question_lifecycle import _build_runtime, _accept_root_question
    from test_dataset_effect_scope_recovery import _scope
    from test_root_workspace import _channel, _external_context, _call
    runtime = _build_runtime(tmp_path / "runtime")
    try:
        quest, _ = _accept_root_question(runtime, "retired-writer")
        scope = _scope(runtime, quest_ref=quest, run_ref="writer-retirement", root_ref="writer-root")
        channel = _channel(runtime, _external_context(scope))

        def retire():
            runtime.owners.agent_runtime.complete_external_root_task_scope(root_kind="companion", root_runtime_scope=scope)

        with observed_endpoint({"choices": [{"message": {"content": "Must not deliver after retirement."}}]},
                after_request=retire if retire_at == "before_response" else None) as (url, calls):
            for key, value in restricted_environment(tmp_path, url).items():
                monkeypatch.setenv(key, value)
            if retire_at == "before_request":
                retire()
            status, response, _ = runtime.harnesses.dispatch_mcp_http(channel.connection.token,
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                    "name": "assistant.content.generate", "arguments": {"model": task_selection(url),
                    "messages": [{"role": "user", "content": "Selected evidence."}]}}}, mcp_session_id=None)
        if retire_at == "before_request":
            assert status == 401
            assert response["error"]["code"] == "mcp_channel_authentication_required"
        else:
            assert status == 200
            assert response["result"].get("isError"), response
        assert "Must not deliver after retirement." not in str(response)
        assert len(calls) == (0 if retire_at == "before_request" else 1)
    finally:
        runtime.close()
