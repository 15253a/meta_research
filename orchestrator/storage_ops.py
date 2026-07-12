"""终态快照主干的离线 verify / restore / retention-GC 工具。

本模块不打开 writer、connector、Docker 或 Runner；它只在独占 instance lease 下读取
CP11.4c.3b.1 的 immutable pointer/manifest/backup/views 链。默认保护最近三代，旧 pointer
与 manifest 永久保留作审计记录；过期 backup 只能经 canonical dry-run plan 和
immutable applied-plan authority 删除。
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import re
import sqlite3
import stat
import uuid
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .instance_lease import (
    RESTORE_IN_PROGRESS_NAME,
    InstanceLease,
    restore_claim_bytes,
    restore_parent_claim_name,
)
from . import storage_governance as sg


VERIFY_SCHEMA = "meta-research-storage-verify/v1"
RESTORE_SCHEMA = "meta-research-storage-restore/v1"
GC_PLAN_SCHEMA = "meta-research-storage-gc-plan/v1"
GC_APPLY_SCHEMA = "meta-research-storage-gc-apply/v1"
MIN_RETAINED_GENERATIONS = 3
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_BACKUP_NAME = re.compile(r"^([0-9a-f]{64})\.sqlite$")
_RECEIPT_NAME = re.compile(r"^([0-9a-f]{64})\.json$")
_APPLIED_TEMP_NAME = re.compile(
    r"^\.[0-9a-f]{64}\.json\.tmp-[0-9a-f]{32}$")


class StorageOperationError(sg.StorageGovernanceError):
    """离线存储操作不满足 fail-closed 契约。"""


class GenerationNotRetained(StorageOperationError):
    """指定历史切面已合法退役，不是未知损坏。"""


def _cycle_number(value: str | int) -> int:
    if isinstance(value, bool):
        raise ValueError("cycle 编号非法")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and re.fullmatch(r"c[1-9][0-9]*", value):
        number = int(value[1:])
    else:
        raise ValueError("cycle 须为 cN")
    if number < 1:
        raise ValueError("cycle 须为正整数")
    return number


def _retention(value: int) -> int:
    if (isinstance(value, bool) or not isinstance(value, int)
            or value < MIN_RETAINED_GENERATIONS):
        raise ValueError(f"retain 不得小于 {MIN_RETAINED_GENERATIONS}")
    return value


def _regular_directory(path: Path, *, label: str) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as error:
        raise StorageOperationError(f"{label} 缺失: {path}") from error
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise StorageOperationError(f"{label} 不是安全目录: {path}")


def _copy_regular(source: Path, destination: Path, *, mode: int,
                  expected_hash: str, expected_bytes: int) -> None:
    source_fd = os.open(
        source, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0))
    destination_fd = -1
    try:
        info = os.fstat(source_fd)
        if not stat.S_ISREG(info.st_mode):
            raise StorageOperationError(f"恢复源不是常规文件: {source}")
        destination_fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            mode)
        digest = hashlib.sha256()
        copied = 0
        while True:
            block = os.read(source_fd, 1024 * 1024)
            if not block:
                break
            digest.update(block)
            copied += len(block)
            offset = 0
            while offset < len(block):
                offset += os.write(destination_fd, block[offset:])
        os.fsync(destination_fd)
        os.fchmod(destination_fd, mode)
        if (digest.hexdigest(), copied) != (expected_hash, expected_bytes):
            raise StorageOperationError("恢复源 backup 在同 fd 复制时身份漂移")
    finally:
        if destination_fd >= 0:
            os.close(destination_fd)
        os.close(source_fd)


def _try_rename_noreplace(source: Path, destination: Path) -> bool:
    """Return false only when this filesystem lacks renameat2 flag support."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        return False
    renameat2.argtypes = [
        ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    if renameat2(
            -100, os.fsencode(source), -100, os.fsencode(destination), 1) == 0:
        return True
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise StorageOperationError("restore target 在发布前被并发创建")
    if error_number in {
            errno.EINVAL, errno.ENOSYS, errno.ENOTSUP,
            getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)}:
        return False
    raise OSError(error_number, os.strerror(error_number), os.fspath(destination))


