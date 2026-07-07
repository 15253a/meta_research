"""WriteDaemon —— 资产层唯一写库执行者（《第二部分》§6.13(1) / §6.6）。

铁律（护 P1/P6，全库写只有一条路）：
- 各模块（Gate 事实注册 / StateStore 状态迁移 / 后续 interaction / importer）**不得自开 sqlite 写连接**，
  只经本 daemon 的**唯一**写连接落库；两族写入**共用同一事务边界 + 72 触发器**（§6.6）。
- **短事务铁律**：长操作（runner 调用 / 训练 / import clone）**绝不持写事务**——在事务外执行、完成后
  才提交一个短写命令。本类只提供短事务上下文，不给任何长事务接口。

M1 形态（本检查点 CP2.2）：**同进程同步**执行——单线程驱动器下，`transaction()` 即一个短事务。
形式化 `WriteCommand` 队列 + 独立 daemon 线程（§6.13(1) 表 / interfaces.WriteDaemon.submit）留到 M5
（interaction daemon 并发入站时才需要串行化队列）；此前的核心保证——唯一写连接 + 原子短事务——现已就位。

事务语义：`BEGIN IMMEDIATE`（立即取写锁，避免升级死锁）→ 块内任一步抛异常即整体 `ROLLBACK`
（decompose 等多写原子性的落点，§4.2.5：kill-9 / 异常后无半写）→ 正常退出 `COMMIT`。
连接置 `isolation_level=None`（关 python sqlite3 隐式事务，改由本类显式 BEGIN/COMMIT/ROLLBACK 掌控）。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator, List, Optional, Sequence, Tuple


class WriteDaemon:
    def __init__(self, conn: sqlite3.Connection):
        """conn = database.connect(...) 建/开的写连接；本 daemon 独占它作唯一写连接。

        单进程 M1：读也走此连接（事务内读须见未提交写，如 decompose 先读父状态再写子问题；
        :memory: 库更是单连接才同库）。Gate 的**受限只读连接 + authorizer**（§6.13(2)）是另一条、
        CP2.3 引入，不在此。
        """
        conn.isolation_level = None   # 显式掌控事务（关隐式 BEGIN）
        self._conn = conn
        self._in_txn = False

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """一个短写事务：整体提交或整体回滚（无半写）。不可嵌套（一个写命令一个短事务）。

        鲁棒性：`_in_txn` 在 finally 复位——即便 `BEGIN IMMEDIATE` 本身失败（如取写锁超时）也不会把
        daemon 永久卡在「事务中」；`COMMIT` 失败（罕见，如磁盘满）则尽力 ROLLBACK，避免真实事务态与
        `_in_txn` 不一致、堵死后续写入。
        """
        if self._in_txn:
            raise RuntimeError("WriteDaemon 事务不可嵌套（一个写命令一个短事务；长操作须在事务外）")
        self._in_txn = True
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                try:
                    self._conn.execute("COMMIT")
                except BaseException:
                    try:
                        self._conn.execute("ROLLBACK")
                    except Exception:
                        pass
                    raise
        finally:
            self._in_txn = False

    def query(self, sql: str, params: Sequence[Any] = ()) -> List[Tuple]:
        """只读查询（同连接；事务内可见未提交写，事务外见已提交态）。"""
        return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> Optional[Tuple]:
        return self._conn.execute(sql, params).fetchone()
