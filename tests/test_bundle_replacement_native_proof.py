"""Prove rolling replacement identity across the real durable provider boundary.

The external fixture speaks app-server RPC and returns a malformed final message.
It does not replace the production bridge, process runner, supervisor or seal
verification, and it makes no model or external MCP call.
"""
from dataclasses import replace
import json
import os
from pathlib import Path
import sys

import pytest

from meta_research.bundle_skill import CodexBundleSkillAdapter
from meta_research.idea_skill import CodexIdeaSkillAdapter, IdeaSkillUnavailable
from meta_research.human_guidance import FrozenGuidanceBinding, FrozenGuidanceCut
from meta_research.owners.common import canonical_hash
from meta_research.provider_supervisor import (
    read_transport_envelope, read_transport_key_for_operation,
)
from test_bundle_input_drift_recovery import _request
from test_bundle_skill_adapter import _FullConformanceAuthority
from test_idea_skill_contract import _request as _idea_request


pytestmark = pytest.mark.skipif(os.name != "posix", reason="Real durable provider processes require Linux")
_NATIVE = "replacement-native-proof"
_ORIGINAL_FIELDS = {
    "schema_ref", "termination_reason", "invocation_hash", "prompt_hash",
    "output_schema_hash", "stdout_hash", "result_file_hash", "supervisor_receipt_hash",
    "contract_failure_code", "contract_failure_detail_code",
}
_SERVER = r'''
import json,sys
from pathlib import Path
if '--version' in sys.argv:
 print('codex-cli 0.159.0');sys.exit(0)
if sys.argv[-2:]==['features','list']:
 print('multi_agent stable true');sys.exit(0)
if 'exec' in sys.argv:
 sys.stdin.read()
 Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text('{malformed final JSON')
 print(json.dumps({'type':'thread.started','thread_id':'replacement-native-proof'}),flush=True)
 print(json.dumps({'type':'turn.completed'}),flush=True)
 sys.exit(0)
requests=Path(__file__).with_suffix('.requests')
state=Path(__file__).with_suffix('.state')
terminal=Path(__file__).with_suffix('.terminal')
def send(value):print(json.dumps(value),flush=True)
for line in sys.stdin:
 message=json.loads(line)
 with requests.open('a') as stream:stream.write(json.dumps(message)+'\n')
 method=message.get('method');params=message.get('params',{})
 if method=='initialized':continue
 result={}
 if method in ('thread/start','thread/resume'):
  result={'thread':{'id':'replacement-native-proof'}}
 elif method=='turn/start':
  if terminal.exists():
   state.write_text(json.dumps({'id':'replacement-turn','status':terminal.read_text(),
    'error':{'message':'The original rolling turn ended '+terminal.read_text(),'codexErrorInfo':'other'},
    'items':[{'id':'original-input','type':'userMessage','clientId':params['clientUserMessageId'],
     'content':params['input']}]}))
   sys.exit(0)
  result={'turn':{'id':'replacement-turn','status':'inProgress'}}
 elif method=='thread/read':
  result={'thread':{'id':'replacement-native-proof','turns':[json.loads(state.read_text())]}}
 send({'id':message['id'],'result':result})
 if method=='turn/start':
  send({'method':'item/completed','params':{'threadId':'replacement-native-proof',
   'turnId':'replacement-turn','item':{'type':'agentMessage','id':'final','text':'{malformed final JSON'}}})
  send({'method':'turn/completed','params':{'threadId':'replacement-native-proof',
   'turn':{'id':'replacement-turn','status':'completed'}}})
'''


def _server(tmp_path: Path) -> Path:
    executable = tmp_path / "codex"
    executable.write_text("#!" + sys.executable + "\n" + _SERVER, encoding="utf-8")
    executable.chmod(0o700)
    return executable


def _replacement_context(request):
    frozen = {"cycle_ref": request.cycle_ref, "cycle_position": request.cycle_position}
    basis = {
        "kind": "native_session_replacement", "stage_request_ref": request.stage_request_ref,
        "safe_point_ref": "safe-point:isolated", "retired_attempt_ref": "attempt:retired",
        "retired_native_session_ref": "native:retired", "provider_operation_ref": "operation:retired",
        "context_pack_hash": canonical_hash(frozen),
    }
    return {**basis, "isolation_basis": basis, "frozen_stage_input": frozen,
            "accepted_target_graph": {"graph_ref": request.graph_ref, "targets": [], "target_commits": []}}


class _EmptyGuidanceAuthority:
    """An empty configured cut selects native transport; no HC authority is tested."""

    def freeze_operation_guidance(self, operation):
        return FrozenGuidanceCut(FrozenGuidanceBinding(
            operation.identity, "quest:transport-fixture", "snapshot:empty", canonical_hash([])),
            "provenance:transport-fixture", ())

    def prepare_live_guidance(self, **scope):
        return ()


class _GuidanceConformance(_FullConformanceAuthority):
    def issue_resident_mcp_channel(self, *, guidance_binding=None, **scope):
        channel = super().issue_resident_mcp_channel(**scope)
        return replace(channel, binding=replace(channel.binding, guidance_binding=guidance_binding))


