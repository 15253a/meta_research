"""CP7.2 · StageProvider 真 Codex→真组件阶段回调（M6）。

核心验收面：把 CodexRunner 一次会话 + 信封解析 + 逐产物 schema 校验 + artifact_parse 重试封成
(cyc, pack)→files；阶段必产在场、在场 optional 校验；结构非法重试并附反馈、用尽即 RunnerError；
用真 SqliteAdvancer 端到端（mock runner）跑通一轮，证适配器契约与组件对得上。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace as NS

import hashlib
import json
import sqlite3

import pytest
import yaml

from orchestrator import database as db
from orchestrator.bundle_tasks import BundleTaskRegistry
from orchestrator.interfaces import Artifact, ContextPack
from orchestrator.runner import RunnerError
from orchestrator.schemas import SchemaSet
from orchestrator.stage_provider import (
    BUNDLE_OPERATOR_SESSION_CONTRACT,
    BUNDLE_TASK_SESSION_CONTRACT,
    PlanReviewProvider,
    STAGE_MAIN_SESSION_CONTRACT,
    StageProvider,
)
from orchestrator.wildidea_adapter import WildIdeaAdapter
from orchestrator.writedaemon import WriteDaemon

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
POLICY = yaml.safe_load((SYSTEM_ROOT / "policies" / "policy.yaml").read_text(encoding="utf-8"))
NO_BUDGET_POLICY = {**POLICY, "budget": {**POLICY["budget"], "session_max": None}}
SCHEMAS = SchemaSet(SYSTEM_ROOT / "schemas")
SKILLS = {s: f"[skill:{s}]" for s in ("idea", "plan", "bundle", "reasoning")}

_GOOD_SELECTION = {"next_question_id": None, "next_intent": "terminate", "scores": [],
                   "terminate_reason_md": "目标达成"}


class MockRunner:
    """脚本化 runner：按调用次序吐预置 Artifact（或抛 RunnerError）。记录收到的 skill 供断言反馈。"""
    def __init__(self, scripted):
        self.scripted = list(scripted)
        self.skills_seen = []

    def run_task(self, *, system_prompt, skill, context_pack):
        self.skills_seen.append(skill)
        item = self.scripted.pop(0)
        if isinstance(item, Exception):
            raise item
        return Artifact(stage=context_pack.stage, files=item, md="")


class _FakeNoveltyProvider:
    """Deterministic trusted-host stand-in; unit tests never use the network."""
    name = "literature_federated_v1"

    def search(self, query, *, policy_hash):
        result_hash = "sha256:" + hashlib.sha256(
            query.encode("utf-8")).hexdigest()
        snapshot_hash = "sha256:" + hashlib.sha256(
            ("snapshot\x00" + query).encode("utf-8")).hexdigest()
        return {
            "final_ref": {
                "query": query,
                "provider": self.name,
                "snapshot_hash": snapshot_hash,
                "snapshot_ref": (
                    "state/novelty/snapshots/sha256/"
                    + snapshot_hash.removeprefix("sha256:") + ".json"),
                "raw_content_hash": "sha256:" + "a" * 64,
                "result_content_hashes": [result_hash],
                "ranking": [result_hash],
                "policy_hash": policy_hash,
            },
            "results": [{
                "rank": 1,
                "result_content_hash": result_hash,
                "id": "https://arxiv.org/abs/fixture",
                "title": "Fixture result",
            }],
        }


def _operator_factory(factory):
    """Explicitly declare the exact injected-runner persistence contract."""
    factory.bundle_operator_session_contract = BUNDLE_OPERATOR_SESSION_CONTRACT
    return factory


def _operator_runner(runner):
    runner.bundle_operator_session_contract = BUNDLE_OPERATOR_SESSION_CONTRACT
    return runner


def _resident_factory(factory):
    factory.stage_main_session_contract = STAGE_MAIN_SESSION_CONTRACT
    return factory


def _resident_runner(runner):
    runner.stage_main_session_contract = STAGE_MAIN_SESSION_CONTRACT
    return runner


def _bundle_task_factory(factory):
    factory.stage_main_session_contract = STAGE_MAIN_SESSION_CONTRACT
    factory.bundle_task_session_contract = BUNDLE_TASK_SESSION_CONTRACT
    return factory


def _bundle_task_runner(runner):
    runner.stage_main_session_contract = STAGE_MAIN_SESSION_CONTRACT
    runner.bundle_task_session_contract = BUNDLE_TASK_SESSION_CONTRACT
    return runner


def _provider(scripted, work):
    runner = MockRunner(scripted)
    sp = StageProvider(runner_factory=lambda td, pt: runner, schemas=SCHEMAS,
                       policy=NO_BUDGET_POLICY, system_prompt="SYS", skills=SKILLS, work_root=str(work))
    return sp, runner


def _pack(stage):
    sources = []
    if stage == "plan":
        plan_hash = hashlib.sha256(json.dumps(
            _PLAN, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
        sources.append(f"staging:plan-draft:{plan_hash}")
    return NS(cycle_id="c1", stage=stage,
              # Bundle calls are target-scoped in production so their durable
              # runner purpose cannot collapse across parallel build targets.
              target_id=(7 if stage == "bundle" else None),
              anchor_md="", neighborhood_md="",
              retrieval_md="", refs=[], sources=sources)


_FIX = SYSTEM_ROOT / "tests" / "fixtures" / "valid"
_IDEA = json.loads((_FIX / "idea_set" / "wildidea.json").read_text(encoding="utf-8"))
_BYPASS_IDEA = json.loads(
    (_FIX / "idea_set" / "bypass.json").read_text(encoding="utf-8"))
_PLAN = json.loads((_FIX / "plan" / "attack.json").read_text(encoding="utf-8"))
_IMPORT_SEARCH_REQUEST = json.loads(
    (_FIX / "import_search_request" / "new_structure.json").read_text(encoding="utf-8"))


def _real_pack(cycle_id="c1", *, anchor="GENERATOR SECRET CONTEXT"):
    pack = ContextPack(
        cycle_id=cycle_id, stage="idea", target_id=None,
        anchor_md=anchor, neighborhood_md="PRIOR IDEA SECRET",
        retrieval_md="PRIVATE RETRIEVAL", refs=[], sources=["db:question:1"])
    pack.pack_hash = hashlib.sha256(
        ("\x00".join((pack.anchor_md, pack.neighborhood_md,
                       pack.retrieval_md, "[]"))).encode("utf-8")
    ).hexdigest()
    return pack


def _audit_source(cycle_id):
    return _real_pack(cycle_id, anchor="QUESTION ONLY").__class__(
        cycle_id=cycle_id, stage="idea", target_id=None,
        anchor_md="QUESTION ONLY", neighborhood_md="", retrieval_md="",
        refs=[], sources=["db:question:1"],
        pack_hash=hashlib.sha256(
            ("\x00".join(("QUESTION ONLY", "", "", "[]"))).encode("utf-8")
        ).hexdigest())


def _bypass_draft():
    draft = {
        "need_innovation": False,
        "candidates": json.loads(json.dumps(_BYPASS_IDEA["candidates"], ensure_ascii=False)),
        "novelty_refs": [],
    }
    draft["candidates"][0]["novelty_queries"] = [
        "cross dataset EEG representation generalization"]
    return draft


def _bypass_audit():
    return {
        "audit_scores": json.loads(json.dumps(
            _BYPASS_IDEA["audit_scores"], ensure_ascii=False)),
        "selected_id": "c1",
    }


# ============ 正常产出（三阶段各直测 schema 校验路径）============
def test_idea_returns_validated_files(tmp_path):
    sp, _ = _provider([{"idea_set.json": _IDEA}], tmp_path)
    out = sp.idea(NS(cycle_id="c1"), _pack("idea"))
    assert out == {"idea_set.json": _IDEA}                     # 真 idea_set fixture 过 schema


class _RecordingLedger:
    def __init__(self):
        self.next_id = 1
        self.begun = []
        self.finished = []

    def begin_call(self, *, cycle_id, phase, purpose):
        runner_call_id = self.next_id
        self.next_id += 1
        self.begun.append((runner_call_id, cycle_id, phase, purpose))
        return runner_call_id

    def mark_call_running(self, **_kwargs):
        return None

    def abort_unstarted_call(self, **_kwargs):
        return None

    def finish_call(self, **kwargs):
        self.finished.append(kwargs)


class _SqlLifecycleLedger:
    """Small durable runner_call ledger used by Worker lifecycle seam tests."""

    def __init__(self, daemon):
        self.daemon = daemon

    def begin_call(self, *, cycle_id, phase, purpose):
        with self.daemon.transaction() as conn:
            return conn.execute(
                "INSERT INTO runner_call(cycle_id,phase,purpose,status) "
                "VALUES (?,?,?,'created')",
                (int(str(cycle_id).removeprefix("c")), phase, purpose),
            ).lastrowid

    def mark_call_running(self, *, runner_call_id, transcript_ref):
        with self.daemon.transaction() as conn:
            changed = conn.execute(
                "UPDATE runner_call SET status='running',transcript_ref=? "
                "WHERE id=? AND status='created'",
                (transcript_ref, runner_call_id),
            ).rowcount
            assert changed == 1

    def abort_unstarted_call(self, *, runner_call_id, failure_kind):
        with self.daemon.transaction() as conn:
            conn.execute(
                "UPDATE runner_call SET status='aborted',failure_kind=? "
                "WHERE id=? AND status IN ('created','running')",
                (failure_kind, runner_call_id),
            )

    def finish_call(
            self, *, runner_call_id, status, usage, failure_kind=None,
            transcript_ref=None, execution_receipt_ref=None,
            provider_receipt_ref=None):
        del usage
        with self.daemon.transaction() as conn:
            row = conn.execute(
                "SELECT cycle_id FROM runner_call WHERE id=?",
                (runner_call_id,),
            ).fetchone()
            changed = conn.execute(
                "UPDATE runner_call SET status=?,failure_kind=?,"
                "transcript_ref=COALESCE(?,transcript_ref) "
                "WHERE id=? AND status='running'",
                (status, failure_kind, transcript_ref, runner_call_id),
            ).rowcount
            assert changed == 1
            if provider_receipt_ref is not None:
                conn.execute(
                    "INSERT INTO decision(cycle_id,actor,type,payload_json) "
                    "VALUES (?,'orchestrator',"
                    "'provider_invocation_accounted',?)",
                    (row[0], json.dumps({
                        "protocol": "provider-accounting-v1",
                        "runner_call_id": runner_call_id,
                        "provider_receipt_ref": provider_receipt_ref,
                        "execution_receipt_ref": execution_receipt_ref,
                        "runner_terminal_status": status,
                    }, sort_keys=True)),
                )


def _bundle_task_daemon(tmp_path):
    conn = db.connect(tmp_path / "bundle-tasks.sqlite")
    daemon = WriteDaemon(conn)
    with daemon.transaction() as sql:
        sql.execute(
            "INSERT INTO goal(id,version,text,predicate_json) "
            "VALUES (1,1,'goal','{}')")
        sql.execute(
            "INSERT INTO cycle(id,goal_id,goal_ver,status,policy_version) "
            "VALUES (1,1,1,'bundle','test')")
        sql.execute(
            "INSERT INTO build_target("
            "id,cycle_id,target_kind,seq,status) "
            "VALUES (7,1,'build',1,'pending')")
    return conn, daemon


def _review_payload_hash(payload):
    return "sha256:" + hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _test_verified_reviews(conn, *, cycle_id):
    return [
        (int(decision_id), json.loads(raw))
        for decision_id, raw in conn.execute(
            "SELECT id,payload_json FROM decision "
            "WHERE cycle_id=? AND actor='agent' "
            "AND type='runtime_review' ORDER BY id",
            (cycle_id,),
        ).fetchall()
    ]


def _record_authoritative_review_children(
        daemon, *, runner_call_id, purpose, target_id=7):
    with daemon.transaction() as conn:
        for review_kind, child_thread_id, role_tag in (
                ("bundle_code", "thread-code-review", "code"),
                ("bundle_result", "thread-result-review", "result")):
            receipt = {
                "protocol": "native-review-receipt-v1",
                "review_request_id": f"nrr-{role_tag}-000000000001",
                "cycle_id": "c1",
                "stage": "bundle",
                "target_id": str(target_id),
                "purpose": purpose,
                "review_kind": review_kind,
                "round_no": 1,
                "configured_rounds": 1,
                "runner_call_id": runner_call_id,
                "child_thread_id": child_thread_id,
                "verdict": "pass",
                "resulting_subject_hash": (
                    "sha256:" + (
                        "a" if review_kind == "bundle_code"
                        else "b") * 64),
            }
            receipt["receipt_hash"] = _review_payload_hash(receipt)
            review_id = conn.execute(
                "INSERT INTO decision(cycle_id,actor,type,payload_json) "
                "VALUES (1,'agent','runtime_review',?)",
                (json.dumps(receipt, sort_keys=True),),
            ).lastrowid
            snapshot_ref = f"/proof/{role_tag}-{runner_call_id}.json"
            proof = {
                "protocol": "native-review-live-owner-proof-v1",
                "review_decision_id": review_id,
                "review_receipt_hash": receipt["receipt_hash"],
                "cycle_id": "c1",
                "stage": "bundle",
                "target_id": str(target_id),
                "purpose": purpose,
                "runner_call_id": runner_call_id,
                "child_thread_id": child_thread_id,
                "snapshot_ref": snapshot_ref,
            }
            conn.execute(
                "INSERT INTO decision(cycle_id,actor,type,payload_json) "
                "VALUES (1,'orchestrator',"
                "'native_review_live_owner_proof',?)",
                (json.dumps(proof, sort_keys=True),),
            )
            if review_kind == "bundle_code":
                selector = {
                    "protocol": "runtime-stage-submission-index-v1",
                    "stage": "bundle",
                    "target_id": str(target_id),
                    "review_decision_id": review_id,
                    "artifact_hash": receipt["resulting_subject_hash"],
                }
                conn.execute(
                    "INSERT INTO decision(cycle_id,actor,type,payload_json) "
                    "VALUES (1,'agent','runtime_stage_submission',?)",
                    (json.dumps(selector, sort_keys=True),),
                )
            else:
                selector = {
                    "protocol": "native-bundle-result-review-ack-v2",
                    "cycle_id": "c1",
                    "build_target_id": target_id,
                    "review_decision_id": review_id,
                    "review_receipt_hash": receipt["receipt_hash"],
                    "subject_hash": receipt["resulting_subject_hash"],
                }
                conn.execute(
                    "INSERT INTO decision(cycle_id,actor,type,payload_json) "
                    "VALUES (1,'orchestrator',"
                    "'runtime_bundle_result_review_ack',?)",
                    (json.dumps(selector, sort_keys=True),),
                )
        conn.execute(
            "UPDATE build_target SET status='complete' WHERE id=?",
            (target_id,),
        )


def _wildidea_provider(scripted, tmp_path, *, bridge=None, adapter=None):
    purposes = []
    packs = []
    runner = MockRunner(scripted)
    original_run = runner.run_task

    def capture_run(*, system_prompt, skill, context_pack):
        packs.append(context_pack)
        return original_run(
            system_prompt=system_prompt, skill=skill, context_pack=context_pack)

    runner.run_task = capture_run
    ledger = _RecordingLedger()

    def factory(transcripts, purpose):
        Path(transcripts).mkdir(parents=True, exist_ok=True)
        purposes.append(purpose)
        return runner

    adapter = adapter or WildIdeaAdapter(
        SYSTEM_ROOT, NO_BUDGET_POLICY,
        novelty_provider=_FakeNoveltyProvider())
    provider = StageProvider(
        runner_factory=factory, schemas=SCHEMAS, policy=NO_BUDGET_POLICY,
        system_prompt="SYS", skills=SKILLS, work_root=str(tmp_path),
        file_request_bridge=bridge, cost_ledger=ledger,
        wildidea_adapter=adapter, idea_audit_pack_builder=_audit_source)
    return provider, runner, purposes, packs, ledger


def test_wildidea_two_sessions_are_blind_accounted_and_repeatable(tmp_path):
    scripted = [
        {"idea_set.draft.json": _bypass_draft()},
        {"idea_audit.json": _bypass_audit()},
        {"idea_set.draft.json": _bypass_draft()},
        {"idea_audit.json": _bypass_audit()},
    ]
    sp, _runner, purposes, packs, ledger = _wildidea_provider(scripted, tmp_path)

    first = sp.idea(NS(cycle_id="c1"), _real_pack("c1"))["idea_set.json"]
    second = sp.idea(NS(cycle_id="c2"), _real_pack("c2"))["idea_set.json"]

    assert first["selected_id"] == second["selected_id"] == "c1"
    assert first["provenance"]["engine_version"].startswith("wildidea@6ff66ada")
    assert purposes == [
        "idea-generate-n1", "idea-audit-n2",
        "idea-generate-n3", "idea-audit-n4",
    ]
    assert [(row[2], row[3]) for row in ledger.begun] == [
        ("idea", "idea-generate-n1-a1"),
        ("audit", "idea-audit-n2-a1"),
        ("idea", "idea-generate-n3-a1"),
        ("audit", "idea-audit-n4-a1"),
    ]
    assert all(item["status"] == "success" for item in ledger.finished)
    # Generator sees its sampled pack; judge gets a separate question-only pack
    # plus candidate_id/audit_mapping, never generator secrets or sampled anchors.
    assert "WildIdea adapter sampled slots" in packs[0].retrieval_md
    assert packs[1].anchor_md == "QUESTION ONLY"
    audit_bytes = "\n".join((
        packs[1].anchor_md, packs[1].neighborhood_md, packs[1].retrieval_md))
    assert "GENERATOR SECRET CONTEXT" not in audit_bytes
    assert "PRIOR IDEA SECRET" not in audit_bytes
    assert "WildIdea adapter sampled slots" not in audit_bytes


def test_wildidea_exact_files_semantic_retry_and_audit_sidecar_forbidden(tmp_path):
    bridge_calls = []
    scripted = [
        # Required draft plus an invented file is rejected, not silently ignored.
        {"idea_set.draft.json": _bypass_draft(), "idea_audit.json": _bypass_audit()},
        # Schema-valid but adapter-invalid model provenance is an artifact retry.
        {"idea_set.draft.json": {
            **_bypass_draft(), "provenance": {"model": "forged"}}},
        {"idea_set.draft.json": _bypass_draft()},
        # A judge can never turn its complete blind pack into a user file request.
        {"idea_audit.json": _bypass_audit(), "resource_request.json": {"bad": True}},
        {"idea_audit.json": _bypass_audit(), "extra.json": {}},
        {"idea_audit.json": _bypass_audit()},
    ]
    sp, runner, purposes, _packs, ledger = _wildidea_provider(
        scripted, tmp_path,
        bridge=lambda *args: bridge_calls.append(args))

    final = sp.idea(NS(cycle_id="c1"), _real_pack("c1"))["idea_set.json"]

    assert final["selected_id"] == "c1"
    assert bridge_calls == []
    assert purposes == ["idea-generate-n1", "idea-audit-n2"]
    assert "files 必须恰为" in runner.skills_seen[1]
    assert "provenance" in runner.skills_seen[2]
    assert "禁止产出 resource_request.json" in runner.skills_seen[4]
    assert "files 必须恰为" in runner.skills_seen[5]
    assert [item["status"] for item in ledger.finished] == [
        "failed", "failed", "success", "failed", "failed", "success"]


def test_wildidea_post_validate_exception_closes_cost_intent(tmp_path):
    class ExplodingAdapter(WildIdeaAdapter):
        def validate_draft(self, draft):
            raise RuntimeError("validator exploded")

    adapter = ExplodingAdapter(
        SYSTEM_ROOT, NO_BUDGET_POLICY,
        novelty_provider=_FakeNoveltyProvider())
    sp, _runner, _purposes, _packs, ledger = _wildidea_provider(
        [{"idea_set.draft.json": _bypass_draft()}], tmp_path, adapter=adapter)

    with pytest.raises(RuntimeError, match="validator exploded"):
        sp.idea(NS(cycle_id="c1"), _real_pack("c1"))
    assert ledger.finished[-1]["status"] == "failed"
    assert ledger.finished[-1]["failure_kind"] == "postprocess_error"


def test_plan_returns_validated_files(tmp_path):
    sp, _ = _provider([{"plan.json": _PLAN}], tmp_path)
    out = sp.plan(NS(cycle_id="c1"), _pack("plan"))
    assert out == {"plan.json": _PLAN}


def test_plan_may_return_import_search_control_sidecar_alone(tmp_path):
    sp, _ = _provider(
        [{"import_search_request.json": _IMPORT_SEARCH_REQUEST}], tmp_path)
    out = sp.plan(NS(cycle_id="c1"), _pack("plan"))
    assert out == {"import_search_request.json": _IMPORT_SEARCH_REQUEST}


def test_plan_search_sidecar_cannot_coexist_with_plan(tmp_path):
    sp, runner = _provider([
        {"import_search_request.json": _IMPORT_SEARCH_REQUEST, "plan.json": _PLAN},
        {"plan.json": _PLAN},
    ], tmp_path)
    assert sp.plan(NS(cycle_id="c1"), _pack("plan")) == {"plan.json": _PLAN}
    assert "独占 files" in runner.skills_seen[1]


def test_plan_search_sidecar_accepts_structural_stuck_survey_request(tmp_path):
    request = {**_IMPORT_SEARCH_REQUEST, "trigger_kind": "stuck"}
    sp, _ = _provider([
        {"import_search_request.json": request}], tmp_path)
    assert sp.plan(NS(cycle_id="c1"), _pack("plan")) == {
        "import_search_request.json": request}


def test_plan_search_sidecar_rejects_human_named_without_authority(tmp_path):
    bad = {**_IMPORT_SEARCH_REQUEST, "trigger_kind": "human_named"}
    sp, runner = _provider([
        {"import_search_request.json": bad}, {"plan.json": _PLAN}], tmp_path)
    assert sp.plan(NS(cycle_id="c1"), _pack("plan")) == {"plan.json": _PLAN}
    assert "source_authority_hash" in runner.skills_seen[1]


def test_plan_gpu_compatibility_flag_derives_from_abstract_resource_count(
        tmp_path):
    plan = json.loads(json.dumps(_PLAN))
    plan["targets"][0]["resources"] = {"gpu_count": 0}
    plan["targets"][0]["gpu_required"] = True
    policy = json.loads(json.dumps(NO_BUDGET_POLICY))
    policy["resources"]["gpu_target_policy"] = "required"
    runner = MockRunner([{"plan.json": plan}])
    provider = StageProvider(
        runner_factory=lambda _td, _purpose: runner,
        schemas=SCHEMAS, policy=policy, system_prompt="SYS",
        skills=SKILLS, work_root=str(tmp_path))

    result = provider.plan(
        NS(cycle_id="c1"), _pack("plan"))["plan.json"]

    assert result["targets"][0]["resources"] == {"gpu_count": 0}
    assert result["targets"][0]["gpu_required"] is False
    assert result["targets"][1]["gpu_required"] is True


def test_reasoning_returns_validated_files(tmp_path):
    sp, _ = _provider([{"selection.json": _GOOD_SELECTION}], tmp_path)
    out = sp.reasoning(NS(cycle_id="c1", question_id=None), _pack("reasoning"))
    assert out == {"selection.json": _GOOD_SELECTION}          # 必产在场、过 schema


def test_optional_files_passed_through_when_present(tmp_path):
    files = {"selection.json": {"next_question_id": "q1", "next_intent": "decompose", "scores": []},
             "tree_ops.json": {"ops": [{"op": "add_children", "parent_question_id": "q1",
                                        "children": [{"text": "子", "local_key": "c"}]}]}}
    sp, _ = _provider([files], tmp_path)
    out = sp.reasoning(NS(cycle_id="c1", question_id="q1"), _pack("reasoning"))
    assert set(out) == {"selection.json", "tree_ops.json"}     # optional 在场 → 一并返回


# ============ 阶段必产缺失 → 重试 ============
def test_missing_required_file_retries_then_succeeds(tmp_path):
    sp, runner = _provider([{"md_only": 1}, {"selection.json": _GOOD_SELECTION}], tmp_path)
    out = sp.reasoning(NS(cycle_id="c1", question_id=None), _pack("reasoning"))
    assert out == {"selection.json": _GOOD_SELECTION}
    assert "缺阶段必产文件" in runner.skills_seen[1]           # 第 2 次调用带上了缺失反馈


# ============ schema 非法 → 重试并附反馈 ============
def test_schema_invalid_retries_with_feedback(tmp_path):
    bad = {"selection.json": {"next_intent": "terminate"}}     # 缺 next_question_id/scores
    sp, runner = _provider([bad, {"selection.json": _GOOD_SELECTION}], tmp_path)
    out = sp.reasoning(NS(cycle_id="c1", question_id=None), _pack("reasoning"))
    assert out == {"selection.json": _GOOD_SELECTION}
    assert "schema 校验失败" in runner.skills_seen[1]          # 反馈含 schema 错误


def test_retries_exhausted_raises(tmp_path):
    bad = {"selection.json": {"bogus": 1}}
    n = POLICY["flow"]["retry"]["artifact_parse"] + 1
    sp, runner = _provider([bad] * n, tmp_path)
    with pytest.raises(RunnerError, match="重试.*用尽"):
        sp.reasoning(NS(cycle_id="c1", question_id=None), _pack("reasoning"))
    assert len(runner.skills_seen) == n                        # 恰好 N+1 次调用（首次 + N 重试）


def test_runner_error_counts_as_retry(tmp_path):
    sp, _ = _provider([RunnerError("超时"), {"selection.json": _GOOD_SELECTION}], tmp_path)
    out = sp.reasoning(NS(cycle_id="c1", question_id=None), _pack("reasoning"))
    assert out == {"selection.json": _GOOD_SELECTION}          # 进程失败也走重试


def test_stage_drift_retries(tmp_path):
    """外审 SHOULD 回归：文件结构对但 envelope stage 漂移 → 计入重试（审计/回放语义）。"""
    class DriftRunner(MockRunner):
        def run_task(self, *, system_prompt, skill, context_pack):
            self.skills_seen.append(skill)
            item = self.scripted.pop(0)
            st, files = item                                   # (stage, files) 元组：显式指定 envelope stage
            return Artifact(stage=st, files=files, md="")
    runner = DriftRunner([("plan", {"selection.json": _GOOD_SELECTION}),      # 漂移
                          ("reasoning", {"selection.json": _GOOD_SELECTION})])
    sp = StageProvider(runner_factory=lambda td, pt: runner, schemas=SCHEMAS, policy=NO_BUDGET_POLICY,
                       system_prompt="S", skills=SKILLS, work_root=str(tmp_path))
    out = sp.reasoning(NS(cycle_id="c1", question_id=None), _pack("reasoning"))
    assert out == {"selection.json": _GOOD_SELECTION}
    assert "stage 漂移" in runner.skills_seen[1]               # 反馈含漂移原因


def test_transcript_purpose_unique_per_call(tmp_path):
    """实例内 purpose 标签递增；生产 transcript 的跨重启唯一性另由 runner_call_id 保证。"""
    seen = []
    sp = StageProvider(runner_factory=lambda td, pt: (seen.append(pt), MockRunner(
        [{"selection.json": _GOOD_SELECTION}]))[1], schemas=SCHEMAS, policy=NO_BUDGET_POLICY,
        system_prompt="S", skills=SKILLS, work_root=str(tmp_path))
    sp.reasoning(NS(cycle_id="c1", question_id=None), _pack("reasoning"))
    sp.reasoning(NS(cycle_id="c1", question_id=None), _pack("reasoning"))
    assert seen == ["reasoning-n1", "reasoning-n2"]


# ============ sidecar fail-loud（内审 SHOULD：不静默丢弃资源请求）============
def test_resource_request_sidecar_fails_loud(tmp_path):
    files = {"selection.json": _GOOD_SELECTION, "resource_request.json": {"x": 1}}
    sp, _ = _provider([files], tmp_path)
    with pytest.raises(RunnerError, match="resource_request"):
        sp.reasoning(NS(cycle_id="c1", question_id=None), _pack("reasoning"))


def test_resource_request_schema_feedback_aggregates_all_root_errors(tmp_path):
    """One expensive retry receives every root schema defect, not one oscillating key."""
    bad = {"version": 1, "reason_md": "need file"}
    runner = MockRunner([
        {"resource_request.json": bad},
        {"selection.json": _GOOD_SELECTION},
    ])
    bridge_calls = []
    sp = StageProvider(
        runner_factory=lambda td, pt: runner, schemas=SCHEMAS,
        policy=NO_BUDGET_POLICY, system_prompt="S", skills=SKILLS,
        work_root=str(tmp_path),
        file_request_bridge=lambda stage, request, cyc: bridge_calls.append(request))
    assert sp.reasoning(NS(cycle_id="c1", question_id=None), _pack("reasoning")) == {
        "selection.json": _GOOD_SELECTION}
    feedback = runner.skills_seen[1]
    assert "summary_md" in feedback and "items" in feedback
    assert "version" in feedback and "reason_md" in feedback
    assert bridge_calls == []


# ============ answer.json 语义边界（内审 #2：reasoning-only 轮不因幻觉 answer 误关问）============
def test_spurious_answer_in_bootstrap_does_not_close(tmp_path):
    """StageProvider 只保证 answer.json 结构合法、透传；语义由组件把关——advancer 的 reasoning-only 轮
    根本不读 answer.json（关问经 attack 轮 gate_close_question 的 I3 证据闸），故幻觉 answer 不误关。"""
    from orchestrator.advancer import SqliteAdvancer
    from orchestrator.compiler_sqlite import SqliteCompiler
    from orchestrator.statestore_sqlite import SQLiteStateStore
    from orchestrator.writedaemon import WriteDaemon
    ans = json.loads((_FIX / "answer" / sorted(p.name for p in (_FIX / "answer").iterdir())[0]).read_text("utf-8"))
    path = str(tmp_path / "sa.sqlite")
    daemon = WriteDaemon(db.connect(path))
    state = SQLiteStateStore(daemon, POLICY); state.create_goal(text="g", predicate_json={})
    compiler = SqliteCompiler(db.connect(path), POLICY)
    boot = {"tree_ops.json": {"ops": [{"op": "create_root", "text": "根", "local_key": "root"}]},
            "selection.json": {"next_question_id": None, "next_intent": "terminate", "scores": [],
                               "terminate_reason_md": "止"},
            "answer.json": ans}                               # 幻觉 answer 混进 bootstrap 产物
    sp, _ = _provider([boot], tmp_path)
    SqliteAdvancer(state, compiler, sp.reasoning).run_cycles(max_cycles=2)
    assert daemon.query_one("SELECT count(*) FROM answer")[0] == 0   # 无问题被关（answer 被 advancer 忽略）


# ============ 与真 SqliteAdvancer 端到端（mock runner）============
def test_end_to_end_with_real_advancer(tmp_path):
    """真 SqliteAdvancer + 真 SqliteCompiler + StageProvider(mock runner) 跑通 bootstrap 创世轮：
    证 (cyc,pack)→files 契约与组件对得上（组件渲 pack→调 provider→落库）。"""
    from orchestrator.advancer import SqliteAdvancer
    from orchestrator.compiler_sqlite import SqliteCompiler
    from orchestrator.statestore_sqlite import SQLiteStateStore
    from orchestrator.writedaemon import WriteDaemon
    path = str(tmp_path / "e.sqlite")
    daemon = WriteDaemon(db.connect(path))
    state = SQLiteStateStore(daemon, POLICY)
    state.create_goal(text="EEG 通用规律", predicate_json={})
    compiler = SqliteCompiler(db.connect(path), POLICY)
    boot = {"tree_ops.json": {"ops": [{"op": "create_root", "text": "根问题", "local_key": "root"}]},
            "selection.json": {"next_question_id": None, "next_intent": "terminate", "scores": [],
                               "terminate_reason_md": "创世即终止"}}
    sp, _ = _provider([boot], tmp_path)
    adv = SqliteAdvancer(state, compiler, sp.reasoning)        # 直接把 provider.reasoning 注入
    ids = adv.run_cycles(max_cycles=3)
    assert len(ids) == 1                                       # bootstrap 轮跑通 + terminate
    assert state.cycle(ids[0]).status == "done"
    assert daemon.query_one("SELECT count(*) FROM question WHERE text='根问题'")[0] == 1   # 真落库


# ============ CP8.3 · bundle 阶段（passthrough）============
_MANIFEST = json.loads((_FIX / "execution_manifest" / "build_toy.json").read_text(encoding="utf-8"))


def _bundle_envelope():
    return {"execution_manifest.json": _MANIFEST, "identity.md": "# toy\n## 复现命令\npython train.py",
            "train.py": "print('t')", "eval.py": "print('e')", "cfg.json": {"lr": 0.1}}


def test_bundle_scheduler_and_target_worker_bind_distinct_resident_tasks(
        tmp_path, monkeypatch):
    class ResidentRunner(MockRunner):
        def __init__(self, scripted, *, receipt_tag):
            super().__init__(scripted)
            self.bound = []
            self.receipt_tag = receipt_tag
            self.runner_call_id = None
            self.purpose = None

        def bind_persistent_session(self, *, session_id, role):
            self.bound.append((session_id, role))

        def bind_runner_call(
                self, *, runner_call_id, reconcile_protocol,
                phase, purpose):
            del reconcile_protocol, phase
            self.runner_call_id = runner_call_id
            self.purpose = purpose

        def run_task(self, *, system_prompt, skill, context_pack):
            artifact = super().run_task(
                system_prompt=system_prompt, skill=skill,
                context_pack=context_pack)
            if self.receipt_tag == "worker":
                _record_authoritative_review_children(
                    daemon, runner_call_id=self.runner_call_id,
                    purpose=self.purpose)
            artifact.execution_receipt_ref = (
                f"/execution/{self.receipt_tag}.json")
            artifact.provider_receipt_ref = (
                f"/provider/{self.receipt_tag}.json")
            return artifact

    conn, daemon = _bundle_task_daemon(tmp_path)
    ledger = _SqlLifecycleLedger(daemon)
    purposes = []
    runners = []

    @_bundle_task_factory
    def factory(_transcripts, purpose):
        purposes.append(purpose)
        scripted = [{}] if purpose.startswith("bundle-scheduler-") else [
            _bundle_envelope()]
        receipt_tag = (
            "scheduler" if purpose.startswith("bundle-scheduler-")
            else "worker")
        runner = _bundle_task_runner(ResidentRunner(
            scripted, receipt_tag=receipt_tag))
        runners.append(runner)
        return runner

    monkeypatch.setattr(
        "orchestrator.stage_provider.load_provider_invocation_receipt",
        lambda path, **_kwargs: NS(
            provider_invocation_id=(
                "thread-scheduler"
                if str(path).endswith("scheduler.json")
                else "thread-worker"),
            execution_outcome="exit",
            execution_returncode=0,
        ))
    monkeypatch.setattr(
        "orchestrator.bundle_tasks._load_verified_native_reviews",
        _test_verified_reviews)
    reconciliations = []
    reconcile = BundleTaskRegistry.reconcile_terminal_workers

    def record_reconciliation(registry, cycle_id):
        reconciliations.append(cycle_id)
        return reconcile(registry, cycle_id)

    monkeypatch.setattr(
        BundleTaskRegistry, "reconcile_terminal_workers",
        record_reconciliation)
    provider = StageProvider(
        runner_factory=factory, schemas=SCHEMAS, policy=NO_BUDGET_POLICY,
        system_prompt="SYS", skills=SKILLS, work_root=str(tmp_path),
        cost_ledger=ledger, resident_stage_sessions=True,
        inline_subagent_review=True)
    scheduler_pack = NS(
        cycle_id="c1", stage="bundle", target_id=None,
        anchor_md="", neighborhood_md="", retrieval_md="", refs=[],
        sources=[], pack_hash="scheduler-pack")

    assert provider.bundle_scheduler(
        NS(cycle_id="c1"), scheduler_pack) == {}
    assert reconciliations == ["c1", "c1"]
    worker_result = provider.bundle_worker(
        NS(cycle_id="c1"), _pack("bundle"))

    assert worker_result["execution_manifest.json"] == _MANIFEST
    assert purposes == [
        "bundle-scheduler-c1-n1",
        "bundle-worker-c1-t7-n2",
    ]
    assert runners[0].bound == [(None, "bundle_scheduler")]
    assert runners[1].bound == [(None, "target_worker")]
    worker_skill = runners[1].skills_seen[0]
    assert "bundle_next_target" not in worker_skill
    assert 'mode="snapshot"' in worker_skill
    assert 'mode="incremental"' in worker_skill
    assert "60→120→300→600→1800" in worker_skill
    assert "自己的 target" in worker_skill
    assert worker_skill.index('mode="snapshot"') < worker_skill.index(
        "bundle_execute 异步启动")
    assert "bundle_execute 返回的更新 cursor" in worker_skill
    assert daemon.query_one(
        "SELECT build_target_id,role,provider_task_id,status,receipt_ref "
        "FROM bundle_worker_task WHERE role='worker'") == (
            7, "worker", "thread-worker", "completed",
            "/provider/worker.json")
    assert daemon.query_one(
        "SELECT count(*) FROM bundle_worker_task "
        "WHERE build_target_id=7") == (3,)
    conn.close()


def test_bundle_worker_provider_interruption_resumes_same_durable_task(
        tmp_path, monkeypatch):
    class ResidentRunner(MockRunner):
        def __init__(self, scripted, *, receipt_tag):
            super().__init__(scripted)
            self.bound = []
            self.receipt_tag = receipt_tag
            self.runner_call_id = None
            self.purpose = None

        def bind_persistent_session(self, *, session_id, role):
            self.bound.append((session_id, role))

        def bind_runner_call(
                self, *, runner_call_id, reconcile_protocol,
                phase, purpose):
            del reconcile_protocol, phase
            self.runner_call_id = runner_call_id
            self.purpose = purpose

        def run_task(self, *, system_prompt, skill, context_pack):
            artifact = super().run_task(
                system_prompt=system_prompt, skill=skill,
                context_pack=context_pack)
            _record_authoritative_review_children(
                daemon, runner_call_id=self.runner_call_id,
                purpose=self.purpose)
            artifact.execution_receipt_ref = (
                f"/execution/{self.receipt_tag}.json")
            artifact.provider_receipt_ref = (
                f"/provider/{self.receipt_tag}.json")
            return artifact

    conn, daemon = _bundle_task_daemon(tmp_path)
    ledger = _SqlLifecycleLedger(daemon)
    runners = []

    @_bundle_task_factory
    def factory(_transcripts, _purpose):
        ordinal = len(runners) + 1
        receipt_tag = f"worker-{ordinal}"
        scripted = (
            [RunnerError(
                "transport interrupted",
                failure_kind="transport",
                execution_receipt_ref=(
                    f"/execution/{receipt_tag}.json"),
                provider_receipt_ref=(
                    f"/provider/{receipt_tag}.json"))]
            if ordinal == 1 else [_bundle_envelope()]
        )
        runner = _bundle_task_runner(ResidentRunner(
            scripted, receipt_tag=receipt_tag))
        runners.append(runner)
        return runner

    monkeypatch.setattr(
        "orchestrator.stage_provider.load_provider_invocation_receipt",
        lambda _path, **_kwargs: NS(
            provider_invocation_id="thread-worker",
            execution_outcome="exit",
            execution_returncode=0,
        ))
    monkeypatch.setattr(
        "orchestrator.bundle_tasks._load_verified_native_reviews",
        _test_verified_reviews)
    provider = StageProvider(
        runner_factory=factory, schemas=SCHEMAS, policy=NO_BUDGET_POLICY,
        system_prompt="SYS", skills=SKILLS, work_root=str(tmp_path),
        cost_ledger=ledger, resident_stage_sessions=True)

    with pytest.raises(RunnerError, match="transport interrupted"):
        provider.bundle_worker(
            NS(cycle_id="c1"), _pack("bundle"))
    assert daemon.query_one(
        "SELECT provider_task_id,status,receipt_ref "
        "FROM bundle_worker_task") == (
            "thread-worker", "waiting", "/provider/worker-1.json")

    result = provider.bundle_worker(
        NS(cycle_id="c1"), _pack("bundle"))

    assert result["execution_manifest.json"] == _MANIFEST
    assert runners[0].bound == [(None, "target_worker")]
    assert runners[1].bound == [("thread-worker", "target_worker")]
    assert daemon.query_one(
        "SELECT count(*),min(id),max(id) FROM bundle_worker_task "
        "WHERE role='worker'") == (
            1, 1, 1)
    assert daemon.query_one(
        "SELECT provider_task_id,status,receipt_ref "
        "FROM bundle_worker_task WHERE role='worker'") == (
            "thread-worker", "completed", "/provider/worker-1.json")
    assert daemon.query(
        "SELECT role,provider_task_id,status FROM bundle_worker_task "
        "WHERE role<>'worker' ORDER BY role") == [
            ("code_review", "thread-code-review", "completed"),
            ("result_review", "thread-result-review", "completed"),
        ]
    conn.close()


def test_bundle_task_scope_rejects_targetless_worker_and_targeted_scheduler(
        tmp_path):
    ledger = _RecordingLedger()
    ledger.daemon = NS(query=lambda _sql, _params: [])

    @_bundle_task_factory
    def factory(_transcripts, _purpose):
        return _bundle_task_runner(MockRunner([]))

    provider = StageProvider(
        runner_factory=factory, schemas=SCHEMAS, policy=NO_BUDGET_POLICY,
        system_prompt="SYS", skills=SKILLS, work_root=str(tmp_path),
        cost_ledger=ledger, resident_stage_sessions=True)
    targetless = NS(
        cycle_id="c1", stage="bundle", target_id=None,
        anchor_md="", neighborhood_md="", retrieval_md="", refs=[],
        sources=[], pack_hash="targetless")

    with pytest.raises(ValueError, match="Scheduler.*target"):
        provider.bundle_scheduler(
            NS(cycle_id="c1"), _pack("bundle"))
    with pytest.raises(ValueError, match="Worker.*target"):
        provider.bundle_worker(
            NS(cycle_id="c1"), targetless)


def _bundle_operator_control(*, event="start", subject_char="a"):
    return {
        "protocol": "bundle-operator-control-v1",
        "build_target_id": 7,
        "phase": "train",
        "event": event,
        "execution_owner": {"kind": "run", "id": 19},
        "plan_slice_hash": "1" * 64,
        "source_tree_hash": "sha256:" + "2" * 64,
        "subject_hash": "sha256:" + subject_char * 64,
        "repair_round": 0,
        "log": {
            "state": "not_started" if event == "start" else "partial",
            "size_bytes": 0,
            "tail_sha256": "sha256:" + "3" * 64,
            "tail_text": "",
            "content_hash": None,
            "exit_code": None,
        },
    }


def _bundle_operator_action(control, action):
    return {
        "version": 1,
        "build_target_id": control["build_target_id"],
        "phase": control["phase"],
        "event": control["event"],
        "action": action,
        "execution_owner": dict(control["execution_owner"]),
        "plan_slice_hash": control["plan_slice_hash"],
        "source_tree_hash": control["source_tree_hash"],
        "subject_hash": control["subject_hash"],
        "diagnosis_md": "checked exact manifest capability",
    }


def test_bundle_passthrough_all_files(tmp_path):
    """bundle 信封全量透传（代码文件名任意、不可枚举）——required 校验后原样返回，物化归组件。"""
    sp, _ = _provider([_bundle_envelope()], tmp_path)
    out = sp.bundle(NS(cycle_id="c1"), _pack("bundle"))
    assert out == _bundle_envelope()                            # 含代码文件与 cfg.json（未被丢弃）


def test_bundle_operator_mode_rejects_undeclared_injected_factory(tmp_path):
    ledger = _RecordingLedger()
    with pytest.raises(ValueError, match="runner_factory 未明确声明"):
        StageProvider(
            runner_factory=lambda _td, _pt: MockRunner([]),
            schemas=SCHEMAS, policy=NO_BUDGET_POLICY,
            system_prompt="SYS", skills=SKILLS, work_root=str(tmp_path),
            cost_ledger=ledger, bundle_operator_mode=True)


def test_bundle_operator_rejects_declared_factory_instance_capability_drift(tmp_path):
    class EmptyDaemon:
        def query(self, _sql, _params):
            return []

    ledger = _RecordingLedger()
    ledger.daemon = EmptyDaemon()
    factory = _operator_factory(lambda _td, _pt: MockRunner([_bundle_envelope()]))
    provider = StageProvider(
        runner_factory=factory, schemas=SCHEMAS, policy=NO_BUDGET_POLICY,
        system_prompt="SYS", skills=SKILLS, work_root=str(tmp_path),
        cost_ledger=ledger, bundle_operator_mode=True)

    with pytest.raises(RuntimeError, match="runner 实例.*漂移"):
        provider.bundle(NS(cycle_id="c1"), _pack("bundle"))


def test_bundle_operator_guard_blocks_turn_before_runner_or_session_resume(tmp_path):
    class SealClosed(RuntimeError):
        pass

    class EmptyDaemon:
        def query(self, _sql, _params):
            return []

    class OperatorRunner(MockRunner):
        def __init__(self):
            super().__init__([_bundle_envelope()])
            self.bound = []

        def bind_persistent_session(self, *, session_id, role="narrator"):
            self.bound.append((session_id, role))

    runner = _operator_runner(OperatorRunner())
    factory_calls = []

    @_operator_factory
    def factory(_td, _pt):
        factory_calls.append(True)
        return runner
    ledger = _RecordingLedger()
    ledger.daemon = EmptyDaemon()
    provider = StageProvider(
        runner_factory=factory, schemas=SCHEMAS, policy=NO_BUDGET_POLICY,
        system_prompt="SYS", skills=SKILLS, work_root=str(tmp_path),
        cost_ledger=ledger, bundle_operator_mode=True,
        bundle_operator_guard=lambda: (_ for _ in ()).throw(
            SealClosed("qualification claim boundary closed")))

    with pytest.raises(SealClosed, match="claim boundary"):
        provider.bundle(NS(cycle_id="c1"), _pack("bundle"))
    assert factory_calls == []
    assert runner.bound == []
    assert runner.skills_seen == []


def test_retired_bundle_operator_does_not_outer_retry_invalid_action(tmp_path):
    control = _bundle_operator_control()
    valid = _bundle_operator_action(control, "start")
    wrong_identity = {**valid, "build_target_id": 8}
    extra_field = {**valid, "argv": ["python", "train.py"]}

    class OperatorRunner(MockRunner):
        def __init__(self):
            super().__init__([
                {"bundle_operator_action.json": wrong_identity},
                {"bundle_operator_action.json": extra_field},
                {"bundle_operator_action.json": valid},
            ])
            self.bound = []

        def bind_persistent_session(self, *, session_id, role="narrator"):
            self.bound.append((session_id, role))

    class EmptyDaemon:
        def query(self, _sql, _params):
            return []

    runner = _operator_runner(OperatorRunner())
    ledger = _RecordingLedger()
    ledger.daemon = EmptyDaemon()
    factory = _operator_factory(lambda _td, _pt: runner)
    provider = StageProvider(
        runner_factory=factory,
        schemas=SCHEMAS, policy=NO_BUDGET_POLICY, system_prompt="SYS",
        skills=SKILLS, work_root=str(tmp_path), cost_ledger=ledger,
        bundle_operator_mode=True)

    with pytest.raises(RunnerError, match="不执行外层 artifact 重试"):
        provider.bundle_operator(NS(cycle_id="c1"), _pack("bundle"), control)

    assert runner.bound == [(None, "bundle_operator")]
    assert len(runner.skills_seen) == 1
    assert [item["status"] for item in ledger.finished] == ["failed"]


def test_bundle_operator_actions_resume_same_cycle_provider_thread(tmp_path, monkeypatch):
    start_control = _bundle_operator_control(event="start", subject_char="a")
    progress_control = _bundle_operator_control(event="progress", subject_char="b")
    scripted = [
        {"bundle_operator_action.json": _bundle_operator_action(start_control, "start")},
        {"bundle_operator_action.json": _bundle_operator_action(progress_control, "continue")},
    ]

    class Daemon:
        rows = []

        def query(self, _sql, _params):
            return list(self.rows)

    class OperatorRunner(MockRunner):
        def __init__(self, item):
            super().__init__([item])
            self.bound = []
            self.packs = []

        def bind_persistent_session(self, *, session_id, role="narrator"):
            self.bound.append((session_id, role))

        def run_task(self, *, system_prompt, skill, context_pack):
            self.packs.append(context_pack)
            return super().run_task(
                system_prompt=system_prompt, skill=skill,
                context_pack=context_pack)

    daemon = Daemon()
    ledger = _RecordingLedger()
    ledger.daemon = daemon
    runners = []
    purposes = []

    @_operator_factory
    def factory(_transcripts, purpose):
        purposes.append(purpose)
        runner = _operator_runner(OperatorRunner(scripted[len(runners)]))
        runners.append(runner)
        return runner

    monkeypatch.setattr(
        "orchestrator.stage_provider.load_provider_invocation_receipt",
        lambda _path, **_kwargs: NS(provider_invocation_id="thread-7"))
    provider = StageProvider(
        runner_factory=factory, schemas=SCHEMAS, policy=NO_BUDGET_POLICY,
        system_prompt="SYS", skills=SKILLS, work_root=str(tmp_path),
        cost_ledger=ledger, bundle_operator_mode=True)
    pack = _pack("bundle")

    first = provider.bundle_operator(NS(cycle_id="c1"), pack, start_control)
    daemon.rows = [(
        11, "bundle", "bundle-c1-t7-operator-train-start-aaaaaaaaaaaa-n1-a1",
        "/provider/11", "/exec/11")]
    second = provider.bundle_operator(NS(cycle_id="c1"), pack, progress_control)

    assert first["bundle_operator_action.json"]["action"] == "start"
    assert second["bundle_operator_action.json"]["action"] == "continue"
    assert runners[0].bound == [(None, "bundle_operator")]
    assert runners[1].bound == [("thread-7", "bundle_operator")]
    assert purposes == [
        "bundle-c1-t7-operator-train-start-aaaaaaaaaaaa-n1",
        "bundle-c1-t7-operator-train-progress-bbbbbbbbbbbb-n2",
    ]
    assert start_control["subject_hash"] in runners[0].packs[0].anchor_md
    assert progress_control["subject_hash"] in runners[1].packs[0].anchor_md


def test_plan_main_resumes_one_stage_thread_across_provider_turns(tmp_path, monkeypatch):
    class Daemon:
        rows = []

        def query(self, _sql, _params):
            return list(self.rows)

    class ResidentRunner(MockRunner):
        def __init__(self):
            super().__init__([{"plan.json": _PLAN}])
            self.bound = []

        def bind_persistent_session(self, *, session_id, role="narrator"):
            self.bound.append((session_id, role))

    daemon = Daemon()
    ledger = _RecordingLedger()
    ledger.daemon = daemon
    runners = []
    purposes = []

    @_resident_factory
    def factory(_transcripts, purpose):
        purposes.append(purpose)
        runner = _resident_runner(ResidentRunner())
        runners.append(runner)
        return runner

    monkeypatch.setattr(
        "orchestrator.stage_provider.load_provider_invocation_receipt",
        lambda _path, **_kwargs: NS(provider_invocation_id="thread-plan-1"))
    provider = StageProvider(
        runner_factory=factory, schemas=SCHEMAS, policy=NO_BUDGET_POLICY,
        system_prompt="SYS", skills=SKILLS, work_root=str(tmp_path),
        cost_ledger=ledger, resident_stage_sessions=True)

    provider.plan(NS(cycle_id="c1"), _pack("plan"))
    daemon.rows = [(
        11, "plan", "plan-main-c1-n1-a1", "/provider/11", "/exec/11")]
    provider.plan(NS(cycle_id="c1"), _pack("plan"))

    assert purposes == ["plan-main-c1-n1", "plan-main-c1-n2"]
    assert runners[0].bound == [(None, "stage_main")]
    assert runners[1].bound == [("thread-plan-1", "stage_main")]


def test_inline_review_skill_uses_owner_input_protocol_and_fresh_children(
        tmp_path):
    provider = StageProvider(
        runner_factory=lambda _td, _pt: MockRunner([]),
        schemas=SCHEMAS, policy=NO_BUDGET_POLICY,
        system_prompt="SYS", skills=SKILLS, work_root=str(tmp_path),
        inline_subagent_review=True)

    plan_skill = provider._main_stage_review_skill(
        "plan", SKILLS["plan"], "plan", 1)
    assert "prepare_review" in plan_skill
    assert 'fork_turns="none"' in plan_skill
    assert "read_review_input" in plan_skill
    assert "native-review-result-v1" in plan_skill
    assert "dispositions" in plan_skill
    assert "record_review" in plan_skill
    assert "精确 1 轮" in plan_skill

    idea_skill = provider._main_stage_review_skill(
        "idea", SKILLS["idea"], "idea", 1)
    assert "wildidea_audit" in idea_skill
    assert "generation_path=wildidea" in idea_skill
    assert "服务端内部生成的 exact draft" in idea_skill
    assert "不得启动 native child" in idea_skill
    assert "generation_path=bypass" in idea_skill
    assert "精确 1 轮" in idea_skill

    bundle_skill = provider._main_stage_review_skill(
        "bundle", SKILLS["bundle"], "bundle_code", 1)
    assert "每个 target" in bundle_skill
    assert "结果审查必须另启一个新的干净子智能体" in bundle_skill
    assert "同一个干净子智能体" not in bundle_skill


@pytest.mark.parametrize("require_provider_binding", [False, True])
def test_resident_idea_main_absorbs_internal_wildidea_audit(
        tmp_path, require_provider_binding):
    purposes = []
    ledger = _RecordingLedger()
    ledger.daemon = NS(query=lambda _sql, _params: [])
    adapter = WildIdeaAdapter(
        SYSTEM_ROOT, NO_BUDGET_POLICY,
        novelty_provider=_FakeNoveltyProvider())
    pack = _real_pack("c1")
    provider_holder = {}

    draft_candidates = []
    audit_scores = []
    for index, candidate_id in enumerate(("c0", "c1", "c2")):
        candidate = json.loads(json.dumps(
            _IDEA["candidates"][0], ensure_ascii=False))
        candidate["candidate_id"] = candidate_id
        candidate["audit_mapping"]["source_domain"] += f"-{candidate_id}"
        candidate["novelty_queries"] = [
            f"cross subject EEG mechanism {candidate_id}"]
        draft_candidates.append(candidate)
        score = json.loads(json.dumps(
            _IDEA["audit_scores"][0], ensure_ascii=False))
        score["candidate_id"] = candidate_id
        audit_scores.append(score)
    draft = {
        "need_innovation": True,
        "candidates": draft_candidates,
        "novelty_refs": [],
    }

    class MainRunner:
        stage_main_session_contract = STAGE_MAIN_SESSION_CONTRACT

        def __init__(self):
            self.bound = []

        def bind_persistent_session(self, *, session_id, role):
            self.bound.append((session_id, role))

        def run_task(self, *, system_prompt, skill, context_pack):
            del system_prompt, skill
            scope = NS(
                cycle_id="c1", stage="idea", target_id=None,
                purpose="idea-main-c1-n1-a1",
                pack_hash=context_pack.pack_hash, runner_call_id=1)
            expanded = provider_holder["provider"].prepare_resident_wildidea(
                scope, need_innovation=True)
            assert expanded["generation_path"] == "wildidea"
            assert expanded["draft"] == draft
            tampered = json.loads(json.dumps(
                expanded["draft"], ensure_ascii=False))
            tampered["candidates"][0]["core_claim"] += " caller mutation"
            with pytest.raises(RuntimeError, match="服务端生成的 exact draft"):
                provider_holder["provider"].audit_resident_wildidea(
                    scope, draft=tampered)
            merged = provider_holder["provider"].audit_resident_wildidea(
                scope, draft=expanded["draft"])
            return Artifact(
                stage="idea",
                files={"idea_set.json": merged["idea_set"]}, md="")

    main_runner = MainRunner()
    generation_runner = MockRunner([{
        "idea_set.draft.json": draft,
    }])
    audit_runner = MockRunner([{
        "idea_audit.json": {
            "audit_scores": audit_scores,
            "selected_id": "c2",
        },
    }])

    @_resident_factory
    def factory(_transcripts, purpose):
        purposes.append(purpose)
        if purpose.startswith("idea-main-"):
            return main_runner
        if purpose.startswith("idea-generate-internal-"):
            return generation_runner
        assert purpose.startswith("idea-audit-internal-")
        return audit_runner

    provider = StageProvider(
        runner_factory=factory, schemas=SCHEMAS,
        policy=NO_BUDGET_POLICY, system_prompt="SYS", skills=SKILLS,
        work_root=str(tmp_path), cost_ledger=ledger,
        resident_stage_sessions=True, inline_subagent_review=True,
        wildidea_adapter=adapter, idea_audit_pack_builder=_audit_source,
        require_wildidea_provider_binding=require_provider_binding)
    provider_holder["provider"] = provider

    if require_provider_binding:
        with pytest.raises(
                RuntimeError, match="accepted provider binding"):
            provider.idea(NS(cycle_id="c1"), pack)
        assert purposes == [
            "idea-main-c1-n1",
            "idea-generate-internal-n2",
            "idea-audit-internal-n3",
        ]
        return

    result = provider.idea(NS(cycle_id="c1"), pack)["idea_set.json"]

    assert result["need_innovation"] is True
    assert {row["candidate_id"] for row in result["audit_scores"]} == {
        "c0", "c1", "c2"}
    assert "provenance" not in result
    assert purposes == [
        "idea-main-c1-n1",
        "idea-generate-internal-n2",
        "idea-audit-internal-n3",
    ]
    assert "need_innovation=true" in generation_runner.skills_seen[0]
    assert "不得调用 wildidea_expand" in generation_runner.skills_seen[0]
    assert main_runner.bound == [(None, "stage_main")]
    assert not hasattr(generation_runner, "bound")
    assert not hasattr(audit_runner, "bound")


def test_resident_plan_schema_reject_never_starts_outer_retry(tmp_path):
    class EmptyDaemon:
        def query(self, _sql, _params):
            return []

    class ResidentRunner(MockRunner):
        def __init__(self):
            super().__init__([
                {"plan.json": {"bad": True}},
                {"plan.json": _PLAN},
            ])
            self.bound = []

        def bind_persistent_session(self, *, session_id, role="narrator"):
            self.bound.append((session_id, role))

    runner = _resident_runner(ResidentRunner())
    ledger = _RecordingLedger()
    ledger.daemon = EmptyDaemon()

    @_resident_factory
    def factory(_transcripts, _purpose):
        return runner

    provider = StageProvider(
        runner_factory=factory, schemas=SCHEMAS, policy=NO_BUDGET_POLICY,
        system_prompt="SYS", skills=SKILLS, work_root=str(tmp_path),
        cost_ledger=ledger, resident_stage_sessions=True)

    with pytest.raises(RunnerError, match="不执行外层 artifact 重试"):
        provider.plan(NS(cycle_id="c1"), _pack("plan"))
    assert runner.bound == [(None, "stage_main")]
    assert len(runner.skills_seen) == 1
    assert len(runner.scripted) == 1
    assert [item["status"] for item in ledger.finished] == ["failed"]


def test_historical_empty_receipt_does_not_poison_later_unique_session(
        tmp_path, monkeypatch):
    class Daemon:
        def query(self, _sql, _params):
            return [
                (11, "plan", "plan-main-c1-n1-a1", "/provider/empty", "/exec/11"),
                (12, "plan", "plan-main-c1-n2-a1", "/provider/good", "/exec/12"),
            ]

    ledger = _RecordingLedger()
    ledger.daemon = Daemon()
    factory = _resident_factory(lambda _td, _pt: None)
    provider = StageProvider(
        runner_factory=factory, schemas=SCHEMAS, policy=NO_BUDGET_POLICY,
        system_prompt="SYS", skills=SKILLS, work_root=str(tmp_path),
        cost_ledger=ledger, resident_stage_sessions=True)
    monkeypatch.setattr(
        "orchestrator.stage_provider.load_provider_invocation_receipt",
        lambda path, **_kwargs: NS(
            provider_invocation_id=(
                None if str(path).endswith("empty") else "thread-plan")))

    assert provider._stage_main_session_id(
        NS(cycle_id="c1"), "plan") == "thread-plan"


def test_all_empty_receipts_block_fresh_resident_session(tmp_path, monkeypatch):
    class Daemon:
        def query(self, _sql, _params):
            return [
                (11, "plan", "plan-main-c1-n1-a1", "/provider/empty", "/exec/11"),
            ]

    ledger = _RecordingLedger()
    ledger.daemon = Daemon()
    factory = _resident_factory(lambda _td, _pt: None)
    provider = StageProvider(
        runner_factory=factory, schemas=SCHEMAS, policy=NO_BUDGET_POLICY,
        system_prompt="SYS", skills=SKILLS, work_root=str(tmp_path),
        cost_ledger=ledger, resident_stage_sessions=True)
    monkeypatch.setattr(
        "orchestrator.stage_provider.load_provider_invocation_receipt",
        lambda _path, **_kwargs: NS(provider_invocation_id=None))

    with pytest.raises(RuntimeError, match="拒绝新建会话"):
        provider._stage_main_session_id(NS(cycle_id="c1"), "plan")


def test_terminal_nonzero_empty_receipt_allows_fresh_resident_session(
        tmp_path, monkeypatch):
    class Daemon:
        def query(self, _sql, _params):
            return [
                (11, "plan", "plan-main-c1-n1-a1", "/provider/empty", "/exec/11"),
            ]

    ledger = _RecordingLedger()
    ledger.daemon = Daemon()
    factory = _resident_factory(lambda _td, _pt: None)
    provider = StageProvider(
        runner_factory=factory, schemas=SCHEMAS, policy=NO_BUDGET_POLICY,
        system_prompt="SYS", skills=SKILLS, work_root=str(tmp_path),
        cost_ledger=ledger, resident_stage_sessions=True)
    monkeypatch.setattr(
        "orchestrator.stage_provider.load_provider_invocation_receipt",
        lambda _path, **_kwargs: NS(
            provider_invocation_id=None,
            execution_outcome="exit",
            execution_returncode=1))

    assert provider._stage_main_session_id(NS(cycle_id="c1"), "plan") is None


def test_bundle_operator_recovers_receipts_from_frozen_decision_schema(
        tmp_path, monkeypatch):
    conn = sqlite3.connect(tmp_path / "operator.sqlite")
    conn.executescript("""
        CREATE TABLE runner_call (
          id INTEGER PRIMARY KEY, cycle_id INTEGER, phase TEXT NOT NULL,
          purpose TEXT NOT NULL, status TEXT NOT NULL,
          prompt_version TEXT, policy_version TEXT, transcript_ref TEXT,
          failure_kind TEXT, started_at TEXT, finished_at TEXT
        );
        CREATE TABLE decision (
          id INTEGER PRIMARY KEY, cycle_id INTEGER, question_id INTEGER,
          directive_id INTEGER, actor TEXT NOT NULL, type TEXT NOT NULL,
          prompt_version TEXT, policy_version TEXT,
          payload_json TEXT NOT NULL, created_at TEXT
        );
    """)
    conn.execute(
        "INSERT INTO runner_call(id,cycle_id,phase,purpose,status) "
        "VALUES (11,1,'bundle','bundle-c1-t7-n1-a1','success')")
    conn.execute(
        "INSERT INTO decision(cycle_id,actor,type,payload_json) "
        "VALUES (1,'orchestrator','provider_invocation_accounted',?)",
        (json.dumps({
            "protocol": "provider-accounting-v1",
            "runner_call_id": 11,
            "provider_receipt_ref": "/provider/11",
            "execution_receipt_ref": "/exec/11",
        }),))
    conn.commit()

    class Daemon:
        def query(self, sql, params):
            return conn.execute(sql, params).fetchall()

    ledger = _RecordingLedger()
    ledger.daemon = Daemon()
    factory = _operator_factory(lambda _td, _pt: None)
    provider = StageProvider(
        runner_factory=factory, schemas=SCHEMAS, policy=NO_BUDGET_POLICY,
        system_prompt="SYS", skills=SKILLS, work_root=str(tmp_path),
        cost_ledger=ledger, bundle_operator_mode=True)
    monkeypatch.setattr(
        "orchestrator.stage_provider.load_provider_invocation_receipt",
        lambda path, **kwargs: NS(provider_invocation_id="thread-7"))

    assert provider._bundle_operator_session_id(
        NS(cycle_id="c1"), _pack("bundle")) == "thread-7"
    assert "provider_receipt_ref" not in {
        row[1] for row in conn.execute("PRAGMA table_info(runner_call)")}
    conn.close()


def test_bundle_operator_resumes_target_session_from_durable_provider_receipts(
        tmp_path, monkeypatch):
    class Daemon:
        rows = []

        def query(self, _sql, _params):
            return list(self.rows)

    class OperatorRunner(MockRunner):
        def __init__(self):
            super().__init__([_bundle_envelope()])
            self.bound = []
            self.packs = []

        def bind_persistent_session(self, *, session_id, role="narrator"):
            self.bound.append((session_id, role))

        def run_task(self, *, system_prompt, skill, context_pack):
            self.packs.append(context_pack)
            return super().run_task(
                system_prompt=system_prompt, skill=skill,
                context_pack=context_pack)

    daemon = Daemon()
    ledger = _RecordingLedger()
    ledger.daemon = daemon
    runners = []

    @_operator_factory
    def factory(transcripts, _purpose):
        Path(transcripts).mkdir(parents=True, exist_ok=True)
        runner = _operator_runner(OperatorRunner())
        runners.append(runner)
        return runner

    monkeypatch.setattr(
        "orchestrator.stage_provider.load_provider_invocation_receipt",
        lambda path, **_kwargs: NS(
            provider_invocation_id=("thread-7" if str(path).endswith("11")
                                    else "unexpected")))
    provider = StageProvider(
        runner_factory=factory, schemas=SCHEMAS, policy=NO_BUDGET_POLICY,
        system_prompt="SYS", skills=SKILLS, work_root=str(tmp_path),
        cost_ledger=ledger, bundle_operator_mode=True)

    provider.bundle(NS(cycle_id="c1"), _pack("bundle"))
    daemon.rows = [(11, "bundle", "bundle-c1-t7-n1-a1", "/provider/11", "/exec/11")]
    repair_pack = _pack("bundle")
    repair_pack.anchor_md = (
        "上一次 bundle 实施失败: phase=train; 真实日志: CUDA out of memory")
    provider.bundle(NS(cycle_id="c1"), repair_pack)

    assert runners[0].bound == [(None, "bundle_operator")]
    assert runners[1].bound == [("thread-7", "bundle_operator")]
    assert "CUDA out of memory" in runners[1].packs[0].anchor_md


def test_bundle_operator_rejects_provider_thread_fork(tmp_path, monkeypatch):
    class Daemon:
        def query(self, _sql, _params):
            return [
                (11, "bundle", "bundle-c1-t7-n1-a1", "/provider/a", "/exec/a"),
                (12, "bundle", "bundle-c1-t8-n2-a1", "/provider/b", "/exec/b"),
            ]

    ledger = _RecordingLedger()
    ledger.daemon = Daemon()
    monkeypatch.setattr(
        "orchestrator.stage_provider.load_provider_invocation_receipt",
        lambda path, **_kwargs: NS(
            provider_invocation_id=("thread-a" if str(path).endswith("a")
                                    else "thread-b")))
    runner = _operator_runner(MockRunner([]))
    factory = _operator_factory(lambda _td, _pt: runner)
    provider = StageProvider(
        runner_factory=factory,
        schemas=SCHEMAS, policy=NO_BUDGET_POLICY, system_prompt="SYS",
        skills=SKILLS, work_root=str(tmp_path), cost_ledger=ledger,
        bundle_operator_mode=True)

    with pytest.raises(RuntimeError, match="provider session 漂移"):
        provider.bundle(NS(cycle_id="c1"), _pack("bundle"))


def test_bundle_requires_target_identity(tmp_path):
    sp, _ = _provider([_bundle_envelope()], tmp_path)
    pack = _pack("bundle")
    pack.target_id = None
    with pytest.raises(ValueError, match="target_id"):
        sp.bundle(NS(cycle_id="c1"), pack)


def test_bundle_missing_identity_retried_then_ok(tmp_path):
    bad = {k: v for k, v in _bundle_envelope().items() if k != "identity.md"}
    sp, runner = _provider([bad, _bundle_envelope()], tmp_path)
    out = sp.bundle(NS(cycle_id="c1"), _pack("bundle"))
    assert "identity.md" in out
    assert "identity.md" in runner.skills_seen[1]               # 重试反馈里点名缺的文件


def test_bundle_blank_identity_rejected(tmp_path):
    bad = {**_bundle_envelope(), "identity.md": "   "}
    sp, _ = _provider([bad] * (POLICY["flow"]["retry"]["artifact_parse"] + 1), tmp_path)
    with pytest.raises(RunnerError, match="identity.md"):
        sp.bundle(NS(cycle_id="c1"), _pack("bundle"))


def test_bundle_invalid_manifest_retried_with_feedback(tmp_path):
    bad_manifest = {**_MANIFEST, "commands": {"eval": _MANIFEST["commands"]["eval"]}}   # build 缺 train/smoke
    sp, runner = _provider([{**_bundle_envelope(), "execution_manifest.json": bad_manifest},
                            _bundle_envelope()], tmp_path)
    out = sp.bundle(NS(cycle_id="c1"), _pack("bundle"))
    assert out["execution_manifest.json"] == _MANIFEST
    assert "execution_manifest.json" in runner.skills_seen[1]   # schema 错误反馈进重试 skill


# ============ plan answerability 独立评审装配 ============
def test_plan_review_provider_records_audit_call_and_durable_verdict(tmp_path):
    daemon, _bt_id, work = _judge_env(tmp_path)
    runner = MockRunner([{
        "plan_review.json": {"verdict": "pass", "round_no": 1, "issues": []}}])
    provider = PlanReviewProvider(
        runner_factory=lambda _td, _purpose: runner, schemas=SCHEMAS,
        policy=NO_BUDGET_POLICY, system_prompt="SYS", skill="[skill:plan]",
        daemon=daemon, work_root=str(work))

    review, decision_id = provider(NS(cycle_id="c1"), _PLAN, 1, _pack("plan"))

    assert review["verdict"] == "pass"
    payload = json.loads(daemon.query_one(
        "SELECT payload_json FROM decision WHERE id=?", (decision_id,))[0])
    assert payload["round_no"] == 1 and payload["plan_hash"]
    assert daemon.query_one(
        "SELECT status,phase,purpose FROM runner_call WHERE id=?",
        (payload["runner_call_id"],)) == ("success", "audit", "plan_review")


def test_plan_review_provider_retries_bad_round_envelope(tmp_path):
    daemon, _bt_id, work = _judge_env(tmp_path)
    runner = MockRunner([
        {"plan_review.json": {"verdict": "pass", "round_no": 2, "issues": []}},
        {"plan_review.json": {"verdict": "pass", "round_no": 1, "issues": []}},
    ])
    provider = PlanReviewProvider(
        runner_factory=lambda _td, _purpose: runner, schemas=SCHEMAS,
        policy=NO_BUDGET_POLICY, system_prompt="SYS", skill="[skill:plan]",
        daemon=daemon, work_root=str(work))

    review, _decision_id = provider(NS(cycle_id="c1"), _PLAN, 1, _pack("plan"))

    assert review["round_no"] == 1
    assert "期望 1" in runner.skills_seen[1]
    assert daemon.query_one("SELECT count(*) FROM decision WHERE type='plan_review'")[0] == 1


def test_plan_review_artifact_retry_cannot_flip_first_valid_verdict(tmp_path):
    daemon, _bt_id, work = _judge_env(tmp_path)
    runner = MockRunner([
        # The semantic judgment is already explicit; only issues is malformed.
        {"plan_review.json": {"verdict": "fail", "round_no": 1}},
        {"plan_review.json": {"verdict": "pass", "round_no": 1, "issues": []}},
        {"plan_review.json": {
            "verdict": "fail", "round_no": 1,
            "issues": [{"item": "statistics", "why": "crossed resampling unspecified"}],
        }},
    ])
    provider = PlanReviewProvider(
        runner_factory=lambda _td, _purpose: runner, schemas=SCHEMAS,
        policy=NO_BUDGET_POLICY, system_prompt="SYS", skill="[skill:plan]",
        daemon=daemon, work_root=str(work))

    review, _decision_id = provider(
        NS(cycle_id="c1"), _PLAN, 1, _pack("plan"))

    assert review["verdict"] == "fail"
    assert "verdict 已冻结为 fail" in runner.skills_seen[1]
    assert "不得改变首次有效 verdict" in runner.skills_seen[2]


def test_plan_review_provider_retries_runner_artifact_parse_with_feedback(tmp_path):
    daemon, _bt_id, work = _judge_env(tmp_path)
    runner = MockRunner([
        RunnerError("信封 JSON 非法：Extra data", failure_kind="artifact_parse"),
        {"plan_review.json": {"verdict": "pass", "round_no": 1, "issues": []}},
    ])
    provider = PlanReviewProvider(
        runner_factory=lambda _td, _purpose: runner, schemas=SCHEMAS,
        policy=NO_BUDGET_POLICY, system_prompt="SYS", skill="[skill:plan]",
        daemon=daemon, work_root=str(work))

    review, _decision_id = provider(NS(cycle_id="c1"), _PLAN, 1, _pack("plan"))

    assert review["verdict"] == "pass"
    assert len(runner.skills_seen) == 2
    assert "信封 JSON 非法" in runner.skills_seen[1]


def test_plan_review_provider_rejects_pack_for_different_plan(tmp_path):
    daemon, _bt_id, work = _judge_env(tmp_path)
    runner = MockRunner([{
        "plan_review.json": {"verdict": "pass", "round_no": 1, "issues": []}}])
    provider = PlanReviewProvider(
        runner_factory=lambda _td, _purpose: runner, schemas=SCHEMAS,
        policy=NO_BUDGET_POLICY, system_prompt="SYS", skill="[skill:plan]",
        daemon=daemon, work_root=str(work))
    pack = _pack("plan")
    pack.sources = ["staging:plan-draft:" + "0" * 64]

    with pytest.raises(ValueError, match="exact plan hash"):
        provider(NS(cycle_id="c1"), _PLAN, 1, pack)
    assert runner.skills_seen == []
    assert daemon.query_one("SELECT count(*) FROM runner_call WHERE purpose='plan_review'")[0] == 0


def test_plan_review_provider_does_not_retry_runner_failure(tmp_path):
    daemon, _bt_id, work = _judge_env(tmp_path)
    runner = MockRunner([
        RunnerError("transport down", failure_kind="transport"),
        {"plan_review.json": {"verdict": "pass", "round_no": 1, "issues": []}},
    ])
    provider = PlanReviewProvider(
        runner_factory=lambda _td, _purpose: runner, schemas=SCHEMAS,
        policy=NO_BUDGET_POLICY, system_prompt="SYS", skill="[skill:plan]",
        daemon=daemon, work_root=str(work))

    with pytest.raises(RunnerError, match="transport down") as error:
        provider(NS(cycle_id="c1"), _PLAN, 1, _pack("plan"))
    assert error.value.failure_kind == "transport"
    assert len(runner.skills_seen) == 1
    assert daemon.query_one("SELECT count(*) FROM decision WHERE type='plan_review'")[0] == 0


# ============ CP8.3 · JudgeProvider（真 Codex 双评审装配）============
def _judge_env(tmp_path):
    """真 SQLite（goal/cycle/question/baseline/variant/build_target[plan_ref=切片]）+ staging 物化材料。"""
    from orchestrator.writedaemon import WriteDaemon
    import conftest
    path = str(tmp_path / "j.sqlite")
    seed = db.connect(path)
    conftest.seed_minimal(seed)                                  # goal/cycle1/question1/baseline1(variant1)
    seed.execute("INSERT INTO build_target(cycle_id,question_id,target_kind,seq,status,baseline_id,variant_id,plan_ref) "
                 "VALUES (1,1,'build',3,'smoke',1,1,?)", (json.dumps({"target_key": "t1", "spec_md": "toy"}),))  # seq=3：seed_minimal 已占 1/2
    seed.commit(); seed.close()
    daemon = WriteDaemon(db.connect(path))
    bt_id = daemon.query_one("SELECT id FROM build_target WHERE seq=3")[0]
    src = tmp_path / "work" / "c1" / f"t{bt_id}" / "src"
    src.mkdir(parents=True)
    (src / "train.py").write_text("print('train')", encoding="utf-8")
    (src / "identity.md").write_text("# toy 身份", encoding="utf-8")
    smoke = tmp_path / "work" / "c1" / f"t{bt_id}" / "smoke"
    smoke.mkdir(parents=True)
    (smoke / "smoke-1.log").write_text("smoke ok", encoding="utf-8")
    return daemon, bt_id, tmp_path / "work"


def _judge(daemon, work, scripted):
    from orchestrator.stage_provider import JudgeProvider
    runner = MockRunner(scripted)
    jp = JudgeProvider(runner_factory=lambda td, pt: runner, schemas=SCHEMAS, policy=NO_BUDGET_POLICY,
                       system_prompt="SYS", skill="[skill:judge]", daemon=daemon, work_root=str(work))
    return jp, runner


def test_judge_records_runner_call_and_decision(tmp_path):
    daemon, bt_id, work = _judge_env(tmp_path)
    jp, runner = _judge(daemon, work, [{"review_verdict.json": {"verdict": "pass", "issues": []}}])
    jp("c1", bt_id, "bundle_code_review", "sh-1")
    rc = daemon.query_one("SELECT phase, purpose, status FROM runner_call ORDER BY id DESC LIMIT 1")
    assert rc == ("audit", "bundle_code_review", "success")
    payload = json.loads(daemon.query_one(
        "SELECT payload_json FROM decision WHERE actor='judge' ORDER BY id DESC LIMIT 1")[0])
    assert payload["verdict"] == "pass" and payload["subject_hash"] == "sh-1"
    assert payload["build_target_id"] == bt_id and payload["round_no"] == 1
    assert payload["runner_call_id"] is not None and len(payload["policy_hash"]) == 64
    # subject 材料真装配：物化代码 + smoke transcript 进 anchor（judge 只读材料、不碰仓库）
    # MockRunner 未存 pack；用 _subject_md 直接断言装配面
    md = jp._subject_md("c1", bt_id, "bundle_code_review")
    assert "train.py" in md and "smoke ok" in md and "toy" in md


def test_judge_fail_verdict_recorded_with_round_increment(tmp_path):
    daemon, bt_id, work = _judge_env(tmp_path)
    jp, _ = _judge(daemon, work, [
        {"review_verdict.json": {"verdict": "pass", "issues": []}},
        {"review_verdict.json": {"verdict": "fail", "issues": [{"item": "指标硬编码", "why": "eval 不读 ckpt"}]}}])
    jp("c1", bt_id, "bundle_code_review", "sh-1")
    jp("c1", bt_id, "bundle_code_review", "sh-2")               # 产物变 → 重评审 → round_no 递增
    rows = daemon.query("SELECT json_extract(payload_json,'$.round_no'), json_extract(payload_json,'$.verdict') "
                        "FROM decision WHERE actor='judge' ORDER BY id")
    assert rows == [(1, "pass"), (2, "fail")]


def test_judge_invalid_verdict_retries_then_raises(tmp_path):
    daemon, bt_id, work = _judge_env(tmp_path)
    bad = {"review_verdict.json": {"verdict": "fail", "issues": []}}     # fail 必至少一条 issue（schema）
    jp, runner = _judge(daemon, work, [bad] * (POLICY["flow"]["retry"]["artifact_parse"] + 1))
    with pytest.raises(RunnerError, match="review_verdict"):
        jp("c1", bt_id, "bundle_code_review", "sh-1")
    assert daemon.query_one("SELECT count(*) FROM decision WHERE actor='judge'")[0] == 0   # 非法裁决不落库
    assert daemon.query_one("SELECT count(*) FROM runner_call")[0] == 0
    assert "should be non-empty" in runner.skills_seen[-1]      # schema 反馈进重试


def test_judge_result_review_subject_includes_logs(tmp_path):
    """result review 材料装配：train/eval log 尾部 + checkpoint 哈希 + identity（log 仅供评审读）。"""
    daemon, bt_id, work = _judge_env(tmp_path)
    with daemon.transaction() as conn:
        rid = conn.execute("INSERT INTO run(cycle_id,variant_id,build_target_id,kind,status) "
                           "VALUES (1,1,?,'build','success')", (bt_id,)).lastrowid
        conn.execute("INSERT INTO checkpoint(variant_id,ckpt_key,path,content_hash,hash_alg,produced_by_run) "
                     "VALUES (1,'final-r1','/x/ckpt.bin','ab12','sha256',?)", (rid,))
    t_dir = work / "c1" / f"t{bt_id}"
    (t_dir / f"run{rid}").mkdir(parents=True)
    (t_dir / f"run{rid}" / "train.log").write_text("loss: 0.2", encoding="utf-8")
    (t_dir / f"eval{rid}").mkdir(parents=True)
    (t_dir / f"eval{rid}" / "eval.log").write_text("metric_value: 1@1=0.93", encoding="utf-8")
    jp, _ = _judge(daemon, work, [{"review_verdict.json": {"verdict": "pass", "issues": []}}])
    md = jp._subject_md("c1", bt_id, "bundle_result_review")
    assert "loss: 0.2" in md and "metric_value: 1@1=0.93" in md and "ab12" in md and "toy 身份" in md


def test_judge_eval_only_subject_includes_existing_checkpoint_and_attempt_log(tmp_path):
    """eval-only 无 run：结果评审仍必须看到既有 checkpoint hash 与本 target attempt log。"""
    daemon, _bt_id, work = _judge_env(tmp_path)
    with daemon.transaction() as conn:
        bt_id = conn.execute(
            "INSERT INTO build_target(cycle_id,question_id,target_kind,seq,status,variant_id,"
            "evaluation_id,eval_action,attempt_purpose,plan_ref) "
            "VALUES (1,1,'eval',4,'running',1,1,'append_attempt','repro_eval','{}')").lastrowid
        aid = conn.execute(
            "INSERT INTO evaluation_attempt(evaluation_id,cycle_id,build_target_id,attempt_no,purpose,status) "
            "VALUES (1,1,?,2,'repro_eval','running')", (bt_id,)).lastrowid
    eval_dir = work / "c1" / f"t{bt_id}" / f"eval-a{aid}"
    eval_dir.mkdir(parents=True)
    (eval_dir / "eval.log").write_text(
        "loss: 0.1\nmetric_value: 1@1=0.95\n", encoding="utf-8")
    jp, _ = _judge(daemon, work, [])
    md = jp._subject_md("c1", bt_id, "bundle_result_review")
    assert "checkpoint ck1" in md and "h" in md
    assert "metric_value: 1@1=0.95" in md and "loss: 0.1" in md


def test_judge_import_subject_uses_import_snapshot_layout(tmp_path):
    daemon, _bt_id, work = _judge_env(tmp_path)
    with daemon.transaction() as conn:
        candidate_id = conn.execute(
            "INSERT INTO external_candidate(question_id,discovered_cycle,trigger_kind,"
            "trigger_snapshot_hash,need_summary,source_kind,canonical_uri,revision,"
            "search_snapshot_json,search_snapshot_hash,rank,retrieved_at) "
            "VALUES (1,1,'sota_reference','th','need','repo','https://example.invalid/r','rev','{}','sh',0,'t')"
        ).lastrowid
        license_id = conn.execute(
            "INSERT INTO license_review(candidate_id,decision,actor,license_scope_json,decided_cycle,policy_hash) "
            "VALUES (?,'allow','auto','{\"allow_eval\":true,\"allow_publish_pool\":true}',1,'ph')",
            (candidate_id,)).lastrowid
        baseline_id = conn.execute(
            "INSERT INTO baseline(slug,canonical_key,status,provenance,license_status,born_cycle) "
            "VALUES ('ext','ext','planned','external_import','allow',1)").lastrowid
        variant_id = conn.execute(
            "INSERT INTO variant(baseline_id,variant_key,config_json,status) "
            "VALUES (?,'imported','{}','planned')", (baseline_id,)).lastrowid
        external_import_id = conn.execute(
            "INSERT INTO external_import(question_id,candidate_id,action,action_cycle,candidate_set_hash,"
            "selection_key,policy_hash,license_decision_snapshot_hash,license_review_id,baseline_id) "
            "VALUES (1,?,'selected_for_materialization',1,'csh','rank_asc','ph','lh',?,?)",
            (candidate_id, license_id, baseline_id)).lastrowid
        bt_id = conn.execute(
            "INSERT INTO build_target(cycle_id,question_id,target_kind,seq,status,baseline_id,variant_id,plan_ref) "
            "VALUES (1,1,'import',4,'smoke',?,?,?)",
            (baseline_id, variant_id, json.dumps({"frozen": True}))).lastrowid
    clone = work / f"import{external_import_id}" / "clone"
    clone.mkdir(parents=True)
    (clone / "model.py").write_text("print('imported-code')", encoding="utf-8")
    smoke = work / f"import{external_import_id}" / "smoke"
    smoke.mkdir(parents=True)
    (smoke / "smoke-1.log").write_text("import smoke ok", encoding="utf-8")

    jp, _ = _judge(daemon, work, [])
    md = jp._subject_md("c1", bt_id, "bundle_code_review")

    assert "model.py" in md and "imported-code" in md
    assert "import smoke ok" in md and '"frozen": true' in md.lower()


def test_judge_subject_bounds_large_and_binary_repository_previews(tmp_path):
    """真实仓库可含大模型/二进制；judge prompt 必须有界，不能把完整文件读入内存。"""
    daemon, bt_id, work = _judge_env(tmp_path)
    src = work / "c1" / f"t{bt_id}" / "src"
    large = b"HEAD-MARKER\n" + (b"a" * 40_000) + b"MIDDLE-SECRET" + (
        b"z" * 40_000) + b"\nTAIL-MARKER"
    (src / "large.txt").write_bytes(large)
    (src / "weights.bin").write_bytes(b"BINARY-HEAD\x00UNSAFE-PAYLOAD")

    jp, _ = _judge(daemon, work, [])
    md = jp._subject_md("c1", bt_id, "bundle_code_review")

    assert "files=4" in md and f"total_bytes={sum(p.stat().st_size for p in src.iterdir())}" in md
    assert "HEAD-MARKER" in md and "TAIL-MARKER" in md
    assert "MIDDLE-SECRET" not in md
    assert "binary 预览省略" in md and "UNSAFE-PAYLOAD" not in md
    assert "未载入评审 prompt" in md
    assert len(md.encode("utf-8")) < 60_000


def test_judge_subject_prioritizes_entrypoint_and_caps_total_repository_preview(tmp_path):
    """大量源码不能挤掉真实命令入口，全部 preview 也必须受一个总 prompt 预算约束。"""
    daemon, bt_id, work = _judge_env(tmp_path)
    plan_ref = {
        "materialization_contract": {
            "smoke_cmd": ["python", "{repo}/zz_entry.py"],
            "eval_cmd": ["python", "{repo}/zz_entry.py", "{artifact}"],
            "artifact_relpath": "weights.bin",
        },
    }
    with daemon.transaction() as conn:
        conn.execute(
            "UPDATE build_target SET plan_ref=? WHERE id=?",
            (json.dumps(plan_ref, sort_keys=True), bt_id))
    src = work / "c1" / f"t{bt_id}" / "src"
    (src / "zz_entry.py").write_text(
        "ENTRYPOINT-MARKER = True\n", encoding="utf-8")
    (src / "weights.bin").write_bytes(b"model\x00payload")
    for index in range(70):
        (src / f"aa_module_{index:02d}.py").write_text(
            f"# module {index}\n" + ("x = 1\n" * 5_000), encoding="utf-8")

    jp, _ = _judge(daemon, work, [])
    md = jp._subject_md("c1", bt_id, "bundle_code_review")

    assert "ENTRYPOINT-MARKER" in md
    assert "预览截断声明" in md and "未冒充已做语义评审" in md
    assert "files=74" in md
    assert len(md.encode("utf-8")) < 230_000


def test_judge_subject_rejects_unlisted_symlink_material(tmp_path):
    daemon, bt_id, work = _judge_env(tmp_path)
    src = work / "c1" / f"t{bt_id}" / "src"
    (src / "alias.py").symlink_to(src / "train.py")
    jp, _ = _judge(daemon, work, [])

    with pytest.raises(RuntimeError, match="非常规文件"):
        jp._subject_md("c1", bt_id, "bundle_code_review")


def test_judge_unknown_kind_fails_loud(tmp_path):
    """codex SHOULD 回归：拼错的 review_kind 当场拒（否则写任意 decision.type，下游永远看不到期望评审）。"""
    daemon, bt_id, work = _judge_env(tmp_path)
    jp, _ = _judge(daemon, work, [])
    with pytest.raises(ValueError, match="review_kind"):
        jp("c1", bt_id, "bundle_code_reviw", "sh-1")            # typo kind
    assert daemon.query_one("SELECT count(*) FROM runner_call")[0] == 0


def test_smoke_latest_is_numeric_order(tmp_path):
    """codex SHOULD 回归：smoke-10.log 数值序 > smoke-2.log（字典序会取错「最新」）。
    attack subject 构造与 judge 材料装配共用 harness.latest_smoke_log 同一口径。"""
    from orchestrator.harness import latest_smoke_log
    daemon, bt_id, work = _judge_env(tmp_path)
    smoke = work / "c1" / f"t{bt_id}" / "smoke"
    (smoke / "smoke-2.log").write_text("OLD-2", encoding="utf-8")
    (smoke / "smoke-10.log").write_text("NEW-10", encoding="utf-8")
    assert latest_smoke_log(smoke).name == "smoke-10.log"
    jp, _ = _judge(daemon, work, [])
    assert "NEW-10" in jp._subject_md("c1", bt_id, "bundle_code_review")


def test_judge_result_review_includes_code_and_full_metrics(tmp_path):
    """codex BLOCKER 回归：result review 材料须含**代码**（判据「据结果反查代码」）与 metric_value 行
    **全量**（不受 log tail 截断）。"""
    daemon, bt_id, work = _judge_env(tmp_path)
    with daemon.transaction() as conn:
        rid = conn.execute("INSERT INTO run(cycle_id,variant_id,build_target_id,kind,status) "
                           "VALUES (1,1,?,'build','success')", (bt_id,)).lastrowid
    t_dir = work / "c1" / f"t{bt_id}"
    (t_dir / f"eval{rid}").mkdir(parents=True)
    big_log = ("filler\n" * 2000) + "metric_value: 1@1=0.93\n" + ("post\n" * 600)   # metric 行不在尾部 2000 字符内
    (t_dir / f"eval{rid}" / "eval.log").write_text(big_log, encoding="utf-8")
    jp, _ = _judge(daemon, work, [])
    md = jp._subject_md("c1", bt_id, "bundle_result_review")
    assert "print('train')" in md                               # 代码在场（反查代码）
    assert "metric_value: 1@1=0.93" in md                       # metric 行全量显式列出，未被 tail 截掉


# ============ CP8.5 · sidecar→file_request 桥 ============
_SIDECAR = {"summary_md": "需要 EEG 数据集", "items": [{
    "kind": "dataset", "desc": "EEG 原始数据", "expected_files": ["eeg.zip"],
    "attempted_paths": ["/data/eeg"], "failure_reason": "无读取权限", "dest_hint": "input/user_provided/"}]}


def test_sidecar_bridged_to_file_request(tmp_path):
    """已接桥：sidecar → 桥落请求单 → StageBlockedOnResources（信封其余产物弃用——工人自述缺文件）。"""
    from orchestrator.interfaces import StageBlockedOnResources
    seen = {}

    def bridge(stage, request, cyc):
        seen.update(stage=stage, request=request, cyc=cyc)
        return 42
    runner = MockRunner([{"selection.json": _GOOD_SELECTION, "resource_request.json": _SIDECAR}])
    sp = StageProvider(runner_factory=lambda td, pt: runner, schemas=SCHEMAS, policy=NO_BUDGET_POLICY,
                       system_prompt="S", skills=SKILLS, work_root=str(tmp_path), file_request_bridge=bridge)
    cyc = NS(cycle_id="c1", question_id="q1")
    with pytest.raises(StageBlockedOnResources) as ei:
        sp.reasoning(cyc, _pack("reasoning"))
    assert ei.value.request_id == 42 and ei.value.stage == "reasoning"
    assert seen["stage"] == "reasoning" and seen["request"] == _SIDECAR and seen["cyc"] is cyc


def test_bundle_material_request_becomes_internal_replan_but_permission_stays_user_actionable(
        tmp_path):
    from orchestrator.interfaces import BundleReplanRequired, StageBlockedOnResources
    bridged = []

    def bridge(stage, request, cyc):
        bridged.append((stage, request, cyc))
        return 9

    material_runner = MockRunner([{"resource_request.json": _SIDECAR}])
    material = StageProvider(
        runner_factory=lambda _td, _pt: material_runner, schemas=SCHEMAS,
        policy=NO_BUDGET_POLICY, system_prompt="S", skills=SKILLS,
        work_root=str(tmp_path / "material"), file_request_bridge=bridge)
    cyc = NS(cycle_id="c1", question_id="q1")
    with pytest.raises(BundleReplanRequired):
        material.bundle(cyc, _pack("bundle"))
    assert bridged == []

    permission = {"summary_md": "需要用户授权读取既知目录", "items": [{
        "kind": "permission", "desc": "只读访问已登记目录"}]}
    permission_runner = MockRunner([{"resource_request.json": permission}])
    permission_provider = StageProvider(
        runner_factory=lambda _td, _pt: permission_runner, schemas=SCHEMAS,
        policy=NO_BUDGET_POLICY, system_prompt="S", skills=SKILLS,
        work_root=str(tmp_path / "permission"), file_request_bridge=bridge)
    with pytest.raises(StageBlockedOnResources) as blocked:
        permission_provider.bundle(cyc, _pack("bundle"))
    assert blocked.value.request_id == 9 and bridged[-1][1] == permission


def test_bundle_replan_survives_replay_outbox_failure(tmp_path):
    """Replay integration is repairable; a frozen-plan outcome still reaches Reasoning."""
    from orchestrator.interfaces import BundleReplanRequired

    class FailingReplay:
        def persist_context_pack(self, *_args, **_kwargs):
            return None

        def persist_stage_artifact(self, *_args, **_kwargs):
            raise RuntimeError("injected replay outbox failure")

    runner = MockRunner([{"resource_request.json": _SIDECAR}])
    provider = StageProvider(
        runner_factory=lambda _td, _pt: runner, schemas=SCHEMAS,
        policy=NO_BUDGET_POLICY, system_prompt="S", skills=SKILLS,
        work_root=str(tmp_path), replay_archive=FailingReplay())

    with pytest.raises(BundleReplanRequired, match="需要 EEG 数据集"):
        provider.bundle(NS(cycle_id="c1"), _pack("bundle"))


def test_sidecar_bridge_reject_feeds_retry(tmp_path):
    """桥拒（sidecar 非法/quota 尽）→ 计入重试反馈（工人可修正或放弃 sidecar），有界后 fail loud。"""
    from orchestrator.notify import FileRequestReject

    def bridge(stage, request, cyc):
        raise FileRequestReject("quota 已达上限")   # 只有业务拒进重试；其余异常 fail loud（内审 NIT）
    runner = MockRunner([{"selection.json": _GOOD_SELECTION, "resource_request.json": _SIDECAR},
                         {"selection.json": _GOOD_SELECTION}])                    # 第 2 次放弃 sidecar
    sp = StageProvider(runner_factory=lambda td, pt: runner, schemas=SCHEMAS, policy=NO_BUDGET_POLICY,
                       system_prompt="S", skills=SKILLS, work_root=str(tmp_path), file_request_bridge=bridge)
    out = sp.reasoning(NS(cycle_id="c1", question_id=None), _pack("reasoning"))
    assert out == {"selection.json": _GOOD_SELECTION}
    assert "sidecar 被拒" in runner.skills_seen[1] and "quota" in runner.skills_seen[1]


def test_judge_rejects_sidecar_with_feedback(tmp_path):
    """判官不受理 sidecar（评审材料已全量给出）——反馈重试，不静默丢弃。"""
    daemon, bt_id, work = _judge_env(tmp_path)
    jp, runner = _judge(daemon, work, [
        {"review_verdict.json": {"verdict": "pass", "issues": []}, "resource_request.json": _SIDECAR},
        {"review_verdict.json": {"verdict": "pass", "issues": []}}])
    jp("c1", bt_id, "bundle_code_review", "sh-1")
    assert "不受理 resource_request" in runner.skills_seen[1]
    assert daemon.query_one("SELECT count(*) FROM decision WHERE actor='judge'")[0] == 1


def test_sidecar_bridge_nonbusiness_error_fails_loud(tmp_path):
    """codex NIT 回归（关键异常边界钉牢）：桥抛**非** FileRequestReject（如 DB 损坏）→ 原样 fail loud，
    不进 artifact_parse 重试（重试会把损坏掩成「工人产物问题」）。"""
    import sqlite3 as _sqlite3

    def bridge(stage, request, cyc):
        raise _sqlite3.OperationalError("database disk image is malformed")
    runner = MockRunner([{"selection.json": _GOOD_SELECTION, "resource_request.json": _SIDECAR},
                         {"selection.json": _GOOD_SELECTION}])   # 若误重试会吃到第 2 项
    sp = StageProvider(runner_factory=lambda td, pt: runner, schemas=SCHEMAS, policy=NO_BUDGET_POLICY,
                       system_prompt="S", skills=SKILLS, work_root=str(tmp_path), file_request_bridge=bridge)
    with pytest.raises(_sqlite3.OperationalError, match="malformed"):
        sp.reasoning(NS(cycle_id="c1", question_id=None), _pack("reasoning"))
    assert len(runner.skills_seen) == 1                          # 未重试（原样上抛）
