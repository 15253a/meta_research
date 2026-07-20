"""CP11.4a.3: durable human/SOTA/stuck trigger authorities."""
from __future__ import annotations

import json
import copy
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
import yaml

import orchestrator.import_triggers as import_triggers_module
from orchestrator import database as db
from orchestrator.console import Console, directive_action_text
from orchestrator.compiler_sqlite import SqliteCompiler
from orchestrator.cost_ledger import CostLedger
from orchestrator.import_authority import load_question_import_authority
from orchestrator.import_search import (
    ImportSearchError, ImportSearchProviderError, ImportSearchService)
from orchestrator.import_triggers import (
    BoundedReferenceSnapshotProvider, TrustedImportTriggerService)
from orchestrator.interfaces import Selection
from orchestrator.question_progress import INCONCLUSIVE_PROTOCOL
from orchestrator.statestore_sqlite import SQLiteStateStore
from orchestrator.writedaemon import WriteDaemon


SYSTEM_ROOT = Path(__file__).resolve().parent.parent
POLICY = yaml.safe_load(
    (SYSTEM_ROOT / "policies" / "policy.yaml").read_text(encoding="utf-8"))


def _candidate(*, repo="example/frozen", revision="a" * 40, spdx="MIT"):
    return {
        "provider_result_id": "101",
        "canonical_uri": f"https://github.com/{repo}",
        "revision": revision,
        "repository": {
            "full_name": repo, "default_branch": "main", "stars": 42,
            "updated_at": "2026-07-01T00:00:00Z",
        },
        "license": {
            "spdx_id": spdx, "lookup_status": "found",
            "evidence_ref": (
                f"https://api.github.com/repos/{repo}/contents/LICENSE?ref={revision}"),
            "content_sha256": "sha256:" + "b" * 64,
        },
    }


def _result(query, *, candidates=None):
    return {
        "provider": "github_rest_v1", "query": query,
        "retrieved_at": "2026-07-11T00:00:00+00:00",
        "candidates": list(candidates or []), "skipped": [],
    }


class RepoProvider:
    name = "github_rest_v1"

    def __init__(self, searches=None, resolves=None):
        self.searches = list(searches or [])
        self.resolves = list(resolves or [])
        self.search_calls = []
        self.resolve_calls = []

    def search(self, *, query, max_candidates):
        self.search_calls.append((query, max_candidates))
        value = self.searches.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def resolve_repository(self, *, canonical_uri, requested_revision, query):
        self.resolve_calls.append((canonical_uri, requested_revision, query))
        value = self.resolves.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class ReferenceProvider:
    name = "bounded_https_v1"

    def __init__(self, content=b"frozen paper bytes"):
        self.content = content
        self.calls = []

    @staticmethod
    def _validate_uri(uri):
        if not isinstance(uri, str) or not uri.startswith("https://arxiv.org/"):
            raise ImportSearchError("test reference URI 越出 allowlist")
        return uri

    def fetch(self, reference):
        self.calls.append(dict(reference))
        uri = self._validate_uri(reference["uri"])
        import hashlib
        return {
            "metadata": {
                "provider": self.name, "kind": reference["kind"],
                "requested_uri": uri, "final_uri": uri,
                "retrieved_at": "2026-07-11T00:00:00+00:00",
                "content_type": "application/pdf",
                "content_sha256": "sha256:" + hashlib.sha256(
                    self.content).hexdigest(),
                "bytes": len(self.content),
            },
            "content": self.content,
        }


