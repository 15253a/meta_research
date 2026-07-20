from pathlib import Path

from orchestrator import web_app


def test_default_data_root_is_inside_the_installation(tmp_path, monkeypatch):
    monkeypatch.delenv("META_RESEARCH_HOME", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-must-not-be-used"))
    system_root = tmp_path / "installed-meta-research"
    assert web_app._default_data_root(system_root) == system_root / "runtime"


def test_meta_research_home_remains_an_explicit_override(tmp_path, monkeypatch):
    relocated = tmp_path / "relocated"
    monkeypatch.setenv("META_RESEARCH_HOME", str(relocated))
    assert web_app._default_data_root(tmp_path / "system") == relocated


def test_web_app_forwards_the_project_local_default(tmp_path, monkeypatch):
    captured = []
    system_root = tmp_path / "installed-meta-research"
    monkeypatch.delenv("META_RESEARCH_HOME", raising=False)
    monkeypatch.setattr(web_app, "_system_root", lambda: system_root)
    monkeypatch.setattr(web_app, "console_main", lambda argv: captured.append(argv) or 0)

    assert web_app.main(["--no-open-browser"]) == 0
    argv = captured[0]
    assert argv[argv.index("--quests-root") + 1] == str(system_root / "runtime")


def test_web_app_builds_zero_configuration_local_console_command(
        tmp_path, monkeypatch):
    captured = []
    monkeypatch.setattr(web_app, "console_main", lambda argv: captured.append(argv) or 0)
    assert web_app.main([
        "--data-root", str(tmp_path / "product-data"),
        "--port", "9876", "--no-open-browser",
    ]) == 0
    argv = captured[0]
    assert argv[argv.index("--quests-root") + 1] == str(tmp_path / "product-data")
    system_root = Path(argv[argv.index("--system-root") + 1])
    assert (system_root / "policies" / "policy.yaml").is_file()
    assert "--no-outbound" in argv
    assert "--no-open-browser" in argv


def test_web_app_forwards_local_roots_and_connector(tmp_path, monkeypatch):
    captured = []
    monkeypatch.setattr(web_app, "console_main", lambda argv: captured.append(argv) or 0)
    connector = tmp_path / "connector.toml"
    assert web_app.main([
        "--data-root", str(tmp_path / "data"),
        "--connector-profile", str(connector),
        "--local-import-root", "/data",
        "--local-import-root", "/papers",
    ]) == 0
    argv = captured[0]
    assert "--connector-profile" in argv and "--no-outbound" not in argv
    roots = [argv[index + 1] for index, value in enumerate(argv)
             if value == "--local-import-root"]
    assert roots == ["/data", "/papers"]


def test_system_root_can_resolve_installed_asset_tree(tmp_path, monkeypatch):
    installed = tmp_path / "share" / "meta-research"
    for marker in web_app._SYSTEM_MARKERS:
        path = installed / marker
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("asset", encoding="utf-8")
    monkeypatch.setenv("META_RESEARCH_SYSTEM_ROOT", str(installed))
    assert web_app._system_root() == installed.resolve()
