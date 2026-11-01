import json
from io import BytesIO

import pytest

from meta_research.companion_writing import WritingRequest, writing_message
from meta_research.writing_content import generate
from meta_research.owners.common import OwnerConflict
from meta_research.root_capabilities import root_operation_catalog
from meta_research.semantic_mcp import ROOT_AGENT_COMMON_OPERATION_IDS


def test_writing_preserves_free_requirements_and_uses_ordinary_assistant():
    task = WritingRequest(document_type="paper", include_presentation=True,
        task_scope="revision", requirements="保留负结果，只改讨论；PPT 也更新。",
        model={"kind": "configured_api", "config_ref": "my-model"}, output_directory="drafts/revision")
    message = writing_message(task, "只修改选定版本", None)
    assert "保留负结果，只改讨论；PPT 也更新。" in message
    assert '"include_presentation": true' in message
    assert '"task_scope": "revision"' in message
    assert '"config_ref": "my-model"' in message
    assert "writing-report/SKILL.md" in message
    assert "不向 Cycle 派工" in message


@pytest.mark.parametrize("directory", ["../escape", "/tmp/escape", "C:/outside", ".creation-context", "dir/../outside"])
def test_output_directory_stays_in_assistant_workspace(directory):
    with pytest.raises(ValueError):
        WritingRequest(requirements="draft", output_directory=directory)


def test_missing_selected_template_fails_before_queuing():
    class Memory:
        def query_asset_version(self, ref):
            return None
    task = WritingRequest(requirements="Use selected template", materials=[{
        "version_ref": "missing-version", "content_hash": "a" * 64, "role": "document_template"}])
    with pytest.raises(OwnerConflict, match="writing_material_version_unavailable"):
        writing_message(task, task.requirements, Memory())


def test_selected_model_receives_only_explicit_material_and_never_falls_back(tmp_path):
    config = tmp_path / "private.json"
    config.write_text(json.dumps({"selected": {"endpoint": "https://model.example/v1/chat/completions",
        "model": "chosen-model", "api_key_env": "PRIVATE_MODEL_KEY"}}))
    env = {"META_RESEARCH_CONTENT_MODELS": str(config), "PRIVATE_MODEL_KEY": "private-test-key"}
    calls = []
    def provider(request, timeout):
        calls.append(request)
        return BytesIO(json.dumps({"choices": [{"message": {"content": "Revised selected paragraph."}}]}).encode())
    request = {"messages": [{"role": "user", "content": "Only selected paragraph and evidence."}]}
    assert generate("selected", request, environment=env, opener=provider) == "Revised selected paragraph."
    payload = json.loads(calls[0].data)
    assert payload == {"model": "chosen-model", **request}
    assert "private-test-key" not in calls[0].data.decode()
    assert calls[0].get_header("Authorization") == "Bearer private-test-key"
    def failed(request, timeout):
        calls.append(request)
        raise OSError("provider unavailable")
    with pytest.raises(ValueError, match="content_model_call_failed"):
        generate("selected", request, environment=env, opener=failed)
    assert len(calls) == 2


def test_assistant_catalog_does_not_grant_research_writes():
    catalog = root_operation_catalog("companion", common_operation_ids=ROOT_AGENT_COMMON_OPERATION_IDS)
    assert "research_memory.content.read" in catalog
    assert "research_workspace.read" in catalog
    assert "research_graph.datasets.register" not in catalog
    assert "research_memory.assets.intake" not in catalog
    assert "research_memory.assets.retire" not in catalog
    assert "research_graph.artifact_roles.adjust" not in catalog
    assert "acquisition.request" not in catalog


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_configured_model_does_not_follow_redirects(tmp_path, status):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from threading import Thread

    received = []
    class Endpoint(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((self.path, self.headers.get("Authorization")))
            self.send_response(status)
            self.send_header("Location", f"http://localhost:{self.server.server_port}/unconfigured")
            self.end_headers()

        do_GET = do_POST

        def log_message(self, *_):
            pass

    with HTTPServer(("127.0.0.1", 0), Endpoint) as server:
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            config = tmp_path / "private.json"
            config.write_text(json.dumps({"selected": {
                "endpoint": f"http://127.0.0.1:{server.server_port}/configured",
                "model": "chosen", "api_key_env": "PRIVATE_TEST_KEY"}}))
            with pytest.raises(ValueError, match="^content_model_call_failed$"):
                generate("selected", {"messages": [{"role": "user", "content": "selected"}]},
                    environment={"META_RESEARCH_CONTENT_MODELS": str(config),
                        "PRIVATE_TEST_KEY": "private-test-key"})
            assert received == [("/configured", "Bearer private-test-key")]
        finally:
            server.shutdown()
            worker.join()


def test_configured_model_is_callable_through_the_actual_assistant_grant(tmp_path, monkeypatch):
    from test_public_manual_question_lifecycle import _build_runtime, _accept_root_question
    from test_dataset_effect_scope_recovery import _scope
    from test_root_workspace import _channel, _external_context, _call, _accepted
    runtime = _build_runtime(tmp_path / "runtime")
    try:
        quest_ref, _ = _accept_root_question(runtime, "external-content")
        scope = _scope(runtime, quest_ref=quest_ref, run_ref="content-call", root_ref="assistant-content")
        channel = _channel(runtime, _external_context(scope))
        config = tmp_path / "content-models.json"
        config.write_text(json.dumps({"selected": {"endpoint": "https://model.example/v1/chat/completions",
            "model": "explicit-model", "api_key_env": "CONTENT_TEST_KEY"}}))
        monkeypatch.setenv("META_RESEARCH_CONTENT_MODELS", str(config))
        monkeypatch.setenv("CONTENT_TEST_KEY", "test-private-key")
        calls = []
        def provider(request, timeout):
            calls.append(json.loads(request.data))
            return BytesIO(b'{"choices":[{"message":{"content":"Selected model content."}}]}')
        monkeypatch.setattr("meta_research.writing_content._open_content_model", provider)
        messages = [{"role": "user", "content": "Selected evidence only."}]
        before = runtime.owners.research_graph.query_snapshot()
        reply = _accepted(_call(runtime, channel, "assistant.content.generate",
            config_ref="selected", messages=messages))
        assert reply == {"config_ref": "selected", "content": "Selected model content."}
        assert calls == [{"model": "explicit-model", "messages": messages}]
        assert runtime.owners.research_graph.query_snapshot() == before
        failure = _call(runtime, channel, "assistant.content.generate",
            config_ref="unknown", messages=messages)
        assert failure["isError"] is True
        assert "content_model_configuration_unavailable" in str(failure)
        assert len(calls) == 1
        assert "test-private-key" not in str(reply) + str(failure)
    finally:
        runtime.close()
