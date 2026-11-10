import json
import os
from pathlib import Path
import sys

import pytest

from meta_research.acquisition_root import _AcquisitionCodexAdapter
from meta_research.idea_skill import IdeaSkillUnavailable
from meta_research.provider_supervisor import ensure_transport_key, read_transport_envelope
from meta_research.quest_drafting import _CancellableProcessRunner


NATIVE = '00000000-0000-4000-8000-000000000099'


def _invoke(tmp_path, executable, *, timeout=10):
    home = tmp_path / 'home'
    home.mkdir()
    adapter = _AcquisitionCodexAdapter(tmp_path / 'provider', executable=str(executable),
        timeout_seconds=timeout,
        process_runner=_CancellableProcessRunner(protected_environment={'CODEX_HOME': str(home)}))
    return adapter._invoke_optional_root_task_operation(operation_name='acquisition-root-turn',
        prompt='Do not use tools. Return {"ok":true}.',
        schema={'type': 'object', 'properties': {'ok': {'type': 'boolean'}},
                'required': ['ok'], 'additionalProperties': False},
        native_session_ref=NATIVE, job_ref='strict-exec-check', root_runtime_scope=None,
        execution_access_mode='provided_only', execution_write_root=tmp_path)


def test_locked_vendor_missing_native_has_signed_strict_exec_proof(tmp_path, monkeypatch):
    if os.name != 'posix':
        pytest.skip('The real durable provider boundary runs on Linux')
    installed = os.environ.get('META_RESEARCH_PROVIDER_TOOLS')
    if not installed:
        pytest.skip('Requires the already provisioned locked vendor CLI')
    executable = Path(installed) / 'codex-cli/installs/0.159.0/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex'
    assert executable.is_file()
    from meta_research.native_guidance import live_native_argv
    from meta_research.root_capabilities import provided_only_cli_arguments
    from meta_research.provider_supervisor import (CODEX_SUPERVISOR_REQUEST_SCHEMA_V2,
        write_supervisor_request, write_transport_envelope, read_verified_exit_receipt, supervise)
    from meta_research.streaming_provider_supervisor import StreamingProviderProcessPlatform
    from meta_research.owners.common import canonical_hash
    from meta_research.idea_skill import _provider_native_session_unavailable

    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('CODEX_HOME', str(home))
    temporary = tmp_path / 'native-tmp'
    temporary.mkdir()
    monkeypatch.setenv('TMPDIR', str(temporary))
    _key_path, key = ensure_transport_key(tmp_path / 'provider')
    directory = tmp_path / 'provider' / 'provider-operations' / 'vendor-check' / 'acquisition-root-turn'
    directory.mkdir(parents=True)
    paths = {name: directory / filename for name, filename in (
        ('prompt_path', 'prompt.txt'), ('schema_path', 'output-schema.json'), ('stdout_path', 'stdout.jsonl'),
        ('result_path', 'last-message.json'), ('lock_path', 'supervisor.lock'), ('ready_path', 'supervisor-ready.json'),
        ('started_path', 'provider-started.json'), ('receipt_path', 'supervisor-exit.json'), ('stop_path', 'supervisor-stop.json'))}
    paths['prompt_path'].write_text('Do not use tools. Return an empty JSON object.')
    paths['schema_path'].write_text('{"type":"object","properties":{},"required":[],"additionalProperties":false}')
    invocation = {'job_ref': 'vendor-check', 'operation_name': 'acquisition-root-turn', 'native_session_ref': NATIVE}
    digest = canonical_hash(invocation)
    write_transport_envelope(directory / 'invocation.json', invocation, key)
    argv = provided_only_cli_arguments([str(executable), 'exec', '--strict-config', '--sandbox', 'workspace-write',
        '--skip-git-repo-check', '--json', '--cd', str(tmp_path), '--output-schema', str(paths['schema_path']),
        '--output-last-message', str(paths['result_path']), 'resume', NATIVE, '-'], writable_root=tmp_path, resident_mcp=False)
    wrapped = live_native_argv(argv, directory, force_transport=True, passthrough_exec=True)
    request = directory / 'supervisor-request.json'
    write_supervisor_request(request, {'schema_ref': CODEX_SUPERVISOR_REQUEST_SCHEMA_V2,
        'invocation_hash': digest, 'working_directory': str(tmp_path), 'argv': wrapped, 'timeout_seconds': 30,
        'prompt_max_bytes': 4096, 'stream_max_bytes': 8192, 'result_max_bytes': 4096,
        **{name: str(path) for name, path in paths.items()}}, key)
    supervise(request, process_platform=StreamingProviderProcessPlatform())
    receipt, envelope = read_verified_exit_receipt(paths['receipt_path'], key=key, invocation_hash=digest,
        prompt_path=paths['prompt_path'], schema_path=paths['schema_path'], stdout_path=paths['stdout_path'], result_path=paths['result_path'])
    marker = {'invocation_hash': digest, 'returncode': receipt['returncode'], 'termination_reason': receipt['termination_reason'],
        'prompt_hash': canonical_hash(paths['prompt_path'].read_text()), 'output_schema_hash': canonical_hash(json.loads(paths['schema_path'].read_text())),
        'stdout_hash': canonical_hash(paths['stdout_path'].read_text()), 'result_file_hash': receipt['result_file_hash'],
        'supervisor_receipt_hash': canonical_hash(envelope)}
    failed = _provider_native_session_unavailable(directory, marker, NATIVE)
    assert failed is not None and failed.code == 'codex_native_session_unavailable'
    assert failed.recovery_checkpoint['native_session_ref'] == NATIVE
    proof_path, = (tmp_path / 'provider').rglob('native-resume-rejected.json')
    proof = read_transport_envelope(proof_path, key)
    assert proof['source'] == 'exec'
    assert proof['exec_returncode'] == 1 and proof['exec_stdout_bytes'] == 0
    assert proof['exec_stderr'] == f'Error: thread/resume: thread/resume failed: no rollout found for thread id {NATIVE} (code -32600)\n'
    assert failed.recovery_checkpoint['result_file_hash'] == 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'
    (tmp_path / 'strict-exec-vendor-observation.json').write_text(json.dumps(proof, indent=2))


