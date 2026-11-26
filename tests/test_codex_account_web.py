import pytest
from fastapi.testclient import TestClient

from meta_research.composition import build_production_runtime
from meta_research.paths import prepare_data_root
from meta_research import web
from test_corrected_quest_initialization import (
    DeterministicDraftingAdapter, DeterministicProbe, _authenticated_client,
)


class AccountService:
    def __init__(self):
        self.calls = []
        self.closed = False

    def read(self):
        return {'status': 'authenticated', 'account': {'type': 'chatgpt',
            'email': 'researcher@example.invalid', 'plan_type': 'plus',
            'access_token': 'must-not-leave-server'}, 'login': None, 'error_code': None,
            'refresh_token': 'must-not-leave-server'}

    def start_login(self):
        self.calls.append('start')
        return {**self.read(), 'login': {'id': 'login-123', 'status': 'pending',
            'verification_url': 'https://auth.openai.com/codex/device', 'user_code': 'ABCD-1234',
            'error_code': None, 'expires_at': 12345, 'access_token': 'must-not-leave-server'}}

    def cancel_login(self, login_id):
        self.calls.append(('cancel', login_id))
        return self.read()

    def logout(self):
        self.calls.append('logout')
        return {'status': 'unauthenticated', 'account': None, 'login': None, 'error_code': None}

    def close(self):
        self.closed = True


@pytest.fixture
def account_runtime(tmp_path, monkeypatch):
    service = AccountService()
    roots = []
    def factory(root):
        roots.append(root)
        return service
    monkeypatch.setattr(web, 'CodexAccountService', factory, raising=False)
    adapter = DeterministicDraftingAdapter()
    runtime = build_production_runtime(prepare_data_root(tmp_path/'account'),
        proposal_drafter=adapter, intent_drafting_provider=adapter, host_compute_probe=DeterministicProbe())
    try:
        yield runtime, service, roots
    finally:
        runtime.close()


def test_codex_account_api_displays_identity_and_retains_login_controls(account_runtime):
    runtime, service, roots = account_runtime
    client, headers = _authenticated_client(runtime)
    response = client.get('/api/v1/codex/account')
    assert response.status_code == 200, response.text
    assert response.json()['account']['email'] == 'researcher@example.invalid'
    assert roots == [runtime.data_root]
    assert response.headers['cache-control'] == 'no-store'
    assert 'must-not-leave-server' not in response.text
    for path, payload in [('login', {}), ('login/cancel', {'login_id': 'login-123'}), ('logout', {})]:
        blocked = client.post('/api/v1/codex/account/' + path, json=payload)
        assert blocked.status_code == 403
        response = client.post('/api/v1/codex/account/' + path, json=payload, headers=headers)
        assert response.status_code == 200, response.text
        assert 'must-not-leave-server' not in response.text
    assert service.calls == ['start', ('cancel', 'login-123'), 'logout']
    assert response.json()['status'] == 'unauthenticated'


def test_codex_account_api_requires_web_auth_and_rejects_credential_payloads(account_runtime):
    runtime, service, _ = account_runtime
    client = TestClient(web.create_app(runtime, base_url='http://testserver', control_key='control'))
    assert client.get('/api/v1/codex/account').status_code == 401
    client, headers = _authenticated_client(runtime)
    assert client.post('/api/v1/codex/account/login', headers=headers,
                       json={'access_token': 'not-an-input'}).status_code == 422
    assert client.post('/api/v1/codex/account/login/cancel', headers=headers,
                       json={'login_id': ''}).status_code == 422
    assert service.calls == []


def test_codex_account_api_redacts_unexpected_provider_failure(account_runtime):
    runtime, service, _ = account_runtime
    client, _ = _authenticated_client(runtime)
    def failure():
        raise RuntimeError('access_token=must-not-leave-server')
    service.read = failure
    response = client.get('/api/v1/codex/account')
    assert response.status_code == 503
    assert response.json()['detail']['code'] == 'codex_account_unavailable'
    assert 'must-not-leave-server' not in response.text


def test_web_shutdown_closes_pending_account_login(account_runtime):
    runtime, service, _ = account_runtime
    with TestClient(web.create_app(runtime, base_url='http://testserver', control_key='control')):
        assert not service.closed
    assert service.closed


def test_codex_account_api_exposes_sanitized_quota_and_stays_compatible(account_runtime):
    runtime, service, _ = account_runtime
    client, _ = _authenticated_client(runtime)
    legacy = client.get('/api/v1/codex/account')
    assert legacy.status_code == 200, legacy.text
    assert 'quota' in legacy.json()
    assert legacy.json()['quota'] is None

    def read_with_quota():
        return {
            'status': 'authenticated',
            'account': {'type': 'chatgpt', 'email': 'researcher@example.invalid', 'plan_type': 'pro'},
            'quota': {
                'ordinary_usage_allowed': True,
                'used_percent': 61.0,
                'remaining_percent': 39.0,
                'window_duration_mins': 10080,
                'resets_at': 1796000000,
                'secondary_used_percent': 15.0,
                'secondary_resets_at': 1796100000,
                'spend_control_reached': False,
                'rate_limit_reached_type': None,
                'has_credits': True,
                'unlimited': False,
                'balance': '18.00',
                'exhausted': False,
                'accountId': 'secret-account-id',
                'access_token': 'must-not-leave-server',
            },
            'login': None,
            'error_code': None,
            'accountId': 'top-level-secret-account-id',
        }

    service.read = read_with_quota
    response = client.get('/api/v1/codex/account')
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload['quota'] == {
        'ordinary_usage_allowed': True,
        'used_percent': 61.0,
        'remaining_percent': 39.0,
        'window_duration_mins': 10080,
        'resets_at': 1796000000,
        'secondary_used_percent': 15.0,
        'secondary_resets_at': 1796100000,
        'spend_control_reached': False,
        'rate_limit_reached_type': None,
        'has_credits': True,
        'unlimited': False,
        'balance': '18.00',
        'exhausted': False,
    }
    assert 'secret-account-id' not in response.text
    assert 'must-not-leave-server' not in response.text
    assert 'accountId' not in response.text