def _base(tmp_path, *, visit_count=0, consecutive_inconclusive=0):
    assert 0 <= consecutive_inconclusive <= visit_count
    daemon = WriteDaemon(db.connect(str(tmp_path / "research.sqlite")))
    state = SQLiteStateStore(daemon, POLICY)
    state.create_goal(text="比较外部 baseline", predicate_json={})
    with daemon.transaction() as conn:
        conn.execute(
            "INSERT INTO cycle(id,goal_id,goal_ver,status,route,policy_version,active_question_id) "
            "VALUES (1,1,1,'idea','attack','test',NULL)")
        conn.execute(
            "INSERT INTO question(id,goal_id,goal_ver,born_goal_ver,text,status,score,est_cost,"
            "visit_count,source,born_cycle,active_cycle) "
            "VALUES (1,1,1,1,'原问题','active',0.2,0.2,?,'agent',1,1)",
            (visit_count,))
        conn.execute("UPDATE cycle SET active_question_id=1 WHERE id=1")
        conn.execute(
            "INSERT INTO idea(question_id,cycle_id,content_md,audit_json,status) "
            "VALUES (1,1,'# selected\n外部 comparator','{}','selected')")
        first_visit = visit_count - consecutive_inconclusive
        for offset in range(consecutive_inconclusive):
            history_cycle = 100 + offset
            conn.execute(
                "INSERT INTO cycle(id,goal_id,goal_ver,status,route,policy_version,finished_at) "
                "VALUES (?,1,1,'done','attack','test',CURRENT_TIMESTAMP)",
                (history_cycle,))
            payload = {
                "protocol": INCONCLUSIVE_PROTOCOL,
                "question_id": 1,
                "cycle_id": history_cycle,
                "goal_id": 1,
                "goal_ver": 1,
                "visit_count_after": first_visit + offset + 1,
                "consecutive_inconclusive": offset + 1,
            }
            conn.execute(
                "INSERT INTO decision(cycle_id,question_id,actor,type,payload_json) "
                "VALUES (?,1,'orchestrator','question_inconclusive',?)",
                (history_cycle, json.dumps(
                    payload, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"))))
    return daemon, state, NS(cycle_id="c1", question_id="q1")


def _stuck_counts():
    thresholds = POLICY["retrieval"]["gate2_stuck_threshold"]
    return int(thresholds["visit_count"]), int(
        thresholds["consecutive_inconclusive"])


def _service(tmp_path, daemon, repo, reference):
    return TrustedImportTriggerService(
        daemon=daemon, policy=POLICY, repo_provider=repo,
        reference_provider=reference, work_root=str(tmp_path),
        cost_ledger=CostLedger(daemon, POLICY))


def _confirm(console, daemon, result):
    did = result["directive_id"]
    source = daemon.query_one(
        "SELECT m.goal_id,m.goal_ver,m.connector,m.conversation_id "
        "FROM directive d JOIN interaction_message m "
        "ON m.id=d.source_interaction_message_id WHERE d.id=?", (did,))
    mid = console.ingest.inbound(
        connector=source[2], raw_text=directive_action_text("confirm", did),
        idempotency_key=f"confirm-{did}", goal_id=source[0], goal_ver=source[1],
        conversation_id=source[3])
    with daemon.transaction() as conn:
        conn.execute(
            "INSERT INTO interaction_classification(message_id,intent,directive_id) "
            "VALUES (?,'unclear',NULL)", (mid,))
    console.confirm_directive(directive_id=did, confirm_message_id=mid)


def _human_plan_env(tmp_path):
    daemon = WriteDaemon(db.connect(str(tmp_path / "research.sqlite")))
    state = SQLiteStateStore(daemon, POLICY)
    state.create_goal(text="人类点名外部实现", predicate_json={})
    with daemon.transaction() as conn:
        conn.execute(
            "INSERT INTO cycle(id,goal_id,goal_ver,status,route,policy_version) "
            "VALUES (1,1,1,'reasoning','bootstrap','test')")
    console = Console(daemon, policy=POLICY)
    raw = (
        '注入问题 {"question_text":"复现点名 repo",'
        '"human_named_repo":{"canonical_uri":"https://github.com/owner/repo",'
        '"requested_revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},'
        '"need_summary":"人类明确点名该仓库作为 comparator"}')
    inbound = console.handle_inbound(
        connector="qq", raw_text=raw, idempotency_key="human-named",
        goal_id=1, goal_ver=1)
    _confirm(console, daemon, inbound)
    effect = console.consume_directive(
        directive_id=inbound["directive_id"], cycle_id="c1")
    return daemon, inbound, effect


def test_human_named_directive_is_a_reasoning_request_not_direct_question_authority(tmp_path):
    daemon, inbound, effect = _human_plan_env(tmp_path)
    assert daemon.query_one("SELECT count(*) FROM question") == (0,)
    assert "question_id" not in effect and "source_authority_hash" not in effect
    request = effect["reasoning_question_request"]
    assert request["suggested_kind"] == "import_reference"
    assert request["human_named_repo"] == {
        "canonical_uri": "https://github.com/owner/repo",
        "requested_revision": "a" * 40,
    }
    assert daemon.query_one(
        "SELECT question_id,actor,type FROM decision WHERE id=("
        "SELECT consumed_decision_id FROM directive WHERE id=?)",
        (inbound["directive_id"],)) == (
            None, "human", "directive_inject_question")

    compiler = SqliteCompiler(
        db.connect(str(tmp_path / "research.sqlite")), POLICY)
    pack = compiler.render(cycle_id="c1", stage="reasoning")
    assert '"protocol":"directive-question-request-v1"' in pack.anchor_md
    assert '"suggested_kind":"import_reference"' in pack.anchor_md
    assert "https://github.com/owner/repo" in pack.anchor_md


def test_human_named_request_has_no_authority_until_reasoning_admits_a_question(tmp_path):
    daemon, _inbound, _effect = _human_plan_env(tmp_path)
    assert load_question_import_authority(daemon.conn, question_id=1) is None
    assert daemon.query_one(
        "SELECT count(*) FROM decision WHERE type='import_reference_authority'") == (0,)


def test_human_named_request_does_not_create_candidate_or_import_rows(tmp_path):
    daemon, _inbound, _effect = _human_plan_env(tmp_path)
    assert daemon.query_one("SELECT count(*) FROM external_candidate") == (0,)
    assert daemon.query_one("SELECT count(*) FROM external_import") == (0,)


def test_stuck_survey_persists_reasoning_request_without_direct_question_write(tmp_path):
    visit_threshold, streak_threshold = _stuck_counts()
    daemon, _state, cyc = _base(
        tmp_path, visit_count=visit_threshold,
        consecutive_inconclusive=streak_threshold)
    request = {
        "version": 1, "trigger_kind": "stuck",
        "query": "external comparator for stuck problem",
        "need_summary": "只派生独立外部参照问题",
    }
    repo = RepoProvider(searches=[
        _result(request["query"], candidates=[_candidate()])])
    service = _service(tmp_path, daemon, repo, ReferenceProvider())

    outcome = service(cyc, request)
    assert outcome["terminalized"] is False
    assert outcome["child_question_id"] is None
    assert outcome["source_authority_hash"] is None
    handoff = outcome["reasoning_question_request"]
    assert handoff["protocol"] == "import-trigger-question-request-v1"
    assert handoff["op"] == "spawn_question"
    assert handoff["kind"] == "import_reference"
    assert handoff["parent_question_id"] == "q1"
    assert handoff["trigger_kind"] == "stuck"
    assert handoff["survey_candidate_count"] == 1
    assert handoff["requires_reasoning_predicate"] is True
    assert daemon.query_one(
        "SELECT status,active_question_id,next_question_id,next_intent FROM cycle WHERE id=1") == (
            "idea", 1, None, None)
    assert daemon.query_one(
        "SELECT status,active_cycle FROM question WHERE id=1") == ("active", 1)
    # Critically, the connector has not inserted or linked another row.
    assert daemon.query_one("SELECT count(*) FROM question") == (1,)
    assert daemon.query_one("SELECT count(*) FROM question_dep") == (0,)
    assert daemon.query_one(
        "SELECT count(*) FROM external_candidate WHERE question_id=1")[0] == 0
    assert daemon.query_one(
        "SELECT count(*) FROM external_import WHERE question_id=1")[0] == 0
    compiler = SqliteCompiler(
        db.connect(str(tmp_path / "research.sqlite")), POLICY)
    plan_pack = compiler.render(cycle_id="c1", stage="plan")
    assert '"reasoning_question_request_pending":true' in plan_pack.anchor_md
    assert '"question_creation_owner":"reasoning/tree_ops"' in plan_pack.anchor_md
    reasoning_pack = compiler.render(cycle_id="c1", stage="reasoning")
    assert "待 reasoning/tree_ops 裁决的 import reference 建题请求" in reasoning_pack.anchor_md
    assert '"parent_question_id": "q1"' in reasoning_pack.anchor_md
    assert handoff["requested_text"] in reasoning_pack.anchor_md
    decision_id = daemon.query_one(
        "SELECT id FROM decision WHERE type='import_trigger_completed'")[0]
    assert f"db:decision:{decision_id}" in reasoning_pack.sources

    assert service(cyc, dict(request)) == outcome
    assert repo.search_calls == [(request["query"], 2)]


def test_stuck_requires_independent_consecutive_inconclusive_threshold(tmp_path):
    visit_threshold, _streak_threshold = _stuck_counts()
    daemon, _state, cyc = _base(
        tmp_path, visit_count=visit_threshold, consecutive_inconclusive=0)
    repo = RepoProvider()
    service = _service(tmp_path, daemon, repo, ReferenceProvider())
    with pytest.raises(ImportSearchError, match="双阈值"):
        service(cyc, {
            "version": 1, "trigger_kind": "stuck", "query": "search",
            "need_summary": "not yet stuck",
        })
    assert repo.search_calls == []


def test_stuck_zero_result_is_durable_without_child_or_repeat(tmp_path):
    visit_threshold, streak_threshold = _stuck_counts()
    daemon, _state, cyc = _base(
        tmp_path, visit_count=visit_threshold,
        consecutive_inconclusive=streak_threshold)
    request = {
        "version": 1, "trigger_kind": "stuck", "query": "nothing found",
        "need_summary": "只读普查无结果",
    }
    repo = RepoProvider(searches=[_result(request["query"])])
    service = _service(tmp_path, daemon, repo, ReferenceProvider())
    first = service(cyc, request)
    second = service(cyc, request)
    assert first == second
    assert first["terminalized"] is False and first["child_question_id"] is None
    assert repo.search_calls == [(request["query"], 2)]
    assert daemon.query_one("SELECT status,active_question_id FROM cycle") == (
        "idea", 1)


def test_sota_freezes_allowlisted_body_then_requests_reasoning_reference_question(
        tmp_path, monkeypatch):
    daemon, _state, cyc = _base(tmp_path)
    request = {
        "version": 1, "trigger_kind": "sota_reference",
        "query": "recognized sota implementation",
        "need_summary": "冻结公认 SOTA 并建立独立参照题",
        "reference": {
            "kind": "paper", "uri": "https://arxiv.org/abs/2401.00001"},
    }
    repo = RepoProvider(searches=[
        _result(request["query"], candidates=[_candidate()])])
    reference = ReferenceProvider(content=b"immutable-paper")
    service = _service(tmp_path, daemon, repo, reference)

    outcome = service(cyc, request)

    assert outcome["terminalized"] is False
    assert outcome["child_question_id"] is None
    assert daemon.query_one("SELECT count(*) FROM question") == (1,)
    assert daemon.query_one("SELECT count(*) FROM question_dep") == (0,)
    handoff = outcome["reasoning_question_request"]
    assert handoff["trigger_kind"] == "sota_reference"
    assert handoff["parent_question_id"] == "q1"
    assert handoff["survey_candidate_count"] == 1
    snapshot = outcome["reference_snapshot"]
    assert Path(snapshot["blob_ref"]).read_bytes() == b"immutable-paper"
    assert snapshot["content_sha256"].startswith("sha256:")
    digest = snapshot["content_sha256"].removeprefix("sha256:")
    assert Path(snapshot["blob_ref"]).name == f"{digest}.bin"
    assert Path(snapshot["blob_ref"]).parts[-4:-2] == (
        "source-blobs", "sha256")
    assert reference.calls == [request["reference"]]
    assert daemon.query_one(
        "SELECT count(*) FROM external_candidate WHERE question_id=1")[0] == 0
    reasoning = SqliteCompiler(
        db.connect(str(tmp_path / "research.sqlite")), POLICY).render(
            cycle_id="c1", stage="reasoning")
    assert handoff["requested_text"] in reasoning.anchor_md
    assert '"requires_reasoning_predicate": true' in reasoning.anchor_md

    bad_completion = {**outcome, "receipt_ref": "/etc/passwd"}
    monkeypatch.setattr(
        import_triggers_module, "read_receipt",
        lambda _path: (_ for _ in ()).throw(
            AssertionError("receipt path was read before validation")))
    with pytest.raises(ImportSearchError, match="receipt_ref 非规范路径"):
        service._verify_existing_completion(
            cyc=cyc, request=request, request_hash=outcome["request_hash"],
            completion=bad_completion)


def test_trigger_receipt_recovers_without_refetch_or_direct_question_write(tmp_path, monkeypatch):
    visit_threshold, streak_threshold = _stuck_counts()
    daemon, _state, cyc = _base(
        tmp_path, visit_count=visit_threshold,
        consecutive_inconclusive=streak_threshold)
    request = {
        "version": 1, "trigger_kind": "stuck", "query": "recover survey",
        "need_summary": "恢复后只建一个参照题",
    }
    repo = RepoProvider(searches=[
        _result(request["query"], candidates=[_candidate()])])
    reference = ReferenceProvider()
    service = _service(tmp_path, daemon, repo, reference)
    monkeypatch.setattr(
        service, "_after_receipt",
        lambda: (_ for _ in ()).throw(RuntimeError("crash-after-trigger-receipt")))
    with pytest.raises(RuntimeError, match="crash-after-trigger-receipt"):
        service(cyc, request)
    assert daemon.query_one("SELECT status FROM runner_call") == ("running",)
    assert daemon.query_one("SELECT count(*) FROM question") == (1,)

    recovered = _service(tmp_path, daemon, repo, reference)
    outcome = recovered(cyc, request)
    assert outcome["terminalized"] is False
    assert outcome["child_question_id"] is None
    assert outcome["reasoning_question_request"]["parent_question_id"] == "q1"
    assert repo.search_calls == [(request["query"], 2)]
    assert daemon.query_one("SELECT count(*) FROM question") == (1,)
    assert recovered(cyc, dict(request)) == outcome


def test_sota_handoff_binds_frozen_source_hash_without_creating_child(tmp_path):
    daemon, _state, cyc = _base(tmp_path)
    request = {
        "version": 1, "trigger_kind": "sota_reference",
        "query": "tamper-resistant sota", "need_summary": "冻结来源不可替换",
        "reference": {
            "kind": "paper", "uri": "https://arxiv.org/abs/2401.00001"},
    }
    repo = RepoProvider(searches=[
        _result(request["query"], candidates=[_candidate()])])
    service = _service(tmp_path, daemon, repo, ReferenceProvider(b"original"))
    outcome = service(cyc, request)
    snapshot = outcome["reference_snapshot"]
    original_hash = snapshot["content_sha256"]
    assert Path(snapshot["blob_ref"]).read_bytes() == b"original"
    stored = json.loads(daemon.query_one(
        "SELECT payload_json FROM decision WHERE type='import_trigger_completed'")[0])
    assert stored["reference_snapshot"]["content_sha256"] == original_hash
    assert stored["reasoning_question_request"] == outcome["reasoning_question_request"]
    assert outcome["child_question_id"] is None
    assert daemon.query_one("SELECT count(*) FROM question") == (1,)
    assert daemon.query_one(
        "SELECT count(*) FROM external_candidate") == (0,)


def test_new_structure_service_rejects_stuck_eligible_original(tmp_path):
    visit_threshold, streak_threshold = _stuck_counts()
    daemon, _state, cyc = _base(
        tmp_path, visit_count=visit_threshold,
        consecutive_inconclusive=streak_threshold)
    repo = RepoProvider(searches=[])
    service = ImportSearchService(
        daemon=daemon, policy=POLICY, provider=repo,
        work_root=str(tmp_path), cost_ledger=CostLedger(daemon, POLICY))
    with pytest.raises(ImportSearchError, match="不得借 new_structure"):
        service(cyc, {
            "version": 1, "trigger_kind": "new_structure",
            "query": "try direct", "need_summary": "must survey first",
        })
    assert repo.search_calls == []


def test_new_structure_is_not_blocked_by_high_visit_without_streak(tmp_path):
    visit_threshold, _streak_threshold = _stuck_counts()
    daemon, _state, cyc = _base(
        tmp_path, visit_count=visit_threshold, consecutive_inconclusive=0)
    request = {
        "version": 1, "trigger_kind": "new_structure",
        "query": "legitimate new family", "need_summary": "缺少新结构基线",
    }
    repo = RepoProvider(searches=[_result(request["query"])])
    service = ImportSearchService(
        daemon=daemon, policy=POLICY, provider=repo,
        work_root=str(tmp_path), cost_ledger=CostLedger(daemon, POLICY))
    outcome = service(cyc, request)
    assert outcome["candidate_count"] == 0
    assert repo.search_calls == [(request["query"], 2)]


def test_stuck_survey_tree_capacity_rejects_before_runner_or_network(tmp_path):
    visit_threshold, streak_threshold = _stuck_counts()
    daemon, _state, cyc = _base(
        tmp_path, visit_count=visit_threshold,
        consecutive_inconclusive=streak_threshold)
    policy = copy.deepcopy(POLICY)
    policy["tree_guard"]["max_open_questions"] = 1
    repo = RepoProvider()
    service = TrustedImportTriggerService(
        daemon=daemon, policy=policy, repo_provider=repo,
        reference_provider=ReferenceProvider(), work_root=str(tmp_path),
        cost_ledger=CostLedger(daemon, policy))
    with pytest.raises(ImportSearchError, match="max_open_questions"):
        service(cyc, {
            "version": 1, "trigger_kind": "stuck", "query": "no capacity",
            "need_summary": "不能创建参照题",
        })
    assert repo.search_calls == []
    assert daemon.query_one("SELECT count(*) FROM runner_call") == (0,)


_REQUEST_PREDICATE = {
    "kind": "evidence_closure_v1",
    "allowed_evidence": ["evaluation", "literature"],
    "answer_criterion_md": "预注册测量或冻结文献支持肯定回答。",
    "refute_criterion_md": "预注册测量或冻结文献支持否定回答。",
}


def _console_request_env(tmp_path, *, human_named=False):
    daemon = WriteDaemon(db.connect(str(tmp_path / "research.sqlite")))
    state = SQLiteStateStore(daemon, POLICY)
    state.create_goal(text="绑定 console 建题请求", predicate_json={})
    bootstrap = state.open_or_resume_cycle()
    state.set_route(bootstrap.cycle_id, "bootstrap")
    state.apply_tree_ops(bootstrap.cycle_id, [{
        "op": "create_root", "local_key": "root", "text": "原始研究问题",
        "predicate_json": _REQUEST_PREDICATE,
    }])
    state.mark_cycle_done(bootstrap.cycle_id)
    cycle = state.open_or_resume_cycle()
    state.set_route(cycle.cycle_id, "attack")
    console = Console(daemon, policy=POLICY)
    if human_named:
        raw = (
            '注入问题 {"question_text":"点名仓库在预注册协议下是否达到对照性能？",'
            '"parent_question_id":"q1",'
            '"human_named_repo":{"canonical_uri":"https://github.com/owner/repo",'
            '"requested_revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},'
            '"need_summary":"人类点名仓库的独立对照"}')
    else:
        raw = (
            '注入问题 {"question_text":"新增对照在预注册协议下是否提高性能？",'
            '"parent_question_id":"q1"}')
    inbound = console.handle_inbound(
        connector="qq", raw_text=raw,
        idempotency_key=("human-bound" if human_named else "ordinary-bound"),
        goal_id=1, goal_ver=1)
    if human_named:
        _confirm(console, daemon, inbound)
    effect = console.consume_directive(
        directive_id=inbound["directive_id"], cycle_id=cycle.cycle_id)
    return daemon, state, cycle, console, inbound, effect


def _admit_console_request(state, cycle, request):
    op = {
        "op": "spawn_question", "local_key": "requested",
        "request_ref": request["request_ref"],
        "kind": request["suggested_kind"],
        "parent_question_id": request["parent_question_id"],
        "text": request["requested_text"],
        "predicate_json": _REQUEST_PREDICATE,
    }
    with state.atomic():
        state.apply_tree_ops(cycle.cycle_id, [op])
        state.persist_selection(cycle.cycle_id, Selection(
            next_question_id="requested", next_intent="attack", scores=[]))
        state.mark_cycle_done(cycle.cycle_id)
    return state.daemon.query_one(
        "SELECT id FROM question WHERE born_cycle=?",
        (int(cycle.cycle_id[1:]),))[0]


def _activate_question_for_import(state, question_id):
    cycle = state.open_or_resume_cycle()
    state.set_route(cycle.cycle_id, "attack")
    state.activate_question(f"q{question_id}")
    with state.daemon.transaction() as conn:
        conn.execute(
            "UPDATE cycle SET status='idea' WHERE id=?",
            (int(cycle.cycle_id[1:]),))
        conn.execute(
            "INSERT INTO idea(question_id,cycle_id,content_md,audit_json,status) "
            "VALUES (?,?,'# selected\\n冻结外部参照','{}','selected')",
            (question_id, int(cycle.cycle_id[1:])))
    return NS(cycle_id=cycle.cycle_id, question_id=f"q{question_id}")


def test_console_ordinary_request_ref_is_bound_by_statestore_only(tmp_path):
    daemon, state, cycle, _console, inbound, effect = _console_request_env(
        tmp_path, human_named=False)
    request = effect["reasoning_question_request"]
    assert request["request_ref"] == f"db:directive:{inbound['directive_id']}"
    assert daemon.query_one("SELECT count(*) FROM question") == (1,)

    child_id = _admit_console_request(state, cycle, request)

    assert daemon.query_one(
        "SELECT parent_id,text,source,status FROM question WHERE id=?",
        (child_id,)) == (
            1, request["requested_text"], "human", "open")
    binding = json.loads(daemon.query_one(
        "SELECT payload_json FROM decision WHERE question_id=? "
        "AND type='question_request_bound'", (child_id,))[0])
    assert binding["request_ref"] == request["request_ref"]
    assert binding["source_kind"] == "console_directive"
    assert binding["source_authority_hash"] is None
    assert load_question_import_authority(
        daemon.conn, question_id=child_id) is None
    assert daemon.query_one(
        "SELECT count(*) FROM decision WHERE question_id=? "
        "AND type IN ('human_named_import_authority','import_reference_authority')",
        (child_id,)) == (0,)


@pytest.mark.parametrize("tamper", ["text", "parent", "kind", "provenance"])
def test_console_request_ref_rejects_non_exact_or_wrong_provenance(
        tmp_path, tamper):
    daemon, state, cycle, _console, inbound, effect = _console_request_env(
        tmp_path, human_named=False)
    request = effect["reasoning_question_request"]
    op = {
        "op": "spawn_question", "request_ref": request["request_ref"],
        "kind": request["suggested_kind"],
        "parent_question_id": request["parent_question_id"],
        "text": request["requested_text"],
        "predicate_json": _REQUEST_PREDICATE,
    }
    if tamper == "text":
        op["text"] += "（改写）"
    elif tamper == "parent":
        op["parent_question_id"] = None
    elif tamper == "kind":
        op["kind"] = "diagnosis"
    else:
        consumed_decision_id = daemon.query_one(
            "SELECT consumed_decision_id FROM directive WHERE id=?",
            (inbound["directive_id"],))[0]
        # It is a real decision from this cycle, but not an
        # import_trigger_completed authority.
        op["request_ref"] = f"db:decision:{consumed_decision_id}"
    with pytest.raises(ValueError, match="request_ref|exact"):
        state.apply_tree_ops(cycle.cycle_id, [op])
    assert daemon.query_one("SELECT count(*) FROM question") == (1,)
    assert daemon.query_one(
        "SELECT count(*) FROM decision WHERE type='question_request_bound'") == (0,)


def test_human_named_request_binding_creates_authority_then_activates(tmp_path):
    daemon, state, cycle, _console, _inbound, effect = _console_request_env(
        tmp_path, human_named=True)
    request = effect["reasoning_question_request"]
    child_id = _admit_console_request(state, cycle, request)
    authority = load_question_import_authority(
        daemon.conn, question_id=child_id)
    assert authority["trigger_kind"] == "human_named"
    assert authority["canonical_uri"] == "https://github.com/owner/repo"
    assert daemon.query_one(
        "SELECT actor,type FROM decision WHERE question_id=? "
        "AND type='human_named_import_authority'", (child_id,)) == (
            "orchestrator", "human_named_import_authority")

    action_cycle = _activate_question_for_import(state, child_id)
    query = "human_named:https://github.com/owner/repo@" + "a" * 40
    repo = RepoProvider(resolves=[
        _result(query, candidates=[_candidate(repo="owner/repo")])])
    service = _service(tmp_path, daemon, repo, ReferenceProvider())
    activation_request = {
        "version": 1, "trigger_kind": "human_named",
        "source_authority_hash": authority["authority_hash"],
        "need_summary": authority["need_summary"],
    }
    first = service(action_cycle, activation_request)
    second = service(action_cycle, dict(activation_request))
    assert first == second and first["candidate_count"] == 1
    assert repo.resolve_calls == [(
        "https://github.com/owner/repo", "a" * 40, query)]
    assert daemon.query_one(
        "SELECT question_id,trigger_kind,canonical_uri,revision "
        "FROM external_candidate") == (
            child_id, "human_named", "https://github.com/owner/repo", "a" * 40)


@pytest.mark.parametrize("trigger_kind", ["stuck", "sota_reference"])
def test_frozen_trigger_request_binding_creates_dep_authority_and_activates(
        tmp_path, trigger_kind):
    visit_threshold, streak_threshold = _stuck_counts()
    daemon, state, origin_cycle = _base(
        tmp_path, visit_count=visit_threshold,
        consecutive_inconclusive=streak_threshold)
    request = {
        "version": 1, "trigger_kind": trigger_kind,
        "query": f"frozen {trigger_kind} comparator",
        "need_summary": f"{trigger_kind} 独立冻结参照",
    }
    if trigger_kind == "sota_reference":
        request["reference"] = {
            "kind": "paper", "uri": "https://arxiv.org/abs/2401.00001"}
    repo = RepoProvider(searches=[
        _result(request["query"], candidates=[_candidate()])])
    reference = ReferenceProvider(content=b"immutable reference")
    service = _service(tmp_path, daemon, repo, reference)
    completion = service(origin_cycle, request)
    completion_id = daemon.query_one(
        "SELECT id FROM decision WHERE type='import_trigger_completed'")[0]
    handoff = completion["reasoning_question_request"]
    request_ref = f"db:decision:{completion_id}"
    reasoning_pack = SqliteCompiler(
        db.connect(str(tmp_path / "research.sqlite")), POLICY).render(
            cycle_id=origin_cycle.cycle_id, stage="reasoning")
    assert f'"request_ref": "{request_ref}"' in reasoning_pack.anchor_md

    with state.atomic():
        state.mark_inconclusive(origin_cycle.question_id)
        state.apply_tree_ops(origin_cycle.cycle_id, [{
            "op": "spawn_question", "local_key": "frozen-ref",
            "request_ref": request_ref, "kind": handoff["kind"],
            "parent_question_id": handoff["parent_question_id"],
            "text": handoff["requested_text"],
            "predicate_json": _REQUEST_PREDICATE,
        }])
        state.persist_selection(origin_cycle.cycle_id, Selection(
            next_question_id="frozen-ref", next_intent="attack", scores=[]))
        state.mark_cycle_done(origin_cycle.cycle_id)
    child_id = daemon.query_one(
        "SELECT id FROM question WHERE id<>1 ORDER BY id")[0]
    authority = load_question_import_authority(
        daemon.conn, question_id=child_id)
    assert authority["trigger_kind"] == trigger_kind
    assert authority["request_hash"] == completion["request_hash"]
    assert daemon.query_one(
        "SELECT question_id,depends_on_question_id,status,created_cycle "
        "FROM question_dep") == (1, child_id, "pending", 1)
    assert daemon.query_one(
        "SELECT request_ref FROM (SELECT json_extract(payload_json,'$.request_ref') "
        "AS request_ref FROM decision WHERE type='question_request_bound')") == (
            request_ref,)

    action_cycle = _activate_question_for_import(state, child_id)
    activation_request = {
        "version": 1, "trigger_kind": trigger_kind,
        "source_authority_hash": authority["authority_hash"],
        "need_summary": authority["need_summary"],
    }
    first = service(action_cycle, activation_request)
    second = service(action_cycle, dict(activation_request))
    assert first == second and first["candidate_count"] == 1
    assert daemon.query_one(
        "SELECT question_id,trigger_kind FROM external_candidate") == (
            child_id, trigger_kind)
    assert daemon.query_one(
        "SELECT count(*) FROM external_candidate WHERE question_id=1") == (0,)
    assert repo.search_calls == [(request["query"], 2)]
    assert len(reference.calls) == (1 if trigger_kind == "sota_reference" else 0)

    # Origin completion remains replayable after StateStore binds the child;
    # neither recovery nor activation repeats the read-only survey.
    assert service(origin_cycle, dict(request)) == completion


class _ReferenceResponse:
    def __init__(self, url, raw=b"paper"):
        self._url = url
        self._raw = raw
        self.headers = {
            "Content-Length": str(len(raw)), "Content-Type": "application/pdf"}

    def geturl(self):
        return self._url

    def read(self, limit):
        return self._raw[:limit]

    def close(self):
        pass


def test_default_reference_provider_enforces_host_allowlist_and_bounds():
    seen = []

    def opener(request, timeout):
        seen.append((request.full_url, timeout))
        return _ReferenceResponse(request.full_url, b"frozen")

    provider = BoundedReferenceSnapshotProvider(
        POLICY["import_reference"]["reference_snapshot"], opener=opener)
    result = provider.fetch({
        "kind": "paper", "uri": "https://arxiv.org/abs/2401.00001"})
    assert result["content"] == b"frozen"
    assert result["metadata"]["content_sha256"].startswith("sha256:")
    with pytest.raises(ImportSearchProviderError, match="allowlist"):
        provider.fetch({
            "kind": "benchmark", "uri": "https://127.0.0.1/private"})
    assert len(seen) == 1
