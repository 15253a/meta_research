"""Trusted handling for ``human_named``, ``stuck`` and ``sota_reference``.

These paths deliberately do not share the default ``new_structure`` semantics:

* ``human_named`` may register candidates on the human-created question, but
  only when an exact consumed directive authority is present.
* ``stuck`` performs one read-only survey and, when it finds anything, creates
  a *new* reference question.  The original question receives only a question
  dependency and never gets a candidate or ``import_defer``.
* ``sota_reference`` first freezes a bounded paper/benchmark body, then creates
  the same kind of independent reference question.  "latest" is never an
  authority.

Survey results are held by a strict append-only authority decision.  When the
new question gets its own action cycle it activates those frozen results into
that cycle's candidate set without another network call.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
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
from .import_authority import (
    ImportAuthorityError,
    authority_hash,
    build_reference_authority,
    canonical_bytes,
    load_question_import_authority,
)
from .import_search import (
    ImportSearchError,
    ImportSearchProviderError,
    ImportSearchService,
    validate_import_search_request,
)
from .importer import DeferredImporter
from .interfaces import CallUsage
from .phase_commit import check_or_record
from .process_supervisor import atomic_write_receipt, read_receipt
from .question_progress import QuestionProgressError, load_inconclusive_streak


_PROTOCOL = "import-trigger-v1"
_ACTIVATION_PROTOCOL = "import-source-activation-v1"
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _canonical_text(value: Any) -> str:
    return canonical_bytes(value).decode("utf-8")


def _hash(value: Any) -> str:
    return authority_hash(value)


def _bytes_hash(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _bounded_text(value: Any, *, field: str, max_bytes: int) -> str:
    if not isinstance(value, str) or not value:
        raise ImportSearchError(f"{field} 须为非空字符串")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise ImportSearchError(f"{field} 非合法 UTF-8") from error
    if size > max_bytes:
        raise ImportSearchError(f"{field} 超过 {max_bytes} bytes")
    if any((ord(ch) < 0x20 and ch not in "\n\r\t") or ord(ch) == 0x7F
           for ch in value):
        raise ImportSearchError(f"{field} 含非法控制字符")
    return value


class _AllowedReferenceRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts):  # noqa: ANN001
        super().__init__()
        self.allowed_hosts = frozenset(host.lower() for host in allowed_hosts)

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        parsed = urllib.parse.urlsplit(newurl)
        try:
            port = parsed.port
        except ValueError as error:
            raise urllib.error.HTTPError(
                req.full_url, code, "reference redirect port invalid", headers, fp
            ) from error
        if (parsed.scheme != "https" or not parsed.hostname
                or parsed.hostname.lower() not in self.allowed_hosts
                or parsed.username or parsed.password or port not in (None, 443)
                or parsed.fragment):
            raise urllib.error.HTTPError(
                req.full_url, code, "reference redirect escaped allowlist",
                headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class BoundedReferenceSnapshotProvider:
    """Fetch an allowlisted HTTPS paper/benchmark as bounded immutable bytes."""

    name = "bounded_https_v1"

    def __init__(self, config: Mapping[str, Any], *, opener=None):
        self.allowed_hosts = frozenset(
            str(host).lower() for host in config["allowed_hosts"])
        if not self.allowed_hosts:
            raise ValueError("reference snapshot allowed_hosts 不能为空")
        self.timeout_s = float(config["timeout_s"])
        self.max_response_bytes = int(config["max_response_bytes"])
        self.opener = opener or urllib.request.build_opener(
            _AllowedReferenceRedirect(self.allowed_hosts)).open

    def _validate_uri(self, uri: Any) -> str:
        uri = _bounded_text(uri, field="reference uri", max_bytes=2048)
        parsed = urllib.parse.urlsplit(uri)
        try:
            port = parsed.port
        except ValueError as error:
            raise ImportSearchProviderError("reference URI port 非法") from error
        if (parsed.scheme != "https" or not parsed.hostname
                or parsed.hostname.lower() not in self.allowed_hosts
                or parsed.username or parsed.password or port not in (None, 443)
                or parsed.fragment):
            raise ImportSearchProviderError(
                "reference URI 必须是 policy allowlist 内无凭据/fragment 的 HTTPS URL")
        return uri

    def fetch(self, reference: Mapping[str, Any]) -> Dict[str, Any]:
        if (not isinstance(reference, Mapping)
                or set(reference) != {"kind", "uri"}
                or reference.get("kind") not in ("paper", "benchmark")):
            raise ImportSearchProviderError("reference request 结构非法")
        requested_uri = self._validate_uri(reference["uri"])
        request = urllib.request.Request(
            requested_uri,
            headers={
                "Accept": "application/pdf,text/html,application/json,text/plain;q=0.8,*/*;q=0.1",
                "User-Agent": "meta-research-reference-snapshot/1",
            },
            method="GET")
        try:
            response = self.opener(request, timeout=self.timeout_s)
        except urllib.error.HTTPError as error:
            try:
                raise ImportSearchProviderError(
                    f"reference snapshot HTTP {error.code}") from error
            finally:
                error.close()
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise ImportSearchProviderError(
                f"reference snapshot 只读调用失败: {type(error).__name__}") from error
        try:
            final_uri = self._validate_uri(response.geturl())
            declared = response.headers.get("Content-Length")
            if declared is not None:
                try:
                    declared_size = int(declared)
                except ValueError as error:
                    raise ImportSearchProviderError(
                        "reference Content-Length 非整数") from error
                if declared_size < 0 or declared_size > self.max_response_bytes:
                    raise ImportSearchProviderError("reference body 超过字节上限")
            raw = response.read(self.max_response_bytes + 1)
            if len(raw) > self.max_response_bytes:
                raise ImportSearchProviderError("reference body 超过字节上限")
            content_type = response.headers.get("Content-Type", "application/octet-stream")
            if not isinstance(content_type, str):
                content_type = "application/octet-stream"
            content_type = content_type.split(";", 1)[0].strip().lower()
            if (not content_type or len(content_type.encode("ascii", "ignore")) > 128
                    or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in content_type)):
                raise ImportSearchProviderError("reference Content-Type 非法")
        finally:
            response.close()
        if not raw:
            raise ImportSearchProviderError("reference body 为空")
        return {
            "metadata": {
                "provider": self.name,
                "kind": reference["kind"],
                "requested_uri": requested_uri,
                "final_uri": final_uri,
                "retrieved_at": datetime.now(timezone.utc).isoformat(
                    timespec="microseconds"),
                "content_type": content_type,
                "content_sha256": _bytes_hash(raw),
                "bytes": len(raw),
            },
            "content": raw,
        }


def _atomic_write_blob(path: Path, raw: bytes) -> None:
    """Write private content-addressed source bytes and fsync file + parent."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}-{threading.get_ident()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("reference blob short write")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(tmp, path)
        dfd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


class ImportTriggerRouter:
    """Route the one plan control sidecar to its mechanically correct gate."""

    def __init__(self, *, new_structure, trusted_triggers):
        self.new_structure = new_structure
        self.trusted_triggers = trusted_triggers

    def __call__(self, cyc, request: Dict[str, Any], pack=None) -> Dict[str, Any]:
        request = validate_import_search_request(request)
        if request["trigger_kind"] == "new_structure":
            return self.new_structure(cyc, request, pack)
        return self.trusted_triggers(cyc, request, pack)


