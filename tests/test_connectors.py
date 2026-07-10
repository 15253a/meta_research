"""CP11.2b.3d: real outbound transports, durable retry receipts, and production wiring."""
from __future__ import annotations

import json
import os
import stat
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import conftest
from orchestrator import database as db
from orchestrator.connectors import (ConnectorConfigError, ConnectorDeliveryError,
                                     OneBotV11Connector, OutboundDelivery,
                                     WebhookV1Connector, load_connectors)
from orchestrator.console import Console, DIRECTIVE_ACTION_SESSION_REF
from orchestrator.interaction import InteractionIngest
from orchestrator.notify import InteractionNotifier, Outbox, ResearchNotifier
from orchestrator.console_server import assemble_db
from orchestrator.run import build_system
from orchestrator.run import System
from orchestrator.writedaemon import WriteDaemon


SYSTEM_ROOT = str(Path(__file__).resolve().parent.parent)
PRODUCER_ID = "mr-" + "1" * 32


@contextmanager
def json_server(responder):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            requests.append({
                "path": self.path,
                "headers": {key: value for key, value in self.headers.items()},
                "body": json.loads(body.decode("utf-8")),
            })
            status, headers, value = responder(requests[-1])
            encoded = json.dumps(value).encode("utf-8")
            self.send_response(status)
            for key, item in headers.items():
                self.send_header(key, item)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _event(key="directive:1:received", *, channel=None):
    value = {"producer_id": PRODUCER_ID, "event_key": key, "kind": "directive_received",
             "payload": {"directive_id": 1}}
    if channel is not None:
        value["channel"] = channel
    return value


def test_webhook_v1_real_http_requires_exact_event_ack():
    def responder(request):
        key = request["body"]["event_key"]
        return 200, {}, {"accepted": True, "producer_id": request["body"]["producer_id"],
                         "event_key": key, "delivery_id": "remote-1"}

    with json_server(responder) as (base, requests):
        connector = WebhookV1Connector(
            channel="qq", url=base + "/events", token="secret", timeout_s=2)
        result = connector.send(_event())

    assert result == {"accepted": True, "producer_id": PRODUCER_ID,
                      "event_key": "directive:1:received",
                      "delivery_id": "remote-1"}
    assert requests[0]["headers"]["Idempotency-Key"] == \
        f"{PRODUCER_ID}:directive:1:received"
    assert requests[0]["headers"]["Authorization"] == "Bearer secret"
    assert requests[0]["body"]["protocol_version"] == 1


def test_webhook_rejects_non_echo_ack_and_does_not_accept_redirect():
    with json_server(lambda _r: (200, {}, {"accepted": True, "event_key": "other"})) as (base, _):
        connector = WebhookV1Connector(channel="qq", url=base, token=None, timeout_s=2)
        with pytest.raises(ConnectorDeliveryError, match="精确回显"):
            connector.send(_event())

    with json_server(lambda _r: (307, {"Location": "http://127.0.0.1:1/steal"}, {})) as (base, _):
        connector = WebhookV1Connector(channel="qq", url=base, token="secret", timeout_s=2)
        with pytest.raises(ConnectorDeliveryError, match="HTTP 307"):
            connector.send(_event())


def test_http_deadline_bounds_slow_drip_response():
    class SlowHandler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.end_headers()
            try:
                for _ in range(100):
                    time.sleep(0.03)             # each byte arrives inside socket timeout
                    self.wfile.write(b" ")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connector = WebhookV1Connector(
        channel="qq", url=f"http://127.0.0.1:{server.server_port}/", token=None,
        timeout_s=0.1)
    started = time.monotonic()
    try:
        with pytest.raises(ConnectorDeliveryError, match="transport"):
            connector.send(_event())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert time.monotonic() - started < 0.8


def test_http_retry_after_is_persisted_as_minimum_backoff(tmp_path):
    with json_server(lambda _r: (503, {"Retry-After": "7"}, {"error": "busy"})) as (base, _):
        connector = WebhookV1Connector(channel="qq", url=base, token=None, timeout_s=1)
        outbox = Outbox(str(tmp_path))
        outbox.emit("retry-after", "x", {})
        delivery = OutboundDelivery(
            outbox, {"qq": connector}, default_channels=["qq"],
            retry_initial_s=1, retry_max_s=30)
        assert delivery.tick(100) == []
    retry = outbox.delivery_retry("qq", "retry-after")
    assert retry["last_error_kind"] == "remote_retryable"
    assert retry["next_attempt_at"] == 107


def test_onebot_v11_real_http_payload_contains_event_key_and_human_text():
    def responder(request):
        return 200, {}, {"status": "ok", "retcode": 0,
                         "echo": request["body"]["echo"], "data": {"message_id": 88}}

    with json_server(responder) as (base, requests):
        connector = OneBotV11Connector(
            channel="qq", base_url=base, token="onebot-token", timeout_s=2,
            target_kind="private", target_id=123456, conversation_id="qq:123456")
        result = connector.send(_event())

    request = requests[0]
    assert request["path"] == "/send_private_msg"
    assert request["body"]["user_id"] == 123456
    assert f"事件键：{PRODUCER_ID}:directive:1:received" in request["body"]["message"]
    assert request["body"]["auto_escape"] is True
    assert result["delivery_id"] == "88"


