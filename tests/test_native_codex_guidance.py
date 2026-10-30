"""Exercise the actual native bridge against an external app-server boundary."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from meta_research.provider_supervisor import (
    ensure_transport_key, read_transport_envelope, write_transport_envelope,
)
from meta_research.native_codex_bridge import reconcile_native_operation


_SERVER = r'''
import json,sys
from pathlib import Path
if '--version' in sys.argv:
 print('codex-cli 0.159.0');sys.exit(0)
if sys.argv[-2:]==['features','list']:
 print('multi_agent stable true');sys.exit(0)
requests = Path(__file__).with_suffix('.requests')
lost = 'lost' in Path(__file__).name
thread, turn, delivered = 'native-root', 'active-turn', None
def send(value): print(json.dumps(value),flush=True)
for line in sys.stdin:
    message=json.loads(line)
    with requests.open('a') as stream: stream.write(json.dumps(message)+'\n')
    method=message.get('method'); params=message.get('params',{})
    response={}
    if method=='initialized': continue
    if method in ('thread/start','thread/resume'):
        thread=params.get('threadId',thread); response={'thread':{'id':thread}}
    elif method=='turn/start': response={'turn':{'id':turn,'status':'inProgress'}}
    elif method=='turn/steer':
        delivered=params
        if lost:
            send({'id':message['id'],'error':{'code':-32000,'message':'reply lost after native acceptance'}})
            continue
        response={'turnId':turn}
    elif method=='thread/items/list':
        response={'nextCursor':None,'data':[{'turnId':turn,'item':
            {'type':'userMessage','id':'native-input','clientId':delivered['clientUserMessageId'],
             'content':delivered['input']}}]}
    send({'id':message['id'],'result':response})
    if method=='turn/start':
        send({'method':'turn/started','params':{'threadId':thread,'turn':{'id':turn,'status':'inProgress'}}})
    if method==('thread/items/list' if lost else 'turn/steer'):
        payload=Path(__file__).with_suffix('.payload')
        send({'method':'item/completed','params':{'threadId':thread,'turnId':turn,
            'item':{'type':'agentMessage','id':'final','text':payload.read_text() if payload.exists() else '{"ok":true}'}}})
        send({'method':'turn/completed','params':{'threadId':thread,
            'turn':{'id':turn,'status':'completed'}}})
'''


@pytest.mark.parametrize("unknown", [False, True])
@pytest.mark.parametrize("resume", [False, True])
def test_native_input_arrives_during_active_turn_and_uncertain_send_is_not_replayed(tmp_path, unknown, resume):
    if os.name != "posix":
        pytest.skip("The production durable provider process boundary runs on Linux")
    server = tmp_path / ("lost-codex" if unknown else "codex")
    server.write_text("#!" + sys.executable + "\n" + _SERVER)
    server.chmod(0o700)
    _key_path, key = ensure_transport_key(tmp_path)
    directory = tmp_path / "provider-operations" / "ab" / ("a" * 64)
    directory.mkdir(parents=True)
    result, schema = directory / "last-message.json", directory / "output-schema.json"
    schema.write_text('{"type":"object"}')
    argv = [str(server), "exec", "--model", "test-model", "--cd", str(tmp_path),
            "--json", "--output-schema", str(schema), "--output-last-message", str(result)]
    argv.extend(["resume", "original-native-root", "-"] if resume else ["-"])
    original = directory / "argv.json"
    write_transport_envelope(original, {"argv": argv}, key)
    process = subprocess.Popen([sys.executable, "-m", "meta_research.native_codex_bridge",
        "--provider-argv", str(original), "--operation-directory", str(directory)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        process.stdin.write(b"Work in this root until a new instruction arrives.")
        process.stdin.close(); process.stdin = None
        requests = server.with_suffix(".requests")
        deadline = time.monotonic() + 8
        while not requests.exists() or '"method": "turn/start"' not in requests.read_text():
            assert process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert process.poll() is None
        mailbox = directory / "live-guidance"
        mailbox.mkdir()
        command_id = "b" * 64
        write_transport_envelope(mailbox / (command_id + ".json"), {
            "id": command_id, "message": {"kind": "confirmed_guidance_change",
                "change": {"status": "active", "text": "Keep the exact calibration condition."}}}, key)
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr.decode()
        assert json.loads(result.read_text()) == {"ok": True}
        observed = [json.loads(line) for line in requests.read_text().splitlines()]
        assert sum(item.get("method") == "turn/start" for item in observed) == 1
        steers = [item["params"] for item in observed if item.get("method") == "turn/steer"]
        assert len(steers) == 1
        assert steers[0]["threadId"] == ("original-native-root" if resume else "native-root")
        assert steers[0]["expectedTurnId"] == "active-turn"
        assert "Keep the exact calibration condition." in steers[0]["input"][0]["text"]
        receipt = read_transport_envelope(mailbox / (command_id + ".receipt"), key)
        assert receipt["status"] == "accepted"
        assert receipt["reconciled"] is unknown
        events = [json.loads(line) for line in stdout.splitlines()]
        assert any(item.get("type") == "thread.started" for item in events)
        assert events[-1]["type"] == "turn.completed"
    finally:
        if process.poll() is None:
            process.terminate(); process.communicate(timeout=5)


def _lost_start_server(server):
    server.write_text("#!" + sys.executable + "\n" + r'''
import json,sys
from pathlib import Path
if '--version' in sys.argv:
 print('codex-cli 0.159.0');sys.exit(0)
if sys.argv[-2:]==['features','list']:
 print('multi_agent stable true');sys.exit(0)
state=Path(__file__).with_suffix('.state');requests=Path(__file__).with_suffix('.requests')
for line in sys.stdin:
 message=json.loads(line)
 with requests.open('a') as stream:stream.write(json.dumps(message)+'\n')
 method=message.get('method');params=message.get('params',{})
 if method=='initialized':continue
 result={}
 if method=='thread/resume':
  print(json.dumps({'id':message['id'],'error':{'code':-32603,
   'message':'required MCP initialization has no live grant'}}),flush=True)
  continue
 if method=='thread/start':result={'thread':{'id':'original-native-root'}}
 if method=='turn/start':
  payload=Path(__file__).with_suffix('.payload')
  final=payload.read_text() if payload.exists() else '{"ok":true}'
  terminal=Path(__file__).with_suffix('.terminal')
  status=terminal.read_text() if terminal.exists() else 'completed'
  state.write_text(json.dumps({'id':'original-turn','status':status,'error':
   None if status=='completed' else {'message':'The original native turn ended '+status,'codexErrorInfo':'other'},'items':[
   {'id':'input','type':'userMessage','clientId':params['clientUserMessageId'],'content':params['input']}]+
   ([{'id':'output','type':'agentMessage','text':final}] if status=='completed' else [])}))
  sys.exit(0)
 if method=='thread/read':result={'thread':{'id':'original-native-root','turns':[json.loads(state.read_text())]}}
 print(json.dumps({'id':message['id'],'result':result}),flush=True)
''')
    server.chmod(0o700)


def test_lost_turn_start_reply_is_reconciled_from_the_exact_native_turn_without_starting_again(tmp_path):
    if os.name != "posix":
        pytest.skip("The production durable provider process boundary runs on Linux")
    server = tmp_path / "lost-start-codex"
    _lost_start_server(server)
    _key_path, key = ensure_transport_key(tmp_path)
    directory = tmp_path / "provider-operations" / "aa" / ("c" * 64)
    directory.mkdir(parents=True)
    source = directory / "native-provider-argv.json"
    write_transport_envelope(source, {"argv": [str(server), "exec", "--json", "-"]}, key)
    completed = subprocess.run([sys.executable, "-m", "meta_research.native_codex_bridge",
        "--provider-argv", str(source), "--operation-directory", str(directory)],
        input="One exact original operation.", text=True, capture_output=True, timeout=10)
    assert completed.returncode == 75, completed.stderr
    assert "meta_research.native_outcome_unknown" in completed.stdout
    start = read_transport_envelope(directory / "native-turn-start.json", key)
    recovered = reconcile_native_operation(directory)
    assert recovered is not None
    message, native, stream = recovered
    assert json.loads(message) == {"ok": True}
    assert native == "original-native-root"
    assert json.loads(stream.splitlines()[-1])["type"] == "turn.completed"
    assert read_transport_envelope(directory / "native-turn-start.json", key) == start
    assert reconcile_native_operation(directory) == recovered
    calls = [json.loads(line)["method"] for line in server.with_suffix(".requests").read_text().splitlines()]
    assert calls.count("turn/start") == 1
    assert calls.count("thread/read") == 1
    assert calls.count("thread/resume") == 0  # Metadata reads need no live MCP grant.


@pytest.mark.parametrize('status', ['failed', 'interrupted'])
def test_unknown_native_turn_terminal_status_is_not_perpetual_pending(tmp_path, status):
    if os.name != 'posix':
        pytest.skip('The production durable provider boundary runs on Linux')
    server = tmp_path / 'lost-start-codex'
    _lost_start_server(server)
    server.with_suffix('.terminal').write_text(status)
    _key_path, key = ensure_transport_key(tmp_path)
    directory = tmp_path / 'provider-operations' / 'aa' / ('c' * 64)
    directory.mkdir(parents=True)
    source = directory / 'native-provider-argv.json'
    write_transport_envelope(source, {'argv': [str(server), 'exec', '--json', '-']}, key)
    initial = subprocess.run([sys.executable, '-m', 'meta_research.native_codex_bridge',
        '--provider-argv', str(source), '--operation-directory', str(directory)],
        input='One exact original operation.', text=True, capture_output=True, timeout=10)
    assert initial.returncode == 75
    with pytest.raises(RuntimeError, match='provider_native_turn_' + status) as failure:
        reconcile_native_operation(directory)
    assert failure.value.status == status
    assert failure.value.native_session_ref == 'original-native-root'
    assert failure.value.checkpoint['native_turn_ref'] == 'original-turn'
    assert failure.value.checkpoint['native_turn_status'] == status
    assert 'turn.failed' in failure.value.stdout
    calls = [json.loads(line)['method'] for line in server.with_suffix('.requests').read_text().splitlines()]
    assert calls.count('turn/start') == 1
    assert calls.count('thread/read') == 1


def test_driver_and_production_adapter_reconcile_unknown_generate_in_the_original_operation(tmp_path):
    if os.name != "posix":
        pytest.skip("The production durable provider process boundary runs on Linux")
    from meta_research.idea_skill import CodexIdeaSkillAdapter
    from test_public_idea_stage import _runtime, _confirm_direct_quest, _DeterministicIdeaSkill

    server = tmp_path / "lost-stage-start-codex"
    _lost_start_server(server)
    observed = []

    class NativeIdea(CodexIdeaSkillAdapter):
        def generate_draft(self, request):
            candidate = _DeterministicIdeaSkill().generate_draft(request)
            server.with_suffix('.payload').write_text(json.dumps({"outcome": candidate.draft}))
            result = super().generate_draft(request)
            observed.append(result)
            return result

    provider = NativeIdea(tmp_path / "native-provider", executable=str(server))
    runtime = _runtime(tmp_path / "runtime", provider)
    runtime.configure_resident_mcp_endpoint("http://127.0.0.1:8999")
    try:
        _confirm_direct_quest(runtime)
        for _ in range(12):
            runtime.idea_stage.process_once()
            if observed:
                break
        assert len(observed) == 1, runtime.idea_stage.transient_error
        assert observed[0].primary_session_ref == "original-native-root"
        calls = [json.loads(line)["method"] for line in server.with_suffix('.requests').read_text().splitlines()]
        assert calls.count("turn/start") == 1
        assert calls.count("thread/read") == 1
        assert calls.count("thread/resume") == 0
        operations = list((tmp_path / "native-provider" / "provider-operations").glob("*/*/native-reconciled.json"))
        assert len(operations) == 1
        operation = operations[0].parent
        _key_path, key = ensure_transport_key(tmp_path / 'native-provider')
        signed_argv = read_transport_envelope(operation / 'native-provider-argv.json', key)['argv']
        assert signed_argv[signed_argv.index('--output-last-message') + 1] == str(operation / 'last-message.json')
        assert not any(value.startswith('/proc/') for value in signed_argv)
    finally:
        runtime.close()


def test_driver_replaces_a_proven_missing_native_session_using_the_actual_supervisor_seal(tmp_path):
    if os.name != 'posix':
        pytest.skip('The production durable provider boundary runs on Linux')
    from meta_research.idea_skill import CodexIdeaSkillAdapter, IdeaSkillUnavailable
    from meta_research.owners.common import OwnerConflict
    from test_public_idea_stage import _runtime, _confirm_direct_quest, _DeterministicIdeaSkill

    server = tmp_path / 'missing-rollout-codex'
    server.write_text('#!' + sys.executable + '\n' + r'''
import json,sys
from pathlib import Path
if '--version' in sys.argv:
 print('codex-cli 0.159.0');sys.exit(0)
if sys.argv[-2:]==['features','list']:
 print('multi_agent stable true');sys.exit(0)
requests=Path(__file__).with_suffix('.requests');count=Path(__file__).with_suffix('.count')
thread=None
for line in sys.stdin:
 message=json.loads(line)
 with requests.open('a') as stream:stream.write(json.dumps(message)+'\n')
 method=message.get('method');params=message.get('params',{})
 if method=='initialized':continue
 if method=='thread/resume':
  print(json.dumps({'id':message['id'],'error':{'code':-32600,
   'message':'no rollout found for thread id '+params['threadId']}}),flush=True)
  continue
 result={}
 if method=='thread/start':
  number=int(count.read_text())+1 if count.exists() else 1;count.write_text(str(number))
  thread='00000000-0000-4000-8000-'+str(number).zfill(12)
  result={'thread':{'id':thread}}
 if method=='turn/start':result={'turn':{'id':'turn-'+thread,'status':'inProgress'}}
 print(json.dumps({'id':message['id'],'result':result}),flush=True)
 if method=='turn/start':
  print(json.dumps({'method':'item/completed','params':{'threadId':thread,'turnId':'turn-'+thread,
   'item':{'type':'agentMessage','id':'final','text':Path(__file__).with_suffix('.payload').read_text()}}}),flush=True)
  print(json.dumps({'method':'turn/completed','params':{'threadId':thread,
   'turn':{'id':'turn-'+thread,'status':'completed'}}}),flush=True)
''')
    server.chmod(0o700)
    generated, replacement_requests, errors = [], [], []

    class NativeIdea(CodexIdeaSkillAdapter):
        def generate_draft(self, request):
            if request.native_replacement_context is not None:
                replacement_requests.append(request)
            candidate = _DeterministicIdeaSkill().generate_draft(request)
            if not generated:
                # A structurally valid title-copy candidate is rejected by RG.
                # Its real same-root correction must resume the old native id.
                candidate.draft['candidates'][0]['direction'] = request.accepted_question_content['title']
            server.with_suffix('.payload').write_text(json.dumps({'outcome': candidate.draft}))
            try:
                result = super().generate_draft(request)
            except IdeaSkillUnavailable as error:
                errors.append(error)
                raise
            generated.append(result)
            return result

    provider = NativeIdea(tmp_path / 'native-provider', executable=str(server))
    runtime = _runtime(tmp_path / 'runtime', provider)
    runtime.configure_resident_mcp_endpoint('http://127.0.0.1:8999')
    try:
        created = _confirm_direct_quest(runtime)
        engine = runtime.owners.advancement_engine
        cycle = engine.query_initial_cycle(created['initialization_id'])
        for _ in range(3):
            assert runtime.idea_stage.process_once()
        request = engine.query_idea_stage_request(cycle.cycle_ref)
        owner = runtime.owners.agent_runtime
        old = owner.query_idea_stage_run(request.request_ref)
        assert old.native_session_ref == '00000000-0000-4000-8000-000000000001'
        assert old.primary_draft is not None
        for _ in range(16):
            runtime.idea_stage.process_once()
            if len(generated) == 2:
                break
        assert len(errors) == 1, (runtime.idea_stage.transient_error,
            owner.query_idea_stage_run(request.request_ref), server.with_suffix('.requests').read_text())
        assert errors[0].code == 'codex_native_session_unavailable'
        proof = errors[0].recovery_checkpoint
        assert proof['schema_ref'] == 'meta-research/provider-native-session-unavailable/v1'
        assert proof['native_session_ref'] == old.native_session_ref
        # The mature supervisor publishes and seals its empty private result
        # even on return 70. This exact non-None seal must survive Stage -> AR.
        assert proof['result_file_hash'] == 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'
        assert len(generated) == 2 and len(replacement_requests) == 1
        replacement = replacement_requests[0]
        assert replacement.native_session_ref is None
        assert replacement.native_replacement_context['retired_native_session_ref'] == old.native_session_ref
        assert replacement.native_replacement_context['context_pack_hash'] == request.context_pack_hash
        assert replacement.native_replacement_context['saved_work'][0]['primary_draft'] == old.primary_draft.draft
        assert 'transcript' not in replacement.native_replacement_context
        current = owner.query_idea_stage_run(request.request_ref)
        assert current.run_ref == old.run_ref and current.root_session_ref == old.root_session_ref
        assert current.attempt_ref != old.attempt_ref and current.fence_ref != old.fence_ref
        assert current.native_session_ref == '00000000-0000-4000-8000-000000000002'
        with pytest.raises(OwnerConflict):
            owner.verify_root_agent_runtime_scope(root_kind='idea', run_ref=old.run_ref,
                attempt_ref=old.attempt_ref, root_session_ref=old.root_session_ref,
                fence_ref=old.fence_ref, runtime_binding_hash=old.runtime_binding_hash)
        calls = [json.loads(line) for line in server.with_suffix('.requests').read_text().splitlines()]
        resumes = [call for call in calls if call['method'] == 'thread/resume']
        assert len(resumes) == 1 and resumes[0]['params']['threadId'] == old.native_session_ref
        assert sum(call['method'] == 'thread/start' for call in calls) == 2
        assert sum(call['method'] == 'turn/start' for call in calls) == 2
        rejected = list((tmp_path / 'native-provider').rglob('native-resume-rejected.json'))
        assert len(rejected) == 1
        _key_path, key = ensure_transport_key(tmp_path / 'native-provider')
        actual = read_transport_envelope(rejected[0], key)
        assert actual['provider_version_output'] == 'codex-cli 0.159.0'
        assert actual['error'] == {'code': -32600,
            'message': 'no rollout found for thread id ' + old.native_session_ref}
    finally:
        runtime.close()


@pytest.mark.parametrize('status', ['failed', 'interrupted'])
def test_driver_records_actual_lost_start_terminal_failure_with_the_signed_native_outcome(tmp_path, status):
    if os.name != 'posix':
        pytest.skip('The production durable provider boundary runs on Linux')
    from meta_research.idea_skill import CodexIdeaSkillAdapter, IdeaSkillUnavailable
    from test_public_idea_stage import _runtime, _confirm_direct_quest
    server = tmp_path / 'lost-terminal-codex'
    _lost_start_server(server)
    server.with_suffix('.terminal').write_text(status)
    errors = []
    class NativeIdea(CodexIdeaSkillAdapter):
        def generate_draft(self, request):
            try:
                return super().generate_draft(request)
            except IdeaSkillUnavailable as error:
                errors.append(error)
                raise
    provider = NativeIdea(tmp_path / 'native-provider', executable=str(server))
    runtime = _runtime(tmp_path / 'runtime', provider)
    runtime.configure_resident_mcp_endpoint('http://127.0.0.1:8999')
    try:
        _confirm_direct_quest(runtime)
        for _ in range(12):
            runtime.idea_stage.process_once()
            if any(error.recovery_checkpoint is not None for error in errors):
                break
        terminal = next(error for error in errors if error.recovery_checkpoint is not None)
        assert terminal.code == 'codex_operation_failed'
        proof = terminal.recovery_checkpoint
        assert proof['schema_ref'] == 'meta-research/provider-native-turn-terminal/v1'
        assert proof['native_turn_status'] == status
        assert proof['supervisor_invocation_hash'] == proof['invocation_hash']
        assert proof['native_session_ref'] == 'original-native-root'
        assert proof['native_error']['message'] == 'The original native turn ended ' + status
        calls = [json.loads(line)['method'] for line in server.with_suffix('.requests').read_text().splitlines()]
        assert calls.count('turn/start') == 1
        assert calls.count('thread/read') == 1
    finally:
        runtime.close()


@pytest.mark.parametrize('damage', ['argv', 'reconciliation'])
def test_corrupt_native_spool_becomes_a_domain_failure_without_replaying_the_original_turn(tmp_path, damage):
    if os.name != 'posix':
        pytest.skip('The production durable provider boundary runs on Linux')
    from meta_research.idea_skill import CodexIdeaSkillAdapter, IdeaSkillUnavailable
    from test_public_idea_stage import _runtime, _confirm_direct_quest
    from meta_research.owners.common import canonical_hash
    server = tmp_path / 'lost-corrupt-codex'
    _lost_start_server(server)
    errors = []
    class NativeIdea(CodexIdeaSkillAdapter):
        def generate_draft(self, request):
            try:
                return super().generate_draft(request)
            except IdeaSkillUnavailable as error:
                errors.append(error)
                if len(errors) == 1:
                    assert error.code == 'codex_operation_reconciliation_pending'
                    directory = self._workspace / 'provider-operations' / canonical_hash({'job_ref': request.job_ref}) / 'primary'
                    if damage == 'reconciliation':
                        assert reconcile_native_operation(directory) is not None
                    path = directory / ('native-provider-argv.json' if damage == 'argv' else 'native-reconciled.json')
                    damaged = json.loads(path.read_text())
                    damaged['seal'] = '0' * 64
                    path.write_text(json.dumps(damaged))
                raise
    provider = NativeIdea(tmp_path / 'native-provider', executable=str(server))
    runtime = _runtime(tmp_path / 'runtime', provider)
    runtime.configure_resident_mcp_endpoint('http://127.0.0.1:8999')
    try:
        _confirm_direct_quest(runtime)
        for _ in range(12):
            runtime.idea_stage.process_once()
            if len(errors) > 1:
                break
        assert len(errors) == 2
        assert errors[-1].code == 'codex_operation_spool_invalid'
        calls = [json.loads(line)['method'] for line in server.with_suffix('.requests').read_text().splitlines()]
        assert calls.count('turn/start') == 1
        assert calls.count('thread/read') == (1 if damage == 'reconciliation' else 0)
    finally:
        runtime.close()


def test_native_turn_still_in_progress_remains_pending_without_replaying(tmp_path):
    if os.name != 'posix':
        pytest.skip('The production durable provider boundary runs on Linux')
    server = tmp_path / 'lost-running-codex'
    _lost_start_server(server)
    server.with_suffix('.terminal').write_text('inProgress')
    _key_path, key = ensure_transport_key(tmp_path)
    directory = tmp_path / 'provider-operations' / 'aa' / ('c' * 64)
    directory.mkdir(parents=True)
    source = directory / 'native-provider-argv.json'
    write_transport_envelope(source, {'argv': [str(server), 'exec', '--json', '-']}, key)
    initial = subprocess.run([sys.executable, '-m', 'meta_research.native_codex_bridge',
        '--provider-argv', str(source), '--operation-directory', str(directory)],
        input='One exact original operation.', text=True, capture_output=True, timeout=10)
    assert initial.returncode == 75
    assert reconcile_native_operation(directory) is None
    assert not (directory / 'native-reconciled.json').exists()
    calls = [json.loads(line)['method'] for line in server.with_suffix('.requests').read_text().splitlines()]
    assert calls.count('turn/start') == 1
    assert calls.count('thread/read') == 1


@pytest.mark.parametrize('status', ['failed', 'interrupted'])
def test_target_harness_records_reconciled_native_terminal_failure(tmp_path, status):
    if os.name != 'posix':
        pytest.skip('The production durable provider boundary runs on Linux')
    from meta_research.harness import HarnessAdmissionError
    from meta_research.harness_adapters import CodexHarnessAdapter
    from test_target_root_finalizer import _current_bundle_runtime, _admit_independent_target_root
    server = tmp_path / 'lost-target-codex'
    _lost_start_server(server)
    server.with_suffix('.terminal').write_text(status)
    runtime = _current_bundle_runtime(tmp_path / 'runtime')
    runtime.configure_resident_mcp_endpoint('http://127.0.0.1:8999')
    try:
        _target, _, _, admission, _ = _admit_independent_target_root(runtime)
        # Replace only the external native-provider boundary after the fixture's
        # existing conformance setup. All HC/Harness/Owner paths remain real.
        runtime.harnesses._adapters['codex'] = CodexHarnessAdapter(
            tmp_path / 'native-target-provider', executable=str(server))
        with pytest.raises(HarnessAdmissionError, match='provider_native_turn_' + status):
            runtime.harnesses.run_or_resume_target_root(admission.run.request_ref,
                prompt='One bounded exact native Target operation.', mcp_base_url='http://127.0.0.1:8999')
        run = runtime.owners.agent_runtime.harness_runs.query_run(admission.run.request_ref)
        assert run.status == 'failed'
        calls = [json.loads(line)['method'] for line in server.with_suffix('.requests').read_text().splitlines()]
        assert calls.count('turn/start') == 1
        assert calls.count('thread/read') == 1
        proofs = list((tmp_path / 'native-target-provider').rglob('native-reconciled.json'))
        assert len(proofs) == 1
        assert json.loads(proofs[0].read_text())['payload']['turn']['status'] == status
    finally:
        runtime.close()


def test_confirmed_guidance_reaches_the_active_driver_adapter_and_native_turn(tmp_path):
    if os.name != "posix":
        pytest.skip("The production durable provider process boundary runs on Linux")
    from threading import Thread
    from meta_research.idea_skill import CodexIdeaSkillAdapter
    from test_public_idea_stage import _runtime, _confirm_direct_quest, _DeterministicIdeaSkill
    from test_guidance_scope_alignment import _confirm_guidance

    server = tmp_path / "codex"
    server.write_text("#!" + sys.executable + "\n" + _SERVER);server.chmod(0o700)
    observed = []

    class NativeIdea(CodexIdeaSkillAdapter):
        def generate_draft(self, request):
            candidate = _DeterministicIdeaSkill().generate_draft(request)
            server.with_suffix('.payload').write_text(json.dumps({"outcome": candidate.draft}))
            result = super().generate_draft(request)
            observed.append(result)
            return result

    provider = NativeIdea(tmp_path / "native-provider", executable=str(server))
    runtime = _runtime(tmp_path / "runtime", provider)
    runtime.configure_resident_mcp_endpoint("http://127.0.0.1:8999")
    def work():
        for _ in range(12):
            runtime.idea_stage.process_once()
            if observed:
                return
    worker = Thread(target=work)
    try:
        quest = _confirm_direct_quest(runtime)["quest_ref"]
        worker.start()
        requests = server.with_suffix('.requests')
        deadline = time.monotonic() + 20
        while not requests.exists() or '"method": "turn/start"' not in requests.read_text():
            assert worker.is_alive(), runtime.idea_stage.transient_error
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert worker.is_alive()
        confirmed = _confirm_guidance(runtime, quest, {"kind": "quest", "quest_ref": quest},
            key="actual-live-turn", text="Preserve the calibration negative result and every missing observation.")
        worker.join(timeout=20)
        assert not worker.is_alive()
        assert len(observed) == 1, runtime.idea_stage.transient_error
        calls = [json.loads(line) for line in requests.read_text().splitlines()]
        steers = [item['params'] for item in calls if item['method'] == 'turn/steer']
        assert len(steers) == 1
        assert steers[0]['threadId'] == 'native-root'
        message = steers[0]['input'][0]['text']
        assert confirmed['guidance']['text'] in message
        assert confirmed['constraint_ref'] in message
        deliveries = runtime.owners.human_collaboration.query_guidance_deliveries(confirmed['constraint_ref'])
        assert len(deliveries) == 1
        assert deliveries[0]['operation_ref'].startswith('guidance_update_')
    finally:
        if worker.is_alive():
            provider.request_stop();worker.join(timeout=10)
        runtime.close()
