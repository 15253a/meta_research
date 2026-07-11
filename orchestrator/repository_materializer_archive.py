"""Safe archive extraction, submodule closure, LFS detection, and license binding."""
from __future__ import annotations

import base64
import configparser
import hashlib
import os
import re
import stat
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Mapping, Sequence

from .artifact_capability import read_artifact_bytes
from .repository_materialization_common import (
    _FULL_NAME_RE, _MAX_GITMODULES_BYTES, RepositoryMaterializationError,
    _bounded_string, _parse_lfs_pointer, _safe_component, _safe_relpath,
    _sha256, _value_hash,
)


class _RepositoryArchiveMixin:
    """Host contract: tree/download/license methods, config, and owner guard."""

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
