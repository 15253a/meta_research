"""Small, file-backed input firewall for the §7.4 T1/T2 qualification runs.

The ordinary research state machine stays generic.  A qualification work root
optionally contains one immutable contract which narrows that generic runtime:

* research Codex calls have no host tools;
* Docker receives only the readonly roots referenced by this invocation;
* T1 cannot mount its DREAMER view before final consumption;
* T2 can mount at most one LOSO fold view at a time; and
* final consumption is irreversible, so released target metrics cannot feed a
  later research cycle.

This module intentionally adds neither a database nor a scheduler.  The only
state is a contract, one claim lock and one final-consumed marker, all canonical
JSON published with no-clobber semantics and directory fsync.
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import math
import os
import re
import secrets
import stat
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Mapping, Optional, Sequence


CONTRACT_PROTOCOL = "meta-research-qualification-firewall/v1"
CLAIM_PROTOCOL = "meta-research-qualification-claim-lock/v1"
FINAL_PROTOCOL = "meta-research-qualification-final-consumed/v1"
CONTRACT_RELATIVE_PATH = Path("state/qualification/contract.json")
CLAIM_RELATIVE_PATH = Path("state/qualification/claim-lock.json")
FINAL_RELATIVE_PATH = Path("state/qualification/final-consumed.json")
VIEW_RECEIPT_NAME = "qualification-view.json"
VIEW_RECEIPT_PROTOCOL = "meta-research-qualification-view/v1"
_MAX_JSON_BYTES = 256 * 1024
_MAX_VIEW_ENTRIES = 100_000
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_UNIT_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ksh", "csh", "tcsh", "fish", "env"})
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1


class QualificationFirewallError(RuntimeError):
    """The qualification capability contract is absent, unsafe or violated."""


class QualificationFinalizedError(QualificationFirewallError):
    """A final target was consumed; the research loop must never resume."""


class QualificationClaimLockedError(QualificationFirewallError):
    """The claim is frozen; ordinary research must not resume after phase B."""


def _canonical(value: Any) -> bytes:
    try:
        return (json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError) as error:
        raise QualificationFirewallError("qualification JSON 含非 JSON/非有限值") from error


def _hash_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _strict_json(
        raw: bytes, *, label: str, max_bytes: int = _MAX_JSON_BYTES) -> Dict[str, Any]:
    if (isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 2):
        raise QualificationFirewallError("qualification JSON max_bytes 非法")
    if not 2 <= len(raw) <= max_bytes:
        raise QualificationFirewallError(f"{label} 大小非法")

    def unique(pairs):  # noqa: ANN001 - json hook protocol
        result = {}
        for key, value in pairs:
            if key in result:
                raise QualificationFirewallError(f"{label} 含重复 key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=unique,
            parse_constant=lambda token: (_ for _ in ()).throw(
                QualificationFirewallError(f"{label} 含非有限数: {token}")))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise QualificationFirewallError(f"{label} 不是严格 UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise QualificationFirewallError(f"{label} 须为 object")
    return value


def _reconcile_publish_link(
        path: Path, *, expected_owner: Optional[int] = None,
        expected_mode: Optional[int] = None) -> None:
    # VEPFS does not implement renameat2(RENAME_NOREPLACE).  The portable
    # no-clobber fallback publishes with link(2), then removes its exact temp
    # name.  A process death between those calls leaves nlink=2; reconcile only
    # that closed naming/inode pattern before enforcing the usual nlink=1 rule.
    try:
        visible = os.lstat(path)
    except OSError:
        return
    if visible is not None and visible.st_nlink == 2 and stat.S_ISREG(visible.st_mode):
        pattern = re.compile(
            r"^\." + re.escape(path.name) + r"\.[0-9]+\.[0-9a-f]{16}\.tmp$")
        matches = []
        for name in os.listdir(path.parent):
            if not pattern.fullmatch(name):
                continue
            candidate = path.parent / name
            info = os.lstat(candidate)
            if ((info.st_dev, info.st_ino) == (visible.st_dev, visible.st_ino)
                    and stat.S_ISREG(info.st_mode)):
                matches.append(candidate)
        if (len(matches) == 1
                and (expected_owner is None or visible.st_uid == expected_owner)
                and (expected_mode is None
                     or stat.S_IMODE(visible.st_mode) == expected_mode)):
            matches[0].unlink()
            directory_fd = os.open(
                path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)


def _read_regular(path: Path, *, label: str, expected_owner: Optional[int] = None,
                  expected_mode: Optional[int] = None) -> bytes:
    _reconcile_publish_link(
        path, expected_owner=expected_owner, expected_mode=expected_mode)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise QualificationFirewallError(f"{label} 不可安全打开: {path}") from error
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_size < 2 or info.st_size > _MAX_JSON_BYTES
                or (expected_owner is not None and info.st_uid != expected_owner)
                or (expected_mode is not None
                    and stat.S_IMODE(info.st_mode) != expected_mode)):
            raise QualificationFirewallError(f"{label} 身份/权限/大小非法")
        chunks = []
        remaining = info.st_size
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                raise QualificationFirewallError(f"{label} 被截断")
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    finally:
        os.close(fd)
    return raw


def _fsync_directory(path: Path) -> None:
    fd = os.open(
        path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise QualificationFirewallError("qualification fsync 目标不是目录")
        os.fsync(fd)
    finally:
        os.close(fd)


def _rename_noreplace(source: Path, destination: Path) -> None:
    """Atomically publish one inode without the hard-link crash window."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise QualificationFirewallError("qualification publish 需要 renameat2(RENAME_NOREPLACE)")
    renameat2.argtypes = [
        ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    if renameat2(
            _AT_FDCWD, os.fsencode(source), _AT_FDCWD,
            os.fsencode(destination), _RENAME_NOREPLACE) == 0:
        return
    code = ctypes.get_errno()
    if code in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(destination)
    if code in {errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP}:
        try:
            os.link(source, destination, follow_symlinks=False)
        except FileExistsError:
            raise
        except OSError as error:
            raise QualificationFirewallError(
                "qualification filesystem 不支持安全 no-clobber publish") from error
        return
    raise QualificationFirewallError(
        f"qualification atomic publish 失败: {os.strerror(code)}")


def _publish_once(path: Path, payload: bytes, *, mode: int = 0o400) -> bool:
    """Publish exact bytes once; an identical visible file is idempotent."""
    if len(payload) > _MAX_JSON_BYTES:
        raise QualificationFirewallError("qualification receipt 超过大小上限")
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent_info = os.lstat(parent)
    if (not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(parent_info.st_mode)
            or parent_info.st_uid != os.geteuid()):
        raise QualificationFirewallError("qualification receipt 目录身份非法")
    os.chmod(parent, 0o700)
    if os.path.lexists(path):
        current = _read_regular(
            path, label=path.name, expected_owner=os.geteuid(), expected_mode=mode)
        if current != payload:
            raise QualificationFirewallError(f"{path.name} 已存在且内容冲突")
        _fsync_directory(parent)
        return False
    tmp = parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
             | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    fd = -1
    try:
        fd = os.open(tmp, flags, mode)
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("qualification receipt short write")
            view = view[written:]
        os.fchmod(fd, mode)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        try:
            _rename_noreplace(tmp, path)
        except FileExistsError:
            current = _read_regular(
                path, label=path.name, expected_owner=os.geteuid(), expected_mode=mode)
            if current != payload:
                raise QualificationFirewallError(f"{path.name} 并发发布内容冲突")
            return False
        _fsync_directory(parent)
        return True
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        _fsync_directory(parent)


def _absolute_path(value: Any, *, label: str) -> Path:
    if (not isinstance(value, str) or not value or "\x00" in value
            or not os.path.isabs(value) or os.path.normpath(value) != value):
        raise QualificationFirewallError(f"{label} 须为规范绝对路径")
    path = Path(value)
    try:
        info = os.lstat(path)
    except OSError as error:
        raise QualificationFirewallError(f"{label} 缺失: {path}") from error
    if stat.S_ISLNK(info.st_mode) or os.path.realpath(path) != str(path):
        raise QualificationFirewallError(f"{label} 路径不得含 symlink")
    return path


def _safe_integer(value: Any, *, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise QualificationFirewallError(f"{label} 须为 >= {minimum} 的整数")
    return value


def _nonempty_json(value: Any, *, label: str, expected: type) -> None:
    if not isinstance(value, expected) or not value:
        raise QualificationFirewallError(f"{label} 须为非空 {expected.__name__}")
    if len(_canonical(value)) > 64 * 1024:
        raise QualificationFirewallError(f"{label} 超过大小上限")


def _paths_overlap(left: Path, right: Path) -> bool:
    try:
        common = os.path.commonpath((str(left), str(right)))
    except ValueError:
        return False
    return common in {str(left), str(right)}


def _view_file_hash(path: Path, cache: Dict[tuple[int, ...], str]) -> tuple[str, int]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise QualificationFirewallError(f"qualification view payload 不可安全打开: {path}") from error
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise QualificationFirewallError(f"qualification view payload 非常规文件: {path}")
        key = (
            before.st_dev, before.st_ino, before.st_size,
            before.st_mtime_ns, before.st_ctime_ns,
        )
        cached = cache.get(key)
        if cached is not None:
            return cached, before.st_size
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(fd)
        if ((after.st_dev, after.st_ino, after.st_size,
             after.st_mtime_ns, after.st_ctime_ns) != key
                or size != before.st_size):
            raise QualificationFirewallError(
                f"qualification view payload 读取期间身份漂移: {path}")
        value = "sha256:" + digest.hexdigest()
        cache[key] = value
        return value, size
    finally:
        os.close(fd)


def _walk_view(root: Path, *, expected_owner: int,
               hash_cache: Optional[Dict[tuple[int, ...], str]] = None) -> Dict[str, tuple[str, int]]:
    """Reject alternate file capabilities and optionally hash one immutable view."""
    actual: Dict[str, tuple[str, int]] = {}
    entries = 0
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        dirs.sort()
        files.sort()
        current_path = Path(current)
        current_info = os.lstat(current_path)
        if (not stat.S_ISDIR(current_info.st_mode)
                or current_info.st_uid != expected_owner
                or current_info.st_mode & 0o222):
            raise QualificationFirewallError(
                f"qualification view 目录 owner/只读权限非法: {current_path}")
        entries += 1
        if entries > _MAX_VIEW_ENTRIES:
            raise QualificationFirewallError("qualification view 文件树超过条目上限")
        for name in dirs:
            child = current_path / name
            info = os.lstat(child)
            if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
                    or info.st_uid != expected_owner or info.st_mode & 0o222):
                raise QualificationFirewallError(
                    f"qualification view 含非法/可写目录: {child}")
        for name in files:
            child = current_path / name
            info = os.lstat(child)
            if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
                    or info.st_uid != expected_owner or info.st_mode & 0o222):
                raise QualificationFirewallError(
                    f"qualification view 含非法/可写文件: {child}")
            entries += 1
            if entries > _MAX_VIEW_ENTRIES:
                raise QualificationFirewallError("qualification view 文件树超过条目上限")
            rel = child.relative_to(root).as_posix()
            if rel == VIEW_RECEIPT_NAME:
                continue
            if hash_cache is None:
                actual[rel] = ("", info.st_size)
            else:
                actual[rel] = _view_file_hash(child, hash_cache)
    if not actual:
        raise QualificationFirewallError("qualification view 不得为空")
    return actual


