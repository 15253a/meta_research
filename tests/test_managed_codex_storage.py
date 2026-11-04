from __future__ import annotations

import os
import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from meta_research.cli import CliError, _daemon_environment, _explicit_data_root
from meta_research.composition import build_production_runtime
from meta_research.paths import DataRootError, prepare_data_root
from meta_research.provider_supervisor import (
    SUPERVISOR_REQUEST_SCHEMA_V2, ensure_transport_key, read_transport_envelope,
    protected_subprocess_environment, write_supervisor_request,
)
from meta_research.quest_drafting import _CancellableProcessRunner


@pytest.mark.skipif(os.name != "posix", reason="Synthetic provider uses a POSIX executable entry.")
def test_logged_out_account_blocks_new_provider_but_allows_local_probe(tmp_path):
    root = prepare_data_root(tmp_path / "data")
    state = root.codex_home / "account-state.json"
    state.write_text(json.dumps({"generation": "b" * 32, "logged_out": True}))
    state.chmod(0o600)
    # A provider already in flight can finish by refreshing its old credentials.
    auth = root.codex_home / "auth.json"
    auth.write_text('{"tokens":{"account_id":"previous-account"}}')
    auth.chmod(0o600)
    executable = tmp_path / "fake-codex"
    executable.write_text(f"#!{sys.executable}\nprint('local probe')\n")
    executable.chmod(0o700)
    runner = _CancellableProcessRunner(protected_environment=root.codex_environment)

    with pytest.raises(OSError, match="^codex_account_logged_out$"):
        runner([str(executable), "exec", "-"], "", 5)

    assert runner.run_command([str(executable), "--version"], 5).stdout == "local probe\n"
    assert runner.run_command([str(executable), "--config", "features.test=false", "features", "list"], 5).stdout == "local probe\n"
    assert runner.run_command([str(executable), "app-server", "generate-json-schema"], 5).stdout == "local probe\n"


@pytest.mark.parametrize("transport", ("exec", "app-server", "native_bridge", "harness_bridge"))
@pytest.mark.parametrize("durable", (False, True))
def test_provider_identity_guard_rejects_old_account_for_codex_transports(tmp_path, transport, durable):
    root = prepare_data_root(tmp_path / "data")
    state = root.codex_home / "account-state.json"
    state.write_text(json.dumps({"generation": "f" * 32, "logged_out": False,
        "account_id_hash": "3d5c2194f0cc7c9a964b46fd0e6d408325f7fdcbe1a877ba8522fb03bba74033"}))
    state.chmod(0o600)
    auth = root.codex_home / "auth.json"
    auth.write_text('{"tokens":{"account_id":"previous-account"}}')
    auth.chmod(0o600)
    argv = {"exec": ["unused-codex", "exec", "-"],
        "app-server": ["unused-codex", "app-server"],
        "native_bridge": [sys.executable, "-m", "meta_research.native_codex_bridge"],
        "harness_bridge": [sys.executable, "-m", "meta_research.harness_cli_bridge", "--family", "codex"],
    }[transport]
    runner = _CancellableProcessRunner(protected_environment=root.codex_environment)

    with pytest.raises(OSError, match="^codex_account_credentials_changed$"):
        if durable:
            runner.run_durable_job("test", argv, "", 5, tmp_path / "stdout", tmp_path / "pid", tmp_path / "request")
        else:
            runner(argv, "", 5)


