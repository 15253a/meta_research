"""CP6.3 · 通知矩阵 outbox + 文件请求全流水 + 全局等待（§4.6.6/§4.6.8/§4.4.1；M5 收尾）。

核心验收面（§7.1 M5）：directive 每状态迁移均外显（**逐态推送断言**）；outbox 幂等投递；文件请求
全流水（创建拒绝三负例 + schema 拒 + uploads hash 入账并入 input/user_provided/ + 恢复推进）；
**全局等待**（pending 请求 → Advancer 不发新研究 runner call **而 query/通知照常**）。
"""
from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import conftest
from orchestrator import database as db
from orchestrator import status_card as SC
from orchestrator.console import (DIRECTIVE_ACTION_SESSION_REF, Console,
                                  directive_action_text)
from orchestrator.compiler_sqlite import SqliteCompiler
from orchestrator.interaction import InteractionIngest
from orchestrator.mediator import Mediator, open_responder_read_conn
from orchestrator.notify import (DirectiveNotifier, FileRequestNotifier, FileRequestReject,
                                 FileRequestService, Outbox, make_advancer_precheck)
from orchestrator.resource_limits import MAX_REASONING_DIRECTIVES_PER_CYCLE
from orchestrator.schemas import SchemaSet
from orchestrator.writedaemon import WriteDaemon

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
POLICY = yaml.safe_load((SYSTEM_ROOT / "policies" / "policy.yaml").read_text(encoding="utf-8"))
SCHEMAS = SchemaSet(SYSTEM_ROOT / "schemas")


class FakeConnector:
    """收集式 connector（M5 测试）：send 记录事件；可注入故障。"""
    def __init__(self):
        self.sent = []
        self.fail_after = None
    def send(self, payload):
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            raise RuntimeError("connector 故障注入")
        self.sent.append(payload)
        return {"ok": True}


@pytest.fixture()
def env(tmp_path):
    daemon = WriteDaemon(db.connect(":memory:"))
    conftest.seed_minimal(daemon.conn)
    outbox = Outbox(str(tmp_path / "outbox"))
    return {"d": daemon, "c": Console(daemon, policy=POLICY), "ob": outbox,
            "dn": DirectiveNotifier(daemon, outbox), "tmp": tmp_path}


def _file_daemon(tmp_path):
    """需要第二只 mode=ro 连接观察已提交真相的测试不能使用 :memory:。"""
    daemon = WriteDaemon(db.connect(str(tmp_path / "research.sqlite")))
    conftest.seed_minimal(daemon.conn)
    return daemon


def _action_message(d, c, result, action):
    """模拟 ConsoleInboxIngest 落下的结构化控件 provenance。"""
    did = result["directive_id"]
    goal = d.query_one(
        "SELECT m.goal_id,m.goal_ver FROM directive x JOIN interaction_message m "
        "ON m.id=x.source_interaction_message_id WHERE x.id=?", (did,))
    mid = c.ingest.inbound(
        connector="test-console-action", raw_text=directive_action_text(action, did),
        idempotency_key=f"test-{action}-d{did}", goal_id=goal[0], goal_ver=goal[1],
        session_ref=DIRECTIVE_ACTION_SESSION_REF)
    with d.transaction() as conn:
        conn.execute("INSERT INTO interaction_classification(message_id,intent,directive_id) "
                     "VALUES (?,'unclear',NULL)", (mid,))
    return mid


def _items(n=1):
    return [{"kind": "dataset", "desc": f"数据集{i}", "expected_files": ["data.bin"],
             "attempted_paths": ["/data/公共区已找过"], "failure_reason": "镜像不含该集",
             "dest_hint": "input/"} for i in range(n)]


def _request(n=1):
    return {"summary_md": "需要外部数据集：镜像不含、无法自行获取", "items": _items(n)}


def test_precheck_applies_durable_set_budget_semantics(env):
    d, c = env["d"], env["c"]
    result = c.handle_inbound(
        connector="qq", raw_text="设置预算 50", idempotency_key="unsupported-budget",
        goal_id=1, goal_ver=1)
    c.confirm_directive(
        directive_id=result["directive_id"],
        confirm_message_id=_action_message(d, c, result, "confirm"))

    assert make_advancer_precheck(c, d)() is None
    status, payload = d.query_one(
        "SELECT status,payload_json FROM directive WHERE id=?", (result["directive_id"],))
    assert status == "consumed"
    parsed = json.loads(payload)
    assert parsed["budget_patch"] == {"session_max": 50.0}
    actor, kind, effect_limit = d.query_one(
        "SELECT actor,type,json_extract(payload_json,'$.effect.budget.session_max') "
        "FROM decision WHERE directive_id=? ORDER BY id DESC LIMIT 1",
        (result["directive_id"],))
    assert (actor, kind, effect_limit) == ("human", "directive_set_budget", 50.0)


def test_precheck_rejects_reasoning_directive_overflow_before_consumption(env):
    """第 129 条不能先 consumed 再让 compiler 永久报错；超额项须可审计 rejected，前 128 条可渲染。"""
    d, c = env["d"], env["c"]
    for index in range(MAX_REASONING_DIRECTIVES_PER_CYCLE + 1):
        c.handle_inbound(
            connector="qq", raw_text=f"备注：批量控制输入 {index}",
            idempotency_key=f"reasoning-note-{index}", goal_id=1, goal_ver=1)

    cyc = SimpleNamespace(cycle_id="c1", route="decompose", status="reasoning")
    assert make_advancer_precheck(c, d)(cyc) is None
    assert d.query_one(
        "SELECT count(*) FROM directive WHERE status='consumed' AND consumed_cycle=1 "
        "AND consume_at='reasoning_start'")[0] == MAX_REASONING_DIRECTIVES_PER_CYCLE
    rejected_id, payload_raw = d.query_one(
        "SELECT id,payload_json FROM directive WHERE status='rejected' ORDER BY id DESC LIMIT 1")
    payload = json.loads(payload_raw)
    assert payload["rejection_kind"] == "application_unavailable"
    assert "上下文安全上限" in payload["rejection_reason"]
    assert d.query_one(
        "SELECT actor,type FROM decision WHERE directive_id=? ORDER BY id DESC LIMIT 1",
        (rejected_id,)) == ("orchestrator", "directive_application_rejected")

    pack = SqliteCompiler(d.conn, POLICY).render(
        cycle_id="c1", stage="reasoning")
    assert f'"directive_id":"d{MAX_REASONING_DIRECTIVES_PER_CYCLE}"' in pack.anchor_md
    assert f'"directive_id":"d{MAX_REASONING_DIRECTIVES_PER_CYCLE + 1}"' not in pack.anchor_md

    # Prompt capacity must never reject the operational controls needed to stop or unpause the same cycle.
    pause = c.handle_inbound(
        connector="qq", raw_text="暂停", idempotency_key="capacity-pause", goal_id=1, goal_ver=1)
    resume = c.handle_inbound(
        connector="qq", raw_text="继续", idempotency_key="capacity-resume", goal_id=1, goal_ver=1)
    c.confirm_directive(
        directive_id=pause["directive_id"],
        confirm_message_id=_action_message(d, c, pause, "confirm"))
    c.confirm_directive(
        directive_id=resume["directive_id"],
        confirm_message_id=_action_message(d, c, resume, "confirm"))
    assert make_advancer_precheck(c, d)(cyc) is None
    assert d.query(
        "SELECT status FROM directive WHERE id IN (?,?) ORDER BY id",
        (pause["directive_id"], resume["directive_id"])) == [("consumed",), ("consumed",)]
    SqliteCompiler(d.conn, POLICY).render(
        cycle_id="c1", stage="reasoning")


def test_goal_amend_round_reserves_reasoning_boundary_from_old_notes(env):
    """专用改版轮先且只消费 amendment；旧 note 不得吃满配额后永久拒掉用户改目标。"""
    d, c = env["d"], env["c"]
    for index in range(MAX_REASONING_DIRECTIVES_PER_CYCLE):
        c.handle_inbound(
            connector="qq", raw_text=f"备注：改版前积压 {index}",
            idempotency_key=f"pre-amend-note-{index}", goal_id=1, goal_ver=1)
    amend = c.handle_inbound(
        connector="qq", raw_text="修订目标：配额下仍须生效", idempotency_key="capacity-amend",
        goal_id=1, goal_ver=1)
    c.confirm_directive(
        directive_id=amend["directive_id"],
        confirm_message_id=_action_message(d, c, amend, "confirm"))
    with d.transaction() as conn:
        conn.execute("UPDATE cycle SET route='goal_amend',status='created' WHERE id=1")
        conn.execute(
            "INSERT INTO decision(cycle_id,directive_id,actor,type,payload_json) "
            "VALUES (1,?,'orchestrator','goal_amend_routed','{\"route\":\"goal_amend\"}')",
            (amend["directive_id"],))

    cyc = SimpleNamespace(cycle_id="c1", route="goal_amend", status="created")
    assert make_advancer_precheck(c, d)(cyc) is None
    assert d.query_one(
        "SELECT status,consumed_cycle FROM directive WHERE id=?",
        (amend["directive_id"],)) == ("consumed", 1)
    assert d.query_one(
        "SELECT count(*) FROM directive WHERE kind='note' AND status='pending'")[0] == \
        MAX_REASONING_DIRECTIVES_PER_CYCLE
    assert d.query_one(
        "SELECT count(*) FROM directive WHERE status='consumed' AND consumed_cycle=1 "
        "AND consume_at='reasoning_start'")[0] == 1
    pack = SqliteCompiler(d.conn, POLICY).render(cycle_id="c1", stage="reasoning")
    assert '"kind":"goal_amend"' in pack.anchor_md
    assert "改版前积压" not in pack.anchor_md


def test_precheck_terminally_rejects_abort_on_active_question_drift(env):
    """权威 cycle/question 漂移时 abort 不得半写或每拍重撞；终态拒绝后保留现场供修复。"""
    d, c = env["d"], env["c"]
    with d.transaction() as conn:
        conn.execute(
            "INSERT INTO question(id,goal_id,goal_ver,born_goal_ver,text,status,source) "
            "VALUES (2,1,1,1,'状态漂移问题','open','agent')")
        conn.execute("UPDATE cycle SET active_question_id=2 WHERE id=1")
    result = c.handle_inbound(
        connector="qq", raw_text="abort 本轮", idempotency_key="abort-drift",
        goal_id=1, goal_ver=1)
    c.confirm_directive(
        directive_id=result["directive_id"],
        confirm_message_id=_action_message(d, c, result, "confirm"))

    assert make_advancer_precheck(c, d)() is None
    assert d.query_one("SELECT status,active_question_id FROM cycle WHERE id=1") == ("reasoning", 2)
    status, payload_raw = d.query_one(
        "SELECT status,payload_json FROM directive WHERE id=?", (result["directive_id"],))
    assert status == "rejected"
    assert "权威状态漂移" in json.loads(payload_raw)["rejection_reason"]


def _insert_raw_pending(daemon, *, summary_md, items_json, request_hash):
    """绕过 create_checked 构造旧版/损坏 pending 行，验证终态入口会重新执行完整闸。"""
    with daemon.transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO interaction_request(goal_id,goal_ver,stage,status,summary_md,items_json,"
            "request_hash) VALUES (1,1,'plan','pending',?,?,?)",
            (summary_md, items_json, request_hash))
        return cursor.lastrowid


