"""Real bridge stdout must flow before a long-running Provider reaches EOF."""
import io
import hashlib
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time

import pytest

from meta_research.harness_cli_bridge import _copy_redacted


@pytest.mark.parametrize('exit_code', (0, 101))
def test_bridge_publishes_first_record_while_provider_is_still_running(tmp_path, exit_code):
    marker = tmp_path / 'provider-ready'
    secret = 'isolated-secret-123456'
    payload = b'first-live-record\n' + b'x' * 128 + b'\n'
    script = (
        'import os,time\n'
        f'os.write(1, {payload!r})\n'
        f'open({str(marker)!r}, "w").write("ready")\n'
        'time.sleep(2)\n'
        f'os.write(1, {secret[:10].encode()!r})\n'
        'time.sleep(0.03)\n'
        f'os.write(1, {secret[10:].encode()!r} + b" final-tail\\n")\n'
        f'os._exit({exit_code})\n'
    )
    argv_path = tmp_path / 'argv.json'
    argv_path.write_text(json.dumps([sys.executable, '-c', script]))
    schema = tmp_path / 'schema.json'
    schema.write_text('{}')
    result = tmp_path / 'result.json'
    env = {key: value for key, value in os.environ.items() if key in {'PATH', 'PYTHONPATH', 'SYSTEMROOT'}}
    env.update({'META_RESEARCH_HARNESS_WORKSPACE': str(tmp_path), 'ISOLATED_TOKEN': secret})
    process = subprocess.Popen([
        sys.executable, '-m', 'meta_research.harness_cli_bridge', '--family', 'codex',
        '--provider-argv', str(argv_path), '--output-schema', str(schema),
        '--output-last-message', str(result), '-'
    ], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    try:
        deadline = time.monotonic() + 3
        while not marker.exists():
            assert process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.01)
        ready, _, _ = select.select([process.stdout], [], [], 0.5)
        assert ready, 'bridge buffered a real flushed first record until 64 KiB or EOF'
        prefix = os.read(process.stdout.fileno(), 4096)
        assert b'first-live-record\n' in prefix
        assert process.poll() is None
        remainder, errors = process.communicate(timeout=4)
        assert process.returncode == exit_code, errors.decode()
        output = prefix + remainder
        expected = payload + b'*' * len(secret) + b' final-tail\n'
        if exit_code:
            prefix = b'\n{"type":"meta_research.provider_error","family":"codex","error_kind":"provider_failed","stderr_observation":'
            assert output.startswith(expected + prefix)
            assert output.endswith(b'\n')
            observation = json.loads(output[len(expected + prefix):-2])
            assert observation == {
                "schema_ref": "meta-research/provider-stderr-summary/v1",
                "status": "available",
                "observed_bytes": 0,
                "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                "limited": False,
                "exact_content_source": "provider stderr bytes retained by harness bridge",
            }
        else:
            assert output == expected
        assert json.loads(result.read_text())['returncode'] == exit_code
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=4)


@pytest.mark.parametrize('read1_available', (False, True))
def test_redaction_across_chunks_and_eof_preserves_every_nonsecret_byte(read1_available):
    chunks = [b'prefix long-sec', b'ret-value suffix ', b'last tail']
    class ChunkReader:
        def read(self, _size):
            return chunks.pop(0) if chunks else b''
        def close(self):
            pass
    reader = ChunkReader()
    if read1_available:
        reader.read1 = reader.read
    destination = io.BytesIO()
    _copy_redacted(reader, destination, (b'long-secret-value',))
    assert destination.getvalue() == b'prefix ' + b'*' * len(b'long-secret-value') + b' suffix last tail'


def test_bridge_projects_provider_stderr_without_forwarding_body(tmp_path):
    secret = "provider-response-secret-123"
    payload = {"type": "thread.started", "thread_id": "bridge-stderr"}
    script = (
        "import json,sys\n"
        f"sys.stderr.write({secret!r})\n"
        f"print(json.dumps({payload!r}), flush=True)\n"
        "sys.exit(101)\n"
    )
    argv_path = tmp_path / "argv.json"
    argv_path.write_text(json.dumps([sys.executable, "-c", script]))
    schema = tmp_path / "schema.json"
    schema.write_text("{}")
    result = tmp_path / "result.json"
    env = {key: value for key, value in os.environ.items() if key in {"PATH", "PYTHONPATH", "SYSTEMROOT"}}
    env["META_RESEARCH_HARNESS_WORKSPACE"] = str(tmp_path)
    process = subprocess.run(
        [sys.executable, "-m", "meta_research.harness_cli_bridge", "--family", "codex",
         "--provider-argv", str(argv_path), "--output-schema", str(schema),
         "--output-last-message", str(result), "-"],
        input=b"bounded prompt", capture_output=True, env=env, check=False,
    )
    assert process.returncode == 101
    assert secret.encode() not in process.stdout
    assert secret.encode() not in process.stderr
    assert b"provider-stderr-summary" in process.stderr
    lines = process.stdout.decode().splitlines()
    event = json.loads(lines[-1])
    observation = event["stderr_observation"]
    assert observation["observed_bytes"] == len(secret.encode())
    assert observation["sha256"] == hashlib.sha256(secret.encode()).hexdigest()
    assert observation["limited"] is False


