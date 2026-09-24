import sqlite3

from meta_research.timeline_summaries import TimelineSummaryStore


def observed(i, size=20):
    return {'node_key': 'stage:c1:bundle', 'kind': 'stage', 'source_hash': str(i),
            'content': {'finding': 'x' * size}, 'sources': [{'ref': 'source-1'}]}


def publish(store, job, native):
    store.publish(job, [{'node_key': n['node_key'], 'source_hash': n['source_hash'],
                        'summary': '已有结果，仍需外部验证。', 'source_refs': ['source-1']}
                       for n in job['nodes']], native, job['created_at'] + 1)


def test_large_complete_snapshots_roll_session_before_context_accumulates(tmp_path):
    store = TimelineSummaryStore(tmp_path / 'summary.sqlite3')
    for i in range(3):
        store.sync('quest-1', [observed(i, 110_000)], i * 400)
        job = store.claim(i * 400, 'zh')
        assert job['native_session_ref'] == (None if i in (0, 2) else 'native-1')
        assert len(job['nodes'][0]['content']['finding']) == 110_000
        if i == 2:
            assert job['nodes'][0]['previous_summary'] == '已有结果，仍需外部验证。'
            assert TimelineSummaryStore(store.path).claim(i * 400, 'zh')['job_ref'] == job['job_ref']
        publish(store, job, 'native-2' if i == 2 else 'native-1')
    assert store.query('quest-1')['nodes'][0]['status'] == 'ready'


def test_failed_calls_also_bound_session_lifetime_and_preserve_old_summary(tmp_path):
    store = TimelineSummaryStore(tmp_path / 'summary.sqlite3')
    for i in range(9):
        store.sync('quest-1', [observed(i)], i * 1000)
        job = store.claim(i * 1000, 'zh')
        assert job['native_session_ref'] == (None if i in (0, 8) else 'native-1')
        if i == 0:
            publish(store, job, 'native-1')
        else:
            store.fail(job, 'codex_cli_failed', i * 1000 + 1)
    assert store.query('quest-1')['nodes'][0]['summary'] == '已有结果，仍需外部验证。'


def test_legacy_session_rotates_after_original_active_job_is_recovered(tmp_path):
    path = tmp_path / 'summary.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE quests (quest_ref TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0, native_session_ref TEXT, observed_at REAL NOT NULL DEFAULT 0)')
        db.execute("INSERT INTO quests VALUES ('quest-1', 12, 'old-full-session', 100)")
    store = TimelineSummaryStore(path)
    store.sync('quest-1', [observed(1)], 1000)
    job = store.claim(1000, 'zh')
    assert job['native_session_ref'] is None
    assert store.claim(1000, 'zh')['job_ref'] == job['job_ref']
    publish(store, job, 'new-session')
    store.sync('quest-1', [observed(2)], 1400)
    assert TimelineSummaryStore(path).claim(1400, 'zh')['native_session_ref'] == 'new-session'


def test_upgrade_recovers_signed_active_job_before_rotating(tmp_path):
    store = TimelineSummaryStore(tmp_path / 'summary.sqlite3')
    store.sync('quest-1', [observed(0)], 100)
    publish(store, store.claim(100, 'zh'), 'old-session')
    store.sync('quest-1', [observed(1)], 500)
    active = store.claim(500, 'zh')
    with sqlite3.connect(store.path) as db:
        db.execute('ALTER TABLE quests DROP COLUMN session_jobs')
        db.execute('ALTER TABLE quests DROP COLUMN session_input_bytes')
    upgraded = TimelineSummaryStore(store.path)
    recovered = upgraded.claim(600, 'zh')
    assert {key: recovered[key] for key in active} == active
    publish(upgraded, recovered, 'old-session')
    upgraded.sync('quest-1', [observed(2)], 900)
    assert upgraded.claim(900, 'zh')['native_session_ref'] is None
