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
from orchestrator.importer import DeferredImporter
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
            "INSERT INTO cycle(id,goal_id,goal_ver,status,route,policy_version,finished_at) "
            "VALUES (1,1,1,'done','bootstrap','test',CURRENT_TIMESTAMP)")
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
    qid = int(effect["question_id"][1:])
    with daemon.transaction() as conn:
        conn.execute(
            "INSERT INTO cycle(id,goal_id,goal_ver,status,route,policy_version,active_question_id) "
            "VALUES (2,1,1,'idea','attack','test',?)", (qid,))
        conn.execute(
            "UPDATE question SET status='active',active_cycle=2,born_cycle=COALESCE(born_cycle,1) "
            "WHERE id=?", (qid,))
        conn.execute(
            "INSERT INTO idea(question_id,cycle_id,content_md,audit_json,status) "
            "VALUES (?,2,'# selected\n人类外部参照','{}','selected')", (qid,))
    return daemon, NS(cycle_id="c2", question_id=f"q{qid}"), effect


def _activate_child(daemon, *, child_id):
    with daemon.transaction() as conn:
        conn.execute(
            "INSERT INTO cycle(id,goal_id,goal_ver,status,route,policy_version,active_question_id) "
            "VALUES (2,1,1,'idea','attack','test',?)", (child_id,))
        conn.execute(
            "UPDATE question SET status='active',active_cycle=2 WHERE id=?", (child_id,))
        conn.execute(
            "INSERT INTO idea(question_id,cycle_id,content_md,audit_json,status) "
            "VALUES (?,2,'# selected\n冻结外部参照','{}','selected')", (child_id,))
    return NS(cycle_id="c2", question_id=f"q{child_id}")


def test_human_named_uses_exact_confirmed_authority_and_direct_resolve(tmp_path):
    daemon, cyc, effect = _human_plan_env(tmp_path)
    query = "human_named:https://github.com/owner/repo@" + "a" * 40
    repo = RepoProvider(resolves=[_result(query, candidates=[_candidate(repo="owner/repo")])])
    service = _service(tmp_path, daemon, repo, ReferenceProvider())
    request = {
        "version": 1, "trigger_kind": "human_named",
        "source_authority_hash": effect["source_authority_hash"],
        "need_summary": "人类明确点名该仓库作为 comparator",
    }

    compiler = SqliteCompiler(
        db.connect(str(tmp_path / "research.sqlite")), POLICY)
    before = compiler.render(cycle_id="c2", stage="plan")
    assert '"may_activate_source_authority":true' in before.anchor_md
    assert effect["source_authority_hash"] in before.anchor_md
    assert '"may_request_import_search":false' in before.anchor_md

    first = service(cyc, request)
    second = service(cyc, dict(request))

    assert first == second and first["candidate_count"] == 1
    assert repo.resolve_calls == [(
        "https://github.com/owner/repo", "a" * 40, query)]
    assert daemon.query_one(
        "SELECT trigger_kind,canonical_uri,revision FROM external_candidate") == (
            "human_named", "https://github.com/owner/repo", "a" * 40)
    assert daemon.query_one(
        "SELECT status FROM cycle WHERE id=2") == ("idea",)
    pack = compiler.render(cycle_id="c2", stage="plan")
    assert '"may_activate_source_authority":false' in pack.anchor_md
    assert '"trigger_kind":"human_named"' in pack.anchor_md


def test_human_named_rejects_mismatched_or_absent_authority(tmp_path):
    daemon, cyc, effect = _human_plan_env(tmp_path)
    repo = RepoProvider()
    service = _service(tmp_path, daemon, repo, ReferenceProvider())
    with pytest.raises(ImportSearchError, match="authority"):
        service(cyc, {
            "version": 1, "trigger_kind": "human_named",
            "source_authority_hash": "sha256:" + "f" * 64,
            "need_summary": "人类明确点名该仓库作为 comparator",
        })
    assert repo.resolve_calls == []
    assert effect["source_authority_hash"] != "sha256:" + "f" * 64


def test_human_named_question_cannot_masquerade_as_new_structure(tmp_path):
    daemon, cyc, _effect = _human_plan_env(tmp_path)
    repo = RepoProvider(searches=[])
    ordinary = ImportSearchService(
        daemon=daemon, policy=POLICY, provider=repo,
        work_root=str(tmp_path), cost_ledger=CostLedger(daemon, POLICY))
    with pytest.raises(ImportSearchError, match="不得借 new_structure"):
        ordinary(cyc, {
            "version": 1, "trigger_kind": "new_structure",
            "query": "substitute a different repo",
            "need_summary": "attempt to bypass human authority",
        })
    assert repo.search_calls == []


