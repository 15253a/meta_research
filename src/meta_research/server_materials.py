"""Read-only, bounded access to originals on the Meta process host."""
from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass
import errno
import getpass
import os
from pathlib import Path
import platform
import socket
import stat
import threading
import time
import uuid

from meta_research.owners.common import OwnerConflict, canonical_hash


def _error(error: OSError) -> OwnerConflict:
    code = {errno.ENOENT: "material_missing", errno.EACCES: "material_not_readable",
            errno.EPERM: "material_not_readable", errno.ELOOP: "material_unsafe",
            errno.ENOTDIR: "material_unsafe"}.get(error.errno, "material_unavailable")
    return OwnerConflict(code)


def relative_parts(path: str) -> tuple[str, ...]:
    if not isinstance(path, str) or len(path) > 4096 or "\\" in path or "\x00" in path:
        raise OwnerConflict("material_path_invalid")
    if path == "":
        return ()
    parts = tuple(path.split("/"))
    if len(parts) > 64 or any(part in {"", ".", ".."} for part in parts) or ":" in parts[0]:
        raise OwnerConflict("material_path_invalid")
    return parts


def observation(value: os.stat_result) -> dict[str, object]:
    kind = "file" if stat.S_ISREG(value.st_mode) else "directory" if stat.S_ISDIR(value.st_mode) else "unsupported"
    result = {"device": str(value.st_dev), "inode": str(value.st_ino), "kind": kind,
              "size": str(value.st_size), "modified_ns": str(value.st_mtime_ns), "changed_ns": str(value.st_ctime_ns)}
    return {**result, "observation_ref": canonical_hash(result)}


@dataclass
class _DirectoryCursor:
    actor: str
    path: str
    descriptor: int
    iterator: object
    observed: dict[str, object]
    expires_at: float
    pending: object | None = None

    def close(self) -> None:
        self.iterator.close()
        os.close(self.descriptor)