def _validate_view_receipt(
        root: Path, expected_hash: str, *, expected_owner: int,
        task: str, role: Any, dataset: str, fold: Any,
        hash_cache: Dict[tuple[int, ...], str]) -> Dict[str, Any]:
    if not isinstance(expected_hash, str) or _SHA256_RE.fullmatch(expected_hash) is None:
        raise QualificationFirewallError("view_receipt_sha256 非法")
    raw = _read_regular(
        root / VIEW_RECEIPT_NAME, label="qualification view receipt",
        expected_owner=expected_owner, expected_mode=0o444)
    if _hash_bytes(raw) != expected_hash:
        raise QualificationFirewallError("qualification view receipt hash 不符")
    value = _strict_json(raw, label="qualification view receipt")
    if raw != _canonical(value):
        raise QualificationFirewallError("qualification view receipt 非 canonical")
    if (set(value) != {
            "version", "protocol", "task", "role", "dataset", "fold",
            "adapter", "adapter_version", "files"}
            or value.get("version") != 1
            or value.get("protocol") != VIEW_RECEIPT_PROTOCOL
            or value.get("task") != task or value.get("role") != role
            or value.get("dataset") != dataset or value.get("fold") != fold
            or not isinstance(value.get("adapter"), str) or not value["adapter"]
            or len(value["adapter"].encode("utf-8")) > 256
            or isinstance(value.get("adapter_version"), bool)
            or not isinstance(value.get("adapter_version"), int)
            or value["adapter_version"] < 1
            or not isinstance(value.get("files"), list)
            or not 1 <= len(value["files"]) <= _MAX_VIEW_ENTRIES):
        raise QualificationFirewallError("qualification view receipt 字段/身份非法")
    declared: Dict[str, tuple[str, int]] = {}
    for index, row in enumerate(value["files"]):
        if not isinstance(row, dict) or set(row) != {"path", "sha256", "bytes"}:
            raise QualificationFirewallError(
                f"qualification view receipt files[{index}] 字段闭包非法")
        raw_path = row["path"]
        if (not isinstance(raw_path, str) or not raw_path or "\\" in raw_path
                or "\x00" in raw_path):
            raise QualificationFirewallError("qualification view receipt 路径非法")
        rel = PurePosixPath(raw_path)
        if (rel.is_absolute() or rel.as_posix() != raw_path
                or any(part in {"", ".", ".."} for part in rel.parts)
                or raw_path == VIEW_RECEIPT_NAME or raw_path in declared
                or not isinstance(row["sha256"], str)
                or _SHA256_RE.fullmatch(row["sha256"]) is None
                or isinstance(row["bytes"], bool) or not isinstance(row["bytes"], int)
                or row["bytes"] < 0):
            raise QualificationFirewallError("qualification view receipt ledger 非法")
        declared[raw_path] = (row["sha256"], row["bytes"])
    actual = _walk_view(
        root, expected_owner=expected_owner, hash_cache=hash_cache)
    if actual != declared:
        raise QualificationFirewallError(
            "qualification view 文件树与 receipt exact ledger 不符")
    return value


