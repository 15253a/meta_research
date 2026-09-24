"""Real bridge stdout must flow before a long-running Provider reaches EOF."""
import io
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
            expected += b'\n{"type":"meta_research.provider_error","family":"codex","error_kind":"provider_failed"}\n'
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


def test_nonzero_provider_large_stdout_reaches_signed_exit_without_losing_tail(tmp_path):
    from meta_research.harness_adapters import HarnessSupervisorTransport
    payload = b'x' * (64 * 1024 + 193) + b' complete-tail\n'
    script = f'import os\nos.write(1, {payload!r})\nos._exit(101)\n'
    completed = HarnessSupervisorTransport(tmp_path / 'supervisor')(
        [sys.executable, '-c', script], 'isolated prompt', None,
        {'META_RESEARCH_HARNESS_FAMILY': 'codex',
         'META_RESEARCH_PROVIDER_OPERATION_REF': 'isolated-large-stdout:harness_turn:1',
         'META_RESEARCH_HARNESS_WORKSPACE': str(tmp_path),
         'ISOLATED_TOKEN': 'synthetic-secret-for-output-test'},
    )
    assert completed.returncode == 101
    assert completed.stdout.encode() == payload + b'\n{"type":"meta_research.provider_error","family":"codex","error_kind":"provider_failed"}\n'
    assert completed.meta_research_transport_receipt['termination_reason'] == 'completed'
    assert completed.meta_research_transport_receipt['provider_returncode'] == 101