# ============ outbox 幂等 ============
def test_outbox_idempotent_emit_and_deliver(env):
    ob = env["ob"]
    assert ob.emit("k1", "x", {"a": 1}) is True
    assert ob.emit("k1", "x", {"a": 1}) is False                  # 重 emit 不重排队
    ob.emit("k2", "x", {})
    conn = FakeConnector()
    assert ob.deliver_pending(conn) == ["k1", "k2"]
    assert ob.deliver_pending(conn) == []                          # 重投递不重发
    assert [e["event_key"] for e in conn.sent] == ["k1", "k2"]


def test_outbox_delivery_resumes_after_connector_failure(env):
    ob = env["ob"]
    for k in ("a", "b", "c"):
        ob.emit(k, "x", {})
    bad = FakeConnector(); bad.fail_after = 1                      # 发出 a 后故障
    with pytest.raises(RuntimeError):
        ob.deliver_pending(bad)
    good = FakeConnector()
    assert ob.deliver_pending(good) == ["b", "c"]                  # a 已标记不重发；b/c 续投


def test_outbox_torn_tail_tolerated_and_repaired(env, tmp_path):
    """内审 SHOULD 回归：append 崩溃撕裂尾行 → 不楔死（半行=未入队，重扫会补）；下次 emit 修复
    （截半行再追加，不粘行）；中段坏行仍 fail loud（非崩溃可造成，别的故障别吞）。"""
    from orchestrator.notify import Outbox
    ob = Outbox(str(tmp_path / "torn"))
    ob.emit("k1", "x", {})
    with ob.queue_path.open("a", encoding="utf-8") as f:
        f.write('{"event_key": "k2", "kind": "x", "pay')           # 模拟 kill-9 半行
    ob2 = Outbox(str(tmp_path / "torn"))                           # 新实例（缓存冷）
    assert ob2.emit("k3", "x", {}) is True                         # 不楔死；修复后追加
    assert ob2.deliver_pending(FakeConnector()) == ["k1", "k3"]    # k2 半行被丢（未 emit 语义）
    assert ob2.emit("k2", "x", {}) is True                         # 重扫补发路径畅通
    # 中段坏行 fail loud
    bad = Outbox(str(tmp_path / "mid"))
    bad.queue_path.write_text('not-json\n{"event_key": "z", "kind": "x", "payload": {}}\n', encoding="utf-8")
    with pytest.raises(ValueError):
        Outbox(str(tmp_path / "mid"))._queued_keys()


def test_outbox_unterminated_complete_json_not_lost(tmp_path):
    """外审 BLOCKER 回归：崩溃可留下"完整 JSON 但无尾换行"——committed 判据必须是换行终止：
    该段不入 _seen、emit 截修丢弃后**可重新入队**（旧实现会把它算入 _seen 又截掉 → 事件永久丢失）。"""
    from orchestrator.notify import Outbox
    ob = Outbox(str(tmp_path / "unterm"))
    ob.emit("k1", "x", {})
    with ob.queue_path.open("a", encoding="utf-8") as f:
        f.write('{"event_key": "k2", "kind": "x", "payload": {}}')   # 完整 JSON、无 \n
    ob2 = Outbox(str(tmp_path / "unterm"))
    assert ob2.emit("k3", "x", {}) is True                           # 触发截修
    assert ob2.emit("k2", "x", {}) is True                           # k2 未 committed → 可重入队（不丢）
    assert ob2.deliver_pending(FakeConnector()) == ["k1", "k3", "k2"]


def test_resolve_symlinked_item_dir_not_ingested(env, tmp_path):
    """外审 BLOCKER 回归：item 目录**本身**是 symlink（指向外部目录）→ 其内常规文件不得并入
    （逐文件 is_symlink 挡不住这条路）。"""
    d = env["d"]
    svc = FileRequestService(d, SCHEMAS, POLICY, str(tmp_path / "input"))
    rid = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(1))
    outside = tmp_path / "outside"; outside.mkdir(); (outside / "secret.txt").write_bytes(b"SECRET")
    up = tmp_path / "up" / str(rid); up.mkdir(parents=True)
    (up / "1").symlink_to(outside, target_is_directory=True)         # item 目录=symlink
    mid = InteractionIngest(d).inbound(connector="qq", raw_text="传了", idempotency_key="sym-1",
                                       goal_id=1, goal_ver=1)
    out = svc.resolve(request_id=rid, uploads_dir=str(up), resolved_message_id=mid)
    assert out["resolution"][0] == {"unavailable": "用户未提供该条目文件"}   # 不跟随、不并入
    assert not (tmp_path / "input" / "user_provided" / str(rid)).exists()


def test_resolve_rejects_symlink_in_upload_root_path(env, tmp_path):
    """uploads_dir 的任一父组件也必须是固定实体目录，不能只给最终组件 O_NOFOLLOW。"""
    d = env["d"]
    svc = FileRequestService(d, SCHEMAS, POLICY, str(tmp_path / "input"))
    rid = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(1))
    outside = tmp_path / "outside"
    (outside / "uploads" / "1").mkdir(parents=True)
    (outside / "uploads" / "1" / "secret.bin").write_bytes(b"SECRET")
    carrier = tmp_path / "carrier"
    carrier.mkdir()
    (carrier / "redirect").symlink_to(outside, target_is_directory=True)
    mid = InteractionIngest(d).inbound(
        connector="qq", raw_text="文件已上传", idempotency_key="upload-parent-symlink",
        goal_id=1, goal_ver=1)

    with pytest.raises(OSError):
        svc.resolve(
            request_id=rid, uploads_dir=str(carrier / "redirect" / "uploads"),
            resolved_message_id=mid)
    assert d.query_one("SELECT status FROM interaction_request WHERE id=?", (rid,)) == ("pending",)
    assert not (tmp_path / "input" / "user_provided" / str(rid)).exists()


def test_resolve_rejects_parent_component_before_abspath_can_hide_symlink(env, tmp_path):
    """不得先词法折叠 symlink/../；否则被跳过的 symlink 父组件从未经过 O_NOFOLLOW。"""
    d = env["d"]
    svc = FileRequestService(d, SCHEMAS, POLICY, str(tmp_path / "input"))
    rid = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(1))
    carrier = tmp_path / "carrier"
    (carrier / "uploads" / "1").mkdir(parents=True)
    (carrier / "uploads" / "1" / "data.bin").write_bytes(b"SAFE")
    (carrier / "redirect").symlink_to(tmp_path, target_is_directory=True)
    mid = InteractionIngest(d).inbound(
        connector="qq", raw_text="文件已上传", idempotency_key="upload-parent-component",
        goal_id=1, goal_ver=1)

    with pytest.raises(ValueError, match="不得含.*\\.\\."):
        svc.resolve(
            request_id=rid,
            uploads_dir=str(carrier / "redirect" / ".." / "uploads"),
            resolved_message_id=mid)
    assert d.query_one("SELECT status FROM interaction_request WHERE id=?", (rid,)) == ("pending",)
    assert not (tmp_path / "input" / "user_provided" / str(rid)).exists()


@pytest.mark.parametrize("replacement", ["item_symlink", "middle_symlink", "file_symlink", "file_inode"])
def test_resolve_enumerated_upload_replacement_never_ingests_external_secret(
        env, tmp_path, monkeypatch, replacement):
    """枚举已完成后替换 item/中间目录/文件：固定 fd 不得转而读外部 SECRET，且整次 resolve 回滚。"""
    import orchestrator.notify as N

    d = env["d"]
    svc = FileRequestService(d, SCHEMAS, POLICY, str(tmp_path / "input"))
    rid = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(1))
    up = tmp_path / "uploads"
    source = up / "1" / "middle" / "data.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"SAFE")
    outside = tmp_path / "outside"
    (outside / "middle").mkdir(parents=True)
    (outside / "middle" / "data.bin").write_bytes(b"SECRET")
    (outside / "data.bin").write_bytes(b"SECRET")
    secret = outside / "secret.bin"
    secret.write_bytes(b"SECRET")
    mid = InteractionIngest(d).inbound(
        connector="qq", raw_text="文件已上传", idempotency_key=f"upload-race-{replacement}",
        goal_id=1, goal_ver=1)

    original_enumerate = N._enumerate_upload_directory
    replaced = False

    def enumerate_then_replace(*args, **kwargs):
        nonlocal replaced
        result = original_enumerate(*args, **kwargs)
        if replaced or not kwargs["opened_files"]:
            return result
        replaced = True                         # 文件 fd 已固定；复制尚未开始
        if replacement == "item_symlink":
            (up / "1").rename(up / "1-original")
            (up / "1").symlink_to(outside, target_is_directory=True)
        elif replacement == "middle_symlink":
            source.parent.rename(up / "1" / "middle-original")
            source.parent.symlink_to(outside, target_is_directory=True)
        elif replacement == "file_symlink":
            source.rename(source.with_name("data-original.bin"))
            source.symlink_to(secret)
        else:
            os.replace(secret, source)           # 新常规 inode，路径名完全不变
        return result

    monkeypatch.setattr(N, "_enumerate_upload_directory", enumerate_then_replace)
    with pytest.raises(OSError, match="替换|改写"):
        svc.resolve(request_id=rid, uploads_dir=str(up), resolved_message_id=mid)

    assert replaced
    assert d.query_one(
        "SELECT status,resolution_json,resolved_message_id FROM interaction_request WHERE id=?", (rid,)) == (
            "pending", None, None)
    managed = tmp_path / "input" / "user_provided"
    assert not (managed / str(rid)).exists()
    assert not (managed / ".staging" / str(rid)).exists()


def test_resolve_rejects_in_place_source_mutation_during_copy(env, tmp_path, monkeypatch):
    """第一次身份复验后原 inode 被原地改写，复制后的第二次复验必须让整次 resolve 回滚。"""
    import orchestrator.notify as N

    d = env["d"]
    svc = FileRequestService(d, SCHEMAS, POLICY, str(tmp_path / "input"))
    rid = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(1))
    source = tmp_path / "uploads" / "1" / "data.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"SAFE")
    original_inode = source.stat().st_ino
    mid = InteractionIngest(d).inbound(
        connector="qq", raw_text="文件已上传", idempotency_key="upload-in-place-mutation",
        goal_id=1, goal_ver=1)
    original_verify = N._verify_opened_upload_file
    calls = 0

    def mutate_after_pre_copy_verify(opened):
        nonlocal calls
        original_verify(opened)
        calls += 1
        if calls == 1:
            source.write_bytes(b"EVIL-LONGER")       # truncate/write 保留 inode，但确定改变 size/ctime
            assert source.stat().st_ino == original_inode

    monkeypatch.setattr(N, "_verify_opened_upload_file", mutate_after_pre_copy_verify)
    with pytest.raises(OSError, match="替换|改写"):
        svc.resolve(request_id=rid, uploads_dir=str(tmp_path / "uploads"), resolved_message_id=mid)

    assert calls == 1                                  # 第二次 original_verify 在递增前即拒绝
    assert d.query_one("SELECT status FROM interaction_request WHERE id=?", (rid,)) == ("pending",)
    managed = tmp_path / "input" / "user_provided"
    assert not (managed / str(rid)).exists()
    assert not (managed / ".staging" / str(rid)).exists()


