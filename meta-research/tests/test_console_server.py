"""CP9.1 · 控制台数据面（console_server）：真表投影 + 派生对象 + 白名单读 + spool 入站。

核心验收：只读组装 /api/db（原型 v2 形状：真表 + status_card + live + notification + policy + FS）；
单写纪律（组装期零 DB 写）；文件白名单防逃逸；入站只写 spool 不碰 DB。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import tempfile
from pathlib import Path

import pytest

import conftest
from orchestrator import console_server as CS
from orchestrator import console_spool as CSP
from orchestrator import database as db
from orchestrator.instance_lease import InstanceLease

SYSTEM_ROOT = str(Path(__file__).resolve().parent.parent)
TEST_CAPABILITY = "a" * 64
TEST_IDEMPOTENCY = "1" * 32


def test_configure_process_storage_stays_beneath_data_root(tmp_path, monkeypatch):
    for name in (
            "TMPDIR", "TMP", "TEMP", "HOME", "CODEX_HOME",
            "CODEX_SQLITE_HOME", "XDG_CACHE_HOME", "PIP_CACHE_DIR",
            "HF_HOME", "HF_HUB_CACHE", "HF_DATASETS_CACHE",
            "TRANSFORMERS_CACHE", "TORCH_HOME", "TORCH_EXTENSIONS_DIR",
            "TRITON_CACHE_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
            "XDG_STATE_HOME", "CONDA_PKGS_DIRS", "CONDA_ENVS_PATH",
            "UV_CACHE_DIR", "CUDA_CACHE_PATH", "MPLCONFIGDIR",
            "NUMBA_CACHE_DIR", "PYTHONPYCACHEPREFIX",
            "METARESEARCH_QUERY_HOME", "METARESEARCH_QUERY_CODEX_HOME",
            "METARESEARCH_QUERY_CODEX_SQLITE_HOME",
            "METARESEARCH_QUERY_CACHE_HOME", "METARESEARCH_STORAGE_ROOT"):
        if name in os.environ:
            monkeypatch.setenv(name, os.environ[name])
        else:
            monkeypatch.setenv(name, "")
            monkeypatch.delenv(name)
    root = tmp_path / "runtime"
    root.mkdir()
    service_source = tmp_path / "service-source"
    query_source = tmp_path / "query-source"
    service_source.mkdir()
    query_source.mkdir()
    (service_source / "auth.json").write_text('{"service":true}\n')
    (service_source / "config.toml").write_text("service = true\n")
    (query_source / "auth.json").write_text('{"query":true}\n')
    (query_source / "config.toml").write_text("query = true\n")
    monkeypatch.setenv("CODEX_HOME", str(service_source))
    monkeypatch.setenv("METARESEARCH_QUERY_CODEX_HOME", str(query_source))
    monkeypatch.delenv("METARESEARCH_STORAGE_ROOT", raising=False)
    for key in ("TMPDIR", "TMP", "TEMP", "XDG_CACHE_HOME", "PIP_CACHE_DIR"):
        monkeypatch.setenv(key, "/outside-before-test")
    monkeypatch.setattr(CS.tempfile, "tempdir", CS.tempfile.tempdir)

    configured = CS.configure_process_storage(
        root, require_external_mount=False)

    expected_tmp = root / ".process-tmp"
    expected_cache = root / ".process-cache" / "service"
    assert {
        key: configured[key] for key in (
            "TMPDIR", "XDG_CACHE_HOME", "PIP_CACHE_DIR")
    } == {
        "TMPDIR": str(expected_tmp),
        "XDG_CACHE_HOME": str(expected_cache),
        "PIP_CACHE_DIR": str(expected_cache / "pip"),
    }
    assert os.environ["TMP"] == os.environ["TEMP"] == str(expected_tmp)
    assert CS.tempfile.tempdir == str(expected_tmp)
    assert os.environ["HOME"] == str(root / ".process-home" / "service")
    assert os.environ["CODEX_HOME"] == str(root / ".codex-runtime" / "service")
    assert os.environ["CODEX_SQLITE_HOME"] == str(
        root / ".codex-runtime" / "service-sqlite")
    assert os.environ["METARESEARCH_QUERY_HOME"] == str(
        root / ".process-home" / "query")
    assert os.environ["METARESEARCH_QUERY_CODEX_HOME"] == str(
        root / ".codex-runtime" / "query")
    assert os.environ["METARESEARCH_QUERY_CODEX_SQLITE_HOME"] == str(
        root / ".codex-runtime" / "query-sqlite")
    for key in (
            "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME",
            "CONDA_PKGS_DIRS", "CONDA_ENVS_PATH", "UV_CACHE_DIR",
            "CUDA_CACHE_PATH", "MPLCONFIGDIR", "NUMBA_CACHE_DIR"):
        assert os.path.commonpath((str(root), os.environ[key])) == str(root)
        assert Path(os.environ[key]).is_dir()
    assert (Path(os.environ["CODEX_HOME"]) / "auth.json").read_text() == (
        '{"service":true}\n')
    assert (Path(os.environ["METARESEARCH_QUERY_CODEX_HOME"]) /
            "auth.json").read_text() == '{"query":true}\n'
    assert stat.S_IMODE(root.stat().st_mode) == 0o711
    assert stat.S_IMODE(expected_tmp.stat().st_mode) == 0o711
    for path in (expected_cache, expected_cache / "pip",
                 root / ".process-home" / "service",
                 root / ".codex-runtime" / "service",
                 root / ".codex-runtime" / "service-sqlite"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700


def _assert_random_idempotency_key(rec):
    assert re.fullmatch(r"console-[0-9a-f]{32}", rec["idempotency_key"])


def _process_append(work: str, count: int, prefix: str, result_queue) -> None:
    """Multiprocessing target must stay at module scope for non-fork test runners."""
    try:
        data = CS.ConsoleData(db_path=str(Path(work) / "unused.sqlite"),
                              work_root=work, system_root=SYSTEM_ROOT)
        result_queue.put([data.enqueue_message(f"{prefix}-{i}")["idempotency_key"]
                          for i in range(count)])
    except BaseException as error:
        result_queue.put((type(error).__name__, str(error)))


class _StoppedConsoleServer:
    """Minimal main()-level server double that exits without a live socket."""

    console_capability_token = TEST_CAPABILITY
    server_address = ("127.0.0.1", 43123)

    def serve_forever(self):
        raise KeyboardInterrupt

    def shutdown(self):
        pass

    def server_close(self):
        pass


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
        json.dumps({"event_key": "e1", "kind": "cycle_done",
                    "payload": {"summary_md": "轮完成"}}) + "\n"
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
    assert payload["live"]["mode"] in ("running", "interrupted", "idle", "awaiting_user")
    assert payload["live"]["orchestrator_active"] is False
    assert payload["live"]["orchestrator_status"] in {"inactive", "invalid"}
    assert "training_live" in payload
    assert payload["training_live"]["available"] is True
    assert payload["training_live"]["contract_version"] == 1
    assert payload["training_live"]["total_targets"] == 0  # 没有 Bundle runner 时不伪造监控轮
    assert isinstance(payload["training_live"]["logs"], list)
    assert payload["training_live"]["agent_live_text"] == ""
    assert payload["training_live"]["agent_activities"] == []
    assert payload["notification"] == [{
        "event_key": "e1", "kind": "cycle_done", "payload": {"summary_md": "轮完成"},
    }]  # 撕裂行被弃
    assert "budget" in payload["policy"] and "tree_guard" in payload["policy"]                       # 真 policy.yaml
    assert payload["fs"]["roots"][0]["p"] == "work"            # FS 树含 work 根
    # ledger 当前空 → 空数组（不炸）
    assert payload["ledger_by_cycle"] == [] or isinstance(payload["ledger_by_cycle"], list)


def test_training_live_projects_real_bounded_log_tail_and_progress(seeded, tmp_path):
    path, work = seeded
    connection = db.connect(path)
    connection.execute("UPDATE cycle SET status='bundle' WHERE id=1")
    connection.execute(
        "INSERT INTO build_target(id,cycle_id,question_id,target_kind,seq,status,variant_id) "
        "VALUES (3,1,1,'build',3,'running',1)")
    connection.execute(
        "INSERT INTO runner_call(id,cycle_id,phase,purpose,status,started_at) "
        "VALUES (10,1,'bundle','bundle-main-c1','running','now')")
    connection.commit()
    connection.close()

    work_path = Path(work)
    context = work_path / "cycles" / "c1" / "context_pack"
    context.mkdir(parents=True)
    (context / "bundle.3.pack.json").write_text("{}\n", encoding="utf-8")
    log_dir = work_path / "c1" / "t3" / "run7"
    log_dir.mkdir(parents=True)
    secret = "super-secret-value"
    train_log = log_dir / "train.log.partial"
    train_log.write_text(
        "x" * (CS._MAX_TRAINING_LOG_TAIL_BYTES + 256) + "\n"
        + "gpu_used: 7\n"
        + "cell_gpu: key=a logical_cuda_index=0\n"
        + "cell_gpu: key=b logical_cuda_index=6\n"
        + "Authorization: Bearer " + secret + "\n"
        + "artifact=" + work + "/c1/t3/run7/checkpoints/a.pt\n"
        + "step=12/20 loss=0.5\n",
        encoding="utf-8")
    (log_dir / "train.log.process.json").write_text(
        '{"metadata":true}\n', encoding="utf-8")
    outside = tmp_path / "outside.log"
    outside.write_text("OUTSIDE-SENTINEL\n", encoding="utf-8")
    (log_dir / "linked.log").symlink_to(outside)

    connection = CS._open_ro(path, work_root=work)
    try:
        live = CS._training_live(
            connection, work_path,
            orchestrator_live={"orchestrator_active": True, "mode": "running"})
    finally:
        connection.close()

    assert live["active"] is True
    assert live["available"] is True and live["contract_version"] == 1
    assert live["cycle_id"] == 1 and live["runner_call_id"] == 10
    assert live["current_target"]["id"] == 3
    assert live["settled_targets"] == 2 and live["total_targets"] == 3
    assert live["target_progress_pct"] == pytest.approx(66.7)
    assert live["substage"] == "train" and live["logs_target_id"] == 3
    assert [entry["path"] for entry in live["logs"]] == ["run7/train.log.partial"]
    visible = live["logs"][0]["tail_text"]
    assert "step=12/20" in visible and "[已遮蔽]" in visible
    assert secret not in visible and work not in visible and "OUTSIDE-SENTINEL" not in visible
    assert live["logs"][0]["truncated"] is True
    assert live["progress"] == {
        "current": 12, "total": 20, "unit": "step",
        "pct": 60.0, "label": "step 12 / 20",
    }
    assert live["gpu_used"] == 7 and live["gpu_indices"] == [0, 6]


def test_training_live_projects_bundle_agent_before_experiment_logs(seeded, monkeypatch):
    path, work = seeded
    connection = db.connect(path)
    connection.execute("UPDATE cycle SET status='bundle' WHERE id=1")
    connection.execute(
        "INSERT INTO runner_call(id,cycle_id,phase,purpose,status,started_at) "
        "VALUES (10,1,'bundle','bundle-main-c1','running','now')")
    connection.commit()
    connection.close()

    activities = [{"key": "a1", "activity_kind": "file",
                   "activity_state": "completed", "text": "文件修改完成"}]
    monkeypatch.setattr(
        CS, "_running_codex_capture_projection",
        lambda _root, _receipt: ("正在构建实验代码", activities))
    connection = CS._open_ro(path, work_root=work)
    try:
        live = CS._training_live(
            connection, Path(work),
            orchestrator_live={"orchestrator_active": True, "mode": "running"},
            running_authority={"runner_call_id": 10, "receipt": {}})
    finally:
        connection.close()

    assert live["active"] is True and live["logs"] == []
    assert live["agent_live_text"] == "正在构建实验代码"
    assert live["agent_activities"] == activities


def test_training_progress_reports_completed_checkpoint_count():
    progress = CS._training_progress([{
        "tail_text": "checkpoint_written: key=a path=x\ntrain_complete: checkpoints=50\n",
    }])
    assert progress == {
        "current": 50, "total": 50, "unit": "checkpoint",
        "pct": 100.0, "label": "训练完成 · 50 个 checkpoint",
    }


def test_assemble_db_no_db_write(seeded):
    """单写纪律：组装用 mode=ro 连接——即便组装出错也绝不写库（本测证 mode=ro 物理只读）。"""
    path, work = seeded
    ro = CS._open_ro(path, work_root=work)
    with pytest.raises(Exception):                            # mode=ro 写必失败
        ro.execute("INSERT INTO decision(actor,type,payload_json) VALUES ('x','y','{}')")
    ro.close()
    CS.assemble_db(path, work, SYSTEM_ROOT)                   # 组装不抛
    n = db.connect(path).execute("SELECT count(*) FROM decision").fetchone()[0]
    CS.assemble_db(path, work, SYSTEM_ROOT)
    assert db.connect(path).execute("SELECT count(*) FROM decision").fetchone()[0] == n   # 组装前后 DB 不变


@pytest.mark.parametrize("instance", [
    {"status": "invalid", "lock_held": False, "local_active_owner": False},
    {"status": "stale", "lock_held": True, "local_active_owner": False},
])
def test_shared_db_reader_rejects_unverified_owner_before_sqlite_open(
        seeded, monkeypatch, instance):
    path, work = seeded
    monkeypatch.setattr(CS, "journal_mode_for_path", lambda _path: "delete")
    monkeypatch.setattr(CS, "read_instance_status", lambda _root: instance)
    monkeypatch.setattr(
        CS.sqlite3, "connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("denied reader must not open SQLite")))
    with pytest.raises(RuntimeError, match="未证实本机 active owner"):
        CS._open_ro(path, work_root=work)


def test_shared_db_reader_accepts_verified_owner_on_same_host(seeded, monkeypatch):
    path, work = seeded
    monkeypatch.setattr(CS, "journal_mode_for_path", lambda _path: "delete")
    monkeypatch.setattr(CS, "read_instance_status", lambda _root: {
        "status": "active", "lock_held": True, "local_active_owner": True,
    })
    conn = CS._open_ro(path, work_root=work)
    conn.close()


def test_live_mode_awaiting_user(seeded):
    """live.mode：有 pending 文件请求 → awaiting_user。"""
    path, work = seeded
    d = db.connect(path)
    d.execute("INSERT INTO interaction_request(goal_id,goal_ver,stage,status,summary_md,items_json,request_hash) "
              "VALUES (1,1,'plan','pending','需数据','[]','rh')")
    d.commit(); d.close()
    assert CS.assemble_db(path, work, SYSTEM_ROOT)["live"]["mode"] == "awaiting_user"


def test_live_running_requires_fresh_instance_owner(seeded):
    path, work = seeded
    without_owner = CS.assemble_db(path, work, SYSTEM_ROOT)["live"]
    assert without_owner["mode"] == "interrupted"
    assert without_owner["orchestrator_active"] is False

    lease = InstanceLease.acquire(work, heartbeat_interval_s=0.02)
    try:
        lease.set_state("running", cycle_id="c1", activity="test")
        live = CS.assemble_db(path, work, SYSTEM_ROOT)["live"]
        assert live["mode"] == "running"
        assert live["orchestrator_active"] is True
        assert live["orchestrator_status"] == "active"
        assert live["orchestrator_owner_id"] == lease.owner_id
        assert live["orchestrator_heartbeat_age_s"] is not None
    finally:
        lease.close()


# ============ 文件白名单读 ============
def test_read_file_whitelist_and_escape(seeded):
    path, work = seeded
    data = CS.ConsoleData(db_path=path, work_root=work, system_root=SYSTEM_ROOT)
    assert b"budget" in data.read_file("policies/policy.yaml")           # system schemas/prompts/policies 可读
    assert data.read_file("../../../etc/passwd") is None                 # 逃逸拒
    assert data.read_file("nonexist.txt") is None
    (Path(work) / "cycles").mkdir()
    (Path(work) / "cycles" / "note.txt").write_text("hi", encoding="utf-8")
    assert data.read_file("work/cycles/note.txt") == b"hi"
    (Path(work) / "state" / "internal.txt").write_text("secret", encoding="utf-8")
    assert data.read_file("work/state/internal.txt") is None
    (Path(work) / "input").mkdir()
    (Path(work) / "input" / "local-sources.json").write_text(
        '{"source_root":"/private/dataset"}', encoding="utf-8")
    assert data.read_file("work/input/local-sources.json") is None


# ============ 入站 spool（只写文件、不碰 DB）============
def test_enqueue_message_spool_only(seeded):
    path, work = seeded
    data = CS.ConsoleData(db_path=path, work_root=work, system_root=SYSTEM_ROOT)
    before = db.connect(path).execute("SELECT count(*) FROM interaction_message").fetchone()[0]
    r1 = data.enqueue_message("暂停一下")
    r2 = data.enqueue_message("q13 现在到哪了？", conversation_id="a" * 32)
    assert r1["seq"] == 1 and r2["seq"] > r1["seq"]
    _assert_random_idempotency_key(r1); _assert_random_idempotency_key(r2)
    lines = (Path(work) / "state" / "console_inbox.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["raw_text"] == "暂停一下"
    assert json.loads(lines[1])["conversation_id"] == "a" * 32
    # **不碰 DB**：interaction_message 未增（run 进程 ingest 才入库）
    assert db.connect(path).execute("SELECT count(*) FROM interaction_message").fetchone()[0] == before
    with pytest.raises(ValueError):
        data.enqueue_message("   ")                                      # 空消息拒
    with pytest.raises(ValueError, match="字符串"):
        data.enqueue_message({})                                         # JSON object 不得打穿 handler
    with pytest.raises(ValueError, match="conversation_id"):
        data.enqueue_message("bad conversation", conversation_id="shared")


def test_enqueue_narrator_query_marks_transport_intent_and_stays_spool_only(seeded):
    path, work = seeded
    data = CS.ConsoleData(db_path=path, work_root=work, system_root=SYSTEM_ROOT)
    before = db.connect(path).execute("SELECT count(*) FROM directive").fetchone()[0]
    rec = data.enqueue_query(
        "暂停并改预算——请解释这句话会触发什么",
        conversation_id="b" * 32,
        client_idempotency_key="9" * 32)
    assert rec["action_target"] == "query"
    assert rec["conversation_id"] == "b" * 32
    assert rec["idempotency_key"] == "console-" + "9" * 32
    assert db.connect(path).execute("SELECT count(*) FROM directive").fetchone()[0] == before


def test_enqueue_fences_torn_tail_before_acknowledging_next_record(seeded):
    """崩溃残尾不会与下一条 HTTP 已 ACK intent 粘接；残尾单独成 poison 行，新动作保持完整。"""
    path, work = seeded
    data = CS.ConsoleData(db_path=path, work_root=work, system_root=SYSTEM_ROOT)
    data.inbox.write_bytes(b'{"connector":"console","raw_text":"torn"')
    rec = data.enqueue_message("下一条必须可消费")
    lines = data.inbox.read_bytes().split(b"\n")
    assert len(lines) == 3 and lines[0].startswith(b'{"connector"')
    assert json.loads(lines[1])["raw_text"] == "下一条必须可消费"
    assert rec["seq"] > 1
    _assert_random_idempotency_key(rec)


def test_enqueue_directive_action_spool_only(seeded):
    """确认/拒绝控件也只写 spool，与普通消息共用 seq；server 不查/改 directive 表。"""
    path, work = seeded
    data = CS.ConsoleData(db_path=path, work_root=work, system_root=SYSTEM_ROOT)
    before = db.connect(path).execute("SELECT count(*) FROM interaction_message").fetchone()[0]
    data.enqueue_message("暂停一下")
    confirm = data.enqueue_directive_action(action="confirm", directive_id=7)
    reject = data.enqueue_directive_action(action="reject", directive_id="8", reason="润色语义不对")
    assert {key: confirm[key] for key in ("connector", "raw_text", "action", "directive_id")} == {
        "connector": "console", "raw_text": "确认指令 d7", "action": "confirm",
        "directive_id": 7}
    _assert_random_idempotency_key(confirm)
    _assert_random_idempotency_key(reject)
    assert confirm["idempotency_key"] != reject["idempotency_key"]
    assert reject["seq"] > confirm["seq"] and reject["action"] == "reject" and reject["reason"] == "润色语义不对"
    assert db.connect(path).execute("SELECT count(*) FROM interaction_message").fetchone()[0] == before
    for bad in ({"action": "apply", "directive_id": 1}, {"action": "confirm", "directive_id": 0},
                {"action": "reject", "directive_id": True}, {"action": "confirm", "directive_id": "1.5"}):
        with pytest.raises(ValueError):
            data.enqueue_directive_action(**bad)


def test_enqueue_file_request_action_whitelist_and_spool_only(seeded):
    """resolve 只接受 work/input uploads 虚拟目录；server 只读核 pending 后写 spool，不迁请求状态。"""
    path, work = seeded
    conn = db.connect(path)
    rid = conn.execute(
        "INSERT INTO interaction_request(goal_id,goal_ver,stage,status,summary_md,items_json,request_hash) "
        "VALUES (1,1,'plan','pending','需文件','[]','fr-http')").lastrowid
    conn.commit(); conn.close()
    src = Path(work) / "uploads" / f"r{rid}" / "1"
    src.mkdir(parents=True); (src / "data.bin").write_bytes(b"DATA")
    data = CS.ConsoleData(db_path=path, work_root=work, system_root=SYSTEM_ROOT)

    queued = data.enqueue_file_request_action(
        action="resolve", request_id=rid, source_ref=f"work/uploads/r{rid}")
    assert queued["action_target"] == "file_request" and queued["source_ref"] == f"work/uploads/r{rid}"
    assert queued["raw_text"].startswith(f"解决文件请求 r{rid}")
    assert db.connect(path).execute(
        "SELECT status,resolved_message_id FROM interaction_request WHERE id=?", (rid,)).fetchone() == (
        "pending", None)

    for bad_ref in ("../outside", "/work/uploads/x", "work/state", "work/uploads/x\ncontrol"):
        with pytest.raises(ValueError):
            data.enqueue_file_request_action(action="resolve", request_id=rid, source_ref=bad_ref)
    # HTTP 只核纯语法；目录可能在入队后、run 消费前才出现，实体/fd 校验属于权威 ingest。
    missing = data.enqueue_file_request_action(
        action="resolve", request_id=rid, source_ref="work/uploads/missing")
    assert missing["source_ref"] == "work/uploads/missing"
    with pytest.raises(ValueError, match="不存在"):
        data.enqueue_file_request_action(action="cancel", request_id=999, reason="x")


def test_file_request_http_does_not_treat_path_preflight_as_authority(seeded):
    """HTTP 只持久化规范 ref；symlink/存在性由 run 的 openat(O_NOFOLLOW) 权威拒绝。"""
    path, work = seeded
    conn = db.connect(path)
    rid = conn.execute(
        "INSERT INTO interaction_request(goal_id,goal_ver,stage,status,summary_md,items_json,request_hash) "
        "VALUES (1,1,'plan','pending','需文件','[]','fr-symlink-root')").lastrowid
    conn.commit(); conn.close()
    (Path(work) / "uploads").symlink_to(Path(work) / "state", target_is_directory=True)
    data = CS.ConsoleData(db_path=path, work_root=work, system_root=SYSTEM_ROOT)
    queued = data.enqueue_file_request_action(
        action="resolve", request_id=rid, source_ref="work/uploads")
    assert queued["source_ref"] == "work/uploads"
    assert db.connect(path).execute(
        "SELECT status FROM interaction_request WHERE id=?", (rid,)).fetchone() == ("pending",)


@pytest.mark.parametrize("bad", [1.0, "01", (1 << 63), True])
def test_action_ids_are_canonical_sqlite_integers(seeded, bad):
    path, work = seeded
    data = CS.ConsoleData(db_path=path, work_root=work, system_root=SYSTEM_ROOT)
    with pytest.raises(ValueError):
        data.enqueue_directive_action(action="confirm", directive_id=bad)


def test_console_refuses_non_loopback_bind(seeded):
    path, work = seeded
    with pytest.raises(ValueError, match="loopback"):
        CS.serve(path, work, SYSTEM_ROOT, host="0.0.0.0", port=0)


# ============ HTTP 端到端（真起服务、真请求）============
def test_http_endpoints(seeded):
    import threading
    import urllib.request
    path, work = seeded
    conn = db.connect(path)
    rid = conn.execute(
        "INSERT INTO interaction_request(goal_id,goal_ver,stage,status,summary_md,items_json,request_hash) "
        "VALUES (1,1,'plan','pending','需文件','[]','fr-endpoint')").lastrowid
    conn.commit(); conn.close()
    (Path(work) / "uploads" / f"r{rid}" / "1").mkdir(parents=True)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 绕过 shell 的 HTTP_PROXY（本机直连）
    opener.addheaders = [("Authorization", f"Bearer {TEST_CAPABILITY}")]
    httpd = CS.serve(path, work, SYSTEM_ROOT, host="127.0.0.1", port=0,
                     capability_token=TEST_CAPABILITY)                     # port=0 自选空闲端口
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
                                     headers={"Content-Type": "application/json", "Idempotency-Key": "1" * 32})
        first_queued = json.loads(opener.open(req, timeout=5).read())["queued"]
        req = urllib.request.Request(base + "/api/query", method="POST",
                                     data=json.dumps({"text": "暂停系统会怎样？",
                                                      "conversation_id": "b" * 32}).encode(),
                                     headers={"Content-Type": "application/json",
                                              "Idempotency-Key": "9" * 32})
        query_queued = json.loads(opener.open(req, timeout=5).read())["queued"]
        assert query_queued["action_target"] == "query"
        req = urllib.request.Request(base + "/api/directive", method="POST",
                                     data=json.dumps({"action": "confirm", "directive_id": 99}).encode(),
                                     headers={"Content-Type": "application/json", "Idempotency-Key": "2" * 32})
        queued = json.loads(opener.open(req, timeout=5).read())["queued"]
        assert queued["action"] == "confirm" and queued["directive_id"] == 99
        assert queued["seq"] > first_queued["seq"]
        directive_seq = queued["seq"]
        req = urllib.request.Request(base + "/api/file-request", method="POST",
                                     data=json.dumps({"action": "resolve", "request_id": rid,
                                                      "source_ref": f"work/uploads/r{rid}"}).encode(),
                                     headers={"Content-Type": "application/json", "Idempotency-Key": "3" * 32})
        with pytest.raises(urllib.error.HTTPError) as ei:
            opener.open(req, timeout=5)
        assert ei.value.code == 400
        req = urllib.request.Request(base + "/api/file-request", method="POST",
                                     data=json.dumps({"action": "cancel", "request_id": rid,
                                                      "reason": "暂时无法提供"}).encode(),
                                     headers={"Content-Type": "application/json",
                                              "Idempotency-Key": "8" * 32})
        queued = json.loads(opener.open(req, timeout=5).read())["queued"]
        assert queued["action_target"] == "file_request" and queued["request_id"] == rid
        assert queued["seq"] > directive_seq
        assert db.connect(path).execute(
            "SELECT status FROM interaction_request WHERE id=?", (rid,)).fetchone()[0] == "pending"
        # 浏览器简单跨站请求（text/plain）与 evil Origin 均在动作入队前拒绝。
        req = urllib.request.Request(base + "/api/directive", method="POST",
                                     data=b'{"action":"confirm","directive_id":99}',
                                     headers={"Content-Type": "text/plain", "Idempotency-Key": "4" * 32})
        with pytest.raises(urllib.error.HTTPError) as ei:
            opener.open(req, timeout=5)
        assert ei.value.code == 400
        req = urllib.request.Request(base + "/api/directive", method="POST",
                                     data=b'{"action":"confirm","directive_id":99}',
                                     headers={"Content-Type": "application/json", "Origin": "http://evil.invalid"})
        with pytest.raises(urllib.error.HTTPError) as ei:
            opener.open(req, timeout=5)
        assert ei.value.code == 403
        assert (Path(work) / "state" / "console_inbox.jsonl").exists()
    finally:
        httpd.shutdown()


def test_multi_quest_http_lists_routes_and_never_crosses_work_roots(tmp_path):
    import http.client
    import threading
    import urllib.error
    import urllib.request

    registry = CS.QuestRegistry(tmp_path / "registry", Path(SYSTEM_ROOT))
    brief = lambda name: ("---\npredicate_json: {kind: test}\n---\n\n# " + name + "\n")
    qa = registry.create(quest_id="alpha", title="Alpha", goal_brief_md=brief("alpha"))
    qb = registry.create(quest_id="beta", title="Beta", goal_brief_md=brief("beta"))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    opener.addheaders = [("Authorization", f"Bearer {TEST_CAPABILITY}")]
    httpd = CS.serve_quests(
        registry.root, SYSTEM_ROOT, host="127.0.0.1", port=0,
        capability_token=TEST_CAPABILITY)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        conn = http.client.HTTPConnection(
            "127.0.0.1", httpd.server_address[1], timeout=5)
        conn.request("GET", "/")
        bootstrap = conn.getresponse()
        assert bootstrap.status == 303
        assert bootstrap.getheader("Location") == (
            "/?console-bootstrap=1#token=" + TEST_CAPABILITY)
        assert bootstrap.getheader("Cache-Control") == "no-store"
        assert bootstrap.getheader("Referrer-Policy") == "no-referrer"
        bootstrap.read(); conn.close()
        page = opener.open(base + "/?console-bootstrap=1", timeout=5).read()
        assert b"<!doctype html>" in page[:100].lower()
        assert TEST_CAPABILITY.encode() not in page

        listing = json.loads(opener.open(base + "/api/quests", timeout=5).read())
        assert [q["quest_id"] for q in listing["quests"]] == ["alpha", "beta"]
        selector = json.loads(opener.open(
            base + "/api/quests?view=selector", timeout=5).read())
        assert [q["quest_id"] for q in selector["quests"]] == ["alpha", "beta"]
        assert all("setup" not in q and "runtime" not in q
                   and "runtime_profile" not in q for q in selector["quests"])
        alpha = json.loads(opener.open(base + "/api/db?quest=alpha", timeout=5).read())
        beta = json.loads(opener.open(base + "/api/db?quest=beta", timeout=5).read())
        assert alpha["tables"]["goal"][0]["text"] == "# alpha"
        assert beta["tables"]["goal"][0]["text"] == "# beta"
        assert [root["p"] for root in alpha["fs"]["roots"]] == ["work"]
        with pytest.raises(urllib.error.HTTPError) as backend_file:
            opener.open(
                base + "/api/file?p=policies/policy.yaml&quest=alpha",
                timeout=5)
        assert backend_file.value.code == 404
        diagnostic = json.loads(opener.open(
            base + "/api/quest-runtime-log?quest=alpha", timeout=5).read())
        assert diagnostic == {"diagnostic": {
            "quest_id": "alpha", "available": False, "text": ""}}

        request = urllib.request.Request(
            base + "/api/query", method="POST",
            data=json.dumps({"quest_id": "alpha", "text": "解释当前任务",
                             "conversation_id": "c" * 32}).encode(),
            headers={"Content-Type": "application/json", "Idempotency-Key": "8" * 32})
        queued = json.loads(opener.open(request, timeout=5).read())["queued"]
        assert queued["action_target"] == "query"
        alpha_lines = qa.work_root.joinpath("state/console_inbox.jsonl").read_text().splitlines()
        assert json.loads(alpha_lines[-1])["raw_text"] == "解释当前任务"
        assert not qb.work_root.joinpath("state/console_inbox.jsonl").exists()

        missing = urllib.request.Request(
            base + "/api/message", method="POST", data=json.dumps({"text": "暂停"}).encode(),
            headers={"Content-Type": "application/json", "Idempotency-Key": "7" * 32})
        with pytest.raises(urllib.error.HTTPError) as denied:
            opener.open(missing, timeout=5)
        assert denied.value.code == 400
    finally:
        httpd.shutdown()
        httpd.server_close()
        worker.join(timeout=5)


def test_multi_quest_create_key_cannot_be_reused_for_different_body(tmp_path):
    import threading
    import urllib.error
    import urllib.request

    root = tmp_path / "registry"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    opener.addheaders = [("Authorization", f"Bearer {TEST_CAPABILITY}")]
    httpd = CS.serve_quests(
        root, SYSTEM_ROOT, host="127.0.0.1", port=0,
        capability_token=TEST_CAPABILITY)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    key = "6" * 32

    def request(quest_id):
        return urllib.request.Request(
            base + "/api/quests", method="POST",
            data=json.dumps({
                "quest_id": quest_id, "title": quest_id,
                "goal_brief_md": "---\npredicate_json: {kind: test}\n---\n\n# test\n",
            }).encode(),
            headers={"Content-Type": "application/json", "Idempotency-Key": key})

    try:
        assert json.loads(opener.open(request("alpha"), timeout=5).read())["quest"]["quest_id"] == "alpha"
        # Exact retry converges to the existing quest.
        assert json.loads(opener.open(request("alpha"), timeout=5).read())["quest"]["quest_id"] == "alpha"
        with pytest.raises(urllib.error.HTTPError) as conflict:
            opener.open(request("beta"), timeout=5)
        assert conflict.value.code == 409
        assert [quest.quest_id for quest in CS.QuestRegistry(root, Path(SYSTEM_ROOT)).list()] == ["alpha"]
    finally:
        httpd.shutdown(); httpd.server_close(); worker.join(timeout=5)


def test_web_http_draft_attaches_local_folders_without_path_echo(tmp_path):
    import threading
    import time
    import urllib.parse
    import urllib.request

    root = tmp_path / "registry"
    dataset = tmp_path / "existing-data" / "SEED"
    dataset.mkdir(parents=True)
    (dataset / "subject-01.mat").write_bytes(b"eeg")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    opener.addheaders = [("Authorization", f"Bearer {TEST_CAPABILITY}")]
    httpd = CS.serve_quests(
        root, SYSTEM_ROOT, host="127.0.0.1", port=0,
        capability_token=TEST_CAPABILITY,
        local_import_roots=[tmp_path])
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def post(path, body, key):
        request = urllib.request.Request(
            base + path, method="POST",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Idempotency-Key": key})
        return json.loads(opener.open(request, timeout=10).read())

    try:
        setup = json.loads(opener.open(base + "/api/setup", timeout=5).read())
        assert setup["upload"]["local_directory_attachment"] is True
        created = post("/api/quest-drafts", {
            "quest_id": "web-local", "title": "Web local",
            "goal_brief_md": "---\npredicate_json: {kind: test}\n---\n\n# local\n",
        }, "1" * 32)
        draft_id = created["draft"]["draft_id"]
        attached = post("/api/quest-drafts/local-sources", {
            "draft_id": draft_id, "kind": "dataset", "path": str(dataset),
        }, "2" * 32)
        serialized = json.dumps(attached, ensure_ascii=False)
        assert str(tmp_path) not in serialized
        assert attached["source"]["label"] == "SEED"
        detail = json.loads(opener.open(
            base + "/api/quest-drafts?draft_id="
            + urllib.parse.quote(draft_id), timeout=5).read())
        assert str(tmp_path) not in json.dumps(detail, ensure_ascii=False)
        assert detail["draft"]["local_sources"][0]["file_count"] == 1
        preflight = post("/api/quest-drafts/preflight", {
            "draft_id": draft_id,
        }, "3" * 32)
        assert {item["dataset"] for item in preflight["preflight"]["candidates"]} >= {
            "SEED"}
        assert str(tmp_path) not in json.dumps(preflight, ensure_ascii=False)
        submitted = post("/api/quest-drafts/publish", {
            "draft_id": draft_id, "start": False,
        }, "4" * 32)
        assert submitted["job"]["status"] in {"running", "succeeded"}
        job_id = submitted["job"]["job_id"]
        deadline = time.monotonic() + 10
        while True:
            polled = json.loads(opener.open(
                base + "/api/quest-publish-status?job="
                + urllib.parse.quote(job_id), timeout=5).read())
            if polled["job"]["status"] in {"succeeded", "failed"}:
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert polled["job"]["status"] == "succeeded", polled["job"].get("error")
        assert polled["job"]["result"]["quest"]["quest_id"] == "web-local"
        assert str(tmp_path) not in json.dumps(polled, ensure_ascii=False)
    finally:
        httpd.shutdown(); httpd.server_close(); worker.join(timeout=5)


def test_runtime_profile_http_get_post_and_quest_list(tmp_path, monkeypatch):
    import threading
    import urllib.parse
    import urllib.error
    import urllib.request
    import orchestrator.quest_process_manager as QPM

    memory_bytes = 80 * 1024 ** 3
    detected = [
        {"index": index, "model": "NVIDIA A100-SXM4-80GB",
         "memory_bytes": memory_bytes}
        for index in range(8)
    ]
    monkeypatch.setattr(QPM, "_local_gpu_devices", lambda: detected)

    root = tmp_path / "registry"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    opener.addheaders = [("Authorization", f"Bearer {TEST_CAPABILITY}")]
    httpd = CS.serve_quests(
        root, SYSTEM_ROOT, host="127.0.0.1", port=0,
        capability_token=TEST_CAPABILITY)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def post(path, body, key):
        request = urllib.request.Request(
            base + path, method="POST",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Idempotency-Key": key})
        return json.loads(opener.open(request, timeout=10).read())

    default = {
        "version": 3, "compute_profile_id": "local-gpu",
        "review_intensity": "once",
        "gpu_device_indices": list(range(8)),
    }
    cpu_off = {
        "version": 1, "compute_profile_id": "local-cpu",
        "review_intensity": "off",
    }
    try:
        setup = json.loads(opener.open(base + "/api/setup", timeout=5).read())
        options = setup["runtime_profile_options"]
        assert options["version"] == 3
        assert options["default_profile"] == default
        assert options["gpu_devices"] == [
            {"index": index,
             "label": f"GPU {index} · NVIDIA A100-SXM4-80GB · 80 GiB",
             "model": "NVIDIA A100-SXM4-80GB",
             "memory_bytes": memory_bytes}
            for index in range(8)
        ]
        assert options["gpu_selection"] == {
            "mode": "exact", "default_count": 8,
            "min_count": 1, "max_count": 8,
        }
        assert "allowed_device_indices" not in json.dumps(options)
        assert "GPU-" not in json.dumps(options)

        outside = urllib.request.Request(
            base + "/api/quest-drafts", method="POST",
            data=json.dumps({
                "quest_id": "runtime-outside", "title": "Runtime outside",
                "goal_brief_md": (
                    "---\npredicate_json: {kind: test}\n---\n\n# outside\n"),
                "runtime_profile": {
                    "version": 3,
                    "compute_profile_id": "local-gpu",
                    "review_intensity": "once",
                    "gpu_device_indices": [8],
                },
            }).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Idempotency-Key": "0" * 32})
        with pytest.raises(urllib.error.HTTPError) as rejected:
            opener.open(outside, timeout=5)
        assert rejected.value.code == 400
        error = json.loads(rejected.value.read())
        assert "当前探测可信列表" in error["error"]
        assert json.loads(opener.open(
            base + "/api/quest-drafts", timeout=5).read())["drafts"] == []

        created = post("/api/quest-drafts", {
            "quest_id": "runtime-http", "title": "Runtime HTTP",
            "goal_brief_md": (
                "---\npredicate_json: {kind: test}\n---\n\n# runtime\n"),
            "runtime_profile": default,
        }, "1" * 32)
        draft_id = created["draft"]["draft_id"]
        assert created["draft"]["runtime_profile"] == default
        published = post("/api/quest-drafts/publish", {
            "draft_id": draft_id, "start": False,
        }, "2" * 32)
        assert published["quest"]["runtime_profile"]["revision"] == 1

        current = json.loads(opener.open(
            base + "/api/quest-runtime-profile?quest_id=runtime-http",
            timeout=5).read())["runtime_profile"]
        assert current["profile"] == default
        alias = json.loads(opener.open(
            base + "/api/quest-runtime-profile?quest=runtime-http",
            timeout=5).read())["runtime_profile"]
        assert alias == current

        updated = post("/api/quest-runtime-profile", {
            "quest_id": "runtime-http", "runtime_profile": cpu_off,
        }, "3" * 32)
        assert updated["runtime_profile"]["revision"] == 2
        assert updated["runtime_profile"]["profile"] == cpu_off
        assert updated["apply_boundary"] == "cycle"
        assert updated["restart_pending"] is False
        assert set(updated) == {
            "ok", "idempotency_key", "runtime_profile", "runtime",
            "restart_pending", "apply_boundary",
        }
        assert post("/api/quest-runtime-profile", {
            "quest_id": "runtime-http", "runtime_profile": cpu_off,
        }, "3" * 32)["runtime_profile"] == updated["runtime_profile"]

        listing = json.loads(opener.open(base + "/api/quests", timeout=5).read())
        row = next(item for item in listing["quests"]
                   if item["quest_id"] == "runtime-http")
        assert row["runtime_profile"] == updated["runtime_profile"]
        assert row["setup"]["runtime_profile"] == updated["runtime_profile"]

        both = urllib.request.Request(
            base + "/api/quest-runtime-profile?quest_id=runtime-http&quest=runtime-http")
        with pytest.raises(urllib.error.HTTPError) as invalid_alias:
            opener.open(both, timeout=5)
        assert invalid_alias.value.code == 400

        quest = CS.QuestRegistry(root, Path(SYSTEM_ROOT)).get("runtime-http")
        assert "runtime_profile" not in json.loads(
            (quest.work_root / "quest.json").read_text(encoding="utf-8"))
    finally:
        httpd.shutdown(); httpd.server_close(); worker.join(timeout=5)


def test_runtime_profile_postcommit_retry_is_503_with_exact_key_echo(
        tmp_path, monkeypatch):
    """A saved mutation is non-definitive; only pre-commit conflicts use 409."""
    import threading
    import urllib.error
    import urllib.request

    key = "e" * 32
    calls = []

    def update_runtime_profile(_self, quest_id, profile, idempotency_key):
        calls.append((quest_id, profile, idempotency_key))
        if quest_id == "precommit-conflict":
            raise CS.WebQuestConflictError("different operation")
        raise CS.WebQuestRetryableError(
            "runtime profile 已保存，但 restart 调度待恢复", idempotency_key)

    monkeypatch.setattr(
        CS.WebQuestService, "update_runtime_profile", update_runtime_profile)
    httpd = CS.serve_quests(
        tmp_path / "registry", SYSTEM_ROOT, host="127.0.0.1", port=0,
        capability_token=TEST_CAPABILITY)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    opener.addheaders = [("Authorization", f"Bearer {TEST_CAPABILITY}")]

    def rejected(quest_id):
        request = urllib.request.Request(
            base + "/api/quest-runtime-profile", method="POST",
            data=json.dumps({
                "quest_id": quest_id,
                "runtime_profile": {
                    "version": 1,
                    "compute_profile_id": "local-gpu",
                    "review_intensity": "once",
                },
            }).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Idempotency-Key": key})
        with pytest.raises(urllib.error.HTTPError) as captured:
            opener.open(request, timeout=5)
        return captured.value.code, json.loads(captured.value.read())

    try:
        status, body = rejected("saved-pending")
        assert status == 503
        assert body == {
            "error": "runtime profile 已保存，但 restart 调度待恢复",
            "retryable": True,
            "operation_state": "saved_pending_restart",
            "idempotency_key": "console-" + key,
        }
        conflict_status, conflict_body = rejected("precommit-conflict")
        assert conflict_status == 409
        assert conflict_body == {"error": "different operation"}
        assert [call[2] for call in calls] == [key, key]
    finally:
        httpd.shutdown(); httpd.server_close(); worker.join(timeout=5)


def test_http_post_requires_one_canonical_idempotency_key_before_spooling(seeded):
    """缺失、重复或非 128-bit 小写 hex 的客户端键都必须在 append 前失败。"""
    import http.client
    import threading

    path, work = seeded
    httpd = CS.serve(path, work, SYSTEM_ROOT, host="127.0.0.1", port=0,
                     capability_token=TEST_CAPABILITY)
    port = httpd.server_address[1]
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    body = b'{"text":"must-not-queue"}'

    def rejected(keys):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.putrequest("POST", "/api/message")
        conn.putheader("Authorization", f"Bearer {TEST_CAPABILITY}")
        for key in keys:
            conn.putheader("Idempotency-Key", key)
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body)
        response = conn.getresponse()
        response_body = response.read()
        status = response.status
        conn.close()
        assert status == 400, response_body

    try:
        rejected([])                                             # missing
        rejected([TEST_IDEMPOTENCY, TEST_IDEMPOTENCY])           # duplicate fields
        rejected(["1" * 31])                                    # short
        rejected(["A" * 32])                                    # uppercase is non-canonical
        rejected(["console-" + "1" * 32])                     # stored namespace is server-owned
        assert not (Path(work) / "state" / "console_inbox.jsonl").exists()
    finally:
        httpd.shutdown(); httpd.server_close(); worker.join(timeout=5)


def test_http_retry_reuses_client_key_in_every_spool_record(seeded):
    """服务端不在只读进程中去重；重试须把同一键原样映射，留给单写 ingest 收敛。"""
    import threading
    import urllib.request

    path, work = seeded
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    opener.addheaders = [("Authorization", f"Bearer {TEST_CAPABILITY}")]
    httpd = CS.serve(path, work, SYSTEM_ROOT, host="127.0.0.1", port=0,
                     capability_token=TEST_CAPABILITY)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    client_key = "d" * 32

    def post():
        request = urllib.request.Request(
            base + "/api/message", method="POST",
            data=json.dumps({"text": "same operation"}).encode(),
            headers={"Content-Type": "application/json", "Idempotency-Key": client_key})
        return json.loads(opener.open(request, timeout=5).read())["queued"]

    try:
        first = post()
        second = post()
        expected = "console-" + client_key
        assert first["idempotency_key"] == second["idempotency_key"] == expected
        assert second["seq"] > first["seq"]
        records = [json.loads(line) for line in
                   (Path(work) / "state" / "console_inbox.jsonl").read_text(encoding="utf-8").splitlines()]
        assert [record["idempotency_key"] for record in records] == [expected, expected]
        assert [record["raw_text"] for record in records] == ["same operation", "same operation"]
    finally:
        httpd.shutdown(); httpd.server_close(); worker.join(timeout=5)


def test_custom_static_directory_cannot_publish_capability_dotfile(seeded, tmp_path):
    """即使运维误把含 secret 的目录配成 static_dir，dotfile 也不进入公开静态面。"""
    import threading
    import urllib.error
    import urllib.request

    path, work = seeded
    static = tmp_path / "custom-static"
    static.mkdir()
    (static / "index.html").write_text("PUBLIC", encoding="utf-8")
    secret = "must-never-be-served"
    (static / CSP.CAPABILITY_NAME).write_text(secret, encoding="utf-8")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    httpd = CS.serve(path, work, SYSTEM_ROOT, host="127.0.0.1", port=0,
                     static_dir=str(static), capability_token=TEST_CAPABILITY)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        assert opener.open(base + "/", timeout=5).read() == b"PUBLIC"
        with pytest.raises(urllib.error.HTTPError) as denied:
            opener.open(base + "/" + CSP.CAPABILITY_NAME, timeout=5)
        body = denied.value.read()
        assert denied.value.code == 403 and secret.encode() not in body
    finally:
        httpd.shutdown(); httpd.server_close(); worker.join(timeout=5)


def test_api_requires_unique_bearer_but_static_is_public(seeded):
    import http.client
    import threading
    import urllib.request

    path, work = seeded
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    httpd = CS.serve(path, work, SYSTEM_ROOT, host="127.0.0.1", port=0,
                     capability_token=TEST_CAPABILITY)
    port = httpd.server_address[1]
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{port}"
    try:
        assert b"<!doctype html>" in opener.open(base + "/", timeout=5).read()[:100].lower()
        with pytest.raises(urllib.error.HTTPError) as missing:
            opener.open(base + "/api/db", timeout=5)
        assert missing.value.code == 401
        wrong = urllib.request.Request(
            base + "/api/db", headers={"Authorization": f"Bearer {'b' * 64}"})
        with pytest.raises(urllib.error.HTTPError) as invalid:
            opener.open(wrong, timeout=5)
        assert invalid.value.code == 401

        good = urllib.request.Request(
            base + "/api/db", headers={"Authorization": f"bEaReR {TEST_CAPABILITY}"})
        assert json.loads(opener.open(good, timeout=5).read())["tables"]["goal"]

        hidden = urllib.request.Request(
            base + "/api/file?p=work/state/.console-capability",
            headers={"Authorization": f"Bearer {TEST_CAPABILITY}"})
        with pytest.raises(urllib.error.HTTPError) as secret:
            opener.open(hidden, timeout=5)
        assert secret.value.code == 404

        bad_host = urllib.request.Request(
            base + "/api/db", headers={"Authorization": f"Bearer {TEST_CAPABILITY}",
                                       "Host": "evil.invalid"})
        with pytest.raises(urllib.error.HTTPError) as rebound:
            opener.open(bad_host, timeout=5)
        assert rebound.value.code == 421

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.putrequest("GET", "/api/db", skip_host=True)
        conn.putheader("Host", f"127.0.0.1:{port}")
        conn.putheader("Host", f"127.0.0.1:{port}")
        conn.putheader("Authorization", f"Bearer {TEST_CAPABILITY}")
        conn.endheaders()
        response = conn.getresponse()
        assert response.status == 421                         # duplicate Host 不能留下代理/解析歧义
        response.read(); conn.close()

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.putrequest("GET", "/api/db")
        conn.putheader("Authorization", f"Bearer {TEST_CAPABILITY}")
        conn.putheader("Authorization", f"Bearer {TEST_CAPABILITY}")
        conn.endheaders()
        response = conn.getresponse()
        assert response.status == 401
        response.read(); conn.close()

        inbox = Path(work) / "state" / "console_inbox.jsonl"
        before_size = inbox.stat().st_size if inbox.exists() else 0
        body = b'{"text":"must-not-queue"}'
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.putrequest("POST", "/api/message")
        conn.putheader("Authorization", f"Bearer {TEST_CAPABILITY}")
        conn.putheader("Idempotency-Key", TEST_IDEMPOTENCY)
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(len(body)))
        conn.putheader("Origin", f"http://127.0.0.1:{port}")
        conn.putheader("Origin", "http://evil.invalid")
        conn.endheaders(body)
        response = conn.getresponse()
        assert response.status == 403                         # duplicate Origin 不能由 get() 任取其一
        response.read(); conn.close()
        assert (inbox.stat().st_size if inbox.exists() else 0) == before_size

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("PUT", "/api/message", headers={"Authorization": f"Bearer {TEST_CAPABILITY}"})
        response = conn.getresponse()
        assert response.status == 405 and response.getheader("Allow") == "GET, POST"
        response.read(); conn.close()

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("DELETE", "/api/message")
        response = conn.getresponse()
        assert response.status == 401                   # unsupported /api methods do not bypass auth as 501
        response.read(); conn.close()
    finally:
        httpd.shutdown(); httpd.server_close()


def test_http_json_framing_rejects_transfer_encoding_oversize_and_short_body(seeded):
    import http.client
    import socket
    import threading

    path, work = seeded
    httpd = CS.serve(path, work, SYSTEM_ROOT, host="127.0.0.1", port=0,
                     capability_token=TEST_CAPABILITY)
    port = httpd.server_address[1]
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    auth = f"Bearer {TEST_CAPABILITY}"
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.putrequest("POST", "/api/message")
        conn.putheader("Authorization", auth)
        conn.putheader("Idempotency-Key", TEST_IDEMPOTENCY)
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", "2")
        conn.putheader("Transfer-Encoding", "chunked")
        conn.endheaders(b"{}")
        response = conn.getresponse()
        assert response.status == 400 and b"Transfer-Encoding" in response.read()
        conn.close()

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.putrequest("POST", "/api/message")
        conn.putheader("Authorization", auth)
        conn.putheader("Idempotency-Key", TEST_IDEMPOTENCY)
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(CS._MAX_HTTP_BODY_BYTES + 1))
        conn.endheaders()
        response = conn.getresponse()
        assert response.status == 400 and b"65536" in response.read()
        conn.close()

        raw = socket.create_connection(("127.0.0.1", port), timeout=5)
        request = (
            "POST /api/message HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"Authorization: {auth}\r\n"
            f"Idempotency-Key: {TEST_IDEMPOTENCY}\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: 100\r\n"
            "Connection: close\r\n\r\n{}"
        ).encode("ascii")
        raw.sendall(request)
        raw.shutdown(socket.SHUT_WR)
        response_bytes = b""
        while True:
            chunk = raw.recv(4096)
            if not chunk:
                break
            response_bytes += chunk
        raw.close()
        assert b" 400 " in response_bytes.split(b"\r\n", 1)[0]
        assert "提前结束".encode("utf-8") in response_bytes
    finally:
        httpd.shutdown(); httpd.server_close()


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


def test_heartbeat_transcript_symlink_is_not_followed(seeded, tmp_path):
    path, work = seeded
    outside = tmp_path / "outside-transcript"
    outside.write_text("secret", encoding="utf-8")
    link = Path(work) / "state" / "hb-link"
    link.symlink_to(outside)
    d = db.connect(path)
    d.execute("INSERT INTO runner_call(cycle_id,phase,purpose,status,transcript_ref) "
              "VALUES (1,'bundle','t','running','state/hb-link')")
    d.commit(); d.close()
    assert CS.assemble_db(path, work, SYSTEM_ROOT)["live"]["heartbeat_age_s"] is None


def test_runner_output_projects_running_codex_cli_activity_without_reasoning(seeded):
    path, work = seeded
    d = db.connect(path)
    runner_call_id = d.execute(
        "INSERT INTO runner_call(cycle_id,phase,purpose,status,transcript_ref) "
        "VALUES (1,'bundle','bundle-live','running',NULL)").lastrowid
    d.commit()
    operation_id = "exec-" + "1" * 32
    capture = Path(work) / "state" / "executions" / f"capture-{operation_id}.stdout.bin"
    capture.parent.mkdir(parents=True, exist_ok=True)
    events = [
        {"type": "item.completed", "item": {
            "id": "hidden", "type": "reasoning", "text": "hidden-chain"}},
        {"type": "item.started", "item": {
            "id": "cmd-1", "type": "command_execution",
            "command": "curl -H 'Authorization: Bearer top-secret' /status",
            "aggregated_output": "", "status": "in_progress"}},
        {"type": "item.completed", "item": {
            "id": "cmd-1", "type": "command_execution",
            "command": "curl -H 'Authorization: Bearer top-secret' /status",
            "aggregated_output": "step 12/100", "exit_code": 0,
            "status": "completed"}},
    ]
    capture.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
    receipt = {
        "operation_id": operation_id,
        "capture_stdout_ref": str(capture),
    }
    projected = CS._runner_output(
        d, Path(work), running_authority={
            "runner_call_id": runner_call_id, "receipt": receipt})
    d.close()
    live = next(item for item in projected if item["kind"] == "live")
    assert live["runner_call_id"] == runner_call_id
    assert live["cycle_id"] == 1 and live["phase"] == "bundle"
    assert "step 12/100" in live["text"] and "命令执行完成" in live["text"]
    assert "top-secret" not in live["text"] and "[已遮蔽]" in live["text"]
    assert "hidden-chain" not in live["text"]
    activities = [item for item in projected if item["kind"] == "activity"]
    assert [item["activity_state"] for item in activities] == ["running", "completed"]
    assert len({item["key"] for item in activities}) == 2
    assert "开始执行命令" in activities[0]["text"]
    assert "命令完成（exit 0）" in activities[1]["text"]
    assert "step 12/100" in activities[1]["text"]
    assert all("top-secret" not in item["text"] for item in activities)


def test_codex_live_activity_uses_only_committed_public_short_events():
    envelope = {"files": {"plan.json": {"large": "x" * 5_000}}}
    lines = [
        {"type": "thread.started", "thread_id": "private-thread"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {
            "id": "hidden", "type": "reasoning", "text": "hidden-chain"}},
        {"type": "item.completed", "item": {
            "id": "answer", "type": "agent_message",
            "text": "```json\n" + json.dumps(envelope) + "\n```"}},
    ]
    raw = "\n".join(json.dumps(line) for line in lines) + "\n"
    raw += '{"type":"error","message":"unterminated-secret"}'
    activities = CS._codex_live_activity_events(raw)
    assert [item["activity_kind"] for item in activities] == [
        "lifecycle", "lifecycle", "message"]
    text = "\n".join(item["text"] for item in activities)
    assert "执行会话已建立" in text and "正在分析上下文" in text
    assert "plan.json" in text and "结构化产物" in text
    assert "hidden-chain" not in text and "private-thread" not in text
    assert "unterminated-secret" not in text and len(text) < 1_000


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


def test_cross_process_enqueue_uses_stable_claim_and_random_idempotency(tmp_path):
    """不同 server 进程必须共享 work-root claim；随机幂等键不依赖 spool 行号。"""
    import multiprocessing

    work = tmp_path / "work"
    work.mkdir()
    ctx = multiprocessing.get_context("fork")
    result_queue = ctx.Queue()
    workers = [ctx.Process(target=_process_append, args=(str(work), 25, f"p{i}", result_queue))
               for i in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(15)
        assert not worker.is_alive() and worker.exitcode == 0
    keys = []
    for _ in workers:
        result = result_queue.get(timeout=5)
        assert isinstance(result, list), result
        keys.extend(result)
    records = [json.loads(line) for line in (work / "state" / "console_inbox.jsonl").read_text().splitlines()]
    seqs = [record["seq"] for record in records]
    assert seqs == sorted(seqs) and len(set(seqs)) == 100 and seqs[0] == 1
    assert len(records) == len(set(keys)) == 100
    assert all(re.fullmatch(r"console-[0-9a-f]{32}", key) for key in keys)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_spool_rejects_symlink_hardlink_and_fifo(tmp_path, kind):
    work = tmp_path / "work"
    state = work / "state"
    state.mkdir(parents=True)
    inbox = state / "console_inbox.jsonl"
    target = tmp_path / "target"
    target.write_bytes(b"DO-NOT-TOUCH")
    if kind == "symlink":
        inbox.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, inbox)
    else:
        os.mkfifo(inbox)
    data = CS.ConsoleData(db_path=str(work / "unused.sqlite"), work_root=str(work),
                          system_root=SYSTEM_ROOT)
    with pytest.raises(OSError):
        data.enqueue_message("must fail closed")
    assert target.read_bytes() == b"DO-NOT-TOUCH"


def test_spool_rejects_state_and_claim_symlinks(tmp_path):
    work = tmp_path / "work"
    outside = tmp_path / "outside"
    work.mkdir(); outside.mkdir()
    (work / "state").symlink_to(outside, target_is_directory=True)
    data = CS.ConsoleData(db_path=str(work / "unused.sqlite"), work_root=str(work),
                          system_root=SYSTEM_ROOT)
    with pytest.raises(OSError):
        data.enqueue_message("must not leave work")
    assert list(outside.iterdir()) == []

    work2 = tmp_path / "work-claim"
    work2.mkdir()
    data2 = CS.ConsoleData(db_path=str(work2 / "unused.sqlite"), work_root=str(work2),
                           system_root=SYSTEM_ROOT)
    target = tmp_path / "claim-target"
    target.write_bytes(b"LOCK")
    (work2 / ".console-inbox.lock").symlink_to(target)
    with pytest.raises(OSError):
        data2.enqueue_message("must not lock alias")
    assert target.read_bytes() == b"LOCK"


def test_first_spool_append_fsyncs_creation_chain_and_sets_private_modes(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    calls = []
    real_fsync = os.fsync

    def trace_fsync(fd):
        info = os.fstat(fd)
        try:
            target = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            target = "?"
        calls.append((stat.S_IFMT(info.st_mode), target))
        real_fsync(fd)

    monkeypatch.setattr(CSP.os, "fsync", trace_fsync)
    data = CS.ConsoleData(db_path=str(work / "unused.sqlite"), work_root=str(work),
                          system_root=SYSTEM_ROOT)
    data.enqueue_message("durable")
    state = work / "state"
    inbox = state / "console_inbox.jsonl"
    claim = work / ".console-inbox.lock"
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert stat.S_IMODE(inbox.stat().st_mode) == 0o600
    assert stat.S_IMODE(claim.stat().st_mode) == 0o600
    targets = [target for _kind, target in calls]
    assert any(target.endswith("/work") for target in targets)
    assert any(target.endswith("/work/state") for target in targets)
    assert any(target.endswith("console_inbox.jsonl") for target in targets)
    assert any(target.endswith(".console-inbox.lock") for target in targets)


def test_append_does_not_rescan_spool_history(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    data = CS.ConsoleData(db_path=str(work / "unused.sqlite"), work_root=str(work),
                          system_root=SYSTEM_ROOT)
    first = data.enqueue_message("first")

    def forbid_full_read(*_args, **_kwargs):
        raise AssertionError("append must not read historical spool")

    monkeypatch.setattr(CSP, "_read_all", forbid_full_read)
    second = data.enqueue_message("second")
    assert second["seq"] > first["seq"]


def test_consumer_cursor_is_incremental_and_detects_same_inode_regrow(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    spool = CSP.ConsoleSpool(work)
    spool.append({"connector": "console", "raw_text": "first"})
    spool.append({"connector": "console", "raw_text": "second"})
    first_batch = spool.read_pending()
    assert len(first_batch.records) == 2
    first_end = first_batch.records[0].end_offset
    spool.write_cursor(first_batch, first_end)
    second_batch = spool.read_pending()
    assert second_batch.start_offset == first_end
    assert len(second_batch.records) == 1 and "second" in second_batch.records[0].line

    # Same inode, truncate, then regrow beyond the old numeric offset.  A bare
    # offset would silently skip the replacement prefix; the anchor must replay.
    replacement = b"X" * first_end + b"\n" + b'{"raw_text":"replacement"}\n'
    with spool.inbox_path.open("r+b") as inbox:
        inbox.truncate(0); inbox.write(replacement); inbox.flush(); os.fsync(inbox.fileno())
    replay = spool.read_pending()
    assert replay.start_offset == 0


def test_legacy_cursor_at_large_spool_eof_is_migrated_once(tmp_path, monkeypatch):
    work = tmp_path / "work"
    state = work / "state"
    state.mkdir(parents=True)
    spool = CSP.ConsoleSpool(work)
    lines = [json.dumps({"seq": i, "raw_text": "x" * 32}) for i in range(20_000)]
    spool.inbox_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    spool.inbox_path.chmod(0o600)
    cursor_path = state / "console_inbox.cursor"
    cursor_path.write_text(str(len(lines)), encoding="ascii")

    first = spool.read_pending()
    assert first.records == () and first.start_offset == spool.inbox_path.stat().st_size
    migrated = json.loads(cursor_path.read_text(encoding="ascii"))
    assert migrated["version"] == 1 and migrated["offset"] == spool.inbox_path.stat().st_size

    monkeypatch.setattr(
        CSP.ConsoleSpool, "_legacy_line_cursor",
        staticmethod(lambda *_args: (_ for _ in ()).throw(AssertionError("legacy rescan"))))
    second = spool.read_pending()
    assert second.records == () and second.start_offset == first.start_offset


def test_cursor_rejects_offset_not_returned_by_batch(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    spool = CSP.ConsoleSpool(work)
    spool.append({"raw_text": "one"})
    batch = spool.read_pending()
    with pytest.raises(ValueError, match="本批"):
        spool.write_cursor(batch, batch.start_offset + 1)


def test_consumer_oversized_committed_record_is_poison_not_wedge(tmp_path):
    work = tmp_path / "work"
    state = work / "state"
    state.mkdir(parents=True)
    spool = CSP.ConsoleSpool(work)
    valid = json.dumps({"connector": "console", "raw_text": "after"}).encode() + b"\n"
    spool.inbox_path.write_bytes(b"x" * (CSP.MAX_RECORD_BYTES + 1) + b"\n" + valid)
    spool.inbox_path.chmod(0o600)
    batch = spool.read_pending()
    assert len(batch.records) == 2
    assert batch.records[0].line is None and "超过" in batch.records[0].error
    assert json.loads(batch.records[1].line)["raw_text"] == "after"
    spool.write_cursor(batch, batch.records[0].end_offset)
    after = spool.read_pending()
    assert len(after.records) == 1 and json.loads(after.records[0].line)["raw_text"] == "after"


def test_bounded_batch_more_flag_counts_only_committed_following_record(tmp_path):
    work = tmp_path / "work"
    state = work / "state"
    state.mkdir(parents=True)
    spool = CSP.ConsoleSpool(work)
    record = b"x" * (CSP.MAX_RECORD_BYTES + 1) + b"\n"
    count = CSP.MAX_BATCH_BYTES // len(record) + 1
    spool.inbox_path.write_bytes(record * count + b"uncommitted-tail")
    spool.inbox_path.chmod(0o600)
    assert spool.read_pending().has_more_committed is False       # torn tail 不造成常驻假阻断
    with spool.inbox_path.open("ab") as inbox:
        inbox.write(b"\n"); inbox.flush(); os.fsync(inbox.fileno())
    assert spool.read_pending().has_more_committed is True        # 同一后缀落 LF 后才算 backlog


def test_retry_sidecar_is_bounded_private_and_corruption_fails_closed(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    spool = CSP.ConsoleSpool(work)
    spool.store_retry_counts({"console-a": 3})
    assert spool.load_retry_counts() == {"console-a": 3}
    retry_path = work / "state" / ".console_inbox.retry.json"
    assert stat.S_IMODE(retry_path.stat().st_mode) == 0o600
    retry_path.write_text("{broken", encoding="utf-8")
    retry_path.chmod(0o600)
    with pytest.raises(ValueError, match="损坏"):
        spool.load_retry_counts()


def test_capability_is_persistent_private_and_fail_closed(seeded):
    path, work_raw = seeded
    work = Path(work_raw)
    first = CS.serve(path, str(work), SYSTEM_ROOT, host="127.0.0.1", port=0,
                     capability_token=TEST_CAPABILITY)
    first.server_close()
    capability = work / "state" / CSP.CAPABILITY_NAME
    assert capability.read_text(encoding="ascii") == TEST_CAPABILITY
    assert stat.S_IMODE(capability.stat().st_mode) == 0o600
    assert capability.stat().st_nlink == 1

    second = CS.serve(path, str(work), SYSTEM_ROOT, host="127.0.0.1", port=0,
                      capability_token=TEST_CAPABILITY)
    second.server_close()
    with pytest.raises(OSError, match="不一致"):
        CS.serve(path, str(work), SYSTEM_ROOT, host="127.0.0.1", port=0,
                 capability_token="b" * 64)
    capability.chmod(0o644)
    with pytest.raises(OSError, match="0600"):
        CS.serve(path, str(work), SYSTEM_ROOT, host="127.0.0.1", port=0,
                 capability_token=TEST_CAPABILITY)


def test_browser_open_failure_prints_fragment_url_not_backend_capability_path(
        tmp_path, monkeypatch, capsys):
    import webbrowser

    opened = []
    monkeypatch.setattr(
        CS, "_configure_process_storage", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(CS, "serve_quests", lambda *_args, **_kwargs: _StoppedConsoleServer())
    monkeypatch.setattr(webbrowser, "open_new_tab", lambda url: opened.append(url) or False)

    assert CS.main([
        "--system-root", SYSTEM_ROOT,
        "--quests-root", str(tmp_path / "product-data"),
        "--host", "127.0.0.1", "--port", "0",
    ]) == 0

    expected = f"http://127.0.0.1:43123/#token={TEST_CAPABILITY}"
    output = capsys.readouterr().out
    assert opened == [expected]
    assert expected in output
    assert CSP.CAPABILITY_NAME not in output
    assert "工单/聊天记录" in output


def test_main_reports_storage_startup_error_without_traceback(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        CS, "_configure_process_storage",
        lambda *_args, **_kwargs: (
            _ for _ in ()).throw(ValueError("storage rejected")))

    assert CS.main([
        "--system-root", SYSTEM_ROOT,
        "--quests-root", str(tmp_path / "product-data"),
        "--no-open-browser",
    ]) == 2
    captured = capsys.readouterr()
    assert "存储绑定失败" in captured.err
    assert "storage rejected" in captured.err


def test_main_single_work_root_binds_shared_storage_to_its_parent(
        vepfs_tmp_path, monkeypatch):
    work = vepfs_tmp_path / "quest"
    before_environment = dict(os.environ)
    before_tempdir = tempfile.tempdir
    monkeypatch.delenv("METARESEARCH_STORAGE_ROOT", raising=False)
    monkeypatch.setattr(
        CS, "serve", lambda *_args, **_kwargs: _StoppedConsoleServer())
    try:
        assert CS.main([
            "--system-root", SYSTEM_ROOT, "--work-root", str(work),
            "--no-open-browser",
        ]) == 0
        assert os.environ["METARESEARCH_STORAGE_ROOT"] == str(vepfs_tmp_path)
        assert (vepfs_tmp_path / ".process-tmp").is_dir()
        assert not (work / ".process-tmp").exists()
    finally:
        os.environ.clear()
        os.environ.update(before_environment)
        tempfile.tempdir = before_tempdir


def test_main_explicit_quests_root_rejects_inherited_marker_without_side_effects(
        vepfs_tmp_path, monkeypatch, capsys):
    requested = vepfs_tmp_path / "requested"
    marker = vepfs_tmp_path / "other-process-root"
    before_environment = dict(os.environ)
    before_tempdir = tempfile.tempdir
    monkeypatch.setenv("METARESEARCH_STORAGE_ROOT", str(marker))
    monkeypatch.setattr(
        CS, "serve_quests",
        lambda *_args, **_kwargs: _StoppedConsoleServer())
    try:
        assert CS.main([
            "--system-root", SYSTEM_ROOT, "--quests-root", str(requested),
            "--no-open-browser",
        ]) == 2
        assert not marker.exists()
        assert not requested.exists()
        assert dict(os.environ) == {
            **before_environment, "METARESEARCH_STORAGE_ROOT": str(marker)}
        assert tempfile.tempdir == before_tempdir
        assert "存储绑定失败" in capsys.readouterr().err
    finally:
        os.environ.clear()
        os.environ.update(before_environment)
        tempfile.tempdir = before_tempdir


def test_main_non_loopback_rejection_precedes_storage_mutation(
        vepfs_tmp_path, monkeypatch, capsys):
    requested = vepfs_tmp_path / "registry"
    before_environment = dict(os.environ)
    before_tempdir = tempfile.tempdir
    monkeypatch.delenv("METARESEARCH_STORAGE_ROOT", raising=False)
    try:
        assert CS.main([
            "--system-root", SYSTEM_ROOT, "--quests-root", str(requested),
            "--host", "0.0.0.0", "--no-open-browser",
        ]) == 2
        assert not requested.exists()
        assert dict(os.environ) == {
            key: value for key, value in before_environment.items()
            if key != "METARESEARCH_STORAGE_ROOT"}
        assert tempfile.tempdir == before_tempdir
        assert "loopback" in capsys.readouterr().err
    finally:
        os.environ.clear()
        os.environ.update(before_environment)
        tempfile.tempdir = before_tempdir


def test_main_unknown_query_username_is_clean_storage_error(
        vepfs_tmp_path, monkeypatch, capsys):
    import orchestrator.runtime_storage as runtime_storage

    requested = vepfs_tmp_path / "registry"
    before_environment = dict(os.environ)
    before_tempdir = tempfile.tempdir
    monkeypatch.delenv("METARESEARCH_STORAGE_ROOT", raising=False)
    monkeypatch.setenv(
        "METARESEARCH_QUERY_RUN_AS_USER", "missing-storage-account")

    def missing_user(_name):
        raise KeyError("unknown username")

    monkeypatch.setattr(runtime_storage.pwd, "getpwnam", missing_user)
    try:
        assert CS.main([
            "--system-root", SYSTEM_ROOT, "--quests-root", str(requested),
            "--no-open-browser",
        ]) == 2
        assert not requested.exists()
        assert "存储绑定失败" in capsys.readouterr().err
    finally:
        os.environ.clear()
        os.environ.update(before_environment)
        tempfile.tempdir = before_tempdir


@pytest.mark.parametrize("error_type", [ValueError, OSError])
def test_main_reports_expected_serve_initialization_errors_cleanly(
        tmp_path, monkeypatch, capsys, error_type):
    monkeypatch.setattr(
        CS, "_configure_process_storage", lambda *_args, **_kwargs: {})

    def reject_serve(*_args, **_kwargs):
        raise error_type("registry rejected")

    monkeypatch.setattr(CS, "serve_quests", reject_serve)
    assert CS.main([
        "--system-root", SYSTEM_ROOT,
        "--quests-root", str(tmp_path / "product-data"),
        "--no-open-browser",
    ]) == 2
    captured = capsys.readouterr()
    assert "启动失败" in captured.err
    assert "registry rejected" in captured.err


def test_successful_browser_open_does_not_echo_bearer_to_terminal(
        tmp_path, monkeypatch, capsys):
    import webbrowser

    opened = []
    monkeypatch.setattr(
        CS, "_configure_process_storage", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(CS, "serve_quests", lambda *_args, **_kwargs: _StoppedConsoleServer())
    monkeypatch.setattr(webbrowser, "open_new_tab", lambda url: opened.append(url) or True)

    assert CS.main([
        "--system-root", SYSTEM_ROOT,
        "--quests-root", str(tmp_path / "product-data"),
    ]) == 0

    output = capsys.readouterr().out
    assert opened == [f"http://127.0.0.1:43123/#token={TEST_CAPABILITY}"]
    assert TEST_CAPABILITY not in output
    assert "已在默认浏览器打开" in output


def test_authenticated_console_url_keeps_token_in_fragment_and_brackets_ipv6():
    url = CS._authenticated_console_url("::1", 8765, TEST_CAPABILITY)
    assert url == f"http://[::1]:8765/#token={TEST_CAPABILITY}"
    assert "?" not in url


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_capability_rejects_nonexclusive_nonregular_entry(tmp_path, kind):
    work = tmp_path / "work"
    state = work / "state"
    state.mkdir(parents=True)
    capability = state / CSP.CAPABILITY_NAME
    target = tmp_path / "cap-target"
    target.write_text(TEST_CAPABILITY)
    if kind == "symlink":
        capability.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, capability)
    else:
        os.mkfifo(capability)
    spool = CSP.ConsoleSpool(work)
    with pytest.raises(OSError):
        spool.load_or_create_capability(TEST_CAPABILITY)
    assert target.read_text() == TEST_CAPABILITY


def test_pinned_upload_capability_survives_parent_swap(tmp_path):
    work = tmp_path / "work"
    system = tmp_path / "system"
    safe = work / "uploads" / "r1" / "1"
    safe.mkdir(parents=True)
    (safe / "data.bin").write_bytes(b"SAFE")
    outside = tmp_path / "outside"
    (outside / "r1" / "1").mkdir(parents=True)
    (outside / "r1" / "1" / "data.bin").write_bytes(b"SECRET")

    with CSP.open_pinned_upload_ref(
            "work/uploads/r1", work_root=work, system_root=system) as pinned:
        (work / "uploads").rename(work / "uploads-old")
        (work / "uploads").symlink_to(outside, target_is_directory=True)
        assert Path(pinned.proc_path, "1", "data.bin").read_bytes() == b"SAFE"
        assert Path(pinned.proc_path, "1", "data.bin").read_bytes() != b"SECRET"


def test_fd_read_is_bounded_and_not_retargeted_after_check(seeded, tmp_path, monkeypatch):
    path, work_raw = seeded
    work = Path(work_raw)
    (work / "cycles").mkdir()
    victim = work / "cycles" / "shown.txt"
    victim.write_bytes(b"SAFE")
    secret = tmp_path / "secret"
    secret.write_bytes(b"SECRET")
    data = CS.ConsoleData(db_path=path, work_root=str(work), system_root=SYSTEM_ROOT)
    original_verify = CSP._verify_entry_matches_fd
    swapped = False

    def swap_after_fd_check(parent_fd, name, fd, *, label, regular):
        nonlocal swapped
        info = original_verify(parent_fd, name, fd, label=label, regular=regular)
        if label == "读取目标" and not swapped:
            swapped = True
            victim.unlink()
            victim.symlink_to(secret)
        return info

    monkeypatch.setattr(CSP, "_verify_entry_matches_fd", swap_after_fd_check)
    assert data.read_file("work/cycles/shown.txt") == b"SAFE"
    assert swapped

    large = work / "cycles" / "large"
    large.write_bytes(b"x" * (CS._MAX_FILE_RESPONSE_BYTES + 1))
    assert data.read_file("work/cycles/large") is None


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_fd_read_rejects_symlink_hardlink_and_fifo(seeded, tmp_path, kind):
    path, work_raw = seeded
    work = Path(work_raw)
    target = tmp_path / "read-target"
    target.write_bytes(b"SECRET")
    (work / "cycles").mkdir()
    entry = work / "cycles" / "entry"
    if kind == "symlink":
        entry.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, entry)
    else:
        os.mkfifo(entry)
    data = CS.ConsoleData(db_path=path, work_root=str(work), system_root=SYSTEM_ROOT)
    assert data.read_file("work/cycles/entry") is None


def test_read_file_virtual_root_any_work_name(tmp_path):
    """codex SHOULD 回归：虚拟根 'work' 显式映射到 work_root（不管其真实目录叫什么名）——
    --work-root 叫 run123/scratch 等，前端按 FS 树根 'work/...' 拼的路径照样命中（非靠 base.parent 猜）。"""
    work = tmp_path / "run-abc-123"                    # work_root 目录名 != "work"
    (work / "cycles").mkdir(parents=True)
    (work / "cycles" / "x.txt").write_text("hi", encoding="utf-8")
    path = str(work / "research.sqlite")
    conn = db.connect(path); conn.commit(); conn.close()
    data = CS.ConsoleData(db_path=path, work_root=str(work), system_root=SYSTEM_ROOT)
    assert data.read_file("work/cycles/x.txt") == b"hi"     # 虚拟根 work → work_root，不管真名
    assert data.read_file("run-abc-123/cycles/x.txt") is None  # 真目录名不是虚拟根 → 拒
    assert data.read_file("unknownroot/x") is None            # 非白名单虚拟根 → 拒


def test_read_file_symlink_escape_blocked(tmp_path):
    """codex 关切：白名单目录内 symlink 指向外部 → resolve+containment 拒（不跟出根）。"""
    work = tmp_path / "work"; (work / "cycles").mkdir(parents=True)
    (tmp_path / "secret.txt").write_text("SECRET", encoding="utf-8")
    (work / "cycles" / "leak").symlink_to(tmp_path / "secret.txt")
    path = str(work / "research.sqlite"); db.connect(path).close()
    data = CS.ConsoleData(db_path=path, work_root=str(work), system_root=SYSTEM_ROOT)
    assert data.read_file("work/cycles/leak") is None      # symlink 解析后越界 → 拒


def test_notifications_drops_unterminated_tail(seeded):
    """codex SHOULD 回归：尾行无换行（append 中途）即便是合法 JSON 也丢——committed=换行终止。"""
    path, work = seeded
    ob = Path(work) / "state" / "outbox.jsonl"
    ob.write_text(json.dumps({"event_key": "a", "kind": "k", "payload": {"text": "done"}}) + "\n"
                  + json.dumps({"event_key": "b", "kind": "k",
                                "payload": {"text": "partial"}}), encoding="utf-8")  # 尾行无 \n
    got = CS.assemble_db(path, work, SYSTEM_ROOT)["notification"]
    assert [n["event_key"] for n in got] == ["a"]         # b 未终止 → 丢


def test_projected_db_rows_and_text_are_bounded_before_json(seeded):
    """高基数表和巨型 TEXT 在 SQLite→Python 边界即裁剪，不能只靠最终 JSON 总上限兜底。"""
    path, _work = seeded
    conn = db.connect(path)
    conn.executemany(
        "INSERT INTO decision(actor,type,payload_json) VALUES ('orchestrator',?,?)",
        [(f"bulk-{index}", "{}") for index in range(CS._ROW_CAP + 25)])
    oversized = "x" * (CS._MAX_DB_TEXT_CHARS + 123)
    newest_id = conn.execute(
        "INSERT INTO decision(actor,type,payload_json) VALUES ('orchestrator',?,?)",
        (oversized, oversized)).lastrowid
    conn.commit(); conn.close()

    ro = CS._open_ro(path, work_root=_work)
    try:
        rows = CS._rows(ro, "decision")
    finally:
        ro.close()
    assert len(rows) == CS._ROW_CAP
    assert rows[0]["id"] == newest_id                         # cap 取最新态，不是任意旧前缀
    assert rows[0]["type"] == oversized[:CS._MAX_DB_TEXT_CHARS]
    assert rows[0]["payload_json"] == oversized[:CS._MAX_DB_TEXT_CHARS]
    assert all(len(row["type"]) <= CS._MAX_DB_TEXT_CHARS for row in rows)


def test_assemble_db_applies_per_table_and_global_projection_byte_budgets(
        seeded, monkeypatch):
    """多张巨型 TEXT 表的 compact JSON 也只能达到全局预算加对象键名开销。"""
    path, work = seeded
    conn = db.connect(path)
    table_names = [f"projection_stress_{index:02d}" for index in range(14)]
    huge = "x" * CS._MAX_DB_TEXT_CHARS
    for table in table_names:
        conn.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY, body TEXT NOT NULL)')
        conn.executemany(f'INSERT INTO "{table}"(body) VALUES (?)', [(huge,)] * 9)
    conn.commit(); conn.close()
    monkeypatch.setattr(CS, "_PROJECT_TABLES", table_names)

    tables = CS.assemble_db(path, work, SYSTEM_ROOT)["tables"]
    compact = json.dumps(tables, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    for rows in tables.values():
        encoded = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        assert len(encoded) <= CS._MAX_DB_TABLE_BYTES

    # 全局预算统计的是各 list 本身；外层 object 另有固定的键名/冒号/逗号结构开销。
    empty_object = json.dumps(
        {name: [] for name in table_names}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    structural_overhead = len(empty_object) - 2 * len(table_names)
    assert len(compact) <= CS._MAX_DB_PROJECTION_BYTES + structural_overhead
    assert len(compact) > 4 * CS._MAX_DB_TABLE_BYTES          # 确实覆盖多表压力，不是空载假绿


def test_projection_budget_cannot_starve_pending_control_rows(seeded, monkeypatch):
    """研究表先后顺序/大量历史都不能把 actionable directive/request 挤出一个成功响应。"""
    path, work = seeded
    conn = db.connect(path)
    pending_id = conn.execute(
        "INSERT INTO directive(kind,hardness,status,consume_at,payload_json) "
        "VALUES ('pause','hard','pending','immediate','{\"confirmed\":false}')").lastrowid
    conn.executemany(
        "INSERT INTO directive(kind,hardness,status,consume_at,payload_json) "
        "VALUES ('note','soft','rejected','reasoning_start','{}')", [()] * (CS._ROW_CAP + 25))
    request_id = conn.execute(
        "INSERT INTO interaction_request(goal_id,goal_ver,stage,status,summary_md,items_json,request_hash) "
        "VALUES (1,1,'plan','pending','待用户供给','[]','projection-pending')").lastrowid
    stress = [f"projection_before_control_{index:02d}" for index in range(14)]
    huge = "x" * CS._MAX_DB_TEXT_CHARS
    for table in stress:
        conn.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY, body TEXT NOT NULL)')
        conn.executemany(f'INSERT INTO "{table}"(body) VALUES (?)', [(huge,)] * 9)
    conn.commit(); conn.close()
    monkeypatch.setattr(
        CS, "_PROJECT_TABLES",
        stress + ["directive", "interaction_request", "interaction_message",
                  "interaction_classification", "interaction_reply"])

    tables = CS.assemble_db(path, work, SYSTEM_ROOT)["tables"]
    assert any(row["id"] == pending_id and row["status"] == "pending"
               for row in tables["directive"])
    assert any(row["id"] == request_id and row["status"] == "pending"
               for row in tables["interaction_request"])
    assert len(tables["directive"]) == CS._ROW_CAP             # pending 优先 + 最近历史，而非纯最新


def test_ledger_policy_and_json_encoding_are_resource_bounded(seeded, tmp_path, monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE ledger(id INTEGER PRIMARY KEY, cycle_id INTEGER, money REAL)")
    conn.executemany("INSERT INTO ledger(cycle_id,money) VALUES (?,?)",
                     [(index, 1.0) for index in range(1, 11)])
    monkeypatch.setattr(CS, "_MAX_LEDGER_CYCLES", 3)
    assert [row["cycle"] for row in CS._ledger_by_cycle(conn)] == ["c8", "c9", "c10"]

    conn.execute("DELETE FROM ledger")
    conn.executemany("INSERT INTO ledger(cycle_id,money) VALUES (?,?)",
                     [(1, 1.0), (2, 1.0), (2, 2.0), (3, 1.0), (3, 2.0), (4, 4.0)])
    monkeypatch.setattr(CS, "_MAX_LEDGER_RECORDS", 4)
    assert CS._ledger_by_cycle(conn) == [
        {"cycle": "c3", "money": 3.0}, {"cycle": "c4", "money": 4.0}]
    # 尾窗切在 c2 中间；宁可省略 c2，也不能显示部分和 1.0 冒充完整成本。
    conn.close()

    path, work = seeded
    alias_root = tmp_path / "alias-policy"
    (alias_root / "policies").mkdir(parents=True)
    (alias_root / "policies" / "policy.yaml").write_text(
        "base: &base [x, y, z]\nexpanded: [*base, *base, *base]\n", encoding="utf-8")
    assert CS.assemble_db(path, work, str(alias_root))["policy"] == {}
    with pytest.raises(CS.JsonResponseTooLarge):
        CS._bounded_json_bytes(["x"] * 1000, max_bytes=32)


def test_fs_tree_stops_directory_iteration_at_per_directory_cap(tmp_path, monkeypatch):
    """目录条目预算必须限制实际迭代量，不能先 materialize 百万项再切片。"""
    work = tmp_path / "work"
    work.mkdir()
    for index in range(50):
        (work / f"entry-{index:03d}").write_text("x", encoding="utf-8")
    empty_system = tmp_path / "empty-system"
    empty_system.mkdir()
    original_scandir = CS.os.scandir
    yielded = 0

    class CountedScandir:
        def __init__(self, inner):
            self.inner = inner

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.inner.close()

        def __iter__(self):
            return self

        def __next__(self):
            nonlocal yielded
            entry = next(self.inner)
            yielded += 1
            return entry

    def counted_scandir(path):
        return CountedScandir(original_scandir(path))

    monkeypatch.setattr(CS.os, "scandir", counted_scandir)
    monkeypatch.setattr(
        CS, "_PUBLIC_WORK_FILES",
        frozenset(f"entry-{index:03d}" for index in range(50)))
    monkeypatch.setattr(CS, "_MAX_FS_ENTRIES_PER_DIRECTORY", 4)
    monkeypatch.setattr(CS, "_MAX_FS_NODES", 100)
    tree = CS._fs_tree(work, empty_system)
    # ``for`` 为判断 break 最多会多取一项，但仍是与目录规模无关的固定上界。
    assert yielded <= CS._MAX_FS_ENTRIES_PER_DIRECTORY + 1
    assert len(tree["roots"][0]["children"]) == 4


def test_fs_tree_does_not_follow_directory_swapped_to_symlink(tmp_path, monkeypatch):
    """symlink 检查之后的 rename 也不能让只读树递归到 work_root 之外。"""
    work = tmp_path / "work"
    victim = work / "cycles"
    victim.mkdir(parents=True)
    (victim / "safe.txt").write_text("SAFE", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    unique = "outside-only-secret.txt"
    (outside / unique).write_text("SECRET", encoding="utf-8")
    empty_system = tmp_path / "empty-system"
    empty_system.mkdir()
    original_open = CS.os.open
    swapped = False

    def swap_before_child_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if path == victim.name and dir_fd is not None and not swapped:
            swapped = True
            victim.rename(work / "cycles-safe-old")
            victim.symlink_to(outside, target_is_directory=True)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(CS.os, "open", swap_before_child_open)
    tree = CS._fs_tree(work, empty_system)

    def names(nodes):
        for node in nodes:
            yield node["p"]
            yield from names(node.get("children", []))

    assert swapped
    assert unique not in set(names(tree["roots"]))


def test_notifications_and_fs_tree_have_global_read_budgets(seeded, monkeypatch):
    path, work_raw = seeded
    work = Path(work_raw)
    outbox = work / "state" / "outbox.jsonl"
    outbox.write_bytes(b'{"event_key":"too-large"}\n' + b"x" * 100)
    monkeypatch.setattr(CS, "_MAX_NOTIFICATION_BYTES", 32)
    monkeypatch.setattr(CS, "_MAX_FS_NODES", 5)
    for i in range(20):
        (work / f"node-{i:02d}").write_text("x")
    payload = CS.assemble_db(path, str(work), SYSTEM_ROOT)
    assert payload["notification"] == []

    def count(nodes):
        return sum(1 + count(node.get("children", [])) for node in nodes)

    assert count(payload["fs"]["roots"]) <= 5 + len(payload["fs"]["roots"])


def test_api_db_error_generic(seeded, monkeypatch):
    """codex SHOULD 回归：/api/db 组装失败 → 泛化错误（不泄内部细节/路径）。"""
    import threading, urllib.request
    path, work = seeded
    monkeypatch.setattr(CS, "assemble_db", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("secret path /etc/x")))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    opener.addheaders = [("Authorization", f"Bearer {TEST_CAPABILITY}")]
    httpd = CS.serve(path, work, SYSTEM_ROOT, host="127.0.0.1", port=0,
                     capability_token=TEST_CAPABILITY)
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


def test_api_db_shared_reader_admission_failure_is_retryable_503(seeded, monkeypatch):
    import threading, urllib.request
    path, work = seeded
    monkeypatch.setattr(
        CS, "assemble_db",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            CS.SharedSQLiteReaderUnavailable("internal owner identity")))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    opener.addheaders = [("Authorization", f"Bearer {TEST_CAPABILITY}")]
    httpd = CS.serve(path, work, SYSTEM_ROOT, host="127.0.0.1", port=0,
                     capability_token=TEST_CAPABILITY)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        with pytest.raises(urllib.error.HTTPError) as denied:
            opener.open(base + "/api/db", timeout=5)
        body = denied.value.read().decode("utf-8")
        assert denied.value.code == 503
        assert "internal owner identity" not in body
    finally:
        httpd.shutdown()
        httpd.server_close()
        worker.join(timeout=5)
