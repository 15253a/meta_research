"""AttackStages —— attack 轮 idea/plan/bundle/reasoning 阶段推进（M4 CP5.4；Advancer 委托）。

**游标语义（§4.4.5）**：`cycle.status` = 最后**已提交**阶段（created→idea→plan→bundle→done）；kill-9 重启读
status 从下一阶段续跑。idea/plan 各为**单一事务**（阶段写 + phase_commit + status 推进同生共死）；bundle
**逐目标**推进（目标间不共事务），每目标进度从 build_target/run/evaluation/variant 状态**结构性重导出**
（崩溃从状态续，§4.2.5 bundle_cursor 语义的结构化实现）。

**phase_commit（§4.2.5）**：idea/plan 整阶段一行（target NULL）；bundle 每 target 一行（= 该 target 注册段
已提交）。同键异 hash → conflict 拒（staging 被改写后不得误判已提交）。

**两段提交（§4.2.5）**：(i) 执行事实随发生短事务（start/finish_run + checkpoint + run-owned log + 观测 ingest）；
(ii) 注册段 = gate_register_evaluation（eval+attempt(success)+metric_result 单事务）→ attempt-owned log/观测
补登 → gate_register_baseline/variant → gate_finish_build_target(complete) → phase_commit(target)。
(ii) 段为**可恢复的短事务序列**（每步幂等或可从状态跳过；测量注册的原子性由 gate_register_evaluation 单事务
保证）——整段合一事务需 WriteDaemon 可嵌套/组合式 gate，留 M5/M6 硬化，此处诚实分解 + 结构恢复。

**管线强制「先 ingest 观测再 complete」**：run log 与 attempt log 均入账 + parser ingest；complete 前显式核
attempt 已有当前口径 parser 观测（防「无据不疑」默认成绕过——suspect 谓词只对已 ingest 数据有效）。

**长操作零事务**（§6.13）：providers（Codex/judge）与 harness 子进程全部在事务外。

**契约分层（步⑧ CP8.2）**：plan 保持**抽象**（消费冻结 `plan.schema`——命令永不入 plan）；执行命令由 bundle
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
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import harness as H
from . import manifest as MF
from . import obs_parser as OP
from . import subject_manifest as SM
from .artifact_capability import (ArtifactCapabilityError, open_artifact,
                                  read_artifact_bytes)
from .budgeting import compute_budget
from .execution_sandbox import SandboxOutputError
from .gate_exec import ExecGate
from .gate_pool import PoolGate
from .gate_sqlite import GateReject
from .ids import cnum as _cnum, decode as _decode_id, qnum as _qnum, parse_positive_sqlite_int
from .import_search import ImportSearchError, validate_import_search_request
from .importer import DeferredImporter
from .interfaces import InvalidSelectionError, Selection
from .phase_commit import check_or_record
from .process_supervisor import ExecutionSupervisor, atomic_write_receipt, read_receipt

_TERMINAL_TARGET = ("complete", "skipped", "failed", "engineering_blocked")


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
    """Two independent answerability rounds were durably exhausted for this final draft."""

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


class _ReasoningReject(Exception):
    """已持久化 reasoning 产物的**语义**业务拒。

    只用于标记由外部产物直接决定的拒绝（answer 目标/证据引用、tree_ops 状态语义）；SQLite、IO、
    staging 损毁等基础设施异常不得转换成此类型。调用方会把它收敛为可恢复的 terminate 终态，避免
    persist-then-consume 的坏产物在每次重启时被确定性复读、永久楔死同一轮。
    """


_METRIC_VALUE_RE = re.compile(
    r"metric_value:\s*([1-9][0-9]*)@([1-9][0-9]*)="
    r"([+-]?(?:(?:[0-9]+(?:\.[0-9]*)?)|(?:\.[0-9]+))(?:[eE][+-]?[0-9]+)?)"
)


def _canon_hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def persist_selection_safe(state, cycle_id: str, sel: Dict[str, Any]) -> None:
    """持久化 reasoning 的下一步 selection——**Codex 选了不可调度的 (question, intent) 不楔死**（步⑧ CP8.8）。

    真实发现（部署首跑）：Codex 反复 attack 同一问题，visit 达 question_guard.max_inconclusive_per_question
    后该题对 attack 不可调度，但 Codex 仍选 `next=该题, intent=attack` → persist_selection 抛 ValueError →
    未捕获打死驱动循环；且 reasoning 产物已持久化（persist-then-consume），重启确定性重崩 = **永久楔死**。

    修法：任何**非法/不可调度**的 Codex selection（不可调度题 / 悬挂 id / 缺 intent / scores 引用不存在
    …均属 Codex 产物问题，非编排器 bug）→ 记 decision(selection_invalid) + 改持久 **terminate** 干净收尾
    （route 停机，durable；对齐 plan/bundle 的「Codex 产物站不住 → 业务收尾不楔死」全自动纪律）。
    编排器不代 Codex 重选题（那是研究决策，违「编排器从不推理」）；停机后运维/后续可续。
    **只兜 InvalidSelectionError**（persist_selection 判定的「Codex 路由产物非法」专用异常，codex SHOULD）：
    编排器内部状态/schema/DB 错误仍抛原生异常 fail loud，不被误吞成正常停机。缺 next_intent（Codex 未产
    该键）也归此类。"""
    try:
        if "next_intent" not in sel:
            raise InvalidSelectionError("selection 缺 next_intent（Codex 产物不完整）")
        state.persist_selection(cycle_id, Selection(
            next_question_id=sel.get("next_question_id"), next_intent=sel["next_intent"],
            scores=sel.get("scores", [])))
    except InvalidSelectionError as e:
        state.reject_unapplied_reprioritize(
            cycle_id, f"selection 无法持久化，reprioritize 未应用: {e}")
        state.daemon.conn.execute(          # atomic 内：daemon.conn == 外层事务连接（单写，随 atomic 提交/回滚）
            "INSERT INTO decision(cycle_id,actor,type,payload_json) VALUES (?,'orchestrator','selection_invalid',?)",
            (_cnum(cycle_id), json.dumps({"reason": str(e), "requested": {
                "next_question_id": sel.get("next_question_id"), "next_intent": sel.get("next_intent")}},
                ensure_ascii=False)))
        state.persist_selection(cycle_id, Selection(next_question_id=None, next_intent="terminate", scores=[]))


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
                 execution_sandbox=None):
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
            self._idea_stage(cyc)
            return "plan"
        if cyc.status == "idea":
            self._plan_stage(cyc)
            return ("done" if self.state.cycle(cyc.cycle_id).status
                    in ("done", "failed", "aborted") else "bundle")
        if cyc.status == "plan":
            self._bundle_stage(cyc)
            return "reasoning"
        if cyc.status == "bundle":
            self._reasoning_stage(cyc)
            return "done"
        raise ValueError(f"attack 轮不可推进的游标 status={cyc.status!r}")

    # ---------------------------------------------------------------- idea --
    def _idea_stage(self, cyc) -> None:
        """idea 阶段（§3.2）：候选全量入 IDEA 表（防重复造轮的关键边，含 failed）+ selected 标记。单一事务。
        **消费冻结 idea_set.schema**（步⑧ CP8.2）：content_md **机械合成**（schema 无 content_md 键——
        由 core_claim/mechanism/assumptions/MFE/audit_mapping 拼装）；audit_score 取该候选六维审计均值、
        status 由 selected_id / audit decision 派生（audit_scores 是独立顶层数组，按 candidate_id 关联）。"""
        pack = self.compiler.render(cycle_id=cyc.cycle_id, stage="idea")
        files = self.p["idea"](cyc, pack)
        iset = files["idea_set.json"]
        cands, selected = iset["candidates"], iset.get("selected_id")
        audits = {a["candidate_id"]: a for a in iset.get("audit_scores", [])}
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
                return                        # 已提交（kill-9 后重做路径）；status 同事务已推进过
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
                     json.dumps(audit, ensure_ascii=False, sort_keys=True) if audit else None, st))
            conn.execute("UPDATE cycle SET status='idea' WHERE id=?", (ci,))

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
            plan = self._plan_artifact(cyc)     # persist-then-consume（原子落盘、恢复复用；失败转 _PlanReject）
            self._validate_plan_schema(plan)    # 结构闸（防裸 KeyError 逃逸）：非 schema-conform → _PlanReject
            if "import_defer" in plan:
                self._commit_import_defer(cyc, plan)
                return
            targets = sorted(plan["targets"], key=lambda x: x["seq"])   # schema 保证 targets/seq 在场
            for t in targets:
                if t["target_kind"] not in ("build", "exec", "eval"):
                    raise _PlanReject(f"不支持的 plan target kind: {t['target_kind']}")
            if not targets:                     # 无 target（复用/聚合/idea 失败）：合法终态、零 target（非拒）
                self._commit_plan_terminal(cyc, plan, built=[], reject=None)
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
        """reference §4.2.5 的 dependency_wait 单事务收尾。

        ``selected_for_materialization + baseline(planned) + question_dep(pending) + phase_commit +
        route + active Qn release + cycle done`` 恰一事务；任一步失败整体回滚。本轮不建 build_target、
        不进 bundle/reasoning。候选/license 快照由 DeferredImporter 机械核验，模型不能直接指定 candidate id。
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
            self.state.release_question(cyc.question_id)
            self.state.mark_cycle_done(cyc.cycle_id)

    def _validate_plan_schema(self, plan: Dict[str, Any]) -> None:
        """plan.json 结构闸（编排器侧防御，不只靠 StageProvider——同 manifest 校验在 _obtain_manifest 侧）：
        非 schema-conform → _PlanReject（业务拒，不楔死）。schemas 未注入（老测试路径）则跳过。"""
        try:
            # jsonschema treats NaN/Infinity as Python numbers on some versions.  They are not JSON
            # values and would otherwise fail later while hashing/rendering the independent review,
            # turning one bad model artifact into a restart-stable poison cycle.
            json.dumps(plan, ensure_ascii=False, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise _PlanReject(f"plan.json 含非有限或不可编码 JSON 值：{error}") from error
        if self.schemas is None:
            return
        errs = [f"{e.json_path} {e.message}" for e in self.schemas.validator("plan").iter_errors(plan)]
        if errs:
            raise _PlanReject("plan.json 非 schema-conform: " + "; ".join(errs[:5]))

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
                if result["status"] != "pass":
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

        assert last_review is not None
        result = {
            "status": "exhausted", "round_no": max_rounds,
            "plan_hash": _canon_hash(plan), "decision_id": decision_id,
            "issues": last_review.get("issues", []),
        }
        self._write_json_atomic(review_result_path, result)
        self._write_json_atomic(art, plan)
        raise _PlanReviewReject(
            f"plan 可回答性评审 {max_rounds} 轮未通过：{last_review.get('issues', [])}", plan)

    def _plan_provider_with_import_search(
            self, cyc, pack, *, search_request_path: Path,
            search_requested: bool):
        """Run one plan call, optionally consume one discovery sidecar, then re-plan.

        The request is persisted before any network call.  Recovery therefore
        resumes the same request and lets ImportSearchService reconcile its
        receipt instead of asking a non-deterministic planner for a new query.
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

    def _read_plan_review_result(self, path: Path, plan: Dict[str, Any],
                                 cycle_id: str) -> Dict[str, Any]:
        if not path.exists():
            raise RuntimeError("plan.json 存在但 plan.review-result.json 缺失")
        try:
            result = json.loads(read_artifact_bytes(
                path, label="stage artifact").decode("utf-8"))
        except json.JSONDecodeError as error:
            raise RuntimeError("plan.review-result.json 损坏") from error
        if (not isinstance(result, dict) or result.get("plan_hash") != _canon_hash(plan)
                or result.get("status") not in ("pass", "exhausted")):
            raise RuntimeError("plan.review-result 与最终 plan 身份不一致")
        round_no = result.get("round_no")
        decision_id = result.get("decision_id")
        if isinstance(round_no, bool) or not isinstance(round_no, int) or round_no <= 0:
            raise RuntimeError("plan.review-result round_no 非法")
        existing = self._existing_plan_review(cycle_id, round_no, result["plan_hash"])
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
            claim = t.get("claim", {})
            if t["target_kind"] == "build":
                ck, slug = claim["canonical_key"], claim["slug"]
                if ck in seen_ck:
                    raise _PlanReject(f"plan 内 canonical_key 重复: {ck!r}（同轮多目标不得共占坑）")
                seen_ck.add(ck)
                occ = d.query_one("SELECT born_cycle, slug FROM baseline WHERE canonical_key=?", (ck,))
                if occ is not None and not (occ[0] == ci and occ[1] == slug):
                    raise _PlanReject(f"canonical_key 被他轮占（I5）: {ck!r}")   # 派生期拦下→claim 段不半途留孤儿
            elif t["target_kind"] == "exec":
                bref, vkey = claim["baseline_ref"], claim["variant_key"]
                brow = d.query_one("SELECT id, status FROM baseline WHERE canonical_key=?", (bref,))
                if brow is None or brow[1] != "legal":
                    raise _PlanReject(f"exec baseline_ref {bref!r} 未解析到 legal baseline"
                                      f"（{'缺失' if brow is None else brow[1]}——首攻新家族须 build）")
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
                    if (erow[1], erow[2]) != (pid, pver):
                        raise _PlanReject(
                            f"eval append e{eid} 协议 p{erow[1]}@{erow[2]} 与 plan p{pid}@{pver} 不一致")
                    vid, bid, eval_key = erow[0], erow[5], erow[3]
                    target_set_hash = d.query_one(
                        "SELECT target_set_hash FROM evaluation WHERE id=?", (eid,))[0]
                else:
                    bref, vkey = claim["baseline_ref"], claim["variant_key"]
                    vrow = d.query_one(
                        "SELECT v.id,v.baseline_id,v.status FROM variant v JOIN baseline b ON b.id=v.baseline_id "
                        "WHERE b.canonical_key=? AND v.variant_key=? AND b.status='legal'",
                        (bref, vkey))
                    if vrow is None or vrow[2] != "legal":
                        raise _PlanReject(
                            f"eval create 的 {bref}/{vkey} 未解析到 legal variant")
                    vid, bid, eid, eval_key = vrow[0], vrow[1], None, t["eval_key"]
                    existing_eval = d.query_one(
                        "SELECT id FROM evaluation WHERE variant_id=? AND protocol_id=? AND protocol_ver=?",
                        (vid, pid, pver))
                    if existing_eval is not None:
                        raise _PlanReject(
                            f"eval create 的格子 v{vid}/p{pid}@{pver} 已有 e{existing_eval[0]}——应走 append_attempt")
                    checkpoints = d.query(
                        "SELECT id,content_hash FROM checkpoint WHERE variant_id=? ORDER BY id", (vid,))
                    if len(checkpoints) != 1:
                        raise _PlanReject(
                            f"eval create 当前实现要求 legal variant 恰一可评 checkpoint，v{vid} 实收 {len(checkpoints)}")
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

    def _commit_plan_terminal(self, cyc, plan: Optional[Dict[str, Any]], *, built: List[tuple],
                              reject: Optional[str]) -> None:
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
    def _bundle_stage(self, cyc) -> None:
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
            if status == "engineering_blocked" or (
                    critical and status == "failed"):
                self._skip_after_critical_failure(cyc, bt_id)
                break
        with d.transaction() as conn:
            conn.execute("UPDATE cycle SET status='bundle' WHERE id=?", (ci,))

    def _skip_after_critical_failure(self, cyc, failed_target_id: int) -> None:
        """收敛 critical/engineering_blocked 早退；可在任意一个 skip 后崩溃并幂等续扫。"""
        skipped = self.gate.gate_skip_remaining_targets(failed_target_id=failed_target_id)
        for target_id in skipped:
            self._ensure_target_pc(cyc, target_id)

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
            files = self.p["bundle"](cyc, pack)
            try:
                if not isinstance(files, dict) or "execution_manifest.json" not in files:
                    raise MF.ManifestError(f"bundle provider 未产 execution_manifest.json（键: "
                                           f"{list(files) if isinstance(files, dict) else type(files)}）")
                manifest = files["execution_manifest.json"]
                self._check_manifest(manifest, slice_)
                actual_refs = MF.extract_manifest_asset_refs(manifest)
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
        except _BundleReject as e:
            if st() not in _TERMINAL_TARGET:      # 产物层业务拒 → 目标 failed（携 failure_kind）+ pc
                self.gate.gate_finish_build_target(build_target_id=bt_id, status="failed",
                                                   failure_kind=e.failure_kind)
            self._ensure_target_pc(cyc, bt_id)

    def _drive_target_inner(self, cyc, bt_id: int) -> None:
        g = self.gate
        d = self.state.daemon
        slice_ = self._slice(bt_id)
        st = lambda: d.query_one("SELECT status FROM build_target WHERE id=?", (bt_id,))[0]
        if st() in _TERMINAL_TARGET:
            self._ensure_target_pc(cyc, bt_id)   # 崩在 complete 与 pc 之间 → 补 pc（幂等）
            return
        staging = self.work / f"c{_cnum(cyc.cycle_id)}" / f"t{bt_id}"
        src_dir = staging / "src"                 # 代码物化目录（每目标唯一；净土物化，与 run/eval 产物分离）
        manifest, ledger, allowed_asset_refs, asset_identities = self._obtain_manifest(
            cyc, bt_id, slice_, src_dir)
        if st() == "pending":
            g.gate_start_build_target(build_target_id=bt_id)
        if slice_["target_kind"] == "eval":
            if st() == "running":
                self._run_eval_target(
                    cyc, bt_id, slice_, manifest, ledger, staging, src_dir,
                    allowed_asset_refs, asset_identities)
            self._ensure_target_pc(cyc, bt_id)
            return
        if st() == "building":                    # 真 smoke（manifest.commands.smoke 子进程）→ 过了才进 smoke 态
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
                    execution_sandbox=self.execution_sandbox)
                if sm is None:
                    self.owner_guard()             # external spawn 的最后一道 owner fence
                    sm = MF.run_manifest_command(
                        manifest, "smoke", staging_dir=str(smoke_dir), log_name=smoke_name,
                        src_dir=src_dir, work_root=self.work, policy=self.policy,
                        expected_source_hashes=ledger,
                        allowed_asset_refs=allowed_asset_refs,
                        expected_asset_identities=asset_identities,
                        execution_supervisor=self.execution_supervisor,
                        execution_context=smoke_context,
                        execution_sandbox=self.execution_sandbox)
            if sm["exit_code"] != 0:              # smoke 失败 → target 失败连坐（codex SHOULD：exit code 不得忽略）
                g.gate_finish_build_target(build_target_id=bt_id, status="failed", failure_kind="smoke")
                self._ensure_target_pc(cyc, bt_id)   # 终态早退**也**落 pc（codex 第2轮 BLOCKER：漏落致杀/不杀分裂）
                return
            g.gate_progress_build_target(build_target_id=bt_id, to="smoke")
        if st() == "smoke":                       # 代码适配评审（subject 编排器重算；judge replay-safe）
            code_sh = self._code_subject_hash(slice_, manifest, ledger, staging)
            self._judge_once(cyc.cycle_id, bt_id, "bundle_code_review", code_sh)
            if not g.review_passed(build_target_id=bt_id, review_kind="bundle_code_review",
                                   current_subject_hash=code_sh):
                # judge FAIL → 目标失败收尾（lockstep：import_worker 同修——直接闯 gate 会拒 → 重启复用同
                # fail 裁决 → 确定性重试死循环；修复重评的轮数语义 = M6 硬化）
                g.gate_finish_build_target(build_target_id=bt_id, status="failed", failure_kind="review_failed")
                self._ensure_target_pc(cyc, bt_id)
                return
            g.gate_progress_build_target(build_target_id=bt_id, to="running", current_subject_hash=code_sh)
        if st() == "running":
            self._run_and_register(cyc, bt_id, slice_, manifest, ledger, staging, src_dir,
                                   allowed_asset_refs, asset_identities)
        self._ensure_target_pc(cyc, bt_id)

    def _run_eval_target(self, cyc, bt_id: int, slice_, manifest, ledger,
                         staging: Path, src_dir: Path,
                         allowed_asset_refs, asset_identities) -> None:
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
        if len(checkpoints) != 1:
            raise RuntimeError(
                f"eval target {bt_id} 的 v{vid} checkpoint 集从 plan 后漂移（实收 {len(checkpoints)}）")
        checkpoint_id, checkpoint_key, checkpoint_path, checkpoint_hash = checkpoints[0]
        if H.file_sha256(checkpoint_path) != checkpoint_hash:
            raise RuntimeError(
                f"eval target {bt_id} checkpoint ck{checkpoint_id} 内容与 DB hash 不一致")

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
            g.gate_finish_build_target(
                build_target_id=bt_id, status="failed", failure_kind=target_failure)
            return
        if latest is not None and latest[1] == "success":
            eid, aid, attempt_no, attempt_purpose = erow[0], latest[0], latest[2], latest[3]
        elif latest is not None and latest[1] == "running":
            eid, aid, attempt_no, attempt_purpose = erow[0], latest[0], latest[2], latest[3]
        else:
            if latest is not None and latest[1] != "aborted":
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
                    execution_sandbox=self.execution_sandbox)
                if ev is None:
                    self.owner_guard()
                    ev = MF.run_manifest_command(
                        manifest, "eval", staging_dir=str(eval_dir), log_name="eval.log",
                        src_dir=src_dir, work_root=self.work, policy=self.policy,
                        ckpt_path=Path(checkpoint_path),
                        ckpt_content_hash=checkpoint_hash,
                        expected_source_hashes=ledger,
                        allowed_asset_refs=allowed_asset_refs,
                        expected_asset_identities=asset_identities,
                        execution_supervisor=self.execution_supervisor,
                        execution_context=eval_context,
                        execution_sandbox=self.execution_sandbox)
                eval_log = read_artifact_bytes(
                    ev["log_path"], expected_hash=ev["log_sha256"],
                    expected_size=ev["log_bytes"], label="eval log receipt")

            def finish_attempt_failure(failure_kind: str, target_failure: str) -> None:
                g.gate_finish_attempt(
                    attempt_id=aid, status="failed", failure_kind=failure_kind,
                    transcript_ref=ev.get("process_receipt_path"),
                    artifact_ref=f"sha256:{ev['log_sha256']}")
                if d.query_one("SELECT status FROM evaluation WHERE id=?", (eid,))[0] != "success":
                    g.gate_finish_evaluation(evaluation_id=eid)
                g.gate_finish_build_target(
                    build_target_id=bt_id, status="failed", failure_kind=target_failure)

            if ev["exit_code"] != 0:
                finish_attempt_failure("runtime", "runtime")
                return
            try:
                metrics = self._metrics_from_eval_log(
                    eval_log.decode("utf-8", errors="replace"))
            except _BundleReject as error:
                finish_attempt_failure(error.failure_kind, error.failure_kind)
                return
            result_subject = SM.subject_hash(SM.result_review_manifest(
                metrics_artifact_hash=_canon_hash(metrics),
                checkpoint_hashes={f"ck{checkpoint_id}:{checkpoint_key}": checkpoint_hash},
                run_log_hashes={ev["log_path"]: ev["log_sha256"]},
                parser_obs_hash=_canon_hash(OP.parse_log(
                    eval_log.decode("utf-8", errors="replace"), self.obs_policy))))
            self._judge_once(ci, bt_id, "bundle_result_review", result_subject)
            if not g.review_passed(
                    build_target_id=bt_id, review_kind="bundle_result_review",
                    current_subject_hash=result_subject):
                finish_attempt_failure("protocol_violation", "review_failed")
                return
            try:
                g.gate_register_evaluation(
                    cycle_id=ci, build_target_id=bt_id, purpose=attempt_purpose,
                    current_subject_hash=result_subject, metric_results=metrics,
                    attempt_id=aid, artifact_ref=f"sha256:{ev['log_sha256']}",
                    transcript_ref=ev.get("process_receipt_path"))
            except GateReject as error:
                finish_attempt_failure("protocol_violation", "protocol_violation")
                raise _BundleReject(
                    f"eval target 测量注册被拒: {error}",
                    failure_kind="protocol_violation") from error

        self._register_and_ingest_log(
            ci, eval_final, log_kind="eval", evaluation_attempt_id=aid)
        if not OP.suspect_attempt_has_current_obs(d.conn, aid, self.obs_policy):
            raise RuntimeError(
                f"eval target {bt_id} attempt {aid} 无当前口径 parser 观测")
        g.gate_finish_build_target(build_target_id=bt_id, status="complete")

    def _run_and_register(self, cyc, bt_id: int, slice_, manifest, ledger, staging: Path, src_dir: Path,
                          allowed_asset_refs, asset_identities) -> None:
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
        if run_row and run_row[1] == "success":
            rid = run_row[0]
        elif run_row and run_row[1] == "failed":
            if run_row[2] != "aborted":
                # startup reconciler 已把 timeout/nonzero 等确证失败收口；这里只补 target，
                # 不把一次耐久失败偷偷重跑。
                g.gate_finish_build_target(
                    build_target_id=bt_id, status="failed",
                    failure_kind=run_row[2] or "runtime")
                return
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
                    execution_sandbox=self.execution_sandbox)
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
            train_result = MF.run_manifest_command(
                manifest, "train", staging_dir=str(staging / f"run{rid}"),
                log_name="train.log", src_dir=src_dir, work_root=self.work,
                policy=self.policy, expected_source_hashes=ledger,
                allowed_asset_refs=allowed_asset_refs,
                expected_asset_identities=asset_identities,
                execution_supervisor=self.execution_supervisor,
                execution_context=train_context,
                execution_sandbox=self.execution_sandbox)

        if d.query_one("SELECT status FROM run WHERE id=?", (rid,))[0] != "success":
            if train_result is None:
                raise RuntimeError(f"run {rid} 非 success 且无可恢复执行结果")
            if train_result["exit_code"] != 0:
                g.gate_finish_run(run_id=rid, status="failed", failure_kind="runtime")
                g.gate_finish_build_target(build_target_id=bt_id, status="failed", failure_kind="runtime")
                return                            # 训练失败入账不入树（§7.1 判例④；答题侧自然无证据）
            ck_path = MF.checkpoint_dest(manifest, staging / f"run{rid}")   # 围栏解析进 run 目录内
            try:
                with open_artifact(ck_path, label="run checkpoint publication") as checkpoint_cap:
                    ck_hash = checkpoint_cap.identity.content_hash.removeprefix("sha256:")
                    with d.transaction() as conn:  # checkpoint 登记（run 产物；finish_run success 的前置）
                        existing = conn.execute(
                            "SELECT variant_id,ckpt_key,path,content_hash FROM checkpoint "
                            "WHERE produced_by_run=?", (rid,)).fetchone()
                        expected = (vid, f"final-r{rid}", str(ck_path), ck_hash)
                        if existing is None:
                            conn.execute(
                                "INSERT INTO checkpoint(variant_id,ckpt_key,path,content_hash,hash_alg,produced_by_run) "
                                "VALUES (?,?,?,?,'sha256',?)", (*expected, rid))
                        elif tuple(existing) != expected:
                            raise RuntimeError(
                                f"run {rid} checkpoint durable identity 与 staging 不一致")
                    checkpoint_cap.verify_unchanged()
                    checkpoint_cap.verify_path_binding()
                    g.gate_finish_run(run_id=rid, status="success")
                    checkpoint_cap.verify_unchanged()
                    checkpoint_cap.verify_path_binding()
            except ArtifactCapabilityError as error:
                raise RuntimeError(f"run {rid} checkpoint publication 身份漂移") from error
        # train log 入账 + 观测 ingest：**无条件、幂等**（不藏在 fresh 分支——崩在 finish_run 与 ingest 之间时，
        # 复用 run 的续跑须从 staging 存活文件补登，否则杀 vs 不杀终库不一致，内审 SHOULD）
        self._register_and_ingest_log(ci, staging / f"run{rid}" / "train.log", log_kind="train", run_id=rid)
        # —— (ii) 出厂评估 + 注册段 ——
        erow = d.query_one(
            "SELECT id,status,canonical_attempt_id FROM evaluation WHERE build_target_id=?", (bt_id,))
        eval_final = None
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
                    # 失败 attempt 已是耐久执行事实；若崩在 target 收口前，补同一失败，绝不偷偷重跑。
                    target_failure = ("protocol_violation"
                                      if latest[4] in ("protocol_violation", "metric_missing",
                                                       "data_invalid", "artifact_invalid")
                                      else latest[4] or "runtime")
                    g.gate_finish_build_target(
                        build_target_id=bt_id, status="failed", failure_kind=target_failure)
                    return
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
                    execution_sandbox=self.execution_sandbox)
                if ev is None:
                    self.owner_guard()
                    checkpoint_identity = d.query_one(
                        "SELECT path,content_hash FROM checkpoint "
                        "WHERE produced_by_run=?", (rid,))
                    if checkpoint_identity is None:
                        raise RuntimeError(f"run {rid} 缺 checkpoint identity")
                    ev = MF.run_manifest_command(
                        manifest, "eval", staging_dir=str(eval_dir),
                        log_name="eval.log", src_dir=src_dir, work_root=self.work,
                        policy=self.policy,
                        ckpt_path=Path(checkpoint_identity[0]),
                        ckpt_content_hash=checkpoint_identity[1],
                        expected_source_hashes=ledger,
                        allowed_asset_refs=allowed_asset_refs,
                        expected_asset_identities=asset_identities,
                        execution_supervisor=self.execution_supervisor,
                        execution_context=eval_context,
                        execution_sandbox=self.execution_sandbox)
                eval_log = read_artifact_bytes(
                    ev["log_path"], expected_hash=ev["log_sha256"],
                    expected_size=ev["log_bytes"], label="eval log receipt")
            if ev["exit_code"] != 0:              # fresh 与 resume 同一判定点（评估进程失败 → target failed）
                g.gate_finish_attempt(
                    attempt_id=aid, status="failed", failure_kind="runtime",
                    transcript_ref=ev.get("process_receipt_path"),
                    artifact_ref=f"sha256:{ev['log_sha256']}")
                g.gate_finish_evaluation(evaluation_id=eid)
                g.gate_finish_build_target(build_target_id=bt_id, status="failed", failure_kind="runtime")
                return
            try:
                metrics = self._metrics_from_eval_log(eval_log.decode("utf-8", errors="replace"))
            except _BundleReject:
                g.gate_finish_attempt(
                    attempt_id=aid, status="failed", failure_kind="protocol_violation",
                    transcript_ref=ev.get("process_receipt_path"),
                    artifact_ref=f"sha256:{ev['log_sha256']}")
                g.gate_finish_evaluation(evaluation_id=eid)
                raise
            res_sh = self._result_subject_hash(bt_id, slice_, ledger, rid, metrics, ev)
            self._judge_once(ci, bt_id, "bundle_result_review", res_sh)
            if not g.review_passed(build_target_id=bt_id, review_kind="bundle_result_review",
                                   current_subject_hash=res_sh):
                # 结果评审 FAIL → review_failed：run(success)+checkpoint 保留（训练事实），测量整包不注册
                # （§4.2.5：第(ii)段不发生）——lockstep：import_worker 同修
                g.gate_finish_attempt(
                    attempt_id=aid, status="failed", failure_kind="protocol_violation",
                    transcript_ref=ev.get("process_receipt_path"),
                    artifact_ref=f"sha256:{ev['log_sha256']}")
                g.gate_finish_evaluation(evaluation_id=eid)
                g.gate_finish_build_target(build_target_id=bt_id, status="failed", failure_kind="review_failed")
                return
            try:
                reg = self.gate.gate_register_evaluation(
                    cycle_id=ci, build_target_id=bt_id, purpose=attempt_purpose, current_subject_hash=res_sh,
                    metric_results=metrics, attempt_id=aid,
                    artifact_ref=f"sha256:{ev['log_sha256']}",
                    transcript_ref=ev.get("process_receipt_path"))
            except GateReject as e:
                # **只在此调用点**把注册闸拒转业务失败（codex 第2轮 BLOCKER 收窄）：此处的拒 = 评估测量包
                # 不满足协议/required 契约（Codex eval 产物层问题）→ 目标 failed(protocol_violation)、不楔死。
                # 其余 gate（start/progress/finish/register_baseline）的拒仍 fail loud。
                g.gate_finish_attempt(
                    attempt_id=aid, status="failed", failure_kind="protocol_violation",
                    transcript_ref=ev.get("process_receipt_path"),
                    artifact_ref=f"sha256:{ev['log_sha256']}")
                g.gate_finish_evaluation(evaluation_id=eid)
                raise _BundleReject(f"测量注册被拒: {e}", failure_kind="protocol_violation") from e
            eid, aid = reg["evaluation_id"], reg["attempt_id"]
        else:
            eid, aid = erow[0], erow[2]
            attempt_no = d.query_one(
                "SELECT attempt_no FROM evaluation_attempt WHERE id=?", (aid,))[0]
            eval_final = (staging / f"eval{rid}" / "eval.log" if attempt_no == 1 else
                          staging / f"eval{rid}" / f"retry-a{aid}" / "eval.log")
        # attempt-owned eval log 补登 + 观测 ingest（§4.2.5(ii)）：**无条件、幂等、从 staging 存活文件重导出**——
        # 崩在 register_evaluation 与 ingest 之间时，resume 走 else 分支若不补登，下方强制核将永远 raise、
        # target 永卡 running（内审 BLOCKER 实证复现：不可恢复楔死）。register/ingest 均幂等，重放零害。
        self._register_and_ingest_log(ci, eval_final, log_kind="eval",
                                      evaluation_attempt_id=aid)
        # 管线强制：complete 前 attempt 须已有**当前口径** parser 观测（否则 suspect 无据可依成绕过）。
        # aid 由 register_evaluation 单事务保证非空（eval 存在 ⟹ canonical 已封）——此判为防御。
        if aid is None or OP.suspect_attempt_has_current_obs(d.conn, aid, self.obs_policy) is False:
            raise RuntimeError(f"bundle 管线约束：attempt {aid} 无当前口径 parser 观测且 staging eval.log 不可得"
                               "——须先 ingest 再 complete（staging 丢失属数据损毁，须人工介入）")
        if d.query_one("SELECT status FROM variant WHERE id=?", (vid,))[0] != "legal":
            res_sh2 = d.query_one(   # 复用注册时的 result_review（其 DECISION 已在库；重算同 hash）
                "SELECT json_extract(payload_json,'$.subject_hash') FROM decision WHERE actor='judge' "
                "AND type='bundle_result_review' AND json_extract(payload_json,'$.build_target_id')=? "
                "ORDER BY id DESC LIMIT 1", (bt_id,))[0]
            if slice_["target_kind"] == "exec":
                # exec：只把本变体入池（baseline 已 legal，身份不动）——register_variant（非 register_baseline）
                self.gate.gate_register_variant(
                    variant_id=vid, build_target_id=bt_id, evaluation_id=eid,
                    cycle_id=ci, current_subject_hash=res_sh2, run_id=rid)
            else:
                # build：终版身份 = bundle 产的 identity.md 全文（替换 plan 期占位草稿）；复现 = manifest.repro_cmd_md
                identity_doc = read_artifact_bytes(
                    src_dir / MF.IDENTITY_FILE,
                    expected_hash=ledger[MF.IDENTITY_FILE],
                    label="baseline identity artifact").decode("utf-8")
                self.gate.gate_register_baseline(
                    baseline_id=bid, variant_id=vid, build_target_id=bt_id, evaluation_id=eid,
                    cycle_id=ci, current_subject_hash=res_sh2,
                    identity_doc=identity_doc, repro_cmd=manifest["repro_cmd_md"], run_id=rid)
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
        ckrow = self.state.daemon.query_one(
            "SELECT ckpt_key, content_hash FROM checkpoint WHERE produced_by_run=?", (rid,))
        return SM.subject_hash(SM.result_review_manifest(
            metrics_artifact_hash=_canon_hash(metrics), checkpoint_hashes={ckrow[0]: ckrow[1]},
            run_log_hashes={ev["log_path"]: ev["log_sha256"]},
            parser_obs_hash=_canon_hash(OP.parse_log(
                read_artifact_bytes(
                    ev["log_path"], expected_hash=ev["log_sha256"],
                    expected_size=ev["log_bytes"],
                    label="result-review eval log").decode("utf-8"),
                self.obs_policy)),
            identity_draft_hash=ledger[MF.IDENTITY_FILE]))

    @staticmethod
    def _metrics_from_eval_log(text: str) -> List[Dict[str, Any]]:
        """评估产物口径（toy 最小）：eval log 每行 `metric_value: <mid>@<mver>=<float>` → aggregate metric_result。
        <mid>/<mver> = 编排器派生的 int 协议绑定（bundle pack 给 Codex，eval 命令照打印）。
        真评估产物规范 artifact（fold+aggregate 文件）= M6 硬化；此处值真来自真评估子进程输出。

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
            key = (mid, mver)
            if key in seen:
                raise _BundleReject(
                    f"eval.log 第 {line_no} 行重复 metric_value 绑定: {mid}@{mver}",
                    failure_kind="protocol_violation")
            try:
                value = float(match.group(3))
            except (ValueError, OverflowError) as e:
                raise _BundleReject(
                    f"eval.log 第 {line_no} 行 metric_value 数值非法: {match.group(3)!r}",
                    failure_kind="protocol_violation") from e
            if not math.isfinite(value):
                raise _BundleReject(
                    f"eval.log 第 {line_no} 行 metric_value 非有限值: {match.group(3)!r}",
                    failure_kind="protocol_violation")
            seen.add(key)
            out.append({"metric_id": mid, "metric_ver": mver, "value": value})
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
    def _reasoning_stage(self, cyc) -> None:
        """attack 轮收尾：answer/evidence→tree_ops→selection→done 单事务。
        **产物先持久化再消费**（codex SHOULD）：reasoning files 先原子落 staging（tmp→replace），resume 时
        复用持久产物、不重调 provider。旧版本若曾崩在独立 Gate 提交后，只有 durable answer 的全部
        身份/正文/证据与持久产物逐项一致时才允许恢复；新路径不存在 close 与轮末提交之间的窗口。

        schema 合法但语义非法的外部产物不能成为 poison pill：answer 的目标/证据引用被拒，或 tree_ops
        被 StateStore 以 ValueError 拒绝时，tree/selection 原子批整体回滚，再以 reasoning_rejected +
        terminate 原子收尾。后续 tree/selection 失败会连同本轮新 answer 一起回滚，再提交确定性失败收尾。
        SQLite/IO/GateInvariantError/RuntimeError 等内部或损毁错误仍 fail loud。"""
        art = self.work / f"c{_cnum(cyc.cycle_id)}" / "reasoning.json"
        if art.exists():
            files = json.loads(read_artifact_bytes(
                art, label="persisted reasoning artifact").decode("utf-8"))
        else:
            pack = self.compiler.render(cycle_id=cyc.cycle_id, stage="reasoning")
            files = self.p["reasoning"](cyc, pack)
            art.parent.mkdir(parents=True, exist_ok=True)
            tmp = art.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(files, ensure_ascii=False, sort_keys=True), encoding="utf-8")
            tmp.replace(art)
        if "selection.json" not in files:
            self._finish_reasoning_rejected(cyc, files, "reasoning 必产 selection.json")
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
                self._finish_reasoning_rejected(
                    cyc, files, f"answer.question_id（{ans.get('question_id')}）≠ 本轮 Qn（{round_question_id}）"
                    "——不得关别的问题")
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
                        # Gate 的 SAVEPOINT 已清掉 answer 半写，reject DECISION 留在外层事务；
                        # 与 deterministic fallback 一起正常提交。
                        self._finish_reasoning_rejected_body(
                            conn, cyc, files, f"answer 语义被 gate 拒绝: {error}",
                            question_id=round_question_id)
                        return
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
                persist_selection_safe(self.state, cyc.cycle_id, sel)
                self.state.mark_cycle_done(cyc.cycle_id)
        except _ReasoningReject as e:
            self._finish_reasoning_rejected(
                cyc, files, str(e), question_id=round_question_id)

    def _finish_reasoning_rejected(self, cyc, files: Dict[str, Any], reason: str,
                                   *, question_id: Optional[str] = None) -> None:
        """把已持久化的坏 reasoning 收敛为可审计、可重启的业务终态。

        decision、当前活跃题 inconclusive、terminate selection 与 cycle done 同一事务；终态二次核保证重入
        不重复记拒。这里使用编排器自产的固定 Selection，不消费坏 selection 的 scores/local refs。
        """
        with self.state.atomic() as conn:
            self._finish_reasoning_rejected_body(
                conn, cyc, files, reason, question_id=question_id or cyc.question_id)

    def _finish_reasoning_rejected_body(self, conn, cyc, files: Dict[str, Any], reason: str,
                                        *, question_id: Optional[str]) -> None:
        """Failure terminalization body; caller already owns the reasoning atomic transaction."""
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
        conn.execute(
            "INSERT INTO decision(cycle_id,question_id,actor,type,payload_json) "
            "VALUES (?,?,'orchestrator','reasoning_rejected',?)",
            (_cnum(cyc.cycle_id), qi, json.dumps({
                "reason": reason, "question_id": question_id,
                "artifact_hash": _canon_hash(files), "fallback_next_intent": "terminate"},
                ensure_ascii=False, sort_keys=True)))
        self.state.persist_selection(
            cyc.cycle_id, Selection(next_question_id=None, next_intent="terminate", scores=[]))
        self.state.mark_cycle_done(cyc.cycle_id)
