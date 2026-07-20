"""ExecGate —— 执行生命周期业务门禁（§4.1.4 池注册 gate 家族·执行侧；M4 CP5.1）。

覆盖：gate_start/progress/finish_build_target、gate_start/finish_run、gate_start/finish_attempt、
gate_finish/abandon_evaluation。注册/评审侧（gate_claim/register_baseline·variant、gate_new_protocol、
pool_publish）= CP5.2；本模块提供两者共用的双评审 DECISION 机械判据校验 `review_passed`（§4.1.4 附注）。

纪律（同 SqliteGate）：
- 判据**读走受限只读连接**（authorizer 拒 log/interaction/external 9 表——执行判据只涉 run/attempt/evaluation/
  build_target/variant/baseline/checkpoint/protocol_metric/decision/runner_call/**evidence**（abandon 引用核），
  全在允许集；观测隔离由此焊死）。
- 写走 WriteDaemon 单写**短**事务（长操作绝不持写事务，§6.13——训练/评估在事务外，gate 只入账）。
- 拒绝 = 记 DECISION(actor=gate, type=reject)（FK-null 安全）+ 抛 GateReject（可行动拒因）。
- DB 触发器（trg_run_frozen/trg_run_target_consistent/trg_attempt_frozen/trg_eval_sc_*/trg_mr_i2/
  trg_eval_no_abandon_if_cited…）是最终焊死层；本模块是前置门禁（干净拒 + 触发器未覆盖的 gate 级判据）。

**bundle 串行（§4.2.5）**：目标间不共事务、按 seq 串行。「当前目标」**从 build_target 状态结构性推导**
（= 本 cycle 最小 seq 的非终态目标），不依赖内存 cursor —— 崩溃重启即可从结构恢复（statestore 的
in-process bundle_cursor 只是提示，真相在此）。

**check-then-act 注**：本模块判据在写事务**前**读（受限连接），未如 gate_close_question 在写锁内重跑
gate-only 判据——单写者模型（WriteDaemon 单连接串行）下读写间无并发写入窗口；触发器兜底层与
IntegrityError→干净拒兜其余。引入多写者（M5）时须升级为写锁内重跑。
"""
from __future__ import annotations

import json
import math
import sqlite3
from typing import Any, Dict, List, Optional

from .gate_sqlite import GateReject
from .ids import cnum as _cnum
from .writedaemon import WriteDaemon

_TERMINAL_TARGET = ("complete", "skipped", "failed", "engineering_blocked")
_ATTEMPT_PURPOSES = ("factory", "retry", "metric_append", "repro_eval", "standalone_eval", "protocol_upgrade")


