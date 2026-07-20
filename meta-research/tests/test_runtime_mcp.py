from __future__ import annotations

import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from orchestrator import database
from orchestrator import manifest as manifest_module
from orchestrator.cycle_replay import CycleReplayArchive
from orchestrator.interfaces import Artifact, ManagedArtifactRef
from orchestrator.runtime_mcp import (
    RuntimeIngestService,
    RuntimeMCPBroker,
    RuntimeMCPError,
)
from orchestrator.schemas import SchemaSet
from orchestrator.writedaemon import WriteDaemon


SYSTEM_ROOT = Path(__file__).resolve().parent.parent


def _runtime_db(tmp_path, *, cycle_status="created"):
    conn = database.connect(str(tmp_path / "research.sqlite"))
    daemon = WriteDaemon(conn)
    with daemon.transaction() as db:
        db.execute(
            "INSERT INTO goal(id,version,text,predicate_json) VALUES (1,1,'goal','{}')")
        db.execute(
            "INSERT INTO cycle(id,goal_id,goal_ver,status,route,policy_version) "
            "VALUES (1,1,1,?,'attack','test')", (cycle_status,))
        qid = db.execute(
            "INSERT INTO question(goal_id,goal_ver,born_goal_ver,text,predicate_json,"
            "status,source,born_cycle,active_cycle) "
            "VALUES (1,1,1,'question','{}','active','agent',1,1)").lastrowid
        db.execute("UPDATE cycle SET active_question_id=? WHERE id=1", (qid,))
    return conn, daemon


def _broker_call(path: str, token: str, tool: str, arguments: dict):
    address = ("\0" + path[1:]) if path.startswith("@") else path
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(address)
        client.sendall((json.dumps({
            "token": token, "tool": tool, "arguments": arguments,
        }) + "\n").encode("utf-8"))
        return json.loads(client.makefile("rb").readline())


def _valid_idea(marker: str) -> dict:
    idea = json.loads((
        SYSTEM_ROOT / "tests" / "fixtures" / "valid" /
        "idea_set" / "bypass.json").read_text(encoding="utf-8"))
    idea["candidates"][0]["core_claim"] += f" [{marker}]"
    return idea


class _BarrierRuntimeIngestService(RuntimeIngestService):
    """Pause one valid submission after validation but before persistence."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.persist_entered = threading.Event()
        self.persist_release = threading.Event()

    def _persist_stage_submission(self, scope, files, md, **kwargs):  # noqa: ANN001
        if md == "slow-before-persist":
            self.persist_entered.set()
            if not self.persist_release.wait(timeout=5):
                raise AssertionError("test did not release stage persistence barrier")
        return super()._persist_stage_submission(
            scope, files, md, **kwargs)


class _PostCommitBarrierRuntimeIngestService(RuntimeIngestService):
    """Pause after the fenced database commit but before broker publication."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.committed = threading.Event()
        self.release = threading.Event()

    def _persist_stage_submission(self, scope, files, md, **kwargs):  # noqa: ANN001
        result = super()._persist_stage_submission(scope, files, md, **kwargs)
        self.committed.set()
        if not self.release.wait(timeout=5):
            raise AssertionError("test did not release post-commit barrier")
        return result


