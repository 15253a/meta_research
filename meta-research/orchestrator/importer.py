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

from typing import Dict, Optional

from .ids import cnum as _cnum, qnum as _qnum   # 前缀校验解码（防类型错 id）
from .writedaemon import WriteDaemon


class DeferredImporter:
    def __init__(self, daemon: WriteDaemon):
        self.daemon = daemon

    def register_candidate(self, *, question_id: str, discovered_cycle: str, trigger_kind: str,
                           trigger_snapshot_hash: str, need_summary: str, source_kind: str,
                           canonical_uri: str, search_snapshot_json: str, search_snapshot_hash: str,
                           rank: int, retrieved_at: str, revision: Optional[str] = None) -> int:
        """发现登记：写不可变 external_candidate 发现快照（append-only，护 I6）。返回 candidate id。"""
        with self.daemon.transaction() as conn:
            return conn.execute(
                "INSERT INTO external_candidate(question_id,discovered_cycle,trigger_kind,trigger_snapshot_hash,"
                "need_summary,source_kind,canonical_uri,revision,search_snapshot_json,search_snapshot_hash,rank,retrieved_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (_qnum(question_id), _cnum(discovered_cycle), trigger_kind, trigger_snapshot_hash, need_summary,
                 source_kind, canonical_uri, revision, search_snapshot_json, search_snapshot_hash, rank, retrieved_at)
            ).lastrowid

    def review_license(self, *, candidate_id: int, decision: str, actor: str = "auto",
                       license_scope_json: Optional[str] = None, decided_cycle: Optional[str] = None,
                       policy_hash: Optional[str] = None) -> int:
        """license 裁定（append-only 事件；allow 须带 scope，DDL CHECK 焊）。返回 license_review id。"""
        with self.daemon.transaction() as conn:
            return conn.execute(
                "INSERT INTO license_review(candidate_id,decision,actor,license_scope_json,decided_cycle,policy_hash) "
                "VALUES (?,?,?,?,?,?)",
                (candidate_id, decision, actor, license_scope_json,
                 _cnum(decided_cycle) if decided_cycle else None, policy_hash)
            ).lastrowid

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
                "WHERE question_id=? AND selection_key=? AND action='selected_for_materialization'",
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
