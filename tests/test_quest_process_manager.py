"""Web-owned quest process lifecycle, authority, and redaction boundaries."""
from __future__ import annotations

import hashlib
import json
import os
import signal
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.instance_lease import InstanceLease, read_instance_status
from orchestrator import quest_process_manager as process_manager_module
from orchestrator import quest_registry as quest_registry_module
from orchestrator.quest_process_manager import (
    QuestProcessManager,
    QuestProcessManagerClosedError,
    QuestProcessUnavailableError,
)
from orchestrator.quest_drafts import QuestDraftRegistry
from orchestrator.quest_registry import QuestRegistry
from orchestrator.quest_runtime_profiles import QuestRuntimeSettings
from orchestrator.qualification_profiles import QualificationProfileRegistry
from orchestrator.qualification_firewall import CONTRACT_RELATIVE_PATH
from orchestrator.web_quest_service import WebQuestService


SYSTEM_ROOT = Path(__file__).resolve().parent.parent


def _brief(name: str) -> str:
    return f"""---
predicate_json: {{"kind": "test", "name": "{name}"}}
---

# {name}
"""


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux prctl contract")
def test_web_owner_child_exits_if_parent_identity_was_lost():
    completed = process_manager_module.subprocess.run(
        [sys.executable, "-m", "orchestrator.web_owner_child",
         "--expected-parent-pid", "2147483647", "--"],
        cwd=str(SYSTEM_ROOT), stdin=process_manager_module.subprocess.DEVNULL,
        stdout=process_manager_module.subprocess.PIPE,
        stderr=process_manager_module.subprocess.PIPE,
        start_new_session=True, timeout=5, check=False)
    assert completed.returncode == -signal.SIGTERM


def _registry(tmp_path: Path, *quest_ids: str) -> QuestRegistry:
    registry = QuestRegistry(tmp_path / "registry", SYSTEM_ROOT)
    for quest_id in quest_ids:
        registry.create(
            quest_id=quest_id, title=quest_id.title(),
            goal_brief_md=_brief(quest_id))
    return registry


def _fake_system_root(tmp_path: Path, *, ignore_term: bool = False,
                      profile_poll_s: float = 0.05) -> Path:
    root = tmp_path / ("fake-system-ignore" if ignore_term else "fake-system")
    package = root / "orchestrator"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        "# Extend the fake package with the real support modules.\n"
        f"__path__.append({str(SYSTEM_ROOT / 'orchestrator')!r})\n",
        encoding="utf-8")
    term_setup = (
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)"
        if ignore_term else
        "signal.signal(signal.SIGTERM, stop)")
    (package / "run.py").write_text(f"""
import argparse
import signal
import sys
import time
from orchestrator.instance_lease import InstanceLease
from orchestrator.quest_runtime_profiles import QuestRuntimeSettings

parser = argparse.ArgumentParser()
parser.add_argument('--system-root', required=True)
parser.add_argument('--work-root', required=True)
parser.add_argument('--quest-id', required=True)
parser.add_argument('--runtime-profile-revision', required=True, type=int)
parser.add_argument('--runtime-profile-record-sha256')
parser.add_argument('--max-cycles', required=True)
parser.add_argument('--poll-interval-s', required=True)
outbound = parser.add_mutually_exclusive_group()
outbound.add_argument('--no-outbound', action='store_true')
outbound.add_argument('--connector-profile')
args = parser.parse_args()

settings = QuestRuntimeSettings(args.work_root, args.quest_id)
applied = settings.current()
if ((applied['revision'], applied['record_sha256']) !=
        (args.runtime_profile_revision, args.runtime_profile_record_sha256)):
    raise SystemExit(24)

lease = InstanceLease.acquire(args.work_root, heartbeat_interval_s=0.02)
lease.set_state('running', activity='fake-web-owner')

def stop(_signum, _frame):
    print('FAKE_OWNER_TERM', flush=True)
    error = lease.close()
    raise SystemExit(0 if error is None else 3)

{term_setup}
print('FAKE_OWNER_READY', flush=True)
while True:
    latest = settings.current()
    if ((latest['revision'], latest['record_sha256']) !=
            (applied['revision'], applied['record_sha256'])):
        print('FAKE_OWNER_PROFILE_EXIT', flush=True)
        error = lease.close()
        raise SystemExit(0 if error is None else 3)
    time.sleep({profile_poll_s!r})
""", encoding="utf-8")
    return root


def _early_exit_system_root(tmp_path: Path) -> Path:
    root = tmp_path / "early-exit-system"
    package = root / "orchestrator"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        "# Extend the fake package with the real launcher support.\n"
        f"__path__.append({str(SYSTEM_ROOT / 'orchestrator')!r})\n",
        encoding="utf-8")
    (package / "run.py").write_text(
        "import sys\n"
        "print('EARLY CONFIG FAILURE', flush=True)\n"
        "raise SystemExit(23)\n",
        encoding="utf-8")
    return root


def _bound_recovery_system_root(
        tmp_path: Path, *, always_crash_bound: bool = False) -> Path:
    """Fake owner that crashes/finishes a bound old-policy generation."""
    suffix = "always-crash" if always_crash_bound else "recover-once"
    root = tmp_path / f"bound-recovery-{suffix}"
    package = root / "orchestrator"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        "# Extend fake package with real owner support.\n"
        f"__path__.append({str(SYSTEM_ROOT / 'orchestrator')!r})\n",
        encoding="utf-8")
    (package / "run.py").write_text(f"""
import argparse
import signal
import time
from pathlib import Path
from orchestrator.instance_lease import InstanceLease
from orchestrator.quest_runtime_profiles import QuestRuntimeSettings

parser = argparse.ArgumentParser()
parser.add_argument('--system-root', required=True)
parser.add_argument('--work-root', required=True)
parser.add_argument('--quest-id', required=True)
parser.add_argument('--runtime-profile-revision', required=True, type=int)
parser.add_argument('--runtime-profile-record-sha256')
parser.add_argument('--max-cycles', required=True)
parser.add_argument('--poll-interval-s', required=True)
outbound = parser.add_mutually_exclusive_group()
outbound.add_argument('--no-outbound', action='store_true')
outbound.add_argument('--connector-profile')
args = parser.parse_args()

settings = QuestRuntimeSettings(args.work_root, args.quest_id)
desired = settings.current()
bound = settings.bound_cycle_profile()
applied = bound if bound is not None else desired
if ((applied['revision'], applied['record_sha256']) !=
        (args.runtime_profile_revision, args.runtime_profile_record_sha256)):
    raise SystemExit(24)

lease = InstanceLease.acquire(args.work_root, heartbeat_interval_s=0.02)
lease.set_state('running', activity='fake-bound-recovery')

def stop(_signum, _frame):
    error = lease.close()
    raise SystemExit(0 if error is None else 3)

signal.signal(signal.SIGTERM, stop)
print('FAKE_BOUND_READY rev=' + str(applied['revision']), flush=True)
if bound is not None and ((applied['revision'], applied['record_sha256']) !=
                          (desired['revision'], desired['record_sha256'])):
    count_path = Path(args.work_root) / 'state' / 'fake-bound-count'
    count = int(count_path.read_text(encoding='ascii')) if count_path.exists() else 0
    count_path.write_text(str(count + 1), encoding='ascii')
    if {always_crash_bound!r} or count == 0:
        print('FAKE_BOUND_CRASH', flush=True)
        lease.close()
        raise SystemExit(31)
    settings.clear_cycle_profile(applied)
    print('FAKE_BOUND_CLEAR', flush=True)
    lease.close()
    raise SystemExit(0)

while True:
    time.sleep(0.05)
""", encoding="utf-8")
    return root


