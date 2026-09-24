import pytest

from meta_research.experiment_logs import ExperimentLogError, TargetExperimentLogs
from meta_research.target_progress import read_target_progress
from test_experiment_logs import Authority, bind, put


def test_progress_keeps_raw_history_and_only_delivers_appends(tmp_path):
    authority = Authority()
    root = bind(authority, tmp_path)
    raw = (b'epoch 1 normal\n' * 10000) + b'epoch 2 complete\n'
    path = put(root, 'logs/train.log', raw)
    logs = TargetExperimentLogs(authority, tmp_path)
    first = read_target_progress(logs, 'target-A', target_run_ref='run-A')
    fragment = first['fragments'][0]
    assert len(fragment['text'].encode()) <= 2048
    assert fragment['offset'] > 0
    assert first['state_changed']
    unchanged = read_target_progress(logs, 'target-A', target_run_ref='run-A', cursor=first['cursor'])
    assert unchanged['fragments'] == []
    assert unchanged['state_changed'] is False
    assert unchanged['suggested_poll_seconds'] == 30
    with path.open('ab') as stream:
        stream.write(b'WARNING: validation loss is NaN\n')
    delta = read_target_progress(logs, 'target-A', target_run_ref='run-A', cursor=unchanged['cursor'])
    assert delta['fragments'][0]['text'] == 'WARNING: validation loss is NaN\n'
    assert delta['fragments'][0]['anomalies'] == ['WARNING: validation loss is NaN']
    assert path.read_bytes() == raw + b'WARNING: validation loss is NaN\n'
    history = logs.read('target-A', fragment['log_ref'], before=fragment['offset'], stream_ref=fragment['stream_ref'])
    assert history['next_offset'] == fragment['offset']


def test_progress_reports_new_stream_and_status_without_fabricating_outcomes(tmp_path):
    authority = Authority()
    root = bind(authority, tmp_path)
    path = put(root, 'logs/train.log', b'epoch 1\n')
    logs = TargetExperimentLogs(authority, tmp_path)
    first = read_target_progress(logs, 'target-A', target_run_ref='run-A')
    path.write_bytes(b'failed\n')
    authority.admissions['target-A'].status = 'suspended'
    update = read_target_progress(logs, 'target-A', target_run_ref='run-A', cursor=first['cursor'])
    assert update['notices'][0]['kind'] == 'stream_replaced'
    assert update['fragments'][0]['text'] == 'failed\n'
    assert update['target_status'] == 'suspended'
    assert update['state_changed']
    assert 'evaluation_status' not in update


def test_progress_bounds_many_logs_and_eventually_delivers_each(tmp_path):
    authority = Authority()
    root = bind(authority, tmp_path)
    for index in range(24):
        put(root, f'logs/train-{index}.log', b'progress\n' * 1000)
    logs = TargetExperimentLogs(authority, tmp_path)
    cursor, refs = None, set()
    for _ in range(4):
        page = read_target_progress(logs, 'target-A', target_run_ref='run-A', cursor=cursor)
        assert len(page['fragments']) <= 8
        assert sum(len(fragment['text'].encode()) for fragment in page['fragments']) <= 16384
        refs.update(fragment['log_ref'] for fragment in page['fragments'])
        cursor = page['cursor']
    assert len(refs) == 24
    assert not page['has_more']


def test_busy_logs_do_not_starve_later_streams(tmp_path):
    authority = Authority()
    root = bind(authority, tmp_path)
    paths = [put(root, f'logs/train-{index}.log', b'epoch 1\n') for index in range(20)]
    logs = TargetExperimentLogs(authority, tmp_path)
    cursor, refs = None, set()
    for _ in range(3):
        page = read_target_progress(logs, 'target-A', target_run_ref='run-A', cursor=cursor)
        refs.update(item['log_ref'] for item in page['fragments'])
        cursor = page['cursor']
        for path in paths:
            with path.open('ab') as stream:
                stream.write(b'epoch 2\n')
    assert len(refs) == 20


def test_truncated_discovery_rotation_keeps_its_returned_cursor_readable(tmp_path):
    authority = Authority()
    root = bind(authority, tmp_path)
    for index in range(64):
        put(root, f'logs/train-{index:02}.log', b'epoch 1\n')
    # A deep output directory makes discovery explicitly incomplete even when
    # exactly 64 current log streams are returned.
    put(root, 'outputs/a/b/c/d/e/eval.log', b'older history\n')
    logs = TargetExperimentLogs(authority, tmp_path)
    cursor = None
    for _ in range(8):
        page = read_target_progress(logs, 'target-A', target_run_ref='run-A', cursor=cursor)
        cursor = page['cursor']
    assert page['discovery_truncated']
    (root / 'logs/train-00.log').rename(root / 'logs/train-retired.txt')
    put(root, 'logs/train-new.log', b'new attempt\n')
    rotated = read_target_progress(logs, 'target-A', target_run_ref='run-A', cursor=cursor)
    assert any(notice['kind'] == 'cursor_tracking_evicted' for notice in rotated['notices'])
    following = read_target_progress(logs, 'target-A', target_run_ref='run-A', cursor=rotated['cursor'])
    assert following['fragments'] == []
    assert (root / 'logs/train-retired.txt').read_bytes() == b'epoch 1\n'


@pytest.mark.parametrize('cursor', ['not-base64', 'W10=', 'e30='])
def test_progress_rejects_invalid_cursor(tmp_path, cursor):
    with pytest.raises(ExperimentLogError, match='experiment_log_cursor_invalid'):
        read_target_progress(TargetExperimentLogs(Authority(), tmp_path), 'target-A', target_run_ref='run-A', cursor=cursor)


def test_progress_cursor_cannot_cross_target_run(tmp_path):
    authority = Authority()
    bind(authority, tmp_path)
    logs = TargetExperimentLogs(authority, tmp_path)
    first = read_target_progress(logs, 'target-A', target_run_ref='run-A')
    with pytest.raises(ExperimentLogError, match='experiment_log_reset_required'):
        read_target_progress(logs, 'target-B', target_run_ref='run-B', cursor=first['cursor'])
