"""CP11.4c.1 adversarial Docker boundary: exact image, fd snapshot, drain and output promotion."""
from __future__ import annotations

import json
import os
import socket
import subprocess
from pathlib import Path

import pytest
import yaml

from orchestrator.execution_sandbox import (
    _verify_created_container,
    _daemon_bind_source_candidates,
    DockerExecutionSandbox,
    ExecutionSandboxError,
    gpu_capability_projection,
    gpu_cli_argument,
    gpu_contract_hash,
    sandbox_environment_hash,
    sandbox_manifest_profile,
    sandbox_workload_environment_hash,
)
from orchestrator.harness import ExecutionRecoveryError, recover_staged_result, run_staged
from orchestrator.process_supervisor import (
    ExecutionSupervisor,
    SupervisedTimeoutExpired,
    atomic_write_receipt,
)


SYSTEM_ROOT = Path(__file__).resolve().parent.parent
POLICY = yaml.safe_load((SYSTEM_ROOT / "policies" / "policy.yaml").read_text(encoding="utf-8"))


def _gpu_contract(*uuids):
    return {
        "version": 1, "provider": "nvidia", "driver_version": "535.129.03",
        "request": {
            "driver": "nvidia",
            "capabilities": ["compute", "utility", "gpu"], "options": {},
        },
        "devices": [{
            "uuid": uuid, "model": "NVIDIA A100-SXM4-80GB",
            "memory_bytes": 80 * 1024 ** 3, "compute_capability": "8.0",
        } for uuid in uuids],
    }


@pytest.mark.parametrize("network_mode", ["none", "bridge"])
def test_sandbox_manifest_profile_projects_policy_network(network_mode):
    profile = sandbox_manifest_profile({"network_mode": network_mode})
    expected = {
        "network_mode": network_mode,
        "rootfs_readonly": True,
    }
    if network_mode == "bridge":
        expected["network_development_only"] = True
    assert profile == expected


def test_sandbox_manifest_profile_rejects_unknown_network():
    with pytest.raises(ValueError, match="network_mode"):
        sandbox_manifest_profile({"network_mode": "host"})


def _runtime(tmp_path):
    work = tmp_path / "work"
    (work / "state").mkdir(parents=True)
    sandbox = DockerExecutionSandbox(
        work_root=work, config=POLICY["execution"]["sandbox"])
    try:
        sandbox.preflight()
    except (ExecutionSandboxError, OSError, subprocess.SubprocessError) as error:
        pytest.skip(f"pinned local Docker sandbox unavailable: {error}")
    supervisor = ExecutionSupervisor.standalone(work / "state" / "executions")
    return work, sandbox, supervisor


def _run(work, sandbox, supervisor, command, *, name="probe.log", timeout=15,
         context=None, fd_expectations=()):
    base = dict(context or {
        "phase": "probe", "db_owner_kind": "build_target", "db_owner_id": 1})
    prepared_context = {**base, "log_name": name}
    invocation = sandbox.prepare(
        command, staging_dir=work / "run", log_name=name, env=None,
        timeout_s=timeout, fd_expectations=fd_expectations,
        execution_context=prepared_context)
    return run_staged(
        invocation.argv, staging_dir=str(work / "run"), log_name=name,
        timeout_s=timeout, env=invocation.env, pass_fds=invocation.pass_fds,
        execution_supervisor=supervisor, execution_kind="sandbox-probe",
        execution_context=base, sandbox_invocation=invocation)


def test_environment_identity_is_exact_policy_projection():
    value = sandbox_environment_hash(POLICY["execution"]["sandbox"])
    assert value.startswith("sha256:") and len(value) == 71
    changed = {**POLICY["execution"]["sandbox"], "memory_mb": 8192}
    assert sandbox_environment_hash(changed) != value
    changed = {
        **POLICY["execution"]["sandbox"],
        "development_gpu_thread_limit": 3,
    }
    assert sandbox_environment_hash(changed) != value


def test_development_local_environment_is_pinned_and_not_a_data_mount(tmp_path):
    config = POLICY["execution"]["sandbox"]
    local = config["local_environment"]
    assert local["source"] not in config["readonly_mounts"]
    work = tmp_path / "work"
    work.mkdir()
    sandbox = DockerExecutionSandbox(work_root=work, config=config)
    assert sandbox.local_environment_identity["identity_sha256"] == (
        local["identity_sha256"])
    assert sandbox.local_environment_identity["conda_package_count"] > 0

    drifted = json.loads(json.dumps(config))
    drifted["local_environment"]["identity_sha256"] = "sha256:" + "0" * 64
    with pytest.raises(ExecutionSandboxError, match="identity 漂移"):
        DockerExecutionSandbox(work_root=work, config=drifted)


def test_development_local_environment_injects_mapped_trusted_ca(tmp_path):
    work = tmp_path / "work"
    (work / "state").mkdir(parents=True)
    sandbox = DockerExecutionSandbox(
        work_root=work, config=POLICY["execution"]["sandbox"])
    sandbox._preflight_done = True
    sandbox._resource_mode = POLICY["execution"]["sandbox"]["resource_mode"]
    invocation = sandbox.prepare(
        [sandbox.config["python_path"], "-c", "pass"],
        staging_dir=work / "run", log_name="ca.log", env=None, timeout_s=10,
        execution_context={"phase": "ca-unit", "log_name": "ca.log"})
    try:
        invocation.spec_file.seek(0)
        spec = json.loads(invocation.spec_file.read())
        expected = "/opt/host-conda/ssl/cert.pem"
        for key in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
            assert spec["env"][key] == expected
            assert spec["payload_env"][key] == expected
            assert sandbox.local_environment["source"] not in spec["payload_env"][key]
        assert spec["payload_env"]["HOME"] == "/mr/output"
        assert spec["payload_env"]["XDG_CACHE_HOME"] == "/mr/output/.cache"
        assert spec["payload_env"]["PIP_CACHE_DIR"] == "/mr/output/.cache/pip"
        assert spec["payload_env"]["HF_HOME"] == "/mr/output/.cache/huggingface"
        assert spec["payload_env"]["TORCH_HOME"] == "/mr/output/.cache/torch"
        assert all("/root/" not in value for value in spec["payload_env"].values())
    finally:
        invocation.close()

    with pytest.raises(ExecutionSandboxError, match="trusted payload_environment"):
        sandbox.prepare(
            [sandbox.config["python_path"], "-c", "pass"],
            staging_dir=work / "override", log_name="override.log",
            env={"SSL_CERT_FILE": "/tmp/disable-verification.pem"}, timeout_s=10,
            execution_context={"phase": "ca-override", "log_name": "override.log"})

    invalid = json.loads(json.dumps(POLICY["execution"]["sandbox"]))
    invalid["payload_environment"]["REQUESTS_CA_BUNDLE"] = "/tmp/policy-ca.pem"
    with pytest.raises(ValueError, match="CA env"):
        DockerExecutionSandbox(work_root=work, config=invalid)


