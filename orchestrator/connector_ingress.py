"""Authenticated durable connector ingress.

The network listener never writes SQLite.  It authenticates and normalizes a
provider event into a closed ``natural_language`` envelope, fsyncs that record
to a channel-isolated spool, and only then returns the transport ACK.  The run
process later consumes the spool through ``poll``/``commit_poll`` and remains
the sole database writer.

Two wire formats are supported:

* ``webhook_v1``: a strict HMAC-SHA256 gateway contract with freshness,
  audience and event identity bound into the signature;
* ``onebot_v11_http_post``: the standard OneBot reverse HTTP POST event shape,
  authenticated with its HMAC-SHA1 ``X-Signature`` and restricted to the
  configured bot, target and operator allowlist.

Provider-controlled JSON can contribute only the external message identity and
plain text.  Connector/channel/conversation/principal/session/idempotency are
derived from the trusted profile and authenticated transport metadata.
"""
from __future__ import annotations

import hashlib
import hmac
import http.client as http_client
import ipaddress
import json
import logging
import math
import re
import socket
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from .console_spool import ConnectorSpool, SpoolBatch, UnsafeConsolePath


logger = logging.getLogger(__name__)

_MAX_BODY_BYTES = 64 * 1024
_MAX_TEXT_CHARS = 20_000
_MAX_SEGMENTS = 64
_MAX_JSON_DEPTH = 8
_MAX_ACCEPTED_IDENTITIES = 100_000
_MAX_HEADER_COUNT = 32
_MAX_HEADER_BYTES = 16 * 1024
_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._/@+-]{0,127}$")
_PATH_RE = re.compile(r"^/[A-Za-z0-9/_-]{0,126}[A-Za-z0-9_-]$")
_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")
_HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_TIMESTAMP_RE = re.compile(r"^(?:0|[1-9][0-9]{0,18})$")


class InboundConfigError(ValueError):
    """An ingress profile is unsafe, ambiguous, or unsupported."""


class InboundStateError(RuntimeError):
    """Durable ingress authority is corrupt or has changed identity."""


class InboundProtocolError(ValueError):
    """A bounded client/provider protocol rejection."""

    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)


def _unique_object(pairs):  # noqa: ANN001
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON key 重复: {key}")
        result[key] = value
    return result


def _strict_json_loads(raw: bytes) -> Any:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("body 不是严格 UTF-8") from error
    return json.loads(
        text, object_pairs_hook=_unique_object,
        parse_constant=lambda token: (_ for _ in ()).throw(
            ValueError(f"JSON 非有限数字: {token}")))


def _json_depth(value: Any, depth: int = 0) -> int:
    if depth > _MAX_JSON_DEPTH:
        raise ValueError(f"JSON 嵌套超过 {_MAX_JSON_DEPTH} 层")
    if isinstance(value, dict):
        for item in value.values():
            _json_depth(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _json_depth(item, depth + 1)
    return depth


def _strict_keys(value: Mapping[str, Any], allowed: set[str], *, label: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"{label} 含未知字段: {sorted(unknown)}")


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False).encode("utf-8")


