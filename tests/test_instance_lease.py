"""CP11.3a · orchestrator 进程级 owner lease 与 spool cursor CAS。

这里故意使用真实子进程，而不是线程或 mock：``flock`` 的验收边界就是
两个独立 orchestrator 进程不能共享一个 work-root。PID / heartbeat 只是
诊断信息；是否能 takeover 只由内核锁是否可取得决定。
"""
from __future__ import annotations

import json
import errno
import fcntl
import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Callable, Tuple

import pytest

from orchestrator.console_spool import ConsoleSpool, UnsafeConsolePath
from orchestrator.attack_stages import AttackStages
from orchestrator.instance_lease import (
    InstanceBusyError,
    InstanceLease,
    InstanceLeaseError,
    read_instance_status,
)
from orchestrator.run import System, build_system


SYSTEM_ROOT = Path(__file__).resolve().parent.parent
LOCK_NAME = ".orchestrator-instance.lock"
HEARTBEAT_REL = Path("state/orchestrator_heartbeat.json")


_HOLDER = r"""
import json
import pathlib
import sys

from orchestrator.instance_lease import InstanceLease, read_instance_status

root = pathlib.Path(sys.argv[1])
ready = pathlib.Path(sys.argv[2])
interval = float(sys.argv[3])
lease = InstanceLease.acquire(root, heartbeat_interval_s=interval)
status = read_instance_status(root)
ready_tmp = ready.with_name(ready.name + ".tmp")
ready_tmp.write_text(json.dumps({"owner_id": status["owner_id"]}), encoding="utf-8")
ready_tmp.replace(ready)
sys.stdin.buffer.read(1)
lease.close()
"""

_FORK_HOLDER = r"""
import json
import os
import pathlib
import sys
import time

from orchestrator.instance_lease import InstanceLease

root = pathlib.Path(sys.argv[1])
ready = pathlib.Path(sys.argv[2])
lease = InstanceLease.acquire(root, heartbeat_interval_s=0.02)
child = os.fork()
if child == 0:
    time.sleep(30)
    os._exit(0)
ready.write_text(json.dumps({"child_pid": child}), encoding="utf-8")
sys.stdin.buffer.read(1)
lease.close()
"""


def _wait_until(predicate: Callable[[], bool], *, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition did not become true before deadline")


def _spawn_holder(work_root: Path, ready: Path, *, interval_s: float = 0.02
                  ) -> Tuple[subprocess.Popen, str]:
    env = os.environ.copy()
    old_path = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(SYSTEM_ROOT) + (os.pathsep + old_path if old_path else "")
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(work_root), str(ready), str(interval_s)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )

    def ready_or_failed() -> bool:
        if ready.is_file():
            return True
        if proc.poll() is not None:
            stdout, stderr = proc.communicate()
            raise AssertionError(
                f"lease holder exited early ({proc.returncode}): "
                f"stdout={stdout!r}, stderr={stderr!r}")
        return False

    _wait_until(ready_or_failed)
    owner_id = json.loads(ready.read_text(encoding="utf-8"))["owner_id"]
    assert isinstance(owner_id, str) and owner_id
    return proc, owner_id


def _stop_holder(proc: subprocess.Popen, *, kill: bool = False) -> None:
    if proc.poll() is not None:
        return
    if kill:
        proc.kill()
    else:
        assert proc.stdin is not None
        proc.stdin.write(b"x")
        proc.stdin.flush()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
        raise
    if not kill:
        assert proc.returncode == 0, proc.stderr.read().decode("utf-8", errors="replace")


