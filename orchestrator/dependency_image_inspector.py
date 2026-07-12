"""Policy-independent filesystem inspection for published dependency-image objects."""
from __future__ import annotations

import os
import re
import stat
import urllib.parse
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Mapping, Optional

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
    _normalized_name,
    _wheel_url_is_allowed,
)
from .repository_materialization_common import (
    _SHA256_RE,
    RepositoryCacheError,
    _canonical,
    _safe_relpath,
    _sha256,
    _strict_json,
    _value_hash,
)


_LOCK_BASENAME = "python-wheel-lock.json"
_MAX_RECEIPT_BYTES = 16 * 1024 * 1024
_MAX_LOCK_BYTES = 16 * 1024 * 1024
_MAX_WHEELS = 1024
_MAX_WHEEL_BYTES = 16 * 1024 * 1024 * 1024
_MAX_TOTAL_WHEEL_BYTES = 64 * 1024 * 1024 * 1024
_MAX_INSTALLED_FILES = 1_000_000
_MAX_INSTALLED_BYTES = 128 * 1024 * 1024 * 1024
_MAX_IMAGE_ARCHIVE_BYTES = 256 * 1024 * 1024 * 1024
_MAX_OBJECT_ENTRIES = 3 * _MAX_INSTALLED_FILES + _MAX_WHEELS + 10_000
_MAX_OBJECT_BYTES = (
    _MAX_IMAGE_ARCHIVE_BYTES + 2 * _MAX_INSTALLED_BYTES
    + _MAX_TOTAL_WHEEL_BYTES + 1024 * 1024 * 1024)
_BASE_IMAGE_RE = re.compile(
    r"(?:^[A-Za-z0-9._:/-]+@sha256:[0-9a-f]{64}$|^sha256:[0-9a-f]{64}$)")
_COMPILER_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


def _contract(receipt: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "version": 1,
        "provider": _PROVIDER,
        "closure_hash": receipt["closure_hash"],
        "receipt_hash": _value_hash(receipt),
        "environment_hash": receipt["environment_hash"],
        "image": receipt["result_image_id"],
        "image_id": receipt["result_image_id"],
    }


def _directory_paths(files: list[Mapping[str, Any]]) -> set[str]:
    result: set[str] = set()
    for item in files:
        parent = PurePosixPath(item["path"]).parent
        while str(parent) != ".":
            result.add(str(parent))
            parent = parent.parent
    return result


def _verify_object_authority(
        object_path: Path, owner_guard: Callable[[], None]) -> None:
    root = os.lstat(object_path)
    if (not stat.S_ISDIR(root.st_mode) or stat.S_ISLNK(root.st_mode)
            or root.st_uid != os.geteuid()
            or stat.S_IMODE(root.st_mode) != 0o500):
        raise RepositoryCacheError("dependency image object root authority 非法")
    context_root = object_path / "context"
    entries = 0
    total = 0
    for current, dirs, files in os.walk(
            object_path, topdown=True, followlinks=False):
        owner_guard()
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
        if entries > _MAX_OBJECT_ENTRIES or total > _MAX_OBJECT_BYTES:
            raise RepositoryCacheError(
                "dependency image object entries/bytes 超 provider 上限")


def _verify_exact_tree(
        root: Path, files: list[Mapping[str, Any]], *, label: str,
        root_mode: int, directory_mode: int, file_mode: int,
        owner_guard: Callable[[], None], mtime_ns: Optional[int] = None,
        exact_directories: bool = True) -> None:
    expected_hashes = {item["path"]: item["sha256"] for item in files}
    expected_sizes = {item["path"]: item["bytes"] for item in files}
    expected_directories = _directory_paths(files)
    fd = open_directory(root, label=label)
    try:
        verify_tree_fd(
            fd, expected_hashes, label=label, exact=True,
            progress_guard=owner_guard)
        root_info = os.fstat(fd)
        if (root_info.st_uid != os.geteuid()
                or stat.S_IMODE(root_info.st_mode) != root_mode
                or (mtime_ns is not None and root_info.st_mtime_ns != mtime_ns)):
            raise RepositoryCacheError(f"{label} root metadata 漂移")
        anchored = Path(f"/proc/self/fd/{fd}")
        actual_directories = set()
        for current, dirs, disk_files in os.walk(
                anchored, topdown=True, followlinks=False):
            owner_guard()
            for name in dirs:
                path = Path(current) / name
                info = os.lstat(path)
                rel = path.relative_to(anchored).as_posix()
                actual_directories.add(rel)
                if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                        or info.st_uid != os.geteuid()
                        or stat.S_IMODE(info.st_mode) != directory_mode
                        or (mtime_ns is not None and info.st_mtime_ns != mtime_ns)):
                    raise RepositoryCacheError(f"{label} directory metadata 漂移")
            for name in disk_files:
                path = Path(current) / name
                info = os.lstat(path)
                rel = path.relative_to(anchored).as_posix()
                if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                        or info.st_nlink != 1 or info.st_uid != os.geteuid()
                        or stat.S_IMODE(info.st_mode) != file_mode
                        or info.st_size != expected_sizes.get(rel)
                        or (mtime_ns is not None and info.st_mtime_ns != mtime_ns)):
                    raise RepositoryCacheError(f"{label} file metadata 漂移")
        if exact_directories and actual_directories != expected_directories:
            raise RepositoryCacheError(f"{label} directory 闭包漂移")
    finally:
        os.close(fd)


