from __future__ import annotations

import hashlib
import json
import socket
import subprocess
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from orchestrator import database
from orchestrator import manifest as manifest_module
from orchestrator import runtime_mcp as runtime_mcp_module
from orchestrator.cycle_replay import CycleReplayArchive
from orchestrator.interfaces import Artifact, ManagedArtifactRef
from orchestrator.native_review import (
    NativeReviewExecutionEvidence,
    NativeReviewLedger,
)
from orchestrator.native_review_verifier import validate_native_reviews
from orchestrator.runtime_mcp import (
    RuntimeIngestService,
    RuntimeMCPBroker,
    RuntimeMCPError,
    RuntimeMCPScope,
)
from orchestrator.schemas import SchemaSet
from orchestrator.writedaemon import WriteDaemon


SYSTEM_ROOT = Path(__file__).resolve().parent.parent


def test_runtime_mcp_direct_script_initializes_from_disposable_cwd(tmp_path):
    request = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18"},
    }

    completed = subprocess.run(
        [
            "/usr/bin/python3",
            str(SYSTEM_ROOT / "orchestrator" / "runtime_mcp.py"),
            "--stdio-bridge",
        ],
        input=(json.dumps(request) + "\n").encode("utf-8"),
        capture_output=True, cwd=tmp_path, check=False, timeout=5)

    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    response = json.loads(completed.stdout)
    assert response["id"] == 1
    assert response["result"]["serverInfo"]["name"] == "meta-research-runtime"


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


def test_runtime_mcp_grant_allows_runner_identity_without_review_ledger(tmp_path):
    conn, daemon = _runtime_db(tmp_path)
    broker = RuntimeMCPBroker(RuntimeIngestService(daemon)).start()
    try:
        token = broker.grant(
            cycle_id="c1", stage="idea", target_id=None,
            purpose="idea-main-c1-n1-a1", runner_call_id=41)

        scope = broker._grants[token]  # noqa: SLF001 - capability contract fixture
        assert scope.runner_call_id == 41
        assert scope.native_review_ledger is None
    finally:
        broker.close()
        conn.close()


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


def _valid_wildidea(marker: str) -> dict:
    idea = json.loads((
        SYSTEM_ROOT / "tests" / "fixtures" / "valid" /
        "idea_set" / "wildidea.json").read_text(encoding="utf-8"))
    idea["candidates"][0]["core_claim"] += f" [{marker}]"
    # The resident parent invocation has not finished while its internal audit
    # MCP is running.  Its durable MCP receipt is therefore the authority and
    # the optional provider provenance must be omitted honestly.
    idea.pop("provenance", None)
    return idea


class _FakeResidentWildIdeaAdapter:
    metadata = {
        "engine_version": "wildidea@test",
        "adapter_version": "meta-research-wildidea-adapter-test",
    }

    def __init__(self, final: dict):
        self.final = final
        self.draft = {"candidate draft": "owner-bound input"}
        self.expand_calls = []
        self.audit_calls = []

    def resident_expand(self, scope, *, need_innovation):  # noqa: ANN001
        self.expand_calls.append((scope, need_innovation))
        result = {
            "engine": dict(self.metadata),
            "need_innovation": need_innovation,
            "generation_path": (
                "wildidea" if need_innovation else "bypass"),
            "candidate_top_k": 3 if need_innovation else 1,
            "thresholds": {},
            "sd_threshold": 6,
            "novelty_enabled": False,
            "novelty_status": "联网查重未启用·文献级待验证",
            "seed": 17 if need_innovation else None,
            "sample": {"slots": []} if need_innovation else None,
        }
        if need_innovation:
            result["draft"] = json.loads(json.dumps(
                self.draft, ensure_ascii=False))
        return result

    def resident_audit(self, scope, *, draft):  # noqa: ANN001
        self.audit_calls.append((scope, draft))
        if draft != self.draft:
            raise RuntimeError(
                "wildidea_audit 只接受服务端生成的 exact draft")
        return {
            "idea_set": json.loads(json.dumps(
                self.final, ensure_ascii=False)),
            "internal_provenance": {
                "engine_version": self.metadata["engine_version"],
                "adapter_version": self.metadata["adapter_version"],
                "judge_runner_call_id": 52,
                "judge_provider_receipt_hash": "sha256:" + "7" * 64,
            },
        }


def _live_review_ledger() -> NativeReviewLedger:
    fixture = (
        SYSTEM_ROOT / "tests" / "fixtures" /
        "native_review_appserver_minimal.jsonl")
    ledger = NativeReviewLedger()
    ledger.feed(b"\n".join(fixture.read_bytes().splitlines()[:3]) + b"\n")
    return ledger