def test_resolve_rejects_hardlinked_upload_file(env, tmp_path):
    """上传文件必须是 nlink=1 的独占 inode；外部 hardlink 不得被当成稳定上传 capability。"""
    d = env["d"]
    svc = FileRequestService(d, SCHEMAS, POLICY, str(tmp_path / "input"))
    rid = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(1))
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"SECRET")
    item = tmp_path / "uploads" / "1"
    item.mkdir(parents=True)
    os.link(outside, item / "linked.bin")
    mid = InteractionIngest(d).inbound(
        connector="qq", raw_text="文件已上传", idempotency_key="upload-hardlink",
        goal_id=1, goal_ver=1)

    with pytest.raises(OSError, match="独占常规文件"):
        svc.resolve(request_id=rid, uploads_dir=str(tmp_path / "uploads"), resolved_message_id=mid)
    assert d.query_one("SELECT status FROM interaction_request WHERE id=?", (rid,))[0] == "pending"
    assert not (tmp_path / "input" / "user_provided" / str(rid)).exists()


@pytest.mark.parametrize("budget_kind", ["per_directory", "request_entries", "directories"])
def test_resolve_upload_tree_traversal_is_bounded(env, tmp_path, monkeypatch, budget_kind):
    """海量空目录/symlink/FIFO 也必须消耗 resolve 枚举预算，不能只按最终 regular file 数限流。"""
    import orchestrator.notify as N

    d = env["d"]
    item_count = 2 if budget_kind == "request_entries" else 1
    svc = FileRequestService(d, SCHEMAS, POLICY, str(tmp_path / "input"))
    rid = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(item_count))
    up = tmp_path / "uploads"
    for item_no in range(1, item_count + 1):
        item = up / str(item_no)
        item.mkdir(parents=True)
        if budget_kind == "directories":
            (item / "d1" / "d2").mkdir(parents=True)
        else:
            for index in range(3):
                (item / f"ignored-{index}").symlink_to(tmp_path / f"missing-{index}")

    if budget_kind == "per_directory":
        monkeypatch.setattr(N, "_MAX_UPLOAD_ENTRIES_PER_DIRECTORY", 2)
    elif budget_kind == "request_entries":
        monkeypatch.setattr(N, "_MAX_UPLOAD_ENTRIES_PER_REQUEST", 4)
    else:
        monkeypatch.setattr(N, "_MAX_UPLOAD_DIRECTORIES_PER_REQUEST", 2)
    mid = InteractionIngest(d).inbound(
        connector="qq", raw_text="文件已上传", idempotency_key=f"upload-tree-{budget_kind}",
        goal_id=1, goal_ver=1)

    with pytest.raises(ValueError, match="安全上限"):
        svc.resolve(request_id=rid, uploads_dir=str(up), resolved_message_id=mid)
    assert d.query_one("SELECT status FROM interaction_request WHERE id=?", (rid,)) == ("pending",)
    assert not (tmp_path / "input" / "user_provided" / str(rid)).exists()


def test_resolve_accepts_pinned_proc_upload_directory_capability(env, tmp_path):
    """console ingest 可把逐组件固定后的目录以 /proc/self/fd/N 交给 service，service 必须 dup 而非跟 symlink。"""
    d = env["d"]
    svc = FileRequestService(d, SCHEMAS, POLICY, str(tmp_path / "input"))
    rid = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(1))
    up = tmp_path / "uploads"
    item = up / "1"
    item.mkdir(parents=True)
    (item / "data.bin").write_bytes(b"PINNED")
    mid = InteractionIngest(d).inbound(
        connector="qq", raw_text="文件已上传", idempotency_key="upload-proc-fd",
        goal_id=1, goal_ver=1)

    upload_fd = os.open(up, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        out = svc.resolve(
            request_id=rid, uploads_dir=f"/proc/self/fd/{upload_fd}", resolved_message_id=mid)
    finally:
        os.close(upload_fd)
    accepted = Path(out["resolution"][0]["provided"][0]["path"])
    assert accepted.read_bytes() == b"PINNED"


def test_resolve_fd_walk_preserves_relative_path_asset_order(env, tmp_path):
    """fd 遍历不能把既有 Path 相对路径排序偷换成 scandir/纯字符串顺序，避免 asset ref 身份漂移。"""
    d = env["d"]
    svc = FileRequestService(d, SCHEMAS, POLICY, str(tmp_path / "input"))
    rid = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(1))
    item = tmp_path / "uploads" / "1"
    for relpath in ("a.z", "a/z", "A/x", "a0/x"):
        path = item / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relpath, encoding="utf-8")
    mid = InteractionIngest(d).inbound(
        connector="qq", raw_text="文件已上传", idempotency_key="upload-fd-order",
        goal_id=1, goal_ver=1)

    out = svc.resolve(
        request_id=rid, uploads_dir=str(tmp_path / "uploads"), resolved_message_id=mid)
    assert [asset["original_relpath"] for asset in out["resolution"][0]["provided"]] == [
        "A/x", "a/z", "a.z", "a0/x"]


def test_create_checked_idempotent_retry_wins_over_quota(env, tmp_path):
    """外审 SHOULD 回归：幂等先于 quota——同 (goal,request_hash) 重试在配额已满时仍返回既有单。"""
    d = env["d"]
    svc = FileRequestService(d, SCHEMAS, POLICY, str(tmp_path / "input"))
    rid = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(1))
    pmid = InteractionIngest(d).inbound(connector="qq", raw_text="铺底", idempotency_key="quota-seed",
                                        goal_id=1, goal_ver=1)
    with d.transaction() as conn:   # 铺满配额（连同已存在的 pending 共 ≥5）
        for i in range(5):
            conn.execute("INSERT INTO interaction_request(goal_id,goal_ver,stage,status,summary_md,items_json,"
                         "request_hash,resolution_json,resolved_at,resolved_message_id) VALUES "
                         "(1,1,'plan','resolved','s','[]',?,'[]',CURRENT_TIMESTAMP,?)", (f"q{i}", pmid))
    assert svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(1)) == rid  # 幂等放行
    two = _request(2)
    with pytest.raises(FileRequestReject, match="上限"):              # 新单仍被 quota 拒
        svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=two)


@pytest.mark.parametrize("terminal", ["resolved", "cancelled"])
def test_same_hash_after_terminal_is_rejected_with_actionable_receipt(env, tmp_path, terminal):
    """终态后无状态工人原样重提会确定性循环；须拒并要求消费 resolved/cancelled 回执。"""
    d = env["d"]
    svc = FileRequestService(d, SCHEMAS, POLICY, str(tmp_path / "input"))
    req = _request(1)
    first = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=req)
    mid = InteractionIngest(d).inbound(connector="console", raw_text="文件请求终态",
                                       idempotency_key=f"redo-{terminal}", goal_id=1, goal_ver=1)
    if terminal == "resolved":
        up = tmp_path / "uploads" / str(first); up.mkdir(parents=True)
        svc.resolve(request_id=first, uploads_dir=str(up), resolved_message_id=mid)
    else:
        svc.cancel(request_id=first, reason="本次取消", resolved_message_id=mid)

    assert d.query_one("SELECT status FROM interaction_request WHERE id=?", (first,))[0] == terminal
    with pytest.raises(FileRequestReject, match=f"{terminal}.*不得原样重提"):
        svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=req)
    assert d.query_one("SELECT COUNT(*) FROM interaction_request")[0] == 1
    changed = _request(1); changed["items"][0]["desc"] += "（条件已变化）"
    second = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=changed)
    assert second != first and d.query_one(
        "SELECT status FROM interaction_request WHERE id=?", (second,))[0] == "pending"


def test_second_pending_different_hash_business_reject(env, tmp_path):
    """外审 NIT 回归：同 goal 第二张不同 hash 的 pending → 业务拒因（不外泄 uq_ireq_one_pending DDL 错）。"""
    svc = FileRequestService(env["d"], SCHEMAS, POLICY, str(tmp_path / "input"))
    svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(1))
    with pytest.raises(FileRequestReject, match="pending"):
        svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(2))


def test_resolve_provenance_goal_mismatch_rejected(env, tmp_path):
    """外审 SHOULD 回归：终态 provenance 消息须与请求同 goal（未绑定 goal 也拒，fail closed）。"""
    d = env["d"]
    with d.transaction() as conn:
        conn.execute("INSERT INTO goal(id,version,text,predicate_json) VALUES (2,1,'g2','{}')")
    svc = FileRequestService(d, SCHEMAS, POLICY, str(tmp_path / "input"))
    rid = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(1))
    ing = InteractionIngest(d)
    other = ing.inbound(connector="qq", raw_text="别的 goal 的消息", idempotency_key="pv-1",
                        goal_id=2, goal_ver=1)
    unbound = ing.inbound(connector="qq", raw_text="未绑定 goal", idempotency_key="pv-2")
    up = tmp_path / "pv"; (up / "1").mkdir(parents=True); (up / "1" / "f").write_bytes(b"x")
    with pytest.raises(ValueError, match="goal"):
        svc.resolve(request_id=rid, uploads_dir=str(up), resolved_message_id=other)
    with pytest.raises(ValueError, match="goal"):
        svc.cancel(request_id=rid, reason="r", resolved_message_id=unbound)
    assert env["d"].query_one("SELECT status FROM interaction_request WHERE id=?", (rid,))[0] == "pending"


@pytest.mark.parametrize("operation", ["resolve", "cancel"])
@pytest.mark.parametrize(
    "corruption",
    ["bad-json", "not-array", "empty-items", "too-many-items", "schema-item",
     "schema-summary", "policy-limit", "hash-mismatch"],
)
def test_terminal_operations_validate_full_pending_request_before_cleanup(
        env, tmp_path, operation, corruption):
    """损坏/旧版 pending 行不得借 resolve 或 cancel 绕过创建闸，且诊断目录须原样保留。"""
    d = env["d"]
    summary_md = _request()["summary_md"]
    items = _items(1)
    policy = POLICY
    if corruption == "not-array":
        items = {"unexpected": "object"}
    elif corruption == "empty-items":
        items = []
    elif corruption == "too-many-items":
        items = _items(11)
    elif corruption == "schema-item":
        del items[0]["failure_reason"]
    elif corruption == "schema-summary":
        summary_md = ""
    elif corruption == "policy-limit":
        items = _items(2)
        policy = {
            **POLICY,
            "interaction_request": {
                **POLICY["interaction_request"], "max_items_per_request": 1,
            },
        }

    items_json = json.dumps(items, ensure_ascii=False, sort_keys=True)
    request_hash = hashlib.sha256(items_json.encode()).hexdigest()
    if corruption == "bad-json":
        items_json = "{not-json"
    elif corruption == "hash-mismatch":
        request_hash = "wrong-hash"
    rid = _insert_raw_pending(
        d, summary_md=summary_md, items_json=items_json, request_hash=request_hash)
    mid = InteractionIngest(d).inbound(
        connector="qq", raw_text="尝试迁终态",
        idempotency_key=f"pending-corrupt-{operation}-{corruption}", goal_id=1, goal_ver=1)
    managed = tmp_path / "input" / "user_provided"
    stage = managed / ".staging" / str(rid)
    final = managed / str(rid)
    stage.mkdir(parents=True); final.mkdir(parents=True)
    (stage / "keep").write_bytes(b"stage-sentinel")
    (final / "keep").write_bytes(b"final-sentinel")
    svc = FileRequestService(d, SCHEMAS, policy, str(tmp_path / "input"))

    with pytest.raises(ValueError, match="pending request 损坏"):
        if operation == "resolve":
            svc.resolve(request_id=rid, uploads_dir=str(tmp_path / "uploads"), resolved_message_id=mid)
        else:
            svc.cancel(request_id=rid, reason="取消", resolved_message_id=mid)

    assert d.query_one(
        "SELECT status,resolution_json,resolved_message_id FROM interaction_request WHERE id=?", (rid,)) == (
            "pending", None, None)
    assert (stage / "keep").read_bytes() == b"stage-sentinel"
    assert (final / "keep").read_bytes() == b"final-sentinel"


