"""Verified Python wheel closure to an exact, restorable Docker image.

Repository Dockerfiles and host package installers are never executed. A v1
lock names exact public wheel URLs, byte sizes and SHA-256 identities. Wheels
are installed by the hardened bootstrap sandbox, while the generated image
contains only a local exact FROM, COPY, and closure label. The first image is
exported as a hashed archive because legacy Docker build timestamps are not
bit-reproducible; restore nevertheless reproduces the exact signed image ID.
"""
from __future__ import annotations

import os
import re
import secrets
import shutil
import stat
import sys
import threading
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Mapping, Optional

from .artifact_capability import open_artifact, open_directory, read_artifact_bytes
from .dependency_image_common import (
    _CLOSURE_LABEL,
    _FSIZE_EXEC,
    _IMAGE_ID_RE,
    _PROVIDER,
    _RUNTIME_PROBE,
    _WheelRedirectHandler,
    _hash_file,
    _write_bytes,
)
from .dependency_image_lock import _DependencyLockMixin
from .dependency_image_store import _DependencyImageStoreMixin
from .execution_sandbox import DockerExecutionSandbox
from .process_supervisor import ExecutionSupervisor
from .repository_materialization_common import (
    _SHA256_RE,
    RepositoryCacheError,
    RepositoryMaterializationError,
    RepositoryTransportError,
    _atomic_write_json,
    _canonical,
    _fsync_directory,
    _remove_private_tree,
    _sha256,
    _strict_json,
    _value_hash,
)


