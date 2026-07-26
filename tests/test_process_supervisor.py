"""CP11.3b: external guardian, owner-death fence and durable receipts."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from orchestrator import process_supervisor as PS
from orchestrator.instance_lease import InstanceBusyError, InstanceLease
from orchestrator.process_supervisor import (
    ExecutionCancelled,
    ExecutionCleanupError,
    ExecutionRecoveryError,
    ExecutionSupervisor,
    ExecutionSupervisorError,
    SupervisedTimeoutExpired,
    _reset_global_hard_stop_for_tests,
    atomic_write_receipt,
    terminate_all_supervised_executions,
)


@pytest.fixture(autouse=True)
def _clean_global_hard_stop():
    _reset_global_hard_stop_for_tests()
    yield
    _reset_global_hard_stop_for_tests()


def _supervisor(tmp_path: Path, *, grace: float = 0.1) -> ExecutionSupervisor:
    return ExecutionSupervisor(
        receipt_dir=tmp_path / "receipts", owner_id="test-owner",
        term_grace_s=grace)


def test_success_nonzero_env_stdin_cwd_pass_fd_and_private_receipt(tmp_path):
    supervisor = _supervisor(tmp_path)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    stdin_file = tmp_path / "stdin"
    stdin_file.write_text("from-stdin", encoding="utf-8")
    asset = tmp_path / "asset"
    asset.write_text("from-asset", encoding="utf-8")
    asset_fd = os.open(asset, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        with stdin_file.open("rb") as stdin:
            result = supervisor.run(
                [sys.executable, "-c",
                 "import os,sys; print(sys.stdin.read(), "
                 "os.read(int(sys.argv[1]),99).decode(), os.getcwd(), os.environ['MR_SECRET'])",
                 str(asset_fd)],
                stdin=stdin, capture_output=True, timeout_s=2, cwd=cwd,
                env={**os.environ, "MR_SECRET": "must-not-enter-receipt"},
                pass_fds=(asset_fd,), kind="probe",
                operation_context={"cycle_id": "c7", "run_id": 11})
    finally:
        os.close(asset_fd)
    assert result.returncode == 0
    assert b"from-stdin from-asset" in result.stdout
    assert str(cwd).encode() in result.stdout
    assert result.receipt["outcome"] == "exit"
    assert result.receipt["context"] == {"cycle_id": "c7", "run_id": 11}
    assert result.receipt["group_drained"] is True
    assert (result.receipt_path.stat().st_mode & 0o777) == 0o600
    assert b"must-not-enter-receipt" not in result.receipt_path.read_bytes()

    failed = supervisor.run(
        [sys.executable, "-c", "raise SystemExit(7)"],
        capture_output=True, timeout_s=2, kind="probe")
    assert failed.returncode == 7 and failed.receipt["returncode"] == 7
    supervisor.close()


def test_resident_stage_is_owner_lifecycle_bound_and_close_cancels_it(tmp_path):
    supervisor = _supervisor(tmp_path, grace=0.08)
    with pytest.raises(ValueError, match="仅 codex-resident-stage"):
        supervisor.run(
            [sys.executable, "-c", "pass"], capture_output=True,
            timeout_s=None, kind="probe")

    caught = []

    def invoke() -> None:
        try:
            supervisor.run(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                capture_output=True, timeout_s=None,
                kind="codex-resident-stage")
        except BaseException as error:
            caught.append(error)

    thread = threading.Thread(target=invoke, name="resident-stage-test")
    thread.start()
    deadline = time.monotonic() + 3
    while supervisor.active_count == 0:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    supervisor.close(timeout_s=3)
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert len(caught) == 1 and isinstance(caught[0], ExecutionCancelled)
    receipt = caught[0].receipt
    assert receipt["kind"] == "codex-resident-stage"
    assert receipt["timeout_s"] is None
    # Cancellation may win before the guardian publishes its running receipt;
    # in either state no wall-clock deadline may appear.
    assert receipt.get("deadline_at_unix") is None
    assert receipt["outcome"] == "cancelled"


def test_timeout_kills_double_fork_setsid_descendant_that_ignores_term(tmp_path):
    marker = tmp_path / "late-marker"
    ready = tmp_path / "daemon-ready"
    supervisor = _supervisor(tmp_path, grace=0.12)
    code = r"""
import os, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
pid = os.fork()
if pid == 0:
    os.setsid()
    pid2 = os.fork()
    if pid2:
        os._exit(0)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    os.close(1); os.close(2)
    open(sys.argv[1], 'wb').close()
    time.sleep(1.5)
    open(sys.argv[2], 'wb').close()
    os._exit(0)
time.sleep(30)
"""
    with pytest.raises(SupervisedTimeoutExpired) as caught:
        supervisor.run(
            [sys.executable, "-c", code, str(ready), str(marker)], capture_output=True,
            timeout_s=0.8, kind="probe")
    receipt = caught.value.receipt
    assert receipt["outcome"] == "timeout"
    assert receipt["term_sent"] is True and receipt["kill_sent"] is True
    assert receipt["max_descendants"] >= 2
    assert ready.exists()
    time.sleep(1.6)
    assert not marker.exists()
    supervisor.close()


def test_normal_direct_exit_with_background_descendant_is_not_success(tmp_path):
    marker = tmp_path / "background-marker"
    supervisor = _supervisor(tmp_path, grace=0.08)
    code = r"""
import os, signal, sys, time
pid = os.fork()
if pid == 0:
    os.setsid()
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(0.5)
    open(sys.argv[1], 'wb').close()
    os._exit(0)
