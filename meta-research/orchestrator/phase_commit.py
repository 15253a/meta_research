"""阶段级原子提交的幂等落库（§4.2.5；interfaces.PhaseCommitStore 的真实现，M4 CP5.4）。

幂等键 = (cycle_id, stage, IFNULL(target_id,-1))（DDL UNIQUE）；判定：
- 无行 → 记录并 'new'（调用方随本阶段事务一起提交）；
- 同键同 artifact_hash → 'duplicate'（kill-9 重启重做时跳过——阶段已提交）；
- 同键**异** hash → 'conflict'（staging 被改写后误判「已提交」的防线——调用方必须拒绝，§4.2.5）。
"""
from __future__ import annotations

import sqlite3
from typing import Literal, Optional

from .ids import cnum as _cnum
from .writedaemon import WriteDaemon


def check_or_record(conn: sqlite3.Connection, *, cycle_id: str, stage: str,
                    target_id: Optional[int], artifact_hash: str) -> Literal["new", "duplicate", "conflict"]:
    """在**调用方已持有的事务连接**上判定并（new 时）记录——供阶段事务内联（记录与阶段写同生共死）。"""
    row = conn.execute("SELECT artifact_hash FROM phase_commit WHERE cycle_id=? AND stage=? "
                       "AND IFNULL(target_id,-1)=IFNULL(?,-1)",
                       (_cnum(cycle_id), stage, target_id)).fetchone()
    if row is not None:
        return "duplicate" if row[0] == artifact_hash else "conflict"
    conn.execute("INSERT INTO phase_commit(cycle_id,stage,target_id,artifact_hash) VALUES (?,?,?,?)",
                 (_cnum(cycle_id), stage, target_id, artifact_hash))
    return "new"


class SqlitePhaseCommit:
    """独立短事务版（interfaces.PhaseCommitStore Protocol）；阶段事务内请直接用 check_or_record(conn,…)。"""

    def __init__(self, daemon: WriteDaemon):
        self.daemon = daemon

    def check_or_record(self, *, cycle_id: str, stage: str, target_id: Optional[int],
                        artifact_hash: str) -> Literal["new", "duplicate", "conflict"]:
        with self.daemon.transaction() as conn:
            return check_or_record(conn, cycle_id=cycle_id, stage=stage,
                                   target_id=target_id, artifact_hash=artifact_hash)
