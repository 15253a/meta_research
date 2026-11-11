from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from meta_research.harness import HarnessRuntime
from meta_research.harness_control import DurableHarnessOperationCanceller
from meta_research.harness_adapters import HarnessSupervisorTransport
from meta_research.provider_supervisor import (
    SUPERVISOR_EXIT_SCHEMA_V2,
    SUPERVISOR_REQUEST_SCHEMA_V2,
    ensure_transport_key,
    read_verified_exit_receipt,
    write_supervisor_request,
)


def test_durable_harness_cancel_stops_exact_flight_and_replays_after_restart(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "harness-control"
    _key_path, key = ensure_transport_key(workspace)
    invocation_hash = "a" * 64
    operation = (
        workspace
        / "provider-operations"
        / invocation_hash[:2]
        / invocation_hash
    )
    operation.mkdir(parents=True)
    request_path = (operation / "supervisor-request.json").resolve()
    prompt_path = operation / "prompt.txt"
    schema_path = operation / "output-schema.json"
    stdout_path = operation / "stdout.jsonl"
    result_path = operation / "last-message.json"
    prompt_path.write_text("cancel me", encoding="utf-8")
    schema_path.write_text("{}", encoding="utf-8")
    child_ready = operation / "test-child-ready"
    write_supervisor_request(
        request_path,
        {
            "schema_ref": SUPERVISOR_REQUEST_SCHEMA_V2,
            "invocation_hash": invocation_hash,
            "argv": [
                sys.executable, "-c",
                "import sys,time; from pathlib import Path; sys.stdin.read(); "
                "Path(sys.argv[1]).write_text('ready'); time.sleep(30)",
                str(child_ready),
                "--output-schema", str(schema_path),
                "--output-last-message", str(result_path), "-",
            ],
            "timeout_seconds": 10.0,
            "stream_max_bytes": 1024,
            "result_max_bytes": 1024,
            "prompt_path": str(prompt_path),
            "schema_path": str(schema_path),
            "stdout_path": str(stdout_path),
            "result_path": str(result_path),
            "lock_path": str(operation / "supervisor.lock"),
            "ready_path": str(operation / "supervisor-ready.json"),
            "started_path": str(operation / "provider-started.json"),
            "receipt_path": str(operation / "supervisor-exit.json"),
            "stop_path": str(operation / "supervisor-stop.json"),
        },
        key,
    )
    process = subprocess.Popen(
        [
            sys.executable, "-m", "meta_research.provider_supervisor",
            str(request_path),
        ],
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 2.0
        while not child_ready.is_file():
            if time.monotonic() >= deadline:
                raise AssertionError("test Harness flight did not become ready")
            time.sleep(0.01)
        # Stop may acknowledge that cleanup is still pending. Keep the same
        # two-second process wait and require a signed receipt before success.
        pending_or_complete = DurableHarnessOperationCanceller(workspace).cancel_operation(invocation_hash)
        assert isinstance(pending_or_complete, bool)
        assert process.wait(timeout=2) == 0
        receipt, _ = read_verified_exit_receipt(
            operation / "supervisor-exit.json", key=key,
            invocation_hash=invocation_hash,
            prompt_path=prompt_path, schema_path=schema_path,
            stdout_path=stdout_path, result_path=result_path,
            expected_schema_ref=SUPERVISOR_EXIT_SCHEMA_V2,
        )
        assert receipt["returncode"] == -15
        assert receipt["termination_reason"] == "stopped"
        assert receipt["process_tree"]["mechanism"] == "linux-subreaper-echild"

        # A restarted daemon re-verifies and reuses the same signed terminal.
        assert DurableHarnessOperationCanceller(workspace).cancel_operation(
            invocation_hash
        )
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


def test_harness_cancel_target_root_resolves_owner_hash_to_signed_transport_process(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "harness-control-bound").resolve()
    provider_workspace = (tmp_path / "provider-workspace").resolve()
    provider_workspace.mkdir()
    owner_invocation_hash = "b" * 64
    provider_operation_ref = "target-run:test:harness_turn:1"
    transport = HarnessSupervisorTransport(workspace)
    outcome: dict[str, object] = {}

    def invoke() -> None:
        try:
            outcome["completed"] = transport(
                [
                    sys.executable,
                    "-c",
                    "import time; time.sleep(30)",
                ],
                "Stop this exact provider process.",
                30.0,
                {
                    "META_RESEARCH_HARNESS_FAMILY": "codex",
                    "META_RESEARCH_PROVIDER_OPERATION_REF": provider_operation_ref,
                    "META_RESEARCH_OWNER_INVOCATION_HASH": owner_invocation_hash,
                    "META_RESEARCH_HARNESS_WORKSPACE": str(provider_workspace),
                },
            )
        except BaseException as error:
            outcome["error"] = error

    thread = threading.Thread(target=invoke, daemon=True)
    thread.start()
    started_path: Path | None = None
    deadline = time.monotonic() + 5.0
    while started_path is None:
        matches = list(
            (workspace / "provider-operations").glob(
                "*/*/provider-started.json"
            )
        )
        if matches:
            started_path = matches[0]
            break
        if not thread.is_alive():
            raise AssertionError(f"transport stopped early: {outcome!r}")
        if time.monotonic() >= deadline:
            raise AssertionError("provider process did not become ready")
        time.sleep(0.01)

    transport_invocation_hash = started_path.parent.name
    assert transport_invocation_hash != owner_invocation_hash
    canceller = DurableHarnessOperationCanceller(workspace)
    harness = object.__new__(HarnessRuntime)
    harness._require_target_request = lambda request_ref: None
    harness._owner = SimpleNamespace(
        query_run=lambda request_ref: SimpleNamespace(
            run_ref="target-run:test",
            status="running",
        ),
        latest_operation=lambda run_ref: SimpleNamespace(
            status="running",
            invocation_hash=owner_invocation_hash,
        ),
    )
    harness._operation_canceller = canceller
    assert harness.cancel_target_root("target-request:test")
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert "error" not in outcome
    completed = outcome["completed"]
    assert isinstance(completed, subprocess.CompletedProcess)
    assert completed.returncode == 143
    receipt = completed.meta_research_transport_receipt
    assert receipt["transport_invocation_hash"] == transport_invocation_hash
    assert receipt["termination_reason"] == "stopped"
    assert DurableHarnessOperationCanceller(workspace).cancel_operation(
        owner_invocation_hash
    )


def test_stop_during_locked_pre_ready_preparation_fences_native_launch(tmp_path):
    from meta_research.provider_supervisor import read_transport_envelope

    workspace = (tmp_path / "pre-ready-control").resolve()
    _, key = ensure_transport_key(workspace)
    invocation_hash = "c" * 64
    operation = workspace / "provider-operations" / invocation_hash[:2] / invocation_hash
    operation.mkdir(parents=True)
    request_path = operation / "supervisor-request.json"
    prompt_path = operation / "prompt.txt"
    schema_path = operation / "output-schema.json"
    prompt_path.write_text("Stop before native launch", encoding="utf-8")
    schema_path.write_text("{}", encoding="utf-8")
    launched = operation / "native-launched"
    blocked = operation / "pre-ready-blocked"
    release = operation / "release-ready"
    write_supervisor_request(request_path, {
        "schema_ref": SUPERVISOR_REQUEST_SCHEMA_V2,
        "invocation_hash": invocation_hash,
        "argv": [sys.executable, "-c",
            "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('launched')",
            str(launched), "--output-schema", str(schema_path),
            "--output-last-message", str(operation / "last-message.json"), "-"],
        "timeout_seconds": 10.0, "stream_max_bytes": 1024, "result_max_bytes": 1024,
        **{name: str(operation / filename) for name, filename in {
            "prompt_path": "prompt.txt", "schema_path": "output-schema.json",
            "stdout_path": "stdout.jsonl", "result_path": "last-message.json",
            "lock_path": "supervisor.lock", "ready_path": "supervisor-ready.json",
            "started_path": "provider-started.json", "receipt_path": "supervisor-exit.json",
            "stop_path": "supervisor-stop.json",
        }.items()},
    }, key)
    script = """
from pathlib import Path
import sys, time
from meta_research import provider_supervisor as supervisor
request = Path(sys.argv[1])
original_marker = supervisor._write_phase_marker
def blocked_marker(path, **kwargs):
    if path.name == 'supervisor-ready.json':
        request.with_name('pre-ready-blocked').write_text('locked')
        deadline = time.monotonic() + 10
        while not request.with_name('release-ready').exists():
            if time.monotonic() > deadline:
                raise RuntimeError('test did not release ready preparation')
            time.sleep(0.01)
    return original_marker(path, **kwargs)
supervisor._write_phase_marker = blocked_marker
raise SystemExit(supervisor.main([str(request)]))
"""
    process = subprocess.Popen([sys.executable, "-c", script, str(request_path)],
                               start_new_session=True)
    try:
        deadline = time.monotonic() + 3
        while not blocked.exists():
            assert process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert not operation.joinpath("supervisor-ready.json").exists()
        canceller = DurableHarnessOperationCanceller(workspace)
        assert canceller.cancel_operation(invocation_hash) is False
        stop = read_transport_envelope(operation / "supervisor-stop.json", key)
        assert stop["invocation_hash"] == invocation_hash
        release.write_text("continue", encoding="utf-8")
        assert process.wait(timeout=3) == 0
        receipt, _ = read_verified_exit_receipt(operation / "supervisor-exit.json",
            key=key, invocation_hash=invocation_hash, prompt_path=prompt_path,
            schema_path=schema_path, stdout_path=operation / "stdout.jsonl",
            result_path=operation / "last-message.json",
            expected_schema_ref=SUPERVISOR_EXIT_SCHEMA_V2)
        assert receipt["returncode"] is None
        assert receipt["termination_reason"] == "stopped"
        assert receipt["process_tree"]["mechanism"] == "not-started"
        assert not launched.exists()
        assert canceller.cancel_operation(invocation_hash) is True
    finally:
        release.write_text("continue", encoding="utf-8")
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
