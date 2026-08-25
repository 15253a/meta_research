from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import text

from meta_research.database import Database
from meta_research.feed import DurableFeed
from meta_research.migration import upgrade_database
from meta_research.paths import prepare_data_root
from meta_research.runtime_protection import (
    InhibitorLease,
    RuntimeEffectIdentity,
    RuntimeEventLogger,
    RuntimeProtection,
    RuntimeProtectionUnavailable,
    record_runtime_boundary,
)


@dataclass
class RecordingInhibitor:
    fail_code: str | None = None
    fail_after_acquire_code: str | None = None
    release_fail_code: str | None = None
    hold_query_override: str | None = None
    exact_absence_without_lease: bool = False
    crash_after_next_native_acquire: bool = False

    def __post_init__(self) -> None:
        self.acquire_calls: list[str] = []
        self.acquire_reasons: list[str] = []
        self.release_calls: list[str] = []
        self.live_holders: set[str] = set()
        self.native_acquire_count = 0

    @property
    def kind(self) -> str:
        return "test_inhibitor"

    def acquire(self, *, holder_ref: str, reason: str) -> InhibitorLease:
        self.acquire_calls.append(holder_ref)
        self.acquire_reasons.append(reason)
        if self.fail_code is not None:
            raise RuntimeProtectionUnavailable(self.fail_code)
        if holder_ref not in self.live_holders:
            self.live_holders.add(holder_ref)
            self.native_acquire_count += 1
        if self.fail_after_acquire_code is not None:
            raise RuntimeProtectionUnavailable(self.fail_after_acquire_code)
        return InhibitorLease(
            holder_ref=holder_ref,
            backend=self.kind,
            scope="sleep",
            acquired_at=1_720_000_000.0,
            native_holder_ref="test-native-holder",
        )

    def is_confirmed(self, lease: InhibitorLease) -> bool:
        return self.query_hold(lease) == "confirmed"

    def query_hold(self, lease: InhibitorLease) -> str:
        if (
            self.crash_after_next_native_acquire
            and lease.holder_ref in self.live_holders
        ):
            self.crash_after_next_native_acquire = False
            raise SimulatedProcessDeath
        if self.hold_query_override is not None:
            return self.hold_query_override
        return "confirmed" if lease.holder_ref in self.live_holders else "absent"

    def query_exact_hold(
        self,
        *,
        holder_ref: str,
    ) -> tuple[str, InhibitorLease | None]:
        lease = InhibitorLease(
            holder_ref=holder_ref,
            backend=self.kind,
            scope="sleep",
            acquired_at=1_720_000_000.0,
            native_holder_ref="test-native-holder",
        )
        status = self.query_hold(lease)
        if status == "absent" and self.exact_absence_without_lease:
            return status, None
        return status, lease

    def release(self, lease: InhibitorLease) -> None:
        self.release_calls.append(lease.holder_ref)
        if self.release_fail_code is not None:
            raise RuntimeProtectionUnavailable(self.release_fail_code)
        self.live_holders.discard(lease.holder_ref)


class RecordingTelemetryExporter:
    provider = "recording_otlp"

    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []
        self.exported = threading.Event()
        self.closed = threading.Event()

    def export(self, event: dict[str, object]) -> None:
        self.events.append(dict(event))
        self.exported.set()

    def close(self) -> None:
        self.closed.set()


class SimulatedProcessDeath(BaseException):
    pass


def _effect(suffix: str) -> RuntimeEffectIdentity:
    return RuntimeEffectIdentity(
        responsibility_ref=f"responsibility:{suffix}",
        owner_scope="agent_runtime",
        root_run_ref=f"run:{suffix}",
        attempt_ref=f"attempt:{suffix}",
        fence_ref=f"fence:{suffix}",
        operation_ref=f"operation:{suffix}",
        effect_kind="provider_unit",
    )


def _coordinator(
    tmp_path: Path,
    adapter: RecordingInhibitor,
    *,
    startup_probe: bool = False,
):
    data_root = prepare_data_root(tmp_path)
    upgrade_database(data_root.database)
    database = Database(data_root.database)
    feed = DurableFeed(database)
    feed.ensure_initialized()
    logger = RuntimeEventLogger(data_root.logs / "runtime.jsonl")
    return (
        RuntimeProtection(
            database=database,
            feed=feed,
            inhibitor=adapter,
            event_logger=logger,
            clock=lambda: 1_720_000_000.0,
            startup_probe=startup_probe,
        ),
        database,
    )


def _record_boundary(
    database: Database,
    effect: RuntimeEffectIdentity,
    *,
    boundary: str,
    checkpoint_ref: str | None = None,
) -> None:
    with database.write() as connection:
        record_runtime_boundary(
            connection,
            identity=effect,
            boundary=boundary,  # type: ignore[arg-type]
            checkpoint_ref=checkpoint_ref,
            owner_evidence_ref=f"owner_evidence:{effect.responsibility_ref}",
        )


def test_startup_probe_uses_a_transient_hold_without_owner_responsibility(
    tmp_path: Path,
) -> None:
    adapter = RecordingInhibitor()
    protection, database = _coordinator(
        tmp_path / "startup-probe",
        adapter,
        startup_probe=True,
    )
    try:
        evidence = protection.query_evidence()

        assert evidence["inhibitor"]["capability"] == {
            "status": "ready",
            "backend": "test_inhibitor",
            "scope": "sleep",
            "reason": None,
            "probed_at": 1_720_000_000.0,
        }
        assert evidence["responsibilities"] == []
        assert evidence["inhibitor"]["active_count"] == 0
        assert adapter.acquire_calls == adapter.release_calls
        assert adapter.acquire_reasons == [
            "meta-research power capability diagnostic"
        ]
        calls = (list(adapter.acquire_calls), list(adapter.release_calls))
        protection.query_evidence()
        protection.query_evidence()
        assert (adapter.acquire_calls, adapter.release_calls) == calls
        with database.read() as connection:
            assert connection.execute(
                text("SELECT COUNT(*) FROM ar_execution_responsibilities")
            ).scalar_one() == 0
            assert connection.execute(
                text("SELECT COUNT(*) FROM ar_runtime_boundary_receipts")
            ).scalar_one() == 0
    finally:
        database.close()


