"""console_server —— 人类控制台数据面（步⑨ CP9.1）：把真运行库投影成**控制台前端消费形状**的 JSON
（CP9.2 前端据此由原型 v2 改造换数据源），并收人工入站消息到 spool 文件。

**形状说明**：本模块产的是 server-canonical 形状（真表投影统一嵌 `payload["tables"][<表名>]`、派生对象
[status_card/live/notification/policy/ledger_by_cycle/fs] 平铺顶层）——**非**原型 mock 的顶层 `DB.<表名>`
形状；CP9.2 前端 loader 负责这层适配（换数据源手术），故本模块不必逐字对齐原型 mock 的键路径。

**单写纪律铁律（§6.6）**：本服务是**独立进程**，对研究库**只读**（`mode=ro` 物理只读，SQLite 拒一切写；
见 _open_ro——控制台是人类全量观测面须读全表+PRAGMA，故用 mode=ro 而非 grounding 应答器的裁剪连接），
入站消息只**追加写 inbox spool 文件**（非 DB）——run 进程在 precheck 边界 ingest 该 spool 走 M5 既有链
（InteractionIngest→Console→Mediator）。控制台**永不写 DB**，故不破坏「WriteDaemon 单写者」。

**为什么动态投影**：DDL 是冻结件（36 表三重锁）。本模块用 `PRAGMA table_info` 取真列名投影每张表为
list[dict]——不硬编码列名，DDL 不变则形状稳定；前端（原型派生）按需读字段、缺的显示空。派生对象
（status_card / live / notification / policy / FS 树）单独组装。

**零新依赖**：stdlib http.server + sqlite3 + json + yaml（已依赖）。
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import math
import os
import pwd
import re
import sqlite3
import stat
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Union
from urllib.parse import parse_qs, quote, urlparse

import yaml

from .ids import parse_positive_sqlite_int
from .console import directive_action_text
from .console_spool import (CAPABILITY_NAME, ConsoleSpool, normalize_upload_ref,
                            open_directory_path, read_regular_file_beneath,
                            stat_regular_file_beneath)
from .instance_lease import read_instance_status
from .narrator_session import public_narrator_session_status
from .database import journal_mode_for_path
from .process_supervisor import read_receipt
from .dataset_preflight import DatasetPreflightError
from .qualification_profiles import QualificationProfileRegistry
from .local_sources import (
    LocalSourceChangedError, LocalSourceConflictError,
    LocalSourceCorruptError, LocalSourceError, LocalSourceRegistry,
)
from .quest_drafts import (
    DraftConflictError, DraftCorruptError, QuestDraftRegistry,
)
from .quest_process_manager import (
    QuestProcessManager, QuestProcessManagerError,
    QuestProcessUnavailableError,
)
from .quest_registry import QuestConflictError, QuestCorruptError, QuestRegistry
from .web_quest_service import (
    WebQuestConflictError, WebQuestNotReadyError, WebQuestService,
    WebQuestRetryableError, WebQuestServiceError,
)


_MAX_HTTP_BODY_BYTES = 64 * 1024
_MAX_DRAFT_HTTP_BODY_BYTES = 512 * 1024
_MAX_UPLOAD_CHUNK_BYTES = 8 * 1024 * 1024
_MAX_MESSAGE_CHARS = 20_000
_MAX_REASON_CHARS = 2_000
_MAX_FILE_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_STATIC_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_JSON_RESPONSE_BYTES = 16 * 1024 * 1024
_MAX_NOTIFICATION_BYTES = 4 * 1024 * 1024
_MAX_NOTIFICATION_RECORDS = 1_000
_MAX_DELIVERY_STATE_BYTES = 1024 * 1024
_MAX_STATUS_CARD_BYTES = 1024 * 1024
_MAX_POLICY_BYTES = 1024 * 1024
_MAX_DB_TEXT_CHARS = 64 * 1024
_MAX_DB_PROJECTION_BYTES = 6 * 1024 * 1024
_MAX_DB_TABLE_BYTES = 512 * 1024
_MAX_LEDGER_CYCLES = 500
_MAX_LEDGER_RECORDS = 10_000
_MAX_RUNNER_OUTPUT_CALLS = 32
_MAX_RUNNER_TRANSCRIPT_BYTES = 512 * 1024
_MAX_RUNNER_LIVE_CAPTURE_BYTES = 128 * 1024
_MAX_RUNNER_OUTPUT_CHARS = 24 * 1024
_MAX_RUNNER_LIVE_ITEMS = 18
_MAX_RUNNER_LIVE_ITEM_CHARS = 6 * 1024
_MAX_RUNNER_ACTIVITY_ITEMS = 28
_MAX_RUNNER_ACTIVITY_TEXT_CHARS = 720
_MAX_TRAINING_LIVE_LOGS = 6
_MAX_TRAINING_LOG_TAIL_BYTES = 64 * 1024
_MAX_TRAINING_LOG_SCAN_DIRS = 256
_MAX_TRAINING_LOG_SCAN_FILES = 4096
_TRAINING_LIVE_CONTRACT_VERSION = 1
_MAX_POLICY_NODES = 10_000
_MAX_POLICY_DEPTH = 32
_MAX_POLICY_STRING_CHARS = 64 * 1024
_MAX_FS_NODES = 2_000
_MAX_FS_ENTRIES_PER_DIRECTORY = 200
_HTTP_SOCKET_TIMEOUT_S = 10.0
_UPLOAD_SOCKET_TIMEOUT_S = 120.0
_MAX_HTTP_WORKERS = 16
_FS_DIRECTORY_FLAGS = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                       | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
                       | getattr(os, "O_NONBLOCK", 0))
_PUBLIC_WORK_DIRECTORIES = frozenset({
    "baselines", "cycles", "evaluations", "input", "questions", "views",
})
_PUBLIC_WORK_FILES = frozenset({"goal_brief.md"})
_PRIVATE_WORK_INPUT_FILES = frozenset({"local-sources.json"})

# 控制台投影的表清单（真 DDL 表名；import/license 等当前无数据则投影空数组，前端照渲染）。
_PROJECT_TABLES = [
    "goal", "question", "question_dep", "cycle", "baseline", "baseline_tag", "variant", "run",
    "protocol", "metric_def", "protocol_metric", "evaluation", "evaluation_attempt", "metric_result",
    "answer", "answer_applicability", "evidence", "decision", "directive", "build_target",
    "build_target_required_metric", "checkpoint", "execution_log", "execution_observation",
    "external_candidate", "license_review", "external_import",
    "interaction_message", "interaction_classification", "interaction_reply", "interaction_request",
    "ledger", "runner_call", "phase_commit",
]
_CONTROL_TABLES = (
    "directive", "interaction_request", "interaction_message",
    "interaction_classification", "interaction_reply",
)


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """Small fixed-concurrency HTTP surface; unauthenticated slow clients cannot spawn unbounded threads."""

    daemon_threads = True
    request_queue_size = 32

    def __init__(self, *args, max_workers: int = _MAX_HTTP_WORKERS, **kwargs):
        self._worker_slots = threading.BoundedSemaphore(max_workers)
        self._close_callbacks = []
        self._callbacks_closed = False
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._worker_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._worker_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._worker_slots.release()

    def add_close_callback(self, callback) -> None:  # noqa: ANN001 - small lifecycle hook
        self._close_callbacks.append(callback)

    def server_close(self):
        errors = []
        if not self._callbacks_closed:
            self._callbacks_closed = True
            for callback in reversed(self._close_callbacks):
                try:
                    callback()
                except BaseException as error:  # close every owned capability before surfacing one
                    errors.append(error)
        super().server_close()
        if errors:
            raise RuntimeError(
                f"console server close 失败（{len(errors)} 个 owned service）") from errors[0]


class JsonResponseTooLarge(ValueError):
    """Incremental JSON encoding crossed the configured response budget."""


class SharedSQLiteReaderUnavailable(RuntimeError):
    """The shared database cannot prove this request is on a safe reader host."""


def _bounded_json_bytes(value: Any, *, max_bytes: int = _MAX_JSON_RESPONSE_BYTES) -> bytes:
    """Encode without first materializing an unbounded string; reject non-finite JSON numbers."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        raise ValueError("JSON 响应预算须为非负整数")
    body = bytearray()
    encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False)
    for piece in encoder.iterencode(value):
        encoded = piece.encode("utf-8")
        if len(body) + len(encoded) > max_bytes:
            raise JsonResponseTooLarge(f"JSON 响应超过 {max_bytes} 字节安全上限")
        body.extend(encoded)
    return bytes(body)


def _open_ro(db_path: str, *, work_root: Union[str, Path]) -> sqlite3.Connection:
    """控制台专用只读连接：**mode=ro**（物理只读，写操作被 SQLite 拒——单写者纪律的硬保证）。
    不用 mediator.open_responder_read_conn（其 authorizer 为 grounding 应答器裁剪、连 PRAGMA/观测表都拒），
    因控制台是**人类全量观测面**、须读得到所有表 + PRAGMA 取列名；mode=ro 已足够保证零写。

    共享文件系统的 rollback journal 额外做请求级准入检查：有 owner 持锁时，
    必须由 lease 状态机械证明它是本次 boot 的 fresh active owner。这不是接管 fence；
    部署仍必须在跨节点接管前停止/fence 旧节点及其独立控制台。"""
    if journal_mode_for_path(db_path) == "delete":
        instance = read_instance_status(work_root)
        local_owner = instance.get("local_active_owner") is True
        proven_inactive = (
            instance.get("status") == "inactive"
            and instance.get("lock_held") is False)
        if not (local_owner or proven_inactive):
            raise SharedSQLiteReaderUnavailable(
                "共享文件系统 SQLite 准入拒绝：未证实本机 active owner")
    conn = sqlite3.connect(f"file:{quote(db_path)}?mode=ro", uri=True)
    conn.isolation_level = None
    return conn


def _canonical_positive_id(value: Any, *, label: str) -> int:
    """解析来自 JSON/HTTP 的 SQLite 正整数，并拒绝布尔、浮点、前导零和越界值。"""
    if isinstance(value, bool):
        raise ValueError(f"{label} 须为正整数")
    if isinstance(value, int):
        digits = str(value)
    elif isinstance(value, str):
        digits = value.strip()
    else:
        raise ValueError(f"{label} 须为正整数")
    try:
        parsed = parse_positive_sqlite_int(digits, label=label)
    except ValueError:
        raise ValueError(f"{label} 须为 SQLite 范围内的正整数") from None
    if digits != str(parsed):
        raise ValueError(f"{label} 须为规范正整数（不得含前导零）")
    return parsed


def _stored_idempotency_key(client_key: Optional[str]) -> Optional[str]:
    """Map an HTTP client nonce to the DB/spool namespace; ``None`` is for direct in-process callers only."""
    if client_key is None:
        return None
    if (not isinstance(client_key, str) or len(client_key) != 32
            or any(ch not in "0123456789abcdef" for ch in client_key)):
        raise ValueError("Idempotency-Key 须为 128-bit 小写 hex（32 字符）")
    return "console-" + client_key


def _console_conversation_id(value: Optional[str]) -> Optional[str]:
    """A browser-profile-scoped 128-bit id; omitted only for legacy/direct callers."""
    if value is None:
        return None
    if (not isinstance(value, str) or len(value) != 32
            or any(ch not in "0123456789abcdef" for ch in value)):
        raise ValueError("conversation_id 须为 128-bit 小写 hex（32 字符）")
    return value


def _is_loopback_host(host: str) -> bool:
    """控制台当前只支持本机监听；远程使用应走 SSH tunnel，而不是暴露无 TLS 的控制面。"""
    candidate = str(host or "").strip().rstrip(".").lower()
    if candidate == "localhost":
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def _authenticated_console_url(host: str, port: int, token: str) -> str:
    """Build the local browser bootstrap URL without putting the bearer in a query.

    The fragment is consumed by the console page and removed from the address
    bar before any API request.  Keeping this in one helper also prevents the
    browser-open and terminal-recovery paths from drifting apart.
    """
    display_host = str(host).strip()
    if ":" in display_host and not display_host.startswith("["):
        display_host = f"[{display_host}]"
    return f"http://{display_host}:{int(port)}/#token={token}"


def _host_header_is_loopback(value: Optional[str]) -> bool:
    """拒绝 DNS rebinding 的非本机 Host；端口由 ``urlparse`` 安全剥离。"""
    if not value:
        return False
    try:
        hostname = urlparse(f"//{value}").hostname
    except ValueError:
        return False
    return bool(hostname) and _is_loopback_host(hostname)


def resolve_upload_ref(source_ref: Any, *, work_root: Union[str, Path],
                       system_root: Union[str, Path]) -> str:
    """HTTP-time upload syntax normalization; returns no pathname authority.

    HTTP must not require the directory to still exist on a replay: the first
    resolve may already have committed and the operator may then remove the
    upload tree before a lost ACK is retried.  The run process performs the
    authoritative ``open_pinned_upload_ref`` only after checking terminal
    provenance; a same-key replay therefore converges without reopening input.
    """
    del work_root, system_root
    normalized, _virtual_root, _tail = normalize_upload_ref(source_ref)
    return normalized


# 控制台是最近态视图而非数据库导出器：每张表统一限制行数，单个 TEXT 也在 SQL 投影时截断。
_ROW_CAP = 500
_CAPPED = frozenset(_PROJECT_TABLES)


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _rows(conn: sqlite3.Connection, table: str, *,
          max_serialized_bytes: Optional[int] = None) -> List[Dict[str, Any]]:
    """动态列投影：PRAGMA 取真列名 → SELECT → list[dict]。

    行数和单列 TEXT 在 SQL 边界受限；HTTP 组装还传入精确的 JSON 字节预算并逐行 fetch，避免先把
    一个理论上数百 MiB 的表 materialize 再由最终响应上限兜底。直接测试/内部调用省略预算时保留
    完整 ``_ROW_CAP`` 语义。
    """
    if max_serialized_bytes is not None and (
            isinstance(max_serialized_bytes, bool) or not isinstance(max_serialized_bytes, int)
            or max_serialized_bytes < 0):
        raise ValueError("DB 投影字节预算须为非负整数")
    try:
        table_info = conn.execute(f"PRAGMA table_info({table})").fetchall()
        cols = [r[1] for r in table_info]
    except sqlite3.DatabaseError:
        return []
    if not cols:
        return []
    # SQLite TEXT 可远大于最终 JSON cap；在 DB→Python 边界先裁剪，BLOB 只显示占位。
    projected = []
    for col in cols:
        quoted = _quote_identifier(col)
        projected.append(
            f"CASE WHEN typeof({quoted})='text' THEN substr({quoted},1,{_MAX_DB_TEXT_CHARS}) "
            f"WHEN typeof({quoted})='blob' THEN '<blob omitted>' ELSE {quoted} END")
    sql = f"SELECT {','.join(projected)} FROM {_quote_identifier(table)}"
    if table in ("directive", "interaction_request"):
        pending = conn.execute(
            f"SELECT COUNT(*) FROM {_quote_identifier(table)} WHERE status='pending'").fetchone()[0]
        if pending > _ROW_CAP:
            raise ValueError(
                f"{table} actionable rows {pending} 超过控制台上限 {_ROW_CAP}，拒绝静默截断")
        identity = "id" if "id" in cols else cols[0]
        sql += (f" ORDER BY CASE WHEN status='pending' THEN 0 ELSE 1 END, "
                f"{_quote_identifier(identity)} DESC LIMIT {_ROW_CAP}")
    elif table in _CAPPED and "id" in cols:
        sql += f" ORDER BY id DESC LIMIT {_ROW_CAP}"
    else:
        primary_key = [r[1] for r in sorted(table_info, key=lambda r: r[5]) if r[5]]
        if primary_key:
            sql += " ORDER BY " + ",".join(
                f"{_quote_identifier(column)} DESC" for column in primary_key)
        sql += f" LIMIT {_ROW_CAP}"
    cursor = conn.execute(sql)
    rows: List[Dict[str, Any]] = []
    used = 2                         # JSON list 的 []；精确预算采用 compact ensure_ascii=False 口径
    for raw_row in cursor:
        row = dict(zip(cols, raw_row))
        if max_serialized_bytes is None:
            rows.append(row)
            continue
        encoded = json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        separator = 1 if rows else 0
        if used + separator + len(encoded) <= max_serialized_bytes:
            rows.append(row)
            used += separator + len(encoded)
            continue
        # 保留最新行的身份/形状，但不让一个多列巨型 row 吞掉整张表乃至整个 /api/db。
        placeholder = {
            key: ("<console projection truncated>" if isinstance(value, str) else value)
            for key, value in row.items()
        }
        encoded = json.dumps(
            placeholder, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if used + separator + len(encoded) <= max_serialized_bytes:
            rows.append(placeholder)
        break                         # 最新优先；达到预算后不继续扫描更老行
    if max_serialized_bytes is not None and table in ("directive", "interaction_request"):
        projected_pending = sum(row.get("status") == "pending" for row in rows)
        if projected_pending != pending:
            raise ValueError(
                f"{table} actionable rows 无法在投影预算内完整展示，拒绝解锁不完整控制面")
    return rows


def _load_status_card(work_root: Path) -> Optional[Dict[str, Any]]:
    """读发布产物 <work>/state/status_card.json（advancer 阶段边界原子发布；无=尚未发布过）。"""
    try:
        raw = read_regular_file_beneath(
            work_root, "state/status_card.json", max_bytes=_MAX_STATUS_CARD_BYTES)
        value = json.loads(raw.decode("utf-8"))
        return value if isinstance(value, dict) else None
    except (UnicodeDecodeError, json.JSONDecodeError, OSError, ValueError, RuntimeError):
        return None


def _strict_projection_json(raw: str) -> Any:
    def unique_object(pairs):  # noqa: ANN001
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"JSON key 重复: {key}")
            value[key] = item
        return value

    return json.loads(
        raw, object_pairs_hook=unique_object,
        parse_constant=lambda token: (_ for _ in ()).throw(
            ValueError(f"JSON 非有限数字: {token}")))


