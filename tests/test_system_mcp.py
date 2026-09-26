from __future__ import annotations

import pytest
import json
import os
import tomllib

from meta_research.root_capabilities import ROOT_AGENT_KINDS

from meta_research.system_mcp import (
    SystemMcpRegistry, SystemMcpConflictError, SystemMcpValidationError,
    SystemMcpLoadError, compile_snapshot, validate_snapshot,
)


def fixture_config(**changes):
    config = {
        "server_id": "fixture",
        "display_name": "Fixture",
        "transport": "stdio",
        "connection": {"command": "python", "args": ["-V"]},
    }
    config.update(changes)
    return config


def test_registry_persists_management_and_rejects_stale_writes(tmp_path):
    path = tmp_path / "system-mcp.json"
    registry = SystemMcpRegistry(path)
    assert registry.read() == {"revision": 0, "servers": []}
    assert not path.exists()
    registry.create(fixture_config(), expected_revision=0)
    restarted = SystemMcpRegistry(path)
    assert restarted.read()["servers"][0]["enabled"] is True
    with pytest.raises(SystemMcpConflictError):
        restarted.update("fixture", fixture_config(enabled=False), expected_revision=0)
    assert restarted.read()["revision"] == 1
    restarted.update("fixture", fixture_config(enabled=False), expected_revision=1)
    assert registry.read()["servers"][0]["enabled"] is False
    restarted.delete("fixture", expected_revision=2)
    assert registry.read() == {"revision": 3, "servers": []}


@pytest.mark.parametrize("changes", [
    {"server_id": "meta_research"},
    {"server_id": "bad.name"},
    {"enabled": "false"},
    {"scope": {"mode": "root_kinds", "root_kinds": []}},
    {"scope": {"mode": "root_kinds", "root_kinds": ["unknown"]}},
    {"scope": {"mode": "all", "root_kinds": ["idea"]}},
    {"transport": "sse"},
    {"connection": {"command": "python", "args": "-V"}},
    {"connection": {"command": "python", "cwd": "relative"}},
    {"connection": {"command": "python", "url": "https://invalid.example"}},
    {"connection": {"command": "python", "env_vars": ["META_RESEARCH_MCP_TOKEN"]}},
    {"connection": {"command": "python", "env": {"ACCESS_TOKEN": "literal-secret"}}},
    {"transport": "streamable_http", "connection": {"url": "https://user:secret@example.invalid/mcp"}},
    {"transport": "streamable_http", "connection": {"url": "https://example.invalid/mcp?token=literal-secret"}},
    {"transport": "streamable_http", "connection": {"url": "https://example.invalid/mcp", "http_headers": {"Authorization": "Bearer literal-secret"}}},
    {"transport": "streamable_http", "connection": {"url": "https://example.invalid/mcp", "http_headers": {"Invalid Header": "value"}}},
    {"transport": "streamable_http", "connection": {"url": "https://example.invalid/mcp", "http_headers": {"X-Header": "one", "x-header": "two"}}},
    {"startup_timeout_sec": float("inf")},
    {"startup_timeout_sec": True},
    {"tool_timeout_sec": 601},
    {"unrecognized": True},
])
def test_invalid_registration_preserves_prior_valid_configuration(tmp_path, changes):
    registry = SystemMcpRegistry(tmp_path / "system-mcp.json")
    original = registry.create(fixture_config(), expected_revision=0)
    with pytest.raises(SystemMcpValidationError):
        registry.update("fixture", fixture_config(**changes), expected_revision=1)
    assert registry.read() == original


def test_registry_corruption_fails_closed(tmp_path):
    path = tmp_path / "system-mcp.json"
    path.write_text('{"revision":0,"servers":"wrong"}', encoding="utf-8")
    with pytest.raises(SystemMcpLoadError):
        SystemMcpRegistry(path).read()


