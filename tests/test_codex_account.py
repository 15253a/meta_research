"""Account behavior through the service and a real, synthetic stdio provider."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import signal
import threading

import pytest

from meta_research.codex_account import CodexAccountService
from meta_research import codex_account
from meta_research.paths import DataRoot, prepare_data_root


_PROVIDER = r'''
import json, os, sys, threading, time, subprocess
from pathlib import Path
home = Path(os.environ['CODEX_HOME'])
state = Path(os.environ['FAKE_ACCOUNT_STATE'])
def emit(value):
    print(json.dumps(value), flush=True)
def complete():
    while True:
        try:
            options = json.loads(state.read_text())
        except json.JSONDecodeError:
            time.sleep(.01)
            continue
        if options.get('complete') or options.get('fail'):
            home.joinpath('auth.json').write_text(json.dumps({'account': {'type': 'chatgpt',
                'email': 'second@example.invalid', 'planType': 'pro'},
                'tokens': {'access_token': 'synthetic-only', 'account_id': 'second-account'}}))
            emit({'method': 'account/login/completed', 'params': {'loginId': 'login-test',
                  'success': not options.get('fail', False), 'error': 'must-never-be-returned'}})
            return
        time.sleep(.02)
for line in sys.stdin:
    request = json.loads(line)
    if 'id' not in request:
        continue
    method = request['method']
    options = json.loads(state.read_text())
    result = {}
    if method == 'account/read':
        if options.get('descendant'):
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
            state.with_suffix('.child-pid').write_text(str(child.pid))
            time.sleep(60)
        if options.get('read_timeout'):
            time.sleep(60)
        if options.get('read_error'):
            emit({'id': request['id'], 'error': {'message': 'must-never-be-returned'}})
            continue
        if options.get('oversize'):
            print('x' * 200000, flush=True)
            continue
        assert request['params'] == {'refreshToken': False}
        auth = home / 'auth.json'
        if options.get('overwrite_on_read') and auth.exists():
            changed = json.loads(auth.read_text())
            changed['tokens']['account_id'] = 'old-account'
            changed['account']['email'] = 'old@example.invalid'
            auth.write_text(json.dumps(changed))
        result = {'account': json.loads(auth.read_text()).get('account') if auth.exists() else None,
                  'access_token': 'must-never-be-returned'}
    elif method == 'account/login/start':
        assert request['params'] == {'type': 'chatgptDeviceCode'}
        result = {'loginId': 'login-test', 'verificationUrl': options.get('url', 'https://auth.openai.com/codex/device'),
                  'userCode': 'TEST-CODE'}
        threading.Thread(target=complete, daemon=True).start()
    elif method == 'account/login/cancel':
        result = {'status': 'cancelled'}
        home.joinpath('auth.json').write_text(json.dumps({'account': {'type': 'chatgpt',
            'email': 'late@example.invalid', 'planType': 'pro'}}))
        emit({'method': 'account/login/completed', 'params': {'loginId': 'login-test', 'success': True}})
    elif method == 'account/logout':
        home.joinpath('auth.json').unlink(missing_ok=True)
    emit({'id': request['id'], 'result': result})
'''


@pytest.fixture
def provider(tmp_path, monkeypatch):
    monkeypatch.setenv('META_RESEARCH_PROVIDER_TOOLS', str(tmp_path / 'shared'))
    monkeypatch.setenv('META_RESEARCH_CODEX_AUTH_TEMPLATE', str(tmp_path / 'absent-template'))
    root = prepare_data_root(tmp_path / 'instance')
    executable = root.codex_cli_executable
    executable.parent.mkdir(parents=True)
    executable.write_text('synthetic-provider', encoding='utf-8')
    executable.chmod(0o700)
    package = root.codex_cli_install_root / 'node_modules/@openai/codex/package.json'
    package.parent.mkdir(parents=True)
    package.write_text(json.dumps({'version': '0.159.0'}))
    shim = tmp_path / 'provider.py'
    shim.write_text(_PROVIDER, encoding='utf-8')
    state = tmp_path / 'fake-state.json'
    state.write_text('{}')
    native_popen = subprocess.Popen
    def spawn(argv, **kwargs):
        assert argv[0] == str(executable.absolute())
        assert 'app-server' in argv
        assert not kwargs.get('shell', False)
        assert Path(kwargs['env']['CODEX_HOME']).is_relative_to(root.root)
        kwargs['env']['FAKE_ACCOUNT_STATE'] = str(state)
        return native_popen([sys.executable, '-u', str(shim)], **kwargs)
    monkeypatch.setattr(subprocess, 'Popen', spawn)
    yield root, state


def test_read_reports_current_provider_account_without_credentials_or_cache(provider):
    root, _ = provider
    auth = root.codex_home / 'auth.json'
    auth.write_text(json.dumps({'account': {'type': 'chatgpt', 'email': 'first@example.invalid',
                               'planType': 'plus', 'access_token': 'must-never-be-returned'}}))
    service = CodexAccountService(root)
    try:
        first = service.read()
        assert first == {'status': 'authenticated', 'account': {'type': 'chatgpt',
                         'email': 'first@example.invalid', 'plan_type': 'plus'},
                         'login': None, 'error_code': None}
        auth.write_text(json.dumps({'account': {'type': 'apiKey', 'apiKey': 'must-never-be-returned'}}))
        assert service.read()['account'] == {'type': 'apiKey', 'email': None, 'plan_type': None}
        auth.unlink()
        assert service.read()['status'] == 'unauthenticated'
    finally:
        service.close()


def test_device_login_keeps_current_account_until_completed_then_survives_restart(provider):
    root, state = provider
    root.codex_home.joinpath('auth.json').write_text(json.dumps({'account': {
        'type': 'chatgpt', 'email': 'first@example.invalid', 'planType': 'plus'}}))
    service = CodexAccountService(root)
    try:
        pending = service.start_login()
        assert pending['account']['email'] == 'first@example.invalid'
        assert pending['login']['status'] == 'pending'
        assert pending['login']['verification_url'] == 'https://auth.openai.com/codex/device'
        assert pending['login']['user_code'] == 'TEST-CODE'
        assert service.start_login()['login']['id'] == pending['login']['id']
        state.write_text('{"complete":true}')
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = service.read()
            if result['login']['status'] == 'completed':
                break
            time.sleep(.02)
        assert result['login']['status'] == 'completed'
        assert result['account']['email'] == 'second@example.invalid'
        assert result['login']['user_code'] is None
        assert 'must-never-be-returned' not in json.dumps(result)
    finally:
        service.close()
    restarted = CodexAccountService(root)
    try:
        assert restarted.read()['account']['email'] == 'second@example.invalid'
    finally:
        restarted.close()


def test_cancelling_login_ignores_late_success_and_preserves_the_current_account(provider):
    root, _ = provider
    root.codex_home.joinpath('auth.json').write_text(json.dumps({'account': {
        'type': 'chatgpt', 'email': 'first@example.invalid', 'planType': 'plus'}}))
    service = CodexAccountService(root)
    try:
        pending = service.start_login()
        cancelled = service.cancel_login(pending['login']['id'])
        assert cancelled['login']['status'] == 'cancelled'
        assert cancelled['account']['email'] == 'first@example.invalid'
        assert cancelled['login']['user_code'] is None
        assert not list(root.run.glob('codex-account-login-*'))
        assert service.read()['account']['email'] == 'first@example.invalid'
    finally:
        service.close()


def test_logout_cancels_pending_login_and_remains_logged_out_after_restart(provider, tmp_path, monkeypatch):
    root, _ = provider
    template = tmp_path / 'shared-auth.json'
    original = json.dumps({'account': {'type': 'chatgpt', 'email': 'first@example.invalid', 'planType': 'plus'}})
    template.write_text(original)
    root.codex_home.joinpath('auth.json').write_text(original)
    monkeypatch.setenv('META_RESEARCH_CODEX_AUTH_TEMPLATE', str(template))
    service = CodexAccountService(root)
    try:
        assert service.start_login()['login']['status'] == 'pending'
        logged_out = service.logout()
        assert logged_out['status'] == 'unauthenticated'
        assert logged_out['login']['status'] == 'cancelled'
        assert not list(root.run.glob('codex-account-login-*'))
    finally:
        service.close()
    restarted = CodexAccountService(prepare_data_root(root.root))
    try:
        assert restarted.read()['status'] == 'unauthenticated'
        assert template.read_text() == original
    finally:
        restarted.close()


@pytest.mark.parametrize('option,code', [('read_error', 'codex_account_provider_rejected'),
    ('read_timeout', 'codex_account_request_timeout'), ('oversize', 'codex_account_invalid_response')])
def test_provider_failures_are_bounded_and_never_expose_native_output(provider, monkeypatch, option, code):
    root, state = provider
    state.write_text(json.dumps({option: True}))
    monkeypatch.setattr(codex_account, '_REQUEST_TIMEOUT', .3)
    service = CodexAccountService(root)
    try:
        started = time.monotonic()
        result = service.read()
        assert result['status'] == 'unavailable'
        assert result['error_code'] == code
        assert 'must-never-be-returned' not in json.dumps(result)
        assert time.monotonic() - started < 5
    finally:
        service.close()


@pytest.mark.parametrize('url', ['http://auth.openai.com/codex/device',
    'https://auth.openai.com.evil.invalid/codex/device', 'https://auth.openai.com/codex/device?access_token=secret',
    'https://other@auth.openai.com/codex/device', 'https://auth.openai.com/other'])
def test_login_rejects_untrusted_device_urls_without_changing_account(provider, url):
    root, state = provider
    state.write_text(json.dumps({'url': url}))
    service = CodexAccountService(root)
    try:
        result = service.start_login()
        assert result['status'] == 'unauthenticated'
        assert result['login']['status'] == 'failed'
        assert result['login']['error_code'] == 'codex_account_login_url_invalid'
        assert result['login']['verification_url'] is None
        assert not list(root.run.glob('codex-account-login-*'))
    finally:
        service.close()


@pytest.mark.parametrize('outcome', ['fail', 'expired', 'close'])
def test_incomplete_login_preserves_existing_authentication_and_cleans_temporary_home(provider, monkeypatch, outcome):
    root, state = provider
    root.codex_home.joinpath('auth.json').write_text(json.dumps({'account': {
        'type': 'chatgpt', 'email': 'first@example.invalid', 'planType': 'plus'}}))
    if outcome == 'expired':
        monkeypatch.setattr(codex_account, '_LOGIN_LIFETIME', .25)
    service = CodexAccountService(root)
    try:
        service.start_login()
        if outcome == 'fail':
            state.write_text('{"fail":true}')
        if outcome == 'close':
            service.close()
        else:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                result = service.read()
                if result['login']['status'] == 'failed':
                    break
                time.sleep(.03)
            assert result['login']['status'] == 'failed'
            assert result['login']['user_code'] is None
            assert 'must-never-be-returned' not in json.dumps(result)
        assert not list(root.run.glob('codex-account-login-*'))
    finally:
        service.close()
    restarted = CodexAccountService(root)
    try:
        assert restarted.read()['account']['email'] == 'first@example.invalid'
    finally:
        restarted.close()


def test_account_service_refuses_a_redirected_managed_home(provider):
    root, _ = provider
    moved = root.root / 'other-home'
    root.codex_home.rename(moved)
    moved.joinpath('auth.json').write_text(json.dumps({'account': {
        'type': 'chatgpt', 'email': 'outside@example.invalid', 'planType': 'plus'}}))
    try:
        root.codex_home.symlink_to(moved, target_is_directory=True)
    except OSError:
        moved.rename(root.codex_home)
        pytest.skip('symlinks unavailable')
    service = CodexAccountService(root)
    try:
        result = service.read()
        assert result['status'] == 'unavailable'
        assert result['account'] is None
        assert result['error_code'] == 'codex_account_credentials_invalid'
    finally:
        service.close()


def test_logout_rejects_late_refresh_from_an_already_running_provider(provider):
    root, _ = provider
    auth = root.codex_home / 'auth.json'
    original = json.dumps({'account': {'type': 'chatgpt', 'email': 'first@example.invalid', 'planType': 'plus'}})
    auth.write_text(original)
    service = CodexAccountService(root)
    try:
        assert service.logout()['status'] == 'unauthenticated'
        auth.write_text(original)
        assert service.read()['status'] == 'unauthenticated'
    finally:
        service.close()


def test_account_switch_accepts_same_account_refresh_but_rejects_late_old_identity(provider):
    root, state = provider
    service = CodexAccountService(root)
    try:
        service.start_login()
        state.write_text('{"complete":true}')
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = service.read()
            if result['login']['status'] == 'completed':
                break
            time.sleep(.02)
        assert result['login']['status'] == 'completed'
        auth = root.codex_home / 'auth.json'
        updated = json.loads(auth.read_text())
        updated['tokens']['access_token'] = 'same-account-refreshed'
        auth.write_text(json.dumps(updated))
        assert service.read()['account']['email'] == 'second@example.invalid'
        updated['tokens']['account_id'] = 'old-account'
        updated['account']['email'] = 'old@example.invalid'
        auth.write_text(json.dumps(updated))
        result = service.read()
        assert result['status'] == 'unavailable'
        assert result['account'] is None
        assert result['error_code'] == 'codex_account_credentials_changed'
    finally:
        service.close()


@pytest.mark.skipif(os.name != 'posix', reason='POSIX native process group lifecycle')
def test_timeout_closes_native_descendants_that_inherit_stdout(provider, monkeypatch):
    root, state = provider
    state.write_text('{"descendant":true}')
    monkeypatch.setattr(codex_account, '_REQUEST_TIMEOUT', .3)
    service = CodexAccountService(root)
    results = []
    worker = threading.Thread(target=lambda: results.append(service.read()), daemon=True)
    worker.start()
    try:
        worker.join(timeout=5)
        assert not worker.is_alive(), 'provider descendants prevented bounded transport shutdown'
        assert results[0]['error_code'] == 'codex_account_request_timeout'
    finally:
        # Own synthetic process group only; also cleans up the intentional red run.
        evidence = state.with_suffix('.child-pid')
        if evidence.exists():
            try:
                group = os.getpgid(int(evidence.read_text()))
                assert group != os.getpgrp()
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass
        worker.join(timeout=3)
        if not worker.is_alive():
            service.close()


def test_account_read_rechecks_identity_if_credentials_change_during_native_request(provider):
    root, state = provider
    service = CodexAccountService(root)
    try:
        service.start_login()
        state.write_text('{"complete":true}')
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = service.read()
            if result['login']['status'] == 'completed':
                break
            time.sleep(.02)
        assert result['login']['status'] == 'completed'
        state.write_text('{"overwrite_on_read":true}')
        result = service.read()
        assert result['status'] == 'unavailable'
        assert result['account'] is None
        assert result['error_code'] == 'codex_account_credentials_changed'
    finally:
        service.close()