def _valid_delivery_receipt(value: Any) -> bool:
    common = bool(
        isinstance(value, dict)
        and isinstance(value.get("channel"), str)
        and re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", value["channel"]) is not None
        and isinstance(value.get("event_key"), str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:._/-]{0,255}", value["event_key"]) is not None
        and not isinstance(value.get("attempt_count"), bool)
        and isinstance(value.get("attempt_count"), int))
    remote_ack = bool(
        common and value.get("version") == 1
        and set(value) == {"version", "channel", "event_key", "accepted_at",
                           "attempt_count", "delivery_id", "ack_hash"}
        and not isinstance(value.get("accepted_at"), bool)
        and isinstance(value.get("accepted_at"), (int, float))
        and math.isfinite(float(value["accepted_at"]))
        and value["attempt_count"] >= 1
        and (value.get("delivery_id") is None
             or (isinstance(value.get("delivery_id"), str) and len(value["delivery_id"]) <= 256))
        and isinstance(value.get("ack_hash"), str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", value["ack_hash"]) is not None)
    suppressed = bool(
        common and value.get("version") == 2
        and set(value) == {"version", "channel", "event_key", "completed_at",
                           "attempt_count", "disposition", "reason_hash"}
        and not isinstance(value.get("completed_at"), bool)
        and isinstance(value.get("completed_at"), (int, float))
        and math.isfinite(float(value["completed_at"]))
        and value["attempt_count"] >= 0
        and value.get("disposition") == "suppressed_unsafe_route"
        and isinstance(value.get("reason_hash"), str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", value["reason_hash"]) is not None)
    return remote_ack or suppressed


def _valid_retry_document(value: Any) -> bool:
    if (not isinstance(value, dict) or set(value) != {"version", "events"}
            or value.get("version") != 1 or not isinstance(value.get("events"), dict)):
        return False
    required = {"channel", "event_key", "attempt_count", "first_failed_at",
                "last_attempt_at", "next_attempt_at", "last_error_kind", "last_error"}
    for identity, entry in value["events"].items():
        if not isinstance(entry, dict) or set(entry) != required:
            return False
        channel, key = entry.get("channel"), entry.get("event_key")
        if (not isinstance(identity, str) or not isinstance(channel, str)
                or re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", channel) is None
                or not isinstance(key, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:._/-]{0,255}", key) is None
                or identity != f"{channel}\x1f{key}"
                or isinstance(entry.get("attempt_count"), bool)
                or not isinstance(entry.get("attempt_count"), int)
                or not 1 <= entry["attempt_count"] <= 2 ** 31 - 1
                or any(isinstance(entry.get(name), bool)
                       or not isinstance(entry.get(name), (int, float))
                       or not math.isfinite(float(entry[name]))
                       for name in ("first_failed_at", "last_attempt_at", "next_attempt_at"))
                or not isinstance(entry.get("last_error_kind"), str)
                or not isinstance(entry.get("last_error"), str)):
            return False
    return True


def _valid_outbox_event(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) not in (
            {"event_key", "kind", "payload"},
            {"event_key", "kind", "payload", "channel"}):
        return False
    channel = value.get("channel")
    return bool(
        isinstance(value.get("event_key"), str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:._/-]{0,255}", value["event_key"]) is not None
        and isinstance(value.get("kind"), str) and 1 <= len(value["kind"]) <= 128
        and isinstance(value.get("payload"), dict)
        and (channel is None or (isinstance(channel, str)
             and re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", channel) is not None)))


def _notifications(work_root: Path) -> List[Dict[str, Any]]:
    """读 outbox 事件队列（notify.Outbox 落的每行 JSON；committed=换行终止，撕裂尾行忽略）。"""
    authority_errors: List[str] = []
    try:
        raw = read_regular_file_beneath(
            work_root, "state/outbox.jsonl", max_bytes=_MAX_NOTIFICATION_BYTES, tail=True)
    except FileNotFoundError:
        return []
    except (OSError, ValueError, RuntimeError) as error:
        return [{
            "event_key": "transport-authority-corrupt",
            "kind": "transport_authority_corrupt",
            "payload": {"errors": [f"outbox_corrupt:{type(error).__name__}"],
                        "message": "投递权威状态损坏；不得把通知视为已交付"},
            "deliveries": [{"status": "authority_corrupt", "channel": "transport",
                            "last_error_kind": f"outbox_corrupt:{type(error).__name__}"}],
        }]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        text = ""
        authority_errors.append(f"outbox_corrupt:{type(error).__name__}")
    # JSON 字符串可合法包含 U+2028/U+2029；``splitlines`` 会把它们误当物理记录边界。spool 协议只认 LF。
    # 只保留最近固定条数；``split`` 百万条短行会在 4 MiB 字节上限内仍制造百万 Python 对象。
    parts = text.rsplit("\n", _MAX_NOTIFICATION_RECORDS + 1)
    lines = parts[:-1]                     # 最后一段无论空/非空都不是另一条已提交记录
    if len(lines) > _MAX_NOTIFICATION_RECORDS:
        lines = lines[-_MAX_NOTIFICATION_RECORDS:]
    out = []                               # JSON 前缀也不当已发事件（committed=换行终止，同 outbox 纪律，codex SHOULD）
    event_keys = set()
    tail_may_start_mid_record = len(raw) >= _MAX_NOTIFICATION_BYTES
    for line_index, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        try:
            value = _strict_projection_json(line)
            if not _valid_outbox_event(value) or value["event_key"] in event_keys:
                raise ValueError("outbox event schema/identity 损坏")
            event_keys.add(value["event_key"])
            out.append(value)
        except (json.JSONDecodeError, ValueError) as error:
            if line_index == 0 and tail_may_start_mid_record:
                continue
            authority_errors.append(f"outbox_corrupt:{type(error).__name__}")
    delivery: Dict[str, List[Dict[str, Any]]] = {}
    try:
        receipt_raw = read_regular_file_beneath(
            work_root, "state/delivery_receipts.jsonl",
            max_bytes=_MAX_NOTIFICATION_BYTES, tail=True).decode("utf-8", errors="replace")
        receipt_parts = receipt_raw.rsplit("\n", _MAX_NOTIFICATION_RECORDS + 1)
        receipt_identities = set()
        receipt_lines = receipt_parts[:-1][- _MAX_NOTIFICATION_RECORDS:]
        receipt_tail_partial = len(receipt_raw.encode("utf-8")) >= _MAX_NOTIFICATION_BYTES
        for line_index, line in enumerate(receipt_lines):
            if not line.strip():
                continue
            try:
                receipt = _strict_projection_json(line)
            except (json.JSONDecodeError, ValueError):
                if line_index == 0 and receipt_tail_partial:
                    continue
                raise
            if not _valid_delivery_receipt(receipt):
                raise ValueError("delivery receipt schema 损坏")
            identity = f"{receipt['channel']}\x1f{receipt['event_key']}"
            if identity in receipt_identities:
                raise ValueError("delivery receipt identity 重复")
            receipt_identities.add(identity)
            if receipt["version"] == 1:
                projected = {
                    "status": "delivered", "channel": receipt.get("channel"),
                    "accepted_at": receipt.get("accepted_at"),
                    "attempt_count": receipt.get("attempt_count"),
                    "delivery_id": receipt.get("delivery_id"),
                }
            else:
                projected = {
                    "status": "suppressed", "channel": receipt.get("channel"),
                    "completed_at": receipt.get("completed_at"),
                    "attempt_count": receipt.get("attempt_count"),
                    "disposition": receipt.get("disposition"),
                }
            delivery.setdefault(receipt["event_key"], []).append(projected)
    except FileNotFoundError:
        pass
    except (UnicodeDecodeError, json.JSONDecodeError, OSError, ValueError, RuntimeError) as error:
        delivery.clear()
        authority_errors.append(f"receipt_corrupt:{type(error).__name__}")
    try:
        state_raw = read_regular_file_beneath(
            work_root, "state/outbound_delivery_state.json",
            max_bytes=_MAX_DELIVERY_STATE_BYTES)
        if not state_raw:
            raise ValueError("outbound retry state 为空")
        state = _strict_projection_json(state_raw.decode("utf-8"))
        if not _valid_retry_document(state):
            raise ValueError("outbound retry state schema 损坏")
        for retry in list(state["events"].values())[:_MAX_NOTIFICATION_RECORDS]:
            existing = delivery.get(retry["event_key"], [])
            if any(item.get("status") in {"delivered", "suppressed"}
                   and item.get("channel") == retry.get("channel") for item in existing):
                continue                    # durable ACK outranks stale retry state after cleanup crash
            delivery.setdefault(retry["event_key"], []).append({
                "status": "retrying", "channel": retry.get("channel"),
                "attempt_count": retry.get("attempt_count"),
                "next_attempt_at": retry.get("next_attempt_at"),
                "last_error_kind": retry.get("last_error_kind"),
                "last_error": retry.get("last_error"),
            })
    except FileNotFoundError:
        pass
    except (UnicodeDecodeError, json.JSONDecodeError, OSError, ValueError, RuntimeError) as error:
        authority_errors.append(f"retry_state_corrupt:{type(error).__name__}")
    for event in out:
        statuses = delivery.get(event.get("event_key"))
        if statuses:
            event["deliveries"] = statuses
    if authority_errors:
        out.append({
            "event_key": "transport-authority-corrupt",
            "kind": "transport_authority_corrupt",
            "payload": {"errors": authority_errors,
                        "message": "投递权威状态损坏；不得把通知视为已交付"},
            "deliveries": [{"status": "authority_corrupt", "channel": "transport",
                            "last_error_kind": authority_errors[0]}],
        })
    return out


def _runner_execution_live(
        work_root: Path, runner_call_id: int, *,
        authority_out: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Project guardian liveness, workload activity, and absolute deadline for one DB owner."""
    matches = []
    receipt_dir = work_root / "state" / "executions"
    for path in sorted(receipt_dir.glob("execution-*.json"))[-512:]:
        try:
            receipt = read_receipt(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        context = receipt.get("context") or {}
        if (context.get("reconcile_protocol") == "runner-call-v1"
                and context.get("db_owner_kind") == "runner_call"
                and context.get("db_owner_id") == runner_call_id):
            matches.append(receipt)
    if len(matches) != 1:
        return {"execution_authority": "missing" if not matches else "duplicate"}
    receipt = matches[0]
    if authority_out is not None:
        authority_out.clear()
        authority_out.update({"runner_call_id": runner_call_id, "receipt": receipt})
    out = {
        "execution_authority": "bound",
        "execution_operation_id": receipt.get("operation_id"),
        "watchdog_deadline_at_unix": receipt.get("deadline_at_unix"),
        "guardian_heartbeat_age_s": None,
        "activity_age_s": None,
        "watchdog_remaining_s": None,
    }
    heartbeat_ref = receipt.get("heartbeat_ref")
    heartbeat = receipt
    if isinstance(heartbeat_ref, str):
        try:
            rel = os.path.relpath(heartbeat_ref, work_root)
            if rel == ".." or rel.startswith(".." + os.sep):
                raise ValueError("heartbeat 越出 work_root")
            raw = read_regular_file_beneath(
                work_root, rel, max_bytes=128 * 1024)
            heartbeat = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError, RuntimeError, json.JSONDecodeError):
            heartbeat = receipt
    now = time.time()
    guardian_at = heartbeat.get("guardian_heartbeat_at_unix")
    activity_at = heartbeat.get("last_activity_at_unix")
    deadline_at = receipt.get("deadline_at_unix")
    if isinstance(guardian_at, (int, float)) and not isinstance(guardian_at, bool):
        out["guardian_heartbeat_age_s"] = round(max(0.0, now - float(guardian_at)), 1)
    if isinstance(activity_at, (int, float)) and not isinstance(activity_at, bool):
        out["activity_age_s"] = round(max(0.0, now - float(activity_at)), 1)
    if isinstance(deadline_at, (int, float)) and not isinstance(deadline_at, bool):
        out["watchdog_remaining_s"] = round(max(0.0, float(deadline_at) - now), 1)
    return out


def _live(
        conn: sqlite3.Connection, work_root: Path, *,
        runner_execution: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """「正在执行」活性信号（不经任何 LLM，§4.6.6 live strip）：在途轮 + 最新 runner_call + 心跳
    （transcript 文件 mtime）+ 独立 instance owner heartbeat。DB 在途只表示耐久游标；没有经
    flock+owner-id+freshness 复验的当前 owner 时必须显示 interrupted，不能伪称 running。"""
    inflight = conn.execute(
        "SELECT id, route, status, active_question_id FROM cycle "
        "WHERE status NOT IN ('done','aborted','failed') ORDER BY id DESC LIMIT 1").fetchone()
    pending_req = conn.execute("SELECT id FROM interaction_request WHERE status='pending' LIMIT 1").fetchone()
    latest_pause_control = conn.execute(
        "SELECT kind FROM directive WHERE status='consumed' AND kind IN ('pause','resume') "
        "ORDER BY consumed_decision_id DESC LIMIT 1").fetchone()
    paused = bool(latest_pause_control) and latest_pause_control[0] == "pause"
    rc = conn.execute(
        "SELECT id,cycle_id,phase,purpose,status,transcript_ref,started_at,finished_at "
        "FROM runner_call ORDER BY "
        "CASE WHEN status IN ('created','running') THEN 0 ELSE 1 END,"
        "CASE WHEN phase='interaction_query' THEN 1 ELSE 0 END,id DESC LIMIT 1").fetchone()
    instance = read_instance_status(work_root)
    mode = ("paused" if paused else
            ("awaiting_user" if pending_req else
             ("running" if inflight and instance["active"]
              and instance["state"] == "running" else
              ("interrupted" if inflight else "idle"))))
    live: Dict[str, Any] = {"mode": mode,
                            "paused": paused,
                            "block_kind": ("pause" if paused else ("file_request" if pending_req else None)),
                            "inflight_cycle": (f"c{inflight[0]}" if inflight else None),
                            "inflight_route": (inflight[1] if inflight else None),
                            "inflight_status": (inflight[2] if inflight else None),
                            "pending_request_id": (pending_req[0] if pending_req else None),
                            "runner_call": None, "heartbeat_age_s": None,
                            "guardian_heartbeat_age_s": None,
                            "activity_age_s": None,
                            "watchdog_remaining_s": None,
                            "watchdog_deadline_at_unix": None,
                            "orchestrator_active": bool(instance["active"]),
                            "orchestrator_status": instance["status"],
                            "orchestrator_owner_id": instance["owner_id"],
                            "orchestrator_pid": instance["pid"],
                            "orchestrator_state": instance["state"],
                            "orchestrator_heartbeat_age_s": instance["heartbeat_age_s"]}
    if rc is not None:
        live["runner_call"] = {"id": rc[0], "cycle_id": rc[1], "phase": rc[2], "purpose": rc[3],
                               "status": rc[4], "transcript_ref": rc[5],
                               "started_at": rc[6], "finished_at": rc[7]}
        if rc[5]:                          # 心跳 = transcript 文件 mtime 相对现在的**年龄秒**（真活性；文件不在则 None）
            try:
                info = stat_regular_file_beneath(work_root, rc[5])
                live["heartbeat_age_s"] = round(time.time() - info.st_mtime, 1)
            except (OSError, ValueError, RuntimeError):
                pass
        if rc[4] in ("created", "running"):
            live.update(_runner_execution_live(
                work_root, int(rc[0]), authority_out=runner_execution))
    return live


_BUNDLE_PACK_TARGET_RE = re.compile(
    r"^bundle\.([1-9][0-9]*)(?:\.[A-Za-z0-9._-]+)?\.pack\.json$")
_TERMINAL_BUILD_TARGET_STATUSES = frozenset({
    "complete", "skipped", "failed", "engineering_blocked",
})


def _latest_bundle_context_target(
        work_root: Path, cycle_id: int, target_ids: set[int]) -> Optional[int]:
    """Infer the target currently bound to the long-lived Bundle turn.

    ``bundle_next_target`` durably publishes a target-specific ContextPack
    before the operator starts smoke/train/eval.  Reading only the filename and
    mtime keeps the Web projection independent from the in-memory operator
    session while avoiding a second orchestration authority.
    """
    directory = work_root / "cycles" / f"c{cycle_id}" / "context_pack"
    fd = -1
    try:
        fd = open_directory_path(directory, label="bundle context-pack directory")
        newest: Optional[tuple[int, int]] = None
        for name in os.listdir(fd)[:512]:
            match = _BUNDLE_PACK_TARGET_RE.fullmatch(name)
            if match is None:
                continue
            target_id = int(match.group(1))
            if target_id not in target_ids:
                continue
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                continue
            candidate = (info.st_mtime_ns, target_id)
            if newest is None or candidate > newest:
                newest = candidate
        return newest[1] if newest is not None else None
    except (OSError, ValueError, RuntimeError):
        return None
    finally:
        if fd >= 0:
            os.close(fd)


def _training_log_kind(relative: str) -> str:
    lowered = relative.lower()
    if "smoke" in lowered:
        return "smoke"
    if "eval" in lowered or "evaluation" in lowered:
        return "eval"
    if "train" in lowered or re.search(r"(^|/)run[0-9]+/", lowered):
        return "train"
    return "execution"


def _training_log_candidates(
        work_root: Path, cycle_id: int, target_id: int
        ) -> List[tuple[int, Path, int]]:
    """List a bounded set of mutable experiment logs without following links."""
    root = work_root / f"c{cycle_id}" / f"t{target_id}"
    try:
        root_info = root.lstat()
    except OSError:
        return []
    if not stat.S_ISDIR(root_info.st_mode) or root.is_symlink():
        return []
    candidates: List[tuple[int, Path, int]] = []
    visited_dirs = 0
    visited_files = 0
    try:
        for current, dirs, files in os.walk(root, followlinks=False):
            visited_dirs += 1
            if (visited_dirs > _MAX_TRAINING_LOG_SCAN_DIRS
                    or visited_files >= _MAX_TRAINING_LOG_SCAN_FILES):
                break
            current_path = Path(current)
            safe_dirs = []
            for name in sorted(dirs)[:_MAX_TRAINING_LOG_SCAN_DIRS]:
                try:
                    info = (current_path / name).lstat()
                except OSError:
                    continue
                if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                    safe_dirs.append(name)
            dirs[:] = safe_dirs
            for name in sorted(files):
                visited_files += 1
                if visited_files > _MAX_TRAINING_LOG_SCAN_FILES:
                    break
                lowered = name.lower()
                if (not (lowered.endswith(".log") or lowered.endswith(".log.partial"))
                        or lowered.endswith(".exit")
                        or "/" in name or "\\" in name):
                    continue
                path = current_path / name
                try:
                    info = path.lstat()
                except OSError:
                    continue
                if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    candidates.append((info.st_mtime_ns, path, info.st_size))
    except OSError:
        return []
    return sorted(candidates, reverse=True)[:_MAX_TRAINING_LIVE_LOGS]


def _training_log_snapshots(
        work_root: Path, cycle_id: int, target_id: int,
        candidates: List[tuple[int, Path, int]]) -> List[Dict[str, Any]]:
    root = work_root / f"c{cycle_id}" / f"t{target_id}"
    snapshots: List[Dict[str, Any]] = []
    for mtime_ns, path, size in candidates:
        try:
            relative_to_target = path.relative_to(root).as_posix()
            relative_to_work = path.relative_to(work_root).as_posix()
            raw = read_regular_file_beneath(
                work_root, relative_to_work,
                max_bytes=_MAX_TRAINING_LOG_TAIL_BYTES, tail=True)
        except (OSError, ValueError, RuntimeError):
            continue
        # This remains the subprocess's real line-oriented output; only bearer
        # credentials and the private quest root are replaced for the browser.
        visible = _redact_runner_text(raw.decode("utf-8", errors="replace"))
        for private_root in sorted(
                {str(work_root), str(work_root.absolute())}, key=len, reverse=True):
            if private_root:
                visible = visible.replace(private_root, "<quest>")
        snapshots.append({
            "key": f"t{target_id}:{relative_to_target}",
            "target_id": target_id,
            "path": relative_to_target,
            "kind": _training_log_kind(relative_to_target),
            "state": "partial" if path.name.endswith(".partial") else "final",
            "size_bytes": size,
            "mtime_unix": round(mtime_ns / 1_000_000_000, 3),
            "age_s": round(max(0.0, time.time() - mtime_ns / 1_000_000_000), 1),
            "tail_text": visible,
            "tail_sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "truncated": size > len(raw),
        })
    return snapshots


def _training_progress(logs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Extract only progress explicitly present in logs; never invent a total."""
    texts = [str(log.get("tail_text") or "") for log in logs]
    combined = "\n".join(reversed(texts))
    completed = list(re.finditer(
        r"(?im)^train_complete\s*:\s*checkpoints\s*=\s*([0-9]+)\s*$",
        combined))
    if completed:
        count = int(completed[-1].group(1))
        return {"current": count, "total": count, "unit": "checkpoint",
                "pct": 100.0, "label": f"训练完成 · {count} 个 checkpoint"}

    explicit = list(re.finditer(
        r"(?i)\b(epoch|step|fold|cell|checkpoint|trial|subject)\b\s*"
        r"(?:[=: #]|进度)*\s*([0-9]+)\s*(?:/|\bof\b)\s*([0-9]+)",
        combined))
    if explicit:
        unit, current_text, total_text = explicit[-1].groups()
        current, total = int(current_text), int(total_text)
        if total > 0 and current <= total:
            return {"current": current, "total": total, "unit": unit.lower(),
                    "pct": round(current * 100.0 / total, 1),
                    "label": f"{unit.lower()} {current} / {total}"}

    percentages = list(re.finditer(
        r"(?<![0-9.])([0-9]{1,2}(?:\.[0-9]+)?|100(?:\.0+)?)%", combined))
    if percentages:
        pct = float(percentages[-1].group(1))
        if 0 <= pct <= 100:
            return {"current": None, "total": None, "unit": "percent",
                    "pct": round(pct, 1), "label": f"日志进度 {pct:g}%"}

    cells = list(re.finditer(
        r"(?im)^cell=([^\s]+).*?\bstep=([0-9]+)\b", combined))
    if cells:
        cell, step = cells[-1].groups()
        return {"current": int(step), "total": None, "unit": "step",
                "pct": None, "label": f"{cell} · step {step}（日志未声明总步数）"}

    checkpoints = set(re.findall(
        r"(?im)^checkpoint_written\s*:\s*key=([^\s]+)", combined))
    if checkpoints:
        count = len(checkpoints)
        return {"current": count, "total": None, "unit": "checkpoint",
                "pct": None,
                "label": f"日志尾部可见 {count} 个 checkpoint（总数未声明）"}
    return {"current": None, "total": None, "unit": None,
            "pct": None, "label": "当前日志未声明可计算的细粒度总进度"}


def _training_live(
        conn: sqlite3.Connection, work_root: Path, *,
        orchestrator_live: Optional[Mapping[str, Any]] = None,
        running_authority: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Read-only Bundle experiment monitor backed by DB state and real log tails."""
    empty: Dict[str, Any] = {
        "available": True,
        "contract_version": _TRAINING_LIVE_CONTRACT_VERSION,
        "active": False, "cycle_id": None, "runner_call_id": None,
        "runner_status": None, "current_target": None, "targets": [],
        "settled_targets": 0, "successful_targets": 0, "total_targets": 0,
        "target_progress_pct": None, "substage": "idle",
        "substage_label": "当前没有 Bundle 实验运行", "logs_target_id": None,
        "logs": [], "progress": _training_progress([]), "gpu_used": None,
        "gpu_indices": [], "snapshot_at_unix": round(time.time(), 3),
        "agent_live_text": "", "agent_activities": [],
    }
    try:
        runner = conn.execute(
            "SELECT id,cycle_id,status,started_at,finished_at FROM runner_call "
            "WHERE phase='bundle' ORDER BY id DESC LIMIT 1").fetchone()
    except sqlite3.DatabaseError:
        return empty
    if runner is None or runner[1] is None:
        return empty
    runner_call_id, cycle_id, runner_status, started_at, finished_at = runner
    rows = conn.execute(
        "SELECT bt.id,bt.seq,bt.target_kind,bt.status,bt.failure_kind,"
        "b.canonical_key,v.variant_key "
        "FROM build_target bt "
        "LEFT JOIN baseline b ON b.id=bt.baseline_id "
        "LEFT JOIN variant v ON v.id=bt.variant_id "
        "WHERE bt.cycle_id=? ORDER BY bt.seq,bt.id", (cycle_id,)).fetchall()
    targets: List[Dict[str, Any]] = []
    for (target_id, seq, target_kind, status_value, failure_kind,
         canonical_key, variant_key) in rows:
        parts = [str(value) for value in (canonical_key, variant_key) if value]
        targets.append({
            "id": int(target_id), "seq": int(seq), "kind": target_kind,
            "status": status_value, "failure_kind": failure_kind,
            "label": " / ".join(parts) if parts else f"{target_kind} target",
        })
    target_ids = {target["id"] for target in targets}
    current_target_id = _latest_bundle_context_target(
        work_root, int(cycle_id), target_ids)
    if current_target_id is None:
        pending = next((target for target in targets
                        if target["status"] not in _TERMINAL_BUILD_TARGET_STATUSES), None)
        fallback = pending or (targets[-1] if targets else None)
        current_target_id = fallback["id"] if fallback is not None else None
    current_target = next(
        (target for target in targets if target["id"] == current_target_id), None)

    logs: List[Dict[str, Any]] = []
    logs_target_id: Optional[int] = None
    search_order: List[int] = []
    if current_target_id is not None:
        search_order.append(current_target_id)
    # During reviewer/target hand-off the new target may have no log yet.  Keep
    # the immediately preceding real output visible and label its owner.
    for target in sorted(targets, key=lambda item: item["seq"], reverse=True):
        if target["id"] not in search_order:
            search_order.append(target["id"])
        if len(search_order) >= 4:
            break
    for target_id in search_order:
        candidates = _training_log_candidates(work_root, int(cycle_id), target_id)
        if not candidates:
            continue
        logs = _training_log_snapshots(
            work_root, int(cycle_id), target_id, candidates)
        if logs:
            logs_target_id = target_id
            break

    runner_active = runner_status in ("created", "running")
    owner_active = True
    if orchestrator_live is not None:
        owner_active = bool(
            orchestrator_live.get("orchestrator_active") is True
            and orchestrator_live.get("mode") == "running")
    active = bool(runner_active and owner_active)
    settled = sum(target["status"] in _TERMINAL_BUILD_TARGET_STATUSES
                  for target in targets)
    successful = sum(target["status"] in ("complete", "skipped")
                     for target in targets)
    total = len(targets)

    # Before smoke/train/eval starts, the long-lived Bundle agent may spend a
    # substantial amount of time inspecting data and editing the experiment.
    # Project that same bounded, public Codex stream into the monitor so an
    # empty experiment-log list never looks like a hung Bundle run.
    agent_live_text = ""
    agent_activities: List[Dict[str, str]] = []
    if (active and running_authority is not None
            and running_authority.get("runner_call_id") == runner_call_id
            and isinstance(running_authority.get("receipt"), Mapping)):
        agent_live_text, agent_activities = _running_codex_capture_projection(
            work_root, running_authority["receipt"])

    if not active:
        substage, substage_label = "idle", "当前没有 Bundle 实验运行"
    elif current_target is None:
        substage, substage_label = "prepare", "Bundle 正在准备实验 target"
    elif current_target["status"] in _TERMINAL_BUILD_TARGET_STATUSES:
        substage, substage_label = "review", "Codex 正在审查结果或切换下一 target"
    elif logs_target_id != current_target_id or not logs:
        substage, substage_label = "prepare", "Codex 正在构建或准备实验命令"
    else:
        substage = str(logs[0].get("kind") or "execution")
        substage_label = {
            "smoke": "Smoke 测试输出",
            "train": "训练输出",
            "eval": "评估输出",
            "execution": "实验命令输出",
        }.get(substage, "实验命令输出")

    newest_kind = logs[0].get("kind") if logs else None
    # Do not carry a smoke-only GPU count into a newer train/eval command.  A
    # missing count is more truthful than claiming the previous subprocess's
    # allocation for the process currently shown.
    all_text = "\n".join(
        str(log.get("tail_text") or "") for log in logs
        if log.get("kind") == newest_kind)
    gpu_counts = [int(value) for value in re.findall(
        r"(?im)^gpu_used\s*:\s*([0-9]+)\s*$", all_text)]
    gpu_indices = sorted({int(value) for value in re.findall(
        r"\blogical_cuda_index=([0-9]+)\b", all_text)})
    return {
        "available": True,
        "contract_version": _TRAINING_LIVE_CONTRACT_VERSION,
        "active": active, "cycle_id": int(cycle_id),
        "runner_call_id": int(runner_call_id), "runner_status": runner_status,
        "runner_started_at": started_at, "runner_finished_at": finished_at,
        "current_target": current_target, "targets": targets,
        "settled_targets": settled, "successful_targets": successful,
        "total_targets": total,
        "target_progress_pct": round(settled * 100.0 / total, 1) if total else None,
        "substage": substage, "substage_label": substage_label,
        "logs_target_id": logs_target_id, "logs": logs,
        "progress": _training_progress(logs),
        "gpu_used": gpu_counts[0] if gpu_counts else (
            len(gpu_indices) if gpu_indices else None),
        "gpu_indices": gpu_indices,
        "agent_live_text": agent_live_text,
        "agent_activities": agent_activities,
        "snapshot_at_unix": round(time.time(), 3),
    }


def _ledger_by_cycle(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Aggregate a bounded append-only tail, never ``GROUP BY`` the full history.

    If the tail cuts through one cycle, omit that boundary cycle instead of
    publishing a plausible but incomplete cost total.
    """
    rows = conn.execute(
        "SELECT cycle_id,money FROM ledger ORDER BY id DESC LIMIT ?",
        (_MAX_LEDGER_RECORDS + 1,)).fetchall()
    truncated = len(rows) > _MAX_LEDGER_RECORDS
    rows = rows[:_MAX_LEDGER_RECORDS]
    incomplete_cycle = rows[-1][0] if truncated and rows else None
    totals: Dict[int, float] = {}
    invalid = set()
    for cycle_id, money in rows:
        if cycle_id == incomplete_cycle:
            continue
        if (isinstance(money, bool) or not isinstance(money, (int, float))
                or not math.isfinite(money)):
            invalid.add(cycle_id)
            continue
        totals[cycle_id] = totals.get(cycle_id, 0.0) + float(money)
    cycle_ids = sorted(totals, reverse=True)[:_MAX_LEDGER_CYCLES]
    out = []
    for cycle_id in reversed(cycle_ids):
        money = totals[cycle_id]
        clean_money = None if cycle_id in invalid or not math.isfinite(money) else money
        out.append({"cycle": f"c{cycle_id}", "money": clean_money})
    return out


def _runner_transcript_relative(
        work_root: Path, transcript_ref: Any, *, runner_call_id: int,
        cycle_id: int) -> Optional[str]:
    """Map one DB-authored transcript ref to a narrowly allowed work-root path.

    Runner refs predate the Web product and may be absolute or relative.  The
    browser must never receive a generic host-path read capability, so only the
    deterministic final-output/event files owned by this runner_call are
    accepted here.  Prompt files, heartbeats and state/ execution captures are
    deliberately excluded.
    """
    if not isinstance(transcript_ref, str) or not transcript_ref or "\x00" in transcript_ref:
        return None
    ref = Path(transcript_ref)
    try:
        relative = ref.relative_to(work_root) if ref.is_absolute() else ref
    except ValueError:
        return None
    parts = relative.parts
    if (not parts or relative.is_absolute()
            or any(part in ("", ".", "..") or "\\" in part for part in parts)):
        return None
    filename = parts[-1]
    suffixes = (f"-rc{runner_call_id}.out.md", f"-rc{runner_call_id}.events.jsonl")
    if not filename.endswith(suffixes):
        return None
    cycle_path = (
        len(parts) == 4 and parts[0] == "cycles"
        and parts[1] == f"c{cycle_id}" and parts[2] == "transcripts")
    interaction_path = (
        len(parts) == 4 and parts[0] == "interactions"
        and parts[1] == "transcripts"
        and re.fullmatch(r"[0-9a-f]{16,64}", parts[2]) is not None)
    if not (cycle_path or interaction_path):
        return None
    return "/".join(parts)


def _compact_runner_text(text: str) -> str:
    text = text.strip()
    if len(text) <= _MAX_RUNNER_OUTPUT_CHARS:
        return text
    tail_chars = min(4096, _MAX_RUNNER_OUTPUT_CHARS // 4)
    head_chars = _MAX_RUNNER_OUTPUT_CHARS - tail_chars
    return (text[:head_chars]
            + "\n\n…（Codex 输出过长，Web 视图已限长；研究产物仍保留完整内容）…\n\n"
            + text[-tail_chars:])


def _codex_event_transcript_text(raw: str) -> str:
    """Project public CLI events without exposing hidden reasoning records."""
    messages: List[str] = []
    usage: Optional[Mapping[str, Any]] = None
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "item.completed" and isinstance(event.get("item"), dict):
            item = event["item"]
            item_kind = item.get("type")
            # agent_message is the user-visible Codex response.  Deliberately
            # do not project any `reasoning` item even if a future CLI emits it.
            if item_kind == "agent_message" and isinstance(item.get("text"), str):
                messages.append(item["text"])
            elif item_kind == "error" and isinstance(item.get("message"), str):
                messages.append("Codex 错误：" + item["message"])
        elif kind == "error" and isinstance(event.get("message"), str):
            messages.append("Codex 错误：" + event["message"])
        elif kind == "turn.failed":
            error = event.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                messages.append("Codex turn 失败：" + error["message"])
        elif kind == "turn.completed" and isinstance(event.get("usage"), dict):
            usage = event["usage"]
    if usage is not None:
        fields = []
        for key, label in (("input_tokens", "input"),
                           ("cached_input_tokens", "cached"),
                           ("output_tokens", "output"),
                           ("reasoning_output_tokens", "reasoning")):
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                fields.append(f"{label}={value}")
        if fields:
            messages.append("usage · " + " · ".join(fields))
    return _compact_runner_text("\n\n".join(messages))


_RUNNER_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s\"']+"),
    re.compile(
        r"(?i)(\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|secret|"
        r"capability)\b\s*[:=]\s*[\"']?)[^\s\"',;}]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
)


def _redact_runner_text(value: Any) -> str:
    """Best-effort redaction for authenticated Web display of CLI-visible activity."""
    text = str(value or "")
    for pattern in _RUNNER_SECRET_PATTERNS:
        if pattern.pattern.startswith("\\bsk-"):
            text = pattern.sub("[已遮蔽凭据]", text)
        else:
            text = pattern.sub(r"\1[已遮蔽]", text)
    return text


def _clip_runner_activity(value: Any, *, limit: int = _MAX_RUNNER_LIVE_ITEM_CHARS) -> str:
    text = _redact_runner_text(value).strip()
    if len(text) <= limit:
        return text
    tail = min(2048, limit // 3)
    return text[:limit - tail] + "\n…（本条输出已限长）…\n" + text[-tail:]


def _runner_activity_one_line(value: Any, *, limit: int) -> str:
    text = re.sub(r"\s+", " ", _redact_runner_text(value)).strip()
    if len(text) <= limit:
        return text
    return text[:max(1, limit - 1)].rstrip() + "…"


def _structured_codex_output_label(value: Any) -> Optional[str]:
    """Recognise a runner envelope without dumping its large JSON into the feed."""
    text = str(value or "").strip()
    candidate = text
    fenced = re.fullmatch(r"```(?:json)?\s*\n([\s\S]*?)\n```", text, re.IGNORECASE)
    if fenced is not None:
        candidate = fenced.group(1).strip()
    try:
        payload = json.loads(candidate)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping) or not isinstance(payload.get("files"), Mapping):
        return None
    names = [
        _runner_activity_one_line(name, limit=80)
        for name in payload["files"]
        if isinstance(name, str) and name
    ][:6]
    if not names:
        return "Codex 已生成本阶段结构化产物"
    suffix = "" if len(payload["files"]) <= len(names) else " 等"
    return "Codex 已生成本阶段结构化产物 · " + "、".join(names) + suffix


def _codex_activity_key(event: Mapping[str, Any], text: str) -> str:
    item = event.get("item")
    if isinstance(item, Mapping) and isinstance(item.get("id"), str) and item["id"]:
        identity: Mapping[str, Any] = {
            "event": event.get("type"),
            "item_type": item.get("type"),
            "item_id": item["id"],
        }
    else:
        identity = {"event": event.get("type"), "text": text}
    encoded = json.dumps(
        identity, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:20]


def _codex_live_activity_events(raw: str) -> List[Dict[str, str]]:
    """Project committed CLI JSONL records into short, stable public activities.

    This is a display projection only.  It deliberately ignores reasoning
    items, redacts CLI-visible secrets and never treats an unterminated final
    line as committed output.  Stable keys let the browser append each real
    action exactly once while the aggregate live transcript can still refresh
    in place inside the detailed question view.
    """
    rows: List[Dict[str, str]] = []
    for raw_line in raw.splitlines(keepends=True):
        if not raw_line.endswith(("\n", "\r")):
            continue
        line = raw_line.rstrip("\r\n")
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_kind = event.get("type")
        item = event.get("item")
        text = ""
        activity_kind = "lifecycle"
        state = "completed"

        if event_kind == "thread.started":
            text = "Codex 执行会话已建立"
            state = "running"
        elif event_kind == "turn.started":
            text = "Codex 已接收本阶段任务，正在分析上下文"
            state = "running"
        elif event_kind in ("item.started", "item.completed") and isinstance(item, dict):
            item_kind = item.get("type")
            state = "running" if event_kind == "item.started" else "completed"
            if item_kind == "reasoning":
                continue
            if item_kind == "agent_message":
                if event_kind != "item.completed" or not isinstance(item.get("text"), str):
                    continue
                activity_kind = "message"
                structured = _structured_codex_output_label(item["text"])
                text = structured or (
                    "Codex 回复 · " + _runner_activity_one_line(
                        item["text"], limit=_MAX_RUNNER_ACTIVITY_TEXT_CHARS))
            elif item_kind == "error":
                activity_kind = "error"
                message = item.get("message")
                if not isinstance(message, str):
                    continue
                text = "Codex 错误 · " + _runner_activity_one_line(
                    message, limit=_MAX_RUNNER_ACTIVITY_TEXT_CHARS)
            elif item_kind == "command_execution":
                activity_kind = "command"
                command = _runner_activity_one_line(item.get("command"), limit=300)
                if state == "running":
                    text = "开始执行命令" + (" · " + command if command else "")
                else:
                    exit_code = item.get("exit_code")
                    exit_label = f"exit {exit_code}" if isinstance(exit_code, int) else "已结束"
                    text = "命令完成（" + exit_label + "）" + (
                        " · " + command if command else "")
                    output = _runner_activity_one_line(
                        item.get("aggregated_output"), limit=320)
                    if output:
                        text += "\n结果 · " + output
            elif item_kind == "web_search":
                activity_kind = "search"
                query = _runner_activity_one_line(item.get("query"), limit=420)
                text = ("正在联网检索" if state == "running" else "联网检索完成")
                if query:
                    text += " · " + query
            elif item_kind == "mcp_tool_call":
                activity_kind = "tool"
                server = _runner_activity_one_line(item.get("server"), limit=100)
                tool = _runner_activity_one_line(item.get("tool"), limit=140)
                target = "/".join(part for part in (server, tool) if part)
                text = ("正在调用工具" if state == "running" else "工具调用完成")
                if target:
                    text += " · " + target
            elif item_kind == "file_change":
                activity_kind = "file"
                changes = item.get("changes")
                names: List[str] = []
                if isinstance(changes, list):
                    for change in changes[:4]:
                        if not isinstance(change, Mapping):
                            continue
                        path = change.get("path")
                        name = os.path.basename(path) if isinstance(path, str) else ""
                        action = _runner_activity_one_line(change.get("kind"), limit=40)
                        label = " ".join(part for part in (action, name) if part)
                        if label:
                            names.append(_runner_activity_one_line(label, limit=120))
                text = ("正在修改文件" if state == "running" else "文件修改完成")
                if names:
                    text += " · " + "、".join(names)
            else:
                activity_kind = "activity"
                label = _runner_activity_one_line(item_kind or "unknown", limit=100)
                text = ("开始 Codex 活动" if state == "running" else "Codex 活动完成")
                text += " · " + label
        elif event_kind == "error" and isinstance(event.get("message"), str):
            activity_kind = "error"
            state = "failed"
            text = "Codex 错误 · " + _runner_activity_one_line(
                event["message"], limit=_MAX_RUNNER_ACTIVITY_TEXT_CHARS)
        elif event_kind == "turn.failed":
            error = event.get("error")
            if not isinstance(error, Mapping) or not isinstance(error.get("message"), str):
                continue
            activity_kind = "error"
            state = "failed"
            text = "Codex 执行失败 · " + _runner_activity_one_line(
                error["message"], limit=_MAX_RUNNER_ACTIVITY_TEXT_CHARS)
        elif event_kind == "turn.completed":
            activity_kind = "usage"
            fields = []
            usage = event.get("usage")
            if isinstance(usage, Mapping):
                for key, label in (("input_tokens", "input"),
                                   ("cached_input_tokens", "cached"),
                                   ("output_tokens", "output"),
                                   ("reasoning_output_tokens", "reasoning")):
                    value = usage.get(key)
                    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                        fields.append(f"{label}={value}")
            text = "Codex 本次执行完成" + (" · " + " · ".join(fields) if fields else "")
        else:
            continue

        text = _clip_runner_activity(text, limit=_MAX_RUNNER_ACTIVITY_TEXT_CHARS)
        rows.append({
            "key": _codex_activity_key(event, text),
            "activity_kind": activity_kind,
            "activity_state": state,
            "text": text,
        })
    return rows[-_MAX_RUNNER_ACTIVITY_ITEMS:]


def _codex_live_event_transcript_text(raw: str) -> str:
    """Render the public part of a running ``codex exec --json`` stream.

    Commands, their captured output, errors and user-visible agent messages are
    the same operational surface a local CLI user can observe.  Reasoning items
    are intentionally ignored.  Each item is kept only in its latest state so
    the Web card refreshes in place instead of replaying start/completion pairs.
    """
    activities: Dict[str, str] = {}
    order: List[str] = []
    notices: List[str] = []
    usage: Optional[Mapping[str, Any]] = None
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        item = event.get("item")
        if kind in ("item.started", "item.completed") and isinstance(item, dict):
            item_kind = item.get("type")
            if item_kind == "reasoning":
                continue
            if item_kind == "agent_message" and isinstance(item.get("text"), str):
                notices.append("Codex 回复\n" + _clip_runner_activity(item["text"]))
                continue
            if item_kind == "error" and isinstance(item.get("message"), str):
                notices.append("Codex 错误\n" + _clip_runner_activity(item["message"]))
                continue
            item_id = str(item.get("id") or f"event-{len(order) + 1}")
            if item_id not in activities:
                order.append(item_id)
            status = str(item.get("status") or (
                "completed" if kind == "item.completed" else "in_progress"))
            if item_kind == "command_execution":
                title = "正在执行命令" if status == "in_progress" else "命令执行完成"
                exit_code = item.get("exit_code")
                if exit_code is not None:
                    title += f"（exit {exit_code}）"
                parts = [title]
                command = _clip_runner_activity(item.get("command"), limit=4096)
                output = _clip_runner_activity(item.get("aggregated_output"))
                if command:
                    parts.append("$ " + command)
                if output:
                    parts.append(output)
                activities[item_id] = "\n".join(parts)
            elif item_kind in ("mcp_tool_call", "web_search", "file_change"):
                labels = {
                    "mcp_tool_call": "工具调用", "web_search": "联网检索",
                    "file_change": "文件修改",
                }
                public = {
                    key: item[key] for key in (
                        "server", "tool", "query", "status", "result", "changes")
                    if key in item
                }
                details = _clip_runner_activity(
                    json.dumps(public, ensure_ascii=False, sort_keys=True))
                activities[item_id] = labels[item_kind] + ("\n" + details if details else "")
            else:
                # Unknown future item types stay observable without dumping
                # their arbitrary payload (which could include hidden state).
                activities[item_id] = f"Codex 活动 · {item_kind or 'unknown'} · {status}"
        elif kind == "error" and isinstance(event.get("message"), str):
            notices.append("Codex 错误\n" + _clip_runner_activity(event["message"]))
        elif kind == "turn.failed":
            error = event.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                notices.append("Codex turn 失败\n" + _clip_runner_activity(error["message"]))
        elif kind == "turn.completed" and isinstance(event.get("usage"), dict):
            usage = event["usage"]
    blocks = [activities[item_id] for item_id in order[-_MAX_RUNNER_LIVE_ITEMS:]]
    blocks.extend(notices[-4:])
    if usage is not None:
        fields = []
        for key, label in (("input_tokens", "input"),
                           ("cached_input_tokens", "cached"),
                           ("output_tokens", "output"),
                           ("reasoning_output_tokens", "reasoning")):
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                fields.append(f"{label}={value}")
        if fields:
            blocks.append("usage · " + " · ".join(fields))
    return _compact_runner_text("\n\n".join(blocks))


def _running_codex_capture_projection(
        work_root: Path, receipt: Mapping[str, Any]
        ) -> tuple[str, List[Dict[str, str]]]:
    operation_id = receipt.get("operation_id")
    capture_ref = receipt.get("capture_stdout_ref")
    if (not isinstance(operation_id, str)
            or re.fullmatch(r"exec-[0-9a-f]{32}", operation_id) is None
            or not isinstance(capture_ref, str)):
        return "", []
    expected = work_root / "state" / "executions" / f"capture-{operation_id}.stdout.bin"
    if capture_ref != str(expected):
        return "", []
    relative = expected.relative_to(work_root).as_posix()
    try:
        raw = read_regular_file_beneath(
            work_root, relative, max_bytes=_MAX_RUNNER_LIVE_CAPTURE_BYTES, tail=True)
        decoded = raw.decode("utf-8")
        return (_codex_live_event_transcript_text(decoded),
                _codex_live_activity_events(decoded))
    except (OSError, UnicodeDecodeError, ValueError, RuntimeError):
        return "", []


def _running_codex_capture_text(
        work_root: Path, receipt: Mapping[str, Any]) -> str:
    """Compatibility wrapper for callers which only need the aggregate view."""
    return _running_codex_capture_projection(work_root, receipt)[0]


def _runner_output(
        conn: sqlite3.Connection, work_root: Path, *,
        running_authority: Optional[Mapping[str, Any]] = None) -> List[Dict[str, Any]]:
    """Bounded, read-only projection of recent user-visible Codex output."""
    rows = conn.execute(
        "SELECT id,cycle_id,phase,purpose,status,transcript_ref,started_at,finished_at "
        "FROM runner_call ORDER BY id DESC LIMIT ?",
        (_MAX_RUNNER_OUTPUT_CALLS,)).fetchall()
    out: List[Dict[str, Any]] = []
    for (runner_call_id, cycle_id, phase, purpose, status, transcript_ref,
         started_at, finished_at) in reversed(rows):
        label = f"runner_call #{runner_call_id} · {phase or 'unknown'} · {purpose or '—'}"
        out.append({
            "key": f"rc{runner_call_id}:start",
            "at": started_at,
            "runner_call_id": runner_call_id,
            "cycle_id": cycle_id,
            "phase": phase,
            "purpose": purpose,
            "call_status": status,
            "kind": "status",
            "text": label + " · Codex 已启动",
        })
        if status in ("created", "running"):
            out.append({
                "key": f"rc{runner_call_id}:running",
                "at": started_at,
                "runner_call_id": runner_call_id,
                "cycle_id": cycle_id,
                "phase": phase,
                "purpose": purpose,
                "call_status": status,
                "kind": "status",
                "text": label + " · Codex 正在执行，后续活动会逐条显示",
            })
            if (running_authority is not None
                    and running_authority.get("runner_call_id") == runner_call_id
                    and isinstance(running_authority.get("receipt"), Mapping)):
                display, activities = _running_codex_capture_projection(
                    work_root, running_authority["receipt"])
                if display:
                    out.append({
                        "key": f"rc{runner_call_id}:live",
                        "at": started_at,
                        "runner_call_id": runner_call_id,
                        "cycle_id": cycle_id,
                        "phase": phase,
                        "purpose": purpose,
                        "call_status": status,
                        "kind": "live",
                        "text": display,
                    })
                for activity in activities:
                    out.append({
                        "key": f"rc{runner_call_id}:activity:{activity['key']}",
                        "at": started_at,
                        "runner_call_id": runner_call_id,
                        "cycle_id": cycle_id,
                        "phase": phase,
                        "purpose": purpose,
                        "call_status": status,
                        "kind": "activity",
                        "activity_kind": activity["activity_kind"],
                        "activity_state": activity["activity_state"],
                        "text": activity["text"],
                    })
            continue
        relative = _runner_transcript_relative(
            work_root, transcript_ref, runner_call_id=int(runner_call_id),
            cycle_id=int(cycle_id))
        if relative is not None:
            try:
                raw = read_regular_file_beneath(
                    work_root, relative, max_bytes=_MAX_RUNNER_TRANSCRIPT_BYTES)
                decoded = raw.decode("utf-8")
                display = (_codex_event_transcript_text(decoded)
                           if relative.endswith(".events.jsonl")
                           else _compact_runner_text(decoded))
                if display:
                    out.append({
                        "key": f"rc{runner_call_id}:output",
                        "at": finished_at or started_at,
                        "runner_call_id": runner_call_id,
                        "cycle_id": cycle_id,
                        "phase": phase,
                        "purpose": purpose,
                        "call_status": status,
                        "kind": "output",
                        "text": display,
                    })
            except (OSError, UnicodeDecodeError, ValueError, RuntimeError):
                # Status remains authoritative even if an old/oversized
                # transcript cannot be projected into the browser.
                pass
        out.append({
            "key": f"rc{runner_call_id}:finish:{status}",
            "at": finished_at or started_at,
            "runner_call_id": runner_call_id,
            "cycle_id": cycle_id,
            "phase": phase,
            "purpose": purpose,
            "call_status": status,
            "kind": "status",
            "text": label + f" · Codex 已结束（{status}）",
        })
    return out


def _load_bounded_policy(raw: bytes) -> Dict[str, Any]:
    """Reject YAML aliases and structures whose JSON projection could amplify a small source."""
    text = raw.decode("utf-8")
    parsed_nodes = 0
    parsed_depth = 0
    for event in yaml.parse(text, Loader=yaml.SafeLoader):
        if isinstance(event, yaml.events.AliasEvent):
            raise ValueError("policy.yaml 不允许 YAML alias")
        if isinstance(event, (yaml.events.MappingStartEvent, yaml.events.SequenceStartEvent)):
            parsed_nodes += 1
            parsed_depth += 1
            if parsed_nodes > _MAX_POLICY_NODES or parsed_depth > _MAX_POLICY_DEPTH:
                raise ValueError("policy.yaml 解析结构超过控制台投影上限")
        elif isinstance(event, (yaml.events.MappingEndEvent, yaml.events.SequenceEndEvent)):
            parsed_depth -= 1
        elif isinstance(event, yaml.events.ScalarEvent):
            parsed_nodes += 1
            if parsed_nodes > _MAX_POLICY_NODES or len(event.value) > _MAX_POLICY_STRING_CHARS:
                raise ValueError("policy.yaml scalar 数量或长度超过控制台投影上限")
    value = yaml.safe_load(text)
    if not isinstance(value, dict):
        return {}
    stack = [(value, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > _MAX_POLICY_NODES or depth > _MAX_POLICY_DEPTH:
            raise ValueError("policy.yaml 结构超过控制台投影上限")
        if isinstance(current, dict):
            for key, child in current.items():
                if not isinstance(key, str) or len(key) > _MAX_POLICY_STRING_CHARS:
                    raise ValueError("policy.yaml key 非法或过长")
                stack.append((child, depth + 1))
        elif isinstance(current, list):
            stack.extend((child, depth + 1) for child in current)
        elif isinstance(current, str):
            if len(current) > _MAX_POLICY_STRING_CHARS:
                raise ValueError("policy.yaml string 过长")
        elif isinstance(current, float):
            if not math.isfinite(current):
                raise ValueError("policy.yaml 含非有限数")
        elif current is not None and not isinstance(current, (bool, int)):
            raise ValueError(f"policy.yaml 含不可投影类型: {type(current).__name__}")
    return value


def _public_work_path(parts: List[str]) -> bool:
    """Whether a work-root path is a browser-readable research artifact.

    ``state/`` contains process receipts, raw logs, qualification capabilities
    and server-authored host-path manifests.  Those objects have dedicated
    redacted API projections and must never become a generic file-browser
    capability.  The browser may inspect only research inputs/outputs.
    """
    if not parts or any(not part or part in {".", ".."} for part in parts):
        return False
    if parts[0] in _PUBLIC_WORK_FILES:
        return len(parts) == 1
    if parts[0] not in _PUBLIC_WORK_DIRECTORIES:
        return False
    if (parts[0] == "input" and len(parts) >= 2
            and parts[1] in _PRIVATE_WORK_INPUT_FILES):
        return False
    return True


def _fs_tree(
        work_root: Path, system_root: Path, *,
        include_system_documents: bool = True) -> Dict[str, Any]:
    """真文件树：仅投影 browser-safe research artifacts。

    The legacy single-work-root operations console may additionally project
    system documents.  The installed multi-quest Web product deliberately
    disables that projection: a researcher sees task inputs/outputs, never a
    navigable view of backend policy, prompts or schemas.

    深度/条目有界（防超大树拖垮前端）；每节点 {p: 相对名, dir: bool, size: 字节}。

    递归只从已经固定的目录 fd 走 ``openat(O_NOFOLLOW)``；pathname/symlink 预检与随后 ``iterdir``
    分离会留下 rename→symlink 窗口，足以把根外文件名投影进已鉴权 API。
    """
    remaining = [_MAX_FS_NODES]

    def open_root(path: Path) -> Optional[int]:
        try:
            fd = open_directory_path(path, label="console 文件树根")
        except OSError:
            return None
        try:
            if not stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError(f"文件树根不是目录: {path}")
            return fd
        except BaseException:
            os.close(fd)
            return None

    def open_child(parent_fd: int, name: str, expected: os.stat_result) -> Optional[int]:
        child_fd: Optional[int] = None
        try:
            if not stat.S_ISDIR(expected.st_mode):
                return None
            child_fd = os.open(name, _FS_DIRECTORY_FLAGS, dir_fd=parent_fd)
            opened = os.fstat(child_fd)
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            expected_id = (expected.st_dev, expected.st_ino)
            if (not stat.S_ISDIR(opened.st_mode) or not stat.S_ISDIR(current.st_mode)
                    or (opened.st_dev, opened.st_ino) != expected_id
                    or (current.st_dev, current.st_ino) != expected_id):
                raise OSError("文件树目录项在枚举后被替换")
            return child_fd
        except OSError:
            if child_fd is not None:
                os.close(child_fd)
            return None

    def walk(directory_fd: int, depth: int, *, relative: tuple[str, ...] = (),
             work_projection: bool = False) -> List[Dict[str, Any]]:
        if depth > 6 or remaining[0] <= 0:
            return []
        entries = []
        try:
            # 每 3 秒轮询不能遍历攻击者放入 uploads 的百万项目录；只枚举固定 fd 的有界前缀。
            with os.scandir(directory_fd) as iterator:
                limit = min(_MAX_FS_ENTRIES_PER_DIRECTORY, remaining[0])
                for _ in range(limit):
                    try:
                        entry = next(iterator)
                    except StopIteration:
                        break
                    # hidden/坏 stat 也消耗 I/O 预算，不能借此制造无界扫描。
                    if entry.name.startswith("."):
                        continue
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISLNK(info.st_mode):
                        continue
                    if (work_projection
                            and not _public_work_path(list(relative + (entry.name,)))):
                        continue
                    entries.append((entry.name, info))
            entries.sort(key=lambda pair: (not stat.S_ISDIR(pair[1].st_mode), pair[0]))
        except OSError:
            return []
        nodes = []
        for name, info in entries:
            if remaining[0] <= 0:
                break
            remaining[0] -= 1
            is_directory = stat.S_ISDIR(info.st_mode)
            node = {"p": name, "dir": is_directory}
            if is_directory:
                child_fd = open_child(directory_fd, name, info)
                if child_fd is None:
                    node["children"] = []
                else:
                    try:
                        node["children"] = walk(
                            child_fd, depth + 1,
                            relative=relative + (name,),
                            work_projection=work_projection)
                    finally:
                        os.close(child_fd)
            else:
                node["size"] = info.st_size if stat.S_ISREG(info.st_mode) else None
            nodes.append(node)
        return nodes

    def root_node(label: str, path: Path, *, required: bool,
                  work_projection: bool = False) -> Optional[Dict[str, Any]]:
        fd = open_root(path)
        if fd is None:
            return {"p": label, "dir": True, "children": []} if required else None
        try:
            return {"p": label, "dir": True, "children": walk(
                fd, 0, work_projection=work_projection)}
        finally:
            os.close(fd)

    roots = [root_node(
        "work", work_root, required=True, work_projection=True)]
    if include_system_documents:
        for sub in ("schemas", "prompts", "policies", "input"):
            node = root_node(sub, system_root / sub, required=False)
            if node is not None:
                roots.append(node)
    return {"roots": roots}


def assemble_db(
        db_path: str, work_root: str, system_root: str, *,
        product_mode: bool = False) -> Dict[str, Any]:
    """组装控制台 /api/db 载荷（纯函数、只读连接、可单测）：真表投影 + 派生对象。"""
    conn = _open_ro(db_path, work_root=work_root)
    try:
        projected: Dict[str, List[Dict[str, Any]]] = {}
        remaining = _MAX_DB_PROJECTION_BYTES
        # 控制表先投影，避免大研究历史吃完整体预算后让 pending confirm/resolve 从 200 响应中消失。
        priority = [table for table in _CONTROL_TABLES if table in _PROJECT_TABLES]
        priority.extend(table for table in _PROJECT_TABLES if table not in _CONTROL_TABLES)
        for table in priority:
            budget = min(_MAX_DB_TABLE_BYTES, remaining)
            rows = _rows(conn, table, max_serialized_bytes=budget)
            projected[table] = rows
            used = len(json.dumps(
                rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            remaining = max(0, remaining - used)
        tables = {table: projected.get(table, []) for table in _PROJECT_TABLES}
        payload: Dict[str, Any] = {"tables": tables}
        payload["status_card"] = _load_status_card(Path(work_root))
        runner_execution: Dict[str, Any] = {}
        payload["live"] = _live(
            conn, Path(work_root), runner_execution=runner_execution)
        payload["training_live"] = _training_live(
            conn, Path(work_root), orchestrator_live=payload["live"],
            running_authority=runner_execution or None)
        payload["notification"] = _notifications(Path(work_root))
        payload["ledger_by_cycle"] = _ledger_by_cycle(conn)
        payload["runner_output"] = _runner_output(
            conn, Path(work_root), running_authority=runner_execution or None)
        payload["narrator_session"] = public_narrator_session_status(Path(work_root))
    finally:
        conn.close()
    try:                                   # policy 解析失败**不拖垮整个仪表盘**
        raw_policy = read_regular_file_beneath(
            system_root, "policies/policy.yaml", max_bytes=_MAX_POLICY_BYTES)
        payload["policy"] = _load_bounded_policy(raw_policy)
    except (UnicodeDecodeError, yaml.YAMLError, OSError, ValueError, RuntimeError):
        payload["policy"] = {}
    payload["fs"] = _fs_tree(
        Path(work_root), Path(system_root),
        include_system_documents=not product_mode)
    return payload


class ConsoleData:
    """控制台数据源 + 入站 spool（不含 HTTP；供 handler 与测试共用）。只读库 + 只写 inbox spool。"""

    def __init__(self, *, db_path: str, work_root: str, system_root: str,
                 spool: Optional[ConsoleSpool] = None,
                 capability_token: Optional[str] = None,
                 product_mode: bool = False):
        self.db_path = db_path
        self.work_root = Path(work_root)
        self.system_root = Path(system_root)
        self.spool = spool or ConsoleSpool(self.work_root)
        self.inbox = self.spool.inbox_path
        self.capability_token = capability_token
        self.product_mode = bool(product_mode)

    def db(self) -> Dict[str, Any]:
        return assemble_db(
            self.db_path, str(self.work_root), str(self.system_root),
            product_mode=self.product_mode)

    def _virtual_root(self, seg: str) -> Optional[Path]:
        """FS 树暴露的虚拟根 → 真目录（显式映射，不靠 base.parent 猜——codex SHOULD：--work-root 叫任意名
        时 base.parent/rel 拼法会 404）。work→work_root；schemas/prompts/policies/input→system_root/<seg>。"""
        if seg == "work":
            return self.work_root
        if self.product_mode:
            return None
        if seg in ("schemas", "prompts", "policies", "input"):
            return self.system_root / seg
        return None

    def read_file(self, rel: str) -> Optional[bytes]:
        """白名单 fd 读：逐组件 ``openat/O_NOFOLLOW``，并从最终常规文件 fd 有界读取。"""
        parts = rel.lstrip("/").split("/", 1)
        base = self._virtual_root(parts[0])
        if base is None:
            return None
        sub = parts[1] if len(parts) > 1 else ""
        if parts[0] == "work" and not _public_work_path(sub.split("/")):
            return None
        if CAPABILITY_NAME in sub.split("/"):
            return None
        try:
            return read_regular_file_beneath(base, sub, max_bytes=_MAX_FILE_RESPONSE_BYTES)
        except (OSError, ValueError, RuntimeError):
            return None
        return None

    def _enqueue(self, rec: Dict[str, Any], *, client_idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        """给已校验的入站记录分配 spool 序号并追加。

        普通文本与 directive 确认/拒绝共用同一序列，因而 ``(connector,idempotency_key)`` 不会在两类
        HTTP 请求之间相撞。console_server 仍然只写文件；动作的权威落库由 run 进程 ingest 完成。
        """
        stored_key = _stored_idempotency_key(client_idempotency_key)
        if stored_key is not None:
            rec = dict(rec)
            rec["idempotency_key"] = stored_key
        return self.spool.append(rec)

    def enqueue_message(self, text: str, connector: str = "console", *,
                        conversation_id: Optional[str] = None,
                        client_idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        """人工文本入站 → 追加写 inbox spool；**不写 DB**。"""
        if not isinstance(text, str):
            raise ValueError("消息 text 须为字符串")
        text = text.strip()
        if not text:
            raise ValueError("空消息")
        if len(text) > _MAX_MESSAGE_CHARS:
            raise ValueError(f"消息过长（最多 {_MAX_MESSAGE_CHARS} 字符）")
        conversation_id = _console_conversation_id(conversation_id)
        record = {"connector": connector, "raw_text": text}
        if conversation_id is not None:
            record["conversation_id"] = conversation_id
        return self._enqueue(record,
                             client_idempotency_key=client_idempotency_key)

    def enqueue_query(self, text: str, connector: str = "console", *,
                      conversation_id: Optional[str] = None,
                      client_idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        """讲解员只读提问 → 明确标记 transport intent 后追加 inbox；不写 DB。

        ``action_target=query`` 由 run 单写者消费为强制 query 分类。这样即便问题正文包含“暂停”或
        “改预算”等控制词，也只能得到解释，不可能借讲解员入口生成 directive。
        """
        if not isinstance(text, str):
            raise ValueError("查询 text 须为字符串")
        text = text.strip()
        if not text:
            raise ValueError("空查询")
        if len(text) > _MAX_MESSAGE_CHARS:
            raise ValueError(f"查询过长（最多 {_MAX_MESSAGE_CHARS} 字符）")
        conversation_id = _console_conversation_id(conversation_id)
        record: Dict[str, Any] = {
            "connector": connector,
            "action_target": "query",
            "raw_text": text,
        }
        if conversation_id is not None:
            record["conversation_id"] = conversation_id
        return self._enqueue(record, client_idempotency_key=client_idempotency_key)

    def enqueue_directive_action(self, *, action: str, directive_id: Any,
                                 reason: str = "", connector: str = "console",
                                 conversation_id: Optional[str] = None,
                                 client_idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        """把显式 directive 确认/拒绝追加到 spool；**绝不查询或写研究 DB**。

        HTTP 控件传来的 ``directive_id`` 只做形状校验，目标是否存在、是否仍可操作由 run 进程在单写
        事务域内判断。``raw_text`` 是之后权威 interaction_message 的不可变原文；拒绝理由另带字段，
        避免把理由重新送进自然语言分类器而意外生成另一条 directive。
        """
        action = str(action or "").strip().lower()
        if action not in ("confirm", "reject"):
            raise ValueError("action 须为 confirm 或 reject")
        did = _canonical_positive_id(directive_id, label="directive_id")
        if not isinstance(reason, str):
            raise ValueError("拒绝理由须为字符串")
        reason = reason.strip()
        if action == "reject" and not reason:
            reason = "用户从控制台拒绝"
        if len(reason) > _MAX_REASON_CHARS:
            raise ValueError(f"拒绝理由过长（最多 {_MAX_REASON_CHARS} 字符）")
        raw = directive_action_text(action, did, reason=reason)
        conversation_id = _console_conversation_id(conversation_id)
        rec: Dict[str, Any] = {"connector": connector, "raw_text": raw,
                               "action": action, "directive_id": did}
        if conversation_id is not None:
            rec["conversation_id"] = conversation_id
        if action == "reject":
            rec["reason"] = reason
        return self._enqueue(rec, client_idempotency_key=client_idempotency_key)

    def enqueue_file_request_action(self, *, action: str, request_id: Any,
                                    source_ref: Any = None, reason: str = "",
                                    connector: str = "console",
                                    client_idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        """把资料上传/权限确认控件动作追加到 spool；研究库仍只读。

        HTTP 层用 mode=ro 只核请求身份存在；状态迁移完全由 run 进程在单写域重核。不能用可变的
        pending/terminal 状态拦 append：首次动作可能已经入 spool 并迁终态、但 HTTP ACK 丢失，客户端
        必须能用同一 Idempotency-Key 重放并由 resolved_message_id provenance 收敛。resolve 只携安全
        虚拟目录引用，不把任意绝对路径送入 spool。
        """
        action = str(action or "").strip().lower()
        if action not in ("resolve", "approve", "cancel"):
            raise ValueError("action 须为 resolve、approve 或 cancel")
        rid = _canonical_positive_id(request_id, label="request_id")
        conn = _open_ro(self.db_path, work_root=self.work_root)
        try:
            row = conn.execute("SELECT 1 FROM interaction_request WHERE id=?", (rid,)).fetchone()
        finally:
            conn.close()
        if row is None:
            raise ValueError(f"interaction_request 不存在: {rid}")

        rec: Dict[str, Any] = {"connector": connector, "action_target": "file_request",
                               "action": action, "request_id": rid}
        if action == "resolve":
            normalized = resolve_upload_ref(source_ref, work_root=self.work_root,
                                            system_root=self.system_root)
            rec["source_ref"] = normalized
            rec["raw_text"] = f"解决文件请求 r{rid}，来源 {normalized}"
        elif action == "approve":
            rec["raw_text"] = f"同意权限请求 r{rid}"
        else:
            if not isinstance(reason, str):
                raise ValueError("取消理由须为字符串")
            reason = reason.strip() or "用户从控制台取消文件请求"
            if len(reason) > _MAX_REASON_CHARS:
                raise ValueError(f"取消理由过长（最多 {_MAX_REASON_CHARS} 字符）")
            rec["reason"] = reason
            rec["raw_text"] = f"取消文件请求 r{rid}：{reason}"
        return self._enqueue(rec, client_idempotency_key=client_idempotency_key)


class QuestConsoleData:
    """Multi-quest facade.  Every selected quest gets its own read DB + inbox spool."""

    def __init__(self, *, registry: QuestRegistry, system_root: str,
                 capability_token: Optional[str] = None,
                 web_service: Optional[WebQuestService] = None):
        self.registry = registry
        self.system_root = Path(system_root)
        self.capability_token = capability_token
        self.web_service = web_service
        # The installed, loopback-only, single-user Web product may bootstrap a
        # browser opened at the bare local URL.  Single-quest/admin ``serve``
        # keeps the stricter explicit-fragment flow.
        self.local_browser_bootstrap = True

    def list_quests(self, *, selector_only: bool = False) -> List[Dict[str, Any]]:
        """Return quest identities, optionally without expensive setup detail.

        The browser selector only consumes ``quest_id`` and ``title``.  Reading
        every quest's dataset preflight/runtime profile while transitioning
        into a freshly published task made that transition look frozen, so the
        selector view deliberately avoids those per-quest filesystem reads.
        The existing full response remains the default API contract.
        """
        if not isinstance(selector_only, bool):
            raise ValueError("selector_only 须为 bool")
        result = []
        for quest in self.registry.list():
            row = quest.public_dict()
            if self.web_service is not None and not selector_only:
                row["setup"] = self.web_service.ready(quest.quest_id)
                row["runtime"] = self.web_service.runtime(quest.quest_id)
                row["runtime_profile"] = self.web_service.runtime_profile(
                    quest.quest_id)
            result.append(row)
        return result

    def quest_data(self, quest_id: Any) -> ConsoleData:
        if not isinstance(quest_id, str) or not quest_id:
            raise ValueError("multi-quest API 必须提供 quest_id")
        quest = self.registry.get(quest_id)
        return ConsoleData(
            db_path=str(quest.db_path), work_root=str(quest.work_root),
            system_root=str(self.system_root), capability_token=self.capability_token,
            product_mode=True)

    def create_quest(self, body: Dict[str, Any], *, idempotency_key: str):
        unexpected = set(body) - {"quest_id", "title", "template_id", "goal_brief_md"}
        if unexpected:
            raise ValueError(f"quest 创建请求含未知字段: {sorted(unexpected)}")
        template_id = body.get("template_id")
        custom = body.get("goal_brief_md")
        if template_id is not None and custom is not None:
            raise ValueError("template_id 与 goal_brief_md 只能提供一个")
        if template_id == "t1-eeg-universal":
            raise ValueError(
                "T1 必须通过 Web 数据向导自动预检并安装内部 qualification contract，"
                "不得用普通 quest 创建接口绕过")
        # Bind before any filesystem publication.  Replaying the same key/body
        # after a crash is safe; reusing one key for a different body is 409.
        self.registry.bind_create_request(idempotency_key, body)
        if template_id is not None:
            return self.registry.create_from_template(
                quest_id=body.get("quest_id"), title=body.get("title"),
                template_id=template_id)
        if custom is None:
            raise ValueError("须提供 template_id 或 goal_brief_md")
        return self.registry.create(
            quest_id=body.get("quest_id"), title=body.get("title"),
            goal_brief_md=custom)

    def require_web_service(self) -> WebQuestService:
        if self.web_service is None:
            raise ValueError("当前多 quest 服务未启用 Web-first setup")
        return self.web_service


def make_handler(data: Union[ConsoleData, QuestConsoleData], static_dir: Optional[Path]):
    """构造 HTTP handler 类（闭包持 data + 静态目录）。路由：
    GET /api/db · GET /api/file?p=… · POST /api/message{text} · POST /api/query{text} ·
    POST /api/directive{action,directive_id,reason?} ·
    POST /api/file-request{action,request_id,source_ref?|reason?} · GET /（静态控制台页）。
    多 quest 模式另提供 GET/POST /api/quests，其他 API 必须显式携 quest 身份。"""
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(_HTTP_SOCKET_TIMEOUT_S)

        def log_message(self, *a):        # 静默（观测服务，不刷屏）
            pass

        def _json(self, code: int, obj: Any, *, extra_headers: Optional[Dict[str, str]] = None):
            try:
                body = _bounded_json_bytes(obj)
            except JsonResponseTooLarge:
                code = 503
                body = _bounded_json_bytes(
                    {"error": f"JSON 响应超过 {_MAX_JSON_RESPONSE_BYTES} 字节安全上限"},
                    max_bytes=_MAX_JSON_RESPONSE_BYTES)
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _request_host_ok(self) -> bool:
            values = self.headers.get_all("Host") or []
            return len(values) == 1 and _host_header_is_loopback(values[0])

        def _origin_ok(self) -> bool:
            """无 Origin 的本机 CLI 允许；浏览器请求必须与当前 loopback Host 严格同源。"""
            origins = self.headers.get_all("Origin") or []
            if not origins:
                return True
            if len(origins) != 1:
                return False
            origin = origins[0]
            try:
                parsed = urlparse(origin)
            except ValueError:
                return False
            hosts = self.headers.get_all("Host") or []
            if len(hosts) != 1:
                return False
            host = hosts[0]
            return (parsed.scheme == "http" and parsed.netloc.lower() == host.lower()
                    and parsed.path in ("", "/") and not parsed.params and not parsed.query and not parsed.fragment)

        @staticmethod
        def _is_api_path(path: str) -> bool:
            return path == "/api" or path.startswith("/api/")

        def _api_authorized(self) -> bool:
            """Require exactly one canonical bearer capability header."""
            values = self.headers.get_all("Authorization") or []
            if len(values) != 1 or data.capability_token is None:
                return False
            value = values[0]
            if value.count(" ") != 1:
                return False
            scheme, candidate = value.split(" ", 1)
            if scheme.lower() != "bearer":
                return False
            if len(candidate) != 64 or any(ch not in "0123456789abcdef" for ch in candidate):
                return False
            return hmac.compare_digest(candidate, data.capability_token)

        def _require_api_authorization(self, path: str) -> bool:
            if not self._is_api_path(path) or self._api_authorized():
                return True
            self._json(401, {"error": "缺少或无效的 console bearer capability"},
                       extra_headers={"WWW-Authenticate": "Bearer"})
            return False

        def _content_length(self, *, maximum: int) -> int:
            if self.headers.get_all("Transfer-Encoding"):
                raise ValueError("不支持 Transfer-Encoding；须使用唯一 Content-Length")
            lengths = self.headers.get_all("Content-Length") or []
            if len(lengths) != 1:
                raise ValueError("须提供唯一 Content-Length")
            raw_length = lengths[0]
            try:
                length = int(raw_length)
            except ValueError:
                raise ValueError("Content-Length 非法") from None
            if length < 0 or length > maximum:
                raise ValueError(f"请求体大小须在 0..{maximum} 字节")
            return length

        def _read_json_object(self, *, maximum: int = _MAX_HTTP_BODY_BYTES) -> Dict[str, Any]:
            if self.headers.get_content_type().lower() != "application/json":
                raise ValueError("Content-Type 必须为 application/json")
            length = self._content_length(maximum=maximum)
            try:
                raw = self.rfile.read(length)
            except OSError as error:
                raise ValueError("请求体读取失败或超时") from error
            if len(raw) != length:
                raise ValueError(f"请求体提前结束（声明 {length} 字节，实际 {len(raw)} 字节）")
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise ValueError("请求体须为 JSON object")
            return body

        def _read_upload_chunk(self) -> bytes:
            if self.headers.get_content_type().lower() != "application/octet-stream":
                raise ValueError("上传 chunk Content-Type 必须为 application/octet-stream")
            length = self._content_length(maximum=_MAX_UPLOAD_CHUNK_BYTES)
            if length <= 0:
                raise ValueError("上传 chunk 不得为空")
            self.connection.settimeout(_UPLOAD_SOCKET_TIMEOUT_S)
            try:
                raw = self.rfile.read(length)
            except OSError as error:
                raise ValueError("上传 chunk 读取失败或超时") from error
            finally:
                self.connection.settimeout(_HTTP_SOCKET_TIMEOUT_S)
            if len(raw) != length:
                raise ValueError(
                    f"上传 chunk 提前结束（声明 {length} 字节，实际 {len(raw)} 字节）")
            return raw

        @staticmethod
        def _one_query(params: Mapping[str, List[str]], name: str) -> str:
            values = params.get(name) or []
            if len(values) != 1 or not values[0]:
                raise ValueError(f"须提供唯一 query 参数 {name}")
            return values[0]

        @classmethod
        def _query_int(cls, params: Mapping[str, List[str]], name: str,
                       *, positive: bool = False) -> int:
            raw = cls._one_query(params, name)
            if re.fullmatch(r"0|[1-9][0-9]*", raw) is None:
                raise ValueError(f"query 参数 {name} 须为规范整数")
            value = int(raw)
            if positive and value <= 0:
                raise ValueError(f"query 参数 {name} 须为正整数")
            return value

        def _request_idempotency_key(self) -> str:
            values = self.headers.get_all("Idempotency-Key") or []
            if len(values) != 1:
                raise ValueError("POST 须提供唯一 Idempotency-Key")
            _stored_idempotency_key(values[0])                 # canonical shape validation
            return values[0]

        @staticmethod
        def _is_multi_quest() -> bool:
            return isinstance(data, QuestConsoleData)

        def _get_data(self, query: str) -> ConsoleData:
            if not self._is_multi_quest():
                return data  # type: ignore[return-value]
            values = parse_qs(query, keep_blank_values=True).get("quest") or []
            if len(values) != 1 or not values[0]:
                raise ValueError("multi-quest API 须提供唯一 ?quest=<quest_id>")
            return data.quest_data(values[0])

        def _post_data(self, body: Dict[str, Any]) -> ConsoleData:
            if not self._is_multi_quest():
                if "quest_id" in body:
                    raise ValueError("单 quest 控制台不接受 quest_id")
                return data  # type: ignore[return-value]
            return data.quest_data(body.get("quest_id"))

        def do_GET(self):
            if not self._request_host_ok():
                self._json(421, {"error": "Host 必须是 loopback 地址（防 DNS rebinding）"})
                return
            u = urlparse(self.path)
            if (isinstance(data, QuestConsoleData)
                    and data.local_browser_bootstrap
                    and u.path == "/" and not u.query
                    and data.capability_token is not None):
                # Product-mode convenience under the v1 single-user/local-host
                # deployment assumption.  The bearer remains in a fragment: it
                # is never sent back in the HTTP request, query, cookie or
                # Referer, and the page immediately removes it from history.
                location = "/?console-bootstrap=1#token=" + data.capability_token
                self.send_response(303)
                self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.end_headers()
                return
            if not self._require_api_authorization(u.path):
                return
            if u.path == "/api/quests":
                if not self._is_multi_quest():
                    self._json(404, {"error": "当前服务未启用多 quest 模式"})
                    return
                try:
                    params = parse_qs(u.query, keep_blank_values=True)
                    if set(params) - {"view"}:
                        raise ValueError("quest list query 含未知参数")
                    views = params.get("view", [])
                    if views not in ([], ["selector"]):
                        raise ValueError("quest list view 仅允许 selector")
                    self._json(200, {"quests": data.list_quests(
                        selector_only=views == ["selector"])})
                except ValueError as error:
                    self._json(400, {"error": str(error)})
                except QuestCorruptError:
                    self._json(503, {"error": "quest registry 损坏或暂不可读"})
                return
            if u.path == "/api/setup":
                if not self._is_multi_quest():
                    self._json(404, {"error": "当前服务未启用 Web-first 多任务模式"})
                    return
                try:
                    self._json(200, data.require_web_service().setup_public())
                except (ValueError, WebQuestServiceError):
                    self._json(503, {"error": "Web setup 配置损坏或暂不可读"})
                return
            if u.path == "/api/quest-publish-status":
                if not self._is_multi_quest():
                    self._json(404, {"error": "当前服务未启用 Web-first 多任务模式"})
                    return
                try:
                    params = parse_qs(u.query, keep_blank_values=True)
                    job_id = self._one_query(params, "job")
                    self._json(200, {
                        "job": data.require_web_service().publish_job_status(job_id)})
                except (ValueError, KeyError) as error:
                    self._json(404 if isinstance(error, KeyError) else 400,
                               {"error": str(error)})
                except WebQuestServiceError:
                    self._json(503, {"error": "任务发布状态暂不可读"})
                return
            if u.path == "/api/quest-drafts":
                if not self._is_multi_quest():
                    self._json(404, {"error": "当前服务未启用 Web-first 多任务模式"})
                    return
                try:
                    web = data.require_web_service()
                    params = parse_qs(u.query, keep_blank_values=True)
                    draft_values = params.get("draft_id") or []
                    if draft_values:
                        if len(draft_values) != 1 or not draft_values[0]:
                            raise ValueError("须提供唯一 draft_id")
                        result = web.drafts.get(draft_values[0])
                        result["local_sources"] = web.local_sources.list(
                            draft_values[0])
                        self._json(200, {"draft": result})
                    else:
                        self._json(200, {"drafts": web.drafts.list()})
                except (ValueError, KeyError) as error:
                    self._json(404 if isinstance(error, KeyError) else 400,
                               {"error": str(error)})
                except (DraftCorruptError, WebQuestServiceError):
                    self._json(503, {"error": "quest draft 损坏或暂不可读"})
                return
            if u.path == "/api/quest-runtime-profile":
                if not self._is_multi_quest():
                    self._json(404, {"error": "当前服务未启用 Web-first 多任务模式"})
                    return
                try:
                    params = parse_qs(u.query, keep_blank_values=True)
                    quest_values = params.get("quest_id") or []
                    alias_values = params.get("quest") or []
                    if quest_values and alias_values:
                        raise ValueError("quest_id 与 quest query alias 只能提供一个")
                    values = quest_values or alias_values
                    if len(values) != 1 or not values[0]:
                        raise ValueError("须提供唯一 query 参数 quest_id")
                    self._json(200, {
                        "runtime_profile": data.require_web_service().runtime_profile(
                            values[0])})
                except (ValueError, KeyError) as error:
                    self._json(404 if isinstance(error, KeyError) else 400,
                               {"error": str(error)})
                except (QuestCorruptError, WebQuestServiceError):
                    self._json(503, {"error": "quest runtime profile 暂不可读"})
                return
            if u.path == "/api/quest-runtime":
                if not self._is_multi_quest():
                    self._json(404, {"error": "当前服务未启用 Web owner"})
                    return
                try:
                    params = parse_qs(u.query, keep_blank_values=True)
                    quest_id = self._one_query(params, "quest")
                    self._json(200, {"runtime": data.require_web_service().runtime(quest_id),
                                     "setup": data.require_web_service().ready(quest_id)})
                except (ValueError, KeyError) as error:
                    self._json(404 if isinstance(error, KeyError) else 400,
                               {"error": str(error)})
                except (QuestCorruptError, WebQuestServiceError,
                        QuestProcessManagerError):
                    self._json(503, {"error": "quest runtime 状态暂不可读"})
                return
            if u.path == "/api/quest-runtime-log":
                if not self._is_multi_quest():
                    self._json(404, {"error": "当前服务未启用 Web owner"})
                    return
                try:
                    params = parse_qs(u.query, keep_blank_values=True)
                    quest_id = self._one_query(params, "quest")
                    self._json(200, {
                        "diagnostic": data.require_web_service().runtime_log(quest_id)})
                except (ValueError, KeyError) as error:
                    self._json(404 if isinstance(error, KeyError) else 400,
                               {"error": str(error)})
                except (QuestCorruptError, WebQuestServiceError,
                        QuestProcessManagerError, OSError):
                    self._json(503, {"error": "quest 运行诊断暂不可读"})
                return
            if u.path == "/api/db":
                try:
                    selected = self._get_data(u.query)
                    self._json(200, selected.db())
                except (ValueError, KeyError) as error:
                    self._json(404 if isinstance(error, KeyError) else 400,
                               {"error": str(error)})
                except QuestCorruptError:
                    self._json(503, {"error": "quest registry 损坏或暂不可读"})
                except SharedSQLiteReaderUnavailable:
                    self._json(503, {"error": "研究库暂不可在本节点读取，请稍后重试"})
                except Exception:                     # 只读观测面：组装失败向客户端**泛化报**（不泄内部细节/路径，
                    import traceback                   # codex SHOULD）；真实细节写 stderr 供运维排障（codex 第2轮
                    traceback.print_exc()              # NIT：文案承诺「详见服务端日志」须真有日志，否则线上排障盲）
                    self._json(500, {"error": "内部错误：/api/db 组装失败（详见服务端日志）"})
                return
            if u.path == "/api/file":
                params = parse_qs(u.query, keep_blank_values=True)
                paths = params.get("p") or []
                if len(paths) != 1:
                    self._json(400, {"error": "须提供唯一文件参数 p"})
                    return
                try:
                    selected = self._get_data(u.query)
                except (ValueError, KeyError) as error:
                    self._json(404 if isinstance(error, KeyError) else 400,
                               {"error": str(error)})
                    return
                except QuestCorruptError:
                    self._json(503, {"error": "quest registry 损坏或暂不可读"})
                    return
                rel = paths[0]
                content = selected.read_file(rel)
                if content is None:
                    self._json(404, {"error": f"文件不可读/不在白名单: {rel}"})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.end_headers()
                self.wfile.write(content)
                return
            self._serve_static(u.path)

        def do_POST(self):
            if not self._request_host_ok():
                self._json(421, {"error": "Host 必须是 loopback 地址（防 DNS rebinding）"})
                return
            u = urlparse(self.path)
            if not self._require_api_authorization(u.path):
                return
            if not self._origin_ok():
                self._json(403, {"error": "跨源控制请求被拒绝"})
                return
            try:
                client_key = self._request_idempotency_key()
            except ValueError as e:
                self._json(400, {"error": str(e)})
                return

            chunk_routes = {
                "/api/quest-drafts/file/chunk",
                "/api/file-request-uploads/file/chunk",
            }
            if u.path in chunk_routes:
                if not self._is_multi_quest():
                    self._json(404, {"error": "当前服务未启用 Web-first 上传"})
                    return
                try:
                    params = parse_qs(u.query, keep_blank_values=True)
                    path = self._one_query(params, "path")
                    offset = self._query_int(params, "offset")
                    digest = self._one_query(params, "sha256")
                    chunk = self._read_upload_chunk()
                    web = data.require_web_service()
                    if u.path == "/api/quest-drafts/file/chunk":
                        draft_id = self._one_query(params, "draft_id")
                        identity = {
                            "draft_id": draft_id, "path": path,
                            "offset": offset, "sha256": digest,
                            "bytes": len(chunk),
                        }
                        web.bind_operation(client_key, u.path, identity)
                        result = web.drafts.append_chunk(
                            draft_id, path, offset, chunk, digest)
                    else:
                        quest_id = self._one_query(params, "quest_id")
                        request_id = self._query_int(params, "request_id", positive=True)
                        upload_id = self._one_query(params, "upload_id")
                        identity = {
                            "quest_id": quest_id, "request_id": request_id,
                            "upload_id": upload_id, "path": path,
                            "offset": offset, "sha256": digest,
                            "bytes": len(chunk),
                        }
                        web.bind_operation(client_key, u.path, identity)
                        result = web.request_append_chunk(
                            quest_id, request_id, upload_id, path, offset,
                            chunk, digest)
                    self._json(200, {
                        "ok": True, "file": result,
                        "idempotency_key": _stored_idempotency_key(client_key),
                    })
                except (DraftConflictError, WebQuestConflictError) as error:
                    self._json(409, {"error": str(error)})
                except (ValueError, KeyError) as error:
                    self._json(404 if isinstance(error, KeyError) else 400,
                               {"error": str(error)})
                except (DraftCorruptError, WebQuestServiceError, OSError):
                    self._json(503, {"error": "受管上传暂不可写，请稍后用同一 chunk 重试"})
                return

            try:
                maximum = (
                    _MAX_DRAFT_HTTP_BODY_BYTES
                    if u.path == "/api/quest-drafts" else _MAX_HTTP_BODY_BYTES)
                body = self._read_json_object(maximum=maximum)
            except (ValueError, json.JSONDecodeError) as error:
                self._json(400, {"error": str(error)})
                return

            web_routes = {
                "/api/quest-drafts",
                "/api/quest-drafts/file/begin",
                "/api/quest-drafts/file/finalize",
                "/api/quest-drafts/local-sources",
                "/api/quest-drafts/preflight",
                "/api/quest-drafts/publish",
                "/api/quest-runtime-profile",
                "/api/quest-control",
                "/api/file-request-uploads",
                "/api/file-request-uploads/file/begin",
                "/api/file-request-uploads/file/finalize",
                "/api/file-request-uploads/publish",
            }
            if u.path in web_routes:
                if not self._is_multi_quest():
                    self._json(404, {"error": "当前服务未启用 Web-first 多任务模式"})
                    return
                try:
                    web = data.require_web_service()
                    stored_key = _stored_idempotency_key(client_key)
                    if u.path == "/api/quest-drafts":
                        if "runtime_profile" in body:
                            web.validate_runtime_profile(body["runtime_profile"])
                        web.bind_operation(client_key, u.path, body)
                        result = web.drafts.create(body, client_key)
                        self._json(201 if result.get("created") else 200, {
                            "ok": True, "draft": result,
                            "idempotency_key": stored_key,
                        })
                        return
                    if u.path == "/api/quest-drafts/file/begin":
                        expected = {"draft_id", "path", "size"}
                        if set(body) != expected:
                            raise ValueError("file begin 字段须恰为 draft_id/path/size")
                        web.bind_operation(client_key, u.path, body)
                        result = web.drafts.begin_file(
                            body["draft_id"], body["path"], body["size"])
                        self._json(200, {"ok": True, "file": result,
                                         "idempotency_key": stored_key})
                        return
                    if u.path == "/api/quest-drafts/file/finalize":
                        expected = {"draft_id", "path", "sha256"}
                        if set(body) != expected:
                            raise ValueError("file finalize 字段须恰为 draft_id/path/sha256")
                        web.bind_operation(client_key, u.path, body)
                        result = web.drafts.finalize_file(
                            body["draft_id"], body["path"], body["sha256"])
                        self._json(200, {"ok": True, "file": result,
                                         "idempotency_key": stored_key})
                        return
                    if u.path == "/api/quest-drafts/local-sources":
                        if set(body) != {"draft_id", "kind", "path"}:
                            raise ValueError(
                                "local source 字段须恰为 draft_id/kind/path")
                        result = web.attach_local_source(
                            body["draft_id"], body["kind"], body["path"],
                            client_key)
                        self._json(201, {"ok": True, "source": result,
                                         "idempotency_key": stored_key})
                        return
                    if u.path == "/api/quest-drafts/preflight":
                        if set(body) != {"draft_id"}:
                            raise ValueError("preflight 字段须恰为 draft_id")
                        web.bind_operation(client_key, u.path, body)
                        result = web.preflight(body["draft_id"])
                        self._json(200, {"ok": True, "preflight": result,
                                         "idempotency_key": stored_key})
                        return
                    if u.path == "/api/quest-drafts/publish":
                        if set(body) != {"draft_id", "start"}:
                            raise ValueError("publish 字段须恰为 draft_id/start")
                        if web.publish_needs_background(body["draft_id"]):
                            job = web.submit_publish(
                                body["draft_id"], start=body["start"],
                                idempotency_key=client_key)
                            self._json(202, {
                                "ok": True, "job": job,
                                "idempotency_key": stored_key})
                        else:
                            result = web.publish(
                                body["draft_id"], start=body["start"],
                                idempotency_key=client_key)
                            self._json(201, {"ok": True, **result,
                                             "idempotency_key": stored_key})
                        return
                    if u.path == "/api/quest-control":
                        if set(body) != {"quest_id", "action"}:
                            raise ValueError("quest control 字段须恰为 quest_id/action")
                        action = body["action"]
                        if action == "start":
                            runtime = web.start(body["quest_id"], client_key)
                        elif action == "terminate":
                            runtime = web.terminate(body["quest_id"], client_key)
                        else:
                            raise ValueError("quest control action 须为 start 或 terminate")
                        self._json(200, {"ok": True, "runtime": runtime,
                                         "idempotency_key": stored_key})
                        return
                    if u.path == "/api/quest-runtime-profile":
                        if set(body) != {"quest_id", "runtime_profile"}:
                            raise ValueError(
                                "runtime profile 字段须恰为 quest_id/runtime_profile")
                        result = web.update_runtime_profile(
                            body["quest_id"], body["runtime_profile"], client_key)
                        self._json(200, {
                            "ok": True, **result,
                            "idempotency_key": stored_key,
                        })
                        return

                    if u.path == "/api/file-request-uploads":
                        if set(body) != {"quest_id", "request_id"}:
                            raise ValueError("request upload 字段须恰为 quest_id/request_id")
                        result = web.create_request_upload(
                            body["quest_id"], body["request_id"], client_key)
                        self._json(201, {"ok": True, **result,
                                         "idempotency_key": stored_key})
                        return
                    if u.path == "/api/file-request-uploads/file/begin":
                        expected = {"quest_id", "request_id", "upload_id", "path", "size"}
                        if set(body) != expected:
                            raise ValueError(
                                "request file begin 字段须恰为 quest_id/request_id/upload_id/path/size")
                        web.bind_operation(client_key, u.path, body)
                        result = web.request_begin_file(
                            body["quest_id"], body["request_id"], body["upload_id"],
                            body["path"], body["size"])
                        self._json(200, {"ok": True, "file": result,
                                         "idempotency_key": stored_key})
                        return
                    if u.path == "/api/file-request-uploads/file/finalize":
                        expected = {
                            "quest_id", "request_id", "upload_id", "path", "sha256"}
                        if set(body) != expected:
                            raise ValueError(
                                "request file finalize 字段须恰为 quest_id/request_id/upload_id/path/sha256")
                        web.bind_operation(client_key, u.path, body)
                        result = web.request_finalize_file(
                            body["quest_id"], body["request_id"], body["upload_id"],
                            body["path"], body["sha256"])
                        self._json(200, {"ok": True, "file": result,
                                         "idempotency_key": stored_key})
                        return
                    if u.path == "/api/file-request-uploads/publish":
                        expected = {"quest_id", "request_id", "upload_id"}
                        if set(body) != expected:
                            raise ValueError(
                                "request upload publish 字段须恰为 quest_id/request_id/upload_id")
                        publication = web.publish_request_upload(
                            body["quest_id"], body["request_id"], body["upload_id"],
                            client_key)
                        selected = data.quest_data(publication.quest_id)
                        rec = selected.enqueue_file_request_action(
                            action="resolve", request_id=publication.request_id,
                            source_ref=publication.source_ref, reason="",
                            client_idempotency_key=client_key)
                        self._json(200, {"ok": True, "queued": rec,
                                         "upload_id": publication.upload_id,
                                         "idempotency_key": stored_key})
                        return
                except WebQuestRetryableError as error:
                    # The mutation is already durable, but its cycle-boundary
                    # restart side effect is not settled.  This is explicitly
                    # non-definitive: echo the exact HTTP operation key so the
                    # browser can prove which pending request must be retried.
                    if (error.operation_state != "saved_pending_restart"
                            or error.idempotency_key != client_key):
                        self._json(503, {
                            "error": "运行配置已保存但恢复凭据不一致；请保留原请求并重试",
                        })
                    else:
                        self._json(503, {
                            "error": str(error),
                            "retryable": True,
                            "operation_state": error.operation_state,
                            "idempotency_key": stored_key,
                        })
                except (DraftConflictError, QuestConflictError,
                        LocalSourceConflictError,
                        WebQuestConflictError) as error:
                    self._json(409, {"error": str(error)})
                except (WebQuestNotReadyError,
                        QuestProcessUnavailableError) as error:
                    self._json(409, {"error": str(error)})
                except LocalSourceChangedError as error:
                    self._json(409, {"error": str(error)})
                except LocalSourceCorruptError:
                    self._json(503, {"error": "本机目录附件账本损坏或暂不可读"})
                except (ValueError, DatasetPreflightError, LocalSourceError,
                        json.JSONDecodeError) as error:
                    self._json(400, {"error": str(error)})
                except KeyError as error:
                    self._json(404, {"error": str(error)})
                except (DraftCorruptError, QuestCorruptError,
                        WebQuestServiceError, QuestProcessManagerError,
                        sqlite3.Error, SharedSQLiteReaderUnavailable, OSError):
                    self._json(503, {"error": "Web setup/上传服务暂不可用，请稍后重试"})
                return

            if u.path == "/api/quests":
                if not self._is_multi_quest():
                    self._json(404, {"error": "当前服务未启用多 quest 模式"})
                    return
                try:
                    quest = data.create_quest(body, idempotency_key=client_key)
                    self._json(201 if quest.created else 200,
                               {"ok": True, "quest": quest.public_dict(),
                                "idempotency_key": _stored_idempotency_key(client_key)})
                except QuestConflictError as error:
                    self._json(409, {"error": str(error)})
                except (ValueError, json.JSONDecodeError) as error:
                    self._json(400, {"error": str(error)})
                except OSError:
                    self._json(503, {"error": "quest registry 暂不可写，请稍后重试"})
                return
            if u.path in ("/api/message", "/api/query", "/api/directive", "/api/file-request"):
                try:
                    selected = self._post_data(body)
                except (ValueError, KeyError) as error:
                    self._json(404 if isinstance(error, KeyError) else 400,
                               {"error": str(error)})
                    return
                except QuestCorruptError:
                    self._json(503, {"error": "quest registry 损坏或暂不可读"})
                    return
            else:
                self._json(404, {"error": "未知路由"})
                return
            if u.path == "/api/message":
                try:
                    # connector 固定 "console"（codex NIT：不许客户端伪造成其他来源；控制台入口即 console）
                    rec = selected.enqueue_message(
                        body.get("text", ""), conversation_id=body.get("conversation_id"),
                        client_idempotency_key=client_key)
                    self._json(200, {"ok": True, "queued": rec})
                except (ValueError, json.JSONDecodeError) as e:
                    self._json(400, {"error": str(e)})
                except OSError:
                    self._json(503, {"error": "console spool 暂不可写，请稍后重试"})
                return
            if u.path == "/api/query":
                try:
                    rec = selected.enqueue_query(
                        body.get("text", ""), conversation_id=body.get("conversation_id"),
                        client_idempotency_key=client_key)
                    self._json(200, {"ok": True, "queued": rec})
                except (ValueError, json.JSONDecodeError) as e:
                    self._json(400, {"error": str(e)})
                except OSError:
                    self._json(503, {"error": "console spool 暂不可写，请稍后重试"})
                return
            if u.path == "/api/directive":
                try:
                    # connector 同样固定为 console；server 只追加 spool，不查 directive 表。
                    rec = selected.enqueue_directive_action(
                        action=body.get("action"), directive_id=body.get("directive_id"),
                        reason=body.get("reason", ""),
                        conversation_id=body.get("conversation_id"),
                        client_idempotency_key=client_key)
                    self._json(200, {"ok": True, "queued": rec})
                except (ValueError, json.JSONDecodeError) as e:
                    self._json(400, {"error": str(e)})
                except OSError:
                    self._json(503, {"error": "console spool 暂不可写，请稍后重试"})
                return
            if u.path == "/api/file-request":
                try:
                    # File resolution is created internally by the managed-upload
                    # publication route.  This route only accepts binary permission
                    # approval or rejection/cancellation; it never accepts a path.
                    allowed = {"action", "request_id", "reason"}
                    if self._is_multi_quest():
                        allowed.add("quest_id")
                    if body.get("action") not in ("approve", "cancel") or set(body) != allowed:
                        raise ValueError(
                            "文件请求解决须使用 Web 的‘选择并上传’流程；"
                            "该接口只接受权限 approve 或请求 cancel")
                    rec = selected.enqueue_file_request_action(
                        action=body.get("action"), request_id=body.get("request_id"),
                        reason=body.get("reason", ""),
                        client_idempotency_key=client_key)
                    self._json(200, {"ok": True, "queued": rec})
                except (ValueError, json.JSONDecodeError) as e:
                    self._json(400, {"error": str(e)})
                except (sqlite3.Error, SharedSQLiteReaderUnavailable):
                    self._json(503, {"error": "研究库暂不可读，请稍后重试"})
                except OSError:
                    self._json(503, {"error": "console spool/上传目录暂不可用，请稍后重试"})
                return
            self._json(500, {"error": "内部路由未收敛"})

        def _unsupported_method(self):
            if not self._request_host_ok():
                self._json(421, {"error": "Host 必须是 loopback 地址（防 DNS rebinding）"})
                return
            path = urlparse(self.path).path
            if not self._require_api_authorization(path):
                return
            self._json(405, {"error": "该路由只支持 GET/POST"},
                       extra_headers={"Allow": "GET, POST"})

        do_PUT = _unsupported_method
        do_PATCH = _unsupported_method
        do_DELETE = _unsupported_method
        do_OPTIONS = _unsupported_method
        do_HEAD = _unsupported_method

        def _serve_static(self, path: str):
            if static_dir is None:
                self._json(404, {"error": "未配静态目录"})
                return
            rel = "index.html" if path in ("/", "") else path.lstrip("/")
            parts = rel.split("/")
            if ".." in parts or any(part.startswith(".") for part in parts):
                self._json(403, {"error": "路径越界"})
                return
            try:
                body = read_regular_file_beneath(
                    static_dir, rel, max_bytes=_MAX_STATIC_RESPONSE_BYTES)
            except (OSError, ValueError, RuntimeError):
                self._json(404, {"error": f"不存在: {rel}"})
                return
            suffix = Path(rel).suffix
            ctype = "text/html; charset=utf-8" if suffix in (".html", ".htm") else \
                    "application/javascript" if suffix == ".js" else \
                    "text/css" if suffix == ".css" else "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)
    return Handler


def serve(db_path: str, work_root: str, system_root: str, *, host: str = "127.0.0.1", port: int = 8765,
          static_dir: Optional[str] = None,
          capability_token: Optional[str] = None) -> ThreadingHTTPServer:
    """起控制台服务（阻塞前调用方 serve_forever）。

    控制面只允许 loopback 监听；远程运维请用 SSH tunnel。这样在尚未引入 TLS/身份系统前不会把
    confirm/reject/resolve/cancel 暴露到局域网。static_dir 默认 ``system_root/views/console``。
    """
    if not _is_loopback_host(host):
        raise ValueError("console_server 只允许 loopback host；远程访问请使用 SSH tunnel")
    sd = Path(static_dir) if static_dir else (Path(system_root) / "views" / "console")
    spool = ConsoleSpool(work_root)
    bearer = spool.load_or_create_capability(capability_token)
    data = ConsoleData(db_path=db_path, work_root=work_root, system_root=system_root,
                       spool=spool, capability_token=bearer)
    httpd = BoundedThreadingHTTPServer((host, port), make_handler(data, sd if sd.exists() else None))
    # Read-only test/embedding introspection and the local browser bootstrap.
    # The server-owned 0600 backing file is never part of the user workflow.
    httpd.console_capability_token = bearer
    return httpd


def serve_quests(quests_root: Union[str, Path], system_root: Union[str, Path], *,
                 host: str = "127.0.0.1", port: int = 8765,
                 static_dir: Optional[str] = None,
                 capability_token: Optional[str] = None,
                 qualification_profiles_root: Optional[Union[str, Path]] = None,
                 connector_profile: Optional[Union[str, Path]] = None,
                 no_outbound: bool = True,
                 max_cycles: int = 100,
                 poll_interval_s: float = 1.0,
                 local_import_roots: Optional[List[Union[str, Path]]] = None,
                 ) -> ThreadingHTTPServer:
    """Serve the local Web product over physically isolated quests.

    This constructor owns the complete post-deployment path: draft uploads,
    dataset preflight, immutable quest publication and the local research
    process capability.  Qualification profiles remain deployment-owned
    inputs and are projected to the browser only through redacted metadata.
    """
    if not _is_loopback_host(host):
        raise ValueError("console_server 只允许 loopback host；远程访问请使用 SSH tunnel")
    registry = QuestRegistry(Path(quests_root), Path(system_root))
    sd = Path(static_dir) if static_dir else (Path(system_root) / "views" / "console")
    spool = ConsoleSpool(registry.root)
    bearer = spool.load_or_create_capability(capability_token)
    drafts = QuestDraftRegistry(registry.state_dir / "quest-drafts")
    profile_root = (
        Path(qualification_profiles_root)
        if qualification_profiles_root is not None
        else registry.root / "qualification-profiles")
    profiles = QualificationProfileRegistry(profile_root)
    local_sources = LocalSourceRegistry(
        registry.state_dir / "local-sources",
        allowed_roots=(
            [Path("/")] if local_import_roots is None else local_import_roots))
    processes = QuestProcessManager(
        registry, system_root,
        connector_profile=connector_profile,
        no_outbound=no_outbound,
        max_cycles=max_cycles,
        poll_interval_s=poll_interval_s,
    )
    web = WebQuestService(
        registry=registry, drafts=drafts, profiles=profiles,
        processes=processes, local_sources=local_sources)
    data = QuestConsoleData(
        registry=registry, system_root=str(system_root), capability_token=bearer,
        web_service=web)
    httpd = BoundedThreadingHTTPServer(
        (host, port), make_handler(data, sd if sd.exists() else None))
    httpd.add_close_callback(web.close)
    httpd.console_capability_token = bearer
    return httpd


def _configure_process_storage(root: Union[str, Path]) -> Dict[str, str]:
    """Keep every mutable host/Codex runtime path beneath the data root."""
    # The console is also responsible for creating a new data root.  Resolve
    # the prospective path first, then create and re-resolve it before placing
    # any process scratch beneath it.
    base = Path(root).expanduser().resolve(strict=False)
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    base = base.resolve(strict=True)
    if stat.S_ISLNK(base.lstat().st_mode):
        raise ValueError("运行时数据根不得是 symlink")

    service_uid, service_gid = os.geteuid(), os.getegid()
    requested_query_user = os.environ.get("METARESEARCH_QUERY_RUN_AS_USER")
    if requested_query_user is None:
        requested_query_user = (
            "codexro" if service_uid == 0 else pwd.getpwuid(service_uid).pw_name
        )
    query_account = pwd.getpwnam(requested_query_user)
    if service_uid != 0 and query_account.pw_uid != service_uid:
        raise ValueError("non-root 服务不得把 Codex 运行目录交给其他 UID")

    def ensure_directory(path: Path, *, uid: int, gid: int, mode: int) -> Path:
        if path.exists() and path.is_symlink():
            raise ValueError("运行时目录不得是 symlink")
        path.mkdir(parents=True, exist_ok=True, mode=mode)
        info = os.lstat(path)
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or os.path.realpath(path) != str(path)):
            raise ValueError("运行时目录身份非法")
        if service_uid == 0:
            os.chown(path, uid, gid)
        elif (info.st_uid, info.st_gid) != (uid, gid):
            raise ValueError("运行时目录 owner 非当前服务 UID")
        os.chmod(path, mode)
        return path

    # Execute-only shared parents let the dedicated codexro UID reach only its
    # private children; quests/ and state/ retain their existing 0700 boundary.
    os.chmod(base, 0o711)
    process_tmp = ensure_directory(
        base / ".process-tmp", uid=service_uid, gid=service_gid, mode=0o711)
    cache_root = ensure_directory(
        base / ".process-cache", uid=service_uid, gid=service_gid, mode=0o711)
    home_root = ensure_directory(
        base / ".process-home", uid=service_uid, gid=service_gid, mode=0o711)
    codex_root = ensure_directory(
        base / ".codex-runtime", uid=service_uid, gid=service_gid, mode=0o711)

    service_cache = ensure_directory(
        cache_root / "service", uid=service_uid, gid=service_gid, mode=0o700)
    query_cache = ensure_directory(
        cache_root / "query", uid=query_account.pw_uid,
        gid=query_account.pw_gid, mode=0o700)
    service_home = ensure_directory(
        home_root / "service", uid=service_uid, gid=service_gid, mode=0o700)
    query_home = ensure_directory(
        home_root / "query", uid=query_account.pw_uid,
        gid=query_account.pw_gid, mode=0o700)

    prior_service_codex_home = os.environ.get("CODEX_HOME")
    prior_query_codex_home = os.environ.get("METARESEARCH_QUERY_CODEX_HOME")
    if prior_query_codex_home is None:
        prior_query_codex_home = str(Path(query_account.pw_dir) / ".codex")
    service_codex_home = ensure_directory(
        codex_root / "service", uid=service_uid, gid=service_gid, mode=0o700)
    query_codex_home = ensure_directory(
        codex_root / "query", uid=query_account.pw_uid,
        gid=query_account.pw_gid, mode=0o700)

    def seed_codex_identity(destination: Path, source: Optional[str], *,
                            uid: int, gid: int) -> None:
        source_root = Path(source).expanduser() if source else None
        for name in ("auth.json", "config.toml"):
            target = destination / name
            if target.exists() or target.is_symlink():
                info = os.lstat(target)
                if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                        or info.st_uid != uid or info.st_gid != gid
                        or stat.S_IMODE(info.st_mode) != 0o600):
                    raise ValueError(f"项目 Codex {name} 身份/权限非法")
                continue
            if source_root is None:
                continue
            source_path = source_root / name
            try:
                source_info = os.lstat(source_path)
            except FileNotFoundError:
                continue
            if (not stat.S_ISREG(source_info.st_mode)
                    or stat.S_ISLNK(source_info.st_mode)
                    or source_info.st_size > 1024 * 1024):
                raise ValueError(f"Codex bootstrap {name} 非有界普通文件")
            payload = source_path.read_bytes()
            if len(payload) != source_info.st_size:
                raise ValueError(f"Codex bootstrap {name} 读取漂移")
            flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
            fd = os.open(target, flags, 0o600)
            try:
                view = memoryview(payload)
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
                os.fchmod(fd, 0o600)
                if service_uid == 0:
                    os.fchown(fd, uid, gid)
                os.fsync(fd)
            except BaseException:
                os.close(fd)
                target.unlink(missing_ok=True)
                raise
            else:
                os.close(fd)

    seed_codex_identity(
        service_codex_home, prior_service_codex_home,
        uid=service_uid, gid=service_gid)
    seed_codex_identity(
        query_codex_home, prior_query_codex_home,
        uid=query_account.pw_uid, gid=query_account.pw_gid)

    paths = {
        "TMPDIR": process_tmp,
        "XDG_CACHE_HOME": service_cache,
        "PIP_CACHE_DIR": service_cache / "pip",
    }
    ensure_directory(
        paths["PIP_CACHE_DIR"], uid=service_uid, gid=service_gid, mode=0o700)
    for key, path in paths.items():
        os.environ[key] = str(path)
    os.environ["TMP"] = str(paths["TMPDIR"])
    os.environ["TEMP"] = str(paths["TMPDIR"])
    os.environ["HOME"] = str(service_home)
    os.environ["CODEX_HOME"] = str(service_codex_home)
    os.environ["HF_HOME"] = str(service_cache / "huggingface")
    os.environ["TORCH_HOME"] = str(service_cache / "torch")
    # Package managers and compute libraries otherwise fall back to the
    # service account's installation prefix (often /root on a small cloud
    # root disk).  Bind their mutable state to the same VEPFS data tree.  HOME
    # already catches most tools; these explicit variables cover tools whose
    # cache location ignores HOME or follows the executable's base prefix.
    extra_service_storage = {
        "XDG_CONFIG_HOME": service_home / ".config",
        "XDG_DATA_HOME": service_home / ".local" / "share",
        "XDG_STATE_HOME": service_home / ".local" / "state",
        "CONDA_PKGS_DIRS": service_cache / "conda-pkgs",
        "CONDA_ENVS_PATH": base / "environments",
        "UV_CACHE_DIR": service_cache / "uv",
        "CUDA_CACHE_PATH": service_cache / "cuda",
        "MPLCONFIGDIR": service_cache / "matplotlib",
        "NUMBA_CACHE_DIR": service_cache / "numba",
    }
    for key, path in extra_service_storage.items():
        ensure_directory(path, uid=service_uid, gid=service_gid, mode=0o700)
        os.environ[key] = str(path)
    os.environ["METARESEARCH_QUERY_HOME"] = str(query_home)
    os.environ["METARESEARCH_QUERY_CODEX_HOME"] = str(query_codex_home)
    os.environ["METARESEARCH_QUERY_CACHE_HOME"] = str(query_cache)
    if "SSL_CERT_FILE" not in os.environ:
        prefix = Path(sys.prefix).resolve(strict=True)
        certificate = prefix / "ssl" / "cert.pem"
        if (certificate.is_file()
                and os.path.commonpath(
                    (str(prefix), str(certificate.resolve(strict=True)))) == str(prefix)):
            os.environ["SSL_CERT_FILE"] = str(certificate)
    tempfile.tempdir = str(paths["TMPDIR"])
    return {key: str(path) for key, path in paths.items()}


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="meta-research 人类控制台数据面服务（只读 + spool 入站；步⑨）")
    ap.add_argument("--system-root", required=True, help="仓库根（含 policies/schemas/prompts/views）")
    roots = ap.add_mutually_exclusive_group(required=True)
    roots.add_argument("--work-root", help="单 quest 运行产物根（research.sqlite / state 落此，同 run.py）")
    roots.add_argument("--quests-root", help="多 quest registry 根；每个 quest 拥有独立 work-root/SQLite")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument(
        "--no-open-browser", action="store_true",
        help="不自动打开浏览器；在本机启动终端显示可复制的授权链接")
    ap.add_argument(
        "--qualification-profiles-root",
        help="部署方生成的 qualification profile 目录；内容不会暴露给 Web")
    ap.add_argument(
        "--local-import-root", action="append", default=None,
        help="Web 可附加的本机目录根（可重复；单机默认使用当前 UID 可读范围）")
    ap.add_argument("--max-cycles", type=int, default=100)
    ap.add_argument("--poll-interval-s", type=float, default=1.0)
    outbound = ap.add_mutually_exclusive_group()
    outbound.add_argument("--connector-profile")
    outbound.add_argument(
        "--no-outbound", action="store_true",
        help="本机运行不对外投递通知（多 quest Web owner 的默认值）")
    args = ap.parse_args(argv)
    _configure_process_storage(args.quests_root or args.work_root)
    if args.quests_root:
        httpd = serve_quests(
            args.quests_root, args.system_root, host=args.host, port=args.port,
            qualification_profiles_root=args.qualification_profiles_root,
            connector_profile=args.connector_profile,
            no_outbound=(args.no_outbound or args.connector_profile is None),
            max_cycles=args.max_cycles,
            poll_interval_s=args.poll_interval_s,
            local_import_roots=args.local_import_root)
        scope = f"多 quest registry {Path(args.quests_root).resolve()}"
    else:
        db_path = str(Path(args.work_root) / "research.sqlite")
        httpd = serve(
            db_path, args.work_root, args.system_root, host=args.host, port=args.port)
        scope = f"只读库 {db_path}"
    bound_port = int(httpd.server_address[1])
    browser_url = _authenticated_console_url(
        args.host, bound_port, httpd.console_capability_token)
    opened = False
    if not args.no_open_browser:
        try:
            import webbrowser
            opened = bool(webbrowser.open_new_tab(browser_url))
        except (OSError, RuntimeError, webbrowser.Error):
            opened = False
    print(f"[console] Web 产品已监听 http://{args.host}:{bound_port}  （{scope}；Ctrl-C 停）")
    if opened:
        print("[console] 已在默认浏览器打开；部署后的任务与文件操作均在 Web 内完成。")
    else:
        print("[console] 浏览器未自动打开；请在这台机器的浏览器中打开下面的授权链接：")
        print(browser_url)
        print("[console] 此链接等同本机会话凭证，请勿转发、公开或粘贴到工单/聊天记录。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
