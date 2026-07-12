from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from orchestrator.deployment_preflight import (
    DeploymentPreflight,
    DeploymentPreflightError,
    longest_mount,
)
from orchestrator.execution_sandbox import (
    gpu_capability_projection,
    sandbox_environment_hash,
)


NOW = 1_800_000_000.0
MACHINE_ID = "0123456789abcdef0123456789abcdef"
BOOT_ID = "01234567-89ab-cdef-0123-456789abcdef"


def _policy(mode="production", attestation_path=None, *, gpus=0, disk_gb=1):
    return {
        "deployment": {
            "mode": mode,
            "attestation_path": (str(attestation_path)
                                 if attestation_path is not None else None),
            "max_attestation_age_s": 300,
        },
        "resources": {
            "gpus": gpus, "gpu_mem_gb": 80 if gpus else 0,
            "disk_quota_gb": disk_gb,
        },
    }


def _sandbox():
    return {
        "engine_path": "/usr/bin/docker",
        "engine_host": "unix:///run/meta-research/docker.sock",
        "resource_mode": "cgroup-v2",
        "max_output_mb": 1024,
        "max_output_files": 100,
    }


def _facts(work: Path):
    return {
        "service": {
            "uid": 1234, "gid": 2345, "groups": [2345],
            "username": "meta-research", "codex_home": "/srv/meta-research/codex",
            "codex_home_stat": {
                "path": "/srv/meta-research/codex", "direct": True,
                "is_directory": True, "uid": 1234, "gid": 2345, "mode": 0o700,
                "auth": {
                    "path": "/srv/meta-research/codex/auth.json", "direct": True,
                    "is_regular": True, "nlink": 1,
                    "uid": 1234, "gid": 2345, "mode": 0o600,
                },
            },
            "error": None,
        },
        "isolation": {
            "machine_id": MACHINE_ID, "boot_id": BOOT_ID,
            "hostname": "mr-vm-01", "vm_kind": "kvm", "container_kind": None,
            "error": None,
        },
        "work_root": {
            "path": str(work), "uid": 1234, "gid": 2345, "mode": 0o700,
            "is_directory": True, "direct": True, "dev": 10, "ino": 20,
            "mount": {
                "mount_id": "42", "parent_id": "1", "major_minor": "0:42",
                "root": "/tenant", "mount_point": "/vepfs", "mount_options": ["rw"],
                "optional_fields": [], "fstype": "gpfs", "source": "fs_prod",
                "super_options": ["rw"],
            },
            "error": None,
        },
        "docker": {
            "engine_path": "/usr/bin/docker",
            "engine_host": "unix:///run/meta-research/docker.sock",
            "socket": {
                "path": "/run/meta-research/docker.sock", "direct": True,
                "is_socket": True, "uid": 1234, "gid": 2345, "mode": 0o600,
                "dev": 30, "ino": 40,
                "realpath": "/run/meta-research/docker.sock",
            },
            "daemon": {
                "id": "daemon-prod", "name": "mr-vm-01", "rootless": True,
                "security_options": ["name=rootless", "name=seccomp,profile=builtin"],
                "cgroup_version": "2", "cgroup_driver": "systemd",
                "resource_mode": "cgroup-v2", "root_dir": "/var/lib/mr-docker",
                "runtimes": ["nvidia", "runc"],
                "limits": {"memory": True, "cpu": True, "pids": True},
            },
            "storage": {"free_bytes": 20 * 1024 ** 3, "free_inodes": 200_000},
            "error": None,
        },
        "gpu": {"driver_version": "535.129.03", "inventory": [], "error": None},
    }


