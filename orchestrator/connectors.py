"""Production outbound connector transports and durable delivery scheduling.

The research loop knows only events.  Transport credentials and vendor details
live in an operator-owned connector profile and environment variables.  Two
wire adapters are provided:

* ``webhook_v1``: a strict JSON webhook.  The receiver must durably accept and
  echo ``producer_id`` plus ``event_key``; replaying that identity pair must be
  a no-op at the receiver.
* ``onebot_v11``: direct QQ private/group delivery through a OneBot HTTP API.
  The event key is included in both ``echo`` and the human-readable message.

``OutboundDelivery`` is intentionally independent from SQLite.  The Outbox is
derived from SQLite truth, while retry/receipt files are transport truth.  A
crash after the remote side accepts but before the local receipt is durable may
replay the same producer/event identity: this is the unavoidable at-least-once
window and is why the webhook contract requires receiver-side idempotency.
"""
from __future__ import annotations

import ipaddress
import http.client
import json
import logging
import math
import os
import re
import socket
import ssl
import stat
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple


_MAX_PROFILE_BYTES = 64 * 1024
_MAX_RESPONSE_BYTES = 64 * 1024
_MAX_TOKEN_CHARS = 4096
_MAX_EVENT_TEXT_CHARS = 3500
_CHANNEL_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_ENV_RE = re.compile(r"^METARESEARCH_[A-Z0-9_]{0,100}(?:TOKEN|SECRET)$")
_EVENT_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._/-]{0,255}$")
_PRODUCER_ID_RE = re.compile(r"^mr-[0-9a-f]{32}$")
logger = logging.getLogger(__name__)


class ConnectorConfigError(ValueError):
    """Connector profile is unsafe, incomplete, or unsupported."""


class ConnectorDeliveryError(RuntimeError):
    """One delivery attempt failed; retry metadata is safe to persist."""

    def __init__(self, message: str, *, kind: str, retry_after_s: Optional[float] = None):
        self.kind = kind
        self.retry_after_s = retry_after_s
        super().__init__(message)


def _unique_object(pairs):  # noqa: ANN001
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON key 重复: {key}")
        result[key] = value
    return result


def _strict_json_loads(raw: str) -> Any:
    return json.loads(
        raw, object_pairs_hook=_unique_object,
        parse_constant=lambda token: (_ for _ in ()).throw(
            ValueError(f"JSON 非有限数字: {token}")))


