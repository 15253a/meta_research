from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from urllib.parse import parse_qsl, urlsplit


_SECRET_KEYS = {
    "password",
    "passwd",
    "passphrase",
    "cookie",
    "cookies",
    "session_cookie",
    "sessionid",
    "sid",
    "set_cookie",
    "otp",
    "one_time_password",
    "secret",
    "client_secret",
    "access_token",
    "refresh_token",
    "session_token",
    "id_token",
    "auth_token",
    "api_token",
    "personal_access_token",
    "token",
    "api_key",
    "secret_key",
    "access_key",
    "secret_access_key",
    "private_key",
    "ssh_private_key",
    "credential",
    "credentials",
    "authorization_header",
}
_SECRET_KEY_SUFFIXES = (
    "_password",
    "_passphrase",
    "_secret",
    "_token",
    "_api_key",
    "_secret_key",
    "_access_key",
    "_private_key",
    "_credential",
    "_credentials",
)
_ASSIGNMENT = re.compile(
    r"(?ix)"
    r"(?:password|passwd|passphrase|cookie|sessionid|sid|set[\s_-]*cookie|"
    r"otp|one[\s_-]*time[\s_-]*password|"
    r"client[\s_-]*secret|private[\s_-]*key|api[\s_-]*(?:key|token)|"
    r"access[\s_-]*(?:key|token)|refresh[\s_-]*token|session[\s_-]*token|"
    r"id[\s_-]*token|auth[\s_-]*token|personal[\s_-]*access[\s_-]*token|"
    r"token|credential(?:s)?)"
    r"[\"']?\s*[:=]\s*[\"']?\S+"
)
_BEARER = re.compile(r"(?i)\b(?:bearer|basic)\s+[a-z0-9._~+/=-]{8,}")
_NATURAL_CREDENTIAL = re.compile(
    r"(?ix)\b(?:my\s+|the\s+|here\s+is\s+the\s+)?(?:"
    r"password|passwd|passphrase|otp|one[\s_-]*time[\s_-]*password|"
    r"client[\s_-]*secret|aws[\s_-]*secret[\s_-]*access[\s_-]*key|"
    r"secret[\s_-]*access[\s_-]*key|private[\s_-]*key|"
    r"access[\s_-]*token|refresh[\s_-]*token|session[\s_-]*token|"
    r"auth[\s_-]*token|api[\s_-]*(?:key|token)|credential(?:s)?)"
    r"\s+(?:is\s+)?[\"']?[^\s\"']{4,}"
)
_COOKIE_VALUE = re.compile(
    r"(?ix)\bcookie\s+(?:is\s+)?(?:sessionid|session|sid|auth|token)"
    r"(?:\s*[:=]\s*|\s+)[^\s;]{3,}"
)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----", re.IGNORECASE
)
_KNOWN_TOKEN = re.compile(
    r"(?:"
    r"github_pat_[A-Za-z0-9_]{12,}|"
    r"gh[pousr]_[A-Za-z0-9_]{12,}|"
    r"xox[baprs]-[A-Za-z0-9-]{12,}|"
    r"sk-[A-Za-z0-9_-]{16,}|"
    r"AKIA[A-Z0-9]{16}|"
    r"AIza[0-9A-Za-z_-]{20,}|"
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r")"
)
_SENSITIVE_URL_QUERY_NAMES = {
    "access_token",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "expires",
    "googleaccessid",
    "key",
    "password",
    "se",
    "secret",
    "session",
    "sessionid",
    "set-cookie",
    "sig",
    "signature",
    "sid",
    "ske",
    "skoid",
    "sks",
    "skt",
    "sktid",
    "skv",
    "sp",
    "spr",
    "srt",
    "ss",
    "st",
    "sv",
    "token",
}


def contains_secret(value: object) -> bool:
    """Conservatively reject credentials before HC persists user-visible content."""

    if isinstance(value, Mapping):
        return any(
            _is_secret_key(str(key)) or contains_secret(item)
            for key, item in value.items()
        )
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return any(contains_secret(item) for item in value)
    return isinstance(value, str) and _contains_secret_text(value)


def _is_secret_key(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    return normalized in _SECRET_KEYS or normalized.endswith(_SECRET_KEY_SUFFIXES)


def _contains_secret_text(value: str) -> bool:
    return bool(
        _ASSIGNMENT.search(value)
        or _NATURAL_CREDENTIAL.search(value)
        or _COOKIE_VALUE.search(value)
        or _BEARER.search(value)
        or _PRIVATE_KEY.search(value)
        or _KNOWN_TOKEN.search(value)
        or _contains_credential_url(value)
    )


def _contains_credential_url(value: str) -> bool:
    for candidate in re.findall(r"[a-z][a-z0-9+.-]*://[^\s<>'\"]+", value, re.I):
        try:
            parsed = urlsplit(candidate.rstrip(".,;!?)"))
            query_names = {name.casefold() for name, _ in parse_qsl(parsed.query)}
        except (TypeError, ValueError):
            continue
        if (
            parsed.username is not None
            or parsed.password is not None
            or bool(query_names & _SENSITIVE_URL_QUERY_NAMES)
            or any(
                name.startswith(("x-amz-", "x-goog-")) for name in query_names
            )
        ):
            return True
    return False
