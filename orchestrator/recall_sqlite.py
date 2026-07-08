"""SqliteRecall / SqliteCtx / 复用判定 selector —— 召回与复用的真实现（M2：§3.6.2 渐进四级 + §4.1.5 O(1)）。

与 M0 StubRecall/StubCtx 并存不替换（M0 driver 仍用桩、基线绿）；M3 Advancer / 编译器检索区接。
用普通只读连接（读 DB 卡片/池；不写库）。

**复用判定 O(1)（§4.1.5，M2 验收核心）**：命中走测量索引 `(variant_id,protocol_id,protocol_ver)` 上的成功
evaluation → 零执行引用历史；EXPLAIN QUERY PLAN 证明走该索引、非全表扫。`parser_result_suspect(ea)` 是 parser
据 execution_observation 派生的负向谓词——**M2 无真 parser（M4）→ 注册桩恒返 0**（不挡任何复用；真派生 M4 落）。

**渐进四级召回（§3.6.2，可停）**：① 卡片初筛（方法族/基线卡文本+tag）② 变体矩阵中筛 ③ 测量索引精确 O(1)
④ ctx-fetch 深潜。每级独立可停（哪级够用停哪级）。M2 用 card_md 文本 LIKE + tag（嵌入语义召回无模型→defer）。
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from .interfaces import Hit, RecallSpec, Ref


def register_parser_suspect_stub(conn: sqlite3.Connection) -> None:
    """注册 parser_result_suspect(attempt_id)->0 的 M2 桩（真派生谓词 M4 落；恒 0 = 不挡复用）。
    必须在跑复用 selector 前对该连接注册一次。"""
    conn.create_function("parser_result_suspect", 1, lambda _attempt_id: 0, deterministic=True)


# 复用判定规范 SQL（§4.1.5）；required 动态展开为 VALUES 行。命中 = 返回行覆盖全部 required (metric_id,metric_ver)。
_REUSE_SQL = """
WITH required(metric_id, metric_ver) AS (VALUES {values}),
ranked AS (
  SELECT r.metric_id, r.metric_ver, mr.id AS metric_result_id, mr.value,
         ROW_NUMBER() OVER (PARTITION BY r.metric_id, r.metric_ver
           ORDER BY (ea.id = e.canonical_attempt_id) DESC, ea.attempt_no DESC) AS rk
  FROM required r
  JOIN evaluation e         ON e.variant_id = ? AND e.protocol_id = ? AND e.protocol_ver = ? AND e.status = 'success'
  JOIN evaluation_attempt ea ON ea.evaluation_id = e.id AND ea.status = 'success'
                            AND ea.env_hash = ? AND parser_result_suspect(ea.id) = 0
  JOIN metric_result mr     ON mr.evaluation_id = e.id AND mr.evaluation_attempt_id = ea.id
                            AND mr.scope = 'aggregate' AND mr.metric_id = r.metric_id AND mr.metric_ver = r.metric_ver
  LEFT JOIN build_target bt ON bt.id = COALESCE(ea.build_target_id, e.build_target_id)
  WHERE (COALESCE(ea.build_target_id, e.build_target_id) IS NULL OR bt.status = 'complete')
)
SELECT metric_id, metric_ver, metric_result_id, value FROM ranked WHERE rk = 1
"""


def _reuse_query(required: List[Tuple[int, int]], variant_id, protocol_id, protocol_ver, env_hash) -> Tuple[str, list]:
    """构造复用 selector（含 required VALUES 展开）+ **完整参数序列**。
    参数序 = required 的 (mid,mver) 全展开**在前**（WITH VALUES 子句先出现），再 variant/protocol/ver/env（JOIN 子句）。"""
    if not required:
        raise ValueError("reuse selector 须至少一个 required metric")
    values = ", ".join(["(?, ?)"] * len(required))
    params: list = []
    for mid, mver in required:
        params += [mid, mver]
    params += [variant_id, protocol_id, protocol_ver, env_hash]
    return _REUSE_SQL.format(values=values), params


def reuse_selector(conn: sqlite3.Connection, *, variant_id: int, protocol_id: int, protocol_ver: int,
                   env_hash: str, required: List[Tuple[int, int]]) -> Dict[str, Any]:
    """复用判定（§4.1.5）：同 variant + protocol@ver + 全 required 指标齐 + env_hash 精确 + 非存疑 + target complete
    → **命中**（零执行引用历史 evaluation）。返回 {hit, results:[{metric_id,metric_ver,metric_result_id,value}]}；
    命中 ⟺ results 覆盖全部 required pair；缺任一 → hit=False（上层落 target）。"""
    sql, params = _reuse_query(required, variant_id, protocol_id, protocol_ver, env_hash)
    rows = _exec_with_suspect(conn, sql, params)
    results = [{"metric_id": m, "metric_ver": v, "metric_result_id": mr, "value": val} for m, v, mr, val in rows]
    covered = {(r["metric_id"], r["metric_ver"]) for r in results}
    hit = covered.issuperset(set(required))
    return {"hit": hit, "results": results}


def _exec_with_suspect(conn: sqlite3.Connection, sql: str, params: list):
    """跑含 parser_result_suspect() 的 selector；未注册该函数时给可行动错误（非裸 OperationalError）。
    **不在此自动注册**——否则会覆盖 M4 的真派生谓词（调用方须显式 register：M2/M3 stub、M4 真）。"""
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        if "parser_result_suspect" in str(e):
            raise RuntimeError(
                "复用 selector 需先在该连接注册 parser_result_suspect（M2/M3 调 register_parser_suspect_stub；"
                "M4 注册真派生）") from e
        raise


def explain_reuse(conn: sqlite3.Connection, *, variant_id: int, protocol_id: int, protocol_ver: int,
                  env_hash: str, required: List[Tuple[int, int]]) -> List[str]:
    """EXPLAIN QUERY PLAN 文本（供验收断言走测量索引、非全表扫 evaluation）。前置：须先 register_parser_suspect_stub。"""
    sql, params = _reuse_query(required, variant_id, protocol_id, protocol_ver, env_hash)
    rows = _exec_with_suspect(conn, "EXPLAIN QUERY PLAN " + sql, params)
    return [r[-1] for r in rows]   # 每行末列 = detail 文本


class SqliteCtx:
    """渐进召回第 4 级：ctx-fetch 按 ref 深潜冷数据（只读）。M2：读 execution_log 产物 ref（供 reasoning 综合判断）。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    _EXECLOG = "execlog:"

    def fetch(self, ref: Ref) -> str:
        """ref 形如 'execlog:<id>' → 返回该 execution_log 的 ref+hash 元信息（真内容读文件系统留 M4）。
        未知 ref 形态 → 诚实返回未知说明（不臆造）。isascii()+isdigit() 双护：防 '²' 等 Unicode 数字过
        isdigit 却让 int() 抛（守「不 crash、只诚实」契约）。"""
        body = ref[len(self._EXECLOG):] if isinstance(ref, str) and ref.startswith(self._EXECLOG) else None
        if body is not None and body.isascii() and body.isdigit():
            row = self.conn.execute("SELECT log_kind, ref, content_hash, bytes FROM execution_log WHERE id=?",
                                    (int(body),)).fetchone()
            if row is None:
                return f"（ctx-fetch: execution_log {ref} 不存在）"
            return f"execution_log[{row[0]}] ref={row[1]} hash={row[2]} bytes={row[3]}（原始内容按 ref 读文件，M4 真流水）"
        return f"（ctx-fetch: 未知 ref 形态 {ref!r}，M2 仅支持 execlog:<id>）"


