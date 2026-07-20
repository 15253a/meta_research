"""M0 最小驱动器：把四阶段接成可跑的元循环（第三部分 §7.1 M0 行）。

范围与边界（M0）：
- Runner 真（codex exec）；Gate/StateStore/Compiler 用桩；**bundle 由本驱动器确定性造假**
  （evaluation.source='fake'、log/观测 synthetic=true——绝不写 M1 才有的 DB 表）。
- 单进程串行；恢复/watchdog 是 M3 主题（本驱动器每轮内顺序执行、轮间状态在 StateStore 桩）。
- 失败语义对齐 §4.2.3：idea 全不合格 / plan 独立评审后一次定向修订仍无效 → **阶段失败 = 研究轮正常收尾**
  （cycle=done、Qn inconclusive、轮尾 reasoning 照跑）；产物结构非法重试（artifact_parse ≤2）
  用尽 / 收尾期语义拒绝 → cycle=failed、Qn 回 open。
  **M0 无事务注记**：reasoning 收尾按 §4.2.5(a) 顺序执行但**非原子**——若失败发生在
  close/mark 之后（如 apply_tree_ops 被拒），已落的状态不回滚（尽力释放）；
  「任一步失败整体回滚」由 M1 phase_commit 事务兑现。
- route 派生完全由本驱动器（reasoning 只产 selection，§2.3）。

M0 造假的确定性：假指标值取固定基数 + 递增偏移（无随机、无时钟），同输入序列可复现（P6 精神）。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .compiler import StubCompiler, StubRecall
from .gate import ArtifactIndex, StubGate
from .goalbrief import parse_goal_brief
from .interfaces import Artifact, PlanOutcome, Route, Selection, Stage
from .runner import CodexRunner, RunnerError
from .schemas import SchemaSet
from .statestore import InMemoryStateStore

_SKILL_SECTION = {  # SKILL.md 的调用点 → 提示词节选说明（整文件内联，节名让工人聚焦）
    "idea_generate": "执行【生成任务】节（第 1 次调用，phase=idea）",
    "idea_audit": "执行【判官任务】节（第 2 次调用，phase=audit，独立会话）",
    "plan": "执行【计划任务】节（phase=plan）",
    "plan_review": "执行【评审任务】节（phase=audit，独立会话）",
    "reasoning": "执行【轮尾任务】步骤（按 route 分派轮型）",
}


class CycleFailed(RuntimeError):
    """系统性失败（产物结构非法重试用尽等）→ cycle=failed、Qn 回 open（§4.2.3 后两类）。"""


class SidecarRequested(RuntimeError):
    """阶段发出合法 resource_request sidecar 且主产物缺失（§3.1.1）。

    M0 桩语义：不触发真实全局等待（M2 起为桩、M5 真流水）——该阶段按失败观测处理，
    idea/plan 走「阶段失败=轮正常收尾」，reasoning 走 CycleFailed（无 selection 不可收尾）。
    请求单已校验并归档 cycles/<id>/interaction/。
    """


class M0Driver:
    def __init__(self, system_root: Path, work_root: Optional[Path] = None,
                 runner_factory=None):
        """runner_factory(transcripts_dir, purpose_tag) -> Runner；默认真 CodexRunner。

        可注入性只为测试替身（不花 token 的驱动器单测）；生产路径永远是真 codex（M0 起即真）。
        """
        self.root = Path(system_root)
        self.work = Path(work_root) if work_root else self.root / "questions" / "toy"
        with open(self.root / "policies" / "policy.yaml", encoding="utf-8") as f:
            self.policy = yaml.safe_load(f)
        brief = parse_goal_brief(self.root / "input" / "goal_brief.md")
        self.state = InMemoryStateStore(self.policy)
        self.state.create_goal(text=brief["body_md"], predicate_json=brief["predicate_json"])
        self.schemas = SchemaSet(self.root / "schemas")
        self.index = ArtifactIndex()
        self.gate = StubGate(self.schemas, self.index, self.state, self.work)
        self.compiler = StubCompiler(self.state, self.index, self.policy,
                                     goal_body_md=brief["body_md"], cycles_root=self.work)
        self.recall = StubRecall()
        self.system_prompt = (self.root / "prompts" / "system_prompt.md").read_text(encoding="utf-8")
        self.skills = {s: (self.root / "prompts" / "skills" / s / "SKILL.md").read_text(encoding="utf-8")
                       for s in ("idea", "plan", "bundle", "reasoning")}
        self._runner_factory = runner_factory or (
            lambda transcripts_dir, purpose_tag: CodexRunner(
                transcripts_dir=transcripts_dir, purpose_tag=purpose_tag))
        self._fake_no = 0
        self._call_seq = 0            # 全局调用序号：保证 transcript 文件名唯一（P6 回放，防覆盖）
        self._next_route: Route = "bootstrap"
        self._next_question: Optional[str] = None
        self.terminated = False

    # ---------------------------------------------------------------- 主循环 --
    def run_cycles(self, max_cycles: int) -> List[str]:
        done: List[str] = []
        while len(done) < max_cycles and not self.terminated:
            done.append(self.run_one_cycle())
        return done

    def run_one_cycle(self) -> str:
        """跑一轮；系统性失败（CycleFailed）按 §4.2.3 后两类：cycle=failed、Qn 回 open
        （不增 visit）、下轮以同一 route/问题重试（等下一轮或人工）。"""
        cycle = self.state.open_or_resume_cycle()
        try:
            self._run_cycle_body(cycle)
            self.state.mark_cycle_done(cycle.cycle_id)
        except CycleFailed as e:
            if cycle.question_id:
                self.state.release_question(cycle.question_id)
            self.state.mark_cycle_done(cycle.cycle_id, "failed")
            self.state._record("orchestrator", "cycle_failed", {"cycle": cycle.cycle_id, "err": str(e)[:300]})
        return cycle.cycle_id

    def _activate_or_halt(self, cycle, qid: Optional[str]) -> None:
        """激活下一目标；不可调度（如上轮收尾半途失败留下的过期指针）→ 本轮 failed 并停机。

        M0 无事务、无 M3 的断点重做：过期路由指针无法自愈，硬停比无限失败轮或裸异常
        炸穿更诚实（验收⓪会红）。"""
        try:
            self.state.activate_question(qid)
        except (ValueError, KeyError) as e:
            self.terminated = True
            self.state._record("orchestrator", "driver_halted_unrecoverable",
                               {"cycle": cycle.cycle_id, "next_question": qid, "err": str(e)[:200]})
            raise CycleFailed(f"下一目标不可调度（路由指针过期）：{qid}（{e}）——停机待人工") from e
        cycle.question_id = qid

    def _run_cycle_body(self, cycle) -> None:
        route = self._next_route
        self.state.set_route(cycle.cycle_id, route)
        if route in ("bootstrap", "decompose"):
            if route == "decompose":
                # §4.2.4：decompose 轮对选中的 active_question 写子问题（add_children 护栏
                # 要求父问题 active；同一 op 内父 active→open 释放）
                self._activate_or_halt(cycle, self._next_question)
            self._reasoning_stage(cycle, stage_failed=False)
            return
        # attack 起手（route 在 plan 后特化，§2.3 规则 5）
        self._activate_or_halt(cycle, self._next_question)
        stage_failed = False
        try:
            if not self._idea_stage(cycle):
                stage_failed = True
            else:
                plan_payload = self._plan_stage(cycle)
                if plan_payload is None:
                    stage_failed = True
                else:
                    route = self._specialize_route(self._classify_plan(plan_payload))
                    self.state.set_route(cycle.cycle_id, route)
                    if route in ("attack", "eval_only"):
                        self._bundle_stage(cycle, plan_payload)
        except SidecarRequested:
            stage_failed = True   # M0 桩：文件请求单已归档，按失败观测收尾（轮尾 reasoning 照跑）
        self._reasoning_stage(cycle, stage_failed=stage_failed)

    # ---------------------------------------------------------------- 阶段 --
    def _idea_stage(self, cycle) -> bool:
        """两次独立 runner_call：生成 + 判官（§7.2 特性③ M0 行）。返回是否有 selected。"""
        pack = self.compiler.render(cycle_id=cycle.cycle_id, stage="idea")
        draft = self._run_with_retry(cycle, "idea", "idea_generate", pack, "idea_set.draft.json")
        audit_pack = self.compiler.render_idea_audit(cycle_id=cycle.cycle_id, draft=draft)
        known_ids = {c["candidate_id"] for c in draft["candidates"]}
        audit = None
        for round_no in (1, 2):   # 候选一致性检查纳入重试语义（判官发明 id → 反馈一次重评）
            audit = self._run_with_retry(cycle, "idea", "idea_audit", audit_pack, "idea_audit.json")
            bogus = ({audit["selected_id"]} if audit["selected_id"] is not None else set()) - known_ids
            bogus |= {s["candidate_id"] for s in audit["audit_scores"]} - known_ids
            if not bogus:
                break
            if round_no == 1:
                self.compiler.amend(
                    audit_pack, title="上次评分被拒：candidate_id 不在候选集",
                    body_md=f"非法 id: {sorted(bogus)}；只许 {sorted(known_ids)}。请重评。",
                    source="feedback:artifact_parse", label="audit-fix")
            else:
                raise CycleFailed(f"判官引用不存在的候选（重评仍败）: {sorted(bogus)}")
        merged = dict(draft)
        merged["audit_scores"] = audit["audit_scores"]
        merged["selected_id"] = audit["selected_id"]
        res = self.gate.commit(Artifact(stage="idea", files={"idea_set.json": merged}, md=""),
                               cycle_id=cycle.cycle_id, stage="idea")
        if not res.ok:
            raise CycleFailed(f"idea_set 合并后不合契约: {res.errors[:3]}")
        return merged["selected_id"] is not None

    def _plan_stage(self, cycle) -> Optional[Dict[str, Any]]:
        """计划 + 独立评审；末轮 fail 后由 generator 定向修订一次。"""
        pack = self.compiler.render(cycle_id=cycle.cycle_id, stage="plan")
        plan = self._run_with_retry(cycle, "plan", "plan", pack, "plan.json")
        max_rounds = self.policy["flow"]["retry"]["plan_review"]
        for round_no in range(1, max_rounds + 1):
            if "import_defer" in plan:
                # M0 明确不支持 import deferred（§7.2 特性② M0 行为空；dependency_wait 的
                # 机械收尾与业务三写入是 M1–M3 的事）——按阶段失败收尾，防 route 矩阵悬空
                self.state._record("orchestrator", "import_defer_unsupported_m0",
                                   {"cycle": cycle.cycle_id})
                return None
            review = self._plan_review_call(cycle, plan, round_no)
            if review["verdict"] == "pass":
                res = self.gate.commit(Artifact(stage="plan", files={"plan.json": plan}, md=""),
                                       cycle_id=cycle.cycle_id, stage="plan")
                if not res.ok:
                    raise CycleFailed(f"plan 入库被拒: {res.errors[:3]}")
                return plan
            if round_no < max_rounds:
                pack = self.compiler.render(cycle_id=cycle.cycle_id, stage="plan")
                self.compiler.amend(
                    pack, title="上一轮可回答性评审意见（逐条修复后重出 plan.json）",
                    body_md="```json\n" + json.dumps(review["issues"], ensure_ascii=False, indent=2) + "\n```",
                    source="staging:plan_review.json", label=f"replan{round_no}")
                plan = self._run_with_retry(cycle, "plan", "plan", pack, "plan.json")
                continue

            # Keep the M0 behavioral model aligned with the production
            # AttackStages path: one independent reviewer is literal, and its
            # final fail buys the original generator one bounded repair rather
            # than silently becoming a second review call.
            rejected = plan
            pack = self.compiler.render(cycle_id=cycle.cycle_id, stage="plan")
            self.compiler.amend(
                pack, title="独立可回答性评审意见（仅一次；逐条修复后重出完整 plan.json）",
                body_md="```json\n" + json.dumps(
                    review["issues"], ensure_ascii=False, indent=2) + "\n```",
                source="staging:plan_review.json", label=f"repair{round_no}")
            plan = self._run_with_retry(cycle, "plan", "plan", pack, "plan.json")
            if plan == rejected or "import_defer" in plan:
                return None
            self.state._record("agent", "plan_review_repair", {
                "cycle": cycle.cycle_id,
                "round_no": round_no,
                "reviewed_plan_hash": hashlib.sha256(json.dumps(
                    rejected, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":")).encode("utf-8")).hexdigest(),
                "plan_hash": hashlib.sha256(json.dumps(
                    plan, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":")).encode("utf-8")).hexdigest(),
            })
            res = self.gate.commit(
                Artifact(stage="plan", files={"plan.json": plan}, md=""),
                cycle_id=cycle.cycle_id, stage="plan")
            if not res.ok:
                return None
            return plan
        return None

    def _plan_review_call(self, cycle, plan: Dict[str, Any], round_no: int) -> Dict[str, Any]:
        pack = self.compiler.render_plan_review(cycle_id=cycle.cycle_id, plan=plan, round_no=round_no)
        return self._run_with_retry(cycle, "plan", "plan_review", pack, "plan_review.json")

    def _bundle_stage(self, cycle, plan: Dict[str, Any]) -> None:
        """M0：驱动器确定性造假逐 target 执行（标 fake/synthetic），不起 Codex 构建会话。

        虽无 runner 调用，仍逐 target 落输入溯源 manifest（假执行消费的是 plan 切片——
        验收②对 bundle 阶段的证据）。"""
        for target in sorted(plan.get("targets", []), key=lambda t: t["seq"]):
            self.state.set_bundle_cursor(cycle.cycle_id, target["target_key"])
            payload = self._fabricate_target(cycle, plan, target)
            slice_hash = hashlib.sha256(
                json.dumps({"target": target, "protocol": plan["protocol"]},
                           ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
            self.compiler.write_stub_manifest(
                cycle_id=cycle.cycle_id, stage="bundle", target_id=target["target_key"],
                sources=[f"artifact:{cycle.cycle_id}:plan:plan.json#target={target['target_key']}"],
                content_hash=slice_hash)
            res = self.gate.commit(
                Artifact(stage="bundle", files={"bundle_target.json": payload}, md=""),
                cycle_id=cycle.cycle_id, stage="bundle", target_id=target["target_key"])
            if not res.ok:
                raise CycleFailed(f"假执行产物不合契约（驱动器 bug）: {res.errors[:3]}")
        self.state.set_bundle_cursor(cycle.cycle_id, None)

    def _fabricate_target(self, cycle, plan: Dict[str, Any], target: Dict[str, Any]) -> Dict[str, Any]:
        self._fake_no += 1
        n = self._fake_no
        kind = target["target_kind"]
        proto = plan["protocol"]
        required = [m for m in plan.get("build_target_required_metric", [])
                    if m["target_key"] == target["target_key"]]
        base_val = 0.90
        metrics = []
        for i, m in enumerate(required or [{"metric_id": "accuracy", "metric_ver": 1}]):
            val = round(base_val + 0.01 * ((n + i) % 5) - 0.02, 4)   # 确定性假值
            metrics.append({"metric_result_id": f"mr:{cycle.cycle_id}:{target['target_key']}#{m['metric_id']}@{m['metric_ver']}:aggregate",
                            "metric_id": m["metric_id"], "metric_ver": m["metric_ver"],
                            "value": val, "scope": "aggregate"})
        ref_base = f"questions/toy/cycles/{cycle.cycle_id}/build/{target['target_key']}"
        fake_hash = lambda tag: "sha256:" + hashlib.sha256(f"{cycle.cycle_id}:{target['target_key']}:{tag}".encode()).hexdigest()
        payload: Dict[str, Any] = {
            "target_key": target["target_key"],
            "target_kind": kind,
            "status": "complete",
            "evaluation": {"eval_key": target.get("eval_key", f"fake-{n}"),
                           "protocol_name": proto["name"], "protocol_ver": proto["version"],
                           "source": "fake", "status": "success"},
            "attempt": {"attempt_no": 1,
                        "purpose": target.get("attempt_purpose", "factory"),
                        "status": "success", "cost": 0.0},
            "metric_results": metrics,
            "execution_logs": [
                {"log_kind": "eval", "ref": f"{ref_base}/eval.fake.log",
                 "content_hash": fake_hash("eval-log"), "synthetic": True}],
            "execution_observation": {"source": "fake", "synthetic": True,
                                      "nan_seen": False, "loss_trend": "unknown",
                                      "wall_clock_sec": 1.0},
            "result_review": {"review_kind": "bundle_result_review", "verdict": "pass",
                              "round_no": 1, "stub": True,
                              "comments_md": "M0 占位判官：自动通过"},
        }
        if kind in ("build", "exec"):
            claim = target.get("claim", {})
            payload["variant"] = {"variant_key": claim.get("variant_key", f"v-fake-{n}"),
                                  "config_json": claim.get("config_json", {"fake": True}),
                                  "overrides": []}
            payload["run"] = {"kind": kind, "status": "success", "seed": n, "cost": 0.0}
            payload["checkpoints"] = [{"ckpt_key": "final", "path": f"{ref_base}/ckpt-final.fake.json",
                                       "content_hash": fake_hash("ckpt"), "hash_alg": "sha256"}]
            payload["identity_draft_md"] = (f"## identity 草稿（M0 假执行）\n- 名称：{claim.get('canonical_key') or claim.get('baseline_ref', 'fake')}"
                                            f"\n- 复现命令：（M0 假执行占位）\n- 环境指纹：{fake_hash('env')}")
            payload["execution_logs"].insert(0, {"log_kind": "train", "ref": f"{ref_base}/train.fake.log",
                                                 "content_hash": fake_hash("train-log"), "synthetic": True})
            payload["execution_observation"].update({"last_loss": 0.2, "loss_trend": "down"})
            payload["code_review"] = {"review_kind": "bundle_code_review", "verdict": "pass",
                                      "round_no": 1, "stub": True, "comments_md": "M0 占位判官：自动通过"}
            payload["run"]["env_hash"] = fake_hash("env")
        return payload

    def _reasoning_stage(self, cycle, *, stage_failed: bool) -> None:
        # 不预置 inconclusive（§4.2.3 第 2 行：此后若 CycleFailed，Qn 须仍为 active 才能
        # 释放回 open 且不增 visit）；R3 可选集含本轮 active Qn 由编译器保证（compiler._open_set）
        qn = cycle.question_id
        pack = self.compiler.render(cycle_id=cycle.cycle_id, stage="reasoning")
        if stage_failed:
            self.compiler.amend(
                pack, title="本轮阶段失败提示",
                body_md="上游阶段失败（详见结果区）：不产 answer.json；"
                        "Qn 将被置 inconclusive，可在 selection 重选它（含在 scores 里打分）。",
                source="orchestrator:stage_failed_note", label="failnote")
        reasoning_only = cycle.route in ("bootstrap", "decompose", "goal_amend")
        files = self._run_reasoning_with_retry(
            cycle, pack, answer_allowed=not stage_failed and not reasoning_only)
        # DB 收尾顺序对齐 §4.2.5(a)：answer→question.status→apply_tree_ops→persist_selection。
        # StateStore 语义拒绝（轮型/护栏/可调度性）→ CycleFailed（§4.2.3 第 2 行），
        # 不让 ValueError 炸穿整跑（M1 起为 phase_commit 事务整体回滚）。
        try:
            if "answer.json" in files and qn and not stage_failed and not reasoning_only:
                ans = files["answer.json"]
                if ans["question_id"] != qn:
                    raise CycleFailed(f"answer.question_id（{ans['question_id']}）≠ 本轮 Qn（{qn}）"
                                      "——不得关别的问题")
                self.state.close_question(cycle.cycle_id, ans["question_id"], ans["verdict"],
                                          ans["evidence"], ans["answer_md"])
            elif qn and not reasoning_only:
                self.state.mark_inconclusive(qn)   # 阶段失败 / 工人自判证据不足（R1 无 answer）
            # reasoning-only 轮不答题：decompose 的父问题已在 add_children 内 active→open 释放
            self.state.apply_tree_ops(cycle.cycle_id, files.get("tree_ops.json", {"ops": []})["ops"])
            sel_raw = files["selection.json"]
            self.state.persist_selection(cycle.cycle_id, Selection(
                next_question_id=sel_raw["next_question_id"],
                next_intent=sel_raw["next_intent"],
                scores=sel_raw.get("scores", [])))
        except ValueError as e:
            raise CycleFailed(f"reasoning 收尾被 StateStore 拒绝：{e}") from e
        self._route_from_selection(cycle)

    # ---------------------------------------------------------------- 路由 --
    def _classify_plan(self, plan: Dict[str, Any]) -> PlanOutcome:
        targets = plan.get("targets", [])
        kinds = {t["target_kind"] for t in targets}
        return PlanOutcome(
            has_build_or_exec=bool(kinds & {"build", "exec"}),
            only_eval=bool(targets) and kinds == {"eval"},
            empty_targets=not targets,
            blocked=False,
            import_deferred="import_defer" in plan,
        )

    @staticmethod
    def _specialize_route(o: PlanOutcome) -> Route:
        """§6.13(3) 矩阵（attack intent 分支；M0 无 blocked/import_deferred 实例）。"""
        if o.blocked or o.import_deferred:
            return "dependency_wait"
        if o.has_build_or_exec:
            return "attack"
        if o.only_eval:
            return "eval_only"
        return "reuse_only"

    def _route_from_selection(self, cycle) -> None:
        """读 persist_selection 落到 cycle 的持久位（local_key 已解析为真实 id）。"""
        intent = cycle.next_intent
        if intent == "terminate":
            self.terminated = True
            self._next_question = None
        elif intent == "decompose":
            self._next_route = "decompose"
            self._next_question = cycle.next_question_id
        else:
            self._next_route = "attack"
            self._next_question = cycle.next_question_id

    # ---------------------------------------------------------------- 调用 --
    def _extract_sidecar(self, cycle, art: Artifact, main_missing: bool) -> None:
        """统一可选 sidecar（§3.1.1）：校验→归档→（主产物缺失时）转 SidecarRequested。

        Gate 拒 sidecar 直 commit（CP1.3 定死），故必须在 commit 前从 files 摘出。
        sidecar 非法 → 不豁免 artifact_parse（按普通产物缺陷走重试），此处抛 ValueError 由
        调用方计入 last_err。
        """
        sidecar = art.files.pop("resource_request.json", None)
        if sidecar is None:
            return
        v = self.schemas.validator("resource_request")
        errors = [f"{e.json_path} {e.message}" for e in v.iter_errors(sidecar)]
        if errors:
            raise ValueError("sidecar 不合 schema（不豁免重试）:\n" + "\n".join(errors[:3]))
        d = self.work / f"cycles/{cycle.cycle_id}/interaction"
        d.mkdir(parents=True, exist_ok=True)
        (d / "resource_request.json").write_text(
            json.dumps(sidecar, ensure_ascii=False, indent=2), encoding="utf-8")
        self.state._record("orchestrator", "resource_request_stub",
                           {"cycle": cycle.cycle_id, "items": len(sidecar["items"])})
        if main_missing:
            raise SidecarRequested(f"阶段发出文件请求单（{len(sidecar['items'])} 条）且主产物缺失；"
                                   "M0 桩：不进入全局等待，按失败观测收尾")

    def _run_with_retry(self, cycle, stage: Stage, call_key: str, pack, filename: str) -> Dict[str, Any]:
        """runner 调用 + schema 预检（artifact_parse ≤2 重试，附错误反馈）→ 返回该文件 payload。"""
        retries = self.policy["flow"]["retry"]["artifact_parse"]
        self._call_seq += 1
        runner = self._runner_factory(self.work / f"cycles/{cycle.cycle_id}/transcripts",
                                      f"{call_key}-n{self._call_seq}")
        skill = self.skills[stage] + f"\n\n===== 调用点 =====\n本次调用：{_SKILL_SECTION[call_key]}；只产 files[\"{filename}\"]。"
        last_err = ""
        for attempt in range(retries + 1):
            if last_err:
                self.compiler.amend(
                    pack, title=f"上次产物被拒（第 {attempt} 次重试）",
                    body_md=f"{last_err}\n请修正后重出。",
                    source="feedback:artifact_parse", label=f"n{self._call_seq}-retry{attempt}")
            try:
                art = runner.run_task(system_prompt=self.system_prompt, skill=skill, context_pack=pack)
                self._extract_sidecar(cycle, art, main_missing=filename not in art.files)
            except RunnerError as e:
                last_err = str(e)
                continue
            except ValueError as e:   # sidecar 非法：计入重试反馈
                last_err = str(e)
                continue
            payload = art.files.get(filename)
            if payload is None:
                last_err = f"缺产物文件 {filename}（files 键: {list(art.files)}）"
                continue
            v = self.schemas.validator_for_artifact(filename)
            errors = []
            for e in v.iter_errors(payload):
                errors.append(f"{e.json_path} {e.message}")
                stack = list(e.context or [])
                while stack:                       # 展平 oneOf 子错误：反馈才有具体键名
                    sub = stack.pop()
                    errors.append(f"{sub.json_path} {sub.message}")
                    stack.extend(sub.context or [])
            if errors:
                last_err = "schema 校验失败:\n" + "\n".join(errors[:8])
                continue
            return payload
        raise CycleFailed(f"{call_key} 产物结构非法，artifact_parse 重试（≤{retries}）用尽：{last_err[:300]}")

    def _run_reasoning_with_retry(self, cycle, pack, *, answer_allowed: bool) -> Dict[str, Any]:
        """reasoning 产多文件：selection 必产、tree_ops 可空、answer 条件产。

        answer_allowed=False（stage_failed / reasoning-only 轮）时，携带 answer.json 的产物
        在 commit 前被拒回（防 artifact 落盘而 StateStore 不关题的分歧）。"""
        retries = self.policy["flow"]["retry"]["artifact_parse"]
        self._call_seq += 1
        runner = self._runner_factory(self.work / f"cycles/{cycle.cycle_id}/transcripts",
                                      f"reasoning-n{self._call_seq}")
        skill = self.skills["reasoning"] + "\n\n===== 调用点 =====\n本次调用：" + _SKILL_SECTION["reasoning"]
        last_err = ""
        for attempt in range(retries + 1):
            if last_err:
                self.compiler.amend(
                    pack, title=f"上次产物被拒（第 {attempt} 次重试）",
                    body_md=f"{last_err}\n请修正后重出。",
                    source="feedback:artifact_parse", label=f"n{self._call_seq}-retry{attempt}")
            try:
                art = runner.run_task(system_prompt=self.system_prompt, skill=skill, context_pack=pack)
                # reasoning 的 sidecar：无 selection 则本轮无法收尾 → SidecarRequested 上抛为
                # CycleFailed（M0 桩注记见 SidecarRequested docstring）
                self._extract_sidecar(cycle, art, main_missing="selection.json" not in art.files)
            except RunnerError as e:
                last_err = str(e)
                continue
            except ValueError as e:
                last_err = str(e)
                continue
            except SidecarRequested as e:
                raise CycleFailed(str(e))
            if not answer_allowed and "answer.json" in art.files:
                last_err = "本轮型不得产 answer.json（阶段失败 / reasoning-only 轮）——去掉后重出"
                continue
            if "selection.json" not in art.files:
                # 必产检查先于 commit：缺 selection 的批次整体不落盘（防重试后 artifacts
                # 残留首次的 answer/tree_ops 与最终状态分歧）
                last_err = "缺 selection.json（reasoning 必产）——本批产物未入库，补齐后整批重出"
                continue
            res = self.gate.commit(art, cycle_id=cycle.cycle_id, stage="reasoning")
            if not res.ok:
                last_err = "门禁拒绝:\n" + "\n".join(res.errors[:5])
                continue
            return art.files
        raise CycleFailed(f"reasoning 产物非法，重试用尽：{last_err[:300]}")