class TrustedImportTriggerService:
    """Durable service for the three source/status-gated import paths."""

    def __init__(self, *, daemon, policy: Dict[str, Any], repo_provider,
                 reference_provider, work_root: str, cost_ledger=None,
                 owner_guard=None):
        self.daemon = daemon
        self.policy = policy
        self.search_config = policy["import_search"]
        self.reference_config = policy["import_reference"]
        self.repo_provider = repo_provider
        self.reference_provider = reference_provider
        self.work = Path(work_root)
        self.cost_ledger = cost_ledger
        self.owner_guard = owner_guard or (lambda: None)
        if getattr(repo_provider, "name", None) != self.search_config["provider"]:
            raise ValueError("trusted trigger repo provider 与 policy 身份不一致")
        expected_reference = self.reference_config["reference_snapshot"]["provider"]
        if getattr(reference_provider, "name", None) != expected_reference:
            raise ValueError("reference snapshot provider 与 policy 身份不一致")
        if self.reference_config.get("max_child_questions_per_survey") != 1:
            raise ValueError("当前 import trigger 协议要求每次 survey 恰最多一个 child")
        self._lock = threading.Lock()

    def _retrieval(self, conn, *, score: Any, est_cost: Any) -> Dict[str, Any]:
        def finite(value: Any, *, field: str) -> float:
            if value is None:
                return 0.0
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ImportSearchError(f"{field} 非有限数字")
            result = float(value)
            if not math.isfinite(result):
                raise ImportSearchError(f"{field} 非有限数字")
            return result

        score_f = finite(score, field="question.score")
        cost_f = max(0.0, finite(est_cost, field="question.est_cost"))
        budget = compute_budget(conn, self.policy["budget"])
        ratio = cost_f / budget if budget > 0 else 0.0
        thresholds = self.policy["retrieval"]["scale_thresholds"]
        if (ratio >= float(thresholds["large"]["est_cost_ratio"])
                or score_f >= float(thresholds["large"]["score"])):
            scale = "large"
        elif (ratio >= float(thresholds["medium"]["est_cost_ratio"])
                or score_f >= float(thresholds["medium"]["score"])):
            scale = "medium"
        else:
            scale = "small"
        limit = min(
            int(self.search_config["max_candidates"]),
            int(self.policy["retrieval"]["budget_by_question_scale"][scale]))
        if limit <= 0:
            raise ImportSearchError(
                f"retrieval scale={scale} 的 import trigger 预算为 0")
        return {
            "scale": scale, "candidate_limit": limit, "B_t": budget,
            "recipe": self.policy["retrieval"]["recipe"],
            "score": score_f, "est_cost": cost_f,
        }

    def _context_in_txn(self, conn, cyc, request: Dict[str, Any]) -> Dict[str, Any]:
        ci, qi = _cnum(cyc.cycle_id), _qnum(cyc.question_id)
        row = conn.execute(
            "SELECT c.goal_id,c.goal_ver,c.status,c.route,c.active_question_id,"
            "q.goal_id,q.goal_ver,q.status,q.active_cycle,q.text,q.score,q.est_cost,"
            "q.visit_count,q.parent_id,g.text,g.predicate_json,"
            "(SELECT MAX(version) FROM goal WHERE id=c.goal_id) "
            "FROM cycle c JOIN question q ON q.id=? "
            "JOIN goal g ON g.id=c.goal_id AND g.version=c.goal_ver WHERE c.id=?",
            (qi, ci)).fetchone()
        if (row is None or row[2] != "idea" or row[3] != "attack"
                or row[4] != qi or tuple(row[:2]) != tuple(row[5:7])
                or row[7] != "active" or row[8] != ci or row[16] != row[1]):
            raise ImportSearchError(
                f"trusted import trigger 只允许 current plan exact active 边界: "
                f"{cyc.cycle_id}/{cyc.question_id}")
        ideas = conn.execute(
            "SELECT id,content_md,audit_json FROM idea "
            "WHERE cycle_id=? AND status='selected' ORDER BY id", (ci,)).fetchall()
        if len(ideas) != 1:
            raise ImportSearchError(
                f"trusted import trigger 要求恰一 selected idea，实收 {len(ideas)}")
        if conn.execute(
                "SELECT 1 FROM external_candidate WHERE question_id=? "
                "AND discovered_cycle=? LIMIT 1", (qi, ci)).fetchone() is not None:
            raise ImportSearchError("当前 action-cycle 已有 candidate，不得再触发来源")

        try:
            source_authority = load_question_import_authority(
                conn, question_id=qi)
        except ImportAuthorityError as error:
            raise ImportSearchError(str(error)) from error
        trigger_kind = request["trigger_kind"]
        activation = "source_authority_hash" in request
        if activation:
            if source_authority is None:
                raise ImportSearchError(
                    f"{trigger_kind} 请求缺 durable source authority")
            if (source_authority["trigger_kind"] != trigger_kind
                    or source_authority["authority_hash"]
                    != request["source_authority_hash"]):
                raise ImportSearchError("请求与当前问题 source authority 不一致")
            if request["need_summary"] != source_authority["need_summary"]:
                raise ImportSearchError("请求 need_summary 与冻结 authority 不一致")
        elif source_authority is not None:
            raise ImportSearchError(
                "当前问题已有冻结 source authority，只允许按 hash 激活，不得重新搜索")

        if trigger_kind == "human_named" and not activation:
            raise ImportSearchError("human_named 只能激活已确认 directive authority")
        stuck_progress = None
        if trigger_kind == "stuck" and not activation:
            thresholds = self.policy["retrieval"]["gate2_stuck_threshold"]
            visit_threshold = int(thresholds["visit_count"])
            inconclusive_threshold = int(thresholds["consecutive_inconclusive"])
            try:
                stuck_progress = load_inconclusive_streak(
                    conn, question_id=qi)
            except QuestionProgressError as error:
                raise ImportSearchError(
                    f"question q{qi} inconclusive 账本损坏: {error}"
                ) from error
            if (stuck_progress["visit_count"] != row[12]
                    or stuck_progress["visit_count"] < visit_threshold
                    or stuck_progress["consecutive_inconclusive"]
                    < inconclusive_threshold):
                raise ImportSearchError(
                    "stuck survey 未达到 visit/consecutive_inconclusive 双阈值")
            prior = conn.execute(
                "SELECT id,payload_json FROM decision WHERE question_id=? "
                "AND actor='orchestrator' AND type='import_trigger_completed' "
                "ORDER BY id", (qi,)).fetchall()
            for decision_id, payload_raw in prior:
                try:
                    payload = json.loads(payload_raw)
                except json.JSONDecodeError as error:
                    raise ImportSearchError(
                        f"import trigger decision {decision_id} 损坏") from error
                if payload.get("trigger_kind") == "stuck":
                    raise ImportSearchError("同一原问题只允许一次 stuck 外部普查")
        if trigger_kind in ("stuck", "sota_reference") and not activation:
            # Capacity is a normal request-admission fact, not a receipt/DB
            # corruption.  Reject it before creating a runner/network intent;
            # finalization repeats the check for TOCTOU.
            self._tree_capacity(conn, parent_id=qi)
        retrieval = self._retrieval(conn, score=row[10], est_cost=row[11])
        policy_hash = DeferredImporter.policy_hash(self.policy)
        context = {
            "version": 1,
            "cycle": {
                "cycle_id": cyc.cycle_id, "question_id": cyc.question_id,
                "goal_id": row[0], "goal_ver": row[1],
                "status": row[2], "route": row[3],
            },
            "question": {
                "text_hash": _bytes_hash(row[9].encode("utf-8")),
                "score": retrieval["score"],
                "est_cost": retrieval["est_cost"],
                "visit_count": row[12],
                "parent_question_id": row[13],
            },
            "goal": {
                "text_hash": _bytes_hash(row[14].encode("utf-8")),
                "predicate_hash": _bytes_hash(row[15].encode("utf-8")),
            },
            "selected_idea": {
                "idea_id": ideas[0][0],
                "content_hash": _bytes_hash(ideas[0][1].encode("utf-8")),
                "audit_hash": (_bytes_hash(ideas[0][2].encode("utf-8"))
                               if ideas[0][2] is not None else None),
            },
            "request": request,
            "source_authority_hash": (
                source_authority["authority_hash"]
                if source_authority is not None else None),
            "policy_hash": policy_hash,
            "retrieval": {
                key: retrieval[key]
                for key in ("scale", "candidate_limit", "B_t", "recipe")
            },
        }
        if trigger_kind == "stuck" and not activation:
            context["stuck_state"] = {
                "visit_count": stuck_progress["visit_count"],
                "consecutive_inconclusive":
                    stuck_progress["consecutive_inconclusive"],
                "decision_ids": stuck_progress["decision_ids"],
                "threshold": dict(
                    self.policy["retrieval"]["gate2_stuck_threshold"]),
            }
        context["trigger_context_hash"] = _hash(context)
        context["authority"] = source_authority
        return context

    def _receipt_path(self, cycle_id: str, request_hash: str,
                      runner_call_id: int) -> Path:
        return (self.work / "state" / "import-trigger" / cycle_id /
                request_hash.removeprefix("sha256:") /
                f"call-{runner_call_id}.json")

    def _blob_path(self, content_sha256: str) -> Path:
        if (not isinstance(content_sha256, str)
                or not _SHA256_RE.fullmatch(content_sha256)):
            raise ImportSearchError("reference content_sha256 非法")
        digest = content_sha256.removeprefix("sha256:")
        return (self.work / "state" / "import-trigger" / "source-blobs" /
                "sha256" / digest[:2] / f"{digest}.bin")

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
                        f"runner_call {runner_call_id} 无法记录 import trigger 失败")

    @staticmethod
    def _completion_in_txn(conn, cycle_id: int) -> Optional[Dict[str, Any]]:
        rows = conn.execute(
            "SELECT id,payload_json FROM decision WHERE cycle_id=? "
            "AND actor='orchestrator' AND type='import_trigger_completed' ORDER BY id",
            (cycle_id,)).fetchall()
        if len(rows) > 1:
            raise ImportSearchError(
                f"cycle c{cycle_id} 存在多个 import_trigger_completed")
        if not rows:
            return None
        try:
            payload = json.loads(rows[0][1])
        except json.JSONDecodeError as error:
            raise ImportSearchError("import_trigger_completed payload 损坏") from error
        required = {
            "protocol", "trigger_kind", "request", "request_hash",
            "trigger_context_hash", "policy_hash", "runner_call_id",
            "receipt_ref", "result_hash", "candidate_count", "skipped_count",
            "candidate_ids", "license_review_ids", "child_question_id",
            "source_authority_hash", "terminalized", "reference_snapshot",
        }
        if (not isinstance(payload, dict) or set(payload) != required
                or payload.get("protocol") != _PROTOCOL
                or payload.get("trigger_kind") not in (
                    "human_named", "stuck", "sota_reference")
                or payload.get("request_hash") != _hash(payload.get("request"))
                or not isinstance(payload.get("request_hash"), str)
                or not _SHA256_RE.fullmatch(payload["request_hash"])
                or not isinstance(payload.get("trigger_context_hash"), str)
                or not _SHA256_RE.fullmatch(payload["trigger_context_hash"])
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
                or any(isinstance(item, bool) or not isinstance(item, int) or item <= 0
                       for item in (payload["candidate_ids"]
                                    + payload["license_review_ids"]))
                or len(set(payload["candidate_ids"])) != len(payload["candidate_ids"])
                or len(set(payload["license_review_ids"]))
                != len(payload["license_review_ids"])
                or isinstance(payload.get("candidate_count"), bool)
                or not isinstance(payload.get("candidate_count"), int)
                or payload["candidate_count"] != len(payload["candidate_ids"])
                or payload["candidate_count"] != len(payload["license_review_ids"])
                or isinstance(payload.get("skipped_count"), bool)
                or not isinstance(payload.get("skipped_count"), int)
                or payload["skipped_count"] < 0
                or (payload.get("child_question_id") is not None and (
                    isinstance(payload["child_question_id"], bool)
                    or not isinstance(payload["child_question_id"], int)
                    or payload["child_question_id"] <= 0))
                or (payload.get("source_authority_hash") is not None and (
                    not isinstance(payload["source_authority_hash"], str)
                    or not _SHA256_RE.fullmatch(payload["source_authority_hash"])))
                or not isinstance(payload.get("terminalized"), bool)
                or payload["terminalized"]
                != (payload["child_question_id"] is not None)
                or (payload["terminalized"] and payload["candidate_count"] != 0)
                or (payload["child_question_id"] is not None
                    and payload["source_authority_hash"] is None)
                or (payload["trigger_kind"] == "human_named" and (
                    payload["terminalized"] or payload["source_authority_hash"] is None
                    or payload["candidate_count"] != 1))
                or ((payload["trigger_kind"] == "sota_reference")
                    != (payload["reference_snapshot"] is not None))):
            raise ImportSearchError("import_trigger_completed 协议非法")
        return payload

    def _validate_reference_snapshot(
            self, value: Any) -> Optional[Dict[str, Any]]:
        if value is None:
            return None
        keys = {
            "provider", "kind", "requested_uri", "final_uri", "retrieved_at",
            "content_type", "content_sha256", "bytes", "blob_ref",
        }
        if not isinstance(value, dict) or set(value) != keys:
            raise ImportSearchError("reference snapshot 结构非法")
        if (value["provider"] != self.reference_provider.name
                or value["kind"] not in ("paper", "benchmark")
                or not isinstance(value["content_sha256"], str)
                or not _SHA256_RE.fullmatch(value["content_sha256"])
                or isinstance(value["bytes"], bool)
                or not isinstance(value["bytes"], int) or value["bytes"] <= 0
                or value["bytes"] > int(
                    self.reference_config["reference_snapshot"]["max_response_bytes"])
                or not isinstance(value["blob_ref"], str) or not value["blob_ref"]):
            raise ImportSearchError("reference snapshot 身份/hash 非法")
        expected_blob_ref = str(self._blob_path(value["content_sha256"]))
        if value["blob_ref"] != expected_blob_ref:
            raise ImportSearchError("reference snapshot blob_ref 非规范路径")
        # Re-run the provider's URL boundary without issuing a request.
        self.reference_provider._validate_uri(value["requested_uri"])
        self.reference_provider._validate_uri(value["final_uri"])
        try:
            parsed_time = datetime.fromisoformat(value["retrieved_at"])
        except (TypeError, ValueError) as error:
            raise ImportSearchError("reference snapshot retrieved_at 非 ISO-8601") from error
        if parsed_time.tzinfo is None:
            raise ImportSearchError("reference snapshot retrieved_at 缺时区")
        blob_path = Path(value["blob_ref"])
        try:
            info = blob_path.lstat()
        except OSError as error:
            raise ImportSearchError("reference snapshot blob 不可读") from error
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_size != value["bytes"]):
            raise ImportSearchError("reference snapshot blob 类型/size 不一致")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(blob_path, flags)
        except OSError as error:
            raise ImportSearchError("reference snapshot blob 无法安全打开") from error
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino, opened.st_size) != (
                    info.st_dev, info.st_ino, value["bytes"]):
                raise ImportSearchError("reference snapshot blob open 前后身份漂移")
            chunks = []
            remaining = value["bytes"] + 1
            while remaining > 0:
                chunk = os.read(fd, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
        finally:
            os.close(fd)
        if len(raw) != value["bytes"] or _bytes_hash(raw) != value["content_sha256"]:
            raise ImportSearchError("reference snapshot blob bytes/hash 不一致")
        return dict(value)

    def _validate_receipt(self, receipt: Any, *, runner_call_id: int,
                          cyc, request: Dict[str, Any], request_hash: str) -> Dict[str, Any]:
        keys = {
            "protocol", "version", "runner_call_id", "cycle_id", "question_id",
            "request", "request_hash", "trigger_context_hash", "policy_hash",
            "repo_provider", "retrieval", "wallclock_sec", "result",
            "result_hash", "reference_snapshot",
        }
        if not isinstance(receipt, dict) or set(receipt) != keys:
            raise ImportSearchError("import trigger receipt 结构非法")
        if (receipt["protocol"] != _PROTOCOL
                or isinstance(receipt["version"], bool) or receipt["version"] != 1
                or receipt["runner_call_id"] != runner_call_id
                or receipt["cycle_id"] != cyc.cycle_id
                or receipt["question_id"] != cyc.question_id
                or receipt["request"] != request
                or receipt["request_hash"] != request_hash
                or receipt["repo_provider"] != self.repo_provider.name
                or receipt["result_hash"] != _hash(receipt["result"])
                or not isinstance(receipt["trigger_context_hash"], str)
                or not _SHA256_RE.fullmatch(receipt["trigger_context_hash"])
                or not isinstance(receipt["policy_hash"], str)
                or not _SHA256_RE.fullmatch(receipt["policy_hash"])):
            raise ImportSearchError("import trigger receipt 身份/hash 不一致")
        wallclock = receipt["wallclock_sec"]
        if (isinstance(wallclock, bool) or not isinstance(wallclock, (int, float))
                or not math.isfinite(float(wallclock)) or wallclock < 0):
            raise ImportSearchError("import trigger receipt wallclock 非法")
        retrieval = receipt["retrieval"]
        if not isinstance(retrieval, dict) or set(retrieval) != {
                "scale", "candidate_limit", "B_t", "recipe"}:
            raise ImportSearchError("import trigger retrieval 结构非法")
        result = ImportSearchService._validate_result(
            receipt["result"], provider=self.repo_provider.name,
            query=(request.get("query") or self._human_query_from_request(request, cyc)),
            limit=retrieval["candidate_limit"])
        expected_receipt = self._receipt_path(
            cyc.cycle_id, request_hash, runner_call_id)
        reference_snapshot = self._validate_reference_snapshot(
            receipt["reference_snapshot"])
        if ((request["trigger_kind"] == "sota_reference"
             and "source_authority_hash" not in request)
                != (reference_snapshot is not None)):
            raise ImportSearchError("sota discovery 与 reference snapshot 存在性矛盾")
        return {**receipt, "result": result,
                "reference_snapshot": reference_snapshot}

    def _human_query_from_request(self, request: Dict[str, Any], cyc) -> str:
        authority = load_question_import_authority(
            self.daemon.conn, question_id=_qnum(cyc.question_id))
        if authority is None or authority["trigger_kind"] != "human_named":
            raise ImportSearchError("human_named query 缺 authority")
        return self._human_query(authority)

    @staticmethod
    def _human_query(authority: Dict[str, Any]) -> str:
        suffix = authority["requested_revision"] or "default"
        return f"human_named:{authority['canonical_uri']}@{suffix}"

    def _register_candidates(self, conn, *, question_id: str, cycle_id: str,
                             trigger_kind: str, trigger_snapshot_hash: str,
                             need_summary: str, result: Dict[str, Any],
                             retrieval: Dict[str, Any], policy_hash: str,
                             source_authority_hash: Optional[str]) -> tuple[list, list]:
        scope_json = _canonical_text(self.search_config["auto_license"]["scope"])
        allow_spdx = set(self.search_config["auto_license"]["allow_spdx"])
        candidate_ids = []
        review_ids = []
        for rank, candidate in enumerate(result["candidates"]):
            snapshot = {
                "version": 2,
                "provider": result["provider"], "query": result["query"],
                "provider_result_id": candidate["provider_result_id"],
                "retrieved_at": result["retrieved_at"],
                "ranking": {
                    "rank": rank, "recipe": retrieval["recipe"],
                    "scale": retrieval["scale"],
                },
                "repository": candidate["repository"],
                "canonical_uri": candidate["canonical_uri"],
                "revision": candidate["revision"],
                "license": candidate["license"],
                "policy_hash": policy_hash,
                "source_authority_hash": source_authority_hash,
            }
            snapshot_json = _canonical_text(snapshot)
            candidate_id = DeferredImporter.register_candidate_in_txn(
                conn, question_id=question_id, discovered_cycle=cycle_id,
                trigger_kind=trigger_kind,
                trigger_snapshot_hash=trigger_snapshot_hash,
                need_summary=need_summary, source_kind="repo",
                canonical_uri=candidate["canonical_uri"],
                revision=candidate["revision"],
                license_id_seen=candidate["license"]["spdx_id"],
                search_provider=result["provider"],
                search_query=result["query"],
                search_snapshot_json=snapshot_json,
                search_snapshot_hash=_bytes_hash(snapshot_json.encode("utf-8")),
                rank=rank, retrieved_at=result["retrieved_at"])
            decision = ("allow" if candidate["license"]["spdx_id"] in allow_spdx
                        else "review")
            review_id = DeferredImporter.review_license_in_txn(
                conn, candidate_id=candidate_id, decision=decision, actor="auto",
                license_scope_json=(scope_json if decision == "allow" else None),
                decided_cycle=cycle_id, policy_hash=policy_hash,
                license_id=candidate["license"]["spdx_id"],
                evidence_ref=candidate["license"]["evidence_ref"])
            candidate_ids.append(candidate_id)
            review_ids.append(review_id)
        return candidate_ids, review_ids

    def _finish_runner_in_txn(self, conn, *, runner_call_id: int,
                              receipt_path: Path, wallclock_sec: float):
        budget_hit = None
        usage = CallUsage(tokens_known=True, wallclock_sec=float(wallclock_sec))
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
                    f"runner_call {runner_call_id} import trigger 无法收口")
        return budget_hit

    def _tree_capacity(self, conn, *, parent_id: int) -> None:
        guard = self.policy["tree_guard"]
        existing_children = conn.execute(
            "SELECT count(*) FROM question WHERE parent_id=?", (parent_id,)).fetchone()[0]
        if existing_children + 1 > int(guard["max_children_per_node"]):
            raise ImportSearchError("import reference child 超出 max_children_per_node")
        depth = 0
        cursor = parent_id
        seen = set()
        while cursor is not None and cursor not in seen:
            seen.add(cursor)
            row = conn.execute(
                "SELECT parent_id FROM question WHERE id=?", (cursor,)).fetchone()
            if row is None:
                raise ImportSearchError("import reference parent lineage 损坏")
            cursor = row[0]
            if cursor is not None:
                depth += 1
        if depth + 1 > int(guard["max_decompose_depth"]):
            raise ImportSearchError("import reference child 超出 max_decompose_depth")
        open_count = conn.execute(
            "SELECT count(*) FROM question WHERE status IN ('open','inconclusive')"
        ).fetchone()[0]
        # current parent is active; terminalization releases it (+1) and adds
        # the reference child (+1).
        if open_count + 2 > int(guard["max_open_questions"]):
            raise ImportSearchError("import reference child 超出 max_open_questions")

    def _finalize_external(self, *, cyc, request: Dict[str, Any],
                           request_hash: str, runner_call_id: int,
                           receipt_path: Path) -> Dict[str, Any]:
        receipt = self._validate_receipt(
            read_receipt(receipt_path), runner_call_id=runner_call_id,
            cyc=cyc, request=request, request_hash=request_hash)
        result = receipt["result"]
        budget_hit = None
        with self.daemon.transaction() as conn:
            existing = self._completion_in_txn(conn, _cnum(cyc.cycle_id))
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise ImportSearchError(
                        f"cycle {cyc.cycle_id} 已完成不同 import trigger")
                return existing
            runner = conn.execute(
                "SELECT cycle_id,phase,purpose,status,transcript_ref FROM runner_call WHERE id=?",
                (runner_call_id,)).fetchone()
            purpose = f"import_trigger:{request_hash}"
            if (runner is None or runner != (
                    _cnum(cyc.cycle_id), "import_search", purpose,
                    "running", str(receipt_path))):
                raise ImportSearchError("import trigger runner/receipt 不一致")
            context = self._context_in_txn(conn, cyc, request)
            if (context["trigger_context_hash"] != receipt["trigger_context_hash"]
                    or context["policy_hash"] != receipt["policy_hash"]
                    or context["retrieval"] != receipt["retrieval"]):
                raise ImportSearchError("import trigger 外调期间 context/policy 已变")

            trigger_kind = request["trigger_kind"]
            candidate_ids = []
            review_ids = []
            child_question_id = None
            source_authority_hash = None
            terminalized = False
            authority = context["authority"]
            if trigger_kind == "human_named":
                if len(result["candidates"]) != 1 or result["skipped"]:
                    raise ImportSearchError("human_named direct resolve 须恰得一个候选")
                resolved = result["candidates"][0]
                if (resolved["canonical_uri"].lower()
                        != authority["canonical_uri"].lower()
                        or (authority["requested_revision"] is not None
                            and resolved["revision"]
                            != authority["requested_revision"])):
                    raise ImportSearchError(
                        "human_named direct resolve 候选与 directive authority 不一致")
                candidate_ids, review_ids = self._register_candidates(
                    conn, question_id=cyc.question_id, cycle_id=cyc.cycle_id,
                    trigger_kind=trigger_kind,
                    trigger_snapshot_hash=context["trigger_context_hash"],
                    need_summary=request["need_summary"], result=result,
                    retrieval=context["retrieval"],
                    policy_hash=context["policy_hash"],
                    source_authority_hash=authority["authority_hash"])
                source_authority_hash = authority["authority_hash"]
            elif trigger_kind in ("stuck", "sota_reference"):
                should_spawn = (trigger_kind == "sota_reference"
                                or bool(result["candidates"]))
                if should_spawn:
                    qi, ci = _qnum(cyc.question_id), _cnum(cyc.cycle_id)
                    self._tree_capacity(conn, parent_id=qi)
                    cycle_row = conn.execute(
                        "SELECT goal_id,goal_ver,status,active_question_id FROM cycle WHERE id=?",
                        (ci,)).fetchone()
                    if cycle_row is None:
                        raise ImportSearchError("import reference origin cycle 不存在")
                    child_text = (
                        f"复现冻结的{('公认 SOTA' if trigger_kind == 'sota_reference' else '外部普查')}"
                        f"参照作为独立 baseline：{request['need_summary']}")
                    child_question_id = conn.execute(
                        "INSERT INTO question(parent_id,goal_id,goal_ver,born_goal_ver,text,status,source,born_cycle) "
                        "VALUES (?,?,?,?,?,'open','agent',?)",
                        (qi, cycle_row[0], cycle_row[1], cycle_row[1],
                         child_text, ci)).lastrowid
                    authority = build_reference_authority(
                        trigger_kind=trigger_kind,
                        origin_cycle_id=ci, origin_question_id=qi,
                        child_question_id=child_question_id,
                        goal_id=cycle_row[0], goal_ver=cycle_row[1],
                        request_hash=request_hash,
                        trigger_context_hash=context["trigger_context_hash"],
                        policy_hash=context["policy_hash"],
                        runner_call_id=runner_call_id,
                        receipt_ref=str(receipt_path),
                        result_hash=receipt["result_hash"],
                        need_summary=request["need_summary"],
                        reference_snapshot=receipt["reference_snapshot"])
                    source_authority_hash = authority["authority_hash"]
                    conn.execute(
                        "INSERT INTO decision(cycle_id,question_id,actor,type,payload_json) "
                        "VALUES (?,?,'orchestrator','import_reference_authority',?)",
                        (ci, child_question_id, _canonical_text(authority)))
                    conn.execute(
                        "INSERT INTO question_dep(question_id,dep_type,depends_on_question_id,status,created_cycle) "
                        "VALUES (?,'question',?,'pending',?)",
                        (qi, child_question_id, ci))
                    pc = check_or_record(
                        conn, cycle_id=cyc.cycle_id, stage="plan",
                        target_id=None, artifact_hash=request_hash)
                    if pc != "new":
                        raise ImportSearchError(
                            f"import reference plan phase_commit 非 new: {pc}")
                    released = conn.execute(
                        "UPDATE question SET status='open',active_cycle=NULL "
                        "WHERE id=? AND status='active' AND active_cycle=?",
                        (qi, ci)).rowcount
                    if released != 1:
                        raise ImportSearchError("import reference origin question 无法释放")
                    finished = conn.execute(
                        "UPDATE cycle SET active_question_id=NULL,next_question_id=?,"
                        "next_intent='attack',status='done',finished_at=CURRENT_TIMESTAMP "
                        "WHERE id=? AND status='idea' AND active_question_id=?",
                        (child_question_id, ci, qi)).rowcount
                    if finished != 1:
                        raise ImportSearchError("import reference origin cycle 无法原子收尾")
                    terminalized = True
            else:                              # defensive; validator is closed
                raise ImportSearchError(f"trusted trigger_kind 非法: {trigger_kind}")

            budget_hit = self._finish_runner_in_txn(
                conn, runner_call_id=runner_call_id,
                receipt_path=receipt_path,
                wallclock_sec=receipt["wallclock_sec"])
            payload = {
                "protocol": _PROTOCOL, "trigger_kind": trigger_kind,
                "request": request, "request_hash": request_hash,
                "trigger_context_hash": context["trigger_context_hash"],
                "policy_hash": context["policy_hash"],
                "runner_call_id": runner_call_id,
                "receipt_ref": str(receipt_path),
                "result_hash": receipt["result_hash"],
                "candidate_count": len(candidate_ids),
                "skipped_count": len(result["skipped"]),
                "candidate_ids": candidate_ids,
                "license_review_ids": review_ids,
                "child_question_id": child_question_id,
                "source_authority_hash": source_authority_hash,
                "terminalized": terminalized,
                "reference_snapshot": receipt["reference_snapshot"],
            }
            conn.execute(
                "INSERT INTO decision(cycle_id,question_id,actor,type,payload_json) "
                "VALUES (?,?,'orchestrator','import_trigger_completed',?)",
                (_cnum(cyc.cycle_id), _qnum(cyc.question_id),
                 _canonical_text(payload)))
        if budget_hit is not None:
            from .cost_ledger import BudgetExhausted
            raise BudgetExhausted(**budget_hit)
        return payload

    def _verify_existing_completion(self, *, cyc, request: Dict[str, Any],
                                    request_hash: str,
                                    completion: Dict[str, Any]) -> Dict[str, Any]:
        if (completion["request"] != request
                or completion["request_hash"] != request_hash):
            raise ImportSearchError(
                f"cycle {cyc.cycle_id} 只允许一个 trusted import trigger")
        receipt_path = self._receipt_path(
            cyc.cycle_id, request_hash, completion["runner_call_id"])
        if completion["receipt_ref"] != str(receipt_path):
            raise ImportSearchError("import trigger completion receipt_ref 非规范路径")
        receipt = self._validate_receipt(
            read_receipt(receipt_path),
            runner_call_id=completion["runner_call_id"], cyc=cyc,
            request=request, request_hash=request_hash)
        runner = self.daemon.query_one(
            "SELECT cycle_id,phase,purpose,status,transcript_ref FROM runner_call WHERE id=?",
            (completion["runner_call_id"],))
        if runner != (
                _cnum(cyc.cycle_id), "import_search",
                f"import_trigger:{request_hash}", "success", str(receipt_path)):
            raise ImportSearchError("import trigger completion 与 runner 不一致")
        if (receipt["result_hash"] != completion["result_hash"]
                or receipt["trigger_context_hash"]
                != completion["trigger_context_hash"]
                or receipt["reference_snapshot"]
                != completion["reference_snapshot"]):
            raise ImportSearchError("import trigger completion 与 receipt 不一致")
        candidate_rows = self.daemon.query(
            "SELECT id,trigger_kind,trigger_snapshot_hash FROM external_candidate "
            "WHERE question_id=? AND discovered_cycle=? ORDER BY id",
            (_qnum(cyc.question_id), _cnum(cyc.cycle_id)))
        if ([row[0] for row in candidate_rows] != completion["candidate_ids"]
                or any(row[1:] != (
                    completion["trigger_kind"], completion["trigger_context_hash"])
                       for row in candidate_rows)):
            raise ImportSearchError("import trigger completion 与 candidate 登记不一致")
        if completion["license_review_ids"]:
            marks = ",".join("?" for _ in completion["license_review_ids"])
            review_rows = self.daemon.query(
                "SELECT id,candidate_id,decided_cycle,policy_hash FROM license_review "
                f"WHERE id IN ({marks}) ORDER BY id",
                tuple(completion["license_review_ids"]))
        else:
            review_rows = []
        if ([row[0] for row in review_rows] != completion["license_review_ids"]
                or [row[1] for row in review_rows] != completion["candidate_ids"]
                or any(row[2:] != (
                    _cnum(cyc.cycle_id), completion["policy_hash"])
                       for row in review_rows)):
            raise ImportSearchError("import trigger completion 与 license 登记不一致")
        child_id = completion["child_question_id"]
        if child_id is not None:
            child = self.daemon.query_one(
                "SELECT parent_id,born_cycle FROM question WHERE id=?", (child_id,))
            dep = self.daemon.query_one(
                "SELECT status FROM question_dep WHERE question_id=? "
                "AND dep_type='question' AND depends_on_question_id=? AND created_cycle=?",
                (_qnum(cyc.question_id), child_id, _cnum(cyc.cycle_id)))
            cycle = self.daemon.query_one(
                "SELECT status,active_question_id,next_question_id,next_intent FROM cycle WHERE id=?",
                (_cnum(cyc.cycle_id),))
            phase_commit = self.daemon.query_one(
                "SELECT artifact_hash FROM phase_commit WHERE cycle_id=? "
                "AND stage='plan' AND target_id IS NULL",
                (_cnum(cyc.cycle_id),))
            try:
                authority = load_question_import_authority(
                    self.daemon.conn, question_id=child_id)
            except ImportAuthorityError as error:
                raise ImportSearchError(str(error)) from error
            if (child != (_qnum(cyc.question_id), _cnum(cyc.cycle_id))
                    or dep is None or dep[0] not in ("pending", "satisfied", "blocked")
                    or cycle != ("done", None, child_id, "attack")
                    or phase_commit != (completion["request_hash"],)
                    or authority is None
                    or authority["authority_hash"]
                    != completion["source_authority_hash"]):
                raise ImportSearchError("import trigger completion 与 reference child 不一致")
        return completion

    def _read_authority_receipt(self, authority: Dict[str, Any]) -> Dict[str, Any]:
        # Reconstruct the origin identity needed by the strict receipt parser.
        class _Origin:
            cycle_id = f"c{authority['origin_cycle_id']}"
            question_id = f"q{authority['origin_question_id']}"

        expected_receipt = self._receipt_path(
            _Origin.cycle_id, authority["request_hash"],
            authority["runner_call_id"])
        if authority["receipt_ref"] != str(expected_receipt):
            raise ImportSearchError("reference authority receipt_ref 非规范路径")
        receipt = read_receipt(expected_receipt)
        request = receipt.get("request") if isinstance(receipt, dict) else None
        if not isinstance(request, dict):
            raise ImportSearchError("reference authority receipt 缺 request")
        validated_request = validate_import_search_request(request)
        if (_hash(validated_request) != authority["request_hash"]
                or validated_request["trigger_kind"] != authority["trigger_kind"]):
            raise ImportSearchError("reference authority 与 origin request 不一致")
        validated = self._validate_receipt(
            receipt, runner_call_id=authority["runner_call_id"],
            cyc=_Origin(), request=validated_request,
            request_hash=authority["request_hash"])
        runner = self.daemon.query_one(
            "SELECT cycle_id,phase,purpose,status,transcript_ref FROM runner_call WHERE id=?",
            (authority["runner_call_id"],))
        if runner != (
                authority["origin_cycle_id"], "import_search",
                f"import_trigger:{authority['request_hash']}", "success",
                authority["receipt_ref"]):
            raise ImportSearchError("reference authority origin runner 不一致")
        if (validated["trigger_context_hash"] != authority["trigger_context_hash"]
                or validated["policy_hash"] != authority["policy_hash"]
                or validated["result_hash"] != authority["result_hash"]
                or validated["reference_snapshot"] != authority["reference_snapshot"]):
            raise ImportSearchError("reference authority 与 origin receipt 不一致")
        return validated

    def _verify_activation_payload(self, *, cyc, request_hash: str,
                                   payload: Any) -> Dict[str, Any]:
        keys = {
            "protocol", "trigger_kind", "request_hash",
            "source_authority_hash", "trigger_context_hash",
            "origin_result_hash", "origin_candidate_count", "candidate_ids",
            "license_review_ids", "candidate_count", "policy_hash",
            "terminalized",
        }
        if (not isinstance(payload, dict) or set(payload) != keys
                or payload.get("protocol") != _ACTIVATION_PROTOCOL
                or payload.get("request_hash") != request_hash
                or payload.get("trigger_kind") not in ("stuck", "sota_reference")
                or not isinstance(payload.get("source_authority_hash"), str)
                or not _SHA256_RE.fullmatch(payload["source_authority_hash"])
                or not isinstance(payload.get("trigger_context_hash"), str)
                or not _SHA256_RE.fullmatch(payload["trigger_context_hash"])
                or not isinstance(payload.get("origin_result_hash"), str)
                or not _SHA256_RE.fullmatch(payload["origin_result_hash"])
                or not isinstance(payload.get("policy_hash"), str)
                or not _SHA256_RE.fullmatch(payload["policy_hash"])
                or isinstance(payload.get("origin_candidate_count"), bool)
                or not isinstance(payload.get("origin_candidate_count"), int)
                or payload["origin_candidate_count"] < 0
                or not isinstance(payload.get("candidate_ids"), list)
                or not isinstance(payload.get("license_review_ids"), list)
                or any(isinstance(item, bool) or not isinstance(item, int) or item <= 0
                       for item in (payload["candidate_ids"]
                                    + payload["license_review_ids"]))
                or len(set(payload["candidate_ids"])) != len(payload["candidate_ids"])
                or len(set(payload["license_review_ids"]))
                != len(payload["license_review_ids"])
                or isinstance(payload.get("candidate_count"), bool)
                or payload["candidate_count"] != len(payload["candidate_ids"])
                or payload["candidate_count"] != len(payload["license_review_ids"])
                or payload["candidate_count"] > payload["origin_candidate_count"]
                or payload.get("terminalized") is not False):
            raise ImportSearchError("import_source_activated 协议非法")
        candidate_rows = self.daemon.query(
            "SELECT id,trigger_kind,trigger_snapshot_hash FROM external_candidate "
            "WHERE question_id=? AND discovered_cycle=? ORDER BY id",
            (_qnum(cyc.question_id), _cnum(cyc.cycle_id)))
        if ([row[0] for row in candidate_rows] != payload["candidate_ids"]
                or any(row[1:] != (
                    payload["trigger_kind"], payload["trigger_context_hash"])
                       for row in candidate_rows)):
            raise ImportSearchError("import_source_activated 与 candidate 不一致")
        if payload["license_review_ids"]:
            marks = ",".join("?" for _ in payload["license_review_ids"])
            reviews = self.daemon.query(
                "SELECT id,candidate_id,decided_cycle,policy_hash FROM license_review "
                f"WHERE id IN ({marks}) ORDER BY id",
                tuple(payload["license_review_ids"]))
        else:
            reviews = []
        if ([row[0] for row in reviews] != payload["license_review_ids"]
                or [row[1] for row in reviews] != payload["candidate_ids"]
                or any(row[2:] != (
                    _cnum(cyc.cycle_id), payload["policy_hash"])
                       for row in reviews)):
            raise ImportSearchError("import_source_activated 与 license review 不一致")
        try:
            authority = load_question_import_authority(
                self.daemon.conn, question_id=_qnum(cyc.question_id))
        except ImportAuthorityError as error:
            raise ImportSearchError(str(error)) from error
        if (authority is None
                or authority["authority_hash"] != payload["source_authority_hash"]
                or authority["result_hash"] != payload["origin_result_hash"]):
            raise ImportSearchError("import_source_activated 与 authority 不一致")
        return payload

    def _activate_reference(self, *, cyc, request: Dict[str, Any]) -> Dict[str, Any]:
        request_hash = _hash(request)
        with self.daemon.transaction() as conn:
            existing_rows = conn.execute(
                "SELECT id,payload_json FROM decision WHERE cycle_id=? "
                "AND actor='orchestrator' AND type='import_source_activated' ORDER BY id",
                (_cnum(cyc.cycle_id),)).fetchall()
            if len(existing_rows) > 1:
                raise ImportSearchError("同一 cycle 存在多个 import_source_activated")
            if existing_rows:
                try:
                    payload = json.loads(existing_rows[0][1])
                except json.JSONDecodeError as error:
                    raise ImportSearchError("import_source_activated payload 损坏") from error
                return self._verify_activation_payload(
                    cyc=cyc, request_hash=request_hash, payload=payload)
            context = self._context_in_txn(conn, cyc, request)
            authority = context["authority"]
        receipt = self._read_authority_receipt(authority)
        with self.daemon.transaction() as conn:
            existing_rows = conn.execute(
                "SELECT id,payload_json FROM decision WHERE cycle_id=? "
                "AND actor='orchestrator' AND type='import_source_activated' ORDER BY id",
                (_cnum(cyc.cycle_id),)).fetchall()
            if existing_rows:
                try:
                    payload = json.loads(existing_rows[0][1])
                except json.JSONDecodeError as error:
                    raise ImportSearchError("import_source_activated payload 损坏") from error
                return self._verify_activation_payload(
                    cyc=cyc, request_hash=request_hash, payload=payload)
            context = self._context_in_txn(conn, cyc, request)
            if context["authority"]["authority_hash"] != authority["authority_hash"]:
                raise ImportSearchError("激活期间 source authority 已变")
            activation_limit = int(context["retrieval"]["candidate_limit"])
            activation_result = {
                **receipt["result"],
                "candidates": list(receipt["result"]["candidates"][:activation_limit]),
            }
            candidate_ids, review_ids = self._register_candidates(
                conn, question_id=cyc.question_id, cycle_id=cyc.cycle_id,
                trigger_kind=request["trigger_kind"],
                trigger_snapshot_hash=context["trigger_context_hash"],
                need_summary=request["need_summary"], result=activation_result,
                retrieval=context["retrieval"],
                policy_hash=context["policy_hash"],
                source_authority_hash=authority["authority_hash"])
            payload = {
                "protocol": _ACTIVATION_PROTOCOL,
                "trigger_kind": request["trigger_kind"],
                "request_hash": request_hash,
                "source_authority_hash": authority["authority_hash"],
                "trigger_context_hash": context["trigger_context_hash"],
                "origin_result_hash": authority["result_hash"],
                "origin_candidate_count": len(receipt["result"]["candidates"]),
                "candidate_ids": candidate_ids,
                "license_review_ids": review_ids,
                "candidate_count": len(candidate_ids),
                "policy_hash": context["policy_hash"],
                "terminalized": False,
            }
            conn.execute(
                "INSERT INTO decision(cycle_id,question_id,actor,type,payload_json) "
                "VALUES (?,?,'orchestrator','import_source_activated',?)",
                (_cnum(cyc.cycle_id), _qnum(cyc.question_id),
                 _canonical_text(payload)))
            return payload

    def _after_receipt(self) -> None:
        """Crash-injection seam used by exact recovery tests."""

    def _external(self, *, cyc, request: Dict[str, Any]) -> Dict[str, Any]:
        request_hash = _hash(request)
        purpose = f"import_trigger:{request_hash}"
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
                    purposes = conn.execute(
                        "SELECT DISTINCT purpose FROM runner_call WHERE cycle_id=? "
                        "AND phase='import_search' AND purpose LIKE 'import_trigger:%' "
                        "ORDER BY purpose", (_cnum(cyc.cycle_id),)).fetchall()
                    if any(row[0] != purpose for row in purposes):
                        raise ImportSearchError(
                            f"cycle {cyc.cycle_id} 已绑定不同 import trigger 请求")
                    if len(active) > 1:
                        raise ImportSearchError(
                            f"cycle {cyc.cycle_id} 多个 active import trigger")
            if existing is not None:
                return self._verify_existing_completion(
                    cyc=cyc, request=request, request_hash=request_hash,
                    completion=existing)
            if active:
                runner_call_id, active_purpose, status, transcript_ref = active[0]
                receipt_path = self._receipt_path(
                    cyc.cycle_id, request_hash, runner_call_id)
                if active_purpose != purpose or transcript_ref != str(receipt_path):
                    raise ImportSearchError("cycle 已绑定不同 import trigger intent")
                if status == "created":
                    self._finish_failed(
                        runner_call_id, failure_kind="orphaned_unstarted_trigger")
                elif receipt_path.exists():
                    return self._finalize_external(
                        cyc=cyc, request=request, request_hash=request_hash,
                        runner_call_id=runner_call_id, receipt_path=receipt_path)
                else:
                    self._finish_failed(
                        runner_call_id, failure_kind="orphaned_readonly_trigger")

            with self.daemon.transaction() as conn:
                context = self._context_in_txn(conn, cyc, request)
                if self.cost_ledger is not None:
                    blocked = self.cost_ledger.new_external_call_block_reason(conn)
                    if blocked is not None:
                        raise ImportSearchError(
                            f"import trigger 被 durable 成本闸阻断: {blocked}")
                runner_call_id = conn.execute(
                    "INSERT INTO runner_call(cycle_id,phase,purpose,status,transcript_ref) "
                    "VALUES (?,'import_search',?,'created',NULL)",
                    (_cnum(cyc.cycle_id), purpose)).lastrowid
                receipt_path = self._receipt_path(
                    cyc.cycle_id, request_hash, runner_call_id)
                conn.execute(
                    "UPDATE runner_call SET transcript_ref=? WHERE id=? AND status='created'",
                    (str(receipt_path), runner_call_id))
            with self.daemon.transaction() as conn:
                changed = conn.execute(
                    "UPDATE runner_call SET status='running',started_at=CURRENT_TIMESTAMP "
                    "WHERE id=? AND status='created'", (runner_call_id,)).rowcount
                if changed != 1:
                    raise ImportSearchError("import trigger runner 无法开始")

            started = time.monotonic()
            try:
                reference_snapshot = None
                if request["trigger_kind"] == "sota_reference":
                    fetched = self.reference_provider.fetch(request["reference"])
                    metadata = fetched.get("metadata")
                    content = fetched.get("content")
                    if not isinstance(metadata, dict) or not isinstance(content, bytes):
                        raise ImportSearchProviderError(
                            "reference provider 返回结构非法")
                    content_sha256 = _bytes_hash(content)
                    if metadata.get("content_sha256") != content_sha256:
                        raise ImportSearchProviderError(
                            "reference provider content_sha256 与 bytes 不一致")
                    blob_path = self._blob_path(content_sha256)
                    _atomic_write_blob(blob_path, content)
                    reference_snapshot = {
                        **metadata, "blob_ref": str(blob_path),
                    }
                    self._validate_reference_snapshot(reference_snapshot)
                if request["trigger_kind"] == "human_named":
                    authority = context["authority"]
                    query = self._human_query(authority)
                    resolver = getattr(self.repo_provider, "resolve_repository", None)
                    if not callable(resolver):
                        raise ImportSearchProviderError(
                            "repo provider 未实现 human_named exact resolve")
                    raw_result = resolver(
                        canonical_uri=authority["canonical_uri"],
                        requested_revision=authority["requested_revision"],
                        query=query)
                else:
                    query = request["query"]
                    raw_result = self.repo_provider.search(
                        query=query,
                        max_candidates=context["retrieval"]["candidate_limit"])
                result = ImportSearchService._validate_result(
                    raw_result, provider=self.repo_provider.name,
                    query=query,
                    limit=context["retrieval"]["candidate_limit"])
                wallclock = time.monotonic() - started
                self.owner_guard()
                receipt = {
                    "protocol": _PROTOCOL, "version": 1,
                    "runner_call_id": runner_call_id,
                    "cycle_id": cyc.cycle_id, "question_id": cyc.question_id,
                    "request": request, "request_hash": request_hash,
                    "trigger_context_hash": context["trigger_context_hash"],
                    "policy_hash": context["policy_hash"],
                    "repo_provider": self.repo_provider.name,
                    "retrieval": context["retrieval"],
                    "wallclock_sec": wallclock,
                    "result": result, "result_hash": _hash(result),
                    "reference_snapshot": reference_snapshot,
                }
                atomic_write_receipt(receipt_path, receipt)
            except Exception as error:
                try:
                    self._finish_failed(
                        runner_call_id,
                        failure_kind=("provider_error" if isinstance(
                            error, ImportSearchProviderError)
                            else "trigger_postprocess_error"),
                        wallclock_sec=time.monotonic() - started)
                except Exception as finish_error:
                    add_note = getattr(error, "add_note", None)
                    if callable(add_note):
                        add_note(
                            "import trigger 失败收口也失败: "
                            f"{type(finish_error).__name__}: {finish_error}")
                raise
            self._after_receipt()
            return self._finalize_external(
                cyc=cyc, request=request, request_hash=request_hash,
                runner_call_id=runner_call_id, receipt_path=receipt_path)

    def __call__(self, cyc, request: Dict[str, Any], _pack=None) -> Dict[str, Any]:
        request = validate_import_search_request(request)
        if request["trigger_kind"] == "new_structure":
            raise ImportSearchError(
                "new_structure 不得进入 trusted source/status trigger service")
        if ("source_authority_hash" in request
                and request["trigger_kind"] in ("stuck", "sota_reference")):
            return self._activate_reference(cyc=cyc, request=request)
        return self._external(cyc=cyc, request=request)