def test_python_requirements_are_bound_into_trusted_launcher_argv(tmp_path):
    work = tmp_path / "work"
    (work / "state").mkdir(parents=True)
    sandbox = DockerExecutionSandbox(
        work_root=work, config=POLICY["execution"]["sandbox"])
    sandbox._preflight_done = True
    sandbox._resource_mode = POLICY["execution"]["sandbox"]["resource_mode"]
    invocation = sandbox.prepare(
        [sandbox.config["python_path"], "-c", "print('ok')"],
        staging_dir=work / "run", log_name="deps.log", env=None, timeout_s=60,
        execution_context={"phase": "deps-unit", "log_name": "deps.log"},
        python_requirements=["einops==0.8.0", "mne>=1.6"])
    try:
        invocation.spec_file.seek(0)
        spec = json.loads(invocation.spec_file.read())
        assert json.loads(spec["argv"][12]) == ["einops==0.8.0", "mne>=1.6"]
        assert ".mr-python-deps" in spec["argv"][4]
        assert "pip','install'" in spec["argv"][4]
    finally:
        invocation.close()

    with pytest.raises(ValueError, match="python_requirements"):
        sandbox.prepare(
            ["python", "-c", "pass"], staging_dir=work / "bad",
            log_name="bad.log", env=None, timeout_s=10,
            execution_context={"phase": "deps-unit", "log_name": "bad.log"},
            python_requirements=["--index-url=https://example.invalid"])


def test_gpu_capability_hash_is_stable_but_allocation_identity_is_exact():
    first = _gpu_contract("GPU-b", "GPU-a")
    replacement = _gpu_contract("GPU-d", "GPU-c")
    assert gpu_capability_projection(first) == gpu_capability_projection(replacement)
    first_config = {
        **POLICY["execution"]["sandbox"],
        "gpu_capability": gpu_capability_projection(first),
    }
    replacement_config = {
        **POLICY["execution"]["sandbox"],
        "gpu_capability": gpu_capability_projection(replacement),
    }
    first_runtime = sandbox_environment_hash(first_config)
    replacement_runtime = sandbox_environment_hash(replacement_config)
    assert first_runtime == replacement_runtime
    assert sandbox_workload_environment_hash(first_runtime, False) == first_runtime
    assert sandbox_workload_environment_hash(
        first_runtime, True) == sandbox_workload_environment_hash(
            replacement_runtime, True)
    assert sandbox_workload_environment_hash(
        first_runtime, True) != sandbox_workload_environment_hash(
            first_runtime, False)
    with pytest.raises(ValueError, match="gpu_required"):
        sandbox_workload_environment_hash(first_runtime, 1)
    with pytest.raises(ValueError, match="runtime_environment_hash"):
        sandbox_workload_environment_hash("not-a-hash", False)
    assert gpu_contract_hash(first) != gpu_contract_hash(replacement)
    assert gpu_cli_argument(first) == (
        '"driver=nvidia","device=GPU-a,GPU-b",'
        '"capabilities=compute,utility"')


def test_gpu_prepare_binds_exact_request_and_inspect_rejects_drift(tmp_path):
    work = tmp_path / "work"
    (work / "state").mkdir(parents=True)
    contract = _gpu_contract("GPU-b", "GPU-a")
    config = {
        **POLICY["execution"]["sandbox"],
        "development_gpu_thread_limit": 4,
    }
    sandbox = DockerExecutionSandbox(
        work_root=work, config=config,
        gpu_contract=contract)
    sandbox._preflight_done = True
    sandbox._resource_mode = config["resource_mode"]
    context = {"phase": "gpu-unit", "log_name": "gpu.log"}
    invocation = sandbox.prepare(
        ["python", "-c", "pass"], staging_dir=work / "run",
        log_name="gpu.log", env=None, timeout_s=10,
        execution_context=context, gpu_required=True)
    try:
        invocation.spec_file.seek(0)
        spec = json.loads(invocation.spec_file.read())
        assert [item["uuid"] for item in spec["gpu"]["devices"]] == [
            "GPU-a", "GPU-b"]
        assert spec["env"]["NVIDIA_DRIVER_CAPABILITIES"] == "compute,utility"
        assert {
            key: spec["payload_env"][key]
            for key in ("MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS",
                        "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS")
        } == {
            "MKL_NUM_THREADS": "4", "NUMEXPR_NUM_THREADS": "4",
            "OMP_NUM_THREADS": "4", "OPENBLAS_NUM_THREADS": "4",
        }
        expected_request = [{
            "Driver": "nvidia", "Count": 0,
            "DeviceIDs": ["GPU-a", "GPU-b"],
            "Capabilities": [["compute", "utility", "gpu"]], "Options": {},
        }]
        host_mounts = [
            {"Type": "bind", "Source": spec["input_root"],
             "Target": "/mr/input", "ReadOnly": True},
            {"Type": "bind", "Source": spec["output_root"],
             "Target": "/mr/output", "ReadOnly": False},
            {"Type": "bind", "Source": spec["local_environment"]["source"],
             "Target": spec["local_environment"]["target"], "ReadOnly": True},
        ]
        payload = [{
            "Name": "/" + spec["name"], "Image": spec["image_id"],
            "Config": {
                "Labels": {"meta-research.sandbox-token": spec["token"]},
                "User": "65534:65534", "Entrypoint": None,
                "Cmd": spec["argv"], "WorkingDir": "/mr/output",
                "Env": [f"{key}={value}" for key, value in spec["env"].items()],
            },
            "HostConfig": {
                "SecurityOpt": ["no-new-privileges:true", "seccomp=/pinned.json"],
                "NetworkMode": spec["network_mode"], "ReadonlyRootfs": True,
                "Privileged": False, "IpcMode": "private", "PidMode": "",
                "CapDrop": ["ALL"], "Devices": [],
                "DeviceRequests": expected_request,
                "PidsLimit": spec["limits"]["pids"],
                "Memory": spec["limits"]["memory_bytes"],
                "MemorySwap": spec["limits"]["memory_bytes"],
                "NanoCpus": int(spec["limits"]["cpus"] * 1_000_000_000),
                "Tmpfs": {"/tmp": (
                    "rw,nosuid,nodev,noexec,size="
                    f"{spec['limits']['tmpfs_bytes']}")},
                "ShmSize": spec["limits"]["shm_bytes"],
                "LogConfig": {"Type": "json-file", "Config": {
                    "max-file": "2", "max-size":
                    f"{max(1, spec['limits']['max_log_bytes'] // 2)}b"}},
                "AutoRemove": False, "Mounts": host_mounts,
            },
            "Mounts": [
                {"Type": "bind", "Destination": "/mr/input", "RW": False},
                {"Type": "bind", "Destination": "/mr/output", "RW": True},
                {"Type": "bind",
                 "Destination": spec["local_environment"]["target"], "RW": False},
            ],
        }]
        _verify_created_container(spec, payload)
        payload[0]["HostConfig"]["IpcMode"] = "none"
        with pytest.raises(ExecutionSandboxError, match="安全/资源配置"):
            _verify_created_container(spec, payload)
        payload[0]["HostConfig"]["IpcMode"] = "private"
        payload[0]["HostConfig"]["DeviceRequests"][0]["DeviceIDs"].append("GPU-extra")
        with pytest.raises(ExecutionSandboxError, match="安全/资源配置"):
            _verify_created_container(spec, payload)
    finally:
        invocation.close()


