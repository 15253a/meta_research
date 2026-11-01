import json
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from meta_research.acquisition import AcquisitionBatchRequest, AcquisitionPaper, AcquisitionUnavailable
from meta_research.acquisition_root import CodexAcquisitionRootAdapter
from meta_research.composition import build_production_runtime
from meta_research.paths import prepare_data_root
from meta_research.power_inhibitors import OperatorAttestedPowerInhibitor
from meta_research.provider_supervisor import ensure_transport_key, read_transport_envelope
from test_external_root_resident_mcp import _AcquisitionDelegate, _HostProbe, _QuestDrafter


def _server(path):
    path.write_text('#!' + sys.executable + '\n' + r'''
import json,sys
from pathlib import Path
if '--version' in sys.argv:
 print('codex-cli 0.159.0');sys.exit(0)
if sys.argv[-2:]==['features','list']:
 print('multi_agent stable true');sys.exit(0)
root=Path(__file__);requests=root.with_suffix('.requests');count=root.with_suffix('.count')
if 'exec' in sys.argv:
 root.with_suffix('.exec-argv').write_text(json.dumps(sys.argv))
 with root.with_suffix('.exec-calls').open('a') as stream:stream.write(json.dumps(sys.argv)+'\n')
 if root.with_suffix('.strict-missing').exists() and 'resume' in sys.argv:
  sys.stderr.write('Error: thread/resume: thread/resume failed: no rollout found for thread id '+sys.argv[sys.argv.index('resume')+1]+' (code -32600)\n');sys.exit(1)
 sys.stdin.read()
 if root.with_suffix('.supplied').exists():
  output=json.loads(root.with_suffix('.payload').read_text())
  for item in output['results']:
   target=Path(item['path'])
   if not target.exists():
    target.write_bytes(root.with_suffix('.supplied').read_bytes())
    with root.with_suffix('.copies').open('a') as stream:stream.write(str(target)+'\n')
 Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(root.with_suffix('.payload').read_text())
 print(json.dumps({'type':'thread.started','thread_id':sys.argv[sys.argv.index('resume')+1] if 'resume' in sys.argv else 'strict-replacement-native'}))
 sys.exit(0)
thread=None
for line in sys.stdin:
 message=json.loads(line)
 with requests.open('a') as stream:stream.write(json.dumps(message)+'\n')
 method=message.get('method');params=message.get('params',{})
 if method=='initialized':continue
 if method=='thread/resume':
  behavior=root.with_suffix('.behavior').read_text() if root.with_suffix('.behavior').exists() else 'missing'
  if behavior!='unknown':
   error={'code':-32600,'message':'no rollout found for thread id '+params['threadId']}
   if behavior=='wrong_thread':error['message']='no rollout found for thread id another-thread'
   if behavior=='generic':error={'code':-32603,'message':'unrelated CLI failure'}
   print(json.dumps({'id':message['id'],'error':error}),flush=True);continue
  thread=params['threadId']
  print(json.dumps({'id':message['id'],'result':{'thread':{'id':thread}}}),flush=True);continue
 result={}
 if method=='thread/start':
  number=int(count.read_text())+1 if count.exists() else 1;count.write_text(str(number))
  thread='00000000-0000-4000-8000-'+str(number).zfill(12)
  result={'thread':{'id':thread}}
 if method=='turn/start':
  if root.with_suffix('.behavior').exists() and root.with_suffix('.behavior').read_text()=='unknown':sys.exit(0)
  result={'turn':{'id':'turn-'+thread,'status':'inProgress'}}
 print(json.dumps({'id':message['id'],'result':result}),flush=True)
 if method=='turn/start':
  observation={'cwd':str(Path.cwd()),'thread':thread,'files':sorted(p.name for p in Path.cwd().iterdir())}
  with root.with_suffix('.observations').open('a') as stream:stream.write(json.dumps(observation)+'\n')
  output=json.loads(root.with_suffix('.payload').read_text())
  for item in output.get('results',[]):
   if item['status']=='obtained':
    target=Path(item['path'])
    if not target.exists():
     target.write_text('bounded article body')
     with root.with_suffix('.downloads').open('a') as stream:stream.write(str(target)+'\n')
  print(json.dumps({'method':'item/completed','params':{'threadId':thread,'turnId':'turn-'+thread,
   'item':{'type':'agentMessage','id':'final','text':json.dumps(output)}}}),flush=True)
  print(json.dumps({'method':'turn/completed','params':{'threadId':thread,
   'turn':{'id':'turn-'+thread,'status':'completed'}}}),flush=True)
''', encoding='utf-8')
    path.chmod(0o700)
    return path


