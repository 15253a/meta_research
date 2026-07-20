"""SQLiteStateStore —— StateStore 的真实现（M1b：落 SQLite，经 WriteDaemon 单写连接）。

对齐《第一部分》§4.2 状态机 / 调度可见性 / 写函数拒绝判据，语义与 M0 `InMemoryStateStore` **等价**
（本文件逐方法对照后者，便于零上下文读者 diff 核对），差别只在真相落 DB、写走 WriteDaemon 短事务。

范围（CP2.2 = M1b）：纯状态机——cycle 生命周期 / question 树七 op / dep / 调度 / route / selection。
**不含 `close_question`**：关问写 answer+evidence+I3 = `gate_close_question`（业务门禁，落 CP2.3；且其证据须
引用池注册产生的真 evaluation/metric_result，CP2.4 才有）——本类 close_question 抛 NotImplementedError 指路。

原子性（§4.2.5，M1b 验收核心）：`apply_tree_ops` 整批在**单一事务**内（add_children = 写子问题 +
父 active→open 释放 + 逐子 question_dep(pending) + decompose DECISION 同一事务）——任一步抛异常整体
回滚、kill-9 后无「子问题已写但父未释放」半写。`atomic()` 供 M3 Advancer 把多方法（答题收尾 +
apply_tree_ops + persist_selection + mark_cycle_done）裹进同一事务（§4.2.5(a) 全序）；单独调用时各自短事务。

ID 表示：对外沿用 M0 前缀串（`c<n>`/`q<n>`/`a<n>`/`g<n>`），内部映射到各表 INTEGER 主键——保持与
driver / Gate 的既有契约一致，未来 InMemory→SQLite 可透明替换。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from .ids import decode as _bounded_decode, decode_optional as _bounded_decode_optional
from .interfaces import InvalidSelectionError, Cycle, Route, Selection
from .budgeting import compute_budget
from .question_progress import QuestionProgressError, append_inconclusive_event
from .question_admission import admission_payload, normalize_question_contract
from .import_authority import (
    authority_hash,
    build_human_named_authority,
    build_question_request_binding,
    build_reference_authority,
    canonical_bytes,
)
from .writedaemon import WriteDaemon

_TERMINAL_Q = {"answered", "refuted", "dead_end"}
_ROUTES = ("bootstrap", "attack", "decompose", "reuse_only", "eval_only", "goal_amend", "dependency_wait")
_SPAWN_SOURCE = {  # §4.2.4 spawn_question kind → question.source（对齐 DDL 枚举）
    "diagnosis": "agent", "followup": "agent", "revalidate": "revalidate",
    "import_reference": "agent", "goal_retarget": "goal_amend",
}
_REQUEST_REF_RE = re.compile(r"^db:(directive|decision):([1-9][0-9]*)$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_QREF_RE = re.compile(r"^q[1-9][0-9]*$")


def _cid(n: int) -> str: return f"c{n}"
def _qid(n: int) -> str: return f"q{n}"
def _aid(n: int) -> str: return f"a{n}"


def _decode(s: Any, prefix: str) -> int:
    """按类型前缀（c/q/a/g）解码为整型主键，**校验前缀**——防类型错 id（如把 'c1' 当问题）静默命中
    别的表的同号行。同时经 ids.decode 限制为 SQLite 正整数，外部超长 id 只产生可分类
    ValueError，不会在 int/SQLite 边界泄出 OverflowError。"""
    return _bounded_decode(s, prefix)


def _cnum(s: Any) -> int: return _decode(s, "c")
def _qnum(s: Any) -> int: return _decode(s, "q")
def _anum(s: Any) -> int: return _decode(s, "a")


def _qnum_opt(s: Any) -> Optional[int]:
    """问题 id 安全解码：形如 q<数字> 才转 int，否则 None——供 selection 处理可能是未解析 local_key /
    畸形 / 类型错 id 的入参（给干净「不存在」拒因，而非裸 ValueError）。"""
    return _bounded_decode_optional(s, "q")


class SQLiteStateStore:
    def __init__(self, daemon: WriteDaemon, policy: Dict[str, Any]):
        self.daemon = daemon
        self.policy = policy
        self._policy_version = str(policy.get("policy_version", "v0"))
        self._active_conn = None                       # 非 None = 处于 atomic() 外层事务中
        self._bundle_cursor: Dict[str, Optional[str]] = {}
        # 同批 local_key→qid（§6.10：StateStore 同事务内解析）；per-cycle 累积，跨 apply_tree_ops→
        # persist_selection 存活（进程内；跨重启的耐久是 M3 恢复主题，非 M1b 验收）。
        self._local_maps: Dict[str, Dict[str, str]] = {}

    # -- 事务 / 读写原语 --------------------------------------------------------
    # 进程内投影（_local_maps / _bundle_cursor）随 DB 事务同生共死：DB 回滚而投影不回滚 = 在
    # 「本检查点要焊死的那一层」留半写（SQLite 复用回滚 rowid → 陈旧 local_key 静默错绑到别的问题）。
    # 故凡开写事务处（atomic() 外层 / 独立 apply_tree_ops）都先快照、异常回滚时复原（对齐
    # InMemoryStateStore._tree_snapshot/_tree_restore）。写计数（answer_review）走 DB decision 计数，
    # 随事务天然回滚，无需在此快照。
    def _projection_snapshot(self):
        return ({k: dict(v) for k, v in self._local_maps.items()},
                dict(self._bundle_cursor))

    def _projection_restore(self, snap) -> None:
        self._local_maps, self._bundle_cursor = snap

    @contextmanager
    def atomic(self) -> Iterator[Any]:
        """把多个写方法裹进同一事务（M3 Advancer 用于答题收尾全序，§4.2.5(a)）。

        **契约**：块内任一写方法抛异常，调用方**不得吞掉**——必须让它传播以中止整个 atomic
        （否则该方法的半途 DB 写入会随本事务一起提交、且投影不复原）。M1b 无跨方法回滚的 savepoint
        （方法级独立回滚留作后续硬化）；M3 Advancer 遵此契约：任一步失败即整轮 advance 失败。
        """
        snap = self._projection_snapshot()
        try:
            with self.daemon.transaction() as conn:
                self._active_conn = conn
                yield conn
        except BaseException:
            self._projection_restore(snap)   # 随外层事务回滚复原投影
            raise
        finally:
            self._active_conn = None

    @contextmanager
    def _write(self) -> Iterator[Any]:
        """一个写作用域：若在 atomic() 内则复用外层事务连接（投影由 atomic 统一快照/复原），
        否则自开一个短事务并在异常时复原本次投影改动。"""
        if self._active_conn is not None:
            yield self._active_conn
        else:
            snap = self._projection_snapshot()
            try:
                with self.daemon.transaction() as conn:
                    yield conn
            except BaseException:
                self._projection_restore(snap)
                raise

    def _rconn(self):
        """读连接。**不变量**：daemon 只有一条连接——独立写事务期间它就是当前事务连接，故这些读
        本就见未提交写（decompose 先读父状态再写子问题所必需）；事务外见已提交态。atomic() 内则显式
        取 _active_conn（同一条连接）。将来若拆出独立只读连接，须重审 _apply_ops 的批内读。"""
        return self._active_conn if self._active_conn is not None else self.daemon.conn

    def _q1(self, sql: str, params=()):
        return self._rconn().execute(sql, params).fetchone()

    def _qall(self, sql: str, params=()):
        return self._rconn().execute(sql, params).fetchall()

    # -- goal / cycle -----------------------------------------------------------
    def create_goal(self, *, text: str, predicate_json: Dict[str, Any]) -> str:
        if self._q1("SELECT 1 FROM goal LIMIT 1"):
            raise ValueError("goal 已存在（后续演化走 goal_amend）")
        with self._write() as conn:
            conn.execute("INSERT INTO goal(id,version,text,predicate_json) VALUES (1,1,?,?)",
                         (text, json.dumps(predicate_json, ensure_ascii=False)))
            conn.execute("INSERT INTO decision(actor,type,payload_json) VALUES ('orchestrator','goal_bootstrap',?)",
                         (json.dumps({"goal_id": "g1"}),))
        return "g1"

    def _goal_ver(self) -> int:
        row = self._q1("SELECT max(version) FROM goal WHERE id=1")
        return row[0] if row and row[0] is not None else 1

    def current_goal_ref(self, goal_id: int = 1) -> tuple[int, int]:
        """返回指定 goal lineage 的当前不可变版本；StateStore 的研究调度权威固定为 goal_id=1。"""
        if goal_id != 1:
            raise ValueError(f"SQLiteStateStore 只调度 active goal_id=1，实收 {goal_id}")
        row = self._q1("SELECT MAX(version) FROM goal WHERE id=?", (goal_id,))
        if row is None or row[0] is None:
            raise RuntimeError(f"active goal {goal_id} 不存在")
        return goal_id, int(row[0])

    def assert_current_cycle(self, cycle_id: str, *, allow_terminal: bool = False) -> tuple[int, int]:
        """Fail loud unless a cycle and its active question belong to the active current goal."""
        ci = _cnum(cycle_id)
        cycle = self._q1(
            "SELECT goal_id,goal_ver,status,active_question_id FROM cycle WHERE id=?", (ci,))
        if cycle is None:
            raise RuntimeError(f"cycle 不存在: {cycle_id}")
        goal_id, goal_ver, status, active_question_id = cycle
        current = self.current_goal_ref(goal_id)
        if (goal_id, goal_ver) != current:
            raise RuntimeError(
                f"cycle {cycle_id} lineage={goal_id}@v{goal_ver} 非 current {current[0]}@v{current[1]}")
        if not allow_terminal and status in ("done", "failed", "aborted"):
            raise RuntimeError(f"cycle {cycle_id} 已终态 {status}")
        if active_question_id is not None:
            question = self._q1(
                "SELECT goal_id,goal_ver,status FROM question WHERE id=?", (active_question_id,))
            if question is None or tuple(question[:2]) != current or question[2] != "active":
                raise RuntimeError(
                    f"cycle {cycle_id} active_question q{active_question_id} 与 current lineage/active 状态不一致")
        return current

    def pending_goal_amend_directive(self) -> Optional[int]:
        """Return the one confirmed amendment for the current immutable goal.

        Route derivation calls this inside ``atomic()`` as well as outside it;
        ``_qall`` therefore uses the same transaction snapshot when present.
        Multiple effective hard amendments are a control-plane invariant breach
        and fail loudly instead of choosing by accident.
        """
        rows = self._qall(
            "SELECT d.id FROM directive d "
            "JOIN interaction_message m ON m.id=d.source_interaction_message_id "
            "WHERE d.kind='goal_amend' AND d.hardness='hard' AND d.status='pending' "
            "AND json_extract(d.payload_json,'$.confirmed')=1 "
            "AND json_type(d.payload_json,'$.parse_error') IS NULL "
            "AND json_type(d.payload_json,'$.new_goal_text')='text' "
            "AND trim(json_extract(d.payload_json,'$.new_goal_text'))<>'' "
            "AND json_type(d.payload_json,'$.rationale_md')='text' "
            "AND trim(json_extract(d.payload_json,'$.rationale_md'))<>'' "
            "AND (json_type(d.payload_json,'$.predicate_json') IS NULL "
            "OR json_type(d.payload_json,'$.predicate_json')='object') "
            "AND m.goal_id=1 AND m.goal_ver=(SELECT max(version) FROM goal WHERE id=1) "
            "ORDER BY d.id")
        if len(rows) > 1:
            raise RuntimeError(
                "当前 goal 同时存在多个 confirmed pending goal_amend；拒绝非确定路由")
        return int(rows[0][0]) if rows else None

    def consumed_goal_amend_directive(self, cycle_id: str) -> Optional[int]:
        rows = self._qall(
            "SELECT d.id FROM directive d JOIN decision x ON x.id=d.consumed_decision_id "
            "WHERE d.kind='goal_amend' AND d.status='consumed' AND d.consumed_cycle=? "
            "AND x.directive_id=d.id AND x.actor='human' AND x.type='directive_goal_amend'",
            (_cnum(cycle_id),))
        if len(rows) > 1:
            raise RuntimeError(f"cycle {cycle_id} 消费了多个 goal_amend")
        return int(rows[0][0]) if rows else None

    def assert_goal_amend_quiescent(self, cycle_id: str) -> None:
        """Goal version may advance only after every prior research execution has a durable terminal fact."""
        self.assert_current_cycle(cycle_id)
        ci = _cnum(cycle_id)
        checks = (
            ("build_target", "SELECT id FROM build_target WHERE cycle_id<>? AND status NOT IN "
             "('complete','skipped','failed','engineering_blocked') LIMIT 1"),
            ("run", "SELECT id FROM run WHERE cycle_id<>? AND status IN ('created','running') LIMIT 1"),
            ("evaluation_attempt", "SELECT id FROM evaluation_attempt WHERE cycle_id<>? "
             "AND status IN ('created','running') LIMIT 1"),
            ("runner_call", "SELECT id FROM runner_call WHERE COALESCE(cycle_id,-1)<>? "
             "AND phase NOT IN ('interaction_query') AND status IN ('created','running') LIMIT 1"),
        )
        for label, sql in checks:
            row = self._q1(sql, (ci,))
            if row is not None:
                raise RuntimeError(
                    f"goal_amend 前仍有 prior {label} {row[0]} 未对账终态；拒绝调用模型/升版")
        unsettled_started_import = self._q1(
            "SELECT s.id FROM external_import s JOIN baseline b ON b.id=s.baseline_id "
            "WHERE s.action='selected_for_materialization' AND b.status<>'planned' AND NOT EXISTS ("
            "SELECT 1 FROM external_import x WHERE x.question_id=s.question_id "
            "AND x.candidate_id=s.candidate_id AND x.action_cycle=s.action_cycle "
            "AND x.candidate_set_hash=s.candidate_set_hash AND x.selection_key=s.selection_key "
            "AND x.policy_hash=s.policy_hash "
            "AND x.action IN ('imported','materialize_failed','superseded')) LIMIT 1")
        if unsettled_started_import is not None:
            raise RuntimeError(
                f"goal_amend 前 import selection {unsettled_started_import[0]} 已开工但未落 settling 事件")

    def set_goal_amend_route(self, cycle_id: str, directive_id: int) -> None:
        """Atomically bind route choice to the confirmed directive it observed."""
        with self._write() as conn:
            rows = conn.execute(
                "SELECT d.id FROM directive d "
                "JOIN interaction_message m ON m.id=d.source_interaction_message_id "
                "WHERE d.kind='goal_amend' AND d.hardness='hard' AND d.status='pending' "
                "AND json_extract(d.payload_json,'$.confirmed')=1 "
                "AND json_type(d.payload_json,'$.parse_error') IS NULL "
                "AND json_type(d.payload_json,'$.new_goal_text')='text' "
                "AND trim(json_extract(d.payload_json,'$.new_goal_text'))<>'' "
                "AND json_type(d.payload_json,'$.rationale_md')='text' "
                "AND trim(json_extract(d.payload_json,'$.rationale_md'))<>'' "
                "AND (json_type(d.payload_json,'$.predicate_json') IS NULL "
                "OR json_type(d.payload_json,'$.predicate_json')='object') "
                "AND m.goal_id=1 AND m.goal_ver=(SELECT max(version) FROM goal WHERE id=1) "
                "ORDER BY d.id").fetchall()
            if len(rows) != 1 or rows[0][0] != directive_id:
                raise ValueError(f"goal_amend d{directive_id} 已不再是当前唯一有效修订")
            status = conn.execute(
                "SELECT status,route,goal_id,goal_ver FROM cycle WHERE id=?",
                (_cnum(cycle_id),)).fetchone()
            current = conn.execute("SELECT MAX(version) FROM goal WHERE id=1").fetchone()
            if (status is None or status[0] in ("done", "failed", "aborted")
                    or status[1] is not None or tuple(status[2:]) != (1, current[0])):
                raise ValueError(f"cycle {cycle_id} 不可绑定 goal_amend route")
            conn.execute("UPDATE cycle SET route='goal_amend' WHERE id=?", (_cnum(cycle_id),))
            conn.execute(
                "INSERT INTO decision(cycle_id,directive_id,actor,type,payload_json) "
                "VALUES (?,?,'orchestrator','goal_amend_routed',?)",
                (_cnum(cycle_id), directive_id,
                 json.dumps({"route": "goal_amend"}, ensure_ascii=False)))

    def _inflight_row(self):
        """在途（非终态）轮的 id 行或 None——open_or_resume_cycle 与 inflight_cycle 共用一处口径（防漂移，内审 NIT）。
        单驱动器模型下至多一条。"""
        rows = self._qall(
            "SELECT id FROM cycle WHERE status NOT IN ('done','failed','aborted') ORDER BY id")
        if len(rows) > 1:
            raise RuntimeError(f"同时存在 {len(rows)} 条在途 cycle，单驱动器不变量已破")
        if not rows:
            return None
        self.assert_current_cycle(_cid(rows[0][0]))
        return rows[0]

    def open_or_resume_cycle(self) -> Cycle:
        row = self._inflight_row()
        if row:
            return self._load_cycle(row[0])
        gver = self._goal_ver()
        with self._write() as conn:
            cur = conn.execute(
                "INSERT INTO cycle(goal_id,goal_ver,status,policy_version) VALUES (1,?,?,?)",
                (gver, "created", self._policy_version))
            cid = cur.lastrowid
        return self._load_cycle(cid)

    def cycle(self, cycle_id: str) -> Cycle:
        """按 id 读某一轮的过程侧对象（M3 Advancer 用：cycle.status = 续跑游标）。不存在 → ValueError。"""
        r = self._q1("SELECT 1 FROM cycle WHERE id=?", (_cnum(cycle_id),))
        if r is None:
            raise ValueError(f"cycle 不存在: {cycle_id}")
        return self._load_cycle(_cnum(cycle_id))

    def inflight_cycle(self) -> Optional[Cycle]:
        """在途（非终态）轮，**不创建**（区别于 open_or_resume_cycle）。M3 驱动循环恢复用：重启先看有无在途轮续跑。"""
        r = self._inflight_row()
        return self._load_cycle(r[0]) if r else None

    def last_done_cycle(self) -> Optional[Cycle]:
        """最近**成功收尾的研究轮**（status='done' 且 route 非 NULL）。M3 驱动循环用其 next_intent/
        next_question_id 定下一轮 route/目标（durable 交接）。failed/aborted 不算（selection 未落或无效）；
        **route=NULL 的物化 worker 轮不算**（非研究轮、无 selection——OPEN #6：混入会污染研究交接）。"""
        goal_id, goal_ver = self.current_goal_ref()
        r = self._q1(
            "SELECT id FROM cycle WHERE status='done' AND route IS NOT NULL "
            "AND goal_id=? AND goal_ver=? ORDER BY id DESC LIMIT 1", (goal_id, goal_ver))
        return self._load_cycle(r[0]) if r else None

    def _load_cycle(self, cid: int) -> Cycle:
        r = self._q1("SELECT id,status,route,active_question_id,next_question_id,next_intent FROM cycle WHERE id=?", (cid,))
        return Cycle(
            cycle_id=_cid(r[0]), status=r[1], route=r[2],
            question_id=_qid(r[3]) if r[3] is not None else None,
            next_question_id=_qid(r[4]) if r[4] is not None else None,
            next_intent=r[5])

    def set_route(self, cycle_id: str, route: Route) -> None:
        cid = _cnum(cycle_id)
        # 保留该公开 API 原有的终态业务错误语义；current-lineage 仍由守卫验证。
        self.assert_current_cycle(cycle_id, allow_terminal=True)
        st = self._q1("SELECT status FROM cycle WHERE id=?", (cid,))
        if st is None:
            raise ValueError(f"cycle 不存在: {cycle_id}")
        if st[0] in ("done", "failed", "aborted"):
            raise ValueError(f"cycle 已终态（{st[0]}），不得改 route")
        if route not in _ROUTES:
            raise ValueError(f"route ∉ 7 形态: {route}")
        with self._write() as conn:
            conn.execute("UPDATE cycle SET route=? WHERE id=?", (route, cid))

    def mark_cycle_done(self, cycle_id: str, status: str = "done") -> None:
        if status not in ("done", "failed", "aborted"):
            raise ValueError(f"cycle 终态非法: {status}")
        self.assert_current_cycle(cycle_id)
        with self._write() as conn:
            active = conn.execute(
                "SELECT active_question_id FROM cycle WHERE id=?", (_cnum(cycle_id),)).fetchone()
            if active is None or active[0] is not None:
                raise RuntimeError(
                    f"cycle {cycle_id} 仍持有 active_question_id="
                    f"{active[0] if active else 'missing'}，拒绝终态提交")
            changed = conn.execute(
                "UPDATE cycle SET status=?, finished_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND status NOT IN ('done','failed','aborted')",
                (status, _cnum(cycle_id))).rowcount
            if changed != 1:
                raise RuntimeError(f"cycle {cycle_id} 终态迁移竞态")
        # 无条件清本轮进程内投影（防长跑无界增长）——二者均在 _projection_snapshot 内，故若在 atomic()
        # 中且外层回滚，会随投影一并复原（cycle 未真终态时投影不丢）。
        self._local_maps.pop(cycle_id, None)
        self._bundle_cursor.pop(cycle_id, None)

    # -- question 调度可见性 ----------------------------------------------------
    def _pending_dep_count(self, qid_int: int) -> int:
        return self._q1("SELECT count(*) FROM question_dep WHERE question_id=? AND status='pending'", (qid_int,))[0]

    def is_schedulable(self, qid: str, *, for_intent: str = "attack") -> bool:
        goal_id, goal_ver = self.current_goal_ref()
        r = self._q1(
            "SELECT status,visit_count FROM question WHERE id=? AND goal_id=? AND goal_ver=?",
            (_qnum(qid), goal_id, goal_ver))
        if r is None:
            return False
        status, visit = r
        if status not in ("open", "inconclusive"):
            return False
        if self._pending_dep_count(_qnum(qid)):
            return False
        if status == "inconclusive" and for_intent == "attack":
            if visit >= self.policy["question_guard"]["max_inconclusive_per_question"]:
                return False   # 到限：对 attack 不可选，仅可作 decompose / propose_prune 对象
        return True

    def list_schedulable_questions(self) -> List[Dict[str, Any]]:
        out = []
        # ORDER BY id：确定性候选序（= 插入序，等价 InMemory 的 dict 迭代序）——护 M3 恢复一致性
        goal_id, goal_ver = self.current_goal_ref()
        for r in self._qall(
                "SELECT id,text,status,visit_count,score,est_cost,parent_id FROM question "
                "WHERE goal_id=? AND goal_ver=? ORDER BY id", (goal_id, goal_ver)):
            qid = _qid(r[0])
            if self.is_schedulable(qid, for_intent="attack") or self.is_schedulable(qid, for_intent="decompose"):
                out.append({"question_id": qid, "text": r[1], "status": r[2], "visit_count": r[3],
                            "score": r[4], "est_cost": r[5],
                            "parent_id": _qid(r[6]) if r[6] is not None else None})
        return out

    def activate_question(self, question_id: str) -> None:
        if not self.is_schedulable(question_id, for_intent="attack") \
           and not self.is_schedulable(question_id, for_intent="decompose"):
            raise ValueError(f"问题不可调度: {question_id}")
        qi = _qnum(question_id)
        with self._write() as conn:
            # active_question_id 须落到**当前轮**（护崩溃重启可恢复；InMemory 由 driver 在 Cycle 对象持之，
            # SQLite 须落库）。单驱动器模型下恰一非终态 cycle——显式核验，否则激活了却无处落 = 又制造不可恢复态。
            cur = conn.execute(
                "SELECT id,active_question_id FROM cycle "
                "WHERE status NOT IN ('done','failed','aborted') ORDER BY id").fetchall()
            if len(cur) != 1:
                raise ValueError(f"activate_question 需恰一非终态 cycle（当前 {len(cur)}）")
            if cur[0][1] is not None:
                raise ValueError(
                    f"cycle c{cur[0][0]} 已持有 active question q{cur[0][1]}，不得覆盖激活租约")
            ci = cur[0][0]
            cycle = conn.execute("SELECT goal_id,goal_ver FROM cycle WHERE id=?", (ci,)).fetchone()
            current = conn.execute(
                "SELECT MAX(version) FROM goal WHERE id=?", (cycle[0],)).fetchone()
            question = conn.execute(
                "SELECT goal_id,goal_ver,status FROM question WHERE id=?", (qi,)).fetchone()
            if (cycle[0] != 1 or current is None or current[0] != cycle[1]
                    or question is None or tuple(question[:2]) != tuple(cycle)
                    or question[2] not in ("open", "inconclusive")):
                raise ValueError(
                    f"问题 {question_id} 与在途 cycle c{ci} 的 current lineage 不一致")
            conn.execute("UPDATE question SET status='active',active_cycle=? WHERE id=?", (ci, qi))
            conn.execute("UPDATE cycle SET active_question_id=? WHERE id=?", (qi, ci))

    def mark_inconclusive(self, question_id: str) -> None:
        qi = _qnum(question_id)
        goal_id, goal_ver = self.current_goal_ref()
        r = self._q1(
            "SELECT status,active_cycle FROM question WHERE id=? AND goal_id=? AND goal_ver=?",
            (qi, goal_id, goal_ver))
        if r is None or r[0] != "active":
            raise ValueError(f"仅 active 可置 inconclusive: {question_id}({r[0] if r else '缺'})")
        if r[1] is None:
            raise RuntimeError(f"active question {question_id} 缺 active_cycle 审计锚")
        with self._write() as conn:
            try:
                append_inconclusive_event(
                    conn, question_id=qi, cycle_id=int(r[1]))
            except QuestionProgressError as error:
                raise RuntimeError(
                    f"question {question_id} inconclusive 账本写入失败: {error}"
                ) from error
            released = conn.execute(
                "UPDATE cycle SET active_question_id=NULL WHERE id=? AND active_question_id=?",
                (r[1], qi)).rowcount
            if released != 1:
                raise RuntimeError(f"释放 inconclusive question {question_id} 的 active 租约失败")

    def release_question(self, question_id: str) -> None:
        """active→open（cycle failed/aborted / dependency_wait；不增 visit，§4.2.3）。"""
        qi = _qnum(question_id)
        goal_id, goal_ver = self.current_goal_ref()
        question = self._q1(
            "SELECT status,active_cycle FROM question WHERE id=? AND goal_id=? AND goal_ver=?",
            (qi, goal_id, goal_ver))
        if question is None:
            raise ValueError(f"问题 {question_id} 不属于 current goal")
        if question[0] != "active" or question[1] is None:
            raise ValueError(f"仅带 active_cycle 的 active 问题可释放: {question_id}")
        with self._write() as conn:
            changed = conn.execute(
                "UPDATE question SET status='open' WHERE id=? AND status='active'", (qi,)).rowcount
            released = conn.execute(
                "UPDATE cycle SET active_question_id=NULL WHERE id=? AND active_question_id=?",
                (question[1], qi)).rowcount
            if changed != 1 or released != 1:
                raise RuntimeError(f"释放 question {question_id} 的状态/active 租约不一致")

    # -- dep ------------------------------------------------------------------
    def record_question_dep(self, question_id: str, *, dep_type: str, target: str) -> None:
        qi = _qnum(question_id)
        goal_id, goal_ver = self.current_goal_ref()
        if self._q1(
                "SELECT 1 FROM question WHERE id=? AND goal_id=? AND goal_ver=?",
                (qi, goal_id, goal_ver)) is None:
            raise ValueError(f"dep 主体问题不存在: {question_id}")
        if dep_type == "question":
            ti = _qnum(target)
            if ti == qi:
                raise ValueError("禁自依赖")
            if self._q1(
                    "SELECT 1 FROM question WHERE id=? AND goal_id=? AND goal_ver=?",
                    (ti, goal_id, goal_ver)) is None:
                raise ValueError(f"dep 目标不存在: {target}（§4.2.4 拒因；防静默不可满足依赖）")
            with self._write() as conn:
                conn.execute("INSERT INTO question_dep(question_id,dep_type,depends_on_question_id,status) "
                             "VALUES (?,?,?,'pending')", (qi, dep_type, ti))
        elif dep_type == "baseline":
            bi = int(target)
            if self._q1("SELECT 1 FROM baseline WHERE id=?", (bi,)) is None:
                raise ValueError(f"baseline dep 目标不存在: {target}")
            with self._write() as conn:
                conn.execute("INSERT INTO question_dep(question_id,dep_type,depends_on_baseline_id,status) "
                             "VALUES (?,?,?,'pending')", (qi, dep_type, bi))
        else:
            raise ValueError(f"dep_type 非法: {dep_type}")

    def resolve_deps(self) -> None:
        # 依赖满足口径 = 目标问题 answered/refuted（与 InMemory 等价）。dead_end 不是“满足”；
        # propose_prune 的同一事务会把指向它的 pending dep 改为 blocked，让父问题回到重规划集合。
        with self._write() as conn:
            conn.execute("UPDATE question_dep SET status='satisfied' WHERE status='pending' AND dep_type='question' "
                         "AND depends_on_question_id IN (SELECT id FROM question WHERE status IN ('answered','refuted'))")
            conn.execute("UPDATE question_dep SET status='satisfied' WHERE status='pending' AND dep_type='baseline' "
                         "AND depends_on_baseline_id IN (SELECT id FROM baseline WHERE status='legal')")

    # -- selection --------------------------------------------------------------
    def _resolve_local(self, cycle_id: str, key: Optional[str]) -> Optional[str]:
        if key is None:
            return None
        return self._local_maps.get(cycle_id, {}).get(key, key)

    def persist_selection(self, cycle_id: str, sel: Selection) -> None:
        current_goal = self.assert_current_cycle(cycle_id)
        if sel.next_intent not in ("attack", "decompose", "terminate"):
            raise InvalidSelectionError(f"next_intent 非法: {sel.next_intent}")
        next_qid = self._resolve_local(cycle_id, sel.next_question_id)
        next_intent = sel.next_intent

        # Resolve and validate the whole score batch before applying the human
        # priority overlay.  The overlay is deterministic and durable: hard pin
        # chooses its target; soft boost/suppress replaces the model-proposed
        # directive_adjust and re-ranks the scored frontier.  This keeps the
        # orchestrator from merely *showing* reprioritize in a prompt while
        # accepting an unrelated selection.
        resolved_scores = []
        seen_score_ids = set()
        for original in sel.scores:
            row = dict(original)
            ri = _qnum_opt(self._resolve_local(cycle_id, row.get("question_id")))
            if ri is None or self._q1(
                    "SELECT 1 FROM question WHERE id=? AND goal_id=? AND goal_ver=?",
                    (ri, current_goal[0], current_goal[1])) is None:
                raise InvalidSelectionError(f"scores 引用的问题不存在: {row.get('question_id')}（不静默丢弃）")
            if ri in seen_score_ids:
                raise InvalidSelectionError(f"scores 重复引用问题 q{ri}")
            seen_score_ids.add(ri)
            missing = [field for field in ("score", "est_cost") if field not in row]
            if missing:
                raise InvalidSelectionError(f"scores.q{ri} 缺必填字段: {missing}")
            for field in ("score", "est_cost", "directive_adjust"):
                if field not in row:
                    continue
                value = row[field]
                if (isinstance(value, bool) or not isinstance(value, (int, float))
                        or not math.isfinite(float(value))):
                    raise InvalidSelectionError(f"scores.q{ri}.{field} 须为有限数字")
                if field == "est_cost" and value < 0:
                    raise InvalidSelectionError(f"scores.q{ri}.est_cost 不得为负")
            resolved_scores.append((row, ri))

        cycle_meta = self._q1("SELECT route,goal_id,goal_ver FROM cycle WHERE id=?",
                              (_cnum(cycle_id),))
        if cycle_meta is None:
            raise InvalidSelectionError(f"cycle 不存在: {cycle_id}")
        if cycle_meta[0] == "goal_amend":
            expected = {r[0] for r in self._qall(
                "SELECT q.id FROM question q WHERE q.goal_id=? AND q.goal_ver=? "
                "AND q.status IN ('open','inconclusive') AND NOT EXISTS ("
                "SELECT 1 FROM question_dep d WHERE d.question_id=q.id AND d.status='pending')",
                (cycle_meta[1], cycle_meta[2]))}
            if seen_score_ids != expected:
                missing = sorted(expected - seen_score_ids)
                extra = sorted(seen_score_ids - expected)
                raise InvalidSelectionError(
                    "goal_amend 必须重评全部且仅限当前可调度 open/inconclusive 前沿；"
                    f"missing={[f'q{x}' for x in missing]}, extra={[f'q{x}' for x in extra]}")

        next_qid, next_intent, resolved_scores, priority_audit = self._apply_reprioritize(
            cycle_id, next_qid, next_intent, resolved_scores)
        next_int = _qnum_opt(next_qid)     # 未解析 local_key / 畸形 id → None → 走干净「不存在」拒因
        if next_intent == "terminate":
            if next_qid is not None:
                raise InvalidSelectionError("terminate 时 next_question_id 必须为 null")
        else:
            if next_int is None or self._q1(
                    "SELECT 1 FROM question WHERE id=? AND goal_id=? AND goal_ver=?",
                    (next_int, current_goal[0], current_goal[1])) is None:
                raise InvalidSelectionError(f"next_question_id 缺失或不存在: {sel.next_question_id}")
            if not self.is_schedulable(next_qid, for_intent=next_intent):
                raise InvalidSelectionError(f"目标问题不可调度: {next_qid}")
        with self._write() as conn:
            for row, ri in resolved_scores:   # schema 要求二者必填；此处是唯一写回点（local_key 同样解析）
                conn.execute(
                    "UPDATE question SET score=?, est_cost=? WHERE id=?",
                    (row["score"], row["est_cost"], ri))
            for directive_id, decision_type, payload in priority_audit:
                conn.execute(
                    "INSERT INTO decision(cycle_id,directive_id,actor,type,payload_json) "
                    "VALUES (?,?,'orchestrator',?,?)",
                    (_cnum(cycle_id), directive_id, decision_type,
                     json.dumps(payload, ensure_ascii=False)))
                if decision_type == "soft_directive_declined":
                    directive_row = conn.execute(
                        "SELECT payload_json FROM directive WHERE id=? AND status='consumed'",
                        (directive_id,)).fetchone()
                    if directive_row is None:
                        raise RuntimeError(
                            f"soft reprioritize d{directive_id} 不在 consumed 状态，无法原子拒绝")
                    directive_payload = json.loads(directive_row[0])
                    directive_payload["rejection_reason"] = payload["reason"]
                    directive_payload["rejection_kind"] = "soft_directive_declined"
                    changed = conn.execute(
                        "UPDATE directive SET status='rejected',payload_json=? "
                        "WHERE id=? AND status='consumed'",
                        (json.dumps(directive_payload, ensure_ascii=False), directive_id)).rowcount
                    if changed != 1:
                        raise RuntimeError(f"soft reprioritize d{directive_id} 拒绝迁移竞态")
            if cycle_meta[0] == "goal_amend" and next_intent == "terminate":
                has_frontier = conn.execute(
                    "SELECT 1 FROM question q WHERE q.goal_id=? AND q.goal_ver=? "
                    "AND q.status IN ('open','inconclusive') AND NOT EXISTS ("
                    "SELECT 1 FROM question_dep d WHERE d.question_id=q.id AND d.status='pending') LIMIT 1",
                    (cycle_meta[1], cycle_meta[2])).fetchone()
                reusable = conn.execute(
                    "SELECT 1 FROM answer_applicability WHERE goal_id=? AND goal_ver=? "
                    "AND status='still_applicable' LIMIT 1",
                    (cycle_meta[1], cycle_meta[2])).fetchone()
                if has_frontier is None and reusable is None:
                    conn.execute(
                        "INSERT INTO decision(cycle_id,actor,type,payload_json) "
                        "VALUES (?,'orchestrator','blocked_by_goal_amend',?)",
                        (_cnum(cycle_id), json.dumps({
                            "reason": "no_schedulable_frontier_or_applicable_prior_answer",
                            "goal_ver": cycle_meta[2]}, ensure_ascii=False)))
            conn.execute("UPDATE cycle SET next_question_id=?, next_intent=? WHERE id=?",
                         (next_int, next_intent, _cnum(cycle_id)))

    def _apply_reprioritize(self, cycle_id: str, next_qid: Optional[str], next_intent: str,
                            resolved_scores):
        """Apply consumed reprioritize controls to a selection projection.

        Returns ``(next_qid, next_intent, scores, audit_decisions)``.  The
        actual DB writes remain in ``persist_selection``'s one write scope so a
        crash cannot commit an audit decision without the normalized scores and
        selected target (or vice versa).
        """
        ci = _cnum(cycle_id)
        rows = self._qall(
            "SELECT id,hardness,payload_json FROM directive WHERE kind='reprioritize' "
            "AND status='consumed' AND consumed_cycle=? ORDER BY id", (ci,))
        if not rows:
            return next_qid, next_intent, resolved_scores, []

        by_qid = {ri: row for row, ri in resolved_scores}
        soft_by_qid: Dict[int, list] = {}
        pin = None
        for directive_id, hardness, payload_raw in rows:
            try:
                payload = json.loads(payload_raw)
            except json.JSONDecodeError as error:
                raise RuntimeError(f"reprioritize d{directive_id} payload_json 损坏") from error
            mode = payload.get("mode")
            qi = _qnum_opt(payload.get("question_id"))
            if qi is None:
                raise RuntimeError(f"已消费 reprioritize d{directive_id} 缺合法 question_id")
            if mode == "pin":
                if hardness != "hard":
                    raise RuntimeError(f"已消费 reprioritize pin d{directive_id} 非 hard")
                if pin is not None:
                    raise RuntimeError(
                        f"cycle {cycle_id} 同时消费多个 hard pin: d{pin[0]}, d{directive_id}")
                pin = (directive_id, qi)
            elif mode in ("boost", "suppress"):
                adjust = payload.get("adjust")
                if (hardness != "soft" or isinstance(adjust, bool)
                        or not isinstance(adjust, (int, float))
                        or not math.isfinite(float(adjust)) or adjust == 0
                        or (mode == "boost") != (adjust > 0)):
                    raise RuntimeError(f"已消费 reprioritize d{directive_id} 的 {mode}/adjust 契约损坏")
                soft_by_qid.setdefault(qi, []).append((directive_id, float(adjust)))
            else:
                raise RuntimeError(f"已消费 reprioritize d{directive_id} mode 非法: {mode!r}")

        audit = []
        soft_applied = False
        for qi, controls in soft_by_qid.items():
            row = by_qid.get(qi)
            if row is None:
                reason = f"selection.scores 未包含 q{qi}，无法安全计算 directive_adjust"
                for directive_id, _ in controls:
                    audit.append((directive_id, "soft_directive_declined", {"reason": reason}))
                continue
            actual = float(row.get("directive_adjust", 0.0))
            requested = sum(adjust for _, adjust in controls)
            row["score"] = float(row["score"]) - actual + requested
            row["directive_adjust"] = requested
            soft_applied = True
            for directive_id, adjust in controls:
                audit.append((directive_id, "reprioritize_applied", {
                    "question_id": f"q{qi}", "adjust": adjust,
                    "aggregate_adjust": requested, "normalized_score": row["score"],
                }))

        # Soft priority changes the ranking but never overrides an explicit
        # terminate.  All scored, currently schedulable rows participate; ties
        # use the stable question id.
        if soft_applied and next_intent != "terminate":
            candidates = [
                (float(row["score"]), -ri, ri, row)
                for row, ri in resolved_scores
                if (self.is_schedulable(f"q{ri}", for_intent="attack")
                    or self.is_schedulable(f"q{ri}", for_intent="decompose"))
            ]
            if candidates:
                _, _, selected, selected_row = max(candidates)
                if f"q{selected}" != next_qid:
                    next_qid = f"q{selected}"
                    next_intent = self._intent_for_est_cost(next_qid, selected_row.get("est_cost"))

        if pin is not None:
            directive_id, qi = pin
            if not (self.is_schedulable(f"q{qi}", for_intent="attack")
                    or self.is_schedulable(f"q{qi}", for_intent="decompose")):
                raise InvalidSelectionError(f"hard pin d{directive_id} 的目标 q{qi} 已不可调度")
            next_qid = f"q{qi}"
            score_row = by_qid.get(qi)
            est_cost = (score_row.get("est_cost") if score_row is not None else
                        self._q1("SELECT est_cost FROM question WHERE id=?", (qi,))[0])
            next_intent = self._intent_for_est_cost(next_qid, est_cost)
            audit.append((directive_id, "reprioritize_enforced", {
                "question_id": next_qid, "next_intent": next_intent,
                "source": "hard_pin",
            }))
        return next_qid, next_intent, resolved_scores, audit

    def _intent_for_est_cost(self, question_id: str, est_cost: Any) -> str:
        """Reference R3's cost split, constrained by the target's live guards."""
        if (isinstance(est_cost, bool) or not isinstance(est_cost, (int, float))
                or not math.isfinite(float(est_cost)) or est_cost < 0):
            # A hard pin with no trustworthy cost must not silently launch an
            # unbudgeted attack; decomposition is the conservative route.
            preferred = "decompose"
        else:
            threshold = self.policy["flow"]["decompose_threshold"]
            budget = compute_budget(self._rconn(), self.policy["budget"])
            preferred = ("attack" if float(est_cost) <= float(threshold) * budget
                         else "decompose")
        alternate = "decompose" if preferred == "attack" else "attack"
        if self.is_schedulable(question_id, for_intent=preferred):
            return preferred
        if self.is_schedulable(question_id, for_intent=alternate):
            return alternate
        # The caller performs the authoritative schedulability validation and
        # will reject this selection.  Returning the deterministic preference
        # here preserves a useful error path without inventing a third route.
        return preferred

    def reject_unapplied_reprioritize(self, cycle_id: str, reason: str) -> None:
        """Terminalize consumed priority controls when the cycle cannot persist a selection.

        Attack's no-wedge fallback converts an invalid external selection into a
        durable terminate.  Without this companion transition, a reprioritize
        consumed just before that selection would remain forever in a false
        "pending actual effect" state.
        """
        ci = _cnum(cycle_id)
        bounded_reason = str(reason)[:2_000]
        with self._write() as conn:
            rows = conn.execute(
                "SELECT x.id,x.payload_json FROM directive x WHERE x.kind='reprioritize' "
                "AND x.status='consumed' AND x.consumed_cycle=? AND NOT EXISTS ("
                "SELECT 1 FROM decision d WHERE d.directive_id=x.id "
                "AND d.type IN ('reprioritize_applied','reprioritize_enforced',"
                "'soft_directive_declined','directive_application_rejected')) ORDER BY x.id",
                (ci,)).fetchall()
            for directive_id, payload_raw in rows:
                payload = json.loads(payload_raw)
                payload["rejection_reason"] = bounded_reason
                payload["rejection_kind"] = "selection_invalid"
                conn.execute(
                    "INSERT INTO decision(cycle_id,directive_id,actor,type,payload_json) "
                    "VALUES (?,?,'orchestrator','directive_application_rejected',?)",
                    (ci, directive_id,
                     json.dumps({"reason": bounded_reason}, ensure_ascii=False)))
                changed = conn.execute(
                    "UPDATE directive SET status='rejected',payload_json=? "
                    "WHERE id=? AND status='consumed'",
                    (json.dumps(payload, ensure_ascii=False), directive_id)).rowcount
                if changed != 1:
                    raise RuntimeError(f"reprioritize d{directive_id} 终态拒绝竞态")

    # -- 七 op 树操作（单事务原子；§4.2.4 封闭词表 / §4.2.5 原子性） --------------
    def apply_tree_ops(self, cycle_id: str, ops: List[Dict[str, Any]]) -> None:
        with self._write() as conn:
            self._apply_ops(conn, cycle_id, ops)

    def _apply_ops(self, conn, cycle_id: str, ops: List[Dict[str, Any]]) -> None:
        ci = _cnum(cycle_id)
        cycle_row = conn.execute(
            "SELECT route,goal_id,goal_ver,status FROM cycle WHERE id=?", (ci,)).fetchone()
        if cycle_row is None:
            raise RuntimeError(f"cycle 不存在: {cycle_id}")
        route, goal_id, cycle_goal_ver, cycle_status = cycle_row
        current = conn.execute(
            "SELECT MAX(version) FROM goal WHERE id=?", (goal_id,)).fetchone()
        if (goal_id != 1 or current is None or current[0] != cycle_goal_ver
                or cycle_status in ("done", "failed", "aborted")):
            raise RuntimeError(
                f"cycle {cycle_id} 不是 active current goal lineage，拒绝应用 tree_ops")
        if route == "goal_amend":
            amend_indexes = [i for i, op in enumerate(ops)
                             if isinstance(op, dict) and op.get("op") == "amend_goal"]
            already_amended = self._q1(
                "SELECT 1 FROM goal WHERE id=1 AND created_cycle=? LIMIT 1",
                (_cnum(cycle_id),)) is not None
            if (not already_amended and amend_indexes != [0]) or (
                    already_amended and amend_indexes):
                raise ValueError(
                    "goal_amend 首批 tree_ops 须恰有一个 amend_goal 且位于首位；"
                    "同 cycle 已升版后不得再次 amend")
        local = self._local_maps.setdefault(cycle_id, {})
        guard = self.policy["tree_guard"]
        gver = int(cycle_goal_ver)
        for op in ops:
            kind = op.get("op")
            if kind == "create_root":
                if route != "bootstrap":
                    raise ValueError("create_root 仅限 bootstrap 轮（§4.2.4）")
                text, contract, contract_source = normalize_question_contract(
                    op.get("text"), op.get("predicate_json"))
                qid = self._insert_question(
                    conn, text, contract, None, "agent", gver, born_cycle=ci)
                if op.get("local_key"):
                    local[op["local_key"]] = qid
                self._record_question_admission(
                    conn, cycle_id, qid, operation="create_root", text=text,
                    contract=contract, contract_source=contract_source)
                conn.execute("INSERT INTO decision(cycle_id,question_id,actor,type,payload_json) "
                             "VALUES (?,?,'agent','create_root',?)", (_cnum(cycle_id), _qnum(qid), json.dumps({"qid": qid})))
            elif kind == "add_children":
                self._add_children(conn, cycle_id, route, op, local, guard, gver)
            elif kind == "spawn_question":
                self._spawn_question(conn, cycle_id, route, op, local, guard, gver)
            elif kind == "mark_answer_applicability":
                self._mark_applicability(conn, cycle_id, op, local, gver)
            elif kind == "propose_prune":
                self._propose_prune(conn, cycle_id, op, gver)
            elif kind == "amend_goal":
                self._amend_goal(conn, cycle_id, route, op)
                gver = self._goal_ver()   # 版本已升，后续 op 用新版
            elif kind == "seed_applicability_audit":
                self._seed_applicability(conn, cycle_id, route, op)
            else:
                raise ValueError(f"op 不在封闭词表: {kind}")

    def _insert_question(self, conn, text: str, predicate_json: Dict[str, Any],
                         parent_qid: Optional[str], source: str, gver: int,
                         status: str = "open", born_cycle: Optional[int] = None) -> str:
        cur = conn.execute(
            "INSERT INTO question(parent_id,goal_id,goal_ver,born_goal_ver,text,predicate_json,"
            "status,source,born_cycle) VALUES (?,1,?,?,?,?,?,?,?)",
            (_qnum(parent_qid) if parent_qid else None, gver, gver, text,
             json.dumps(predicate_json, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
             status, source, born_cycle))
        return _qid(cur.lastrowid)

    @staticmethod
    def _record_question_admission(
            conn, cycle_id: str, qid: str, *, operation: str, text: str,
            contract: Dict[str, Any], contract_source: str) -> None:
        payload = admission_payload(
            qid=qid, operation=operation, text=text, contract=contract,
            contract_source=contract_source)
        conn.execute(
            "INSERT INTO decision(cycle_id,question_id,actor,type,payload_json) "
            "VALUES (?,?,'agent','question_admission',?)",
            (_cnum(cycle_id), _qnum(qid), json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))))

    def _open_count(self, conn) -> int:
        return conn.execute("SELECT count(*) FROM question WHERE status IN ('open','inconclusive')").fetchone()[0]

    def _depth_of(self, conn, qid_int: int) -> int:
        """根=0；沿 parent 链计深（DDL 已焊 parent 不可改环，无需 seen 防护，但保留上界防坏数据）。"""
        depth, pid = 0, conn.execute("SELECT parent_id FROM question WHERE id=?", (qid_int,)).fetchone()[0]
        seen = set()
        while pid is not None and pid not in seen:
            seen.add(pid)
            depth += 1
            row = conn.execute("SELECT parent_id FROM question WHERE id=?", (pid,)).fetchone()
            pid = row[0] if row else None
        return depth

    def _add_children(self, conn, cycle_id, route, op, local, guard, gver) -> None:
        if route != "decompose":
            raise ValueError("add_children 仅限 decompose 轮（§4.2.4）")
        parent_qid = op["parent_question_id"]
        pr = conn.execute(
            "SELECT status,goal_id,goal_ver,active_cycle FROM question WHERE id=?",
            (_qnum(parent_qid),)).fetchone()
        if pr is None:
            raise ValueError(f"decompose 父问题不存在: {parent_qid}")
        if pr[0] != "active":
            raise ValueError(f"decompose 父问题须为 active（当前 {pr[0]}；终态/未选中不可分解）")
        if tuple(pr[1:3]) != (1, gver) or pr[3] != _cnum(cycle_id):
            raise ValueError("decompose 父问题不属于本 cycle 的 current goal lineage")
        pqi = _qnum(parent_qid)
        if self._depth_of(conn, pqi) + 1 > guard["max_decompose_depth"]:
            raise ValueError("超出 max_decompose_depth")
        children = op["children"]
        # max_children_per_node = 该节点**累计**子问题上限（含此前多次 decompose 已挂的），非单次 op 上限——
        # 按策略键名的 per_node 语义修正 InMemory 的 per-op 疏漏（本组件为真相侧、取正确口径）。
        existing_children = conn.execute("SELECT count(*) FROM question WHERE parent_id=?", (pqi,)).fetchone()[0]
        if existing_children + len(children) > guard["max_children_per_node"]:
            raise ValueError("超出 max_children_per_node")
        # +1：父问题本轮 active→open 释放后重新计入 open 池（否则实际 open 数会超上限 1）
        if self._open_count(conn) + len(children) + 1 > guard["max_open_questions"]:
            raise ValueError("超出 max_open_questions")
        child_qids = []
        for ch in children:                      # 同事务：写子问题 + 逐子 dep（父依赖每个子）
            text, contract, contract_source = normalize_question_contract(
                ch.get("text"), ch.get("predicate_json"))
            cq = self._insert_question(
                conn, text, contract, parent_qid, "decompose", gver,
                born_cycle=_cnum(cycle_id))
            local[ch["local_key"]] = cq
            child_qids.append(cq)
            self._record_question_admission(
                conn, cycle_id, cq, operation="add_children", text=text,
                contract=contract, contract_source=contract_source)
            conn.execute("INSERT INTO question_dep(question_id,dep_type,depends_on_question_id,status,created_cycle) "
                         "VALUES (?,'question',?,'pending',?)", (pqi, _qnum(cq), _cnum(cycle_id)))
        conn.execute("UPDATE question SET decompose_count=decompose_count+1, status='open' WHERE id=?", (pqi,))
        released = conn.execute(
            "UPDATE cycle SET active_question_id=NULL WHERE id=? AND active_question_id=?",
            (_cnum(cycle_id), pqi)).rowcount
        if released != 1:
            raise RuntimeError(f"decompose 释放父问题 {parent_qid} 的 active 租约失败")
        conn.execute("INSERT INTO decision(cycle_id,question_id,actor,type,payload_json) VALUES (?,?,'agent','decompose',?)",
                     (_cnum(cycle_id), _qnum(parent_qid), json.dumps({"parent": parent_qid, "children": child_qids})))

    @staticmethod
    def _strict_json_object(raw: Any, *, label: str) -> Dict[str, Any]:
        try:
            value = json.loads(
                raw, parse_constant=lambda token: (_ for _ in ()).throw(
                    ValueError(f"非有限 JSON number: {token}")))
        except (json.JSONDecodeError, ValueError, TypeError) as error:
            raise RuntimeError(f"{label} payload 损坏") from error
        if not isinstance(value, dict):
            raise RuntimeError(f"{label} payload 须为 object")
        return value

    @staticmethod
    def _request_ref(value: Any) -> tuple[str, int]:
        if not isinstance(value, str):
            raise ValueError("spawn_question.request_ref 须为 db:directive:<id> 或 db:decision:<id>")
        match = _REQUEST_REF_RE.fullmatch(value)
        if match is None:
            raise ValueError("spawn_question.request_ref 须为 db:directive:<id> 或 db:decision:<id>")
        return match.group(1), int(match.group(2))

    @staticmethod
    def _assert_sha256(value: Any, *, field: str) -> str:
        if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
            raise RuntimeError(f"{field} 非 sha256")
        return value

    def _assert_request_ref_unused(
            self, conn, *, request_ref: str,
            directive_id: Optional[int] = None) -> None:
        if directive_id is not None:
            rows = conn.execute(
                "SELECT id,question_id FROM decision WHERE actor='orchestrator' "
                "AND type='question_request_bound' AND directive_id=? ORDER BY id",
                (directive_id,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT id,question_id FROM decision WHERE actor='orchestrator' "
                "AND type='question_request_bound' AND json_valid(payload_json) "
                "AND json_extract(payload_json,'$.request_ref')=? ORDER BY id",
                (request_ref,)).fetchall()
        if rows:
            raise ValueError(
                f"spawn_question.request_ref 已被 q{rows[0][1]} 消费: {request_ref}")

    def _console_spawn_request(
            self, conn, *, cycle_id: str, directive_id: int,
            request_ref: str, op: Dict[str, Any], gver: int) -> Dict[str, Any]:
        ci = _cnum(cycle_id)
        row = conn.execute(
            "SELECT d.status,d.kind,d.hardness,d.consumed_cycle,"
            "d.consumed_decision_id,d.source_interaction_message_id,d.payload_json,"
            "x.cycle_id,x.question_id,x.directive_id,x.actor,x.type,x.payload_json,"
            "m.goal_id,m.goal_ver "
            "FROM directive d JOIN decision x ON x.id=d.consumed_decision_id "
            "JOIN interaction_message m ON m.id=d.source_interaction_message_id "
            "WHERE d.id=?", (directive_id,)).fetchone()
        if row is None:
            raise ValueError(f"spawn_question.request_ref 不存在: {request_ref}")
        (status, directive_kind, hardness, consumed_cycle,
         consumed_decision_id, source_message_id, directive_raw,
         decision_cycle, decision_question, decision_directive, decision_actor,
         decision_type, decision_raw, message_goal_id, message_goal_ver) = row
        if (status != "consumed" or directive_kind != "inject_question"
                or consumed_cycle != ci or decision_cycle != ci
                or decision_question is not None
                or decision_directive != directive_id
                or decision_actor != "human"
                or decision_type != "directive_inject_question"
                or (message_goal_id, message_goal_ver) != (1, gver)):
            raise ValueError(
                f"spawn_question.request_ref 未指向本轮 current-goal 已消费 inject_question: {request_ref}")
        classified = conn.execute(
            "SELECT intent,directive_id FROM interaction_classification "
            "WHERE message_id=?", (source_message_id,)).fetchone()
        if classified != ("directive", directive_id):
            raise RuntimeError(
                f"inject_question d{directive_id} classification provenance 损坏")
        decision_payload = self._strict_json_object(
            decision_raw, label=f"directive_inject_question d{directive_id}")
        effect = decision_payload.get("effect")
        request = (effect.get("reasoning_question_request")
                   if isinstance(effect, dict) else None)
        base_keys = {
            "protocol", "request_ref", "requested_text",
            "parent_question_id", "suggested_kind",
            "requires_reasoning_predicate",
        }
        if (not isinstance(effect, dict) or not isinstance(request, dict)
                or set(request) not in (
                    base_keys,
                    base_keys | {"human_named_repo", "need_summary"})
                or effect.get("applies_to_reasoning_cycle") != cycle_id
                or request.get("protocol") != "directive-question-request-v1"
                or request.get("request_ref") != request_ref
                or request.get("suggested_kind") not in (
                    "followup", "import_reference")
                or request.get("requires_reasoning_predicate") is not True
                or not isinstance(request.get("requested_text"), str)
                or not request["requested_text"].strip()
                or (request.get("parent_question_id") is not None
                    and (not isinstance(request["parent_question_id"], str)
                         or _QREF_RE.fullmatch(
                             request["parent_question_id"]) is None))
                or (("human_named_repo" in request)
                    != (request.get("suggested_kind") == "import_reference"))):
            raise RuntimeError(
                f"inject_question d{directive_id} frozen reasoning request 损坏")
        if (op.get("kind") != request["suggested_kind"]
                or op.get("text") != request["requested_text"]
                or op.get("parent_question_id")
                != request["parent_question_id"]):
            raise ValueError(
                "spawn_question 与 request_ref 的 exact text/parent/kind 不一致")
        if not isinstance(op.get("predicate_json"), dict):
            raise ValueError(
                "request_ref 建题必须由 reasoning 显式给出 predicate_json")
        self._assert_request_ref_unused(
            conn, request_ref=request_ref, directive_id=directive_id)
        directive_payload = self._strict_json_object(
            directive_raw, label=f"inject_question directive d{directive_id}")
        repo = request.get("human_named_repo")
        authority_spec = None
        if repo is not None:
            if (hardness != "hard" or directive_payload.get("confirmed") is not True
                    or not isinstance(repo, dict)
                    or set(repo) != {"canonical_uri", "requested_revision"}
                    or not isinstance(request.get("need_summary"), str)
                    or not request["need_summary"].strip()
                    or directive_payload.get("human_named_repo") != repo
                    or directive_payload.get("need_summary")
                    != request["need_summary"]):
                raise RuntimeError(
                    f"human_named inject_question d{directive_id} confirmation/provenance 损坏")
            authority_spec = {
                "directive_id": directive_id,
                "source_message_id": source_message_id,
                "goal_id": 1, "goal_ver": gver,
                "canonical_uri": repo.get("canonical_uri"),
                "requested_revision": repo.get("requested_revision"),
                "need_summary": request["need_summary"],
            }
        elif request["suggested_kind"] != "followup":
            raise RuntimeError(
                f"ordinary inject_question d{directive_id} suggested_kind 非 followup")
        return {
            "source_kind": "console_directive",
            "request_ref": request_ref,
            "request_decision_id": consumed_decision_id,
            "reasoning_request_hash": authority_hash(request),
            "requested_text": request["requested_text"],
            "parent_question_id": request["parent_question_id"],
            "spawn_kind": request["suggested_kind"],
            "question_source": "human",
            "directive_id": directive_id,
            "human_authority_spec": authority_spec,
            "reference_authority_spec": None,
            "dependency_origin_id": None,
        }

    def _import_trigger_spawn_request(
            self, conn, *, cycle_id: str, decision_id: int,
            request_ref: str, op: Dict[str, Any], gver: int) -> Dict[str, Any]:
        ci = _cnum(cycle_id)
        row = conn.execute(
            "SELECT cycle_id,question_id,actor,type,payload_json FROM decision "
            "WHERE id=?", (decision_id,)).fetchone()
        if row is None:
            raise ValueError(f"spawn_question.request_ref 不存在: {request_ref}")
        decision_cycle, origin_id, actor, decision_type, payload_raw = row
        if (decision_cycle != ci or actor != "orchestrator"
                or decision_type != "import_trigger_completed"
                or origin_id is None):
            raise ValueError(
                f"spawn_question.request_ref 未指向本轮 import_trigger_completed: {request_ref}")
        payload = self._strict_json_object(
            payload_raw, label=f"import_trigger_completed d{decision_id}")
        required = {
            "protocol", "trigger_kind", "request", "request_hash",
            "trigger_context_hash", "policy_hash", "runner_call_id",
            "receipt_ref", "result_hash", "candidate_count", "skipped_count",
            "candidate_ids", "license_review_ids", "child_question_id",
            "source_authority_hash", "terminalized", "reference_snapshot",
            "reasoning_question_request",
        }
        reasoning = payload.get("reasoning_question_request")
        reasoning_keys = {
            "protocol", "op", "kind", "parent_question_id",
            "requested_text", "need_summary", "trigger_kind",
            "survey_candidate_count", "request_hash", "result_hash",
            "requires_reasoning_predicate",
        }
        external_request = payload.get("request")
        trigger_kind = payload.get("trigger_kind")
        if (set(payload) != required
                or payload.get("protocol") != "import-trigger-v1"
                or trigger_kind not in ("stuck", "sota_reference")
                or not isinstance(external_request, dict)
                or payload.get("request_hash")
                != authority_hash(external_request)
                or external_request.get("trigger_kind") != trigger_kind
                or not isinstance(reasoning, dict)
                or set(reasoning) != reasoning_keys
                or reasoning.get("protocol")
                != "import-trigger-question-request-v1"
                or reasoning.get("op") != "spawn_question"
                or reasoning.get("kind") != "import_reference"
                or reasoning.get("trigger_kind") != trigger_kind
                or reasoning.get("requires_reasoning_predicate") is not True
                or reasoning.get("request_hash") != payload.get("request_hash")
                or reasoning.get("result_hash") != payload.get("result_hash")
                or reasoning.get("need_summary")
                != external_request.get("need_summary")
                or not isinstance(reasoning.get("requested_text"), str)
                or not reasoning["requested_text"].strip()
                or reasoning.get("parent_question_id") != f"q{origin_id}"
                or isinstance(reasoning.get("survey_candidate_count"), bool)
                or not isinstance(reasoning.get("survey_candidate_count"), int)
                or reasoning["survey_candidate_count"] < 0
                or (trigger_kind == "stuck"
                    and reasoning["survey_candidate_count"] == 0)
                or payload.get("candidate_count") != 0
                or payload.get("candidate_ids") != []
                or payload.get("license_review_ids") != []
                or payload.get("child_question_id") is not None
                or payload.get("source_authority_hash") is not None
                or payload.get("terminalized") is not False
                or isinstance(payload.get("skipped_count"), bool)
                or not isinstance(payload.get("skipped_count"), int)
                or payload["skipped_count"] < 0
                or ((trigger_kind == "sota_reference")
                    != isinstance(payload.get("reference_snapshot"), dict))):
            raise RuntimeError(
                f"import_trigger_completed d{decision_id} frozen request 损坏")
        for field in (
                "request_hash", "trigger_context_hash", "policy_hash",
                "result_hash"):
            self._assert_sha256(payload.get(field), field=f"completion.{field}")
        if (op.get("kind") != "import_reference"
                or op.get("text") != reasoning["requested_text"]
                or op.get("parent_question_id")
                != reasoning["parent_question_id"]):
            raise ValueError(
                "spawn_question 与 request_ref 的 exact text/parent/kind 不一致")
        if not isinstance(op.get("predicate_json"), dict):
            raise ValueError(
                "request_ref 建题必须由 reasoning 显式给出 predicate_json")
        origin = conn.execute(
            "SELECT goal_id,goal_ver,status FROM question WHERE id=?",
            (origin_id,)).fetchone()
        if (origin is None or origin[:2] != (1, gver)
                or origin[2] != "inconclusive"):
            raise ValueError(
                "import trigger request parent 须先按本轮证据不足收为 current-goal inconclusive")
        runner_call_id = payload.get("runner_call_id")
        if (isinstance(runner_call_id, bool)
                or not isinstance(runner_call_id, int) or runner_call_id <= 0
                or not isinstance(payload.get("receipt_ref"), str)
                or not payload["receipt_ref"]):
            raise RuntimeError(
                f"import_trigger_completed d{decision_id} runner/receipt 身份损坏")
        runner = conn.execute(
            "SELECT cycle_id,phase,purpose,status,transcript_ref FROM runner_call "
            "WHERE id=?", (runner_call_id,)).fetchone()
        if runner != (
                ci, "import_search",
                f"import_trigger:{payload['request_hash']}", "success",
                payload["receipt_ref"]):
            raise RuntimeError(
                f"import_trigger_completed d{decision_id} runner provenance 不一致")
        self._assert_request_ref_unused(conn, request_ref=request_ref)
        return {
            "source_kind": "import_trigger_completed",
            "request_ref": request_ref,
            "request_decision_id": decision_id,
            "reasoning_request_hash": authority_hash(reasoning),
            "requested_text": reasoning["requested_text"],
            "parent_question_id": reasoning["parent_question_id"],
            "spawn_kind": "import_reference",
            "question_source": "agent",
            "directive_id": None,
            "human_authority_spec": None,
            "reference_authority_spec": {
                "trigger_kind": trigger_kind,
                "origin_cycle_id": ci,
                "origin_question_id": origin_id,
                "goal_id": 1, "goal_ver": gver,
                "request_hash": payload["request_hash"],
                "trigger_context_hash": payload["trigger_context_hash"],
                "policy_hash": payload["policy_hash"],
                "runner_call_id": runner_call_id,
                "receipt_ref": payload["receipt_ref"],
                "result_hash": payload["result_hash"],
                "need_summary": reasoning["need_summary"],
                "reference_snapshot": payload["reference_snapshot"],
            },
            "dependency_origin_id": origin_id,
        }

    def _spawn_request(
            self, conn, *, cycle_id: str, op: Dict[str, Any],
            gver: int) -> Optional[Dict[str, Any]]:
        request_ref = op.get("request_ref")
        if request_ref is None:
            return None
        domain, source_id = self._request_ref(request_ref)
        if domain == "directive":
            return self._console_spawn_request(
                conn, cycle_id=cycle_id, directive_id=source_id,
                request_ref=request_ref, op=op, gver=gver)
        return self._import_trigger_spawn_request(
            conn, cycle_id=cycle_id, decision_id=source_id,
            request_ref=request_ref, op=op, gver=gver)

    def _request_child_capacity(
            self, conn, *, parent_qid: str, guard: Dict[str, Any]) -> None:
        parent_id = _qnum(parent_qid)
        children = conn.execute(
            "SELECT count(*) FROM question WHERE parent_id=?",
            (parent_id,)).fetchone()[0]
        if children + 1 > int(guard["max_children_per_node"]):
            raise ValueError("request_ref child 超出 max_children_per_node")
        if self._depth_of(conn, parent_id) + 1 > int(
                guard["max_decompose_depth"]):
            raise ValueError("request_ref child 超出 max_decompose_depth")

    def _bind_spawn_request(
            self, conn, *, cycle_id: str, qid: str, text: str,
            op: Dict[str, Any], request: Dict[str, Any], gver: int) -> str:
        qi, ci = _qnum(qid), _cnum(cycle_id)
        human_authority = None
        reference_authority = None
        if request["human_authority_spec"] is not None:
            human_authority = build_human_named_authority(
                **request["human_authority_spec"], question_id=qi)
            source_authority_hash = human_authority["authority_hash"]
        elif request["reference_authority_spec"] is not None:
            reference_authority = build_reference_authority(
                **request["reference_authority_spec"], child_question_id=qi)
            source_authority_hash = reference_authority["authority_hash"]
        else:
            source_authority_hash = None
        binding = build_question_request_binding(
            source_kind=request["source_kind"],
            request_ref=request["request_ref"],
            request_decision_id=request["request_decision_id"],
            reasoning_request_hash=request["reasoning_request_hash"],
            question_id=qi, goal_id=1, goal_ver=gver,
            spawn_kind=op["kind"],
            parent_question_id=op.get("parent_question_id"),
            requested_text=text,
            source_authority_hash=source_authority_hash)
        conn.execute(
            "INSERT INTO decision(cycle_id,question_id,directive_id,actor,type,payload_json) "
            "VALUES (?,?,?,'orchestrator','question_request_bound',?)",
            (ci, qi, request["directive_id"],
             canonical_bytes(binding).decode("utf-8")))
        if human_authority is not None:
            conn.execute(
                "INSERT INTO decision(cycle_id,question_id,directive_id,actor,type,payload_json) "
                "VALUES (?,?,?,'orchestrator','human_named_import_authority',?)",
                (ci, qi, request["directive_id"],
                 canonical_bytes(human_authority).decode("utf-8")))
        if reference_authority is not None:
            conn.execute(
                "INSERT INTO decision(cycle_id,question_id,actor,type,payload_json) "
                "VALUES (?,?,'orchestrator','import_reference_authority',?)",
                (ci, qi, canonical_bytes(reference_authority).decode("utf-8")))
            origin_id = request["dependency_origin_id"]
            conn.execute(
                "INSERT INTO question_dep(question_id,dep_type,depends_on_question_id,status,created_cycle) "
                "VALUES (?,'question',?,'pending',?)",
                (origin_id, qi, ci))
        return source_authority_hash

    def _spawn_question(self, conn, cycle_id, route, op, local, guard, gver) -> None:
        if self._open_count(conn) >= guard["max_open_questions"]:
            raise ValueError("超出 max_open_questions")
        request = self._spawn_request(
            conn, cycle_id=cycle_id, op=op, gver=gver)
        if route == "goal_amend":   # 只数 goal_amend 路由下的 spawn（对齐 InMemory；非「本轮全部 spawn」）
            cap = self.policy["goal_amend"]["max_spawn_from_goal_amend"]
            already = conn.execute(
                "SELECT count(*) FROM decision WHERE cycle_id=? AND type='spawn_question'",
                (_cnum(cycle_id),)).fetchone()[0]
            if already + 1 > cap:
                raise ValueError("超出 max_spawn_from_goal_amend（goal_amend 护栏）")
        parent = op.get("parent_question_id")
        if op["kind"] == "goal_retarget":
            if request is not None:
                raise ValueError("goal_retarget 不得消费 question request_ref")
            if route != "goal_amend":
                raise ValueError("goal_retarget 仅限 goal_amend 轮（§4.2.4）")
            if parent is not None:
                raise ValueError("goal_retarget 必须 parent=null")
        elif parent is None and request is None:
            raise ValueError(f"spawn parent 不存在: {parent}")
        elif parent is None and request["source_kind"] != "console_directive":
            raise ValueError("只有 exact console request_ref 可派生 parent=null 问题")
        elif op["kind"] == "revalidate":
            # 回看题允许挂在旧 goal_ver 的 closed 问题下；closed 结论本身版本冻结，
            # 新版 applicability 正是通过这条跨版本边完成复核。answer↔parent 的
            # 精确绑定随后由 mark_answer_applicability 在同事务内校验。
            if conn.execute(
                    "SELECT 1 FROM question q WHERE q.id=? AND q.goal_id=1 "
                    "AND q.status IN ('answered','refuted') AND EXISTS ("
                    "SELECT 1 FROM answer a WHERE a.question_id=q.id "
                    "AND a.goal_id=q.goal_id AND a.goal_ver=q.goal_ver AND a.verdict=q.status)",
                    (_qnum(parent),)).fetchone() is None:
                raise ValueError(f"revalidate parent 须为带 answer 的 answered/refuted 问题: {parent}")
        elif conn.execute(
                "SELECT 1 FROM question WHERE id=? AND goal_id=1 AND goal_ver=?",
                (_qnum(parent), gver)).fetchone() is None:
            raise ValueError(f"spawn parent 不存在: {parent}")
        if request is not None and parent is not None:
            self._request_child_capacity(
                conn, parent_qid=parent, guard=guard)
        text, contract, contract_source = normalize_question_contract(
            op.get("text"), op.get("predicate_json"))
        if request is not None and text != request["requested_text"]:
            raise ValueError(
                "request_ref.requested_text 不是 question admission 的规范 text；拒绝隐式改写")
        qid = self._insert_question(
            conn, text, contract, parent,
            (request["question_source"] if request is not None
             else _SPAWN_SOURCE[op["kind"]]), gver,
            born_cycle=_cnum(cycle_id))
        if op.get("local_key"):
            local[op["local_key"]] = qid
        self._record_question_admission(
            conn, cycle_id, qid, operation="spawn_question", text=text,
            contract=contract, contract_source=contract_source)
        source_authority_hash = None
        if request is not None:
            source_authority_hash = self._bind_spawn_request(
                conn, cycle_id=cycle_id, qid=qid, text=text, op=op,
                request=request, gver=gver)
        spawn_payload = {"qid": qid, "kind": op["kind"]}
        if request is not None:
            spawn_payload.update({
                "request_ref": op["request_ref"],
                "source_authority_hash": source_authority_hash,
            })
        spawn_payload_json = (
            json.dumps(spawn_payload)
            if request is None else json.dumps(
                spawn_payload, ensure_ascii=False, sort_keys=True,
                separators=(",", ":")))
        conn.execute("INSERT INTO decision(cycle_id,question_id,actor,type,payload_json) "
                     "VALUES (?,?,'agent','spawn_question',?)", (
                         _cnum(cycle_id), _qnum(qid), spawn_payload_json))

    def _propose_prune(self, conn, cycle_id, op, gver: int) -> None:
        qid = op["question_id"]
        r = conn.execute(
            "SELECT status,goal_id,goal_ver FROM question WHERE id=?", (_qnum(qid),)).fetchone()
        if r is None:
            raise ValueError(f"propose_prune 目标不存在: {qid}")
        if tuple(r[1:]) != (1, gver):
            raise ValueError(f"propose_prune 目标不属于本 cycle 的 current lineage: {qid}")
        if r[0] not in ("open", "inconclusive"):
            raise ValueError(f"仅 open/inconclusive 可剪枝: {qid}({r[0]})")
        # decision(prune_branch) 先行（DB 触发器 trg_q_deadend 要求 dead_end 关闭须有此 decision），再置 dead_end
        conn.execute("INSERT INTO decision(cycle_id,question_id,actor,type,payload_json) "
                     "VALUES (?,?,'agent','prune_branch',?)", (_cnum(cycle_id), _qnum(qid), json.dumps({"reason": op["reason_md"]})))
        conn.execute("UPDATE question SET status='dead_end' WHERE id=?", (_qnum(qid),))
        conn.execute(
            "UPDATE question_dep SET status='blocked' WHERE dep_type='question' "
            "AND depends_on_question_id=? AND status='pending'", (_qnum(qid),))

    def _amend_goal(self, conn, cycle_id, route, op) -> None:
        if route != "goal_amend":
            raise ValueError("amend_goal 仅限 goal_amend 轮")
        ci = _cnum(cycle_id)
        cycle_row = conn.execute(
            "SELECT goal_id,goal_ver,active_question_id,status FROM cycle WHERE id=?", (ci,)).fetchone()
        if cycle_row is None or cycle_row[0] != 1 or cycle_row[3] in ("done", "failed", "aborted"):
            raise ValueError(f"goal_amend cycle 状态非法: {cycle_id}")
        if cycle_row[2] is not None or conn.execute(
                "SELECT 1 FROM question WHERE status='active' LIMIT 1").fetchone() is not None:
            raise ValueError("goal_amend 轮不得携 active 问题；旧轮必须先按旧 goal 收口")
        directives = conn.execute(
            "SELECT d.id,x.payload_json FROM directive d "
            "JOIN decision x ON x.id=d.consumed_decision_id "
            "WHERE d.kind='goal_amend' AND d.status='consumed' AND d.consumed_cycle=? "
            "AND x.directive_id=d.id AND x.actor='human' AND x.type='directive_goal_amend'",
            (ci,)).fetchall()
        if len(directives) != 1:
            raise ValueError(
                f"goal_amend 轮须恰好绑定一条已消费的人类 goal_amend（当前 {len(directives)}）")
        directive_id, decision_raw = directives[0]
        routed = conn.execute(
            "SELECT directive_id FROM decision WHERE cycle_id=? AND actor='orchestrator' "
            "AND type IN ('goal_amend_routed','goal_amend_rebound') "
            "ORDER BY id DESC LIMIT 1", (ci,)).fetchone()
        if routed is None or routed[0] != directive_id:
            raise ValueError(
                f"goal_amend 轮路由绑定与已消费 directive 不一致: routed={routed}, consumed=d{directive_id}")
        try:
            decision_payload = json.loads(decision_raw)
            effect = decision_payload["effect"]
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            raise RuntimeError(f"goal_amend d{directive_id} 消费决策损坏") from error
        if not isinstance(effect, dict):
            raise RuntimeError(f"goal_amend d{directive_id} effect 非 object")

        current = conn.execute(
            "SELECT version,predicate_json FROM goal WHERE id=1 ORDER BY version DESC LIMIT 1").fetchone()
        if current is None:
            raise RuntimeError("goal_amend 时当前 goal 不存在")
        cur, old_predicate_raw = current
        if cycle_row[1] != cur:
            raise ValueError(
                f"goal_amend cycle 绑定 v{cycle_row[1]}，当前 goal 已是 v{cur}；拒绝重复/越版")
        if effect.get("source_goal_ver") != cur or effect.get("target_goal_ver") != cur + 1:
            raise ValueError("goal_amend 消费决策的 source/target goal version 与当前状态不符")
        try:
            json.loads(old_predicate_raw)
        except json.JSONDecodeError as error:
            raise RuntimeError("当前 goal.predicate_json 损坏") from error
        expected_predicate = effect.get("predicate_json")
        if not isinstance(expected_predicate, dict):
            raise ValueError("goal_amend 已确认 predicate_json 非 object")
        if (op.get("new_goal_text") != effect.get("new_goal_text")
                or op.get("rationale_md") != effect.get("rationale_md")
                or op.get("predicate_json") != expected_predicate):
            raise ValueError(
                "amend_goal op 与用户已确认的 new_goal_text/predicate_json/rationale_md 不一致")
        if conn.execute(
                "SELECT 1 FROM goal WHERE id=1 AND created_cycle=? LIMIT 1", (ci,)).fetchone() is not None:
            raise ValueError(f"cycle {cycle_id} 已创建过 goal 版本")

        newv = cur + 1
        predicate_raw = json.dumps(
            expected_predicate, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        conn.execute(
            "INSERT INTO goal(id,version,text,predicate_json,previous_version,created_cycle,directive_id) "
            "VALUES (1,?,?,?,?,?,?)",
            (newv, effect["new_goal_text"], predicate_raw, cur, ci, directive_id))
        # Closed questions/answers/evidence stay on their born version.  Only
        # unresolved questions move in place; stale scores are erased before R3
        # is required to write the complete new-version schedulable frontier.
        conn.execute(
            "UPDATE question SET goal_ver=?,score=NULL,est_cost=NULL "
            "WHERE goal_id=1 AND status IN ('open','inconclusive')", (newv,))
        conn.execute("UPDATE cycle SET goal_ver=? WHERE id=?", (newv, ci))
        self._supersede_pending_imports(conn, cycle_id=ci, old_goal_ver=cur, new_goal_ver=newv)
        canonical_effect = {
            "new_goal_text": effect["new_goal_text"],
            "predicate_json": expected_predicate,
            "rationale_md": effect["rationale_md"],
            "source_goal_ver": cur,
            "target_goal_ver": newv,
        }
        effect_hash = hashlib.sha256(json.dumps(
            canonical_effect, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        conn.execute(
            "INSERT INTO decision(cycle_id,directive_id,actor,type,payload_json) "
            "VALUES (?,?,'agent','goal_amend',?)",
            (ci, directive_id, json.dumps({
                "effect": canonical_effect,
                "effect_sha256": effect_hash,
                "migrated_open_questions": conn.execute(
                    "SELECT count(*) FROM question WHERE goal_id=1 AND goal_ver=? "
                    "AND status IN ('open','inconclusive')", (newv,)).fetchone()[0],
            }, ensure_ascii=False, sort_keys=True)))

    @staticmethod
    def _supersede_pending_imports(conn, *, cycle_id: int, old_goal_ver: int,
                                   new_goal_ver: int) -> None:
        """Append a superseded outcome for old-goal deferred imports that never started."""
        rows = conn.execute(
            "SELECT s.id,s.question_id,s.candidate_id,s.action_cycle,s.candidate_set_hash,"
            "s.selection_key,s.policy_hash,s.license_decision_snapshot_hash,s.license_review_id,s.baseline_id "
            "FROM external_import s JOIN cycle ac ON ac.id=s.action_cycle "
            "JOIN baseline b ON b.id=s.baseline_id "
            "WHERE s.action='selected_for_materialization' AND ac.goal_id=1 AND ac.goal_ver=? "
            "AND b.status='planned' AND NOT EXISTS ("
            "SELECT 1 FROM build_target bt WHERE bt.baseline_id=s.baseline_id) AND NOT EXISTS ("
            "SELECT 1 FROM external_import x WHERE x.question_id=s.question_id "
            "AND x.candidate_id=s.candidate_id AND x.action_cycle=s.action_cycle "
            "AND x.candidate_set_hash=s.candidate_set_hash AND x.selection_key=s.selection_key "
            "AND x.policy_hash=s.policy_hash "
            "AND x.action IN ('imported','materialize_failed','superseded')) ORDER BY s.id",
            (old_goal_ver,)).fetchall()
        for (source_id, question_id, candidate_id, action_cycle, candidate_set_hash,
             selection_key, policy_hash, license_hash, license_review_id, baseline_id) in rows:
            reason = {
                "reason": "goal_amend_superseded_before_materialization",
                "source_external_import_id": source_id,
                "superseded_cycle": cycle_id,
                "source_goal_ver": old_goal_ver,
                "target_goal_ver": new_goal_ver,
            }
            conn.execute(
                "INSERT INTO decision(cycle_id,question_id,actor,type,payload_json) "
                "VALUES (?,?,'orchestrator','import_superseded_by_goal_amend',?)",
                (cycle_id, question_id,
                 json.dumps(reason, ensure_ascii=False, sort_keys=True)))
            conn.execute(
                "INSERT INTO external_import(question_id,candidate_id,action,action_cycle,"
                "candidate_set_hash,selection_key,policy_hash,license_decision_snapshot_hash,"
                "license_review_id,reason_json) VALUES (?,?,'superseded',?,?,?,?,?,?,?)",
                (question_id, candidate_id, action_cycle, candidate_set_hash, selection_key,
                 policy_hash, license_hash, license_review_id,
                 json.dumps(reason, ensure_ascii=False, sort_keys=True)))
            conn.execute(
                "UPDATE baseline SET status='abandoned' WHERE id=? AND status='planned'",
                (baseline_id,))
            conn.execute(
                "UPDATE question_dep SET status='blocked' WHERE question_id=? "
                "AND dep_type='baseline' AND depends_on_baseline_id=? AND status='pending'",
                (question_id, baseline_id))

    def _seed_applicability(self, conn, cycle_id, route, op) -> None:
        if route != "goal_amend":
            raise ValueError("seed_applicability_audit 仅限 goal_amend 轮")
        cnum = _cnum(cycle_id)
        raw_by_id = {}                                    # 去重（同 op 内重复 answer_id 算一个），保留首个原串供报错
        for a in op["answer_ids"]:
            raw_by_id.setdefault(_anum(a), a)
        new_ids = list(raw_by_id)
        if not new_ids:
            raise ValueError("seed_applicability_audit.answer_ids 不得为空")
        gver = self._goal_ver()
        for ai in new_ids:
            answer = conn.execute(
                "SELECT a.goal_id,a.goal_ver,q.status FROM answer a "
                "JOIN question q ON q.id=a.question_id WHERE a.id=?", (ai,)).fetchone()
            if answer is None:
                raise ValueError(f"seed_applicability_audit 引用悬空 answer: {raw_by_id[ai]}")
            if answer[0] != 1 or answer[1] >= gver or answer[2] not in ("answered", "refuted"):
                raise ValueError(
                    f"seed_applicability_audit 仅允许当前 goal 旧版本的 closed answer: {raw_by_id[ai]}")
        # max_closed_revalidate_per_cycle = 本轮**累计**上限（按策略键名 per_cycle 语义；修正 InMemory per-op
        # 疏漏）。计数按 audit_cycle=本轮；upsert 把 audit_cycle 迁到本轮（下方 excluded.audit_cycle），故
        # 再 seed 旧轮同版行也计入本轮预算——否则分批 re-seed 旧行可绕过上限（codex 第2轮 BLOCKER）。
        limit = self.policy["goal_amend"]["max_closed_revalidate_per_cycle"]
        seeded = conn.execute("SELECT count(*) FROM answer_applicability WHERE audit_cycle=?", (cnum,)).fetchone()[0]
        ph = ",".join("?" * len(new_ids))
        already = conn.execute(f"SELECT count(*) FROM answer_applicability WHERE audit_cycle=? AND answer_id IN ({ph})",
                               (cnum, *new_ids)).fetchone()[0]
        if seeded + (len(new_ids) - already) > limit:
            raise ValueError("超出 max_closed_revalidate_per_cycle")
        for ai in new_ids:
            conn.execute("INSERT INTO answer_applicability(answer_id,goal_id,goal_ver,audit_cycle,status,rationale_md) "
                         "VALUES (?,1,?,?,'pending',?) "
                         "ON CONFLICT(answer_id,goal_id,goal_ver) DO UPDATE SET status='pending', "
                         "rationale_md=excluded.rationale_md, audit_cycle=excluded.audit_cycle",
                         (ai, gver, cnum, op.get("rationale_md", "")))

    def _mark_applicability(self, conn, cycle_id, op, local, gver) -> None:
        """§4.2.4 绑定判据：answer 存在 + 回看题 source=revalidate 且 parent=被回看 answer 所属问题
        + 每轮回看上限（answer_review）。计数在全部校验通过后（借 decision 计数，天然事务回滚）。"""
        done = conn.execute("SELECT count(*) FROM decision WHERE cycle_id=? AND type='answer_review'",
                            (_cnum(cycle_id),)).fetchone()[0]
        if done + 1 > self.policy["answer_review"]["max_reviews_per_cycle"]:
            raise ValueError("超出 max_reviews_per_cycle（answer_review 护栏）")
        ar = conn.execute("SELECT question_id FROM answer WHERE id=?", (_anum(op["answer_id"]),)).fetchone()
        if ar is None:
            raise ValueError(f"answer 不存在: {op['answer_id']}")
        status = op["status"]
        spawned = op.get("spawned_question_ref")
        spawned_qid = local.get(spawned, spawned) if spawned else None
        if status in ("needs_revalidation", "contradicted"):
            qr = conn.execute("SELECT source,parent_id,goal_id,goal_ver,status FROM question WHERE id=?",
                              (_qnum(spawned_qid),)).fetchone() if spawned_qid else None
            if qr is None or qr[0] != "revalidate":
                raise ValueError("needs_revalidation/contradicted 须指向 source=revalidate 的回看题")
            if qr[1] != ar[0]:   # 回看题 parent 须为被回看 answer 所属问题
                raise ValueError("回看题 parent 须为被回看 answer 所属问题（§4.2.4）")
            if tuple(qr[2:4]) != (1, gver) or qr[4] in ("answered", "refuted", "dead_end"):
                raise ValueError("回看题须属于本 cycle 的 current goal 且仍非终态")
        conn.execute(
            "INSERT INTO answer_applicability(answer_id,goal_id,goal_ver,audit_cycle,status,rationale_md,spawned_question_id) "
            "VALUES (?,1,?,?,?,?,?) "
            "ON CONFLICT(answer_id,goal_id,goal_ver) DO UPDATE SET status=excluded.status, "
            "rationale_md=excluded.rationale_md, spawned_question_id=excluded.spawned_question_id, "
            "audit_cycle=excluded.audit_cycle",   # 审计归属迁到本轮（与 seed 一致，codex 第2轮）
            (_anum(op["answer_id"]), gver, _cnum(cycle_id), status, op["rationale_md"],
             _qnum(spawned_qid) if spawned_qid else None))
        conn.execute("INSERT INTO decision(cycle_id,actor,type,payload_json) VALUES (?,'agent','answer_review',?)",
                     (_cnum(cycle_id), json.dumps({"answer_id": op["answer_id"], "status": status})))

    # -- 未在本检查点落地 -------------------------------------------------------
    def close_question(self, cycle_id: str, question_id: str, verdict: str,
                       evidence: List[Dict[str, Any]], answer_md: str) -> str:
        raise NotImplementedError(
            "关问写 answer+evidence+I3 = gate_close_question（业务门禁，CP2.3；其证据须引用池注册的真 "
            "evaluation/metric_result，CP2.4 才有）——不在 M1b（纯状态机）范围")

    def consume_directive(self, directive_id: str) -> None:
        raise NotImplementedError("directive 消费归 console.Console.consume_directive（M5 CP6.1：效果+DECISION+"
                                  "状态迁移单事务；本 Protocol 位保留给未来 statestore 级接线）")

    # -- bundle cursor（进程内瞬态调度位；非 DDL 持久列，§4.2.1 由 build_target 状态派生的恢复留 M3/M4） --
    def bundle_cursor(self, cycle_id: str) -> Optional[str]:
        return self._bundle_cursor.get(cycle_id)

    def set_bundle_cursor(self, cycle_id: str, target_key: Optional[str]) -> None:
        self._bundle_cursor[cycle_id] = target_key
