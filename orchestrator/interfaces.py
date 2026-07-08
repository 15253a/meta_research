"""流程层 ↔ 资产层的唯一接口缝（《第二部分》§6.10）。

铁律：流程层（skill + system_prompt + 驱动器）只经本文件的接口读写状态——
**状态永远在接口之后、不在会话 / prompt 里**（§6.1）。M0 用桩、M1+ 换真实现，
**签名不变**；桩→真替换矩阵见《第二部分》§6.10。改动本文件 = 决策性改动，
必须走检查点评审（CLAUDE.md §1）。

数据形状约定：阶段产物 = 「合 schema 的 JSON + 中文 md 正文」（§6.4）；一个阶段
可产多份 JSON 文件（reasoning = answer/tree_ops/selection；任一阶段可附 sidecar
resource_request.json，§3.1.1），故 Artifact 以「文件名 → JSON 对象」承载。

运行时 Python 3.9：注解统一用 typing.Optional/List/Dict（不用 3.10+ 的 | 语法）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Protocol, Union

# ---------------------------------------------------------------------------
# 基础类型（对齐《第一部分》术语锁定 §0.2 / route 七形态 §2.3）
# ---------------------------------------------------------------------------

Stage = Literal["idea", "plan", "bundle", "reasoning"]

Route = Literal[
    "bootstrap", "attack", "decompose", "reuse_only",
    "eval_only", "goal_amend", "dependency_wait",
]  # terminate 不是 route（§2.3）

Intent = Literal["attack", "decompose", "terminate"]

Ref = str  # ctx-fetch 深潜引用（execution_log.ref / identity / src 等，§3.6.2 第 4 级）


@dataclass
class ContextPack:
    """上下文编译器产出的四区包（§4.5.1）：①固定锚 ②结构邻域 ③检索区 ④引用区。

    确定性契约（M2 起强制）：同快照 + 同配方 + 同预算 + 同 target → 字节一致。
    """
    cycle_id: str
    stage: Stage
    target_id: Optional[str]           # bundle 逐目标时非空（§6.10 参数名；M0 装载 plan 的 target_key 值，M1 起 DB build_target id）
    anchor_md: str                     # 固定锚：任务关键集必含、不截断（§3.1.1）
    neighborhood_md: str               # 结构邻域：受限血缘 / 依赖卡
    retrieval_md: str                  # 检索区：按配方召回 top-k（有额度）
    refs: List[Ref] = field(default_factory=list)   # 引用区：可深潜的 ref 清单
    pack_hash: str = ""                # 包哈希（写 DECISION / 归档回放，P6）
    sources: List[str] = field(default_factory=list)   # 溯源来源清单（确定性排序）；manifest 从此取，使溯源是 pack 的纯函数（M2）


@dataclass
class Artifact:
    """一次阶段执行的全部产物：文件名 → 合 schema 的 JSON 对象，外加中文 md 正文。

    files 的键 = 产物文件名（如 "idea_set.json" / "answer.json"），值 = 待校验 JSON；
    Gate 逐文件按 schemas/ 对应 schema 校验。md 为该阶段的中文正文（可空）。
    """
    stage: Stage
    files: Dict[str, Any]
    md: str = ""


@dataclass
class ValidationResult:
    ok: bool
    errors: List[str] = field(default_factory=list)


@dataclass
class CommitResult:
    ok: bool
    errors: List[str] = field(default_factory=list)
    committed_files: List[str] = field(default_factory=list)


@dataclass
class Selection:
    """reasoning R3 产物（§2.3 / §4.2.4）：只选下一步、不写 route。

    terminate ⇒ next_question_id 必为 None（持久停机标志）。
    scores 逐项 {question_id, score, est_cost, ...}（写回 question 的唯一维护点）。
    """
    next_question_id: Optional[str]
    next_intent: Intent
    scores: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class PlanOutcome:
    """plan 落库后的结果分类（derive_next_route 的输入，《第二部分》§6.13(3) 矩阵）。"""
    has_build_or_exec: bool = False    # targets 含 build/exec → attack
    only_eval: bool = False            # targets 仅 eval → eval_only
    empty_targets: bool = False        # targets=[] 且无 blocked → reuse_only
    blocked: bool = False              # 本轮新写未满足 dep（撞占用）→ dependency_wait
    import_deferred: bool = False      # import deferred（M1–3 业务三写入）→ dependency_wait


@dataclass
class Cycle:
    """一轮（过程侧对象，§1.3.3）。M0 由内存 StateStore 持有，M1 落 SQLite。

    next_question_id / next_intent 由 persist_selection 落（§4.2.4）；
    terminate ⇒ next_question_id=None 是持久停机标志（§4.4.6）。
    """
    cycle_id: str
    status: str = "created"            # created/…/done/failed/aborted（§4.2.1）
    route: Optional[Route] = None
    question_id: Optional[str] = None  # 本轮攻坚目标 Qn（reasoning-only 轮可空）
    stage_cursor: Optional[Stage] = None
    next_question_id: Optional[str] = None
    next_intent: Optional[Intent] = None


@dataclass
class RecallSpec:
    query: str
    stage: Stage
    k: int = 5


@dataclass
class Hit:
    ref: Ref
    card_md: str
    score: float = 0.0


# ---------------------------------------------------------------------------
# 核心接口（M0 即需；替换矩阵见《第二部分》§6.10）
# ---------------------------------------------------------------------------

class Runner(Protocol):
    """智能运行时窄接口（M0 起即真：codex exec 一次性调用，无状态工人，§6.2）。"""

    def run_task(self, *, system_prompt: str, skill: str, context_pack: ContextPack) -> Artifact: ...


class Compiler(Protocol):
    """出方向：DB→context_pack（确定性四区包；M0 固定模板桩，M2 换真）。"""

    def render(self, *, cycle_id: str, stage: Stage, target_id: Optional[str] = None) -> ContextPack: ...


class Gate(Protocol):
    """三级校验 + 门禁 = 唯一写库入口（M0 桩：schema+引用两级真、业务门禁放过；M1 换真）。

    preview 只读预检、不写库（供 MCP validate_artifact；其结论不被信任，commit 时重校验，§4.1.3）。
    """

    def preview(self, artifact: Artifact) -> ValidationResult: ...

    def commit(self, artifact: Artifact, *, cycle_id: str, stage: Stage,
               target_id: Optional[str] = None) -> CommitResult: ...


class Ctx(Protocol):
    """按 ref 深潜冷数据（MCP 只读；渐进召回第 4 级，§3.6.2）。M0 假数据桩。"""

    def fetch(self, ref: Ref) -> str: ...


class Recall(Protocol):
    """渐进四级召回（§3.6.2）。M0 假数据桩，M2 换真（FTS5+嵌入+测量索引）。"""

    def query(self, spec: RecallSpec) -> List[Hit]: ...


class StateStore(Protocol):
    """状态读写（§4.2.4 写函数表；与 Gate 共用同一写服务 / 事务边界，不得各开写入口）。

    M0 内存 dict（cycle/question/route），M1 落 SQLite（避免双真相）。
    """

    def create_goal(self, *, text: str, predicate_json: Dict[str, Any]) -> str: ...

    def open_or_resume_cycle(self) -> Cycle: ...

    def set_route(self, cycle_id: str, route: Route) -> None: ...

    def persist_selection(self, cycle_id: str, sel: Selection) -> None: ...
    # 同批 local_key（tree_ops 的 create_root/add_children/spawn_question 所建）由
    # StateStore 在同一事务内解析为真实 id 后再校验（§6.10）——调用方不做穿线。

    def apply_tree_ops(self, cycle_id: str, ops: List[Dict[str, Any]]) -> None: ...

    def close_question(self, cycle_id: str, question_id: str, verdict: str,
                       evidence: List[Dict[str, Any]], answer_md: str) -> str:
        """关问状态迁移（answered/refuted + answer 登记 + resolve_deps）。

        M0 归属说明：业务判据（I3 证据合法性）在 Gate 第三级（M1 落 gate_close_question）；
        状态迁移在 StateStore（与 Gate 共用同一写服务，§4.1.1）。返回 answer_id。
        """
        ...

    def release_question(self, question_id: str) -> None:
        """active→open 释放（cycle failed/aborted / dependency_wait；不增 visit，§4.2.3）。"""
        ...

    def mark_inconclusive(self, question_id: str) -> None: ...

    def mark_cycle_done(self, cycle_id: str, status: str = "done") -> None: ...

    def activate_question(self, question_id: str) -> None: ...

    def record_question_dep(self, question_id: str, *, dep_type: str, target: str) -> None: ...

    def resolve_deps(self) -> None: ...

    def list_schedulable_questions(self) -> List[Dict[str, Any]]: ...

    def consume_directive(self, directive_id: str) -> None: ...

    def bundle_cursor(self, cycle_id: str) -> Optional[str]: ...


class Advancer(Protocol):
    """编排器步进（M3 换真：幂等步进 + 恢复 + watchdog；M0 最小推进）。

    derive_next_route 的 outcome 按 §6.10 非 Optional：decompose / terminate 等
    无 plan 结果的意图，调用方传空 PlanOutcome()（全 False）。
    """

    def advance(self, cycle_id: str) -> Union[Stage, Literal["done"]]: ...

    def derive_next_route(self, prev_selection: Selection, outcome: PlanOutcome) -> Optional[Route]: ...


# ---------------------------------------------------------------------------
# v2.3 子系统接口（M1/M4/M5 落真；写库者一律经 WriteDaemon，《第二部分》§6.13(1)）
# 提前冻结签名以防实现期漂移；M0 不实现。
# ---------------------------------------------------------------------------

@dataclass
class WriteCommand:
    """写命令（《第二部分》§6.13(1) 表定形状）：kind 决定事务边界与幂等键族。"""
    kind: str                          # 如 "gate_commit:gate_register_variant" / "state:set_route"
    payload: Dict[str, Any]
    idempotency_key: str
    txn_scope: str = "short"           # 铁律：长操作绝不持写事务


class WriteDaemon(Protocol):
    """唯一写库执行者（单线程、单 connection）；各模块只提交命令、不自开写连接。"""

    def submit(self, cmd: WriteCommand) -> CommitResult: ...


class Connector(Protocol):
    def send(self, payload: Dict[str, Any]) -> Dict[str, Any]: ...

    def poll(self) -> List[Dict[str, Any]]: ...

    def status(self) -> Dict[str, Any]: ...


class Classifier(Protocol):
    """保守三分类 + unclear 低置信出口（§4.6.2）。"""

    def classify(self, message: Dict[str, Any]) -> Literal["query", "directive", "note", "unclear"]: ...


class Responder(Protocol):
    """只读应答器：不收 raw 对话，只收已消毒查询 + 状态卡（§4.6.2 / §6.8）。"""

    def answer(self, sanitized_query: str, status_card: str) -> str: ...


class StatusPublisher(Protocol):
    """advance 在阶段边界原子发布发布快照点（status_card，§4.6.6）。"""

    def publish(self, cycle_id: str) -> str: ...


class InteractionStore(Protocol):
    """经 WriteDaemon：message / classification / reply（append-only）/ request（§4.6.8）。"""

    def submit(self, cmd: WriteCommand) -> CommitResult: ...


class Importer(Protocol):
    """外部 import 子系统（§6.9）：search 只读 MCP；register/select/materialize 写命令经 WriteDaemon。"""

    def search(self, spec: Dict[str, Any]) -> List[Dict[str, Any]]: ...

    def register(self, candidate: Dict[str, Any]) -> None: ...

    def select(self, question_id: str, action_cycle: str) -> Dict[str, Any]: ...

    def materialize(self, import_id: str) -> Dict[str, Any]: ...


class LicenseReviewer(Protocol):
    def review(self, candidate_id: str) -> Dict[str, Any]: ...


class ObservationExtractor(Protocol):
    """确定性 parser；codex 摘要另路、只存 ref+hash（§4.3.1）。M4 落真。"""

    def extract(self, execution_log_id: str) -> Dict[str, Any]: ...


class PhaseCommitStore(Protocol):
    """幂等键 (cycle,stage,target) + artifact_hash 比对：同→跳过，异→拒绝（§4.2.5）。M1 落真。"""

    def check_or_record(self, *, cycle_id: str, stage: Stage,
                        target_id: Optional[str], artifact_hash: str) -> Literal["new", "duplicate", "conflict"]: ...