def test_frozen_operation_survives_removal_and_broken_registry(tmp_path, monkeypatch):
    registry = SystemMcpRegistry(tmp_path / "system-mcp.json")
    registry.create(fixture_config(), expected_revision=0)
    frozen = registry.snapshot("idea")
    registry.delete("fixture", expected_revision=1)
    assert registry.snapshot("idea")["servers"] == []
    registry.path.write_text("broken", encoding="utf-8")
    with pytest.raises(SystemMcpLoadError):
        registry.snapshot("idea")
    arguments, _ = compile_snapshot(frozen)
    assert any('mcp_servers.external_fixture.command="python"' == item for item in arguments)
    registry.record_loading(frozen, "operation-1")
    assert registry.load_status()[0]["registry_revision"] == 1
    assert registry.load_status()[0]["connection_status"] == "unknown"
    frozen["servers"][0]["connection"]["command"] = "other"
    with pytest.raises(SystemMcpValidationError):
        validate_snapshot(frozen)


def test_authoritative_roots_receive_only_selected_enabled_services(tmp_path):
    registry = SystemMcpRegistry(tmp_path / "system-mcp.json")
    registry.create(fixture_config(), expected_revision=0)
    registry.create(fixture_config(server_id="limited", scope={"mode": "root_kinds", "root_kinds": ["companion", "target"]}), expected_revision=1)
    registry.create(fixture_config(server_id="disabled", enabled=False), expected_revision=2)
    for root in ROOT_AGENT_KINDS:
        snapshot = registry.snapshot(root)
        expected = ["fixture", "limited"] if root in {"companion", "target"} else ["fixture"]
        assert [server["server_id"] for server in snapshot["servers"]] == expected
        assert snapshot["root_kind"] == root
    with pytest.raises(SystemMcpValidationError):
        registry.snapshot("not-a-root")


def test_native_compilation_keeps_credentials_out_of_snapshot_and_arguments(tmp_path, monkeypatch):
    monkeypatch.setenv("FIXTURE_ACCESS_TOKEN", "private-example-value")
    monkeypatch.setenv("META_RESEARCH_MCP_TOKEN", "internal-operation-secret")
    registry = SystemMcpRegistry(tmp_path / "system-mcp.json")
    registry.create(fixture_config(connection={"command": "python", "args": ["fixture.py", 'quoted"argument', "line\nbreak"], "env": {"LOG_LEVEL": "debug"}, "env_vars": ["FIXTURE_ACCESS_TOKEN"]}), expected_revision=0)
    registry.create(fixture_config(server_id="http", transport="streamable_http", connection={"url": "https://fixture.invalid/mcp", "bearer_token_env_var": "FIXTURE_ACCESS_TOKEN", "http_headers": {"X-Project": "study"}}), expected_revision=1)
    snapshot = registry.snapshot("companion")
    arguments, sensitive = compile_snapshot(snapshot)
    native = tomllib.loads("\n".join(value for flag, value in zip(arguments[::2], arguments[1::2]) if flag == "--config" and value != "mcp_servers={}"))["mcp_servers"]
    assert set(native) == {"external_fixture", "external_http"}
    assert native["external_fixture"]["env_vars"] == ["FIXTURE_ACCESS_TOKEN"]
    assert native["external_fixture"]["args"][1:] == ['quoted"argument', "line\nbreak"]
    assert native["external_http"]["required"] is False
    assert native["external_http"]["startup_timeout_sec"] == 10
    assert sensitive == {"FIXTURE_ACCESS_TOKEN": "private-example-value"}
    assert "private-example-value" not in json.dumps(snapshot)
    assert "private-example-value" not in " ".join(arguments)
    assert "internal-operation-secret" not in json.dumps([snapshot, arguments, sensitive])