def test_runtime_mcp_returns_identity_conflict_to_same_turn(tmp_path):
    conn, daemon = _runtime_db(tmp_path, cycle_status="idea")
    runtime_root = tmp_path / "runtime" / "mcp"
    broker = RuntimeMCPBroker(
        RuntimeIngestService(
            daemon, schemas=SchemaSet(SYSTEM_ROOT / "schemas"),
            work_root=tmp_path), runtime_root=runtime_root).start()
    try:
        assert (broker.socket_path.startswith("@meta-research-mcp-")
                or broker.socket_path.startswith(str(runtime_root) + "/"))
        token = broker.grant(
            cycle_id="c1", stage="plan", target_id=None, purpose="plan-n1")
        plan = json.loads((
            SYSTEM_ROOT / "tests" / "fixtures" / "valid" /
            "plan" / "attack.json").read_text(encoding="utf-8"))
        preflight = _broker_call(
            broker.socket_path, token, "preflight_plan", {"plan": plan})
        assert preflight["ok"] is True
        assert preflight["writes_performed"] == 0
        assert daemon.query_one("SELECT count(*) FROM baseline") == (0,)
        with daemon.transaction() as db:
            db.execute(
                "INSERT INTO baseline(slug,canonical_key,born_cycle,status) "
                "VALUES ('different','logreg-gauss2d',1,'planned')")
        conflict = _broker_call(
            broker.socket_path, token, "preflight_plan", {"plan": plan})
        assert conflict["ok"] is False
        assert "identity 冲突" in conflict["error"]

        review = _broker_call(broker.socket_path, token, "record_review", {
            "review_kind": "plan", "verdict": "fail",
            "summary_md": "revise", "issues": ["missing control"],
        })
        assert review["ok"] is True
        repeated_review = _broker_call(broker.socket_path, token, "record_review", {
            "review_kind": "plan", "verdict": "fail",
            "summary_md": "revise", "issues": ["missing control"],
        })
        assert repeated_review["ok"] is True
        assert repeated_review["created"] is False
        assert repeated_review["decision_id"] == review["decision_id"]
        assert daemon.query_one(
            "SELECT count(*) FROM decision WHERE type='runtime_review'") == (1,)

        broker.revoke(token)
        revoked = _broker_call(
            broker.socket_path, token, "get_runtime_index", {})
        assert revoked["ok"] is False
        assert "撤销" in revoked["error"]
    finally:
        broker.close()
        conn.close()


def test_runtime_mcp_reasoning_summary_is_stage_scoped(tmp_path):
    conn, daemon = _runtime_db(tmp_path)
    broker = RuntimeMCPBroker(RuntimeIngestService(daemon)).start()
    try:
        token = broker.grant(
            cycle_id="c1", stage="reasoning", target_id=None,
            purpose="reasoning-n1")
        result = _broker_call(broker.socket_path, token, "record_cycle_summary", {
            "conclusion_md": "cycle summary", "decision": "continue",
            "next_step_md": "next cycle", "evidence_refs": [],
        })
        assert result["ok"] is True
        repeated = _broker_call(broker.socket_path, token, "record_cycle_summary", {
            "conclusion_md": "cycle summary", "decision": "continue",
            "next_step_md": "next cycle", "evidence_refs": [],
        })
        assert repeated["ok"] is True
        assert repeated["created"] is False
        assert repeated["decision_id"] == result["decision_id"]
        assert daemon.query_one(
            "SELECT count(*) FROM decision "
            "WHERE type='runtime_cycle_summary'") == (1,)
        payload = json.loads(daemon.query_one(
            "SELECT payload_json FROM decision "
            "WHERE type='runtime_cycle_summary'")[0])
        assert payload["decision"] == "continue"
        assert payload["revision"] == 1
    finally:
        broker.close()
        conn.close()


def test_runtime_mcp_long_project_path_uses_abstract_socket(tmp_path):
    conn, daemon = _runtime_db(tmp_path)
    runtime_root = tmp_path / ("very-long-project-segment-" * 4) / "runtime" / "mcp"
    broker = RuntimeMCPBroker(
        RuntimeIngestService(daemon), runtime_root=runtime_root).start()
    try:
        assert broker.socket_path.startswith("@meta-research-mcp-")
        token = broker.grant(
            cycle_id="c1", stage="plan", target_id=None, purpose="long-path")
        result = _broker_call(
            broker.socket_path, token, "get_runtime_index", {})
        assert result["ok"] is True
        assert result["cycle_id"] == "c1"
    finally:
        broker.close()
        conn.close()


