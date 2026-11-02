"""Observe the Adapter policy through the locked CLI's actual command runner."""
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading

import pytest

from meta_research.acquisition import AcquisitionBatchRequest, AcquisitionPaper
from meta_research.acquisition_root import CodexAcquisitionRootAdapter
from test_external_root_resident_mcp import (
    _SequenceRunner, _WaitingAcquisitionDelegate, _authority, _scope,
)


def _vendor():
    installed = os.environ.get('META_RESEARCH_PROVIDER_TOOLS')
    if os.name != 'posix' or not installed:
        pytest.skip('Requires the provisioned Linux Codex 0.159.0')
    executable = Path(installed) / 'codex-cli/installs/0.159.0/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex'
    assert subprocess.check_output([executable, '--version'], text=True).strip() == 'codex-cli 0.159.0'
    return executable


def _command_environment(executable, arguments, environment, cwd):
    # No model call or MCP credential is needed for Codex's real shell runner.
    policy = [part for pair in zip(arguments, arguments[1:])
              if pair[0] == '--config' and pair[1].startswith('shell_environment_policy')
              for part in pair]
    process = subprocess.Popen([str(executable), 'app-server', '--strict-config', *policy],
        env=environment, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True)
    records = queue.Queue()
    threading.Thread(target=lambda: [records.put(json.loads(line)) for line in process.stdout], daemon=True).start()

    def call(identifier, method, params):
        process.stdin.write(json.dumps({'id': identifier, 'method': method, 'params': params}) + '\n')
        process.stdin.flush()
        while True:
            record = records.get(timeout=20)
            if record.get('id') == identifier:
                assert 'error' not in record, record
                return record['result']

    try:
        call(1, 'initialize', {'clientInfo': {'name': 'shell-policy-test', 'version': '1'},
                              'capabilities': {'experimentalApi': True}})
        process.stdin.write('{"method":"initialized"}\n')
        process.stdin.flush()
        code = ('import json,os,shutil,subprocess; print(json.dumps({'
                '"tools":{n:shutil.which(n) for n in ("node","python","cat")},'
                '"node_ran":subprocess.check_output(["node","-p","2+2"],text=True).strip()=="4",'
                '"python_ran":subprocess.check_output(["python","-c","print(3+3)"],text=True).strip()=="6",'
                '"home":os.environ.get("HOME"),"tmp":os.environ.get("TMPDIR"),'
                '"proxy_present":os.environ.get("HTTP_PROXY")=="http://probe:private@127.0.0.1:9",'
                '"forbidden":[n for n in ("META_RESEARCH_MCP_TOKEN","EXTERNAL_MCP_AUTH",'
                '"ARBITRARY_SECRET","CONFIG_SECRET","CONFIG_OTHER") if n in os.environ]}))')
        result = call(2, 'command/exec', {'command': [sys.executable, '-c', code],
            'cwd': str(cwd), 'sandboxPolicy': {'type': 'dangerFullAccess'}, 'timeoutMs': 10000})
        assert result['exitCode'] == 0, result
        return json.loads(result['stdout'])
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=5)