def _publish_lease_fenced_directory(source: Path, destination: Path) -> None:
    """VEPFS fallback: exclusive name claim + target lease + durable ready marker."""
    token = uuid.uuid4().hex
    claim = destination.parent / restore_parent_claim_name(destination)
    claim_raw = restore_claim_bytes(token)
    try:
        claim_fd = os.open(
            claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            0o400)
    except FileExistsError as error:
        raise StorageOperationError(
            "restore target 存在未完成 parent claim；拒绝覆盖") from error
    try:
        offset = 0
        while offset < len(claim_raw):
            offset += os.write(claim_fd, claim_raw[offset:])
        os.fchmod(claim_fd, 0o400)
        os.fsync(claim_fd)
    finally:
        os.close(claim_fd)
    sg._sync_dir(destination.parent)
    try:
        destination.mkdir(mode=0o700)
    except FileExistsError as error:
        raise StorageOperationError(
            "restore target 在发布前被并发创建") from error
    sg._sync_dir(destination.parent)
    marker = destination / RESTORE_IN_PROGRESS_NAME
    lease = InstanceLease.acquire(destination, restore_claim_token=token)
    primary_error: Optional[BaseException] = None
    try:
        lease.assert_owned()
        sg._atomic_write(
            marker, b"meta-research-restore-in-progress/v1\n", mode=0o400)
        sg._sync_dir(destination)
        for name in ("research.sqlite", "restore.json"):
            target = destination / name
            if os.path.lexists(target):
                raise StorageOperationError(f"restore fallback 目标条目已存在: {name}")
            lease.assert_owned()
            os.rename(source / name, target)
            sg._sync_dir(source)
            sg._sync_dir(destination)
        lease.assert_owned()
        claim.unlink()
        sg._sync_dir(destination.parent)
        lease.assert_owned()
        marker.unlink()
        sg._sync_dir(destination)
    except BaseException as error:
        primary_error = error
        raise
    finally:
        close_error = lease.close()
        if close_error is not None:
            if primary_error is None:
                raise close_error
            add_note = getattr(primary_error, "add_note", None)
            if callable(add_note):
                add_note(
                    "restore target lease close 失败；flock 保留供重试: "
                    f"{type(close_error).__name__}: {close_error}")


def _rename_noreplace(source: Path, destination: Path) -> None:
    """Publish without clobber; use a fail-closed lease fence when flags are absent."""
    if _try_rename_noreplace(source, destination):
        return
    _publish_lease_fenced_directory(source, destination)


