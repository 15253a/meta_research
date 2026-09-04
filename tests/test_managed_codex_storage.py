from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from meta_research.cli import CliError, _daemon_environment, _explicit_data_root
from meta_research.composition import build_production_runtime
from meta_research.paths import DataRootError, prepare_data_root
from meta_research.provider_supervisor import protected_subprocess_environment
from meta_research.quest_drafting import _CancellableProcessRunner


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
        "XDG_CACHE_HOME": str(data_root.codex_cache),
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
    data_root.codex_npm_cache.rmdir()
    data_root.codex_uv_cache.rmdir()
    data_root.codex_pip_cache.rmdir()
    data_root.codex_cache.rmdir()
    data_root.codex_tmp.rmdir()
    data_root.codex_sessions.rmdir()
    data_root.codex_archived_sessions.rmdir()
    data_root.codex_home.rmdir()
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
            runtime.writing._provider,
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
            assert provider._child_review_verifier is not None
        assert codex_harness.executable == expected_executable
        assert codex_harness._codex_child_ledger_reader._codex_home == (
            data_root.codex_home.resolve()
        )
        assert codex_harness._runner._process_runner is codex_runner
    finally:
        runtime.close()
