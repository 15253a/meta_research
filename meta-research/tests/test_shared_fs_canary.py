"""CP11.4c.3c.2b.1 one-shot shared-filesystem canary.

The combined test intentionally uses two real processes and a real SIGKILL,
guardian, rollback/WAL recovery, lease takeover and FD path replacement.  It is
still one boot, so its final receipt must remain a local prerequisite.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time

import pytest

from orchestrator import shared_fs_canary as SFC
from orchestrator.instance_lease import InstanceBusyError, InstanceLease


RUN_ID = "0123456789abcdef0123456789abcdef"


@pytest.fixture(scope="module")
def local_result(tmp_path_factory):
    root = tmp_path_factory.mktemp("shared-fs-canary") / "run"
    result = SFC.run_local_canary(
        canary_root=root, run_id=RUN_ID,
        timeout_s=15.0, guardian_grace_s=0.1)
    return root, result


def test_local_canary_runs_real_mechanics_without_claiming_two_nodes(local_result):
    root, result = local_result
    assert result["status"] == "passed"
    assert result["verified_scope"] == SFC.LOCAL_SCOPE
    assert result["local_checks_passed"] is True
    assert result["observed_node_count"] == 1
    assert result["two_node_verified"] is False
    assert result["shared_fs_ready"] is False
    assert result["infrastructure_fence_verified"] is False
    for role in ("holder", "contender"):
        terminal = json.loads((
            root / f"state/shared-fs-canary/phases/{role}_complete.json"
        ).read_text())
        assert terminal["evidence"]["cleanup_complete"] is True

    complete = json.loads((
        root / "state/shared-fs-canary/phases/contender_complete.json").read_text())
    evidence = complete["evidence"]
    assert evidence["busy_after_kill"] >= 1
    assert evidence["guardian_outcome"] == "owner_lost"
    assert evidence["guardian_group_drained"] is True
    assert evidence["committed_retained"] is True
    assert evidence["uncommitted_discarded"] is True
    assert evidence["journal_removed_after_recovery"] is True
    assert evidence["fd_original_bytes_retained"] is True
    assert evidence["fd_path_binding_rejected"] is True
    assert evidence["journal_mode"] == evidence["required_journal_mode"]

    crash = json.loads((
        root / "state/shared-fs-canary/phases/crash_ready.json").read_text())
    if crash["evidence"]["journal_mode"] == "delete":
        assert evidence["hot_rollback_recovered"] is True
        assert crash["evidence"]["hot_rollback_journal"] is True
        assert crash["evidence"]["journal_magic"] == "d9d505f920a163d7"
        assert (crash["evidence"]["baseline_db_sha256"]
                != crash["evidence"]["dirty_db_sha256"])
    else:
        assert crash["evidence"]["journal_mode"] == "wal"
        assert evidence["hot_rollback_recovered"] is False


def test_local_contract_cannot_be_upgraded_to_two_node(local_result, capsys):
    root, _result = local_result
    local = SFC.verify_canary(
        canary_root=root, run_id=RUN_ID, required_scope=SFC.LOCAL_SCOPE)
    assert local["status"] == "passed"
    assert local["two_node_verified"] is False
    with pytest.raises(SFC.SharedFSCanaryError, match="不得升级"):
        SFC.verify_canary(canary_root=root, run_id=RUN_ID)
    with pytest.raises(SFC.SharedFSCanaryError, match="scope/timing"):
        SFC.run_node_canary(
            canary_root=root, run_id=RUN_ID, role="holder",
            timeout_s=15.0, guardian_grace_s=1.0)

    code = SFC.main([
        "verify", "--canary-root", str(root), "--run-id", RUN_ID,
    ])
    captured = capsys.readouterr()
    output = json.loads(captured.err)
    assert code == 3 and captured.out == ""
    assert output["status"] == "unsafe"


def test_missing_phases_are_incomplete_and_not_published_as_final(tmp_path):
    root = tmp_path / "incomplete"
    SFC._create_contract(
        root=root, run_id=RUN_ID, scope=SFC.LOCAL_SCOPE,
        timeout_s=5.0, guardian_grace_s=0.1)
    result = SFC.verify_canary(
        canary_root=root, run_id=RUN_ID,
        required_scope=SFC.LOCAL_SCOPE)
    assert result["status"] == "incomplete"
    assert "holder_lease" in result["missing"]
    assert not (root / "state/shared-fs-canary/final-local.json").exists()


def test_final_requires_both_roles_to_finish_cleanup(tmp_path):
    root = tmp_path / "cleanup-gate"
    SFC.run_local_canary(
        canary_root=root, run_id=RUN_ID,
        timeout_s=15.0, guardian_grace_s=0.1)
    (root / "state/shared-fs-canary/final-local.json").unlink()
    (root / "state/shared-fs-canary/phases/contender_complete.json").unlink()

    result = SFC.verify_canary(
        canary_root=root, run_id=RUN_ID,
        required_scope=SFC.LOCAL_SCOPE)
    assert result["status"] == "incomplete"
    assert result["shared_fs_ready"] is False
    assert "contender_complete" in result["missing"]


@pytest.mark.parametrize("bad_run_id", ["", "A" * 32, "0" * 31, "0" * 33, "../escape"])
def test_bad_run_id_is_rejected_before_root_creation(tmp_path, bad_run_id):
    root = tmp_path / "must-not-exist"
    with pytest.raises(SFC.SharedFSCanaryError, match="run_id"):
        SFC.run_local_canary(canary_root=root, run_id=bad_run_id)
    assert not root.exists()


def test_two_node_fence_observation_window_is_bounded_before_mutation(tmp_path):
    root = tmp_path / "too-short"
    with pytest.raises(SFC.SharedFSCanaryError, match="至少 1 秒"):
        SFC.run_node_canary(
            canary_root=root, run_id=RUN_ID, role="holder",
            timeout_s=5.0, guardian_grace_s=0.1)
    assert not root.exists()


def test_delete_probe_mechanically_forms_hot_rollback_journal(tmp_path):
    path = tmp_path / "hot.sqlite"
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("CREATE TABLE probe(id INTEGER PRIMARY KEY, body BLOB)")
        conn.execute("INSERT INTO probe VALUES(1, zeroblob(16384))")
        conn.commit()
        baseline = SFC._read_stable_regular(path, max_bytes=4 * 1024 * 1024)
        conn.execute("PRAGMA cache_size=8")
        conn.execute("PRAGMA cache_spill=ON")
        conn.execute("BEGIN IMMEDIATE")
        for row_id in range(2, 34):
            conn.execute(
                "INSERT INTO probe VALUES(?, zeroblob(16384))", (row_id,))
        dirty = SFC._read_stable_regular(path, max_bytes=4 * 1024 * 1024)
        journal = SFC._read_stable_regular(
            Path(str(path) + "-journal"), max_bytes=4 * 1024 * 1024)
        evidence = SFC._hot_journal_evidence(journal)
        assert dirty != baseline
        assert evidence["journal_magic"] == "d9d505f920a163d7"
        assert evidence["journal_record_count"] > 0
    finally:
        conn.close()


def test_relative_symlink_and_nonempty_roots_fail_before_canary_mutation(
        tmp_path, monkeypatch):
    with pytest.raises(SFC.SharedFSCanaryError, match="绝对路径"):
        SFC.run_local_canary(canary_root="relative", run_id=RUN_ID)

    target = tmp_path / "target"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(SFC.SharedFSCanaryError, match="身份非法"):
        SFC.run_local_canary(canary_root=alias, run_id=RUN_ID)
    assert list(target.iterdir()) == []

    occupied = tmp_path / "production-like"
    occupied.mkdir(mode=0o755)
    marker = occupied / "research.sqlite"
    marker.write_bytes(b"DO-NOT-TOUCH")
    monkeypatch.setattr(
        SFC.database, "connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("nonempty root must be rejected before DB open")))
    before_mode = occupied.stat().st_mode
    with pytest.raises(SFC.SharedFSCanaryError, match="专用空目录"):
        SFC.run_local_canary(canary_root=occupied, run_id=RUN_ID)
    assert marker.read_bytes() == b"DO-NOT-TOUCH"
    assert occupied.stat().st_mode == before_mode


def test_broken_exclusivity_fails_before_database_open(tmp_path, monkeypatch):
    root = tmp_path / "double-owner"
    contract = SFC._create_contract(
        root=root, run_id=RUN_ID, scope=SFC.LOCAL_SCOPE,
        timeout_s=5.0, guardian_grace_s=0.1)
    SFC._publish_node(root, role="holder", contract=contract)
    lease = InstanceLease.acquire(root, heartbeat_interval_s=0.05)
    owner = dict(lease.owner)
    assert lease.close() is None
    SFC._publish_phase(
        root, contract=contract, phase="holder_lease", role="holder",
        evidence={"owner": owner})
    monkeypatch.setattr(
        SFC.database, "connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("double-owner failure must precede DB open")))
    with pytest.raises(SFC.SharedFSCanaryError, match="双 owner"):
        SFC._contender_node(root, contract=contract)


def test_phase_publication_is_no_clobber(local_result):
    root, _result = local_result
    contract, _raw = SFC._load_canonical(
        root / "state/shared-fs-canary/contract.json", label="contract")
    SFC._load_node(root, role="holder", contract=contract)
    with pytest.raises(SFC.SharedFSCanaryError, match="发布失败|receipt"):
        SFC._publish_phase(
            root, contract=contract, phase="holder_lease", role="holder",
            evidence={"owner": {"conflict": True}})


def test_killed_holder_wrapper_cannot_leave_orphan_owner_lease(tmp_path):
    root = tmp_path / "wrapper-death"
    holder = subprocess.Popen(
        [sys.executable, "-m", "orchestrator.shared_fs_canary", "node",
         "--role", "holder", "--canary-root", str(root),
         "--run-id", RUN_ID, "--timeout-s", "5",
         "--guardian-grace-s", "1.0"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        close_fds=True, start_new_session=True)
    owner_pid = None
    replacement = None
    try:
        phase = root / "state/shared-fs-canary/phases/holder_lease.json"
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not phase.exists():
            assert holder.poll() is None
            time.sleep(0.02)
        assert phase.exists()
        owner_pid = json.loads(phase.read_text())["evidence"]["owner"]["pid"]

        os.kill(holder.pid, signal.SIGKILL)
        assert holder.wait(timeout=5.0) == -signal.SIGKILL
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                replacement = InstanceLease.acquire(
                    root, heartbeat_interval_s=0.05)
                break
            except InstanceBusyError:
                time.sleep(0.02)
        assert replacement is not None
        assert replacement.owner["pid"] == os.getpid()
    finally:
        if replacement is not None:
            assert replacement.close() is None
        if holder.poll() is None:
            os.killpg(holder.pid, signal.SIGKILL)
            holder.wait(timeout=5.0)
        if owner_pid is not None:
            try:
                os.kill(owner_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_cli_reports_unexpected_operational_error_as_json(
        tmp_path, monkeypatch, capsys):
    def fail(**_kwargs):
        raise OSError("synthetic operational failure")

    monkeypatch.setattr(SFC, "run_local_canary", fail)
    code = SFC.main([
        "local", "--canary-root", str(tmp_path / "unused"),
        "--run-id", RUN_ID,
    ])
    captured = capsys.readouterr()
    result = json.loads(captured.err)
    assert code == 3
    assert captured.out == ""
    assert result["status"] == "unsafe"
    assert result["error"].startswith("OSError:")


def test_local_second_spawn_failure_cleans_first_process(tmp_path, monkeypatch):
    events = []

    class FakeProcess:
        pid = 424242

        @staticmethod
        def poll():
            return None

        @staticmethod
        def wait(*, timeout):
            events.append(("wait", timeout))
            return -signal.SIGKILL

    calls = 0

    def spawn(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return FakeProcess()
        raise OSError("synthetic second spawn failure")

    monkeypatch.setattr(SFC.subprocess, "Popen", spawn)
    monkeypatch.setattr(
        SFC.os, "killpg", lambda pid, sig: events.append(("killpg", pid, sig)))
    with pytest.raises(OSError, match="second spawn"):
        SFC.run_local_canary(
            canary_root=tmp_path / "spawn-gap", run_id=RUN_ID,
            timeout_s=5.0, guardian_grace_s=0.1)
    assert events == [
        ("killpg", FakeProcess.pid, signal.SIGKILL), ("wait", 5.0)]
