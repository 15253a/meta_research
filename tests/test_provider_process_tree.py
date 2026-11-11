from __future__ import annotations

import os
import json
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from meta_research.provider_supervisor import (
    SUPERVISOR_REQUEST_SCHEMA_V2,
    ensure_transport_key,
    read_transport_envelope,
    request_supervisor_stop,
    supervisor_request_never_started,
    write_supervisor_request,
    write_supervisor_stop_request,
)


pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux process ownership")


PROVIDER = r'''
import os, signal, sys, time
from pathlib import Path
operation = Path(sys.argv[1])
mode = sys.argv[2]
with (operation / 'native-launches').open('a') as launches:
    launches.write('launched\n')
sys.stdin.buffer.read()
while not (operation / 'provider-started.json').exists():
    time.sleep(.005)
result_fd = os.open(sys.argv[sys.argv.index('--output-last-message') + 1], os.O_WRONLY)
os.write(result_fd, b'{}')
os.close(result_fd)
if mode == 'direct':
    sys.exit(23)
if mode == 'signal':
    os.kill(os.getpid(), signal.SIGTERM)
if mode in ('detached', 'old-fd', 'reopen'):
    target = operation / 'late-write'
    fd = os.open(target, os.O_WRONLY | os.O_CREAT, 0o600) if mode == 'old-fd' else None
    if os.fork() == 0:
        os.setsid()
        if os.fork() != 0:
            os._exit(0)
        os.environ.clear()
        os.chdir('/')
        for descriptor in (0, 1, 2):
            os.close(descriptor)
        if fd is not None:
            os.set_inheritable(fd, True)
        worker = """import os,sys,time
from pathlib import Path
operation=Path(sys.argv[1])
target=operation/'late-write'
fd=int(sys.argv[2])
if sys.argv[3]=='reopen':
    temporary=os.open(target,os.O_WRONLY|os.O_CREAT,0o600)
    os.close(temporary)
(operation/'worker-environment').write_bytes(Path('/proc/self/environ').read_bytes())
(operation/'fixture-ready').write_text(str(os.getpid()))
time.sleep(1)
if fd>=0:
    os.write(fd,b'late')
else:
    target.write_text('late')
"""
        os.execve(sys.executable, [sys.executable, '-c', worker, str(operation),
                                  str(fd if fd is not None else -1), mode], {})
    while not (operation / 'fixture-ready').exists():
        time.sleep(.005)
    os._exit(23)
(operation / 'fixture-ready').write_text(str(os.getpid()))
started = time.monotonic()
while True:
    if mode == 'bounded' and time.monotonic() - started > 1:
        sys.exit(0)
    if mode == 'output':
        print('output' * 1000, flush=True)
    else:
        (operation / 'heartbeat').write_text(str(time.monotonic_ns()))
    time.sleep(.01)
'''