def _feed_review_child(
        ledger: NativeReviewLedger, *, request_id: str, ordinal: int,
        review_input: dict | None = None,
        review_input_request_id: str | None = None,
        verdict: str = "fail", findings: list[dict] | None = None) -> str:
    child_id = f"thread-child-{ordinal}"
    call_id = f"call-spawn-{ordinal}"
    turn_id = f"turn-child-{ordinal}"
    message_id = f"msg-child-{ordinal}"
    result = {
        "protocol": "native-review-result-v1",
        "review_request_id": request_id,
        "verdict": verdict,
        "summary_md": "adversarial review",
        "findings": findings if findings is not None else [{
            "finding_id": f"F{ordinal}",
            "issue": "missing control",
            "rationale": "the claim is otherwise underdetermined",
            "fix_hint": "add the control",
        }],
    }
    text = json.dumps(
        result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    input_item = None
    if review_input is not None:
        delivered_request_id = review_input_request_id or request_id
        structured = {
            "ok": True,
            "protocol": "native-review-input-v1",
            "review_request_id": delivered_request_id,
            "reviewer_brief_hash": review_input["reviewer_brief_hash"],
            "candidate_manifest_hash":
                review_input["candidate_manifest_hash"],
        }
        input_item = {
            "arguments": {"review_request_id": delivered_request_id},
            "error": None,
            "id": f"mcp-review-input-{ordinal}",
            "result": {
                "content": [],
                "structuredContent": structured,
                "_meta": None,
            },
            "server": "meta_research_runtime",
            "status": "completed",
            "tool": "read_review_input",
            "type": "mcpToolCall",
        }
    events = [
        {
            "method": "rawResponseItem/completed",
            "params": {
                "item": {
                    "arguments": json.dumps({
                        "task_name": f"reviewer-{ordinal}",
                        "fork_turns": "none",
                        "message": "gAAAA-test-encrypted-review-task",
                    }),
                    "call_id": call_id,
                    "id": f"fc-spawn-{ordinal}",
                    "name": "spawn_agent",
                    "namespace": "collaboration",
                    "type": "function_call",
                },
                "threadId": "thread-parent-1",
                "turnId": "turn-parent-1",
            },
        },
        {
            "method": "item/completed",
            "params": {
                "item": {
                    "agentPath": "<redacted>",
                    "agentThreadId": child_id,
                    "id": call_id,
                    "kind": "started",
                    "type": "subAgentActivity",
                },
                "threadId": "thread-parent-1",
                "turnId": "turn-parent-1",
            },
        },
        *([{
            "method": "item/completed",
            "params": {
                "item": input_item,
                "threadId": child_id,
                "turnId": turn_id,
            },
        }] if input_item is not None else []),
        {
            "method": "item/completed",
            "params": {
                "item": {
                    "id": message_id,
                    "phase": "final_answer",
                    "text": text,
                    "type": "agentMessage",
                },
                "threadId": child_id,
                "turnId": turn_id,
            },
        },
        {
            "method": "turn/completed",
            "params": {
                "threadId": child_id,
                "turn": {
                    "error": None,
                    "id": turn_id,
                    "status": "completed",
                },
            },
        },
        {
            "id": f"native-review-read:{child_id}",
            "result": {
                "thread": {
                    "id": child_id,
                    "parentThreadId": "thread-parent-1",
                    "source": {
                        "subAgent": {
                            "thread_spawn": {
                                "parent_thread_id": "thread-parent-1",
                            },
                        },
                    },
                    "turns": [{
                        "error": None,
                        "id": turn_id,
                        "items": ([
                            input_item,
                        ] if input_item is not None else []) + [{
                            "id": message_id,
                            "phase": "final_answer",
                            "text": text,
                            "type": "agentMessage",
                        }],
                        "status": "completed",
                    }],
                },
            },
        },
    ]
    ledger.feed(b"".join(
        json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
        + b"\n" for event in events))
    return child_id


def _record_genuine_inflight_native_review(tmp_path):
    """Record one review while its owner runner is genuinely still running."""
    conn, daemon = _runtime_db(tmp_path)
    purpose = "idea-main-c1-n1-a1"
    with daemon.transaction() as db:
        db.execute(
            "INSERT INTO runner_call("
            "id,cycle_id,phase,purpose,status,started_at"
            ") VALUES (41,1,'idea',?,'running',CURRENT_TIMESTAMP)",
            (purpose,))
    service = RuntimeIngestService(
        daemon, schemas=SchemaSet(SYSTEM_ROOT / "schemas"),
        policy={"flow": {"retry": {"plan_review": 1}}},
        work_root=tmp_path)
    broker = RuntimeMCPBroker(service).start()
    ledger = _live_review_ledger()
    token = broker.grant(
        cycle_id="c1", stage="idea", target_id=None,
        purpose=purpose, ttl_s=None, pack_hash="a" * 64,
        runner_call_id=41, native_review_ledger=ledger)
    initial = _valid_idea("inflight-review-h0")
    revised = _valid_idea("inflight-review-h1")
    prepared = _broker_call(
        broker.socket_path, token, "prepare_review", {
            "review_kind": "idea",
            "files": {"idea_set.json": initial},
        })
    assert prepared["ok"] is True
    _feed_review_child(
        ledger, request_id=prepared["review_request_id"], ordinal=1,
        review_input=prepared, verdict="pass", findings=[])
    recorded = _broker_call(
        broker.socket_path, token, "record_review", {
            "review_request_id": prepared["review_request_id"],
            "dispositions": [],
            "files": {"idea_set.json": revised},
        })
    assert recorded["ok"] is True
    return conn, daemon, broker, recorded


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

        legacy = _broker_call(broker.socket_path, token, "record_review", {
            "review_kind": "plan", "verdict": "fail",
            "summary_md": "revise", "issues": ["missing control"],
        })
        assert legacy["ok"] is False
        assert "prepare_review" in legacy["error"]
        assert daemon.query_one(
            "SELECT count(*) FROM decision WHERE type='runtime_review'") == (0,)

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


def test_native_fail_review_with_dispositions_completes_one_exact_round(tmp_path):
    conn, daemon = _runtime_db(tmp_path)
    service = RuntimeIngestService(
        daemon, schemas=SchemaSet(SYSTEM_ROOT / "schemas"),
        policy={"flow": {"retry": {"plan_review": 1}}},
        work_root=tmp_path)
    broker = RuntimeMCPBroker(service).start()
    ledger = _live_review_ledger()
    try:
        token = broker.grant(
            cycle_id="c1", stage="idea", target_id=None,
            purpose="idea-main-c1-n1-a1", ttl_s=None,
            pack_hash="a" * 64, runner_call_id=41,
            native_review_ledger=ledger)
        initial = _valid_idea("review-required-h0")
        revised = _valid_idea("review-required-h1")
        missing = _broker_call(
            broker.socket_path, token, "submit_stage_artifact",
            {"files": {"idea_set.json": revised}})
        assert missing["ok"] is False
        assert "prepare_review" in missing["error"]
        assert daemon.query_one(
            "SELECT count(*) FROM decision "
            "WHERE type='runtime_stage_submission'") == (0,)

        prepared = _broker_call(broker.socket_path, token, "prepare_review", {
            "review_kind": "idea",
            "files": {"idea_set.json": initial},
        })
        assert prepared["ok"] is True
        assert prepared["round_no"] == 1
        assert Path(prepared["reviewer_brief_ref"]).is_file()
        _feed_review_child(
            ledger, request_id=prepared["review_request_id"], ordinal=1,
            review_input=prepared, verdict="fail")

        incomplete = _broker_call(broker.socket_path, token, "record_review", {
            "review_request_id": prepared["review_request_id"],
            "dispositions": [],
            "files": {"idea_set.json": revised},
        })
        assert incomplete["ok"] is False
        assert "disposition" in incomplete["error"]

        review = _broker_call(broker.socket_path, token, "record_review", {
            "review_request_id": prepared["review_request_id"],
            "dispositions": [{
                "finding_id": "F1", "decision": "accept",
                "rationale": "added the requested control",
            }],
            "files": {"idea_set.json": revised},
        })
        assert review["ok"] is True
        assert review["round_no"] == 1
        assert review["verdict"] == "fail"
        assert review["reviewed_subject_hash"] == prepared["reviewed_subject_hash"]

        accepted = _broker_call(
            broker.socket_path, token, "submit_stage_artifact",
            {"files": {"idea_set.json": revised}})
        assert accepted["ok"] is True
        assert accepted["artifact_hash"] == review["resulting_subject_hash"]
        receipt = json.loads(Path(
            accepted["submission_ref"]).read_text(encoding="utf-8"))
        assert receipt["review_decision_id"] == review["decision_id"]
        payload_json = daemon.query_one(
            "SELECT payload_json FROM decision "
            "WHERE type='runtime_review'")[0]
        payload = json.loads(payload_json)
        assert payload["protocol"] == "native-review-receipt-v1"
        assert payload["runner_call_id"] == 41
        assert payload["parent_thread_id"] == "thread-parent-1"
        assert "missing control" not in payload_json

        no_implicit_extra = _broker_call(
            broker.socket_path, token, "prepare_review", {
                "review_kind": "idea",
                "files": {"idea_set.json": revised},
            })
        assert no_implicit_extra["ok"] is False
        assert "已完成" in no_implicit_extra["error"]
    finally:
        broker.close()
        conn.close()


def test_resident_wildidea_uses_internal_audit_receipt_not_native_child(
        tmp_path):
    conn, daemon = _runtime_db(tmp_path)
    final = _valid_wildidea("internal-audit")
    adapter = _FakeResidentWildIdeaAdapter(final)
    service = RuntimeIngestService(
        daemon, schemas=SchemaSet(SYSTEM_ROOT / "schemas"),
        policy={"flow": {"retry": {"plan_review": 1}}},
        work_root=tmp_path, wildidea_adapter=adapter)
    broker = RuntimeMCPBroker(service).start()
    ledger = _live_review_ledger()
    try:
        token = broker.grant(
            cycle_id="c1", stage="idea", target_id=None,
            purpose="idea-main-c1-n1-a1", ttl_s=None,
            pack_hash="a" * 64, runner_call_id=41,
            native_review_ledger=ledger)

        route = _broker_call(
            broker.socket_path, token, "wildidea_expand",
            {"need_innovation": True})
        assert route["ok"] is True
        assert route["generation_path"] == "wildidea"
        assert route["draft"] == adapter.draft
        assert route["route_receipt_hash"].startswith("sha256:")

        native = _broker_call(
            broker.socket_path, token, "prepare_review", {
                "review_kind": "idea",
                "files": {"idea_set.json": final},
            })
        assert native["ok"] is False
        assert "WildIdea 内部 audit" in native["error"]

        missing_internal = _broker_call(
            broker.socket_path, token, "submit_stage_artifact",
            {"files": {"idea_set.json": final}})
        assert missing_internal["ok"] is False
        assert "wildidea_audit" in missing_internal["error"]

        tampered_draft = json.loads(json.dumps(
            route["draft"], ensure_ascii=False))
        tampered_draft["caller mutation"] = True
        tampered_audit = _broker_call(
            broker.socket_path, token, "wildidea_audit",
            {"draft": tampered_draft})
        assert tampered_audit["ok"] is False
        assert "服务端生成的 exact draft" in tampered_audit["error"]

        audited = _broker_call(
            broker.socket_path, token, "wildidea_audit",
            {"draft": route["draft"]})
        assert audited["ok"] is True
        assert audited["idea_set"] == final
        assert audited["result_receipt_hash"].startswith("sha256:")
        assert adapter.audit_calls

        changed = json.loads(json.dumps(final, ensure_ascii=False))
        changed["candidates"][0]["core_claim"] += " caller mutation"
        forged = _broker_call(
            broker.socket_path, token, "submit_stage_artifact",
            {"files": {"idea_set.json": changed}})
        assert forged["ok"] is False
        assert "internal audit 结果 hash" in forged["error"]

        accepted = _broker_call(
            broker.socket_path, token, "submit_stage_artifact",
            {"files": {"idea_set.json": audited["idea_set"]}})
        assert accepted["ok"] is True
        submission = json.loads(Path(
            accepted["submission_ref"]).read_text(encoding="utf-8"))
        assert submission["review_decision_id"] == audited["decision_id"]
        counts = dict(daemon.query(
            "SELECT type,count(*) FROM decision "
            "WHERE type IN ('runtime_idea_generation_path',"
            "'runtime_wildidea_result','runtime_review') GROUP BY type"))
        assert counts == {
            "runtime_idea_generation_path": 1,
            "runtime_wildidea_result": 1,
        }
    finally:
        broker.close()
        conn.close()


def test_resident_bypass_route_is_server_bound_and_uses_exact_native_rounds(
        tmp_path):
    conn, daemon = _runtime_db(tmp_path)
    adapter = _FakeResidentWildIdeaAdapter(_valid_wildidea("unused"))
    service = RuntimeIngestService(
        daemon, schemas=SchemaSet(SYSTEM_ROOT / "schemas"),
        policy={"flow": {"retry": {"plan_review": 1}}},
        work_root=tmp_path, wildidea_adapter=adapter)
    broker = RuntimeMCPBroker(service).start()
    ledger = _live_review_ledger()
    try:
        token = broker.grant(
            cycle_id="c1", stage="idea", target_id=None,
            purpose="idea-main-c1-n1-a1", ttl_s=None,
            pack_hash="b" * 64, runner_call_id=41,
            native_review_ledger=ledger)
        route = _broker_call(
            broker.socket_path, token, "wildidea_expand",
            {"need_innovation": False})
        assert route["ok"] is True
        assert route["generation_path"] == "bypass"

        rebound = _broker_call(
            broker.socket_path, token, "wildidea_expand",
            {"need_innovation": True})
        assert rebound["ok"] is False
        assert "不得重绑定" in rebound["error"]

        wrong_path = _broker_call(
            broker.socket_path, token, "submit_stage_artifact", {
                "files": {"idea_set.json": _valid_wildidea("caller-flip")}})
        assert wrong_path["ok"] is False
        assert "服务端 generation_path=bypass" in wrong_path["error"]

        initial = _valid_idea("bypass-h0")
        revised = _valid_idea("bypass-h1")
        prepared = _broker_call(
            broker.socket_path, token, "prepare_review", {
                "review_kind": "idea",
                "files": {"idea_set.json": initial},
            })
        assert prepared["ok"] is True
        _feed_review_child(
            ledger, request_id=prepared["review_request_id"], ordinal=1,
            review_input=prepared)
        review = _broker_call(
            broker.socket_path, token, "record_review", {
                "review_request_id": prepared["review_request_id"],
                "dispositions": [{
                    "finding_id": "F1", "decision": "accept",
                    "rationale": "added the requested control",
                }],
                "files": {"idea_set.json": revised},
            })
        assert review["ok"] is True

        accepted = _broker_call(
            broker.socket_path, token, "submit_stage_artifact",
            {"files": {"idea_set.json": revised}})
        assert accepted["ok"] is True
        submission = json.loads(Path(
            accepted["submission_ref"]).read_text(encoding="utf-8"))
        assert submission["review_decision_id"] == review["decision_id"]
        assert daemon.query_one(
            "SELECT count(*) FROM decision "
            "WHERE type='runtime_wildidea_result'") == (0,)
    finally:
        broker.close()
        conn.close()


def test_durable_native_review_proof_binds_guardian_child_and_findings():
    ledger = _live_review_ledger()
    request_id = "nrr-durable-proof-request"
    review_input = {
        "reviewer_brief_hash": "sha256:" + "5" * 64,
        "candidate_manifest_hash": "sha256:" + "6" * 64,
    }
    findings = [{
        "finding_id": "F1",
        "issue": "missing control",
        "rationale": "the claim is otherwise underdetermined",
        "fix_hint": "add the control",
    }]
    _feed_review_child(
        ledger, request_id=request_id, ordinal=1,
        review_input=review_input, verdict="fail", findings=findings)
    ledger.feed(b"".join(
        json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
        + b"\n" for event in [
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "id": "msg-parent-final",
                        "phase": "final_answer",
                        "text": "done",
                        "type": "agentMessage",
                    },
                    "threadId": "thread-parent-1",
                    "turnId": "turn-parent-1",
                },
            },
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-parent-1",
                    "turn": {
                        "error": None,
                        "id": "turn-parent-1",
                        "status": "completed",
                    },
                },
            },
        ]))
    raw = bytes(ledger._raw)  # noqa: SLF001 - exact captured stream fixture
    guardian_receipt = {
        "state": "terminal", "outcome": "exit", "returncode": 0,
        "group_drained": True, "capture_stdout_bytes": len(raw),
        "capture_stdout_sha256":
            "sha256:" + hashlib.sha256(raw).hexdigest(),
    }
    children = ledger.finalize(
        receipt=guardian_receipt, captured_stdout=raw)
    execution = NativeReviewExecutionEvidence(
        runner_call_id=41, cycle_id="c1", stage="idea",
        purpose="idea-main-c1",
        execution_receipt_ref="/trusted/execution.json",
        execution_operation_id="exec-" + "a" * 32,
        capture_stdout_sha256=guardian_receipt["capture_stdout_sha256"],
        children=children)
    payload = {
        "protocol": "native-review-receipt-v1",
        "review_request_id": request_id,
        "cycle_id": "c1", "stage": "idea", "target_id": None,
        "purpose": "idea-main-c1", "review_kind": "idea",
        "round_no": 1, "configured_rounds": 1,
        "reviewed_subject_hash": "sha256:" + "1" * 64,
        "resulting_subject_hash": "sha256:" + "2" * 64,
        "prior_receipt_hash": None, "runner_call_id": 41,
        "parent_thread_id": "thread-parent-1",
        "parent_turn_id": "turn-parent-1",
        "child_call_id": "call-spawn-1",
        "child_thread_id": "thread-child-1",
        "child_turn_id": "turn-child-1",
        "review_input_item_id": "mcp-review-input-1",
        "review_input_brief_hash": review_input["reviewer_brief_hash"],
        "review_input_candidate_manifest_hash":
            review_input["candidate_manifest_hash"],
        "verdict": "fail",
        "findings_ref": "/trusted/reviewer-result.json",
        "findings_hash": "sha256:" + hashlib.sha256(
            RuntimeIngestService._canonical_bytes(findings)).hexdigest(),
        "dispositions_ref": "/trusted/dispositions.json",
        "disposition_hash": "sha256:" + "8" * 64,
        "revised_candidate_manifest_ref": "/trusted/revised.json",
        "revised_candidate_manifest_hash": "sha256:" + "9" * 64,
    }
    payload["receipt_hash"] = RuntimeIngestService._receipt_hash(payload)

    proof = RuntimeIngestService._durable_native_review_child_proof(
        payload, execution)

    assert proof["protocol"] == "native-review-child-event-proof-v1"
    assert proof["runner_call_id"] == 41
    assert proof["execution_operation_id"] == "exec-" + "a" * 32
    assert proof["review_request_id"] == request_id
    assert proof["child_thread_id"] == "thread-child-1"
    assert proof["findings_hash"] == payload["findings_hash"]
    assert proof["round_no"] == 1
    assert proof["reviewed_subject_hash"] == payload["reviewed_subject_hash"]

    forged = dict(payload)
    forged["findings_hash"] = "sha256:" + "0" * 64
    forged["receipt_hash"] = RuntimeIngestService._receipt_hash(forged)
    with pytest.raises(RuntimeMCPError, match="findings"):
        RuntimeIngestService._durable_native_review_child_proof(
            forged, execution)