def test_runtime_mcp_stage_submission_rejects_then_accepts_in_same_capability(tmp_path):
    conn, daemon = _runtime_db(tmp_path)
    schemas = SchemaSet(SYSTEM_ROOT / "schemas")
    service = RuntimeIngestService(
        daemon, schemas=schemas, work_root=tmp_path)
    broker = RuntimeMCPBroker(service).start()
    try:
        token = broker.grant(
            cycle_id="c1", stage="idea", target_id=None,
            purpose="idea-main-c1-n1-a1", pack_hash="a" * 64)
        rejected = _broker_call(
            broker.socket_path, token, "submit_stage_artifact",
            {"files": {"idea_set.json": {"bad": True}}})
        assert rejected["ok"] is False
        assert "idea_set.json schema 校验失败" in rejected["error"]
        assert broker.latest_stage_submission(token) is None
        assert daemon.query_one(
            "SELECT count(*) FROM decision "
            "WHERE type='runtime_stage_submission'") == (0,)

        idea = json.loads((
            SYSTEM_ROOT / "tests" / "fixtures" / "valid" /
            "idea_set" / "bypass.json").read_text(encoding="utf-8"))
        accepted = _broker_call(
            broker.socket_path, token, "submit_stage_artifact",
            {"files": {"idea_set.json": idea}})
        assert accepted["ok"] is True
        loaded = broker.latest_stage_submission(token)
        assert loaded["files"] == {"idea_set.json": idea}
        assert loaded["artifact_hash"] == accepted["artifact_hash"]

        payload_json = daemon.query_one(
            "SELECT payload_json FROM decision "
            "WHERE type='runtime_stage_submission'")[0]
        payload = json.loads(payload_json)
        assert payload["submission_ref"].startswith(
            str(tmp_path / "runtime" / "stage-submissions"))
        assert payload["artifact_hash"] == accepted["artifact_hash"]
        assert "core_claim" not in payload_json
        assert Path(payload["submission_ref"]).is_file()
    finally:
        broker.close()
        conn.close()


def test_configured_child_review_is_required_in_same_live_capability(tmp_path):
    conn, daemon = _runtime_db(tmp_path)
    service = RuntimeIngestService(
        daemon, schemas=SchemaSet(SYSTEM_ROOT / "schemas"),
        policy={"flow": {"retry": {"plan_review": 1}}},
        work_root=tmp_path)
    broker = RuntimeMCPBroker(service).start()
    try:
        token = broker.grant(
            cycle_id="c1", stage="idea", target_id=None,
            purpose="idea-main-c1-n1-a1", ttl_s=None,
            pack_hash="a" * 64)
        idea = _valid_idea("review-required")
        missing = _broker_call(
            broker.socket_path, token, "submit_stage_artifact",
            {"files": {"idea_set.json": idea}})
        assert missing["ok"] is False
        assert "record_review(review_kind=idea)" in missing["error"]
        assert daemon.query_one(
            "SELECT count(*) FROM decision "
            "WHERE type='runtime_stage_submission'") == (0,)

        review = _broker_call(broker.socket_path, token, "record_review", {
            "review_kind": "idea", "verdict": "fail",
            "summary_md": "one independent review; main revised the draft",
            "issues": ["tighten falsification criterion"],
        })
        assert review["ok"] is True
        accepted = _broker_call(
            broker.socket_path, token, "submit_stage_artifact",
            {"files": {"idea_set.json": idea}})
        assert accepted["ok"] is True
        receipt = json.loads(Path(
            accepted["submission_ref"]).read_text(encoding="utf-8"))
        assert receipt["review_decision_id"] == review["decision_id"]
    finally:
        broker.close()
        conn.close()


def test_later_invalid_submit_fences_slow_valid_submit_from_latest(tmp_path):
    conn, daemon = _runtime_db(tmp_path)
    service = _BarrierRuntimeIngestService(
        daemon, schemas=SchemaSet(SYSTEM_ROOT / "schemas"), work_root=tmp_path)
    broker = RuntimeMCPBroker(service).start()
    try:
        token = broker.grant(
            cycle_id="c1", stage="idea", target_id=None,
            purpose="idea-concurrent-invalid", pack_hash="a" * 64)
        with ThreadPoolExecutor(max_workers=1) as pool:
            slow = pool.submit(
                _broker_call, broker.socket_path, token,
                "submit_stage_artifact", {
                    "files": {"idea_set.json": _valid_idea("slow-a")},
                    "md": "slow-before-persist",
                })
            try:
                assert service.persist_entered.wait(timeout=5)
                later = _broker_call(
                    broker.socket_path, token, "submit_stage_artifact",
                    {"files": {"idea_set.json": {"bad": True}}})
            finally:
                service.persist_release.set()
            earlier = slow.result(timeout=5)

        assert earlier["ok"] is True
        assert later["ok"] is False
        assert "schema 校验失败" in later["error"]
        assert broker.latest_stage_submission(token) is None
        assert daemon.query_one(
            "SELECT count(*) FROM decision "
            "WHERE type='runtime_stage_submission'") == (1,)
    finally:
        service.persist_release.set()
        broker.close()
        conn.close()


