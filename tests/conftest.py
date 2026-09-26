from __future__ import annotations

import time
from pathlib import Path

import pytest

from meta_research.runtime_protection import InhibitorLease


@pytest.fixture
def scoped_system_mcp(tmp_path: Path):
    """Exercise real provider boundaries with matching and excluded servers."""
    from meta_research.root_capabilities import ROOT_AGENT_KINDS
    from meta_research.system_mcp import SystemMcpRegistry

    def build(root_kind: str):
        registry = SystemMcpRegistry(tmp_path / f"system-mcp-{root_kind}.json")
        for revision, (server_id, scope) in enumerate((
            ("shared", {"mode": "all"}),
            ("selected", {"mode": "root_kinds", "root_kinds": [root_kind]}),
            ("excluded", {"mode": "root_kinds", "root_kinds": [kind for kind in ROOT_AGENT_KINDS if kind != root_kind]}),
        )):
            registry.create({
                "server_id": server_id, "display_name": server_id,
                "transport": "stdio", "connection": {"command": "fixture-" + server_id},
                "scope": scope,
            }, expected_revision=revision)

        def assert_loaded(argv: list[str]):
            assert 'mcp_servers.external_shared.command="fixture-shared"' in argv
            assert 'mcp_servers.external_selected.command="fixture-selected"' in argv
            assert not any("external_excluded" in argument for argument in argv)

        return registry, assert_loaded

    return build


@pytest.fixture
def pre_system_mcp_binding():
    """Use source/instruction identities captured from the actual 8769 baseline."""
    from dataclasses import replace
    import json

    revisions = json.loads((Path(__file__).parent / "fixtures" / "system_mcp_binding_before.json").read_text())

    def restore(binding, profile=None):
        revision = next(item for item in revisions if item["binding_type"] == type(binding).__name__ and item["profile"] == profile)
        sources = {entry.split("@sha256:")[0]: entry for entry in revision["sources"]}
        return replace(binding,
            instruction_set_hash=revision["instruction_set_hash"],
            resource_bindings=tuple(sources.get(entry.split("@sha256:")[0], entry) for entry in binding.resource_bindings),
        )

    return restore


class _DeterministicPowerInhibitor:
    """Process-local platform substitute for non-platform test modules.

    Production conformance tests instantiate ProductionPowerInhibitor directly;
    the broad Owner/Web suite should not depend on the CI host having a logind
    system bus or a native Windows guardian.
    """

    kind = "test_inhibitor"

    def __init__(self) -> None:
        self._active: set[str] = set()

    def acquire(self, *, holder_ref: str, reason: str) -> InhibitorLease:
        del reason
        self._active.add(holder_ref)
        return InhibitorLease(
            holder_ref=holder_ref,
            backend=self.kind,
            scope="sleep",
            acquired_at=time.time(),
            native_holder_ref=f"test-native:{holder_ref}",
        )

    def is_confirmed(self, lease: InhibitorLease) -> bool:
        return lease.holder_ref in self._active

    def release(self, lease: InhibitorLease) -> None:
        self._active.discard(lease.holder_ref)


@pytest.fixture(autouse=True)
def _isolate_platform_power_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep ordinary tests deterministic while preserving explicit adapters."""

    monkeypatch.delenv("META_RESEARCH_ASSUME_ALWAYS_ON", raising=False)
    adapters: dict[Path, _DeterministicPowerInhibitor] = {}

    def build_adapter(state_directory: Path) -> _DeterministicPowerInhibitor:
        return adapters.setdefault(state_directory, _DeterministicPowerInhibitor())

    monkeypatch.setattr(
        "meta_research.composition.ProductionPowerInhibitor",
        build_adapter,
    )
