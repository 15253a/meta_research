"""CP5.6 · §7.1 M4「语义判据 5 判例确定归属」——显式命名逐一断言（M4 步级验证核心）。

判例（第三部分 §7.1 M4 行）：
① 自建 baseline ② import factory baseline ③ 复用命中零重训 ④ 训练失败入账不入树 ⑤ log suspect 不成证据。
另附：证据可回溯到一次真实 evaluation（① 内显式 join 链断言）。

各判例独立起真 SQLite + 真子进程场景（复用 test_attack_advance / test_import_worker 的环境构造器——
它们的细粒度回归已各自覆盖崩溃缝隙，此处是**验收归属**的端到端命名断言）。
M4 行其余条款的落点：import 失败路径负例三条 = test_import_worker（license scope/smoke/eval 全拒）；
provenance 五件套 join = test_import_worker.test_materialize_full_chain——步级验证按两文件联合勾兑。
注：跨测试模块 import 依赖 pytest 默认 importmode=prepend 且 tests/ 无 __init__.py（改动须同步这里）。
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import test_attack_advance as TA
import test_import_worker as TW
from orchestrator import database as db
from orchestrator import obs_parser as OP
from orchestrator import recall_sqlite as R
from orchestrator.advancer import SqliteAdvancer

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
POLICY = yaml.safe_load((SYSTEM_ROOT / "policies" / "policy.yaml").read_text(encoding="utf-8"))
OBS = POLICY["observation"]


def _run_full_attack(tmp_path):
    path = str(tmp_path / "research.sqlite")
    daemon, state, compiler, attack = TA._mk_env(path, tmp_path / "w")
    TA._bootstrap_attack(state)
    SqliteAdvancer(state, compiler, lambda c, p: None, attack=attack).run_cycles(max_cycles=4)
    return path, daemon, state


# ============ 判例①：自建 baseline（variant identity + smoke 过 + metrics 声明）============
def test_case1_self_built_baseline(tmp_path):
    path, d, s = _run_full_attack(tmp_path)
    assert d.query_one("SELECT status, provenance FROM baseline WHERE canonical_key='ck-attack'") == ("legal", "self_built")
    assert d.query_one("SELECT identity_doc FROM baseline WHERE canonical_key='ck-attack'")[0].strip()  # identity 落库
    assert list((tmp_path / "w").rglob("smoke-*.log"))                                # smoke 真跑过（transcript 在）
    assert d.query_one("SELECT count(*) FROM protocol_metric WHERE protocol_id=1 AND metric_id=1")[0] == 1  # metrics 声明
    # 证据可回溯到一次真实 evaluation：answer→evidence→metric_result→attempt→evaluation(success) 全链 join，
    # 并把 **protocol_metric 声明**纳入同链（codex SHOULD：证被引用的测量确属该协议声明的 metric，非残留声明行）；
    # 锚定到本判例的 baseline（codex NIT：防 helper 扩展后误命中首行）
    chain = d.query_one(
        "SELECT q.status, e.valid, mr.value, ea.status, ev.status, ev.source FROM answer a "
        "JOIN question q ON q.id=a.question_id "
        "JOIN evidence e ON e.answer_id=a.id AND e.kind='evaluation' "
        "JOIN metric_result mr ON mr.id=e.metric_result_id "
        "JOIN evaluation_attempt ea ON ea.id=mr.evaluation_attempt_id "
        "JOIN evaluation ev ON ev.id=mr.evaluation_id "
        "JOIN protocol_metric pm ON pm.protocol_id=ev.protocol_id AND pm.protocol_ver=ev.protocol_ver "
        "  AND pm.metric_id=mr.metric_id AND pm.metric_ver=mr.metric_ver "
        "JOIN variant v ON v.id=ev.variant_id "
        "JOIN baseline b ON b.id=v.baseline_id AND b.canonical_key='ck-attack'")
    assert chain == ("answered", 1, 0.93, "success", "success", "factory")
    d.conn.close()


# ============ 判例②：import factory baseline（origin + manifest_hash 记录）============
def test_case2_import_factory_baseline(tmp_path):
    path = str(tmp_path / "r.sqlite")
    daemon, state, w = TW._mk_worker(path, tmp_path / "w")
    TW._seed_deferred(daemon, state)
    w.materialize_pending()
    ck = daemon.query_one("SELECT origin, manifest_hash FROM checkpoint WHERE origin='external_import'")
    imp = daemon.query_one("SELECT manifest_hash FROM external_import WHERE action='imported'")
    assert ck[0] == "external_import" and ck[1] == imp[0]            # checkpoint.origin + manifest_hash 双记录且互链
    # 「经本系统 harness 出 factory evidence」——source 字面之外附**真执行指纹**（内审 SHOULD：source 是
    # 注册期字面，须以真子进程产物自证）：metric 值来自真 eval 子进程输出 + parser 观测在 + run kind=import
    assert daemon.query_one("SELECT source FROM evaluation")[0] == "factory"
    assert daemon.query_one("SELECT value FROM metric_result")[0] == 0.88
    assert daemon.query_one("SELECT count(*) FROM execution_observation WHERE source='parser'")[0] >= 1
    assert daemon.query_one("SELECT kind, status FROM run ORDER BY id DESC LIMIT 1")[0:2] == ("import", "success")  # 最近 run 锚定（NIT）
    daemon.conn.close()


# ============ 判例③：复用命中零重训（metric_result 在 + env 匹配 + suspect=0 → hit）============
def test_case3_reuse_hit_zero_retrain(tmp_path):
    path, d, s = _run_full_attack(tmp_path)
    before = {t: d.query_one(f"SELECT count(*) FROM {t}")[0] for t in ("run", "evaluation", "evaluation_attempt")}
    mr_registered = d.query_one("SELECT id FROM metric_result WHERE scope='aggregate' ORDER BY id DESC LIMIT 1")[0]
    vid, recorded_env_hash = d.query_one(
        "SELECT ev.variant_id,ea.env_hash FROM evaluation ev "
        "JOIN evaluation_attempt ea ON ea.evaluation_id=ev.id "
        "WHERE ev.source='factory' AND ea.status='success'")
    assert recorded_env_hash.startswith("sha256:")
    OP.register_parser_suspect_real(d.conn, d.conn, OBS)             # 真谓词（真执行数据上复用判定的前提）
    r = R.reuse_selector(d.conn, variant_id=vid, protocol_id=1, protocol_ver=1,
                         env_hash=recorded_env_hash, required=[(1, 1)])
    assert r["hit"] is True                                          # 命中：同格子成功测量 + env 精确 + 非存疑
    assert r["results"][0]["value"] == 0.93
    assert r["results"][0]["metric_result_id"] == mr_registered      # 命中即**既有**测量（零执行引用历史，§4.1.5）
    # 「零重训」= 满足需求不需任何新 run/evaluation/attempt 行（selector 只读；编排器级 reuse_only 跳过
    # 路由 = plan 特化，M6 接——内审 SHOULD：此处按可证面收窄断言口径并注明）
    after = {t: d.query_one(f"SELECT count(*) FROM {t}")[0] for t in ("run", "evaluation", "evaluation_attempt")}
    assert after == before
    miss = R.reuse_selector(d.conn, variant_id=vid, protocol_id=1, protocol_ver=1,
                            env_hash="other-env", required=[(1, 1)])
    assert miss["hit"] is False                                      # env 不匹配 → 不复用（负向对照）
    d.conn.close()


# ============ 判例④：训练失败入账不入树 ============
def test_case4_failure_accounted_not_in_tree(tmp_path):
    path = str(tmp_path / "research.sqlite")
    daemon, state, compiler, attack = TA._mk_env(path, tmp_path / "w",
                                                 train_body="import sys; print('loss: 1.0'); sys.exit(1)")
    TA._bootstrap_attack(state)
    attack.p["reasoning"] = lambda cyc, pack: {
        "selection.json": {"next_question_id": None, "next_intent": "terminate", "scores": []}}
    SqliteAdvancer(state, compiler, lambda c, p: None, attack=attack).run_cycles(max_cycles=4)
    assert daemon.query_one("SELECT status, failure_kind FROM run")[:2] == ("failed", "runtime")   # 入账
    for table in ("evidence", "answer", "metric_result"):                                          # 不入树
        assert daemon.query_one(f"SELECT count(*) FROM {table}")[0] == 0
    daemon.conn.close()


# ============ 判例⑤：log suspect 不成证据（挡复用 + 挡关问，不支持结论）============
def test_case5_suspect_not_evidence(tmp_path):
    """真执行全链后，对其 attempt 旁路 ingest 一份 nan 观测（模拟脏 log 被 parser 派生标疑）→
    ① 复用判定 miss ② gate_close_question 拒该 attempt 作证据——suspect 只负向过滤、绝不支持结论。"""
    import conftest
    from orchestrator import harness as H
    from orchestrator.gate_sqlite import GateReject, SqliteGate, open_gate_read_conn
    from orchestrator.schemas import SchemaSet
    from orchestrator.writedaemon import WriteDaemon
    path = str(tmp_path / "research.sqlite")
    seed = db.connect(path)
    conftest.seed_minimal(seed)
    seed.executescript(
        "INSERT INTO question(id,goal_id,goal_ver,born_goal_ver,text,status,source,active_cycle) "
        "VALUES (2,1,1,1,'q2','active','agent',1); "
        "UPDATE cycle SET active_question_id=2 WHERE id=1")
    seed.commit(); seed.close()
    daemon = WriteDaemon(db.connect(path))
    conn = daemon.conn
    # 可复用格子（variant2：success eval + success attempt(env eh1) + aggregate mr）——先证 hit，再证被 suspect 挡
    conn.executescript("""
    INSERT INTO variant(id,baseline_id,variant_key,config_json,status) VALUES (2,1,'v2','{}','legal');
    INSERT INTO evaluation(id,variant_id,protocol_id,protocol_ver,eval_key,source,status,created_cycle,target_set_hash) VALUES (2,2,1,1,'e2','factory','created',1,'h2');
    INSERT INTO evaluation_attempt(id,evaluation_id,cycle_id,attempt_no,purpose,status,env_hash) VALUES (2,2,1,1,'factory','success','eh1');
    INSERT INTO metric_result(id,evaluation_id,evaluation_attempt_id,metric_id,metric_ver,value,scope) VALUES (2,2,2,1,1,0.91,'aggregate');
    UPDATE evaluation SET status='success', canonical_attempt_id=2 WHERE id=2;
    """)
    conn.commit()
    OP.register_parser_suspect_real(conn, conn, OBS)
    kw = dict(variant_id=2, protocol_id=1, protocol_ver=1, env_hash="eh1", required=[(1, 1)])
    assert R.reuse_selector(conn, **kw)["hit"] is True               # 前置：无观测 → 可复用
    nan_log = b"loss: 1.0\nloss: nan\n"
    import hashlib
    elid = H.register_execution_log(daemon, cycle_id="c1", log_kind="eval", ref="st/a2.log",
                                    content_hash=hashlib.sha256(nan_log).hexdigest(), n_bytes=len(nan_log),
                                    evaluation_attempt_id=2)
    OP.ingest_observation(daemon, execution_log_id=elid, log_bytes=nan_log, obs_policy=OBS)
    assert R.reuse_selector(conn, **kw)["hit"] is False              # ① 挡复用（同格子由 hit 翻 miss，归因 suspect）
    nan2 = b"loss: 2.0\nloss: nan\n"
    elid1 = H.register_execution_log(daemon, cycle_id="c1", log_kind="eval", ref="st/a1.log",
                                     content_hash=hashlib.sha256(nan2).hexdigest(), n_bytes=len(nan2),
                                     evaluation_attempt_id=1)       # seed attempt1（被 evidence 引用面）也标疑
    OP.ingest_observation(daemon, execution_log_id=elid1, log_bytes=nan2, obs_policy=OBS)
    obs_conn = db.connect(path)
    gate_read = open_gate_read_conn(path)
    gate = SqliteGate(daemon, gate_read, SchemaSet(SYSTEM_ROOT / "schemas"),
                      parser_suspect=lambda aid: OP.suspect_for_attempt(obs_conn, aid, OBS))
    with pytest.raises(GateReject, match="parser_result_suspect"):             # ② 挡关问
        gate.gate_close_question(cycle_id="c1", question_id="q2", verdict="answered",
                                 evidence=[{"kind": "evaluation", "metric_result_id": "mr1", "claim_md": "c"}],
                                 answer_md="以存疑测量关问")
    obs_conn.close(); gate_read.close(); daemon.conn.close()
