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
    SUPERVISOR_REQUEST_SCHEMA,
    ensure_transport_key,
    read_verified_exit_receipt,
    transport_canonical_json,
    write_supervisor_request,
)


pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux supervisor result transport")

# Open with the same O_NOFOLLOW behavior as Codex 0.169 across every supervised
# POSIX provider operation (recorder, deepfetch, drafting, and other roles).
_READER = """#!/usr/bin/env python3
import json, os, stat, sys
from pathlib import Path
path = Path(sys.argv[sys.argv.index('--output-last-message') + 1])
operation = Path(os.environ['META_RESEARCH_PROVIDER_OPERATION']).parent
with (operation / 'launches').open('a') as f:
    f.write('1')
named = stat.S_ISFIFO(path.lstat().st_mode)
print(json.dumps({'named': named, 'parent_mode': stat.S_IMODE(path.parent.stat().st_mode),
                  'mode': stat.S_IMODE(path.lstat().st_mode), 'prefix': path.parent.name.split('-')[0]}), flush=True)
scenario = sys.stdin.read()
if scenario == 'exit':
    sys.exit(9)
fd = os.open(path, os.O_WRONLY | os.O_NOFOLLOW)
with os.fdopen(fd, 'wb') as dst:
    dst.write(b'x' * (8192 if scenario == 'overflow' else 4))
if scenario == 'replace':
    path.unlink()
    path.write_text('replacement')
elif scenario == 'unlink':
    path.unlink()
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
    if scope == "deepfetch":
        workspace = tmp_path / "deepfetch-provider"
        phase_dir = "deepfetch-initial"
        schema_ref = SUPERVISOR_REQUEST_SCHEMA
        argv_flags = ["--skip-git-repo-check", "--strict-config"]
    elif scope == "drafting":
        workspace = tmp_path / "quest-drafting-provider"
        phase_dir = "drafting"
        schema_ref = CODEX_SUPERVISOR_REQUEST_SCHEMA_V2
        argv_flags = ["--ignore-user-config"]
    else:
        workspace = tmp_path / ("other-role" if scope == "other-role" else "research-recorder") / "provider"
        phase_dir = "drafting"
        schema_ref = CODEX_SUPERVISOR_REQUEST_SCHEMA_V2
        argv_flags = ["--ignore-user-config"]
    _, key = ensure_transport_key(workspace)
    job_ref = "timeline-summary-4b3ab1c90ffb493d890b70e6ec655352"
    operation = workspace / "provider-operations" / hashlib.sha256(job_ref.encode()).hexdigest() / phase_dir
    operation.mkdir(parents=True)
    paths = {name + "_path": operation / filename for name, filename in (
        ("prompt", "prompt.txt"), ("schema", "output-schema.json"), ("result", "last-message.json"),
        ("stdout", "stdout.jsonl"), ("lock", "supervisor.lock"), ("ready", "supervisor-ready.json"),
        ("started", "provider-started.json"), ("receipt", "supervisor-exit.json"), ("stop", "supervisor-stop.json"),
    )}
    paths["prompt_path"].write_text(scenario)
    paths["schema_path"].write_text("{}")
    argv = [str(executable), "exec", *argv_flags, "--output-schema", str(paths["schema_path"]),
            "--output-last-message", str(paths["result_path"]), "-"]
    invocation = {"job_ref": job_ref, "execution_contract": {"provider_version": version}}
    data = transport_canonical_json(invocation).encode()
    (operation / "invocation.json").write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    request = operation / "supervisor-request.json"
    request_payload = {
        "schema_ref": schema_ref, "invocation_hash": digest,
        "argv": argv, "timeout_seconds": 5, "stream_max_bytes": 4096,
        "result_max_bytes": 1024,
        **{name: str(path) for name, path in paths.items()},
    }
    if schema_ref == CODEX_SUPERVISOR_REQUEST_SCHEMA_V2:
        request_payload["prompt_max_bytes"] = 4096
    write_supervisor_request(request, request_payload, key)
    return request, key, digest, paths


def _run(request):
    env = os.environ.copy()
    env.pop("CODEX_HOME", None)
    return subprocess.run([sys.executable, "-m", "meta_research.provider_supervisor", str(request)],
                          env=env, capture_output=True, timeout=12, check=False)


@pytest.mark.parametrize(("version", "scope", "scenario"), [
    ("0.169.0", "recorder", "normal"),
    ("0.169.0", "recorder", "overflow"),
    ("0.169.0", "recorder", "exit"),
    ("0.169.0", "deepfetch", "normal"),
    ("0.169.0", "deepfetch", "overflow"),
    ("0.169.0", "drafting", "normal"),
    ("0.159.0", "recorder", "normal"),
    ("0.169.0", "other-role", "normal"),
    ("0.169.0", "unmanaged", "normal"),
])
def test_posix_provider_result_transport_uses_private_named_fifo(tmp_path, version, scope, scenario):
    request, key, digest, paths = _operation(tmp_path, version=version, scope=scope, scenario=scenario)
    first = _run(request)
    assert first.returncode == 0, first.stderr
    receipt, _ = read_verified_exit_receipt(paths["receipt_path"], key=key, invocation_hash=digest,
        **{name: paths[name] for name in ("prompt_path", "schema_path", "stdout_path", "result_path")})
    observed = json.loads(paths["stdout_path"].read_text())
    assert observed["named"] is True
    assert (observed["mode"], observed["parent_mode"]) == (0o600, 0o700)
    assert not list(request.parent.glob(".result-pipe-*"))
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
    assert not list(request.parent.glob(".result-pipe-*"))


@pytest.mark.parametrize("damage", ["replace", "unlink"])
def test_named_fifo_rejects_tampered_pipe_without_sealing(tmp_path, damage):
    request, _, _, paths = _operation(tmp_path, scope="deepfetch", scenario=damage)
    assert _run(request).returncode == 70
    assert not paths["receipt_path"].exists()
    assert not paths["result_path"].exists()
    assert (request.parent / "launches").read_text() == "1"
    assert not list(request.parent.glob(".result-pipe-*"))
