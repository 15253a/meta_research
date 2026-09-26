"""Deployment-owned MCP registrations and operation-scoped native configuration.

The registry stores references to deployment credentials, never their values.
Snapshots are JSON values owned by a provider operation, not a long-lived Run.
"""
from __future__ import annotations

import json
import hashlib
import math
import os
import re
import tempfile
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from meta_research.root_capabilities import ROOT_AGENT_KINDS


SNAPSHOT_SCHEMA = "meta-research/system-mcp-snapshot/v1"
NATIVE_MCP_PROFILE_NAME = "meta-research-system-mcp-v1"
_NATIVE_MCP_PROFILE = "mcp_servers = []\nprojects = []\n"
DEFAULT_STARTUP_TIMEOUT_SEC = 10
MAX_STARTUP_TIMEOUT_SEC = 60
DEFAULT_TOOL_TIMEOUT_SEC = 60
MAX_TOOL_TIMEOUT_SEC = 600
_ID = re.compile(r"[a-z][a-z0-9_-]{0,47}\Z")
_ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_HEADER = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_SENSITIVE = re.compile(r"(?:token|secret|password|credential|api[_-]?key|authorization|cookie)", re.I)


class SystemMcpError(RuntimeError):
    """A safe-to-display MCP configuration failure."""


class SystemMcpValidationError(SystemMcpError, ValueError):
    pass


class SystemMcpConflictError(SystemMcpError):
    pass


class SystemMcpLoadError(SystemMcpError):
    pass


