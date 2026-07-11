"""Exact-image receipt verification, sandbox resolution, and archive restore."""
from __future__ import annotations

import os
import re
import secrets
import stat
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Mapping

from .artifact_capability import (
    ArtifactCapabilityError,
    open_directory,
    read_artifact_bytes,
    verify_tree_fd,
)
from .dependency_image_common import (
    _CLOSURE_LABEL,
    _IMAGE_ID_RE,
    _NAME_RE,
    _PROVIDER,
    _VERSION_RE,
    _WHEEL_RE,
    _hash_file,
    _wheel_url_is_allowed,
)
from .execution_sandbox import (
    DockerExecutionSandbox,
    sandbox_environment_hash,
)
from .repository_materialization_common import (
    _SHA256_RE,
    RepositoryCacheError,
    _canonical,
    _fsync_directory,
    _remove_private_tree,
    _safe_relpath,
    _sha256,
    _strict_json,
    _value_hash,
)
from .dependency_image_runtime import _DependencyImageRuntimeMixin


class _DependencyImageStoreMixin(_DependencyImageRuntimeMixin):
    """Verify, resolve, and restore published exact-image capabilities."""

    def _contract(self, receipt: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "version": 1, "provider": _PROVIDER,
            "closure_hash": receipt["closure_hash"],
            "receipt_hash": _value_hash(receipt),
            "environment_hash": receipt["environment_hash"],
            "image": receipt["result_image_id"],
            "image_id": receipt["result_image_id"],
        }

    def _verify_object(self, object_path: Path) -> tuple[Dict[str, Any], Dict[str, Any]]:
        try:
            object_info = os.lstat(object_path)
            if (not stat.S_ISDIR(object_info.st_mode) or stat.S_ISLNK(object_info.st_mode)
                    or object_info.st_uid != os.geteuid()
                    or stat.S_IMODE(object_info.st_mode) != 0o500):
                raise RepositoryCacheError("dependency image object root authority 非法")
            self._verify_object_authority(object_path)
            raw = read_artifact_bytes(
                object_path / "receipt.json", max_bytes=16 * 1024 * 1024,
                label="dependency image receipt", progress_guard=self.owner_guard)
            receipt = _strict_json(raw, label="dependency image receipt")
            required = {
                "version", "provider", "closure_hash", "builder_config_hash",
                "base_environment_hash", "base_image", "base_image_id",
                "result_image_id", "environment_hash", "payload_environment",
                "lock", "wheels", "wheel_manifest_hash", "install_manifest_hash",
                "build_context_hash", "dockerfile_sha256", "runtime",
                "image_archive", "compiler", "engine",
            }
            if (raw != _canonical(receipt)
                    or not isinstance(receipt, dict) or set(receipt) != required
                    or receipt.get("version") != 1 or receipt.get("provider") != _PROVIDER
                    or re.fullmatch(r"[0-9a-f]{64}", object_path.name) is None
                    or receipt.get("closure_hash") != "sha256:" + object_path.name
                    or receipt.get("builder_config_hash") != self.config_hash
                    or receipt.get("base_environment_hash")
                    != self.bootstrap_sandbox.environment_hash
                    or receipt.get("base_image") != self.bootstrap_sandbox.config["image"]
                    or receipt.get("base_image_id") != self.bootstrap_sandbox.config["image_id"]
                    or not isinstance(receipt.get("result_image_id"), str)
                    or _IMAGE_ID_RE.fullmatch(receipt["result_image_id"]) is None
                    or receipt.get("payload_environment") != {
                        **self.bootstrap_sandbox.config["payload_environment"],
                        "PYTHONPATH": self.config["site_packages_path"],
                    }
                    or receipt.get("compiler") != self.compiler):
                raise RepositoryCacheError("dependency image receipt identity 非法")
            engine = receipt.get("engine")
            if (not isinstance(engine, dict)
                    or set(engine) != {
                        "client_version", "server_version", "os", "architecture"}
                    or engine.get("os") != "linux" or engine.get("architecture") != "amd64"
                    or any(not isinstance(value, str) or not value or len(value) > 128
                           for value in engine.values())):
                raise RepositoryCacheError("dependency image receipt engine identity 非法")
            lock = receipt.get("lock")
            if (not isinstance(lock, dict)
                    or set(lock) != {"path", "sha256", "bytes", "canonical_hash"}
                    or not isinstance(lock.get("path"), str)
                    or _safe_relpath(
                        lock["path"], field="dependency stored lock path",
                        max_depth=128) != lock["path"]
                    or PurePosixPath(lock["path"]).name != self.config["lock_basename"]
                    or not isinstance(lock.get("sha256"), str)
                    or _SHA256_RE.fullmatch(lock["sha256"]) is None
                    or not isinstance(lock.get("canonical_hash"), str)
                    or _SHA256_RE.fullmatch(lock["canonical_hash"]) is None
                    or isinstance(lock.get("bytes"), bool)
                    or not isinstance(lock.get("bytes"), int)
                    or not 1 <= lock["bytes"] <= self.config["max_lock_bytes"]):
                raise RepositoryCacheError("dependency image receipt lock 非法")
            closure_identity = {
                "provider": _PROVIDER,
                "lock_sha256": lock["sha256"],
                "canonical_lock_hash": lock["canonical_hash"],
                "base_image": receipt["base_image"],
                "base_image_id": receipt["base_image_id"],
                "builder_config_hash": receipt["builder_config_hash"],
            }
            if _value_hash(closure_identity) != receipt["closure_hash"]:
                raise RepositoryCacheError("dependency image closure_hash 重算不一致")
            parsed_lock, stored_lock_raw = self._parse_lock(
                object_path, {
                    "path": self.config["lock_basename"],
                    "sha256": lock["sha256"], "bytes": lock["bytes"],
                })
            if (_sha256(stored_lock_raw) != lock["sha256"]
                    or _value_hash(parsed_lock) != lock["canonical_hash"]):
                raise RepositoryCacheError("dependency image stored lock identity 漂移")
            wheels = receipt.get("wheels")
            wheel_keys = {"name", "version", "filename", "url", "sha256", "bytes"}
            if (not isinstance(wheels, list)
                    or not 1 <= len(wheels) <= self.config["max_wheels"]
                    or wheels != sorted(wheels, key=lambda item: (
                        item.get("name", "") if isinstance(item, dict) else "",
                        item.get("version", "") if isinstance(item, dict) else "",
                        item.get("filename", "") if isinstance(item, dict) else ""))
                    or any(not isinstance(item, dict) or set(item) != wheel_keys
                           or not isinstance(item.get("name"), str)
                           or _NAME_RE.fullmatch(item["name"]) is None
                           or not isinstance(item.get("version"), str)
                           or _VERSION_RE.fullmatch(item["version"]) is None
                           or not isinstance(item.get("filename"), str)
                           or _WHEEL_RE.fullmatch(item["filename"]) is None
                           or not isinstance(item.get("sha256"), str)
                           or _SHA256_RE.fullmatch(item["sha256"]) is None
                           or isinstance(item.get("bytes"), bool)
                           or not isinstance(item.get("bytes"), int)
                           or not 1 <= item["bytes"] <= self.config["max_wheel_bytes"]
                           or not isinstance(item.get("url"), str)
                           for item in wheels)
                    or len({item["name"] for item in wheels}) != len(wheels)
                    or sum(item["bytes"] for item in wheels)
                    > self.config["max_total_wheel_bytes"]
                    or receipt.get("wheel_manifest_hash") != _value_hash(wheels)
                    or wheels != parsed_lock["distributions"]):
                raise RepositoryCacheError("dependency image receipt wheels 非法")
            for item in wheels:
                if not _wheel_url_is_allowed(
                        item["url"], self.config["allowed_hosts"],
                        filename=item["filename"]):
                    raise RepositoryCacheError("dependency image receipt wheel URL 非法")
            for field in (
                    "install_manifest_hash", "build_context_hash", "dockerfile_sha256"):
                if (not isinstance(receipt.get(field), str)
                        or _SHA256_RE.fullmatch(receipt[field]) is None):
                    raise RepositoryCacheError(f"dependency image receipt {field} 非法")
            runtime = receipt.get("runtime")
            if (not isinstance(runtime, dict)
                    or set(runtime) != {
                        "identity",
                        "runtime_log_sha256", "runtime_output_sha256",
                        "pip_check_log_sha256"}
                    or any(not isinstance(runtime.get(key), str)
                           or _SHA256_RE.fullmatch(value) is None
                           for key, value in runtime.items() if key != "identity")):
                raise RepositoryCacheError("dependency image runtime receipt 非法")
            expected_manifest_raw = read_artifact_bytes(
                object_path / "installed-manifest.json", max_bytes=128 * 1024 * 1024,
                label="dependency installed manifest",
                progress_guard=self.owner_guard)
            expected_manifest = _strict_json(
                expected_manifest_raw, label="dependency installed manifest")
            files = expected_manifest.get("files") if isinstance(expected_manifest, dict) else None
            if (expected_manifest_raw != _canonical(expected_manifest)
                    or not isinstance(expected_manifest, dict)
                    or set(expected_manifest) != {"version", "files", "manifest_hash"}
                    or expected_manifest.get("version") != 1
                    or not isinstance(files, list) or not files
                    or files != sorted(files, key=lambda item: (
                        item.get("path", "") if isinstance(item, dict) else ""))
                    or any(not isinstance(item, dict)
                           or set(item) != {"path", "sha256", "bytes"}
                           or not isinstance(item.get("path"), str)
                           or _safe_relpath(
                               item["path"], field="dependency installed receipt path",
                               max_depth=128) != item["path"]
                           or not isinstance(item.get("sha256"), str)
                           or _SHA256_RE.fullmatch(item["sha256"]) is None
                           or isinstance(item.get("bytes"), bool)
                           or not isinstance(item.get("bytes"), int)
                           or item["bytes"] < 0
                           for item in files)
                    or len({item["path"] for item in files}) != len(files)
                    or len(files) > self.config["max_installed_files"]
                    or sum(item["bytes"] for item in files)
                    > self.config["max_installed_bytes"]
                    or expected_manifest.get("manifest_hash") != _value_hash(files)
                    or receipt["install_manifest_hash"] != _value_hash(files)):
                raise RepositoryCacheError("dependency installed manifest identity 非法")
            runtime_identity = runtime["identity"]
            runtime_payload = read_artifact_bytes(
                object_path / "runtime" / "runtime.json", max_bytes=64 * 1024,
                label="dependency stored runtime receipt",
                progress_guard=self.owner_guard)
            stored_runtime = _strict_json(
                runtime_payload, label="dependency stored runtime receipt")
            if (runtime_payload != _canonical(stored_runtime)
                    or stored_runtime != runtime_identity
                    or not isinstance(runtime_identity, dict)
                    or set(runtime_identity) != {
                        "implementation", "version", "executable",
                        "installed_manifest_hash"}
                    or runtime_identity.get("implementation") != "cpython"
                    or runtime_identity.get("version") != self.compiler["version"]
                    or runtime_identity.get("installed_manifest_hash")
                    != receipt["install_manifest_hash"]
                    or not isinstance(runtime_identity.get("executable"), str)
                    or not PurePosixPath(runtime_identity["executable"]).is_absolute()
                    or len(runtime_identity["executable"].encode("utf-8")) > 4096
                    or _sha256(runtime_payload) != runtime["runtime_output_sha256"]
                    or _hash_file(object_path / "runtime" / "runtime.log")[0]
                    != runtime["runtime_log_sha256"]
                    or _hash_file(object_path / "check" / "pip-check.log")[0]
                    != runtime["pip_check_log_sha256"]
                    or self._read_exit(object_path / "runtime", "runtime.log") != 0
                    or self._read_exit(object_path / "check", "pip-check.log") != 0):
                raise RepositoryCacheError(
                    "dependency stored runtime evidence identity 漂移")
            dockerfile = (
                f"FROM {receipt['base_image_id']}\n"
                f"COPY site-packages/ {self.config['site_packages_path']}/\n"
                f"LABEL {_CLOSURE_LABEL}=\"{receipt['closure_hash']}\"\n").encode("ascii")
            context_files = [{
                "path": "Dockerfile", "sha256": _sha256(dockerfile),
                "bytes": len(dockerfile), "mode": "0444", "mtime_ns": 0,
            }, *({"path": "site-packages/" + item["path"],
                  "sha256": item["sha256"], "bytes": item["bytes"],
                  "mode": "0444", "mtime_ns": 0}
                 for item in files)]
            context_files = sorted(context_files, key=lambda item: item["path"])
            directory_paths = set()
            for item in context_files:
                parent = PurePosixPath(item["path"]).parent
                while str(parent) != ".":
                    directory_paths.add(str(parent))
                    parent = parent.parent
            context_directories = [
                {"path": path, "mode": "0555", "mtime_ns": 0}
                for path in sorted(directory_paths)]
            context_identity = {
                "version": 1,
                "root": {"mode": "0555", "mtime_ns": 0},
                "directories": context_directories,
                "files": context_files,
            }
            if (receipt["dockerfile_sha256"] != _sha256(dockerfile)
                    or receipt["build_context_hash"] != _value_hash(context_identity)):
                raise RepositoryCacheError("dependency generated build context identity 漂移")
            context_fd = open_directory(
                object_path / "context", label="dependency build context")
            try:
                verify_tree_fd(
                    context_fd,
                    {item["path"]: item["sha256"] for item in context_files},
                    label="dependency build context", exact=True,
                    progress_guard=self.owner_guard)
                context_root = Path(f"/proc/self/fd/{context_fd}")
                root_info = os.fstat(context_fd)
                actual_directories = set()
                if (root_info.st_uid != os.geteuid()
                        or stat.S_IMODE(root_info.st_mode) != 0o555
                        or root_info.st_mtime_ns != 0):
                    raise RepositoryCacheError(
                        "dependency build context root metadata 漂移")
                for current, dirs, disk_files in os.walk(
                        context_root, topdown=True, followlinks=False):
                    for name in dirs:
                        path = Path(current) / name
                        info = os.lstat(path)
                        rel = str(path.relative_to(context_root)).replace(os.sep, "/")
                        actual_directories.add(rel)
                        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                                or info.st_uid != os.geteuid()
                                or stat.S_IMODE(info.st_mode) != 0o555
                                or info.st_mtime_ns != 0):
                            raise RepositoryCacheError(
                                "dependency build context directory metadata 漂移")
                    for name in disk_files:
                        path = Path(current) / name
                        info = os.lstat(path)
                        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                                or info.st_nlink != 1 or info.st_uid != os.geteuid()
                                or stat.S_IMODE(info.st_mode) != 0o444
                                or info.st_mtime_ns != 0):
                            raise RepositoryCacheError(
                                "dependency build context file metadata 漂移")
                if actual_directories != {
                        item["path"] for item in context_directories}:
                    raise RepositoryCacheError(
                        "dependency build context directory 闭包漂移")
            finally:
                os.close(context_fd)
            derived_config = dict(self.bootstrap_sandbox.config)
            derived_config.update({
                "image": receipt["result_image_id"],
                "image_id": receipt["result_image_id"],
                "payload_environment": receipt["payload_environment"],
            })
            if receipt.get("environment_hash") != sandbox_environment_hash(derived_config):
                raise RepositoryCacheError("dependency image environment_hash 重算不一致")
            archive = receipt.get("image_archive")
            if (not isinstance(archive, dict)
                    or set(archive) != {"sha256", "bytes"}
                    or not isinstance(archive.get("sha256"), str)
                    or _SHA256_RE.fullmatch(archive["sha256"]) is None
                    or isinstance(archive.get("bytes"), bool)
                    or not isinstance(archive.get("bytes"), int)
                    or not 1 <= archive["bytes"] <= self.config["max_image_archive_bytes"]):
                raise RepositoryCacheError("dependency image archive receipt 非法")
            actual_hash, actual_size = _hash_file(
                object_path / "image.tar", maximum=self.config["max_image_archive_bytes"])
            if (actual_hash, actual_size) != (archive["sha256"], archive["bytes"]):
                raise RepositoryCacheError("dependency image archive bytes 漂移")
            contract = self._contract(receipt)
            return receipt, contract
        except RepositoryCacheError:
            raise
        except (ArtifactCapabilityError, OSError, KeyError, TypeError, ValueError) as error:
            raise RepositoryCacheError("dependency image object 核验失败") from error

    def resolve(self, contract: Mapping[str, Any]) -> DockerExecutionSandbox:
        with self._build_lock:
            return self._resolve_locked(contract)

    def resolve_environment_hash(self, environment_hash: str) -> DockerExecutionSandbox:
        """Resolve only an environment identity emitted by a verified image object."""
        if environment_hash == self.bootstrap_sandbox.environment_hash:
            return self.bootstrap_sandbox
        if (not isinstance(environment_hash, str)
                or _SHA256_RE.fullmatch(environment_hash) is None):
            raise RepositoryCacheError("dependency environment_hash 非法")
        with self._build_lock:
            objects, _staging, _artifacts = self._authority_directories()
            matches = []
            object_paths = sorted(objects.iterdir())
            if len(object_paths) > self.config["max_cached_images"]:
                raise RepositoryCacheError("dependency image object 数量超过 policy")
            for object_path in object_paths:
                info = os.lstat(object_path)
                if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                        or info.st_uid != os.geteuid() or info.st_mode & 0o222):
                    raise RepositoryCacheError(
                        "dependency environment index object authority 非法")
                raw = read_artifact_bytes(
                    object_path / "receipt.json", max_bytes=16 * 1024 * 1024,
                    label="dependency environment index receipt",
                    progress_guard=self.owner_guard)
                receipt = _strict_json(raw, label="dependency environment index receipt")
                if raw != _canonical(receipt):
                    raise RepositoryCacheError(
                        "dependency environment index receipt 非 canonical")
                if isinstance(receipt, dict) and receipt.get("environment_hash") == environment_hash:
                    matches.append(object_path)
            if len(matches) != 1:
                raise RepositoryCacheError(
                    "dependency environment_hash 未绑定唯一 verified image object")
            receipt, contract = self._verify_object(matches[0])
            if receipt["environment_hash"] != environment_hash:
                raise RepositoryCacheError("dependency environment index 竞态漂移")
            return self._resolve_verified(receipt, contract)

    def _resolve_locked(self, contract: Mapping[str, Any]) -> DockerExecutionSandbox:
        self.owner_guard()
        required = {
            "version", "provider", "closure_hash", "receipt_hash",
            "environment_hash", "image", "image_id",
        }
        if (not isinstance(contract, Mapping) or set(contract) != required
                or contract.get("version") != 1 or contract.get("provider") != _PROVIDER
                or not isinstance(contract.get("closure_hash"), str)
                or _SHA256_RE.fullmatch(contract["closure_hash"]) is None
                or not isinstance(contract.get("receipt_hash"), str)
                or _SHA256_RE.fullmatch(contract["receipt_hash"]) is None
                or contract.get("image") != contract.get("image_id")
                or not isinstance(contract.get("image_id"), str)
                or _IMAGE_ID_RE.fullmatch(contract["image_id"]) is None):
            raise RepositoryCacheError("dependency image capability 字段闭包/身份非法")
        objects, _staging, _artifacts = self._authority_directories()
        object_path = objects / contract["closure_hash"].removeprefix("sha256:")
        receipt, expected = self._verify_object(object_path)
        if dict(contract) != expected:
            raise RepositoryCacheError("dependency image capability 与 receipt 不一致")
        return self._resolve_verified(receipt, expected)

    def _resolve_verified(
            self, receipt: Mapping[str, Any],
            contract: Mapping[str, Any]) -> DockerExecutionSandbox:
        """Consume a receipt already verified under the caller-held build lock."""
        objects, _staging, _artifacts = self._authority_directories()
        object_path = objects / receipt["closure_hash"].removeprefix("sha256:")
        if self._inspect_image(receipt["result_image_id"], missing_ok=True) is None:
            self.execution_supervisor.recover_previous_generation()
            restore_root = self.work_root / "state" / "dependency-images" / "restores"
            if not os.path.lexists(restore_root):
                restore_root.mkdir(mode=0o700)
            restore_info = os.lstat(restore_root)
            if (not stat.S_ISDIR(restore_info.st_mode)
                    or stat.S_ISLNK(restore_info.st_mode)
                    or restore_info.st_uid != os.geteuid()
                    or restore_info.st_mode & 0o022):
                raise RepositoryCacheError("dependency image restore root authority 非法")
            for stale in restore_root.iterdir():
                stale_info = os.lstat(stale)
                if (not stat.S_ISDIR(stale_info.st_mode)
                        or stat.S_ISLNK(stale_info.st_mode)
                        or stale_info.st_uid != os.geteuid()
                        or stale_info.st_mode & 0o022):
                    raise RepositoryCacheError(
                        "dependency image stale restore authority 非法")
                _remove_private_tree(stale)
            restore = restore_root / (
                receipt["closure_hash"].removeprefix("sha256:")
                + "." + secrets.token_hex(8))
            restore.mkdir(mode=0o700)
            restore_owner_id = secrets.randbelow((1 << 53) - 1) + 1
            context = {
                "phase": "dependency-image-restore",
                "db_owner_kind": "dependency_image_restore",
                "db_owner_id": restore_owner_id,
            }
            result = self._run_host(
                [self.bootstrap_sandbox.engine_path, "image", "load", "--input",
                 str(object_path / "image.tar")],
                directory=restore, log_name="restore.log", context=context,
                timeout_s=float(self.config["load_timeout_s"]),
                kind="dependency-image-restore")
            if result["exit_code"] != 0:
                raise RepositoryCacheError("dependency image exact archive 恢复失败")
            try:
                _remove_private_tree(restore)
                _fsync_directory(restore_root)
            except OSError as error:
                raise RepositoryCacheError(
                    "dependency image restore staging 清理失败") from error
        base = self._inspect_image(self.bootstrap_sandbox.config["image"])
        if base is None:
            raise RepositoryCacheError("dependency base image 在 resolve 时消失")
        self._verify_result_image(
            image_id=receipt["result_image_id"],
            closure_hash=receipt["closure_hash"], base=base)
        sandbox = self._derived_sandbox(receipt["result_image_id"])
        if sandbox.environment_hash != contract["environment_hash"]:
            raise RepositoryCacheError("dependency image resolved sandbox identity 漂移")
        return sandbox