def _read_exit(
        directory: Path, log_name: str,
        owner_guard: Callable[[], None]) -> int:
    raw = read_artifact_bytes(
        directory / (log_name + ".exit"), max_bytes=32,
        label=f"dependency image {log_name} exit",
        progress_guard=owner_guard)
    try:
        value = int(raw.decode("ascii"))
    except (UnicodeError, ValueError) as error:
        raise RepositoryCacheError("dependency image exit sidecar 非法") from error
    if raw != str(value).encode("ascii"):
        raise RepositoryCacheError("dependency image exit sidecar 非 canonical")
    return value


def _safe_wheel_url(value: Any, *, filename: str) -> bool:
    try:
        hostname = urllib.parse.urlsplit(value).hostname if isinstance(value, str) else None
    except ValueError:
        return False
    return bool(hostname) and _wheel_url_is_allowed(
        value, [hostname], filename=filename)


def inspect_dependency_image_object(
        object_path: Path | str, expected_capability: Optional[Mapping[str, Any]] = None,
        owner_guard: Optional[Callable[[], None]] = None,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Inspect one immutable object using only its files and embedded identities."""
    path = Path(object_path)
    guard = owner_guard or (lambda: None)
    try:
        _verify_object_authority(path, guard)
        raw = read_artifact_bytes(
            path / "receipt.json", max_bytes=_MAX_RECEIPT_BYTES,
            label="dependency image receipt", progress_guard=guard)
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
                or re.fullmatch(r"[0-9a-f]{64}", path.name) is None
                or receipt.get("closure_hash") != "sha256:" + path.name
                or any(not isinstance(receipt.get(key), str)
                       or _SHA256_RE.fullmatch(receipt[key]) is None
                       for key in (
                           "builder_config_hash", "base_environment_hash",
                           "environment_hash", "wheel_manifest_hash",
                           "install_manifest_hash", "build_context_hash",
                           "dockerfile_sha256"))
                or not isinstance(receipt.get("base_image"), str)
                or _BASE_IMAGE_RE.fullmatch(receipt["base_image"]) is None
                or not isinstance(receipt.get("base_image_id"), str)
                or _IMAGE_ID_RE.fullmatch(receipt["base_image_id"]) is None
                or not isinstance(receipt.get("result_image_id"), str)
                or _IMAGE_ID_RE.fullmatch(receipt["result_image_id"]) is None):
            raise RepositoryCacheError("dependency image receipt identity 非法")

        compiler = receipt.get("compiler")
        if (not isinstance(compiler, dict)
                or set(compiler) != {"implementation", "version", "artifact_sha256"}
                or compiler.get("implementation") != "CPython"
                or not isinstance(compiler.get("version"), str)
                or _COMPILER_VERSION_RE.fullmatch(compiler["version"]) is None
                or not isinstance(compiler.get("artifact_sha256"), str)
                or _SHA256_RE.fullmatch(compiler["artifact_sha256"]) is None):
            raise RepositoryCacheError("dependency image receipt compiler identity 非法")
        payload_environment = receipt.get("payload_environment")
        site_packages_path = (
            payload_environment.get("PYTHONPATH")
            if isinstance(payload_environment, dict) else None)
        if (not isinstance(payload_environment, dict) or len(payload_environment) > 64
                or any(not isinstance(key, str) or not key or "=" in key or "\x00" in key
                       or len(key.encode("utf-8")) > 256
                       or not isinstance(value, str) or "\x00" in value
                       or len(value.encode("utf-8")) > 65536
                       for key, value in payload_environment.items())
                or not isinstance(site_packages_path, str)
                or not PurePosixPath(site_packages_path).is_absolute()
                or os.path.normpath(site_packages_path) != site_packages_path
                or "\\" in site_packages_path
                or any(ord(character) < 0x20 or ord(character) == 0x7f
                       for character in site_packages_path)):
            raise RepositoryCacheError("dependency image payload environment 非法")
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
                or PurePosixPath(lock["path"]).name != _LOCK_BASENAME
                or not isinstance(lock.get("sha256"), str)
                or _SHA256_RE.fullmatch(lock["sha256"]) is None
                or not isinstance(lock.get("canonical_hash"), str)
                or _SHA256_RE.fullmatch(lock["canonical_hash"]) is None
                or isinstance(lock.get("bytes"), bool)
                or not isinstance(lock.get("bytes"), int)
                or not 1 <= lock["bytes"] <= _MAX_LOCK_BYTES):
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
        lock_raw = read_artifact_bytes(
            path / _LOCK_BASENAME, expected_hash=lock["sha256"],
            expected_size=lock["bytes"], max_bytes=_MAX_LOCK_BYTES,
            label="dependency stored wheel lock", progress_guard=guard)
        stored_lock = _strict_json(lock_raw, label="dependency stored wheel lock")
        if (lock_raw != _canonical(stored_lock)
                or not isinstance(stored_lock, dict)
                or set(stored_lock) != {"version", "python", "platform", "distributions"}
                or stored_lock.get("version") != 1
                or stored_lock.get("python") != compiler
                or stored_lock.get("platform") != {"os": "linux", "architecture": "amd64"}
                or _value_hash(stored_lock) != lock["canonical_hash"]):
            raise RepositoryCacheError("dependency image stored lock identity 漂移")

        wheels = receipt.get("wheels")
        wheel_keys = {"name", "version", "filename", "url", "sha256", "bytes"}
        if (not isinstance(wheels, list) or not 1 <= len(wheels) <= _MAX_WHEELS
                or wheels != sorted(wheels, key=lambda item: (
                    item.get("name", "") if isinstance(item, dict) else "",
                    item.get("version", "") if isinstance(item, dict) else "",
                    item.get("filename", "") if isinstance(item, dict) else ""))
                or any(not isinstance(item, dict) or set(item) != wheel_keys
                       or not isinstance(item.get("name"), str)
                       or _NAME_RE.fullmatch(item["name"]) is None
                       or _normalized_name(item["name"]) != item["name"]
                       or not isinstance(item.get("version"), str)
                       or _VERSION_RE.fullmatch(item["version"]) is None
                       or not isinstance(item.get("filename"), str)
                       or _WHEEL_RE.fullmatch(item["filename"]) is None
                       or not isinstance(item.get("sha256"), str)
                       or _SHA256_RE.fullmatch(item["sha256"]) is None
                       or isinstance(item.get("bytes"), bool)
                       or not isinstance(item.get("bytes"), int)
                       or not 1 <= item["bytes"] <= _MAX_WHEEL_BYTES
                       or not _safe_wheel_url(item.get("url"), filename=item["filename"])
                       for item in wheels)
                or len({item["name"] for item in wheels}) != len(wheels)
                or len({item["filename"] for item in wheels}) != len(wheels)
                or len({item["sha256"] for item in wheels}) != len(wheels)
                or sum(item["bytes"] for item in wheels) > _MAX_TOTAL_WHEEL_BYTES
                or receipt["wheel_manifest_hash"] != _value_hash(wheels)
                or stored_lock.get("distributions") != wheels):
            raise RepositoryCacheError("dependency image receipt wheels 非法")
        wheel_files = [
            {"path": item["filename"], "sha256": item["sha256"], "bytes": item["bytes"]}
            for item in wheels]
        _verify_exact_tree(
            path / "wheelhouse", wheel_files, label="dependency wheelhouse",
            root_mode=0o500, directory_mode=0o500, file_mode=0o400,
            owner_guard=guard)

        manifest_raw = read_artifact_bytes(
            path / "installed-manifest.json", max_bytes=128 * 1024 * 1024,
            label="dependency installed manifest", progress_guard=guard)
        manifest = _strict_json(manifest_raw, label="dependency installed manifest")
        files = manifest.get("files") if isinstance(manifest, dict) else None
        if (manifest_raw != _canonical(manifest)
                or not isinstance(manifest, dict)
                or set(manifest) != {"version", "files", "manifest_hash"}
                or manifest.get("version") != 1
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
                       or not isinstance(item.get("bytes"), int) or item["bytes"] < 0
                       for item in files)
                or len({item["path"] for item in files}) != len(files)
                or len(files) > _MAX_INSTALLED_FILES
                or sum(item["bytes"] for item in files) > _MAX_INSTALLED_BYTES
                or manifest.get("manifest_hash") != _value_hash(files)
                or receipt["install_manifest_hash"] != _value_hash(files)):
            raise RepositoryCacheError("dependency installed manifest identity 非法")
        _verify_exact_tree(
            path / "install" / "site-packages", files,
            label="dependency installed tree", root_mode=0o500,
            directory_mode=0o500, file_mode=0o400, owner_guard=guard,
            exact_directories=False)

        runtime = receipt.get("runtime")
        if (not isinstance(runtime, dict)
                or set(runtime) != {
                    "identity", "runtime_log_sha256", "runtime_output_sha256",
                    "pip_check_log_sha256"}
                or any(not isinstance(runtime.get(key), str)
                       or _SHA256_RE.fullmatch(runtime[key]) is None
                       for key in (
                           "runtime_log_sha256", "runtime_output_sha256",
                           "pip_check_log_sha256"))):
            raise RepositoryCacheError("dependency image runtime receipt 非法")
        runtime_identity = runtime["identity"]
        runtime_payload = read_artifact_bytes(
            path / "runtime" / "runtime.json", max_bytes=64 * 1024,
            label="dependency stored runtime receipt", progress_guard=guard)
        stored_runtime = _strict_json(
            runtime_payload, label="dependency stored runtime receipt")
        if (runtime_payload != _canonical(stored_runtime)
                or stored_runtime != runtime_identity
                or not isinstance(runtime_identity, dict)
                or set(runtime_identity) != {
                    "implementation", "version", "executable",
                    "installed_manifest_hash"}
                or runtime_identity.get("implementation") != "cpython"
                or runtime_identity.get("version") != compiler["version"]
                or runtime_identity.get("installed_manifest_hash")
                != receipt["install_manifest_hash"]
                or not isinstance(runtime_identity.get("executable"), str)
                or not PurePosixPath(runtime_identity["executable"]).is_absolute()
                or "\x00" in runtime_identity["executable"]
                or len(runtime_identity["executable"].encode("utf-8")) > 4096
                or _sha256(runtime_payload) != runtime["runtime_output_sha256"]
                or _hash_file(
                    path / "runtime" / "runtime.log",
                    progress_guard=guard)[0]
                != runtime["runtime_log_sha256"]
                or _hash_file(
                    path / "check" / "pip-check.log",
                    progress_guard=guard)[0]
                != runtime["pip_check_log_sha256"]
                or _read_exit(path / "runtime", "runtime.log", guard) != 0
                or _read_exit(path / "check", "pip-check.log", guard) != 0):
            raise RepositoryCacheError(
                "dependency stored runtime evidence identity 漂移")

        dockerfile = (
            f"FROM {receipt['base_image_id']}\n"
            f"COPY site-packages/ {site_packages_path}/\n"
            f"LABEL {_CLOSURE_LABEL}=\"{receipt['closure_hash']}\"\n").encode("ascii")
        context_files = [{
            "path": "Dockerfile", "sha256": _sha256(dockerfile),
            "bytes": len(dockerfile), "mode": "0444", "mtime_ns": 0,
        }, *({"path": "site-packages/" + item["path"],
              "sha256": item["sha256"], "bytes": item["bytes"],
              "mode": "0444", "mtime_ns": 0}
             for item in files)]
        context_files = sorted(context_files, key=lambda item: item["path"])
        context_directories = [
            {"path": item, "mode": "0555", "mtime_ns": 0}
            for item in sorted(_directory_paths(context_files))]
        context_identity = {
            "version": 1,
            "root": {"mode": "0555", "mtime_ns": 0},
            "directories": context_directories,
            "files": context_files,
        }
        if (receipt["dockerfile_sha256"] != _sha256(dockerfile)
                or receipt["build_context_hash"] != _value_hash(context_identity)):
            raise RepositoryCacheError("dependency generated build context identity 漂移")
        _verify_exact_tree(
            path / "context", context_files, label="dependency build context",
            root_mode=0o555, directory_mode=0o555, file_mode=0o444,
            owner_guard=guard, mtime_ns=0)

        archive = receipt.get("image_archive")
        if (not isinstance(archive, dict) or set(archive) != {"sha256", "bytes"}
                or not isinstance(archive.get("sha256"), str)
                or _SHA256_RE.fullmatch(archive["sha256"]) is None
                or isinstance(archive.get("bytes"), bool)
                or not isinstance(archive.get("bytes"), int)
                or not 1 <= archive["bytes"] <= _MAX_IMAGE_ARCHIVE_BYTES):
            raise RepositoryCacheError("dependency image archive receipt 非法")
        actual_hash, actual_size = _hash_file(
            path / "image.tar", maximum=_MAX_IMAGE_ARCHIVE_BYTES,
            progress_guard=guard)
        if (actual_hash, actual_size) != (archive["sha256"], archive["bytes"]):
            raise RepositoryCacheError("dependency image archive bytes 漂移")

        capability = _contract(receipt)
        if expected_capability is not None and (
                not isinstance(expected_capability, Mapping)
                or dict(expected_capability) != capability):
            raise RepositoryCacheError("dependency image capability 与 receipt 不一致")
        return receipt, capability
    except RepositoryCacheError:
        raise
    except (ArtifactCapabilityError, OSError, KeyError, TypeError, ValueError) as error:
        raise RepositoryCacheError("dependency image object 核验失败") from error