def test_stuck_survey_spawns_child_and_child_activation_never_imports_original(tmp_path):
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
    child_id = outcome["child_question_id"]
    assert outcome["terminalized"] is True and child_id > 1
    assert daemon.query_one(
        "SELECT status,active_question_id,next_question_id,next_intent FROM cycle WHERE id=1") == (
            "done", None, child_id, "attack")
    assert daemon.query_one(
        "SELECT status,active_cycle FROM question WHERE id=1") == ("open", None)
    assert daemon.query_one(
        "SELECT question_id,depends_on_question_id,status FROM question_dep") == (
            1, child_id, "pending")
    assert daemon.query_one(
        "SELECT count(*) FROM external_candidate WHERE question_id=1")[0] == 0
    assert daemon.query_one(
        "SELECT count(*) FROM external_import WHERE question_id=1")[0] == 0
    diagnostic = SqliteCompiler(
        db.connect(str(tmp_path / "research.sqlite")), POLICY).render(
            cycle_id="c1", stage="plan")
    assert '"terminalized":true' in diagnostic.anchor_md
    assert f'"child_question_id":{child_id}' in diagnostic.anchor_md

    authority = load_question_import_authority(daemon.conn, question_id=child_id)
    child_cyc = _activate_child(daemon, child_id=child_id)
    activated = service(child_cyc, {
        "version": 1, "trigger_kind": "stuck",
        "source_authority_hash": authority["authority_hash"],
        "need_summary": authority["need_summary"],
    })
    assert activated["candidate_count"] == 1
    assert activated["policy_hash"] == DeferredImporter.policy_hash(POLICY)
    assert repo.search_calls == [(request["query"], 2)]
    assert daemon.query_one(
        "SELECT question_id,discovered_cycle,trigger_kind FROM external_candidate") == (
            child_id, 2, "stuck")
    snapshot = DeferredImporter.plan_snapshot(
        daemon.conn, question_id=child_id, action_cycle=2,
        policy_hash=DeferredImporter.policy_hash(POLICY))
    assert snapshot["selected"] is not None
    corrupted = {**activated, "policy_hash": "sha256:" + "f" * 64}
    with pytest.raises(ImportSearchError, match="license review"):
        service._verify_activation_payload(
            cyc=child_cyc, request_hash=activated["request_hash"],
            payload=corrupted)


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


def test_sota_freezes_allowlisted_body_then_spawns_reference_question(
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

    assert outcome["terminalized"] is True
    snapshot = outcome["reference_snapshot"]
    assert Path(snapshot["blob_ref"]).read_bytes() == b"immutable-paper"
    assert snapshot["content_sha256"].startswith("sha256:")
    digest = snapshot["content_sha256"].removeprefix("sha256:")
    assert Path(snapshot["blob_ref"]).name == f"{digest}.bin"
    assert Path(snapshot["blob_ref"]).parts[-4:-2] == (
        "source-blobs", "sha256")
    assert reference.calls == [request["reference"]]
    child = daemon.query_one(
        "SELECT id,parent_id,source,born_cycle FROM question WHERE id<>1")
    assert child == (outcome["child_question_id"], 1, "agent", 1)
    assert daemon.query_one(
        "SELECT count(*) FROM external_candidate WHERE question_id=1")[0] == 0
    authority = load_question_import_authority(
        daemon.conn, question_id=outcome["child_question_id"])
    child_cyc = _activate_child(
        daemon, child_id=outcome["child_question_id"])
    activated = service(child_cyc, {
        "version": 1, "trigger_kind": "sota_reference",
        "source_authority_hash": authority["authority_hash"],
        "need_summary": authority["need_summary"],
    })
    assert activated["candidate_count"] == 1
    assert len(reference.calls) == 1 and len(repo.search_calls) == 1
    assert daemon.query_one(
        "SELECT question_id,discovered_cycle,trigger_kind FROM external_candidate") == (
            outcome["child_question_id"], 2, "sota_reference")
    bad_authority = {**authority, "receipt_ref": "/etc/passwd"}
    monkeypatch.setattr(
        import_triggers_module, "read_receipt",
        lambda _path: (_ for _ in ()).throw(
            AssertionError("receipt path was read before validation")))
    with pytest.raises(ImportSearchError, match="receipt_ref 非规范路径"):
        service._read_authority_receipt(bad_authority)


def test_trigger_receipt_recovers_without_refetch_or_duplicate_child(tmp_path, monkeypatch):
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
    assert outcome["terminalized"] is True
    assert repo.search_calls == [(request["query"], 2)]
    assert daemon.query_one("SELECT count(*) FROM question") == (2,)


def test_sota_child_activation_rejects_tampered_frozen_source_blob(tmp_path):
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
    authority = load_question_import_authority(
        daemon.conn, question_id=outcome["child_question_id"])
    Path(authority["reference_snapshot"]["blob_ref"]).write_bytes(b"tampered")
    child_cyc = _activate_child(
        daemon, child_id=outcome["child_question_id"])
    with pytest.raises(ImportSearchError, match="blob"):
        service(child_cyc, {
            "version": 1, "trigger_kind": "sota_reference",
            "source_authority_hash": authority["authority_hash"],
            "need_summary": authority["need_summary"],
        })
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