def _strict_keys(value: Mapping[str, Any], allowed: set[str], *, label: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ConnectorConfigError(f"{label} 含未知字段: {sorted(unknown)}")


def _finite_number(value: Any, *, label: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConnectorConfigError(f"{label} 须为数字")
    result = float(value)
    if not math.isfinite(result) or result < minimum or result > maximum:
        raise ConnectorConfigError(f"{label} 须在 [{minimum}, {maximum}] 内")
    return result


def _validate_https_or_loopback_http(url: str, *, label: str) -> str:
    if (not isinstance(url, str) or not url or len(url) > 2048 or not url.isascii()
            or any(ord(ch) <= 0x20 or ord(ch) == 0x7f for ch in url)):
        raise ConnectorConfigError(f"{label} 须为非空 URL")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        raise ConnectorConfigError(f"{label} 只允许 https 或 loopback http")
    try:
        port = parsed.port
    except ValueError as error:
        raise ConnectorConfigError(f"{label} 端口非法") from error
    if port is not None and not 1 <= port <= 65535:
        raise ConnectorConfigError(f"{label} 端口须为 1..65535")
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise ConnectorConfigError(f"{label} 不得含 userinfo/query/fragment（凭据只许走环境变量）")
    if parsed.scheme == "http":
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError as error:
            raise ConnectorConfigError(f"{label} 的明文 http 只允许 loopback IP 字面量") from error
        if not address.is_loopback:
            raise ConnectorConfigError(f"{label} 的明文 http 只允许 loopback IP")
    return url


def _read_profile(path: Path) -> Dict[str, Any]:
    flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_NONBLOCK", 0))
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ConnectorConfigError("connector profile 须为单硬链常规文件")
        if info.st_uid not in {os.geteuid(), 0}:
            raise ConnectorConfigError("connector profile 须由当前用户或 root 拥有")
        if info.st_mode & 0o022:
            raise ConnectorConfigError("connector profile 不得被 group/other 写入")
        if info.st_size <= 0 or info.st_size > _MAX_PROFILE_BYTES:
            raise ConnectorConfigError(f"connector profile 大小须在 1..{_MAX_PROFILE_BYTES} bytes")
        chunks: List[bytes] = []
        remaining = info.st_size
        while remaining:
            chunk = os.read(fd, min(remaining, 16 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) != info.st_size:
            raise ConnectorConfigError("connector profile 读取时被截断")
        after = os.fstat(fd)
        if ((after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)):
            raise ConnectorConfigError("connector profile 读取期间发生替换/改写")
    finally:
        os.close(fd)
    try:
        value = _strict_json_loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ConnectorConfigError(f"connector profile 不是合法 UTF-8 JSON: {error}") from error
    if not isinstance(value, dict):
        raise ConnectorConfigError("connector profile 顶层须为 object")
    return value


def _token_from_env(profile: Mapping[str, Any], *, label: str) -> Optional[str]:
    name = profile.get("token_env")
    if name is None:
        return None
    if not isinstance(name, str) or _ENV_RE.fullmatch(name) is None:
        raise ConnectorConfigError(
            f"{label}.token_env 须为 METARESEARCH_*_TOKEN/SECRET 专用环境变量名")
    token = os.environ.get(name)
    if not token:
        raise ConnectorConfigError(f"{label} 所需环境变量 {name} 未设置")
    if len(token) > _MAX_TOKEN_CHARS or any(not 0x21 <= ord(ch) <= 0x7e for ch in token):
        raise ConnectorConfigError(f"{label} token 非法")
    return token


def _retry_after(headers: Mapping[str, str]) -> Optional[float]:
    value = headers.get("Retry-After")
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if math.isfinite(seconds) and 0 <= seconds <= 3600 else None


class _HTTPJSONConnector:
    def __init__(self, *, channel: str, url: str, token: Optional[str], timeout_s: float):
        if not isinstance(channel, str) or _CHANNEL_RE.fullmatch(channel) is None:
            raise ConnectorConfigError("connector channel 非法")
        if (isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float))
                or not math.isfinite(float(timeout_s)) or not 0.1 <= float(timeout_s) <= 60):
            raise ConnectorConfigError(f"channel {channel}.timeout_s 须在 [0.1, 60] 内")
        if token is not None and (
                not isinstance(token, str) or not token or len(token) > _MAX_TOKEN_CHARS
                or any(not 0x21 <= ord(ch) <= 0x7e for ch in token)):
            raise ConnectorConfigError(f"channel {channel} token 非法")
        self.channel = channel
        self.url = _validate_https_or_loopback_http(url, label=f"channel {channel}.url")
        self.token = token
        self.timeout_s = float(timeout_s)
        parsed = urllib.parse.urlsplit(self.url)
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = False
        if not loopback and token is None:
            raise ConnectorConfigError(f"channel {channel} 远端 endpoint 必须配置 token_env")
        self._request_guard = threading.RLock()
        self._inflight: Optional[Dict[str, Any]] = None
        # Connector credentials never traverse process-wide HTTP(S)_PROXY used
        # by Codex.  Explicit http.client also never follows redirects.

    def _post(self, body: Dict[str, Any], *, event_key: str) -> Dict[str, Any]:
        encoded = json.dumps(
            body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "Idempotency-Key": event_key,
            "User-Agent": "meta-research-connector/1",
        }
        if self.token is not None:
            headers["Authorization"] = f"Bearer {self.token}"
        parsed_url = urllib.parse.urlsplit(self.url)
        deadline = time.monotonic() + self.timeout_s
        # A prior timed-out DNS resolver may still be inside libc.  Never spawn
        # an unbounded chain of resolver threads; wait only this attempt's
        # deadline and fail without issuing another request if it has not
        # terminated.  Its cancel flag is checked before any HTTP bytes send.
        with self._request_guard:
            prior = self._inflight
        if prior is not None and not prior["done"].is_set():
            remaining = max(0.0, deadline - time.monotonic())
            if not prior["done"].wait(remaining):
                prior["cancel"].set()
                connection = prior.get("connection")
                if connection is not None:
                    try:
                        connection.close()
                    except OSError:
                        pass
                raise ConnectorDeliveryError(
                    "connector transport 前次超时尝试尚未终止", kind="transport")

        done = threading.Event()
        cancel = threading.Event()
        attempt: Dict[str, Any] = {
            "done": done, "cancel": cancel, "connection": None,
            "raw": None, "error": None,
        }

        def constrain_socket(connection) -> None:  # noqa: ANN001
            remaining = deadline - time.monotonic()
            if cancel.is_set() or remaining <= 0:
                raise socket.timeout("connector wall-clock deadline")
            if connection.sock is not None:
                connection.sock.settimeout(remaining)

        def request_once() -> None:
            connection = None
            try:
                connection_type = (http.client.HTTPSConnection
                                   if parsed_url.scheme == "https" else http.client.HTTPConnection)
                connection_kwargs: Dict[str, Any] = {"timeout": self.timeout_s}
                if parsed_url.scheme == "https":
                    connection_kwargs["context"] = ssl.create_default_context()
                connection = connection_type(
                    parsed_url.hostname, parsed_url.port, **connection_kwargs)
                attempt["connection"] = connection
                constrain_socket(connection)
                connection.connect()
                constrain_socket(connection)
                connection.request("POST", parsed_url.path or "/", body=encoded, headers=headers)
                constrain_socket(connection)
                response = connection.getresponse()
                constrain_socket(connection)
                status = int(response.status)
                if status < 200 or status >= 300:
                    kind = ("remote_retryable" if status in (408, 425, 429) or status >= 500
                            else "remote_rejected")
                    raise ConnectorDeliveryError(
                        f"connector HTTP {status}", kind=kind,
                        retry_after_s=_retry_after(response.headers))
                chunks: List[bytes] = []
                total = 0
                while True:
                    constrain_socket(connection)
                    # read1 performs at most one buffered/raw read.  The outer
                    # caller additionally closes the socket at the total wall
                    # deadline, covering DNS/connect/TLS/header slow-drip too.
                    chunk = response.read1(min(16 * 1024, _MAX_RESPONSE_BYTES + 1 - total))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > _MAX_RESPONSE_BYTES:
                        raise ConnectorDeliveryError(
                            "connector 响应超过上限", kind="response_too_large")
                attempt["raw"] = b"".join(chunks)
            except ConnectorDeliveryError as error:
                attempt["error"] = error
            except (http.client.HTTPException, socket.timeout, ssl.SSLError,
                    TimeoutError, OSError, ValueError) as error:
                attempt["error"] = ConnectorDeliveryError(
                    f"connector transport 失败: {type(error).__name__}", kind="transport")
            finally:
                try:
                    if connection is not None:
                        connection.close()
                finally:
                    done.set()
                    with self._request_guard:
                        if self._inflight is attempt:
                            self._inflight = None

        thread = threading.Thread(
            target=request_once, daemon=True, name=f"connector-http-{self.channel}")
        attempt["thread"] = thread
        with self._request_guard:
            self._inflight = attempt
        try:
            thread.start()
        except BaseException as error:
            with self._request_guard:
                if self._inflight is attempt:
                    self._inflight = None
            raise ConnectorDeliveryError(
                f"connector transport worker 启动失败: {type(error).__name__}",
                kind="transport") from error
        remaining = max(0.0, deadline - time.monotonic())
        if not done.wait(remaining):
            cancel.set()
            connection = attempt.get("connection")
            if connection is not None:
                try:
                    connection.close()
                except OSError:
                    pass
            raise ConnectorDeliveryError("connector transport 总墙钟超时", kind="transport")
        if attempt["error"] is not None:
            raise attempt["error"]
        raw = attempt["raw"]
        if not isinstance(raw, bytes):
            raise ConnectorDeliveryError("connector 未产生响应", kind="transport")
        try:
            parsed = _strict_json_loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise ConnectorDeliveryError("connector ACK 不是合法 UTF-8 JSON", kind="invalid_ack") from error
        if not isinstance(parsed, dict):
            raise ConnectorDeliveryError("connector ACK 须为 object", kind="invalid_ack")
        return parsed

    def poll(self) -> List[Dict[str, Any]]:
        return []

    def status(self) -> Dict[str, Any]:
        parsed = urllib.parse.urlsplit(self.url)
        return {
            "channel": self.channel,
            "transport": type(self).__name__,
            "endpoint": f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
            "configured": True,
        }


class WebhookV1Connector(_HTTPJSONConnector):
    """Strict event webhook with receiver-side event-key idempotency."""

    def send(self, event: Dict[str, Any]) -> Dict[str, Any]:
        producer_id, event_key, delivery_key = _event_identity(event)
        ack = self._post({
            "protocol_version": 1,
            "channel": self.channel,
            "producer_id": producer_id,
            "event_key": event_key,
            "kind": event.get("kind"),
            "payload": _safe_wire_payload(event),
        }, event_key=delivery_key)
        if (ack.get("accepted") is not True
                or ack.get("producer_id") != producer_id
                or ack.get("event_key") != event_key):
            raise ConnectorDeliveryError(
                "webhook ACK 必须 accepted=true 且精确回显 producer_id/event_key",
                kind="invalid_ack")
        delivery_id = ack.get("delivery_id")
        if delivery_id is not None and (not isinstance(delivery_id, str) or len(delivery_id) > 256):
            raise ConnectorDeliveryError("webhook delivery_id 非法", kind="invalid_ack")
        return {"accepted": True, "producer_id": producer_id,
                "event_key": event_key, "delivery_id": delivery_id}


def _safe_wire_payload(event: Mapping[str, Any]) -> Dict[str, Any]:
    """Last-mile confidentiality guard, including legacy queued file receipts."""
    payload = event.get("payload")
    if not isinstance(payload, dict):
        raise ConnectorDeliveryError("outbox payload 非 object", kind="invalid_event")
    if event.get("kind") != "file_request_resolved":
        return payload
    summary = payload.get("resolution_summary")
    if not isinstance(summary, dict):
        legacy = payload.get("resolution")
        if isinstance(legacy, dict) and legacy.get("cancelled") is True:
            summary = {"cancelled": True, "item_count": 0,
                       "provided_file_count": 0, "unavailable_item_count": 0}
        elif isinstance(legacy, list):
            provided = sum(
                len(item.get("provided", []))
                for item in legacy
                if isinstance(item, dict) and isinstance(item.get("provided", []), list))
            unavailable = sum(
                1 for item in legacy if isinstance(item, dict) and "unavailable" in item)
            summary = {"cancelled": False, "item_count": len(legacy),
                       "provided_file_count": provided,
                       "unavailable_item_count": unavailable}
        else:
            summary = None
    return {
        "request_id": payload.get("request_id"),
        "summary_md": payload.get("summary_md"),
        "status": payload.get("status"),
        "resolution_summary": summary,
    }


_KIND_TITLES = {
    "interaction_received": "已收到消息",
    "interaction_reply": "状态查询回复",
    "interaction_unclear": "需要确认消息意图",
    "directive_received": "已收到控制指令",
    "directive_classified": "控制指令已分类",
    "directive_pending_confirmation": "控制指令等待确认",
    "directive_pending_effect": "控制指令等待生效",
    "directive_applied": "控制指令已生效",
    "directive_rejected": "控制指令已拒绝",
    "directive_superseded": "控制指令已被替代",
    "file_request_pending": "系统请求文件",
    "file_request_reminder": "文件请求仍在等待",
    "file_request_resolved": "文件请求已结束",
    "cycle_failed": "研究轮次失败",
    "engineering_blocked": "工程执行受阻",
    "cycle_summary": "周期研究摘要",
    "answer_applicability_changed": "旧结论适用性变化",
}


def render_event_text(event: Dict[str, Any]) -> str:
    """Bounded Chinese rendering for human-facing QQ messages."""
    _producer_id, _event_key_value, delivery_key = _event_identity(event)
    kind = event.get("kind")
    payload = event.get("payload")
    if isinstance(payload.get("summary_md"), str):
        body = payload["summary_md"]
    elif kind == "interaction_reply" and isinstance(payload.get("reply_text"), str):
        body = payload["reply_text"]
    elif kind == "interaction_unclear":
        body = "这条消息的意图不明确；系统没有执行状态变更，请明确说明是查询、备注还是控制指令。"
    elif kind == "interaction_received":
        body = f"消息 #{payload.get('message_id')} 已耐久入账；分类意图：{payload.get('intent') or '待定'}。"
    elif kind == "directive_pending_confirmation":
        body = f"请确认规范化指令：{payload.get('polished') or '未提供'}。确认前不会改变研究状态。"
    elif kind == "directive_pending_effect":
        body = f"指令 #{payload.get('directive_id')} 已入队，将在 {payload.get('consume_at')} 边界应用。"
    elif kind == "directive_applied":
        body = f"指令 #{payload.get('directive_id')} 已应用；效果：" + json.dumps(
            payload.get("effect") or {}, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False)
    elif kind == "directive_rejected":
        body = f"指令 #{payload.get('directive_id')} 未应用；理由：{payload.get('reason') or '未提供'}。"
    elif kind == "directive_superseded":
        body = f"指令 #{payload.get('directive_id')} 已被更新的控制动作替代。"
    elif kind == "directive_received":
        body = f"控制指令 #{payload.get('directive_id')} 已耐久入账。"
    elif kind == "directive_classified":
        body = (f"控制指令 #{payload.get('directive_id')} 已分类为 {payload.get('kind')}，"
                f"消费边界：{payload.get('consume_at')}。")
    elif kind == "file_request_pending":
        body = f"文件请求 #{payload.get('request_id')}：{payload.get('summary_md') or '请查看控制台详情'}"
    elif kind == "file_request_reminder":
        body = (f"文件请求 #{payload.get('request_id')} 仍未解决，"
                f"已等待 {payload.get('waited_intervals')} 个提醒周期。")
    elif kind == "file_request_resolved":
        body = f"文件请求 #{payload.get('request_id')} 已进入 {payload.get('status')} 终态。"
    else:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    title = _KIND_TITLES.get(kind, "系统通知")
    suffix = f"\n事件键：{delivery_key}"
    budget = _MAX_EVENT_TEXT_CHARS - len(suffix)
    text = f"【{title}】\n{body}"
    if len(text) > budget:
        text = text[: max(0, budget - 1)] + "…"
    return text + suffix


class OneBotV11Connector(_HTTPJSONConnector):
    """Direct QQ outbound adapter for OneBot v11 HTTP implementations."""

    def __init__(self, *, channel: str, base_url: str, token: Optional[str], timeout_s: float,
                 target_kind: str, target_id: int, conversation_id: str):
        if target_kind not in ("private", "group"):
            raise ConnectorConfigError(f"channel {channel}.target_kind 只允许 private/group")
        if (isinstance(target_id, bool) or not isinstance(target_id, int)
                or not 1 <= target_id <= 2 ** 63 - 1):
            raise ConnectorConfigError(f"channel {channel}.target_id 须为正整数")
        if (not isinstance(conversation_id, str) or not conversation_id
                or len(conversation_id) > 128
                or any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in conversation_id)):
            raise ConnectorConfigError(
                f"channel {channel}.conversation_id 须为 1..128 字符且不得含控制字符")
        base = _validate_https_or_loopback_http(base_url, label=f"channel {channel}.base_url")
        parsed = urllib.parse.urlsplit(base)
        path = parsed.path.rstrip("/") + ("/send_private_msg" if target_kind == "private" else "/send_group_msg")
        url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))
        super().__init__(channel=channel, url=url, token=token, timeout_s=timeout_s)
        self.target_kind = target_kind
        self.target_id = target_id
        self.conversation_id = conversation_id

    def send(self, event: Dict[str, Any]) -> Dict[str, Any]:
        producer_id, event_key, delivery_key = _event_identity(event)
        if (event.get("kind") in {"interaction_received", "interaction_reply", "interaction_unclear"}
                and event["payload"].get("conversation_id") != self.conversation_id):
            raise ConnectorDeliveryError(
                "OneBot interaction conversation_id 与固定 target 绑定不一致",
                kind="invalid_route")
        target_field = "user_id" if self.target_kind == "private" else "group_id"
        ack = self._post({
            target_field: self.target_id,
            "message": render_event_text(event),
            "auto_escape": True,
            "echo": delivery_key,
        }, event_key=delivery_key)
        retcode = ack.get("retcode")
        if (ack.get("status") != "ok" or isinstance(retcode, bool)
                or not isinstance(retcode, int) or retcode != 0):
            raise ConnectorDeliveryError("OneBot ACK 表示发送失败", kind="remote_rejected")
        if ack.get("echo") != delivery_key:
            raise ConnectorDeliveryError("OneBot ACK echo 与 event_key 不一致", kind="invalid_ack")
        data = ack.get("data")
        message_id = data.get("message_id") if isinstance(data, dict) else None
        if (isinstance(message_id, bool) or not isinstance(message_id, int)
                or not -(2 ** 63) <= message_id <= 2 ** 63 - 1):
            raise ConnectorDeliveryError("OneBot message_id 缺失或非整数", kind="invalid_ack")
        return {"accepted": True, "producer_id": producer_id,
                "event_key": event_key, "delivery_id": str(message_id)}


