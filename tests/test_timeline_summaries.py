from copy import deepcopy
from pathlib import Path
import sqlite3

import pytest

from meta_research.owners.common import canonical_hash
from meta_research.quest_drafting import DraftingUnavailable
from meta_research.timeline_summaries import TimelineSummaryService, TimelineSummaryStore


def node(value='正在核验独立受试者数据。'):
    return {'node_key': 'stage:c1:plan', 'kind': 'stage', 'question_ref': 'q1',
            'cycle_ref': 'c1', 'stage': 'plan', 'target_ref': None,
            'content': {'finding': value}, 'source_hash': canonical_hash(value),
            'sources': [{'ref': 'accepted-plan-1', 'label': 'Plan'}]}


class Reader:
    def __init__(self):
        self.nodes = {'quest-1': [node()]}
        self.calls = 0
        self.broken = set()

    def quest_refs(self):
        return list(self.nodes)

    def read(self, quest_ref):
        self.calls += 1
        if quest_ref in self.broken:
            raise OSError('source unavailable')
        return deepcopy(self.nodes[quest_ref])


class Provider:
    def __init__(self):
        self.calls = []
        self.results = {}
        self.pending = False
        self.cancelled = []
        self.failure = None
        self.after_generate = lambda: None
        self.finished = []
        self.next_scan_seconds = 300

    def reconcile_job(self, job_ref):
        return 'pending' if self.pending else 'terminal' if job_ref in self.results else 'absent'

    def cancel_job(self, job_ref):
        self.cancelled.append(job_ref)
        self.pending = False
        return True

    def summarize(self, **request):
        if request['job_ref'] in self.results:
            return self.results[request['job_ref']]
        self.calls.append(deepcopy(request))
        if self.failure:
            raise self.failure
        result = ([{'node_key': n['node_key'], 'source_hash': n['source_hash'],
                    'summary': n['content']['finding'], 'source_refs': [n['sources'][0]['ref']]}
                   for n in request['nodes']], request['native_session_ref'] or 'native-recorder-1', self.next_scan_seconds)
        self.results[request['job_ref']] = result
        self.after_generate()
        return result

    def recover_summaries(self, job):
        return self.results[job['job_ref']]

    def request_stop(self):
        pass

    def finish_job(self, job_ref):
        self.finished.append(job_ref)


@pytest.fixture
def recorder(tmp_path):
    store = TimelineSummaryStore(tmp_path / 'recorder' / 'summaries.sqlite3')
    reader, provider, now = Reader(), Provider(), [100.0]
    service = TimelineSummaryService(store, reader, lambda: provider, clock=lambda: now[0])
    return service, store, reader, provider, now


def test_queries_are_read_only_and_worker_backfills_without_any_page(recorder):
    service, store, reader, provider, _ = recorder
    assert service.query('quest-1')['nodes'] == []
    assert not store.path.exists()
    assert not provider.calls and reader.calls == 0
    assert service.process_once()
    result = service.query('quest-1')
    assert result['nodes'][0]['summary'] == node()['content']['finding']
    assert result['nodes'][0]['status'] == 'ready'
    revision = result['revision']
    for _ in range(5):
        assert service.query('quest-1')['revision'] == revision
        assert not service.process_once()
    assert len(provider.calls) == 1


def test_unchanged_inputs_do_not_regenerate_and_restart_reuses_native(recorder):
    service, store, reader, provider, now = recorder
    assert service.process_once()
    revision = service.query('quest-1')['revision']
    now[0] += 301
    assert not service.process_once()
    assert service.query('quest-1')['revision'] == revision
    reader.nodes['quest-1'] = [node('按被试独立划分完成验证，仍缺少外部队列复核。')]
    restarted = TimelineSummaryService(TimelineSummaryStore(store.path), reader, lambda: provider, clock=lambda: now[0])
    assert not restarted.process_once()
    now[0] += 301
    assert restarted.process_once()
    assert provider.calls[-1]['native_session_ref'] == 'native-recorder-1'
    assert len(provider.calls) == 2
    assert restarted.query('quest-1')['nodes'][0]['summary'].startswith('按被试')