def test_profile_loader_is_bounded_strict_and_reads_secret_only_from_env(tmp_path, monkeypatch):
    profile = tmp_path / "outbound.json"
    profile.write_text(json.dumps({
        "version": 1,
        "channels": {"qq": {"type": "webhook_v1", "url": "https://notify.example/events",
                              "token_env": "METARESEARCH_TEST_CONNECTOR_TOKEN", "timeout_s": 1}},
        "delivery": {"retry_initial_s": 2, "retry_max_s": 30, "batch_size": 7},
    }), encoding="utf-8")
    profile.chmod(0o600)
    monkeypatch.setenv("METARESEARCH_TEST_CONNECTOR_TOKEN", "from-env")

    loaded = load_connectors(str(profile))
    assert set(loaded["channels"]) == {"qq"}
    assert loaded["retry_initial_s"] == 2 and loaded["batch_size"] == 7
    assert loaded["channels"]["qq"].token == "from-env"
    assert "from-env" not in profile.read_text(encoding="utf-8")
    assert "METARESEARCH_TEST_CONNECTOR_TOKEN" not in os.environ  # no child inheritance

    profile.chmod(0o622)
    with pytest.raises(ConnectorConfigError, match="group/other"):
        load_connectors(str(profile))

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"version":1,"version":1,"channels":{}}', encoding="utf-8")
    with pytest.raises(ConnectorConfigError, match="重复"):
        load_connectors(str(duplicate))


@pytest.mark.parametrize("url", ["http://example.com/events", "ftp://127.0.0.1/x",
                                  "http://localhost/events", "https://u:p@example.com/x",
                                  "https://example.com/x?access_token=secret"])
def test_profile_url_rejects_plaintext_remote_and_credential_bearing_urls(tmp_path, url):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"version": 1, "channels": {
        "qq": {"type": "webhook_v1", "url": url}}}), encoding="utf-8")
    with pytest.raises(ConnectorConfigError):
        load_connectors(str(path))


def test_remote_https_connector_requires_authentication(tmp_path):
    path = tmp_path / "unauthenticated.json"
    path.write_text(json.dumps({"version": 1, "channels": {
        "qq": {"type": "webhook_v1", "url": "https://notify.example/events"}}}),
        encoding="utf-8")
    with pytest.raises(ConnectorConfigError, match="token_env"):
        load_connectors(str(path))


def test_profile_requires_auth_even_on_loopback_and_rejects_fifo(tmp_path):
    local = tmp_path / "local.json"
    local.write_text(json.dumps({"version": 1, "channels": {
        "qq": {"type": "webhook_v1", "url": "http://127.0.0.1:5700/events"}}}),
        encoding="utf-8")
    with pytest.raises(ConnectorConfigError, match="loopback"):
        load_connectors(str(local))

    fifo = tmp_path / "profile.fifo"
    os.mkfifo(fifo)
    with pytest.raises(ConnectorConfigError, match="常规文件"):
        load_connectors(str(fifo))


class _FlakyConnector:
    def __init__(self, failures=0):
        self.failures = failures
        self.calls = []

    def send(self, event):
        self.calls.append(event["event_key"])
        if len(self.calls) <= self.failures:
            raise ConnectorDeliveryError("temporary", kind="transport")
        return {"accepted": True, "producer_id": event["producer_id"],
                "event_key": event["event_key"], "delivery_id": "ok"}

    def status(self):
        return {"configured": True}


def test_retry_state_survives_restart_and_receipt_suppresses_future_send(tmp_path):
    outbox = Outbox(str(tmp_path))
    outbox.emit("k1", "x", {})
    flaky = _FlakyConnector(failures=1)
    delivery = OutboundDelivery(
        outbox, {"qq": flaky}, default_channels=["qq"],
        retry_initial_s=2, retry_max_s=10, clock=lambda: 100)

    assert delivery.tick() == []
    retry = outbox.delivery_retry("qq", "k1")
    assert retry["attempt_count"] == 1 and retry["next_attempt_at"] == 102

    restarted_outbox = Outbox(str(tmp_path))
    restarted = OutboundDelivery(
        restarted_outbox, {"qq": flaky}, default_channels=["qq"],
        retry_initial_s=2, retry_max_s=10)
    assert restarted.tick(101) == []
    assert restarted.tick(102) == ["qq:k1"]
    assert restarted_outbox.delivery_retry("qq", "k1") is None

    third = _FlakyConnector()
    assert OutboundDelivery(
        Outbox(str(tmp_path)), {"qq": third}, default_channels=["qq"]).tick(200) == []
    assert third.calls == []
    receipts = [json.loads(line) for line in (tmp_path / "delivery_receipts.jsonl").read_text().splitlines()]
    assert receipts[0]["event_key"] == "k1" and receipts[0]["attempt_count"] == 2


def test_remote_success_before_local_receipt_replays_same_key_not_new_effect(tmp_path, monkeypatch):
    outbox = Outbox(str(tmp_path))
    outbox.emit("k-crash", "x", {})
    remote_effects = set()

    class IdempotentRemote(_FlakyConnector):
        def send(self, event):
            self.calls.append(event["event_key"])
            remote_effects.add(event["event_key"])
            return {"accepted": True, "producer_id": event["producer_id"],
                    "event_key": event["event_key"], "delivery_id": "same"}

    remote = IdempotentRemote()
    original = outbox.record_delivery_success
    crashed = {"once": False}

    def crash_before_receipt(*args, **kwargs):
        if not crashed["once"]:
            crashed["once"] = True
            raise OSError("simulated SIGKILL boundary")
        return original(*args, **kwargs)

    monkeypatch.setattr(outbox, "record_delivery_success", crash_before_receipt)
    with pytest.raises(OSError, match="SIGKILL"):
        OutboundDelivery(outbox, {"qq": remote}, default_channels=["qq"]).tick(1)

    restarted = OutboundDelivery(Outbox(str(tmp_path)), {"qq": remote}, default_channels=["qq"])
    assert restarted.tick(2) == ["qq:k-crash"]
    assert remote.calls == ["k-crash", "k-crash"]
    assert remote_effects == {"k-crash"}