@pytest.mark.skipif(os.name != "posix", reason="Synthetic provider uses a POSIX executable entry.")
def test_durable_supervisor_rechecks_account_before_provider_launch(tmp_path):
    root = prepare_data_root(tmp_path / "data")
    state = root.codex_home / "account-state.json"
    state.write_text(json.dumps({"generation": "c" * 32, "logged_out": True}))
    state.chmod(0o600)
    executable = tmp_path / "fake-codex"
    executable.write_text(f"#!{sys.executable}\nprint('must not launch')\n")
    executable.chmod(0o700)
    _, key = ensure_transport_key(tmp_path)
    invocation_hash = "d" * 64
    operation = tmp_path / "provider-operations" / invocation_hash[:2] / invocation_hash
    operation.mkdir(parents=True)
    paths = {name + "_path": operation / filename for name, filename in (
        ("prompt", "prompt.txt"), ("schema", "output-schema.json"),
        ("stdout", "stdout.jsonl"), ("result", "last-message.json"),
        ("lock", "supervisor.lock"), ("ready", "supervisor-ready.json"),
        ("started", "provider-started.json"), ("receipt", "supervisor-exit.json"),
        ("stop", "supervisor-stop.json"),
    )}
    paths["prompt_path"].write_text("synthetic test")
    paths["schema_path"].write_text("{}")
    request = operation / "supervisor-request.json"
    write_supervisor_request(request, {
        "schema_ref": SUPERVISOR_REQUEST_SCHEMA_V2, "invocation_hash": invocation_hash,
        "argv": [str(executable), "exec", "--output-schema", str(paths["schema_path"]),
                 "--output-last-message", str(paths["result_path"]), "-"],
        "timeout_seconds": 5.0, "stream_max_bytes": 4096, "result_max_bytes": 4096,
        **{name: str(path) for name, path in paths.items()},
    }, key)

    completed = subprocess.run([sys.executable, "-m", "meta_research.streaming_provider_supervisor", str(request)],
        env={**os.environ, **root.codex_environment}, capture_output=True, timeout=10)

    assert completed.returncode == 0
    assert read_transport_envelope(paths["receipt_path"], key)["termination_reason"] == "launch_failed"
    assert not paths["started_path"].exists()
    assert paths["stdout_path"].read_bytes() == b""


def test_explicit_account_logout_prevents_template_reseed(tmp_path, monkeypatch):
    template = tmp_path / "deployment-auth.json"
    template.write_text('{"auth_mode":"chatgpt","tokens":{"test":"original"}}')
    monkeypatch.setenv("META_RESEARCH_CODEX_AUTH_TEMPLATE", str(template))
    root = prepare_data_root(tmp_path / "data")
    assert (root.codex_home / "auth.json").is_file()
    (root.codex_home / "account-state.json").write_text(json.dumps({
        "generation": "a" * 32, "logged_out": True,
    }))
    (root.codex_home / "auth.json").unlink()

    reopened = prepare_data_root(root.root)

    assert not (reopened.codex_home / "auth.json").exists()
    assert json.loads(template.read_text())["tokens"] == {"test": "original"}


@pytest.mark.parametrize("state", ('{"generation":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","logged_out":false}', "broken-state"))
def test_managed_account_state_never_falls_back_to_old_template(tmp_path, monkeypatch, state):
    template = tmp_path / "deployment-auth.json"
    template.write_text('{"test":"old-account"}')
    monkeypatch.setenv("META_RESEARCH_CODEX_AUTH_TEMPLATE", str(template))
    root = prepare_data_root(tmp_path / "data")
    (root.codex_home / "account-state.json").write_text(state)
    (root.codex_home / "auth.json").unlink()

    assert not (prepare_data_root(root.root).codex_home / "auth.json").exists()


