"""CP2.2 · WriteDaemon 短事务原语（单写连接 / 提交回滚 / 不可嵌套）。"""
from __future__ import annotations

import threading
import time

import pytest

from orchestrator import database as db
from orchestrator.writedaemon import WriteDaemon


@pytest.fixture()
def daemon():
    return WriteDaemon(db.connect(":memory:"))


def test_isolation_level_none(daemon):
    assert daemon.conn.isolation_level is None   # 显式掌控事务


def test_transaction_commits_on_success(daemon):
    with daemon.transaction() as conn:
        conn.execute("INSERT INTO goal(id,version,text,predicate_json) VALUES (1,1,'g','{}')")
    assert daemon.query_one("SELECT text FROM goal WHERE id=1")[0] == "g"


def test_transaction_rolls_back_on_exception(daemon):
    with pytest.raises(RuntimeError, match="boom"):
        with daemon.transaction() as conn:
            conn.execute("INSERT INTO goal(id,version,text,predicate_json) VALUES (1,1,'g','{}')")
            raise RuntimeError("boom")
    assert daemon.query_one("SELECT count(*) FROM goal")[0] == 0   # 整体回滚、无半写


def test_transaction_not_reentrant(daemon):
    with pytest.raises(RuntimeError, match="不可嵌套"):
        with daemon.transaction():
            with daemon.transaction():
                pass


def test_reusable_after_rollback(daemon):
    with pytest.raises(ValueError):
        with daemon.transaction() as conn:
            conn.execute("INSERT INTO goal(id,version,text,predicate_json) VALUES (1,1,'g','{}')")
            raise ValueError()
    # 回滚后 daemon 仍可用（_in_txn 已复位）
    with daemon.transaction() as conn:
        conn.execute("INSERT INTO goal(id,version,text,predicate_json) VALUES (2,1,'g2','{}')")
    assert daemon.query_one("SELECT count(*) FROM goal")[0] == 1


def test_transaction_serializes_cross_thread_query_until_commit(daemon):
    entered = threading.Event()
    release = threading.Event()
    reader_started = threading.Event()
    reader_done = threading.Event()
    observed = []

    def writer():
        with daemon.transaction() as conn:
            conn.execute("INSERT INTO goal(id,version,text,predicate_json) VALUES (1,1,'g','{}')")
            entered.set()
            assert release.wait(1)

    def reader():
        assert entered.wait(1)
        reader_started.set()
        observed.append(daemon.query_one("SELECT count(*) FROM goal")[0])
        reader_done.set()

    writer_thread = threading.Thread(target=writer)
    reader_thread = threading.Thread(target=reader)
    writer_thread.start(); reader_thread.start()
    assert reader_started.wait(1)
    time.sleep(0.03)
    assert not reader_done.is_set(), "另一线程不得插入事务中间读取同一 writer connection"
    release.set()
    writer_thread.join(1); reader_thread.join(1)
    assert not writer_thread.is_alive() and not reader_thread.is_alive()
    assert observed == [1]
