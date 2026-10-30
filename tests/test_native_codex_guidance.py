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
 if method in ('thread/start','thread/resume'):result={'thread':{'id':'original-native-root'}}
 if method=='turn/start':
  payload=Path(__file__).with_suffix('.payload')
  final=payload.read_text() if payload.exists() else '{"ok":true}'
  state.write_text(json.dumps({'id':'original-turn','status':'completed','items':[
   {'id':'input','type':'userMessage','clientId':params['clientUserMessageId'],'content':params['input']},
   {'id':'output','type':'agentMessage','text':final}]}))
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
    assert calls.count("thread/resume") == 1  # Load native history, no business turn.


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
        assert calls.count("thread/resume") == 1
        operations = list((tmp_path / "native-provider" / "provider-operations").glob("*/*/native-reconciled.json"))
        assert len(operations) == 1
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
