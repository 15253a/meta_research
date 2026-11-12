"""Actual protected creation process -> accepted Quest -> ordinary native resume."""
from pathlib import Path
import hashlib
import json

import pytest

from meta_research.composition import build_production_runtime
from meta_research.paths import prepare_data_root
from meta_research.quest_drafting import _CancellableProcessRunner
from native_creation_provider import ObservedCompanion, native_creation_executable
from test_public_plan_stage import _DeterministicProbe
from test_public_quest_initialization import _draft_from, _confirmation_payload


NATIVE = '18e82a10-38e2-4ea3-b92f-9cf460123abc'
RELATIVE = 'sessions/2026/11/12/rollout-2026-11-12T01-00-00-' + NATIVE + '.jsonl'


def _persistent_program(code):
    continuity = '''prompt = sys.stdin.read()
native = NATIVE
history = Path(os.environ['CODEX_HOME']) / RELATIVE
if 'Protected creation binding and explicit guidance file locations:' not in prompt:
    assert 'resume' in sys.argv and native in sys.argv
    original = history.read_bytes()
    assert b'creation-history-fact' in original
    assert json.loads((Path(os.environ['CODEX_HOME']) / 'auth.json').read_text())['account'] == 'current-account'
    with history.open('a') as stream:
        stream.write(json.dumps({'type':'response_item','payload':{'role':'assistant','text':'continued-history-fact'}}) + '\\n')
    Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(json.dumps({'reply':'Read creation-history-fact in the original native session.','agent_proposal':None}))
    print(json.dumps({'type':'thread.started','thread_id':native}))
    print(json.dumps({'type':'turn.completed'}))
    raise SystemExit(0)
history.parent.mkdir(parents=True, exist_ok=True)
if not history.exists():
    history.write_text(json.dumps({'type':'session_meta','timestamp':'2026-11-12T01:00:00Z','payload':{'id':native,'cwd':'/workspace'}}) + '\\n')
with history.open('a') as stream:
    stream.write(json.dumps({'type':'response_item','payload':{'role':'assistant','text':'creation-history-fact'}}) + '\\n')
'''.replace('NATIVE', repr(NATIVE)).replace('RELATIVE', repr(RELATIVE))
    return code.replace('prompt = sys.stdin.read()\n', continuity).replace("'local-native-root'", repr(NATIVE))


@pytest.fixture
def accepted_creation(tmp_path):
    from fastapi.testclient import TestClient
    from meta_research.web import create_app
    from test_creation_material_consumption import _open

    data_root = prepare_data_root(tmp_path / 'runtime')
    (data_root.codex_home / 'auth.json').write_text('{"account":"creation-account"}')
    executable = native_creation_executable(tmp_path / 'native-companion', program_transform=_persistent_program)
    class RequestObserver(ObservedCompanion):
        def reply(self, request):
            self.turn_requests.append(request)
            return super().reply(request)

    adapter = RequestObserver(tmp_path / 'companion', executable=str(executable),
        process_runner=_CancellableProcessRunner(protected_environment=data_root.codex_environment))
    adapter.turn_requests = []
    runtime = build_production_runtime(data_root, proposal_drafter=adapter, intent_drafting_provider=adapter,
        host_compute_probe=_DeterministicProbe())
    runtime.configure_resident_mcp_endpoint('http://testserver')
    runtime.root_workspaces.configure_creation_runtime(executable=executable, credentials_home=data_root.codex_home)
    client = TestClient(create_app(runtime, base_url='http://testserver', control_key='control'))
    try:
        human = runtime.owners.human_collaboration
        source = tmp_path / 'original.txt'
        source.write_text('Original retained research material.')
        anchor, _, _ = _open(client, runtime, source, 'native-custody-open')
        initial = human.observe_host_compute(anchor.ref, ['GPU-plan-test'], 'native-custody-probe')
        draft = _draft_from(initial)
        draft['route'] = 'direct'
        saved = human.revise_quest_draft(anchor.ref, draft, initial['quest_draft']['hash'],
            'native-custody-draft', expected_draft_revision=initial['quest_draft']['revision'])
        human.generate_question_proposal(anchor.ref, saved['quest_draft']['hash'],
            'native-custody-proposal', expected_draft_revision=saved['quest_draft']['revision'])
        assert human.process_drafting_once()
        ready = human.query_quest_creation(anchor.ref)
        assert ready['proposal'] is not None, ready
        preview = human.preview_confirmation(anchor.ref,
            quest_draft_revision=ready['quest_draft']['revision'], quest_draft_hash=ready['quest_draft']['hash'],
            proposal_ref=ready['proposal']['ref'], proposal_hash=ready['proposal']['hash'],
            idempotency_key='native-custody-preview')
        human.confirm_quest(anchor.ref, **_confirmation_payload(preview), idempotency_key='native-custody-confirm')
        for _ in range(16):
            if human.query_quest_creation(anchor.ref)['status'] == 'completed':
                break
            assert human.reconcile_once()
        accepted = human.query_quest_creation(anchor.ref)
        assert accepted['status'] == 'completed'
        protected = adapter.native_calls[-1][1].runtime
        original = protected.jail_root / 'home/creation/.codex' / RELATIVE
        assert original.is_file()
        (data_root.codex_home / 'auth.json').write_text('{"account":"current-account"}')
        yield runtime, adapter, data_root, accepted, protected, original
    finally:
        client.close()
        runtime.close()