os._exit(0)
"""
    with pytest.raises(ExecutionCleanupError) as caught:
        supervisor.run(
            [sys.executable, "-c", code, str(marker)], capture_output=True,
            timeout_s=2, kind="probe")
    assert caught.value.receipt["outcome"] == "lingering_descendant"
    assert caught.value.receipt["group_drained"] is True
    time.sleep(0.6)
    assert not marker.exists()
    supervisor.close()


def test_hard_stop_cancels_registered_guardian_and_rejects_future_spawn(tmp_path):
    marker = tmp_path / "cancel-marker"
    ready = tmp_path / "cancel-ready"
    supervisor = _supervisor(tmp_path, grace=0.08)
    errors = []

    def worker():
        try:
            supervisor.run(
                [sys.executable, "-c",
                 "import signal,sys,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                 "open(sys.argv[1],'wb').close(); time.sleep(.6); "
                 "open(sys.argv[2],'wb').close(); time.sleep(30)",
                 str(ready), str(marker)],
                capture_output=True, timeout_s=30, kind="probe")
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=worker)
    thread.start()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not ready.exists():
        time.sleep(0.005)
    assert ready.exists() and supervisor.active_count == 1
    try:
        terminate_all_supervised_executions(wait_s=3)
        thread.join(1)
        assert not thread.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], ExecutionCancelled)
        assert errors[0].receipt["kill_sent"] is True
        with pytest.raises(Exception, match="hard-stop"):
            supervisor.run(
                [sys.executable, "-c", "pass"], capture_output=True,
                timeout_s=1, kind="probe")
        time.sleep(0.7)
        assert not marker.exists()
    finally:
        supervisor.close()
        _reset_global_hard_stop_for_tests()


def test_progress_observer_cancel_drains_guardian_and_keeps_supervisor_reusable(
        tmp_path):
    supervisor = _supervisor(tmp_path, grace=0.08)
    ready = tmp_path / "observer-cancel-ready"
    observations = []

    def observer():
        observations.append(time.monotonic())
        return ready.exists()

    try:
        with pytest.raises(ExecutionCancelled) as caught:
            supervisor.run(
                [sys.executable, "-c",
                 "import signal,sys,time; "
                 "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                 "open(sys.argv[1],'wb').close(); time.sleep(30)",
                 str(ready)],
                capture_output=True, timeout_s=30, kind="probe",
                progress_observer=observer, progress_interval_s=0.05)

        receipt = caught.value.receipt
        persisted = json.loads(caught.value.receipt_path.read_text())
        assert observations and ready.exists()
        assert receipt["state"] == "terminal"
        assert receipt["outcome"] == "cancelled"
        assert receipt["group_drained"] is True
        assert receipt["term_sent"] is True and receipt["kill_sent"] is True
        assert persisted == receipt
        assert supervisor.active_count == 0

        successor = supervisor.run(
            [sys.executable, "-c", "print('successor-ran')"],
            capture_output=True, timeout_s=2, kind="probe")
        assert successor.returncode == 0
        assert successor.stdout == b"successor-ran\n"
        assert successor.receipt["state"] == "terminal"
        assert successor.receipt["group_drained"] is True
    finally:
        supervisor.close()


def test_capture_observer_receives_each_stdout_byte_once_including_final_suffix(
        tmp_path):
    supervisor = _supervisor(tmp_path)
    chunks = []
    payload = "alpha-βeta-final".encode("utf-8")
    code = (
        "import os,time; "
        "os.write(1,b'alpha-'); time.sleep(.12); "
        "os.write(1,'βeta'.encode()); time.sleep(.12); "
        "os.write(1,b'-final')")
    try:
        result = supervisor.run(
            [sys.executable, "-c", code],
            capture_output=True, timeout_s=2, kind="probe",
            capture_observer=chunks.append, progress_interval_s=0.05)
    finally:
        supervisor.close()

    assert result.stdout == payload
    assert b"".join(chunks) == payload
    assert len(chunks) >= 2


def test_stream_capture_observer_preserves_stream_identity_without_buffering_result(
        tmp_path):
    supervisor = _supervisor(tmp_path)
    observed = []
    code = (
        "import os,time; "
        "os.write(1,b'alpha'); time.sleep(.12); "
        "os.write(2,b'warning'); time.sleep(.12); "
        "os.write(1,b'omega')")
    try:
        result = supervisor.run(
            [sys.executable, "-c", code],
            capture_output=True, capture_result=False,
            timeout_s=2, kind="probe",
            stream_capture_observer=lambda stream, chunk, frame_end: (
                observed.append((stream, chunk, frame_end))),
            progress_interval_s=0.05)
    finally:
        supervisor.close()

    assert [stream for stream, _chunk, _frame_end in observed] == [
        "stdout", "stderr", "stdout",
    ]
    assert b"".join(
        chunk for stream, chunk, _frame_end in observed
        if stream == "stdout"
    ) == b"alphaomega"
    assert b"".join(
        chunk for stream, chunk, _frame_end in observed
        if stream == "stderr"
    ) == b"warning"
    assert [frame_end for _stream, _chunk, frame_end in observed] == sorted(
        frame_end for _stream, _chunk, frame_end in observed)
    assert result.stdout is None
    assert result.stderr is None
    assert result.receipt["capture_stream_identity"] is True
    assert PS.read_execution_capture(
        result.receipt, stream="stdout") == b"alphaomega"
    assert PS.read_execution_capture(
        result.receipt, stream="stderr") == b"warning"


def test_stream_capture_guardian_preserves_stderr_before_stdout_without_sleep(
        tmp_path):
    supervisor = _supervisor(tmp_path)
    observed = []
    try:
        result = supervisor.run(
            [
                sys.executable, "-c",
                "import os; os.write(2,b'stderr-first\\n'); "
                "os.write(1,b'stdout-second\\n')",
            ],
            capture_output=True, capture_result=False,
            timeout_s=2, kind="probe",
            stream_capture_observer=lambda stream, chunk, frame_end: (
                observed.append((stream, chunk, frame_end))),
            progress_interval_s=0.05)
    finally:
        supervisor.close()

    assert [(stream, chunk) for stream, chunk, _end in observed] == [
        ("stderr", b"stderr-first\n"),
        ("stdout", b"stdout-second\n"),
    ]
    assert observed[0][2] < observed[1][2]
    assert result.receipt["capture_frame_format"] == "ordered-stream-v1"
    assert result.receipt["capture_frame_bytes"] == observed[-1][2]
    assert result.receipt["capture_frame_sha256"].startswith("sha256:")
    assert result.receipt["capture_frame_device"] >= 0
    assert result.receipt["capture_frame_inode"] > 0
    assert PS.verified_execution_frame_size(
        result.receipt) == observed[-1][2]


def test_capture_observer_requires_capture_output_and_callable(tmp_path):
    supervisor = _supervisor(tmp_path)
    try:
        with pytest.raises(ValueError, match="capture_output"):
            supervisor.run(
                [sys.executable, "-c", "pass"],
                capture_output=False, timeout_s=2, kind="probe",
                capture_observer=lambda _chunk: None)
        with pytest.raises(ValueError, match="capture_observer"):
            supervisor.run(
                [sys.executable, "-c", "pass"],
                capture_output=True, timeout_s=2, kind="probe",
                capture_observer=object())
        with pytest.raises(ValueError, match="capture_output"):
            supervisor.run(
                [sys.executable, "-c", "pass"],
                capture_output=False, timeout_s=2, kind="probe",
                stream_capture_observer=lambda _stream, _chunk, _end: None)
        with pytest.raises(ValueError, match="stream_capture_observer"):
            supervisor.run(
                [sys.executable, "-c", "pass"],
                capture_output=True, timeout_s=2, kind="probe",
                stream_capture_observer=object())
        with pytest.raises(ValueError, match="不得同时"):
            supervisor.run(
                [sys.executable, "-c", "pass"],
                capture_output=True, timeout_s=2, kind="probe",
                capture_observer=lambda _chunk: None,
                stream_capture_observer=lambda _stream, _chunk, _end: None)
    finally:
        supervisor.close()


def test_capture_observer_failure_cancels_exact_execution_and_attaches_receipt(
        tmp_path):
    supervisor = _supervisor(tmp_path, grace=0.08)

    def reject(_chunk):
        raise RuntimeError("ledger rejected capture")

    try:
        with pytest.raises(RuntimeError, match="ledger rejected") as caught:
            supervisor.run(
                [sys.executable, "-c",
                 "import os,time; os.write(1,b'bad-event\\n'); time.sleep(30)"],
                capture_output=True, timeout_s=30, kind="probe",
                capture_observer=reject, progress_interval_s=0.05)
        receipt = caught.value.execution_receipt
        assert receipt["state"] == "terminal"
        assert receipt["outcome"] == "cancelled"
        assert receipt["group_drained"] is True
        assert supervisor.active_count == 0
    finally:
        supervisor.close()


@pytest.mark.parametrize("ignored", [True, False])
def test_sigint_ignore_or_benign_custom_handler_keeps_original_semantics(
        tmp_path, ignored):
    supervisor = _supervisor(tmp_path)
    ready = tmp_path / "sigint-ready"
    seen = []
    original_handler = signal.getsignal(signal.SIGINT)

    def custom_handler(signum, _frame):
        seen.append(signum)

    configured_handler = signal.SIG_IGN if ignored else custom_handler
    signal.signal(signal.SIGINT, configured_handler)

    def sender():
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not ready.exists():
            time.sleep(0.005)
        assert ready.exists()
        os.kill(os.getpid(), signal.SIGINT)

    thread = threading.Thread(target=sender)
    thread.start()
    try:
        result = supervisor.run(
            [sys.executable, "-c",
             "import sys,time; open(sys.argv[1],'wb').close(); time.sleep(.2)",
             str(ready)], capture_output=True, timeout_s=2, kind="probe")
        restored_handler = signal.getsignal(signal.SIGINT)
    finally:
        signal.signal(signal.SIGINT, original_handler)
        thread.join(2)
        supervisor.close()
    assert result.returncode == 0
    if ignored:
        assert seen == [] and restored_handler == signal.SIG_IGN
    else:
        assert seen == [signal.SIGINT] and restored_handler is custom_handler


def test_custom_sigint_handler_can_replace_its_successor_without_dropping_barrier(
        tmp_path):
    supervisor = _supervisor(tmp_path)
    ready = tmp_path / "sigint-replace-ready"
    seen = []
    original_handler = signal.getsignal(signal.SIGINT)

    def successor(signum, _frame):
        seen.append(("successor", signum))

    def replacing_handler(signum, _frame):
        seen.append(("original", signum))
        signal.signal(signal.SIGINT, successor)

    signal.signal(signal.SIGINT, replacing_handler)

    def sender():
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not ready.exists():
            time.sleep(0.005)
        assert ready.exists()
        os.kill(os.getpid(), signal.SIGINT)
        time.sleep(0.05)
        os.kill(os.getpid(), signal.SIGINT)

    thread = threading.Thread(target=sender)
    thread.start()
    try:
        result = supervisor.run(
            [sys.executable, "-c",
             "import sys,time; open(sys.argv[1],'wb').close(); time.sleep(.3)",
             str(ready)], capture_output=True, timeout_s=2, kind="probe")
        assert signal.getsignal(signal.SIGINT) is successor
    finally:
        signal.signal(signal.SIGINT, original_handler)
        thread.join(2)
        supervisor.close()
    assert result.returncode == 0
    assert seen == [("original", signal.SIGINT), ("successor", signal.SIGINT)]


def test_raising_sigint_handler_is_deferred_until_guardian_drain(tmp_path):
    supervisor = _supervisor(tmp_path, grace=0.08)
    ready = tmp_path / "sigint-ready"
    late = tmp_path / "sigint-late"
    sentinel = RuntimeError("custom-sigint")
    original_handler = signal.getsignal(signal.SIGINT)

    def raising_handler(_signum, _frame):
        raise sentinel

    signal.signal(signal.SIGINT, raising_handler)

    def sender():
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not ready.exists():
            time.sleep(0.005)
        assert ready.exists()
        os.kill(os.getpid(), signal.SIGINT)

    thread = threading.Thread(target=sender)
    thread.start()
    try:
        with pytest.raises(RuntimeError) as caught:
            supervisor.run(
                [sys.executable, "-c",
                 "import signal,sys,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                 "open(sys.argv[1],'wb').close(); time.sleep(.6); "
                 "open(sys.argv[2],'wb').close(); time.sleep(30)",
                 str(ready), str(late)],
                capture_output=True, timeout_s=30, kind="probe")
        assert caught.value is sentinel
        assert caught.value.execution_receipt["outcome"] == "cancelled"
        assert caught.value.execution_receipt["group_drained"] is True
        assert signal.getsignal(signal.SIGINT) is raising_handler
        assert supervisor.active_count == 0
        with pytest.raises(ExecutionSupervisorError, match="hard-stop"):
            supervisor.run(
                [sys.executable, "-c", "pass"], capture_output=True,
                timeout_s=1, kind="probe")
    finally:
        signal.signal(signal.SIGINT, original_handler)
        thread.join(2)
        supervisor.close()
    time.sleep(0.7)
    assert not late.exists()


def test_sigint_default_disposition_replayed_only_after_drain(tmp_path):
    ready = tmp_path / "default-ready"
    late = tmp_path / "default-late"
    receipts = tmp_path / "receipts"
    probe = r"""
