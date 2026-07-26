"""WriteDaemon —— 资产层唯一写库执行者（《第二部分》§6.13(1) / §6.6）。

铁律（护 P1/P6，全库写只有一条路）：
- 各模块（Gate 事实注册 / StateStore 状态迁移 / 后续 interaction / importer）**不得自开 sqlite 写连接**，
  只经本 daemon 的**唯一**写连接落库；两族写入**共用同一事务边界 + 72 触发器**（§6.6）。
- **短事务铁律**：长操作（runner 调用 / 训练 / import clone）**绝不持写事务**——在事务外执行、完成后
  才提交一个短写命令。本类只提供短事务上下文，不给任何长事务接口。

CP11 形态：研究驱动与 interaction pump 同进程两线程，仍共用**唯一** writer
connection。`RLock` 串行化每次连接操作，`transaction()` 持锁覆盖整个短事务；长外部调用仍在
事务外。因此并发仅是入站/回执及时性，不会变成两个 writer 或交叉事务。

事务语义：`BEGIN IMMEDIATE`（立即取写锁，避免升级死锁）→ 块内任一步抛异常即整体 `ROLLBACK`
（decompose 等多写原子性的落点，§4.2.5：kill-9 / 异常后无半写）→ 正常退出 `COMMIT`。
连接置 `isolation_level=None`（关 python sqlite3 隐式事务，改由本类显式 BEGIN/COMMIT/ROLLBACK 掌控）。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Callable, Iterator, List, Optional, Sequence, Tuple


class _LockedCursor:
    """Cursor facade that keeps every sqlite C call under the daemon lock."""

    def __init__(self, cursor: sqlite3.Cursor, lock: threading.RLock,
                 owner_guard: Callable[[], None]):
        self._cursor = cursor
        self._lock = lock
        self._owner_guard = owner_guard

    def __getattr__(self, name):
        attr = getattr(self._cursor, name)
        if not callable(attr):
            return attr

        def guarded(*args, **kwargs):
            with self._lock:
                self._owner_guard()
                result = attr(*args, **kwargs)
                return self if result is self._cursor else result
        return guarded

    def __iter__(self):
        return self

    def __next__(self):
        with self._lock:
            self._owner_guard()
            return next(self._cursor)


class _LockedConnection:
    """Compatibility facade for legacy ``daemon.conn`` call sites.

    Transaction bodies receive the real connection while WriteDaemon holds the
    same re-entrant lock.  Out-of-transaction readers that still use
    ``daemon.conn`` are serialized here, so the interaction pump cannot execute
    on the connection midway through another sqlite operation.
    """

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock,
                 owner_guard: Callable[[], None]):
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "_lock", lock)
        object.__setattr__(self, "_owner_guard", owner_guard)

    def __getattr__(self, name):
        attr = getattr(self._conn, name)
        if not callable(attr):
            with self._lock:
                self._owner_guard()
                return getattr(self._conn, name)

        def guarded(*args, **kwargs):
            with self._lock:
                self._owner_guard()
                result = attr(*args, **kwargs)
                return (_LockedCursor(result, self._lock, self._owner_guard)
                        if isinstance(result, sqlite3.Cursor) else result)
        return guarded

    def __setattr__(self, name, value):
        if name in {"_conn", "_lock", "_owner_guard"}:
            object.__setattr__(self, name, value)
            return
        with self._lock:
            self._owner_guard()
            setattr(self._conn, name, value)


class WriteDaemon:
    def __init__(self, conn: sqlite3.Connection,
                 owner_guard: Optional[Callable[[], None]] = None):
        """conn = database.connect(...) 建/开的写连接；本 daemon 独占它作唯一写连接。

        单进程 M1：读也走此连接（事务内读须见未提交写，如 decompose 先读父状态再写子问题；
        :memory: 库更是单连接才同库）。Gate 的**受限只读连接 + authorizer**（§6.13(2)）是另一条、
        CP2.3 引入，不在此。
        """
        conn.isolation_level = None   # 显式掌控事务（关隐式 BEGIN）
        self._conn = conn
        self._lock = threading.RLock()
        self._owner_guard = owner_guard or (lambda: None)
        self._public_conn = _LockedConnection(conn, self._lock, self._owner_guard)
        self._in_txn = False

    @property
    def conn(self):
        return self._public_conn

    @contextmanager
    def connection_guard(self) -> Iterator[None]:
        """Keep one multi-statement connection operation uninterleaved.

        This is not a transaction API.  It exists for deep modules that use a
        SQLite SAVEPOINT as their own tiny atomic suffix while sharing this
        daemon-owned connection.  Holding the daemon lock across the complete
        SAVEPOINT/RELEASE sequence prevents concurrent callers from nesting
        unrelated savepoints on the same connection.
        """
        with self._lock:
            self._owner_guard()
            yield

    def owns_active_transaction(self, conn: sqlite3.Connection) -> bool:
        """Return whether ``conn`` is this daemon's raw connection inside its current transaction."""
        return self._in_txn and conn is self._conn and self._conn.in_transaction

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """一个短写事务：整体提交或整体回滚（无半写）。不可嵌套（一个写命令一个短事务）。

        鲁棒性：`_in_txn` 在 finally 复位——即便 `BEGIN IMMEDIATE` 本身失败（如取写锁超时）也不会把
        daemon 永久卡在「事务中」；`COMMIT` 失败（罕见，如磁盘满）则尽力 ROLLBACK，避免真实事务态与
        `_in_txn` 不一致、堵死后续写入。
        """
        with self._lock:
            self._owner_guard()
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
                        # Re-fence at the commit edge.  A heartbeat/path-owner
                        # failure during the short body must roll back instead
                        # of allowing an old capability to commit after a new
                        # stable lock identity can be acquired.
                        self._owner_guard()
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
        with self._lock:
            self._owner_guard()
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> Optional[Tuple]:
        with self._lock:
            self._owner_guard()
            return self._conn.execute(sql, params).fetchone()
