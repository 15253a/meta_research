from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

import meta_research.deepfetch as deepfetch_module
from meta_research.deepfetch import CodexDeepFetchAdapter, DeepFetchUnavailable
from test_deepfetch_adapter import (
    PROTOTYPE_ACQUIRE,
    RecordingAcquisitionClient,
    _bind_acquisition,
    _request,
)
from test_deepfetch_native_acquisition import NativeAcquisitionRunner


# Public MCP arguments captured from the bounded VM acceptance. No tool-result
# claims, session transcript, credentials, or paper bytes are needed to reproduce.
CAPTURED_EFFECT = {
    "effect_id": "provided-v7-dd9cdd2300284109acf19cbfa1d42045",
    "targets": [{
        "paper_id": "arxiv:1706.03762@22a706ae6723",
        "title": "Attention Is All You Need",
        "arxiv_id": "1706.03762v7",
        "source_urls": [
            "file:///opt/issue270/acceptance/provided-deepfetch-3a22e2d/data/"
            "deepfetch-provider/research-workspaces/"
            "555f9bbbb4390d0b18853b091fe3a688acc9bc0056382494ab899167b670504d-"
            "14303d8c15faba3706c6c9eb06b0b57af148111b99ca7f2abbe3ea149cfb717a/"
            "provided-materials/cf82bdee0a108e9cef7d0e767ecab7adc91931399bbc81ff354f8adba7da1c77/"
            "eec69f5969f35ca769345f1d4094d72f5756afce2a730e4f3d7fb9c9481708b3/attention-v7.pdf"
        ],
    }],
}


def _native_event(effect):
    return json.dumps({"type": "item.completed", "item": {
        "id": "native-acquisition", "type": "mcp_tool_call",
        "server": "meta_research", "tool": "agent_runtime.acquisition.request",
        "arguments": effect, "result": {}, "error": None, "status": "completed",
    }})


def test_captured_local_acquisition_arguments_preserve_exact_owner_target():
    assert deepfetch_module._native_acquisition_effects(_native_event(CAPTURED_EFFECT)) == (
        CAPTURED_EFFECT,
    )


class _ProvidedNativeRunner(NativeAcquisitionRunner):
    def __call__(self, argv, prompt, timeout):
        assert "web_evidence_gate=v1" not in prompt
        completed = super().__call__(argv, prompt, timeout)
        public = Path(next(line.split("=", 1)[1] for line in prompt.splitlines()
                           if line.startswith("public_output_root=")))
        path = public / "papers.json"
        ledger = json.loads(path.read_text(encoding="utf-8"))
        ledger["run"]["dimensions_used"] = []
        ledger["run"]["active_search_elapsed_seconds"] = 0
        path.write_text(json.dumps(ledger), encoding="utf-8")
        return completed


def _setup(tmp_path, monkeypatch, *, commit=True):
    monkeypatch.setitem(PROTOTYPE_ACQUIRE["acquisition_request"]["targets"][0],
                        "source_urls", CAPTURED_EFFECT["targets"][0]["source_urls"])
    workspace = tmp_path / "provider"
    authority = RecordingAcquisitionClient(tmp_path / "owner-artifacts")
    runner = _ProvidedNativeRunner(workspace, authority, commit=commit)
    runner.emit_web_evidence = False
    adapter = _bind_acquisition(CodexDeepFetchAdapter(
        workspace, model_ref="gpt-test", process_runner=runner), authority)
    request = replace(_request(), literature_access_mode="provided_only",
                      runtime_binding=adapter.runtime_binding(), job_ref="provided-local")
    return adapter, request, authority, runner


def test_signed_local_acquisition_reconciles_without_provider_dispatch(tmp_path, monkeypatch):
    adapter, request, authority, runner = _setup(tmp_path, monkeypatch)
    result = adapter.execute(request)
    assert len(result.fulltexts) == 1
    assert result.web_evidence["search_event_count"] == 0
    assert result.web_evidence["fetch_event_count"] == 0
    assert len(runner.calls) == len(authority.calls) == 1
    before = {path: path.read_bytes() for path in (tmp_path / "provider").rglob("supervisor-exit.json")}
    assert before
    assert adapter.execute(replace(request, reconcile_only=True)) == result
    assert len(runner.calls) == len(authority.calls) == 1
    assert all(path.read_bytes() == original for path, original in before.items())


def test_signed_result_before_checkpoint_recovers_local_effect_without_redispatch(tmp_path, monkeypatch):
    adapter, request, authority, runner = _setup(tmp_path, monkeypatch)

    def interrupted(_stdout):
        raise DeepFetchUnavailable("deepfetch_protocol_checkpoint_invalid")

    with monkeypatch.context() as scoped:
        scoped.setattr(deepfetch_module, "_native_acquisition_effects", interrupted)
        with pytest.raises(DeepFetchUnavailable) as caught:
            adapter.execute(request)
    assert caught.value.native_session_ref == "native-web-research-1"
    assert caught.value.durable_outcome == "terminal"
    result = adapter.execute(replace(request, reconcile_only=True))
    assert result.native_session_ref == "native-web-research-1"
    assert len(result.fulltexts) == 1
    assert len(runner.calls) == len(authority.calls) == 1


def test_local_uri_is_not_an_owner_commit(tmp_path, monkeypatch):
    adapter, request, authority, _runner = _setup(tmp_path, monkeypatch, commit=False)
    with pytest.raises(DeepFetchUnavailable, match="deepfetch_acquisition_reattestation_required"):
        adapter.execute(request)
    assert authority.calls == []


@pytest.mark.parametrize("uri", [
    "file://remote.example/paper.pdf", "file:relative.pdf", "file:///paper.pdf?token=secret",
    "file:///paper.pdf#part", "file:///tmp/../secret.pdf", "file:///tmp/%2e%2e/secret.pdf",
    "file:///tmp/paper%00.pdf", "file:///tmp/paper.pdf\n", "data:application/pdf;base64,AAAA",
])
def test_local_source_uri_rejects_ambiguous_or_remote_paths(uri):
    effect = copy.deepcopy(CAPTURED_EFFECT)
    effect["targets"][0]["source_urls"] = [uri]
    with pytest.raises(DeepFetchUnavailable, match="deepfetch_protocol_checkpoint_invalid"):
        deepfetch_module._native_acquisition_effects(_native_event(effect))


def test_local_source_never_becomes_a_public_web_url():
    with pytest.raises(DeepFetchUnavailable, match="not_public"):
        deepfetch_module._validated_public_url(CAPTURED_EFFECT["targets"][0]["source_urls"][0], "not_public")
