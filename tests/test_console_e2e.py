"""CP9.4 · 人类控制台端到端验收（步⑨收尾）。

全栈真跑一遍（真 HTTP + 真 DB + 真 spool + 入站闭环），落步⑨步级验证的③②证据：
- ① 真视图（数据层）：CS.serve 起真服务 → GET / 出真控制台页 + GET /api/db 出真 DB 数据 + GET /api/file 白名单读（含负例拒）；
- ③ 单写纪律：console_server 处理 GET/POST 前后 DB 的**逻辑内容快照不变**（观测面零 DB 写；用逻辑 dump 而非主库 sha256，
  因本地文件库可开 WAL——真写可能只落 -wal、主库字节不变，主库 sha 会假绿）；
- ② 入站闭环：POST /api/message 写 spool → ConsoleInboxIngest 消费 server 写的那份 spool → pause directive →
  （确认消息也经 HTTP/spool/ingest）确认 → precheck 阻断；query → grounded 应答（据卡）+ 重放不重复（no-dup）。

（真 Codex 冒烟另见步⑧留证/README §2；本 e2e 用 seed_minimal 真库聚焦「控制台栈 + 入站闭环」真跑，确定性、可回归。）
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import yaml

import conftest
from orchestrator import console_server as CS
from orchestrator import database as db
from orchestrator import status_card as SC
from orchestrator.console import Console
from orchestrator.console_ingest import ConsoleInboxIngest
from orchestrator.mediator import Mediator, open_responder_read_conn
from orchestrator.notify import FileRequestService, make_advancer_precheck
from orchestrator.schemas import SchemaSet
from orchestrator.writedaemon import WriteDaemon

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
POLICY = yaml.safe_load((SYSTEM_ROOT / "policies" / "policy.yaml").read_text(encoding="utf-8"))
TEST_CAPABILITY = "c" * 64
_post_nonce = 0


def _post_headers() -> dict:
    global _post_nonce
    _post_nonce += 1
    return {"Content-Type": "application/json", "Idempotency-Key": f"{_post_nonce:032x}"}


def _db_snapshot(conn) -> str:
    """DB 全表逻辑内容指纹（WAL-proof：捕获**已提交内容**变化，不受 -wal/checkpoint/主库字节影响）。"""
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    parts = []
    for t in tables:
        rows = conn.execute(f"SELECT * FROM {t}").fetchall()               # noqa: S608 —— 表名来自 sqlite_master，非外部输入
        parts.append(f"{t}={len(rows)}:" + "|".join(sorted(repr(r) for r in rows)))
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


@pytest.fixture()
def running(tmp_path):
    """真库 + 已发布卡 + 起真 console_server（后台线程）。返回 base URL + 句柄。"""
    work = tmp_path / "work"; (work / "state").mkdir(parents=True)
    db_path = str(work / "research.sqlite")
    daemon = WriteDaemon(db.connect(db_path))
    conftest.seed_minimal(daemon.conn)
    daemon.conn.execute("UPDATE cycle SET active_question_id=1, route='attack' WHERE id=1")
    daemon.conn.commit()
    card_path = work / "state" / "status_card.json"
    SC.SqliteStatusPublisher(open_responder_read_conn(db_path), policy=POLICY,
                             out_path=str(card_path)).publish("c1")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 绕过 shell HTTP_PROXY（本机直连）
    opener.addheaders = [("Authorization", f"Bearer {TEST_CAPABILITY}")]
    httpd = CS.serve(db_path, str(work), str(SYSTEM_ROOT), host="127.0.0.1", port=0,
                     capability_token=TEST_CAPABILITY)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True); thread.start()
    try:
        yield {"base": f"http://127.0.0.1:{port}", "opener": opener, "work": work,
               "daemon": daemon, "card_path": card_path, "db_path": db_path}
    finally:
        httpd.shutdown(); httpd.server_close(); thread.join(timeout=5)
        daemon.conn.close()


def _get(env, path: str) -> bytes:
    return env["opener"].open(env["base"] + path, timeout=5).read()


def _post(env, text: str, *, idempotency_key: str | None = None):
    headers = _post_headers()
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    req = urllib.request.Request(env["base"] + "/api/message", method="POST",
                                 data=json.dumps({"text": text}).encode(),
                                 headers=headers)
    return json.loads(env["opener"].open(req, timeout=5).read())


def _post_directive(env, action: str, directive_id: int, *, reason: str = ""):
    req = urllib.request.Request(env["base"] + "/api/directive", method="POST",
                                 data=json.dumps({"action": action, "directive_id": directive_id,
                                                  "reason": reason}).encode(),
                                 headers=_post_headers())
    return json.loads(env["opener"].open(req, timeout=5).read())


def _post_file_request(env, action: str, request_id: int, *, source_ref: str = "", reason: str = "",
                       idempotency_key: str | None = None):
    headers = _post_headers()
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    req = urllib.request.Request(env["base"] + "/api/file-request", method="POST",
                                 data=json.dumps({"action": action, "request_id": request_id,
                                                  "source_ref": source_ref, "reason": reason}).encode(),
                                 headers=headers)
    return json.loads(env["opener"].open(req, timeout=5).read())


def test_console_e2e_view_zero_write_and_inbound_loop(running):
    env = running
    daemon = env["daemon"]
    before = _db_snapshot(daemon.conn)                          # ③ 逻辑快照（在任何 console 请求之前）

    # ① 真视图：静态控制台页 + /api/db 真数据 + /api/file 白名单（正例）+ 逃逸负例拒
    page = _get(env, "/").decode("utf-8", "replace")
    assert "adaptPayload" in page and "refreshDB" in page       # 出的是 CP9.2 接入真数据的控制台页（非 mock 原型）
    dbj = json.loads(_get(env, "/api/db"))
    assert dbj["tables"]["question"] and dbj["status_card"]["snapshot_cycle"] == "c1"
    assert [r["p"] for r in dbj["fs"]["roots"]]                 # 真 FS 树（work + schemas/prompts/policies/input）
    assert b"tree_guard" in _get(env, "/api/file?p=policies/policy.yaml")
    with pytest.raises(urllib.error.HTTPError) as ei:           # 白名单负例：逃逸路径拒（404）
        _get(env, "/api/file?p=../../etc/passwd")
    assert ei.value.code == 404

    # ② 控制台下指令：POST /api/message 写 spool
    first_queued = _post(env, "暂停一下")["queued"]

    # ③ 单写纪律【强证】：console_server 全部 DB 访问经 _open_ro（mode=ro）→ 任何写被 SQLite 物理拒——
    # 这覆盖每个 GET/POST（不依赖逐请求快照）；逻辑快照仅作补充行为证。
    ro = CS._open_ro(env["db_path"], work_root=env["work"])
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("INSERT INTO directive(kind,hardness,status,consume_at,payload_json) "
                       "VALUES('note','soft','pending','reasoning_start','{}')")
    finally:
        ro.close()
    assert _db_snapshot(daemon.conn) == before                 # 补充：所有 console GET/POST 前后 DB 逻辑内容不变
    inbox = env["work"] / "state" / "console_inbox.jsonl"
    assert inbox.exists() and "暂停" in inbox.read_text(encoding="utf-8")

    # ② 入站闭环：run 进程 ingest server 写的那份 spool → pause directive → 确认（也经 HTTP/spool/ingest）→ precheck 阻断
    console = Console(daemon)
    mediator = Mediator(daemon, str(env["card_path"]))
    ingest = ConsoleInboxIngest(console, mediator, str(env["work"]))
    base_precheck = make_advancer_precheck(console, daemon)

    def precheck(cyc=None):
        ingest.ingest(cyc)
        return base_precheck(cyc)

    assert precheck() is None                                   # ingest 建 pending 硬指令（未确认 → 不阻断）
    did = daemon.query_one("SELECT id FROM directive WHERE kind='pause' ORDER BY id LIMIT 1")[0]
    pending_snapshot = _db_snapshot(daemon.conn)
    queued = _post_directive(env, "confirm", did)["queued"]     # 显式确认 HTTP → action spool
    assert queued["action"] == "confirm" and queued["directive_id"] == did
    assert queued["seq"] > first_queued["seq"] and queued["idempotency_key"] != first_queued["idempotency_key"]
    assert _db_snapshot(daemon.conn) == pending_snapshot         # action HTTP 仍只写 spool，server 零 DB 写
    assert precheck() == "pause 指令生效中（等待 resume）"        # ingest 先落消息+确认，再同拍消费 pause
    cmid = daemon.query_one(
        "SELECT id FROM interaction_message WHERE idempotency_key=?",
        (queued["idempotency_key"],))[0]
    assert daemon.query_one("SELECT raw_text FROM interaction_message WHERE id=?", (cmid,))[0] == f"确认指令 d{did}"
    bound = daemon.query_one("SELECT json_extract(payload_json,'$.confirmation_message_id') "
                             "FROM directive WHERE id=?", (did,))[0]
    assert bound == cmid                                       # 确认 provenance 真落库绑定到该 directive（非仅阻断即算过）
    assert daemon.query_one("SELECT intent FROM interaction_classification WHERE message_id=?", (cmid,))[0] == "unclear"
    live_after_pause = json.loads(_get(env, "/api/db"))["live"]
    assert live_after_pause["mode"] == "paused" and live_after_pause["block_kind"] == "pause"


def test_console_e2e_query_grounded_and_no_dup(running):
    """query 全链：POST 查询 → ingest → mediator **grounded** 应答落 interaction_reply（据卡）；重放 no-dup。"""
    env = running
    daemon = env["daemon"]
    queued = _post(env, "现在进展如何")["queued"]
    ingest = ConsoleInboxIngest(Console(daemon), Mediator(daemon, str(env["card_path"])), str(env["work"]))
    assert ingest.ingest() == 1
    mid = daemon.query_one(
        "SELECT id FROM interaction_message WHERE idempotency_key=?",
        (queued["idempotency_key"],))[0]
    rep = daemon.query_one("SELECT reply_text FROM interaction_reply WHERE message_id=?", (mid,))
    assert rep is not None
    # grounded：回复据真卡渲染，含快照 c1 / 活跃问题 q1 / 「当前问题」段（非固定串 "ok"）
    assert "快照 c1" in rep[0] and "当前问题" in rep[0] and "q1" in rep[0]
    # no-dup：游标丢/重放既不重复 message 也不重复 reply（按 idempotency key 断言，堵「另建同 key message+reply」假绿口）
    (env["work"] / "state" / "console_inbox.cursor").unlink()
    ingest.ingest()
    assert daemon.query_one("SELECT COUNT(*) FROM interaction_message "
                            "WHERE connector='console' AND idempotency_key=?",
                            (queued["idempotency_key"],))[0] == 1
    assert daemon.query_one("SELECT COUNT(*) FROM interaction_reply r JOIN interaction_message m "
                            "ON r.message_id=m.id WHERE m.idempotency_key=?",
                            (queued["idempotency_key"],))[0] == 1


def test_console_e2e_late_503_retry_converges_on_one_authoritative_message(running, monkeypatch):
    """append 已持久化但 ACK/响应失败时，客户端复用同 key，run 单写消费最终只能有一份权威消息。"""
    env = running
    original_append = CS.ConsoleSpool.append
    calls = 0

    def append_then_lose_first_ack(spool, record):
        nonlocal calls
        calls += 1
        queued = original_append(spool, record)
        if calls == 1:
            raise OSError("simulated failure after durable append")
        return queued

    monkeypatch.setattr(CS.ConsoleSpool, "append", append_then_lose_first_ack)
    client_key = "e" * 32
    with pytest.raises(urllib.error.HTTPError) as late_failure:
        _post(env, "现在进展如何", idempotency_key=client_key)
    assert late_failure.value.code == 503
    queued = _post(env, "现在进展如何", idempotency_key=client_key)["queued"]
    stored_key = "console-" + client_key
    assert queued["idempotency_key"] == stored_key

    records = [json.loads(line) for line in
               (env["work"] / "state" / "console_inbox.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [record["idempotency_key"] for record in records] == [stored_key, stored_key]

    daemon = env["daemon"]
    ingest = ConsoleInboxIngest(Console(daemon), Mediator(daemon, str(env["card_path"])), str(env["work"]))
    ingest.ingest()
    assert daemon.query_one(
        "SELECT COUNT(*) FROM interaction_message WHERE connector='console' AND idempotency_key=?",
        (stored_key,))[0] == 1
    assert daemon.query_one(
        "SELECT COUNT(*) FROM interaction_reply r JOIN interaction_message m ON r.message_id=m.id "
        "WHERE m.connector='console' AND m.idempotency_key=?", (stored_key,))[0] == 1


def test_console_file_request_http_spool_ingest_precheck_loop(running):
    """CP11.2 真闭环：HTTP 只入 spool → run 单写 ingest resolve + provenance → precheck 同拍解阻断。"""
    env, daemon = running, running["daemon"]
    managed_input = env["work"] / "managed-input"
    service = FileRequestService(daemon, SchemaSet(SYSTEM_ROOT / "schemas"), POLICY, str(managed_input))
    request = {"summary_md": "需要控制台提供 toy 数据", "items": [{
        "kind": "dataset", "desc": "toy", "expected_files": ["data.bin"],
        "attempted_paths": ["/data/toy"], "failure_reason": "本机缺失", "dest_hint": "input/"}]}
    rid = service.create_checked(goal_id=1, goal_ver=1, stage="plan", request=request)
    console = Console(daemon)
    base_precheck = make_advancer_precheck(console, daemon)
    assert f"#{rid}" in base_precheck()                         # HTTP 前：真实 pending 阻断

    src = env["work"] / "uploads" / f"r{rid}" / "1"
    src.mkdir(parents=True); (src / "data.bin").write_bytes(b"HTTP-DATA")
    before = _db_snapshot(daemon.conn)
    client_key = "d" * 32
    queued = _post_file_request(
        env, "resolve", rid, source_ref=f"work/uploads/r{rid}",
        idempotency_key=client_key)["queued"]
    assert queued["action_target"] == "file_request" and queued["request_id"] == rid
    assert _db_snapshot(daemon.conn) == before                  # console_server 仍零 DB 写

    ingest = ConsoleInboxIngest(console, Mediator(daemon, str(env["card_path"])), str(env["work"]),
                                file_requests=service, system_root=str(SYSTEM_ROOT))

    def precheck():
        ingest.ingest()
        return base_precheck()

    assert precheck() is None                                  # ingest 终态化后，同拍解除全局等待
    status, resolution_raw, mid = daemon.query_one(
        "SELECT status,resolution_json,resolved_message_id FROM interaction_request WHERE id=?", (rid,))
    assert status == "resolved" and mid is not None
    resolution = json.loads(resolution_raw)
    provided = resolution[0]["provided"][0]
    assert provided["hash"] == hashlib.sha256(b"HTTP-DATA").hexdigest()
    assert Path(provided["path"]).read_bytes() == b"HTTP-DATA"
    assert daemon.query_one("SELECT goal_id,goal_ver FROM interaction_message WHERE id=?", (mid,)) == (1, 1)

    # 游标丢失重放不改 provenance；权威终态可见后，同一客户端 key 也必须允许再次 append：
    # 首次 append 后若 ACK 丢失，HTTP 不能拿可变 status 把安全重试误判成 4xx。
    (env["work"] / "state" / "console_inbox.cursor").unlink()
    assert precheck() is None
    assert daemon.query_one("SELECT resolved_message_id FROM interaction_request WHERE id=?", (rid,))[0] == mid
    shutil.rmtree(env["work"] / "uploads" / f"r{rid}")         # 成功后清理上传源是正常运维动作
    retried = _post_file_request(
        env, "resolve", rid, source_ref=f"work/uploads/r{rid}",
        idempotency_key=client_key)["queued"]
    assert retried["idempotency_key"] == queued["idempotency_key"]
    assert precheck() is None
    assert daemon.query_one(
        "SELECT COUNT(*) FROM interaction_message WHERE idempotency_key=?",
        (queued["idempotency_key"],))[0] == 1
    assert daemon.query_one("SELECT resolved_message_id FROM interaction_request WHERE id=?", (rid,))[0] == mid
