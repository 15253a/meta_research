"""SqliteCompiler —— 上下文编译器的真实现（M2：DB 真相 → 确定性四区 context_pack，§4.5.1）。

对齐 M0 StubCompiler 的两条硬纪律（此处从真 DB 读，非内存 StateStore/ArtifactIndex）：
1. **输入只来自 DB 真相**：四区包只从 DB 已提交行渲染，来源清单落 manifest（pack 溯源）。
2. **确定性（M2 验收核心）**：同快照 + 同配方(policy) + 同预算 + 同 target → **字节一致（diff=0）**——
   一切遍历 `ORDER BY id` 定序、无 wall-clock / 随机 / dict 无序；pack_hash = sha256(四区拼接)。

四区（§4.5.1）：①固定锚(任务关键集、不截断) ②结构邻域(祖先链) ③检索区(top-k 卡片) ④引用区(ctx-fetch ref)。
**applicability 徽标（编译器确定性规则，§4.5.1）**：任何呈现已关闭结论处必 join 该 answer 当前 goal_ver 的
`answer_applicability` 行、渲染单行六枚举徽标；无行=无徽标。
运行观测摘要段(§4.7)于 reasoning 固定锚**已渲**（CP3.3，`_observation_summary`）。检索区/引用区留空——recall
组件(recall_sqlite)已备（CP3.2），**接入 compiler.render 检索区 = M3 Advancer**（编译器按配方调 recall）。
status_card(§4.6.6)另置 `status_card.py`（派生卡，非 render 产物）。

与 M0 StubCompiler 并存不替换（M0 driver 仍用 Stub、基线绿）；M3 Advancer 接真组件。
读连接为普通只读连接（**非** gate 的受限连接——编译器可读 execution_observation 渲观测摘要给 reasoning；
gate 判据禁读 observation 由 SqliteGate 的 authorizer 另管，二者分离，§3.1.2）。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

from .budgeting import compute_budget
from .ids import cnum as _cnum
from .interfaces import ContextPack, Stage


class SqliteCompiler:
    def __init__(self, conn, policy: Dict[str, Any], goal_body_md: str):
        # conn = 本类**独占**的只读用连接（isolation_level=None 交本类掌控事务，供 render 钉单一读快照）。
        # 「只读」是架构约定：调用方（M3 Advancer）应传一条专用读连接（宜 mode=ro，§6.2 WAL 读写分离）；
        # 本类只读不写，不在此强制 mode=ro（编译器不该越俎给连接改物理模式）。
        conn.isolation_level = None
        self.conn = conn
        self.policy = policy
        self.goal_body_md = goal_body_md

    # -- Compiler Protocol ------------------------------------------------------
    def render(self, *, cycle_id: str, stage: Stage, target_id: Optional[str] = None) -> ContextPack:
        """确定性四区包。**钉单一读快照**：整个 render 在一个读事务内（BEGIN…COMMIT）——WAL 下这一致快照
        不被并发 WriteDaemon 提交撬动，杜绝「cycle 取自 A 态、answers 取自 B 态」的混态包（护「同快照」）。

        「同快照」= 同行 + **同 id**：id 是快照身份的一部分，故 ORDER BY id 定序确定；不同插入序 = 不同快照
        （合法产出不同字节），M2 不要求二者相等。
        """
        if stage == "bundle" and target_id is None:
            raise ValueError("bundle 阶段须逐 target 渲染（target_id 不可为 None）")
        ci = _cnum(cycle_id)
        self.conn.execute("BEGIN")            # 钉读快照（deferred；首个读起快照）
        try:
            cyc = self.conn.execute(
                "SELECT route, active_question_id, goal_id, goal_ver, status FROM cycle WHERE id=?", (ci,)).fetchone()
            if cyc is None:
                raise ValueError(f"cycle 不存在: {cycle_id}")
            route, aq, goal_id, goal_ver, cstatus = cyc
            sources: List[str] = []
            anchor = self._anchor(cycle_id, ci, stage, target_id, route, aq, goal_id, goal_ver, sources)
            neighborhood = self._neighborhood(aq, sources)
        finally:
            self.conn.execute("COMMIT")       # 结束只读快照（无写、COMMIT 即释放）
        retrieval = ""                        # CP3.2 recall 填
        refs: List[str] = []                  # CP3.2 引用区填
        pack = ContextPack(cycle_id=cycle_id, stage=stage, target_id=target_id,
                           anchor_md=anchor, neighborhood_md=neighborhood, retrieval_md=retrieval, refs=refs,
                           sources=sorted(set(sources)))
        # \x00 分隔四区（含 refs 规范化）再 hash：防区界重排碰撞；refs 现空、纳入以定 CP3.2 契约不再改口径
        pack.pack_hash = hashlib.sha256(
            ("\x00".join((anchor, neighborhood, retrieval, json.dumps(refs, ensure_ascii=False)))).encode("utf-8")).hexdigest()
        return pack

    def manifest(self, pack: ContextPack) -> Dict[str, Any]:
        """pack 溯源 manifest（pack_hash + 分区来源清单）——**pack 的纯函数**（sources 就在 pack 上，
        不依赖实例态/instance，跨实例/重启/穿插 render 皆一致）；M3 起随 DECISION 入账。"""
        return {"pack_hash": pack.pack_hash, "stage": pack.stage, "target_id": pack.target_id,
                "sources": list(pack.sources)}

    # -- 分区渲染 ---------------------------------------------------------------
    def _anchor(self, cycle_id, ci, stage, target_id, route, aq, goal_id, goal_ver, sources) -> str:
        parts: List[str] = [f"route={route}；本轮 cycle={cycle_id}"]
        if aq is not None:
            q = self.conn.execute("SELECT text, status, visit_count FROM question WHERE id=?", (aq,)).fetchone()
            parts.append(f"## 本轮问题卡 Qn\n- id: q{aq}\n- 问题: {q[0]}\n- 状态: {q[1]}（visit={q[2]}）")
            sources.append(f"db:question:{aq}")
        if stage == "idea":
            parts.append(self._prior_ideas(aq, sources))
        elif stage == "plan":
            parts.append(f"## 单轮预算\nB(t) = {self._budget()}（policy budget 节）")
            sources.append("policy:budget")
        elif stage == "bundle" and target_id is not None:
            # target_id 已消费（不同 target → 不同 pack）；完整计划切片（build_target 行 + 协议 + required_metric）
            # = M3（plan gate 产 build_target 行后编译），本检查点占位——同 retrieval/观测段的延后。
            parts.append(f"## 本目标\n- target: {target_id}\n（完整计划切片 = CP-M3：plan gate 产 build_target 后编译）")
            sources.append(f"db:build_target:{target_id}")
        elif stage == "reasoning":
            parts.append(f"## 目标全文（当前版 v{goal_ver}）\n{self.goal_body_md}")
            sources.append("input:goal_brief.md")
            parts.append(self._closed_conclusions(goal_id, goal_ver, sources))
            parts.append(self._open_set(aq, goal_id, sources))
            parts.append(self._observation_summary(ci, sources))
            parts.append("## 采集打分参数\n```json\n" + json.dumps(
                {"acquisition": self.policy["acquisition"], "B_t": self._budget(),
                 "decompose_threshold": self.policy["flow"]["decompose_threshold"],
                 "tau": self.policy["flow"]["tau"]}, ensure_ascii=False, sort_keys=True) + "\n```")
            sources.append("policy:acquisition")
        return "\n\n".join(p for p in parts if p)

    def _neighborhood(self, aq, sources) -> str:
        """结构邻域 = 祖先链（recursive on parent_id；DDL trg_question_parent_frozen 防环，seen 兜底坏数据）。"""
        if aq is None:
            return ""
        parent = self.conn.execute("SELECT parent_id FROM question WHERE id=?", (aq,)).fetchone()[0]
        chain, seen = [], set()
        while parent is not None and parent not in seen:
            seen.add(parent)
            p = self.conn.execute("SELECT text, status FROM question WHERE id=?", (parent,)).fetchone()
            if p is None:
                chain.append(f"- （坏引用 parent=q{parent}，链在此截断）")
                break
            chain.append(f"- q{parent}（{p[1]}）: {p[0]}")
            parent = self.conn.execute("SELECT parent_id FROM question WHERE id=?", (parent,)).fetchone()[0]
        if not chain:
            return ""
        sources.append(f"db:ancestors:{aq}")
        return "## 祖先链\n" + "\n".join(chain)

    def _prior_ideas(self, aq, sources) -> str:
        if aq is None:
            return ""
        rows = self.conn.execute(
            "SELECT content_md, status FROM idea WHERE question_id=? ORDER BY id", (aq,)).fetchall()
        if rows:
            sources.append(f"db:ideas:{aq}")
        tried = [f"- [{s}] {c}" for c, s in rows]
        return "## 该问题已试 idea 及结局（防重复造轮）\n" + ("\n".join(tried) or "（无）")

    def _closed_conclusions(self, goal_id, goal_ver, sources) -> str:
        """已关闭结论 + **applicability 徽标**（编译器确定性规则，§4.5.1）。
        跨全部 goal 版本累积（不加 a.goal_ver=? 过滤）——跨版有效性**由徽标传达**：某 v1 结论在 v2 未审
        则无徽标行、不占额度（§4.5.1「无行=无徽标」），有审则渲染 pending/still_applicable/… 徽标。"""
        rows = self.conn.execute(
            "SELECT a.id, a.question_id, a.verdict, q.text FROM answer a JOIN question q ON q.id=a.question_id "
            "WHERE a.goal_id=? ORDER BY a.id", (goal_id,)).fetchall()
        if not rows:
            return "## 已关闭结论\n（无）"
        sources.append("db:answers")
        sources.append("db:answer_applicability")   # 徽标 join 的来源亦入溯源（codex SHOULD）
        lines = []
        for aid, qid, verdict, qtext in rows:
            badge = self._applicability_badge(aid, goal_id, goal_ver)
            lines.append(f"- a{aid}（q{qid} {verdict}）{badge}: {qtext}")
        return "## 已关闭结论（含 applicability 徽标）\n" + "\n".join(lines)

    def _applicability_badge(self, answer_id, goal_id, goal_ver) -> str:
        """join 该 answer 当前 goal_ver 的 answer_applicability 行；无行=无徽标、不占额度。
        needs_revalidation → 附回看题 QN(状态)（§4.5.1 六枚举全渲染）。"""
        r = self.conn.execute(
            "SELECT status, spawned_question_id FROM answer_applicability WHERE answer_id=? AND goal_id=? AND goal_ver=?",
            (answer_id, goal_id, goal_ver)).fetchone()
        if r is None:
            return ""
        status, spawned = r
        # §4.5.1 徽标形态：仅 needs_revalidation 渲 →QN(状态)；contradicted（DDL 亦有 spawned）等其余渲纯枚举。
        if status == "needs_revalidation" and spawned is not None:
            sq = self.conn.execute("SELECT status FROM question WHERE id=?", (spawned,)).fetchone()
            return f" [applicability: needs_revalidation→q{spawned}({sq[0] if sq else '?'})]"
        return f" [applicability: {status}]"

    def _open_set(self, aq, goal_id, sources) -> str:
        """可调度问题集（本 goal 的 open/inconclusive 且无 pending dep），ORDER BY id 定序（护字节一致）。
        **限 goal_id**（防跨 goal 泄漏别目标的问题，codex BLOCKER）。**含本轮 active Qn**（收尾后可重选）——
        对齐 M0 StubCompiler，防单问题场景工人「无题可选」误终止（driver.py 依赖；reasoning skill 亦同批 union，双保险）。"""
        rows = self.conn.execute(
            "SELECT id, text, status, visit_count, score, est_cost FROM question "
            "WHERE goal_id=? AND status IN ('open','inconclusive') AND id NOT IN "
            "(SELECT question_id FROM question_dep WHERE status='pending') ORDER BY id", (goal_id,)).fetchall()
        sources.append("db:schedulable")
        lines = [f"- q{i}（{s}，visit={v}，score={sc}，est_cost={ec}）: {t}"
                 for i, t, s, v, sc, ec in rows]
        if aq is not None:   # 本轮 active Qn 也列入（收尾后重新可选）
            a = self.conn.execute("SELECT text, status, visit_count FROM question WHERE id=?", (aq,)).fetchone()
            if a and a[1] == "active":
                lines.append(f"- q{aq}（active·本轮 Qn，收尾后可重选，visit={a[2]}）: {a[0]}")
        return "## 可调度问题集（open/inconclusive 且无 pending dep；含本轮 Qn）\n" + ("\n".join(lines) or "（空）")

    def _observation_summary(self, ci, sources) -> str:
        """本轮运行观测摘要（§4.7）：从 `execution_observation` 渲机器事实进 reasoning 固定锚（不塞全量 log）。
        经 `execution_log(cycle_id=本轮)` 一跳取本轮观测；ORDER BY eo.id 定序（护字节一致）。
        **只渲机器事实字段**（nan/发散/oom/warning/retry/last_loss/loss_trend/wall_clock_sec）——
        `wall_clock_sec` 是 parser 从日志解析的**观测值**（P6 可回放、同快照同值），**非**行 `created_at`
        （插入 wall-clock，绝不渲，否则破字节一致）。source='codex' 行按 DDL CHECK 机器列恒 NULL、只带
        digest_ref → 单独标注「codex 摘要」不冒充机器事实（§3.1.2）。

        **铁律（§3.1.2 原样，硬约束）**：观测摘要进锚点后**只影响调试 / 复现 / 下一步评估计划，不得作
        novelty / success / correctness / 关问题的选择输入**（防 log 经 reasoning 间接绕过门禁）。header
        显式声明此约束，提示 reasoning 工人。真正的隔离强制在门禁侧（authorizer 拒读 execution_observation，
        §CP2.3）——本段只负责「诚实渲染 + 用途声明」，不承担门禁职责。"""
        rows = self.conn.execute(
            "SELECT eo.id, eo.source, eo.nan_seen, eo.divergence_flag, eo.oom_count, eo.warning_count, "
            "eo.retry_count, eo.last_loss, eo.loss_trend, eo.wall_clock_sec, eo.digest_ref, el.log_kind "
            "FROM execution_observation eo JOIN execution_log el ON el.id = eo.execution_log_id "
            "WHERE el.cycle_id=? ORDER BY eo.id", (ci,)).fetchall()
        header = ("## 本轮运行观测摘要（§4.7）\n"
                  "> 用途限定：仅供**解释指标可信度 / 失败模式 / 调试 / 复现 / 下一步评估计划**；"
                  "**不得作 novelty / success / correctness / 关问题的选择输入**（I3 铁律，§3.1.2）。")
        if not rows:
            return header + "\n（本轮无运行观测）"
        sources.append(f"db:execution_observation:{ci}")
        lines = []
        for (oid, src, nan, div, oom, warn, retry, last_loss, trend, wall, digest, kind) in rows:
            if src == "codex":   # 机器列恒 NULL（DDL CHECK）→ 只报 digest，不冒充观测事实
                lines.append(f"- [obs{oid}·codex 摘要·{kind}] digest_ref={digest}")
            else:                # parser：机器事实
                lines.append(
                    f"- [obs{oid}·parser·{kind}] nan={nan} 发散={div} oom={oom} warning={warn} "
                    f"retry={retry} last_loss={last_loss} loss_trend={trend} wall_clock_sec={wall}")
        return header + "\n" + "\n".join(lines)

    def _budget(self) -> float:
        return compute_budget(self.conn, self.policy["budget"])   # 唯一定义在 budgeting.compute_budget（防漂移）