@dataclass(frozen=True)
class QualificationMount:
    path: Path
    role: str
    dataset: str
    fold: Optional[int]
    view_receipt_sha256: Optional[str]
    adapter: Optional[str]
    adapter_version: Optional[int]


class QualificationFirewall:
    """Validated qualification contract and invocation mount authorizer."""

    def __init__(self, *, work_root: Path, value: Mapping[str, Any], raw: bytes,
                 require_research_uid: bool = True):
        self.work_root = Path(os.path.abspath(os.fspath(work_root)))
        self.value = dict(value)
        self.raw = raw
        self.contract_sha256 = _hash_bytes(raw)
        self.task = ""
        self.research_uid = -1
        self.evaluator_uid = -1
        self.mounts: tuple[QualificationMount, ...] = ()
        self._view_hash_cache: Dict[tuple[int, ...], str] = {}
        self.sealed_truth_path = Path()
        self.sealed_truth_sha256 = ""
        self.t1_label_rule: Optional[Dict[str, Any]] = None
        self.final: Dict[str, Any] = {}
        self._validate(require_research_uid=require_research_uid)

    @property
    def contract_path(self) -> Path:
        return self.work_root / CONTRACT_RELATIVE_PATH

    @property
    def claim_path(self) -> Path:
        return self.work_root / CLAIM_RELATIVE_PATH

    @property
    def final_path(self) -> Path:
        return self.work_root / FINAL_RELATIVE_PATH

    def _validate(self, *, require_research_uid: bool) -> None:
        expected = {
            "version", "protocol", "task", "research_uid", "evaluator_uid",
            "forbid_code_imports", "mounts", "sealed_truth", "final",
        }
        value = self.value
        if (set(value) != expected or value.get("version") != 1
                or value.get("protocol") != CONTRACT_PROTOCOL
                or value.get("task") not in {"T1", "T2"}
                or value.get("forbid_code_imports") is not True):
            raise QualificationFirewallError("qualification contract 字段闭包/协议非法")
        self.task = value["task"]
        self.research_uid = _safe_integer(
            value["research_uid"], label="research_uid", minimum=0)
        self.evaluator_uid = _safe_integer(
            value["evaluator_uid"], label="evaluator_uid", minimum=0)
        if self.research_uid == self.evaluator_uid:
            raise QualificationFirewallError("research_uid 与 evaluator_uid 必须不同")
        if require_research_uid and os.geteuid() != self.research_uid:
            raise QualificationFirewallError("当前进程 UID 不是 qualification research_uid")

        raw_mounts = value["mounts"]
        if not isinstance(raw_mounts, list) or not raw_mounts or len(raw_mounts) > 64:
            raise QualificationFirewallError("qualification mounts 须为有界非空数组")
        mounts = []
        seen_paths = set()
        for index, item in enumerate(raw_mounts):
            if not isinstance(item, dict) or set(item) != {
                    "path", "role", "dataset", "fold", "view_receipt_sha256"}:
                raise QualificationFirewallError(f"mounts[{index}] 字段闭包非法")
            path = _absolute_path(item["path"], label=f"mounts[{index}].path")
            info = os.lstat(path)
            if not stat.S_ISDIR(info.st_mode):
                raise QualificationFirewallError("qualification mount 须为目录")
            if info.st_uid != self.research_uid or info.st_mode & 0o222:
                raise QualificationFirewallError("qualification mount owner/只读权限非法")
            if str(path) in seen_paths:
                raise QualificationFirewallError("qualification mounts 含重复路径")
            seen_paths.add(str(path))
            role, dataset, fold = item["role"], item["dataset"], item["fold"]
            if (not isinstance(dataset, str) or not dataset or len(dataset) > 128
                    or any(ord(char) < 0x20 or ord(char) == 0x7f for char in dataset)):
                raise QualificationFirewallError("qualification mount dataset 非法")
            receipt_hash = item["view_receipt_sha256"]
            receipt = None
            if receipt_hash is not None:
                receipt = _validate_view_receipt(
                    path, receipt_hash, expected_owner=self.research_uid,
                    task=self.task, role=role, dataset=dataset, fold=fold,
                    hash_cache=self._view_hash_cache)
            else:
                _walk_view(path, expected_owner=self.research_uid)
            mounts.append(QualificationMount(
                path=path, role=role, dataset=dataset, fold=fold,
                view_receipt_sha256=receipt_hash,
                adapter=(None if receipt is None else receipt["adapter"]),
                adapter_version=(None if receipt is None else receipt["adapter_version"])))
        for index, left in enumerate(mounts):
            for right in mounts[index + 1:]:
                if _paths_overlap(left.path, right.path):
                    raise QualificationFirewallError(
                        "qualification mount roots 必须互不为祖先/后代")

        truth = value["sealed_truth"]
        if not isinstance(truth, dict) or set(truth) != {"path", "sha256"}:
            raise QualificationFirewallError("sealed_truth 字段闭包非法")
        truth_path = _absolute_path(truth["path"], label="sealed_truth.path")
        truth_info = os.lstat(truth_path)
        if (not stat.S_ISREG(truth_info.st_mode) or truth_info.st_nlink != 1
                or truth_info.st_uid != self.evaluator_uid
                or stat.S_IMODE(truth_info.st_mode) != 0o400
                or not isinstance(truth["sha256"], str)
                or _SHA256_RE.fullmatch(truth["sha256"]) is None):
            raise QualificationFirewallError("sealed truth owner/mode/hash 身份非法")
        for mount in mounts:
            if _paths_overlap(mount.path, truth_path):
                raise QualificationFirewallError("sealed truth 不得落入 research mount 能力")
        self.sealed_truth_path = truth_path
        self.sealed_truth_sha256 = truth["sha256"]

        final = value["final"]
        if not isinstance(final, dict) or set(final) != {
                "classes", "seeds", "folds", "unit_ids", "gpu_required"}:
            raise QualificationFirewallError("qualification final 字段闭包非法")
        if not isinstance(final["gpu_required"], bool):
            raise QualificationFirewallError("qualification final.gpu_required 须为 bool")
        if self.task == "T1":
            explores = [mount for mount in mounts if mount.role == "explore"]
            holdouts = [mount for mount in mounts if mount.role == "sealed_holdout"]
            if (len({mount.dataset.casefold() for mount in explores}) < 3
                    or len(holdouts) != 1
                    or holdouts[0].dataset.casefold() != "dreamer"
                    or holdouts[0].view_receipt_sha256 is None
                    or holdouts[0].adapter != "meta-research-dreamer-public-view"
                    or holdouts[0].adapter_version != 1
                    or any(mount.role not in {"explore", "sealed_holdout"} for mount in mounts)
                    or any(mount.fold is not None for mount in mounts)
                    or any(mount.dataset.casefold() == "dreamer" for mount in explores)
                    or final["classes"] != 2 or final["seeds"] != []
                    or final["folds"] != [] or final["unit_ids"] != ["dreamer"]):
                raise QualificationFirewallError("T1 qualification mount/final 合同非法")
            manifest_raw = _read_regular(
                holdouts[0].path / "manifest.json",
                label="DREAMER public manifest", expected_owner=self.research_uid,
                expected_mode=0o444)
            manifest = _strict_json(manifest_raw, label="DREAMER public manifest")
            if manifest_raw != _canonical(manifest):
                raise QualificationFirewallError("DREAMER public manifest 非 canonical")
            try:
                rule = manifest["label_rule"]
            except KeyError as error:
                raise QualificationFirewallError(
                    "DREAMER public manifest 缺冻结 label_rule") from error
            if (not isinstance(rule, dict) or set(rule) != {
                    "score", "threshold", "comparison", "neutral_policy"}
                    or rule.get("score") != "valence"
                    or rule.get("comparison") != "higher_is_positive"
                    or isinstance(rule.get("threshold"), bool)
                    or not isinstance(rule.get("threshold"), (int, float))
                    or not math.isfinite(float(rule["threshold"]))
                    or rule.get("neutral_policy") not in {"drop", "negative", "positive"}):
                raise QualificationFirewallError(
                    "DREAMER public manifest label_rule 非法")
            self.t1_label_rule = {
                "score": "valence", "threshold": float(rule["threshold"]),
                "comparison": "higher_is_positive",
                "neutral_policy": rule["neutral_policy"],
            }
        else:
            if (len(mounts) != 15
                    or any(mount.role != "fold" or mount.dataset.casefold() != "seed"
                           or mount.view_receipt_sha256 is None
                           or mount.adapter != "meta-research-seed-public-view"
                           or mount.adapter_version != 1 for mount in mounts)
                    or any(isinstance(mount.fold, bool) or not isinstance(mount.fold, int)
                           or not 1 <= mount.fold <= 15 for mount in mounts)
                    or sorted(mount.fold for mount in mounts) != list(range(1, 16))
                    or final["classes"] != 3 or final["folds"] != list(range(1, 16))
                    or final["unit_ids"] != []
                    or not isinstance(final["seeds"], list)
                    or len(final["seeds"]) != 3
                    or len(set(final["seeds"])) != 3
                    or any(isinstance(seed, bool) or not isinstance(seed, int)
                           or not 0 <= seed <= 2147483647 for seed in final["seeds"])):
                raise QualificationFirewallError("T2 qualification 3×15 mount/final 合同非法")
        self.mounts = tuple(mounts)
        self.final = json.loads(_canonical(final).decode("utf-8"))

    def validate_policy(self, policy: Mapping[str, Any]) -> None:
        try:
            path_allowlist = policy["execution"]["path_allowlist"]
            readonly_mounts = policy["execution"]["sandbox"]["readonly_mounts"]
            deployment_mode = policy["deployment"]["mode"]
        except (KeyError, TypeError) as error:
            raise QualificationFirewallError("policy 缺 qualification 执行边界") from error
        expected = {str(item.path) for item in self.mounts}
        if (not isinstance(path_allowlist, list) or not isinstance(readonly_mounts, list)
                or set(path_allowlist) != expected or set(readonly_mounts) != expected
                or len(path_allowlist) != len(expected)
                or len(readonly_mounts) != len(expected)):
            raise QualificationFirewallError(
                "qualification policy 只允许且必须列出 contract 的精确 research mounts")
        if deployment_mode == "production" and (
                self.research_uid == 0 or self.evaluator_uid != 0):
            raise QualificationFirewallError(
                "production qualification 要求 non-root research_uid 与 root evaluator_uid")

    def assert_research_open(self) -> None:
        if os.path.lexists(self.final_path):
            self.read_final_marker()
            raise QualificationFinalizedError(
                "qualification final 已消费；禁止恢复研究循环或让 target 指标反馈下一轮")
        if os.path.lexists(self.claim_path):
            self.read_claim_lock()
            raise QualificationClaimLockedError(
                "qualification claim 已锁定；普通研究循环已关闭，请进入资格执行阶段")

    def authorize_mounts(
            self, selected_paths: Sequence[str], *, execution_context: Mapping[str, Any]) -> None:
        if isinstance(selected_paths, (str, bytes)):
            raise QualificationFirewallError("selected mounts 须为路径序列")
        by_path = {str(item.path): item for item in self.mounts}
        if len(set(selected_paths)) != len(selected_paths):
            raise QualificationFirewallError("同一 qualification mount 被重复选择")
        try:
            selected = [by_path[path] for path in selected_paths]
        except KeyError as error:
            raise QualificationFirewallError("执行请求了 contract 外 research mount") from error
        final_consumed = os.path.lexists(self.final_path)
        for mount in selected:
            if mount.view_receipt_sha256 is None:
                _walk_view(mount.path, expected_owner=self.research_uid)
            else:
                _validate_view_receipt(
                    mount.path, mount.view_receipt_sha256,
                    expected_owner=self.research_uid, task=self.task,
                    role=mount.role, dataset=mount.dataset, fold=mount.fold,
                    hash_cache=self._view_hash_cache)
        phase = execution_context.get("phase")
        if final_consumed:
            self.read_final_marker()
            if phase != "qualification-final":
                raise QualificationFinalizedError("final consumed 后只允许 qualification-final 续接")
        if self.task == "T1":
            holdout = [item for item in selected if item.role == "sealed_holdout"]
            if holdout and not final_consumed:
                raise QualificationFirewallError(
                    "DREAMER sealed holdout 在 A/B/C/HPO/claim 阶段不可挂载")
            if final_consumed and (len(holdout) != 1 or len(selected) != 1):
                raise QualificationFirewallError("T1 final 只允许精确 DREAMER X-only view")
        else:
            if selected and not final_consumed:
                raise QualificationFirewallError(
                    "T2 final folds 在冻结源码并消费 final 前不可挂载，"
                    "防跨 invocation 累积 source_y 后还原 target_y")
            if final_consumed and (len(selected) != 1 or phase != "qualification-final"):
                raise QualificationFirewallError(
                    "T2 final 每次只允许一个冻结 LOSO fold")

    def read_claim_lock(self) -> tuple[Dict[str, Any], bytes]:
        raw = _read_regular(
            self.claim_path, label="qualification claim lock",
            expected_owner=self.research_uid, expected_mode=0o400)
        value = _strict_json(raw, label="qualification claim lock")
        if raw != _canonical(value):
            raise QualificationFirewallError("qualification claim lock 非 canonical")
        _validate_claim(value, self)
        return value, raw

    def read_final_marker(self) -> Dict[str, Any]:
        raw = _read_regular(
            self.final_path, label="qualification final marker",
            expected_owner=self.research_uid, expected_mode=0o400)
        value = _strict_json(raw, label="qualification final marker")
        if raw != _canonical(value):
            raise QualificationFirewallError("qualification final marker 非 canonical")
        if (set(value) != {
                "version", "protocol", "task", "contract_sha256", "claim_sha256",
                "source_tree_sha256", "runtime_identity_sha256",
                "gpu_canary_sha256", "units", "consumed_at_unix"}
                or value.get("version") != 1 or value.get("protocol") != FINAL_PROTOCOL
                or value.get("task") != self.task
                or value.get("contract_sha256") != self.contract_sha256
                or not isinstance(value.get("claim_sha256"), str)
                or _SHA256_RE.fullmatch(value["claim_sha256"]) is None
                or not isinstance(value.get("source_tree_sha256"), str)
                or _SHA256_RE.fullmatch(value["source_tree_sha256"]) is None
                or not isinstance(value.get("runtime_identity_sha256"), str)
                or _SHA256_RE.fullmatch(value["runtime_identity_sha256"]) is None
                or (self.final["gpu_required"] and (
                    not isinstance(value.get("gpu_canary_sha256"), str)
                    or _SHA256_RE.fullmatch(value["gpu_canary_sha256"]) is None))
                or (not self.final["gpu_required"]
                    and value.get("gpu_canary_sha256") is not None)
                or not isinstance(value.get("consumed_at_unix"), (int, float))
                or isinstance(value.get("consumed_at_unix"), bool)
                or not math.isfinite(float(value["consumed_at_unix"]))
                or float(value["consumed_at_unix"]) <= 0):
            raise QualificationFirewallError("qualification final marker 字段/绑定非法")
        expected_units = final_units(self)
        if value.get("units") != expected_units:
            raise QualificationFirewallError("qualification final marker units 非冻结全集")
        return value