def test_targeted_events_never_cross_connector_channel(tmp_path):
    outbox = Outbox(str(tmp_path))
    outbox.emit("default", "x", {})
    outbox.emit("reply-qq", "x", {}, channel="qq")
    outbox.emit("reply-console", "x", {}, channel="console")
    qq = _FlakyConnector()
    webhook = _FlakyConnector()
    delivery = OutboundDelivery(
        outbox, {"qq": qq, "webhook": webhook}, default_channels=["qq"])
    delivered = delivery.tick(1)
    assert delivered == ["qq:default", "qq:reply-qq"]
    assert qq.calls == ["default", "reply-qq"]
    assert webhook.calls == []


def test_interaction_notifier_derives_query_reply_and_skips_action_clarification(tmp_path):
    daemon = WriteDaemon(db.connect(":memory:"))
    conftest.seed_minimal(daemon.conn)
    ingest = InteractionIngest(daemon)
    console = Console(daemon)
    result = console.handle_inbound(
        connector="qq", conversation_id="a" * 32, raw_text="当前状态是什么？",
        idempotency_key="qq-query", goal_id=1, goal_ver=1)
    ingest.ack(message_id=result["message_id"], reply_text="[快照 c1] 正常",
               snapshot_cycle="c1", reply_role="final-template")
    unclear = console.handle_inbound(
        connector="qq", raw_text="呃", idempotency_key="qq-unclear", goal_id=1, goal_ver=1)
    action = ingest.inbound(
        connector="qq", raw_text="action", idempotency_key="qq-action", goal_id=1, goal_ver=1,
        session_ref=DIRECTIVE_ACTION_SESSION_REF)
    with daemon.transaction() as conn:
        conn.execute("INSERT INTO interaction_classification(message_id,intent,directive_id) "
                     "VALUES (?,'unclear',NULL)", (action,))

    outbox = Outbox(str(tmp_path))
    keys = InteractionNotifier(daemon, outbox).scan()
    events = outbox._events()
    assert f"interaction:{result['message_id']}:reply:1" in keys
    assert f"interaction:{unclear['message_id']}:unclear" in keys
    assert f"interaction:{action}:unclear" not in keys
    assert all(event.get("channel") == "qq" for event in events)
    assert next(e for e in events if e["kind"] == "interaction_reply")["payload"]["reply_text"] == \
        "[快照 c1] 正常"


def test_build_system_wires_interaction_reply_to_configured_connector(tmp_path):
    class Capture(_FlakyConnector):
        def __init__(self):
            super().__init__()
            self.payloads = []

        def send(self, event):
            self.calls.append(event["event_key"])
            self.payloads.append(event["payload"])
            return {"accepted": True, "producer_id": event["producer_id"],
                    "event_key": event["event_key"], "delivery_id": "ok"}

    connector = Capture()
    config = {"channels": {"qq": connector}, "retry_initial_s": 1,
              "retry_max_s": 10, "batch_size": 32}
    system = build_system(
        SYSTEM_ROOT, str(tmp_path), runner_factory=lambda _td, _pt: None,
        attack=False, outbound_config=config)
    result = Console(system.daemon).handle_inbound(
        connector="qq", raw_text="当前状态是什么？", idempotency_key="wired-query",
        goal_id=1, goal_ver=1)
    InteractionIngest(system.daemon).ack(
        message_id=result["message_id"], reply_text="已接地回复", reply_role="final-template")

    system.sync_notifications()
    system.flush_outbound()
    assert any(key.startswith(f"interaction:{result['message_id']}:reply:")
               for key in connector.calls)


def test_slow_network_delivery_never_blocks_interaction_pump(tmp_path):
    outbox = Outbox(str(tmp_path / "state"))
    outbox.emit("slow-event", "x", {})
    release = threading.Event()
    interaction_seen = threading.Event()

    class SlowConnector(_FlakyConnector):
        def send(self, event):
            self.calls.append(event["event_key"])
            assert release.wait(1)
            return {"accepted": True, "producer_id": event["producer_id"],
                    "event_key": event["event_key"], "delivery_id": "slow"}

    connector = SlowConnector()
    delivery = OutboundDelivery(outbox, {"qq": connector}, default_channels=["qq"])

    class Advancer:
        last_stop_reason = None
        last_block_reason = None

        def run_cycles(self, _count):
            assert interaction_seen.wait(1), "network send must run on a different thread"
            release.set()
            return []

    system = System(
        advancer=Advancer(), state=None, daemon=None, dual_mode="A", work_root=tmp_path,
        sync_interactions=interaction_seen.set, outbound_delivery=delivery)
    assert system.run(0) == []
    assert connector.calls == ["slow-event"]


def test_cli_requires_explicit_outbound_choice_and_accepts_real_profile(tmp_path, monkeypatch, capsys):
    import orchestrator.run as run_module

    missing_work = tmp_path / "missing-work"
    assert run_module.main([
        "--system-root", SYSTEM_ROOT, "--work-root", str(missing_work),
        "--max-cycles", "0", "--once",
    ]) == 2
    assert "--no-outbound" in capsys.readouterr().err
    assert not missing_work.exists()              # fail before DB/work mutation

    profile = tmp_path / "outbound.json"
    profile.write_text(json.dumps({
        "version": 1,
        "channels": {"qq": {"type": "webhook_v1", "url": "https://notify.example/events",
                              "token_env": "METARESEARCH_CLI_CONNECTOR_TOKEN"}},
    }), encoding="utf-8")
    profile.chmod(0o600)
    monkeypatch.setenv("METARESEARCH_CLI_CONNECTOR_TOKEN", "secret")
    assert run_module.main([
        "--system-root", SYSTEM_ROOT, "--work-root", str(tmp_path / "configured-work"),
        "--max-cycles", "0", "--once", "--connector-profile", str(profile),
    ]) == 0


