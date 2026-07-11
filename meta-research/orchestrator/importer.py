"""DeferredImporter —— 外部 import 的 M1–M3 降级实现（§3.6.3 里程碑降级）。

M1–M3 只做「发现 + 登记（不物化）」：产 `external_candidate` 发现快照 + `license_review` 裁定 +
`external_import(selected_for_materialization)` 审计状态 + **占位 `baseline(planned)`** + `question_dep(baseline,pending)`
——**不产可执行 target、不进 bundle、不入池(legal)、不 pool_publish**；真物化（占位 baseline → legal）**只在 M4**
（`gate_register_baseline`），届时 dep 由 baseline→legal 机械 satisfied、问题恢复可调度（§4.2.1）。
`import_pending_materialization` 只是「占位 baseline 的 question_dep 处于 pending」这一处境的**描述名**，非新状态。

写路径经 WriteDaemon（与 Gate/StateStore 共用单写连接，§6.6）。`materialize()` 不在本类（M4）。

隔离边界（M1c 验收，见 test_isolation_m1c）：本类的产物**不得**越 M0–M3 假执行边界——由「不写 build_target /
不置 baseline legal / 占位 dep 使问题不可调度」在实现上保证，用例断言之。
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any, Dict, Optional

from .cost_ledger import policy_fingerprint
from .ids import cnum as _cnum, qnum as _qnum   # 前缀校验解码（防类型错 id）
from .writedaemon import WriteDaemon


_MAX_PLAN_CANDIDATES = 128
_MAX_DISCOVERY_SNAPSHOT_BYTES = 128 * 1024 * 1024
_MAX_LICENSE_SCOPE_BYTES = 16 * 1024
_IDENTITY_COMPONENT_RE = re.compile(r"[^\w.-]+", re.UNICODE)


def _snapshot_hash(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _strict_json_object(raw: str, *, field: str) -> Dict[str, Any]:
    """Parse one finite JSON object and reject duplicate keys.

    Discovery bytes are later interpreted by more than one component.  Accepting duplicate object
    keys would make the registered byte hash stable while leaving the semantic value dependent on
    parser policy (first-key/last-key wins), which is not a replayable snapshot.
    """
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{field} 含重复 JSON key: {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw, object_pairs_hook=object_pairs,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"非有限 JSON number: {token}")))
    except (json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"{field} 非合法严格 JSON") from error
    if not isinstance(value, dict):
        raise ValueError(f"{field} 须为 JSON object")
    return value


def _normalized_identity_component(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"import placeholder {field} 须为非空字符串")
    normalized = unicodedata.normalize("NFKC", value).strip().lower()
    normalized = _IDENTITY_COMPONENT_RE.sub("-", normalized).strip("-._")
    if not normalized or len(normalized.encode("utf-8")) > 160:
        raise ValueError(f"import placeholder {field} 规范化后为空或超过 160 bytes")
    return normalized


def _bounded_text(value: Any, *, field: str, max_bytes: int,
                  optional: bool = False) -> Optional[str]:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} 须为{'可空或' if optional else ''}非空字符串")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise ValueError(f"{field} 不是合法 UTF-8") from error
    if size > max_bytes:
        raise ValueError(f"{field} 超过 {max_bytes} bytes")
    return value


class DeferredImporter:
    def __init__(self, daemon: WriteDaemon):
        self.daemon = daemon

    @classmethod
    def register_candidate_in_txn(
            cls, conn, *, question_id: str, discovered_cycle: str, trigger_kind: str,
            trigger_snapshot_hash: str, need_summary: str, source_kind: str,
            canonical_uri: str, search_snapshot_json: str, search_snapshot_hash: str,
            rank: int, retrieved_at: str, revision: Optional[str] = None,
            license_id_seen: Optional[str] = None,
            search_provider: Optional[str] = None,
            search_query: Optional[str] = None) -> int:
        """Register one immutable discovery snapshot inside the caller's short transaction."""
        if not getattr(conn, "in_transaction", False):
            raise RuntimeError("register_candidate_in_txn 必须在 WriteDaemon 短事务内调用")
        _bounded_text(trigger_snapshot_hash, field="trigger_snapshot_hash", max_bytes=256)
        _bounded_text(need_summary, field="need_summary", max_bytes=4096)
        _bounded_text(canonical_uri, field="canonical_uri", max_bytes=4096)
        _bounded_text(revision, field="revision", max_bytes=512, optional=True)
        _bounded_text(
            license_id_seen, field="license_id_seen", max_bytes=256, optional=True)
        _bounded_text(
            search_provider, field="search_provider", max_bytes=256, optional=True)
        _bounded_text(search_query, field="search_query", max_bytes=4096, optional=True)
        _bounded_text(search_snapshot_hash, field="search_snapshot_hash", max_bytes=256)
        _bounded_text(retrieved_at, field="retrieved_at", max_bytes=256)
        if (isinstance(rank, bool) or not isinstance(rank, int) or rank < 0):
            raise ValueError("external_candidate.rank 须为非负整数")
        _bounded_text(
            search_snapshot_json, field="search_snapshot_json",
            max_bytes=_MAX_DISCOVERY_SNAPSHOT_BYTES)
        _strict_json_object(search_snapshot_json, field="search_snapshot_json")
        actual_snapshot_hash = (
            "sha256:" + hashlib.sha256(search_snapshot_json.encode("utf-8")).hexdigest())
        if search_snapshot_hash != actual_snapshot_hash:
            raise ValueError(
                "search_snapshot_hash 与 search_snapshot_json 的精确 UTF-8 字节不一致")
        return conn.execute(
            "INSERT INTO external_candidate(question_id,discovered_cycle,trigger_kind,trigger_snapshot_hash,"
            "need_summary,source_kind,canonical_uri,revision,license_id_seen,search_provider,search_query,"
            "search_snapshot_json,search_snapshot_hash,rank,retrieved_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (_qnum(question_id), _cnum(discovered_cycle), trigger_kind, trigger_snapshot_hash,
             need_summary, source_kind, canonical_uri, revision, license_id_seen,
             search_provider, search_query, search_snapshot_json, search_snapshot_hash,
             rank, retrieved_at)).lastrowid

    def register_candidate(self, *, question_id: str, discovered_cycle: str, trigger_kind: str,
                           trigger_snapshot_hash: str, need_summary: str, source_kind: str,
                           canonical_uri: str, search_snapshot_json: str, search_snapshot_hash: str,
                           rank: int, retrieved_at: str, revision: Optional[str] = None,
                           license_id_seen: Optional[str] = None,
                           search_provider: Optional[str] = None,
                           search_query: Optional[str] = None) -> int:
        """发现登记：写不可变 external_candidate 发现快照（append-only，护 I6）。返回 candidate id。"""
        with self.daemon.transaction() as conn:
            return self.register_candidate_in_txn(
                conn, question_id=question_id, discovered_cycle=discovered_cycle,
                trigger_kind=trigger_kind, trigger_snapshot_hash=trigger_snapshot_hash,
                need_summary=need_summary, source_kind=source_kind,
                canonical_uri=canonical_uri, revision=revision,
                license_id_seen=license_id_seen, search_provider=search_provider,
                search_query=search_query, search_snapshot_json=search_snapshot_json,
                search_snapshot_hash=search_snapshot_hash, rank=rank,
                retrieved_at=retrieved_at)

    @classmethod
    def review_license_in_txn(
            cls, conn, *, candidate_id: int, decision: str, actor: str = "auto",
            license_scope_json: Optional[str] = None, decided_cycle: Optional[str] = None,
            policy_hash: Optional[str] = None, license_id: Optional[str] = None,
            evidence_ref: Optional[str] = None) -> int:
        """Append one license decision inside the caller's short transaction."""
        if not getattr(conn, "in_transaction", False):
            raise RuntimeError("review_license_in_txn 必须在 WriteDaemon 短事务内调用")
        if (isinstance(candidate_id, bool) or not isinstance(candidate_id, int)
                or candidate_id <= 0):
            raise ValueError("license candidate_id 须为正整数")
        _bounded_text(license_id, field="license_id", max_bytes=256, optional=True)
        _bounded_text(evidence_ref, field="license evidence_ref", max_bytes=4096, optional=True)
        if license_scope_json is not None:
            _bounded_text(
                license_scope_json, field="license_scope_json",
                max_bytes=_MAX_LICENSE_SCOPE_BYTES)
            scope = _strict_json_object(
                license_scope_json, field="license_scope_json")
            allowed_scope_keys = {
                "allow_eval", "allow_modify", "allow_publish_pool", "allow_redistribute",
            }
            unknown = sorted(set(scope) - allowed_scope_keys)
            if unknown:
                raise ValueError(f"license_scope_json 含未知键: {unknown}")
            if any(not isinstance(value, bool) for value in scope.values()):
                raise ValueError("license_scope_json 的 capability 值必须为 boolean")
        if policy_hash is not None:
            _bounded_text(policy_hash, field="license policy_hash", max_bytes=256)
        return conn.execute(
            "INSERT INTO license_review(candidate_id,decision,license_id,evidence_ref,actor,"
            "license_scope_json,decided_cycle,policy_hash) VALUES (?,?,?,?,?,?,?,?)",
            (candidate_id, decision, license_id, evidence_ref, actor,
             license_scope_json, _cnum(decided_cycle) if decided_cycle else None,
             policy_hash)).lastrowid

    def review_license(self, *, candidate_id: int, decision: str, actor: str = "auto",
                       license_scope_json: Optional[str] = None, decided_cycle: Optional[str] = None,
                       policy_hash: Optional[str] = None, license_id: Optional[str] = None,
                       evidence_ref: Optional[str] = None) -> int:
        """license 裁定（append-only 事件；allow 须带 scope，DDL CHECK 焊）。返回 license_review id。"""
        with self.daemon.transaction() as conn:
            return self.review_license_in_txn(
                conn, candidate_id=candidate_id, decision=decision, actor=actor,
                license_scope_json=license_scope_json, decided_cycle=decided_cycle,
                policy_hash=policy_hash, license_id=license_id,
                evidence_ref=evidence_ref)

    @staticmethod
    def policy_hash(policy: Dict[str, Any]) -> str:
        """plan/import 共用的当前 policy 内容指纹；带算法前缀，禁止手填版本冒充内容。"""
        return "sha256:" + policy_fingerprint(policy)

    @classmethod
    def plan_snapshot(cls, conn, *, question_id: int, action_cycle: int,
                      policy_hash: str) -> Dict[str, Any]:
        """冻结本 action cycle 已登记候选与 license 裁定，供 compiler/plan commit 同口径使用。

        候选集只取 ``discovered_cycle == action_cycle``，绝不隐式读取“累计至今/latest”。候选的
        ``rank`` 已是 search provider 冻结的排序结果，故唯一支持的机械选择配方为 ``rank_asc``；
        ``retrieved_at/created_at`` 仅审计，不进入 hash 或选择。license 同样只取本 action cycle 的
        append-only 裁定，并把完整快照 hash 落到 selected_for_materialization。
        """
        if (isinstance(question_id, bool) or not isinstance(question_id, int)
                or question_id <= 0 or isinstance(action_cycle, bool)
                or not isinstance(action_cycle, int) or action_cycle <= 0):
            raise ValueError("plan import snapshot 要求正整数 question/action_cycle")
        _bounded_text(policy_hash, field="plan import policy_hash", max_bytes=256)
        rows = conn.execute(
            "SELECT id,trigger_kind,trigger_snapshot_hash,need_summary,source_kind,"
            "canonical_uri,revision,license_id_seen,search_provider,search_query,"
            "search_snapshot_hash,rank "
            "FROM external_candidate WHERE question_id=? AND discovered_cycle=? "
            "ORDER BY rank,canonical_uri,COALESCE(revision,''),source_kind,"
            "trigger_snapshot_hash,search_snapshot_hash",
            (question_id, action_cycle)).fetchall()
        if len(rows) > _MAX_PLAN_CANDIDATES:
            raise ValueError(
                f"cycle c{action_cycle} 的 q{question_id} import 候选超过 {_MAX_PLAN_CANDIDATES}，"
                "拒绝隐式截断冻结集")
        candidates = []
        for row in rows:
            candidates.append({
                "candidate_id": row[0], "trigger_kind": row[1],
                "trigger_snapshot_hash": _bounded_text(
                    row[2], field=f"external_candidate {row[0]} trigger_snapshot_hash",
                    max_bytes=256),
                "need_summary": _bounded_text(
                    row[3], field=f"external_candidate {row[0]} need_summary",
                    max_bytes=4096),
                "source_kind": row[4],
                "canonical_uri": _bounded_text(
                    row[5], field=f"external_candidate {row[0]} canonical_uri",
                    max_bytes=4096),
                "revision": _bounded_text(
                    row[6], field=f"external_candidate {row[0]} revision",
                    max_bytes=512, optional=True),
                "license_id_seen": _bounded_text(
                    row[7], field=f"external_candidate {row[0]} license_id_seen",
                    max_bytes=256, optional=True),
                "search_provider": _bounded_text(
                    row[8], field=f"external_candidate {row[0]} search_provider",
                    max_bytes=256, optional=True),
                "search_query": _bounded_text(
                    row[9], field=f"external_candidate {row[0]} search_query",
                    max_bytes=4096, optional=True),
                "search_snapshot_hash": _bounded_text(
                    row[10], field=f"external_candidate {row[0]} search_snapshot_hash",
                    max_bytes=256),
                "rank": row[11],
            })
        # SQLite surrogate ids are local references, not discovery content.  They must never enter
        # the I6 hash: restoring the same immutable snapshots into a fresh DB may allocate different
        # row ids but must reproduce the same candidate-set identity and deterministic choice.
        legacy_candidate_keys = (
            "trigger_kind", "trigger_snapshot_hash", "need_summary", "source_kind",
            "canonical_uri", "revision", "search_snapshot_hash", "rank")
        provenance_candidate_keys = legacy_candidate_keys[:-2] + (
            "license_id_seen", "search_provider", "search_query") + legacy_candidate_keys[-2:]
        # CP11.4a.1 already persisted v2 hashes for candidates registered before the production
        # connector existed.  Those rows have all three additive provenance columns NULL.  Keep the
        # exact v2 formula for them so a crash-staged import_defer remains commit-compatible across
        # upgrade; only connector-backed rows opt into v3.
        candidate_hash_version = (3 if any(
            candidate[key] is not None
            for candidate in candidates
            for key in ("license_id_seen", "search_provider", "search_query")) else 2)
        candidate_hash_keys = (provenance_candidate_keys
                               if candidate_hash_version == 3 else legacy_candidate_keys)
        candidate_hash_records = [{
            key: candidate[key] for key in candidate_hash_keys
        } for candidate in candidates]
        candidate_set_hash = _snapshot_hash({
            "version": candidate_hash_version, "selection_key": "rank_asc",
            "candidates": candidate_hash_records,
        })
        candidate_ids = [item["candidate_id"] for item in candidates]
        review_rows = []
        if candidate_ids:
            placeholders = ",".join("?" for _ in candidate_ids)
            review_rows = conn.execute(
                "SELECT id,candidate_id,decision,license_id,evidence_ref,license_scope_json,actor,policy_hash "
                f"FROM license_review WHERE candidate_id IN ({placeholders}) AND decided_cycle=? "
                "ORDER BY candidate_id,id", (*candidate_ids, action_cycle)).fetchall()
        reviews = []
        terminal_by_candidate: Dict[int, list] = {}
        for row in review_rows:
            if row[5] is not None:
                _bounded_text(
                    row[5], field=f"license_review {row[0]} scope",
                    max_bytes=_MAX_LICENSE_SCOPE_BYTES)
            if row[7] is not None:
                _bounded_text(
                    row[7], field=f"license_review {row[0]} policy_hash",
                    max_bytes=256)
            _bounded_text(
                row[3], field=f"license_review {row[0]} license_id",
                max_bytes=256, optional=True)
            _bounded_text(
                row[4], field=f"license_review {row[0]} evidence_ref",
                max_bytes=4096, optional=True)
            try:
                scope = (json.loads(
                    row[5], parse_constant=lambda token: (_ for _ in ()).throw(
                        ValueError(f"非有限 JSON number: {token}")))
                         if row[5] is not None else None)
            except (json.JSONDecodeError, ValueError) as error:
                raise ValueError(f"license_review {row[0]} scope JSON 损坏") from error
            if scope is not None and not isinstance(scope, dict):
                raise ValueError(f"license_review {row[0]} scope 须为 JSON object")
            item = {
                "license_review_id": row[0], "candidate_id": row[1],
                "decision": row[2], "license_id": row[3],
                "evidence_ref": row[4], "license_scope": scope,
                "actor": row[6], "policy_hash": row[7],
            }
            reviews.append(item)
            if row[2] in ("allow", "deny") and row[7] == policy_hash:
                terminal_by_candidate.setdefault(row[1], []).append(item)
        for candidate_id, events in terminal_by_candidate.items():
            if len(events) > 1:
                raise ValueError(
                    f"candidate {candidate_id} 在 action cycle c{action_cycle} 有多个当前 policy "
                    "terminal license 裁定；冻结选择歧义")
        candidate_content_by_id = {
            candidate["candidate_id"]: content
            for candidate, content in zip(candidates, candidate_hash_records)
        }
        review_hash_version = (3 if candidate_hash_version == 3 or any(
            review["license_id"] is not None or review["evidence_ref"] is not None
            for review in reviews) else 2)
        review_hash_records = []
        for review in reviews:
            record = {
                "candidate": candidate_content_by_id[review["candidate_id"]],
                "decision": review["decision"],
                "license_scope": review["license_scope"],
                "actor": review["actor"],
                "policy_hash": review["policy_hash"],
            }
            if review_hash_version == 3:
                record.update({
                    "license_id": review["license_id"],
                    "evidence_ref": review["evidence_ref"],
                })
            review_hash_records.append(record)
        review_hash_records.sort(key=lambda item: json.dumps(
            item, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))
        license_hash = _snapshot_hash({
            "version": review_hash_version, "candidate_set_hash": candidate_set_hash,
            "reviews": review_hash_records,
        })
        rejected = []
        selected = None
        for candidate in candidates:
            events = terminal_by_candidate.get(candidate["candidate_id"], [])
            if not events:
                continue
            event = events[0]
            if event["decision"] == "deny":
                rejected.append({"candidate": candidate, "review": event})
                continue
            scope = event.get("license_scope")
            if (isinstance(scope, dict) and scope.get("allow_eval") is True
                    and scope.get("allow_publish_pool") is True):
                selected = {"candidate": candidate, "review": event}
                break
        return {
            "selection_key": "rank_asc", "policy_hash": policy_hash,
            "candidate_set_hash": candidate_set_hash,
            "license_decision_snapshot_hash": license_hash,
            "candidates": candidates, "reviews": reviews,
            "rejected_before_selection": rejected,
            "selected": selected,
        }

    @classmethod
    def select_plan_deferred_in_txn(
            cls, conn, *, question_id: str, action_cycle: str,
            import_defer: Dict[str, Any], policy: Dict[str, Any]) -> Dict[str, int]:
        """plan phase_commit 内的确定性 import 选择与业务三写入。

        调用方必须在同一外层事务继续写 ``phase_commit + dependency_wait route + Qn release +
        cycle done``。本方法绝不自开事务，因而任何校验/后续状态迁移失败都会把选择、占位和 dep
        一起回滚。
        """
        qi, ci = _qnum(question_id), _cnum(action_cycle)
        lineage = conn.execute(
            "SELECT c.goal_id,c.goal_ver,c.status,c.route,c.active_question_id,"
            "q.goal_id,q.goal_ver,q.status,q.active_cycle,"
            "(SELECT MAX(version) FROM goal WHERE id=c.goal_id) "
            "FROM cycle c JOIN question q ON q.id=? WHERE c.id=?",
            (qi, ci)).fetchone()
        if (lineage is None or lineage[2] in ("done", "failed", "aborted")
                or lineage[4] != qi or tuple(lineage[:2]) != tuple(lineage[5:7])
                or lineage[7] != "active" or lineage[8] != ci
                or lineage[9] != lineage[1]):
            raise ValueError(
                f"import_defer 只允许 current cycle c{ci} 的 exact active question q{qi}")
        expected_policy_hash = cls.policy_hash(policy)
        snapshot = cls.plan_snapshot(
            conn, question_id=qi, action_cycle=ci,
            policy_hash=expected_policy_hash)
        if import_defer.get("policy_hash") != expected_policy_hash:
            raise ValueError("import_defer.policy_hash 与当前 policy 内容指纹不一致")
        if import_defer.get("selection_key") != snapshot["selection_key"]:
            raise ValueError(
                f"import_defer.selection_key 只接受 {snapshot['selection_key']!r}")
        if import_defer.get("candidate_set_hash") != snapshot["candidate_set_hash"]:
            raise ValueError("import_defer.candidate_set_hash 与本 action cycle 冻结候选集不一致")
        if (import_defer.get("license_decision_snapshot_hash")
                != snapshot["license_decision_snapshot_hash"]):
            raise ValueError(
                "import_defer.license_decision_snapshot_hash 与 plan 所见冻结 license 裁定集不一致")
        selected = snapshot["selected"]
        if selected is None:
            raise ValueError(
                "import_defer 无当前 policy 下同时 allow_eval+allow_publish_pool 的确定性候选")
        identity = import_defer.get("placeholder_baseline_identity")
        if not isinstance(identity, dict):
            raise ValueError("import_defer 缺 placeholder_baseline_identity")
        canonical_key = _normalized_identity_component(
            identity.get("canonical_key_draft"), field="canonical_key")
        slug = _normalized_identity_component(identity.get("slug_draft"), field="slug")
        identity_md = _bounded_text(
            identity.get("identity_md"), field="import placeholder identity_md",
            max_bytes=64 * 1024)
        reason_md = _bounded_text(
            import_defer.get("reason_md"), field="import_defer.reason_md",
            max_bytes=8 * 1024)
        if conn.execute(
                "SELECT 1 FROM baseline WHERE canonical_key=?", (canonical_key,)).fetchone():
            raise ValueError(f"import placeholder canonical_key 已占（I5）: {canonical_key!r}")
        if conn.execute(
                "SELECT 1 FROM external_import WHERE question_id=? "
                "AND action='selected_for_materialization' AND NOT EXISTS ("
                "SELECT 1 FROM external_import x WHERE x.question_id=external_import.question_id "
                "AND x.candidate_id=external_import.candidate_id "
                "AND x.action_cycle=external_import.action_cycle "
                "AND x.candidate_set_hash=external_import.candidate_set_hash "
                "AND x.selection_key=external_import.selection_key "
                "AND x.policy_hash=external_import.policy_hash "
                "AND x.action IN ('imported','materialize_failed','superseded')) LIMIT 1",
                (qi,)).fetchone():
            raise ValueError(f"question q{qi} 已有未收口 materialization selection")

        # deny 事件也是冻结选择轨迹；只记录排在最终 allow 之前且由同一 policy/action cycle 裁定者。
        for rejected in snapshot["rejected_before_selection"]:
            candidate, review = rejected["candidate"], rejected["review"]
            conn.execute(
                "INSERT INTO external_import(question_id,candidate_id,action,action_cycle,"
                "candidate_set_hash,selection_key,policy_hash,license_decision_snapshot_hash,"
                "license_review_id,reason_json) VALUES (?,?,'rejected_by_license',?,?,?,?,?,?,?)",
                (qi, candidate["candidate_id"], ci, snapshot["candidate_set_hash"],
                 snapshot["selection_key"], expected_policy_hash,
                 snapshot["license_decision_snapshot_hash"], review["license_review_id"],
                 json.dumps({"reason": "frozen_license_deny"}, sort_keys=True)))
        bid = conn.execute(
            "INSERT INTO baseline(slug,canonical_key,identity_doc,born_cycle,status,provenance,license_status) "
            "VALUES (?,?,?,?, 'planned','external_import','allow')",
            (slug, canonical_key, identity_md.strip(), ci)).lastrowid
        candidate, review = selected["candidate"], selected["review"]
        eid = conn.execute(
            "INSERT INTO external_import(question_id,candidate_id,action,action_cycle,candidate_set_hash,"
            "selection_key,policy_hash,license_decision_snapshot_hash,license_review_id,baseline_id,reason_json) "
            "VALUES (?,?,'selected_for_materialization',?,?,?,?,?,?,?,?)",
            (qi, candidate["candidate_id"], ci, snapshot["candidate_set_hash"],
             snapshot["selection_key"], expected_policy_hash,
             snapshot["license_decision_snapshot_hash"], review["license_review_id"], bid,
             json.dumps({"reason_md": reason_md},
                        ensure_ascii=False, sort_keys=True))).lastrowid
        did = conn.execute(
            "INSERT INTO question_dep(question_id,dep_type,depends_on_baseline_id,status,created_cycle) "
            "VALUES (?,'baseline',?,'pending',?)", (qi, bid, ci)).lastrowid
        return {"baseline_id": bid, "external_import_id": eid,
                "question_dep_id": did}

    def select_deferred(self, *, question_id: str, candidate_id: int, license_review_id: int,
                        action_cycle: str, candidate_set_hash: str, selection_key: str, policy_hash: str,
                        license_decision_snapshot_hash: str, placeholder_canonical_key: str,
                        placeholder_slug: str = "import-placeholder") -> Dict[str, int]:
        """确定性选择（deferred）：**三写入同一事务**（§3.6.3 / §4.2.5 防半写）——
        ① 占位 baseline(planned, provenance=external_import, license_status=allow)
        ② external_import(selected_for_materialization, baseline_id=占位, 无 manifest)
        ③ question_dep(baseline → 占位, pending) 使问题不可调度。
        返回 {baseline_id, external_import_id, question_dep_id}。**不物化**（M4）。
        **幂等（§7.1 M3「不重复登记」）**：同 (question, selection_key) 重放返回既有三元、不重复登记（护崩溃续跑 / 重入）。
        provenance 全在 append-only external_import 行（候选/选择锚），**不写 decision 账**（M1c 隔离口径「三写入」，
        避免 import 污染研究账本；与 interaction 同纪律，用例负断言之）。

        前置：候选须属本 question（防错挂）；license_review 须同候选 decision='allow'（§3.6.3「只从 allow 候选选」）。
        scope 匹配（allow_modify 可加变体 / allow_publish_pool 入池）**延至 M4 物化时验**（M1–M3 不物化，无 scope 消费点）。
        """
        qi, aci = _qnum(question_id), _cnum(action_cycle)
        with self.daemon.transaction() as conn:
            # 幂等（§7.1 M3「不重复登记」）：同 (question, selection_key) 已有 selected_for_materialization →
            # 返回既有三元、**不重复三写入**。I6 选择锚确定性 → 同一选择重放（崩溃续跑 / 重入）不产重复占位 baseline / dep。
            # **锚假设（M4 接手者注意）**：(question, selection_key) 唯一标识一个**在生效**的选择——M3 成立因
            # ① select 后问题带 pending dep 不可调度（调度器不可能对它再出新选择）② M3 无 supersession（=M4；
            # 届时须改判「未被 superseded 的」并复核 selection_key 是否仍每选择唯一，勿静默沿用）。
            # 此 check-then-act 依赖单写者（WriteDaemon 单连接 + BEGIN IMMEDIATE 串行）；DDL 无唯一约束兜底——
            # 引入多写者/写队列（M5）时须补 DB 级约束或队内去重。
            existing_rows = conn.execute(
                "SELECT id, baseline_id, candidate_id, license_review_id FROM external_import "
                "WHERE question_id=? AND selection_key=? AND action='selected_for_materialization' "
                "AND NOT EXISTS (SELECT 1 FROM external_import x WHERE "
                "x.question_id=external_import.question_id AND x.candidate_id=external_import.candidate_id "
                "AND x.action_cycle=external_import.action_cycle "
                "AND x.candidate_set_hash=external_import.candidate_set_hash "
                "AND x.selection_key=external_import.selection_key AND x.policy_hash=external_import.policy_hash "
                "AND x.action='superseded')",
                (qi, selection_key)).fetchall()
            if len(existing_rows) > 1:   # 无 DB 唯一约束兜底 → 主动探测重复（守卫失效/旁路写入即 fail loud，勿任取一条）
                raise ValueError(
                    f"external_import 存在 {len(existing_rows)} 条 (q{qi}, selection_key={selection_key!r}, "
                    "selected_for_materialization) 重复登记——「不重复登记」不变量已破，须人工修复")
            if existing_rows:
                eid0, bid0, cand0, lic0 = existing_rows[0]
                # fail loud（内审/外审 SHOULD）：幂等只豁免**真重放**（同候选 + 同 license 裁定）——
                # candidate 或 license_review_id 不符 = 调用方选择锚错乱 / 授权前置被换，不得静默返回旧登记冒充成功。
                # （candidate_set_hash/policy_hash 等审计锚分量按首次登记为准，不校验：同一候选+同一裁定已证同一选择。）
                if cand0 != candidate_id:
                    raise ValueError(
                        f"select_deferred 幂等重放候选不符：(q{qi}, selection_key={selection_key!r}) 既有登记属 "
                        f"candidate {cand0}，本次传 {candidate_id}——疑调用方选择锚错乱，拒绝静默复用")
                if lic0 != license_review_id:
                    raise ValueError(
                        f"select_deferred 幂等重放 license 裁定不符：既有登记据 license_review {lic0}，"
                        f"本次传 {license_review_id}——授权前置条件是契约、非审计细节，拒绝静默复用")
                did0 = conn.execute(
                    "SELECT id FROM question_dep WHERE question_id=? AND dep_type='baseline' AND depends_on_baseline_id=?",
                    (qi, bid0)).fetchone()
                if did0 is None:   # 三写入原子 → 合法态必有 dep；缺失 = 隔离锚已破（问题会错误恢复可调度），fail loud
                    raise ValueError(
                        f"select_deferred 幂等重放发现隔离锚破损：登记 {eid0} 存在但 question_dep(baseline→{bid0}) "
                        "缺失——不产 target/不可调度不变量失效，须人工修复")
                return {"baseline_id": bid0, "external_import_id": eid0, "question_dep_id": did0[0]}
            cand = conn.execute("SELECT question_id FROM external_candidate WHERE id=?", (candidate_id,)).fetchone()
            if cand is None or cand[0] != qi:   # 候选须属本问题（防把 q1 发现的候选错挂到 q2、污染 provenance / 错阻别问题）
                raise ValueError(f"candidate {candidate_id} 不属于 question {question_id}（当前 {cand}）")
            lic = conn.execute("SELECT candidate_id, decision FROM license_review WHERE id=?",
                               (license_review_id,)).fetchone()
            if lic is None or lic[0] != candidate_id or lic[1] != "allow":
                raise ValueError(f"select_deferred 须同候选 decision=allow 的 license_review（当前 {lic}）")
            # ① 占位 baseline（planned；external_import 来源须 license_status=allow，DDL CHECK）
            bid = conn.execute(
                "INSERT INTO baseline(slug,canonical_key,status,provenance,license_status,born_cycle) "
                "VALUES (?,?,'planned','external_import','allow',?)",
                (placeholder_slug, placeholder_canonical_key, aci)).lastrowid
            # ② 选择/导入事件（selected_for_materialization：携 baseline_id、无 manifest；ldsh 必填）
            eid = conn.execute(
                "INSERT INTO external_import(question_id,candidate_id,action,action_cycle,candidate_set_hash,"
                "selection_key,policy_hash,license_decision_snapshot_hash,license_review_id,baseline_id) "
                "VALUES (?,?,'selected_for_materialization',?,?,?,?,?,?,?)",
                (qi, candidate_id, aci, candidate_set_hash, selection_key, policy_hash,
                 license_decision_snapshot_hash, license_review_id, bid)).lastrowid
            # ③ pending baseline dep：问题因占位 baseline 未 legal 而不可调度（§4.2.1；非新增 question.status）
            did = conn.execute(
                "INSERT INTO question_dep(question_id,dep_type,depends_on_baseline_id,status,created_cycle) "
                "VALUES (?,'baseline',?,'pending',?)", (qi, bid, aci)).lastrowid
        return {"baseline_id": bid, "external_import_id": eid, "question_dep_id": did}

    def reject_by_license(self, *, question_id: str, candidate_id: int, action_cycle: str,
                          candidate_set_hash: str, selection_key: str, policy_hash: str,
                          license_review_id: int) -> int:
        """deny 分支：external_import(rejected_by_license)（无 baseline/manifest）→ 上层按 selection_key 取下一个。
        前置一致性（同 select_deferred 纪律）：候选须属本 question；license_review_id 须同候选 decision='deny'——
        回指是哪条 deny 裁定致拒、补全审计溯源。"""
        qi = _qnum(question_id)
        with self.daemon.transaction() as conn:
            cand = conn.execute("SELECT question_id FROM external_candidate WHERE id=?", (candidate_id,)).fetchone()
            if cand is None or cand[0] != qi:
                raise ValueError(f"candidate {candidate_id} 不属于 question {question_id}（当前 {cand}）")
            lic = conn.execute("SELECT candidate_id, decision FROM license_review WHERE id=?",
                               (license_review_id,)).fetchone()
            if lic is None or lic[0] != candidate_id or lic[1] != "deny":
                raise ValueError(f"reject_by_license 须同候选 decision=deny 的 license_review（当前 {lic}）")
            return conn.execute(
                "INSERT INTO external_import(question_id,candidate_id,action,action_cycle,candidate_set_hash,"
                "selection_key,policy_hash,license_review_id) VALUES (?,?,'rejected_by_license',?,?,?,?,?)",
                (qi, candidate_id, _cnum(action_cycle), candidate_set_hash, selection_key, policy_hash, license_review_id)
            ).lastrowid

    # materialize() —— 占位 baseline → legal（gate_register_baseline）——**M4**，本类不实现。