def test_concurrent_valid_submits_get_unique_revisions_and_later_request_is_latest(
        tmp_path):
    conn, daemon = _runtime_db(tmp_path)
    service = _BarrierRuntimeIngestService(
        daemon, schemas=SchemaSet(SYSTEM_ROOT / "schemas"), work_root=tmp_path)
    broker = RuntimeMCPBroker(service).start()
    later_idea = _valid_idea("later-b")
    try:
        token = broker.grant(
            cycle_id="c1", stage="idea", target_id=None,
            purpose="idea-concurrent-valid", pack_hash="b" * 64)
        with ThreadPoolExecutor(max_workers=1) as pool:
            slow = pool.submit(
                _broker_call, broker.socket_path, token,
                "submit_stage_artifact", {
                    "files": {"idea_set.json": _valid_idea("slow-a")},
                    "md": "slow-before-persist",
                })
            try:
                assert service.persist_entered.wait(timeout=5)
                later = _broker_call(
                    broker.socket_path, token, "submit_stage_artifact",
                    {"files": {"idea_set.json": later_idea}})
            finally:
                service.persist_release.set()
            earlier = slow.result(timeout=5)

        assert earlier["ok"] is True and later["ok"] is True
        assert {earlier["revision"], later["revision"]} == {1, 2}
        latest = broker.latest_stage_submission(token)
        assert latest["files"]["idea_set.json"] == later_idea
        revisions = [
            json.loads(row[0])["revision"]
            for row in daemon.query(
                "SELECT payload_json FROM decision "
                "WHERE type='runtime_stage_submission'")
        ]
        assert sorted(revisions) == [1, 2]
    finally:
        service.persist_release.set()
        broker.close()
        conn.close()


def test_revoke_fences_inflight_submit_before_database_commit(tmp_path):
    conn, daemon = _runtime_db(tmp_path)
    service = _BarrierRuntimeIngestService(
        daemon, schemas=SchemaSet(SYSTEM_ROOT / "schemas"), work_root=tmp_path)
    broker = RuntimeMCPBroker(service).start()
    try:
        token = broker.grant(
            cycle_id="c1", stage="idea", target_id=None,
            purpose="idea-revoke-inflight", pack_hash="c" * 64)
        with ThreadPoolExecutor(max_workers=1) as pool:
            inflight = pool.submit(
                _broker_call, broker.socket_path, token,
                "submit_stage_artifact", {
                    "files": {"idea_set.json": _valid_idea("revoked")},
                    "md": "slow-before-persist",
                })
            try:
                assert service.persist_entered.wait(timeout=5)
                broker.revoke(token)
            finally:
                service.persist_release.set()
            result = inflight.result(timeout=5)

        assert result["ok"] is False
        assert "撤销" in result["error"]
        assert daemon.query_one(
            "SELECT count(*) FROM decision "
            "WHERE type='runtime_stage_submission'") == (0,)
        with pytest.raises(RuntimeMCPError, match="撤销"):
            broker.latest_stage_submission(token)
    finally:
        service.persist_release.set()
        broker.close()
        conn.close()