def test_console_projection_exposes_retry_and_success_receipts(tmp_path):
    work = tmp_path / "work"
    state = work / "state"
    state.mkdir(parents=True)
    database_path = work / "research.sqlite"
    connection = db.connect(database_path)
    connection.close()
    outbox = Outbox(str(state))
    outbox.emit("observable", "x", {})
    event = outbox._events()[0]
    outbox.record_delivery_failure(
        "qq", event, attempt_count=1, next_attempt_at=20,
        error_kind="transport", error_text="timeout", attempted_at=10)

    pending = assemble_db(str(database_path), str(work), SYSTEM_ROOT)["notification"][0]
    assert pending["deliveries"] == [{
        "status": "retrying", "channel": "qq", "attempt_count": 1,
        "next_attempt_at": 20.0, "last_error_kind": "transport", "last_error": "timeout",
    }]

    outbox.record_delivery_success(
        "qq", event,
        ack={"accepted": True, "producer_id": outbox.producer_id,
             "event_key": "observable", "delivery_id": "r1"},
        accepted_at=30)
    delivered = assemble_db(str(database_path), str(work), SYSTEM_ROOT)["notification"][0]
    assert delivered["deliveries"] == [{
        "status": "delivered", "channel": "qq", "accepted_at": 30.0,
        "attempt_count": 2, "delivery_id": "r1",
    }]
    # Receipt append succeeded but retry-state cleanup crashed: the durable ACK
    # remains authoritative in the read-only operations projection.
    (state / "outbound_delivery_state.json").write_text(json.dumps({
        "version": 1, "events": {"qq\u001fobservable": {
            "channel": "qq", "event_key": "observable", "attempt_count": 1,
            "first_failed_at": 10, "last_attempt_at": 10, "next_attempt_at": 20,
            "last_error_kind": "transport", "last_error": "stale",
        }}
    }), encoding="utf-8")
    projected = assemble_db(str(database_path), str(work), SYSTEM_ROOT)["notification"][0]
    assert projected["deliveries"] == delivered["deliveries"]


def test_research_notifier_covers_failure_block_summary_and_applicability(tmp_path):
    daemon = WriteDaemon(db.connect(":memory:"))
    conftest.seed_minimal(daemon.conn)
    with daemon.transaction() as conn:
        conn.execute("UPDATE cycle SET status='failed',failure_kind='contract' WHERE id=1")
        conn.execute(
            "INSERT INTO cycle(id,goal_id,goal_ver,status,route,cost_total,policy_version,next_intent) "
            "VALUES (2,1,1,'done','reuse_only',3.5,'v0','terminate')")
        conn.execute(
            "INSERT INTO build_target(id,cycle_id,question_id,target_kind,seq,status,failure_kind) "
            "VALUES (3,1,1,'exec',3,'engineering_blocked','environment')")
        conn.execute(
            "INSERT INTO answer_applicability(answer_id,goal_id,goal_ver,audit_cycle,status,rationale_md) "
            "VALUES (1,1,1,2,'obsolete','新目标不再包含旧范围')")

    outbox = Outbox(str(tmp_path))
    notifier = ResearchNotifier(daemon, outbox, audit_cadence_k=2)
    keys = notifier.scan()
    kinds = {event["kind"] for event in outbox._events()}
    assert {"cycle_failed", "engineering_blocked", "cycle_summary",
            "answer_applicability_changed"} <= kinds
    assert "cycle:1:failed" in keys and "cycle:2:summary" in keys
    assert notifier.scan() == []

    with daemon.transaction() as conn:
        conn.execute(
            "UPDATE answer_applicability SET status='blocked',rationale_md='等待人工裁定' "
            "WHERE answer_id=1 AND goal_id=1 AND goal_ver=1")
    changed = notifier.scan()
    assert len([key for key in changed if key.startswith("applicability:1:1:1:")]) == 1
    with daemon.transaction() as conn:
        conn.execute("UPDATE answer_applicability SET rationale_md=? WHERE answer_id=1", ("长" * 100_000,))
    notifier.scan()
    latest = [event for event in outbox._events()
              if event["kind"] == "answer_applicability_changed"][-1]
    assert len(latest["payload"]["rationale_md"]) < 5000
    assert latest["payload"]["rationale_hash"].startswith("sha256:")