def _validate_command(value: Any, firewall: QualificationFirewall) -> None:
    if not isinstance(value, dict) or set(value) != {"argv", "output", "gpu_required"}:
        raise QualificationFirewallError("claim final_command 字段闭包非法")
    argv = value["argv"]
    if (not isinstance(argv, list) or not 1 <= len(argv) <= 128
            or any(not isinstance(token, str) or not token or "\x00" in token
                   or len(token.encode("utf-8")) > 8192 for token in argv)):
        raise QualificationFirewallError("claim final_command.argv 非法")
    if argv[0].rsplit("/", 1)[-1] in _SHELLS:
        raise QualificationFirewallError("claim final_command 不得以 shell/env 启动")
    joined = "\n".join(argv)
    required = {"{src}", "{data}"}
    if firewall.task == "T2":
        required |= {"{seed}", "{fold}"}
    if any(token not in joined for token in required):
        raise QualificationFirewallError("claim final_command 缺冻结 placeholder")
    for path_placeholder in ("{src}", "{data}"):
        if not any(token == path_placeholder or token.startswith(path_placeholder + "/")
                   for token in argv):
            raise QualificationFirewallError(
                f"claim final_command {path_placeholder} 必须位于可识别路径前缀")
    allowed = required | {"{unit_id}"}
    placeholders = set(re.findall(r"\{[a-z_]+\}", joined))
    if not placeholders <= allowed:
        raise QualificationFirewallError("claim final_command 含未知 placeholder")
    if value["output"] != "predictions.json":
        raise QualificationFirewallError("claim final_command.output 固定为 predictions.json")
    if value["gpu_required"] is not firewall.final["gpu_required"]:
        raise QualificationFirewallError("claim GPU 模式与 qualification contract 不一致")


