"""CP11.4a.2: durable read-only discovery -> atomic import registration."""
from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
import yaml

from orchestrator import database as db
from orchestrator.cost_ledger import CostLedger
from orchestrator.import_search import (
    GitHubRepoSearchProvider,
    ImportSearchError,
    ImportSearchProviderError,
    ImportSearchService,
)
from orchestrator.importer import DeferredImporter
from orchestrator.process_supervisor import atomic_write_receipt, read_receipt
from orchestrator.statestore_sqlite import SQLiteStateStore
from orchestrator.writedaemon import WriteDaemon


SYSTEM_ROOT = Path(__file__).resolve().parent.parent
POLICY = yaml.safe_load(
    (SYSTEM_ROOT / "policies" / "policy.yaml").read_text(encoding="utf-8"))
REQUEST = {
    "version": 1,
    "trigger_kind": "new_structure",
    "query": "long context comparator implementation",
    "need_summary": "当前问题需要独立的外部 comparator baseline 家族",
}


def _candidate(*, spdx="MIT", revision="a" * 40, repo="example/frozen"):
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


def _result(query, *, candidates=None, skipped=None):
    return {
        "provider": "github_rest_v1", "query": query,
        "retrieved_at": "2026-07-11T00:00:00+00:00",
        "candidates": list(candidates or []), "skipped": list(skipped or []),
    }


class ScriptedProvider:
    name = "github_rest_v1"

    def __init__(self, scripted):
        self.scripted = list(scripted)
        self.calls = []

    def search(self, *, query, max_candidates):
        self.calls.append((query, max_candidates))
        item = self.scripted.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _env(tmp_path, provider, *, score=0.1, est_cost=0.1):
    daemon = WriteDaemon(db.connect(str(tmp_path / "research.sqlite")))
    state = SQLiteStateStore(daemon, POLICY)
    state.create_goal(text="比较新结构与外部 baseline", predicate_json={})
    with daemon.transaction() as conn:
        conn.execute(
            "INSERT INTO cycle(id,goal_id,goal_ver,status,route,policy_version) "
            "VALUES (1,1,1,'idea','attack','test')")
        conn.execute(
            "INSERT INTO question(id,goal_id,goal_ver,born_goal_ver,text,status,score,est_cost,"
            "source,born_cycle,active_cycle) VALUES (1,1,1,1,'需要外部 comparator','active',?,?,"
            "'agent',1,1)", (score, est_cost))
        conn.execute("UPDATE cycle SET active_question_id=1 WHERE id=1")
        conn.execute(
            "INSERT INTO idea(question_id,cycle_id,content_md,audit_json,status) "
            "VALUES (1,1,'# selected\n引入独立家族对照','{}','selected')")
    ledger = CostLedger(daemon, POLICY)
    service = ImportSearchService(
        daemon=daemon, policy=POLICY, provider=provider,
        work_root=str(tmp_path), cost_ledger=ledger)
    return daemon, service, NS(cycle_id="c1", question_id="q1")


def test_search_registers_candidate_license_runner_and_decision_atomically(tmp_path):
    provider = ScriptedProvider([_result(REQUEST["query"], candidates=[_candidate()])])
    daemon, service, cyc = _env(tmp_path, provider)

    outcome = service(cyc, REQUEST)

    assert outcome["candidate_count"] == 1
    assert provider.calls == [(REQUEST["query"], 2)]  # small: retrieval policy 档，非模型自报
    assert daemon.query_one(
        "SELECT status,phase FROM runner_call WHERE id=?",
        (outcome["runner_call_id"],)) == ("success", "import_search")
    assert daemon.query_one(
        "SELECT tokens_total,money FROM ledger WHERE runner_call_id=?",
        (outcome["runner_call_id"],)) == (0, 0.0)
    candidate = daemon.query_one(
        "SELECT trigger_kind,revision,license_id_seen,search_provider,search_query,rank "
        "FROM external_candidate")
    assert candidate == (
        "new_structure", "a" * 40, "MIT", "github_rest_v1", REQUEST["query"], 0)
    license_row = daemon.query_one(
        "SELECT decision,license_id,evidence_ref,actor,policy_hash FROM license_review")
    assert license_row[:2] == ("allow", "MIT")
    assert license_row[2].endswith("?ref=" + "a" * 40)
    assert license_row[3] == "auto" and license_row[4].startswith("sha256:")
    snapshot = DeferredImporter.plan_snapshot(
        daemon.conn, question_id=1, action_cycle=1,
        policy_hash=DeferredImporter.policy_hash(POLICY))
    assert snapshot["selected"] is not None
    assert snapshot["selected"]["review"]["evidence_ref"] == license_row[2]
    assert daemon.query_one(
        "SELECT count(*) FROM decision WHERE type='import_search_completed'")[0] == 1


