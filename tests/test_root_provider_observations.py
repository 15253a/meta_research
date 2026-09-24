import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from meta_research.owners.common import canonical_hash
from meta_research.provider_supervisor import provider_operation_ref, write_transport_envelope
from meta_research.root_provider_observations import ProviderRootObservations


class ReadDatabase:
    def __init__(self):
        self.engine = create_engine("sqlite://")

    @contextmanager
    def read(self):
        with self.engine.connect() as connection:
            yield connection


@pytest.fixture
def setup(tmp_path):
    db = ReadDatabase()
    definitions = {
        "rg_quests": "quest_ref TEXT, initialization_id TEXT",
        "hc_deepfetch_requests": "request_ref TEXT,initialization_id TEXT,scope_json TEXT,scope_hash TEXT,draft_hash TEXT,created_at REAL",
        "hc_manual_deepfetch_requests": "request_ref TEXT,quest_ref TEXT,scope_json TEXT,scope_hash TEXT,quest_draft_hash TEXT,parent_question_ref TEXT,context_ref TEXT,created_at REAL",
        "ae_autonomous_deepfetch_requests": "request_ref TEXT,quest_ref TEXT,request_json TEXT,cycle_ref TEXT,reasoning_stage_run_request_ref TEXT,context_ref TEXT,created_at REAL",
        "ar_stage_runs": "request_ref TEXT,root_session_ref TEXT",
        "ar_deepfetch_runs": "run_ref TEXT,request_ref TEXT,correlation_ref TEXT,status TEXT,runtime_binding_hash TEXT,provider_operation_ref TEXT,provider_operation_generation INTEGER,created_at REAL,updated_at REAL",
        "ar_deepfetch_sessions": "run_ref TEXT,root_session_ref TEXT,native_session_ref TEXT",
        "ar_acquisition_sessions": "session_ref TEXT,initialization_id TEXT,quest_ref TEXT,status TEXT,current_request_id TEXT,config_hash TEXT,runtime_binding_hash TEXT,preflight_generation INTEGER,created_at REAL,updated_at REAL",
        "ar_acquisition_requests": "request_id TEXT,session_ref TEXT,status TEXT,created_at REAL,updated_at REAL,completed_at REAL",
    }
    with db.engine.begin() as conn:
        for name, columns in definitions.items():
            conn.execute(text(f"CREATE TABLE {name} ({columns})"))
        conn.execute(text("INSERT INTO rg_quests VALUES ('q1','init1'),('q2','init2')"))
    runtime = SimpleNamespace(data_root=SimpleNamespace(root=tmp_path), _database=db)
    return db, tmp_path, ProviderRootObservations(runtime)


def insert(db, table, row):
    with db.engine.begin() as conn:
        conn.execute(text(f"INSERT INTO {table} ({','.join(row)}) VALUES ({','.join(':'+k for k in row)})"), row)


def key_for(workspace):
    directory = workspace / "provider-operations"
    directory.mkdir(parents=True, exist_ok=True)
    key = b"x" * 32
    (directory / ".transport-seal.key").write_bytes(key)
    return key


def stdout(root="native1", label="根会话输出", child=False):
    events = [{"type": "thread.started", "thread_id": root}, {"type": "turn.started"},
              {"type": "item.completed", "item": {"type": "agent_message", "id": "m1", "text": label}}]
    if child:
        events += [{"type": "item.completed", "thread_id": "child1", "item": {"type": "agent_message", "text": "CHILD_SECRET"}},
                   {"type": "item.completed", "item": {"type": "agent_message", "thread_id": "child1", "text": "CHILD_SECRET2"}},
                   {"type": "thread.started", "thread_id": "child1", "parent_thread_id": root}]
    return "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in events)


def deepfetch(setup, *, quest="q1", root="df1", native="native1", autonomous=False):
    db, base, reader = setup
    run, request = "deepfetch_run_" + root, "request_" + root
    job = provider_operation_ref(run, "deepfetch", 1)
    scope = {"source_question_ref": "question1"} if autonomous else {"topic": "test"}
    scope_hash, draft_hash, binding = canonical_hash(scope), "d" * 64, "b" * 64
    if autonomous:
        insert(db, "ar_stage_runs", {"request_ref": "reason1", "root_session_ref": "reason-session"})
        insert(db, "ae_autonomous_deepfetch_requests", {"request_ref": request, "quest_ref": quest,
            "request_json": json.dumps({"scope": scope, "scope_hash": scope_hash, "draft_hash": draft_hash}),
            "cycle_ref": "cycle1", "reasoning_stage_run_request_ref": "reason1", "context_ref": "context1", "created_at": 1})
    else:
        insert(db, "hc_deepfetch_requests", {"request_ref": request, "initialization_id": "init" + quest[-1],
            "scope_json": json.dumps(scope), "scope_hash": scope_hash, "draft_hash": draft_hash, "created_at": 1})
    insert(db, "ar_deepfetch_runs", {"run_ref": run, "request_ref": request, "correlation_ref": "corr_" + root,
        "status": "running", "runtime_binding_hash": binding, "provider_operation_ref": job,
        "provider_operation_generation": 1, "created_at": 1, "updated_at": 2})
    insert(db, "ar_deepfetch_sessions", {"run_ref": run, "root_session_ref": root, "native_session_ref": native})
    workspace = base / "deepfetch-provider"
    key = key_for(workspace)
    material = {"schema_ref": "meta-research/deepfetch-provider-operation/v3", "job_ref": job,
        "request_ref": request, "correlation_ref": "corr_" + root, "draft_hash": draft_hash,
        "scope_hash": scope_hash, "runtime_binding_hash": binding, "native_session_ref": None,
        "segment_name": "initial"}
    directory = workspace / "provider-operations" / (binding + "-" + canonical_hash({"job_ref": job})) / "deepfetch-initial"
    directory.mkdir(parents=True)
    write_transport_envelope(directory / "invocation.json", material, key)
    (directory / "stdout.jsonl").write_text(stdout(native, child=True), encoding="utf-8")
    return directory, material, key