_T1_CONTROLS = frozenset({
    "majority", "class-prior-random", "matched-random", "label-permutation",
    "subject-id-only", "dataset-id-only", "trial-id-only", "source-only-linear",
    "confidence-only", "preprocessing-consistency", "leakage-probe",
})
_T2_CONTROLS = frozenset({
    "majority", "source-prior-random", "source-only-linear", "source-only-mlp",
    "source-only-deep", "single-best-source", "confidence-only", "label-shuffle",
    "trial-id-only",
})


def _validate_claim(value: Mapping[str, Any], firewall: QualificationFirewall) -> None:
    expected = {
        "version", "protocol", "task", "claims", "feature_operator", "label_mapping",
        "model", "preprocessing", "hpo", "search_space", "primary_metrics",
        "statistical_tests", "multiple_testing", "exclusion_rules", "controls",
        "datasets", "final_command",
    }
    if (set(value) != expected or value.get("version") != 1
            or value.get("protocol") != CLAIM_PROTOCOL
            or value.get("task") != firewall.task):
        raise QualificationFirewallError("qualification claim-lock 字段闭包/协议非法")
    claims = value["claims"]
    if (not isinstance(claims, list) or not 1 <= len(claims) <= 3
            or len({_canonical(item) for item in claims}) != len(claims)):
        raise QualificationFirewallError("主 claim 须为 1..3 个互异冻结对象")
    for key in (
            "feature_operator", "label_mapping", "model", "preprocessing", "hpo",
            "search_space", "statistical_tests", "multiple_testing", "exclusion_rules",
            "datasets"):
        _nonempty_json(value[key], label=f"claim.{key}", expected=dict)
    _nonempty_json(value["primary_metrics"], label="claim.primary_metrics", expected=list)
    controls = value["controls"]
    if (not isinstance(controls, list) or len(controls) != len(set(controls))
            or any(not isinstance(item, str) or not item for item in controls)):
        raise QualificationFirewallError("claim.controls 须为互异非空字符串数组")
    required_controls = _T1_CONTROLS if firewall.task == "T1" else _T2_CONTROLS
    missing = sorted(required_controls - set(controls))
    if missing:
        raise QualificationFirewallError(f"claim 缺 mandatory controls: {missing}")
    datasets = value["datasets"]
    if firewall.task == "T1":
        if set(datasets) != {"exploration", "confirmatory_lodo", "sealed_holdout"}:
            raise QualificationFirewallError("T1 claim.datasets 字段闭包非法")
        exploration, lodo, sealed = (
            datasets["exploration"], datasets["confirmatory_lodo"],
            datasets["sealed_holdout"])
        expected_datasets = {
            mount.dataset.casefold() for mount in firewall.mounts
            if mount.role == "explore"}
        if (not isinstance(exploration, list) or not isinstance(lodo, list)
                or any(not isinstance(item, str) or not item for item in exploration + lodo)
                or {item.casefold() for item in exploration} != expected_datasets
                or {item.casefold() for item in lodo} != expected_datasets
                or len(exploration) != len(expected_datasets)
                or len(lodo) != len(expected_datasets)
                or not isinstance(sealed, dict)
                or set(sealed) != {
                    "dataset", "score", "comparison", "threshold", "neutral_policy"}
                or str(sealed.get("dataset", "")).casefold() != "dreamer"
                or sealed.get("score") != "valence"
                or sealed.get("comparison") != "higher_is_positive"
                or isinstance(sealed.get("threshold"), bool)
                or not isinstance(sealed.get("threshold"), (int, float))
                or not math.isfinite(float(sealed["threshold"]))
                or sealed.get("neutral_policy") not in {"negative", "positive", "drop"}):
            raise QualificationFirewallError("T1 claim LODO/DREAMER binary-valence 合同非法")
        locked_rule = {
            "score": "valence", "threshold": float(sealed["threshold"]),
            "comparison": "higher_is_positive",
            "neutral_policy": sealed["neutral_policy"],
        }
        if locked_rule != firewall.t1_label_rule:
            raise QualificationFirewallError(
                "T1 claim DREAMER label_rule 与可信 public view 制备规则不一致")
    else:
        if (set(datasets) != {"dataset", "subjects", "classes", "input", "normalization",
                             "hpo_labels", "final_seeds", "final_folds"}
                or datasets.get("dataset") != "SEED"
                or datasets.get("subjects") != list(range(1, 16))
                or datasets.get("classes") != 3
                or datasets.get("input") != "1s-nonoverlap-DE-62x5"
                or datasets.get("normalization") != "per-fold"
                or datasets.get("hpo_labels") not in {"source-inner-loso", "unsupervised-target-x"}
                or datasets.get("final_seeds") != firewall.final["seeds"]
                or datasets.get("final_folds") != list(range(1, 16))):
            raise QualificationFirewallError("T2 locked SEED 3×15 protocol 非法")
    _validate_command(value["final_command"], firewall)