def test_shared_verifier_accepts_genuine_owner_live_proof_before_runner_returns(
        tmp_path):
    conn, daemon, broker, recorded = (
        _record_genuine_inflight_native_review(tmp_path))
    try:
        assert daemon.query_one(
            "SELECT status FROM runner_call WHERE id=41") == ("running",)
        assert daemon.query_one(
            "SELECT count(*) FROM decision "
            "WHERE type='provider_invocation_accounted'") == (0,)

        reviews = validate_native_reviews(conn, cycle_id=1)

        assert len(reviews) == 1
        assert reviews[0][0] == recorded["decision_id"]
        assert reviews[0][1]["receipt_hash"] == recorded["receipt_hash"]
    finally:
        broker.close()
        conn.close()


@pytest.mark.parametrize("damage", ["caller_authored", "missing_snapshot"])
def test_shared_verifier_rejects_forged_or_missing_live_owner_proof(
        tmp_path, damage):
    conn, daemon, broker, _recorded = (
        _record_genuine_inflight_native_review(tmp_path))
    try:
        row = daemon.query_one(
            "SELECT id,payload_json FROM decision "
            "WHERE type='native_review_live_owner_proof'")
        assert row is not None
        proof = json.loads(row[1])
        if damage == "caller_authored":
            with daemon.transaction() as db:
                # Corruption fixture: bypass the production append-only
                # trigger solely to prove that an agent-attributed clone is
                # not owner authority.
                db.execute("DROP TRIGGER trg_decision_noupd")
                db.execute(
                    "UPDATE decision SET actor='agent' WHERE id=?",
                    (row[0],))
        else:
            Path(proof["snapshot_ref"]).unlink()

        with pytest.raises(ValueError, match="live|owner|proof|snapshot|durable"):
            validate_native_reviews(conn, cycle_id=1)
    finally:
        broker.close()
        conn.close()


