"""CP2.2 · WriteDaemon 短事务原语（单写连接 / 提交回滚 / 不可嵌套）。"""
from __future__ import annotations

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
