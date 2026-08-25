from __future__ import annotations

import time
from pathlib import Path

import pytest

from meta_research.runtime_protection import InhibitorLease


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

    adapters: dict[Path, _DeterministicPowerInhibitor] = {}

    def build_adapter(state_directory: Path) -> _DeterministicPowerInhibitor:
        return adapters.setdefault(state_directory, _DeterministicPowerInhibitor())

    monkeypatch.setattr(
        "meta_research.composition.ProductionPowerInhibitor",
        build_adapter,
    )