def test_development_gpu_thread_limit_is_gpu_only_and_not_overridable(tmp_path):
    work = tmp_path / "work"
    (work / "state").mkdir(parents=True)
    config = {
        **POLICY["execution"]["sandbox"],
        "development_gpu_thread_limit": 4,
    }
    sandbox = DockerExecutionSandbox(
        work_root=work, config=config,
        gpu_contract=_gpu_contract("GPU-a"))
    sandbox._preflight_done = True
    sandbox._resource_mode = config["resource_mode"]
    thread_keys = {
        "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS",
        "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS",
    }
    cpu = sandbox.prepare(
        ["python", "-c", "pass"], staging_dir=work / "cpu",
        log_name="cpu.log", env=None, timeout_s=10,
        execution_context={"phase": "cpu", "log_name": "cpu.log"},
        gpu_required=False)
    try:
        cpu.spec_file.seek(0)
        cpu_spec = json.loads(cpu.spec_file.read())
        assert thread_keys.isdisjoint(cpu_spec["payload_env"])
    finally:
        cpu.close()
    with pytest.raises(ExecutionSandboxError, match="trusted payload_environment"):
        sandbox.prepare(
            ["python", "-c", "pass"], staging_dir=work / "gpu-override",
            log_name="gpu.log", env={"OPENBLAS_NUM_THREADS": "64"}, timeout_s=10,
            execution_context={"phase": "gpu", "log_name": "gpu.log"},
            gpu_required=True)

    invalid = json.loads(json.dumps(config))
    invalid["local_environment"] = None
    with pytest.raises(ValueError, match="local environment"):
        DockerExecutionSandbox(work_root=work, config=invalid)
    invalid = json.loads(json.dumps(config))
    invalid["pids"] = invalid["development_gpu_thread_limit"] + 7
    with pytest.raises(ValueError, match="保留足够 pids"):
        DockerExecutionSandbox(work_root=work, config=invalid)


def test_gpu_required_fails_before_session_creation_without_contract(tmp_path):
    work = tmp_path / "work"
    (work / "state").mkdir(parents=True)
    sandbox = DockerExecutionSandbox(
        work_root=work, config=POLICY["execution"]["sandbox"])
    with pytest.raises(ExecutionSandboxError, match="exact GPU allocation"):
        sandbox.prepare(
            ["python", "-c", "pass"], staging_dir=work / "run",
            log_name="gpu.log", env=None, timeout_s=10, gpu_required=True)
    assert not (work / "run").exists()


def test_cgroup_gpu_keeps_resident_memory_limit_without_cuda_rlimit_as_trap(tmp_path):
    work = tmp_path / "work"
    (work / "state").mkdir(parents=True)
    config = {**POLICY["execution"]["sandbox"], "resource_mode": "cgroup-v2"}
    sandbox = DockerExecutionSandbox(
        work_root=work, config=config, gpu_contract=_gpu_contract("GPU-a"))
    sandbox._preflight_done = True
    sandbox._resource_mode = "cgroup-v2"
    invocation = sandbox.prepare(
        ["python", "-c", "pass"], staging_dir=work / "run",
        log_name="gpu.log", env=None, timeout_s=10,
        execution_context={"phase": "gpu-cgroup", "log_name": "gpu.log"},
        gpu_required=True)
    try:
        invocation.spec_file.seek(0)
        spec = json.loads(invocation.spec_file.read())
        assert spec["limits"]["memory_bytes"] == config["memory_mb"] * 1024 ** 2
        assert spec["limits"]["address_space_bytes"] == -1
        assert spec["argv"][5] == "-1"
    finally:
        invocation.close()


