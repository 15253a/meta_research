"""Exact GitHub commit materialization into a content-addressed host snapshot.

Discovery freezes repository metadata and a 40-hex commit, but deliberately
does not clone or execute it.  This module closes the production hand-off:

* obtain the commit's root tree and every subtree through the read-only Git
  database API, rejecting truncated/ambiguous trees;
* download a commit-addressed source archive, extract it without ``tarfile``
  path/link semantics, and match every regular file to its Git blob SHA-1;
* recursively materialize pinned GitHub submodules under an explicit license
  policy; reject Git LFS pointers until an OID-verified transfer is available;
* validate the repository's declarative adapter v2, allocate stable numeric
  protocol/metric identities, and generate the full supply-chain manifest; and
* atomically publish a file-backed, read-only content-addressed snapshot plus
  a deterministic candidate index.  Database rows contain only bounded hashes.

Network calls are read-only and occur outside SQLite transactions.  A killed
download can be repeated; only a fully verified snapshot receives a durable
index and may enter the adversarial execution sandbox.
"""
from __future__ import annotations

import base64
import configparser
import errno
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import stat
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

from .artifact_capability import (
    ArtifactCapabilityError,
    open_artifact,
    open_directory,
    read_artifact_bytes,
    verify_tree_fd,
)
_PROTOCOL = "github-repository-snapshot-v1"
_ADAPTER_VERSION = 2
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_FULL_NAME_RE = re.compile(
    r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_GITHUB_URI_RE = re.compile(
    r"^https://github\.com/([A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100})$")
_LOG_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_ARTIFACT_TYPES = frozenset({
    "checkpoint", "external_model", "prompt_only", "algorithm",
    "retrieval_index",
})
_LFS_VERSION = "https://git-lfs.github.com/spec/v1"
_MAX_ADAPTER_BYTES = 1024 * 1024
_MAX_GITMODULES_BYTES = 1024 * 1024
_MAX_SEARCH_SNAPSHOT_BYTES = 2 * 1024 * 1024
_MAX_RECEIPT_BYTES = 128 * 1024 * 1024
_CONTROL_ENV_KEYS = (
    "HOME", "LANG", "LC_ALL", "PATH", "PYTHONDONTWRITEBYTECODE",
)


class RepositoryMaterializationError(ValueError):
    """The frozen repository cannot be materialized without guessing."""


class RepositoryTransportError(RuntimeError):
    """Retryable provider/runtime transport failure; never settle a candidate."""


class RepositoryCacheError(RuntimeError):
    """Local authority/cache corruption; never blame or settle the candidate."""


def _canonical(value: Any) -> bytes:
    return (json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False) + "\n").encode("utf-8")


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _value_hash(value: Any) -> str:
    return _sha256(_canonical(value))