class SqliteRecall:
    """渐进四级召回（§3.6.2），每级独立可停。M2：卡片文本 LIKE + tag（嵌入 defer）+ 变体矩阵 + 测量索引 O(1)。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def level1_cards(self, query: str, k: int = 5) -> List[Hit]:
        """① 初筛（§3.6.2 第1级）：语义检索 + faceted tag → 命中方法族/基线卡。ORDER BY id 定序。
        - 语义检索：M2 无嵌入模型 → 用 card_md LIKE 作 v1 stand-in（真嵌入召回 defer）。
        - **faceted tag**：tag 挂 baseline 级（`baseline_tag`，§4.5.3）；基线卡 ref_id=baseline.id，其 tag LIKE query
          亦命中（即使 card_md 不含 query）——v1 用纯自由 tag、不强求规整（§3.6.2）。
        参数化（无注入）；query 中的 %/_ 会当 LIKE 通配——v1 模糊匹配可接受。"""
        like = f"%{query}%"
        rows = self.conn.execute(
            "SELECT card_type, ref_id, card_md, src_hash FROM card c "
            "WHERE c.card_type IN ('baseline','family','protocol') AND ("
            "  c.card_md LIKE ?"
            "  OR (c.card_type='baseline' AND EXISTS ("
            "        SELECT 1 FROM baseline_tag bt WHERE bt.baseline_id=c.ref_id AND bt.tag LIKE ?))"
            ") ORDER BY c.id LIMIT ?",
            (like, like, k)).fetchall()
        return [Hit(ref=f"card:{ct}:{rid}", card_md=md, score=1.0) for ct, rid, md, _sh in rows]

    def level2_variants(self, baseline_id: int) -> List[Hit]:
        """② 中筛：读基线的变体矩阵（该 baseline 下 variant + result_summary）。"""
        rows = self.conn.execute(
            "SELECT id, variant_key, result_summary, status FROM variant WHERE baseline_id=? ORDER BY id",
            (baseline_id,)).fetchall()
        return [Hit(ref=f"variant:{vid}", card_md=f"variant {vk}（{st}）: {rs or ''}", score=1.0)
                for vid, vk, rs, st in rows]

    def level3_reuse(self, *, variant_id: int, protocol_id: int, protocol_ver: int,
                     env_hash: str, required: List[Tuple[int, int]]) -> Dict[str, Any]:
        """③ 精确：测量索引 O(1) 复用判定（§4.1.5）。返回 reuse_selector 结果。
        前置：conn 须已 register_parser_suspect_stub（未注册 → reuse_selector 抛可行动 RuntimeError）。"""
        return reuse_selector(self.conn, variant_id=variant_id, protocol_id=protocol_id,
                              protocol_ver=protocol_ver, env_hash=env_hash, required=required)

    # ④ 深潜 = SqliteCtx.fetch（另类，只读 MCP）。

    def query(self, spec: RecallSpec) -> List[Hit]:
        """Recall Protocol 入口：M2 默认走第 1 级卡片初筛（够用即停；深层由编译器按需逐级调）。"""
        return self.level1_cards(spec.query, k=spec.k)