def test_outbox_rejects_symlink_state_and_corrupt_delivery_authority(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError, match="真实目录"):
        Outbox(str(linked))

    state = tmp_path / "state"
    outbox = Outbox(str(state))
    outbox.emit("safe", "x", {})
    (state / "outbound_delivery_state.json").write_text(
        '{"version":1,"events":{"bad":{"channel":"qq","event_key":"safe",'
        '"attempt_count":1,"first_failed_at":NaN,"last_attempt_at":1,"next_attempt_at":2,'
        '"last_error_kind":"x","last_error":"x"}}}\n', encoding="utf-8")
    connector = _FlakyConnector()
    with pytest.raises(ValueError, match="非有限"):
        OutboundDelivery(outbox, {"qq": connector}, default_channels=["qq"]).tick(1)
    assert connector.calls == []

    (state / "outbound_delivery_state.json").unlink()
    (state / "delivery_receipts.jsonl").write_text(
        '{"version":1,"channel":"qq","event_key":"safe"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="receipt 损坏"):
        OutboundDelivery(Outbox(str(state)), {"qq": connector}, default_channels=["qq"]).tick(2)
    assert connector.calls == []


def test_outbox_files_are_private_and_duplicate_committed_keys_fail_loud(tmp_path):
    outbox = Outbox(str(tmp_path / "state"))
    outbox.emit("private", "x", {})
    OutboundDelivery(outbox, {"qq": _FlakyConnector()}, default_channels=["qq"]).tick(1)
    assert stat.S_IMODE((tmp_path / "state").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "state" / "outbox.jsonl").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "state" / "delivery_receipts.jsonl").stat().st_mode) == 0o600

    queue = tmp_path / "state" / "outbox.jsonl"
    first = queue.read_text(encoding="utf-8")
    queue.write_text(first + first, encoding="utf-8")
    with pytest.raises(ValueError, match="event_key 重复"):
        Outbox(str(tmp_path / "state")).pending_for_channel("qq", include_default=True)


def test_hundreds_of_event_rescans_use_constant_time_idempotency_cache(tmp_path, monkeypatch):
    outbox = Outbox(str(tmp_path / "state"))
    original = outbox._events_snapshot
    reads = {"n": 0}

    def counted_events():
        reads["n"] += 1
        return original()

    monkeypatch.setattr(outbox, "_events_snapshot", counted_events)
    for index in range(500):
        assert outbox.emit(f"cycle:{index}:summary", "cycle_summary", {"cycle": index})
    for index in range(500):
        assert not outbox.emit(f"cycle:{index}:summary", "cycle_summary", {"cycle": index})
    assert reads["n"] == 1                 # first cache load only; no O(events²) duplicate scan