def acquisition(setup):
    db, base, _ = setup
    insert(db, "ar_acquisition_sessions", {"session_ref": "acq1", "initialization_id": "init1", "quest_ref": "q1",
        "status": "ready", "current_request_id": None, "config_hash": "c" * 64, "runtime_binding_hash": "a" * 64,
        "preflight_generation": 1, "created_at": 1, "updated_at": 3})
    workspace = base / "acquisition-root-provider"
    key = key_for(workspace)
    op_key = key_for(workspace / "codex-root")
    receipts = workspace / "root-sessions" / hashlib.sha256(b"acq1").hexdigest()
    receipts.mkdir(parents=True)
    for i in (1, 2):
        req = f"request{i}"
        insert(db, "ar_acquisition_requests", {"request_id": req, "session_ref": "acq1", "status": "obtained",
            "created_at": i, "updated_at": i + 1, "completed_at": i + 1})
        job = f"acquisition:acq1:batch:{req}"
        write_transport_envelope(receipts / f"turn-{i:08d}.json", {"schema_ref": "meta-research/acquisition-root-session/v1",
            "session_ref": "acq1", "generation": i, "native_session_ref": "acq-native",
            "previous_native_session_ref": None if i == 1 else "acq-native", "job_ref": job, "phase": "batch"}, key)
        directory = workspace / "codex-root/provider-operations" / canonical_hash({"job_ref": job}) / "acquisition-root-turn"
        directory.mkdir(parents=True)
        write_transport_envelope(directory / "invocation.json", {"job_ref": job, "operation_name": "acquisition-root-turn",
            "native_session_ref": None if i == 1 else "acq-native"}, op_key)
        (directory / "stdout.jsonl").write_text(stdout("acq-native", f"batch{i}"), encoding="utf-8")
    return receipts, key


def test_deepfetch_root_filter_and_utf8_pagination(setup):
    deepfetch(setup)
    reader = setup[2]
    session = reader.query("q1")["sessions"][0]
    assert session["session_ref"] == "df1"
    assert session["native_session_ref"] == "native1"
    operation = session["operations"][0]["operation_ref"]
    combined, after = "", 0
    for _ in range(200):
        page = reader.query_output("q1", "df1", operation_ref=operation, after=after, limit=17)
        assert page["offset"] == after
        assert page["next_offset"] - after == len(page["text"].encode())
        assert page["source_caught_up"] == (not page["has_more"])
        combined += page["text"]
        after = page["next_offset"]
        if not page["has_more"]:
            break
    assert "根会话输出" in combined
    assert "CHILD_SECRET" not in combined
    assert "child1" not in combined
    assert after == page["source_bytes"]


def test_quest_and_operation_isolation(setup):
    deepfetch(setup)
    deepfetch(setup, quest="q2", root="df2", native="native2")
    reader = setup[2]
    one, two = reader.query("q1")["sessions"], reader.query("q2")["sessions"]
    assert [s["session_ref"] for s in one] == ["df1"]
    assert [s["session_ref"] for s in two] == ["df2"]
    with pytest.raises(ValueError, match="root_session_not_found"):
        reader.query_output("q1", "df2", operation_ref=two[0]["operations"][0]["operation_ref"])
    with pytest.raises(ValueError, match="root_session_operation_not_found"):
        reader.query_output("q1", "df1", operation_ref=two[0]["operations"][0]["operation_ref"])