import os, signal, sys, threading, time
from pathlib import Path
from orchestrator.process_supervisor import ExecutionSupervisor
receipts, ready, late = map(Path, sys.argv[1:])
signal.signal(signal.SIGINT, signal.SIG_DFL)
supervisor = ExecutionSupervisor(
    receipt_dir=receipts, owner_id='default-sigint', term_grace_s=.08)
def sender():
    while not ready.exists():
        time.sleep(.005)
    os.kill(os.getpid(), signal.SIGINT)
threading.Thread(target=sender, daemon=True).start()
code = "import signal,sys,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); " \
       "open(sys.argv[1],'wb').close(); time.sleep(.6); " \
       "open(sys.argv[2],'wb').close(); time.sleep(30)"
supervisor.run([sys.executable, '-c', code, str(ready), str(late)],
               capture_output=True, timeout_s=30, kind='probe')
raise AssertionError('SIG_DFL was not replayed')
"""
    result = subprocess.run(
        [sys.executable, "-c", probe, str(receipts), str(ready), str(late)],
        capture_output=True, text=True, timeout=6,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)})
    assert result.returncode == -signal.SIGINT, result.stderr
    receipt_path = next(receipts.glob("execution-*.json"))
    receipt = json.loads(receipt_path.read_text())
    assert receipt["outcome"] == "cancelled"
    assert receipt["group_drained"] is True
    time.sleep(0.7)
    assert not late.exists()


def test_guardian_closes_its_pass_fd_copy_so_pipe_eof_is_preserved(tmp_path):
    supervisor = _supervisor(tmp_path)
    read_fd, write_fd = os.pipe2(getattr(os, "O_CLOEXEC", 0))

    def close_parent_writer():
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and supervisor.active_count == 0:
            time.sleep(0.005)
        os.close(write_fd)

    closer = threading.Thread(target=close_parent_writer)
    closer.start()
    try:
        result = supervisor.run(
            [sys.executable, "-c",
             "import os,sys; r,w=map(int,sys.argv[1:]); os.close(w); print(os.read(r,1))",
             str(read_fd), str(write_fd)],
            capture_output=True, timeout_s=2,
            pass_fds=(read_fd, write_fd), kind="probe")
    finally:
        try:
            os.close(read_fd)
        except OSError:
            pass
        closer.join(2)
    assert result.stdout == b"b''\n"
    supervisor.close()


def test_waitpid_echild_not_proc_snapshot_is_drain_authority(tmp_path):
    probe = r"""
