from __future__ import annotations

import os
import signal
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
    SUPERVISOR_REQUEST_SCHEMA_V2,
    ensure_transport_key,
    write_supervisor_request,
    write_transport_envelope,
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
    stdout_path.write_text("", encoding="utf-8")
    write_supervisor_request(
        request_path,
        {
            "schema_ref": SUPERVISOR_REQUEST_SCHEMA_V2,
            "invocation_hash": invocation_hash,
        },
        key,
    )
    child_ready = operation / "test-child-ready"
    script = (
        "from pathlib import Path\n"
        "import signal, sys, time\n"
        "from meta_research.provider_supervisor import "
        "SUPERVISOR_EXIT_SCHEMA_V2, write_exit_receipt\n"
        "operation = Path(sys.argv[1])\n"
        "key = bytes.fromhex(sys.argv[2])\n"
        "invocation_hash = sys.argv[3]\n"
        "def stop(*_args):\n"
        "    write_exit_receipt(operation / 'supervisor-exit.json', key=key, "
        "invocation_hash=invocation_hash, prompt_path=operation / 'prompt.txt', "
        "schema_path=operation / 'output-schema.json', stdout_path=operation / "
        "'stdout.jsonl', result_path=operation / 'last-message.json', "
        "returncode=143, input_bytes=(operation / 'prompt.txt').stat().st_size, "
        "termination_reason='stopped', schema_ref=SUPERVISOR_EXIT_SCHEMA_V2)\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "(operation / 'test-child-ready').write_text('ready', encoding='utf-8')\n"
        "while True:\n"
        "    time.sleep(0.02)\n"
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(operation),
            key.hex(),
            invocation_hash,
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
        write_transport_envelope(
            operation / "supervisor-ready.json",
            {
                "schema_ref": "meta-research/provider-supervisor-ready/v2",
                "invocation_hash": invocation_hash,
                "supervisor_process_id": process.pid,
                "supervisor_process_group": os.getpgid(process.pid),
            },
            key,
        )

        assert DurableHarnessOperationCanceller(workspace).cancel_operation(
            invocation_hash
        )
        assert process.wait(timeout=2) == 0

        # A restarted daemon re-verifies and reuses the same signed terminal.
        assert DurableHarnessOperationCanceller(workspace).cancel_operation(
            invocation_hash
        )
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)


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
        except BaseException as error:  # surfaced on the test thread below
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