def test_shared_verifier_does_not_use_live_proof_after_runner_success_without_accounting(
        tmp_path):
    conn, daemon, broker, _recorded = (
        _record_genuine_inflight_native_review(tmp_path))
    try:
        with daemon.transaction() as db:
            db.execute(
                "UPDATE runner_call SET status='success',"
                "finished_at=CURRENT_TIMESTAMP WHERE id=41")
        assert daemon.query_one(
            "SELECT count(*) FROM decision "
            "WHERE type='provider_invocation_accounted'") == (0,)

        with pytest.raises(ValueError, match="provider|accounting|guardian|durable"):
            validate_native_reviews(conn, cycle_id=1)
    finally:
        broker.close()
        conn.close()


def test_native_review_rejects_child_that_did_not_read_owner_input(tmp_path):
    conn, daemon = _runtime_db(tmp_path)
    service = RuntimeIngestService(
        daemon, schemas=SchemaSet(SYSTEM_ROOT / "schemas"),
        policy={"flow": {"retry": {"plan_review": 1}}},
        work_root=tmp_path)
    broker = RuntimeMCPBroker(service).start()
    ledger = _live_review_ledger()
    try:
        token = broker.grant(
            cycle_id="c1", stage="idea", target_id=None,
            purpose="idea-review-input-required", ttl_s=None,
            runner_call_id=42, native_review_ledger=ledger)
        prepared = _broker_call(
            broker.socket_path, token, "prepare_review", {
                "review_kind": "idea",
                "files": {"idea_set.json": _valid_idea("unread-h0")},
            })
        _feed_review_child(
            ledger, request_id=prepared["review_request_id"], ordinal=1)

        rejected = _broker_call(
            broker.socket_path, token, "record_review", {
                "review_request_id": prepared["review_request_id"],
                "dispositions": [{
                    "finding_id": "F1", "decision": "accept",
                    "rationale": "would revise",
                }],
                "files": {"idea_set.json": _valid_idea("unread-h1")},
            })

        assert rejected["ok"] is False
        assert "read_review_input" in rejected["error"]
        assert daemon.query_one(
            "SELECT count(*) FROM decision "
            "WHERE type='runtime_review'") == (0,)
    finally:
        broker.close()
        conn.close()


def test_native_review_rejects_child_input_hash_mismatch(tmp_path):
    conn, daemon = _runtime_db(tmp_path)
    service = RuntimeIngestService(
        daemon, schemas=SchemaSet(SYSTEM_ROOT / "schemas"),
        policy={"flow": {"retry": {"plan_review": 1}}},
        work_root=tmp_path)
    broker = RuntimeMCPBroker(service).start()
    ledger = _live_review_ledger()
    try:
        token = broker.grant(
            cycle_id="c1", stage="idea", target_id=None,
            purpose="idea-review-input-hash", ttl_s=None,
            runner_call_id=43, native_review_ledger=ledger)
        prepared = _broker_call(
            broker.socket_path, token, "prepare_review", {
                "review_kind": "idea",
                "files": {"idea_set.json": _valid_idea("hash-h0")},
            })
        wrong_input = {
            **prepared,
            "reviewer_brief_hash": "sha256:" + "0" * 64,
        }
        _feed_review_child(
            ledger, request_id=prepared["review_request_id"], ordinal=1,
            review_input=wrong_input)

        rejected = _broker_call(
            broker.socket_path, token, "record_review", {
                "review_request_id": prepared["review_request_id"],
                "dispositions": [{
                    "finding_id": "F1", "decision": "accept",
                    "rationale": "would revise",
                }],
                "files": {"idea_set.json": _valid_idea("hash-h1")},
            })

        assert rejected["ok"] is False
        assert "权威输入" in rejected["error"]
        assert daemon.query_one(
            "SELECT count(*) FROM decision "
            "WHERE type='runtime_review'") == (0,)
    finally:
        broker.close()
        conn.close()


@pytest.mark.parametrize("tamper_kind", ("brief", "manifest", "candidate"))
def test_native_review_rechecks_durable_owner_input_before_recording(
        tmp_path, tamper_kind):
    conn, daemon = _runtime_db(tmp_path)
    service = RuntimeIngestService(
        daemon, schemas=SchemaSet(SYSTEM_ROOT / "schemas"),
        policy={"flow": {"retry": {"plan_review": 1}}},
        work_root=tmp_path)
    broker = RuntimeMCPBroker(service).start()
    ledger = _live_review_ledger()
    try:
        token = broker.grant(
            cycle_id="c1", stage="idea", target_id=None,
            purpose=f"idea-review-input-tamper-{tamper_kind}", ttl_s=None,
            runner_call_id=44, native_review_ledger=ledger)
        prepared = _broker_call(
            broker.socket_path, token, "prepare_review", {
                "review_kind": "idea",
                "files": {"idea_set.json": _valid_idea("tamper-h0")},
            })
        _feed_review_child(
            ledger, request_id=prepared["review_request_id"], ordinal=1,
            review_input=prepared)
        brief_path = Path(prepared["reviewer_brief_ref"])
        manifest_path = brief_path.parent / "candidate" / "candidate-manifest.json"
        if tamper_kind == "brief":
            tampered_path = brief_path
        elif tamper_kind == "manifest":
            tampered_path = manifest_path
        else:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            tampered_path = Path(manifest["files"][0]["path"])
        original = tampered_path.read_bytes()
        arguments = {
            "review_request_id": prepared["review_request_id"],
            "dispositions": [{
                "finding_id": "F1", "decision": "accept",
                "rationale": "revised after the adversarial finding",
            }],
            "files": {"idea_set.json": _valid_idea("tamper-h1")},
        }
        try:
            tampered_path.write_bytes(original + b" ")
            rejected = _broker_call(
                broker.socket_path, token, "record_review", arguments)
            assert rejected["ok"] is False
            assert "复验失败" in rejected["error"]
            assert daemon.query_one(
                "SELECT count(*) FROM decision "
                "WHERE type='runtime_review'") == (0,)
        finally:
            tampered_path.write_bytes(original)

        completed = _broker_call(
            broker.socket_path, token, "record_review", arguments)
        assert completed["ok"] is True
    finally:
        broker.close()
        conn.close()