def test_startup_probe_release_failure_is_unavailable_and_never_reacquires(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "startup-probe-release-failure")
    upgrade_database(data_root.database)
    adapter = RecordingInhibitor(
        release_fail_code="power_inhibitor_release_failed"
    )
    first_database = Database(data_root.database)
    first = RuntimeProtection(
        database=first_database,
        feed=DurableFeed(first_database),
        inhibitor=adapter,
        event_logger=RuntimeEventLogger(data_root.logs / "runtime.jsonl"),
        startup_probe=True,
    )
    first_evidence = first.query_evidence()
    assert first_evidence["inhibitor"]["capability"]["status"] == "unavailable"
    assert first_evidence["inhibitor"]["capability"]["reason"] == {
        "code": "power_inhibitor_release_failed"
    }
    assert adapter.acquire_calls == ["power_probe:startup"]
    assert adapter.release_calls == ["power_probe:startup"]

    second_database = Database(data_root.database)
    second = RuntimeProtection(
        database=second_database,
        feed=DurableFeed(second_database),
        inhibitor=adapter,
        event_logger=RuntimeEventLogger(data_root.logs / "runtime.jsonl"),
        startup_probe=True,
    )
    try:
        assert adapter.acquire_calls == ["power_probe:startup"]
        assert adapter.release_calls == [
            "power_probe:startup",
            "power_probe:startup",
        ]
        capability = second.query_evidence()["inhibitor"]["capability"]
        assert capability["status"] == "unavailable"
        assert capability["reason"] == {
            "code": "power_inhibitor_release_failed"
        }
    finally:
        first_database.close()
        second_database.close()


