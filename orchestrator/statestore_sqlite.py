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

import json
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from .interfaces import Cycle, Route, Selection
from .writedaemon import WriteDaemon

_TERMINAL_Q = {"answered", "refuted", "dead_end"}
_ROUTES = ("bootstrap", "attack", "decompose", "reuse_only", "eval_only", "goal_amend", "dependency_wait")
_SPAWN_SOURCE = {  # §4.2.4 spawn_question kind → question.source（对齐 DDL 枚举）
    "diagnosis": "agent", "followup": "agent", "revalidate": "revalidate",
    "import_reference": "agent", "goal_retarget": "goal_amend",
}


def _cid(n: int) -> str: return f"c{n}"
def _qid(n: int) -> str: return f"q{n}"
def _aid(n: int) -> str: return f"a{n}"


def _decode(s: Any, prefix: str) -> int:
    """按类型前缀（c/q/a/g）解码为整型主键，**校验前缀**——防类型错 id（如把 'c1' 当问题）静默命中
    别的表的同号行。对齐 InMemory 的 dict-key 类型天然隔离（其键含前缀、跨类型不撞）。"""
    if not (isinstance(s, str) and len(s) > 1 and s[0] == prefix and s[1:].isdigit()):
        raise ValueError(f"id 前缀/格式非法（期望 {prefix}<数字>）: {s!r}")
    return int(s[1:])


def _cnum(s: Any) -> int: return _decode(s, "c")
def _qnum(s: Any) -> int: return _decode(s, "q")
def _anum(s: Any) -> int: return _decode(s, "a")