import os, signal, subprocess, sys, time
from orchestrator import process_supervisor as ps
ps._set_subreaper()
code = "import os,time; p=os.fork(); " \
       "(os.close(1),os.close(2),time.sleep(30)) if p==0 else None"
proc = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
proc.wait()
assert proc.returncode == 0
assert ps._tree_empty(os.getpid(), proc) is False
children = ps._children(os.getpid())
assert children
for pid in children:
    os.kill(pid, signal.SIGKILL)
deadline = time.monotonic() + 2
while time.monotonic() < deadline and not ps._tree_empty(os.getpid(), proc):
    time.sleep(.01)
assert ps._tree_empty(os.getpid(), proc) is True
print('ECHILD-OK')
"""
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=5,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ECHILD-OK"


def test_abnormal_guardian_death_poison_rejects_next_run_and_close(tmp_path):
    supervisor = _supervisor(tmp_path)
    started = tmp_path / "started"
    errors = []

    def worker():
        try:
            supervisor.run(
                [sys.executable, "-c",
                 "import sys,time; open(sys.argv[1],'wb').close(); time.sleep(30)",
                 str(started)], capture_output=True, timeout_s=30, kind="probe")
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=worker)
    thread.start()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not started.exists():
        time.sleep(0.005)
    assert started.exists()
    receipt_path = next((tmp_path / "receipts").glob("execution-*.json"))
    running = json.loads(receipt_path.read_text())
    assert running["state"] == "running"
    with supervisor._guard:
        helper_pid = next(iter(supervisor._active.values())).helper.pid
    os.kill(helper_pid, signal.SIGKILL)
    thread.join(2)
    assert len(errors) == 1 and isinstance(errors[0], ExecutionSupervisorError)
    with pytest.raises(ExecutionSupervisorError, match="永久停机"):
        supervisor.run(
            [sys.executable, "-c", "pass"], capture_output=True,
            timeout_s=1, kind="probe")
    with pytest.raises(ExecutionSupervisorError, match="拒绝报告 close"):
        supervisor.close()
    # The deliberately killed guardian cannot clean its payload; test teardown
    # must remove the known group explicitly.
    try:
        os.killpg(int(running["initial_pgid"]), signal.SIGKILL)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and Path(f"/proc/{running['payload_pid']}").exists():
        time.sleep(0.01)


@pytest.mark.parametrize("raised", [KeyboardInterrupt, OSError])
def test_ambiguous_popen_result_poison_blocks_overlap(tmp_path, monkeypatch, raised):
    supervisor = _supervisor(tmp_path, grace=0.15)
    started = tmp_path / "ambiguous-started"
    late = tmp_path / "ambiguous-late"
    original_popen = PS.subprocess.Popen
    injected = {"done": False}

    def ambiguous_popen(args, *pargs, **kwargs):
        proc = original_popen(args, *pargs, **kwargs)
        if (not injected["done"] and isinstance(args, list)
                and "execution-guardian" in args):
            injected["done"] = True
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not started.exists():
                time.sleep(0.005)
            assert started.exists()
            raise raised("injected after real spawn")
        return proc

    monkeypatch.setattr(PS.subprocess, "Popen", ambiguous_popen)
    code = ("import signal,sys,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
            "open(sys.argv[1],'wb').close(); time.sleep(.7); "
            "open(sys.argv[2],'wb').close(); time.sleep(30)")
    with pytest.raises(raised):
        supervisor.run(
            [sys.executable, "-c", code, str(started), str(late)],
            capture_output=True, timeout_s=30, kind="probe")
    monkeypatch.setattr(PS.subprocess, "Popen", original_popen)
    with pytest.raises(ExecutionSupervisorError, match="永久停机"):
        supervisor.run(
            [sys.executable, "-c", "pass"], capture_output=True,
            timeout_s=1, kind="probe")
    with pytest.raises(ExecutionSupervisorError, match="拒绝报告 close"):
        supervisor.close()
    receipt_path = next((tmp_path / "receipts").glob("execution-*.json"))
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        receipt = json.loads(receipt_path.read_text())
        if receipt.get("state") == "terminal":
            break
        time.sleep(0.01)
    assert receipt["state"] == "terminal" and receipt["group_drained"] is True
    time.sleep(0.8)
    assert not late.exists()


def test_ambiguous_popen_and_sigint_preserve_original_signal_error(
        tmp_path, monkeypatch):
    supervisor = _supervisor(tmp_path)
    original_popen = PS.subprocess.Popen
    original_handler = signal.getsignal(signal.SIGINT)
    injected = {"done": False}

    def ambiguous_popen(args, *pargs, **kwargs):
        proc = original_popen(args, *pargs, **kwargs)
        if (not injected["done"] and isinstance(args, list)
                and "execution-guardian" in args):
            injected["done"] = True
            os.kill(os.getpid(), signal.SIGINT)
            raise OSError("spawn-result-lost")
        return proc

    signal.signal(signal.SIGINT, signal.default_int_handler)
    monkeypatch.setattr(PS.subprocess, "Popen", ambiguous_popen)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            supervisor.run(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                capture_output=True, timeout_s=30, kind="probe")
        assert isinstance(caught.value.__cause__, OSError)
        assert str(caught.value.__cause__) == "spawn-result-lost"
        assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
    finally:
        signal.signal(signal.SIGINT, original_handler)
        monkeypatch.setattr(PS.subprocess, "Popen", original_popen)
    with pytest.raises(ExecutionSupervisorError, match="永久停机"):
        supervisor.run(
            [sys.executable, "-c", "pass"], capture_output=True,
            timeout_s=1, kind="probe")
    with pytest.raises(ExecutionSupervisorError, match="拒绝报告 close"):
        supervisor.close()
    receipt_path = next((tmp_path / "receipts").glob("execution-*.json"))
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        receipt = json.loads(receipt_path.read_text())
        if receipt.get("state") == "terminal":
            break
        time.sleep(0.01)
    assert receipt["state"] == "terminal"
    assert receipt["group_drained"] is True


def test_instance_fence_never_reaches_target(tmp_path):
    work = tmp_path / "work"
    lease = InstanceLease.acquire(work, heartbeat_interval_s=0.05)
    lock_info = os.stat(work / ".orchestrator-instance.lock")
    supervisor = ExecutionSupervisor(
        receipt_dir=work / "state" / "executions",
        owner_id=lease.owner_id, owner_guard=lease.assert_owned,
        fence_context_factory=lease.delegate_owner_fence,
        term_grace_s=0.08)
    try:
        result = supervisor.run(
            [sys.executable, "-c", r"""
