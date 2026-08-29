from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import text

from meta_research.database import Database


BOOTSTRAP_TTL_SECONDS = 300
BROWSER_GRANT_TTL_SECONDS = 30
SESSION_TTL_SECONDS = 12 * 60 * 60
GrantKind = Literal["token", "browser"]


@dataclass(frozen=True)
class AuthSession:
    token: str
    csrf_token: str
    expires_at: float


class Authentication:
    """Loopback session infrastructure, distinct from capability authorization."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def issue_bootstrap_token(self) -> str:
        return self._issue_grant("token", BOOTSTRAP_TTL_SECONDS)

    def issue_browser_grant(self) -> str:
        return self._issue_grant("browser", BROWSER_GRANT_TTL_SECONDS)

    def issue_session(self) -> AuthSession:
        """Create a session after the caller has verified a trusted boundary."""

        now = time.time()
        with self._database.write() as connection:
            return self._create_session(connection, now)

    def exchange_bootstrap_token(self, token: str) -> AuthSession | None:
        return self._exchange_grant(token, "token")

    def exchange_browser_grant(self, grant: str) -> AuthSession | None:
        return self._exchange_grant(grant, "browser")

    def _issue_grant(self, grant_kind: GrantKind, ttl_seconds: int) -> str:
        grant = secrets.token_urlsafe(32)
        now = time.time()
        with self._database.write() as connection:
            self._remove_expired_grants(connection, now)
            connection.execute(
                text(
                    "INSERT INTO auth_bootstrap_grants "
                    "(token_hash, grant_kind, created_at, expires_at, consumed_at) "
                    "VALUES (:token_hash, :grant_kind, :created_at, :expires_at, NULL)"
                ),
                {
                    "token_hash": _digest(grant),
                    "grant_kind": grant_kind,
                    "created_at": now,
                    "expires_at": now + ttl_seconds,
                },
            )
        return grant

    def _exchange_grant(
        self, grant: str, grant_kind: GrantKind
    ) -> AuthSession | None:
        if not grant:
            return None
        now = time.time()
        token_hash = _digest(grant)
        with self._database.write() as connection:
            consumed = connection.execute(
                text(
                    "UPDATE auth_bootstrap_grants SET consumed_at = :now "
                    "WHERE token_hash = :token_hash AND grant_kind = :grant_kind "
                    "AND consumed_at IS NULL AND expires_at > :now"
                ),
                {
                    "now": now,
                    "token_hash": token_hash,
                    "grant_kind": grant_kind,
                },
            )
            if consumed.rowcount != 1:
                return None
            return self._create_session(connection, now)

    def browser_grant_was_consumed(self, grant: str) -> bool:
        with self._database.read() as connection:
            consumed_at = connection.execute(
                text(
                    "SELECT consumed_at FROM auth_bootstrap_grants "
                    "WHERE token_hash = :token_hash AND grant_kind = 'browser'"
                ),
                {"token_hash": _digest(grant)},
            ).scalar_one_or_none()
        return consumed_at is not None

    def session_is_valid(self, token: str | None) -> bool:
        if not token:
            return False
        now = time.time()
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT 1 FROM auth_sessions WHERE session_hash = :session_hash "
                    "AND revoked_at IS NULL AND expires_at > :now"
                ),
                {"session_hash": _digest(token), "now": now},
            ).first()
        return row is not None

    def csrf_matches(self, token: str | None, csrf_token: str | None) -> bool:
        if not token or not csrf_token:
            return False
        now = time.time()
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT 1 FROM auth_sessions WHERE session_hash = :session_hash "
                    "AND csrf_hash = :csrf_hash AND revoked_at IS NULL "
                    "AND expires_at > :now"
                ),
                {
                    "session_hash": _digest(token),
                    "csrf_hash": _digest(csrf_token),
                    "now": now,
                },
            ).first()
        return row is not None

    def revoke_session(self, token: str, csrf_token: str) -> bool:
        now = time.time()
        with self._database.write() as connection:
            revoked = connection.execute(
                text(
                    "UPDATE auth_sessions SET revoked_at = :now "
                    "WHERE session_hash = :session_hash AND csrf_hash = :csrf_hash "
                    "AND revoked_at IS NULL AND expires_at > :now"
                ),
                {
                    "now": now,
                    "session_hash": _digest(token),
                    "csrf_hash": _digest(csrf_token),
                },
            )
        return revoked.rowcount == 1

    @staticmethod
    def control_key_matches(candidate: str | None, expected: str) -> bool:
        return candidate is not None and hmac.compare_digest(candidate, expected)

    def _create_session(self, connection, now: float) -> AuthSession:
        token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(24)
        expires_at = now + SESSION_TTL_SECONDS
        connection.execute(
            text(
                "INSERT INTO auth_sessions "
                "(session_hash, csrf_hash, created_at, expires_at, revoked_at) "
                "VALUES (:session_hash, :csrf_hash, :created_at, :expires_at, NULL)"
            ),
            {
                "session_hash": _digest(token),
                "csrf_hash": _digest(csrf_token),
                "created_at": now,
                "expires_at": expires_at,
            },
        )
        return AuthSession(token=token, csrf_token=csrf_token, expires_at=expires_at)

    @staticmethod
    def _remove_expired_grants(connection, now: float) -> None:
        connection.execute(
            text("DELETE FROM auth_bootstrap_grants WHERE expires_at <= :now"),
            {"now": now},
        )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