def test_changed_source_during_generation_preserves_provenance_then_updates(recorder):
    service, _, reader, provider, now = recorder
    def changed():
        now[0] += 301
        reader.nodes.update({'quest-1': [node('新增独立队列，正在复核。')]})
    provider.after_generate = changed
    assert service.process_once()
    result = service.query('quest-1')['nodes'][0]
    assert result['status'] == 'pending'
    assert result['source_hash'] != result['summarized_source_hash']
    assert result['summary'] == node()['content']['finding']
    assert service.process_once()
    result = service.query('quest-1')['nodes'][0]
    assert result['status'] == 'ready'
    assert result['source_hash'] == result['summarized_source_hash']
    assert result['summary'] == '新增独立队列，正在复核。'


def test_failure_keeps_previous_sentence_and_retries_with_recovered_session(recorder):
    service, _, reader, provider, now = recorder
    assert service.process_once()
    old = service.query('quest-1')['nodes'][0]['summary']
    now[0] += 301
    reader.nodes['quest-1'] = [node('两个外部队列可用。')]
    provider.failure = DraftingUnavailable('timeline_summary_output_invalid', native_session_ref='native-recovered')
    assert not service.process_once()
    assert service.query('quest-1')['nodes'][0]['status'] == 'failed'
    assert service.query('quest-1')['nodes'][0]['summary'] == old
    assert not service.process_once()
    assert len(provider.calls) == 2
    provider.failure = None
    now[0] += 301
    assert service.process_once()
    assert provider.calls[-1]['native_session_ref'] == 'native-recovered'
    assert service.query('quest-1')['nodes'][0]['summary'] == '两个外部队列可用。'


def test_pending_durable_job_after_restart_is_not_launched_twice(recorder):
    service, store, reader, provider, now = recorder
    service._observe('quest-1', now[0])
    job = store.claim(now[0], 'zh')
    provider.pending = True
    restarted = TimelineSummaryService(TimelineSummaryStore(store.path), reader, lambda: provider, clock=lambda: now[0])
    assert not restarted.process_once()
    assert not provider.calls
    assert store.claim(now[0] + 31, 'zh')['job_ref'] == job['job_ref']
    provider.pending = False
    now[0] += 31
    assert restarted.process_once()
    assert provider.calls[0]['job_ref'] == job['job_ref']
    assert provider.calls[0]['nodes'] == job['nodes']


def test_finished_result_survives_publication_source_failure(recorder):
    service, store, reader, provider, now = recorder
    def unavailable():
        now[0] += 301
        reader.broken.add('quest-1')
    provider.after_generate = unavailable
    assert not service.process_once()
    active = store.claim(now[0] + 31, 'zh')
    assert active['job_ref'] == provider.calls[0]['job_ref']
    reader.broken.clear()
    now[0] += 31
    restarted = TimelineSummaryService(TimelineSummaryStore(store.path), reader, lambda: provider, clock=lambda: now[0])
    assert restarted.process_once()
    assert len(provider.calls) == 1  # durable result replay, no new model call
    assert restarted.query('quest-1')['nodes'][0]['status'] == 'ready'


def test_source_failure_of_one_quest_does_not_block_another(recorder):
    service, _, reader, provider, _ = recorder
    reader.broken.add('quest-1')
    reader.nodes['quest-2'] = [node('第二个项目独立推进。')]
    assert service.process_once()
    assert provider.calls[0]['quest_ref'] == 'quest-2'
    assert service.query('quest-1')['nodes'] == []
    assert service.query('quest-2')['nodes'][0]['status'] == 'ready'


def test_source_and_prompt_versions_trigger_updates_but_queries_never_do(recorder):
    service, _, _, provider, now = recorder
    assert service.process_once()
    service._prompt_version = lambda: 'recorder-v2'
    for _ in range(3):
        service.query('quest-1')
    assert len(provider.calls) == 1
    now[0] += 301
    assert service.process_once()
    assert len(provider.calls) == 2
    assert provider.calls[0]['nodes'][0]['source_hash'] != provider.calls[1]['nodes'][0]['source_hash']


def test_timed_out_pending_job_must_confirm_cancellation_before_retry(recorder):
    service, store, _, provider, now = recorder
    service._observe('quest-1', now[0])
    job = store.claim(now[0], 'zh')
    provider.pending = True
    now[0] += 961
    assert not service.process_once()
    assert provider.cancelled == [job['job_ref']]
    assert not provider.calls
    now[0] += 301
    assert service.process_once()
    assert provider.calls[0]['job_ref'] != job['job_ref']