class ServerFiles:
    def __init__(self) -> None:
        self._cursors: dict[str, _DirectoryCursor] = {}
        self._lock = threading.RLock()
        self.server = None
        if os.name == "posix" and hasattr(os, "O_NOFOLLOW"):
            try:
                machine = Path("/etc/machine-id").read_text().strip()
                if machine:
                    self.server = {"server_ref": "server:" + canonical_hash({"machine": machine}),
                                   "hostname": socket.gethostname(), "platform": platform.system(),
                                   "permission_context": getpass.getuser()}
            except OSError:
                pass

    @contextmanager
    def _open(self, path: str):
        if self.server is None:
            raise OwnerConflict("material_safe_reader_unavailable")
        if not isinstance(path, str) or not path.startswith("/") or len(path) > 16000:
            raise OwnerConflict("material_path_invalid")
        parts = relative_parts(path[1:])
        descriptors = []
        try:
            parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
            descriptors.append(parent)
            for part in parts[:-1]:
                parent = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                descriptors.append(parent)
            leaf = parent if not parts else os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            if parts:
                descriptors.append(leaf)
            value = os.fstat(leaf)
            if not stat.S_ISREG(value.st_mode) and not stat.S_ISDIR(value.st_mode):
                raise OwnerConflict("material_unsafe")
            yield leaf, observation(value)
        except OSError as error:
            raise _error(error) from error
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def inspect(self, path: str, *, description: str = "") -> dict[str, object]:
        with self._open(path) as (_, observed):
            return {"server": self.server, "absolute_path": path, "kind": observed["kind"],
                    "description": description, "observation": observed, "availability": "available"}

    def validate_selection(self, selection: dict[str, object]) -> dict[str, object]:
        if not isinstance(selection, dict) or set(selection) != {"server", "absolute_path", "kind", "description", "observation", "availability"}:
            raise OwnerConflict("material_selection_invalid")
        description = selection["description"]
        if not isinstance(description, str) or len(description) > 4000:
            raise OwnerConflict("material_selection_invalid")
        current = self.inspect(selection["absolute_path"], description=description)
        if current != selection:
            raise OwnerConflict("material_source_changed")
        return current

    def _expire(self) -> None:
        now = time.monotonic()
        for key, cursor in tuple(self._cursors.items()):
            if cursor.expires_at <= now:
                cursor.close()
                del self._cursors[key]

    def browse(self, *, actor: str, path: str, cursor: str | None = None,
               limit: int = 50) -> dict[str, object]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise OwnerConflict("material_page_invalid")
        with self._lock:
            self._expire()
            if cursor:
                held = self._cursors.get(cursor)
                if held is None or (held.actor, held.path) != (actor, path):
                    raise OwnerConflict("material_cursor_expired")
            else:
                if len(self._cursors) >= 128:
                    raise OwnerConflict("material_cursor_capacity")
                with self._open(path) as (descriptor, observed):
                    if observed["kind"] != "directory":
                        raise OwnerConflict("material_directory_required")
                    descriptor = os.dup(descriptor)
                held = _DirectoryCursor(actor, path, descriptor, os.scandir(descriptor), observed, time.monotonic() + 60)
                cursor = uuid.uuid4().hex
                self._cursors[cursor] = held
            try:
                with self._open(path) as (_, fresh):
                    if fresh != held.observed:
                        raise OwnerConflict("material_namespace_changed")
                entries = []
                while len(entries) < limit:
                    entry = held.pending or next(held.iterator, None)
                    held.pending = None
                    if entry is None:
                        break
                    absolute = path.rstrip("/") + "/" + entry.name
                    try:
                        value = observation(entry.stat(follow_symlinks=False))
                        availability = "available" if value["kind"] != "unsupported" else "unsafe"
                        entries.append({"name": entry.name, "absolute_path": absolute, "kind": value["kind"],
                                        "observation": value, "availability": availability})
                    except OSError as error:
                        entries.append({"name": entry.name, "absolute_path": absolute,
                                        "kind": "unknown", "availability": _error(error).code})
                held.pending = next(held.iterator, None)
                if observation(os.fstat(held.descriptor)) != held.observed:
                    raise OwnerConflict("material_namespace_changed")
                next_cursor = cursor if held.pending is not None else None
                if next_cursor is None:
                    held.close()
                    del self._cursors[cursor]
                else:
                    held.expires_at = time.monotonic() + 60
                return {"server": self.server, "absolute_path": path, "entries": entries,
                        "next_cursor": next_cursor, "unexpanded": True}
            except (OSError, OwnerConflict) as error:
                held.close()
                del self._cursors[cursor]
                if isinstance(error, OSError):
                    raise _error(error) from error
                raise

    def cancel(self, *, actor: str, cursor: str) -> dict[str, object]:
        with self._lock:
            held = self._cursors.get(cursor)
            if held is not None and held.actor == actor:
                held.close()
                del self._cursors[cursor]
        return {"cancelled": True}

    def _referenced_path(self, selection, path):
        relative_parts(path)
        with self._open(selection["absolute_path"]) as (_, current):
            original = selection["observation"]
            if self.server != selection["server"] or any(current[key] != original[key] for key in ("device", "inode", "kind")):
                raise OwnerConflict("material_source_changed")
            if current["kind"] == "file" and path:
                raise OwnerConflict("material_path_invalid")
        return selection["absolute_path"].rstrip("/") + ("/" + path if path else "") or "/"

    @contextmanager
    def _open_reference(self, selection, path):
        parts = relative_parts(path)
        descriptors = []
        try:
            with self._open(selection["absolute_path"]) as (root, current):
                original = selection["observation"]
                if self.server != selection["server"] or any(current[key] != original[key] for key in ("device", "inode", "kind")):
                    raise OwnerConflict("material_source_changed")
                if parts and current["kind"] != "directory":
                    raise OwnerConflict("material_path_invalid")
                parent = root
                for part in parts[:-1]:
                    parent = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                    descriptors.append(parent)
                leaf = root if not parts else os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                if parts:
                    descriptors.append(leaf)
                observed = observation(os.fstat(leaf))
                if observed["kind"] == "unsupported":
                    raise OwnerConflict("material_unsafe")
                yield leaf, observed
                with self._open(selection["absolute_path"]) as (_, fresh):
                    if any(fresh[key] != original[key] for key in ("device", "inode", "kind")):
                        raise OwnerConflict("material_source_changed")
        except OSError as error:
            raise _error(error) from error
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def discover(self, selection, *, actor, path="", cursor=None, limit=50):
        absolute = self._referenced_path(selection, path)
        with self._open_reference(selection, path) as (_, observed):
            current = self.inspect(absolute)
            if current["observation"] != observed:
                raise OwnerConflict("material_source_changed")
            if current["kind"] == "file":
                result = {"server": self.server, "path": path, "entries": [{"path": path, **current}],
                          "next_cursor": None, "unexpanded": False}
            else:
                result = self.browse(actor=actor, path=absolute, cursor=cursor, limit=limit)
                result["path"] = path
                result["entries"] = [{**entry, "path": (path + "/" if path else "") + entry["name"]} for entry in result["entries"]]
            if self.inspect(absolute)["observation"] != observed:
                if result["next_cursor"]:
                    self.cancel(actor=actor, cursor=result["next_cursor"])
                raise OwnerConflict("material_source_changed")
        return result

    def read(self, selection, *, path: str, observation_ref: str, offset=0, max_bytes=65536):
        if type(offset) is not int or not 0 <= offset <= 2**63 - 1 or type(max_bytes) is not int or not 1 <= max_bytes <= 65536:
            raise OwnerConflict("material_range_invalid")
        absolute = self._referenced_path(selection, path)
        with self._open_reference(selection, path) as (descriptor, before):
            if before["kind"] != "file":
                raise OwnerConflict("material_file_required")
            if before["observation_ref"] != observation_ref:
                raise OwnerConflict("material_source_changed")
            content = os.pread(descriptor, max_bytes, offset)
            if observation(os.fstat(descriptor)) != before:
                raise OwnerConflict("material_source_changed")
            with self._open(absolute) as (_, after):
                if after != before:
                    raise OwnerConflict("material_source_changed")
        result = {"server": self.server, "path": path, "observation": before, "offset": offset,
                  "bytes": len(content), "next_offset": offset + len(content),
                  "eof": offset + len(content) >= int(before["size"]), "availability": "available"}
        try:
            return {**result, "encoding": "utf-8", "text": content.decode("utf-8")}
        except UnicodeDecodeError:
            return {**result, "encoding": "base64", "base64": base64.b64encode(content).decode("ascii")}

    def close(self):
        with self._lock:
            for cursor in self._cursors.values():
                cursor.close()
            self._cursors.clear()
