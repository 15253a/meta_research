"""CP11.4c.2b.2b: exact Python wheel closure and restorable project image."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import stat
import subprocess
import zipfile
from pathlib import Path

import pytest
import yaml

from orchestrator.dependency_image import PythonWheelImageBuilder
from orchestrator.dependency_image_common import _wheel_url_is_allowed
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


def test_exact_image_build_reuse_and_archive_restore(tmp_path, monkeypatch):
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
