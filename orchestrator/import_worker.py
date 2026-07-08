"""ImportWorker —— 外部 import 物化（§3.6.3 M4；OPEN #6 落地）。

**worker cycle 形态（OPEN #6 裁决，ROADMAP 步⑤）**：
- `cycle.route` **终身 NULL**（§2.3 七研究形态封闭不扩；NULL=非研究轮）；
- **权威标记** = 开轮同一事务写 `decision(actor='orchestrator', type='import_worker_cycle',
  payload={external_import_id})`——durable、恢复可识别（研究驱动循环据此把在途 worker 轮交本模块续跑，
  不误当「未 setup 研究轮」）；
- **收尾** = mark done/failed，**不产 cycle_report**（审计 = external_import 事件链 + run(kind=import) +
  execution_log + 开轮决策行）。

**物化链**（Advancer 独立入口，与等待问题解耦）：scope 核（allow_eval+allow_publish_pool——license 表
authorizer 对 gate 拒读，故 scope 消费点在本 worker[普通连接]）→ worker cycle → 物化变体+import 目标 →
clone（fetch provider 产内容，供应链 manifest=条目+revision 规范哈希）→ 沙箱 smoke（真子进程）→ 代码适配
评审（subject=manifest+smoke transcript）→ target_ready(running) → import run + checkpoint(origin=
external_import, source_uri/revision/manifest_hash) → 出厂 evaluation（真子进程+gate_register_evaluation，
source 仍 'factory'——外部性只在 checkpoint.origin+manifest_hash，§3.6.3 证据归属）→ gate_register_baseline
（占位 planned→legal）→ external_import(action='imported') → resolve_deps 机械 satisfied → 问题回可调度。

**失败路径全拒（§7.1 M4）**：scope 缺 → 不物化；smoke 失败 → 不 target_ready；factory eval 失败 → 不
pool_publish——均记 external_import(materialize_failed)（DDL CHECK：该 action 不携 baseline/manifest，
reason_json 记因）+ 占位 baseline 连坐 build_failed（scope 缺除外：未动工保持 planned）+ dep 保持 pending
（问题继续不可调度）+ worker cycle failed。

**恢复**：结构续跑（同 attack：目标状态阶梯 + 幂等补登 + judge replay-safe）；imported 事件已在 → 幂等跳过。
fetch provider 契约（注入；生产=真 clone/pin，测试=确定性内容）：fetch(candidate) → {files:{名:bytes…},
smoke_cmd, eval_cmd, protocol_id, protocol_ver, eval_key, target_set_hash, required:[[mid,mver]…],
artifact_type?, env_hash?}——candidate = {id, question_id, canonical_uri, revision}。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import harness as H
from . import obs_parser as OP
from . import subject_manifest as SM
from .attack_stages import _canon_hash, judge_once
from .gate_pool import PoolGate
from .phase_commit import check_or_record


def _cid(n: int) -> str:
    return f"c{n}"

_TERMINAL_TARGET = ("complete", "skipped", "failed", "engineering_blocked")


class ImportWorker:
    def __init__(self, *, state, pool_gate: PoolGate, providers: Dict[str, Callable],
                 obs_policy: Dict[str, Any], work_root: str):
        self.state = state
        self.gate = pool_gate
        self.p = providers          # 需 fetch + judge
        self.obs_policy = obs_policy
        self.work = Path(work_root)

    # ---------------------------------------------------------------- 入口 --
    def materialize_pending(self) -> List[int]:
        """扫 selected_for_materialization 工作队列（§3.6.3 物化由 Advancer 驱动、非等问题调度）；
        逐条物化。已 imported / 已 failed（worker cycle failed）→ 跳过（重试策略=后续）。返回处理的 ei ids。"""
        d = self.state.daemon
        rows = d.query("SELECT id FROM external_import WHERE action='selected_for_materialization' ORDER BY id")
        done: List[int] = []
        for (ei_id,) in rows:
            if self._already_settled(ei_id):
                continue
            self.materialize_one(ei_id)
            done.append(ei_id)
        return done

    def _already_settled(self, ei_id: int) -> bool:
        d = self.state.daemon
        sel = d.query_one("SELECT question_id, candidate_id, baseline_id FROM external_import WHERE id=?", (ei_id,))
        settled = d.query_one(
            "SELECT 1 FROM external_import WHERE candidate_id=? AND action IN ('imported','materialize_failed')",
            (sel[1],))
        return settled is not None

    def resume_cycle(self, cyc) -> None:
        """研究驱动循环递来的在途 worker 轮（route=NULL + 标记）：按标记回溯 external_import 续物化。"""
        row = self.state.daemon.query_one(
            "SELECT json_extract(payload_json,'$.external_import_id') FROM decision "
            "WHERE actor='orchestrator' AND type='import_worker_cycle' AND cycle_id=? ORDER BY id DESC LIMIT 1", (int(cyc.cycle_id[1:]),))
        if row is None or row[0] is None:
            raise ValueError(f"worker 轮 {cyc.cycle_id} 无 import_worker_cycle 标记——不可恢复态，须人工核")
        self.materialize_one(int(row[0]))

    @staticmethod
    def is_worker_cycle(daemon, cycle_id: str) -> bool:
        """route=NULL 在途轮是否物化 worker（研究驱动循环的识别点，OPEN #6 裁决④）。"""
        return daemon.query_one(
            "SELECT 1 FROM decision WHERE actor='orchestrator' AND type='import_worker_cycle' AND cycle_id=?",
            (int(cycle_id[1:]),)) is not None

    # ---------------------------------------------------------------- 单条物化 --
    def materialize_one(self, ei_id: int) -> None:
        d = self.state.daemon
        sel = d.query_one("SELECT question_id, candidate_id, baseline_id, license_review_id, "
                          "license_decision_snapshot_hash FROM external_import WHERE id=? "
                          "AND action='selected_for_materialization'", (ei_id,))
        if sel is None:
            raise ValueError(f"external_import {ei_id} 非 selected_for_materialization")
        qi, cand_id, bid, lic_id, ldsh = sel
        # ① scope 消费点（license 表 gate 不可读——authorizer 拒；worker 用普通连接核，§3.6.3）
        scope_row = d.query_one("SELECT license_scope_json FROM license_review WHERE id=?", (lic_id,))
        scope = json.loads(scope_row[0]) if scope_row and scope_row[0] else {}
        if not (scope.get("allow_eval") and scope.get("allow_publish_pool")):
            # 未动工即拒：不开 worker、不动占位（保持 planned）、dep 保持 pending
            self._record_failed(ei_id, qi, cand_id, reason="license scope 缺 allow_eval/allow_publish_pool，不物化")
            return
        cyc_id = self._worker_cycle(ei_id)
        cand = d.query_one("SELECT canonical_uri, revision FROM external_candidate WHERE id=?", (cand_id,))
        spec = self.p["fetch"]({"id": cand_id, "question_id": qi, "canonical_uri": cand[0], "revision": cand[1]})
        vid, bt_id = self._variant_and_target(cyc_id, qi, bid, spec)
        ok = self._drive_import_target(cyc_id, ei_id, qi, cand_id, bid, vid, bt_id, spec, revision=cand[1])
        # worker 收尾与 dep 解锁**同一 atomic**（内审 BLOCKER 实证：分离时崩在两提交间 → worker done 但
        # dep 永 pending、问题永不可调度且无自愈路径）。mark_cycle_done 亦补 finished_at（审计一致）。
        with self.state.atomic():
            self.state.mark_cycle_done(cyc_id, "done" if ok else "failed")
            if ok:
                self.state.resolve_deps()  # baseline→legal 机械 satisfied → 原问题回可调度（§4.2.1）

    def _worker_cycle(self, ei_id: int) -> str:
        """既有在途 worker 轮（标记匹配）→ 复用；否则开轮+标记（同一事务，OPEN #6 裁决②）。"""
        d = self.state.daemon
        row = d.query_one(
            "SELECT c.id FROM cycle c JOIN decision dd ON dd.cycle_id=c.id "
            "WHERE dd.actor='orchestrator' AND dd.type='import_worker_cycle' "
            "AND json_extract(dd.payload_json,'$.external_import_id')=? "
            "AND c.status NOT IN ('done','failed','aborted') ORDER BY c.id DESC LIMIT 1", (ei_id,))
        if row:
            return _cid(row[0])
        gver = d.query_one("SELECT MAX(version) FROM goal WHERE id=1")[0]
        with d.transaction() as conn:
            ci = conn.execute("INSERT INTO cycle(goal_id,goal_ver,status,policy_version) VALUES (1,?, 'created', ?)",
                              (gver, str(self.state.policy.get("policy_version", "v0")))).lastrowid
            conn.execute("INSERT INTO decision(cycle_id,actor,type,payload_json) VALUES (?,'orchestrator',"
                         "'import_worker_cycle',?)", (ci, json.dumps({"external_import_id": ei_id})))
        return _cid(ci)

    def _variant_and_target(self, cyc_id: str, qi: int, bid: int, spec) -> tuple:
        d = self.state.daemon
        v = d.query_one("SELECT id FROM variant WHERE baseline_id=? AND variant_key='imported'", (bid,))
        with d.transaction() as conn:
            vid = v[0] if v else conn.execute(
                "INSERT INTO variant(baseline_id,variant_key,config_json,status) VALUES (?,'imported','{}','planned')",
                (bid,)).lastrowid
            bt = conn.execute("SELECT id FROM build_target WHERE cycle_id=? AND target_kind='import'",
                              (int(cyc_id[1:]),)).fetchone()
            bt_id = bt[0] if bt else conn.execute(
                "INSERT INTO build_target(cycle_id,question_id,target_kind,seq,status,baseline_id,variant_id,plan_ref) "
                "VALUES (?,?,'import',1,'pending',?,?,?)",
                (int(cyc_id[1:]), qi, bid, vid, json.dumps(spec, ensure_ascii=False, sort_keys=True, default=str))).lastrowid
        return vid, bt_id

    def _drive_import_target(self, cyc_id: str, ei_id: int, qi: int, cand_id: int, bid: int,
                             vid: int, bt_id: int, spec, *, revision: str) -> bool:
        """物化目标阶梯（结构续跑）。返回是否成功（imported）。失败路径记 materialize_failed + 连坐。"""
        g, d = self.gate, self.state.daemon
        st = lambda: d.query_one("SELECT status FROM build_target WHERE id=?", (bt_id,))[0]
        staging = self.work / f"import{ei_id}"
        clone_dir = staging / "clone"
        if st() in _TERMINAL_TARGET:
            ok = st() == "complete" or self._settled_ok(cand_id)
            if not ok:
                # 崩在「finish failed → _record_failed」两提交之间的自愈（内审 SHOULD）：终败目标续跑到此
                # 补记 materialize_failed（幂等）——否则 settled 判不到、materialize_pending 会对 build_failed
                # 占位重开 worker 重物化（smoke 偶过则楔死在 register_baseline 拒 build_failed）。
                self._record_failed(ei_id, qi, cand_id, reason="终败目标续跑收尾（崩后补记 settling 事件）")
            self._target_pc(cyc_id, bt_id)   # 崩在 finish 与 pc 之间 → 终态短路亦补 pc（幂等，codex SHOULD）
            return ok
        if st() == "pending":
            g.gate_start_build_target(build_target_id=bt_id)
        # clone（幂等：文件已在即跳过）+ 供应链 manifest
        clone_dir.mkdir(parents=True, exist_ok=True)
        for name, content in spec["files"].items():
            # 路径卫生（codex SHOULD）：拒绝绝对路径/越界（../），并建父目录（src/model.py 类正常仓库路径）
            f = (clone_dir / name)
            if Path(name).is_absolute() or ".." in Path(name).parts:
                g.gate_finish_build_target(build_target_id=bt_id, status="failed", failure_kind="clone_path")
                self._record_failed(ei_id, qi, cand_id, reason=f"clone 路径非法（越界/绝对）：{name!r}")
                self._target_pc(cyc_id, bt_id)
                return False
            if not f.exists():
                f.parent.mkdir(parents=True, exist_ok=True)
                tmp = f.with_suffix(f.suffix + ".tmp")
                tmp.write_bytes(content if isinstance(content, bytes) else str(content).encode())
                tmp.replace(f)
        manifest_entries = [{"kind": "import_file", "ref": n, "content_hash": H.file_sha256(str(clone_dir / n))}
                            for n in sorted(spec["files"])] + \
                           [{"kind": "revision", "ref": "revision", "content_hash": _canon_hash(revision)}]
        manifest_hash = SM.subject_hash(manifest_entries)
        if st() == "building":                    # 沙箱 smoke（真子进程；失败 → 不 target_ready）
            sm = H.run_staged(spec["smoke_cmd"], staging_dir=str(staging / "smoke"),
                              log_name=f"smoke-{len(list((staging / 'smoke').glob('smoke-*.log'))) + 1 if (staging / 'smoke').exists() else 1}.log",
                              timeout_s=120)
            if sm["exit_code"] != 0:
                g.gate_finish_build_target(build_target_id=bt_id, status="failed", failure_kind="smoke")
                self._record_failed(ei_id, qi, cand_id, reason="沙箱 smoke 失败，不 target_ready")
                self._target_pc(cyc_id, bt_id)
                return False
            g.gate_progress_build_target(build_target_id=bt_id, to="smoke")
        if st() == "smoke":                       # 适配评审：subject = 供应链 manifest + smoke transcript
            logs = sorted((staging / "smoke").glob("smoke-*.log"))
            code_sh = SM.subject_hash(manifest_entries + [
                {"kind": "smoke_transcript", "ref": str(logs[-1]), "content_hash": H.file_sha256(str(logs[-1]))}])
            judge_once(d, self.p["judge"], cyc_id, bt_id, "bundle_code_review", code_sh)
            if not g.review_passed(build_target_id=bt_id, review_kind="bundle_code_review",
                                   current_subject_hash=code_sh):
                # judge FAIL → 全拒收尾（codex BLOCKER：直接闯 gate 会拒 → 重启 judge_once 复用同 fail 裁决
                # → 确定性重试死循环、worker 永悬置）
                g.gate_finish_build_target(build_target_id=bt_id, status="failed", failure_kind="review_failed")
                self._record_failed(ei_id, qi, cand_id, reason="适配评审 FAIL，不 target_ready")
                self._target_pc(cyc_id, bt_id)
                return False
            g.gate_progress_build_target(build_target_id=bt_id, to="running", current_subject_hash=code_sh)
        if st() == "running":
            return self._run_and_register_import(cyc_id, ei_id, qi, cand_id, bid, vid, bt_id, spec,
                                                 staging, clone_dir, manifest_hash, revision)
        return st() == "complete"

    def _run_and_register_import(self, cyc_id, ei_id, qi, cand_id, bid, vid, bt_id, spec,
                                 staging: Path, clone_dir: Path, manifest_hash: str, revision: str) -> bool:
        """⚠️ 恢复关键阶梯与 attack_stages._run_and_register **同构**（eval-final+exit 侧车续跑 / artifact_ref
        锚 / 无条件补登 / judge replay-safe）——那边的崩溃缝隙修复（CP5.4 两层评审五 BLOCKER）必须同步到此，
        反之亦然（内审 SHOULD：双拷贝会漂移；共享骨架抽取 = M6 硬化项）。
        另注：本 spec 每次续跑重取自 fetch provider——恢复正确性隐含依赖 fetch 对 (canonical_uri, revision)
        纯函数（pinned revision 契约）；import 目标的 complete 前置无 eval_action/variant-legal gate 核
        （eval_action=NULL、kind∉build/exec），完成纪律由本 worker 序列保证（单生产者模型）。"""
        g, d = self.gate, self.state.daemon
        run_row = d.query_one("SELECT id,status FROM run WHERE build_target_id=? ORDER BY id DESC", (bt_id,))
        if run_row and run_row[1] == "running":
            g.gate_finish_run(run_id=run_row[0], status="failed", failure_kind="aborted")
            run_row = None
        if run_row and run_row[1] == "success":
            rid = run_row[0]
        else:
            rid = g.gate_start_run(build_target_id=bt_id, cycle_id=cyc_id, variant_id=vid, kind="import",
                                   env_hash=spec.get("env_hash", "import-env"))
            main_file = sorted(spec["files"])[0]
            cand_row = d.query_one("SELECT canonical_uri FROM external_candidate WHERE id=?", (cand_id,))
            with d.transaction() as conn:      # checkpoint = 外部可评 target（供应链溯源列 DDL CHECK 焊）
                conn.execute("INSERT INTO checkpoint(variant_id,ckpt_key,path,content_hash,hash_alg,artifact_type,"
                             "origin,manifest_hash,source_uri,revision,produced_by_run) "
                             "VALUES (?,?,?,?,'sha256',?,'external_import',?,?,?,?)",
                             (vid, f"import-r{rid}", str(clone_dir / main_file),
                              H.file_sha256(str(clone_dir / main_file)),
                              spec.get("artifact_type", "external_model"),
                              manifest_hash, cand_row[0], revision, rid))
            g.gate_finish_run(run_id=rid, status="success")
        # 出厂评估（源仍 factory——外部性只在 checkpoint.origin+manifest_hash，§3.6.3 证据归属）
        erow = d.query_one("SELECT id FROM evaluation WHERE build_target_id=?", (bt_id,))
        if erow is None:
            eval_final = staging / f"eval{rid}" / "eval.log"
            if eval_final.exists():
                exit_file = eval_final.with_name("eval.log.exit")
                if not exit_file.exists():
                    raise RuntimeError(f"staging 损毁：{eval_final} 在而 exit 侧车缺——须人工核")
                ev = {"log_path": str(eval_final), "exit_code": int(exit_file.read_text())}
                eval_log = eval_final.read_bytes()
                ev["log_sha256"] = hashlib.sha256(eval_log).hexdigest()
            else:
                ev = H.run_staged(spec["eval_cmd"], staging_dir=str(staging / f"eval{rid}"),
                                  log_name="eval.log", timeout_s=600)
                eval_log = Path(ev["log_path"]).read_bytes()
            if ev["exit_code"] != 0:               # factory eval 失败 → 不 pool_publish
                g.gate_finish_build_target(build_target_id=bt_id, status="failed", failure_kind="runtime")
                self._record_failed(ei_id, qi, cand_id, reason="出厂评估失败，不 pool_publish")
                self._target_pc(cyc_id, bt_id)
                return False
            from .attack_stages import AttackStages
            metrics = AttackStages._metrics_from_eval_log(eval_log.decode("utf-8", errors="replace"), spec)
            ckrow = d.query_one("SELECT ckpt_key, content_hash FROM checkpoint WHERE produced_by_run=?", (rid,))
            res_sh = SM.subject_hash(SM.result_review_manifest(
                metrics_artifact_hash=_canon_hash(metrics), checkpoint_hashes={ckrow[0]: ckrow[1]},
                run_log_hashes={ev["log_path"]: ev["log_sha256"]},
                parser_obs_hash=_canon_hash(OP.parse_log(eval_log.decode("utf-8", errors="replace"), self.obs_policy))))
            judge_once(d, self.p["judge"], cyc_id, bt_id, "bundle_result_review", res_sh)
            if not g.review_passed(build_target_id=bt_id, review_kind="bundle_result_review",
                                   current_subject_hash=res_sh):
                g.gate_finish_build_target(build_target_id=bt_id, status="failed", failure_kind="review_failed")
                self._record_failed(ei_id, qi, cand_id, reason="结果评审 FAIL，不注册不 pool_publish")
                self._target_pc(cyc_id, bt_id)
                return False
            reg = g.gate_register_evaluation(
                cycle_id=cyc_id, build_target_id=bt_id, purpose="factory", current_subject_hash=res_sh,
                metric_results=metrics,
                create={"variant_id": vid, "protocol_id": spec["protocol_id"], "protocol_ver": spec["protocol_ver"],
                        "eval_key": spec["eval_key"], "source": "factory", "target_set_hash": spec["target_set_hash"]},
                env_hash=spec.get("env_hash", "import-env"), artifact_ref=f"sha256:{ev['log_sha256']}")
            eid, aid = reg["evaluation_id"], reg["attempt_id"]
        else:
            eid = erow[0]
            aid = d.query_one("SELECT canonical_attempt_id FROM evaluation WHERE id=?", (eid,))[0]
        self._eval_log_backfill(cyc_id, staging / f"eval{rid}" / "eval.log", aid)
        if aid is None or not OP.suspect_attempt_has_current_obs(d.conn, aid, self.obs_policy):
            raise RuntimeError(f"import 管线约束：attempt {aid} 无当前口径 parser 观测——须先 ingest 再入池")
        if d.query_one("SELECT status FROM variant WHERE id=?", (vid,))[0] != "legal":
            res_sh2 = d.query_one(
                "SELECT json_extract(payload_json,'$.subject_hash') FROM decision WHERE actor='judge' "
                "AND type='bundle_result_review' AND json_extract(payload_json,'$.build_target_id')=? "
                "ORDER BY id DESC LIMIT 1", (bt_id,))[0]
            g.gate_register_baseline(baseline_id=bid, variant_id=vid, build_target_id=bt_id,
                                     evaluation_id=eid, cycle_id=cyc_id, current_subject_hash=res_sh2,
                                     identity_doc=f"# imported baseline\n- uri 见 checkpoint.source_uri\n- revision: {revision}",
                                     repro_cmd=f"materialize external_import {ei_id}", run_id=rid)
        self._record_imported(ei_id, qi, cand_id, bid, manifest_hash)
        if d.query_one("SELECT status FROM build_target WHERE id=?", (bt_id,))[0] not in _TERMINAL_TARGET:
            g.gate_finish_build_target(build_target_id=bt_id, status="complete")
        self._target_pc(cyc_id, bt_id)
        return True

    # ---------------------------------------------------------------- 事件/杂项 --
    def _record_imported(self, ei_id, qi, cand_id, bid, manifest_hash) -> None:
        """external_import(action='imported')（幂等；DDL CHECK：须携 baseline+manifest+license 双 hash）。
        锚字段（candidate_set/selection_key/policy/license hash）复制自 selected 行——同一次选择的物化结局。"""
        d = self.state.daemon
        if d.query_one("SELECT 1 FROM external_import WHERE candidate_id=? AND action='imported'", (cand_id,)):
            return
        src = d.query_one("SELECT action_cycle, candidate_set_hash, selection_key, policy_hash, "
                          "license_decision_snapshot_hash, license_review_id FROM external_import WHERE id=?", (ei_id,))
        with d.transaction() as conn:
            conn.execute("INSERT INTO external_import(question_id,candidate_id,action,action_cycle,"
                         "candidate_set_hash,selection_key,policy_hash,license_decision_snapshot_hash,"
                         "license_review_id,baseline_id,manifest_hash) VALUES (?,?,'imported',?,?,?,?,?,?,?,?)",
                         (qi, cand_id, src[0], src[1], src[2], src[3], src[4], src[5], bid, manifest_hash))

    def _record_failed(self, ei_id, qi, cand_id, *, reason: str) -> None:
        """external_import(action='materialize_failed')（幂等；DDL CHECK：不携 baseline/manifest；reason_json 记因）。"""
        d = self.state.daemon
        if d.query_one("SELECT 1 FROM external_import WHERE candidate_id=? AND action='materialize_failed'", (cand_id,)):
            return
        src = d.query_one("SELECT action_cycle, candidate_set_hash, selection_key, policy_hash, license_review_id "
                          "FROM external_import WHERE id=?", (ei_id,))
        with d.transaction() as conn:
            conn.execute("INSERT INTO external_import(question_id,candidate_id,action,action_cycle,"
                         "candidate_set_hash,selection_key,policy_hash,license_review_id,reason_json) "
                         "VALUES (?,?,'materialize_failed',?,?,?,?,?,?)",
                         (qi, cand_id, src[0], src[1], src[2], src[3], src[4],
                          json.dumps({"reason": reason}, ensure_ascii=False)))

    def _settled_ok(self, cand_id) -> bool:
        return self.state.daemon.query_one(
            "SELECT 1 FROM external_import WHERE candidate_id=? AND action='imported'", (cand_id,)) is not None

    def _eval_log_backfill(self, cyc_id: str, log_path: Path, aid: Optional[int]) -> None:
        """attempt-owned eval log 补登+ingest（幂等 + sha256 锚强校验——同 attack_stages 契约）。"""
        if aid is None or not log_path.exists():
            return
        data = log_path.read_bytes()
        got = hashlib.sha256(data).hexdigest()
        exp = self.state.daemon.query_one("SELECT artifact_ref FROM evaluation_attempt WHERE id=?", (aid,))
        if not exp or not exp[0] or not exp[0].startswith("sha256:"):
            raise RuntimeError(f"attempt {aid} 无 sha256: artifact_ref 锚——拒绝从 staging 补登")
        if exp[0] != f"sha256:{got}":
            raise RuntimeError(f"eval log 补登哈希不符（锚 {exp[0][:19]}…，实收 sha256:{got[:12]}…）——staging 被改写")
        elid = H.register_execution_log(self.state.daemon, cycle_id=cyc_id, log_kind="eval", ref=str(log_path),
                                        content_hash=got, n_bytes=len(data), evaluation_attempt_id=aid)
        OP.ingest_observation(self.state.daemon, execution_log_id=elid, log_bytes=data, obs_policy=self.obs_policy)

    def _target_pc(self, cyc_id: str, bt_id: int) -> None:
        row = self.state.daemon.query_one("SELECT status, seq FROM build_target WHERE id=?", (bt_id,))
        ah = _canon_hash({"target": bt_id, "final": row[0], "seq": row[1]})
        with self.state.daemon.transaction() as conn:
            if check_or_record(conn, cycle_id=cyc_id, stage="bundle", target_id=bt_id,
                               artifact_hash=ah) == "conflict":
                raise ValueError(f"import target {bt_id} phase_commit 冲突（终态被改写？）")
