"""确定性观测 parser（§4.3.1/§4.7；M4 CP5.3）——raw log → execution_observation(source='parser') → parser_result_suspect。

**可回放（P6）**：同 log 内容 + 同 PARSER_VERSION + 同 policy.observation → 同观测字段 / 同 suspect。
`extraction_policy_hash` = policy.observation 节规范化 JSON 的 sha256，随观测行落库——复算按行内 hash 取当时口径。

**铁律（§3.1.2/§4.3.1）**：观测内容永不作正向 evidence / metric_result / gate 判据；parser 据观测 + policy 阈值
**确定性派生**的负向谓词 `parser_result_suspect`（非存储列，可复算）仅作负向过滤——挡复用（§4.1.5 selector）、
挡关问（gate_close_question 拒存疑 attempt 作证据）——不是 evidence、不支持结论。

**log 行约定**（toy harness 与真训练脚本共用的最小口径；不认识的行忽略——parser 只认下列前缀，宽进严出）：
  `loss: <float|nan>` ｜ `warning: …` ｜ `oom`/`cuda out of memory`（行内任意处，不区分大小写）
  ｜ `retry` 行首 ｜ `wall_clock_sec: <float>`（脚本自报；非 DB 插入时钟）
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from typing import Any, Dict, Optional

from .writedaemon import WriteDaemon

PARSER_VERSION = "1.0.0"


def extraction_policy_hash(obs_policy: Dict[str, Any]) -> str:
    """policy.observation 节 → 规范化 JSON（键排序、紧凑分隔）→ sha256（P6 复算锚）。"""
    return hashlib.sha256(json.dumps(obs_policy, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def parse_log(text: str, obs_policy: Dict[str, Any]) -> Dict[str, Any]:
    """确定性解析：log 文本 → 机器事实字段（execution_observation 列口径）。纯函数、无时钟无随机。"""
    p = obs_policy["parse"]
    losses: list = []          # 有限数值 loss 序列（非有限值不入序列、置 nan_seen）
    nan_seen = 0
    oom = warn = retry = 0
    wall: Optional[float] = None
    for raw in text.splitlines():
        line = raw.strip()
        low = line.lower()
        if low.startswith("loss:"):
            val = low[5:].strip()
            try:
                f = float(val)
            except ValueError:
                continue                      # 非数值 loss 行忽略（宽进）
            if not math.isfinite(f):          # nan/±inf 一律置位（非有限=退化值；-inf 若只判 nan 会漏，内审 NIT）
                nan_seen = 1
            else:
                losses.append(f)
        elif low.startswith("warning:"):
            warn += 1
        elif "cuda out of memory" in low or re.search(r"\boom\b", low):
            oom += 1                          # 词边界防 bloom/room 误报（内审 NIT）
        elif low.startswith("retry"):
            retry += 1
        elif low.startswith("wall_clock_sec:"):
            try:
                w = float(low.split(":", 1)[1].strip())
                wall = w if math.isfinite(w) else None   # 非有限归 None（nan≠nan 破回放比对，codex SHOULD）
            except ValueError:
                pass
    divergence = 0
    # 乘法阈值仅对**正 loss 序列**语义正确（首 loss ≤0 时判式反转/塌缩——log-likelihood 类负 loss 不适用，
    # 内审 SHOULD）：非正首 loss → 不判 divergence（nan/oom 判据仍在；负 loss 域的发散判据留给未来
    # policy.parse 扩展，届时升 extraction_policy_hash 自然换口径）。policy.yaml 注释同步声明此边界。
    if losses and losses[0] > 0 and losses[-1] > losses[0] * p["divergence_ratio"]:
        divergence = 1
    if nan_seen:
        trend = "nan"
    elif len(losses) < 2:
        trend = "unknown"
    else:
        w = losses[-p["trend_window"]:]
        delta = w[-1] - w[0]
        trend = "flat" if abs(delta) <= p["flat_epsilon"] else ("down" if delta < 0 else "up")
    return {"nan_seen": nan_seen, "divergence_flag": divergence, "oom_count": oom,
            "warning_count": warn, "retry_count": retry,
            "last_loss": losses[-1] if losses else None, "loss_trend": trend, "wall_clock_sec": wall,
            "parser_json": json.dumps({"n_loss_lines": len(losses)}, sort_keys=True)}


def derive_suspect(fields: Dict[str, Any], obs_policy: Dict[str, Any]) -> int:
    """parser_result_suspect（§4.3.1 派生谓词，纯函数）：任一判据命中 → 1。字段 None 按不命中处理（无据不疑）。"""
    s = obs_policy["suspect"]
    if s.get("nan_seen") and fields.get("nan_seen"):
        return 1
    if s.get("divergence_flag") and fields.get("divergence_flag"):
        return 1
    if fields.get("loss_trend") in s.get("loss_trend_suspect", []):
        return 1
    if fields.get("oom_count") is not None and fields["oom_count"] > s.get("max_oom_count", 0):
        return 1
    if fields.get("retry_count") is not None and fields["retry_count"] > s.get("max_retry_count", 2):
        return 1
    return 0


def ingest_observation(daemon: WriteDaemon, *, execution_log_id: int, log_bytes: bytes,
                       obs_policy: Dict[str, Any]) -> int:
    """解析 + 落 execution_observation(source='parser')（短事务）。**幂等**：同 (log, parser_version,
    extraction_policy_hash) 已有行 → 返回既有 id（append-only 表防重复观测行）。
    **content_hash 锚校验（codex SHOULD）**：入参字节须 hash 等于 execution_log.content_hash——观测锚在
    登记的 log 内容上，防调用方 bug 用别的文本对同一 log id 产「假干净」观测。"""
    got = hashlib.sha256(log_bytes).hexdigest()
    fields = parse_log(log_bytes.decode("utf-8", errors="replace"), obs_policy)
    eph = extraction_policy_hash(obs_policy)
    with daemon.transaction() as conn:
        el = conn.execute("SELECT content_hash FROM execution_log WHERE id=?", (execution_log_id,)).fetchone()
        if el is None:
            raise ValueError(f"execution_log {execution_log_id} 不存在")
        if el[0] != got:
            raise ValueError(f"log 内容与登记 content_hash 不符（登记 {el[0][:12]}…，实收 {got[:12]}…）"
                             "——观测必须锚在入账的 log 字节上")
        ex = conn.execute("SELECT id FROM execution_observation WHERE execution_log_id=? AND source='parser' "
                          "AND parser_version=? AND extraction_policy_hash=?",
                          (execution_log_id, PARSER_VERSION, eph)).fetchone()
        if ex:
            return ex[0]
        return conn.execute(
            "INSERT INTO execution_observation(execution_log_id,source,nan_seen,divergence_flag,oom_count,"
            "warning_count,retry_count,last_loss,loss_trend,wall_clock_sec,parser_json,parser_version,"
            "extraction_policy_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (execution_log_id, "parser", fields["nan_seen"], fields["divergence_flag"], fields["oom_count"],
             fields["warning_count"], fields["retry_count"], fields["last_loss"], fields["loss_trend"],
             fields["wall_clock_sec"], fields["parser_json"], PARSER_VERSION, eph)).lastrowid


def suspect_for_attempt(plain_conn: sqlite3.Connection, attempt_id: int, obs_policy: Dict[str, Any]) -> int:
    """按 attempt 复算 parser_result_suspect（P6 口径三条，内审 BLOCKER + codex BLOCKER 合力定形）：
    ① 只认与**当前 (PARSER_VERSION, extraction_policy_hash)** 匹配的观测行——旧版本/旧 policy 行不作数
       （旧宽松 policy 下解析的行冒充「当前口径干净」= 契约级假阴性）；
    ② 某 log **有** parser 观测但**无**当前口径行 → 返回 1（**stale ≠ clean，fail closed**：须重 ingest 才可复用/作证）；
    ③ 每 log 取当前口径最新行、**跨 log 取 OR**（train.log 的 nan 绝不被后入账的干净 stderr 观测掩盖）。
    完全无 parser 观测 → 0（无据不疑；M2 期历史数据口径同 M2 桩；真管线 CP5.4 强制先 ingest 再注册）。
    plain_conn = 普通只读连接（**非** gate 受限连接——观测隔离豁免仅限本派生谓词，§4.3.1 负向过滤）。"""
    eph = extraction_policy_hash(obs_policy)
    rows = plain_conn.execute(
        "SELECT (SELECT eo.id FROM execution_observation eo WHERE eo.execution_log_id=el.id "
        "         AND eo.source='parser' AND eo.parser_version=? AND eo.extraction_policy_hash=? "
        "         ORDER BY eo.id DESC LIMIT 1) AS cur_id, "
        "       EXISTS(SELECT 1 FROM execution_observation eo2 WHERE eo2.execution_log_id=el.id "
        "              AND eo2.source='parser') AS has_any "
        "FROM execution_log el WHERE el.evaluation_attempt_id=?",
        (PARSER_VERSION, eph, attempt_id)).fetchall()
    for cur_id, has_any in rows:
        if cur_id is None:
            if has_any:
                return 1   # ② stale：有观测但非当前口径 → fail closed（重 ingest 前不得当干净用）
            continue       # 该 log 无任何 parser 观测（无据不疑）
        r = plain_conn.execute(
            "SELECT nan_seen, divergence_flag, oom_count, warning_count, retry_count, last_loss, loss_trend "
            "FROM execution_observation WHERE id=?", (cur_id,)).fetchone()
        fields = {"nan_seen": r[0], "divergence_flag": r[1], "oom_count": r[2],
                  "warning_count": r[3], "retry_count": r[4], "last_loss": r[5], "loss_trend": r[6]}
        if derive_suspect(fields, obs_policy):
            return 1
    return 0


def register_parser_suspect_real(conn: sqlite3.Connection, obs_conn: sqlite3.Connection,
                                 obs_policy: Dict[str, Any]) -> None:
    """在 conn 上注册**真** parser_result_suspect(attempt_id)（替 M2 桩 recall_sqlite.register_parser_suspect_stub）。
    obs_conn = 读观测的普通只读连接（可与 conn 不同——如 gate 受限连接消费本谓词时，观测读走独立连接：
    gate SQL 仍不可 SELECT 观测表，负向谓词豁免仅经本函数，§3.1.2/§4.3.1）。"""
    conn.create_function("parser_result_suspect", 1,
                         lambda aid: suspect_for_attempt(obs_conn, aid, obs_policy), deterministic=True)
