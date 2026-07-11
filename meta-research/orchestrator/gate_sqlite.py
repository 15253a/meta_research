"""SqliteGate —— Gate 三级校验 + 门禁的真实现（M1a-Gate：写路径落真 DB，经 WriteDaemon）。

对齐《第一部分》§4.1（唯一写库路径 / 三级校验 / gate_close_question）+《第二部分》§6.13(2)（gate_input 隔离）。
与 M0 StubGate 并存、不替换它（M0 driver 仍用 StubGate，基线保持绿）；端到端切真 loop 归 M3 Advancer。

三级校验（§4.1.1）：① JSON schema ② 引用完整性（所引 id 在真 DB 存在）③ 业务门禁（不变量 I1–I6）。
**gate_input 隔离（§6.13(2)，机制非约定）**：门禁判据经一条**受限只读连接** + SQLite authorizer 取数，
authorizer 对「日志 / 人机 / 外部检索」9 表的 SELECT 一律 DENY（含其派生视图 v_metric_result_trajectory
——底表 execution_log 被拒即自动拒）。写路径（answer/evidence/…）走 WriteDaemon 写连接（无 authorizer，
它须能写全表；两条连接分工，§6.6）。

范围（CP2.3）：authorizer 隔离 + 三级校验框架 + **gate_close_question**（关问业务门禁）。
池注册 gate_*（§4.1.4 其余）= M4 gate_exec/gate_pool。
`gate_close_question` 的 **parser_result_suspect** 判据（§4.3.1）自 M4 CP5.3 起接真：构造时传
`parser_suspect=lambda aid: obs_parser.suspect_for_attempt(普通只读连接, aid, policy['observation'])`
——gate SQL 仍不可 SELECT 观测表（authorizer 拒），负向过滤豁免仅经该派生谓词；None = 不查（M0–M3 无真观测）。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from .ids import decode_optional
from .interfaces import Artifact, ValidationResult
from .schemas import ARTIFACT_SCHEMA_MAP, SchemaSet
from .writedaemon import WriteDaemon

# 「日志 / 人机 / 外部检索」三类表：门禁判据禁读（§6.13(2) 逐表列名，不靠前缀通配）
GATE_DENY_TABLES = frozenset({
    "execution_log", "execution_observation",
    "interaction_message", "interaction_classification", "interaction_reply", "interaction_request",
    "external_candidate", "external_import", "license_review",
})


def gate_authorizer(action, arg1, arg2, arg3, arg4):
    """SQLite authorizer：对 9 禁表的 SELECT(SQLITE_READ) 一律 DENY，其余放行。

    v_metric_result_trajectory（底表含 execution_log）读它时会触发对 execution_log 列的 SQLITE_READ →
    自动被拒（无需单列该视图名）。"""
    if action == sqlite3.SQLITE_READ and arg1 in GATE_DENY_TABLES:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


# gate_input_* 只读视图（§6.13(2)②）：门禁判据**只从这些视图取数**——依赖闭包**不含**任何禁表
# （test_gate_isolation 的闭包测试 = §6.13(2)③）。建为 TEMP VIEW：per-连接、落 sqlite_temp_master，
# 不进冻结主 schema 的 36/72/29/1 计数。形状按 gate_close_question 判据裁剪（非 SELECT-*，减少暴露面）。
GATE_INPUT_VIEWS = """
CREATE TEMP VIEW gate_input_cycle AS
  SELECT id, status, goal_id, goal_ver, active_question_id FROM cycle;
CREATE TEMP VIEW gate_input_goal_current AS
  SELECT id AS goal_id, MAX(version) AS goal_ver FROM goal GROUP BY id;
CREATE TEMP VIEW gate_input_question AS SELECT id, status, goal_id, goal_ver FROM question;
CREATE TEMP VIEW gate_input_measurement AS
  SELECT mr.id AS metric_result_id, mr.evaluation_id, mr.evaluation_attempt_id, mr.metric_id, mr.metric_ver, mr.scope,
         e.status AS eval_status, ea.status AS attempt_status,
         COALESCE(ea.build_target_id, e.build_target_id) AS build_target_id
  FROM metric_result mr JOIN evaluation e ON e.id = mr.evaluation_id
       JOIN evaluation_attempt ea ON ea.id = mr.evaluation_attempt_id;
