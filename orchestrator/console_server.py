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

import hmac
import ipaddress
import json
import math
import os
import re
import sqlite3
import stat
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from urllib.parse import quote, urlparse

import yaml

from .ids import parse_positive_sqlite_int
from .console import directive_action_text
from .console_spool import (CAPABILITY_NAME, ConsoleSpool, normalize_upload_ref,
                            open_directory_path, read_regular_file_beneath,
                            stat_regular_file_beneath)
from .instance_lease import read_instance_status
from .database import journal_mode_for_path
from .process_supervisor import read_receipt


_MAX_HTTP_BODY_BYTES = 64 * 1024
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
_MAX_POLICY_NODES = 10_000
_MAX_POLICY_DEPTH = 32
_MAX_POLICY_STRING_CHARS = 64 * 1024
_MAX_FS_NODES = 2_000
_MAX_FS_ENTRIES_PER_DIRECTORY = 200
_HTTP_SOCKET_TIMEOUT_S = 10.0
_MAX_HTTP_WORKERS = 16
_FS_DIRECTORY_FLAGS = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                       | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
                       | getattr(os, "O_NONBLOCK", 0))

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


def _runner_execution_live(work_root: Path, runner_call_id: int) -> Dict[str, Any]:
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


def _live(conn: sqlite3.Connection, work_root: Path) -> Dict[str, Any]:
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
            live.update(_runner_execution_live(work_root, int(rc[0])))
    return live


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