@pytest.fixture
def native_session(tmp_path):
    if os.name != 'posix':
        pytest.skip('The production signed provider supervisor runs on Linux')
    server = _server(tmp_path / 'native-codex')
    server.with_suffix('.payload').write_text(json.dumps({'status': 'ready',
        'browser_context_ref': None, 'reason_code': None, 'evidence': {
            'configuration_health': 'ready', 'browser_control': 'not_required', 'authorized_resource': 'oa_only'}}))
    data_root = prepare_data_root(tmp_path / 'data')
    provider = tmp_path / 'data' / 'acquisition-root-provider'
    adapter = CodexAcquisitionRootAdapter(provider, _AcquisitionDelegate(), executable=str(server))
    drafter = _QuestDrafter()
    runtime = build_production_runtime(data_root, acquisition_provider=adapter,
        proposal_drafter=drafter, intent_drafting_provider=drafter, host_compute_probe=_HostProbe(),
        power_inhibitor=OperatorAttestedPowerInhibitor(), startup_power_probe=False, startup_harness_diagnostics=False)
    try:
        runtime.configure_resident_mcp_endpoint('http://127.0.0.1:18768')
        creation = runtime.owners.human_collaboration.create_quest({}, 'native-replacement-create')
        session = runtime.owners.agent_runtime.prepare_acquisition_session(
            initialization_id=creation['initialization_id'], draft_revision=creation['quest_draft']['revision'],
            config={'mode': 'oa_only', 'library_entry_url': ''}, provider=adapter)
        assert session.status == 'ready', session
        binding = runtime.root_workspaces.bind_acquisition_session(session.session_ref)
        (binding.directory / 'download-notes.md').write_text('Reuse the existing article; preserve this session directory.')
        target = binding.directory / 'downloads'
        target.mkdir()
        (target / 'previous.html').write_text('previous verified body')
        request = AcquisitionBatchRequest('replacement-batch', 'oa_first_then_institution',
            (AcquisitionPaper('new-paper', 'Unfinished exact title', None, None, ()),),
            session_ref=session.session_ref, session_mode='oa_only', target_dir=str(target),
            provider_state_dir=str(tmp_path / 'private'))
        server.with_suffix('.payload').write_text(json.dumps({'results': [{'paper_id': 'new-paper',
            'status': 'obtained', 'path': str(target / 'new.html'), 'format': 'html', 'failure': None}]}))
        yield runtime, adapter, server, request, binding.directory, provider
    finally:
        runtime.close()


def test_signed_missing_native_replacement_preserves_workspace_and_reconciles_original_job(native_session):
    runtime, adapter, server, request, workspace, provider = native_session
    result = adapter.acquire(request)
    assert result[0].status == 'obtained'
    assert (workspace / 'downloads' / 'previous.html').read_text() == 'previous verified body'
    assert (workspace / 'download-notes.md').is_file()
    calls = [json.loads(line) for line in server.with_suffix('.requests').read_text().splitlines()]
    assert sum(call.get('method') == 'thread/start' for call in calls) == 2
    assert sum(call.get('method') == 'thread/resume' for call in calls) == 1
    observed = [json.loads(line) for line in server.with_suffix('.observations').read_text().splitlines()]
    assert [item['cwd'] for item in observed] == [str(workspace)] * 2
    assert 'download-notes.md' in observed[1]['files']
    _key_path, key = ensure_transport_key(provider)
    receipts = [read_transport_envelope(path, key) for path in sorted((provider / 'root-sessions').glob('*/turn-*.json'))]
    assert len(receipts) == 2
    replacement = receipts[1]['replacement']
    assert replacement['recovery_checkpoint']['native_session_ref'] == receipts[0]['native_session_ref']
    assert receipts[1]['previous_native_session_ref'] == receipts[0]['native_session_ref']
    assert receipts[1]['native_session_ref'] != receipts[0]['native_session_ref']
    prompts = list((provider / 'codex-root' / 'provider-operations').glob('*/acquisition-root-turn/prompt.txt'))
    replacement_prompt = next(path.read_text() for path in prompts if 'replacement_context=' in path.read_text())
    assert 'Unfinished exact title' in replacement_prompt
    assert 'previous.html' in replacement_prompt
    assert 'run_batch_download.py' in replacement_prompt
    before = {str(path): path.read_bytes() for path in provider.rglob('invocation.json')}
    restarted = CodexAcquisitionRootAdapter(provider, _AcquisitionDelegate(), executable=str(server))
    restarted.bind_workspaces(runtime.root_workspaces)
    restarted.bind_external_mcp(runtime.external_mcp)
    assert restarted.reconcile(request) == result
    assert {str(path): path.read_bytes() for path in provider.rglob('invocation.json')} == before
    assert len(server.with_suffix('.downloads').read_text().splitlines()) == 1
    assert server.with_suffix('.requests').read_text().splitlines() == [json.dumps(call) for call in calls]