def _attestation(*, gpu=None):
    return {
        "version": 2, "protocol": "deployment-attestation-v2",
        "issued_at_unix": NOW - 10,
        "service": {
            "uid": 1234, "gid": 2345, "groups": [2345],
            "username": "meta-research", "codex_home": "/srv/meta-research/codex",
        },
        "isolation": {
            "kind": "dedicated-vm", "deployment_id": "mr-vm-01",
            "machine_id": MACHINE_ID, "boot_id": BOOT_ID,
            "hostname": "mr-vm-01", "vm_kind": "kvm",
        },
        "docker": {
            "socket_path": "/run/meta-research/docker.sock",
            "socket_uid": 1234, "socket_gid": 2345, "socket_mode": 0o600,
            "daemon_id": "daemon-prod", "daemon_name": "mr-vm-01",
            "rootless": True,
            "security_options": ["name=rootless", "name=seccomp,profile=builtin"],
            "cgroup_version": "2", "cgroup_driver": "systemd",
            "resource_mode": "cgroup-v2", "root_dir": "/var/lib/mr-docker",
            "runtimes": ["nvidia", "runc"], "min_free_bytes": 10 * 1024 ** 3,
            "min_free_inodes": 100_000,
        },
        "work_root": {
            "path": "/placeholder", "mount_point": "/vepfs", "mount_source": "fs_prod",
            "mount_fstype": "gpfs", "quota_provider": "gpfs-fileset-v1",
            "quota_scope": "fileset:meta-research", "hard_bytes": 2 * 1024 ** 3,
            "used_bytes": 0, "hard_inodes": 1000, "used_inodes": 0,
        },
        "gpu": {"memory_bytes_by_uuid": dict(gpu or {})},
    }


def _write_attestation(path: Path, value, *, canonical=True, mode=0o644):
    value = copy.deepcopy(value)
    if value.get("work_root", {}).get("path") == "/placeholder":
        value["work_root"]["path"] = str(path.parent / "work")
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    path.write_text(encoded + ("\n" if canonical else ""), encoding="utf-8")
    path.chmod(mode)


class FakeProbe:
    def __init__(self, facts):
        self.facts = facts
        self.calls = 0

    def collect(self, **_kwargs):
        self.calls += 1
        return copy.deepcopy(self.facts)


def _run(tmp_path, policy, facts, *, owner="owner-test"):
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    probe = facts if isinstance(facts, FakeProbe) else FakeProbe(facts)
    preflight = DeploymentPreflight(
        work, policy, _sandbox(), owner,
        probe_backend=probe, clock=lambda: NOW)
    return preflight, probe


def _hash(value):
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")) + "\n").encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _gpu_evidence(contract, candidate_hash):
    runtime_config = {
        **_sandbox(), "gpu_capability": gpu_capability_projection(contract)}
    environment_hash = sandbox_environment_hash(runtime_config)
    runtime_identity_hash = _hash({
        "environment_hash": environment_hash, "gpu_contract": contract})
    return {
        "version": 1, "protocol": "sandbox-gpu-canary-v1", "ok": True,
        "checked_at_unix": NOW,
        "candidate_hash": candidate_hash,
        "contract_hash": _hash(contract),
        "environment_hash": environment_hash,
        "runtime_identity_hash": runtime_identity_hash,
        "inventory": contract["devices"],
        "guardian": {
            "operation_id": "exec-" + "5" * 32,
            "state": "terminal", "outcome": "exit", "returncode": 0,
            "group_drained": True, "containment": "docker-container-v1",
            "context": {
                "phase": "deployment-gpu-canary",
                "candidate_hash": candidate_hash,
                "runtime_identity_hash": runtime_identity_hash,
                "log_name": "gpu-canary.log",
            },
            "sandbox": {
                "backend": "docker-container-v1",
                "spec_sha256": "sha256:" + "4" * 64,
                "container_drained": True,
            },
        },
        "execution": {
            "log_path": "/work/gpu-canary.log",
            "log_sha256": "sha256:" + "3" * 64,
            "process_receipt_path": "/work/execution.json",
            "operation_id": "exec-" + "5" * 32,
            "sandbox_spec_sha256": "sha256:" + "4" * 64,
        },
        "error": None,
    }


