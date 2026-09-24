from concurrent.futures import ThreadPoolExecutor
from threading import Event
import time

import pytest
from sqlalchemy import text

from meta_research.database import Database
from test_public_human_collaboration_web import _runtime, _authenticated_client, _confirm_direct_quest


def test_nested_reads_keep_one_snapshot_while_writer_advances(tmp_path):
    db = Database(tmp_path / 'snapshot.sqlite3')
    with db.write() as c:
        c.execute(text('CREATE TABLE sample (value INTEGER)'))
        c.execute(text('INSERT INTO sample VALUES (1)'))
    try:
        with db.read_snapshot() as outer:
            assert outer.execute(text('SELECT value FROM sample')).scalar_one() == 1
            def advance():
                with db.write() as c:
                    c.execute(text('UPDATE sample SET value=2'))
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(advance).result(timeout=2)
            with db.read() as nested:
                assert nested is outer
                assert nested.execute(text('SELECT value FROM sample')).scalar_one() == 1
            with pytest.raises(RuntimeError, match='read_snapshot'):
                with db.write():
                    pass
        with db.read() as fresh:
            assert fresh.execute(text('SELECT value FROM sample')).scalar_one() == 2
    finally:
        db.close()


def test_status_and_health_do_not_run_expensive_projection(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    def heavy():
        pytest.fail('a lightweight GET invoked the full projection')
    monkeypatch.setattr(runtime.projection, 'query_snapshot', heavy)
    monkeypatch.setattr(runtime.bundle_stage, 'query_current', heavy)
    client, _ = _authenticated_client(runtime)
    try:
        status = client.get('/api/v1/status')
        assert status.status_code == 200
        payload = status.json()
        assert payload['schema_ref'] == 'meta-research/runtime-status/v1'
        assert payload['state'] == 'idle'
        assert payload['updated_at'] and payload['observed_at']
        assert 'research_assets' not in payload
        health = client.get('/api/v1/health')
        assert health.status_code == 200
        assert health.json()['checks']
        assert health.json()['status'] == 'unavailable'  # workers have not started
        readiness = client.get('/internal/readiness', headers={'X-Meta-Research-Control':'control-secret'})
        assert readiness.status_code == 200
    finally:
        client.close()
        runtime.close()


def test_status_uses_exact_foreground_and_excludes_historical_stage_runs(tmp_path):
    runtime = _runtime(tmp_path)
    _confirm_direct_quest(runtime)
    client, _ = _authenticated_client(runtime)
    try:
        payload = client.get('/api/v1/status').json()
        assert payload['foreground']['stage'] == 'idea'
        assert payload['current_task']['kind'] == 'stage'
        assert payload['current_task']['title'] == '研究思路'
        assert payload['current_task']['run_ref'] is None
        assert payload['state'] == 'failed'  # current Idea worker has not started
        assert '研究推进暂时受阻' in payload['waiting_reason']
    finally:
        client.close()
        runtime.close()


def test_production_projection_has_one_read_cut_during_event_change(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    original = runtime.owners.research_graph.query_snapshot
    calls = []
    def advance_between_owners():
        result = original()
        calls.append(result.revision)
        def advance():
            with runtime._database.write() as c:
                c.execute(text("INSERT INTO durable_feed (event_type,payload_json,recorded_at) VALUES ('test.advance','{}',123)"))
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(advance).result(timeout=2)
        return result
    monkeypatch.setattr(runtime.owners.research_graph, 'query_snapshot', advance_between_owners)
    before = runtime.feed.query_readiness().current_revision
    try:
        snapshot = runtime.projection.query_snapshot()
        assert calls and len(set(calls)) == 1
        assert snapshot['revision'] == before
        assert runtime.feed.query_readiness().current_revision > before
        assert snapshot['query_diagnostics']['attempts'] == 1
        assert snapshot['query_diagnostics']['sql_calls'] > 0
    finally:
        runtime.close()


def test_deferred_sections_do_not_query_assets_or_question_history(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    _confirm_direct_quest(runtime)
    def unwanted(*args, **kwargs):
        pytest.fail('a deferred section was queried')
    monkeypatch.setattr(runtime.owners.research_memory, 'query_asset_projection_inventory', unwanted)
    monkeypatch.setattr(runtime.owners.research_graph, 'query_question_tree', unwanted)
    monkeypatch.setattr(runtime.writing, 'query_overview', unwanted)
    try:
        snapshot = runtime.projection.query_snapshot(include_assets=False, include_history=False)
        assert snapshot['research_assets']['loaded'] is False
        assert snapshot['question_tree']['loaded'] is False
        assert snapshot['query_diagnostics']['retries'] == 0
    finally:
        runtime.close()


def test_two_http_refreshes_share_one_query_and_health_does_not_wait(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    sample = runtime.projection.query_snapshot()
    entered, release = Event(), Event()
    calls = []
    def slow(**kwargs):
        calls.append(kwargs)
        entered.set()
        assert release.wait(5)
        return sample
    monkeypatch.setattr(runtime.projection, 'query_snapshot', slow)
    client, _ = _authenticated_client(runtime)
    try:
        with client, ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(client.get, '/api/v1/snapshot')
            assert entered.wait(2)
            second = pool.submit(client.get, '/api/v1/snapshot')
            try:
                began = time.monotonic()
                assert client.get('/api/v1/health').status_code == 200
                assert time.monotonic() - began < 0.5
                time.sleep(0.15)
                assert len(calls) == 1
            finally:
                release.set()
            assert first.result(timeout=3).status_code == 200
            assert second.result(timeout=3).status_code == 200
            assert len(calls) == 1
    finally:
        release.set()
        client.close()
        runtime.close()


def test_evidence_page_can_join_an_existing_public_read_snapshot(tmp_path):
    runtime = _runtime(tmp_path)
    try:
        with runtime._database.read_snapshot():
            total, rows = runtime.owners.research_graph.query_target_commit_evidence_candidates(quest_ref='quest_absent')
            assert total == 0 and rows == ()
    finally:
        runtime.close()