def test_non_allowlisted_spdx_is_review_only_and_cannot_emit_import_defer(tmp_path):
    provider = ScriptedProvider([
        _result(REQUEST["query"], candidates=[_candidate(spdx="GPL-3.0-only")])])
    daemon, service, cyc = _env(tmp_path, provider)

    service(cyc, REQUEST)

    assert daemon.query_one(
        "SELECT decision,license_scope_json,license_id FROM license_review") == (
            "review", None, "GPL-3.0-only")
    snapshot = DeferredImporter.plan_snapshot(
        daemon.conn, question_id=1, action_cycle=1,
        policy_hash=DeferredImporter.policy_hash(POLICY))
    assert snapshot["selected"] is None


def test_human_license_event_carries_replayable_evidence_and_can_supersede_review(tmp_path):
    provider = ScriptedProvider([
        _result(REQUEST["query"], candidates=[_candidate(spdx="GPL-3.0-only")])])
    daemon, service, cyc = _env(tmp_path, provider)
    service(cyc, REQUEST)
    candidate_id = daemon.query_one("SELECT id FROM external_candidate")[0]
    policy_hash = DeferredImporter.policy_hash(POLICY)

    human_review_id = DeferredImporter(daemon).review_license(
        candidate_id=candidate_id, decision="allow", actor="human",
        license_id="GPL-3.0-only",
        evidence_ref="decision:human-license-approval:ticket-42",
        license_scope_json=json.dumps({
            "allow_eval": True, "allow_modify": False,
            "allow_publish_pool": True, "allow_redistribute": False,
        }, sort_keys=True),
        decided_cycle="c1", policy_hash=policy_hash)
    snapshot = DeferredImporter.plan_snapshot(
        daemon.conn, question_id=1, action_cycle=1, policy_hash=policy_hash)

    assert snapshot["selected"]["review"]["license_review_id"] == human_review_id
    assert snapshot["selected"]["review"]["actor"] == "human"
    assert snapshot["selected"]["review"]["evidence_ref"].endswith("ticket-42")


def test_zero_result_has_durable_marker_and_is_not_searched_twice(tmp_path):
    provider = ScriptedProvider([_result(REQUEST["query"])])
    daemon, service, cyc = _env(tmp_path, provider)

    first = service(cyc, REQUEST)
    second = service(cyc, dict(REQUEST))

    assert first == second
    assert len(provider.calls) == 1
    assert first["candidate_count"] == 0
    assert daemon.query_one("SELECT count(*) FROM external_candidate")[0] == 0
    assert daemon.query_one(
        "SELECT count(*) FROM runner_call WHERE phase='import_search'")[0] == 1


def test_receipt_before_db_crash_recovers_without_refetch(tmp_path, monkeypatch):
    provider = ScriptedProvider([_result(REQUEST["query"], candidates=[_candidate()])])
    daemon, service, cyc = _env(tmp_path, provider)

    def crash():
        raise RuntimeError("crash-after-receipt")

    monkeypatch.setattr(service, "_after_receipt", crash)
    with pytest.raises(RuntimeError, match="crash-after-receipt"):
        service(cyc, REQUEST)
    assert daemon.query_one("SELECT status FROM runner_call")[0] == "running"
    assert daemon.query_one("SELECT count(*) FROM external_candidate")[0] == 0

    recovered = ImportSearchService(
        daemon=daemon, policy=POLICY, provider=provider,
        work_root=str(tmp_path), cost_ledger=service.cost_ledger)
    outcome = recovered(cyc, REQUEST)
    assert outcome["candidate_count"] == 1
    assert len(provider.calls) == 1
    assert daemon.query_one("SELECT count(*) FROM external_candidate")[0] == 1


def test_receipt_version_rejects_bool_even_though_true_equals_one(tmp_path, monkeypatch):
    provider = ScriptedProvider([_result(REQUEST["query"])])
    daemon, service, cyc = _env(tmp_path, provider)
    monkeypatch.setattr(
        service, "_after_receipt",
        lambda: (_ for _ in ()).throw(RuntimeError("stop-after-receipt")))
    with pytest.raises(RuntimeError, match="stop-after-receipt"):
        service(cyc, REQUEST)
    receipt_path = Path(daemon.query_one(
        "SELECT transcript_ref FROM runner_call WHERE phase='import_search'")[0])
    receipt = read_receipt(receipt_path)
    receipt["version"] = True
    atomic_write_receipt(receipt_path, receipt)

    recovered = ImportSearchService(
        daemon=daemon, policy=POLICY, provider=provider,
        work_root=str(tmp_path), cost_ledger=service.cost_ledger)
    with pytest.raises(ImportSearchError, match="receipt 身份"):
        recovered(cyc, REQUEST)
    assert len(provider.calls) == 1


