"""StubCompiler / StubCtx / StubRecall —— 上下文供给的 M0 桩（M2 换真，签名不变）。

M0 桩的两条硬纪律（M0 验收依据）：
1. **输入只来自上一阶段 contract**：四区包的固定锚只从「StateStore 状态 + 上一阶段已
   过门禁的产物」渲染（ArtifactIndex），来源清单落 manifest（pack 溯源，验收断言②的证据）。
2. **确定性**：同状态 + 同产物 → 字节一致（无时间戳 / 随机量；pack_hash = sha256）。

manifest 落轮目录 context_pack/<stage>[.<target>].manifest.json（图 01：包哈希+分区
manifest 留痕、快照归档；M1 起随 DECISION 入账）。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .gate import ArtifactIndex
from .interfaces import ContextPack, Hit, RecallSpec, Ref, Stage


class StubCtx:
    """按 ref 深潜——M0 假数据：固定占位文本（M2 接真冷数据）。"""

    def fetch(self, ref: Ref) -> str:
        return f"（M0 桩：ref={ref} 的冷数据深潜在 M2 接入，此处无内容）"


class StubRecall:
    """渐进四级召回——M0 假数据：恒空命中（池为空是 M0 事实，非桩造假）。"""

    def query(self, spec: RecallSpec) -> List[Hit]:
        return []


class StubCompiler:
    def __init__(self, statestore, index: ArtifactIndex, policy: Dict[str, Any],
                 goal_body_md: str, cycles_root: Path):
        self.state = statestore
        self.index = index
        self.policy = policy
        self.goal_body_md = goal_body_md
        self.cycles_root = cycles_root

    # -- Compiler Protocol -------------------------------------------------------
    def render(self, *, cycle_id: str, stage: Stage, target_id: Optional[str] = None) -> ContextPack:
        cycle = self.state.cycles[cycle_id]
        sources: List[str] = []
        anchor = self._anchor(cycle_id, stage, target_id, sources)
        pack = ContextPack(
            cycle_id=cycle_id, stage=stage, target_id=target_id,
            anchor_md=anchor,
            neighborhood_md=self._neighborhood(cycle, sources),
            retrieval_md="（M0：池空，召回无命中）",
            refs=[],
        )
        pack.pack_hash = hashlib.sha256(
            (pack.anchor_md + pack.neighborhood_md + pack.retrieval_md).encode("utf-8")
        ).hexdigest()
        self._write_manifest(cycle_id, stage, target_id, pack, sources)
        return pack

    # -- 分区渲染 -----------------------------------------------------------------
    def _anchor(self, cycle_id: str, stage: Stage, target_id: Optional[str],
                sources: List[str]) -> str:
        cycle = self.state.cycles[cycle_id]
        parts: List[str] = [f"route={cycle.route}；本轮 cycle={cycle_id}"]
        qn = cycle.question_id and self.state.questions.get(cycle.question_id)
        if qn:
            parts.append(f"## 本轮问题卡 Qn\n- id: {qn.qid}\n- 问题: {qn.text}\n"
                         f"- 状态: {qn.status}（visit={qn.visit_count}）")
            sources.append(f"state:question:{qn.qid}")
        if stage == "idea":
            parts.append(self._prior_ideas(qn, sources))
        if stage == "plan":
            idea = self.index.get(cycle_id, "idea", "idea_set.json")
            if idea:
                parts.append("## normalized selected idea（上一阶段契约）\n```json\n"
                             + json.dumps(self._normalize_selected(idea), ensure_ascii=False, indent=2)
                             + "\n```")
                sources.append(f"artifact:{cycle_id}:idea:idea_set.json")
            parts.append(f"## 单轮预算\nB(t) = {self._budget()}（policy budget 节）")
            sources.append("policy:budget")
        if stage == "bundle" and target_id:
            plan = self.index.get(cycle_id, "plan", "plan.json")
            if plan:
                slice_ = self._plan_slice(plan, target_id)
                parts.append("## 计划切片（本 target + 协议 + required_metric）\n```json\n"
                             + json.dumps(slice_, ensure_ascii=False, indent=2) + "\n```")
                sources.append(f"artifact:{cycle_id}:plan:plan.json#target={target_id}")
        if stage == "reasoning":
            parts.append(f"## 目标全文（当前版 v{self.state.goal['ver']}）\n" + self.goal_body_md)
            sources.append("input:goal_brief.md")
            parts.append(self._bundle_results(cycle_id, sources))
            parts.append(self._open_set(sources))
            parts.append("## 采集打分参数\n```json\n"
                         + json.dumps({"acquisition": self.policy["acquisition"],
                                       "B_t": self._budget(),
                                       "tau": self.policy["flow"]["tau"]}, ensure_ascii=False) + "\n```")
            sources.append("policy:acquisition")
        return "\n\n".join(p for p in parts if p)

    def _neighborhood(self, cycle, sources: List[str]) -> str:
        qn = cycle.question_id and self.state.questions.get(cycle.question_id)
        if not qn or qn.parent_id is None:
            return ""
        chain, seen = [], set()
        pid = qn.parent_id
        while pid and pid not in seen:   # seen 防御：正常创建路径不产环，坏数据也不挂死
            seen.add(pid)
            p = self.state.questions.get(pid)
            if p is None:
                chain.append(f"- （坏引用 parent={pid}，链在此截断）")
                break
            chain.append(f"- {p.qid}（{p.status}）: {p.text}")
            pid = p.parent_id
        sources.append("state:ancestors:" + qn.qid)
        return "## 祖先链\n" + "\n".join(chain)

    def _prior_ideas(self, qn, sources: List[str]) -> str:
        if not qn:
            return ""
        tried = []
        # 按 (cycle_id, filename) 稳定排序遍历：pack 字节一致性不依赖登记顺序（§6.6 确定性）
        for (cid, stage, _t, fname) in sorted(self.index.records,
                                              key=lambda k: (k[0], str(k[2]), k[3])):
            payload = self.index.records[(cid, stage, _t, fname)]
            if stage == "idea" and fname == "idea_set.json":
                owner = self.state.cycles[cid].question_id
                if owner == qn.qid:
                    for c in payload["candidates"]:
                        verdict = "selected" if c["candidate_id"] == payload.get("selected_id") else "failed"
                        tried.append(f"- [{verdict}] {c['core_claim']}")
                    sources.append(f"artifact:{cid}:idea:idea_set.json")
        return "## 该问题已试 idea 及结局（防重复造轮）\n" + ("\n".join(tried) or "（无）")

    def _bundle_results(self, cycle_id: str, sources: List[str]) -> str:
        rows = []
        for (cid, stage, target_id, fname) in sorted(self.index.records,
                                                     key=lambda k: (k[0], str(k[2]), k[3])):
            payload = self.index.records[(cid, stage, target_id, fname)]
            if cid == cycle_id and stage == "bundle" and fname == "bundle_target.json":
                obs = payload.get("execution_observation") or {}
                rows.append(f"- target={target_id} status={payload['status']}"
                            + (f" failure={payload.get('failure_kind')}" if payload.get("failure_kind") else "")
                            + "；指标: " + json.dumps(payload.get("metric_results") or [], ensure_ascii=False)
                            + f"；观测摘要: loss_trend={obs.get('loss_trend')} nan={obs.get('nan_seen')}"
                              f" synthetic={obs.get('synthetic')}")
                sources.append(f"artifact:{cycle_id}:bundle:{target_id}")
        plan = self.index.get(cycle_id, "plan", "plan.json")
        reuse = ""
        if plan and plan.get("reuse_evidence"):
            reuse = "\n复用证据: " + json.dumps(plan["reuse_evidence"], ensure_ascii=False)
            sources.append(f"artifact:{cycle_id}:plan:plan.json#reuse_evidence")
        return "## 本轮 bundle 结果（含运行观测摘要；观测只作解释、不作证据）\n" + ("\n".join(rows) or "（本轮无执行目标）") + reuse

    def _open_set(self, sources: List[str]) -> str:
        rows = [f"- {q['question_id']}（{q['status']}，visit={q['visit_count']}，"
                f"score={q['score']}，est_cost={q['est_cost']}）: {q['text']}"
                for q in self.state.list_schedulable_questions()]
        sources.append("state:schedulable_questions")
        return "## 可调度问题集（open/inconclusive 且无 pending dep）\n" + ("\n".join(rows) or "（空）")

    # -- 工具 ----------------------------------------------------------------------
    @staticmethod
    def _normalize_selected(idea_set: Dict[str, Any]) -> Dict[str, Any]:
        """plan 只消费 normalized selected（§3.2.4）；wildidea_extra 不进 plan。"""
        sid = idea_set.get("selected_id")
        cand = next((c for c in idea_set["candidates"] if c["candidate_id"] == sid), None)
        if cand is None:
            return {}
        return {k: cand[k] for k in ("candidate_id", "core_claim", "mechanism", "assumptions",
                                     "min_falsifiable_experiment", "audit_mapping", "novelty_type")
                if k in cand} | {"novelty_refs": idea_set.get("novelty_refs", [])}

    @staticmethod
    def _plan_slice(plan: Dict[str, Any], target_id: str) -> Dict[str, Any]:
        target = next((t for t in plan["targets"] if t["target_key"] == target_id), None)
        return {
            "target": target,
            "protocol": plan.get("protocol"),
            "metric_defs": plan.get("metric_defs"),
            "readout_rules": plan.get("readout_rules"),
            "required_metrics": [m for m in plan.get("build_target_required_metric", [])
                                 if m["target_key"] == target_id],
        }

    def _budget(self) -> float:
        b = self.policy["budget"]
        n = max(0, len([c for c in self.state.cycles.values() if c.status == "done"]))
        return min(b["B0"] * (2 ** (n // b["doubling_period_m"])), b["B_max"])

    def _write_manifest(self, cycle_id: str, stage: Stage, target_id: Optional[str],
                        pack: ContextPack, sources: List[str]) -> None:
        d = self.cycles_root / f"cycles/{cycle_id}/context_pack"
        d.mkdir(parents=True, exist_ok=True)
        name = f"{stage}{'.' + target_id if target_id else ''}"
        manifest = {"pack_hash": pack.pack_hash, "stage": stage, "target_id": target_id,
                    "sources": sorted(set(sources))}
        (d / f"{name}.manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        (d / f"{name}.pack.md").write_text(
            pack.anchor_md + "\n\n" + pack.neighborhood_md + "\n\n" + pack.retrieval_md,
            encoding="utf-8")