def test_invalid_revised_candidate_does_not_consume_native_review_round(tmp_path):
    conn, daemon = _runtime_db(tmp_path)
    service = RuntimeIngestService(
        daemon, schemas=SchemaSet(SYSTEM_ROOT / "schemas"),
        policy={"flow": {"retry": {"plan_review": 1}}},
        work_root=tmp_path)
    broker = RuntimeMCPBroker(service).start()
    ledger = _live_review_ledger()
    try:
        token = broker.grant(
            cycle_id="c1", stage="idea", target_id=None,
            purpose="idea-invalid-revision", ttl_s=None,
            runner_call_id=45, native_review_ledger=ledger)
        initial = _valid_idea("invalid-revision-h0")
        revised = _valid_idea("invalid-revision-h1")
        prepared = _broker_call(
            broker.socket_path, token, "prepare_review", {
                "review_kind": "idea",
                "files": {"idea_set.json": initial},
            })
        assert prepared["ok"] is True
        _feed_review_child(
            ledger, request_id=prepared["review_request_id"], ordinal=1,
            review_input=prepared)
        common = {
            "review_request_id": prepared["review_request_id"],
            "dispositions": [{
                "finding_id": "F1", "decision": "accept",
                "rationale": "will revise before completing the round",
            }],
        }

        bad_schema = _broker_call(
            broker.socket_path, token, "record_review", {
                **common, "files": {"idea_set.json": {"bad": True}},
            })
        assert bad_schema["ok"] is False
        assert "schema 校验失败" in bad_schema["error"]
        assert daemon.query_one(
            "SELECT count(*) FROM decision "
            "WHERE type='runtime_review'") == (0,)

        bad_closure = _broker_call(
            broker.socket_path, token, "record_review", {
                **common, "files": {"wrong.json": {}},
            })
        assert bad_closure["ok"] is False
        assert "必须且只能包含 idea_set.json" in bad_closure["error"]
        assert daemon.query_one(
            "SELECT count(*) FROM decision "
            "WHERE type='runtime_review'") == (0,)

        completed = _broker_call(
            broker.socket_path, token, "record_review", {
                **common, "files": {"idea_set.json": revised},
            })
        assert completed["ok"] is True
        submitted = _broker_call(
            broker.socket_path, token, "submit_stage_artifact", {
                "files": {"idea_set.json": revised},
            })
        assert submitted["ok"] is True
        assert submitted["artifact_hash"] == completed["resulting_subject_hash"]
    finally:
        broker.close()
        conn.close()


def test_native_review_exact_two_round_hash_chain_and_scope_binding(tmp_path):
    conn, daemon = _runtime_db(tmp_path)
    service = RuntimeIngestService(
        daemon, schemas=SchemaSet(SYSTEM_ROOT / "schemas"),
        policy={"flow": {"retry": {"plan_review": 2}}},
        work_root=tmp_path)
    broker = RuntimeMCPBroker(service).start()
    ledger = _live_review_ledger()
    try:
        token = broker.grant(
            cycle_id="c1", stage="idea", target_id=None,
            purpose="idea-main-c1", ttl_s=None, runner_call_id=51,
            native_review_ledger=ledger)
        wrong_purpose_token = broker.grant(
            cycle_id="c1", stage="idea", target_id=None,
            purpose="other-purpose", ttl_s=None, runner_call_id=51,
            native_review_ledger=ledger)
        wrong_runner_token = broker.grant(
            cycle_id="c1", stage="idea", target_id=None,
            purpose="idea-main-c1", ttl_s=None, runner_call_id=52,
            native_review_ledger=ledger)
        wrong_stage_token = broker.grant(
            cycle_id="c1", stage="plan", target_id=None,
            purpose="idea-main-c1", ttl_s=None, runner_call_id=51,
            native_review_ledger=ledger)
        h0 = _valid_idea("h0")
        h1 = _valid_idea("h1")
        h2 = _valid_idea("h2")

        p1 = _broker_call(broker.socket_path, token, "prepare_review", {
            "review_kind": "idea", "files": {"idea_set.json": h0},
        })
        assert p1["ok"] is True and p1["round_no"] == 1
        _feed_review_child(
            ledger, request_id=p1["review_request_id"], ordinal=1,
            review_input=p1)
        record_arguments = {
            "review_request_id": p1["review_request_id"],
            "dispositions": [{
                "finding_id": "F1", "decision": "reject",
                "rationale": "the frozen protocol already supplies it",
            }],
            "files": {"idea_set.json": h1},
        }
        for crossed_token in (
                wrong_purpose_token, wrong_runner_token, wrong_stage_token):
            crossed = _broker_call(
                broker.socket_path, crossed_token, "record_review",
                record_arguments)
            assert crossed["ok"] is False
            assert "binding" in crossed["error"]

        r1 = _broker_call(broker.socket_path, token, "record_review", {
                "review_request_id": p1["review_request_id"],
                "dispositions": [{
                    "finding_id": "F1", "decision": "accept",
                    "rationale": "revised H1",
                }],
                "files": {"idea_set.json": h1},
            })
        assert r1["ok"] is True

        stale = _broker_call(broker.socket_path, token, "prepare_review", {
            "review_kind": "idea", "files": {"idea_set.json": h2},
        })
        assert stale["ok"] is False
        assert "current review head" in stale["error"]

        p2 = _broker_call(broker.socket_path, token, "prepare_review", {
            "review_kind": "idea", "files": {"idea_set.json": h1},
        })
        assert p2["ok"] is True and p2["round_no"] == 2
        assert p2["reviewed_subject_hash"] == r1["resulting_subject_hash"]
        _feed_review_child(
            ledger, request_id=p2["review_request_id"], ordinal=2,
            review_input=p2, verdict="pass", findings=[])
        r2 = _broker_call(broker.socket_path, token, "record_review", {
            "review_request_id": p2["review_request_id"],
            "dispositions": [],
            "files": {"idea_set.json": h2},
        })
        assert r2["ok"] is True and r2["round_no"] == 2
        assert r2["reviewed_subject_hash"] == r1["resulting_subject_hash"]

        wrong_final = _broker_call(
            broker.socket_path, token, "submit_stage_artifact",
            {"files": {"idea_set.json": h1}})
        assert wrong_final["ok"] is False
        assert "final review hash" in wrong_final["error"]
        accepted = _broker_call(
            broker.socket_path, token, "submit_stage_artifact",
            {"files": {"idea_set.json": h2}})
        assert accepted["ok"] is True
        assert accepted["artifact_hash"] == r2["resulting_subject_hash"]
    finally:
        broker.close()
        conn.close()


def test_native_review_requires_unique_matching_child_and_rejects_duplicate(
        tmp_path):
    conn, daemon = _runtime_db(tmp_path)
    service = RuntimeIngestService(
        daemon, schemas=SchemaSet(SYSTEM_ROOT / "schemas"),
        policy={"flow": {"retry": {"plan_review": 1}}},
        work_root=tmp_path)
    broker = RuntimeMCPBroker(service).start()
    ledger = _live_review_ledger()
    try:
        token = broker.grant(
            cycle_id="c1", stage="idea", target_id=None,
            purpose="idea-live-child", ttl_s=None, runner_call_id=71,
            native_review_ledger=ledger)
        h0, h1 = _valid_idea("unique-h0"), _valid_idea("unique-h1")
        prepared = _broker_call(
            broker.socket_path, token, "prepare_review", {
                "review_kind": "idea", "files": {"idea_set.json": h0},
            })
        args = {
            "review_request_id": prepared["review_request_id"],
            "dispositions": [{
                "finding_id": "F2", "decision": "accept",
                "rationale": "revised",
            }],
            "files": {"idea_set.json": h1},
        }
        no_spawn = _broker_call(
            broker.socket_path, token, "record_review", args)
        assert no_spawn["ok"] is False
        assert "唯一" in no_spawn["error"]

        _feed_review_child(
            ledger, request_id="nrr-" + "x" * 24, ordinal=1)
        wrong_request = _broker_call(
            broker.socket_path, token, "record_review", args)
        assert wrong_request["ok"] is False
        assert "唯一" in wrong_request["error"]

        _feed_review_child(
            ledger, request_id=prepared["review_request_id"], ordinal=2,
            review_input=prepared)
        completed = _broker_call(
            broker.socket_path, token, "record_review", args)
        assert completed["ok"] is True
        durable = json.loads(daemon.query_one(
            "SELECT payload_json FROM decision "
            "WHERE type='runtime_review'")[0])
        durable_files = {
            field: Path(durable[field]).read_bytes()
            for field in (
                "findings_ref", "dispositions_ref",
                "revised_candidate_manifest_ref")
        }
        assert (
            "sha256:" + hashlib.sha256(
                durable_files["findings_ref"]).hexdigest()
            == durable["findings_hash"])
        assert (
            "sha256:" + hashlib.sha256(
                durable_files["dispositions_ref"]).hexdigest()
            == durable["disposition_hash"])
        assert (
            "sha256:" + hashlib.sha256(
                durable_files["revised_candidate_manifest_ref"]).hexdigest()
            == durable["revised_candidate_manifest_hash"])
        duplicate = _broker_call(
            broker.socket_path, token, "record_review", args)
        assert duplicate["ok"] is False
        assert "重复" in duplicate["error"]
        for field, body in durable_files.items():
            assert Path(durable[field]).read_bytes() == body
    finally:
        broker.close()
        conn.close()


