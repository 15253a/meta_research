"""notify —— 通知矩阵 outbox + 文件请求全流水 + 全局等待前置检查（§4.6.6/§4.6.8/§4.4.1；M5 CP6.3）。

**outbox = 实现层文件队列，不建表**（核心 DDL 36 表冻结；§4.6.2 heartbeat/outbox 明示非核心 DDL）：
`outbox.jsonl` 追加事件（一行一 JSON），`delivery_receipts.jsonl` 记录远端 ACK 或
安全抑制终态，
`outbound_delivery_state.json` 原子保存跨重启退避。emit 按 event_key 去重并拒绝同键异文；远端成功但
本地 receipt 前崩溃会重发同一 key（at-least-once），严格 webhook 接收端据 key 耐久去重。

**事件从 DB 状态扫描派生**（Directive/Interaction/Research/FileRequest Notifier），不在 console/interaction 内联发
——写路径保持单一职责，通知层随时可重扫补发（崩溃后 outbox 丢了也能从 DB 重建全部事件）。
event_key 确定性（directive:{id}:{state} / filereq:{id}:{event}）⇒ 重扫幂等。

**directive 逐态外显（§4.6.6 矩阵，7 态）**：received（源消息已入）/ classified（意图+kind）/
pending_confirmation（硬指令待回显确认，**展示润色稿**）/ pending_effect（已就绪待时机，示预计消费点）/
applied（consumed_cycle+效果摘要）/ rejected（附理由）/ superseded。
**文件请求 3 事件**：request_pending / reminder（每 remind_interval_h 一档；时间由调用方注入 now_ts
——本模块不调 wall-clock，保确定性可测）/ resolved（含 cancelled，附 resolution 摘要）。

**文件请求全流水（§4.6.8）**：create_checked = schema 校验（resource_request.schema.json：items 必带
attempted_paths/failure_reason——"能自己获取的不得请求"的自证）+ 创建拒绝三判据（enabled=false /
len(items)>max_items_per_request / 同 goal (pending+resolved)≥max_requests_per_goal）→ 落单。
resolve = uploads/<req_id>/<item_no>/ 逐文件 sha256 入账 → 复制并入 input/user_provided/<req_id>/ →
resolution_json + resolved_message_id **一次性迁终态**（DDL trg_ireq_identity_frozen 只许这一跳）。
cancel 同 provenance。用户文件 = 输入资产非证据（不进 evidence 链）。

**全局等待（§4.4.1 v1）**：make_advancer_precheck 装到 SqliteAdvancer.precheck——每格 advance 前：
①按时机消费到期 directive（immediate 恒到期；stage_boundary 每格即边界；reasoning_start 仅当下一格
将进 reasoning——结构判定见 _due_timings）；②查阻断：已消费未解除的 pause（console.has_blocking_pause）
或存在 pending 文件请求 → 返回拒因，Advancer 停止推进（**不发新研究 Runner 调用、不推阶段**；
query/通知照常——它们不走 Advancer）。
"""
from __future__ import annotations

import codecs
import copy
import fcntl
import hashlib
import json
import logging
import math
import os
import re
import secrets
import shutil
import sqlite3
import stat
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

from .console import (DIRECTIVE_ACTION_SESSION_REF, FILE_REQUEST_ACTION_SESSION_REF,
                      Console, DirectiveApplicationError)
from .resource_limits import (MAX_ASSETS_PER_GOAL, MAX_CANCEL_REASON_CHARS,
                              MAX_FILE_REQUESTS_PER_GOAL, MAX_REQUEST_ITEMS)
from .writedaemon import WriteDaemon

logger = logging.getLogger(__name__)


def _state_json_object(pairs):  # noqa: ANN001
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"outbox JSON key 重复: {key}")
        value[key] = item
    return value


def _load_state_json(raw: str) -> Any:
    return json.loads(
        raw, object_pairs_hook=_state_json_object,
        parse_constant=lambda token: (_ for _ in ()).throw(
            ValueError(f"outbox JSON 非有限数字: {token}")))

# ------------------------------------------------------------------- outbox --


