from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from meta_research.paths import DataRoot
from meta_research.provider_supervisor import (
    CODEX_SUPERVISOR_REQUEST_SCHEMA_V2,
    ensure_transport_key,
    read_verified_exit_receipt,
    transport_canonical_json,
    write_supervisor_request,
)


pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux supervisor result transport")

# Open with the same O_NOFOLLOW behavior as Codex 0.169 only on the FIFO
# branch. The old CLI and other roles still use the original procfs transport.
_READER = """#!/usr/bin/env python3
import json, os, stat, sys
from pathlib import Path
path = Path(sys.argv[sys.argv.index('--output-last-message') + 1])
operation = Path(os.environ['META_RESEARCH_PROVIDER_OPERATION']).parent
with (operation / 'launches').open('a') as f:
    f.write('1')
named = stat.S_ISFIFO(path.lstat().st_mode)
print(json.dumps({'named': named, 'parent_mode': stat.S_IMODE(path.parent.stat().st_mode),
                  'mode': stat.S_IMODE(path.lstat().st_mode)}), flush=True)
scenario = sys.stdin.read()
if scenario == 'exit':
    sys.exit(9)
fd = os.open(path, os.O_WRONLY | (os.O_NOFOLLOW if named else 0))
with os.fdopen(fd, 'wb') as dst:
    dst.write(b'x' * (8192 if scenario == 'overflow' else 4))
if scenario == 'replace':
    path.unlink()
    path.write_text('replacement')
"""


def _operation(tmp_path, *, version="0.169.0", scope="recorder", scenario="normal"):
    root = DataRoot(tmp_path)
    executable = root.codex_cli_executable
    executable.parent.mkdir(parents=True)
    executable.write_text(_READER)
    executable.chmod(0o700)
    package = root.codex_cli_install_root / "node_modules/@openai/codex/package.json"
    package.parent.mkdir(parents=True)
    package.write_text(json.dumps({"version": version}))
    if scope == "unmanaged":
        executable = tmp_path / "external-codex"
        executable.write_text(_READER)
        executable.chmod(0o700)
    workspace = tmp_path / ("other-role" if scope == "other-role" else "research-recorder") / "provider"
    _, key = ensure_transport_key(workspace)
    job_ref = "timeline-summary-4b3ab1c90ffb493d890b70e6ec655352"
    operation = workspace / "provider-operations" / hashlib.sha256(job_ref.encode()).hexdigest() / "drafting"
    operation.mkdir(parents=True)
    paths = {name + "_path": operation / filename for name, filename in (
        ("prompt", "prompt.txt"), ("schema", "output-schema.json"), ("result", "last-message.json"),
        ("stdout", "stdout.jsonl"), ("lock", "supervisor.lock"), ("ready", "supervisor-ready.json"),
        ("started", "provider-started.json"), ("receipt", "supervisor-exit.json"), ("stop", "supervisor-stop.json"),
    )}
    paths["prompt_path"].write_text(scenario)
    paths["schema_path"].write_text("{}")
    argv = [str(executable), "exec", "--ignore-user-config", "--output-schema", str(paths["schema_path"]),
            "--output-last-message", str(paths["result_path"]), "-"]
    invocation = {"job_ref": job_ref, "execution_contract": {"provider_version": version}}
    data = transport_canonical_json(invocation).encode()
    (operation / "invocation.json").write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    request = operation / "supervisor-request.json"
    write_supervisor_request(request, {
        "schema_ref": CODEX_SUPERVISOR_REQUEST_SCHEMA_V2, "invocation_hash": digest,
        "argv": argv, "timeout_seconds": 5, "stream_max_bytes": 4096,
        "result_max_bytes": 1024, "prompt_max_bytes": 4096,
        **{name: str(path) for name, path in paths.items()},
    }, key)
    return request, key, digest, paths


def _run(request):
    env = os.environ.copy()
    env.pop("CODEX_HOME", None)
    return subprocess.run([sys.executable, "-m", "meta_research.provider_supervisor", str(request)],
                          env=env, capture_output=True, timeout=12, check=False)


@pytest.mark.parametrize(("version", "scope", "scenario", "named"), [
    ("0.169.0", "recorder", "normal", True),
    ("0.169.0", "recorder", "overflow", True),
    ("0.169.0", "recorder", "exit", True),
    ("0.159.0", "recorder", "normal", False),
    ("0.169.0", "other-role", "normal", False),
    ("0.169.0", "unmanaged", "normal", False),
])
def test_recorder_result_transport(tmp_path, version, scope, scenario, named):
    request, key, digest, paths = _operation(tmp_path, version=version, scope=scope, scenario=scenario)
    first = _run(request)
    assert first.returncode == 0, first.stderr
    receipt, _ = read_verified_exit_receipt(paths["receipt_path"], key=key, invocation_hash=digest,
        **{name: paths[name] for name in ("prompt_path", "schema_path", "stdout_path", "result_path")})
    observed = json.loads(paths["stdout_path"].read_text())
    assert observed["named"] is named
    if named:
        assert (observed["mode"], observed["parent_mode"]) == (0o600, 0o700)
    assert not list(request.parent.glob(".recorder-pipe-*"))
    if scenario == "overflow":
        assert receipt["termination_reason"] == "output_limit"
        assert paths["result_path"].read_bytes() == b"x" * 1024
    else:
        assert receipt["termination_reason"] == "completed"
        assert receipt["returncode"] == (9 if scenario == "exit" else 0)
        assert paths["result_path"].read_bytes() == (b"" if scenario == "exit" else b"xxxx")
    original = paths["receipt_path"].read_bytes()
    assert _run(request).returncode == 0
    assert paths["receipt_path"].read_bytes() == original
    assert (request.parent / "launches").read_text() == "1"
    assert not list(request.parent.glob(".recorder-pipe-*"))


@pytest.mark.parametrize("damage", ["invocation", "pipe"])
def test_recorder_rejects_changed_input_or_pipe_without_sealing(tmp_path, damage):
    request, _, _, paths = _operation(tmp_path, scenario="replace" if damage == "pipe" else "normal")
    if damage == "invocation":
        (request.parent / "invocation.json").write_text("{}")
    assert _run(request).returncode == 70
    assert not paths["receipt_path"].exists()
    assert not paths["result_path"].exists()
    assert (request.parent / "launches").exists() is (damage == "pipe")
    assert not list(request.parent.glob(".recorder-pipe-*"))
