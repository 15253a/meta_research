"""CP11.2b.3e: authenticated, durable connector ingress boundaries.

These tests deliberately exercise the real loopback HTTP listener.  A 2xx
transport ACK is therefore evidence that the authenticated envelope has
already crossed the fsync boundary, not merely that a parser accepted it.
"""
from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest

import conftest
from orchestrator import database as db
from orchestrator.connector_ingest import ConnectorChannelIngest, ConnectorInboxIngest
from orchestrator.connector_ingress import (InboundConfigError, InboundStateError,
                                            build_inbound_connector)
from orchestrator.connectors import (BidirectionalConnector, ConnectorConfigError,
                                     load_connectors)
from orchestrator.console import Console
from orchestrator.run import System, build_system
from orchestrator.writedaemon import WriteDaemon


INBOUND_SECRET = "inbound-auth-secret-" + "a" * 40
OUTBOUND_SECRET = "outbound-auth-secret-" + "b" * 40


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _webhook_connector(tmp_path: Path, *, port: int | None = None,
                       secret: str = INBOUND_SECRET,
                       source_id: str = "operator-webhook",
                       request_timeout_s: float = 1.0,
                       channel: str = "qq"):
    return build_inbound_connector(
        channel=channel, outbound_type="webhook_v1", outbound_profile={},
        inbound_profile={
            "type": "webhook_v1",
            "listen_host": "127.0.0.1",
            "listen_port": port or _free_port(),
            "path": "/meta-research/inbound",
            "secret_env": "METARESEARCH_TEST_INGRESS_SECRET",
            "consumer_id": "research-loop",
            "source_id": source_id,
            "conversation_id": "operator:primary",
            "principal_id": "operator-alice",
            "key_id": "key-2026-07",
            "max_skew_s": 300,
            "request_timeout_s": request_timeout_s,
        },
        secret=secret, outbound_secret=OUTBOUND_SECRET, work_root=str(tmp_path))


def _onebot_connector(tmp_path: Path, *, target_kind: str = "private",
                      target_id: int = 1001, allowed=(1001,), require_at=False,
                      port: int | None = None):
    inbound = {
        "type": "onebot_v11_http_post",
        "listen_host": "127.0.0.1",
        "listen_port": port or _free_port(),
        "path": "/onebot/events",
        "secret_env": "METARESEARCH_TEST_ONEBOT_INGRESS_SECRET",
        "consumer_id": "research-loop",
        "source_id": "onebot-reverse-http",
        "self_id": 9000,
        "allowed_user_ids": list(allowed),
        "require_at": require_at,
        "request_timeout_s": 1.0,
    }
    if target_kind == "group":
        inbound["group_shared_conversation_ack"] = True
    return build_inbound_connector(
        channel="qq", outbound_type="onebot_v11",
        outbound_profile={
            "target_kind": target_kind,
            "target_id": target_id,
            "conversation_id": f"qq:{target_kind}:{target_id}",
        },
        inbound_profile=inbound, secret=INBOUND_SECRET,
        outbound_secret=OUTBOUND_SECRET, work_root=str(tmp_path))


def _http(connector, method: str, path: str, body: bytes = b"",
          headers: dict[str, str] | None = None):
    client = http.client.HTTPConnection(
        connector.binding.listen_host, connector.binding.listen_port, timeout=2)
    try:
        client.request(method, path, body=body, headers=headers or {})
        response = client.getresponse()
        raw = response.read()
        value = json.loads(raw.decode("utf-8")) if raw else None
        return response.status, value
    finally:
        client.close()


def _webhook_headers(connector, body: bytes, *, event_id: str,
                     timestamp: str | None = None, request_id: str | None = None,
                     secret: str = INBOUND_SECRET, signature: str | None = None):
    timestamp = timestamp or str(int(time.time()))
    request_id = request_id or hashlib.md5(  # noqa: S324 - request nonce, not authentication
        f"{event_id}:{time.time_ns()}".encode()).hexdigest()
    key_id = connector.binding.key_id
    audience = connector.binding.consumer_id
    signed = (f"meta-research-inbound-v1\n{key_id}\n{audience}\n{timestamp}\n"
              f"{request_id}\n{event_id}\n").encode("ascii") + body
    digest = signature or hmac.new(secret.encode("ascii"), signed, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Meta-Research-Version": "1",
        "X-Meta-Research-Key-Id": key_id,
        "X-Meta-Research-Audience": audience,
        "X-Meta-Research-Timestamp": timestamp,
        "X-Meta-Research-Request-Id": request_id,
        "X-Meta-Research-Event-Id": event_id,
        "X-Meta-Research-Signature": "sha256=" + digest,
    }