import os, sys
needle = (int(sys.argv[1]), int(sys.argv[2]))
found = []
for name in os.listdir('/proc/self/fd'):
    try:
        st = os.fstat(int(name))
    except OSError:
        continue
    if (st.st_dev, st.st_ino) == needle:
        found.append(name)
print(','.join(found))
""", str(lock_info.st_dev), str(lock_info.st_ino)],
            capture_output=True, timeout_s=2, kind="probe")
        assert result.stdout == b"\n"
    finally:
        supervisor.close()
        assert lease.close() is None


def test_owner_sigkill_guardian_holds_flock_until_tree_and_receipt_drain(tmp_path):
    work = tmp_path / "work"
    started = tmp_path / "started"
    late = tmp_path / "late"
    owner_script = r"""
import sys
from pathlib import Path
from orchestrator.instance_lease import InstanceLease
from orchestrator.process_supervisor import ExecutionSupervisor
work, started, late = map(Path, sys.argv[1:])
lease = InstanceLease.acquire(work, heartbeat_interval_s=.05)
supervisor = ExecutionSupervisor(
    receipt_dir=work/'state'/'executions', owner_id=lease.owner_id,
    owner_guard=lease.assert_owned,
    fence_context_factory=lease.delegate_owner_fence, term_grace_s=.35)
