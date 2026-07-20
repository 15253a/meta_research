"""Safe, durable references to local files selected by the Web application.

The first local-machine release may let a user type a local data/reference
path instead of uploading every byte through the browser.  This module keeps
that convenience behind a deliberately small boundary:

* browser-facing values are summaries and never contain an absolute path;
* paths are constrained to deployment-owned allow roots and traversed with
  ``openat``/``O_NOFOLLOW`` semantics;
* attachment/preflight only inspect metadata; publication verification is the
  first operation which reads every file and computes SHA-256;
* idempotency bindings are canonical, fsync'd records and survive restarts.

``preflight_manifest`` and ``verified_manifest`` are explicitly server-only.
Their return values contain local paths and must never be serialized to an
untrusted client.  Use ``public_preflight`` for HTTP responses.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple


_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")
_KIND_VALUES = frozenset({"dataset", "references"})
_VERSION = 1
_RECEIPT_VERSION = 1
_MAX_JSON_BYTES = 1024 * 1024
_MAX_INPUT_PATH_BYTES = 16 * 1024
_MAX_RELATIVE_PATH_BYTES = 16 * 1024
_MAX_LABEL_CHARS = 200
_MAX_SOURCES_PER_DRAFT = 64
_MAX_FILES = 100_000
_MAX_ENTRIES = 200_000
_MAX_DEPTH = 64
_MAX_FILE_BYTES = 64 * 1024 ** 3
_MAX_TOTAL_BYTES = 256 * 1024 ** 3
_HASH_CHUNK_BYTES = 1024 * 1024


class LocalSourceError(RuntimeError):
    """A local source cannot be safely attached or enumerated."""


class LocalSourceConflictError(LocalSourceError, ValueError):
    """An idempotency key is already bound to another request."""


class LocalSourceCorruptError(LocalSourceError):
    """The private registry cannot prove its own durable identity."""


class LocalSourceChangedError(LocalSourceError):
    """A selected source changed while it was being enumerated/read."""


def _canonical(value: Any) -> bytes:
    try:
        return (json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as error:
        raise ValueError("local source value cannot be canonicalized") from error


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_hex32(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _HEX32_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be 32 lowercase hexadecimal characters")
    return value


def _validate_kind(value: object) -> str:
    if not isinstance(value, str) or value not in _KIND_VALUES:
        raise ValueError("kind must be 'dataset' or 'references'")
    return value


def _fsync_dir(path: Path) -> None:
    flags = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
             | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_new(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
             | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    fd = os.open(path, flags, mode)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        os.close(fd)


def _directory_info(path: Path, *, owner: int, label: str) -> os.stat_result:
    try:
        info = os.lstat(path)
    except OSError as error:
        raise LocalSourceCorruptError(f"{label} is not readable") from error
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != owner or stat.S_IMODE(info.st_mode) != 0o700):
        raise LocalSourceCorruptError(f"{label} has an unsafe owner/type/mode")
    return info


def _open_private_file(path: Path, *, owner: int,
                       label: str, writable: bool = False) -> Tuple[int, os.stat_result]:
    flags = (os.O_RDWR if writable else os.O_RDONLY)
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise LocalSourceCorruptError(f"{label} cannot be safely opened") from error
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise LocalSourceCorruptError(
                f"{label} has an unsafe owner/type/link/mode")
        return fd, info
    except BaseException:
        os.close(fd)
        raise


def _read_canonical(path: Path, *, owner: int, label: str) -> Dict[str, Any]:
    fd, before = _open_private_file(path, owner=owner, label=label)
    try:
        if not 2 <= before.st_size <= _MAX_JSON_BYTES:
            raise LocalSourceCorruptError(f"{label} has an invalid size")
        chunks: List[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                raise LocalSourceCorruptError(f"{label} was truncated")
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(fd)
        if _identity(after) != _identity(before):
            raise LocalSourceCorruptError(f"{label} changed while being read")
    finally:
        os.close(fd)

    def unique(pairs):  # noqa: ANN001 - json object hook protocol
        result: Dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise LocalSourceCorruptError(f"{label} contains a duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=unique,
            parse_constant=lambda token: (_ for _ in ()).throw(
                LocalSourceCorruptError(
                    f"{label} contains a non-finite value: {token}")))
    except LocalSourceCorruptError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LocalSourceCorruptError(f"{label} is not strict UTF-8 JSON") from error
    if not isinstance(value, dict) or raw != _canonical(value):
        raise LocalSourceCorruptError(f"{label} is not canonical JSON")
    return value


def _identity(info: os.stat_result) -> Tuple[int, ...]:
    """Fields which must remain stable while a source is observed.

    ``atime`` is intentionally excluded because merely reading a file may
    update it.  uid/gid/mode/link count are included so a same-inode metadata
    privilege change is not silently accepted.
    """

    return (
        info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
        info.st_uid, info.st_gid, info.st_size, info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _binding_identity(info: os.stat_result) -> Tuple[int, ...]:
    """Stable identity for an allow-root directory, excluding child churn."""

    return (
        # A directory's link count changes when direct child directories are
        # added/removed, so it is content metadata just like size/mtime here.
        info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
    )


def _identity_dict(info: os.stat_result) -> Dict[str, int]:
    return {
        "device": info.st_dev,
        "inode": info.st_ino,
        "mode": stat.S_IMODE(info.st_mode),
        "links": info.st_nlink,
        "uid": info.st_uid,
        "gid": info.st_gid,
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "ctime_ns": info.st_ctime_ns,
    }


def _safe_label(path: Path) -> str:
    raw = path.name or "Local filesystem"
    label = "".join(
        char if ord(char) >= 0x20 and ord(char) != 0x7F else "�"
        for char in raw)
    return label[:_MAX_LABEL_CHARS] or "Local source"


def _relative_name(parts: Sequence[str]) -> str:
    # File names are placed into JSON and later interpreted as POSIX-relative
    # paths.  Backslashes would be ambiguous for cross-platform consumers.
    for part in parts:
        try:
            encoded = part.encode("utf-8")
        except UnicodeEncodeError as error:
            raise LocalSourceError("source contains a non-UTF-8 file name") from error
        if (not encoded or part in {".", ".."} or "/" in part
                or "\\" in part or "\x00" in part):
            raise LocalSourceError("source contains an unsafe file name")
    relative = "/".join(parts)
    if len(relative.encode("utf-8")) > _MAX_RELATIVE_PATH_BYTES:
        raise LocalSourceError("source contains a path longer than the path budget")
    return relative


def _assert_same(actual: os.stat_result, expected: os.stat_result,
                 *, label: str) -> None:
    if _identity(actual) != _identity(expected):
        raise LocalSourceChangedError(f"{label} changed during inspection")


class LocalSourceRegistry:
    """Private registry for local dataset/reference directory attachments.

    ``allowed_roots=None`` permits only the process startup working directory.
    A local-machine deployment which intentionally exposes the full local
    filesystem must opt in explicitly with ``allowed_roots=["/"]``.
    """

    def __init__(self, root: Path | str,
                 allowed_roots: Optional[Sequence[Path | str]] = None):
        self.owner = os.geteuid()
        self.service_cwd = Path(os.getcwd())
        supplied = Path(os.path.abspath(os.fspath(root)))
        if not os.path.lexists(supplied):
            try:
                supplied.mkdir(parents=True, mode=0o700)
            except FileExistsError:
                pass
        try:
            info = os.lstat(supplied)
        except OSError as error:
            raise ValueError("local source registry root is not readable") from error
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or os.path.realpath(supplied) != str(supplied)
                or info.st_uid != self.owner or info.st_mode & 0o022):
            raise ValueError(
                "local source registry root must be canonical, private, and owner-controlled")
        os.chmod(supplied, 0o700)
        self.root = supplied

        roots = [self.service_cwd] if allowed_roots is None else list(allowed_roots)
        if not roots:
            raise ValueError("allowed_roots must not be empty")
        normalized: List[Tuple[Path, Tuple[int, ...]]] = []
        for item in roots:
            if isinstance(item, (bytes, bytearray)):
                raise ValueError("allowed root must be a text path")
            text = os.path.expanduser(os.fspath(item))
            if not isinstance(text, str) or not text or "\x00" in text:
                raise ValueError("allowed root must be a non-empty text path")
            path = Path(os.path.abspath(text))
            try:
                root_info = os.lstat(path)
            except OSError as error:
                raise ValueError("allowed root is not readable") from error
            if (not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode)
                    or os.path.realpath(path) != str(path)):
                raise ValueError("allowed root must be a canonical non-symlink directory")
            normalized.append((path, _binding_identity(root_info)))
        # Longest roots first makes containment and descriptor traversal
        # deterministic when allow roots overlap.
        deduplicated = {str(path): (path, identity) for path, identity in normalized}
        self._allowed_roots = tuple(sorted(
            deduplicated.values(), key=lambda item: len(str(item[0])), reverse=True))

        self.attachments_dir = self.root / "attachments"
        self.requests_dir = self.root / "attach-requests"
        for path in (self.attachments_dir, self.requests_dir):
            if not os.path.lexists(path):
                try:
                    path.mkdir(mode=0o700)
                except FileExistsError:
                    pass
            _directory_info(path, owner=self.owner, label=path.name)
        self.lock_path = self.root / ".local-sources.lock"
        if not os.path.lexists(self.lock_path):
            try:
                _write_new(self.lock_path, b"local-sources-v1\n")
            except FileExistsError:
                pass
        _fsync_dir(self.attachments_dir)
        _fsync_dir(self.requests_dir)
        _fsync_dir(self.root)
        fd, _ = _open_private_file(
            self.lock_path, owner=self.owner, label="local source registry lock")
        os.close(fd)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        fd, opened = _open_private_file(
            self.lock_path, owner=self.owner,
            label="local source registry lock", writable=True)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            current = os.lstat(self.lock_path)
            if (_identity(current) != _identity(opened)
                    or current.st_nlink != 1):
                raise LocalSourceCorruptError("registry lock binding changed")
            self._validate_registry_locked()
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _validate_registry_locked(self) -> None:
        _directory_info(self.root, owner=self.owner, label="registry root")
        _directory_info(
            self.attachments_dir, owner=self.owner, label="attachments")
        _directory_info(self.requests_dir, owner=self.owner, label="attach-requests")
        for directory, label in (
                (self.attachments_dir, "attachment"),
                (self.requests_dir, "attach receipt")):
            for entry in directory.iterdir():
                if entry.suffix != ".json" or _HEX32_RE.fullmatch(entry.stem) is None:
                    raise LocalSourceCorruptError(
                        f"{directory.name} contains an unknown entry")
                fd, _ = _open_private_file(
                    entry, owner=self.owner, label=f"{label} {entry.stem}")
                os.close(fd)

    def _normalize_source(self, source_path: Path | str) -> Tuple[Path, Path, Tuple[int, ...]]:
        if isinstance(source_path, (bytes, bytearray)):
            raise ValueError("source_path must be a text path")
        try:
            text = os.fspath(source_path)
        except TypeError as error:
            raise ValueError("source_path must be path-like") from error
        if not isinstance(text, str) or not text or "\x00" in text:
            raise ValueError("source_path must be a non-empty text path")
        try:
            if len(text.encode("utf-8")) > _MAX_INPUT_PATH_BYTES:
                raise ValueError("source_path is too long")
        except UnicodeEncodeError as error:
            raise ValueError("source_path must be valid UTF-8") from error
        expanded = os.path.expanduser(text)
        if expanded.startswith("~"):
            raise ValueError("source_path contains an unknown home directory")
        absolute = Path(os.path.abspath(
            expanded if os.path.isabs(expanded)
            else os.path.join(self.service_cwd, expanded)))
        # Never permit a source to contain, or live inside, the private ledger.
        if (self._contains(absolute, self.root)
                or self._contains(self.root, absolute)):
            raise LocalSourceError("source_path overlaps private application storage")
        for allowed, expected_identity in self._allowed_roots:
            if self._contains(allowed, absolute):
                return absolute, allowed, expected_identity
        raise LocalSourceError("source_path is outside the configured local roots")

    @staticmethod
    def _contains(parent: Path, child: Path) -> bool:
        try:
            return os.path.commonpath((str(parent), str(child))) == str(parent)
        except ValueError:
            return False

    @staticmethod
    def _record_path(directory: Path, identifier: str) -> Path:
        return directory / f"{identifier}.json"

    def _open_source(self, source: Path, allowed: Path,
                     allowed_identity: Tuple[int, ...]) -> Tuple[
                         int, os.stat_result, int, Optional[str]]:
        root_flags = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                      | getattr(os, "O_CLOEXEC", 0)
                      | getattr(os, "O_NOFOLLOW", 0))
        try:
            allowed_fd = os.open(allowed, root_flags)
        except OSError as error:
            raise LocalSourceError("configured local root cannot be opened") from error
        try:
            if _binding_identity(os.fstat(allowed_fd)) != allowed_identity:
                raise LocalSourceChangedError("configured local root changed")
            relative = source.relative_to(allowed)
            parts = relative.parts
            if not parts:
                source_fd = os.dup(allowed_fd)
                return source_fd, os.fstat(source_fd), allowed_fd, None
            current_fd = allowed_fd
            for index, component in enumerate(parts):
                is_last = index == len(parts) - 1
                try:
                    before = os.stat(
                        component, dir_fd=current_fd, follow_symlinks=False)
                except OSError as error:
                    raise LocalSourceError("selected local source is not readable") from error
                if stat.S_ISLNK(before.st_mode):
                    raise LocalSourceError("selected local source contains a symbolic link")
                if not is_last and not stat.S_ISDIR(before.st_mode):
                    raise LocalSourceError("selected local source has a non-directory ancestor")
                if stat.S_ISDIR(before.st_mode):
                    flags = root_flags
                elif stat.S_ISREG(before.st_mode):
                    flags = (os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
                             | getattr(os, "O_CLOEXEC", 0)
                             | getattr(os, "O_NOFOLLOW", 0))
                else:
                    raise LocalSourceError(
                        "selected local source is not a regular file or directory")
                try:
                    child_fd = os.open(component, flags, dir_fd=current_fd)
                except OSError as error:
                    raise LocalSourceError("selected local source cannot be safely opened") from error
                try:
                    opened = os.fstat(child_fd)
                    _assert_same(opened, before, label="selected local source")
                except BaseException:
                    os.close(child_fd)
                    raise
                if is_last:
                    # The caller owns both descriptors and uses parent_fd to
                    # prove that the directory entry remained bound.
                    return child_fd, opened, current_fd, component
                os.close(current_fd)
                current_fd = child_fd
            raise AssertionError("unreachable source traversal")
        except BaseException:
            # Before the first hop this is allowed_fd; afterwards ownership was
            # transferred to the currently open intermediate directory.
            try:
                os.close(current_fd if "current_fd" in locals() else allowed_fd)
            except OSError:
                pass
            raise

    def _scan(self, source: Path, allowed: Path,
              allowed_identity: Tuple[int, ...], *, verified: bool) -> Dict[str, Any]:
        source_fd, source_info, parent_fd, entry_name = self._open_source(
            source, allowed, allowed_identity)
        # parent_fd is allowed_fd when the source is the allow root.  Otherwise
        # it may be an intermediate fd.  source_fd is always distinct.
        files: List[Dict[str, Any]] = []
        counters = {"files": 0, "entries": 0, "bytes": 0}
        try:
            if stat.S_ISREG(source_info.st_mode):
                self._scan_file(
                    source_fd, source_info, (source.name,), files, counters,
                    verified=verified)
                source_type = "file"
                manifest_root = source.parent
            elif stat.S_ISDIR(source_info.st_mode):
                self._scan_directory(
                    source_fd, (), files, counters, depth=0,
                    verified=verified)
                source_type = "directory"
                manifest_root = source
            else:  # The descriptor check in _open_source should make this unreachable.
                raise LocalSourceError(
                    "selected local source is not a regular file or directory")
            source_after = os.fstat(source_fd)
            _assert_same(source_after, source_info, label="selected local source")
            if entry_name is None:
                rebound = os.lstat(allowed)
            else:
                rebound = os.stat(
                    entry_name, dir_fd=parent_fd, follow_symlinks=False)
            _assert_same(rebound, source_info, label="selected local source binding")
        except OSError as error:
            raise LocalSourceChangedError(
                "selected local source binding changed during inspection") from error
        finally:
            os.close(source_fd)
            os.close(parent_fd)
        return {
            "source_type": source_type,
            "source_path": str(source),
            "source_root": str(manifest_root),
            "root_identity": _identity_dict(source_info),
            "file_count": counters["files"],
            "total_bytes": counters["bytes"],
            "files": files,
        }

    def _scan_directory(self, directory_fd: int, parts: Tuple[str, ...],
                        files: List[Dict[str, Any]], counters: Dict[str, int],
                        *, depth: int, verified: bool) -> None:
        if depth > _MAX_DEPTH:
            raise LocalSourceError("local source exceeds the directory depth budget")
        before = os.fstat(directory_fd)
        if not stat.S_ISDIR(before.st_mode):
            raise LocalSourceChangedError("directory type changed during inspection")
        try:
            names = os.listdir(directory_fd)
        except OSError as error:
            raise LocalSourceError("local source directory cannot be enumerated") from error
        # Validate before sorting so surrogate-escaped names fail closed.
        for name in names:
            _relative_name((*parts, name))
        names.sort()
        for name in names:
            counters["entries"] += 1
            if counters["entries"] > _MAX_ENTRIES:
                raise LocalSourceError("local source exceeds the entry-count budget")
            try:
                observed = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError as error:
                raise LocalSourceChangedError(
                    "local source entry changed during inspection") from error
            if stat.S_ISLNK(observed.st_mode):
                raise LocalSourceError("local source contains a symbolic link")
            child_parts = (*parts, name)
            if stat.S_ISDIR(observed.st_mode):
                if depth >= _MAX_DEPTH:
                    raise LocalSourceError(
                        "local source exceeds the directory depth budget")
                flags = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                         | getattr(os, "O_CLOEXEC", 0)
                         | getattr(os, "O_NOFOLLOW", 0))
                try:
                    child_fd = os.open(name, flags, dir_fd=directory_fd)
                except OSError as error:
                    raise LocalSourceChangedError(
                        "local source directory changed during inspection") from error
                try:
                    opened = os.fstat(child_fd)
                    _assert_same(opened, observed, label="local source directory")
                    self._scan_directory(
                        child_fd, child_parts, files, counters,
                        depth=depth + 1, verified=verified)
                    after = os.fstat(child_fd)
                    _assert_same(after, observed, label="local source directory")
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(observed.st_mode):
                flags = (os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
                         | getattr(os, "O_CLOEXEC", 0)
                         | getattr(os, "O_NOFOLLOW", 0))
                try:
                    child_fd = os.open(name, flags, dir_fd=directory_fd)
                except OSError as error:
                    raise LocalSourceChangedError(
                        "local source file changed during inspection") from error
                try:
                    opened = os.fstat(child_fd)
                    _assert_same(opened, observed, label="local source file")
                    self._scan_file(
                        child_fd, opened, child_parts, files, counters,
                        verified=verified)
                finally:
                    os.close(child_fd)
            else:
                # FIFO/socket/device are rejected before opening, avoiding FIFO
                # hangs and device side effects.
                raise LocalSourceError(
                    "local source contains a socket, device, FIFO, or unsupported entry")
            try:
                rebound = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError as error:
                raise LocalSourceChangedError(
                    "local source entry disappeared during inspection") from error
            _assert_same(rebound, observed, label="local source entry binding")
        try:
            after_names = os.listdir(directory_fd)
        except OSError as error:
            raise LocalSourceChangedError(
                "local source directory changed during inspection") from error
        if sorted(after_names) != names:
            raise LocalSourceChangedError(
                "local source directory entries changed during inspection")
        _assert_same(
            os.fstat(directory_fd), before,
            label="local source directory metadata")

    def _scan_file(self, fd: int, info: os.stat_result,
                   parts: Tuple[str, ...], files: List[Dict[str, Any]],
                   counters: Dict[str, int], *, verified: bool) -> None:
        if not stat.S_ISREG(info.st_mode):
            raise LocalSourceChangedError("local source file type changed")
        size = info.st_size
        if size < 0 or size > _MAX_FILE_BYTES:
            raise LocalSourceError("local source exceeds the per-file size budget")
        counters["files"] += 1
        counters["bytes"] += size
        if counters["files"] > _MAX_FILES:
            raise LocalSourceError("local source exceeds the file-count budget")
        if counters["bytes"] > _MAX_TOTAL_BYTES:
            raise LocalSourceError("local source exceeds the total-size budget")
        relative = _relative_name(parts)
        digest: Optional[str] = None
        if verified:
            hasher = hashlib.sha256()
            consumed = 0
            while True:
                try:
                    chunk = os.read(fd, _HASH_CHUNK_BYTES)
                except OSError as error:
                    raise LocalSourceChangedError(
                        "local source file could not be completely read") from error
                if not chunk:
                    break
                consumed += len(chunk)
                if consumed > size:
                    raise LocalSourceChangedError(
                        "local source file grew during verification")
                hasher.update(chunk)
            if consumed != size:
                raise LocalSourceChangedError(
                    "local source file size changed during verification")
            digest = "sha256:" + hasher.hexdigest()
        after = os.fstat(fd)
        _assert_same(after, info, label="local source file")
        row: Dict[str, Any] = {
            "path": relative,
            "size": size,
            "identity": _identity_dict(info),
        }
        if verified:
            row["sha256"] = digest
        files.append(row)

    @staticmethod
    def _validate_record(value: object, *, source_id: Optional[str] = None) -> Dict[str, Any]:
        if not isinstance(value, Mapping):
            raise LocalSourceCorruptError("attachment record is not an object")
        expected = {
            "version", "source_id", "draft_id", "kind", "label",
            "source_path", "source_type", "attached_at", "file_count",
            "total_bytes",
        }
        if set(value) != expected or value.get("version") != _VERSION:
            raise LocalSourceCorruptError("attachment record has an invalid shape")
        try:
            sid = _validate_hex32(value.get("source_id"), label="source_id")
            _validate_hex32(value.get("draft_id"), label="draft_id")
            _validate_kind(value.get("kind"))
        except ValueError as error:
            raise LocalSourceCorruptError("attachment record identity is invalid") from error
        if source_id is not None and sid != source_id:
            raise LocalSourceCorruptError("attachment record filename is mismatched")
        if (not isinstance(value.get("label"), str) or not value["label"]
                or len(value["label"]) > _MAX_LABEL_CHARS
                or not isinstance(value.get("source_path"), str)
                or not os.path.isabs(value["source_path"])
                or os.path.normpath(value["source_path"]) != value["source_path"]
                or value.get("source_type") not in {"file", "directory"}
                or not isinstance(value.get("attached_at"), str)
                or not value["attached_at"]):
            raise LocalSourceCorruptError("attachment record fields are invalid")
        for key, maximum in (
                ("file_count", _MAX_FILES), ("total_bytes", _MAX_TOTAL_BYTES)):
            number = value.get(key)
            if (isinstance(number, bool) or not isinstance(number, int)
                    or not 0 <= number <= maximum):
                raise LocalSourceCorruptError("attachment record counters are invalid")
        return dict(value)

    def _read_record_locked(self, source_id: str) -> Dict[str, Any]:
        path = self._record_path(self.attachments_dir, source_id)
        if not os.path.lexists(path):
            raise LocalSourceCorruptError("attachment receipt points to a missing record")
        return self._validate_record(
            _read_canonical(
                path, owner=self.owner, label=f"attachment {source_id}"),
            source_id=source_id)

    def _read_receipt_locked(self, key: str) -> Optional[Dict[str, Any]]:
        path = self._record_path(self.requests_dir, key)
        if not os.path.lexists(path):
            return None
        value = _read_canonical(
            path, owner=self.owner, label=f"attach receipt {key}")
        expected = {
            "version", "idempotency_key", "request_sha256", "request",
            "record_sha256", "record",
        }
        if (set(value) != expected or value.get("version") != _RECEIPT_VERSION
                or value.get("idempotency_key") != key
                or not isinstance(value.get("request"), dict)
                or not isinstance(value.get("record"), dict)
                or value.get("request_sha256") != _sha256(_canonical(value["request"]))
                or value.get("record_sha256") != _sha256(_canonical(value["record"]))):
            raise LocalSourceCorruptError("attach receipt is invalid")
        record = self._validate_record(value["record"])
        if set(value["request"]) != {"draft_id", "kind", "source_path"}:
            raise LocalSourceCorruptError("attach receipt request shape is invalid")
        try:
            _validate_hex32(value["request"].get("draft_id"), label="draft_id")
            _validate_kind(value["request"].get("kind"))
        except ValueError as error:
            raise LocalSourceCorruptError("attach receipt request is invalid") from error
        if (not isinstance(value["request"].get("source_path"), str)
                or value["request"]["source_path"] != record["source_path"]
                or value["request"]["draft_id"] != record["draft_id"]
                or value["request"]["kind"] != record["kind"]):
            raise LocalSourceCorruptError("attach receipt binding is invalid")
        return value

    @staticmethod
    def _public(record: Mapping[str, Any], *, status: str = "attached",
                file_count: Optional[int] = None,
                total_bytes: Optional[int] = None) -> Dict[str, Any]:
        return {
            "source_id": record["source_id"],
            "label": record["label"],
            "kind": record["kind"],
            "file_count": record["file_count"] if file_count is None else file_count,
            "total_bytes": record["total_bytes"] if total_bytes is None else total_bytes,
            "status": status,
        }

    def attach(self, draft_id: object, kind: object,
               source_path: Path | str, idempotency_key: object, *,
               require_directory: bool = False) -> Dict[str, Any]:
        """Attach a source and return a path-free public summary."""

        if not isinstance(require_directory, bool):
            raise ValueError("require_directory must be bool")

        did = _validate_hex32(draft_id, label="draft_id")
        safe_kind = _validate_kind(kind)
        key = _validate_hex32(idempotency_key, label="idempotency_key")
        source, allowed, allowed_identity = self._normalize_source(source_path)
        request = {
            "draft_id": did,
            "kind": safe_kind,
            "source_path": str(source),
        }
        request_hash = _sha256(_canonical(request))
        with self._locked():
            receipt = self._read_receipt_locked(key)
            if receipt is not None:
                if (receipt["request_sha256"] != request_hash
                        or receipt["request"] != request):
                    raise LocalSourceConflictError(
                        "idempotency key is already bound to a different local source")
                record = self._validate_record(receipt["record"])
                if require_directory and record["source_type"] != "directory":
                    raise LocalSourceError("selected local source must be a directory")
                record_path = self._record_path(
                    self.attachments_dir, record["source_id"])
                if not os.path.lexists(record_path):
                    _write_new(record_path, _canonical(record))
                    _fsync_dir(self.attachments_dir)
                elif self._read_record_locked(record["source_id"]) != record:
                    raise LocalSourceCorruptError(
                        "attachment record disagrees with its durable receipt")
                return self._public(record)

            records = self._records_for_draft_locked(did)
            if len(records) >= _MAX_SOURCES_PER_DRAFT:
                raise LocalSourceError("draft exceeds the local-source count budget")
            scanned = self._scan(
                source, allowed, allowed_identity, verified=False)
            if require_directory and scanned["source_type"] != "directory":
                raise LocalSourceError("selected local source must be a directory")
            source_id = uuid.uuid4().hex
            while os.path.lexists(self._record_path(self.attachments_dir, source_id)):
                source_id = uuid.uuid4().hex
            record = {
                "version": _VERSION,
                "source_id": source_id,
                "draft_id": did,
                "kind": safe_kind,
                "label": _safe_label(source),
                "source_path": str(source),
                "source_type": scanned["source_type"],
                "attached_at": _now(),
                "file_count": scanned["file_count"],
                "total_bytes": scanned["total_bytes"],
            }
            receipt = {
                "version": _RECEIPT_VERSION,
                "idempotency_key": key,
                "request_sha256": request_hash,
                "request": request,
                "record_sha256": _sha256(_canonical(record)),
                "record": record,
            }
            # Receipt first: a restart can reconstruct a missing attachment,
            # while the reverse order would leave an unbound orphan.
            _write_new(
                self._record_path(self.requests_dir, key), _canonical(receipt))
            _fsync_dir(self.requests_dir)
            _write_new(
                self._record_path(self.attachments_dir, source_id),
                _canonical(record))
            _fsync_dir(self.attachments_dir)
            return self._public(record)

    def _records_for_draft_locked(self, draft_id: str) -> List[Dict[str, Any]]:
        records = []
        for entry in sorted(self.attachments_dir.iterdir(), key=lambda item: item.name):
            record = self._validate_record(
                _read_canonical(
                    entry, owner=self.owner,
                    label=f"attachment {entry.stem}"),
                source_id=entry.stem)
            if record["draft_id"] == draft_id:
                records.append(record)
        records.sort(key=lambda row: (row["attached_at"], row["source_id"]))
        return records

    def list(self, draft_id: object) -> List[Dict[str, Any]]:
        """Return durable, path-free summaries without touching source data."""

        did = _validate_hex32(draft_id, label="draft_id")
        with self._locked():
            return [self._public(row) for row in self._records_for_draft_locked(did)]

    def _manifest(self, draft_id: object, *, verified: bool) -> Dict[str, Any]:
        did = _validate_hex32(draft_id, label="draft_id")
        with self._locked():
            rows = self._records_for_draft_locked(did)
            sources = []
            total_files = 0
            total_bytes = 0
            for record in rows:
                source, allowed, allowed_identity = self._normalize_source(
                    record["source_path"])
                scanned = self._scan(
                    source, allowed, allowed_identity, verified=verified)
                total_files += scanned["file_count"]
                total_bytes += scanned["total_bytes"]
                if total_files > _MAX_FILES or total_bytes > _MAX_TOTAL_BYTES:
                    raise LocalSourceError(
                        "combined local sources exceed the draft budget")
                sources.append({
                    "source_id": record["source_id"],
                    "label": record["label"],
                    "kind": record["kind"],
                    "status": "verified" if verified else "preflighted",
                    **scanned,
                })
            return {
                "version": _VERSION,
                "draft_id": did,
                "status": "verified" if verified else "preflighted",
                "generated_at": _now(),
                "file_count": total_files,
                "total_bytes": total_bytes,
                "sources": sources,
            }

    def preflight_manifest(self, draft_id: object) -> Dict[str, Any]:
        """Return a server-only metadata manifest (contains absolute paths)."""

        return self._manifest(draft_id, verified=False)

    def verified_manifest(self, draft_id: object) -> Dict[str, Any]:
        """Return a server-only SHA-256 manifest (contains absolute paths)."""

        return self._manifest(draft_id, verified=True)

    def public_preflight(self, draft_id: object) -> Dict[str, Any]:
        """Rescan metadata and return the only preflight shape safe for HTTP."""

        manifest = self.preflight_manifest(draft_id)
        # Derive the projection from this exact scan.  Re-reading the registry
        # here would create a race where a concurrently attached source exists
        # in the second list but not in ``manifest``.
        sources = [{
            "source_id": row["source_id"],
            "label": row["label"],
            "kind": row["kind"],
            "file_count": row["file_count"],
            "total_bytes": row["total_bytes"],
            "status": "preflighted",
        } for row in manifest["sources"]]
        return {
            "draft_id": manifest["draft_id"],
            "status": "preflighted",
            "file_count": manifest["file_count"],
            "total_bytes": manifest["total_bytes"],
            "sources": sources,
        }


__all__ = [
    "LocalSourceChangedError",
    "LocalSourceConflictError",
    "LocalSourceCorruptError",
    "LocalSourceError",
    "LocalSourceRegistry",
]