def final_units(firewall: QualificationFirewall) -> list[Dict[str, Any]]:
    if firewall.task == "T1":
        return [{"unit_id": "dreamer", "seed": None, "fold": None}]
    return [
        {"unit_id": f"seed-{seed}-fold-{fold:02d}", "seed": seed, "fold": fold}
        for seed in firewall.final["seeds"] for fold in range(1, 16)
    ]


def install_contract(work_root: Path | str, value: Mapping[str, Any]) -> QualificationFirewall:
    root = Path(os.path.abspath(os.fspath(work_root)))
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = os.lstat(root)
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.geteuid()):
        raise QualificationFirewallError("qualification work_root owner/类型非法")
    os.chmod(root, 0o700)
    if os.path.lexists(root / "research.sqlite") or os.path.lexists(root / "cycles"):
        raise QualificationFirewallError("qualification contract 只能在首次研究启动前安装")
    raw = _canonical(dict(value))
    parsed = _strict_json(raw, label="qualification contract")
    candidate = QualificationFirewall(
        work_root=root, value=parsed, raw=raw, require_research_uid=True)
    _publish_once(root / CONTRACT_RELATIVE_PATH, raw)
    return load_qualification_firewall(root, require_research_uid=True)  # type: ignore[return-value]


def load_qualification_firewall(
        work_root: Path | str, *, policy: Optional[Mapping[str, Any]] = None,
        require_research_uid: bool = True) -> Optional[QualificationFirewall]:
    root = Path(os.path.abspath(os.fspath(work_root)))
    path = root / CONTRACT_RELATIVE_PATH
    if not os.path.lexists(path):
        return None
    # Read owner from the payload only after verifying the file is private and
    # owned by the work-root owner.  A different evaluator may use
    # require_research_uid=False for offline scoring.
    root_info = os.lstat(root)
    raw = _read_regular(
        path, label="qualification contract", expected_owner=root_info.st_uid,
        expected_mode=0o400)
    value = _strict_json(raw, label="qualification contract")
    if raw != _canonical(value):
        raise QualificationFirewallError("qualification contract 非 canonical")
    firewall = QualificationFirewall(
        work_root=root, value=value, raw=raw,
        require_research_uid=require_research_uid)
    if firewall.research_uid != root_info.st_uid:
        raise QualificationFirewallError("qualification research_uid 与 work_root owner 不一致")
    if policy is not None:
        firewall.validate_policy(policy)
    return firewall


