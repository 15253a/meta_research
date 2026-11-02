from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest

from meta_research.provider_supervisor import (
    SUPERVISOR_REQUEST_SCHEMA_V2,
    ensure_transport_key,
    read_transport_envelope,
    supervisor_request_never_started,
    write_supervisor_request,
)


@pytest.mark.skipif(os.name != "posix", reason="requires real POSIX result pipe")
@pytest.mark.parametrize("scenario", ["normal", "cleanup_failure", "external_writer"])
def test_supervisor_exits_after_descendant_cleanup_failure(
    tmp_path: Path, scenario: str,
) -> None:
    cleanup_fails = scenario != "normal"
    _, key = ensure_transport_key(tmp_path)
    invocation_hash = "e" * 64
    operation = tmp_path / "provider-operations" / invocation_hash[:2] / invocation_hash
    operation.mkdir(parents=True)
    paths = {
        name + "_path": operation / filename
        for name, filename in (
            ("prompt", "prompt.txt"), ("schema", "output-schema.json"),
            ("stdout", "stdout.jsonl"), ("result", "last-message.json"),
            ("lock", "supervisor.lock"), ("ready", "supervisor-ready.json"),
            ("started", "provider-started.json"),
            ("receipt", "supervisor-exit.json"), ("stop", "supervisor-stop.json"),
        )
    }
    paths["prompt_path"].write_text("bounded test input", encoding="utf-8")
    paths["schema_path"].write_text("{}", encoding="utf-8")
    # Wait for the real signed started marker so Popen cannot race the PID probe.
    provider = """
from pathlib import Path
import os, signal, sys, time
operation = Path(sys.argv[1])
while not (operation / 'provider-started.json').exists():
    time.sleep(.01)
print('provider output preserved', flush=True)
if sys.argv[2] == 'external_writer':
    # Simulate an unkillable descendant still holding both capture pipes.
    fd = os.open(sys.argv[sys.argv.index('--output-last-message') + 1], os.O_WRONLY)
    child = os.fork()
    if child == 0:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        (operation / 'external-writer-ready').touch()
        while True:
            time.sleep(.1)
    while not (operation / 'external-writer-ready').exists():
        time.sleep(.01)
"""
    request_path = operation / "supervisor-request.json"
    write_supervisor_request(request_path, {
        "schema_ref": SUPERVISOR_REQUEST_SCHEMA_V2,
        "invocation_hash": invocation_hash,
        "argv": [sys.executable, "-c", provider, str(operation), scenario,
                 "--output-schema", str(paths["schema_path"]),
                 "--output-last-message", str(paths["result_path"]), "-"],
        "timeout_seconds": 2.0, "stream_max_bytes": 4096, "result_max_bytes": 4096,
        **{name: str(path) for name, path in paths.items()},
    }, key)
    supervisor = """
from pathlib import Path
import sys
import meta_research.provider_supervisor as module
if sys.argv[2] == 'True':
    module.ProviderProcessPlatform.process_group_running = lambda self, pid: True
    def cleanup_failure(self, pid):
        (Path(sys.argv[1]).parent / 'cleanup-failed').touch()
        return False
    module.ProviderProcessPlatform.terminate_process_group = cleanup_failure
raise SystemExit(module.main([sys.argv[1]]))
"""
    process = subprocess.Popen(
        [sys.executable, "-c", supervisor, str(request_path), str(cleanup_fails)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=2)
        if paths["started_path"].exists():
            started = read_transport_envelope(paths["started_path"], key)
            try:
                os.killpg(started["provider_process_group"], signal.SIGKILL)
            except ProcessLookupError:
                pass
    assert (process.returncode, stdout, stderr) == (
        70 if cleanup_fails else 0, b"", b"",
    )
    assert paths["started_path"].is_file()
    assert (operation / "cleanup-failed").exists() is cleanup_fails
    assert paths["stdout_path"].read_text() == "provider output preserved\n"
    assert paths["receipt_path"].exists() is not cleanup_fails
    assert paths["result_path"].exists() is not cleanup_fails
    assert not supervisor_request_never_started(
        operation, key=key, invocation_hash=invocation_hash,
        request_schema=SUPERVISOR_REQUEST_SCHEMA_V2,
    )
