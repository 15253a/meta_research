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
from pathlib import Path
from typing import Any, Dict, List, Optional

from .harness import latest_smoke_log as _latest_smoke_log
from .ids import cnum as _cnum
from .interfaces import ContextPack
from .runner import RunnerError

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
    "plan": "执行【计划任务】，产 plan.json（冻结 plan.schema：抽象 targets + protocol + metric_defs；命令不在 plan）",
    "bundle": "按锚区「本目标」切片产可执行包：execution_manifest.json + identity.md + 代码文件（一信封装齐）",
    "reasoning": "执行【轮尾任务】，按 route 产 selection.json（必），酌情 tree_ops.json / answer.json",
}


class StageProvider:
    def __init__(self, *, runner_factory, schemas, policy: Dict[str, Any],
                 system_prompt: str, skills: Dict[str, str], work_root: str):
        """runner_factory(transcripts_dir, purpose_tag)→Runner（默认真 CodexRunner，见 run.py 装配）；
        schemas=SchemaSet（产物校验）；skills={stage: SKILL.md 文本}；work_root=cycles/<id>/transcripts 的根。
        不持 compiler——pack 由调用方（advancer/attack_stages）渲染后传入，本类不 render。"""
        self.runner_factory = runner_factory
        self.schemas = schemas
        self.retries = policy["flow"]["retry"]["artifact_parse"]
        self.system_prompt = system_prompt
        self.skills = skills
        self.work = Path(work_root)
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
            try:
                art = runner.run_task(system_prompt=self.system_prompt, skill=skill, context_pack=pack)
            except RunnerError as e:               # 进程失败/超时/信封不可解析 → 计入重试
                last_err = str(e)
                continue
            if "resource_request.json" in art.files:
                # 阶段发资源请求 sidecar（§3.1.1「需用户提供文件」）：本路径尚未接 sidecar→
                # notify.create_file_request 桥（全局等待走 CP6.3 独立通道）。**fail loud**，绝不静默丢弃
                # （否则 worker 的资源诉求无声消失、阶段假装成功）——接线 = CP7.4 硬化。
                raise RunnerError(f"{stage} 产出 resource_request.json sidecar，但本路径未接文件请求桥"
                                  "（CP7.4 接 notify.create_file_request）——不静默丢弃")
            if art.stage != stage:              # 阶段漂移（外审 SHOULD）：文件对但 envelope stage 错 → 计入重试
                last_err = f"产物 stage 漂移：envelope stage={art.stage!r} ≠ 期望 {stage!r}"
                continue
            err = self._validate_files(art.files, spec)
            if err:
                last_err = err
                continue
            if spec.get("passthrough"):        # bundle：代码文件名任意 → 信封全量透传（物化/交叉核在组件侧）
                return dict(art.files)
            return {k: art.files[k] for k in spec["required"] + [o for o in spec["optional"] if o in art.files]}
        # 完整 last_err（外审 NIT：不截断——这是 fail-fast 排障入口，schema oneOf 展开的字段路径不能丢）
        raise RunnerError(f"{stage} 产物结构非法，artifact_parse 重试（≤{self.retries}）用尽：{last_err}")

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
        errors: List[str] = []
        for e in v.iter_errors(payload):
            errors.append(f"{e.json_path} {e.message}")
            stack = list(e.context or [])
            while stack:
                sub = stack.pop()
                errors.append(f"{sub.json_path} {sub.message}")
                stack.extend(sub.context or [])
        return errors


def _tail(text: str, n: int = 2000) -> str:
    """材料摘要截尾（评审对象走 prompt，防超长；截断显式标注，不冒充全文）。"""
    return text if len(text) <= n else f"…（前 {len(text) - n} 字符截断）…\n" + text[-n:]


