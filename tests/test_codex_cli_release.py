from pathlib import Path
import json
import os
import pytest
from meta_research.paths import DataRoot, DataRootError

def install_at(root: Path, version: str) -> Path:
    package = root / "node_modules/@openai/codex"
    package.mkdir(parents=True)
    (package / "package.json").write_text(json.dumps({"version": version}))
    executable = root / "node_modules/.bin/codex"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    return executable

def test_wrong_private_cli_does_not_shadow_requested_shared_release(tmp_path, monkeypatch):
    data = DataRoot(tmp_path / "data")
    private = install_at(data.codex_cli_install_root, "0.153.2")
    shared = tmp_path / "shared"
    exact = install_at(shared / "codex-cli/installs/0.156.1", "0.156.1")
    monkeypatch.setenv("META_RESEARCH_PROVIDER_TOOLS", str(shared))
    assert data.validated_codex_cli_executable("0.156.1") == exact
    assert private.exists()

def test_missing_requested_cli_never_returns_installed_wrong_version(tmp_path, monkeypatch):
    data = DataRoot(tmp_path / "data")
    install_at(data.codex_cli_install_root, "0.153.2")
    monkeypatch.setenv("META_RESEARCH_PROVIDER_TOOLS", str(tmp_path / "shared"))
    with pytest.raises(DataRootError, match="requested Codex version"):
        data.validated_codex_cli_executable("0.156.1")

def test_audited_cli_rejects_changed_install_file(tmp_path, monkeypatch):
    import hashlib
    data = DataRoot(tmp_path / "data")
    executable = install_at(data.codex_cli_install_root, "0.156.1")
    manifest = {"schema":"meta-research/codex-cli-install/v1", "version":"0.156.1",
                "files":{"node_modules/.bin/codex": hashlib.sha256(executable.read_bytes()).hexdigest()}}
    (data.codex_cli_install_root / "codex-install-manifest.json").write_text(json.dumps(manifest))
    executable.write_text("#!/bin/sh\necho changed\n")
    with pytest.raises(DataRootError, match="manifest mismatch"):
        data.validated_codex_cli_executable("0.156.1")
