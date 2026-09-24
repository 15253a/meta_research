from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from meta_research import __version__


DATA_ROOT_FORMAT = 1


class DataRootError(RuntimeError):
    """The requested directory is not a compatible vNext data root."""


def shared_provider_tools_root() -> Path | None:
    """Deployment-level provider tool install shared by every data root.

    Resolved from ``META_RESEARCH_PROVIDER_TOOLS`` when set, otherwise from a
    ``provider-tools`` directory beside the running virtual environment (the
    release layout: ``release/provider-tools`` next to ``release/.venv``).
    The raw interpreter location is preferred over its resolved form because
    managed venvs symlink their python to a base interpreter several
    directories away.  Returns ``None`` when the location cannot be
    determined.
    """

    override = os.environ.get("META_RESEARCH_PROVIDER_TOOLS", "")
    if override:
        return Path(override)
    try:
        executable = Path(sys.executable)
        raw = executable.parent.parent.parent / "provider-tools"
        resolved = executable.resolve().parent.parent.parent / "provider-tools"
    except OSError:
        return None
    if (raw / "codex-cli").exists():
        return raw
    if (resolved / "codex-cli").exists():
        return resolved
    return raw


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
    def codex_user_home(self) -> Path:
        return self.codex_home / "home"

    @property
    def codex_config(self) -> Path:
        return self.codex_home / "config"

    @property
    def codex_data(self) -> Path:
        return self.codex_home / "data"

    @property
    def codex_state(self) -> Path:
        return self.codex_home / "state"

    @property
    def codex_sessions(self) -> Path:
        return self.codex_home / "sessions"

    @property
    def codex_archived_sessions(self) -> Path:
        return self.codex_home / "archived_sessions"

    @property
    def codex_cache(self) -> Path:
        return self.codex_home / "cache"

    @property
    def codex_tmp(self) -> Path:
        return self.codex_home / "tmp"

    @property
    def codex_npm_cache(self) -> Path:
        return self.codex_cache / "npm"

    @property
    def codex_uv_cache(self) -> Path:
        return self.codex_cache / "uv"

    @property
    def codex_pip_cache(self) -> Path:
        return self.codex_cache / "pip"

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

    def _codex_cli_install_candidates(
        self, version: str | None = None
    ) -> list[tuple[Path, Path]]:
        """Managed installs to try, most specific first: (executable, install root).

        The data-root install keeps precedence for roots that manage their own
        CLI; the deployment-level shared install (versioned, then plain) is the
        fallback so a fresh data root reuses the one CLI provisioned with the
        release instead of needing its own copy.
        """

        executable = "codex.cmd" if os.name == "nt" else "codex"
        relative = Path("node_modules") / ".bin" / executable
        candidates: list[tuple[Path, Path]] = [
            (self.codex_cli_install_root / relative, self.codex_cli_install_root)
        ]
        shared = shared_provider_tools_root()
        if shared is not None:
            shared_cli = shared / "codex-cli"
            if version:
                install_root = shared_cli / "installs" / version
                candidates.append((install_root / relative, install_root))
            candidates.append((shared_cli / relative, shared_cli))
        return candidates

    def validated_codex_cli_executable(self, version: str | None = None) -> Path:
        """Return the managed CLI path, rejecting an installed link that escapes it.

        A missing install returns the data-root default path unchanged:
        capability probing owns the normal "not installed" result, and the
        absolute managed path prevents a fallback to global PATH.
        """

        mismatched_installs = []
        for candidate, install_root in self._codex_cli_install_candidates(version):
            executable = candidate.absolute()
            try:
                executable.lstat()
            except FileNotFoundError:
                continue
            except OSError as error:
                raise DataRootError(
                    f"cannot inspect managed Codex executable: {executable}"
                ) from error
            try:
                resolved_install_root = install_root.resolve(strict=True)
                resolved = executable.resolve(strict=True)
            except OSError as error:
                raise DataRootError(
                    f"cannot resolve managed Codex executable: {executable}"
                ) from error
            if not resolved.is_relative_to(resolved_install_root) or (
                not resolved.is_file()
            ):
                raise DataRootError(
                    f"managed Codex executable escapes its install root: {executable}"
                )
            if os.name == "posix" and not os.access(resolved, os.X_OK):
                raise DataRootError(
                    f"managed Codex executable is not executable: {executable}"
                )
            if version:
                package = install_root / "node_modules/@openai/codex/package.json"
                try:
                    actual_version = json.loads(package.read_text())["version"]
                except (OSError, ValueError, KeyError, TypeError) as error:
                    raise DataRootError("managed Codex package metadata is invalid") from error
                if actual_version != version:
                    mismatched_installs.append(str(install_root))
                    continue
                manifest_path = install_root / "codex-install-manifest.json"
                if manifest_path.exists():
                    try:
                        manifest = json.loads(manifest_path.read_text())
                        if (manifest["schema"] != "meta-research/codex-cli-install/v1"
                            or manifest["version"] != version
                            or not manifest["files"]):
                            raise ValueError("invalid manifest")
                        for relative, expected_hash in manifest["files"].items():
                            path = (install_root / relative).resolve(strict=True)
                            if not path.is_relative_to(resolved_install_root):
                                raise ValueError("manifest path escapes install")
                            with path.open("rb") as stream:
                                actual_hash = hashlib.file_digest(stream, "sha256").hexdigest()
                            if actual_hash != expected_hash:
                                raise ValueError("manifest file hash mismatch")
                    except (OSError, ValueError, KeyError, TypeError) as error:
                        raise DataRootError("managed Codex install manifest mismatch") from error
            return executable
        if mismatched_installs:
            raise DataRootError(f"requested Codex version {version} is not installed")
        return self.codex_cli_executable.absolute()

    @property
    def codex_environment(self) -> dict[str, str]:
        managed_home = str(self.codex_home.absolute())
        managed_cache = str(self.codex_cache.absolute())
        managed_tmp = str(self.codex_tmp.absolute())
        return {
            "CODEX_HOME": managed_home,
            "CODEX_SQLITE_HOME": managed_home,
            "HOME": str(self.codex_user_home.absolute()),
            "USERPROFILE": str(self.codex_user_home.absolute()),
            "XDG_CONFIG_HOME": str(self.codex_config.absolute()),
            "XDG_DATA_HOME": str(self.codex_data.absolute()),
            "XDG_STATE_HOME": str(self.codex_state.absolute()),
            "XDG_CACHE_HOME": managed_cache,
            "NODE_PATH": "",
            "PIP_CACHE_DIR": str(self.codex_pip_cache.absolute()),
            "npm_config_cache": str(self.codex_npm_cache.absolute()),
            "UV_CACHE_DIR": str(self.codex_uv_cache.absolute()),
            "TMPDIR": managed_tmp,
            "TEMP": managed_tmp,
            "TMP": managed_tmp,
            "SQLITE_TMPDIR": managed_tmp,
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
        root.codex_user_home,
        root.codex_config,
        root.codex_data,
        root.codex_state,
        root.codex_sessions,
        root.codex_archived_sessions,
        root.codex_cache,
        root.codex_npm_cache,
        root.codex_uv_cache,
        root.codex_pip_cache,
        root.codex_tmp,
        root.provider_tools,
        root.codex_cli_install_root,
    ):
        _prepare_private_directory(directory)

    _seed_codex_home_auth(root)

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


def _seed_codex_home_auth(root: DataRoot) -> None:
    """Seed a fresh CODEX_HOME with the deployment's Codex credentials.

    Codex keeps its login inside ``CODEX_HOME/auth.json`` while sessions and
    queues stay per research root, so a new root copies the deployment-level
    credential template once and then owns its own state.  An existing auth
    file is never overwritten; a missing or unreadable template is a silent
    no-op (capability probing reports the unauthenticated provider).
    """

    auth = root.codex_home / "auth.json"
    try:
        if auth.exists():
            return
    except OSError:
        return
    template_override = os.environ.get("META_RESEARCH_CODEX_AUTH_TEMPLATE", "")
    candidates: list[Path] = []
    if template_override:
        candidates.append(Path(template_override))
    shared = shared_provider_tools_root()
    if shared is not None:
        candidates.append(shared / "codex-auth.json")
    for candidate in candidates:
        try:
            payload = candidate.read_bytes()
        except OSError:
            continue
        try:
            json.loads(payload)
        except ValueError:
            continue
        try:
            root.codex_home.mkdir(parents=True, exist_ok=True, mode=0o700)
            auth.write_bytes(payload)
            os.chmod(auth, 0o600)
        except OSError:
            return
        return


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