def test_late_duplicate_completion_cannot_replace_newer_summary(recorder):
    service, store, reader, provider, now = recorder
    assert service.process_once()
    old_request = provider.calls[0]
    now[0] += 301
    reader.nodes['quest-1'] = [node('已完成新的外部验证。')]
    assert service.process_once()
    store.publish(old_request, provider.results[old_request['job_ref']][0], 'old-native', now[0])
    store.fail(old_request, 'late_failure', now[0])
    result = service.query('quest-1')['nodes'][0]
    assert result['summary'] == '已完成新的外部验证。'
    assert result['status'] == 'ready'


def test_stop_leaves_durable_completion_for_next_startup(recorder):
    service, store, reader, provider, now = recorder
    provider.after_generate = service.request_stop
    assert not service.process_once()
    assert not service.process_once()
    restarted = TimelineSummaryService(TimelineSummaryStore(store.path), reader, lambda: provider, clock=lambda: now[0])
    assert restarted.process_once()
    assert len(provider.calls) == 1
    assert restarted.query('quest-1')['nodes'][0]['status'] == 'ready'


def test_disappeared_nodes_are_hidden_and_stale_generation_cannot_restore_them(recorder):
    service, _, reader, provider, now = recorder
    def removed():
        now[0] += 301
        reader.nodes.update({'quest-1': []})
    provider.after_generate = removed
    assert service.process_once()
    assert service.query('quest-1')['nodes'] == []


def test_lost_provider_response_cannot_launch_a_second_job(recorder, monkeypatch):
    service, store, _, provider, now = recorder

    def response_lost(**request):
        provider.calls.append(request)
        provider.pending = True
        raise DraftingUnavailable('codex_job_outcome_unknown')

    monkeypatch.setattr(provider, 'summarize', response_lost)
    assert not service.process_once()
    job_ref = provider.calls[0]['job_ref']
    now[0] += 31
    assert not service.process_once()
    assert len(provider.calls) == 1
    assert store.claim(now[0] + 31, 'zh')['job_ref'] == job_ref
    assert service.query('quest-1')['nodes'][0]['status'] == 'updating'


def test_unpublishable_finished_job_does_not_starve_other_quests(recorder):
    service, _, reader, provider, now = recorder
    reader.nodes['quest-2'] = [node('另一个项目可正常生成。')]
    def unavailable():
        now[0] += 301
        reader.broken.add('quest-1')
    provider.after_generate = unavailable
    assert not service.process_once()
    assert service.process_once()
    assert service.query('quest-2')['nodes'][0]['status'] == 'ready'
    assert [call['quest_ref'] for call in provider.calls] == ['quest-1', 'quest-2']
    reader.broken.clear()
    now[0] += 31
    assert service.process_once()
    assert service.query('quest-1')['nodes'][0]['status'] == 'ready'
    assert len(provider.calls) == 2


def test_terminal_jobs_release_full_inputs_only_after_publication(recorder):
    service, store, reader, provider, now = recorder
    def unavailable():
        now[0] += 301
        reader.broken.add('quest-1')
    provider.after_generate = unavailable
    assert not service.process_once()
    assert provider.finished == []
    with sqlite3.connect(store.path) as db:
        assert 'finding' in db.execute('SELECT request_json FROM jobs').fetchone()[0]
    reader.broken.clear()
    now[0] += 31
    assert service.process_once()
    assert provider.finished == [provider.calls[0]['job_ref']]
    with sqlite3.connect(store.path) as db:
        assert db.execute('SELECT request_json FROM jobs').fetchone()[0] == '{}'
    assert service.query('quest-1')['nodes'][0]['sources']


def test_default_scan_waits_five_minutes_and_survives_restart(recorder):
    service, store, reader, provider, now = recorder
    assert service.process_once()
    assert reader.calls == 1
    first = store.query('quest-1')
    assert first['scan_interval_seconds'] == 300
    assert first['next_scan_at'] == 400
    reader.nodes['quest-1'] = [node('新结果等待到扫描时间读取。')]
    now[0] = 399
    restarted = TimelineSummaryService(TimelineSummaryStore(store.path), reader, lambda: provider, clock=lambda: now[0])
    assert not restarted.process_once()
    assert reader.calls == 1 and len(provider.calls) == 1
    now[0] = 400
    assert restarted.process_once()
    assert reader.calls == 2 and len(provider.calls) == 2


