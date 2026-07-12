"""Dependency-image execution, Docker control, and authority primitives."""
from __future__ import annotations

import json
import os
import re
import secrets
import stat
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from . import harness as H
from .artifact_capability import read_artifact_bytes
from .dependency_image_common import (
    _CLOSURE_LABEL,
    _IMAGE_ID_RE,
    _hash_file,
)
from .execution_sandbox import (
    DockerExecutionSandbox,
    _bounded_text,
    _engine,
    _safe_engine_env,
    sandbox_environment_hash,
)
from .repository_materialization_common import (
    RepositoryCacheError, RepositoryMaterializationError,
    RepositoryTransportError, _safe_relpath,
)


class _DependencyImageRuntimeMixin:
    """Host contract: builder config, bootstrap sandbox, supervisor, locks and guards."""

    def _verify_object_authority(self, object_path: Path) -> None:
        root = os.lstat(object_path)
        if (not stat.S_ISDIR(root.st_mode) or stat.S_ISLNK(root.st_mode)
                or root.st_uid != os.geteuid()
                or stat.S_IMODE(root.st_mode) != 0o500):
            raise RepositoryCacheError(
                "dependency image object root authority 非法")
        context_root = object_path / "context"
        entries = 0
        total = 0
        maximum_entries = (
            3 * self.config["max_installed_files"]
            + self.config["max_wheels"] + 10_000)
        maximum_bytes = (
            self.config["max_image_archive_bytes"]
            + 2 * self.config["max_installed_bytes"]
            + self.config["max_total_wheel_bytes"] + 1024 * 1024 * 1024)
        for current, dirs, files in os.walk(
                object_path, topdown=True, followlinks=False):
            for name in dirs:
                path = Path(current) / name
                info = os.lstat(path)
                entries += 1
                in_context = path == context_root or context_root in path.parents
                expected_mode = 0o555 if in_context else 0o500
                if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                        or info.st_uid != os.geteuid()
                        or stat.S_IMODE(info.st_mode) != expected_mode):
                    raise RepositoryCacheError(
                        "dependency image object directory authority 非法")
            for name in files:
                path = Path(current) / name
                info = os.lstat(path)
                entries += 1
                total += max(info.st_size, 0)
                in_context = context_root in path.parents
                expected_mode = 0o444 if in_context else 0o400
                if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                        or info.st_nlink != 1 or info.st_uid != os.geteuid()
                        or stat.S_IMODE(info.st_mode) != expected_mode):
                    raise RepositoryCacheError(
                        "dependency image object file authority 非法")
            if entries > maximum_entries or total > maximum_bytes:
                raise RepositoryCacheError(
                    "dependency image object entries/bytes 超 policy-derived 上限")

    def _tree_ledger(self, root: Path) -> list[Dict[str, Any]]:
        if not os.path.lexists(root) or stat.S_ISLNK(os.lstat(root).st_mode):
            raise RepositoryCacheError("dependency installed tree 缺失/非法")
        ledger = []
        total = 0
        entries = 0
        for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
            dirs.sort()
            files.sort()
            for name in dirs:
                path = Path(current) / name
                if stat.S_ISLNK(os.lstat(path).st_mode):
                    raise RepositoryMaterializationError("dependency installed tree 含 symlink 目录")
                entries += 1
                if entries > self.config["max_installed_files"]:
                    raise RepositoryMaterializationError(
                        "dependency installed tree 超 policy")
            for name in files:
                path = Path(current) / name
                rel = str(path.relative_to(root)).replace(os.sep, "/")
                _safe_relpath(rel, field="dependency installed path", max_depth=128)
                info = os.lstat(path)
                entries += 1
                if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
                        or info.st_nlink != 1):
                    raise RepositoryMaterializationError(
                        "dependency installed tree 只接受单链接常规文件")
                total += info.st_size
                if (entries > self.config["max_installed_files"]
                        or total > self.config["max_installed_bytes"]):
                    raise RepositoryMaterializationError("dependency installed tree 超 policy")
                content_hash, size = _hash_file(path)
                ledger.append({"path": rel, "sha256": content_hash, "bytes": size})
        if not ledger:
            raise RepositoryMaterializationError("dependency installed tree 为空")
        return sorted(ledger, key=lambda item: item["path"])

    @staticmethod
    def _read_exit(directory: Path, log_name: str) -> int:
        raw = read_artifact_bytes(
            directory / (log_name + ".exit"), max_bytes=32,
            label=f"dependency image {log_name} exit")
        try:
            value = int(raw.decode("ascii"))
        except (UnicodeError, ValueError) as error:
            raise RepositoryCacheError("dependency image exit sidecar 非法") from error
        if raw != str(value).encode("ascii"):
            raise RepositoryCacheError("dependency image exit sidecar 非 canonical")
        return value

    def _run_sandbox(
            self, sandbox: DockerExecutionSandbox, command: list[str], *,
            directory: Path, log_name: str, context: Dict[str, Any],
            timeout_s: float, env: Optional[Dict[str, str]] = None,
            fd_expectations=(), tree_expectations=()) -> Dict[str, Any]:
        if (directory / log_name).exists():
            return {"exit_code": self._read_exit(directory, log_name),
                    "log_path": str(directory / log_name),
                    "log_sha256": _hash_file(directory / log_name)[0].removeprefix("sha256:")}
        recovered = H.recover_staged_result(
            staging_dir=str(directory), log_name=log_name,
            execution_supervisor=self.execution_supervisor,
            execution_kind="dependency-image-sandbox", execution_context=context,
            execution_sandbox=sandbox)
        if recovered is not None:
            return recovered
        invocation = sandbox.prepare(
            command, staging_dir=directory, log_name=log_name, env=env,
            timeout_s=timeout_s, fd_expectations=fd_expectations,
            tree_expectations=tree_expectations,
            execution_context={**context, "log_name": log_name},
            execution_supervisor=self.execution_supervisor)
        try:
            return H.run_staged(
                invocation.argv, staging_dir=str(directory), log_name=log_name,
                timeout_s=timeout_s, env=invocation.env,
                pass_fds=invocation.pass_fds,
                execution_supervisor=self.execution_supervisor,
                execution_kind="dependency-image-sandbox",
                execution_context=context, sandbox_invocation=invocation)
        finally:
            invocation.close()

    def _run_host(
            self, command: list[str], *, directory: Path, log_name: str,
            context: Dict[str, Any], timeout_s: float, kind: str) -> Dict[str, Any]:
        if (directory / log_name).exists():
            return {"exit_code": self._read_exit(directory, log_name),
                    "log_path": str(directory / log_name),
                    "log_sha256": _hash_file(directory / log_name)[0].removeprefix("sha256:")}
        recovered = H.recover_staged_result(
            staging_dir=str(directory), log_name=log_name,
            execution_supervisor=self.execution_supervisor,
            execution_kind=kind, execution_context=context)
        if recovered is not None:
            return recovered
        host_env = _safe_engine_env(
            self.bootstrap_sandbox.config["engine_host"])
        host_env["DOCKER_BUILDKIT"] = "0"
        return H.run_staged(
            command, staging_dir=str(directory), log_name=log_name,
            timeout_s=timeout_s, env=host_env,
            execution_supervisor=self.execution_supervisor,
            execution_kind=kind, execution_context=context,
            inherit_environment=False)

    def _inspect_image(self, reference: str, *, missing_ok: bool = False) -> Optional[Dict[str, Any]]:
        result = _engine(
            self.bootstrap_sandbox.engine_path,
            self.bootstrap_sandbox.config["engine_host"],
            ["image", "inspect", reference, "--format", "{{json .}}"], timeout=30.0)
        if missing_ok and result.returncode != 0:
            stderr = result.stderr if isinstance(result.stderr, bytes) else b""
            if len(stderr) <= 64 * 1024:
                detail = stderr.decode("utf-8", errors="replace")
                if re.search(r"(?:No such image|No such object):?\s", detail, re.IGNORECASE):
                    return None
            raise RepositoryTransportError(
                "dependency image inspect 失败且未证明 image missing")
        text = _bounded_text(result, what="dependency image inspect")
        try:
            value = json.loads(text)
        except json.JSONDecodeError as error:
            raise RepositoryCacheError("dependency image inspect 非 JSON") from error
        if not isinstance(value, dict):
            raise RepositoryCacheError("dependency image inspect 非 object")
        return value

    def _engine_identity(self) -> Dict[str, str]:
        text = _bounded_text(_engine(
            self.bootstrap_sandbox.engine_path,
            self.bootstrap_sandbox.config["engine_host"],
            ["version", "--format",
             "{{.Client.Version}}|{{.Server.Version}}|{{.Server.Os}}|{{.Server.Arch}}"],
            timeout=30.0), what="dependency image docker version")
        values = text.split("|")
        if (len(values) != 4 or any(not value or len(value) > 128 for value in values)
                or values[2:] != ["linux", "amd64"]):
            raise RepositoryTransportError("dependency image Docker engine identity 非法")
        return dict(zip(("client_version", "server_version", "os", "architecture"), values))

    def _closure_image_ids(self, closure_hash: str) -> list[str]:
        text = _bounded_text(_engine(
            self.bootstrap_sandbox.engine_path,
            self.bootstrap_sandbox.config["engine_host"],
            ["image", "ls", "--no-trunc", "--quiet", "--filter",
             f"label={_CLOSURE_LABEL}={closure_hash}"],
            timeout=30.0), what="dependency uncommitted image inventory")
        values = [] if not text else text.splitlines()
        if (len(values) > self.config["max_cached_images"] + 1
                or len(values) != len(set(values))
                or any(_IMAGE_ID_RE.fullmatch(value) is None for value in values)):
            raise RepositoryCacheError(
                "dependency uncommitted image inventory 非法/超上限")
        for image_id in values:
            if image_id == self.bootstrap_sandbox.config["image_id"]:
                raise RepositoryCacheError(
                    "dependency base image 意外带有 derived closure label")
            image = self._inspect_image(image_id)
            labels = (image.get("Config") or {}).get("Labels") if image else None
            if not isinstance(labels, dict) or labels.get(_CLOSURE_LABEL) != closure_hash:
                raise RepositoryCacheError(
                    "dependency image inventory label 竞态漂移")
        return values

    def _discard_uncommitted_images(self, *, stage: Path, closure_hash: str) -> None:
        """Delete only locally labelled results that have no published object."""
        image_ids = self._closure_image_ids(closure_hash)
        if not image_ids:
            return
        info = os.lstat(stage)
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid != os.geteuid() or info.st_mode & 0o022):
            raise RepositoryCacheError(
                "dependency uncommitted cleanup staging authority 非法")
        os.chmod(stage, 0o700)
        cleanup = stage / (".image-cleanup." + secrets.token_hex(8))
        cleanup.mkdir(mode=0o700)
        context = {
            "phase": "dependency-image-discard",
            "db_owner_kind": "dependency_image_discard",
            "db_owner_id": secrets.randbelow((1 << 53) - 1) + 1,
        }
        result = self._run_host(
            [self.bootstrap_sandbox.engine_path, "image", "rm", *image_ids],
            directory=cleanup, log_name="discard.log", context=context,
            timeout_s=float(self.config["load_timeout_s"]),
            kind="dependency-image-discard")
        if result["exit_code"] != 0 or self._closure_image_ids(closure_hash):
            raise RepositoryCacheError(
                "dependency uncommitted image 无法精确清理")

    def _derived_sandbox(self, image_id: str) -> DockerExecutionSandbox:
        config = dict(self.bootstrap_sandbox.config)
        config.update({
            "image": image_id,
            "image_id": image_id,
            "payload_environment": {
                **config["payload_environment"],
                "PYTHONPATH": self.config["site_packages_path"],
            },
        })
        environment_hash = sandbox_environment_hash(config)
        cached = self._sandboxes.get(environment_hash)
        if cached is not None:
            return cached
        sandbox = DockerExecutionSandbox(
            work_root=self.work_root, config=config, owner_guard=self.owner_guard,
            system_root=self.bootstrap_sandbox.system_root,
            gpu_contract=self.bootstrap_sandbox.gpu_contract)
        sandbox.preflight()
        self._sandboxes[environment_hash] = sandbox
        return sandbox

    def _verify_result_image(
            self, *, image_id: str, closure_hash: str,
            base: Mapping[str, Any]) -> Dict[str, Any]:
        result = self._inspect_image(image_id)
        if result is None:
            raise RepositoryCacheError("dependency result image 在验收时消失")
        base_root = base.get("RootFS")
        result_root = result.get("RootFS")
        base_config = base.get("Config")
        result_config = result.get("Config")
        if (result.get("Id") != image_id or result.get("Os") != "linux"
                or result.get("Architecture") != "amd64"
                or not isinstance(base_root, dict) or not isinstance(result_root, dict)
                or base_root.get("Type") != "layers" or result_root.get("Type") != "layers"
                or not isinstance(base_root.get("Layers"), list)
                or result_root.get("Layers", [])[:-1] != base_root["Layers"]
                or len(result_root.get("Layers", [])) != len(base_root["Layers"]) + 1
                or not isinstance(base_config, dict) or not isinstance(result_config, dict)):
            raise RepositoryCacheError("dependency result image platform/layer ancestry 非法")
        expected_config = dict(base_config)
        expected_labels = dict(expected_config.get("Labels") or {})
        expected_labels[_CLOSURE_LABEL] = closure_hash
        expected_config["Labels"] = expected_labels
        # The legacy builder rewrites this deprecated provenance-only field to
        # the ephemeral COPY-step image ID (the following LABEL step is the
        # final image).  It is not container runtime configuration; require an
        # exact content ID, then compare every other Config field byte-for-byte.
        legacy_parent = result_config.get("Image")
        if (legacy_parent != base_config.get("Image")
                and (not isinstance(legacy_parent, str)
                     or _IMAGE_ID_RE.fullmatch(legacy_parent) is None)):
            raise RepositoryCacheError(
                "dependency result image Config.Image legacy parent 非 exact ID")
        expected_config["Image"] = legacy_parent
        if result_config != expected_config:
            drift = sorted({
                *expected_config, *result_config} - {
                    key for key in {*expected_config, *result_config}
                    if expected_config.get(key) == result_config.get(key)})
            raise RepositoryCacheError(
                f"dependency result image Config/label 闭包漂移: {drift}")
        return result
