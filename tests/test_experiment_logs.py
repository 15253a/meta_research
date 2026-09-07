from pathlib import Path
from types import SimpleNamespace
import os
import subprocess
import sys
import threading

from fastapi.testclient import TestClient
import pytest

from meta_research.experiment_logs import TargetExperimentLogs, ExperimentLogError
from meta_research.owners.common import canonical_hash
from meta_research.web import create_app
from test_public_plan_stage import _runtime, _DeterministicIdeaSkill, _DeterministicPlanSkill


class Authority:
    def __init__(self):
        self.admissions = {}
        self.workspaces = {}

    def query_target_harness_admission(self, target):
        return self.admissions.get(target)

    def query_target_workspace(self, run):
        return self.workspaces.get(run)


def bind(authority, base, target='target-A', run='run-A', workspace='workspace-A'):
    authority.admissions[target] = SimpleNamespace(target_run_ref=run, root_session_ref='root-'+run,
        execution_attempt_ref='attempt-'+run, execution_fence_ref='fence-'+run, status='running')
    authority.workspaces[run] = SimpleNamespace(target_ref=target, target_run_ref=run,
        root_session_ref='root-'+run, target_attempt_ref='attempt-'+run, target_fence_ref='fence-'+run,
        workspace_ref=workspace)
    root = base / canonical_hash({'workspace_ref': workspace})
    root.mkdir(parents=True, exist_ok=True)
    return root


def put(root, path, data):
    file = root / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(data)
    return file


def error(code, fn):
    with pytest.raises(ExperimentLogError) as caught:
        fn()
    assert caught.value.code == code


def test_discovers_only_bounded_output_logs_not_inputs_sources_or_arbitrary_commands(tmp_path):
    authority = Authority()
    root = bind(authority, tmp_path)
    for path in ('logs/train.log', 'logs/eval.log', 'implementation/logs/training-fold1.log', 'outputs/evaluation.stderr'):
        put(root, path, b'epoch 1\n')
    for path in ('inputs/train.log', 'implementation/tests/train.log', 'implementation/source/train.log',
                 '.git/train.log', 'node_modules/logs/train.log', 'stdout.jsonl', 'logs/setup.log', 'logs/restraint.log'):
        put(root, path, b'must not be displayed')
    service = TargetExperimentLogs(authority, tmp_path)
    result = service.list('target-A', target_run_ref='run-A')
    assert result['status'] == 'ready'
    assert {item['relative_path'] for item in result['logs']} == {
        'logs/train.log', 'logs/eval.log', 'implementation/logs/training-fold1.log', 'outputs/evaluation.stderr'}
    assert result['target_status'] == 'running'
    assert 'process_alive' not in result
    assert all(not item['relative_path'].startswith('/') for item in result['logs'])
    assert len(result['logs']) == 4


def test_growing_utf8_stream_preserves_ansi_carriage_returns_and_half_character(tmp_path):
    authority = Authority()
    root = bind(authority, tmp_path)
    prefix = b'\x1b[32mepoch 1\x1b[0m\rprogress 20%\n\n'
    file = put(root, 'logs/train.log', prefix + '中'.encode()[:2])
    service = TargetExperimentLogs(authority, tmp_path)
    log = service.list('target-A')['logs'][0]
    first = service.read('target-A', log['log_ref'])
    assert first['text'] == prefix.decode()
    assert first['pending_utf8_bytes'] == 2
    assert first['next_offset'] == len(prefix)
    with file.open('ab') as stream:
        stream.write('中'.encode()[2:] + '\n第二轮\n'.encode())
    second = service.read('target-A', log['log_ref'], after=first['next_offset'], stream_ref=first['stream_ref'])
    assert second['text'] == '中\n第二轮\n'
    assert second['stream_ref'] == first['stream_ref']
    assert second['source_caught_up'] is True
    assert second['decode_replacements'] is False


