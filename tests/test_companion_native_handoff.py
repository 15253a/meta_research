"""Host-direct creation process -> accepted Quest -> ordinary native resume (plus legacy jail handoff guard)."""
from dataclasses import replace
from pathlib import Path
import hashlib
import json

import pytest

from meta_research.composition import build_production_runtime
from meta_research.owners.common import OwnerConflict, canonical_hash
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
    history.write_text(json.dumps({'type':'session_meta','timestamp':'2026-11-12T01:00:00Z','payload':{'id':native,'cwd':os.getcwd()}}) + '\\n')
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

    adapter = RequestObserver(
        tmp_path / 'companion',
        executable=str(executable),
        process_runner=_CancellableProcessRunner(protected_environment=data_root.codex_environment),
    )
    adapter.turn_requests = []
    runtime = build_production_runtime(
        data_root,
        proposal_drafter=adapter,
        intent_drafting_provider=adapter,
        host_compute_probe=_DeterministicProbe(),
    )
    runtime.configure_resident_mcp_endpoint('http://testserver')
    client = TestClient(create_app(runtime, base_url='http://testserver', control_key='control'))
    try:
        human = runtime.owners.human_collaboration
        source = tmp_path / 'original.txt'
        source.write_text('Original retained research material.')
        anchor, _, _ = _open(client, runtime, source, 'native-custody-open')
        initial = human.observe_host_compute(anchor.ref, ['GPU-plan-test'], 'native-custody-probe')
        draft = _draft_from(initial)
        draft['route'] = 'direct'
        saved = human.revise_quest_draft(
            anchor.ref,
            draft,
            initial['quest_draft']['hash'],
            'native-custody-draft',
            expected_draft_revision=initial['quest_draft']['revision'],
        )
        human.generate_question_proposal(
            anchor.ref,
            saved['quest_draft']['hash'],
            'native-custody-proposal',
            expected_draft_revision=saved['quest_draft']['revision'],
        )
        assert human.process_drafting_once()
        ready = human.query_quest_creation(anchor.ref)
        assert ready['proposal'] is not None, ready
        preview = human.preview_confirmation(
            anchor.ref,
            quest_draft_revision=ready['quest_draft']['revision'],
            quest_draft_hash=ready['quest_draft']['hash'],
            proposal_ref=ready['proposal']['ref'],
            proposal_hash=ready['proposal']['hash'],
            idempotency_key='native-custody-preview',
        )
        human.confirm_quest(anchor.ref, **_confirmation_payload(preview), idempotency_key='native-custody-confirm')
        for _ in range(16):
            if human.query_quest_creation(anchor.ref)['status'] == 'completed':
                break
            assert human.reconcile_once()
        accepted = human.query_quest_creation(anchor.ref)
        assert accepted['status'] == 'completed'
        # Host-direct creation writes its rollout directly into data_root.codex_home.
        assert adapter.native_calls[-1][1].runtime is None
        rollout = data_root.codex_home / RELATIVE
        assert rollout.is_file()
        (data_root.codex_home / 'auth.json').write_text('{"account":"current-account"}')
        yield runtime, adapter, data_root, accepted, rollout
    finally:
        client.close()
        runtime.close()