def _object(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not set(value) <= fields:
        raise SystemMcpValidationError(f"system_mcp_invalid_{label}")
    return value


def _text(value: Any, label: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or "\0" in value or (not empty and not value.strip()):
        raise SystemMcpValidationError(f"system_mcp_invalid_{label}")
    return value


def _integer(value: Any, label: str) -> int:
    if type(value) is not int or value < 0:
        raise SystemMcpValidationError(f"system_mcp_invalid_{label}")
    return value


def _env_name(value: Any) -> str:
    value = _text(value, "environment_reference")
    if not _ENV.fullmatch(value) or value.upper().startswith("META_RESEARCH_"):
        raise SystemMcpValidationError("system_mcp_invalid_environment_reference")
    return value


def _strings(value: Any, label: str) -> list[str]:
    if not isinstance(value, list):
        raise SystemMcpValidationError(f"system_mcp_invalid_{label}")
    return [_text(item, label, empty=True) for item in value]


def _mapping(value: Any, label: str, *, references: bool = False, environment: bool = False) -> dict[str, str]:
    if not isinstance(value, dict):
        raise SystemMcpValidationError(f"system_mcp_invalid_{label}")
    result = {}
    for key, item in value.items():
        key = _env_name(key) if environment else _text(key, label)
        if not environment and (not _HEADER.fullmatch(key) or key.lower() in {existing.lower() for existing in result}):
            raise SystemMcpValidationError(f"system_mcp_invalid_{label}")
        if not references and _SENSITIVE.search(key):
            raise SystemMcpValidationError("system_mcp_sensitive_value_requires_environment_reference")
        item = _env_name(item) if references else _text(item, label, empty=True)
        if not environment and ("\r" in item or "\n" in item):
            raise SystemMcpValidationError(f"system_mcp_invalid_{label}")
        result[key] = item
    return result


def _timeout(value: Any, maximum: int) -> int | float:
    if type(value) not in (int, float) or not math.isfinite(value) or not 1 <= value <= maximum:
        raise SystemMcpValidationError("system_mcp_invalid_timeout")
    return value


def validate_registration(value: Any) -> dict[str, Any]:
    value = _object(value, {"server_id", "display_name", "description", "enabled", "scope", "transport", "connection", "revision", "startup_timeout_sec", "tool_timeout_sec"}, "registration")
    server_id = _text(value.get("server_id"), "server_id")
    if not _ID.fullmatch(server_id) or server_id == "meta_research":
        raise SystemMcpValidationError("system_mcp_invalid_server_id")
    enabled = value.get("enabled", True)
    if type(enabled) is not bool:
        raise SystemMcpValidationError("system_mcp_invalid_enabled")
    scope = _object(value.get("scope", {"mode": "all"}), {"mode", "root_kinds"}, "scope")
    if scope.get("mode") == "all" and set(scope) == {"mode"}:
        scope = {"mode": "all"}
    elif scope.get("mode") == "root_kinds" and set(scope) == {"mode", "root_kinds"}:
        roots = _strings(scope["root_kinds"], "root_kinds")
        if not roots or len(set(roots)) != len(roots) or not set(roots) <= set(ROOT_AGENT_KINDS):
            raise SystemMcpValidationError("system_mcp_invalid_root_kinds")
        scope = {"mode": "root_kinds", "root_kinds": sorted(roots)}
    else:
        raise SystemMcpValidationError("system_mcp_invalid_scope")
    transport = value.get("transport")
    if transport == "stdio":
        raw = _object(value.get("connection"), {"command", "args", "cwd", "env", "env_vars"}, "stdio_connection")
        connection: dict[str, Any] = {
            "command": _text(raw.get("command"), "command"),
            "args": _strings(raw.get("args", []), "args"),
            "env": _mapping(raw.get("env", {}), "environment", environment=True),
            "env_vars": [_env_name(name) for name in _strings(raw.get("env_vars", []), "environment_references")],
        }
        if len(set(connection["env_vars"])) != len(connection["env_vars"]) or set(connection["env"]) & set(connection["env_vars"]):
            raise SystemMcpValidationError("system_mcp_conflicting_environment")
        if "cwd" in raw:
            cwd = _text(raw["cwd"], "working_directory")
            if not Path(cwd).is_absolute():
                raise SystemMcpValidationError("system_mcp_working_directory_must_be_absolute")
            connection["cwd"] = str(Path(cwd).resolve())
    elif transport == "streamable_http":
        from urllib.parse import parse_qsl, urlsplit
        raw = _object(value.get("connection"), {"url", "http_headers", "env_http_headers", "bearer_token_env_var"}, "http_connection")
        url = _text(raw.get("url"), "url")
        try:
            parsed = urlsplit(url)
            parsed.port
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment or any(character.isspace() for character in url) or any(_SENSITIVE.search(name) for name, _ in parse_qsl(parsed.query)):
                raise ValueError
        except ValueError as error:
            raise SystemMcpValidationError("system_mcp_invalid_url") from error
        connection = {
            "url": url,
            "http_headers": _mapping(raw.get("http_headers", {}), "headers"),
            "env_http_headers": _mapping(raw.get("env_http_headers", {}), "header_references", references=True),
        }
        headers = {name.lower() for name in connection["http_headers"]}
        ref_headers = {name.lower() for name in connection["env_http_headers"]}
        if headers & ref_headers:
            raise SystemMcpValidationError("system_mcp_conflicting_headers")
        if "bearer_token_env_var" in raw:
            if "authorization" in ref_headers:
                raise SystemMcpValidationError("system_mcp_conflicting_authentication")
            connection["bearer_token_env_var"] = _env_name(raw["bearer_token_env_var"])
    else:
        raise SystemMcpValidationError("system_mcp_invalid_transport")
    result = {
        "server_id": server_id,
        "display_name": _text(value.get("display_name"), "display_name"),
        "description": _text(value.get("description", ""), "description", empty=True),
        "enabled": enabled, "scope": scope, "transport": transport, "connection": connection,
        "startup_timeout_sec": _timeout(value.get("startup_timeout_sec", DEFAULT_STARTUP_TIMEOUT_SEC), MAX_STARTUP_TIMEOUT_SEC),
        "tool_timeout_sec": _timeout(value.get("tool_timeout_sec", DEFAULT_TOOL_TIMEOUT_SEC), MAX_TOOL_TIMEOUT_SEC),
    }
    if "revision" in value:
        result["revision"] = _integer(value["revision"], "revision")
    return result


def _hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _selected(server: dict[str, Any], root_kind: str) -> bool:
    scope = server["scope"]
    return server["enabled"] and (scope["mode"] == "all" or root_kind in scope["root_kinds"])


def validate_snapshot(snapshot: Any) -> dict[str, Any]:
    fields = {"schema", "registry_revision", "root_kind", "servers", "snapshot_id"}
    snapshot = _object(snapshot, fields, "snapshot")
    if set(snapshot) != fields or snapshot["schema"] != SNAPSHOT_SCHEMA or snapshot["root_kind"] not in ROOT_AGENT_KINDS:
        raise SystemMcpValidationError("system_mcp_invalid_snapshot")
    revision = _integer(snapshot["registry_revision"], "snapshot_revision")
    if not isinstance(snapshot["servers"], list):
        raise SystemMcpValidationError("system_mcp_invalid_snapshot_servers")
    ids = set()
    for server in snapshot["servers"]:
        normalized = validate_registration(server)
        if normalized != server or not _selected(server, snapshot["root_kind"]) or not 0 < server.get("revision", 0) <= revision or server["server_id"] in ids:
            raise SystemMcpValidationError("system_mcp_invalid_snapshot_server")
        ids.add(server["server_id"])
    payload = {key: snapshot[key] for key in fields - {"snapshot_id"}}
    if _hash(payload) != snapshot["snapshot_id"]:
        raise SystemMcpValidationError("system_mcp_snapshot_identity_mismatch")
    return deepcopy(snapshot)


def _toml(value: Any) -> str:
    if isinstance(value, dict):
        return "{" + ",".join(f"{json.dumps(key)}={_toml(item)}" for key, item in value.items()) + "}"
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def compile_snapshot(snapshot: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    """Compile an isolated MCP table; callers merge their internal channel after it.

    Codex's stdio allowlist excludes arbitrary parent environment variables.
    Only explicitly listed deployment references are forwarded to those servers.
    The named managed profile resets inherited maps in an earlier native config
    layer. An empty CLI table alone only deep-merges and does not clear them.
    Do not use native --strict-config with this profile: it type-checks each
    intermediate layer before the CLI restores the final, valid map values.
    """
    snapshot = validate_snapshot(snapshot)
    arguments: list[str] = [
        "--profile", NATIVE_MCP_PROFILE_NAME,
        "--config", "projects={}",
        "--config", "mcp_servers={}",
    ]
    sensitive_environment: dict[str, str] = {}
    for server in snapshot["servers"]:
        name = "external_" + server["server_id"]
        connection = server["connection"]
        native = dict(connection, required=False, startup_timeout_sec=server["startup_timeout_sec"], tool_timeout_sec=server["tool_timeout_sec"], default_tools_approval_mode="approve")
        references = set(connection.get("env_vars", [])) | set(connection.get("env_http_headers", {}).values())
        if connection.get("bearer_token_env_var"):
            references.add(connection["bearer_token_env_var"])
        for reference in references:
            if reference in os.environ:
                sensitive_environment[reference] = os.environ[reference]
        for key, item in native.items():
            arguments.extend(["--config", f"mcp_servers.{name}.{key}={_toml(item)}"])
    return arguments, sensitive_environment


@contextmanager
def _locked(path: Path):
    descriptor = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        if os.name == "nt":
            import msvcrt
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    except OSError as error:
        raise SystemMcpLoadError("system_mcp_storage_unavailable") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _write_json(path: Path, value: Any) -> None:
    temporary: str | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=".system-mcp-", dir=path.parent)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        if os.name != "nt":
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    except OSError as error:
        raise SystemMcpLoadError("system_mcp_save_failed") from error
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def ensure_native_mcp_profile(codex_home: Path | str) -> None:
    """Install the immutable profile in an explicitly supplied, managed home.

    User config, auth, skills, plugins and native session files are untouched.
    The profile suppresses project-local config layers during native bootstrap;
    explicit AR capability and per-operation MCP overrides remain authoritative.
    Never infer another user's global CODEX_HOME from this helper.
    """
    home = Path(codex_home).absolute()
    profile = home / (NATIVE_MCP_PROFILE_NAME + ".config.toml")
    with _locked(home / ".system-mcp-profile.lock"):
        if profile.is_symlink():
            raise SystemMcpLoadError("system_mcp_native_profile_conflict")
        try:
            existing = profile.read_text(encoding="utf-8")
        except FileNotFoundError:
            existing = None
        except (OSError, UnicodeError) as error:
            raise SystemMcpLoadError("system_mcp_native_profile_unavailable") from error
        if existing is not None:
            if existing != _NATIVE_MCP_PROFILE:
                raise SystemMcpLoadError("system_mcp_native_profile_conflict")
            return
        temporary = None
        try:
            descriptor, temporary = tempfile.mkstemp(prefix=".system-mcp-profile-", dir=home)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(_NATIVE_MCP_PROFILE)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o400)
            os.replace(temporary, profile)
            temporary = None
            if os.name != "nt":
                descriptor = os.open(home, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        except OSError as error:
            raise SystemMcpLoadError("system_mcp_native_profile_unavailable") from error
        finally:
            if temporary is not None:
                os.chmod(temporary, 0o600)
                Path(temporary).unlink(missing_ok=True)


class SystemMcpRegistry:
    def __init__(self, path: Path):
        self.path = Path(path).absolute()

    def read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"revision": 0, "servers": []}
        except (OSError, ValueError) as error:
            raise SystemMcpLoadError("system_mcp_registry_load_failed") from error
        try:
            if not isinstance(value, dict) or set(value) != {"revision", "servers"} or not isinstance(value["servers"], list):
                raise ValueError
            revision = _integer(value["revision"], "revision")
            ids = set()
            for server in value["servers"]:
                normalized = validate_registration(server)
                if normalized != server or server["server_id"] in ids or not 0 < server.get("revision", 0) <= revision:
                    raise ValueError
                ids.add(server["server_id"])
        except (ValueError, TypeError, KeyError) as error:
            raise SystemMcpLoadError("system_mcp_registry_invalid") from error
        return value

    def create(self, config: dict[str, Any], *, expected_revision: int) -> dict[str, Any]:
        config = validate_registration(config)
        if config["transport"] == "stdio":
            config["connection"].setdefault("cwd", str(self.path.parent.resolve()))
        return self._mutate("create", config["server_id"], config, expected_revision)

    def update(self, server_id: str, config: dict[str, Any], *, expected_revision: int) -> dict[str, Any]:
        config = validate_registration(config)
        if config["server_id"] != server_id:
            raise SystemMcpValidationError("system_mcp_server_id_is_immutable")
        if config["transport"] == "stdio":
            config["connection"].setdefault("cwd", str(self.path.parent.resolve()))
        return self._mutate("update", server_id, config, expected_revision)

    def delete(self, server_id: str, *, expected_revision: int) -> dict[str, Any]:
        return self._mutate("delete", server_id, None, expected_revision)

    def _mutate(self, action, server_id, config, expected_revision):
        _integer(expected_revision, "expected_revision")
        with _locked(self.path.with_suffix(self.path.suffix + ".lock")):
            current = self.read()
            if current["revision"] != expected_revision:
                raise SystemMcpConflictError("system_mcp_revision_conflict")
            servers = {item["server_id"]: item for item in current["servers"]}
            if action == "create" and server_id in servers:
                raise SystemMcpConflictError("system_mcp_server_already_exists")
            if action != "create" and server_id not in servers:
                raise SystemMcpConflictError("system_mcp_server_not_found")
            revision = current["revision"] + 1
            if config is None:
                del servers[server_id]
            else:
                servers[server_id] = dict(config, revision=revision)
            result = {"revision": revision, "servers": [servers[key] for key in sorted(servers)]}
            _write_json(self.path, result)
            return result

    def snapshot(self, root_kind: str) -> dict[str, Any]:
        if root_kind not in ROOT_AGENT_KINDS:
            raise SystemMcpValidationError("system_mcp_invalid_root_kind")
        registry = self.read()
        result = {"schema": SNAPSHOT_SCHEMA, "registry_revision": registry["revision"], "root_kind": root_kind, "servers": [server for server in registry["servers"] if _selected(server, root_kind)]}
        return dict(result, snapshot_id=_hash(result))

    def record_loading(self, snapshot: dict[str, Any], operation_id: str, native_session_ref: str | None = None) -> None:
        """Best-effort projection of selection, deliberately not connection evidence."""
        snapshot = validate_snapshot(snapshot)
        path = self.path.with_suffix(".loads.json")
        try:
            with _locked(path.with_suffix(".lock")):
                try:
                    previous = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    previous = []
                if not isinstance(previous, list):
                    previous = []
                record = {
                    "operation_id": operation_id, "native_session_ref": native_session_ref,
                    "root_kind": snapshot["root_kind"], "registry_revision": snapshot["registry_revision"],
                    "snapshot_id": snapshot["snapshot_id"],
                    "server_ids": [server["server_id"] for server in snapshot["servers"]],
                    "status": "selected", "connection_status": "unknown",
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                }
                records = [item for item in previous if isinstance(item, dict) and item.get("operation_id") != operation_id]
                _write_json(path, (records + [record])[-100:])
        except (OSError, SystemMcpError):
            pass

    def load_status(self) -> list[dict[str, Any]]:
        try:
            value = json.loads(self.path.with_suffix(".loads.json").read_text(encoding="utf-8"))
            return value if isinstance(value, list) else []
        except (OSError, ValueError):
            return []

    def check_snapshot(self, server_id: str, *, expected_revision: int) -> dict[str, Any]:
        """Select exactly one registration for an explicitly requested check."""
        _integer(expected_revision, "expected_revision")
        registry = self.read()
        if registry["revision"] != expected_revision:
            raise SystemMcpConflictError("system_mcp_revision_conflict")
        server = next((item for item in registry["servers"] if item["server_id"] == server_id), None)
        if server is None:
            raise SystemMcpConflictError("system_mcp_server_not_found")
        server = dict(server, enabled=True)
        root_kind = "companion" if server["scope"]["mode"] == "all" else server["scope"]["root_kinds"][0]
        result = {"schema": SNAPSHOT_SCHEMA, "registry_revision": registry["revision"], "root_kind": root_kind, "servers": [server]}
        return dict(result, snapshot_id=_hash(result))

    def record_check(self, result: dict[str, Any]) -> None:
        path = self.path.with_suffix(".checks.json")
        with _locked(path.with_suffix(".checks.lock")):
            records = [item for item in self.check_status() if item["server_id"] != result["server_id"]]
            _write_json(path, (records + [deepcopy(result)])[-100:])

    def check_status(self) -> list[dict[str, Any]]:
        try:
            value = json.loads(self.path.with_suffix(".checks.json").read_text(encoding="utf-8"))
            return value if isinstance(value, list) else []
        except (OSError, ValueError):
            return []
