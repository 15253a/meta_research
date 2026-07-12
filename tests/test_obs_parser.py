"""CP5.3 · 真执行 harness + 确定性观测 parser + parser_result_suspect 真派生（§4.3.1/§4.7；OPEN #5）。

核心验收：真子进程 → staging log（原子改名）→ execution_log 入账 → parser 观测（P6 可回放）→
suspect 真派生挡复用（§4.1.5 selector）+ 挡关问（gate_close_question 证据拒）。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import conftest
from orchestrator import database as db
from orchestrator import harness as H
from orchestrator import obs_parser as OP
from orchestrator import recall_sqlite as R
from orchestrator.writedaemon import WriteDaemon

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
POLICY = yaml.safe_load((SYSTEM_ROOT / "policies" / "policy.yaml").read_text(encoding="utf-8"))
OBS = POLICY["observation"]

CLEAN_LOG = "loss: 1.0\nloss: 0.5\nwarning: lr decayed\nloss: 0.2\nwall_clock_sec: 12.5\n"
NAN_LOG = "loss: 1.0\nloss: nan\nloss: 0.9\n"
DIVERGE_LOG = "loss: 1.0\nloss: 2.0\nloss: 9.9\n"
OOM_LOG = "loss: 1.0\nCUDA out of memory\nretry\nloss: 0.9\n"


def _h(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode()).hexdigest()


# ============ parser 纯函数（确定性 + 字段口径）============
def test_parse_clean_log():
    f = OP.parse_log(CLEAN_LOG, OBS)
    assert f == {"nan_seen": 0, "divergence_flag": 0, "oom_count": 0, "warning_count": 1, "retry_count": 0,
                 "last_loss": 0.2, "loss_trend": "down", "wall_clock_sec": 12.5,
                 "parser_json": '{"n_loss_lines": 3}'}
    assert OP.derive_suspect(f, OBS) == 0


@pytest.mark.parametrize("log,field", [(NAN_LOG, "nan_seen"), (DIVERGE_LOG, "divergence_flag"), (OOM_LOG, "oom_count")])
def test_parse_suspect_paths(log, field):
    f = OP.parse_log(log, OBS)
    assert f[field] >= 1
    assert OP.derive_suspect(f, OBS) == 1


def test_parse_replay_deterministic():
    """P6：同 log + 同 policy → 逐字段一致（两次解析比对）。"""
    assert OP.parse_log(CLEAN_LOG, OBS) == OP.parse_log(CLEAN_LOG, OBS)


def test_extraction_policy_hash_stability():
    h1 = OP.extraction_policy_hash(OBS)
    assert h1 == OP.extraction_policy_hash(yaml.safe_load(yaml.safe_dump(OBS)))   # 序列化往返不变
    changed = {**OBS, "suspect": {**OBS["suspect"], "max_retry_count": 99}}
    assert OP.extraction_policy_hash(changed) != h1                               # 改任一阈值 → hash 变


def test_trend_flat_and_unknown():
    assert OP.parse_log("loss: 0.5\nloss: 0.5\nloss: 0.5\n", OBS)["loss_trend"] == "flat"
    assert OP.parse_log("loss: 0.5\n", OBS)["loss_trend"] == "unknown"
    assert OP.parse_log("", OBS)["loss_trend"] == "unknown"


# ============ harness：真子进程 + staging 原子改名 ============
def test_harness_real_subprocess_and_atomic_log(tmp_path):
    cmd = [sys.executable, "-c",
           "print('loss: 1.0'); print('loss: 0.5'); print('loss: 0.2'); print('wall_clock_sec: 1.0')"]
    r = H.run_staged(cmd, staging_dir=str(tmp_path / "st"), log_name="train.log", timeout_s=60)
    assert r["exit_code"] == 0
    p = Path(r["log_path"])
    assert p.name == "train.log" and p.exists()
    pointer = json.loads(Path(r["process_pointer_path"]).read_text())
    assert pointer["operation_id"] == r["process_receipt"]["operation_id"]
    assert r["process_receipt"]["context"]["log_name"] == "train.log"
    assert not p.with_name("train.log.partial").exists()          # 已原子改名，无残留半成品
    f = OP.parse_log(p.read_text(), OBS)
    assert f["loss_trend"] == "down" and OP.derive_suspect(f, OBS) == 0
    r2 = H.run_staged(cmd, staging_dir=str(tmp_path / "st2"), log_name="train.log", timeout_s=60)
    assert r2["log_sha256"] == r["log_sha256"]                    # 同命令同输出 → 同 content_hash


def test_harness_timeout_leaves_partial(tmp_path):
    with pytest.raises(subprocess.TimeoutExpired):
        H.run_staged([sys.executable, "-c", "import time; time.sleep(60)"],
                     staging_dir=str(tmp_path), log_name="hang.log", timeout_s=0.5)
    assert (tmp_path / "hang.log.partial").exists()               # 半成品留 .partial（可辨识丢弃）
    assert not (tmp_path / "hang.log").exists()                   # 绝不冒充完整 log
    receipts = list((tmp_path / ".execution-receipts").glob("execution-*.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text())
    assert receipt["outcome"] == "timeout" and receipt["group_drained"] is True
    pointer = json.loads((tmp_path / "hang.log.process.json").read_text())
    assert pointer["operation_id"] == receipt["operation_id"]
    assert receipt["context"]["log_name"] == "hang.log"


def test_harness_pointer_failure_preserves_timeout_authority(tmp_path, monkeypatch):
    """Pointer 是便利索引；写坏它不得覆盖 guardian 的 timeout+receipt 权威。"""
    original = H.atomic_write_receipt

    def fail_process_pointer(path, receipt):
        if str(path).endswith(".process.json"):
            raise OSError("pointer-fsync")
        return original(path, receipt)

    monkeypatch.setattr(H, "atomic_write_receipt", fail_process_pointer)
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        H.run_staged(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            staging_dir=str(tmp_path), log_name="hang.log", timeout_s=0.2)
    error = caught.value
    assert error.receipt["outcome"] == "timeout"
    assert error.receipt["group_drained"] is True
    assert str(error.process_pointer_error) == "pointer-fsync"
    assert (tmp_path / "hang.log.partial").exists()
    assert not (tmp_path / "hang.log").exists()
    assert not (tmp_path / "hang.log.exit").exists()
    assert not (tmp_path / "hang.log.process.json").exists()


def test_harness_success_pointer_failure_does_not_promote_partial(tmp_path, monkeypatch):
    original = H.atomic_write_receipt

    def fail_process_pointer(_path, _receipt):
        raise OSError("pointer-fsync")

    monkeypatch.setattr(H, "atomic_write_receipt", fail_process_pointer)
    with pytest.raises(OSError, match="pointer-fsync"):
        H.run_staged(
            [sys.executable, "-c", "print('complete')"],
            staging_dir=str(tmp_path), log_name="train.log", timeout_s=2)
    assert (tmp_path / "train.log.partial").exists()
    assert not (tmp_path / "train.log").exists()
    assert not (tmp_path / "train.log.exit").exists()

    # 模拟新 owner：中央 guardian receipt 已 terminal+drained，恢复 helper 只补 harness
    # 本地发布，不把 exit(0) 解释成 DB success。
    monkeypatch.setattr(H, "atomic_write_receipt", original)
    context = {
        "reconcile_protocol": "execution-owner-v1",
        "db_owner_kind": "evaluation_attempt",
        "db_owner_id": 42,
        "cycle_id": "c1",
        "build_target_id": 7,
        "phase": "eval",
    }
    # 前一次调用未带 owner context，故不得把那只 partial 猜配给 owner 42。
    with pytest.raises(H.ExecutionRecoveryError, match="无 exact guardian receipt"):
        H.recover_staged_result(
            staging_dir=str(tmp_path), log_name="train.log", execution_supervisor=None,
            execution_kind="harness", execution_context=context)


def test_harness_recovers_drained_exit_partial_for_exact_owner(tmp_path, monkeypatch):
    original = H.atomic_write_receipt
    context = {
        "reconcile_protocol": "execution-owner-v1",
        "db_owner_kind": "evaluation_attempt",
        "db_owner_id": 42,
        "cycle_id": "c1",
        "build_target_id": 7,
        "phase": "eval",
    }

    def fail_process_pointer(path, receipt):
        if str(path).endswith(".process.json"):
            raise OSError("owner-died-before-local-publish")
        return original(path, receipt)

    monkeypatch.setattr(H, "atomic_write_receipt", fail_process_pointer)
    with pytest.raises(OSError, match="owner-died-before-local-publish"):
        H.run_staged(
            [sys.executable, "-c", "print('metric_value: 1@1=0.9')"],
            staging_dir=str(tmp_path), log_name="eval.log", timeout_s=2,
            execution_kind="harness", execution_context=context)
    assert (tmp_path / "eval.log.partial").exists()

    monkeypatch.setattr(H, "atomic_write_receipt", original)
    recovered = H.recover_staged_result(
        staging_dir=str(tmp_path), log_name="eval.log", execution_supervisor=None,
        execution_kind="harness", execution_context=context)
    assert recovered is not None and recovered["exit_code"] == 0
    assert recovered["recovered_after_owner_loss"] is True
    assert (tmp_path / "eval.log").read_text().strip() == "metric_value: 1@1=0.9"
    assert (tmp_path / "eval.log.exit").read_text() == "0"
    assert not (tmp_path / "eval.log.partial").exists()


def test_harness_opt_in_revalidates_already_published_completed_log(tmp_path):
    context = {
        "reconcile_protocol": "qualification-final-v1",
        "db_owner_kind": "qualification_final_unit",
        "db_owner_id": 1,
        "phase": "qualification-final",
        "unit_id": "dreamer",
    }
    first = H.run_staged(
        [sys.executable, "-c", "print('complete')"],
        staging_dir=str(tmp_path), log_name="final.log", timeout_s=2,
        execution_kind="qualification-final", execution_context=context)
    assert H.recover_staged_result(
        staging_dir=str(tmp_path), log_name="final.log",
        execution_supervisor=None, execution_kind="qualification-final",
        execution_context=context) is None

    recovered = H.recover_staged_result(
        staging_dir=str(tmp_path), log_name="final.log",
        execution_supervisor=None, execution_kind="qualification-final",
        execution_context=context, recover_completed=True)
    assert recovered is not None and recovered["exit_code"] == 0
    assert recovered["log_sha256"] == first["log_sha256"]
    assert (tmp_path / "final.log").read_text().strip() == "complete"


def test_harness_opt_in_returns_exact_terminal_failure_without_retry(tmp_path):
    context = {
        "reconcile_protocol": "qualification-final-v1",
        "db_owner_kind": "qualification_final_unit",
        "db_owner_id": 1,
        "phase": "qualification-final",
        "unit_id": "dreamer",
    }
    with pytest.raises(subprocess.TimeoutExpired):
        H.run_staged(
            [sys.executable, "-c", "import time; time.sleep(2)"],
            staging_dir=str(tmp_path), log_name="final.log", timeout_s=0.05,
            execution_kind="qualification-final", execution_context=context)

    recovered = H.recover_staged_result(
        staging_dir=str(tmp_path), log_name="final.log",
        execution_supervisor=None, execution_kind="qualification-final",
        execution_context=context, recover_completed=True,
        return_terminal_failure=True)
    assert recovered is not None and recovered["terminal_failure"] is True
    assert recovered["failure_outcome"] == "timeout"
    assert recovered["exit_code"] == 125


def test_harness_exit_sidecar_write_all_handles_short_writes(tmp_path, monkeypatch):
    original_write = H.os.write

    def one_byte_write(fd, payload):
        return original_write(fd, memoryview(payload)[:1])

    monkeypatch.setattr(H.os, "write", one_byte_write)
    result = H.run_staged(
        [sys.executable, "-c", "raise SystemExit(17)"],
        staging_dir=str(tmp_path), log_name="nonzero.log", timeout_s=2)
    assert result["exit_code"] == 17
    assert (tmp_path / "nonzero.log.exit").read_text() == "17"


# ============ 入账 + 观测落库（幂等）============
def _env():
    daemon = WriteDaemon(db.connect(":memory:"))
    conftest.seed_minimal(daemon.conn)
    return daemon


def test_register_execution_log_idempotent():
    d = _env()
    eid1 = H.register_execution_log(d, cycle_id="c1", log_kind="train", ref="st/train.log",
                                    content_hash="h1", n_bytes=10, run_id=1)
    eid2 = H.register_execution_log(d, cycle_id="c1", log_kind="train", ref="st/train.log",
                                    content_hash="h1", n_bytes=10, run_id=1)
    assert eid1 == eid2
    assert d.query_one("SELECT count(*) FROM execution_log WHERE run_id=1")[0] == 1


def test_ingest_observation_idempotent_and_fields():
    d = _env()
    elid = H.register_execution_log(d, cycle_id="c1", log_kind="eval", ref="st/e.log",
                                    content_hash=_h(NAN_LOG), n_bytes=9, evaluation_attempt_id=1)
    o1 = OP.ingest_observation(d, execution_log_id=elid, log_bytes=NAN_LOG.encode(), obs_policy=OBS)
    o2 = OP.ingest_observation(d, execution_log_id=elid, log_bytes=NAN_LOG.encode(), obs_policy=OBS)
    assert o1 == o2                                               # 幂等（同 log/parser_version/policy_hash）
    row = d.query_one("SELECT source,nan_seen,loss_trend,parser_version,extraction_policy_hash "
                      "FROM execution_observation WHERE id=?", (o1,))
    assert row == ("parser", 1, "nan", OP.PARSER_VERSION, OP.extraction_policy_hash(OBS))


def test_suspect_multi_log_no_masking():
    """内审 BLOCKER 回归：attempt 有多 log（train nan + 其后入账的干净 stderr 观测）→ 跨 log OR，
    nan **不被最新干净行掩盖**（每 log 取最新行、任一存疑即 1）。"""
    d = _env()
    el_train = H.register_execution_log(d, cycle_id="c1", log_kind="eval", ref="st/t.log",
                                        content_hash=_h(NAN_LOG), n_bytes=1, evaluation_attempt_id=1)
    el_err = H.register_execution_log(d, cycle_id="c1", log_kind="stderr", ref="st/e.log",
                                      content_hash=_h(CLEAN_LOG), n_bytes=1, evaluation_attempt_id=1)
    OP.ingest_observation(d, execution_log_id=el_train, log_bytes=NAN_LOG.encode(), obs_policy=OBS)     # 先脏
    OP.ingest_observation(d, execution_log_id=el_err, log_bytes=CLEAN_LOG.encode(), obs_policy=OBS)     # 后净（id 更大）
    assert OP.suspect_for_attempt(d.conn, 1, OBS) == 1            # nan 不被掩盖


def test_divergence_only_for_positive_first_loss():
    """内审 SHOULD 回归：首 loss ≤0（log-likelihood 类）不判 divergence（乘法阈值语义反转/塌缩域）。"""
    assert OP.parse_log("loss: -5.0\nloss: -4.9\n", OBS)["divergence_flag"] == 0   # 健康负 loss 不误标
    assert OP.parse_log("loss: 0.0\nloss: 0.9\n", OBS)["divergence_flag"] == 0     # 零首 loss 不塌缩
    assert OP.parse_log("loss: 1.0\nloss: 9.9\n", OBS)["divergence_flag"] == 1     # 正域照判


def test_nonfinite_loss_and_oom_word_boundary():
    """内审 NIT 回归：±inf 亦置 nan_seen（非有限=退化）；oom 词边界（bloom 不误报）。"""
    assert OP.parse_log("loss: 1.0\nloss: -inf\n", OBS)["nan_seen"] == 1
    f = OP.parse_log("using bloom filter\nloss: 1.0\nloss: 0.5\n", OBS)
    assert f["oom_count"] == 0
    assert OP.parse_log("OOM killed\n", OBS)["oom_count"] == 1


def test_suspect_stale_policy_fails_closed():
    """codex BLOCKER 回归：观测行存在但非当前 (parser_version, extraction_policy_hash) → **stale≠clean**，
    返回 1（旧宽松 policy 下的行不得冒充当前口径干净；重 ingest 前挡复用/挡作证）。"""
    d = _env()
    elid = H.register_execution_log(d, cycle_id="c1", log_kind="eval", ref="st/s.log",
                                    content_hash=_h(CLEAN_LOG), n_bytes=1, evaluation_attempt_id=1)
    OP.ingest_observation(d, execution_log_id=elid, log_bytes=CLEAN_LOG.encode(), obs_policy=OBS)
    assert OP.suspect_for_attempt(d.conn, 1, OBS) == 0                    # 当前口径干净行 → 0
    tightened = {**OBS, "suspect": {**OBS["suspect"], "max_retry_count": 0}}
    assert OP.suspect_for_attempt(d.conn, 1, tightened) == 1              # policy 变 → 旧行 stale → fail closed
    OP.ingest_observation(d, execution_log_id=elid, log_bytes=CLEAN_LOG.encode(), obs_policy=tightened)
    assert OP.suspect_for_attempt(d.conn, 1, tightened) == 0              # 重 ingest 当前口径 → 恢复 0


def test_ingest_rejects_hash_mismatch():
    """codex SHOULD 回归：入参字节 hash ≠ execution_log.content_hash → ValueError（观测锚在登记 log 内容上）。"""
    d = _env()
    elid = H.register_execution_log(d, cycle_id="c1", log_kind="eval", ref="st/x.log",
                                    content_hash=_h(NAN_LOG), n_bytes=1, evaluation_attempt_id=1)
    with pytest.raises(ValueError, match="content_hash 不符"):
        OP.ingest_observation(d, execution_log_id=elid, log_bytes=CLEAN_LOG.encode(), obs_policy=OBS)


def test_wall_clock_nonfinite_normalized():
    """codex SHOULD 回归：wall_clock_sec 非有限（inf/nan）归 None（nan≠nan 破回放比对）。"""
    assert OP.parse_log("wall_clock_sec: inf\n", OBS)["wall_clock_sec"] is None
    assert OP.parse_log("wall_clock_sec: nan\n", OBS)["wall_clock_sec"] is None


def test_run_staged_rejects_stale_final(tmp_path):
    """codex NIT 回归：staging 已有同名 final → FileExistsError（防旧 final 冒充本次产物）。"""
    (tmp_path / "train.log").write_text("old")
    with pytest.raises(FileExistsError, match="log_name"):
        H.run_staged([sys.executable, "-c", "print(1)"], staging_dir=str(tmp_path),
                     log_name="train.log", timeout_s=30)


# ============ suspect 真派生：挡复用 + 挡关问 ============
def test_real_suspect_blocks_reuse():
    """§4.1.5 复用判定接真谓词：attempt 的 parser 观测存疑 → 复用 miss；干净 → hit（替 M2 恒 0 桩）。"""
    d = _env()
    conn = d.conn
    conn.executescript("""
    INSERT INTO variant(id,baseline_id,variant_key,config_json,status) VALUES (2,1,'v2','{}','legal');
    INSERT INTO evaluation(id,variant_id,protocol_id,protocol_ver,eval_key,source,status,created_cycle,target_set_hash) VALUES (2,2,1,1,'e2','factory','created',1,'h2');
    INSERT INTO evaluation_attempt(id,evaluation_id,cycle_id,attempt_no,purpose,status,env_hash) VALUES (2,2,1,1,'factory','success','eh1');
    INSERT INTO metric_result(id,evaluation_id,evaluation_attempt_id,metric_id,metric_ver,value,scope) VALUES (2,2,2,1,1,0.91,'aggregate');
    UPDATE evaluation SET status='success', canonical_attempt_id=2 WHERE id=2;
    """)
    conn.commit()
    elid = H.register_execution_log(d, cycle_id="c1", log_kind="eval", ref="st/e2.log",
                                    content_hash=_h(NAN_LOG), n_bytes=1, evaluation_attempt_id=2)
    OP.register_parser_suspect_real(conn, conn, OBS)              # 单测同连接即可（生产 gate 用独立观测连接）
    kw = dict(variant_id=2, protocol_id=1, protocol_ver=1, env_hash="eh1", required=[(1, 1)])
    assert R.reuse_selector(conn, **kw)["hit"] is True            # 无观测 → 无据不疑 → hit
    OP.ingest_observation(d, execution_log_id=elid, log_bytes=NAN_LOG.encode(), obs_policy=OBS)
    assert R.reuse_selector(conn, **kw)["hit"] is False           # nan 观测 → suspect=1 → 挡复用


def test_real_suspect_blocks_close_question(tmp_path):
    """§4.1.4 gate_close_question 接真谓词：证据 attempt 存疑 → GateReject（文件库 + 独立观测读连接）。"""
    from orchestrator.gate_sqlite import GateReject, SqliteGate, open_gate_read_conn
    from orchestrator.schemas import SchemaSet
    path = str(tmp_path / "research.sqlite")
    seed = db.connect(path)
    conftest.seed_minimal(seed)
    seed.executescript("INSERT INTO question(id,goal_id,goal_ver,born_goal_ver,text,status,source,active_cycle) "
                       "VALUES (2,1,1,1,'q2','active','agent',1); "
                       "UPDATE cycle SET active_question_id=2 WHERE id=1")
    seed.commit(); seed.close()
    daemon = WriteDaemon(db.connect(path))
    obs_conn = db.connect(path)                                    # 独立普通只读用连接（观测豁免仅经谓词）
    gate = SqliteGate(daemon, open_gate_read_conn(path), SchemaSet(SYSTEM_ROOT / "schemas"),
                      parser_suspect=lambda aid: OP.suspect_for_attempt(obs_conn, aid, OBS))
    ev = [{"kind": "evaluation", "metric_result_id": "mr1", "claim_md": "c"}]
    elid = H.register_execution_log(daemon, cycle_id="c1", log_kind="eval", ref="st/a1.log",
                                    content_hash=_h(NAN_LOG), n_bytes=1, evaluation_attempt_id=1)
    OP.ingest_observation(daemon, execution_log_id=elid, log_bytes=NAN_LOG.encode(), obs_policy=OBS)
    with pytest.raises(GateReject, match="parser_result_suspect"):
        gate.gate_close_question(cycle_id="c1", question_id="q2", verdict="answered",
                                 evidence=ev, answer_md="以存疑测量关问")