def _qnum_opt(s: Any) -> Optional[int]:
    """问题 id 安全解码：形如 q<数字> 才转 int，否则 None——供 selection 处理可能是未解析 local_key /
    畸形 / 类型错 id 的入参（给干净「不存在」拒因，而非裸 ValueError）。"""
    return int(s[1:]) if isinstance(s, str) and len(s) > 1 and s[0] == "q" and s[1:].isdigit() else None


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
        # goal_amend 轮 spawn 计数（in-memory，语义同 InMemoryStateStore._spawns_in_cycle：只数
        # goal_amend 路由下的 spawn，不同于「本轮全部 spawn」）。与 _local_maps 同属**进程内投影**，
        # 须随写事务回滚一并复原（否则 SQLite 复用回滚 rowid 会致 local_key 静默错绑，见 _projection_snapshot）。
        self._spawns_in_cycle: Dict[str, int] = {}

    # -- 事务 / 读写原语 --------------------------------------------------------
    # 进程内投影（_local_maps / _spawns_in_cycle）随 DB 事务同生共死：DB 回滚而投影不回滚 = 在
    # 「本检查点要焊死的那一层」留半写（SQLite 复用回滚 rowid → 陈旧 local_key 静默错绑到别的问题）。
    # 故凡开写事务处（atomic() 外层 / 独立 apply_tree_ops）都先快照、异常回滚时复原（对齐
    # InMemoryStateStore._tree_snapshot/_tree_restore）。写计数（answer_review）走 DB decision 计数，
    # 随事务天然回滚，无需在此快照。
    def _projection_snapshot(self):
        return ({k: dict(v) for k, v in self._local_maps.items()},
                dict(self._spawns_in_cycle), dict(self._bundle_cursor))

    def _projection_restore(self, snap) -> None:
        self._local_maps, self._spawns_in_cycle, self._bundle_cursor = snap

    @contextmanager
    def atomic(self) -> Iterator[None]:
        """把多个写方法裹进同一事务（M3 Advancer 用于答题收尾全序，§4.2.5(a)）。

        **契约**：块内任一写方法抛异常，调用方**不得吞掉**——必须让它传播以中止整个 atomic
        （否则该方法的半途 DB 写入会随本事务一起提交、且投影不复原）。M1b 无跨方法回滚的 savepoint
        （方法级独立回滚留作后续硬化）；M3 Advancer 遵此契约：任一步失败即整轮 advance 失败。
        """
        snap = self._projection_snapshot()
        try:
            with self.daemon.transaction() as conn:
                self._active_conn = conn
                yield
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

    def _inflight_row(self):
        """在途（非终态）轮的 id 行或 None——open_or_resume_cycle 与 inflight_cycle 共用一处口径（防漂移，内审 NIT）。
        单驱动器模型下至多一条。"""
        return self._q1("SELECT id FROM cycle WHERE status NOT IN ('done','failed','aborted') ORDER BY id LIMIT 1")

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
        r = self._q1("SELECT id FROM cycle WHERE status='done' AND route IS NOT NULL ORDER BY id DESC LIMIT 1")
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
        with self._write() as conn:
            conn.execute("UPDATE cycle SET status=?, finished_at=CURRENT_TIMESTAMP WHERE id=?", (status, _cnum(cycle_id)))
        # 无条件清本轮进程内投影（防长跑无界增长）——三者均在 _projection_snapshot 内，故若在 atomic()
        # 中且外层回滚，会随投影一并复原（cycle 未真终态时投影不丢）。
        self._local_maps.pop(cycle_id, None)
        self._spawns_in_cycle.pop(cycle_id, None)
        self._bundle_cursor.pop(cycle_id, None)

    # -- question 调度可见性 ----------------------------------------------------
    def _pending_dep_count(self, qid_int: int) -> int:
        return self._q1("SELECT count(*) FROM question_dep WHERE question_id=? AND status='pending'", (qid_int,))[0]

    def is_schedulable(self, qid: str, *, for_intent: str = "attack") -> bool:
        r = self._q1("SELECT status, visit_count FROM question WHERE id=?", (_qnum(qid),))
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
        for r in self._qall("SELECT id,text,status,visit_count,score,est_cost,parent_id FROM question ORDER BY id"):
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
            cur = conn.execute("SELECT id FROM cycle WHERE status NOT IN ('done','failed','aborted') ORDER BY id").fetchall()
            if len(cur) != 1:
                raise ValueError(f"activate_question 需恰一非终态 cycle（当前 {len(cur)}）")
            conn.execute("UPDATE question SET status='active' WHERE id=?", (qi,))
            conn.execute("UPDATE cycle SET active_question_id=? WHERE id=?", (qi, cur[0][0]))

    def mark_inconclusive(self, question_id: str) -> None:
        qi = _qnum(question_id)
        r = self._q1("SELECT status FROM question WHERE id=?", (qi,))
        if r is None or r[0] != "active":
            raise ValueError(f"仅 active 可置 inconclusive: {question_id}({r[0] if r else '缺'})")
        with self._write() as conn:
            conn.execute("UPDATE question SET status='inconclusive', visit_count=visit_count+1 WHERE id=?", (qi,))
            conn.execute("UPDATE cycle SET active_question_id=NULL WHERE active_question_id=?", (qi,))

    def release_question(self, question_id: str) -> None:
        """active→open（cycle failed/aborted / dependency_wait；不增 visit，§4.2.3）。"""
        qi = _qnum(question_id)
        with self._write() as conn:
            conn.execute("UPDATE question SET status='open' WHERE id=? AND status='active'", (qi,))
            conn.execute("UPDATE cycle SET active_question_id=NULL WHERE active_question_id=?", (qi,))

    # -- dep ------------------------------------------------------------------
    def record_question_dep(self, question_id: str, *, dep_type: str, target: str) -> None:
        qi = _qnum(question_id)
        if self._q1("SELECT 1 FROM question WHERE id=?", (qi,)) is None:
            raise ValueError(f"dep 主体问题不存在: {question_id}")
        if dep_type == "question":
            ti = _qnum(target)
            if ti == qi:
                raise ValueError("禁自依赖")
            if self._q1("SELECT 1 FROM question WHERE id=?", (ti,)) is None:
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
        # 依赖满足口径 = 目标问题 answered/refuted（与 InMemory 等价）。**dead_end 目标不解除依赖**——
        # 与 M0 一致。⚠️ 已知悬案（codex 第2轮 SHOULD）：若父问题依赖的子问题被 propose_prune 成 dead_end，
        # 父的 pending dep 将永久挂住、不回可调度集。规格未明「被剪子问题是否应满足父依赖」；本检查点保持
        # M0 语义（真相侧不擅自改判），留待 M3 Advancer / M6 长跑跑通 decompose→prune→聚合全链时按规格定夺。
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
        if sel.next_intent not in ("attack", "decompose", "terminate"):
            raise ValueError(f"next_intent 非法: {sel.next_intent}")
        next_qid = self._resolve_local(cycle_id, sel.next_question_id)
        next_int = _qnum_opt(next_qid)     # 未解析 local_key / 畸形 id → None → 走干净「不存在」拒因
        if sel.next_intent == "terminate":
            if next_qid is not None:
                raise ValueError("terminate 时 next_question_id 必须为 null")
        else:
            if next_int is None or self._q1("SELECT 1 FROM question WHERE id=?", (next_int,)) is None:
                raise ValueError(f"next_question_id 缺失或不存在: {sel.next_question_id}")
            if not self.is_schedulable(next_qid, for_intent=sel.next_intent):
                raise ValueError(f"目标问题不可调度: {next_qid}")
        with self._write() as conn:
            for row in sel.scores:   # 唯一维护点：写回 question.score/est_cost（local_key 同样解析）
                ri = _qnum_opt(self._resolve_local(cycle_id, row.get("question_id")))
                if ri is None or conn.execute("SELECT 1 FROM question WHERE id=?", (ri,)).fetchone() is None:
                    raise ValueError(f"scores 引用的问题不存在: {row.get('question_id')}（不静默丢弃）")
                if "est_cost" in row:   # 显式提供（含 null）才改；缺省保留旧值（对齐 InMemory row.get 语义）
                    conn.execute("UPDATE question SET score=?, est_cost=? WHERE id=?", (row.get("score"), row["est_cost"], ri))
                else:
                    conn.execute("UPDATE question SET score=? WHERE id=?", (row.get("score"), ri))
            conn.execute("UPDATE cycle SET next_question_id=?, next_intent=? WHERE id=?",
                         (next_int, sel.next_intent, _cnum(cycle_id)))

    # -- 七 op 树操作（单事务原子；§4.2.4 封闭词表 / §4.2.5 原子性） --------------
    def apply_tree_ops(self, cycle_id: str, ops: List[Dict[str, Any]]) -> None:
        with self._write() as conn:
            self._apply_ops(conn, cycle_id, ops)

    def _apply_ops(self, conn, cycle_id: str, ops: List[Dict[str, Any]]) -> None:
        route = self._q1("SELECT route FROM cycle WHERE id=?", (_cnum(cycle_id),))[0]
        local = self._local_maps.setdefault(cycle_id, {})
        guard = self.policy["tree_guard"]
        gver = self._goal_ver()
        for op in ops:
            kind = op.get("op")
            if kind == "create_root":
                if route != "bootstrap":
                    raise ValueError("create_root 仅限 bootstrap 轮（§4.2.4）")
                qid = self._insert_question(conn, op["text"], None, "agent", gver)
                if op.get("local_key"):
                    local[op["local_key"]] = qid
                conn.execute("INSERT INTO decision(cycle_id,question_id,actor,type,payload_json) "
                             "VALUES (?,?,'agent','create_root',?)", (_cnum(cycle_id), _qnum(qid), json.dumps({"qid": qid})))
            elif kind == "add_children":
                self._add_children(conn, cycle_id, route, op, local, guard, gver)
            elif kind == "spawn_question":
                self._spawn_question(conn, cycle_id, route, op, local, guard, gver)
            elif kind == "mark_answer_applicability":
                self._mark_applicability(conn, cycle_id, op, local, gver)
            elif kind == "propose_prune":
                self._propose_prune(conn, cycle_id, op)
            elif kind == "amend_goal":
                self._amend_goal(conn, cycle_id, route, op)
                gver = self._goal_ver()   # 版本已升，后续 op 用新版
            elif kind == "seed_applicability_audit":
                self._seed_applicability(conn, cycle_id, route, op)
            else:
                raise ValueError(f"op 不在封闭词表: {kind}")

    def _insert_question(self, conn, text: str, parent_qid: Optional[str], source: str, gver: int,
                         status: str = "open") -> str:
        cur = conn.execute(
            "INSERT INTO question(parent_id,goal_id,goal_ver,born_goal_ver,text,status,source) "
            "VALUES (?,1,?,?,?,?,?)",
            (_qnum(parent_qid) if parent_qid else None, gver, gver, text, status, source))
        return _qid(cur.lastrowid)

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
        pr = conn.execute("SELECT status FROM question WHERE id=?", (_qnum(parent_qid),)).fetchone()
        if pr is None:
            raise ValueError(f"decompose 父问题不存在: {parent_qid}")
        if pr[0] != "active":
            raise ValueError(f"decompose 父问题须为 active（当前 {pr[0]}；终态/未选中不可分解）")
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
            cq = self._insert_question(conn, ch["text"], parent_qid, "decompose", gver)
            local[ch["local_key"]] = cq
            child_qids.append(cq)
            conn.execute("INSERT INTO question_dep(question_id,dep_type,depends_on_question_id,status,created_cycle) "
                         "VALUES (?,'question',?,'pending',?)", (pqi, _qnum(cq), _cnum(cycle_id)))
        conn.execute("UPDATE question SET decompose_count=decompose_count+1, status='open' WHERE id=?", (pqi,))
        conn.execute("UPDATE cycle SET active_question_id=NULL WHERE active_question_id=?", (pqi,))  # 父已释放
        conn.execute("INSERT INTO decision(cycle_id,question_id,actor,type,payload_json) VALUES (?,?,'agent','decompose',?)",
                     (_cnum(cycle_id), _qnum(parent_qid), json.dumps({"parent": parent_qid, "children": child_qids})))

    def _spawn_question(self, conn, cycle_id, route, op, local, guard, gver) -> None:
        if self._open_count(conn) >= guard["max_open_questions"]:
            raise ValueError("超出 max_open_questions")
        if route == "goal_amend":   # 只数 goal_amend 路由下的 spawn（对齐 InMemory；非「本轮全部 spawn」）
            cap = self.policy["goal_amend"]["max_spawn_from_goal_amend"]
            if self._spawns_in_cycle.get(cycle_id, 0) + 1 > cap:
                raise ValueError("超出 max_spawn_from_goal_amend（goal_amend 护栏）")
            self._spawns_in_cycle[cycle_id] = self._spawns_in_cycle.get(cycle_id, 0) + 1
        parent = op.get("parent_question_id")
        if op["kind"] == "goal_retarget":
            if route != "goal_amend":
                raise ValueError("goal_retarget 仅限 goal_amend 轮（§4.2.4）")
            if parent is not None:
                raise ValueError("goal_retarget 必须 parent=null")
        elif parent is None or conn.execute("SELECT 1 FROM question WHERE id=?", (_qnum(parent),)).fetchone() is None:
            raise ValueError(f"spawn parent 不存在: {parent}")
        qid = self._insert_question(conn, op["text"], parent, _SPAWN_SOURCE[op["kind"]], gver)
        if op.get("local_key"):
            local[op["local_key"]] = qid
        conn.execute("INSERT INTO decision(cycle_id,question_id,actor,type,payload_json) "
                     "VALUES (?,?,'agent','spawn_question',?)", (_cnum(cycle_id), _qnum(qid), json.dumps({"qid": qid, "kind": op["kind"]})))

    def _propose_prune(self, conn, cycle_id, op) -> None:
        qid = op["question_id"]
        r = conn.execute("SELECT status FROM question WHERE id=?", (_qnum(qid),)).fetchone()
        if r is None:
            raise ValueError(f"propose_prune 目标不存在: {qid}")
        if r[0] not in ("open", "inconclusive"):
            raise ValueError(f"仅 open/inconclusive 可剪枝: {qid}({r[0]})")
        # decision(prune_branch) 先行（DB 触发器 trg_q_deadend 要求 dead_end 关闭须有此 decision），再置 dead_end
        conn.execute("INSERT INTO decision(cycle_id,question_id,actor,type,payload_json) "
                     "VALUES (?,?,'agent','prune_branch',?)", (_cnum(cycle_id), _qnum(qid), json.dumps({"reason": op["reason_md"]})))
        conn.execute("UPDATE question SET status='dead_end' WHERE id=?", (_qnum(qid),))

    def _amend_goal(self, conn, cycle_id, route, op) -> None:
        if route != "goal_amend":
            raise ValueError("amend_goal 仅限 goal_amend 轮")
        cur = conn.execute("SELECT max(version) FROM goal WHERE id=1").fetchone()[0]
        newv = cur + 1
        old = conn.execute("SELECT predicate_json FROM goal WHERE id=1 AND version=?", (cur,)).fetchone()[0]
        pj = json.dumps(op["predicate_json"], ensure_ascii=False) if op.get("predicate_json") else old
        conn.execute("INSERT INTO goal(id,version,text,predicate_json,previous_version,created_cycle) "
                     "VALUES (1,?,?,?,?,?)", (newv, op["new_goal_text"], pj, cur, _cnum(cycle_id)))
        # 未关闭问题迁到新版本（closed 问题受 trg_q_closed_goalfrozen 保护、留旧版）
        conn.execute("UPDATE question SET goal_ver=? WHERE status IN ('open','inconclusive','active')", (newv,))
        conn.execute("INSERT INTO decision(cycle_id,actor,type,payload_json) VALUES (?,'agent','goal_amend',?)",
                     (_cnum(cycle_id), json.dumps({"new_ver": newv})))

    def _seed_applicability(self, conn, cycle_id, route, op) -> None:
        if route != "goal_amend":
            raise ValueError("seed_applicability_audit 仅限 goal_amend 轮")
        cnum = _cnum(cycle_id)
        raw_by_id = {}                                    # 去重（同 op 内重复 answer_id 算一个），保留首个原串供报错
        for a in op["answer_ids"]:
            raw_by_id.setdefault(_anum(a), a)
        new_ids = list(raw_by_id)
        for ai in new_ids:
            if conn.execute("SELECT 1 FROM answer WHERE id=?", (ai,)).fetchone() is None:
                raise ValueError(f"seed_applicability_audit 引用悬空 answer: {raw_by_id[ai]}")
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
        gver = self._goal_ver()
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
            qr = conn.execute("SELECT source, parent_id FROM question WHERE id=?",
                              (_qnum(spawned_qid),)).fetchone() if spawned_qid else None
            if qr is None or qr[0] != "revalidate":
                raise ValueError("needs_revalidation/contradicted 须指向 source=revalidate 的回看题")
            if qr[1] != ar[0]:   # 回看题 parent 须为被回看 answer 所属问题
                raise ValueError("回看题 parent 须为被回看 answer 所属问题（§4.2.4）")
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
