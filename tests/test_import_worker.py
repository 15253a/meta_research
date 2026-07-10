"""CP5.5 · ImportWorker 外部 import 物化（§3.6.3 M4；OPEN #6 落地）。

核心验收（§7.1 M4）：imported 经本系统 harness 出 factory evidence、全链 provenance
（checkpoint.origin/external_import_id/manifest_hash/revision/license_review_id join 可达）；
失败路径负例全拒：scope 缺→不物化 / smoke 失败→不 target_ready / factory eval 失败→不 pool_publish。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

from orchestrator import database as db
from orchestrator.advancer import SqliteAdvancer
from orchestrator.compiler_sqlite import SqliteCompiler
from orchestrator.gate_pool import PoolGate
from orchestrator.gate_sqlite import open_gate_read_conn
from orchestrator.import_worker import ImportWorker
from orchestrator.importer import DeferredImporter
from orchestrator.statestore_sqlite import SQLiteStateStore
from orchestrator.writedaemon import WriteDaemon

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
POLICY = yaml.safe_load((SYSTEM_ROOT / "policies" / "policy.yaml").read_text(encoding="utf-8"))
OBS = POLICY["observation"]

SMOKE_OK = [sys.executable, "-c", "print('loss: 0.9'); print('smoke ok')"]
EVAL_OK = [sys.executable, "-c", "print('loss: 0.3'); print('metric_value: 1@1=0.88')"]


def _fetch_ok(cand):
    """确定性 fetch：内容由 uri+revision 决定（供应链闭包可复算）。"""
    return {"files": {"model.bin": f"weights@{cand['canonical_uri']}@{cand['revision']}".encode()},
            "smoke_cmd": SMOKE_OK, "eval_cmd": EVAL_OK,
            "protocol_id": 1, "protocol_ver": 1, "eval_key": "import-fac",
            "target_set_hash": "tsh-imp", "required": [[1, 1]], "env_hash": "imp-env"}


def _judge(daemon):
    def judge(cycle_id, bt_id, kind, subject_hash):
        from orchestrator.ids import cnum
        with daemon.transaction() as conn:
            rc = conn.execute("INSERT INTO runner_call(cycle_id,phase,purpose,status) VALUES (?,'audit',?,'success')",
                              (cnum(cycle_id), kind)).lastrowid
            conn.execute("INSERT INTO decision(cycle_id,actor,type,payload_json) VALUES (?,'judge',?,?)",
                         (cnum(cycle_id), kind, json.dumps({"build_target_id": bt_id, "review_kind": kind,
                                                            "round_no": 1, "verdict": "pass",
                                                            "subject_hash": subject_hash,
                                                            "runner_call_id": rc, "policy_hash": "ph"})))
    return judge


def _seed_deferred(daemon, state, *, scope='{"allow_eval": true, "allow_publish_pool": true}'):
    """走 M1c DeferredImporter 真三写入：goal/协议/问题 + candidate + license(allow, scope) + select_deferred。"""
    state.create_goal(text="import 研究目标", predicate_json={})
    with daemon.transaction() as conn:
        conn.execute("INSERT INTO protocol(id,version,name,scope_spec_json) VALUES (1,1,'proto','{}')")
        conn.execute("INSERT INTO metric_def(id,version,name,direction) VALUES (1,1,'acc','higher')")
        conn.execute("INSERT INTO protocol_metric(protocol_id,protocol_ver,metric_id,metric_ver) VALUES (1,1,1,1)")
        conn.execute("INSERT INTO cycle(id,goal_id,goal_ver,status,route,next_intent,policy_version) "
                     "VALUES (1,1,1,'done','attack','terminate','v0')")   # 研究轮已停机（worker 后循环不再开新轮）
        conn.execute("INSERT INTO question(id,goal_id,goal_ver,born_goal_ver,text,status,source) "
                     "VALUES (1,1,1,1,'需要外部 baseline','open','agent')")
    imp = DeferredImporter(daemon)
    cid = imp.register_candidate(question_id="q1", discovered_cycle="c1", trigger_kind="sota_reference",
                                 trigger_snapshot_hash="tsh", need_summary="need", source_kind="repo",
                                 canonical_uri="hub://model-x", search_snapshot_json="{}",
                                 search_snapshot_hash="ssh", rank=0, retrieved_at="t", revision="rev-abc")
    lic = imp.review_license(candidate_id=cid, decision="allow", license_scope_json=scope)
    r = imp.select_deferred(question_id="q1", candidate_id=cid, license_review_id=lic, action_cycle="c1",
                            candidate_set_hash="csh", selection_key="sk", policy_hash="ph",
                            license_decision_snapshot_hash="ldsh", placeholder_canonical_key="hub-model-x")
    return {"cid": cid, "lic": lic, **r}


def _mk_worker(path, work, fetch=_fetch_ok):
    daemon = WriteDaemon(db.connect(path))
    state = SQLiteStateStore(daemon, POLICY)
    pool = PoolGate(daemon, open_gate_read_conn(path))
    w = ImportWorker(state=state, pool_gate=pool, providers={"fetch": fetch, "judge": _judge(daemon)},
                     obs_policy=OBS, work_root=str(work))
    return daemon, state, w


@pytest.fixture()
def env(tmp_path):
    path = str(tmp_path / "research.sqlite")
    daemon, state, w = _mk_worker(path, tmp_path / "w")
    sel = _seed_deferred(daemon, state)
    return {"d": daemon, "s": state, "w": w, "sel": sel, "path": path, "tmp": tmp_path}


# ============ happy：全链 provenance + dep 解锁 ============
def test_materialize_full_chain(env):
    d, s, w, sel = env["d"], env["s"], env["w"], env["sel"]
    assert s.is_schedulable("q1") is False                          # 物化前被 pending dep 挡
    assert w.materialize_pending() != []
    # 占位 baseline → legal（入池）+ imported 事件
    assert d.query_one("SELECT status FROM baseline WHERE id=?", (sel["baseline_id"],))[0] == "legal"
    imp = d.query_one("SELECT baseline_id, manifest_hash FROM external_import WHERE action='imported'")
    assert imp[0] == sel["baseline_id"] and imp[1]
    # checkpoint 供应链 provenance 五件套 join 可达（§3.6.3 证据归属）
    ck = d.query_one("SELECT origin, source_uri, revision, manifest_hash FROM checkpoint "
                     "WHERE origin='external_import'")
    assert ck == ("external_import", "hub://model-x", "rev-abc", imp[1])
    # factory 证据经本系统 harness：evaluation source='factory' + metric + 观测
    assert d.query_one("SELECT source,status FROM evaluation")[0:2] == ("factory", "success")
    assert d.query_one("SELECT value FROM metric_result")[0] == 0.88
    assert d.query_one("SELECT count(*) FROM execution_observation WHERE source='parser'")[0] >= 1
    # run kind=import 审计
    assert d.query_one("SELECT kind,status FROM run")[0:2] == ("import", "success")
    # worker cycle：route NULL + 标记 + done + 不产 cycle_report（无此表，结构即证）
    wc = d.query_one("SELECT id,route,status FROM cycle WHERE id=(SELECT cycle_id FROM decision "
                     "WHERE type='import_worker_cycle')")
    assert wc[1] is None and wc[2] == "done"
    # dep 机械 satisfied → 问题回可调度（§4.2.1）
    assert d.query_one("SELECT status FROM question_dep WHERE question_id=1")[0] == "satisfied"
    assert s.is_schedulable("q1") is True
    # 幂等：再扫不重物化
    assert w.materialize_pending() == []


# ============ 失败路径负例全拒（§7.1 M4）============
def test_scope_missing_no_materialize(tmp_path):
    path = str(tmp_path / "r.sqlite")
    daemon, state, w = _mk_worker(path, tmp_path / "w")
    sel = _seed_deferred(daemon, state, scope='{"allow_eval": true}')   # 缺 allow_publish_pool
    w.materialize_pending()
    assert daemon.query_one("SELECT status FROM baseline WHERE id=?", (sel["baseline_id"],))[0] == "planned"  # 未动
    assert daemon.query_one("SELECT count(*) FROM external_import WHERE action='materialize_failed'")[0] == 1
    assert daemon.query_one("SELECT count(*) FROM run")[0] == 0                      # 未物化
    assert daemon.query_one("SELECT status FROM question_dep WHERE question_id=1")[0] == "pending"  # 仍锁
    assert state.is_schedulable("q1") is False


def test_smoke_fail_no_target_ready(tmp_path):
    path = str(tmp_path / "r.sqlite")
    fetch = lambda cand: {**_fetch_ok(cand), "smoke_cmd": [sys.executable, "-c", "import sys; sys.exit(3)"]}
    daemon, state, w = _mk_worker(path, tmp_path / "w", fetch=fetch)
    sel = _seed_deferred(daemon, state)
    w.materialize_pending()
    assert daemon.query_one("SELECT status,failure_kind FROM build_target")[:2] == ("failed", "smoke")
    assert daemon.query_one("SELECT count(*) FROM run")[0] == 0                      # 未 target_ready（无 run）
    assert daemon.query_one("SELECT status FROM baseline WHERE id=?", (sel["baseline_id"],))[0] == "build_failed"
    assert daemon.query_one("SELECT count(*) FROM external_import WHERE action='materialize_failed'")[0] == 1
    assert daemon.query_one("SELECT count(*) FROM external_import WHERE action='imported'")[0] == 0
    assert state.is_schedulable("q1") is False                                       # dep 仍 pending
    assert daemon.query_one("SELECT status FROM cycle ORDER BY id DESC LIMIT 1")[0] == "failed"   # worker failed


def test_eval_fail_no_pool_publish(tmp_path):
    path = str(tmp_path / "r.sqlite")
    fetch = lambda cand: {**_fetch_ok(cand), "eval_cmd": [sys.executable, "-c", "import sys; sys.exit(1)"]}
    daemon, state, w = _mk_worker(path, tmp_path / "w", fetch=fetch)
    sel = _seed_deferred(daemon, state)
    w.materialize_pending()
    assert daemon.query_one("SELECT count(*) FROM evaluation")[0] == 0               # 未注册测量
    assert daemon.query_one("SELECT status FROM baseline WHERE id=?", (sel["baseline_id"],))[0] == "build_failed"
    assert daemon.query_one("SELECT count(*) FROM external_import WHERE action='imported'")[0] == 0   # 不 pool_publish
    assert daemon.query_one("SELECT count(*) FROM external_import WHERE action='materialize_failed'")[0] == 1
    assert state.is_schedulable("q1") is False


def test_crash_before_resolve_deps_self_heals(tmp_path):
    """内审 BLOCKER 回归：崩在「worker 收尾 ↔ resolve_deps」缝隙 → atomic 合并后整体回滚（worker 仍在途）
    → 续跑补完 → dep satisfied、问题回可调度（不再有 done-但-dep-永锁 的静默楔死）。"""
    path = str(tmp_path / "r.sqlite")
    daemon, state, w = _mk_worker(path, tmp_path / "w")
    sel = _seed_deferred(daemon, state)
    orig = state.resolve_deps
    def crash_resolve():
        raise SystemExit("SIM-KILL9-before-resolve-deps")
    state.resolve_deps = crash_resolve
    with pytest.raises(SystemExit):
        w.materialize_pending()
    # atomic 回滚：worker 未 done（in-flight）——绝不出现 done+pending 的分裂态
    assert daemon.query_one("SELECT status FROM cycle ORDER BY id DESC LIMIT 1")[0] not in ("done", "failed")
    assert daemon.query_one("SELECT status FROM question_dep WHERE question_id=1")[0] == "pending"
    state.resolve_deps = orig
    w.materialize_pending()                                          # 续跑（settled 判不到 imported? imported 已记
    # 注：imported 事件在 register 段已落 → _already_settled 跳过 → 须经 resume 路（在途 worker）
    daemon.conn.close()
    daemon2, state2, w2 = _mk_worker(path, tmp_path / "w")
    cyc_row = daemon2.query_one("SELECT id, status FROM cycle ORDER BY id DESC LIMIT 1")
    if cyc_row[1] not in ("done", "failed"):                          # 仍在途 → resumer 收尾
        from types import SimpleNamespace
        w2.resume_cycle(SimpleNamespace(cycle_id=f"c{cyc_row[0]}"))
    assert daemon2.query_one("SELECT status FROM question_dep WHERE question_id=1")[0] == "satisfied"
    assert state2.is_schedulable("q1") is True
    daemon2.conn.close()


def test_unwired_worker_fails_loud(tmp_path):
    """内审 SHOULD 回归：在途 worker 轮 + advancer 未装配 import_worker → fail loud，
    **不得**被 _setup_cycle 误派研究 route（静默损坏）。"""
    path = str(tmp_path / "r.sqlite")
    daemon, state, w = _mk_worker(path, tmp_path / "w")
    _seed_deferred(daemon, state)
    w._run_and_register_import = lambda *a, **k: (_ for _ in ()).throw(SystemExit("SIM"))
    with pytest.raises(SystemExit):
        w.materialize_pending()
    daemon.conn.close()
    daemon2 = WriteDaemon(db.connect(path))
    state2 = SQLiteStateStore(daemon2, POLICY)
    compiler = SqliteCompiler(db.connect(path), POLICY)
    adv = SqliteAdvancer(state2, compiler, lambda c, p: None)        # 未装配 import_worker
    with pytest.raises(RuntimeError, match="import_worker 未装配"):
        adv.run_cycles(max_cycles=1)
    assert daemon2.query_one("SELECT route FROM cycle ORDER BY id DESC LIMIT 1")[0] is None   # 未被误派研究 route
    daemon2.conn.close()


def test_crash_between_fail_and_record_self_heals(tmp_path):
    """内审 SHOULD 回归：崩在「finish failed ↔ _record_failed」缝隙 → 续跑在终败短路处补记
    materialize_failed（settled），不再对 build_failed 占位重开 worker 重物化。"""
    path = str(tmp_path / "r.sqlite")
    fetch = lambda cand: {**_fetch_ok(cand), "smoke_cmd": [sys.executable, "-c", "import sys; sys.exit(3)"]}
    daemon, state, w = _mk_worker(path, tmp_path / "w", fetch=fetch)
    sel = _seed_deferred(daemon, state)
    orig_rec = w._record_failed
    box = {"n": 0}
    def crash_first_record(*a, **k):
        if box["n"] == 0:
            box["n"] = 1
            raise SystemExit("SIM-KILL9-before-record")
        return orig_rec(*a, **k)
    w._record_failed = crash_first_record
    with pytest.raises(SystemExit):
        w.materialize_pending()
    assert daemon.query_one("SELECT count(*) FROM external_import WHERE action='materialize_failed'")[0] == 0
    w._record_failed = orig_rec
    w.materialize_pending()                                          # 续跑：在途 worker → 终败短路补记
    assert daemon.query_one("SELECT count(*) FROM external_import WHERE action='materialize_failed'")[0] == 1
    assert daemon.query_one("SELECT count(*) FROM cycle WHERE status NOT IN ('done','failed','aborted')")[0] == 0
    assert w.materialize_pending() == []                             # settled → 不再重物化
    daemon.conn.close()


def test_judge_fail_settles_not_wedges(tmp_path):
    """codex BLOCKER 回归：judge FAIL → materialize_failed+target failed(review_failed)+worker failed
    （不楔死——原实现直接闯 gate 被拒后重启复用同 fail 裁决 = 确定性重试死循环）；再扫不重物化。"""
    path = str(tmp_path / "r.sqlite")
    daemon, state, w = _mk_worker(path, tmp_path / "w")
    sel = _seed_deferred(daemon, state)
    def fail_judge(cycle_id, bt_id, kind, subject_hash):
        from orchestrator.ids import cnum
        with daemon.transaction() as conn:
            rc = conn.execute("INSERT INTO runner_call(cycle_id,phase,purpose,status) VALUES (?,'audit',?,'success')",
                              (cnum(cycle_id), kind)).lastrowid
            conn.execute("INSERT INTO decision(cycle_id,actor,type,payload_json) VALUES (?,'judge',?,?)",
                         (cnum(cycle_id), kind, json.dumps({"build_target_id": bt_id, "review_kind": kind,
                                                            "round_no": 1, "verdict": "fail",
                                                            "subject_hash": subject_hash,
                                                            "runner_call_id": rc, "policy_hash": "ph"})))
    w.p["judge"] = fail_judge
    w.materialize_pending()
    assert daemon.query_one("SELECT status,failure_kind FROM build_target")[:2] == ("failed", "review_failed")
    assert daemon.query_one("SELECT count(*) FROM external_import WHERE action='materialize_failed'")[0] == 1
    assert daemon.query_one("SELECT count(*) FROM external_import WHERE action='imported'")[0] == 0
    assert daemon.query_one("SELECT status FROM cycle ORDER BY id DESC LIMIT 1")[0] == "failed"   # worker 收尾
    assert w.materialize_pending() == []                              # settled，不再重试
    daemon.conn.close()


# ============ 恢复：驱动循环识别在途 worker 轮 ============
def test_advancer_hands_inflight_worker_to_resumer(tmp_path):
    """崩在物化中途（worker 轮在途）→ 研究驱动循环不把它当研究轮 setup，而是交物化 resumer 续完。"""
    path = str(tmp_path / "r.sqlite")
    daemon, state, w = _mk_worker(path, tmp_path / "w")
    sel = _seed_deferred(daemon, state)
    orig = w._run_and_register_import
    def crash_mid(*a, **k):
        raise SystemExit("SIM-KILL9-mid-materialize")
    w._run_and_register_import = crash_mid
    with pytest.raises(SystemExit):
        w.materialize_pending()
    assert daemon.query_one("SELECT status FROM cycle ORDER BY id DESC LIMIT 1")[0] != "done"   # worker 在途
    daemon.conn.close()

    daemon2, state2, w2 = _mk_worker(path, tmp_path / "w")           # 重启：研究驱动循环入口
    compiler = SqliteCompiler(db.connect(path), POLICY)
    adv = SqliteAdvancer(state2, compiler, lambda c, p: None)
    adv.import_worker = w2
    adv.run_cycles(max_cycles=1)                                     # _resume_or_open 识别 worker → 续物化
    assert daemon2.query_one("SELECT status FROM baseline WHERE id=?", (sel["baseline_id"],))[0] == "legal"
    assert daemon2.query_one("SELECT count(*) FROM external_import WHERE action='imported'")[0] == 1
    assert state2.is_schedulable("q1") is True
    daemon2.conn.close()
