import sqlite3
import threading

from fastapi.testclient import TestClient

from meta_research.web import create_app
from test_research_datasets import _runtime, _quest


def test_authenticated_reads_do_not_start_recorder_or_change_research(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path / 'recorder-api')
    try:
        question = _quest(runtime, 'recorder')
        service = runtime.timeline_summaries
        assert service is not None
        calls = []
        monkeypatch.setattr(service, '_provider_factory', lambda: calls.append('provider'))
        monkeypatch.setattr(service.reader, 'read', lambda *args: calls.append('sources'))
        client = TestClient(create_app(runtime, base_url='http://testserver', control_key='test-control'))
        endpoint = f'/api/v1/quests/{question.quest_ref}/timeline-summaries'
        assert client.get(endpoint).status_code == 401
        response = client.post('/auth/bootstrap', headers={'Origin': 'http://testserver'},
            json={'token': runtime.authentication.issue_bootstrap_token()})
        assert response.status_code == 200
        before = runtime.owners.human_collaboration.query_snapshot().revision
        for _ in range(3):
            response = client.get(endpoint)
            assert response.status_code == 200, response.text
            assert response.json()['quest_ref'] == question.quest_ref
            assert response.json()['nodes'] == []
        assert client.get('/api/v1/quests/missing/timeline-summaries').status_code == 404
        assert not calls
        assert not service.store.path.exists()
        assert runtime.owners.human_collaboration.query_snapshot().revision == before
        monkeypatch.setattr(service.store, 'query', lambda _: (_ for _ in ()).throw(sqlite3.OperationalError('/private/location')))
        failed = client.get(endpoint)
        assert failed.status_code == 503
        assert '/private/' not in failed.text
    finally:
        runtime.close()


def test_recorder_starts_without_a_page_and_failure_does_not_gate_research(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path / 'recorder-lifecycle')
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