def test_default_is_bounded_tail_and_history_aligns_unicode(tmp_path):
    authority = Authority()
    root = bind(authority, tmp_path)
    content = ('训练甲乙丙丁\n' * 10000).encode()
    put(root, 'logs/train.log', content)
    service = TargetExperimentLogs(authority, tmp_path)
    log = service.list('target-A')['logs'][0]
    tail = service.read('target-A', log['log_ref'])
    assert tail['offset'] > 0
    assert 65533 <= len(tail['text'].encode()) <= 65536
    assert tail['text'].encode() == content[tail['offset']:tail['next_offset']]
    previous = service.read('target-A', log['log_ref'], before=tail['offset'], stream_ref=tail['stream_ref'])
    assert previous['next_offset'] == tail['offset']
    assert previous['text'].encode() == content[previous['offset']:tail['offset']]
    assert previous['decode_replacements'] is False


def test_log_grows_while_real_writer_process_is_still_alive(tmp_path):
    authority = Authority()
    root = bind(authority, tmp_path)
    file = root/'logs/train.log'
    file.parent.mkdir()
    script = '''import sys
with open(sys.argv[1], 'ab', buffering=0) as log:
    log.write(b'epoch 1 loss=0.8\\n')
    print('ready', flush=True)
    sys.stdin.readline()
    log.write(b'epoch 2 loss=0.6\\n')
    print('grew', flush=True)
    sys.stdin.readline()
'''
    process = subprocess.Popen([sys.executable, '-u', '-c', script, str(file)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == 'ready'
        assert process.poll() is None
        service = TargetExperimentLogs(authority, tmp_path)
        ref = service.list('target-A')['logs'][0]['log_ref']
        first = service.read('target-A', ref)
        assert first['text'] == 'epoch 1 loss=0.8\n'
        process.stdin.write('grow\n')
        process.stdin.flush()
        assert process.stdout.readline().strip() == 'grew'
        assert process.poll() is None
        next_page = service.read('target-A', ref, after=first['next_offset'], stream_ref=first['stream_ref'])
        assert next_page['text'] == 'epoch 2 loss=0.6\n'
        assert process.poll() is None
    finally:
        process.communicate('exit\n', timeout=5)


def test_concurrent_list_readers_do_not_treat_append_as_truncation(tmp_path):
    authority = Authority()
    root = bind(authority, tmp_path)
    file = put(root, 'logs/train.log', b'epoch 1\n')
    service = TargetExperimentLogs(authority, tmp_path)
    first = service.list('target-A')['logs'][0]
    paused = threading.Event()
    resume = threading.Event()
    mutex = threading.RLock()

    class GateLock:
        def __enter__(self):
            if threading.current_thread().name == 'delayed-log-reader':
                paused.set()
                assert resume.wait(5)
            mutex.acquire()

        def __exit__(self, *_args):
            mutex.release()

    service._lock = GateLock()
    observed = []
    errors = []

    def delayed():
        try:
            observed.append(service.list('target-A')['logs'][0])
        except BaseException as exception:
            errors.append(exception)

    thread = threading.Thread(target=delayed, name='delayed-log-reader')
    thread.start()
    try:
        assert paused.wait(5)
        with file.open('ab') as stream:
            stream.write(b'epoch 2\n')
        newer = service.list('target-A')['logs'][0]
    finally:
        resume.set()
        thread.join(5)
    assert not thread.is_alive()
    assert not errors
    assert observed[0]['source_bytes'] == newer['source_bytes']
    assert observed[0]['stream_ref'] == newer['stream_ref'] == first['stream_ref']


def test_rotation_truncate_regrowth_and_reader_restart_invalidate_old_stream(tmp_path):
    authority = Authority()
    root = bind(authority, tmp_path)
    file = put(root, 'logs/train.log', b'first epoch\n' * 1000)
    service = TargetExperimentLogs(authority, tmp_path)
    log_ref = service.list('target-A')['logs'][0]['log_ref']
    first = service.read('target-A', log_ref)
    replacement = put(root, 'logs/replacement.tmp', b'new epoch\n')
    os.replace(replacement, file)
    error('experiment_log_reset_required', lambda: service.read('target-A', log_ref, after=first['next_offset'], stream_ref=first['stream_ref']))
    second = service.read('target-A', log_ref)
    assert second['stream_ref'] != first['stream_ref']
    file.write_bytes(b'copytruncate grew back\n' * 3000)
    error('experiment_log_reset_required', lambda: service.read('target-A', log_ref, after=second['next_offset'], stream_ref=second['stream_ref']))
    third = service.read('target-A', log_ref)
    file.write_bytes(b'')
    error('experiment_log_reset_required', lambda: service.read('target-A', log_ref, after=third['next_offset'], stream_ref=third['stream_ref']))
    fresh = service.read('target-A', log_ref)
    restarted = TargetExperimentLogs(authority, tmp_path)
    error('experiment_log_reset_required', lambda: restarted.read('target-A', log_ref, after=fresh['next_offset'], stream_ref=fresh['stream_ref']))


def test_cross_target_run_binding_path_traversal_links_and_nonfiles_are_rejected(tmp_path):
    authority = Authority()
    root_a = bind(authority, tmp_path)
    root_b = bind(authority, tmp_path, 'target-B', 'run-B', 'workspace-B')
    file_a = put(root_a, 'logs/train.log', b'A-only')
    file_b = put(root_b, 'logs/eval.log', b'B-only')
    (root_a/'logs/eval.log').symlink_to(file_b)
    (root_a/'outputs').symlink_to(root_b/'logs', target_is_directory=True)
    os.link(file_b, root_a/'logs/training.log')
    os.mkfifo(root_a/'logs/evaluation.log')
    service = TargetExperimentLogs(authority, tmp_path)
    logs = service.list('target-A')['logs']
    assert [item['relative_path'] for item in logs] == ['logs/train.log']
    ref = logs[0]['log_ref']
    error('experiment_log_not_found', lambda: service.read('target-B', ref))
    error('experiment_log_not_found', lambda: service.read('target-A', '../logs/train.log'))
    error('experiment_log_reset_required', lambda: service.list('target-A', target_run_ref='run-B'))
    authority.workspaces['run-A'].target_ref = 'target-B'
    error('experiment_log_workspace_binding_invalid', lambda: service.list('target-A'))
    assert file_a.read_bytes() == b'A-only'


def test_no_log_is_explicit_empty_and_no_workspace_is_unavailable(tmp_path):
    authority = Authority()
    bind(authority, tmp_path)
    service = TargetExperimentLogs(authority, tmp_path)
    assert service.list('target-A')['status'] == 'empty'
    missing = service.list('target-missing')
    assert missing['status'] == 'unavailable'
    assert missing['reason']['code'] == 'experiment_log_workspace_unavailable'


def test_http_auth_and_cursor_errors_are_stable(tmp_path, monkeypatch):
    monkeypatch.delenv('META_RESEARCH_TRUST_SSH_LOOPBACK', raising=False)
    runtime = _runtime(tmp_path/'http', idea_skill=_DeterministicIdeaSkill(), plan_skill=_DeterministicPlanSkill(no_gap=False))
    authority = Authority()
    root = bind(authority, runtime.data_root.run/'target-workspaces')
    put(root, 'logs/train.log', b'epoch 1\n')
    actual_authority = runtime.target_run_authorities.agent_runtime
    monkeypatch.setattr(actual_authority, 'query_target_harness_admission', authority.query_target_harness_admission)
    monkeypatch.setattr(actual_authority, 'query_target_workspace', authority.query_target_workspace)
    client = TestClient(create_app(runtime, base_url='http://testserver', control_key='test-only-key'))
    try:
        path = '/api/v1/bundle/targets/target-A/experiment-logs'
        assert client.get(path).status_code == 401
        token = runtime.authentication.issue_bootstrap_token()
        assert client.post('/auth/bootstrap', headers={'Origin':'http://testserver'}, json={'token':token}).status_code == 200
        listed = client.get(path, params={'target_run_ref':'run-A'})
        assert listed.status_code == 200
        ref = listed.json()['logs'][0]['log_ref']
        tail = client.get(path+'/'+ref).json()
        assert tail['text'] == 'epoch 1\n'
        stale = client.get(path, params={'target_run_ref':'run-other'})
        assert stale.status_code == 409
        assert stale.json()['detail']['code'] == 'experiment_log_reset_required'
        no_stream = client.get(path+'/'+ref, params={'after':0})
        assert no_stream.status_code == 422
        assert no_stream.json()['detail']['code'] == 'experiment_log_cursor_invalid'
        conflict = client.get(path+'/'+ref, params={'after':0, 'before':1, 'stream_ref':tail['stream_ref']})
        assert conflict.status_code == 422
        assert client.get(path+'/unknown').status_code == 404
    finally:
        client.close()
        runtime.close()
