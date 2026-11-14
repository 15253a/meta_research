"""Provider processes inherit the parent environment without adapter filtering."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


def _vendor():
    installed = os.environ.get('META_RESEARCH_PROVIDER_TOOLS')
    if os.name != 'posix' or not installed:
        pytest.skip('Requires the provisioned Linux Codex 0.159.0')
    executable = Path(installed) / 'codex-cli/installs/0.159.0/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex'
    assert subprocess.check_output([executable, '--version'], text=True).strip() == 'codex-cli 0.159.0'
    return executable


@pytest.mark.parametrize('with_overrides', [False, True])
def test_process_runner_inherits_parent_environment(monkeypatch, with_overrides):
    from meta_research.quest_drafting import _CancellableProcessRunner

    monkeypatch.setenv('RESEARCH_PARENT_SETTING', 'inherited')
    monkeypatch.setenv('RESEARCH_INVOCATION_SETTING', 'parent')
    monkeypatch.setenv('RESEARCH_STORAGE_SETTING', 'parent')
    runner = _CancellableProcessRunner(protected_environment=(
        {'RESEARCH_STORAGE_SETTING': 'managed'} if with_overrides else None
    ))
    environment = ({'RESEARCH_INVOCATION_SETTING': 'invocation',
                    'RESEARCH_STORAGE_SETTING': 'requested'} if with_overrides else None)
    names = ['RESEARCH_PARENT_SETTING', 'RESEARCH_INVOCATION_SETTING', 'RESEARCH_STORAGE_SETTING']
    code = 'import json,os,sys; print(json.dumps({name: os.environ.get(name) for name in sys.argv[1:]}))'

    completed = runner([sys.executable, '-c', code, *names], '', 10, environment)

    assert completed.returncode == 0
    assert json.loads(completed.stdout) == {
        'RESEARCH_PARENT_SETTING': 'inherited',
        'RESEARCH_INVOCATION_SETTING': 'invocation' if with_overrides else 'parent',
        'RESEARCH_STORAGE_SETTING': 'managed' if with_overrides else 'parent',
    }


def test_resident_acquisition_does_not_override_shell_environment_policy(tmp_path):
    from meta_research.acquisition import AcquisitionBatchRequest, AcquisitionPaper
    from meta_research.acquisition_root import CodexAcquisitionRootAdapter
    from test_external_root_resident_mcp import (
        _SequenceRunner, _WaitingAcquisitionDelegate, _authority, _scope,
    )
    from provider_supervisor_support import closed_tree_fixture

    class CompletedSequenceRunner(_SequenceRunner):
        def __call__(self, *args, **kwargs):
            completed = super().__call__(*args, **kwargs)
            completed.process_tree = closed_tree_fixture()
            return completed

    runner = CompletedSequenceRunner([{'results': [{'paper_id': 'paper:1', 'status': 'waiting_user',
        'path': None, 'format': None, 'failure': {'code': 'institutional_login_required', 'detail': 'Login required.'}}]}])
    adapter = CodexAcquisitionRootAdapter(tmp_path / 'provider', _WaitingAcquisitionDelegate(),
        process_runner=runner)
    adapter.bind_resident_mcp_authority(_authority('acquisition', 'acquisition-root-turn'))
    adapter.configure_resident_mcp_endpoint('http://127.0.0.1:8766')
    request = AcquisitionBatchRequest('shell-env', 'oa_first_then_institution',
        (AcquisitionPaper('paper:1', 'Exact paper', None, None, ()),),
        session_ref='acquisition-session:1', session_mode='oa_only', target_dir=str(tmp_path),
        root_runtime_scope=_scope('acquisition'))
    assert adapter.acquire(request)[0].status == 'waiting_user'
    argv = runner.argvs[0]
    assert not any(value.startswith('shell_environment_policy') for value in argv)


def test_provided_only_keeps_strict_exec_isolation_without_environment_filter(tmp_path):
    executable = _vendor()
    from meta_research.paths import prepare_data_root
    from meta_research.root_capabilities import provided_only_cli_arguments

    root = prepare_data_root(tmp_path / 'data')
    # This missing-native check must remain offline even on credentialed hosts.
    (root.codex_home / 'auth.json').unlink(missing_ok=True)
    schema = tmp_path / 'schema.json'
    schema.write_text('{"type":"object","properties":{},"required":[],"additionalProperties":false}')
    argv = provided_only_cli_arguments([str(executable), 'exec', '--strict-config',
        '--skip-git-repo-check', '--sandbox', 'danger-full-access', '--json',
        '--cd', str(tmp_path), '--output-schema', str(schema), '--output-last-message', str(tmp_path / 'result'),
        'resume', '00000000-0000-4000-8000-000000000099', '-'], writable_root=tmp_path, resident_mcp=False)
    assert argv[argv.index('--sandbox') + 1] == 'workspace-write'
    assert '--ignore-user-config' in argv and '--ignore-rules' in argv
    assert 'sandbox_workspace_write.network_access=false' in argv
    assert 'web_search="disabled"' in argv
    assert 'mcp_servers={}' in argv
    assert not any(value.startswith('shell_environment_policy') for value in argv)
    # The real exec must ignore even an invalid base configuration, without
    # starting a model turn or relaxing the provided-only sandbox.
    (root.codex_home / 'config.toml').write_text('unknown_config_must_be_ignored=true\n')
    completed = subprocess.run(argv, input='Do not start a new thread.', text=True,
        env={**os.environ, **root.codex_environment}, capture_output=True, timeout=30)
    assert completed.returncode == 1
    assert not completed.stdout
    assert 'no rollout found for thread id 00000000-0000-4000-8000-000000000099' in completed.stderr
