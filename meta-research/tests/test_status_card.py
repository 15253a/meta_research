"""CP3.3 · status_card 构建器（§4.6.6 封闭字段清单）。

核心验收（§7.1 M2）：status_card 从 DB 真相构建**封闭字段集**（不多不少），真字段取自 DB、
M3-待接字段诚实置 None；canonical JSON 可比对。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import conftest
from orchestrator import database as db
from orchestrator import status_card as SC

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
POLICY = yaml.safe_load((SYSTEM_ROOT / "policies" / "policy.yaml").read_text(encoding="utf-8"))

CLOSED_FIELDS = {"snapshot_cycle", "goal", "active_question", "cycle_status", "route",
                 "selection", "budget", "counts", "heartbeat_ref", "pending_file_request"}


def _seed(conn):
    conftest.seed_minimal(conn)   # goal1/cycle1(reasoning)/q1(answered)
    conn.executescript("""
      INSERT INTO question(id,goal_id,goal_ver,born_goal_ver,text,status,source) VALUES (2,1,1,1,'q2 活跃','active','agent');
      INSERT INTO question(id,goal_id,goal_ver,born_goal_ver,text,status,source) VALUES (3,1,1,1,'q3 开放','open','agent');
      INSERT INTO question(id,goal_id,goal_ver,born_goal_ver,text,status,source) VALUES (4,1,1,1,'q4 无定论','open','agent');
      UPDATE question SET status='inconclusive' WHERE id=4;   -- 初始须 open/active，再转 inconclusive（触发器）
      UPDATE cycle SET active_question_id=2, route='attack', next_question_id=3, next_intent='attack', cost_total=3.5 WHERE id=1;
      INSERT INTO ledger(id,cycle_id,phase,evaluation_attempt_id,money,policy_version)
        VALUES (2,1,'reasoning',1,3.5,'v0');
      INSERT INTO interaction_request(id,goal_id,goal_ver,cycle_id,stage,status,summary_md,items_json,request_hash)
        VALUES (1,1,1,1,'plan','pending','需要数据集 X','[{"kind":"dataset","desc":"X"},{"kind":"paper","desc":"Y"}]','rh1');
    """)
    conn.commit()


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    _seed(c)
    return c


def _card(conn):
    return SC.build_status_card(conn, cycle_id="c1", policy=POLICY)


# ============ 封闭字段集（不多不少）============
def test_closed_field_set_exact(conn):
    assert set(_card(conn).keys()) == CLOSED_FIELDS


# ============ 真字段取自 DB ============
def test_core_fields_from_db(conn):
    c = _card(conn)
    assert c["snapshot_cycle"] == "c1"
    assert c["goal"] == {"id": 1, "ver": 1, "summary": "g"}   # 精确取 DB goal 版本首行
    assert c["active_question"]["id"] == "q2" and c["active_question"]["status"] == "active"
    assert c["cycle_status"] == "reasoning" and c["route"] == "attack"


def test_selection_from_cycle(conn):
    """selection 权威状态取 cycle.next_*；latest_decision（M5 CP6.2 接线）= 本 cycle 作用域最近 decision
    摘要；本轮无 decision 行 → 诚实 None。"""
    sel = _card(conn)["selection"]
    assert set(sel.keys()) == {"intent", "next_question_id", "latest_decision"}   # 子字段封闭（防悄悄扩表）
    assert sel["intent"] == "attack" and sel["next_question_id"] == "q3"
    assert sel["latest_decision"] is None                                         # 本轮无 decision（无跨轮串卡）


def test_latest_decision_cycle_scoped(conn):
    """latest_decision 按 cycle 作用域取最近一条（非全局 LIMIT 1——防跨轮/跨 goal 串卡）。"""
    conn.executescript("""
      INSERT INTO decision(cycle_id,actor,type,payload_json) VALUES (1,'agent','decompose','{}');
      INSERT INTO decision(cycle_id,actor,type,payload_json) VALUES (1,'human','directive_pause','{}');
      INSERT INTO cycle(id,goal_id,goal_ver,status,policy_version) VALUES (2,1,1,'reasoning','v0');
      INSERT INTO decision(cycle_id,actor,type,payload_json) VALUES (2,'agent','create_root','{}');
    """)
    conn.commit()
    ld = _card(conn)["selection"]["latest_decision"]          # 卡取 c1：c2 的更新 decision 不串入
    assert (ld["actor"], ld["type"]) == ("human", "directive_pause")
    assert ld["id"] == conn.execute("SELECT max(id) FROM decision WHERE cycle_id=1").fetchone()[0]


def test_budget_fields(conn):
    b = _card(conn)["budget"]
    assert set(b.keys()) == {"B_t", "cycle_spent", "global_remaining"}   # §4.6.6 预算三元（不多不少）
    assert b["B_t"] == 5.0 and b["cycle_spent"] == 3.5   # 无 done cycle → B0=5；花费只认 ledger
    assert b["global_remaining"] == 99996.5              # session_max - 全局 ledger


def test_counts_open_inconclusive(conn):
    assert _card(conn)["counts"] == {"open": 1, "inconclusive": 1}   # q3 open, q4 inconclusive（q1 answered/q2 active 不计）


def test_counts_are_scoped_to_snapshot_goal_version(conn):
    conn.executescript("""
      INSERT INTO goal(id,version,text,predicate_json,previous_version)
        VALUES (1,2,'g-v2','{}',1);
      INSERT INTO question(id,goal_id,goal_ver,born_goal_ver,text,status,source)
        VALUES (99,1,2,2,'v2 open','open','goal_amend');
    """)
    conn.commit()
    assert _card(conn)["counts"] == {"open": 1, "inconclusive": 1}


def test_pending_file_request(conn):
    p = _card(conn)["pending_file_request"]
    assert p["request_id"] == 1 and p["item_count"] == 2 and p["created_at"]


def test_pending_request_non_list_items_count_none():
    """codex SHOULD 回归：items_json 契约=数组；若为 JSON 对象/串（畸形）→ item_count 诚实 None（不按键/字符数误报）。"""
    c = db.connect(":memory:")
    conftest.seed_minimal(c)
    c.execute("INSERT INTO interaction_request(id,goal_id,goal_ver,cycle_id,stage,status,summary_md,items_json,request_hash) "
              "VALUES (1,1,1,1,'plan','pending','s','{\"kind\":\"dataset\"}','rh')")   # JSON 对象，非数组
    c.commit()
    p = SC.build_status_card(c, cycle_id="c1", policy=POLICY)["pending_file_request"]
    assert p["request_id"] == 1 and p["item_count"] is None


def test_m3_pending_fields_none(conn):
    c = _card(conn)
    assert c["heartbeat_ref"] is None               # M3 outbox


# ============ 无 active question / 无 pending 请求 ============
def test_no_active_question_and_no_pending():
    c = db.connect(":memory:")
    conftest.seed_minimal(c)   # cycle1 无 active_question_id、无 pending 请求
    card = SC.build_status_card(c, cycle_id="c1", policy=POLICY)
    assert card["active_question"] is None and card["pending_file_request"] is None
    assert set(card.keys()) == CLOSED_FIELDS         # 封闭集不变（None 也占字段位）


# ============ canonical JSON ============
def test_status_card_json_roundtrip(conn):
    card = _card(conn)
    assert json.loads(SC.status_card_json(card)) == card   # sort_keys 序列化后可还原


def test_missing_cycle_raises(conn):
    with pytest.raises(ValueError, match="cycle 不存在"):
        SC.build_status_card(conn, cycle_id="c999", policy=POLICY)