def _exec_provider(path, behavior):
    path.write_text('#!' + sys.executable + '\n' + r'''
import json,sys,time
from pathlib import Path
if '--version' in sys.argv:
 print('codex-cli 0.159.0');sys.exit(0)
if sys.argv[-2:]==['features','list']:
 print('multi_agent stable true');sys.exit(0)
root=Path(__file__);behavior=root.with_suffix('.behavior').read_text()
root.with_suffix('.argv').write_text(json.dumps(sys.argv))
assert 'exec' in sys.argv and 'app-server' not in sys.argv
native=sys.argv[sys.argv.index('resume')+1]
result=Path(sys.argv[sys.argv.index('--output-last-message')+1])
if behavior=='timeout':time.sleep(30)
if behavior=='normal':
 sys.stdin.read();result.write_text('{"ok":true}')
 print(json.dumps({'type':'thread.started','thread_id':native}));sys.exit(0)
if behavior=='stdout':print(json.dumps({'type':'thread.started','thread_id':native}))
if behavior=='result':result.write_text('{"ok":true}')
if behavior=='wrong_thread':native='00000000-0000-4000-8000-000000000098'
message='Error: thread/resume: thread/resume failed: no rollout found for thread id '+native+' (code -32600)\n'
if behavior=='ordinary':message='Error: unrelated CLI failure\n'
if behavior=='stderr_overflow':message += 'x' * 100000
sys.stderr.write(message)
sys.exit(2 if behavior=='wrong_exit' else 1)
''', encoding='utf-8')
    path.with_suffix('.behavior').write_text(behavior)
    path.chmod(0o700)
    return path


@pytest.mark.parametrize('behavior', ['wrong_thread', 'stdout', 'result', 'ordinary', 'wrong_exit', 'stderr_overflow', 'timeout'])
def test_strict_exec_cannot_promote_nonexact_or_effectful_failure(tmp_path, behavior):
    provider = _exec_provider(tmp_path / 'codex', behavior)
    with pytest.raises(IdeaSkillUnavailable) as failed:
        _invoke(tmp_path, provider, timeout=0.5 if behavior == 'timeout' else 10)
    assert failed.value.code != 'codex_native_session_unavailable'
    assert not failed.value.recovery_checkpoint or failed.value.recovery_checkpoint['schema_ref'] != 'meta-research/provider-native-session-unavailable/v1'
    if behavior == 'timeout':
        assert failed.value.code == 'codex_operation_timeout'


def test_strict_exec_preserves_normal_stdout_policy_and_supervisor_result_fifo(tmp_path):
    provider = _exec_provider(tmp_path / 'codex', 'normal')
    result, native, stdout = _invoke(tmp_path, provider)
    assert result == {'ok': True} and native == NATIVE
    assert json.loads(stdout)['thread_id'] == NATIVE
    argv = json.loads(provider.with_suffix('.argv').read_text())
    destination = argv[argv.index('--output-last-message') + 1]
    assert destination.startswith('/proc/') and '/fd/' in destination
    assert '--ignore-user-config' in argv and '--ignore-rules' in argv
    assert argv[argv.index('--sandbox') + 1] == 'workspace-write'
    assert argv[argv.index('--add-dir') + 1] == str(tmp_path)
    assert 'sandbox_workspace_write.network_access=false' in argv
    assert 'web_search="disabled"' in argv
    assert not list((tmp_path / 'provider').rglob('native-resume-rejected.json'))
