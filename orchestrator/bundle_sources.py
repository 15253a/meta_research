"""Publication-backed source binding and private materialization."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath
from typing import Dict, Iterator, List, Tuple

from .storage_paths import RegisteredPathError, resolve_registered_path


_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TREE_HASH_ALG = "sha256-tree-v1"
_COPY_BLOCK = 1024 * 1024


class BundleSourceError(RuntimeError):
    """A published source cannot be bound or materialized safely."""


class SourceBindingError(BundleSourceError):
    """The request lacks one exact publication-backed admission."""


class SourceBindingConflict(SourceBindingError):
    """A durable binding differs from the exact admission."""


class SourceMaterializationError(BundleSourceError):
    """A bound source cannot be copied into a private target input."""


@dataclass(frozen=True)
class SourceBinding:
    binding_id: int
    request_id: int
    cycle_id: int
    downstream_target_id: int
    input_key: str
    upstream_target_id: int
    upstream_admission_id: int
    publication_decision_id: int
    manifest_ref: str
    manifest_hash: str
    source_ref: str
    source_hash: str
    source_hash_alg: str


class SourceCapability:
    """Owned directory descriptor for one private materialized source."""

    def __init__(
        self,
        fd: int,
        *,
        binding: SourceBinding,
        durable_path: Path,
    ) -> None:
        self.fd = fd
        self.binding_id = binding.binding_id
        self.request_id = binding.request_id
        self.cycle_id = binding.cycle_id
        self.downstream_target_id = binding.downstream_target_id
        self.input_key = binding.input_key
        self.upstream_target_id = binding.upstream_target_id
        self.manifest_ref = binding.manifest_ref
        self.manifest_hash = binding.manifest_hash
        self.source_hash = binding.source_hash
        self.source_hash_alg = binding.source_hash_alg
        self.durable_path = str(durable_path)

    @property
    def ref(self) -> str:
        if self.fd < 0:
            raise SourceMaterializationError("source capability is closed")
        return f"/proc/self/fd/{self.fd}"

    @property
    def pass_fds(self) -> Tuple[int, ...]:
        if self.fd < 0:
            raise SourceMaterializationError("source capability is closed")
        return (self.fd,)

    def detach(self) -> int:
        if self.fd < 0:
            raise SourceMaterializationError("source capability is closed")
        fd, self.fd = self.fd, -1
        return fd

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self) -> "SourceCapability":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


@contextmanager
def _atomic(conn: sqlite3.Connection) -> Iterator[None]:
    name = "bundle_source_operation"
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield
    except BaseException:
        conn.execute(f"ROLLBACK TO {name}")
        conn.execute(f"RELEASE {name}")
        raise
    else:
        conn.execute(f"RELEASE {name}")


class BundleSources:
    """Deep module for exact bindings and private source capabilities."""

    def __init__(self, conn: sqlite3.Connection, *, work_root: Path) -> None:
        # WriteDaemon exposes a lock-preserving connection facade.  Requiring
        # the concrete sqlite3 class would force callers to bypass that single
        # writer capability merely to perform the read/materialize half.
        if not callable(getattr(conn, "execute", None)):
            raise TypeError("conn must provide SQLite execute()")
        self._conn = conn
        self._work_root = Path(os.path.abspath(os.fspath(work_root)))
        self._lock = threading.RLock()

    def bind(self, request_id: int) -> SourceBinding:
        """Bind one request to its upstream target's exact admission."""
        request = self._positive_id(request_id, label="request_id")
        with self._lock:
            return self._bind_locked(request)

    def _bind_locked(self, request: int) -> SourceBinding:
        row = self._conn.execute(
            "SELECT r.id,r.cycle_id,r.downstream_target_id,r.input_key,"
            "r.upstream_target_id,a.id,a.publication_decision_id,"
            "a.manifest_ref,a.manifest_hash,a.source_ref,a.source_hash,"
            "a.source_hash_alg "
            "FROM bundle_source_request r "
            "LEFT JOIN bundle_target_admission a "
            "ON a.target_id=r.upstream_target_id AND a.cycle_id=r.cycle_id "
            "WHERE r.id=?",
            (request,),
        ).fetchone()
        if row is None:
            raise SourceBindingError(
                f"bundle source request {request} does not exist"
            )
        if row[5] is None:
            raise SourceBindingError(
                f"bundle source request {request} upstream is not admitted"
            )
        values = tuple(row)
        self._validate_exact_values(values)
        insert_values = (
            int(values[0]),
            int(values[1]),
            int(values[2]),
            str(values[3]),
            int(values[4]),
            int(values[5]),
            int(values[6]),
            str(values[7]),
            str(values[8]),
            str(values[9]),
            str(values[10]),
            str(values[11]),
        )
        with _atomic(self._conn):
            existing = self._conn.execute(
                "SELECT id,request_id,cycle_id,downstream_target_id,input_key,"
                "upstream_target_id,upstream_admission_id,"
                "publication_decision_id,manifest_ref,manifest_hash,"
                "source_ref,source_hash,source_hash_alg "
                "FROM bundle_source_binding WHERE request_id=?",
                (request,),
            ).fetchone()
            if existing is not None:
                if tuple(existing[1:]) != insert_values:
                    raise SourceBindingConflict(
                        f"bundle source request {request} binding conflicts "
                        "with exact admission"
                    )
                return self._binding_from_row(existing)
            try:
                cursor = self._conn.execute(
                    "INSERT INTO bundle_source_binding("
                    "request_id,cycle_id,downstream_target_id,input_key,"
                    "upstream_target_id,upstream_admission_id,"
                    "publication_decision_id,manifest_ref,manifest_hash,"
                    "source_ref,source_hash,source_hash_alg"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    insert_values,
                )
            except sqlite3.IntegrityError as error:
                raise SourceBindingConflict(
                    f"bundle source request {request} binding conflicts"
                ) from error
            binding_id = int(cursor.lastrowid)
        return SourceBinding(binding_id=binding_id, **self._binding_values(insert_values))

    def materialize(
        self,
        request_id: int,
        *,
        target_directory: Path,
        input_key: str,
    ) -> SourceCapability:
        """Create and pin one exact private writable source copy."""
        request = self._positive_id(request_id, label="request_id")
        key = self._safe_key(input_key, label="input_key")
        with self._lock:
            binding = self._load_binding(request)
            if key != binding.input_key:
                raise SourceMaterializationError(
                    f"input_key {key!r} does not match bound "
                    f"{binding.input_key!r}"
                )
            self._assert_binding_exact(binding)
            target = self._validated_target_directory(target_directory)
            destination = target / key
            if os.path.lexists(destination):
                recovered = self._tree_digest(
                    destination, expected_uid=os.geteuid())
                if recovered != binding.source_hash:
                    raise SourceMaterializationError(
                        "existing target input hash conflicts with binding"
                    )
                return SourceCapability(
                    self._open_directory_capability(destination),
                    binding=binding,
                    durable_path=destination,
                )
            source = self._resolve_source(binding.source_ref)
            expected = binding.source_hash
            before = self._tree_digest(source)
            if before != expected:
                raise SourceMaterializationError(
                    "published source hash drifted before materialization"
                )

            staging = target / f".{key}.source-{uuid.uuid4().hex}"
            published = False
            try:
                self._copy_tree(source, staging)
                after = self._tree_digest(source)
                copied = self._tree_digest(staging)
                if after != expected or copied != expected:
                    raise SourceMaterializationError(
                        "published source drifted during materialization"
                    )
                if os.path.lexists(destination):
                    raise SourceMaterializationError(
                        f"target input appeared during materialization: "
                        f"{destination}"
                    )
                os.rename(staging, destination)
                published = True
                self._fsync_directory(target)
                if self._tree_digest(destination) != expected:
                    raise SourceMaterializationError(
                        "private source hash drifted after publication"
                    )
                fd = self._open_directory_capability(destination)
                return SourceCapability(
                    fd,
                    binding=binding,
                    durable_path=destination,
                )
            except BundleSourceError:
                if published:
                    self._safe_remove_tree(destination)
                else:
                    self._safe_remove_tree(staging)
                raise
            except (OSError, ValueError) as error:
                if published:
                    self._safe_remove_tree(destination)
                else:
                    self._safe_remove_tree(staging)
                raise SourceMaterializationError(
                    f"source materialization failed for request {request}"
                ) from error

    @staticmethod
    def _positive_id(value: int, *, label: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise SourceBindingError(f"{label} must be a positive integer")
        return value

    @staticmethod
    def _validate_exact_values(values) -> None:
        if (
            not isinstance(values[3], str)
            or not values[3]
            or not isinstance(values[7], str)
            or not values[7]
            or not isinstance(values[8], str)
            or _HASH_RE.fullmatch(values[8]) is None
            or not isinstance(values[9], str)
            or not values[9]
            or not isinstance(values[10], str)
            or _HASH_RE.fullmatch(values[10]) is None
            or values[11] != _TREE_HASH_ALG
        ):
            raise SourceBindingError(
                "upstream admission has incomplete source ref/hash/algorithm"
            )

    @staticmethod
    def _binding_values(values):
        return {
            "request_id": int(values[0]),
            "cycle_id": int(values[1]),
            "downstream_target_id": int(values[2]),
            "input_key": str(values[3]),
            "upstream_target_id": int(values[4]),
            "upstream_admission_id": int(values[5]),
            "publication_decision_id": int(values[6]),
            "manifest_ref": str(values[7]),
            "manifest_hash": str(values[8]),
            "source_ref": str(values[9]),
            "source_hash": str(values[10]),
            "source_hash_alg": str(values[11]),
        }

    @classmethod
    def _binding_from_row(cls, row) -> SourceBinding:
        return SourceBinding(
            binding_id=int(row[0]),
            **cls._binding_values(tuple(row[1:])),
        )

    def _load_binding(self, request_id: int) -> SourceBinding:
        row = self._conn.execute(
            "SELECT id,request_id,cycle_id,downstream_target_id,input_key,"
            "upstream_target_id,upstream_admission_id,"
            "publication_decision_id,manifest_ref,manifest_hash,"
            "source_ref,source_hash,source_hash_alg "
            "FROM bundle_source_binding WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if row is None:
            raise SourceMaterializationError(
                f"bundle source request {request_id} is not bound"
            )
        return self._binding_from_row(row)

    def _assert_binding_exact(self, binding: SourceBinding) -> None:
        row = self._conn.execute(
            "SELECT r.id,r.cycle_id,r.downstream_target_id,r.input_key,"
            "r.upstream_target_id,a.id,a.publication_decision_id,"
            "a.manifest_ref,a.manifest_hash,a.source_ref,a.source_hash,"
            "a.source_hash_alg "
            "FROM bundle_source_request r "
            "JOIN bundle_target_admission a "
            "ON a.target_id=r.upstream_target_id AND a.cycle_id=r.cycle_id "
            "WHERE r.id=?",
            (binding.request_id,),
        ).fetchone()
        if row is None:
            raise SourceMaterializationError(
                "bound request no longer has an exact upstream admission"
            )
        self._validate_exact_values(tuple(row))
        expected = self._binding_values(tuple(row))
        observed = {
            key: getattr(binding, key)
            for key in expected
        }
        if observed != expected:
            raise SourceBindingConflict(
                f"bundle source request {binding.request_id} binding "
                "conflicts with exact admission"
            )

    @staticmethod
    def _safe_key(value: str, *, label: str) -> str:
        if (
            not isinstance(value, str)
            or _SAFE_KEY_RE.fullmatch(value) is None
        ):
            raise SourceMaterializationError(
                f"{label} must be a path-safe key"
            )
        return value

    @staticmethod
    def _validated_target_directory(value: Path) -> Path:
        try:
            raw = os.fspath(value)
        except TypeError as error:
            raise SourceMaterializationError(
                "target_directory must be a canonical absolute path"
            ) from error
        if (
            not isinstance(raw, str)
            or not raw
            or "\x00" in raw
            or not os.path.isabs(raw)
            or os.path.abspath(raw) != raw
        ):
            raise SourceMaterializationError(
                "target_directory must be a canonical absolute path"
            )
        target = Path(raw)
        current = Path(target.anchor)
        for component in target.parts[1:]:
            current = current / component
            try:
                info = current.lstat()
            except OSError as error:
                raise SourceMaterializationError(
                    f"target_directory is missing: {target}"
                ) from error
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise SourceMaterializationError(
                    f"target_directory crosses a symlink/non-directory: "
                    f"{current}"
                )
        info = target.lstat()
        if info.st_uid != os.geteuid():
            raise SourceMaterializationError(
                "target_directory is not owned by the service user"
            )
        return target

    def _resolve_source(self, source_ref: str) -> Path:
        pure = PurePosixPath(source_ref)
        if (
            not isinstance(source_ref, str)
            or not source_ref
            or "\x00" in source_ref
            or "\\" in source_ref
            or pure.is_absolute()
            or any(part in ("", ".", "..") for part in pure.parts)
        ):
            raise SourceMaterializationError(
                "bound source_ref is not a safe relative reference"
            )
        try:
            source = resolve_registered_path(self._work_root, source_ref)
        except RegisteredPathError as error:
            raise SourceMaterializationError(
                "bound source_ref escapes the registered work root"
            ) from error
        try:
            relative = source.relative_to(self._work_root)
        except ValueError as error:
            raise SourceMaterializationError(
                "bound source_ref escapes the work root"
            ) from error
        current = self._work_root
        try:
            root_info = current.lstat()
        except OSError as error:
            raise SourceMaterializationError("work_root is missing") from error
        if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
            raise SourceMaterializationError(
                "work_root is a symlink or non-directory"
            )
        for component in relative.parts:
            current = current / component
            try:
                info = current.lstat()
            except OSError as error:
                raise SourceMaterializationError(
                    f"published source is missing: {source_ref}"
                ) from error
            if stat.S_ISLNK(info.st_mode):
                raise SourceMaterializationError(
                    f"published source crosses a symlink: {source_ref}"
                )
        if not stat.S_ISDIR(current.lstat().st_mode):
            raise SourceMaterializationError(
                "published source is not a directory tree"
            )
        return current

    @classmethod
    def _tree_digest(
        cls,
        root: Path,
        *,
        expected_uid: int | None = None,
    ) -> str:
        records = []  # type: List[Dict[str, object]]

        def visit(directory: Path, relative: PurePosixPath) -> None:
            try:
                directory_info = directory.lstat()
            except OSError as error:
                raise SourceMaterializationError(
                    f"source tree entry disappeared: {directory}"
                ) from error
            if (
                stat.S_ISLNK(directory_info.st_mode)
                or not stat.S_ISDIR(directory_info.st_mode)
            ):
                raise SourceMaterializationError(
                    f"source tree contains a symlink/non-directory: "
                    f"{directory}"
                )
            if (expected_uid is not None
                    and directory_info.st_uid != expected_uid):
                raise SourceMaterializationError(
                    f"source tree directory has unexpected owner: "
                    f"{directory}"
                )
            try:
                with os.scandir(directory) as iterator:
                    entries = sorted(iterator, key=lambda item: item.name)
            except OSError as error:
                raise SourceMaterializationError(
                    f"source tree cannot be enumerated: {directory}"
                ) from error
            if relative != PurePosixPath(".") and not entries:
                records.append(
                    {"path": relative.as_posix(), "type": "dir"}
                )
            for entry in entries:
                child = directory / entry.name
                child_relative = (
                    PurePosixPath(entry.name)
                    if relative == PurePosixPath(".")
                    else relative / entry.name
                )
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError as error:
                    raise SourceMaterializationError(
                        f"source tree entry disappeared: {child}"
                    ) from error
                if stat.S_ISLNK(info.st_mode):
                    raise SourceMaterializationError(
                        f"source tree contains a symlink: {child}"
                    )
                if expected_uid is not None and info.st_uid != expected_uid:
                    raise SourceMaterializationError(
                        f"source tree entry has unexpected owner: {child}"
                    )
                if stat.S_ISDIR(info.st_mode):
                    visit(child, child_relative)
                elif stat.S_ISREG(info.st_mode):
                    if expected_uid is not None and info.st_nlink != 1:
                        raise SourceMaterializationError(
                            f"source tree file is hard-linked: {child}"
                        )
                    raw, _mode = cls._read_regular(
                        child, expected_uid=expected_uid)
                    records.append(
                        {
                            "path": child_relative.as_posix(),
                            "type": "file",
                            "sha256": hashlib.sha256(raw).hexdigest(),
                            "bytes": len(raw),
                        }
                    )
                else:
                    raise SourceMaterializationError(
                        f"source tree contains a non-regular entry: {child}"
                    )

        visit(root, PurePosixPath("."))
        payload = (
            json.dumps(
                records,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _read_regular(
        path: Path,
        *,
        expected_uid: int | None = None,
    ) -> Tuple[bytes, int]:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            fd = os.open(path, flags)
        except OSError as error:
            raise SourceMaterializationError(
                f"source file cannot be opened safely: {path}"
            ) from error
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise SourceMaterializationError(
                    f"source entry is not a regular file: {path}"
                )
            if (expected_uid is not None
                    and (before.st_uid != expected_uid
                         or before.st_nlink != 1)):
                raise SourceMaterializationError(
                    f"source file owner/link identity is invalid: {path}"
                )
            raw = bytearray()
            while len(raw) < before.st_size:
                chunk = os.read(
                    fd,
                    min(_COPY_BLOCK, before.st_size - len(raw)),
                )
                if not chunk:
                    raise SourceMaterializationError(
                        f"source file ended early: {path}"
                    )
                raw.extend(chunk)
            after = os.fstat(fd)
            identity = (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_uid,
                before.st_nlink,
            )
            if (
                (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_uid,
                    after.st_nlink,
                )
                != identity
            ):
                raise SourceMaterializationError(
                    f"source file drifted while reading: {path}"
                )
            current = path.lstat()
            if (
                stat.S_ISLNK(current.st_mode)
                or (
                    current.st_dev,
                    current.st_ino,
                    current.st_size,
                    current.st_mtime_ns,
                    current.st_uid,
                    current.st_nlink,
                )
                != identity
            ):
                raise SourceMaterializationError(
                    f"source file path drifted while reading: {path}"
                )
            return bytes(raw), stat.S_IMODE(before.st_mode)
        finally:
            os.close(fd)

    @classmethod
    def _copy_tree(cls, source: Path, destination: Path) -> None:
        destination.mkdir(mode=0o700)

        def visit(source_directory: Path, target_directory: Path) -> None:
            with os.scandir(source_directory) as iterator:
                entries = sorted(iterator, key=lambda item: item.name)
            for entry in entries:
                source_child = source_directory / entry.name
                target_child = target_directory / entry.name
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    raise SourceMaterializationError(
                        f"source tree contains a symlink: {source_child}"
                    )
                if stat.S_ISDIR(info.st_mode):
                    target_child.mkdir(mode=0o700)
                    visit(source_child, target_child)
                    cls._fsync_directory(target_child)
                elif stat.S_ISREG(info.st_mode):
                    raw, mode = cls._read_regular(source_child)
                    cls._write_new_file(
                        target_child,
                        raw,
                        executable=bool(mode & 0o111),
                    )
                else:
                    raise SourceMaterializationError(
                        f"source tree contains a non-regular entry: "
                        f"{source_child}"
                    )

        visit(source, destination)
        cls._fsync_directory(destination)

    @staticmethod
    def _write_new_file(
        path: Path,
        payload: bytes,
        *,
        executable: bool,
    ) -> None:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        fd = os.open(path, flags, 0o700 if executable else 0o600)
        try:
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("private source copy write made no progress")
                view = view[written:]
            os.fchmod(fd, 0o700 if executable else 0o600)
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        fd = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            if not stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError(f"fsync target is not a directory: {path}")
            os.fsync(fd)
        finally:
            os.close(fd)

    @classmethod
    def _safe_remove_tree(cls, path: Path) -> None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            path.unlink()
            return
        with os.scandir(path) as iterator:
            names = [entry.name for entry in iterator]
        for name in names:
            cls._safe_remove_tree(path / name)
        path.rmdir()

    @staticmethod
    def _open_directory_capability(path: Path) -> int:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
            raise SourceMaterializationError(
                "materialized source is not a real directory"
            )
        fd = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            opened = os.fstat(fd)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or (opened.st_dev, opened.st_ino)
                != (before.st_dev, before.st_ino)
            ):
                raise SourceMaterializationError(
                    "materialized source path drifted before capability open"
                )
            return fd
        except BaseException:
            os.close(fd)
            raise


def copy_verified_source_tree(
    source: Path,
    destination: Path,
    *,
    expected_hash: str,
    source_uid: int,
    destination_uid: int,
    destination_gid: int,
) -> None:
    """Reverify one materialized source and create a private writable copy.

    The source and copy must both match the publication-bound
    ``sha256-tree-v1`` digest.  No symlink, foreign-owned entry or hard-linked
    file is accepted, and the source is checked again after the copy.
    """
    if (not isinstance(expected_hash, str)
            or _HASH_RE.fullmatch(expected_hash) is None):
        raise SourceMaterializationError(
            "worker source expected_hash must be 64 lowercase hex")
    for value, label in (
            (source_uid, "source_uid"),
            (destination_uid, "destination_uid"),
            (destination_gid, "destination_gid")):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise SourceMaterializationError(
                f"worker source {label} must be a non-negative integer")
    source_path = Path(source)
    destination_path = Path(destination)
    if os.path.lexists(destination_path):
        raise SourceMaterializationError(
            f"worker source destination already exists: {destination_path}")

    try:
        before = BundleSources._tree_digest(  # noqa: SLF001 - shared verifier
            source_path, expected_uid=source_uid)
        if before != expected_hash:
            raise SourceMaterializationError(
                "materialized source hash drifted before worker copy")
        BundleSources._copy_tree(  # noqa: SLF001 - same deep-module operation
            source_path, destination_path)
        after = BundleSources._tree_digest(  # noqa: SLF001
            source_path, expected_uid=source_uid)
        copied = BundleSources._tree_digest(  # noqa: SLF001
            destination_path, expected_uid=os.geteuid())
        if after != expected_hash or copied != expected_hash:
            raise SourceMaterializationError(
                "materialized source drifted during worker copy")

        if (destination_uid, destination_gid) != (
                os.geteuid(), os.getegid()):
            def chown_tree(directory: Path) -> None:
                with os.scandir(directory) as iterator:
                    entries = sorted(iterator, key=lambda item: item.name)
                for entry in entries:
                    child = directory / entry.name
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode):
                        raise SourceMaterializationError(
                            f"worker source copy contains a symlink: {child}")
                    if stat.S_ISDIR(info.st_mode):
                        chown_tree(child)
                    elif not stat.S_ISREG(info.st_mode):
                        raise SourceMaterializationError(
                            f"worker source copy contains a non-file: {child}")
                    os.chown(
                        child, destination_uid, destination_gid,
                        follow_symlinks=False)
                os.chown(
                    directory, destination_uid, destination_gid,
                    follow_symlinks=False)

            chown_tree(destination_path)
        if BundleSources._tree_digest(  # noqa: SLF001
                destination_path,
                expected_uid=destination_uid) != expected_hash:
            raise SourceMaterializationError(
                "worker source copy hash/owner drifted after publication")
    except BundleSourceError:
        BundleSources._safe_remove_tree(destination_path)  # noqa: SLF001
        raise
    except (OSError, ValueError) as error:
        BundleSources._safe_remove_tree(destination_path)  # noqa: SLF001
        raise SourceMaterializationError(
            "worker source private copy failed") from error