def test_outbox_fsync_uncertainty_recalibrates_before_retry(tmp_path, monkeypatch):
    outbox = Outbox(str(tmp_path / "state"))
    real_fsync = os.fsync
    failed = {"once": False}

    def durable_then_raise(fd):
        real_fsync(fd)
        if not failed["once"]:
            failed["once"] = True
            raise OSError("fsync result uncertain")

    monkeypatch.setattr(os, "fsync", durable_then_raise)
    with pytest.raises(OSError, match="uncertain"):
        outbox.emit("uncertain", "x", {})
    monkeypatch.setattr(os, "fsync", real_fsync)

    assert outbox.emit("uncertain", "x", {}) is False
    rows = (tmp_path / "state" / "outbox.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1 and json.loads(rows[0])["event_key"] == "uncertain"


@pytest.mark.parametrize("mutation", ["truncate", "unlink"])
def test_outbox_cached_queue_drift_fails_loud_instead_of_losing_event(tmp_path, mutation):
    outbox = Outbox(str(tmp_path / "state"))
    outbox.emit("stable", "x", {})
    queue = tmp_path / "state" / "outbox.jsonl"
    if mutation == "truncate":
        queue.write_bytes(b"")
    else:
        queue.unlink()
    with pytest.raises(OSError, match="过期缓存"):
        outbox.emit("stable", "x", {})


def test_emit_detaches_nested_payload_from_caller_mutation(tmp_path):
    outbox = Outbox(str(tmp_path / "state"))
    payload = {"value": "durable", "nested": [{"items": [1, 2]}]}
    outbox.emit("detached", "x", payload)
    payload["value"] = "mutated"
    payload["nested"][0]["items"].append(3)
    class PayloadCapture(_FlakyConnector):
        def __init__(self):
            super().__init__()
            self.payloads = []

        def send(self, event):
            self.calls.append(event["event_key"])
            self.payloads.append(event["payload"])
            return {"accepted": True, "producer_id": event["producer_id"],
                    "event_key": event["event_key"], "delivery_id": "ok"}

    connector = PayloadCapture()

    OutboundDelivery(outbox, {"qq": connector}, default_channels=["qq"]).tick(1)
    sent = connector.calls
    assert sent == ["detached"]
    assert connector.payloads == [{"value": "durable", "nested": [{"items": [1, 2]}]}]
    disk = json.loads((tmp_path / "state" / "outbox.jsonl").read_text(encoding="utf-8"))
    assert disk["payload"] == {"value": "durable", "nested": [{"items": [1, 2]}]}


def test_torn_receipt_tail_is_repaired_before_replay_success(tmp_path):
    state = tmp_path / "state"
    outbox = Outbox(str(state))
    outbox.emit("receipt-repair", "x", {})
    (state / "delivery_receipts.jsonl").write_bytes(b'{"version":1,"channel":"qq"')
    connector = _FlakyConnector()

    assert OutboundDelivery(
        outbox, {"qq": connector}, default_channels=["qq"]).tick(1) == ["qq:receipt-repair"]
    assert OutboundDelivery(
        Outbox(str(state)), {"qq": connector}, default_channels=["qq"]).tick(2) == []
    lines = (state / "delivery_receipts.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["event_key"] == "receipt-repair"
    assert connector.calls == ["receipt-repair"]


def test_receipt_fsync_uncertainty_does_not_resend_committed_ack(tmp_path, monkeypatch):
    state = tmp_path / "state"
    outbox = Outbox(str(state))
    outbox.emit("receipt-uncertain", "x", {})
    connector = _FlakyConnector()
    real_fsync = os.fsync
    failed = {"once": False}

    def durable_then_raise(fd):
        real_fsync(fd)
        if not failed["once"]:
            failed["once"] = True
            raise OSError("receipt fsync uncertain")

    monkeypatch.setattr(os, "fsync", durable_then_raise)
    with pytest.raises(OSError, match="receipt fsync uncertain"):
        OutboundDelivery(outbox, {"qq": connector}, default_channels=["qq"]).tick(1)
    monkeypatch.setattr(os, "fsync", real_fsync)

    assert OutboundDelivery(
        Outbox(str(state)), {"qq": connector}, default_channels=["qq"]).tick(2) == []
    assert connector.calls == ["receipt-uncertain"]


def test_receipt_cache_avoids_quadratic_full_log_reads(tmp_path, monkeypatch):
    outbox = Outbox(str(tmp_path / "state"))
    for index in range(100):
        outbox.emit(f"fast:{index}", "x", {})
    original = outbox._read_regular_snapshot
    receipt_reads = {"n": 0}

    def counted(path, **kwargs):
        if path == outbox.receipts_path:
            receipt_reads["n"] += 1
        return original(path, **kwargs)

    monkeypatch.setattr(outbox, "_read_regular_snapshot", counted)
    connector = _FlakyConnector()
    delivered = OutboundDelivery(
        outbox, {"qq": connector}, default_channels=["qq"], batch_size=256).tick(1)
    assert len(delivered) == 100
    assert receipt_reads["n"] <= 2       # bind/repair once, then fingerprint-only membership


def test_future_retry_backlog_reads_retry_document_once_per_tick(tmp_path, monkeypatch):
    state = tmp_path / "state"
    outbox = Outbox(str(state))
    events = {}
    for index in range(500):
        key = f"future:{index}"
        outbox.emit(key, "cycle_summary", {"cycle_id": f"c{index}"})
        events[f"qq\x1f{key}"] = {
            "channel": "qq", "event_key": key, "attempt_count": 1,
            "first_failed_at": 1, "last_attempt_at": 1, "next_attempt_at": 1000,
            "last_error_kind": "transport", "last_error": "later",
        }
    (state / "outbound_delivery_state.json").write_text(
        json.dumps({"version": 1, "events": events}) + "\n", encoding="utf-8")
    original = outbox._read_regular_snapshot
    retry_reads = {"n": 0}

    def counted(path, **kwargs):
        if path == outbox.retry_path:
            retry_reads["n"] += 1
        return original(path, **kwargs)

    monkeypatch.setattr(outbox, "_read_regular_snapshot", counted)
    connector = _FlakyConnector()
    assert OutboundDelivery(
        outbox, {"qq": connector}, default_channels=["qq"], batch_size=256).tick(1) == []
    assert connector.calls == []
    assert retry_reads["n"] == 1


def test_zero_byte_retry_authority_fails_loud(tmp_path):
    state = tmp_path / "state"
    outbox = Outbox(str(state))
    outbox.emit("zero-retry", "x", {})
    (state / "outbound_delivery_state.json").write_bytes(b"")
    with pytest.raises(ValueError, match="为空"):
        OutboundDelivery(
            Outbox(str(state)), {"qq": _FlakyConnector()}, default_channels=["qq"]).tick(1)


def test_restart_prunes_retry_state_dominated_by_receipt(tmp_path):
    state = tmp_path / "state"
    outbox = Outbox(str(state))
    outbox.emit("cleanup-crash", "x", {})
    event = outbox._events()[0]
    outbox.record_delivery_failure(
        "qq", event, attempt_count=1, next_attempt_at=20,
        error_kind="transport", error_text="timeout", attempted_at=10)
    outbox.record_delivery_success(
        "qq", event,
        ack={"accepted": True, "producer_id": outbox.producer_id,
             "event_key": "cleanup-crash", "delivery_id": "remote"},
        accepted_at=11)
    # Recreate the exact receipt-success / retry-cleanup crash window.
    (state / "outbound_delivery_state.json").write_text(json.dumps({
        "version": 1, "events": {"qq\x1fcleanup-crash": {
            "channel": "qq", "event_key": "cleanup-crash", "attempt_count": 1,
            "first_failed_at": 10, "last_attempt_at": 10, "next_attempt_at": 20,
            "last_error_kind": "transport", "last_error": "stale",
        }}
    }) + "\n", encoding="utf-8")

    connector = _FlakyConnector()
    assert OutboundDelivery(
        Outbox(str(state)), {"qq": connector}, default_channels=["qq"]).tick(12) == []
    assert connector.calls == []
    stored = json.loads((state / "outbound_delivery_state.json").read_text(encoding="utf-8"))
    assert stored == {"version": 1, "events": {}}


def test_two_work_roots_use_distinct_remote_idempotency_namespaces(tmp_path):
    effects = set()

    def responder(request):
        identity = (request["body"]["producer_id"], request["body"]["event_key"])
        effects.add(identity)
        return 200, {}, {"accepted": True, "producer_id": identity[0],
                         "event_key": identity[1], "delivery_id": "ok"}

    with json_server(responder) as (base, requests):
        connector = WebhookV1Connector(channel="qq", url=base, token=None, timeout_s=2)
        for name in ("run-a", "run-b"):
            outbox = Outbox(str(tmp_path / name))
            outbox.emit("cycle:1:failed", "cycle_failed", {"cycle_id": "c1"})
            assert OutboundDelivery(
                outbox, {"qq": connector}, default_channels=["qq"]).tick(1)

    assert len(effects) == 2
    assert requests[0]["body"]["event_key"] == requests[1]["body"]["event_key"]
    assert requests[0]["body"]["producer_id"] != requests[1]["body"]["producer_id"]


def test_webhook_redacts_legacy_file_resolution_at_last_mile():
    def responder(request):
        return 200, {}, {
            "accepted": True, "producer_id": request["body"]["producer_id"],
            "event_key": request["body"]["event_key"], "delivery_id": "safe",
        }

    event = _event("filereq:1:resolved")
    event["kind"] = "file_request_resolved"
    event["payload"] = {
        "request_id": 1, "summary_md": "done", "status": "resolved",
        "resolution": [{"provided": [{"path": "/private/patient.csv",
                                         "preview": "SECRET_ROW=alice"}]}],
    }
    with json_server(responder) as (base, requests):
        WebhookV1Connector(channel="qq", url=base, token=None, timeout_s=2).send(event)
    encoded = json.dumps(requests[0]["body"], ensure_ascii=False)
    assert all(secret not in encoded for secret in (
        "/private", "patient.csv", "SECRET_ROW", "preview", "resolution\""))
    assert requests[0]["body"]["payload"]["resolution_summary"]["provided_file_count"] == 1


def test_producer_id_restart_migration_and_loss_guards(tmp_path):
    state = tmp_path / "stable"
    first = Outbox(str(state))
    assert Outbox(str(state)).producer_id == first.producer_id

    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "outbox.jsonl").write_text(
        '{"event_key":"old","kind":"x","payload":{}}\n', encoding="utf-8")
    migrated = Outbox(str(legacy))
    assert migrated.pending_for_channel("qq", include_default=True)[0]["event_key"] == "old"

    broken = tmp_path / "partial-first-init"
    broken.mkdir()
    (broken / "outbound_producer_id").write_bytes(b"mr-half")
    repaired = Outbox(str(broken))
    assert repaired.producer_id.startswith("mr-")

    first.emit("delivered", "x", {})
    OutboundDelivery(first, {"qq": _FlakyConnector()}, default_channels=["qq"]).tick(1)
    (state / "outbound_producer_id").unlink()
    with pytest.raises(OSError, match="缺 outbound_producer_id"):
        Outbox(str(state))


def test_concurrent_first_init_returns_one_producer_namespace(tmp_path, monkeypatch):
    original = Outbox._load_or_create_producer_id_locked
    entered = threading.Event()
    release = threading.Event()
    first = {"value": True}

    def paused(self):
        if first["value"]:
            first["value"] = False
            entered.set()
            assert release.wait(1)
        return original(self)

    monkeypatch.setattr(Outbox, "_load_or_create_producer_id_locked", paused)
    values = []
    errors = []

    def construct():
        try:
            values.append(Outbox(str(tmp_path / "state")).producer_id)
        except BaseException as error:
            errors.append(error)

    first_thread = threading.Thread(target=construct)
    second_thread = threading.Thread(target=construct)
    first_thread.start()
    assert entered.wait(1)
    second_thread.start()
    time.sleep(0.02)
    assert second_thread.is_alive()          # serialized behind producer init lock
    release.set()
    first_thread.join(timeout=2)
    second_thread.join(timeout=2)
    assert not errors and len(values) == 2 and values[0] == values[1]


def test_local_event_key_must_fit_namespaced_wire_boundary(tmp_path):
    outbox = Outbox(str(tmp_path / "state"))
    assert outbox.emit("a" * 220, "x", {})
    with pytest.raises(ValueError, match="线协议上限"):
        outbox.emit("b" * 221, "x", {})


def test_query_reply_overtakes_poison_and_large_unrelated_backlog(tmp_path):
    outbox = Outbox(str(tmp_path / "state"))
    outbox.emit("old-bad", "x", {})
    for index in range(500):
        outbox.emit(f"old:{index}", "cycle_summary", {"cycle_id": f"c{index}"})

    class SelectiveConnector(_FlakyConnector):
        def send(self, event):
            self.calls.append(event["event_key"])
            if event["event_key"] == "old-bad":
                raise ConnectorDeliveryError("permanent", kind="remote_rejected")
            return {"accepted": True, "producer_id": event["producer_id"],
                    "event_key": event["event_key"], "delivery_id": "ok"}

    connector = SelectiveConnector()
    delivery = OutboundDelivery(
        outbox, {"qq": connector}, default_channels=["qq"],
        retry_initial_s=300, retry_max_s=300, batch_size=32)
    delivery.tick(1)
    outbox.emit("interaction:7:received", "interaction_received",
                {"message_id": 7, "conversation_id": "qq:7"}, channel="qq")
    outbox.emit("interaction:7:reply:1", "interaction_reply",
                {"message_id": 7, "conversation_id": "qq:7", "reply_text": "ready"},
                channel="qq")

    connector.calls.clear()
    delivery.tick(2)
    assert connector.calls[0] == "interaction:7:received"
    assert connector.calls.index("interaction:7:reply:1") <= 2
    assert "old-bad" not in connector.calls


def test_scheduler_reserves_low_priority_progress_after_urgent_first(tmp_path):
    outbox = Outbox(str(tmp_path / "state"))
    for message_id in (1, 2):
        outbox.emit(f"interaction:{message_id}:received", "interaction_received",
                    {"message_id": message_id, "conversation_id": f"qq:{message_id}"})
        outbox.emit(f"interaction:{message_id}:reply:1", "interaction_reply",
                    {"message_id": message_id, "conversation_id": f"qq:{message_id}",
                     "reply_text": "ok"})
    outbox.emit("audit:old", "cycle_summary", {"cycle_id": "c1"})
    connector = _FlakyConnector()

    OutboundDelivery(
        outbox, {"qq": connector}, default_channels=["qq"], batch_size=4).tick(1)
    assert connector.calls[0].startswith("interaction:")
    assert "audit:old" in connector.calls       # reserved lower-priority slot in same tick


def test_unrelated_engineering_failure_groups_do_not_block_each_other(tmp_path):
    outbox = Outbox(str(tmp_path / "state"))
    outbox.emit("target:1:blocked", "engineering_blocked", {"build_target_id": "t1"})
    outbox.emit("target:2:blocked", "engineering_blocked", {"build_target_id": "t2"})

    class FirstFails(_FlakyConnector):
        def send(self, event):
            self.calls.append(event["event_key"])
            if event["event_key"] == "target:1:blocked":
                raise ConnectorDeliveryError("bad target", kind="remote_rejected")
            return {"accepted": True, "producer_id": event["producer_id"],
                    "event_key": event["event_key"], "delivery_id": "ok"}

    connector = FirstFails()
    assert OutboundDelivery(
        outbox, {"qq": connector}, default_channels=["qq"]).tick(1) == ["qq:target:2:blocked"]


def test_delivery_stop_finishes_current_send_but_starts_no_next_event(tmp_path):
    outbox = Outbox(str(tmp_path / "state"))
    outbox.emit("stop:1", "x", {})
    outbox.emit("stop:2", "y", {})
    started = threading.Event()
    release = threading.Event()

    class BlockingConnector(_FlakyConnector):
        timeout_s = 1

        def send(self, event):
            self.calls.append(event["event_key"])
            started.set()
            assert release.wait(1)
            return {"accepted": True, "producer_id": event["producer_id"],
                    "event_key": event["event_key"], "delivery_id": "ok"}

    connector = BlockingConnector()
    delivery = OutboundDelivery(outbox, {"qq": connector}, default_channels=["qq"])
    assert delivery.start(0.05)
    assert started.wait(1)
    stopped = []
    stopper = threading.Thread(target=lambda: stopped.append(delivery.stop()))
    stopper.start()
    deadline = time.monotonic() + 1
    while delivery._worker_stop is not None and not delivery._worker_stop.is_set():
        assert time.monotonic() < deadline
        time.sleep(0.005)
    release.set()
    stopper.join(timeout=2)
    assert not stopper.is_alive() and stopped == [None]
    assert connector.calls == ["stop:1"]


def test_onebot_requires_request_bound_ack_and_conversation(tmp_path, monkeypatch):
    connector = OneBotV11Connector(
        channel="qq", base_url="http://127.0.0.1:5700", token=None, timeout_s=1,
        target_kind="private", target_id=7, conversation_id="qq:7")
    monkeypatch.setattr(connector, "_post", lambda *_args, **_kwargs: {
        "status": "ok", "retcode": 0, "data": {"message_id": 1}})
    with pytest.raises(ConnectorDeliveryError, match="echo"):
        connector.send(_event())

    mismatched = _event("interaction:1:reply")
    mismatched["kind"] = "interaction_reply"
    mismatched["payload"] = {"message_id": 1, "conversation_id": "qq:other",
                             "reply_text": "secret"}
    with pytest.raises(ConnectorDeliveryError, match="conversation_id"):
        connector.send(mismatched)


def test_http_connect_phase_is_inside_total_wall_deadline(monkeypatch):
    import http.client

    def slow_connect(_self):
        time.sleep(0.35)

    monkeypatch.setattr(http.client.HTTPConnection, "connect", slow_connect)
    connector = WebhookV1Connector(
        channel="qq", url="http://127.0.0.1:9/events", token=None, timeout_s=0.1)
    started = time.monotonic()
    with pytest.raises(ConnectorDeliveryError, match="总墙钟超时"):
        connector.send(_event())
    assert time.monotonic() - started < 0.25


def test_console_never_projects_malformed_receipt_as_delivered(tmp_path):
    work = tmp_path / "work"
    state = work / "state"
    state.mkdir(parents=True)
    database_path = work / "research.sqlite"
    db.connect(database_path).close()
    outbox = Outbox(str(state))
    outbox.emit("not-delivered", "x", {})
    (state / "delivery_receipts.jsonl").write_text(
        '{"event_key":"not-delivered"}\n', encoding="utf-8")

    notifications = assemble_db(str(database_path), str(work), SYSTEM_ROOT)["notification"]
    original = next(item for item in notifications if item["event_key"] == "not-delivered")
    assert not original.get("deliveries")
    corrupt = next(item for item in notifications
                   if item["kind"] == "transport_authority_corrupt")
    assert corrupt["deliveries"][0]["status"] == "authority_corrupt"


def test_resident_system_surfaces_delivery_worker_local_failure(tmp_path):
    state = tmp_path / "state"
    outbox = Outbox(str(state))
    outbox.emit("health", "x", {})
    (state / "outbound_delivery_state.json").write_text(
        '{"version":1,"events":{"bad":NaN}}\n', encoding="utf-8")
    delivery = OutboundDelivery(
        outbox, {"qq": _FlakyConnector()}, default_channels=["qq"])

    class IdleAdvancer:
        last_stop_reason = None
        last_block_reason = None

        def run_cycles(self, _count):
            return []

    system = System(
        advancer=IdleAdvancer(), state=None, daemon=None, dual_mode="A", work_root=tmp_path,
        outbound_delivery=delivery)
    started = time.monotonic()
    with pytest.raises(ValueError, match="非有限"):
        system.run_forever(0, poll_interval_s=0.01, linger_after_terminal=True)
    assert time.monotonic() - started < 1
    assert delivery.status()["_worker"]["healthy"] is False