def _atomic_write_json(path: Path, value: Any, *, maximum: int) -> None:
    """Publish a bounded canonical JSON object under an already trusted parent."""
    try:
        payload = _canonical(value)
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise RepositoryMaterializationError(
            f"{path.name} 不是有限 JSON") from error
    if len(payload) > maximum:
        raise RepositoryMaterializationError(
            f"{path.name} 超过 {maximum} bytes")
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
             | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    directory_fd = os.open(
        path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    temporary = f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    fd = -1
    try:
        info = os.fstat(directory_fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise RepositoryCacheError(
                f"{path.name} parent authority 非法")
        fd = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("canonical JSON short write")
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(
            temporary, path.name, src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except OSError:
            pass
        os.close(directory_fd)


def _fsync_directory(path: Path) -> None:
    fd = os.open(
        path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise RepositoryCacheError(
                "materialization fsync target 非目录")
        os.fsync(fd)
    finally:
        os.close(fd)


def _remove_private_tree(path: Path) -> None:
    if not os.path.lexists(path):
        return
    for current, dirs, _files in os.walk(path, topdown=True, followlinks=False):
        os.chmod(current, 0o700)
        for name in dirs:
            child = Path(current) / name
            if stat.S_ISLNK(os.lstat(child).st_mode):
                continue
            os.chmod(child, 0o700)
    shutil.rmtree(path)


def _strict_json(raw: bytes, *, label: str) -> Any:
    def unique(pairs):  # noqa: ANN001
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key {key!r}")
            result[key] = value
        return result

    try:
        return json.loads(
            raw.decode("utf-8"), object_pairs_hook=unique,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non-finite {token}")))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError,
            RecursionError) as error:
        raise RepositoryMaterializationError(
            f"{label} 不是严格 UTF-8 JSON") from error


def _bounded_string(value: Any, *, field: str, max_bytes: int) -> str:
    if not isinstance(value, str) or not value:
        raise RepositoryMaterializationError(f"{field} 须为非空字符串")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise RepositoryMaterializationError(f"{field} 不是合法 UTF-8") from error
    if (len(encoded) > max_bytes
            or any(ord(char) < 0x20 or ord(char) == 0x7f for char in value)):
        raise RepositoryMaterializationError(f"{field} 超出文本边界")
    return value


def _safe_relpath(value: Any, *, field: str, max_depth: int) -> str:
    raw = _bounded_string(value, field=field, max_bytes=4096)
    if "\\" in raw:
        raise RepositoryMaterializationError(f"{field} 不得含反斜线")
    path = PurePosixPath(raw)
    parts = raw.split("/")
    if (path.is_absolute() or len(parts) > max_depth
            or any(part in ("", ".", "..") for part in parts)):
        raise RepositoryMaterializationError(f"{field} 非安全相对路径")
    return path.as_posix()


def _safe_component(value: Any, *, field: str) -> str:
    raw = _bounded_string(value, field=field, max_bytes=1024)
    if "/" in raw or "\\" in raw or raw in (".", ".."):
        raise RepositoryMaterializationError(f"{field} 非安全 Git tree component")
    return raw


def _positive_int(value: Any, *, field: str, maximum: Optional[int] = None) -> int:
    if (isinstance(value, bool) or not isinstance(value, int) or value <= 0
            or (maximum is not None and value > maximum)):
        raise RepositoryMaterializationError(f"{field} 须为有界正整数")
    return value


def _stable_id(namespace: str, value: Any) -> int:
    digest = hashlib.sha256(namespace.encode("ascii") + b"\0" + _canonical(value)).digest()
    # These IDs cross JSON/UI boundaries as well as SQLite.  Staying within
    # IEEE-754's exact-integer range prevents a browser/client from silently
    # rounding a protocol or metric foreign key.  Semantic collision checks
    # remain mandatory even with the 53-bit family space.
    result = int.from_bytes(digest[:8], "big") & ((1 << 53) - 1)
    return result or 1


def _git_blob_sha1(payload: bytes) -> str:
    header = f"blob {len(payload)}\0".encode("ascii")
    return hashlib.sha1(header + payload).hexdigest()  # noqa: S324 - Git object identity


def _git_tree_sha1(entries: Sequence[Mapping[str, Any]]) -> str:
    chunks = []
    for entry in sorted(
            entries,
            key=lambda item: (item["name"].encode("utf-8")
                              + (b"/" if item["type"] == "tree" else b""))):
        mode = "40000" if entry["mode"] == "040000" else entry["mode"]
        chunks.append(
            mode.encode("ascii") + b" " + entry["name"].encode("utf-8")
            + b"\0" + bytes.fromhex(entry["sha"]))
    content = b"".join(chunks)
    header = f"tree {len(content)}\0".encode("ascii")
    return hashlib.sha1(header + content).hexdigest()  # noqa: S324 - Git object identity


def _parse_lfs_pointer(payload: bytes) -> Optional[Dict[str, Any]]:
    if not payload or len(payload) > 1024:
        return None
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return None
    # Git's line scanner accepts LF/CRLF and a final line without a newline.
    # Detection must be at least as broad or a valid pointer could be mistaken
    # for the model bytes that it references.
    lines = text.splitlines()
    if not lines or lines[0] != f"version {_LFS_VERSION}":
        return None
    values = {}
    for line in lines[1:]:
        if " " not in line:
            return None
        key, value = line.split(" ", 1)
        if (not re.fullmatch(r"[a-z0-9.-]+", key) or not value
                or key in values):
            return None
        values[key] = value
    oid = values.get("oid")
    size = values.get("size")
    if (not isinstance(oid, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", oid)
            or not isinstance(size, str) or not size.isascii() or not size.isdigit()):
        return None
    numeric_size = int(size)
    if numeric_size < 0:
        return None
    return {"oid": oid, "size": numeric_size}


class _ApiRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        target = urllib.parse.urlsplit(newurl)
        if target.scheme != "https" or target.hostname != "api.github.com":
            raise urllib.error.HTTPError(
                req.full_url, code, "GitHub API redirect escaped api.github.com",
                headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _ArchiveRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts: Sequence[str]):
        super().__init__()
        self.allowed_hosts = frozenset(allowed_hosts)

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        target = urllib.parse.urlsplit(newurl)
        if (target.scheme != "https" or target.hostname not in self.allowed_hosts
                or target.username or target.password or target.port not in (None, 443)):
            raise urllib.error.HTTPError(
                req.full_url, code, "GitHub archive redirect escaped allowlist",
                headers, fp)
        clean_headers = {
            key: value for key, value in req.headers.items()
            if key.lower() != "authorization"}
        return urllib.request.Request(
            newurl, headers=clean_headers, method="GET",
            origin_req_host=req.origin_req_host, unverifiable=True)


class GitHubRepositoryMaterializer:
    """Materialize one exact GitHub commit and repository adapter v2."""

    name = "github_archive_v1"

    def __init__(
            self, *, work_root: Path | str, config: Mapping[str, Any],
            sandbox_config: Mapping[str, Any], auto_license: Mapping[str, Any],
            runtime_environment: Mapping[str, str],
            owner_guard: Optional[Callable[[], None]] = None,
            api_getter: Optional[Callable[[str, str], Any]] = None,
            archive_fetcher: Optional[
                Callable[[str, str, Path, int], Mapping[str, Any]]] = None,
            token_env: str = "METARESEARCH_GITHUB_TOKEN"):
        self.work_root = Path(os.path.abspath(os.fspath(work_root)))
        self.config = dict(config)
        self.sandbox_config = dict(sandbox_config)
        self.auto_license = dict(auto_license)
        self.runtime_environment = dict(runtime_environment)
        self.owner_guard = owner_guard or (lambda: None)
        self.token_env = token_env
        self._validate_config()
        self.api_getter = api_getter
        self.archive_fetcher = archive_fetcher
        self._api_opener = urllib.request.build_opener(_ApiRedirectHandler()).open
        self._archive_opener = urllib.request.build_opener(
            _ArchiveRedirectHandler(self.config["allowed_archive_hosts"])).open

    def _validate_config(self) -> None:
        required = {
            "provider", "adapter_path", "timeout_s", "max_api_response_bytes",
            "max_archive_bytes", "max_file_bytes", "max_total_bytes", "max_files",
            "max_tree_depth", "max_tree_objects", "max_submodules", "lfs_policy",
            "allowed_archive_hosts", "dependency_lock_names", "compiler",
        }
        if set(self.config) != required or self.config.get("provider") != self.name:
            raise ValueError("policy.import_materialization 字段闭包/provider 非法")
        if self.config.get("adapter_path") != ".meta-research/import-adapter.json":
            raise ValueError("import adapter_path 非冻结 v2 路径")
        bounds = {
            "max_api_response_bytes": (65536, 16777216),
            "max_archive_bytes": (1048576, 68719476736),
            "max_file_bytes": (1, 17179869184),
            "max_total_bytes": (1, 1099511627776),
            "max_files": (1, 100000),
            "max_tree_depth": (1, 128),
            "max_tree_objects": (1, 100000),
            "max_submodules": (0, 256),
        }
        for key, (minimum, maximum) in bounds.items():
            value = self.config[key]
            if (isinstance(value, bool) or not isinstance(value, int)
                    or not minimum <= value <= maximum):
                raise ValueError(
                    f"import_materialization.{key} 越出封闭边界")
        timeout = self.config["timeout_s"]
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(float(timeout))
                or not 1 <= float(timeout) <= 300):
            raise ValueError("import_materialization.timeout_s 非正有限数")
        if self.config["lfs_policy"] != "reject":
            raise ValueError("CP11.4c.2a 只接受显式 lfs_policy=reject")
        hosts = self.config["allowed_archive_hosts"]
        if (not isinstance(hosts, list) or not 1 <= len(hosts) <= 8
                or len(set(hosts)) != len(hosts)
                or any(not isinstance(host, str) or host != host.lower()
                       or re.fullmatch(
                           r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host) is None
                       for host in hosts)):
            raise ValueError("import_materialization archive host allowlist 非法")
        locks = self.config["dependency_lock_names"]
        if (not isinstance(locks, list) or not 1 <= len(locks) <= 64
                or len(set(locks)) != len(locks)
                or any(not isinstance(name, str)
                       or re.fullmatch(r"[A-Za-z0-9._-]{1,128}", name) is None
                       for name in locks)):
            raise ValueError("import_materialization dependency_lock_names 非法")
        compiler = self.config["compiler"]
        if (not isinstance(compiler, dict)
                or set(compiler) != {"implementation", "version", "artifact_sha256"}
                or compiler.get("implementation") != "CPython"
                or not isinstance(compiler.get("version"), str)
                or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", compiler["version"]) is None
                or not isinstance(compiler.get("artifact_sha256"), str)
                or _SHA256_RE.fullmatch(compiler["artifact_sha256"]) is None):
            raise ValueError("import_materialization.compiler identity 非法")
        if (not isinstance(self.auto_license.get("allow_spdx"), list)
                or not isinstance(self.auto_license.get("scope"), dict)):
            raise ValueError("import materializer auto_license 非法")
        for key in ("image", "image_id", "python_path"):
            if key not in self.sandbox_config:
                raise ValueError(f"sandbox config 缺 {key}")
        compiler = self.config["compiler"]
        expected_runtime = {
            "PYTHON_VERSION": compiler["version"],
            "PYTHON_SHA256": compiler["artifact_sha256"].removeprefix("sha256:"),
        }
        if (not all(isinstance(key, str) and isinstance(value, str)
                    for key, value in self.runtime_environment.items())
                or any(self.runtime_environment.get(key) != value
                       for key, value in expected_runtime.items())):
            raise ValueError(
                "pinned sandbox image compiler environment 与 import policy 不一致")

    @property
    def environment_hash(self) -> str:
        return _sha256(_canonical(self.sandbox_config))

    @property
    def config_hash(self) -> str:
        return _value_hash({
            "materialization": self.config,
            "sandbox": self.sandbox_config,
            "auto_license": self.auto_license,
        })

    def _authority_directories(self) -> tuple[Path, Path, Path, Path]:
        current = self.work_root
        root_info = os.lstat(current)
        if (not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode)
                or root_info.st_uid != os.geteuid()):
            raise RepositoryCacheError(
                "import materialization work_root authority 非法")
        paths = []
        for component in ("state", "import-materializations", "staging"):
            current = current / component
            if not os.path.lexists(current):
                current.mkdir(mode=0o700)
            info = os.lstat(current)
            if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                    or info.st_uid != os.geteuid()):
                raise RepositoryCacheError(
                    "import materialization authority directory 非法")
            paths.append(current)
        base = paths[1]
        staging = paths[2]
        children = []
        for component in ("objects", "indexes"):
            child = base / component
            if not os.path.lexists(child):
                child.mkdir(mode=0o700)
            info = os.lstat(child)
            if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                    or info.st_uid != os.geteuid()):
                raise RepositoryCacheError(
                    "import materialization authority directory 非法")
            children.append(child)
        return base, staging, children[0], children[1]

    def _headers(self) -> Dict[str, str]:
        result = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "meta-research-materializer/1",
        }
        token = os.environ.get(self.token_env)
        if token:
            result["Authorization"] = f"Bearer {token}"
        return result

    def _get_json(self, url: str, *, label: str) -> Any:
        self.owner_guard()
        if self.api_getter is not None:
            value = self.api_getter(url, label)
            self.owner_guard()
            return value
        request = urllib.request.Request(
            url, headers=self._headers(), method="GET")
        try:
            response = self._api_opener(
                request, timeout=float(self.config["timeout_s"]))
        except urllib.error.HTTPError as error:
            try:
                error_type = (RepositoryMaterializationError
                              if error.code in (404, 410)
                              else RepositoryTransportError)
                raise error_type(f"GitHub {label} HTTP {error.code}") from error
            finally:
                error.close()
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise RepositoryTransportError(
                f"GitHub {label} 读取失败: {type(error).__name__}") from error
        try:
            final = urllib.parse.urlsplit(response.geturl())
            if final.scheme != "https" or final.hostname != "api.github.com":
                raise RepositoryMaterializationError(
                    f"GitHub {label} final URL 越出 api.github.com")
            maximum = int(self.config["max_api_response_bytes"])
            declared = response.headers.get("Content-Length")
            if declared is not None:
                try:
                    declared_size = int(declared)
                except ValueError as error:
                    raise RepositoryMaterializationError(
                        f"GitHub {label} Content-Length 非整数") from error
                if declared_size < 0 or declared_size > maximum:
                    raise RepositoryMaterializationError(
                        f"GitHub {label} 响应超过上限")
            raw = response.read(maximum + 1)
            if len(raw) > maximum:
                raise RepositoryMaterializationError(
                    f"GitHub {label} 响应超过上限")
        finally:
            response.close()
        try:
            self.owner_guard()
            return _strict_json(raw, label=f"GitHub {label}")
        except RepositoryMaterializationError as error:
            raise RepositoryTransportError(
                f"GitHub {label} 响应不是可解析 JSON") from error

    def _download_archive(
            self, full_name: str, revision: str, destination: Path) -> Dict[str, Any]:
        self.owner_guard()
        maximum = int(self.config["max_archive_bytes"])
        if self.archive_fetcher is not None:
            result = dict(self.archive_fetcher(
                full_name, revision, destination, maximum))
            if (set(result) != {"url", "bytes", "sha256"}
                    or not destination.is_file()):
                raise RepositoryMaterializationError(
                    "test/injected archive_fetcher contract 非法")
            try:
                declared_size = result["bytes"]
                declared_hash = result["sha256"]
                if (isinstance(declared_size, bool)
                        or not isinstance(declared_size, int)
                        or not 0 < declared_size <= maximum
                        or not isinstance(declared_hash, str)
                        or _SHA256_RE.fullmatch(declared_hash) is None
                        or result["url"] != (
                            f"https://api.github.com/repos/{full_name}/tarball/"
                            f"{revision}")):
                    raise RepositoryMaterializationError(
                        "test/injected archive_fetcher receipt 非法")
                with open_artifact(
                        destination, expected_hash=declared_hash,
                        expected_size=declared_size,
                        label="injected GitHub archive"):
                    pass
            except ArtifactCapabilityError as error:
                raise RepositoryMaterializationError(
                    "test/injected archive_fetcher bytes 与 receipt 不一致") from error
            self.owner_guard()
            return result
        url = f"https://api.github.com/repos/{full_name}/tarball/{revision}"
        request = urllib.request.Request(url, headers=self._headers(), method="GET")
        try:
            response = self._archive_opener(
                request, timeout=float(self.config["timeout_s"]))
        except urllib.error.HTTPError as error:
            try:
                error_type = (RepositoryMaterializationError
                              if error.code in (404, 410)
                              else RepositoryTransportError)
                raise error_type(f"GitHub archive HTTP {error.code}") from error
            finally:
                error.close()
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise RepositoryTransportError(
                f"GitHub archive 读取失败: {type(error).__name__}") from error
        digest = hashlib.sha256()
        total = 0
        try:
            final = urllib.parse.urlsplit(response.geturl())
            if (final.scheme != "https"
                    or final.hostname not in self.config["allowed_archive_hosts"]
                    or final.username or final.password or final.port not in (None, 443)):
                raise RepositoryMaterializationError(
                    "GitHub archive final URL 越出 allowlist")
            declared = response.headers.get("Content-Length")
            if declared is not None:
                try:
                    declared_size = int(declared)
                except ValueError as error:
                    raise RepositoryMaterializationError(
                        "GitHub archive Content-Length 非整数") from error
                if declared_size < 0 or declared_size > maximum:
                    raise RepositoryMaterializationError(
                        "GitHub archive 压缩字节超过上限")
            fd = os.open(
                destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                0o600)
            try:
                while True:
                    self.owner_guard()
                    chunk = response.read(min(1024 * 1024, maximum - total + 1))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > maximum:
                        raise RepositoryMaterializationError(
                            "GitHub archive 压缩字节超过上限")
                    digest.update(chunk)
                    view = memoryview(chunk)
                    while view:
                        written = os.write(fd, view)
                        if written <= 0:
                            raise OSError("archive short write")
                        view = view[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            response.close()
        self.owner_guard()
        return {"url": url, "bytes": total,
                "sha256": "sha256:" + digest.hexdigest()}

    def _commit_tree(self, full_name: str, revision: str) -> str:
        payload = self._get_json(
            f"https://api.github.com/repos/{full_name}/git/commits/{revision}",
            label=f"commit {full_name}@{revision}")
        tree = payload.get("tree") if isinstance(payload, dict) else None
        if (not isinstance(payload, dict) or payload.get("sha") != revision
                or not isinstance(tree, dict)
                or not isinstance(tree.get("sha"), str)
                or _COMMIT_RE.fullmatch(tree["sha"]) is None):
            raise RepositoryMaterializationError(
                f"GitHub commit {full_name}@{revision} identity/tree 非法")
        return tree["sha"]

    def _walk_tree(
            self, *, full_name: str, tree_sha: str, prefix: str,
            depth: int, files: Dict[str, Dict[str, Any]],
            submodules: list[Dict[str, Any]], tree_counter: list[int]) -> None:
        if depth > int(self.config["max_tree_depth"]):
            raise RepositoryMaterializationError("Git tree 深度超过 policy")
        tree_counter[0] += 1
        if tree_counter[0] > int(self.config["max_tree_objects"]):
            raise RepositoryMaterializationError(
                "Git tree object 请求数超过 policy")
        payload = self._get_json(
            f"https://api.github.com/repos/{full_name}/git/trees/{tree_sha}",
            label=f"tree {full_name}:{tree_sha}")
        entries = payload.get("tree") if isinstance(payload, dict) else None
        if (not isinstance(payload, dict) or payload.get("sha") != tree_sha
                or payload.get("truncated") is not False
                or not isinstance(entries, list)):
            raise RepositoryMaterializationError(
                f"Git tree {full_name}:{tree_sha} 缺失/截断/身份错配")
        normalized = []
        names = set()
        for index, raw in enumerate(entries):
            if not isinstance(raw, dict):
                raise RepositoryMaterializationError("Git tree entry 非 object")
            name = _safe_component(raw.get("path"), field=f"tree[{index}].path")
            mode, kind, sha = raw.get("mode"), raw.get("type"), raw.get("sha")
            if (mode, kind) not in {
                    ("100644", "blob"), ("100755", "blob"),
                    ("040000", "tree"), ("160000", "commit"),
                    ("120000", "blob")}:
                raise RepositoryMaterializationError(
                    f"Git tree entry {name} mode/type 非法")
            if not isinstance(sha, str) or _COMMIT_RE.fullmatch(sha) is None:
                raise RepositoryMaterializationError(
                    f"Git tree entry {name} sha 非法")
            if name in names:
                raise RepositoryMaterializationError(
                    f"Git tree entry name 重复: {name}")
            names.add(name)
            size = raw.get("size")
            if kind == "blob" and mode != "120000" and (
                    isinstance(size, bool) or not isinstance(size, int)
                    or size < 0 or size > int(self.config["max_file_bytes"])):
                raise RepositoryMaterializationError(
                    f"Git blob {name} size 非法/超限")
            normalized.append({
                "name": name, "mode": mode, "type": kind, "sha": sha,
                "size": size,
            })
        if _git_tree_sha1(normalized) != tree_sha:
            raise RepositoryMaterializationError(
                f"Git tree {full_name}:{tree_sha} 对象 SHA 重算不一致")
        for entry in normalized:
            rel = f"{prefix}/{entry['name']}" if prefix else entry["name"]
            if entry["mode"] == "040000":
                self._walk_tree(
                    full_name=full_name, tree_sha=entry["sha"], prefix=rel,
                    depth=depth + 1, files=files, submodules=submodules,
                    tree_counter=tree_counter)
                continue
            if entry["mode"] == "160000":
                if len(submodules) >= int(self.config["max_submodules"]):
                    raise RepositoryMaterializationError("Git submodule 数超过 policy")
                submodules.append({"path": rel, "revision": entry["sha"]})
                continue
            if entry["mode"] == "120000":
                raise RepositoryMaterializationError(
                    f"repository 含 symlink {rel}；当前 snapshot capability 不接受")
            if len(files) >= int(self.config["max_files"]):
                raise RepositoryMaterializationError("repository 文件数超过 policy")
            files[rel] = {
                "git_blob_sha1": entry["sha"], "git_mode": entry["mode"],
                "declared_bytes": entry["size"], "repository": full_name,
            }

    @staticmethod
    def _secure_parent(root: Path, rel: str) -> Path:
        current = root
        parts = PurePosixPath(rel).parts
        for part in parts[:-1]:
            current = current / part
            if not os.path.lexists(current):
                current.mkdir(mode=0o700)
            info = os.lstat(current)
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise RepositoryMaterializationError(
                    "archive destination parent 非可信目录")
        return current / parts[-1]

    def _extract_archive(
            self, *, archive: Path, destination: Path,
            expected: Mapping[str, Mapping[str, Any]],
            allowed_empty_directories: Sequence[str] = ()) -> list[Dict[str, Any]]:
        seen = set()
        ledger = []
        total = 0
        root_component = None
        allowed_directories = set()
        for rel in expected:
            parts = PurePosixPath(rel).parts
            allowed_directories.update(
                "/".join(parts[:index]) for index in range(1, len(parts)))
        for rel in allowed_empty_directories:
            safe = _safe_relpath(
                rel, field="archive empty gitlink directory",
                max_depth=int(self.config["max_tree_depth"]))
            parts = PurePosixPath(safe).parts
            allowed_directories.update(
                "/".join(parts[:index]) for index in range(1, len(parts) + 1))
        member_count = 0
        try:
            handle = tarfile.open(archive, mode="r:*")
        except (tarfile.TarError, OSError) as error:
            raise RepositoryMaterializationError("GitHub archive 非合法 tar") from error
        with handle:
            for member in handle:
                self.owner_guard()
                member_count += 1
                if member_count > len(expected) + len(allowed_directories) + 1:
                    raise RepositoryMaterializationError(
                        "archive member 数超过 Git tree 闭包")
                raw_name = member.name
                if (not isinstance(raw_name, str) or not raw_name
                        or "\\" in raw_name or "\x00" in raw_name):
                    raise RepositoryMaterializationError("archive member name 非法")
                path = PurePosixPath(raw_name)
                parts = path.parts
                if path.is_absolute() or any(part in ("", ".", "..") for part in parts):
                    raise RepositoryMaterializationError("archive member path traversal")
                if root_component is None:
                    root_component = _safe_component(
                        parts[0], field="archive root component")
                if parts[0] != root_component:
                    raise RepositoryMaterializationError("archive 含多个顶层根")
                if len(parts) == 1:
                    if not member.isdir():
                        raise RepositoryMaterializationError("archive 顶层根不是目录")
                    continue
                rel = "/".join(parts[1:])
                _safe_relpath(
                    rel, field="archive member", max_depth=int(self.config["max_tree_depth"]))
                if member.isdir():
                    if rel not in allowed_directories:
                        raise RepositoryMaterializationError(
                            f"archive 含 Git tree 外目录: {rel}")
                    continue
                if (member.issym() or member.islnk() or member.isdev()
                        or member.isfifo() or not member.isfile()):
                    raise RepositoryMaterializationError(
                        f"archive member {rel} 不是常规文件")
                if rel in seen:
                    raise RepositoryMaterializationError(f"archive member 重复: {rel}")
                expected_entry = expected.get(rel)
                if expected_entry is None:
                    raise RepositoryMaterializationError(
                        f"archive 含 Git tree 外文件: {rel}")
                if member.size != expected_entry["declared_bytes"]:
                    raise RepositoryMaterializationError(
                        f"archive {rel} size 与 Git tree 不一致")
                executable = expected_entry["git_mode"] == "100755"
                if bool(member.mode & 0o111) != executable:
                    raise RepositoryMaterializationError(
                        f"archive {rel} executable mode 与 Git tree 不一致")
                total += member.size
                if (member.size > int(self.config["max_file_bytes"])
                        or total > int(self.config["max_total_bytes"])):
                    raise RepositoryMaterializationError("archive 解压内容超过 policy")
                source = handle.extractfile(member)
                if source is None:
                    raise RepositoryMaterializationError(f"archive {rel} 无 payload")
                target = self._secure_parent(destination, rel)
                fd = os.open(
                    target, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                    | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                    0o400)
                sha1 = hashlib.sha1(  # noqa: S324 - Git object identity
                    f"blob {member.size}\0".encode("ascii"))
                sha256 = hashlib.sha256()
                copied = 0
                try:
                    while copied < member.size:
                        self.owner_guard()
                        chunk = source.read(min(1024 * 1024, member.size - copied))
                        if not chunk:
                            raise RepositoryMaterializationError(
                                f"archive {rel} payload 截断")
                        copied += len(chunk)
                        sha1.update(chunk)
                        sha256.update(chunk)
                        view = memoryview(chunk)
                        while view:
                            written = os.write(fd, view)
                            if written <= 0:
                                raise OSError("snapshot short write")
                            view = view[written:]
                    if source.read(1):
                        raise RepositoryMaterializationError(
                            f"archive {rel} payload 超出 header size")
                    os.fchmod(fd, 0o555 if executable else 0o444)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                    source.close()
                actual_git = sha1.hexdigest()
                if actual_git != expected_entry["git_blob_sha1"]:
                    raise RepositoryMaterializationError(
                        f"archive {rel} Git blob SHA 不一致")
                seen.add(rel)
                ledger.append({
                    "path": rel, "sha256": "sha256:" + sha256.hexdigest(),
                    "bytes": member.size, "git_blob_sha1": actual_git,
                    "git_mode": expected_entry["git_mode"],
                    "repository": expected_entry["repository"],
                })
        missing = sorted(set(expected) - seen)
        if missing:
            raise RepositoryMaterializationError(
                f"archive 缺 Git tree 文件: {missing[:5]}")
        return sorted(ledger, key=lambda item: item["path"])

    def _license_snapshot(self, full_name: str, revision: str) -> Dict[str, Any]:
        payload = self._get_json(
            f"https://api.github.com/repos/{full_name}/license?ref={revision}",
            label=f"submodule license {full_name}@{revision}")
        license_obj = payload.get("license") if isinstance(payload, dict) else None
        spdx = license_obj.get("spdx_id") if isinstance(license_obj, dict) else None
        content = payload.get("content") if isinstance(payload, dict) else None
        license_path = payload.get("path") if isinstance(payload, dict) else None
        if (not isinstance(spdx, str) or not isinstance(content, str)
                or payload.get("encoding") != "base64"
                or not isinstance(license_path, str)):
            raise RepositoryMaterializationError(
                f"submodule {full_name} license snapshot 非法")
        license_path = _safe_relpath(
            license_path, field=f"submodule {full_name} license path",
            max_depth=int(self.config["max_tree_depth"]))
        try:
            raw = base64.b64decode("".join(content.split()), validate=True)
        except (ValueError, base64.binascii.Error) as error:
            raise RepositoryMaterializationError(
                f"submodule {full_name} license base64 非法") from error
        if spdx not in set(self.auto_license["allow_spdx"]):
            raise RepositoryMaterializationError(
                f"submodule {full_name} license {spdx} 未获自动 allow")
        return {"spdx_id": spdx, "content_sha256": _sha256(raw),
                "repository_path": license_path,
                "evidence_ref": (
                    f"https://api.github.com/repos/{full_name}/license?ref={revision}")}

    @staticmethod
    def _resolve_submodule_repo(parent: str, raw_url: str) -> str:
        value = raw_url.strip()
        match = re.fullmatch(
            r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?",
            value)
        if match:
            result = match.group(1)
        else:
            ssh = re.fullmatch(
                r"git@github\.com:([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?",
                value)
            ssh_url = re.fullmatch(
                r"ssh://git@github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)"
                r"(?:\.git)?", value)
            if ssh:
                result = ssh.group(1)
            elif ssh_url:
                result = ssh_url.group(1)
            elif re.fullmatch(r"\.\./[A-Za-z0-9_.-]+(?:\.git)?", value):
                owner = parent.split("/", 1)[0]
                result = owner + "/" + value.removeprefix("../").removesuffix(".git")
            elif re.fullmatch(
                    r"\.\./\.\./[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?",
                    value):
                result = value.removeprefix("../../").removesuffix(".git")
            else:
                raise RepositoryMaterializationError(
                    f"submodule URL 非受限 GitHub repository: {value!r}")
        if _FULL_NAME_RE.fullmatch(result) is None:
            raise RepositoryMaterializationError("submodule repository identity 非法")
        return result

    def _gitmodule_map(
            self, tree_root: Path, prefix: str, submodules: Sequence[Mapping[str, Any]],
            parent_repo: str) -> Dict[str, str]:
        if not submodules:
            return {}
        path = tree_root / prefix / ".gitmodules" if prefix else tree_root / ".gitmodules"
        if not path.exists():
            raise RepositoryMaterializationError("Git tree 含 submodule 但缺 .gitmodules")
        raw = read_artifact_bytes(
            path, max_bytes=_MAX_GITMODULES_BYTES,
            label="repository .gitmodules",
            progress_guard=self.owner_guard)
        parser = configparser.ConfigParser(interpolation=None, strict=True)
        parser.optionxform = str
        try:
            parser.read_string(raw.decode("utf-8"))
        except (UnicodeDecodeError, configparser.Error) as error:
            raise RepositoryMaterializationError(".gitmodules 非严格 INI/UTF-8") from error
        if parser.defaults():
            # ``DEFAULT`` has inheritance semantics in ConfigParser but no
            # equivalent in Git config.  Accepting it would materialize a
            # repository different from what an exact Git checkout means.
            raise RepositoryMaterializationError(
                ".gitmodules 不得使用 ConfigParser DEFAULT 继承")
        mapped = {}
        for section in parser.sections():
            if not section.startswith('submodule "') or not section.endswith('"'):
                raise RepositoryMaterializationError(".gitmodules section 非 submodule")
            _bounded_string(
                section, field=".gitmodules section", max_bytes=1024)
            keys = set(parser[section])
            allowed = {
                "path", "url", "branch", "update", "ignore", "shallow",
                "fetchRecurseSubmodules",
            }
            if not {"path", "url"}.issubset(keys) or not keys <= allowed:
                raise RepositoryMaterializationError(
                    ".gitmodules 每节须 path/url，且只接受已知非执行元数据")
            if "branch" in keys:
                _bounded_string(
                    parser[section]["branch"], field="submodule.branch",
                    max_bytes=512)
            enums = {
                "update": {"checkout", "rebase", "merge", "none"},
                "ignore": {"all", "dirty", "untracked", "none"},
                "shallow": {"true", "false"},
                "fetchRecurseSubmodules": {"true", "false", "on-demand"},
            }
            for key, accepted in enums.items():
                if key in keys and parser[section][key] not in accepted:
                    raise RepositoryMaterializationError(
                        f".gitmodules {key} 非安全封闭值")
            rel = _safe_relpath(
                parser[section]["path"], field="submodule.path",
                max_depth=int(self.config["max_tree_depth"]))
            if rel in mapped:
                raise RepositoryMaterializationError(".gitmodules path 重复")
            mapped[rel] = self._resolve_submodule_repo(
                parent_repo, parser[section]["url"])
        expected = {
            item["path"].removeprefix(prefix + "/") if prefix else item["path"]
            for item in submodules}
        if set(mapped) != expected:
            raise RepositoryMaterializationError(
                ".gitmodules path 与 Git tree gitlink 闭包不一致")
        return mapped

    def _snapshot_repo(
            self, *, full_name: str, revision: str, prefix: str,
            tree_root: Path, downloads: Path, seen_repositories: set[tuple[str, str]],
            all_ledger: list[Dict[str, Any]], sources: list[Dict[str, Any]],
            submodule_records: list[Dict[str, Any]], tree_counter: list[int],
            is_root: bool) -> str:
        self.owner_guard()
        identity = (full_name.lower(), revision)
        if identity in seen_repositories:
            raise RepositoryMaterializationError("submodule repository/revision cycle")
        seen_repositories.add(identity)
        tree_sha = self._commit_tree(full_name, revision)
        files: Dict[str, Dict[str, Any]] = {}
        submodules: list[Dict[str, Any]] = []
        self._walk_tree(
            full_name=full_name, tree_sha=tree_sha, prefix="", depth=0,
            files=files, submodules=submodules, tree_counter=tree_counter)
        remaining_files = int(self.config["max_files"]) - len(all_ledger)
        remaining_bytes = (int(self.config["max_total_bytes"])
                           - sum(item["bytes"] for item in all_ledger))
        if len(files) > remaining_files:
            raise RepositoryMaterializationError(
                "recursive repository 总文件数超过 policy")
        if sum(item["declared_bytes"] for item in files.values()) > remaining_bytes:
            raise RepositoryMaterializationError(
                "recursive repository 总 bytes 超过 policy")
        archive_path = downloads / f"{len(sources):04d}.tar"
        archive_record = self._download_archive(full_name, revision, archive_path)
        repo_destination = tree_root / prefix if prefix else tree_root
        repo_destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        local_ledger = self._extract_archive(
            archive=archive_path, destination=repo_destination, expected=files,
            allowed_empty_directories=[item["path"] for item in submodules])
        for item in local_ledger:
            local_path = repo_destination / item["path"]
            if item["bytes"] <= 1024:
                pointer = _parse_lfs_pointer(read_artifact_bytes(
                    local_path, expected_hash=item["sha256"],
                    expected_size=item["bytes"], max_bytes=1024,
                    label=f"LFS pointer probe:{item['path']}",
                    progress_guard=self.owner_guard))
                if pointer is not None:
                    raise RepositoryMaterializationError(
                        f"Git LFS pointer {full_name}:{item['path']} ({pointer['oid']}, "
                        f"{pointer['size']} bytes) 被 lfs_policy=reject 拒绝")
            item = dict(item)
            combined_path = f"{prefix}/{item['path']}" if prefix else item["path"]
            item["path"] = _safe_relpath(
                combined_path, field="recursive repository file path",
                max_depth=int(self.config["max_tree_depth"]))
            item["revision"] = revision
            all_ledger.append(item)
        license_record = None if is_root else self._license_snapshot(
            full_name, revision)
        if license_record is not None:
            matches = [
                item for item in local_ledger
                if item["path"] == license_record["repository_path"]]
            if (len(matches) != 1
                    or matches[0]["sha256"] != license_record["content_sha256"]):
                raise RepositoryMaterializationError(
                    f"submodule {full_name} license evidence 与 commit 文件 ledger 不一致")
        source_record = {
            "repository": full_name, "revision": revision,
            "root_tree_sha1": tree_sha, "archive_url": archive_record["url"],
            # GitHub only promises stable extracted contents for a commit; the
            # compressed tar stream may be regenerated.  Keep transport bytes
            # as evidence, never as the reproducible source identity.
            "archive_transport_sha256": archive_record["sha256"],
            "archive_transport_bytes": archive_record["bytes"],
            "file_ledger_hash": _value_hash(local_ledger),
            "license": license_record,
        }
        sources.append(source_record)
        module_map = self._gitmodule_map(
            tree_root, prefix, submodules, full_name)
        for item in sorted(submodules, key=lambda value: value["path"]):
            if len(sources) > int(self.config["max_submodules"]):
                raise RepositoryMaterializationError(
                    "recursive Git submodule 总数超过 policy")
            local_rel = item["path"]
            child_repo = module_map[local_rel]
            child_prefix = f"{prefix}/{local_rel}" if prefix else local_rel
            child_prefix = _safe_relpath(
                child_prefix, field="recursive submodule path",
                max_depth=int(self.config["max_tree_depth"]))
            child_tree = self._snapshot_repo(
                full_name=child_repo, revision=item["revision"], prefix=child_prefix,
                tree_root=tree_root, downloads=downloads,
                seen_repositories=seen_repositories, all_ledger=all_ledger,
                sources=sources, submodule_records=submodule_records,
                tree_counter=tree_counter,
                is_root=False)
            submodule_records.append({
                "path": child_prefix, "repository": child_repo,
                "revision": item["revision"], "root_tree_sha1": child_tree,
            })
        seen_repositories.remove(identity)
        return tree_sha

    def _argv(self, value: Any, *, field: str) -> list[str]:
        if not isinstance(value, list) or not value or len(value) > 128:
            raise RepositoryMaterializationError(f"adapter.{field} argv 非法")
        result = []
        for index, raw in enumerate(value):
            arg = _bounded_string(
                raw, field=f"adapter.{field}[{index}]", max_bytes=4096)
            if any(marker in arg for marker in ("{src}", "{ckpt}")):
                raise RepositoryMaterializationError(
                    f"adapter.{field} 只接受 {{repo}}/{{artifact}} 占位符")
            unknown = re.findall(r"\{[^{}]+\}", arg)
            if any(item not in ("{repo}", "{artifact}") for item in unknown):
                raise RepositoryMaterializationError(
                    f"adapter.{field} 含未知占位符")
            result.append(arg)
        if PurePosixPath(result[0]).name in {
                "bash", "sh", "dash", "zsh", "env", "sudo", "docker", "git", "curl", "wget"}:
            raise RepositoryMaterializationError(
                f"adapter.{field} program 不得是 shell/network/host launcher")
        return result

    def _adapter_spec(
            self, *, tree_root: Path, ledger: Sequence[Mapping[str, Any]],
            repository: str, revision: str, root_tree_sha: str,
            sources: Sequence[Mapping[str, Any]],
            submodules: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        adapter_path = self.config["adapter_path"]
        adapter_file = tree_root / adapter_path
        ledger_by_path = {item["path"]: item for item in ledger}
        if adapter_path not in ledger_by_path:
            raise RepositoryMaterializationError(
                f"repository 缺 production adapter {adapter_path}")
        raw = read_artifact_bytes(
            adapter_file, expected_hash=ledger_by_path[adapter_path]["sha256"],
            expected_size=ledger_by_path[adapter_path]["bytes"],
            max_bytes=_MAX_ADAPTER_BYTES, label="repository import adapter",
            progress_guard=self.owner_guard)
        value = _strict_json(raw, label="repository import adapter")
        required_keys = {
            "version", "artifact_relpath", "artifact_type", "smoke_argv",
            "eval_argv", "dependency_mode", "dependency_locks",
            "factory_protocol",
        }
        if (not isinstance(value, dict) or set(value) != required_keys
                or value.get("version") != _ADAPTER_VERSION):
            raise RepositoryMaterializationError(
                "repository adapter v2 字段闭包/version 非法")
        artifact = _safe_relpath(
            value["artifact_relpath"], field="adapter.artifact_relpath",
            max_depth=int(self.config["max_tree_depth"]))
        if artifact not in ledger_by_path:
            raise RepositoryMaterializationError(
                "adapter artifact_relpath 不在 repository snapshot")
        artifact_type = value["artifact_type"]
        if artifact_type not in _ARTIFACT_TYPES:
            raise RepositoryMaterializationError("adapter artifact_type 非法")
        dependency_mode = value["dependency_mode"]
        locks = value["dependency_locks"]
        if dependency_mode != "pinned_image_only" or not isinstance(locks, list):
            raise RepositoryMaterializationError(
                "adapter dependency contract 非法；CP11.4c.2a 只允许 "
                "pinned_image_only")
        parsed_locks = []
        for index, path_value in enumerate(locks):
            path = _safe_relpath(
                path_value, field=f"adapter.dependency_locks[{index}]",
                max_depth=int(self.config["max_tree_depth"]))
            if (path not in ledger_by_path
                    or PurePosixPath(path).name not in self.config["dependency_lock_names"]):
                raise RepositoryMaterializationError(
                    f"adapter dependency lock 未在允许清单/快照: {path}")
            parsed_locks.append({
                "path": path, "sha256": ledger_by_path[path]["sha256"],
                "bytes": ledger_by_path[path]["bytes"],
            })
        if len({item["path"] for item in parsed_locks}) != len(parsed_locks):
            raise RepositoryMaterializationError("adapter dependency lock 重复")
        if parsed_locks:
            raise RepositoryMaterializationError(
                "CP11.4c.2a 只允许 pinned_image_only 且 dependency_locks 为空；"
                "未验证安装的 lock 不得冒充可复现环境")
        protocol = value["factory_protocol"]
        if (not isinstance(protocol, dict) or set(protocol) != {
                "name", "version", "scope_spec", "metrics", "required"}):
            raise RepositoryMaterializationError("adapter factory_protocol 字段闭包非法")
        protocol_name = _bounded_string(
            protocol["name"], field="factory_protocol.name", max_bytes=512)
        protocol_version = _positive_int(
            protocol["version"], field="factory_protocol.version", maximum=1_000_000)
        scope = protocol["scope_spec"]
        if not isinstance(scope, dict) or not scope:
            raise RepositoryMaterializationError(
                "factory_protocol.scope_spec 须为非空 object")
        try:
            scope_json = _canonical(scope).decode("utf-8").rstrip("\n")
        except (TypeError, ValueError, UnicodeEncodeError,
                RecursionError) as error:
            raise RepositoryMaterializationError(
                "factory_protocol.scope_spec 非有限 JSON") from error
        metrics = protocol["metrics"]
        if not isinstance(metrics, list) or not 1 <= len(metrics) <= 256:
            raise RepositoryMaterializationError("factory_protocol.metrics 数量非法")
        metric_defs = []
        log_map = {}
        semantic_metrics = []
        metric_pairs = set()
        metric_family_names: Dict[int, str] = {}
        for index, metric in enumerate(metrics):
            keys = {
                "log_key", "name", "version", "direction", "unit",
                "compute_spec", "readout_rule",
            }
            if not isinstance(metric, dict) or set(metric) != keys:
                raise RepositoryMaterializationError(
                    f"factory_protocol.metrics[{index}] 字段闭包非法")
            log_key = metric["log_key"]
            if not isinstance(log_key, str) or _LOG_KEY_RE.fullmatch(log_key) is None:
                raise RepositoryMaterializationError("factory metric log_key 非法")
            if log_key in log_map:
                raise RepositoryMaterializationError("factory metric log_key 重复")
            descriptor = {
                "name": _bounded_string(
                    metric["name"], field="factory metric.name", max_bytes=512),
                "version": _positive_int(
                    metric["version"], field="factory metric.version", maximum=1_000_000),
                "direction": metric["direction"],
                "unit": metric["unit"], "compute_spec": metric["compute_spec"],
                "readout_rule": metric["readout_rule"],
            }
            if descriptor["direction"] not in ("higher", "lower"):
                raise RepositoryMaterializationError("factory metric.direction 非法")
            if descriptor["unit"] is not None:
                descriptor["unit"] = _bounded_string(
                    descriptor["unit"], field="factory metric.unit",
                    max_bytes=8192)
            for field in ("compute_spec", "readout_rule"):
                descriptor[field] = _bounded_string(
                    descriptor[field], field=f"factory metric.{field}",
                    max_bytes=8192)
            metric_id = _stable_id("metric-family", {
                "repository": repository.lower(),
                "protocol_name": protocol_name,
                "metric_name": descriptor["name"],
            })
            prior_family_name = metric_family_names.setdefault(
                metric_id, descriptor["name"])
            if prior_family_name != descriptor["name"]:
                raise RepositoryMaterializationError(
                    "factory protocol stable metric family hash collision")
            metric_pair = (metric_id, descriptor["version"])
            if metric_pair in metric_pairs:
                raise RepositoryMaterializationError(
                    "factory protocol 含重复 metric family/version")
            metric_pairs.add(metric_pair)
            metric_def = {"id": metric_id, **descriptor, "log_key": log_key}
            metric_defs.append(metric_def)
            log_map[log_key] = [metric_id, descriptor["version"]]
            semantic_metrics.append(descriptor)
        required_log_keys = protocol["required"]
        if (not isinstance(required_log_keys, list) or not required_log_keys
                or any(not isinstance(key, str) or key not in log_map
                       for key in required_log_keys)
                or len(set(required_log_keys)) != len(required_log_keys)):
            raise RepositoryMaterializationError("factory_protocol.required 非法")
        metric_defs.sort(key=lambda item: (item["id"], item["version"]))
        semantic_metrics.sort(
            key=lambda item: (item["name"], item["version"], _value_hash(item)))
        protocol_identity = {
            "name": protocol_name, "version": protocol_version,
            "scope_spec": scope, "metrics": semantic_metrics,
        }
        protocol_id = _stable_id("protocol-family", {
            "repository": repository.lower(), "name": protocol_name,
        })
        required = sorted(log_map[key] for key in required_log_keys)
        adapter_hash = _sha256(raw)
        dependency_lock_hash = _value_hash(parsed_locks)
        source_identity = [{
            "repository": item["repository"], "revision": item["revision"],
            "root_tree_sha1": item["root_tree_sha1"],
            "archive_url": item["archive_url"],
            "file_ledger_hash": item["file_ledger_hash"],
            "license": item["license"],
        } for item in sources]
        smoke_cmd = self._argv(value["smoke_argv"], field="smoke_argv")
        eval_cmd = self._argv(value["eval_argv"], field="eval_argv")
        allowed_programs = {
            "python", "python3", self.sandbox_config["python_path"],
        }
        if smoke_cmd[0] not in allowed_programs or eval_cmd[0] not in allowed_programs:
            raise RepositoryMaterializationError(
                "adapter v2 当前只允许 pinned Python 作为直接 launcher")
        supply_chain = {
            "revision": revision, "root_tree_sha1": root_tree_sha,
            "submodules": list(sorted(
                submodules, key=lambda item: item["path"])),
            "patch_set_hash": _value_hash([]), "patch_apply_order": [],
            "lfs_objects": [], "dependency_mode": dependency_mode,
            "dependency_locks": parsed_locks,
            "dependency_lock_hash": dependency_lock_hash,
            "harness_adapter_hash": adapter_hash,
            "environment_hash": self.environment_hash,
            "network_isolation": True,
            "artifact_download_sources": source_identity,
            "system_package_sources": [],
            "container_digest": self.sandbox_config["image"],
            "container_image_id": self.sandbox_config["image_id"],
            "compiler": dict(self.config["compiler"]),
            "generated_files_hash": _value_hash([]),
            "environment_allowlist": list(_CONTROL_ENV_KEYS),
            "commands": {
                "smoke": smoke_cmd, "eval": eval_cmd},
        }
        target_identity = {
            "repository": repository, "revision": revision,
            "root_tree_sha1": root_tree_sha, "adapter_sha256": adapter_hash,
            "protocol_id": protocol_id, "protocol_version": protocol_version,
            "protocol_semantics_hash": _value_hash(protocol_identity),
            "required": required,
        }
        return {
            "smoke_cmd": smoke_cmd,
            "eval_cmd": eval_cmd,
            "protocol_id": protocol_id, "protocol_ver": protocol_version,
            "factory_protocol": {
                "id": protocol_id, "version": protocol_version,
                "name": protocol_name, "scope_spec_json": scope_json,
                "metric_defs": metric_defs,
                "metrics": [[item["id"], item["version"]]
                            for item in metric_defs],
            },
            "metric_log_map": log_map,
            "eval_key": "import-" + _value_hash(target_identity).removeprefix("sha256:")[:32],
            "target_set_hash": _value_hash(target_identity),
            "required": required, "artifact_relpath": artifact,
            "artifact_type": artifact_type, "env_hash": self.environment_hash,
            "supply_chain": supply_chain, "requires_adversarial_sandbox": True,
        }

    def _read_json_file(self, path: Path, *, maximum: int, label: str) -> Any:
        raw = read_artifact_bytes(
            path, max_bytes=maximum, label=label,
            progress_guard=self.owner_guard)
        return _strict_json(raw, label=label)

    def _verify_published_impl(
            self, *, object_path: Path, repository: str,
            revision: str) -> Dict[str, Any]:
        if (not isinstance(object_path.name, str)
                or re.fullmatch(r"[0-9a-f]{64}", object_path.name) is None):
            raise RepositoryMaterializationError(
                "repository snapshot object path 非法")
        object_fd = open_directory(
            object_path, label="published repository object")
        try:
            if set(os.listdir(object_fd)) != {
                    "tree", "ledger.json", "spec.json", "transport.json",
                    "receipt.json"}:
                raise RepositoryMaterializationError(
                    "repository snapshot object 文件闭包漂移")
            anchored = Path(f"/proc/self/fd/{object_fd}")
            receipt_path = anchored / "receipt.json"
            receipt = self._read_json_file(
                receipt_path, maximum=256 * 1024,
                label="repository snapshot receipt")
            expected_receipt_keys = {
                "protocol", "version", "repository", "revision",
                "root_tree_sha1", "object_hash", "config_hash",
                "environment_hash", "file_count", "total_bytes",
                "file_ledger_hash", "spec_hash", "transport_evidence_hash",
            }
            if (not isinstance(receipt, dict)
                    or set(receipt) != expected_receipt_keys
                    or receipt.get("protocol") != _PROTOCOL
                    or receipt.get("version") != 1
                    or receipt.get("repository") != repository
                    or receipt.get("revision") != revision
                    or receipt.get("config_hash") != self.config_hash
                    or receipt.get("environment_hash") != self.environment_hash
                    or receipt.get("object_hash") != "sha256:" + object_path.name
                    or not isinstance(receipt.get("root_tree_sha1"), str)
                    or _COMMIT_RE.fullmatch(receipt["root_tree_sha1"]) is None
                    or any(not isinstance(receipt.get(key), str)
                           or _SHA256_RE.fullmatch(receipt[key]) is None
                           for key in ("file_ledger_hash", "spec_hash",
                                       "transport_evidence_hash"))
                    or isinstance(receipt.get("file_count"), bool)
                    or not isinstance(receipt.get("file_count"), int)
                    or receipt["file_count"] < 1
                    or receipt["file_count"] > int(self.config["max_files"])
                    or isinstance(receipt.get("total_bytes"), bool)
                    or not isinstance(receipt.get("total_bytes"), int)
                    or receipt["total_bytes"] < 0
                    or receipt["total_bytes"] > int(self.config["max_total_bytes"])):
                raise RepositoryMaterializationError(
                    "repository snapshot receipt identity 非法")
            ledger = self._read_json_file(
                anchored / "ledger.json", maximum=_MAX_RECEIPT_BYTES,
                label="repository snapshot ledger")
            spec = self._read_json_file(
                anchored / "spec.json", maximum=16 * 1024 * 1024,
                label="repository snapshot spec")
            transport = self._read_json_file(
                anchored / "transport.json", maximum=4 * 1024 * 1024,
                label="repository transport evidence")
            if (_value_hash(ledger) != receipt["file_ledger_hash"]
                    or _value_hash(spec) != receipt["spec_hash"]
                    or _value_hash(transport) != receipt["transport_evidence_hash"]
                    or not isinstance(ledger, list)
                    or not isinstance(spec, dict)
                    or not isinstance(transport, list)):
                raise RepositoryMaterializationError(
                    "repository snapshot component hash/type 不一致")
            object_identity = {
                "protocol": _PROTOCOL, "repository": repository,
                "revision": revision,
                "root_tree_sha1": receipt["root_tree_sha1"],
                "file_ledger_hash": receipt["file_ledger_hash"],
                "spec_hash": receipt["spec_hash"],
                "config_hash": self.config_hash,
            }
            if _value_hash(object_identity) != receipt["object_hash"]:
                raise RepositoryMaterializationError(
                    "repository snapshot object hash 重算不一致")
            if (len(ledger) != receipt["file_count"]
                    or sum(item.get("bytes", -1) if isinstance(item, dict) else -1
                           for item in ledger) != receipt["total_bytes"]):
                raise RepositoryMaterializationError(
                    "repository snapshot ledger count/bytes 不一致")
            hashes = {}
            expected_ledger_keys = {
                "path", "sha256", "bytes", "git_blob_sha1", "git_mode",
                "repository", "revision",
            }
            if ledger != sorted(ledger, key=lambda item: item.get("path", "")
                                if isinstance(item, dict) else ""):
                raise RepositoryMaterializationError(
                    "repository snapshot ledger 未 canonical 排序")
            for item in ledger:
                if (not isinstance(item, dict) or set(item) != expected_ledger_keys
                        or not isinstance(item.get("path"), str)
                        or _safe_relpath(
                            item["path"], field="published ledger path",
                            max_depth=int(self.config["max_tree_depth"])) != item["path"]
                        or item["path"] in hashes
                        or not isinstance(item.get("sha256"), str)
                        or _SHA256_RE.fullmatch(item["sha256"]) is None
                        or isinstance(item.get("bytes"), bool)
                        or not isinstance(item.get("bytes"), int)
                        or not 0 <= item["bytes"] <= int(self.config["max_file_bytes"])
                        or not isinstance(item.get("git_blob_sha1"), str)
                        or _COMMIT_RE.fullmatch(item["git_blob_sha1"]) is None
                        or item.get("git_mode") not in ("100644", "100755")
                        or not isinstance(item.get("repository"), str)
                        or _FULL_NAME_RE.fullmatch(item["repository"]) is None
                        or not isinstance(item.get("revision"), str)
                        or _COMMIT_RE.fullmatch(item["revision"]) is None):
                    raise RepositoryMaterializationError(
                        "repository snapshot ledger 非法")
                hashes[item["path"]] = item["sha256"]
            tree_fd = open_directory(
                anchored / "tree", label="published repository tree")
            try:
                verify_tree_fd(
                    tree_fd, hashes, label="published repository tree", exact=True,
                    progress_guard=self.owner_guard)
                for item in ledger:
                    self.owner_guard()
                    path = Path(f"/proc/self/fd/{tree_fd}") / item["path"]
                    mode_info = os.lstat(path)
                    mode = stat.S_IMODE(mode_info.st_mode)
                    expected_mode = 0o555 if item["git_mode"] == "100755" else 0o444
                    if (not stat.S_ISREG(mode_info.st_mode)
                            or stat.S_ISLNK(mode_info.st_mode)
                            or mode != expected_mode):
                        raise RepositoryMaterializationError(
                            f"published repository mode 不一致: {item['path']}")
            finally:
                os.close(tree_fd)
        finally:
            os.close(object_fd)
        result = dict(spec)
        result["source_tree"] = str(object_path / "tree")
        result["file_ledger"] = ledger
        result["snapshot_receipt"] = str(object_path / "receipt.json")
        result["repository_snapshot_hash"] = receipt["object_hash"]
        return result

    def _verify_published(
            self, *, object_path: Path, repository: str,
            revision: str) -> Dict[str, Any]:
        try:
            return self._verify_published_impl(
                object_path=object_path, repository=repository,
                revision=revision)
        except RepositoryCacheError:
            raise
        except (RepositoryMaterializationError, ArtifactCapabilityError,
                OSError, KeyError, TypeError) as error:
            raise RepositoryCacheError(
                "published repository snapshot tree/hash 完整性核验失败") from error

    def _load_index(self, candidate: Mapping[str, Any], identity_hash: str) -> Optional[Dict[str, Any]]:
        index_path = (
            self.work_root / "state" / "import-materializations" / "indexes"
            / f"{identity_hash}.json")
        if not os.path.lexists(index_path):
            return None
        try:
            index = self._read_json_file(
                index_path, maximum=128 * 1024,
                label="repository materialization index")
        except RepositoryCacheError:
            raise
        except (RepositoryMaterializationError, ArtifactCapabilityError,
                OSError, TypeError) as error:
            # The candidate identity was already validated before consulting
            # this local authority.  A malformed existing index is therefore
            # cache/control-plane corruption, never a durable candidate fault.
            raise RepositoryCacheError(
                "repository materialization index 完整性核验失败") from error
        expected = {
            "version": 1, "candidate_id": candidate["id"],
            "canonical_uri": candidate["canonical_uri"],
            "revision": candidate["revision"],
            "search_snapshot_hash": candidate["search_snapshot_hash"],
            "config_hash": self.config_hash,
            "environment_hash": self.environment_hash,
        }
        if (not isinstance(index, dict)
                or set(index) != set(expected) | {"object_hash"}
                or any(index.get(key) != value for key, value in expected.items())
                or not isinstance(index.get("object_hash"), str)
                or _SHA256_RE.fullmatch(index["object_hash"]) is None):
            raise RepositoryCacheError(
                "repository materialization index identity 漂移")
        object_path = (
            self.work_root / "state" / "import-materializations" / "objects"
            / index["object_hash"].removeprefix("sha256:"))
        return self._verify_published(
            object_path=object_path,
            repository=urllib.parse.urlsplit(candidate["canonical_uri"]).path.strip("/"),
            revision=candidate["revision"])

    def _validate_search_snapshot(
            self, snapshot: Any, *, repository: str, revision: str,
            canonical_uri: str) -> Dict[str, Any]:
        if not isinstance(snapshot, dict) or snapshot.get("version") not in (1, 2):
            raise RepositoryMaterializationError(
                "candidate search snapshot version 非法")
        expected = {
            "version", "provider", "query", "provider_result_id", "retrieved_at",
            "ranking", "repository", "canonical_uri", "revision", "license",
            "policy_hash",
        }
        if snapshot["version"] == 2:
            expected.add("source_authority_hash")
        if set(snapshot) != expected or snapshot.get("provider") != "github_rest_v1":
            raise RepositoryMaterializationError(
                "candidate search snapshot 字段闭包/provider 非法")
        for field, maximum in (
                ("query", 8192), ("provider_result_id", 128),
                ("retrieved_at", 128)):
            _bounded_string(snapshot[field], field=f"snapshot.{field}", max_bytes=maximum)
        ranking = snapshot["ranking"]
        if (not isinstance(ranking, dict)
                or set(ranking) != {"rank", "recipe", "scale"}
                or isinstance(ranking.get("rank"), bool)
                or not isinstance(ranking.get("rank"), int)
                or ranking["rank"] < 0):
            raise RepositoryMaterializationError("snapshot.ranking 非法")
        _bounded_string(ranking["recipe"], field="snapshot.ranking.recipe", max_bytes=512)
        _bounded_string(ranking["scale"], field="snapshot.ranking.scale", max_bytes=128)
        repo = snapshot["repository"]
        if (not isinstance(repo, dict)
                or set(repo) != {"full_name", "default_branch", "stars", "updated_at"}
                or repo.get("full_name") != repository
                or isinstance(repo.get("stars"), bool)
                or not isinstance(repo.get("stars"), int) or repo["stars"] < 0):
            raise RepositoryMaterializationError("snapshot.repository 权威非法")
        _bounded_string(
            repo["default_branch"], field="snapshot.repository.default_branch",
            max_bytes=512)
        _bounded_string(
            repo["updated_at"], field="snapshot.repository.updated_at",
            max_bytes=128)
        if (snapshot.get("canonical_uri") != canonical_uri
                or snapshot.get("revision") != revision
                or not isinstance(snapshot.get("policy_hash"), str)
                or _SHA256_RE.fullmatch(snapshot["policy_hash"]) is None):
            raise RepositoryMaterializationError(
                "candidate search snapshot repository/revision/policy 权威不一致")
        if snapshot["version"] == 2:
            authority_hash = snapshot["source_authority_hash"]
            if authority_hash is not None and (
                    not isinstance(authority_hash, str)
                    or _SHA256_RE.fullmatch(authority_hash) is None):
                raise RepositoryMaterializationError(
                    "snapshot.source_authority_hash 非法")
        license_value = snapshot["license"]
        if (not isinstance(license_value, dict)
                or set(license_value) != {
                    "spdx_id", "lookup_status", "evidence_ref", "content_sha256"}
                or not isinstance(license_value.get("spdx_id"), str)
                or re.fullmatch(r"(?:[A-Za-z0-9.+-]{1,128}|NOASSERTION)",
                                license_value["spdx_id"]) is None
                or license_value.get("lookup_status") not in ("found", "missing")):
            raise RepositoryMaterializationError("snapshot.license 非法")
        evidence = license_value.get("evidence_ref")
        if not isinstance(evidence, str):
            raise RepositoryMaterializationError("snapshot.license.evidence_ref 非法")
        parsed_evidence = urllib.parse.urlsplit(evidence)
        evidence_query = urllib.parse.parse_qs(
            parsed_evidence.query, keep_blank_values=True)
        if (parsed_evidence.scheme != "https"
                or parsed_evidence.hostname != "api.github.com"
                or parsed_evidence.username or parsed_evidence.password
                or parsed_evidence.port not in (None, 443)
                or not parsed_evidence.path.startswith(f"/repos/{repository}/")
                or parsed_evidence.fragment
                or set(evidence_query) != {"ref"}
                or evidence_query.get("ref") != [revision]):
            raise RepositoryMaterializationError(
                "snapshot.license.evidence_ref 未绑定同一 commit")
        content_hash = license_value.get("content_sha256")
        if ((license_value["lookup_status"] == "found")
                != (isinstance(content_hash, str)
                    and _SHA256_RE.fullmatch(content_hash) is not None)):
            raise RepositoryMaterializationError(
                "snapshot.license lookup/content hash 矛盾")
        if license_value["lookup_status"] == "missing" and content_hash is not None:
            raise RepositoryMaterializationError(
                "snapshot.license missing 不得带 content hash")
        normalized_license = dict(license_value)
        if license_value["lookup_status"] == "found":
            prefix = f"/repos/{repository}/contents/"
            if not parsed_evidence.path.startswith(prefix):
                raise RepositoryMaterializationError(
                    "snapshot.license found evidence 非 commit contents path")
            encoded_path = parsed_evidence.path[len(prefix):]
            try:
                repository_path = urllib.parse.unquote_to_bytes(
                    encoded_path).decode("utf-8")
            except UnicodeDecodeError as error:
                raise RepositoryMaterializationError(
                    "snapshot.license repository path 非 UTF-8") from error
            normalized_license["repository_path"] = _safe_relpath(
                repository_path, field="snapshot.license repository path",
                max_depth=int(self.config["max_tree_depth"]))
        else:
            if parsed_evidence.path != f"/repos/{repository}/license":
                raise RepositoryMaterializationError(
                    "snapshot.license missing evidence 非 pinned license endpoint")
            normalized_license["repository_path"] = None
        return normalized_license

    def __call__(self, candidate: Dict[str, Any]) -> Dict[str, Any]:
        self.owner_guard()
        required_candidate = {
            "id", "question_id", "canonical_uri", "revision", "source_kind",
            "search_snapshot_json", "search_snapshot_hash",
        }
        if not isinstance(candidate, dict) or set(candidate) != required_candidate:
            raise RepositoryMaterializationError("repository candidate 字段闭包非法")
        if (isinstance(candidate["id"], bool) or not isinstance(candidate["id"], int)
                or candidate["id"] <= 0
                or isinstance(candidate["question_id"], bool)
                or not isinstance(candidate["question_id"], int)
                or candidate["question_id"] <= 0
                or candidate["source_kind"] != "repo"):
            raise RepositoryMaterializationError("repository candidate id/source_kind 非法")
        uri_match = (_GITHUB_URI_RE.fullmatch(candidate["canonical_uri"])
                     if isinstance(candidate["canonical_uri"], str) else None)
        if uri_match is None:
            raise RepositoryMaterializationError("repository candidate URI 非规范 GitHub URI")
        repository = uri_match.group(1)
        revision = candidate["revision"]
        if not isinstance(revision, str) or _COMMIT_RE.fullmatch(revision) is None:
            raise RepositoryMaterializationError("repository candidate revision 非 40-hex commit")
        raw_snapshot = candidate["search_snapshot_json"]
        if not isinstance(raw_snapshot, str):
            raise RepositoryMaterializationError("candidate search snapshot 非字符串")
        try:
            snapshot_bytes = raw_snapshot.encode("utf-8")
        except UnicodeEncodeError as error:
            raise RepositoryMaterializationError(
                "candidate search snapshot 非合法 UTF-8") from error
        if len(snapshot_bytes) > _MAX_SEARCH_SNAPSHOT_BYTES:
            raise RepositoryMaterializationError(
                "candidate search snapshot 超过物化上限")
        if (not isinstance(candidate["search_snapshot_hash"], str)
                or _SHA256_RE.fullmatch(candidate["search_snapshot_hash"]) is None
                or _sha256(snapshot_bytes) != candidate["search_snapshot_hash"]):
            raise RepositoryMaterializationError("candidate search snapshot hash 不一致")
        snapshot = _strict_json(snapshot_bytes, label="candidate search snapshot")
        root_license = self._validate_search_snapshot(
            snapshot, repository=repository, revision=revision,
            canonical_uri=candidate["canonical_uri"])
        identity = {
            "candidate_id": candidate["id"], "canonical_uri": candidate["canonical_uri"],
            "revision": revision, "search_snapshot_hash": candidate["search_snapshot_hash"],
            "config_hash": self.config_hash, "environment_hash": self.environment_hash,
        }
        identity_hash = _value_hash(identity).removeprefix("sha256:")
        self._authority_directories()
        reused = self._load_index(candidate, identity_hash)
        if reused is not None:
            return reused

        _base, staging_parent, objects, indexes = self._authority_directories()
        for stale in staging_parent.glob(identity_hash + ".*"):
            stale_info = os.lstat(stale)
            if (not stat.S_ISDIR(stale_info.st_mode)
                    or stat.S_ISLNK(stale_info.st_mode)
                    or stale_info.st_uid != os.geteuid()):
                raise RepositoryCacheError(
                    "stale materialization authority 非法")
            _remove_private_tree(stale)
        staging = staging_parent / f"{identity_hash}.{secrets.token_hex(8)}"
        staging.mkdir(mode=0o700)
        tree_root = staging / "tree"
        downloads = staging / "downloads"
        tree_root.mkdir(mode=0o700)
        downloads.mkdir(mode=0o700)
        try:
            ledger: list[Dict[str, Any]] = []
            sources: list[Dict[str, Any]] = []
            submodules: list[Dict[str, Any]] = []
            root_tree_sha = self._snapshot_repo(
                full_name=repository, revision=revision, prefix="",
                tree_root=tree_root, downloads=downloads,
                seen_repositories=set(), all_ledger=ledger,
                sources=sources, submodule_records=submodules,
                tree_counter=[0], is_root=True)
            if not sources or sources[0].get("repository") != repository:
                raise RepositoryMaterializationError(
                    "root repository source record 缺失")
            if root_license["repository_path"] is not None:
                root_license_matches = [
                    item for item in ledger
                    if (item["path"] == root_license["repository_path"]
                        and item["repository"] == repository
                        and item["revision"] == revision)]
                if (len(root_license_matches) != 1
                        or root_license_matches[0]["sha256"]
                        != root_license["content_sha256"]):
                    raise RepositoryMaterializationError(
                        "root license evidence 与 commit 文件 ledger 不一致")
            sources[0]["license"] = root_license
            ledger.sort(key=lambda item: item["path"])
            if len({item["path"] for item in ledger}) != len(ledger):
                raise RepositoryMaterializationError(
                    "recursive repository 文件路径冲突")
            if len(ledger) > int(self.config["max_files"]):
                raise RepositoryMaterializationError("repository 总文件数超过 policy")
            total = sum(item["bytes"] for item in ledger)
            if total > int(self.config["max_total_bytes"]):
                raise RepositoryMaterializationError("repository 总 bytes 超过 policy")
            spec = self._adapter_spec(
                tree_root=tree_root, ledger=ledger, repository=repository,
                revision=revision, root_tree_sha=root_tree_sha,
                sources=sources, submodules=submodules)
            file_ledger_hash = _value_hash(ledger)
            spec_hash = _value_hash(spec)
            transport_evidence_hash = _value_hash(sources)
            snapshot_identity = {
                "protocol": _PROTOCOL, "repository": repository,
                "revision": revision, "root_tree_sha1": root_tree_sha,
                "file_ledger_hash": file_ledger_hash,
                "spec_hash": spec_hash,
                "config_hash": self.config_hash,
            }
            object_hash = _value_hash(snapshot_identity)
            receipt = {
                "protocol": _PROTOCOL, "version": 1,
                "repository": repository, "revision": revision,
                "root_tree_sha1": root_tree_sha, "object_hash": object_hash,
                "config_hash": self.config_hash,
                "environment_hash": self.environment_hash,
                "file_count": len(ledger), "total_bytes": total,
                "file_ledger_hash": file_ledger_hash,
                "spec_hash": spec_hash,
                "transport_evidence_hash": transport_evidence_hash,
            }
            shutil.rmtree(downloads)
            self.owner_guard()
            _atomic_write_json(
                staging / "ledger.json", ledger, maximum=_MAX_RECEIPT_BYTES)
            _atomic_write_json(
                staging / "spec.json", spec, maximum=16 * 1024 * 1024)
            _atomic_write_json(
                staging / "transport.json", sources, maximum=4 * 1024 * 1024)
            _atomic_write_json(
                staging / "receipt.json", receipt, maximum=256 * 1024)
            object_path = objects / object_hash.removeprefix("sha256:")
            if os.path.lexists(object_path):
                self.owner_guard()
                existing = self._verify_published(
                    object_path=object_path, repository=repository,
                    revision=revision)
                _remove_private_tree(staging)
            else:
                for current, dirs, files in os.walk(
                        tree_root, topdown=False, followlinks=False):
                    for name in files:
                        path = Path(current) / name
                        mode = os.lstat(path).st_mode
                        os.chmod(path, 0o555 if mode & 0o111 else 0o444)
                    for name in dirs:
                        os.chmod(Path(current) / name, 0o555)
                    os.chmod(Path(current), 0o555)
                    _fsync_directory(Path(current))
                    self.owner_guard()
                self.owner_guard()
                try:
                    os.replace(staging, object_path)
                except OSError as error:
                    if error.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                        raise
                    # Another publisher won after the existence check.  Only
                    # exact verified content is reusable; our private staging
                    # tree remains ours to remove.
                    existing = self._verify_published(
                        object_path=object_path, repository=repository,
                        revision=revision)
                    _remove_private_tree(staging)
                    _fsync_directory(staging_parent)
                else:
                    _fsync_directory(objects)
                    _fsync_directory(staging_parent)
                    existing = self._verify_published(
                        object_path=object_path, repository=repository,
                        revision=revision)
            self.owner_guard()
            _atomic_write_json(
                indexes / f"{identity_hash}.json",
                {"version": 1, **identity, "object_hash": object_hash},
                maximum=128 * 1024)
            return existing
        except BaseException:
            if os.path.lexists(staging):
                try:
                    _remove_private_tree(staging)
                except OSError:
                    pass
            raise


class ProductionCandidateFetcher:
    """Preserve registered legacy snapshots while enabling real GitHub candidates."""

    def __init__(self, *, legacy_fetcher, repository_fetcher):
        if not callable(legacy_fetcher) or not callable(repository_fetcher):
            raise ValueError(
                "ProductionCandidateFetcher requires callable legacy/repository fetchers")
        self.legacy_fetcher = legacy_fetcher
        self.repository_fetcher = repository_fetcher

    def __call__(self, candidate: Dict[str, Any]) -> Dict[str, Any]:
        raw = candidate.get("search_snapshot_json")
        if isinstance(raw, str):
            try:
                snapshot = json.loads(raw)
            except json.JSONDecodeError:
                snapshot = None
            if isinstance(snapshot, dict) and "materialization" in snapshot:
                return self.legacy_fetcher(candidate)
        return self.repository_fetcher(candidate)
