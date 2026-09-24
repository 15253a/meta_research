from __future__ import annotations

import copy
import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from meta_research.deepfetch import CodexDeepFetchAdapter, DeepFetchUnavailable
from meta_research.owners.common import canonical_hash
from meta_research.provider_supervisor import (
    ensure_transport_key,
    read_transport_envelope,
    write_exit_receipt,
)
from test_deepfetch_adapter import (
    PROTOTYPE_ACQUIRE,
    PROTOTYPE_FINAL,
    RecordingAcquisitionClient,
    SequencedPrototypeRunner,
    _bind_acquisition,
    _request,
)


class NativeAcquisitionRunner(SequencedPrototypeRunner):
    def __init__(self, workspace: Path, authority: RecordingAcquisitionClient, *,
                 commit: bool = True, emit: bool = True) -> None:
        super().__init__([copy.deepcopy(PROTOTYPE_FINAL)])
        self.workspace = workspace
        self.authority = authority
        self.commit = commit
        self.emit = emit

    def __call__(self, argv, prompt, timeout):
        completed = super().__call__(argv, prompt, timeout)
        if "web_evidence_gate=v1" in prompt:
            return completed
        effect = copy.deepcopy(PROTOTYPE_ACQUIRE["acquisition_request"])
        if self.commit:
            self.authority.dispatch_mcp("deepfetch-token", {
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "agent_runtime.acquisition.request", "arguments": effect},
            })
        if not self.emit:
            return completed
        # Deliberately omit tool result claims: acceptance must query the Owner.
        event = {"type": "item.completed", "item": {
            "id": "native-acquisition", "type": "mcp_tool_call",
            "server": "meta_research", "tool": "agent_runtime.acquisition.request",
            "arguments": effect, "result": {}, "error": None, "status": "completed",
        }}
        return subprocess.CompletedProcess(argv, completed.returncode,
            completed.stdout + "\n" + json.dumps(event), completed.stderr)

    def run_durable_job(self, job_ref, argv, prompt, timeout, stdout_path,
                        pid_path, supervisor_request_path):
        completed = self(argv, prompt, timeout)
        stdout_path.write_text(completed.stdout, encoding="utf-8")
        result_path = Path(argv[argv.index("--output-last-message") + 1])
        _, key = ensure_transport_key(self.workspace)
        invocation = read_transport_envelope(stdout_path.parent / "invocation.json", key)
        write_exit_receipt(stdout_path.parent / "supervisor-exit.json", key=key,
            invocation_hash=canonical_hash(invocation),
            prompt_path=stdout_path.parent / "prompt.txt",
            schema_path=stdout_path.parent / "output-schema.json",
            stdout_path=stdout_path, result_path=result_path, returncode=0,
            input_bytes=len(prompt.encode("utf-8")))
        return completed


def _setup(tmp_path, *, commit=True, emit=True):
    workspace = tmp_path / "provider"
    authority = RecordingAcquisitionClient(tmp_path / "owner-artifacts")
    runner = NativeAcquisitionRunner(workspace, authority, commit=commit, emit=emit)
    adapter = _bind_acquisition(CodexDeepFetchAdapter(
        workspace, model_ref="gpt-test", process_runner=runner), authority)
    request = replace(_request(), runtime_binding=adapter.runtime_binding(),
                      job_ref="deepfetch-native-acquisition")
    return adapter, request, authority, runner


def test_native_acquisition_is_reattested_and_replays_without_dispatch(tmp_path):
    adapter, request, authority, runner = _setup(tmp_path)
    result = adapter.execute(request)
    assert len(result.fulltexts) == 1
    assert len(authority.calls) == 1
    assert ("turn-1", PROTOTYPE_ACQUIRE["acquisition_request"]["effect_id"]) in authority.query_calls
    count = len(runner.calls)
    replay = adapter.execute(replace(request, reconcile_only=True,
                                    native_session_ref=result.native_session_ref))
    assert replay == result
    assert len(runner.calls) == count
    assert len(authority.calls) == 1


def test_native_trace_without_owner_commit_cannot_supply_fulltext_proof(tmp_path):
    adapter, request, authority, _ = _setup(tmp_path, commit=False)
    with pytest.raises(DeepFetchUnavailable, match="deepfetch_acquisition_reattestation_required"):
        adapter.execute(request)
    assert not authority.calls


def test_native_acquisition_uses_the_authorized_human_resume_phase(tmp_path):
    adapter, request, authority, _ = _setup(tmp_path)
    request = replace(request, human_request_resume={
        "effect_id": "human-response", "request_ref": "human-request-1",
        "phase": "turn-7",
    })
    result = adapter.execute(request)
    assert len(result.fulltexts) == 1
    assert len(authority.calls) == 1
    assert authority.calls[0]["phase"] == "turn-7"
    assert authority.query_calls
    assert all(phase == "turn-7" for phase, _ in authority.query_calls)


def test_owner_commit_without_native_trace_does_not_bypass_provenance(tmp_path):
    adapter, request, _, _ = _setup(tmp_path, emit=False)
    with pytest.raises(DeepFetchUnavailable, match="deepfetch_hosted_acquisition_proof_missing"):
        adapter.execute(request)


def test_native_acquisition_replay_rejects_owner_artifact_drift(tmp_path):
    adapter, request, authority, _ = _setup(tmp_path)
    result = adapter.execute(request)
    for artifact in authority.artifact_root.glob("*.html"):
        artifact.write_text("changed owner artifact", encoding="utf-8")
    # Existing checkpoint proof verification catches changed bytes before reattestation.
    with pytest.raises(DeepFetchUnavailable, match="deepfetch_provider_reconciliation_pending"):
        adapter.execute(replace(request, reconcile_only=True,
                                native_session_ref=result.native_session_ref))