def _bundle(tmp_path, kind):
    executable = _server(tmp_path)
    workspace = tmp_path / "provider"
    adapter = CodexBundleSkillAdapter(workspace, executable=str(executable), timeout_seconds=12)
    adapter.bind_full_conformance_authority(_GuidanceConformance())
    adapter.bind_human_guidance_authority(_EmptyGuidanceAuthority())
    adapter.configure_resident_mcp_endpoint("http://127.0.0.1:8767")
    request = _request(adapter, kind)
    request = replace(request, native_session_ref=None, native_replacement_context=_replacement_context(request))
    return adapter, request, executable, workspace


def _invoke(adapter, request, kind):
    return (adapter.schedule_target if kind == "dispatch" else adapter.propose_target_batch)(request)


def _calls(executable):
    return [json.loads(line) for line in executable.with_suffix(".requests").read_text().splitlines()]


def _operation(workspace, kind):
    pattern = "dispatch-*" if kind == "dispatch" else "target-batch-*"
    return next((workspace / "provider-operations").glob("*/" + pattern))


@pytest.mark.parametrize("kind", ["dispatch", "batch"])
def test_malformed_replacement_result_retains_verified_native_without_completed_json(tmp_path, kind):
    adapter, request, executable, workspace = _bundle(tmp_path, kind)
    with pytest.raises(IdeaSkillUnavailable) as failure:
        _invoke(adapter, request, kind)
    assert failure.value.code == "bundle_review_result_contract_invalid"
    proof = failure.value.recovery_checkpoint
    assert proof is not None
    assert proof["schema_ref"] == "meta-research/provider-native-contract-failure/v1"
    assert set(proof) == _ORIGINAL_FIELDS | {"native_session_ref"}
    assert proof["native_session_ref"] == _NATIVE
    assert proof["contract_failure_detail_code"] == "codex_output_invalid"
    operation = _operation(workspace, kind)
    assert not (operation / "completed.json").exists()
    assert (operation / "last-message.json").read_text() == "{malformed final JSON"
    _key_path, key = read_transport_key_for_operation(operation)
    invocation = read_transport_envelope(operation / "invocation.json", key)
    receipt = read_transport_envelope(operation / "supervisor-exit.json", key)
    assert invocation["transport_mode"] == "durable_supervisor"
    assert invocation["native_session_ref"] is None
    assert receipt["returncode"] == 0 and receipt["termination_reason"] == "completed"
    assert proof["invocation_hash"] == receipt["invocation_hash"] == canonical_hash(invocation)
    assert proof["stdout_hash"] == canonical_hash((operation / "stdout.jsonl").read_text())
    original = {p.name: p.read_bytes() for p in operation.iterdir() if p.is_file()}
    with pytest.raises(IdeaSkillUnavailable) as recovered:
        _invoke(adapter, request, kind)
    assert recovered.value.recovery_checkpoint == proof
    assert {name: (operation / name).read_bytes() for name in original} == original
    calls = _calls(executable)
    assert [item["method"] for item in calls].count("thread/start") == 1
    assert [item["method"] for item in calls].count("turn/start") == 1
    assert not any(item["method"] in {"thread/read", "thread/resume"} for item in calls)


@pytest.mark.parametrize("kind", ["dispatch", "batch"])
def test_changed_terminal_stdout_cannot_recreate_native_replacement_proof(tmp_path, kind):
    adapter, request, executable, workspace = _bundle(tmp_path, kind)
    with pytest.raises(IdeaSkillUnavailable) as first:
        _invoke(adapter, request, kind)
    assert first.value.recovery_checkpoint["native_session_ref"] == _NATIVE
    operation = _operation(workspace, kind)
    stdout = operation / "stdout.jsonl"
    stdout.write_text(stdout.read_text().replace(_NATIVE, "foreign-native-proof"), encoding="utf-8")
    changed_files = {p.name: p.read_bytes() for p in operation.iterdir() if p.is_file()}
    calls = _calls(executable)

    with pytest.raises(IdeaSkillUnavailable, match="^codex_operation_spool_invalid$") as refused:
        _invoke(adapter, request, kind)

    assert refused.value.recovery_checkpoint is None
    assert _calls(executable) == calls
    assert {name: (operation / name).read_bytes() for name in changed_files} == changed_files
    assert not (operation / "completed.json").exists()


@pytest.mark.parametrize("kind", ["dispatch", "batch"])
def test_foreign_native_resume_response_never_attests_a_replacement_identity(tmp_path, kind):
    adapter, request, executable, workspace = _bundle(tmp_path, kind)
    request = replace(request, native_session_ref="expected-original-native", native_replacement_context=None)

    with pytest.raises(IdeaSkillUnavailable) as refused:
        _invoke(adapter, request, kind)

    assert refused.value.code == "codex_operation_failed"
    assert refused.value.native_session_ref is None
    assert refused.value.recovery_checkpoint["schema_ref"] == "meta-research/provider-hard-ceiling/v1"
    assert "native_session_ref" not in refused.value.recovery_checkpoint
    operation = _operation(workspace, kind)
    assert not (operation / "completed.json").exists()
    events = [json.loads(line) for line in (operation / "stdout.jsonl").read_text().splitlines()]
    assert not any(item.get("type") == "thread.started" for item in events)
    calls = _calls(executable)
    resume, = [item for item in calls if item["method"] == "thread/resume"]
    assert resume["params"]["threadId"] == "expected-original-native"
    assert not any(item["method"] == "turn/start" for item in calls)