def publish_claim_lock(
        work_root: Path | str, value: Mapping[str, Any]) -> Dict[str, Any]:
    firewall = load_qualification_firewall(work_root, require_research_uid=True)
    if firewall is None:
        raise QualificationFirewallError("work_root 未安装 qualification contract")
    if os.path.lexists(firewall.final_path):
        firewall.read_final_marker()
        raise QualificationFinalizedError("final 已消费，不能再锁定 claim")
    claim = dict(value)
    _validate_claim(claim, firewall)
    raw = _canonical(claim)
    _publish_once(firewall.claim_path, raw)
    return {"claim_sha256": _hash_bytes(raw), "path": str(firewall.claim_path)}


def consume_final(
        work_root: Path | str, *, source_tree_sha256: str,
        runtime_identity_sha256: str, gpu_canary_sha256: Optional[str] = None,
        now: Optional[float] = None,
        validated_firewall: Optional[QualificationFirewall] = None) -> Dict[str, Any]:
    root = Path(os.path.abspath(os.fspath(work_root)))
    if validated_firewall is None:
        firewall = load_qualification_firewall(root, require_research_uid=True)
        if firewall is None:
            raise QualificationFirewallError("work_root 未安装 qualification contract")
    else:
        firewall = validated_firewall
        if (not isinstance(firewall, QualificationFirewall)
                or firewall.work_root != root or os.geteuid() != firewall.research_uid):
            raise QualificationFirewallError(
                "consume_final validated firewall/work_root/UID 错配")
        contract_raw = _read_regular(
            firewall.contract_path, label="qualification contract",
            expected_owner=firewall.research_uid, expected_mode=0o400)
        if _hash_bytes(contract_raw) != firewall.contract_sha256:
            raise QualificationFirewallError(
                "consume_final qualification contract hash 漂移")
    if not isinstance(source_tree_sha256, str) or _SHA256_RE.fullmatch(source_tree_sha256) is None:
        raise QualificationFirewallError("source_tree_sha256 非法")
    if (not isinstance(runtime_identity_sha256, str)
            or _SHA256_RE.fullmatch(runtime_identity_sha256) is None):
        raise QualificationFirewallError("runtime_identity_sha256 非法")
    if (firewall.final["gpu_required"] and (
            not isinstance(gpu_canary_sha256, str)
            or _SHA256_RE.fullmatch(gpu_canary_sha256) is None)):
        raise QualificationFirewallError("GPU final 缺可信 canary hash")
    if not firewall.final["gpu_required"] and gpu_canary_sha256 is not None:
        raise QualificationFirewallError("CPU final 不接受 GPU canary hash")
    _claim, claim_raw = firewall.read_claim_lock()
    timestamp = time.time() if now is None else now
    if (isinstance(timestamp, bool) or not isinstance(timestamp, (int, float))
            or not math.isfinite(float(timestamp)) or float(timestamp) <= 0):
        raise QualificationFirewallError("final consumed timestamp 非法")
    marker = {
        "version": 1, "protocol": FINAL_PROTOCOL, "task": firewall.task,
        "contract_sha256": firewall.contract_sha256,
        "claim_sha256": _hash_bytes(claim_raw),
        "source_tree_sha256": source_tree_sha256,
        "runtime_identity_sha256": runtime_identity_sha256,
        "gpu_canary_sha256": gpu_canary_sha256,
        "units": final_units(firewall),
        "consumed_at_unix": float(timestamp),
    }
    raw = _canonical(marker)
    if os.path.lexists(firewall.final_path):
        existing = firewall.read_final_marker()
        comparable = dict(existing)
        comparable["consumed_at_unix"] = marker["consumed_at_unix"]
        if comparable != marker:
            raise QualificationFinalizedError("final 已由另一冻结输入消费")
        return existing
    _publish_once(firewall.final_path, raw)
    return firewall.read_final_marker()