@pytest.mark.parametrize('behavior', ['wrong_thread', 'generic', 'unknown'])
def test_unproven_native_failure_cannot_authorize_replacement(native_session, behavior):
    _runtime, adapter, server, request, _workspace, _provider = native_session
    server.with_suffix('.behavior').write_text(behavior)
    with pytest.raises(AcquisitionUnavailable):
        adapter.acquire(request)
    with pytest.raises(AcquisitionUnavailable):
        adapter.reconcile(request)
    calls = [json.loads(line) for line in server.with_suffix('.requests').read_text().splitlines()]
    assert sum(call.get('method') == 'thread/start' for call in calls) == 1
    assert sum(call.get('method') == 'thread/resume' for call in calls) == 1
    assert not server.with_suffix('.downloads').exists()


def test_provided_only_production_runner_keeps_strict_exec_boundary(native_session):
    _runtime, adapter, server, request, workspace, _provider = native_session
    before = server.with_suffix('.requests').read_bytes()
    supplied = workspace / 'downloads' / 'new.html'
    supplied.write_text('user-provided full text')
    result = adapter.acquire(replace(request, session_mode='provided_only'))
    assert result[0].path == str(supplied)
    assert supplied.read_text() == 'user-provided full text'
    assert server.with_suffix('.requests').read_bytes() == before
    argv = json.loads(server.with_suffix('.exec-argv').read_text())
    assert 'app-server' not in argv
    assert '--ignore-user-config' in argv and '--ignore-rules' in argv
    assert 'sandbox_workspace_write.network_access=false' in argv
    assert 'web_search="disabled"' in argv
    assert 'shell_environment_policy.inherit="none"' in argv
    assert argv[argv.index('--sandbox') + 1] == 'workspace-write'
    assert argv[argv.index('--add-dir') + 1] == request.target_dir
    assert argv[argv.index('--cd') + 1] == str(workspace)


def test_provided_only_missing_native_replaces_with_same_strict_policy_and_reconciles(native_session):
    runtime, adapter, server, request, workspace, provider = native_session
    request = replace(request, session_mode='provided_only')
    server.with_suffix('.strict-missing').touch()
    server.with_suffix('.supplied').write_bytes(b'user-supplied full article')
    before = server.with_suffix('.requests').read_bytes()
    result = adapter.acquire(request)
    assert Path(result[0].path).read_bytes() == b'user-supplied full article'
    assert (workspace / 'downloads' / 'previous.html').read_text() == 'previous verified body'
    assert (workspace / 'download-notes.md').is_file()
    calls = [json.loads(line) for line in server.with_suffix('.exec-calls').read_text().splitlines()]
    assert len(calls) == 2 and 'resume' in calls[0] and 'resume' not in calls[1]
    for argv in calls:
        assert '--ignore-user-config' in argv and '--ignore-rules' in argv
        assert 'sandbox_workspace_write.network_access=false' in argv
        assert argv[argv.index('--sandbox') + 1] == 'workspace-write'
        assert argv[argv.index('--cd') + 1] == str(workspace)
        assert argv[argv.index('--add-dir') + 1] == request.target_dir
    restarted = CodexAcquisitionRootAdapter(provider, _AcquisitionDelegate(), executable=str(server))
    restarted.bind_workspaces(runtime.root_workspaces)
    restarted.bind_external_mcp(runtime.external_mcp)
    assert restarted.reconcile(request) == result
    assert len(server.with_suffix('.exec-calls').read_text().splitlines()) == 2
    assert len(server.with_suffix('.copies').read_text().splitlines()) == 1
    assert server.with_suffix('.requests').read_bytes() == before
