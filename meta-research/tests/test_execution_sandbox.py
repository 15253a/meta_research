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
    _daemon_bind_source_candidates,
    DockerExecutionSandbox,
    ExecutionSandboxError,
    sandbox_environment_hash,
)
from orchestrator.harness import ExecutionRecoveryError, recover_staged_result, run_staged
from orchestrator.process_supervisor import ExecutionSupervisor, SupervisedTimeoutExpired


SYSTEM_ROOT = Path(__file__).resolve().parent.parent
POLICY = yaml.safe_load((SYSTEM_ROOT / "policies" / "policy.yaml").read_text(encoding="utf-8"))


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


def test_real_sandbox_blocks_network_and_rootfs_then_promotes_output(tmp_path):
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
    print('network=BAD')
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
        assert "rootfs_write=blocked" in log and "network=blocked" in log
        assert "=BAD" not in log
    finally:
        supervisor.close()


def test_verified_fd_is_copied_before_host_path_swap(tmp_path):
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


def test_drained_exit_recovers_output_after_owner_publish_gap(tmp_path):
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


def test_symlink_output_never_crosses_quarantine(tmp_path):
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


def test_promotion_never_traverses_preexisting_staging_symlink(tmp_path):
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