def test_resident_acquisition_shell_has_tools_and_proxy_without_bearer_or_host_secrets(tmp_path, monkeypatch):
    executable = _vendor()
    home = tmp_path / 'managed-home'
    home.mkdir()
    temporary = tmp_path / 'managed-tmp'
    temporary.mkdir()
    codex_home = tmp_path / 'codex'
    codex_home.mkdir()
    # An empty override map is insufficient: Codex deeply merges config.set.
    (codex_home / 'config.toml').write_text('[shell_environment_policy]\ninherit="all"\n'
        'include_only=["*"]\n[shell_environment_policy.set]\nPATH="/wrong/config/path"\n'
        'HOME="/wrong/config/home"\nCONFIG_SECRET="private"\nCONFIG_OTHER="private"\n'
        'META_RESEARCH_MCP_TOKEN="config-bearer"\n')
    expected_tools = {name: shutil.which(name) for name in ('node', 'python', 'cat')}
    assert all(expected_tools.values())
    for name, value in {'CODEX_HOME': str(codex_home), 'HOME': str(home), 'TMPDIR': str(temporary),
        'HTTP_PROXY': 'http://probe:private@127.0.0.1:9', 'EXTERNAL_MCP_AUTH': 'external-private',
        'ARBITRARY_SECRET': 'host-private'}.items():
        monkeypatch.setenv(name, value)
    runner = _SequenceRunner([{'results': [{'paper_id': 'paper:1', 'status': 'waiting_user',
        'path': None, 'format': None, 'failure': {'code': 'institutional_login_required', 'detail': 'Login required.'}}]}])
    adapter = CodexAcquisitionRootAdapter(tmp_path / 'provider', _WaitingAcquisitionDelegate(),
        executable=str(executable), process_runner=runner, codex_home=codex_home)
    adapter.bind_resident_mcp_authority(_authority('acquisition', 'acquisition-root-turn'))
    adapter.configure_resident_mcp_endpoint('http://127.0.0.1:8766')
    request = AcquisitionBatchRequest('shell-env', 'oa_first_then_institution',
        (AcquisitionPaper('paper:1', 'Exact paper', None, None, ()),),
        session_ref='acquisition-session:1', session_mode='oa_only', target_dir=str(tmp_path),
        root_runtime_scope=_scope('acquisition'))
    assert adapter.acquire(request)[0].status == 'waiting_user'
    argv = runner.argvs[0]
    assert 'probe:private' not in json.dumps(argv)
    environment = dict(os.environ, **runner.environments[0])
    observed = _command_environment(executable, argv, environment, tmp_path)
    assert observed == {'tools': expected_tools, 'node_ran': True, 'python_ran': True,
                        'home': str(home), 'tmp': str(temporary),
                        'proxy_present': True, 'forbidden': []}
    before = len(runner.argvs)
    assert adapter.reconcile(request)[0].status == 'waiting_user'
    assert len(runner.argvs) == before


def test_managed_paths_win_and_provided_only_keeps_strict_exec_isolation(tmp_path, monkeypatch):
    from meta_research.paths import prepare_data_root
    from meta_research.quest_drafting import _CancellableProcessRunner, protected_codex_shell_arguments
    from meta_research.root_capabilities import provided_only_cli_arguments
    executable = _vendor()
    root = prepare_data_root(tmp_path / 'data')
    # This missing-native check must remain offline even on credentialed hosts.
    (root.codex_home / 'auth.json').unlink(missing_ok=True)
    monkeypatch.setenv('HOME', '/wrong/host/home')
    monkeypatch.setenv('TMPDIR', '/wrong/host/tmp')
    monkeypatch.setenv('HTTP_PROXY', 'http://probe:private@127.0.0.1:9')
    runner = _CancellableProcessRunner(protected_environment=root.codex_environment)
    schema = tmp_path / 'schema.json'
    schema.write_text('{"type":"object","properties":{},"required":[],"additionalProperties":false}')
    argv = provided_only_cli_arguments([str(executable), 'exec', '--strict-config',
        '--skip-git-repo-check', '--sandbox', 'danger-full-access', '--json',
        '--cd', str(tmp_path), '--output-schema', str(schema), '--output-last-message', str(tmp_path / 'result'),
        'resume', '00000000-0000-4000-8000-000000000099', '-'], writable_root=tmp_path, resident_mcp=False)
    argv = protected_codex_shell_arguments(argv, runner)
    assert argv[argv.index('--sandbox') + 1] == 'workspace-write'
    assert '--ignore-user-config' in argv and '--ignore-rules' in argv
    assert 'sandbox_workspace_write.network_access=false' in argv
    assert 'web_search="disabled"' in argv
    assert 'mcp_servers={}' in argv
    assert 'probe:private' not in json.dumps(argv)
    # The real exec must ignore even an invalid base configuration, without
    # starting a model turn or relaxing the provided-only sandbox.
    (root.codex_home / 'config.toml').write_text('unknown_config_must_be_ignored=true\n')
    completed = subprocess.run(argv, input='Do not start a new thread.', text=True,
        env={**os.environ, **root.codex_environment}, capture_output=True, timeout=30)
    assert completed.returncode == 1
    assert not completed.stdout
    assert 'no rollout found for thread id 00000000-0000-4000-8000-000000000099' in completed.stderr
    (root.codex_home / 'config.toml').unlink()
    # Exercise the same allowlist with the OA command runner. This check does
    # not claim that the unavailable host bwrap sandbox executed successfully.
    observed = _command_environment(executable, argv, {**os.environ, **root.codex_environment}, tmp_path)
    assert observed['home'] == root.codex_environment['HOME']
    assert observed['tmp'] == root.codex_environment['TMPDIR']
    assert observed['forbidden'] == []
    assert observed['proxy_present'] and observed['node_ran'] and observed['python_ran']