@pytest.mark.parametrize("operation,terminal", [("resolve", "resolved"), ("cancel", "cancelled")])
def test_terminal_operations_accept_valid_max_request_items(env, tmp_path, operation, terminal):
    """完整重校验的硬边界是闭区间：合法 10 items 仍可正常迁入两种终态。"""
    d = env["d"]
    svc = FileRequestService(d, SCHEMAS, POLICY, str(tmp_path / "input"))
    rid = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(10))
    mid = InteractionIngest(d).inbound(
        connector="qq", raw_text="终态确认", idempotency_key=f"max-items-{operation}",
        goal_id=1, goal_ver=1)

    if operation == "resolve":
        out = svc.resolve(
            request_id=rid, uploads_dir=str(tmp_path / "uploads"), resolved_message_id=mid)
        assert len(out["resolution"]) == 10
        assert all(item == {"unavailable": "用户未提供该条目文件"}
                   for item in out["resolution"])
    else:
        svc.cancel(request_id=rid, reason="不提供", resolved_message_id=mid)

    assert d.query_one("SELECT status FROM interaction_request WHERE id=?", (rid,))[0] == terminal


def test_due_timings_matrix():
    """内审 SHOULD 回归：reasoning_start 只在下一格将进 reasoning 时到期——attack 轮以 cycle.status
    为"最后已提交阶段"游标（attack_stages.advance_stage）：created/idea/plan 均不消费，'bundle' 才
    （下一格=reasoning）；reasoning-only 轮每格即 reasoning；开轮前（None）只 immediate+stage_boundary。"""
    from types import SimpleNamespace as NS
    from orchestrator.notify import _due_timings
    assert _due_timings(None) == ["immediate", "stage_boundary"]
    for st in ("created", "idea", "plan", "reasoning"):
        assert "reasoning_start" not in _due_timings(NS(route="attack", status=st)), st
    assert "reasoning_start" in _due_timings(NS(route="attack", status="bundle"))
    for route in ("bootstrap", "decompose", "goal_amend"):
        assert "reasoning_start" in _due_timings(NS(route=route, status="created"))


# ============ directive 逐态推送（§7.1 M5 矩阵断言）============
def _keys(ob):
    return set(ob._queued_keys())


def test_directive_hard_full_lifecycle_events(env):
    d, c, ob, dn = env["d"], env["c"], env["ob"], env["dn"]
    r = c.handle_inbound(connector="qq", raw_text="暂停", idempotency_key="n-1", goal_id=1, goal_ver=1)
    did = r["directive_id"]
    dn.scan()
    assert _keys(ob) == {f"directive:{did}:received", f"directive:{did}:classified",
                         f"directive:{did}:pending_confirmation"}          # 硬未确认：三态，无 pending_effect
    ev = [json.loads(l) for l in (ob.queue_path.read_text().splitlines())]
    pc = next(e for e in ev if e["kind"] == "directive_pending_confirmation")
    assert pc["payload"]["polished"].startswith("[pause]")                  # 确认事件展示润色稿
    c.confirm_directive(directive_id=did,
                        confirm_message_id=_action_message(d, c, r, "confirm"))
    dn.scan()
    assert f"directive:{did}:pending_effect" in _keys(ob)                   # 确认后 → 就绪态
    c.consume_directive(directive_id=did, cycle_id="c1")
    new = dn.scan()
    assert f"directive:{did}:applied" in new
    applied = [json.loads(l) for l in ob.queue_path.read_text().splitlines()
               if json.loads(l)["kind"] == "directive_applied"][0]
    assert applied["payload"]["consumed_cycle"] == "c1"                     # applied 带消费轮+效果摘要
    assert applied["payload"]["effect"]["kind"] == "pause"
    dn.scan()
    assert dn.scan() == []                                                  # 重扫幂等：无新事件


def test_directive_rejected_and_superseded_events(env):
    d, c, ob, dn = env["d"], env["c"], env["ob"], env["dn"]
    # 软指令系统不从 → rejected 附理由（理由在 decline 决策）
    r1 = c.handle_inbound(connector="qq", raw_text="注入问题：试试量子计算", idempotency_key="n-r",
                          goal_id=1, goal_ver=1)
    c.reject_directive(directive_id=r1["directive_id"], reason="与目标谓词无关", by_decision=True, cycle_id="c1")
    # pause 被 resume 覆盖 → superseded
    r2 = c.handle_inbound(connector="qq", raw_text="暂停", idempotency_key="n-p", goal_id=1, goal_ver=1)
    r3 = c.handle_inbound(connector="qq", raw_text="继续", idempotency_key="n-c", goal_id=1, goal_ver=1)
    c.confirm_directive(directive_id=r3["directive_id"],
                        confirm_message_id=_action_message(d, c, r3, "confirm"))
    c.consume_directive(directive_id=r3["directive_id"], cycle_id="c1")
    dn.scan()
    ks = _keys(ob)
    assert f"directive:{r1['directive_id']}:rejected" in ks
    rej = [json.loads(l) for l in ob.queue_path.read_text().splitlines()
           if json.loads(l)["event_key"] == f"directive:{r1['directive_id']}:rejected"][0]
    assert rej["payload"]["reason"] == "与目标谓词无关"                     # rejected 附理由
    assert f"directive:{r2['directive_id']}:superseded" in ks


# ============ 文件请求：创建负例 + schema 拒 ============
def test_create_checked_schema_rejects_missing_attempted_paths(env, tmp_path):
    svc = FileRequestService(env["d"], SCHEMAS, POLICY, str(tmp_path / "input"))
    bad = _request(1)
    del bad["items"][0]["attempted_paths"]
    with pytest.raises(FileRequestReject, match="schema"):
        svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=bad)


def test_create_checked_policy_negatives(env, tmp_path):
    d = env["d"]
    svc = FileRequestService(d, SCHEMAS, POLICY, str(tmp_path / "input"))
    with pytest.raises(FileRequestReject, match="schema 拒|超上限"):
        svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(11))   # >max_items=10
    off = json.loads(json.dumps(POLICY)); off["interaction_request"]["enabled"] = False
    with pytest.raises(FileRequestReject, match="未启用"):
        FileRequestService(d, SCHEMAS, off, str(tmp_path / "i2")).create_checked(
            goal_id=1, goal_ver=1, stage="plan", request=_request(1))
    # 同 goal (pending+resolved) 5 条后第 6 条拒（DDL 一 goal 一 pending → 5 条 resolved 铺底；
    # 终态 CHECK 要求 resolved_message_id 非空 → 先落一条入站消息作 provenance）
    pmid = InteractionIngest(d).inbound(connector="qq", raw_text="铺底", idempotency_key="neg-seed",
                                        goal_id=1, goal_ver=1)
    with d.transaction() as conn:
        for i in range(5):
            conn.execute("INSERT INTO interaction_request(goal_id,goal_ver,stage,status,summary_md,items_json,"
                         "request_hash,resolution_json,resolved_at,resolved_message_id) VALUES "
                         "(1,1,'plan','resolved','s','[]',?,'[]',CURRENT_TIMESTAMP,?)", (f"h{i}", pmid))
    with pytest.raises(FileRequestReject, match="上限"):
        svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(1))


def test_cancelled_requests_count_toward_goal_wide_receipt_quota(env, tmp_path):
    """cancelled 也永久进入 goal-wide 回执；不能用不同 hash 无限创建/取消撑爆 prompt。"""
    d = env["d"]
    svc = FileRequestService(d, SCHEMAS, POLICY, str(tmp_path / "input"))
    mid = InteractionIngest(d).inbound(connector="qq", raw_text="取消铺底",
                                       idempotency_key="cancel-quota-seed", goal_id=1, goal_ver=1)
    with d.transaction() as conn:
        for i in range(POLICY["interaction_request"]["max_requests_per_goal"]):
            conn.execute(
                "INSERT INTO interaction_request(goal_id,goal_ver,stage,status,summary_md,items_json,"
                "request_hash,resolution_json,resolved_at,resolved_message_id) VALUES "
                "(1,1,'plan','cancelled','s','[]',?,?,CURRENT_TIMESTAMP,?)",
                (f"cancel-{i}", json.dumps({"cancelled": True, "reason": f"r{i}"}), mid))
    with pytest.raises(FileRequestReject, match="含 cancelled"):
        svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(1))


# ============ 文件请求：resolve 全流水（uploads→hash→并入→终态）============
def test_resolve_pipeline_hash_and_ingest(env, tmp_path):
    import hashlib
    d = env["d"]
    svc = FileRequestService(d, SCHEMAS, POLICY, str(tmp_path / "input"))
    rid = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(2))
    up = tmp_path / "uploads" / str(rid)
    (up / "1").mkdir(parents=True); (up / "1" / "data.bin").write_bytes(b"DATA-1")
    # 条目 2 用户未提供（目录缺失）→ unavailable
    ing = InteractionIngest(d)
    mid = ing.inbound(connector="qq", raw_text="文件已上传", idempotency_key="up-1", goal_id=1, goal_ver=1)
    out = svc.resolve(request_id=rid, uploads_dir=str(up), resolved_message_id=mid)
    prov = out["resolution"][0]["provided"][0]
    assert prov["hash"] == hashlib.sha256(b"DATA-1").hexdigest()            # hash 入账
    assert prov["size_bytes"] == 6 and prov["ref"].startswith(f"user-file-request:r{rid}:")
    dest = Path(prov["path"])
    assert dest.exists() and dest.read_bytes() == b"DATA-1"                 # 并入 input/user_provided/<rid>/
    assert dest.stat().st_mode & 0o222 == 0                                  # 发布树默认只读，防意外改写
    assert str(tmp_path / "input" / "user_provided" / str(rid)) in str(dest)
    assert out["resolution"][1] == {"unavailable": "用户未提供该条目文件"}
    row = d.query_one("SELECT status, resolved_message_id FROM interaction_request WHERE id=?", (rid,))
    assert row == ("resolved", mid)                                         # 终态 + 入站 provenance
    with pytest.raises(ValueError, match="非 pending"):                     # 一次性迁移：二次 resolve 拒
        svc.resolve(request_id=rid, uploads_dir=str(up), resolved_message_id=mid)