def test_default_stdio_directory_does_not_follow_target_workspace(tmp_path, monkeypatch):
    registry = SystemMcpRegistry(tmp_path / "system-mcp.json")
    registry.create(fixture_config(), expected_revision=0)
    workspace = tmp_path / "target"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    arguments, _ = compile_snapshot(registry.snapshot("target"))
    native = tomllib.loads("\n".join(value for flag, value in zip(arguments[::2], arguments[1::2]) if flag == "--config" and value != "mcp_servers={}"))["mcp_servers"]
    assert native["external_fixture"]["cwd"] == str(tmp_path)


def test_simultaneous_management_writes_cannot_overwrite_each_other(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    path = tmp_path / "registry.json"
    def create(server_id):
        try:
            return SystemMcpRegistry(path).create(fixture_config(server_id=server_id), expected_revision=0)
        except SystemMcpConflictError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, ["first", "second"]))
    assert len([result for result in results if result is not None]) == 1
    saved = SystemMcpRegistry(path).read()
    assert saved["revision"] == 1
    assert len(saved["servers"]) == 1


def test_named_native_profile_is_immutable_and_does_not_edit_user_config(tmp_path):
    from meta_research.system_mcp import ensure_native_mcp_profile, NATIVE_MCP_PROFILE_NAME
    user_config = tmp_path / "config.toml"
    user_config.write_text('model="preserved-model"\n', encoding="utf-8")
    ensure_native_mcp_profile(tmp_path)
    profile = tmp_path / (NATIVE_MCP_PROFILE_NAME + ".config.toml")
    assert tomllib.loads(profile.read_text()) == {"mcp_servers": [], "projects": []}
    ensure_native_mcp_profile(tmp_path)
    assert user_config.read_text() == 'model="preserved-model"\n'
    profile.chmod(0o600)
    profile.write_text("unexpected=true\n", encoding="utf-8")
    with pytest.raises(SystemMcpLoadError):
        ensure_native_mcp_profile(tmp_path)
    assert profile.read_text() == "unexpected=true\n"


@pytest.mark.skipif(not os.environ.get("META_RESEARCH_NATIVE_MCP_TEST_CODEX"), reason="requires the pinned native executable")
def test_native_configuration_excludes_user_and_trusted_project_mcp(tmp_path):
    import os
    import subprocess
    from meta_research.system_mcp import ensure_native_mcp_profile
    native_home = tmp_path / "native-home"
    native_home.mkdir()
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    (workspace / ".codex").mkdir()
    (native_home / "config.toml").write_text(
        'model="preserved-model"\n'
        f'[projects.{json.dumps(str(workspace))}]\ntrust_level="trusted"\n'
        '[mcp_servers.user_hidden]\ncommand="never-run-user-command"\n'
        '[mcp_servers.external_fixture]\nurl="https://unexpected.invalid/mcp"\n'
        '[mcp_servers.external_fixture.http_headers]\nX-Unexpected="must-not-merge"\n', encoding="utf-8")
    (workspace / ".codex" / "config.toml").write_text('[mcp_servers.project_hidden]\ncommand="never-run-project-command"\n', encoding="utf-8")
    ensure_native_mcp_profile(native_home)
    registry = SystemMcpRegistry(tmp_path / "registry.json")
    registry.create(fixture_config(), expected_revision=0)
    for expected_names in [["external_fixture"], []]:
        arguments, _ = compile_snapshot(registry.snapshot("target"))
        result = subprocess.run(
            [os.environ["META_RESEARCH_NATIVE_MCP_TEST_CODEX"], *arguments[:2], "mcp", "list", "--json", *arguments[2:]],
            capture_output=True, text=True, check=True, cwd=workspace,
            env=dict(os.environ, CODEX_HOME=str(native_home), CODEX_SQLITE_HOME=str(native_home)), timeout=10)
        servers = json.loads(result.stdout)
        assert [server["name"] for server in servers] == expected_names
        if servers:
            assert servers[0]["transport"]["type"] == "stdio"
            assert servers[0]["transport"]["command"] == "python"
            assert servers[0]["transport"]["env"] == {}
            registry.delete("fixture", expected_revision=1)