def test_accepted_quest_companion_resumes_exact_creation_native_in_current_account_home(accepted_creation, monkeypatch):
    import meta_research.companion_native_handoff as custody
    from meta_research.owners.common import canonical_hash
    runtime, adapter, data_root, accepted, protected, original = accepted_creation
    human = runtime.owners.human_collaboration
    scope = 'quest:' + accepted['quest_ref']
    before = human.query_companion(scope)
    assert before['native_session_ref'] == NATIVE
    original_bytes = original.read_bytes()
    original_auth = (protected.jail_root / 'home/creation/.codex/auth.json').read_bytes()
    target = data_root.codex_home / RELATIVE
    assert not target.exists()
    # Reproduce the previous ordinary-home failure with a real provider. The
    # subsequent legal message must not rewrite or retry this failed operation.
    with monkeypatch.context() as patch:
        patch.setattr(custody, 'ensure_companion_native_handoff', lambda *args: None)
        human.send_companion_message(scope, 'Read the creation conversation.', 'native-custody-old-failed-message')
        assert human.process_drafting_once()
    failed = human.query_companion(scope)['turns'][-1]
    assert failed['assistant_status'] == 'failed'
    failed_directory = adapter._workspace / 'provider-operations' / canonical_hash({'job_ref': adapter.turn_requests[-1].job_ref})
    failed_files = {str(path.relative_to(failed_directory)): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in failed_directory.rglob('*') if path.is_file()}
    assert failed_files and not target.exists()
    human.send_companion_message(scope, 'Read the original preparation conversation.', 'native-custody-first-message')
    assert human.process_drafting_once()
    after = human.query_companion(scope)
    assert after['turns'][-1]['assistant_status'] == 'completed', after['turns'][-1]
    assert after['session_ref'] == before['session_ref'] and after['native_session_ref'] == NATIVE
    assert 'creation-history-fact' in after['turns'][-1]['assistant_content']
    assert target.read_bytes().startswith(original_bytes)
    first_continuation = target.read_bytes()
    with monkeypatch.context() as patch:
        def no_history_scan(*args):
            raise AssertionError('A subsequent native turn must use its exact custody path.')
        patch.setattr(custody, '_matching_rollouts', no_history_scan)
        human.send_companion_message(scope, 'Continue the same conversation.', 'native-custody-second-message')
        assert human.process_drafting_once()
    assert human.query_companion(scope)['turns'][-1]['assistant_status'] == 'completed'
    assert target.read_bytes().startswith(first_continuation)
    assert original.read_bytes() == original_bytes
    assert (protected.jail_root / 'home/creation/.codex/auth.json').read_bytes() == original_auth
    assert json.loads((data_root.codex_home / 'auth.json').read_text()) == {'account':'current-account'}
    assert not (data_root.codex_home / 'outputs').exists()
    receipts = list((adapter._workspace / 'provider-operations').glob('*/companion-turn/supervisor-exit.json'))
    assert len(receipts) == 3
    assert all(json.loads(path.read_text())['payload']['process_tree']['state'] == 'closed' for path in receipts)
    assert sorted(json.loads(path.read_text())['payload']['returncode'] for path in receipts) == [0, 0, 1]
    assert human.query_companion(scope)['turns'][0]['interaction_ref'] == failed['interaction_ref']
    assert human.query_companion(scope)['turns'][0]['assistant_status'] == 'failed'
    assert {str(path.relative_to(failed_directory)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in failed_directory.rglob('*') if path.is_file()} == failed_files


@pytest.mark.parametrize('kind', ['collision', 'symlink', 'missing_process_seal', 'foreign_scope'])
def test_native_handoff_rejects_unsafe_or_unproven_custody_before_provider_launch(accepted_creation, kind):
    from dataclasses import replace
    from meta_research.companion_native_handoff import ensure_companion_native_handoff
    from meta_research.owners.common import OwnerConflict
    from meta_research.protected_creation_runtime import ProtectedCreationError
    from meta_research.semantic_mcp import SemanticMcpError

    runtime, adapter, data_root, accepted, protected, original = accepted_creation
    human = runtime.owners.human_collaboration
    scope = 'quest:' + accepted['quest_ref']
    root = human.query_companion(scope)['session_ref']
    binding = runtime.root_workspaces.bind_companion_session(scope, root)
    target = data_root.codex_home / RELATIVE
    source_bytes = original.read_bytes()
    active = None
    saved_active = None
    if kind in {'collision', 'symlink'}:
        target.parent.mkdir(parents=True, exist_ok=True)
        if kind == 'collision':
            target.write_bytes(b'Unrelated history must not be overwritten.\n')
        else:
            target.symlink_to(original)
    elif kind == 'missing_process_seal':
        active = protected._control / 'active.json'
        saved_active = active.read_bytes()
        value = json.loads(saved_active)
        value['receipt'] = 'missing-original-process-receipt.json'
        active.write_text(json.dumps(value))
    else:
        binding = replace(binding, location=replace(binding.location, quest_ref='quest:foreign'))
    try:
        with pytest.raises((OwnerConflict, ProtectedCreationError, SemanticMcpError)):
            ensure_companion_native_handoff(adapter, binding, NATIVE)
        assert original.read_bytes() == source_bytes
        assert not list((adapter._workspace / 'provider-operations').glob('*/companion-turn/invocation.json'))
        if kind == 'collision':
            assert target.read_bytes() == b'Unrelated history must not be overwritten.\n'
        elif kind == 'symlink':
            assert target.is_symlink()
        else:
            assert not target.exists()
    finally:
        if active is not None:
            active.write_bytes(saved_active)


def test_native_custody_streams_history_larger_than_material_reader_limit(accepted_creation):
    from meta_research.companion_native_handoff import ensure_companion_native_handoff
    runtime, adapter, data_root, accepted, protected, original = accepted_creation
    # A valid long historical record; this is fixture data, not a model result.
    with original.open('ab') as stream:
        stream.write(b'{"type":"response_item","payload":{"role":"assistant","text":"')
        for _ in range(65):
            stream.write(b'x' * (1024 * 1024))
        stream.write(b'"}}\n')
    scope = 'quest:' + accepted['quest_ref']
    root = runtime.owners.human_collaboration.query_companion(scope)['session_ref']
    binding = runtime.root_workspaces.bind_companion_session(scope, root)
    ensure_companion_native_handoff(adapter, binding, NATIVE)
    target = data_root.codex_home / RELATIVE
    assert target.stat().st_size == original.stat().st_size > 64 * 1024 * 1024
    with original.open('rb') as left, target.open('rb') as right:
        assert hashlib.file_digest(left, 'sha256').digest() == hashlib.file_digest(right, 'sha256').digest()