def test_resolve_failure_leaves_no_partial_or_stale_final(env, tmp_path, monkeypatch):
    """第二文件失败时 DB 保持 pending、staging/final 都清空；重试源变化后 manifest 与目录严格一致。"""
    import orchestrator.notify as N
    d = env["d"]
    svc = FileRequestService(d, SCHEMAS, POLICY, str(tmp_path / "input"))
    rid = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(1))
    up = tmp_path / "uploads"; (up / "1").mkdir(parents=True)
    (up / "1" / "a.bin").write_bytes(b"A")
    (up / "1" / "b.bin").write_bytes(b"B")
    final = tmp_path / "input" / "user_provided" / str(rid)
    (final / "1").mkdir(parents=True); (final / "1" / "stale.bin").write_bytes(b"STALE")
    mid = InteractionIngest(d).inbound(connector="qq", raw_text="传了", idempotency_key="atomic-1",
                                       goal_id=1, goal_ver=1)
    original = N._copy_hash_regular
    calls = {"n": 0}

    def fail_second(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("injected copy failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(N, "_copy_hash_regular", fail_second)
    with pytest.raises(OSError, match="injected"):
        svc.resolve(request_id=rid, uploads_dir=str(up), resolved_message_id=mid)
    assert d.query_one("SELECT status FROM interaction_request WHERE id=?", (rid,))[0] == "pending"
    assert not final.exists()
    assert not (tmp_path / "input" / "user_provided" / ".staging" / str(rid)).exists()

    (up / "1" / "a.bin").unlink()                               # 重试只剩 b；旧 a/stale 均不得复活
    out = svc.resolve(request_id=rid, uploads_dir=str(up), resolved_message_id=mid)
    assert [Path(x["path"]).name for x in out["resolution"][0]["provided"]] == ["asset-1"]
    assert sorted(p.name for p in (final / "1").iterdir()) == ["asset-1"]


def test_resolve_rollback_cleanup_failure_preserves_and_notes_primary(
        env, tmp_path, monkeypatch, caplog):
    """rollback 自身失败不能覆盖真正的 copy/publish 异常；cleanup 详情挂到原异常 notes。"""
    import orchestrator.notify as N

    d = env["d"]
    svc = FileRequestService(d, SCHEMAS, POLICY, str(tmp_path / "input"))
    rid = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(1))
    up = tmp_path / "uploads"; (up / "1").mkdir(parents=True); (up / "1" / "x").write_bytes(b"x")
    mid = InteractionIngest(d).inbound(
        connector="qq", raw_text="传了", idempotency_key="rollback-primary", goal_id=1, goal_ver=1)
    primary = RuntimeError("primary copy failure")
    original_cleanup = N._remove_attempt_durable
    cleanup_calls = 0

    def fail_copy(*_args, **_kwargs):
        raise primary

    def fail_rollback_only(*args):
        nonlocal cleanup_calls
        cleanup_calls += 1
        if cleanup_calls == 1:                 # attempt 开始前的正常清理仍成功
            return original_cleanup(*args)
        raise OSError("injected rollback cleanup failure")

    monkeypatch.setattr(N, "_copy_hash_regular", fail_copy)
    monkeypatch.setattr(N, "_remove_attempt_durable", fail_rollback_only)
    with caplog.at_level("ERROR", logger="orchestrator.notify"):
        with pytest.raises(RuntimeError, match="primary copy failure") as caught:
            svc.resolve(request_id=rid, uploads_dir=str(up), resolved_message_id=mid)

    assert caught.value is primary
    assert any("rollback cleanup" in note and "injected rollback cleanup failure" in note
               for note in getattr(primary, "__notes__", ()))
    assert "rollback cleanup 失败；保留原始异常 RuntimeError" in caplog.text
    assert cleanup_calls == 2
    assert d.query_one("SELECT status FROM interaction_request WHERE id=?", (rid,))[0] == "pending"


def test_resolve_db_primary_survives_rollback_cleanup_failure(tmp_path, monkeypatch, caplog):
    """DB/COMMIT 主异常的 reconciliation cleanup 失败时，同样只记 notes/log、不换异常。"""
    from contextlib import contextmanager

    import orchestrator.notify as N

    d = _file_daemon(tmp_path)
    svc = FileRequestService(d, SCHEMAS, POLICY, str(tmp_path / "input"))
    rid = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(1))
    up = tmp_path / "uploads"; (up / "1").mkdir(parents=True); (up / "1" / "x").write_bytes(b"x")
    mid = InteractionIngest(d).inbound(
        connector="qq", raw_text="传了", idempotency_key="rollback-db-primary", goal_id=1, goal_ver=1)
    primary = sqlite3.OperationalError("primary transaction failure")
    original_transaction = d.transaction
    original_cleanup = N._remove_attempt_durable
    cleanup_calls = 0

    @contextmanager
    def fail_after_update():
        with original_transaction() as conn:
            yield conn
            raise primary

    def fail_reconciliation_cleanup(*args):
        nonlocal cleanup_calls
        cleanup_calls += 1
        if cleanup_calls == 1:                 # attempt 开始前清理
            return original_cleanup(*args)
        raise OSError("injected DB rollback cleanup failure")

    monkeypatch.setattr(d, "transaction", fail_after_update)
    monkeypatch.setattr(N, "_remove_attempt_durable", fail_reconciliation_cleanup)
    with caplog.at_level("ERROR", logger="orchestrator.notify"):
        with pytest.raises(sqlite3.OperationalError, match="primary transaction failure") as caught:
            svc.resolve(request_id=rid, uploads_dir=str(up), resolved_message_id=mid)

    assert caught.value is primary
    assert any("rollback cleanup" in note and "injected DB rollback cleanup failure" in note
               for note in getattr(primary, "__notes__", ()))
    assert "rollback cleanup 失败；保留原始异常 OperationalError" in caplog.text
    assert cleanup_calls == 2
    assert d.query_one("SELECT status FROM interaction_request WHERE id=?", (rid,))[0] == "pending"


def test_resolve_rejects_destination_symlink_and_byte_quota(env, tmp_path):
    d = env["d"]
    svc = FileRequestService(d, SCHEMAS, POLICY, str(tmp_path / "input"))
    rid = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(1))
    up = tmp_path / "uploads"; (up / "1").mkdir(parents=True); (up / "1" / "x").write_bytes(b"1234")
    outside = tmp_path / "outside"; outside.mkdir()
    managed = tmp_path / "input" / "user_provided"; managed.mkdir(parents=True)
    (managed / str(rid)).symlink_to(outside, target_is_directory=True)
    mid = InteractionIngest(d).inbound(connector="qq", raw_text="传了", idempotency_key="safe-dest",
                                       goal_id=1, goal_ver=1)
    with pytest.raises(OSError, match="symlink"):
        svc.resolve(request_id=rid, uploads_dir=str(up), resolved_message_id=mid)
    assert list(outside.iterdir()) == [] and d.query_one(
        "SELECT status FROM interaction_request WHERE id=?", (rid,))[0] == "pending"
    (managed / str(rid)).unlink()
    svc.max_managed_bytes = 3
    with pytest.raises(ValueError, match="disk_quota"):
        svc.resolve(request_id=rid, uploads_dir=str(up), resolved_message_id=mid)
    assert not (managed / str(rid)).exists()


def test_resolve_rejects_symlinked_input_root(env, tmp_path):
    """input_root 本身若是 symlink，mkdir(exist_ok=True) 会默许并把整棵托管树写到 work 外。"""
    outside = tmp_path / "outside-input"; outside.mkdir()
    linked_input = tmp_path / "input"; linked_input.symlink_to(outside, target_is_directory=True)
    svc = FileRequestService(env["d"], SCHEMAS, POLICY, str(linked_input))
    rid = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(1))
    up = tmp_path / "uploads"; (up / "1").mkdir(parents=True); (up / "1" / "x").write_bytes(b"x")
    mid = InteractionIngest(env["d"]).inbound(
        connector="qq", raw_text="传了", idempotency_key="symlink-input-root", goal_id=1, goal_ver=1)

    with pytest.raises(OSError, match="input_root.*symlink"):
        svc.resolve(request_id=rid, uploads_dir=str(up), resolved_message_id=mid)

    assert not (outside / "user_provided").exists()
    assert env["d"].query_one(
        "SELECT status FROM interaction_request WHERE id=?", (rid,))[0] == "pending"


def test_resolve_db_failure_removes_published_tree_and_keeps_pending(tmp_path, monkeypatch):
    """发布完成后 UPDATE/事务失败时，pending DB 状态不得配上一棵看似可用的 final 树。"""
    from contextlib import contextmanager

    d = _file_daemon(tmp_path)
    svc = FileRequestService(d, SCHEMAS, POLICY, str(tmp_path / "input"))
    rid = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(1))
    up = tmp_path / "uploads"; (up / "1").mkdir(parents=True); (up / "1" / "x").write_bytes(b"x")
    mid = InteractionIngest(d).inbound(
        connector="qq", raw_text="传了", idempotency_key="resolve-db-fail", goal_id=1, goal_ver=1)
    original_transaction = d.transaction

    @contextmanager
    def fail_after_update():
        # 让 UPDATE 真正执行、再在事务退出前失败；WriteDaemon 应回滚，而 resolve 必须撤回已发布树。
        with original_transaction() as conn:
            yield conn
            raise sqlite3.OperationalError("injected transaction failure")

    monkeypatch.setattr(d, "transaction", fail_after_update)
    with pytest.raises(sqlite3.OperationalError, match="injected transaction failure"):
        svc.resolve(request_id=rid, uploads_dir=str(up), resolved_message_id=mid)

    final = tmp_path / "input" / "user_provided" / str(rid)
    staging = tmp_path / "input" / "user_provided" / ".staging" / str(rid)
    assert not final.exists() and not staging.exists()
    assert d.query_one("SELECT status, resolution_json FROM interaction_request WHERE id=?", (rid,)) == (
        "pending", None)


def test_resolve_commit_ack_failure_preserves_exact_resolved_tree(tmp_path, monkeypatch):
    """若 COMMIT 已生效但随后才报错，精确匹配本次回执即视为成功，不能删掉权威 resolved 的资产。"""
    from contextlib import contextmanager

    d = _file_daemon(tmp_path)
    svc = FileRequestService(d, SCHEMAS, POLICY, str(tmp_path / "input"))
    rid = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(1))
    up = tmp_path / "uploads"; (up / "1").mkdir(parents=True); (up / "1" / "x").write_bytes(b"x")
    mid = InteractionIngest(d).inbound(
        connector="qq", raw_text="传了", idempotency_key="resolve-commit-ack", goal_id=1, goal_ver=1)

    @contextmanager
    def commit_then_fail_ack():
        d.conn.execute("BEGIN IMMEDIATE")
        try:
            yield d.conn
        except BaseException:
            d.conn.execute("ROLLBACK")
            raise
        else:
            d.conn.execute("COMMIT")
            raise sqlite3.OperationalError("commit acknowledgement lost")

    monkeypatch.setattr(d, "transaction", commit_then_fail_ack)
    out = svc.resolve(request_id=rid, uploads_dir=str(up), resolved_message_id=mid)

    final = tmp_path / "input" / "user_provided" / str(rid)
    assert final.is_dir() and Path(out["resolution"][0]["provided"][0]["path"]).read_bytes() == b"x"
    row = d.query_one(
        "SELECT status, resolution_json, resolved_message_id FROM interaction_request WHERE id=?", (rid,))
    assert row == ("resolved", json.dumps(out["resolution"], ensure_ascii=False), mid)