def test_runtime_mcp_bundle_submission_keeps_code_path_backed(tmp_path):
    conn, daemon = _runtime_db(tmp_path, cycle_status="plan")
    schemas = SchemaSet(SYSTEM_ROOT / "schemas")
    manifest = json.loads((
        SYSTEM_ROOT / "tests" / "fixtures" / "valid" /
        "execution_manifest" / "build_toy.json").read_text(encoding="utf-8"))
    plan_slice = {
        "target_key": manifest["target_ref"]["target_key"],
        "target_kind": manifest["target_ref"]["target_kind"],
        "seq": manifest["target_ref"]["seq"],
        "protocol_id": manifest["protocol_ref"]["protocol_id"],
        "protocol_ver": manifest["protocol_ref"]["protocol_ver"],
        "gpu_required": manifest.get("gpu_required", False),
        "claim": {},
    }
    manifest["target_ref"]["plan_slice_hash"] = manifest_module.canon_hash(
        plan_slice)
    with daemon.transaction() as db:
        db.execute(
            "INSERT INTO build_target(id,cycle_id,question_id,target_kind,seq,status,"
            "plan_ref) VALUES (7,1,1,'build',1,'pending',?)",
            (json.dumps(plan_slice, ensure_ascii=False, sort_keys=True),))

    runtime = tmp_path / "one-turn"
    submission = runtime / "submission"
    submission.mkdir(parents=True)
    payloads = {
        "execution_manifest.json": json.dumps(manifest, ensure_ascii=False),
        "identity.md": "# toy\n\n## 复现命令\npython train.py\n",
        "train.py": "print('train')\n",
        "eval.py": "print('eval')\n",
        "cfg.json": json.dumps({"lr": 0.1}),
    }
    for name, body in payloads.items():
        (submission / name).write_text(body, encoding="utf-8")

    class FakeBundleController:
        def __init__(self):
            self.executions = []
            self.bound = False

        def bind_next_bundle_target(self, _scope):
            self.bound = True
            return {"cycle_complete": False, "build_target_id": 7,
                    "context_pack": {"pack_hash": "c" * 64}}

        def bundle_session_scope(self, _scope):
            if not self.bound:
                raise RuntimeError("call next first")
            return {"target_id": 7, "pack_hash": "c" * 64,
                    "refs": ["request-ref-7"]}

        def execute_bundle_session(self, scope, files):
            self.executions.append((scope, files))
            return {"build_target_id": 7, "status": "complete", "terminal": True}

        def bundle_session_status(self, _scope):
            return {"build_target_id": 7, "status": "pending", "terminal": False}

        def request_bundle_repair(self, _scope, diagnosis):
            return {"build_target_id": 7, "status": "running",
                    "terminal": False, "diagnosis_md": diagnosis}

        def replan_bundle_session(self, _scope, diagnosis):
            return {"build_target_id": 7, "status": "failed", "terminal": True,
                    "diagnosis_md": diagnosis}

    controller = FakeBundleController()
    service = RuntimeIngestService(
        daemon, schemas=schemas, work_root=tmp_path)
    service.bind_bundle_controller(controller)
    broker = RuntimeMCPBroker(service).start()
    try:
        token = broker.grant(
            cycle_id="c1", stage="bundle", target_id="7",
            purpose="bundle-c1-t7-n1-a1", pack_hash="b" * 64,
            workspace_root=runtime, output_uid=runtime.stat().st_uid)
        bound = _broker_call(
            broker.socket_path, token, "bundle_next_target", {})
        assert bound["ok"] is True and bound["build_target_id"] == 7
        accepted = _broker_call(
            broker.socket_path, token, "submit_stage_artifact", {
                "files": {},
                "workspace_files": [f"submission/{name}" for name in payloads],
            })
        assert accepted["ok"] is True
        receipt = json.loads(Path(accepted["submission_ref"]).read_text(encoding="utf-8"))
        assert receipt["target_id"] == "7"
        assert receipt["purpose"] == "bundle-c1-t7-n1-a1"
        assert receipt["pack_hash"] == "c" * 64
        loaded = broker.latest_stage_submission(token)
        assert loaded["target_id"] == "7"
        assert loaded["pack_hash"] == "c" * 64
        assert loaded["files"]["execution_manifest.json"] == manifest
        assert loaded["files"]["identity.md"].startswith("# toy")
        assert isinstance(loaded["files"]["train.py"], ManagedArtifactRef)
        code = loaded["files"]["train.py"]
        assert code.path.startswith(str(tmp_path / "runtime" / "stage-submissions"))
        assert Path(code.path).read_text(encoding="utf-8") == "print('train')\n"
        replay = CycleReplayArchive(tmp_path, submission_registry=daemon)
        replay_result = replay.persist_stage_artifact(
            cycle_id="c1", stage="bundle", target_id="7",
            purpose="bundle-c1-t7", pack_hash="c" * 64,
            artifact=Artifact(
                stage="bundle", files=loaded["files"], md=loaded["md"],
                stage_submission_ref=loaded["submission_ref"],
                stage_submission_hash=loaded["artifact_hash"]))
        assert (tmp_path / "cycles" / "c1" / "artifacts" / "history" /
                replay_result["event_id"] / "managed-files.json").is_file()
        decision = json.loads(daemon.query_one(
            "SELECT payload_json FROM decision "
            "WHERE type='runtime_stage_submission'")[0])
        assert decision["file_names"] == sorted(payloads)
        assert "print('train')" not in json.dumps(decision)

        executed = _broker_call(
            broker.socket_path, token, "bundle_execute", {
                "submission_ref": accepted["submission_ref"],
                "submission_hash": accepted["submission_hash"],
            })
        assert executed == {
            "ok": True, "build_target_id": 7,
            "status": "complete", "terminal": True,
        }
        assert len(controller.executions) == 1
        assert controller.executions[0][0].pack_hash == "c" * 64
        assert controller.executions[0][0].refs == ("request-ref-7",)
        assert isinstance(controller.executions[0][1]["train.py"], ManagedArtifactRef)
        status = _broker_call(broker.socket_path, token, "bundle_status", {})
        assert status["ok"] is True and status["terminal"] is False
        rebound = _broker_call(
            broker.socket_path, token, "bundle_next_target", {})
        assert rebound["ok"] is True and rebound["cycle_complete"] is False
        assert broker.latest_stage_submission(token) is None
    finally:
        broker.close()
        conn.close()


