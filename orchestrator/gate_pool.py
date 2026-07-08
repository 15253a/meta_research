"""PoolGate —— 注册/评审侧业务门禁（§4.1.4 池注册 gate 家族·注册侧；M4 CP5.2）。

继承 ExecGate（同一 §4.1.4 家族：共享 _reject 审计、受限读连接、review_passed 双评审机械判据）。
覆盖：gate_claim_baseline / gate_register_baseline / gate_register_evaluation / gate_claim_variant /
gate_register_variant / gate_new_protocol。

**「入池」语义**：冻结 DDL 无独立池表——baseline/variant `status='legal'` 即入池（§4.1「复制入池」的
非剪切语义体现在 legal 状态 + 卡片/索引侧写，卡片物化 = 编译器/召回已读 legal 池）。
**§4.2.5(ii) 单事务**：gate_register_evaluation 把 evaluation+attempt(success)+metric_result 一次事务写入
（成功 attempt 在此之前**不存在**——执行期只有 staging；失败 attempt 才走 gate_start/finish_attempt 入账）。
register_baseline/variant 是其后的池迁移短事务；CP5.4 attack advance 以**可恢复短事务序列 + 结构续跑**组合
注册段（每步幂等或可从状态跳过）；整段合一事务（需 WriteDaemon 可嵌套/组合式 gate）= M5/M6 硬化项。

**smoke 判据注**：gate 不可读 execution_log（authorizer 拒）——「smoke 未过」由 build_target 已推进到
'running'（经 smoke 阶段 + 代码评审）结构性保证，本模块据此判。
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from .gate_exec import _ATTEMPT_PURPOSES, ExecGate
from .ids import cnum as _cnum


class PoolGate(ExecGate):

    def _require_keys(self, ci, item: Dict[str, Any], keys: Tuple[str, ...], what: str) -> None:
        """输入结构完整性 → 干净拒（缺键不许裸 KeyError，契约同 _reject 审计，codex SHOULD）。"""
        missing = [k for k in keys if k not in item]
        if missing:
            self._reject(ci, f"{what} 缺键 {missing}: {item!r}")

    # -- claim ------------------------------------------------------------------
    def gate_claim_baseline(self, *, canonical_key: str, slug: str, cycle_id: str,
                            identity_draft_md: str, parent_id: Optional[int] = None,
                            initial_variant_key: str = "base",
                            config_json: str = "{}") -> Dict[str, int]:
        """占位声明：baseline(planned) + 初始 claim variant(planned)。拒：canonical_key 已占（I5）；identity 草稿空。"""
        ci = _cnum(cycle_id)
        if not identity_draft_md or not identity_draft_md.strip():
            self._reject(ci, "claim_baseline identity 草稿为空（模板缺字段）")
        if self._q1("SELECT id FROM baseline WHERE canonical_key=?", (canonical_key,)):
            self._reject(ci, f"canonical_key 已占（I5）: {canonical_key!r}")
        if parent_id is not None and self._q1("SELECT 1 FROM baseline WHERE id=?", (parent_id,)) is None:
            self._reject(ci, f"parent baseline 不存在: {parent_id}")
        try:
            with self.daemon.transaction() as conn:
                bid = conn.execute(
                    "INSERT INTO baseline(slug,canonical_key,parent_id,identity_doc,born_cycle,status) "
                    "VALUES (?,?,?,?,?,'planned')", (slug, canonical_key, parent_id, identity_draft_md, ci)).lastrowid
                vid = conn.execute(
                    "INSERT INTO variant(baseline_id,variant_key,config_json,status) VALUES (?,?,?,'planned')",
                    (bid, initial_variant_key, config_json)).lastrowid
        except sqlite3.IntegrityError as e:   # 未被前置覆盖的约束（干净拒契约统一，内审 SHOULD）
            self._reject(ci, f"claim_baseline 写入被 DB 约束拒绝：{e}")
        return {"baseline_id": bid, "variant_id": vid}

    def gate_claim_variant(self, *, baseline_id: int, variant_key: str, config_json: str,
                           cycle_id: str, seq: int, question_id: Optional[int] = None) -> Dict[str, int]:
        """exec 声明：legal baseline 下新 variant(planned) + build_target(exec, pending)。
        拒：baseline 非 legal；variant_key 已占；config 空。"""
        ci = _cnum(cycle_id)
        brow = self._q1("SELECT status FROM baseline WHERE id=?", (baseline_id,))
        if brow is None or brow[0] != "legal":
            self._reject(ci, f"claim_variant 须 legal baseline（{baseline_id} 当前 {brow[0] if brow else '缺失'}）")
        if self._q1("SELECT 1 FROM variant WHERE baseline_id=? AND variant_key=?", (baseline_id, variant_key)):
            self._reject(ci, f"variant_key 已占: {variant_key!r}（baseline {baseline_id}）")
        if not config_json or not config_json.strip() or config_json.strip() == "{}":
            self._reject(ci, "claim_variant config_json 空（变体须有配置增量）")
        try:
            with self.daemon.transaction() as conn:
                vid = conn.execute("INSERT INTO variant(baseline_id,variant_key,config_json,status) "
                                   "VALUES (?,?,?,'planned')", (baseline_id, variant_key, config_json)).lastrowid
                bt = conn.execute(
                    "INSERT INTO build_target(cycle_id,question_id,target_kind,seq,status,baseline_id,variant_id) "
                    "VALUES (?,?,'exec',?,'pending',?,?)", (ci, question_id, seq, baseline_id, vid)).lastrowid
        except sqlite3.IntegrityError as e:   # 如 UNIQUE(cycle,seq) 撞（seq 由编排器排位，撞=调用方 bug→干净拒）
            self._reject(ci, f"claim_variant 写入被 DB 约束拒绝：{e}")
        return {"variant_id": vid, "build_target_id": bt}

    # -- register（评审闸后的注册段）---------------------------------------------
    def gate_register_evaluation(self, *, cycle_id: str, build_target_id: int, purpose: str,
                                 current_subject_hash: str, metric_results: List[Dict[str, Any]],
                                 evaluation_id: Optional[int] = None, create: Optional[Dict[str, Any]] = None,
                                 env_hash: Optional[str] = None, commit_hash: Optional[str] = None,
                                 cost: float = 0.0, artifact_ref: Optional[str] = None) -> Dict[str, int]:
        """§4.2.5(ii) 测量注册入口（**单事务**）：结果评审通过后，一次写 evaluation(create)/复用(append) +
        attempt(**success**) + metric_result[] + eval success/canonical。成功 attempt 此前不存在（执行期只有
        staging）。拒：无通过结果评审；create 撞格子/缺 target_set_hash；append 无既有；I2；ckpt 跨 variant；
        required 未覆盖；target 绑定不符。"""
        ci = _cnum(cycle_id)
        if (create is None) == (evaluation_id is None):
            self._reject(ci, "gate_register_evaluation 须恰一模式：create=… 或 evaluation_id=…")
        if purpose not in _ATTEMPT_PURPOSES:
            self._reject(ci, f"attempt purpose 非法: {purpose}")
        if not self.review_passed(build_target_id=build_target_id, review_kind="bundle_result_review",
                                  current_subject_hash=current_subject_hash):
            self._reject(ci, f"target {build_target_id} 无通过的结果评审"
                             "（需 verdict=pass 且 subject_hash 与当下重算一致 + audit runner_call success）")
        bt = self._bt(build_target_id)
        if bt is None:
            self._reject(ci, f"build_target 不存在: {build_target_id}")
        if create is not None:
            missing = [k for k in ("variant_id", "protocol_id", "protocol_ver", "eval_key", "source", "target_set_hash")
                       if create.get(k) in (None, "")]
            if missing:
                self._reject(ci, f"register_evaluation create 缺必填: {missing}（I6：target_set_hash）")
            if self._q1("SELECT id FROM evaluation WHERE variant_id=? AND protocol_id=? AND protocol_ver=?",
                        (create["variant_id"], create["protocol_id"], create["protocol_ver"])):
                self._reject(ci, "一格子一 evaluation（I6）：格子已有 evaluation")
            var_id, pid, pver = create["variant_id"], create["protocol_id"], create["protocol_ver"]
        else:
            var_id = pid = pver = None   # append：从既有 evaluation 读（下）
        if bt[2] == "import":
            raise NotImplementedError("import 目标的测量注册随物化设计 = CP5.5（OPEN #6）")
        if create is not None and bt[6] != var_id:
            # target↔variant 绑定（codex BLOCKER×2）：不许拿 variant A 的评审/target 注册 variant B 的测量；
            # **NULL 不作通配**——未绑 variant 的 build/exec/eval 目标是非法态，同样拒（第2轮 BLOCKER）。
            self._reject(ci, f"target 绑定不符：target {build_target_id} 绑 variant {bt[6]}（NULL=未绑，非法），"
                             f"注册的是 variant {var_id}")
        if create is None:
            erow = self._q1("SELECT variant_id, protocol_id, protocol_ver, status, build_target_id "
                            "FROM evaluation WHERE id=?", (evaluation_id,))
            if erow is None:
                self._reject(ci, f"append 的 evaluation 不存在: {evaluation_id}")
            var_id, pid, pver = erow[0], erow[1], erow[2]
            if erow[3] == "abandoned":
                self._reject(ci, f"evaluation {evaluation_id} 已 abandoned")
            if erow[4] is not None and erow[4] != build_target_id:
                self._reject(ci, f"target 绑定不符：evaluation 绑 target {erow[4]}，本次 {build_target_id}")
            if bt[6] != var_id:   # append 侧同样核 target↔variant（NULL 不作通配，codex BLOCKER×2）
                self._reject(ci, f"target 绑定不符：target {build_target_id} 绑 variant {bt[6]}（NULL=未绑，非法），"
                                 f"evaluation 属 {var_id}")
        for m in metric_results:   # I2 + scope/checkpoint 配对 + ckpt 跨 variant 前置干净拒
            self._require_keys(ci, m, ("metric_id", "metric_ver", "value"), "metric_result")
            if self._q1("SELECT 1 FROM protocol_metric WHERE protocol_id=? AND protocol_ver=? AND metric_id=? AND metric_ver=?",
                        (pid, pver, m["metric_id"], m["metric_ver"])) is None:
                self._reject(ci, f"I2：metric ({m['metric_id']}@{m['metric_ver']}) 不在协议 p{pid}@{pver}")
            scope = m.get("scope", "aggregate")
            if (scope == "aggregate") != (m.get("checkpoint_id") is None):
                self._reject(ci, f"metric scope/checkpoint 配对非法：{scope}/{m.get('checkpoint_id')}")
            if m.get("checkpoint_id") is not None:
                ck = self._q1("SELECT variant_id FROM checkpoint WHERE id=?", (m["checkpoint_id"],))
                if ck is None or ck[0] != var_id:
                    self._reject(ci, f"checkpoint {m.get('checkpoint_id')} 跨 variant（评估 v{var_id}）")
        required = self.read.execute("SELECT metric_id, metric_ver FROM build_target_required_metric "
                                     "WHERE build_target_id=?", (build_target_id,)).fetchall()
        got = {(m["metric_id"], m["metric_ver"]) for m in metric_results if m.get("scope", "aggregate") == "aggregate"}
        missing_req = [rm for rm in required if tuple(rm) not in got]
        if missing_req:
            self._reject(ci, f"required metric 未覆盖（aggregate）: {missing_req}")
        try:
            with self.daemon.transaction() as conn:   # ——§4.2.5(ii) 单事务——
                if create is not None:
                    eid = conn.execute(
                        "INSERT INTO evaluation(variant_id,protocol_id,protocol_ver,eval_key,source,status,"
                        "created_cycle,build_target_id,target_set_hash) VALUES (?,?,?,?,?,'created',?,?,?)",
                        (var_id, pid, pver, create["eval_key"], create["source"], ci,
                         build_target_id, create["target_set_hash"])).lastrowid
                else:
                    eid = evaluation_id
                n = conn.execute("SELECT COALESCE(MAX(attempt_no),0)+1 FROM evaluation_attempt WHERE evaluation_id=?",
                                 (eid,)).fetchone()[0]
                aid = conn.execute(
                    "INSERT INTO evaluation_attempt(evaluation_id,cycle_id,build_target_id,attempt_no,purpose,"
                    "status,env_hash,commit_hash,started_cycle,completed_cycle,cost,artifact_ref) "
                    "VALUES (?,?,?,?,?,'success',?,?,?,?,?,?)",
                    (eid, ci, build_target_id, n, purpose, env_hash, commit_hash, ci, ci, cost, artifact_ref)).lastrowid
                for m in metric_results:
                    conn.execute("INSERT INTO metric_result(evaluation_id,evaluation_attempt_id,metric_id,"
                                 "metric_ver,value,scope,checkpoint_id) VALUES (?,?,?,?,?,?,?)",
                                 (eid, aid, m["metric_id"], m["metric_ver"], m["value"],
                                  m.get("scope", "aggregate"), m.get("checkpoint_id")))
                cur = conn.execute("SELECT status FROM evaluation WHERE id=?", (eid,)).fetchone()[0]
                if cur != "success":   # 首成功封 canonical；append 到已 success 的保留原 canonical
                    conn.execute("UPDATE evaluation SET status='success', canonical_attempt_id=? WHERE id=?",
                                 (aid, eid))
        except sqlite3.IntegrityError as e:
            self._reject(ci, f"register_evaluation 写入被 DB 约束拒绝：{e}")
        return {"evaluation_id": eid, "attempt_id": aid}

    def _register_common_checks(self, ci, *, variant_id: int, build_target_id: int,
                                run_id: Optional[int], evaluation_id: int,
                                current_subject_hash: str, expect_kind: str) -> None:
        """register_baseline/variant 共用判据：target kind/variant 绑定 + run success（origin=run_produced 时）+
        checkpoint 回指 + factory evaluation success/canonical + required 全产 + smoke 结构性已过 + 结果评审。
        expect_kind：register_baseline 须 'build' 目标、register_variant 须 'exec'（codex BLOCKER——否则可拿
        variant A 的评审/target 把 variant B 入池）。"""
        bt0 = self._bt(build_target_id)
        if bt0 is None:
            self._reject(ci, f"register：build_target {build_target_id} 不存在")
        if bt0[2] != expect_kind:
            self._reject(ci, f"register：target {build_target_id} kind={bt0[2]}，须 {expect_kind}")
        if bt0[6] != variant_id:   # NULL 不作通配（未绑=非法态，codex 第2轮 BLOCKER）
            self._reject(ci, f"register：target {build_target_id} 绑 variant {bt0[6]}（NULL=未绑，非法），"
                             f"注册的是 {variant_id}")
        if run_id is None and self._q1(
                "SELECT 1 FROM checkpoint WHERE variant_id=? AND origin='run_produced'", (variant_id,)):
            # 防御（内审 SHOULD）：run(success)? 的「?」仅豁免非 run_produced 来源（external/none）——
            # 该 variant 已有 run_produced checkpoint 却不给 run_id = 跳过 run success 核验，拒。
            self._reject(ci, f"register：variant {variant_id} 有 run_produced checkpoint，须给 run_id 核 run success")
        if run_id is not None:
            rrow = self._q1("SELECT status, variant_id FROM run WHERE id=?", (run_id,))
            if rrow is None or rrow[0] != "success":
                self._reject(ci, f"register：run {run_id} 非 success（{rrow[0] if rrow else '缺失'}）")
            if rrow[1] != variant_id:
                self._reject(ci, f"register：run {run_id} 属 variant {rrow[1]}，非 {variant_id}")
            if self._q1("SELECT 1 FROM checkpoint WHERE produced_by_run=? AND variant_id=?", (run_id, variant_id)) is None:
                self._reject(ci, f"register：run {run_id} 无回指 checkpoint（content_hash 由 DDL NOT NULL 保证）")
        erow = self._q1("SELECT variant_id, source, status, canonical_attempt_id, build_target_id "
                        "FROM evaluation WHERE id=?", (evaluation_id,))
        if erow is None:
            self._reject(ci, f"register：evaluation {evaluation_id} 不存在")
        if erow[0] != variant_id:
            self._reject(ci, f"register：evaluation 属 variant {erow[0]}，非 {variant_id}")
        if erow[1] != "factory":
            self._reject(ci, f"register：evaluation source 须 factory（当前 {erow[1]}）")
        if erow[2] != "success" or erow[3] is None:
            self._reject(ci, f"register：出厂 evaluation 非 success/无 canonical（{erow[2]}/{erow[3]}）")
        if erow[4] is not None and erow[4] != build_target_id:
            self._reject(ci, f"register：evaluation 绑 target {erow[4]}，本次 {build_target_id}")
        required = self.read.execute("SELECT metric_id, metric_ver FROM build_target_required_metric "
                                     "WHERE build_target_id=?", (build_target_id,)).fetchall()
        missing = [rm for rm in required if self._q1(
            "SELECT 1 FROM metric_result WHERE evaluation_id=? AND metric_id=? AND metric_ver=? AND scope='aggregate'",
            (evaluation_id, rm[0], rm[1])) is None]
        if missing:
            self._reject(ci, f"register：required metric 未全产（aggregate）: {missing}")
        bt = self._bt(build_target_id)
        if bt is None or bt[4] not in ("running",):
            self._reject(ci, f"register：target {build_target_id} 未处 running（smoke/代码评审结构性未过，当前 "
                             f"{bt[4] if bt else '缺失'}）")
        if not self.review_passed(build_target_id=build_target_id, review_kind="bundle_result_review",
                                  current_subject_hash=current_subject_hash):
            self._reject(ci, f"target {build_target_id} 无通过的结果评审")

    def gate_register_baseline(self, *, baseline_id: int, variant_id: int, build_target_id: int,
                               evaluation_id: int, cycle_id: str, current_subject_hash: str,
                               identity_doc: str, repro_cmd: str,
                               run_id: Optional[int] = None,
                               capability_summary: Optional[str] = None,
                               code_ref: Optional[str] = None,
                               commit_hash: Optional[str] = None) -> None:
        """自建 baseline 注册入池（I4）：全判据过 → baseline+初变体 → legal + 落 identity。
        拒面 = _register_common_checks + identity/复现命令缺 + baseline 态非法。"""
        ci = _cnum(cycle_id)
        brow = self._q1("SELECT status FROM baseline WHERE id=?", (baseline_id,))
        if brow is None or brow[0] not in ("planned", "building"):
            self._reject(ci, f"register_baseline：baseline {baseline_id} 态非法（{brow[0] if brow else '缺失'}）")
        vrow = self._q1("SELECT baseline_id, status FROM variant WHERE id=?", (variant_id,))
        if vrow is None or vrow[0] != baseline_id:
            self._reject(ci, f"register_baseline：variant {variant_id} 不属 baseline {baseline_id}")
        if vrow[1] not in ("planned", "building"):   # 与 register_variant 同前置——不许复活 abandoned/failed 变体（codex SHOULD）
            self._reject(ci, f"register_baseline：初变体 {variant_id} 态非法（{vrow[1]}）")
        if not identity_doc.strip() or not repro_cmd.strip():
            self._reject(ci, "register_baseline：identity/复现命令字段缺")
        self._register_common_checks(ci, variant_id=variant_id, build_target_id=build_target_id,
                                     run_id=run_id, evaluation_id=evaluation_id,
                                     current_subject_hash=current_subject_hash, expect_kind="build")
        try:
            with self.daemon.transaction() as conn:
                # repro_cmd 进 identity_doc（自由 markdown）；code_ref/commit_hash 是**仓库引用**语义（喂卡片/召回），
                # 只写调用方显式给的真引用、绝不塞命令串（内审 SHOULD：污染池资产字段）。
                conn.execute("UPDATE baseline SET status='legal', identity_doc=?, "
                             "capability_summary=COALESCE(?,capability_summary), "
                             "code_ref=COALESCE(?,code_ref), commit_hash=COALESCE(?,commit_hash) "
                             "WHERE id=?", (identity_doc + "\n\n## 复现命令\n" + repro_cmd, capability_summary,
                                            code_ref, commit_hash, baseline_id))
                conn.execute("UPDATE variant SET status='legal' WHERE id=?", (variant_id,))
        except sqlite3.IntegrityError as e:
            self._reject(ci, f"register_baseline 写入被 DB 约束拒绝：{e}")

    def gate_register_variant(self, *, variant_id: int, build_target_id: int, evaluation_id: int,
                              cycle_id: str, current_subject_hash: str,
                              run_id: Optional[int] = None) -> None:
        """exec variant 注册入池：全判据过 → 仅该 variant → legal（baseline 保持 legal；身份字段触发器冻结）。"""
        ci = _cnum(cycle_id)
        vrow = self._q1("SELECT status FROM variant WHERE id=?", (variant_id,))
        if vrow is None or vrow[0] not in ("planned", "building"):
            self._reject(ci, f"register_variant：variant {variant_id} 态非法（{vrow[0] if vrow else '缺失'}）")
        self._register_common_checks(ci, variant_id=variant_id, build_target_id=build_target_id,
                                     run_id=run_id, evaluation_id=evaluation_id,
                                     current_subject_hash=current_subject_hash, expect_kind="exec")
        try:
            with self.daemon.transaction() as conn:
                conn.execute("UPDATE variant SET status='legal' WHERE id=?", (variant_id,))
        except sqlite3.IntegrityError as e:
            self._reject(ci, f"register_variant 写入被 DB 约束拒绝：{e}")

    # -- protocol（I1）------------------------------------------------------------
    def gate_new_protocol(self, *, protocol_id: int, version: int, name: str, scope_spec_json: str,
                          cycle_id: str, metric_defs: Optional[List[Dict[str, Any]]] = None,
                          metrics: Optional[List[Tuple[int, int]]] = None) -> None:
        """新 protocol@version + 其 metric_def / protocol_metric（append-only；I1：改场景/口径须升版）。
        拒：(id,version) 已存在（同号重复提交 = 未升版）；metric_def (id,version) 已存在且口径不同；
        protocol_metric 指向不存在 metric_def。"""
        ci = _cnum(cycle_id)
        if self._q1("SELECT 1 FROM protocol WHERE id=? AND version=?", (protocol_id, version)):
            self._reject(ci, f"I1：protocol ({protocol_id}@{version}) 已存在——改场景须升 version 提交新版")
        seen_defs = set()
        for md in (metric_defs or []):
            self._require_keys(ci, md, ("id", "version", "name", "direction"), "metric_def")
            key = (md["id"], md["version"])
            if key in seen_defs:   # 批内重复（受限读连接见不到未提交首插，须批内显式判——内审 SHOULD/NIT）
                self._reject(ci, f"metric_defs 批内重复 ({key[0]}@{key[1]})")
            seen_defs.add(key)
            # I1 口径比较取**全部口径列**（name/direction/unit/compute_spec）——漏比会把不同单位/计算规格
            # 静默复用旧 def、破坏 append-only 语义（codex BLOCKER）。None 与既有值不同亦算不同。
            ex = self._q1("SELECT name, direction, unit, compute_spec FROM metric_def WHERE id=? AND version=?", key)
            if ex is not None and tuple(ex) != (md["name"], md["direction"], md.get("unit"), md.get("compute_spec")):
                self._reject(ci, f"I1：metric_def ({key[0]}@{key[1]}) 已存在且口径不同"
                                 f"（{tuple(ex)} vs {(md['name'], md['direction'], md.get('unit'), md.get('compute_spec'))}）"
                                 "——改口径须升 version")
        for (mid, mver) in (metrics or []):
            if self._q1("SELECT 1 FROM metric_def WHERE id=? AND version=?", (mid, mver)) is None \
               and (mid, mver) not in seen_defs:
                self._reject(ci, f"protocol_metric 指向不存在的 metric_def ({mid}@{mver})")
        try:
            with self.daemon.transaction() as conn:
                conn.execute("INSERT INTO protocol(id,version,name,scope_spec_json) VALUES (?,?,?,?)",
                             (protocol_id, version, name, scope_spec_json))
                for md in (metric_defs or []):
                    # self.read 只见已提交态（对既有 def 去重正确）；批内重复已在上前置拒
                    if self._q1("SELECT 1 FROM metric_def WHERE id=? AND version=?", (md["id"], md["version"])) is None:
                        conn.execute("INSERT INTO metric_def(id,version,name,direction,unit,compute_spec) "
                                     "VALUES (?,?,?,?,?,?)",
                                     (md["id"], md["version"], md["name"], md["direction"],
                                      md.get("unit"), md.get("compute_spec")))
                for (mid, mver) in (metrics or []):
                    conn.execute("INSERT INTO protocol_metric(protocol_id,protocol_ver,metric_id,metric_ver) "
                                 "VALUES (?,?,?,?)", (protocol_id, version, mid, mver))
        except sqlite3.IntegrityError as e:
            self._reject(ci, f"new_protocol 写入被 DB 约束拒绝：{e}")