def _fs_tree(work_root: Path, system_root: Path) -> Dict[str, Any]:
    """真文件树（控制台文件浏览器）：work_root 运行产物 + system_root 的 schemas/prompts/policies（只读展示）。
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

    def walk(directory_fd: int, depth: int) -> List[Dict[str, Any]]:
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
                        node["children"] = walk(child_fd, depth + 1)
                    finally:
                        os.close(child_fd)
            else:
                node["size"] = info.st_size if stat.S_ISREG(info.st_mode) else None
            nodes.append(node)
        return nodes

    def root_node(label: str, path: Path, *, required: bool) -> Optional[Dict[str, Any]]:
        fd = open_root(path)
        if fd is None:
            return {"p": label, "dir": True, "children": []} if required else None
        try:
            return {"p": label, "dir": True, "children": walk(fd, 0)}
        finally:
            os.close(fd)

    roots = [root_node("work", work_root, required=True)]
    for sub in ("schemas", "prompts", "policies", "input"):
        node = root_node(sub, system_root / sub, required=False)
        if node is not None:
            roots.append(node)
    return {"roots": roots}


def assemble_db(db_path: str, work_root: str, system_root: str) -> Dict[str, Any]:
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
        payload["live"] = _live(conn, Path(work_root))
        payload["notification"] = _notifications(Path(work_root))
        payload["ledger_by_cycle"] = _ledger_by_cycle(conn)
    finally:
        conn.close()
    try:                                   # policy 解析失败**不拖垮整个仪表盘**
        raw_policy = read_regular_file_beneath(
            system_root, "policies/policy.yaml", max_bytes=_MAX_POLICY_BYTES)
        payload["policy"] = _load_bounded_policy(raw_policy)
    except (UnicodeDecodeError, yaml.YAMLError, OSError, ValueError, RuntimeError):
        payload["policy"] = {}
    payload["fs"] = _fs_tree(Path(work_root), Path(system_root))
    return payload


class ConsoleData:
    """控制台数据源 + 入站 spool（不含 HTTP；供 handler 与测试共用）。只读库 + 只写 inbox spool。"""

    def __init__(self, *, db_path: str, work_root: str, system_root: str,
                 spool: Optional[ConsoleSpool] = None,
                 capability_token: Optional[str] = None):
        self.db_path = db_path
        self.work_root = Path(work_root)
        self.system_root = Path(system_root)
        self.spool = spool or ConsoleSpool(self.work_root)
        self.inbox = self.spool.inbox_path
        self.capability_token = capability_token

    def db(self) -> Dict[str, Any]:
        return assemble_db(self.db_path, str(self.work_root), str(self.system_root))

    def _virtual_root(self, seg: str) -> Optional[Path]:
        """FS 树暴露的虚拟根 → 真目录（显式映射，不靠 base.parent 猜——codex SHOULD：--work-root 叫任意名
        时 base.parent/rel 拼法会 404）。work→work_root；schemas/prompts/policies/input→system_root/<seg>。"""
        if seg == "work":
            return self.work_root
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
        """把文件请求 resolve/cancel 控件动作追加到 spool；研究库仍只读。

        HTTP 层用 mode=ro 只核请求身份存在；状态迁移完全由 run 进程在单写域重核。不能用可变的
        pending/terminal 状态拦 append：首次动作可能已经入 spool 并迁终态、但 HTTP ACK 丢失，客户端
        必须能用同一 Idempotency-Key 重放并由 resolved_message_id provenance 收敛。resolve 只携安全
        虚拟目录引用，不把任意绝对路径送入 spool。
        """
        action = str(action or "").strip().lower()
        if action not in ("resolve", "cancel"):
            raise ValueError("action 须为 resolve 或 cancel")
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
        else:
            if not isinstance(reason, str):
                raise ValueError("取消理由须为字符串")
            reason = reason.strip() or "用户从控制台取消文件请求"
            if len(reason) > _MAX_REASON_CHARS:
                raise ValueError(f"取消理由过长（最多 {_MAX_REASON_CHARS} 字符）")
            rec["reason"] = reason
            rec["raw_text"] = f"取消文件请求 r{rid}：{reason}"
        return self._enqueue(rec, client_idempotency_key=client_idempotency_key)


def make_handler(data: ConsoleData, static_dir: Optional[Path]):
    """构造 HTTP handler 类（闭包持 data + 静态目录）。路由：
    GET /api/db · GET /api/file?p=… · POST /api/message{text} ·
    POST /api/directive{action,directive_id,reason?} ·
    POST /api/file-request{action,request_id,source_ref?|reason?} · GET /（静态控制台页）。"""
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

        def _read_json_object(self) -> Dict[str, Any]:
            if self.headers.get_content_type().lower() != "application/json":
                raise ValueError("Content-Type 必须为 application/json")
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
            if length < 0 or length > _MAX_HTTP_BODY_BYTES:
                raise ValueError(f"请求体大小须在 0..{_MAX_HTTP_BODY_BYTES} 字节")
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

        def _request_idempotency_key(self) -> str:
            values = self.headers.get_all("Idempotency-Key") or []
            if len(values) != 1:
                raise ValueError("POST 须提供唯一 Idempotency-Key")
            _stored_idempotency_key(values[0])                 # canonical shape validation
            return values[0]

        def do_GET(self):
            if not self._request_host_ok():
                self._json(421, {"error": "Host 必须是 loopback 地址（防 DNS rebinding）"})
                return
            u = urlparse(self.path)
            if not self._require_api_authorization(u.path):
                return
            if u.path == "/api/db":
                try:
                    self._json(200, data.db())
                except SharedSQLiteReaderUnavailable:
                    self._json(503, {"error": "研究库暂不可在本节点读取，请稍后重试"})
                except Exception:                     # 只读观测面：组装失败向客户端**泛化报**（不泄内部细节/路径，
                    import traceback                   # codex SHOULD）；真实细节写 stderr 供运维排障（codex 第2轮
                    traceback.print_exc()              # NIT：文案承诺「详见服务端日志」须真有日志，否则线上排障盲）
                    self._json(500, {"error": "内部错误：/api/db 组装失败（详见服务端日志）"})
                return
            if u.path == "/api/file":
                from urllib.parse import parse_qs
                rel = (parse_qs(u.query).get("p") or [""])[0]
                content = data.read_file(rel)
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
            if u.path == "/api/message":
                try:
                    body = self._read_json_object()
                    # connector 固定 "console"（codex NIT：不许客户端伪造成其他来源；控制台入口即 console）
                    rec = data.enqueue_message(
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
                    body = self._read_json_object()
                    # connector 同样固定为 console；server 只追加 spool，不查 directive 表。
                    rec = data.enqueue_directive_action(action=body.get("action"),
                                                        directive_id=body.get("directive_id"),
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
                    body = self._read_json_object()
                    rec = data.enqueue_file_request_action(
                        action=body.get("action"), request_id=body.get("request_id"),
                        source_ref=body.get("source_ref"), reason=body.get("reason", ""),
                        client_idempotency_key=client_key)
                    self._json(200, {"ok": True, "queued": rec})
                except (ValueError, json.JSONDecodeError) as e:
                    self._json(400, {"error": str(e)})
                except (sqlite3.Error, SharedSQLiteReaderUnavailable):
                    self._json(503, {"error": "研究库暂不可读，请稍后重试"})
                except OSError:
                    self._json(503, {"error": "console spool/上传目录暂不可用，请稍后重试"})
                return
            self._json(404, {"error": "未知路由"})

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
    # Read-only test/embedding introspection; production clients load the 0600
    # capability file rather than scraping stdout or server internals.
    httpd.console_capability_token = bearer
    return httpd


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="meta-research 人类控制台数据面服务（只读 + spool 入站；步⑨）")
    ap.add_argument("--system-root", required=True, help="仓库根（含 policies/schemas/prompts/views）")
    ap.add_argument("--work-root", required=True, help="运行产物根（research.sqlite / state 落此，同 run.py）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args(argv)
    db_path = str(Path(args.work_root) / "research.sqlite")
    httpd = serve(db_path, args.work_root, args.system_root, host=args.host, port=args.port)
    capability_path = Path(args.work_root) / "state" / CAPABILITY_NAME
    print(f"[console] 数据面 http://{args.host}:{args.port}  （只读库 {db_path}；Ctrl-C 停）\n"
          f"[console] Bearer capability: {capability_path}（0600；勿复制到日志/网页）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