class PythonWheelImageBuilder(
        _DependencyLockMixin, _DependencyImageStoreMixin):
    """Build and resolve trusted exact-image capabilities from canonical locks."""

    def __init__(
            self, *, work_root: Path | str, config: Mapping[str, Any],
            compiler: Mapping[str, Any], bootstrap_sandbox: DockerExecutionSandbox,
            execution_supervisor: ExecutionSupervisor,
            owner_guard: Optional[Callable[[], None]] = None,
            wheel_fetcher: Optional[
                Callable[[str, Path, int], Mapping[str, Any]]] = None):
        if not isinstance(bootstrap_sandbox, DockerExecutionSandbox):
            raise ValueError("dependency image builder 要求 DockerExecutionSandbox")
        if not isinstance(execution_supervisor, ExecutionSupervisor):
            raise ValueError("dependency image builder 要求 ExecutionSupervisor")
        self.work_root = Path(os.path.abspath(os.fspath(work_root)))
        self.config = dict(config)
        self.compiler = dict(compiler)
        self.bootstrap_sandbox = bootstrap_sandbox
        self.execution_supervisor = execution_supervisor
        self.owner_guard = owner_guard or (lambda: None)
        self.wheel_fetcher = wheel_fetcher
        self._sandboxes: Dict[str, DockerExecutionSandbox] = {}
        self._build_lock = threading.RLock()
        self._validate_config()
        self._wheel_opener = urllib.request.build_opener(
            _WheelRedirectHandler(self.config["allowed_hosts"])).open

    def _validate_config(self) -> None:
        required = {
            "provider", "lock_basename", "allowed_hosts", "timeout_s",
            "max_lock_bytes", "max_wheels", "max_wheel_bytes",
            "max_total_wheel_bytes", "max_unpacked_wheel_bytes",
            "max_wheel_entries", "max_installed_bytes", "max_installed_files",
            "install_timeout_s", "build_timeout_s", "load_timeout_s",
            "max_image_archive_bytes", "max_cached_images", "site_packages_path",
        }
        if set(self.config) != required or self.config.get("provider") != _PROVIDER:
            raise ValueError("dependency_image 字段闭包/provider 非法")
        if self.config.get("lock_basename") != "python-wheel-lock.json":
            raise ValueError("dependency_image.lock_basename 非冻结名称")
        hosts = self.config["allowed_hosts"]
        if (not isinstance(hosts, list) or not 1 <= len(hosts) <= 16
                or len(set(hosts)) != len(hosts)
                or any(not isinstance(host, str) or host != host.lower()
                       or re.fullmatch(
                           r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host) is None
                       for host in hosts)):
            raise ValueError("dependency_image.allowed_hosts 非法")
        integer_bounds = {
            "max_lock_bytes": (1024, 16 * 1024 * 1024),
            "max_wheels": (1, 1024),
            "max_wheel_bytes": (1, 16 * 1024 * 1024 * 1024),
            "max_total_wheel_bytes": (1, 64 * 1024 * 1024 * 1024),
            "max_unpacked_wheel_bytes": (1, 128 * 1024 * 1024 * 1024),
            "max_wheel_entries": (1, 1_000_000),
            "max_installed_bytes": (1, 128 * 1024 * 1024 * 1024),
            "max_installed_files": (1, 1_000_000),
            "max_image_archive_bytes": (1, 256 * 1024 * 1024 * 1024),
            "max_cached_images": (1, 4096),
        }
        for key, (minimum, maximum) in integer_bounds.items():
            value = self.config[key]
            if (isinstance(value, bool) or not isinstance(value, int)
                    or not minimum <= value <= maximum):
                raise ValueError(f"dependency_image.{key} 越界")
        for key in ("timeout_s", "install_timeout_s", "build_timeout_s", "load_timeout_s"):
            value = self.config[key]
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not 1 <= float(value) <= 86400):
                raise ValueError(f"dependency_image.{key} 非有界正数")
        site = self.config["site_packages_path"]
        if (site != "/opt/meta-research/site-packages"
                or not isinstance(site, str) or os.path.normpath(site) != site):
            raise ValueError("dependency_image.site_packages_path 非冻结路径")
        expected_compiler = {"implementation", "version", "artifact_sha256"}
        if (set(self.compiler) != expected_compiler
                or self.compiler.get("implementation") != "CPython"
                or not isinstance(self.compiler.get("version"), str)
                or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", self.compiler["version"]) is None
                or not isinstance(self.compiler.get("artifact_sha256"), str)
                or _SHA256_RE.fullmatch(self.compiler["artifact_sha256"]) is None):
            raise ValueError("dependency image compiler identity 非法")
        if self.config["max_total_wheel_bytes"] > (
                self.bootstrap_sandbox.config["input_max_mb"] * 1024 * 1024):
            raise ValueError("dependency wheel 总量超过 bootstrap sandbox input 上限")
        if self.config["max_installed_bytes"] > (
                self.bootstrap_sandbox.config["max_output_mb"] * 1024 * 1024):
            raise ValueError("dependency installed 总量超过 bootstrap sandbox output 上限")
        if self.config["max_installed_files"] > self.bootstrap_sandbox.config["max_output_files"]:
            raise ValueError("dependency installed 文件数超过 bootstrap sandbox output 上限")

    @property
    def config_hash(self) -> str:
        return _value_hash({
            "builder": self.config,
            "compiler": self.compiler,
            "bootstrap_environment_hash": self.bootstrap_sandbox.environment_hash,
        })

    def _authority_directories(self) -> tuple[Path, Path, Path]:
        current = self.work_root
        info = os.lstat(current)
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid != os.geteuid() or info.st_mode & 0o022):
            raise RepositoryCacheError("dependency image work_root authority 非法")
        for component in ("state", "dependency-images"):
            current = current / component
            if not os.path.lexists(current):
                current.mkdir(mode=0o700)
            info = os.lstat(current)
            if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                    or info.st_uid != os.geteuid() or info.st_mode & 0o022):
                raise RepositoryCacheError("dependency image authority directory 非法")
        result = []
        for component in ("objects", "staging", "artifacts"):
            path = current / component
            if not os.path.lexists(path):
                path.mkdir(mode=0o700)
            info = os.lstat(path)
            if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                    or info.st_uid != os.geteuid() or info.st_mode & 0o022):
                raise RepositoryCacheError("dependency image cache directory 非法")
            result.append(path)
        return result[0], result[1], result[2]

    def build(
            self, *, tree_root: Path, lock_entry: Mapping[str, Any],
            repository: str, revision: str) -> Dict[str, Any]:
        with self._build_lock:
            return self._build_locked(
                tree_root=tree_root, lock_entry=lock_entry,
                repository=repository, revision=revision)

    def _build_locked(
            self, *, tree_root: Path, lock_entry: Mapping[str, Any],
            repository: str, revision: str) -> Dict[str, Any]:
        self.owner_guard()
        self.bootstrap_sandbox.preflight()
        image_environment = self.bootstrap_sandbox.image_environment
        if (image_environment.get("PYTHON_VERSION") != self.compiler["version"]
                or image_environment.get("PYTHON_SHA256")
                != self.compiler["artifact_sha256"].removeprefix("sha256:")):
            raise RepositoryCacheError(
                "dependency bootstrap image 未绑定 policy compiler artifact identity")
        lock, raw_lock = self._parse_lock(tree_root, lock_entry)
        closure_identity = {
            "provider": _PROVIDER,
            "lock_sha256": _sha256(raw_lock),
            "canonical_lock_hash": _value_hash(lock),
            "base_image": self.bootstrap_sandbox.config["image"],
            "base_image_id": self.bootstrap_sandbox.config["image_id"],
            "builder_config_hash": self.config_hash,
        }
        closure_hash = _value_hash(closure_identity)
        objects, staging_root, artifacts = self._authority_directories()
        object_path = objects / closure_hash.removeprefix("sha256:")
        if os.path.lexists(object_path):
            receipt, contract = self._verify_object(object_path)
            self._resolve_verified(receipt, contract)
            return {**contract,
                    "lock_canonical_hash": receipt["lock"]["canonical_hash"],
                    "wheel_manifest_hash": receipt["wheel_manifest_hash"],
                    "build_context_hash": receipt["build_context_hash"],
                    "runtime_receipt_hash": _value_hash(receipt["runtime"]),
                    "image_archive_sha256": receipt["image_archive"]["sha256"],
                    "wheels": receipt["wheels"]}
        cached_objects = list(objects.iterdir())
        if (len(cached_objects) >= self.config["max_cached_images"]
                or any(not stat.S_ISDIR(os.lstat(path).st_mode)
                       or stat.S_ISLNK(os.lstat(path).st_mode)
                       for path in cached_objects)):
            raise RepositoryCacheError(
                "dependency image cache 达上限或 object authority 非法")
        stage = staging_root / closure_hash.removeprefix("sha256:")
        if os.path.lexists(stage):
            # One active system instance owns this work root.  Under the local
            # build lock, a remaining directory can only be an earlier owner
            # generation.  Drain its guardian/container receipts before
            # discarding the uncommitted attempt; published objects are never
            # removed by this path.
            self.execution_supervisor.recover_previous_generation()
            self.bootstrap_sandbox.recover_terminal_sessions(
                self.execution_supervisor)
            self._discard_uncommitted_images(
                stage=stage, closure_hash=closure_hash)
            _remove_private_tree(stage)
        stage.mkdir(mode=0o700)
        try:
            _write_bytes(
                stage / self.config["lock_basename"], raw_lock, mode=0o400)
            wheelhouse = stage / "wheelhouse"
            wheelhouse.mkdir(mode=0o700)
            wheels = []
            wheel_hashes = {}
            unpacked_wheel_bytes = 0
            wheel_archive_entries = 0
            for item in lock["distributions"]:
                self.owner_guard()
                cached, evidence, unpacked_bytes, archive_entries = (
                    self._wheel_artifact(item, artifacts))
                unpacked_wheel_bytes += unpacked_bytes
                wheel_archive_entries += archive_entries
                if (unpacked_wheel_bytes > self.config["max_unpacked_wheel_bytes"]
                        or wheel_archive_entries > self.config["max_wheel_entries"]):
                    raise RepositoryMaterializationError(
                        "python wheel closure 解压 bytes/entry 总量超 policy")
                destination = wheelhouse / item["filename"]
                shutil.copyfile(cached, destination)
                os.chmod(destination, 0o400)
                os.utime(destination, (0, 0), follow_symlinks=False)
                wheel_hashes[item["filename"]] = item["sha256"]
                wheels.append(evidence)
            _fsync_directory(wheelhouse)
            owner_id = secrets.randbelow((1 << 53) - 1) + 1
            install = stage / "install"
            install.mkdir(mode=0o700)
            wheel_fd = open_directory(wheelhouse, label="dependency wheelhouse")
            try:
                wheel_args = [
                    f"/proc/self/fd/{wheel_fd}/{item['filename']}"
                    for item in lock["distributions"]]
                context = {
                    "phase": "dependency-image-install",
                    "repository": repository, "revision": revision,
                    "dependency_closure": closure_hash,
                    "db_owner_kind": "dependency_image", "db_owner_id": owner_id,
                }
                result = self._run_sandbox(
                    self.bootstrap_sandbox,
                    [self.bootstrap_sandbox.config["python_path"], "-I", "-B", "-m", "pip",
                     "install", "--no-index", "--no-deps", "--only-binary=:all:",
                     "--no-compile", "--no-cache-dir", "--disable-pip-version-check",
                     "--target", "/mr/output/site-packages", *wheel_args],
                    directory=install, log_name="install.log", context=context,
                    timeout_s=float(self.config["install_timeout_s"]),
                    env={"PIP_CONFIG_FILE": "/dev/null", "PIP_NO_INPUT": "1",
                         "PIP_DISABLE_PIP_VERSION_CHECK": "1"},
                    tree_expectations=((wheel_fd, wheel_hashes, ()),))
            finally:
                os.close(wheel_fd)
            if result["exit_code"] != 0:
                raise RepositoryMaterializationError(
                    "python wheel closure 无法在 pinned bootstrap image 离线安装")
            installed_root = install / "site-packages"
            installed_ledger = self._tree_ledger(installed_root)
            for current, dirs, files in os.walk(installed_root, topdown=False, followlinks=False):
                for name in files:
                    path = Path(current) / name
                    os.chmod(path, 0o444)
                    os.utime(path, (0, 0), follow_symlinks=False)
                for name in dirs:
                    path = Path(current) / name
                    os.chmod(path, 0o555)
                    os.utime(path, (0, 0), follow_symlinks=False)
                os.chmod(current, 0o555)
                os.utime(current, (0, 0), follow_symlinks=False)
            install_manifest_hash = _value_hash(installed_ledger)

            context_root = stage / "context"
            copied_root = context_root / "site-packages"
            copied_root.mkdir(parents=True, mode=0o755)
            for item in installed_ledger:
                source = installed_root / item["path"]
                destination = copied_root / item["path"]
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                shutil.copyfile(source, destination)
                os.chmod(destination, 0o444)
                os.utime(destination, (0, 0), follow_symlinks=False)
            dockerfile = (
                f"FROM {self.bootstrap_sandbox.config['image_id']}\n"
                f"COPY site-packages/ {self.config['site_packages_path']}/\n"
                f"LABEL {_CLOSURE_LABEL}=\"{closure_hash}\"\n").encode("ascii")
            _write_bytes(context_root / "Dockerfile", dockerfile, mode=0o444)
            os.utime(context_root / "Dockerfile", (0, 0), follow_symlinks=False)
            for current, dirs, _files in os.walk(
                    context_root, topdown=False, followlinks=False):
                for name in dirs:
                    directory = Path(current) / name
                    os.chmod(directory, 0o555)
                    os.utime(directory, (0, 0), follow_symlinks=False)
                os.chmod(current, 0o555)
                os.utime(current, (0, 0), follow_symlinks=False)
            context_files = [{
                "path": "Dockerfile", "sha256": _sha256(dockerfile),
                "bytes": len(dockerfile), "mode": "0444", "mtime_ns": 0,
            }, *({"path": "site-packages/" + item["path"],
                  "sha256": item["sha256"], "bytes": item["bytes"],
                  "mode": "0444", "mtime_ns": 0}
                 for item in installed_ledger)]
            context_files = sorted(context_files, key=lambda item: item["path"])
            directory_paths = set()
            for item in context_files:
                parent = PurePosixPath(item["path"]).parent
                while str(parent) != ".":
                    directory_paths.add(str(parent))
                    parent = parent.parent
            context_identity = {
                "version": 1,
                "root": {"mode": "0555", "mtime_ns": 0},
                "directories": [
                    {"path": path, "mode": "0555", "mtime_ns": 0}
                    for path in sorted(directory_paths)],
                "files": context_files,
            }
            build_context_hash = _value_hash(context_identity)

            base = self._inspect_image(self.bootstrap_sandbox.config["image"])
            if base is None:
                raise RepositoryCacheError("dependency base image 在 build 时消失")
            if base.get("Id") != self.bootstrap_sandbox.config["image_id"]:
                raise RepositoryCacheError("dependency base image exact pin 漂移")
            engine_identity = self._engine_identity()
            build_dir = stage / "build"
            build_dir.mkdir(mode=0o700)
            iidfile = build_dir / "image.id"
            build_context = {
                "phase": "dependency-image-build", "dependency_closure": closure_hash,
                "db_owner_kind": "dependency_image_build", "db_owner_id": owner_id,
            }
            build_result = self._run_host(
                [self.bootstrap_sandbox.engine_path, "build", "--quiet", "--no-cache",
                 "--force-rm", "--pull=false", "--network", "none",
                 "--platform", "linux/amd64", "--iidfile", str(iidfile),
                 str(context_root)],
                directory=build_dir, log_name="build.log", context=build_context,
                timeout_s=float(self.config["build_timeout_s"]),
                kind="dependency-image-build")
            if build_result["exit_code"] != 0:
                raise RepositoryTransportError("generated offline dependency image build 失败")
            image_id = read_artifact_bytes(
                iidfile, max_bytes=128, label="dependency image iidfile").decode("ascii").strip()
            if _IMAGE_ID_RE.fullmatch(image_id) is None:
                raise RepositoryCacheError("dependency image iidfile 非 exact image ID")
            self._verify_result_image(
                image_id=image_id, closure_hash=closure_hash, base=base)
            derived = self._derived_sandbox(image_id)

            expected_manifest = {
                "version": 1, "files": installed_ledger,
                "manifest_hash": install_manifest_hash,
            }
            expected_path = stage / "installed-manifest.json"
            _write_bytes(expected_path, _canonical(expected_manifest), mode=0o400)
            with open_artifact(
                    expected_path, label="dependency runtime expected manifest") as capability:
                identity = capability.identity
                manifest_fd = capability.detach()
            try:
                runtime_dir = stage / "runtime"
                runtime_dir.mkdir(mode=0o700)
                runtime_context = {
                    "phase": "dependency-image-runtime", "dependency_closure": closure_hash,
                    "db_owner_kind": "dependency_image_runtime", "db_owner_id": owner_id,
                }
                runtime_result = self._run_sandbox(
                    derived,
                    [derived.config["python_path"], "-I", "-S", "-B", "-c",
                     _RUNTIME_PROBE, f"/proc/self/fd/{manifest_fd}",
                     self.config["site_packages_path"], self.compiler["version"]],
                    directory=runtime_dir, log_name="runtime.log", context=runtime_context,
                    timeout_s=float(self.config["install_timeout_s"]),
                    fd_expectations=((manifest_fd, identity.content_hash,
                                      identity.size_bytes, identity.device, identity.inode),))
            finally:
                os.close(manifest_fd)
            if runtime_result["exit_code"] != 0:
                raise RepositoryCacheError("dependency result image runtime tree/compiler 验收失败")
            runtime_payload = read_artifact_bytes(
                stage / "runtime" / "runtime.json", max_bytes=64 * 1024,
                label="dependency runtime receipt")
            runtime_value = _strict_json(runtime_payload, label="dependency runtime receipt")
            if (runtime_payload != _canonical(runtime_value)
                    or not isinstance(runtime_value, dict)
                    or set(runtime_value) != {
                        "implementation", "version", "executable",
                        "installed_manifest_hash"}
                    or runtime_value.get("implementation") != "cpython"
                    or runtime_value.get("version") != self.compiler["version"]
                    or runtime_value.get("installed_manifest_hash") != install_manifest_hash
                    or not isinstance(runtime_value.get("executable"), str)
                    or not PurePosixPath(runtime_value["executable"]).is_absolute()
                    or len(runtime_value["executable"].encode("utf-8")) > 4096):
                raise RepositoryCacheError("dependency runtime receipt identity 漂移")
            check_context = {
                "phase": "dependency-image-pip-check", "dependency_closure": closure_hash,
                "db_owner_kind": "dependency_image_check", "db_owner_id": owner_id,
            }
            check_result = self._run_sandbox(
                derived,
                [derived.config["python_path"], "-B", "-m", "pip", "check"],
                directory=stage / "check", log_name="pip-check.log", context=check_context,
                timeout_s=float(self.config["install_timeout_s"]))
            if check_result["exit_code"] != 0:
                raise RepositoryMaterializationError(
                    "python wheel lock 缺失/冲突依赖（offline pip check failed）")

            archive_tmp = stage / "image.tar.tmp"
            save_dir = stage / "save"
            save_dir.mkdir(mode=0o700)
            save_context = {
                "phase": "dependency-image-save", "dependency_closure": closure_hash,
                "db_owner_kind": "dependency_image_save", "db_owner_id": owner_id,
            }
            save_result = self._run_host(
                [sys.executable, "-I", "-c", _FSIZE_EXEC,
                 str(self.config["max_image_archive_bytes"]),
                 self.bootstrap_sandbox.engine_path, "image", "save", "--output",
                 str(archive_tmp), image_id],
                directory=save_dir, log_name="save.log", context=save_context,
                timeout_s=float(self.config["build_timeout_s"]),
                kind="dependency-image-save")
            if save_result["exit_code"] != 0:
                raise RepositoryTransportError("dependency exact image archive 导出失败")
            archive_hash, archive_size = _hash_file(
                archive_tmp, maximum=self.config["max_image_archive_bytes"])
            archive_path = stage / "image.tar"
            os.chmod(archive_tmp, 0o400)
            os.replace(archive_tmp, archive_path)
            _fsync_directory(stage)
            if self._engine_identity() != engine_identity:
                raise RepositoryTransportError(
                    "dependency image Docker engine identity 在 build/save 期间漂移")
            payload_environment = dict(derived.config["payload_environment"])
            runtime_receipt = {
                "identity": runtime_value,
                "runtime_log_sha256": "sha256:" + runtime_result["log_sha256"],
                "runtime_output_sha256": _sha256(runtime_payload),
                "pip_check_log_sha256": "sha256:" + check_result["log_sha256"],
            }
            receipt = {
                "version": 1, "provider": _PROVIDER,
                "closure_hash": closure_hash,
                "builder_config_hash": self.config_hash,
                "base_environment_hash": self.bootstrap_sandbox.environment_hash,
                "base_image": self.bootstrap_sandbox.config["image"],
                "base_image_id": self.bootstrap_sandbox.config["image_id"],
                "result_image_id": image_id,
                "environment_hash": derived.environment_hash,
                "payload_environment": payload_environment,
                "lock": {
                    "path": lock_entry["path"], "sha256": lock_entry["sha256"],
                    "bytes": lock_entry["bytes"],
                    "canonical_hash": _value_hash(lock),
                },
                "wheels": wheels,
                "wheel_manifest_hash": _value_hash(wheels),
                "install_manifest_hash": install_manifest_hash,
                "build_context_hash": build_context_hash,
                "dockerfile_sha256": _sha256(dockerfile),
                "runtime": runtime_receipt,
                "image_archive": {"sha256": archive_hash, "bytes": archive_size},
                "compiler": self.compiler,
                "engine": engine_identity,
            }
            _atomic_write_json(stage / "receipt.json", receipt, maximum=16 * 1024 * 1024)
            for current, dirs, files in os.walk(stage, topdown=False, followlinks=False):
                for name in files:
                    path = Path(current) / name
                    info = os.lstat(path)
                    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
                            or info.st_nlink != 1 or info.st_uid != os.geteuid()):
                        raise RepositoryCacheError(
                            "dependency image object publish 前出现非可信常规文件")
                    fd = os.open(
                        path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_NOFOLLOW", 0))
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                    in_context = path == context_root or context_root in path.parents
                    os.chmod(path, 0o444 if in_context else 0o400)
                for name in dirs:
                    path = Path(current) / name
                    info = os.lstat(path)
                    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                            or info.st_uid != os.geteuid()):
                        raise RepositoryCacheError(
                            "dependency image object publish 前出现非可信 directory")
                    in_context = path == context_root or context_root in path.parents
                    os.chmod(path, 0o555 if in_context else 0o500)
                current_path = Path(current)
                in_context = (
                    current_path == context_root or context_root in current_path.parents)
                os.chmod(current_path, 0o555 if in_context else 0o500)
                _fsync_directory(Path(current))
            self.owner_guard()
            if os.path.lexists(object_path):
                winner_receipt, contract = self._verify_object(object_path)
                _remove_private_tree(stage)
                receipt = winner_receipt
            else:
                os.replace(stage, object_path)
                _fsync_directory(objects)
                receipt, contract = self._verify_object(object_path)
            self._resolve_verified(receipt, contract)
            return {**contract,
                    "lock_canonical_hash": receipt["lock"]["canonical_hash"],
                    "wheel_manifest_hash": receipt["wheel_manifest_hash"],
                    "build_context_hash": receipt["build_context_hash"],
                    "runtime_receipt_hash": _value_hash(receipt["runtime"]),
                    "image_archive_sha256": receipt["image_archive"]["sha256"],
                    "wheels": receipt["wheels"]}
        except BaseException:
            if os.path.lexists(stage):
                cleanup_complete = False
                try:
                    self.execution_supervisor.recover_previous_generation()
                    self.bootstrap_sandbox.recover_terminal_sessions(
                        self.execution_supervisor)
                    self._discard_uncommitted_images(
                        stage=stage, closure_hash=closure_hash)
                    cleanup_complete = True
                except BaseException:
                    # Preserve the original failure; the surviving labelled
                    # image and staging tree are deliberate forensic evidence
                    # and will be retried by the next owner generation.
                    cleanup_complete = False
                if cleanup_complete:
                    try:
                        _remove_private_tree(stage)
                    except OSError:
                        pass
            raise
