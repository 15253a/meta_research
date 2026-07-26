"""StageProvider / JudgeProvider —— 真 CodexRunner 的生产装配。

**为什么要它**：M0 driver（driver.py）用真 Codex 但跑在**桩栈**（StubGate/StubStateStore/StubCompiler +
造假 bundle）——那是 M0 验收栈。M3+ 的真组件（SqliteAdvancer/AttackStages）消费**注入式** provider
回调（生产=真 Codex 会话、测试=确定性替身）。生产研究路径中，每个 cycle 的 Idea、Plan、Reasoning
各有一个逻辑常驻主 Codex thread；Bundle 则由一个图级 Scheduler task 与每 target 一个固定 Worker
task 协作，ready target 可并发产包、smoke/train/eval、观察日志和工程修复。耐久 provider id 只用于
进程灾难后找回原 task，不是新建替代上下文的机制。

**provider 契约**（attack_stages 模块注释 / advancer reasoning_provider）：
- StageProvider（产文件四阶段）：
  - idea(cyc, pack) → {"idea_set.json": …}
  - plan(cyc, pack) → {"plan.json": …}（冻结 plan.schema 抽象形态；命令不在 plan）
  - bundle(cyc, pack) → {"execution_manifest.json": …, "identity.md": str, <代码文件passthrough…>}
  - bundle 主 turn 在提交包后继续通过 runtime MCP 的
    bundle_execute/bundle_status/bundle_replan 执行、观察、修复或收口；旧
    bundle_operator(cyc, pack, control) 仅保留给显式隔离资格测试
  - reasoning(cyc, pack) → {"selection.json": …, "tree_ops.json"?: …, "answer.json"?: …}
- 正常研究路径的 Plan/Bundle 与 bypass Idea reviewer 由阶段主智能体在当前 turn 内启动干净
  子智能体；WildIdea generation/audit 是 Idea-stage 内部 capability，主智能体吸收其机械 merge。
  PlanReviewProvider/JudgeProvider 仅保留给隔离资格测试和兼容装配。

**阶段提交**：正常研究路径通过 runtime MCP 的 ``submit_stage_artifact`` 在主 turn 内完成文件闭包、
schema、cycle/target 身份与 Bundle 冻结计划校验。错误直接返回给同一主智能体修正；成功后大产物留在
file manager，SQLite 只保存路径/哈希索引。核心 question/baseline/阶段事务仍由原 gate 提交。

**职责边界**：本类通常只保证「产出**结构合法**（过 schema）的 files」；**语义**由组件把关——reasoning-only
轮的 create_root/add_children 必产、answer 时序、manifest↔plan 切片交叉核等由 advancer/attack_stages 校验
（§4.2.3/§4.2.5；attack_stages._check_manifest）。故本类不判 answer_allowed 等轮型语义，只校验**在场**
产物的 schema + 阶段必产文件在场。

**pack 已由调用方渲染**：attack_stages/advancer 先 compiler.render 再传入 pack；本类**不重渲**。
MCP 可修错误和 Bundle 运行反馈都在同一个 turn 内闭环；只有宿主进程灾难才按原
provider id 恢复，没有 id 时 fail closed，绝不新建主 thread 当重试。

**长操作零事务（§6.13）**：Codex 子进程 + 纯计算不持写事务；JudgeProvider 仅在产物过校验后以一个
**短事务**落 runner_call+DECISION（评审裁决入账）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .cost_ledger import BudgetExhausted, CostAccountingFailed
from .bundle_tasks import BundleTaskRegistry
from .harness import latest_smoke_log as _latest_smoke_log
from .ids import cnum as _cnum
from .interfaces import BundleReplanRequired, ContextPack, StageBlockedOnResources
from .import_search import ImportSearchError, validate_import_search_request
from .notify import FileRequestReject
from .process_supervisor import atomic_write_receipt
from .provider_invocation import load_provider_invocation_receipt
from .runner import RunnerError

logger = logging.getLogger(__name__)

_RECONCILE_PROTOCOL = "runner-call-v1"
# An injected runner is trusted code, but cycle-scoped Bundle persistence still has to
# be an explicit, exact capability rather than something inferred from a method
# name.  Tool availability follows the runtime profile; official execution and
# state authority remain orchestrator-only, and continuation is recovered only
# from a durable provider invocation id.
BUNDLE_OPERATOR_SESSION_CONTRACT = (
    "bundle-operator-persistent-session-v2:"
    "runtime-tools:orchestrator-execution-only:durable-provider-id")

# Idea/Plan/Reasoning are also logical resident workers.  MCP submission
# correction, import-search refresh and semantic feedback happen before the one
# normal provider turn exits.  Only process-disaster recovery may resume the same
# top-level Codex thread; it must never silently create a replacement worker.
STAGE_MAIN_SESSION_CONTRACT = (
    "stage-main-persistent-session-v1:"
    "one-cycle-one-stage-one-provider-thread:durable-provider-id")

# Bundle is no longer one cycle-wide target author.  The graph Scheduler and
# every target Worker are separate durable provider tasks with disjoint runtime
# capabilities.  This declaration is checked on the injected factory and each
# produced runner before any provider session is created or resumed.
BUNDLE_TASK_SESSION_CONTRACT = (
    "bundle-task-persistent-session-v1:"
    "one-cycle-one-scheduler:one-target-one-worker:durable-provider-id")


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
_IDEA_GENERATION_FILES = {
    "required": ["idea_set.draft.json"], "optional": [], "exact": True}
_IDEA_AUDIT_FILES = {
    "required": ["idea_audit.json"], "optional": [], "exact": True}
_BUNDLE_OPERATOR_FILES = {
    "required": ["bundle_operator_action.json"], "optional": [], "exact": True}
_BUNDLE_SCHEDULER_FILES = {
    "required": [], "optional": [], "exact": True}
# skill 调用点说明（让工人聚焦本阶段；仿 driver._SKILL_SECTION）
_CALL_NOTE = {
    "idea": "执行【生成任务】+【判官任务】，产 idea_set.json（候选全集 + selected_id）",
    "plan": "执行【计划任务】：必要时在本 turn 调用 plan_import_search 刷新候选，最终只提交 plan.json",
    "bundle": "按锚区「本目标」切片产可执行包：execution_manifest.json + identity.md + 代码文件（一信封装齐）",
    "reasoning": "执行【轮尾任务】，按 route 产 selection.json（必），酌情 tree_ops.json / answer.json",
}


class StageProvider:
    def __init__(self, *, runner_factory, schemas, policy: Dict[str, Any],
                 system_prompt: str, skills: Dict[str, str], work_root: str,
                 file_request_bridge=None, cost_ledger=None,
                 wildidea_adapter=None, idea_audit_pack_builder=None,
                 require_wildidea_provider_binding: bool = False,
                 replay_archive=None, bundle_operator_mode: bool = False,
                 bundle_operator_guard=None,
                 inline_subagent_review: bool = False,
                 resident_stage_sessions: bool = False):
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
        if not isinstance(inline_subagent_review, bool):
            raise ValueError("inline_subagent_review 须为 bool")
        self.inline_subagent_review = inline_subagent_review
        if not isinstance(resident_stage_sessions, bool):
            raise ValueError("resident_stage_sessions 须为 bool")
        if resident_stage_sessions and cost_ledger is None:
            raise ValueError("常驻阶段主会话须依赖耐久 runner_call/provider receipt 账本")
        resident_contract = getattr(
            runner_factory, "stage_main_session_contract", None)
        if (resident_stage_sessions
                and resident_contract != STAGE_MAIN_SESSION_CONTRACT):
            raise ValueError("resident runner_factory 未声明精确阶段主会话合同")
        self.resident_stage_sessions = resident_stage_sessions
        retry_policy = policy["flow"]["retry"]
        self.review_rounds = {
            "idea": int(retry_policy.get("plan_review", 0)),
            "plan": int(retry_policy.get("plan_review", 0)),
            "bundle_code": int(retry_policy.get("bundle_code_review", 0)),
            "bundle_result": int(retry_policy.get("bundle_result_review", 0)),
        }
        self.gpu_target_policy = policy.get("resources", {}).get(
            "gpu_target_policy", "planner_select")
        # Optional file-side outbox for exact ContextPack/artifact/handoff replay.
        # It never writes SQLite and therefore stays outside stage DB transactions.
        self.replay_archive = replay_archive
        if not isinstance(bundle_operator_mode, bool):
            raise ValueError("bundle_operator_mode 须为 bool")
        if bundle_operator_mode and cost_ledger is None:
            raise ValueError(
                "bundle operator 须依赖耐久 runner_call/provider receipt 成本账本")
        factory_contract = getattr(
            runner_factory, "bundle_operator_session_contract", None)
        if (bundle_operator_mode
                and factory_contract != BUNDLE_OPERATOR_SESSION_CONTRACT):
            raise ValueError(
                "bundle operator runner_factory 未明确声明精确持久会话/"
                "runtime-tools 合同")
        if bundle_operator_guard is not None and not callable(bundle_operator_guard):
            raise ValueError("bundle_operator_guard 须为 callable 或 None")
        self.bundle_operator_mode = bundle_operator_mode
        self.bundle_operator_guard = bundle_operator_guard or (lambda: None)
        # The adapter path remains for explicit compatibility/qualification
        # assembly. Normal research uses the resident Idea main-agent branch:
        # WildIdea owns its internal blind audit; bypass owns native review.
        self.wildidea_adapter = wildidea_adapter
        self.idea_audit_pack_builder = idea_audit_pack_builder
        self.require_wildidea_provider_binding = require_wildidea_provider_binding
        if not isinstance(require_wildidea_provider_binding, bool):
            raise ValueError("require_wildidea_provider_binding 须为 bool")
        if require_wildidea_provider_binding and wildidea_adapter is None:
            raise ValueError("缺 WildIdea adapter 时不能要求 provider binding")
        if ((self.wildidea_adapter is None)
                != (self.idea_audit_pack_builder is None)):
            raise ValueError(
                "WildIdea adapter 与 question-only idea audit pack builder 必须成对注入")
        self._resident_idea_lock = threading.RLock()
        self._resident_idea_contexts: Dict[
            tuple[str, str], Dict[str, Any]] = {}
        if (self.wildidea_adapter is not None
                and self.resident_stage_sessions
                and self.inline_subagent_review):
            self.wildidea_adapter.bind_resident_controller(self)
        self.cost_ledger = cost_ledger         # None 仅允许 session_max=null 的显式诊断/测试装配
        self._cost_required = policy.get("budget", {}).get("session_max") is not None
        if self._cost_required and self.cost_ledger is None:
            raise ValueError("budget.session_max 已启用，StageProvider 必须注入 cost_ledger；"
                             "测试/诊断须显式设 session_max=null")
        self._call_seq = 0                     # 实例内可读 purpose；耐久文件身份由 runner_call_id 提供

    # -- provider 回调（绑定阶段）------------------------------------------------
    def idea(self, cyc, pack) -> Dict[str, Any]:
        if self.inline_subagent_review:
            # One live main Codex owns the whole Idea stage.  WildIdea's
            # generation/audit workers stay behind its MCP capability; only a
            # server-bound bypass route uses native child review.
            active_key = (str(cyc.cycle_id), str(pack.pack_hash))
            if self.wildidea_adapter is not None and self.resident_stage_sessions:
                with self._resident_idea_lock:
                    if active_key in self._resident_idea_contexts:
                        raise RuntimeError(
                            "resident Idea ContextPack 已有活动主会话")
                    self._resident_idea_contexts[active_key] = {
                        "cyc": cyc, "pack": pack,
                        "generation_path": None,
                        "generation_pack": None,
                    }
            try:
                return self._produce(
                    cyc, pack, stage="idea",
                    purpose_tag=(
                        f"idea-main-c{_cnum(cyc.cycle_id)}"
                        if self.resident_stage_sessions else None),
                    skill=self._main_stage_review_skill(
                        "idea", self.skills["idea"], "idea",
                        self.review_rounds["idea"]))
            finally:
                if (self.wildidea_adapter is not None
                        and self.resident_stage_sessions):
                    with self._resident_idea_lock:
                        self._resident_idea_contexts.pop(active_key, None)
        if self.wildidea_adapter is None:
            return self._produce(cyc, pack, stage="idea")

        generation_pack, generation_skill = self.wildidea_adapter.prepare_generation(
            pack, self.skills["idea"])
        def validate_draft(files: Dict[str, Any]) -> Optional[str]:
            errors = self.wildidea_adapter.validate_draft(
                files["idea_set.draft.json"])
            return ("idea_set.draft.json adapter 闭包校验失败:\n" + "\n".join(errors)
                    if errors else None)

        def bind_generation_invocation(artifact, runner_call_id) -> None:  # noqa: ANN001
            if (artifact.prompt_sha256 is None
                    and artifact.provider_receipt_ref is None):
                return
            self.wildidea_adapter.bind_accepted_invocation(
                generation_pack, role="generation",
                runner_call_id=runner_call_id,
                prompt_sha256=artifact.prompt_sha256,
                provider_receipt_ref=artifact.provider_receipt_ref,
                execution_receipt_ref=artifact.execution_receipt_ref)

        draft_files = self._produce(
            cyc, generation_pack, stage="idea", phase="idea",
            purpose_tag="idea-generate", spec=_IDEA_GENERATION_FILES,
            skill=generation_skill,
            append_call_note=False,
            post_validate=validate_draft,
            on_accept=bind_generation_invocation)
        draft = draft_files["idea_set.draft.json"]

        audit_source_pack = self.idea_audit_pack_builder(cyc.cycle_id)
        audit_pack, audit_skill = self.wildidea_adapter.prepare_audit(
            audit_source_pack, draft, self.skills["idea"],
            generation_pack=generation_pack)

        def validate_audit(files: Dict[str, Any]) -> Optional[str]:
            errors = self.wildidea_adapter.validate_audit(
                draft, files["idea_audit.json"])
            return ("idea_audit.json 候选闭包校验失败:\n" + "\n".join(errors)
                    if errors else None)

        def bind_judge_invocation(artifact, runner_call_id) -> None:  # noqa: ANN001
            if (artifact.prompt_sha256 is None
                    and artifact.provider_receipt_ref is None):
                return
            self.wildidea_adapter.bind_accepted_invocation(
                generation_pack, role="judge",
                runner_call_id=runner_call_id,
                prompt_sha256=artifact.prompt_sha256,
                provider_receipt_ref=artifact.provider_receipt_ref,
                execution_receipt_ref=artifact.execution_receipt_ref)

        audit_files = self._produce(
            cyc, audit_pack, stage="idea", phase="audit",
            purpose_tag="idea-audit", spec=_IDEA_AUDIT_FILES,
            skill=audit_skill,
            append_call_note=False,
            post_validate=validate_audit,
            on_accept=bind_judge_invocation,
            allow_resource_request=False)
        merged = self.wildidea_adapter.merge(
            draft, audit_files["idea_audit.json"],
            generation_pack=generation_pack,
            base_skill=self.skills["idea"],
            require_invocation_binding=self.require_wildidea_provider_binding)
        final_errors = self._schema_errors("idea_set.json", merged)
        if final_errors:
            # Adapter merge is deterministic local code.  A failure here cannot
            # be repaired by spending another model call and indicates a release
            # contract bug or corrupted vendored assets.
            raise RunnerError(
                "WildIdea adapter 合并后的 idea_set.json 非法:\n"
                + "\n".join(final_errors[:8]),
                failure_kind="adapter_contract")
        if self.replay_archive is not None:
            # The two model turns were archived by _produce.  The merge is a
            # deterministic adapter projection, so publish the canonical idea
            # artifact without inventing a third conversational handoff.
            self.replay_archive.persist_stage_output(
                cycle_id=cyc.cycle_id, stage="idea",
                files={"idea_set.json": merged}, md="", purpose="idea",
                pack_hash=generation_pack.pack_hash, handoff=False,
                provenance={"derived_by": "wildidea_adapter.merge"})
        return {"idea_set.json": merged}

    def _resident_idea_context(self, scope) -> Dict[str, Any]:  # noqa: ANN001
        """Return the exact active resident Idea pack for one MCP capability."""
        if (getattr(scope, "stage", None) != "idea"
                or getattr(scope, "target_id", None) is not None):
            raise RuntimeError("resident WildIdea scope 必须属于 Idea 主阶段")
        key = (
            str(getattr(scope, "cycle_id", "")),
            str(getattr(scope, "pack_hash", "")),
        )
        with self._resident_idea_lock:
            context = self._resident_idea_contexts.get(key)
        if context is None:
            raise RuntimeError(
                "resident WildIdea scope 未绑定当前 Idea ContextPack")
        return context

    def prepare_resident_wildidea(
            self, scope, *, need_innovation: bool) -> Dict[str, Any]:
        """Run the chosen WildIdea generation behind the resident capability."""
        if self.wildidea_adapter is None:
            raise RuntimeError("resident WildIdea adapter 未装配")
        context = self._resident_idea_context(scope)
        expected_path = "wildidea" if need_innovation else "bypass"
        with self._resident_idea_lock:
            prior = context["generation_path"]
            if prior is not None and prior != expected_path:
                raise RuntimeError("resident WildIdea generation_path 不得重绑定")
            if prior is not None:
                return json.loads(json.dumps(
                    context["expand_result"], ensure_ascii=False))

        generation_pack = None
        draft = None
        if need_innovation:
            generation_pack, generation_skill = (
                self.wildidea_adapter.prepare_generation(
                    context["pack"], self.skills["idea"]))
            generation_skill += (
                "\n\n===== Resident WildIdea route binding =====\n"
                "当前服务端已冻结 need_innovation=true、"
                "generation_path=wildidea。你是 capability 内部 generator，"
                "不得重新判 NEED，不得调用 wildidea_expand、wildidea_audit、"
                "prepare_review 或 submit_stage_artifact；只按上方 adapter ABI "
                "产出 exact idea_set.draft.json。")

            def validate_draft(files: Dict[str, Any]) -> Optional[str]:
                errors = self.wildidea_adapter.validate_draft(
                    files["idea_set.draft.json"])
                return (
                    "idea_set.draft.json adapter 闭包校验失败:\n"
                    + "\n".join(errors) if errors else None)

            def bind_generation_invocation(
                    artifact, runner_call_id) -> None:  # noqa: ANN001
                if (artifact.prompt_sha256 is None
                        and artifact.provider_receipt_ref is None):
                    return
                self.wildidea_adapter.bind_accepted_invocation(
                    generation_pack, role="generation",
                    runner_call_id=runner_call_id,
                    prompt_sha256=artifact.prompt_sha256,
                    provider_receipt_ref=artifact.provider_receipt_ref,
                    execution_receipt_ref=artifact.execution_receipt_ref)

            draft_files = self._produce(
                context["cyc"], generation_pack, stage="idea", phase="idea",
                purpose_tag="idea-generate-internal",
                spec=_IDEA_GENERATION_FILES,
                skill=generation_skill, append_call_note=False,
                post_validate=validate_draft,
                on_accept=bind_generation_invocation,
                allow_resource_request=False,
                internal_stage_capability=True)
            draft = json.loads(json.dumps(
                draft_files["idea_set.draft.json"], ensure_ascii=False,
                sort_keys=True, separators=(",", ":"), allow_nan=False))
        result = self.wildidea_adapter.expand_for_tool(
            pack_hash=context["pack"].pack_hash,
            need_innovation=need_innovation)
        result = {**result, "generation_path": expected_path}
        if draft is not None:
            result["draft"] = draft
        with self._resident_idea_lock:
            if context["generation_path"] not in {None, expected_path}:
                raise RuntimeError("resident WildIdea generation_path 并发漂移")
            context["generation_path"] = expected_path
            context["generation_pack"] = generation_pack
            context["generation_draft"] = draft
            context["expand_result"] = json.loads(json.dumps(
                result, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False))
        return result

    def audit_resident_wildidea(
            self, scope, *, draft: Dict[str, Any]) -> Dict[str, Any]:
        """Run WildIdea's blind audit internally and return its exact merge."""
        if self.wildidea_adapter is None:
            raise RuntimeError("resident WildIdea adapter 未装配")
        context = self._resident_idea_context(scope)
        with self._resident_idea_lock:
            if context["generation_path"] != "wildidea":
                raise RuntimeError(
                    "wildidea_audit 只属于服务端绑定的 WildIdea 路径")
            if context.get("audit_complete"):
                raise RuntimeError("resident WildIdea internal audit 已完成")
            generation_pack = context["generation_pack"]
            generation_draft = context.get("generation_draft")
        if generation_pack is None:
            raise RuntimeError("resident WildIdea 缺 generation preparation")
        if generation_draft is None:
            raise RuntimeError("resident WildIdea 缺服务端 generation draft")
        try:
            supplied_draft_json = json.dumps(
                draft, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False)
            generation_draft_json = json.dumps(
                generation_draft, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as error:
            raise RuntimeError(
                "wildidea_audit draft 不是有限 JSON") from error
        if supplied_draft_json != generation_draft_json:
            raise RuntimeError(
                "wildidea_audit 只接受服务端生成的 exact draft")
        authoritative_draft = json.loads(generation_draft_json)

        audit_source_pack = self.idea_audit_pack_builder(
            context["cyc"].cycle_id)
        audit_pack, audit_skill = self.wildidea_adapter.prepare_audit(
            audit_source_pack, authoritative_draft, self.skills["idea"],
            generation_pack=generation_pack)

        def validate_audit(files: Dict[str, Any]) -> Optional[str]:
            errors = self.wildidea_adapter.validate_audit(
                authoritative_draft, files["idea_audit.json"])
            return ("idea_audit.json 候选闭包校验失败:\n" + "\n".join(errors)
                    if errors else None)

        def bind_judge_invocation(artifact, runner_call_id) -> None:  # noqa: ANN001
            if (artifact.prompt_sha256 is None
                    and artifact.provider_receipt_ref is None):
                return
            self.wildidea_adapter.bind_accepted_invocation(
                generation_pack, role="judge",
                runner_call_id=runner_call_id,
                prompt_sha256=artifact.prompt_sha256,
                provider_receipt_ref=artifact.provider_receipt_ref,
                execution_receipt_ref=artifact.execution_receipt_ref)

        audit_files = self._produce(
            context["cyc"], audit_pack, stage="idea", phase="audit",
            purpose_tag="idea-audit-internal", spec=_IDEA_AUDIT_FILES,
            skill=audit_skill, append_call_note=False,
            post_validate=validate_audit,
            on_accept=bind_judge_invocation,
            allow_resource_request=False,
            internal_stage_capability=True)
        merged = self.wildidea_adapter.merge(
            authoritative_draft, audit_files["idea_audit.json"],
            generation_pack=generation_pack,
            base_skill=self.skills["idea"],
            require_invocation_binding=self.require_wildidea_provider_binding)
        final_errors = self._schema_errors("idea_set.json", merged)
        if final_errors:
            raise RunnerError(
                "resident WildIdea merge 后 idea_set.json 非法:\n"
                + "\n".join(final_errors[:8]),
                failure_kind="adapter_contract")
        internal_provenance = merged.pop("provenance", {})
        result = {
            "idea_set": merged,
            "internal_provenance": internal_provenance,
        }
        with self._resident_idea_lock:
            context["audit_complete"] = True
        return json.loads(json.dumps(result, ensure_ascii=False))

    def plan(self, cyc, pack) -> Dict[str, Any]:
        return self._produce(
            cyc, pack, stage="plan",
            purpose_tag=(
                f"plan-main-c{_cnum(cyc.cycle_id)}"
                if self.resident_stage_sessions else None),
            skill=self._main_stage_review_skill(
                "plan", self.skills["plan"], "plan",
                self.review_rounds["plan"]))

    def bundle(self, cyc, pack) -> Dict[str, Any]:
        if pack.target_id is None:
            raise ValueError("bundle ContextPack 缺 target_id")
        return self._produce(
            cyc, pack, stage="bundle",
            purpose_tag=(
                f"bundle-main-c{_cnum(cyc.cycle_id)}"
                if self.resident_stage_sessions else
                f"bundle-c{_cnum(cyc.cycle_id)}-t{pack.target_id}"),
            skill=self._main_stage_review_skill(
                "bundle", self.skills["bundle"], "bundle_code",
                self.review_rounds["bundle_code"]))

    def bundle_scheduler(self, cyc, pack) -> Dict[str, Any]:
        """Run the one graph-level Scheduler task for this cycle.

        Scheduler state is authoritative in SQL/controller projections.  The
        model task only calls the bounded overview/dispatch/wait/drain tools
        and therefore returns no target artifact.
        """
        if pack.target_id is not None:
            raise ValueError("Bundle Scheduler ContextPack 不得绑定 target")
        if not self.resident_stage_sessions:
            raise RuntimeError("Bundle Scheduler 必须使用耐久 resident task")
        cycle_id = f"c{_cnum(cyc.cycle_id)}"
        registry = self._bundle_task_registry()
        registry.reconcile_terminal_workers(cycle_id)
        try:
            return self._produce(
                cyc, pack, stage="bundle",
                purpose_tag=f"bundle-scheduler-c{_cnum(cyc.cycle_id)}",
                spec=_BUNDLE_SCHEDULER_FILES,
                skill=self.skills.get(
                    "bundle_scheduler", self.skills["bundle"]),
                call_note=(
                    "只调度 ready frontier 并等待/排空 Worker；"
                    "不得编写、提交或执行 target 代码"),
                allow_resource_request=False,
                bundle_task_role="bundle_scheduler")
        finally:
            # A Worker can commit its target and then lose the host process
            # before sealing the task ledger.  Scheduler boundaries are the
            # production recovery seam because they already bracket graph
            # dispatch/drain and carry no injected target artifact authority.
            registry.reconcile_terminal_workers(cycle_id)

    def bundle_worker(self, cyc, pack) -> Dict[str, Any]:
        """Run or recover the stable resident Worker for one build target."""
        try:
            target_id = int(pack.target_id)
        except (TypeError, ValueError) as error:
            raise ValueError(
                "Bundle Target Worker ContextPack 缺合法 target") from error
        if target_id <= 0:
            raise ValueError("Bundle Target Worker target 须为正整数")
        if not self.resident_stage_sessions:
            raise RuntimeError("Bundle Target Worker 必须使用耐久 resident task")
        cycle_id = f"c{_cnum(cyc.cycle_id)}"
        registry = self._bundle_task_registry()
        task = registry.prepare_worker(cycle_id, target_id=target_id)
        worker_task = (
            registry, cycle_id, target_id, task.provider_task_id)
        try:
            result = self._produce(
                cyc, pack, stage="bundle",
                purpose_tag=(
                    f"bundle-worker-c{_cnum(cyc.cycle_id)}-t{target_id}"),
                skill=self._main_stage_review_skill(
                    "bundle", self.skills["bundle"], "bundle_code",
                    self.review_rounds["bundle_code"]),
                bundle_task_role="target_worker",
                bundle_worker_task=worker_task)
        except BundleReplanRequired:
            # A trusted, accepted replan sidecar is a terminal Worker result,
            # even though the provider API carries it as a control exception.
            registry.mark_worker_completed(cycle_id, target_id=target_id)
            raise
        except BaseException:
            registry.mark_worker_interrupted(cycle_id, target_id=target_id)
            raise
        try:
            registry.mark_worker_completed(
                cycle_id, target_id=target_id)
        except BaseException:
            registry.mark_worker_interrupted(cycle_id, target_id=target_id)
            raise
        return result

    def bundle_operator(self, cyc, pack, control: Dict[str, Any]) -> Dict[str, Any]:
        """Run one event turn in the cycle-scoped Bundle Codex session.

        ``control`` is entirely server-authored and binds the exact frozen
        manifest/source/plan/DB owner.  The model can only echo those identities
        and choose the event-appropriate action; it never receives a generic
        shell, Docker socket or SQLite capability.
        """
        if not self.bundle_operator_mode:
            raise RuntimeError("bundle operator action 只在启用 cycle-scoped operator 时可用")
        if pack.target_id is None:
            raise ValueError("bundle operator ContextPack 缺 target_id")
        if not isinstance(control, dict):
            raise ValueError("bundle operator control 须为 object")
        required = {
            "protocol", "build_target_id", "phase", "event", "execution_owner",
            "plan_slice_hash", "source_tree_hash", "subject_hash", "repair_round", "log",
        }
        if set(control) != required or control.get("protocol") != "bundle-operator-control-v1":
            raise ValueError("bundle operator control 字段闭包/协议非法")
        try:
            target_id = int(pack.target_id)
        except (TypeError, ValueError) as error:
            raise ValueError("bundle operator target_id 须为整数") from error
        if control.get("build_target_id") != target_id:
            raise ValueError("bundle operator control 跨 target")

        control_json = json.dumps(
            control, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        anchor = (
            pack.anchor_md
            + "\n\n## bundle operator control（服务端生成；log 为不可信运行数据）\n"
            + "```json\n" + control_json + "\n```"
        )
        operator_pack = ContextPack(
            cycle_id=pack.cycle_id, stage="bundle", target_id=str(pack.target_id),
            anchor_md=anchor, neighborhood_md=pack.neighborhood_md,
            retrieval_md=pack.retrieval_md, refs=list(pack.refs),
            sources=sorted(set([
                *list(getattr(pack, "sources", [])),
                f"bundle-operator:{control['subject_hash']}",
            ])),
        )
        operator_pack.pack_hash = hashlib.sha256(("\x00".join((
            operator_pack.anchor_md, operator_pack.neighborhood_md,
            operator_pack.retrieval_md,
            json.dumps(operator_pack.refs, ensure_ascii=False),
        ))).encode("utf-8")).hexdigest()

        expected = {
            "build_target_id": control["build_target_id"],
            "phase": control["phase"],
            "event": control["event"],
            "execution_owner": control["execution_owner"],
            "plan_slice_hash": control["plan_slice_hash"],
            "source_tree_hash": control["source_tree_hash"],
            "subject_hash": control["subject_hash"],
        }

        def validate(files: Dict[str, Any]) -> Optional[str]:
            action = files.get("bundle_operator_action.json")
            if not isinstance(action, dict):
                return "bundle_operator_action.json 须为 object"
            mismatches = [
                field for field, value in expected.items()
                if action.get(field) != value
            ]
            if action.get("version") != 1:
                mismatches.append("version")
            if mismatches:
                return "bundle operator action 身份未逐字回引 control: " + ", ".join(mismatches)
            if (control["event"] == "terminal"
                    and control["log"].get("exit_code") not in (None, 0)
                    and action.get("action") not in {"repair", "replan"}):
                return "非零退出的 terminal action 只能 repair 或 replan"
            return None

        subject_short = str(control["subject_hash"]).removeprefix("sha256:")[:12]
        operator_skill = self.skills.get("bundle_operator", self.skills["bundle"])
        if (self.inline_subagent_review
                and control["event"] == "terminal"
                and control["phase"] == "eval"):
            operator_skill = self._main_stage_review_skill(
                "bundle", operator_skill, "bundle_result",
                self.review_rounds["bundle_result"])
        return self._produce(
            cyc, operator_pack, stage="bundle", phase="bundle",
            purpose_tag=(f"bundle-c{_cnum(cyc.cycle_id)}-t{pack.target_id}-operator-"
                         f"{control['phase']}-{control['event']}-{subject_short}"),
            spec=_BUNDLE_OPERATOR_FILES,
            skill=operator_skill,
            call_note=("只产 bundle_operator_action.json；逐字回引服务端 control 身份，"
                       "选择当前 event 允许的 start/continue/accept/repair/replan"),
            post_validate=validate, allow_resource_request=False,
        )

    def reasoning(self, cyc, pack) -> Dict[str, Any]:
        skill = self.skills["reasoning"] + (
            "\n\n===== 本轮必须收口 =====\n"
            "Reasoning 是每个 cycle 的必经阶段，包括全部成功、工程失败、plan 不可执行、"
            "无候选和 dependency_wait。先总结本 cycle 的证据、失败与限制，再做结论和下一 cycle 决策；"
            "不得把任何分支直接终态化而跳过本阶段。若注入 meta_research_runtime MCP，"
            "在最终信封前调用 record_cycle_summary，记录 conclusion_md、decision、next_step_md"
            "及真实 evidence_refs；该索引调用不替代 selection/tree_ops/answer 的核心事务。"
        )
        return self._produce(
            cyc, pack, stage="reasoning", skill=skill,
            purpose_tag=(
                f"reasoning-main-c{_cnum(cyc.cycle_id)}"
                if self.resident_stage_sessions else None))

    def _main_stage_review_skill(
            self, stage: str, skill: str, review_kind: str, rounds: int) -> str:
        """Keep the main stage agent alive while clean child contexts review.

        This is intentionally a prompt-level autonomy contract.  The runtime
        MCP gives the main agent immediate durable feedback; the orchestrator
        no longer performs a second reviewer call or interprets/re-writes the
        review.  Core artifact schemas and question/baseline gates remain.
        """
        if not self.inline_subagent_review:
            return skill
        if stage == "idea":
            return skill + (
                "\n\n===== Idea 路径分流（服务端绑定）=====\n"
                "先调用 wildidea_expand；其返回的 generation_path 是本 turn "
                "唯一权威分流，之后不得改写。"
                "generation_path=wildidea 时，wildidea_expand 已在 Idea-stage "
                "capability 内执行 generation；只把它返回的服务端内部生成的 exact draft "
                "原样交给 wildidea_audit，不得自行生成、修订或替换 draft。该工具继续运行 "
                "WildIdea 自己的盲审并机械 merge。主智能体只吸收并原样提交工具返回的 "
                "idea_set，不得启动 native child，也不得自行改写 audit 结果。"
                "generation_path=bypass 时，不调用 wildidea_audit；主智能体对现成"
                f"方案完成精确 {rounds} 轮 native child review。每轮仍严格使用 "
                "prepare_review → fork_turns=\"none\" 的子智能体读取 "
                "read_review_input → 主智能体 dispositions/修订 → record_review；"
                "第 N 轮修订后直接提交，不增加复审。"
                "两条路径最终都调用 submit_stage_artifact；提交门只接受与服务端"
                "路径回执及对应内部 audit/native review 证据一致的最终 hash。\n"
            )
        if rounds <= 0:
            return skill
        stage_specific = ""
        if stage == "plan":
            stage_specific = (
                "先完成 plan 草稿，再让 reviewer 只检查 selected idea、该草稿与固定研究/资源约束；"
                "reviewer 不读取主智能体推理，不编辑文件。主智能体收到意见后立即修订同一份计划，"
                "外部发现必须在当前主 turn 调用 plan_import_search 并继续工作；最终只输出 plan.json。"
                "最终确定普通 plan 后调用 runtime MCP 的 submit_stage_artifact；该工具会同时检查 plan "
                "schema、固定 GPU 选择、协议/指标/预算/依赖与 baseline 身份冲突。若返回错误，直接在当前"
                "主上下文修订 plan 后重试；不要预先逐项 register_baseline，也不要等 turn 结束后才反馈。"
                "核心 baseline claim 在成功阶段回执被消费时短事务提交；exec/eval 不伪造新 baseline。"
            )
        elif review_kind == "bundle_code":
            stage_specific = (
                "你是只绑定锚区“本目标”的 Target Worker；整个 task 只能实现、执行、修复并准入"
                "自己的 target，不得调用 Scheduler 工具、不得选择、切换或推进其他 target。"
                "进入本 task 或中断恢复后，第一次 status 动作必须在实现/执行前且只调用一次 "
                "bundle_status(mode=\"snapshot\", limit=200)，并保存返回 cursor。"
                "Bundle 以每个 target 为独立评审作用域。提交实现包前，让"
                "本次新建的干净 reviewer "
                "独立检查冻结 plan 对齐、"
                "代码/manifest、依赖、GPU 使用、smoke 可启动性与输出协议；修复后用 "
                "record_review 留索引。本次代码 reviewer 完成后不复用；实验完成后的结果审查必须另启"
                "一个新的干净子智能体。提交成功后立即在当前 turn 调用 "
                "bundle_execute 异步启动；若有 bundle_execute 返回的更新 cursor，采用该更新 cursor，"
                "不得重复消费已读事件。之后只用 "
                "bundle_status(mode=\"incremental\", after_seq=<cursor>, limit=200, "
                "timeout_s=<秒>) 读取新增事实，单次 limit 绝不超过 1000。长实验的有界等待按 "
                "60→120→300→600→1800 秒递增（观测到进展后可从 60 秒重新开始），不得重复加载"
                "完整日志。中间反馈由本 Worker 就地修复。得到 eval terminal 后，以权威退出码、"
                "日志、测量和产物回执准备 "
                "bundle_result 审查并走同一套 owner 输入协议；结果候选由 owner 在正式入池前从 "
                "eval/scientific facts 生成，主智能体不得自行拼装或替换。审查完成后，主智能体明确"
                "选择再次调用 bundle_execute 继续准入，或调用 bundle_repair 重跑，或调用 "
                "bundle_replan 交 Reasoning。只有自己的 target 已达到耐久终态，且 complete 时已"
                "完成精确 admission，才允许结束本 Worker turn；不得替 Scheduler 宣告 cycle 完成。"
            )
        elif review_kind == "bundle_result":
            stage_specific = (
                "只有 owner 报告 awaiting_result_review=true 时执行本审查：每个 result candidate "
                "新启干净 reviewer，独立检查真实退出码、日志、测量与产物可信度；此时尚未正式入池，"
                "target 也尚未 complete。prepare_review 只传 review_kind；record_review 只传 "
                "review_request_id 与完整 dispositions，均不得传 files/md/workspace_files，"
                "因为 owner 会绑定不可变的 pre-admission 材料。record_review 后必须显式选择 "
                "bundle_execute（接受并继续准入）、bundle_repair（工程修复/重跑）或 "
                "bundle_replan（冻结计划问题交 Reasoning）。"
                "工程问题必须选择 repair；只有研究计划/协议本身不可执行、继续改代码或环境也无法解决时"
                "才选择 replan 交 Reasoning。"
            )
        return skill + (
            "\n\n===== 同一主智能体内的独立子智能体评审（当前配置）=====\n"
            f"主智能体在本 stage turn 内保持在线，并完成精确 {rounds} 轮 "
            f"review_kind={review_kind}。每轮都必须按以下协议执行："
            "（1）主智能体用当前候选调用 prepare_review；"
            "（2）只把返回的 review_request_id 及“调用 meta_research_runtime.read_review_input”"
            "这一指令放入 spawn_agent 的 message，以 fork_turns=\"none\" 新启恰好一个干净子智能体，"
            "不得手工复制候选或主上下文给它；"
            "（3）该子智能体必须先以 review_request_id 调用 read_review_input，独立审查其返回的"
            "权威 brief，并以严格 native-review-result-v1 JSON 作为 final；"
            "（4）主智能体等待结果，逐条形成 dispositions，在当前主上下文修订候选，再用 "
            "review_request_id、完整 dispositions 与修订后候选调用 record_review；"
            "bundle_result 按上述 owner-bound 例外不传候选。"
            "record_review 会从 live child ledger 读取 reviewer 原文，禁止由主智能体代填 verdict、"
            "summary 或 findings。每一轮均新启一个干净子智能体；完成一轮修改即计一轮，不追加隐式复审。"
            + stage_specific
            + "MCP 返回格式、身份或候选错误时，在当前主 turn 按错误修正并重试；"
              "最终信封不得夹带 review sidecar，也不得为修订再开启顶层 Codex 会话。\n"
        )

    # -- 阶段主 turn -----------------------------------------------------------
    def _produce(self, cyc, pack, *, stage: str, phase: Optional[str] = None,
                 purpose_tag: Optional[str] = None,
                 spec: Optional[Dict[str, Any]] = None,
                 skill: Optional[str] = None,
                 call_note: Optional[str] = None,
                 append_call_note: bool = True,
                 post_validate: Optional[Callable[[Dict[str, Any]], Optional[str]]] = None,
                 on_accept: Optional[Callable[[Any, Optional[int]], None]] = None,
                 allow_resource_request: bool = True,
                 internal_stage_capability: bool = False,
                 bundle_task_role: Optional[str] = None,
                 bundle_worker_task=None,
                 ) -> Dict[str, Any]:
        """Run one resident Codex turn and validate its accepted artifact.

        ``stage`` 是信封与文件所属的流程阶段；``phase`` 是成本账/runner_call 的审计阶段。
        idea 独立判官因此仍返回 envelope stage=idea，但耐久记账 phase=audit。
        Resident stages never turn a schema/precheck rejection into another
        top-level provider call: the live main agent must repair through MCP in
        this turn.  The bounded loop remains only for explicit legacy/
        qualification adapters that do not declare the resident contract.
        """
        spec = spec or _STAGE_FILES[stage]
        accounting_phase = phase or stage
        purpose_tag = purpose_tag or stage
        if self.replay_archive is not None:
            # Compiler-produced packs are already archived; this also covers
            # adapter/auditor and feedback-amended packs.  Exact duplicates are
            # byte no-ops in the archive.
            self.replay_archive.persist_context_pack(pack, label=purpose_tag)
        self._call_seq += 1
        if bundle_task_role is not None:
            if (stage != "bundle"
                    or bundle_task_role not in {
                        "bundle_scheduler", "target_worker"}):
                raise ValueError("Bundle task role/stage 组合非法")
            if ((bundle_task_role == "target_worker")
                    != (bundle_worker_task is not None)):
                raise ValueError(
                    "Bundle Worker task lifecycle 只允许 target_worker")
        elif stage == "bundle" and self.bundle_operator_mode:
            # Qualification passes assert_research_open here.  Check it before
            # even constructing a Runner, so a published claim/final seal cannot
            # create a fresh provider session or any model-facing side effect.
            self.bundle_operator_guard()
        runner = self.runner_factory(self.work / f"cycles/{cyc.cycle_id}/transcripts",
                                     f"{purpose_tag}-n{self._call_seq}")
        if bundle_task_role is not None:
            if (getattr(runner, "bundle_task_session_contract", None)
                    != BUNDLE_TASK_SESSION_CONTRACT):
                raise RuntimeError(
                    "Bundle task runner 实例持久会话能力未声明或装配后漂移")
            bind_session = getattr(runner, "bind_persistent_session", None)
            if not callable(bind_session):
                raise RuntimeError("Bundle task runner 缺 persistent session capability")
            session_id = (
                bundle_worker_task[3]
                if bundle_task_role == "target_worker"
                else self._bundle_task_session_id(
                    cyc, pack, role=bundle_task_role))
            bind_session(
                session_id=session_id,
                role=bundle_task_role)
        elif stage == "bundle" and self.bundle_operator_mode:
            if (getattr(runner, "bundle_operator_session_contract", None)
                    != BUNDLE_OPERATOR_SESSION_CONTRACT):
                raise RuntimeError(
                    "bundle operator runner 实例持久会话能力未声明或装配后漂移")
            session_id = self._bundle_operator_session_id(cyc, pack)
            bind_session = getattr(runner, "bind_persistent_session", None)
            if not callable(bind_session):
                raise RuntimeError(
                    "bundle operator runner 缺 persistent session capability")
            bind_session(
                session_id=session_id, role="bundle_operator")
        elif self.resident_stage_sessions and not internal_stage_capability:
            if (getattr(runner, "stage_main_session_contract", None)
                    != STAGE_MAIN_SESSION_CONTRACT):
                raise RuntimeError(
                    "resident runner 实例阶段主会话能力未声明或装配后漂移")
            bind_session = getattr(runner, "bind_persistent_session", None)
            if not callable(bind_session):
                raise RuntimeError("resident runner 缺 persistent session capability")
            bind_session(
                session_id=self._stage_main_session_id(cyc, stage),
                role="stage_main")
        base_skill = skill or self.skills[stage]
        if append_call_note:
            base_skill += (
                f"\n\n===== 调用点 =====\n本次调用：{call_note or _CALL_NOTE[stage]}。")
        resident_turn = bundle_task_role is not None or (
            self.resident_stage_sessions and not internal_stage_capability
        ) or (stage == "bundle" and self.bundle_operator_mode)
        max_attempts = 1 if resident_turn else self.retries + 1
        last_err = ""
        for attempt in range(max_attempts):
            skill = base_skill if not last_err else (
                base_skill + f"\n\n===== 上次产物被拒（第 {attempt} 次重试）=====\n{last_err}\n请修正后重出。")
            call = self._begin_cost_call(
                cyc, accounting_phase, runner, attempt,
                purpose_tag=purpose_tag)
            if bundle_worker_task is not None:
                registry, cycle_id, target_id, _session_id = (
                    bundle_worker_task)
                try:
                    registry.mark_worker_running(
                        cycle_id, target_id=target_id)
                except BaseException:
                    if call is not None:
                        runner_call_id, heartbeat = call
                        heartbeat.finish("aborted")
                        self.cost_ledger.abort_unstarted_call(
                            runner_call_id=runner_call_id,
                            failure_kind="worker_task_state_failed")
                    raise
            try:
                art = runner.run_task(system_prompt=self.system_prompt, skill=skill, context_pack=pack)
            except RunnerError as e:               # resident: close accounting, recover only above us
                self._record_cost(
                    cyc, accounting_phase, e.usage, status="failed", failure_kind=e.failure_kind,
                    attempt=attempt, call=call, transcript_ref=e.transcript_ref,
                    execution_receipt_ref=e.execution_receipt_ref,
                    provider_receipt_ref=e.provider_receipt_ref)
                last_err = str(e)
                if resident_turn:
                    raise
                continue
            except Exception as error:             # lifecycle/control failure：调用已放行，必须收口同一 intent
                self._record_cost(
                    cyc, accounting_phase, getattr(error, "usage", None), status="failed",
                    failure_kind=getattr(error, "failure_kind", type(error).__name__.lower()),
                    attempt=attempt, call=call,
                    transcript_ref=getattr(error, "transcript_ref", None),
                    execution_receipt_ref=(getattr(error, "execution_receipt_ref", None)
                                           or str(getattr(error, "receipt_path", "") or "") or None),
                    provider_receipt_ref=getattr(error, "provider_receipt_ref", None))
                raise
            mcp_submitted = getattr(art, "stage_submission_ref", None) is not None
            # 步⑩ CP10.2：每次真 LLM 调用都记账，但 runner_call 还要诚实区分「有效产物」与
            # 「进程成功但产物被拒」。因此各分支在结论确定后各记恰好一次；基础设施异常也会先
            # 记本次已发生的调用再 fail loud。session_max 启用时写账失败 fail-closed。
            if "resource_request.json" in art.files:
                # bundle 已消费冻结 plan；此时发现环境、依赖或规模不兼容，责任在系统内部重规划，
                # 不能伪装成“请用户上传文件”并让整个产品进入 awaiting_user。合法 sidecar 在这里
                # 作为结构化内部阻塞信号收口；AttackStages 会把目标记 engineering_blocked 后进入
                # reasoning。非法 sidecar 仍反馈给同一调用的有界产物重试。
                request_items = art.files["resource_request.json"].get("items")
                permission_request = (
                    isinstance(request_items, list) and bool(request_items)
                    and all(isinstance(item, dict) and item.get("kind") == "permission"
                            for item in request_items))
                if stage == "bundle" and not permission_request:
                    sidecar_errors = self._schema_errors_by_name(
                        "resource_request", art.files["resource_request.json"])
                    if sidecar_errors:
                        self._record_cost(
                            cyc, accounting_phase, art.usage, status="failed",
                            failure_kind="artifact_parse", attempt=attempt, call=call,
                            transcript_ref=art.transcript_ref,
                            execution_receipt_ref=art.execution_receipt_ref)
                        last_err = (
                            "bundle 内部重规划说明 schema 校验失败：\n"
                            + "\n".join(sidecar_errors[:8]))
                        continue
                    self._archive_accepted(
                        cyc, pack, art, stage=stage, purpose=purpose_tag,
                        call=call, attempt=attempt,
                        accounting_phase=accounting_phase,
                        defer_on_error=True)
                    self._record_cost(
                        cyc, accounting_phase, art.usage, status="success",
                        attempt=attempt, call=call,
                        transcript_ref=art.transcript_ref,
                        execution_receipt_ref=art.execution_receipt_ref,
                        provider_receipt_ref=art.provider_receipt_ref)
                    raise BundleReplanRequired(art.files["resource_request.json"])
                if not allow_resource_request:
                    self._record_cost(
                        cyc, accounting_phase, art.usage, status="failed",
                        failure_kind="artifact_parse", attempt=attempt, call=call,
                        transcript_ref=art.transcript_ref,
                        execution_receipt_ref=art.execution_receipt_ref)
                    last_err = (
                        f"{purpose_tag} 上下文由编排器完整投影，禁止产出 "
                        "resource_request.json")
                    continue
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
                    self._record_cost(cyc, accounting_phase, art.usage, status="failed",
                                      failure_kind="artifact_parse", attempt=attempt, call=call,
                                      transcript_ref=art.transcript_ref,
                                      execution_receipt_ref=art.execution_receipt_ref)
                    raise RunnerError(f"{stage} 产出 resource_request.json sidecar，但本装配未接文件请求桥"
                                      "——不静默丢弃")
                sidecar_errors = self._schema_errors_by_name(
                    "resource_request", art.files["resource_request.json"])
                if sidecar_errors:
                    self._record_cost(
                        cyc, accounting_phase, art.usage, status="failed",
                        failure_kind="artifact_parse", attempt=attempt, call=call,
                        transcript_ref=art.transcript_ref,
                        execution_receipt_ref=art.execution_receipt_ref)
                    last_err = (
                        "resource_request.json schema 校验失败（请一次修完全部列出的字段）:\n"
                        + "\n".join(sidecar_errors[:8]))
                    continue
                try:
                    rid = self.file_request_bridge(stage, art.files["resource_request.json"], cyc)
                except FileRequestReject as e:  # 只兜业务拒（sidecar 非法/quota 尽）→ 反馈重试（有界）；
                    self._record_cost(cyc, accounting_phase, art.usage, status="failed",
                                      failure_kind="artifact_parse", attempt=attempt, call=call,
                                      transcript_ref=art.transcript_ref,
                                      execution_receipt_ref=art.execution_receipt_ref)
                    last_err = f"resource_request sidecar 被拒: {e}"   # 其余异常（DB 损坏等）fail loud（内审 NIT）
                    continue
                except Exception:              # noqa: BLE001 —— 调用已发生，先记账再保留原异常
                    self._record_cost(cyc, accounting_phase, art.usage, status="failed",
                                      failure_kind="postprocess_error", attempt=attempt, call=call,
                                      transcript_ref=art.transcript_ref,
                                      execution_receipt_ref=art.execution_receipt_ref)
                    raise
                self._archive_accepted(
                    cyc, pack, art, stage=stage, purpose=purpose_tag,
                    call=call, attempt=attempt, accounting_phase=accounting_phase)
                self._record_cost(
                    cyc, accounting_phase, art.usage, status="success", attempt=attempt, call=call,
                    transcript_ref=art.transcript_ref,
                    execution_receipt_ref=art.execution_receipt_ref)
                raise StageBlockedOnResources(rid, stage)
            if art.stage != stage:              # 阶段漂移（外审 SHOULD）：文件对但 envelope stage 错 → 计入重试
                self._record_cost(cyc, accounting_phase, art.usage, status="failed",
                                  failure_kind="artifact_parse", attempt=attempt, call=call,
                                  transcript_ref=art.transcript_ref,
                                  execution_receipt_ref=art.execution_receipt_ref)
                last_err = f"产物 stage 漂移：envelope stage={art.stage!r} ≠ 期望 {stage!r}"
                continue
            if "import_search_request.json" in art.files:
                # Compatibility-only protocol for non-resident qualification or
                # old injected runners.  A resident Plan main must use the MCP
                # tool and continue in the same top-level thread.
                if self.resident_stage_sessions:
                    self._record_cost(
                        cyc, accounting_phase, art.usage, status="failed",
                        failure_kind="artifact_parse", attempt=attempt, call=call,
                        transcript_ref=art.transcript_ref,
                        execution_receipt_ref=art.execution_receipt_ref)
                    raise RunnerError(
                        "resident Plan 不接受 import_search_request sidecar；"
                        "须在同一主 turn 调用 plan_import_search 后最终提交 plan.json",
                        failure_kind="artifact_parse")
                if stage != "plan" or set(art.files) != {"import_search_request.json"}:
                    self._record_cost(
                        cyc, accounting_phase, art.usage, status="failed",
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
                        cyc, accounting_phase, art.usage, status="failed",
                        failure_kind="artifact_parse", attempt=attempt, call=call,
                        transcript_ref=art.transcript_ref,
                        execution_receipt_ref=art.execution_receipt_ref)
                    last_err = "import_search_request.json schema/边界校验失败:\n" + "\n".join(errors[:8])
                    continue
                self._archive_accepted(
                    cyc, pack, art, stage=stage, purpose=purpose_tag,
                    call=call, attempt=attempt, accounting_phase=accounting_phase)
                self._record_cost(
                    cyc, accounting_phase, art.usage, status="success", attempt=attempt, call=call,
                    transcript_ref=art.transcript_ref,
                    execution_receipt_ref=art.execution_receipt_ref)
                return {"import_search_request.json": request}
            if (not mcp_submitted and stage == "plan"
                    and set(art.files) != {"plan.json"}):
                self._record_cost(
                    cyc, accounting_phase, art.usage, status="failed",
                    failure_kind="artifact_parse", attempt=attempt, call=call,
                    transcript_ref=art.transcript_ref,
                    execution_receipt_ref=art.execution_receipt_ref)
                last_err = (
                    "plan 普通产物须恰为 plan.json 一个文件；"
                    f"实收键 {sorted(art.files)}")
                continue
            if (not mcp_submitted and spec.get("exact")
                    and set(art.files) != set(spec["required"])):
                self._record_cost(
                    cyc, accounting_phase, art.usage, status="failed",
                    failure_kind="artifact_parse", attempt=attempt, call=call,
                    transcript_ref=art.transcript_ref,
                    execution_receipt_ref=art.execution_receipt_ref)
                last_err = (
                    f"{purpose_tag} files 必须恰为 {sorted(spec['required'])}；"
                    f"实收 {sorted(art.files)}")
                continue
            if (not mcp_submitted and stage == "plan"
                    and isinstance(art.files.get("plan.json"), dict)):
                self._normalize_plan_target_gpu_mode(art.files["plan.json"])
            # submit_stage_artifact already performed the complete transport,
            # file-closure and JSON-schema check inside the live main turn.
            # Test/legacy runners without that receipt retain this compatibility
            # validator. Semantic post_validate and downstream core gates remain.
            err = None if mcp_submitted else self._validate_files(art.files, spec)
            if err:
                self._record_cost(cyc, accounting_phase, art.usage, status="failed",
                                  failure_kind="artifact_parse", attempt=attempt, call=call,
                                  transcript_ref=art.transcript_ref,
                                  execution_receipt_ref=art.execution_receipt_ref)
                last_err = err
                continue
            if post_validate is not None:
                try:
                    semantic_error = post_validate(art.files)
                except Exception:
                    self._record_cost(
                        cyc, accounting_phase, art.usage, status="failed",
                        failure_kind="postprocess_error", attempt=attempt, call=call,
                        transcript_ref=art.transcript_ref,
                        execution_receipt_ref=art.execution_receipt_ref)
                    raise
                if semantic_error:
                    self._record_cost(
                        cyc, accounting_phase, art.usage, status="failed",
                        failure_kind="artifact_parse", attempt=attempt, call=call,
                        transcript_ref=art.transcript_ref,
                        execution_receipt_ref=art.execution_receipt_ref)
                    last_err = semantic_error
                    continue
            self._archive_accepted(
                cyc, pack, art, stage=stage, purpose=purpose_tag,
                call=call, attempt=attempt, accounting_phase=accounting_phase)
            self._record_cost(
                cyc, accounting_phase, art.usage, status="success", attempt=attempt, call=call,
                transcript_ref=art.transcript_ref,
                execution_receipt_ref=art.execution_receipt_ref,
                provider_receipt_ref=art.provider_receipt_ref)
            if on_accept is not None:
                on_accept(art, call[0] if call is not None else None)
            if spec.get("passthrough"):        # bundle：代码文件名任意 → 信封全量透传（物化/交叉核在组件侧）
                return dict(art.files)
            return {k: art.files[k] for k in spec["required"] + [o for o in spec["optional"] if o in art.files]}
        # 完整 last_err（外审 NIT：不截断——这是 fail-fast 排障入口，schema oneOf 展开的字段路径不能丢）
        if resident_turn:
            raise RunnerError(
                f"{purpose_tag} 产物结构非法；resident 主 turn 不执行外层 artifact 重试："
                f"{last_err}", failure_kind="artifact_parse")
        raise RunnerError(
            f"{purpose_tag} 产物结构非法，artifact_parse 重试（≤{self.retries}）用尽：{last_err}",
            failure_kind="artifact_parse")

    def _normalize_plan_target_gpu_mode(self, plan: Dict[str, Any]) -> None:
        """Project abstract GPU counts into the legacy boolean compatibility field."""
        targets = plan.get("targets")
        if not isinstance(targets, list):
            return
        for target in targets:
            if not isinstance(target, dict):
                continue
            resources = target.get("resources")
            gpu_count = (
                resources.get("gpu_count")
                if isinstance(resources, dict) else None)
            if (isinstance(gpu_count, int)
                    and not isinstance(gpu_count, bool)
                    and 0 <= gpu_count <= 64):
                target["gpu_required"] = gpu_count > 0
            elif ("resources" not in target
                    and self.gpu_target_policy in {
                        "required", "forbidden"}):
                target["gpu_required"] = (
                    self.gpu_target_policy == "required")

    def _bundle_operator_session_id(self, cyc, pack) -> Optional[str]:
        """Recover one cycle-scoped Bundle Codex thread from call receipts.

        The durable identity already exists in provider invocation receipts;
        no second session database or model-authored success marker is needed.
        Reading every prior receipt (rather than trusting only the newest one)
        also fails closed if a provider ever forked sequential targets in the
        same cycle onto different threads.  Each target's immutable ``plan_ref``
        remains enforced by the manifest cross-check and core gates.
        """
        if pack.target_id is None:
            raise ValueError("bundle operator ContextPack 缺 target_id")
        try:
            target_id = int(pack.target_id)
        except (TypeError, ValueError) as error:
            raise ValueError("bundle operator target_id 须为 build_target 整数") from error
        if target_id <= 0:
            raise ValueError("bundle operator target_id 须为正整数")
        return self._recover_persistent_session_id(
            cyc, phase="bundle",
            purpose_glob=f"bundle-c{_cnum(cyc.cycle_id)}-*",
            label=f"bundle cycle {cyc.cycle_id}")

    def _stage_main_session_id(self, cyc, stage: str) -> Optional[str]:
        """Recover the one durable top-level Codex thread for a stage.

        The purpose namespace is new and resident-only, so an in-flight quest
        created by an older ephemeral release cannot be mistaken for a thread
        that is safe to resume.  Retries, semantic corrections and process
        restarts all resolve through the same provider receipts.
        """
        if stage not in {"idea", "plan", "bundle", "reasoning"}:
            raise ValueError(f"普通常驻阶段非法: {stage!r}")
        ci = _cnum(cyc.cycle_id)
        purpose_glob = f"{stage}-main-c{ci}*"
        return self._recover_persistent_session_id(
            cyc, phase=stage, purpose_glob=purpose_glob,
            label=f"{stage} main {cyc.cycle_id}")

    def _bundle_task_session_id(
            self, cyc, pack, *, role: str) -> Optional[str]:
        """Recover the exact Scheduler/Worker provider task from receipts."""
        registry = self._bundle_task_registry()
        cycle_id = f"c{_cnum(cyc.cycle_id)}"
        if role == "bundle_scheduler":
            registry_role = "scheduler"
            target_id = None
        elif role == "target_worker":
            registry_role = "target_worker"
            try:
                target_id = int(pack.target_id)
            except (TypeError, ValueError) as error:
                raise ValueError(
                    "Bundle Target Worker ContextPack 缺合法 target") from error
        else:
            raise ValueError("Bundle task role 非法")
        return registry.recover(
            cycle_id, role=registry_role, target_id=target_id)

    def _bundle_task_registry(self) -> BundleTaskRegistry:
        """Return the receipt-backed durable task ledger facade."""
        daemon = getattr(self.cost_ledger, "daemon", None)
        if daemon is None:
            raise RuntimeError("Bundle task 缺 runner_call 权威库")
        return BundleTaskRegistry(
            daemon, receipt_loader=load_provider_invocation_receipt)

    def _recover_persistent_session_id(
            self, cyc, *, phase: str, purpose_glob: str,
            label: str) -> Optional[str]:
        """Resolve exactly one provider thread from accounted call receipts."""
        daemon = getattr(self.cost_ledger, "daemon", None)
        if daemon is None:
            raise RuntimeError(f"{label} 缺 runner_call 权威库")
        # The frozen runner_call table intentionally contains only lifecycle
        # columns. Provider/execution receipt refs live in the append-only
        # provider_invocation_accounted decision written in the same terminal
        # cost transaction; recover through that established authority instead
        # of extending or bypassing the core SQL schema.
        rows = daemon.query(
            "SELECT rc.id,rc.phase,rc.purpose,"
            "json_extract(d.payload_json,'$.provider_receipt_ref'),"
            "json_extract(d.payload_json,'$.execution_receipt_ref') "
            "FROM runner_call AS rc JOIN decision AS d "
            "ON d.cycle_id=rc.cycle_id AND d.actor='orchestrator' "
            "AND d.type='provider_invocation_accounted' "
            "AND json_valid(d.payload_json) "
            "AND json_extract(d.payload_json,'$.protocol')='provider-accounting-v1' "
            "AND json_extract(d.payload_json,'$.runner_call_id')=rc.id "
            "WHERE rc.cycle_id=? AND rc.phase=? "
            "AND rc.purpose GLOB ? AND rc.status IN ('success','failed') "
            "AND json_type(d.payload_json,'$.provider_receipt_ref')='text' "
            "AND json_type(d.payload_json,'$.execution_receipt_ref')='text' "
            "ORDER BY rc.id,d.id",
            (_cnum(cyc.cycle_id), phase, purpose_glob))
        session_id: Optional[str] = None
        missing_provider_ids = 0
        retryable_missing_provider_ids = 0
        seen_runner_calls: set[int] = set()
        for runner_call_id, phase, purpose, provider_ref, execution_ref in rows:
            if runner_call_id in seen_runner_calls:
                raise RuntimeError(
                    f"{label} runner_call {runner_call_id} provider accounting 重复")
            seen_runner_calls.add(runner_call_id)
            invocation = load_provider_invocation_receipt(
                Path(provider_ref), expected_runner_call_id=runner_call_id,
                expected_cycle_id=cyc.cycle_id, expected_phase=phase,
                expected_purpose=purpose,
                expected_execution_receipt_ref=execution_ref)
            candidate = invocation.provider_invocation_id
            if candidate is None:
                missing_provider_ids += 1
                # A normal non-zero process exit has a terminal, drained
                # execution receipt and no accepted stage output.  Retrying it
                # is safe and necessary for pre-session failures such as
                # authentication or MCP startup.  Ambiguous timeout, owner
                # loss, cancellation, or an unknown outcome still blocks a
                # fresh thread because a provider session may remain live.
                if (getattr(invocation, "execution_outcome", None) == "exit"
                        and isinstance(
                            getattr(invocation, "execution_returncode", None), int)
                        and invocation.execution_returncode != 0):
                    retryable_missing_provider_ids += 1
                continue
            if session_id is not None and candidate != session_id:
                raise RuntimeError(
                    f"{label} provider session 漂移")
            session_id = candidate
        if (rows and session_id is None
                and retryable_missing_provider_ids != missing_provider_ids):
            raise RuntimeError(
                f"{label} 历史调用均缺可续接 provider id；拒绝新建会话"
                f"（receipts={missing_provider_ids}）")
        return session_id

    def _archive_accepted(self, cyc, pack, art, *, stage: str, purpose: str,
                          call, attempt: int, accounting_phase: str,
                          defer_on_error: bool = False) -> None:  # noqa: ANN001
        """Persist one validated/control envelope and close cost intent on IO failure."""
        if self.replay_archive is None:
            return
        try:
            submitted = getattr(art, "stage_submission_ref", None) is not None
            submitted_target = getattr(
                art, "stage_submission_target_id", None)
            archive_target = (
                submitted_target
                if submitted and stage == "bundle" and submitted_target is not None
                else getattr(pack, "target_id", None))
            archive_pack_hash = (
                getattr(art, "stage_submission_pack_hash", None)
                if submitted
                else None) or (getattr(pack, "pack_hash", None) or None)
            self.replay_archive.persist_stage_artifact(
                cycle_id=cyc.cycle_id, stage=stage, artifact=art,
                target_id=archive_target, purpose=purpose,
                pack_hash=archive_pack_hash,
                runner_call_id=(call[0] if call is not None else None))
        except Exception as error:
            if defer_on_error:
                # A Bundle replan/protocol diagnosis is a research outcome
                # whose next mandatory state is Reasoning.  Replay is a
                # repairable file outbox, not a second semantic gate: retain a
                # durable pointer to the already MCP-indexed submission and let
                # BundleReplanRequired continue upward.  Other accepted stage
                # artifacts keep the stricter fail-loud archive contract.
                daemon = getattr(self.cost_ledger, "daemon", None)
                if daemon is not None:
                    try:
                        runner_call_id = call[0] if call is not None else None
                        payload = json.dumps({
                            "protocol": "replay-archive-deferred-v1",
                            "stage": stage,
                            "purpose": purpose,
                            "runner_call_id": runner_call_id,
                            "submission_ref": getattr(
                                art, "stage_submission_ref", None),
                            "artifact_hash": getattr(
                                art, "stage_submission_hash", None),
                            "error_kind": type(error).__name__,
                            "error_md": str(error)[:2048],
                        }, ensure_ascii=False, sort_keys=True)
                        with daemon.transaction() as conn:
                            duplicate = conn.execute(
                                "SELECT 1 FROM decision WHERE cycle_id=? "
                                "AND actor='orchestrator' "
                                "AND type='replay_archive_deferred' "
                                "AND json_extract(payload_json,'$.runner_call_id') "
                                "IS ? LIMIT 1",
                                (_cnum(cyc.cycle_id), runner_call_id)).fetchone()
                            if duplicate is None:
                                conn.execute(
                                    "INSERT INTO decision(cycle_id,actor,type,payload_json) "
                                    "VALUES (?,'orchestrator',"
                                    "'replay_archive_deferred',?)",
                                    (_cnum(cyc.cycle_id), payload))
                    except Exception:
                        logger.exception(
                            "Bundle replan replay deferred 诊断入库失败 "
                            "(cycle=%s)", cyc.cycle_id)
                logger.exception(
                    "Bundle replan replay archive 延后修复，继续进入 Reasoning "
                    "(cycle=%s purpose=%s)", cyc.cycle_id, purpose)
                return
            # The model call happened but its required replay output did not
            # publish.  Close that exact runner_call as failed; never leave a
            # running heartbeat or pretend the turn was accepted.
            self._record_cost(
                cyc, accounting_phase, art.usage, status="failed",
                failure_kind="replay_archive_failed", attempt=attempt, call=call,
                transcript_ref=art.transcript_ref,
                execution_receipt_ref=art.execution_receipt_ref,
                provider_receipt_ref=art.provider_receipt_ref)
            raise

    def _begin_cost_call(self, cyc, stage: str, runner, attempt: int, *,
                         purpose_tag: Optional[str] = None):
        if self.cost_ledger is None:
            return None
        purpose = f"{purpose_tag or stage}-n{self._call_seq}-a{attempt + 1}"
        rc = self.cost_ledger.begin_call(
            cycle_id=cyc.cycle_id, phase=stage, purpose=purpose)
        heartbeat_path = (
            self.work / f"cycles/{cyc.cycle_id}/transcripts" /
            f"{stage}-rc{rc}.heartbeat.json")
        heartbeat = _RunnerCallHeartbeat(
            heartbeat_path, runner_call_id=rc, cycle_id=cyc.cycle_id,
            phase=stage, purpose=purpose)
        try:
            _bind_runner_call(runner, rc, phase=stage, purpose=purpose)
            self.cost_ledger.mark_call_running(
                runner_call_id=rc, transcript_ref=str(heartbeat_path))
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
                     execution_receipt_ref: Optional[str] = None,
                     provider_receipt_ref: Optional[str] = None) -> None:
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
                transcript_ref=transcript_ref or str(heartbeat.path),
                execution_receipt_ref=execution_receipt_ref,
                provider_receipt_ref=provider_receipt_ref)
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
                 cost_ledger=None, replay_archive=None):
        self.runner_factory = runner_factory
        self.schemas = schemas
        self.retries = policy["flow"]["retry"]["artifact_parse"]
        self.system_prompt = system_prompt
        self.skill = skill
        self.daemon = daemon
        self.work = Path(work_root)
        self.replay_archive = replay_archive
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
        if self.replay_archive is not None:
            self.replay_archive.persist_context_pack(
                pack, label=f"plan-review-{round_no}")
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
        frozen_verdict: Optional[str] = None
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
                # Only a successfully returned but malformed envelope belongs to the configured
                # artifact_parse retry budget.  Transport/timeout/runtime failures stay fail-loud.
                if error.failure_kind != "artifact_parse":
                    raise
                last_err = str(error)
                continue
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
                # One configured review round means one semantic judgment.
                # If the first returned artifact already expresses a valid
                # verdict but misses a required structural field, an
                # artifact_parse retry may repair only the envelope; it must
                # not silently turn the same review from fail into pass (or
                # vice versa) in a fresh provider invocation.
                candidate_verdict = (
                    review.get("verdict") if isinstance(review, dict) else None)
                if candidate_verdict in {"pass", "fail"}:
                    if frozen_verdict is None:
                        frozen_verdict = candidate_verdict
                    elif candidate_verdict != frozen_verdict:
                        errors.append(
                            "artifact retry 不得改变首次有效 verdict："
                            f"{frozen_verdict!r} -> {candidate_verdict!r}")
                if (isinstance(review, dict)
                        and review.get("round_no") != round_no):
                    errors.append(
                        f"plan_review.round_no={review.get('round_no')!r}，期望 {round_no}")
            if errors:
                self._finish_failed(
                    call, art.usage, failure_kind="artifact_parse",
                    transcript_ref=art.transcript_ref,
                    execution_receipt_ref=art.execution_receipt_ref)
                last_err = "\n".join(errors[:8])
                if frozen_verdict is not None:
                    last_err += (
                        "\n首次有效 verdict 已冻结为 " + frozen_verdict
                        + "；只补齐/修正 JSON 结构，不得重新评审或改变结论。")
                continue
            if self.replay_archive is not None:
                try:
                    self.replay_archive.persist_stage_artifact(
                        cycle_id=cyc.cycle_id, stage="plan", artifact=art,
                        purpose=f"plan-review-{round_no}",
                        pack_hash=getattr(pack, "pack_hash", None) or None,
                        runner_call_id=(call[0] if call is not None else None))
                except Exception:
                    self._finish_failed(
                        call, art.usage, failure_kind="replay_archive_failed",
                        transcript_ref=art.transcript_ref,
                        execution_receipt_ref=art.execution_receipt_ref)
                    raise
            decision_id = self._record(
                cyc.cycle_id, round_no, plan_hash, review, art.usage, call=call,
                transcript_ref=art.transcript_ref,
                execution_receipt_ref=art.execution_receipt_ref)
            return review, decision_id
        raise RunnerError(
            f"plan_review 第 {round_no} 轮产物结构非法，artifact_parse 重试"
            f"（≤{self.retries}）用尽：{last_err}", failure_kind="artifact_parse")

    def _begin_call(self, cycle_id: str, runner, round_no: int, attempt: int):
        if self.cost_ledger is None:
            return None
        runner_call_id = self.cost_ledger.begin_call(
            cycle_id=cycle_id, phase="audit", purpose="plan_review")
        heartbeat_path = (
            self.work / f"cycles/{cycle_id}/transcripts" /
            f"plan-review-rc{runner_call_id}.heartbeat.json")
        heartbeat = _RunnerCallHeartbeat(
            heartbeat_path, runner_call_id=runner_call_id, cycle_id=cycle_id,
            phase="audit", purpose="plan_review")
        try:
            _bind_runner_call(
                runner, runner_call_id, phase="audit", purpose="plan_review")
            self.cost_ledger.mark_call_running(
                runner_call_id=runner_call_id, transcript_ref=str(heartbeat_path))
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
            transcript_ref=transcript_ref or str(heartbeat.path),
            execution_receipt_ref=execution_receipt_ref)
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
                    transcript_ref=transcript_ref or str(heartbeat.path),
                    execution_receipt_ref=execution_receipt_ref)
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
                        transcript_ref=transcript_ref or str(heartbeat.path),
                        execution_receipt_ref=execution_receipt_ref)
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
                    transcript_ref=transcript_ref or str(heartbeat.path),
                    execution_receipt_ref=execution_receipt_ref)
            raise
        if budget_hit is not None:
            raise BudgetExhausted(**budget_hit)
        return decision_id