def test_signed_turn_registry_adds_operations_not_sessions(setup):
    directory, material, key = deepfetch(setup)
    root_dir = directory.parent
    registry = root_dir / "registered-turns"
    registry.mkdir()
    job = material["job_ref"] + ":v4-turn:14"
    write_transport_envelope(registry / "turn-14.json", {"schema_ref": "meta-research/deepfetch-provider-operation-registry/v1",
        "root_job_ref": material["job_ref"], "turn_number": 14, "provider_job_ref": job}, key)
    second = root_dir.parent / (material["runtime_binding_hash"] + "-" + canonical_hash({"job_ref": job})) / "deepfetch-initial"
    second.mkdir(parents=True)
    write_transport_envelope(second / "invocation.json", {**material, "job_ref": job, "native_session_ref": "native1"}, key)
    (second / "stdout.jsonl").write_text(stdout(label="后续回合"), encoding="utf-8")
    result = setup[2].query("q1")
    assert not result["limited"]
    assert len(result["sessions"]) == 1
    assert len(result["sessions"][0]["operations"]) == 2


def test_tampered_invocation_and_wrong_native_are_not_exposed(setup):
    directory, material, key = deepfetch(setup)
    (directory / "stdout.jsonl").write_text(stdout("other-native"), encoding="utf-8")
    result = setup[2].query("q1")
    assert result["limited"] and result["sessions"][0]["operations"] == []
    (directory / "stdout.jsonl").write_text(stdout(), encoding="utf-8")
    (directory / "invocation.json").unlink()
    write_transport_envelope(directory / "invocation.json", {**material, "request_ref": "other-request"}, key)
    result = setup[2].query("q1")
    assert result["limited"] and result["sessions"][0]["operations"] == []


def test_symlink_stdout_is_rejected(setup, tmp_path):
    directory, _, _ = deepfetch(setup)
    path = directory / "stdout.jsonl"
    path.unlink()
    external = tmp_path / "outside.jsonl"
    external.write_text(stdout(label="MUST_NOT_READ"), encoding="utf-8")
    path.symlink_to(external)
    result = setup[2].query("q1")
    assert result["limited"] and result["sessions"][0]["operations"] == []


def test_acquisition_reuses_one_quest_session_and_does_not_write(setup):
    acquisition(setup)
    reader = setup[2]
    before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in setup[1].rglob("*") if p.is_file()}
    result = reader.query("q1")
    assert not result["limited"]
    session = result["sessions"][0]
    assert session["session_ref"] == "acq1"
    assert session["native_session_ref"] == "acq-native"
    assert session["stage"] is None and session["related_stages"] == []
    assert len(session["operations"]) == 2
    assert session["status"] == "waiting"
    for operation in session["operations"]:
        reader.query_output("q1", "acq1", operation_ref=operation["operation_ref"])
    after = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in setup[1].rglob("*") if p.is_file()}
    assert before == after


def test_acquisition_chain_cannot_jump_native_session(setup):
    receipts, key = acquisition(setup)
    path = receipts / "turn-00000002.json"
    payload = json.loads(path.read_text())["payload"]
    path.unlink()
    write_transport_envelope(path, {**payload, "native_session_ref": "unrelated"}, key)
    result = setup[2].query("q1")
    assert result["limited"] and not result["sessions"]


def test_nonterminal_deepfetch_without_bound_process_is_waiting(setup):
    deepfetch(setup)
    session = setup[2].query("q1")["sessions"][0]
    assert session["status"] == "waiting"
    assert not session["is_executing"]
    assert session["operations"][0]["status"] == "waiting"


def test_acquisition_download_activity_does_not_require_a_root_model_turn(setup):
    acquisition(setup)
    db = setup[0]
    with db.engine.begin() as conn:
        conn.execute(text("UPDATE ar_acquisition_sessions SET status='acquiring', current_request_id='next-request'"))
        conn.execute(text("INSERT INTO ar_acquisition_requests VALUES ('next-request','acq1','running',4,4,NULL)"))
    session = setup[2].query("q1")["sessions"][0]
    assert session["is_executing"]
    assert session["activity_label"] == "正在获取资料"
    assert len(session["operations"]) == 2


def test_autonomous_deepfetch_uses_source_reasoning_association(setup):
    deepfetch(setup, autonomous=True)
    session = setup[2].query("q1")["sessions"][0]
    assert session["stage"] == "reasoning"
    assert session["related_stages"] == ["reasoning"]
    assert session["owner_session_ref"] == "reason-session"
    assert session["cycle_ref"] == "cycle1"
    assert session["question_ref"] == "question1"


def test_terminal_stdout_hash_prevents_replaced_content(setup):
    directory, material, key = deepfetch(setup)
    path = directory / "stdout.jsonl"
    write_transport_envelope(directory / "supervisor-exit.json", {"invocation_hash": canonical_hash(material),
        "returncode": 0, "termination_reason": "completed", "stdout_file_hash": hashlib.sha256(path.read_bytes()).hexdigest()}, key)
    reader = setup[2]
    session = reader.query("q1")["sessions"][0]
    assert session["status"] == "waiting" and not session["is_executing"]
    operation = session["operations"][0]["operation_ref"]
    assert reader.query_output("q1", "df1", operation_ref=operation)["status"] == "terminal"
    path.write_text(stdout(label="modified"), encoding="utf-8")
    with pytest.raises(ValueError, match="root_session_output_integrity_invalid"):
        reader.query_output("q1", "df1", operation_ref=operation)