def test_nonzero_provider_large_stdout_reaches_signed_exit_without_losing_tail(tmp_path):
    from meta_research.harness_adapters import HarnessSupervisorTransport
    payload = b'x' * (64 * 1024 + 193) + b' complete-tail\n'
    script = f'import os\nos.write(1, {payload!r})\nos._exit(101)\n'
    completed = HarnessSupervisorTransport(tmp_path / 'supervisor')(
        [sys.executable, '-c', script], 'isolated prompt', None,
        {'META_RESEARCH_HARNESS_FAMILY': 'codex',
         'META_RESEARCH_PROVIDER_OPERATION_REF': 'isolated-large-stdout:harness_turn:1',
         'META_RESEARCH_HARNESS_WORKSPACE': str(tmp_path),
         'PYTHONPATH': str(Path(__file__).parents[1] / 'src'),
         'ISOLATED_TOKEN': 'synthetic-secret-for-output-test'},
    )
    assert completed.returncode == 101
    output = completed.stdout.encode()
    prefix = payload + b'\n{"type":"meta_research.provider_error","family":"codex","error_kind":"provider_failed","stderr_observation":'
    assert output.startswith(prefix)
    assert output.endswith(b'\n')
    assert json.loads(output[len(prefix):-2]) == {
        "schema_ref": "meta-research/provider-stderr-summary/v1",
        "status": "available",
        "observed_bytes": 0,
        "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "limited": False,
        "exact_content_source": "provider stderr bytes retained by harness bridge",
    }
    assert completed.meta_research_transport_receipt['termination_reason'] == 'completed'
    assert completed.meta_research_transport_receipt['provider_returncode'] == 101


def test_durable_harness_sidecar_keeps_bridge_stderr_summary_only(tmp_path):
    from meta_research.harness_adapters import HarnessSupervisorTransport
    secret = b"provider-response-secret-456"
    provider = tmp_path / "provider.py"
    provider.write_text(
        "import json, os, sys\n"
        f"os.write(2, {secret!r})\n"
        "print(json.dumps({'type':'thread.started','thread_id':'bridge-sidecar'}), flush=True)\n"
        "sys.exit(101)\n",
        encoding="utf-8",
    )
    environment = {
        "META_RESEARCH_HARNESS_FAMILY": "codex",
        "META_RESEARCH_PROVIDER_OPERATION_REF": "bridge-sidecar:1",
        "META_RESEARCH_HARNESS_WORKSPACE": str(tmp_path),
        "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
    }
    completed = HarnessSupervisorTransport(tmp_path / "supervisor")(
        [sys.executable, str(provider)], "bounded prompt", 10.0, environment
    )
    assert completed.returncode == 101
    assert secret.decode() not in completed.stdout
    operation = next((tmp_path / "supervisor" / "provider-operations").rglob("provider-stderr.log"))
    sidecar = operation.read_bytes()
    assert secret not in sidecar
    summary = json.loads(sidecar)
    assert summary["schema_ref"] == "meta-research/provider-stderr-summary/v1"
    assert summary["observed_bytes"] == len(secret)
    assert summary["sha256"] == hashlib.sha256(secret).hexdigest()


def test_retired_claude_bridge_rejects_before_provider_spawn(tmp_path):
    marker = tmp_path / "unexpected-provider"
    argv_path = tmp_path / "argv.json"
    argv_path.write_text(json.dumps([sys.executable, "-c",
        f"from pathlib import Path; Path({str(marker)!r}).touch()"]))
    schema = tmp_path / "schema.json"
    schema.write_text("{}")
    result = tmp_path / "result.json"
    completed = subprocess.run([sys.executable, "-m", "meta_research.harness_cli_bridge",
        "--family", "claude", "--provider-argv", str(argv_path),
        "--output-schema", str(schema), "--output-last-message", str(result), "-"],
        input=b"bounded prompt", capture_output=True,
        env={**os.environ, "META_RESEARCH_HARNESS_WORKSPACE": str(tmp_path)}, timeout=10)
    assert completed.returncode == 64
    assert b"invalid choice" in completed.stderr and b"claude" in completed.stderr
    assert not marker.exists() and not result.exists()