def _load_json_file(path: Path, *, label: str) -> Dict[str, Any]:
    raw = _read_regular(path, label=label)
    value = _strict_json(raw, label=label)
    if raw != _canonical(value):
        raise QualificationFirewallError(f"{label} 须为 canonical JSON + newline")
    return value


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="meta-research T1/T2 qualification firewall")
    parser.add_argument("--work-root", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    install = sub.add_parser("install-contract")
    install.add_argument("--contract", required=True)
    claim = sub.add_parser("lock-claim")
    claim.add_argument("--claim", required=True)
    sub.add_parser("verify")
    args = parser.parse_args(argv)
    root = Path(os.path.abspath(args.work_root))
    from .instance_lease import InstanceLease
    lease = InstanceLease.acquire(root)
    primary: Optional[BaseException] = None
    try:
        if args.command == "install-contract":
            result = install_contract(
                root, _load_json_file(
                    Path(args.contract), label="qualification contract input"))
            output = {"task": result.task, "contract_sha256": result.contract_sha256}
        elif args.command == "lock-claim":
            output = publish_claim_lock(
                root, _load_json_file(Path(args.claim), label="qualification claim input"))
        else:
            firewall = load_qualification_firewall(root, require_research_uid=True)
            if firewall is None:
                raise QualificationFirewallError("work_root 未安装 qualification contract")
            output = {
                "task": firewall.task, "contract_sha256": firewall.contract_sha256,
                "claim_locked": os.path.lexists(firewall.claim_path),
                "final_consumed": os.path.lexists(firewall.final_path),
                "research_mounts": [str(item.path) for item in firewall.mounts],
            }
            if output["claim_locked"]:
                firewall.read_claim_lock()
            if output["final_consumed"]:
                firewall.read_final_marker()
    except BaseException as error:
        primary = error
        raise
    finally:
        close_error = lease.close()
        if close_error is not None:
            if primary is not None:
                note = getattr(primary, "add_note", None)
                if callable(note):
                    note(f"qualification CLI lease close 失败: {close_error}")
            else:
                raise close_error
    print(json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CLAIM_PROTOCOL", "CONTRACT_PROTOCOL", "FINAL_PROTOCOL",
    "QualificationClaimLockedError", "QualificationFinalizedError", "QualificationFirewall",
    "QualificationFirewallError", "QualificationMount", "consume_final",
    "final_units", "install_contract", "load_qualification_firewall",
    "publish_claim_lock",
]
