"""An offline local delete must not refresh or replace the configured login."""
import base64
import http.server
import json
import os
from pathlib import Path
import sqlite3
import threading

from meta_research.native_session_cleanup import delete_codex_sessions
from meta_research.paths import DataRoot
from test_codex_shell_environment import _vendor
from test_native_session_cleanup import _server, _saved_threads


def _expired_synthetic_login():
    def part(data):
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    account = "00000000-0000-4000-8000-000000000001"
    claims = {"sub": "synthetic", "exp": 1, "iat": 0, "email": "synthetic@example.invalid",
              "https://api.openai.com/auth": {"chatgpt_account_id": account, "chatgpt_plan_type": "plus"}}
    token = ".".join([part({"alg": "RS256", "typ": "JWT"}), part(claims), "AA"])
    return json.dumps({"auth_mode": "chatgpt", "last_refresh": "2020-01-01T00:00:00Z",
                       "tokens": {"id_token": token, "access_token": token, "account_id": account,
                                  "refresh_token": "synthetic-expired"}}).encode()


def test_delete_local_rollout_without_refreshing_expired_login(tmp_path, monkeypatch):
    executable = _vendor()
    # Construct the test home directly: it must never seed an operator's auth template.
    root = DataRoot(tmp_path / "instance")
    root.codex_user_home.mkdir(parents=True)
    root.codex_tmp.mkdir()
    refreshing = threading.Event()
    release = threading.Event()

    class Proxy(http.server.BaseHTTPRequestHandler):
        def do_CONNECT(self):
            if self.path == "auth.openai.com:443":
                refreshing.set()
                release.wait(8)
            self.send_response(403)
            self.end_headers()

        def log_message(self, *args):
            pass

    with http.server.ThreadingHTTPServer(("127.0.0.1", 0), Proxy) as proxy:
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        address = f"http://127.0.0.1:{proxy.server_port}"
        monkeypatch.setattr(os, "environ", {"PATH": os.environ["PATH"], "HTTP_PROXY": address,
                            "HTTPS_PROXY": address, "ALL_PROXY": address, "NO_PROXY": "127.0.0.1,localhost"})
        try:
            target, survivor = _saved_threads(executable, root, ["temporary work", "continuing work"])
            state = root.codex_home / "state_5.sqlite"
            with sqlite3.connect(state) as db:
                paths = {ref: Path(path) for ref, path in db.execute("SELECT id,rollout_path FROM threads")}
            auth = root.codex_home / "auth.json"
            login = _expired_synthetic_login()
            auth.write_bytes(login)

            record, = delete_codex_sessions(root, str(executable), [target], dry_run=False)
            assert record["action"] == "removed", record
            assert not refreshing.is_set()
            assert not paths[target].exists() and paths[survivor].is_file()
            with sqlite3.connect(state) as db:
                assert db.execute("SELECT id FROM threads").fetchall() == [(survivor,)]
            assert auth.read_bytes() == login

            release.set()
            with _server(executable, root) as read:
                assert read("thread/read", {"threadId": survivor})["thread"]["id"] == survivor
        finally:
            release.set()
            proxy.shutdown()
