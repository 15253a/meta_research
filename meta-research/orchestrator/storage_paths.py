"""Relocation-safe resolution for immutable DB-registered file references.

The core schema intentionally keeps the original ``checkpoint.path`` / ``execution_log.ref``
bytes append-only.  A SQLite restore therefore records the source-root lineage instead of
rewriting those rows.  Consumers map an old absolute root to the current work root and still let
the artifact capability layer perform the final fd/path-binding checks.
"""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple


_MAX_RECEIPT_BYTES = 64 * 1024
_CYCLE_RE = re.compile(r"^c[1-9][0-9]*$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class RegisteredPathError(RuntimeError):
    """A stored path cannot be mapped into the current work-root authority."""


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def _read_restore_receipt(work_root: Path) -> Dict[str, Any] | None:
    receipt_path = work_root / "restore.json"
    if not os.path.lexists(receipt_path):
        return None
    try:
        fd = os.open(
            receipt_path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0))
    except OSError as error:
        raise RegisteredPathError("restore path-lineage receipt 不可打开") from error
    try:
        info = os.fstat(fd)
        path_info = receipt_path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o400
                or (info.st_dev, info.st_ino) != (path_info.st_dev, path_info.st_ino)
                or info.st_size < 2 or info.st_size > _MAX_RECEIPT_BYTES):
            raise RegisteredPathError("restore path-lineage receipt authority 非法")
        raw = bytearray()
        while len(raw) < info.st_size:
            block = os.read(fd, info.st_size - len(raw))
            if not block:
                raise RegisteredPathError("restore path-lineage receipt 提前 EOF")
            raw.extend(block)
    finally:
        os.close(fd)
    try:
        value = json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RegisteredPathError("restore path-lineage receipt JSON 损坏") from error
    required = {
        "schema", "scope", "continuation_mode", "publication_contract",
        "source_work_root", "source_cycle", "source_manifest_sha256", "backup",
    }
    if (not isinstance(value, dict) or _canonical(value) != bytes(raw)
            or set(value) not in (required, required | {"registered_path_roots"})
            or value.get("schema") != "meta-research-storage-restore/v1"
            or value.get("scope") != "sqlite_truth_only"
            or value.get("publication_contract")
            != "atomic_noreplace_or_lease_fenced_ready"
            or not isinstance(value.get("source_work_root"), str)
            or not _CYCLE_RE.fullmatch(str(value.get("source_cycle", "")))
            or not _HASH_RE.fullmatch(str(value.get("source_manifest_sha256", "")))
            or not isinstance(value.get("backup"), dict)):
        raise RegisteredPathError("restore path-lineage receipt 身份非法")
    return value


def _canonical_absolute(value: Any, *, label: str) -> Path:
    if (not isinstance(value, str) or not value or "\x00" in value
            or not Path(value).is_absolute()):
        raise RegisteredPathError(f"{label} 非 canonical absolute path")
    normalized = Path(os.path.abspath(value))
    if str(normalized) != value:
        raise RegisteredPathError(f"{label} 非 canonical absolute path")
    return normalized


def _reject_nested(roots: Iterable[Path]) -> None:
    ordered = list(roots)
    for position, left in enumerate(ordered):
        for right in ordered[position + 1:]:
            if left == right or left in right.parents or right in left.parents:
                raise RegisteredPathError("restore path-lineage roots 重复/嵌套")


def registered_path_roots(work_root: Path | str) -> Tuple[Path, ...]:
    """Return current root followed by every immutable historical source root."""
    requested = Path(os.path.abspath(os.fspath(work_root)))
    try:
        current = requested.resolve(strict=True)
    except OSError as error:
        raise RegisteredPathError("current work_root 不可解析") from error
    if requested != current:
        raise RegisteredPathError("current work_root 含 symlink/alias")
    receipt = _read_restore_receipt(current)
    if receipt is None:
        return (current,)
    source = _canonical_absolute(
        receipt["source_work_root"], label="source_work_root")
    raw_lineage = receipt.get("registered_path_roots")
    if raw_lineage is None:
        historical = [source]
    else:
        if (not isinstance(raw_lineage, list) or not raw_lineage
                or raw_lineage[0] != str(source)):
            raise RegisteredPathError("registered_path_roots 与 source_work_root 不闭合")
        historical = [
            _canonical_absolute(value, label="registered_path_roots")
            for value in raw_lineage
        ]
    roots = [current, *historical]
    _reject_nested(roots)
    return tuple(roots)


def validate_restore_target_lineage(
        work_root: Path | str, target: Path | str) -> Tuple[Path, ...]:
    """Reject a restore target equal to, inside, or containing any lineage root.

    The check is lexical because a new target need not exist yet.  Callers must first
    canonicalize the target parent so aliases cannot bypass the comparison.
    """
    candidate = Path(os.path.abspath(os.fspath(target)))
    roots = registered_path_roots(work_root)
    for root in roots:
        if (candidate == root or candidate in root.parents
                or root in candidate.parents):
            raise RegisteredPathError(
                "restore target 与 registered path-lineage root 不得相等/嵌套")
    return roots


def resolve_registered_path(work_root: Path | str, stored_ref: Path | str) -> Path:
    """Map a relative/current/historical DB ref into the current work root.

    This function is lexical by design.  Callers must subsequently open with ``O_NOFOLLOW`` or
    the artifact-capability helpers so a symlink swap cannot turn lineage mapping into authority.
    """
    roots = registered_path_roots(work_root)
    current = roots[0]
    if not isinstance(stored_ref, (str, os.PathLike)):
        raise RegisteredPathError("registered ref 类型非法")
    raw_text = os.fspath(stored_ref)
    if not isinstance(raw_text, str) or not raw_text or "\x00" in raw_text:
        raise RegisteredPathError("registered ref 为空/非法")
    raw = Path(raw_text)
    if not raw.is_absolute():
        candidate = Path(os.path.abspath(os.fspath(current / raw)))
        try:
            relative = candidate.relative_to(current)
        except ValueError as error:
            raise RegisteredPathError("relative registered ref 越出 work_root") from error
        if not relative.parts:
            raise RegisteredPathError("registered ref 不得指向 work_root 本身")
        return candidate
    normalized = Path(os.path.abspath(raw_text))
    if str(normalized) != raw_text:
        raise RegisteredPathError("absolute registered ref 非 canonical path")
    matches = []
    for root in roots:
        try:
            relative = normalized.relative_to(root)
        except ValueError:
            continue
        if not relative.parts:
            raise RegisteredPathError("registered ref 不得指向 work_root 本身")
        matches.append(current / relative)
    # Legacy/non-restored roots historically allowed exact absolute external refs.
    # Keep runtime compatibility there; the registered-asset archive separately
    # requires every mirrored asset to be relative to its work root.  Once a
    # restore lineage exists, falling back to the old source path would silently
    # defeat hydration and is therefore forbidden.
    if not matches and len(roots) == 1:
        return normalized
    if len(matches) != 1:
        raise RegisteredPathError("registered ref 越出/歧义于 path-lineage authority")
    return matches[0]
