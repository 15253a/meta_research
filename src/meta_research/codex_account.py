"""Instance-local Codex account management through the locked native protocol.

Only explicit public account fields cross this boundary. Provider streams and
credential files never enter application logs or research records.
"""
from __future__ import annotations

import json
import base64
import hashlib
import os
from pathlib import Path
import queue
import re
import secrets
import shutil
import signal
import stat
import subprocess
import threading
import time
from urllib.parse import urlsplit

from meta_research.codex_runtime import CODEX_LOCKED_VERSION
from meta_research.paths import DataRoot

_REQUEST_TIMEOUT = 10.0
_MAX_RECORD = 128 * 1024
_MAX_STREAM = 2 * 1024 * 1024
_LOGIN_LIFETIME = 15 * 60
_AUTH_LIMIT = 1024 * 1024


class CodexAccountStateError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


_AccountError = CodexAccountStateError


class _Rpc:
    """Bounded native stdio transport; responses and notifications are separate."""

    def __init__(self, executable: Path, root: DataRoot):
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._pending: dict[int, queue.Queue] = {}
        self._next_id = 0
        self._failure: _AccountError | None = None
        self.notifications: queue.Queue = queue.Queue(maxsize=32)
        self._closed = False
        environment = dict(os.environ)
        environment.update(root.codex_environment)
        try:
            self.process = subprocess.Popen(
                [str(executable), "app-server", "--config", 'cli_auth_credentials_store="file"'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                cwd=str(root.codex_home), env=environment, close_fds=True,
                **({"start_new_session": True} if os.name == "posix" else
                   {"creationflags": subprocess.CREATE_NO_WINDOW}),
            )
        except (OSError, ValueError) as error:
            raise _AccountError("codex_account_provider_unavailable") from error
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        try:
            self.request("initialize", {"clientInfo": {"name": "meta_research_account", "version": "1.0"}})
            self._send({"method": "initialized"})
        except Exception:
            self.close()
            raise

    def _send(self, value: dict) -> None:
        try:
            with self._write_lock:
                self.process.stdin.write((json.dumps(value) + "\n").encode("utf-8"))
                self.process.stdin.flush()
        except (OSError, ValueError) as error:
            raise _AccountError("codex_account_provider_unavailable") from error

    def _fail(self, code: str) -> None:
        with self._lock:
            self._failure = _AccountError(code)
            for response in self._pending.values():
                try:
                    response.put_nowait(self._failure)
                except queue.Full:
                    pass
        try:
            self.notifications.put_nowait(self._failure)
        except queue.Full:
            pass

    def _read(self) -> None:
        try:
            total = 0
            while line := self.process.stdout.readline(_MAX_RECORD + 1):
                total += len(line)
                if len(line) > _MAX_RECORD or total > _MAX_STREAM:
                    raise _AccountError("codex_account_invalid_response")
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise _AccountError("codex_account_invalid_response")
                if "id" in record and "method" not in record:
                    with self._lock:
                        response = self._pending.get(record["id"])
                        if response is not None:
                            response.put_nowait(record)
                elif record.get("method") == "account/login/completed":
                    self.notifications.put_nowait(record.get("params"))
            self._fail("codex_account_provider_unavailable")
        except Exception:
            self._fail("codex_account_invalid_response")

    def request(self, method: str, params: dict) -> dict:
        response: queue.Queue = queue.Queue(maxsize=1)
        with self._lock:
            if self._failure is not None:
                raise self._failure
            self._next_id += 1
            request_id = self._next_id
            self._pending[request_id] = response
        try:
            self._send({"id": request_id, "method": method, "params": params})
            try:
                value = response.get(timeout=_REQUEST_TIMEOUT)
            except queue.Empty as error:
                raise _AccountError("codex_account_request_timeout") from error
            if isinstance(value, _AccountError):
                raise value
            if "error" in value:
                raise _AccountError("codex_account_provider_rejected")
            return value.get("result")
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        def signal_group(number: int) -> None:
            # Popen created this exact group with start_new_session=True.
            # A node wrapper may already have exited while its native child
            # still holds stdout, so signal the group even after parent exit.
            if self.process.pid != os.getpgrp():
                try:
                    os.killpg(self.process.pid, number)
                except ProcessLookupError:
                    pass
        if os.name == "posix":
            signal_group(signal.SIGTERM)
        elif self.process.poll() is None:
            self.process.terminate()
        try:
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        finally:
            if os.name == "posix":
                signal_group(signal.SIGKILL)
        self._reader.join(timeout=1)
        for pipe in (self.process.stdin, self.process.stdout):
            if pipe is not None:
                pipe.close()


def _public_account(result: object) -> dict | None:
    if not isinstance(result, dict) or "account" not in result:
        raise _AccountError("codex_account_invalid_response")
    account = result["account"]
    if account is None:
        return None
    if not isinstance(account, dict) or account.get("type") not in {"chatgpt", "apiKey"}:
        raise _AccountError("codex_account_invalid_response")
    def text(value: object, limit: int) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
            raise _AccountError("codex_account_invalid_response")
        return value
    return {"type": account["type"],
            "email": text(account.get("email"), 320) if account["type"] == "chatgpt" else None,
            "plan_type": text(account.get("planType"), 64) if account["type"] == "chatgpt" else None}


def _read_private(path: Path) -> bytes | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _AUTH_LIMIT:
        raise _AccountError("codex_account_credentials_invalid")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise _AccountError("codex_account_credentials_invalid")
        content = stream.read(_AUTH_LIMIT + 1)
    if len(content) > _AUTH_LIMIT:
        raise _AccountError("codex_account_credentials_invalid")
    return content


def _write_private(path: Path, content: bytes) -> None:
    _read_private(path)
    temporary = path.with_name("." + path.name + "." + secrets.token_hex(12))
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise _AccountError("codex_account_credentials_invalid")
    path.chmod(0o700)


def _account_id_hash(content: bytes) -> str:
    try:
        value = json.loads(content)
        tokens = value.get("tokens") or {}
        account_id = tokens.get("account_id")
        if not account_id:
            token = tokens.get("id_token", "")
            segment = token.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
            account_id = (claims.get("https://api.openai.com/auth") or {}).get("chatgpt_account_id")
        if not isinstance(account_id, str) or not 1 <= len(account_id) <= 512:
            raise ValueError("missing account identity")
        return hashlib.sha256(account_id.encode("utf-8")).hexdigest()
    except Exception as error:
        raise _AccountError("codex_account_credentials_invalid") from error


def ensure_codex_account_current(home: Path) -> None:
    """Refuse new provider calls after logout or an old account's late refresh.

    A marker is written only by an explicit account activation/logout. Legacy
    homes retain their existing behavior. Token rotation within the selected
    account is allowed; no credential payload escapes this boundary.
    """
    try:
        marker = home / "account-state.json"
        content = _read_private(marker)
        if content is None:
            return
        metadata = marker.lstat()
        if os.name == "posix" and (metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077):
            raise ValueError("private account state required")
        state = json.loads(content)
        if (not isinstance(state, dict)
                or set(state) not in ({"generation", "logged_out"}, {"generation", "logged_out", "account_id_hash"})
                or not isinstance(state.get("generation"), str)
                or re.fullmatch(r"[0-9a-f]{16,128}", state["generation"]) is None
                or type(state.get("logged_out")) is not bool):
            raise ValueError("invalid account state")
        expected = state.get("account_id_hash")
        if expected is not None and (not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{64}", expected) is None):
            raise ValueError("invalid account identity")
        if state["logged_out"]:
            raise _AccountError("codex_account_logged_out")
        if expected is not None:
            actual = _read_private(home / "auth.json")
            if actual is None or _account_id_hash(actual) != expected:
                raise _AccountError("codex_account_credentials_changed")
    except _AccountError:
        raise
    except Exception as error:
        raise _AccountError("codex_account_credentials_invalid") from error


class CodexAccountService:
    def __init__(self, root: DataRoot):
        self.root = root
        self._lock = threading.RLock()
        self._closed = False
        self._login = None
        self._login_rpc: _Rpc | None = None
        self._staging: DataRoot | None = None

    def _check_home(self) -> None:
        try:
            if (not self.root.codex_home.is_dir()
                    or self.root.codex_home.resolve(strict=True) != self.root.codex_home.absolute()):
                raise _AccountError("codex_account_credentials_invalid")
            _read_private(self.root.codex_home / "auth.json")
        except OSError as error:
            raise _AccountError("codex_account_credentials_invalid") from error

    def _account(self) -> dict | None:
        self._check_home()
        try:
            ensure_codex_account_current(self.root.codex_home)
        except _AccountError as error:
            if error.code == "codex_account_logged_out":
                return None
            raise
        rpc = _Rpc(self.root.validated_codex_cli_executable(CODEX_LOCKED_VERSION), self.root)
        try:
            account = _public_account(rpc.request("account/read", {"refreshToken": False}))
            ensure_codex_account_current(self.root.codex_home)
            return account
        finally:
            rpc.close()

    def read(self) -> dict:
        with self._lock:
            account = None
            error = None
            try:
                if self._closed:
                    raise _AccountError("codex_account_service_closed")
                account = self._account()
            except _AccountError as failure:
                error = failure.code
            except Exception:
                error = "codex_account_provider_unavailable"
            return {"status": "unavailable" if error else "authenticated" if account else "unauthenticated",
                    "account": account, "login": None if self._login is None else dict(self._login),
                    "error_code": error}

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._login is not None and self._login["status"] == "pending":
                self._finish("cancelled")
            self._dispose_login()

    def _finish(self, status: str, error: str | None = None) -> None:
        self._login.update(status=status, verification_url=None, user_code=None, error_code=error)

    def cancel_login(self, login_id: str) -> dict:
        with self._lock:
            if self._login is None or login_id != self._login["id"]:
                result = self.read()
                result["error_code"] = "codex_account_login_not_found"
                return result
            if self._login["status"] == "pending":
                # Revoke promotion before the provider can deliver a late completion.
                self._finish("cancelled")
                try:
                    self._login_rpc.request("account/login/cancel", {"loginId": login_id})
                except Exception:
                    pass
                self._dispose_login()
            return self.read()

    def logout(self) -> dict:
        with self._lock:
            if self._closed:
                return self.read()
            if self._login is not None and self._login["status"] == "pending":
                self.cancel_login(self._login["id"])
            try:
                self._check_home()
                auth = self.root.codex_home / "auth.json"
                _read_private(auth)
                # This explicit local choice survives daemon restart and cannot
                # be undone by the deployment's initial credential template.
                _write_private(self.root.codex_home / "account-state.json", json.dumps({
                    "generation": secrets.token_hex(16), "logged_out": True}).encode())
                rpc = None
                try:
                    rpc = _Rpc(self.root.validated_codex_cli_executable(CODEX_LOCKED_VERSION), self.root)
                    rpc.request("account/logout", {})
                except Exception:
                    # Native transport failure must not undo local logout.
                    pass
                finally:
                    if rpc is not None:
                        rpc.close()
                auth.unlink(missing_ok=True)
                return {"status": "unauthenticated", "account": None,
                        "login": None if self._login is None else dict(self._login), "error_code": None}
            except Exception as failure:
                result = self.read()
                result["error_code"] = (failure.code if isinstance(failure, _AccountError)
                                        else "codex_account_logout_failed")
                return result

    def _dispose_login(self) -> None:
        rpc, staging = self._login_rpc, self._staging
        self._login_rpc = self._staging = None
        if rpc is not None:
            rpc.close()
        if staging is not None:
            # Delete only the exact private temporary root created by this instance.
            base = self.root.run.resolve()
            target = staging.root.resolve()
            if (target.parent == base and target.name.startswith("codex-account-login-")
                    and not staging.root.is_symlink()):
                shutil.rmtree(staging.root)

    def start_login(self) -> dict:
        with self._lock:
            if self._closed or (self._login is not None and self._login["status"] == "pending"):
                return self.read()
            self._dispose_login()
            try:
                self._check_home()
                _private_directory(self.root.run)
                self._staging = DataRoot(self.root.run / ("codex-account-login-" + secrets.token_hex(16)))
                for directory in self._staging.codex_environment.values():
                    if directory:
                        _private_directory(Path(directory))
                rpc = self._login_rpc = _Rpc(
                    self.root.validated_codex_cli_executable(CODEX_LOCKED_VERSION), self._staging)
                response = rpc.request("account/login/start", {"type": "chatgptDeviceCode"})
                if not isinstance(response, dict):
                    raise _AccountError("codex_account_invalid_response")
                login_id, url, code = (response.get(k) for k in ("loginId", "verificationUrl", "userCode"))
                if (not isinstance(login_id, str) or not 1 <= len(login_id) <= 160
                        or any(ord(c) < 32 for c in login_id)
                        or not isinstance(url, str) or len(url) > 2048
                        or not isinstance(code, str) or re.fullmatch(r"[A-Za-z0-9-]{4,64}", code) is None):
                    raise _AccountError("codex_account_invalid_response")
                parsed = urlsplit(url)
                if (parsed.scheme != "https" or parsed.netloc != "auth.openai.com"
                        or parsed.path != "/codex/device" or parsed.query or parsed.fragment):
                    raise _AccountError("codex_account_login_url_invalid")
                self._login = {"id": login_id, "status": "pending", "verification_url": url,
                               "user_code": code, "error_code": None, "expires_at": time.time() + _LOGIN_LIFETIME}
                threading.Thread(target=self._watch_login, args=(rpc, self._staging, time.monotonic() + _LOGIN_LIFETIME),
                                 daemon=True).start()
            except Exception as failure:
                code = failure.code if isinstance(failure, _AccountError) else "codex_account_login_failed"
                self._login = {"id": secrets.token_hex(16), "status": "failed", "verification_url": None,
                               "user_code": None, "error_code": code, "expires_at": None}
                self._dispose_login()
            return self.read()

    def _watch_login(self, rpc: _Rpc, staging: DataRoot, deadline: float) -> None:
        while True:
            try:
                notification = rpc.notifications.get(timeout=.1)
            except queue.Empty:
                notification = None
            with self._lock:
                if self._closed or rpc is not self._login_rpc or self._login["status"] != "pending":
                    return
                if time.monotonic() >= deadline:
                    self._finish("failed", "codex_account_login_expired")
                    self._dispose_login()
                    return
                if isinstance(notification, _AccountError):
                    self._finish("failed", notification.code)
                    self._dispose_login()
                    return
                if not isinstance(notification, dict) or notification.get("loginId") != self._login["id"]:
                    continue
                if notification.get("success") is not True:
                    self._finish("failed", "codex_account_login_failed")
                else:
                    try:
                        account = _public_account(rpc.request("account/read", {"refreshToken": False}))
                        if account is None or account["type"] != "chatgpt":
                            raise _AccountError("codex_account_login_failed")
                        content = _read_private(staging.codex_home / "auth.json")
                        if content is None or not isinstance(json.loads(content), dict):
                            raise _AccountError("codex_account_credentials_invalid")
                        self._activate(content)
                        self._finish("completed")
                    except Exception as failure:
                        code = failure.code if isinstance(failure, _AccountError) else "codex_account_login_failed"
                        self._finish("failed", code)
                self._dispose_login()
                return

    def _activate(self, content: bytes) -> None:
        self._check_home()
        auth, marker = (self.root.codex_home / name for name in ("auth.json", "account-state.json"))
        previous_auth, previous_marker = _read_private(auth), _read_private(marker)
        state = json.dumps({"generation": secrets.token_hex(16), "logged_out": False,
                            "account_id_hash": _account_id_hash(content)}).encode()
        try:
            _write_private(auth, content)
            _write_private(marker, state)
        except Exception:
            for path, original in ((auth, previous_auth), (marker, previous_marker)):
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    _write_private(path, original)
            raise
