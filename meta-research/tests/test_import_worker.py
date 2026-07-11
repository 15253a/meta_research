"""CP5.5 · ImportWorker 外部 import 物化（§3.6.3 M4；OPEN #6 落地）。

核心验收（§7.1 M4）：imported 经本系统 harness 出 factory evidence、全链 provenance
（checkpoint.origin/external_import_id/manifest_hash/revision/license_review_id join 可达）；
失败路径负例全拒：scope 缺→不物化 / smoke 失败→不 target_ready / factory eval 失败→不 pool_publish。
"""
from __future__ import annotations

import json
import hashlib
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from orchestrator import database as db
from orchestrator import harness as H
from orchestrator.advancer import SqliteAdvancer
from orchestrator.compiler_sqlite import SqliteCompiler
from orchestrator.gate_pool import PoolGate
from orchestrator.gate_sqlite import open_gate_read_conn
from orchestrator.import_worker import ImportWorker
from orchestrator.import_fetcher import FrozenCandidateFetcher
from orchestrator.execution_sandbox import (
    DockerExecutionSandbox,
    ExecutionSandboxError,
    SandboxOutputError,
)
from orchestrator.importer import DeferredImporter
from orchestrator.instance_lease import InstanceLease
from orchestrator.process_supervisor import ExecutionSupervisor
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


def _frozen_snapshot(*, digest=None, env_hash=None, portable=False):
    payload = b"frozen-weights"
    environment_hash = env_hash or "sha256:" + "3" * 64
    interpreter = "python" if portable else sys.executable
    return json.dumps({"materialization": {
        "version": 1,
        "files": [{
            "path": "model.bin", "encoding": "utf-8", "data": payload.decode(),
            "sha256": digest or "sha256:" + hashlib.sha256(payload).hexdigest(),
        }],
        "smoke_argv": [interpreter, "-c", "print('smoke ok')"],
        "eval_argv": [interpreter, "-c", "print('metric_value: 1@1=0.88')"],
        "protocol_id": 1, "protocol_ver": 1, "eval_key": "import-fac",
        "target_set_hash": "tsh-imp", "required": [[1, 1]],
        "artifact_relpath": "model.bin", "artifact_type": "external_model",
        "env_hash": environment_hash,
        "supply_chain": {
            "dependency_lock_hash": "sha256:" + "1" * 64,
            "harness_adapter_hash": "sha256:" + "2" * 64,
            "environment_hash": environment_hash,
            "network_isolation": True,
        },
    }}, sort_keys=True)


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


def _seed_deferred(daemon, state, *, scope='{"allow_eval": true, "allow_publish_pool": true}',
                   search_snapshot_json="{}", search_snapshot_hash=None):
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
    if search_snapshot_hash is None:
        search_snapshot_hash = (
            "sha256:" + hashlib.sha256(search_snapshot_json.encode("utf-8")).hexdigest())
    cid = imp.register_candidate(question_id="q1", discovered_cycle="c1", trigger_kind="sota_reference",
                                 trigger_snapshot_hash="tsh", need_summary="need", source_kind="repo",
                                 canonical_uri="hub://model-x",
                                 search_snapshot_json=search_snapshot_json,
                                 search_snapshot_hash=search_snapshot_hash,
                                 rank=0, retrieved_at="t", revision="rev-abc")
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