def test_resolve_db_failure_with_unreadable_state_quarantines_published_tree(tmp_path, monkeypatch):
    """事务异常后若权威回读也失败，不能猜测 commit 未生效并删树；保留 quarantine、抛原异常。"""
    from contextlib import contextmanager

    import orchestrator.notify as N

    d = _file_daemon(tmp_path)
    svc = FileRequestService(d, SCHEMAS, POLICY, str(tmp_path / "input"))
    rid = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(1))
    up = tmp_path / "uploads"; (up / "1").mkdir(parents=True); (up / "1" / "x").write_bytes(b"x")
    mid = InteractionIngest(d).inbound(
        connector="qq", raw_text="传了", idempotency_key="resolve-db-unknown", goal_id=1, goal_ver=1)
    original_transaction = d.transaction

    @contextmanager
    def fail_after_update():
        with original_transaction() as conn:
            yield conn
            raise sqlite3.OperationalError("original transaction failure")

    def fail_authoritative_reread(_daemon, _request_id):
        raise sqlite3.OperationalError("database unavailable during reconciliation")

    monkeypatch.setattr(d, "transaction", fail_after_update)
    monkeypatch.setattr(N, "_read_committed_resolution_state", fail_authoritative_reread)
    with pytest.raises(sqlite3.OperationalError, match="original transaction failure"):
        svc.resolve(request_id=rid, uploads_dir=str(up), resolved_message_id=mid)

    final = tmp_path / "input" / "user_provided" / str(rid)
    assert final.is_dir() and (final / "1" / "asset-1").read_bytes() == b"x"
    assert d.conn.execute("SELECT status FROM interaction_request WHERE id=?", (rid,)).fetchone()[0] == "pending"


def test_resolve_commit_error_does_not_trust_writer_uncommitted_view(tmp_path, monkeypatch):
    """writer 自己可见的 exact UPDATE 不是已提交真相；独立 ro 仍见 pending 时必须撤回 final。"""
    from contextlib import contextmanager

    d = _file_daemon(tmp_path)
    svc = FileRequestService(d, SCHEMAS, POLICY, str(tmp_path / "input"))
    rid = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(1))
    up = tmp_path / "uploads"; (up / "1").mkdir(parents=True); (up / "1" / "x").write_bytes(b"x")
    mid = InteractionIngest(d).inbound(
        connector="qq", raw_text="传了", idempotency_key="resolve-uncommitted", goal_id=1, goal_ver=1)

    @contextmanager
    def leave_update_uncommitted():
        d.conn.execute("BEGIN IMMEDIATE")
        yield d.conn
        raise sqlite3.OperationalError("commit failed before durable visibility")

    monkeypatch.setattr(d, "transaction", leave_update_uncommitted)
    with pytest.raises(sqlite3.OperationalError, match="commit failed"):
        svc.resolve(request_id=rid, uploads_dir=str(up), resolved_message_id=mid)

    # 同 writer 看见自己的未提交 exact row；新连接仍见 pending，因此 final 已撤回。
    assert d.conn.execute("SELECT status FROM interaction_request WHERE id=?", (rid,)).fetchone()[0] == "resolved"
    observer = sqlite3.connect(str(tmp_path / "research.sqlite"))
    try:
        assert observer.execute(
            "SELECT status FROM interaction_request WHERE id=?", (rid,)).fetchone()[0] == "pending"
    finally:
        observer.close()
    assert not (tmp_path / "input" / "user_provided" / str(rid)).exists()
    d.conn.execute("ROLLBACK")


def test_stale_cancel_precheck_cannot_delete_newly_resolved_assets(tmp_path):
    """cancel 初检 pending 后若 resolve 抢先完成，锁内重检须在删除 final 之前拒绝旧 cancel。"""
    db_path = tmp_path / "research.sqlite"
    d = _file_daemon(tmp_path)
    resolver = FileRequestService(d, SCHEMAS, POLICY, str(tmp_path / "input"))
    rid = resolver.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(1))
    up = tmp_path / "uploads"; (up / "1").mkdir(parents=True); (up / "1" / "x").write_bytes(b"x")
    ing = InteractionIngest(d)
    resolve_mid = ing.inbound(
        connector="qq", raw_text="提供", idempotency_key="race-resolve", goal_id=1, goal_ver=1)
    cancel_mid = ing.inbound(
        connector="qq", raw_text="取消", idempotency_key="race-cancel", goal_id=1, goal_ver=1)
    cancel_prechecked = threading.Event()
    continue_cancel = threading.Event()
    cancel_errors = []

    def cancel_worker():
        daemon = WriteDaemon(db.connect(str(db_path)))
        service = FileRequestService(daemon, SCHEMAS, POLICY, str(tmp_path / "input"))
        original_check = service._check_provenance
        calls = 0

        def pause_after_first_check(*args, **kwargs):
            nonlocal calls
            result = original_check(*args, **kwargs)
            calls += 1
            if calls == 1:
                cancel_prechecked.set()
                if not continue_cancel.wait(10):
                    raise TimeoutError("test did not release stale cancel")
            return result

        service._check_provenance = pause_after_first_check
        try:
            service.cancel(request_id=rid, reason="过时取消", resolved_message_id=cancel_mid)
        except BaseException as exc:
            cancel_errors.append(exc)
        finally:
            daemon.conn.close()

    worker = threading.Thread(target=cancel_worker, daemon=True)
    worker.start()
    assert cancel_prechecked.wait(5)
    resolved = resolver.resolve(request_id=rid, uploads_dir=str(up), resolved_message_id=resolve_mid)
    final = Path(resolved["resolution"][0]["provided"][0]["path"])
    continue_cancel.set()
    worker.join(10)

    assert not worker.is_alive()
    assert len(cancel_errors) == 1 and isinstance(cancel_errors[0], ValueError)
    assert "非 pending" in str(cancel_errors[0])
    assert d.query_one("SELECT status FROM interaction_request WHERE id=?", (rid,))[0] == "resolved"
    assert final.read_bytes() == b"x"                    # stale cancel 未先删 final 再发现终态


def test_file_request_operation_claim_blocks_across_process_and_recovers_after_crash(tmp_path):
    """root-global flock 必须跨进程串行；持锁进程被杀后内核释锁，pending resolve 可继续完成。"""
    import orchestrator.notify as N

    db_path = tmp_path / "research.sqlite"
    d = _file_daemon(tmp_path)
    svc = FileRequestService(d, SCHEMAS, POLICY, str(tmp_path / "input"))
    rid = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(1))
    up = tmp_path / "uploads"; (up / "1").mkdir(parents=True); (up / "1" / "x").write_bytes(b"x")
    mid = InteractionIngest(d).inbound(
        connector="qq", raw_text="提供", idempotency_key="claim-crash", goal_id=1, goal_ver=1)
    managed_root = svc._managed_paths(rid)[0]
    ctx = multiprocessing.get_context("fork")
    claim_ready = ctx.Event()
    hold_forever = ctx.Event()

    def crashed_holder():
        with N._claim_file_request_operation(managed_root):
            claim_ready.set()
            hold_forever.wait(60)

    holder = ctx.Process(target=crashed_holder, daemon=True)
    holder.start()
    resolve_started = threading.Event()
    resolve_done = threading.Event()
    result = []
    errors = []

    def resolve_worker():
        daemon = WriteDaemon(db.connect(str(db_path)))
        service = FileRequestService(daemon, SCHEMAS, POLICY, str(tmp_path / "input"))
        resolve_started.set()
        try:
            result.append(service.resolve(
                request_id=rid, uploads_dir=str(up), resolved_message_id=mid))
        except BaseException as exc:
            errors.append(exc)
        finally:
            daemon.conn.close()
            resolve_done.set()

    worker = threading.Thread(target=resolve_worker, daemon=True)
    try:
        assert claim_ready.wait(5)
        worker.start()
        assert resolve_started.wait(5)
        assert not resolve_done.wait(0.3)                 # 另一进程持 claim 时不得触碰/完成 request
        holder.terminate()                                # 模拟 kill -9/崩溃：无 finally 主动 unlock
        holder.join(5)
        assert not holder.is_alive()
        assert resolve_done.wait(10)                      # fd 被内核关闭后自动恢复推进
        worker.join(1)
    finally:
        if holder.is_alive():
            holder.terminate(); holder.join(5)

    assert errors == [] and len(result) == 1
    assert d.query_one("SELECT status FROM interaction_request WHERE id=?", (rid,))[0] == "resolved"
    assert Path(result[0]["resolution"][0]["provided"][0]["path"]).read_bytes() == b"x"


def test_resolution_preview_is_bounded_strict_text_from_copy_fd(env, tmp_path, monkeypatch):
    """preview 只来自复制 fd：单资产 8KiB；全文件含 NUL/非法 UTF-8 时即使前缀像文本也不外显。"""
    d = env["d"]
    svc = FileRequestService(d, SCHEMAS, POLICY, str(tmp_path / "input"))
    rid = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(3))
    up = tmp_path / "uploads"
    payloads = (b"A" * 9000, b"B" * 9000 + b"\x00", b"C" * 9000 + b"\xff")
    sources = set()
    for item_no, payload in enumerate(payloads, start=1):
        source = up / str(item_no) / "source.bin"
        source.parent.mkdir(parents=True); source.write_bytes(payload); sources.add(source)
    mid = InteractionIngest(d).inbound(
        connector="qq", raw_text="传了", idempotency_key="preview-strict", goal_id=1, goal_ver=1)
    original_path_open = Path.open

    def reject_source_path_reopen(path, *args, **kwargs):
        if path in sources:
            raise AssertionError("preview 不得重新按路径打开上传源")
        return original_path_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", reject_source_path_reopen)
    out = svc.resolve(request_id=rid, uploads_dir=str(up), resolved_message_id=mid)
    assets = [item["provided"][0] for item in out["resolution"]]

    assert assets[0]["preview"] == "A" * (8 * 1024)
    assert assets[0]["preview_truncated"] is True
    assert "preview" not in assets[1] and "preview" not in assets[2]
    stored = json.loads(d.query_one(
        "SELECT resolution_json FROM interaction_request WHERE id=?", (rid,))[0])
    assert stored == out["resolution"]


def test_resolution_preview_has_request_wide_32k_budget(env, tmp_path):
    d = env["d"]
    svc = FileRequestService(d, SCHEMAS, POLICY, str(tmp_path / "input"))
    rid = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(5))
    up = tmp_path / "uploads"
    for item_no in range(1, 6):
        item = up / str(item_no); item.mkdir(parents=True); (item / "text.txt").write_bytes(b"x" * 9000)
    mid = InteractionIngest(d).inbound(
        connector="qq", raw_text="传了", idempotency_key="preview-total", goal_id=1, goal_ver=1)

    out = svc.resolve(request_id=rid, uploads_dir=str(up), resolved_message_id=mid)
    previews = [item["provided"][0]["preview"] for item in out["resolution"]]
    assert [len(text.encode("utf-8")) for text in previews] == [8192, 8192, 8192, 8192, 0]
    assert sum(len(text.encode("utf-8")) for text in previews) == 32 * 1024
    assert all(item["provided"][0]["preview_truncated"] is True for item in out["resolution"])


