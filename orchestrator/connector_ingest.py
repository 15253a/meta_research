"""Run-process consumer for authenticated connector ingress spools.

Network listeners own only channel-isolated files.  This module runs inside the
single writer process, revalidates every server-derived envelope, and commits
it to the existing authoritative ``interaction_*`` chain.  Provider text can
never select the console structured-action branch; the only connector action
syntax is the closed, explicit directive confirmation/rejection grammar below.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from typing import Any, Dict, List, Mapping, Optional

from .console import IdempotencyCollisionError
from .connector_ingress import InboundStateError
from .console_ingest import ConsoleInboxIngest, RetryStateError
from .ids import parse_positive_sqlite_int


logger = logging.getLogger(__name__)

_CONFIRM_RE = re.compile(r"^确认指令 d([1-9][0-9]*)$")
_REJECT_RE = re.compile(r"^拒绝指令 d([1-9][0-9]*)$")


def _connector_action(text: str) -> Optional[tuple[str, int]]:
    for action, pattern in (("confirm", _CONFIRM_RE), ("reject", _REJECT_RE)):
        match = pattern.fullmatch(text)
        if match is not None:
            try:
                directive_id = parse_positive_sqlite_int(
                    match.group(1), label="connector directive_id")
            except ValueError:
                # Authenticated natural language is still untrusted data.  An
                # out-of-range dID must not be promoted to an ingress-fatal
                # program error; it simply does not match the closed action
                # language and is classified as an ordinary message.
                return None
            return action, directive_id
    return None


class ConnectorChannelIngest(ConsoleInboxIngest):
    """Consume one authenticated channel through explicit poll receipts."""

    _MAX_EVENTS_PER_PROBE = 1

    def __init__(self, console, mediator, work_root: str, connector):  # noqa: ANN001
        if not bool(getattr(connector, "has_inbound", False)):
            raise ValueError("ConnectorChannelIngest 要求 bidirectional connector")
        self.connector_adapter = connector
        channel = getattr(connector, "channel", None)
        if not isinstance(channel, str) or not channel:
            raise ValueError("inbound connector channel 非法")
        self.channel = channel
        super().__init__(
            console, mediator, work_root, spool=connector.inbound_spool,
            source_label=f"connector_inbox[{channel}]")
        if self._retry_state_error is not None:
            error = InboundStateError(
                f"connector {channel} retry authority 无法安全读取")
            self.connector_adapter.record_inbound_fatal(error)
            raise error from self._retry_state_error

    def ingest(self, cyc: Any = None) -> int:
        with self._ingest_lock:
            try:
                # Unlike the browser console, authenticated connector files
                # are transport authority.  Do not let the base class turn a
                # corrupt cursor/retry/spool into an indefinitely "healthy"
                # blocked state.
                if self._retry_state_error is not None:
                    raise InboundStateError(
                        f"connector {self.channel} retry authority 损坏") from self._retry_state_error
                if self._retry_write_error is not None:
                    raise InboundStateError(
                        f"connector {self.channel} retry authority 写入状态不确定") \
                        from self._retry_write_error
                result = self._ingest(cyc)
                self.connector_adapter.raise_if_inbound_failed()
                return result
            except sqlite3.OperationalError:
                # DB busy is retried by the resident System boundary and is
                # not evidence that the connector authority itself is corrupt.
                raise
            except Exception as error:
                self.connector_adapter.record_inbound_fatal(error)
                raise

    def _ingest(self, cyc: Any) -> int:
        events = self.connector_adapter.poll()
        cycle_id: Optional[str] = getattr(cyc, "cycle_id", None)
        processed = 0
        retry_pending = False
        for event in events[:self._MAX_EVENTS_PER_PROBE]:
            token = event.get("_poll_token")
            outcome = self._process_verified_envelope(event, cycle_id)
            if outcome == "retry":
                retry_pending = True
                break
            if outcome not in ("ok", "poison"):
                raise RuntimeError(f"connector ingest outcome 非法: {outcome!r}")
            self.connector_adapter.commit_poll(token)
            if outcome == "ok":
                processed += 1
        status = self.connector_adapter.inbound_pending_status()
        self.has_pending = retry_pending or bool(status["pending"] or status["has_more"])
        self.connector_adapter.raise_if_inbound_failed()
        return processed

    def _process_verified_envelope(self, event: Mapping[str, Any],
                                   cycle_id: Optional[str]) -> str:
        try:
            value = {key: item for key, item in event.items() if key != "_poll_token"}
            value = self.connector_adapter.validate_inbound_envelope(value)
            if value["record_kind"] != "natural_language" or value["connector"] != self.channel:
                raise RuntimeError("authenticated connector envelope domain 漂移")
            raw = value["raw_text"]
            action = _connector_action(raw)
            if action is not None:
                return self._process_authenticated_action(
                    value, action=action[0], directive_id=action[1], cycle_id=cycle_id)
            return self._process_text(
                connector=self.channel, raw=raw, idem=value["idempotency_key"],
                cycle_id=cycle_id, conversation_id=value["conversation_id"],
                session_ref=value["session_ref"], strict_failures=True)
        except Exception as error:
            self.connector_adapter.record_inbound_fatal(error)
            raise

    def _process_authenticated_action(self, event: Mapping[str, Any], *, action: str,
                                      directive_id: int,
                                      cycle_id: Optional[str]) -> str:
        idem = event["idempotency_key"]
        raw = event["raw_text"]
        action_session = event["session_ref"] + ":action"
        mid: Optional[int] = None
        try:
            source = self.console.daemon.query_one(
                "SELECT m.goal_id,m.goal_ver FROM directive d "
                "JOIN interaction_message m ON m.id=d.source_interaction_message_id "
                "WHERE d.id=?", (directive_id,))
            if source is None:
                goal_id, goal_ver = self._message_goal_binding(self.channel, idem)
            else:
                goal_id, goal_ver = source
            mid = self.console.ingest.inbound(
                connector=self.channel, raw_text=raw, idempotency_key=idem,
                cycle_id=cycle_id, goal_id=goal_id, goal_ver=goal_ver,
                session_ref=action_session,
                conversation_id=event["conversation_id"])
            self._verify_inbound_message(
                mid, raw=raw, idem=idem, goal_id=goal_id, goal_ver=goal_ver,
                conversation_id=event["conversation_id"], connector=self.channel,
                session_ref=action_session)
            self._ensure_action_classification(mid)
            if self._has_action_failure(mid):
                self._clear_attempt(idem)
                return "poison"
            row = self.console.daemon.query_one(
                "SELECT status,hardness,payload_json FROM directive WHERE id=?",
                (directive_id,))
            if row is None:
                raise ValueError(f"directive 不存在: {directive_id}")
            status, hardness, payload_raw = row
            payload = json.loads(payload_raw)
            if not isinstance(payload, dict):
                raise ValueError(f"directive {directive_id} payload 损坏")
            if action == "confirm":
                if payload.get("confirmed") is True:
                    if payload.get("confirmation_message_id") == mid:
                        self._clear_attempt(idem)
                        return "ok"
                    raise ValueError(f"directive {directive_id} 已由另一条消息确认")
                if (status != "pending" or hardness != "hard"
                        or payload.get("confirmed") is not False):
                    raise ValueError(f"directive {directive_id} 不是可确认的 pending hard 指令")
                self.console.confirm_directive(
                    directive_id=directive_id, confirm_message_id=mid)
            else:
                if status == "rejected":
                    if payload.get("rejection_message_id") == mid:
                        self._clear_attempt(idem)
                        return "ok"
                    raise ValueError(f"directive {directive_id} 已由另一条消息拒绝")
                if status != "pending":
                    raise ValueError(f"directive {directive_id} 非 pending，不可拒绝")
                self.console.reject_directive(
                    directive_id=directive_id,
                    reason="用户从认证 connector 拒绝",
                    reject_message_id=mid)
        except IdempotencyCollisionError:
            # Stable provider identity changing body/authority is corruption,
            # never a skippable chat typo.
            raise
        except RetryStateError:
            raise
        except sqlite3.OperationalError:
            attempts = self._bump(idem)
            if attempts >= self._MAX_ATTEMPTS:
                raise RuntimeError(
                    f"connector directive action DB 重试达到上限: {idem}")
            return "retry"
        except ValueError as error:
            # An authenticated operator can still reference another principal's
            # directive or a stale/missing id.  Persist a visible no-effect
            # terminal receipt before advancing this source cursor.
            if mid is None:
                raise RuntimeError(
                    "connector directive action 未能建立失败 provenance") from error
            if not self._record_action_failure(
                    mid, f"connector directive action 被拒：{error}"):
                attempts = self._bump(idem)
                if attempts >= self._MAX_ATTEMPTS:
                    raise RuntimeError(
                        f"connector directive action 失败回执重试达到上限: {idem}") from error
                return "retry"
            self._clear_attempt(idem)
            return "poison"
        self._clear_attempt(idem)
        return "ok"


class ConnectorInboxIngest:
    """Fairly probe every configured inbound channel without sharing cursors."""

    def __init__(self, console, mediator, work_root: str,
                 connectors: Mapping[str, Any]):
        self.mediator = mediator
        self._lock = threading.RLock()
        self.channels: List[ConnectorChannelIngest] = []
        self._channel_cursor = 0
        for channel in sorted(connectors):
            connector = connectors[channel]
            if bool(getattr(connector, "has_inbound", False)):
                self.channels.append(
                    ConnectorChannelIngest(console, mediator, work_root, connector))

    def ingest(self, cyc: Any = None) -> int:
        with self._lock:
            processed = 0
            # Each channel has its own bounded batch/cursor.  A retry in one
            # channel does not stop the next channel or the console trust domain.
            if not self.channels:
                return 0
            cursor = self._channel_cursor % len(self.channels)
            ordered = self.channels[cursor:] + self.channels[:cursor]
            self._channel_cursor = (cursor + 1) % len(self.channels)
            for channel in ordered:
                processed += channel.ingest(cyc)
            return processed

    def poll_accepted(self) -> None:
        with self._lock:
            self.mediator.poll()

    @property
    def has_pending(self) -> bool:
        with self._lock:
            return any(channel.has_pending for channel in self.channels)

    @property
    def interaction_pending(self) -> bool:
        with self._lock:
            return self.has_pending or self.mediator.has_pending_queries

    @property
    def accepted_interaction_pending(self) -> bool:
        with self._lock:
            return self.mediator.has_pending_queries

    def start(self) -> List[Any]:
        owned = []
        try:
            for channel in self.channels:
                if channel.connector_adapter.start_inbound():
                    owned.append(channel.connector_adapter)
            return owned
        except BaseException as primary:
            cleanup_errors = []
            for _attempt in range(2):
                if not owned:
                    break
                error = self.stop(owned)
                if error is not None:
                    cleanup_errors.append(error)
            if owned:
                # System.start has not yet received a return value.  Attach the
                # surviving capabilities to the original exception so its
                # outer rollback can retain/retry them instead of orphaning a
                # non-daemon listener.
                primary.inbound_owned = owned
            add_note = getattr(primary, "add_note", None)
            if callable(add_note):
                for error in cleanup_errors:
                    add_note(
                        f"partial inbound start 回滚失败: "
                        f"{type(error).__name__}: {error}")
            raise

    @staticmethod
    def stop(owned: List[Any]) -> Optional[BaseException]:
        first = None
        remaining = []
        for connector in reversed(list(owned)):
            try:
                error = connector.stop_inbound()
            except BaseException as stop_error:
                error = stop_error
            if first is None and error is not None:
                first = error
            if not connector.inbound_stopped():
                remaining.append(connector)
        owned[:] = list(reversed(remaining))
        return first

    def raise_if_failed(self) -> None:
        for channel in self.channels:
            channel.connector_adapter.raise_if_inbound_failed()