def test_review_round_zero_submits_directly_and_revoked_token_cannot_record(
        tmp_path, monkeypatch):
    conn, daemon = _runtime_db(tmp_path)
    service = RuntimeIngestService(
        daemon, schemas=SchemaSet(SYSTEM_ROOT / "schemas"),
        policy={"flow": {"retry": {"plan_review": 0}}},
        work_root=tmp_path)
    broker = RuntimeMCPBroker(service).start()
    try:
        direct = broker.grant(
            cycle_id="c1", stage="idea", target_id=None,
            purpose="idea-no-review")
        submitted = _broker_call(
            broker.socket_path, direct, "submit_stage_artifact", {
                "files": {"idea_set.json": _valid_idea("n0")},
            })
        assert submitted["ok"] is True

        service.policy["flow"]["retry"]["plan_review"] = 1
        ledger = _live_review_ledger()
        reviewed = broker.grant(
            cycle_id="c1", stage="idea", target_id=None,
            purpose="idea-revoked", ttl_s=None, runner_call_id=81,
            native_review_ledger=ledger)
        prepared = _broker_call(
            broker.socket_path, reviewed, "prepare_review", {
                "review_kind": "idea",
                "files": {"idea_set.json": _valid_idea("revoked-h0")},
            })
        assert prepared["ok"] is True
        _feed_review_child(
            ledger, request_id=prepared["review_request_id"], ordinal=1,
            review_input=prepared)
        broker.revoke(reviewed)
        rejected = _broker_call(
            broker.socket_path, reviewed, "record_review", {
                "review_request_id": prepared["review_request_id"],
                "dispositions": [{
                    "finding_id": "F1", "decision": "accept",
                    "rationale": "would revise",
                }],
                "files": {"idea_set.json": _valid_idea("revoked-h1")},
            })
        assert rejected["ok"] is False
        assert "撤销" in rejected["error"]

        clock = [time.monotonic()]
        monkeypatch.setattr(
            runtime_mcp_module, "time",
            types.SimpleNamespace(
                monotonic=lambda: clock[0],
                time_ns=time.time_ns,
            ))
        expiring_ledger = _live_review_ledger()
        expiring = broker.grant(
            cycle_id="c1", stage="idea", target_id=None,
            purpose="idea-expired", ttl_s=1.0, runner_call_id=82,
            native_review_ledger=expiring_ledger)
        expiring_request = _broker_call(
            broker.socket_path, expiring, "prepare_review", {
                "review_kind": "idea",
                "files": {"idea_set.json": _valid_idea("expired-h0")},
            })
        assert expiring_request["ok"] is True
        _feed_review_child(
            expiring_ledger,
            request_id=expiring_request["review_request_id"], ordinal=1,
            review_input=expiring_request)
        clock[0] += 2.0
        expired = _broker_call(
            broker.socket_path, expiring, "record_review", {
                "review_request_id": expiring_request["review_request_id"],
                "dispositions": [{
                    "finding_id": "F1", "decision": "accept",
                    "rationale": "would revise",
                }],
                "files": {"idea_set.json": _valid_idea("expired-h1")},
            })
        assert expired["ok"] is False
        assert "过期" in expired["error"]
        assert daemon.query_one(
            "SELECT count(*) FROM decision "
            "WHERE type='runtime_review'") == (0,)
    finally:
        broker.close()
        conn.close()


def test_two_completed_children_for_one_request_are_ambiguous(tmp_path):
    conn, daemon = _runtime_db(tmp_path)
    service = RuntimeIngestService(
        daemon, schemas=SchemaSet(SYSTEM_ROOT / "schemas"),
        policy={"flow": {"retry": {"plan_review": 1}}},
        work_root=tmp_path)
    broker = RuntimeMCPBroker(service).start()
    ledger = _live_review_ledger()
    try:
        token = broker.grant(
            cycle_id="c1", stage="idea", target_id=None,
            purpose="idea-ambiguous-child", ttl_s=None, runner_call_id=91,
            native_review_ledger=ledger)
        prepared = _broker_call(
            broker.socket_path, token, "prepare_review", {
                "review_kind": "idea",
                "files": {"idea_set.json": _valid_idea("ambiguous-h0")},
            })
        for ordinal in (1, 2):
            _feed_review_child(
                ledger, request_id=prepared["review_request_id"],
                ordinal=ordinal)
        ambiguous = _broker_call(
            broker.socket_path, token, "record_review", {
                "review_request_id": prepared["review_request_id"],
                "dispositions": [{
                    "finding_id": "F1", "decision": "accept",
                    "rationale": "cannot choose between children",
                }],
                "files": {"idea_set.json": _valid_idea("ambiguous-h1")},
            })
        assert ambiguous["ok"] is False
        assert "唯一" in ambiguous["error"]
        assert daemon.query_one(
            "SELECT count(*) FROM decision "
            "WHERE type='runtime_review'") == (0,)
    finally:
        broker.close()
        conn.close()


def test_late_second_completed_child_poisoned_review_cannot_submit(tmp_path):
    conn, daemon = _runtime_db(tmp_path)
    service = RuntimeIngestService(
        daemon, schemas=SchemaSet(SYSTEM_ROOT / "schemas"),
        policy={"flow": {"retry": {"plan_review": 1}}},
        work_root=tmp_path)
    broker = RuntimeMCPBroker(service).start()
    ledger = _live_review_ledger()
    try:
        token = broker.grant(
            cycle_id="c1", stage="idea", target_id=None,
            purpose="idea-late-duplicate-child", ttl_s=None,
            runner_call_id=92, native_review_ledger=ledger)
        h0 = _valid_idea("late-duplicate-h0")
        h1 = _valid_idea("late-duplicate-h1")
        prepared = _broker_call(
            broker.socket_path, token, "prepare_review", {
                "review_kind": "idea",
                "files": {"idea_set.json": h0},
            })
        request_id = prepared["review_request_id"]
        _feed_review_child(
            ledger, request_id=request_id, ordinal=1,
            review_input=prepared)
        recorded = _broker_call(
            broker.socket_path, token, "record_review", {
                "review_request_id": request_id,
                "dispositions": [{
                    "finding_id": "F1", "decision": "accept",
                    "rationale": "revised after the unique first reviewer",
                }],
                "files": {"idea_set.json": h1},
            })
        assert recorded["ok"] is True

        _feed_review_child(ledger, request_id=request_id, ordinal=2)
        poisoned = _broker_call(
            broker.socket_path, token, "submit_stage_artifact", {
                "files": {"idea_set.json": h1},
            })

        assert poisoned["ok"] is False
        assert "唯一" in poisoned["error"]
    finally:
        broker.close()
        conn.close()


def test_second_child_reading_claimed_request_poisons_even_if_final_names_other(
        tmp_path):
    conn, daemon = _runtime_db(tmp_path)
    service = RuntimeIngestService(
        daemon, schemas=SchemaSet(SYSTEM_ROOT / "schemas"),
        policy={"flow": {"retry": {"plan_review": 1}}},
        work_root=tmp_path)
    broker = RuntimeMCPBroker(service).start()
    ledger = _live_review_ledger()
    try:
        token = broker.grant(
            cycle_id="c1", stage="idea", target_id=None,
            purpose="idea-duplicate-owner-delivery", ttl_s=None,
            runner_call_id=93, native_review_ledger=ledger)
        h0 = _valid_idea("duplicate-delivery-h0")
        h1 = _valid_idea("duplicate-delivery-h1")
        prepared = _broker_call(
            broker.socket_path, token, "prepare_review", {
                "review_kind": "idea", "files": {"idea_set.json": h0},
            })
        request_id = prepared["review_request_id"]
        _feed_review_child(
            ledger, request_id=request_id, ordinal=1,
            review_input=prepared)
        recorded = _broker_call(
            broker.socket_path, token, "record_review", {
                "review_request_id": request_id,
                "dispositions": [{
                    "finding_id": "F1", "decision": "accept",
                    "rationale": "revised after the unique first reviewer",
                }],
                "files": {"idea_set.json": h1},
            })
        assert recorded["ok"] is True

        _feed_review_child(
            ledger, request_id="nrr-" + "z" * 24, ordinal=2,
            review_input=prepared, review_input_request_id=request_id)
        poisoned = _broker_call(
            broker.socket_path, token, "submit_stage_artifact", {
                "files": {"idea_set.json": h1},
            })

        assert poisoned["ok"] is False
        assert "唯一" in poisoned["error"]
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


