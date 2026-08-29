from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Protocol, cast


class CodexSessionLedgerReader(Protocol):
    """Read one exact append-only Codex session ledger."""

    def read(self, session_ref: str) -> tuple[dict[str, object], ...]: ...

    def verify_skill_package(self, skill_path: str, injected_body: str) -> str: ...


class CodexHomeLedgerReader:
    """Read ledgers only from the configured, non-symlink CODEX_HOME tree."""

    def __init__(self, codex_home: Path) -> None:
        if not codex_home.is_absolute() or codex_home.is_symlink():
            raise ValueError("codex home is not a trusted directory")
        self._codex_home = codex_home.resolve()

    def read(self, session_ref: str) -> tuple[dict[str, object], ...]:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", session_ref):
            raise OSError("session reference invalid")
        candidates: list[tuple[Path, tuple[dict[str, object], ...]]] = []
        for relative in ("sessions", "archived_sessions"):
            directory = self._codex_home / relative
            if not directory.exists():
                continue
            if directory.is_symlink() or not directory.is_dir():
                raise OSError("codex ledger directory invalid")
            for path in directory.rglob(f"*{session_ref}*.jsonl"):
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or not _is_non_symlink_codex_home_descendant(
                        path, self._codex_home
                    )
                ):
                    raise OSError("session ledger path invalid")
                try:
                    resolved = path.resolve(strict=True)
                    resolved.relative_to(self._codex_home)
                    records = _parse_jsonl(resolved.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, ValueError) as error:
                    raise OSError("session ledger invalid") from error
                if _ledger_session_id(records) != session_ref:
                    raise OSError("session ledger identity mismatch")
                candidates.append((resolved, records))
        if len(candidates) != 1:
            raise OSError("session ledger missing or ambiguous")
        return candidates[0][1]

    def verify_skill_package(self, skill_path: str, injected_body: str) -> str:
        path = Path(skill_path)
        if (
            not path.is_absolute()
            or not path.is_file()
            or not _is_non_symlink_codex_home_descendant(path, self._codex_home)
        ):
            raise OSError("child Skill package invalid")
        try:
            resolved = path.resolve(strict=True)
            package_bytes = resolved.read_bytes()
            package = package_bytes.decode("utf-8")
        except (OSError, ValueError) as error:
            raise OSError("child Skill package invalid") from error
        except UnicodeDecodeError as error:
            raise OSError("child Skill package invalid") from error
        if not any(
            candidate == package
            for candidate in _skill_body_without_wrapper_newline(injected_body)
        ):
            raise OSError("child Skill package content drift")
        return hashlib.sha256(package_bytes).hexdigest()


def _parse_jsonl(value: str) -> tuple[dict[str, object], ...]:
    events: list[dict[str, object]] = []
    for line in value.splitlines():
        if not line:
            raise ValueError("empty JSONL record")
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError("malformed JSONL record") from error
        if not isinstance(event, dict):
            raise ValueError("JSONL record is not an object")
        events.append(cast(dict[str, object], event))
    return tuple(events)


def _ledger_session_id(records: tuple[dict[str, object], ...]) -> str | None:
    identifiers = [
        payload.get("id")
        for record in records
        if record.get("type") == "session_meta"
        and isinstance((payload := record.get("payload")), dict)
        and isinstance(payload.get("id"), str)
    ]
    return cast(str, identifiers[0]) if len(identifiers) == 1 else None


def _is_non_symlink_codex_home_descendant(path: Path, codex_home: Path) -> bool:
    try:
        relative = path.relative_to(codex_home)
    except ValueError:
        return False
    current = codex_home
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return False
    return True


def _skill_body_without_wrapper_newline(body: str) -> tuple[str, ...]:
    candidates = [body]
    if body.startswith("\n"):
        candidates.append(body[1:])
    if body.endswith("\n"):
        candidates.append(body[:-1])
    if body.startswith("\n") and body.endswith("\n"):
        candidates.append(body[1:-1])
    return tuple(dict.fromkeys(candidates))
