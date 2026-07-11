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
reason_json 记因）+ 占位 baseline 连坐 build_failed + 同一事务写失败裁决并把 exact dep 置 blocked；blocked
不再隐藏问题（§4.2.1），故问题可在下一研究轮改走另一候选/自建/分解，不会因 terminal failure 永久 pending。
已开 worker 的 cycle 同时 failed；scope 在开工前拒绝则不造 worker cycle。

**恢复**：结构续跑（同 attack：目标状态阶梯 + 幂等补登 + judge replay-safe）；imported 事件已在 → 幂等跳过。
fetch provider 契约（注入；当前默认=已登记的内容寻址冻结文件快照，测试亦可注入确定性内容；
大仓库 clone/LFS 是后续 capability）：fetch(candidate) → {files:{名:bytes…},
smoke_cmd, eval_cmd, protocol_id, protocol_ver, eval_key, target_set_hash, required:[[mid,mver]…],
artifact_type?, env_hash?}——candidate = {id, question_id, canonical_uri, revision}。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import harness as H
from . import obs_parser as OP
from . import subject_manifest as SM
from .artifact_capability import (
    ArtifactCapabilityError,
    open_artifact,
    open_directory,
    read_artifact_bytes,
    verify_open_fd,
    verify_tree_fd,
)
from .attack_stages import (
    AttackStages,
    _BundleReject,
    _canon_hash,
    judge_once,
    settle_sandbox_output_failure,
)
from .execution_sandbox import SandboxOutputError
from .gate_pool import PoolGate
from .gate_sqlite import GateReject
from .phase_commit import check_or_record
from .process_supervisor import ExecutionSupervisor


def _cid(n: int) -> str:
    return f"c{n}"

_TERMINAL_TARGET = ("complete", "skipped", "failed", "engineering_blocked")


