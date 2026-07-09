"""CP9.1 · 控制台数据面（console_server）：真表投影 + 派生对象 + 白名单读 + spool 入站。

核心验收：只读组装 /api/db（原型 v2 形状：真表 + status_card + live + notification + policy + FS）；
单写纪律（组装期零 DB 写）；文件白名单防逃逸；入站只写 spool 不碰 DB。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import conftest
from orchestrator import console_server as CS
from orchestrator import database as db

SYSTEM_ROOT = str(Path(__file__).resolve().parent.parent)


@pytest.fixture()
def seeded(tmp_path):
    """最小合法图 + 发布产物（status_card / outbox）落 <work>/state/。"""
    work = tmp_path / "work"
    (work / "state").mkdir(parents=True)
    path = str(work / "research.sqlite")
    conn = db.connect(path)
    conftest.seed_minimal(conn)
    conn.commit(); conn.close()
    (work / "state" / "status_card.json").write_text(json.dumps(
        {"snapshot_cycle": "c1", "goal": {"id": 1, "ver": 1, "summary": "toy 目标"},
         "cycle_status": "done", "route": "bootstrap", "counts": {"open": 0, "inconclusive": 0}},
        ensure_ascii=False), encoding="utf-8")
    (work / "state" / "outbox.jsonl").write_text(
        json.dumps({"event_key": "e1", "kind": "cycle_done", "text": "轮完成"}) + "\n"
        + "{半行撕裂无换行", encoding="utf-8")            # 撕裂尾行须被忽略
    return path, str(work)


# ============ /api/db 组装（原型形状 + 只读）============
def test_assemble_db_shape(seeded):
    path, work = seeded
    payload = CS.assemble_db(path, work, SYSTEM_ROOT)
    # 真表投影（动态列名）：question 表在场且带真列
    assert "question" in payload["tables"] and payload["tables"]["question"]
    q0 = payload["tables"]["question"][0]
    assert {"id", "text", "status"} <= set(q0)                 # 真 DDL 列名
    assert payload["tables"]["baseline"] and "canonical_key" in payload["tables"]["baseline"][0]
    # 派生对象在场
    assert payload["status_card"]["snapshot_cycle"] == "c1"
    assert payload["live"]["mode"] in ("running", "idle", "awaiting_user")
    assert payload["notification"] == [{"event_key": "e1", "kind": "cycle_done", "text": "轮完成"}]  # 撕裂行被弃
    assert "budget" in payload["policy"] and "tree_guard" in payload["policy"]                       # 真 policy.yaml
    assert payload["fs"]["roots"][0]["p"] == "work"            # FS 树含 work 根
    # ledger 当前空 → 空数组（不炸）
    assert payload["ledger_by_cycle"] == [] or isinstance(payload["ledger_by_cycle"], list)


def test_assemble_db_no_db_write(seeded):
    """单写纪律：组装用 mode=ro 连接——即便组装出错也绝不写库（本测证 mode=ro 物理只读）。"""
    path, work = seeded
    ro = CS._open_ro(path)
    with pytest.raises(Exception):                            # mode=ro 写必失败
        ro.execute("INSERT INTO decision(actor,type,payload_json) VALUES ('x','y','{}')")
    ro.close()
    CS.assemble_db(path, work, SYSTEM_ROOT)                   # 组装不抛
    n = db.connect(path).execute("SELECT count(*) FROM decision").fetchone()[0]
    CS.assemble_db(path, work, SYSTEM_ROOT)
    assert db.connect(path).execute("SELECT count(*) FROM decision").fetchone()[0] == n   # 组装前后 DB 不变


def test_live_mode_awaiting_user(seeded):
    """live.mode：有 pending 文件请求 → awaiting_user。"""
    path, work = seeded
    d = db.connect(path)
    d.execute("INSERT INTO interaction_request(goal_id,goal_ver,stage,status,summary_md,items_json,request_hash) "
              "VALUES (1,1,'plan','pending','需数据','[]','rh')")
    d.commit(); d.close()
    assert CS.assemble_db(path, work, SYSTEM_ROOT)["live"]["mode"] == "awaiting_user"


# ============ 文件白名单读 ============
def test_read_file_whitelist_and_escape(seeded):
    path, work = seeded
    data = CS.ConsoleData(db_path=path, work_root=work, system_root=SYSTEM_ROOT)
    assert b"budget" in data.read_file("policies/policy.yaml")           # system schemas/prompts/policies 可读
    assert data.read_file("../../../etc/passwd") is None                 # 逃逸拒
    assert data.read_file("nonexist.txt") is None
    (Path(work) / "state" / "note.txt").write_text("hi", encoding="utf-8")
    assert data.read_file("work/state/note.txt") == b"hi"                # work 下可读


# ============ 入站 spool（只写文件、不碰 DB）============
def test_enqueue_message_spool_only(seeded):
    path, work = seeded
    data = CS.ConsoleData(db_path=path, work_root=work, system_root=SYSTEM_ROOT)
    before = db.connect(path).execute("SELECT count(*) FROM interaction_message").fetchone()[0]
    r1 = data.enqueue_message("暂停一下")
    r2 = data.enqueue_message("q13 现在到哪了？")
    assert r1["seq"] == 1 and r2["seq"] == 2
    lines = (Path(work) / "state" / "console_inbox.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["raw_text"] == "暂停一下"
    # **不碰 DB**：interaction_message 未增（run 进程 ingest 才入库）
    assert db.connect(path).execute("SELECT count(*) FROM interaction_message").fetchone()[0] == before
    with pytest.raises(ValueError):
        data.enqueue_message("   ")                                      # 空消息拒


# ============ HTTP 端到端（真起服务、真请求）============
def test_http_endpoints(seeded):
    import threading
    import urllib.request
    path, work = seeded
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 绕过 shell 的 HTTP_PROXY（本机直连）
    httpd = CS.serve(path, work, SYSTEM_ROOT, host="127.0.0.1", port=0)     # port=0 自选空闲端口
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True); t.start()
    try:
        base = f"http://127.0.0.1:{port}"
        got = json.loads(opener.open(base + "/api/db", timeout=5).read())
        assert got["tables"]["question"] and got["status_card"]["snapshot_cycle"] == "c1"
        f = opener.open(base + "/api/file?p=policies/policy.yaml", timeout=5).read()
        assert b"tree_guard" in f
        req = urllib.request.Request(base + "/api/message", method="POST",
                                     data=json.dumps({"text": "pause"}).encode(),
                                     headers={"Content-Type": "application/json"})
        assert json.loads(opener.open(req, timeout=5).read())["ok"] is True
        assert (Path(work) / "state" / "console_inbox.jsonl").exists()
    finally:
        httpd.shutdown()


def test_malformed_policy_degrades_gracefully(seeded, tmp_path):
    """内审 SHOULD 回归：坏 policy.yaml 不拖垮整个 /api/db（policy 面空、其余照常）——与其余 reader 一致。"""
    path, work = seeded
    bad_root = tmp_path / "badroot"
    (bad_root / "policies").mkdir(parents=True)
    (bad_root / "policies" / "policy.yaml").write_text("{ this: is: not: valid: yaml", encoding="utf-8")
    payload = CS.assemble_db(path, work, str(bad_root))          # 不抛
    assert payload["policy"] == {}                              # 坏配置 → 空 policy
    assert payload["tables"]["question"]                        # 其余面照常


def test_heartbeat_age_seconds(seeded):
    """内审 SHOULD 回归：live.heartbeat_age_s = now - transcript mtime（单键单义、真年龄非绝对 mtime）。"""
    import os, time
    path, work = seeded
    d = db.connect(path)
    d.execute("INSERT INTO runner_call(cycle_id,phase,purpose,status,transcript_ref) "
              "VALUES (1,'bundle','t','running','state/hb.jsonl')")
    d.commit(); d.close()
    hb = Path(work) / "state" / "hb.jsonl"
    hb.write_text("x", encoding="utf-8")
    old = time.time() - 30
    os.utime(hb, (old, old))
    live = CS.assemble_db(path, work, SYSTEM_ROOT)["live"]
    assert "heartbeat_mtime" not in live                        # 旧键已废
    assert 28 <= live["heartbeat_age_s"] <= 40                  # ~30s 年龄


def test_concurrent_enqueue_unique_seq(seeded):
    """内审 SHOULD 回归：ThreadingHTTPServer 并发 POST 下 seq 分配串行——100 并发提交得 100 个唯一 seq。"""
    import threading
    path, work = seeded
    data = CS.ConsoleData(db_path=path, work_root=work, system_root=SYSTEM_ROOT)
    seqs, lock = [], threading.Lock()

    def submit(i):
        r = data.enqueue_message(f"msg-{i}")
        with lock:
            seqs.append(r["seq"])
    ts = [threading.Thread(target=submit, args=(i,)) for i in range(100)]
    for t in ts: t.start()
    for t in ts: t.join()
    assert len(set(seqs)) == 100                               # 无撞 seq
    assert len((Path(work) / "state" / "console_inbox.jsonl").read_text().splitlines()) == 100


def test_read_file_virtual_root_any_work_name(tmp_path):
    """codex SHOULD 回归：虚拟根 'work' 显式映射到 work_root（不管其真实目录叫什么名）——
    --work-root 叫 run123/scratch 等，前端按 FS 树根 'work/...' 拼的路径照样命中（非靠 base.parent 猜）。"""
    work = tmp_path / "run-abc-123"                    # work_root 目录名 != "work"
    (work / "state").mkdir(parents=True)
    (work / "state" / "x.txt").write_text("hi", encoding="utf-8")
    path = str(work / "research.sqlite")
    conn = db.connect(path); conn.commit(); conn.close()
    data = CS.ConsoleData(db_path=path, work_root=str(work), system_root=SYSTEM_ROOT)
    assert data.read_file("work/state/x.txt") == b"hi"     # 虚拟根 work → work_root，不管真名
    assert data.read_file("run-abc-123/state/x.txt") is None  # 真目录名不是虚拟根 → 拒
    assert data.read_file("unknownroot/x") is None            # 非白名单虚拟根 → 拒


def test_read_file_symlink_escape_blocked(tmp_path):
    """codex 关切：白名单目录内 symlink 指向外部 → resolve+containment 拒（不跟出根）。"""
    work = tmp_path / "work"; (work / "state").mkdir(parents=True)
    (tmp_path / "secret.txt").write_text("SECRET", encoding="utf-8")
    (work / "state" / "leak").symlink_to(tmp_path / "secret.txt")
    path = str(work / "research.sqlite"); db.connect(path).close()
    data = CS.ConsoleData(db_path=path, work_root=str(work), system_root=SYSTEM_ROOT)
    assert data.read_file("work/state/leak") is None      # symlink 解析后越界 → 拒


def test_notifications_drops_unterminated_tail(seeded):
    """codex SHOULD 回归：尾行无换行（append 中途）即便是合法 JSON 也丢——committed=换行终止。"""
    path, work = seeded
    ob = Path(work) / "state" / "outbox.jsonl"
    ob.write_text(json.dumps({"event_key": "a", "kind": "k", "text": "done"}) + "\n"
                  + json.dumps({"event_key": "b", "kind": "k", "text": "partial"}), encoding="utf-8")  # 尾行无 \n
    got = CS.assemble_db(path, work, SYSTEM_ROOT)["notification"]
    assert [n["event_key"] for n in got] == ["a"]         # b 未终止 → 丢


def test_api_db_error_generic(seeded, monkeypatch):
    """codex SHOULD 回归：/api/db 组装失败 → 泛化错误（不泄内部细节/路径）。"""
    import threading, urllib.request
    path, work = seeded
    monkeypatch.setattr(CS, "assemble_db", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("secret path /etc/x")))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    httpd = CS.serve(path, work, SYSTEM_ROOT, host="127.0.0.1", port=0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True); t.start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            opener.open(base + "/api/db", timeout=5)
            assert False
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            assert e.code == 500 and "secret path" not in body and "组装失败" in body
    finally:
        httpd.shutdown()