def _tail(text: str, n: int = 2000) -> str:
    """材料摘要截尾（评审对象走 prompt，防超长；截断显式标注，不冒充全文）。"""
    return text if len(text) <= n else f"…（前 {len(text) - n} 字符截断）…\n" + text[-n:]


_REVIEW_INVENTORY_PATHS = 256
_REVIEW_INVENTORY_TOTAL_BYTES = 32_000
_REVIEW_PREVIEW_FILES = 64
_REVIEW_PREVIEW_FILE_BYTES = 20_000
_REVIEW_PREVIEW_TOTAL_BYTES = 160_000


def _bounded_review_preview(path: Path, limit: int = 20000) -> str:
    """Read at most *limit* bytes from a no-follow regular file for an LLM prompt."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("judge preview limit 须为正整数")
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        return f"（非常规文件，拒绝预览；mode={oct(info.st_mode)}）"
    fd = os.open(
        path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(fd)
        if (not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino, opened.st_size)
                != (info.st_dev, info.st_ino, info.st_size)):
            raise RuntimeError(f"judge preview 文件身份漂移: {path}")
        if opened.st_size <= limit:
            payload = bytearray()
            while len(payload) < opened.st_size:
                chunk = os.read(fd, opened.st_size - len(payload))
                if not chunk:
                    raise RuntimeError(f"judge preview 文件读取截断: {path}")
                payload.extend(chunk)
            payload = bytes(payload)
            prefix = ""
        else:
            head_size = limit // 2
            tail_size = limit - head_size
            head = bytearray()
            while len(head) < head_size:
                chunk = os.read(fd, head_size - len(head))
                if not chunk:
                    raise RuntimeError(f"judge preview 文件头读取截断: {path}")
                head.extend(chunk)
            os.lseek(fd, opened.st_size - tail_size, os.SEEK_SET)
            tail = bytearray()
            while len(tail) < tail_size:
                chunk = os.read(fd, tail_size - len(tail))
                if not chunk:
                    raise RuntimeError(f"judge preview 文件尾读取截断: {path}")
                tail.extend(chunk)
            payload = bytes(head + tail)
            prefix = (
                f"…（文件 {opened.st_size} bytes，中间 "
                f"{opened.st_size - len(payload)} bytes 未载入评审 prompt）…\n")
        after = os.fstat(fd)
        if ((after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
             after.st_ctime_ns)
                != (opened.st_dev, opened.st_ino, opened.st_size,
                    opened.st_mtime_ns, opened.st_ctime_ns)):
            raise RuntimeError(f"judge preview 文件读取期间漂移: {path}")
    finally:
        os.close(fd)
    if b"\x00" in payload:
        return f"（binary 预览省略；{info.st_size} bytes）"
    return prefix + payload.decode("utf-8", errors="replace")


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
                 skill: str, daemon, work_root: str, cost_ledger=None,
                 replay_archive=None):
        self.runner_factory = runner_factory
        self.schemas = schemas
        self.retries = policy["flow"]["retry"]["artifact_parse"]
        self.system_prompt = system_prompt
        self.skill = skill
        self.daemon = daemon
        self.work = Path(work_root)
        self.replay_archive = replay_archive
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
        if self.replay_archive is not None:
            self.replay_archive.persist_context_pack(pack, label=review_kind)
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
                # 判官不能借 sidecar 扩大读取权限；有界材料若不足，必须在 verdict
                # 中 fail 并指出缺口，而不是发起一个不可审计的新取数路径。
                self._record_cost(cycle_id, review_kind, art.usage, status="failed",
                                  failure_kind="artifact_parse", attempt=attempt, call=call,
                                  transcript_ref=art.transcript_ref,
                                  execution_receipt_ref=art.execution_receipt_ref)
                last_err = ("judge 不受理 resource_request sidecar（材料由编排器有界冻结；"
                            "若关键路径不足，请在 review_verdict.json 中 fail 并指出缺口）")
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
            if self.replay_archive is not None:
                try:
                    self.replay_archive.persist_stage_artifact(
                        cycle_id=cycle_id, stage="bundle", artifact=art,
                        target_id=str(build_target_id), purpose=review_kind,
                        pack_hash=getattr(pack, "pack_hash", None) or None,
                        runner_call_id=(call[0] if call is not None else None))
                except Exception:
                    self._record_cost(
                        cycle_id, review_kind, art.usage, status="failed",
                        failure_kind="replay_archive_failed", attempt=attempt, call=call,
                        transcript_ref=art.transcript_ref,
                        execution_receipt_ref=art.execution_receipt_ref)
                    raise
            self._record(
                cycle_id, build_target_id, review_kind, subject_hash, verdict, art.usage,
                call=call, transcript_ref=art.transcript_ref,
                execution_receipt_ref=art.execution_receipt_ref)
            return
        raise RunnerError(
            f"judge {review_kind} 产物结构非法，artifact_parse 重试（≤{self.retries}）用尽：{last_err}",
            failure_kind="artifact_parse")

    def _begin_cost_call(self, cycle_id: str, review_kind: str, runner, attempt: int):
        if self.cost_ledger is None:
            return None
        # Gate.review_passed mechanically requires runner_call.purpose == review_kind.
        rc = self.cost_ledger.begin_call(
            cycle_id=cycle_id, phase="audit", purpose=review_kind)
        heartbeat_path = (
            self.work / f"cycles/{cycle_id}/transcripts" /
            f"{review_kind}-rc{rc}.heartbeat.json")
        heartbeat = _RunnerCallHeartbeat(
            heartbeat_path, runner_call_id=rc, cycle_id=cycle_id,
            phase="audit", purpose=review_kind)
        try:
            _bind_runner_call(runner, rc, phase="audit", purpose=review_kind)
            self.cost_ledger.mark_call_running(
                runner_call_id=rc, transcript_ref=str(heartbeat_path))
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
                transcript_ref=transcript_ref or str(heartbeat.path),
                execution_receipt_ref=execution_receipt_ref)
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
                    transcript_ref=transcript_ref or str(heartbeat.path),
                    execution_receipt_ref=execution_receipt_ref)
                raise error
        try:
            with self.daemon.transaction() as conn:
                round_no = conn.execute(
                    "SELECT COUNT(*)+1 FROM decision WHERE actor='judge' AND type=? AND json_valid(payload_json) "
                    "AND json_extract(payload_json,'$.build_target_id')=?", (kind, bt_id)).fetchone()[0]
                if self.cost_ledger is not None:
                    budget_hit = self.cost_ledger.finish_call_in_txn(
                        conn, runner_call_id=rc, status="success", usage=usage,
                        transcript_ref=transcript_ref or str(heartbeat.path),
                        execution_receipt_ref=execution_receipt_ref)
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
                    transcript_ref=transcript_ref or str(heartbeat.path),
                    execution_receipt_ref=execution_receipt_ref)
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
        review_files = []
        total_bytes = 0
        if os.path.lexists(src):
            src_info = os.lstat(src)
            if (not stat.S_ISDIR(src_info.st_mode)
                    or stat.S_ISLNK(src_info.st_mode)):
                raise RuntimeError("judge 物化源码根不是可信目录")
        for current, dirs, files in os.walk(src, followlinks=False):
            for name in dirs:
                info = os.lstat(Path(current) / name)
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                    raise RuntimeError(
                        f"judge 物化源码含非常规目录: {Path(current) / name}")
            for name in files:
                path = Path(current) / name
                if name == "_staged.ok":
                    continue
                info = os.lstat(path)
                if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    raise RuntimeError(f"judge 物化源码含非常规文件: {path}")
                review_files.append((path, info.st_size))
                total_bytes += info.st_size
        command_paths = set()
        artifact_path = None
        try:
            plan_ref = json.loads(slice_md)
            contract = (plan_ref.get("materialization_contract")
                        if isinstance(plan_ref, dict) else None)
            if isinstance(contract, dict):
                raw_artifact = contract.get("artifact_relpath")
                if isinstance(raw_artifact, str):
                    artifact_path = raw_artifact
                for command_key in ("smoke_cmd", "eval_cmd"):
                    command = contract.get(command_key)
                    if not isinstance(command, list):
                        continue
                    for argument in command:
                        if isinstance(argument, str) and argument.startswith("{repo}/"):
                            command_paths.add(argument.removeprefix("{repo}/"))
        except (json.JSONDecodeError, TypeError):
            pass

        def review_priority(item):  # noqa: ANN001, ANN202 - local sort key
            rel = str(item[0].relative_to(src))
            if rel == ".meta-research/import-adapter.json":
                rank = 0
            elif rel in command_paths:
                rank = 1
            elif item[0].suffix == ".py":
                rank = 2
            elif rel == artifact_path:
                rank = 3
            else:
                rank = 4
            return rank, rel

        review_files.sort(key=review_priority)
        inventory_lines = []
        inventory_bytes = 0
        for path, size in review_files[:_REVIEW_INVENTORY_PATHS]:
            line = f"- {path.relative_to(src)} ({size} bytes)"
            line_bytes = len((line + "\n").encode("utf-8"))
            if inventory_bytes + line_bytes > _REVIEW_INVENTORY_TOTAL_BYTES:
                break
            inventory_lines.append(line)
            inventory_bytes += line_bytes
        inventory_omitted = len(review_files) - len(inventory_lines)
        parts.append(
            f"### 物化文件清单摘要\nfiles={len(review_files)} "
            f"total_bytes={total_bytes} paths_shown={len(inventory_lines)}\n"
            + "\n".join(inventory_lines)
            + (f"\n- …其余 {inventory_omitted} 个路径只由 manifest hash 闭包"
               if inventory_omitted else ""))
        # Both reviews need code for result backtracking, but a repository may
        # legitimately contain a multi-GB model or 100k files.  Prompt material
        # is therefore a bounded, explicitly truncated view; the complete byte
        # closure remains enforced by the independently hashed subject manifest.
        remaining_preview_bytes = _REVIEW_PREVIEW_TOTAL_BYTES
        previewed_files = 0
        for path, _size in review_files[:_REVIEW_PREVIEW_FILES]:
            if remaining_preview_bytes <= 0:
                break
            per_file_limit = min(
                _REVIEW_PREVIEW_FILE_BYTES, remaining_preview_bytes)
            preview = _bounded_review_preview(path, per_file_limit)
            encoded = preview.encode("utf-8")
            if len(encoded) > remaining_preview_bytes:
                preview = (encoded[:remaining_preview_bytes]
                           .decode("utf-8", errors="ignore")
                           + "\n…（总预览预算已用尽）…")
                consumed = remaining_preview_bytes
            else:
                consumed = len(encoded)
            parts.append(
                f"### 物化文件 {path.relative_to(src)}\n```\n"
                f"{preview}\n```")
            remaining_preview_bytes -= consumed
            previewed_files += 1
        if len(review_files) > previewed_files:
            parts.append(
                f"### 预览截断声明\n已按 adapter/命令入口/Python 优先顺序预览 "
                f"{previewed_files}/{len(review_files)} 个文件；内容总预算 "
                f"{_REVIEW_PREVIEW_TOTAL_BYTES} bytes，单文件上限 "
                f"{_REVIEW_PREVIEW_FILE_BYTES} bytes。其余 "
                f"{len(review_files) - previewed_files} 个文件仍在 subject manifest hash 闭包中，"
                "但未冒充已做语义评审。")
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