class ImportWorker:
    def __init__(self, *, state, pool_gate: PoolGate, providers: Dict[str, Callable],
                 obs_policy: Dict[str, Any], work_root: str,
                 owner_guard: Optional[Callable[[], None]] = None,
                 execution_supervisor=None,
                 execution_sandbox=None):
        if owner_guard is not None:
            if (not isinstance(execution_supervisor, ExecutionSupervisor)
                    or not execution_supervisor.binds_fenced_owner(owner_guard)):
                raise ValueError(
                    "ImportWorker 绑定 owner_guard 时必须注入同一 owner guard 且持有"
                    " delegated instance fence 的 ExecutionSupervisor")
        self.state = state
        self.gate = pool_gate
        self.p = providers          # 需 fetch + judge
        self.obs_policy = obs_policy
        self.work = Path(work_root)
        self.owner_guard = owner_guard or (lambda: None)
        self.execution_supervisor = execution_supervisor
        self.execution_sandbox = execution_sandbox

    # ---------------------------------------------------------------- 入口 --
    def materialize_pending(self, *, max_items: Optional[int] = None) -> List[int]:
        """扫 selected_for_materialization 工作队列（§3.6.3 物化由 Advancer 驱动、非等问题调度）；
        逐条物化。已 imported / 已 failed（worker cycle failed）→ 跳过（重试策略=后续）。返回处理的 ei ids。"""
        if (max_items is not None and (isinstance(max_items, bool)
                                       or not isinstance(max_items, int) or max_items <= 0)):
            raise ValueError("max_items 须为正整数或 None")
        d = self.state.daemon
        rows = d.query(
            "SELECT s.id FROM external_import s WHERE s.action='selected_for_materialization' "
            "AND NOT EXISTS (SELECT 1 FROM external_import x WHERE x.question_id=s.question_id "
            "AND x.candidate_id=s.candidate_id AND x.action_cycle=s.action_cycle "
            "AND x.candidate_set_hash=s.candidate_set_hash AND x.selection_key=s.selection_key "
            "AND x.policy_hash=s.policy_hash AND x.action='superseded') ORDER BY s.id")
        done: List[int] = []
        for (ei_id,) in rows:
            if self._already_settled(ei_id):
                continue
            self.materialize_one(ei_id)
            done.append(ei_id)
            if max_items is not None and len(done) >= max_items:
                break
        return done

    def _already_settled(self, ei_id: int) -> bool:
        return self._has_selection_outcome(
            ei_id, ("imported", "materialize_failed", "superseded"))

    def _has_selection_outcome(self, ei_id: int, actions) -> bool:
        d = self.state.daemon
        sel = d.query_one(
            "SELECT question_id,candidate_id,action_cycle,candidate_set_hash,selection_key,policy_hash "
            "FROM external_import WHERE id=? AND action='selected_for_materialization'", (ei_id,))
        if sel is None:
            raise ValueError(f"external_import {ei_id} 非 selected_for_materialization")
        placeholders = ",".join("?" for _ in actions)
        return d.query_one(
            "SELECT 1 FROM external_import WHERE question_id=? AND candidate_id=? AND action_cycle=? "
            "AND candidate_set_hash=? AND selection_key=? AND policy_hash=? "
            f"AND action IN ({placeholders}) LIMIT 1", (*sel, *actions)) is not None

    def resume_cycle(self, cyc) -> None:
        """研究驱动循环递来的在途 worker 轮（route=NULL + 标记）：按标记回溯 external_import 续物化。"""
        row = self.state.daemon.query_one(
            "SELECT json_extract(payload_json,'$.external_import_id') FROM decision "
            "WHERE actor='orchestrator' AND type='import_worker_cycle' AND cycle_id=? ORDER BY id DESC LIMIT 1", (int(cyc.cycle_id[1:]),))
        if row is None or row[0] is None:
            raise ValueError(f"worker 轮 {cyc.cycle_id} 无 import_worker_cycle 标记——不可恢复态，须人工核")
        ei_id = int(row[0])
        if self._already_settled(ei_id):
            self._finish_settled_resume(cyc.cycle_id, ei_id)
            return
        self.materialize_one(ei_id)

    def _finish_settled_resume(self, cycle_id: str, ei_id: int) -> None:
        """Close the crash gap after an append-only outcome but before worker terminal commit.

        ``imported`` may be written just before target completion/phase_commit; replay those mechanical
        suffixes before resolving dependencies.  A failed outcome is only emitted after a started target
        is terminal (or before any target exists on fetch failure), so a live target is corruption rather
        than permission to invent a new failure transition.
        """
        d = self.state.daemon
        imported = self._settled_ok(ei_id)
        failed = self._has_selection_outcome(ei_id, ("materialize_failed",))
        superseded = self._has_selection_outcome(ei_id, ("superseded",))
        if sum((imported, failed, superseded)) != 1:
            raise RuntimeError(
                f"external_import {ei_id} settled outcome 非唯一（imported={imported}, "
                f"failed={failed}, superseded={superseded}）")
        targets = d.query(
            "SELECT id,status FROM build_target WHERE cycle_id=? AND target_kind='import' ORDER BY id",
            (int(cycle_id[1:]),))
        if len(targets) > 1:
            raise RuntimeError(f"import worker {cycle_id} 有多个 import target")
        if imported:
            if len(targets) != 1:
                raise RuntimeError(f"imported external_import {ei_id} 缺 worker target")
            bt_id, status = targets[0]
            if status not in _TERMINAL_TARGET:
                self.gate.gate_finish_build_target(
                    build_target_id=bt_id, status="complete")
            elif status != "complete":
                raise RuntimeError(
                    f"imported external_import {ei_id} 的 target {bt_id} 终态为 {status}")
            self._target_pc(cycle_id, bt_id)
        elif targets and targets[0][1] not in _TERMINAL_TARGET:
            raise RuntimeError(
                f"settled failed/superseded external_import {ei_id} 仍有非终态 target {targets[0]}")

        if failed:
            failure = d.query_one(
                "SELECT s.question_id,s.candidate_id,json_extract(f.reason_json,'$.reason') "
                "FROM external_import s JOIN external_import f ON f.question_id=s.question_id "
                "AND f.candidate_id=s.candidate_id AND f.action_cycle=s.action_cycle "
                "AND f.candidate_set_hash=s.candidate_set_hash AND f.selection_key=s.selection_key "
                "AND f.policy_hash=s.policy_hash AND f.action='materialize_failed' "
                "WHERE s.id=? AND s.action='selected_for_materialization' ORDER BY f.id", (ei_id,))
            if failure is None:
                raise RuntimeError(f"external_import {ei_id} failed outcome 无 durable reason event")
            # Reconciles databases written before failure event + blocked dep became one transaction.
            self._record_failed(
                ei_id, failure[0], failure[1], reason=failure[2] or "legacy materialize_failed")

        with self.state.atomic() as conn:
            if failed:
                baseline = conn.execute(
                    "SELECT baseline_id FROM external_import WHERE id=?", (ei_id,)).fetchone()
                if baseline is None or baseline[0] is None:
                    raise RuntimeError(f"external_import {ei_id} 缺占位 baseline")
                conn.execute(
                    "UPDATE baseline SET status='build_failed' WHERE id=? AND status='planned'",
                    (baseline[0],))
            self.state.mark_cycle_done(
                cycle_id, "done" if imported else ("aborted" if superseded else "failed"))
            if imported:
                self.state.resolve_deps()

    @staticmethod
    def is_worker_cycle(daemon, cycle_id: str) -> bool:
        """route=NULL 在途轮是否物化 worker（研究驱动循环的识别点，OPEN #6 裁决④）。"""
        return daemon.query_one(
            "SELECT 1 FROM decision WHERE actor='orchestrator' AND type='import_worker_cycle' AND cycle_id=?",
            (int(cycle_id[1:]),)) is not None

    # ---------------------------------------------------------------- 单条物化 --
    def materialize_one(self, ei_id: int) -> None:
        d = self.state.daemon
        if self._has_selection_outcome(ei_id, ("superseded",)):
            return
        sel = d.query_one("SELECT question_id, candidate_id, baseline_id, license_review_id, "
                          "license_decision_snapshot_hash FROM external_import WHERE id=? "
                          "AND action='selected_for_materialization'", (ei_id,))
        if sel is None:
            raise ValueError(f"external_import {ei_id} 非 selected_for_materialization")
        qi, cand_id, bid, lic_id, ldsh = sel
        lineage = d.query_one(
            "SELECT q.goal_id,q.goal_ver,c.goal_id,c.goal_ver,"
            "(SELECT MAX(version) FROM goal WHERE id=q.goal_id) "
            "FROM external_import s JOIN question q ON q.id=s.question_id "
            "JOIN cycle c ON c.id=s.action_cycle WHERE s.id=?", (ei_id,))
        if (lineage is None or tuple(lineage[:2]) != tuple(lineage[2:4])
                or lineage[1] != lineage[4]):
            raise RuntimeError(
                f"external_import {ei_id} 不属于 current goal lineage；须先 supersede，拒绝物化")
        # ① scope 消费点（license 表 gate 不可读——authorizer 拒；worker 用普通连接核，§3.6.3）
        scope_row = d.query_one("SELECT license_scope_json FROM license_review WHERE id=?", (lic_id,))
        scope = json.loads(scope_row[0]) if scope_row and scope_row[0] else {}
        if not (scope.get("allow_eval") and scope.get("allow_publish_pool")):
            # 未动工即拒：不开 worker；但 selection 已终败，故原子标 baseline=build_failed、dep=blocked，
            # 让原问题回到重规划集合，不能留下一个永远无人消费的 pending 依赖。
            self._record_failed(ei_id, qi, cand_id, reason="license scope 缺 allow_eval/allow_publish_pool，不物化")
            return
        cyc_id = self._worker_cycle(ei_id, qi)
        cand = d.query_one(
            "SELECT canonical_uri,revision,source_kind,search_snapshot_json,search_snapshot_hash "
            "FROM external_candidate WHERE id=?", (cand_id,))
        self.owner_guard()
        try:
            spec = self.p["fetch"]({
                "id": cand_id, "question_id": qi, "canonical_uri": cand[0],
                "revision": cand[1], "source_kind": cand[2],
                "search_snapshot_json": cand[3], "search_snapshot_hash": cand[4],
            })
        except ValueError as error:
            # Frozen snapshot/spec rejection is a durable candidate failure.  Infrastructure/control
            # exceptions (owner loss, supervisor failure, budget stop, provider bug) deliberately
            # propagate so they cannot be mislabeled as a scientific/materialization outcome.
            self._record_failed(
                ei_id, qi, cand_id,
                reason=f"冻结候选物化规格无效：{type(error).__name__}: {error}")
            with self.state.atomic() as conn:
                conn.execute(
                    "UPDATE baseline SET status='build_failed' WHERE id=? AND status='planned'",
                    (bid,))
                self.state.mark_cycle_done(cyc_id, "failed")
            return
        if (self.execution_sandbox is not None
                and spec.get("env_hash") != self.execution_sandbox.environment_hash):
            self._record_failed(
                ei_id, qi, cand_id,
                reason="冻结候选 environment_hash 与 policy pinned sandbox runtime identity 不一致")
            with self.state.atomic():
                self.state.mark_cycle_done(cyc_id, "failed")
            return
        if spec.get("requires_adversarial_sandbox") is True:
            if self.execution_sandbox is None:
                # ExecutionSupervisor fences ownership/descendants but explicitly is not an adversarial
                # sandbox.  Default discovery content is untrusted, so a missing strong runner remains
                # a durable candidate failure rather than silently falling back to host execution.
                self._record_failed(
                    ei_id, qi, cand_id,
                    reason="默认冻结候选要求 adversarial sandbox；当前仅有 lifecycle supervisor，拒绝在 host 执行")
                with self.state.atomic():
                    self.state.mark_cycle_done(cyc_id, "failed")
                return
        vid, bt_id = self._variant_and_target(cyc_id, qi, bid, spec)
        try:
            ok = self._drive_import_target(
                cyc_id, ei_id, qi, cand_id, bid, vid, bt_id, spec,
                revision=cand[1], source_uri=cand[0])
        except SandboxOutputError as error:
            # The guardian has drained the exact container, but an unsafe output
            # quarantine must never become a checkpoint/metric artifact.  Settle
            # its exact DB owner and the import failure so retries cannot wedge on
            # a permanently-running run/attempt.
            settle_sandbox_output_failure(self.gate, d, bt_id, error)
            self._record_failed(
                ei_id, qi, cand_id,
                reason=("沙箱输出隔离区拒收，不发布、不 pool_publish；receipt="
                        f"{error.receipt_path.name}: {error}"))
            self._target_pc(cyc_id, bt_id)
            ok = False
        # worker 收尾与 dep 解锁**同一 atomic**（内审 BLOCKER 实证：分离时崩在两提交间 → worker done 但
        # dep 永 pending、问题永不可调度且无自愈路径）。mark_cycle_done 亦补 finished_at（审计一致）。
        with self.state.atomic():
            self.state.mark_cycle_done(cyc_id, "done" if ok else "failed")
            if ok:
                self.state.resolve_deps()  # baseline→legal 机械 satisfied → 原问题回可调度（§4.2.1）

    def _worker_cycle(self, ei_id: int, question_id: int) -> str:
        """既有在途 worker 轮（标记匹配）→ 复用；否则开轮+标记（同一事务，OPEN #6 裁决②）。"""
        d = self.state.daemon
        row = d.query_one(
            "SELECT c.id FROM cycle c JOIN decision dd ON dd.cycle_id=c.id "
            "WHERE dd.actor='orchestrator' AND dd.type='import_worker_cycle' "
            "AND json_extract(dd.payload_json,'$.external_import_id')=? "
            "AND c.status NOT IN ('done','failed','aborted') ORDER BY c.id DESC LIMIT 1", (ei_id,))
        if row:
            marker = d.query_one(
                "SELECT 1 FROM decision WHERE cycle_id=? AND actor='orchestrator' "
                "AND type='import_worker_cycle' AND json_valid(payload_json) "
                "AND json_extract(payload_json,'$.external_import_id')=? "
                "AND json_extract(payload_json,'$.question_id')=? LIMIT 1",
                (row[0], ei_id, question_id))
            if marker is None:
                # 兼容迁移前已在途 marker：从权威 selection 重新核 question 后追加精确绑定，
                # 不改写旧审计事实。
                selected = d.query_one(
                    "SELECT question_id FROM external_import "
                    "WHERE id=? AND action='selected_for_materialization'", (ei_id,))
                if selected is None or selected[0] != question_id:
                    raise RuntimeError(f"worker cycle c{row[0]} 的 import/question 绑定损坏")
                with d.transaction() as conn:
                    conn.execute(
                        "INSERT INTO decision(cycle_id,actor,type,payload_json) "
                        "VALUES (?,'orchestrator','import_worker_cycle',?)",
                        (row[0], json.dumps({"external_import_id": ei_id,
                                             "question_id": question_id}, sort_keys=True)))
            return _cid(row[0])
        lineage = d.query_one(
            "SELECT c.goal_id,c.goal_ver FROM external_import s "
            "JOIN cycle c ON c.id=s.action_cycle WHERE s.id=?", (ei_id,))
        if lineage is None:
            raise RuntimeError(f"external_import {ei_id} action_cycle lineage 缺失")
        goal_id, gver = lineage
        current = d.query_one("SELECT MAX(version) FROM goal WHERE id=?", (goal_id,))
        if current is None or current[0] != gver:
            raise RuntimeError(f"external_import {ei_id} action_cycle 已非 current，拒绝开 worker")
        with d.transaction() as conn:
            ci = conn.execute("INSERT INTO cycle(goal_id,goal_ver,status,policy_version) VALUES (?,?, 'created', ?)",
                              (goal_id, gver, str(self.state.policy.get("policy_version", "v0")))).lastrowid
            conn.execute("INSERT INTO decision(cycle_id,actor,type,payload_json) VALUES (?,'orchestrator',"
                         "'import_worker_cycle',?)",
                         (ci, json.dumps({"external_import_id": ei_id,
                                          "question_id": question_id}, sort_keys=True)))
        return _cid(ci)

    def _variant_and_target(self, cyc_id: str, qi: int, bid: int, spec) -> tuple:
        d = self.state.daemon
        v = d.query_one("SELECT id FROM variant WHERE baseline_id=? AND variant_key='imported'", (bid,))
        expected_ref = json.dumps(
            self._spec_ref(spec), ensure_ascii=False, sort_keys=True)
        with d.transaction() as conn:
            vid = v[0] if v else conn.execute(
                "INSERT INTO variant(baseline_id,variant_key,config_json,status) VALUES (?,'imported','{}','planned')",
                (bid,)).lastrowid
            targets = conn.execute(
                "SELECT id,question_id,baseline_id,variant_id,plan_ref FROM build_target "
                "WHERE cycle_id=? AND target_kind='import' ORDER BY id",
                (int(cyc_id[1:]),)).fetchall()
            if len(targets) > 1:
                raise RuntimeError(f"import worker {cyc_id} 有多个 import target")
            if targets:
                bt = targets[0]
                if tuple(bt[1:4]) != (qi, bid, vid) or bt[4] != expected_ref:
                    raise RuntimeError(
                        f"import worker {cyc_id} existing target {bt[0]} 与冻结 spec 身份漂移")
                bt_id = bt[0]
            else:
                bt_id = conn.execute(
                    "INSERT INTO build_target(cycle_id,question_id,target_kind,seq,status,baseline_id,variant_id,plan_ref) "
                    "VALUES (?,?,'import',1,'pending',?,?,?)",
                    (int(cyc_id[1:]), qi, bid, vid, expected_ref)).lastrowid
        return vid, bt_id

    @staticmethod
    def _execution_contract(spec: Dict[str, Any]) -> Dict[str, Any]:
        """Everything affecting adapter execution, excluding file bytes (hashed separately)."""
        return {
            "smoke_cmd": list(spec["smoke_cmd"]), "eval_cmd": list(spec["eval_cmd"]),
            "protocol_id": spec["protocol_id"], "protocol_ver": spec["protocol_ver"],
            "eval_key": spec["eval_key"], "target_set_hash": spec["target_set_hash"],
            "required": spec["required"], "artifact_relpath": spec.get("artifact_relpath"),
            "artifact_type": spec.get("artifact_type", "external_model"),
            "env_hash": spec.get("env_hash", "import-env"),
            "supply_chain": spec.get("supply_chain") or {},
        }

    @classmethod
    def _spec_ref(cls, spec: Dict[str, Any]) -> Dict[str, Any]:
        """Bounded DB plan_ref: content identities, never the up-to-64MiB raw frozen files."""
        return {
            "materialization_contract": cls._execution_contract(spec),
            "files": [{
                "path": name,
                "sha256": "sha256:" + hashlib.sha256(
                    content if isinstance(content, bytes) else str(content).encode()).hexdigest(),
                "bytes": len(content if isinstance(content, bytes) else str(content).encode()),
            } for name, content in sorted(spec["files"].items())],
        }

    def _drive_import_target(self, cyc_id: str, ei_id: int, qi: int, cand_id: int, bid: int,
                             vid: int, bt_id: int, spec, *, revision: str,
                             source_uri: str) -> bool:
        """物化目标阶梯（结构续跑）。返回是否成功（imported）。失败路径记 materialize_failed + 连坐。"""
        g, d = self.gate, self.state.daemon
        st = lambda: d.query_one("SELECT status FROM build_target WHERE id=?", (bt_id,))[0]
        staging = self.work / f"import{ei_id}"
        clone_dir = staging / "clone"
        if st() in _TERMINAL_TARGET:
            ok = st() == "complete" or self._settled_ok(ei_id)
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
            for parent in f.parents:
                if parent == clone_dir.parent:
                    break
                if parent.exists() and parent.is_symlink():
                    raise RuntimeError(f"import clone parent 不得是 symlink: {parent}")
            payload = content if isinstance(content, bytes) else str(content).encode()
            expected_file_hash = hashlib.sha256(payload).hexdigest()
            if f.exists():
                try:
                    existing = read_artifact_bytes(
                        f, expected_hash=expected_file_hash,
                        expected_size=len(payload),
                        label=f"import clone {name}")
                except ArtifactCapabilityError as error:
                    raise RuntimeError(
                        f"import clone 既有文件与冻结 snapshot 不一致: {name}") from error
                if existing != payload:
                    raise RuntimeError(f"import clone 既有文件与冻结 snapshot 不一致: {name}")
            else:
                f.parent.mkdir(parents=True, exist_ok=True)
                tmp = f.with_suffix(f.suffix + ".tmp")
                tmp.write_bytes(payload)
                tmp.replace(f)
        manifest_entries = [{"kind": "import_file", "ref": n, "content_hash": H.file_sha256(str(clone_dir / n))}
                            for n in sorted(spec["files"])] + \
                           [{"kind": "revision", "ref": "revision", "content_hash": _canon_hash(revision)},
                            {"kind": "source_uri", "ref": "source_uri", "content_hash": _canon_hash(source_uri)},
                            {"kind": "materialization_contract", "ref": "execution_contract",
                             "content_hash": _canon_hash(self._execution_contract(spec))}]
        for key, value in sorted((spec.get("supply_chain") or {}).items()):
            manifest_entries.append({
                "kind": "supply_chain", "ref": key,
                "content_hash": _canon_hash(value),
            })
        manifest_hash = SM.subject_hash(manifest_entries)
        if st() == "building":                    # 沙箱 smoke（真子进程；失败 → 不 target_ready）
            smoke_dir = staging / "smoke"
            existing_final = H.latest_smoke_log(smoke_dir)
            partials = sorted(smoke_dir.glob("smoke-*.log.partial")) if smoke_dir.exists() else []
            if len(partials) > 1:
                raise RuntimeError(f"import target {bt_id} 有多个未发布 smoke partial")
            smoke_name = (existing_final.name if existing_final is not None else
                          (partials[0].name[:-len(".partial")] if partials else "smoke-1.log"))
            smoke_context = {
                "cycle_id": cyc_id, "external_import_id": ei_id,
                "build_target_id": bt_id, "phase": "smoke",
                "reconcile_protocol": "execution-owner-v1",
                "db_owner_kind": "build_target", "db_owner_id": bt_id,
            }
            if existing_final is not None:
                exit_file = existing_final.with_name(existing_final.name + ".exit")
                if not exit_file.exists():
                    raise RuntimeError(
                        f"staging 损毁：{existing_final} 在而 exit 侧车缺——须人工核")
                smoke_bytes = read_artifact_bytes(
                    existing_final, label="persisted import smoke log")
                sm = {"exit_code": int(read_artifact_bytes(
                          exit_file, max_bytes=32,
                          label="import smoke exit sidecar").decode("ascii")),
                      "log_path": str(existing_final),
                      "log_sha256": hashlib.sha256(smoke_bytes).hexdigest(),
                      "log_bytes": len(smoke_bytes)}
            else:
                sm = H.recover_staged_result(
                    staging_dir=str(smoke_dir), log_name=smoke_name,
                    execution_supervisor=self.execution_supervisor,
                    execution_kind="import-smoke", execution_context=smoke_context,
                    execution_sandbox=self.execution_sandbox)
                if sm is None:
                    self.owner_guard()
                    sm = self._run_frozen_command(
                        spec["smoke_cmd"], clone_dir, spec,
                        staging_dir=str(smoke_dir),
                        log_name=smoke_name, timeout_s=120,
                        execution_supervisor=self.execution_supervisor,
                        execution_kind="import-smoke", execution_context=smoke_context)
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

    def _run_frozen_command(self, cmd, clone_dir: Path, spec: Dict[str, Any],
                            **run_kwargs):
        """Run an adapter against stable dir/file capabilities, then re-verify bytes."""
        rel = spec.get("artifact_relpath") or sorted(spec["files"])[0]
        payloads = {
            name: payload if isinstance(payload, bytes) else str(payload).encode()
            for name, payload in spec["files"].items()
        }
        hashes = {
            name: hashlib.sha256(payload).hexdigest()
            for name, payload in payloads.items()
        }
        source_fd = -1
        artifact_fd = -1
        invocation = None
        try:
            source_fd = open_directory(clone_dir, label="import frozen tree")
            verify_tree_fd(
                source_fd, hashes, label="import frozen tree", exact=True)
            capability = open_artifact(
                Path(f"/proc/self/fd/{source_fd}") / rel,
                expected_hash=hashes[rel], expected_size=len(payloads[rel]),
                label="import artifact capability")
            identity = capability.identity
            artifact_fd = capability.detach()
            repo_proc = f"/proc/self/fd/{source_fd}"
            artifact_proc = f"/proc/self/fd/{artifact_fd}"
            resolved = [
                arg.replace("{repo}", repo_proc).replace("{artifact}", artifact_proc)
                for arg in cmd
            ]
            if self.execution_sandbox is None:
                result = H.run_staged(
                    resolved, pass_fds=(source_fd, artifact_fd), **run_kwargs)
            else:
                sandbox_context = dict(run_kwargs.get("execution_context") or {})
                log_name = run_kwargs["log_name"]
                if ("log_name" in sandbox_context
                        and sandbox_context["log_name"] != log_name):
                    raise RuntimeError("import sandbox context/log_name 冲突")
                sandbox_context["log_name"] = log_name
                invocation = self.execution_sandbox.prepare(
                    resolved, staging_dir=run_kwargs["staging_dir"],
                    log_name=log_name, env=None,
                    timeout_s=run_kwargs.get("timeout_s", 600.0),
                    fd_expectations=((
                        artifact_fd, identity.content_hash, identity.size_bytes,
                        identity.device, identity.inode),),
                    tree_expectations=((source_fd, hashes, ()),),
                    execution_context=sandbox_context,
                    execution_supervisor=self.execution_supervisor)
                sandbox_kwargs = dict(run_kwargs)
                sandbox_kwargs.update({
                    "env": invocation.env,
                    "pass_fds": invocation.pass_fds,
                    "sandbox_invocation": invocation,
                })
                result = H.run_staged(invocation.argv, **sandbox_kwargs)
            verify_open_fd(
                artifact_fd, expected_hash=identity.content_hash,
                expected_size=identity.size_bytes,
                expected_device=identity.device, expected_inode=identity.inode)
            verify_tree_fd(
                source_fd, hashes, label="import frozen tree post-use", exact=True)
            return result
        except ArtifactCapabilityError as error:
            raise RuntimeError(str(error)) from error
        finally:
            if invocation is not None:
                invocation.close()
            for fd in (artifact_fd, source_fd):
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

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
            main_file = spec.get("artifact_relpath") or sorted(spec["files"])[0]
            main_payload = (spec["files"][main_file]
                            if isinstance(spec["files"][main_file], bytes)
                            else str(spec["files"][main_file]).encode())
            cand_row = d.query_one("SELECT canonical_uri FROM external_candidate WHERE id=?", (cand_id,))
            with open_artifact(
                    clone_dir / main_file,
                    expected_hash=hashlib.sha256(main_payload).hexdigest(),
                    expected_size=len(main_payload),
                    label="import checkpoint artifact") as artifact_capability:
                artifact_hash = artifact_capability.identity.content_hash.removeprefix(
                    "sha256:")
                with d.transaction() as conn:  # checkpoint = 外部可评 target（供应链溯源列 DDL CHECK 焊）
                    conn.execute("INSERT INTO checkpoint(variant_id,ckpt_key,path,content_hash,hash_alg,artifact_type,"
                                 "origin,manifest_hash,source_uri,revision,produced_by_run) "
                                 "VALUES (?,?,?,?,'sha256',?,'external_import',?,?,?,?)",
                                 (vid, f"import-r{rid}", str(clone_dir / main_file),
                                  artifact_hash,
                                  spec.get("artifact_type", "external_model"),
                                  manifest_hash, cand_row[0], revision, rid))
                artifact_capability.verify_unchanged()
                artifact_capability.verify_path_binding()
                g.gate_finish_run(run_id=rid, status="success")
                artifact_capability.verify_unchanged()
                artifact_capability.verify_path_binding()
        # 出厂评估（源仍 factory——外部性只在 checkpoint.origin+manifest_hash，§3.6.3 证据归属）。
        # 与 attack lockstep：任何外部 eval 进程放行前，evaluation+attempt(running) 已耐久落库；
        # guardian receipt 因而能以 execution-owner-v1 精确回指 DB owner，禁止事后伪造成功 attempt。
        erow = d.query_one(
            "SELECT id,status,canonical_attempt_id FROM evaluation WHERE build_target_id=?", (bt_id,))
        eval_final: Optional[Path] = None
        if erow is None or erow[1] != "success":
            if erow is None:
                attempt_purpose = "factory"
                started = g.gate_start_attempt(
                    cycle_id=cyc_id, purpose=attempt_purpose, build_target_id=bt_id,
                    create={"variant_id": vid, "protocol_id": spec["protocol_id"],
                            "protocol_ver": spec["protocol_ver"], "eval_key": spec["eval_key"],
                            "source": "factory", "target_set_hash": spec["target_set_hash"]},
                    env_hash=spec.get("env_hash", "import-env"), watchdog_sec=600.0)
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
                    # 已完成的失败执行是不可覆写事实。若此前崩在 target/settling 收口缝隙，只补收口，
                    # 不把一次失败偷偷重跑成另一事实。
                    target_failure = ("protocol_violation"
                                      if latest[4] in ("protocol_violation", "metric_missing",
                                                       "data_invalid", "artifact_invalid")
                                      else latest[4] or "runtime")
                    g.gate_finish_build_target(
                        build_target_id=bt_id, status="failed", failure_kind=target_failure)
                    self._record_failed(
                        ei_id, qi, cand_id, reason=f"出厂评估已失败（{latest[4] or 'runtime'}），不 pool_publish")
                    self._target_pc(cyc_id, bt_id)
                    return False
                elif latest[1] == "aborted":
                    attempt_purpose = "retry"
                    started = g.gate_start_attempt(
                        cycle_id=cyc_id, purpose=attempt_purpose, build_target_id=bt_id,
                        evaluation_id=erow[0], retry_of=latest[0],
                        env_hash=spec.get("env_hash", "import-env"), watchdog_sec=600.0)
                else:
                    raise RuntimeError(
                        f"evaluation {erow[0]} status={erow[1]} 与 latest attempt={latest[1]} 不一致")
            eid, aid, attempt_no = (
                started["evaluation_id"], started["attempt_id"], started["attempt_no"])
            eval_dir = (staging / f"eval{rid}" if attempt_no == 1 else
                        staging / f"eval{rid}" / f"retry-a{aid}")
            eval_final = eval_dir / "eval.log"
            eval_context = {
                "cycle_id": cyc_id,
                "external_import_id": ei_id,
                "build_target_id": bt_id,
                "run_id": rid, "phase": "eval",
                "reconcile_protocol": "execution-owner-v1",
                "db_owner_kind": "evaluation_attempt",
                "db_owner_id": aid,
            }
            if eval_final.exists():
                exit_file = eval_final.with_name("eval.log.exit")
                if not exit_file.exists():
                    raise RuntimeError(f"staging 损毁：{eval_final} 在而 exit 侧车缺——须人工核")
                ev = {"log_path": str(eval_final), "exit_code": int(
                    read_artifact_bytes(
                        exit_file, max_bytes=32,
                        label="import eval exit sidecar").decode("ascii"))}
                eval_log = read_artifact_bytes(
                    eval_final, label="persisted import eval log")
                ev["log_sha256"] = hashlib.sha256(eval_log).hexdigest()
            else:
                ev = H.recover_staged_result(
                    staging_dir=str(eval_dir), log_name="eval.log",
                    execution_supervisor=self.execution_supervisor,
                    execution_kind="import-eval", execution_context=eval_context,
                    execution_sandbox=self.execution_sandbox)
                if ev is None:
                    self.owner_guard()
                    ev = self._run_frozen_command(
                        spec["eval_cmd"], clone_dir, spec,
                        staging_dir=str(eval_dir),
                        log_name="eval.log", timeout_s=600,
                        execution_supervisor=self.execution_supervisor,
                        execution_kind="import-eval", execution_context=eval_context)
                eval_log = read_artifact_bytes(
                    ev["log_path"], expected_hash=ev["log_sha256"],
                    expected_size=ev.get("log_bytes"),
                    label="import eval log receipt")
            if ev["exit_code"] != 0:               # factory eval 失败 → 不 pool_publish
                g.gate_finish_attempt(
                    attempt_id=aid, status="failed", failure_kind="runtime",
                    transcript_ref=ev.get("process_receipt_path"),
                    artifact_ref=f"sha256:{ev['log_sha256']}")
                g.gate_finish_evaluation(evaluation_id=eid)
                g.gate_finish_build_target(build_target_id=bt_id, status="failed", failure_kind="runtime")
                self._record_failed(ei_id, qi, cand_id, reason="出厂评估失败，不 pool_publish")
                self._target_pc(cyc_id, bt_id)
                return False
            try:
                metrics = AttackStages._metrics_from_eval_log(eval_log.decode("utf-8", errors="replace"))
            except _BundleReject as e:
                g.gate_finish_attempt(
                    attempt_id=aid, status="failed", failure_kind=e.failure_kind,
                    transcript_ref=ev.get("process_receipt_path"),
                    artifact_ref=f"sha256:{ev['log_sha256']}")
                g.gate_finish_evaluation(evaluation_id=eid)
                g.gate_finish_build_target(
                    build_target_id=bt_id, status="failed", failure_kind=e.failure_kind)
                self._record_failed(ei_id, qi, cand_id, reason=f"出厂评估协议违规：{e}")
                self._target_pc(cyc_id, bt_id)
                return False
            ckrow = d.query_one("SELECT ckpt_key, content_hash FROM checkpoint WHERE produced_by_run=?", (rid,))
            res_sh = SM.subject_hash(SM.result_review_manifest(
                metrics_artifact_hash=_canon_hash(metrics), checkpoint_hashes={ckrow[0]: ckrow[1]},
                run_log_hashes={ev["log_path"]: ev["log_sha256"]},
                parser_obs_hash=_canon_hash(OP.parse_log(eval_log.decode("utf-8", errors="replace"), self.obs_policy))))
            judge_once(d, self.p["judge"], cyc_id, bt_id, "bundle_result_review", res_sh)
            if not g.review_passed(build_target_id=bt_id, review_kind="bundle_result_review",
                                   current_subject_hash=res_sh):
                g.gate_finish_attempt(
                    attempt_id=aid, status="failed", failure_kind="protocol_violation",
                    transcript_ref=ev.get("process_receipt_path"),
                    artifact_ref=f"sha256:{ev['log_sha256']}")
                g.gate_finish_evaluation(evaluation_id=eid)
                g.gate_finish_build_target(build_target_id=bt_id, status="failed", failure_kind="review_failed")
                self._record_failed(ei_id, qi, cand_id, reason="结果评审 FAIL，不注册不 pool_publish")
                self._target_pc(cyc_id, bt_id)
                return False
            try:
                reg = g.gate_register_evaluation(
                    cycle_id=cyc_id, build_target_id=bt_id, purpose=attempt_purpose,
                    current_subject_hash=res_sh, metric_results=metrics, attempt_id=aid,
                    artifact_ref=f"sha256:{ev['log_sha256']}",
                    transcript_ref=ev.get("process_receipt_path"))
            except GateReject as e:
                g.gate_finish_attempt(
                    attempt_id=aid, status="failed", failure_kind="protocol_violation",
                    transcript_ref=ev.get("process_receipt_path"),
                    artifact_ref=f"sha256:{ev['log_sha256']}")
                g.gate_finish_evaluation(evaluation_id=eid)
                g.gate_finish_build_target(
                    build_target_id=bt_id, status="failed", failure_kind="protocol_violation")
                self._record_failed(ei_id, qi, cand_id, reason=f"出厂评估注册被拒：{e}")
                self._target_pc(cyc_id, bt_id)
                return False
            eid, aid = reg["evaluation_id"], reg["attempt_id"]
        else:
            eid, aid = erow[0], erow[2]
            attempt_no = d.query_one(
                "SELECT attempt_no FROM evaluation_attempt WHERE id=?", (aid,))[0]
            eval_final = (staging / f"eval{rid}" / "eval.log" if attempt_no == 1 else
                          staging / f"eval{rid}" / f"retry-a{aid}" / "eval.log")
        self._eval_log_backfill(cyc_id, eval_final, aid)
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
        with d.transaction() as conn:
            src = conn.execute(
                "SELECT question_id,candidate_id,action_cycle,candidate_set_hash,selection_key,policy_hash,"
                "license_decision_snapshot_hash,license_review_id,baseline_id FROM external_import "
                "WHERE id=? AND action='selected_for_materialization'", (ei_id,)).fetchone()
            if (src is None or src[0] != qi or src[1] != cand_id or src[8] != bid
                    or src[6] is None or src[7] is None):
                raise RuntimeError(f"external_import {ei_id} imported selection 绑定损坏")
            terminal = conn.execute(
                "SELECT id,action,baseline_id,manifest_hash,license_review_id,"
                "license_decision_snapshot_hash FROM external_import WHERE question_id=? "
                "AND candidate_id=? AND action_cycle=? AND candidate_set_hash=? "
                "AND selection_key=? AND policy_hash=? "
                "AND action IN ('imported','materialize_failed','superseded') ORDER BY id",
                src[:6]).fetchall()
            if len(terminal) > 1 or (terminal and terminal[0][1] != "imported"):
                raise RuntimeError(
                    f"external_import {ei_id} 已有冲突/重复终局: "
                    f"{[(row[0], row[1]) for row in terminal]}")
            if terminal:
                row = terminal[0]
                if tuple(row[2:]) != (bid, manifest_hash, src[7], src[6]):
                    raise RuntimeError(
                        f"external_import {ei_id} imported replay 身份与 durable event 不一致")
                return
            conn.execute("INSERT INTO external_import(question_id,candidate_id,action,action_cycle,"
                         "candidate_set_hash,selection_key,policy_hash,license_decision_snapshot_hash,"
                         "license_review_id,baseline_id,manifest_hash) VALUES (?,?,'imported',?,?,?,?,?,?,?,?)",
                         (qi, cand_id, src[2], src[3], src[4], src[5], src[6], src[7], bid,
                          manifest_hash))

    def _record_failed(self, ei_id, qi, cand_id, *, reason: str) -> None:
        """Atomically settle a failed selection and unblock the question for replanning.

        The worker never retries a terminal ``materialize_failed`` event.  Leaving its dependency pending
        would therefore be an unrecoverable scheduler deadlock, not a retry policy.  Reference §4.2.1
        defines ``blocked`` as the adjudicated escape state, so failure event + decision + baseline failure
        + exact dep transition are one short transaction and replay idempotently.
        """
        d = self.state.daemon
        if not isinstance(reason, str) or not reason:
            raise ValueError("materialize_failed reason 须为非空文本")
        reason = reason.encode("utf-8", errors="replace").decode("utf-8")
        reason_bytes = reason.encode("utf-8")
        if len(reason_bytes) > 4096:
            reason = reason_bytes[:4096].decode("utf-8", errors="ignore") + "…（已裁剪）"
        with d.transaction() as conn:
            src = conn.execute(
                "SELECT question_id,candidate_id,action_cycle,candidate_set_hash,selection_key,"
                "policy_hash,license_review_id,baseline_id FROM external_import "
                "WHERE id=? AND action='selected_for_materialization'", (ei_id,)).fetchone()
            if src is None or src[0] != qi or src[1] != cand_id or src[7] is None:
                raise RuntimeError(f"external_import {ei_id} selection 绑定损坏")
            identity = tuple(src[:7])
            terminal = conn.execute(
                "SELECT id,action,reason_json FROM external_import WHERE question_id=? AND candidate_id=? "
                "AND action_cycle=? AND candidate_set_hash=? AND selection_key=? AND policy_hash=? "
                "AND action IN ('imported','materialize_failed','superseded') ORDER BY id",
                identity[:6]).fetchall()
            if len(terminal) > 1 or (terminal and terminal[0][1] != "materialize_failed"):
                raise RuntimeError(
                    f"external_import {ei_id} 已有冲突/重复终局: "
                    f"{[(row[0], row[1]) for row in terminal]}")
            if terminal:
                failed_event_id = terminal[0][0]
                durable_payload = json.loads(terminal[0][2] or "{}")
                if not isinstance(durable_payload, dict):
                    raise RuntimeError(f"materialize_failed event {failed_event_id} reason_json 非 object")
                durable_reason = durable_payload.get("reason") or reason
            else:
                failed_event_id = conn.execute(
                    "INSERT INTO external_import(question_id,candidate_id,action,action_cycle,"
                    "candidate_set_hash,selection_key,policy_hash,license_review_id,reason_json) "
                    "VALUES (?,?,'materialize_failed',?,?,?,?,?,?)",
                    (*identity, json.dumps({"reason": reason}, ensure_ascii=False))).lastrowid
                durable_reason = reason
            baseline_status = conn.execute(
                "SELECT status FROM baseline WHERE id=?", (src[7],)).fetchone()
            if baseline_status is None or baseline_status[0] not in (
                    "planned", "building", "build_failed"):
                raise RuntimeError(
                    f"materialize_failed selection {ei_id} baseline {src[7]} 状态非法: "
                    f"{baseline_status[0] if baseline_status else 'missing'}")
            conn.execute(
                "UPDATE baseline SET status='build_failed' WHERE id=? AND status IN ('planned','building')",
                (src[7],))
            deps = conn.execute(
                "SELECT id,status FROM question_dep WHERE question_id=? AND dep_type='baseline' "
                "AND depends_on_baseline_id=? ORDER BY id", (qi, src[7])).fetchall()
            if len(deps) != 1 or deps[0][1] not in ("pending", "blocked"):
                raise RuntimeError(
                    f"materialize_failed selection {ei_id} exact dep 非 pending/blocked 唯一态: {deps}")
            decision = conn.execute(
                "SELECT id FROM decision WHERE actor='orchestrator' "
                "AND type='import_materialization_blocked' AND json_valid(payload_json) "
                "AND json_extract(payload_json,'$.source_external_import_id')=? "
                "AND json_extract(payload_json,'$.materialize_failed_event_id')=? ORDER BY id",
                (ei_id, failed_event_id)).fetchall()
            if len(decision) > 1:
                raise RuntimeError(f"external_import {ei_id} 有重复 blocked decision")
            if not decision:
                conn.execute(
                    "INSERT INTO decision(cycle_id,question_id,actor,type,payload_json) "
                    "VALUES (?,?,'orchestrator','import_materialization_blocked',?)",
                    (src[2], qi, json.dumps({
                        "source_external_import_id": ei_id,
                        "materialize_failed_event_id": failed_event_id,
                        "baseline_id": src[7], "reason": durable_reason,
                    }, ensure_ascii=False, sort_keys=True)))
            conn.execute(
                "UPDATE question_dep SET status='blocked' WHERE id=? AND status='pending'",
                (deps[0][0],))

    def _settled_ok(self, ei_id) -> bool:
        return self._has_selection_outcome(ei_id, ("imported",))

    def _eval_log_backfill(self, cyc_id: str, log_path: Path, aid: Optional[int]) -> None:
        """attempt-owned eval log 补登+ingest（幂等 + sha256 锚强校验——同 attack_stages 契约）。"""
        if aid is None or not log_path.exists():
            return
        exp = self.state.daemon.query_one("SELECT artifact_ref FROM evaluation_attempt WHERE id=?", (aid,))
        if not exp or not exp[0] or not exp[0].startswith("sha256:"):
            raise RuntimeError(f"attempt {aid} 无 sha256: artifact_ref 锚——拒绝从 staging 补登")
        data = read_artifact_bytes(
            log_path, expected_hash=exp[0], label="import eval log backfill")
        got = hashlib.sha256(data).hexdigest()
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