def test_managed_quota_counts_finals_and_staging_and_rejects_symlink(tmp_path):
    import orchestrator.notify as N

    managed = tmp_path / "user_provided"
    (managed / ".staging" / "99").mkdir(parents=True)
    (managed / ".staging" / "99" / "partial").write_bytes(b"z" * 1000)
    (managed / "1" / "1").mkdir(parents=True)
    (managed / "1" / "1" / "asset-1").write_bytes(b"abc")
    (managed / "1" / "assets.manifest.json").write_bytes(b"manifest")

    assert N._managed_published_bytes(managed) == len(b"abcmanifest") + 1000
    outside = tmp_path / "outside"; outside.mkdir()
    (managed / "2").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError, match="实体目录|异常"):
        N._managed_published_bytes(managed)
    (managed / "2").unlink()
    (managed / ".staging" / "99" / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError, match="异常"):
        N._managed_published_bytes(managed)


def test_resolve_quota_includes_other_request_staging(env, tmp_path):
    """崩溃遗留的别单 staging 占用真实磁盘，也必须压缩后续 request 的可接纳余额。"""
    d = env["d"]
    svc = FileRequestService(d, SCHEMAS, POLICY, str(tmp_path / "input"))
    rid = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(1))
    managed, _current_stage, current_final = svc._managed_paths(rid)
    stale_stage = managed / ".staging" / "999"
    stale_stage.mkdir()
    (stale_stage / "partial").write_bytes(b"z" * 1024)
    up = tmp_path / "uploads"; (up / "1").mkdir(parents=True); (up / "1" / "x").write_bytes(b"x")
    mid = InteractionIngest(d).inbound(
        connector="qq", raw_text="上传", idempotency_key="quota-stale-stage", goal_id=1, goal_ver=1)
    svc.max_managed_bytes = 1024

    with pytest.raises(ValueError, match="disk_quota"):
        svc.resolve(request_id=rid, uploads_dir=str(up), resolved_message_id=mid)

    assert (stale_stage / "partial").read_bytes() == b"z" * 1024
    assert not current_final.exists()
    assert d.query_one("SELECT status FROM interaction_request WHERE id=?", (rid,))[0] == "pending"


def test_resolve_quota_includes_previously_published_assets(env, tmp_path):
    import orchestrator.notify as N

    d = env["d"]
    svc = FileRequestService(d, SCHEMAS, POLICY, str(tmp_path / "input"))
    first = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(1))
    up1 = tmp_path / "up1"; (up1 / "1").mkdir(parents=True); (up1 / "1" / "x").write_bytes(b"old")
    mid1 = InteractionIngest(d).inbound(
        connector="qq", raw_text="第一次", idempotency_key="quota-existing-1", goal_id=1, goal_ver=1)
    svc.resolve(request_id=first, uploads_dir=str(up1), resolved_message_id=mid1)
    managed = tmp_path / "input" / "user_provided"
    existing = N._managed_published_bytes(managed)

    changed = _request(1); changed["items"][0]["desc"] += "（第二份）"
    second = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=changed)
    up2 = tmp_path / "up2"; (up2 / "1").mkdir(parents=True); (up2 / "1" / "x").write_bytes(b"four")
    mid2 = InteractionIngest(d).inbound(
        connector="qq", raw_text="第二次", idempotency_key="quota-existing-2", goal_id=1, goal_ver=1)
    svc.max_managed_bytes = existing + 3

    with pytest.raises(ValueError, match="disk_quota"):
        svc.resolve(request_id=second, uploads_dir=str(up2), resolved_message_id=mid2)
    assert not (managed / str(second)).exists()
    assert d.query_one("SELECT status FROM interaction_request WHERE id=?", (second,))[0] == "pending"


def test_root_operation_claim_serializes_cross_goal_disk_quota(tmp_path, monkeypatch):
    """不同 goal/request 共用 managed_root：第二个必须看见第一个的已发布字节，不能并发双双越 quota。"""
    import orchestrator.notify as N

    db_path = tmp_path / "research.sqlite"
    d = _file_daemon(tmp_path)
    with d.transaction() as conn:
        conn.execute("INSERT INTO goal(id,version,text,predicate_json) VALUES (2,1,'g2','{}')")
    creator = FileRequestService(d, SCHEMAS, POLICY, str(tmp_path / "input"))
    rid1 = creator.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(1))
    rid2 = creator.create_checked(goal_id=2, goal_ver=1, stage="plan", request=_request(1))
    ing = InteractionIngest(d)
    mid1 = ing.inbound(
        connector="qq", raw_text="g1 文件", idempotency_key="root-quota-g1", goal_id=1, goal_ver=1)
    mid2 = ing.inbound(
        connector="qq", raw_text="g2 文件", idempotency_key="root-quota-g2", goal_id=2, goal_ver=1)
    up1 = tmp_path / "up1"; (up1 / "1").mkdir(parents=True)
    up2 = tmp_path / "up2"; (up2 / "1").mkdir(parents=True)
    source1 = up1 / "1" / "data.bin"
    source1.write_bytes(b"a" * 1200)
    (up2 / "1" / "data.bin").write_bytes(b"b" * 1200)
    first_copy_started = threading.Event()
    release_first_copy = threading.Event()
    second_done = threading.Event()
    original_copy = N._copy_hash_regular
    paused = False

    def pause_first_after_quota_snapshot(src, *args, **kwargs):
        nonlocal paused
        if Path(src) == source1 and not paused:
            paused = True
            first_copy_started.set()
            if not release_first_copy.wait(10):
                raise TimeoutError("test did not release first copy")
        return original_copy(src, *args, **kwargs)

    monkeypatch.setattr(N, "_copy_hash_regular", pause_first_after_quota_snapshot)
    outcomes = {}

    def resolve_worker(name, rid, uploads, mid, done=None):
        daemon = WriteDaemon(db.connect(str(db_path)))
        service = FileRequestService(daemon, SCHEMAS, POLICY, str(tmp_path / "input"))
        service.max_managed_bytes = 2048       # 一份 1200B+manifest 可接纳，两份累计必超
        try:
            outcomes[name] = ("ok", service.resolve(
                request_id=rid, uploads_dir=str(uploads), resolved_message_id=mid))
        except BaseException as exc:
            outcomes[name] = ("error", exc)
        finally:
            daemon.conn.close()
            if done is not None:
                done.set()

    first = threading.Thread(target=resolve_worker, args=("first", rid1, up1, mid1), daemon=True)
    second = threading.Thread(
        target=resolve_worker, args=("second", rid2, up2, mid2, second_done), daemon=True)
    first.start()
    assert first_copy_started.wait(5)          # first 已在 root claim 内完成 existing-bytes 快照
    second.start()
    assert not second_done.wait(0.3)           # 不得并发拿同一旧 quota 快照继续复制
    release_first_copy.set()
    first.join(10); second.join(10)

    assert not first.is_alive() and not second.is_alive()
    assert outcomes["first"][0] == "ok"
    assert outcomes["second"][0] == "error"
    assert isinstance(outcomes["second"][1], ValueError)
    assert "disk_quota" in str(outcomes["second"][1])
    assert d.query_one("SELECT status FROM interaction_request WHERE id=?", (rid1,))[0] == "resolved"
    assert d.query_one("SELECT status FROM interaction_request WHERE id=?", (rid2,))[0] == "pending"
    managed = tmp_path / "input" / "user_provided"
    assert N._managed_published_bytes(managed) <= 2048


def test_resolve_rejects_goal_wide_asset_count_before_terminal(env, tmp_path):
    """不可变 resolved 资产累计不得超过 compiler 的 512 上限；拒绝后当前请求仍 pending、无 final。"""
    d = env["d"]
    svc = FileRequestService(d, SCHEMAS, POLICY, str(tmp_path / "input"))
    old_mid = InteractionIngest(d).inbound(
        connector="qq", raw_text="旧资产", idempotency_key="goal-assets-old", goal_id=1, goal_ver=1)
    legacy_assets = [{"path": f"/legacy/{i}", "hash": "a" * 64, "hash_alg": "sha256"}
                     for i in range(511)]
    with d.transaction() as conn:
        conn.execute(
            "INSERT INTO interaction_request(goal_id,goal_ver,stage,status,summary_md,items_json,"
            "request_hash,resolution_json,resolved_at,resolved_message_id) VALUES "
            "(1,1,'plan','resolved','old',?,?,?,CURRENT_TIMESTAMP,?)",
            (json.dumps(_items(1)), "old-511-assets",
             json.dumps([{"provided": legacy_assets}]), old_mid))
    rid = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(1))
    up = tmp_path / "uploads"; (up / "1").mkdir(parents=True)
    (up / "1" / "a").write_bytes(b"a"); (up / "1" / "b").write_bytes(b"b")
    mid = InteractionIngest(d).inbound(
        connector="qq", raw_text="新资产", idempotency_key="goal-assets-new", goal_id=1, goal_ver=1)

    with pytest.raises(ValueError, match="ContextPack 上限"):
        svc.resolve(request_id=rid, uploads_dir=str(up), resolved_message_id=mid)
    assert d.query_one("SELECT status FROM interaction_request WHERE id=?", (rid,))[0] == "pending"
    assert not (tmp_path / "input" / "user_provided" / str(rid)).exists()


def test_resolve_fails_closed_on_corrupt_prior_resolution(env, tmp_path):
    d = env["d"]
    svc = FileRequestService(d, SCHEMAS, POLICY, str(tmp_path / "input"))
    old_mid = InteractionIngest(d).inbound(
        connector="qq", raw_text="坏旧回执", idempotency_key="bad-old-resolution", goal_id=1, goal_ver=1)
    with d.transaction() as conn:
        conn.execute(
            "INSERT INTO interaction_request(goal_id,goal_ver,stage,status,summary_md,items_json,"
            "request_hash,resolution_json,resolved_at,resolved_message_id) VALUES "
            "(1,1,'plan','resolved','old',?,?,?,CURRENT_TIMESTAMP,?)",
            (json.dumps(_items(1)), "corrupt-old", json.dumps([{"provided": "not-an-array"}]), old_mid))
    rid = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(1))
    up = tmp_path / "uploads"; (up / "1").mkdir(parents=True); (up / "1" / "x").write_bytes(b"x")
    mid = InteractionIngest(d).inbound(
        connector="qq", raw_text="新上传", idempotency_key="after-bad-old", goal_id=1, goal_ver=1)

    with pytest.raises(ValueError, match="provided 回执损坏"):
        svc.resolve(request_id=rid, uploads_dir=str(up), resolved_message_id=mid)
    assert d.query_one("SELECT status FROM interaction_request WHERE id=?", (rid,))[0] == "pending"
    assert not (tmp_path / "input" / "user_provided" / str(rid)).exists()


