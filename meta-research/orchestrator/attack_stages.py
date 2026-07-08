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
provider 契约（注入式；生产 = 真 Codex 会话，范式见 M0 driver._run_with_retry；测试 = 确定性替身）：
- idea(cyc, pack) → {"idea_set.json": {candidates:[{candidate_id, content_md, audit_score?}], selected_id}}
- plan(cyc, pack) → {"plan.json": {protocol:{id, version}, targets:[TARGET_SPEC…]}}
  TARGET_SPEC（build 种）= {kind:'build', seq, canonical_key, slug, identity_draft_md, repro_cmd,
    train_cmd:[…], smoke_cmd:[…], eval_cmd:[…], ckpt_path, eval_key, target_set_hash,
    required:[[metric_id,metric_ver]…], config_json?}
- judge(cycle_id, build_target_id, review_kind, subject_hash) → 写 runner_call(audit)+DECISION(judge)（含 fail 权）
- reasoning(cyc, pack) → {"answer.json"?, "tree_ops.json"?, "selection.json"}（answer.evidence 引用真 metric_result）
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import harness as H
from . import obs_parser as OP
from . import subject_manifest as SM
from .gate_exec import ExecGate
from .gate_pool import PoolGate
from .ids import cnum as _cnum
from .interfaces import Selection
from .phase_commit import check_or_record

_TERMINAL_TARGET = ("complete", "skipped", "failed", "engineering_blocked")