def _wait_status(manager: QuestProcessManager, quest_id: str, predicate,
                 timeout_s: float = 8.0):
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        last = manager.status(quest_id)
        if predicate(last):
            return last
        time.sleep(0.03)
    raise AssertionError(f"status did not converge: {last}")


def _canonical(value) -> bytes:
    return (json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def test_start_is_fixed_argv_single_process_and_idempotent_across_keys(
        tmp_path, monkeypatch):
    registry = _registry(tmp_path, "alpha")
    fake_root = _fake_system_root(tmp_path)
    calls = []
    real_popen = process_manager_module.subprocess.Popen

    def recording_popen(argv, **kwargs):
        calls.append((list(argv), dict(kwargs)))
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(process_manager_module.subprocess, "Popen", recording_popen)
    manager = QuestProcessManager(
        registry, fake_root, max_cycles=7, poll_interval_s=0.02)
    try:
        first = manager.start("alpha", "a" * 32)
        assert first["active"] is True and first["managed_by_web"] is True
        running = _wait_status(
            manager, "alpha", lambda value: value["owner_state"] == "running")
        assert running["state"] == "running" and running["terminable"] is True

        assert manager.start("alpha", "a" * 32)["active"] is True
        assert manager.start("alpha", "b" * 32)["active"] is True
        assert len(calls) == 1
        argv, kwargs = calls[0]
        quest = registry.get("alpha")
        assert argv == [
            sys.executable, "-m", "orchestrator.web_owner_child",
            "--expected-parent-pid", str(os.getpid()), "--",
            "--system-root", str(fake_root),
            "--work-root", str(quest.work_root),
            "--quest-id", "alpha",
            "--runtime-profile-revision", "0",
            "--max-cycles", "7", "--poll-interval-s", "0.02",
            "--no-outbound",
        ]
        assert kwargs["shell"] is False
        assert kwargs["start_new_session"] is True
        assert kwargs["close_fds"] is True
        assert kwargs["stdin"] is process_manager_module.subprocess.DEVNULL
        assert kwargs["stderr"] is process_manager_module.subprocess.STDOUT

        stopped = manager.terminate("alpha", "c" * 32)
        assert stopped["state"] == "stopped" and stopped["exit_code"] == 0
        assert manager.terminate("alpha", "c" * 32) == stopped
        # Both keys that observed the first active generation remain
        # idempotent after exit; neither may unexpectedly launch generation 2.
        assert manager.start("alpha", "a" * 32)["state"] == "stopped"
        assert manager.start("alpha", "b" * 32)["state"] == "stopped"
        assert len(calls) == 1

        manager.start("alpha", "d" * 32)
        _wait_status(manager, "alpha", lambda value: value["owner_state"] == "running")
        assert len(calls) == 2
        manager.terminate("alpha", "e" * 32)

        log = quest.work_root / "state" / "web-owner.log"
        assert log.stat().st_mode & 0o777 == 0o600
        body = log.read_text(encoding="utf-8")
        assert body.count("FAKE_OWNER_READY") == 2
        assert body.count("FAKE_OWNER_TERM") == 2
    finally:
        manager.close()


def test_active_noop_start_key_does_not_spawn_after_stop_and_reconstruction(
        tmp_path, monkeypatch):
    registry = _registry(tmp_path, "alpha")
    fake_root = _fake_system_root(tmp_path)
    first = QuestProcessManager(
        registry, fake_root, poll_interval_s=0.02)
    try:
        started = first.start("alpha", "a" * 32)
        assert started["active"] is True
        _wait_status(
            first, "alpha", lambda value: value["owner_state"] == "running")

        active_noop = first.start("alpha", "b" * 32)
        assert active_noop["active"] is True
        assert active_noop["managed_by_web"] is True
        receipt_path = (
            registry.get("alpha").work_root / "state" / "runtime-settings"
            / "start-operations" / ("b" * 32 + ".json"))
        receipt = json.loads(receipt_path.read_bytes())
        assert set(receipt) == {
            "version", "quest_id", "idempotency_key", "outcome",
            "owner_intent_revision", "recorded_at"}
        assert receipt["outcome"] == "active-noop"
        assert receipt["owner_intent_revision"] == 1
        assert receipt_path.stat().st_mode & 0o777 == 0o600
        assert receipt_path.read_bytes() == _canonical(receipt)

        stopped = first.terminate("alpha", "c" * 32)
        assert stopped["active"] is False
    finally:
        first.close()

    second = QuestProcessManager(
        registry, fake_root, poll_interval_s=0.02)
    spawn_attempts = []

    def forbidden_spawn(*args, **kwargs):
        spawn_attempts.append((args, kwargs))
        raise AssertionError(
            "重建 manager 重放 active-noop start key 不得 spawn")

    monkeypatch.setattr(second, "_spawn_managed_child", forbidden_spawn)
    try:
        replay = second.start("alpha", "b" * 32)
        assert replay["active"] is False
        assert spawn_attempts == []
    finally:
        second.close()


def test_start_observes_immediate_owner_exit_and_web_diagnostic(tmp_path):
    registry = _registry(tmp_path, "alpha")
    manager = QuestProcessManager(registry, _early_exit_system_root(tmp_path))
    try:
        started = manager.start("alpha", "a" * 32)
        assert started["state"] == "exited"
        assert started["active"] is False
        assert started["managed_by_web"] is True
        assert started["exit_code"] == 23
        diagnostic = manager.log_tail("alpha")
        assert diagnostic["available"] is True
        assert "EARLY CONFIG FAILURE" in diagnostic["text"]
        assert str(manager.system_root) not in diagnostic["text"]
    finally:
        manager.close()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux prctl contract")
def test_owner_survives_the_short_lived_http_style_start_thread(tmp_path):
    """PDEATHSIG must follow a manager-lifetime launcher, not a request thread."""
    registry = _registry(tmp_path, "alpha")
    manager = QuestProcessManager(
        registry, _fake_system_root(tmp_path), poll_interval_s=0.02)
    result = {}
    errors = []

    def http_style_request():
        try:
            result.update(manager.start("alpha", "a" * 32))
        except BaseException as error:
            errors.append(error)

    request_thread = process_manager_module.threading.Thread(
        target=http_style_request)
    try:
        request_thread.start()
        request_thread.join(timeout=5)
        assert not request_thread.is_alive() and not errors
        assert result["active"] is True
        # The creator request thread is now gone.  A child created directly by
        # it receives SIGTERM here on Linux; the broker-spawned owner remains.
        time.sleep(0.15)
        running = _wait_status(
            manager, "alpha", lambda value: value["owner_state"] == "running")
        assert running["active"] is True and running["exit_code"] is None
        assert manager.terminate("alpha", "b" * 32)["exit_code"] == 0
    finally:
        manager.close()


def test_public_status_redacts_process_and_path_authority(tmp_path):
    registry = _registry(tmp_path, "alpha")
    manager = QuestProcessManager(registry, _fake_system_root(tmp_path))
    try:
        inactive = manager.status("alpha")
        assert set(inactive) == {
            "quest_id", "state", "active", "managed_by_web", "terminable",
            "exit_code", "owner_state", "heartbeat_age_s", "log_ref",
            "runtime_profile_restart_pending",
            "applied_runtime_profile_revision",
            "runtime_profile_restart_error",
        }
        serialized = json.dumps(inactive, sort_keys=True)
        assert str(registry.get("alpha").work_root) not in serialized
        assert str(manager.system_root) not in serialized
        assert "web-owner.log" in serialized
        assert inactive["runtime_profile_restart_pending"] is False
        assert inactive["applied_runtime_profile_revision"] is None
        assert inactive["runtime_profile_restart_error"] is None
        assert not ({"pid", "argv", "command", "work_root", "system_root"}
                    & set(inactive))
    finally:
        manager.close()


def test_runtime_health_is_path_free_and_checks_exact_image(tmp_path, monkeypatch):
    registry = _registry(tmp_path, "alpha")
    manager = QuestProcessManager(registry, SYSTEM_ROOT)
    monkeypatch.delenv("METARESEARCH_CODEX_BIN", raising=False)
    image_id = (
        "sha256:eeeca161459e242a142661623fed2320f84e810603a42ffc4a5cdbc8694b3bb3")
    completed = type("Completed", (), {
        "returncode": 0, "stdout": image_id + "\n"})()
    launcher_probes = []

    def available_launcher(value):
        launcher_probes.append(value)
        return "/bin/tool"

    monkeypatch.setattr(process_manager_module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(process_manager_module.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        process_manager_module.shutil, "which", available_launcher)
    monkeypatch.setattr(process_manager_module.os.path, "isfile", lambda _value: True)
    monkeypatch.setattr(process_manager_module.os, "access", lambda *_args: True)
    monkeypatch.setattr(
        process_manager_module.subprocess, "run", lambda *_args, **_kwargs: completed)
    try:
        health = manager.runtime_health()
        assert health["ready"] is True
        assert all(health["checks"].values())
        assert launcher_probes.count("/usr/local/bin/codex") == 2
        assert "codex-chatgpt" not in launcher_probes
        assert str(SYSTEM_ROOT) not in json.dumps(health)
        assert str(registry.root) not in json.dumps(health)
    finally:
        manager.close()


def test_runtime_profile_options_projects_live_trusted_exact_gpu_catalog(
        tmp_path, monkeypatch):
    registry = _registry(tmp_path, "alpha")
    system_root = tmp_path / "gpu-catalog-system"
    policy_root = system_root / "policies"
    policy_root.mkdir(parents=True)
    (policy_root / "policy.yaml").write_text(
        "resources:\n"
        "  gpus: 1\n"
        "  gpu_mem_gb: 80\n"
        "  allowed_device_indices: [1, 3, 5]\n",
        encoding="utf-8")
    calls = []

    def probe(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=(
                b"5, NVIDIA H100 96GB, 98304\n"
                b"1, NVIDIA A100-SXM4-80GB, 81920\n"
                b"3, NVIDIA L40S, 49152\n"
                b"7, NVIDIA H100 outside policy, 98304\n"),
            stderr=b"")

    monkeypatch.setattr(
        process_manager_module.shutil, "which",
        lambda name, path=None: "/usr/bin/nvidia-smi"
        if name == "nvidia-smi" and path == os.defpath else None)
    monkeypatch.setattr(process_manager_module.subprocess, "run", probe)
    manager = QuestProcessManager(registry, system_root)
    try:
        options = manager.runtime_profile_options()
        assert options["version"] == 3
        assert [{
            "index": row["index"],
            "model": row["model"],
            "memory_bytes": row["memory_bytes"],
        } for row in options["gpu_devices"]] == [
            {
                "index": 1,
                "model": "NVIDIA A100-SXM4-80GB",
                "memory_bytes": 81920 * 1024 * 1024,
            },
            {
                "index": 5,
                "model": "NVIDIA H100 96GB",
                "memory_bytes": 98304 * 1024 * 1024,
            },
        ]
        assert all(set(row) == {"index", "label", "model", "memory_bytes"}
                   for row in options["gpu_devices"])
        assert options["gpu_selection"] == {
            "mode": "exact", "default_count": 2,
            "min_count": 1, "max_count": 2,
        }
        assert options["default_profile"] == {
            "version": 3,
            "compute_profile_id": "local-gpu",
            "review_intensity": "once",
            "gpu_device_indices": [1, 5],
        }
        assert manager.runtime_profile_legacy_gpu_count() == 1
        serialized = json.dumps(options, ensure_ascii=False, sort_keys=True)
        assert "allowed_device_indices" not in serialized
        assert "gpu_mem_gb" not in serialized
        assert "uuid" not in serialized.lower()
        assert str(system_root) not in serialized

        argv, kwargs = calls[0]
        assert argv == [
            "/usr/bin/nvidia-smi",
            "--query-gpu=index,name,memory.total",
            "--format=csv,noheader,nounits",
        ]
        assert kwargs["timeout"] <= 3.0
        assert kwargs["stdin"] is process_manager_module.subprocess.DEVNULL
        assert kwargs["stdout"] is process_manager_module.subprocess.PIPE
        assert kwargs["stderr"] is process_manager_module.subprocess.PIPE
        assert kwargs["check"] is False
        assert kwargs["start_new_session"] is True
        assert kwargs["env"] == {
            "PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        }

        # Every read is a fresh projection; callers cannot mutate policy-backed
        # defaults through a previously returned catalog.
        options["gpu_devices"][0]["index"] = 99
        assert manager.runtime_profile_options()["gpu_devices"][0]["index"] == 1
    finally:
        manager.close()


@pytest.mark.parametrize("stdout", [
    b"1, NVIDIA A100, 81920\n1, NVIDIA A100, 81920\n",
    b"1, GPU-secret, NVIDIA A100, 81920\n",
    b"1, /private/gpu-name, 81920\n",
    b"1, NVIDIA A100, 0\n",
    b"1, NVIDIA A100, 81920\xff\n",
    b"x" * (64 * 1024 + 1),
], ids=["duplicate", "extra-uuid-field", "path-model", "zero-memory",
        "non-utf8", "over-limit"])
def test_runtime_profile_options_rejects_malformed_or_unbounded_gpu_probe(
        tmp_path, monkeypatch, stdout):
    registry = _registry(tmp_path, "alpha")
    system_root = tmp_path / "gpu-catalog-invalid"
    (system_root / "policies").mkdir(parents=True)
    (system_root / "policies" / "policy.yaml").write_text(
        "resources:\n"
        "  gpus: 1\n"
        "  gpu_mem_gb: 1\n"
        "  allowed_device_indices: [1]\n",
        encoding="utf-8")
    monkeypatch.setattr(
        process_manager_module.shutil, "which",
        lambda _name, path=None: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(
        process_manager_module.subprocess, "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout=stdout, stderr=b""))
    manager = QuestProcessManager(registry, system_root)
    try:
        with pytest.raises(
                QuestProcessUnavailableError,
                match="runtime GPU option catalog 不可用"):
            manager.runtime_profile_options()
    finally:
        manager.close()


def test_runtime_profile_options_gpu_probe_timeout_fails_closed(
        tmp_path, monkeypatch):
    registry = _registry(tmp_path, "alpha")
    system_root = tmp_path / "gpu-catalog-timeout"
    (system_root / "policies").mkdir(parents=True)
    (system_root / "policies" / "policy.yaml").write_text(
        "resources:\n"
        "  gpus: 1\n"
        "  gpu_mem_gb: 1\n"
        "  allowed_device_indices: [0]\n",
        encoding="utf-8")
    monkeypatch.setattr(
        process_manager_module.shutil, "which",
        lambda _name, path=None: "/usr/bin/nvidia-smi")

    def timeout(*_args, **_kwargs):
        raise process_manager_module.subprocess.TimeoutExpired(
            cmd="nvidia-smi", timeout=3.0)

    monkeypatch.setattr(process_manager_module.subprocess, "run", timeout)
    manager = QuestProcessManager(registry, system_root)
    try:
        with pytest.raises(QuestProcessUnavailableError):
            manager.runtime_profile_options()
    finally:
        manager.close()


def test_public_log_tail_is_bounded_and_redacts_backend_paths(tmp_path):
    registry = _registry(tmp_path, "alpha")
    manager = QuestProcessManager(registry, _fake_system_root(tmp_path))
    quest = registry.get("alpha")
    log = quest.work_root / "state" / "web-owner.log"
    log.write_text(
        f"loading {quest.work_root}/input\n"
        f"system={manager.system_root}/policies/policy.yaml\n"
        "RuntimeError: Docker capability unavailable\n",
        encoding="utf-8")
    log.chmod(0o600)
    try:
        diagnostic = manager.log_tail("alpha")
        assert diagnostic["available"] is True
        assert "Docker capability unavailable" in diagnostic["text"]
        assert str(quest.work_root) not in diagnostic["text"]
        assert str(manager.system_root) not in diagnostic["text"]
        assert "[quest]" in diagnostic["text"] or "[path]" in diagnostic["text"]
    finally:
        manager.close()


def test_external_owner_is_observable_but_never_terminable_by_this_manager(
        tmp_path):
    registry = _registry(tmp_path, "alpha")
    quest = registry.get("alpha")
    lease = InstanceLease.acquire(quest.work_root, heartbeat_interval_s=0.02)
    manager = QuestProcessManager(registry, _fake_system_root(tmp_path))
    try:
        external = manager.status("alpha")
        assert external["state"] == "external_active"
        assert external["active"] is True
        assert external["managed_by_web"] is False
        assert external["terminable"] is False
        started = manager.start("alpha", "a" * 32)
        assert started["state"] == "external_active"
        assert started["active"] is True
        assert started["managed_by_web"] is False
        assert started["terminable"] is False
        denied = manager.terminate("alpha", "b" * 32)
        assert denied["state"] == "external_active"
        assert denied["active"] is True
        assert denied["managed_by_web"] is False
        assert denied["terminable"] is False
        with pytest.raises(QuestProcessUnavailableError, match="external_active"):
            manager.schedule_runtime_profile_restart("alpha", "d" * 32)
        lease.assert_owned()
        assert not (quest.work_root / "state" / "web-owner.log").exists()
    finally:
        manager.close()
        assert lease.close() is None


def test_runtime_profile_restart_coalesces_latest_after_cooperative_exit(
        tmp_path, monkeypatch):
    registry = _registry(tmp_path, "alpha")
    quest = registry.get("alpha")
    settings = QuestRuntimeSettings(quest.work_root, quest.quest_id)
    first = settings.initialize(
        {"version": 1, "compute_profile_id": "local-gpu",
         "review_intensity": "once"},
        "1" * 32)
    fake_root = _fake_system_root(tmp_path, profile_poll_s=0.5)
    calls = []
    real_popen = process_manager_module.subprocess.Popen

    def recording_popen(argv, **kwargs):
        calls.append(list(argv))
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(process_manager_module.subprocess, "Popen", recording_popen)
    manager = QuestProcessManager(
        registry, fake_root, poll_interval_s=0.02)
    try:
        manager.start("alpha", "a" * 32)
        _wait_status(manager, "alpha", lambda value: value["owner_state"] == "running")
        slot = manager._slot("alpha", require_open=False)
        with slot.lock:
            assert slot.child is not None
            assert slot.child.applied_runtime_revision == first["revision"]

        second = settings.update(
            {"version": 1, "compute_profile_id": "local-cpu",
             "review_intensity": "once"},
            "2" * 32)
        scheduled = manager.schedule_runtime_profile_restart("alpha", "c" * 32)
        assert scheduled["runtime_profile_restart"] == "scheduled"
        assert scheduled["runtime_profile_revision"] == second["revision"]
        assert scheduled["runtime_profile_restart_pending"] is True
        assert scheduled["applied_runtime_profile_revision"] == first["revision"]

        # Commit another choice before the old owner reaches its polling
        # boundary.  One watcher remains and must launch only the newest
        # revision, never the intermediate one.
        latest = settings.update(
            {"version": 1, "compute_profile_id": "local-gpu",
             "review_intensity": "off"},
            "3" * 32)
        coalesced = manager.schedule_runtime_profile_restart("alpha", "d" * 32)
        assert coalesced["runtime_profile_restart"] == "scheduled"
        assert coalesced["runtime_profile_revision"] == latest["revision"]

        def forbidden_signal(*_args, **_kwargs):
            raise AssertionError("profile restart watcher 不得 signal 当前 stage")

        # This guard covers the whole old-owner exit/replacement interval.
        with monkeypatch.context() as guarded:
            guarded.setattr(manager, "_send_group", forbidden_signal)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                status = manager.status("alpha")
                with slot.lock:
                    child = slot.child
                    applied = (
                        None if child is None
                        else child.applied_runtime_revision)
                if status["owner_state"] == "running" and applied == latest["revision"]:
                    break
                time.sleep(0.03)
            else:
                raise AssertionError("cooperative runtime profile replacement 未完成")

        refreshed = manager.status("alpha")
        assert refreshed["runtime_profile_restart_pending"] is False
        assert refreshed["applied_runtime_profile_revision"] == latest["revision"]
        assert refreshed["runtime_profile_restart_error"] is None

        assert len(calls) == 2
        assert "--runtime-profile-record-sha256" in calls[1]
        assert latest["record_sha256"] in calls[1]
        body = (quest.work_root / "state" / "web-owner.log").read_text(
            encoding="utf-8")
        assert body.count("FAKE_OWNER_PROFILE_EXIT") == 1
        assert body.count("FAKE_OWNER_READY") == 2
        manager.terminate("alpha", "e" * 32)
    finally:
        manager.close()


def test_cross_manager_stop_fence_closes_replacement_popen_race(
        tmp_path, monkeypatch):
    registry = _registry(tmp_path, "alpha")
    quest = registry.get("alpha")
    settings = QuestRuntimeSettings(quest.work_root, quest.quest_id)
    settings.initialize({
        "version": 1, "compute_profile_id": "local-gpu",
        "review_intensity": "once",
    }, "1" * 32)
    fake_root = _fake_system_root(tmp_path, profile_poll_s=0.05)
    manager_a = QuestProcessManager(
        registry, fake_root, poll_interval_s=0.02)
    manager_b = QuestProcessManager(
        registry, fake_root, poll_interval_s=0.02)
    replacement_spawn_entered = process_manager_module.threading.Event()
    release_replacement_spawn = process_manager_module.threading.Event()
    replacement_popen_returned = process_manager_module.threading.Event()
    spawn_calls = []
    replacement_processes = []
    real_spawn = manager_a._spawn

    def block_second_spawn(argv, **kwargs):
        spawn_calls.append(list(argv))
        is_replacement = len(spawn_calls) == 2
        if is_replacement:
            replacement_spawn_entered.set()
            assert release_replacement_spawn.wait(timeout=5)
        try:
            process = real_spawn(argv, **kwargs)
            if is_replacement:
                replacement_processes.append(process)
            return process
        finally:
            if is_replacement:
                replacement_popen_returned.set()

    monkeypatch.setattr(manager_a, "_spawn", block_second_spawn)
    try:
        manager_a.start("alpha", "a" * 32)
        _wait_status(
            manager_a, "alpha",
            lambda value: value["owner_state"] == "running")

        update_key = "b" * 32
        operation = settings.begin_runtime_update({
            "version": 1, "compute_profile_id": "local-cpu",
            "review_intensity": "off",
        }, update_key)
        latest = operation["outcome"]
        assert settings.accept_runtime_update(update_key)["status"] == "accepted"
        scheduled = manager_a.schedule_runtime_profile_restart(
            "alpha", update_key)
        assert scheduled["runtime_profile_restart"] == "scheduled"
        assert replacement_spawn_entered.wait(timeout=8)

        stopped = manager_b.terminate("alpha", "c" * 32)
        assert stopped["active"] is False
        assert settings.runtime_update_operation_by_key(
            update_key)["status"] == "terminated"

        release_replacement_spawn.set()
        assert replacement_popen_returned.wait(timeout=5)
        slot = manager_a._slot("alpha", require_open=False)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            with slot.lock:
                watcher = slot.runtime_restart_watcher
            if watcher is None or not watcher.is_alive():
                break
            time.sleep(0.03)
        else:
            raise AssertionError("replacement watcher 未在 Popen race 后退出")

        assert len(spawn_calls) == 2
        assert len(replacement_processes) == 1
        replacement = replacement_processes[0]
        assert replacement.poll() is not None
        assert manager_a._group_alive(replacement.pid) is False
        with slot.lock:
            watcher = slot.runtime_restart_watcher
            assert watcher is None or not watcher.is_alive()
        final = manager_a.status("alpha")
        assert final["active"] is False, (
            "durable stop fence 写入后启动的 replacement 未被收口: "
            f"revision={latest['revision']}, state={final['state']}")
    finally:
        release_replacement_spawn.set()
        manager_b.close()
        manager_a.close()


def test_cross_manager_stop_fence_before_replacement_prevents_popen(
        tmp_path, monkeypatch):
    registry = _registry(tmp_path, "alpha")
    quest = registry.get("alpha")
    settings = QuestRuntimeSettings(quest.work_root, quest.quest_id)
    settings.initialize({
        "version": 1, "compute_profile_id": "local-gpu",
        "review_intensity": "once",
    }, "1" * 32)
    fake_root = _fake_system_root(tmp_path, profile_poll_s=0.05)
    manager_a = QuestProcessManager(
        registry, fake_root, poll_interval_s=0.02)
    manager_b = QuestProcessManager(
        registry, fake_root, poll_interval_s=0.02)
    replacement_decided = process_manager_module.threading.Event()
    release_replacement = process_manager_module.threading.Event()
    actual_replacement_popens = []
    try:
        manager_a.start("alpha", "a" * 32)
        _wait_status(
            manager_a, "alpha",
            lambda value: value["owner_state"] == "running")

        real_managed_spawn = manager_a._spawn_managed_child
        real_spawn = manager_a._spawn

        def pause_before_generation_precheck(*args, **kwargs):
            replacement_decided.set()
            assert release_replacement.wait(timeout=5)
            return real_managed_spawn(*args, **kwargs)

        def recording_replacement_popen(argv, **kwargs):
            actual_replacement_popens.append(list(argv))
            return real_spawn(argv, **kwargs)

        monkeypatch.setattr(
            manager_a, "_spawn_managed_child", pause_before_generation_precheck)
        monkeypatch.setattr(manager_a, "_spawn", recording_replacement_popen)

        update_key = "b" * 32
        settings.begin_runtime_update({
            "version": 1, "compute_profile_id": "local-cpu",
            "review_intensity": "off",
        }, update_key)
        settings.accept_runtime_update(update_key)
        assert manager_a.schedule_runtime_profile_restart(
            "alpha", update_key)["runtime_profile_restart"] == "scheduled"
        assert replacement_decided.wait(timeout=8)

        assert manager_b.terminate("alpha", "c" * 32)["active"] is False
        assert settings.runtime_update_operation_by_key(
            update_key)["status"] == "terminated"
        release_replacement.set()

        slot = manager_a._slot("alpha", require_open=False)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            with slot.lock:
                watcher = slot.runtime_restart_watcher
            if watcher is None or not watcher.is_alive():
                break
            time.sleep(0.03)
        else:
            raise AssertionError("pre-Popen fence 后 replacement watcher 未退出")

        assert actual_replacement_popens == []
        assert manager_a.status("alpha")["active"] is False
    finally:
        release_replacement.set()
        manager_b.close()
        manager_a.close()


def test_runtime_profile_restart_is_noop_while_inactive(tmp_path):
    registry = _registry(tmp_path, "alpha")
    manager = QuestProcessManager(registry, _fake_system_root(tmp_path))
    try:
        result = manager.schedule_runtime_profile_restart("alpha", "a" * 32)
        assert result["runtime_profile_restart"] == "not_required"
        assert result["state"] == "inactive"
        assert not (registry.get("alpha").work_root / "state" / "web-owner.log").exists()
    finally:
        manager.close()


def test_reconstructed_manager_applies_accepted_update_without_binding(tmp_path):
    registry = _registry(tmp_path, "alpha")
    quest = registry.get("alpha")
    settings = QuestRuntimeSettings(quest.work_root, quest.quest_id)
    settings.initialize({
        "version": 1, "compute_profile_id": "local-gpu",
        "review_intensity": "once",
    }, "1" * 32)
    key = "c" * 32
    operation = settings.begin_runtime_update({
        "version": 1, "compute_profile_id": "local-cpu",
        "review_intensity": "off",
    }, key)
    latest = operation["outcome"]
    settings.accept_runtime_update(key)

    manager = QuestProcessManager(
        registry, _fake_system_root(tmp_path), poll_interval_s=0.02)
    try:
        scheduled = manager.schedule_runtime_profile_restart("alpha", key)
        assert scheduled["runtime_profile_restart"] == "scheduled"
        status = _wait_status(
            manager, "alpha",
            lambda value: (value["owner_state"] == "running"
                           and value["applied_runtime_profile_revision"]
                           == latest["revision"]))
        assert status["active"] is True
        assert settings.runtime_update_operation_by_key(key)["status"] == "applied"
        manager.terminate("alpha", "d" * 32)
    finally:
        manager.close()


def test_reconstructed_manager_recovers_accepted_stale_binding_then_latest(
        tmp_path, monkeypatch):
    registry = _registry(tmp_path, "alpha")
    quest = registry.get("alpha")
    settings = QuestRuntimeSettings(quest.work_root, quest.quest_id)
    old = settings.initialize({
        "version": 1, "compute_profile_id": "local-gpu",
        "review_intensity": "once",
    }, "1" * 32)
    settings.bind_cycle_profile(old)
    key = "c" * 32
    operation = settings.begin_runtime_update({
        "version": 1, "compute_profile_id": "local-cpu",
        "review_intensity": "off",
    }, key)
    latest = operation["outcome"]
    settings.accept_runtime_update(key)
    calls = []
    real_popen = process_manager_module.subprocess.Popen

    def recording_popen(argv, **kwargs):
        calls.append(list(argv))
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(process_manager_module.subprocess, "Popen", recording_popen)
    manager = QuestProcessManager(
        registry, _bound_recovery_system_root(tmp_path),
        poll_interval_s=0.02)
    try:
        scheduled = manager.schedule_runtime_profile_restart("alpha", key)
        assert scheduled["runtime_profile_restart"] == "scheduled"
        _wait_status(
            manager, "alpha",
            lambda value: (value["owner_state"] == "running"
                           and value["applied_runtime_profile_revision"]
                           == latest["revision"]))
        revisions = [
            int(argv[argv.index("--runtime-profile-revision") + 1])
            for argv in calls]
        assert revisions == [old["revision"], old["revision"], latest["revision"]]
        assert settings.bound_cycle_profile() is None
        assert settings.runtime_update_operation_by_key(key)["status"] == "applied"
        manager.terminate("alpha", "d" * 32)
    finally:
        manager.close()


def test_explicit_terminate_prevents_accepted_key_from_reviving_in_new_manager(
        tmp_path):
    registry = _registry(tmp_path, "alpha")
    quest = registry.get("alpha")
    settings = QuestRuntimeSettings(quest.work_root, quest.quest_id)
    settings.initialize({
        "version": 1, "compute_profile_id": "local-gpu",
        "review_intensity": "once",
    }, "1" * 32)
    key = "c" * 32
    settings.begin_runtime_update({
        "version": 1, "compute_profile_id": "local-cpu",
        "review_intensity": "off",
    }, key)
    settings.accept_runtime_update(key)

    first = QuestProcessManager(registry, _fake_system_root(tmp_path / "first"))
    try:
        inactive = first.terminate("alpha", "d" * 32)
        assert inactive["active"] is False
        assert settings.runtime_update_operation_by_key(key)["status"] == "terminated"
    finally:
        first.close()

    second = QuestProcessManager(registry, _fake_system_root(tmp_path / "second"))
    try:
        replay = second.schedule_runtime_profile_restart("alpha", key)
        assert replay["runtime_profile_restart"] == "not_required"
        assert replay["active"] is False
        assert not (quest.work_root / "state" / "web-owner.log").exists()
    finally:
        second.close()


def test_terminate_between_update_status_and_receipt_fences_restart(
        tmp_path, monkeypatch):
    registry = QuestRegistry(tmp_path / "product", SYSTEM_ROOT)
    drafts = QuestDraftRegistry(registry.state_dir / "quest-drafts")
    profiles = QualificationProfileRegistry(tmp_path / "no-profiles")
    manager = QuestProcessManager(
        registry, _fake_system_root(tmp_path), poll_interval_s=0.02)
    service = WebQuestService(
        registry=registry, drafts=drafts, profiles=profiles,
        processes=manager)
    update_thread = None
    release_update = process_manager_module.threading.Event()
    try:
        draft = drafts.create({
            "quest_id": "stop-fence-race",
            "title": "Stop fence race",
            "template_id": "toy-gauss-smoke",
        }, "1" * 32)
        service.publish(
            draft["draft_id"], start=False,
            idempotency_key="2" * 32)
        quest = registry.get("stop-fence-race")
        settings = QuestRuntimeSettings(quest.work_root, quest.quest_id)
        old_profile = settings.current()
        settings.bind_cycle_profile(old_profile)
        manager.start(quest.quest_id, "3" * 32)
        _wait_status(
            manager, quest.quest_id,
            lambda value: value["owner_state"] == "running")

        first_status_returned = process_manager_module.threading.Event()
        real_status = manager.status
        status_calls = []

        def status_barrier(quest_id):
            value = real_status(quest_id)
            status_calls.append(value["state"])
            if len(status_calls) == 1:
                first_status_returned.set()
                assert release_update.wait(timeout=5)
            return value

        spawn_attempts = []

        def forbidden_spawn(*args, **kwargs):
            spawn_attempts.append((args, kwargs))
            raise AssertionError("stop fence 后不得尝试 recovery spawn")

        monkeypatch.setattr(manager, "status", status_barrier)
        monkeypatch.setattr(manager, "_spawn_managed_child", forbidden_spawn)
        profile = {
            "version": 1, "compute_profile_id": "local-cpu",
            "review_intensity": "off",
        }
        update_key = "a" * 32
        results = {}
        errors = []

        def update_profile():
            try:
                results["update"] = service.update_runtime_profile(
                    quest.quest_id, profile, update_key)
            except BaseException as error:  # surfaced in the parent thread
                errors.append(error)

        update_thread = process_manager_module.threading.Thread(
            target=update_profile)
        update_thread.start()
        assert first_status_returned.wait(timeout=5)

        stopped = manager.terminate(quest.quest_id, "b" * 32)
        assert stopped["active"] is False
        assert settings.runtime_update_operation_by_key(update_key) is None
        release_update.set()
        update_thread.join(timeout=10)

        assert not update_thread.is_alive()
        assert spawn_attempts == []
        assert errors == []
        assert results["update"]["restart_pending"] is False
        assert settings.current()["profile"] == profile
        operation = settings.runtime_update_operation_by_key(update_key)
        assert operation is not None
        assert operation["status"] == "terminated"
    finally:
        release_update.set()
        if update_thread is not None:
            update_thread.join(timeout=10)
        service.close()


def test_terminal_update_key_remains_no_spawn_after_manager_reconstruction(
        tmp_path, monkeypatch):
    registry = _registry(tmp_path, "alpha")
    quest = registry.get("alpha")
    settings = QuestRuntimeSettings(quest.work_root, quest.quest_id)
    settings.initialize({
        "version": 1, "compute_profile_id": "local-gpu",
        "review_intensity": "once",
    }, "1" * 32)
    update_key = "a" * 32
    settings.begin_runtime_update({
        "version": 1, "compute_profile_id": "local-cpu",
        "review_intensity": "off",
    }, update_key)
    settings.accept_runtime_update(update_key)

    first = QuestProcessManager(
        registry, _fake_system_root(tmp_path / "first-manager"))
    try:
        first.terminate("alpha", "b" * 32)
        assert settings.runtime_update_operation_by_key(
            update_key)["status"] == "terminated"
    finally:
        first.close()

    second = QuestProcessManager(
        registry, _fake_system_root(tmp_path / "second-manager"))
    spawn_attempts = []

    def forbidden_spawn(*args, **kwargs):
        spawn_attempts.append((args, kwargs))
        raise AssertionError("重建 manager 不得用 terminal update key spawn")

    monkeypatch.setattr(second, "_spawn_managed_child", forbidden_spawn)
    try:
        replay = second.schedule_runtime_profile_restart("alpha", update_key)
        assert replay["runtime_profile_restart"] == "not_required"
        assert replay["active"] is False
        assert spawn_attempts == []
        assert settings.runtime_update_operation_by_key(
            update_key)["status"] == "terminated"
    finally:
        second.close()


def test_new_explicit_start_after_stop_launches_latest_without_reopening_old_update(
        tmp_path, monkeypatch):
    registry = _registry(tmp_path, "alpha")
    quest = registry.get("alpha")
    settings = QuestRuntimeSettings(quest.work_root, quest.quest_id)
    settings.initialize({
        "version": 1, "compute_profile_id": "local-gpu",
        "review_intensity": "once",
    }, "1" * 32)
    update_key = "a" * 32
    operation = settings.begin_runtime_update({
        "version": 1, "compute_profile_id": "local-cpu",
        "review_intensity": "off",
    }, update_key)
    latest = operation["outcome"]
    settings.accept_runtime_update(update_key)

    calls = []
    real_popen = process_manager_module.subprocess.Popen

    def recording_popen(argv, **kwargs):
        calls.append(list(argv))
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(
        process_manager_module.subprocess, "Popen", recording_popen)
    manager = QuestProcessManager(
        registry, _fake_system_root(tmp_path), poll_interval_s=0.02)
    try:
        manager.terminate("alpha", "b" * 32)
        assert settings.runtime_update_operation_by_key(
            update_key)["status"] == "terminated"

        started = manager.start("alpha", "c" * 32)
        assert started["active"] is True
        running = _wait_status(
            manager, "alpha",
            lambda value: (value["owner_state"] == "running"
                           and value["applied_runtime_profile_revision"]
                           == latest["revision"]))
        assert running["active"] is True
        assert len(calls) == 1
        argv = calls[0]
        assert argv[argv.index("--runtime-profile-revision") + 1] == (
            str(latest["revision"]))

        replay = manager.schedule_runtime_profile_restart(
            "alpha", update_key)
        assert replay["runtime_profile_restart"] == "not_required"
        assert len(calls) == 1
        assert settings.runtime_update_operation_by_key(
            update_key)["status"] == "terminated"
        manager.terminate("alpha", "d" * 32)
    finally:
        manager.close()


def test_reconstructed_manager_resumes_bound_profile_then_switches_latest(
        tmp_path, monkeypatch):
    registry = _registry(tmp_path, "alpha")
    quest = registry.get("alpha")
    settings = QuestRuntimeSettings(quest.work_root, quest.quest_id)
    old = settings.initialize({
        "version": 1, "compute_profile_id": "local-gpu",
        "review_intensity": "once",
    }, "1" * 32)
    settings.bind_cycle_profile(old)
    latest = settings.update({
        "version": 1, "compute_profile_id": "local-cpu",
        "review_intensity": "off",
    }, "2" * 32)
    calls = []
    real_popen = process_manager_module.subprocess.Popen

    def recording_popen(argv, **kwargs):
        calls.append(list(argv))
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(process_manager_module.subprocess, "Popen", recording_popen)
    manager = QuestProcessManager(
        registry, _bound_recovery_system_root(tmp_path),
        poll_interval_s=0.02)
    try:
        manager.start("alpha", "a" * 32)
        slot = manager._slot("alpha", require_open=False)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            status = manager.status("alpha")
            if (status["owner_state"] == "running"
                    and status["applied_runtime_profile_revision"]
                    == latest["revision"]):
                break
            time.sleep(0.03)
        else:
            raise AssertionError("bound old-profile recovery 未切换到 latest")
        revisions = [
            int(argv[argv.index("--runtime-profile-revision") + 1])
            for argv in calls]
        assert revisions == [old["revision"], old["revision"], latest["revision"]]
        assert settings.bound_cycle_profile() is None
        with slot.lock:
            assert slot.bound_profile_recovery_attempts == 0
            assert slot.runtime_restart_requested is False
        body = (quest.work_root / "state" / "web-owner.log").read_text(
            encoding="utf-8")
        assert body.count("FAKE_BOUND_CRASH") == 1
        assert body.count("FAKE_BOUND_CLEAR") == 1
        manager.terminate("alpha", "b" * 32)
    finally:
        manager.close()


def test_bound_profile_recovery_is_bounded_and_preserves_marker(
        tmp_path, monkeypatch):
    registry = _registry(tmp_path, "alpha")
    quest = registry.get("alpha")
    settings = QuestRuntimeSettings(quest.work_root, quest.quest_id)
    old = settings.initialize({
        "version": 1, "compute_profile_id": "local-gpu",
        "review_intensity": "once",
    }, "1" * 32)
    settings.bind_cycle_profile(old)
    settings.update({
        "version": 1, "compute_profile_id": "local-cpu",
        "review_intensity": "off",
    }, "2" * 32)
    calls = []
    real_popen = process_manager_module.subprocess.Popen

    def recording_popen(argv, **kwargs):
        calls.append(list(argv))
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(process_manager_module.subprocess, "Popen", recording_popen)
    manager = QuestProcessManager(
        registry,
        _bound_recovery_system_root(tmp_path, always_crash_bound=True),
        poll_interval_s=0.02)
    try:
        manager.start("alpha", "a" * 32)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            status = manager.status("alpha")
            if status["runtime_profile_restart_error"] == (
                    "RuntimeBindingRecoveryExhausted"):
                break
            time.sleep(0.03)
        else:
            raise AssertionError("bound recovery 未在有界次数后 fail closed")
        assert status["active"] is False
        assert status["runtime_profile_restart_pending"] is False
        assert settings.bound_cycle_profile()["revision"] == old["revision"]
        assert len(calls) == 2
        time.sleep(0.25)
        assert len(calls) == 2, "recovery exhausted 后不得形成 Popen storm"
    finally:
        manager.close()


def test_spawn_post_read_closes_runtime_profile_update_race(
        tmp_path, monkeypatch):
    registry = _registry(tmp_path, "alpha")
    quest = registry.get("alpha")
    settings = QuestRuntimeSettings(quest.work_root, quest.quest_id)
    first = settings.initialize({
        "version": 1, "compute_profile_id": "local-gpu",
        "review_intensity": "once",
    }, "1" * 32)
    calls = []
    real_popen = process_manager_module.subprocess.Popen

    def recording_popen(argv, **kwargs):
        calls.append(list(argv))
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(process_manager_module.subprocess, "Popen", recording_popen)
    manager = QuestProcessManager(
        registry, _fake_system_root(tmp_path, profile_poll_s=0.05),
        poll_interval_s=0.02)
    real_spawn = manager._spawn_managed_child
    raced = {"done": False, "latest": None}

    def racing_spawn(
            quest_arg, *, start_key, runtime_profile,
            owner_intent_revision):
        child = real_spawn(
            quest_arg, start_key=start_key,
            runtime_profile=runtime_profile,
            owner_intent_revision=owner_intent_revision)
        if not raced["done"]:
            raced["done"] = True
            raced["latest"] = settings.update({
                "version": 1, "compute_profile_id": "local-cpu",
                "review_intensity": "off",
            }, "2" * 32)
        return child

    monkeypatch.setattr(manager, "_spawn_managed_child", racing_spawn)
    try:
        manager.start("alpha", "a" * 32)
        latest = raced["latest"]
        assert latest is not None and latest["revision"] != first["revision"]
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            status = manager.status("alpha")
            if (status["owner_state"] == "running"
                    and status["applied_runtime_profile_revision"]
                    == latest["revision"]):
                break
            time.sleep(0.03)
        else:
            raise AssertionError("snapshot->spawn update 未自动收敛到 latest")
        assert len(calls) == 2
        assert manager.status("alpha")["runtime_profile_restart_pending"] is False
        manager.terminate("alpha", "b" * 32)
    finally:
        manager.close()


def test_same_update_key_retries_failed_restart_side_effect(
        tmp_path, monkeypatch):
    registry = _registry(tmp_path, "alpha")
    quest = registry.get("alpha")
    settings = QuestRuntimeSettings(quest.work_root, quest.quest_id)
    settings.initialize({
        "version": 1, "compute_profile_id": "local-gpu",
        "review_intensity": "once",
    }, "1" * 32)
    manager = QuestProcessManager(
        registry, _fake_system_root(tmp_path, profile_poll_s=0.05),
        poll_interval_s=0.02)
    try:
        manager.start("alpha", "a" * 32)
        _wait_status(manager, "alpha", lambda value: value["owner_state"] == "running")
        latest = settings.update({
            "version": 1, "compute_profile_id": "local-cpu",
            "review_intensity": "off",
        }, "2" * 32)
        real_spawn = manager._spawn_managed_child
        fail = {"once": True}

        def fail_latest_once(
                quest_arg, *, start_key, runtime_profile,
                owner_intent_revision):
            if (runtime_profile["revision"] == latest["revision"]
                    and fail["once"]):
                fail["once"] = False
                raise OSError("injected replacement spawn failure")
            return real_spawn(
                quest_arg, start_key=start_key,
                runtime_profile=runtime_profile,
                owner_intent_revision=owner_intent_revision)

        monkeypatch.setattr(manager, "_spawn_managed_child", fail_latest_once)
        key = "c" * 32
        assert manager.schedule_runtime_profile_restart(
            "alpha", key)["runtime_profile_restart"] == "scheduled"
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            failed = manager.status("alpha")
            if failed["runtime_profile_restart_error"] == "OSError":
                break
            time.sleep(0.03)
        else:
            raise AssertionError("injected restart failure 未被记录")
        assert failed["active"] is False

        retried = manager.schedule_runtime_profile_restart("alpha", key)
        assert retried["runtime_profile_restart"] == "scheduled"
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            recovered = manager.status("alpha")
            if (recovered["owner_state"] == "running"
                    and recovered["applied_runtime_profile_revision"]
                    == latest["revision"]):
                break
            time.sleep(0.03)
        else:
            raise AssertionError("同 update key 未恢复未完成 restart side effect")
        assert recovered["runtime_profile_restart_error"] is None
        manager.terminate("alpha", "d" * 32)
    finally:
        manager.close()


def test_restarted_manager_never_signals_owner_known_only_from_disk(tmp_path):
    registry = _registry(tmp_path, "alpha")
    fake_root = _fake_system_root(tmp_path)
    first = QuestProcessManager(registry, fake_root, poll_interval_s=0.02)
    second = QuestProcessManager(registry, fake_root, poll_interval_s=0.02)
    try:
        first.start("alpha", "a" * 32)
        _wait_status(first, "alpha", lambda value: value["owner_state"] == "running")
        observed = second.status("alpha")
        assert observed["state"] == "external_active"
        assert observed["terminable"] is False
        denied = second.terminate("alpha", "b" * 32)
        assert denied["state"] == "external_active"
        assert denied["active"] is True
        assert denied["managed_by_web"] is False
        assert denied["terminable"] is False
        assert first.status("alpha")["active"] is True
        second.close()
        assert first.status("alpha")["active"] is True
        first.terminate("alpha", "c" * 32)
    finally:
        second.close()
        first.close()


def test_qualification_uid_mismatch_fails_without_root_fallback_or_spawn(
        tmp_path, monkeypatch):
    research_uid = 65534 if os.geteuid() != 65534 else 65533
    contract = {"task": "T1", "research_uid": research_uid}

    def fake_install(work_root, value):
        raw = _canonical(dict(value))
        path = Path(work_root) / CONTRACT_RELATIVE_PATH
        path.parent.mkdir(parents=True, mode=0o700)
        path.write_bytes(raw)
        path.chmod(0o400)
        return SimpleNamespace(
            task="T1",
            contract_sha256="sha256:" + hashlib.sha256(raw).hexdigest())

    monkeypatch.setattr(quest_registry_module, "install_contract", fake_install)
    registry = QuestRegistry(tmp_path / "registry", SYSTEM_ROOT)
    quest = registry.create(
        quest_id="qualified", title="Qualified",
        goal_brief_md=_brief("qualified"),
        qualification_profile_id="t1-local",
        qualification_contract=contract)
    manager = QuestProcessManager(registry, _fake_system_root(tmp_path))
    try:
        with pytest.raises(QuestProcessUnavailableError, match="research_uid") as error:
            manager.start("qualified", "a" * 32)
        assert str(quest.work_root) not in str(error.value)
        assert manager.status("qualified")["state"] == "inactive"
        assert not (quest.work_root / "state" / "web-owner.log").exists()
    finally:
        manager.close()


def test_terminate_escalates_to_sigkill_for_owned_group_only(
        tmp_path, monkeypatch):
    registry = _registry(tmp_path, "alpha")
    fake_root = _fake_system_root(tmp_path, ignore_term=True)
    monkeypatch.setattr(process_manager_module, "_TERMINATE_TIMEOUT_S", 0.15)
    monkeypatch.setattr(process_manager_module, "_KILL_TIMEOUT_S", 2.0)
    manager = QuestProcessManager(registry, fake_root, poll_interval_s=0.02)
    try:
        manager.start("alpha", "a" * 32)
        _wait_status(manager, "alpha", lambda value: value["owner_state"] == "running")
        stopped = manager.terminate("alpha", "b" * 32)
        assert stopped["state"] == "stopped"
        assert stopped["exit_code"] == -signal.SIGKILL
        deadline = time.monotonic() + 3
        while (read_instance_status(registry.get("alpha").work_root)["lock_held"]
               and time.monotonic() < deadline):
            time.sleep(0.03)
        assert read_instance_status(
            registry.get("alpha").work_root)["lock_held"] is False
    finally:
        manager.close()


def test_close_terminates_all_owned_children_and_rejects_new_mutations(tmp_path):
    registry = _registry(tmp_path, "alpha", "beta")
    manager = QuestProcessManager(
        registry, _fake_system_root(tmp_path), poll_interval_s=0.02)
    manager.start("alpha", "a" * 32)
    manager.start("beta", "b" * 32)
    _wait_status(manager, "alpha", lambda value: value["owner_state"] == "running")
    _wait_status(manager, "beta", lambda value: value["owner_state"] == "running")

    manager.close()
    manager.close()
    assert read_instance_status(registry.get("alpha").work_root)["lock_held"] is False
    assert read_instance_status(registry.get("beta").work_root)["lock_held"] is False
    with pytest.raises(QuestProcessManagerClosedError):
        manager.start("alpha", "c" * 32)
    with pytest.raises(QuestProcessManagerClosedError):
        manager.terminate("alpha", "d" * 32)
    assert manager.status("alpha")["state"] == "stopped"


@pytest.mark.parametrize("bad_key", ["", "A" * 32, "a" * 31, "g" * 32, None])
def test_mutations_require_canonical_idempotency_key(tmp_path, bad_key):
    registry = _registry(tmp_path, "alpha")
    manager = QuestProcessManager(registry, _fake_system_root(tmp_path))
    try:
        with pytest.raises(ValueError, match="idempotency_key"):
            manager.start("alpha", bad_key)
        with pytest.raises(ValueError, match="idempotency_key"):
            manager.terminate("alpha", bad_key)
    finally:
        manager.close()