code = "import signal,sys,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); " \
       "open(sys.argv[1],'wb').close(); time.sleep(.8); " \
       "open(sys.argv[2],'wb').close(); time.sleep(30)"
supervisor.run([sys.executable, '-c', code, str(started), str(late)],
               capture_output=True, timeout_s=30, kind='probe')
"""
    owner = subprocess.Popen(
        [sys.executable, "-c", owner_script, str(work), str(started), str(late)],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not started.exists():
        time.sleep(0.01)
    assert started.exists()
    receipt_path = next((work / "state" / "executions").glob("execution-*.json"))

    os.kill(owner.pid, signal.SIGKILL)
    owner.wait(timeout=2)
    saw_busy = False
    acquire_started = time.monotonic()
    while True:
        try:
            replacement = InstanceLease.acquire(work, heartbeat_interval_s=0.05)
        except InstanceBusyError:
            saw_busy = True
            time.sleep(0.015)
            continue
        break
    elapsed = time.monotonic() - acquire_started
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        assert saw_busy and elapsed >= 0.20
        assert receipt["outcome"] == "owner_lost"
        assert receipt["group_drained"] is True
        assert receipt["term_sent"] is True and receipt["kill_sent"] is True
    finally:
        assert replacement.close() is None
    time.sleep(0.9)
    assert not late.exists()


def test_two_guardians_hold_owner_fence_until_last_tree_drains(tmp_path):
    """同 owner 并发 execution：较快 guardian 收口后，慢树仍须继续挡 takeover。"""
    work = tmp_path / "work"
    fast_ready = tmp_path / "fast-ready"
    slow_ready = tmp_path / "slow-ready"
    fast_late = tmp_path / "fast-late"
    slow_late = tmp_path / "slow-late"
    owner_script = r"""
import signal, sys, threading, time
from pathlib import Path
from orchestrator.instance_lease import InstanceLease
from orchestrator.process_supervisor import ExecutionSupervisor
work, fast_ready, slow_ready, fast_late, slow_late = map(Path, sys.argv[1:])
lease = InstanceLease.acquire(work, heartbeat_interval_s=.05)
supervisor = ExecutionSupervisor(
    receipt_dir=work/'state'/'executions', owner_id=lease.owner_id,
    owner_guard=lease.assert_owned,
    fence_context_factory=lease.delegate_owner_fence, term_grace_s=1.0)
def launch(slot, ready, late, ignore_term):
    prefix = ("import signal,sys,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
              if ignore_term else "import sys,time; ")
    code = prefix + "open(sys.argv[1],'wb').close(); time.sleep(2); " \
                    "open(sys.argv[2],'wb').close(); time.sleep(30)"
    try:
        supervisor.run(
            [sys.executable, '-c', code, str(ready), str(late)],
            capture_output=True, timeout_s=30, kind='probe',
            operation_context={'slot': slot})
    except BaseException:
        pass
