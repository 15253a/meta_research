from __future__ import annotations

import copy
import json
import os
import subprocess
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from meta_research.deepfetch import CodexDeepFetchAdapter, DeepFetchUnavailable, canonical_hash
from meta_research.provider_supervisor import (
    ensure_transport_key,
    read_transport_envelope,
    write_exit_receipt,
)
from test_deepfetch_adapter import (
    PROTOTYPE_ACQUIRE,
    PROTOTYPE_EMPTY_FINAL,
    PROTOTYPE_FINAL,
    RecordingAcquisitionClient,
    RecordingRunner,
    SequencedPrototypeRunner,
    _bind_acquisition,
    _request,
)


HOST_EXIT_TIMESTAMP = 1790214123
MODEL_TIMESTAMP = "2040-01-01T00:00:00Z"


class DurableFulltextRunner:
    """Use the normal fulltext/Reader fixture behind signed host receipts."""

    def __init__(self, workspace: Path, final: dict[str, object], *, fulltext: bool = True) -> None:
        self.workspace = workspace
        self.provider = (
            SequencedPrototypeRunner([PROTOTYPE_ACQUIRE, final])
            if fulltext else RecordingRunner(final)
        )
        self.calls: list[str] = []

    def __call__(self, *_args: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("durable test dispatched through the direct runner")

    def run_durable_job(
        self,
        job_ref: str,
        argv: list[str],
        prompt: str,
        timeout: float | None,
        stdout_path: Path,
        pid_path: Path,
        supervisor_request_path: Path,
    ) -> subprocess.CompletedProcess[str]:
        del pid_path, supervisor_request_path
        self.calls.append(job_ref)
        result = self.provider(argv, prompt, timeout)
        stdout_path.write_text(result.stdout, encoding="utf-8")
        result_path = Path(argv[argv.index("--output-last-message") + 1])
        _key_path, key = ensure_transport_key(self.workspace)
        invocation = read_transport_envelope(stdout_path.parent / "invocation.json", key)
        receipt_path = stdout_path.parent / "supervisor-exit.json"
        write_exit_receipt(
            receipt_path,
            key=key,
            invocation_hash=canonical_hash(invocation),
            prompt_path=stdout_path.parent / "prompt.txt",
            schema_path=stdout_path.parent / "output-schema.json",
            stdout_path=stdout_path,
            result_path=result_path,
            returncode=result.returncode,
            input_bytes=len(prompt.encode("utf-8")),
        )
        # Only the host-owned exit observation supplies the completion time.
        # Deliberately make the provider output file's mtime disagree with it.
        os.utime(result_path, (HOST_EXIT_TIMESTAMP - 100, HOST_EXIT_TIMESTAMP - 100))
        os.utime(receipt_path, (HOST_EXIT_TIMESTAMP, HOST_EXIT_TIMESTAMP))
        return result


def _fixture(tmp_path: Path, *, durable: bool, model_timestamp: object, fulltext: bool = True):
    final = copy.deepcopy(PROTOTYPE_FINAL if fulltext else PROTOTYPE_EMPTY_FINAL)
    final["workflow"]["finalized_at"] = model_timestamp
    workspace = tmp_path / "provider"
    runner = (
        DurableFulltextRunner(workspace, final, fulltext=fulltext)
        if durable
        else SequencedPrototypeRunner([PROTOTYPE_ACQUIRE, final])
    )
    authority = RecordingAcquisitionClient(tmp_path / "owner-artifacts")
    adapter = CodexDeepFetchAdapter(workspace, process_runner=runner)
    if fulltext:
        adapter = _bind_acquisition(adapter, authority)
    request = replace(
        _request(),
        runtime_binding=adapter.runtime_binding(),
        job_ref="deepfetch-run:host-completion" if durable else None,
    )
    return adapter, request, runner, authority, workspace


def _finalized_at(workspace: Path) -> str:
    checkpoint_path = next(workspace.glob("runs/*/private/protocol.json"))
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert checkpoint["phase"] == "finalized"
    return checkpoint["final_envelope"]["workflow"]["finalized_at"]


@pytest.mark.parametrize("durable", [False, True], ids=["direct", "durable"])
@pytest.mark.parametrize(
    "model_timestamp", [None, "yesterday", MODEL_TIMESTAMP], ids=["null", "invalid", "fabricated"]
)
def test_finalization_uses_host_completion_time_with_verified_fulltext_and_reader(
    tmp_path: Path, durable: bool, model_timestamp: object
) -> None:
    adapter, request, runner, authority, workspace = _fixture(
        tmp_path, durable=durable, model_timestamp=model_timestamp
    )
    before = datetime.now(timezone.utc).timestamp()

    result = adapter.execute(request)

    after = datetime.now(timezone.utc).timestamp()
    assert result.completion == "complete"
    assert len(result.papers) == len(result.fulltexts) == 1
    assert len(authority.calls) == 1
    assert len(runner.calls) == 3
    assert result.web_evidence["prototype"]["reader_assignments"][0]["status"] == "complete"
    host_time = datetime.fromisoformat(_finalized_at(workspace)).timestamp()
    assert datetime.fromisoformat(result.papers[0]["retrieved_at"]).timestamp() == host_time
    if durable:
        assert host_time == HOST_EXIT_TIMESTAMP
    else:
        assert before <= host_time <= after


@pytest.mark.parametrize("fulltext", [False, True], ids=["empty", "fulltext-reader"])
def test_durable_replay_preserves_host_time_without_dispatch_or_artifact_changes(
    tmp_path: Path, fulltext: bool
) -> None:
    adapter, request, runner, authority, workspace = _fixture(
        tmp_path, durable=True, model_timestamp=None, fulltext=fulltext
    )
    first = adapter.execute(request)
    host_time = _finalized_at(workspace)
    receipts = {
        path: path.read_bytes()
        for path in workspace.glob("provider-operations/*/*/*")
        if path.is_file()
    }

    replayed = adapter.execute(request)
    reconciled = adapter.execute(
        replace(request, reconcile_only=True, native_session_ref=first.native_session_ref)
    )

    assert replayed == reconciled == first
    assert _finalized_at(workspace) == host_time
    assert len(runner.calls) == 2 + int(fulltext)
    assert len(authority.calls) == int(fulltext)
    assert all(path.read_bytes() == content for path, content in receipts.items())


@pytest.mark.parametrize("checkpoint_timestamp", [None, MODEL_TIMESTAMP], ids=["null", "model"])
@pytest.mark.parametrize("fulltext", [False, True], ids=["empty", "fulltext-reader"])
def test_frozen_binding_reconciliation_normalizes_only_model_completion_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, checkpoint_timestamp: object,
    fulltext: bool,
) -> None:
    adapter, request, runner, authority, workspace = _fixture(
        tmp_path, durable=True, model_timestamp=MODEL_TIMESTAMP, fulltext=fulltext
    )
    first = adapter.execute(request)
    checkpoint_path = next(workspace.glob("runs/*/private/protocol.json"))
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["final_envelope"]["workflow"]["finalized_at"] = checkpoint_timestamp
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    # The request keeps its admitted binding while the running adapter changes.
    monkeypatch.setattr(
        adapter, "runtime_binding", lambda: replace(request.runtime_binding, model_ref="gpt-next")
    )

    reconciled = adapter.execute(
        replace(request, reconcile_only=True, native_session_ref=first.native_session_ref)
    )

    assert reconciled == first
    assert datetime.fromisoformat(_finalized_at(workspace)).timestamp() == HOST_EXIT_TIMESTAMP
    if fulltext:
        assert datetime.fromisoformat(reconciled.papers[0]["retrieved_at"]).timestamp() == HOST_EXIT_TIMESTAMP
    assert len(runner.calls) == 2 + int(fulltext)
    assert len(authority.calls) == int(fulltext)


@pytest.mark.parametrize("fulltext", [False, True], ids=["empty", "fulltext-reader"])
def test_frozen_reconciliation_still_rejects_changed_final_research_facts(
    tmp_path: Path, fulltext: bool
) -> None:
    adapter, request, runner, authority, workspace = _fixture(
        tmp_path, durable=True, model_timestamp=MODEL_TIMESTAMP, fulltext=fulltext
    )
    first = adapter.execute(request)
    checkpoint_path = next(workspace.glob("runs/*/private/protocol.json"))
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["final_envelope"]["limitations"] = ["unverified checkpoint change"]
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")

    with pytest.raises(DeepFetchUnavailable) as failure:
        adapter.execute(
            replace(request, reconcile_only=True, native_session_ref=first.native_session_ref)
        )

    assert failure.value.code == "deepfetch_provider_reconciliation_pending"
    assert len(runner.calls) == 2 + int(fulltext)
    assert len(authority.calls) == int(fulltext)