def test_complete_bundle_target_requires_owner_bound_result_review_before_exit(
        tmp_path):
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

    class FakeBundleController:
        def __init__(self):
            self.bound = False
            self.force_ready_projection = False

        def bind_next_bundle_target(self, _scope):
            if not self.bound:
                self.bound = True
                return {
                    "cycle_complete": False, "build_target_id": 7,
                    "context_pack": {"pack_hash": "c" * 64},
                }
            status = daemon.query_one(
                "SELECT status FROM build_target WHERE id=7")[0]
            return {"cycle_complete": status == "complete"}

        def bundle_session_scope(self, _scope):
            if not self.bound:
                raise RuntimeError("call next first")
            return {
                "target_id": 7, "pack_hash": "c" * 64,
                "refs": ["request-ref-7"],
            }

        def execute_bundle_session(self, _scope, _files):
            active = daemon.query_one(
                "SELECT d.id,json_extract(d.payload_json,"
                "'$.evaluation_attempt_id') FROM decision d "
                "WHERE d.type='bundle_result_candidate' "
                "AND NOT EXISTS (SELECT 1 FROM decision s "
                " WHERE s.type='bundle_result_candidate_superseded' "
                " AND json_extract(s.payload_json,"
                "'$.candidate_decision_id')=d.id) ORDER BY d.id DESC LIMIT 1")
            ack = (None if active is None else daemon.query_one(
                "SELECT 1 FROM decision "
                "WHERE type='runtime_bundle_result_review_ack' "
                "AND json_extract(payload_json,'$.candidate_decision_id')=?",
                (active[0],)))
            if ack is not None and active is not None:
                with daemon.transaction() as db:
                    db.execute(
                        "UPDATE evaluation_attempt SET status='success' "
                        "WHERE id=?", (active[1],))
                    db.execute(
                        "UPDATE build_target SET status='complete' WHERE id=7")
                return {
                    "build_target_id": 7, "status": "complete",
                    "terminal": True,
                }
            return {
                "build_target_id": 7, "status": "running",
                "terminal": False,
            }

        def bundle_session_status(self, _scope):
            status = daemon.query_one(
                "SELECT status FROM build_target WHERE id=7")[0]
            candidate = daemon.query_one(
                "SELECT d.id FROM decision d "
                "WHERE d.type='bundle_result_candidate' "
                "AND NOT EXISTS (SELECT 1 FROM decision s "
                " WHERE s.type='bundle_result_candidate_superseded' "
                " AND json_extract(s.payload_json,"
                "'$.candidate_decision_id')=d.id) "
                "ORDER BY d.id DESC LIMIT 1")
            ack = (None if candidate is None else daemon.query_one(
                "SELECT 1 FROM decision "
                "WHERE type='runtime_bundle_result_review_ack' "
                "AND json_extract(payload_json,'$.candidate_decision_id')=?",
                (candidate[0],)))
            projected_ready = bool(
                ack is not None or self.force_ready_projection)
            return {
                "cycle_id": "c1", "build_target_id": 7,
                "target_kind": "build", "seq": 1,
                "status": status, "failure_kind": None,
                "terminal": status == "complete",
                "worker_running": False, "controller_error": None,
                "awaiting_result_review": (
                    candidate is not None and not projected_ready),
                "result_review_ready": projected_ready,
                "result_candidate_decision_id": (
                    None if candidate is None else candidate[0]),
                "cancellation_requested": None, "latest_repair": None,
                "execution_logs": [], "live_logs": [],
            }

        def request_bundle_repair(self, _scope, _diagnosis):
            return {"build_target_id": 7, "status": "running", "terminal": False}

        def replan_bundle_session(self, _scope, _diagnosis):
            return {"build_target_id": 7, "status": "failed", "terminal": True}

    controller = FakeBundleController()
    service = RuntimeIngestService(
        daemon, schemas=schemas,
        policy={"flow": {"retry": {
            "bundle_code_review": 0, "bundle_result_review": 1,
        }}},
        work_root=tmp_path)
    service.bind_bundle_controller(controller)
    broker = RuntimeMCPBroker(service).start()
    ledger = _live_review_ledger()
    try:
        token = broker.grant(
            cycle_id="c1", stage="bundle", target_id="7",
            purpose="bundle-main-c1-n1-a1", pack_hash="b" * 64,
            runner_call_id=94, native_review_ledger=ledger)
        bound = _broker_call(
            broker.socket_path, token, "bundle_next_target", {})
        assert bound["ok"] is True and bound["build_target_id"] == 7, bound
        accepted = _broker_call(
            broker.socket_path, token, "submit_stage_artifact", {
                "files": {
                    "execution_manifest.json": manifest,
                    "identity.md": "# toy\n\n## 复现命令\npython train.py\n",
                    "train.py": "print('train')\n",
                    "eval.py": "print('eval')\n",
                    "cfg.json": {"lr": 0.1},
                },
            })
        assert accepted["ok"] is True
        with daemon.transaction() as db:
            db.execute(
                "INSERT INTO baseline(id,slug,canonical_key,born_cycle,status) "
                "VALUES (11,'b','b',1,'building')")
            db.execute(
                "INSERT INTO variant(id,baseline_id,variant_key,config_json,status) "
                "VALUES (12,11,'v','{}','building')")
            db.execute(
                "INSERT INTO protocol(id,version,name,scope_spec_json) "
                "VALUES (1,1,'p','{}')")
            db.execute(
                "INSERT INTO evaluation(id,variant_id,protocol_id,protocol_ver,"
                "eval_key,source,status,created_cycle,build_target_id,"
                "target_set_hash) "
                "VALUES (21,12,1,1,'factory','factory','running',1,7,'set')")
            db.execute(
                "INSERT INTO evaluation_attempt(id,evaluation_id,cycle_id,"
                "build_target_id,attempt_no,purpose,status,started_cycle) "
                "VALUES (31,21,1,7,1,'factory','running',1)")
            db.execute(
                "UPDATE build_target SET status='running',baseline_id=11,"
                "variant_id=12,evaluation_id=21 WHERE id=7")
            candidate = {
                "protocol": "bundle-result-candidate-v1",
                "cycle_id": 1, "build_target_id": 7,
                "evaluation_id": 21, "evaluation_attempt_id": 31,
                "result_subject_hash": "a" * 64,
                "scientific_decision_hash": "b" * 64,
                "execution_status": "succeeded",
                "validity_status": "valid",
                "scientific_outcome": "refuted",
                "pool_eligibility": "eligible",
                "metric_results": [{
                    "metric_id": 1, "metric_ver": 1,
                    "value": 0.2, "scope": "aggregate",
                }],
                "eval_log": {
                    "ref": str(tmp_path / "eval.log"),
                    "content_hash": "c" * 64, "bytes": 17,
                },
                "checkpoint_hashes": {"final": "d" * 64},
            }
            db.execute(
                "INSERT INTO decision(cycle_id,actor,type,payload_json) "
                "VALUES (1,'orchestrator','bundle_result_candidate',?)",
                (json.dumps(candidate, ensure_ascii=False, sort_keys=True),))

        blocked = _broker_call(
            broker.socket_path, token, "bundle_execute", {
                "submission_ref": accepted["submission_ref"],
                "submission_hash": accepted["submission_hash"],
            })
        assert blocked["ok"] is False
        assert "result candidate" in blocked["error"]

        # A compact controller projection is never authority to resume
        # admission.  The owner must replay the durable native-child proof.
        controller.force_ready_projection = True
        forged = _broker_call(
            broker.socket_path, token, "bundle_execute", {
                "submission_ref": accepted["submission_ref"],
                "submission_hash": accepted["submission_hash"],
            })
        assert forged["ok"] is False
        assert "review" in forged["error"]
        controller.force_ready_projection = False

        injected = _broker_call(
            broker.socket_path, token, "prepare_review", {
                "review_kind": "bundle_result",
                "files": {"forged.json": {"status": "complete"}},
            })
        assert injected["ok"] is False
        assert "owner" in injected["error"]
        prepared = _broker_call(
            broker.socket_path, token, "prepare_review", {
                "review_kind": "bundle_result",
            })
        assert prepared["ok"] is True
        _feed_review_child(
            ledger, request_id=prepared["review_request_id"], ordinal=1,
            review_input=prepared)
        record_arguments = {
            "review_request_id": prepared["review_request_id"],
            "dispositions": [{
                "finding_id": "F1", "decision": "accept",
                "rationale": "terminal evidence was independently checked",
            }],
        }
        recorded = _broker_call(
            broker.socket_path, token, "record_review", {
                **record_arguments,
            })
        assert recorded["ok"] is True
        assert recorded["bundle_result_ack_decision_id"] > 0

        first_candidate_id = json.loads(daemon.query_one(
            "SELECT payload_json FROM decision "
            "WHERE type='runtime_bundle_result_review_ack' "
            "ORDER BY id LIMIT 1")[0])["candidate_decision_id"]
        replacement = json.loads(json.dumps(candidate))
        replacement["evaluation_attempt_id"] = 32
        replacement["result_subject_hash"] = "e" * 64
        replacement["scientific_decision_hash"] = "f" * 64
        replacement["metric_results"][0]["value"] = 0.3
        replacement["eval_log"]["content_hash"] = "1" * 64
        with daemon.transaction() as db:
            db.execute(
                "UPDATE evaluation_attempt SET status='failed',"
                "failure_kind='protocol_violation' WHERE id=31")
            db.execute(
                "INSERT INTO evaluation_attempt(id,evaluation_id,cycle_id,"
                "build_target_id,attempt_no,purpose,status,retry_of,"
                "started_cycle) "
                "VALUES (32,21,1,7,2,'retry','running',31,1)")
            db.execute(
                "INSERT INTO decision(cycle_id,actor,type,payload_json) "
                "VALUES (1,'orchestrator',"
                "'bundle_result_candidate_superseded',?)",
                (json.dumps({
                    "protocol":
                        "bundle-result-candidate-superseded-v1",
                    "build_target_id": 7,
                    "candidate_decision_id": first_candidate_id,
                    "evaluation_id": 21,
                    "evaluation_attempt_id": 31,
                    "action": "repair",
                    "diagnosis_md": "review requested rerun",
                }, ensure_ascii=False, sort_keys=True),))
            db.execute(
                "INSERT INTO decision(cycle_id,actor,type,payload_json) "
                "VALUES (1,'orchestrator','bundle_result_candidate',?)",
                (json.dumps(
                    replacement, ensure_ascii=False, sort_keys=True),))

        prepared_again = _broker_call(
            broker.socket_path, token, "prepare_review", {
                "review_kind": "bundle_result",
            })
        assert prepared_again["ok"] is True
        assert prepared_again["round_no"] == 1
        _feed_review_child(
            ledger, request_id=prepared_again["review_request_id"],
            ordinal=2, review_input=prepared_again)
        recorded_again = _broker_call(
            broker.socket_path, token, "record_review", {
                "review_request_id": prepared_again["review_request_id"],
                "dispositions": [{
                    "finding_id": "F2", "decision": "reject",
                    "rationale": "rerun evidence now addresses the concern",
                }],
            })
        assert recorded_again["ok"] is True
        assert daemon.query_one(
            "SELECT count(*) FROM decision "
            "WHERE type='runtime_review'") == (2,)
        assert daemon.query_one(
            "SELECT count(*) FROM decision "
            "WHERE type='runtime_bundle_result_review_ack'") == (2,)

        resumed = _broker_call(
            broker.socket_path, token, "bundle_execute", {
                "submission_ref": accepted["submission_ref"],
                "submission_hash": accepted["submission_hash"],
            })
        assert resumed["ok"] is True
        assert resumed["status"] == "complete"
        complete = _broker_call(
            broker.socket_path, token, "bundle_next_target", {})
        assert complete["ok"] is True and complete["cycle_complete"] is True, complete
        broker.assert_stage_turn_complete(token)
        assert broker.latest_stage_submission(token)["artifact_hash"] == (
            accepted["artifact_hash"])
        ack = json.loads(daemon.query_one(
            "SELECT payload_json FROM decision "
            "WHERE type='runtime_bundle_result_review_ack' "
            "ORDER BY id DESC LIMIT 1")[0])
        assert ack["build_target_id"] == 7
        assert ack["candidate_decision_id"] > 0
        assert ack["protocol"] == "native-bundle-result-review-ack-v2"
        assert ack["subject_hash"] == recorded_again["resulting_subject_hash"]
    finally:
        broker.close()
        conn.close()