def _identity(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or _IDENTITY_RE.fullmatch(value) is None:
        raise InboundConfigError(f"{label} 须为 1..128 字符的稳定 ASCII identity")
    return value


def _conversation(value: Any, *, label: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > 128
            or any(ord(ch) < 0x20 or ord(ch) == 0x7f
                   or 0xD800 <= ord(ch) <= 0xDFFF for ch in value)):
        raise InboundConfigError(f"{label} 须为 1..128 字符且不得含控制字符")
    return value


def _positive_int(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 2 ** 63 - 1:
        raise InboundConfigError(f"{label} 须为 int64 正整数")
    return value


def _loopback_literal(value: Any, *, label: str) -> str:
    if not isinstance(value, str):
        raise InboundConfigError(f"{label} 须为 loopback IP 字面量")
    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise InboundConfigError(f"{label} 须为 loopback IP 字面量") from error
    if not address.is_loopback or address.version != 4:
        raise InboundConfigError(f"{label} 当前只允许 IPv4 loopback IP")
    return value


def _listen_port(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise InboundConfigError("inbound.listen_port 须为 1..65535 整数")
    return value


def _listen_path(value: Any) -> str:
    if (not isinstance(value, str) or len(value) > 128 or _PATH_RE.fullmatch(value) is None
            or "//" in value or "/../" in value or value.endswith("/..")):
        raise InboundConfigError("inbound.path 须为不含 query/fragment/dot-segment 的绝对路径")
    return value


def _timeout(value: Any) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(float(value)) or not 0.1 <= float(value) <= 2.0):
        raise InboundConfigError("inbound.request_timeout_s 须在 [0.1, 2.0] 内")
    return float(value)


def _clean_text(value: Any) -> str:
    if not isinstance(value, str):
        raise InboundProtocolError(400, "message text 须为字符串")
    text = value.strip()
    if not text or len(text) > _MAX_TEXT_CHARS:
        raise InboundProtocolError(400, f"message text 须为 1..{_MAX_TEXT_CHARS} 字符")
    if any((ord(ch) < 0x20 and ch not in "\n\t") or ord(ch) == 0x7f
           or 0xD800 <= ord(ch) <= 0xDFFF for ch in text):
        raise InboundProtocolError(400, "message text 含不允许的控制字符")
    return text


@dataclass(frozen=True)
class InboundBinding:
    channel: str
    wire_type: str
    consumer_id: str
    source_id: str
    conversation_id: str
    profile_fingerprint: str
    listen_host: str
    listen_port: int
    path: str
    request_timeout_s: float
    key_id: Optional[str] = None
    max_skew_s: int = 300
    principal_id: Optional[str] = None
    self_id: Optional[int] = None
    target_kind: Optional[str] = None
    target_id: Optional[int] = None
    allowed_user_ids: Tuple[int, ...] = ()
    require_at: bool = False

    @property
    def session_base(self) -> str:
        return f"connector-inbound-v1:{self.profile_fingerprint}"

    def session_ref(self, principal_ref: str, *, action: bool = False) -> str:
        suffix = f":principal:{principal_ref}"
        if action:
            suffix += ":action"
        value = self.session_base + suffix
        if len(value) > 256:
            raise InboundStateError("connector session_ref 超过 DB 契约上限")
        return value


def _event_key(binding: InboundBinding, *, principal_ref: str,
               external_message_id: str) -> str:
    digest = hashlib.sha256(
        ("meta-research-inbound-id-v1\x00" + binding.consumer_id + "\x00"
         + binding.channel + "\x00" + binding.source_id + "\x00"
         + binding.conversation_id + "\x00" + principal_ref + "\x00"
         + external_message_id).encode("utf-8")).hexdigest()[:32]
    return "connector-" + digest


def _source_envelope_hash(binding: InboundBinding, *, principal_ref: str,
                          external_message_id: str, raw_text: str) -> str:
    canonical = _canonical_json({
        "consumer_id": binding.consumer_id,
        "source_id": binding.source_id,
        "conversation_id": binding.conversation_id,
        "principal_ref": principal_ref,
        "external_message_id": external_message_id,
        "raw_text": raw_text,
    })
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _make_envelope(binding: InboundBinding, *, principal_ref: str,
                   external_message_id: str, raw_text: str) -> Dict[str, Any]:
    return {
        "version": 1,
        "record_kind": "natural_language",
        "connector": binding.channel,
        "profile_fingerprint": binding.profile_fingerprint,
        "consumer_id": binding.consumer_id,
        "source_id": binding.source_id,
        "principal_ref": principal_ref,
        "conversation_id": binding.conversation_id,
        "external_message_id": external_message_id,
        "raw_text": raw_text,
        "source_envelope_hash": _source_envelope_hash(
            binding, principal_ref=principal_ref,
            external_message_id=external_message_id, raw_text=raw_text),
        "session_ref": binding.session_ref(principal_ref),
        "idempotency_key": _event_key(
            binding, principal_ref=principal_ref,
            external_message_id=external_message_id),
    }


def _header_values(headers, name: str) -> List[str]:  # noqa: ANN001
    values = headers.get_all(name) or []
    return [str(value).strip() for value in values]


def _one_header(headers, name: str) -> str:  # noqa: ANN001
    values = _header_values(headers, name)
    if len(values) != 1 or not values[0]:
        raise InboundProtocolError(400, f"{name} 须恰有一个非空值")
    return values[0]


class _BoundedIngressServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 32

    def __init__(self, address, handler, *, connector: "InboundHTTPConnector",
                 max_workers: int = 16):  # noqa: ANN001
        self.connector = connector
        self._slots = threading.BoundedSemaphore(max_workers)
        self._handlers_guard = threading.Lock()
        self._active_handlers = 0
        self._handlers_idle = threading.Event()
        self._handlers_idle.set()
        self._deadline_guard = threading.Lock()
        self._deadline_timers: Dict[int, threading.Timer] = {}
        super().__init__(address, handler)

    def get_request(self):
        request, client = super().get_request()
        request.settimeout(self.connector.binding.request_timeout_s)
        timer = threading.Timer(
            self.connector.binding.request_timeout_s,
            self._expire_request, args=(request,))
        timer.daemon = True
        with self._deadline_guard:
            self._deadline_timers[id(request)] = timer
        try:
            timer.start()
        except BaseException:
            with self._deadline_guard:
                self._deadline_timers.pop(id(request), None)
            request.close()
            raise
        return request, client

    def _expire_request(self, request) -> None:  # noqa: ANN001
        with self._deadline_guard:
            timer = self._deadline_timers.pop(id(request), None)
        if timer is None:
            return
        try:
            request.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            request.close()
        except OSError:
            pass

    def _cancel_deadline(self, request) -> None:  # noqa: ANN001
        with self._deadline_guard:
            timer = self._deadline_timers.pop(id(request), None)
        if timer is not None:
            timer.cancel()

    def shutdown_request(self, request) -> None:  # noqa: ANN001
        self._cancel_deadline(request)
        super().shutdown_request(request)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        with self._handlers_guard:
            self._active_handlers += 1
            self._handlers_idle.clear()
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._cancel_deadline(request)
            self._handler_finished()
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._handler_finished()
            self._slots.release()

    def _handler_finished(self) -> None:
        with self._handlers_guard:
            self._active_handlers -= 1
            if self._active_handlers < 0:
                raise RuntimeError("connector ingress handler 计数下溢")
            if self._active_handlers == 0:
                self._handlers_idle.set()

    def wait_handlers(self, timeout_s: float) -> bool:
        return self._handlers_idle.wait(timeout_s)


def _handler_class():
    class BoundedHeaderReader:
        """Limit header bytes/lines before stdlib materializes Message headers."""

        def __init__(self, raw):  # noqa: ANN001
            self.raw = raw
            self.total = 0
            self.lines = 0

        def readline(self, size: int = -1) -> bytes:
            remaining = _MAX_HEADER_BYTES - self.total
            if remaining <= 0:
                raise http_client.LineTooLong("ingress header block")
            limit = remaining + 1 if size is None or size < 0 else min(size, remaining + 1)
            line = self.raw.readline(limit)
            if self.lines >= _MAX_HEADER_COUNT and line not in (b"\r\n", b"\n", b""):
                raise http_client.HTTPException("too many ingress header lines")
            self.lines += 1
            self.total += len(line)
            if self.total > _MAX_HEADER_BYTES:
                raise http_client.LineTooLong("ingress header block")
            return line

        def __getattr__(self, name: str) -> Any:
            return getattr(self.raw, name)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        @property
        def connector(self) -> "InboundHTTPConnector":
            return self.server.connector  # type: ignore[attr-defined]

        def log_message(self, fmt: str, *args) -> None:  # noqa: ANN002
            logger.info("connector ingress HTTP " + fmt, *args)

        def parse_request(self) -> bool:
            raw = self.rfile
            self.rfile = BoundedHeaderReader(raw)
            try:
                return super().parse_request()
            finally:
                self.rfile = raw

        def _write_json(self, status: int, value: Dict[str, Any]) -> None:
            body = _canonical_json(value)
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                # A client may disconnect after the durable accept boundary.
                # It will replay the same event identity; transport loss is
                # not connector authority corruption.
                pass
            finally:
                self.close_connection = True

        def _empty(self, status: int) -> None:
            try:
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
            except OSError:
                pass
            finally:
                self.close_connection = True

        def _read_body(self) -> bytes:
            header_items = list(self.headers.raw_items())
            if (len(header_items) > _MAX_HEADER_COUNT
                    or sum(len(k) + len(v) for k, v in header_items) > _MAX_HEADER_BYTES):
                raise InboundProtocolError(431, "headers 超过安全上限")
            if _header_values(self.headers, "Transfer-Encoding"):
                raise InboundProtocolError(400, "不支持 Transfer-Encoding/chunked")
            if _header_values(self.headers, "Expect"):
                raise InboundProtocolError(417, "不支持 Expect")
            content_type = _one_header(self.headers, "Content-Type").lower()
            if content_type.split(";", 1)[0].strip() != "application/json":
                raise InboundProtocolError(415, "Content-Type 必须为 application/json")
            raw_length = _one_header(self.headers, "Content-Length")
            try:
                length = int(raw_length)
            except ValueError as error:
                raise InboundProtocolError(400, "Content-Length 非整数") from error
            if raw_length != str(length) or not 1 <= length <= _MAX_BODY_BYTES:
                raise InboundProtocolError(
                    413, f"Content-Length 须为 1..{_MAX_BODY_BYTES} 的规范整数")
            try:
                body = self.rfile.read(length)
            except socket.timeout as error:
                raise InboundProtocolError(408, "request body 读取超时") from error
            except OSError as error:
                raise InboundProtocolError(400, "request body 连接中断") from error
            if len(body) != length:
                raise InboundProtocolError(400, "request body 被截断")
            return body

        def do_POST(self) -> None:
            try:
                try:
                    parsed = urlsplit(self.path)
                except ValueError as error:
                    raise InboundProtocolError(400, "request target 非法") from error
                if (parsed.path != self.connector.binding.path or parsed.query
                        or parsed.fragment):
                    raise InboundProtocolError(404, "未知 ingress 路由")
                body = self._read_body()
                receipt = self.connector.accept_http(self.headers, body)
                if self.connector.binding.wire_type == "onebot_v11_http_post":
                    self._empty(204)
                else:
                    self._write_json(200, receipt)
            except InboundProtocolError as error:
                self.connector.note_rejection(error)
                self._write_json(error.status, {"accepted": False, "error": str(error)})
            except UnsafeConsolePath as error:
                self.connector.record_fatal(error)
                logger.error("connector ingress spool authority 损坏", exc_info=True)
                self._write_json(500, {"accepted": False, "error": "ingress authority failure"})
            except OSError as error:
                logger.error("connector ingress spool 暂不可写", exc_info=True)
                self._write_json(503, {"accepted": False, "error": "durable spool unavailable"})
            except BaseException as error:  # program/state corruption is fatal, never a client poison
                self.connector.record_fatal(error)
                logger.error("connector ingress 内部故障", exc_info=True)
                self._write_json(500, {"accepted": False, "error": "internal ingress failure"})

        def _unsupported(self) -> None:
            self._write_json(405, {"accepted": False, "error": "method not allowed"})

        do_GET = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _unsupported

    return Handler


class InboundHTTPConnector:
    """A supervised HTTP receiver plus durable ``poll``/``commit_poll`` cursor."""

    def __init__(self, binding: InboundBinding, *, secret: str, work_root: str):
        if (not isinstance(secret, str) or not 32 <= len(secret) <= 4096
                or any(not 0x21 <= ord(ch) <= 0x7e for ch in secret)):
            raise InboundConfigError("inbound secret 须为至少 32 字符的随机 printable-ASCII 密钥")
        self.binding = binding
        self._secret = secret.encode("ascii")
        self.spool = ConnectorSpool(work_root, binding.channel)
        self._accept_lock = threading.RLock()
        self._poll_lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._stop_lock = threading.Lock()
        self._accepted: Dict[str, Tuple[bytes, str]] = {}
        self._index_loaded = False
        self._prepared = False
        self._poll_batch: Optional[SpoolBatch] = None
        self._poll_events: List[Dict[str, Any]] = []
        self._poll_index = 0
        self._server: Optional[_BoundedIngressServer] = None
        self._thread: Optional[threading.Thread] = None
        self._fatal: Optional[BaseException] = None
        self._accepted_total = 0
        self._rejected_total = 0
        self._last_rejection: Optional[str] = None

    # ---------------------------------------------------------- envelope/index
    def validate_envelope(self, value: Mapping[str, Any]) -> Dict[str, Any]:
        allowed = {
            "version", "record_kind", "connector", "profile_fingerprint",
            "consumer_id", "source_id", "principal_ref", "conversation_id",
            "external_message_id", "raw_text", "source_envelope_hash",
            "session_ref", "idempotency_key", "seq",
        }
        _strict_keys(value, allowed, label="connector inbox envelope")
        required = allowed - {"seq"}
        if set(value) - {"seq"} != required:
            raise InboundStateError("connector inbox envelope 缺字段")
        if (value.get("version") != 1 or value.get("record_kind") != "natural_language"
                or value.get("connector") != self.binding.channel
                or value.get("profile_fingerprint") != self.binding.profile_fingerprint
                or value.get("consumer_id") != self.binding.consumer_id
                or value.get("source_id") != self.binding.source_id
                or value.get("conversation_id") != self.binding.conversation_id):
            raise InboundStateError("connector inbox envelope 与当前认证 binding 不一致")
        principal = value.get("principal_ref")
        external_id = value.get("external_message_id")
        raw_text = value.get("raw_text")
        if not isinstance(principal, str) or _IDENTITY_RE.fullmatch(principal) is None:
            raise InboundStateError("connector inbox principal_ref 非法")
        if not isinstance(external_id, str) or _IDENTITY_RE.fullmatch(external_id) is None:
            raise InboundStateError("connector inbox external_message_id 非法")
        try:
            text = _clean_text(raw_text)
        except InboundProtocolError as error:
            raise InboundStateError(str(error)) from error
        if text != raw_text:
            raise InboundStateError("connector inbox raw_text 非规范形态")
        if self.binding.wire_type == "webhook_v1":
            if principal != self.binding.principal_id:
                raise InboundStateError("webhook principal 与 binding 不一致")
        else:
            try:
                principal_int = int(principal)
            except ValueError as error:
                raise InboundStateError("OneBot principal 非整数") from error
            if str(principal_int) != principal or principal_int not in self.binding.allowed_user_ids:
                raise InboundStateError("OneBot principal 不在 allowlist")
        expected_key = _event_key(
            self.binding, principal_ref=principal, external_message_id=external_id)
        expected_session = self.binding.session_ref(principal)
        expected_hash = _source_envelope_hash(
            self.binding, principal_ref=principal,
            external_message_id=external_id, raw_text=text)
        if (value.get("idempotency_key") != expected_key
                or value.get("session_ref") != expected_session
                or value.get("source_envelope_hash") != expected_hash):
            raise InboundStateError("connector inbox 服务端派生身份校验失败")
        if "seq" in value and (isinstance(value["seq"], bool)
                               or not isinstance(value["seq"], int) or value["seq"] < 1):
            raise InboundStateError("connector inbox seq 非法")
        return dict(value)

    @staticmethod
    def _without_seq(value: Mapping[str, Any]) -> Dict[str, Any]:
        return {key: item for key, item in value.items() if key != "seq"}

    def _load_acceptance_index(self) -> None:
        if self._index_loaded:
            return
        for stored in self.spool.scan_committed(max_records=_MAX_ACCEPTED_IDENTITIES):
            value = self.validate_envelope(stored)
            canonical = _canonical_json(self._without_seq(value))
            key = value["idempotency_key"]
            receipt = hashlib.sha256(b"inbound-receipt-v1\x00" + canonical).hexdigest()
            existing = self._accepted.get(key)
            if existing is not None and existing[0] != canonical:
                raise InboundStateError("connector inbox 历史 identity collision")
            self._accepted[key] = (canonical, receipt)
        self._accepted_total = len(self._accepted)
        self._index_loaded = True

    def prepare(self) -> None:
        """Load and validate durable authority after work_root exists, before DB/Runner work."""
        with self._accept_lock:
            if self._prepared:
                return
            self.spool.repair_uncommitted_inbox_tail()
            quarantine = self.spool.quarantine_records()
            if quarantine:
                raise InboundStateError(
                    "connector ingress 存在 identity collision quarantine；"
                    "须由运维保留证据并显式归档后再启动")
            self._load_acceptance_index()
            # Retry counters decide whether a DB operation may be attempted
            # again.  They are connector authority, not a best-effort cache;
            # validate them in the same pre-DB startup gate as the inbox.
            self.spool.load_retry_counts()
            self._prepared = True

    def _accept_envelope(self, envelope: Dict[str, Any]) -> Dict[str, Any]:
        envelope = self.validate_envelope(envelope)
        canonical = _canonical_json(self._without_seq(envelope))
        key = envelope["idempotency_key"]
        receipt = hashlib.sha256(b"inbound-receipt-v1\x00" + canonical).hexdigest()
        with self._accept_lock:
            self._load_acceptance_index()
            existing = self._accepted.get(key)
            if existing is not None:
                if existing[0] != canonical:
                    collision = InboundStateError(
                        f"authenticated inbound identity collision: {key}")
                    self.spool.record_quarantine({
                        "version": 1,
                        "kind": "identity_collision",
                        "idempotency_key": key,
                        "profile_fingerprint": self.binding.profile_fingerprint,
                        "existing_hash": "sha256:" + hashlib.sha256(existing[0]).hexdigest(),
                        "received_hash": "sha256:" + hashlib.sha256(canonical).hexdigest(),
                        "recorded_at_unix": int(time.time()),
                    })
                    self.record_fatal(collision)
                    raise InboundProtocolError(409, "event identity 已绑定不同 envelope")
                return self._receipt(envelope, existing[1], duplicate=True)
            if len(self._accepted) >= _MAX_ACCEPTED_IDENTITIES:
                raise InboundStateError("connector ingress identity 数达到上限，须停机归档")
            self.spool.append(envelope)  # fsync happens before transport ACK
            self._accepted[key] = (canonical, receipt)
            self._accepted_total += 1
            return self._receipt(envelope, receipt, duplicate=False)

    def _receipt(self, envelope: Mapping[str, Any], receipt: str, *, duplicate: bool) -> Dict[str, Any]:
        return {
            "accepted": True,
            "consumer_id": self.binding.consumer_id,
            "source_id": self.binding.source_id,
            "message_id": envelope["external_message_id"],
            "receipt_id": receipt,
            "duplicate": duplicate,
        }

    # --------------------------------------------------------------- wire auth
    def accept_http(self, headers, body: bytes) -> Dict[str, Any]:  # noqa: ANN001
        self.raise_if_failed()
        if self.binding.wire_type == "webhook_v1":
            envelope = self._accept_webhook(headers, body)
        elif self.binding.wire_type == "onebot_v11_http_post":
            envelope = self._accept_onebot(headers, body)
        else:  # construction already validates this
            raise InboundStateError(f"未知 inbound wire_type: {self.binding.wire_type}")
        return self._accept_envelope(envelope)

    def _accept_webhook(self, headers, body: bytes) -> Dict[str, Any]:  # noqa: ANN001
        version = _one_header(headers, "X-Meta-Research-Version")
        key_id = _one_header(headers, "X-Meta-Research-Key-Id")
        audience = _one_header(headers, "X-Meta-Research-Audience")
        timestamp_text = _one_header(headers, "X-Meta-Research-Timestamp")
        request_id = _one_header(headers, "X-Meta-Research-Request-Id")
        event_id = _one_header(headers, "X-Meta-Research-Event-Id")
        signature = _one_header(headers, "X-Meta-Research-Signature")
        if (version != "1" or key_id != self.binding.key_id
                or audience != self.binding.consumer_id):
            raise InboundProtocolError(401, "webhook version/key/audience 不匹配")
        if _HEX32_RE.fullmatch(request_id) is None:
            raise InboundProtocolError(400, "webhook request id 须为 128-bit 小写 hex")
        if _IDENTITY_RE.fullmatch(event_id) is None:
            raise InboundProtocolError(400, "webhook event id 非法")
        if _TIMESTAMP_RE.fullmatch(timestamp_text) is None:
            raise InboundProtocolError(400, "webhook timestamp 形状非法")
        try:
            timestamp = int(timestamp_text)
        except ValueError as error:
            raise InboundProtocolError(400, "webhook timestamp 非整数") from error
        if (timestamp > 2 ** 63 - 1
                or abs(int(time.time()) - timestamp) > self.binding.max_skew_s):
            raise InboundProtocolError(401, "webhook timestamp 过期或超前")
        if not signature.startswith("sha256=") or _HEX64_RE.fullmatch(signature[7:]) is None:
            raise InboundProtocolError(401, "webhook signature 形状非法")
        signed = (f"meta-research-inbound-v1\n{key_id}\n{audience}\n{timestamp_text}\n"
                  f"{request_id}\n{event_id}\n").encode("ascii") + body
        expected = hmac.new(self._secret, signed, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature[7:], expected):
            raise InboundProtocolError(401, "webhook signature 无效")
        try:
            value = _strict_json_loads(body)
            _json_depth(value)
        except (ValueError, RecursionError) as error:
            raise InboundProtocolError(400, str(error)) from error
        if not isinstance(value, dict):
            raise InboundProtocolError(400, "webhook body 须为 object")
        try:
            _strict_keys(value, {"protocol_version", "message_id", "text"}, label="webhook body")
        except (ValueError, RecursionError) as error:
            raise InboundProtocolError(400, str(error)) from error
        message_id = value.get("message_id")
        if (type(value.get("protocol_version")) is not int  # noqa: E721
                or value.get("protocol_version") != 1 or message_id != event_id):
            raise InboundProtocolError(400, "webhook body version/message_id 与签名头不一致")
        text = _clean_text(value.get("text"))
        return _make_envelope(
            self.binding, principal_ref=self.binding.principal_id or "",
            external_message_id=message_id, raw_text=text)

    def _accept_onebot(self, headers, body: bytes) -> Dict[str, Any]:  # noqa: ANN001
        signature = _one_header(headers, "X-Signature")
        self_header = _one_header(headers, "X-Self-ID")
        if not signature.startswith("sha1=") or _HEX40_RE.fullmatch(signature[5:]) is None:
            raise InboundProtocolError(401, "OneBot X-Signature 形状非法")
        expected = hmac.new(self._secret, body, hashlib.sha1).hexdigest()
        if not hmac.compare_digest(signature[5:], expected):
            raise InboundProtocolError(401, "OneBot X-Signature 无效")
        if self_header != str(self.binding.self_id):
            raise InboundProtocolError(403, "OneBot X-Self-ID 与绑定不一致")
        try:
            value = _strict_json_loads(body)
            _json_depth(value)
        except (ValueError, RecursionError) as error:
            raise InboundProtocolError(400, str(error)) from error
        if not isinstance(value, dict):
            raise InboundProtocolError(400, "OneBot event 须为 object")
        required = {"post_type", "message_type", "sub_type", "self_id", "message_id",
                    "user_id", "message"}
        if not required <= set(value):
            raise InboundProtocolError(400, "OneBot message event 缺少必需字段")
        if (value.get("post_type") != "message"
                or type(value.get("self_id")) is not int  # noqa: E721
                or value.get("self_id") != self.binding.self_id):
            raise InboundProtocolError(403, "OneBot event 类型/self_id 不匹配")
        message_id = value.get("message_id")
        user_id = value.get("user_id")
        if (isinstance(message_id, bool) or not isinstance(message_id, int)
                or not -(2 ** 63) <= message_id <= 2 ** 63 - 1):
            raise InboundProtocolError(400, "OneBot message_id 须为 int64")
        if (isinstance(user_id, bool) or not isinstance(user_id, int)
                or user_id not in self.binding.allowed_user_ids
                or user_id == self.binding.self_id):
            raise InboundProtocolError(403, "OneBot sender 不在 operator allowlist")
        if self.binding.target_kind == "private":
            if (value.get("message_type") != "private" or value.get("sub_type") != "friend"
                    or user_id != self.binding.target_id):
                raise InboundProtocolError(403, "OneBot private source 与固定目标不一致")
        else:
            group_id = value.get("group_id")
            if (value.get("message_type") != "group" or value.get("sub_type") != "normal"
                    or type(group_id) is not int  # noqa: E721
                    or group_id != self.binding.target_id
                    or value.get("anonymous") not in (None,)):
                raise InboundProtocolError(403, "OneBot group source 与固定目标不一致")
        text = self._onebot_text(value.get("message"))
        return _make_envelope(
            self.binding, principal_ref=str(user_id),
            external_message_id=f"onebot:{message_id}", raw_text=text)

    def _onebot_text(self, message: Any) -> str:
        if not isinstance(message, list) or not 1 <= len(message) <= _MAX_SEGMENTS:
            raise InboundProtocolError(
                400, f"OneBot message 须为 1..{_MAX_SEGMENTS} 个严格 segment 的 array")
        pieces: List[str] = []
        saw_at = False
        for index, segment in enumerate(message):
            if not isinstance(segment, dict) or set(segment) != {"type", "data"}:
                raise InboundProtocolError(400, "OneBot segment 形状非法")
            kind, data = segment.get("type"), segment.get("data")
            if not isinstance(data, dict):
                raise InboundProtocolError(400, "OneBot segment.data 须为 object")
            if kind == "text":
                if set(data) != {"text"} or not isinstance(data.get("text"), str):
                    raise InboundProtocolError(400, "OneBot text segment 非法")
                pieces.append(data["text"])
            elif kind == "at" and index == 0 and not saw_at:
                if set(data) != {"qq"} or str(data.get("qq")) != str(self.binding.self_id):
                    raise InboundProtocolError(403, "OneBot at segment 未指向绑定 bot")
                saw_at = True
            else:
                raise InboundProtocolError(400, "OneBot 只接受 text 与首段 bot-at，不接收 CQ 元数据/附件")
        if self.binding.require_at and not saw_at:
            raise InboundProtocolError(403, "OneBot group message 必须首段 at 绑定 bot")
        return _clean_text("".join(pieces))

    # --------------------------------------------------------------- poll/commit
    def poll(self) -> List[Dict[str, Any]]:
        """Non-destructively return one durable batch; commit is explicit."""
        self.prepare()
        self.raise_if_failed()
        with self._poll_lock:
            if self._poll_batch is None:
                batch = self.spool.read_pending()
                events: List[Dict[str, Any]] = []
                for record in batch.records:
                    if record.line is None:
                        error = InboundStateError(
                            f"connector committed record 不可解析: {record.error}")
                        self.record_fatal(error)
                        raise error
                    try:
                        value = _strict_json_loads(record.line.encode("utf-8"))
                    except ValueError as cause:
                        error = InboundStateError("connector committed record 是坏 JSON")
                        self.record_fatal(error)
                        raise error from cause
                    if not isinstance(value, dict):
                        error = InboundStateError("connector committed record 须为 object")
                        self.record_fatal(error)
                        raise error
                    event = self.validate_envelope(value)
                    event["_poll_token"] = record.end_offset
                    events.append(event)
                self._poll_batch = batch
                self._poll_events = events
                self._poll_index = 0
            return [dict(event) for event in self._poll_events[self._poll_index:]]

    def commit_poll(self, token: Any) -> None:
        with self._poll_lock:
            if self._poll_batch is None or self._poll_index >= len(self._poll_events):
                raise InboundStateError("connector poll 没有待 commit event")
            expected = self._poll_events[self._poll_index]["_poll_token"]
            if isinstance(token, bool) or not isinstance(token, int) or token != expected:
                raise InboundStateError("connector poll commit 非当前队首 receipt")
            self.spool.write_cursor(self._poll_batch, token)
            self._poll_index += 1
            if self._poll_index >= len(self._poll_events):
                self._poll_batch = None
                self._poll_events = []
                self._poll_index = 0

    def load_retry_counts(self) -> Dict[str, int]:
        return self.spool.load_retry_counts()

    def store_retry_counts(self, counts: Dict[str, int]) -> None:
        self.spool.store_retry_counts(counts)

    def pending_status(self) -> Dict[str, Any]:
        self.prepare()
        with self._poll_lock:
            if self._poll_batch is not None:
                return {
                    "pending": len(self._poll_events) - self._poll_index,
                    "has_more": self._poll_batch.has_more_committed,
                }
            batch = self.spool.read_pending()
            return {"pending": len(batch.records), "has_more": batch.has_more_committed}

    # --------------------------------------------------------------- lifecycle
    def start(self) -> bool:
        # Preserve accept->lifecycle lock order used by collision handling;
        # holding lifecycle while prepare waits on accept can deadlock a
        # concurrent authenticated collision recording its fatal state.
        self.prepare()
        self.raise_if_failed()
        with self._lifecycle_lock:
            self.raise_if_failed()
            if self._thread is not None:
                return False
            try:
                server = _BoundedIngressServer(
                    (self.binding.listen_host, self.binding.listen_port), _handler_class(),
                    connector=self)
            except OSError as error:
                raise InboundStateError(
                    f"connector ingress 无法监听 {self.binding.listen_host}:"
                    f"{self.binding.listen_port}: {type(error).__name__}") from error

            def serve() -> None:
                try:
                    server.serve_forever(poll_interval=0.05)
                except BaseException as error:
                    self.record_fatal(error)

            thread = threading.Thread(
                target=serve, daemon=False,
                name=f"connector-ingress-{self.binding.channel}")
            self._server = server
            self._thread = thread
            try:
                thread.start()
            except BaseException:
                self._server = None
                self._thread = None
                server.server_close()
                raise
            return True

    def stop(self) -> Optional[BaseException]:
        with self._stop_lock:
            with self._lifecycle_lock:
                server, thread = self._server, self._thread
                if thread is None:
                    return self._fatal
            try:
                if server is None:
                    return InboundStateError("connector ingress listener handle 损坏")
                server.shutdown()
                thread.join(timeout=1.0)
                if thread.is_alive():
                    return InboundStateError(
                        "connector ingress listener 未在 deadline 内停止")
                if not server.wait_handlers(self.binding.request_timeout_s + 1.0):
                    return InboundStateError(
                        "connector ingress 在途请求未在 deadline 内排空")
                server.server_close()
            except BaseException as error:
                # Retain the only server/thread handles.  A later stop call can
                # finish cleanup; losing a live non-daemon handle can hang the
                # process forever with no recovery path.
                return error
            with self._lifecycle_lock:
                if self._server is server and self._thread is thread:
                    self._server = None
                    self._thread = None
            return self._fatal

    def is_stopped(self) -> bool:
        with self._lifecycle_lock:
            return self._thread is None

    def record_fatal(self, error: BaseException) -> None:
        with self._lifecycle_lock:
            if self._fatal is None:
                self._fatal = error

    def raise_if_failed(self) -> None:
        if self._fatal is not None:
            raise self._fatal

    def note_rejection(self, error: InboundProtocolError) -> None:
        self._rejected_total += 1
        self._last_rejection = f"HTTP {error.status}: {str(error)[:160]}"

    def status(self) -> Dict[str, Any]:
        pending = self.pending_status()
        return {
            "configured": True,
            "running": self._thread is not None and self._thread.is_alive(),
            "healthy": self._fatal is None,
            "transport": self.binding.wire_type,
            "listen": f"http://{self.binding.listen_host}:{self.binding.listen_port}{self.binding.path}",
            "consumer_id": self.binding.consumer_id,
            "source_id": self.binding.source_id,
            "profile_fingerprint": self.binding.profile_fingerprint,
            "accepted_identities": self._accepted_total,
            "rejected_requests": self._rejected_total,
            "last_rejection": self._last_rejection,
            "pending": pending["pending"],
            "has_more": pending["has_more"],
            "allowlisted_principals": (
                1 if self.binding.wire_type == "webhook_v1"
                else len(self.binding.allowed_user_ids)),
        }


def build_inbound_connector(*, channel: str, outbound_type: str,
                            outbound_profile: Mapping[str, Any],
                            inbound_profile: Mapping[str, Any], secret: str,
                            outbound_secret: str, work_root: str) -> InboundHTTPConnector:
    """Validate one channel's inbound profile and construct its receiver."""
    if not isinstance(work_root, str) or not work_root:
        raise InboundConfigError("配置 inbound 时必须提供 work_root")
    if channel == "console":
        raise InboundConfigError("connector channel 名 console 保留给本地控制台信任域")
    common = {
        "type", "listen_host", "listen_port", "path", "secret_env",
        "consumer_id", "source_id", "request_timeout_s",
    }
    wire_type = inbound_profile.get("type")
    if wire_type == "webhook_v1":
        allowed = common | {"conversation_id", "principal_id", "key_id", "max_skew_s"}
    elif wire_type == "onebot_v11_http_post":
        allowed = common | {"self_id", "allowed_user_ids", "require_at",
                            "group_shared_conversation_ack"}
    else:
        raise InboundConfigError(f"channel {channel}.inbound.type 不支持: {wire_type!r}")
    try:
        _strict_keys(inbound_profile, allowed, label=f"channel {channel}.inbound")
    except ValueError as error:
        raise InboundConfigError(str(error)) from error
    if secret == outbound_secret:
        raise InboundConfigError("inbound secret 不得与 outbound token 复用")
    host = _loopback_literal(
        inbound_profile.get("listen_host"), label=f"channel {channel}.inbound.listen_host")
    port = _listen_port(inbound_profile.get("listen_port"))
    path = _listen_path(inbound_profile.get("path"))
    consumer_id = _identity(
        inbound_profile.get("consumer_id"), label=f"channel {channel}.inbound.consumer_id")
    source_id = _identity(
        inbound_profile.get("source_id"), label=f"channel {channel}.inbound.source_id")
    request_timeout_s = _timeout(inbound_profile.get("request_timeout_s", 1.0))

    binding_payload: Dict[str, Any] = {
        "version": 1,
        "channel": channel,
        "wire_type": wire_type,
        "consumer_id": consumer_id,
        "source_id": source_id,
    }
    kwargs: Dict[str, Any] = {}
    if wire_type == "webhook_v1":
        if outbound_type != "webhook_v1":
            raise InboundConfigError("webhook_v1 inbound 只可绑定 webhook_v1 outbound channel")
        conversation_id = _conversation(
            inbound_profile.get("conversation_id"),
            label=f"channel {channel}.inbound.conversation_id")
        principal_id = _identity(
            inbound_profile.get("principal_id"),
            label=f"channel {channel}.inbound.principal_id")
        if principal_id.endswith(":action"):
            raise InboundConfigError("webhook principal_id 不得使用保留后缀 :action")
        key_id = _identity(
            inbound_profile.get("key_id"), label=f"channel {channel}.inbound.key_id")
        max_skew_s = inbound_profile.get("max_skew_s", 300)
        if (isinstance(max_skew_s, bool) or not isinstance(max_skew_s, int)
                or not 30 <= max_skew_s <= 3600):
            raise InboundConfigError("inbound.max_skew_s 须为 30..3600 整数")
        binding_payload.update({
            "conversation_id": conversation_id,
            "principal_id": principal_id,
            "key_id": key_id,
        })
        kwargs.update({
            "conversation_id": conversation_id,
            "principal_id": principal_id,
            "key_id": key_id,
            "max_skew_s": max_skew_s,
        })
    else:
        if outbound_type != "onebot_v11":
            raise InboundConfigError(
                "onebot_v11_http_post inbound 只可绑定 onebot_v11 outbound channel")
        self_id = _positive_int(
            inbound_profile.get("self_id"), label=f"channel {channel}.inbound.self_id")
        outer_self = outbound_profile.get("self_id")
        if outer_self is not None and outer_self != self_id:
            raise InboundConfigError("OneBot inbound self_id 与 outbound profile 不一致")
        allowed_users = inbound_profile.get("allowed_user_ids")
        if not isinstance(allowed_users, list) or not allowed_users:
            raise InboundConfigError("OneBot inbound.allowed_user_ids 须为非空数组")
        cleaned = tuple(sorted({_positive_int(
            value, label=f"channel {channel}.inbound.allowed_user_ids")
            for value in allowed_users}))
        if len(cleaned) != len(allowed_users):
            raise InboundConfigError("OneBot inbound.allowed_user_ids 不得重复")
        if len(cleaned) > 256:
            raise InboundConfigError("OneBot inbound.allowed_user_ids 最多 256 项")
        if self_id in cleaned:
            raise InboundConfigError("OneBot inbound.allowed_user_ids 不得包含 bot self_id")
        target_kind = outbound_profile.get("target_kind")
        if target_kind not in ("private", "group"):
            raise InboundConfigError("OneBot outbound target_kind 只允许 private/group")
        target_id = _positive_int(
            outbound_profile.get("target_id"), label=f"channel {channel}.target_id")
        conversation_id = _conversation(
            outbound_profile.get("conversation_id"), label=f"channel {channel}.conversation_id")
        require_at = inbound_profile.get("require_at", target_kind == "group")
        if not isinstance(require_at, bool):
            raise InboundConfigError("OneBot inbound.require_at 须为 bool")
        if target_kind == "private":
            if cleaned != (target_id,):
                raise InboundConfigError(
                    "OneBot private inbound allowlist 必须恰为固定 target_id")
            if inbound_profile.get("group_shared_conversation_ack") is not None:
                raise InboundConfigError("private inbound 不接受 group_shared_conversation_ack")
        else:
            if inbound_profile.get("group_shared_conversation_ack") is not True:
                raise InboundConfigError(
                    "OneBot group inbound 须显式确认所有 allowlist 成员共享群会话与群可见回复")
        binding_payload.update({
            "conversation_id": conversation_id,
            "self_id": self_id,
            "target_kind": target_kind,
            "target_id": target_id,
            "allowed_user_ids": cleaned,
            "require_at": require_at,
        })
        kwargs.update({
            "conversation_id": conversation_id,
            "self_id": self_id,
            "target_kind": target_kind,
            "target_id": target_id,
            "allowed_user_ids": cleaned,
            "require_at": require_at,
        })

    fingerprint = hashlib.sha256(
        b"connector-inbound-profile-v1\x00" + _canonical_json(binding_payload)).hexdigest()
    binding = InboundBinding(
        channel=channel, wire_type=wire_type, consumer_id=consumer_id,
        source_id=source_id, profile_fingerprint=fingerprint,
        listen_host=host, listen_port=port, path=path,
        request_timeout_s=request_timeout_s, **kwargs)
    return InboundHTTPConnector(binding, secret=secret, work_root=work_root)