def test_provider_failure_is_terminal_for_call_but_same_persisted_request_can_retry(tmp_path):
    provider = ScriptedProvider([
        ImportSearchProviderError("rate limited"),
        _result(REQUEST["query"], candidates=[_candidate()]),
    ])
    daemon, service, cyc = _env(tmp_path, provider)

    with pytest.raises(ImportSearchProviderError, match="rate limited"):
        service(cyc, REQUEST)
    assert daemon.query_one(
        "SELECT status,failure_kind FROM runner_call ORDER BY id") == (
            "failed", "provider_error")
    assert daemon.query_one("SELECT count(*) FROM external_candidate")[0] == 0

    with pytest.raises(ImportSearchError, match="已绑定不同"):
        service(cyc, {**REQUEST, "query": "change query after failure"})
    assert len(provider.calls) == 1

    outcome = service(cyc, REQUEST)
    assert outcome["candidate_count"] == 1
    assert len(provider.calls) == 2
    assert daemon.query_one(
        "SELECT count(*) FROM runner_call WHERE phase='import_search'")[0] == 2


def test_completed_cycle_rejects_a_different_search_request(tmp_path):
    provider = ScriptedProvider([_result(REQUEST["query"])])
    _daemon, service, cyc = _env(tmp_path, provider)
    service(cyc, REQUEST)

    changed = {**REQUEST, "query": "a second query"}
    with pytest.raises(ImportSearchError, match="只允许一个"):
        service(cyc, changed)


@pytest.mark.parametrize(
    "score,est_cost,expected_limit",
    [(0.1, 0.1, 2), (0.5, 2.0, 5), (0.9, 4.0, 10)],
)
def test_retrieval_scale_is_mechanical_from_question_and_budget(
        tmp_path, score, est_cost, expected_limit):
    provider = ScriptedProvider([_result(REQUEST["query"])])
    _daemon, service, cyc = _env(
        tmp_path, provider, score=score, est_cost=est_cost)
    service(cyc, REQUEST)
    assert provider.calls == [(REQUEST["query"], expected_limit)]


class _Response:
    def __init__(self, url, payload):
        self._url = url
        self._raw = json.dumps(payload).encode("utf-8")
        self.headers = {"Content-Length": str(len(self._raw))}

    def geturl(self):
        return self._url

    def read(self, _limit):
        return self._raw

    def close(self):
        pass


def test_github_provider_pins_commit_and_hashes_license_without_leaking_token(
        monkeypatch):
    revision = "c" * 40
    license_bytes = b"permission text"
    seen = []

    def opener(request, timeout):
        seen.append((request, timeout))
        url = request.full_url
        if "/search/repositories?" in url:
            payload = {"items": [{
                "id": 7, "full_name": "owner/repo", "default_branch": "main",
                "stargazers_count": 9, "updated_at": "2026-07-01T00:00:00Z",
            }]}
        elif "/commits/main" in url:
            payload = {"sha": revision}
        else:
            payload = {
                "path": "LICENSE", "encoding": "base64",
                "content": base64.b64encode(license_bytes).decode("ascii"),
                "license": {"spdx_id": "MIT"},
            }
        return _Response(url, payload)

    monkeypatch.setenv("METARESEARCH_GITHUB_TOKEN", "secret-must-not-persist")
    provider = GitHubRepoSearchProvider(POLICY["import_search"], opener=opener)
    result = provider.search(query="frozen baseline", max_candidates=1)

    candidate = result["candidates"][0]
    assert candidate["revision"] == revision
    assert candidate["license"]["content_sha256"].startswith("sha256:")
    assert candidate["license"]["evidence_ref"].endswith("?ref=" + revision)
    assert "secret-must-not-persist" not in json.dumps(result)
    assert all(req.get_header("Authorization") == "Bearer secret-must-not-persist"
               for req, _timeout in seen)


def test_github_provider_direct_resolve_verifies_human_requested_commit():
    revision = "d" * 40
    seen = []

    def opener(request, timeout):
        seen.append((request.full_url, timeout))
        url = request.full_url
        if url == "https://api.github.com/repos/owner/repo":
            payload = {
                "id": 9, "full_name": "owner/repo", "default_branch": "main",
                "stargazers_count": 11, "updated_at": "2026-07-01T00:00:00Z",
            }
        elif url.endswith("/commits/" + revision):
            payload = {"sha": revision}
        else:
            payload = {
                "path": "LICENSE", "encoding": "base64",
                "content": base64.b64encode(b"license").decode("ascii"),
                "license": {"spdx_id": "Apache-2.0"},
            }
        return _Response(url, payload)

    provider = GitHubRepoSearchProvider(POLICY["import_search"], opener=opener)
    query = f"human_named:https://github.com/owner/repo@{revision}"
    result = provider.resolve_repository(
        canonical_uri="https://github.com/owner/repo",
        requested_revision=revision, query=query)

    assert result["query"] == query
    assert result["candidates"][0]["revision"] == revision
    assert result["candidates"][0]["license"]["spdx_id"] == "Apache-2.0"
    assert any(url.endswith("/commits/" + revision) for url, _ in seen)


def test_github_provider_direct_resolve_rejects_noncanonical_uri_without_network():
    called = []
    provider = GitHubRepoSearchProvider(
        POLICY["import_search"],
        opener=lambda *_args, **_kwargs: called.append(True))
    with pytest.raises(ImportSearchProviderError, match="非规范"):
        provider.resolve_repository(
            canonical_uri="https://github.com/owner/repo/",
            requested_revision=None, query="bad")
    assert called == []