class SnapshotArchive:
    """已持有 exact work-root lease 时使用的离线原语；不自行取锁或发布 cycle。"""

    def __init__(self, *, work_root: Path | str, lease: InstanceLease,
                 git_binary: Optional[str] = None):
        self.work_root = Path(os.path.abspath(os.fspath(work_root)))
        if (not isinstance(lease, InstanceLease)
                or Path(os.path.abspath(os.fspath(lease.work_root)))
                != self.work_root):
            raise StorageOperationError("SnapshotArchive 需要 exact work-root lease")
        lease.assert_owned()
        self.lease = lease
        self.owner_guard = lease.assert_owned
        self.publisher = sg.CycleSnapshotPublisher(
            db_path=self.work_root / "research.sqlite",
            work_root=self.work_root,
            owner_guard=self.owner_guard,
            git_binary=git_binary,
            read_only=True)
        self.gc_root = self.publisher.storage_root / "gc"
        self.applied_plans = self.gc_root / "applied" / "sha256"

    @staticmethod
    def _validate_plan_shape(plan: Mapping[str, Any]) -> None:
        if set(plan) != {
                "schema", "high_water_cycle", "high_water_manifest_sha256",
                "retain_generations", "protected", "victims", "bytes_reclaimable"}:
            raise StorageOperationError("GC plan 字段闭包漂移")
        if (plan.get("schema") != GC_PLAN_SCHEMA
                or not isinstance(plan.get("high_water_cycle"), str)
                or not isinstance(plan.get("high_water_manifest_sha256"), str)
                or _HASH_RE.fullmatch(plan["high_water_manifest_sha256"]) is None):
            raise StorageOperationError("GC plan high-water 身份非法")
        high_water = _cycle_number(plan["high_water_cycle"])
        retain = _retention(plan.get("retain_generations"))
        if not isinstance(plan.get("protected"), list) or not isinstance(
                plan.get("victims"), list):
            raise StorageOperationError("GC plan 集合非法")
        if not plan["protected"] or len(plan["protected"]) > retain:
            raise StorageOperationError("GC protected 数量非法")
        previous = None
        protected_cycles = set()
        for item in plan["protected"]:
            if (not isinstance(item, dict)
                    or set(item) != {"cycle_id", "backup_sha256"}
                    or not isinstance(item.get("backup_sha256"), str)
                    or _HASH_RE.fullmatch(item["backup_sha256"]) is None):
                raise StorageOperationError("GC protected 身份非法")
            number = _cycle_number(item.get("cycle_id"))
            if previous is not None and number <= previous:
                raise StorageOperationError("GC protected 未严格排序")
            previous = number
            protected_cycles.add(number)
        if previous != high_water:
            raise StorageOperationError("GC protected 未闭合到 high-water")
        total = 0
        previous_path = None
        victim_cycles = set()
        for item in plan["victims"]:
            if (not isinstance(item, dict)
                    or set(item) != {
                        "path", "sha256", "bytes", "reason", "cycle_ids"}
                    or item.get("reason") not in {"expired", "orphan"}
                    or not isinstance(item.get("path"), str)
                    or not isinstance(item.get("sha256"), str)
                    or _HASH_RE.fullmatch(item["sha256"]) is None
                    or not isinstance(item.get("bytes"), int)
                    or isinstance(item["bytes"], bool) or item["bytes"] < 0
                    or not isinstance(item.get("cycle_ids"), list)):
                raise StorageOperationError("GC victim 身份非法")
            expected = f"state/storage/backups/sha256/{item['sha256']}.sqlite"
            if item["path"] != expected:
                raise StorageOperationError("GC victim 路径越界")
            numbers = [_cycle_number(value) for value in item["cycle_ids"]]
            if numbers != sorted(set(numbers)):
                raise StorageOperationError("GC victim cycle_ids 非法")
            if (set(numbers) & protected_cycles
                    or set(numbers) & victim_cycles
                    or (numbers and numbers[-1] >= min(protected_cycles))):
                raise StorageOperationError("GC victim 覆盖受保护/重复 cycle")
            victim_cycles.update(numbers)
            if (item["reason"] == "expired") != bool(numbers):
                raise StorageOperationError("GC victim reason/cycle_ids 不一致")
            if previous_path is not None and item["path"] <= previous_path:
                raise StorageOperationError("GC victims 未严格排序")
            previous_path = item["path"]
            total += item["bytes"]
        if (not isinstance(plan.get("bytes_reclaimable"), int)
                or isinstance(plan["bytes_reclaimable"], bool)
                or plan["bytes_reclaimable"] != total):
            raise StorageOperationError("GC plan bytes 不对账")

    def _load_retirements(self) -> tuple[
            List[Dict[str, Any]], Dict[tuple[int, str], Dict[str, Any]]]:
        if not os.path.lexists(self.applied_plans):
            return [], {}
        _regular_directory(self.applied_plans, label="applied GC plans")
        receipts = []
        coverage: Dict[tuple[int, str], Dict[str, Any]] = {}
        for path in sorted(self.applied_plans.iterdir(), key=lambda item: item.name):
            match = _RECEIPT_NAME.fullmatch(path.name)
            if match is None:
                if _APPLIED_TEMP_NAME.fullmatch(path.name) is not None:
                    info = path.lstat()
                    if stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                        # fsync(temp) 后、rename 前的 kill 痕迹尚未成为 authority；
                        # verify/plan 只读忽略，由下一次 apply 在 owner fence 下回收。
                        continue
                raise StorageOperationError(
                    f"applied GC 目录含非法条目: {path.name}")
            if path.is_symlink() or not path.is_file():
                raise StorageOperationError(f"applied GC 目录含非法条目: {path.name}")
            raw = sg._read(path)
            if sg._hash_bytes(raw) != match.group(1):
                raise StorageOperationError("applied GC plan hash 漂移")
            plan = sg._parse_json(raw, path)
            self._validate_plan_shape(plan)
            protected_hashes = {item["backup_sha256"] for item in plan["protected"]}
            for item in plan["victims"]:
                if item["sha256"] in protected_hashes:
                    raise StorageOperationError("applied GC plan 包含受保护 backup")
                for cycle in item["cycle_ids"]:
                    key = (_cycle_number(cycle), item["sha256"])
                    if key in coverage and coverage[key] != item:
                        raise StorageOperationError("retirement coverage 相互冲突")
                    coverage[key] = item
            receipts.append(plan)
        return receipts, coverage

    def _discard_applied_temps(self) -> None:
        if not os.path.lexists(self.applied_plans):
            return
        _regular_directory(self.applied_plans, label="applied GC plans")
        changed = False
        for path in self.applied_plans.iterdir():
            if _APPLIED_TEMP_NAME.fullmatch(path.name) is None:
                continue
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise StorageOperationError(f"applied GC 临时对象类型非法: {path.name}")
            self.owner_guard()
            path.unlink()
            changed = True
        if changed:
            sg._sync_dir(self.applied_plans)

    def _ensure_durable_directory(self, path: Path) -> None:
        self.owner_guard()
        sg._ensure_dir(path)
        # 即使目录是上次 mkdir 后、parent fsync 前的 kill 残留，也重新闭合目录项。
        sg._sync_dir(path.parent)

    def _confirm_durable_authority(
            self, path: Path, *, expected_hash: str, expected_bytes: int) -> None:
        """在任何 unlink 前重新证明 exact authority file 与整条目录链耐久。"""
        fd = os.open(
            path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0))
        try:
            fd_info = os.fstat(fd)
            path_info = path.lstat()
            if (not stat.S_ISREG(fd_info.st_mode)
                    or (fd_info.st_dev, fd_info.st_ino)
                    != (path_info.st_dev, path_info.st_ino)
                    or sg._hash_fd(fd, path) != (expected_hash, expected_bytes)):
                raise StorageOperationError("applied GC authority 身份漂移")
            os.fsync(fd)
        finally:
            os.close(fd)
        self.owner_guard()
        for directory in (
                self.applied_plans, self.applied_plans.parent,
                self.gc_root, self.publisher.storage_root):
            self.owner_guard()
            sg._sync_dir(directory)

    def _chain(self, *, retain: int) -> Dict[str, Any]:
        retain = _retention(retain)
        self.owner_guard()
        refs = self.publisher._refs()
        if not refs:
            raise StorageOperationError("snapshot chain 为空")
        pending = self.publisher._pending_ids()
        if pending:
            raise StorageOperationError(
                "存在 pending snapshot；须先用主编排器 reconcile，离线工具不代发 pointer")
        genesis = self.publisher._genesis(
            [], refs=refs, pending_ids=set(), startup=False)
        ordered = sorted(refs)
        start = genesis["coverage_start_cycle"]
        if ordered != list(range(start, ordered[-1] + 1)):
            raise StorageOperationError("snapshot pointer chain 存在缺口")
        protected_cycles = set(ordered[-retain:])
        authorities, retirement_coverage = self._load_retirements()

        manifests: List[Dict[str, Any]] = []
        previous_hash = None
        available: List[str] = []
        expired: List[str] = []
        expired_present: List[str] = []
        deep_verified: List[str] = []
        for position, cycle_id in enumerate(ordered):
            # 先由 b.1 validator 核 pointer/manifest 路径形状，避免在验路径前读越界目标。
            manifest = self.publisher._validate_pointer(
                cycle_id, verify_backup=False, verify_git=False)
            if set(manifest) != {
                    "schema", "cycle_id", "cycle_status",
                    "bootstrap_before_cycle", "adoption_baseline",
                    "previous_manifest_sha256", "backup", "views",
                    "asset_inventory_sha256", "assets", "manifest_sha256"}:
                raise StorageOperationError(f"cycle c{cycle_id} manifest 字段闭包漂移")
            if manifest.get("cycle_status") not in sg.TERMINAL_CYCLE_STATES:
                raise StorageOperationError(f"cycle c{cycle_id} manifest 非终态")
            if manifest.get("previous_manifest_sha256") != previous_hash:
                raise StorageOperationError(f"cycle c{cycle_id} manifest 父链漂移")
            if position == 0:
                if (manifest.get("adoption_baseline")
                        is not genesis["adoption_baseline"]
                        or manifest.get("bootstrap_before_cycle")
                        != genesis["bootstrap_before_cycle"]):
                    raise StorageOperationError("snapshot 首 manifest 与 genesis 不一致")
            elif (manifest.get("adoption_baseline") is not False
                  or manifest.get("bootstrap_before_cycle") is not None):
                raise StorageOperationError("adoption 只允许出现在首 manifest")
            backup = manifest["backup"]
            backup_path = self.publisher.backups / f"{backup['sha256']}.sqlite"
            exists = os.path.lexists(backup_path)
            key = (cycle_id, backup["sha256"])
            retired = retirement_coverage.get(key)
            if retired is not None and (
                    retired["path"] != backup["path"]
                    or retired["bytes"] != backup["bytes"]):
                raise StorageOperationError(
                    f"cycle c{cycle_id} applied-plan authority 与 manifest 漂移")
            if cycle_id in protected_cycles and retired is not None:
                raise StorageOperationError(
                    f"受保护 generation c{cycle_id} 被 applied-plan authority 覆盖")
            if cycle_id in protected_cycles and not exists:
                raise StorageOperationError(
                    f"受保护 generation c{cycle_id} backup 缺失")
            if exists:
                info = backup_path.lstat()
                if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                        or info.st_size != backup["bytes"]):
                    raise StorageOperationError(
                        f"cycle c{cycle_id} backup 类型/bytes 漂移")
                if cycle_id in protected_cycles:
                    self.publisher._verify_backup_object(
                        backup_path,
                        expected_hash=backup["sha256"], expected_bytes=backup["bytes"],
                        cycle_id=cycle_id, cycle_status=manifest["cycle_status"],
                        allow_later_cycles=manifest["adoption_baseline"] is True)
                    deep_verified.append(f"c{cycle_id}")
            if retired is not None:
                expired.append(f"c{cycle_id}")
                if exists:
                    expired_present.append(f"c{cycle_id}")
            elif exists:
                available.append(f"c{cycle_id}")
            else:
                raise StorageOperationError(
                    f"cycle c{cycle_id} backup 缺失且无 applied-plan authority")
            subject = f"cycle c{cycle_id} storage snapshot"
            body = (
                f"Cycle: c{cycle_id}\nDB-Backup-SHA256: {backup['sha256']}\n"
                f"Asset-Inventory-SHA256: {manifest['asset_inventory_sha256']}")
            parent_commit = None if position == 0 else manifests[-1]["views"]["commit"]
            self.publisher._validate_commit(
                manifest["views"]["commit"], parent_commit, subject, body)
            previous_hash = manifest["manifest_sha256"]
            manifests.append(manifest)

        manifest_by_cycle = dict(zip(ordered, manifests))
        for authority in authorities:
            high_water = _cycle_number(authority["high_water_cycle"])
            if (high_water not in manifest_by_cycle
                    or manifest_by_cycle[high_water]["manifest_sha256"]
                    != authority["high_water_manifest_sha256"]):
                raise StorageOperationError(
                    "applied GC plan high-water 与 immutable chain 不一致")
            historical = [value for value in ordered if value <= high_water]
            expected_protected = historical[-authority["retain_generations"]:]
            actual_protected = [
                _cycle_number(item["cycle_id"])
                for item in authority["protected"]]
            if actual_protected != expected_protected:
                raise StorageOperationError(
                    "applied GC plan protected window 与 immutable chain 不一致")
            for item in authority["victims"]:
                for value in item["cycle_ids"]:
                    cycle_id = _cycle_number(value)
                    manifest = manifest_by_cycle.get(cycle_id)
                    if (manifest is None
                            or manifest["backup"]["sha256"] != item["sha256"]
                            or manifest["backup"]["path"] != item["path"]
                            or manifest["backup"]["bytes"] != item["bytes"]):
                        raise StorageOperationError(
                            "applied GC plan victim 与 immutable chain 不一致")

        self.publisher._require_git_repo()
        latest_commit = manifests[-1]["views"]["commit"]
        git_rows = self.publisher._git(
            "log", "--first-parent", "--reverse", "--format=%H %T",
            latest_commit).stdout.splitlines()
        actual_git = [tuple(row.split()) for row in git_rows if row.strip()]
        expected_git = [
            (manifest["views"]["commit"], manifest["views"]["tree"])
            for manifest in manifests]
        if actual_git != expected_git:
            raise StorageOperationError("views Git first-parent/tree 链与 manifest 不一致")
        if (self.publisher._head() != latest_commit
                or self.publisher._git(
                    "status", "--porcelain", "--untracked-files=all").stdout):
            raise StorageOperationError("views Git HEAD/worktree 不在最新完成 snapshot")
        self.owner_guard()
        return {
            "ordered": ordered,
            "manifests": manifests,
            "genesis": genesis,
            "protected_cycles": sorted(protected_cycles),
            "available": available,
            "expired": expired,
            "expired_present": expired_present,
            "deep_verified": deep_verified,
        }

    def verify(self, *, retain: int = MIN_RETAINED_GENERATIONS) -> Dict[str, Any]:
        chain = self._chain(retain=retain)
        ordered = chain["ordered"]
        latest = chain["manifests"][-1]
        return {
            "schema": VERIFY_SCHEMA,
            "scope": "snapshot_chain_and_retained_sqlite",
            "coverage_start_cycle": f"c{ordered[0]}",
            "high_water_cycle": f"c{ordered[-1]}",
            "high_water_manifest_sha256": latest["manifest_sha256"],
            "retain_generations": _retention(retain),
            "protected_cycles": [f"c{value}" for value in chain["protected_cycles"]],
            "available_cycles": chain["available"],
            "expired_cycles": chain["expired"],
            "expired_but_present_cycles": chain["expired_present"],
            "deep_verified_cycles": chain["deep_verified"],
            "views_commit": latest["views"]["commit"],
        }

    def restore(self, *, target: Path | str, cycle: Optional[str | int] = None,
                retain: int = MIN_RETAINED_GENERATIONS) -> Dict[str, Any]:
        chain = self._chain(retain=retain)
        selected = chain["ordered"][-1] if cycle is None else _cycle_number(cycle)
        if selected not in chain["ordered"]:
            raise StorageOperationError(f"snapshot cycle c{selected} 不存在")
        if f"c{selected}" in chain["expired"]:
            raise GenerationNotRetained(
                f"generation_not_retained: c{selected} backup 已由 applied plan 退役")
        manifest = chain["manifests"][chain["ordered"].index(selected)]
        source = self.work_root / manifest["backup"]["path"]
        if not os.path.lexists(source):
            raise GenerationNotRetained(
                f"generation_not_retained: c{selected} backup 已合法退役")

        destination = Path(os.path.abspath(os.fspath(target)))
        if destination == self.work_root or self.work_root in destination.parents:
            raise StorageOperationError("restore target 不得位于源 work_root 内")
        if os.path.lexists(destination):
            raise StorageOperationError("restore target 必须不存在")
        try:
            parent = destination.parent.resolve(strict=True)
        except OSError as error:
            raise StorageOperationError("restore target parent 不可解析") from error
        _regular_directory(parent, label="restore target parent")
        destination = parent / destination.name
        source_root = self.work_root.resolve(strict=True)
        if destination == source_root or source_root in destination.parents:
            raise StorageOperationError("restore target 解析后不得位于源 work_root 内")
        temporary = parent / f".{destination.name}.restore-{uuid.uuid4().hex}"
        temporary.mkdir(mode=0o700)
        temporary_identity = temporary.lstat()
        try:
            self.owner_guard()
            target_db = temporary / "research.sqlite"
            _copy_regular(
                source, target_db, mode=0o600,
                expected_hash=manifest["backup"]["sha256"],
                expected_bytes=manifest["backup"]["bytes"])
            self.publisher._verify_backup_object(
                target_db,
                expected_hash=manifest["backup"]["sha256"],
                expected_bytes=manifest["backup"]["bytes"],
                cycle_id=selected,
                cycle_status=manifest["cycle_status"],
                allow_later_cycles=manifest["adoption_baseline"] is True)
            receipt = {
                "schema": RESTORE_SCHEMA,
                "scope": "sqlite_truth_only",
                "continuation_mode": "legacy_adoption_on_first_start",
                "publication_contract": "atomic_noreplace_or_lease_fenced_ready",
                "source_work_root": str(self.work_root),
                "source_cycle": f"c{selected}",
                "source_manifest_sha256": manifest["manifest_sha256"],
                "backup": manifest["backup"],
            }
            sg._atomic_write(
                temporary / "restore.json", sg._canonical(receipt), mode=0o400)
            sg._sync_dir(temporary)
            self.owner_guard()
            _rename_noreplace(temporary, destination)
            sg._sync_dir(parent)
            self.publisher._verify_backup_object(
                destination / "research.sqlite",
                expected_hash=manifest["backup"]["sha256"],
                expected_bytes=manifest["backup"]["bytes"],
                cycle_id=selected,
                cycle_status=manifest["cycle_status"],
                allow_later_cycles=manifest["adoption_baseline"] is True)
            return receipt
        finally:
            if os.path.lexists(temporary):
                current = temporary.lstat()
                if ((current.st_dev, current.st_ino)
                        != (temporary_identity.st_dev, temporary_identity.st_ino)
                        or not stat.S_ISDIR(current.st_mode)):
                    raise StorageOperationError("restore 临时目录身份漂移；拒绝递归清理")
                # 只清理由本操作创建的两个已知文件；未知/替代树一律保留并报错，
                # 不使用 pathname recursive delete。
                (temporary / "restore.json").unlink(missing_ok=True)
                (temporary / "research.sqlite").unlink(missing_ok=True)
                temporary.rmdir()

    def _plan_value(self, *, retain: int) -> Dict[str, Any]:
        chain = self._chain(retain=retain)
        referenced: Dict[str, List[int]] = {}
        sizes: Dict[str, int] = {}
        for cycle_id, manifest in zip(chain["ordered"], chain["manifests"]):
            backup = manifest["backup"]
            referenced.setdefault(backup["sha256"], []).append(cycle_id)
            sizes[backup["sha256"]] = backup["bytes"]
        protected = [
            {
                "cycle_id": f"c{cycle_id}",
                "backup_sha256": chain["manifests"][
                    chain["ordered"].index(cycle_id)]["backup"]["sha256"],
            }
            for cycle_id in chain["protected_cycles"]]
        protected_hashes = {item["backup_sha256"] for item in protected}
        victims = []
        for path in sorted(self.publisher.backups.iterdir(), key=lambda item: item.name):
            match = _BACKUP_NAME.fullmatch(path.name)
            if match is None or path.is_symlink() or not path.is_file():
                raise StorageOperationError(f"backup CAS 含非法条目: {path.name}")
            digest = match.group(1)
            got_hash, got_bytes = sg._hash_file(path)
            if got_hash != digest:
                raise StorageOperationError(f"backup CAS hash 漂移: {path.name}")
            if digest in sizes and sizes[digest] != got_bytes:
                raise StorageOperationError(f"backup CAS bytes 与 manifest 不一致: {path.name}")
            if digest in protected_hashes:
                continue
            cycle_ids = sorted(referenced.get(digest, []))
            victims.append({
                "path": path.relative_to(self.work_root).as_posix(),
                "sha256": digest,
                "bytes": got_bytes,
                "reason": "expired" if cycle_ids else "orphan",
                "cycle_ids": [f"c{value}" for value in cycle_ids],
            })
        victims.sort(key=lambda item: item["path"])
        latest = chain["manifests"][-1]
        return {
            "schema": GC_PLAN_SCHEMA,
            "high_water_cycle": f"c{chain['ordered'][-1]}",
            "high_water_manifest_sha256": latest["manifest_sha256"],
            "retain_generations": _retention(retain),
            "protected": protected,
            "victims": victims,
            "bytes_reclaimable": sum(item["bytes"] for item in victims),
        }

    def plan_gc(self, *, retain: int = MIN_RETAINED_GENERATIONS) -> Dict[str, Any]:
        plan = self._plan_value(retain=retain)
        self._validate_plan_shape(plan)
        raw = sg._canonical(plan)
        digest = sg._hash_bytes(raw)
        # 真 dry-run：不在 source work-root 内写任何 plan/receipt。
        return {"plan_sha256": digest, "plan": plan}

    def _applied_plan(self, plan_hash: str) -> Optional[Dict[str, Any]]:
        receipts, _coverage = self._load_retirements()
        matched = [item for item in receipts if sg._hash_bytes(sg._canonical(item)) == plan_hash]
        return matched[0] if matched else None

    def apply_gc(self, *, plan: Mapping[str, Any],
                 expected_sha256: str) -> Dict[str, Any]:
        if (not isinstance(expected_sha256, str)
                or _HASH_RE.fullmatch(expected_sha256) is None):
            raise StorageOperationError("GC expected plan hash 非法")
        if not isinstance(plan, Mapping):
            raise StorageOperationError("GC plan 须为 object")
        plan = dict(plan)
        self._validate_plan_shape(plan)
        raw_plan = sg._canonical(plan)
        if sg._hash_bytes(raw_plan) != expected_sha256:
            raise StorageOperationError("GC plan 与显式 expected hash 不一致")
        self._discard_applied_temps()
        existing = self._applied_plan(expected_sha256)
        if existing is None:
            current = self._plan_value(retain=plan["retain_generations"])
            if current != plan:
                raise StorageOperationError("GC plan 已 stale；high-water/roots/victims 发生变化")
            self._ensure_durable_directory(self.gc_root)
            self._ensure_durable_directory(self.gc_root / "applied")
            self._ensure_durable_directory(self.applied_plans)
            # 应用权威先于 unlink 落盘；kill 只会留「已退役但物理文件尚在」。
            self.owner_guard()
            sg._publish_once(
                self.applied_plans / f"{expected_sha256}.json", raw_plan)
        else:
            report = self.verify(retain=plan["retain_generations"])
            if (report["high_water_cycle"] != plan["high_water_cycle"]
                    or report["high_water_manifest_sha256"]
                    != plan["high_water_manifest_sha256"]):
                raise StorageOperationError("GC resume 时 high-water 已漂移")

        # `_atomic_write` 可能上次 kill 在 final rename 与 dir fsync 之间；可见不等于
        # 已耐久。首次和 resume 都在删除前重新 fsync exact file + 全部新目录父链。
        self._confirm_durable_authority(
            self.applied_plans / f"{expected_sha256}.json",
            expected_hash=expected_sha256, expected_bytes=len(raw_plan))

        protected_hashes = {item["backup_sha256"] for item in plan["protected"]}
        deleted = []
        for item in plan["victims"]:
            if item["sha256"] in protected_hashes:
                raise StorageOperationError("GC apply 企图删除受保护 backup")
            path = self.work_root / item["path"]
            if not os.path.lexists(path):
                continue
            got_hash, got_bytes = sg._hash_file(path)
            if (got_hash, got_bytes) != (item["sha256"], item["bytes"]):
                raise StorageOperationError(f"GC victim 内容漂移: {item['path']}")
            self.owner_guard()
            path.unlink()
            deleted.append(item["path"])
        if deleted:
            sg._sync_dir(self.publisher.backups)
        final = self.verify(retain=plan["retain_generations"])
        return {
            "schema": GC_APPLY_SCHEMA,
            "plan_sha256": expected_sha256,
            "deleted": deleted,
            "deleted_bytes": sum(
                item["bytes"] for item in plan["victims"]
                if item["path"] in deleted),
            "high_water_cycle": final["high_water_cycle"],
            "protected_cycles": final["protected_cycles"],
        }