def test_owner_guard_requires_shared_execution_supervisor(env):
    """Leased import 只接受同 owner guard 且持 delegated fence 的 supervisor。"""
    with pytest.raises(ValueError, match="ExecutionSupervisor"):
        ImportWorker(
            state=env["s"], pool_gate=env["w"].gate,
            providers=env["w"].p, obs_policy=OBS,
            work_root=str(env["tmp"] / "guarded-import"),
            owner_guard=lambda: None)

    guard = lambda: None
    standalone = ExecutionSupervisor.standalone(env["tmp"] / "standalone-receipts")
    try:
        with pytest.raises(ValueError, match="delegated instance fence"):
            ImportWorker(
                state=env["s"], pool_gate=env["w"].gate,
                providers=env["w"].p, obs_policy=OBS,
                work_root=str(env["tmp"] / "standalone-import"),
                owner_guard=guard, execution_supervisor=standalone)
    finally:
        standalone.close()

    lease_a = InstanceLease.acquire(
        env["tmp"] / "lease-a", heartbeat_interval_s=0.05)
    lease_b = InstanceLease.acquire(
        env["tmp"] / "lease-b", heartbeat_interval_s=0.05)
    supervisor_a = ExecutionSupervisor(
        receipt_dir=env["tmp"] / "lease-a" / "state" / "executions",
        owner_id=lease_a.owner_id, owner_guard=lease_a.assert_owned,
        fence_context_factory=lease_a.delegate_owner_fence)
    supervisor_b = ExecutionSupervisor(
        receipt_dir=env["tmp"] / "lease-b" / "state" / "executions",
        owner_id=lease_b.owner_id, owner_guard=lease_b.assert_owned,
        fence_context_factory=lease_b.delegate_owner_fence)
    try:
        worker = ImportWorker(
            state=env["s"], pool_gate=env["w"].gate,
            providers=env["w"].p, obs_policy=OBS,
            work_root=str(env["tmp"] / "same-owner-import"),
            owner_guard=lease_a.assert_owned,
            execution_supervisor=supervisor_a)
        assert worker.execution_supervisor is supervisor_a
        with pytest.raises(ValueError, match="同一 owner guard"):
            ImportWorker(
                state=env["s"], pool_gate=env["w"].gate,
                providers=env["w"].p, obs_policy=OBS,
                work_root=str(env["tmp"] / "foreign-owner-import"),
                owner_guard=lease_a.assert_owned,
                execution_supervisor=supervisor_b)
    finally:
        supervisor_a.close()
        supervisor_b.close()
        assert lease_a.close() is None
        assert lease_b.close() is None


# ============ happy：全链 provenance + dep 解锁 ============
def test_default_frozen_candidate_fetcher_validates_content_and_adapter():
    snapshot = _frozen_snapshot()
    spec = FrozenCandidateFetcher()({
        "revision": "a" * 40, "search_snapshot_json": snapshot,
        "search_snapshot_hash": "sha256:" + hashlib.sha256(snapshot.encode()).hexdigest()})
    assert spec["files"] == {"model.bin": b"frozen-weights"}
    assert spec["artifact_relpath"] == "model.bin"
    assert spec["required"] == [[1, 1]]
    assert spec["requires_adversarial_sandbox"] is True
    with pytest.raises(ValueError, match="sha256"):
        bad_snapshot = _frozen_snapshot(digest="sha256:" + "0" * 64)
        FrozenCandidateFetcher()({
            "revision": "a" * 40,
            "search_snapshot_json": bad_snapshot,
            "search_snapshot_hash": "sha256:" + hashlib.sha256(bad_snapshot.encode()).hexdigest()})
    with pytest.raises(ValueError, match="search_snapshot_hash"):
        FrozenCandidateFetcher()({
            "revision": "a" * 40, "search_snapshot_json": snapshot,
            "search_snapshot_hash": "sha256:" + "0" * 64})


def test_default_frozen_materialization_refuses_host_execution_without_sandbox(tmp_path):
    path = str(tmp_path / "r.sqlite")
    snapshot = _frozen_snapshot()
    snapshot_hash = "sha256:" + hashlib.sha256(snapshot.encode()).hexdigest()
    daemon, state, worker = _mk_worker(
        path, tmp_path / "w", fetch=FrozenCandidateFetcher())
    _seed_deferred(
        daemon, state, search_snapshot_json=snapshot,
        search_snapshot_hash=snapshot_hash)

    worker.materialize_pending(max_items=1)

    assert daemon.query_one("SELECT count(*) FROM run")[0] == 0
    assert daemon.query_one("SELECT count(*) FROM build_target")[0] == 0
    reason = daemon.query_one(
        "SELECT json_extract(reason_json,'$.reason') FROM external_import "
        "WHERE action='materialize_failed'")[0]
    assert "adversarial sandbox" in reason
    assert daemon.query_one("SELECT status FROM question_dep")[0] == "blocked"


