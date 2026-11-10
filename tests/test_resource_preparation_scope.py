"""Exercise real adapter preparation, exact bytes and next-call invalidation."""
from dataclasses import replace
from pathlib import Path

import pytest

from meta_research import idea_skill
from meta_research.bundle_skill import BundleSkillUnavailable
from meta_research.resource_preparation import resource_preparation, resource_sha256
from test_binding_compatibility import adapter, request


def test_preparation_shares_workspace_and_small_executable_bytes(tmp_path, monkeypatch):
    from test_idea_skill_contract import _IdeaResidentMcpAuthority
    from test_bundle_skill_adapter import _fake_codex
    provider = idea_skill.CodexIdeaSkillAdapter(tmp_path / "provider", executable=str(_fake_codex(tmp_path / "codex")))
    provider.bind_resident_mcp_authority(_IdeaResidentMcpAuthority())
    provider.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
    original = Path.open
    counts = {}
    workspace = Path(idea_skill.__file__).with_name("root_workspace.py").resolve()
    def opened(path, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if mode == "rb":
            key = str(path.resolve())
            counts[key] = counts.get(key, 0) + 1
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", opened)
    provider.runtime_binding()
    assert counts[str(workspace)] == 1
    cli = next(key for key in counts if Path(key).name == "codex")
    assert counts[cli] == 1
    provider.runtime_binding()
    assert counts[str(workspace)] == counts[cli] == 2


def test_new_preparation_observes_same_size_replaced_executable(adapter):
    provider, runner, authority = adapter
    frozen = provider.runtime_binding()
    artifact = next(x for x in frozen.resource_bindings if x.startswith("harness-artifact:") and Path(x.split("@sha256:")[0].removeprefix("harness-artifact:")).name == "codex")
    path = Path(artifact.split("@sha256:")[0].removeprefix("harness-artifact:"))
    original, stat = path.read_bytes(), path.stat()
    import os
    replacement = original.replace(b"python", b"pyth0n", 1) if b"python" in original else original[:-1] + (b" " if original[-1:] != b" " else b"\n")
    assert len(replacement) == len(original) and replacement != original
    path.write_bytes(replacement)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert provider.runtime_binding() != frozen
    with pytest.raises(BundleSkillUnavailable, match="bundle_runtime_binding_drift"):
        provider.schedule_target(request(frozen))
    assert runner.calls == [] and authority.issued == []


def test_large_artifact_digest_stays_streaming_and_next_scope_rereads(tmp_path, monkeypatch):
    path = tmp_path / "binary"
    path.write_bytes(b"x" * 2_000_001)
    original = Path.read_bytes
    def no_large_read(p):
        assert p != path
        return original(p)
    monkeypatch.setattr(Path, "read_bytes", no_large_read)
    @resource_preparation
    def digest():
        return resource_sha256(path), resource_sha256(path)
    before = digest()
    assert before[0] == before[1]
    path.write_bytes(b"y" * 2_000_001)
    after = digest()
    assert after[0] == after[1] and before != after