def test_default_idea_terminal_contract_proof_keeps_its_original_ten_fields(tmp_path):
    # This unscoped Idea adapter uses the fixture's explicit exec-wire branch.
    # Its actual durable supervisor and signed stdout still verify the native;
    # the new Bundle rolling attestation must not alter the shared default.
    workspace = tmp_path / "idea-provider"
    adapter = CodexIdeaSkillAdapter(workspace, executable=str(_server(tmp_path)), timeout_seconds=12)
    request = _idea_request(runtime_binding=adapter.runtime_binding(), job_ref="idea-primary:malformed-proof")

    with pytest.raises(IdeaSkillUnavailable) as failed:
        adapter.generate_draft(request)

    assert failed.value.code == "idea_primary_result_contract_invalid"
    proof = failed.value.recovery_checkpoint
    assert set(proof) == _ORIGINAL_FIELDS
    assert proof["schema_ref"] == "meta-research/provider-terminal-contract-failure/v1"
    assert proof["contract_failure_detail_code"] == "codex_output_invalid"
    assert failed.value.rejected_native_session_ref == _NATIVE
    operation, = workspace.glob("provider-operations/*/primary")
    assert not (operation / "completed.json").exists()
    _key_path, key = read_transport_key_for_operation(operation)
    invocation = read_transport_envelope(operation / "invocation.json", key)
    receipt = read_transport_envelope(operation / "supervisor-exit.json", key)
    assert invocation["transport_mode"] == "durable_supervisor"
    assert receipt["returncode"] == 0 and receipt["input_complete"] is True
    assert proof["invocation_hash"] == receipt["invocation_hash"] == canonical_hash(invocation)


@pytest.mark.parametrize("status", ["failed", "interrupted"])
def test_unknown_initial_rolling_turn_reconciles_actual_terminal_native_and_original_seal(tmp_path, status):
    adapter, request, executable, workspace = _bundle(tmp_path, "dispatch")
    executable.with_suffix(".terminal").write_text(status, encoding="utf-8")
    with pytest.raises(IdeaSkillUnavailable, match="^codex_operation_reconciliation_pending$") as unknown:
        adapter.schedule_target(request)
    assert unknown.value.recovery_checkpoint is None
    operation = _operation(workspace, "dispatch")
    original = {p.name: p.read_bytes() for p in operation.iterdir() if p.is_file()}
    assert not (operation / "completed.json").exists()
    _key_path, key = read_transport_key_for_operation(operation)
    invocation = read_transport_envelope(operation / "invocation.json", key)
    receipt = read_transport_envelope(operation / "supervisor-exit.json", key)
    assert invocation["native_session_ref"] is None
    assert receipt["returncode"] == 75 and receipt["termination_reason"] == "completed"

    with pytest.raises(IdeaSkillUnavailable, match="^codex_operation_failed$") as reconciled:
        adapter.schedule_target(request)

    proof = reconciled.value.recovery_checkpoint
    assert proof["schema_ref"] == "meta-research/provider-native-turn-terminal/v1"
    assert proof["native_session_ref"] == reconciled.value.native_session_ref == _NATIVE
    assert proof["native_turn_status"] == status and proof["native_turn_ref"] == "replacement-turn"
    assert proof["native_error"]["message"] == "The original rolling turn ended " + status
    assert proof["invocation_hash"] == proof["supervisor_invocation_hash"] == canonical_hash(invocation)
    assert proof["termination_reason"] == receipt["termination_reason"]
    assert proof["prompt_hash"] == invocation["prompt_hash"]
    assert proof["output_schema_hash"] == invocation["output_schema_hash"]
    assert proof["stdout_hash"] == canonical_hash((operation / "stdout.jsonl").read_text())
    assert proof["result_file_hash"] == receipt["result_file_hash"] == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
    assert proof["supervisor_receipt_hash"] == canonical_hash(json.loads(original["supervisor-exit.json"]))
    assert proof["native_start_hash"] == canonical_hash(read_transport_envelope(operation / "native-turn-start.json", key))
    assert proof["native_reconciliation_hash"] == canonical_hash(read_transport_envelope(operation / "native-reconciled.json", key))
    assert {name: (operation / name).read_bytes() for name in original} == original
    assert not (operation / "completed.json").exists()
    with pytest.raises(IdeaSkillUnavailable, match="^codex_operation_failed$") as repeated:
        adapter.schedule_target(request)
    assert repeated.value.recovery_checkpoint == proof
    methods = [item["method"] for item in _calls(executable)]
    assert methods.count("thread/start") == methods.count("turn/start") == methods.count("thread/read") == 1
    assert "thread/resume" not in methods
