"""Actual SQL and response-copy costs on disposable research data."""
import asyncio
import json
from time import perf_counter

from sqlalchemy import event

from meta_research import snapshot_queries
from meta_research.database import Database
from meta_research.runtime_status import RuntimeStatusReader
from test_public_human_collaboration_web import _runtime, _authenticated_client, _confirm_direct_quest


def test_http_closed_panels_do_not_build_deferred_sections(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    _confirm_direct_quest(runtime)

    def forbidden(*args, **kwargs):
        raise AssertionError('unrequested heavy section was built')

    monkeypatch.setattr(runtime.owners.research_memory, 'query_asset_projection_inventory', forbidden)
    monkeypatch.setattr(runtime.owners.research_graph, 'query_question_tree', forbidden)
    monkeypatch.setattr(runtime.writing, 'query_overview', forbidden)
    client, _ = _authenticated_client(runtime)
    try:
        response = client.get('/api/v1/snapshot?include_assets=false&include_history=false')
        assert response.status_code == 200
        value = response.json()
        assert value['research_assets']['loaded'] is False
        assert value['question_tree']['loaded'] is False
        status = client.get('/api/v1/status').json()
        assert value['revision_domain'] == status['revision_domain']
        reopened = Database(runtime._database.path)
        try:
            assert reopened.revision_domain() == value['revision_domain']
        finally:
            reopened.close()
        other = _runtime(tmp_path / 'other-instance')
        try:
            assert RuntimeStatusReader(other._database).query()['revision_domain'] != value['revision_domain']
        finally:
            other.close()
    finally:
        client.close()
        runtime.close()


def test_section_costs_use_sql_hooks_and_copy_only_returned_content(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    _confirm_direct_quest(runtime)
    sql = []
    copies = []
    original_copy = snapshot_queries.deepcopy

    def record_sql(_connection, _cursor, statement, _parameters, _context, _many):
        sql.append(statement)

    def record_copy(value):
        began = perf_counter()
        result = original_copy(value)
        copies.append({
            'bytes': len(json.dumps(value, ensure_ascii=False).encode()),
            'ms': (perf_counter() - began) * 1000,
            'assets_loaded': value['research_assets']['loaded'],
            'history_loaded': value['question_tree']['loaded'],
            'asset_items': len(value['research_assets']['items']),
            'history_items': len(value['question_tree']['items']),
        })
        return result

    monkeypatch.setattr(snapshot_queries, 'deepcopy', record_copy)
    event.listen(runtime._database._engine, 'before_cursor_execute', record_sql)
    measurements = {}

    async def run():
        async def measure(name, action):
            sql.clear()
            copies.clear()
            began = perf_counter()
            result = await action()
            measurements[name] = {'sql_calls': len(sql), 'elapsed_ms': (perf_counter() - began) * 1000,
                                  'copies': list(copies)}
            return result

        def coordinator():
            return snapshot_queries.SnapshotQueryCoordinator(
                runtime.projection.query_snapshot,
                feed_revision=lambda: runtime.feed.query_readiness().current_revision,
            )

        light_reader = coordinator()
        light = await measure('light', lambda: light_reader.query(include_assets=False, include_history=False))
        full_reader = coordinator()
        full = await measure('full', lambda: full_reader.query(include_assets=True, include_history=True))
        assert light['revision'] == full['revision']
        assert measurements['full']['sql_calls'] > measurements['light']['sql_calls']
        assert measurements['full']['copies'][0]['bytes'] > measurements['light']['copies'][0]['bytes']
        full['question_tree']['items'][0]['title'] = 'caller-local edit'
        retained = await measure('retained', lambda: full_reader.query(include_assets=False, include_history=False))
        assert retained['question_tree']['items'] == []
        assert measurements['retained']['copies'][0]['history_items'] == 0
        sql.clear()
        runtime.feed.query_readiness()
        assert measurements['retained']['sql_calls'] == len(sql)
        # Full retrieval after a deferred retained hit is still isolated.
        assert (await full_reader.query(include_assets=True, include_history=True))['question_tree']['items'][0]['title'] != 'caller-local edit'
        concurrent_reader = coordinator()
        results = await measure('concurrent', lambda: asyncio.gather(
            concurrent_reader.query(include_assets=False, include_history=False),
            concurrent_reader.query(include_assets=True, include_history=True),
            concurrent_reader.query(include_assets=True, include_history=False),
        ))
        assert {item['revision'] for item in results} == {full['revision']}
        assert len(measurements['concurrent']['copies']) == 3
        assert measurements['concurrent']['sql_calls'] < 2 * measurements['full']['sql_calls']
        measurements['full']['source_diagnostics'] = full['query_diagnostics']
        measurements['light']['source_diagnostics'] = light['query_diagnostics']

    try:
        asyncio.run(run())
        (tmp_path / 'section-cost-observation.json').write_text(json.dumps(measurements, indent=2), encoding='utf-8')
        print('SECTION_COST_OBSERVATION ' + json.dumps(measurements))
    finally:
        event.remove(runtime._database._engine, 'before_cursor_execute', record_sql)
        runtime.close()
