"""Exact-image receipt verification, sandbox resolution, and archive restore."""
from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Mapping

from .artifact_capability import (
    ArtifactCapabilityError,
    read_artifact_bytes,
)
from .dependency_image_common import (
    _IMAGE_ID_RE,
    _PROVIDER,
    _wheel_url_is_allowed,
)
from .dependency_image_inspector import inspect_dependency_image_object
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
    _sha256,
    _strict_json,
    _value_hash,
)
from .dependency_image_runtime import _DependencyImageRuntimeMixin


class _DependencyImageStoreMixin(_DependencyImageRuntimeMixin):
    """Verify, resolve, and restore published exact-image capabilities."""

    def _verify_object(self, object_path: Path) -> tuple[Dict[str, Any], Dict[str, Any]]:
        """Apply the live builder policy to a policy-independent file inspection."""
        try:
            self._verify_object_authority(object_path)
            archive_info = os.lstat(object_path / "image.tar")
            if (not stat.S_ISREG(archive_info.st_mode)
                    or stat.S_ISLNK(archive_info.st_mode)
                    or archive_info.st_size > self.config["max_image_archive_bytes"]):
                raise RepositoryCacheError(
                    "dependency image archive 超当前 policy")
            receipt, contract = inspect_dependency_image_object(
                object_path, owner_guard=self.owner_guard)
            lock = receipt["lock"]
            derived_config = self._derived_sandbox_config(
                receipt["result_image_id"])
            expected_payload_environment = derived_config["payload_environment"]
            if (receipt["builder_config_hash"] != self.config_hash
                    or receipt["base_environment_hash"]
                    != self.bootstrap_sandbox.environment_hash
                    or receipt["base_image"] != self.bootstrap_sandbox.config["image"]
                    or receipt["base_image_id"]
                    != self.bootstrap_sandbox.config["image_id"]
                    or receipt["payload_environment"] != expected_payload_environment
                    or receipt["compiler"] != self.compiler
                    or PurePosixPath(lock["path"]).name != self.config["lock_basename"]
                    or lock["bytes"] > self.config["max_lock_bytes"]):
                raise RepositoryCacheError(
                    "dependency image receipt 与当前 builder policy 不一致")

            parsed_lock, stored_lock_raw = self._parse_lock(
                object_path, {
                    "path": self.config["lock_basename"],
                    "sha256": lock["sha256"], "bytes": lock["bytes"],
                })
            wheels = receipt["wheels"]
            if (_sha256(stored_lock_raw) != lock["sha256"]
                    or _value_hash(parsed_lock) != lock["canonical_hash"]
                    or wheels != parsed_lock["distributions"]
                    or len(wheels) > self.config["max_wheels"]
                    or any(item["bytes"] > self.config["max_wheel_bytes"]
                           for item in wheels)
                    or sum(item["bytes"] for item in wheels)
                    > self.config["max_total_wheel_bytes"]):
                raise RepositoryCacheError(
                    "dependency image lock/wheels 与当前 policy 不一致")
            for item in wheels:
                if not _wheel_url_is_allowed(
                        item["url"], self.config["allowed_hosts"],
                        filename=item["filename"]):
                    raise RepositoryCacheError(
                        "dependency image wheel URL 越出当前 allowlist")

            manifest_raw = read_artifact_bytes(
                object_path / "installed-manifest.json",
                max_bytes=128 * 1024 * 1024,
                label="dependency installed policy manifest",
                progress_guard=self.owner_guard)
            manifest = _strict_json(
                manifest_raw, label="dependency installed policy manifest")
            files = manifest.get("files") if isinstance(manifest, dict) else None
            if (not isinstance(files, list)
                    or len(files) > self.config["max_installed_files"]
                    or sum(item["bytes"] for item in files)
                    > self.config["max_installed_bytes"]):
                raise RepositoryCacheError(
                    "dependency installed manifest 超当前 policy")

            archive = receipt["image_archive"]
            if (receipt["environment_hash"]
                    != sandbox_environment_hash(derived_config)
                    or archive["bytes"] > self.config["max_image_archive_bytes"]):
                raise RepositoryCacheError(
                    "dependency image runtime/archive 与当前 policy 不一致")
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
