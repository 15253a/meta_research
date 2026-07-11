"""StageProvider / JudgeProvider —— 真 CodexRunner 的生产装配（M6 CP7.2；步⑧ CP8.3 扩 bundle+judge）。

**为什么要它**：M0 driver（driver.py）用真 Codex 但跑在**桩栈**（StubGate/StubStateStore/StubCompiler +
造假 bundle）——那是 M0 验收栈。M3+ 的真组件（SqliteAdvancer/AttackStages）消费**注入式** provider
回调（生产=真 Codex 会话、测试=确定性替身）。本模块提供生产回调：把 CodexRunner 的一次会话 +
信封解析 + 逐产物 schema 校验 + artifact_parse 重试（§4.2.3）封成组件期望的签名，
从而真组件 + 真 Codex 端到端跑起来（run.py 装配，CP7.3/CP8.4）。

**provider 契约**（attack_stages 模块注释 / advancer reasoning_provider）：
- StageProvider（产文件四阶段）：
  - idea(cyc, pack) → {"idea_set.json": …}
  - plan(cyc, pack) → {"plan.json": …}（冻结 plan.schema 抽象形态；命令不在 plan）
  - bundle(cyc, pack) → {"execution_manifest.json": …, "identity.md": str, <代码文件passthrough…>}
  - reasoning(cyc, pack) → {"selection.json": …, "tree_ops.json"?: …, "answer.json"?: …}
- PlanReviewProvider（写库形态）：独立会话审 plan answerability →
  runner_call(audit/plan_review)+DECISION(judge/plan_review)。
- JudgeProvider（写库形态）：judge(cycle_id, bt_id, review_kind, subject_hash) →
  真 Codex 产 review_verdict.json → 本模块落 runner_call(audit)+DECISION(judge)（Codex 永不碰 DB）。

**职责边界**：本类只保证「产出**结构合法**（过 schema）的 files」；**语义**由组件把关——reasoning-only
轮的 create_root/add_children 必产、answer 时序、manifest↔plan 切片交叉核等由 advancer/attack_stages 校验
（§4.2.3/§4.2.5；attack_stages._check_manifest）。故本类不判 answer_allowed 等轮型语义，只校验**在场**
产物的 schema + 阶段必产文件在场。

**pack 已由调用方渲染**：attack_stages/advancer 先 compiler.render 再传入 pack；本类**不重渲**，只在
重试时把上次的结构错误**追加进 skill**（自足反馈，不依赖 SqliteCompiler.amend——它没有该方法）。

**长操作零事务（§6.13）**：Codex 子进程 + 纯计算不持写事务；JudgeProvider 仅在产物过校验后以一个
**短事务**落 runner_call+DECISION（评审裁决入账）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .cost_ledger import BudgetExhausted, CostAccountingFailed
from .harness import latest_smoke_log as _latest_smoke_log
from .ids import cnum as _cnum
from .interfaces import ContextPack, StageBlockedOnResources
from .import_search import ImportSearchError, validate_import_search_request
from .notify import FileRequestReject
from .process_supervisor import atomic_write_receipt
from .runner import RunnerError

logger = logging.getLogger(__name__)

_RECONCILE_PROTOCOL = "runner-call-v1"


class _RunnerCallHeartbeat:
    """Structured per-call liveness record referenced by runner_call.transcript_ref."""

    def __init__(self, path: Path, *, runner_call_id: int, cycle_id: str,
                 phase: str, purpose: str, interval_s: float = 5.0):
        self.path = path
        self.runner_call_id = runner_call_id
        self.cycle_id = cycle_id
        self.phase = phase
        self.purpose = purpose
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._seq = 0
        self._error: Optional[BaseException] = None

    def _write(self, state: str, *, execution_receipt_ref: Optional[str] = None) -> None:
        self._seq += 1
        payload = {
            "protocol": _RECONCILE_PROTOCOL,
            "signal_kind": "owner_liveness",
            "runner_call_id": self.runner_call_id,
            "cycle_id": self.cycle_id,
            "phase": self.phase,
            "purpose": self.purpose,
            "state": state,
            "heartbeat_seq": self._seq,
            "heartbeat_at_unix": time.time(),
            "execution_receipt_ref": execution_receipt_ref,
        }
        atomic_write_receipt(self.path, payload)

    def start(self) -> None:
        self._write("running")

        def loop() -> None:
            while not self._stop.wait(self.interval_s):
                try:
                    self._write("running")
                except BaseException as error:  # heartbeat failure is checked at the call boundary
                    self._error = error
                    return

        self._thread = threading.Thread(
            target=loop, daemon=True, name=f"runner-call-heartbeat-{self.runner_call_id}")
        self._thread.start()

    def finish(self, state: str, *, execution_receipt_ref: Optional[str] = None) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(2.0, self.interval_s * 2))
            if self._thread.is_alive():
                raise RuntimeError(f"runner_call {self.runner_call_id} heartbeat thread 未停止")
        if self._error is not None:
            raise RuntimeError(
                f"runner_call {self.runner_call_id} heartbeat 写失败") from self._error
        self._write(state, execution_receipt_ref=execution_receipt_ref)


def _bind_runner_call(runner, runner_call_id: int, *, phase: str, purpose: str) -> None:
    bind = getattr(runner, "bind_runner_call", None)
    if callable(bind):
        bind(runner_call_id=runner_call_id, reconcile_protocol=_RECONCILE_PROTOCOL,
             phase=phase, purpose=purpose)

# 阶段 → 产物契约：required=阶段必产（缺即重试）、optional=在场才校验；passthrough=返回信封全部文件
# （bundle 的代码文件名任意、不可枚举——由 attack_stages 按 manifest.code_files 物化并交叉核）。
_STAGE_FILES = {
    "idea": {"required": ["idea_set.json"], "optional": []},
    "plan": {"required": ["plan.json"], "optional": []},
    "bundle": {"required": ["execution_manifest.json", "identity.md"], "optional": [], "passthrough": True},
    "reasoning": {"required": ["selection.json"], "optional": ["tree_ops.json", "answer.json"]},
}
# skill 调用点说明（让工人聚焦本阶段；仿 driver._SKILL_SECTION）
_CALL_NOTE = {
    "idea": "执行【生成任务】+【判官任务】，产 idea_set.json（候选全集 + selected_id）",
    "plan": "执行【计划任务】：产 plan.json；若类型门确认需新外部 baseline 且本轮候选为空，则只产 import_search_request.json",
    "bundle": "按锚区「本目标」切片产可执行包：execution_manifest.json + identity.md + 代码文件（一信封装齐）",
    "reasoning": "执行【轮尾任务】，按 route 产 selection.json（必），酌情 tree_ops.json / answer.json",
}


class StageProvider:
    def __init__(self, *, runner_factory, schemas, policy: Dict[str, Any],
                 system_prompt: str, skills: Dict[str, str], work_root: str,
                 file_request_bridge=None, cost_ledger=None):
        """runner_factory(transcripts_dir, purpose_tag)→Runner（默认真 CodexRunner，见 run.py 装配）；
        schemas=SchemaSet（产物校验）；skills={stage: SKILL.md 文本}；work_root=cycles/<id>/transcripts 的根。
        不持 compiler——pack 由调用方（advancer/attack_stages）渲染后传入，本类不 render。
        file_request_bridge（步⑧ CP8.5）：callable(stage, request, cyc)→request_id——把 resource_request
        sidecar 落成 interaction_request（notify.FileRequestService.create_checked 的装配闭包）；None=未接
        桥（如诊断装配），sidecar 保持 fail-loud。"""
        self.runner_factory = runner_factory
        self.schemas = schemas
        self.retries = policy["flow"]["retry"]["artifact_parse"]
        self.system_prompt = system_prompt
        self.skills = skills
        self.work = Path(work_root)
        self.file_request_bridge = file_request_bridge
        self.cost_ledger = cost_ledger         # None 仅允许 session_max=null 的显式诊断/测试装配
        self._cost_required = policy.get("budget", {}).get("session_max") is not None
        if self._cost_required and self.cost_ledger is None:
            raise ValueError("budget.session_max 已启用，StageProvider 必须注入 cost_ledger；"
                             "测试/诊断须显式设 session_max=null")
        self._call_seq = 0                     # 全局调用序（transcript 文件名唯一，P6 回放防覆盖）

    # -- provider 回调（绑定阶段）------------------------------------------------
    def idea(self, cyc, pack) -> Dict[str, Any]:
        return self._produce(cyc, pack, stage="idea")

    def plan(self, cyc, pack) -> Dict[str, Any]:
        return self._produce(cyc, pack, stage="plan")

    def bundle(self, cyc, pack) -> Dict[str, Any]:
        return self._produce(cyc, pack, stage="bundle")

    def reasoning(self, cyc, pack) -> Dict[str, Any]:
        return self._produce(cyc, pack, stage="reasoning")

    # -- 重试核心 --------------------------------------------------------------
    def _produce(self, cyc, pack, *, stage: str) -> Dict[str, Any]:
        """一次阶段 Codex 会话 + 信封解析 + 逐产物 schema 校验（artifact_parse ≤N 重试，附错误反馈）。
        返回 files dict（含 required + 在场的 optional，均已过 schema）。用尽重试仍非法 → RunnerError。"""
        spec = _STAGE_FILES[stage]
        self._call_seq += 1
        runner = self.runner_factory(self.work / f"cycles/{cyc.cycle_id}/transcripts",
                                     f"{stage}-n{self._call_seq}")
        base_skill = self.skills[stage] + f"\n\n===== 调用点 =====\n本次调用：{_CALL_NOTE[stage]}。"
        last_err = ""
        for attempt in range(self.retries + 1):
            skill = base_skill if not last_err else (
                base_skill + f"\n\n===== 上次产物被拒（第 {attempt} 次重试）=====\n{last_err}\n请修正后重出。")
            call = self._begin_cost_call(cyc, stage, runner, attempt)
            try:
                art = runner.run_task(system_prompt=self.system_prompt, skill=skill, context_pack=pack)
            except RunnerError as e:               # 进程失败/超时/信封不可解析 → 计入重试
                self._record_cost(
                    cyc, stage, e.usage, status="failed", failure_kind=e.failure_kind,
                    attempt=attempt, call=call, transcript_ref=e.transcript_ref,
                    execution_receipt_ref=e.execution_receipt_ref)
                last_err = str(e)
                continue
            except Exception as error:             # lifecycle/control failure：调用已放行，必须收口同一 intent
                self._record_cost(
                    cyc, stage, getattr(error, "usage", None), status="failed",
                    failure_kind=getattr(error, "failure_kind", type(error).__name__.lower()),
                    attempt=attempt, call=call,
                    transcript_ref=getattr(error, "transcript_ref", None),
                    execution_receipt_ref=(getattr(error, "execution_receipt_ref", None)
                                           or str(getattr(error, "receipt_path", "") or "") or None))
                raise
            # 步⑩ CP10.2：每次真 LLM 调用都记账，但 runner_call 还要诚实区分「有效产物」与
            # 「进程成功但产物被拒」。因此各分支在结论确定后各记恰好一次；基础设施异常也会先
            # 记本次已发生的调用再 fail loud。session_max 启用时写账失败 fail-closed。
            if "resource_request.json" in art.files:
                # 阶段发资源请求 sidecar（§3.1.1「需用户提供文件」，步⑧ CP8.5 接桥）。
                # **有意置于 stage 漂移/schema 校验之前**（codex NIT 注记）：sidecar 是「无法工作」的控制
                # 信号，优先于产物质量判定——信封哪怕 stage 漂移/产物残缺，资源诉求也须落单，不得因产物
                # 校验失败而丢。
                # - 已接桥 → 落 interaction_request（create_checked：schema+幂等+quota 闸）→ 抛
                #   StageBlockedOnResources（本轮停在游标处等待；信封里的其余阶段产物**弃用**——工人
                #   自述缺文件无法完成，半成品不冒充阶段产物；resolve 后重做本阶段）；
                # - 桥拒（sidecar 非法/quota 尽）→ 计入重试反馈（工人可修正或放弃 sidecar）；
                # - 未接桥（诊断装配）→ 保持 fail loud，绝不静默丢弃。
                if self.file_request_bridge is None:
                    self._record_cost(cyc, stage, art.usage, status="failed",
                                      failure_kind="artifact_parse", attempt=attempt, call=call,
                                      transcript_ref=art.transcript_ref,
                                      execution_receipt_ref=art.execution_receipt_ref)
                    raise RunnerError(f"{stage} 产出 resource_request.json sidecar，但本装配未接文件请求桥"
                                      "——不静默丢弃")
                try:
                    rid = self.file_request_bridge(stage, art.files["resource_request.json"], cyc)
                except FileRequestReject as e:  # 只兜业务拒（sidecar 非法/quota 尽）→ 反馈重试（有界）；
                    self._record_cost(cyc, stage, art.usage, status="failed",
                                      failure_kind="artifact_parse", attempt=attempt, call=call,
                                      transcript_ref=art.transcript_ref,
                                      execution_receipt_ref=art.execution_receipt_ref)
                    last_err = f"resource_request sidecar 被拒: {e}"   # 其余异常（DB 损坏等）fail loud（内审 NIT）
                    continue
                except Exception:              # noqa: BLE001 —— 调用已发生，先记账再保留原异常
                    self._record_cost(cyc, stage, art.usage, status="failed",
                                      failure_kind="postprocess_error", attempt=attempt, call=call,
                                      transcript_ref=art.transcript_ref,
                                      execution_receipt_ref=art.execution_receipt_ref)
                    raise
                self._record_cost(
                    cyc, stage, art.usage, status="success", attempt=attempt, call=call,
                    transcript_ref=art.transcript_ref,
                    execution_receipt_ref=art.execution_receipt_ref)
                raise StageBlockedOnResources(rid, stage)
            if art.stage != stage:              # 阶段漂移（外审 SHOULD）：文件对但 envelope stage 错 → 计入重试
                self._record_cost(cyc, stage, art.usage, status="failed",
                                  failure_kind="artifact_parse", attempt=attempt, call=call,
                                  transcript_ref=art.transcript_ref,
                                  execution_receipt_ref=art.execution_receipt_ref)
                last_err = f"产物 stage 漂移：envelope stage={art.stage!r} ≠ 期望 {stage!r}"
                continue
            if "import_search_request.json" in art.files:
                # plan 的只读发现控制 sidecar：须独占信封，编排器消费后
                # 重渲染 ContextPack 并在新会话里重做 plan。它不是 Gate 事实产物，
                # 也不能与 plan.json 共存（否则模型可在搜索前偷塞决策）。
                if stage != "plan" or set(art.files) != {"import_search_request.json"}:
                    self._record_cost(
                        cyc, stage, art.usage, status="failed",
                        failure_kind="artifact_parse", attempt=attempt, call=call,
                        transcript_ref=art.transcript_ref,
                        execution_receipt_ref=art.execution_receipt_ref)
                    last_err = (
                        "import_search_request.json 只允许 plan 阶段独占 files；"
                        f"实收键 {sorted(art.files)}")
                    continue
                errors = self._schema_errors_by_name(
                    "import_search_request", art.files["import_search_request.json"])
                try:
                    request = validate_import_search_request(
                        art.files["import_search_request.json"])
                except ImportSearchError as error:
                    errors.append(str(error))
                if errors:
                    self._record_cost(
                        cyc, stage, art.usage, status="failed",
                        failure_kind="artifact_parse", attempt=attempt, call=call,
                        transcript_ref=art.transcript_ref,
                        execution_receipt_ref=art.execution_receipt_ref)
                    last_err = "import_search_request.json schema/边界校验失败:\n" + "\n".join(errors[:8])
                    continue
                self._record_cost(
                    cyc, stage, art.usage, status="success", attempt=attempt, call=call,
                    transcript_ref=art.transcript_ref,
                    execution_receipt_ref=art.execution_receipt_ref)
                return {"import_search_request.json": request}
            if stage == "plan" and set(art.files) != {"plan.json"}:
                self._record_cost(
                    cyc, stage, art.usage, status="failed",
                    failure_kind="artifact_parse", attempt=attempt, call=call,
                    transcript_ref=art.transcript_ref,
                    execution_receipt_ref=art.execution_receipt_ref)
                last_err = (
                    "plan 普通产物须恰为 plan.json 一个文件；"
                    f"实收键 {sorted(art.files)}")
                continue
            err = self._validate_files(art.files, spec)
            if err:
                self._record_cost(cyc, stage, art.usage, status="failed",
                                  failure_kind="artifact_parse", attempt=attempt, call=call,
                                  transcript_ref=art.transcript_ref,
                                  execution_receipt_ref=art.execution_receipt_ref)
                last_err = err
                continue
            self._record_cost(
                cyc, stage, art.usage, status="success", attempt=attempt, call=call,
                transcript_ref=art.transcript_ref,
                execution_receipt_ref=art.execution_receipt_ref)
            if spec.get("passthrough"):        # bundle：代码文件名任意 → 信封全量透传（物化/交叉核在组件侧）
                return dict(art.files)
            return {k: art.files[k] for k in spec["required"] + [o for o in spec["optional"] if o in art.files]}
        # 完整 last_err（外审 NIT：不截断——这是 fail-fast 排障入口，schema oneOf 展开的字段路径不能丢）
        raise RunnerError(f"{stage} 产物结构非法，artifact_parse 重试（≤{self.retries}）用尽：{last_err}")

    def _begin_cost_call(self, cyc, stage: str, runner, attempt: int):
        if self.cost_ledger is None:
            return None
        purpose = f"{stage}-n{self._call_seq}-a{attempt + 1}"
        heartbeat_path = (
            self.work / f"cycles/{cyc.cycle_id}/transcripts" /
            f"{purpose}.heartbeat.json")
        rc = self.cost_ledger.begin_call(
            cycle_id=cyc.cycle_id, phase=stage, purpose=purpose,
            transcript_ref=str(heartbeat_path))
        heartbeat = _RunnerCallHeartbeat(
            heartbeat_path, runner_call_id=rc, cycle_id=cyc.cycle_id,
            phase=stage, purpose=purpose)
        try:
            _bind_runner_call(runner, rc, phase=stage, purpose=purpose)
            self.cost_ledger.mark_call_running(runner_call_id=rc)
            heartbeat.start()
        except BaseException:
            try:
                heartbeat.finish("aborted")
            except BaseException:
                pass
            self.cost_ledger.abort_unstarted_call(
                runner_call_id=rc, failure_kind="call_prepare_failed")
            raise
        return rc, heartbeat

    def _record_cost(self, cyc, stage: str, usage, *, status: str,
                     attempt: int, call=None, failure_kind: Optional[str] = None,
                     transcript_ref: Optional[str] = None,
                     execution_receipt_ref: Optional[str] = None) -> None:
        """本次调用记账。预算网开启时 fail-closed；只有 session_max=null 时才容忍记账故障。

        usage=None 也写 money=0 行；RunnerError 携带多少就记多少，保证失败重试不从调用历史消失。
        cost_ledger=None 仅保留给 session_max=null 的显式诊断/测试装配，生产 run.py 总会注入。
        """
        if self.cost_ledger is None:
            return
        if call is None:
            raise RuntimeError("已启用 cost_ledger 但缺 runner_call lifecycle")
        runner_call_id, heartbeat = call
        heartbeat_error = None
        try:
            heartbeat.finish(status, execution_receipt_ref=execution_receipt_ref)
        except Exception as error:              # 调用已完成；heartbeat 失败归 postprocess，不丢成本
            heartbeat_error = error
            status = "failed"
            failure_kind = "heartbeat_failed"
        try:
            self.cost_ledger.finish_call(
                runner_call_id=runner_call_id, usage=usage, status=status,
                failure_kind=failure_kind,
                transcript_ref=transcript_ref or str(heartbeat.path))
        except (BudgetExhausted, CostAccountingFailed):
            raise                               # ledger/global_stop 已提交；立即阻断，不当作写账故障吞掉
        except Exception:                      # noqa: BLE001 —— 预算开启时必须 fail-closed
            logger.error("成本记账失败 (cycle=%s phase=%s)", getattr(cyc, "cycle_id", "?"), stage,
                         exc_info=True)
            if self._cost_required:
                raise
        if heartbeat_error is not None:
            raise heartbeat_error

    def _validate_files(self, files: Dict[str, Any], spec: Dict[str, Any]) -> Optional[str]:
        """校验：required 全在场 + required/在场 optional 各过 schema（.json 走对应 schema；.md 只须
        非空字符串——identity.md 无 schema，语义由组件核）。返回错误串（供反馈重试）或 None。"""
        missing = [f for f in spec["required"] if f not in files]
        if missing:
            return f"缺阶段必产文件 {missing}（files 键: {list(files)}）"
        for name in spec["required"] + [o for o in spec["optional"] if o in files]:
            if name.endswith(".json"):
                errs = self._schema_errors(name, files[name])
                if errs:
                    return f"{name} schema 校验失败:\n" + "\n".join(errs[:8])
            elif not (isinstance(files[name], str) and files[name].strip()):
                return f"{name} 须为非空文本（实收 {type(files[name]).__name__}）"
        return None

    def _schema_errors(self, filename: str, payload: Any) -> List[str]:
        """逐产物 schema 校验，展平 oneOf 子错误（反馈才有具体键名；同 driver._run_with_retry）。"""
        v = self.schemas.validator_for_artifact(filename)
        return self._validator_errors(v, payload)

    def _schema_errors_by_name(self, name: str, payload: Any) -> List[str]:
        return self._validator_errors(self.schemas.validator(name), payload)

    @staticmethod
    def _validator_errors(v, payload: Any) -> List[str]:  # noqa: ANN001
        errors: List[str] = []
        for e in v.iter_errors(payload):
            errors.append(f"{e.json_path} {e.message}")
            stack = list(e.context or [])
            while stack:
                sub = stack.pop()
                errors.append(f"{sub.json_path} {sub.message}")
                stack.extend(sub.context or [])
        return errors


class PlanReviewProvider:
    """Independent plan answerability judge (semantic rounds ≤ policy, artifact retries per call)."""

    def __init__(self, *, runner_factory, schemas, policy: Dict[str, Any],
                 system_prompt: str, skill: str, daemon, work_root: str,
                 cost_ledger=None):
        self.runner_factory = runner_factory
        self.schemas = schemas
        self.retries = policy["flow"]["retry"]["artifact_parse"]
        self.system_prompt = system_prompt
        self.skill = skill
        self.daemon = daemon
        self.work = Path(work_root)
        self.cost_ledger = cost_ledger
        self._cost_required = policy.get("budget", {}).get("session_max") is not None
        if self._cost_required and cost_ledger is None:
            raise ValueError("budget.session_max 已启用，PlanReviewProvider 必须注入 cost_ledger")
        self.policy_hash = hashlib.sha256(skill.encode("utf-8")).hexdigest()
        self._call_seq = 0

    def __call__(self, cyc, plan: Dict[str, Any], round_no: int,
                 pack: ContextPack) -> tuple[Dict[str, Any], int]:
        if isinstance(round_no, bool) or not isinstance(round_no, int) or not 1 <= round_no <= 2:
            raise ValueError("plan review round_no 必须在 1..2")
        if not isinstance(plan, dict):
            raise ValueError("plan review 输入 plan 须为 object")
        if (pack.cycle_id != cyc.cycle_id or pack.stage != "plan"
                or getattr(pack, "target_id", None) is not None):
            raise ValueError("plan review ContextPack cycle/stage/target 身份漂移")
        self._call_seq += 1
        runner = self.runner_factory(
            self.work / f"cycles/{cyc.cycle_id}/transcripts",
            f"plan-review-r{round_no}-n{self._call_seq}")
        plan_hash = hashlib.sha256(json.dumps(
            plan, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
        if f"staging:plan-draft:{plan_hash}" not in getattr(pack, "sources", []):
            raise ValueError(
                "plan review ContextPack 未锚定调用参数中的 exact plan hash")
        prior = self.daemon.query(
            "SELECT id,json_extract(payload_json,'$.plan_hash') FROM decision "
            "WHERE cycle_id=? AND actor='judge' AND type='plan_review' "
            "AND json_valid(payload_json) AND json_extract(payload_json,'$.round_no')=? "
            "ORDER BY id", (_cnum(cyc.cycle_id), round_no))
        if prior:
            raise RuntimeError(
                f"plan review c{_cnum(cyc.cycle_id)} round {round_no} 已有 durable verdict: {prior}")
        base = self.skill + (
            "\n\n===== 调用点 =====\n只执行【评审任务】；这是与 plan generator 独立的新会话。"
            f"产 plan_review.json，round_no 必须为 {round_no}。")
        last_err = ""
        for attempt in range(self.retries + 1):
            skill = base if not last_err else (
                base + f"\n\n===== 上次评审产物被拒（第 {attempt} 次重试）=====\n"
                + last_err + "\n只修正评审信封，不生成 plan.json。")
            call = self._begin_call(cyc.cycle_id, runner, round_no, attempt)
            try:
                art = runner.run_task(
                    system_prompt=self.system_prompt, skill=skill, context_pack=pack)
            except RunnerError as error:
                self._finish_failed(
                    call, error.usage, failure_kind=error.failure_kind,
                    transcript_ref=error.transcript_ref,
                    execution_receipt_ref=error.execution_receipt_ref)
                # flow.retry.artifact_parse only repairs a returned but structurally invalid verdict.
                # Process/transport/timeout/envelope RunnerError is an infrastructure call failure:
                # retrying it here would spend again and later misreport it as artifact_parse exhaustion.
                raise
            except Exception as error:
                self._finish_failed(
                    call, getattr(error, "usage", None),
                    failure_kind=getattr(error, "failure_kind", type(error).__name__.lower()),
                    transcript_ref=getattr(error, "transcript_ref", None),
                    execution_receipt_ref=(getattr(error, "execution_receipt_ref", None)
                                           or str(getattr(error, "receipt_path", "") or "") or None))
                raise
            review = art.files.get("plan_review.json")
            errors = []
            if art.stage != "plan":
                errors.append(f"envelope stage={art.stage!r}，期望 'plan'")
            if "resource_request.json" in art.files:
                errors.append("plan reviewer 不受理 resource_request sidecar")
            unexpected = sorted(set(art.files) - {"plan_review.json"})
            if unexpected:
                errors.append(f"plan reviewer 含未授权额外 files: {unexpected}")
            if review is None:
                errors.append(f"缺 plan_review.json（files 键: {list(art.files)}）")
            else:
                errors.extend(
                    f"{error.json_path} {error.message}"
                    for error in self.schemas.validator("plan_review").iter_errors(review))
                if review.get("round_no") != round_no:
                    errors.append(
                        f"plan_review.round_no={review.get('round_no')!r}，期望 {round_no}")
            if errors:
                self._finish_failed(
                    call, art.usage, failure_kind="artifact_parse",
                    transcript_ref=art.transcript_ref,
                    execution_receipt_ref=art.execution_receipt_ref)
                last_err = "\n".join(errors[:8])
                continue
            decision_id = self._record(
                cyc.cycle_id, round_no, plan_hash, review, art.usage, call=call,
                transcript_ref=art.transcript_ref,
                execution_receipt_ref=art.execution_receipt_ref)
            return review, decision_id
        raise RunnerError(
            f"plan_review 第 {round_no} 轮产物结构非法，artifact_parse 重试"
            f"（≤{self.retries}）用尽：{last_err}")

    def _begin_call(self, cycle_id: str, runner, round_no: int, attempt: int):
        if self.cost_ledger is None:
            return None
        heartbeat_path = (
            self.work / f"cycles/{cycle_id}/transcripts" /
            f"plan-review-r{round_no}-n{self._call_seq}-a{attempt + 1}.heartbeat.json")
        runner_call_id = self.cost_ledger.begin_call(
            cycle_id=cycle_id, phase="audit", purpose="plan_review",
            transcript_ref=str(heartbeat_path))
        heartbeat = _RunnerCallHeartbeat(
            heartbeat_path, runner_call_id=runner_call_id, cycle_id=cycle_id,
            phase="audit", purpose="plan_review")
        try:
            _bind_runner_call(
                runner, runner_call_id, phase="audit", purpose="plan_review")
            self.cost_ledger.mark_call_running(runner_call_id=runner_call_id)
            heartbeat.start()
        except BaseException:
            try:
                heartbeat.finish("aborted")
            except BaseException:
                pass
            self.cost_ledger.abort_unstarted_call(
                runner_call_id=runner_call_id, failure_kind="call_prepare_failed")
            raise
        return runner_call_id, heartbeat

    def _finish_failed(self, call, usage, *, failure_kind: str,
                       transcript_ref=None, execution_receipt_ref=None) -> None:
        if call is None:
            return
        runner_call_id, heartbeat = call
        heartbeat_error = None
        try:
            heartbeat.finish("failed", execution_receipt_ref=execution_receipt_ref)
        except Exception as error:
            heartbeat_error = error
            failure_kind = "heartbeat_failed"
        self.cost_ledger.finish_call(
            runner_call_id=runner_call_id, usage=usage, status="failed",
            failure_kind=failure_kind,
            transcript_ref=transcript_ref or str(heartbeat.path))
        if heartbeat_error is not None:
            raise heartbeat_error

    def _record(self, cycle_id: str, round_no: int, plan_hash: str,
                review: Dict[str, Any], usage, *, call,
                transcript_ref=None, execution_receipt_ref=None) -> int:
        ci = _cnum(cycle_id)
        budget_hit = None
        if self.cost_ledger is not None and call is None:
            raise RuntimeError("plan review verdict 缺预调用 runner_call")
        if call is not None:
            runner_call_id, heartbeat = call
            try:
                heartbeat.finish("success", execution_receipt_ref=execution_receipt_ref)
            except Exception as error:
                self.cost_ledger.finish_call(
                    runner_call_id=runner_call_id, status="failed", usage=usage,
                    failure_kind="heartbeat_failed",
                    transcript_ref=transcript_ref or str(heartbeat.path))
                raise error
        try:
            with self.daemon.transaction() as conn:
                duplicate = conn.execute(
                    "SELECT id FROM decision WHERE cycle_id=? AND actor='judge' "
                    "AND type='plan_review' AND json_valid(payload_json) "
                    "AND json_extract(payload_json,'$.round_no')=? ORDER BY id",
                    (ci, round_no)).fetchall()
                if duplicate:
                    raise RuntimeError(
                        f"plan review c{ci} round {round_no} 已有 durable decision {duplicate}")
                if self.cost_ledger is not None:
                    budget_hit = self.cost_ledger.finish_call_in_txn(
                        conn, runner_call_id=runner_call_id, status="success", usage=usage,
                        transcript_ref=transcript_ref or str(heartbeat.path))
                else:
                    runner_call_id = conn.execute(
                        "INSERT INTO runner_call(cycle_id,phase,purpose,status,transcript_ref,"
                        "started_at,finished_at) VALUES (?,'audit','plan_review','success',?,"
                        "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
                        (ci, transcript_ref)).lastrowid
                decision_id = conn.execute(
                    "INSERT INTO decision(cycle_id,actor,type,payload_json) "
                    "VALUES (?,'judge','plan_review',?)",
                    (ci, json.dumps({
                        "round_no": round_no, "verdict": review["verdict"],
                        "issues": review.get("issues", []),
                        "notes_md": review.get("notes_md", ""),
                        "plan_hash": plan_hash, "runner_call_id": runner_call_id,
                        "policy_hash": self.policy_hash,
                    }, ensure_ascii=False, sort_keys=True))).lastrowid
        except Exception:
            if self.cost_ledger is not None:
                self.cost_ledger.finish_call(
                    runner_call_id=runner_call_id, status="failed", usage=usage,
                    failure_kind="postprocess_error",
                    transcript_ref=transcript_ref or str(heartbeat.path))
            raise
        if budget_hit is not None:
            raise BudgetExhausted(**budget_hit)
        return decision_id


def _tail(text: str, n: int = 2000) -> str:
    """材料摘要截尾（评审对象走 prompt，防超长；截断显式标注，不冒充全文）。"""
    return text if len(text) <= n else f"…（前 {len(text) - n} 字符截断）…\n" + text[-n:]


class JudgeProvider:
    """真 Codex 双评审装配（步⑧ CP8.3）：attack/import target 的 judge 契约实现——
    judge(cycle_id, build_target_id, review_kind, subject_hash)。
    ``_subject_md`` 先由 build_target.kind 机械解析布局：研究 target 读
    ``work/c<ci>/t<bt>/{src,smoke,run*,eval*}``，import worker target 读
    ``work/import<external_import_id>/{clone,smoke,eval*}``；两者仍走同一 subject_hash/runner_call/decision 判据。

    分工（§4.1.4 附注）：**编排器**机械装 subject 材料（DB 切片/checkpoint 哈希 + staging 物化代码/
    log 摘要）→ 独立 Codex 会话按 judge SKILL 产 review_verdict.json（schema 校验 + artifact_parse
    重试）→ **本类**以短事务落 runner_call(phase='audit')+DECISION(actor='judge')。Codex 永不碰 DB、
    不自算 subject_hash（编排器传入并原样落 DECISION——review_passed 按它对账当下重算值）。
    fail 权真实：verdict 原样入库，fail ⇒ review_passed=False ⇒ 目标 failed(review_failed)。
    replay-safe 由调用方 judge_once 保证（同 (target,kind,subject_hash) 不重调）。
    policy_hash = judge SKILL 文本 sha256（prompt 版本指纹：措辞即行为，换 prompt 即换裁决口径）。"""

    def __init__(self, *, runner_factory, schemas, policy: Dict[str, Any], system_prompt: str,
                 skill: str, daemon, work_root: str, cost_ledger=None):
        self.runner_factory = runner_factory
        self.schemas = schemas
        self.retries = policy["flow"]["retry"]["artifact_parse"]
        self.system_prompt = system_prompt
        self.skill = skill
        self.daemon = daemon
        self.work = Path(work_root)
        self.policy_hash = hashlib.sha256(skill.encode("utf-8")).hexdigest()
        self.cost_ledger = cost_ledger         # 步⑩ CP10.2：judge(audit) 调用记账（复用 _record 建的 runner_call）
        self._cost_required = policy.get("budget", {}).get("session_max") is not None
        if self._cost_required and self.cost_ledger is None:
            raise ValueError("budget.session_max 已启用，JudgeProvider 必须注入 cost_ledger；"
                             "测试/诊断须显式设 session_max=null")
        self._call_seq = 0

    def __call__(self, cycle_id: str, build_target_id: int, review_kind: str, subject_hash: str) -> None:
        if review_kind not in ("bundle_code_review", "bundle_result_review"):
            # fail loud（codex SHOULD）：拼错的 kind 会写进任意 decision.type/runner_call.purpose，
            # 下游 review_passed 永远看不到期望评审 → 静默楔死；此处当场拒。
            raise ValueError(f"未知 review_kind: {review_kind!r}（只支持 bundle_code_review/bundle_result_review）")
        pack = ContextPack(cycle_id=cycle_id, stage="bundle", target_id=str(build_target_id),
                           anchor_md=self._subject_md(cycle_id, build_target_id, review_kind),
                           neighborhood_md="", retrieval_md="", refs=[])
        self._call_seq += 1
        runner = self.runner_factory(self.work / f"cycles/{cycle_id}/transcripts",
                                     f"judge-{review_kind}-n{self._call_seq}")
        base = self.skill + (f"\n\n===== 调用点 =====\n本次评审：{review_kind}"
                             f"（build_target={build_target_id}）。产 review_verdict.json。")
        last_err = ""
        for attempt in range(self.retries + 1):
            sk = base if not last_err else base + f"\n\n===== 上次产物被拒（第 {attempt} 次重试）=====\n{last_err}\n请修正后重出。"
            call = self._begin_cost_call(cycle_id, review_kind, runner, attempt)
            try:
                art = runner.run_task(system_prompt=self.system_prompt, skill=sk, context_pack=pack)
            except RunnerError as e:
                self._record_cost(
                    cycle_id, review_kind, e.usage, status="failed",
                    failure_kind=e.failure_kind, attempt=attempt, call=call,
                    transcript_ref=e.transcript_ref,
                    execution_receipt_ref=e.execution_receipt_ref)
                last_err = str(e)
                continue
            except Exception as error:
                self._record_cost(
                    cycle_id, review_kind, getattr(error, "usage", None), status="failed",
                    failure_kind=getattr(error, "failure_kind", type(error).__name__.lower()),
                    attempt=attempt, call=call,
                    transcript_ref=getattr(error, "transcript_ref", None),
                    execution_receipt_ref=(getattr(error, "execution_receipt_ref", None)
                                           or str(getattr(error, "receipt_path", "") or "") or None))
                raise
            if "resource_request.json" in art.files:
                # 判官不许要文件（评审对象已全在材料里；sidecar 出现=越界）——反馈重试，不静默丢弃
                self._record_cost(cycle_id, review_kind, art.usage, status="failed",
                                  failure_kind="artifact_parse", attempt=attempt, call=call,
                                  transcript_ref=art.transcript_ref,
                                  execution_receipt_ref=art.execution_receipt_ref)
                last_err = "judge 不受理 resource_request sidecar（评审材料已全量给出，产 review_verdict.json 即可）"
                continue
            verdict = art.files.get("review_verdict.json")
            if verdict is None:
                self._record_cost(cycle_id, review_kind, art.usage, status="failed",
                                  failure_kind="artifact_parse", attempt=attempt, call=call,
                                  transcript_ref=art.transcript_ref,
                                  execution_receipt_ref=art.execution_receipt_ref)
                last_err = f"缺 review_verdict.json（files 键: {list(art.files)}）"
                continue
            errs = [f"{e.json_path} {e.message}"
                    for e in self.schemas.validator("review_verdict").iter_errors(verdict)]
            if errs:
                self._record_cost(cycle_id, review_kind, art.usage, status="failed",
                                  failure_kind="artifact_parse", attempt=attempt, call=call,
                                  transcript_ref=art.transcript_ref,
                                  execution_receipt_ref=art.execution_receipt_ref)
                last_err = "review_verdict.json schema 校验失败:\n" + "\n".join(errs[:8])
                continue
            self._record(
                cycle_id, build_target_id, review_kind, subject_hash, verdict, art.usage,
                call=call, transcript_ref=art.transcript_ref,
                execution_receipt_ref=art.execution_receipt_ref)
            return
        raise RunnerError(f"judge {review_kind} 产物结构非法，artifact_parse 重试（≤{self.retries}）用尽：{last_err}")

    def _begin_cost_call(self, cycle_id: str, review_kind: str, runner, attempt: int):
        if self.cost_ledger is None:
            return None
        # Gate.review_passed mechanically requires runner_call.purpose == review_kind.
        heartbeat_path = (
            self.work / f"cycles/{cycle_id}/transcripts" /
            f"{review_kind}-n{self._call_seq}-a{attempt + 1}.heartbeat.json")
        rc = self.cost_ledger.begin_call(
            cycle_id=cycle_id, phase="audit", purpose=review_kind,
            transcript_ref=str(heartbeat_path))
        heartbeat = _RunnerCallHeartbeat(
            heartbeat_path, runner_call_id=rc, cycle_id=cycle_id,
            phase="audit", purpose=review_kind)
        try:
            _bind_runner_call(runner, rc, phase="audit", purpose=review_kind)
            self.cost_ledger.mark_call_running(runner_call_id=rc)
            heartbeat.start()
        except BaseException:
            try:
                heartbeat.finish("aborted")
            except BaseException:
                pass
            self.cost_ledger.abort_unstarted_call(
                runner_call_id=rc, failure_kind="call_prepare_failed")
            raise
        return rc, heartbeat

    def _record_cost(self, cycle_id: str, review_kind: str, usage, *, status: str,
                     attempt: int, call=None, failure_kind: Optional[str] = None,
                     transcript_ref: Optional[str] = None,
                     execution_receipt_ref: Optional[str] = None) -> None:
        """记录未形成最终裁决的 judge 调用（RunnerError 或 Artifact 被拒）。"""
        if self.cost_ledger is None:
            return
        if call is None:
            raise RuntimeError("已启用 cost_ledger 但缺 judge runner_call lifecycle")
        runner_call_id, heartbeat = call
        heartbeat_error = None
        try:
            heartbeat.finish(status, execution_receipt_ref=execution_receipt_ref)
        except Exception as error:
            heartbeat_error = error
            status = "failed"
            failure_kind = "heartbeat_failed"
        try:
            self.cost_ledger.finish_call(
                runner_call_id=runner_call_id, usage=usage, status=status,
                failure_kind=failure_kind,
                transcript_ref=transcript_ref or str(heartbeat.path))
        except (BudgetExhausted, CostAccountingFailed):
            raise
        except Exception:                      # noqa: BLE001 —— 预算开启时必须 fail-closed
            logger.error("judge 成本记账失败 (cycle=%s kind=%s attempt=%s)",
                         cycle_id, review_kind, attempt + 1, exc_info=True)
            if self._cost_required:
                raise
        if heartbeat_error is not None:
            raise heartbeat_error

    # -- 落库（短事务；runner_call + ledger + DECISION 同生共死）-----------------
    def _record(self, cycle_id: str, bt_id: int, kind: str, subject_hash: str,
                verdict: Dict[str, Any], usage, *, call=None,
                transcript_ref: Optional[str] = None,
                execution_receipt_ref: Optional[str] = None) -> int:
        """最终裁决把预调用 intent + ledger + DECISION 原子收口；不另造 runner_call。"""
        ci = _cnum(cycle_id)
        budget_hit = None
        if self.cost_ledger is not None and call is None:
            raise RuntimeError("有效 judge verdict 缺预调用 runner_call")
        if call is not None:
            rc, heartbeat = call
            try:
                heartbeat.finish("success", execution_receipt_ref=execution_receipt_ref)
            except Exception as error:
                self.cost_ledger.finish_call(
                    runner_call_id=rc, status="failed", usage=usage,
                    failure_kind="heartbeat_failed",
                    transcript_ref=transcript_ref or str(heartbeat.path))
                raise error
        try:
            with self.daemon.transaction() as conn:
                round_no = conn.execute(
                    "SELECT COUNT(*)+1 FROM decision WHERE actor='judge' AND type=? AND json_valid(payload_json) "
                    "AND json_extract(payload_json,'$.build_target_id')=?", (kind, bt_id)).fetchone()[0]
                if self.cost_ledger is not None:
                    budget_hit = self.cost_ledger.finish_call_in_txn(
                        conn, runner_call_id=rc, status="success", usage=usage,
                        transcript_ref=transcript_ref or str(heartbeat.path))
                else:
                    rc = conn.execute(
                        "INSERT INTO runner_call(cycle_id,phase,purpose,status,transcript_ref,"
                        "started_at,finished_at) VALUES (?,'audit',?,'success',?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
                        (ci, kind, transcript_ref)).lastrowid
                conn.execute("INSERT INTO decision(cycle_id,actor,type,payload_json) VALUES (?,'judge',?,?)",
                             (ci, kind, json.dumps(
                                 {"build_target_id": bt_id, "review_kind": kind, "round_no": round_no,
                                  "verdict": verdict["verdict"], "issues": verdict.get("issues", []),
                                  "notes_md": verdict.get("notes_md", ""), "subject_hash": subject_hash,
                                  "runner_call_id": rc, "policy_hash": self.policy_hash}, ensure_ascii=False)))
        except Exception as e:
            if self.cost_ledger is not None:
                # verdict/DECISION 后处理失败：原事务已回滚，复用同一真实调用 intent 记失败与成本。
                self.cost_ledger.finish_call(
                    runner_call_id=rc, status="failed", usage=usage,
                    failure_kind="postprocess_error",
                    transcript_ref=transcript_ref or str(heartbeat.path))
            raise
        if budget_hit is not None:              # runner_call+ledger+DECISION+global_stop 已提交后才阻断后续调用
            raise BudgetExhausted(**budget_hit)
        return rc

    # -- subject 材料装配（编排器机械拼装，零推理；judge 只读它）-----------------
    def _subject_md(self, cycle_id: str, bt_id: int, kind: str) -> str:
        """code review：切片 + 物化代码全清单 + smoke transcript。result review：**代码也在场**（判据
        「据结果反查代码」，codex BLOCKER）+ smoke + metric_value 行**全量显式列出**（不受 log tail 截断）
        + train/eval log 尾部 + checkpoint 哈希 + identity。"""
        d = self.daemon
        row = d.query_one(
            "SELECT plan_ref,target_kind,baseline_id FROM build_target WHERE id=?", (bt_id,))
        slice_md = row[0] if row and row[0] else "（无 plan_ref）"
        if row is None:
            raise ValueError(f"judge build_target {bt_id} 不存在")
        if row[1] == "import":
            selected_rows = d.query(
                "SELECT id FROM external_import WHERE baseline_id=? "
                "AND action='selected_for_materialization' ORDER BY id", (row[2],))
            if len(selected_rows) != 1:
                raise RuntimeError(
                    f"import target {bt_id} 须恰一 selected_for_materialization provenance，"
                    f"实收 {len(selected_rows)}")
            t_dir = self.work / f"import{selected_rows[0][0]}"
            src = t_dir / "clone"
        else:
            t_dir = self.work / f"c{_cnum(cycle_id)}" / f"t{bt_id}"
            src = t_dir / "src"
        parts = [f"## 评审对象（{kind}）", f"### resolved 计划切片\n```json\n{slice_md}\n```"]
        for p in sorted(src.rglob("*")):        # 两种评审都给代码（result review 须能据结果反查代码）
            if p.is_file() and p.name != "_staged.ok":
                parts.append(f"### 物化文件 {p.relative_to(src)}\n```\n"
                             f"{_tail(p.read_text(encoding='utf-8', errors='replace'), 20000)}\n```")
        smoke = _latest_smoke_log(t_dir / "smoke")
        if smoke is not None:
            parts.append("### smoke transcript（最新）\n```\n"
                         + _tail(smoke.read_text(encoding="utf-8", errors="replace")) + "\n```")
        if kind == "bundle_result_review":      # 结果材料（log 仅供评审读，不作证据）
            rrow = d.query_one("SELECT id FROM run WHERE build_target_id=? AND status='success' "
                               "ORDER BY id DESC", (bt_id,))
            rid = rrow[0] if rrow else None
            if rid is not None:
                ck = d.query_one("SELECT ckpt_key, content_hash FROM checkpoint WHERE produced_by_run=?", (rid,))
                if ck:
                    parts.append(f"### checkpoint\n{ck[0]} sha256={ck[1]}")
                attempt = d.query_one(
                    "SELECT id,attempt_no FROM evaluation_attempt WHERE build_target_id=? "
                    "ORDER BY attempt_no DESC LIMIT 1", (bt_id,))
                ev = (t_dir / f"eval{rid}" / "eval.log" if attempt is None or attempt[1] == 1 else
                      t_dir / f"eval{rid}" / f"retry-a{attempt[0]}" / "eval.log")
                if ev.exists():                 # metric 行全量显式列出（不受下方 tail 截断，codex BLOCKER）
                    mlines = [ln for ln in ev.read_text(encoding="utf-8", errors="replace").splitlines()
                              if ln.strip().startswith("metric_value:")]
                    parts.append("### metric_value 行（全量）\n```\n" + "\n".join(mlines) + "\n```")
                for name, p in (("train", t_dir / f"run{rid}" / "train.log"),
                                ("eval", ev)):
                    if p.exists():
                        parts.append(f"### {name}.log（尾部）\n```\n"
                                     + _tail(p.read_text(encoding="utf-8", errors="replace")) + "\n```")
            else:
                target = d.query_one(
                    "SELECT target_kind,variant_id FROM build_target WHERE id=?", (bt_id,))
                if target is not None and target[0] == "eval":
                    for ck_id, ck_key, ck_hash in d.query(
                            "SELECT id,ckpt_key,content_hash FROM checkpoint "
                            "WHERE variant_id=? ORDER BY id", (target[1],)):
                        parts.append(f"### checkpoint ck{ck_id}\n{ck_key} sha256={ck_hash}")
                    attempt = d.query_one(
                        "SELECT id FROM evaluation_attempt WHERE build_target_id=? "
                        "ORDER BY attempt_no DESC LIMIT 1", (bt_id,))
                    if attempt is not None:
                        ev = t_dir / f"eval-a{attempt[0]}" / "eval.log"
                        if ev.exists():
                            text = ev.read_text(encoding="utf-8", errors="replace")
                            mlines = [line for line in text.splitlines()
                                      if line.strip().startswith("metric_value:")]
                            parts.append(
                                "### metric_value 行（全量）\n```\n" + "\n".join(mlines) + "\n```")
                            parts.append(
                                "### eval.log（尾部）\n```\n" + _tail(text) + "\n```")
        return "\n\n".join(parts)