def _canon_hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


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
                 obs_policy: Dict[str, Any], work_root: str):
        """state=SQLiteStateStore；compiler=SqliteCompiler；pool_gate=PoolGate(含 ExecGate 全家)；
        close_gate=SqliteGate（parser_suspect 已接真）；providers 见模块注释；work_root=staging 根目录。"""
        self.state = state
        self.compiler = compiler
        self.gate: PoolGate = pool_gate
        self.close_gate = close_gate
        self.p = providers
        self.obs_policy = obs_policy
        self.work = Path(work_root)

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
        """idea 阶段（§3.2）：候选全量入 IDEA 表（防重复造轮的关键边，含 failed）+ selected 标记。单一事务。"""
        pack = self.compiler.render(cycle_id=cyc.cycle_id, stage="idea")
        files = self.p["idea"](cyc, pack)
        iset = files["idea_set.json"]
        cands, selected = iset["candidates"], iset.get("selected_id")
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
                st = "selected" if c["candidate_id"] == selected else c.get("status", "candidate")
                conn.execute("INSERT INTO idea(question_id,cycle_id,content_md,audit_score,status) VALUES (?,?,?,?,?)",
                             (qi, ci, c["content_md"], c.get("audit_score"), st))
            conn.execute("UPDATE cycle SET status='idea' WHERE id=?", (ci,))

    # ---------------------------------------------------------------- plan --
    def _plan_stage(self, cyc) -> None:
        """plan 阶段：落 build_target[] + 池占位（claim 语义**事务内内联**——gate_claim_* 是外部单独入口，
        此处与 phase_commit/status 同生共死防「claim 成功但 target 未落」的半态；判据面同 claim：canonical_key
        I5 前置、identity 非空）。单一事务。"""
        pack = self.compiler.render(cycle_id=cyc.cycle_id, stage="plan")
        files = self.p["plan"](cyc, pack)
        plan = files["plan.json"]
        ah = _canon_hash(plan)
        ci, qi = _cnum(cyc.cycle_id), int(cyc.question_id[1:])
        with self.state.daemon.transaction() as conn:
            pc = check_or_record(conn, cycle_id=cyc.cycle_id, stage="plan", target_id=None, artifact_hash=ah)
            if pc == "conflict":
                raise ValueError("plan 阶段 phase_commit 冲突：同键异 artifact_hash")
            if pc == "duplicate":
                return
            for t in sorted(plan.get("targets", []), key=lambda x: x["seq"]):
                if t["kind"] != "build":
                    raise NotImplementedError(f"CP5.4 plan 只落 build 目标（exec 链已由 gate 级验证；import=CP5.5）：{t['kind']}")
                if not t.get("identity_draft_md", "").strip():
                    raise ValueError("plan build 目标缺 identity 草稿（claim 判据面）")
                if conn.execute("SELECT 1 FROM baseline WHERE canonical_key=?", (t["canonical_key"],)).fetchone():
                    raise ValueError(f"canonical_key 已占（I5）: {t['canonical_key']!r}")
                bid = conn.execute("INSERT INTO baseline(slug,canonical_key,identity_doc,born_cycle,status) "
                                   "VALUES (?,?,?,?,'planned')",
                                   (t["slug"], t["canonical_key"], t["identity_draft_md"], ci)).lastrowid
                vid = conn.execute("INSERT INTO variant(baseline_id,variant_key,config_json,status) "
                                   "VALUES (?,?,?,'planned')",
                                   (bid, t.get("variant_key", "base"), t.get("config_json", "{}"))).lastrowid
                bt = conn.execute("INSERT INTO build_target(cycle_id,question_id,target_kind,seq,status,"
                                  "baseline_id,variant_id,plan_ref) VALUES (?,?,'build',?,'pending',?,?,?)",
                                  (ci, qi, t["seq"], bid, vid, json.dumps(t, sort_keys=True))).lastrowid
                for (mid, mver) in t["required"]:
                    conn.execute("INSERT INTO build_target_required_metric(build_target_id,metric_id,metric_ver) "
                                 "VALUES (?,?,?)", (bt, mid, mver))
            conn.execute("UPDATE cycle SET status='plan' WHERE id=?", (ci,))

    # ---------------------------------------------------------------- bundle --
    def _bundle_stage(self, cyc) -> None:
        """bundle：逐目标（seq 序）两段提交；每目标进度从 DB 状态结构性续。全部终态后推 status='bundle'。"""
        ci = _cnum(cyc.cycle_id)
        rows = self.state.daemon.query(
            "SELECT id FROM build_target WHERE cycle_id=? ORDER BY seq", (ci,))
        for (bt_id,) in rows:
            self._drive_target(cyc, bt_id)
        with self.state.daemon.transaction() as conn:
            conn.execute("UPDATE cycle SET status='bundle' WHERE id=?", (ci,))

    def _target_spec(self, bt_id: int) -> Dict[str, Any]:
        row = self.state.daemon.query_one("SELECT plan_ref FROM build_target WHERE id=?", (bt_id,))
        return json.loads(row[0])

    def _drive_target(self, cyc, bt_id: int) -> None:
        """单目标推进（可重入）：按当前状态从断点续。"""
        g = self.gate
        d = self.state.daemon
        spec = self._target_spec(bt_id)
        st = lambda: d.query_one("SELECT status FROM build_target WHERE id=?", (bt_id,))[0]
        if st() in _TERMINAL_TARGET:
            self._ensure_target_pc(cyc, bt_id)   # 崩在 complete 与 pc 之间 → 补 pc（幂等）
            return
        staging = self.work / f"c{_cnum(cyc.cycle_id)}" / f"t{bt_id}"
        if st() == "pending":
            g.gate_start_build_target(build_target_id=bt_id)
        if st() == "building":                    # 真 smoke（子进程）→ 过了才进 smoke 态
            sm = H.run_staged(spec["smoke_cmd"], staging_dir=str(staging / "smoke"),
                              log_name=f"smoke-{self._next_serial(staging, 'smoke')}.log", timeout_s=120)
            if sm["exit_code"] != 0:              # smoke 失败 → target 失败连坐（codex SHOULD：exit code 不得忽略）
                g.gate_finish_build_target(build_target_id=bt_id, status="failed", failure_kind="smoke")
                self._ensure_target_pc(cyc, bt_id)   # 终态早退**也**落 pc（codex 第2轮 BLOCKER：漏落致杀/不杀分裂）
                return
            g.gate_progress_build_target(build_target_id=bt_id, to="smoke")
        if st() == "smoke":                       # 代码适配评审（subject 编排器重算；judge replay-safe）
            code_sh = self._code_subject_hash(bt_id, spec, staging)
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
            self._run_and_register(cyc, bt_id, spec, staging)
        self._ensure_target_pc(cyc, bt_id)

    def _run_and_register(self, cyc, bt_id: int, spec, staging: Path) -> None:
        """phase (i) 执行事实 + phase (ii) 注册段（结构可恢复的短事务序列）。
        ⚠️ 与 import_worker._run_and_register_import **同构**（恢复缝隙修复须双向同步；共享骨架=M6 硬化）。"""
        g, d = self.gate, self.state.daemon
        ci = cyc.cycle_id
        vid = d.query_one("SELECT variant_id FROM build_target WHERE id=?", (bt_id,))[0]
        bid = d.query_one("SELECT baseline_id FROM build_target WHERE id=?", (bt_id,))[0]
        # —— (i) 训练 run（结构续：残留 running run 先 abort；成功 run 直接复用）——
        run_row = d.query_one("SELECT id,status FROM run WHERE build_target_id=? ORDER BY id DESC", (bt_id,))
        if run_row and run_row[1] == "running":   # 崩溃残留：终结后重跑（run append-only，不复用半途 run）
            g.gate_finish_run(run_id=run_row[0], status="failed", failure_kind="aborted")
            run_row = None
        if run_row and run_row[1] == "success":
            rid = run_row[0]
        else:
            rid = g.gate_start_run(build_target_id=bt_id, cycle_id=ci, variant_id=vid, kind="build",
                                   env_hash=spec.get("env_hash", "toy-env"))
            r = H.run_staged(spec["train_cmd"], staging_dir=str(staging / f"run{rid}"),
                             log_name="train.log", timeout_s=600)
            if r["exit_code"] != 0:
                g.gate_finish_run(run_id=rid, status="failed", failure_kind="runtime")
                g.gate_finish_build_target(build_target_id=bt_id, status="failed", failure_kind="runtime")
                return                            # 训练失败入账不入树（§7.1 判例④；答题侧自然无证据）
            ck_path = staging / f"run{rid}" / spec["ckpt_name"]
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
                ev = H.run_staged(spec["eval_cmd"], staging_dir=str(staging / f"eval{rid}"),
                                  log_name="eval.log", timeout_s=600)
                eval_log = Path(ev["log_path"]).read_bytes()
            if ev["exit_code"] != 0:              # fresh 与 resume 同一判定点（评估进程失败 → target failed）
                g.gate_finish_build_target(build_target_id=bt_id, status="failed", failure_kind="runtime")
                return
            metrics = self._metrics_from_eval_log(eval_log.decode("utf-8", errors="replace"), spec)
            res_sh = self._result_subject_hash(bt_id, spec, rid, metrics, ev)
            self._judge_once(ci, bt_id, "bundle_result_review", res_sh)
            if not g.review_passed(build_target_id=bt_id, review_kind="bundle_result_review",
                                   current_subject_hash=res_sh):
                # 结果评审 FAIL → review_failed：run(success)+checkpoint 保留（训练事实），测量整包不注册
                # （§4.2.5：第(ii)段不发生）——lockstep：import_worker 同修
                g.gate_finish_build_target(build_target_id=bt_id, status="failed", failure_kind="review_failed")
                return
            reg = self.gate.gate_register_evaluation(
                cycle_id=ci, build_target_id=bt_id, purpose="factory", current_subject_hash=res_sh,
                metric_results=metrics,
                create={"variant_id": vid, "protocol_id": spec["protocol_id"], "protocol_ver": spec["protocol_ver"],
                        "eval_key": spec["eval_key"], "source": "factory", "target_set_hash": spec["target_set_hash"]},
                env_hash=spec.get("env_hash", "toy-env"),
                artifact_ref=f"sha256:{ev['log_sha256']}")   # 评估 log 哈希锚落 attempt（补登强校验用，codex BLOCKER）
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
            self.gate.gate_register_baseline(
                baseline_id=bid, variant_id=vid, build_target_id=bt_id, evaluation_id=eid,
                cycle_id=ci, current_subject_hash=res_sh2,
                identity_doc=spec["identity_draft_md"], repro_cmd=spec["repro_cmd"], run_id=rid)
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
    def _code_subject_hash(self, bt_id: int, spec, staging: Path) -> str:
        smoke_dir = staging / "smoke"
        logs = sorted(smoke_dir.glob("smoke-*.log"))
        smoke_ref = str(logs[-1]) if logs else "smoke:none"
        smoke_hash = H.file_sha256(smoke_ref) if logs else "0" * 64
        return SM.subject_hash(SM.code_review_manifest(
            plan_slice_hash=_canon_hash(spec), code_diff_hash=_canon_hash(spec["train_cmd"]),
            config_hashes={}, identity_draft_hash=_canon_hash(spec["identity_draft_md"]),
            smoke_transcript_ref=smoke_ref, smoke_transcript_hash=smoke_hash))

    def _result_subject_hash(self, bt_id: int, spec, rid: int, metrics, ev) -> str:
        ckrow = self.state.daemon.query_one(
            "SELECT ckpt_key, content_hash FROM checkpoint WHERE produced_by_run=?", (rid,))
        return SM.subject_hash(SM.result_review_manifest(
            metrics_artifact_hash=_canon_hash(metrics), checkpoint_hashes={ckrow[0]: ckrow[1]},
            run_log_hashes={ev["log_path"]: ev["log_sha256"]},
            parser_obs_hash=_canon_hash(OP.parse_log(Path(ev["log_path"]).read_text(), self.obs_policy)),
            identity_draft_hash=_canon_hash(spec["identity_draft_md"])))

    @staticmethod
    def _metrics_from_eval_log(text: str, spec) -> List[Dict[str, Any]]:
        """评估产物口径（toy 最小）：eval log 每行 `metric_value: <mid>@<mver>=<float>` → aggregate metric_result。
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
            self.state.persist_selection(cyc.cycle_id, Selection(
                next_question_id=sel.get("next_question_id"), next_intent=sel["next_intent"],
                scores=sel.get("scores", [])))
            self.state.mark_cycle_done(cyc.cycle_id)
