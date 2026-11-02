import sqlite3
import threading

from fastapi.testclient import TestClient

from meta_research.web import create_app
from test_public_manual_question_lifecycle import _build_runtime, _accept_root_question


def test_authenticated_reads_do_not_start_recorder_or_change_research(tmp_path, monkeypatch):
    runtime = _build_runtime(tmp_path / 'recorder-api')
    try:
        quest_ref, question_ref = _accept_root_question(runtime, 'recorder')
        service = runtime.timeline_summaries
        assert service is not None
        calls = []
        monkeypatch.setattr(service, '_provider_factory', lambda: calls.append('provider'))
        monkeypatch.setattr(service.reader, 'read', lambda *args: calls.append('sources'))
        client = TestClient(create_app(runtime, base_url='http://testserver', control_key='test-control'))
        endpoint = f'/api/v1/quests/{quest_ref}/timeline-summaries'
        assert client.get(endpoint).status_code == 401
        response = client.post('/auth/bootstrap', headers={'Origin': 'http://testserver'},
            json={'token': runtime.authentication.issue_bootstrap_token()})
        assert response.status_code == 200
        before = runtime.owners.human_collaboration.query_snapshot().revision
        for _ in range(3):
            response = client.get(endpoint)
            assert response.status_code == 200, response.text
            assert response.json()['quest_ref'] == quest_ref
            assert response.json()['nodes'] == []
        assert client.get('/api/v1/quests/missing/timeline-summaries').status_code == 404
        assert not calls
        assert not service.store.path.exists()
        assert runtime.owners.human_collaboration.query_snapshot().revision == before
        # Publish two display notes through the ordinary store; reading the page
        # must expose both milestone states without waking the provider.
        notes = [
            {'kind': 'question', 'node_key': f'question:{question_ref}', 'source_hash': 'q1',
             'sources': [{'ref': question_ref}]},
            {'kind': 'target', 'node_key': 'target:first-result', 'source_hash': 't1',
             'sources': [{'ref': 'first-result'}]},
        ]
        service.store.sync(quest_ref, notes, 10)
        job = service.store.claim(10, 'zh')
        service.store.publish(job, [
            {**node, 'summary': sentence, 'summary_kind': kind, 'milestone': milestone,
             'source_refs': [node['sources'][0]['ref']]}
            for node, sentence, kind, milestone in (
                (notes[0], '比较受控条件下的成像误差。', 'process', False),
                (notes[1], '初步观察到成像误差降低，尚待外部复核。', 'tentative_finding', True),
            )
        ], 'recorder-native', 11)
        revision = service.query(quest_ref)['revision']
        response = client.get(endpoint).json()
        assert response['revision'] == revision
        assert {n['node_key']: n['milestone'] for n in response['nodes']} == {
            notes[0]['node_key']: False, notes[1]['node_key']: True,
        }
        assert not calls
        monkeypatch.setattr(service.store, 'query', lambda _: (_ for _ in ()).throw(sqlite3.OperationalError('/private/location')))
        failed = client.get(endpoint)
        assert failed.status_code == 503
        assert '/private/' not in failed.text
    finally:
        runtime.close()


def test_recorder_starts_without_a_page_and_failure_does_not_gate_research(tmp_path, monkeypatch):
    runtime = _build_runtime(tmp_path / 'recorder-lifecycle')
    started = threading.Event()
    stopped = []

    def unavailable():
        started.set()
        raise OSError('independent recorder unavailable')

    monkeypatch.setattr(runtime.timeline_summaries, 'process_once', unavailable)
    monkeypatch.setattr(runtime.timeline_summaries, 'request_stop', lambda: stopped.append(True))
    try:
        with TestClient(create_app(runtime, base_url='http://testserver', control_key='test-control')) as client:
            assert started.wait(timeout=2), 'daemon startup must run the recorder without a browser request'
            response = client.post('/auth/bootstrap', headers={'Origin': 'http://testserver'},
                json={'token': runtime.authentication.issue_bootstrap_token()})
            assert response.status_code == 200
            health = client.get('/api/v1/health').json()
            assert health['status'] == 'ready', health
            assert not any('recorder' in check['name'] or 'summary' in check['name'] for check in health['checks'])
        assert stopped == [True]
    finally:
        runtime.close()