class JudgeProvider:
    """真 Codex 双评审装配（步⑧ CP8.3）：**attack 轮**的 judge 契约实现——
    judge(cycle_id, build_target_id, review_kind, subject_hash)。
    ⚠️ 只服务 attack 目标：_subject_md 按 attack staging 布局（work/c<ci>/t<bt>/{src,smoke,run*,eval*}）装
    材料；import 物化的布局不同（work/import<ei>/{clone,smoke}，代码在 clone/ 非 src/）——给 import_worker
    接真 judge 时须另配 subject 装配器（内审 SHOULD 记：勿把本类直接塞给 import_worker）。

    分工（§4.1.4 附注）：**编排器**机械装 subject 材料（DB 切片/checkpoint 哈希 + staging 物化代码/
    log 摘要）→ 独立 Codex 会话按 judge SKILL 产 review_verdict.json（schema 校验 + artifact_parse
    重试）→ **本类**以短事务落 runner_call(phase='audit')+DECISION(actor='judge')。Codex 永不碰 DB、
    不自算 subject_hash（编排器传入并原样落 DECISION——review_passed 按它对账当下重算值）。
    fail 权真实：verdict 原样入库，fail ⇒ review_passed=False ⇒ 目标 failed(review_failed)。
    replay-safe 由调用方 judge_once 保证（同 (target,kind,subject_hash) 不重调）。
    policy_hash = judge SKILL 文本 sha256（prompt 版本指纹：措辞即行为，换 prompt 即换裁决口径）。"""

    def __init__(self, *, runner_factory, schemas, policy: Dict[str, Any], system_prompt: str,
                 skill: str, daemon, work_root: str):
        self.runner_factory = runner_factory
        self.schemas = schemas
        self.retries = policy["flow"]["retry"]["artifact_parse"]
        self.system_prompt = system_prompt
        self.skill = skill
        self.daemon = daemon
        self.work = Path(work_root)
        self.policy_hash = hashlib.sha256(skill.encode("utf-8")).hexdigest()
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
            try:
                art = runner.run_task(system_prompt=self.system_prompt, skill=sk, context_pack=pack)
            except RunnerError as e:
                last_err = str(e)
                continue
            verdict = art.files.get("review_verdict.json")
            if verdict is None:
                last_err = f"缺 review_verdict.json（files 键: {list(art.files)}）"
                continue
            errs = [f"{e.json_path} {e.message}"
                    for e in self.schemas.validator("review_verdict").iter_errors(verdict)]
            if errs:
                last_err = "review_verdict.json schema 校验失败:\n" + "\n".join(errs[:8])
                continue
            self._record(cycle_id, build_target_id, review_kind, subject_hash, verdict)
            return
        raise RunnerError(f"judge {review_kind} 产物结构非法，artifact_parse 重试（≤{self.retries}）用尽：{last_err}")

    # -- 落库（短事务；runner_call 与 DECISION 同生共死）------------------------
    def _record(self, cycle_id: str, bt_id: int, kind: str, subject_hash: str, verdict: Dict[str, Any]) -> None:
        ci = _cnum(cycle_id)
        with self.daemon.transaction() as conn:
            round_no = conn.execute(
                "SELECT COUNT(*)+1 FROM decision WHERE actor='judge' AND type=? AND json_valid(payload_json) "
                "AND json_extract(payload_json,'$.build_target_id')=?", (kind, bt_id)).fetchone()[0]
            rc = conn.execute("INSERT INTO runner_call(cycle_id,phase,purpose,status) VALUES (?,'audit',?,'success')",
                              (ci, kind)).lastrowid
            conn.execute("INSERT INTO decision(cycle_id,actor,type,payload_json) VALUES (?,'judge',?,?)",
                         (ci, kind, json.dumps(
                             {"build_target_id": bt_id, "review_kind": kind, "round_no": round_no,
                              "verdict": verdict["verdict"], "issues": verdict.get("issues", []),
                              "notes_md": verdict.get("notes_md", ""), "subject_hash": subject_hash,
                              "runner_call_id": rc, "policy_hash": self.policy_hash}, ensure_ascii=False)))

    # -- subject 材料装配（编排器机械拼装，零推理；judge 只读它）-----------------
    def _subject_md(self, cycle_id: str, bt_id: int, kind: str) -> str:
        """code review：切片 + 物化代码全清单 + smoke transcript。result review：**代码也在场**（判据
        「据结果反查代码」，codex BLOCKER）+ smoke + metric_value 行**全量显式列出**（不受 log tail 截断）
        + train/eval log 尾部 + checkpoint 哈希 + identity。"""
        d = self.daemon
        row = d.query_one("SELECT plan_ref FROM build_target WHERE id=?", (bt_id,))
        slice_md = row[0] if row and row[0] else "（无 plan_ref）"
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
                ev = t_dir / f"eval{rid}" / "eval.log"
                if ev.exists():                 # metric 行全量显式列出（不受下方 tail 截断，codex BLOCKER）
                    mlines = [ln for ln in ev.read_text(encoding="utf-8", errors="replace").splitlines()
                              if ln.strip().startswith("metric_value:")]
                    parts.append("### metric_value 行（全量）\n```\n" + "\n".join(mlines) + "\n```")
                for name, sub in (("train", f"run{rid}"), ("eval", f"eval{rid}")):
                    p = t_dir / sub / f"{name}.log"
                    if p.exists():
                        parts.append(f"### {name}.log（尾部）\n```\n"
                                     + _tail(p.read_text(encoding="utf-8", errors="replace")) + "\n```")
        return "\n\n".join(parts)