class ExecGate:
    def __init__(self, daemon: WriteDaemon, read_conn: sqlite3.Connection, *,
                 require_code_review: bool = True,
                 require_result_review: bool = True):
        if type(require_code_review) is not bool or type(require_result_review) is not bool:
            raise TypeError("review gate 策略必须是显式 bool")
        self.daemon = daemon        # 写路径（无 authorizer）
        self.read = read_conn       # 判据读路径（authorizer 拒 9 表）
        # 评审是可选的模型策略；生命周期、运行、指标、证据和 DB 约束不受此开关影响。
        self.require_code_review = require_code_review
        self.require_result_review = require_result_review

    # -- 基础设施 ---------------------------------------------------------------
    def _q1(self, sql: str, params=()):
        return self.read.execute(sql, params).fetchone()

    def _reject(self, cycle_id: Optional[int], msg: str, **attempted):
        """干净拒：DECISION(actor=gate,type=reject) + GateReject。cycle 引用可能不存在 → FK 列判空写。"""
        with self.daemon.transaction() as conn:
            cref = cycle_id if cycle_id is not None and conn.execute(
                "SELECT 1 FROM cycle WHERE id=?", (cycle_id,)).fetchone() else None
            conn.execute("INSERT INTO decision(cycle_id,actor,type,payload_json) VALUES (?,'gate','reject',?)",
                         (cref, json.dumps({"reason": msg, **attempted}, ensure_ascii=False)))
        raise GateReject(msg)

    # -- 双评审 DECISION 机械判据（§4.1.4 附注；CP5.2 register 亦用） -------------
    def review_passed(self, *, build_target_id: int, review_kind: str, current_subject_hash: str) -> bool:
        """通过 ⟺ 存在 verdict=pass 的 DECISION(actor=judge,type=review_kind) 且
        ① payload.subject_hash == 编排器**当下重算**的 current_subject_hash（产物变 → 旧 pass 自动失效）
        ② payload.runner_call_id → runner_call(status='success', phase='audit', purpose=review_kind)。
        取该 target 最新一条对应 DECISION 判定（新 FAIL 覆盖旧 PASS；修复后须重评审、产新 DECISION）。
        **畸形 payload fail-closed**：decision.payload_json 无 json_valid CHECK（冻结 DDL）——SQL 侧先
        json_valid 过滤防 json_extract 裸抛；且若存在**更新的**畸形同类 judge DECISION（其 target 不可知，
        可能正是本 target 的新 FAIL）→ 整体 fail closed，不让旧 PASS 静默生效（codex SHOULD）。"""
        row = self._q1(
            "SELECT id, payload_json FROM decision WHERE actor='judge' AND type=? "
            "AND json_valid(payload_json) AND json_extract(payload_json,'$.build_target_id')=? "
            "ORDER BY id DESC LIMIT 1",
            (review_kind, build_target_id))
        newer_malformed = self._q1(
            "SELECT 1 FROM decision WHERE actor='judge' AND type=? AND NOT json_valid(payload_json) AND id>?",
            (review_kind, row[0] if row else 0))
        if newer_malformed or row is None:
            return False
        p = json.loads(row[1])   # json_valid 已保证可解析
        if p.get("verdict") != "pass" or p.get("subject_hash") != current_subject_hash:
            return False
        rc = self._q1(
            "SELECT status,phase,purpose,cycle_id FROM runner_call WHERE id=?",
            (p.get("runner_call_id"),))
        target = self._q1("SELECT cycle_id FROM build_target WHERE id=?", (build_target_id,))
        return (rc is not None and target is not None and rc[0] == "success"
                and rc[1] == "audit" and rc[2] == review_kind and rc[3] == target[0])

    # -- build_target 生命周期 ---------------------------------------------------
    def _bt(self, build_target_id: int):
        return self._q1("SELECT id, cycle_id, target_kind, seq, status, baseline_id, variant_id, evaluation_id, "
                        "eval_action FROM build_target WHERE id=?", (build_target_id,))

    def _assert_current_cycle(self, cycle_id: int, *, action: str) -> tuple[int, int]:
        cycle = self._q1(
            "SELECT goal_id,goal_ver,status FROM cycle WHERE id=?", (cycle_id,))
        if cycle is None:
            self._reject(None, f"{action}: cycle c{cycle_id} 不存在")
        current = self._q1("SELECT MAX(version) FROM goal WHERE id=?", (cycle[0],))
        if (current is None or current[0] != cycle[1]
                or cycle[2] in ("done", "failed", "aborted")):
            self._reject(
                cycle_id, f"{action}: cycle c{cycle_id} 非 current writable lineage"
                f"（绑定 {cycle[0]}@v{cycle[1]}，current={current[0] if current else None}，status={cycle[2]}）")
        return int(cycle[0]), int(cycle[1])

    def _assert_current_target(self, cycle_id: int, build_target_id: int, *, action: str) -> None:
        goal_id, goal_ver = self._assert_current_cycle(cycle_id, action=action)
        row = self._q1(
            "SELECT bt.cycle_id,bt.question_id,c.active_question_id,q.goal_id,q.goal_ver,q.status,"
            "bt.target_kind,c.route "
            "FROM build_target bt JOIN cycle c ON c.id=bt.cycle_id "
            "LEFT JOIN question q ON q.id=bt.question_id WHERE bt.id=?",
            (build_target_id,))
        lineage_ok = (row is not None and row[0] == cycle_id and row[1] is not None
                      and (row[3], row[4]) == (goal_id, goal_ver))
        research_owned = bool(
            lineage_ok and row[1] == row[2] and row[5] == "active")
        # import 物化轮不是七类研究轮，不会把被 baseline dependency 挡住的问题伪装成 active。
        # 它以 durable import_worker_cycle marker 精确绑定 question；只允许唯一的 import target
        # 消费该绑定。这样既不放宽普通研究 target，也不让 worker 劫持问题 active lease。
        worker_owned = False
        if (lineage_ok and row[2] is None and row[5] in ("open", "inconclusive")
                and row[6] == "import" and row[7] is None):
            worker_owned = self._q1(
                "SELECT 1 FROM decision WHERE cycle_id=? AND actor='orchestrator' "
                "AND type='import_worker_cycle' AND json_valid(payload_json) "
                "AND json_extract(payload_json,'$.question_id')=? "
                "AND json_type(payload_json,'$.external_import_id')='integer' "
                "ORDER BY id DESC LIMIT 1", (cycle_id, row[1])) is not None
        if not (research_owned or worker_owned):
            self._reject(
                cycle_id, f"{action}: target {build_target_id} 未绑定 cycle 的 exact active current question")

    def gate_start_build_target(self, *, build_target_id: int) -> None:
        """pending → building（build/exec/import，池对象连动 building）/ running（eval，不动池）。
        拒：非 pending；非当前串行目标（= 本 cycle 最小 seq 非终态者；有同 cycle 目标在途亦拒）。
        import 目标（M4 CP5.5 物化 worker）：连动同 build（占位 baseline planned→building + 物化变体）。"""
        bt = self._bt(build_target_id)
        if bt is None:
            self._reject(None, f"build_target 不存在: {build_target_id}", attempted_target=build_target_id)
        _, ci, kind, _seq, status, bl, var, _eid, _ea = bt
        self._assert_current_target(ci, build_target_id, action="start_build_target")
        if status != "pending":
            self._reject(ci, f"build_target {build_target_id} 非 pending（当前 {status}），不可 start")
        inflight = self._q1("SELECT id FROM build_target WHERE cycle_id=? AND status IN ('building','smoke','running')", (ci,))
        if inflight:
            self._reject(ci, f"目标间串行（§4.2.5）：target {inflight[0]} 尚在途，不可并行 start {build_target_id}")
        stopped = self._q1(
            "SELECT id,status FROM build_target WHERE cycle_id=? AND seq<? AND "
            "((critical=1 AND status='failed') OR status='engineering_blocked') "
            "ORDER BY seq LIMIT 1", (ci, _seq))
        if stopped is not None:
            self._reject(
                ci, f"target {stopped[0]}({stopped[1]}) 已触发 bundle 早退；"
                f"后继 target {build_target_id} 只能置 skipped，不得启动")
        cur = self._q1("SELECT id FROM build_target WHERE cycle_id=? AND status NOT IN "
                       "('complete','skipped','failed','engineering_blocked') ORDER BY seq LIMIT 1", (ci,))
        if cur is None or cur[0] != build_target_id:
            self._reject(ci, f"非当前串行目标：应先处理 target {cur[0] if cur else '∅'}（seq 序），拒 start {build_target_id}")
        with self.daemon.transaction() as conn:
            if kind == "eval":
                conn.execute("UPDATE build_target SET status='running' WHERE id=?", (build_target_id,))
                return
            conn.execute("UPDATE build_target SET status='building' WHERE id=?", (build_target_id,))
            # 池对象连动（§4.1.4）：build/import → baseline+变体 building；exec → 仅本 target 新变体 building
            if kind in ("build", "import") and bl is not None:
                conn.execute("UPDATE baseline SET status='building' WHERE id=? AND status='planned'", (bl,))
            if var is not None:
                conn.execute("UPDATE variant SET status='building' WHERE id=? AND status='planned'", (var,))

    def gate_progress_build_target(self, *, build_target_id: int, to: str,
                                   current_subject_hash: Optional[str] = None) -> None:
        """building→smoke→running。启用代码评审时，to='running' 须通过 bundle_code_review
        （subject_hash 当下重算相符 + runner_call(success/audit)，§4.1.4 附注）；关闭时仍不得跳过 smoke。"""
        bt = self._bt(build_target_id)
        if bt is None:
            self._reject(None, f"build_target 不存在: {build_target_id}", attempted_target=build_target_id)
        _, ci, _kind, _seq, status, *_ = bt
        self._assert_current_target(ci, build_target_id, action="progress_build_target")
        legal = {("building", "smoke"), ("smoke", "running")}
        if (status, to) not in legal:
            self._reject(ci, f"build_target 非法迁移 {status}→{to}（只许 building→smoke→running）")
        if to == "running" and self.require_code_review:
            if current_subject_hash is None or not self.review_passed(
                    build_target_id=build_target_id, review_kind="bundle_code_review",
                    current_subject_hash=current_subject_hash):
                self._reject(ci, f"target {build_target_id} 无通过的代码适配评审"
                                 "（需 verdict=pass 且 subject_hash 与当下重算一致 + audit runner_call success）")
        with self.daemon.transaction() as conn:
            conn.execute("UPDATE build_target SET status=? WHERE id=?", (to, build_target_id))

    def gate_finish_build_target(self, *, build_target_id: int, status: str,
                                 failure_kind: Optional[str] = None) -> None:
        """→ complete/skipped/failed/engineering_blocked。complete 须产物已注册（eval 成功；build/exec 另须
        variant legal）；failed 连坐（build→baseline+variant build_failed；exec/import→仅 variant；eval 不动池）。"""
        bt = self._bt(build_target_id)
        if bt is None:
            self._reject(None, f"build_target 不存在: {build_target_id}", attempted_target=build_target_id)
        _, ci, kind, _seq, cur_status, bl, var, eid, eval_action = bt
        if status == "complete":
            self._assert_current_target(ci, build_target_id, action="complete_build_target")
        if status not in _TERMINAL_TARGET:
            self._reject(ci, f"build_target 终态非法: {status}")
        if cur_status in _TERMINAL_TARGET:
            self._reject(ci, f"build_target {build_target_id} 已终态（{cur_status}），不可再 finish")
        if status == "skipped":
            if cur_status != "pending":
                self._reject(ci, f"skipped 只表示从未执行，只许 pending→skipped（当前 {cur_status}）")
            if failure_kind is not None:
                self._reject(ci, "skipped 不得携 failure_kind")
            self._assert_current_target(ci, build_target_id, action="skip_build_target")
            stopped = self._q1(
                "SELECT id,status FROM build_target WHERE cycle_id=? AND seq<? AND "
                "((critical=1 AND status='failed') OR status='engineering_blocked') "
                "ORDER BY seq LIMIT 1", (ci, _seq))
            if stopped is None:
                self._reject(
                    ci, "skipped 仅允许在此前 critical failure / engineering_blocked 触发 bundle 早退后写入")
        if status == "complete":
            if eval_action is not None:   # 承评目标：其 evaluation 须 success
                if eval_action == "create_evaluation":
                    # evaluation.build_target_id 无 UNIQUE（正常纪律 = 一 create 目标一 eval）——判据须**确定**：
                    # 有绑定 且 全部绑定 eval 皆 success（fetchone 任取一条会被多绑腐化态骗过，内审 SHOULD）
                    rows = self.read.execute("SELECT status FROM evaluation WHERE build_target_id=?",
                                             (build_target_id,)).fetchall()
                    bad = [s for (s,) in rows if s != "success"]
                    if not rows or bad:
                        self._reject(ci, f"complete 前置不满足：target {build_target_id} 的 evaluation "
                                         f"{'缺失' if not rows else f'非 success（{bad}）'}")
                else:
                    erow = self._q1("SELECT status FROM evaluation WHERE id=?", (eid,))
                    if erow is None or erow[0] != "success":
                        self._reject(ci, f"complete 前置不满足：target {build_target_id} 的 evaluation "
                                         f"{'缺失' if erow is None else erow[0]}（须 success）")
            if kind in ("build", "exec") and var is not None:
                vrow = self._q1("SELECT status FROM variant WHERE id=?", (var,))
                if vrow is None or vrow[0] != "legal":
                    self._reject(ci, f"complete 前置不满足：variant {var} 未注册 legal（当前 "
                                     f"{vrow[0] if vrow else '缺失'}）——先 gate_register_*")
            if kind == "eval":
                attempt = self._q1(
                    "SELECT 1 FROM evaluation_attempt WHERE build_target_id=? AND status='success' LIMIT 1",
                    (build_target_id,))
                if attempt is None:
                    self._reject(
                        ci, f"complete 前置不满足：eval target {build_target_id} "
                        "无本 target 的 success evaluation_attempt")
        with self.daemon.transaction() as conn:
            conn.execute("UPDATE build_target SET status=?, failure_kind=? WHERE id=?",
                         (status, failure_kind, build_target_id))
            # 连坐（§4.1.4「失败/blocked 时」）：failed **与 engineering_blocked** 都连坐（内审 BLOCKER——
            # 只 failed 会把池对象永久卡在 building）；**仅 build/exec/import 连坐、eval 目标不动池**（codex
            # BLOCKER——eval 目标也带 variant_id，不加 kind 守卫会误伤被评变体）；失败卡/通知 outbox = M5 基建。
            if status in ("failed", "engineering_blocked") and kind in ("build", "exec", "import"):
                if kind in ("build", "import") and bl is not None:
                    conn.execute("UPDATE baseline SET status='build_failed' WHERE id=? AND status IN ('planned','building')", (bl,))
                if var is not None:
                    conn.execute("UPDATE variant SET status='build_failed' WHERE id=? AND status IN ('planned','building')", (var,))
            elif status == "skipped" and kind in ("build", "exec", "import"):
                # plan 已在 bundle 前占坑；未执行旁路必须释放身份键，否则后续轮永远撞 I5。
                if kind in ("build", "import") and bl is not None:
                    conn.execute("UPDATE baseline SET status='abandoned' WHERE id=? AND status='planned'", (bl,))
                if var is not None:
                    conn.execute("UPDATE variant SET status='abandoned' WHERE id=? AND status='planned'", (var,))

    def gate_fail_sandbox_output(self, *, build_target_id: int,
                                 owner_kind: str, owner_id: int,
                                 transcript_ref: str) -> None:
        """Atomically reject unsafe sandbox output and freeze its exact DB owner.

        The guardian has already proved the container drained.  Publishing the
        quarantine is nevertheless part of the execution fact, so a rejected
        output must not leave a run/attempt active.  Owner + evaluation + target
        and pool cascade are one transaction; a kill cannot expose a partially
        classified recovery state.
        """
        if owner_kind not in ("build_target", "run", "evaluation_attempt"):
            raise ValueError("sandbox output owner_kind 非法")
        if (isinstance(owner_id, bool) or not isinstance(owner_id, int)
                or owner_id <= 0):
            raise ValueError("sandbox output owner_id 非法")
        if not isinstance(transcript_ref, str) or not transcript_ref:
            raise ValueError("sandbox output transcript_ref 非法")
        with self.daemon.transaction() as conn:
            bt = conn.execute(
                "SELECT cycle_id,target_kind,status,baseline_id,variant_id,failure_kind "
                "FROM build_target WHERE id=?", (build_target_id,)).fetchone()
            if bt is None:
                raise RuntimeError(f"sandbox output target {build_target_id} 不存在")
            _cycle_id, kind, target_status, baseline_id, variant_id, target_failure = bt

            if owner_kind == "build_target":
                if owner_id != build_target_id:
                    raise RuntimeError("sandbox output receipt 指向另一 build_target")
            elif owner_kind == "run":
                owner = conn.execute(
                    "SELECT status,failure_kind,build_target_id FROM run WHERE id=?",
                    (owner_id,)).fetchone()
                if owner is None or owner[2] != build_target_id:
                    raise RuntimeError("sandbox output receipt 指向不存在/异 target 的 run")
                if owner[0] == "running":
                    conn.execute(
                        "UPDATE run SET status='failed',failure_kind='data_invalid' WHERE id=?",
                        (owner_id,))
                elif tuple(owner[:2]) != ("failed", "data_invalid"):
                    raise RuntimeError(
                        f"sandbox output run {owner_id} 已有矛盾终态 {owner[:2]}")
            else:
                owner = conn.execute(
                    "SELECT ea.status,ea.failure_kind,ea.evaluation_id,ea.build_target_id,e.status "
                    "FROM evaluation_attempt ea JOIN evaluation e ON e.id=ea.evaluation_id "
                    "WHERE ea.id=?", (owner_id,)).fetchone()
                if owner is None or owner[3] != build_target_id:
                    raise RuntimeError(
                        "sandbox output receipt 指向不存在/异 target 的 evaluation_attempt")
                if owner[0] == "running":
                    conn.execute(
                        "UPDATE evaluation_attempt SET status='failed',"
                        "failure_kind='artifact_invalid',transcript_ref=COALESCE(?,transcript_ref),"
                        "completed_cycle=cycle_id WHERE id=?",
                        (transcript_ref, owner_id))
                elif tuple(owner[:2]) != ("failed", "artifact_invalid"):
                    raise RuntimeError(
                        f"sandbox output attempt {owner_id} 已有矛盾终态 {owner[:2]}")
                evaluation_id = owner[2]
                evaluation_status = conn.execute(
                    "SELECT status FROM evaluation WHERE id=?", (evaluation_id,)).fetchone()[0]
                if evaluation_status == "success":
                    raise RuntimeError(
                        f"sandbox output evaluation {evaluation_id} 已 success，事实矛盾")
                active = conn.execute(
                    "SELECT 1 FROM evaluation_attempt WHERE evaluation_id=? "
                    "AND status IN ('created','running') LIMIT 1", (evaluation_id,)).fetchone()
                successful = conn.execute(
                    "SELECT 1 FROM evaluation_attempt WHERE evaluation_id=? "
                    "AND status='success' LIMIT 1", (evaluation_id,)).fetchone()
                if active is not None or successful is not None:
                    raise RuntimeError(
                        f"sandbox output evaluation {evaluation_id} 仍有其他 active/success attempt")
                conn.execute(
                    "UPDATE evaluation SET status='failed' WHERE id=?", (evaluation_id,))

            if target_status not in _TERMINAL_TARGET:
                conn.execute(
                    "UPDATE build_target SET status='failed',failure_kind='artifact_invalid' "
                    "WHERE id=?", (build_target_id,))
                if kind in ("build", "exec", "import"):
                    if kind in ("build", "import") and baseline_id is not None:
                        conn.execute(
                            "UPDATE baseline SET status='build_failed' WHERE id=? "
                            "AND status IN ('planned','building')", (baseline_id,))
                    if variant_id is not None:
                        conn.execute(
                            "UPDATE variant SET status='build_failed' WHERE id=? "
                            "AND status IN ('planned','building')", (variant_id,))
            elif (target_status, target_failure) != ("failed", "artifact_invalid"):
                raise RuntimeError(
                    f"sandbox output target {build_target_id} 已有矛盾终态 "
                    f"{(target_status, target_failure)}")

    def gate_skip_remaining_targets(self, *, failed_target_id: int) -> List[int]:
        """在 critical failure / engineering_blocked 后原子跳过所有尚未执行的后继目标。

        返回本次或此前已收敛为 skipped 的后继 id，供编排器幂等补 phase_commit。池占坑与 target
        同事务转 abandoned；任何后继已经开工都表示串行/恢复不变量损坏，必须 fail loud。
        """
        failed = self._bt(failed_target_id)
        if failed is None:
            self._reject(None, f"build_target 不存在: {failed_target_id}")
        _, ci, _kind, seq, status, *_ = failed
        self._assert_current_target(ci, failed_target_id, action="skip_remaining_targets")
        critical = self._q1("SELECT critical FROM build_target WHERE id=?", (failed_target_id,))[0]
        if not (status == "engineering_blocked" or (status == "failed" and critical == 1)):
            self._reject(
                ci, f"target {failed_target_id} 未触发早退（status={status}, critical={critical}）")
        rows = self.read.execute(
            "SELECT id,target_kind,status,baseline_id,variant_id FROM build_target "
            "WHERE cycle_id=? AND seq>? ORDER BY seq", (ci, seq)).fetchall()
        bad = [(row[0], row[2]) for row in rows if row[2] not in ("pending", "skipped")]
        if bad:
            self._reject(ci, f"早退后继目标已有执行/其他终态，拒绝掩盖串行损坏: {bad}")
        with self.daemon.transaction() as conn:
            for target_id, kind, target_status, baseline_id, variant_id in rows:
                if target_status == "skipped":
                    continue
                conn.execute(
                    "UPDATE build_target SET status='skipped',failure_kind=NULL WHERE id=? AND status='pending'",
                    (target_id,))
                if kind in ("build", "import") and baseline_id is not None:
                    conn.execute(
                        "UPDATE baseline SET status='abandoned' WHERE id=? AND status='planned'",
                        (baseline_id,))
                if kind in ("build", "exec", "import") and variant_id is not None:
                    conn.execute(
                        "UPDATE variant SET status='abandoned' WHERE id=? AND status='planned'",
                        (variant_id,))
            if conn.execute(
                    "SELECT 1 FROM decision WHERE actor='orchestrator' "
                    "AND type='bundle_critical_early_exit' "
                    "AND json_extract(payload_json,'$.failed_target_id')=? LIMIT 1",
                    (failed_target_id,)).fetchone() is None:
                conn.execute(
                    "INSERT INTO decision(cycle_id,actor,type,payload_json) "
                    "VALUES (?,'orchestrator','bundle_critical_early_exit',?)",
                    (ci, json.dumps({
                        "failed_target_id": failed_target_id,
                        "failure_status": status,
                        "skipped_target_ids": [row[0] for row in rows],
                    }, ensure_ascii=False, sort_keys=True)))
        return [row[0] for row in rows]

    # -- run --------------------------------------------------------------------
    def gate_start_run(self, *, build_target_id: int, cycle_id: str, variant_id: int, kind: str,
                       seed: Optional[int] = None, env_hash: Optional[str] = None,
                       commit_hash: Optional[str] = None) -> int:
        """开训即落 running（§4.2.5(i) 执行事实短事务）。拒：target 非 running（训练在代码评审过后）；
        variant 态非法（须 building——池连动后）；kind/cycle/variant 与 target 不符（触发器兜底，此处前置干净拒）。"""
        ci = _cnum(cycle_id)
        bt = self._bt(build_target_id)
        if bt is None:
            self._reject(ci, f"build_target 不存在: {build_target_id}", attempted_target=build_target_id)
        _, bt_ci, bt_kind, _seq, bt_status, _bl, bt_var, _eid, _ea = bt
        self._assert_current_target(ci, build_target_id, action="start_run")
        if bt_status != "running":
            self._reject(ci, f"target {build_target_id} 非 running（当前 {bt_status}）——训练须在代码评审通过后")
        if bt_kind != kind or bt_ci != ci or bt_var != variant_id:
            self._reject(ci, f"run 的 kind/cycle/variant（{kind}/c{ci}/v{variant_id}）与 target"
                             f"（{bt_kind}/c{bt_ci}/v{bt_var}）不一致")
        vrow = self._q1("SELECT status FROM variant WHERE id=?", (variant_id,))
        if vrow is None or vrow[0] != "building":
            self._reject(ci, f"variant {variant_id} 态非法（{vrow[0] if vrow else '缺失'}，须 building）")
        with self.daemon.transaction() as conn:
            return conn.execute(
                "INSERT INTO run(cycle_id,variant_id,build_target_id,kind,seed,status,commit_hash,env_hash) "
                "VALUES (?,?,?,?,?,'running',?,?)",
                (ci, variant_id, build_target_id, kind, seed, commit_hash, env_hash)).lastrowid

    def gate_finish_run(self, *, run_id: int, status: str, failure_kind: Optional[str] = None,
                        cost: Optional[float] = None) -> None:
        """run → success/failed（冻结）。拒：非 running；success 无 checkpoint（produced_by_run=本 run）。"""
        row = self._q1("SELECT status, cycle_id FROM run WHERE id=?", (run_id,))
        if row is None:
            self._reject(None, f"run 不存在: {run_id}", attempted_run=run_id)
        cur, ci = row
        if cur != "running":
            self._reject(ci, f"run {run_id} 非 running（当前 {cur}），不可 finish")
        if status not in ("success", "failed"):
            self._reject(ci, f"run 终态非法: {status}")
        if status == "success" and self._q1("SELECT 1 FROM checkpoint WHERE produced_by_run=?", (run_id,)) is None:
            self._reject(ci, f"run {run_id} success 须已登记 checkpoint（produced_by_run 回指）")
        if status == "failed" and failure_kind is None:
            self._reject(ci, "run failed 须给 failure_kind")
        with self.daemon.transaction() as conn:
            conn.execute("UPDATE run SET status=?, failure_kind=?, cost=COALESCE(?,cost) WHERE id=?",
                         (status, failure_kind, cost, run_id))

    # -- evaluation / attempt -----------------------------------------------------
    def gate_start_attempt(self, *, cycle_id: str, purpose: str, build_target_id: Optional[int] = None,
                           evaluation_id: Optional[int] = None, create: Optional[Dict[str, Any]] = None,
                           env_hash: Optional[str] = None, commit_hash: Optional[str] = None,
                           retry_of: Optional[int] = None, transcript_ref: Optional[str] = None,
                           watchdog_sec: Optional[float] = None) -> Dict[str, int]:
        """create 模式（create={variant_id,protocol_id,protocol_ver,eval_key,source,target_set_hash}）→
        新 evaluation(created→running) + attempt#1；append 模式（evaluation_id）→ 既有 evaluation 追加 attempt。
        拒：两模式参数不恰一；purpose 非法；create 撞既有格子（I6 一格子一 eval）/缺 target_set_hash；
        append 的 evaluation 缺失/abandoned。返回 {evaluation_id, attempt_id, attempt_no}。"""
        ci = _cnum(cycle_id)
        if (create is None) == (evaluation_id is None):
            self._reject(ci, "gate_start_attempt 须恰一模式：create=… 或 evaluation_id=…")
        if purpose not in _ATTEMPT_PURPOSES:
            self._reject(ci, f"attempt purpose 非法: {purpose}")
        if watchdog_sec is not None and (
                isinstance(watchdog_sec, bool) or not isinstance(watchdog_sec, (int, float))
                or not math.isfinite(float(watchdog_sec)) or float(watchdog_sec) <= 0):
            self._reject(ci, "attempt watchdog_sec 须为正有限数")
        if purpose == "retry":
            if retry_of is None or evaluation_id is None or create is not None:
                self._reject(ci, "purpose=retry 须 append 同 evaluation 并显式给 retry_of")
        elif retry_of is not None:
            self._reject(ci, "非 retry attempt 不得携 retry_of")
        self._assert_current_cycle(ci, action="start_attempt")
        if build_target_id is not None:
            self._assert_current_target(ci, build_target_id, action="start_attempt")
            target = self._bt(build_target_id)
            if target is None or target[4] != "running":
                self._reject(
                    ci, f"start_attempt 要求 target {build_target_id} 已 running"
                    f"（当前 {target[4] if target else 'missing'}）")
        if create is not None:
            missing = [k for k in ("variant_id", "protocol_id", "protocol_ver", "eval_key", "source", "target_set_hash")
                       if create.get(k) in (None, "")]   # 判缺失/空串（这些字段无合法 0/False 值）
            if missing:
                self._reject(ci, f"create_evaluation 缺必填: {missing}（I6：target_set_hash 身份冻结必填）")
            hit = self._q1("SELECT id FROM evaluation WHERE variant_id=? AND protocol_id=? AND protocol_ver=?",
                           (create["variant_id"], create["protocol_id"], create["protocol_ver"]))
            if hit:
                self._reject(ci, f"一格子一 evaluation（I6）：(v{create['variant_id']},p{create['protocol_id']}"
                                 f"@{create['protocol_ver']}) 已有 evaluation {hit[0]}——走 append_attempt 或复用")
            if self._q1("SELECT 1 FROM evaluation WHERE variant_id=? AND eval_key=?",
                        (create["variant_id"], create["eval_key"])):
                self._reject(ci, f"eval_key 撞路径：variant {create['variant_id']} 已有 eval_key={create['eval_key']!r}")
            with self.daemon.transaction() as conn:
                eid = conn.execute(
                    "INSERT INTO evaluation(variant_id,protocol_id,protocol_ver,eval_key,source,status,"
                    "created_cycle,build_target_id,target_set_hash) VALUES (?,?,?,?,?,'created',?,?,?)",
                    (create["variant_id"], create["protocol_id"], create["protocol_ver"], create["eval_key"],
                     create["source"], ci, build_target_id, create["target_set_hash"])).lastrowid
                aid = conn.execute(
                    "INSERT INTO evaluation_attempt(evaluation_id,cycle_id,build_target_id,attempt_no,purpose,"
                    "status,env_hash,commit_hash,transcript_ref,watchdog_sec,started_cycle) "
                    "VALUES (?,?,?,1,?,'running',?,?,?,?,?)",
                    (eid, ci, build_target_id, purpose, env_hash, commit_hash,
                     transcript_ref, watchdog_sec, ci)).lastrowid
                conn.execute("UPDATE evaluation SET status='running' WHERE id=?", (eid,))
            return {"evaluation_id": eid, "attempt_id": aid, "attempt_no": 1}
        erow = self._q1("SELECT status FROM evaluation WHERE id=?", (evaluation_id,))
        if erow is None:
            self._reject(ci, f"append_attempt 的 evaluation 不存在: {evaluation_id}")
        if erow[0] == "abandoned":
            self._reject(ci, f"evaluation {evaluation_id} 已 abandoned，不可追加 attempt")
        if retry_of is not None:
            previous = self._q1(
                "SELECT evaluation_id,status FROM evaluation_attempt WHERE id=?", (retry_of,))
            if (previous is None or previous[0] != evaluation_id
                    or previous[1] not in ("failed", "aborted")):
                self._reject(
                    ci, "retry_of 须指向同 evaluation 的 failed/aborted attempt")
        with self.daemon.transaction() as conn:
            n = conn.execute("SELECT COALESCE(MAX(attempt_no),0)+1 FROM evaluation_attempt WHERE evaluation_id=?",
                             (evaluation_id,)).fetchone()[0]
            aid = conn.execute(
                "INSERT INTO evaluation_attempt(evaluation_id,cycle_id,build_target_id,attempt_no,purpose,"
                "status,env_hash,commit_hash,retry_of,transcript_ref,watchdog_sec,started_cycle) "
                "VALUES (?,?,?,?,?,'running',?,?,?,?,?,?)",
                (evaluation_id, ci, build_target_id, n, purpose, env_hash, commit_hash,
                 retry_of, transcript_ref, watchdog_sec, ci)).lastrowid
            if erow[0] in ("created", "failed"):   # 重试显式 failed→running（§4.1.4；success 保持不回退）
                conn.execute("UPDATE evaluation SET status='running' WHERE id=?", (evaluation_id,))
        return {"evaluation_id": evaluation_id, "attempt_id": aid, "attempt_no": n}

    def gate_finish_attempt(self, *, attempt_id: int, status: str, failure_kind: Optional[str] = None,
                            metric_results: Optional[List[Dict[str, Any]]] = None,
                            cost: Optional[float] = None,
                            transcript_ref: Optional[str] = None,
                            artifact_ref: Optional[str] = None) -> None:
        """attempt → success/failed/aborted。success：单事务写 metric_result[]（I2 前置核 + required 逐项核
        aggregate 覆盖）+ attempt success + evaluation success/canonical（首个成功 attempt 封 canonical）。"""
        row = self._q1("SELECT ea.status, ea.evaluation_id, ea.build_target_id, ea.cycle_id, e.protocol_id, "
                       "e.protocol_ver, e.status FROM evaluation_attempt ea JOIN evaluation e ON e.id=ea.evaluation_id "
                       "WHERE ea.id=?", (attempt_id,))
        if row is None:
            self._reject(None, f"attempt 不存在: {attempt_id}", attempted_attempt=attempt_id)
        cur, eid, bt_id, ci, pid, pver, estatus = row
        if cur != "running":
            self._reject(ci, f"attempt {attempt_id} 非 running（当前 {cur}），不可 finish")
        if status not in ("success", "failed", "aborted"):
            self._reject(ci, f"attempt 终态非法: {status}")
        if status == "success":
            self._assert_current_cycle(ci, action="finish_attempt_success")
            if bt_id is not None:
                self._assert_current_target(ci, bt_id, action="finish_attempt_success")
        if status == "failed" and failure_kind is None:
            self._reject(ci, "attempt failed 须给 failure_kind")
        if status != "success":
            with self.daemon.transaction() as conn:
                conn.execute(
                    "UPDATE evaluation_attempt SET status=?,failure_kind=?,cost=COALESCE(?,cost),"
                    "transcript_ref=COALESCE(?,transcript_ref),artifact_ref=COALESCE(?,artifact_ref),"
                    "completed_cycle=? WHERE id=?",
                    (status, failure_kind, cost, transcript_ref, artifact_ref, ci, attempt_id))
            return
        mrs = metric_results or []
        for m in mrs:   # I2 前置：metric ∈ 本协议声明（触发器兜底，此处干净拒）
            if self._q1("SELECT 1 FROM protocol_metric WHERE protocol_id=? AND protocol_ver=? AND metric_id=? AND metric_ver=?",
                        (pid, pver, m["metric_id"], m["metric_ver"])) is None:
                self._reject(ci, f"I2：metric ({m['metric_id']}@{m['metric_ver']}) 不在协议 p{pid}@{pver} 声明集")
            scope = m.get("scope", "aggregate")   # DDL CHECK 前置干净拒：fold⇔带 checkpoint、aggregate⇔不带
            if (scope == "aggregate") != (m.get("checkpoint_id") is None):
                self._reject(ci, f"metric_result scope/checkpoint 配对非法：scope={scope} "
                                 f"checkpoint_id={m.get('checkpoint_id')}（fold 须带、aggregate 须空）")
        if bt_id is not None:   # required 覆盖：本 attempt 的 aggregate 结果须逐项覆盖 target 声明（§4.1.4）
            required = self.read.execute(
                "SELECT metric_id, metric_ver FROM build_target_required_metric WHERE build_target_id=?", (bt_id,)).fetchall()
            got = {(m["metric_id"], m["metric_ver"]) for m in mrs if m.get("scope", "aggregate") == "aggregate"}
            missing = [rm for rm in required if tuple(rm) not in got]
            if missing:
                self._reject(ci, f"required metric 未覆盖（aggregate）: {missing}")
        try:
            with self.daemon.transaction() as conn:
                conn.execute("UPDATE evaluation_attempt SET status='success', cost=COALESCE(?,cost), completed_cycle=? "
                             ",transcript_ref=COALESCE(?,transcript_ref),"
                             "artifact_ref=COALESCE(?,artifact_ref) WHERE id=?",
                             (cost, ci, transcript_ref, artifact_ref, attempt_id))
                for m in mrs:
                    conn.execute("INSERT INTO metric_result(evaluation_id,evaluation_attempt_id,metric_id,metric_ver,"
                                 "value,scope,checkpoint_id) VALUES (?,?,?,?,?,?,?)",
                                 (eid, attempt_id, m["metric_id"], m["metric_ver"], m["value"],
                                  m.get("scope", "aggregate"), m.get("checkpoint_id")))
                if estatus != "success":   # 首个成功 attempt 封 canonical；已 success 的（metric_append/repro）保留原 canonical
                    conn.execute("UPDATE evaluation SET status='success', canonical_attempt_id=? WHERE id=?",
                                 (attempt_id, eid))
        except sqlite3.IntegrityError as e:
            # 触发器/CHECK 兜底层拦下未被前置覆盖的畸形载荷（如 checkpoint 跨 variant）——事务已整体回滚，
            # 转干净拒 + 审计（对齐 gate_close_question 的「干净拒」契约，内审 SHOULD）
            self._reject(ci, f"metric_result 写入被 DB 约束拒绝：{e}")

    def gate_finish_evaluation(self, *, evaluation_id: int) -> None:
        """无成功 attempt 且无在途 attempt → evaluation failed（后续可再 append 重试）。拒：仍有在途；已 success。"""
        erow = self._q1("SELECT status, created_cycle FROM evaluation WHERE id=?", (evaluation_id,))
        if erow is None:
            self._reject(None, f"evaluation 不存在: {evaluation_id}", attempted_evaluation=evaluation_id)
        eci = erow[1]
        if erow[0] == "success":
            self._reject(eci, f"evaluation {evaluation_id} 已 success，不可置 failed")
        if self._q1("SELECT 1 FROM evaluation_attempt WHERE evaluation_id=? AND status='success'", (evaluation_id,)):
            # 判据字面=「无成功 attempt」——不只看 eval.status（腐化/迁移态下 status 可能未同步 success attempt，codex SHOULD）
            self._reject(eci, f"evaluation {evaluation_id} 已有 success attempt（status 未同步？腐化态），不可置 failed")
        if self._q1("SELECT 1 FROM evaluation_attempt WHERE evaluation_id=? AND status IN ('created','running')",
                    (evaluation_id,)):
            self._reject(eci, f"evaluation {evaluation_id} 仍有在途 attempt，先 finish 它")
        with self.daemon.transaction() as conn:
            conn.execute("UPDATE evaluation SET status='failed' WHERE id=?", (evaluation_id,))

    def gate_abandon_evaluation(self, *, evaluation_id: int, reason_md: str = "") -> None:
        """→ abandoned + DECISION（审计）。被 valid evidence 引用 / 已发布变体的出厂 eval → 拒（触发器兜底，前置干净拒）。"""
        erow = self._q1("SELECT status, variant_id, source, created_cycle FROM evaluation WHERE id=?", (evaluation_id,))
        if erow is None:
            self._reject(None, f"evaluation 不存在: {evaluation_id}", attempted_evaluation=evaluation_id)
        status, var, source, eci = erow
        if self._q1("SELECT 1 FROM evidence WHERE evaluation_id=? AND kind='evaluation' AND valid=1", (evaluation_id,)):
            self._reject(eci, f"evaluation {evaluation_id} 已被 valid evidence 引用，不可弃用")
        if source == "factory" and status == "success" and \
           self._q1("SELECT 1 FROM variant WHERE id=? AND status='legal'", (var,)):
            self._reject(eci, f"已发布(legal)变体 {var} 的出厂 evaluation 不可弃用（须先 deprecate 变体）")
        with self.daemon.transaction() as conn:
            conn.execute("UPDATE evaluation SET status='abandoned', canonical_attempt_id=NULL WHERE id=?",
                         (evaluation_id,))
            conn.execute("INSERT INTO decision(actor,type,payload_json) VALUES ('gate','abandon_evaluation',?)",
                         (json.dumps({"evaluation_id": evaluation_id, "reason": reason_md}, ensure_ascii=False),))