def test_default_frozen_materialization_runs_only_in_pinned_sandbox(tmp_path):
    work = tmp_path / "w"
    (work / "state").mkdir(parents=True)
    sandbox = DockerExecutionSandbox(
        work_root=work, config=POLICY["execution"]["sandbox"])
    try:
        sandbox.preflight()
    except (ExecutionSandboxError, OSError, subprocess.SubprocessError) as error:
        pytest.skip(f"pinned local Docker sandbox unavailable: {error}")
    supervisor = ExecutionSupervisor.standalone(work / "state" / "executions")
    path = str(tmp_path / "r.sqlite")
    daemon = WriteDaemon(db.connect(path))
    state = SQLiteStateStore(daemon, POLICY)
    pool = PoolGate(daemon, open_gate_read_conn(path))
    worker = ImportWorker(
        state=state, pool_gate=pool,
        providers={"fetch": FrozenCandidateFetcher(), "judge": _judge(daemon)},
        obs_policy=OBS, work_root=str(work),
        execution_supervisor=supervisor, execution_sandbox=sandbox)
    snapshot = _frozen_snapshot(
        env_hash=sandbox.environment_hash, portable=True)
    _seed_deferred(
        daemon, state, search_snapshot_json=snapshot,
        search_snapshot_hash="sha256:" + hashlib.sha256(snapshot.encode()).hexdigest())
    try:
        assert worker.materialize_pending(max_items=1)
        assert daemon.query_one("SELECT status FROM baseline")[0] == "legal"
        assert daemon.query_one("SELECT status FROM evaluation")[0] == "success"
        receipts = [json.loads(path.read_text()) for path in
                    (work / "state" / "executions").glob("execution-*.json")]
        sandboxed = [receipt for receipt in receipts
                     if receipt.get("containment") == "docker-container-v1"]
        assert len(sandboxed) == 2                 # smoke + factory eval
        assert all(receipt["sandbox"]["container_drained"] is True
                   for receipt in sandboxed)
    finally:
        supervisor.close()


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


def test_advancer_materializes_queue_then_reopens_satisfied_dependency(env):
    d, s, w = env["d"], env["s"], env["w"]
    with d.transaction() as conn:
        conn.execute(
            "UPDATE cycle SET route='dependency_wait',next_intent=NULL,next_question_id=NULL WHERE id=1")
    adv = SqliteAdvancer(
        s, SqliteCompiler(db.connect(env["path"]), POLICY), lambda *_args: {},
        attack=object(), import_worker=w)

    resumed = adv._resume_or_open()

    assert resumed is not None and resumed.route == "attack" and resumed.question_id == "q1"
    assert d.query_one("SELECT status FROM baseline WHERE id=?", (env["sel"]["baseline_id"],))[0] == "legal"
    assert d.query_one("SELECT status FROM question_dep WHERE question_id=1")[0] == "satisfied"
    assert d.query_one(
        "SELECT count(*) FROM decision WHERE type='import_worker_cycle'")[0] == 1