def test_development_rlimit_fallback_does_not_cap_workload_address_space(
        tmp_path):
    work = tmp_path / "work"
    (work / "state").mkdir(parents=True)
    config = {**POLICY["execution"]["sandbox"], "resource_mode": "rlimit-fallback"}
    sandbox = DockerExecutionSandbox(
        work_root=work, config=config, gpu_contract=_gpu_contract("GPU-a"))
    sandbox._preflight_done = True
    sandbox._resource_mode = "rlimit-fallback"

    gpu = sandbox.prepare(
        ["python", "-c", "pass"], staging_dir=work / "gpu",
        log_name="gpu.log", env=None, timeout_s=10,
        execution_context={"phase": "gpu-development", "log_name": "gpu.log"},
        gpu_required=True)
    try:
        gpu.spec_file.seek(0)
        spec = json.loads(gpu.spec_file.read())
        assert spec["limits"]["memory_bytes"] == config["memory_mb"] * 1024 ** 2
        assert spec["limits"]["address_space_bytes"] == -1
        assert spec["argv"][5] == "-1"
    finally:
        gpu.close()

    cpu = sandbox.prepare(
        ["python", "-c", "pass"], staging_dir=work / "cpu",
        log_name="cpu.log", env=None, timeout_s=10,
        execution_context={"phase": "cpu-development", "log_name": "cpu.log"},
        gpu_required=False)
    try:
        cpu.spec_file.seek(0)
        spec = json.loads(cpu.spec_file.read())
        assert spec["limits"]["address_space_bytes"] == -1
        assert spec["argv"][5] == "-1"
    finally:
        cpu.close()

    production_config = {
        **config,
        "python_path": "/usr/local/bin/python",
        "local_environment": None,
        "development_gpu_thread_limit": None,
    }
    production = DockerExecutionSandbox(
        work_root=work, config=production_config,
        gpu_contract=_gpu_contract("GPU-a"))
    production._preflight_done = True
    production._resource_mode = "rlimit-fallback"
    invocation = production.prepare(
        ["python", "-c", "pass"], staging_dir=work / "production-gpu",
        log_name="gpu.log", env=None, timeout_s=10,
        execution_context={"phase": "gpu-production", "log_name": "gpu.log"},
        gpu_required=True)
    try:
        invocation.spec_file.seek(0)
        spec = json.loads(invocation.spec_file.read())
        expected = production_config["memory_mb"] * 1024 ** 2
        assert spec["limits"]["address_space_bytes"] == expected
        assert spec["argv"][5] == str(expected)
    finally:
        invocation.close()


def test_startup_recovers_db_less_gpu_canary_exit_publish_gap(tmp_path):
    work = tmp_path / "work"
    (work / "state" / "executions").mkdir(parents=True)
    sandbox = DockerExecutionSandbox(
        work_root=work, config=POLICY["execution"]["sandbox"])
    sandbox._preflight_done = True
    sandbox._resource_mode = POLICY["execution"]["sandbox"]["resource_mode"]
    context = {
        "phase": "deployment-gpu-canary", "candidate_hash": "sha256:" + "a" * 64,
        "runtime_identity_hash": sandbox.runtime_identity_hash,
        "log_name": "gpu-canary.log",
    }
    invocation = sandbox.prepare(
        ["python", "-c", "pass"], staging_dir=work / "canary",
        log_name="gpu-canary.log", env=None, timeout_s=10,
        execution_context=context)
    try:
        partial = work / "canary" / "gpu-canary.log.partial"
        partial.write_bytes(b"GPU canary complete\n")
        operation_id = "exec-" + "b" * 32
        receipt = {
            "operation_id": operation_id, "containment": "docker-container-v1",
            "state": "terminal", "outcome": "exit", "returncode": 0,
            "group_drained": True, "context": context,
            "sandbox": {**invocation.external_container, "container_drained": True},
        }
        atomic_write_receipt(
            work / "state" / "executions" / f"execution-{operation_id}.json",
            receipt)

        class _Supervisor:
            receipt_dir = work / "state" / "executions"

            @staticmethod
            def recover_previous_generation():
                return None

        assert sandbox.recover_terminal_sessions(_Supervisor()) == 1
        assert (work / "canary" / "gpu-canary.log").read_bytes() == b"GPU canary complete\n"
        assert (work / "canary" / "gpu-canary.log.exit").read_bytes() == b"0"
        assert list((work / "canary" / ".sandbox-meta").glob("*.promoted.json"))
        assert not partial.exists()
    finally:
        invocation.close()


def test_trusted_payload_environment_is_identity_bound_and_not_overridable(tmp_path):
    work = tmp_path / "work"
    (work / "state").mkdir(parents=True)
    config = {
        **POLICY["execution"]["sandbox"],
        "image": POLICY["execution"]["sandbox"]["image_id"],
        "payload_environment": {"PYTHONPATH": "/opt/meta-research/site-packages"},
    }
    sandbox = DockerExecutionSandbox(work_root=work, config=config)
    try:
        sandbox.preflight()
    except (ExecutionSandboxError, OSError, subprocess.SubprocessError) as error:
        pytest.skip(f"pinned local Docker sandbox unavailable: {error}")
    supervisor = ExecutionSupervisor.standalone(work / "state" / "executions")
    context = {"phase": "payload-env", "db_owner_kind": "build_target", "db_owner_id": 91}
    try:
        result = _run(
            work, sandbox, supervisor,
            ["python", "-c", "import os; print(os.environ['PYTHONPATH'])"],
            name="payload-env.log", context=context)
        assert "/opt/meta-research/site-packages" in Path(result["log_path"]).read_text()
        with pytest.raises(ExecutionSandboxError, match="不得覆盖"):
            sandbox.prepare(
                ["python", "-c", "pass"], staging_dir=work / "override",
                log_name="override.log",
                env={"PYTHONPATH": "/tmp/attacker"}, timeout_s=10,
                execution_context={
                    "phase": "override", "db_owner_kind": "build_target",
                    "db_owner_id": 92, "log_name": "override.log"},
                execution_supervisor=supervisor)
    finally:
        supervisor.close()