def test_data_root_prepares_private_codex_home_and_deployment_cli_path(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "deployment")

    assert data_root.codex_home == tmp_path / "deployment/provider-homes/codex"
    assert data_root.codex_cli_executable == (
        tmp_path
        / "deployment/provider-tools/codex-cli/node_modules/.bin"
        / ("codex.cmd" if os.name == "nt" else "codex")
    )
    assert data_root.codex_environment == {
        "CODEX_HOME": str(data_root.codex_home),
        "CODEX_SQLITE_HOME": str(data_root.codex_home),
        "HOME": str(data_root.codex_user_home),
        "USERPROFILE": str(data_root.codex_user_home),
        "XDG_CONFIG_HOME": str(data_root.codex_config),
        "XDG_DATA_HOME": str(data_root.codex_data),
        "XDG_STATE_HOME": str(data_root.codex_state),
        "XDG_CACHE_HOME": str(data_root.codex_cache),
        "NODE_PATH": "",
        "PIP_CACHE_DIR": str(data_root.codex_pip_cache),
        "npm_config_cache": str(data_root.codex_npm_cache),
        "UV_CACHE_DIR": str(data_root.codex_uv_cache),
        "TMPDIR": str(data_root.codex_tmp),
        "TEMP": str(data_root.codex_tmp),
        "TMP": str(data_root.codex_tmp),
        "SQLITE_TMPDIR": str(data_root.codex_tmp),
    }
    for directory in (
        data_root.provider_homes,
        data_root.codex_home,
        data_root.codex_user_home,
        data_root.codex_config,
        data_root.codex_data,
        data_root.codex_state,
        data_root.codex_sessions,
        data_root.codex_archived_sessions,
        data_root.codex_cache,
        data_root.codex_npm_cache,
        data_root.codex_uv_cache,
        data_root.codex_pip_cache,
        data_root.codex_tmp,
        data_root.provider_tools,
        data_root.codex_cli_install_root,
    ):
        assert directory.is_dir()
        assert not directory.is_symlink()
        if os.name == "posix":
            assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_data_root_rejects_a_linked_codex_home(tmp_path: Path) -> None:
    data_root = prepare_data_root(tmp_path / "deployment")
    outside = tmp_path / "outside"
    outside.mkdir()
    # Preserve the prepared managed tree while replacing only its entry path.
    data_root.codex_home.rename(data_root.codex_home.with_name("preserved-codex"))
    try:
        data_root.codex_home.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")

    with pytest.raises(DataRootError, match="not a real directory"):
        prepare_data_root(data_root.root)


@pytest.mark.parametrize("directory_name", ("sessions", "archived_sessions"))
def test_data_root_rejects_linked_codex_session_directories(
    tmp_path: Path, directory_name: str
) -> None:
    data_root = prepare_data_root(tmp_path / "deployment")
    directory = data_root.codex_home / directory_name
    outside = tmp_path / f"outside-{directory_name}"
    outside.mkdir()
    directory.rmdir()
    try:
        directory.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")

    with pytest.raises(DataRootError, match="not a real directory"):
        prepare_data_root(data_root.root)


def test_runner_protected_environment_wins_over_each_invocation() -> None:
    runner = _CancellableProcessRunner(
        protected_environment={
            "CODEX_HOME": "/deployment/provider-homes/codex",
            "CODEX_SQLITE_HOME": "/deployment/provider-homes/codex",
            "XDG_CACHE_HOME": "/deployment/provider-homes/codex/cache",
            "TMPDIR": "/deployment/provider-homes/codex/tmp",
        }
    )

    environment = runner._subprocess_environment(
        {
            "CODEX_HOME": "/escape",
            "XDG_CACHE_HOME": "/escape-cache",
            "TMPDIR": "/escape-tmp",
            "REQUEST_MARKER": "kept",
        }
    )

    assert environment is not None
    assert environment["CODEX_HOME"] == "/deployment/provider-homes/codex"
    assert environment["CODEX_SQLITE_HOME"] == "/deployment/provider-homes/codex"
    assert environment["XDG_CACHE_HOME"] == "/deployment/provider-homes/codex/cache"
    assert environment["TMPDIR"] == "/deployment/provider-homes/codex/tmp"
    assert environment["REQUEST_MARKER"] == "kept"


def test_windows_protected_environment_removes_case_aliases() -> None:
    environment = protected_subprocess_environment(
        protected={
            "CODEX_HOME": r"D:\deployment\provider-homes\codex",
            "CODEX_SQLITE_HOME": r"D:\deployment\provider-homes\codex",
            "XDG_CACHE_HOME": r"D:\deployment\provider-homes\codex\cache",
        },
        requested={
            "codex_home": r"C:\escape",
            "CoDeX_sQlItE_hOmE": r"C:\escape-sqlite",
            "xdg_cache_home": r"C:\escape-cache",
        },
        source_environment={"Codex_Home": r"C:\host", "Path": "bin"},
        platform_name="nt",
    )

    assert environment == {
        "Path": "bin",
        "CODEX_HOME": r"D:\deployment\provider-homes\codex",
        "CODEX_SQLITE_HOME": r"D:\deployment\provider-homes\codex",
        "XDG_CACHE_HOME": r"D:\deployment\provider-homes\codex\cache",
    }