def test_runtime_tool_inventory_has_read_only_plan_preflight_not_early_claim():
    tools = {item["name"]: item for item in RuntimeIngestService.tools()}
    assert "register_baseline" not in tools
    assert tools["preflight_plan"]["annotations"]["readOnlyHint"] is True
    assert {
        "plan_import_search", "wildidea_expand", "wildidea_search",
        "wildidea_audit",
    }.issubset(tools)
    assert {"bundle_next_target", "bundle_execute", "bundle_status",
            "bundle_repair", "bundle_replan"}.issubset(tools)
    assert "prepare_review" in tools
    assert tools["read_review_input"]["annotations"]["readOnlyHint"] is True
    record_schema = tools["record_review"]["inputSchema"]
    assert "child_thread_id" not in record_schema["properties"]
    assert "child_thread_id" not in record_schema["required"]
    status_schema = tools["bundle_status"]["inputSchema"]
    assert status_schema["properties"]["after_status_revision"] == {
        "type": "integer", "minimum": 0}


def test_bundle_status_forwards_log_and_status_cursors_to_fixed_worker(
        tmp_path):
    _conn, daemon = _runtime_db(tmp_path, cycle_status="plan")

    class Controller:
        def __init__(self):
            self.status_calls = []

        def bundle_session_scope(self, _scope):
            return {
                "target_id": 7,
                "pack_hash": "a" * 64,
                "refs": [],
            }

        def bundle_session_status(self, scope, **kwargs):
            self.status_calls.append((scope, kwargs))
            return {
                "build_target_id": 7,
                "terminal": False,
                "journal": {
                    "cursor": kwargs["after_seq"],
                    "status_revision": kwargs[
                        "after_status_revision"],
                },
            }

        def execute_bundle_session(self, _scope, _files):
            return {}

        def request_bundle_repair(self, _scope, _diagnosis):
            return {}

        def replan_bundle_session(self, _scope, _diagnosis):
            return {}

        def bundle_scheduler_overview(self, _scope):
            return {}

        def dispatch_bundle_frontier(self, _scope):
            return {}

        def wait_bundle_scheduler(self, _scope, **_kwargs):
            return {}

        def drain_bundle_scheduler(self, _scope):
            return {}

    controller = Controller()
    service = RuntimeIngestService(daemon)
    service.bind_bundle_controller(controller)
    scope = RuntimeMCPScope(
        cycle_id="c1",
        stage="bundle",
        target_id="7",
        purpose="bundle-worker-c1-t7",
        expires_at=None,
    )

    result = service.call(scope, "bundle_status", {
        "mode": "incremental",
        "after_seq": 41,
        "after_status_revision": 9,
        "limit": 200,
        "timeout_s": 60,
    })

    assert result["ok"] is True
    assert controller.status_calls[0][1] == {
        "mode": "incremental",
        "after_seq": 41,
        "after_status_revision": 9,
        "limit": 200,
        "timeout_s": 60.0,
    }
    daemon.conn.close()


def test_bundle_code_native_review_focus_covers_plan_and_data_boundaries():
    focus = RuntimeIngestService._native_review_focus("bundle_code")

    assert focus["protocol"] == "bundle-code-review-focus-v1"
    assert set(focus["required_checks"]) == {
        "frozen_plan_conformance",
        "train_validation_test_isolation",
        "heldout_access_order",
        "train_only_preprocessing_fit",
        "outcome_leakage",
    }
    assert focus["evidence_limit"] == (
        "reviewer attestation; not runtime proof of heldout non-access")


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