def _launch(root: Path, mode: str, *, timeout: float = 4, supervisor: str | None = None):
    _, key = ensure_transport_key(root)
    invocation = "a" * 64
    operation = root / "provider-operations" / invocation[:2] / invocation
    operation.mkdir(parents=True)
    paths = {name + "_path": operation / filename for name, filename in (
        ("prompt", "prompt.txt"), ("schema", "output-schema.json"),
        ("stdout", "stdout.jsonl"), ("result", "last-message.json"),
        ("lock", "supervisor.lock"), ("ready", "supervisor-ready.json"),
        ("started", "provider-started.json"), ("receipt", "supervisor-exit.json"),
        ("stop", "supervisor-stop.json"))}
    paths["prompt_path"].write_text("isolated local process fixture")
    paths["schema_path"].write_text("{}")
    request = operation / "supervisor-request.json"
    write_supervisor_request(request, {
        "schema_ref": SUPERVISOR_REQUEST_SCHEMA_V2,
        "invocation_hash": invocation,
        "argv": [sys.executable, "-c", PROVIDER, str(operation), mode,
                 "--output-schema", str(paths["schema_path"]),
                 "--output-last-message", str(paths["result_path"]), "-"],
        "timeout_seconds": timeout, "stream_max_bytes": 1024, "result_max_bytes": 1024,
        **{name: str(path) for name, path in paths.items()},
    }, key)
    process = subprocess.Popen(
        ([sys.executable, "-c", supervisor, str(request)] if supervisor else
         [sys.executable, "-m", "meta_research.provider_supervisor", str(request)]),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    return process, operation, key


def _wait_file(path: Path):
    deadline = time.monotonic() + 4
    while not path.exists():
        assert time.monotonic() < deadline, path
        time.sleep(.01)


def _finish(process, operation, key):
    stdout, stderr = process.communicate(timeout=8)
    assert (process.returncode, stdout, stderr) == (0, b"", b"")
    return read_transport_envelope(operation / "supervisor-exit.json", key)


@pytest.mark.parametrize("mode,code,reason", [
    ("direct", 23, "completed"), ("signal", -15, "completed"),
    ("detached", 23, "descendant_process"), ("old-fd", 23, "descendant_process"),
    ("reopen", 23, "descendant_process"), ("timeout", -15, "timeout"),
    ("output", -15, "output_limit"), ("stop", -15, "stopped"),
])
def test_linux_supervisor_proves_owned_tree_closed(tmp_path, mode, code, reason):
    process, operation, key = _launch(tmp_path, mode, timeout=.4 if mode == "timeout" else 4)
    if mode == "stop":
        _wait_file(operation / "fixture-ready")
        assert request_supervisor_stop(operation, key=key, invocation_hash="a" * 64,
                                       ready_schema="meta-research/provider-supervisor-ready/v2")
    receipt = _finish(process, operation, key)
    assert receipt["returncode"] == code
    assert receipt["termination_reason"] == reason
    assert receipt["process_tree"]["state"] == "closed"
    assert receipt["process_tree"]["mechanism"] == "linux-subreaper-echild"
    if mode in {"detached", "old-fd", "reopen"}:
        time.sleep(1.1)
        target = operation / "late-write"
        assert not target.exists() or target.read_bytes() == b""
        pid = int((operation / "fixture-ready").read_text())
        assert not Path(f"/proc/{pid}").exists()
        assert (operation / "worker-environment").read_bytes() == b""


def test_cleanup_does_not_stop_parallel_operation(tmp_path):
    other, other_operation, other_key = _launch(tmp_path / "other", "heartbeat")
    _wait_file(other_operation / "heartbeat")
    before = (other_operation / "heartbeat").read_text()
    process, operation, key = _launch(tmp_path / "closed", "detached")
    receipt = _finish(process, operation, key)
    assert receipt["returncode"] == 23
    assert other.poll() is None
    assert (other_operation / "heartbeat").read_text() != before
    write_supervisor_stop_request(other_operation / "supervisor-stop.json",
                                  key=other_key, invocation_hash="a" * 64)
    assert _finish(other, other_operation, other_key)["termination_reason"] == "stopped"


def test_unissued_stop_fences_a_delayed_supervisor(tmp_path):
    process, operation, key = _launch(tmp_path, "direct", supervisor="""
import sys,time
from meta_research.provider_supervisor import main
time.sleep(.5)
raise SystemExit(main([sys.argv[1]]))
""")
    assert supervisor_request_never_started(operation, key=key, invocation_hash="a" * 64,
                                            request_schema=SUPERVISOR_REQUEST_SCHEMA_V2)
    receipt = _finish(process, operation, key)
    assert receipt["process_tree"]["mechanism"] == "not-started"
    assert receipt["returncode"] is None
    assert not (operation / "native-launches").exists()


def test_missing_request_does_not_prove_unissued_work(tmp_path):
    operation = tmp_path / "pending-operation"
    operation.mkdir()
    assert not supervisor_request_never_started(operation, key=b"k" * 32,
                                                invocation_hash="a" * 64,
                                                request_schema=SUPERVISOR_REQUEST_SCHEMA_V2)
    assert not (operation / "supervisor-stop.json").exists()


@pytest.mark.parametrize("failure", ["cleanup", "receipt"])
def test_missing_tree_or_durable_receipt_blocks_reentry(tmp_path, failure):
    supervisor = r'''
import sys
import meta_research.provider_supervisor as supervisor
if FAILURE == 'receipt':
    def reject(*args, **kwargs):
        raise OSError('fixture interrupted before durable receipt')
    supervisor.write_exit_receipt = reject
else:
    original_popen = supervisor.subprocess.Popen
    helper = """
import sys
import meta_research.linux_provider_tree as tree
def reject(*args):
    raise OSError('fixture pidfd signal failure')
tree._signal_children = reject
raise SystemExit(tree.main())
"""
    def launch(argv, **kwargs):
        if argv[1:3] == ['-m', 'meta_research.linux_provider_tree']:
            argv = [argv[0], '-c', helper, *argv[3:]]
        return original_popen(argv, **kwargs)
    supervisor.subprocess.Popen = launch
raise SystemExit(supervisor.main([sys.argv[1]]))
'''.replace("FAILURE", repr(failure))
    process, operation, key = _launch(tmp_path, "detached", supervisor=supervisor)
    process.communicate(timeout=8)
    assert process.returncode == 70
    assert not (operation / "supervisor-exit.json").exists()
    again = subprocess.run([sys.executable, "-m", "meta_research.provider_supervisor",
                            str(operation / "supervisor-request.json")], timeout=5)
    assert again.returncode == 70
    assert (operation / "native-launches").read_text() == "launched\n"
    assert not supervisor_request_never_started(operation, key=key, invocation_hash="a" * 64,
                                                request_schema=SUPERVISOR_REQUEST_SCHEMA_V2)
    time.sleep(1.1)


def test_supervisor_killed_after_launch_cannot_prove_terminal(tmp_path):
    process, operation, key = _launch(tmp_path, "bounded")
    _wait_file(operation / "fixture-ready")
    process.kill()
    process.communicate(timeout=5)
    time.sleep(1.2)
    assert not (operation / "supervisor-exit.json").exists()
    again = subprocess.run([sys.executable, "-m", "meta_research.provider_supervisor",
                            str(operation / "supervisor-request.json")], timeout=5)
    assert again.returncode == 70
    assert (operation / "native-launches").read_text() == "launched\n"
    assert not supervisor_request_never_started(operation, key=key, invocation_hash="a" * 64,
                                                request_schema=SUPERVISOR_REQUEST_SCHEMA_V2)


def test_reaper_loss_does_not_produce_a_receipt(tmp_path):
    from meta_research.linux_provider_tree import _pidfd_open, _signal_pidfd
    process, operation, key = _launch(tmp_path, "bounded")
    _wait_file(operation / "fixture-ready")
    started = read_transport_envelope(operation / "provider-started.json", key)
    descriptor = _pidfd_open(started["provider_process_id"])
    try:
        _signal_pidfd(descriptor, signal.SIGKILL)
    finally:
        os.close(descriptor)
    process.communicate(timeout=5)
    assert process.returncode == 70
    assert not (operation / "supervisor-exit.json").exists()
    time.sleep(1.1)


def test_native_provider_cannot_inherit_helper_report_pipe():
    from meta_research.linux_provider_tree import LinuxProviderJob
    job = LinuxProviderJob()
    try:
        process = job.spawn([sys.executable, "-c", """
import json,os
from pathlib import Path
descriptors = []
for path in Path('/proc/self/fd').iterdir():
    try:
        descriptors.append(os.fstat(int(path.name)).st_ino)
    except OSError:
        pass
print(json.dumps(descriptors))
"""], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           start_new_session=True)
        report_inode = os.fstat(job._control).st_ino
        stdout, stderr = process.communicate(timeout=5)
        assert stderr == b""
        assert report_inode not in json.loads(stdout)
        assert job.result()["returncode"] == 0
    finally:
        job.close()


def test_immediate_stop_waits_for_reaper_signal_handlers():
    from meta_research.linux_provider_tree import LinuxProviderJob
    job = LinuxProviderJob()
    try:
        job.spawn([sys.executable, "-c", "import time; time.sleep(5)"],
                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                  stderr=subprocess.DEVNULL, start_new_session=True)
        assert job.terminate()
        result = job.result()
        assert result["tree_closed"] is True
        assert result["returncode"] == (-15 if result["native_started"] else None)
    finally:
        job.close()