def test_rootless_bindfs_sources_are_derived_from_deepest_mount():
    mountinfo = (
        "1 0 0:1 / / rw - overlay overlay rw\n"
        "2 1 0:53 /c20250511/250806010 "
        "/vepfs-mlp2/c20250511/250806010 rw - "
        "gpfs fs_vepfs-cnbj2c98dea54433 rw\n")
    source = "/vepfs-mlp2/c20250511/250806010/mxm/input"
    assert _daemon_bind_source_candidates(
        source, mountinfo_text=mountinfo) == {
            source,
            ("/bindfs-mapped/mnt/vepfs-cnbj2c98dea54433/"
             "c20250511/250806010/mxm/input"),
        }
    assert _daemon_bind_source_candidates(
        "/tmp/input", mountinfo_text=mountinfo) == {
            "/tmp/input", "/bindfs-mapped/ebs/rootfs/tmp/input"}


def test_readonly_mount_cannot_be_work_root_ancestor(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    config = {**POLICY["execution"]["sandbox"],
              "readonly_mounts": [str(tmp_path)]}
    with pytest.raises(ValueError, match="祖先"):
        DockerExecutionSandbox(work_root=work, config=config)


def test_failed_prepare_rolls_back_private_session_paths(tmp_path):
    work, sandbox, supervisor = _runtime(tmp_path)
    context = {
        "phase": "probe", "db_owner_kind": "build_target", "db_owner_id": 9,
        "log_name": "invalid.log",
    }
    try:
        with pytest.raises(ExecutionSandboxError, match="argv"):
            sandbox.prepare(
                ["python", ""], staging_dir=work / "run",
                log_name="invalid.log", env=None, timeout_s=10,
                execution_context=context)
        assert not list((work / "run" / ".sandbox-meta").glob("*.json"))
        assert not list((work / "run" / ".sandbox-output").iterdir())
        assert not list((work / "state" / "sandbox" / "inputs").iterdir())
        assert not list((work / "state" / "sandbox" / "sessions").glob("*.json"))
    finally:
        supervisor.close()


def test_prepare_rejects_symlinked_staging_component(tmp_path):
    work, sandbox, supervisor = _runtime(tmp_path)
    outside = tmp_path / "outside-staging"
    outside.mkdir()
    (work / "redirect").symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises((ExecutionSandboxError, OSError), match="symlink|loop"):
            sandbox.prepare(
                ["python", "-c", "print('bad')"],
                staging_dir=work / "redirect" / "run", log_name="escape.log",
                env=None, timeout_s=10,
                execution_context={"phase": "probe"})
        assert not (outside / "run").exists()
    finally:
        supervisor.close()


def test_prepare_to_guardian_kill_window_recovers_without_wedge(tmp_path):
    work, sandbox, supervisor = _runtime(tmp_path)
    context = {
        "phase": "probe", "db_owner_kind": "build_target", "db_owner_id": 10,
        "reconcile_protocol": "execution-owner-v1", "log_name": "gap.log",
    }
    first = sandbox.prepare(
        ["python", "-c", "print('first')"], staging_dir=work / "gap",
        log_name="gap.log", env=None, timeout_s=10,
        execution_context=context, execution_supervisor=supervisor)
    first_token = first.external_container["token"]
    first.close()  # simulate owner death before harness/supervisor entry
    second = None
    try:
        second = sandbox.prepare(
            ["python", "-c", "print('second')"], staging_dir=work / "gap",
            log_name="gap.log", env=None, timeout_s=10,
            execution_context=context, execution_supervisor=supervisor)
        assert second.external_container["token"] != first_token
        second.close()
        (work / "gap" / "gap.log.partial").write_bytes(b"")
        assert recover_staged_result(
            staging_dir=str(work / "gap"), log_name="gap.log",
            execution_supervisor=supervisor, execution_kind="sandbox-gap",
            execution_context={key: value for key, value in context.items()
                               if key != "log_name"},
            execution_sandbox=sandbox) is None
        third = sandbox.prepare(
            ["python", "-c", "print('recovered')"], staging_dir=work / "gap",
            log_name="gap.log", env=None, timeout_s=10,
            execution_context=context, execution_supervisor=supervisor)
        result = run_staged(
            third.argv, staging_dir=str(work / "gap"), log_name="gap.log",
            timeout_s=10, env=third.env, pass_fds=third.pass_fds,
            execution_supervisor=supervisor, execution_kind="sandbox-gap",
            execution_context={key: value for key, value in context.items()
                               if key != "log_name"},
            sandbox_invocation=third)
        assert result["exit_code"] == 0
        assert (work / "gap" / "gap.log").read_text().strip() == "recovered"
    finally:
        if second is not None:
            second.close()
        supervisor.close()


def test_prepare_recovery_ignores_drained_receipt_from_prior_repair_attempt(tmp_path):
    work = tmp_path / "work"
    (work / "state" / "executions").mkdir(parents=True)
    sandbox = DockerExecutionSandbox(
        work_root=work, config=POLICY["execution"]["sandbox"])
    receipt_dir = work / "state" / "executions"
    old_context = {
        "phase": "smoke", "db_owner_kind": "build_target", "db_owner_id": 7,
        "build_target_id": 7, "reconcile_protocol": "execution-owner-v1",
        "execution_attempt": 1, "log_name": "smoke-1.log",
    }
    atomic_write_receipt(receipt_dir / ("execution-exec-" + "a" * 32 + ".json"), {
        "state": "terminal", "group_drained": True, "context": old_context,
    })

    class _Supervisor:
        @staticmethod
        def recover_previous_generation():
            return None

    _Supervisor.receipt_dir = receipt_dir
    replacement = {**old_context, "execution_attempt": 2}
    assert sandbox.recover_unstarted_session(
        staging_dir=work / "smoke", log_name="smoke-1.log",
        execution_context=replacement, execution_supervisor=_Supervisor()) is False

    future = {**old_context, "execution_attempt": 3}
    atomic_write_receipt(receipt_dir / ("execution-exec-" + "b" * 32 + ".json"), {
        "state": "terminal", "group_drained": True, "context": future,
    })
    with pytest.raises(ExecutionSandboxError, match="错配 guardian receipt"):
        sandbox.recover_unstarted_session(
            staging_dir=work / "smoke", log_name="smoke-1.log",
            execution_context=replacement, execution_supervisor=_Supervisor())


def test_terminal_session_index_is_retired_before_bundle_archive(tmp_path):
    work = tmp_path / "work"
    (work / "state" / "executions").mkdir(parents=True)
    sandbox = DockerExecutionSandbox(
        work_root=work, config=POLICY["execution"]["sandbox"])
    sandbox._preflight_done = True
    sandbox._resource_mode = POLICY["execution"]["sandbox"]["resource_mode"]
    receipt_dir = work / "state" / "executions"

    class _Supervisor:
        @staticmethod
        def recover_previous_generation():
            return None

    _Supervisor.receipt_dir = receipt_dir
    context = {
        "phase": "smoke", "db_owner_kind": "build_target", "db_owner_id": 8,
        "build_target_id": 8, "reconcile_protocol": "execution-owner-v1",
        "execution_attempt": 1, "log_name": "smoke-1.log",
    }
    invocation = sandbox.prepare(
        [sandbox.config["python_path"], "-c", "pass"],
        staging_dir=work / "smoke", log_name="smoke-1.log", env=None,
        timeout_s=10, execution_context=context,
        execution_supervisor=_Supervisor())
    try:
        receipt = {
            "operation_id": "exec-" + "c" * 32,
            "containment": "docker-container-v1", "state": "terminal",
            "outcome": "exit", "returncode": 1, "group_drained": True,
            "context": context,
            "sandbox": {**invocation.external_container, "container_drained": True},
        }
        atomic_write_receipt(
            receipt_dir / f"execution-{receipt['operation_id']}.json", receipt)
        from orchestrator.execution_sandbox import finalize_sandbox_output
        finalize_sandbox_output(
            staging_dir=work / "smoke", log_name="smoke-1.log",
            context=context, execution_receipt=receipt, exit_code=1)
        indexes = list((work / "state" / "sandbox" / "sessions").glob("*.json"))
        assert len(indexes) == 1
        assert sandbox.retire_terminal_sessions_for_archive(
            staging_dir=work / "smoke", execution_supervisor=_Supervisor()) == 1
        assert not indexes[0].exists()
        assert list((work / "smoke" / ".sandbox-meta").glob("*.json"))
        assert sandbox.retire_terminal_sessions_for_archive(
            staging_dir=work / "smoke", execution_supervisor=_Supervisor()) == 0
    finally:
        invocation.close()


def test_startup_recovery_discards_prepare_only_session(tmp_path):
    work, sandbox, supervisor = _runtime(tmp_path)
    context = {
        "phase": "probe", "db_owner_kind": "build_target", "db_owner_id": 13,
        "reconcile_protocol": "execution-owner-v1", "log_name": "startup-gap.log",
    }
    invocation = sandbox.prepare(
        ["python", "-c", "print('never started')"],
        staging_dir=work / "startup-gap", log_name="startup-gap.log", env=None,
        timeout_s=10, execution_context=context,
        execution_supervisor=supervisor)
    try:
        invocation.close()  # simulate owner death before supervisor.run()
        assert sandbox.recover_terminal_sessions(supervisor) == 1
        assert not list((work / "startup-gap" / ".sandbox-meta").glob("*.json"))
        assert not list((work / "startup-gap" / ".sandbox-output").iterdir())
        assert not list((work / "state" / "sandbox" / "inputs").iterdir())
        assert not list((work / "state" / "sandbox" / "sessions").glob("*.json"))
    finally:
        invocation.close()
        supervisor.close()


def test_startup_recovery_discards_index_only_prepare_crash(tmp_path):
    work, sandbox, supervisor = _runtime(tmp_path)
    context = {
        "phase": "probe", "db_owner_kind": "build_target", "db_owner_id": 14,
        "reconcile_protocol": "execution-owner-v1", "log_name": "index-gap.log",
    }
    invocation = sandbox.prepare(
        ["python", "-c", "print('never returned')"],
        staging_dir=work / "index-gap", log_name="index-gap.log", env=None,
        timeout_s=10, execution_context=context,
        execution_supervisor=supervisor)
    try:
        invocation.close()
        metadata = list((work / "index-gap" / ".sandbox-meta").glob("*.json"))
        assert len(metadata) == 1
        metadata[0].unlink()  # simulate SIGKILL after durable index, before metadata publish
        assert sandbox.recover_terminal_sessions(supervisor) == 1
        assert not list((work / "index-gap" / ".sandbox-output").iterdir())
        assert not list((work / "state" / "sandbox" / "inputs").iterdir())
        assert not list((work / "state" / "sandbox" / "sessions").glob("*.json"))
    finally:
        invocation.close()
        supervisor.close()


def test_recovery_discards_terminal_nonexit_sandbox_session(tmp_path):
    work, sandbox, supervisor = _runtime(tmp_path)
    context = {
        "phase": "probe", "db_owner_kind": "build_target", "db_owner_id": 11,
        "reconcile_protocol": "execution-owner-v1", "log_name": "nonexit.log",
    }
    invocation = sandbox.prepare(
        ["python", "-c", "import time; time.sleep(60)"],
        staging_dir=work / "nonexit", log_name="nonexit.log", env=None,
        timeout_s=0.4, execution_context=context,
        execution_supervisor=supervisor)
    partial = work / "nonexit" / "nonexit.log.partial"
    try:
        with partial.open("wb") as output, pytest.raises(SupervisedTimeoutExpired):
            supervisor.run(
                invocation.argv, stdout=output, stderr=subprocess.STDOUT,
                timeout_s=0.4, cwd=work / "nonexit", env=invocation.env,
                pass_fds=invocation.pass_fds, kind="sandbox-nonexit",
                operation_context=context,
                external_container=invocation.external_container)
        invocation.close()
        assert sandbox.recover_terminal_sessions(supervisor) == 1
        assert not list((work / "nonexit" / ".sandbox-output").iterdir())
        assert not list((work / "state" / "sandbox" / "inputs").iterdir())
        with pytest.raises(ExecutionRecoveryError, match="prior outcome=timeout"):
            recover_staged_result(
                staging_dir=str(work / "nonexit"), log_name="nonexit.log",
                execution_supervisor=supervisor, execution_kind="sandbox-nonexit",
                execution_context={key: value for key, value in context.items()
                                   if key != "log_name"},
                execution_sandbox=sandbox)
        assert sandbox.recover_terminal_sessions(supervisor) == 1  # idempotent authority replay
    finally:
        invocation.close()
        supervisor.close()


def test_payload_env_is_injected_only_after_rlimits(tmp_path):
    work, sandbox, supervisor = _runtime(tmp_path)
    context = {
        "phase": "probe", "db_owner_kind": "build_target", "db_owner_id": 12,
        "reconcile_protocol": "execution-owner-v1", "log_name": "env.log",
    }
    invocation = sandbox.prepare(
        ["python", "-c",
         "import os,resource; print(os.environ['LD_PRELOAD']); "
         "print(resource.getrlimit(resource.RLIMIT_NOFILE)[0])"],
        staging_dir=work / "env", log_name="env.log",
        env={"LD_PRELOAD": "/attacker.so"}, timeout_s=10,
        execution_context=context, execution_supervisor=supervisor)
    try:
        invocation.spec_file.seek(0)
        spec = json.loads(invocation.spec_file.read())
        invocation.spec_file.seek(0)
        assert "LD_PRELOAD" not in spec["env"]
        assert spec["payload_env"]["LD_PRELOAD"] == "/attacker.so"
        assert json.loads(spec["argv"][10])["LD_PRELOAD"] == "/attacker.so"
        result = run_staged(
            invocation.argv, staging_dir=str(work / "env"), log_name="env.log",
            timeout_s=10, env=invocation.env, pass_fds=invocation.pass_fds,
            execution_supervisor=supervisor, execution_kind="sandbox-env",
            execution_context={key: value for key, value in context.items()
                               if key != "log_name"},
            sandbox_invocation=invocation)
        assert result["exit_code"] == 0
        log = (work / "env" / "env.log").read_text()
        assert "/attacker.so" in log
        assert str(POLICY["execution"]["sandbox"]["nofile"]) in log
    finally:
        invocation.close()
        supervisor.close()


def test_pinned_launcher_seccomp_blocks_syscall_platform_profile_allows(tmp_path):
    work, sandbox, supervisor = _runtime(tmp_path)
    code = ("import ctypes; c=ctypes.CDLL(None,use_errno=True); "
            "r=c.syscall(272,0); print(f'unshare_zero={r}:{ctypes.get_errno()}'); "
            "assert r == -1 and ctypes.get_errno() == 1")
    try:
        result = _run(
            work, sandbox, supervisor, ["python", "-c", code], name="seccomp.log")
        assert result["exit_code"] == 0
        assert "unshare_zero=-1:1" in (work / "run" / "seccomp.log").read_text()
    finally:
        supervisor.close()


def test_real_development_sandbox_allows_bridge_but_blocks_rootfs_write(tmp_path):
    work, sandbox, supervisor = _runtime(tmp_path)
    code = """from pathlib import Path
import socket
Path('result.txt').write_text('sandbox-ok', encoding='utf-8')
try:
    Path('/etc/escape').write_text('bad')
    print('rootfs_write=BAD')
except OSError:
    print('rootfs_write=blocked')
try:
    socket.create_connection(('1.1.1.1', 53), 0.2)
    print('network=allowed')
except OSError:
    print('network=blocked')
print('metric_value: 1@1=0.9')
"""
    try:
        result = _run(work, sandbox, supervisor, ["python", "-c", code])
        receipt = result["process_receipt"]
        assert result["exit_code"] == 0
        assert receipt["containment"] == "docker-container-v1"
        assert receipt["sandbox"]["container_drained"] is True
        assert receipt["sandbox"]["resource_mode"] in {
            "cgroup-v1", "cgroup-v2", "rlimit-fallback"}
        assert (work / "run" / "result.txt").read_text() == "sandbox-ok"
        log = (work / "run" / "probe.log").read_text()
        assert receipt["sandbox"]["network_mode"] == "bridge"
        assert receipt["sandbox"]["network_development_only"] is True
        assert receipt["sandbox"]["local_environment_identity_sha256"] == (
            POLICY["execution"]["sandbox"]["local_environment"]["identity_sha256"])
        assert "rootfs_write=blocked" in log and "network=allowed" in log
        assert "=BAD" not in log
    finally:
        supervisor.close()


def test_real_development_sandbox_https_uses_mapped_conda_ca(tmp_path):
    if POLICY["execution"]["sandbox"]["network_mode"] != "bridge":
        pytest.skip("HTTPS integration probe requires the development bridge contract")
    work, sandbox, supervisor = _runtime(tmp_path)
    expected_ca = "/opt/host-conda/ssl/cert.pem"
    code = f"""import os
import urllib.request
assert os.environ['SSL_CERT_FILE'] == {expected_ca!r}
assert os.environ['REQUESTS_CA_BUNDLE'] == {expected_ca!r}
with urllib.request.urlopen('https://pypi.org/simple/pip/', timeout=15) as response:
    print(f'https_status={{response.status}}')
    assert response.status == 200
"""
    try:
        result = _run(
            work, sandbox, supervisor,
            [sandbox.config["python_path"], "-c", code],
            name="https-ca.log", timeout=30,
            context={"phase": "https-ca", "db_owner_kind": "build_target",
                     "db_owner_id": 1})
        assert result["exit_code"] == 0
        assert "https_status=200" in (
            work / "run" / "https-ca.log").read_text(encoding="utf-8")
    finally:
        supervisor.close()


def test_verified_fd_is_copied_before_host_path_swap(docker_workspace_tmp_path):
    tmp_path = docker_workspace_tmp_path
    work, sandbox, supervisor = _runtime(tmp_path)
    source = work / "authority.bin"
    source.write_bytes(b"trusted-before-swap")
    fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    info = os.fstat(fd)
    import hashlib
    digest = "sha256:" + hashlib.sha256(b"trusted-before-swap").hexdigest()
    context = {"phase": "probe", "db_owner_kind": "build_target", "db_owner_id": 2}
    invocation = sandbox.prepare(
        ["python", "-c", "import pathlib,sys; print(pathlib.Path(sys.argv[1]).read_text())",
         f"/proc/self/fd/{fd}"],
        staging_dir=work / "run", log_name="fd.log", env=None, timeout_s=15,
        fd_expectations=((fd, digest, info.st_size, info.st_dev, info.st_ino),),
        execution_context={**context, "log_name": "fd.log"})
    source.replace(work / "authority.old")
    source.write_bytes(b"malicious-after-swap")
    try:
        result = run_staged(
            invocation.argv, staging_dir=str(work / "run"), log_name="fd.log",
            timeout_s=15, env=invocation.env, pass_fds=invocation.pass_fds,
            execution_supervisor=supervisor, execution_kind="sandbox-probe",
            execution_context=context, sandbox_invocation=invocation)
        assert result["exit_code"] == 0
        assert (work / "run" / "fd.log").read_text().strip() == "trusted-before-swap"
    finally:
        os.close(fd)
        supervisor.close()


def test_timeout_guardian_force_removes_daemon_container(tmp_path):
    work, sandbox, supervisor = _runtime(tmp_path)
    command = ["python", "-c", "import time; print('started', flush=True); time.sleep(60)"]
    try:
        with pytest.raises(SupervisedTimeoutExpired) as caught:
            _run(work, sandbox, supervisor, command, name="timeout.log", timeout=0.5,
                 context={"phase": "probe", "db_owner_kind": "build_target", "db_owner_id": 3})
        receipt = caught.value.receipt
        assert receipt["outcome"] == "timeout"
        assert receipt["sandbox"]["container_drained"] is True
        config = POLICY["execution"]["sandbox"]
        listed = subprocess.run(
            [config["engine_path"], "container", "ls", "--all", "--filter",
             f"name=^/{receipt['sandbox']['container_name']}$", "--format", "{{.Names}}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            env={**os.environ, "DOCKER_HOST": config["engine_host"]})
        assert listed.returncode == 0 and listed.stdout.strip() == b""
    finally:
        supervisor.close()


def test_drained_exit_recovers_output_after_owner_publish_gap(
        docker_workspace_tmp_path):
    tmp_path = docker_workspace_tmp_path
    work, sandbox, supervisor = _runtime(tmp_path)
    context = {
        "phase": "probe", "db_owner_kind": "build_target", "db_owner_id": 4,
        "reconcile_protocol": "execution-owner-v1", "log_name": "recover.log",
    }
    invocation = sandbox.prepare(
        ["python", "-c",
         "from pathlib import Path; Path('checkpoint.bin').write_bytes(b'ck'); print('complete')"],
        staging_dir=work / "recover", log_name="recover.log", env=None,
        timeout_s=15, execution_context=context)
    directory = work / "recover"
    partial = directory / "recover.log.partial"
    try:
        with partial.open("wb") as output:
            result = supervisor.run(
                invocation.argv, stdout=output, stderr=subprocess.STDOUT,
                timeout_s=15, cwd=directory, env=invocation.env,
                pass_fds=invocation.pass_fds, kind="sandbox-recover",
                operation_context=context,
                external_container=invocation.external_container)
            output.flush()
            os.fsync(output.fileno())
        assert result.returncode == 0
        invocation.close()
        # Simulate owner loss after guardian return but before harness output/log publication.
        recovered = recover_staged_result(
            staging_dir=str(directory), log_name="recover.log",
            execution_supervisor=supervisor, execution_kind="sandbox-recover",
            execution_context={key: value for key, value in context.items()
                               if key != "log_name"})
        assert recovered is not None and recovered["recovered_after_owner_loss"] is True
        assert (directory / "checkpoint.bin").read_bytes() == b"ck"
        assert (directory / "recover.log").read_text().strip() == "complete"
    finally:
        invocation.close()
        supervisor.close()


def test_symlink_output_never_crosses_quarantine(docker_workspace_tmp_path):
    tmp_path = docker_workspace_tmp_path
    work, sandbox, supervisor = _runtime(tmp_path)
    code = "import os; os.symlink('/etc/passwd', 'escaped.txt'); print('done')"
    try:
        with pytest.raises(ExecutionSandboxError, match="symlink"):
            _run(work, sandbox, supervisor, ["python", "-c", code], name="symlink.log")
        assert not (work / "run" / "escaped.txt").exists()
        assert not list((work / "run" / ".sandbox-output").iterdir())
        assert not list((work / "state" / "sandbox" / "inputs").iterdir())
    finally:
        supervisor.close()


def test_promotion_never_traverses_preexisting_staging_symlink(
        docker_workspace_tmp_path):
    tmp_path = docker_workspace_tmp_path
    work, sandbox, supervisor = _runtime(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    staging = work / "run"
    staging.mkdir(parents=True, exist_ok=True)
    (staging / "redirect").symlink_to(outside, target_is_directory=True)
    code = ("from pathlib import Path; Path('redirect').mkdir(exist_ok=True); "
            "Path('redirect/escaped.txt').write_text('bad'); print('done')")
    try:
        with pytest.raises(ExecutionSandboxError, match="destination parent"):
            _run(work, sandbox, supervisor, ["python", "-c", code], name="redirect.log")
        assert not (outside / "escaped.txt").exists()
        assert not list((work / "run" / ".sandbox-output").iterdir())
        assert not list((work / "state" / "sandbox" / "inputs").iterdir())
    finally:
        supervisor.close()


def test_docker_log_driver_hard_rotates_adversarial_stdout(tmp_path):
    work = tmp_path / "work"
    (work / "state").mkdir(parents=True)
    config = {**POLICY["execution"]["sandbox"], "max_log_mb": 1}
    sandbox = DockerExecutionSandbox(work_root=work, config=config)
    try:
        sandbox.preflight()
    except (ExecutionSandboxError, OSError, subprocess.SubprocessError) as error:
        pytest.skip(f"pinned local Docker sandbox unavailable: {error}")
    supervisor = ExecutionSupervisor.standalone(work / "state" / "executions")
    code = "for _ in range(4096): print('x' * 1024)\nprint('tail-marker')"
    try:
        result = _run(
            work, sandbox, supervisor, ["python", "-c", code], name="bounded.log")
        assert result["exit_code"] == 125
        log = work / "run" / "bounded.log"
        assert log.stat().st_size <= 64 * 1024
        assert "硬日志上限" in log.read_text()
    finally:
        supervisor.close()