class Outbox:
    """Durable event queue plus per-channel delivery receipts/retry state.

    ``outbox.jsonl`` remains a DB-derived, replayable event stream.  Transport
    facts live separately: a newline-committed receipt proves a remote ACK,
    while an atomically replaced state file holds the latest retry schedule.
    The in-process locks permit the research boundary and resident interaction
    sideband to scan concurrently; CP11.3 still owns cross-process exclusion.
    """

    _MAX_EVENT_BYTES = 64 * 1024
    _MAX_LOG_BYTES = 64 * 1024 * 1024
    _MAX_ERROR_CHARS = 1000
    _EVENT_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._/-]{0,255}$")
    _CHANNEL_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

    def __init__(self, out_dir: str):
        self.dir = Path(out_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        directory_info = self.dir.lstat()
        if not stat.S_ISDIR(directory_info.st_mode) or directory_info.st_uid != os.geteuid():
            raise OSError("outbox 目录须为当前进程拥有的真实目录")
        os.chmod(self.dir, 0o700)
        self.queue_path = self.dir / "outbox.jsonl"
        self.delivered_path = self.dir / "delivered.log"
        self.receipts_path = self.dir / "delivery_receipts.jsonl"
        self.retry_path = self.dir / "outbound_delivery_state.json"
        self.producer_path = self.dir / "outbound_producer_id"
        self.producer_id = self._load_or_create_producer_id()
        # event_key -> canonical JSON.  Keeping the full canonical value makes
        # idempotency collisions O(1) without re-reading the growing queue for
        # every notifier rescan (the old set-only cache regressed to O(n²)).
        self._seen: Optional[Dict[str, str]] = None
        self._event_cache: Optional[List[Dict[str, Any]]] = None
        self._queue_fingerprint: Optional[Tuple[int, int, int, int, int]] = None
        self._receipt_seen: Optional[set] = None
        self._receipt_fingerprint: Optional[Tuple[int, int, int, int, int]] = None
        self._retry_cache: Optional[Dict[str, Any]] = None
        self._retry_fingerprint: Optional[Tuple[int, int, int, int, int]] = None
        self._lock = threading.RLock()
        self._legacy_delivery_lock = threading.Lock()

    @staticmethod
    def _fingerprint(info: os.stat_result) -> Tuple[int, int, int, int, int]:
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    def _read_regular_snapshot(
            self, path: Path, *, max_bytes: Optional[int] = None,
    ) -> Tuple[bytes, Optional[Tuple[int, int, int, int, int]]]:
        flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_NONBLOCK", 0))
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            return b"", None
        try:
            info = os.fstat(fd)
            limit = self._MAX_LOG_BYTES if max_bytes is None else max_bytes
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.geteuid() or info.st_size > limit):
                raise OSError(f"outbox 状态文件身份/大小非法: {path.name}")
            chunks: List[bytes] = []
            remaining = info.st_size
            while remaining:
                chunk = os.read(fd, min(remaining, 1024 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            after = os.fstat(fd)
            if (len(data) != info.st_size
                    or self._fingerprint(after) != self._fingerprint(info)):
                raise OSError(f"outbox 状态文件读取期间变化: {path.name}")
            return data, self._fingerprint(after)
        finally:
            os.close(fd)

    def _read_regular_bytes(self, path: Path, *, max_bytes: Optional[int] = None) -> bytes:
        return self._read_regular_snapshot(path, max_bytes=max_bytes)[0]

    def _path_fingerprint(self, path: Path) -> Optional[Tuple[int, int, int, int, int]]:
        flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_NONBLOCK", 0))
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            return None
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.geteuid() or info.st_size > self._MAX_LOG_BYTES):
                raise OSError(f"outbox 状态文件身份/大小非法: {path.name}")
            return self._fingerprint(info)
        finally:
            os.close(fd)

    def _load_or_create_producer_id(self) -> str:
        """Return one durable namespace shared by every restart of this work-root.

        Initialization has its own cross-process lock because it precedes the
        CP11.3 run-owner lock.  Without it, one constructor could misclassify
        another constructor's O_EXCL-but-not-yet-written file as a crash orphan
        and both would return different producer IDs.
        """
        lock_path = self.dir / ".outbound_producer_id.lock"
        flags = (os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
                 | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        fd = os.open(lock_path, flags, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.geteuid()):
                raise OSError("outbound producer init lock 身份非法")
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            return self._load_or_create_producer_id_locked()
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _load_or_create_producer_id_locked(self) -> str:
        pattern = re.compile(r"^mr-[0-9a-f]{32}$")

        def parse_existing(raw: bytes) -> str:
            try:
                value = raw.decode("ascii")
            except UnicodeDecodeError as error:
                raise OSError("outbound producer id 非 ASCII") from error
            if not value.endswith("\n") or pattern.fullmatch(value[:-1]) is None:
                raise OSError("outbound producer id 损坏")
            return value[:-1]

        # queue/delivered existed before real transport wiring and can migrate
        # safely: their local keys remain unchanged.  A receipt/retry, however,
        # proves that a producer namespace was already used remotely; losing it
        # must fail loud rather than minting duplicate user-visible effects.
        namespace_authority_paths = (self.receipts_path, self.retry_path)
        any_transport_paths = (
            self.queue_path, self.delivered_path, self.receipts_path, self.retry_path)

        raw, identity = self._read_regular_snapshot(self.producer_path, max_bytes=128)
        if identity is not None:
            try:
                return parse_existing(raw)
            except OSError as error:
                if any(self._path_fingerprint(path) is not None
                       for path in any_transport_paths):
                    raise OSError(
                        "outbound producer id 损坏且已有投递状态；请从同一 work-root 备份恢复该文件") from error
                # First initialization can be killed between O_EXCL and fsync.
                # No transport state can yet reference that value, so this one
                # malformed orphan is safe to remove and recreate.
                if self._path_fingerprint(self.producer_path) != identity:
                    raise OSError("outbound producer id 修复时发生身份漂移") from error
                self.producer_path.unlink()
                self._fsync_dir()
                return self._load_or_create_producer_id_locked()
        if any(self._path_fingerprint(path) is not None for path in namespace_authority_paths):
            raise OSError(
                "已有 outbox/receipt/retry 但缺 outbound_producer_id；拒绝生成新远端幂等命名空间")
        candidate = "mr-" + secrets.token_hex(16)
        temp = self.dir / (
            f".outbound_producer_id.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp")
        flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
                 | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        fd = None
        try:
            fd = os.open(temp, flags, 0o600)
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or info.st_uid != os.geteuid()):
                    raise OSError("outbound producer id 创建目标身份非法")
                os.fchmod(fd, 0o600)
                encoded = (candidate + "\n").encode("ascii")
                view = memoryview(encoded)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise OSError("outbound producer id 短写")
                    view = view[written:]
                os.fsync(fd)
            finally:
                try:
                    os.close(fd)
                finally:
                    fd = None
            if self._path_fingerprint(self.producer_path) is not None:
                raw, _identity = self._read_regular_snapshot(self.producer_path, max_bytes=128)
                return parse_existing(raw)
            os.replace(temp, self.producer_path)
            self._fsync_dir()
            return candidate
        finally:
            try:
                if fd is not None:
                    os.close(fd)
            finally:
                try:
                    temp.unlink()
                except OSError:
                    pass

    def _committed_jsonl_snapshot(
            self, path: Path,
    ) -> Tuple[List[Dict[str, Any]], Optional[Tuple[int, int, int, int, int]]]:
        data, identity = self._read_regular_snapshot(path)
        if not data:
            return [], identity
        if not data.endswith(b"\n"):
            data = data[:data.rfind(b"\n") + 1]
        return ([_load_state_json(line) for line in data.decode("utf-8").split("\n") if line.strip()],
                identity)

    def _committed_jsonl(self, path: Path) -> List[Dict[str, Any]]:
        return self._committed_jsonl_snapshot(path)[0]

    def _events_snapshot(
            self,
    ) -> Tuple[List[Dict[str, Any]], Optional[Tuple[int, int, int, int, int]]]:
        events, identity = self._committed_jsonl_snapshot(self.queue_path)
        seen: Dict[str, str] = {}
        for event in events:
            if (not isinstance(event, dict)
                    or set(event) not in ({"event_key", "kind", "payload"},
                                          {"event_key", "kind", "payload", "channel"})):
                raise ValueError("outbox committed event 结构损坏")
            key, kind, payload = event.get("event_key"), event.get("kind"), event.get("payload")
            channel = event.get("channel")
            if (not isinstance(key, str) or self._EVENT_KEY_RE.fullmatch(key) is None
                    or not isinstance(kind, str) or not kind or len(kind) > 128
                    or not isinstance(payload, dict)
                    or (channel is not None and (
                        not isinstance(channel, str) or self._CHANNEL_RE.fullmatch(channel) is None))):
                raise ValueError("outbox committed event 字段损坏")
            canonical = json.dumps(
                event, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            if len(canonical.encode("utf-8")) > self._MAX_EVENT_BYTES:
                raise ValueError("outbox committed event 超过字节上限")
            if key in seen:
                raise ValueError(f"outbox committed event_key 重复: {key}")
            seen[key] = canonical
        return events, identity

    def _events(self) -> List[Dict[str, Any]]:
        """读全队列。**committed 判据 = 换行终止**（外审 BLOCKER：append 崩溃可能留下"完整 JSON 但无
        尾换行"——若按可解析性判会先算入 _seen、后被 emit 截修丢弃 → 事件永久丢失。故无尾换行的末段
        一律当未入队丢弃，与 emit 的截修口径一致；重扫会补）。换行终止段解析失败 = 中段损坏
        （非崩溃可造成，磁盘/人为改写），fail loud。"""
        with self._lock:
            return self._events_snapshot()[0]

    def _verify_cached_fingerprint(
            self, path: Path, expected: Optional[Tuple[int, int, int, int, int]], *, label: str,
    ) -> None:
        if self._path_fingerprint(path) != expected:
            raise OSError(f"{label} 在进程外被替换/截断/改写；拒绝使用过期缓存")

    def _load_queue_cache(self) -> None:
        # Repair once when binding the cache.  Every subsequent use verifies
        # the exact fingerprint, and every in-process append is known to end in
        # LF, avoiding an O(n²) full-log repair scan per emit.
        self._repair_torn_jsonl_tail(self.queue_path)
        events, identity = self._events_snapshot()
        self._event_cache = events
        self._seen = {
            event["event_key"]: json.dumps(
                event, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            for event in events
        }
        self._queue_fingerprint = identity

    def _clear_queue_cache(self) -> None:
        self._seen = None
        self._event_cache = None
        self._queue_fingerprint = None

    def _queued_keys(self) -> Dict[str, str]:
        with self._lock:
            if self._seen is None:
                self._load_queue_cache()
            else:
                self._verify_cached_fingerprint(
                    self.queue_path, self._queue_fingerprint, label="outbox queue")
            return self._seen

    def _queued_events(self) -> List[Dict[str, Any]]:
        with self._lock:
            self._queued_keys()
            return self._event_cache

    def _delivered_keys(self) -> set:
        with self._lock:
            raw = self._read_regular_bytes(self.delivered_path)
            if not raw:
                return set()
            # Legacy authority also obeys newline commit.  A crash-written
            # prefix must never suppress a different, shorter event key.
            if not raw.endswith(b"\n"):
                raw = raw[:raw.rfind(b"\n") + 1]
            keys = [line.strip() for line in raw.decode("utf-8").split("\n") if line.strip()]
            if any(self._EVENT_KEY_RE.fullmatch(key) is None for key in keys):
                raise ValueError("legacy delivered.log 含非法 event_key")
            if len(keys) != len(set(keys)):
                raise ValueError("legacy delivered.log 含重复 event_key")
            return set(keys)

    def _append_line(self, path: Path, line: str) -> Tuple[int, int, int, int, int]:
        encoded = line.encode("utf-8")
        flags = (os.O_WRONLY | os.O_APPEND | getattr(os, "O_CLOEXEC", 0)
                 | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        created = False
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            try:
                fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
                created = True
            except FileExistsError:
                fd = os.open(path, flags)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.geteuid()):
                raise OSError(f"outbox append 目标身份非法: {path.name}")
            if info.st_size + len(encoded) > self._MAX_LOG_BYTES:
                raise OSError(f"outbox append 超过日志上限: {path.name}")
            os.fchmod(fd, 0o600)
            view = memoryview(encoded)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("outbox append 短写")
                view = view[written:]
            os.fsync(fd)
            identity = self._fingerprint(os.fstat(fd))
        finally:
            os.close(fd)
        if created:
            self._fsync_dir()
        return identity

    def _repair_torn_jsonl_tail(
            self, path: Path,
    ) -> Optional[Tuple[int, int, int, int, int]]:
        """Discard only the non-LF-committed tail, using the identity just read."""
        data, identity = self._read_regular_snapshot(path)
        if not data or data.endswith(b"\n"):
            return identity
        flags = (os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_NONBLOCK", 0))
        fd = os.open(path, flags)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.geteuid() or self._fingerprint(info) != identity):
                raise OSError(f"outbox torn-tail 修复目标身份漂移: {path.name}")
            os.ftruncate(fd, data.rfind(b"\n") + 1)
            os.fsync(fd)
            return self._fingerprint(os.fstat(fd))
        finally:
            os.close(fd)

    def _fsync_dir(self) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(self.dir, flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _read_retry_document(self) -> Tuple[Dict[str, Any], Optional[Tuple[int, int, int, int, int]]]:
        raw, identity_snapshot = self._read_regular_snapshot(
            self.retry_path, max_bytes=1024 * 1024)
        if identity_snapshot is None:
            return {"version": 1, "events": {}}, identity_snapshot
        if not raw:
            raise ValueError("outbound_delivery_state.json 为空（原子状态文件损坏）")
        value = _load_state_json(raw.decode("utf-8"))
        if (not isinstance(value, dict) or set(value) != {"version", "events"}
                or value.get("version") != 1 or not isinstance(value.get("events"), dict)):
            raise ValueError("outbound_delivery_state.json 结构损坏")
        for identity, entry in value["events"].items():
            if not isinstance(identity, str) or not isinstance(entry, dict):
                raise ValueError("outbound delivery retry entry 损坏")
            required = {"channel", "event_key", "attempt_count", "first_failed_at",
                        "last_attempt_at", "next_attempt_at", "last_error_kind", "last_error"}
            channel, key = entry.get("channel"), entry.get("event_key")
            times = (entry.get("first_failed_at"), entry.get("last_attempt_at"),
                     entry.get("next_attempt_at"))
            if (set(entry) != required
                    or not isinstance(channel, str) or self._CHANNEL_RE.fullmatch(channel) is None
                    or not isinstance(key, str) or self._EVENT_KEY_RE.fullmatch(key) is None
                    or identity != self._delivery_identity(channel, key)
                    or isinstance(entry.get("attempt_count"), bool)
                    or not isinstance(entry.get("attempt_count"), int)
                    or not 1 <= entry["attempt_count"] <= 2 ** 31 - 1
                    or any(isinstance(item, bool) or not isinstance(item, (int, float))
                           or not math.isfinite(float(item)) for item in times)
                    or not isinstance(entry.get("last_error_kind"), str)
                    or not isinstance(entry.get("last_error"), str)):
                raise ValueError("outbound delivery retry entry 字段损坏")
        return value, identity_snapshot

    def _retry_document(self) -> Dict[str, Any]:
        with self._lock:
            if self._retry_cache is None:
                self._retry_cache, self._retry_fingerprint = self._read_retry_document()
            else:
                self._verify_cached_fingerprint(
                    self.retry_path, self._retry_fingerprint,
                    label="outbound retry state")
            return self._retry_cache

    @staticmethod
    def _copy_retry_document(value: Dict[str, Any]) -> Dict[str, Any]:
        return {"version": 1, "events": {
            identity: dict(entry) for identity, entry in value["events"].items()
        }}

    def _clear_retry_cache(self) -> None:
        self._retry_cache = None
        self._retry_fingerprint = None

    def _store_retry_document(self, value: Dict[str, Any]) -> None:
        encoded = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                              allow_nan=False) + "\n").encode("utf-8")
        if len(encoded) > 1024 * 1024:
            raise OSError("outbound delivery retry state 超过字节上限")
        temp = self.dir / (
            f".outbound_delivery_state.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp")
        flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
                 | getattr(os, "O_NOFOLLOW", 0))
        try:
            fd = os.open(temp, flags, 0o600)
            try:
                view = memoryview(encoded)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise OSError("delivery retry state 短写")
                    view = view[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temp, self.retry_path)
            self._fsync_dir()
            self._retry_cache = value
            self._retry_fingerprint = self._path_fingerprint(self.retry_path)
        except BaseException as store_error:
            try:
                temp.unlink()
            except OSError:
                pass
            self._clear_retry_cache()
            try:
                self._retry_document()
            except BaseException as calibration_error:
                add_note = getattr(store_error, "add_note", None)
                if callable(add_note):
                    add_note(
                        "retry state 写入失败后的落盘校准也失败: "
                        f"{type(calibration_error).__name__}: {calibration_error}")
            raise

    @staticmethod
    def _delivery_identity(channel: str, event_key: str) -> str:
        return f"{channel}\x1f{event_key}"

    def _receipt_keys(self) -> set:
        with self._lock:
            if self._receipt_seen is not None:
                self._verify_cached_fingerprint(
                    self.receipts_path, self._receipt_fingerprint,
                    label="delivery receipt log")
                return self._receipt_seen
            # Same discipline as the event cache: repair only while (re)binding
            # to disk, then guard every O(1) cache lookup with a fingerprint.
            self._repair_torn_jsonl_tail(self.receipts_path)
            rows, identity_snapshot = self._committed_jsonl_snapshot(self.receipts_path)
            keys = set()
            for row in rows:
                channel, key = row.get("channel") if isinstance(row, dict) else None, \
                    row.get("event_key") if isinstance(row, dict) else None
                identity = (self._delivery_identity(channel, key)
                            if isinstance(channel, str) and isinstance(key, str) else None)
                common_valid = bool(
                    isinstance(row, dict)
                    and isinstance(channel, str)
                    and self._CHANNEL_RE.fullmatch(channel) is not None
                    and isinstance(key, str)
                    and self._EVENT_KEY_RE.fullmatch(key) is not None
                    and not isinstance(row.get("attempt_count"), bool)
                    and isinstance(row.get("attempt_count"), int)
                    and identity not in keys)
                version_one = bool(
                    common_valid and row.get("version") == 1
                    and set(row) == {
                        "version", "channel", "event_key", "accepted_at", "attempt_count",
                        "delivery_id", "ack_hash"}
                    and not isinstance(row.get("accepted_at"), bool)
                    and isinstance(row.get("accepted_at"), (int, float))
                    and math.isfinite(float(row["accepted_at"]))
                    and row["attempt_count"] >= 1
                    and (row.get("delivery_id") is None
                         or (isinstance(row.get("delivery_id"), str)
                             and len(row["delivery_id"]) <= 256))
                    and isinstance(row.get("ack_hash"), str)
                    and re.fullmatch(r"sha256:[0-9a-f]{64}", row["ack_hash"]) is not None)
                version_two = bool(
                    common_valid and row.get("version") == 2
                    and set(row) == {
                        "version", "channel", "event_key", "completed_at", "attempt_count",
                        "disposition", "reason_hash"}
                    and not isinstance(row.get("completed_at"), bool)
                    and isinstance(row.get("completed_at"), (int, float))
                    and math.isfinite(float(row["completed_at"]))
                    and row["attempt_count"] >= 0
                    and row.get("disposition") == "suppressed_unsafe_route"
                    and isinstance(row.get("reason_hash"), str)
                    and re.fullmatch(r"sha256:[0-9a-f]{64}", row["reason_hash"]) is not None)
                if not (version_one or version_two):
                    raise ValueError("delivery_receipts.jsonl committed receipt 损坏")
                keys.add(identity)
            self._receipt_seen = keys
            self._receipt_fingerprint = identity_snapshot
            return self._receipt_seen

    def _clear_receipt_cache(self) -> None:
        self._receipt_seen = None
        self._receipt_fingerprint = None

    def emit(self, event_key: str, kind: str, payload: Dict[str, Any], *,
             channel: Optional[str] = None) -> bool:
        """幂等排队：event_key 已在队列即跳过（返回 False）。追加写单行 JSON（行内自含 event_key，
        队列文件本身即持久事件序）。"""
        if not isinstance(event_key, str) or self._EVENT_KEY_RE.fullmatch(event_key) is None:
            raise ValueError("outbox event_key 非法")
        if self._EVENT_KEY_RE.fullmatch(f"{self.producer_id}:{event_key}") is None:
            raise ValueError("outbox event_key 加 producer namespace 后超过线协议上限")
        if not isinstance(kind, str) or not kind or len(kind) > 128:
            raise ValueError("outbox kind 须为 1..128 字符")
        if not isinstance(payload, dict):
            raise ValueError("outbox payload 须为 object")
        if channel is not None and (
                not isinstance(channel, str) or self._CHANNEL_RE.fullmatch(channel) is None):
            raise ValueError("outbox channel 非法")
        event = {"event_key": event_key, "kind": kind, "payload": payload}
        if channel is not None:
            event["channel"] = channel
        rendered = json.dumps(
            event, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(rendered.encode("utf-8")) > self._MAX_EVENT_BYTES:
            raise ValueError("outbox event 超过字节上限")
        # Cache exactly the JSON value committed to disk, never caller-owned
        # mutable dict/list aliases.  Otherwise one post-emit mutation could
        # change the current-process wire payload while restart replays the
        # original bytes under the same idempotency identity.
        durable_event = _load_state_json(rendered)
        with self._lock:
            queued = self._queued_keys()
            if event_key in queued:
                if queued[event_key] != rendered:
                    raise ValueError(f"outbox event_key 已绑定不同事件: {event_key}")
                return False
            # Cache binding already repaired any torn tail; its fingerprint
            # check above proves no out-of-process drift before this append.
            try:
                identity = self._append_line(self.queue_path, rendered + "\n")
            except BaseException as append_error:
                # fsync may throw after the complete LF-terminated record is
                # already durable.  Rebuild before propagating so a caller
                # retry observes that committed key instead of appending it a
                # second time.  A torn record remains uncommitted and will be
                # repaired on the retry.
                self._clear_queue_cache()
                try:
                    self._load_queue_cache()
                except BaseException as calibration_error:
                    add_note = getattr(append_error, "add_note", None)
                    if callable(add_note):
                        add_note(
                            "outbox append 失败后的落盘校准也失败: "
                            f"{type(calibration_error).__name__}: {calibration_error}")
                raise
            self._queue_fingerprint = identity
            self._seen[event_key] = rendered
            self._event_cache.append(durable_event)
            return True

    def contains_event(self, event_key: str) -> bool:
        """Return whether an immutable local event identity is already bound."""
        if not isinstance(event_key, str) or self._EVENT_KEY_RE.fullmatch(event_key) is None:
            raise ValueError("outbox event_key 非法")
        with self._lock:
            return event_key in self._queued_keys()

    def pending_for_channel(self, channel: str, *, include_default: bool) -> List[Dict[str, Any]]:
        """Return a stable FIFO snapshot not yet ACKed for ``channel``."""
        if self._CHANNEL_RE.fullmatch(channel) is None:
            raise ValueError("delivery channel 非法")
        with self._lock:
            receipts = self._receipt_keys()
            legacy = self._delivered_keys()
            pending = []
            for event in self._queued_events():
                target = event.get("channel")
                if target is None:
                    if not include_default:
                        continue
                elif target != channel:
                    continue
                key = event.get("event_key")
                if key in legacy or self._delivery_identity(channel, key) in receipts:
                    continue
                pending.append(copy.deepcopy(event))
            return pending

    def delivery_retry(self, channel: str, event_key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            value = self._retry_document()["events"].get(
                self._delivery_identity(channel, event_key))
            return dict(value) if isinstance(value, dict) else None

    def delivery_retries(self, channel: str) -> Dict[str, Dict[str, Any]]:
        """Return one O(1)-lookup snapshot for a scheduler channel tick."""
        if self._CHANNEL_RE.fullmatch(channel) is None:
            raise ValueError("delivery channel 非法")
        with self._lock:
            result = {}
            for entry in self._retry_document()["events"].values():
                if entry["channel"] == channel:
                    result[entry["event_key"]] = dict(entry)
            return result

    def reconcile_delivery_state(self) -> int:
        """Prune retry entries already dominated by a durable remote receipt."""
        with self._lock:
            receipts = self._receipt_keys()
            document = self._retry_document()
            stale = set(document["events"]) & receipts
            if not stale:
                return 0
            updated = self._copy_retry_document(document)
            for identity in stale:
                del updated["events"][identity]
            self._store_retry_document(updated)
            return len(stale)

    def record_delivery_failure(self, channel: str, event: Dict[str, Any], *, attempt_count: int,
                                next_attempt_at: float, error_kind: str, error_text: str,
                                attempted_at: float) -> None:
        key = event["event_key"]
        if (self._CHANNEL_RE.fullmatch(channel) is None
                or isinstance(attempt_count, bool) or not isinstance(attempt_count, int)
                or not 1 <= attempt_count <= 2 ** 31 - 1
                or any(isinstance(value, bool) or not isinstance(value, (int, float))
                       or not math.isfinite(float(value))
                       for value in (next_attempt_at, attempted_at))):
            raise ValueError("delivery failure receipt 参数非法")
        cleaned = "".join(ch for ch in str(error_text) if ord(ch) >= 0x20 and ord(ch) != 0x7f)
        with self._lock:
            document = self._copy_retry_document(self._retry_document())
            identity = self._delivery_identity(channel, key)
            previous = document["events"].get(identity, {})
            document["events"][identity] = {
                "channel": channel,
                "event_key": key,
                "attempt_count": int(attempt_count),
                "first_failed_at": previous.get("first_failed_at", float(attempted_at)),
                "last_attempt_at": float(attempted_at),
                "next_attempt_at": float(next_attempt_at),
                "last_error_kind": str(error_kind)[:128],
                "last_error": cleaned[:self._MAX_ERROR_CHARS],
            }
            self._store_retry_document(document)

    def record_delivery_success(self, channel: str, event: Dict[str, Any], *, ack: Dict[str, Any],
                                accepted_at: float) -> None:
        key = event["event_key"]
        if (self._CHANNEL_RE.fullmatch(channel) is None
                or isinstance(accepted_at, bool) or not isinstance(accepted_at, (int, float))
                or not math.isfinite(float(accepted_at))):
            raise ValueError("delivery success accepted_at 非法")
        if (not isinstance(ack, dict) or ack.get("accepted") is not True
                or ack.get("producer_id") != self.producer_id
                or ack.get("event_key") != key):
            raise ValueError("connector success ACK 非法")
        delivery_id = ack.get("delivery_id")
        if delivery_id is not None and (
                not isinstance(delivery_id, str) or len(delivery_id) > 256):
            raise ValueError("connector success delivery_id 非法")
        ack_bytes = json.dumps(
            ack, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        if len(ack_bytes) > 16 * 1024:
            raise ValueError("connector ACK 超过本地回执上限")
        with self._lock:
            identity = self._delivery_identity(channel, key)
            receipts = self._receipt_keys()
            if identity in receipts:
                return
            retry_document = self._retry_document()
            retry = retry_document["events"].get(identity, {})
            receipt = {
                "version": 1,
                "channel": channel,
                "event_key": key,
                "accepted_at": float(accepted_at),
                "attempt_count": int(retry.get("attempt_count", 0)) + 1,
                "delivery_id": delivery_id,
                "ack_hash": "sha256:" + hashlib.sha256(ack_bytes).hexdigest(),
            }
            try:
                receipt_identity = self._append_line(
                    self.receipts_path,
                    json.dumps(receipt, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False) + "\n",
                )
            except BaseException as append_error:
                self._clear_receipt_cache()
                try:
                    self._receipt_keys()
                except BaseException as calibration_error:
                    add_note = getattr(append_error, "add_note", None)
                    if callable(add_note):
                        add_note(
                            "receipt append 失败后的落盘校准也失败: "
                            f"{type(calibration_error).__name__}: {calibration_error}")
                raise
            self._receipt_fingerprint = receipt_identity
            receipts.add(identity)
            # Receipt is the success authority.  If state cleanup crashes, the
            # receipt still suppresses replay on restart.
            if identity in retry_document["events"]:
                updated = self._copy_retry_document(retry_document)
                del updated["events"][identity]
                self._store_retry_document(updated)

    def record_delivery_suppressed(self, channel: str, event: Dict[str, Any], *,
                                   reason: str, completed_at: float) -> None:
        """Durably terminalize an event whose historical route is not safe to infer.

        This is deliberately distinct from a remote ACK: operators must be
        able to see that no connector accepted the event.  A newly derived,
        explicitly routed successor event may still be delivered normally.
        """
        key = event["event_key"]
        if (self._CHANNEL_RE.fullmatch(channel) is None
                or isinstance(completed_at, bool)
                or not isinstance(completed_at, (int, float))
                or not math.isfinite(float(completed_at))
                or not isinstance(reason, str) or not reason or len(reason) > 1024):
            raise ValueError("delivery suppression 参数非法")
        reason_bytes = reason.encode("utf-8")
        with self._lock:
            identity = self._delivery_identity(channel, key)
            receipts = self._receipt_keys()
            if identity in receipts:
                return
            retry_document = self._retry_document()
            retry = retry_document["events"].get(identity, {})
            receipt = {
                "version": 2,
                "channel": channel,
                "event_key": key,
                "completed_at": float(completed_at),
                "attempt_count": int(retry.get("attempt_count", 0)),
                "disposition": "suppressed_unsafe_route",
                "reason_hash": "sha256:" + hashlib.sha256(reason_bytes).hexdigest(),
            }
            try:
                receipt_identity = self._append_line(
                    self.receipts_path,
                    json.dumps(receipt, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False) + "\n",
                )
            except BaseException as append_error:
                self._clear_receipt_cache()
                try:
                    self._receipt_keys()
                except BaseException as calibration_error:
                    add_note = getattr(append_error, "add_note", None)
                    if callable(add_note):
                        add_note(
                            "suppression receipt append 失败后的落盘校准也失败: "
                            f"{type(calibration_error).__name__}: {calibration_error}")
                raise
            self._receipt_fingerprint = receipt_identity
            receipts.add(identity)
            if identity in retry_document["events"]:
                updated = self._copy_retry_document(retry_document)
                del updated["events"][identity]
                self._store_retry_document(updated)

    def deliver_pending(self, connector) -> List[str]:
        """把未投递事件按队列序经 connector.send 发出；成功一条标记一条（append delivered.log）。
        send 与标记之间崩溃 → 该条重发（at-least-once；接收端按 event_key 去重）。send 抛错则中断
        （后续事件保持未投递，下次续投——不吞错、不乱序跳发）。返回本次投出的 event_key 序列。"""
        with self._legacy_delivery_lock:
            with self._lock:
                self._repair_torn_jsonl_tail(self.delivered_path)
            done = self._delivered_keys()
            sent: List[str] = []
            for ev in self._events():
                if ev["event_key"] in done:
                    continue
                outbound_event = copy.deepcopy(ev)
                outbound_event["producer_id"] = self.producer_id
                connector.send(outbound_event)          # 抛错即中断，本条未标记 → 下次重试
                with self._lock:
                    self._append_line(self.delivered_path, ev["event_key"] + "\n")
                done.add(ev["event_key"])
                sent.append(ev["event_key"])
            return sent


# ------------------------------------------------------- directive notifier --

def _directive_state_events(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    """单 directive 当前应存在的事件集（按其生命周期已走到的态；早态事件保留——outbox 幂等去重）。
    人机门控的中间态（pending_confirmation）只在扫描窗口内被外显：确认前必有扫描（人回显确认的时延
    远大于扫描节拍）；若 directive 在首次扫描前已走完生命周期，中间态事件不补发（对已生效指令追发
    "请确认"是误导）——consumed 分支例外补 pending_effect（它无行动含义、只是就绪记录）。"""
    d = row
    payload_base = {
        "directive_id": d["id"], "kind": d["kind"], "hardness": d["hardness"],
        "conversation_id": d.get("conversation_id"),
    }
    evs = [
        {"event_key": f"directive:{d['id']}:received:v2", "kind": "directive_received",
         "payload": {**payload_base, "message_id": d["source_interaction_message_id"]}},
        {"event_key": f"directive:{d['id']}:classified:v2", "kind": "directive_classified",
         "payload": {**payload_base, "consume_at": d["consume_at"]}},
    ]
    p = json.loads(d["payload_json"])
    if d["status"] == "pending":
        if d["hardness"] == "hard" and not p.get("confirmed"):
            evs.append({"event_key": f"directive:{d['id']}:pending_confirmation:v2",
                        "kind": "directive_pending_confirmation",
                        "payload": {**payload_base, "polished": p.get("polished")}})   # 展示润色稿（§4.6.3）
        else:
            evs.append({"event_key": f"directive:{d['id']}:pending_effect:v2",
                        "kind": "directive_pending_effect",
                        "payload": {**payload_base, "consume_at": d["consume_at"]}})   # 预计消费点
    elif d["status"] == "consumed":
        # 已确认硬指令必然途径 pending_effect；补齐该态事件（若消费前未扫描过，幂等 emit 不重复）
        evs.append({"event_key": f"directive:{d['id']}:pending_effect:v2",
                    "kind": "directive_pending_effect",
                    "payload": {**payload_base, "consume_at": d["consume_at"]}})
        # reprioritize and goal_amend are consumed at reasoning_start but their
        # real effects exist only in the later atomic selection / goal-version
        # commit.  Do not announce "applied" in the precheck→Runner window.
        if not d.get("_application_pending"):
            evs.append({"event_key": f"directive:{d['id']}:applied:v2", "kind": "directive_applied",
                        "payload": {**payload_base,
                                    "consumed_cycle": f"c{d['consumed_cycle']}" if d["consumed_cycle"] else None,
                                    "effect": (d.get("_decision_effect") or {})}})
    elif d["status"] == "rejected":
        # 理由恒在 payload.rejection_reason（console.reject_directive 两条路径都 json_set 写入）
        evs.append({"event_key": f"directive:{d['id']}:rejected:v2", "kind": "directive_rejected",
                    "payload": {**payload_base, "reason": p.get("rejection_reason")}})
    elif d["status"] == "superseded":
        evs.append({"event_key": f"directive:{d['id']}:superseded:v2", "kind": "directive_superseded",
                    "payload": payload_base})
    return evs


class DirectiveNotifier:
    """从 DB 扫描派生 directive 生命周期事件 → outbox（幂等）。"""

    def __init__(self, daemon: WriteDaemon, outbox: Outbox):
        self.daemon = daemon
        self.outbox = outbox

    def scan(self) -> List[str]:
        """全量扫描（幂等：已排队事件跳过）。返回本次新排队的 event_key。"""
        new_keys: List[str] = []
        rows = self.daemon.query(
            "SELECT d.id,d.kind,d.hardness,d.status,d.consume_at,d.payload_json,d.consumed_cycle,"
            "d.consumed_decision_id,d.source_interaction_message_id,m.connector,m.conversation_id "
            "FROM directive d JOIN interaction_message m ON m.id=d.source_interaction_message_id "
            "ORDER BY d.id")
        for (did, kind, hardness, status, consume_at, payload_json, ccy, cdec, smid,
             connector, conversation_id) in rows:
            row = {"id": did, "kind": kind, "hardness": hardness, "status": status,
                   "consume_at": consume_at, "payload_json": payload_json,
                   "consumed_cycle": ccy, "source_interaction_message_id": smid,
                   "conversation_id": conversation_id}
            if cdec is not None:      # applied 效果摘要取自消费决策 payload（真相在 decision 台账）
                dp = self.daemon.query_one("SELECT payload_json FROM decision WHERE id=?", (cdec,))
                row["_decision_effect"] = (json.loads(dp[0]).get("effect") if dp else None)
            if kind == "reprioritize" and status == "consumed":
                actual = self.daemon.query_one(
                    "SELECT type,payload_json FROM decision WHERE directive_id=? "
                    "AND actor='orchestrator' "
                    "AND type IN ('reprioritize_applied','reprioritize_enforced') "
                    "ORDER BY id DESC LIMIT 1", (did,))
                if actual is None:
                    row["_application_pending"] = True
                else:
                    row["_decision_effect"] = json.loads(actual[1])
            if kind == "goal_amend" and status == "consumed":
                actual = self.daemon.query_one(
                    "SELECT payload_json FROM decision WHERE directive_id=? "
                    "AND actor='agent' AND type='goal_amend' "
                    "ORDER BY id DESC LIMIT 1", (did,))
                if actual is None:
                    row["_application_pending"] = True
                else:
                    payload = json.loads(actual[0])
                    row["_decision_effect"] = payload.get("effect", payload)
            for ev in _directive_state_events(row):
                if self.outbox.emit(
                        ev["event_key"], ev["kind"], ev["payload"], channel=connector):
                    new_keys.append(ev["event_key"])
        return new_keys


# ------------------------------------------------------ interaction notifier --

class InteractionNotifier:
    """Derive outbound ACK/query/clarification events from append-only interaction truth.

    Directive/note messages are already covered by ``DirectiveNotifier``.  This
    scanner owns query and unclear messages, including every durable
    ``interaction_reply``.  Events target the source connector, so a web-console
    query remains local while a QQ query returns to QQ instead of leaking across
    channels.
    """

    _ACTION_SESSION_REFS = {DIRECTIVE_ACTION_SESSION_REF, FILE_REQUEST_ACTION_SESSION_REF}

    def __init__(self, daemon: WriteDaemon, outbox: Outbox):
        self.daemon = daemon
        self.outbox = outbox

    def scan(self) -> List[str]:
        rows = self.daemon.query(
            "SELECT m.id,m.connector,m.conversation_id,m.session_ref,c.intent,"
            "r.id,r.reply_ref,r.reply_text,r.snapshot_cycle,r.responder_kind "
            "FROM interaction_message m "
            "LEFT JOIN interaction_classification c ON c.message_id=m.id "
            "LEFT JOIN interaction_reply r ON r.message_id=m.id "
            "ORDER BY m.id,r.id")
        new_keys: List[str] = []
        received = set()
        unclear = set()
        for (mid, connector, conversation_id, session_ref, intent,
             reply_id, reply_ref, reply_text, snapshot_cycle, responder_kind) in rows:
            if mid not in received:
                received.add(mid)
                legacy_key = f"interaction:{mid}:received"
                key = f"interaction:{mid}:received:v2"
                # A v1 receipt already proves this historical message was
                # surfaced.  Do not replay a second human ACK merely because
                # v2 removed the then-premature intent field.
                if (not self.outbox.contains_event(legacy_key) and self.outbox.emit(
                        key, "interaction_received",
                        {"message_id": mid, "conversation_id": conversation_id},
                        channel=connector)):
                    new_keys.append(key)
            if (intent == "unclear" and mid not in unclear
                    and session_ref not in self._ACTION_SESSION_REFS
                    and not (isinstance(session_ref, str)
                             and session_ref.endswith(":action"))):
                unclear.add(mid)
                key = f"interaction:{mid}:unclear"
                if self.outbox.emit(
                        key, "interaction_unclear",
                        {"message_id": mid, "conversation_id": conversation_id},
                        channel=connector):
                    new_keys.append(key)
            if reply_id is not None:
                key = f"interaction:{mid}:reply:{reply_id}"
                text = reply_text if isinstance(reply_text, str) else f"回复制品：{reply_ref}"
                if self.outbox.emit(
                        key, "interaction_reply",
                        {"message_id": mid, "reply_id": reply_id, "reply_ref": reply_ref,
                         "reply_text": text,
                         "snapshot_cycle": f"c{snapshot_cycle}" if snapshot_cycle else None,
                         "responder_kind": responder_kind,
                         "conversation_id": conversation_id},
                        channel=connector):
                    new_keys.append(key)
        return new_keys


# --------------------------------------------------------- research notifier --

class ResearchNotifier:
    """Derive the remaining §4.6.6 research notification matrix.

    Terminal cycle/target facts are immutable.  ``answer_applicability`` is
    intentionally mutable, so its event key includes a canonical state hash;
    a later restriction or contradiction cannot collide with the earlier
    notification while an identical rescan remains idempotent.
    """

    def __init__(self, daemon: WriteDaemon, outbox: Outbox, audit_cadence_k: int):
        if isinstance(audit_cadence_k, bool) or not isinstance(audit_cadence_k, int) \
                or audit_cadence_k < 1:
            raise ValueError("audit_cadence_k 须为正整数")
        self.daemon = daemon
        self.outbox = outbox
        self.audit_cadence_k = audit_cadence_k

    @staticmethod
    def _bounded_text(value: Any, max_chars: int = 4096) -> Tuple[Optional[str], Optional[str]]:
        if value is None:
            return None, None
        text = str(value)
        digest = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
        if len(text) <= max_chars:
            return text, digest
        return text[:max_chars] + "…（通知投影已截断，完整值见 DB）", digest

    def scan(self) -> List[str]:
        events: List[Dict[str, Any]] = []
        for (cycle_id, goal_id, goal_ver, route, question_id, failure_kind) in self.daemon.query(
                "SELECT id,goal_id,goal_ver,route,active_question_id,failure_kind "
                "FROM cycle WHERE status='failed' ORDER BY id"):
            failure_preview, failure_hash = self._bounded_text(failure_kind, 512)
            events.append({
                "event_key": f"cycle:{cycle_id}:failed",
                "kind": "cycle_failed",
                "payload": {
                    "cycle_id": f"c{cycle_id}", "goal_id": goal_id, "goal_ver": goal_ver,
                    "route": route, "active_question_id": (
                        f"q{question_id}" if question_id is not None else None),
                    "failure_kind": failure_preview, "failure_kind_hash": failure_hash,
                    "summary_md": f"轮次 c{cycle_id} 失败；失败类型：{failure_preview or '未分类'}。",
                },
            })
        for (target_id, cycle_id, question_id, target_kind, seq, failure_kind) in self.daemon.query(
                "SELECT id,cycle_id,question_id,target_kind,seq,failure_kind FROM build_target "
                "WHERE status='engineering_blocked' ORDER BY id"):
            failure_preview, failure_hash = self._bounded_text(failure_kind, 512)
            events.append({
                "event_key": f"build_target:{target_id}:engineering_blocked",
                "kind": "engineering_blocked",
                "payload": {
                    "build_target_id": target_id, "cycle_id": f"c{cycle_id}",
                    "question_id": f"q{question_id}" if question_id is not None else None,
                    "target_kind": target_kind, "seq": seq,
                    "failure_kind": failure_preview, "failure_kind_hash": failure_hash,
                    "summary_md": (
                        f"构建目标 #{target_id}（c{cycle_id} 第 {seq} 项）遇到工程阻塞，"
                        "需要人工检查环境后由后续轮建立新 target。"),
                },
            })
        for (target_id, cycle_id, question_id, target_kind, seq, failure_kind) in self.daemon.query(
                "SELECT id,cycle_id,question_id,target_kind,seq,failure_kind FROM build_target "
                "WHERE status='failed' ORDER BY id"):
            failure_preview, failure_hash = self._bounded_text(failure_kind, 512)
            run = self.daemon.query_one(
                "SELECT id,status,failure_kind FROM run WHERE build_target_id=? "
                "ORDER BY id DESC LIMIT 1", (target_id,))
            attempt = self.daemon.query_one(
                "SELECT id,status,failure_kind,transcript_ref FROM evaluation_attempt "
                "WHERE build_target_id=? ORDER BY id DESC LIMIT 1", (target_id,))
            reconciled = self.daemon.query_one(
                "SELECT json_extract(d.payload_json,'$.operation_id'),"
                "json_extract(d.payload_json,'$.outcome'),"
                "json_extract(d.payload_json,'$.receipt_ref') FROM decision d "
                "WHERE d.actor='orchestrator' AND d.type='execution_reconciled' "
                "AND json_valid(d.payload_json) AND ("
                "(json_extract(d.payload_json,'$.db_owner_kind')='build_target' "
                " AND json_extract(d.payload_json,'$.db_owner_id')=?) OR "
                "(json_extract(d.payload_json,'$.db_owner_kind')='run' "
                " AND json_extract(d.payload_json,'$.db_owner_id') IN "
                "   (SELECT id FROM run WHERE build_target_id=?)) OR "
                "(json_extract(d.payload_json,'$.db_owner_kind')='evaluation_attempt' "
                " AND json_extract(d.payload_json,'$.db_owner_id') IN "
                "   (SELECT id FROM evaluation_attempt WHERE build_target_id=?))) "
                "ORDER BY d.id DESC LIMIT 1", (target_id, target_id, target_id))
            events.append({
                "event_key": f"build_target:{target_id}:failed",
                "kind": "build_target_failed",
                "payload": {
                    "build_target_id": target_id, "cycle_id": f"c{cycle_id}",
                    "question_id": f"q{question_id}" if question_id is not None else None,
                    "target_kind": target_kind, "seq": seq,
                    "failure_kind": failure_preview, "failure_kind_hash": failure_hash,
                    "run": ({"run_id": run[0], "status": run[1], "failure_kind": run[2]}
                            if run is not None else None),
                    "evaluation_attempt": ({
                        "attempt_id": attempt[0], "status": attempt[1],
                        "failure_kind": attempt[2], "transcript_ref": attempt[3],
                    } if attempt is not None else None),
                    "execution_receipt": ({
                        "operation_id": reconciled[0], "outcome": reconciled[1],
                        "receipt_ref": reconciled[2],
                    } if reconciled is not None else None),
                    "summary_md": (
                        f"构建目标 #{target_id}（c{cycle_id} 第 {seq} 项）失败；"
                        f"失败类型：{failure_preview or '未分类'}。"),
                },
            })
        for (cycle_id, goal_id, goal_ver, status, route, question_id,
             cost_total, next_intent) in self.daemon.query(
                "SELECT id,goal_id,goal_ver,status,route,active_question_id,cost_total,next_intent "
                "FROM cycle WHERE status IN ('done','failed','aborted') AND (id % ?)=0 ORDER BY id",
                (self.audit_cadence_k,)):
            events.append({
                "event_key": f"cycle:{cycle_id}:summary",
                "kind": "cycle_summary",
                "payload": {
                    "cycle_id": f"c{cycle_id}", "goal_id": goal_id, "goal_ver": goal_ver,
                    "status": status, "route": route,
                    "active_question_id": f"q{question_id}" if question_id is not None else None,
                    "cost_total": cost_total, "next_intent": next_intent,
                    "summary_md": (
                        f"周期摘要：c{cycle_id} 状态 {status}，路线 {route or '未定'}，"
                        f"下一意图 {next_intent or '未定'}。"),
                },
            })
        for (answer_id, goal_id, goal_ver, audit_cycle, status, rationale,
             spawned_question_id, question_id) in self.daemon.query(
                "SELECT aa.answer_id,aa.goal_id,aa.goal_ver,aa.audit_cycle,aa.status,"
                "aa.rationale_md,aa.spawned_question_id,a.question_id "
                "FROM answer_applicability aa JOIN answer a ON a.id=aa.answer_id "
                "WHERE aa.status IN ('blocked','needs_revalidation','obsolete','contradicted') "
                "ORDER BY aa.answer_id,aa.goal_id,aa.goal_ver"):
            rationale_preview, rationale_hash = self._bounded_text(rationale)
            payload = {
                "answer_id": answer_id, "question_id": f"q{question_id}",
                "goal_id": goal_id, "goal_ver": goal_ver,
                "audit_cycle": f"c{audit_cycle}" if audit_cycle is not None else None,
                "status": status, "rationale_md": rationale_preview,
                "rationale_hash": rationale_hash,
                "spawned_question_id": (
                    f"q{spawned_question_id}" if spawned_question_id is not None else None),
                "summary_md": (
                    f"旧结论 #{answer_id} 在目标 v{goal_ver} 的适用性变为 {status}："
                    f"{(rationale_preview or '未提供理由')[:1200]}"),
            }
            canonical = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
            events.append({
                "event_key": f"applicability:{answer_id}:{goal_id}:{goal_ver}:{digest}",
                "kind": "answer_applicability_changed", "payload": payload,
            })
        new_keys: List[str] = []
        for event in events:
            if self.outbox.emit(event["event_key"], event["kind"], event["payload"]):
                new_keys.append(event["event_key"])
        return new_keys


# ----------------------------------------------------- file-request service --

_COPY_CHUNK_BYTES = 1024 * 1024
# 必须与 compiler goal-wide ContextPack 总资产上限保持一致；resolve 在不可变终态前执行同口径接纳闸。
_MAX_MANAGED_FILES_PER_GOAL = MAX_ASSETS_PER_GOAL
# 非 bundle 阶段只能看到有界文本预览；双层预算防单个/多个附件把 ContextPack 挤爆。
_MAX_ASSET_PREVIEW_BYTES = 8 * 1024
_MAX_REQUEST_PREVIEW_BYTES = 32 * 1024


_UPLOAD_DIRECTORY_FLAGS = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                           | getattr(os, "O_NOFOLLOW", 0)
                           | getattr(os, "O_CLOEXEC", 0))
_UPLOAD_FILE_FLAGS = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                      | getattr(os, "O_CLOEXEC", 0)
                      | getattr(os, "O_NONBLOCK", 0))
_MAX_UPLOAD_DIRECTORY_DEPTH = 64
_MAX_UPLOAD_ENTRIES_PER_DIRECTORY = 1024
_MAX_UPLOAD_ENTRIES_PER_REQUEST = 4096
_MAX_UPLOAD_DIRECTORIES_PER_REQUEST = 1024


def _inode_identity(info: os.stat_result) -> Tuple[int, int]:
    return info.st_dev, info.st_ino


def _regular_fingerprint(info: os.stat_result) -> tuple:
    """复制期间必须稳定的常规文件身份；ctime 可捕获原 inode 的原地改写。"""
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _require_same_directory(*, expected: Tuple[int, int], path_info: os.stat_result,
                            opened_info: os.stat_result, display: Path) -> None:
    if (not stat.S_ISDIR(path_info.st_mode) or not stat.S_ISDIR(opened_info.st_mode)
            or _inode_identity(path_info) != expected
            or _inode_identity(opened_info) != expected):
        raise OSError(f"上传目录在枚举期间发生路径替换: {display}")


def _require_same_regular(*, expected_fingerprint: tuple, path_info: os.stat_result,
                          opened_info: os.stat_result, display: Path) -> None:
    if (not stat.S_ISREG(path_info.st_mode) or not stat.S_ISREG(opened_info.st_mode)
            or path_info.st_nlink != 1 or opened_info.st_nlink != 1
            or _inode_identity(path_info) != _inode_identity(opened_info)
            or _regular_fingerprint(opened_info) != expected_fingerprint):
        raise OSError(f"上传文件在接纳期间发生替换/改写或不是独占常规文件: {display}")


@dataclass
class _OpenedUploadFile:
    """枚举时固定的上传文件 capability；``display_path`` 只供审计/兼容测试，绝不用于重开。"""

    fd: int
    root_fd: int
    components: Tuple[str, ...]
    directory_identities: Tuple[Tuple[int, int], ...]
    fingerprint: tuple
    display_path: Path
    relative_path: Path

    def __fspath__(self) -> str:
        # 保留现有 monkeypatch 中 ``Path(src)`` 的可观测形态；安全逻辑不信任这个字符串。
        return str(self.display_path)


@dataclass
class _UploadTraversalBudget:
    """一次 resolve 的目录枚举总预算；非文件项和失败 stat 同样不能绕过。"""

    entries: int = 0
    directories: int = 0

    def observe_entry(self, display: Path) -> None:
        self.entries += 1
        if self.entries > _MAX_UPLOAD_ENTRIES_PER_REQUEST:
            raise ValueError(
                f"用户上传树目录项超过安全上限 {_MAX_UPLOAD_ENTRIES_PER_REQUEST}: {display}")

    def enter_directory(self, display: Path) -> None:
        self.directories += 1
        if self.directories > _MAX_UPLOAD_DIRECTORIES_PER_REQUEST:
            raise ValueError(
                f"用户上传树目录数超过安全上限 {_MAX_UPLOAD_DIRECTORIES_PER_REQUEST}: {display}")


def _open_directory_entry(parent_fd: int, name: str, expected: os.stat_result,
                          display: Path) -> int:
    """从已固定父目录 openat 子目录，并把 scandir 快照、fd、当前目录项三方对齐。"""
    fd = os.open(name, _UPLOAD_DIRECTORY_FLAGS, dir_fd=parent_fd)
    try:
        opened = os.fstat(fd)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        identity = _inode_identity(expected)
        if not stat.S_ISDIR(expected.st_mode):
            raise OSError(f"上传目录项不是目录: {display}")
        _require_same_directory(
            expected=identity, path_info=current, opened_info=opened, display=display)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_upload_file(parent_fd: int, name: str, expected: os.stat_result, *, root_fd: int,
                      components: Tuple[str, ...],
                      directory_identities: Tuple[Tuple[int, int], ...],
                      display: Path, relative: Path) -> _OpenedUploadFile:
    """从固定父目录 openat 常规文件；O_NONBLOCK 防检查后被换 FIFO 时阻塞 daemon。"""
    if not stat.S_ISREG(expected.st_mode) or expected.st_nlink != 1:
        raise OSError(f"上传文件不是独占常规文件: {display}")
    fd = os.open(name, _UPLOAD_FILE_FLAGS, dir_fd=parent_fd)
    try:
        opened = os.fstat(fd)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        fingerprint = _regular_fingerprint(expected)
        _require_same_regular(
            expected_fingerprint=fingerprint, path_info=current,
            opened_info=opened, display=display)
        return _OpenedUploadFile(
            fd=fd, root_fd=root_fd, components=components,
            directory_identities=directory_identities,
            fingerprint=fingerprint, display_path=display, relative_path=relative)
    except BaseException:
        os.close(fd)
        raise


def _enumerate_upload_directory(*, directory_fd: int, root_fd: int,
                                components: Tuple[str, ...],
                                directory_identities: Tuple[Tuple[int, int], ...],
                                display_root: Path, relative_root: Path,
                                opened_files: List[_OpenedUploadFile],
                                max_files: int, traversal_budget: _UploadTraversalBudget) -> None:
    """只经已持有 dirfd 递归；先 lstat 分类，再用 openat 固定每个被接纳文件。"""
    if len(components) > _MAX_UPLOAD_DIRECTORY_DEPTH:
        raise ValueError(
            f"用户文件目录嵌套超过安全上限 {_MAX_UPLOAD_DIRECTORY_DEPTH}: {display_root}")
    entries = []
    with os.scandir(directory_fd) as iterator:
        for entry in iterator:
            traversal_budget.observe_entry(display_root / entry.name)
            if len(entries) >= _MAX_UPLOAD_ENTRIES_PER_DIRECTORY:
                raise ValueError(
                    f"单个上传目录项超过安全上限 {_MAX_UPLOAD_ENTRIES_PER_DIRECTORY}: {display_root}")
            entries.append((entry.name, entry.stat(follow_symlinks=False)))
    for name, entry_info in sorted(entries, key=lambda pair: pair[0]):
        display = display_root / name
        relative = relative_root / name
        if stat.S_ISLNK(entry_info.st_mode):
            continue                         # 既有 symlink 从来不是“已提供文件”
        if stat.S_ISDIR(entry_info.st_mode):
            traversal_budget.enter_directory(display)
            child_fd = _open_directory_entry(directory_fd, name, entry_info, display)
            try:
                child_identity = _inode_identity(os.fstat(child_fd))
                _enumerate_upload_directory(
                    directory_fd=child_fd, root_fd=root_fd,
                    components=components + (name,),
                    directory_identities=directory_identities + (child_identity,),
                    display_root=display, relative_root=relative,
                    opened_files=opened_files, max_files=max_files,
                    traversal_budget=traversal_budget)
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(entry_info.st_mode):
            if len(opened_files) >= max_files:
                raise ValueError(
                    f"用户文件数超过 goal-wide 安全上限 {_MAX_MANAGED_FILES_PER_GOAL}；"
                    "请先打包成 tar/zip 再上传")
            opened_files.append(_open_upload_file(
                directory_fd, name, entry_info, root_fd=root_fd,
                components=components + (name,),
                directory_identities=directory_identities,
                display=display, relative=relative))
        # FIFO/device/socket 不是文件上传，且绝不 open，避免阻塞或设备副作用。


@contextmanager
def _regular_files_no_symlink(root_fd: int, item_name: str, src: Path, *,
                              max_files: int, traversal_budget: _UploadTraversalBudget):
    """固定 item 目录并返回已打开的常规文件；结束时统一关闭 capability。

    初始缺失、既有 symlink/非目录仍按“用户未提供”处理；一旦观察到目录后发生替换则 fail closed，
    由 resolve rollback 保持请求 pending。
    """
    opened_files: List[_OpenedUploadFile] = []
    item_fd: Optional[int] = None
    try:
        try:
            item_info = os.stat(item_name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            yield opened_files
            return
        if not stat.S_ISDIR(item_info.st_mode):
            yield opened_files
            return
        traversal_budget.enter_directory(src)
        item_fd = _open_directory_entry(root_fd, item_name, item_info, src)
        item_identity = _inode_identity(os.fstat(item_fd))
        _enumerate_upload_directory(
            directory_fd=item_fd, root_fd=root_fd, components=(item_name,),
            directory_identities=(item_identity,), display_root=src,
            relative_root=Path(), opened_files=opened_files, max_files=max_files,
            traversal_budget=traversal_budget)
        # 与旧实现 ``sorted(List[Path])`` 完全同序，确保 asset-N/ref 的确定性身份不漂移。
        opened_files.sort(key=lambda opened: opened.relative_path)
        yield opened_files
    finally:
        if item_fd is not None:
            os.close(item_fd)
        for opened in opened_files:
            os.close(opened.fd)


@contextmanager
def _open_upload_root(path: Path):
    """固定 uploads 根；允许上层安全解析器传入本进程的 ``/proc/self/fd/N`` capability。"""
    raw = str(path)
    proc_prefix = "/proc/self/fd/"
    fd: Optional[int] = None
    if raw.startswith(proc_prefix) and raw[len(proc_prefix):].isdigit():
        fd = os.dup(int(raw[len(proc_prefix):]))
    else:
        # abspath 会在真正的路径解析前词法折叠 ``..``，从而跳过本应被 O_NOFOLLOW
        # 检查的中间 symlink（例如 symlink/../uploads）。上传路径不需要父级穿越，直接拒绝。
        if os.pardir in raw.split(os.sep):
            raise ValueError("uploads_dir 不得含 '..' 路径组件")
        try:
            absolute = os.path.abspath(raw)
            fd = os.open(os.sep, _UPLOAD_DIRECTORY_FLAGS)
            for component in (part for part in absolute.split(os.sep) if part):
                next_fd = os.open(component, _UPLOAD_DIRECTORY_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = next_fd
        except FileNotFoundError:
            if fd is not None:
                os.close(fd)
            yield None                       # 整个上传根缺失 = 所有 item 未提供
            return
        except BaseException:
            if fd is not None:
                os.close(fd)
            raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise OSError(f"uploads_dir 不是目录 capability: {path}")
        yield fd
    finally:
        os.close(fd)


def _verify_opened_upload_file(source: _OpenedUploadFile) -> None:
    """从固定 uploads 根逐组件重走目录项，确认枚举身份仍在；文件本体始终使用原 fd。"""
    directory_components = source.components[:-1]
    if len(directory_components) != len(source.directory_identities):
        raise RuntimeError("上传 capability 的目录组件与身份数量不一致")
    current_fd = os.dup(source.root_fd)
    try:
        for index, (name, expected_identity) in enumerate(zip(
                directory_components, source.directory_identities)):
            display = source.display_path.parents[
                len(source.components) - index - 2]
            child_fd: Optional[int] = None
            try:
                child_fd = os.open(name, _UPLOAD_DIRECTORY_FLAGS, dir_fd=current_fd)
                opened = os.fstat(child_fd)
                path_info = os.stat(name, dir_fd=current_fd, follow_symlinks=False)
                _require_same_directory(
                    expected=expected_identity, path_info=path_info,
                    opened_info=opened, display=display)
            except OSError as error:
                if child_fd is not None:
                    os.close(child_fd)
                raise OSError(
                    f"上传源目录在枚举后发生替换: {source.display_path}") from error
            os.close(current_fd)
            current_fd = child_fd

        filename = source.components[-1]
        opened_info = os.fstat(source.fd)
        path_info = os.stat(filename, dir_fd=current_fd, follow_symlinks=False)
        _require_same_regular(
            expected_fingerprint=source.fingerprint, path_info=path_info,
            opened_info=opened_info, display=source.display_path)
    except OSError as error:
        if str(source.display_path) in str(error):
            raise
        raise OSError(f"上传源在枚举后发生替换: {source.display_path}") from error
    finally:
        os.close(current_fd)


def _remove_private_tree(path: Path) -> None:
    """删除 daemon 专属 staging/final 树；任何 symlink 都 fail closed，避免清理时跟出托管根。"""
    if not os.path.lexists(str(path)):
        return
    if path.is_symlink():
        raise OSError(f"托管路径不得是 symlink: {path}")
    if not path.is_dir():
        raise OSError(f"托管路径不是目录: {path}")
    # 已发布树是只读的；重试/取消清理前只给 owner 临时恢复目录写权限和文件写权限。
    for dirpath, dirnames, filenames in os.walk(path, topdown=True, followlinks=False):
        for name in dirnames:
            p = Path(dirpath) / name
            if p.is_symlink():
                raise OSError(f"托管树内出现 symlink: {p}")
            p.chmod(0o700)
        for name in filenames:
            p = Path(dirpath) / name
            if p.is_symlink():
                raise OSError(f"托管树内出现 symlink: {p}")
            p.chmod(0o600)
        Path(dirpath).chmod(0o700)
    shutil.rmtree(path)


def _publish_read_only(root: Path) -> None:
    """减少解析后被意外改写的机会（同 UID 对抗隔离仍留给后续内容寻址/只读挂载检查点）。"""
    for dirpath, dirnames, filenames in os.walk(root, topdown=False, followlinks=False):
        for name in filenames:
            p = Path(dirpath) / name
            if p.is_symlink():
                raise OSError(f"staging 内出现 symlink: {p}")
            p.chmod(0o444)
        for name in dirnames:
            p = Path(dirpath) / name
            if p.is_symlink():
                raise OSError(f"staging 内出现 symlink: {p}")
            p.chmod(0o555)
        Path(dirpath).chmod(0o555)


def _fsync_directory(path: Path) -> None:
    """持久化目录项；O_NOFOLLOW 保证 fsync 的仍是预期托管目录而非替换后的 symlink。"""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(path), flags)
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(f"fsync 目标不是目录: {path}")
        os.fsync(fd)
    finally:
        os.close(fd)


def _validated_tree_bytes(root: Path, *, count_bytes: bool) -> int:
    """验证一棵 daemon 托管树只含真实目录/常规文件，并按需累计逻辑文件字节。"""
    if root.is_symlink() or not root.is_dir():
        raise OSError(f"托管树不是实体目录: {root}")
    total = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames:
            child = Path(dirpath) / name
            info = os.stat(str(child), follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode):
                raise OSError(f"托管树目录项异常（含 symlink）: {child}")
        for name in filenames:
            child = Path(dirpath) / name
            info = os.stat(str(child), follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                raise OSError(f"托管树文件项异常（含 symlink）: {child}")
            if count_bytes:
                total += info.st_size
    return total


def _managed_published_bytes(managed_root: Path) -> int:
    """累计既有 final 与遗留 staging 的实体文件；调用方须先删除本 request 的旧 attempt。

    ``.staging`` 中可能有别的 request 崩溃后留下的实体字节；若只验证不计费，反复制造遗留 attempt
    就能绕过 root-global disk quota。当前 request 在调用本函数前已清理，因此不会重复计费。
    """
    total = 0
    for child in managed_root.iterdir():
        if child.name == ".staging":
            total += _validated_tree_bytes(child, count_bytes=True)
            continue
        if not child.name.isdecimal():
            raise OSError(f"input/user_provided 出现未知托管项: {child}")
        total += _validated_tree_bytes(child, count_bytes=True)
    return total


def _remove_attempt_durable(managed_root: Path, stage_root: Path, dest_root: Path) -> None:
    """撤回非权威 attempt，并把两个父目录的删除目录项落盘。"""
    _remove_private_tree(stage_root)
    _remove_private_tree(dest_root)
    _fsync_directory(stage_root.parent)
    _fsync_directory(managed_root)


def _rollback_attempt_best_effort(managed_root: Path, stage_root: Path, dest_root: Path,
                                  primary: BaseException) -> None:
    """主流程已失败时尽力撤回 attempt；cleanup 失败只注记，不得覆盖 ``primary``。

    生产环境仍支持 Python 3.9，故在没有 ``BaseException.add_note`` 时写入同语义的
    ``__notes__``；升级至 3.11+ 后会自动使用原生异常注记。
    """
    try:
        _remove_attempt_durable(managed_root, stage_root, dest_root)
    except BaseException as cleanup_error:
        note = ("file request rollback cleanup 失败："
                f"{type(cleanup_error).__name__}: {cleanup_error}")
        try:
            add_note = getattr(primary, "add_note", None)
            if callable(add_note):
                add_note(note)
            else:
                notes = list(getattr(primary, "__notes__", ()))
                notes.append(note)
                primary.__notes__ = notes
        except BaseException:
            # 异常对象若拒绝自定义属性，仍必须保留并重抛原始失败。
            pass
        try:
            logger.exception("file request rollback cleanup 失败；保留原始异常 %s",
                             type(primary).__name__)
        except BaseException:
            # 日志 handler 也不应反客为主覆盖原始异常。
            pass


def _fsync_managed_ancestors(managed_root: Path) -> None:
    """持久化首次创建链：managed 自身 fsync 不会替代其父目录中的目录项落盘。"""
    input_root = managed_root.parent
    work_root = input_root.parent
    _fsync_directory(input_root)   # user_provided 在 input/ 中的目录项
    _fsync_directory(work_root)    # input/ 在 work_root 中的目录项


@contextmanager
def _claim_file_request_operation(managed_root: Path):
    """跨进程独占同一 managed_root 的 FS→DB 操作，进程退出时由内核自动释放。

    锁文件使用永不 unlink 的稳定 inode；否则旧持有者锁住被删 inode、新调用者锁住新 inode，会形成
    split-brain。root-global 临界区覆盖 cleanup→累计 quota→copy/publish→DB 终态：既串行同 request 的
    resolve/cancel，也保证不同 goal/request 不会各自基于同一旧 quota 快照同时超额接纳。锁内仍须重读
    DB pending，因为调用者可能在等锁期间已被另一个进程迁入终态。
    """
    work_root = managed_root.parent.parent
    lock_path = work_root / ".file-request-operation.lock"
    flags = (os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
             | getattr(os, "O_NOFOLLOW", 0))
    fd = os.open(str(lock_path), flags, 0o600)
    acquired = False
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError(f"file request claim 不是独占常规文件: {lock_path}")
        fcntl.flock(fd, fcntl.LOCK_EX)
        acquired = True
        yield
    finally:
        if acquired:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        else:
            os.close(fd)


def _count_resolved_assets_for_goal(daemon: WriteDaemon, goal_id: int) -> int:
    """严格解析同 goal 的不可变 resolved 回执并累计 provided 资产；旧回执损坏即 fail closed。"""
    total = 0
    seen_refs = set()
    rows = daemon.query(
        "SELECT id,items_json,resolution_json FROM interaction_request "
        "WHERE goal_id=? AND status='resolved' ORDER BY id", (goal_id,))
    for request_id, items_json, resolution_json in rows:
        try:
            items = json.loads(items_json)
            resolution = json.loads(resolution_json)
        except (TypeError, json.JSONDecodeError) as e:
            raise ValueError(f"interaction_request {request_id} resolved 回执 JSON 损坏") from e
        if (not isinstance(items, list) or not items or any(not isinstance(item, dict) for item in items)
                or not isinstance(resolution, list)
                or len(resolution) != len(items)):
            raise ValueError(
                f"interaction_request {request_id} resolved 回执须与非空 items 等长")
        for item_no, outcome in enumerate(resolution, start=1):
            outcome_keys = set(outcome) if isinstance(outcome, dict) else set()
            if outcome_keys != {"provided"} and outcome_keys != {"unavailable"}:
                raise ValueError(
                    f"interaction_request {request_id} item {item_no} 回执须恰含 provided/unavailable")
            if "unavailable" in outcome:
                if not isinstance(outcome["unavailable"], str) or not outcome["unavailable"].strip():
                    raise ValueError(
                        f"interaction_request {request_id} item {item_no} unavailable 理由损坏")
                continue
            provided = outcome["provided"]
            if not isinstance(provided, list) or not provided or any(not isinstance(a, dict) for a in provided):
                raise ValueError(
                    f"interaction_request {request_id} item {item_no} provided 回执损坏")
            legacy_flags = []
            for asset_no, asset in enumerate(provided, start=1):
                digest = asset.get("hash")
                if (not isinstance(asset.get("path"), str) or not asset["path"]
                        or asset.get("hash_alg") != "sha256" or not isinstance(digest, str)
                        or len(digest) != 64
                        or any(ch not in "0123456789abcdefABCDEF" for ch in digest)):
                    raise ValueError(
                        f"interaction_request {request_id} item {item_no} asset {asset_no} 身份损坏")
                ref = asset.get("ref")
                size = asset.get("size_bytes")
                legacy = ref is None and size is None
                legacy_flags.append(legacy)
                if not legacy:
                    expected_ref = (
                        f"user-file-request:r{request_id}:item:{item_no}:asset:{asset_no}")
                    if (ref != expected_ref or isinstance(size, bool)
                            or not isinstance(size, int) or size < 0 or ref in seen_refs):
                        raise ValueError(
                            f"interaction_request {request_id} item {item_no} asset {asset_no} ref/size 损坏")
                    seen_refs.add(ref)
            if any(legacy_flags) and not all(legacy_flags):
                raise ValueError(
                    f"interaction_request {request_id} item {item_no} 混合 legacy/新版资产回执")
            total += len(provided)
            if total > _MAX_MANAGED_FILES_PER_GOAL:
                raise ValueError(
                    f"goal {goal_id} 已 resolved 资产数超过安全上限 {_MAX_MANAGED_FILES_PER_GOAL}")
    return total


def _read_committed_resolution_state(daemon: WriteDaemon, request_id: int) -> Optional[tuple]:
    """用全新只读连接判定 COMMIT 后的外部可见真相；同 writer 连接可能仍看到未提交行。"""
    databases = daemon.conn.execute("PRAGMA database_list").fetchall()
    main = next((row for row in databases if row[1] == "main"), None)
    if main is None or not main[2]:
        raise sqlite3.OperationalError("内存库/匿名主库无法独立确认 COMMIT 终态")
    db_path = Path(main[2])
    if db_path.is_symlink():
        raise sqlite3.OperationalError("research.sqlite 路径为 symlink，拒绝独立确认")
    conn = sqlite3.connect(f"file:{quote(str(db_path))}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT status,resolution_json,resolved_message_id "
            "FROM interaction_request WHERE id=?", (request_id,)).fetchone()
    finally:
        conn.close()
    return row


def _copy_hash_regular(src: _OpenedUploadFile, dest: Path, *, source_root: Path,
                       remaining_bytes: int, preview_limit_bytes: int) -> tuple:
    """从枚举时已固定的同一 fd 流式复制/hash/校验 UTF-8；绝不再按源路径打开。

    返回 ``(size, sha256, preview_or_none, preview_bytes, preview_truncated)``。只有整个文件都是严格
    UTF-8 时才返回 preview；预览原始字节受调用方预算限制，尾部若截在多字节字符中间则只丢该残片。
    """
    del source_root                         # 兼容既有 monkeypatch 签名；安全边界已是 src capability。
    if not isinstance(src, _OpenedUploadFile):
        raise TypeError("_copy_hash_regular 只接受枚举时固定的上传文件 capability")
    _verify_opened_upload_file(src)
    os.lseek(src.fd, 0, os.SEEK_SET)
    dest.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    utf8_decoder = codecs.getincrementaldecoder("utf-8")("strict")
    utf8_valid = True
    preview_raw = bytearray()
    copied = 0
    with dest.open("xb") as out:
        while True:
            chunk = os.read(src.fd, _COPY_CHUNK_BYTES)
            if not chunk:
                break
            copied += len(chunk)
            if copied > remaining_bytes:
                raise ValueError("用户文件总字节数超过 resources.disk_quota_gb")
            out.write(chunk)
            digest.update(chunk)
            if utf8_valid:
                try:
                    decoded = utf8_decoder.decode(chunk, final=False)
                    # 严格 UTF-8 仍可能是带 NUL/终端控制符的二进制；只保留常用文本空白。
                    if any((ord(ch) < 0x20 and ch not in "\t\n\r") or ord(ch) == 0x7f
                           for ch in decoded):
                        utf8_valid = False
                except UnicodeDecodeError:
                    utf8_valid = False
            if len(preview_raw) < preview_limit_bytes:
                preview_raw.extend(chunk[:preview_limit_bytes - len(preview_raw)])
        out.flush()
        os.fsync(out.fileno())
    # 路径替换、hardlink、新 inode 或原 inode 原地改写都必须在发布前让整个 attempt 回滚。
    _verify_opened_upload_file(src)
    if utf8_valid:
        try:
            utf8_decoder.decode(b"", final=True)
        except UnicodeDecodeError:
            utf8_valid = False
    preview = None
    preview_bytes = 0
    preview_truncated = False
    if utf8_valid:
        # final=False 保留被预算截断的 UTF-8 尾部残片，确保返回 str 始终可安全 JSON 化。
        preview = codecs.getincrementaldecoder("utf-8")("strict").decode(bytes(preview_raw), final=False)
        preview_bytes = len(preview.encode("utf-8"))
        preview_truncated = copied > preview_bytes
    return copied, digest.hexdigest(), preview, preview_bytes, preview_truncated


class FileRequestReject(Exception):
    """创建拒绝（§4.6.8 三判据 / schema 拒 / 已有 pending）——干净拒，不落单。"""


class FileRequestService:
    """文件请求全流水：create_checked → [全局等待] → managed-root 跨进程 claim → 一次性终态。

    resolve/cancel 的文件系统副作用与 DB 条件迁移始终处于同一稳定 ``flock`` claim 内；锁内二次读取
    pending，保证等待者不会拿过时初检去删除赢家已发布的资产；root-global 锁同时串行累计 disk quota。
    锁不写 DB 中间态，崩溃由内核释锁后可重试。
    """

    def __init__(self, daemon: WriteDaemon, schema_set, policy: Dict[str, Any], input_root: str):
        self.daemon = daemon
        self.schema_set = schema_set          # SchemaSet：validator("resource_request")
        self.policy = policy["interaction_request"]
        self.input_root = Path(input_root)    # input/user_provided/ 的父目录（input/）
        self.max_managed_bytes = int(float(policy["resources"]["disk_quota_gb"]) * (1024 ** 3))

    def _managed_paths(self, request_id: int) -> tuple:
        """构造同文件系统 staging/final 路径，并机械拒绝托管根 symlink/越界。"""
        # ``Path.mkdir(exist_ok=True)`` 会默许现有的目录 symlink；必须在创建/解析任何子路径前拒绝，
        # 否则攻击者可让整个 user_provided 树落到 work/input 之外。
        if self.input_root.is_symlink():
            raise OSError("input_root 不得是 symlink")
        self.input_root.mkdir(parents=True, exist_ok=True)
        input_real = self.input_root.resolve(strict=True)
        managed = self.input_root / "user_provided"
        if managed.is_symlink():
            raise OSError("input/user_provided 不得是 symlink")
        managed.mkdir(mode=0o700, exist_ok=True)
        managed_real = managed.resolve(strict=True)
        if input_real not in managed_real.parents:
            raise OSError("input/user_provided 逃出 input_root")
        staging_parent = managed_real / ".staging"
        if staging_parent.is_symlink():
            raise OSError("input/user_provided/.staging 不得是 symlink")
        staging_parent.mkdir(mode=0o700, exist_ok=True)
        return managed_real, staging_parent / str(request_id), managed_real / str(request_id)

    def create_checked(self, *, goal_id: int, goal_ver: int, stage: str, request: Dict[str, Any],
                       cycle_id: Optional[str] = None, question_id: Optional[str] = None) -> int:
        """schema 校验 → **幂等先行** → 三判据 → interaction_request(pending)。
        幂等在 quota 之前（外审 SHOULD），但**只复用 pending attempt**：同一阶段调用在请求仍等待时重放
        返回既有单；resolved/cancelled 是已完成的回执，后续无状态工人若再次提出同 hash 请求会收到明确
        ``FileRequestReject`` 反馈，必须消费已有托管资产/取消理由或改变请求条件，不能再开一张相同 pending。
        落单撞 uq_ireq_one_pending（同 goal 另一张 pending）→ 转业务拒因，不外泄 DDL 错误文本。"""
        from jsonschema import ValidationError
        try:
            self.schema_set.validator("resource_request").validate(request)
        except ValidationError as e:
            raise FileRequestReject(f"schema 拒: {e.message}") from e
        items = request["items"]
        items_json = json.dumps(items, ensure_ascii=False, sort_keys=True)
        request_hash = hashlib.sha256(items_json.encode()).hexdigest()
        existing = self.daemon.query_one(
            "SELECT id FROM interaction_request WHERE goal_id=? AND request_hash=? AND status='pending' "
            "ORDER BY id DESC LIMIT 1",
            (goal_id, request_hash))
        if existing:
            return existing[0]                     # 同一 pending attempt 的幂等重试：quota/enabled 不再拦
        terminal = self.daemon.query_one(
            "SELECT id,status,resolution_json FROM interaction_request WHERE goal_id=? AND request_hash=? "
            "AND status<>'pending' ORDER BY id DESC LIMIT 1", (goal_id, request_hash))
        if terminal:
            legacy = False
            if terminal[1] == "resolved":
                try:
                    old_resolution = json.loads(terminal[2])
                    legacy = any(
                        isinstance(outcome, dict) and isinstance(outcome.get("provided"), list)
                        and any(isinstance(asset, dict)
                                and ("ref" not in asset or "size_bytes" not in asset)
                                for asset in outcome["provided"])
                        for outcome in old_resolution)
                except (TypeError, json.JSONDecodeError):
                    raise FileRequestReject(
                        f"同 hash 文件请求终态 attempt #{terminal[0]} resolution 损坏，须人工修复") from None
            detail = ("旧版回执缺安全 ref/size，须改变请求条件后重新上传" if legacy else
                      "已提供托管资产，须消费 ContextPack 文件回执") if terminal[1] == "resolved" else \
                     "用户已取消，须消费取消理由并改道"
            raise FileRequestReject(
                f"同 hash 文件请求已有终态 attempt #{terminal[0]}（{terminal[1]}）：{detail}；不得原样重提")
        if not self.policy.get("enabled", True):
            raise FileRequestReject("文件请求通道未启用（policy.interaction_request.enabled=false）")
        if len(items) > self.policy["max_items_per_request"]:
            raise FileRequestReject(f"条目数 {len(items)} 超上限 {self.policy['max_items_per_request']}")
        # cancelled 也是不可变终态并会进入 goal-wide ContextPack；若不计它，模型可用不同 hash
        # 无限“创建→取消”撑爆固定锚。额度按全部历史请求计，和回执可见范围保持一致。
        n = self.daemon.query_one(
            "SELECT count(*) FROM interaction_request WHERE goal_id=?", (goal_id,))[0]
        request_limit = min(int(self.policy["max_requests_per_goal"]), MAX_FILE_REQUESTS_PER_GOAL)
        if n >= request_limit:
            raise FileRequestReject(f"goal {goal_id} 请求数已达上限 {request_limit}"
                                    "（含 cancelled 的全部历史请求口径）")
        from .interaction import InteractionIngest
        try:
            return InteractionIngest(self.daemon).create_file_request(
                goal_id=goal_id, goal_ver=goal_ver, stage=stage, summary_md=request["summary_md"],
                items_json=items_json, request_hash=request_hash, cycle_id=cycle_id, question_id=question_id)
        except sqlite3.IntegrityError as e:
            if "uq_ireq_one_pending" in str(e) or "interaction_request" in str(e):
                raise FileRequestReject("同 goal 已有一张 pending 文件请求（先 resolve/cancel 再提新单）") from e
            raise

    def _check_provenance(self, request_id: int, resolved_message_id: int) -> tuple:
        """终态 provenance 校验（外审 SHOULD）：resolved_message_id 必须存在且与请求同 goal——
        否则可把别的 goal 的入站消息挂到本请求终态上，破坏"用户答复/取消 provenance"语义。
        消息 goal 未绑定（NULL）也拒（fail closed）。返回 (status, items_json, goal_id)。"""
        row = self.daemon.query_one("SELECT status, items_json, goal_id FROM interaction_request WHERE id=?",
                                    (request_id,))
        if row is None:
            raise ValueError(f"interaction_request 不存在: {request_id}")
        msg = self.daemon.query_one("SELECT goal_id FROM interaction_message WHERE id=?", (resolved_message_id,))
        if msg is None:
            raise ValueError(f"provenance 消息不存在: {resolved_message_id}")
        if msg[0] is None or msg[0] != row[2]:
            raise ValueError(f"provenance 消息 goal（{msg[0]}）与请求 goal（{row[2]}）不符，拒绝挂账")
        return row[0], row[1], row[2]

    def _validate_pending_request(self, request_id: int, resolved_message_id: int) -> tuple:
        """锁内、任何 attempt 清理前校验 pending 请求的完整不可变载荷。

        不信任仅因其已在 DB 就默认合法的 ``summary_md/items_json/request_hash``：旧版本、人工修复或
        损坏数据库都可能留下绕过 create_checked 的 pending 行。resolve/cancel 共用此闸；失败时只读
        DB，保留 pending 与现有 staging/final 供人工诊断。
        """
        row = self.daemon.query_one(
            "SELECT status,summary_md,items_json,request_hash,goal_id "
            "FROM interaction_request WHERE id=?", (request_id,))
        if row is None:
            raise ValueError(f"interaction_request 不存在: {request_id}")
        status, summary_md, items_json, request_hash, goal_id = row
        msg = self.daemon.query_one(
            "SELECT goal_id FROM interaction_message WHERE id=?", (resolved_message_id,))
        if msg is None:
            raise ValueError(f"provenance 消息不存在: {resolved_message_id}")
        if msg[0] is None or msg[0] != goal_id:
            raise ValueError(f"provenance 消息 goal（{msg[0]}）与请求 goal（{goal_id}）不符，拒绝挂账")
        if status != "pending":
            raise ValueError(f"request {request_id} 非 pending（{status}），不可迁终态")

        try:
            items = json.loads(items_json)
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError(
                f"request {request_id} pending request 损坏：items_json 不是合法 JSON") from error
        if not isinstance(items, list):
            raise ValueError(f"request {request_id} pending request 损坏：items 必须为数组")
        if not 1 <= len(items) <= MAX_REQUEST_ITEMS:
            raise ValueError(
                f"request {request_id} pending request 损坏：items 数须在 1..{MAX_REQUEST_ITEMS}")
        try:
            policy_limit = int(self.policy["max_items_per_request"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("interaction_request.max_items_per_request policy 损坏") from error
        if len(items) > policy_limit:
            raise ValueError(
                f"request {request_id} pending request 损坏：items 数 {len(items)} 超 policy 上限 "
                f"{policy_limit}")

        from jsonschema import ValidationError
        try:
            self.schema_set.validator("resource_request").validate(
                {"summary_md": summary_md, "items": items})
        except ValidationError as error:
            raise ValueError(
                f"request {request_id} pending request 损坏：resource_request schema 拒："
                f"{error.message}") from error

        canonical_items_json = json.dumps(items, ensure_ascii=False, sort_keys=True)
        canonical_hash = hashlib.sha256(canonical_items_json.encode()).hexdigest()
        if request_hash != canonical_hash:
            raise ValueError(
                f"request {request_id} pending request 损坏：request_hash 与 canonical items 不一致")
        return items, goal_id

    def resolve(self, *, request_id: int, uploads_dir: str, resolved_message_id: int) -> Dict[str, Any]:
        """uploads/<item_no>/ 逐文件复制并入 input/user_provided/<request_id>/<item_no>/ → 对**并入后
        字节**sha256 → resolution_json + resolved_* 一次性迁终态（trg_ireq_identity_frozen 只许这一跳）。
        条目目录缺失 = 用户未提供 → 该条记 unavailable（合法：部分提供也算 resolved，§4.6.8）。"""
        status, _items_json, _goal_id = self._check_provenance(request_id, resolved_message_id)
        if status != "pending":
            raise ValueError(f"request {request_id} 非 pending（{status}），不可 resolve")
        managed_paths = self._managed_paths(request_id)
        with _claim_file_request_operation(managed_paths[0]):
            # 初检到 claim 之间可能等待另一个进程；锁内完整校验才有权触碰 final/staging。
            items, goal_id = self._validate_pending_request(request_id, resolved_message_id)
            return self._resolve_claimed(
                request_id=request_id, uploads_dir=uploads_dir,
                resolved_message_id=resolved_message_id, items=items,
                goal_id=goal_id, managed_paths=managed_paths)

    def _resolve_claimed(self, *, request_id: int, uploads_dir: str, resolved_message_id: int,
                         items: List[Dict[str, Any]], goal_id: int,
                         managed_paths: tuple) -> Dict[str, Any]:
        """claim 锁内实体；调用方已完整校验 DB pending 请求且尚未清理 attempt。"""
        up = Path(uploads_dir)
        managed_root, stage_root, dest_root = managed_paths
        # pending 请求的这些目录都不是权威状态：每次 attempt 从空 staging/final 重建，绝不继承半复制陈货。
        _remove_attempt_durable(managed_root, stage_root, dest_root)
        existing_managed_bytes = _managed_published_bytes(managed_root)
        existing_goal_assets = _count_resolved_assets_for_goal(self.daemon, goal_id)
        resolution: List[Dict[str, Any]] = []
        asset_manifest: List[Dict[str, Any]] = []
        total_bytes = 0
        preview_bytes = 0
        file_count = 0
        populated_items: List[int] = []
        traversal_budget = _UploadTraversalBudget()
        try:
            stage_root.mkdir(mode=0o700, parents=True)
            _fsync_directory(stage_root.parent)
            with _open_upload_root(up) as upload_root_fd:
                for i, _item in enumerate(items, start=1):
                    src = up / str(i)
                    if upload_root_fd is None:
                        resolution.append({"unavailable": "用户未提供该条目文件"})
                        continue
                    with _regular_files_no_symlink(
                            upload_root_fd, str(i), src,
                            max_files=_MAX_MANAGED_FILES_PER_GOAL - file_count,
                            traversal_budget=traversal_budget) as files:
                        if not files:
                            resolution.append({"unavailable": "用户未提供该条目文件"})
                            continue
                        provided = []
                        for asset_no, f in enumerate(files, start=1):
                            file_count += 1
                            rel = f.relative_path
                            # 外部文件名不进入托管路径/ref（文件名可含换行/```/引号，直接进 prompt 会注入）。
                            # 请求条目仍保留 expected_files；真实原相对名只留 DB 审计字段，不参与路径解析。
                            safe_name = f"asset-{asset_no}"
                            stage_dest = stage_root / str(i) / safe_name
                            preview_limit = min(
                                _MAX_ASSET_PREVIEW_BYTES,
                                max(0, _MAX_REQUEST_PREVIEW_BYTES - preview_bytes))
                            size, digest, preview, preview_size, preview_truncated = _copy_hash_regular(
                                f, stage_dest, source_root=src,
                                remaining_bytes=(self.max_managed_bytes - existing_managed_bytes
                                                 - total_bytes),
                                preview_limit_bytes=preview_limit)
                            total_bytes += size
                            preview_bytes += preview_size
                            final_dest = dest_root / str(i) / safe_name
                            opaque_ref = f"user-file-request:r{request_id}:item:{i}:asset:{asset_no}"
                            asset = {
                                "path": str(final_dest),
                                "ref": opaque_ref,
                                "original_relpath": rel.as_posix(),
                                "hash": digest,
                                "hash_alg": "sha256",
                                "size_bytes": size,
                            }
                            if preview is not None:
                                asset["preview"] = preview
                                if preview_truncated:
                                    asset["preview_truncated"] = True
                            provided.append(asset)
                            asset_manifest.append({
                                "ref": opaque_ref,
                                "relative_path": f"{i}/{safe_name}",
                                "sha256": digest,
                                "size_bytes": size,
                            })
                        _fsync_directory(stage_root / str(i))
                        populated_items.append(i)
                        resolution.append({"provided": provided})
            if existing_goal_assets + file_count > _MAX_MANAGED_FILES_PER_GOAL:
                raise ValueError(
                    f"goal {goal_id} resolved 资产累计将达 {existing_goal_assets + file_count}，"
                    f"超过 ContextPack 上限 {_MAX_MANAGED_FILES_PER_GOAL}；请减少本次文件数")
            # staging 与 final 同属 managed_root，rename 在同一文件系统；发布完整树后才允许 DB 进终态。
            # 全部 unavailable 时没有资产可发布，保持 final 不存在（避免空目录冒充已提供输入）。
            if file_count:
                manifest_path = stage_root / "assets.manifest.json"
                manifest_bytes = (json.dumps(
                    {"version": 1, "request_id": request_id, "assets": asset_manifest},
                    ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
                if existing_managed_bytes + total_bytes + len(manifest_bytes) > self.max_managed_bytes:
                    raise ValueError("用户文件总字节数超过 resources.disk_quota_gb（含既有托管资产与 manifest）")
                with manifest_path.open("xb") as manifest_file:
                    manifest_file.write(manifest_bytes)
                    manifest_file.flush()
                    os.fsync(manifest_file.fileno())
                # stage 内子目录和根先持久化，再跨 .staging/ 与 managed/ 两个父目录原子发布。
                _fsync_directory(stage_root)
                _fsync_directory(stage_root.parent)
                os.replace(str(stage_root), str(dest_root))
                _publish_read_only(dest_root)
                for item_no in populated_items:
                    _fsync_directory(dest_root / str(item_no))
                _fsync_directory(dest_root)
                _fsync_directory(stage_root.parent)
                _fsync_directory(managed_root)
            else:
                _remove_attempt_durable(managed_root, stage_root, dest_root)
            # DB 只有在从 work_root 到已发布树的完整目录创建链都持久化后才可迁 resolved。
            _fsync_managed_ancestors(managed_root)
        except BaseException as primary:
            _rollback_attempt_best_effort(managed_root, stage_root, dest_root, primary)
            raise
        resolution_json = json.dumps(resolution, ensure_ascii=False)
        try:
            with self.daemon.transaction() as conn:
                n = conn.execute(
                    "UPDATE interaction_request SET status='resolved', resolution_json=?, "
                    "resolved_at=CURRENT_TIMESTAMP, resolved_message_id=? WHERE id=? AND status='pending'",
                    (resolution_json, resolved_message_id, request_id)).rowcount
                if n != 1:
                    raise RuntimeError(f"request {request_id} resolve 竞态：迁移失败")
        except BaseException as db_error:
            # COMMIT 报错并不总能说明事务未提交（例如连接在确认提交后才丢失响应）。以重新读取的权威
            # 终态裁决：只有本次 resolution/provenance **逐字段精确一致**才算成功；明确读到 pending、
            # 别的终态时撤回 final。
            try:
                authoritative = _read_committed_resolution_state(self.daemon, request_id)
            except BaseException:
                # 回读失败无法区分「未提交」与「提交成功后连接损坏」。保留已 fsync final 作为 quarantine
                # 并抛原事务异常；消费者仍须经 DB resolved 授权，后续 resolve/cancel 会清理重建。
                raise db_error from None
            if authoritative == ("resolved", resolution_json, resolved_message_id):
                return {"request_id": request_id, "resolution": resolution}
            _rollback_attempt_best_effort(managed_root, stage_root, dest_root, db_error)
            raise
        return {"request_id": request_id, "resolution": resolution}

    def cancel(self, *, request_id: int, reason: str, resolved_message_id: int) -> None:
        """用户取消（同 provenance：入站消息回指，goal 校验同 resolve）。"""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("cancel reason 须为非空字符串")
        if any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in reason):
            raise ValueError("cancel reason 不得含 C0/DEL 控制字符")
        if len(reason) > MAX_CANCEL_REASON_CHARS:
            raise ValueError(f"cancel reason 超过 {MAX_CANCEL_REASON_CHARS} 字符上限")
        status, _, _goal_id = self._check_provenance(request_id, resolved_message_id)
        if status != "pending":
            raise ValueError(f"request {request_id} 非 pending（{status}），不可 cancel")
        managed_paths = self._managed_paths(request_id)
        with _claim_file_request_operation(managed_paths[0]):
            self._validate_pending_request(request_id, resolved_message_id)
            # resolve 失败可能留下旧版本实现的 final 或本版 staging；claim 后才可清非权威资产。
            managed_root, stage_root, dest_root = managed_paths
            _remove_attempt_durable(managed_root, stage_root, dest_root)
            with self.daemon.transaction() as conn:
                row = conn.execute("SELECT status FROM interaction_request WHERE id=?", (request_id,)).fetchone()
                if row[0] != "pending":
                    raise ValueError(f"request {request_id} 非 pending（{row[0]}），不可 cancel")
                n = conn.execute(
                    "UPDATE interaction_request SET status='cancelled', resolution_json=?, "
                    "resolved_at=CURRENT_TIMESTAMP, resolved_message_id=? WHERE id=? AND status='pending'",
                    (json.dumps({"cancelled": True, "reason": reason}, ensure_ascii=False),
                     resolved_message_id, request_id)).rowcount
                if n != 1:            # 兜底同 resolve（同事务已校验，理论不可达）
                    raise RuntimeError(f"request {request_id} cancel 竞态：迁移失败")


class FileRequestNotifier:
    """文件请求 3 事件（§4.6.6）：request_pending / reminder（分档）/ resolved（含 cancelled）。
    now_ts 由调用方注入（unix 秒）——本模块不调 wall-clock（确定性可测；生产由驱动循环传 time.time()）。"""

    def __init__(self, daemon: WriteDaemon, outbox: Outbox, remind_interval_h: float):
        self.daemon = daemon
        self.outbox = outbox
        self.remind_interval_s = remind_interval_h * 3600

    @staticmethod
    def _safe_resolution_summary(raw: Optional[str]) -> Optional[Dict[str, Any]]:
        """Project completion counts only; never export file paths/previews/user data."""
        if raw is None:
            return None
        value = _load_state_json(raw)
        if isinstance(value, dict) and value.get("cancelled") is True:
            return {"cancelled": True, "item_count": 0,
                    "provided_file_count": 0, "unavailable_item_count": 0}
        if not isinstance(value, list):
            raise ValueError("interaction_request resolution_json 结构损坏")
        provided = 0
        unavailable = 0
        for item in value:
            if not isinstance(item, dict):
                raise ValueError("interaction_request resolution item 结构损坏")
            assets = item.get("provided")
            if isinstance(assets, list):
                provided += len(assets)
            if "unavailable" in item:
                unavailable += 1
        return {"cancelled": False, "item_count": len(value),
                "provided_file_count": provided, "unavailable_item_count": unavailable}

    def scan(self, now_ts: float) -> List[str]:
        new_keys: List[str] = []
        rows = self.daemon.query(
            "SELECT id, status, summary_md, resolution_json, strftime('%s', created_at) "
            "FROM interaction_request ORDER BY id")
        for rid, status, summary, resolution, created_ts in rows:
            base = {"request_id": rid, "summary_md": summary}
            if self.outbox.emit(f"filereq:{rid}:pending", "file_request_pending", base):
                new_keys.append(f"filereq:{rid}:pending")
            if status == "pending":
                elapsed = now_ts - float(created_ts)
                tier = int(elapsed // self.remind_interval_s)     # 每 interval 一档；同档幂等
                # 只发**当前档**、不补历史档（外审 NIT 权衡已定）：停机跨多档后一口气补发一串过期提醒
                # 是骚扰不是信息——提醒语义=「现在还在等」，当前档已完整表达等待时长（waited_intervals）
                if tier >= 1 and self.outbox.emit(f"filereq:{rid}:reminder:{tier}", "file_request_reminder",
                                                  {**base, "waited_intervals": tier}):
                    new_keys.append(f"filereq:{rid}:reminder:{tier}")
            else:
                # v2 removes resolution paths/previews from the wire payload.
                # Versioning the key avoids same-key/different-body collision
                # when upgrading a work-root that already queued legacy v1.
                if self.outbox.emit(f"filereq:{rid}:resolved:v2", "file_request_resolved",
                                    {**base, "status": status,
                                     "resolution_summary": self._safe_resolution_summary(resolution)}):
                    new_keys.append(f"filereq:{rid}:resolved:v2")
        return new_keys


# ------------------------------------------------------ advancer 前置检查 --

def _due_timings(cyc) -> List[str]:
    """到期时机结构判定：immediate 恒到期；stage_boundary 每格即边界（precheck 恰在格间跑）；
    reasoning_start 仅当**下一格**将进 reasoning——reasoning-only 轮（bootstrap/decompose）每格即
    reasoning；attack 轮 cycle.status 是"最后已提交阶段"游标（attack_stages.advance_stage），
    status='bundle' 的下一格才是 reasoning（早一格消费即违约）。cyc=None（开轮前）只消费前两类。"""
    due = ["immediate", "stage_boundary"]
    if cyc is not None and (
            cyc.route in ("bootstrap", "decompose", "goal_amend") or
            (cyc.route == "attack" and cyc.status == "bundle")):
        due.append("reasoning_start")
    return due


def make_advancer_precheck(console: Console, daemon: WriteDaemon) -> Callable:
    """§4.4.1 前置检查（SqliteAdvancer.precheck 装配件）：先消费到期 directive（_due_timings）、再查
    阻断。返回 callable(cyc_or_none) -> Optional[str]（None=放行；str=拒因，Advancer 停止推进）。"""
    def precheck(cyc=None) -> Optional[str]:
        console.supersede_stale_goal_amends()
        for timing in _due_timings(cyc):
            for did in console.pending_directives(timing):
                kind_row = daemon.query_one("SELECT kind FROM directive WHERE id=?", (did,))
                kind = kind_row[0] if kind_row is not None else None
                # A goal amendment arriving during an attack/decompose cycle is
                # intentionally deferred: the current cycle closes under its
                # old goal version, then route priority opens a dedicated amend
                # cycle.  Consuming it here would expose it to the wrong pack.
                if kind == "goal_amend" and (cyc is None or cyc.route != "goal_amend"):
                    continue
                # goal_amend is a dedicated control round.  Its bound hard
                # amendment must not sit behind up to 128 older notes/priority
                # controls and then be terminally rejected by the per-pack cap.
                # Defer every companion reasoning_start directive to the next
                # ordinary reasoning boundary under the newly committed goal.
                # Immediate/stage-boundary controls (pause/abort/budget) remain
                # unaffected and are still handled by their earlier timing.
                if (timing == "reasoning_start" and cyc is not None
                        and cyc.route == "goal_amend" and kind != "goal_amend"):
                    continue
                cycle_id = cyc.cycle_id if cyc is not None else None
                try:
                    console.consume_directive(directive_id=did, cycle_id=cycle_id)
                except DirectiveApplicationError as error:
                    console.reject_unapplicable_directive(
                        directive_id=did, reason=str(error), cycle_id=cycle_id)
        if console.has_blocking_pause():
            return "pause 指令生效中（等待 resume）"
        pending = daemon.query_one("SELECT id FROM interaction_request WHERE status='pending' LIMIT 1")
        if pending:
            return f"文件请求 #{pending[0]} 等待用户提供（全局等待 v1：不发新研究执行）"
        return None

    return precheck