threads = [
    threading.Thread(target=launch, args=('fast', fast_ready, fast_late, False)),
    threading.Thread(target=launch, args=('slow', slow_ready, slow_late, True)),
]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()
"""
    owner = subprocess.Popen(
        [sys.executable, "-c", owner_script, str(work), str(fast_ready),
         str(slow_ready), str(fast_late), str(slow_late)],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline and not (fast_ready.exists() and slow_ready.exists()):
        time.sleep(0.01)
    assert fast_ready.exists() and slow_ready.exists()
    receipt_paths = list((work / "state" / "executions").glob("execution-*.json"))
    assert len(receipt_paths) == 2
    running = [json.loads(path.read_text()) for path in receipt_paths]
    assert {item["context"]["slot"] for item in running} == {"fast", "slow"}
    assert all(item["state"] == "running" for item in running)

    os.kill(owner.pid, signal.SIGKILL)
    owner.wait(timeout=2)
    fast_terminal = None
    slow_running = None
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        by_slot = {
            item["context"]["slot"]: item
            for item in (json.loads(path.read_text()) for path in receipt_paths)
        }
        if by_slot["fast"]["state"] == "terminal" and by_slot["slow"]["state"] == "running":
            fast_terminal, slow_running = by_slot["fast"], by_slot["slow"]
            break
        time.sleep(0.01)
    assert fast_terminal is not None and slow_running is not None
    with pytest.raises(InstanceBusyError):
        InstanceLease.acquire(work, heartbeat_interval_s=0.05)

    deadline = time.monotonic() + 4
    while True:
        try:
            replacement = InstanceLease.acquire(work, heartbeat_interval_s=0.05)
        except InstanceBusyError:
            if time.monotonic() >= deadline:
                pytest.fail("last guardian never released delegated owner fence")
            time.sleep(0.02)
            continue
        break
    try:
        terminal = {
            item["context"]["slot"]: item
            for item in (json.loads(path.read_text()) for path in receipt_paths)
        }
        assert all(item["state"] == "terminal" for item in terminal.values())
        assert all(item["outcome"] == "owner_lost" for item in terminal.values())
        assert all(item["group_drained"] is True for item in terminal.values())
        assert terminal["slow"]["kill_sent"] is True
    finally:
        assert replacement.close() is None
    assert not fast_late.exists() and not slow_late.exists()


def test_raw_fork_child_does_not_retain_owner_death_pipe_or_flock(tmp_path):
    """An unrelated fork child may live, but must not delay guardian cleanup/takeover."""
    work = tmp_path / "work"
    started = tmp_path / "started"
    fork_pid_file = tmp_path / "fork-pid"
    owner_script = r"""
import os, sys
from pathlib import Path
from orchestrator.instance_lease import InstanceLease
from orchestrator.process_supervisor import ExecutionSupervisor
work, started, fork_pid_file = map(Path, sys.argv[1:])
lease = InstanceLease.acquire(work, heartbeat_interval_s=.05)
supervisor = ExecutionSupervisor(
    receipt_dir=work/'state'/'executions', owner_id=lease.owner_id,
    owner_guard=lease.assert_owned,
    fence_context_factory=lease.delegate_owner_fence, term_grace_s=.08)
pid = os.fork()
if pid == 0:
    import time
    time.sleep(1.2)
    os._exit(0)
fork_pid_file.write_text(str(pid))
code = "import sys,time; open(sys.argv[1],'wb').close(); time.sleep(30)"
supervisor.run([sys.executable, '-c', code, str(started)],
               capture_output=True, timeout_s=30, kind='probe')