def test_runtime_tool_inventory_has_read_only_plan_preflight_not_early_claim():
    tools = {item["name"]: item for item in RuntimeIngestService.tools()}
    assert "register_baseline" not in tools
    assert tools["preflight_plan"]["annotations"]["readOnlyHint"] is True
    assert {"plan_import_search", "wildidea_expand", "wildidea_search"}.issubset(tools)
    assert {"bundle_next_target", "bundle_execute", "bundle_status",
            "bundle_repair", "bundle_replan"}.issubset(tools)


def test_plan_import_search_returns_to_same_capability_and_semantic_preflight(
        tmp_path):
    conn, daemon = _runtime_db(tmp_path, cycle_status="idea")

    class FakePlanController:
        def __init__(self):
            self.searches = []
            self.preflights = []

        def run_plan_import_search(self, scope, request):
            self.searches.append((scope, request))
            return {
                "cycle_id": scope.cycle_id,
                "search": {"candidate_count": 1},
                "context_pack": {
                    "pack_hash": "f" * 64,
                    "index_ref": str(tmp_path / "context" / "index.json"),
                    "index_sha256": "sha256:" + "e" * 64,
                },
            }

        def preflight_plan_session(self, scope, plan):
            self.preflights.append((scope, plan))
            return {"kind": "execution", "target_count": len(plan["targets"])}

    controller = FakePlanController()
    service = RuntimeIngestService(
        daemon, schemas=SchemaSet(SYSTEM_ROOT / "schemas"), work_root=tmp_path)
    service.bind_plan_controller(controller)
    broker = RuntimeMCPBroker(service).start()
    try:
        token = broker.grant(
            cycle_id="c1", stage="plan", target_id=None,
            purpose="plan-main-c1", pack_hash="a" * 64)
        request = {
            "version": 1, "trigger_kind": "new_structure",
            "query": "robust EEG baseline implementation",
            "need_summary": "find a reproducible external baseline",
        }
        searched = _broker_call(
            broker.socket_path, token, "plan_import_search", {"request": request})
        assert searched["ok"] is True
        assert searched["search"]["candidate_count"] == 1
        assert searched["context_pack"]["index_ref"].endswith("context/index.json")
        assert controller.searches[0][0].purpose == "plan-main-c1"
        assert controller.searches[0][1] == request

        plan = json.loads((
            SYSTEM_ROOT / "tests" / "fixtures" / "valid" /
            "plan" / "attack.json").read_text(encoding="utf-8"))
        checked = _broker_call(
            broker.socket_path, token, "preflight_plan", {"plan": plan})
        assert checked["ok"] is True
        assert len(controller.preflights) == 1

        sidecar = _broker_call(
            broker.socket_path, token, "submit_stage_artifact", {
                "files": {"import_search_request.json": request}})
        assert sidecar["ok"] is False
        assert "plan_import_search" in sidecar["error"]
        # A rejected control sidecar does not revoke the main capability; the
        # same Plan turn can still submit its corrected final plan.
        accepted = _broker_call(
            broker.socket_path, token, "submit_stage_artifact", {
                "files": {"plan.json": plan}})
        assert accepted["ok"] is True
        assert len(controller.preflights) == 2
    finally:
        broker.close()
        conn.close()