def test_daemon_environment_defensively_binds_managed_codex_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = prepare_data_root(tmp_path / "deployment")
    for name in data_root.codex_environment:
        monkeypatch.setenv(name, str(tmp_path / f"escape-{name.lower()}"))

    environment = _daemon_environment(data_root)

    for name, value in data_root.codex_environment.items():
        assert environment[name] == value


def test_managed_codex_executable_rejects_a_link_outside_install_root(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "deployment")
    executable = data_root.codex_cli_executable
    executable.parent.mkdir(parents=True)
    outside = tmp_path / "global-codex"
    outside.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    outside.chmod(0o700)
    try:
        executable.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")

    with pytest.raises(DataRootError, match="escapes its install root"):
        data_root.validated_codex_cli_executable()


def test_cli_data_root_fails_closed_without_argument_or_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("META_RESEARCH_DATA_ROOT", raising=False)

    with pytest.raises(CliError, match="explicit data root is required"):
        _explicit_data_root(None)


def test_cli_data_root_accepts_only_an_argument_or_explicit_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    argument = tmp_path / "argument-root"
    configured = tmp_path / "configured-root"
    monkeypatch.setenv("META_RESEARCH_DATA_ROOT", str(configured))

    assert _explicit_data_root(argument) == argument
    assert _explicit_data_root(None) == configured


@pytest.mark.parametrize(
    ("argument", "configured"),
    ((Path("relative-argument"), None), (None, "relative-environment")),
)
def test_cli_data_root_rejects_relative_paths(
    argument: Path | None,
    configured: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if configured is None:
        monkeypatch.delenv("META_RESEARCH_DATA_ROOT", raising=False)
    else:
        monkeypatch.setenv("META_RESEARCH_DATA_ROOT", configured)

    with pytest.raises(CliError, match="absolute deployment path"):
        _explicit_data_root(argument)


def test_default_codex_adapters_share_managed_runner_and_stage_ledgers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("META_RESEARCH_PROVIDER_TOOLS", raising=False)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "wrong-codex-home"))
    data_root = prepare_data_root(tmp_path / "deployment")
    runtime = build_production_runtime(
        data_root,
        startup_harness_diagnostics=False,
        startup_power_probe=False,
    )
    try:
        drafting = runtime.owners.human_collaboration._proposal_drafter
        codex_providers = (
            drafting,
            runtime.idea_stage._provider,
            runtime.plan_stage._provider,
            runtime.bundle_stage._provider,
            runtime.reasoning_stage._provider,
            runtime.deepfetch._provider,
        )
        codex_runner = drafting._runner
        assert runtime.owners.human_collaboration._intent_drafting_provider is drafting
        expected_executable = str(data_root.validated_codex_cli_executable())
        for provider in codex_providers:
            assert provider._executable == expected_executable
            runner = (
                provider._runner
                if hasattr(provider, "_runner")
                else provider._process_runner
            )
            assert runner is codex_runner
            assert runner._protected_environment == data_root.codex_environment

        codex_harness = runtime.harnesses._adapters["codex"]
        assert runtime.deepfetch._provider._codex_ledger_reader._codex_home == (
            data_root.codex_home.resolve()
        )
        for provider in codex_providers[1:5]:
            assert provider._codex_ledger_reader._codex_home == (
                data_root.codex_home.resolve()
            )
        assert codex_harness.executable == expected_executable
        assert codex_harness._codex_child_ledger_reader._codex_home == (
            data_root.codex_home.resolve()
        )
        assert codex_harness._runner._process_runner is codex_runner
    finally:
        runtime.close()