def test_restart_adopts_exact_acquiring_holder_after_pre_activation_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = prepare_data_root(tmp_path / "acquire-before-activate-crash")
    upgrade_database(data_root.database)
    adapter = RecordingInhibitor()
    first_database = Database(data_root.database)
    first = RuntimeProtection(
        database=first_database,
        feed=DurableFeed(first_database),
        inhibitor=adapter,
        event_logger=RuntimeEventLogger(data_root.logs / "runtime.jsonl"),
    )
    effect = _effect("pre-activate-crash")

    def crash_before_activation(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated_daemon_crash_before_activation")

    monkeypatch.setattr(first, "_activate", crash_before_activation)
    with pytest.raises(RuntimeError, match="simulated_daemon_crash"):
        first.acquire(effect)
    with first_database.read() as connection:
        acquiring = connection.execute(
            text(
                "SELECT holder_ref, status FROM ar_execution_responsibilities "
                "WHERE responsibility_ref = :responsibility_ref"
            ),
            {"responsibility_ref": effect.responsibility_ref},
        ).mappings().one()
        assert acquiring["status"] == "acquiring"
        assert acquiring["holder_ref"] is not None
        holder_ref = str(acquiring["holder_ref"])
        assert connection.execute(
            text(
                "SELECT status FROM ar_power_inhibitor_epochs WHERE "
                "holder_ref = :holder_ref"
            ),
            {"holder_ref": holder_ref},
        ).scalar_one() == "acquiring"

    second_database = Database(data_root.database)
    second = RuntimeProtection(
        database=second_database,
        feed=DurableFeed(second_database),
        inhibitor=adapter,
        event_logger=RuntimeEventLogger(data_root.logs / "runtime.jsonl"),
    )
    try:
        assert adapter.acquire_calls == [holder_ref, holder_ref]
        assert adapter.native_acquire_count == 1
        with second_database.read() as connection:
            assert connection.execute(
                text("SELECT COUNT(*) FROM ar_power_inhibitor_epochs")
            ).scalar_one() == 1
            recovered = connection.execute(
                text(
                    "SELECT holder_ref, status FROM "
                    "ar_execution_responsibilities WHERE responsibility_ref = "
                    ":responsibility_ref"
                ),
                {"responsibility_ref": effect.responsibility_ref},
            ).mappings().one()
        assert recovered == {"holder_ref": holder_ref, "status": "acquiring"}

        permit = second.acquire(effect)
        assert permit.holder_ref == holder_ref
        assert adapter.acquire_calls == [holder_ref, holder_ref]

        _record_boundary(second_database, effect, boundary="terminal")
        second.finish(effect.responsibility_ref, boundary="terminal")
        assert adapter.live_holders == set()
    finally:
        first_database.close()
        second_database.close()


def test_transient_startup_adoption_failure_retries_same_holder_on_next_acquire(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = prepare_data_root(tmp_path / "transient-exact-holder-retry")
    upgrade_database(data_root.database)
    adapter = RecordingInhibitor()
    first_database = Database(data_root.database)
    first = RuntimeProtection(
        database=first_database,
        feed=DurableFeed(first_database),
        inhibitor=adapter,
        event_logger=RuntimeEventLogger(data_root.logs / "runtime.jsonl"),
    )
    effect = _effect("transient-exact-holder")

    monkeypatch.setattr(
        first,
        "_activate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("simulated_daemon_crash_before_activation")
        ),
    )
    with pytest.raises(RuntimeError, match="simulated_daemon_crash"):
        first.acquire(effect)
    exact_holder = adapter.acquire_calls[0]
    assert adapter.native_acquire_count == 1

    adapter.fail_code = "power_inhibitor_systemd_reconciliation_required"
    second_database = Database(data_root.database)
    second = RuntimeProtection(
        database=second_database,
        feed=DurableFeed(second_database),
        inhibitor=adapter,
        event_logger=RuntimeEventLogger(data_root.logs / "runtime.jsonl"),
    )
    try:
        adapter.fail_code = None
        permit = second.acquire(effect)

        assert permit.holder_ref == exact_holder
        assert adapter.acquire_calls == [
            exact_holder,
            exact_holder,
            exact_holder,
        ]
        assert adapter.native_acquire_count == 1
        with second_database.read() as connection:
            assert connection.execute(
                text("SELECT COUNT(*) FROM ar_power_inhibitor_epochs")
            ).scalar_one() == 1
    finally:
        first_database.close()
        second_database.close()


def test_same_process_waiting_retry_reuses_exact_holder(
    tmp_path: Path,
) -> None:
    adapter = RecordingInhibitor(
        fail_code="power_inhibitor_systemd_reconciliation_required"
    )
    protection, database = _coordinator(
        tmp_path / "same-process-waiting-holder",
        adapter,
    )
    effect = _effect("same-process-waiting-holder")
    try:
        with pytest.raises(RuntimeProtectionUnavailable):
            protection.acquire(effect)
        exact_holder = adapter.acquire_calls[0]

        adapter.fail_code = None
        permit = protection.acquire(effect)

        assert permit.holder_ref == exact_holder
        assert adapter.acquire_calls == [exact_holder, exact_holder]
        assert adapter.native_acquire_count == 1
        with database.read() as connection:
            assert connection.execute(
                text("SELECT COUNT(*) FROM ar_power_inhibitor_epochs")
            ).scalar_one() == 1
    finally:
        database.close()


def test_unknown_shared_hold_blocks_new_holder_until_exact_query_recovers(
    tmp_path: Path,
) -> None:
    adapter = RecordingInhibitor()
    protection, database = _coordinator(tmp_path / "unknown-shared-holder", adapter)
    first_effect = _effect("unknown-shared-first")
    next_effect = _effect("unknown-shared-next")
    try:
        first = protection.acquire(first_effect)
        adapter.hold_query_override = "unknown"

        with pytest.raises(RuntimeProtectionUnavailable) as blocked:
            protection.acquire(next_effect)

        assert blocked.value.code == "power_inhibitor_reconciliation_required"
        assert adapter.acquire_calls == [first.holder_ref]
        assert adapter.native_acquire_count == 1
        evidence = protection.query_evidence()
        assert evidence["inhibitor"]["holder_ref"] == first.holder_ref
        assert evidence["inhibitor"]["status"] == "active"
        assert evidence["inhibitor"]["capability"]["status"] == "unavailable"
        assert evidence["inhibitor"]["capability"]["reason"] == {
            "code": "power_inhibitor_reconciliation_required"
        }

        adapter.hold_query_override = None
        recovered = protection.acquire(next_effect)

        assert recovered.holder_ref == first.holder_ref
        assert adapter.acquire_calls == [first.holder_ref]
        assert adapter.native_acquire_count == 1
    finally:
        database.close()


def test_unknown_native_acquire_retries_same_holder_until_query_recovers(
    tmp_path: Path,
) -> None:
    adapter = RecordingInhibitor(hold_query_override="unknown")
    protection, database = _coordinator(tmp_path / "unknown-native-acquire", adapter)
    effect = _effect("unknown-native-acquire")
    try:
        with pytest.raises(RuntimeProtectionUnavailable) as first:
            protection.acquire(effect)
        exact_holder = adapter.acquire_calls[0]

        with pytest.raises(RuntimeProtectionUnavailable) as retried:
            protection.acquire(effect)

        assert first.value.code == "power_inhibitor_reconciliation_required"
        assert retried.value.code == "power_inhibitor_reconciliation_required"
        assert adapter.acquire_calls == [exact_holder, exact_holder]
        assert adapter.native_acquire_count == 1
        assert protection.query_evidence()["inhibitor"]["status"] == "acquiring"

        adapter.hold_query_override = None
        permit = protection.acquire(effect)

        assert permit.holder_ref == exact_holder
        assert adapter.acquire_calls == [exact_holder, exact_holder, exact_holder]
        assert adapter.native_acquire_count == 1
    finally:
        database.close()


def test_no_effect_finish_keeps_unknown_native_acquisition_until_exact_release(
    tmp_path: Path,
) -> None:
    adapter = RecordingInhibitor(hold_query_override="unknown")
    protection, database = _coordinator(
        tmp_path / "unknown-native-no-effect-finish",
        adapter,
    )
    effect = _effect("unknown-native-no-effect-finish")
    successor = _effect("after-unknown-native-no-effect-finish")
    try:
        with pytest.raises(RuntimeProtectionUnavailable) as acquisition:
            protection.acquire(effect)
        assert acquisition.value.code == "power_inhibitor_reconciliation_required"
        exact_holder = adapter.acquire_calls[0]
        _record_boundary(
            database,
            effect,
            boundary="checkpoint",
            checkpoint_ref="checkpoint:provider-not-started",
        )

        with pytest.raises(RuntimeProtectionUnavailable) as unresolved:
            protection.finish(
                effect.responsibility_ref,
                boundary="checkpoint",
                checkpoint_ref="checkpoint:provider-not-started",
            )

        assert unresolved.value.code == "power_inhibitor_reconciliation_required"
        evidence = protection.query_evidence()
        assert evidence["inhibitor"]["status"] == "acquiring"
        assert [
            item["responsibility_ref"] for item in evidence["durable_waiting"]
        ] == [effect.responsibility_ref]
        with pytest.raises(RuntimeProtectionUnavailable) as blocked:
            protection.acquire(successor)
        assert blocked.value.code == "power_inhibitor_reconciliation_required"
        assert adapter.acquire_calls == [exact_holder]
        assert adapter.release_calls == []

        adapter.hold_query_override = None
        protection.finish(
            effect.responsibility_ref,
            boundary="checkpoint",
            checkpoint_ref="checkpoint:provider-not-started",
        )

        assert adapter.release_calls == [exact_holder]
        assert adapter.live_holders == set()
        permit = protection.acquire(successor)
        assert permit.holder_ref != exact_holder
    finally:
        database.close()


def test_restart_preserves_unknown_no_effect_finish_until_exact_query_recovers(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "unknown-no-effect-finish-restart")
    upgrade_database(data_root.database)
    adapter = RecordingInhibitor(hold_query_override="unknown")
    first_database = Database(data_root.database)
    first_feed = DurableFeed(first_database)
    first_feed.ensure_initialized()
    first = RuntimeProtection(
        database=first_database,
        feed=first_feed,
        inhibitor=adapter,
        event_logger=RuntimeEventLogger(data_root.logs / "runtime.jsonl"),
    )
    effect = _effect("unknown-no-effect-finish-restart")
    successor = _effect("after-unknown-no-effect-finish-restart")
    with pytest.raises(RuntimeProtectionUnavailable):
        first.acquire(effect)
    exact_holder = adapter.acquire_calls[0]
    _record_boundary(
        first_database,
        effect,
        boundary="checkpoint",
        checkpoint_ref="checkpoint:provider-not-started-restart",
    )
    with pytest.raises(RuntimeProtectionUnavailable):
        first.finish(
            effect.responsibility_ref,
            boundary="checkpoint",
            checkpoint_ref="checkpoint:provider-not-started-restart",
        )

    second_database = Database(data_root.database)
    try:
        second = RuntimeProtection(
            database=second_database,
            feed=DurableFeed(second_database),
            inhibitor=adapter,
            event_logger=RuntimeEventLogger(data_root.logs / "runtime.jsonl"),
            startup_probe=True,
        )

        with pytest.raises(RuntimeProtectionUnavailable) as blocked:
            second.acquire(successor)
        assert blocked.value.code == "power_inhibitor_reconciliation_required"
        assert adapter.acquire_calls == [exact_holder]
        assert adapter.release_calls == []

        adapter.hold_query_override = None
        permit = second.acquire(successor)

        assert permit.holder_ref != exact_holder
        assert adapter.release_calls == [exact_holder]
        assert adapter.acquire_calls == [exact_holder, permit.holder_ref]
    finally:
        first_database.close()
        second_database.close()


@pytest.mark.parametrize(
    "reason_code",
    [
        "power_inhibitor_systemd_reconciliation_required",
        "power_inhibitor_windows_guardian_reconciliation_failed",
    ],
)
def test_backend_uncertainty_keeps_exact_no_effect_finish_recoverable(
    tmp_path: Path,
    reason_code: str,
) -> None:
    adapter = RecordingInhibitor(
        fail_after_acquire_code=reason_code,
        hold_query_override="unknown",
    )
    protection, database = _coordinator(
        tmp_path / reason_code,
        adapter,
    )
    effect = _effect(reason_code)
    successor = _effect(f"after-{reason_code}")
    try:
        with pytest.raises(RuntimeProtectionUnavailable) as acquisition:
            protection.acquire(effect)
        assert acquisition.value.code == reason_code
        exact_holder = adapter.acquire_calls[0]
        adapter.fail_after_acquire_code = None
        _record_boundary(
            database,
            effect,
            boundary="checkpoint",
            checkpoint_ref=f"checkpoint:{reason_code}",
        )

        with pytest.raises(RuntimeProtectionUnavailable) as unresolved:
            protection.finish(
                effect.responsibility_ref,
                boundary="checkpoint",
                checkpoint_ref=f"checkpoint:{reason_code}",
            )
        assert unresolved.value.code == "power_inhibitor_reconciliation_required"
        with pytest.raises(RuntimeProtectionUnavailable):
            protection.acquire(successor)
        assert adapter.acquire_calls == [exact_holder]
        assert adapter.release_calls == []

        adapter.hold_query_override = None
        protection.finish(
            effect.responsibility_ref,
            boundary="checkpoint",
            checkpoint_ref=f"checkpoint:{reason_code}",
        )

        assert adapter.release_calls == [exact_holder]
        assert adapter.live_holders == set()
        assert protection.acquire(successor).holder_ref != exact_holder
    finally:
        database.close()


def test_post_issuance_backend_reconciliation_keeps_exact_holder_until_finish(
    tmp_path: Path,
) -> None:
    adapter = RecordingInhibitor(
        fail_after_acquire_code=(
            "power_inhibitor_windows_guardian_reconciliation_failed"
        ),
        hold_query_override="unknown",
    )
    protection, database = _coordinator(
        tmp_path / "post-issuance-exception",
        adapter,
    )
    effect = _effect("post-issuance-exception")
    successor = _effect("after-post-issuance-exception")
    try:
        with pytest.raises(RuntimeProtectionUnavailable) as acquisition:
            protection.acquire(effect)
        assert acquisition.value.code == (
            "power_inhibitor_windows_guardian_reconciliation_failed"
        )
        exact_holder = adapter.acquire_calls[0]
        assert exact_holder in adapter.live_holders
        assert adapter.native_acquire_count == 1
        adapter.fail_after_acquire_code = None
        _record_boundary(
            database,
            effect,
            boundary="checkpoint",
            checkpoint_ref="checkpoint:post-issuance-exception",
        )

        with pytest.raises(RuntimeProtectionUnavailable) as unresolved:
            protection.finish(
                effect.responsibility_ref,
                boundary="checkpoint",
                checkpoint_ref="checkpoint:post-issuance-exception",
            )
        assert unresolved.value.code == "power_inhibitor_reconciliation_required"
        with pytest.raises(RuntimeProtectionUnavailable) as blocked:
            protection.acquire(successor)
        assert blocked.value.code == "power_inhibitor_reconciliation_required"
        assert adapter.acquire_calls == [exact_holder]
        assert adapter.native_acquire_count == 1

        adapter.hold_query_override = None
        protection.finish(
            effect.responsibility_ref,
            boundary="checkpoint",
            checkpoint_ref="checkpoint:post-issuance-exception",
        )
        assert adapter.release_calls == [exact_holder]
        assert exact_holder not in adapter.live_holders

        replacement = protection.acquire(successor)
        assert replacement.holder_ref != exact_holder
        assert adapter.native_acquire_count == 2
    finally:
        database.close()


def test_exact_absence_closes_no_effect_acquisition_without_a_release_lease(
    tmp_path: Path,
) -> None:
    adapter = RecordingInhibitor(
        fail_after_acquire_code=(
            "power_inhibitor_windows_guardian_reconciliation_failed"
        ),
        hold_query_override="absent",
        exact_absence_without_lease=True,
    )
    protection, database = _coordinator(
        tmp_path / "no-effect-exact-absence",
        adapter,
    )
    effect = _effect("no-effect-exact-absence")
    successor = _effect("after-no-effect-exact-absence")
    try:
        with pytest.raises(RuntimeProtectionUnavailable):
            protection.acquire(effect)
        exact_holder = adapter.acquire_calls[0]
        adapter.fail_after_acquire_code = None
        adapter.live_holders.clear()
        _record_boundary(
            database,
            effect,
            boundary="checkpoint",
            checkpoint_ref="checkpoint:exact-absence",
        )

        protection.finish(
            effect.responsibility_ref,
            boundary="checkpoint",
            checkpoint_ref="checkpoint:exact-absence",
        )

        evidence = protection.query_evidence()
        assert evidence["inhibitor"]["status"] == "idle"
        assert evidence["durable_waiting"] == []
        assert adapter.release_calls == []
        adapter.hold_query_override = None
        permit = protection.acquire(successor)
        assert permit.holder_ref != exact_holder
    finally:
        database.close()


def test_restart_retries_exact_releasing_epoch_after_pre_release_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = prepare_data_root(tmp_path / "releasing-before-native-crash")
    upgrade_database(data_root.database)
    adapter = RecordingInhibitor()
    first_database = Database(data_root.database)
    first = RuntimeProtection(
        database=first_database,
        feed=DurableFeed(first_database),
        inhibitor=adapter,
        event_logger=RuntimeEventLogger(data_root.logs / "runtime.jsonl"),
    )
    effect = _effect("pre-release-crash")
    permit = first.acquire(effect)
    _record_boundary(first_database, effect, boundary="terminal")

    def crash_before_native_release(_lease: InhibitorLease) -> None:
        raise RuntimeError("simulated_daemon_crash_before_native_release")

    monkeypatch.setattr(first, "_release_epoch", crash_before_native_release)
    with pytest.raises(RuntimeError, match="simulated_daemon_crash"):
        first.finish(effect.responsibility_ref, boundary="terminal")
    with first_database.read() as connection:
        assert connection.execute(
            text(
                "SELECT status FROM ar_power_inhibitor_epochs WHERE "
                "holder_ref = :holder_ref"
            ),
            {"holder_ref": permit.holder_ref},
        ).scalar_one() == "releasing"
    assert adapter.release_calls == []

    second_database = Database(data_root.database)
    second = RuntimeProtection(
        database=second_database,
        feed=DurableFeed(second_database),
        inhibitor=adapter,
        event_logger=RuntimeEventLogger(data_root.logs / "runtime.jsonl"),
    )
    try:
        assert adapter.release_calls == [permit.holder_ref]
        assert adapter.live_holders == set()
        with second_database.read() as connection:
            assert connection.execute(
                text(
                    "SELECT status FROM ar_power_inhibitor_epochs WHERE "
                    "holder_ref = :holder_ref"
                ),
                {"holder_ref": permit.holder_ref},
            ).scalar_one() == "released"
    finally:
        first_database.close()
        second_database.close()


def test_observability_counts_are_exact_when_diagnostic_pages_are_truncated(
    tmp_path: Path,
) -> None:
    adapter = RecordingInhibitor()
    protection, database = _coordinator(tmp_path / "exact-observability-counts", adapter)
    try:
        for sequence in range(105):
            protection.acquire(_effect(f"page-{sequence}"))
        assert protection.interrupt_active(
            interruption_kind="daemon",
            reason_code="daemon_shutdown_requested",
        ) == 105

        evidence = protection.query_evidence()
        assert len(evidence["durable_waiting"]) == 100
        assert evidence["durable_waiting_count"] == 105
        assert evidence["durable_waiting_page_truncated"] is True
        assert len(evidence["interruptions"]) == 100
        assert evidence["interruption_count"] == 105
        assert evidence["interruption_page_truncated"] is True
    finally:
        database.close()


def test_unrelated_completion_cannot_release_shared_power_hold(
    tmp_path: Path,
) -> None:
    adapter = RecordingInhibitor()
    protection, database = _coordinator(tmp_path / "refcount", adapter)
    try:
        first = protection.acquire(_effect("first"))
        second = protection.acquire(_effect("second"))

        assert first.status == second.status == "active"
        assert first.holder_ref == second.holder_ref
        assert len(adapter.acquire_calls) == 1
        active_evidence = protection.query_evidence()
        assert active_evidence["inhibitor"]["active_count"] == 2
        assert active_evidence["inhibitor"]["capability"]["status"] == "ready"

        _record_boundary(
            database,
            _effect("first"),
            boundary="checkpoint",
            checkpoint_ref="checkpoint:first",
        )
        protection.finish(
            _effect("first").responsibility_ref,
            boundary="checkpoint",
            checkpoint_ref="checkpoint:first",
        )

        assert adapter.release_calls == []
        assert protection.query_evidence()["inhibitor"]["active_count"] == 1

        _record_boundary(database, _effect("second"), boundary="terminal")
        protection.finish(
            _effect("second").responsibility_ref,
            boundary="terminal",
        )
        assert adapter.release_calls == adapter.acquire_calls
        assert protection.query_evidence()["inhibitor"]["status"] == "idle"
    finally:
        database.close()


def test_acquire_failure_is_durable_typed_and_fail_closed(tmp_path: Path) -> None:
    adapter = RecordingInhibitor(fail_code="power_inhibitor_systemd_unavailable")
    protection, database = _coordinator(tmp_path / "failed", adapter)
    effect = _effect("blocked")
    try:
        with pytest.raises(RuntimeProtectionUnavailable) as raised:
            protection.acquire(effect)

        assert raised.value.code == "power_inhibitor_systemd_unavailable"
        evidence = protection.query_evidence()
        assert evidence["inhibitor"]["status"] == "unavailable"
        assert evidence["inhibitor"]["active_count"] == 0
        assert evidence["inhibitor"]["capability"]["status"] == "unavailable"
        assert evidence["inhibitor"]["capability"]["reason"] == {
            "code": "power_inhibitor_systemd_unavailable"
        }
        assert evidence["durable_waiting"] == [
            {
                "responsibility_ref": effect.responsibility_ref,
                "operation_ref": effect.operation_ref,
                "effect_kind": effect.effect_kind,
                "reason": {"code": "power_inhibitor_systemd_unavailable"},
            }
        ]
    finally:
        database.close()


def test_release_requires_owner_recorded_boundary_evidence(tmp_path: Path) -> None:
    adapter = RecordingInhibitor()
    protection, database = _coordinator(tmp_path / "owner-boundary", adapter)
    effect = _effect("unverified")
    try:
        protection.acquire(effect)

        with pytest.raises(RuntimeProtectionUnavailable) as raised:
            protection.finish(
                effect.responsibility_ref,
                boundary="checkpoint",
                checkpoint_ref="checkpoint:caller-only",
            )

        assert raised.value.code == "runtime_boundary_evidence_missing"
        assert protection.query_evidence()["inhibitor"]["active_count"] == 1
        assert adapter.release_calls == []
    finally:
        database.close()


def test_restart_records_interruption_and_rejects_the_old_incarnation(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "restart")
    upgrade_database(data_root.database)
    adapter = RecordingInhibitor()
    first_database = Database(data_root.database)
    first_feed = DurableFeed(first_database)
    first_feed.ensure_initialized()
    first = RuntimeProtection(
        database=first_database,
        feed=first_feed,
        inhibitor=adapter,
        event_logger=RuntimeEventLogger(data_root.logs / "runtime.jsonl"),
        clock=lambda: 1_720_000_000.0,
    )
    effect = _effect("restart")
    first.acquire(effect)

    second_database = Database(data_root.database)
    second_feed = DurableFeed(second_database)
    second = RuntimeProtection(
        database=second_database,
        feed=second_feed,
        inhibitor=adapter,
        event_logger=RuntimeEventLogger(data_root.logs / "runtime.jsonl"),
        clock=lambda: 1_720_000_001.0,
    )
    try:
        evidence = second.query_evidence()
        assert evidence["inhibitor"]["status"] == "active"
        assert evidence["inhibitor"]["active_count"] == 1
        assert evidence["durable_waiting"][0]["reason"] == {
            "code": "daemon_restarted"
        }
        assert evidence["interruptions"][0]["old_fence_ref"] == effect.fence_ref
        assert evidence["interruptions"][0]["reconciliation_status"] == (
            "protected"
        )

        with pytest.raises(RuntimeProtectionUnavailable) as reacquire:
            second.acquire(effect)
        assert reacquire.value.code == "runtime_reconciliation_required"

        with pytest.raises(RuntimeProtectionUnavailable) as stale_finish:
            first.finish(
                effect.responsibility_ref,
                boundary="checkpoint",
                checkpoint_ref="checkpoint:late",
            )
        assert stale_finish.value.code == "runtime_incarnation_stale"

        _record_boundary(
            second_database,
            effect,
            boundary="checkpoint",
            checkpoint_ref="checkpoint:recovered-owner-commit",
        )
        second.finish(
            effect.responsibility_ref,
            boundary="checkpoint",
            checkpoint_ref="checkpoint:recovered-owner-commit",
        )
        assert adapter.release_calls == adapter.acquire_calls
    finally:
        first_database.close()
        second_database.close()


def test_restart_reacquires_lost_hold_before_recovery_can_continue(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "restart-reacquire")
    upgrade_database(data_root.database)
    adapter = RecordingInhibitor()
    first_database = Database(data_root.database)
    first_feed = DurableFeed(first_database)
    first_feed.ensure_initialized()
    first = RuntimeProtection(
        database=first_database,
        feed=first_feed,
        inhibitor=adapter,
        event_logger=RuntimeEventLogger(data_root.logs / "runtime.jsonl"),
    )
    effect = _effect("lost-on-restart")
    original = first.acquire(effect)
    adapter.live_holders.clear()

    second_database = Database(data_root.database)
    second = RuntimeProtection(
        database=second_database,
        feed=DurableFeed(second_database),
        inhibitor=adapter,
        event_logger=RuntimeEventLogger(data_root.logs / "runtime.jsonl"),
    )
    try:
        evidence = second.query_evidence()
        assert len(adapter.acquire_calls) == 2
        assert evidence["inhibitor"]["status"] == "active"
        assert evidence["inhibitor"]["holder_ref"] != original.holder_ref
        assert evidence["inhibitor"]["active_count"] == 1
        assert any(
            item["reconciliation_status"] == "protected"
            for item in evidence["interruptions"]
        )
    finally:
        first_database.close()
        second_database.close()


def test_restart_adopts_exact_interrupted_replacement_after_native_acquire_crash(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "interrupted-replacement-crash")
    upgrade_database(data_root.database)
    adapter = RecordingInhibitor()
    first_database = Database(data_root.database)
    first = RuntimeProtection(
        database=first_database,
        feed=DurableFeed(first_database),
        inhibitor=adapter,
        event_logger=RuntimeEventLogger(data_root.logs / "runtime.jsonl"),
    )
    effect = _effect("interrupted-replacement-crash")
    original = first.acquire(effect)
    adapter.live_holders.remove(original.holder_ref)
    adapter.crash_after_next_native_acquire = True

    crashed_database = Database(data_root.database)
    with pytest.raises(SimulatedProcessDeath):
        RuntimeProtection(
            database=crashed_database,
            feed=DurableFeed(crashed_database),
            inhibitor=adapter,
            event_logger=RuntimeEventLogger(data_root.logs / "runtime.jsonl"),
        )
    replacement_holder = adapter.acquire_calls[-1]
    assert replacement_holder != original.holder_ref

    recovered_database = Database(data_root.database)
    recovered = RuntimeProtection(
        database=recovered_database,
        feed=DurableFeed(recovered_database),
        inhibitor=adapter,
        event_logger=RuntimeEventLogger(data_root.logs / "runtime.jsonl"),
    )
    try:
        assert adapter.acquire_calls == [
            original.holder_ref,
            replacement_holder,
            replacement_holder,
        ]
        assert adapter.native_acquire_count == 2
        assert adapter.live_holders == {replacement_holder}

        permit = recovered.acquire(_effect("interrupted-replacement-next"))

        assert permit.holder_ref == replacement_holder
        assert adapter.acquire_calls[-2:] == [
            replacement_holder,
            replacement_holder,
        ]
    finally:
        first_database.close()
        crashed_database.close()
        recovered_database.close()


def test_restart_unknown_hold_waits_for_exact_query_without_replacement(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "restart-unknown-holder")
    upgrade_database(data_root.database)
    adapter = RecordingInhibitor()
    first_database = Database(data_root.database)
    first = RuntimeProtection(
        database=first_database,
        feed=DurableFeed(first_database),
        inhibitor=adapter,
        event_logger=RuntimeEventLogger(data_root.logs / "runtime.jsonl"),
    )
    original_effect = _effect("restart-unknown-original")
    original = first.acquire(original_effect)
    adapter.hold_query_override = "unknown"

    second_database = Database(data_root.database)
    second = RuntimeProtection(
        database=second_database,
        feed=DurableFeed(second_database),
        inhibitor=adapter,
        event_logger=RuntimeEventLogger(data_root.logs / "runtime.jsonl"),
        startup_probe=True,
    )
    try:
        assert adapter.acquire_calls == [original.holder_ref]
        assert adapter.native_acquire_count == 1
        evidence = second.query_evidence()
        assert evidence["inhibitor"]["holder_ref"] == original.holder_ref
        assert evidence["inhibitor"]["status"] == "active"
        assert evidence["inhibitor"]["capability"]["reason"] == {
            "code": "power_inhibitor_reconciliation_required"
        }
        assert any(
            item["reconciliation_status"] == "required"
            for item in evidence["interruptions"]
        )

        adapter.hold_query_override = None
        recovered = second.acquire(_effect("restart-unknown-next"))

        assert recovered.holder_ref == original.holder_ref
        assert adapter.acquire_calls == [original.holder_ref]
        assert adapter.native_acquire_count == 1
    finally:
        first_database.close()
        second_database.close()


def test_restart_finishes_an_interrupted_responsibility_with_recorded_owner_receipt(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "restart-recorded-boundary")
    upgrade_database(data_root.database)
    adapter = RecordingInhibitor()
    first_database = Database(data_root.database)
    first_feed = DurableFeed(first_database)
    first_feed.ensure_initialized()
    first = RuntimeProtection(
        database=first_database,
        feed=first_feed,
        inhibitor=adapter,
        event_logger=RuntimeEventLogger(data_root.logs / "runtime.jsonl"),
    )
    effect = _effect("owner-commit-before-finish-ack")
    first.acquire(effect)
    _record_boundary(
        first_database,
        effect,
        boundary="checkpoint",
        checkpoint_ref="checkpoint:owner-commit-before-finish-ack",
    )

    second_database = Database(data_root.database)
    second = RuntimeProtection(
        database=second_database,
        feed=DurableFeed(second_database),
        inhibitor=adapter,
        event_logger=RuntimeEventLogger(data_root.logs / "runtime.jsonl"),
    )
    try:
        evidence = second.query_evidence()
        assert evidence["responsibilities"] == []
        assert evidence["inhibitor"]["active_count"] == 0
        assert evidence["inhibitor"]["status"] == "idle"
        assert adapter.release_calls == adapter.acquire_calls
    finally:
        first_database.close()
        second_database.close()


def test_restart_does_not_reacquire_for_an_already_recorded_owner_boundary(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "restart-recorded-boundary-no-reacquire")
    upgrade_database(data_root.database)
    adapter = RecordingInhibitor()
    first_database = Database(data_root.database)
    first_feed = DurableFeed(first_database)
    first_feed.ensure_initialized()
    first = RuntimeProtection(
        database=first_database,
        feed=first_feed,
        inhibitor=adapter,
        event_logger=RuntimeEventLogger(data_root.logs / "runtime.jsonl"),
    )
    effect = _effect("settled-before-restart")
    first.acquire(effect)
    _record_boundary(first_database, effect, boundary="terminal")
    adapter.live_holders.clear()
    adapter.fail_code = "power_inhibitor_systemd_unavailable"

    second_database = Database(data_root.database)
    second = RuntimeProtection(
        database=second_database,
        feed=DurableFeed(second_database),
        inhibitor=adapter,
        event_logger=RuntimeEventLogger(data_root.logs / "runtime.jsonl"),
    )
    try:
        evidence = second.query_evidence()
        assert len(adapter.acquire_calls) == 1
        assert evidence["responsibilities"] == []
        assert evidence["durable_waiting"] == []
        assert evidence["inhibitor"]["active_count"] == 0
    finally:
        first_database.close()
        second_database.close()


def test_next_acquire_replays_a_same_incarnation_owner_boundary_ack_loss(
    tmp_path: Path,
) -> None:
    adapter = RecordingInhibitor()
    protection, database = _coordinator(tmp_path / "same-incarnation-ack-loss", adapter)
    settled = _effect("same-incarnation-settled")
    successor = _effect("same-incarnation-successor")
    try:
        first = protection.acquire(settled)
        _record_boundary(database, settled, boundary="terminal")

        second = protection.acquire(successor)

        assert second.holder_ref != first.holder_ref
        assert adapter.release_calls == [first.holder_ref]
        assert len(adapter.acquire_calls) == 2
        evidence = protection.query_evidence()
        assert [
            item["responsibility_ref"] for item in evidence["responsibilities"]
        ] == [successor.responsibility_ref]
        with database.read() as connection:
            status = connection.exec_driver_sql(
                "SELECT status FROM ar_execution_responsibilities "
                "WHERE responsibility_ref = ?",
                (settled.responsibility_ref,),
            ).scalar_one()
        assert status == "finished"
    finally:
        database.close()


def test_release_failure_is_retryable_without_reopening_finished_work(
    tmp_path: Path,
) -> None:
    adapter = RecordingInhibitor(
        release_fail_code="power_inhibitor_release_failed"
    )
    protection, database = _coordinator(tmp_path / "release-pending", adapter)
    finished = _effect("release-pending")
    next_effect = _effect("after-release-retry")
    try:
        protection.acquire(finished)
        _record_boundary(database, finished, boundary="terminal")
        protection.finish(finished.responsibility_ref, boundary="terminal")

        evidence = protection.query_evidence()
        assert evidence["inhibitor"]["status"] == "release_pending"
        assert evidence["inhibitor"]["active_count"] == 0

        adapter.release_fail_code = None
        protection.acquire(next_effect)
        assert len(adapter.release_calls) == 2
        assert protection.query_evidence()["inhibitor"]["active_count"] == 1
        with pytest.raises(RuntimeProtectionUnavailable) as replay:
            protection.acquire(finished)
        assert replay.value.code == "runtime_responsibility_already_finished"
    finally:
        database.close()


def test_telemetry_is_default_off_and_revoke_stops_future_exports(
    tmp_path: Path,
) -> None:
    adapter = RecordingInhibitor()
    protection, database = _coordinator(tmp_path / "telemetry", adapter)
    exporter = RecordingTelemetryExporter()
    try:
        assert protection.query_evidence()["telemetry"]["mode"] == "disabled"
        protection.enable_telemetry(
            exporter,
            authorization_ref="telemetry_authorization_grant_1",
        )
        assert exporter.exported.wait(timeout=1)
        before_revoke_count = len(exporter.events)
        correlation_ref = protection.query_evidence()["correlation_ref"]

        protection.revoke_telemetry(
            authorization_ref="telemetry_authorization_revoke_1"
        )
        protection.acquire(_effect("local-after-revoke"))

        assert len(exporter.events) == before_revoke_count
        assert exporter.closed.wait(timeout=1)
        evidence = protection.query_evidence()
        assert evidence["telemetry"]["mode"] == "revoked"
        assert evidence["correlation_ref"] == correlation_ref
        assert evidence["inhibitor"]["active_count"] == 1
    finally:
        protection.close()
        database.close()


def test_runtime_log_rotates_and_never_serializes_unapproved_payload(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.jsonl"
    logger = RuntimeEventLogger(path, max_bytes=220, backup_count=2)
    forbidden = "prompt=TOP-SECRET /home/alice/private.txt raw stdout"

    for sequence in range(12):
        logger.record(
            event_code="runtime.effect.active",
            status="active",
            component="runtime_protection",
            correlation={
                "run_ref": f"run:{sequence}",
                "operation_ref": f"operation:{sequence}",
            },
            unsafe_payload={"prompt": forbidden},
        )

    generations = sorted(tmp_path.glob("runtime.jsonl*"))
    assert 1 < len(generations) <= 3
    content = "".join(item.read_text(encoding="utf-8") for item in generations)
    assert forbidden not in content
    assert "unsafe_payload" not in content
    for item in generations:
        for line in item.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            assert set(event) <= {
                "schema_ref",
                "recorded_at",
                "level",
                "component",
                "event_code",
                "status",
                "correlation",
                "reason_code",
                "active_count",
            }
