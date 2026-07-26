"""InMemoryStateStore —— StateStore 桩（M0：内存 dict；M1 落 SQLite，签名不变）。

语义对齐《第一部分》§4.2（状态机 / 调度可见性 / 写函数拒绝判据）：
- question：open ⇄ active；关闭 → answered/refuted/dead_end（终态不可重开）；inconclusive 非终态。
- 调度可见性：status ∈ {open, inconclusive} 且无 pending dep；inconclusive 对 attack 另受
  max_inconclusive_per_question（policy question_guard）。
- decompose：add_children 同一"事务"内 = 写子问题 + 父 active→open 释放（不增 visit）+
  逐子 question_dep(pending)；子问题 answered/refuted → resolve_deps 置 satisfied，
  全 satisfied 后父问题回可调度集（聚合轮）。
- persist_selection 是 question.score / est_cost 的唯一维护点。

M0 简化边界（写明防误用）：单进程单驱动器；无并发；ID 为确定性递增（q1/q2…、c1/c2…）；
拒绝判据以 ValueError 表达（M1 起变为 DB 触发器/门禁拒绝 + DECISION(reject)）。
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .interfaces import Cycle, Route, Selection
from .question_admission import (
    ALLOWED_QUESTION_EVIDENCE,
    admission_payload,
    normalize_question_contract,
)

_TERMINAL_Q = {"answered", "refuted", "dead_end"}
_SPAWN_SOURCE = {  # §4.2.4 spawn_question kind → question.source 映射
    "diagnosis": "agent", "followup": "agent", "revalidate": "revalidate",
    "import_reference": "agent", "goal_retarget": "goal_amend",
}


@dataclass
class Question:
    qid: str
    text: str
    parent_id: Optional[str]
    source: str = "agent"
    status: str = "open"
    goal_ver: int = 1
    visit_count: int = 0
    decompose_count: int = 0
    score: Optional[float] = None
    est_cost: Optional[float] = None
    predicate_json: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Dep:
    question_id: str          # 被阻塞的问题（如 decompose 的父问题）
    dep_type: str             # question | baseline
    target: str               # 依赖的 question_id / baseline 键
    status: str = "pending"   # pending | satisfied | blocked


@dataclass
class Answer:
    answer_id: str
    question_id: str
    verdict: str
    goal_ver: int
    evidence: List[Dict[str, Any]] = field(default_factory=list)


class InMemoryStateStore:
    def __init__(self, policy: Dict[str, Any]):
        self.policy = policy
        self.goal: Optional[Dict[str, Any]] = None
        self.questions: Dict[str, Question] = {}
        self.deps: List[Dep] = []
        self.answers: Dict[str, Answer] = {}
        self.applicability: List[Dict[str, Any]] = []
        self.cycles: Dict[str, Cycle] = {}
        self.decisions: List[Dict[str, Any]] = []   # M0 审计留痕（M1 落 DECISION 表）
        self._q_no = 0
        self._c_no = 0
        self._a_no = 0
        self._bundle_cursor: Dict[str, Optional[str]] = {}
        self._local_maps: Dict[str, Dict[str, str]] = {}   # cycle_id → 同批 local_key→qid（§6.10：StateStore 同事务解析）
        self._reviews_in_cycle: Dict[str, int] = {}        # cycle_id → 本轮 mark_answer_applicability 次数（answer_review 护栏）
        self._spawns_in_cycle: Dict[str, int] = {}         # cycle_id → goal_amend 轮 spawn 次数（max_spawn_from_goal_amend 护栏）

    # -- 内部 -----------------------------------------------------------------
    def _next_qid(self) -> str:
        self._q_no += 1
        return f"q{self._q_no}"

    def _record(self, actor: str, dtype: str, payload: Dict[str, Any]) -> None:
        self.decisions.append({"actor": actor, "type": dtype, "payload": payload})

    def _pending_deps(self, qid: str) -> List[Dep]:
        return [d for d in self.deps if d.question_id == qid and d.status == "pending"]

    def is_schedulable(self, qid: str, *, for_intent: str = "attack") -> bool:
        q = self.questions[qid]
        if q.status not in ("open", "inconclusive"):
            return False
        if self._pending_deps(qid):
            return False
        if q.status == "inconclusive" and for_intent == "attack":
            limit = self.policy["question_guard"]["max_inconclusive_per_question"]
            if q.visit_count >= limit:
                return False   # 到限：对 attack 不可选，仅可作 decompose / propose_prune 对象
        return True

    # -- StateStore Protocol ----------------------------------------------------
    def create_goal(self, *, text: str, predicate_json: Dict[str, Any]) -> str:
        if self.goal is not None:
            raise ValueError("goal 已存在（后续演化走 goal_amend）")
        self.goal = {"goal_id": "g1", "ver": 1, "text": text, "predicate_json": predicate_json}
        self._record("orchestrator", "goal_bootstrap", {"goal_id": "g1"})
        return "g1"

    def open_or_resume_cycle(self) -> Cycle:
        for c in self.cycles.values():
            if c.status not in ("done", "failed", "aborted"):
                return c
        self._c_no += 1
        c = Cycle(cycle_id=f"c{self._c_no}")
        self.cycles[c.cycle_id] = c
        return c

    def set_route(self, cycle_id: str, route: Route) -> None:
        c = self.cycles[cycle_id]
        if c.status in ("done", "failed", "aborted"):
            raise ValueError(f"cycle 已终态（{c.status}），不得改 route")
        if route not in ("bootstrap", "attack", "decompose", "reuse_only",
                         "eval_only", "goal_amend", "dependency_wait"):
            raise ValueError(f"route ∉ 7 形态: {route}")
        c.route = route

    def _resolve_local(self, cycle_id: str, key: Optional[str]) -> Optional[str]:
        """同批 local_key → 真实 qid（§6.10：由 StateStore 同一事务内解析后再校验）。"""
        if key is None:
            return None
        return self._local_maps.get(cycle_id, {}).get(key, key)

    def persist_selection(self, cycle_id: str, sel: Selection) -> None:
        if sel.next_intent not in ("attack", "decompose", "terminate"):
            raise ValueError(f"next_intent 非法: {sel.next_intent}")
        next_qid = self._resolve_local(cycle_id, sel.next_question_id)
        if sel.next_intent == "terminate":
            if next_qid is not None:
                raise ValueError("terminate 时 next_question_id 必须为 null")
        else:
            if not next_qid or next_qid not in self.questions:
                raise ValueError(f"next_question_id 缺失或不存在: {sel.next_question_id}")
            if not self.is_schedulable(next_qid, for_intent=sel.next_intent):
                raise ValueError(f"目标问题不可调度: {next_qid}")
        for row in sel.scores:   # 唯一维护点：写回 question.score / est_cost（local_key 同样解析）
            rid = self._resolve_local(cycle_id, row.get("question_id", "")) or ""
            q = self.questions.get(rid)
            if q is None:
                raise ValueError(f"scores 引用的问题不存在: {row.get('question_id')}（不静默丢弃）")
            missing = [field for field in ("score", "est_cost") if field not in row]
            if missing:
                raise ValueError(f"scores.{rid} 缺 schema 必填字段: {missing}")
            q.score = row["score"]
            q.est_cost = row["est_cost"]
        c = self.cycles[cycle_id]
        c.next_question_id = next_qid
        c.next_intent = sel.next_intent

    def _tree_snapshot(self, cycle_id: str) -> Dict[str, Any]:
        """apply_tree_ops 的事务快照（M0 内存版回滚；M1 由 DB 事务替代）。"""
        return {
            "questions": copy.deepcopy(self.questions),
            "deps": copy.deepcopy(self.deps),
            "applicability": copy.deepcopy(self.applicability),
            "goal": copy.deepcopy(self.goal),
            "local": copy.deepcopy(self._local_maps.get(cycle_id, {})),
            "reviews": self._reviews_in_cycle.get(cycle_id, 0),
            "spawns": self._spawns_in_cycle.get(cycle_id, 0),
            "decisions_len": len(self.decisions),
            "q_no": self._q_no,
        }

    def _tree_restore(self, cycle_id: str, snap: Dict[str, Any]) -> None:
        self.questions = snap["questions"]
        self.deps = snap["deps"]
        self.applicability = snap["applicability"]
        self.goal = snap["goal"]
        self._local_maps[cycle_id] = snap["local"]
        self._reviews_in_cycle[cycle_id] = snap["reviews"]
        self._spawns_in_cycle[cycle_id] = snap["spawns"]
        del self.decisions[snap["decisions_len"]:]
        self._q_no = snap["q_no"]

    def apply_tree_ops(self, cycle_id: str, ops: List[Dict[str, Any]]) -> None:
        """执行封闭七词表（-> None，签名冻结 §6.10）。

        事务语义（§4.2.5）：任一 op 被拒 → 整批回滚、不留半写。
        同批 local_key→qid 映射暂存于 StateStore（per-cycle 累积），由同一"事务"内的
        persist_selection / mark_answer_applicability 解析——调用方不做穿线。
        """
        snap = self._tree_snapshot(cycle_id)
        try:
            self._apply_tree_ops_inner(cycle_id, ops)
        except Exception:
            self._tree_restore(cycle_id, snap)
            raise

    def _apply_tree_ops_inner(self, cycle_id: str, ops: List[Dict[str, Any]]) -> None:
        cycle = self.cycles[cycle_id]
        local: Dict[str, str] = self._local_maps.setdefault(cycle_id, {})
        guard = self.policy["tree_guard"]
        for op in ops:
            kind = op.get("op")
            if kind == "create_root":
                if cycle.route != "bootstrap":
                    # goal 改版下的新 root 走 spawn_question(kind=goal_retarget)，不走 create_root
                    raise ValueError("create_root 仅限 bootstrap 轮（§4.2.4）")
                text, contract, contract_source = normalize_question_contract(
                    op.get("text"), op.get("predicate_json"))
                qid = self._next_qid()
                self.questions[qid] = Question(
                    qid, text, None, "agent", goal_ver=self.goal["ver"],
                    predicate_json=contract)
                if op.get("local_key"):
                    local[op["local_key"]] = qid
                self._record("agent", "question_admission", admission_payload(
                    qid=qid, operation="create_root", text=text, contract=contract,
                    contract_source=contract_source))
                self._record("agent", "create_root", {"qid": qid})
            elif kind == "add_children":
                if cycle.route != "decompose":
                    raise ValueError("add_children 仅限 decompose 轮（§4.2.4）")
                parent = self.questions.get(op["parent_question_id"])
                if parent is None:
                    raise ValueError(f"decompose 父问题不存在: {op['parent_question_id']}")
                if parent.status != "active":
                    raise ValueError(f"decompose 父问题须为 active（当前 {parent.status}；终态/未选中不可分解）")
                max_depth = guard.get("max_decompose_depth")
                if max_depth is not None and self._depth_of(parent.qid) + 1 > max_depth:
                    raise ValueError("超出 max_decompose_depth")
                children = op["children"]
                if len(children) > guard["max_children_per_node"]:
                    raise ValueError("超出 max_children_per_node")
                if self._open_count() + len(children) > guard["max_open_questions"]:
                    raise ValueError("超出 max_open_questions")
                for ch in children:
                    text, contract, contract_source = normalize_question_contract(
                        ch.get("text"), ch.get("predicate_json"))
                    qid = self._next_qid()
                    self.questions[qid] = Question(
                        qid, text, parent.qid, "decompose", goal_ver=self.goal["ver"],
                        predicate_json=contract)   # source 对齐 DDL 枚举
                    local[ch["local_key"]] = qid
                    self.deps.append(Dep(parent.qid, "question", qid))
                    self._record("agent", "question_admission", admission_payload(
                        qid=qid, operation="add_children", text=text, contract=contract,
                        contract_source=contract_source))
                parent.decompose_count += 1
                parent.status = "open"             # 同事务释放（等待非尝试、不增 visit）
                self._record("agent", "decompose", {"parent": parent.qid,
                                                    "children": [local[c_["local_key"]] for c_ in children]})
            elif kind == "spawn_question":
                if op.get("request_ref") is not None:
                    # Trusted request refs are DB append-only identities.  The
                    # M0 in-memory store has no directive/completion ledger and
                    # must fail closed instead of pretending it bound one.
                    raise ValueError(
                        "request_ref 建题需要 SQLiteStateStore 的 durable provenance")
                if self._open_count() >= guard["max_open_questions"]:
                    raise ValueError("超出 max_open_questions")
                if cycle.route == "goal_amend":
                    cap = self.policy["goal_amend"]["max_spawn_from_goal_amend"]
                    if self._spawns_in_cycle.get(cycle_id, 0) + 1 > cap:
                        raise ValueError("超出 max_spawn_from_goal_amend（goal_amend 护栏）")
                    self._spawns_in_cycle[cycle_id] = self._spawns_in_cycle.get(cycle_id, 0) + 1
                parent = op.get("parent_question_id")
                if op["kind"] == "goal_retarget":
                    if cycle.route != "goal_amend":
                        raise ValueError("goal_retarget 仅限 goal_amend 轮（§4.2.4）")
                    if parent is not None:
                        raise ValueError("goal_retarget 必须 parent=null")
                elif parent not in self.questions:
                    raise ValueError(f"spawn parent 不存在: {parent}")
                text, contract, contract_source = normalize_question_contract(
                    op.get("text"), op.get("predicate_json"))
                qid = self._next_qid()
                self.questions[qid] = Question(
                    qid, text, parent, _SPAWN_SOURCE[op["kind"]],
                    goal_ver=self.goal["ver"], predicate_json=contract)
                if op.get("local_key"):
                    local[op["local_key"]] = qid
                self._record("agent", "question_admission", admission_payload(
                    qid=qid, operation="spawn_question", text=text, contract=contract,
                    contract_source=contract_source))
                self._record("agent", "spawn_question", {"qid": qid, "kind": op["kind"]})
            elif kind == "mark_answer_applicability":
                self._mark_applicability(cycle_id, op, local)
            elif kind == "propose_prune":
                q = self.questions[op["question_id"]]
                if q.status not in ("open", "inconclusive"):
                    raise ValueError(f"仅 open/inconclusive 可剪枝: {q.qid}({q.status})")
                self._record("agent", "prune_branch", {"qid": q.qid, "reason": op["reason_md"]})
                q.status = "dead_end"              # decision 先行、再置 dead_end（§4.2.4）
                for dep in self.deps:
                    if (dep.dep_type == "question" and dep.target == q.qid
                            and dep.status == "pending"):
                        dep.status = "blocked"      # impossible child releases parent to replan
            elif kind == "amend_goal":
                if cycle.route != "goal_amend":
                    raise ValueError("amend_goal 仅限 goal_amend 轮")
                if any(q.status == "active" for q in self.questions.values()):
                    raise ValueError("goal_amend 轮不得携 active 问题")
                if not isinstance(op.get("predicate_json"), dict):
                    raise ValueError("amend_goal.predicate_json 须为 object")
                self.goal["ver"] += 1
                self.goal["text"] = op["new_goal_text"]
                self.goal["predicate_json"] = op["predicate_json"]
                for q in self.questions.values():
                    if q.status in ("open", "inconclusive"):
                        q.goal_ver = self.goal["ver"]
                        q.score = None
                        q.est_cost = None
                self._record("agent", "goal_amend", {"new_ver": self.goal["ver"]})
            elif kind == "seed_applicability_audit":
                if cycle.route != "goal_amend":
                    raise ValueError("seed_applicability_audit 仅限 goal_amend 轮")
                limit = self.policy["goal_amend"]["max_closed_revalidate_per_cycle"]
                if len(op["answer_ids"]) > limit:
                    raise ValueError("超出 max_closed_revalidate_per_cycle")
                for aid in op["answer_ids"]:
                    self.applicability.append({"answer_id": aid, "goal_ver": self.goal["ver"],
                                               "status": "pending", "rationale_md": op.get("rationale_md", "")})
            else:
                raise ValueError(f"op 不在封闭词表: {kind}")

    def _mark_applicability(self, cycle_id: str, op: Dict[str, Any], local: Dict[str, str]) -> None:
        """§4.2.4 绑定判据全量：answer 存在 + 回看题 source=revalidate 且 parent=被回看
        answer 所属问题 + 每轮回看上限（policy answer_review）。

        计数在全部校验通过后才累加（对齐 M1 事务回滚语义：被拒的写不消耗配额）。"""
        if self._reviews_in_cycle.get(cycle_id, 0) + 1 > self.policy["answer_review"]["max_reviews_per_cycle"]:
            raise ValueError("超出 max_reviews_per_cycle（answer_review 护栏）")
        answer = self.answers.get(op["answer_id"])
        if answer is None:
            raise ValueError(f"answer 不存在: {op['answer_id']}")
        status = op["status"]
        spawned = op.get("spawned_question_ref")
        if status in ("needs_revalidation", "contradicted"):
            qid = local.get(spawned, spawned)
            q = self.questions.get(qid)
            if q is None or q.source != "revalidate":
                raise ValueError("needs_revalidation/contradicted 须指向 source=revalidate 的回看题")
            if q.parent_id != answer.question_id:
                raise ValueError("回看题 parent 须为被回看 answer 所属问题（§4.2.4）")
        row = {"answer_id": op["answer_id"], "goal_ver": self.goal["ver"],
               "status": status, "rationale_md": op["rationale_md"],
               "spawned_question_id": local.get(spawned, spawned)}
        # UNIQUE(answer, goal, ver) 语义：同版行覆盖更新，历史留痕走 decision
        self.applicability = [r for r in self.applicability
                              if not (r["answer_id"] == row["answer_id"] and r["goal_ver"] == row["goal_ver"])]
        self.applicability.append(row)
        self._reviews_in_cycle[cycle_id] = self._reviews_in_cycle.get(cycle_id, 0) + 1
        self._record("agent", "answer_review", row)

    def close_question(self, cycle_id: str, question_id: str, verdict: str,
                       evidence: List[Dict[str, Any]], answer_md: str) -> str:
        """gate_close_question 的状态迁移侧（M0：业务判据在 Gate 桩放过，仅结构约束）。"""
        q = self.questions[question_id]
        if q.status in _TERMINAL_Q:
            raise ValueError(f"终态问题不可重关: {question_id}")
        if not evidence:
            raise ValueError("I3：关问需 ≥1 条证据")
        allowed = set(q.predicate_json.get("allowed_evidence", ALLOWED_QUESTION_EVIDENCE))
        disallowed = sorted({ev.get("kind") for ev in evidence if ev.get("kind") not in allowed},
                            key=lambda value: str(value))
        if disallowed:
            raise ValueError(
                f"I3：证据类型不在 question closure contract 中: {disallowed}；"
                f"allowed={sorted(allowed)}")
        self._a_no += 1
        aid = f"a{self._a_no}"
        self.answers[aid] = Answer(aid, question_id, verdict, self.goal["ver"], evidence)
        q.status = "answered" if verdict == "answered" else "refuted"
        self.resolve_deps()
        self._record("agent", "close_question", {"qid": question_id, "answer": aid, "verdict": verdict})
        return aid

    def mark_inconclusive(self, question_id: str) -> None:
        q = self.questions[question_id]
        if q.status != "active":
            raise ValueError(f"仅 active 可置 inconclusive: {question_id}({q.status})")
        q.status = "inconclusive"
        q.visit_count += 1

    def release_question(self, question_id: str) -> None:
        """cycle failed/aborted 或 dependency_wait 释放：active→open、不增 visit（§4.2.3）。"""
        q = self.questions[question_id]
        if q.status == "active":
            q.status = "open"

    def mark_cycle_done(self, cycle_id: str, status: str = "done") -> None:
        if status not in ("done", "failed", "aborted"):
            raise ValueError(f"cycle 终态非法: {status}")
        self.cycles[cycle_id].status = status

    def activate_question(self, question_id: str) -> None:
        if not self.is_schedulable(question_id, for_intent="attack") \
           and not self.is_schedulable(question_id, for_intent="decompose"):
            raise ValueError(f"问题不可调度: {question_id}")
        self.questions[question_id].status = "active"

    def record_question_dep(self, question_id: str, *, dep_type: str, target: str) -> None:
        if dep_type == "question" and target == question_id:
            raise ValueError("禁自依赖")
        if question_id not in self.questions:
            raise ValueError(f"dep 主体问题不存在: {question_id}")
        if dep_type == "question" and target not in self.questions:
            raise ValueError(f"dep 目标不存在: {target}（§4.2.4 拒因；防静默不可满足依赖）")
        # baseline dep：M0 无池、目标存在性校验留缝（M1 起对 baseline 表核）
        self.deps.append(Dep(question_id, dep_type, target))

    def resolve_deps(self) -> None:
        for d in self.deps:
            if d.status != "pending":
                continue
            if d.dep_type == "question":
                tq = self.questions.get(d.target)
                if tq and tq.status in ("answered", "refuted"):
                    d.status = "satisfied"
            # baseline dep：M1+（baseline→legal 机械 satisfied）

    def list_schedulable_questions(self) -> List[Dict[str, Any]]:
        out = []
        for q in self.questions.values():
            if self.is_schedulable(q.qid, for_intent="attack") \
               or self.is_schedulable(q.qid, for_intent="decompose"):
                out.append({"question_id": q.qid, "text": q.text, "status": q.status,
                            "visit_count": q.visit_count, "score": q.score,
                            "est_cost": q.est_cost, "parent_id": q.parent_id})
        return out

    def consume_directive(self, directive_id: str) -> None:
        raise NotImplementedError("directive 收件箱 M5 落地（M0 无人机入站）")

    def bundle_cursor(self, cycle_id: str) -> Optional[str]:
        return self._bundle_cursor.get(cycle_id)

    def set_bundle_cursor(self, cycle_id: str, target_key: Optional[str]) -> None:
        self._bundle_cursor[cycle_id] = target_key

    def _open_count(self) -> int:
        return sum(1 for q in self.questions.values() if q.status in ("open", "inconclusive"))

    def _depth_of(self, qid: str) -> int:
        """根=0；沿 parent 链计深（max_decompose_depth 护栏用；seen 防坏数据环）。"""
        depth, seen = 0, set()
        pid = self.questions[qid].parent_id
        while pid and pid not in seen:
            seen.add(pid)
            depth += 1
            pid = self.questions[pid].parent_id if pid in self.questions else None
        return depth
