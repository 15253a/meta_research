from __future__ import annotations

import json
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from meta_research import __version__


DATA_ROOT_FORMAT = 1


class DataRootError(RuntimeError):
    """The requested directory is not a compatible vNext data root."""


@dataclass(frozen=True)
class DataRoot:
    root: Path

    @property
    def marker(self) -> Path:
        return self.root / "data-root.json"

    @property
    def database(self) -> Path:
        return self.root / "meta-research.sqlite3"

    @property
    def objects(self) -> Path:
        return self.root / "objects" / "sha256"

    @property
    def object_store_marker(self) -> Path:
        return self.root / "objects" / "object-store.json"

    @property
    def run(self) -> Path:
        return self.root / "run"

    @property
    def runtime_state(self) -> Path:
        return self.run / "runtime.json"

    @property
    def daemon_lock(self) -> Path:
        return self.run / "daemon.lock"

    @property
    def control_key(self) -> Path:
        return self.run / "control.key"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def daemon_log(self) -> Path:
        return self.logs / "daemon.jsonl"

    @property
    def provider_homes(self) -> Path:
        return self.root / "provider-homes"

    @property
    def codex_home(self) -> Path:
        return self.provider_homes / "codex"

    @property
    def codex_sessions(self) -> Path:
        return self.codex_home / "sessions"

    @property
    def codex_archived_sessions(self) -> Path:
        return self.codex_home / "archived_sessions"

    @property
    def provider_tools(self) -> Path:
        return self.root / "provider-tools"

    @property
    def codex_cli_install_root(self) -> Path:
        return self.provider_tools / "codex-cli"

    @property
    def codex_cli_executable(self) -> Path:
        executable = "codex.cmd" if os.name == "nt" else "codex"
        return self.codex_cli_install_root / "node_modules" / ".bin" / executable

    @property
    def codex_environment(self) -> dict[str, str]:
        managed_home = str(self.codex_home.absolute())
        return {
            "CODEX_HOME": managed_home,
            "CODEX_SQLITE_HOME": managed_home,
        }


@dataclass(frozen=True)
class RuntimeState:
    status: Literal["running", "stopped"]
    pid: int
    host: str
    port: int
    base_url: str
    version: str
    started_at: float
    stopped_at: float | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RuntimeState":
        status = value["status"]
        if status not in {"running", "stopped"}:
            raise ValueError("runtime status is invalid")
        host = str(value["host"])
        if host not in {"127.0.0.1", "::1"}:
            raise ValueError("runtime host is not loopback")
        pid = int(value["pid"])
        port = int(value["port"])
        if pid <= 0 or not 1 <= port <= 65535:
            raise ValueError("runtime process identity is invalid")
        stopped_at = value.get("stopped_at")
        return cls(
            status=status,
            pid=pid,
            host=host,
            port=port,
            base_url=str(value["base_url"]),
            version=str(value["version"]),
            started_at=float(value["started_at"]),
            stopped_at=float(stopped_at) if stopped_at is not None else None,
        )

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "status": self.status,
            "pid": self.pid,
            "host": self.host,
            "port": self.port,
            "base_url": self.base_url,
            "version": self.version,
            "started_at": self.started_at,
        }
        if self.stopped_at is not None:
            value["stopped_at"] = self.stopped_at
        return value


def prepare_data_root(path: Path) -> DataRoot:
    root = DataRoot(path.expanduser().resolve())
    root.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        root.root.chmod(0o700)
    except OSError:
        pass

    existing = list(root.root.iterdir())
    if existing and not root.marker.exists():
        raise DataRootError(
            f"refusing non-empty directory without a vNext marker: {root.root}"
        )

    if root.marker.exists():
        marker = _read_json(root.marker)
        if marker.get("product") != "meta-research-vnext" or marker.get(
            "format"
        ) != DATA_ROOT_FORMAT:
            raise DataRootError(f"incompatible data root marker: {root.marker}")
    else:
        _write_json_exclusive(
            root.marker,
            {
                "product": "meta-research-vnext",
                "format": DATA_ROOT_FORMAT,
                "created_by_version": __version__,
            },
            mode=0o600,
        )

    for directory in (root.run, root.logs, root.objects):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    for directory in (
        root.provider_homes,
        root.codex_home,
        root.codex_sessions,
        root.codex_archived_sessions,
        root.provider_tools,
        root.codex_cli_install_root,
    ):
        _prepare_private_directory(directory)

    if not root.object_store_marker.exists():
        _write_json_exclusive(
            root.object_store_marker,
            {
                "store": "managed-content-addressed",
                "algorithm": "sha256",
                "format": 1,
            },
            mode=0o600,
        )

    if not root.control_key.exists():
        _write_exclusive(root.control_key, secrets.token_urlsafe(48), mode=0o600)
    return root


def _prepare_private_directory(path: Path) -> None:
    """Create a managed private directory without accepting link indirection."""

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        try:
            path.mkdir(mode=0o700)
            metadata = path.lstat()
        except OSError as error:
            raise DataRootError(f"cannot create managed directory: {path}") from error
    except OSError as error:
        raise DataRootError(f"cannot inspect managed directory: {path}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise DataRootError(f"managed directory is not a real directory: {path}")
    try:
        path.chmod(0o700)
    except OSError as error:
        if os.name == "posix":
            raise DataRootError(f"cannot protect managed directory: {path}") from error


def read_control_key(root: DataRoot) -> str:
    return root.control_key.read_text(encoding="utf-8").strip()


def read_runtime_state(root: DataRoot) -> RuntimeState | None:
    if not root.runtime_state.exists():
        return None
    try:
        return RuntimeState.from_dict(_read_json(root.runtime_state))
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def write_runtime_state(root: DataRoot, value: RuntimeState) -> None:
    temporary = root.runtime_state.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(value.as_dict(), ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    os.replace(temporary, root.runtime_state)


def append_daemon_event(root: DataRoot, event: dict[str, Any]) -> None:
    # Keep the compatibility entry point narrow: callers cannot smuggle
    # prompts, paths, stdout or exception text into ordinary daemon logs.
    from meta_research.runtime_protection import RuntimeEventLogger

    event_code = event.get("event")
    RuntimeEventLogger(root.daemon_log).record(
        event_code=(
            event_code if isinstance(event_code, str) else "daemon.event.invalid"
        ),
        status=(
            "starting"
            if event_code == "daemon.starting"
            else "ready"
            if event_code == "daemon.ready"
            else "stopped"
            if event_code == "daemon.stopped"
            else "unknown"
        ),
        component="daemon",
    )


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected an object in {path}")
    return value


def _write_json_exclusive(path: Path, value: dict[str, Any], *, mode: int) -> None:
    _write_exclusive(
        path,
        json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n",
        mode=mode,
    )


def _write_exclusive(path: Path, value: str, *, mode: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(value)