def _event_identity(event: Mapping[str, Any]) -> Tuple[str, str, str]:
    value = event.get("event_key")
    if not isinstance(value, str) or _EVENT_KEY_RE.fullmatch(value) is None:
        raise ConnectorDeliveryError("outbox event_key 非法", kind="invalid_event")
    producer_id = event.get("producer_id")
    if not isinstance(producer_id, str) or _PRODUCER_ID_RE.fullmatch(producer_id) is None:
        raise ConnectorDeliveryError("outbox producer_id 非法", kind="invalid_event")
    if not isinstance(event.get("kind"), str) or not isinstance(event.get("payload"), dict):
        raise ConnectorDeliveryError("outbox event kind/payload 非法", kind="invalid_event")
    delivery_key = f"{producer_id}:{value}"
    if _EVENT_KEY_RE.fullmatch(delivery_key) is None:
        raise ConnectorDeliveryError("producer_id + event_key 超过线协议上限", kind="invalid_event")
    return producer_id, value, delivery_key


def load_connectors(profile_path: str) -> Dict[str, Any]:
    """Load a bounded profile and instantiate channel connectors.

    Secrets are never accepted in JSON.  Profiles name an environment variable
    and startup fails if that variable is absent.
    """
    raw = _read_profile(Path(profile_path))
    _strict_keys(raw, {"version", "channels", "delivery"}, label="connector profile")
    if raw.get("version") != 1:
        raise ConnectorConfigError("connector profile.version 必须为 1")
    channels = raw.get("channels")
    if not isinstance(channels, dict) or not channels:
        raise ConnectorConfigError("connector profile.channels 须为非空 object")
    delivery = raw.get("delivery", {})
    if not isinstance(delivery, dict):
        raise ConnectorConfigError("connector profile.delivery 须为 object")
    _strict_keys(delivery, {"retry_initial_s", "retry_max_s", "batch_size"}, label="delivery")
    retry_initial = _finite_number(
        delivery.get("retry_initial_s", 1.0), label="delivery.retry_initial_s", minimum=0.1, maximum=3600)
    retry_max = _finite_number(
        delivery.get("retry_max_s", 300.0), label="delivery.retry_max_s", minimum=retry_initial, maximum=86400)
    batch_size = delivery.get("batch_size", 32)
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 4 <= batch_size <= 256:
        raise ConnectorConfigError("delivery.batch_size 须为 4..256 整数")

    result: Dict[str, Any] = {}
    secret_env_names = set()
    for channel, profile in channels.items():
        if not isinstance(channel, str) or _CHANNEL_RE.fullmatch(channel) is None:
            raise ConnectorConfigError(f"connector channel 名非法: {channel!r}")
        if not isinstance(profile, dict):
            raise ConnectorConfigError(f"channel {channel} profile 须为 object")
        kind = profile.get("type")
        timeout_s = _finite_number(
            profile.get("timeout_s", 1.0), label=f"channel {channel}.timeout_s", minimum=0.1, maximum=1.0)
        token = _token_from_env(profile, label=f"channel {channel}")
        if token is None:
            raise ConnectorConfigError(
                f"channel {channel} 生产 profile 必须配置 token_env（loopback 也需防端口冒占）")
        if profile.get("token_env") is not None:
            secret_env_names.add(profile["token_env"])
        if kind == "webhook_v1":
            _strict_keys(profile, {"type", "url", "token_env", "timeout_s"}, label=f"channel {channel}")
            result[channel] = WebhookV1Connector(
                channel=channel, url=profile.get("url"), token=token, timeout_s=timeout_s)
        elif kind == "onebot_v11":
            _strict_keys(
                profile,
                {"type", "base_url", "token_env", "timeout_s", "target_kind", "target_id",
                 "conversation_id"},
                label=f"channel {channel}",
            )
            result[channel] = OneBotV11Connector(
                channel=channel, base_url=profile.get("base_url"), token=token,
                timeout_s=timeout_s, target_kind=profile.get("target_kind"),
                target_id=profile.get("target_id"), conversation_id=profile.get("conversation_id"),
            )
        else:
            raise ConnectorConfigError(f"channel {channel}.type 不支持: {kind!r}")
    if "qq" not in result:
        raise ConnectorConfigError("当前 notify_matrix=all_qq_on，profile 必须配置 qq channel")
    # Research Codex and manifest subprocesses inherit the run process
    # environment.  Once transports own their token in memory, erase the
    # source variables before any DB/runner/harness is constructed.
    for name in secret_env_names:
        os.environ.pop(name, None)
    return {
        "channels": result,
        "retry_initial_s": retry_initial,
        "retry_max_s": retry_max,
        "batch_size": batch_size,
    }