def test_accepted_quest_companion_resumes_exact_creation_native_in_current_account_home(accepted_creation):
    runtime, adapter, data_root, accepted, rollout = accepted_creation
    human = runtime.owners.human_collaboration
    scope = 'quest:' + accepted['quest_ref']
    before = human.query_companion(scope)
    assert before['native_session_ref'] == NATIVE
    original_bytes = rollout.read_bytes()
    assert b'creation-history-fact' in original_bytes

    human.send_companion_message(scope, 'Read the original preparation conversation.', 'native-custody-first-message')
    assert human.process_drafting_once()
    after = human.query_companion(scope)
    assert after['turns'][-1]['assistant_status'] == 'completed', after['turns'][-1]
    assert after['session_ref'] == before['session_ref'] and after['native_session_ref'] == NATIVE
    assert 'creation-history-fact' in after['turns'][-1]['assistant_content']
    assert rollout.read_bytes().startswith(original_bytes)
    first_continuation = rollout.read_bytes()

    human.send_companion_message(scope, 'Continue the same conversation.', 'native-custody-second-message')
    assert human.process_drafting_once()
    assert human.query_companion(scope)['turns'][-1]['assistant_status'] == 'completed'
    assert rollout.read_bytes().startswith(first_continuation)
    assert json.loads((data_root.codex_home / 'auth.json').read_text()) == {'account': 'current-account'}
    receipts = list((adapter._workspace / 'provider-operations').glob('*/companion-turn/supervisor-exit.json'))
    assert len(receipts) == 2
    assert all(json.loads(path.read_text())['payload']['process_tree']['state'] == 'closed' for path in receipts)
    assert all(json.loads(path.read_text())['payload']['returncode'] == 0 for path in receipts)


def test_native_handoff_rejects_foreign_scope_and_is_noop_for_host_direct(accepted_creation):
    from meta_research.companion_native_handoff import ensure_companion_native_handoff
    from meta_research.semantic_mcp import SemanticMcpError

    runtime, adapter, data_root, accepted, rollout = accepted_creation
    human = runtime.owners.human_collaboration
    scope = 'quest:' + accepted['quest_ref']
    root = human.query_companion(scope)['session_ref']
    binding = runtime.root_workspaces.bind_companion_session(scope, root)
    # Valid host-direct scope is a clean no-op (rollout is already in codex_home).
    assert ensure_companion_native_handoff(adapter, binding, NATIVE) is None
    # Foreign scope is still rejected before launch.
    foreign_binding = replace(binding, location=replace(binding.location, quest_ref='quest:foreign'))
    with pytest.raises((OwnerConflict, SemanticMcpError)):
        ensure_companion_native_handoff(adapter, foreign_binding, NATIVE)


def test_legacy_protected_jail_handoff_guard_still_streams_and_rejects_collision(accepted_creation):
    from meta_research.companion_native_handoff import ensure_companion_native_handoff
    from meta_research.protected_creation_runtime import ProtectedCreationError
    from meta_research.semantic_mcp import SemanticMcpError

    runtime, adapter, data_root, accepted, rollout = accepted_creation
    human = runtime.owners.human_collaboration
    scope = 'quest:' + accepted['quest_ref']
    root = human.query_companion(scope)['session_ref']
    binding = runtime.root_workspaces.bind_companion_session(scope, root)

    # Simulate a legacy `.protected/<identity>` jail on disk (from pre-host-direct data)
    # and verify missing process seal / collision rejection still protects codex_home.
    prep_session = accepted['intent_session']['ref']
    prepared = runtime.root_workspaces._initialization_location(accepted['initialization_id'], prep_session)
    identity = canonical_hash({'workspace_ref': prepared.workspace_ref, 'root_session_ref': prep_session})
    runtime.root_workspaces.configure_creation_runtime(
        executable=adapter._executable,
        credentials_home=data_root.codex_home,
    )
    legacy_root = runtime.root_workspaces._bases['companion'] / '.protected' / identity
    legacy_codex = legacy_root / 'home' / 'creation' / '.codex'
    legacy_file = legacy_codex / RELATIVE
    legacy_file.parent.mkdir(parents=True, exist_ok=True)
    legacy_file.write_bytes(b'legacy-jail-rollout-bytes\n')
    control_dir = legacy_root.parent / f'.{identity}-control'
    control_dir.mkdir(parents=True, exist_ok=True)
    control_dir.chmod(0o700)
    (control_dir / 'active.json').write_text(json.dumps({'receipt': 'missing-receipt.json'}))
    with pytest.raises((OwnerConflict, ProtectedCreationError, SemanticMcpError)):
        ensure_companion_native_handoff(adapter, binding, NATIVE)
