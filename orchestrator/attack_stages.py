"""AttackStages —— attack 轮 idea/plan/bundle/reasoning 阶段推进（M4 CP5.4；Advancer 委托）。

**游标语义（§4.4.5）**：`cycle.status` = 最后**已提交**阶段（created→idea→plan→bundle→done）；kill-9 重启读
status 从下一阶段续跑。idea/plan 各为**单一事务**（阶段写 + phase_commit + status 推进同生共死）；bundle
**逐目标**推进（目标间不共事务），每目标进度从 build_target/run/evaluation/variant 状态**结构性重导出**
（崩溃从状态续，§4.2.5 bundle_cursor 语义的结构化实现）。

**phase_commit（§4.2.5）**：idea/plan 整阶段一行（target NULL）；bundle 每 target 一行（= 该 target 注册段
已提交）。同键异 hash → conflict 拒（staging 被改写后不得误判已提交）。

**正式池发布与短事务序列（§4.2.5）**：(i) 训练成功后先把代码/config/checkpoint 复制进正式池并发布
内容寻址 training manifest，再在同一 checkpoint 事务写正式相对路径/hash 与
``pool_training_publication`` 锚；(ii) 评估成功后先发布 protocol、attempt 树和完整 manifest，并登记正式
execution log，随后 ``gate_register_evaluation`` 在一个事务内封 attempt/evaluation/metrics 与
``pool_publication``/cards；最后才允许 baseline/variant legal、finish target 与 phase_commit。
文件系统和 SQLite 不能共享事务，因此每步均按“不可变文件先行、DB 权威后行”设计为幂等可恢复短段：崩溃
最多留下未引用文件，恢复按 manifest/decision 复验收养，不会让 legal 身份指向 cycle staging 或部分发布。

**管线强制「先 ingest 观测再 complete」**：run log 与 attempt log 均入账 + parser ingest；complete 前显式核
attempt 已有当前口径 parser 观测（防「无据不疑」默认成绕过——suspect 谓词只对已 ingest 数据有效）。

**长操作零事务**（§6.13）：providers（Codex/judge）与 harness 子进程全部在事务外。

**契约分层（步⑧ CP8.2）**：plan 保持**抽象**（含 CPU/GPU 资源意图，命令永不入 plan）；执行命令由 bundle
阶段 Codex 产的 `execution_manifest.json`（+ 代码文件 + identity.md）承载，经 `orchestrator/manifest.py`
校验/交叉核/围栏后由 harness 机械执行。plan 阶段走**正式 gate 通道**（gate_new_protocol I1 + gate_claim_baseline
I5），不再内联建 baseline、不再把命令塞进 plan_ref——plan_ref 存**resolved 切片**（冻结 target 原样 +
编排器机械派生的 protocol_id/protocol_ver/eval_key/target_set_hash），是 bundle manifest 交叉核的锚。

provider 契约（注入式；生产 = 真 Codex 会话，范式见 M0 driver._run_with_retry；测试 = 确定性替身）：
- idea(cyc, pack) → {"idea_set.json": 冻结 idea_set.schema（candidates[]/audit_scores[]/selected_id/novelty_refs[]）}
- plan(cyc, pack) → {"plan.json": 冻结 plan.schema（needs/targets[抽象]/protocol/metric_defs/…）}
- bundle(cyc, pack) → {"execution_manifest.json": 冻结 execution_manifest.schema, "identity.md": str, <code_files…>}
- judge(cycle_id, build_target_id, review_kind, subject_hash) → 写 runner_call(audit)+DECISION(judge)（含 fail 权）
- reasoning(cyc, pack) → {"answer.json"?, "tree_ops.json"?, "selection.json"}（answer.evidence 引用真 metric_result）
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from . import harness as H
from . import manifest as MF
from . import obs_parser as OP
from . import subject_manifest as SM
from .artifact_capability import (ArtifactCapabilityError, open_artifact,
                                  read_artifact_bytes)
from .budgeting import compute_budget
from .execution_sandbox import (
    SandboxOutputError,
    sandbox_environment_hash,
    sandbox_workload_environment_hash,
)
from .gate_exec import ExecGate
from .gate_pool import PoolGate
from .gate_sqlite import GateReject
from .ids import cnum as _cnum, decode as _decode_id, qnum as _qnum, parse_positive_sqlite_int
from .import_search import ImportSearchError, validate_import_search_request
from .importer import DeferredImporter
from .interfaces import BundleReplanRequired, InvalidSelectionError, Selection
from .phase_commit import check_or_record
from .pool_publication import (
    BaselinePublication,
    CheckpointPublication,
    EvaluationPublicationSpec,
    PoolPublicationError,
    PoolPublisher,
    ProtocolPublication,
    TrainingPublicationSpec,
    VariantPublication,
    VerifiedPoolPublication,
    VerifiedTrainingPublication,
    bind_training_database,
    formal_publication_event,
)
from .process_supervisor import (
    ExecutionCancelled,
    ExecutionSupervisor,
    atomic_write_receipt,
    read_receipt,
)
from .recall_sqlite import reuse_selector
from .storage_paths import RegisteredPathError, resolve_registered_path

_TERMINAL_TARGET = ("complete", "skipped", "failed", "engineering_blocked")
_SAFE_POOL_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_BUNDLE_FAILURE_KINDS = frozenset({
    "build", "smoke", "timeout", "runtime", "data_invalid", "aborted",
    "review_failed", "metric_missing", "protocol_violation", "env_invalid",
    "artifact_invalid",
})
_BUNDLE_OPERATOR_TAIL_BYTES = 8192
_BUNDLE_OPERATOR_SUSPICIOUS = re.compile(
    r"(?:traceback|\berror\b|exception|out[ -]?of[ -]?memory|\boom\b|"
    r"cuda[^\n]*(?:fail|error)|\bnan\b|\binf\b|diverg|segmentation fault|killed)",
    re.IGNORECASE,
)


def settle_sandbox_output_failure(gate, daemon, build_target_id: int,
                                  error: SandboxOutputError) -> None:
    """Fail the exact durable owner named by a drained sandbox receipt.

    Output quarantine rejection happens *after* the guardian has proved that the
    container is gone, but before the harness can publish its log/artifacts.  It
    is therefore an artifact failure, not an infrastructure exception that may
    leave a run/attempt forever ``running``.  The receipt is the authority for
    which one owner may be settled; scanning and failing every owner below a
    target would be an unsafe overreach.

    The transitions deliberately tolerate their own crash gaps (owner failed,
    evaluation not yet failed, target not yet failed), while contradictory
    terminal facts still fail loud.
    """
    receipt = error.receipt
    context = receipt.get("context")
    sandbox = receipt.get("sandbox")
    if (receipt.get("state") != "terminal" or receipt.get("outcome") != "exit"
            or receipt.get("group_drained") is not True
            or receipt.get("containment") != "docker-container-v1"
            or not isinstance(sandbox, dict)
            or sandbox.get("container_drained") is not True
            or not isinstance(context, dict)
            or context.get("reconcile_protocol") != "execution-owner-v1"
            or context.get("build_target_id") != build_target_id):
        raise RuntimeError(
            "sandbox output reject 缺 exact terminal+drained execution owner authority")
    owner_kind = context.get("db_owner_kind")
    owner_id = context.get("db_owner_id")
    if isinstance(owner_id, bool) or not isinstance(owner_id, int) or owner_id <= 0:
        raise RuntimeError("sandbox output reject receipt 的 db_owner_id 非法")

    gate.gate_fail_sandbox_output(
        build_target_id=build_target_id, owner_kind=owner_kind,
        owner_id=owner_id, transcript_ref=str(error.receipt_path))


class _PlanReject(Exception):
    """plan 派生期的业务拒（非法/不可满足的抽象 plan）——转 decision(plan_rejected)+零 target 终态，
    不 raise 到 advance（否则轮永不推进 = 全自动死循环）。与 GateReject 一并在 _plan_stage 捕获。"""


class _PlanReviewReject(_PlanReject):
    """The configured independent review plus bounded generator repair was exhausted."""

    def __init__(self, message: str, plan: Dict[str, Any]):
        super().__init__(message)
        self.plan = plan


class _PlanTerminalized(Exception):
    """A trusted control sidecar atomically ended this cycle by spawning a
    separate reference question.  This is successful control flow, not a plan
    rejection and must not be followed by plan/reviewer/bundle work."""


class _BundleReject(Exception):
    """bundle 阶段的业务拒（Codex 产物层面站不住）——转目标 failed(failure_kind)+pc，不楔死。来源：
    ① fresh manifest/信封非法（artifact_invalid；resume 的已物化 manifest 校验不过 = 数据损毁，走
    ManifestError fail loud）；② eval.log 指标记录非法或 gate_register_evaluation 拒（protocol_violation：
    测量包不满足解析/协议/required 契约——GateReject **只在该注册调用点显式转换**，其余 GateReject
    [状态机/库损毁类]一律 fail loud 上抛，codex 第2轮 BLOCKER）。"""

    def __init__(self, msg: str, failure_kind: str = "artifact_invalid"):
        super().__init__(msg)
        self.failure_kind = failure_kind


class _BundleRepairNeeded(Exception):
    """A plan-preserving implementation failure that bundle must repair.

    The owner/run/attempt failure is already durably recorded when applicable;
    the build target deliberately remains non-terminal so the still-live
    cycle-wide Bundle main turn can consume exact log/reviewer feedback and
    materialize a replacement.
    """

    def __init__(self, message: str, *, failure_kind: str, phase: str,
                 repair_of: Optional[Dict[str, int]] = None):
        super().__init__(message)
        self.failure_kind = failure_kind
        self.phase = phase
        self.repair_of = dict(repair_of or {})


class _BundleOperatorRepair(Exception):
    """The cycle-scoped Codex operator requested a controlled repair.

    If raised during a live command, the guardian has already published a
    terminal+drained cancellation receipt.  The caller still owns the exact
    run/attempt state transition before converting this into the ordinary
    ``_BundleRepairNeeded`` path.
    """

    def __init__(self, phase: str, diagnosis: str):
        super().__init__(diagnosis)
        self.phase = phase
        self.diagnosis = diagnosis


class _BundleSessionCompleted(Exception):
    """The resident Bundle MCP loop already terminalized this target."""


class _ReasoningReject(Exception):
    """已持久化 reasoning 产物的**语义**业务拒。

    只用于标记由外部产物直接决定的拒绝（answer 目标/证据引用、tree_ops 状态语义）；SQLite、IO、
    staging 损毁等基础设施异常不得转换成此类型。调用方先做有界的 durable
    semantic retry；只有连续坏产物耗尽额度才收敛为安全终态，避免一次模型失手直接停掉研究，
    也避免 persist-then-consume 的坏产物在重启后被确定性复读。
    """

    def __init__(self, message: str, *, selection_invalid: bool = False):
        super().__init__(message)
        self.selection_invalid = selection_invalid


class _ReasoningPreflightRollback(Exception):
    """Internal sentinel that rolls a successful Reasoning dry-run back."""


_METRIC_VALUE_RE = re.compile(
    r"metric_value:\s*([1-9][0-9]*)@([1-9][0-9]*)"
    r"(?:\[checkpoint=([A-Za-z0-9][A-Za-z0-9._-]{0,127})\])?="
    r"([+-]?(?:(?:[0-9]+(?:\.[0-9]*)?)|(?:\.[0-9]+))(?:[eE][+-]?[0-9]+)?)"
)


def _canon_hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def persist_selection_safe(state, cycle_id: str, sel: Dict[str, Any], *,
                           retry_on_invalid: bool = False) -> None:
    """持久化 reasoning 的下一步 selection，将产物性拒绝分类给上层有界重试。

    真实发现（部署首跑）：Codex 反复 attack 同一问题，visit 达 question_guard.max_inconclusive_per_question
    后该题对 attack 不可调度，但 Codex 仍选 `next=该题, intent=attack` → persist_selection 抛 ValueError →
    未捕获打死驱动循环；且 reasoning 产物已持久化（persist-then-consume），重启确定性重崩 = **永久楔死**。

    任何**非法/不可调度**的 Codex selection（不可调度题 / 悬挂 id / 缺 intent /
    scores 引用不存在）均属产物问题，不是编排器 bug。这里只兜专用的
    InvalidSelectionError 并转成 _ReasoningReject；SQLite/schema/内部状态错误仍 fail loud。
    attack 生产路径传 ``retry_on_invalid=True``：上层回滚未应用的
    answer/tree/selection 批次，再用权威 DB 前沿确定性收尾，不重问 Codex。
    默认分支保留给独立调用者的旧安全契约：
    无重试驱动器时记审计并 terminate，避免直接调用把轮留成无法恢复的半状态。"""
    try:
        if "next_intent" not in sel:
            raise InvalidSelectionError("selection 缺 next_intent（Codex 产物不完整）")
        state.persist_selection(cycle_id, Selection(
            next_question_id=sel.get("next_question_id"), next_intent=sel["next_intent"],
            scores=sel.get("scores", [])))
    except InvalidSelectionError as e:
        if retry_on_invalid:
            raise _ReasoningReject(
                f"selection 无法持久化: {e}", selection_invalid=True) from e
        state.reject_unapplied_reprioritize(
            cycle_id, f"selection 无法持久化，reprioritize 未应用: {e}")
        state.daemon.conn.execute(
            "INSERT INTO decision(cycle_id,actor,type,payload_json) "
            "VALUES (?,'orchestrator','selection_invalid',?)",
            (_cnum(cycle_id), json.dumps({
                "reason": str(e), "requested": {
                    "next_question_id": sel.get("next_question_id"),
                    "next_intent": sel.get("next_intent")}}, ensure_ascii=False)))
        state.persist_selection(
            cycle_id, Selection(next_question_id=None, next_intent="terminate", scores=[]))


def _synth_content_md(c: Dict[str, Any]) -> str:
    """把冻结 idea_set.schema 的候选字段机械合成 idea.content_md（NOT NULL；供卡片/召回读）——
    编排器不推理，只是确定性拼装（同候选恒同串）。"""
    am = c.get("audit_mapping", {})
    lines = [f"# {c['candidate_id']}", f"## 核心主张\n{c['core_claim']}", f"## 机制\n{c['mechanism']}",
             "## 假设\n" + "\n".join(f"- {a}" for a in c.get("assumptions", [])),
             f"## 最小可证伪实验\n{c['min_falsifiable_experiment']}",
             f"## 类比映射\n源域={am.get('source_domain','')}；目标域={am.get('target_domain','')}；"
             f"对象={am.get('object_mapping','')}；共享关系={am.get('shared_relations','')}",
             f"## novelty\n类型={c.get('novelty_type','')}；状态={c.get('novelty_status','')}"]
    return "\n\n".join(lines)


def _audit_mean(audit: Optional[Dict[str, Any]]) -> Optional[float]:
    """六维审计分均值（idea.audit_score REAL）；无审计条目 → None。"""
    if not audit:
        return None
    sc = audit.get("scores", {})
    return round(sum(sc.values()) / len(sc), 4) if sc else None


def _persisted_idea_audit(c: Dict[str, Any], audit: Optional[Dict[str, Any]],
                          provenance: Optional[Dict[str, Any]]) -> Optional[str]:
    """Compose the durable per-candidate audit envelope without breaking its old ABI.

    Existing readers expect ``candidate_id/scores/decision/rationale`` at the
    top level of ``idea.audit_json``.  Keep those fields exactly where they
    were, then attach the merged artifact's pinned engine provenance and only
    this candidate's WildIdea-only metadata.  A legacy artifact that has none
    of the three remains SQL NULL, matching the previous behavior.
    """
    payload: Dict[str, Any] = dict(audit) if audit is not None else {}
    if provenance is not None:
        payload["provenance"] = provenance
    if "wildidea_extra" in c:
        payload["wildidea_extra"] = c["wildidea_extra"]
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True)
            if payload else None)


def _synth_identity_md(t: Dict[str, Any]) -> str:
    """从冻结 plan.schema 的 build target 机械合成 identity 草稿（gate_claim_baseline 非空判据用；
    bundle 出真 identity.md 后 register_baseline 时替换为终版）。"""
    claim = t.get("claim", {})
    return (f"# {claim.get('slug','')}（canonical_key={claim.get('canonical_key','')}）\n\n"
            f"## 计划意图\n{t.get('spec_md','')}\n\n## claim\n{json.dumps(claim, ensure_ascii=False, sort_keys=True)}")


def judge_once(daemon, judge_provider: Callable, cycle_id: str, bt_id: int,
               review_kind: str, subject_hash: str) -> None:
    """judge 调用 replay-safe（codex CP5.4 第2轮 SHOULD；attack 与 import 物化共用）：同 (target, kind,
    subject_hash) 已有 judge DECISION → 复用既有裁决、不重调 provider——否则崩在「judge 已写 DECISION →
    gate 消费前」的缝隙会重调非确定 judge（有 fail 权，第二次结果可能改变杀/不杀结局）。
    subject_hash 不同（产物变）→ 照常重评审。"""
    row = daemon.query_one(
        "SELECT json_extract(payload_json,'$.subject_hash') FROM decision WHERE actor='judge' AND type=? "
        "AND json_valid(payload_json) AND json_extract(payload_json,'$.build_target_id')=? "
        "ORDER BY id DESC LIMIT 1", (review_kind, bt_id))
    if row is not None and row[0] == subject_hash:
        return
    judge_provider(cycle_id, bt_id, review_kind, subject_hash)


class AttackStages:
    def __init__(self, *, state, compiler, pool_gate: PoolGate, close_gate, providers: Dict[str, Callable],
                 obs_policy: Dict[str, Any], work_root: str, schemas=None,
                 policy: Optional[Dict[str, Any]] = None,
                 owner_guard: Optional[Callable[[], None]] = None,
                 execution_supervisor=None,
                 execution_sandbox=None,
                 execution_sandbox_resolver=None,
                 qualification_firewall=None,
                 reuse_conn=None,
                 pool_publisher: Optional[PoolPublisher] = None):
        """state=SQLiteStateStore；compiler=SqliteCompiler；pool_gate=PoolGate(含 ExecGate 全家)；
        close_gate=SqliteGate（parser_suspect 已接真）；providers 见模块注释；work_root=staging 根目录。
        schemas=SchemaSet（步⑧：manifest 校验执法在编排器侧，不只靠 StageProvider）；
        policy=policy.yaml dict（manifest 命令围栏 execution 节；缺省从既有 obs_policy 无法取，须显式传）。"""
        if owner_guard is not None:
            if (not isinstance(execution_supervisor, ExecutionSupervisor)
                    or not execution_supervisor.binds_fenced_owner(owner_guard)):
                raise ValueError(
                    "AttackStages 绑定 owner_guard 时必须注入同一 owner guard 且持有"
                    " delegated instance fence 的 ExecutionSupervisor")
        self.state = state
        self.compiler = compiler
        self.gate: PoolGate = pool_gate
        self.close_gate = close_gate
        self.p = providers
        self.obs_policy = obs_policy
        self.work = Path(work_root)
        self.schemas = schemas
        self.policy = policy
        self._configured_owner_guard = owner_guard or (lambda: None)
        self.owner_guard = self._configured_owner_guard
        self.execution_supervisor = execution_supervisor
        self.execution_sandbox = execution_sandbox
        self.execution_sandbox_resolver = execution_sandbox_resolver
        self.qualification_firewall = qualification_firewall
        # Cheap partial-log snapshots are polled frequently, while a real Codex
        # progress turn is rate-limited unless the new bytes contain an obvious
        # runtime failure marker.  Engineering env knobs avoid changing the
        # frozen research policy surface and are bounded here.
        try:
            self.bundle_operator_poll_s = float(os.environ.get(
                "METARESEARCH_BUNDLE_OPERATOR_POLL_S", "2"))
            self.bundle_operator_probe_s = float(os.environ.get(
                "METARESEARCH_BUNDLE_OPERATOR_PROBE_S", "300"))
        except ValueError as error:
            raise ValueError("bundle operator poll/probe 环境变量须为数字") from error
        if not 0.05 <= self.bundle_operator_poll_s <= 60.0:
            raise ValueError("METARESEARCH_BUNDLE_OPERATOR_POLL_S 须在 [0.05,60]")
        if not 1.0 <= self.bundle_operator_probe_s <= 3600.0:
            raise ValueError("METARESEARCH_BUNDLE_OPERATOR_PROBE_S 须在 [1,3600]")
        gate_publisher = getattr(pool_gate, "pool_publisher", None)
        if (pool_publisher is not None and gate_publisher is not None
                and pool_publisher is not gate_publisher):
            raise ValueError("AttackStages 与 PoolGate 必须共享同一 PoolPublisher")
        self.pool_publisher = pool_publisher or gate_publisher
        if (getattr(pool_gate, "require_formal_publication", False)
                and self.pool_publisher is None):
            raise ValueError("生产 AttackStages 必须注入正式 PoolPublisher")
        # Production injects a full read-only connection with the real
        # parser_result_suspect UDF.  Keeping it separate from the writer avoids
        # a selector UDF recursively querying the connection currently executing
        # the selector SQL.
        self.reuse_conn = reuse_conn
        self._resident_plan_session_enabled = False
        self._plan_session_lock = threading.RLock()
        self._resident_reasoning_session_enabled = False
        self._resident_bundle_session_enabled = False
        self._bundle_session_lock = threading.RLock()
        self._bundle_accepting = True
        self._bundle_closed = False
        self._bundle_worker_threads: set[threading.Thread] = set()
        self._bundle_session_payloads: Dict[int, Dict[str, Any]] = {}
        # One entry per cycle-wide resident Bundle turn.  SQL remains the
        # authority for target/run/attempt state; this map only coordinates the
        # live MCP binding and its background execution worker.
        self._bundle_cycle_sessions: Dict[int, Dict[str, Any]] = {}

    def enable_resident_bundle_session(self) -> None:
        """Require one cycle-wide Bundle turn with asynchronous MCP execution."""
        with self._bundle_session_lock:
            if not self._bundle_accepting:
                raise RuntimeError("AttackStages 已进入关闭生命周期")
        self._resident_bundle_session_enabled = True

    def begin_close(self) -> None:
        """Fence admission before the shared supervisor starts cancellation."""
        with self._bundle_session_lock:
            self._bundle_accepting = False

    def close(self, *, timeout_s: float = 10.0) -> None:
        """Fence new Bundle work and join every official execution worker.

        ``ExecutionSupervisor`` owns process-tree cancellation and durable
        receipts; this controller owns the Python thread that performs the
        subsequent gate/publication/SQLite convergence.  Both must be empty
        before the broker or database can close.  A join timeout is retryable:
        the worker references remain held and ``System`` therefore retains its
        instance lease.
        """
        if (isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float))
                or not math.isfinite(float(timeout_s)) or float(timeout_s) < 0):
            raise ValueError("AttackStages close timeout_s 须为非负有限数")
        with self._bundle_session_lock:
            if self._bundle_closed:
                return
        self.begin_close()

        # This is idempotent and is normally already called by System.close().
        # Retaining it here also makes a directly owned AttackStages safe.
        supervisor_close = getattr(self.execution_supervisor, "close", None)
        if callable(supervisor_close):
            supervisor_close(timeout_s=float(timeout_s))

        deadline = time.monotonic() + float(timeout_s)
        current = threading.current_thread()
        while True:
            with self._bundle_session_lock:
                workers = [
                    worker for worker in self._bundle_worker_threads
                    if worker.is_alive()]
            if not workers:
                break
            if current in workers:
                raise RuntimeError("Bundle worker 不得从自身线程关闭 AttackStages")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                names = ",".join(sorted(worker.name for worker in workers))
                raise RuntimeError(
                    "Bundle worker 未在 close deadline 内完成 SQL/publication 收口: "
                    + names)
            # Join one at a time so every retry observes the exact live set.
            workers[0].join(timeout=min(0.1, remaining))

        with self._bundle_session_lock:
            self._bundle_worker_threads.clear()
            self._bundle_closed = True

    def enable_resident_plan_session(self) -> None:
        """Expose Plan discovery/preflight as tools of the one resident turn."""
        self._resident_plan_session_enabled = True

    def enable_resident_reasoning_session(self) -> None:
        """Return Reasoning semantic gate errors to its still-live main turn."""
        self._resident_reasoning_session_enabled = True

    @staticmethod
    def _atomic_live_context_file(path: Path, payload: bytes) -> None:
        """Publish one immutable, non-authoritative ContextPack projection file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.is_symlink() or path.read_bytes() != payload:
                raise RuntimeError(f"live ContextPack 投影身份冲突: {path}")
            return
        tmp = path.with_name(
            f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
                 | getattr(os, "O_CLOEXEC", 0)
                 | getattr(os, "O_NOFOLLOW", 0))
        fd = os.open(tmp, flags, 0o600)
        try:
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("live ContextPack 投影短写")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.replace(tmp, path)
            os.chmod(path, 0o444)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    def _publish_live_context_pack(self, pack) -> Dict[str, Any]:  # noqa: ANN001
        """Keep large refreshed contexts in VEPFS and return only a small index ref."""
        ci = _cnum(pack.cycle_id)
        target = "stage" if pack.target_id is None else f"t{int(pack.target_id)}"
        digest = str(pack.pack_hash)
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise RuntimeError("live ContextPack pack_hash 非 64-hex")
        root = (self.work / "runtime" / "live-contexts" / f"c{ci}" /
                str(pack.stage) / target / digest)
        sections = {
            "anchor": ("anchor.md", pack.anchor_md.encode("utf-8")),
            "neighborhood": (
                "neighborhood.md", pack.neighborhood_md.encode("utf-8")),
            "retrieval": ("retrieval.md", pack.retrieval_md.encode("utf-8")),
            "refs": ("refs.json", (json.dumps(
                {"refs": list(pack.refs)}, ensure_ascii=False, sort_keys=True,
                separators=(",", ":")) + "\n").encode("utf-8")),
        }
        indexed = []
        for region, (name, payload) in sections.items():
            path = root / name
            self._atomic_live_context_file(path, payload)
            indexed.append({
                "region": region, "path": str(path), "bytes": len(payload),
                "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
                "required_read": region == "anchor",
            })
        index = {
            "version": 1, "delivery": "managed_readonly_paths",
            "cycle_id": pack.cycle_id, "stage": pack.stage,
            "target_id": pack.target_id, "pack_hash": digest,
            "sources": sorted(set(pack.sources)), "sections": indexed,
        }
        raw = (json.dumps(
            index, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
        index_path = root / "index.json"
        self._atomic_live_context_file(index_path, raw)
        return {
            "cycle_id": pack.cycle_id, "stage": pack.stage,
            "target_id": pack.target_id, "pack_hash": digest,
            "index_ref": str(index_path), "index_bytes": len(raw),
            "index_sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
        }

    def _plan_scope_cycle(self, scope) -> int:  # noqa: ANN001
        if not self._resident_plan_session_enabled:
            raise RuntimeError("resident Plan session controller 未启用")
        if getattr(scope, "stage", None) != "plan" or getattr(
                scope, "target_id", None) is not None:
            raise RuntimeError("Plan session scope 身份非法")
        ci = _cnum(getattr(scope, "cycle_id", ""))
        row = self.state.daemon.query_one(
            "SELECT status FROM cycle WHERE id=?", (ci,))
        if row is None or row[0] != "idea":
            raise RuntimeError("Plan cycle 不存在或已离开 idea 游标")
        return ci

    def _preflight_import_defer(self, cyc, plan: Mapping[str, Any]) -> None:  # noqa: ANN001
        """Read-only mirror of the mutable import-defer gate inputs."""
        ci = _cnum(cyc.cycle_id)
        qi = _qnum(cyc.question_id)
        item = plan["import_defer"]
        expected_policy_hash = DeferredImporter.policy_hash(self.policy)
        snapshot = DeferredImporter.plan_snapshot(
            self.state.daemon.conn, question_id=qi, action_cycle=ci,
            policy_hash=expected_policy_hash)
        expected = {
            "policy_hash": expected_policy_hash,
            "selection_key": snapshot["selection_key"],
            "candidate_set_hash": snapshot["candidate_set_hash"],
            "license_decision_snapshot_hash": (
                snapshot["license_decision_snapshot_hash"]),
        }
        mismatches = [key for key, value in expected.items()
                      if item.get(key) != value]
        if mismatches:
            raise _PlanReject(
                "import_defer 冻结选择锚不匹配: " + ", ".join(mismatches))
        if snapshot.get("selected") is None:
            raise _PlanReject(
                "import_defer 无当前 policy 下可物化的 allow 候选")
        identity = item.get("placeholder_baseline_identity")
        if not isinstance(identity, Mapping):
            raise _PlanReject("import_defer 缺 placeholder_baseline_identity")
        canonical = identity.get("canonical_key_draft")
        slug = identity.get("slug_draft")
        if (_SAFE_POOL_KEY_RE.fullmatch(str(canonical or "")) is None
                or _SAFE_POOL_KEY_RE.fullmatch(str(slug or "")) is None):
            raise _PlanReject("import_defer placeholder identity 非安全索引键")
        if not isinstance(identity.get("identity_md"), str) or not identity["identity_md"].strip():
            raise _PlanReject("import_defer placeholder identity_md 为空")
        if self.state.daemon.query_one(
                "SELECT 1 FROM baseline WHERE canonical_key=?", (canonical,)):
            raise _PlanReject(
                f"import placeholder canonical_key 已占: {canonical!r}")
        if self.state.daemon.query_one(
                "SELECT 1 FROM external_import WHERE question_id=? "
                "AND action='selected_for_materialization' AND NOT EXISTS ("
                "SELECT 1 FROM external_import x "
                "WHERE x.question_id=external_import.question_id "
                "AND x.candidate_id=external_import.candidate_id "
                "AND x.action_cycle=external_import.action_cycle "
                "AND x.candidate_set_hash=external_import.candidate_set_hash "
                "AND x.selection_key=external_import.selection_key "
                "AND x.policy_hash=external_import.policy_hash "
                "AND x.action IN ('imported','materialize_failed','superseded')) "
                "LIMIT 1", (qi,)):
            raise _PlanReject(
                f"question q{qi} 已有未收口 materialization selection")

    def preflight_plan_session(self, scope, plan: Mapping[str, Any]) -> Dict[str, Any]:  # noqa: ANN001
        """Run the pure semantic half of Plan gates inside the live main turn."""
        ci = self._plan_scope_cycle(scope)
        try:
            proposal = json.loads(json.dumps(
                plan, ensure_ascii=False, sort_keys=True, allow_nan=False))
        except (TypeError, ValueError) as error:
            raise _PlanReject(f"plan 不是严格 JSON: {error}") from error
        self._validate_plan_schema(proposal)
        cyc = self.state.cycle(f"c{ci}")
        if "import_defer" in proposal:
            self._preflight_import_defer(cyc, proposal)
            return {"kind": "import_defer", "target_count": 0}
        targets = sorted(proposal["targets"], key=lambda item: item["seq"])
        for target in targets:
            if target["target_kind"] not in ("build", "exec", "eval"):
                raise _PlanReject(
                    f"不支持的 plan target kind: {target['target_kind']}")
        if not targets:
            resolved = self._validate_reuse_only_plan(cyc, proposal)
            return {"kind": "reuse_only", "evidence_count": len(resolved)}
        derived = self._derive_plan(ci, proposal, targets)
        return {"kind": "execution", "target_count": len(derived["targets"])}

    def run_plan_import_search(self, scope, request: Mapping[str, Any]) -> Dict[str, Any]:  # noqa: ANN001
        """Execute one replayable discovery and return to the same Plan turn."""
        ci = self._plan_scope_cycle(scope)
        try:
            normalized = validate_import_search_request(dict(request))
            if self.schemas is not None:
                errors = [
                    f"{item.json_path} {item.message}"
                    for item in self.schemas.validator(
                        "import_search_request").iter_errors(normalized)
                ]
                if errors:
                    raise ImportSearchError("; ".join(errors[:8]))
        except (ImportSearchError, TypeError, ValueError) as error:
            raise RuntimeError(
                f"import_search_request 非法: {error}") from error
        with self._plan_session_lock:
            cyc = self.state.cycle(f"c{ci}")
            pack = self.compiler.render(cycle_id=cyc.cycle_id, stage="plan")
            outcome = self._run_import_search(cyc, normalized, pack)
            if outcome.get("terminalized") is True:
                self._assert_import_control_terminalized(cyc, outcome)
                raise RuntimeError(
                    "legacy import connector 已直接终态化 cycle；resident Plan "
                    "协议要求该分支改由 Reasoning 收口")
            refreshed = self.compiler.render(
                cycle_id=cyc.cycle_id, stage="plan")
            context_ref = self._publish_live_context_pack(refreshed)
        result_keys = (
            "request_hash", "result_hash", "candidate_count", "skipped_count",
            "candidate_ids", "license_review_ids", "source_authority_hash",
            "reasoning_question_request", "retrieval", "provider",
        )
        return {
            "cycle_id": cyc.cycle_id,
            "search": {key: outcome.get(key) for key in result_keys
                       if key in outcome},
            "context_pack": context_ref,
        }

    @staticmethod
    def _reasoning_only_preflight_ops(files: Mapping[str, Any], route: str) -> List[Dict[str, Any]]:
        if "answer.json" in files:
            raise ValueError(f"{route} reasoning-only 轮不得提交 answer.json")
        tree = files.get("tree_ops.json")
        if not isinstance(tree, Mapping) or not isinstance(tree.get("ops"), list):
            raise ValueError(f"{route} 轮必产 tree_ops.json 且 ops 须为数组")
        ops = list(tree["ops"])
        if route == "goal_amend":
            indexes = [index for index, op in enumerate(ops)
                       if isinstance(op, Mapping) and op.get("op") == "amend_goal"]
            if indexes != [0]:
                raise ValueError(
                    "goal_amend 轮须恰有一个 amend_goal 且必须是首个 op")
            return ops
        required = "create_root" if route == "bootstrap" else "add_children"
        if not any(isinstance(op, Mapping) and op.get("op") == required for op in ops):
            raise ValueError(f"{route} 轮 tree_ops 须含 {required}")
        return ops

    def preflight_reasoning_session(
            self, scope, files: Mapping[str, Any]) -> Dict[str, Any]:  # noqa: ANN001
        """Dry-run the exact Reasoning core transaction, then roll it back.

        This gives the resident Reasoning main agent answer/evidence, tree-op and
        selection errors through its live MCP call.  The authoritative stage
        commit repeats the same checks; this method grants no state transition.
        """
        if not self._resident_reasoning_session_enabled:
            raise RuntimeError("resident Reasoning session controller 未启用")
        if (getattr(scope, "stage", None) != "reasoning"
                or getattr(scope, "target_id", None) is not None):
            raise RuntimeError("Reasoning session scope 身份非法")
        ci = _cnum(getattr(scope, "cycle_id", ""))
        cyc = self.state.cycle(f"c{ci}")
        if cyc.status not in {"created", "bundle"}:
            raise RuntimeError(
                f"Reasoning cycle 已离开可提交游标: {cyc.status!r}")
        if "selection.json" not in files:
            raise ValueError("Reasoning 必产 selection.json")
        selection = files["selection.json"]

        if cyc.route == "dependency_wait":
            if "answer.json" in files:
                raise ValueError("dependency_wait 尚无新证据，不得关闭 question")
            tree = files.get("tree_ops.json")
            if not isinstance(tree, Mapping) or tree.get("ops") != []:
                raise ValueError("dependency_wait Reasoning 必须提交空 tree_ops.ops")
            if selection.get("next_intent") == "terminate":
                raise ValueError("dependency_wait selection 只作继续等待建议，不得 terminate")
            return {"kind": "dependency_wait", "writes_performed": 0}

        if cyc.route in {"bootstrap", "decompose", "goal_amend"}:
            if cyc.status != "created":
                raise RuntimeError("reasoning-only route 游标须为 created")
            if cyc.route == "goal_amend":
                if self.state.consumed_goal_amend_directive(cyc.cycle_id) is None:
                    raise ValueError("goal_amend 缺已确认 directive authority")
                self.state.assert_goal_amend_quiescent(cyc.cycle_id)
            ops = self._reasoning_only_preflight_ops(files, cyc.route)
            try:
                with self.state.atomic():
                    self.state.apply_tree_ops(cyc.cycle_id, ops)
                    self.state.persist_selection(cyc.cycle_id, Selection(
                        next_question_id=selection.get("next_question_id"),
                        next_intent=selection["next_intent"],
                        scores=selection.get("scores", [])))
                    raise _ReasoningPreflightRollback()
            except _ReasoningPreflightRollback:
                return {"kind": cyc.route, "writes_performed": 0}

        if cyc.route not in {"attack", "eval_only", "reuse_only"}:
            raise RuntimeError(f"Reasoning route 非法: {cyc.route!r}")
        if cyc.status != "bundle" or cyc.question_id is None:
            raise RuntimeError("attack Reasoning 缺 bundle 游标或 active question")
        answer = files.get("answer.json")
        if answer is not None and answer.get("question_id") != cyc.question_id:
            raise ValueError(
                f"answer.question_id={answer.get('question_id')!r} "
                f"不是本轮 active {cyc.question_id!r}")
        try:
            with self.state.atomic() as conn:
                fresh = self.state.cycle(cyc.cycle_id)
                if fresh.status != "bundle" or fresh.question_id != cyc.question_id:
                    raise RuntimeError("Reasoning dry-run 时 cycle/question 身份漂移")
                qi = _qnum(cyc.question_id)
                qrow = conn.execute(
                    "SELECT status FROM question WHERE id=?", (qi,)).fetchone()
                if qrow is None:
                    raise RuntimeError(f"active question {cyc.question_id} 不存在")
                if answer is not None and qrow[0] not in {
                        "answered", "refuted", "dead_end"}:
                    self.close_gate.gate_close_question_in_txn(
                        conn, cycle_id=cyc.cycle_id,
                        question_id=answer["question_id"],
                        verdict=answer["verdict"], evidence=answer["evidence"],
                        answer_md=answer["answer_md"])
                elif answer is not None:
                    raise ValueError("Reasoning 不得重复关闭终态 question")
                current_status = conn.execute(
                    "SELECT status FROM question WHERE id=?", (qi,)).fetchone()[0]
                if current_status == "active":
                    self.state.mark_inconclusive(cyc.question_id)
                self.state.apply_tree_ops(
                    cyc.cycle_id,
                    files.get("tree_ops.json", {"ops": []}).get("ops", []))
                self.state.persist_selection(cyc.cycle_id, Selection(
                    next_question_id=selection.get("next_question_id"),
                    next_intent=selection["next_intent"],
                    scores=selection.get("scores", [])))
                raise _ReasoningPreflightRollback()
        except _ReasoningPreflightRollback:
            return {"kind": "attack", "writes_performed": 0}

    def _bundle_scope_cycle(self, scope) -> int:  # noqa: ANN001
        if getattr(scope, "stage", None) != "bundle":
            raise RuntimeError("Bundle session scope.stage 非 bundle")
        ci = _cnum(getattr(scope, "cycle_id", ""))
        row = self.state.daemon.query_one(
            "SELECT status FROM cycle WHERE id=?", (ci,))
        if row is None or row[0] != "plan":
            raise RuntimeError(
                "Bundle cycle 不存在或已离开 plan 游标")
        return ci

    def _bundle_cycle_session(self, ci: int) -> Dict[str, Any]:
        with self._bundle_session_lock:
            return self._bundle_cycle_sessions.setdefault(ci, {
                "active_target_id": None,
                "pack": None,
                "worker": None,
                "worker_error": None,
                "repair_requested": None,
                "control_sequence": 0,
                "started_at": None,
                "finished_at": None,
            })

    def _bundle_pack_projection(self, pack) -> Dict[str, Any]:  # noqa: ANN001
        return self._publish_live_context_pack(pack)

    def _bundle_target_rows(self, ci: int) -> List[tuple]:
        return self.state.daemon.query(
            "SELECT id,target_kind,seq,critical,status,failure_kind,plan_ref "
            "FROM build_target WHERE cycle_id=? AND plan_ref IS NOT NULL "
            "ORDER BY seq,id", (ci,))

    def _bundle_apply_early_exit(self, ci: int) -> None:
        replan = self.state.daemon.query_one(
            "SELECT json_extract(payload_json,'$.build_target_id') "
            "FROM decision WHERE cycle_id=? AND actor='orchestrator' "
            "AND type='bundle_replan_required' AND json_valid(payload_json) "
            "ORDER BY id DESC LIMIT 1", (ci,))
        if replan is not None and replan[0] is not None:
            # A frozen-plan diagnosis ends this Bundle cycle regardless of the
            # target's critical bit.  Remaining targets belong to the same plan
            # and are skipped mechanically so the mandatory next stage is
            # Reasoning, which decides the next cycle.
            self._skip_after_critical_failure(
                self.state.cycle(f"c{ci}"), int(replan[0]))
            return
        failed = self.state.daemon.query_one(
            "SELECT id FROM build_target WHERE cycle_id=? AND plan_ref IS NOT NULL "
            "AND (status='engineering_blocked' OR "
            "(critical=1 AND status='failed')) ORDER BY seq,id LIMIT 1", (ci,))
        if failed is not None:
            self._skip_after_critical_failure(
                self.state.cycle(f"c{ci}"), int(failed[0]))

    def bind_next_bundle_target(self, scope) -> Dict[str, Any]:  # noqa: ANN001
        """Bind the next target without leaving the one cycle-wide model turn."""
        if not self._resident_bundle_session_enabled:
            raise RuntimeError("resident Bundle session controller 未启用")
        ci = self._bundle_scope_cycle(scope)
        session = self._bundle_cycle_session(ci)
        with self._bundle_session_lock:
            if not self._bundle_accepting:
                raise RuntimeError("Bundle controller 正在关闭，拒绝绑定新 target")
            worker = session.get("worker")
            if worker is not None and worker.is_alive():
                raise RuntimeError(
                    "当前 Bundle target 仍在执行；请用 bundle_status 轮询")
            if session.get("worker_error") is not None:
                raise RuntimeError(
                    "Bundle 官方执行管线异常: "
                    + str(session["worker_error"]))

            self._bundle_apply_early_exit(ci)
            rows = self._bundle_target_rows(ci)
            active_id = session.get("active_target_id")
            active = next((row for row in rows if row[0] == active_id), None)
            if active is not None and active[4] not in _TERMINAL_TARGET:
                chosen = active
            else:
                chosen = next(
                    (row for row in rows if row[4] not in _TERMINAL_TARGET), None)
            if chosen is None:
                session["active_target_id"] = None
                session["pack"] = None
                return {
                    "cycle_id": f"c{ci}", "cycle_complete": True,
                    "targets": [
                        {"build_target_id": row[0], "target_kind": row[1],
                         "seq": row[2], "critical": bool(row[3]),
                         "status": row[4], "failure_kind": row[5]}
                        for row in rows],
                }

            target_id = int(chosen[0])
            pack = self.compiler.render(
                cycle_id=f"c{ci}", stage="bundle", target_id=str(target_id))
            session["active_target_id"] = target_id
            session["pack"] = pack
            session["worker"] = None
            session["repair_requested"] = None
            return {
                "cycle_id": f"c{ci}", "cycle_complete": False,
                "build_target_id": target_id, "target_kind": chosen[1],
                "seq": chosen[2], "critical": bool(chosen[3]),
                "status": chosen[4], "failure_kind": chosen[5],
                "needs_repair": self._latest_open_repair(target_id) is not None,
                "context_pack": self._bundle_pack_projection(pack),
            }

    def bundle_session_scope(self, scope) -> Dict[str, Any]:  # noqa: ANN001
        """Return the exact target/ContextPack bound to this cycle turn."""
        ci = self._bundle_scope_cycle(scope)
        session = self._bundle_cycle_session(ci)
        with self._bundle_session_lock:
            target_id, pack = session.get("active_target_id"), session.get("pack")
            if target_id is None or pack is None:
                raise RuntimeError(
                    "Bundle 主 turn 尚未绑定 target；请先调用 bundle_next_target")
            return {
                "target_id": target_id,
                "pack_hash": pack.pack_hash,
                "refs": list(pack.refs),
            }

    def _bundle_scope_target(self, scope) -> int:  # noqa: ANN001
        if getattr(scope, "stage", None) != "bundle":
            raise RuntimeError("Bundle session scope.stage 非 bundle")
        try:
            target_id = int(getattr(scope, "target_id", None))
        except (TypeError, ValueError) as error:
            raise RuntimeError("Bundle session target_id 非法") from error
        row = self.state.daemon.query_one(
            "SELECT cycle_id,plan_ref FROM build_target WHERE id=?", (target_id,))
        if (row is None or row[0] != _cnum(scope.cycle_id) or row[1] is None):
            raise RuntimeError("Bundle session target 不属于当前 cycle 或缺冻结 plan")
        return target_id

    def _bundle_live_logs(self, ci: int, target_id: int) -> List[Dict[str, Any]]:
        """Return bounded tails of mutable execution logs; never register them."""
        root = self.work / f"c{ci}" / f"t{target_id}"
        if not root.exists() or root.is_symlink():
            return []
        candidates = []
        visited_dirs = 0
        visited_files = 0
        try:
            for current, dirs, files in os.walk(root, followlinks=False):
                visited_dirs += 1
                if visited_dirs > 256 or visited_files >= 4096:
                    break
                current_path = Path(current)
                dirs[:] = sorted(
                    name for name in dirs
                    if not (current_path / name).is_symlink())[:256]
                for name in sorted(files):
                    visited_files += 1
                    if visited_files > 4096:
                        break
                    if ".log" not in name or name.endswith(".exit"):
                        continue
                    path = current_path / name
                    try:
                        info = path.lstat()
                    except OSError:
                        continue
                    if stat.S_ISREG(info.st_mode) and not path.is_symlink():
                        candidates.append((info.st_mtime_ns, path, info.st_size))
        except OSError:
            return []
        snapshots: List[Dict[str, Any]] = []
        for _mtime, path, size in sorted(candidates, reverse=True)[:8]:
            try:
                with path.open("rb") as handle:
                    if size > _BUNDLE_OPERATOR_TAIL_BYTES:
                        handle.seek(size - _BUNDLE_OPERATOR_TAIL_BYTES)
                    tail = handle.read(_BUNDLE_OPERATOR_TAIL_BYTES)
            except OSError:
                continue
            snapshots.append({
                "path": str(path.relative_to(root)),
                "state": ("partial" if path.name.endswith(".partial") else "final"),
                "size_bytes": size,
                "tail_text": tail.decode("utf-8", errors="replace"),
                "tail_sha256": "sha256:" + hashlib.sha256(tail).hexdigest(),
                "truncated": size > len(tail),
            })
        return snapshots

    def bundle_session_status(self, scope) -> Dict[str, Any]:  # noqa: ANN001
        """Return compact authoritative execution/repair state to the live turn."""
        ci = self._bundle_scope_cycle(scope)
        target_id = self._bundle_scope_target(scope)
        session = self._bundle_cycle_session(ci)
        with self._bundle_session_lock:
            if session.get("active_target_id") != target_id:
                raise RuntimeError("Bundle status target 与 cycle session 绑定漂移")
            worker = session.get("worker")
            worker_running = bool(worker is not None and worker.is_alive())
            worker_error = session.get("worker_error")
            repair_requested = session.get("repair_requested")
        row = self.state.daemon.query_one(
            "SELECT status,failure_kind,target_kind,seq FROM build_target WHERE id=?",
            (target_id,))
        repair = self.state.daemon.query_one(
            "SELECT id,payload_json FROM decision WHERE cycle_id=? "
            "AND actor='orchestrator' AND type='bundle_repair_requested' "
            "AND json_valid(payload_json) "
            "AND json_extract(payload_json,'$.build_target_id')=? "
            "ORDER BY id DESC LIMIT 1",
            (_cnum(scope.cycle_id), target_id))
        logs = self.state.daemon.query(
            "SELECT el.log_kind,el.ref,el.content_hash,el.bytes FROM execution_log el "
            "LEFT JOIN run r ON r.id=el.run_id "
            "LEFT JOIN evaluation_attempt ea ON ea.id=el.evaluation_attempt_id "
            "WHERE coalesce(r.build_target_id,ea.build_target_id)=? "
            "ORDER BY el.id DESC LIMIT 20", (target_id,))
        return {
            "cycle_id": scope.cycle_id,
            "build_target_id": target_id,
            "target_kind": row[2],
            "seq": row[3],
            "status": row[0],
            "failure_kind": row[1],
            "terminal": row[0] in _TERMINAL_TARGET,
            "worker_running": worker_running,
            "controller_error": (
                None if worker_error is None else str(worker_error)),
            "cancellation_requested": (
                None if repair_requested is None else dict(repair_requested)),
            "latest_repair": (
                None if repair is None else {
                    "decision_id": repair[0], **json.loads(repair[1])}),
            "execution_logs": [
                {"log_kind": item[0], "ref": item[1],
                 "content_hash": item[2], "bytes": item[3]}
                for item in logs],
            "live_logs": self._bundle_live_logs(ci, target_id),
        }

    def _bundle_worker(self, ci: int, target_id: int) -> None:
        session = self._bundle_cycle_session(ci)
        error: Optional[BaseException] = None
        try:
            self._drive_target(self.state.cycle(f"c{ci}"), target_id)
        except BaseException as caught:  # surfaced to model and outer owner
            error = caught
        finally:
            # A repair/replan request accepted while the worker was alive must
            # end in a durable repair/replan decision.  This catches the narrow
            # race where the final command exits between the MCP request and the
            # guardian's next observer tick; never silently advance to a new
            # target after dropping a live main-agent decision.
            with self._bundle_session_lock:
                requested = (None if session.get("repair_requested") is None
                             else dict(session["repair_requested"]))
            if error is None and requested is not None:
                if requested.get("replan"):
                    settled = self.state.daemon.query_one(
                        "SELECT 1 FROM decision WHERE cycle_id=? "
                        "AND actor='orchestrator' AND type='bundle_replan_required' "
                        "AND json_extract(payload_json,'$.build_target_id')=? LIMIT 1",
                        (ci, target_id)) is not None
                else:
                    settled = self._latest_open_repair(target_id) is not None
                if not settled:
                    error = RuntimeError(
                        "Bundle repair/replan 请求在 worker 终态边界未完成结算；"
                        "拒绝静默切换 target")
            with self._bundle_session_lock:
                self._bundle_session_payloads.pop(target_id, None)
                if session.get("active_target_id") == target_id:
                    session["worker_error"] = error
                    session["finished_at"] = time.monotonic()

    def execute_bundle_session(self, scope, files: Mapping[str, Any]) -> Dict[str, Any]:  # noqa: ANN001
        """Start official execution asynchronously and return to Codex promptly."""
        if not self._resident_bundle_session_enabled:
            raise RuntimeError("resident Bundle session controller 未启用")
        target_id = self._bundle_scope_target(scope)
        if not isinstance(files, Mapping):
            raise RuntimeError("Bundle session files 非 mapping")
        with self._bundle_session_lock:
            if not self._bundle_accepting:
                raise RuntimeError("Bundle controller 正在关闭，拒绝启动新执行")
            ci = self._bundle_scope_cycle(scope)
            session = self._bundle_cycle_session(ci)
            if session.get("active_target_id") != target_id:
                raise RuntimeError("Bundle execute target 与 cycle session 绑定漂移")
            status = self.state.daemon.query_one(
                "SELECT status FROM build_target WHERE id=?", (target_id,))[0]
            if status in _TERMINAL_TARGET:
                raise RuntimeError(
                    "Bundle target 已终态，不能再接受工程 repair；"
                    "请读取终态证据，必要时用 bundle_replan 交 Reasoning")
            worker = session.get("worker")
            if worker is not None and worker.is_alive():
                raise RuntimeError("同一 build_target 已有 Bundle session 执行中")
            self._bundle_session_payloads[target_id] = dict(files)
            session["worker_error"] = None
            session["repair_requested"] = None
            session["started_at"] = time.monotonic()
            session["finished_at"] = None
            worker = threading.Thread(
                target=self._bundle_worker, args=(ci, target_id),
                name=f"bundle-cycle-c{ci}-t{target_id}", daemon=False)
            session["worker"] = worker
            self._bundle_worker_threads = {
                item for item in self._bundle_worker_threads if item.is_alive()}
            self._bundle_worker_threads.add(worker)
            try:
                worker.start()
            except BaseException:
                self._bundle_worker_threads.discard(worker)
                session["worker"] = None
                raise
        return self.bundle_session_status(scope)

    def request_bundle_repair(self, scope, diagnosis: str) -> Dict[str, Any]:  # noqa: ANN001
        """Ask the live guardian observer to cancel for an engineering repair."""
        ci = self._bundle_scope_cycle(scope)
        target_id = self._bundle_scope_target(scope)
        session = self._bundle_cycle_session(ci)
        with self._bundle_session_lock:
            worker = session.get("worker")
            if worker is None or not worker.is_alive():
                raise RuntimeError(
                    "Bundle target 当前无执行中命令；可直接修改并重提")
            status = self.state.daemon.query_one(
                "SELECT status FROM build_target WHERE id=?", (target_id,))[0]
            if status in _TERMINAL_TARGET:
                raise RuntimeError(
                    "Bundle target 已终态，repair 请求未被接受")
            session["control_sequence"] = int(
                session.get("control_sequence") or 0) + 1
            session["repair_requested"] = {
                "request_id": session["control_sequence"],
                "diagnosis_md": diagnosis, "replan": False,
                "requested_at": time.monotonic(),
                "observed": False,
            }
        return self.bundle_session_status(scope)

    def _resident_bundle_control(self, target_id: int) -> Optional[Dict[str, Any]]:
        with self._bundle_session_lock:
            for session in self._bundle_cycle_sessions.values():
                if session.get("active_target_id") == target_id:
                    request = session.get("repair_requested")
                    if request is None:
                        return None
                    request["observed"] = True
                    request["observed_at"] = time.monotonic()
                    return dict(request)
        return None

    def replan_bundle_session(self, scope, diagnosis: str) -> Dict[str, Any]:  # noqa: ANN001
        """Route an explicit main-agent frozen-plan diagnosis to Reasoning."""
        if not self._resident_bundle_session_enabled:
            raise RuntimeError("resident Bundle session controller 未启用")
        target_id = self._bundle_scope_target(scope)
        with self._bundle_session_lock:
            ci = self._bundle_scope_cycle(scope)
            session = self._bundle_cycle_session(ci)
            worker = session.get("worker")
            if worker is not None and worker.is_alive():
                session["control_sequence"] = int(
                    session.get("control_sequence") or 0) + 1
                session["repair_requested"] = {
                    "request_id": session["control_sequence"],
                    "diagnosis_md": diagnosis, "replan": True,
                    "requested_at": time.monotonic(),
                    "observed": False,
                }
                return self.bundle_session_status(scope)
            status = self.state.daemon.query_one(
                "SELECT status FROM build_target WHERE id=?", (target_id,))[0]
            if status in _TERMINAL_TARGET:
                raise RuntimeError(
                    "Bundle target 已终态，不能回写 replan；每个 cycle 仍会进入 "
                    "Reasoning，请在该必经阶段基于终态证据决策")
            # ``engineering_blocked`` is the existing mechanical early-exit
            # state.  The accompanying durable bundle_replan_required record
            # distinguishes a frozen-plan diagnosis from an implementation or
            # environment block; Reasoning owns the research interpretation.
            self.gate.gate_finish_build_target(
                build_target_id=target_id, status="engineering_blocked",
                failure_kind="protocol_violation")
            with self.state.daemon.transaction() as conn:
                exists = conn.execute(
                    "SELECT 1 FROM decision WHERE cycle_id=? "
                    "AND actor='orchestrator' AND type='bundle_replan_required' "
                    "AND json_extract(payload_json,'$.build_target_id')=? LIMIT 1",
                    (ci, target_id)).fetchone()
                if exists is None:
                    conn.execute(
                        "INSERT INTO decision(cycle_id,actor,type,payload_json) "
                        "VALUES (?,'orchestrator','bundle_replan_required',?)",
                        (ci, json.dumps({
                            "protocol": "bundle-replan-v1",
                            "build_target_id": target_id,
                            "summary_md": diagnosis,
                            "source": "resident_bundle_main",
                        }, ensure_ascii=False, sort_keys=True)))
            self._ensure_target_pc(self.state.cycle(scope.cycle_id), target_id)
            return self.bundle_session_status(scope)

    def _publish_training_assets(
            self, *, cycle_id: str, build_target_id: int, run_id: int,
            variant_id: int, baseline_id: int, target_kind: str,
            manifest: Mapping[str, Any], src_dir: Path,
            checkpoint_sources: Mapping[str, Path],
            checkpoint_hashes: Mapping[str, str]) -> Optional[VerifiedTrainingPublication]:
        """Copy immutable training assets before checkpoint rows reference them."""
        if self.pool_publisher is None:
            return None
        if target_kind not in ("build", "exec"):
            raise RuntimeError(f"formal training publication 不支持 target_kind={target_kind}")
        baseline = self.state.daemon.query_one(
            "SELECT slug,canonical_key FROM baseline WHERE id=?", (baseline_id,))
        variant = self.state.daemon.query_one(
            "SELECT variant_key,config_json FROM variant WHERE id=? AND baseline_id=?",
            (variant_id, baseline_id))
        if baseline is None or variant is None:
            raise RuntimeError("formal training publication 的 baseline/variant 身份缺失")
        legacy = "checkpoint" in manifest.get("expected_outputs", {})
        checkpoints = []
        for key, source in checkpoint_sources.items():
            db_key = f"final-r{run_id}" if legacy else key
            checkpoints.append(CheckpointPublication(
                ckpt_key=db_key, source=Path(source),
                expected_sha256=checkpoint_hashes[key],
                # The bundle output's basename is not a DB identity and may be
                # Unicode.  Keep formal pool paths independent of that label.
                file_name="artifact.bin"))
        baseline_spec = BaselinePublication(
            baseline_id=baseline_id, slug=baseline[0], canonical_key=baseline[1],
            identity_source=(src_dir / MF.IDENTITY_FILE if target_kind == "build" else None),
            code_source=(src_dir if target_kind == "build" else None),
            repro_cmd_md=(manifest["repro_cmd_md"] if target_kind == "build" else None))
        variant_spec = VariantPublication(
            variant_id=variant_id, variant_key=variant[0], config=variant[1],
            overrides_source=(src_dir if target_kind == "exec" else None))
        return self.pool_publisher.publish_training(TrainingPublicationSpec(
            baseline=baseline_spec, variant=variant_spec,
            checkpoints=checkpoints))

    def _training_checkpoint_ids(
            self, training: VerifiedTrainingPublication, *, variant_id: int,
            require_complete_variant: bool = True) -> Dict[str, int]:
        """Resolve a verified training manifest to exact formal checkpoint rows."""
        objects = training.payload.get("objects", {})
        if objects.get("variant", {}).get("variant_id") != variant_id:
            raise RuntimeError("training publication 与目标 variant 不一致")
        mapping: Dict[str, int] = {}
        for item in training.checkpoint_bindings:
            row = self.state.daemon.query_one(
                "SELECT id FROM checkpoint WHERE variant_id=? AND ckpt_key=? AND path=? "
                "AND content_hash=? AND hash_alg=?",
                (variant_id, item["ckpt_key"], item["path"],
                 item["content_hash"], item["hash_alg"]))
            if row is None:
                raise RuntimeError(
                    f"formal checkpoint {item['ckpt_key']!r} 未按 publication 入账")
            mapping[item["ckpt_key"]] = row[0]
        if require_complete_variant:
            current = self.state.daemon.query(
                "SELECT id FROM checkpoint WHERE variant_id=? ORDER BY id", (variant_id,))
            if {row[0] for row in current} != set(mapping.values()):
                raise RuntimeError("variant checkpoint 集合超出 formal training publication")
        return mapping

    def _recover_training_publication(
            self, *, variant_id: int,
            run_id: Optional[int] = None) -> Optional[VerifiedTrainingPublication]:
        """Recover the immutable training receipt from its DB anchor, never staging."""
        if self.pool_publisher is None:
            return None
        rows = self.state.daemon.query(
            "SELECT payload_json FROM decision WHERE actor='gate' "
            "AND type='pool_training_publication' "
            "AND json_extract(payload_json,'$.variant_id')=? ORDER BY id DESC",
            (variant_id,))
        for (raw,) in rows:
            try:
                event = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            if run_id is not None and event.get("run_id") != run_id:
                continue
            reference, digest = event.get("manifest_ref"), event.get("manifest_hash")
            if not isinstance(reference, str) or not isinstance(digest, str):
                continue
            training = self.pool_publisher.verify_training(
                reference, expected_hash=digest)
            ids = self._training_checkpoint_ids(training, variant_id=variant_id)
            if event.get("checkpoint_ids") != [
                    ids[item["ckpt_key"]] for item in training.checkpoint_bindings]:
                raise RuntimeError("pool_training_publication checkpoint id 锚损坏")
            return training
        suffix = f"/run {run_id}" if run_id is not None else ""
        raise RuntimeError(f"variant {variant_id}{suffix} 缺 formal training publication")

    def _publish_evaluation_assets(
            self, *, training: VerifiedTrainingPublication, evaluation_id: int,
            attempt_id: int, attempt_no: int, metrics: List[Dict[str, Any]],
            eval_log: Path, transcript_ref: Optional[str]) -> VerifiedPoolPublication:
        """Publish protocol and evaluation artifacts before sealing DB success."""
        if self.pool_publisher is None:
            raise RuntimeError("formal evaluation publication 未装配")
        evaluation = self.state.daemon.query_one(
            "SELECT variant_id,protocol_id,protocol_ver,eval_key FROM evaluation WHERE id=?",
            (evaluation_id,))
        if evaluation is None:
            raise RuntimeError(f"evaluation {evaluation_id} 不存在")
        variant_id, protocol_id, protocol_ver, eval_key = evaluation
        protocol = self.state.daemon.query_one(
            "SELECT name,scope_spec_json FROM protocol WHERE id=? AND version=?",
            (protocol_id, protocol_ver))
        if protocol is None:
            raise RuntimeError(f"protocol p{protocol_id}@{protocol_ver} 不存在")
        checkpoint_ids = self._training_checkpoint_ids(
            training, variant_id=variant_id)
        transcript_source = self._evaluation_transcript_source(
            eval_log, transcript_ref)
        return self.pool_publisher.publish_evaluation(EvaluationPublicationSpec(
            training=training, evaluation_id=evaluation_id, eval_key=eval_key,
            attempt_id=attempt_id, attempt_no=attempt_no,
            results_source=eval_log.parent, primary_artifact=eval_log.name,
            metrics=metrics,
            protocol=ProtocolPublication(
                protocol_id=protocol_id, version=protocol_ver,
                name=protocol[0], scope_spec=protocol[1]),
            transcript_source=transcript_source,
            checkpoint_ids=checkpoint_ids))

    @staticmethod
    def _evaluation_transcript_source(
            eval_log: Path, transcript_ref: Optional[str]) -> Optional[Path]:
        """Recover the exact guardian receipt used on the first publication.

        Fresh harness results return ``process_receipt_path`` directly.  A crash
        after file publication but before DB registration reconstructs ``ev``
        from ``eval.log``/``.exit``; its fsynced process pointer is then the
        durable route back to the same receipt.  This keeps replay byte-identical
        instead of colliding with an attempt directory that suddenly lacks its
        transcript.
        """
        if isinstance(transcript_ref, str) and transcript_ref:
            direct = Path(transcript_ref)
            if direct.exists():
                return direct
        pointer_path = eval_log.with_name(eval_log.name + ".process.json")
        if not pointer_path.exists():
            return None
        pointer = read_receipt(pointer_path)
        receipt_ref = pointer.get("receipt_path")
        if (pointer.get("version") != 1 or pointer.get("outcome") != "exit"
                or pointer.get("group_drained") is not True
                or not isinstance(receipt_ref, str) or not receipt_ref):
            raise RuntimeError("eval process pointer 损坏，无法恢复 formal transcript")
        receipt_path = Path(receipt_ref)
        receipt = read_receipt(receipt_path)
        if (receipt.get("operation_id") != pointer.get("operation_id")
                or receipt.get("outcome") != pointer.get("outcome")
                or receipt.get("group_drained") is not True
                or receipt.get("state") != "terminal"):
            raise RuntimeError("eval guardian receipt 与 process pointer 不一致")
        return receipt_path

    def _recover_evaluation_publication(
            self, *, evaluation_id: int,
            attempt_id: int) -> Optional[VerifiedPoolPublication]:
        """Recover the complete content-addressed receipt after registration crashes."""
        if self.pool_publisher is None:
            return None
        rows = self.state.daemon.query(
            "SELECT payload_json FROM decision WHERE actor='gate' AND type='pool_publication' "
            "AND json_extract(payload_json,'$.evaluation_id')=? "
            "AND json_extract(payload_json,'$.attempt_id')=? ORDER BY id DESC",
            (evaluation_id, attempt_id))
        if not rows:
            raise RuntimeError(
                f"evaluation {evaluation_id}/attempt {attempt_id} 缺 formal pool publication")
        identities = set()
        publication = None
        for (raw,) in rows:
            try:
                event = json.loads(raw)
            except (TypeError, json.JSONDecodeError) as error:
                raise RuntimeError("pool_publication decision JSON 损坏") from error
            identity = (event.get("manifest_ref"), event.get("manifest_hash"))
            identities.add(identity)
            if publication is None:
                publication = self.pool_publisher.verify_publication(
                    identity[0], expected_hash=identity[1])
        if len(identities) != 1 or publication is None:
            raise RuntimeError("同一 canonical attempt 有冲突的 formal pool publication")
        evaluation = publication.payload["objects"]["evaluation"]
        if (evaluation.get("evaluation_id"), evaluation.get("attempt_id")) != (
                evaluation_id, attempt_id):
            raise RuntimeError("pool publication attempt 身份漂移")
        return publication

    def _register_published_evaluation_log(
            self, cycle_id: str, publication: VerifiedPoolPublication,
            attempt_id: int) -> None:
        """Register and ingest the formal eval log before DB success sealing."""
        binding = publication.database_bindings["evaluation_attempt"]
        if binding.get("attempt_id") != attempt_id:
            raise RuntimeError("formal eval log attempt 绑定不一致")
        reference = binding["execution_log_ref"]
        digest = binding["execution_log_hash"]
        path = resolve_registered_path(self.work, reference)
        data = read_artifact_bytes(
            path, expected_hash=digest, label="formal evaluation execution log")
        elid = H.register_execution_log(
            self.state.daemon, cycle_id=cycle_id, log_kind="eval",
            ref=reference, content_hash=digest, n_bytes=len(data),
            evaluation_attempt_id=attempt_id)
        row = self.state.daemon.query_one(
            "SELECT ref,content_hash,bytes FROM execution_log WHERE id=?", (elid,))
        if row != (reference, digest, len(data)):
            raise RuntimeError(
                "attempt 已有同 hash 的非正式 execution_log，无法建立 formal DB 绑定")
        OP.ingest_observation(
            self.state.daemon, execution_log_id=elid, log_bytes=data,
            obs_policy=self.obs_policy)

    def _formal_variant_usable(self, variant_id: int) -> bool:
        """Re-hash the exact DB-closed publication before production reuse."""
        if not getattr(self.gate, "require_formal_publication", False):
            return True
        try:
            event = formal_publication_event(
                self.state.daemon.conn, variant_id=variant_id)
            if event is None or self.pool_publisher is None:
                return False
            publication = self.pool_publisher.verify_publication(
                event["manifest_ref"], expected_hash=event["manifest_hash"])
            objects = publication.payload["objects"]
            baseline = objects["baseline"]
            variant = objects["variant"]
            protocol = objects["protocol"]
            evaluation = objects["evaluation"]
            return (
                baseline["baseline_id"] == event["baseline_id"]
                and variant["variant_id"] == variant_id == event["variant_id"]
                and evaluation["evaluation_id"] == event["evaluation_id"]
                and evaluation["attempt_id"] == event["attempt_id"]
                and protocol["protocol_id"] == event["protocol_id"]
                and protocol["version"] == event["protocol_ver"]
            )
        except (PoolPublicationError, sqlite3.DatabaseError, OSError,
                KeyError, TypeError, ValueError):
            return False

    def _baseline_environment_hash(self, baseline_id: int) -> str:
        imported = self.state.daemon.query(
            "SELECT DISTINCT r.env_hash FROM variant v "
            "JOIN checkpoint c ON c.variant_id=v.id "
            "JOIN run r ON r.id=c.produced_by_run "
            "WHERE v.baseline_id=? AND c.origin='external_import' "
            "AND r.status='success' ORDER BY r.env_hash", (baseline_id,))
        if len(imported) > 1:
            raise RuntimeError(
                f"baseline {baseline_id} 绑定多个 external-import execution environment")
        if imported:
            value = imported[0][0]
            if (not isinstance(value, str)
                    or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None):
                raise RuntimeError(f"baseline {baseline_id} imported env_hash 非法")
            return value
        if self.execution_sandbox is None:
            return sandbox_environment_hash(self.policy["execution"]["sandbox"])
        return self.execution_sandbox.environment_hash

    def _target_environment_hash(self, build_target_id: int) -> str:
        row = self.state.daemon.query_one(
            "SELECT baseline_id FROM build_target WHERE id=?", (build_target_id,))
        if row is None:
            raise RuntimeError(f"build_target {build_target_id} 不存在")
        return self._baseline_environment_hash(row[0])

    def _execution_sandbox_for(self, manifest: Mapping[str, Any], build_target_id: int):
        runtime_environment_hash = self._target_environment_hash(build_target_id)
        gpu_required = manifest.get("gpu_required", False)
        if not isinstance(gpu_required, bool):
            raise _BundleReject(
                "manifest.gpu_required 须为 bool", failure_kind="artifact_invalid")
        expected = sandbox_workload_environment_hash(
            runtime_environment_hash, gpu_required)
        if manifest.get("env_hash") != expected:
            raise _BundleReject(
                "manifest.env_hash 未继承 build_target baseline 的可信 CPU/GPU workload identity",
                failure_kind="artifact_invalid")
        if (self.execution_sandbox is None
                or runtime_environment_hash == self.execution_sandbox.environment_hash):
            selected = self.execution_sandbox
        else:
            if self.execution_sandbox_resolver is None:
                raise RuntimeError(
                    "imported baseline 要求 dependency image，但系统未配置可信 resolver")
            selected = self.execution_sandbox_resolver.resolve_environment_hash(
                runtime_environment_hash)
            if (getattr(selected, "environment_hash", None)
                    != runtime_environment_hash):
                raise RuntimeError(
                    "dependency image resolver 返回了不同的 runtime identity")
        if gpu_required and getattr(selected, "gpu_contract", None) is None:
            raise _BundleReject(
                "plan 要求 GPU，但部署未建立可执行的 fixed GPU allocation",
                failure_kind="env_invalid")
        return selected

    def bind_owner_guard(self, owner_guard: Callable[[], None]) -> None:
        if not callable(owner_guard):
            raise TypeError("AttackStages owner_guard 须可调用")
        configured = self._configured_owner_guard

        def combined() -> None:
            owner_guard()
            configured()

        self.owner_guard = combined

    # ---------------------------------------------------------------- 调度 --
    def advance_stage(self, cyc) -> str:
        """按 cycle.status 游标推进一格；返回下一 stage 或 'done'。"""
        self.owner_guard()
        self.state.assert_current_cycle(cyc.cycle_id)
        if cyc.status == "created":
            selected = self._idea_stage(cyc)
            # 全部候选被拒是研究失败，不是编排器故障：idea 行与失败裁决已在同一事务
            # 入账，游标直接越过 plan/bundle，让 reasoning 正常把 Qn 收为
            # inconclusive 并选择下一步。不得调用一个没有 selected idea 的 planner。
            return "plan" if selected else "reasoning"
        if cyc.status == "idea":
            self._plan_stage(cyc)
            return ("done" if self.state.cycle(cyc.cycle_id).status
                    in ("done", "failed", "aborted") else "bundle")
        if cyc.status == "plan":
            return "reasoning" if self._bundle_stage(cyc) else "bundle"
        if cyc.status == "bundle":
            self._reasoning_stage(cyc)
            return "done"
        raise ValueError(f"attack 轮不可推进的游标 status={cyc.status!r}")

    # ---------------------------------------------------------------- idea --
    def _idea_stage(self, cyc) -> bool:
        """idea 阶段（§3.2）：候选全量入 IDEA 表（防重复造轮的关键边，含 failed）+ selected 标记。单一事务。
        **消费冻结 idea_set.schema**（步⑧ CP8.2）：content_md **机械合成**（schema 无 content_md 键——
        由 core_claim/mechanism/assumptions/MFE/audit_mapping 拼装）；audit_score 取该候选六维审计均值、
        status 由 selected_id / audit decision 派生（audit_scores 是独立顶层数组，按 candidate_id 关联）。
        ``audit_json`` 保持既有 audit 字段在顶层，并附最终 provenance 与该候选 wildidea_extra；同事务写
        ``DECISION(actor=judge,type=idea_audit)``，使独立审计裁决不只停留在 runner 产物文件。

        返回是否存在 selected idea。``selected_id=null`` 是 schema 明定的正常研究失败：候选与
        phase_commit 照常入账，另写 ``idea_stage_failed`` 裁决，并把 cycle 游标直接推进到
        ``bundle``（下一格为 reasoning），从而跳过无输入的 plan 与空 bundle。"""
        pack = self.compiler.render(cycle_id=cyc.cycle_id, stage="idea")
        files = self.p["idea"](cyc, pack)
        iset = files["idea_set.json"]
        cands, selected = iset["candidates"], iset.get("selected_id")
        audits = {a["candidate_id"]: a for a in iset.get("audit_scores", [])}
        provenance = iset.get("provenance")
        nrefs = json.dumps(iset.get("novelty_refs", []), ensure_ascii=False, sort_keys=True)
        if selected is not None and selected not in {c["candidate_id"] for c in cands}:
            raise ValueError(f"idea selected_id {selected!r} 不在候选集")
        ah = _canon_hash(iset)
        ci, qi = _cnum(cyc.cycle_id), int(cyc.question_id[1:])
        with self.state.daemon.transaction() as conn:
            # duplicate/conflict 在单写者+status 游标下为**防御分支**（重做只发生在回滚后=无已提交行→恒 new；
            # 游标已推进则 advance 不会再进本阶段）——留作多写者/游标损毁的兜底，勿依赖（内审 NIT 注记）。
            pc = check_or_record(conn, cycle_id=cyc.cycle_id, stage="idea", target_id=None, artifact_hash=ah)
            if pc == "conflict":
                raise ValueError("idea 阶段 phase_commit 冲突：同键异 artifact_hash（staging 被改写？）")
            if pc == "duplicate":
                # 已提交（kill-9 后重做路径）；从 DB 真相恢复分支结果，不能把 None
                # 误当“有 selected”而在重启后闯入 plan。
                return conn.execute(
                    "SELECT 1 FROM idea WHERE cycle_id=? AND question_id=? "
                    "AND status='selected' LIMIT 1", (ci, qi)).fetchone() is not None
            for c in cands:
                cid = c["candidate_id"]
                audit = audits.get(cid)
                if cid == selected:
                    st = "selected"
                elif audit is not None and audit.get("decision") == "fail":
                    st = "failed"
                else:
                    st = "candidate"
                conn.execute(
                    "INSERT INTO idea(question_id,cycle_id,content_md,novelty_refs_json,audit_score,audit_json,status) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (qi, ci, _synth_content_md(c), nrefs, _audit_mean(audit),
                     _persisted_idea_audit(c, audit, provenance), st))
            conn.execute(
                "INSERT INTO decision(cycle_id,question_id,actor,type,payload_json) "
                "VALUES (?,?,'judge','idea_audit',?)",
                (ci, qi, json.dumps({
                    "protocol": "idea-audit-v1",
                    "question_id": f"q{qi}",
                    "candidate_ids": [c["candidate_id"] for c in cands],
                    "audit_scores": iset.get("audit_scores", []),
                    "selected_id": selected,
                    "provenance_hash": "sha256:" + _canon_hash(provenance or {}),
                }, ensure_ascii=False, sort_keys=True)))
            if selected is None:
                conn.execute(
                    "INSERT INTO decision(cycle_id,question_id,actor,type,payload_json) "
                    "VALUES (?,?,'orchestrator','idea_stage_failed',?)",
                    (ci, qi, json.dumps({
                        "protocol": "idea-stage-failed-v1",
                        "reason": "no_selected_candidate",
                        "question_id": f"q{qi}",
                        "candidate_count": len(cands),
                    }, ensure_ascii=False, sort_keys=True)))
                conn.execute("UPDATE cycle SET status='bundle' WHERE id=?", (ci,))
            else:
                conn.execute("UPDATE cycle SET status='idea' WHERE id=?", (ci,))
        return selected is not None

    # ---------------------------------------------------------------- plan --
    def _plan_stage(self, cyc) -> None:
        """plan 阶段（步⑧ CP8.2 重写）：消费**冻结 plan.schema** → 走正式 gate 通道（gate_new_protocol I1 +
        gate_claim_baseline I5）→ 落 build_target[]（plan_ref=**resolved 切片**）。

        **可恢复短事务序列**（gate 各自事务，不共一大事务——WriteDaemon 单写不可嵌套；同 bundle 注册段范式）：
        persist-then-consume plan.json（崩溃重放得同一 plan）→ 纯读派生（protocol/metric int 映射确定性）→
        gate_new_protocol（幂等：已注册则跳过）→ 逐目标 gate_claim_baseline（本 cycle 已占则复用）→ 终局单
        事务落全部 build_target + required_metric + phase_commit + status。
        **全自动不楔死**：Codex 产的**任何**站不住的 plan——结构非法（缺键/非 schema-conform）、语义非法
        （I1 冲突 / canonical_key 被他轮占 / plan 内 canonical_key 重复 / required 版本不符 / 未支持的 target
        kind）——一律 **_PlanReject → 业务拒**（记 decision(plan_rejected) + 零 target 终态）→ bundle 空转 →
        reasoning 收 inconclusive，**绝不 raise 到 advance**（否则轮永不推进=死循环）。故 schema 校验 + 结构
        取键 + 派生 + gate 全裹在 try 内、异常一律归 _PlanReject/GateReject（内审 SHOULD-高：裸 KeyError 曾逃逸）。"""
        ci, qi = _cnum(cyc.cycle_id), int(cyc.question_id[1:])
        if self._plan_committed(ci):
            return                              # 幂等/恢复：plan 阶段已终态（成功或业务拒）
        plan = None                             # 信封获取也纳入 try（codex BLOCKER：缺 plan.json 键/JSON 损坏不得逃逸）
        try:
            plan = self._plan_artifact(cyc)     # plan 自己锁定研究语义；编排器只核验/解析/占坑
            self._validate_plan_schema(plan)    # 结构闸（防裸 KeyError 逃逸）：非 schema-conform → _PlanReject
            if "import_defer" in plan:
                if self.qualification_firewall is not None:
                    raise _PlanReject(
                        "T1/T2 qualification 从头约束禁止物化既有 repo/code/baseline；"
                        "外部仓库只能作为文献线索")
                self._commit_import_defer(cyc, plan)
                return
            targets = sorted(plan["targets"], key=lambda x: x["seq"])
            for t in targets:
                if t["target_kind"] not in ("build", "exec", "eval"):
                    raise _PlanReject(f"不支持的 plan target kind: {t['target_kind']}")
            if not targets:                     # 无 target（复用/聚合/idea 失败）：合法终态、零 target（非拒）
                validated_reuse = self._validate_reuse_only_plan(cyc, plan)
                self._commit_plan_terminal(
                    cyc, plan, built=[], reject=None,
                    validated_reuse=validated_reuse)
                return
            derived = self._derive_plan(ci, plan, targets)      # 纯读派生（build/exec 占坑身份前置判）
            self._register_protocol(cyc.cycle_id, derived)      # gate_new_protocol（幂等跳过）
            claims = self._claim_targets(ci, cyc.cycle_id, derived)   # build→claim_baseline / exec→claim_variant
        except _PlanTerminalized:
            return
        except (_PlanReject, GateReject) as e:                  # 非法 plan / gate 拒 → 业务拒收尾（不楔死）
            rejected_plan = getattr(e, "plan", plan)
            self._commit_plan_terminal(cyc, rejected_plan, built=[], reject=str(e))
            return
        self._commit_plan_terminal(cyc, plan, built=[(d, claims[d["target_key"]]) for d in derived["targets"]],
                                   reject=None)

    def _commit_import_defer(self, cyc, plan: Dict[str, Any]) -> None:
        """Commit dependency_wait, then route the cycle through Reasoning.

        ``selected_for_materialization + baseline(planned) + question_dep(pending) + phase_commit + route``
        are one transaction.  The active question lease is intentionally kept
        and cycle.status advances to ``bundle`` so the next and mandatory stage
        is Reasoning.  Reasoning summarizes this cycle, records its decision,
        then releases the waiting question without counting an inconclusive
        research attempt.  No build_target is created here.
        """
        ah = _canon_hash(plan)
        with self.state.atomic() as conn:
            pc = check_or_record(
                conn, cycle_id=cyc.cycle_id, stage="plan",
                target_id=None, artifact_hash=ah)
            if pc == "conflict":
                raise ValueError(
                    "import_defer plan phase_commit 冲突：同键异 artifact_hash")
            if pc == "duplicate":
                raise RuntimeError(
                    "import_defer phase_commit 已存在但 cycle 仍进入 plan；原子终态不变量损坏")
            try:
                DeferredImporter.select_plan_deferred_in_txn(
                    conn, question_id=cyc.question_id,
                    action_cycle=cyc.cycle_id,
                    import_defer=plan["import_defer"], policy=self.policy)
            except ValueError as error:
                raise _PlanReject(f"import_defer 确定性选择被拒：{error}") from error
            self.state.set_route(cyc.cycle_id, "dependency_wait")
            changed = conn.execute(
                "UPDATE cycle SET status='bundle' WHERE id=? AND status='idea'",
                (_cnum(cyc.cycle_id),)).rowcount
            if changed != 1:
                raise RuntimeError("dependency_wait 无法推进到 mandatory Reasoning 游标")

    def _validate_plan_schema(self, plan: Dict[str, Any]) -> None:
        """plan.json 结构闸（编排器侧防御，不只靠 StageProvider——同 manifest 校验在 _obtain_manifest 侧）：
        非 schema-conform → _PlanReject（业务拒，不楔死）。schemas 未注入（老测试路径）则跳过。"""
        # GPU allocation is a user/runtime choice, not research semantics the
        # planner should fail a whole cycle for repeating incorrectly.  In the
        # fixed required/forbidden profiles, normalize only new target booleans
        # before schema validation.  planner_select and historical reuse facts
        # remain Codex-owned.
        self._normalize_plan_target_gpu_mode(plan)
        try:
            # jsonschema treats NaN/Infinity as Python numbers on some versions.  They are not JSON
            # values and would otherwise fail later while hashing/rendering the independent review,
            # turning one bad model artifact into a restart-stable poison cycle.
            json.dumps(plan, ensure_ascii=False, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise _PlanReject(f"plan.json 含非有限或不可编码 JSON 值：{error}") from error
        if self.schemas is not None:
            errs = [f"{e.json_path} {e.message}" for e in self.schemas.validator("plan").iter_errors(plan)]
            if errs:
                raise _PlanReject("plan.json 非 schema-conform: " + "; ".join(errs[:5]))
        self._validate_plan_gpu_target_contract(plan)

    def _normalize_plan_target_gpu_mode(self, plan: Dict[str, Any]) -> None:
        if not isinstance(self.policy, Mapping) or not isinstance(plan, dict):
            return
        resources = self.policy.get("resources")
        if not isinstance(resources, Mapping):
            return
        target_policy = resources.get("gpu_target_policy")
        if target_policy == "planner_select":
            return
        if target_policy not in {"required", "forbidden"}:
            return
        targets = plan.get("targets")
        if not isinstance(targets, list):
            return
        expected = target_policy == "required"
        for target in targets:
            if isinstance(target, dict):
                target["gpu_required"] = expected

    def _validate_plan_gpu_target_contract(self, plan: Mapping[str, Any]) -> None:
        """Enforce the deployment's explicit per-target GPU-mode policy.

        New target booleans were already normalized from the user's fixed
        runtime allocation. Reuse-only plans still obey the same mode contract
        because a historical CPU/GPU measurement identity is research evidence
        and must not be rewritten.
        """
        if self.policy is None:
            return
        resources = self.policy.get("resources")
        if not isinstance(resources, Mapping):
            raise RuntimeError("policy.resources 须为 object")
        target_policy = resources.get("gpu_target_policy")
        if target_policy not in {"planner_select", "required", "forbidden"}:
            raise RuntimeError("policy.resources.gpu_target_policy 非法")
        if target_policy == "planner_select":
            return
        expected = target_policy == "required"
        targets = plan.get("targets", [])
        if not isinstance(targets, list):
            # The schema-enabled path reports the more specific shape error;
            # this protects legacy tests/embedders that omit SchemaSet.
            raise _PlanReject("plan.targets 须为数组")
        mismatched_targets = []
        for index, item in enumerate(targets):
            if not isinstance(item, Mapping):
                mismatched_targets.append(f"#{index + 1}")
            elif item.get("gpu_required") is not expected:
                mismatched_targets.append(item.get("target_key", f"#{index + 1}"))
        reuse = plan.get("reuse_evidence", [])
        if not isinstance(reuse, list):
            raise _PlanReject("plan.reuse_evidence 须为数组")
        mismatched_reuse = [
            item.get("evaluation_id", f"#{index + 1}")
            for index, item in enumerate(reuse)
            if (isinstance(item, Mapping) and item.get("kind") == "evaluation"
                and item.get("gpu_required") is not expected)
        ]
        if mismatched_targets or mismatched_reuse:
            raise _PlanReject(
                "plan GPU mode 与 policy.resources.gpu_target_policy="
                f"{target_policy} 冲突；期望 gpu_required="
                f"{str(expected).lower()}；targets={mismatched_targets!r}；"
                f"evaluations={mismatched_reuse!r}")

    def _plan_committed(self, ci: int) -> bool:
        return self.state.daemon.query_one(
            "SELECT 1 FROM phase_commit WHERE cycle_id=? AND stage='plan' AND target_id IS NULL", (ci,)) is not None

    def _plan_artifact(self, cyc) -> Dict[str, Any]:
        """persist-then-consume（同 _reasoning_stage）：plan.json 先原子落盘，恢复复用同一 plan——否则多事务
        gate 序列下崩溃重调非确定 provider 会产不同 plan → 半注册孤儿（protocol 注册了但 target 用了新 plan）。
        **信封/解析失败统一转 _PlanReject**（codex BLOCKER：provider 返回缺 plan.json 键 / 落盘文件 JSON 损坏
        会抛裸 KeyError/JSONDecodeError 逃出 _plan_stage 的 except → 楔死）。本方法在 _plan_stage 的 try 内调，
        故 _PlanReject 会被业务拒兜住。**注**：provider 进程级失败（RunnerError）不在此转——那是「未产出 plan」
        的另一失败类，与其他阶段（idea/reasoning）一致向上传播。"""
        cycle_dir = self.work / f"c{_cnum(cyc.cycle_id)}"
        art = cycle_dir / "plan.json"
        review_result_path = cycle_dir / "plan.review-result.json"
        search_request_path = cycle_dir / "import_search_request.json"
        legacy_plan = None
        if art.exists():
            try:
                plan = json.loads(read_artifact_bytes(
                    art, label="persisted plan artifact").decode("utf-8"))
            except json.JSONDecodeError as e:
                raise _PlanReject(f"持久 plan.json 解析失败（staging 损坏？）：{e}") from e
            # import_defer 在图 04 的 IMP→WAIT 分支于 protocol/review 之前机械收尾；它没有
            # protocol/metrics/targets，不能拿普通实验计划的 answerability checklist 审。
            if "plan_review" not in self.p or "import_defer" in plan:
                return plan
            if review_result_path.exists():
                result = self._read_plan_review_result(
                    review_result_path, plan, cyc.cycle_id)
                if result["status"] == "exhausted":
                    raise _PlanReviewReject(
                        f"plan 可回答性评审 {result.get('round_no')} 轮未通过："
                        f"{result.get('issues', [])}", plan)
                if result["status"] not in {
                        "pass", "repaired_after_single_review"}:
                    raise RuntimeError(
                        f"持久 plan review result 状态非法: {result['status']!r}")
                return plan
            # 兼容升级前已经产出、尚未 phase_commit 的 plan：不把缺 sidecar 当永久楔死；
            # 把同一字节身份作为 r1 draft 补做独立评审。DB verdict 仍是唯一权威。
            legacy_plan = plan

        pack = self.compiler.render(cycle_id=cyc.cycle_id, stage="plan")
        search_requested = False
        if search_request_path.exists():
            request = self._read_import_search_request(search_request_path)
            outcome = self._run_import_search(cyc, request, pack)
            if outcome.get("terminalized") is True:
                self._assert_import_control_terminalized(cyc, outcome)
                raise _PlanTerminalized()
            # Registration is a short atomic DB commit.  The old pack was
            # rendered before it and must never be passed to the next plan
            # call, otherwise the model could not see the exact frozen anchors.
            pack = self.compiler.render(cycle_id=cyc.cycle_id, stage="plan")
            search_requested = True
        if "plan_review" not in self.p:
            plan, pack, search_requested = self._plan_provider_with_import_search(
                cyc, pack, search_request_path=search_request_path,
                search_requested=search_requested)
            self._validate_plan_schema(plan)
            self._write_json_atomic(art, plan)
            return plan

        max_rounds = self.policy["flow"]["retry"]["plan_review"]
        if isinstance(max_rounds, bool) or not isinstance(max_rounds, int) or not 1 <= max_rounds <= 2:
            raise RuntimeError("policy.flow.retry.plan_review 必须在 1..2")
        last_review = None
        for round_no in range(1, max_rounds + 1):
            draft_path = cycle_dir / f"plan.draft-r{round_no}.json"
            if draft_path.exists():
                try:
                    plan = json.loads(read_artifact_bytes(
                        draft_path, label="plan draft artifact").decode("utf-8"))
                except json.JSONDecodeError as error:
                    raise RuntimeError(f"持久 plan draft r{round_no} JSON 损坏") from error
                if (round_no == 1 and legacy_plan is not None
                        and _canon_hash(plan) != _canon_hash(legacy_plan)):
                    raise RuntimeError(
                        "升级恢复发现 plan.json 与 plan.draft-r1.json 身份冲突")
            elif round_no == 1 and legacy_plan is not None:
                plan = legacy_plan
                self._validate_plan_schema(plan)
                self._write_json_atomic(draft_path, plan)
            else:
                if round_no == 1:
                    plan, pack, search_requested = self._plan_provider_with_import_search(
                        cyc, pack, search_request_path=search_request_path,
                        search_requested=search_requested)
                else:
                    files = self.p["plan"](cyc, pack)
                    if (isinstance(files, dict)
                            and "import_search_request.json" in files):
                        raise _PlanReject(
                            "plan 可回答性修订轮不得新发 import_search；"
                            "每 action-cycle 最多一次只读发现")
                    plan = self._plan_from_provider(files)
                self._validate_plan_schema(plan)
                self._write_json_atomic(draft_path, plan)
            self._validate_plan_schema(plan)
            if "import_defer" in plan:
                self._write_json_atomic(art, plan)
                return plan
            plan_hash = _canon_hash(plan)
            existing = self._existing_plan_review(cyc.cycle_id, round_no, plan_hash)
            if existing is None:
                review, decision_id = self.p["plan_review"](
                    cyc, plan, round_no,
                    self.compiler.render_plan_review(
                        cycle_id=cyc.cycle_id, plan=plan, round_no=round_no))
                durable = self._existing_plan_review(
                    cyc.cycle_id, round_no, plan_hash)
                if durable is None or durable[1] != decision_id:
                    raise RuntimeError(
                        "plan reviewer 返回后缺 exact durable judge decision")
                review, decision_id = durable
            else:
                review, decision_id = existing
            self._validate_plan_review(review, round_no)
            last_review = review
            if review["verdict"] == "pass":
                result = {
                    "status": "pass", "round_no": round_no,
                    "plan_hash": plan_hash, "decision_id": decision_id,
                    "issues": review.get("issues", []),
                }
                self._write_json_atomic(review_result_path, result)
                self._write_json_atomic(art, plan)
                return plan
            if round_no < max_rounds:
                pack = self.compiler.amend_plan_review_feedback(
                    pack, plan=plan, review=review, decision_id=decision_id)
                continue

            # ``review_intensity=once`` means exactly one independent clean
            # reviewer, not "one reviewer failure kills the whole research
            # cycle".  Give the original plan generator that durable feedback
            # once, then subject its replacement to all ordinary schema,
            # reference, budget and SQL gates.  We deliberately do not call a
            # second reviewer: the UI setting remains literal and the audit
            # record below binds rejected and repaired plan identities.
            repair_pack = self.compiler.amend_plan_review_feedback(
                pack, plan=plan, review=review, decision_id=decision_id)
            repair_path = cycle_dir / f"plan.repair-after-r{round_no}.json"
            try:
                if repair_path.exists():
                    repaired = json.loads(read_artifact_bytes(
                        repair_path, label="plan review repair artifact").decode("utf-8"))
                else:
                    repaired_files = self.p["plan"](cyc, repair_pack)
                    if (isinstance(repaired_files, dict)
                            and "import_search_request.json" in repaired_files):
                        raise _PlanReject(
                            "plan review 定向修订不得新发 import_search")
                    repaired = self._plan_from_provider(repaired_files)
                    self._validate_plan_schema(repaired)
                    self._write_json_atomic(repair_path, repaired)
                self._validate_plan_schema(repaired)
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise RuntimeError(
                    f"持久 plan review repair r{round_no} JSON 损坏") from error
            except _PlanReject as error:
                result = {
                    "status": "exhausted", "round_no": round_no,
                    "plan_hash": plan_hash, "decision_id": decision_id,
                    "issues": review.get("issues", []),
                    "repair_error": str(error),
                }
                self._write_json_atomic(review_result_path, result)
                self._write_json_atomic(art, plan)
                raise _PlanReviewReject(
                    "plan 独立评审未通过，且定向修订产物无效：" + str(error), plan) from error

            repaired_hash = _canon_hash(repaired)
            if repaired_hash == plan_hash:
                result = {
                    "status": "exhausted", "round_no": round_no,
                    "plan_hash": plan_hash, "decision_id": decision_id,
                    "issues": review.get("issues", []),
                    "repair_error": "generator returned the reviewed plan unchanged",
                }
                self._write_json_atomic(review_result_path, result)
                self._write_json_atomic(art, plan)
                raise _PlanReviewReject(
                    "plan 独立评审未通过，定向修订却原样返回被拒计划", plan)

            repair_decision_id = self._record_plan_review_repair(
                cycle_id=cyc.cycle_id, round_no=round_no,
                review_decision_id=decision_id,
                reviewed_plan_hash=plan_hash, repaired_plan_hash=repaired_hash,
                issues=review.get("issues", []))
            result = {
                "status": "repaired_after_single_review", "round_no": round_no,
                "reviewed_plan_hash": plan_hash, "plan_hash": repaired_hash,
                "decision_id": decision_id,
                "repair_decision_id": repair_decision_id,
                "issues": review.get("issues", []),
            }
            self._write_json_atomic(review_result_path, result)
            self._write_json_atomic(art, repaired)
            return repaired

        assert last_review is not None  # loop always returns/raises above
        raise RuntimeError("plan review loop 非法落空")

    def _plan_provider_with_import_search(
            self, cyc, pack, *, search_request_path: Path,
            search_requested: bool):
        """Legacy adapter: consume one discovery sidecar between Plan calls.

        Normal resident Plan never enters this branch: it calls
        ``plan_import_search`` through runtime MCP and continues inside the same
        top-level turn.  This path remains only for explicit legacy/qualification
        adapters.  Its request is persisted before any network call so recovery
        can reconcile the same read without inventing another query.
        """
        files = self.p["plan"](cyc, pack)
        if not (isinstance(files, dict)
                and "import_search_request.json" in files):
            return self._plan_from_provider(files), pack, search_requested
        if search_requested or search_request_path.exists():
            raise _PlanReject(
                "同一 action-cycle 的第二个 import_search_request 被拒绝")
        if set(files) != {"import_search_request.json"}:
            raise _PlanReject(
                "import_search_request.json 须独占 plan provider files")
        try:
            request = validate_import_search_request(
                files["import_search_request.json"])
            if self.schemas is not None:
                errors = [
                    f"{error.json_path} {error.message}"
                    for error in self.schemas.validator(
                        "import_search_request").iter_errors(request)
                ]
                if errors:
                    raise ImportSearchError("; ".join(errors[:5]))
        except ImportSearchError as error:
            raise _PlanReject(
                f"import_search_request 非 schema-conform：{error}") from error
        # This control record is the recovery authority for the external read,
        # so unlike ordinary staging drafts it is fsync'd before the connector
        # is invoked and published as a private no-follow receipt.
        atomic_write_receipt(search_request_path, request)
        outcome = self._run_import_search(cyc, request, pack)
        if outcome.get("terminalized") is True:
            self._assert_import_control_terminalized(cyc, outcome)
            raise _PlanTerminalized()
        refreshed = self.compiler.render(cycle_id=cyc.cycle_id, stage="plan")
        final_files = self.p["plan"](cyc, refreshed)
        if (isinstance(final_files, dict)
                and "import_search_request.json" in final_files):
            raise _PlanReject(
                "import_search 完成后 plan 仍请求第二次搜索；"
                "必须消费冻结候选/零结果并做决策")
        return self._plan_from_provider(final_files), refreshed, True

    def _run_import_search(self, cyc, request: Dict[str, Any], pack) -> Dict[str, Any]:
        provider = self.p.get("import_search")
        if provider is None:
            raise RuntimeError(
                "plan 产出 import_search_request，但本装配缺 import_search 受信 connector")
        return provider(cyc, request, pack)

    def _assert_import_control_terminalized(self, cyc, outcome: Dict[str, Any]) -> None:
        child_id = outcome.get("child_question_id")
        if isinstance(child_id, bool) or not isinstance(child_id, int) or child_id <= 0:
            raise RuntimeError("import trigger 自报 terminalized 但缺合法 child_question_id")
        cycle = self.state.daemon.query_one(
            "SELECT status,active_question_id,next_question_id,next_intent FROM cycle WHERE id=?",
            (_cnum(cyc.cycle_id),))
        committed = self.state.daemon.query_one(
            "SELECT 1 FROM phase_commit WHERE cycle_id=? AND stage='plan' "
            "AND target_id IS NULL", (_cnum(cyc.cycle_id),))
        if cycle != ("done", None, child_id, "attack") or committed is None:
            raise RuntimeError(
                "import trigger terminalized 回执与 cycle/plan phase_commit 不一致")

    def _read_import_search_request(self, path: Path) -> Dict[str, Any]:
        try:
            payload = read_receipt(path)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            raise RuntimeError(
                f"持久 import_search_request 损坏: {path}") from error
        try:
            request = validate_import_search_request(payload)
        except ImportSearchError as error:
            raise RuntimeError(
                f"持久 import_search_request 边界非法: {error}") from error
        if self.schemas is not None:
            errors = [
                f"{item.json_path} {item.message}"
                for item in self.schemas.validator(
                    "import_search_request").iter_errors(request)
            ]
            if errors:
                raise RuntimeError(
                    "持久 import_search_request schema 损坏: " + "; ".join(errors[:5]))
        return request

    @staticmethod
    def _plan_from_provider(files: Any) -> Dict[str, Any]:
        if isinstance(files, dict) and "import_search_request.json" in files:
            raise _PlanReject(
                "import_search_request 未在受限的 plan 首轮控制边界消费")
        if not isinstance(files, dict) or "plan.json" not in files:
            raise _PlanReject(
                f"plan provider 未产 plan.json（返回键: "
                f"{list(files) if isinstance(files, dict) else type(files)}）")
        if not isinstance(files["plan.json"], dict):
            raise _PlanReject("plan provider 的 plan.json 须为 object")
        return files["plan.json"]

    @staticmethod
    def _write_json_atomic(path: Path, payload: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False),
            encoding="utf-8")
        tmp.replace(path)

    def _validate_plan_review(self, review: Any, round_no: int) -> None:
        if not isinstance(review, dict):
            raise RuntimeError("plan reviewer 返回值须为 object")
        if self.schemas is not None:
            errors = [
                f"{error.json_path} {error.message}"
                for error in self.schemas.validator("plan_review").iter_errors(review)
            ]
            if errors:
                raise RuntimeError("plan review verdict 结构损坏: " + "; ".join(errors[:5]))
        elif review.get("verdict") not in ("pass", "fail"):
            raise RuntimeError("plan review verdict 非 pass/fail")
        if review.get("round_no") != round_no:
            raise RuntimeError(
                f"plan review round_no={review.get('round_no')!r}，期望 {round_no}")

    def _record_plan_review_repair(
            self, *, cycle_id: str, round_no: int, review_decision_id: int,
            reviewed_plan_hash: str, repaired_plan_hash: str,
            issues: List[Dict[str, Any]]) -> int:
        """Durably bind the one-review/one-repair transition for replay.

        The independent judge decision remains immutable and applies only to
        ``reviewed_plan_hash``.  This separate agent decision makes it explicit
        that the final plan is a generator repair, not a silently re-labelled
        reviewer pass.
        """
        payload = {
            "protocol": "plan-review-repair-v1",
            "round_no": round_no,
            "review_decision_id": review_decision_id,
            "reviewed_plan_hash": reviewed_plan_hash,
            "plan_hash": repaired_plan_hash,
            "issues": issues,
        }
        rows = self.state.daemon.query(
            "SELECT id,payload_json FROM decision WHERE cycle_id=? "
            "AND actor='agent' AND type='plan_review_repair' ORDER BY id",
            (_cnum(cycle_id),))
        if len(rows) > 1:
            raise RuntimeError(
                f"plan review repair c{_cnum(cycle_id)} 有多个 durable decision")
        if rows:
            existing = json.loads(rows[0][1])
            if existing != payload:
                raise RuntimeError("plan review repair durable identity 漂移")
            return int(rows[0][0])
        with self.state.daemon.transaction() as conn:
            return int(conn.execute(
                "INSERT INTO decision(cycle_id,actor,type,payload_json) "
                "VALUES (?,'agent','plan_review_repair',?)",
                (_cnum(cycle_id), json.dumps(
                    payload, ensure_ascii=False, sort_keys=True))).lastrowid)

    def _read_plan_review_result(self, path: Path, plan: Dict[str, Any],
                                 cycle_id: str) -> Dict[str, Any]:
        if not path.exists():
            raise RuntimeError("plan.json 存在但 plan.review-result.json 缺失")
        try:
            result = json.loads(read_artifact_bytes(
                path, label="stage artifact").decode("utf-8"))
        except json.JSONDecodeError as error:
            raise RuntimeError("plan.review-result.json 损坏") from error
        allowed_statuses = {
            "pass", "exhausted", "repaired_after_single_review"}
        if (not isinstance(result, dict) or result.get("plan_hash") != _canon_hash(plan)
                or result.get("status") not in allowed_statuses):
            raise RuntimeError("plan.review-result 与最终 plan 身份不一致")
        round_no = result.get("round_no")
        decision_id = result.get("decision_id")
        if isinstance(round_no, bool) or not isinstance(round_no, int) or round_no <= 0:
            raise RuntimeError("plan.review-result round_no 非法")
        reviewed_plan_hash = (
            result.get("reviewed_plan_hash")
            if result["status"] == "repaired_after_single_review"
            else result["plan_hash"])
        if (not isinstance(reviewed_plan_hash, str)
                or re.fullmatch(r"[0-9a-f]{64}", reviewed_plan_hash) is None):
            raise RuntimeError("plan.review-result reviewed_plan_hash 非法")
        existing = self._existing_plan_review(
            cycle_id, round_no, reviewed_plan_hash)
        if existing is None or existing[1] != decision_id:
            raise RuntimeError("plan.review-result 未绑定唯一 durable judge decision")
        review, _ = existing
        expected_verdict = "pass" if result["status"] == "pass" else "fail"
        if review["verdict"] != expected_verdict:
            raise RuntimeError("plan.review-result 状态与 durable judge verdict 不一致")
        if result.get("issues", []) != review.get("issues", []):
            raise RuntimeError("plan.review-result issues 与 durable judge verdict 不一致")
        if result["status"] == "exhausted" and round_no != self.policy["flow"]["retry"]["plan_review"]:
            raise RuntimeError("plan.review-result exhausted 轮次与冻结 policy 不一致")
        if result["status"] == "repaired_after_single_review":
            if (round_no != self.policy["flow"]["retry"]["plan_review"]
                    or reviewed_plan_hash == result["plan_hash"]):
                raise RuntimeError("plan.review-result 单次评审修订身份非法")
            repair_decision_id = result.get("repair_decision_id")
            if (isinstance(repair_decision_id, bool)
                    or not isinstance(repair_decision_id, int)
                    or repair_decision_id <= 0):
                raise RuntimeError("plan.review-result repair_decision_id 非法")
            row = self.state.daemon.query_one(
                "SELECT payload_json FROM decision WHERE id=? AND cycle_id=? "
                "AND actor='agent' AND type='plan_review_repair'",
                (repair_decision_id, _cnum(cycle_id)))
            expected_payload = {
                "protocol": "plan-review-repair-v1",
                "round_no": round_no,
                "review_decision_id": decision_id,
                "reviewed_plan_hash": reviewed_plan_hash,
                "plan_hash": result["plan_hash"],
                "issues": review.get("issues", []),
            }
            if row is None or json.loads(row[0]) != expected_payload:
                raise RuntimeError(
                    "plan.review-result 未绑定 exact durable repair decision")
        return result

    def _existing_plan_review(self, cycle_id: str, round_no: int,
                              plan_hash: str):
        rows = self.state.daemon.query(
            "SELECT d.id,d.payload_json,rc.status,rc.phase,rc.purpose,rc.cycle_id "
            "FROM decision d LEFT JOIN runner_call rc ON rc.id="
            "json_extract(d.payload_json,'$.runner_call_id') "
            "WHERE d.cycle_id=? AND d.actor='judge' AND d.type='plan_review' "
            "AND json_valid(d.payload_json) "
            "AND json_extract(d.payload_json,'$.round_no')=? ORDER BY d.id",
            (_cnum(cycle_id), round_no))
        if len(rows) > 1:
            raise RuntimeError(
                f"plan review c{_cnum(cycle_id)} round {round_no} 有多个 verdict")
        if not rows:
            return None
        decision_id, raw, status, phase, purpose, runner_cycle_id = rows[0]
        if ((status, phase, purpose) != ("success", "audit", "plan_review")
                or runner_cycle_id != _cnum(cycle_id)):
            raise RuntimeError(
                f"plan review decision {decision_id} 无 success audit runner_call")
        payload = json.loads(raw)
        if payload.get("plan_hash") != plan_hash:
            raise RuntimeError(
                f"plan review c{_cnum(cycle_id)} round {round_no} 的 durable plan 身份漂移")
        review = {
            "verdict": payload.get("verdict"), "round_no": payload.get("round_no"),
            "issues": payload.get("issues", []), "notes_md": payload.get("notes_md", ""),
        }
        if review["verdict"] not in ("pass", "fail"):
            raise RuntimeError(f"plan review decision {decision_id} verdict 损坏")
        return review, decision_id

    def _derive_plan(self, ci: int, plan: Dict[str, Any], targets: List[Dict[str, Any]]) -> Dict[str, Any]:
        """纯读派生（编排器机械翻译，不推理；确定性 = 崩溃重放同结果）：抽象 plan → protocol int id / metric
        string→int 映射 / 每目标 resolved 切片（冻结 target + 绑定四件）+ required int 对 + identity 草稿。
        全部前置判失败一律 → _PlanReject（业务拒，不 raise 死循环）：I1 scope 冲突 / 复用协议缺 metric 绑定 /
        target_key·seq·metric_id 重复 / required 悬挂引用 / canonical_key 冲突（codex BLOCKER×3）。"""
        d = self.state.daemon
        seqs = [t["seq"] for t in targets]
        if seqs != list(range(1, len(targets) + 1)):
            raise _PlanReject(f"targets.seq 须为从 1 起连续依赖序，实收 {seqs}")
        estimates = []
        for target in targets:
            estimate = target.get("budget_estimate")
            if (isinstance(estimate, bool) or not isinstance(estimate, (int, float))
                    or not math.isfinite(float(estimate)) or float(estimate) < 0):
                raise _PlanReject(
                    f"target {target.get('target_key')!r} budget_estimate 须为有限非负数")
            estimates.append(float(estimate))
        estimate_total = math.fsum(estimates)
        cycle_budget = compute_budget(d.conn, self.policy["budget"])
        if estimate_total > cycle_budget:
            raise _PlanReject(
                f"targets budget_estimate 总和 {estimate_total} 超出本轮 B(t)={cycle_budget}")
        # target 平面唯一性（codex BLOCKER：target_key 重复 → claims dict 覆盖、build_target 错绑；seq 重复 →
        # 终局事务撞 UNIQUE(cycle_id,seq) IntegrityError 楔死）——派生期拦下转业务拒
        tkeys = [t["target_key"] for t in targets]
        if len(set(tkeys)) != len(tkeys):
            raise _PlanReject(f"targets 内 target_key 重复: {tkeys}")
        if len({t["seq"] for t in targets}) != len(targets):
            raise _PlanReject(f"targets 内 seq 重复: {[t['seq'] for t in targets]}")
        for rm in plan.get("build_target_required_metric", []):
            if rm["target_key"] not in set(tkeys):
                raise _PlanReject(f"required_metric.target_key {rm['target_key']!r} 无对应 target")
        proto = plan["protocol"]
        pname, pver = proto["name"], proto["version"]
        scope_json = json.dumps(proto["scope_spec"], ensure_ascii=False, sort_keys=True)
        prow = d.query_one("SELECT id FROM protocol WHERE name=? ORDER BY id LIMIT 1", (pname,))
        pid = prow[0] if prow else (d.query_one("SELECT COALESCE(MAX(id),0) FROM protocol")[0] + 1)
        exist = d.query_one("SELECT scope_spec_json FROM protocol WHERE id=? AND version=?", (pid, pver))
        proto_exists = exist is not None
        if proto_exists and json.dumps(json.loads(exist[0]), sort_keys=True) != json.dumps(json.loads(scope_json), sort_keys=True):
            raise _PlanReject(f"I1：protocol {pname}@{pver} 已存在且 scope 不同——改场景须升 version")
        # metric string→int 映射（身份=name；同 plan 内重名共 id；DB 已有按 name 复用；否则顺序分配 max+1..）
        mmap: Dict[str, int] = {}
        def_versions = set()                    # (plan_metric_id, version) 声明集——required 版本一致性核（内审 SHOULD）
        defs: List[Dict[str, Any]] = []
        next_id = d.query_one("SELECT COALESCE(MAX(id),0) FROM metric_def")[0]
        for md in plan.get("metric_defs", []):
            if md["metric_id"] in mmap:         # metric_id 是 plan 内 join 键，须唯一（codex BLOCKER）
                raise _PlanReject(f"metric_defs 内 metric_id 重复: {md['metric_id']!r}")
            def_versions.add((md["metric_id"], md["version"]))
            name = md["name"]
            if name in {defs_e["name"] for defs_e in defs}:
                mmap[md["metric_id"]] = next(dd["id"] for dd in defs if dd["name"] == name)
                continue
            row = d.query_one("SELECT id FROM metric_def WHERE name=? ORDER BY id LIMIT 1", (name,))
            if row:
                mid_int = row[0]
            else:
                next_id += 1
                mid_int = next_id
            mmap[md["metric_id"]] = mid_int
            defs.append({"id": mid_int, "version": md["version"], "name": name, "direction": md["direction"],
                         "unit": md.get("unit"), "compute_spec": md.get("compute_spec_md")})
        req_by_tk: Dict[str, List[tuple]] = {}
        for rm in plan.get("build_target_required_metric", []):
            if rm["metric_id"] not in mmap:
                raise _PlanReject(f"required_metric 引用未声明的 metric_id {rm['metric_id']!r}（不在 metric_defs）")
            if (rm["metric_id"], rm["metric_ver"]) not in def_versions:
                # required 版本须与 metric_defs 声明一致（内审 SHOULD）：否则 protocol_metric 落 def_ver、
                # eval/register 用 req_ver → I2 在 bundle 阶段拒（不 catch）→ 楔死；派生侧拦下转业务拒。
                raise _PlanReject(f"required_metric {rm['metric_id']}@{rm['metric_ver']} 版本与 metric_defs 声明不符")
            req_by_tk.setdefault(rm["target_key"], []).append((mmap[rm["metric_id"]], rm["metric_ver"]))
        if proto_exists:
            # 复用既有 protocol（跳过 gate_new_protocol）时，required 的每个 (int_id, ver) 必须已在
            # protocol_metric(pid,pver) 里——否则 bundle 的 gate_register_evaluation I2 拒→楔死（codex BLOCKER）。
            # 协议不可变（I1）：要用新 metric 集须升 version。派生期拦下转业务拒。
            for pairs in req_by_tk.values():
                for (mid_int, mver) in pairs:
                    if d.query_one("SELECT 1 FROM protocol_metric WHERE protocol_id=? AND protocol_ver=? "
                                   "AND metric_id=? AND metric_ver=?", (pid, pver, mid_int, mver)) is None:
                        raise _PlanReject(f"复用 protocol {pname}@{pver} 但 required metric {mid_int}@{mver} "
                                          "不在其 protocol_metric（I1：改 metric 集须升 version）")
        # 占坑身份前置判（按 kind）：build 占 canonical_key（唯一 + 未被他轮占）；exec 占既有 legal baseline
        # 下的新 variant_key（baseline_ref 须解析到 legal baseline；variant_key 未占；config 非空）。
        seen_ck, seen_bv = set(), set()
        resolved_eval: Dict[str, Dict[str, Any]] = {}
        for t in targets:
            tk = t["target_key"]
            if not isinstance(tk, str) or _SAFE_POOL_KEY_RE.fullmatch(tk) is None:
                raise _PlanReject(
                    f"target_key {tk!r} 非正式池安全键（ASCII 字母数字开头，仅 ._-，最长 128）")
            claim = t.get("claim", {})
            if t["target_kind"] == "build":
                ck, slug = claim["canonical_key"], claim["slug"]
                if (not isinstance(slug, str)
                        or _SAFE_POOL_KEY_RE.fullmatch(slug) is None):
                    raise _PlanReject(
                        f"baseline slug {slug!r} 非正式池安全路径键")
                if ck in seen_ck:
                    raise _PlanReject(f"plan 内 canonical_key 重复: {ck!r}（同轮多目标不得共占坑）")
                seen_ck.add(ck)
                occ = d.query_one("SELECT born_cycle, slug FROM baseline WHERE canonical_key=?", (ck,))
                if occ is not None and not (occ[0] == ci and occ[1] == slug):
                    raise _PlanReject(f"canonical_key 被他轮占（I5）: {ck!r}")   # 派生期拦下→claim 段不半途留孤儿
            elif t["target_kind"] == "exec":
                bref, vkey = claim["baseline_ref"], claim["variant_key"]
                if (not isinstance(vkey, str)
                        or _SAFE_POOL_KEY_RE.fullmatch(vkey) is None):
                    raise _PlanReject(
                        f"variant_key {vkey!r} 非正式池安全路径键")
                brow = d.query_one("SELECT id, status FROM baseline WHERE canonical_key=?", (bref,))
                if brow is None or brow[1] != "legal":
                    raise _PlanReject(f"exec baseline_ref {bref!r} 未解析到 legal baseline"
                                      f"（{'缺失' if brow is None else brow[1]}——首攻新家族须 build）")
                if getattr(self.gate, "require_formal_publication", False):
                    legal_variants = d.query(
                        "SELECT id FROM variant WHERE baseline_id=? AND status='legal' ORDER BY id",
                        (brow[0],))
                    if not any(self._formal_variant_usable(row[0]) for row in legal_variants):
                        raise _PlanReject(
                            f"exec baseline_ref {bref!r} 仅有 status=legal，缺正式池发布闭包")
                if not claim.get("config_json"):
                    raise _PlanReject(f"exec 目标 {tk} 缺 config_json（变体须有配置增量）")
                key = (brow[0], vkey)
                if key in seen_bv:
                    raise _PlanReject(f"plan 内 exec variant_key 重复: {vkey!r}（baseline {bref}）")
                seen_bv.add(key)
                vrow = d.query_one("SELECT id, config_json FROM variant WHERE baseline_id=? AND variant_key=?", key)
                if vrow is not None and not self._is_own_exec_reoccupy(vrow[0], ci, claim["config_json"]):
                    # variant_key 已占且**不是本轮自己的未终局 exec 占坑**（崩溃重放）→ 拒（他处占/身份漂移）。
                    # 「本轮自占」严核（codex 第2轮 BLOCKER）：pending + plan_ref NULL + config 一致——防身份
                    # 漂移（重放 plan 若换 config/seq 会把新 plan_ref 写到旧 variant 上，破坏确定性）。
                    raise _PlanReject(f"exec variant_key {vkey!r} 已占（baseline {bref}）")
            else:   # eval：只消费既有 legal target，不占新池身份。
                action = t["eval_action"]
                if action == "append_attempt":
                    try:
                        eid = _decode_id(t["evaluation_id"], "e")
                    except ValueError as error:
                        raise _PlanReject(
                            f"eval target {tk} evaluation_id 非 e<正整数>: {t.get('evaluation_id')!r}") from error
                    erow = d.query_one(
                        "SELECT e.variant_id,e.protocol_id,e.protocol_ver,e.eval_key,e.status,"
                        "v.baseline_id,v.status FROM evaluation e JOIN variant v ON v.id=e.variant_id "
                        "WHERE e.id=?", (eid,))
                    if erow is None or erow[4] == "abandoned" or erow[6] != "legal":
                        raise _PlanReject(
                            f"eval append 的 evaluation e{eid} 缺失/abandoned 或 variant 非 legal")
                    if not self._formal_variant_usable(erow[0]):
                        raise _PlanReject(
                            f"eval append 的 evaluation e{eid} 所属 variant 缺正式池发布闭包")
                    if (erow[1], erow[2]) != (pid, pver):
                        raise _PlanReject(
                            f"eval append e{eid} 协议 p{erow[1]}@{erow[2]} 与 plan p{pid}@{pver} 不一致")
                    vid, bid, eval_key = erow[0], erow[5], erow[3]
                    if (not isinstance(eval_key, str)
                            or _SAFE_POOL_KEY_RE.fullmatch(eval_key) is None):
                        raise _PlanReject(
                            f"eval append e{eid} 的 eval_key 非正式池安全路径键")
                    target_set_hash = d.query_one(
                        "SELECT target_set_hash FROM evaluation WHERE id=?", (eid,))[0]
                else:
                    bref, vkey = claim["baseline_ref"], claim["variant_key"]
                    if (not isinstance(vkey, str)
                            or _SAFE_POOL_KEY_RE.fullmatch(vkey) is None):
                        raise _PlanReject(
                            f"eval variant_key {vkey!r} 非正式池安全路径键")
                    if (not isinstance(t.get("eval_key"), str)
                            or _SAFE_POOL_KEY_RE.fullmatch(t["eval_key"]) is None):
                        raise _PlanReject(
                            f"eval_key {t.get('eval_key')!r} 非正式池安全路径键")
                    vrow = d.query_one(
                        "SELECT v.id,v.baseline_id,v.status FROM variant v JOIN baseline b ON b.id=v.baseline_id "
                        "WHERE b.canonical_key=? AND v.variant_key=? AND b.status='legal'",
                        (bref, vkey))
                    if vrow is None or vrow[2] != "legal":
                        raise _PlanReject(
                            f"eval create 的 {bref}/{vkey} 未解析到 legal variant")
                    if not self._formal_variant_usable(vrow[0]):
                        raise _PlanReject(
                            f"eval create 的 {bref}/{vkey} 缺正式池发布闭包")
                    vid, bid, eid, eval_key = vrow[0], vrow[1], None, t["eval_key"]
                    existing_eval = d.query_one(
                        "SELECT id FROM evaluation WHERE variant_id=? AND protocol_id=? AND protocol_ver=?",
                        (vid, pid, pver))
                    if existing_eval is not None:
                        raise _PlanReject(
                            f"eval create 的格子 v{vid}/p{pid}@{pver} 已有 e{existing_eval[0]}——应走 append_attempt")
                    checkpoints = d.query(
                        "SELECT id,content_hash FROM checkpoint WHERE variant_id=? ORDER BY id", (vid,))
                    if not checkpoints:
                        raise _PlanReject(
                            f"eval create 的 legal variant v{vid} 无可评 checkpoint")
                    target_set_hash = _canon_hash({
                        "variant_id": vid,
                        "checkpoints": [{"id": row[0], "content_hash": row[1]} for row in checkpoints],
                        "protocol": [pid, pver],
                    })
                resolved_eval[tk] = {
                    "baseline_id": bid, "variant_id": vid, "evaluation_id": eid,
                    "eval_key": eval_key, "target_set_hash": target_set_hash,
                }
        dts = []
        for t in targets:
            claim = t.get("claim", {})
            tk, kind = t["target_key"], t["target_kind"]
            if kind == "build":
                id_anchor = claim.get("canonical_key")
                eval_key = tk
                target_set_hash = _canon_hash({
                    "factory_of": {"cycle": ci, "target_key": tk, "id_anchor": id_anchor}})
            elif kind == "exec":
                id_anchor = f"{claim.get('baseline_ref')}/{claim.get('variant_key')}"
                eval_key = tk
                target_set_hash = _canon_hash({
                    "factory_of": {"cycle": ci, "target_key": tk, "id_anchor": id_anchor}})
            else:
                resolved = resolved_eval[tk]
                id_anchor = f"v{resolved['variant_id']}"
                eval_key = resolved["eval_key"]
                target_set_hash = resolved["target_set_hash"]
            slice_ = dict(t)                    # 冻结 target 原样 + 绑定四件（plan_ref 权威；bundle manifest 交叉核锚）
            slice_.update({"protocol_id": pid, "protocol_ver": pver, "eval_key": eval_key,
                           "target_set_hash": target_set_hash})
            if kind == "eval":
                slice_.update({
                    "resolved_baseline_id": resolved_eval[tk]["baseline_id"],
                    "resolved_variant_id": resolved_eval[tk]["variant_id"],
                    "resolved_evaluation_id": resolved_eval[tk]["evaluation_id"],
                })
            dts.append({"target_key": tk, "kind": kind, "seq": t["seq"], "slice": slice_,
                        "required": req_by_tk.get(tk, []), "identity_md": _synth_identity_md(t),
                        "claim": claim, **(resolved_eval.get(tk) or {})})
        return {"protocol": {"id": pid, "version": pver, "name": pname, "scope_json": scope_json,
                             "exists": proto_exists, "defs": defs,
                             "metrics": [(dd["id"], dd["version"]) for dd in defs]},
                "targets": dts}

    def _is_own_exec_reoccupy(self, variant_id: int, ci: int, claim_config: Dict[str, Any]) -> bool:
        """variant 是否 == 本轮自己的**未终局** exec 占坑（崩溃重放待复用）：挂一个本 cycle 的 exec
        build_target 且 status='pending'、plan_ref IS NULL（终局未落），且 variant.config_json 与 plan claim
        一致（身份不漂移）。满足才允许复用（否则视作他处占/漂移，拒）——codex 第2轮 BLOCKER 严核。"""
        d = self.state.daemon
        if d.query_one("SELECT 1 FROM build_target WHERE variant_id=? AND cycle_id=? AND target_kind='exec' "
                       "AND status='pending' AND plan_ref IS NULL", (variant_id, ci)) is None:
            return False
        cfg = d.query_one("SELECT config_json FROM variant WHERE id=?", (variant_id,))[0]
        return json.loads(cfg) == claim_config

    def _register_protocol(self, cycle_id: str, derived: Dict[str, Any]) -> None:
        """gate_new_protocol（I1）——**幂等**：protocol (id,ver) 已存在（scope 已在 derive 核一致）则跳过，
        护跨轮复用 + 崩溃重放（gate 对已存在 (id,ver) 会 GateReject）。"""
        p = derived["protocol"]
        if p["exists"]:
            return
        self.gate.gate_new_protocol(protocol_id=p["id"], version=p["version"], name=p["name"],
                                    scope_spec_json=p["scope_json"], cycle_id=cycle_id,
                                    metric_defs=p["defs"], metrics=p["metrics"])

    def _claim_targets(self, ci: int, cycle_id: str, derived: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        """逐目标占坑（I5）——build 走 gate_claim_baseline（占 canonical_key）；**exec 走 gate_claim_variant**
        （既有 legal baseline 下占 variant_key，**gate 自建 exec build_target**）。**本 cycle 已占则复用**
        （崩溃重放：canonical_key/variant_key 被自己占，gate 会拒 → 复用既有 ids）。冲突已在 _derive_plan
        前置拦下，故正常不半途失败；万一 gate 仍拒，**回滚本次已占的孤儿**（DELETE planned 无引用者，释放键）。
        返回 {tk: {kind, baseline_id, variant_id, build_target_id[exec 有、build 为 None，终局 INSERT]}}。"""
        d = self.state.daemon
        qi = int(self.state.cycle(cycle_id).question_id[1:])   # exec 目标绑本轮活跃问题（gate_claim_variant 入参）
        claims: Dict[str, Dict[str, Any]] = {}
        fresh_bl: List[int] = []                             # 本调用新占的 baseline_id（build 回滚用）
        fresh_bt: List[int] = []                             # 本调用新建的 exec build_target（回滚用，连带 variant）
        try:
            for dt in derived["targets"]:
                tk, claim = dt["target_key"], dt["claim"]
                if dt["kind"] == "build":
                    ck, slug = claim["canonical_key"], claim["slug"]
                    row = d.query_one("SELECT id, born_cycle, slug FROM baseline WHERE canonical_key=?", (ck,))
                    if row and row[1] == ci and row[2] == slug:   # 本 cycle 已占（重放）→ 复用 baseline+base variant
                        vrow = d.query_one("SELECT id FROM variant WHERE baseline_id=? AND variant_key='base'", (row[0],))
                        claims[tk] = {"kind": "build", "baseline_id": row[0], "variant_id": vrow[0], "build_target_id": None}
                    else:
                        r = self.gate.gate_claim_baseline(canonical_key=ck, slug=slug, cycle_id=cycle_id,
                                                          identity_draft_md=dt["identity_md"])
                        fresh_bl.append(r["baseline_id"])
                        claims[tk] = {"kind": "build", "baseline_id": r["baseline_id"],
                                      "variant_id": r["variant_id"], "build_target_id": None}
                elif dt["kind"] == "exec":   # gate_claim_variant 自建 exec build_target
                    bid = d.query_one("SELECT id FROM baseline WHERE canonical_key=?", (claim["baseline_ref"],))[0]
                    vkey = claim["variant_key"]
                    # 本 cycle **未终局**自占（重放）：pending+plan_ref NULL 的 exec bt → 复用。claim 侧
                    # **独立重核 config**（codex 第2轮硬化建议：不只依赖 derive 已核，复用分支自防身份漂移）。
                    reuse = d.query_one(
                        "SELECT v.id, bt.id FROM variant v JOIN build_target bt ON bt.variant_id=v.id "
                        "WHERE v.baseline_id=? AND v.variant_key=? AND bt.cycle_id=? AND bt.target_kind='exec' "
                        "AND bt.status='pending' AND bt.plan_ref IS NULL", (bid, vkey, ci))
                    if reuse and not self._is_own_exec_reoccupy(reuse[0], ci, claim["config_json"]):
                        raise _PlanReject(f"exec variant_key {vkey!r} 自占身份漂移（config 不符）——拒复用")
                    if reuse:
                        claims[tk] = {"kind": "exec", "baseline_id": bid, "variant_id": reuse[0], "build_target_id": reuse[1]}
                    else:
                        r = self.gate.gate_claim_variant(
                            baseline_id=bid, variant_key=vkey, config_json=json.dumps(claim["config_json"], sort_keys=True),
                            cycle_id=cycle_id, seq=dt["seq"], question_id=qi)
                        fresh_bt.append(r["build_target_id"])
                        claims[tk] = {"kind": "exec", "baseline_id": bid, "variant_id": r["variant_id"],
                                      "build_target_id": r["build_target_id"]}
                else:   # eval：只引用既有 legal variant/evaluation，不占池身份。
                    claims[tk] = {
                        "kind": "eval", "baseline_id": dt["baseline_id"],
                        "variant_id": dt["variant_id"], "evaluation_id": dt["evaluation_id"],
                        "build_target_id": None,
                    }
        except GateReject:
            if fresh_bl or fresh_bt:                         # 回滚孤儿（DELETE planned 无引用者，释放键）
                with d.transaction() as conn:
                    for bid in fresh_bl:
                        conn.execute("DELETE FROM variant WHERE baseline_id=? AND status='planned'", (bid,))
                        conn.execute("DELETE FROM baseline WHERE id=? AND status='planned'", (bid,))
                    for bt in fresh_bt:
                        vrow = conn.execute("SELECT variant_id FROM build_target WHERE id=?", (bt,)).fetchone()
                        conn.execute("DELETE FROM build_target WHERE id=? AND status='pending'", (bt,))
                        if vrow:
                            conn.execute("DELETE FROM variant WHERE id=? AND status='planned'", (vrow[0],))
            raise
        return claims

    def _validate_reuse_only_plan(self, cyc, plan: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Mechanically prove every zero-target reuse reference against current DB truth.

        A model saying "reuse" is only a proposal.  This selector accepts exact canonical
        measurements or direct child answers; free-form ``ref_md`` never grants authority.
        """
        items = plan.get("reuse_evidence")
        if not isinstance(items, list) or not items:
            raise _PlanReject("targets=[] 的 reuse_only 必须至少引用一条结构化历史证据")
        needs = plan.get("needs", [])
        need_ids = [item.get("need_id") for item in needs]
        if len(set(need_ids)) != len(need_ids):
            raise _PlanReject(f"reuse_only needs.need_id 重复: {need_ids}")
        known_needs = set(need_ids)
        covered = set()
        resolved: List[Dict[str, Any]] = []
        goal_id, goal_ver = self.state.current_goal_ref()
        question_id = _qnum(cyc.question_id)
        seen_refs = set()
        evaluation_groups: Dict[int, Dict[str, Any]] = {}

        for index, item in enumerate(items, 1):
            kind = item.get("kind")
            need_id = item.get("need_id")
            if known_needs:
                if need_id not in known_needs:
                    raise _PlanReject(
                        f"reuse_evidence[{index}].need_id {need_id!r} 不在 needs")
                covered.add(need_id)
            elif need_id is not None:
                raise _PlanReject(
                    f"聚合轮 needs=[] 时 reuse_evidence[{index}] 不得伪造 need_id")

            if kind == "evaluation":
                if not known_needs:
                    raise _PlanReject(
                        "聚合轮 needs=[] 只能复用直接子问题 answer，不得用无 need 测量替代")
                try:
                    evaluation_id = _decode_id(item.get("evaluation_id"), "e")
                    metric_result_id = _decode_id(item.get("metric_result_id"), "mr")
                except ValueError as error:
                    raise _PlanReject(
                        f"reuse_evidence[{index}] evaluation/mr id 非法: {error}") from error
                row = self.state.daemon.query_one(
                    "SELECT e.status,e.canonical_attempt_id,mr.evaluation_attempt_id,mr.scope,"
                    "ea.status,COALESCE(ea.build_target_id,e.build_target_id),b.status,v.status,"
                    "mr.metric_id,mr.metric_ver,mr.value,b.id,e.variant_id,e.protocol_id,"
                    "e.protocol_ver,ea.env_hash "
                    "FROM metric_result mr JOIN evaluation e ON e.id=mr.evaluation_id "
                    "JOIN evaluation_attempt ea ON ea.id=mr.evaluation_attempt_id "
                    "JOIN variant v ON v.id=e.variant_id JOIN baseline b ON b.id=v.baseline_id "
                    "WHERE mr.id=? AND e.id=?",
                    (metric_result_id, evaluation_id))
                if row is None:
                    raise _PlanReject(
                        f"reuse_evidence[{index}] 的 e{evaluation_id}/mr{metric_result_id} 不存在或不匹配")
                (evaluation_status, canonical_attempt, attempt_id, scope, attempt_status,
                 build_target_id, baseline_status, variant_status, metric_id,
                 metric_ver, value, baseline_id, variant_id, protocol_id,
                 protocol_ver, attempt_env_hash) = row
                if (evaluation_status != "success" or attempt_status != "success"
                        or canonical_attempt != attempt_id or scope != "aggregate"
                        or baseline_status != "legal" or variant_status != "legal"):
                    raise _PlanReject(
                        f"reuse_evidence[{index}] 非 canonical success aggregate 或池身份非 legal")
                if not self._formal_variant_usable(variant_id):
                    raise _PlanReject(
                        f"reuse_evidence[{index}] 所属 variant 缺正式池发布闭包")
                if build_target_id is not None:
                    target = self.state.daemon.query_one(
                        "SELECT status FROM build_target WHERE id=?", (build_target_id,))
                    if target is None or target[0] != "complete":
                        raise _PlanReject(
                            f"reuse_evidence[{index}] 未过 target_complete")
                if OP.suspect_for_attempt(
                        self.state.daemon.conn, int(attempt_id), self.obs_policy):
                    raise _PlanReject(
                        f"reuse_evidence[{index}] attempt ea{attempt_id} 被 parser_result_suspect 标存疑")
                ref = ("evaluation", evaluation_id, metric_result_id)
                if ref in seen_refs:
                    raise _PlanReject(f"reuse_evidence 重复引用 mr{metric_result_id}")
                seen_refs.add(ref)
                gpu_required = item.get("gpu_required")
                if not isinstance(gpu_required, bool):
                    raise _PlanReject(
                        f"reuse_evidence[{index}] 须显式声明 gpu_required，供 selector 派生 expected env")
                group = evaluation_groups.setdefault(evaluation_id, {
                    "baseline_id": baseline_id,
                    "variant_id": variant_id,
                    "protocol_id": protocol_id,
                    "protocol_ver": protocol_ver,
                    "gpu_required": gpu_required,
                    "required": [],
                    "proposed": {},
                    "attempt_env_hash": attempt_env_hash,
                })
                identity = (baseline_id, variant_id, protocol_id, protocol_ver, gpu_required)
                existing_identity = (
                    group["baseline_id"], group["variant_id"], group["protocol_id"],
                    group["protocol_ver"], group["gpu_required"])
                if identity != existing_identity:
                    raise _PlanReject(
                        f"reuse_evidence 对 e{evaluation_id} 的对象/protocol/GPU 声明不一致")
                pair = (metric_id, metric_ver)
                group["required"].append(pair)
                group["proposed"][pair] = metric_result_id
                resolved.append({
                    "kind": "evaluation", "need_id": need_id,
                    "evaluation_id": f"e{evaluation_id}",
                    "metric_result_id": f"mr{metric_result_id}",
                    "metric_id": f"m{metric_id}", "metric_ver": metric_ver,
                    "scope": scope, "value": value,
                })
            elif kind == "child_answer":
                try:
                    answer_id = _decode_id(item.get("answer_id"), "a")
                except ValueError as error:
                    raise _PlanReject(
                        f"reuse_evidence[{index}] answer_id 非法: {error}") from error
                row = self.state.daemon.query_one(
                    "SELECT a.question_id,a.goal_id,a.goal_ver,a.verdict,q.parent_id,q.status,"
                    "aa.status FROM answer a JOIN question q ON q.id=a.question_id "
                    "LEFT JOIN answer_applicability aa ON aa.answer_id=a.id "
                    "AND aa.goal_id=? AND aa.goal_ver=? WHERE a.id=?",
                    (goal_id, goal_ver, answer_id))
                if row is None:
                    raise _PlanReject(
                        f"reuse_evidence[{index}] answer a{answer_id} 不存在")
                child_qid, answer_goal, answer_ver, verdict, parent_id, qstatus, applicability = row
                if (parent_id != question_id or qstatus != verdict
                        or (answer_goal, answer_ver) != (goal_id, goal_ver)
                        or (applicability is not None and applicability != "still_applicable")):
                    raise _PlanReject(
                        f"reuse_evidence[{index}] a{answer_id} 非本题直接子答案/current goal 不可用")
                ref = ("child_answer", answer_id)
                if ref in seen_refs:
                    raise _PlanReject(f"reuse_evidence 重复引用 a{answer_id}")
                seen_refs.add(ref)
                resolved.append({
                    "kind": "child_answer", "need_id": need_id,
                    "answer_id": f"a{answer_id}", "child_question_id": f"q{child_qid}",
                    "verdict": verdict,
                })
            else:
                raise _PlanReject(f"reuse_evidence[{index}].kind 非法: {kind!r}")

        missing = sorted(known_needs - covered)
        if missing:
            raise _PlanReject(f"reuse_only 存在未覆盖 verification needs: {missing}")

        # The catalogue shown to the model is candidate-only.  Recompute the
        # canonical selector here using the exact expected workload identity;
        # neither ref_md nor a canonical-looking mr id can bypass env/coverage/
        # parser-suspect/target-complete checks.
        selector_conn = self.reuse_conn or self.state.daemon.conn
        if self.reuse_conn is None:
            OP.register_parser_suspect_real(
                selector_conn, self.state.daemon.conn, self.obs_policy)
        for evaluation_id, group in sorted(evaluation_groups.items()):
            runtime_hash = self._baseline_environment_hash(group["baseline_id"])
            expected_env_hash = sandbox_workload_environment_hash(
                runtime_hash, group["gpu_required"])
            selected = reuse_selector(
                selector_conn,
                variant_id=group["variant_id"],
                protocol_id=group["protocol_id"],
                protocol_ver=group["protocol_ver"],
                env_hash=expected_env_hash,
                required=sorted(set(group["required"])),
            )
            if not selected["hit"]:
                raise _PlanReject(
                    f"reuse selector miss: e{evaluation_id} 未同时满足 required metrics、"
                    "exact env、非存疑与 target complete")
            canonical = {
                (entry["metric_id"], entry["metric_ver"]): entry["metric_result_id"]
                for entry in selected["results"]
            }
            if any(canonical.get(pair) != mrid
                   for pair, mrid in group["proposed"].items()):
                raise _PlanReject(
                    f"reuse selector 为 e{evaluation_id} 选出的 canonical/latest success measurement "
                    "与 plan 引用不一致")
        return resolved

    def _commit_plan_terminal(self, cyc, plan: Optional[Dict[str, Any]], *, built: List[tuple],
                              reject: Optional[str],
                              validated_reuse: Optional[List[Dict[str, Any]]] = None) -> None:
        """plan 阶段终态单事务：build_target[]（plan_ref=切片）+ required_metric + phase_commit + status='plan'。
        reject 非 None → 零 target + 记 decision(plan_rejected)（业务拒，不楔死）。phase_commit 幂等/conflict 兜底。
        plan=None（信封获取即失败的业务拒）→ 用固定哨兵 hash（该轮无有效 plan 产物可锚）。"""
        ci = _cnum(cyc.cycle_id)
        qi = int(cyc.question_id[1:])
        ah = _canon_hash(plan) if plan is not None else _canon_hash({"plan_artifact_failed": True})
        with self.state.daemon.transaction() as conn:
            pc = check_or_record(conn, cycle_id=cyc.cycle_id, stage="plan", target_id=None, artifact_hash=ah)
            if pc == "conflict":
                raise ValueError("plan 阶段 phase_commit 冲突：同键异 artifact_hash（plan.json 被改写？）")
            if pc == "duplicate":
                return
            if reject is not None:
                # payload 带 question_id（步⑧ CP8.4）：轮末 active_question_id 会随问题释放置 NULL，
                # compiler 的拒因回流（下一轮 plan pack「上轮被拒原因」）须按此锚定位本问题的拒记录
                conn.execute("INSERT INTO decision(cycle_id,actor,type,payload_json) VALUES (?,'orchestrator','plan_rejected',?)",
                             (ci, json.dumps({"reason": reject, "question_id": qi}, ensure_ascii=False)))
            elif validated_reuse is not None:
                conn.execute(
                    "INSERT INTO decision(cycle_id,question_id,actor,type,payload_json) "
                    "VALUES (?,?,'orchestrator','plan_reuse_validated',?)",
                    (ci, qi, json.dumps({
                        "protocol": "plan-reuse-validation-v1",
                        "plan_hash": ah,
                        "evidence": validated_reuse,
                    }, ensure_ascii=False, sort_keys=True)))
            for dt, claim_info in built:
                slice_json = json.dumps(dt["slice"], ensure_ascii=False, sort_keys=True)
                if dt["kind"] == "build":                       # build：终局 INSERT build_target
                    bt = conn.execute("INSERT INTO build_target(cycle_id,question_id,target_kind,seq,status,"
                                      "critical,budget_estimate,baseline_id,variant_id,eval_key,plan_ref) "
                                      "VALUES (?,?,'build',?,'pending',?,?,?,?,?,?)",
                                      (ci, qi, dt["seq"], int(dt["slice"]["critical"]),
                                       float(dt["slice"]["budget_estimate"]), claim_info["baseline_id"],
                                       claim_info["variant_id"], dt["slice"]["eval_key"], slice_json)).lastrowid
                elif dt["kind"] == "exec":                    # gate_claim_variant 已建 bt → 补 plan_ref/eval_key
                    bt = claim_info["build_target_id"]
                    conn.execute("UPDATE build_target SET critical=?,budget_estimate=?,eval_key=?,plan_ref=? WHERE id=?",
                                 (int(dt["slice"]["critical"]), float(dt["slice"]["budget_estimate"]),
                                  dt["slice"]["eval_key"], slice_json, bt))
                else:                                          # eval：引用既有 legal variant/evaluation
                    bt = conn.execute(
                        "INSERT INTO build_target(cycle_id,question_id,target_kind,seq,status,critical,"
                        "budget_estimate,baseline_id,variant_id,evaluation_id,eval_action,attempt_purpose,"
                        "evaluation_source,eval_key,plan_ref) "
                        "VALUES (?,?,'eval',?,'pending',?,?,?,?,?,?,?,?,?,?)",
                        (ci, qi, dt["seq"], int(dt["slice"]["critical"]),
                         float(dt["slice"]["budget_estimate"]), claim_info["baseline_id"],
                         claim_info["variant_id"], claim_info["evaluation_id"],
                         dt["slice"]["eval_action"], dt["slice"]["attempt_purpose"],
                         dt["slice"].get("evaluation_source"), dt["slice"]["eval_key"],
                         slice_json)).lastrowid
                for (mid, mver) in dt["required"]:
                    conn.execute("INSERT INTO build_target_required_metric(build_target_id,metric_id,metric_ver) "
                                 "VALUES (?,?,?)", (bt, mid, mver))
            kinds = [dt["kind"] for dt, _claim in built]
            route = ("attack" if reject is not None or any(k in ("build", "exec") for k in kinds)
                     else "eval_only" if kinds else "reuse_only")
            conn.execute("UPDATE cycle SET status='plan',route=? WHERE id=?", (route, ci))

    # ---------------------------------------------------------------- bundle --
    def _latest_open_repair(self, bt_id: int) -> Optional[Dict[str, Any]]:
        row = self.state.daemon.query_one(
            "SELECT d.id,d.payload_json FROM decision d WHERE d.actor='orchestrator' "
            "AND d.type='bundle_repair_requested' AND json_valid(d.payload_json) "
            "AND json_extract(d.payload_json,'$.build_target_id')=? "
            "AND NOT EXISTS (SELECT 1 FROM decision v WHERE v.actor='orchestrator' "
            "AND v.type='bundle_repair_validated' AND json_valid(v.payload_json) "
            "AND json_extract(v.payload_json,'$.request_decision_id')=d.id) "
            "ORDER BY d.id DESC LIMIT 1", (bt_id,))
        if row is None:
            return None
        try:
            payload = json.loads(row[1])
        except (TypeError, json.JSONDecodeError) as error:
            raise RuntimeError(f"bundle repair decision d{row[0]} 损坏") from error
        if (not isinstance(payload, dict)
                or payload.get("protocol") != "bundle-self-heal-v1"
                or payload.get("build_target_id") != bt_id
                or not isinstance(payload.get("round_no"), int)):
            raise RuntimeError(f"bundle repair decision d{row[0]} 契约损坏")
        return {"decision_id": row[0], **payload}

    def _bundle_execution_attempt(self, bt_id: int) -> int:
        """Return the durable smoke implementation generation (initial=1).

        A repair request supersedes the rejected implementation as soon as its
        SQL intent commits, including the crash window before archive/ready.
        Keeping this ordinal in every guardian context lets recovery distinguish
        multiple smoke executions that intentionally share one build_target.
        """
        rows = self.state.daemon.query(
            "SELECT id,payload_json FROM decision WHERE actor='orchestrator' "
            "AND type='bundle_repair_requested' AND json_valid(payload_json) "
            "AND json_extract(payload_json,'$.build_target_id')=? ORDER BY id",
            (bt_id,))
        next_attempt = 1
        for decision_id, raw in rows:
            try:
                payload = json.loads(raw)
            except (TypeError, json.JSONDecodeError) as error:
                raise RuntimeError(
                    f"bundle repair decision d{decision_id} 损坏") from error
            if (not isinstance(payload, dict)
                    or payload.get("protocol") != "bundle-self-heal-v1"
                    or payload.get("build_target_id") != bt_id
                    or payload.get("round_no") != next_attempt):
                raise RuntimeError(
                    f"bundle repair decision d{decision_id} attempt 链损坏")
            next_attempt += 1
        return next_attempt

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            if not stat.S_ISDIR(os.fstat(fd).st_mode):
                raise RuntimeError(f"bundle repair 路径不是目录: {path}")
            os.fsync(fd)
        finally:
            os.close(fd)

    def _prepare_bundle_repair(self, cyc, bt_id: int,
                               request: Dict[str, Any]) -> None:
        """Idempotently archive the rejected implementation before regeneration."""
        ready = self.state.daemon.query_one(
            "SELECT 1 FROM decision WHERE actor='orchestrator' "
            "AND type='bundle_repair_ready' AND json_valid(payload_json) "
            "AND json_extract(payload_json,'$.request_decision_id')=? LIMIT 1",
            (request["decision_id"],))
        if ready is not None:
            return
        staging = self.work / f"c{_cnum(cyc.cycle_id)}" / f"t{bt_id}"
        archive = staging / "repairs" / f"r{request['round_no']}"
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.mkdir(exist_ok=True)
        for name in ("src", "smoke"):
            source = staging / name
            destination = archive / name
            if os.path.lexists(source):
                info = os.lstat(source)
                if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    raise RuntimeError(
                        f"bundle repair 拒绝归档非常规 {name}: {source}")
                if os.path.lexists(destination):
                    raise RuntimeError(
                        f"bundle repair 同时存在 current/archive {name}: {source} / {destination}")
                if name == "smoke" and self.execution_sandbox is not None:
                    # The sandbox's global session index is a live recovery
                    # authority with absolute paths.  Retire only sessions
                    # already proven terminal+drained before moving their
                    # metadata into the rejected implementation archive.
                    self.execution_sandbox.retire_terminal_sessions_for_archive(
                        staging_dir=source,
                        execution_supervisor=self.execution_supervisor)
                os.replace(source, destination)
                self._fsync_dir(archive)
                self._fsync_dir(staging)
        with self.state.daemon.transaction() as conn:
            exists = conn.execute(
                "SELECT 1 FROM decision WHERE actor='orchestrator' "
                "AND type='bundle_repair_ready' AND json_valid(payload_json) "
                "AND json_extract(payload_json,'$.request_decision_id')=? LIMIT 1",
                (request["decision_id"],)).fetchone()
            if exists is None:
                conn.execute(
                    "INSERT INTO decision(cycle_id,actor,type,payload_json) "
                    "VALUES (?,'orchestrator','bundle_repair_ready',?)",
                    (_cnum(cyc.cycle_id), json.dumps({
                        "protocol": "bundle-self-heal-ready-v1",
                        "build_target_id": bt_id,
                        "request_decision_id": request["decision_id"],
                        "round_no": request["round_no"],
                        "archive_ref": str(archive),
                    }, ensure_ascii=False, sort_keys=True)))

    def _schedule_bundle_repair(self, cyc, bt_id: int,
                                error: _BundleRepairNeeded) -> bool:
        """Request another plan-preserving Bundle engineering turn.

        ``flow.retry.bundle_repair=null`` means engineering repair has no
        count/budget/watchdog ceiling.  Costs and elapsed time remain observable
        facts, but they cannot turn an implementation problem into a research
        result.  The loop exits only on successful execution, explicit owner
        interruption, or a Codex ``replan`` decision that identifies a frozen
        plan problem and therefore continues through Reasoning.

        Integer values retain the legacy bounded mode for explicit profiles.
        """
        d = self.state.daemon
        if error.failure_kind not in _BUNDLE_FAILURE_KINDS:
            raise RuntimeError(
                f"bundle repair failure_kind 非法: {error.failure_kind!r}")
        max_repairs = self.policy["flow"]["retry"]["bundle_repair"]
        unlimited = max_repairs is None
        if (not unlimited and (isinstance(max_repairs, bool)
                or not isinstance(max_repairs, int)
                or not 0 <= max_repairs <= 3)):
            raise RuntimeError(
                "policy.flow.retry.bundle_repair 必须为 null 或 0..3")
        row = d.query_one(
            "SELECT cycle_id,status,budget_estimate,created_at FROM build_target WHERE id=?",
            (bt_id,))
        if row is None:
            raise RuntimeError(f"bundle repair target {bt_id} 不存在")
        if row[1] in _TERMINAL_TARGET:
            return False
        budget = float(row[2] or 0.0)
        spent = float(d.query_one(
            "SELECT COALESCE(SUM(l.money),0) FROM ledger l "
            "JOIN runner_call rc ON rc.id=l.runner_call_id WHERE rc.cycle_id=? AND ("
            "rc.purpose LIKE ? OR rc.id IN (SELECT json_extract(payload_json,'$.runner_call_id') "
            "FROM decision WHERE actor='judge' AND type IN "
            "('bundle_code_review','bundle_result_review') AND json_valid(payload_json) "
            "AND json_extract(payload_json,'$.build_target_id')=?))",
            (row[0], f"bundle-c{row[0]}-t{bt_id}%", bt_id))[0])
        elapsed = float(d.query_one(
            "SELECT MAX(0,(julianday('now')-julianday(created_at))*86400.0) "
            "FROM build_target WHERE id=?", (bt_id,))[0] or 0.0)
        watchdog = float(self.policy["flow"]["watchdog"]["full_h"]) * 3600.0
        deployment_mode = self.policy.get("deployment", {}).get("mode")
        if deployment_mode not in {"development", "production"}:
            raise RuntimeError("policy.deployment.mode 非法")
        extension = False
        development_override = False
        blocked_by = None
        with d.transaction() as conn:
            repair_count = int(conn.execute(
                "SELECT COUNT(*) FROM decision WHERE actor='orchestrator' "
                "AND type='bundle_repair_requested' AND json_valid(payload_json) "
                "AND json_extract(payload_json,'$.build_target_id')=?",
                (bt_id,)).fetchone()[0])
            if unlimited:
                # Deliberately do not create one synthetic "budget extension"
                # per round.  The normal ledger/runner_call rows already expose
                # exact spend; repair authorization stays a single compact row.
                allowed = True
                extension = spent >= budget
                development_override = extension
            elif elapsed >= watchdog:
                allowed = False
                blocked_by = "watchdog"
            elif repair_count >= max_repairs:
                allowed = False
                blocked_by = "repair_limit"
            elif spent < budget:
                allowed = True
            else:
                prior_extension = conn.execute(
                    "SELECT 1 FROM decision WHERE actor='orchestrator' "
                    "AND type='bundle_budget_extension' AND json_valid(payload_json) "
                    "AND json_extract(payload_json,'$.build_target_id')=? LIMIT 1",
                    (bt_id,)).fetchone()
                if prior_extension is None:
                    allowed = True
                    extension = True
                    conn.execute(
                        "INSERT INTO decision(cycle_id,actor,type,payload_json) "
                        "VALUES (?,'orchestrator','bundle_budget_extension',?)",
                        (row[0], json.dumps({
                            "protocol": "bundle-one-fresh-session-extension-v1",
                            "build_target_id": bt_id, "spent": spent,
                            "budget_estimate": budget,
                        }, ensure_ascii=False, sort_keys=True)))
                elif deployment_mode == "development":
                    allowed = True
                    extension = True
                    development_override = True
                    conn.execute(
                        "INSERT INTO decision(cycle_id,actor,type,payload_json) "
                        "VALUES (?,'orchestrator','bundle_development_budget_override',?)",
                        (row[0], json.dumps({
                            "protocol": "bundle-development-budget-override-v1",
                            "build_target_id": bt_id,
                            "repair_round": repair_count + 1,
                            "spent": spent,
                            "budget_estimate": budget,
                            "repair_limit": max_repairs,
                        }, ensure_ascii=False, sort_keys=True)))
                else:
                    allowed = False
                    blocked_by = "budget"
            if allowed:
                round_no = repair_count + 1
                source_hashes = MF.staged_hashes(
                    self.work / f"c{_cnum(cyc.cycle_id)}" / f"t{bt_id}" / "src")
                payload = {
                    "protocol": "bundle-self-heal-v1",
                    "build_target_id": bt_id,
                    "round_no": round_no,
                    "phase": error.phase,
                    "failure_kind": error.failure_kind,
                    "feedback": str(error)[:8192],
                    "repair_of": error.repair_of,
                    "rejected_source_hash": (
                        None if source_hashes is None else _canon_hash(source_hashes)),
                    "spent": spent,
                    "budget_estimate": budget,
                    "repair_limit": max_repairs,
                    "fresh_session_extension": extension,
                    "development_budget_override": development_override,
                }
                decision_id = conn.execute(
                    "INSERT INTO decision(cycle_id,actor,type,payload_json) "
                    "VALUES (?,'orchestrator','bundle_repair_requested',?)",
                    (row[0], json.dumps(payload, ensure_ascii=False, sort_keys=True))).lastrowid
        if not allowed:
            self.gate.gate_finish_build_target(
                build_target_id=bt_id, status="engineering_blocked",
                # ``repair_limit`` / ``budget`` describe why orchestration stops,
                # not a bundle artifact failure kind.  Preserve the final
                # allowed implementation failure; watchdog has its own allowed
                # terminal category.
                failure_kind=("timeout" if blocked_by == "watchdog"
                              else error.failure_kind))
            self._ensure_target_pc(cyc, bt_id)
            return False
        request = {"decision_id": decision_id, **payload}
        self._prepare_bundle_repair(cyc, bt_id, request)
        return True

    def _validate_open_repair(self, cyc, bt_id: int) -> None:
        request = self._latest_open_repair(bt_id)
        if request is None:
            return
        with self.state.daemon.transaction() as conn:
            conn.execute(
                "INSERT INTO decision(cycle_id,actor,type,payload_json) "
                "VALUES (?,'orchestrator','bundle_repair_validated',?)",
                (_cnum(cyc.cycle_id), json.dumps({
                    "protocol": "bundle-self-heal-validated-v1",
                    "build_target_id": bt_id,
                    "request_decision_id": request["decision_id"],
                    "round_no": request["round_no"],
                    "repair_of": request.get("repair_of", {}),
                }, ensure_ascii=False, sort_keys=True)))

    def _repair_authorized(self, bt_id: int, owner_kind: str, owner_id: int) -> bool:
        return self.state.daemon.query_one(
            "SELECT 1 FROM decision WHERE actor='orchestrator' "
            "AND type='bundle_repair_validated' AND json_valid(payload_json) "
            "AND json_extract(payload_json,'$.build_target_id')=? "
            "AND json_extract(payload_json,?)=? ORDER BY id DESC LIMIT 1",
            (bt_id, f"$.repair_of.{owner_kind}_id", owner_id)) is not None

    def _bundle_stage(self, cyc) -> bool:
        """bundle：逐目标（seq 序）两段提交；每目标进度从 DB 状态结构性续。全部终态后推 status='bundle'。
        **进场先收敛未终局 exec 孤儿**（codex SHOULD）：exec 的 build_target 由 gate_claim_variant 先建
        （plan_ref=NULL），终局事务补 plan_ref——正常全部已补，且崩溃重放靠自占复用/回滚兜底。万一有 pending
        +plan_ref NULL 的孤儿逃逸到此（否则 _slice 的 json.loads(None) 裸崩、且孤儿永占 variant_key 毒化后续
        轮），**显式清理**（DELETE bt+其 planned variant，释放 variant_key）+ 记 decision——不静默过滤。"""
        ci = _cnum(cyc.cycle_id)
        d = self.state.daemon
        orphans = d.query("SELECT id, variant_id FROM build_target WHERE cycle_id=? AND target_kind='exec' "
                          "AND status='pending' AND plan_ref IS NULL", (ci,))
        if orphans:
            with d.transaction() as conn:
                for (obt, ovar) in orphans:
                    conn.execute("DELETE FROM build_target WHERE id=?", (obt,))
                    conn.execute("DELETE FROM variant WHERE id=? AND status='planned'", (ovar,))
                conn.execute("INSERT INTO decision(cycle_id,actor,type,payload_json) VALUES (?,'orchestrator','orphan_exec_cleanup',?)",
                             (ci, json.dumps({"cleaned": [o[0] for o in orphans],
                                              "reason": "未终局 exec 占坑（plan_ref NULL）进 bundle——清理释放 variant_key"},
                                             ensure_ascii=False)))
        rows = d.query("SELECT id FROM build_target WHERE cycle_id=? AND plan_ref IS NOT NULL ORDER BY seq", (ci,))
        if self._resident_bundle_session_enabled:
            return self._bundle_stage_resident_cycle(cyc, rows)
        for (bt_id,) in rows:
            prior = d.query_one(
                "SELECT id FROM build_target WHERE cycle_id=? AND seq<(SELECT seq FROM build_target WHERE id=?) "
                "AND critical=1 AND status IN ('failed','engineering_blocked') ORDER BY seq LIMIT 1",
                (ci, bt_id))
            if prior is not None:
                self._skip_after_critical_failure(cyc, prior[0])
                break
            self._drive_target(cyc, bt_id)
            status, critical = d.query_one(
                "SELECT status,critical FROM build_target WHERE id=?", (bt_id,))
            if status not in _TERMINAL_TARGET:
                # A repair request deliberately leaves the target in its current
                # in-flight state.  Do not advance cycle.status to bundle and do
                # not start a later seq target; the next advance consumes the
                # newly rendered repair ContextPack.
                return False
            if status == "engineering_blocked" or (
                    critical and status == "failed"):
                self._skip_after_critical_failure(cyc, bt_id)
                break
        with d.transaction() as conn:
            conn.execute("UPDATE cycle SET status='bundle' WHERE id=?", (ci,))
        return True

    def _bundle_stage_resident_cycle(self, cyc, rows: List[tuple]) -> bool:
        """Run the whole cycle Bundle stage in one top-level Codex process.

        Per-target artifacts are consumed only by asynchronous MCP workers.
        The provider's final (last-target) submission is deliberately ignored;
        it can never be reinterpreted as the first target by the outer driver.
        """
        ci = _cnum(cyc.cycle_id)
        d = self.state.daemon
        self._bundle_apply_early_exit(ci)
        live = d.query_one(
            "SELECT id FROM build_target WHERE cycle_id=? AND plan_ref IS NOT NULL "
            "AND status NOT IN ('complete','skipped','failed','engineering_blocked') "
            "ORDER BY seq,id LIMIT 1", (ci,))
        if live is not None:
            first_target = int(live[0])
            pack = self.compiler.render(
                cycle_id=cyc.cycle_id, stage="bundle",
                target_id=str(first_target))
            # Exactly one normal provider call for the entire cycle.  A later
            # call is permitted only when the owner is reconstructing this same
            # interrupted stage and StageProvider resumes its durable id.
            try:
                self.p["bundle"](cyc, pack)
            except BundleReplanRequired as error:
                # The cycle-wide resident main returns this control outcome
                # only after it has concluded that code/environment repair
                # cannot satisfy the frozen protocol.  Settle it at the same
                # mechanical boundary used by a worker-side replan, then keep
                # the normal Bundle -> Reasoning route instead of treating the
                # control signal as an owner crash.
                self._settle_bundle_replan(cyc, first_target, error)

        session = self._bundle_cycle_session(ci)
        with self._bundle_session_lock:
            worker = session.get("worker")
            worker_error = session.get("worker_error")
        if worker is not None and worker.is_alive():
            raise RuntimeError(
                "Bundle 主 turn 在后台 target 执行结束前退出")
        if worker_error is not None:
            raise RuntimeError(
                "Bundle 官方执行管线异常") from worker_error

        self._bundle_apply_early_exit(ci)
        remaining = d.query(
            "SELECT id,status FROM build_target WHERE cycle_id=? "
            "AND plan_ref IS NOT NULL AND status NOT IN "
            "('complete','skipped','failed','engineering_blocked') "
            "ORDER BY seq,id", (ci,))
        if remaining:
            raise RuntimeError(
                "Bundle 主 turn 在 cycle_complete 前退出；"
                f"未终态 targets={remaining}")
        with d.transaction() as conn:
            fresh = conn.execute(
                "SELECT status FROM cycle WHERE id=?", (ci,)).fetchone()
            if fresh is None or fresh[0] != "plan":
                raise RuntimeError("Bundle 收口时 cycle 游标漂移")
            dangling = conn.execute(
                "SELECT id FROM build_target WHERE cycle_id=? AND plan_ref IS NOT NULL "
                "AND status NOT IN ('complete','skipped','failed','engineering_blocked') "
                "LIMIT 1", (ci,)).fetchone()
            if dangling is not None:
                raise RuntimeError(
                    f"Bundle 收口事务发现未终态 target {dangling[0]}")
            conn.execute("UPDATE cycle SET status='bundle' WHERE id=?", (ci,))
        with self._bundle_session_lock:
            self._bundle_cycle_sessions.pop(ci, None)
        return True

    def _skip_after_critical_failure(self, cyc, failed_target_id: int) -> None:
        """收敛 critical/engineering_blocked 早退；可在任意一个 skip 后崩溃并幂等续扫。"""
        skipped = self.gate.gate_skip_remaining_targets(failed_target_id=failed_target_id)
        for target_id in skipped:
            self._ensure_target_pc(cyc, target_id)

    def _settle_bundle_replan(
            self, cyc, bt_id: int, error: BundleReplanRequired) -> None:
        """Idempotently turn a frozen-plan diagnosis into Reasoning evidence."""
        d = self.state.daemon
        status = d.query_one(
            "SELECT status FROM build_target WHERE id=?", (bt_id,))
        if status is None:
            raise RuntimeError(f"Bundle replan target {bt_id} 不存在")
        if status[0] not in _TERMINAL_TARGET:
            self.gate.gate_finish_build_target(
                build_target_id=bt_id, status="engineering_blocked",
                failure_kind="protocol_violation")
        with d.transaction() as conn:
            exists = conn.execute(
                "SELECT 1 FROM decision WHERE cycle_id=? "
                "AND actor='orchestrator' AND type='bundle_replan_required' "
                "AND json_extract(payload_json,'$.build_target_id')=? LIMIT 1",
                (_cnum(cyc.cycle_id), bt_id)).fetchone()
            if exists is None:
                conn.execute(
                    "INSERT INTO decision(cycle_id,actor,type,payload_json) "
                    "VALUES (?,'orchestrator','bundle_replan_required',?)",
                    (_cnum(cyc.cycle_id), json.dumps({
                        "protocol": "bundle-replan-v1",
                        "build_target_id": bt_id,
                        "legacy_request_id": error.request_id,
                        "summary_md": str(
                            error.request.get("summary_md") or "")[:2048],
                    }, ensure_ascii=False, sort_keys=True)))
        self._ensure_target_pc(cyc, bt_id)

    def _slice(self, bt_id: int) -> Dict[str, Any]:
        """resolved plan 切片（plan_ref）：冻结 target 原样 + 编排器派生绑定四件（protocol_id/protocol_ver/
        eval_key/target_set_hash）。bundle manifest 交叉核的锚。"""
        row = self.state.daemon.query_one("SELECT plan_ref FROM build_target WHERE id=?", (bt_id,))
        return json.loads(row[0])

    def _obtain_manifest(self, cyc, bt_id: int, slice_: Dict[str, Any], src_dir: Path) -> tuple:
        """取本目标的 execution_manifest（persist-then-consume）：src 未物化 → 调 bundle provider 产信封
        → 校验 + 交叉核 + 净土物化；已物化 → 复用。fresh / resume 都重新编译当前 bundle ContextPack，
        返回 (manifest, ledger, allowed_asset_refs, asset_identities)，使可预测 opaque ref 只能
        消费当前 goal 在 bundle 生成时已授权且内容身份未变的资产。

        **fresh 与 resume 同校验口径**（codex SHOULD-1）：两路径都 validate_manifest + cross_check(切片)。
        **失败分流**（codex SHOULD-2）：
        - fresh 路径（Codex 刚产的 manifest 非法/信封缺件）→ **_BundleReject**（业务拒→目标 failed，不楔死）；
        - resume 路径（已物化的 manifest 竟校验不过 = 已入库产物损坏，而非 Codex 出错）→ ManifestError 上抛
          fail loud（同 staged_hashes 篡改）——我们只物化校验通过的 manifest，故此路径校验失败 = 数据损毁。"""
        if self.policy is None:
            raise RuntimeError("AttackStages 须注入 policy（manifest 命令围栏 execution 节）")
        # 不只在 fresh provider 调用时拿 pack：resume 也要确认“生成时冻结的 refs”在当前 DB 快照仍可见。
        # 注意当前 refs 是追加集，绝不能直接拿它当恢复授权——否则旧 manifest 可预猜未来 request id，
        # kill 后等该请求 resolved 再扩权。实际能力只来自 staging ledger 内的生成时授权快照。
        pack = self.compiler.render(cycle_id=cyc.cycle_id, stage="bundle", target_id=str(bt_id))
        ledger = MF.staged_hashes(src_dir)        # 篡改自查（损毁→ManifestError fail loud）
        if ledger is None:                        # fresh：Codex 出错归业务拒
            # 旧版本曾把 bundle 的内部环境矛盾误建成用户文件请求。用户/系统从 Web 取消后，
            # 直接消费这条耐久结论进入重规划，不再花一次昂贵 Codex 调用重提同一问题。
            legacy = self.state.daemon.query_one(
                "SELECT id,summary_md,items_json FROM interaction_request "
                "WHERE cycle_id=? AND stage='bundle' AND status='cancelled' "
                "ORDER BY id DESC LIMIT 1", (_cnum(cyc.cycle_id),))
            if legacy is not None:
                try:
                    items = json.loads(legacy[2])
                except (TypeError, json.JSONDecodeError):
                    items = []
                raise BundleReplanRequired(
                    {"summary_md": legacy[1], "items": items}, request_id=legacy[0])
            with self._bundle_session_lock:
                session_files = self._bundle_session_payloads.pop(bt_id, None)
            if session_files is not None:
                files = session_files
            else:
                files = self.p["bundle"](cyc, pack)
                if self._resident_bundle_session_enabled:
                    target_status = self.state.daemon.query_one(
                        "SELECT status FROM build_target WHERE id=?", (bt_id,))[0]
                    if target_status in _TERMINAL_TARGET:
                        raise _BundleSessionCompleted()
                    raise RuntimeError(
                        "resident Bundle 主 turn 在官方 target 终态前结束；"
                        "须在同一 turn 使用 bundle_execute 修复/执行，或用 "
                        "bundle_replan 显式交给 Reasoning")
            try:
                if not isinstance(files, dict) or "execution_manifest.json" not in files:
                    raise MF.ManifestError(f"bundle provider 未产 execution_manifest.json（键: "
                                           f"{list(files) if isinstance(files, dict) else type(files)}）")
                manifest = files["execution_manifest.json"]
                self._check_manifest(manifest, slice_)
                actual_refs = MF.extract_manifest_asset_refs(manifest)
                if self.qualification_firewall is not None and actual_refs:
                    raise MF.ManifestError(
                        "qualification bundle 禁止消费 uploaded/external asset refs")
                unauthorized = sorted(set(actual_refs) - set(pack.refs))
                if unauthorized:
                    raise MF.ManifestError(
                        f"manifest 使用未获生成时 ContextPack 授权的输入资产 ref: {unauthorized}")
                identities = MF.capture_asset_identities(actual_refs, work_root=self.work)
                ledger = MF.stage_bundle_files(
                    files, manifest, src_dir,
                    authorization_pack_hash=pack.pack_hash,
                    allowed_asset_refs=pack.refs,
                    asset_identities=identities)
                authorization = MF.load_asset_authorization(src_dir, manifest)
                MF.verify_asset_authorization(authorization, work_root=self.work)
            except MF.ManifestError as e:
                raise _BundleReject(str(e)) from e
            frozen_refs = authorization.asset_refs if authorization is not None else frozenset()
            frozen_identities = authorization.identities if authorization is not None else {}
            return manifest, ledger, frozen_refs, frozen_identities
        manifest = json.loads(read_artifact_bytes(
            src_dir / MF.MANIFEST_FILE,
            expected_hash=ledger[MF.MANIFEST_FILE],
            label="staged execution manifest").decode("utf-8"))
        self._check_manifest(manifest, slice_)    # resume 再校验（损坏→ManifestError 上抛，不吞）
        authorization = MF.load_asset_authorization(src_dir, manifest)
        frozen_refs = authorization.asset_refs if authorization is not None else frozenset()
        if self.qualification_firewall is not None and frozen_refs:
            raise MF.ManifestError(
                "qualification 已物化 bundle 含外部 asset refs，拒绝恢复")
        missing_now = sorted(set(frozen_refs) - set(pack.refs))
        if missing_now:
            raise MF.ManifestError(
                f"staging 生成时资产授权已不在当前 ContextPack：{missing_now}——DB/回执损坏，拒绝恢复")
        # pack_hash 是「当时生成包」的受 ledger 保护审计锚，不是 resume 等值闸：后续 append-only
        # 回执会合法改变当前 pack hash。恢复时的执法闸是「冻结 ref 仍在当前 pack」+「内容身份未变」。
        MF.verify_asset_authorization(authorization, work_root=self.work)
        frozen_identities = authorization.identities if authorization is not None else {}
        return manifest, ledger, frozen_refs, frozen_identities

    def _check_manifest(self, manifest: Dict[str, Any], slice_: Dict[str, Any]) -> None:
        """manifest 结构 + 交叉核 + kind 支持（fresh/resume 共用）。非 build kind → ManifestError（fresh 侧
        转 _BundleReject 业务拒；CP8.6 接 exec/eval）。"""
        if self.schemas is not None:
            MF.validate_manifest(self.schemas, manifest)
        MF.cross_check(manifest, slice_)          # 防「manifest 自立目标/换协议/改配置」
        if manifest["target_ref"]["target_kind"] not in ("build", "exec", "eval"):
            raise MF.ManifestError(
                f"bundle target kind 不支持：{manifest['target_ref']['target_kind']}")

    @staticmethod
    def _bundle_operator_log_snapshot(path: Path, *, state: str,
                                      exit_code: Optional[int] = None,
                                      content_hash: Optional[str] = None) -> Dict[str, Any]:
        """Read a bounded diagnostic tail from a growing/final execution log.

        Partial bytes are never evidence.  They are copied into the Codex prompt
        as explicitly untrusted data and identified by size+tail hash; terminal
        callers additionally provide the harness' full immutable log hash.
        """
        if state not in {"not_started", "partial", "final"}:
            raise ValueError("bundle operator log state 非法")
        if state == "not_started":
            return {
                "state": state, "size_bytes": 0, "tail_sha256": None,
                "tail_text": "", "content_hash": None, "exit_code": None,
            }
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            if state == "partial":
                return {
                    "state": "partial", "size_bytes": 0, "tail_sha256": None,
                    "tail_text": "", "content_hash": None, "exit_code": None,
                }
            raise
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise RuntimeError("bundle operator 日志不是常规文件")
            take = min(before.st_size, _BUNDLE_OPERATOR_TAIL_BYTES)
            offset = max(0, before.st_size - take)
            chunks: List[bytes] = []
            remaining = take
            while remaining:
                chunk = os.pread(fd, remaining, offset + take - remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            tail = b"".join(chunks)
            after = os.fstat(fd)
            if ((before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                    or after.st_size < before.st_size):
                raise RuntimeError("bundle operator 日志观察期间身份/长度回退")
        finally:
            os.close(fd)
        return {
            "state": state,
            "size_bytes": before.st_size,
            "tail_sha256": "sha256:" + hashlib.sha256(tail).hexdigest(),
            "tail_text": tail.decode("utf-8", errors="replace"),
            "content_hash": content_hash,
            "exit_code": exit_code,
        }

    def _bundle_operator_control(
            self, cyc, bt_id: int, *, phase: str, event: str,
            owner_kind: str, owner_id: int, manifest: Mapping[str, Any],
            ledger: Mapping[str, str], log: Mapping[str, Any]) -> Dict[str, Any]:
        if phase not in {"smoke", "train", "eval"} or event not in {
                "start", "progress", "terminal"}:
            raise ValueError("bundle operator phase/event 非法")
        expected_owner = {
            "smoke": "build_target", "train": "run", "eval": "evaluation_attempt"}
        if expected_owner[phase] != owner_kind:
            raise ValueError("bundle operator phase 与 execution owner 不一致")
        if (isinstance(owner_id, bool) or not isinstance(owner_id, int) or owner_id <= 0):
            raise ValueError("bundle operator execution owner id 非法")
        repair_round = int(self.state.daemon.query_one(
            "SELECT COUNT(*) FROM decision WHERE actor='orchestrator' "
            "AND type='bundle_repair_requested' AND json_valid(payload_json) "
            "AND json_extract(payload_json,'$.build_target_id')=?", (bt_id,))[0])
        log_identity = {key: value for key, value in log.items() if key != "tail_text"}
        identity = {
            "protocol": "bundle-operator-control-v1",
            "cycle_id": cyc.cycle_id,
            "build_target_id": bt_id,
            "phase": phase,
            "event": event,
            "execution_owner": {"kind": owner_kind, "id": owner_id},
            "plan_slice_hash": manifest["target_ref"]["plan_slice_hash"],
            "source_tree_hash": "sha256:" + _canon_hash(dict(ledger)),
            "manifest_hash": "sha256:" + _canon_hash(dict(manifest)),
            "repair_round": repair_round,
            "log": log_identity,
        }
        subject_hash = "sha256:" + _canon_hash(identity)
        return {
            "protocol": "bundle-operator-control-v1",
            "build_target_id": bt_id,
            "phase": phase,
            "event": event,
            "execution_owner": {"kind": owner_kind, "id": owner_id},
            "plan_slice_hash": identity["plan_slice_hash"],
            "source_tree_hash": identity["source_tree_hash"],
            "subject_hash": subject_hash,
            "repair_round": repair_round,
            "log": dict(log),
        }

    def _bundle_operator_decide(
            self, cyc, bt_id: int, *, phase: str, event: str,
            owner_kind: str, owner_id: int, manifest: Mapping[str, Any],
            ledger: Mapping[str, str], log: Mapping[str, Any]) -> Dict[str, Any]:
        """Obtain and durably record one replay-safe Codex operator action."""
        defaults = {"start": "start", "progress": "continue", "terminal": "accept"}
        if "bundle_operator" not in self.p:
            return {"action": defaults[event], "diagnosis_md": "operator disabled"}
        control = self._bundle_operator_control(
            cyc, bt_id, phase=phase, event=event, owner_kind=owner_kind,
            owner_id=owner_id, manifest=manifest, ledger=ledger, log=log)
        subject_hash = control["subject_hash"]
        existing = self.state.daemon.query_one(
            "SELECT payload_json FROM decision WHERE actor='agent' "
            "AND type='bundle_operator_action' AND json_valid(payload_json) "
            "AND json_extract(payload_json,'$.build_target_id')=? "
            "AND json_extract(payload_json,'$.subject_hash')=? ORDER BY id DESC LIMIT 1",
            (bt_id, subject_hash))
        if existing is not None:
            payload = json.loads(existing[0])
            action = payload.get("model_action")
            if not isinstance(action, dict):
                raise RuntimeError("durable bundle operator action 损坏")
            return action

        pack = self.compiler.render(
            cycle_id=cyc.cycle_id, stage="bundle", target_id=str(bt_id))
        files = self.p["bundle_operator"](cyc, pack, control)
        if not isinstance(files, dict) or set(files) != {"bundle_operator_action.json"}:
            raise ValueError("bundle operator provider 须只返回 bundle_operator_action.json")
        action = files["bundle_operator_action.json"]
        if self.schemas is not None:
            errors = sorted(
                self.schemas.validator("bundle_operator_action").iter_errors(action),
                key=lambda error: list(error.absolute_path))
            if errors:
                raise ValueError(
                    "bundle operator action schema 非法: "
                    + "; ".join(error.message for error in errors[:8]))
        expected = {
            "version": 1,
            "build_target_id": bt_id,
            "phase": phase,
            "event": event,
            "execution_owner": {"kind": owner_kind, "id": owner_id},
            "plan_slice_hash": control["plan_slice_hash"],
            "source_tree_hash": control["source_tree_hash"],
            "subject_hash": subject_hash,
        }
        if not isinstance(action, dict) or any(
                action.get(key) != value for key, value in expected.items()):
            raise ValueError("bundle operator action 未精确回引服务端 control")
        allowed = {
            "start": {"start", "repair", "replan"},
            "progress": {"continue", "repair", "replan"},
            "terminal": {"accept", "repair", "replan"},
        }
        if action.get("action") not in allowed[event]:
            raise ValueError("bundle operator action 与 event 不相容")
        if (event == "terminal" and log.get("exit_code") not in (None, 0)
                and action.get("action") not in {"repair", "replan"}):
            raise ValueError("非零 terminal 只能由 bundle operator 请求 repair 或 replan")
        persisted_log = {key: value for key, value in control["log"].items()
                         if key != "tail_text"}
        payload = {
            "protocol": "bundle-operator-decision-v1",
            "build_target_id": bt_id,
            "phase": phase,
            "event": event,
            "execution_owner": control["execution_owner"],
            "plan_slice_hash": control["plan_slice_hash"],
            "source_tree_hash": control["source_tree_hash"],
            "subject_hash": subject_hash,
            "repair_round": control["repair_round"],
            "log": persisted_log,
            "model_action": action,
        }
        with self.state.daemon.transaction() as conn:
            duplicate = conn.execute(
                "SELECT 1 FROM decision WHERE actor='agent' "
                "AND type='bundle_operator_action' AND json_valid(payload_json) "
                "AND json_extract(payload_json,'$.build_target_id')=? "
                "AND json_extract(payload_json,'$.subject_hash')=? LIMIT 1",
                (bt_id, subject_hash)).fetchone()
            if duplicate is None:
                conn.execute(
                    "INSERT INTO decision(cycle_id,actor,type,payload_json) "
                    "VALUES (?,'agent','bundle_operator_action',?)",
                    (_cnum(cyc.cycle_id), json.dumps(
                        payload, ensure_ascii=False, sort_keys=True)))
        return action

    def _run_manifest_as_bundle_operator(
            self, cyc, bt_id: int, *, phase: str, owner_kind: str, owner_id: int,
            manifest: Dict[str, Any], ledger: Mapping[str, str],
            staging_dir: str, log_name: str, **kwargs) -> Dict[str, Any]:
        """Let Codex start and live-observe one exact manifest capability."""
        if "bundle_operator" not in self.p:
            if not self._resident_bundle_session_enabled:
                return MF.run_manifest_command(
                    manifest, phase, staging_dir=staging_dir,
                    log_name=log_name, **kwargs)

            requested: Dict[str, Any] = {
                "diagnosis_md": None, "replan": False}

            def observe_resident() -> bool:
                control = self._resident_bundle_control(bt_id)
                if control is None:
                    return False
                requested.update(control)
                return True

            def raise_requested(error: Optional[BaseException] = None) -> None:
                diagnosis = requested.get("diagnosis_md")
                if not diagnosis:
                    return
                if requested.get("replan"):
                    self._settle_operator_owner_for_replan(owner_kind, owner_id)
                    exc = BundleReplanRequired({
                        "summary_md": diagnosis, "items": [], "phase": phase,
                    })
                else:
                    exc = _BundleOperatorRepair(phase, diagnosis)
                if error is None:
                    raise exc
                raise exc from error

            try:
                result = MF.run_manifest_command(
                    manifest, phase, staging_dir=staging_dir, log_name=log_name,
                    progress_observer=observe_resident,
                    progress_interval_s=self.bundle_operator_poll_s,
                    **kwargs)
            except ExecutionCancelled as error:
                control = self._resident_bundle_control(bt_id)
                if control is not None:
                    requested.update(control)
                raise_requested(error)
                raise
            control = self._resident_bundle_control(bt_id)
            if control is not None:
                requested.update(control)
            # A short command may finish between the MCP cancellation request
            # and the next observer tick.  The live main-agent decision still
            # wins and is converted through the ordinary repair/replan path.
            raise_requested()
            return result
        start_log = self._bundle_operator_log_snapshot(
            Path(staging_dir) / (log_name + ".partial"), state="not_started")
        start = self._bundle_operator_decide(
            cyc, bt_id, phase=phase, event="start", owner_kind=owner_kind,
            owner_id=owner_id, manifest=manifest, ledger=ledger, log=start_log)
        if start["action"] == "repair":
            raise _BundleOperatorRepair(
                phase, start.get("diagnosis_md") or "Codex operator 在启动前请求修复")
        if start["action"] == "replan":
            self._settle_operator_owner_for_replan(owner_kind, owner_id)
            raise BundleReplanRequired({
                "summary_md": (start.get("diagnosis_md")
                               or "Codex operator 判定冻结 plan 无法执行"),
                "items": [], "phase": phase,
            })

        partial_path = Path(staging_dir) / (log_name + ".partial")
        last_decided: Optional[tuple] = None
        next_probe = time.monotonic() + self.bundle_operator_probe_s
        requested: Dict[str, Any] = {"diagnosis": None, "replan": False}

        def observe() -> bool:
            nonlocal last_decided, next_probe
            snapshot = self._bundle_operator_log_snapshot(partial_path, state="partial")
            identity = (snapshot["size_bytes"], snapshot["tail_sha256"])
            now = time.monotonic()
            suspicious = bool(_BUNDLE_OPERATOR_SUSPICIOUS.search(snapshot["tail_text"]))
            if identity == last_decided and now < next_probe:
                return False
            if not suspicious and now < next_probe:
                return False
            action = self._bundle_operator_decide(
                cyc, bt_id, phase=phase, event="progress", owner_kind=owner_kind,
                owner_id=owner_id, manifest=manifest, ledger=ledger, log=snapshot)
            last_decided = identity
            next_probe = now + self.bundle_operator_probe_s
            if action["action"] == "repair":
                requested["diagnosis"] = (
                    action.get("diagnosis_md") or "Codex operator 从运行日志请求修复")
                return True
            if action["action"] == "replan":
                requested["diagnosis"] = (
                    action.get("diagnosis_md") or "Codex operator 从运行日志判定 plan 不可执行")
                requested["replan"] = True
                return True
            return False

        try:
            result = MF.run_manifest_command(
                manifest, phase, staging_dir=staging_dir, log_name=log_name,
                progress_observer=observe,
                progress_interval_s=self.bundle_operator_poll_s,
                **kwargs)
        except ExecutionCancelled as error:
            if requested["diagnosis"] is not None:
                if requested["replan"]:
                    self._settle_operator_owner_for_replan(owner_kind, owner_id)
                    raise BundleReplanRequired({
                        "summary_md": requested["diagnosis"],
                        "items": [], "phase": phase,
                    }) from error
                raise _BundleOperatorRepair(
                    phase, requested["diagnosis"]) from error
            raise
        # A short command can finish in the small race between the observer's
        # repair decision and the guardian consuming its cancel byte.  The
        # model's durable repair request still wins; never silently downgrade
        # it merely because the exact process happened to exit first.
        if requested["diagnosis"] is not None:
            if requested["replan"]:
                self._settle_operator_owner_for_replan(owner_kind, owner_id)
                raise BundleReplanRequired({
                    "summary_md": requested["diagnosis"],
                    "items": [], "phase": phase,
                })
            raise _BundleOperatorRepair(phase, requested["diagnosis"])
        return result

    def _bundle_operator_terminal_reason(
            self, cyc, bt_id: int, *, phase: str, owner_kind: str, owner_id: int,
            manifest: Mapping[str, Any], ledger: Mapping[str, str],
            result: Mapping[str, Any]) -> Optional[str]:
        if "bundle_operator" not in self.p:
            return None
        log = self._bundle_operator_log_snapshot(
            Path(result["log_path"]), state="final",
            exit_code=int(result["exit_code"]),
            content_hash="sha256:" + str(result["log_sha256"]))
        action = self._bundle_operator_decide(
            cyc, bt_id, phase=phase, event="terminal", owner_kind=owner_kind,
            owner_id=owner_id, manifest=manifest, ledger=ledger, log=log)
        if action["action"] == "repair":
            return action.get("diagnosis_md") or "Codex operator 拒绝该执行终态并请求修复"
        if action["action"] == "replan":
            self._settle_operator_owner_for_replan(owner_kind, owner_id)
            raise BundleReplanRequired({
                "summary_md": (action.get("diagnosis_md")
                               or "Codex operator 判定冻结 plan 本身不可执行"),
                "items": [], "phase": phase,
            })
        return None

    def _settle_operator_owner_for_replan(
            self, owner_kind: str, owner_id: int) -> None:
        """Close a cancelled/finished execution owner before Reasoning routing.

        The target itself is finalized by ``_drive_target``.  Train/eval have
        nested durable owners which must not remain ``running`` when the Codex
        operator classifies the failure as a frozen-plan problem.
        """
        d = self.state.daemon
        if owner_kind == "build_target":
            return
        if owner_kind == "run":
            row = d.query_one("SELECT status FROM run WHERE id=?", (owner_id,))
            if row is not None and row[0] not in {"success", "failed", "aborted"}:
                self.gate.gate_finish_run(
                    run_id=owner_id, status="failed", failure_kind="runtime")
            return
        if owner_kind == "evaluation_attempt":
            row = d.query_one(
                "SELECT evaluation_id,status FROM evaluation_attempt WHERE id=?",
                (owner_id,))
            if row is None:
                raise RuntimeError(f"bundle replan attempt {owner_id} 不存在")
            evaluation_id, status = int(row[0]), str(row[1])
            if status not in {"success", "failed", "aborted"}:
                self.gate.gate_finish_attempt(
                    attempt_id=owner_id, status="failed", failure_kind="runtime")
            evaluation_status = d.query_one(
                "SELECT status FROM evaluation WHERE id=?", (evaluation_id,))
            if evaluation_status is not None and evaluation_status[0] != "success":
                self.gate.gate_finish_evaluation(evaluation_id=evaluation_id)
            return
        raise RuntimeError(f"bundle operator owner_kind 非法: {owner_kind!r}")

    def _drive_target(self, cyc, bt_id: int) -> None:
        """单目标推进（可重入）：按当前状态从断点续。执行命令由 manifest 承载（步⑧）。
        **契约违规不楔死、损毁必楔**（codex 第2轮 BLOCKER 收窄）：捕 _BundleReject（Codex 产物层业务拒：
        非法 manifest = artifact_invalid / 测量包违约 = protocol_violation，见 _BundleReject 注）→ 目标
        failed(failure_kind) + 落 pc、续下一目标；另捕已 terminal+drained 且 exact owner 可核的
        SandboxOutputError，将不安全 quarantine 结算为 artifact_invalid。**GateReject 不在此捕**——状态机/
        库损毁类拒绝（start/progress/finish/register_baseline）必须 fail loud，误终态化会把恢复/篡改问题
        永久掩埋。"""
        st = lambda: self.state.daemon.query_one("SELECT status FROM build_target WHERE id=?", (bt_id,))[0]
        try:
            self._drive_target_inner(cyc, bt_id)
        except SandboxOutputError as error:
            settle_sandbox_output_failure(self.gate, self.state.daemon, bt_id, error)
            self._ensure_target_pc(cyc, bt_id)
        except _BundleRepairNeeded as error:
            self._schedule_bundle_repair(cyc, bt_id, error)
        except BundleReplanRequired as error:
            self._settle_bundle_replan(cyc, bt_id, error)
        except _BundleReject as e:
            if e.failure_kind == "env_invalid":
                if st() not in _TERMINAL_TARGET:
                    self.gate.gate_finish_build_target(
                        build_target_id=bt_id, status="engineering_blocked",
                        failure_kind=e.failure_kind)
                self._ensure_target_pc(cyc, bt_id)
            else:
                self._schedule_bundle_repair(
                    cyc, bt_id, _BundleRepairNeeded(
                        str(e), failure_kind=e.failure_kind,
                        phase="artifact"))

    def _drive_target_inner(self, cyc, bt_id: int) -> None:
        g = self.gate
        d = self.state.daemon
        slice_ = self._slice(bt_id)
        st = lambda: d.query_one("SELECT status FROM build_target WHERE id=?", (bt_id,))[0]
        if st() in _TERMINAL_TARGET:
            self._ensure_target_pc(cyc, bt_id)   # 崩在 complete 与 pc 之间 → 补 pc（幂等）
            return
        staging = self.work / f"c{_cnum(cyc.cycle_id)}" / f"t{bt_id}"
        repair = self._latest_open_repair(bt_id)
        if repair is not None:
            self._prepare_bundle_repair(cyc, bt_id, repair)
        src_dir = staging / "src"                 # 代码物化目录（每目标唯一；净土物化，与 run/eval 产物分离）
        try:
            manifest, ledger, allowed_asset_refs, asset_identities = self._obtain_manifest(
                cyc, bt_id, slice_, src_dir)
        except _BundleSessionCompleted:
            self._ensure_target_pc(cyc, bt_id)
            return
        execution_sandbox = self._execution_sandbox_for(manifest, bt_id)
        if st() == "pending":
            g.gate_start_build_target(build_target_id=bt_id)
        if slice_["target_kind"] == "eval":
            if repair is not None:
                self._validate_open_repair(cyc, bt_id)
            if st() == "running":
                self._run_eval_target(
                    cyc, bt_id, slice_, manifest, ledger, staging, src_dir,
                    allowed_asset_refs, asset_identities,
                    execution_sandbox=execution_sandbox)
            self._ensure_target_pc(cyc, bt_id)
            return
        if st() == "building" or repair is not None:
            # Every replacement implementation is smoke-tested before it may
            # reuse/append execution facts, even when the durable target state
            # is already smoke/running from an earlier code version.
            smoke_dir = staging / "smoke"
            existing_final = H.latest_smoke_log(smoke_dir)
            partials = sorted(smoke_dir.glob("smoke-*.log.partial")) if smoke_dir.exists() else []
            if len(partials) > 1:
                raise RuntimeError(f"target {bt_id} 有多个未发布 smoke partial")
            smoke_name = (existing_final.name if existing_final is not None else
                          (partials[0].name[:-len(".partial")] if partials else "smoke-1.log"))
            smoke_context = {
                "cycle_id": cyc.cycle_id, "build_target_id": bt_id,
                "phase": "smoke", "reconcile_protocol": "execution-owner-v1",
                "db_owner_kind": "build_target", "db_owner_id": bt_id,
                "execution_attempt": self._bundle_execution_attempt(bt_id),
            }
            if existing_final is not None:
                exit_file = existing_final.with_name(existing_final.name + ".exit")
                if not exit_file.exists():
                    raise RuntimeError(
                        f"staging 损毁：{existing_final} 在而 exit 侧车缺——须人工核")
                smoke_bytes = read_artifact_bytes(
                    existing_final, label="persisted smoke log")
                sm = {"exit_code": int(read_artifact_bytes(
                          exit_file, max_bytes=32,
                          label="smoke exit sidecar").decode("ascii")),
                      "log_path": str(existing_final),
                      "log_sha256": hashlib.sha256(smoke_bytes).hexdigest(),
                      "log_bytes": len(smoke_bytes)}
            else:
                sm = H.recover_staged_result(
                    staging_dir=str(smoke_dir), log_name=smoke_name,
                    execution_supervisor=self.execution_supervisor,
                    execution_kind="manifest-smoke", execution_context=smoke_context,
                    execution_sandbox=execution_sandbox)
                if sm is None:
                    self.owner_guard()             # external spawn 的最后一道 owner fence
                    try:
                        sm = self._run_manifest_as_bundle_operator(
                            cyc, bt_id, phase="smoke", owner_kind="build_target",
                            owner_id=bt_id, manifest=manifest, ledger=ledger,
                            staging_dir=str(smoke_dir), log_name=smoke_name,
                            src_dir=src_dir, work_root=self.work, policy=self.policy,
                            expected_source_hashes=ledger,
                            allowed_asset_refs=allowed_asset_refs,
                            expected_asset_identities=asset_identities,
                            execution_supervisor=self.execution_supervisor,
                            execution_context=smoke_context,
                            execution_sandbox=execution_sandbox)
                    except _BundleOperatorRepair as error:
                        raise _BundleRepairNeeded(
                            "Codex operator 在 smoke 运行中请求停止并修复：\n"
                            + error.diagnosis,
                            failure_kind="smoke", phase="smoke") from error
            operator_reason = self._bundle_operator_terminal_reason(
                cyc, bt_id, phase="smoke", owner_kind="build_target", owner_id=bt_id,
                manifest=manifest, ledger=ledger, result=sm)
            if sm["exit_code"] != 0 or operator_reason is not None:
                tail = read_artifact_bytes(
                    Path(sm["log_path"]), expected_hash=sm["log_sha256"],
                    expected_size=sm["log_bytes"], label="failed smoke log")[-8192:]
                raise _BundleRepairNeeded(
                    (("Codex operator 拒绝 smoke 终态：" + operator_reason + "\n")
                     if operator_reason is not None else "smoke 失败，按原 plan 修复代码：\n")
                    + tail.decode("utf-8", errors="replace"),
                    failure_kind="smoke", phase="smoke")
            if st() == "building":
                g.gate_progress_build_target(build_target_id=bt_id, to="smoke")
        if st() == "smoke" or repair is not None:
            # Exactly one independent code↔plan review per materialized code
            # version, after its smoke and before any new training/evaluation.
            code_sh = self._code_subject_hash(slice_, manifest, ledger, staging)
            if g.require_code_review:
                if "judge" not in self.p:
                    raise RuntimeError("代码评审已启用但未装配 judge provider")
                self._judge_once(cyc.cycle_id, bt_id, "bundle_code_review", code_sh)
            if (g.require_code_review and not g.review_passed(
                        build_target_id=bt_id, review_kind="bundle_code_review",
                        current_subject_hash=code_sh)):
                review = d.query_one(
                    "SELECT payload_json FROM decision WHERE actor='judge' "
                    "AND type='bundle_code_review' AND json_valid(payload_json) "
                    "AND json_extract(payload_json,'$.build_target_id')=? "
                    "ORDER BY id DESC LIMIT 1", (bt_id,))
                raise _BundleRepairNeeded(
                    "code↔plan reviewer 未通过：\n" + (review[0] if review else "未给反馈"),
                    failure_kind="review_failed", phase="code_review")
            if st() == "smoke":
                g.gate_progress_build_target(
                    build_target_id=bt_id, to="running", current_subject_hash=code_sh)
            if repair is not None:
                self._validate_open_repair(cyc, bt_id)
        if st() == "running":
            self._run_and_register(cyc, bt_id, slice_, manifest, ledger, staging, src_dir,
                                   allowed_asset_refs, asset_identities,
                                   execution_sandbox=execution_sandbox)
        self._ensure_target_pc(cyc, bt_id)

    def _run_eval_target(self, cyc, bt_id: int, slice_, manifest, ledger,
                         staging: Path, src_dir: Path,
                         allowed_asset_refs, asset_identities, *,
                         execution_sandbox=None) -> None:
        """Execute a plan ``target_kind=eval`` against one existing legal checkpoint.

        The target owns no training run and never mutates pool identity.  It
        creates/appends a pre-call evaluation attempt, executes only the eval
        command, result-reviews the measurement package, then atomically seals
        attempt+metrics.  Recovery uses the same attempt-owned guardian receipt
        contract as factory evaluation.
        """
        g, d, ci = self.gate, self.state.daemon, cyc.cycle_id
        bt = d.query_one(
            "SELECT variant_id,evaluation_id,eval_action,attempt_purpose FROM build_target WHERE id=?",
            (bt_id,))
        if bt is None or bt[0] is None or bt[2] not in ("create_evaluation", "append_attempt"):
            raise RuntimeError(f"eval target {bt_id} 绑定损坏")
        vid, bound_eid, eval_action, planned_purpose = bt
        checkpoints = d.query(
            "SELECT id,ckpt_key,path,content_hash FROM checkpoint WHERE variant_id=? ORDER BY id", (vid,))
        if not checkpoints:
            raise RuntimeError(
                f"eval target {bt_id} 的 v{vid} 无 legal checkpoint")
        checkpoint_ids = {row[1]: row[0] for row in checkpoints}
        checkpoint_paths: Dict[str, Path] = {}
        checkpoint_hashes = {row[1]: row[3] for row in checkpoints}
        for checkpoint_id, checkpoint_key, checkpoint_path, checkpoint_hash in checkpoints:
            try:
                checkpoint_file = resolve_registered_path(self.work, checkpoint_path)
            except RegisteredPathError as error:
                raise RuntimeError(
                    f"eval target {bt_id} checkpoint ck{checkpoint_id} path-lineage 非法") from error
            if H.file_sha256(checkpoint_file) != checkpoint_hash:
                raise RuntimeError(
                    f"eval target {bt_id} checkpoint ck{checkpoint_id} 内容与 DB hash 不一致")
            checkpoint_paths[checkpoint_key] = checkpoint_file

        training_publication = self._recover_training_publication(
            variant_id=vid) if self.pool_publisher is not None else None
        pool_publication: Optional[VerifiedPoolPublication] = None

        if eval_action == "create_evaluation":
            erow = d.query_one(
                "SELECT id,status,canonical_attempt_id FROM evaluation WHERE build_target_id=?", (bt_id,))
        else:
            erow = d.query_one(
                "SELECT id,status,canonical_attempt_id FROM evaluation WHERE id=?", (bound_eid,))
            if erow is None:
                raise RuntimeError(f"eval append target {bt_id} 指向不存在的 e{bound_eid}")
        latest = (None if erow is None else d.query_one(
            "SELECT id,status,attempt_no,purpose,failure_kind FROM evaluation_attempt "
            "WHERE evaluation_id=? AND build_target_id=? ORDER BY attempt_no DESC LIMIT 1",
            (erow[0], bt_id)))

        if latest is not None and latest[1] == "failed":
            target_failure = ("protocol_violation"
                              if latest[4] in ("protocol_violation", "metric_missing",
                                               "data_invalid", "artifact_invalid")
                              else latest[4] or "runtime")
            if not self._repair_authorized(bt_id, "attempt", latest[0]):
                raise _BundleRepairNeeded(
                    f"eval attempt {latest[0]} 已失败，须先由 bundle 修复",
                    failure_kind=target_failure, phase="eval",
                    repair_of={"attempt_id": latest[0]})
        if latest is not None and latest[1] == "success":
            eid, aid, attempt_no, attempt_purpose = erow[0], latest[0], latest[2], latest[3]
        elif latest is not None and latest[1] == "running":
            eid, aid, attempt_no, attempt_purpose = erow[0], latest[0], latest[2], latest[3]
        else:
            if latest is not None and latest[1] not in ("aborted", "failed"):
                raise RuntimeError(
                    f"eval target {bt_id} latest attempt 状态不可恢复: {latest[1]}")
            if latest is not None:
                attempt_purpose = "retry"
                started = g.gate_start_attempt(
                    cycle_id=ci, purpose="retry", build_target_id=bt_id,
                    evaluation_id=erow[0], retry_of=latest[0],
                    env_hash=manifest["env_hash"],
                    watchdog_sec=min(
                        float(manifest["commands"]["eval"].get(
                            "timeout_s", self.policy["execution"]["default_timeout_s"])),
                        float(self.policy["execution"]["max_timeout_s"])))
            elif eval_action == "create_evaluation":
                attempt_purpose = planned_purpose
                if attempt_purpose == "retry":
                    raise _BundleReject(
                        "create_evaluation 首 attempt 不得声明 retry", failure_kind="protocol_violation")
                started = g.gate_start_attempt(
                    cycle_id=ci, purpose=attempt_purpose, build_target_id=bt_id,
                    create={
                        "variant_id": vid, "protocol_id": slice_["protocol_id"],
                        "protocol_ver": slice_["protocol_ver"], "eval_key": slice_["eval_key"],
                        "source": slice_["evaluation_source"],
                        "target_set_hash": slice_["target_set_hash"],
                    }, env_hash=manifest["env_hash"],
                    watchdog_sec=min(
                        float(manifest["commands"]["eval"].get(
                            "timeout_s", self.policy["execution"]["default_timeout_s"])),
                        float(self.policy["execution"]["max_timeout_s"])))
            else:
                attempt_purpose = planned_purpose
                retry_of = None
                if attempt_purpose == "retry":
                    previous = d.query_one(
                        "SELECT id,status FROM evaluation_attempt WHERE evaluation_id=? "
                        "ORDER BY attempt_no DESC LIMIT 1", (erow[0],))
                    if previous is None or previous[1] not in ("failed", "aborted"):
                        raise _BundleReject(
                            "plan retry target 无同 evaluation 的 failed/aborted 前序 attempt",
                            failure_kind="protocol_violation")
                    retry_of = previous[0]
                started = g.gate_start_attempt(
                    cycle_id=ci, purpose=attempt_purpose, build_target_id=bt_id,
                    evaluation_id=erow[0], retry_of=retry_of,
                    env_hash=manifest["env_hash"],
                    watchdog_sec=min(
                        float(manifest["commands"]["eval"].get(
                            "timeout_s", self.policy["execution"]["default_timeout_s"])),
                        float(self.policy["execution"]["max_timeout_s"])))
            eid, aid, attempt_no = (
                started["evaluation_id"], started["attempt_id"], started["attempt_no"])

        eval_dir = staging / f"eval-a{aid}"
        eval_final = eval_dir / "eval.log"
        if latest is not None and latest[1] == "success":
            ev = None
        else:
            eval_context = {
                "cycle_id": ci, "build_target_id": bt_id, "phase": "eval",
                "reconcile_protocol": "execution-owner-v1",
                "db_owner_kind": "evaluation_attempt", "db_owner_id": aid,
            }
            if eval_final.exists():
                exit_file = eval_final.with_name("eval.log.exit")
                if not exit_file.exists():
                    raise RuntimeError(f"staging 损毁：{eval_final} 在而 exit 侧车缺——须人工核")
                eval_log = read_artifact_bytes(
                    eval_final, label="persisted eval log")
                ev = {"exit_code": int(read_artifact_bytes(
                          exit_file, max_bytes=32,
                          label="eval exit sidecar").decode("ascii")),
                      "log_path": str(eval_final),
                      "log_sha256": hashlib.sha256(eval_log).hexdigest(),
                      "log_bytes": len(eval_log)}
            else:
                ev = H.recover_staged_result(
                    staging_dir=str(eval_dir), log_name="eval.log",
                    execution_supervisor=self.execution_supervisor,
                    execution_kind="manifest-eval", execution_context=eval_context,
                    execution_sandbox=execution_sandbox)
                if ev is None:
                    self.owner_guard()
                    try:
                        ev = self._run_manifest_as_bundle_operator(
                            cyc, bt_id, phase="eval", owner_kind="evaluation_attempt",
                            owner_id=aid, manifest=manifest, ledger=ledger,
                            staging_dir=str(eval_dir), log_name="eval.log",
                            src_dir=src_dir, work_root=self.work, policy=self.policy,
                            checkpoint_paths=checkpoint_paths,
                            checkpoint_content_hashes=checkpoint_hashes,
                            expected_source_hashes=ledger,
                            allowed_asset_refs=allowed_asset_refs,
                            expected_asset_identities=asset_identities,
                            execution_supervisor=self.execution_supervisor,
                            execution_context=eval_context,
                            execution_sandbox=execution_sandbox)
                    except _BundleOperatorRepair as error:
                        g.gate_finish_attempt(
                            attempt_id=aid, status="failed", failure_kind="runtime")
                        if d.query_one(
                                "SELECT status FROM evaluation WHERE id=?", (eid,))[0] != "success":
                            g.gate_finish_evaluation(evaluation_id=eid)
                        raise _BundleRepairNeeded(
                            "Codex operator 在 eval 运行中请求停止并修复：\n"
                            + error.diagnosis,
                            failure_kind="runtime", phase="eval",
                            repair_of={"attempt_id": aid}) from error
                eval_log = read_artifact_bytes(
                    ev["log_path"], expected_hash=ev["log_sha256"],
                    expected_size=ev["log_bytes"], label="eval log receipt")

            attempt_artifact_ref = f"sha256:{ev['log_sha256']}"
            attempt_transcript_ref = ev.get("process_receipt_path")

            def finish_attempt_failure(failure_kind: str) -> None:
                g.gate_finish_attempt(
                    attempt_id=aid, status="failed", failure_kind=failure_kind,
                    transcript_ref=attempt_transcript_ref,
                    artifact_ref=attempt_artifact_ref)
                if d.query_one("SELECT status FROM evaluation WHERE id=?", (eid,))[0] != "success":
                    g.gate_finish_evaluation(evaluation_id=eid)

            operator_reason = self._bundle_operator_terminal_reason(
                cyc, bt_id, phase="eval", owner_kind="evaluation_attempt",
                owner_id=aid, manifest=manifest, ledger=ledger, result=ev)
            if ev["exit_code"] != 0 or operator_reason is not None:
                finish_attempt_failure("runtime")
                raise _BundleRepairNeeded(
                    (("Codex operator 拒绝 eval 终态：" + operator_reason + "\n")
                     if operator_reason is not None
                     else "评估失败，按冻结 evaluation/protocol 修复：\n")
                    + eval_log[-8192:].decode("utf-8", errors="replace"),
                    failure_kind="runtime", phase="eval",
                    repair_of={"attempt_id": aid})
            try:
                metrics = self._metrics_from_eval_log(
                    eval_log.decode("utf-8", errors="replace"), checkpoint_ids)
            except _BundleReject as error:
                finish_attempt_failure(error.failure_kind)
                raise _BundleRepairNeeded(
                    str(error), failure_kind=error.failure_kind, phase="eval_parse",
                    repair_of={"attempt_id": aid}) from error
            result_subject = SM.subject_hash(SM.result_review_manifest(
                metrics_artifact_hash=_canon_hash(metrics),
                checkpoint_hashes={
                    f"ck{checkpoint_ids[key]}:{key}": checkpoint_hashes[key]
                    for key in sorted(checkpoint_ids)},
                run_log_hashes={ev["log_path"]: ev["log_sha256"]},
                parser_obs_hash=_canon_hash(OP.parse_log(
                    eval_log.decode("utf-8", errors="replace"), self.obs_policy))))
            if g.require_result_review:
                if "judge" not in self.p:
                    raise RuntimeError("结果评审已启用但未装配 judge provider")
                self._judge_once(ci, bt_id, "bundle_result_review", result_subject)
            if (g.require_result_review and not g.review_passed(
                    build_target_id=bt_id, review_kind="bundle_result_review",
                    current_subject_hash=result_subject)):
                finish_attempt_failure("protocol_violation")
                review = d.query_one(
                    "SELECT payload_json FROM decision WHERE actor='judge' "
                    "AND type='bundle_result_review' AND json_valid(payload_json) "
                    "AND json_extract(payload_json,'$.build_target_id')=? "
                    "ORDER BY id DESC LIMIT 1", (bt_id,))
                raise _BundleRepairNeeded(
                    "结果 reviewer 未通过：\n" + (review[0] if review else "未给反馈"),
                    failure_kind="review_failed", phase="result_review",
                    repair_of={"attempt_id": aid})
            if training_publication is not None:
                pool_publication = self._publish_evaluation_assets(
                    training=training_publication, evaluation_id=eid,
                    attempt_id=aid, attempt_no=attempt_no, metrics=metrics,
                    eval_log=eval_final,
                    transcript_ref=ev.get("process_receipt_path"))
                self._register_published_evaluation_log(ci, pool_publication, aid)
                binding = pool_publication.database_bindings["evaluation_attempt"]
                attempt_artifact_ref = binding["artifact_ref"]
                attempt_transcript_ref = pool_publication.manifest_ref
            try:
                g.gate_register_evaluation(
                    cycle_id=ci, build_target_id=bt_id, purpose=attempt_purpose,
                    current_subject_hash=result_subject, metric_results=metrics,
                    attempt_id=aid, artifact_ref=attempt_artifact_ref,
                    transcript_ref=attempt_transcript_ref,
                    publication=pool_publication)
            except GateReject as error:
                finish_attempt_failure("protocol_violation")
                raise _BundleRepairNeeded(
                    f"eval target 测量注册被拒: {error}",
                    failure_kind="protocol_violation", phase="register_evaluation",
                    repair_of={"attempt_id": aid}) from error

        if pool_publication is None:
            pool_publication = self._recover_evaluation_publication(
                evaluation_id=eid, attempt_id=aid)
        if pool_publication is not None:
            self._register_published_evaluation_log(ci, pool_publication, aid)
        else:
            self._register_and_ingest_log(
                ci, eval_final, log_kind="eval", evaluation_attempt_id=aid)
        if not OP.suspect_attempt_has_current_obs(d.conn, aid, self.obs_policy):
            raise RuntimeError(
                f"eval target {bt_id} attempt {aid} 无当前口径 parser 观测")
        g.gate_finish_build_target(build_target_id=bt_id, status="complete")

    def _run_and_register(self, cyc, bt_id: int, slice_, manifest, ledger, staging: Path, src_dir: Path,
                          allowed_asset_refs, asset_identities, *,
                          execution_sandbox=None) -> None:
        """phase (i) 执行事实 + phase (ii) 注册段（结构可恢复的短事务序列）。命令由 manifest 驱动（步⑧）。
        ⚠️ 与 import_worker._run_and_register_import **同构**（恢复缝隙修复须双向同步；共享骨架=M6 硬化）。"""
        g, d = self.gate, self.state.daemon
        ci = cyc.cycle_id
        vid = d.query_one("SELECT variant_id FROM build_target WHERE id=?", (bt_id,))[0]
        bid = d.query_one("SELECT baseline_id FROM build_target WHERE id=?", (bt_id,))[0]
        env_hash = manifest["env_hash"]
        # —— (i) 训练 run。DB intent 先于进程；drained exit 回执可补 harness 本地发布，绝不盲目重训。——
        run_row = d.query_one(
            "SELECT id,status,failure_kind FROM run WHERE build_target_id=? ORDER BY id DESC", (bt_id,))
        rid: Optional[int] = None
        train_result: Optional[Dict[str, Any]] = None
        training_publication: Optional[VerifiedTrainingPublication] = None
        if run_row and run_row[1] == "success":
            rid = run_row[0]
        elif run_row and run_row[1] == "failed":
            if run_row[2] != "aborted":
                if not self._repair_authorized(bt_id, "run", run_row[0]):
                    raise _BundleRepairNeeded(
                        f"run {run_row[0]} 已失败，须先由 bundle 消费日志并修复",
                        failure_kind=run_row[2] or "runtime", phase="train",
                        repair_of={"run_id": run_row[0]})
                # A validated replacement authorizes a new run intent.  The old
                # failed run stays append-only as an execution fact.
                rid = None
            # owner_lost / 调用前崩溃是显式 aborted，可追加新 run 重试。
        elif run_row and run_row[1] == "running":
            rid = run_row[0]
            train_dir = staging / f"run{rid}"
            train_final = train_dir / "train.log"
            train_context = {
                "cycle_id": ci, "build_target_id": bt_id,
                "run_id": rid, "phase": "train",
                "reconcile_protocol": "execution-owner-v1",
                "db_owner_kind": "run", "db_owner_id": rid,
            }
            if train_final.exists():
                exit_file = train_final.with_name("train.log.exit")
                if not exit_file.exists():
                    raise RuntimeError(
                        f"staging 损毁：{train_final} 在而 exit 侧车缺——须人工核")
                train_bytes = read_artifact_bytes(
                    train_final, label="persisted train log")
                train_result = {
                    "exit_code": int(read_artifact_bytes(
                        exit_file, max_bytes=32,
                        label="train exit sidecar").decode("ascii")),
                    "log_path": str(train_final),
                    "log_sha256": hashlib.sha256(train_bytes).hexdigest(),
                    "log_bytes": len(train_bytes),
                }
            else:
                train_result = H.recover_staged_result(
                    staging_dir=str(train_dir), log_name="train.log",
                    execution_supervisor=self.execution_supervisor,
                    execution_kind="manifest-train", execution_context=train_context,
                    execution_sandbox=execution_sandbox)
            if train_result is None:
                # 没有 receipt/partial = gate_start_run 后、外部调用前死亡；确证未启动，
                # 冻结旧 intent 为 aborted 后用新 run id 重试。
                g.gate_finish_run(run_id=rid, status="failed", failure_kind="aborted")
                rid = None

        if rid is None:
            rid = g.gate_start_run(build_target_id=bt_id, cycle_id=ci, variant_id=vid,
                                   kind=slice_["target_kind"],   # exec 目标→run.kind='exec'（trg_run_target_consistent）
                                   env_hash=env_hash)
            train_context = {
                "cycle_id": ci, "build_target_id": bt_id,
                "run_id": rid, "phase": "train",
                "reconcile_protocol": "execution-owner-v1",
                "db_owner_kind": "run", "db_owner_id": rid,
            }
            self.owner_guard()
            try:
                train_result = self._run_manifest_as_bundle_operator(
                    cyc, bt_id, phase="train", owner_kind="run", owner_id=rid,
                    manifest=manifest, ledger=ledger,
                    staging_dir=str(staging / f"run{rid}"), log_name="train.log",
                    src_dir=src_dir, work_root=self.work,
                    policy=self.policy, expected_source_hashes=ledger,
                    allowed_asset_refs=allowed_asset_refs,
                    expected_asset_identities=asset_identities,
                    execution_supervisor=self.execution_supervisor,
                    execution_context=train_context,
                    execution_sandbox=execution_sandbox)
            except _BundleOperatorRepair as error:
                g.gate_finish_run(run_id=rid, status="failed", failure_kind="runtime")
                raise _BundleRepairNeeded(
                    "Codex operator 在 train 运行中请求停止并修复：\n"
                    + error.diagnosis,
                    failure_kind="runtime", phase="train",
                    repair_of={"run_id": rid}) from error

        if d.query_one("SELECT status FROM run WHERE id=?", (rid,))[0] != "success":
            if train_result is None:
                raise RuntimeError(f"run {rid} 非 success 且无可恢复执行结果")
            operator_reason = self._bundle_operator_terminal_reason(
                cyc, bt_id, phase="train", owner_kind="run", owner_id=rid,
                manifest=manifest, ledger=ledger, result=train_result)
            if train_result["exit_code"] != 0 or operator_reason is not None:
                g.gate_finish_run(run_id=rid, status="failed", failure_kind="runtime")
                train_log = read_artifact_bytes(
                    Path(train_result["log_path"]),
                    expected_hash=train_result["log_sha256"],
                    expected_size=train_result["log_bytes"],
                    label="failed train log")
                raise _BundleRepairNeeded(
                    (("Codex operator 拒绝 train 终态：" + operator_reason + "\n")
                     if operator_reason is not None
                     else "训练失败，按冻结 plan 修复实现：\n")
                    + train_log[-8192:].decode("utf-8", errors="replace"),
                    failure_kind="runtime", phase="train",
                    repair_of={"run_id": rid})
            ck_paths = MF.checkpoint_destinations(manifest, staging / f"run{rid}")
            try:
                with ExitStack() as stack:
                    checkpoint_caps = {
                        key: stack.enter_context(open_artifact(
                            path, label=f"run checkpoint publication {key}"))
                        for key, path in ck_paths.items()
                    }
                    legacy = "checkpoint" in manifest.get("expected_outputs", {})
                    source_hashes = {
                        key: checkpoint_caps[key].identity.content_hash.removeprefix("sha256:")
                        for key in ck_paths}
                    training_publication = self._publish_training_assets(
                        cycle_id=ci, build_target_id=bt_id, run_id=rid,
                        variant_id=vid, baseline_id=bid,
                        target_kind=slice_["target_kind"], manifest=manifest,
                        src_dir=src_dir, checkpoint_sources=ck_paths,
                        checkpoint_hashes=source_hashes)
                    if training_publication is None:
                        expected_rows = [
                            (vid, f"final-r{rid}" if legacy else key, str(path),
                             source_hashes[key], rid)
                            for key, path in ck_paths.items()]
                    else:
                        expected_rows = [
                            (vid, item["ckpt_key"], item["path"],
                             item["content_hash"], rid)
                            for item in training_publication.checkpoint_bindings]
                    with d.transaction() as conn:  # checkpoint 登记（run 产物；finish_run success 的前置）
                        existing = conn.execute(
                            "SELECT variant_id,ckpt_key,path,content_hash,produced_by_run "
                            "FROM checkpoint WHERE produced_by_run=? ORDER BY ckpt_key", (rid,)).fetchall()
                        expected_sorted = sorted(expected_rows, key=lambda item: item[1])
                        if not existing:
                            conn.executemany(
                                "INSERT INTO checkpoint(variant_id,ckpt_key,path,content_hash,hash_alg,produced_by_run) "
                                "VALUES (?,?,?,?,'sha256',?)", expected_sorted)
                        elif [tuple(item) for item in existing] != expected_sorted:
                            raise RuntimeError(
                                f"run {rid} checkpoint set durable identity 与正式发布不一致")
                        if training_publication is not None:
                            checkpoint_rows = conn.execute(
                                "SELECT id,ckpt_key FROM checkpoint WHERE produced_by_run=? "
                                "ORDER BY ckpt_key", (rid,)).fetchall()
                            bind_training_database(
                                conn, training_publication, updated_cycle=_cnum(ci),
                                checkpoint_ids={row[1]: row[0] for row in checkpoint_rows},
                                run_id=rid)
                    for checkpoint_cap in checkpoint_caps.values():
                        checkpoint_cap.verify_unchanged()
                        checkpoint_cap.verify_path_binding()
                    g.gate_finish_run(run_id=rid, status="success")
                    for checkpoint_cap in checkpoint_caps.values():
                        checkpoint_cap.verify_unchanged()
                        checkpoint_cap.verify_path_binding()
            except ArtifactCapabilityError as error:
                if d.query_one("SELECT status FROM run WHERE id=?", (rid,))[0] == "running":
                    g.gate_finish_run(
                        run_id=rid, status="failed", failure_kind="artifact_invalid")
                raise _BundleRepairNeeded(
                    f"run {rid} checkpoint 集缺失或身份漂移: {error}",
                    failure_kind="artifact_invalid", phase="checkpoint",
                    repair_of={"run_id": rid}) from error
        if self.pool_publisher is not None and training_publication is None:
            training_publication = self._recover_training_publication(
                variant_id=vid, run_id=rid)
        # train log 入账 + 观测 ingest：**无条件、幂等**（不藏在 fresh 分支——崩在 finish_run 与 ingest 之间时，
        # 复用 run 的续跑须从 staging 存活文件补登，否则杀 vs 不杀终库不一致，内审 SHOULD）
        self._register_and_ingest_log(ci, staging / f"run{rid}" / "train.log", log_kind="train", run_id=rid)
        checkpoint_rows = d.query(
            "SELECT id,ckpt_key,path,content_hash FROM checkpoint "
            "WHERE produced_by_run=? ORDER BY ckpt_key", (rid,))
        checkpoint_by_db_key = {row[1]: row for row in checkpoint_rows}
        checkpoint_ids: Dict[str, int] = {}
        checkpoint_paths: Dict[str, Path] = {}
        checkpoint_hashes: Dict[str, str] = {}
        legacy_checkpoint = "checkpoint" in manifest.get("expected_outputs", {})
        for spec in MF.checkpoint_specs(manifest):
            db_key = f"final-r{rid}" if legacy_checkpoint else spec.ckpt_key
            row = checkpoint_by_db_key.get(db_key)
            if row is None:
                raise RuntimeError(
                    f"run {rid} 缺 manifest checkpoint {spec.ckpt_key!r}/{db_key!r}")
            try:
                path = resolve_registered_path(self.work, row[2])
            except RegisteredPathError as error:
                raise RuntimeError(
                    f"run {rid} checkpoint {spec.ckpt_key!r} path-lineage 非法") from error
            if H.file_sha256(path) != row[3]:
                raise RuntimeError(
                    f"run {rid} checkpoint {spec.ckpt_key!r} 内容与 DB hash 不一致")
            checkpoint_ids[spec.ckpt_key] = row[0]
            checkpoint_paths[spec.ckpt_key] = path
            checkpoint_hashes[spec.ckpt_key] = row[3]
        # —— (ii) 出厂评估 + 注册段 ——
        erow = d.query_one(
            "SELECT id,status,canonical_attempt_id FROM evaluation WHERE build_target_id=?", (bt_id,))
        eval_final = None
        pool_publication: Optional[VerifiedPoolPublication] = None
        if erow is None or erow[1] != "success":
            if erow is None:
                attempt_purpose = "factory"
                started = g.gate_start_attempt(
                    cycle_id=ci, purpose="factory", build_target_id=bt_id,
                    create={"variant_id": vid, "protocol_id": slice_["protocol_id"],
                            "protocol_ver": slice_["protocol_ver"], "eval_key": slice_["eval_key"],
                            "source": "factory", "target_set_hash": slice_["target_set_hash"]},
                    env_hash=env_hash,
                    watchdog_sec=min(
                        float(manifest["commands"]["eval"].get(
                            "timeout_s", self.policy["execution"]["default_timeout_s"])),
                        float(self.policy["execution"]["max_timeout_s"])))
            else:
                latest = d.query_one(
                    "SELECT id,status,attempt_no,purpose,failure_kind FROM evaluation_attempt "
                    "WHERE evaluation_id=? ORDER BY attempt_no DESC LIMIT 1", (erow[0],))
                if latest is None:
                    raise RuntimeError(f"evaluation {erow[0]} 非 success 却无 attempt")
                if latest[1] == "running":
                    attempt_purpose = latest[3]
                    started = {"evaluation_id": erow[0], "attempt_id": latest[0],
                               "attempt_no": latest[2]}
                elif latest[1] == "failed":
                    if not self._repair_authorized(bt_id, "attempt", latest[0]):
                        target_failure = ("protocol_violation"
                                          if latest[4] in ("protocol_violation", "metric_missing",
                                                           "data_invalid", "artifact_invalid")
                                          else latest[4] or "runtime")
                        raise _BundleRepairNeeded(
                            f"evaluation attempt {latest[0]} 已失败，须先修复 bundle",
                            failure_kind=target_failure, phase="eval",
                            repair_of={"attempt_id": latest[0]})
                    attempt_purpose = "retry"
                    started = g.gate_start_attempt(
                        cycle_id=ci, purpose="retry", build_target_id=bt_id,
                        evaluation_id=erow[0], retry_of=latest[0], env_hash=env_hash,
                        watchdog_sec=min(
                            float(manifest["commands"]["eval"].get(
                                "timeout_s", self.policy["execution"]["default_timeout_s"])),
                            float(self.policy["execution"]["max_timeout_s"])))
                elif latest[1] == "aborted":
                    attempt_purpose = "retry"
                    started = g.gate_start_attempt(
                        cycle_id=ci, purpose="retry", build_target_id=bt_id,
                        evaluation_id=erow[0], retry_of=latest[0], env_hash=env_hash,
                        watchdog_sec=min(
                            float(manifest["commands"]["eval"].get(
                                "timeout_s", self.policy["execution"]["default_timeout_s"])),
                            float(self.policy["execution"]["max_timeout_s"])))
                else:
                    raise RuntimeError(
                        f"evaluation {erow[0]} status={erow[1]} 与 latest attempt={latest[1]} 不一致")
            eid, aid, attempt_no = (
                started["evaluation_id"], started["attempt_id"], started["attempt_no"])
            eval_dir = (staging / f"eval{rid}" if attempt_no == 1 else
                        staging / f"eval{rid}" / f"retry-a{aid}")
            eval_final = eval_dir / "eval.log"
            eval_context = {
                "cycle_id": ci,
                "build_target_id": bt_id,
                "run_id": rid,
                "phase": "eval",
                "reconcile_protocol": "execution-owner-v1",
                "db_owner_kind": "evaluation_attempt",
                "db_owner_id": aid,
            }
            if eval_final.exists():
                # 崩在「eval 跑完（final 已原子改名）→ register 前」的缝隙：**从存活 final 续注册、不重跑**
                # ——重跑会撞 run_staged 的同名 final 拒（codex BLOCKER：永久 FileExistsError 楔死）。
                # exit 判定复用 harness 侧车（final 存在 ⟹ 侧车先落）：失败进程即使输出了合法 metrics 也
                # **不得**被续注册成成功（codex 第2轮 BLOCKER）；侧车缺失 = staging 损毁 → fail loud。
                exit_file = eval_final.with_name("eval.log.exit")
                if not exit_file.exists():
                    raise RuntimeError(f"staging 损毁：{eval_final} 在而 exit 侧车缺——须人工核（不得臆判成功）")
                exit_code = int(read_artifact_bytes(
                    exit_file, max_bytes=32,
                    label="eval exit sidecar").decode("ascii"))
                eval_log = read_artifact_bytes(
                    eval_final, label="persisted eval log")
                ev = {"log_path": str(eval_final), "log_sha256": hashlib.sha256(eval_log).hexdigest(),
                      "log_bytes": len(eval_log), "exit_code": exit_code}
            else:
                ev = H.recover_staged_result(
                    staging_dir=str(eval_dir), log_name="eval.log",
                    execution_supervisor=self.execution_supervisor,
                    execution_kind="manifest-eval", execution_context=eval_context,
                    execution_sandbox=execution_sandbox)
                if ev is None:
                    self.owner_guard()
                    try:
                        ev = self._run_manifest_as_bundle_operator(
                            cyc, bt_id, phase="eval", owner_kind="evaluation_attempt",
                            owner_id=aid, manifest=manifest, ledger=ledger,
                            staging_dir=str(eval_dir), log_name="eval.log",
                            src_dir=src_dir, work_root=self.work,
                            policy=self.policy,
                            checkpoint_paths=checkpoint_paths,
                            checkpoint_content_hashes=checkpoint_hashes,
                            expected_source_hashes=ledger,
                            allowed_asset_refs=allowed_asset_refs,
                            expected_asset_identities=asset_identities,
                            execution_supervisor=self.execution_supervisor,
                            execution_context=eval_context,
                            execution_sandbox=execution_sandbox)
                    except _BundleOperatorRepair as error:
                        g.gate_finish_attempt(
                            attempt_id=aid, status="failed", failure_kind="runtime")
                        g.gate_finish_evaluation(evaluation_id=eid)
                        raise _BundleRepairNeeded(
                            "Codex operator 在 eval 运行中请求停止并修复：\n"
                            + error.diagnosis,
                            failure_kind="runtime", phase="eval",
                            repair_of={"attempt_id": aid}) from error
                eval_log = read_artifact_bytes(
                    ev["log_path"], expected_hash=ev["log_sha256"],
                    expected_size=ev["log_bytes"], label="eval log receipt")
            operator_reason = self._bundle_operator_terminal_reason(
                cyc, bt_id, phase="eval", owner_kind="evaluation_attempt",
                owner_id=aid, manifest=manifest, ledger=ledger, result=ev)
            if ev["exit_code"] != 0 or operator_reason is not None:
                g.gate_finish_attempt(
                    attempt_id=aid, status="failed", failure_kind="runtime",
                    transcript_ref=ev.get("process_receipt_path"),
                    artifact_ref=f"sha256:{ev['log_sha256']}")
                g.gate_finish_evaluation(evaluation_id=eid)
                raise _BundleRepairNeeded(
                    (("Codex operator 拒绝 eval 终态：" + operator_reason + "\n")
                     if operator_reason is not None
                     else "出厂评估失败，按冻结 plan/protocol 修复：\n")
                    + eval_log[-8192:].decode("utf-8", errors="replace"),
                    failure_kind="runtime", phase="eval",
                    repair_of={"attempt_id": aid})
            try:
                metrics = self._metrics_from_eval_log(
                    eval_log.decode("utf-8", errors="replace"), checkpoint_ids)
            except _BundleReject as error:
                g.gate_finish_attempt(
                    attempt_id=aid, status="failed", failure_kind="protocol_violation",
                    transcript_ref=ev.get("process_receipt_path"),
                    artifact_ref=f"sha256:{ev['log_sha256']}")
                g.gate_finish_evaluation(evaluation_id=eid)
                raise _BundleRepairNeeded(
                    str(error), failure_kind=error.failure_kind, phase="eval_parse",
                    repair_of={"attempt_id": aid}) from error
            res_sh = self._result_subject_hash(bt_id, slice_, ledger, rid, metrics, ev)
            if g.require_result_review:
                if "judge" not in self.p:
                    raise RuntimeError("结果评审已启用但未装配 judge provider")
                self._judge_once(ci, bt_id, "bundle_result_review", res_sh)
            if (g.require_result_review and not g.review_passed(
                        build_target_id=bt_id, review_kind="bundle_result_review",
                        current_subject_hash=res_sh)):
                # 结果评审 FAIL → review_failed：run(success)+checkpoint 保留（训练事实），测量整包不注册
                # （§4.2.5：第(ii)段不发生）——lockstep：import_worker 同修
                g.gate_finish_attempt(
                    attempt_id=aid, status="failed", failure_kind="protocol_violation",
                    transcript_ref=ev.get("process_receipt_path"),
                    artifact_ref=f"sha256:{ev['log_sha256']}")
                g.gate_finish_evaluation(evaluation_id=eid)
                review = d.query_one(
                    "SELECT payload_json FROM decision WHERE actor='judge' "
                    "AND type='bundle_result_review' AND json_valid(payload_json) "
                    "AND json_extract(payload_json,'$.build_target_id')=? "
                    "ORDER BY id DESC LIMIT 1", (bt_id,))
                raise _BundleRepairNeeded(
                    "结果 reviewer 未通过：\n" + (review[0] if review else "未给反馈"),
                    failure_kind="review_failed", phase="result_review",
                    repair_of={"attempt_id": aid})
            registration_artifact_ref = f"sha256:{ev['log_sha256']}"
            registration_transcript_ref = ev.get("process_receipt_path")
            if self.pool_publisher is not None:
                if training_publication is None:
                    raise RuntimeError("factory evaluation 缺 formal training publication")
                pool_publication = self._publish_evaluation_assets(
                    training=training_publication, evaluation_id=eid,
                    attempt_id=aid, attempt_no=attempt_no, metrics=metrics,
                    eval_log=eval_final,
                    transcript_ref=ev.get("process_receipt_path"))
                self._register_published_evaluation_log(ci, pool_publication, aid)
                binding = pool_publication.database_bindings["evaluation_attempt"]
                registration_artifact_ref = binding["artifact_ref"]
                registration_transcript_ref = pool_publication.manifest_ref
            try:
                reg = self.gate.gate_register_evaluation(
                    cycle_id=ci, build_target_id=bt_id, purpose=attempt_purpose, current_subject_hash=res_sh,
                    metric_results=metrics, attempt_id=aid,
                    artifact_ref=registration_artifact_ref,
                    transcript_ref=registration_transcript_ref,
                    publication=pool_publication)
            except GateReject as e:
                # **只在此调用点**把注册闸拒转业务失败（codex 第2轮 BLOCKER 收窄）：此处的拒 = 评估测量包
                # 不满足协议/required 契约（Codex eval 产物层问题）→ 目标 failed(protocol_violation)、不楔死。
                # 其余 gate（start/progress/finish/register_baseline）的拒仍 fail loud。
                g.gate_finish_attempt(
                    attempt_id=aid, status="failed", failure_kind="protocol_violation",
                    transcript_ref=registration_transcript_ref,
                    artifact_ref=registration_artifact_ref)
                g.gate_finish_evaluation(evaluation_id=eid)
                raise _BundleRepairNeeded(
                    f"测量注册被拒: {e}", failure_kind="protocol_violation",
                    phase="register_evaluation",
                    repair_of={"attempt_id": aid}) from e
            eid, aid = reg["evaluation_id"], reg["attempt_id"]
        else:
            eid, aid = erow[0], erow[2]
            attempt_no = d.query_one(
                "SELECT attempt_no FROM evaluation_attempt WHERE id=?", (aid,))[0]
            eval_final = (staging / f"eval{rid}" / "eval.log" if attempt_no == 1 else
                          staging / f"eval{rid}" / f"retry-a{aid}" / "eval.log")
            pool_publication = self._recover_evaluation_publication(
                evaluation_id=eid, attempt_id=aid)
        # attempt-owned eval log 补登 + 观测 ingest（§4.2.5(ii)）：**无条件、幂等、从 staging 存活文件重导出**——
        # 崩在 register_evaluation 与 ingest 之间时，resume 走 else 分支若不补登，下方强制核将永远 raise、
        # target 永卡 running（内审 BLOCKER 实证复现：不可恢复楔死）。register/ingest 均幂等，重放零害。
        if pool_publication is not None:
            self._register_published_evaluation_log(ci, pool_publication, aid)
        else:
            self._register_and_ingest_log(ci, eval_final, log_kind="eval",
                                          evaluation_attempt_id=aid)
        # 管线强制：complete 前 attempt 须已有**当前口径** parser 观测（否则 suspect 无据可依成绕过）。
        # aid 由 register_evaluation 单事务保证非空（eval 存在 ⟹ canonical 已封）——此判为防御。
        if aid is None or OP.suspect_attempt_has_current_obs(d.conn, aid, self.obs_policy) is False:
            raise RuntimeError(f"bundle 管线约束：attempt {aid} 无当前口径 parser 观测且 staging eval.log 不可得"
                               "——须先 ingest 再 complete（staging 丢失属数据损毁，须人工介入）")
        if d.query_one("SELECT status FROM variant WHERE id=?", (vid,))[0] != "legal":
            if g.require_result_review:
                review_row = d.query_one(  # 复用注册时的 durable result review
                    "SELECT json_extract(payload_json,'$.subject_hash') FROM decision WHERE actor='judge' "
                    "AND type='bundle_result_review' AND json_extract(payload_json,'$.build_target_id')=? "
                    "ORDER BY id DESC LIMIT 1", (bt_id,))
                if review_row is None or not review_row[0]:
                    raise RuntimeError(f"target {bt_id} 已注册 evaluation 但缺 result review")
                res_sh2 = review_row[0]
            else:
                # Gate 在该策略下不消费 subject hash；明确占位只满足稳定调用 ABI，不伪造 review。
                res_sh2 = "review-disabled"
            if slice_["target_kind"] == "exec":
                # exec：只把本变体入池（baseline 已 legal，身份不动）——register_variant（非 register_baseline）
                self.gate.gate_register_variant(
                    variant_id=vid, build_target_id=bt_id, evaluation_id=eid,
                    cycle_id=ci, current_subject_hash=res_sh2, run_id=rid,
                    publication=pool_publication)
            else:
                # build：终版身份 = bundle 产的 identity.md 全文（替换 plan 期占位草稿）；复现 = manifest.repro_cmd_md
                identity_doc = read_artifact_bytes(
                    src_dir / MF.IDENTITY_FILE,
                    expected_hash=ledger[MF.IDENTITY_FILE],
                    label="baseline identity artifact").decode("utf-8")
                self.gate.gate_register_baseline(
                    baseline_id=bid, variant_id=vid, build_target_id=bt_id, evaluation_id=eid,
                    cycle_id=ci, current_subject_hash=res_sh2,
                    identity_doc=identity_doc, repro_cmd=manifest["repro_cmd_md"], run_id=rid,
                    publication=pool_publication)
        if d.query_one("SELECT status FROM build_target WHERE id=?", (bt_id,))[0] not in _TERMINAL_TARGET:
            self.gate.gate_finish_build_target(build_target_id=bt_id, status="complete")

    def _judge_once(self, cycle_id: str, bt_id: int, review_kind: str, subject_hash: str) -> None:
        judge_once(self.state.daemon, self.p["judge"], cycle_id, bt_id, review_kind, subject_hash)

    def _register_and_ingest_log(self, cycle_id: str, log_path: Path, *, log_kind: str,
                                 run_id: Optional[int] = None,
                                 evaluation_attempt_id: Optional[int] = None) -> None:
        """log 入账 + 观测 ingest 的**幂等补登**（fresh 与 resume 共用）：从 staging 存活文件重导出
        ref/hash/bytes——两调用均幂等（同 owner+kind+hash / 同 log+version+policy_hash 返既有行）。
        文件不存在 → 静默跳过（交由下游强制核裁决：eval 缺观测会 raise，train 缺仅失摘要面）。"""
        if evaluation_attempt_id is None and run_id is None:
            return
        if not log_path.exists():
            return
        expected_hash = None
        if evaluation_attempt_id is not None:
            # 强校验（codex BLOCKER×2）：补登字节须等于注册时锚在 attempt.artifact_ref 的评估 log 哈希——
            # 崩后 staging 被改写不得把 suspect attempt 洗成 clean。**无锚不 ingest**（None 锚放行=同一洞的
            # append/repro 变体）：凡走本管线补登的 success attempt 注册时必须带 sha256: 锚。
            exp = self.state.daemon.query_one("SELECT artifact_ref FROM evaluation_attempt WHERE id=?",
                                              (evaluation_attempt_id,))
            if not exp or not exp[0] or not exp[0].startswith("sha256:"):
                raise RuntimeError(f"attempt {evaluation_attempt_id} 无 sha256: artifact_ref 锚——"
                                   "拒绝从 staging 补登（注册时须锚评估 log 哈希）")
            expected_hash = exp[0]
        data = read_artifact_bytes(
            log_path, expected_hash=expected_hash,
            label=f"{log_kind} execution log")
        got = hashlib.sha256(data).hexdigest()
        if evaluation_attempt_id is not None:
            if exp[0] != f"sha256:{got}":
                raise RuntimeError(f"eval log 补登哈希不符（注册锚 {exp[0][:19]}…，实收 sha256:{got[:12]}…）"
                                   "——staging 被改写，拒绝入账（须人工核）")
        elid = H.register_execution_log(self.state.daemon, cycle_id=cycle_id, log_kind=log_kind,
                                        ref=str(log_path), content_hash=got,
                                        n_bytes=len(data), run_id=run_id,
                                        evaluation_attempt_id=evaluation_attempt_id)
        OP.ingest_observation(self.state.daemon, execution_log_id=elid, log_bytes=data,
                              obs_policy=self.obs_policy)

    def _ensure_target_pc(self, cyc, bt_id: int) -> None:
        """目标终态后落 phase_commit(bundle, target)——**完成标记**（幂等短事务；hash=终态+seq 规范串）。
        诚实注记（内审 SHOULD）：此 hash 锚终态而非产物集，**不承担** §4.2.5「staging 改写→conflict」侦测
        ——该防护在 bundle 侧由 result_review subject_hash 当下重算 + 终态目标不可重入（_drive_target 短路）
        + append-only gates 承担；产物集哈希锚 = M5/M6 硬化项。"""
        row = self.state.daemon.query_one("SELECT status, seq FROM build_target WHERE id=?", (bt_id,))
        ah = _canon_hash({"target": bt_id, "final": row[0], "seq": row[1]})
        with self.state.daemon.transaction() as conn:
            if check_or_record(conn, cycle_id=cyc.cycle_id, stage="bundle",
                               target_id=bt_id, artifact_hash=ah) == "conflict":
                raise ValueError(f"bundle target {bt_id} phase_commit 冲突（终态被改写？）")

    # -- subject 构造（编排器确定性重算，judge 不自算，§4.1.4 附注）----------------
    def _code_subject_hash(self, slice_: Dict[str, Any], manifest, ledger: Dict[str, str], staging: Path) -> str:
        """代码评审 subject（步⑧：真材料）：plan 切片哈希 + 物化代码 ledger 哈希（=真代码内容）+ 配置哈希 +
        identity 草稿哈希（staged）+ smoke transcript。编排器确定性重算，judge/register 两处一致。"""
        latest = H.latest_smoke_log(staging / "smoke")   # 数值序取最新（字典序 smoke-10<smoke-2，codex SHOULD）
        smoke_ref = str(latest) if latest else "smoke:none"
        smoke_hash = H.file_sha256(smoke_ref) if latest else "0" * 64
        return SM.subject_hash(SM.code_review_manifest(
            plan_slice_hash=_canon_hash(slice_), code_diff_hash=_canon_hash(ledger),
            config_hashes={"config_json": _canon_hash(manifest["config_json"])},
            identity_draft_hash=ledger[MF.IDENTITY_FILE],
            smoke_transcript_ref=smoke_ref, smoke_transcript_hash=smoke_hash))

    def _result_subject_hash(self, bt_id: int, slice_: Dict[str, Any], ledger: Dict[str, str],
                             rid: int, metrics, ev) -> str:
        ckrows = self.state.daemon.query(
            "SELECT ckpt_key, content_hash FROM checkpoint WHERE produced_by_run=? ORDER BY ckpt_key", (rid,))
        if not ckrows:
            raise RuntimeError(f"result review run {rid} 缺 checkpoint set")
        return SM.subject_hash(SM.result_review_manifest(
            metrics_artifact_hash=_canon_hash(metrics),
            checkpoint_hashes={row[0]: row[1] for row in ckrows},
            run_log_hashes={ev["log_path"]: ev["log_sha256"]},
            parser_obs_hash=_canon_hash(OP.parse_log(
                read_artifact_bytes(
                    ev["log_path"], expected_hash=ev["log_sha256"],
                    expected_size=ev["log_bytes"],
                    label="result-review eval log").decode("utf-8"),
                self.obs_policy)),
            identity_draft_hash=ledger[MF.IDENTITY_FILE]))

    @staticmethod
    def _metrics_from_eval_log(
            text: str, checkpoint_ids: Optional[Mapping[str, int]] = None
    ) -> List[Dict[str, Any]]:
        """Parse aggregate and fold metrics from the evaluated checkpoint set.

        Aggregate: ``metric_value: <mid>@<ver>=<float>``.
        Fold: ``metric_value: <mid>@<ver>[checkpoint=<ckpt_key>]=<float>``.
        A fold key is resolved only through the exact checkpoint rows supplied by
        the caller; free-form fold labels cannot manufacture checkpoint identity.

        `metric_value` 是保留记录前缀：出现该前缀却不完全匹配语法、同一 (metric_id, metric_ver) 重复、
        id 超出 SQLite INTEGER，或值为 NaN/Inf（含十进制溢出成 Inf），均是外部测量包协议违规，必须在
        进入 judge/DB 前转成 `_BundleReject(protocol_violation)`。普通日志行仍可共存。"""
        out: List[Dict[str, Any]] = []
        seen = set()
        for line_no, raw in enumerate(text.splitlines(), start=1):
            line = raw.strip()
            if not line.startswith("metric_value"):
                continue
            match = _METRIC_VALUE_RE.fullmatch(line)
            if match is None:
                raise _BundleReject(
                    f"eval.log 第 {line_no} 行 metric_value 记录格式非法: {line!r}",
                    failure_kind="protocol_violation")
            try:
                # 必须在 int() 前作字符串边界判定：数千位数字会先命中 Python
                # int_max_str_digits 而抛裸 ValueError，变成可跨重启复读的 poison pill。
                mid = parse_positive_sqlite_int(match.group(1), label="metric_id")
                mver = parse_positive_sqlite_int(match.group(2), label="metric_ver")
            except ValueError as e:
                raise _BundleReject(
                    f"eval.log 第 {line_no} 行 metric 绑定非法: {e}",
                    failure_kind="protocol_violation") from e
            try:
                value = float(match.group(4))
            except (ValueError, OverflowError) as e:
                raise _BundleReject(
                    f"eval.log 第 {line_no} 行 metric_value 数值非法: {match.group(4)!r}",
                    failure_kind="protocol_violation") from e
            if not math.isfinite(value):
                raise _BundleReject(
                    f"eval.log 第 {line_no} 行 metric_value 非有限值: {match.group(4)!r}",
                    failure_kind="protocol_violation")
            checkpoint_key = match.group(3)
            key = (mid, mver, checkpoint_key)
            if key in seen:
                raise _BundleReject(
                    f"eval.log 第 {line_no} 行重复 metric_value 绑定: "
                    f"{mid}@{mver}/{checkpoint_key or 'aggregate'}",
                    failure_kind="protocol_violation")
            seen.add(key)
            metric = {"metric_id": mid, "metric_ver": mver, "value": value,
                      "scope": "aggregate"}
            if checkpoint_key is not None:
                if checkpoint_ids is None or checkpoint_key not in checkpoint_ids:
                    raise _BundleReject(
                        f"eval.log 第 {line_no} 行引用未消费的 checkpoint key "
                        f"{checkpoint_key!r}", failure_kind="protocol_violation")
                metric.update({"scope": "fold",
                               "checkpoint_id": checkpoint_ids[checkpoint_key]})
            out.append(metric)
        return out

    @staticmethod
    def _next_serial(staging: Path, prefix: str) -> int:
        d = staging / prefix
        return len(list(d.glob(f"{prefix}-*.log"))) + 1 if d.exists() else 1

    def _durable_reasoning_answer_matches(self, cyc, ans: Dict[str, Any]) -> bool:
        """Strictly recognize a pre-atomic-version close left by an interrupted older process."""
        try:
            qi = _qnum(ans.get("question_id"))
        except ValueError:
            return False
        ci = _cnum(cyc.cycle_id)
        row = self.state.daemon.query_one(
            "SELECT a.id,a.goal_id,a.goal_ver,a.verdict,a.answer_md,q.status,q.closed_cycle,"
            "c.goal_id,c.goal_ver,c.status,c.active_question_id "
            "FROM answer a JOIN question q ON q.id=a.question_id "
            "JOIN cycle c ON c.id=a.cycle_id "
            "WHERE a.cycle_id=? AND a.question_id=?",
            (ci, qi))
        if row is None:
            return False
        aid, goal_id, goal_ver, verdict, answer_md, qstatus, closed_cycle, cgid, cgver, cstatus, active = row
        if (verdict != ans.get("verdict") or answer_md != ans.get("answer_md")
                or qstatus != verdict or closed_cycle != ci or (goal_id, goal_ver) != (cgid, cgver)
                or cstatus in ("done", "failed", "aborted") or active is not None):
            return False

        actual_rows = self.state.daemon.query(
            "SELECT e.kind,e.metric_result_id,e.literature_ref,ca.question_id,"
            "e.human_decision_id,e.claim_md "
            "FROM evidence e LEFT JOIN answer ca ON ca.id=e.child_answer_id "
            "WHERE e.answer_id=? ORDER BY e.id", (aid,))
        actual = []
        for kind, metric_result_id, literature_ref, child_question_id, human_id, claim_md in actual_rows:
            ref = (f"mr{metric_result_id}" if kind == "evaluation" else literature_ref
                   if kind == "literature" else f"q{child_question_id}"
                   if kind == "child_answer" else f"d{human_id}")
            actual.append((kind, ref, claim_md))
        expected = []
        for evidence in ans.get("evidence", []):
            kind = evidence.get("kind")
            ref = (evidence.get("metric_result_id") if kind == "evaluation" else
                   evidence.get("citation_md") if kind == "literature" else
                   evidence.get("child_question_id") if kind == "child_answer" else
                   evidence.get("human_ref") if kind == "human" else None)
            expected.append((kind, ref, evidence.get("note_md") or "(见 answer 正文)"))
        return actual == expected

    # ---------------------------------------------------------------- reasoning --
    def _semantic_retry_limit(self) -> int:
        """Use the existing bounded artifact retry budget for DB-aware semantics too."""
        if not isinstance(self.policy, dict):
            return 2
        value = self.policy.get("flow", {}).get("retry", {}).get("artifact_parse", 2)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RuntimeError("policy.flow.retry.artifact_parse 非法")
        return value

    def _archive_rejected_reasoning(self, art: Path, decision_id: int,
                                    artifact_hash: str) -> None:
        """Move a durably rejected poison artifact out of the active slot."""
        if not art.exists():
            return
        archived = art.with_name(
            f"reasoning.rejected-d{decision_id}-{artifact_hash[:12]}.json")
        if archived.exists():
            active_body = read_artifact_bytes(art, label="active rejected reasoning")
            archived_body = read_artifact_bytes(archived, label="archived rejected reasoning")
            if active_body != archived_body:
                raise RuntimeError("同一 reasoning retry decision 对应两份不同产物")
            art.unlink()
            return
        art.replace(archived)

    def _load_or_generate_reasoning(self, cyc) -> tuple[Path, Dict[str, Any]]:
        """Resume an accepted artifact, or skip every artifact durably rejected before a crash."""
        art = self.work / f"c{_cnum(cyc.cycle_id)}" / "reasoning.json"
        while art.exists():
            files = json.loads(read_artifact_bytes(
                art, label="persisted reasoning artifact").decode("utf-8"))
            artifact_hash = _canon_hash(files)
            rejected = self.state.daemon.query_one(
                "SELECT id FROM decision WHERE cycle_id=? AND actor='orchestrator' "
                "AND type='reasoning_semantic_retry' "
                "AND json_extract(payload_json,'$.artifact_hash')=? ORDER BY id DESC LIMIT 1",
                (_cnum(cyc.cycle_id), artifact_hash))
            if rejected is None:
                return art, files
            self._archive_rejected_reasoning(art, int(rejected[0]), artifact_hash)

        pack = self.compiler.render(cycle_id=cyc.cycle_id, stage="reasoning")
        files = self.p["reasoning"](cyc, pack)
        art.parent.mkdir(parents=True, exist_ok=True)
        tmp = art.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(files, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        tmp.replace(art)
        return art, files

    def _retry_reasoning_or_finish(self, cyc, files: Dict[str, Any], reason: str,
                                   *, question_id: Optional[str] = None,
                                   selection_invalid: bool = False) -> None:
        """Terminalize one semantically rejected resident Reasoning result.

        Schema and precheck corrections belong to the live turn through MCP.
        Once that turn has submitted and the core semantic gate rejects it, the
        orchestrator must not recursively start/resume another model turn.
        This function therefore records the rejection and lets the Reasoning
        core transaction perform the only cycle/question terminalization.
        """
        self._finish_reasoning_rejected(
            cyc, files, reason, question_id=question_id,
            selection_invalid=selection_invalid)

    def _reasoning_stage(self, cyc) -> None:
        """attack 轮收尾：answer/evidence→tree_ops→selection→done 单事务。
        **产物先持久化再消费**（codex SHOULD）：reasoning files 先原子落 staging（tmp→replace），resume 时
        复用持久产物、不重调 provider。旧版本若曾崩在独立 Gate 提交后，只有 durable answer 的全部
        身份/正文/证据与持久产物逐项一致时才允许恢复；新路径不存在 close 与轮末提交之间的窗口。

        schema 合法但语义非法的外部产物不能成为 poison pill：answer 的目标/证据引用被拒，或 tree_ops
        被 StateStore 以 ValueError 拒绝时，tree/selection 原子批整体回滚，并由 reasoning_rejected
        核心事务直接收尾；编排器不重问同一 resident Reasoning 主会话。
        SQLite/IO/GateInvariantError/RuntimeError 等内部或损毁错误仍 fail loud。"""
        _art, files = self._load_or_generate_reasoning(cyc)
        if "selection.json" not in files:
            self._retry_reasoning_or_finish(
                cyc, files, "reasoning 必产 selection.json",
                question_id=cyc.question_id)
            return
        if self.state.cycle(cyc.cycle_id).route == "dependency_wait":
            self._finish_dependency_wait_reasoning(cyc, files)
            return
        ans = files.get("answer.json")
        round_question_id = cyc.question_id
        if ans is not None and round_question_id is None:
            if not self._durable_reasoning_answer_matches(cyc, ans):
                raise RuntimeError(
                    f"cycle {cyc.cycle_id} 已释放 active question，但无与 reasoning artifact 完全一致的 durable answer")
            round_question_id = ans["question_id"]
        if round_question_id is None:
            raise RuntimeError(f"attack cycle {cyc.cycle_id} 缺 active question 锚")
        if ans is not None:
            if ans.get("question_id") != round_question_id:
                # 树契约（codex SHOULD）：attack 轮只许关**本轮 Qn**——关别的问题再把本 Qn 置 inconclusive
                # 属状态破坏（对齐 M0 driver「不得关别的问题」判据）
                self._retry_reasoning_or_finish(
                    cyc, files, f"answer.question_id（{ans.get('question_id')}）≠ 本轮 Qn（{round_question_id}）"
                    "——不得关别的问题", question_id=round_question_id)
                return
        sel = files["selection.json"]
        try:
            with self.state.atomic() as conn:
                if self.state.cycle(cyc.cycle_id).status in ("done", "failed", "aborted"):
                    return
                qi = _qnum(round_question_id)
                qrow = conn.execute("SELECT status FROM question WHERE id=?", (qi,)).fetchone()
                if qrow is None:
                    raise RuntimeError(
                        f"cycle {cyc.cycle_id} 的 active question {round_question_id} 在 DB 不存在")
                if ans is not None and qrow[0] not in ("answered", "refuted", "dead_end"):
                    try:
                        self.close_gate.gate_close_question_in_txn(
                            conn, cycle_id=cyc.cycle_id, question_id=ans["question_id"],
                            verdict=ans["verdict"], evidence=ans["evidence"],
                            answer_md=ans["answer_md"])
                    except GateReject as error:
                        # Escape the outer atomic so every answer/tree write,
                        # including the gate rejection side effect, rolls back
                        # before a fresh model artifact is requested.
                        raise _ReasoningReject(
                            f"answer 语义被 gate 拒绝: {error}") from error
                elif ans is not None and not self._durable_reasoning_answer_matches(cyc, ans):
                    raise RuntimeError("终态 question 与持久 reasoning answer 不一致，拒绝静默续跑")

                if conn.execute("SELECT status FROM question WHERE id=?", (qi,)).fetchone()[0] == "active":
                    # 无 answer（或未关成）的攻坚轮：Qn 不得永卡 active——置 inconclusive（增 visit，§4.2.3
                    # 「阶段失败=轮正常收尾」口径，对齐 M0 driver；训练/评估失败路径由此收干净）
                    self.state.mark_inconclusive(round_question_id)
                try:
                    self.state.apply_tree_ops(
                        cyc.cycle_id, files.get("tree_ops.json", {"ops": []}).get("ops", []))
                except ValueError as e:
                    # apply_tree_ops 的 ValueError 是封闭 op/route/引用/guard 等产物语义拒；让自定义异常
                    # 逃出 atomic，保证之前的 mark_inconclusive/tree 半写一并回滚。SQLite 异常不在此捕获。
                    raise _ReasoningReject(f"tree_ops 语义被拒绝: {e}") from e
                persist_selection_safe(
                    self.state, cyc.cycle_id, sel, retry_on_invalid=True)
                self.state.mark_cycle_done(cyc.cycle_id)
        except _ReasoningReject as e:
            self._retry_reasoning_or_finish(
                cyc, files, str(e), question_id=round_question_id,
                selection_invalid=e.selection_invalid)

    def _finish_dependency_wait_reasoning(
            self, cyc, files: Dict[str, Any]) -> None:
        """Close an import-deferred cycle only after its Reasoning turn.

        A pending dependency is not negative evidence and therefore does not
        increment the question's inconclusive visit count.  The model's cycle
        summary and advisory selection are retained, while the core scheduler
        keeps the route in ``dependency_wait`` until the exact dependency is
        satisfied or blocked.
        """
        if files.get("answer.json") is not None:
            self._retry_reasoning_or_finish(
                cyc, files,
                "dependency_wait 尚无新证据，不得在本轮关闭 question",
                question_id=cyc.question_id)
            return
        tree_ops = files.get("tree_ops.json", {"ops": []}).get("ops", [])
        if tree_ops:
            self._retry_reasoning_or_finish(
                cyc, files,
                "dependency_wait Reasoning 只总结与决定等待，不得改写 question tree",
                question_id=cyc.question_id)
            return
        if cyc.question_id is None:
            raise RuntimeError(f"dependency_wait {cyc.cycle_id} 缺 active question")
        with self.state.atomic() as conn:
            current = self.state.cycle(cyc.cycle_id)
            if current.status in ("done", "failed", "aborted"):
                return
            self.state.release_question(cyc.question_id)
            conn.execute(
                "INSERT INTO decision(cycle_id,question_id,actor,type,payload_json) "
                "VALUES (?,?,'agent','dependency_wait_reasoning',?)",
                (_cnum(cyc.cycle_id), _qnum(cyc.question_id), json.dumps({
                    "protocol": "dependency-wait-reasoning-v1",
                    "requested_selection": files["selection.json"],
                    "disposition": "wait_for_registered_dependency",
                }, ensure_ascii=False, sort_keys=True)))
            # dependency_wait routing owns the next question; the ordinary
            # selection schema has no `wait` intent.  Keep an explicit internal
            # continuation marker that the Advancer ignores until deps resolve.
            conn.execute(
                "UPDATE cycle SET next_question_id=NULL,next_intent='attack' WHERE id=?",
                (_cnum(cyc.cycle_id),))
            self.state.mark_cycle_done(cyc.cycle_id)

    def _finish_reasoning_rejected(self, cyc, files: Dict[str, Any], reason: str,
                                   *, question_id: Optional[str] = None,
                                   selection_invalid: bool = False) -> None:
        """把已持久化的坏 reasoning 收敛为可审计、可重启的业务终态。

        decision、当前活跃题 inconclusive、回退 selection 与 cycle done 同一事务；终态二次核保证
        重入不重复记拒。产物性 selection 错误改用权威 DB 前沿；其他语义拒绝才安全停机。
        两者都不消费坏 selection 的 scores/local refs。
        """
        with self.state.atomic() as conn:
            self._finish_reasoning_rejected_body(
                conn, cyc, files, reason, question_id=question_id or cyc.question_id,
                selection_invalid=selection_invalid)

    def _finish_reasoning_rejected_body(self, conn, cyc, files: Dict[str, Any], reason: str,
                                        *, question_id: Optional[str],
                                        selection_invalid: bool = False) -> None:
        """Finish a rejected reasoning artifact in the caller's transaction.

        A genuine answer/tree semantic failure remains fail-closed after its
        bounded retries.  An invalid *selection* is different: it must not turn
        one bad routing suggestion into a global terminate.  In that case the
        current question is accounted as inconclusive and a legal frontier is
        selected using only authoritative DB state and the existing scheduling
        guards.
        """
        if self.state.cycle(cyc.cycle_id).status in ("done", "failed", "aborted"):
            return
        qi = _qnum(question_id) if question_id else None
        if qi is not None:
            qrow = conn.execute("SELECT status FROM question WHERE id=?", (qi,)).fetchone()
            if qrow is None:
                raise RuntimeError(
                    f"cycle {cyc.cycle_id} 的 active question {question_id} 在 DB 不存在")
            if qrow[0] == "active":
                self.state.mark_inconclusive(question_id)
        if selection_invalid:
            self.state.reject_unapplied_reprioritize(
                cyc.cycle_id,
                f"selection 无法持久化，reprioritize 未应用: {reason}")
        retries = conn.execute(
            "SELECT count(*) FROM decision WHERE cycle_id=? AND actor='orchestrator' "
            "AND type='reasoning_semantic_retry'",
            (_cnum(cyc.cycle_id),)).fetchone()[0]
        fallback_question_id: Optional[str] = None
        fallback_next_intent = "terminate"
        if selection_invalid:
            fallback_question_id, fallback_next_intent = self._legal_reasoning_fallback(
                conn, cyc, current_question_id=question_id)
        decision_type = "selection_invalid" if selection_invalid else "reasoning_rejected"
        conn.execute(
            "INSERT INTO decision(cycle_id,question_id,actor,type,payload_json) "
            "VALUES (?,?,'orchestrator',?,?)",
            (_cnum(cyc.cycle_id), qi, decision_type, json.dumps({
                "reason": reason, "question_id": question_id,
                "artifact_hash": _canon_hash(files),
                "requested_selection": {
                    "next_question_id": (files.get("selection.json") or {}).get("next_question_id"),
                    "next_intent": (files.get("selection.json") or {}).get("next_intent"),
                },
                "fallback_question_id": fallback_question_id,
                "fallback_next_intent": fallback_next_intent,
                "semantic_retries": retries},
                ensure_ascii=False, sort_keys=True)))
        self.state.persist_selection(
            cyc.cycle_id, Selection(next_question_id=fallback_question_id,
                                    next_intent=fallback_next_intent, scores=[]))
        self.state.mark_cycle_done(cyc.cycle_id)

    def _legal_reasoning_fallback(self, conn, cyc, *, current_question_id: Optional[str]) \
            -> tuple[Optional[str], str]:
        """Choose a deterministic, guard-checked frontier after a bad selection.

        Prefer a different question so a locally stuck question cannot monopolize
        the loop.  Within that order prefer attack; only use decompose when no
        attack frontier is legal.  Existing scores are merely a stable ordering
        hint; the rejected artifact's scores are deliberately not consumed.
        """
        cycle_row = conn.execute(
            "SELECT goal_id,goal_ver FROM cycle WHERE id=?", (_cnum(cyc.cycle_id),)
        ).fetchone()
        if cycle_row is None:
            raise RuntimeError(f"cycle {cyc.cycle_id} 不存在")
        current_qi = _qnum(current_question_id) if current_question_id else None
        rows = conn.execute(
            "SELECT q.id FROM question q "
            "WHERE q.goal_id=? AND q.goal_ver=? "
            "AND q.status IN ('open','inconclusive') "
            "AND NOT EXISTS (SELECT 1 FROM question_dep d "
            "                WHERE d.question_id=q.id AND d.status='pending') "
            "ORDER BY CASE WHEN q.id=? THEN 1 ELSE 0 END, "
            "CASE WHEN q.score IS NULL THEN 1 ELSE 0 END, q.score DESC, "
            "q.visit_count ASC, q.id ASC",
            (cycle_row[0], cycle_row[1], current_qi),
        ).fetchall()
        ordered = [f"q{int(row[0])}" for row in rows]
        for intent in ("attack", "decompose"):
            for qid in ordered:
                # is_schedulable is the authoritative status/dependency/visit
                # guard.  persist_selection validates it again in the same
                # transaction before writing the cycle pointer.
                if self.state.is_schedulable(qid, for_intent=intent):
                    return qid, intent
        return None, "terminate"
