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
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import harness as H
from . import manifest as MF
from . import obs_parser as OP
from . import subject_manifest as SM
from .gate_exec import ExecGate
from .gate_pool import PoolGate
from .gate_sqlite import GateReject
from .ids import cnum as _cnum
from .interfaces import InvalidSelectionError, Selection
from .phase_commit import check_or_record

_TERMINAL_TARGET = ("complete", "skipped", "failed", "engineering_blocked")


class _PlanReject(Exception):
    """plan 派生期的业务拒（非法/不可满足的抽象 plan）——转 decision(plan_rejected)+零 target 终态，
    不 raise 到 advance（否则轮永不推进 = 全自动死循环）。与 GateReject 一并在 _plan_stage 捕获。"""


class _BundleReject(Exception):
    """bundle 阶段的业务拒（Codex 产物层面站不住）——转目标 failed(failure_kind)+pc，不楔死。两类来源：
    ① fresh manifest/信封非法（artifact_invalid；resume 的已物化 manifest 校验不过 = 数据损毁，走
    ManifestError fail loud）；② gate_register_evaluation 拒（protocol_violation：测量包不满足协议/required
    契约——**只在该调用点显式转换**，其余 GateReject[状态机/库损毁类]一律 fail loud 上抛，codex 第2轮 BLOCKER）。"""

    def __init__(self, msg: str, failure_kind: str = "artifact_invalid"):
        super().__init__(msg)
        self.failure_kind = failure_kind


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
                 obs_policy: Dict[str, Any], work_root: str, schemas=None, policy: Optional[Dict[str, Any]] = None):
        """state=SQLiteStateStore；compiler=SqliteCompiler；pool_gate=PoolGate(含 ExecGate 全家)；
        close_gate=SqliteGate（parser_suspect 已接真）；providers 见模块注释；work_root=staging 根目录。
        schemas=SchemaSet（步⑧：manifest 校验执法在编排器侧，不只靠 StageProvider）；
        policy=policy.yaml dict（manifest 命令围栏 execution 节；缺省从既有 obs_policy 无法取，须显式传）。"""
        self.state = state
        self.compiler = compiler
        self.gate: PoolGate = pool_gate
        self.close_gate = close_gate
        self.p = providers
        self.obs_policy = obs_policy
        self.work = Path(work_root)
        self.schemas = schemas
        self.policy = policy

    # ---------------------------------------------------------------- 调度 --
    def advance_stage(self, cyc) -> str:
        """按 cycle.status 游标推进一格；返回下一 stage 或 'done'。"""
        if cyc.status == "created":
            self._idea_stage(cyc)
            return "plan"
        if cyc.status == "idea":
            self._plan_stage(cyc)
            return "bundle"
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
                # schema 允许（targets 必空）但 CP8.6 未接线（DeferredImporter+dependency_wait）——静默当
                # 空 plan 会**丢外部导入意图**且无痕；显式业务拒并把 defer 对象**整体嵌入拒因**（codex
                # SHOULD：decision 是恢复/检索面，只留 reason 字符串不够——意图全文随 payload 可查，
                # 持久化的 plan.json 是制品级备份）
                raise _PlanReject("plan 含 import_defer——外部导入延迟决定 CP8.6 接线，本轮拒收；defer="
                                  + json.dumps(plan["import_defer"], ensure_ascii=False, sort_keys=True))
            targets = sorted(plan["targets"], key=lambda x: x["seq"])   # schema 保证 targets/seq 在场
            for t in targets:                   # eval/import target kind = 后续检查点（graceful 拒、不楔死）
                if t["target_kind"] not in ("build", "exec"):
                    raise _PlanReject(f"暂只支持 build/exec 目标（eval/import 后续接线）：{t['target_kind']}")
            if not targets:                     # 无 target（复用/聚合/idea 失败）：合法终态、零 target（非拒）
                self._commit_plan_terminal(cyc, plan, built=[], reject=None)
                return
            derived = self._derive_plan(ci, plan, targets)      # 纯读派生（build/exec 占坑身份前置判）
            self._register_protocol(cyc.cycle_id, derived)      # gate_new_protocol（幂等跳过）
            claims = self._claim_targets(ci, cyc.cycle_id, derived)   # build→claim_baseline / exec→claim_variant
        except (_PlanReject, GateReject) as e:                  # 非法 plan / gate 拒 → 业务拒收尾（不楔死）
            self._commit_plan_terminal(cyc, plan, built=[], reject=str(e))
            return
        self._commit_plan_terminal(cyc, plan, built=[(d, claims[d["target_key"]]) for d in derived["targets"]],
                                   reject=None)

    def _validate_plan_schema(self, plan: Dict[str, Any]) -> None:
        """plan.json 结构闸（编排器侧防御，不只靠 StageProvider——同 manifest 校验在 _obtain_manifest 侧）：
        非 schema-conform → _PlanReject（业务拒，不楔死）。schemas 未注入（老测试路径）则跳过。"""
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
        art = self.work / f"c{_cnum(cyc.cycle_id)}" / "plan.json"
        if art.exists():
            try:
                return json.loads(art.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                raise _PlanReject(f"持久 plan.json 解析失败（staging 损坏？）：{e}") from e
        pack = self.compiler.render(cycle_id=cyc.cycle_id, stage="plan")
        files = self.p["plan"](cyc, pack)
        if not isinstance(files, dict) or "plan.json" not in files:
            raise _PlanReject(f"plan provider 未产 plan.json（返回键: {list(files) if isinstance(files, dict) else type(files)}）")
        plan = files["plan.json"]
        art.parent.mkdir(parents=True, exist_ok=True)
        tmp = art.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(plan, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        tmp.replace(art)
        return plan

    def _derive_plan(self, ci: int, plan: Dict[str, Any], targets: List[Dict[str, Any]]) -> Dict[str, Any]:
        """纯读派生（编排器机械翻译，不推理；确定性 = 崩溃重放同结果）：抽象 plan → protocol int id / metric
        string→int 映射 / 每目标 resolved 切片（冻结 target + 绑定四件）+ required int 对 + identity 草稿。
        全部前置判失败一律 → _PlanReject（业务拒，不 raise 死循环）：I1 scope 冲突 / 复用协议缺 metric 绑定 /
        target_key·seq·metric_id 重复 / required 悬挂引用 / canonical_key 冲突（codex BLOCKER×3）。"""
        d = self.state.daemon
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
            else:   # exec
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
        dts = []
        for t in targets:
            claim = t.get("claim", {})
            tk, kind = t["target_key"], t["target_kind"]
            id_anchor = claim.get("canonical_key") if kind == "build" else \
                f"{claim.get('baseline_ref')}/{claim.get('variant_key')}"
            slice_ = dict(t)                    # 冻结 target 原样 + 绑定四件（plan_ref 权威；bundle manifest 交叉核锚）
            slice_.update({"protocol_id": pid, "protocol_ver": pver, "eval_key": tk,
                           "target_set_hash": _canon_hash({"factory_of": {"cycle": ci, "target_key": tk,
                                                                          "id_anchor": id_anchor}})})
            dts.append({"target_key": tk, "kind": kind, "seq": t["seq"], "slice": slice_,
                        "required": req_by_tk.get(tk, []), "identity_md": _synth_identity_md(t),
                        "claim": claim})
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
                else:   # exec：gate_claim_variant 自建 exec build_target
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
                if claim_info["build_target_id"] is None:       # build：终局 INSERT build_target
                    bt = conn.execute("INSERT INTO build_target(cycle_id,question_id,target_kind,seq,status,"
                                      "baseline_id,variant_id,eval_key,plan_ref) VALUES (?,?,'build',?,'pending',?,?,?,?)",
                                      (ci, qi, dt["seq"], claim_info["baseline_id"], claim_info["variant_id"],
                                       dt["slice"]["eval_key"], slice_json)).lastrowid
                else:                                        # exec：gate_claim_variant 已建 bt → 补 plan_ref/eval_key
                    bt = claim_info["build_target_id"]
                    conn.execute("UPDATE build_target SET eval_key=?, plan_ref=? WHERE id=?",
                                 (dt["slice"]["eval_key"], slice_json, bt))
                for (mid, mver) in dt["required"]:
                    conn.execute("INSERT INTO build_target_required_metric(build_target_id,metric_id,metric_ver) "
                                 "VALUES (?,?,?)", (bt, mid, mver))
            conn.execute("UPDATE cycle SET status='plan' WHERE id=?", (ci,))

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
            self._drive_target(cyc, bt_id)
        with d.transaction() as conn:
            conn.execute("UPDATE cycle SET status='bundle' WHERE id=?", (ci,))

    def _slice(self, bt_id: int) -> Dict[str, Any]:
        """resolved plan 切片（plan_ref）：冻结 target 原样 + 编排器派生绑定四件（protocol_id/protocol_ver/
        eval_key/target_set_hash）。bundle manifest 交叉核的锚。"""
        row = self.state.daemon.query_one("SELECT plan_ref FROM build_target WHERE id=?", (bt_id,))
        return json.loads(row[0])

    def _obtain_manifest(self, cyc, bt_id: int, slice_: Dict[str, Any], src_dir: Path) -> tuple:
        """取本目标的 execution_manifest（persist-then-consume）：src 未物化 → 调 bundle provider 产信封
        → 校验 + 交叉核 + 净土物化；已物化 → 复用。返回 (manifest, ledger)。

        **fresh 与 resume 同校验口径**（codex SHOULD-1）：两路径都 validate_manifest + cross_check(切片)。
        **失败分流**（codex SHOULD-2）：
        - fresh 路径（Codex 刚产的 manifest 非法/信封缺件）→ **_BundleReject**（业务拒→目标 failed，不楔死）；
        - resume 路径（已物化的 manifest 竟校验不过 = 已入库产物损坏，而非 Codex 出错）→ ManifestError 上抛
          fail loud（同 staged_hashes 篡改）——我们只物化校验通过的 manifest，故此路径校验失败 = 数据损毁。"""
        if self.policy is None:
            raise RuntimeError("AttackStages 须注入 policy（manifest 命令围栏 execution 节）")
        ledger = MF.staged_hashes(src_dir)        # 篡改自查（损毁→ManifestError fail loud）
        if ledger is None:                        # fresh：Codex 出错归业务拒
            pack = self.compiler.render(cycle_id=cyc.cycle_id, stage="bundle", target_id=str(bt_id))
            files = self.p["bundle"](cyc, pack)
            try:
                if not isinstance(files, dict) or "execution_manifest.json" not in files:
                    raise MF.ManifestError(f"bundle provider 未产 execution_manifest.json（键: "
                                           f"{list(files) if isinstance(files, dict) else type(files)}）")
                manifest = files["execution_manifest.json"]
                self._check_manifest(manifest, slice_)
                ledger = MF.stage_bundle_files(files, manifest, src_dir)
            except MF.ManifestError as e:
                raise _BundleReject(str(e)) from e
            return manifest, ledger
        manifest = json.loads((src_dir / MF.MANIFEST_FILE).read_text(encoding="utf-8"))
        self._check_manifest(manifest, slice_)    # resume 再校验（损坏→ManifestError 上抛，不吞）
        return manifest, ledger

    def _check_manifest(self, manifest: Dict[str, Any], slice_: Dict[str, Any]) -> None:
        """manifest 结构 + 交叉核 + kind 支持（fresh/resume 共用）。非 build kind → ManifestError（fresh 侧
        转 _BundleReject 业务拒；CP8.6 接 exec/eval）。"""
        if self.schemas is not None:
            MF.validate_manifest(self.schemas, manifest)
        MF.cross_check(manifest, slice_)          # 防「manifest 自立目标/换协议/改配置」
        if manifest["target_ref"]["target_kind"] not in ("build", "exec"):
            raise MF.ManifestError(f"bundle 只驱动 build/exec 目标（eval 后续接线）：{manifest['target_ref']['target_kind']}")

    def _drive_target(self, cyc, bt_id: int) -> None:
        """单目标推进（可重入）：按当前状态从断点续。执行命令由 manifest 承载（步⑧）。
        **契约违规不楔死、损毁必楔**（codex 第2轮 BLOCKER 收窄）：只捕 _BundleReject（Codex 产物层业务拒：
        非法 manifest = artifact_invalid / 测量包违约 = protocol_violation，见 _BundleReject 注）→ 目标
        failed(failure_kind) + 落 pc、续下一目标。**GateReject 不在此捕**——状态机/库损毁类拒绝（start/
        progress/finish/register_baseline）必须 fail loud，误终态化会把恢复/篡改问题永久掩埋。"""
        st = lambda: self.state.daemon.query_one("SELECT status FROM build_target WHERE id=?", (bt_id,))[0]
        try:
            self._drive_target_inner(cyc, bt_id)
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
        manifest, ledger = self._obtain_manifest(cyc, bt_id, slice_, src_dir)
        if st() == "pending":
            g.gate_start_build_target(build_target_id=bt_id)
        if st() == "building":                    # 真 smoke（manifest.commands.smoke 子进程）→ 过了才进 smoke 态
            sm = MF.run_manifest_command(manifest, "smoke", staging_dir=str(staging / "smoke"),
                                         log_name=f"smoke-{self._next_serial(staging, 'smoke')}.log",
                                         src_dir=src_dir, work_root=self.work, policy=self.policy)
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
            self._run_and_register(cyc, bt_id, slice_, manifest, ledger, staging, src_dir)
        self._ensure_target_pc(cyc, bt_id)

    def _run_and_register(self, cyc, bt_id: int, slice_, manifest, ledger, staging: Path, src_dir: Path) -> None:
        """phase (i) 执行事实 + phase (ii) 注册段（结构可恢复的短事务序列）。命令由 manifest 驱动（步⑧）。
        ⚠️ 与 import_worker._run_and_register_import **同构**（恢复缝隙修复须双向同步；共享骨架=M6 硬化）。"""
        g, d = self.gate, self.state.daemon
        ci = cyc.cycle_id
        vid = d.query_one("SELECT variant_id FROM build_target WHERE id=?", (bt_id,))[0]
        bid = d.query_one("SELECT baseline_id FROM build_target WHERE id=?", (bt_id,))[0]
        env_hash = manifest["env_hash"]
        # —— (i) 训练 run（结构续：残留 running run 先 abort；成功 run 直接复用）——
        run_row = d.query_one("SELECT id,status FROM run WHERE build_target_id=? ORDER BY id DESC", (bt_id,))
        if run_row and run_row[1] == "running":   # 崩溃残留：终结后重跑（run append-only，不复用半途 run）
            g.gate_finish_run(run_id=run_row[0], status="failed", failure_kind="aborted")
            run_row = None
        if run_row and run_row[1] == "success":
            rid = run_row[0]
        else:
            rid = g.gate_start_run(build_target_id=bt_id, cycle_id=ci, variant_id=vid,
                                   kind=slice_["target_kind"],   # exec 目标→run.kind='exec'（trg_run_target_consistent）
                                   env_hash=env_hash)
            r = MF.run_manifest_command(manifest, "train", staging_dir=str(staging / f"run{rid}"),
                                        log_name="train.log", src_dir=src_dir, work_root=self.work, policy=self.policy)
            if r["exit_code"] != 0:
                g.gate_finish_run(run_id=rid, status="failed", failure_kind="runtime")
                g.gate_finish_build_target(build_target_id=bt_id, status="failed", failure_kind="runtime")
                return                            # 训练失败入账不入树（§7.1 判例④；答题侧自然无证据）
            ck_path = MF.checkpoint_dest(manifest, staging / f"run{rid}")   # 围栏解析进 run 目录内
            with d.transaction() as conn:         # checkpoint 登记（run 产物；finish_run success 的前置）
                # ckpt_key 带 run id（codex SHOULD）：UNIQUE(variant,ckpt_key) 下，崩在 ckpt 与 finish_run 之间
                # → 旧 run 被 abort、新 run 重训——固定 'final' 会撞唯一键永久楔死；残留 ckpt 归属 aborted run
                # 无害（消费方按 produced_by_run=成功 run 取）。
                conn.execute("INSERT INTO checkpoint(variant_id,ckpt_key,path,content_hash,hash_alg,produced_by_run) "
                             "VALUES (?,?,?,?,'sha256',?)",
                             (vid, f"final-r{rid}", str(ck_path), H.file_sha256(str(ck_path)), rid))
            g.gate_finish_run(run_id=rid, status="success")
        # train log 入账 + 观测 ingest：**无条件、幂等**（不藏在 fresh 分支——崩在 finish_run 与 ingest 之间时，
        # 复用 run 的续跑须从 staging 存活文件补登，否则杀 vs 不杀终库不一致，内审 SHOULD）
        self._register_and_ingest_log(ci, staging / f"run{rid}" / "train.log", log_kind="train", run_id=rid)
        # —— (ii) 出厂评估 + 注册段 ——
        erow = d.query_one("SELECT id,status FROM evaluation WHERE build_target_id=?", (bt_id,))
        if erow is None:
            eval_final = staging / f"eval{rid}" / "eval.log"
            if eval_final.exists():
                # 崩在「eval 跑完（final 已原子改名）→ register 前」的缝隙：**从存活 final 续注册、不重跑**
                # ——重跑会撞 run_staged 的同名 final 拒（codex BLOCKER：永久 FileExistsError 楔死）。
                # exit 判定复用 harness 侧车（final 存在 ⟹ 侧车先落）：失败进程即使输出了合法 metrics 也
                # **不得**被续注册成成功（codex 第2轮 BLOCKER）；侧车缺失 = staging 损毁 → fail loud。
                exit_file = eval_final.with_name("eval.log.exit")
                if not exit_file.exists():
                    raise RuntimeError(f"staging 损毁：{eval_final} 在而 exit 侧车缺——须人工核（不得臆判成功）")
                exit_code = int(exit_file.read_text())
                eval_log = eval_final.read_bytes()
                ev = {"log_path": str(eval_final), "log_sha256": hashlib.sha256(eval_log).hexdigest(),
                      "log_bytes": len(eval_log), "exit_code": exit_code}
            else:
                ev = MF.run_manifest_command(manifest, "eval", staging_dir=str(staging / f"eval{rid}"),
                                             log_name="eval.log", src_dir=src_dir, work_root=self.work,
                                             policy=self.policy, ckpt_path=MF.checkpoint_dest(manifest, staging / f"run{rid}"))
                eval_log = Path(ev["log_path"]).read_bytes()
            if ev["exit_code"] != 0:              # fresh 与 resume 同一判定点（评估进程失败 → target failed）
                g.gate_finish_build_target(build_target_id=bt_id, status="failed", failure_kind="runtime")
                return
            metrics = self._metrics_from_eval_log(eval_log.decode("utf-8", errors="replace"))
            res_sh = self._result_subject_hash(bt_id, slice_, ledger, rid, metrics, ev)
            self._judge_once(ci, bt_id, "bundle_result_review", res_sh)
            if not g.review_passed(build_target_id=bt_id, review_kind="bundle_result_review",
                                   current_subject_hash=res_sh):
                # 结果评审 FAIL → review_failed：run(success)+checkpoint 保留（训练事实），测量整包不注册
                # （§4.2.5：第(ii)段不发生）——lockstep：import_worker 同修
                g.gate_finish_build_target(build_target_id=bt_id, status="failed", failure_kind="review_failed")
                return
            try:
                reg = self.gate.gate_register_evaluation(
                    cycle_id=ci, build_target_id=bt_id, purpose="factory", current_subject_hash=res_sh,
                    metric_results=metrics,
                    create={"variant_id": vid, "protocol_id": slice_["protocol_id"], "protocol_ver": slice_["protocol_ver"],
                            "eval_key": slice_["eval_key"], "source": "factory", "target_set_hash": slice_["target_set_hash"]},
                    env_hash=env_hash,
                    artifact_ref=f"sha256:{ev['log_sha256']}")   # 评估 log 哈希锚落 attempt（补登强校验用，codex BLOCKER）
            except GateReject as e:
                # **只在此调用点**把注册闸拒转业务失败（codex 第2轮 BLOCKER 收窄）：此处的拒 = 评估测量包
                # 不满足协议/required 契约（Codex eval 产物层问题）→ 目标 failed(protocol_violation)、不楔死。
                # 其余 gate（start/progress/finish/register_baseline）的拒仍 fail loud。
                raise _BundleReject(f"测量注册被拒: {e}", failure_kind="protocol_violation") from e
            eid, aid = reg["evaluation_id"], reg["attempt_id"]
        else:
            eid = erow[0]
            aid = d.query_one("SELECT canonical_attempt_id FROM evaluation WHERE id=?", (eid,))[0]
        # attempt-owned eval log 补登 + 观测 ingest（§4.2.5(ii)）：**无条件、幂等、从 staging 存活文件重导出**——
        # 崩在 register_evaluation 与 ingest 之间时，resume 走 else 分支若不补登，下方强制核将永远 raise、
        # target 永卡 running（内审 BLOCKER 实证复现：不可恢复楔死）。register/ingest 均幂等，重放零害。
        self._register_and_ingest_log(ci, staging / f"eval{rid}" / "eval.log", log_kind="eval",
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
                identity_doc = (src_dir / MF.IDENTITY_FILE).read_text(encoding="utf-8")
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
        data = log_path.read_bytes()
        got = hashlib.sha256(data).hexdigest()
        if evaluation_attempt_id is not None:
            # 强校验（codex BLOCKER×2）：补登字节须等于注册时锚在 attempt.artifact_ref 的评估 log 哈希——
            # 崩后 staging 被改写不得把 suspect attempt 洗成 clean。**无锚不 ingest**（None 锚放行=同一洞的
            # append/repro 变体）：凡走本管线补登的 success attempt 注册时必须带 sha256: 锚。
            exp = self.state.daemon.query_one("SELECT artifact_ref FROM evaluation_attempt WHERE id=?",
                                              (evaluation_attempt_id,))
            if not exp or not exp[0] or not exp[0].startswith("sha256:"):
                raise RuntimeError(f"attempt {evaluation_attempt_id} 无 sha256: artifact_ref 锚——"
                                   "拒绝从 staging 补登（注册时须锚评估 log 哈希）")
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
            parser_obs_hash=_canon_hash(OP.parse_log(Path(ev["log_path"]).read_text(), self.obs_policy)),
            identity_draft_hash=ledger[MF.IDENTITY_FILE]))

    @staticmethod
    def _metrics_from_eval_log(text: str) -> List[Dict[str, Any]]:
        """评估产物口径（toy 最小）：eval log 每行 `metric_value: <mid>@<mver>=<float>` → aggregate metric_result。
        <mid>/<mver> = 编排器派生的 int 协议绑定（bundle pack 给 Codex，eval 命令照打印）。
        真评估产物规范 artifact（fold+aggregate 文件）= M6 硬化；此处值真来自真评估子进程输出。"""
        out = []
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("metric_value:"):
                body = line.split(":", 1)[1].strip()          # "1@1=0.93"
                key, val = body.split("=", 1)
                mid, mver = key.split("@", 1)
                out.append({"metric_id": int(mid), "metric_ver": int(mver), "value": float(val)})
        return out

    @staticmethod
    def _next_serial(staging: Path, prefix: str) -> int:
        d = staging / prefix
        return len(list(d.glob(f"{prefix}-*.log"))) + 1 if d.exists() else 1

    # ---------------------------------------------------------------- reasoning --
    def _reasoning_stage(self, cyc) -> None:
        """attack 轮收尾：gate_close_question（真证据 + suspect 谓词）→ atomic(tree_ops+selection+mark_done)。
        **产物先持久化再消费**（codex SHOULD）：reasoning files 先原子落 staging（tmp→replace），resume 时
        复用持久产物、不重调 provider——否则崩在 close 与 atomic 之间时非确定 provider 会产生杀/不杀分歧
        （close 用旧 answer、selection 用新产物的分裂）。可重入：问题已终态 → 跳过 close。"""
        art = self.work / f"c{_cnum(cyc.cycle_id)}" / "reasoning.json"
        if art.exists():
            files = json.loads(art.read_text(encoding="utf-8"))
        else:
            pack = self.compiler.render(cycle_id=cyc.cycle_id, stage="reasoning")
            files = self.p["reasoning"](cyc, pack)
            art.parent.mkdir(parents=True, exist_ok=True)
            tmp = art.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(files, ensure_ascii=False, sort_keys=True), encoding="utf-8")
            tmp.replace(art)
        if "selection.json" not in files:
            raise ValueError("reasoning 必产 selection.json")
        ans = files.get("answer.json")
        if ans is not None:
            if ans.get("question_id") != cyc.question_id:
                # 树契约（codex SHOULD）：attack 轮只许关**本轮 Qn**——关别的问题再把本 Qn 置 inconclusive
                # 属状态破坏（对齐 M0 driver「不得关别的问题」判据）
                raise ValueError(f"answer.question_id（{ans.get('question_id')}）≠ 本轮 Qn（{cyc.question_id}）"
                                 "——不得关别的问题")
            qi = int(ans["question_id"][1:])
            qst = self.state.daemon.query_one("SELECT status FROM question WHERE id=?", (qi,))[0]
            if qst not in ("answered", "refuted", "dead_end"):
                self.close_gate.gate_close_question(
                    cycle_id=cyc.cycle_id, question_id=ans["question_id"], verdict=ans["verdict"],
                    evidence=ans["evidence"], answer_md=ans["answer_md"])
        sel = files["selection.json"]
        with self.state.atomic():
            if self.state.cycle(cyc.cycle_id).status in ("done", "failed", "aborted"):
                return
            qi = int(cyc.question_id[1:]) if cyc.question_id else None
            if qi is not None and self.state.daemon.query_one(
                    "SELECT status FROM question WHERE id=?", (qi,))[0] == "active":
                # 无 answer（或未关成）的攻坚轮：Qn 不得永卡 active——置 inconclusive（增 visit，§4.2.3
                # 「阶段失败=轮正常收尾」口径，对齐 M0 driver；训练/评估失败路径由此收干净）
                self.state.mark_inconclusive(cyc.question_id)
            self.state.apply_tree_ops(cyc.cycle_id, files.get("tree_ops.json", {"ops": []}).get("ops", []))
            persist_selection_safe(self.state, cyc.cycle_id, sel)
            self.state.mark_cycle_done(cyc.cycle_id)