def _run_with_lease(work_root: Path, operation) -> Dict[str, Any]:
    # InstanceLease.acquire 会为正常启动创建 work-root；离线工具不得把输入错误
    # 变成新空目录，故在取 lease 前先只读固定存在性/类型。
    _regular_directory(work_root, label="offline source work_root")
    lease = InstanceLease.acquire(work_root)
    primary: Optional[BaseException] = None
    try:
        archive = SnapshotArchive(
            work_root=work_root, lease=lease)
        return operation(archive)
    except BaseException as error:
        primary = error
        raise
    finally:
        close_error = lease.close()
        if close_error is not None:
            if primary is not None:
                add_note = getattr(primary, "add_note", None)
                if callable(add_note):
                    add_note(f"offline storage lease close 失败: {close_error}")
            else:
                raise close_error


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="meta-research offline snapshot verify/restore/GC")
    parser.add_argument("--work-root", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--retain", type=int, default=MIN_RETAINED_GENERATIONS)
    restore = sub.add_parser("restore")
    restore.add_argument("--target", required=True)
    restore.add_argument("--cycle")
    restore.add_argument("--retain", type=int, default=MIN_RETAINED_GENERATIONS)
    plan = sub.add_parser("gc-plan")
    plan.add_argument("--retain", type=int, default=MIN_RETAINED_GENERATIONS)
    apply = sub.add_parser("gc-apply")
    apply.add_argument("--plan-file", required=True)
    apply.add_argument("--expect-sha256", required=True)
    sub.add_parser("mirror-logs")
    sub.add_parser("verify-log-mirrors")
    args = parser.parse_args(argv)
    work_root = Path(os.path.abspath(args.work_root))

    if args.command == "verify":
        result = _run_with_lease(
            work_root, lambda archive: archive.verify(retain=args.retain))
    elif args.command == "restore":
        result = _run_with_lease(
            work_root, lambda archive: archive.restore(
                target=args.target, cycle=args.cycle, retain=args.retain))
    elif args.command == "gc-plan":
        result = _run_with_lease(
            work_root, lambda archive: archive.plan_gc(retain=args.retain))
    elif args.command == "gc-apply":
        plan_path = Path(args.plan_file)
        wrapper = sg._parse_json(sg._read(plan_path), plan_path)
        if (set(wrapper) != {"plan", "plan_sha256"}
                or wrapper.get("plan_sha256") != args.expect_sha256
                or not isinstance(wrapper.get("plan"), dict)):
            raise StorageOperationError("gc-plan 文件 wrapper/hash 非法")
        result = _run_with_lease(
            work_root, lambda archive: archive.apply_gc(
                plan=wrapper["plan"], expected_sha256=args.expect_sha256))
    else:
        from .storage_assets import RegisteredAssetArchive
        operation = (
            (lambda archive: RegisteredAssetArchive(archive).mirror_logs())
            if args.command == "mirror-logs" else
            (lambda archive: RegisteredAssetArchive(archive).verify_log_mirrors()))
        result = _run_with_lease(work_root, operation)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