def test_development_always_writes_private_nonproduction_receipt(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    facts = _facts(work)
    facts["docker"]["error"] = "daemon unavailable"
    preflight = DeploymentPreflight(
        work, _policy("development"), _sandbox(), "owner-dev",
        probe_backend=FakeProbe(facts), clock=lambda: NOW)
    receipt = preflight.run()
    assert receipt["production_ready"] is False
    assert receipt["mode"] == "development"
    assert receipt["prerequisite"]["facts"]["docker"]["error"] == "daemon unavailable"
    raw = preflight.receipt_path.read_bytes()
    assert stat.S_IMODE(preflight.receipt_path.stat().st_mode) == 0o600
    assert raw == (json.dumps(receipt, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":")) + "\n").encode()


def test_prerequisite_candidate_is_deep_frozen_and_replayable(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    preflight = DeploymentPreflight(
        work, _policy("development"), _sandbox(), "owner-frozen",
        probe_backend=FakeProbe(_facts(work)), clock=lambda: NOW)
    returned = preflight.prepare()
    original_uid = returned["facts"]["service"]["uid"]
    returned["facts"]["service"]["uid"] = 999999
    returned["checks"][0]["ok"] = not returned["checks"][0]["ok"]
    receipt = preflight.finalize(None)
    prerequisite = receipt["prerequisite"]
    assert prerequisite["facts"]["service"]["uid"] == original_uid
    projection = {
        key: value for key, value in prerequisite.items() if key != "candidate_hash"}
    assert receipt["candidate_hash"] == prerequisite["candidate_hash"] == _hash(projection)


@pytest.mark.skipif(os.geteuid() != 0, reason="root-owned attestation profile test")
def test_production_positive_with_zero_requested_gpus(tmp_path):
    attestation = tmp_path / "deployment.json"
    _write_attestation(attestation, _attestation())
    work = tmp_path / "work"
    preflight, probe = _run(
        tmp_path, _policy(attestation_path=attestation), _facts(work))
    receipt = preflight.run()
    assert receipt["production_ready"] is True
    assert all(item["ok"] for item in receipt["checks"])
    assert receipt["prerequisite"]["attestation"]["sha256"].startswith("sha256:")
    assert probe.calls == 1


@pytest.mark.skipif(os.geteuid() != 0, reason="root-owned attestation profile test")
@pytest.mark.parametrize(
    "failure",
    ["root", "cgroup", "quota", "space", "rootful", "provider",
     "container", "codex_home", "socket_parent", "reserve"],
)
def test_production_rejects_core_trust_failures(tmp_path, failure):
    value = _attestation()
    facts = _facts(tmp_path / "work")
    policy = _policy(attestation_path=tmp_path / "deployment.json")
    if failure == "root":
        facts["service"]["uid"] = 0
        value["service"]["uid"] = 0
        facts["work_root"]["uid"] = 0
    elif failure == "cgroup":
        facts["docker"]["daemon"]["resource_mode"] = "rlimit-fallback"
        facts["docker"]["daemon"]["cgroup_driver"] = "none"
        facts["docker"]["daemon"]["limits"] = {
            "memory": False, "cpu": False, "pids": False}
    elif failure == "quota":
        value["work_root"]["hard_bytes"] = 1024
    elif failure == "space":
        facts["docker"]["storage"]["free_bytes"] = 1024
    elif failure == "rootful":
        facts["docker"]["daemon"]["rootless"] = False
        value["docker"]["rootless"] = False
    elif failure == "provider":
        value["work_root"]["quota_provider"] = "df-statvfs"
    elif failure == "container":
        facts["isolation"]["container_kind"] = "docker"
    elif failure == "codex_home":
        facts["service"]["codex_home_stat"]["mode"] = 0o755
    elif failure == "socket_parent":
        facts["docker"]["socket"]["direct"] = False
        facts["docker"]["socket"]["realpath"] = "/other/docker.sock"
    else:
        value["docker"]["min_free_bytes"] = 1
    _write_attestation(tmp_path / "deployment.json", value)
    preflight, _probe = _run(tmp_path, policy, facts)
    with pytest.raises(DeploymentPreflightError) as caught:
        preflight.run()
    assert caught.value.receipt["production_ready"] is False
    persisted = json.loads(preflight.receipt_path.read_text())
    assert persisted["production_ready"] is False


@pytest.mark.skipif(os.geteuid() != 0, reason="root-owned attestation profile test")
def test_requested_gpu_requires_guardian_canary_then_becomes_ready(tmp_path, monkeypatch):
    memory = 80 * 1024 ** 3
    value = _attestation(gpu={"GPU-test-0001": memory})
    facts = _facts(tmp_path / "work")
    facts["gpu"]["inventory"] = [
        {"index": 0, "uuid": "GPU-test-0001", "model": "NVIDIA A100",
         "memory_bytes": memory, "compute_capability": "8.0"},
        {"index": 1, "uuid": "GPU-unallocated", "model": "NVIDIA A100",
         "memory_bytes": memory, "compute_capability": "8.0"},
    ]
    path = tmp_path / "deployment.json"
    _write_attestation(path, value)
    preflight, _probe = _run(
        tmp_path, _policy(attestation_path=path, gpus=1), facts)
    candidate = preflight.prepare()
    assert [item["uuid"] for item in candidate["gpu_contract"]["devices"]] == [
        "GPU-test-0001"]
    with pytest.raises(DeploymentPreflightError, match="sandbox_gpu_access"):
        preflight.finalize(None)

    # A fresh two-phase attempt accepts only evidence bound to that exact map.
    preflight, _probe = _run(
        tmp_path, _policy(attestation_path=path, gpus=1), facts,
        owner="owner-gpu-ready")
    candidate = preflight.prepare()
    # The durable log/guardian authority replay is covered separately; this
    # unit isolates the pure final evaluator with an already-validated result.
    monkeypatch.setattr(
        preflight, "_validate_gpu_evidence_authority",
        lambda _evidence, _candidate: None)
    receipt = preflight.finalize(_gpu_evidence(
        candidate["gpu_contract"], candidate["candidate_hash"]))
    assert receipt["production_ready"] is True
    assert receipt["phase"] == "final"
    assert receipt["gpu_canary"]["contract_hash"] == candidate["gpu_contract_hash"]


@pytest.mark.skipif(os.geteuid() != 0, reason="root-owned attestation profile test")
def test_gpu_attestation_is_exact_service_allocation_not_host_inventory(tmp_path):
    memory = 80 * 1024 ** 3
    path = tmp_path / "deployment.json"
    _write_attestation(path, _attestation(gpu={
        "GPU-a": memory, "GPU-c": memory,
    }))
    facts = _facts(tmp_path / "work")
    facts["gpu"]["inventory"] = [
        {"index": index, "uuid": uuid, "model": "NVIDIA A100",
         "memory_bytes": memory, "compute_capability": "8.0"}
        for index, uuid in enumerate(("GPU-a", "GPU-b", "GPU-c", "GPU-d"))
    ]
    preflight, _probe = _run(
        tmp_path, _policy(attestation_path=path, gpus=2), facts)
    candidate = preflight.prepare()
    assert [item["uuid"] for item in candidate["gpu_contract"]["devices"]] == [
        "GPU-a", "GPU-c"]


@pytest.mark.skipif(os.geteuid() != 0, reason="root-owned attestation profile test")
def test_gpu_attestation_count_must_equal_policy_request(tmp_path):
    memory = 80 * 1024 ** 3
    path = tmp_path / "deployment.json"
    _write_attestation(path, _attestation(gpu={"GPU-a": memory, "GPU-b": memory}))
    facts = _facts(tmp_path / "work")
    facts["gpu"]["inventory"] = [
        {"index": index, "uuid": uuid, "model": "NVIDIA A100",
         "memory_bytes": memory, "compute_capability": "8.0"}
        for index, uuid in enumerate(("GPU-a", "GPU-b"))
    ]
    preflight, _probe = _run(
        tmp_path, _policy(attestation_path=path, gpus=1), facts)
    with pytest.raises(DeploymentPreflightError) as caught:
        preflight.prepare()
    assert "gpu_inventory" in str(caught.value)


@pytest.mark.skipif(os.geteuid() != 0, reason="root-owned attestation profile test")
def test_gpu_canary_evidence_must_bind_guardian_spec_and_candidate(tmp_path):
    memory = 80 * 1024 ** 3
    path = tmp_path / "deployment.json"
    _write_attestation(path, _attestation(gpu={"GPU-a": memory}))
    facts = _facts(tmp_path / "work")
    facts["gpu"]["inventory"] = [{
        "index": 0, "uuid": "GPU-a", "model": "NVIDIA A100",
        "memory_bytes": memory, "compute_capability": "8.0",
    }]
    preflight, _probe = _run(
        tmp_path, _policy(attestation_path=path, gpus=1), facts,
        owner="owner-gpu-forged")
    candidate = preflight.prepare()
    evidence = _gpu_evidence(
        candidate["gpu_contract"], candidate["candidate_hash"])
    evidence["guardian"]["sandbox"]["spec_sha256"] = "sha256:" + "9" * 64
    with pytest.raises(DeploymentPreflightError, match="sandbox_gpu_access") as caught:
        preflight.finalize(evidence)
    assert caught.value.receipt["gpu_canary"] is None
    assert caught.value.receipt["gpu_canary_validation_error"]


@pytest.mark.skipif(os.geteuid() != 0, reason="root-owned attestation profile test")
def test_v1_attestation_is_rejected_after_exact_allocation_protocol_upgrade(tmp_path):
    path = tmp_path / "deployment.json"
    value = _attestation()
    value.update({"version": 1, "protocol": "deployment-attestation-v1"})
    _write_attestation(path, value)
    preflight, _probe = _run(
        tmp_path, _policy(attestation_path=path), _facts(tmp_path / "work"))
    with pytest.raises(DeploymentPreflightError) as caught:
        preflight.prepare()
    assert "attestation_loaded" in str(caught.value)


@pytest.mark.skipif(os.geteuid() != 0, reason="root-owned attestation profile test")
@pytest.mark.parametrize(
    "drift", [None, "owner", "kind", "fence", "engine", "spec", "log_hash"])
def test_gpu_canary_authority_replays_exact_log_and_guardian(
        tmp_path, monkeypatch, drift):
    import orchestrator.deployment_preflight as DP

    memory = 80 * 1024 ** 3
    path = tmp_path / "deployment.json"
    _write_attestation(path, _attestation(gpu={"GPU-a": memory}))
    facts = _facts(tmp_path / "work")
    facts["gpu"]["inventory"] = [{
        "index": 0, "uuid": "GPU-a", "model": "NVIDIA A100",
        "memory_bytes": memory, "compute_capability": "8.0",
    }]
    preflight, _probe = _run(
        tmp_path, _policy(attestation_path=path, gpus=1), facts,
        owner="owner-gpu-authority")
    candidate = preflight.prepare()
    evidence = _gpu_evidence(
        candidate["gpu_contract"], candidate["candidate_hash"])
    token = candidate["candidate_hash"].removeprefix("sha256:")[:32]
    log_path = (tmp_path / "work" / "state" / "deployment" / "gpu-canary"
                / token / "gpu-canary.log")
    log_path.parent.mkdir(parents=True)
    raw = b"GPU-a, NVIDIA A100, 81920, 8.0, 535.129.03\n"
    log_path.write_bytes(raw)
    operation_id = evidence["execution"]["operation_id"]
    receipt_path = (tmp_path / "work" / "state" / "executions"
                    / f"execution-{operation_id}.json")
    evidence["execution"].update({
        "log_path": str(log_path),
        "log_sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "process_receipt_path": str(receipt_path),
    })
    guardian = evidence["guardian"]
    receipt = {
        "owner_id": "owner-gpu-authority", "kind": "deployment-gpu-canary",
        "fenced_by_instance_lease": True,
        "operation_id": operation_id, "state": "terminal", "outcome": "exit",
        "returncode": 0, "group_drained": True,
        "containment": "docker-container-v1", "context": guardian["context"],
        "sandbox": {
            "backend": "docker-container-v1",
            "engine_path": os.path.realpath("/usr/bin/docker"),
            "engine_host": _sandbox()["engine_host"],
            "resource_mode": _sandbox()["resource_mode"],
            "spec_sha256": guardian["sandbox"]["spec_sha256"],
            "container_drained": True,
        },
    }
    if drift == "owner":
        receipt["owner_id"] = "other-owner"
    elif drift == "kind":
        receipt["kind"] = "other-kind"
    elif drift == "fence":
        receipt["fenced_by_instance_lease"] = False
    elif drift == "engine":
        receipt["sandbox"]["engine_path"] = "/usr/bin/true"
    elif drift == "spec":
        receipt["sandbox"]["spec_sha256"] = "sha256:" + "9" * 64
    elif drift == "log_hash":
        evidence["execution"]["log_sha256"] = "sha256:" + "8" * 64
    monkeypatch.setattr(DP, "read_receipt", lambda checked: (
        receipt if checked == receipt_path else pytest.fail("wrong receipt path")))
    monkeypatch.setattr(DP, "validate_execution_receipt", lambda _r, _p: None)
    if drift is None:
        preflight._validate_gpu_evidence_authority(evidence, candidate)
    else:
        with pytest.raises(ValueError):
            preflight._validate_gpu_evidence_authority(evidence, candidate)


@pytest.mark.skipif(os.geteuid() != 0, reason="root-owned attestation profile test")
@pytest.mark.parametrize("case", ["noncanonical", "writable"])
def test_attestation_canonical_and_permission_profile_is_fail_closed(tmp_path, case):
    path = tmp_path / "deployment.json"
    _write_attestation(
        path, _attestation(), canonical=case != "noncanonical",
        mode=0o666 if case == "writable" else 0o644)
    preflight, _probe = _run(
        tmp_path, _policy(attestation_path=path), _facts(tmp_path / "work"))
    with pytest.raises(DeploymentPreflightError) as caught:
        preflight.run()
    assert "attestation_loaded" in str(caught.value)
    assert caught.value.receipt["attestation"]["sha256"] is None


@pytest.mark.skipif(os.geteuid() != 0, reason="root-owned attestation profile test")
def test_reprobe_overwrites_stale_success_after_attestation_tamper(tmp_path):
    path = tmp_path / "deployment.json"
    value = _attestation()
    _write_attestation(path, value)
    probe = FakeProbe(_facts(tmp_path / "work"))
    preflight, _ = _run(
        tmp_path, _policy(attestation_path=path), probe, owner="owner-reprobe")
    success = preflight.run()
    old_hash = success["prerequisite"]["attestation"]["sha256"]

    value["service"]["username"] = "tampered-service"
    _write_attestation(path, value)
    with pytest.raises(DeploymentPreflightError) as caught:
        preflight.run()
    assert probe.calls == 2
    assert caught.value.receipt["attestation"]["sha256"] != old_hash
    persisted = json.loads(preflight.receipt_path.read_text())
    assert persisted["production_ready"] is False


def test_mountinfo_uses_longest_containing_mount():
    text = (
        "1 0 0:1 / / rw - overlay overlay rw\n"
        "2 1 0:2 /tenant /vepfs rw shared:2 - gpfs fs_prod rw\n"
        "3 2 0:3 /tenant/job /vepfs/job rw - gpfs fs_job rw\n")
    result = longest_mount("/vepfs/job/work/run", text)
    assert result["mount_point"] == "/vepfs/job"
    assert result["source"] == "fs_job"