def test_agent_can_extend_to_two_hours_then_shorten_with_new_progress(recorder):
    service, store, reader, provider, now = recorder
    provider.next_scan_seconds = 7200
    assert service.process_once()
    assert store.query('quest-1')['next_scan_at'] == 7300
    reader.nodes['quest-1'] = [node('等待已结束，正在快速推进。')]
    now[0] = 7299
    restarted = TimelineSummaryService(TimelineSummaryStore(store.path), reader, lambda: provider, clock=lambda: now[0])
    assert not restarted.process_once()
    assert len(provider.calls) == 1 and reader.calls == 1
    provider.next_scan_seconds = 300
    now[0] = 7300
    assert restarted.process_once()
    assert store.query('quest-1')['next_scan_at'] == 7600
    assert store.query('quest-1')['scan_interval_seconds'] == 300
    now[0] = 7600
    assert not restarted.process_once()
    assert reader.calls == 3 and len(provider.calls) == 2


@pytest.mark.parametrize('interval', [1, 299, 7201, None, True, '900'])
def test_invalid_agent_schedule_retains_summary_and_uses_five_minutes(recorder, interval):
    service, store, _, provider, _ = recorder
    provider.next_scan_seconds = interval
    assert service.process_once()
    result = store.query('quest-1')
    assert result['nodes'][0]['summary']
    assert result['scan_interval_seconds'] == 300


def test_source_read_failure_is_not_retried_before_five_minutes_even_after_restart(recorder):
    service, store, reader, provider, now = recorder
    reader.broken.add('quest-1')
    assert not service.process_once()
    now[0] = 399
    restarted = TimelineSummaryService(TimelineSummaryStore(store.path), reader, lambda: provider, clock=lambda: now[0])
    assert not restarted.process_once()
    assert reader.calls == 1 and not provider.calls
    reader.broken.clear()
    now[0] = 400
    assert restarted.process_once()
    assert reader.calls == 2


def test_existing_recorder_database_adopts_cadence_without_immediate_rescan(tmp_path):
    path = tmp_path / 'old-recorder.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('''CREATE TABLE quests(quest_ref TEXT PRIMARY KEY,revision INTEGER NOT NULL DEFAULT 0,
            native_session_ref TEXT,observed_at REAL NOT NULL DEFAULT 0)''')
        db.execute("INSERT INTO quests VALUES ('quest-1',7,'preserved-native',100)")
    store = TimelineSummaryStore(path)
    reader, provider, now = Reader(), Provider(), [200.0]
    service = TimelineSummaryService(store, reader, lambda: provider, clock=lambda: now[0])
    assert not service.process_once()
    assert not provider.calls and reader.calls == 0
    assert store.query('quest-1')['next_scan_at'] == 400
    now[0] = 400
    assert service.process_once()
    # Old sessions have no measured input budget; start fresh at the next due
    # scan while retaining their old logs and all persisted summaries.
    assert provider.calls[0]['native_session_ref'] is None


def test_failed_publication_read_cannot_bypass_scan_cadence(recorder):
    service, _, reader, provider, now = recorder
    def source_down_after_long_generation():
        now[0] += 301
        reader.broken.add('quest-1')
    provider.after_generate = source_down_after_long_generation
    assert not service.process_once()
    assert reader.calls == 2
    now[0] += 31
    assert service.process_once()  # Replays the already completed original snapshot.
    assert reader.calls == 2 and len(provider.calls) == 1


def test_later_historical_batch_cannot_postpone_urgent_scan_after_restart(recorder):
    service, store, reader, provider, now = recorder
    reader.nodes['quest-1'] = [dict(node(), node_key=f'stage:c{i}:plan') for i in range(21)]
    provider.next_scan_seconds = 300
    assert service.process_once()
    assert len(provider.calls[0]['nodes']) == 20
    deadline = store.query('quest-1')['next_scan_at']
    now[0] += 50
    provider.next_scan_seconds = 7200
    restarted = TimelineSummaryService(TimelineSummaryStore(store.path), reader, lambda: provider, clock=lambda: now[0])
    assert restarted.process_once()
    result = store.query('quest-1')
    assert result['scan_interval_seconds'] == 300
    assert result['next_scan_at'] == deadline
    assert reader.calls == 1 and len(provider.calls) == 2
