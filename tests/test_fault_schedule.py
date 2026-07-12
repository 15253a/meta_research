"""CP11.4c.3c.2b.2 fixed linear fault sidecar tests."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

import pytest

from orchestrator import fault_schedule as FS


SCHEDULE_ID = "0123456789abcdef0123456789abcdef"
OWNER_HELPER = r"""
import sys
from pathlib import Path
from orchestrator.instance_lease import InstanceLease
from orchestrator.process_supervisor import ExecutionSupervisor

root = Path(sys.argv[1])
db_owner_id = int(sys.argv[2])
execution_kind = sys.argv[3]
lease = InstanceLease.acquire(root, heartbeat_interval_s=0.05)
supervisor = ExecutionSupervisor(
    receipt_dir=root / "state/executions", owner_id=lease.owner_id,
    owner_guard=lease.assert_owned,
    fence_context_factory=lease.delegate_owner_fence, term_grace_s=0.1)
try:
    supervisor.run(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        capture_output=True, timeout_s=60, kind=execution_kind,
        operation_context={"db_owner_kind": "run", "db_owner_id": db_owner_id})
finally:
    supervisor.close(timeout_s=5)
    error = lease.close()
    if error is not None:
        raise error
"""


def _schedule_value(work: Path, *, action: str = "kill_owner", timeout=10.0):
    return {
        "version": 1,
        "protocol": FS.SCHEDULE_PROTOCOL,
        "schedule_id": SCHEDULE_ID,
        "work_root": str(work),
        "event_timeout_s": timeout,
        "events": [{
            "event_id": "event1",
            "action": action,
            "execution_kind": "fault-test",
            "db_owner_kind": "run",
            "db_owner_id": 41,
        }],
    }


def _write_schedule(tmp_path: Path, work: Path, *, action="kill_owner", timeout=10.0):
    value = _schedule_value(work, action=action, timeout=timeout)
    path = tmp_path / f"{action}.json"
    path.write_bytes(FS._canonical(value))
    path.chmod(0o600)
    return path, value


def _start_owner(
        work: Path, *, db_owner_id: int = 41,
        execution_kind: str = "fault-test") -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", OWNER_HELPER, str(work),
         str(db_owner_id), execution_kind],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE, close_fds=True, start_new_session=True)


def _cleanup(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def test_validate_is_read_only_and_rejects_general_workflow_fields(tmp_path, capsys):
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    path, value = _write_schedule(tmp_path, work)

    schedule, raw = FS.load_schedule(path)
    assert schedule == value
    assert raw == FS._canonical(value)
    assert not (work / "state").exists()
    assert FS.main(["validate", "--schedule", str(path)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "valid" and output["event_count"] == 1
    assert not (work / "state").exists()

    value["argv"] = ["sh", "-c", "kill -9 1"]
    path.write_bytes(FS._canonical(value))
    with pytest.raises(FS.FaultScheduleError, match="顶层字段"):
        FS.load_schedule(path)

    duplicate = _schedule_value(work)
    duplicate["events"].append({**duplicate["events"][0], "event_id": "event2"})
    path.write_bytes(FS._canonical(duplicate))
    with pytest.raises(FS.FaultScheduleError, match="selector 不得重复"):
        FS.load_schedule(path)


def test_noncanonical_or_writable_schedule_is_rejected(tmp_path):
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    path, value = _write_schedule(tmp_path, work)
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(FS.FaultScheduleError, match="canonical"):
        FS.load_schedule(path)
    path.write_bytes(FS._canonical(value))
    path.chmod(0o622)
    with pytest.raises(FS.FaultScheduleError, match="owner/mode"):
        FS.load_schedule(path)


def test_real_owner_fault_uses_pidfd_and_observes_guardian_drain(tmp_path):
    work = tmp_path / "owner-work"
    work.mkdir(mode=0o700)
    path, _value = _write_schedule(tmp_path, work, action="kill_owner")
    owner = _start_owner(work)
    try:
        final = FS.run_fault_schedule(path)
        assert final["status"] == "complete"
        assert final["signal_exactly_once"] is False
        assert final["recovery_verified"] is False
        assert owner.wait(timeout=5) == -signal.SIGKILL

        root = work / "state/fault-schedules" / SCHEDULE_ID
        spent = json.loads((root / "events/event1.spent.json").read_text())
        applied = json.loads((root / "events/event1.applied.json").read_text())
        result = json.loads((root / "events/event1.result.json").read_text())
        assert spent["target"]["kind"] == "instance_owner"
        assert applied["send_result"] == "pidfd_kernel_accepted"
        assert result["status"] == "observed"
        assert result["evidence"]["kind"] == "pinned-owner-exited-guardian-drained"
        assert FS.verify_fault_schedule(path) == final

        spent_path = root / "events/event1.spent.json"
        spent["unexpected"] = True
        spent_path.chmod(0o600)
        spent_path.write_bytes(FS._canonical(spent))
        spent_path.chmod(0o400)
        with pytest.raises(FS.FaultScheduleError, match="spent receipt"):
            FS.verify_fault_schedule(path)
    finally:
        _cleanup(owner)


def test_real_payload_fault_leaves_owner_to_close_normally(tmp_path):
    work = tmp_path / "payload-work"
    work.mkdir(mode=0o700)
    path, _value = _write_schedule(
        tmp_path, work, action="kill_execution_payload")
    owner = _start_owner(work)
    try:
        final = FS.run_fault_schedule(path)
        assert final["status"] == "complete"
        assert owner.wait(timeout=5) == 0
        root = work / "state/fault-schedules" / SCHEDULE_ID
        result = json.loads((root / "events/event1.result.json").read_text())
        assert result["status"] == "observed"
        assert result["evidence"]["kind"] == "execution-terminal-sigkill-drained"
        assert FS.verify_fault_schedule(path) == final

        terminal = result["evidence"]["terminal_receipt"]
        terminal["outcome"] = "owner_lost"
        result["evidence"]["receipt_sha256"] = FS._hash_bytes(
            FS._canonical(terminal))
        result_path = root / "events/event1.result.json"
        result_path.chmod(0o600)
        result_path.write_bytes(FS._canonical(result))
        result_path.chmod(0o400)
        with pytest.raises(FS.FaultScheduleError, match="aftermath evidence"):
            FS.verify_fault_schedule(path)
    finally:
        _cleanup(owner)


def test_two_event_schedule_waits_for_external_owner_restart(tmp_path):
    work = tmp_path / "two-event-work"
    work.mkdir(mode=0o700)
    schedule = _schedule_value(work)
    schedule["events"].append({
        "event_id": "event2",
        "action": "kill_execution_payload",
        "execution_kind": "fault-test-next",
        "db_owner_kind": "run",
        "db_owner_id": 42,
    })
    path = tmp_path / "two-events.json"
    path.write_bytes(FS._canonical(schedule))
    path.chmod(0o600)

    first = _start_owner(work)
    second = {"proc": None, "error": None, "first_returncode": None}
    stop = threading.Event()

    def external_restart():
        try:
            second["first_returncode"] = first.wait(timeout=10)
            deadline = time.monotonic() + 10
            while not stop.is_set() and time.monotonic() < deadline:
                if FS._owner_authority(str(work)) is None:
                    second["proc"] = _start_owner(
                        work, db_owner_id=42,
                        execution_kind="fault-test-next")
                    return
                time.sleep(0.01)
            if not stop.is_set():
                raise RuntimeError("delegated owner fence did not release")
        except BaseException as error:
            second["error"] = error

    restart = threading.Thread(target=external_restart, daemon=True)
    restart.start()
    try:
        final = FS.run_fault_schedule(path)
        restart.join(timeout=10)
        assert not restart.is_alive()
        if second["error"] is not None:
            raise second["error"]
        assert final["status"] == "complete"
        assert second["first_returncode"] == -signal.SIGKILL
        assert second["proc"] is not None
        assert second["proc"].wait(timeout=5) == 0

        root = work / "state/fault-schedules" / SCHEDULE_ID
        first_result = json.loads(
            FS._event_path(root, "event1", "result").read_text())
        second_result = json.loads(
            FS._event_path(root, "event2", "result").read_text())
        assert first_result["evidence"]["kind"] == (
            "pinned-owner-exited-guardian-drained")
        assert second_result["evidence"]["kind"] == (
            "execution-terminal-sigkill-drained")
    finally:
        stop.set()
        _cleanup(first)
        restart.join(timeout=5)
        if second["proc"] is not None:
            _cleanup(second["proc"])


def test_spent_gap_is_inconclusive_and_never_replays_signal(tmp_path, monkeypatch):
    work = tmp_path / "gap-work"
    work.mkdir(mode=0o700)
    path, _schedule = _write_schedule(tmp_path, work)
    owner_proc = _start_owner(work)
    try:
        monkeypatch.setattr(
            FS, "_pidfd_sigkill",
            lambda _fd: (_ for _ in ()).throw(KeyboardInterrupt()))
        assert FS.main(["run", "--schedule", str(path)]) == 130

        root = work / "state/fault-schedules" / SCHEDULE_ID
        assert FS._event_path(root, "event1", "spent").exists()
        assert not FS._event_path(root, "event1", "applied").exists()
        assert not FS._event_path(root, "event1", "result").exists()
        assert not (root / "final.json").exists()
        assert owner_proc.poll() is None

        monkeypatch.setattr(
            FS, "_pidfd_sigkill",
            lambda _fd: (_ for _ in ()).throw(
                AssertionError("must not replay")))
        monkeypatch.setattr(
            FS, "_wait_target",
            lambda *_args: (_ for _ in ()).throw(
                AssertionError("must not retarget")))

        final = FS.run_fault_schedule(path)
        assert final["status"] == "inconclusive"
        assert owner_proc.poll() is None
        result = json.loads((root / "events/event1.result.json").read_text())
        assert result["status"] == "inconclusive"
        assert "not replayed" in result["reason"]
        assert not FS._event_path(root, "event1", "applied").exists()
    finally:
        _cleanup(owner_proc)


def test_applied_gap_resumes_aftermath_without_resending(tmp_path, monkeypatch):
    work = tmp_path / "applied-gap-work"
    work.mkdir(mode=0o700)
    path, _schedule = _write_schedule(
        tmp_path, work, action="kill_execution_payload")
    owner = _start_owner(work)
    real_wait = FS._wait_payload_terminal
    try:
        monkeypatch.setattr(
            FS, "_wait_payload_terminal",
            lambda *_args: (_ for _ in ()).throw(KeyboardInterrupt()))
        assert FS.main(["run", "--schedule", str(path)]) == 130

        root = work / "state/fault-schedules" / SCHEDULE_ID
        assert FS._event_path(root, "event1", "spent").exists()
        assert FS._event_path(root, "event1", "applied").exists()
        assert not FS._event_path(root, "event1", "result").exists()
        assert not (root / "final.json").exists()

        monkeypatch.setattr(FS, "_wait_payload_terminal", real_wait)
        monkeypatch.setattr(
            FS, "_pidfd_sigkill",
            lambda _fd: (_ for _ in ()).throw(
                AssertionError("must not resend")))
        final = FS.run_fault_schedule(path)
        assert final["status"] == "complete"
        assert owner.wait(timeout=5) == 0
        result = json.loads(
            FS._event_path(root, "event1", "result").read_text())
        assert "after restart" in result["reason"]
    finally:
        _cleanup(owner)


def test_visible_spent_publish_error_reloads_durable_chain(tmp_path, monkeypatch):
    work = tmp_path / "publish-gap-work"
    work.mkdir(mode=0o700)
    path, _schedule = _write_schedule(tmp_path, work)
    owner = _start_owner(work)
    real_publish = FS._publish
    raised = False

    def publish_then_error(target, value):
        nonlocal raised
        result = real_publish(target, value)
        if target.name == "event1.spent.json" and not raised:
            raised = True
            raise OSError("simulated post-publication failure")
        return result

    try:
        monkeypatch.setattr(FS, "_publish", publish_then_error)
        monkeypatch.setattr(
            FS, "_pidfd_sigkill",
            lambda _fd: (_ for _ in ()).throw(
                AssertionError("post-publish error must not signal")))
        final = FS.run_fault_schedule(path)
        assert final["status"] == "inconclusive"
        assert owner.poll() is None
        root = work / "state/fault-schedules" / SCHEDULE_ID
        result = json.loads(
            FS._event_path(root, "event1", "result").read_text())
        assert result["spent_sha256"].startswith("sha256:")
        assert result["applied_sha256"] is None
        assert FS.verify_fault_schedule(path) == final
    finally:
        _cleanup(owner)


def test_visible_applied_publish_error_observes_without_resending(
        tmp_path, monkeypatch):
    work = tmp_path / "applied-publish-gap-work"
    work.mkdir(mode=0o700)
    path, _schedule = _write_schedule(
        tmp_path, work, action="kill_execution_payload")
    owner = _start_owner(work)
    real_publish = FS._publish
    raised = False

    def publish_then_error(target, value):
        nonlocal raised
        result = real_publish(target, value)
        if target.name == "event1.applied.json" and not raised:
            raised = True
            raise OSError("simulated post-applied publication failure")
        return result

    try:
        monkeypatch.setattr(FS, "_publish", publish_then_error)
        final = FS.run_fault_schedule(path)
        assert final["status"] == "complete"
        assert owner.wait(timeout=5) == 0
        root = work / "state/fault-schedules" / SCHEDULE_ID
        result = json.loads(
            FS._event_path(root, "event1", "result").read_text())
        assert result["status"] == "observed"
        assert result["reason"] == "durable applied SIGKILL aftermath observed"
        assert FS.verify_fault_schedule(path) == final
    finally:
        _cleanup(owner)


def test_duplicate_execution_selector_fails_closed(tmp_path, monkeypatch):
    work = tmp_path / "duplicate-work"
    work.mkdir(mode=0o700)
    receipts = work / "state/executions"
    receipts.mkdir(parents=True)
    receipts.chmod(0o700)
    paths = [
        receipts / ("execution-exec-" + token * 32 + ".json")
        for token in ("a", "b")]
    for item in paths:
        item.write_bytes(b"{}\n")
    event = _schedule_value(work)["events"][0]
    receipt = {
        "kind": "fault-test", "state": "running",
        "context": {"db_owner_kind": "run", "db_owner_id": 41},
    }
    monkeypatch.setattr(
        FS, "_read_execution_json",
        lambda _path: (dict(receipt), FS._canonical(receipt)))
    monkeypatch.setattr(FS, "validate_execution_receipt", lambda *_args: None)
    monkeypatch.setattr(FS, "_owner_authority", lambda _root: None)
    with pytest.raises(FS.FaultScheduleError, match="全历史匹配多个"):
        FS._wait_target(_schedule_value(work, timeout=0.2), event)


def test_selector_is_rechecked_after_spent_before_signal(tmp_path, monkeypatch):
    work = tmp_path / "selector-race-work"
    work.mkdir(mode=0o700)
    path, _schedule = _write_schedule(tmp_path, work)
    owner = _start_owner(work)
    real_scan = FS._scan_execution_matches
    real_wait_target = FS._wait_target
    matched_scans = 0
    wait_complete = False

    def tracked_wait(schedule, event):
        nonlocal wait_complete
        result = real_wait_target(schedule, event)
        wait_complete = True
        return result

    def racing_scan(schedule, event):
        nonlocal matched_scans
        matches = real_scan(schedule, event)
        if wait_complete and matches:
            matched_scans += 1
            if matched_scans == 2:
                return [*matches, matches[0]]
        return matches

    try:
        monkeypatch.setattr(FS, "_wait_target", tracked_wait)
        monkeypatch.setattr(FS, "_scan_execution_matches", racing_scan)
        monkeypatch.setattr(
            FS, "_pidfd_sigkill",
            lambda _fd: (_ for _ in ()).throw(
                AssertionError("ambiguous selector must not signal")))
        final = FS.run_fault_schedule(path)
        assert final["status"] == "inconclusive"
        assert owner.poll() is None
        root = work / "state/fault-schedules" / SCHEDULE_ID
        assert FS._event_path(root, "event1", "spent").exists()
        assert not FS._event_path(root, "event1", "applied").exists()
    finally:
        _cleanup(owner)


def test_receipt_drift_after_pin_fails_before_spent(tmp_path, monkeypatch):
    work = tmp_path / "receipt-drift-work"
    work.mkdir(mode=0o700)
    path, _schedule = _write_schedule(tmp_path, work)
    owner = _start_owner(work)
    real_scan = FS._scan_execution_matches
    real_wait_target = FS._wait_target
    matched_scans = 0
    wait_complete = False

    def tracked_wait(schedule, event):
        nonlocal wait_complete
        result = real_wait_target(schedule, event)
        wait_complete = True
        return result

    def drifting_scan(schedule, event):
        nonlocal matched_scans
        matches = real_scan(schedule, event)
        if wait_complete and matches:
            matched_scans += 1
            if matched_scans == 1:
                path_value, receipt, _raw = matches[0]
                changed = {**receipt, "state": "terminal"}
                return [(path_value, changed, FS._canonical(changed))]
        return matches

    try:
        monkeypatch.setattr(FS, "_wait_target", tracked_wait)
        monkeypatch.setattr(FS, "_scan_execution_matches", drifting_scan)
        monkeypatch.setattr(
            FS, "_pidfd_sigkill",
            lambda _fd: (_ for _ in ()).throw(
                AssertionError("drifted receipt must not signal")))
        final = FS.run_fault_schedule(path)
        assert final["status"] == "failed"
        root = work / "state/fault-schedules" / SCHEDULE_ID
        assert not FS._event_path(root, "event1", "spent").exists()
        assert owner.poll() is None
    finally:
        _cleanup(owner)


def test_global_runner_lock_and_unknown_state_file_fail_closed(
        tmp_path, monkeypatch):
    work = tmp_path / "lock-work"
    work.mkdir(mode=0o700)
    path, schedule = _write_schedule(tmp_path, work)
    schedule, raw = FS.load_schedule(path)
    base, root = FS._prepare_state(schedule, raw)
    lock_fd = FS._runner_lock(base)
    try:
        with pytest.raises(FS.FaultScheduleError, match="已有 fault runner"):
            FS._runner_lock(base)
    finally:
        os.close(lock_fd)

    unknown = root / "events/unknown.json"
    unknown.write_bytes(b"{}\n")
    unknown.chmod(0o400)
    with pytest.raises(FS.FaultScheduleError, match="未知文件"):
        FS.verify_fault_schedule(path)
    monkeypatch.setattr(
        FS, "_wait_target",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("layout must be checked before target/signal")))
    with pytest.raises(FS.FaultScheduleError, match="未知文件"):
        FS.run_fault_schedule(path)


def test_verify_directory_before_schedule_publication_is_incomplete(tmp_path):
    work = tmp_path / "pre-publish-work"
    work.mkdir(mode=0o700)
    path, schedule = _write_schedule(tmp_path, work)
    _base, root = FS._prepare_state(schedule, FS._canonical(schedule))
    (root / "schedule.json").unlink()
    result = FS.verify_fault_schedule(path)
    assert result["status"] == "incomplete"
    assert "schedule missing" in result["reason"]

    (root / "final.json").write_bytes(b"{}\n")
    (root / "final.json").chmod(0o400)
    with pytest.raises(FS.FaultScheduleError, match="schedule 缺失"):
        FS.verify_fault_schedule(path)


def test_start_ticks_mismatch_and_zombie_are_rejected(tmp_path, monkeypatch):
    real_proc_identity = FS._proc_identity
    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(FS, "_pidfd_open", lambda _pid: read_fd)
    monkeypatch.setattr(FS, "_proc_identity", lambda _pid: ("S", "999"))
    owner = {
        "pid": os.getpid(), "process_start_ticks": "111",
        "boot_id": FS._boot_id(), "owner_id": "owner-" + "a" * 32,
    }
    receipt = {"payload_pid": os.getpid(), "payload_start_ticks": "111",
               "operation_id": "exec-" + "b" * 32}
    event = _schedule_value(tmp_path)["events"][0]
    with pytest.raises(FS.FaultScheduleError, match="start ticks"):
        FS._pin_target(event, owner, receipt)
    os.close(write_fd)
    with pytest.raises(OSError):
        os.fstat(read_fd)
    monkeypatch.setattr(FS, "_proc_identity", real_proc_identity)

    pid = os.fork()
    if pid == 0:
        os._exit(0)
    try:
        # waitid(WNOWAIT) makes the child a stable zombie without reaping it.
        os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT)
        with pytest.raises(FS.FaultScheduleError, match="zombie"):
            FS._proc_identity(pid)
    finally:
        os.waitpid(pid, 0)


def test_verify_absent_is_incomplete_and_cli_exit_two(tmp_path, capsys):
    work = tmp_path / "verify-work"
    work.mkdir(mode=0o700)
    path, _value = _write_schedule(tmp_path, work)
    result = FS.verify_fault_schedule(path)
    assert result["status"] == "incomplete"
    assert FS.main(["verify", "--schedule", str(path)]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "incomplete"