def test_import_eval_attempt_exists_before_process_spawn(tmp_path, monkeypatch):
    """import eval 的 guardian owner 在放行子进程前已是 exact running attempt。"""
    path = str(tmp_path / "r.sqlite")
    daemon, _state, worker = _mk_worker(path, tmp_path / "w")
    _seed_deferred(daemon, worker.state)
    original = H.run_staged
    observed = []

    def wrapped(cmd, **kwargs):
        if kwargs.get("execution_kind") == "import-eval":
            row = daemon.query_one(
                "SELECT id,status,build_target_id FROM evaluation_attempt "
                "ORDER BY id DESC LIMIT 1")
            assert row is not None and row[1] == "running"
            context = kwargs["execution_context"]
            assert context["reconcile_protocol"] == "execution-owner-v1"
            assert context["db_owner_kind"] == "evaluation_attempt"
            assert context["db_owner_id"] == row[0]
            assert context["build_target_id"] == row[2]
            observed.append(row[0])
        return original(cmd, **kwargs)

    monkeypatch.setattr(H, "run_staged", wrapped)
    worker.materialize_pending()
    assert len(observed) == 1
    assert daemon.query_one(
        "SELECT status FROM evaluation_attempt WHERE id=?", (observed[0],))[0] == "success"


# ============ 失败路径负例全拒（§7.1 M4）============
def test_fetch_infrastructure_error_is_not_recorded_as_candidate_failure(tmp_path):
    path = str(tmp_path / "r.sqlite")

    def infrastructure_failure(_candidate):
        raise RuntimeError("search connector temporarily unavailable")

    daemon, state, worker = _mk_worker(
        path, tmp_path / "w", fetch=infrastructure_failure)
    _seed_deferred(daemon, state)

    with pytest.raises(RuntimeError, match="temporarily unavailable"):
        worker.materialize_pending(max_items=1)

    assert daemon.query_one(
        "SELECT count(*) FROM external_import WHERE action='materialize_failed'")[0] == 0
    assert daemon.query_one("SELECT status FROM question_dep")[0] == "pending"
    inflight = state.inflight_cycle()
    assert inflight is not None and inflight.route is None


def test_fetch_failure_outcome_recovery_does_not_refetch(tmp_path, monkeypatch):
    path = str(tmp_path / "r.sqlite")
    calls = []

    def broken_fetch(_candidate):
        calls.append(True)
        raise ValueError("bad frozen snapshot")

    daemon, state, worker = _mk_worker(path, tmp_path / "w", fetch=broken_fetch)
    selected = _seed_deferred(daemon, state)
    original_done = state.mark_cycle_done

    def crash_before_worker_terminal(cycle_id, status="done"):
        if cycle_id != "c1":
            raise RuntimeError("crash-after-materialize-failed-event")
        return original_done(cycle_id, status)

    monkeypatch.setattr(state, "mark_cycle_done", crash_before_worker_terminal)
    with pytest.raises(RuntimeError, match="crash-after-materialize-failed-event"):
        worker.materialize_pending(max_items=1)
    assert len(calls) == 1
    assert daemon.query_one(
        "SELECT count(*) FROM external_import WHERE action='materialize_failed'")[0] == 1
    assert daemon.query_one(
        "SELECT status FROM baseline WHERE id=?", (selected["baseline_id"],))[0] == "build_failed"
    assert daemon.query_one("SELECT status FROM question_dep")[0] == "blocked"

    monkeypatch.setattr(state, "mark_cycle_done", original_done)
    worker.resume_cycle(state.inflight_cycle())
    assert len(calls) == 1
    assert daemon.query_one("SELECT status FROM cycle ORDER BY id DESC LIMIT 1")[0] == "failed"
    assert daemon.query_one(
        "SELECT status FROM baseline WHERE id=?", (selected["baseline_id"],))[0] == "build_failed"