def _post_webhook(connector, *, event_id: str, text: str,
                  request_id: str | None = None):
    body = json.dumps({
        "protocol_version": 1, "message_id": event_id, "text": text,
    }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return _http(
        connector, "POST", connector.binding.path, body,
        _webhook_headers(connector, body, event_id=event_id, request_id=request_id))


def _onebot_headers(body: bytes, *, self_id=9000, secret=INBOUND_SECRET):
    return {
        "Content-Type": "application/json",
        "X-Self-ID": str(self_id),
        "X-Signature": "sha1=" + hmac.new(
            secret.encode("ascii"), body, hashlib.sha1).hexdigest(),
    }


def _post_onebot(connector, event: dict):
    body = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return _http(connector, "POST", connector.binding.path, body, _onebot_headers(body))


def _private_event(message_id: int, text: str, *, user_id=1001):
    return {
        "post_type": "message", "message_type": "private", "sub_type": "friend",
        "self_id": 9000, "message_id": message_id, "user_id": user_id,
        "message": [{"type": "text", "data": {"text": text}}],
    }


def _group_event(message_id: int, text: str, *, user_id=1001, group_id=2001,
                 include_at=True):
    message = []
    if include_at:
        message.append({"type": "at", "data": {"qq": "9000"}})
    message.append({"type": "text", "data": {"text": text}})
    return {
        "post_type": "message", "message_type": "group", "sub_type": "normal",
        "self_id": 9000, "message_id": message_id, "user_id": user_id,
        "group_id": group_id, "anonymous": None, "message": message,
    }


def test_webhook_ack_duplicate_and_restart_poll_commit_are_durable(tmp_path):
    """ACK follows append; replay is stable; only explicit commit advances the cursor."""
    port = _free_port()
    first = _webhook_connector(tmp_path, port=port)
    assert first.start() is True
    status, receipt = _post_webhook(first, event_id="msg-1", text="备注：保留边界")
    assert status == 200 and receipt["accepted"] is True and receipt["duplicate"] is False
    status, duplicate = _post_webhook(
        first, event_id="msg-1", text="备注：保留边界", request_id="f" * 32)
    assert status == 200 and duplicate == {**receipt, "duplicate": True}
    assert len(first.spool.scan_committed()) == 1
    assert first.stop() is None

    # Crash/restart before poll receipt: the same event remains visible.
    second = _webhook_connector(tmp_path, port=port)
    pending = second.poll()
    assert len(pending) == 1
    assert pending[0]["external_message_id"] == "msg-1"
    assert pending[0]["raw_text"] == "备注：保留边界"
    assert pending[0]["connector"] == "qq"
    assert pending[0]["principal_ref"] == "operator-alice"
    assert pending[0]["conversation_id"] == "operator:primary"

    # Another restart without commit must see it again; a valid receipt consumes it.
    third = _webhook_connector(tmp_path, port=port)
    replay = third.poll()
    assert replay[0]["idempotency_key"] == pending[0]["idempotency_key"]
    third.commit_poll(replay[0]["_poll_token"])
    assert _webhook_connector(tmp_path, port=port).poll() == []


def test_restart_audits_and_truncates_never_acked_crash_tail(tmp_path):
    first = _webhook_connector(tmp_path)
    first.start()
    assert _post_webhook(first, event_id="before-tail", text="durable")[0] == 200
    assert first.stop() is None
    with first.spool.inbox_path.open("ab") as stream:
        stream.write(b'{"text":"NEVER-ACKED-PRIVATE-TAIL"')
        stream.flush()
        os.fsync(stream.fileno())

    restarted = _webhook_connector(tmp_path)
    restarted.prepare()
    recovery = restarted.spool.recovery_records()
    assert len(recovery) == 1
    assert recovery[0]["kind"] == "uncommitted_tail_truncated"
    assert recovery[0]["byte_count"] > 0
    assert "NEVER-ACKED" not in json.dumps(recovery)
    assert restarted.spool.inbox_path.read_bytes().endswith(b"\n")

    restarted.start()
    try:
        assert _post_webhook(restarted, event_id="after-tail", text="safe")[0] == 200
        events = restarted.poll()
        assert [event["external_message_id"] for event in events] == [
            "before-tail", "after-tail"]
        for event in events:
            restarted.commit_poll(event["_poll_token"])
    finally:
        assert restarted.stop() is None


def test_authenticated_webhook_rejections_are_bounded_nonfatal_and_leave_no_spool(tmp_path):
    connector = _webhook_connector(tmp_path)
    connector.start()
    try:
        normal = json.dumps({
            "protocol_version": 1, "message_id": "bad", "text": "hello",
        }, separators=(",", ":")).encode()
        cases = []
        cases.append((connector.binding.path, normal,
                      _webhook_headers(connector, normal, event_id="bad", signature="0" * 64), 401))
        cases.append((connector.binding.path, normal,
                      _webhook_headers(connector, normal, event_id="bad",
                                       timestamp="9" * 20), 400))
        bool_version = b'{"protocol_version":true,"message_id":"bool","text":"hello"}'
        cases.append((connector.binding.path, bool_version,
                      _webhook_headers(connector, bool_version, event_id="bool"), 400))

        surrogate = b'{"protocol_version":1,"message_id":"surrogate","text":"\\ud800"}'
        cases.append((connector.binding.path, surrogate,
                      _webhook_headers(connector, surrogate, event_id="surrogate"), 400))

        deep_value = "[" * 10 + '"x"' + "]" * 10
        deep = (b'{"protocol_version":1,"message_id":"deep","text":'
                + deep_value.encode() + b"}")
        cases.append((connector.binding.path, deep,
                      _webhook_headers(connector, deep, event_id="deep"), 400))

        for path, body, headers, expected in cases:
            status, response = _http(connector, "POST", path, body, headers)
            assert status == expected and response["accepted"] is False

        status, response = _http(
            connector, "POST", "/not-the-bound-ingress?smuggle=1", normal,
            _webhook_headers(connector, normal, event_id="bad"))
        assert status == 404 and response["accepted"] is False
        status, response = _http(connector, "GET", connector.binding.path)
        assert status == 405 and response["accepted"] is False

        assert connector.poll() == []
        health = connector.status()
        assert health["healthy"] is True
        assert health["rejected_requests"] == len(cases) + 1  # GET is not a protocol parse
    finally:
        assert connector.stop() is None


def test_ingress_total_deadline_reclaims_all_slow_drip_slots(tmp_path):
    connector = _webhook_connector(tmp_path, request_timeout_s=0.15)
    connector.start()
    sockets = []
    stop_drip = threading.Event()
    try:
        prefix = (
            f"POST {connector.binding.path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            "Content-Type: application/json\r\nContent-Length: 4096\r\n\r\n{{"
        ).encode("ascii")
        for _ in range(16):
            client = socket.create_connection(
                (connector.binding.listen_host, connector.binding.listen_port), timeout=1)
            client.sendall(prefix)
            sockets.append(client)

        def drip() -> None:
            while not stop_drip.wait(0.03):
                for client in sockets:
                    try:
                        client.sendall(b"x")
                    except OSError:
                        pass

        worker = threading.Thread(target=drip, daemon=True)
        worker.start()
        time.sleep(0.35)  # > hard deadline even though every socket receives bytes
        status, receipt = _post_webhook(
            connector, event_id="after-slow-drip", text="healthy")
        assert status == 200 and receipt["accepted"] is True
        assert connector.status()["healthy"] is True
    finally:
        stop_drip.set()
        for client in sockets:
            client.close()
        assert connector.stop() is None


def test_header_limits_apply_before_materialization_and_listener_recovers(tmp_path):
    connector = _webhook_connector(tmp_path)
    connector.start()
    try:
        client = socket.create_connection(
            (connector.binding.listen_host, connector.binding.listen_port), timeout=1)
        oversized = (
            f"POST {connector.binding.path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            + "".join(f"X-Fill-{index}: x\r\n" for index in range(40))
            + "Content-Length: 1\r\n\r\nx"
        ).encode("ascii")
        client.sendall(oversized)
        response = client.recv(4096)
        client.close()
        assert b" 431 " in response
        assert connector.poll() == []
        assert _post_webhook(
            connector, event_id="after-large-headers", text="healthy")[0] == 200
        assert connector.status()["healthy"] is True
    finally:
        assert connector.stop() is None


def test_authenticated_identity_collision_is_quarantined_and_blocks_restart(tmp_path):
    port = _free_port()
    connector = _webhook_connector(tmp_path, port=port)
    connector.start()
    assert _post_webhook(connector, event_id="stable-id", text="first")[0] == 200
    status, response = _post_webhook(connector, event_id="stable-id", text="changed")
    assert status == 409 and response["accepted"] is False

    quarantine = connector.spool.quarantine_records()
    assert len(quarantine) == 1
    assert quarantine[0]["kind"] == "identity_collision"
    assert set(quarantine[0]) >= {"existing_hash", "received_hash", "idempotency_key"}
    # Evidence is hashed: provider text and the ingress secret are never copied to quarantine.
    serialized = json.dumps(quarantine, ensure_ascii=False)
    assert "first" not in serialized and "changed" not in serialized
    assert INBOUND_SECRET not in serialized
    assert isinstance(connector.stop(), InboundStateError)

    restarted = _webhook_connector(tmp_path, port=port)
    with pytest.raises(InboundStateError, match="quarantine"):
        restarted.prepare()


def test_listener_stop_failure_retains_handle_for_retry(tmp_path, monkeypatch):
    connector = _webhook_connector(tmp_path)
    connector.start()
    server = connector._server
    original = server.shutdown
    calls = {"count": 0}

    def fail_once():
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("shutdown fault")
        return original()

    monkeypatch.setattr(server, "shutdown", fail_once)
    error = connector.stop()
    assert isinstance(error, RuntimeError)
    assert connector.is_stopped() is False
    assert connector._server is server and connector._thread is not None
    assert connector.stop() is None
    assert connector.is_stopped() is True


def test_listener_keyboard_interrupt_retains_handle_for_retry(tmp_path, monkeypatch):
    connector = _webhook_connector(tmp_path)
    connector.start()
    server = connector._server
    original = server.shutdown
    calls = {"count": 0}

    def interrupt_once():
        calls["count"] += 1
        if calls["count"] == 1:
            raise KeyboardInterrupt()
        return original()

    monkeypatch.setattr(server, "shutdown", interrupt_once)
    error = connector.stop()
    assert isinstance(error, KeyboardInterrupt)
    assert connector.is_stopped() is False
    assert connector.stop() is None
    assert connector.is_stopped() is True


def test_system_retries_inbound_cleanup_before_propagating_interrupt(tmp_path):
    class Advancer:
        last_stop_reason = None
        last_block_reason = None

        def run_cycles(self, _count):
            return []

    owned = ["listener"]
    calls = {"count": 0}

    def stop_inbound(items):
        calls["count"] += 1
        if calls["count"] == 1:
            return KeyboardInterrupt()
        items.clear()
        return None

    system = System(
        advancer=Advancer(), state=None, daemon=None, dual_mode="A",
        work_root=tmp_path, start_inbound=lambda: owned,
        stop_inbound=stop_inbound,
        inbound_cleanup_pending=lambda items, _error: bool(items))
    with pytest.raises(KeyboardInterrupt):
        system.run(0)
    assert calls["count"] == 2
    assert system._pump_inbound_owned == []


def test_partial_multi_channel_start_retries_owned_listener_rollback():
    class Adapter:
        def __init__(self, *, fail_start=False):
            self.fail_start = fail_start
            self.started = False
            self.stop_calls = 0

        def start_inbound(self):
            if self.fail_start:
                raise RuntimeError("second bind failed")
            self.started = True
            return True

        def stop_inbound(self):
            self.stop_calls += 1
            if self.stop_calls == 1:
                return RuntimeError("transient shutdown failure")
            self.started = False
            return None

        def inbound_stopped(self):
            return not self.started

    first = Adapter()
    second = Adapter(fail_start=True)
    inbox = object.__new__(ConnectorInboxIngest)
    inbox.channels = [
        type("Channel", (), {"connector_adapter": first})(),
        type("Channel", (), {"connector_adapter": second})(),
    ]
    with pytest.raises(RuntimeError, match="second bind failed"):
        inbox.start()
    assert first.stop_calls == 2 and first.inbound_stopped()


def test_outbound_start_failure_retries_inbound_rollback(tmp_path):
    class Advancer:
        last_stop_reason = None
        last_block_reason = None

        def run_cycles(self, _count):  # pragma: no cover - start fails first
            raise AssertionError("advancer must not run")

    class BrokenOutbound:
        def start(self, _interval):
            raise RuntimeError("outbound start failed")

    owned = ["listener"]
    calls = {"count": 0}

    def stop_inbound(items):
        calls["count"] += 1
        if calls["count"] == 1:
            return RuntimeError("first cleanup failed")
        items.clear()
        return None

    system = System(
        advancer=Advancer(), state=None, daemon=None, dual_mode="A",
        work_root=tmp_path, outbound_delivery=BrokenOutbound(),
        start_inbound=lambda: owned, stop_inbound=stop_inbound,
        inbound_cleanup_pending=lambda items, _error: bool(items))
    with pytest.raises(RuntimeError, match="outbound start failed"):
        system.run(0)
    assert calls["count"] == 2
    assert system._pump_inbound_owned == []


def test_binding_drift_and_corrupt_retry_authority_fail_startup(tmp_path):
    drift_root = tmp_path / "drift"
    drift_root.mkdir()
    first = _webhook_connector(drift_root)
    first.start()
    assert _post_webhook(first, event_id="bound-message", text="hello")[0] == 200
    assert first.stop() is None
    changed = _webhook_connector(drift_root, source_id="different-source")
    with pytest.raises(InboundStateError, match="binding"):
        changed.prepare()

    retry_root = tmp_path / "retry"
    retry_root.mkdir()
    initial = _webhook_connector(retry_root)
    initial.spool.store_retry_counts({"connector-" + "a" * 32: 1})
    (retry_root / "state" / ".connector_qq_inbox.retry.json").write_text(
        "{broken", encoding="utf-8")
    with pytest.raises(ValueError, match="retry state 损坏"):
        _webhook_connector(retry_root).prepare()


def test_onebot_private_accepts_signed_negative_message_id_and_rejects_wrong_authority(tmp_path):
    connector = _onebot_connector(tmp_path)
    connector.start()
    try:
        status, response = _post_onebot(connector, _private_event(-7, "继续"))
        assert (status, response) == (204, None)
        event = connector.poll()[0]
        assert event["external_message_id"] == "onebot:-7"
        assert event["principal_ref"] == "1001"
        assert event["raw_text"] == "继续"

        wrong_sender = _private_event(8, "hello", user_id=1002)
        assert _post_onebot(connector, wrong_sender)[0] == 403

        float_self = _private_event(81, "hello")
        float_self["self_id"] = 9000.0
        assert _post_onebot(connector, float_self)[0] == 403

        body = json.dumps(_private_event(9, "hello"), separators=(",", ":")).encode()
        assert _http(
            connector, "POST", connector.binding.path, body,
            _onebot_headers(body, self_id=9999))[0] == 403
        bad_signature = _onebot_headers(body)
        bad_signature["X-Signature"] = "sha1=" + "0" * 40
        assert _http(connector, "POST", connector.binding.path, body, bad_signature)[0] == 401
        assert connector.status()["healthy"] is True
    finally:
        assert connector.stop() is None


def test_onebot_group_requires_bot_at_and_strict_text_segments(tmp_path):
    connector = _onebot_connector(
        tmp_path, target_kind="group", target_id=2001,
        allowed=(1001, 1002), require_at=True)
    connector.start()
    try:
        assert _post_onebot(
            connector, _group_event(1, "not addressed", include_at=False))[0] == 403

        attachment = _group_event(2, "ignored")
        attachment["message"].append({"type": "image", "data": {"file": "secret.jpg"}})
        assert _post_onebot(connector, attachment)[0] == 400

        cq_like = _group_event(3, "[CQ:image,file=x]")
        # Literal text is inert text; only provider segment metadata is privileged.
        assert _post_onebot(connector, cq_like)[0] == 204
        event = connector.poll()[0]
        assert event["raw_text"] == "[CQ:image,file=x]"

        deep = _group_event(4, "deep")
        cursor = deep
        for i in range(10):
            cursor["extra"] = {}
            cursor = cursor["extra"]
        body = json.dumps(deep, separators=(",", ":")).encode()
        assert _http(
            connector, "POST", connector.binding.path, body,
            _onebot_headers(body))[0] == 400
        assert connector.status()["healthy"] is True
    finally:
        assert connector.stop() is None


def _write_profile(path: Path, channel: dict) -> None:
    path.write_text(json.dumps({
        "version": 1, "channels": {"qq": channel},
        "delivery": {"retry_initial_s": 1, "retry_max_s": 10, "batch_size": 8},
    }), encoding="utf-8")
    path.chmod(0o600)


def _webhook_profile(port: int) -> dict:
    return {
        "type": "webhook_v1", "url": "http://127.0.0.1:5700/outbound",
        "token_env": "METARESEARCH_TEST_OUTBOUND_TOKEN", "timeout_s": 0.2,
        "inbound": {
            "type": "webhook_v1", "listen_host": "127.0.0.1",
            "listen_port": port, "path": "/inbound",
            "secret_env": "METARESEARCH_TEST_INBOUND_SECRET",
            "consumer_id": "research-loop", "source_id": "operator-webhook",
            "conversation_id": "operator:primary", "principal_id": "alice",
            "key_id": "key-1", "max_skew_s": 300,
        },
    }


def test_profile_loads_distinct_inbound_secret_erases_env_and_never_projects_it(tmp_path, monkeypatch):
    profile = tmp_path / "connectors.json"
    _write_profile(profile, _webhook_profile(_free_port()))
    monkeypatch.setenv("METARESEARCH_TEST_OUTBOUND_TOKEN", OUTBOUND_SECRET)
    monkeypatch.setenv("METARESEARCH_TEST_INBOUND_SECRET", INBOUND_SECRET)

    loaded = load_connectors(str(profile), work_root=str(tmp_path))
    connector = loaded["channels"]["qq"]
    assert connector.has_inbound is True
    assert "METARESEARCH_TEST_OUTBOUND_TOKEN" not in os.environ
    assert "METARESEARCH_TEST_INBOUND_SECRET" not in os.environ
    projected = json.dumps(connector.status(), ensure_ascii=False, sort_keys=True)
    assert INBOUND_SECRET not in projected and OUTBOUND_SECRET not in projected
    assert connector.inbound.binding.session_base.startswith("connector-inbound-v1:")


def test_profile_rejects_secret_reuse_unknown_fields_and_missing_work_root(tmp_path, monkeypatch):
    profile = tmp_path / "bad-connectors.json"

    same_env = _webhook_profile(_free_port())
    same_env["inbound"]["secret_env"] = same_env["token_env"]
    _write_profile(profile, same_env)
    monkeypatch.setenv("METARESEARCH_TEST_OUTBOUND_TOKEN", OUTBOUND_SECRET)
    with pytest.raises(ConnectorConfigError, match="不得复用"):
        load_connectors(str(profile), work_root=str(tmp_path))

    same_value = _webhook_profile(_free_port())
    _write_profile(profile, same_value)
    monkeypatch.setenv("METARESEARCH_TEST_OUTBOUND_TOKEN", OUTBOUND_SECRET)
    monkeypatch.setenv("METARESEARCH_TEST_INBOUND_SECRET", OUTBOUND_SECRET)
    with pytest.raises(ConnectorConfigError, match="secret value"):
        load_connectors(str(profile), work_root=str(tmp_path))

    weak = _webhook_profile(_free_port())
    _write_profile(profile, weak)
    monkeypatch.setenv("METARESEARCH_TEST_OUTBOUND_TOKEN", OUTBOUND_SECRET)
    monkeypatch.setenv("METARESEARCH_TEST_INBOUND_SECRET", "too-short")
    with pytest.raises(ConnectorConfigError, match="至少 32"):
        load_connectors(str(profile), work_root=str(tmp_path))

    unknown = _webhook_profile(_free_port())
    unknown["inbound"]["forward_raw_body"] = True
    _write_profile(profile, unknown)
    monkeypatch.setenv("METARESEARCH_TEST_OUTBOUND_TOKEN", OUTBOUND_SECRET)
    monkeypatch.setenv("METARESEARCH_TEST_INBOUND_SECRET", INBOUND_SECRET)
    with pytest.raises(ConnectorConfigError, match="未知字段"):
        load_connectors(str(profile), work_root=str(tmp_path))

    valid = _webhook_profile(_free_port())
    _write_profile(profile, valid)
    monkeypatch.setenv("METARESEARCH_TEST_OUTBOUND_TOKEN", OUTBOUND_SECRET)
    monkeypatch.setenv("METARESEARCH_TEST_INBOUND_SECRET", INBOUND_SECRET)
    with pytest.raises(ConnectorConfigError, match="work_root"):
        load_connectors(str(profile))


def test_build_rejects_incomplete_inbound_adapter_before_work_or_database(tmp_path):
    class IncompleteInbound:
        has_inbound = True

        def send(self, _event):
            return {}

        def poll(self):
            return []

        def status(self):
            return {}

    work = tmp_path / "must-not-exist"
    with pytest.raises(ValueError, match="durable inbound 契约不完整"):
        build_system(
            str(Path(__file__).resolve().parent.parent), str(work),
            runner_factory=lambda _td, _pt: None, attack=False,
            outbound_config={
                "channels": {"qq": IncompleteInbound()},
                "retry_initial_s": 1, "retry_max_s": 10, "batch_size": 8,
            })
    assert not work.exists()


@pytest.mark.parametrize("mutation, message", [
    (lambda p: p.update({"listen_host": "0.0.0.0"}), "loopback"),
    (lambda p: p.update({"listen_host": "::1"}), "IPv4"),
    (lambda p: p.update({"path": "/inbound/../escape"}), "path"),
    (lambda p: p.update({"request_timeout_s": 5}), "request_timeout"),
    (lambda p: p.update({"principal_id": "alice:action"}), "保留后缀"),
])
def test_inbound_profile_closes_network_and_path_configuration(tmp_path, mutation, message):
    inbound = _webhook_profile(_free_port())["inbound"]
    mutation(inbound)
    with pytest.raises(InboundConfigError, match=message):
        build_inbound_connector(
            channel="qq", outbound_type="webhook_v1", outbound_profile={},
            inbound_profile=inbound, secret=INBOUND_SECRET,
            outbound_secret=OUTBOUND_SECRET, work_root=str(tmp_path))


def test_remote_connector_cannot_claim_console_trust_domain(tmp_path):
    inbound = _webhook_profile(_free_port())["inbound"]
    with pytest.raises(InboundConfigError, match="本地控制台信任域"):
        build_inbound_connector(
            channel="console", outbound_type="webhook_v1", outbound_profile={},
            inbound_profile=inbound, secret=INBOUND_SECRET,
            outbound_secret=OUTBOUND_SECRET, work_root=str(tmp_path))


class _OutboundStub:
    channel = "qq"
    timeout_s = 0.2

    def send(self, _event):  # pragma: no cover - inbound-only fixture
        raise AssertionError("outbound transport must not be used")


class _TemplateOnlyMediator:
    async_enabled = False
    has_pending_queries = False

    def handle_query(self, *, message_id):  # pragma: no cover - continue has durable template
        raise AssertionError(f"continue template unexpectedly called responder for {message_id}")

    def poll(self):
        return None


def _ingest_env(tmp_path: Path, inbound):
    daemon = WriteDaemon(db.connect(":memory:"))
    conftest.seed_minimal(daemon.conn)
    outbound = _OutboundStub()
    outbound.channel = inbound.binding.channel
    adapter = BidirectionalConnector(outbound, inbound)
    console = Console(daemon)
    ingest = ConnectorChannelIngest(
        console, _TemplateOnlyMediator(), str(tmp_path), adapter)
    return daemon, adapter, ingest


def test_connector_ingest_persists_server_identity_and_running_continue_is_noop(tmp_path):
    inbound = _webhook_connector(tmp_path)
    daemon, _adapter, ingest = _ingest_env(tmp_path, inbound)
    inbound.start()
    try:
        assert _post_webhook(inbound, event_id="continue-1", text="继续")[0] == 200
        assert ingest.ingest() == 1
    finally:
        assert inbound.stop() is None

    row = daemon.query_one(
        "SELECT connector,conversation_id,session_ref,goal_id,goal_ver,raw_text "
        "FROM interaction_message")
    assert row[0:2] == ("qq", "operator:primary")
    assert row[2] == inbound.binding.session_ref("operator-alice")
    assert row[3:] == (1, 1, "继续")
    assert daemon.query_one(
        "SELECT intent,directive_id FROM interaction_classification") == ("query", None)
    assert daemon.query_one("SELECT COUNT(*) FROM directive")[0] == 0
    reply = daemon.query_one("SELECT responder_kind,reply_text FROM interaction_reply")
    assert reply[0] == "template" and "本消息未产生状态变更" in reply[1]


def test_fair_probe_consumes_one_event_per_channel_and_keeps_backlog_visible(tmp_path):
    daemon = WriteDaemon(db.connect(":memory:"))
    conftest.seed_minimal(daemon.conn)
    console = Console(daemon)
    first = _webhook_connector(tmp_path, channel="qq")
    first.start()
    second = _webhook_connector(tmp_path, channel="ops", source_id="operator-ops")
    second.start()
    first_outbound = _OutboundStub()
    first_outbound.channel = "qq"
    second_outbound = _OutboundStub()
    second_outbound.channel = "ops"
    adapters = {
        "qq": BidirectionalConnector(first_outbound, first),
        "ops": BidirectionalConnector(second_outbound, second),
    }
    inbox = ConnectorInboxIngest(
        console, _TemplateOnlyMediator(), str(tmp_path), adapters)
    try:
        for index in range(3):
            assert _post_webhook(
                first, event_id=f"qq-{index}", text=f"备注：qq {index}")[0] == 200
            assert _post_webhook(
                second, event_id=f"ops-{index}", text=f"备注：ops {index}")[0] == 200

        assert inbox.ingest() == 2
        assert daemon.query(
            "SELECT connector,count(*) FROM interaction_message GROUP BY connector ORDER BY connector") == [
                ("ops", 1), ("qq", 1)]
        assert inbox.has_pending is True
        assert first.pending_status()["pending"] == 2
        assert second.pending_status()["pending"] == 2

        assert inbox.ingest() == 2
        assert inbox.has_pending is True
        assert inbox.ingest() == 2
        assert inbox.has_pending is False
        assert daemon.query_one("SELECT count(*) FROM interaction_message") == (6,)
    finally:
        assert first.stop() is None
        assert second.stop() is None


def test_controlled_stop_drains_all_transport_acked_connector_messages(tmp_path):
    daemon = WriteDaemon(db.connect(":memory:"))
    conftest.seed_minimal(daemon.conn)
    inbound = _webhook_connector(tmp_path)
    outbound = _OutboundStub()
    adapter = BidirectionalConnector(outbound, inbound)
    mediator = _TemplateOnlyMediator()
    inbox = ConnectorInboxIngest(
        Console(daemon), mediator, str(tmp_path), {"qq": adapter})
    stop = threading.Event()
    result = []
    errors = []

    class IdleAdvancer:
        last_stop_reason = None
        last_block_reason = None

    def resident_probe():
        # Hold the three records in the durable transport spool so this test
        # exercises the post-listener finite drain, not an ordinary pump tick.
        if inbound.is_stopped():
            inbox.ingest()

    system = System(
        advancer=IdleAdvancer(), state=None, daemon=daemon,
        dual_mode="A", work_root=tmp_path,
        sync_interactions=resident_probe,
        interaction_pending=lambda: inbox.interaction_pending,
        sync_accepted_interactions=inbox.poll_accepted,
        accepted_interaction_pending=lambda: inbox.accepted_interaction_pending,
        sync_closed_inbound=lambda: inbox.ingest(),
        closed_inbound_pending=lambda: inbox.has_pending,
        start_inbound=inbox.start, stop_inbound=inbox.stop,
        raise_inbound=inbox.raise_if_failed,
        inbound_cleanup_pending=lambda owned, _error: bool(owned))

    def run_system():
        try:
            result.extend(system.run_forever(
                0, poll_interval_s=0.01, stop_event=stop))
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append(error)

    thread = threading.Thread(target=run_system, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 2.0
        while not inbound.status()["running"] and time.monotonic() < deadline:
            time.sleep(0.005)
        assert inbound.status()["running"] is True
        for index in range(3):
            assert _post_webhook(
                inbound, event_id=f"exit-{index}", text="继续")[0] == 200
    finally:
        stop.set()
        thread.join(timeout=3.0)

    assert not thread.is_alive()
    assert errors == [] and result == []
    assert inbound.is_stopped() is True
    assert inbound.pending_status() == {"pending": 0, "has_more": False}
    assert daemon.query_one("SELECT count(*) FROM interaction_message") == (3,)
    assert daemon.query_one(
        "SELECT count(*) FROM interaction_classification WHERE intent='query'") == (3,)
    assert daemon.query_one(
        "SELECT count(*) FROM interaction_reply WHERE responder_kind='template'") == (3,)


def test_onebot_group_directive_confirmation_isolated_by_principal(tmp_path):
    inbound = _onebot_connector(
        tmp_path, target_kind="group", target_id=2001,
        allowed=(1001, 1002), require_at=True)
    daemon, _adapter, ingest = _ingest_env(tmp_path, inbound)
    inbound.start()
    try:
        assert _post_onebot(inbound, _group_event(10, "暂停一下", user_id=1001))[0] == 204
        assert ingest.ingest() == 1
        directive_id = daemon.query_one("SELECT id FROM directive WHERE kind='pause'")[0]

        # Bob shares the group conversation, but not Alice's authenticated principal session.
        assert _post_onebot(
            inbound, _group_event(11, f"确认指令 d{directive_id}", user_id=1002))[0] == 204
        assert ingest.ingest() == 0
        assert daemon.query_one(
            "SELECT json_extract(payload_json,'$.confirmed') FROM directive WHERE id=?",
            (directive_id,)) == (0,)
        bob = daemon.query_one(
            "SELECT m.session_ref,c.intent,r.reply_text FROM interaction_message m "
            "JOIN interaction_classification c ON c.message_id=m.id "
            "JOIN interaction_reply r ON r.message_id=m.id WHERE m.raw_text LIKE '确认指令%'")
        assert ":principal:1002:action" in bob[0]
        assert bob[1] == "unclear" and "被拒" in bob[2]

        assert _post_onebot(
            inbound, _group_event(12, f"确认指令 d{directive_id}", user_id=1001))[0] == 204
        assert ingest.ingest() == 1
        confirmed = daemon.query_one(
            "SELECT json_extract(payload_json,'$.confirmed'),"
            "json_extract(payload_json,'$.confirmation_message_id') "
            "FROM directive WHERE id=?", (directive_id,))
        assert confirmed[0] == 1 and isinstance(confirmed[1], int)
        alice_session = daemon.query_one(
            "SELECT session_ref FROM interaction_message WHERE id=?", (confirmed[1],))[0]
        assert ":principal:1001:action" in alice_session
    finally:
        assert inbound.stop() is None


def test_oversized_action_id_is_ordinary_text_not_connector_fatal(tmp_path):
    inbound = _webhook_connector(tmp_path)
    daemon, _adapter, ingest = _ingest_env(tmp_path, inbound)
    inbound.start()
    try:
        text = "确认指令 d" + "9" * 200
        assert _post_webhook(inbound, event_id="large-action-id", text=text)[0] == 200
        assert ingest.ingest() == 1
        assert daemon.query_one(
            "SELECT intent,directive_id FROM interaction_classification") == ("unclear", None)
        assert inbound.status()["healthy"] is True
    finally:
        assert inbound.stop() is None