def _read_heartbeat(work_root: Path) -> dict:
    value = json.loads((work_root / HEARTBEAT_REL).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_real_process_same_root_is_busy_but_different_root_can_run(tmp_path):
    shared = tmp_path / "shared"
    other = tmp_path / "other"
    proc, owner_id = _spawn_holder(shared, tmp_path / "holder.ready")
    try:
        started = time.monotonic()
        with pytest.raises(InstanceBusyError):
            InstanceLease.acquire(shared, heartbeat_interval_s=0.02)
        assert time.monotonic() - started < 1.0       # non-blocking startup refusal

        independent = InstanceLease.acquire(other, heartbeat_interval_s=0.02)
        try:
            status = read_instance_status(other)
            assert status["active"] is True
            assert status["owner_id"] != owner_id
        finally:
            independent.close()
    finally:
        _stop_holder(proc)

    # Graceful holder close releases the kernel lease; no pathname cleanup is
    # needed and the same stable lock entry remains reusable.
    resumed = InstanceLease.acquire(shared, heartbeat_interval_s=0.02)
    resumed.close()


def test_sigkill_releases_kernel_lease_and_new_owner_replaces_heartbeat(tmp_path):
    work = tmp_path / "work"
    proc, old_owner = _spawn_holder(work, tmp_path / "killed.ready")
    lock_path = work / LOCK_NAME
    old_inode = (lock_path.stat().st_dev, lock_path.stat().st_ino)
    assert read_instance_status(work)["owner_id"] == old_owner

    _stop_holder(proc, kill=True)                     # no finally/lease.close in holder
    replacement = InstanceLease.acquire(work, heartbeat_interval_s=0.02)
    try:
        new_status = read_instance_status(work)
        assert new_status["active"] is True
        assert new_status["owner_id"] != old_owner
        _wait_until(lambda: _read_heartbeat(work).get("owner_id") == new_status["owner_id"])
        assert (lock_path.stat().st_dev, lock_path.stat().st_ino) == old_inode
    finally:
        replacement.close()


def test_fork_child_cannot_unlock_parent_and_does_not_block_parent_death_takeover(tmp_path):
    work = tmp_path / "forked"
    ready = tmp_path / "forked.ready"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SYSTEM_ROOT) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    proc = subprocess.Popen(
        [sys.executable, "-c", _FORK_HOLDER, str(work), str(ready)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    child_pid = None
    try:
        _wait_until(ready.is_file)
        child_pid = int(json.loads(ready.read_text(encoding="utf-8"))["child_pid"])
        assert child_pid > 0
        with pytest.raises(InstanceBusyError):
            InstanceLease.acquire(work, heartbeat_interval_s=0.02)

        proc.kill()
        proc.wait(timeout=5)
        # The fork child is intentionally still alive.  Its at-fork detach must
        # have closed the inherited OFD, so parent death releases authority.
        os.kill(child_pid, 0)
        replacement = InstanceLease.acquire(work, heartbeat_interval_s=0.02)
        assert replacement.close() is None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        if child_pid is not None:
            with suppress(ProcessLookupError):
                os.kill(child_pid, signal.SIGKILL)


def test_fork_child_close_and_assert_are_detached_from_parent_owner(tmp_path):
    lease = InstanceLease.acquire(tmp_path / "direct-fork", heartbeat_interval_s=0.02)
    pid = os.fork()
    if pid == 0:
        try:
            try:
                lease.assert_owned()
            except InstanceLeaseError:
                pass
            else:
                os._exit(2)
            os._exit(0 if lease.close() is None else 3)
        except BaseException:
            os._exit(4)
    _, status = os.waitpid(pid, 0)
    try:
        assert os.waitstatus_to_exitcode(status) == 0
        lease.assert_owned()
        with pytest.raises(InstanceBusyError):
            InstanceLease.acquire(tmp_path / "direct-fork", heartbeat_interval_s=0.02)
    finally:
        assert lease.close() is None


def test_relative_work_root_identity_survives_chdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    lease = InstanceLease.acquire("relative-work", heartbeat_interval_s=0.02)
    monkeypatch.chdir("/")
    lease.assert_owned()
    assert lease.close() is None


def test_pid_metadata_never_overrides_flock_authority(tmp_path):
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    lock_path = work / LOCK_NAME

    # A free lock with stale metadata naming a real live PID is recoverable.
    lock_path.write_text(json.dumps({"pid": os.getpid(), "owner_id": "stale"}),
                         encoding="utf-8")
    lock_path.chmod(0o600)
    recovered = InstanceLease.acquire(work, heartbeat_interval_s=0.02)
    recovered.close()

    # Conversely, a held lock remains busy even if its diagnostic body claims
    # an impossible/dead PID.  Advisory metadata must never authorize stealing.
    proc, _owner = _spawn_holder(work, tmp_path / "live.ready")
    try:
        lock_path.write_text(
            json.dumps({"pid": 2 ** 31 - 1, "owner_id": "looks-dead"}),
            encoding="utf-8")
        lock_path.chmod(0o600)
        with pytest.raises(InstanceBusyError):
            InstanceLease.acquire(work, heartbeat_interval_s=0.02)
    finally:
        _stop_holder(proc, kill=True)


@pytest.mark.parametrize("entry_kind", ["symlink", "fifo", "hardlink"])
def test_unsafe_lock_entry_is_rejected_without_following_or_blocking(tmp_path, entry_kind):
    work = tmp_path / entry_kind
    work.mkdir(mode=0o700)
    lock_path = work / LOCK_NAME
    target = work / "target"
    if entry_kind == "symlink":
        target.write_text("do not follow", encoding="utf-8")
        lock_path.symlink_to(target.name)
    elif entry_kind == "fifo":
        os.mkfifo(lock_path, 0o600)
    else:
        target.write_text("two names", encoding="utf-8")
        target.chmod(0o600)
        os.link(target, lock_path)

    started = time.monotonic()
    with pytest.raises(InstanceLeaseError):
        InstanceLease.acquire(work, heartbeat_interval_s=0.02)
    assert time.monotonic() - started < 1.0            # FIFO open must use O_NONBLOCK


@pytest.mark.parametrize("replace", ["lock", "work_root"])
def test_assert_owned_fails_if_stable_path_is_replaced(tmp_path, replace):
    work = tmp_path / "work"
    lease = InstanceLease.acquire(work, heartbeat_interval_s=60.0)
    try:
        if replace == "lock":
            lock_path = work / LOCK_NAME
            lock_path.unlink()
            lock_path.write_text("replacement", encoding="utf-8")
            lock_path.chmod(0o600)
        else:
            parked = tmp_path / "parked"
            work.rename(parked)
            work.mkdir(mode=0o700)

        with pytest.raises(InstanceLeaseError):
            lease.assert_owned()
    finally:
        # Once external pathname tampering is detected, close still has to stop
        # its heartbeat worker and release the descriptor it actually owns.
        with suppress(InstanceLeaseError):
            lease.close()


def test_heartbeat_is_atomic_continuous_and_stateful(tmp_path):
    work = tmp_path / "work"
    lease = InstanceLease.acquire(work, heartbeat_interval_s=0.02)
    try:
        lease.set_state("running")
        _wait_until(lambda: _read_heartbeat(work).get("state") == "running")

        sequences = []
        deadline = time.monotonic() + 0.15
        while time.monotonic() < deadline:
            # Reading the pathname concurrently with repeated replace must
            # always yield one complete generation, never partial JSON.
            heartbeat = _read_heartbeat(work)
            assert heartbeat["owner_id"] == read_instance_status(work)["owner_id"]
            assert heartbeat["state"] == "running"
            assert isinstance(heartbeat["sequence"], int)
            sequences.append(heartbeat["sequence"])
            time.sleep(0.002)
        assert max(sequences) > min(sequences)

        status = read_instance_status(work)
        assert status["active"] is True
        assert status["state"] == "running"
        assert status["sequence"] >= max(sequences)
    finally:
        lease.close()


def test_configured_heartbeat_deadline_prevents_false_stale_owner(tmp_path):
    work = tmp_path / "cadence"
    lease = InstanceLease.acquire(work, heartbeat_interval_s=3.0)
    try:
        heartbeat = _read_heartbeat(work)
        heartbeat["updated_at_unix"] -= 6.0
        heartbeat["updated_monotonic_s"] -= 6.0
        path = work / HEARTBEAT_REL
        path.write_text(json.dumps(heartbeat), encoding="utf-8")
        path.chmod(0o600)
        status = read_instance_status(work)
        assert status["active"] is True
        assert status["heartbeat_age_s"] >= 5.0
        assert heartbeat["heartbeat_deadline_s"] >= 9.0
    finally:
        assert lease.close() is None


def test_status_retries_transient_observer_flock_before_projecting_owner(
        tmp_path, monkeypatch):
    import orchestrator.instance_lease as lease_module

    work = tmp_path / "status-observer-race"
    lease = InstanceLease.acquire(work, heartbeat_interval_s=0.02)
    assert lease.close() is None
    real_flock = lease_module.fcntl.flock
    transient = [0]

    def observer_race(fd, operation):
        if operation == (lease_module.fcntl.LOCK_SH | lease_module.fcntl.LOCK_NB):
            if transient[0] < 2:
                transient[0] += 1
                raise BlockingIOError(errno.EAGAIN, "injected observer probe")
        return real_flock(fd, operation)

    monkeypatch.setattr(lease_module.fcntl, "flock", observer_race)
    status = read_instance_status(work)
    assert transient == [2]
    assert status["lock_held"] is False
    assert status["active"] is False
    assert status["status"] == "inactive"


def test_concurrent_observer_lock_cannot_project_fresh_crash_residue_active(tmp_path):
    work = tmp_path / "status-shared-observers"
    lease = InstanceLease.acquire(work, heartbeat_interval_s=0.02)
    assert lease.close() is None

    # Simulate a fresh running sidecar left by a crash while kernel authority
    # is already free.  A long-lived observer probe must not make another
    # observer mistake this diagnostic residue for a real owner.
    heartbeat = _read_heartbeat(work)
    heartbeat["state"] = "running"
    heartbeat["updated_at_unix"] = time.time()
    heartbeat["updated_monotonic_s"] = time.monotonic()
    (work / HEARTBEAT_REL).write_text(
        json.dumps(heartbeat) + "\n", encoding="utf-8")
    (work / HEARTBEAT_REL).chmod(0o600)

    observer_fd = os.open(work / LOCK_NAME, os.O_RDONLY)
    try:
        fcntl.flock(observer_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        status = read_instance_status(work)
    finally:
        fcntl.flock(observer_fd, fcntl.LOCK_UN)
        os.close(observer_fd)
    assert status["lock_held"] is False
    assert status["active"] is False
    assert status["status"] == "inactive"


def test_invalid_owner_metadata_never_projects_active(tmp_path):
    work = tmp_path / "invalid-status"
    lease = InstanceLease.acquire(work, heartbeat_interval_s=60.0)
    try:
        (work / LOCK_NAME).write_text("{}\n", encoding="utf-8")
        (work / LOCK_NAME).chmod(0o600)
        status = read_instance_status(work)
        assert status["active"] is False
        assert status["status"] == "invalid"
    finally:
        assert lease.close() is None


def test_status_schema_overflow_and_malformed_heartbeat_fail_closed(tmp_path):
    work = tmp_path / "invalid-status-types"
    lease = InstanceLease.acquire(work, heartbeat_interval_s=60.0)
    try:
        owner = dict(lease.owner)
        owner["acquired_at_unix"] = int("9" * 4000)
        (work / LOCK_NAME).write_text(json.dumps(owner) + "\n", encoding="utf-8")
        (work / LOCK_NAME).chmod(0o600)
        status = read_instance_status(work)
        assert status["active"] is False and status["status"] == "invalid"

        (work / LOCK_NAME).write_text(json.dumps(lease.owner) + "\n", encoding="utf-8")
        heartbeat = _read_heartbeat(work)
        heartbeat.update({"version": True, "hostname": [], "boot_id": 123})
        (work / HEARTBEAT_REL).write_text(
            json.dumps(heartbeat) + "\n", encoding="utf-8")
        (work / HEARTBEAT_REL).chmod(0o600)
        status = read_instance_status(work)
        assert status["active"] is False and status["status"] == "invalid"
    finally:
        assert lease.close() is None


def test_new_claimant_invalidates_previous_generation_before_startup_work(
        tmp_path, monkeypatch):
    import orchestrator.instance_lease as lease_module

    work = tmp_path / "generation-claim"
    old = InstanceLease.acquire(work, heartbeat_interval_s=60.0)
    old.set_state("running")
    old_owner = dict(old.owner)
    old_heartbeat = _read_heartbeat(work)
    assert old.close() is None

    # Recreate the exact crash residue: free stable lock with a fresh-looking
    # old owner heartbeat.  The next claimant must invalidate it immediately
    # after flock, before any state-directory/startup work can block.
    (work / LOCK_NAME).write_text(json.dumps(old_owner) + "\n", encoding="utf-8")
    (work / LOCK_NAME).chmod(0o600)
    old_heartbeat["state"] = "running"
    old_heartbeat["updated_at_unix"] = time.time()
    old_heartbeat["updated_monotonic_s"] = time.monotonic()
    (work / HEARTBEAT_REL).write_text(
        json.dumps(old_heartbeat) + "\n", encoding="utf-8")
    (work / HEARTBEAT_REL).chmod(0o600)

    entered = threading.Event()
    release = threading.Event()
    real_ensure_state = lease_module._ensure_private_state

    def blocked_ensure_state(work_fd):
        entered.set()
        assert release.wait(3.0)
        return real_ensure_state(work_fd)

    monkeypatch.setattr(lease_module, "_ensure_private_state", blocked_ensure_state)
    outcome = []

    def acquire_replacement() -> None:
        try:
            outcome.append(InstanceLease.acquire(work, heartbeat_interval_s=0.02))
        except BaseException as error:
            outcome.append(error)

    thread = threading.Thread(target=acquire_replacement)
    thread.start()
    assert entered.wait(3.0)
    status = read_instance_status(work)
    assert status["lock_held"] is True
    assert status["active"] is False
    assert status["status"] == "invalid"
    release.set()
    thread.join(3.0)
    assert len(outcome) == 1 and isinstance(outcome[0], InstanceLease)
    assert outcome[0].close() is None


def test_close_serializes_against_concurrent_state_transition(tmp_path, monkeypatch):
    work = tmp_path / "close-race"
    lease = InstanceLease.acquire(work, heartbeat_interval_s=60.0)
    entered = threading.Event()
    release = threading.Event()
    original_write = lease._write_heartbeat

    def blocked_final_write() -> None:
        if lease._desired_state == "stopped":
            entered.set()
            assert release.wait(3.0)
        original_write()

    monkeypatch.setattr(lease, "_write_heartbeat", blocked_final_write)
    close_result = []
    transition_result = []
    closer = threading.Thread(target=lambda: close_result.append(lease.close()))
    closer.start()
    assert entered.wait(3.0)

    def transition() -> None:
        try:
            lease.set_state("running")
        except BaseException as error:
            transition_result.append(error)

    updater = threading.Thread(target=transition)
    updater.start()
    time.sleep(0.03)
    assert updater.is_alive()                       # serialized behind close_guard
    release.set()
    closer.join(3.0)
    updater.join(3.0)
    assert close_result == [None]
    assert len(transition_result) == 1
    assert isinstance(transition_result[0], InstanceLeaseError)
    assert _read_heartbeat(work)["state"] == "stopped"


def test_close_join_timeout_stays_fenced_until_close_retry(tmp_path):
    work = tmp_path / "join-timeout"
    lease = InstanceLease.acquire(work, heartbeat_interval_s=60.0)
    real_thread = lease._thread

    class JoinTimeout:
        alive = True

        @staticmethod
        def join(timeout=None):
            return None

        def is_alive(self):
            return self.alive

    fake = JoinTimeout()
    lease._thread = fake
    first = lease.close()
    assert isinstance(first, InstanceLeaseError)
    assert lease.closing is True and lease.closed is False
    with pytest.raises(InstanceLeaseError, match="关闭"):
        lease.set_state("running")
    with pytest.raises(InstanceBusyError):
        InstanceLease.acquire(work, heartbeat_interval_s=0.02)

    _wait_until(lambda: not real_thread.is_alive())
    fake.alive = False
    assert lease.close() is None
    replacement = InstanceLease.acquire(work, heartbeat_interval_s=0.02)
    assert replacement.close() is None


def test_close_stopped_heartbeat_failure_stays_fenced_until_retry(tmp_path, monkeypatch):
    work = tmp_path / "heartbeat-close-failure"
    lease = InstanceLease.acquire(work, heartbeat_interval_s=60.0)
    original_write = lease._write_heartbeat
    attempts = []

    def flaky_write() -> None:
        attempts.append("write")
        if len(attempts) == 1:
            raise OSError("injected stopped heartbeat failure")
        original_write()

    monkeypatch.setattr(lease, "_write_heartbeat", flaky_write)
    first = lease.close()
    assert isinstance(first, InstanceLeaseError)
    assert isinstance(first.__cause__, OSError)
    assert lease.closing is True and lease.closed is False
    with pytest.raises(InstanceBusyError):
        InstanceLease.acquire(work, heartbeat_interval_s=0.02)

    assert lease.close() is None
    assert attempts == ["write", "write"]
    replacement = InstanceLease.acquire(work, heartbeat_interval_s=0.02)
    assert replacement.close() is None


def test_close_wraps_thread_join_failure_and_keeps_flock(tmp_path):
    work = tmp_path / "join-failure"
    lease = InstanceLease.acquire(work, heartbeat_interval_s=60.0)
    real_thread = lease._thread

    class FailingJoin:
        @staticmethod
        def join(timeout=None):
            raise OSError("injected join failure")

        @staticmethod
        def is_alive():
            return False

    lease._thread = FailingJoin()
    first = lease.close()
    assert isinstance(first, InstanceLeaseError)
    assert isinstance(first.__cause__, OSError)
    assert lease.closed is False
    with pytest.raises(InstanceBusyError):
        InstanceLease.acquire(work, heartbeat_interval_s=0.02)

    lease._thread = real_thread
    assert lease.close() is None


def test_descriptor_release_survives_main_thread_interrupt_without_lock_leak(
        tmp_path, monkeypatch):
    work = tmp_path / "release-interrupt"
    lease = InstanceLease.acquire(work, heartbeat_interval_s=60.0)
    target_state_fd = lease._state_fd
    real_close = os.close
    real_join = threading.Thread.join
    delayed = [False]
    interrupted = [False]

    def delayed_close(fd):
        if fd == target_state_fd and not delayed[0]:
            delayed[0] = True
            time.sleep(0.05)
        return real_close(fd)

    def interrupted_join(thread, timeout=None):
        if thread.name == "orchestrator-owner-release" and not interrupted[0]:
            interrupted[0] = True
            real_join(thread, timeout)
            raise KeyboardInterrupt("injected release join interruption")
        return real_join(thread, timeout)

    monkeypatch.setattr(os, "close", delayed_close)
    monkeypatch.setattr(threading.Thread, "join", interrupted_join)
    close_error = lease.close()
    assert isinstance(close_error, InstanceLeaseError)
    assert delayed == [True] and interrupted == [True]
    assert lease.closed is True

    # The interruption is reported only after the release worker has closed
    # lock FD last; a replacement must never see a leaked flock.
    replacement = InstanceLease.acquire(work, heartbeat_interval_s=0.02)
    assert replacement.close() is None


def test_acquire_interrupt_after_heartbeat_start_stops_writer_before_unlock(
        tmp_path, monkeypatch):
    work = tmp_path / "acquire-start-interrupt"
    real_start = threading.Thread.start
    injected = [False]
    started_heartbeats = []

    def start_then_interrupt(thread):
        result = real_start(thread)
        if thread.name == "orchestrator-owner-heartbeat" and not injected[0]:
            started_heartbeats.append(thread)
            injected[0] = True
            raise KeyboardInterrupt("injected post-start interruption")
        return result

    monkeypatch.setattr(threading.Thread, "start", start_then_interrupt)
    with pytest.raises(KeyboardInterrupt, match="post-start"):
        InstanceLease.acquire(work, heartbeat_interval_s=0.02)
    assert injected == [True]
    assert len(started_heartbeats) == 1
    _wait_until(lambda: not started_heartbeats[0].is_alive())

    replacement = InstanceLease.acquire(work, heartbeat_interval_s=0.02)
    assert replacement.close() is None


def test_wrapped_acquire_error_exposes_incomplete_cleanup_handle(
        tmp_path, monkeypatch):
    work = tmp_path / "wrapped-acquire-cleanup"
    real_start = threading.Thread.start
    real_join = threading.Thread.join
    injected_start = [False]
    injected_join = [False]

    def start_then_fail(thread):
        result = real_start(thread)
        if thread.name == "orchestrator-owner-heartbeat" and not injected_start[0]:
            injected_start[0] = True
            raise OSError("injected post-start failure")
        return result

    def fail_heartbeat_join(thread, timeout=None):
        if thread.name == "orchestrator-owner-heartbeat" and not injected_join[0]:
            injected_join[0] = True
            raise OSError("injected rollback join failure")
        return real_join(thread, timeout)

    monkeypatch.setattr(threading.Thread, "start", start_then_fail)
    monkeypatch.setattr(threading.Thread, "join", fail_heartbeat_join)
    with pytest.raises(InstanceLeaseError, match="无法打开") as caught:
        InstanceLease.acquire(work, heartbeat_interval_s=0.02)
    cleanup = caught.value.orchestrator_cleanup
    assert cleanup.closed is False
    with pytest.raises(InstanceBusyError):
        InstanceLease.acquire(work, heartbeat_interval_s=0.02)

    monkeypatch.setattr(threading.Thread, "join", real_join)
    assert cleanup.close() is None
    replacement = InstanceLease.acquire(work, heartbeat_interval_s=0.02)
    assert replacement.close() is None


def test_close_is_idempotent_and_only_then_allows_takeover(tmp_path):
    work = tmp_path / "work"
    lease = InstanceLease.acquire(work, heartbeat_interval_s=0.02)
    with pytest.raises(InstanceBusyError):
        InstanceLease.acquire(work, heartbeat_interval_s=0.02)

    lease.close()
    lease.close()
    with pytest.raises(InstanceLeaseError):
        lease.assert_owned()
    assert read_instance_status(work)["active"] is False

    next_lease = InstanceLease.acquire(work, heartbeat_interval_s=0.02)
    next_lease.close()


def test_build_system_owns_lease_by_default_and_close_releases_it(tmp_path):
    work = tmp_path / "run"
    system = build_system(str(SYSTEM_ROOT), str(work), attack=False, outbound_config=None)
    try:
        with pytest.raises(InstanceBusyError):
            build_system(str(SYSTEM_ROOT), str(work), attack=False, outbound_config=None)
        assert read_instance_status(work)["active"] is True
    finally:
        assert system.close() is None
        assert system.close() is None                   # public lifecycle is idempotent

    restarted = build_system(str(SYSTEM_ROOT), str(work), attack=False, outbound_config=None)
    assert restarted.close() is None


def test_production_build_rejects_foreign_preassembled_attack_capability(tmp_path):
    foreign_attack = object.__new__(AttackStages)
    work = tmp_path / "foreign-attack"
    with pytest.raises(ValueError, match="拒绝注入现成 AttackStages"):
        build_system(
            str(SYSTEM_ROOT), str(work), attack=foreign_attack,
            outbound_config=None)
    assert not work.exists()


def test_system_close_keeps_lease_until_failed_resource_cleanup_is_retried(tmp_path):
    work = tmp_path / "run"
    lease = InstanceLease.acquire(work, heartbeat_interval_s=0.02)
    attempts = []

    def flaky_close() -> None:
        attempts.append("close")
        if len(attempts) == 1:
            raise OSError("injected close failure")

    class IdleAdvancer:
        last_stop_reason = None

    system = System(
        advancer=IdleAdvancer(), state=None, daemon=None, dual_mode="A",
        work_root=work, instance_lease=lease, resource_closers=[flaky_close])
    first_error = system.close()
    assert isinstance(first_error, OSError)
    assert attempts == ["close"]
    with pytest.raises(RuntimeError, match="shutdown"):
        system.flush_outbound()
    with pytest.raises(InstanceBusyError):
        InstanceLease.acquire(work, heartbeat_interval_s=0.02)

    assert system.close() is None
    assert attempts == ["close", "close"]
    assert system.close() is None
    replacement = InstanceLease.acquire(work, heartbeat_interval_s=0.02)
    replacement.close()


def test_build_failure_after_lease_acquisition_releases_owner(tmp_path, monkeypatch):
    work = tmp_path / "failed-build"
    observed_active = []

    def fail_connect(_path):
        observed_active.append(read_instance_status(work)["active"])
        raise OSError("injected DB open failure")

    monkeypatch.setattr("orchestrator.run._db.connect", fail_connect)
    with pytest.raises(OSError, match="DB open failure"):
        build_system(str(SYSTEM_ROOT), str(work), attack=False, outbound_config=None)
    assert observed_active == [True]                  # lease precedes shared DB initialization

    # Constructor rollback must not strand a process-global owner capability.
    replacement = InstanceLease.acquire(work, heartbeat_interval_s=0.02)
    replacement.close()


def test_assembly_cleanup_failure_retains_lease_and_exposes_retry_handle(
        tmp_path, monkeypatch):
    import orchestrator.run as run_module

    work = tmp_path / "failed-cleanup"
    attempts = []

    def fail_assembly(**kwargs):
        def flaky_close() -> None:
            attempts.append("close")
            if len(attempts) == 1:
                raise OSError("injected assembly closer failure")

        kwargs["resource_closers"].append(flaky_close)
        raise ValueError("injected assembly failure")

    monkeypatch.setattr(run_module, "_assemble_system", fail_assembly)
    with pytest.raises(ValueError, match="assembly failure") as caught:
        build_system(str(SYSTEM_ROOT), str(work), attack=False, outbound_config=None)
    cleanup = caught.value.orchestrator_cleanup
    assert cleanup.closed is False
    with pytest.raises(InstanceBusyError):
        InstanceLease.acquire(work, heartbeat_interval_s=0.02)

    assert cleanup.close() is None
    assert cleanup.closed is True
    replacement = InstanceLease.acquire(work, heartbeat_interval_s=0.02)
    replacement.close()


def test_run_state_failure_does_not_strand_depth_or_lease(tmp_path, monkeypatch):
    work = tmp_path / "run-state-failure"
    lease = InstanceLease.acquire(work, heartbeat_interval_s=0.02)

    class IdleAdvancer:
        last_stop_reason = None

        def run_cycles(self, _max_cycles):
            return []

    system = System(
        advancer=IdleAdvancer(), state=None, daemon=None, dual_mode="A",
        work_root=work, instance_lease=lease)
    original_set_state = lease.set_state

    def fail_running(state, **kwargs):
        if state == "running":
            raise OSError("injected heartbeat transition failure")
        return original_set_state(state, **kwargs)

    monkeypatch.setattr(lease, "set_state", fail_running)
    with pytest.raises(OSError, match="transition failure"):
        system.run(0)
    assert system._run_depth == 0
    assert system.close() is None


def test_concurrent_system_run_is_rejected_without_second_advancer_call(tmp_path):
    work = tmp_path / "concurrent-run"
    lease = InstanceLease.acquire(work, heartbeat_interval_s=0.02)
    entered = threading.Event()
    release = threading.Event()
    calls = []

    class BlockingAdvancer:
        last_stop_reason = None

        def run_cycles(self, _max_cycles):
            calls.append(threading.get_ident())
            entered.set()
            assert release.wait(3.0)
            return []

    system = System(
        advancer=BlockingAdvancer(), state=None, daemon=None, dual_mode="A",
        work_root=work, instance_lease=lease)
    outcome = []

    def first_run() -> None:
        try:
            outcome.append(system.run(0))
        except BaseException as error:
            outcome.append(error)

    thread = threading.Thread(target=first_run)
    thread.start()
    assert entered.wait(3.0)
    try:
        with pytest.raises(RuntimeError, match="并发 run"):
            system.run(0)
        assert len(calls) == 1
    finally:
        release.set()
        thread.join(3.0)
        system.close()
    assert outcome == [[]]


def test_close_cannot_release_lease_during_public_outbound_operation(tmp_path):
    work = tmp_path / "flush-race"
    lease = InstanceLease.acquire(work, heartbeat_interval_s=0.02)
    entered = threading.Event()
    release = threading.Event()

    class Delivery:
        def tick(self):
            entered.set()
            assert release.wait(3.0)
            return []

        @staticmethod
        def pending_status():
            return {"pending": 0, "retrying": 0, "urgent_pending": 0, "channels": {}}

        @staticmethod
        def worker_running():
            return False

        @staticmethod
        def stop():
            return None

    class IdleAdvancer:
        last_stop_reason = None

    system = System(
        advancer=IdleAdvancer(), state=None, daemon=None, dual_mode="A",
        work_root=work, instance_lease=lease, outbound_delivery=Delivery())
    outcome = []
    thread = threading.Thread(target=lambda: outcome.append(system.flush_outbound()))
    thread.start()
    assert entered.wait(3.0)
    close_error = system.close()
    assert isinstance(close_error, RuntimeError)
    assert lease.closed is False
    release.set()
    thread.join(3.0)
    assert outcome and outcome[0]["pending"] == 0
    assert system.close() is None


def test_close_cannot_release_lease_during_public_callback_operation(tmp_path):
    work = tmp_path / "callback-race"
    lease = InstanceLease.acquire(work, heartbeat_interval_s=0.02)
    entered = threading.Event()
    release = threading.Event()

    def blocked_notification() -> None:
        entered.set()
        assert release.wait(3.0)

    class IdleAdvancer:
        last_stop_reason = None

    system = System(
        advancer=IdleAdvancer(), state=None, daemon=None, dual_mode="A",
        work_root=work, instance_lease=lease,
        sync_notifications=blocked_notification)
    thread = threading.Thread(target=system.sync_notifications)
    thread.start()
    assert entered.wait(3.0)
    error = system.close()
    assert isinstance(error, RuntimeError)
    assert lease.closed is False
    release.set()
    thread.join(3.0)
    assert system.close() is None


def test_close_retains_lease_while_accepted_query_is_inflight(tmp_path):
    work = tmp_path / "accepted-query"
    lease = InstanceLease.acquire(work, heartbeat_interval_s=0.02)
    pending = [True]

    class IdleAdvancer:
        last_stop_reason = None

    system = System(
        advancer=IdleAdvancer(), state=None, daemon=None, dual_mode="A",
        work_root=work, instance_lease=lease,
        accepted_interaction_pending=lambda: pending[0])
    error = system.close()
    assert isinstance(error, RuntimeError)
    assert lease.closed is False
    pending[0] = False
    assert system.close() is None


def test_close_retry_privately_converges_accepted_query_after_shutdown(tmp_path):
    work = tmp_path / "accepted-query-retry"
    lease = InstanceLease.acquire(work, heartbeat_interval_s=0.02)
    pending = [True]
    polls = []

    class IdleAdvancer:
        last_stop_reason = None

    def poll_accepted() -> None:
        polls.append("poll")
        if len(polls) == 2:
            pending[0] = False

    system = System(
        advancer=IdleAdvancer(), state=None, daemon=None, dual_mode="A",
        work_root=work, instance_lease=lease,
        sync_accepted_interactions=poll_accepted,
        accepted_interaction_pending=lambda: pending[0])
    first = system.close()
    assert isinstance(first, RuntimeError)
    assert polls == ["poll"]
    with pytest.raises(RuntimeError, match="shutdown"):
        system.accepted_interaction_pending()

    assert system.close() is None
    assert polls == ["poll", "poll"]
    replacement = InstanceLease.acquire(work, heartbeat_interval_s=0.02)
    assert replacement.close() is None


def test_stale_spool_batch_cannot_move_durable_cursor_backwards(tmp_path):
    spool = ConsoleSpool(tmp_path)
    for nonce, text in (("1" * 32, "first"), ("2" * 32, "second")):
        spool.append({
            "connector": "console",
            "raw_text": text,
            "idempotency_key": f"console-{nonce}",
        })

    current = spool.read_pending()
    stale = spool.read_pending()
    assert current.start_offset == stale.start_offset == 0
    assert len(current.records) == len(stale.records) == 2

    spool.write_cursor(current, current.records[-1].end_offset)
    with pytest.raises(UnsafeConsolePath, match="cursor|\u6e38\u6807|\u65e7 batch"):
        spool.write_cursor(stale, stale.records[0].end_offset)
    assert spool.read_pending().records == ()
