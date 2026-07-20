"""CP11.4c.2b.2b: exact Python wheel closure and restorable project image."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import stat
import subprocess
import urllib.request
import zipfile
from pathlib import Path

import pytest
import yaml

from orchestrator.dependency_image import PythonWheelImageBuilder
from orchestrator.dependency_image_common import _wheel_url_is_allowed
from orchestrator.dependency_image_inspector import inspect_dependency_image_object
import orchestrator.dependency_image_runtime as dependency_image_runtime
from orchestrator.execution_sandbox import (
    DockerExecutionSandbox,
    ExecutionSandboxError,
    sandbox_environment_hash,
    sandbox_workload_environment_hash,
)
from orchestrator.process_supervisor import ExecutionSupervisor
from orchestrator.repository_materialization_common import (
    RepositoryCacheError,
    RepositoryMaterializationError,
    RepositoryTransportError,
    _canonical,
    _value_hash,
)


SYSTEM_ROOT = Path(__file__).resolve().parent.parent
POLICY = yaml.safe_load(
    (SYSTEM_ROOT / "policies" / "policy.yaml").read_text(encoding="utf-8"))


def _wheel() -> tuple[str, bytes]:
    filename = "mr_demo-1.0-py3-none-any.whl"
    members = {
        "mr_demo/__init__.py": b"VALUE = 42\n",
        "mr_demo-1.0.dist-info/METADATA": (
            b"Metadata-Version: 2.1\nName: mr-demo\nVersion: 1.0\n\n"),
        "mr_demo-1.0.dist-info/WHEEL": (
            b"Wheel-Version: 1.0\nGenerator: meta-research-test\n"
            b"Root-Is-Purelib: true\nTag: py3-none-any\n\n"),
    }
    rows = []
    for name, payload in members.items():
        digest = hashlib.sha256(payload).digest()
        import base64
        encoded = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        rows.append([name, "sha256=" + encoded, str(len(payload))])
    record_name = "mr_demo-1.0.dist-info/RECORD"
    rows.append([record_name, "", ""])
    record = io.StringIO(newline="")
    csv.writer(record, lineterminator="\n").writerows(rows)
    members[record_name] = record.getvalue().encode("utf-8")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100444 << 16
            archive.writestr(info, payload)
    return filename, output.getvalue()


def _lock(filename: str, payload: bytes) -> dict:
    compiler = POLICY["import_materialization"]["compiler"]
    return {
        "version": 1,
        "python": compiler,
        "platform": {"os": "linux", "architecture": "amd64"},
        "distributions": [{
            "name": "mr-demo", "version": "1.0", "filename": filename,
            "url": f"https://files.pythonhosted.org/packages/{filename}",
            "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload),
        }],
    }


def _pure_dependency_image_object(
        tmp_path, *, wheel_host="files.pythonhosted.org"):
    """Build the immutable on-disk provider contract without Docker or network."""
    dependency_config = POLICY["import_materialization"]["dependency_image"]
    compiler = POLICY["import_materialization"]["compiler"]
    bootstrap = POLICY["execution"]["sandbox"]
    base_environment_hash = sandbox_environment_hash(bootstrap)
    builder_config_hash = _value_hash({
        "builder": dependency_config,
        "compiler": compiler,
        "bootstrap_environment_hash": base_environment_hash,
    })
    wheel_name = "mr_demo-1.0-py3-none-any.whl"
    wheel_payload = b"fixture wheel bytes\n"
    wheel = {
        "name": "mr-demo", "version": "1.0", "filename": wheel_name,
        "url": f"https://{wheel_host}/packages/{wheel_name}",
        "sha256": "sha256:" + hashlib.sha256(wheel_payload).hexdigest(),
        "bytes": len(wheel_payload),
    }
    lock = {
        "version": 1,
        "python": compiler,
        "platform": {"os": "linux", "architecture": "amd64"},
        "distributions": [wheel],
    }
    lock_raw = _canonical(lock)
    lock_sha256 = "sha256:" + hashlib.sha256(lock_raw).hexdigest()
    lock_canonical_hash = _value_hash(lock)
    closure_hash = _value_hash({
        "provider": "python-wheel-image-v1",
        "lock_sha256": lock_sha256,
        "canonical_lock_hash": lock_canonical_hash,
        "base_image": bootstrap["image"],
        "base_image_id": bootstrap["image_id"],
        "builder_config_hash": builder_config_hash,
    })
    result_image_id = "sha256:" + "c" * 64
    payload_environment = {
        **bootstrap["payload_environment"],
        "PYTHONPATH": dependency_config["site_packages_path"],
    }
    derived_config = dict(bootstrap)
    derived_config.update({
        "image": result_image_id,
        "image_id": result_image_id,
        "python_path": "/usr/local/bin/python3",
        "local_environment": None,
        "development_gpu_thread_limit": None,
        "payload_environment": payload_environment,
    })

    installed_payload = b"VALUE = 42\n"
    installed_files = [{
        "path": "mr_demo/__init__.py",
        "sha256": "sha256:" + hashlib.sha256(installed_payload).hexdigest(),
        "bytes": len(installed_payload),
    }]
    install_manifest_hash = _value_hash(installed_files)
    installed_manifest = {
        "version": 1, "files": installed_files,
        "manifest_hash": install_manifest_hash,
    }
    runtime_identity = {
        "implementation": "cpython", "version": compiler["version"],
        "executable": "/usr/local/bin/python",
        "installed_manifest_hash": install_manifest_hash,
    }
    runtime_payload = _canonical(runtime_identity)
    runtime_log = b"runtime verified\n"
    check_log = b"No broken requirements found.\n"
    dockerfile = (
        f"FROM {bootstrap['image_id']}\n"
        f"COPY site-packages/ {dependency_config['site_packages_path']}/\n"
        f"LABEL org.meta-research.dependency-closure=\"{closure_hash}\"\n"
    ).encode("ascii")
    context_files = sorted([{
        "path": "Dockerfile",
        "sha256": "sha256:" + hashlib.sha256(dockerfile).hexdigest(),
        "bytes": len(dockerfile), "mode": "0444", "mtime_ns": 0,
    }, {
        "path": "site-packages/mr_demo/__init__.py",
        "sha256": installed_files[0]["sha256"],
        "bytes": len(installed_payload), "mode": "0444", "mtime_ns": 0,
    }], key=lambda item: item["path"])
    context_identity = {
        "version": 1,
        "root": {"mode": "0555", "mtime_ns": 0},
        "directories": [
            {"path": value, "mode": "0555", "mtime_ns": 0}
            for value in ["site-packages", "site-packages/mr_demo"]],
        "files": context_files,
    }
    archive_payload = b"fixture exact image archive\n"
    receipt = {
        "version": 1, "provider": "python-wheel-image-v1",
        "closure_hash": closure_hash,
        "builder_config_hash": builder_config_hash,
        "base_environment_hash": base_environment_hash,
        "base_image": bootstrap["image"], "base_image_id": bootstrap["image_id"],
        "result_image_id": result_image_id,
        "environment_hash": sandbox_environment_hash(derived_config),
        "payload_environment": payload_environment,
        "lock": {
            "path": "python-wheel-lock.json", "sha256": lock_sha256,
            "bytes": len(lock_raw), "canonical_hash": lock_canonical_hash,
        },
        "wheels": [wheel], "wheel_manifest_hash": _value_hash([wheel]),
        "install_manifest_hash": install_manifest_hash,
        "build_context_hash": _value_hash(context_identity),
        "dockerfile_sha256": "sha256:" + hashlib.sha256(dockerfile).hexdigest(),
        "runtime": {
            "identity": runtime_identity,
            "runtime_log_sha256": "sha256:" + hashlib.sha256(runtime_log).hexdigest(),
            "runtime_output_sha256": "sha256:" + hashlib.sha256(runtime_payload).hexdigest(),
            "pip_check_log_sha256": "sha256:" + hashlib.sha256(check_log).hexdigest(),
        },
        "image_archive": {
            "sha256": "sha256:" + hashlib.sha256(archive_payload).hexdigest(),
            "bytes": len(archive_payload),
        },
        "compiler": compiler,
        "engine": {
            "client_version": "24.0.9", "server_version": "24.0.9",
            "os": "linux", "architecture": "amd64",
        },
    }
    object_path = tmp_path / closure_hash.removeprefix("sha256:")

    payloads = {
        "python-wheel-lock.json": lock_raw,
        f"wheelhouse/{wheel_name}": wheel_payload,
        "install/site-packages/mr_demo/__init__.py": installed_payload,
        "installed-manifest.json": _canonical(installed_manifest),
        "runtime/runtime.json": runtime_payload,
        "runtime/runtime.log": runtime_log,
        "runtime/runtime.log.exit": b"0",
        "check/pip-check.log": check_log,
        "check/pip-check.log.exit": b"0",
        "context/Dockerfile": dockerfile,
        "context/site-packages/mr_demo/__init__.py": installed_payload,
        "image.tar": archive_payload,
        "receipt.json": _canonical(receipt),
    }
    for relative, payload in payloads.items():
        destination = object_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
    context_root = object_path / "context"
    for current, dirs, files in os.walk(object_path, topdown=False):
        current_path = Path(current)
        for name in files:
            file_path = current_path / name
            in_context = context_root in file_path.parents
            os.chmod(file_path, 0o444 if in_context else 0o400)
            if in_context:
                os.utime(file_path, ns=(0, 0), follow_symlinks=False)
        for name in dirs:
            directory = current_path / name
            in_context = directory == context_root or context_root in directory.parents
            os.chmod(directory, 0o555 if in_context else 0o500)
            if in_context:
                os.utime(directory, ns=(0, 0), follow_symlinks=False)
        in_context = current_path == context_root or context_root in current_path.parents
        os.chmod(current_path, 0o555 if in_context else 0o500)
        if in_context:
            os.utime(current_path, ns=(0, 0), follow_symlinks=False)
    capability = {
        "version": 1, "provider": "python-wheel-image-v1",
        "closure_hash": closure_hash, "receipt_hash": _value_hash(receipt),
        "environment_hash": receipt["environment_hash"],
        "image": result_image_id, "image_id": result_image_id,
    }
    return object_path, capability, {
        "wheel": object_path / "wheelhouse" / wheel_name,
        "installed": object_path / "install" / "site-packages" / "mr_demo" / "__init__.py",
        "runtime": object_path / "runtime" / "runtime.log",
        "archive": object_path / "image.tar",
    }


def test_dependency_image_file_inspector_is_policy_independent_and_never_uses_docker(
        tmp_path, monkeypatch):
    object_path, capability, _artifacts = _pure_dependency_image_object(tmp_path)
    calls = 0

    def owner_guard():
        nonlocal calls
        calls += 1

    def forbidden(*_args, **_kwargs):
        pytest.fail("pure dependency-image inspection entered Docker/network")

    monkeypatch.setattr(dependency_image_runtime, "_engine", forbidden)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    receipt, actual = inspect_dependency_image_object(
        object_path, expected_capability=capability, owner_guard=owner_guard)
    assert actual == capability
    assert receipt["closure_hash"] == capability["closure_hash"]
    assert calls > 0

    work = tmp_path / "runtime-work"
    (work / "state").mkdir(parents=True)
    sandbox = DockerExecutionSandbox(
        work_root=work, config=POLICY["execution"]["sandbox"])
    supervisor = ExecutionSupervisor.standalone(work / "state" / "executions")
    builder = PythonWheelImageBuilder(
        work_root=work,
        config=POLICY["import_materialization"]["dependency_image"],
        compiler=POLICY["import_materialization"]["compiler"],
        bootstrap_sandbox=sandbox, execution_supervisor=supervisor)
    try:
        assert builder._verify_object(object_path) == (receipt, capability)
        foreign_path, foreign_capability, _foreign_artifacts = (
            _pure_dependency_image_object(
                tmp_path / "foreign-host", wheel_host="example.com"))
        inspect_dependency_image_object(
            foreign_path, expected_capability=foreign_capability)
        with pytest.raises(
                RepositoryCacheError, match="核验失败") as rejected:
            builder._verify_object(foreign_path)
        assert "exact HTTPS" in str(rejected.value.__cause__)
        linked = tmp_path / "linked-object"
        linked.symlink_to(object_path, target_is_directory=True)
        with monkeypatch.context() as guarded:
            guarded.setattr(
                dependency_image_runtime.os, "walk",
                lambda *_args, **_kwargs: pytest.fail(
                    "symlink object root was traversed before rejection"))
            with pytest.raises(RepositoryCacheError, match="root authority"):
                builder._verify_object(linked)
    finally:
        supervisor.close()


@pytest.mark.parametrize("artifact", ["wheel", "installed", "runtime", "archive"])
def test_dependency_image_file_inspector_rejects_correlated_object_tamper(
        tmp_path, artifact):
    object_path, capability, artifacts = _pure_dependency_image_object(tmp_path)
    target = artifacts[artifact]
    raw = target.read_bytes()
    os.chmod(target, 0o600)
    target.write_bytes(b"X" + raw[1:])
    os.chmod(target, 0o400)
    with pytest.raises(RepositoryCacheError, match="dependency"):
        inspect_dependency_image_object(
            object_path, expected_capability=capability)


def _builder(tmp_path, fetcher):
    work = tmp_path / "work"
    (work / "state").mkdir(parents=True)
    sandbox = DockerExecutionSandbox(
        work_root=work, config=POLICY["execution"]["sandbox"])
    try:
        sandbox.preflight()
    except (ExecutionSandboxError, OSError, subprocess.SubprocessError) as error:
        pytest.skip(f"pinned local Docker sandbox unavailable: {error}")
    supervisor = ExecutionSupervisor.standalone(work / "state" / "executions")
    builder = PythonWheelImageBuilder(
        work_root=work,
        config=POLICY["import_materialization"]["dependency_image"],
        compiler=POLICY["import_materialization"]["compiler"],
        bootstrap_sandbox=sandbox, execution_supervisor=supervisor,
        wheel_fetcher=fetcher)
    return work, builder, supervisor


def test_derived_image_sandbox_inherits_exact_gpu_contract(tmp_path, monkeypatch):
    work = tmp_path / "work"
    (work / "state").mkdir(parents=True)
    gpu_contract = {
        "version": 1, "provider": "nvidia", "driver_version": "535.129.03",
        "request": {
            "driver": "nvidia",
            "capabilities": ["compute", "utility", "gpu"], "options": {},
        },
        "devices": [{
            "uuid": "GPU-test", "model": "NVIDIA A100-SXM4-80GB",
            "memory_bytes": 80 * 1024 ** 3, "compute_capability": "8.0",
        }],
    }
    bootstrap = DockerExecutionSandbox(
        work_root=work, config=POLICY["execution"]["sandbox"],
        gpu_contract=gpu_contract)
    supervisor = ExecutionSupervisor.standalone(work / "state" / "executions")
    builder = PythonWheelImageBuilder(
        work_root=work,
        config=POLICY["import_materialization"]["dependency_image"],
        compiler=POLICY["import_materialization"]["compiler"],
        bootstrap_sandbox=bootstrap, execution_supervisor=supervisor)
    monkeypatch.setattr(DockerExecutionSandbox, "preflight", lambda _self: None)
    try:
        derived = builder._derived_sandbox("sha256:" + "a" * 64)
        assert derived.gpu_contract == bootstrap.gpu_contract
        assert derived.config["gpu_capability"] == bootstrap.config["gpu_capability"]
        assert bootstrap.config["local_environment"] is not None
        assert derived.config["local_environment"] is None
        assert derived.config["development_gpu_thread_limit"] is None
        assert derived.config["python_path"] == "/usr/local/bin/python3"
        assert derived.config["network_mode"] == bootstrap.config["network_mode"]
        assert derived.config["readonly_mounts"] == bootstrap.config["readonly_mounts"]
        assert derived.environment_hash == sandbox_environment_hash(derived.config)
        assert derived.environment_hash != derived.workload_environment_hash(True)
        assert derived.workload_environment_hash(True) == (
            sandbox_workload_environment_hash(derived.environment_hash, True))
        assert derived.runtime_identity_hash != bootstrap.runtime_identity_hash
    finally:
        supervisor.close()


@pytest.mark.parametrize("url", [
    "https://files.pythonhosted.org:bad/example.whl",
    "https://files.pythonhosted.org/example.whl?mirror=1",
    "https://files.pythonhosted.org/example.whl#fragment",
    "https://files.pythonhosted.org/example.whl\n",
    "https://user@files.pythonhosted.org/example.whl",
])
def test_wheel_url_literal_normalization_is_rejected(url):
    assert not _wheel_url_is_allowed(
        url, ["files.pythonhosted.org"], filename="example.whl")


def test_wheel_url_accepts_explicit_default_https_port():
    assert _wheel_url_is_allowed(
        "https://files.pythonhosted.org:443/example.whl",
        ["files.pythonhosted.org"], filename="example.whl")


def test_lock_requires_canonical_bytes_and_exact_wheel_identity(tmp_path):
    filename, wheel = _wheel()
    lock = _lock(filename, wheel)

    def fetcher(_url, destination, _maximum):
        destination.write_bytes(wheel)
        return {"sha256": lock["distributions"][0]["sha256"],
                "bytes": len(wheel)}

    work, builder, supervisor = _builder(tmp_path, fetcher)
    tree = work / "tree"
    tree.mkdir()
    raw = json.dumps(lock, indent=2).encode("utf-8")
    path = tree / "python-wheel-lock.json"
    path.write_bytes(raw)
    entry = {"path": path.name,
             "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
             "bytes": len(raw)}
    try:
        with pytest.raises(RepositoryMaterializationError, match="canonical JSON"):
            builder.build(
                tree_root=tree, lock_entry=entry,
                repository="acme/model", revision="a" * 40)

        lock["distributions"][0]["filename"] = "mr_demo-1.0-cp310-none-any.whl"
        lock["distributions"][0]["url"] = (
            "https://files.pythonhosted.org/packages/"
            + lock["distributions"][0]["filename"])
        raw = _canonical(lock)
        path.write_bytes(raw)
        entry = {"path": path.name,
                 "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
                 "bytes": len(raw)}
        with pytest.raises(RepositoryMaterializationError, match="target/identity"):
            builder.build(
                tree_root=tree, lock_entry=entry,
                repository="acme/model", revision="a" * 40)

        lock["distributions"][0]["filename"] = "mr_demo-1.0-py3-none-win_x86_64.whl"
        lock["distributions"][0]["url"] = (
            "https://files.pythonhosted.org/packages/"
            + lock["distributions"][0]["filename"])
        raw = _canonical(lock)
        path.write_bytes(raw)
        entry = {"path": path.name,
                 "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
                 "bytes": len(raw)}
        with pytest.raises(RepositoryMaterializationError, match="target/identity"):
            builder.build(
                tree_root=tree, lock_entry=entry,
                repository="acme/model", revision="a" * 40)
    finally:
        supervisor.close()


def test_lock_rejects_ssrf_and_download_identity_drift(tmp_path):
    filename, wheel = _wheel()
    lock = _lock(filename, wheel)
    fetched = False

    def fetcher(_url, destination, _maximum):
        nonlocal fetched
        fetched = True
        destination.write_bytes(wheel + b"drift")
        return {"sha256": "sha256:" + hashlib.sha256(wheel + b"drift").hexdigest(),
                "bytes": len(wheel) + 5}

    work, builder, supervisor = _builder(tmp_path, fetcher)
    tree = work / "tree"
    tree.mkdir()
    path = tree / "python-wheel-lock.json"
    try:
        lock["distributions"][0]["url"] = f"https://127.0.0.1/{filename}"
        raw = _canonical(lock)
        path.write_bytes(raw)
        with pytest.raises(RepositoryMaterializationError, match="exact HTTPS"):
            builder.build(
                tree_root=tree,
                lock_entry={"path": path.name,
                            "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
                            "bytes": len(raw)},
                repository="acme/model", revision="a" * 40)
        assert fetched is False

        lock["distributions"][0]["url"] = (
            f"https://files.pythonhosted.org/packages/{filename}")
        raw = _canonical(lock)
        path.write_bytes(raw)
        with pytest.raises(RepositoryTransportError, match="hash/size"):
            builder.build(
                tree_root=tree,
                lock_entry={"path": path.name,
                            "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
                            "bytes": len(raw)},
                repository="acme/model", revision="a" * 40)
        assert fetched is True
    finally:
        supervisor.close()


def test_exact_image_build_reuse_and_archive_restore(
        docker_workspace_tmp_path, monkeypatch):
    tmp_path = docker_workspace_tmp_path
    filename, wheel = _wheel()
    lock = _lock(filename, wheel)
    calls = 0

    def fetcher(url, destination, maximum):
        nonlocal calls
        calls += 1
        assert url == lock["distributions"][0]["url"]
        assert len(wheel) <= maximum
        destination.write_bytes(wheel)
        return {"url": url, "sha256": lock["distributions"][0]["sha256"],
                "bytes": len(wheel)}

    work, builder, supervisor = _builder(tmp_path, fetcher)
    tree = work / "tree"
    tree.mkdir()
    raw = _canonical(lock)
    path = tree / "python-wheel-lock.json"
    path.write_bytes(raw)
    entry = {"path": path.name,
             "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
             "bytes": len(raw)}
    image_id = None
    try:
        closure_hash = _value_hash({
            "provider": "python-wheel-image-v1",
            "lock_sha256": entry["sha256"],
            "canonical_lock_hash": _value_hash(lock),
            "base_image": builder.bootstrap_sandbox.config["image"],
            "base_image_id": builder.bootstrap_sandbox.config["image_id"],
            "builder_config_hash": builder.config_hash,
        })
        stale = (work / "state" / "dependency-images" / "staging"
                 / closure_hash.removeprefix("sha256:"))
        stale.mkdir(parents=True)
        (stale / "owner-killed.partial").write_text("partial")
        verify_result_image = builder._verify_result_image

        def reject_result_image(**_kwargs):
            raise RepositoryCacheError("injected pre-publish failure")

        monkeypatch.setattr(builder, "_verify_result_image", reject_result_image)
        with pytest.raises(RepositoryCacheError, match="injected pre-publish"):
            builder.build(
                tree_root=tree, lock_entry=entry,
                repository="acme/model", revision="a" * 40)
        assert builder._closure_image_ids(closure_hash) == []
        assert not stale.exists()
        monkeypatch.setattr(builder, "_verify_result_image", verify_result_image)

        first = builder.build(
            tree_root=tree, lock_entry=entry,
            repository="acme/model", revision="a" * 40)
        capability_keys = {
            "version", "provider", "closure_hash", "receipt_hash",
            "environment_hash", "image", "image_id",
        }
        capability = {key: first[key] for key in capability_keys}
        image_id = first["image_id"]
        assert first["image"] == image_id
        assert first["environment_hash"] != builder.bootstrap_sandbox.environment_hash
        project_sandbox = builder.resolve(capability)
        assert project_sandbox.environment_hash == first["environment_hash"]
        payload_result = builder._run_sandbox(
            project_sandbox,
            [project_sandbox.config["python_path"], "-c",
             "import mr_demo,pathlib; pathlib.Path('/mr/output/value.txt').write_text(str(mr_demo.VALUE))"],
            directory=work / "payload-probe", log_name="payload.log",
            context={"phase": "dependency-payload-probe",
                     "db_owner_kind": "dependency_payload_probe", "db_owner_id": 1},
            timeout_s=30)
        assert payload_result["exit_code"] == 0
        assert (work / "payload-probe" / "value.txt").read_text() == "42"
        verify_object = builder._verify_object
        verify_calls = 0

        def count_verify_object(object_path):
            nonlocal verify_calls
            verify_calls += 1
            return verify_object(object_path)

        monkeypatch.setattr(builder, "_verify_object", count_verify_object)
        assert builder.resolve_environment_hash(
            first["environment_hash"]).environment_hash == first["environment_hash"]
        assert verify_calls == 1
        monkeypatch.setattr(builder, "_verify_object", verify_object)
        with pytest.raises(RepositoryCacheError, match="未绑定唯一"):
            builder.resolve_environment_hash("sha256:" + "f" * 64)
        object_path = (
            work / "state" / "dependency-images" / "objects"
            / first["closure_hash"].removeprefix("sha256:"))
        context_file = object_path / "context" / "site-packages" / "mr_demo" / "__init__.py"
        original = context_file.read_bytes()
        original_mode = stat.S_IMODE(os.lstat(context_file).st_mode)
        original_times = (
            os.lstat(context_file).st_atime_ns,
            os.lstat(context_file).st_mtime_ns)
        os.chmod(context_file, 0o644)
        context_file.write_bytes(original + b"# tampered\n")
        with pytest.raises(RepositoryCacheError, match="build context|object"):
            builder.resolve(capability)
        context_file.write_bytes(original)
        os.chmod(context_file, original_mode)
        os.utime(context_file, ns=original_times, follow_symlinks=False)
        runtime_log = object_path / "runtime" / "runtime.log"
        runtime_original = runtime_log.read_bytes()
        runtime_mode = stat.S_IMODE(os.lstat(runtime_log).st_mode)
        os.chmod(runtime_log, 0o600)
        runtime_log.write_bytes(runtime_original + b"tampered\n")
        with pytest.raises(RepositoryCacheError, match="runtime evidence|object"):
            builder.resolve(capability)
        runtime_log.write_bytes(runtime_original)
        os.chmod(runtime_log, runtime_mode)
        second = builder.build(
            tree_root=tree, lock_entry=entry,
            repository="acme/model", revision="a" * 40)
        assert second["receipt_hash"] == first["receipt_hash"]
        assert calls == 1

        subprocess.run(
            [builder.bootstrap_sandbox.engine_path, "image", "rm", image_id],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={"PATH": os.defpath,
                 "DOCKER_HOST": builder.bootstrap_sandbox.config["engine_host"]})
        assert builder._inspect_image(image_id, missing_ok=True) is None
        restored = builder.resolve(capability)
        assert restored.environment_hash == first["environment_hash"]
        assert builder._inspect_image(image_id, missing_ok=True)["Id"] == image_id
        subprocess.run(
            [builder.bootstrap_sandbox.engine_path, "image", "rm", image_id],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={"PATH": os.defpath,
                 "DOCKER_HOST": builder.bootstrap_sandbox.config["engine_host"]})
        assert builder.resolve(capability).environment_hash == first["environment_hash"]
    finally:
        if image_id is not None:
            subprocess.run(
                [builder.bootstrap_sandbox.engine_path, "image", "rm", image_id],
                check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env={"PATH": os.defpath,
                     "DOCKER_HOST": builder.bootstrap_sandbox.config["engine_host"]})
        supervisor.close()


def test_public_pypi_wheel_canary(tmp_path):
    if os.environ.get("META_RESEARCH_PYPI_CANARY") != "1":
        pytest.skip("set META_RESEARCH_PYPI_CANARY=1 for the read-only public canary")
    filename = "idna-3.15-py3-none-any.whl"
    lock = {
        "version": 1,
        "python": POLICY["import_materialization"]["compiler"],
        "platform": {"os": "linux", "architecture": "amd64"},
        "distributions": [{
            "name": "idna", "version": "3.15", "filename": filename,
            "url": ("https://files.pythonhosted.org/packages/d2/23/"
                    "408243171aa9aaba178d3e2559159c24c1171a641aa83b67bdd3394ead8e/"
                    + filename),
            "sha256": ("sha256:048adeaf8c2d788c40fee287673ccaa74c24ffd8dcf09ffa"
                       "555a2fbb59f10ac8"),
            "bytes": 72340,
        }],
    }
    work, builder, supervisor = _builder(tmp_path, None)
    tree = work / "tree"
    tree.mkdir()
    raw = _canonical(lock)
    path = tree / "python-wheel-lock.json"
    path.write_bytes(raw)
    image_id = None
    try:
        result = builder.build(
            tree_root=tree,
            lock_entry={"path": path.name,
                        "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
                        "bytes": len(raw)},
            repository="pypi-canary/idna", revision="0" * 40)
        image_id = result["image_id"]
        assert result["wheels"] == lock["distributions"]
        assert builder.resolve_environment_hash(
            result["environment_hash"]).environment_hash == result["environment_hash"]
    finally:
        if image_id is not None:
            subprocess.run(
                [builder.bootstrap_sandbox.engine_path, "image", "rm", image_id],
                check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env={"PATH": os.defpath,
                     "DOCKER_HOST": builder.bootstrap_sandbox.config["engine_host"]})
        supervisor.close()