def test_advancer_reopens_question_after_terminal_import_failure(tmp_path):
    path = str(tmp_path / "r.sqlite")

    def broken_fetch(_candidate):
        raise ValueError("frozen materialization unavailable")

    daemon, state, worker = _mk_worker(path, tmp_path / "w", fetch=broken_fetch)
    _seed_deferred(daemon, state)
    with daemon.transaction() as conn:
        conn.execute(
            "UPDATE cycle SET route='dependency_wait',next_intent=NULL,next_question_id=NULL WHERE id=1")
    adv = SqliteAdvancer(
        state, SqliteCompiler(db.connect(path), POLICY), lambda *_args: {},
        attack=object(), import_worker=worker)

    resumed = adv._resume_or_open()

    assert resumed.route == "attack" and resumed.question_id == "q1"
    assert daemon.query_one("SELECT status FROM question_dep")[0] == "blocked"
    assert daemon.query_one(
        "SELECT count(*) FROM decision WHERE type='import_materialization_blocked'")[0] == 1
    plan_pack = adv.compiler.render(cycle_id=resumed.cycle_id, stage="plan")
    assert "external import 物化失败" in plan_pack.anchor_md
    assert "frozen materialization unavailable" in plan_pack.anchor_md


def test_imported_outcome_recovery_finishes_target_without_refetch(tmp_path, monkeypatch):
    path = str(tmp_path / "r.sqlite")
    calls = []

    def counted_fetch(candidate):
        calls.append(True)
        return _fetch_ok(candidate)

    daemon, state, worker = _mk_worker(path, tmp_path / "w", fetch=counted_fetch)
    _seed_deferred(daemon, state)
    original_record = worker._record_imported

    def crash_after_imported(*args, **kwargs):
        original_record(*args, **kwargs)
        raise RuntimeError("crash-after-imported-event")

    monkeypatch.setattr(worker, "_record_imported", crash_after_imported)
    with pytest.raises(RuntimeError, match="crash-after-imported-event"):
        worker.materialize_pending(max_items=1)
    assert len(calls) == 1
    assert daemon.query_one(
        "SELECT count(*) FROM external_import WHERE action='imported'")[0] == 1
    assert daemon.query_one("SELECT status FROM build_target")[0] == "running"

    monkeypatch.setattr(worker, "_record_imported", original_record)
    worker.resume_cycle(state.inflight_cycle())
    assert len(calls) == 1
    assert daemon.query_one("SELECT status FROM build_target")[0] == "complete"
    assert daemon.query_one("SELECT status FROM cycle ORDER BY id DESC LIMIT 1")[0] == "done"
    assert daemon.query_one("SELECT status FROM question_dep")[0] == "satisfied"


def test_worker_resume_rejects_fetch_spec_identity_drift(tmp_path, monkeypatch):
    path = str(tmp_path / "r.sqlite")
    daemon, state, worker = _mk_worker(path, tmp_path / "w")
    _seed_deferred(daemon, state)
    original_drive = worker._drive_import_target
    monkeypatch.setattr(
        worker, "_drive_import_target",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(SystemExit("crash-after-target")))
    with pytest.raises(SystemExit, match="crash-after-target"):
        worker.materialize_pending(max_items=1)
    assert daemon.query_one("SELECT status FROM build_target")[0] == "pending"

    monkeypatch.setattr(worker, "_drive_import_target", original_drive)
    worker.p["fetch"] = lambda candidate: {
        **_fetch_ok(candidate), "eval_key": "drifted-eval-key"}
    with pytest.raises(RuntimeError, match="冻结 spec 身份漂移"):
        worker.resume_cycle(state.inflight_cycle())


def test_scope_missing_no_materialize(tmp_path):
    path = str(tmp_path / "r.sqlite")
    daemon, state, w = _mk_worker(path, tmp_path / "w")
    sel = _seed_deferred(daemon, state, scope='{"allow_eval": true}')   # 缺 allow_publish_pool
    w.materialize_pending()
    assert daemon.query_one("SELECT status FROM baseline WHERE id=?", (sel["baseline_id"],))[0] == "build_failed"
    assert daemon.query_one("SELECT count(*) FROM external_import WHERE action='materialize_failed'")[0] == 1
    assert daemon.query_one("SELECT count(*) FROM run")[0] == 0                      # 未物化
    assert daemon.query_one("SELECT status FROM question_dep WHERE question_id=1")[0] == "blocked"
    assert state.is_schedulable("q1") is True
    assert daemon.query_one(
        "SELECT count(*) FROM decision WHERE type='import_materialization_blocked'")[0] == 1


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
    assert state.is_schedulable("q1") is True                                        # blocked dep → 可重规划
    assert daemon.query_one("SELECT status FROM cycle ORDER BY id DESC LIMIT 1")[0] == "failed"   # worker failed


