"""Canonical wheel-lock parsing, HTTPS acquisition, and archive validation."""
from __future__ import annotations

import email.parser
import hashlib
import os
import re
import secrets
import stat
import urllib.error
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Mapping

from .artifact_capability import (
    ArtifactCapabilityError,
    open_artifact,
    read_artifact_bytes,
)
from .dependency_image_common import (
    _LOCK_VERSION,
    _NAME_RE,
    _VERSION_RE,
    _WHEEL_RE,
    _hash_file,
    _normalized_name,
    _wheel_url_is_allowed,
)
from .repository_materialization_common import (
    _SHA256_RE,
    RepositoryCacheError,
    RepositoryMaterializationError,
    RepositoryTransportError,
    _canonical,
    _fsync_directory,
    _safe_relpath,
    _strict_json,
)


class _DependencyLockMixin:
    """Host contract: config/compiler/owner_guard/wheel_fetcher/_wheel_opener."""

    def _parse_lock(
            self, tree_root: Path, lock_entry: Mapping[str, Any]) -> tuple[Dict[str, Any], bytes]:
        lock_path = lock_entry.get("path") if isinstance(lock_entry, dict) else None
        if (not isinstance(lock_entry, dict)
                or set(lock_entry) != {"path", "sha256", "bytes"}
                or not isinstance(lock_path, str)
                or not isinstance(lock_entry.get("sha256"), str)
                or _SHA256_RE.fullmatch(lock_entry["sha256"]) is None
                or isinstance(lock_entry.get("bytes"), bool)
                or not isinstance(lock_entry.get("bytes"), int)
                or not 1 <= lock_entry["bytes"] <= self.config["max_lock_bytes"]):
            raise RepositoryMaterializationError("python wheel lock ledger identity 非法")
        if (_safe_relpath(
                lock_path, field="python wheel lock path", max_depth=128) != lock_path
                or PurePosixPath(lock_path).name != self.config["lock_basename"]):
            raise RepositoryMaterializationError("python wheel lock ledger path 非法")
        raw = read_artifact_bytes(
            tree_root / lock_path, expected_hash=lock_entry["sha256"],
            expected_size=lock_entry["bytes"], max_bytes=self.config["max_lock_bytes"],
            label="python wheel lock", progress_guard=self.owner_guard)
        value = _strict_json(raw, label="python wheel lock")
        if raw != _canonical(value):
            raise RepositoryMaterializationError("python wheel lock 必须是 canonical JSON + LF")
        if (not isinstance(value, dict)
                or set(value) != {"version", "python", "platform", "distributions"}
                or value.get("version") != _LOCK_VERSION):
            raise RepositoryMaterializationError("python wheel lock 字段闭包/version 非法")
        python = value["python"]
        if python != self.compiler:
            raise RepositoryMaterializationError("python wheel lock compiler 与 pinned base 不一致")
        platform = value["platform"]
        if platform != {"os": "linux", "architecture": "amd64"}:
            raise RepositoryMaterializationError("python wheel lock platform 非 linux/amd64")
        values = value["distributions"]
        if not isinstance(values, list) or not 1 <= len(values) <= self.config["max_wheels"]:
            raise RepositoryMaterializationError("python wheel lock distributions 数量非法")
        parsed = []
        total = 0
        names = set()
        filenames = set()
        hashes = set()
        for index, item in enumerate(values):
            if (not isinstance(item, dict)
                    or set(item) != {"name", "version", "filename", "url", "sha256", "bytes"}):
                raise RepositoryMaterializationError(
                    f"python wheel lock distributions[{index}] 字段闭包非法")
            name = item["name"]
            version = item["version"]
            filename = item["filename"]
            if (not isinstance(name, str) or _NAME_RE.fullmatch(name) is None
                    or _normalized_name(name) != name
                    or not isinstance(version, str) or _VERSION_RE.fullmatch(version) is None
                    or not isinstance(filename, str) or _WHEEL_RE.fullmatch(filename) is None):
                raise RepositoryMaterializationError("python wheel lock name/version/filename 非法")
            wheel_parts = filename[:-4].split("-")
            if len(wheel_parts) not in (5, 6):
                raise RepositoryMaterializationError(f"wheel filename 结构非法: {filename}")
            if (len(wheel_parts) == 6
                    and re.fullmatch(r"[0-9][0-9A-Za-z_]*", wheel_parts[2]) is None):
                raise RepositoryMaterializationError(
                    f"wheel filename build tag 非法: {filename}")
            distribution, wheel_version = wheel_parts[0], wheel_parts[1]
            python_tag, abi_tag, platform_tag = wheel_parts[-3:]
            python_tags = python_tag.split(".")
            abi_tags = abi_tag.split(".")
            platform_tags = platform_tag.split(".")
            major, minor, _patch = (
                int(part) for part in self.compiler["version"].split("."))
            target_py_tag = f"py{major}{minor}"
            target_cp_tag = f"cp{major}{minor}"

            compatible_python_tag = (
                lambda tag: tag in {f"py{major}", target_py_tag, target_cp_tag}
                or (re.fullmatch(r"cp([0-9])([0-9]{1,2})", tag) is not None
                    and int(tag[2]) == major
                    and 2 <= int(tag[3:]) < minor
                    and "abi3" in abi_tags))
            compatible_platform_tag = lambda tag: (
                tag == "any" or re.fullmatch(
                    r"(?:linux|manylinux(?:1|2010|2014|_[0-9]+_[0-9]+)"
                    r"|musllinux_[0-9]+_[0-9]+)_x86_64", tag) is not None)
            if (_normalized_name(distribution) != name or wheel_version != version
                    or not any(compatible_python_tag(tag) for tag in python_tags)
                    or not all(tag == "py2" or compatible_python_tag(tag)
                               for tag in python_tags)
                    or not all(tag in {"none", "abi3", target_cp_tag}
                               for tag in abi_tags)
                    or not all(compatible_platform_tag(tag)
                               for tag in platform_tags)
                    or ("any" in platform_tags
                        and (set(platform_tags) != {"any"}
                             or set(abi_tags) != {"none"}))):
                raise RepositoryMaterializationError(f"wheel filename target/identity 非法: {filename}")
            url = item["url"]
            if not _wheel_url_is_allowed(
                    url, self.config["allowed_hosts"], filename=filename):
                raise RepositoryMaterializationError(f"wheel URL 非允许的 exact HTTPS artifact: {url!r}")
            expected_hash = item["sha256"]
            size = item["bytes"]
            if (not isinstance(expected_hash, str) or _SHA256_RE.fullmatch(expected_hash) is None
                    or isinstance(size, bool) or not isinstance(size, int)
                    or not 1 <= size <= self.config["max_wheel_bytes"]):
                raise RepositoryMaterializationError("python wheel lock hash/bytes 非法")
            total += size
            if total > self.config["max_total_wheel_bytes"]:
                raise RepositoryMaterializationError("python wheel lock 总 bytes 超限")
            if name in names or filename in filenames or expected_hash in hashes:
                raise RepositoryMaterializationError("python wheel lock name/filename/hash 重复")
            names.add(name)
            filenames.add(filename)
            hashes.add(expected_hash)
            parsed.append(dict(item))
        if parsed != sorted(parsed, key=lambda item: (
                item["name"], item["version"], item["filename"])):
            raise RepositoryMaterializationError("python wheel lock distributions 未 canonical 排序")
        return {**value, "distributions": parsed}, raw

    def _download_wheel(self, url: str, destination: Path, maximum: int) -> Mapping[str, Any]:
        request = urllib.request.Request(
            url, headers={"Accept": "application/octet-stream",
                          "User-Agent": "meta-research-dependency-image/1"})
        try:
            response = self._wheel_opener(request, timeout=float(self.config["timeout_s"]))
            try:
                if not _wheel_url_is_allowed(
                        response.geturl(), self.config["allowed_hosts"]):
                    raise RepositoryTransportError("dependency wheel final URL 越出 allowlist")
                declared = response.headers.get("Content-Length")
                if declared is not None:
                    try:
                        declared_size = int(declared)
                    except ValueError as error:
                        raise RepositoryTransportError("dependency wheel Content-Length 非法") from error
                    if not 0 <= declared_size <= maximum:
                        raise RepositoryTransportError("dependency wheel Content-Length 越界")
                fd = os.open(
                    destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                    | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0), 0o600)
                digest = hashlib.sha256()
                total = 0
                try:
                    while True:
                        self.owner_guard()
                        chunk = response.read(min(1024 * 1024, maximum + 1 - total))
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > maximum:
                            raise RepositoryTransportError("dependency wheel 下载越界")
                        digest.update(chunk)
                        view = memoryview(chunk)
                        while view:
                            written = os.write(fd, view)
                            if written <= 0:
                                raise OSError("dependency wheel short write")
                            view = view[written:]
                    os.fsync(fd)
                finally:
                    os.close(fd)
                return {"url": response.geturl(), "bytes": total,
                        "sha256": "sha256:" + digest.hexdigest()}
            finally:
                response.close()
        except urllib.error.HTTPError as error:
            if error.code in (404, 410):
                raise RepositoryMaterializationError(
                    f"dependency wheel URL 不可用: HTTP {error.code}") from error
            raise RepositoryTransportError(
                f"dependency wheel HTTP 失败: {error.code}") from error
        except RepositoryMaterializationError:
            raise
        except RepositoryTransportError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise RepositoryTransportError("dependency wheel transport/IO 失败") from error

    def _validate_wheel(
            self, path: Path, expected: Mapping[str, Any]) -> tuple[int, int]:
        try:
            with zipfile.ZipFile(path, "r") as archive:
                infos = archive.infolist()
                if not 1 <= len(infos) <= self.config["max_wheel_entries"]:
                    raise RepositoryMaterializationError("wheel archive entry 数量越界")
                names = set()
                total = 0
                metadata = []
                records = []
                wheel_metadata = []
                for info in infos:
                    raw_name = info.filename
                    parts = raw_name.split("/")
                    if (not raw_name or raw_name.startswith("/") or "\\" in raw_name
                            or any(part in ("", ".", "..") for part in parts[:-1])
                            or (parts[-1] in (".", "..")) or raw_name in names
                            or info.flag_bits & 0x1):
                        raise RepositoryMaterializationError("wheel archive path/encryption 非法")
                    names.add(raw_name)
                    mode = (info.external_attr >> 16) & 0xFFFF
                    file_type = stat.S_IFMT(mode) if mode else 0
                    if file_type not in (0, stat.S_IFREG, stat.S_IFDIR):
                        raise RepositoryMaterializationError("wheel archive 含特殊文件/symlink")
                    total += info.file_size
                    if total > self.config["max_unpacked_wheel_bytes"]:
                        raise RepositoryMaterializationError("wheel archive 解压总量越界")
                    if raw_name.endswith(".dist-info/METADATA"):
                        metadata.append(info)
                    if raw_name.endswith(".dist-info/RECORD"):
                        records.append(info)
                    if raw_name.endswith(".dist-info/WHEEL"):
                        wheel_metadata.append(info)
                if (len(metadata) != 1 or len(records) != 1
                        or len(wheel_metadata) != 1
                        or metadata[0].file_size > 1024 * 1024
                        or wheel_metadata[0].file_size > 1024 * 1024):
                    raise RepositoryMaterializationError("wheel METADATA/RECORD 闭包非法")
                control_paths = [
                    PurePosixPath(item.filename)
                    for item in (metadata[0], records[0], wheel_metadata[0])]
                if (any(len(item.parts) != 2 for item in control_paths)
                        or len({item.parent for item in control_paths}) != 1):
                    raise RepositoryMaterializationError(
                        "wheel dist-info control files 未绑定同一 top-level directory")
                dist_info = control_paths[0].parent.name
                identity = dist_info[:-len(".dist-info")]
                if (not dist_info.endswith(".dist-info") or "-" not in identity):
                    raise RepositoryMaterializationError("wheel dist-info identity 非法")
                dist_name, dist_version = identity.rsplit("-", 1)
                if (_normalized_name(dist_name) != expected["name"]
                        or dist_version != expected["version"]):
                    raise RepositoryMaterializationError(
                        "wheel dist-info directory 与 lock identity 不一致")
                message = email.parser.BytesParser().parsebytes(archive.read(metadata[0]))
                names_meta = message.get_all("Name") or []
                versions_meta = message.get_all("Version") or []
                if (len(names_meta) != 1 or len(versions_meta) != 1
                        or _normalized_name(names_meta[0]) != expected["name"]
                        or versions_meta[0] != expected["version"]):
                    raise RepositoryMaterializationError("wheel METADATA name/version 与 lock 不一致")
                return total, len(infos)
        except RepositoryMaterializationError:
            raise
        except (OSError, zipfile.BadZipFile, RuntimeError, ValueError) as error:
            raise RepositoryMaterializationError("wheel archive 无法严格解析") from error

    def _wheel_artifact(
            self, item: Mapping[str, Any], artifacts: Path
    ) -> tuple[Path, Dict[str, Any], int, int]:
        destination = artifacts / (item["sha256"].removeprefix("sha256:") + ".whl")
        if os.path.lexists(destination):
            try:
                with open_artifact(
                        destination, expected_hash=item["sha256"],
                        expected_size=item["bytes"], label="cached dependency wheel"):
                    pass
            except (ArtifactCapabilityError, OSError) as error:
                raise RepositoryCacheError("cached dependency wheel identity 漂移") from error
        else:
            temporary = destination.with_name(
                f".{destination.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp")
            try:
                evidence = (self.wheel_fetcher(
                    item["url"], temporary, self.config["max_wheel_bytes"])
                    if self.wheel_fetcher is not None
                    else self._download_wheel(
                        item["url"], temporary, self.config["max_wheel_bytes"]))
                actual_hash, actual_size = _hash_file(
                    temporary, maximum=self.config["max_wheel_bytes"])
                if (not isinstance(evidence, Mapping)
                        or evidence.get("sha256") != actual_hash
                        or evidence.get("bytes") != actual_size
                        or actual_hash != item["sha256"] or actual_size != item["bytes"]):
                    raise RepositoryTransportError("dependency wheel fetch evidence/hash/size 漂移")
                os.chmod(temporary, 0o400)
                os.replace(temporary, destination)
                _fsync_directory(artifacts)
            finally:
                try:
                    temporary.unlink()
                except OSError:
                    pass
        unpacked_bytes, archive_entries = self._validate_wheel(destination, item)
        return destination, {
            "name": item["name"], "version": item["version"],
            "filename": item["filename"], "url": item["url"],
            "sha256": item["sha256"], "bytes": item["bytes"],
        }, unpacked_bytes, archive_entries
