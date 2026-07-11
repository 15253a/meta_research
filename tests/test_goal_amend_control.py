"""CP11.2b.3b · goal_amend durable control and version transition."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from orchestrator import database as db
from orchestrator.advancer import SqliteAdvancer
from orchestrator.console import (DIRECTIVE_ACTION_SESSION_REF, Console,
                                  directive_action_text)
from orchestrator.compiler_sqlite import SqliteCompiler
from orchestrator.interfaces import Selection
from orchestrator.notify import DirectiveNotifier, Outbox, make_advancer_precheck
from orchestrator.statestore_sqlite import SQLiteStateStore
from orchestrator.status_card import build_status_card
from orchestrator.writedaemon import WriteDaemon

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
POLICY = yaml.safe_load((SYSTEM_ROOT / "policies" / "policy.yaml").read_text(encoding="utf-8"))


def _components(path: str):
    daemon = WriteDaemon(db.connect(path))
    state = SQLiteStateStore(daemon, POLICY)
    compiler = SqliteCompiler(db.connect(path), POLICY)
    return daemon, state, compiler


def _bootstrap_terminated(state: SQLiteStateStore) -> str:
    cyc = state.open_or_resume_cycle()
    state.set_route(cyc.cycle_id, "bootstrap")
    state.apply_tree_ops(cyc.cycle_id, [
        {"op": "create_root", "text": "旧目标下的开放根问题", "local_key": "root"}])
    state.persist_selection(cyc.cycle_id, Selection(
        next_question_id=None, next_intent="terminate",
        scores=[{"question_id": "root", "score": 0.2, "est_cost": 3.0}]))
    state.mark_cycle_done(cyc.cycle_id)
    return cyc.cycle_id


def _closed_answer(daemon: WriteDaemon, cycle_id: str) -> str:
    ci = int(cycle_id[1:])
    with daemon.transaction() as conn:
        qi = conn.execute(
            "INSERT INTO question(parent_id,goal_id,goal_ver,born_goal_ver,text,status,source) "
            "VALUES (1,1,1,1,'旧版已关闭结论','open','agent')").lastrowid
        aid = conn.execute(
            "INSERT INTO answer(question_id,goal_id,goal_ver,cycle_id,verdict,answer_md) "
            "VALUES (?,1,1,?,'answered','旧答案')", (qi, ci)).lastrowid
        conn.execute(
            "INSERT INTO evidence(answer_id,question_id,goal_id,goal_ver,kind,literature_ref,claim_md) "
            "VALUES (?,?,1,1,'literature','ref:old','旧证据')", (aid, qi))
        conn.execute("UPDATE question SET status='answered' WHERE id=?", (qi,))
    return f"a{aid}"


def _action_message(daemon: WriteDaemon, console: Console, result: dict) -> int:
    did = result["directive_id"]
    source = daemon.query_one(
        "SELECT m.goal_id,m.goal_ver,m.connector,m.conversation_id,m.session_ref "
        "FROM directive d JOIN interaction_message m "
        "ON m.id=d.source_interaction_message_id WHERE d.id=?", (did,))
    session_ref = (DIRECTIVE_ACTION_SESSION_REF
                   if source[2] == "console" else
                   (source[4] + ":action" if source[4] is not None else None))
    mid = console.ingest.inbound(
        connector=source[2], raw_text=directive_action_text("confirm", did),
        idempotency_key=f"confirm-d{did}", goal_id=source[0], goal_ver=source[1],
        session_ref=session_ref, conversation_id=source[3])
    with daemon.transaction() as conn:
        conn.execute(
            "INSERT INTO interaction_classification(message_id,intent,directive_id) "
            "VALUES (?,'unclear',NULL)", (mid,))
    return mid


def _submit_amend(daemon: WriteDaemon, *, text="新的生产目标"):
    console = Console(daemon, policy=POLICY)
    goal_id, goal_ver = daemon.query_one(
        "SELECT id,version FROM goal ORDER BY version DESC LIMIT 1")
    result = console.handle_inbound(
        connector="console", raw_text=f"修订目标：改为{text}",
        idempotency_key=f"amend-v{goal_ver}-{text}", goal_id=goal_id, goal_ver=goal_ver)
    console.confirm_directive(
        directive_id=result["directive_id"],
        confirm_message_id=_action_message(daemon, console, result))
    return console, result["directive_id"]


def _provider_for(answer_id: str, *, text="新的生产目标", complete_scores=True,
                  mismatched=False):
    def provider(cyc, pack):
        assert cyc.route == "goal_amend"
        assert "旧目标 v1" in pack.anchor_md
        assert '"kind":"goal_amend"' in pack.anchor_md
        directive_section = pack.anchor_md.split("## 本轮已消费人类 directive", 1)[1]
        directive_json = directive_section.split("```json\n", 1)[1].split("\n```", 1)[0]
        amendments = [item for item in json.loads(directive_json)
                      if item["kind"] == "goal_amend"]
        assert amendments == [{
            "directive_id": amendments[0]["directive_id"],
            "hardness": "hard",
            "kind": "goal_amend",
            "new_goal_text": text,
            "predicate_json": {"scope": "old"},
            "polished": amendments[0]["polished"],
            "rationale_md": "用户明确修订研究目标",
            "source_goal_ver": 1,
            "target_goal_ver": 2,
        }]
        assert any(source.startswith("db:decision:") for source in pack.sources)
        new_text = "模型擅自改写的目标" if mismatched else text
        ops = [
            {"op": "amend_goal", "new_goal_text": new_text,
             "predicate_json": {"scope": "old"},
             "rationale_md": "用户明确修订研究目标"},
            {"op": "seed_applicability_audit", "answer_ids": [answer_id],
             "rationale_md": "新谓词可能影响旧结论"},
            {"op": "spawn_question", "kind": "goal_retarget",
             "parent_question_id": None, "text": "新版根问题", "local_key": "retarget"},
        ]
        scores = ([
            {"question_id": "q1", "score": 0.4, "est_cost": 2.0},
            {"question_id": "retarget", "score": 0.9, "est_cost": 1.0},
        ] if complete_scores else [])
        return {"tree_ops.json": {"ops": ops},
                "selection.json": {"next_question_id": "retarget",
                                   "next_intent": "attack", "scores": scores}}
    return provider


def test_stale_cycle_cannot_bind_goal_amend_route(tmp_path):
    path = str(tmp_path / "stale-route.sqlite")
    daemon, state, compiler = _components(path)
    state.create_goal(text="v1", predicate_json={})
    stale = state.open_or_resume_cycle()
    with daemon.transaction() as conn:
        conn.execute(
            "INSERT INTO goal(id,version,text,predicate_json,previous_version) "
            "VALUES (1,2,'v2','{}',1)")
    _, did = _submit_amend(daemon, text="v3")
    with pytest.raises(ValueError, match="不可绑定"):
        state.set_goal_amend_route(stale.cycle_id, did)
    assert daemon.query_one("SELECT route FROM cycle WHERE id=?", (int(stale.cycle_id[1:]),))[0] is None
    compiler.conn.close(); daemon.conn.close()


def test_goal_amend_route_restart_versioning_and_notifications(tmp_path):
    path = str(tmp_path / "research.sqlite")
    d1, state1, compiler1 = _components(path)
    state1.create_goal(text="旧目标 v1", predicate_json={"scope": "old"})
    c1 = _bootstrap_terminated(state1)
    answer_id = _closed_answer(d1, c1)
    console1, did = _submit_amend(d1)

    outbox = Outbox(str(tmp_path / "outbox"))
    notifier1 = DirectiveNotifier(d1, outbox)
    adv1 = SqliteAdvancer(
        state1, compiler1, lambda *_: (_ for _ in ()).throw(AssertionError("重启前不得调用 provider")),
        precheck=make_advancer_precheck(console1, d1))

    # goal_amend rule precedes the previous durable terminate selection.
    c2 = adv1._resume_or_open()
    assert c2.route == "goal_amend" and c2.question_id is None
    assert adv1._blocked(c2) is False                  # reasoning_start consumes exact directive
    assert d1.query_one("SELECT status,consumed_cycle FROM directive WHERE id=?", (did,)) == (
        "consumed", int(c2.cycle_id[1:]))
    notifier1.scan()
    queued = outbox._queued_keys()
    assert f"directive:{did}:pending_effect:v2" in queued
    assert f"directive:{did}:applied:v2" not in queued   # consume != goal version committed

    # Crash/restart after consume but before model/application: a fresh process
    # resumes the same control cycle and commits the whole version transition.
    compiler1.conn.close(); d1.conn.close()
    d2, state2, compiler2 = _components(path)
    console2 = Console(d2, policy=POLICY)
    adv2 = SqliteAdvancer(
        state2, compiler2, _provider_for(answer_id),
        precheck=make_advancer_precheck(console2, d2))
    assert adv2.run_cycles(max_cycles=1) == [c2.cycle_id]

    goals = d2.query(
        "SELECT version,text,previous_version,created_cycle,directive_id FROM goal WHERE id=1 ORDER BY version")
    assert goals == [
        (1, "旧目标 v1", None, None, None),
        (2, "新的生产目标", 1, int(c2.cycle_id[1:]), did),
    ]
    assert d2.query_one(
        "SELECT goal_ver,born_goal_ver,status,score,est_cost FROM question WHERE id=1") == (
        2, 1, "open", 0.4, 2.0)
    closed = d2.query_one(
        "SELECT q.goal_ver,q.born_goal_ver,q.status,a.goal_ver FROM question q "
        "JOIN answer a ON a.question_id=q.id WHERE a.id=?", (int(answer_id[1:]),))
    assert closed == (1, 1, "answered", 1)             # closed question/answer never reopened/migrated
    assert d2.query_one(
        "SELECT goal_ver,born_goal_ver,source,score FROM question WHERE text='新版根问题'") == (
        2, 2, "goal_amend", 0.9)
    assert d2.query_one(
        "SELECT goal_ver,status,audit_cycle FROM answer_applicability WHERE answer_id=?",
        (int(answer_id[1:]),)) == (2, "pending", int(c2.cycle_id[1:]))
    assert d2.query_one(
        "SELECT goal_ver,route,status,next_intent FROM cycle WHERE id=?", (int(c2.cycle_id[1:]),)) == (
        2, "goal_amend", "done", "attack")

    notifier2 = DirectiveNotifier(d2, outbox)
    assert f"directive:{did}:applied:v2" in notifier2.scan()
    applied = [json.loads(line) for line in outbox.queue_path.read_text(encoding="utf-8").splitlines()
               if json.loads(line)["event_key"] == f"directive:{did}:applied:v2"][0]
    assert applied["payload"]["effect"]["target_goal_ver"] == 2

    # Exact-cycle rendering remains version-correct both backward and forward;
    # no startup-cached goal text can leak v1 into the new cycle.
    reader = SqliteCompiler(db.connect(path), POLICY)
    old_pack = reader.render(cycle_id=c1, stage="reasoning")
    new_pack = reader.render(cycle_id=c2.cycle_id, stage="reasoning")
    assert "旧目标 v1" in old_pack.anchor_md and "新的生产目标" not in old_pack.anchor_md
    assert "新版根问题" not in old_pack.anchor_md     # v1 pack 不混入 v2 前沿
    assert "新的生产目标" in new_pack.anchor_md and "db:goal:1:v2" in new_pack.sources
    old_card = build_status_card(db.connect(path), cycle_id=c1, policy=POLICY)
    assert old_card["counts"] == {"open": 0, "inconclusive": 0}
    card = build_status_card(db.connect(path), cycle_id=c2.cycle_id, policy=POLICY)
    assert card["goal"] == {"id": 1, "ver": 2, "summary": "新的生产目标"}
    assert card["counts"] == {"open": 2, "inconclusive": 0}


def test_goal_amend_exact_effect_is_not_truncated_in_reasoning_anchor(tmp_path):
    """展示用 polished 可裁剪；机械权威三字段必须完整、含继承后的有效 predicate。"""
    path = str(tmp_path / "long-effect.sqlite")
    daemon, state, compiler = _components(path)
    state.create_goal(text="旧目标 v1", predicate_json={"scope": "old"})
    c1 = _bootstrap_terminated(state)
    answer_id = _closed_answer(daemon, c1)
    long_text = "长目标" * 600                 # >2KB UTF-8，且含命令前缀仍低于 2,000 字符
    console, _ = _submit_amend(daemon, text=long_text)
    adv = SqliteAdvancer(
        state, compiler, _provider_for(answer_id, text=long_text),
        precheck=make_advancer_precheck(console, daemon))
    assert adv.run_cycles(max_cycles=1)
    assert daemon.query_one(
        "SELECT text FROM goal WHERE id=1 AND version=2")[0] == long_text


def test_goal_amend_mismatch_and_incomplete_rescore_roll_back(tmp_path):
    path = str(tmp_path / "rollback.sqlite")
    daemon, state, compiler = _components(path)
    state.create_goal(text="旧目标 v1", predicate_json={"scope": "old"})
    c1 = _bootstrap_terminated(state)
    answer_id = _closed_answer(daemon, c1)
    console, _ = _submit_amend(daemon)
    adv = SqliteAdvancer(state, compiler, _provider_for(answer_id, mismatched=True),
                         precheck=make_advancer_precheck(console, daemon))
    c2 = adv._resume_or_open(); assert adv._blocked(c2) is False

    with pytest.raises(ValueError, match="用户已确认"):
        adv.advance(c2.cycle_id)
    assert daemon.query_one("SELECT count(*) FROM goal")[0] == 1
    assert daemon.query_one("SELECT goal_ver,score FROM question WHERE id=1") == (1, 0.2)
    assert state.cycle(c2.cycle_id).status == "created"

    adv._reasoning = _provider_for(answer_id, complete_scores=False)
    with pytest.raises(ValueError, match="重评全部"):
        adv.advance(c2.cycle_id)
    assert daemon.query_one("SELECT count(*) FROM goal")[0] == 1
    assert daemon.query_one("SELECT count(*) FROM answer_applicability")[0] == 0

    adv._reasoning = _provider_for(answer_id)
    assert adv.advance(c2.cycle_id) == "done"
    assert daemon.query_one("SELECT max(version) FROM goal WHERE id=1")[0] == 2


def test_goal_amend_route_without_consumed_authority_aborts_without_model(tmp_path):
    path = str(tmp_path / "cancel.sqlite")
    daemon, state, compiler = _components(path)
    state.create_goal(text="旧目标 v1", predicate_json={})
    _bootstrap_terminated(state)
    console, did = _submit_amend(daemon)
    called = {"n": 0}

    def provider(*_):
        called["n"] += 1
        raise AssertionError("无已消费改版权威时不得调用模型")

    adv = SqliteAdvancer(state, compiler, provider)
    cyc = adv._resume_or_open()
    assert cyc.route == "goal_amend"
    console.reject_unapplicable_directive(
        directive_id=did, reason="路由提交后改版被撤销", cycle_id=cyc.cycle_id)
    assert adv.advance(cyc.cycle_id) == "done"
    assert called["n"] == 0
    assert state.cycle(cyc.cycle_id).status == "aborted"
    assert daemon.query_one("SELECT count(*) FROM goal")[0] == 1
    assert daemon.query_one(
        "SELECT count(*) FROM decision WHERE cycle_id=? AND type='goal_amend_cancelled_before_effect'",
        (int(cyc.cycle_id[1:]),))[0] == 1


def test_bootstrap_route_precedes_pending_goal_amend(tmp_path):
    path = str(tmp_path / "bootstrap-first.sqlite")
    daemon, state, compiler = _components(path)
    state.create_goal(text="尚未创树的 v1", predicate_json={})
    _submit_amend(daemon, text="启动后收到的修订")
    adv = SqliteAdvancer(state, compiler, lambda *_: None)
    cyc = adv._resume_or_open()
    assert cyc.route == "bootstrap"


def test_goal_amend_arriving_during_attack_reasoning_is_deferred(tmp_path):
    path = str(tmp_path / "defer.sqlite")
    daemon, state, _ = _components(path)
    state.create_goal(text="旧目标", predicate_json={})
    cyc = state.open_or_resume_cycle()
    state.set_route(cyc.cycle_id, "attack")
    with daemon.transaction() as conn:
        conn.execute("UPDATE cycle SET status='bundle' WHERE id=?", (int(cyc.cycle_id[1:]),))
    console, did = _submit_amend(daemon)
    current = state.cycle(cyc.cycle_id)
    assert make_advancer_precheck(console, daemon)(current) is None
    assert daemon.query_one(
        "SELECT status,consumed_cycle FROM directive WHERE id=?", (did,)) == ("pending", None)


def test_newer_confirmation_between_route_and_consume_rebinds_atomically(tmp_path):
    path = str(tmp_path / "rebind.sqlite")
    daemon, state, compiler = _components(path)
    state.create_goal(text="旧目标", predicate_json={})
    _bootstrap_terminated(state)
    console, first = _submit_amend(daemon, text="第一修订")
    adv = SqliteAdvancer(state, compiler, lambda *_: None)
    cyc = adv._resume_or_open()
    assert daemon.query_one(
        "SELECT directive_id FROM decision WHERE cycle_id=? AND type='goal_amend_routed'",
        (int(cyc.cycle_id[1:]),))[0] == first

    console, second = _submit_amend(daemon, text="第二修订")
    assert daemon.query_one("SELECT status FROM directive WHERE id=?", (first,))[0] == "superseded"
    assert make_advancer_precheck(console, daemon)(cyc) is None
    assert daemon.query_one("SELECT status FROM directive WHERE id=?", (second,))[0] == "consumed"
    assert daemon.query_one(
        "SELECT directive_id FROM decision WHERE cycle_id=? "
        "AND type IN ('goal_amend_routed','goal_amend_rebound') ORDER BY id DESC LIMIT 1",
        (int(cyc.cycle_id[1:]),))[0] == second


def test_legacy_confirmed_parse_error_is_not_an_effective_amendment(tmp_path):
    """纵深防御：升级前遗留的 malformed+confirmed pending 行也不能占路由或制造多有效修订。"""
    path = str(tmp_path / "legacy-malformed.sqlite")
    daemon, state, compiler = _components(path)
    state.create_goal(text="旧目标", predicate_json={})
    _bootstrap_terminated(state)
    console, valid = _submit_amend(daemon, text="有效修订")
    bad = console.handle_inbound(
        connector="console", raw_text='修订目标 {"new_goal_text":"坏","unknown":1}',
        idempotency_key="legacy-bad", goal_id=1, goal_ver=1)
    console.confirm_directive(
        directive_id=bad["directive_id"],
        confirm_message_id=_action_message(daemon, console, bad))
    # 模拟旧版本已经留下的 confirmed/pending poison row。
    with daemon.transaction() as conn:
        conn.execute("UPDATE directive SET status='pending' WHERE id=?", (bad["directive_id"],))

    assert state.pending_goal_amend_directive() == valid
    adv = SqliteAdvancer(state, compiler, lambda *_: None)
    cyc = adv._resume_or_open()
    assert cyc.route == "goal_amend"
    assert daemon.query_one(
        "SELECT directive_id FROM decision WHERE cycle_id=? AND type='goal_amend_routed'",
        (int(cyc.cycle_id[1:]),))[0] == valid
