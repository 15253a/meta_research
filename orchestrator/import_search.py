"""Durable, read-only repository discovery for the deferred-import pipeline.

The plan model may request a bounded search, but it never reports candidates or
license facts and never receives a database capability.  A trusted host
connector resolves immutable revisions and pinned license evidence outside a
database transaction.  Its private receipt is then consumed by one short
transaction which atomically registers candidates, license decisions, the
runner terminal state, and the completion decision.

An interrupted read-only HTTP call can be repeated when no receipt exists; DB
registration is exactly-once.  Once the receipt exists, recovery finalizes it
without another provider call.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .budgeting import compute_budget
from .ids import cnum as _cnum, qnum as _qnum
from .importer import DeferredImporter
from .interfaces import CallUsage
from .process_supervisor import atomic_write_receipt, read_receipt


_PROTOCOL = "import-search-v1"
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_FULL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_SPDX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+-]{0,127}$")
_REQUEST_KEYS = frozenset({"version", "trigger_kind", "query", "need_summary"})
_COMPLETION_KEYS = frozenset({
    "protocol", "request", "request_hash", "trigger_snapshot_hash", "policy_hash",
    "provider", "runner_call_id", "receipt_ref", "result_hash", "retrieval",
    "candidate_ids", "license_review_ids", "candidate_count", "skipped_count",
})


class ImportSearchError(RuntimeError):
    """The discovery request, receipt, or durable state is unsafe."""


class ImportSearchProviderError(ImportSearchError):
    """The trusted read-only connector failed before publishing a receipt."""


class _TriggerChanged(ImportSearchError):
    """The action-cycle inputs changed while a connector call was in flight."""


class _GitHubOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject a redirect before urllib can forward the Authorization header."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        target = urllib.parse.urlsplit(newurl)
        if target.scheme != "https" or target.hostname != "api.github.com":
            raise urllib.error.HTTPError(
                req.full_url, code, "GitHub redirect escaped api.github.com",
                headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False).encode("utf-8")


def _canonical_text(value: Any) -> str:
    return _canonical_bytes(value).decode("utf-8")


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _bytes_hash(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _bounded_text(value: Any, *, field: str, max_bytes: int,
                  optional: bool = False) -> Optional[str]:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value:
        raise ImportSearchError(f"{field} 须为非空字符串")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise ImportSearchError(f"{field} 不是合法 UTF-8") from error
    if size > max_bytes:
        raise ImportSearchError(f"{field} 超过 {max_bytes} bytes")
    return value


def validate_import_search_request(request: Any) -> Dict[str, Any]:
    """Validate the control sidecar independently of jsonschema injection."""
    if not isinstance(request, dict) or set(request) != _REQUEST_KEYS:
        raise ImportSearchError(
            f"import_search_request 须精确包含 {sorted(_REQUEST_KEYS)}")
    if isinstance(request.get("version"), bool) or request.get("version") != 1:
        raise ImportSearchError("import_search_request.version 只接受 1")
    if request.get("trigger_kind") != "new_structure":
        raise ImportSearchError(
            "生产 import_search 只开放 new_structure 类型门；"
            "stuck 不得直达，human_named/sota_reference 须有受信冻结来源")
    query = _bounded_text(request.get("query"), field="import search query", max_bytes=2048)
    summary = _bounded_text(
        request.get("need_summary"), field="import need_summary", max_bytes=8192)
    if len(query) > 512 or len(summary) > 2048:
        raise ImportSearchError("import_search_request 字符数超过 schema 上限")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in query):
        raise ImportSearchError("import search query 不得含控制字符")
    if any((ord(char) < 0x20 and char not in "\n\r\t") or ord(char) == 0x7F
           for char in summary):
        raise ImportSearchError("import need_summary 不得含非文本控制字符")
    # Return a detached canonical shape so a caller cannot mutate the request
    # between request hashing and provider invocation.
    return {
        "version": 1, "trigger_kind": "new_structure",
        "query": query, "need_summary": summary,
    }


def _strict_json(raw: bytes, *, label: str) -> Any:
    def unique(pairs):  # noqa: ANN001
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"重复 JSON key: {key}")
            result[key] = value
        return result

    try:
        return json.loads(
            raw.decode("utf-8"), object_pairs_hook=unique,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"非有限 JSON number: {token}")))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ImportSearchProviderError(f"{label} 返回非严格 JSON") from error


class GitHubRepoSearchProvider:
    """A bounded GitHub REST reader; it never clones or executes repository code."""

    name = "github_rest_v1"

    def __init__(self, config: Mapping[str, Any], *, opener=None,
                 token_env: str = "METARESEARCH_GITHUB_TOKEN"):
        self.timeout_s = float(config["timeout_s"])
        self.max_response_bytes = int(config["max_response_bytes"])
        self.max_candidates = int(config["max_candidates"])
        self.opener = opener or urllib.request.build_opener(
            _GitHubOnlyRedirectHandler()).open
        self.token_env = token_env

    def _get_json(self, url: str, *, label: str, allow_404: bool = False) -> Any:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "meta-research-import-search/1",
        }
        token = os.environ.get(self.token_env)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            response = self.opener(request, timeout=self.timeout_s)
        except urllib.error.HTTPError as error:
            try:
                if allow_404 and error.code == 404:
                    return None
                raise ImportSearchProviderError(
                    f"GitHub {label} HTTP {error.code}") from error
            finally:
                error.close()
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise ImportSearchProviderError(
                f"GitHub {label} 只读调用失败: {type(error).__name__}") from error
        try:
            final = urllib.parse.urlsplit(response.geturl())
            if final.scheme != "https" or final.hostname != "api.github.com":
                raise ImportSearchProviderError(
                    f"GitHub {label} 重定向越出 api.github.com")
            declared = response.headers.get("Content-Length")
            if declared is not None:
                try:
                    declared_size = int(declared)
                except ValueError as error:
                    raise ImportSearchProviderError(
                        f"GitHub {label} Content-Length 非整数") from error
                if declared_size < 0 or declared_size > self.max_response_bytes:
                    raise ImportSearchProviderError(
                        f"GitHub {label} 返回超过字节上限")
            raw = response.read(self.max_response_bytes + 1)
            if len(raw) > self.max_response_bytes:
                raise ImportSearchProviderError(
                    f"GitHub {label} 返回超过字节上限")
        finally:
            response.close()
        return _strict_json(raw, label=f"GitHub {label}")

    @staticmethod
    def _repository_item(item: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(item, dict):
            return None
        full_name = item.get("full_name")
        default_branch = item.get("default_branch")
        repo_id = item.get("id")
        stars = item.get("stargazers_count")
        updated_at = item.get("updated_at")
        if (not isinstance(full_name, str) or not _FULL_NAME_RE.fullmatch(full_name)
                or not isinstance(default_branch, str) or not default_branch
                or len(default_branch.encode("utf-8")) > 512
                or any(ord(char) < 0x20 or ord(char) == 0x7F
                       for char in default_branch)
                or isinstance(repo_id, bool) or not isinstance(repo_id, int) or repo_id <= 0
                or isinstance(stars, bool) or not isinstance(stars, int) or stars < 0
                or not isinstance(updated_at, str) or not updated_at
                or len(updated_at.encode("utf-8")) > 128):
            return None
        return {
            "provider_result_id": str(repo_id),
            "full_name": full_name,
            "default_branch": default_branch,
            "stars": stars,
            "updated_at": updated_at,
        }

    def search(self, *, query: str, max_candidates: int) -> Dict[str, Any]:
        if (isinstance(max_candidates, bool) or not isinstance(max_candidates, int)
                or not 1 <= max_candidates <= self.max_candidates):
            raise ValueError("GitHub max_candidates 越界")
        encoded_query = urllib.parse.urlencode({
            "q": query, "sort": "stars", "order": "desc",
            "per_page": max_candidates,
        })
        search_url = f"https://api.github.com/search/repositories?{encoded_query}"
        payload = self._get_json(search_url, label="repository search")
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise ImportSearchProviderError("GitHub repository search 缺 items[]")
        retrieved_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        candidates = []
        skipped = []
        for raw_item in payload["items"][:max_candidates]:
            item = self._repository_item(raw_item)
            if item is None:
                skipped.append({"provider_result_id": "unknown", "reason": "metadata_invalid"})
                continue
            full_name = item["full_name"]
            branch = urllib.parse.quote(item["default_branch"], safe="")
            commit_url = f"https://api.github.com/repos/{full_name}/commits/{branch}"
            commit = self._get_json(
                commit_url, label=f"commit {item['provider_result_id']}", allow_404=True)
            if commit is None:
                skipped.append({
                    "provider_result_id": item["provider_result_id"],
                    "reason": "revision_unavailable",
                })
                continue
            revision = commit.get("sha") if isinstance(commit, dict) else None
            if not isinstance(revision, str) or not _COMMIT_RE.fullmatch(revision):
                skipped.append({
                    "provider_result_id": item["provider_result_id"],
                    "reason": "revision_invalid",
                })
                continue
            license_url = f"https://api.github.com/repos/{full_name}/license?ref={revision}"
            license_payload = self._get_json(
                license_url, label=f"license {item['provider_result_id']}", allow_404=True)
            if license_payload is None:
                license_info = {
                    "spdx_id": "NOASSERTION", "lookup_status": "missing",
                    "evidence_ref": license_url, "content_sha256": None,
                }
            else:
                if not isinstance(license_payload, dict):
                    raise ImportSearchProviderError(
                        f"GitHub license {item['provider_result_id']} 非 object")
                license_obj = license_payload.get("license")
                spdx_id = (license_obj.get("spdx_id")
                           if isinstance(license_obj, dict) else None)
                if not isinstance(spdx_id, str) or not _SPDX_RE.fullmatch(spdx_id):
                    spdx_id = "NOASSERTION"
                encoded = license_payload.get("content")
                encoding = license_payload.get("encoding")
                if not isinstance(encoded, str) or encoding != "base64":
                    raise ImportSearchProviderError(
                        f"GitHub license {item['provider_result_id']} 缺 base64 content")
                try:
                    content = base64.b64decode("".join(encoded.split()), validate=True)
                except (ValueError, base64.binascii.Error) as error:
                    raise ImportSearchProviderError(
                        f"GitHub license {item['provider_result_id']} base64 损坏") from error
                if len(content) > self.max_response_bytes:
                    raise ImportSearchProviderError(
                        f"GitHub license {item['provider_result_id']} 解码后超限")
                path = license_payload.get("path")
                if not isinstance(path, str) or not path or len(path.encode("utf-8")) > 2048:
                    raise ImportSearchProviderError(
                        f"GitHub license {item['provider_result_id']} path 非法")
                evidence_ref = (
                    f"https://api.github.com/repos/{full_name}/contents/"
                    f"{urllib.parse.quote(path, safe='/')}?ref={revision}")
                license_info = {
                    "spdx_id": spdx_id, "lookup_status": "found",
                    "evidence_ref": evidence_ref,
                    "content_sha256": _bytes_hash(content),
                }
            candidates.append({
                "provider_result_id": item["provider_result_id"],
                "canonical_uri": f"https://github.com/{full_name}",
                "revision": revision,
                "repository": {
                    "full_name": full_name, "default_branch": item["default_branch"],
                    "stars": item["stars"], "updated_at": item["updated_at"],
                },
                "license": license_info,
            })
        return {
            "provider": self.name, "query": query, "retrieved_at": retrieved_at,
            "candidates": candidates, "skipped": skipped,
        }


class ImportSearchService:
    """Orchestrate durable discovery and atomic import registration."""

    def __init__(self, *, daemon, policy: Dict[str, Any], provider,
                 work_root: str, cost_ledger=None, owner_guard=None):
        self.daemon = daemon
        self.policy = policy
        self.config = policy["import_search"]
        self.provider = provider
        self.work = Path(work_root)
        self.cost_ledger = cost_ledger
        self.owner_guard = owner_guard or (lambda: None)
        if getattr(provider, "name", None) != self.config["provider"]:
            raise ValueError(
                "import_search provider 身份与 policy.import_search.provider 不一致")
        thresholds = policy["retrieval"]["scale_thresholds"]
        if (float(thresholds["medium"]["est_cost_ratio"])
                > float(thresholds["large"]["est_cost_ratio"])
                or float(thresholds["medium"]["score"])
                > float(thresholds["large"]["score"])):
            raise ValueError("retrieval.scale_thresholds 必须 medium <= large")
        self._lock = threading.Lock()

    @staticmethod
    def _completion_in_txn(conn, cycle_id: int) -> Optional[Dict[str, Any]]:
        rows = conn.execute(
            "SELECT id,payload_json FROM decision WHERE cycle_id=? "
            "AND actor='orchestrator' AND type='import_search_completed' ORDER BY id",
            (cycle_id,)).fetchall()
        if len(rows) > 1:
            raise ImportSearchError(
                f"cycle c{cycle_id} 存在多个 import_search_completed，状态腐化")
        if not rows:
            return None
        try:
            payload = json.loads(
                rows[0][1], parse_constant=lambda token: (_ for _ in ()).throw(
                    ValueError(f"非有限 JSON number: {token}")))
        except (json.JSONDecodeError, ValueError) as error:
            raise ImportSearchError("import_search_completed payload 损坏") from error
        if (not isinstance(payload, dict) or set(payload) != _COMPLETION_KEYS
                or payload.get("protocol") != _PROTOCOL
                or not isinstance(payload.get("request"), dict)
                or payload.get("request_hash") != _hash(payload["request"])
                or not isinstance(payload.get("request_hash"), str)
                or not _SHA256_RE.fullmatch(payload["request_hash"])
                or not isinstance(payload.get("trigger_snapshot_hash"), str)
                or not _SHA256_RE.fullmatch(payload["trigger_snapshot_hash"])
                or not isinstance(payload.get("policy_hash"), str)
                or not _SHA256_RE.fullmatch(payload["policy_hash"])
                or not isinstance(payload.get("result_hash"), str)
                or not _SHA256_RE.fullmatch(payload["result_hash"])
                or isinstance(payload.get("runner_call_id"), bool)
                or not isinstance(payload.get("runner_call_id"), int)
                or payload["runner_call_id"] <= 0
                or not isinstance(payload.get("receipt_ref"), str)
                or not payload["receipt_ref"]
                or not isinstance(payload.get("candidate_ids"), list)
                or not isinstance(payload.get("license_review_ids"), list)
                or any(isinstance(value, bool) or not isinstance(value, int) or value <= 0
                       for value in payload["candidate_ids"] + payload["license_review_ids"])
                or len(set(payload["candidate_ids"])) != len(payload["candidate_ids"])
                or len(set(payload["license_review_ids"])) != len(payload["license_review_ids"])
                or isinstance(payload.get("candidate_count"), bool)
                or not isinstance(payload.get("candidate_count"), int)
                or payload["candidate_count"] < 0
                or payload["candidate_count"] != len(payload["candidate_ids"])
                or payload["candidate_count"] != len(payload["license_review_ids"])
                or isinstance(payload.get("skipped_count"), bool)
                or not isinstance(payload.get("skipped_count"), int)
                or payload["skipped_count"] < 0):
            raise ImportSearchError("import_search_completed payload 协议非法")
        return payload

    def _verify_completion(self, cyc, request: Dict[str, Any],
                           request_hash: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if payload["request"] != request or payload["request_hash"] != request_hash:
            raise ImportSearchError(
                f"cycle {cyc.cycle_id} 只允许一个 import_search 请求")
        runner_call_id = payload["runner_call_id"]
        expected_path = self._receipt_path(
            cyc.cycle_id, request_hash, runner_call_id)
        if payload["receipt_ref"] != str(expected_path):
            raise ImportSearchError("import_search_completed receipt_ref 不是规范路径")
        receipt = self._validate_receipt(
            read_receipt(expected_path), runner_call_id=runner_call_id,
            cycle_id=cyc.cycle_id, question_id=cyc.question_id,
            request=request, request_hash=request_hash,
            provider=self.provider.name)
        if (receipt["result_hash"] != payload["result_hash"]
                or receipt["trigger_snapshot_hash"] != payload["trigger_snapshot_hash"]
                or receipt["policy_hash"] != payload["policy_hash"]
                or receipt["retrieval"] != payload["retrieval"]
                or len(receipt["result"]["candidates"]) != payload["candidate_count"]
                or len(receipt["result"]["skipped"]) != payload["skipped_count"]):
            raise ImportSearchError("import_search_completed 与回执不一致")
        runner = self.daemon.query_one(
            "SELECT cycle_id,phase,purpose,status,transcript_ref FROM runner_call WHERE id=?",
            (runner_call_id,))
        if (runner is None or runner != (
                _cnum(cyc.cycle_id), "import_search",
                f"import_search:{request_hash}", "success", str(expected_path))):
            raise ImportSearchError("import_search_completed 与 runner_call 终态不一致")
        candidate_rows = self.daemon.query(
            "SELECT id,trigger_snapshot_hash,search_provider,search_query "
            "FROM external_candidate WHERE question_id=? AND discovered_cycle=? ORDER BY id",
            (_qnum(cyc.question_id), _cnum(cyc.cycle_id)))
        if ([row[0] for row in candidate_rows] != payload["candidate_ids"]
                or any(row[1:] != (
                    payload["trigger_snapshot_hash"], payload["provider"], request["query"])
                    for row in candidate_rows)):
            raise ImportSearchError("import_search_completed 与 candidate 登记不一致")
        review_rows = self.daemon.query(
            "SELECT id,candidate_id,decided_cycle,policy_hash FROM license_review "
            f"WHERE id IN ({','.join('?' for _ in payload['license_review_ids'])}) ORDER BY id"
            if payload["license_review_ids"] else
            "SELECT id,candidate_id,decided_cycle,policy_hash FROM license_review WHERE 0",
            tuple(payload["license_review_ids"]))
        if ([row[0] for row in review_rows] != payload["license_review_ids"]
                or [row[1] for row in review_rows] != payload["candidate_ids"]
                or any(row[2:] != (_cnum(cyc.cycle_id), payload["policy_hash"])
                       for row in review_rows)):
            raise ImportSearchError("import_search_completed 与 license_review 登记不一致")
        return payload

    def _trigger_context_in_txn(self, conn, cyc, request: Dict[str, Any]) -> Dict[str, Any]:
        cycle_id = _cnum(cyc.cycle_id)
        question_id = _qnum(cyc.question_id)
        row = conn.execute(
            "SELECT c.goal_id,c.goal_ver,c.status,c.route,c.active_question_id,"
            "q.goal_id,q.goal_ver,q.status,q.active_cycle,q.text,q.score,q.est_cost,"
            "g.text,g.predicate_json,(SELECT MAX(version) FROM goal WHERE id=c.goal_id) "
            "FROM cycle c JOIN question q ON q.id=? "
            "JOIN goal g ON g.id=c.goal_id AND g.version=c.goal_ver WHERE c.id=?",
            (question_id, cycle_id)).fetchone()
        if (row is None or row[2] != "idea" or row[4] != question_id
                or tuple(row[:2]) != tuple(row[5:7]) or row[7] != "active"
                or row[8] != cycle_id or row[14] != row[1]):
            raise _TriggerChanged(
                f"import_search 只允许 current plan 边界的 exact active cycle/question: "
                f"{cyc.cycle_id}/{cyc.question_id}")
        ideas = conn.execute(
            "SELECT id,content_md,audit_json FROM idea "
            "WHERE cycle_id=? AND status='selected' ORDER BY id", (cycle_id,)).fetchall()
        if len(ideas) != 1:
            raise _TriggerChanged(
                f"import_search 要求当前 cycle 恰一 selected idea，实收 {len(ideas)}")
        if conn.execute(
                "SELECT 1 FROM external_candidate WHERE question_id=? AND discovered_cycle=? LIMIT 1",
                (question_id, cycle_id)).fetchone() is not None:
            raise ImportSearchError(
                "当前 action-cycle 已有冻结 external_candidate，不得再发搜索")

        def finite(value: Any, *, field: str) -> float:
            if value is None:
                return 0.0
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ImportSearchError(f"{field} 非有限数字")
            result = float(value)
            if not math.isfinite(result):
                raise ImportSearchError(f"{field} 非有限数字")
            return result

        budget = compute_budget(conn, self.policy["budget"])
        score = finite(row[10], field="question.score")
        est_cost = max(0.0, finite(row[11], field="question.est_cost"))
        ratio = est_cost / budget if budget > 0 else 0.0
        thresholds = self.policy["retrieval"]["scale_thresholds"]
        if (ratio >= float(thresholds["large"]["est_cost_ratio"])
                or score >= float(thresholds["large"]["score"])):
            scale = "large"
        elif (ratio >= float(thresholds["medium"]["est_cost_ratio"])
                or score >= float(thresholds["medium"]["score"])):
            scale = "medium"
        else:
            scale = "small"
        limit = min(
            int(self.config["max_candidates"]),
            int(self.policy["retrieval"]["budget_by_question_scale"][scale]))
        if limit <= 0:
            raise ImportSearchError(
                f"retrieval scale={scale} 的 import_search 预算为 0")
        policy_hash = DeferredImporter.policy_hash(self.policy)
        context = {
            "version": 1,
            "cycle": {
                "cycle_id": cyc.cycle_id, "question_id": cyc.question_id,
                "goal_id": row[0], "goal_ver": row[1], "status": row[2],
                "route": row[3],
            },
            "question": {
                "text_hash": _bytes_hash(row[9].encode("utf-8")),
                "score": score, "est_cost": est_cost,
            },
            "goal": {
                "text_hash": _bytes_hash(row[12].encode("utf-8")),
                "predicate_hash": _bytes_hash(row[13].encode("utf-8")),
            },
            "selected_idea": {
                "idea_id": ideas[0][0],
                "content_hash": _bytes_hash(ideas[0][1].encode("utf-8")),
                "audit_hash": (_bytes_hash(ideas[0][2].encode("utf-8"))
                               if ideas[0][2] is not None else None),
            },
            "request": request,
            "policy_hash": policy_hash,
            "retrieval": {
                "scale": scale, "candidate_limit": limit, "B_t": budget,
                "recipe": self.policy["retrieval"]["recipe"],
            },
        }
        context["trigger_snapshot_hash"] = _hash(context)
        return context

    def _receipt_path(self, cycle_id: str, request_hash: str,
                      runner_call_id: int) -> Path:
        # One path per invocation: a retry must not overwrite the receipt still
        # referenced by an earlier failed runner_call.  DB registration remains
        # keyed by request/completion, while provider attempts retain full audit.
        return (self.work / "state" / "import-search" / cycle_id /
                request_hash.removeprefix("sha256:") /
                f"call-{runner_call_id}.json")

    def _finish_failed(self, runner_call_id: int, *, failure_kind: str,
                       wallclock_sec: float = 0.0) -> None:
        row = self.daemon.query_one(
            "SELECT status FROM runner_call WHERE id=?", (runner_call_id,))
        if row is None or row[0] not in ("created", "running"):
            return
        if row[0] == "created":
            with self.daemon.transaction() as conn:
                conn.execute(
                    "UPDATE runner_call SET status='aborted',failure_kind=?,"
                    "finished_at=CURRENT_TIMESTAMP WHERE id=? AND status='created'",
                    (failure_kind[:200], runner_call_id))
            return
        usage = CallUsage(tokens_known=True, wallclock_sec=max(0.0, wallclock_sec))
        if self.cost_ledger is not None:
            self.cost_ledger.finish_call(
                runner_call_id=runner_call_id, status="failed", usage=usage,
                failure_kind=failure_kind[:200])
        else:
            with self.daemon.transaction() as conn:
                changed = conn.execute(
                    "UPDATE runner_call SET status='failed',failure_kind=?,"
                    "finished_at=CURRENT_TIMESTAMP WHERE id=? AND status='running'",
                    (failure_kind[:200], runner_call_id)).rowcount
                if changed != 1:
                    raise ImportSearchError(
                        f"runner_call {runner_call_id} 无法记录 import_search 失败")

    @staticmethod
    def _validate_result(result: Any, *, provider: str, query: str,
                         limit: int) -> Dict[str, Any]:
        expected = {"provider", "query", "retrieved_at", "candidates", "skipped"}
        if not isinstance(result, dict) or set(result) != expected:
            raise ImportSearchError("import search provider result 顶层结构非法")
        if result["provider"] != provider or result["query"] != query:
            raise ImportSearchError("import search provider/query 回执与请求不一致")
        retrieved_at = _bounded_text(
            result["retrieved_at"], field="retrieved_at", max_bytes=128)
        try:
            parsed_time = datetime.fromisoformat(retrieved_at)
        except ValueError as error:
            raise ImportSearchError("retrieved_at 非 ISO-8601") from error
        if parsed_time.tzinfo is None:
            raise ImportSearchError("retrieved_at 必须带时区")
        candidates = result["candidates"]
        skipped = result["skipped"]
        if (not isinstance(candidates, list) or len(candidates) > limit
                or not isinstance(skipped, list) or len(skipped) > limit
                or len(candidates) + len(skipped) > limit):
            raise ImportSearchError("import search candidates/skipped 数量越界")
        normalized_candidates = []
        identities = set()
        result_ids = set()
        for index, candidate in enumerate(candidates):
            if not isinstance(candidate, dict) or set(candidate) != {
                    "provider_result_id", "canonical_uri", "revision",
                    "repository", "license"}:
                raise ImportSearchError(f"candidate[{index}] 结构非法")
            result_id = _bounded_text(
                candidate["provider_result_id"], field="provider_result_id", max_bytes=128)
            if result_id in result_ids:
                raise ImportSearchError("import search provider_result_id 重复")
            result_ids.add(result_id)
            uri = _bounded_text(
                candidate["canonical_uri"], field="canonical_uri", max_bytes=4096)
            revision = candidate["revision"]
            if not isinstance(revision, str) or not _COMMIT_RE.fullmatch(revision):
                raise ImportSearchError(f"candidate[{index}].revision 非 pinned 40-hex commit")
            repo = candidate["repository"]
            if not isinstance(repo, dict) or set(repo) != {
                    "full_name", "default_branch", "stars", "updated_at"}:
                raise ImportSearchError(f"candidate[{index}].repository 结构非法")
            full_name = repo["full_name"]
            if not isinstance(full_name, str) or not _FULL_NAME_RE.fullmatch(full_name):
                raise ImportSearchError(f"candidate[{index}].full_name 非法")
            expected_uri = f"https://github.com/{full_name}"
            if uri != expected_uri:
                raise ImportSearchError(f"candidate[{index}].canonical_uri 不是规范 GitHub URI")
            branch = _bounded_text(
                repo["default_branch"], field="default_branch", max_bytes=512)
            stars = repo["stars"]
            if isinstance(stars, bool) or not isinstance(stars, int) or stars < 0:
                raise ImportSearchError(f"candidate[{index}].stars 非非负整数")
            updated_at = _bounded_text(
                repo["updated_at"], field="repository.updated_at", max_bytes=128)
            license_info = candidate["license"]
            if not isinstance(license_info, dict) or set(license_info) != {
                    "spdx_id", "lookup_status", "evidence_ref", "content_sha256"}:
                raise ImportSearchError(f"candidate[{index}].license 结构非法")
            spdx_id = license_info["spdx_id"]
            if not isinstance(spdx_id, str) or not _SPDX_RE.fullmatch(spdx_id):
                raise ImportSearchError(f"candidate[{index}].license.spdx_id 非法")
            lookup = license_info["lookup_status"]
            if lookup not in ("found", "missing"):
                raise ImportSearchError(f"candidate[{index}].license.lookup_status 非法")
            evidence_ref = _bounded_text(
                license_info["evidence_ref"], field="license.evidence_ref", max_bytes=4096)
            evidence_url = urllib.parse.urlsplit(evidence_ref)
            evidence_query = urllib.parse.parse_qs(
                evidence_url.query, keep_blank_values=True)
            if (evidence_url.scheme != "https" or evidence_url.hostname != "api.github.com"
                    or evidence_query.get("ref") != [revision]
                    or not evidence_url.path.startswith(f"/repos/{full_name}/")):
                raise ImportSearchError(
                    f"candidate[{index}].license.evidence_ref 未 pinned 到同一 revision")
            content_hash = license_info["content_sha256"]
            if content_hash is not None and (
                    not isinstance(content_hash, str) or not _SHA256_RE.fullmatch(content_hash)):
                raise ImportSearchError(f"candidate[{index}].license.content_sha256 非法")
            if (lookup == "found") != (content_hash is not None):
                raise ImportSearchError(
                    f"candidate[{index}] license lookup/content hash 状态矛盾")
            identity = (uri, revision)
            if identity in identities:
                raise ImportSearchError("import search provider 返回重复候选")
            identities.add(identity)
            normalized_candidates.append({
                "provider_result_id": result_id, "canonical_uri": uri,
                "revision": revision,
                "repository": {
                    "full_name": full_name, "default_branch": branch,
                    "stars": stars, "updated_at": updated_at,
                },
                "license": {
                    "spdx_id": spdx_id, "lookup_status": lookup,
                    "evidence_ref": evidence_ref, "content_sha256": content_hash,
                },
            })
        normalized_skipped = []
        for index, item in enumerate(skipped):
            if not isinstance(item, dict) or set(item) != {"provider_result_id", "reason"}:
                raise ImportSearchError(f"skipped[{index}] 结构非法")
            reason = item["reason"]
            if reason not in ("metadata_invalid", "revision_unavailable", "revision_invalid"):
                raise ImportSearchError(f"skipped[{index}].reason 非法")
            normalized_skipped.append({
                "provider_result_id": _bounded_text(
                    item["provider_result_id"], field="skipped.provider_result_id",
                    max_bytes=128),
                "reason": reason,
            })
        return {
            "provider": provider, "query": query, "retrieved_at": retrieved_at,
            "candidates": normalized_candidates, "skipped": normalized_skipped,
        }

    @staticmethod
    def _validate_receipt(receipt: Any, *, runner_call_id: int,
                          cycle_id: str, question_id: str,
                          request: Dict[str, Any], request_hash: str,
                          provider: str) -> Dict[str, Any]:
        expected = {
            "protocol", "version", "runner_call_id", "cycle_id", "question_id",
            "request", "request_hash", "trigger_snapshot_hash", "policy_hash",
            "provider", "retrieval", "wallclock_sec", "result", "result_hash",
        }
        if not isinstance(receipt, dict) or set(receipt) != expected:
            raise ImportSearchError("import_search receipt 结构非法")
        if (receipt["protocol"] != _PROTOCOL
                or isinstance(receipt["version"], bool) or receipt["version"] != 1
                or receipt["runner_call_id"] != runner_call_id
                or receipt["cycle_id"] != cycle_id or receipt["question_id"] != question_id
                or receipt["request"] != request or receipt["request_hash"] != request_hash
                or receipt["provider"] != provider
                or not isinstance(receipt["trigger_snapshot_hash"], str)
                or not _SHA256_RE.fullmatch(receipt["trigger_snapshot_hash"])
                or not isinstance(receipt["policy_hash"], str)
                or not _SHA256_RE.fullmatch(receipt["policy_hash"])
                or receipt["result_hash"] != _hash(receipt["result"])):
            raise ImportSearchError("import_search receipt 身份/hash 不一致")
        wallclock = receipt["wallclock_sec"]
        if (isinstance(wallclock, bool) or not isinstance(wallclock, (int, float))
                or not math.isfinite(float(wallclock)) or float(wallclock) < 0):
            raise ImportSearchError("import_search receipt wallclock_sec 非法")
        retrieval = receipt["retrieval"]
        if not isinstance(retrieval, dict) or set(retrieval) != {
                "scale", "candidate_limit", "B_t", "recipe"}:
            raise ImportSearchError("import_search receipt retrieval 结构非法")
        limit = retrieval["candidate_limit"]
        if (isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10):
            raise ImportSearchError("import_search receipt candidate_limit 非法")
        receipt["result"] = ImportSearchService._validate_result(
            receipt["result"], provider=provider, query=request["query"], limit=limit)
        return receipt

    def _finalize(self, *, cyc, request: Dict[str, Any], request_hash: str,
                  runner_call_id: int, receipt_path: Path) -> Dict[str, Any]:
        receipt = self._validate_receipt(
            read_receipt(receipt_path), runner_call_id=runner_call_id,
            cycle_id=cyc.cycle_id, question_id=cyc.question_id,
            request=request, request_hash=request_hash,
            provider=self.provider.name)
        result = receipt["result"]
        scope_json = _canonical_text(self.config["auto_license"]["scope"])
        allow_spdx = set(self.config["auto_license"]["allow_spdx"])
        candidate_ids = []
        review_ids = []
        budget_hit = None
        with self.daemon.transaction() as conn:
            existing = self._completion_in_txn(conn, _cnum(cyc.cycle_id))
            if existing is not None:
                if existing.get("request_hash") != request_hash:
                    raise ImportSearchError(
                        f"cycle {cyc.cycle_id} 已用不同请求完成 import_search")
                return existing
            runner = conn.execute(
                "SELECT cycle_id,phase,purpose,status,transcript_ref FROM runner_call WHERE id=?",
                (runner_call_id,)).fetchone()
            expected_purpose = f"import_search:{request_hash}"
            if (runner is None or runner[0] != _cnum(cyc.cycle_id)
                    or runner[1] != "import_search" or runner[2] != expected_purpose
                    or runner[3] != "running" or runner[4] != str(receipt_path)):
                raise ImportSearchError(
                    f"runner_call {runner_call_id} 与 import_search receipt 不一致")
            current = self._trigger_context_in_txn(conn, cyc, request)
            if current["trigger_snapshot_hash"] != receipt["trigger_snapshot_hash"]:
                raise _TriggerChanged("import_search 外调期间 action-cycle 触发快照已变")
            if current["policy_hash"] != receipt["policy_hash"]:
                raise _TriggerChanged("import_search 外调期间 policy 已变")
            if current["retrieval"] != receipt["retrieval"]:
                raise _TriggerChanged("import_search receipt 与当前机械检索档不一致")
            for rank, candidate in enumerate(result["candidates"]):
                snapshot = {
                    "version": 1,
                    "provider": result["provider"], "query": result["query"],
                    "provider_result_id": candidate["provider_result_id"],
                    "retrieved_at": result["retrieved_at"],
                    "ranking": {
                        "rank": rank, "recipe": receipt["retrieval"]["recipe"],
                        "scale": receipt["retrieval"]["scale"],
                    },
                    "repository": candidate["repository"],
                    "canonical_uri": candidate["canonical_uri"],
                    "revision": candidate["revision"],
                    "license": candidate["license"],
                    "policy_hash": receipt["policy_hash"],
                }
                snapshot_json = _canonical_text(snapshot)
                candidate_id = DeferredImporter.register_candidate_in_txn(
                    conn, question_id=cyc.question_id,
                    discovered_cycle=cyc.cycle_id,
                    trigger_kind=request["trigger_kind"],
                    trigger_snapshot_hash=receipt["trigger_snapshot_hash"],
                    need_summary=request["need_summary"], source_kind="repo",
                    canonical_uri=candidate["canonical_uri"],
                    revision=candidate["revision"],
                    license_id_seen=candidate["license"]["spdx_id"],
                    search_provider=result["provider"], search_query=result["query"],
                    search_snapshot_json=snapshot_json,
                    search_snapshot_hash=_bytes_hash(snapshot_json.encode("utf-8")),
                    rank=rank, retrieved_at=result["retrieved_at"])
                decision = ("allow" if candidate["license"]["spdx_id"] in allow_spdx
                            else "review")
                review_id = DeferredImporter.review_license_in_txn(
                    conn, candidate_id=candidate_id, decision=decision, actor="auto",
                    license_scope_json=(scope_json if decision == "allow" else None),
                    decided_cycle=cyc.cycle_id, policy_hash=receipt["policy_hash"],
                    license_id=candidate["license"]["spdx_id"],
                    evidence_ref=candidate["license"]["evidence_ref"])
                candidate_ids.append(candidate_id)
                review_ids.append(review_id)
            usage = CallUsage(
                tokens_known=True, wallclock_sec=float(receipt["wallclock_sec"]))
            if self.cost_ledger is not None:
                budget_hit = self.cost_ledger.finish_call_in_txn(
                    conn, runner_call_id=runner_call_id, status="success",
                    usage=usage, transcript_ref=str(receipt_path))
            else:
                changed = conn.execute(
                    "UPDATE runner_call SET status='success',finished_at=CURRENT_TIMESTAMP "
                    "WHERE id=? AND status='running'", (runner_call_id,)).rowcount
                if changed != 1:
                    raise ImportSearchError(
                        f"runner_call {runner_call_id} import_search 无法收口")
            payload = {
                "protocol": _PROTOCOL, "request": request,
                "request_hash": request_hash,
                "trigger_snapshot_hash": receipt["trigger_snapshot_hash"],
                "policy_hash": receipt["policy_hash"], "provider": result["provider"],
                "runner_call_id": runner_call_id, "receipt_ref": str(receipt_path),
                "result_hash": receipt["result_hash"],
                "retrieval": receipt["retrieval"],
                "candidate_ids": candidate_ids, "license_review_ids": review_ids,
                "candidate_count": len(candidate_ids),
                "skipped_count": len(result["skipped"]),
            }
            conn.execute(
                "INSERT INTO decision(cycle_id,question_id,actor,type,payload_json) "
                "VALUES (?,?,'orchestrator','import_search_completed',?)",
                (_cnum(cyc.cycle_id), _qnum(cyc.question_id),
                 _canonical_text(payload)))
        if budget_hit is not None:
            # The already-completed read and its exact registration remain
            # durable; the next external call is stopped by the ledger marker.
            from .cost_ledger import BudgetExhausted
            raise BudgetExhausted(**budget_hit)
        return payload

    def _after_receipt(self) -> None:
        """Crash-injection seam: a receipt is authoritative before DB finalization."""

    def __call__(self, cyc, request: Dict[str, Any], _pack=None) -> Dict[str, Any]:
        request = validate_import_search_request(request)
        request_hash = _hash(request)
        purpose = f"import_search:{request_hash}"
        with self._lock:
            self.owner_guard()
            with self.daemon.transaction() as conn:
                existing = self._completion_in_txn(conn, _cnum(cyc.cycle_id))
                active = []
                if existing is None:
                    active = conn.execute(
                        "SELECT id,purpose,status,transcript_ref FROM runner_call "
                        "WHERE cycle_id=? AND phase='import_search' "
                        "AND status IN ('created','running') ORDER BY id",
                        (_cnum(cyc.cycle_id),)).fetchall()
                    request_purposes = conn.execute(
                        "SELECT DISTINCT purpose FROM runner_call "
                        "WHERE cycle_id=? AND phase='import_search' ORDER BY purpose",
                        (_cnum(cyc.cycle_id),)).fetchall()
                    if any(row[0] != purpose for row in request_purposes):
                        raise ImportSearchError(
                            f"cycle {cyc.cycle_id} 已绑定不同 import_search 请求")
                    if len(active) > 1:
                        raise ImportSearchError(
                            f"cycle {cyc.cycle_id} 存在多个 active import_search intent")
            if existing is not None:
                return self._verify_completion(
                    cyc, request, request_hash, existing)
            if active:
                runner_call_id, active_purpose, status, transcript_ref = active[0]
                receipt_path = self._receipt_path(
                    cyc.cycle_id, request_hash, runner_call_id)
                if active_purpose != purpose or transcript_ref != str(receipt_path):
                    raise ImportSearchError(
                        f"cycle {cyc.cycle_id} 已有不同 import_search intent")
                if status == "created":
                    self._finish_failed(
                        runner_call_id, failure_kind="orphaned_unstarted_search")
                elif receipt_path.exists():
                    try:
                        return self._finalize(
                            cyc=cyc, request=request, request_hash=request_hash,
                            runner_call_id=runner_call_id, receipt_path=receipt_path)
                    except _TriggerChanged:
                        self._finish_failed(
                            runner_call_id, failure_kind="trigger_changed")
                    # Every other finalize error means the durable receipt/DB
                    # contract is corrupt or an implementation invariant broke.
                    # Keep running+receipt unresolved and fail loud on every
                    # restart; converting it to a research failure would hide
                    # corruption and issue another non-deterministic GET.
                else:
                    # The old process may have died before or during a GET.
                    # Repeating a bounded read is safe; no DB facts exist yet.
                    self._finish_failed(
                        runner_call_id, failure_kind="orphaned_readonly_search")

            self.owner_guard()
            with self.daemon.transaction() as conn:
                existing = self._completion_in_txn(conn, _cnum(cyc.cycle_id))
                if existing is None:
                    context = self._trigger_context_in_txn(conn, cyc, request)
                    if self.cost_ledger is not None:
                        blocked = self.cost_ledger.new_external_call_block_reason(conn)
                        if blocked is not None:
                            raise ImportSearchError(
                                f"import_search 被 durable 成本闸阻断: {blocked}")
                    runner_call_id = conn.execute(
                        "INSERT INTO runner_call(cycle_id,phase,purpose,status,transcript_ref) "
                        "VALUES (?,'import_search',?,'created',NULL)",
                        (_cnum(cyc.cycle_id), purpose)).lastrowid
                    receipt_path = self._receipt_path(
                        cyc.cycle_id, request_hash, runner_call_id)
                    changed = conn.execute(
                        "UPDATE runner_call SET transcript_ref=? "
                        "WHERE id=? AND status='created'",
                        (str(receipt_path), runner_call_id)).rowcount
                    if changed != 1:
                        raise ImportSearchError(
                            f"runner_call {runner_call_id} 无法绑定 import_search 回执")
            if existing is not None:
                return self._verify_completion(
                    cyc, request, request_hash, existing)
            with self.daemon.transaction() as conn:
                changed = conn.execute(
                    "UPDATE runner_call SET status='running',started_at=CURRENT_TIMESTAMP "
                    "WHERE id=? AND status='created'", (runner_call_id,)).rowcount
                if changed != 1:
                    raise ImportSearchError(
                        f"runner_call {runner_call_id} 无法开始 import_search")

            started = time.monotonic()
            try:
                provider_result = self.provider.search(
                    query=request["query"],
                    max_candidates=context["retrieval"]["candidate_limit"])
                result = self._validate_result(
                    provider_result, provider=self.provider.name,
                    query=request["query"],
                    limit=context["retrieval"]["candidate_limit"])
                wallclock = time.monotonic() - started
                self.owner_guard()
                receipt = {
                    "protocol": _PROTOCOL, "version": 1,
                    "runner_call_id": runner_call_id,
                    "cycle_id": cyc.cycle_id, "question_id": cyc.question_id,
                    "request": request, "request_hash": request_hash,
                    "trigger_snapshot_hash": context["trigger_snapshot_hash"],
                    "policy_hash": context["policy_hash"],
                    "provider": self.provider.name,
                    "retrieval": context["retrieval"],
                    "wallclock_sec": wallclock,
                    "result": result, "result_hash": _hash(result),
                }
                atomic_write_receipt(receipt_path, receipt)
            except Exception as error:
                try:
                    self._finish_failed(
                        runner_call_id,
                        failure_kind=("provider_error" if isinstance(
                            error, ImportSearchProviderError) else "search_postprocess_error"),
                        wallclock_sec=time.monotonic() - started)
                except Exception as finish_error:
                    add_note = getattr(error, "add_note", None)
                    if callable(add_note):
                        add_note(
                            "import_search 失败收口也失败: "
                            f"{type(finish_error).__name__}: {finish_error}")
                raise
            self._after_receipt()
            return self._finalize(
                cyc=cyc, request=request, request_hash=request_hash,
                runner_call_id=runner_call_id, receipt_path=receipt_path)