class OutboundDelivery:
    """Single-process delivery scheduler with durable per-event backoff state."""

    def __init__(self, outbox, connectors: Mapping[str, Any], *, default_channels: List[str],
                 retry_initial_s: float = 1.0, retry_max_s: float = 300.0,
                 batch_size: int = 32, clock=time.time):
        self.outbox = outbox
        self.connectors = dict(connectors)
        self.default_channels = tuple(default_channels)
        self.retry_initial_s = float(retry_initial_s)
        self.retry_max_s = float(retry_max_s)
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 4 <= batch_size <= 256:
            raise ValueError("outbound batch_size 须为 4..256 整数")
        self.batch_size = batch_size
        self.clock = clock
        self._lock = threading.Lock()
        self._worker_guard = threading.RLock()
        self._worker_thread: Optional[threading.Thread] = None
        self._worker_stop: Optional[threading.Event] = None
        self._worker_error: Optional[BaseException] = None
        self._last_worker_error: Optional[BaseException] = None
        self._group_cursor: Dict[Tuple[str, int], int] = {}

    def _delay(self, attempts: int, error: Exception) -> float:
        exponent = min(max(0, attempts - 1), 30)
        delay = min(self.retry_max_s, self.retry_initial_s * (2 ** exponent))
        supplied = getattr(error, "retry_after_s", None)
        if isinstance(supplied, (int, float)) and math.isfinite(float(supplied)):
            delay = max(delay, min(self.retry_max_s, max(0.0, float(supplied))))
        return delay

    @staticmethod
    def _causal_group(event: Mapping[str, Any]) -> Tuple[str, Any]:
        kind = event.get("kind")
        payload = event.get("payload", {})

        def group(label: str, candidate: Any) -> Tuple[str, Any]:
            if ((isinstance(candidate, int) and not isinstance(candidate, bool))
                    or isinstance(candidate, str)):
                return label, candidate
            return "event", event.get("event_key")

        if isinstance(kind, str) and kind.startswith("interaction_"):
            return group("interaction", payload.get("message_id"))
        if isinstance(kind, str) and kind.startswith("directive_"):
            return group("directive", payload.get("directive_id"))
        if isinstance(kind, str) and kind.startswith("file_request_"):
            return group("file_request", payload.get("request_id"))
        if kind in {"cycle_failed", "cycle_summary"}:
            return group("cycle", payload.get("cycle_id"))
        if kind == "engineering_blocked":
            return group("build_target", payload.get("build_target_id"))
        if kind == "answer_applicability_changed":
            return group("answer", payload.get("answer_id"))
        return "event", event.get("event_key")

    @staticmethod
    def _priority(events: List[Dict[str, Any]]) -> int:
        kinds = {event.get("kind") for event in events}
        if "interaction_reply" in kinds:
            return 0
        if kinds & {"interaction_received", "interaction_unclear"}:
            return 1
        if any(isinstance(kind, str) and (
                kind.startswith("directive_") or kind.startswith("file_request_")) for kind in kinds):
            return 2
        return 3

    def tick(self, now_ts: Optional[float] = None,
             stop_requested: Optional[Callable[[], bool]] = None) -> List[str]:
        """Attempt due causal groups; urgent replies can overtake unrelated backlog.

        FIFO is preserved inside one interaction/directive/request lifecycle.
        A poison event or a future retry blocks only that causal group, never
        the entire channel.  Transport failures remain durable retry facts;
        local state corruption still raises and terminates the worker.
        """
        now = self.clock() if now_ts is None else float(now_ts)
        if not math.isfinite(now):
            raise ValueError("delivery now_ts 须为有限数字")
        should_stop = stop_requested or (lambda: False)
        delivered: List[str] = []
        with self._lock:
            self.outbox.reconcile_delivery_state()
            for channel, connector in self.connectors.items():
                include_default = channel in self.default_channels
                attempted = 0
                retries = self.outbox.delivery_retries(channel)
                groups: Dict[Tuple[str, Any], List[Dict[str, Any]]] = {}
                for event in self.outbox.pending_for_channel(
                        channel, include_default=include_default):
                    groups.setdefault(self._causal_group(event), []).append(event)
                buckets: Dict[int, List[Tuple[Tuple[str, Any], List[Dict[str, Any]]]]] = {}
                for group_key, group_events in groups.items():
                    buckets.setdefault(self._priority(group_events), []).append(
                        (group_key, group_events))
                active_priorities = sorted(buckets)
                for priority in active_priorities:
                    bucket = buckets[priority]
                    cursor_key = (channel, priority)
                    cursor = self._group_cursor.get(cursor_key, 0) % len(bucket)
                    buckets[priority] = bucket[cursor:] + bucket[:cursor]
                    self._group_cursor[cursor_key] = cursor + 1

                # Urgent stays first.  The first causal group of every active
                # priority receives exactly one reserved slot (batch>=4), then
                # normal priority order consumes the remainder.  Poison groups
                # cannot monopolize their priority, and lower priorities cannot
                # starve under a steady reply stream.
                schedule = []
                for priority in active_priorities:
                    group_key, group_events = buckets[priority][0]
                    schedule.append((group_key, group_events, 0, 1))
                for priority in active_priorities:
                    for index, (group_key, group_events) in enumerate(buckets[priority]):
                        schedule.append((group_key, group_events, 1 if index == 0 else 0, None))

                blocked_groups = set()
                for group_key, group, start, stop_index in schedule:
                    if group_key in blocked_groups:
                        continue
                    for event in group[start:stop_index]:
                        if attempted >= self.batch_size or should_stop():
                            break
                        current = now if now_ts is not None else float(self.clock())
                        if not math.isfinite(current):
                            raise ValueError("delivery clock 须返回有限数字")
                        retry = retries.get(event["event_key"])
                        if retry is not None and float(retry["next_attempt_at"]) > current:
                            blocked_groups.add(group_key)
                            break
                        attempted += 1
                        outbound_event = dict(event)
                        outbound_event["producer_id"] = self.outbox.producer_id
                        try:
                            ack = connector.send(outbound_event)
                        except ConnectorDeliveryError as error:
                            completed_at = current if now_ts is not None else float(self.clock())
                            if not math.isfinite(completed_at):
                                raise ValueError("delivery clock 须返回有限数字")
                            attempts = int(retry["attempt_count"]) + 1 if retry is not None else 1
                            kind = getattr(error, "kind", type(error).__name__)
                            self.outbox.record_delivery_failure(
                                channel, event, attempt_count=attempts,
                                next_attempt_at=completed_at + self._delay(attempts, error),
                                error_kind=str(kind), error_text=str(error), attempted_at=completed_at)
                            logger.warning(
                                "outbound delivery 将重试 channel=%s event_key=%s attempt=%s kind=%s",
                                channel, event["event_key"], attempts, kind)
                            blocked_groups.add(group_key)
                            break
                        completed_at = current if now_ts is not None else float(self.clock())
                        if not math.isfinite(completed_at):
                            raise ValueError("delivery clock 须返回有限数字")
                        self.outbox.record_delivery_success(
                            channel, event, ack=ack, accepted_at=completed_at)
                        logger.info("outbound delivery 已 ACK channel=%s event_key=%s",
                                    channel, event["event_key"])
                        delivered.append(f"{channel}:{event['event_key']}")
                    if attempted >= self.batch_size or should_stop():
                        break
        return delivered

    def status(self) -> Dict[str, Any]:
        result = {channel: connector.status() for channel, connector in self.connectors.items()}
        with self._worker_guard:
            thread = self._worker_thread
            error = self._worker_error or self._last_worker_error
            result["_worker"] = {
                "running": bool(thread is not None and thread.is_alive()),
                "healthy": error is None,
                "error": (None if error is None else
                          f"{type(error).__name__}: {str(error)[:256]}"),
            }
        return result

    def pending_status(self) -> Dict[str, Any]:
        """Bounded operational summary; no network call and no future-retry wait."""
        with self._lock:
            self.outbox.reconcile_delivery_state()
            total = 0
            retrying = 0
            urgent = 0
            by_channel = {}
            for channel in self.connectors:
                pending = self.outbox.pending_for_channel(
                    channel, include_default=channel in self.default_channels)
                retries = self.outbox.delivery_retries(channel)
                channel_retrying = sum(event["event_key"] in retries for event in pending)
                channel_urgent = sum(
                    event.get("kind") in {
                        "interaction_received", "interaction_unclear", "interaction_reply"}
                    for event in pending)
                by_channel[channel] = {
                    "pending": len(pending), "retrying": channel_retrying,
                    "urgent_pending": channel_urgent,
                }
                total += len(pending)
                retrying += channel_retrying
                urgent += channel_urgent
            return {"pending": total, "retrying": retrying,
                    "urgent_pending": urgent, "channels": by_channel}

    def raise_if_failed(self) -> None:
        with self._worker_guard:
            error = self._worker_error
        if error is not None:
            raise error

    def start(self, poll_interval_s: float) -> bool:
        """Start one transport-only worker; return whether this call owns it."""
        interval = float(poll_interval_s)
        if not math.isfinite(interval) or interval < 0.05:
            raise ValueError("outbound poll_interval_s 须为不小于 0.05 的有限数字")
        with self._worker_guard:
            if self._worker_thread is not None:
                return False
            stop = threading.Event()
            self._worker_stop = stop
            self._worker_error = None
            self._last_worker_error = None

            def work() -> None:
                while not stop.is_set():
                    try:
                        self.tick(stop_requested=stop.is_set)
                    except BaseException as error:
                        with self._worker_guard:
                            self._worker_error = error
                            self._last_worker_error = error
                        stop.set()
                        return
                    stop.wait(interval)

            # Delivery owns no uncommitted research truth.  If an emergency
            # process exit cuts this thread, the absent receipt deliberately
            # replays the event key on restart.
            thread = threading.Thread(target=work, daemon=True, name="outbound-delivery")
            self._worker_thread = thread
            try:
                thread.start()
            except BaseException:
                self._worker_thread = None
                self._worker_stop = None
                self._worker_error = None
                raise
            return True

    def stop(self) -> Optional[BaseException]:
        with self._worker_guard:
            thread, stop = self._worker_thread, self._worker_stop
            if thread is None:
                return None
            if stop is not None:
                stop.set()
        connector_deadline = max(
            [float(getattr(connector, "timeout_s", 5.0))
             for connector in self.connectors.values()] or [5.0])
        thread.join(timeout=min(65.0, max(1.0, connector_deadline + 1.0)))
        with self._worker_guard:
            if thread.is_alive():
                error = TimeoutError("outbound delivery 未在当前单次 send deadline 后停止")
                if self._worker_error is None:
                    self._worker_error = error
                self._last_worker_error = self._worker_error
                return self._worker_error
            error = self._worker_error
            if error is not None:
                self._last_worker_error = error
            self._worker_thread = None
            self._worker_stop = None
            self._worker_error = None
        return error
