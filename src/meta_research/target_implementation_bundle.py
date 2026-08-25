"""Closed, deterministic implementation trees for formal Target execution.

Formal intake starts from an Owner-private Target workspace.  Research Memory
accepts that directory and materializes it as a deterministic, stored ZIP.
The generic execution port consumes only those accepted bytes plus the exact
inner-tree content hash; it never receives a caller-selected host path.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable


IMPLEMENTATION_BUNDLE_MEDIA_TYPE = "application/zip"
IMPLEMENTATION_TREE_KIND = "directory"
IMPLEMENTATION_ENTRY_MAX_COUNT = 4096
IMPLEMENTATION_ENTRY_MAX_BYTES = 16 * 1024 * 1024
IMPLEMENTATION_TOTAL_MAX_BYTES = 64 * 1024 * 1024
IMPLEMENTATION_BUNDLE_MAX_BYTES = 96 * 1024 * 1024
IMPLEMENTATION_PATH_MAX_BYTES = 1024
IMPLEMENTATION_ALLOWED_FILE_MODES = frozenset({0o600, 0o644, 0o700, 0o755})
IMPLEMENTATION_ALLOWED_DIRECTORY_MODES = frozenset({0o700, 0o755})
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


class TargetImplementationBundleError(ValueError):
    """Typed fail-closed implementation-tree validation error."""

    def __init__(self, code: str = "target_implementation_bundle_invalid") -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class TargetImplementationBundleEntry:
    relative_path: str
    mode: int
    byte_count: int
    content_sha256: str
    content: bytes

    def tree_value(self) -> dict[str, object]:
        return {
            "path": self.relative_path,
            "sha256": self.content_sha256,
            "size": self.byte_count,
        }


@dataclass(frozen=True, slots=True)
class TargetImplementationBundle:
    kind: str
    directories: tuple[str, ...]
    entries: tuple[TargetImplementationBundleEntry, ...]
    bundle_bytes: bytes
    bundle_sha256: str
    tree_manifest_bytes: bytes
    tree_sha256: str

    def entry(self, relative_path: str) -> TargetImplementationBundleEntry:
        validated = validate_bundle_relative_path(relative_path)
        for entry in self.entries:
            if entry.relative_path == validated:
                return entry
        raise TargetImplementationBundleError(
            "target_implementation_entrypoint_missing"
        )


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def parse_target_implementation_bundle(
    bundle_bytes: bytes,
    *,
    expected_tree_sha256: str | None = None,
) -> TargetImplementationBundle:
    """Validate a deterministic RM directory ZIP and its inner-tree hash."""

    if type(bundle_bytes) is not bytes or not bundle_bytes:
        raise TargetImplementationBundleError()
    if len(bundle_bytes) > IMPLEMENTATION_BUNDLE_MAX_BYTES:
        raise TargetImplementationBundleError(
            "target_implementation_bundle_too_large"
        )
    if expected_tree_sha256 is not None:
        _validate_sha256(expected_tree_sha256)
    try:
        archive = zipfile.ZipFile(io.BytesIO(bundle_bytes), "r")
    except (OSError, zipfile.BadZipFile) as error:
        raise TargetImplementationBundleError() from error
    directories: list[str] = []
    entries: list[TargetImplementationBundleEntry] = []
    names: list[str] = []
    total_bytes = 0
    with archive:
        infos = archive.infolist()
        if not infos or len(infos) > IMPLEMENTATION_ENTRY_MAX_COUNT:
            raise TargetImplementationBundleError()
        for info in infos:
            name = info.filename
            is_directory = name.endswith("/")
            relative_path = validate_bundle_relative_path(
                name[:-1] if is_directory else name
            )
            normalized_name = relative_path + ("/" if is_directory else "")
            mode = (info.external_attr >> 16) & 0o7777
            file_type = (info.external_attr >> 16) & 0o170000
            if (
                name != normalized_name
                or info.date_time != _ZIP_EPOCH
                or info.compress_type != zipfile.ZIP_STORED
                or info.flag_bits & 0x1
                or info.extra not in {b"", b"\x00\x00\x00\x00"}
                or info.comment
                or info.create_system != 3
                or info.header_offset < 0
            ):
                raise TargetImplementationBundleError()
            if is_directory:
                if (
                    file_type not in {0, stat.S_IFDIR}
                    or mode not in IMPLEMENTATION_ALLOWED_DIRECTORY_MODES
                    or info.file_size != 0
                    or archive.read(info) != b""
                ):
                    raise TargetImplementationBundleError()
                directories.append(relative_path)
            else:
                if (
                    file_type not in {0, stat.S_IFREG}
                    or mode not in IMPLEMENTATION_ALLOWED_FILE_MODES
                    or info.file_size < 0
                    or info.file_size > IMPLEMENTATION_ENTRY_MAX_BYTES
                    or info.compress_size != info.file_size
                ):
                    raise TargetImplementationBundleError()
                try:
                    content = archive.read(info)
                except (OSError, RuntimeError, zipfile.BadZipFile) as error:
                    raise TargetImplementationBundleError() from error
                if len(content) != info.file_size:
                    raise TargetImplementationBundleError()
                total_bytes += len(content)
                if total_bytes > IMPLEMENTATION_TOTAL_MAX_BYTES:
                    raise TargetImplementationBundleError(
                        "target_implementation_bundle_too_large"
                    )
                entries.append(
                    TargetImplementationBundleEntry(
                        relative_path=relative_path,
                        mode=mode,
                        byte_count=len(content),
                        content_sha256=hashlib.sha256(content).hexdigest(),
                        content=content,
                    )
                )
            names.append(normalized_name)
    canonical_names = [path + "/" for path in directories] + [
        entry.relative_path for entry in entries
    ]
    if (
        names != canonical_names
        or directories != sorted(directories)
        or len(names) != len(set(names))
        or not entries
    ):
        raise TargetImplementationBundleError()
    directory_set = set(directories)
    file_paths = [entry.relative_path for entry in entries]
    if file_paths != sorted(file_paths) or len(file_paths) != len(set(file_paths)):
        raise TargetImplementationBundleError()
    _validate_tree_shape(directory_set, file_paths)
    tree_value = _rm_directory_content_manifest(directories, entries)
    tree_bytes = canonical_json(tree_value).encode("utf-8")
    tree_sha256 = hashlib.sha256(tree_bytes).hexdigest()
    if expected_tree_sha256 is not None and tree_sha256 != expected_tree_sha256:
        raise TargetImplementationBundleError(
            "target_implementation_tree_hash_mismatch"
        )
    # Re-encoding detects duplicate central-directory tricks and freezes the
    # exact byte representation accepted by the port.
    rebuilt = build_target_implementation_bundle(
        ((entry.relative_path, entry.mode, entry.content) for entry in entries),
        directories=directories,
    )
    if rebuilt.bundle_bytes != bundle_bytes:
        raise TargetImplementationBundleError(
            "target_implementation_bundle_noncanonical"
        )
    return TargetImplementationBundle(
        kind=IMPLEMENTATION_TREE_KIND,
        directories=tuple(directories),
        entries=tuple(entries),
        bundle_bytes=bundle_bytes,
        bundle_sha256=hashlib.sha256(bundle_bytes).hexdigest(),
        tree_manifest_bytes=tree_bytes,
        tree_sha256=tree_sha256,
    )


def build_target_implementation_bundle(
    entries: Iterable[tuple[str, int, bytes]],
    *,
    directories: Iterable[str] = (),
) -> TargetImplementationBundle:
    """Encode the deterministic ZIP shape used by RM directory materialization."""

    normalized_entries: list[TargetImplementationBundleEntry] = []
    for relative_path, mode, content in entries:
        path = validate_bundle_relative_path(relative_path)
        if (
            type(mode) is not int
            or isinstance(mode, bool)
            or mode not in IMPLEMENTATION_ALLOWED_FILE_MODES
            or type(content) is not bytes
            or len(content) > IMPLEMENTATION_ENTRY_MAX_BYTES
        ):
            raise TargetImplementationBundleError()
        normalized_entries.append(
            TargetImplementationBundleEntry(
                relative_path=path,
                mode=mode,
                byte_count=len(content),
                content_sha256=hashlib.sha256(content).hexdigest(),
                content=content,
            )
        )
    normalized_entries.sort(key=lambda entry: entry.relative_path)
    normalized_directories = sorted(
        validate_bundle_relative_path(path) for path in directories
    )
    if (
        not normalized_entries
        or len(normalized_entries) + len(normalized_directories)
        > IMPLEMENTATION_ENTRY_MAX_COUNT
        or len({entry.relative_path for entry in normalized_entries})
        != len(normalized_entries)
        or len(set(normalized_directories)) != len(normalized_directories)
        or sum(entry.byte_count for entry in normalized_entries)
        > IMPLEMENTATION_TOTAL_MAX_BYTES
    ):
        raise TargetImplementationBundleError()
    _validate_tree_shape(
        set(normalized_directories),
        [entry.relative_path for entry in normalized_entries],
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        # RM materialization emits all directories first, then all files.
        for directory in normalized_directories:
            info = zipfile.ZipInfo(directory + "/", date_time=_ZIP_EPOCH)
            info.create_system = 3
            info.external_attr = (stat.S_IFDIR | 0o700) << 16
            archive.writestr(info, b"")
        for entry in normalized_entries:
            info = zipfile.ZipInfo(entry.relative_path, date_time=_ZIP_EPOCH)
            info.create_system = 3
            # RM normalizes managed directory files to 0600 on materialize.
            info.external_attr = 0o600 << 16
            archive.writestr(info, entry.content)
    encoded = output.getvalue()
    if len(encoded) > IMPLEMENTATION_BUNDLE_MAX_BYTES:
        raise TargetImplementationBundleError(
            "target_implementation_bundle_too_large"
        )
    tree_value = _rm_directory_content_manifest(
        normalized_directories, normalized_entries
    )
    tree_bytes = canonical_json(tree_value).encode("utf-8")
    return TargetImplementationBundle(
        kind=IMPLEMENTATION_TREE_KIND,
        directories=tuple(normalized_directories),
        entries=tuple(
            TargetImplementationBundleEntry(
                relative_path=entry.relative_path,
                mode=0o600,
                byte_count=entry.byte_count,
                content_sha256=entry.content_sha256,
                content=entry.content,
            )
            for entry in normalized_entries
        ),
        bundle_bytes=encoded,
        bundle_sha256=hashlib.sha256(encoded).hexdigest(),
        tree_manifest_bytes=tree_bytes,
        tree_sha256=hashlib.sha256(tree_bytes).hexdigest(),
    )


def build_target_implementation_bundle_from_directory(
    source: Path,
) -> TargetImplementationBundle:
    """Freeze an Owner-private workspace directory without following links."""

    descriptor = _open_pinned_source_directory(source)
    try:
        return build_target_implementation_bundle_from_open_directory(descriptor)
    finally:
        os.close(descriptor)


def build_target_implementation_bundle_from_open_directory(
    source_descriptor: int,
) -> TargetImplementationBundle:
    """Freeze one already pinned directory, keeping every lookup fd-relative."""

    if type(source_descriptor) is not int:
        raise TargetImplementationBundleError(
            "target_implementation_workspace_invalid"
        )
    try:
        descriptor = os.open(
            ".",
            _directory_open_flags(),
            dir_fd=source_descriptor,
        )
    except OSError as error:
        raise TargetImplementationBundleError(
            "target_implementation_workspace_unavailable"
        ) from error
    try:
        source_stat = os.fstat(descriptor)
    except OSError as error:
        os.close(descriptor)
        raise TargetImplementationBundleError(
            "target_implementation_workspace_unavailable"
        ) from error
    if not stat.S_ISDIR(source_stat.st_mode):
        os.close(descriptor)
        raise TargetImplementationBundleError(
            "target_implementation_workspace_invalid"
        )
    directories: list[str] = []
    entries: list[tuple[str, int, bytes]] = []
    try:
        try:
            _collect_open_directory(
                descriptor,
                relative_prefix="",
                directories=directories,
                entries=entries,
                remaining_bytes=[IMPLEMENTATION_TOTAL_MAX_BYTES],
                remaining_entries=[IMPLEMENTATION_ENTRY_MAX_COUNT],
            )
        finally:
            os.close(descriptor)
        return build_target_implementation_bundle(
            entries, directories=directories
        )
    except TargetImplementationBundleError as error:
        if error.code == "target_implementation_bundle_invalid":
            raise TargetImplementationBundleError(
                "target_implementation_workspace_entry_unsupported"
            ) from error
        raise


def _open_pinned_source_directory(source: Path) -> int:
    try:
        if source.is_absolute():
            descriptor = os.open("/", _directory_open_flags())
            parts = source.parts[1:]
        else:
            descriptor = os.open(".", _directory_open_flags())
            parts = source.parts
    except OSError as error:
        raise TargetImplementationBundleError(
            "target_implementation_workspace_unavailable"
        ) from error
    try:
        for part in parts:
            if part in {"", "."}:
                continue
            next_descriptor = _open_directory_at(
                descriptor,
                part,
                error_code="target_implementation_workspace_invalid",
            )
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _collect_open_directory(
    descriptor: int,
    *,
    relative_prefix: str,
    directories: list[str],
    entries: list[tuple[str, int, bytes]],
    remaining_bytes: list[int],
    remaining_entries: list[int],
) -> None:
    try:
        before = os.fstat(descriptor)
    except OSError as error:
        raise TargetImplementationBundleError(
            "target_implementation_workspace_unavailable"
        ) from error
    names_before = _bounded_directory_names(
        descriptor,
        remaining_entries=remaining_entries,
        overflow_code="target_implementation_bundle_too_large",
    )
    for name in names_before:
        relative_path = validate_bundle_relative_path(
            f"{relative_prefix}/{name}" if relative_prefix else name
        )
        try:
            listed = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError as error:
            raise TargetImplementationBundleError(
                "target_implementation_workspace_changed"
            ) from error
        except OSError as error:
            raise TargetImplementationBundleError(
                "target_implementation_workspace_unavailable"
            ) from error
        if stat.S_ISLNK(listed.st_mode):
            raise TargetImplementationBundleError(
                "target_implementation_workspace_entry_unsupported"
            )
        if stat.S_ISDIR(listed.st_mode):
            child = _open_directory_at(
                descriptor,
                name,
                error_code="target_implementation_workspace_entry_unsupported",
            )
            try:
                opened = os.fstat(child)
                if _stat_identity(listed) != _stat_identity(opened):
                    raise TargetImplementationBundleError(
                        "target_implementation_workspace_changed"
                    )
                directories.append(relative_path)
                _collect_open_directory(
                    child,
                    relative_prefix=relative_path,
                    directories=directories,
                    entries=entries,
                    remaining_bytes=remaining_bytes,
                    remaining_entries=remaining_entries,
                )
            except OSError as error:
                raise TargetImplementationBundleError(
                    "target_implementation_workspace_unavailable"
                ) from error
            finally:
                os.close(child)
        elif stat.S_ISREG(listed.st_mode):
            child = _open_regular_file_at(descriptor, name)
            try:
                opened = os.fstat(child)
                if _stat_identity(listed) != _stat_identity(opened):
                    raise TargetImplementationBundleError(
                        "target_implementation_workspace_changed"
                    )
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_nlink != 1
                    or opened.st_size > IMPLEMENTATION_ENTRY_MAX_BYTES
                ):
                    raise TargetImplementationBundleError(
                        "target_implementation_workspace_entry_unsupported"
                    )
                if opened.st_size > remaining_bytes[0]:
                    raise TargetImplementationBundleError(
                        "target_implementation_bundle_too_large"
                    )
                content = _read_open_regular_file(child, opened)
            except OSError as error:
                raise TargetImplementationBundleError(
                    "target_implementation_workspace_unavailable"
                ) from error
            finally:
                os.close(child)
            remaining_bytes[0] -= len(content)
            mode = 0o755 if opened.st_mode & 0o111 else 0o644
            entries.append((relative_path, mode, content))
        else:
            raise TargetImplementationBundleError(
                "target_implementation_workspace_entry_unsupported"
            )
    names_after = _bounded_directory_names(
        descriptor,
        maximum_count=len(names_before),
        overflow_code="target_implementation_workspace_changed",
    )
    try:
        after = os.fstat(descriptor)
    except OSError as error:
        raise TargetImplementationBundleError(
            "target_implementation_workspace_unavailable"
        ) from error
    if names_before != names_after or _stat_identity(before) != _stat_identity(after):
        raise TargetImplementationBundleError(
            "target_implementation_workspace_changed"
        )


def _open_directory_at(descriptor: int, name: str, *, error_code: str) -> int:
    try:
        child = os.open(name, _directory_open_flags(), dir_fd=descriptor)
    except OSError as error:
        raise TargetImplementationBundleError(error_code) from error
    try:
        opened = os.fstat(child)
    except OSError as error:
        os.close(child)
        raise TargetImplementationBundleError(error_code) from error
    if not stat.S_ISDIR(opened.st_mode):
        os.close(child)
        raise TargetImplementationBundleError(error_code)
    return child


def _open_regular_file_at(descriptor: int, name: str) -> int:
    flags = (
        os.O_RDONLY
        | os.O_CLOEXEC
        | os.O_NOFOLLOW
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        return os.open(name, flags, dir_fd=descriptor)
    except OSError as error:
        raise TargetImplementationBundleError(
            "target_implementation_workspace_entry_unsupported"
        ) from error


def _read_open_regular_file(descriptor: int, before: os.stat_result) -> bytes:
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size > IMPLEMENTATION_ENTRY_MAX_BYTES
    ):
        raise TargetImplementationBundleError(
            "target_implementation_workspace_entry_unsupported"
        )
    chunks: list[bytes] = []
    remaining = before.st_size
    while remaining:
        chunk = os.read(descriptor, min(1024 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    after = os.fstat(descriptor)
    content = b"".join(chunks)
    if (
        _stat_identity(before) != _stat_identity(after)
        or len(content) != before.st_size
    ):
        raise TargetImplementationBundleError(
            "target_implementation_workspace_changed"
        )
    return content


def _bounded_directory_names(
    descriptor: int,
    *,
    overflow_code: str,
    remaining_entries: list[int] | None = None,
    maximum_count: int | None = None,
) -> list[str]:
    if (remaining_entries is None) == (maximum_count is None):
        raise TargetImplementationBundleError()
    names: list[str] = []
    try:
        with os.scandir(descriptor) as iterator:
            for entry in iterator:
                if remaining_entries is not None:
                    if remaining_entries[0] == 0:
                        raise TargetImplementationBundleError(overflow_code)
                    remaining_entries[0] -= 1
                elif maximum_count is not None and len(names) == maximum_count:
                    raise TargetImplementationBundleError(overflow_code)
                names.append(entry.name)
    except OSError as error:
        raise TargetImplementationBundleError(
            "target_implementation_workspace_unavailable"
        ) from error
    names.sort()
    return names


def _directory_open_flags() -> int:
    return os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW


def _stat_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def parse_target_single_file_bundle(
    content: bytes,
    *,
    entrypoint: str,
    expected_content_sha256: str,
) -> TargetImplementationBundle:
    """Compatibility-only one-file materialization; not a second contract."""

    path = validate_bundle_relative_path(entrypoint)
    _validate_sha256(expected_content_sha256)
    if (
        type(content) is not bytes
        or not content
        or len(content) > IMPLEMENTATION_ENTRY_MAX_BYTES
        or hashlib.sha256(content).hexdigest() != expected_content_sha256
    ):
        raise TargetImplementationBundleError(
            "target_implementation_tree_hash_mismatch"
        )
    tree_bytes = content
    return TargetImplementationBundle(
        kind="file",
        directories=(),
        entries=(
            TargetImplementationBundleEntry(
                relative_path=path,
                mode=0o600,
                byte_count=len(content),
                content_sha256=expected_content_sha256,
                content=content,
            ),
        ),
        bundle_bytes=content,
        bundle_sha256=hashlib.sha256(content).hexdigest(),
        tree_manifest_bytes=tree_bytes,
        tree_sha256=expected_content_sha256,
    )


def materialize_target_implementation_bundle(
    bundle: TargetImplementationBundle,
    destination: Path,
) -> None:
    """Safely materialize a root-owned read-only tree with no link entries."""

    if type(bundle) is not TargetImplementationBundle:
        raise TargetImplementationBundleError()
    _prepare_empty_directory(destination)
    created_directories: set[Path] = {destination}
    try:
        for relative_directory in bundle.directories:
            directory = destination.joinpath(*relative_directory.split("/"))
            directory.mkdir(mode=0o555, parents=True, exist_ok=False)
            created_directories.add(directory)
        for entry in bundle.entries:
            target = destination.joinpath(*entry.relative_path.split("/"))
            parent = target.parent
            pending: list[Path] = []
            while parent != destination and not parent.exists():
                pending.append(parent)
                parent = parent.parent
            if parent != destination and not _is_directory_without_symlink(parent):
                raise TargetImplementationBundleError()
            for directory in reversed(pending):
                directory.mkdir(mode=0o555)
                created_directories.add(directory)
            with target.open("xb") as stream:
                os.fchmod(stream.fileno(), 0o555 if entry.mode & 0o111 else 0o444)
                stream.write(entry.content)
                stream.flush()
                os.fsync(stream.fileno())
            file_stat = target.lstat()
            if (
                not stat.S_ISREG(file_stat.st_mode)
                or file_stat.st_nlink != 1
                or target.read_bytes() != entry.content
            ):
                raise TargetImplementationBundleError()
        for directory in sorted(
            created_directories, key=lambda item: len(item.parts), reverse=True
        ):
            os.chmod(directory, 0o555)
    except (OSError, ValueError) as error:
        if isinstance(error, TargetImplementationBundleError):
            raise
        raise TargetImplementationBundleError() from error


def validate_bundle_relative_path(value: object) -> str:
    if type(value) is not str or not value:
        raise TargetImplementationBundleError()
    try:
        path_bytes = value.encode("utf-8")
    except UnicodeError as error:
        raise TargetImplementationBundleError(
            "target_implementation_workspace_entry_unsupported"
        ) from error
    if (
        len(path_bytes) > IMPLEMENTATION_PATH_MAX_BYTES
        or value != value.strip()
        or "\\" in value
        or "\x00" in value
        or "\r" in value
        or "\n" in value
        or value.startswith("/")
        or value.endswith("/")
        or "//" in value
    ):
        raise TargetImplementationBundleError()
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or str(path) != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise TargetImplementationBundleError()
    return value


def _rm_directory_content_manifest(
    directories: Iterable[str],
    entries: Iterable[TargetImplementationBundleEntry],
) -> dict[str, object]:
    return {
        "kind": "directory",
        "directories": list(directories),
        "entries": [entry.tree_value() for entry in entries],
    }


def _validate_tree_shape(directories: set[str], file_paths: list[str]) -> None:
    file_set = set(file_paths)
    if directories & file_set:
        raise TargetImplementationBundleError()
    for path in sorted(directories | file_set):
        components = path.split("/")
        for index in range(1, len(components)):
            prefix = "/".join(components[:index])
            if prefix in file_set:
                raise TargetImplementationBundleError()
            if prefix not in directories:
                # RM directory manifests enumerate every directory, including
                # non-empty parents, so omission is non-canonical.
                raise TargetImplementationBundleError()


def _prepare_empty_directory(destination: Path) -> None:
    try:
        if destination.exists() or destination.is_symlink():
            raise TargetImplementationBundleError()
        destination.mkdir(mode=0o555, parents=False)
        if not _is_directory_without_symlink(destination):
            raise TargetImplementationBundleError()
    except OSError as error:
        raise TargetImplementationBundleError() from error


def _is_directory_without_symlink(path: Path) -> bool:
    try:
        return stat.S_ISDIR(path.lstat().st_mode) and not path.is_symlink()
    except OSError:
        return False


def _validate_sha256(value: object) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise TargetImplementationBundleError()
    return value