"""
    owner = subprocess.Popen(
        [sys.executable, "-c", owner_script, str(work), str(started), str(fork_pid_file)],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not started.exists():
        time.sleep(0.01)
    assert started.exists() and fork_pid_file.exists()
    fork_pid = int(fork_pid_file.read_text())
    os.kill(owner.pid, signal.SIGKILL)
    owner.wait(timeout=2)
    t0 = time.monotonic()
    while True:
        try:
            replacement = InstanceLease.acquire(work, heartbeat_interval_s=0.05)
            break
        except InstanceBusyError:
            time.sleep(0.01)
    try:
        assert time.monotonic() - t0 < 0.8
        assert Path(f"/proc/{fork_pid}").exists()  # unrelated child still lives
    finally:
        assert replacement.close() is None
    try:
        os.waitpid(fork_pid, 0)
    except ChildProcessError:
        pass


def test_pdeath_signal_beats_inherited_pipe_writer_during_pending_spawn(
        tmp_path, monkeypatch):
    """PDEATHSIG remains authoritative if a pending-window fork keeps the pipe."""
    supervisor = _supervisor(tmp_path, grace=0.08)
    original_popen = PS.subprocess.Popen
    spawn_entered = threading.Event()
    release_spawn = threading.Event()

    def blocked_popen(args, *pargs, **kwargs):
        if isinstance(args, list) and "execution-guardian" in args:
            spawn_entered.set()
            assert release_spawn.wait(2)
        return original_popen(args, *pargs, **kwargs)

    monkeypatch.setattr(PS.subprocess, "Popen", blocked_popen)
    run_errors = []

    def run_workload():
        try:
            supervisor.run(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                capture_output=True, timeout_s=30, kind="probe")
        except BaseException as error:
            run_errors.append(error)

    worker = threading.Thread(target=run_workload)
    worker.start()
    assert spawn_entered.wait(2)

    fork_created = threading.Event()
    fork_pid = []

    def concurrent_fork():
        pid = os.fork()
        if pid == 0:
            time.sleep(1.2)
            os._exit(0)
        fork_pid.append(pid)
        fork_created.set()
        os.waitpid(pid, 0)

    fork_thread = threading.Thread(target=concurrent_fork)
    fork_thread.start()
    time.sleep(0.05)
    assert fork_created.is_set()  # child inherited the not-yet-registered writer
    release_spawn.set()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and supervisor.active_count == 0:
        time.sleep(0.005)
    assert supervisor.active_count == 1
    with supervisor._guard:
        helper_pid = next(iter(supervisor._active.values())).helper.pid
    receipt_path = next((tmp_path / "receipts").glob("execution-*.json"))
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if json.loads(receipt_path.read_text()).get("state") == "running":
            break
        time.sleep(0.005)
    else:
        pytest.fail("guardian never armed/published running")
    os.kill(helper_pid, signal.SIGUSR1)  # exact non-fatal PDEATHSIG event
    worker.join(0.5)
    assert not worker.is_alive()  # child still sleeps, but its inherited write end was closed
    assert len(run_errors) == 1 and isinstance(run_errors[0], ExecutionCleanupError)
    assert run_errors[0].receipt["outcome"] == "owner_lost"
    assert Path(f"/proc/{fork_pid[0]}").exists()
    fork_thread.join(2)
    assert not fork_thread.is_alive()
    supervisor.close()


def test_recovery_resolves_fenced_prepared_but_rejects_running_or_corrupt(tmp_path):
    receipt_dir = tmp_path / "receipts"
    receipt_dir.mkdir(mode=0o700)
    class _Fence:
        def __enter__(self):
            return -1

        def __exit__(self, *_args):
            return None

    seed = ExecutionSupervisor(
        receipt_dir=receipt_dir, owner_id="old",
        fence_context_factory=lambda: _Fence())
    old_id = "exec-" + "a" * 32
    prepared = seed._prepared_receipt(
        operation_id=old_id, kind="probe",
        spec_sha256="sha256:" + "b" * 64, timeout_s=10,
        operation_context={})
    prepared_path = receipt_dir / f"execution-{old_id}.json"
    atomic_write_receipt(prepared_path, prepared)

    supervisor = ExecutionSupervisor(
        receipt_dir=receipt_dir, owner_id="new",
        fence_context_factory=lambda: _Fence())
    supervisor.recover_previous_generation()
    assert json.loads(prepared_path.read_text())["outcome"] == "owner_lost_before_start"

    running_id = "exec-" + "c" * 32
    running_path = receipt_dir / f"execution-{running_id}.json"
    atomic_write_receipt(running_path, {
        **prepared, "operation_id": running_id, "state": "running",
        "helper_pid": 100, "payload_pid": 101, "initial_pgid": 101,
        "helper_start_ticks": "1", "payload_start_ticks": "2",
        "started_at_unix": time.time(), "deadline_at_unix": time.time() + 10,
        "heartbeat_ref": str(receipt_dir / f"heartbeat-{running_id}.json"),
        "guardian_heartbeat_seq": 0, "guardian_heartbeat_at_unix": time.time(),
        "last_activity_at_unix": time.time(), "activity_cpu_ticks": 0,
        "activity_output_bytes": 0, "activity_descendant_count": 0,
    })
    with pytest.raises(ExecutionRecoveryError, match="prior running"):
        ExecutionSupervisor(
            receipt_dir=receipt_dir, owner_id="next",
            fence_context_factory=lambda: _Fence()).recover_previous_generation()
    running_path.unlink()
    corrupt = receipt_dir / ("execution-exec-" + "d" * 32 + ".json")
    corrupt.write_text("{", encoding="utf-8")
    os.chmod(corrupt, 0o600)
    with pytest.raises(ExecutionRecoveryError, match="损坏"):
        ExecutionSupervisor(
            receipt_dir=receipt_dir, owner_id="next",
            fence_context_factory=lambda: _Fence()).recover_previous_generation()


def test_recovery_never_synthesizes_drain_for_unfenced_or_moved_receipt(tmp_path):
    class _Fence:
        def __enter__(self):
            return -1

        def __exit__(self, *_args):
            return None

    unfenced_dir = tmp_path / "unfenced"
    unfenced = ExecutionSupervisor.standalone(unfenced_dir)
    unfenced_id = "exec-" + "e" * 32
    unfenced_receipt = unfenced._prepared_receipt(
        operation_id=unfenced_id, kind="probe",
        spec_sha256="sha256:" + "f" * 64, timeout_s=10,
        operation_context={})
    atomic_write_receipt(
        unfenced_dir / f"execution-{unfenced_id}.json", unfenced_receipt)
    with pytest.raises(ExecutionRecoveryError, match="无 instance fence"):
        ExecutionSupervisor(
            receipt_dir=unfenced_dir, owner_id="leased",
            fence_context_factory=lambda: _Fence()).recover_previous_generation()

    moved_dir = tmp_path / "moved"
    seeded = ExecutionSupervisor(
        receipt_dir=moved_dir, owner_id="old",
        fence_context_factory=lambda: _Fence())
    moved_id = "exec-" + "1" * 32
    moved_receipt = seeded._prepared_receipt(
        operation_id=moved_id, kind="probe",
        spec_sha256="sha256:" + "2" * 64, timeout_s=10,
        operation_context={})
    path = moved_dir / f"execution-{moved_id}.json"
    atomic_write_receipt(path, moved_receipt)
    payload = path.read_bytes()
    os.rename(moved_dir, tmp_path / "old-moved")
    moved_dir.mkdir(mode=0o700)
    replacement = moved_dir / path.name
    replacement.write_bytes(payload)
    os.chmod(replacement, 0o600)
    with pytest.raises(ExecutionRecoveryError, match="损坏"):
        ExecutionSupervisor(
            receipt_dir=moved_dir, owner_id="new",
            fence_context_factory=lambda: _Fence()).recover_previous_generation()