def test_resolve_fsyncs_files_and_both_sides_of_publish_rename(env, tmp_path, monkeypatch):
    import orchestrator.notify as N

    d = env["d"]
    svc = FileRequestService(d, SCHEMAS, POLICY, str(tmp_path / "input"))
    rid = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(1))
    up = tmp_path / "uploads"; (up / "1").mkdir(parents=True); (up / "1" / "x").write_bytes(b"x")
    mid = InteractionIngest(d).inbound(
        connector="qq", raw_text="传了", idempotency_key="fsync-publish", goal_id=1, goal_ver=1)
    original_fsync = N.os.fsync
    original_fsync_directory = N._fsync_directory
    fsynced_fds = []
    fsynced_dirs = []

    def track_fsync(fd):
        try:
            fsynced_fds.append(Path(os.readlink(f"/proc/self/fd/{fd}")))
        except OSError:
            pass
        return original_fsync(fd)

    def track_directory(path):
        fsynced_dirs.append(Path(path))
        return original_fsync_directory(path)

    import os
    monkeypatch.setattr(N.os, "fsync", track_fsync)
    monkeypatch.setattr(N, "_fsync_directory", track_directory)
    svc.resolve(request_id=rid, uploads_dir=str(up), resolved_message_id=mid)

    managed = tmp_path / "input" / "user_provided"
    stage = managed / ".staging" / str(rid)
    final = managed / str(rid)
    assert any(path.name == "asset-1" for path in fsynced_fds)
    assert any(path.name == "assets.manifest.json" for path in fsynced_fds)
    for expected in (stage / "1", stage, managed / ".staging", final / "1", final, managed,
                     tmp_path / "input", tmp_path):
        assert expected in fsynced_dirs


def test_cancel_cleans_non_authoritative_request_directories(env, tmp_path):
    d = env["d"]
    svc = FileRequestService(d, SCHEMAS, POLICY, str(tmp_path / "input"))
    rid = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(1))
    managed = tmp_path / "input" / "user_provided"
    for p in (managed / str(rid), managed / ".staging" / str(rid)):
        p.mkdir(parents=True); (p / "partial").write_bytes(b"x")
    mid = InteractionIngest(d).inbound(connector="qq", raw_text="取消", idempotency_key="cleanup-cancel",
                                       goal_id=1, goal_ver=1)
    svc.cancel(request_id=rid, reason="不再提供", resolved_message_id=mid)
    assert not (managed / str(rid)).exists() and not (managed / ".staging" / str(rid)).exists()


def test_cancel_pipeline(env, tmp_path):
    d = env["d"]
    svc = FileRequestService(d, SCHEMAS, POLICY, str(tmp_path / "input"))
    rid = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(1))
    mid = InteractionIngest(d).inbound(connector="qq", raw_text="不用了", idempotency_key="cx-1",
                                       goal_id=1, goal_ver=1)
    svc.cancel(request_id=rid, reason="用户改用公开数据", resolved_message_id=mid)
    st, res = d.query_one("SELECT status, resolution_json FROM interaction_request WHERE id=?", (rid,))
    assert st == "cancelled" and json.loads(res)["reason"] == "用户改用公开数据"


@pytest.mark.parametrize("reason", [None, "", "   ", "\x01bad", "x" * 2001])
def test_cancel_reason_rejects_empty_non_string_and_oversize(env, tmp_path, reason):
    d = env["d"]
    svc = FileRequestService(d, SCHEMAS, POLICY, str(tmp_path / "input"))
    rid = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(1))
    mid = InteractionIngest(d).inbound(
        connector="qq", raw_text="取消", idempotency_key="cancel-reason-bad", goal_id=1, goal_ver=1)
    with pytest.raises(ValueError, match="reason"):
        svc.cancel(request_id=rid, reason=reason, resolved_message_id=mid)
    assert d.query_one("SELECT status FROM interaction_request WHERE id=?", (rid,))[0] == "pending"


def test_cancel_reason_accepts_2000_character_boundary(env, tmp_path):
    d = env["d"]
    svc = FileRequestService(d, SCHEMAS, POLICY, str(tmp_path / "input"))
    rid = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(1))
    mid = InteractionIngest(d).inbound(
        connector="qq", raw_text="取消", idempotency_key="cancel-reason-boundary", goal_id=1, goal_ver=1)
    reason = "界" * 2000
    svc.cancel(request_id=rid, reason=reason, resolved_message_id=mid)
    stored = json.loads(d.query_one(
        "SELECT resolution_json FROM interaction_request WHERE id=?", (rid,))[0])
    assert stored["reason"] == reason


# ============ 文件请求 3 事件（reminder 分档幂等；now 注入保确定性）============
def test_file_request_events_pending_reminder_resolved(env, tmp_path):
    d, ob = env["d"], env["ob"]
    svc = FileRequestService(d, SCHEMAS, POLICY, str(tmp_path / "input"))
    rid = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(1))
    fn = FileRequestNotifier(d, ob, remind_interval_h=POLICY["interaction_request"]["remind_interval_h"])
    created = float(d.query_one("SELECT strftime('%s', created_at) FROM interaction_request WHERE id=?", (rid,))[0])
    assert f"filereq:{rid}:pending" in fn.scan(now_ts=created + 60)          # 立即：pending 事件
    assert fn.scan(now_ts=created + 60) == []                                # 同态重扫幂等
    assert f"filereq:{rid}:reminder:1" in fn.scan(now_ts=created + 25 * 3600)   # 过 24h：第 1 档提醒
    assert fn.scan(now_ts=created + 25 * 3600) == []                         # 同档幂等
    assert f"filereq:{rid}:reminder:2" in fn.scan(now_ts=created + 49 * 3600)   # 第 2 档
    mid = InteractionIngest(d).inbound(connector="qq", raw_text="上传了", idempotency_key="ev-1",
                                       goal_id=1, goal_ver=1)
    up = tmp_path / "up"; (up / "1").mkdir(parents=True); (up / "1" / "f").write_bytes(b"x")
    svc.resolve(request_id=rid, uploads_dir=str(up), resolved_message_id=mid)
    assert f"filereq:{rid}:resolved" in fn.scan(now_ts=created + 50 * 3600)  # 终态事件


# ============ 全局等待（§7.1 M5：Advancer 停、query/通知照常）============
@pytest.fixture()
def adv_env(tmp_path):
    """文件库真组件 advancer + console + mediator + 通知（端到端全局等待）。"""
    from orchestrator.advancer import SqliteAdvancer
    from orchestrator.compiler_sqlite import SqliteCompiler
    from orchestrator.statestore_sqlite import SQLiteStateStore
    dbp = str(tmp_path / "w.sqlite")
    daemon = WriteDaemon(db.connect(dbp))
    state = SQLiteStateStore(daemon, POLICY)
    state.create_goal(text="g", predicate_json={})
    console = Console(daemon)
    card_path = tmp_path / "sc.json"
    pub = SC.SqliteStatusPublisher(open_responder_read_conn(dbp), policy=POLICY,
                                   out_path=str(card_path))

    def provider(cyc, pack):
        return {"tree_ops.json": {"ops": [{"op": "create_root", "text": "根问题", "local_key": "r"}]},
                "selection.json": {"next_question_id": None, "next_intent": "terminate", "scores": []}}

    adv = SqliteAdvancer(state, SqliteCompiler(db.connect(dbp), POLICY), provider,
                         status_publisher=pub, precheck=make_advancer_precheck(console, daemon))
    return {"d": daemon, "c": console, "adv": adv, "tmp": tmp_path, "card": card_path, "dbp": dbp}


def test_global_wait_pending_request_blocks_advancer_but_not_query(adv_env, tmp_path):
    d, adv = adv_env["d"], adv_env["adv"]
    svc = FileRequestService(d, SCHEMAS, POLICY, str(adv_env["tmp"] / "input"))
    rid = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(1))
    assert adv.run_cycles(2) == []                                           # 不开新轮、不发研究执行
    assert "文件请求" in adv.last_block_reason
    assert d.query_one("SELECT count(*) FROM cycle")[0] == 0                 # 一个轮都没开
    # query/通知照常（不走 Advancer）
    ob = Outbox(str(adv_env["tmp"] / "ob"))
    fn = FileRequestNotifier(d, ob, remind_interval_h=24)
    created = float(d.query_one("SELECT strftime('%s', created_at) FROM interaction_request WHERE id=?", (rid,))[0])
    assert fn.scan(now_ts=created + 1)                                       # 通知照常
    conn = FakeConnector(); assert ob.deliver_pending(conn)                  # 投递照常
    # resolve 后恢复推进
    mid = InteractionIngest(d).inbound(connector="qq", raw_text="上传", idempotency_key="gw-1",
                                       goal_id=1, goal_ver=1)
    up = adv_env["tmp"] / "u"; (up / "1").mkdir(parents=True); (up / "1" / "f").write_bytes(b"x")
    svc.resolve(request_id=rid, uploads_dir=str(up), resolved_message_id=mid)
    ids = adv.run_cycles(2)
    assert ids and adv_env["card"].exists()                                  # 恢复推进 + 发布照常


def test_global_wait_query_answers_during_block(adv_env):
    """阻断期间 mediator 应答照常（发布快照已存在时）。"""
    d, adv = adv_env["d"], adv_env["adv"]
    ids = adv.run_cycles(1)                                                  # 先正常跑一轮 → 卡已发布
    assert ids
    r = Console(d).handle_inbound(connector="qq", raw_text="暂停", idempotency_key="gw-p", goal_id=1, goal_ver=1)
    console = Console(d)
    console.confirm_directive(directive_id=r["directive_id"],
                              confirm_message_id=_action_message(d, console, r, "confirm"))
    assert adv.run_cycles(1) == []                                           # precheck 消费 pause → 阻断
    assert "pause" in adv.last_block_reason
    med = Mediator(d, str(adv_env["card"]))
    mid = InteractionIngest(d).inbound(connector="qq", raw_text="现在状态？", idempotency_key="gw-q",
                                       goal_id=1, goal_ver=1)
    ans = med.handle_query(message_id=mid)                                   # query 照常
    assert ans["reply_text"] and ans["snapshot_cycle"] == ids[-1]


def test_precheck_consumes_immediate_directive_with_decision(adv_env):
    """前置检查按时机消费：immediate pause 在 run_cycles 入口被消费（DECISION 落账）后生效阻断。"""
    d, c, adv = adv_env["d"], adv_env["c"], adv_env["adv"]
    r = c.handle_inbound(connector="qq", raw_text="暂停", idempotency_key="pc-1", goal_id=1, goal_ver=1)
    c.confirm_directive(directive_id=r["directive_id"],
                        confirm_message_id=_action_message(d, c, r, "confirm"))
    assert adv.run_cycles(1) == []
    st = d.query_one("SELECT status, consumed_cycle FROM directive WHERE id=?", (r["directive_id"],))
    assert st[0] == "consumed" and st[1] is None                             # 开轮前消费：无在途轮，cycle 空
    assert d.query_one("SELECT count(*) FROM decision WHERE directive_id=? AND actor='human'",
                       (r["directive_id"],))[0] == 1                        # 同记 DECISION
    # resume 解除后恢复
    r2 = c.handle_inbound(connector="qq", raw_text="继续", idempotency_key="pc-2", goal_id=1, goal_ver=1)
    c.confirm_directive(directive_id=r2["directive_id"],
                        confirm_message_id=_action_message(d, c, r2, "confirm"))
    assert adv.run_cycles(1)                                                 # resume 被消费 → 放行
