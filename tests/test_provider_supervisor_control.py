from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from meta_research.provider_supervisor import (
    request_supervisor_stop,
    supervisor_request_never_started,
    write_transport_envelope,
)


def test_stop_accepts_the_signed_provider_ready_marker(tmp_path: Path) -> None:
    operation = tmp_path / "provider-operations" / "operation" / "phase"
    operation.mkdir(parents=True)
    request_path = (operation / "supervisor-request.json").resolve()
    receipt_path = operation / "supervisor-exit.json"
    child_ready_path = operation / "test-child-ready"
    script = (
        "from pathlib import Path\n"
        "import signal, sys, time\n"
        "receipt = Path(sys.argv[2])\n"
        "def stop(*_args):\n"
        "    receipt.write_text('stopped', encoding='utf-8')\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "Path(sys.argv[3]).write_text('ready', encoding='utf-8')\n"
        "while True:\n"
        "    time.sleep(0.02)\n"
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(request_path),
            str(receipt_path),
            str(child_ready_path),
        ],
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 2
        while not child_ready_path.is_file():
            if time.monotonic() >= deadline:
                raise AssertionError("test supervisor did not become ready")
            time.sleep(0.01)
        marker: dict[str, object] = {
            "schema_ref": "meta-research/codex-provider-supervisor-ready/v1",
            "invocation_hash": "a" * 64,
            "supervisor_process_id": process.pid,
            "supervisor_process_group": os.getpgid(process.pid),
        }
        write_transport_envelope(
            operation / "supervisor-ready.json",
            marker,
            b"k" * 32,
        )

        assert request_supervisor_stop(
            operation,
            key=b"k" * 32,
            invocation_hash="a" * 64,
            ready_schema="meta-research/codex-provider-supervisor-ready/v1",
        )
        assert process.wait(timeout=2) == 0
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)


def test_aged_request_is_not_safe_while_exact_supervisor_is_still_live(
    tmp_path: Path,
) -> None:
    operation = tmp_path / "provider-operations" / "operation" / "phase"
    operation.mkdir(parents=True)
    request_path = (operation / "supervisor-request.json").resolve()
    invocation_hash = "b" * 64
    request_schema = "meta-research/codex-provider-supervisor-request/v1"
    write_transport_envelope(
        request_path,
        {
            "schema_ref": request_schema,
            "invocation_hash": invocation_hash,
        },
        b"k" * 32,
    )
    old = time.time() - 30
    os.utime(request_path, (old, old))
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            str(request_path),
        ],
        start_new_session=True,
    )
    try:
        assert not supervisor_request_never_started(
            operation,
            key=b"k" * 32,
            invocation_hash=invocation_hash,
            request_schema=request_schema,
        )
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)

    assert supervisor_request_never_started(
        operation,
        key=b"k" * 32,
        invocation_hash=invocation_hash,
        request_schema=request_schema,
    )