def test_reasoning_semantic_rejection_returns_to_same_live_capability(tmp_path):
    conn, daemon = _runtime_db(tmp_path, cycle_status="bundle")

    class FakeReasoningController:
        def __init__(self):
            self.calls = []

        def preflight_reasoning_session(self, scope, files):
            self.calls.append((scope, files))
            if files["selection.json"]["next_question_id"] == "q999":
                raise ValueError("fixture requires a schedulable continuation")
            return {"kind": "attack", "writes_performed": 0}

    controller = FakeReasoningController()
    service = RuntimeIngestService(
        daemon, schemas=SchemaSet(SYSTEM_ROOT / "schemas"), work_root=tmp_path)
    service.bind_reasoning_controller(controller)
    broker = RuntimeMCPBroker(service).start()
    try:
        token = broker.grant(
            cycle_id="c1", stage="reasoning", target_id=None,
            purpose="reasoning-main-c1", pack_hash="b" * 64)
        rejected = _broker_call(
            broker.socket_path, token, "submit_stage_artifact", {"files": {
                "selection.json": {
                    "next_question_id": "q999", "next_intent": "attack", "scores": [],
                },
            }})
        assert rejected["ok"] is False
        assert "fixture requires a schedulable continuation" in rejected["error"]
        assert broker.latest_stage_submission(token) is None

        accepted = _broker_call(
            broker.socket_path, token, "submit_stage_artifact", {"files": {
                "selection.json": {
                    "next_question_id": "q1", "next_intent": "attack", "scores": [],
                },
            }})
        assert accepted["ok"] is True
        assert len(controller.calls) == 2
        assert broker.latest_stage_submission(token)["files"]["selection.json"] == {
            "next_question_id": "q1", "next_intent": "attack", "scores": [],
        }
    finally:
        broker.close()
        conn.close()


def test_submit_final_transaction_rechecks_terminal_cycle(tmp_path):
    conn, daemon = _runtime_db(tmp_path)
    service = _BarrierRuntimeIngestService(
        daemon, schemas=SchemaSet(SYSTEM_ROOT / "schemas"), work_root=tmp_path)
    broker = RuntimeMCPBroker(service).start()
    try:
        token = broker.grant(
            cycle_id="c1", stage="idea", target_id=None,
            purpose="idea-terminal-race", pack_hash="d" * 64)
        with ThreadPoolExecutor(max_workers=1) as pool:
            inflight = pool.submit(
                _broker_call, broker.socket_path, token,
                "submit_stage_artifact", {
                    "files": {"idea_set.json": _valid_idea("terminal-race")},
                    "md": "slow-before-persist",
                })
            try:
                assert service.persist_entered.wait(timeout=5)
                with daemon.transaction() as db:
                    db.execute("UPDATE cycle SET status='aborted' WHERE id=1")
            finally:
                service.persist_release.set()
            result = inflight.result(timeout=5)
        assert result["ok"] is False
        assert "已终态" in result["error"]
        assert daemon.query_one(
            "SELECT count(*) FROM decision "
            "WHERE type='runtime_stage_submission'") == (0,)
    finally:
        service.persist_release.set()
        broker.close()
        conn.close()


def test_capability_expiry_after_commit_cannot_reverse_submit_success(tmp_path):
    conn, daemon = _runtime_db(tmp_path)
    service = _PostCommitBarrierRuntimeIngestService(
        daemon, schemas=SchemaSet(SYSTEM_ROOT / "schemas"), work_root=tmp_path)
    broker = RuntimeMCPBroker(service).start()
    try:
        token = broker.grant(
            cycle_id="c1", stage="idea", target_id=None,
            purpose="idea-expiry-linearization", pack_hash="e" * 64,
            ttl_s=0.05)
        with ThreadPoolExecutor(max_workers=1) as pool:
            inflight = pool.submit(
                _broker_call, broker.socket_path, token,
                "submit_stage_artifact", {
                    "files": {"idea_set.json": _valid_idea("expiry")},
                })
            try:
                assert service.committed.wait(timeout=5)
                time.sleep(0.08)
            finally:
                service.release.set()
            result = inflight.result(timeout=5)
        assert result["ok"] is True
        loaded = broker.latest_stage_submission(token)
        assert loaded["files"]["idea_set.json"] == _valid_idea("expiry")
    finally:
        service.release.set()
        broker.close()
        conn.close()