CREATE TEMP VIEW gate_input_build_target AS SELECT id, status FROM build_target;
CREATE TEMP VIEW gate_input_child_answer AS SELECT id AS answer_id, question_id, goal_id, goal_ver FROM answer;
CREATE TEMP VIEW gate_input_applicability AS SELECT answer_id, goal_id, goal_ver, status FROM answer_applicability;
CREATE TEMP VIEW gate_input_decision_human AS SELECT id FROM decision WHERE actor = 'human';
"""
GATE_INPUT_VIEW_NAMES = ("gate_input_cycle", "gate_input_goal_current", "gate_input_question", "gate_input_measurement", "gate_input_build_target",
                         "gate_input_child_answer", "gate_input_applicability", "gate_input_decision_human")


def open_gate_read_conn(path: str) -> sqlite3.Connection:
    """开一条门禁**受限只读连接**：`mode=ro` URI 打开（主库物理只读、不可翻回可写，护单写路径 P1，
    比 PRAGMA query_only 强）→ 建 gate_input_* TEMP 视图（temp schema 可写、不受 mode=ro 限）→ 装
    authorizer 拒 9 禁表 SELECT。

    须与写连接指向同一**已建**文件库（:memory: 每连接独立库，故门禁读连接测试须用文件库）。
    顺序：authorizer 在建视图后装（视图创建需读 schema）；此后本连接自始至终禁读 9 表、且物理不可写。
    """
    conn = sqlite3.connect(f"file:{quote(path)}?mode=ro", uri=True)   # quote 护含 ?/#/% 的路径
    conn.executescript(GATE_INPUT_VIEWS)
    conn.set_authorizer(gate_authorizer)
    return conn


def _num(s: Any, prefix: str) -> Optional[int]:
    """解码前缀 id（mr/q/d/ev…）→ SQLite 正整数；任何格式/越界均返回 None。"""
    return decode_optional(s, prefix)


class GateReject(Exception):
    """业务门禁拒绝（记 DECISION(actor=gate,type=reject)，§4.1.1）。"""


class GateInvariantError(RuntimeError):
    """Gate 写路径命中非预期 DB 约束：视为实现/状态损毁，不得洗成外部产物业务拒绝。"""


# 这些 RAISE(ABORT) 是 Gate 所有的 I3 最终焊死层：即使前置给出的可行动拒因漏了
# TOCTOU/跨版细节，仍属 answer/evidence 业务不成立。其他 IntegrityError（FK/CHECK/未知触发器）
# 说明 Gate 自身写入形状或 DB 状态异常，必须 GateInvariantError fail loud。
_EXPECTED_I3_INTEGRITY_MARKERS = (
    "I3:", "answer 的 goal 版本", "evidence question_id", "evidence 的 goal 版本",
    "evaluation 证据", "evidence.evaluation_attempt_id", "child_answer", "human 证据",
)


class SqliteGate:
    def __init__(self, daemon: WriteDaemon, read_conn: sqlite3.Connection, schema_set: SchemaSet,
                 parser_suspect=None):
        self.daemon = daemon            # 写路径（无 authorizer）
        self.read = read_conn           # 门禁判据读路径（有 authorizer，禁 9 表）
        self.schemas = schema_set
        # parser_result_suspect(attempt_id)->0/1（§4.3.1 派生谓词，M4 CP5.3 起接真：obs_parser.suspect_for_attempt
        # + 独立普通只读连接——gate SQL 仍不可 SELECT 观测表，负向过滤豁免仅经此谓词）。None = 不查（M2/M3 行为）。
        self.parser_suspect = parser_suspect

    # -- 三级校验：① schema ----------------------------------------------------
    def _level1_schema(self, artifact: Artifact) -> List[str]:
        errors: List[str] = []
        if not artifact.files:
            return ["产物为空（files 无内容）"]
        for filename, payload in artifact.files.items():
            if filename == "resource_request.json":
                errors.append("resource_request.json: sidecar 非 Gate 产物（§6.11）——驱动器须在 commit 前摘出")
                continue
            if filename not in ARTIFACT_SCHEMA_MAP:
                errors.append(f"{filename}: 未知产物文件名")
                continue
            v = self.schemas.validator_for_artifact(filename)
            for e in v.iter_errors(payload):
                errors.append(f"{filename}: {e.json_path} {e.message}")
                for sub in (e.context or []):     # 展平 oneOf 子错误（否则工人拿不到具体键名）
                    errors.append(f"{filename}: {sub.json_path} {sub.message}")
        return errors

    def preview(self, artifact: Artifact) -> ValidationResult:
        """MCP 只读预检：schema + 引用完整性；不写库、结论不被信任（commit 时重校验，§4.1.3）。"""
        errors = self._level1_schema(artifact)
        if not errors and "answer.json" in artifact.files:
            errors = self._preview_answer_refs(artifact.files["answer.json"])
        return ValidationResult(ok=not errors, errors=errors)

    def _preview_answer_refs(self, ans: Dict[str, Any]) -> List[str]:
        """引用完整性预检（读路径）：answer.question_id / evidence 各 ref 在真 DB 存在。"""
        errors: List[str] = []
        if self._q1("SELECT 1 FROM gate_input_question WHERE id=?", (_num(ans["question_id"], "q"),)) is None:
            errors.append(f"answer.question_id 不存在: {ans['question_id']}")
        for ev in ans["evidence"]:
            errors.extend(self._evidence_ref_errors(ev))
        return errors

    # -- 读路径（受限只读连接） ------------------------------------------------
    def _q1(self, sql: str, params=()):
        return self.read.execute(sql, params).fetchone()

    # -- gate_close_question（§4.1.4）------------------------------------------
    def gate_close_question(self, *, cycle_id: str, question_id: str, verdict: str,
                            evidence: List[Dict[str, Any]], answer_md: str) -> str:
        """关问业务门禁：校验 I3（读走受限连接）→ 写 answer+evidence+question 迁移（写走 WriteDaemon 单事务）。

        返回 answer id（"a<n>"）。校验不过抛 GateReject（含拒因），并记 DECISION(actor=gate,type=reject)。
        DB 触发器（trg_q_i3 / trg_evidence_* / trg_answer_goalver）是最终焊死层，本函数是可行动错误 + 触发器
        未覆盖的 gate 级判据（target_complete / applicability 同版负向）的前置门禁。
        """
        rejected = None
        aid = None
        with self.daemon.transaction() as conn:
            try:
                aid = self.gate_close_question_in_txn(
                    conn, cycle_id=cycle_id, question_id=question_id, verdict=verdict,
                    evidence=evidence, answer_md=answer_md)
            except GateReject as error:
                # in-txn 入口已在同一事务写 gate reject；在 with 内吞到提交边，
                # 提交审计后再向调用方抛业务拒绝。
                rejected = error
        if rejected is not None:
            raise rejected
        return aid

    def gate_close_question_in_txn(
            self, conn: sqlite3.Connection, *, cycle_id: str, question_id: str,
            verdict: str, evidence: List[Dict[str, Any]], answer_md: str) -> str:
        """在调用方现有 WriteDaemon 事务内提交关问，供 reasoning 全序原子提交。

        业务拒绝会在同一外层事务写 ``decision(gate,reject)`` 后抛 ``GateReject``；调用方必须在
        外层 ``with transaction`` **内部**接住它并完成失败收尾，才能让审计随失败终态一起提交。
        Gate 自己的写段用 SAVEPOINT 隔离，已知 I3 触发器拒绝不会留下 answer/evidence 半写。
        """
        if not self.daemon.owns_active_transaction(conn):
            raise GateInvariantError("gate_close_question_in_txn 须使用本 WriteDaemon 的 active transaction")

        qi = _num(question_id, "q")
        ci = _num(cycle_id, "c")
        rej = lambda msg: self._reject(  # 顶层拒统一带原始 question_id
            cycle_id, qi, msg, question_id_raw=question_id, conn=conn)
        crow = self._q1(
            "SELECT status,goal_id,goal_ver,active_question_id FROM gate_input_cycle WHERE id=?",
            (ci,)) if ci is not None else None
        if ci is None or crow is None:
            rej(f"cycle 不存在/非法: {cycle_id!r}")   # 预校验，避免 answer.cycle_id FK 撞裸约束错
        qrow = self._q1("SELECT status, goal_id, goal_ver FROM gate_input_question WHERE id=?", (qi,)) if qi else None
        if qrow is None:
            rej(f"question 不存在: {question_id}")
        status, goal_id, goal_ver = qrow
        if status in ("answered", "refuted", "dead_end"):
            rej(f"question 已终态（{status}），不可关")
        current = self._q1(
            "SELECT goal_ver FROM gate_input_goal_current WHERE goal_id=?", (goal_id,))
        if (crow[0] in ("done", "failed", "aborted") or crow[3] != qi
                or status != "active" or (crow[1], crow[2]) != (goal_id, goal_ver)
                or current is None or current[0] != goal_ver):
            rej(
                "关问 lineage 非法：须 current 非终态 cycle 的 active Qn，且 cycle/question goal 完全一致")
        if verdict not in ("answered", "refuted"):
            rej(f"verdict 非法: {verdict}")
        if not evidence:
            rej("I3：关问需 ≥1 条证据")

        resolved = []   # 逐条证据形状/引用/非成功测量校验（触发器兜底项）通过后的落库参数
        for ev in evidence:
            resolved.append(self._validate_evidence(cycle_id, qi, ev, reject_conn=conn))

        # 写：先在写锁内**重跑 gate-only 不变量**（target_complete / applicability 同版负向——无触发器兜底，
        # §4.1.3「提交在事务内重新跑校验」；BEGIN IMMEDIATE 持写锁 → 期间无并发提交 → self.read 见冻结的
        # 已提交态，杜绝校验↔写入间被别的写事务改状态的 TOCTOU）→ 再写 answer+evidence+迁移+resolve dep。
        # except IntegrityError：触发器/CHECK 焊死的 I3 分支（child 子树/子问题未关/跨版）撞 ABORT → 转干净拒
        # （只收约束类，database-locked / IO 等基础设施错原样抛，不误记为 gate reject）。
        gate_err = self._lineage_violation(conn, ci, qi)
        if gate_err is None:
            gate_err = self._gate_only_violation(resolved, goal_id, goal_ver, conn=conn)
        if gate_err is not None:
            rej(gate_err)

        savepoint = "gate_close_question_write"
        conn.execute(f"SAVEPOINT {savepoint}")
        try:
            aid = conn.execute(
                "INSERT INTO answer(question_id,goal_id,goal_ver,cycle_id,verdict,answer_md) VALUES (?,?,?,?,?,?)",
                (qi, goal_id, goal_ver, ci, verdict, answer_md)).lastrowid
            for r in resolved:
                self._insert_evidence(conn, aid, qi, goal_id, goal_ver, r)
            conn.execute("UPDATE question SET status=?, closed_cycle=? WHERE id=?", (verdict, ci, qi))
            # 关问即释放本轮 active-question 租约；answer/cycle_id 保留历史锚。
            released = conn.execute(
                "UPDATE cycle SET active_question_id=NULL WHERE id=? AND active_question_id=?",
                (ci, qi)).rowcount
            if released != 1:
                raise GateInvariantError(
                    f"关问提交时 active_question 租约丢失: cycle={cycle_id}, question={question_id}")
            conn.execute(
                "UPDATE question_dep SET status='satisfied' WHERE status='pending' AND dep_type='question' "
                "AND depends_on_question_id=?", (qi,))
        except sqlite3.IntegrityError as error:
            conn.execute(f"ROLLBACK TO {savepoint}")
            conn.execute(f"RELEASE {savepoint}")
            detail = str(error)
            if any(marker in detail for marker in _EXPECTED_I3_INTEGRITY_MARKERS):
                self._reject(
                    cycle_id, qi, f"DB 不变量拒绝: {error}",
                    question_id_raw=question_id, conn=conn)
            raise GateInvariantError(
                f"gate_close_question 命中非预期 DB 约束: {error}") from error
        except BaseException:
            conn.execute(f"ROLLBACK TO {savepoint}")
            conn.execute(f"RELEASE {savepoint}")
            raise
        else:
            conn.execute(f"RELEASE {savepoint}")
        return f"a{aid}"

    @staticmethod
    def _lineage_violation(conn, cycle_id: int, question_id: int) -> Optional[str]:
        """写锁内重核 current cycle↔active question lineage；旧 cycle 不得组合产生新版 answer。"""
        cycle = conn.execute(
            "SELECT status,goal_id,goal_ver,active_question_id FROM cycle WHERE id=?",
            (cycle_id,)).fetchone()
        question = conn.execute(
            "SELECT status,goal_id,goal_ver FROM question WHERE id=?", (question_id,)).fetchone()
        if cycle is None or question is None:
            return "cycle/question 在提交前消失"
        current = conn.execute(
            "SELECT MAX(version) FROM goal WHERE id=?", (cycle[1],)).fetchone()
        if (cycle[0] in ("done", "failed", "aborted") or cycle[3] != question_id
                or question[0] != "active" or tuple(cycle[1:3]) != tuple(question[1:3])
                or current is None or current[0] != cycle[2]):
            return ("关问 lineage 非法：cycle 必须非终态并指向 active question，"
                    "且二者绑定同一 current goal version")
        return None

    def _gate_only_violation(self, resolved: List[Dict[str, Any]], goal_id: int, goal_ver: int,
                             *, conn: Optional[sqlite3.Connection] = None) -> Optional[str]:
        """无触发器兜底的 gate-only 不变量（写锁内重跑，TOCTOU-safe）：target_complete + applicability 同版负向。
        经 self.read（受限连接 + gate_input 视图）取数；返回首个违规拒因或 None。"""
        for r in resolved:
            if r["kind"] == "evaluation" and self.parser_suspect is not None \
               and r.get("evaluation_attempt_id") is not None \
               and self.parser_suspect(r["evaluation_attempt_id"]):
                # §4.1.4/§4.3.1：证据 attempt 被 parser 派生标存疑 → 拒（负向过滤：只挡引用、不支持结论）
                return f"evidence attempt {r['evaluation_attempt_id']} 被 parser_result_suspect 标存疑，不可作证据"
            if r["kind"] == "evaluation" and r.get("build_target_id") is not None:
                bt = (conn.execute("SELECT status FROM build_target WHERE id=?", (r["build_target_id"],)).fetchone()
                      if conn is not None else
                      self._q1("SELECT status FROM gate_input_build_target WHERE id=?", (r["build_target_id"],)))
                if bt is None or bt[0] != "complete":
                    return f"evaluation 证据未过 target_complete（build_target={bt[0] if bt else '缺'}）"
            elif r["kind"] == "child_answer":
                params = (r["child_answer_id"], goal_id, goal_ver)
                app = (conn.execute(
                    "SELECT status FROM answer_applicability WHERE answer_id=? AND goal_id=? AND goal_ver=?",
                    params).fetchone() if conn is not None else self._q1(
                    "SELECT status FROM gate_input_applicability WHERE answer_id=? AND goal_id=? AND goal_ver=?",
                    params))
                if app is not None and app[0] != "still_applicable":
                    return f"child_answer 所指 answer 在当前 goal_ver applicability={app[0]}（非 still_applicable）"
        return None

    def _reject(self, cycle_id, qi, msg: str, question_id_raw=None,
                conn: Optional[sqlite3.Connection] = None):
        # cycle_id / question_id 都可能是不存在的引用（拒因正是「不存在」）——decision 的 FK 列写 NULL、
        # 原始 attempted 值入 payload，否则 _reject 自身 INSERT 会撞 FK、把「干净拒 + 记 DECISION」搞砸。
        # attempted_question 记**原始**入参串（顶层传 question_id_raw；内部校验期 qi 已确知存在→回编 q{qi}）。
        ci = _num(cycle_id, "c")
        att_q = question_id_raw if question_id_raw is not None else (f"q{qi}" if qi is not None else None)
        def record(target_conn):
            cref = ci if ci is not None and target_conn.execute(
                "SELECT 1 FROM cycle WHERE id=?", (ci,)).fetchone() else None
            qref = qi if qi is not None and target_conn.execute(
                "SELECT 1 FROM question WHERE id=?", (qi,)).fetchone() else None
            target_conn.execute(
                "INSERT INTO decision(cycle_id,question_id,actor,type,payload_json) "
                "VALUES (?,?,'gate','reject',?)",
                (cref, qref, json.dumps({"reason": msg, "attempted_cycle": cycle_id,
                                        "attempted_question": att_q})))
        if conn is None:
            with self.daemon.transaction() as owned_conn:
                record(owned_conn)
        else:
            if not self.daemon.owns_active_transaction(conn):
                raise GateInvariantError("gate reject 审计须使用本 WriteDaemon 的 active transaction")
            record(conn)
        raise GateReject(msg)

    def _evidence_ref_errors(self, ev: Dict[str, Any]) -> List[str]:
        """引用完整性（不含业务判据）：证据所指 id 在真 DB 存在。"""
        kind = ev.get("kind")
        if kind == "evaluation":
            if self._q1("SELECT 1 FROM gate_input_measurement WHERE metric_result_id=?", (_num(ev.get("metric_result_id"), "mr"),)) is None:
                return [f"evidence.metric_result_id 不存在: {ev.get('metric_result_id')}"]
        elif kind == "child_answer":
            if self._q1("SELECT 1 FROM gate_input_question WHERE id=?", (_num(ev.get("child_question_id"), "q"),)) is None:
                return [f"evidence.child_question_id 不存在: {ev.get('child_question_id')}"]
        elif kind == "human":
            if self._q1("SELECT 1 FROM gate_input_decision_human WHERE id=?", (_num(ev.get("human_ref"), "d"),)) is None:
                return [f"evidence.human_ref 无 actor=human 的 decision: {ev.get('human_ref')}"]
        return []

    def _validate_evidence(self, cycle_id, qi, ev: Dict[str, Any],
                           *, reject_conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
        """单条证据的形状 + 引用 + 触发器兜底项（非成功测量）预检；解析落库字段。
        target_complete / applicability 同版负向（无触发器兜底）在写锁内 _gate_only_violation 重跑，不在此。"""
        # 形状再校验（§4.1.3：commit 不信任上游预检）——kind 合法 + 该态必需键在（缺则干净拒、非 KeyError）
        required = {"evaluation": "metric_result_id", "literature": "citation_md",
                    "child_answer": "child_question_id", "human": "human_ref"}
        kind = ev.get("kind")
        if kind not in required:
            self._reject(cycle_id, qi, f"evidence.kind 非法: {kind!r}", conn=reject_conn)
        if not ev.get(required[kind]):
            self._reject(cycle_id, qi, f"evidence({kind}) 缺必需键 {required[kind]}", conn=reject_conn)
        referr = self._evidence_ref_errors(ev)
        if referr:
            self._reject(cycle_id, qi, referr[0], conn=reject_conn)
        if kind == "evaluation":
            r = self._validate_eval_evidence(cycle_id, qi, ev, reject_conn=reject_conn)
        elif kind == "child_answer":
            r = self._validate_child_evidence(cycle_id, qi, ev, reject_conn=reject_conn)
        elif kind == "literature":
            r = {"kind": "literature", "literature_ref": ev["citation_md"]}
        else:
            r = {"kind": "human", "human_decision_id": _num(ev["human_ref"], "d")}
        r["claim_md"] = ev.get("note_md") or "(见 answer 正文)"   # 证据支持的论断（note 可选；evidence.claim_md NOT NULL）
        return r

    def _validate_eval_evidence(self, cycle_id, qi, ev: Dict[str, Any],
                                *, reject_conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
        mrid = _num(ev["metric_result_id"], "mr")
        row = self._q1(
            "SELECT evaluation_id, evaluation_attempt_id, metric_id, metric_ver, scope, "
            "eval_status, attempt_status, build_target_id FROM gate_input_measurement WHERE metric_result_id=?", (mrid,))
        eval_id, att_id, metric_id, metric_ver, scope, e_st, ea_st, bt_id = row
        if e_st != "success" or ea_st != "success":    # 触发器 trg_evidence_eval_valid 兜底；此处给可行动错误
            self._reject(cycle_id, qi, f"evaluation 证据非成功测量（eval={e_st}, attempt={ea_st}）",
                         conn=reject_conn)
        # target_complete / parser_result_suspect(M4) 不在此——前者移到 _gate_only_violation（写锁内重跑，TOCTOU-safe）。
        # build_target_id 带回，供 _gate_only_violation 用（NULL 如 standalone_eval / M4 import 来源 → 无 target 可谈）。
        return {"kind": "evaluation", "evaluation_id": eval_id, "evaluation_attempt_id": att_id,
                "metric_result_id": mrid, "metric_id": metric_id, "metric_ver": metric_ver, "scope": scope,
                "build_target_id": bt_id}

    def _validate_child_evidence(self, cycle_id, qi, ev: Dict[str, Any],
                                 *, reject_conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
        cq = _num(ev["child_question_id"], "q")
        # 子问题的 answer（一问一 goal_ver 一 answer）；取最新版本的 answer。applicability 同版负向不在此——
        # 移到 _gate_only_violation（写锁内重跑，无触发器兜底、须 TOCTOU-safe）。
        # ⚠️ M3 注记：多版本（goal-amend）下最新 answer 版本可能 ≠ 父当前 goal_ver → 触发器 trg_evidence_child
        # 跨版分支要求 still_applicable；本轮单版本不触发，跨版则由写路径 except 兜底为干净拒（见 gate_close_question）。
        arow = self._q1("SELECT answer_id FROM gate_input_child_answer WHERE question_id=? ORDER BY goal_ver DESC LIMIT 1", (cq,))
        if arow is None:
            self._reject(cycle_id, qi, f"child_answer 子问题无 answer: {ev['child_question_id']}",
                         conn=reject_conn)
        return {"kind": "child_answer", "child_answer_id": arow[0]}

    def _insert_evidence(self, conn, aid, qi, goal_id, goal_ver, r: Dict[str, Any]) -> None:
        kind = r["kind"]
        base = "INSERT INTO evidence(answer_id,question_id,goal_id,goal_ver,kind,claim_md,{cols}) VALUES (?,?,?,?,?,?,{vals})"
        common = (aid, qi, goal_id, goal_ver, kind, r.get("claim_md", ""))
        if kind == "evaluation":
            conn.execute(base.format(cols="evaluation_id,evaluation_attempt_id,metric_result_id,metric_id,metric_ver,scope",
                                     vals="?,?,?,?,?,?"),
                         common + (r["evaluation_id"], r["evaluation_attempt_id"], r["metric_result_id"],
                                   r["metric_id"], r["metric_ver"], r["scope"]))
        elif kind == "child_answer":
            conn.execute(base.format(cols="child_answer_id", vals="?"), common + (r["child_answer_id"],))
        elif kind == "literature":
            conn.execute(base.format(cols="literature_ref", vals="?"), common + (r["literature_ref"],))
        else:   # human
            conn.execute(base.format(cols="human_decision_id", vals="?"), common + (r["human_decision_id"],))