def test_eval_fail_no_pool_publish(tmp_path):
    path = str(tmp_path / "r.sqlite")
    fetch = lambda cand: {**_fetch_ok(cand), "eval_cmd": [sys.executable, "-c", "import sys; sys.exit(1)"]}
    daemon, state, w = _mk_worker(path, tmp_path / "w", fetch=fetch)
    sel = _seed_deferred(daemon, state)
    w.materialize_pending()
    assert daemon.query_one("SELECT status FROM evaluation")[0] == "failed"
    assert daemon.query_one(
        "SELECT status,failure_kind FROM evaluation_attempt")[:2] == ("failed", "runtime")
    assert daemon.query_one("SELECT count(*) FROM metric_result")[0] == 0
    assert daemon.query_one("SELECT status FROM baseline WHERE id=?", (sel["baseline_id"],))[0] == "build_failed"
    assert daemon.query_one("SELECT count(*) FROM external_import WHERE action='imported'")[0] == 0   # 不 pool_publish
    assert daemon.query_one("SELECT count(*) FROM external_import WHERE action='materialize_failed'")[0] == 1
    assert state.is_schedulable("q1") is True


def test_import_sandbox_output_reject_settles_exact_eval_owner(tmp_path, monkeypatch):
    """Unsafe eval output fails its exact attempt and worker without reopening the successful import run."""
    path = str(tmp_path / "r.sqlite")
    daemon, state, worker = _mk_worker(path, tmp_path / "w")
    sel = _seed_deferred(daemon, state)
    original = H.run_staged
    receipt_path = tmp_path / "execution-import-eval-output-reject.json"

    def reject_eval_output(cmd, **kwargs):
        if kwargs.get("execution_kind") != "import-eval":
            return original(cmd, **kwargs)
        raise SandboxOutputError(
            "quarantine contains hardlink",
            receipt={
                "state": "terminal", "outcome": "exit", "group_drained": True,
                "containment": "docker-container-v1",
                "sandbox": {"container_drained": True},
                "context": dict(kwargs["execution_context"]),
            },
            receipt_path=receipt_path)

    monkeypatch.setattr(H, "run_staged", reject_eval_output)
    assert worker.materialize_pending(max_items=1) == [sel["external_import_id"]]

    assert daemon.query_one("SELECT status,failure_kind FROM run") == ("success", None)
    assert daemon.query_one(
        "SELECT status,failure_kind,transcript_ref FROM evaluation_attempt") == (
            "failed", "artifact_invalid", str(receipt_path))
    assert daemon.query_one("SELECT status FROM evaluation")[0] == "failed"
    assert daemon.query_one("SELECT status,failure_kind FROM build_target")[:2] == (
        "failed", "artifact_invalid")
    assert daemon.query_one(
        "SELECT status FROM baseline WHERE id=?", (sel["baseline_id"],))[0] == "build_failed"
    assert daemon.query_one(
        "SELECT count(*) FROM external_import WHERE action='imported'")[0] == 0
    assert daemon.query_one(
        "SELECT count(*) FROM external_import WHERE action='materialize_failed'")[0] == 1
    assert daemon.query_one("SELECT status FROM question_dep")[0] == "blocked"
    assert daemon.query_one("SELECT status FROM cycle ORDER BY id DESC LIMIT 1")[0] == "failed"
    assert daemon.query_one(
        "SELECT count(*) FROM evaluation_attempt WHERE status='running'")[0] == 0
    assert worker.materialize_pending(max_items=1) == []


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
