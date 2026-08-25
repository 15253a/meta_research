from __future__ import annotations

import hashlib
import os
import stat
from copy import deepcopy
from pathlib import Path

import pytest
from sqlalchemy import text

import meta_research.owners.writing_delivery_runtime as writing_delivery_runtime_module
import meta_research.writing_delivery as writing_delivery_module
from meta_research.database import Database
from meta_research.feed import DurableFeed
from meta_research.migration import upgrade_database
from meta_research.owners.common import (
    AcceptanceReceipt,
    OwnerConflict,
    canonical_hash,
    canonical_json,
)
from meta_research.owners.writing_delivery_runtime import SQLiteWritingDeliveryRuntime
from meta_research.runtime_protection import (
    InhibitorLease,
    RuntimeEventLogger,
    RuntimeProtection,
    RuntimeProtectionUnavailable,
)
from meta_research.writing_delivery import (
    InMemoryWritingDeliveryProvider,
    LocalFilesystemWritingDeliveryProvider,
    WritingDeliveryOutcomeUnknown,
    WritingDeliveryProviderObservation,
    WritingDeliveryProviderRegistry,
    derive_writing_delivery_operation_ref,
    normalize_writing_delivery_payload,
    normalize_writing_delivery_target,
)


_HASH = "a" * 64


def _receipt(issuer: str, kind: str, subject_ref: str, payload_hash: str) -> dict[str, str]:
    return AcceptanceReceipt(
        issuer=issuer,
        kind=kind,
        receipt_ref=f"receipt:{subject_ref}",
        subject_ref=subject_ref,
        payload_hash=payload_hash,
    ).as_public_dict()


def _payload(tmp_path: Path, *, nonce: str = "delivery-request-1") -> dict[str, object]:
    target = normalize_writing_delivery_target(
        "local-filesystem",
        "publish",
        {
            "path": str((tmp_path / "paper.html").resolve()),
            "permissions": 0o600,
            "expected_existing_hash": None,
        },
    )
    binding_provider = InMemoryWritingDeliveryProvider()
    binding_provider.provider_ref = "local-filesystem"
    body: dict[str, object] = {
        "schema_ref": "meta-research/writing-external-delivery/v1",
        "request_nonce": nonce,
        "action": "publish",
        "provider_ref": "local-filesystem",
        "target": target,
        "target_binding": binding_provider.verify_target_current(
            "publish", target
        ),
        "effects": [
            {
                "effect_kind": "create_file",
                "target_ref": str((tmp_path / "paper.html").resolve()),
                "destructive": False,
            }
        ],
        "run_ref": "writing-run:1",
        "document_type": "paper",
        "asset_ref": "asset:source",
        "version_ref": "version:source",
        "content_hash": "1" * 64,
        "manifest_hash": "2" * 64,
        "version_receipt": _receipt(
            "research_memory", "asset_version_accepted", "version:source", "3" * 64
        ),
        "citation_decision_ref": "citation:1",
        "citation_receipt": _receipt(
            "research_graph", "writing_citations_accepted", "citation:1", "4" * 64
        ),
        "renderer_asset_ref": "asset:renderer",
        "renderer_version_ref": "version:renderer",
        "renderer_content_hash": "5" * 64,
        "renderer_manifest_hash": "6" * 64,
        "renderer_artifact_sha256": hashlib.sha256(b"rendered").hexdigest(),
        "renderer_format": "html",
        "renderer_media_type": "text/html; charset=utf-8",
        "renderer_receipt": _receipt(
            "research_memory",
            "asset_version_accepted",
            "version:renderer",
            "7" * 64,
        ),
    }
    body["operation_ref"] = derive_writing_delivery_operation_ref(body)
    return normalize_writing_delivery_payload(body)


def test_payload_identity_is_exact_nonce_bound_and_rejects_target_drift(
    tmp_path: Path,
) -> None:
    first = _payload(tmp_path, nonce="delivery-request-1")
    replay = normalize_writing_delivery_payload(dict(first))
    second = _payload(tmp_path, nonce="delivery-request-2")

    assert replay == first
    assert first["operation_ref"] != second["operation_ref"]

    drifted_binding = deepcopy(first)
    provider_binding = drifted_binding["target_binding"]["provider_binding"]
    provider_binding["target_hash"] = "b" * 64
    drifted_binding["target_binding"]["provider_binding_hash"] = canonical_hash(
        provider_binding
    )
    with pytest.raises(
        OwnerConflict, match="writing_delivery_operation_identity_invalid"
    ):
        normalize_writing_delivery_payload(drifted_binding)

    with pytest.raises(OwnerConflict, match="writing_delivery_payload_invalid"):
        normalize_writing_delivery_payload({**first, "unexpected": True})
    with pytest.raises(OwnerConflict, match="writing_delivery_target_invalid"):
        normalize_writing_delivery_target(
            "local-filesystem",
            "publish",
            {
                "path": "relative/paper.html",
                "permissions": 0o600,
                "expected_existing_hash": None,
            },
        )
    with pytest.raises(OwnerConflict, match="writing_delivery_target_invalid"):
        normalize_writing_delivery_target(
            "local-filesystem",
            "overwrite",
            {
                "path": str((tmp_path / "paper.html").resolve()),
                "permissions": 0o600,
                "expected_existing_hash": None,
            },
        )


def test_local_filesystem_provider_performs_only_safe_real_publish(
    tmp_path: Path,
) -> None:
    provider = LocalFilesystemWritingDeliveryProvider()
    assert provider.supported_actions == frozenset({"publish"})
    (tmp_path / "delivery").mkdir()
    target = normalize_writing_delivery_target(
        "local-filesystem",
        "publish",
        {
            "path": str((tmp_path / "delivery" / "paper.html").resolve()),
            "permissions": 0o600,
            "expected_existing_hash": None,
        },
    )
    artifact = b"first rendered paper"
    request = provider.request(
        operation_ref="writing_delivery:" + "1" * 48,
        action="publish",
        target=target,
        target_binding=provider.verify_target_current("publish", target),
        artifact=artifact,
        artifact_sha256=hashlib.sha256(artifact).hexdigest(),
    )

    observation = provider.execute(request)
    delivered = Path(str(target["path"]))

    assert observation.outcome == "completed"
    assert delivered.read_bytes() == artifact
    assert stat.S_IMODE(delivered.stat().st_mode) == 0o600
    assert not list(delivered.parent.glob(".*.writing-delivery-*"))
    assert provider.reconcile(request).outcome == "completed"

    overwrite_target = normalize_writing_delivery_target(
        "local-filesystem",
        "overwrite",
        {
            "path": str(delivered),
            "permissions": 0o600,
            "expected_existing_hash": hashlib.sha256(artifact).hexdigest(),
        },
    )
    replacement = b"replacement rendered paper"
    with pytest.raises(OwnerConflict, match="writing_delivery_provider_request_invalid"):
        provider.request(
            operation_ref="writing_delivery:" + "2" * 48,
            action="overwrite",
            target=overwrite_target,
            target_binding=provider.verify_target_current(
                "overwrite", overwrite_target
            ),
            artifact=replacement,
            artifact_sha256=hashlib.sha256(replacement).hexdigest(),
        )
    assert delivered.read_bytes() == artifact

    delete_target = normalize_writing_delivery_target(
        "local-filesystem",
        "delete",
        {
            "path": str(delivered),
            "permissions": 0o600,
            "expected_existing_hash": hashlib.sha256(artifact).hexdigest(),
        },
    )
    with pytest.raises(OwnerConflict, match="writing_delivery_provider_request_invalid"):
        provider.request(
            operation_ref="writing_delivery:" + "3" * 48,
            action="delete",
            target=delete_target,
            target_binding=provider.verify_target_current(
                "delete", delete_target
            ),
            artifact=None,
            artifact_sha256=hashlib.sha256(artifact).hexdigest(),
        )
    assert delivered.read_bytes() == artifact


def test_local_filesystem_provider_rejects_symlink_and_existing_publish_target(
    tmp_path: Path,
) -> None:
    provider = LocalFilesystemWritingDeliveryProvider()
    real_directory = tmp_path / "real"
    real_directory.mkdir()
    linked_directory = tmp_path / "linked"
    linked_directory.symlink_to(real_directory, target_is_directory=True)
    with pytest.raises(OwnerConflict, match="writing_delivery_target_invalid"):
        normalize_writing_delivery_target(
            "local-filesystem",
            "publish",
            {
                "path": str(linked_directory / "artifact.html"),
                "permissions": 0o600,
                "expected_existing_hash": None,
            },
        )

    target = real_directory / "artifact.html"
    target.write_bytes(b"old")
    target.chmod(0o600)
    existing_target = normalize_writing_delivery_target(
        "local-filesystem",
        "publish",
        {
            "path": str(target.resolve()),
            "permissions": 0o600,
            "expected_existing_hash": None,
        },
    )
    with pytest.raises(OwnerConflict, match="writing_delivery_target_already_exists"):
        provider.verify_target_current("publish", existing_target)
    assert target.read_bytes() == b"old"

    symlink = real_directory / "symlink.html"
    symlink.symlink_to(target)
    with pytest.raises(OwnerConflict, match="writing_delivery_target_invalid"):
        normalize_writing_delivery_target(
            "local-filesystem",
            "publish",
            {
                "path": str(symlink.absolute()),
                "permissions": 0o600,
                "expected_existing_hash": None,
            },
        )
    assert target.read_bytes() == b"old"


def test_local_publish_is_anchored_to_the_confirmed_parent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = LocalFilesystemWritingDeliveryProvider()
    confirmed_parent = tmp_path / "confirmed-parent"
    confirmed_parent.mkdir(mode=0o700)
    moved_parent = tmp_path / "confirmed-parent-moved"
    attacker_parent = tmp_path / "attacker-parent"
    attacker_parent.mkdir(mode=0o700)
    target = confirmed_parent / "paper.html"
    artifact = b"sensitive accepted artifact"
    normalized_target = normalize_writing_delivery_target(
        "local-filesystem",
        "publish",
        {
            "path": str(target.resolve()),
            "permissions": 0o600,
            "expected_existing_hash": None,
        },
    )
    request = provider.request(
        operation_ref="writing_delivery:" + "4" * 48,
        action="publish",
        target=normalized_target,
        target_binding=provider.verify_target_current(
            "publish", normalized_target
        ),
        artifact=artifact,
        artifact_sha256=hashlib.sha256(artifact).hexdigest(),
    )
    real_open = os.open
    swapped = False

    def swap_parent_before_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        candidate = os.fspath(path)
        if (
            not swapped
            and (
                candidate == str(confirmed_parent)
                or candidate == confirmed_parent.name
                or candidate.startswith(str(confirmed_parent) + os.sep)
            )
        ):
            confirmed_parent.rename(moved_parent)
            confirmed_parent.symlink_to(attacker_parent, target_is_directory=True)
            swapped = True
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(writing_delivery_module.os, "open", swap_parent_before_open)

    with pytest.raises(OwnerConflict, match="writing_delivery_target_stale"):
        provider.execute(request)

    assert swapped is True
    assert not (attacker_parent / target.name).exists()
    assert not (moved_parent / target.name).exists()


def test_local_publish_opens_every_ancestor_without_following_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = LocalFilesystemWritingDeliveryProvider()
    trusted_root = tmp_path / "trusted-root"
    confirmed_parent = trusted_root / "confirmed-parent"
    confirmed_parent.mkdir(parents=True, mode=0o700)
    moved_root = tmp_path / "trusted-root-moved"
    attacker_root = tmp_path / "attacker-root"
    attacker_parent = attacker_root / confirmed_parent.name
    attacker_parent.mkdir(parents=True, mode=0o700)
    target = confirmed_parent / "paper.html"
    artifact = b"ancestor-confined artifact"
    normalized_target = normalize_writing_delivery_target(
        "local-filesystem",
        "publish",
        {
            "path": str(target.resolve()),
            "permissions": 0o600,
            "expected_existing_hash": None,
        },
    )
    request = provider.request(
        operation_ref="writing_delivery:" + "5" * 48,
        action="publish",
        target=normalized_target,
        target_binding=provider.verify_target_current(
            "publish", normalized_target
        ),
        artifact=artifact,
        artifact_sha256=hashlib.sha256(artifact).hexdigest(),
    )
    real_open = os.open
    swapped = False

    def swap_ancestor_before_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        candidate = os.fspath(path)
        if not swapped and candidate in {str(confirmed_parent), trusted_root.name}:
            trusted_root.rename(moved_root)
            trusted_root.symlink_to(attacker_root, target_is_directory=True)
            swapped = True
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(writing_delivery_module.os, "open", swap_ancestor_before_open)

    with pytest.raises(OwnerConflict, match="writing_delivery_target_stale"):
        provider.execute(request)

    assert swapped is True
    assert not (attacker_parent / target.name).exists()
    assert not (moved_root / confirmed_parent.name / target.name).exists()


def test_local_publish_rejects_a_same_name_real_parent_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = LocalFilesystemWritingDeliveryProvider()
    confirmed_parent = tmp_path / "confirmed-real-parent"
    confirmed_parent.mkdir(mode=0o700)
    moved_parent = tmp_path / "confirmed-real-parent-moved"
    target = confirmed_parent / "paper.html"
    normalized_target = normalize_writing_delivery_target(
        "local-filesystem",
        "publish",
        {
            "path": str(target.resolve()),
            "permissions": 0o600,
            "expected_existing_hash": None,
        },
    )
    artifact = b"real-parent-confined artifact"
    request = provider.request(
        operation_ref="writing_delivery:" + "6" * 48,
        action="publish",
        target=normalized_target,
        target_binding=provider.verify_target_current(
            "publish", normalized_target
        ),
        artifact=artifact,
        artifact_sha256=hashlib.sha256(artifact).hexdigest(),
    )
    real_open = os.open
    swapped = False

    def swap_parent_before_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if not swapped and os.fspath(path) == confirmed_parent.name:
            confirmed_parent.rename(moved_parent)
            confirmed_parent.mkdir(mode=0o700)
            swapped = True
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(writing_delivery_module.os, "open", swap_parent_before_open)

    with pytest.raises(OwnerConflict, match="writing_delivery_target_stale"):
        provider.execute(request)

    assert swapped is True
    assert not target.exists()
    assert not (moved_parent / target.name).exists()


def test_local_publish_rejects_a_same_name_real_ancestor_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = LocalFilesystemWritingDeliveryProvider()
    trusted_root = tmp_path / "confirmed-real-root"
    confirmed_parent = trusted_root / "accepted"
    confirmed_parent.mkdir(parents=True, mode=0o700)
    moved_root = tmp_path / "confirmed-real-root-moved"
    target = confirmed_parent / "paper.html"
    normalized_target = normalize_writing_delivery_target(
        "local-filesystem",
        "publish",
        {
            "path": str(target.resolve()),
            "permissions": 0o600,
            "expected_existing_hash": None,
        },
    )
    artifact = b"real-ancestor-confined artifact"
    request = provider.request(
        operation_ref="writing_delivery:" + "7" * 48,
        action="publish",
        target=normalized_target,
        target_binding=provider.verify_target_current(
            "publish", normalized_target
        ),
        artifact=artifact,
        artifact_sha256=hashlib.sha256(artifact).hexdigest(),
    )
    real_open = os.open
    swapped = False

    def swap_ancestor_before_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if not swapped and os.fspath(path) == trusted_root.name:
            trusted_root.rename(moved_root)
            confirmed_parent.mkdir(parents=True, mode=0o700)
            swapped = True
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(
        writing_delivery_module.os,
        "open",
        swap_ancestor_before_open,
    )

    with pytest.raises(OwnerConflict, match="writing_delivery_target_stale"):
        provider.execute(request)

    assert swapped is True
    assert not target.exists()
    assert not (moved_root / "accepted" / target.name).exists()


def test_local_publish_cleans_the_effect_when_parent_drifts_at_the_link_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = LocalFilesystemWritingDeliveryProvider()
    confirmed_parent = tmp_path / "link-window-parent"
    confirmed_parent.mkdir(mode=0o700)
    moved_parent = tmp_path / "link-window-parent-moved"
    target = confirmed_parent / "paper.html"
    normalized_target = normalize_writing_delivery_target(
        "local-filesystem",
        "publish",
        {
            "path": str(target.resolve()),
            "permissions": 0o600,
            "expected_existing_hash": None,
        },
    )
    artifact = b"must not survive a stale canonical parent"
    request = provider.request(
        operation_ref="writing_delivery:" + "8" * 48,
        action="publish",
        target=normalized_target,
        target_binding=provider.verify_target_current(
            "publish", normalized_target
        ),
        artifact=artifact,
        artifact_sha256=hashlib.sha256(artifact).hexdigest(),
    )
    real_link = os.link
    swapped = False

    def swap_parent_before_link(
        src,
        dst,
        *,
        src_dir_fd=None,
        dst_dir_fd=None,
        follow_symlinks=True,
    ):
        nonlocal swapped
        confirmed_parent.rename(moved_parent)
        confirmed_parent.mkdir(mode=0o700)
        swapped = True
        return real_link(
            src,
            dst,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(writing_delivery_module.os, "link", swap_parent_before_link)

    with pytest.raises(OwnerConflict, match="writing_delivery_target_stale"):
        provider.execute(request)

    assert swapped is True
    assert not target.exists()
    assert not (moved_parent / target.name).exists()


def test_local_reconcile_rejects_parent_drift_after_its_confirmed_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = LocalFilesystemWritingDeliveryProvider()
    confirmed_parent = tmp_path / "reconcile-window-parent"
    confirmed_parent.mkdir(mode=0o700)
    moved_parent = tmp_path / "reconcile-window-parent-moved"
    target = confirmed_parent / "paper.html"
    normalized_target = normalize_writing_delivery_target(
        "local-filesystem",
        "publish",
        {
            "path": str(target.resolve()),
            "permissions": 0o600,
            "expected_existing_hash": None,
        },
    )
    artifact = b"same bytes cannot replace the frozen parent binding"
    request = provider.request(
        operation_ref="writing_delivery:" + "9" * 48,
        action="publish",
        target=normalized_target,
        target_binding=provider.verify_target_current(
            "publish", normalized_target
        ),
        artifact=artifact,
        artifact_sha256=hashlib.sha256(artifact).hexdigest(),
    )
    assert provider.execute(request).outcome == "completed"
    real_open = os.open
    swapped = False

    def swap_parent_before_target_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if not swapped and os.fspath(path) == target.name:
            confirmed_parent.rename(moved_parent)
            confirmed_parent.mkdir(mode=0o700)
            target.write_bytes(artifact)
            target.chmod(0o600)
            swapped = True
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(
        writing_delivery_module.os,
        "open",
        swap_parent_before_target_open,
    )

    with pytest.raises(OwnerConflict, match="writing_delivery_target_stale"):
        provider.reconcile(request)

    assert swapped is True
    assert target.read_bytes() == artifact
    assert (moved_parent / target.name).read_bytes() == artifact


class _HumanConfirmationVerifier:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.calls: list[dict[str, object]] = []

    def verify_command_confirmation(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(dict(kwargs))
        return {
            "command_kind": "writing_external_delivery",
            "payload": self.payload,
        }


class _BindingVerifier:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def verify_writing_delivery_binding(self, payload: dict[str, object]) -> None:
        self.calls.append(payload)


class _AckLostOnceProvider(InMemoryWritingDeliveryProvider):
    provider_ref = "local-filesystem"

    def __init__(self) -> None:
        super().__init__()
        self.execute_calls = 0
        self.reconcile_calls = 0
        self._ack_lost = False

    def execute(self, request):
        self.execute_calls += 1
        observation = super().execute(request)
        if not self._ack_lost:
            self._ack_lost = True
            raise WritingDeliveryOutcomeUnknown("provider_ack_lost")
        return observation

    def reconcile(self, request):
        self.reconcile_calls += 1
        return super().reconcile(request)


class _CompletedProvider(InMemoryWritingDeliveryProvider):
    provider_ref = "local-filesystem"

    def __init__(self) -> None:
        super().__init__()
        self.execute_calls = 0

    def execute(self, request):
        self.execute_calls += 1
        return super().execute(request)


class _UnknownReconciliationProvider(_AckLostOnceProvider):
    def __init__(self, *, unknown_reconciliations: int) -> None:
        super().__init__()
        self._unknown_reconciliations = unknown_reconciliations

    def reconcile(self, request):
        self.reconcile_calls += 1
        if self.reconcile_calls <= self._unknown_reconciliations:
            raise WritingDeliveryOutcomeUnknown("provider_reconciliation_ack_lost")
        return InMemoryWritingDeliveryProvider.reconcile(self, request)


class _RecordingInhibitor:
    kind = "test_writing_delivery_inhibitor"

    def __init__(self, *, reject: bool = False) -> None:
        self.reject = reject
        self.active: set[str] = set()
        self.acquire_calls = 0
        self.release_calls = 0

    def acquire(self, *, holder_ref: str, reason: str) -> InhibitorLease:
        del reason
        self.acquire_calls += 1
        if self.reject:
            raise RuntimeProtectionUnavailable(
                "power_inhibitor_acquisition_failed"
            )
        self.active.add(holder_ref)
        return InhibitorLease(
            holder_ref=holder_ref,
            backend=self.kind,
            scope="sleep",
            acquired_at=1.0,
            native_holder_ref="test-native:" + holder_ref,
        )

    def is_confirmed(self, lease: InhibitorLease) -> bool:
        return lease.holder_ref in self.active

    def release(self, lease: InhibitorLease) -> None:
        self.release_calls += 1
        self.active.discard(lease.holder_ref)


class _LoseFinishAck:
    def __init__(self, protection: RuntimeProtection) -> None:
        self._protection = protection

    def acquire(self, identity):
        return self._protection.acquire(identity)

    def finish(self, responsibility_ref, *, boundary, checkpoint_ref=None):
        del responsibility_ref, boundary, checkpoint_ref
        raise RuntimeProtectionUnavailable("runtime_finish_ack_lost")


class _ExplodingProvider(InMemoryWritingDeliveryProvider):
    provider_ref = "local-filesystem"

    def __init__(self, phase: str, secret: str) -> None:
        super().__init__()
        self._phase = phase
        self._secret = secret

    def request(self, **kwargs):
        if self._phase == "request":
            raise RuntimeError(self._secret)
        return super().request(**kwargs)

    def execute(self, request):
        if self._phase == "execute":
            raise RuntimeError(self._secret)
        if self._phase == "reconcile":
            super().execute(request)
            raise WritingDeliveryOutcomeUnknown("provider_ack_lost")
        return super().execute(request)

    def reconcile(self, request):
        if self._phase == "reconcile":
            raise RuntimeError(self._secret)
        return super().reconcile(request)


class _MissingRequestProvider:
    provider_ref = "missing-request"
    production_ready = False
    supported_actions = frozenset({"publish"})

    def verify_target_current(self, action, target):
        return {"action": action, "target": target, "current": True}

    def execute(self, request):
        raise AssertionError(request)

    def reconcile(self, request):
        raise AssertionError(request)


def _seed_writing_run(path: Path) -> None:
    import sqlite3

    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "INSERT INTO ar_writing_runs (run_ref, intent_id, quest_ref, "
            "document_type, intent_json, intent_hash, snapshot_ref, snapshot_json, "
            "snapshot_hash, confirmation_ref, confirmation_hash, status, "
            "failure_code, execution_budget_json, execution_budget_hash, "
            "output_bytes, attempt_ref, attempt_generation, root_session_ref, "
            "native_session_ref, fence_ref, predecessor_version_ref, feedback_json, "
            "feedback_hash, runtime_binding_json, runtime_binding_hash, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, "
            "0, ?, 1, ?, ?, ?, NULL, '[]', ?, ?, ?, 1.0, 1.0)",
            (
                "writing-run:1",
                "writing-intent:1",
                "quest:1",
                "paper",
                "{}",
                _HASH,
                "snapshot:1",
                "{}",
                _HASH,
                "confirmation:1",
                _HASH,
                "completed",
                "{}",
                _HASH,
                "attempt:1",
                "root-session:1",
                "native-session:1",
                "fence:1",
                canonical_hash([]),
                "{}",
                _HASH,
            ),
        )
        connection.commit()


def _admit_test_delivery(
    authority: SQLiteWritingDeliveryRuntime,
    payload: dict[str, object],
    *,
    suffix: str,
):
    return authority.admit(
        payload,
        intent_id=f"writing-intent:{suffix}",
        draft_revision=1,
        draft_hash="9" * 64,
        preview_ref=f"preview:{suffix}",
        preview_hash="a" * 64,
        confirmation=AcceptanceReceipt(
            issuer="human_collaboration",
            kind="command_confirmed",
            receipt_ref=f"hc-confirmation:{suffix}",
            subject_ref=f"writing-intent:{suffix}",
            payload_hash="8" * 64,
        ),
        idempotency_key=f"delivery-admit-{suffix}",
    )


def _protected_delivery_runtime(
    *,
    path: Path,
    payload: dict[str, object],
    provider,
    inhibitor: _RecordingInhibitor,
    runtime_protection=None,
):
    database = Database(path)
    feed = DurableFeed(database)
    protection = runtime_protection or RuntimeProtection(
        database=database,
        feed=feed,
        inhibitor=inhibitor,
        event_logger=RuntimeEventLogger(path.with_suffix(".runtime.jsonl")),
    )
    authority = SQLiteWritingDeliveryRuntime(
        database,
        feed,
        _HumanConfirmationVerifier(payload),
        WritingDeliveryProviderRegistry((provider,)),
        production_mode=False,
        runtime_protection=protection,
    )
    authority.bind_binding_verifier(_BindingVerifier())
    return database, protection, authority


def test_delivery_execute_is_fail_closed_when_power_hold_is_unavailable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "delivery-protection-rejected.sqlite3"
    upgrade_database(path)
    _seed_writing_run(path)
    payload = _payload(tmp_path, nonce="protected-rejected")
    provider = _CompletedProvider()
    inhibitor = _RecordingInhibitor(reject=True)
    database, _protection, authority = _protected_delivery_runtime(
        path=path,
        payload=payload,
        provider=provider,
        inhibitor=inhibitor,
    )
    try:
        operation = _admit_test_delivery(
            authority, payload, suffix="protected-rejected"
        )

        with pytest.raises(
            OwnerConflict, match="power_inhibitor_acquisition_failed"
        ):
            authority.execute_once(operation.operation_ref, artifact=b"rendered")

        current = authority.query_operation(operation.operation_ref)
        assert current is not None
        assert current.status == "executing"
        assert current.attempt_count == 1
        assert current.reconciliation_generation == 0
        assert provider.execute_calls == 0
        with database.read() as connection:
            responsibility = connection.execute(
                text(
                    "SELECT status, effect_kind FROM "
                    "ar_execution_responsibilities WHERE operation_ref = "
                    ":operation_ref"
                ),
                {"operation_ref": operation.provider_operation_ref},
            ).one()
        assert responsibility.status == "waiting"
        assert responsibility.effect_kind == "provider_unit"
    finally:
        database.close()


def test_unknown_reconciliation_generations_stay_held_until_exact_success(
    tmp_path: Path,
) -> None:
    path = tmp_path / "delivery-protection-reconciliation.sqlite3"
    upgrade_database(path)
    _seed_writing_run(path)
    payload = _payload(tmp_path, nonce="protected-reconciliation")
    provider = _UnknownReconciliationProvider(unknown_reconciliations=2)
    inhibitor = _RecordingInhibitor()
    database, _protection, authority = _protected_delivery_runtime(
        path=path,
        payload=payload,
        provider=provider,
        inhibitor=inhibitor,
    )
    try:
        operation = _admit_test_delivery(
            authority, payload, suffix="protected-reconciliation"
        )
        first = authority.execute_once(
            operation.operation_ref, artifact=b"rendered"
        )
        assert first.status == "outcome_unknown"
        assert first.reconciliation_generation == 0

        first_unknown = authority.execute_once(
            operation.operation_ref, artifact=b"rendered"
        )
        second_unknown = authority.execute_once(
            operation.operation_ref, artifact=b"rendered"
        )
        assert first_unknown.status == second_unknown.status == "outcome_unknown"
        assert first_unknown.reconciliation_generation == 1
        assert second_unknown.reconciliation_generation == 2
        assert provider.execute_calls == 1
        assert provider.reconcile_calls == 2
        assert len(inhibitor.active) == 1
        with database.read() as connection:
            unresolved = connection.execute(
                text(
                    "SELECT responsibility_ref, effect_kind, status FROM "
                    "ar_execution_responsibilities WHERE operation_ref = "
                    ":operation_ref ORDER BY created_at, responsibility_ref"
                ),
                {"operation_ref": operation.provider_operation_ref},
            ).all()
            boundary_count = connection.execute(
                text(
                    "SELECT COUNT(*) FROM ar_runtime_boundary_receipts WHERE "
                    "operation_ref = :operation_ref"
                ),
                {"operation_ref": operation.provider_operation_ref},
            ).scalar_one()
        assert len(unresolved) == 3
        assert len({row.responsibility_ref for row in unresolved}) == 3
        assert [row.effect_kind for row in unresolved] == [
            "provider_unit",
            "runtime_reconciliation",
            "runtime_reconciliation",
        ]
        assert {row.status for row in unresolved} == {"active"}
        assert boundary_count == 0

        completed = authority.execute_once(
            operation.operation_ref, artifact=b"rendered"
        )
        assert completed.status == "completed"
        assert completed.reconciliation_generation == 3
        assert provider.execute_calls == 1
        assert provider.reconcile_calls == 3
        assert inhibitor.active == set()
        with database.read() as connection:
            settled = connection.execute(
                text(
                    "SELECT responsibility.effect_kind, responsibility.status, "
                    "receipt.boundary FROM ar_execution_responsibilities AS "
                    "responsibility JOIN ar_runtime_boundary_receipts AS receipt "
                    "ON receipt.responsibility_ref = "
                    "responsibility.responsibility_ref WHERE "
                    "responsibility.operation_ref = :operation_ref ORDER BY "
                    "responsibility.created_at, responsibility.responsibility_ref"
                ),
                {"operation_ref": operation.provider_operation_ref},
            ).all()
        assert len(settled) == 4
        assert {row.status for row in settled} == {"finished"}
        assert [row.boundary for row in settled].count("permanent_fence") == 3
        assert [row.boundary for row in settled].count("terminal") == 1
    finally:
        database.close()


def test_stale_reconciliation_observation_cannot_fence_newer_acquired_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "delivery-protection-generation-race.sqlite3"
    upgrade_database(path)
    _seed_writing_run(path)
    payload = _payload(tmp_path, nonce="protected-generation-race")
    provider = _CompletedProvider()
    inhibitor = _RecordingInhibitor()
    database, _protection, authority = _protected_delivery_runtime(
        path=path,
        payload=payload,
        provider=provider,
        inhibitor=inhibitor,
    )
    try:
        operation = _admit_test_delivery(
            authority, payload, suffix="protected-generation-race"
        )
        request = provider.request(
            operation_ref=operation.operation_ref,
            action=str(payload["action"]),
            target=payload["target"],
            target_binding=payload["target_binding"],
            artifact=b"rendered",
            artifact_sha256=str(payload["renderer_artifact_sha256"]),
        )
        authority.claim(
            operation.operation_ref,
            provider_request_hash=request.request_hash,
        )
        first = authority._begin_reconciliation(
            operation.operation_ref,
            provider_request_hash=request.request_hash,
        )
        first_effect = authority._acquire_runtime_effect(
            first,
            provider_request_hash=request.request_hash,
            reconciling=True,
        )
        second = authority._begin_reconciliation(
            operation.operation_ref,
            provider_request_hash=request.request_hash,
        )
        second_effect = authority._acquire_runtime_effect(
            second,
            provider_request_hash=request.request_hash,
            reconciling=True,
        )
        assert first_effect is not None and second_effect is not None

        # Deterministically reproduce the transaction race: generation one read
        # itself as current immediately before generation two committed its claim.
        original_query = authority.query_operation
        query_count = 0

        def stale_first_query(operation_ref: str):
            nonlocal query_count
            query_count += 1
            if query_count == 1:
                return first
            return original_query(operation_ref)

        monkeypatch.setattr(authority, "query_operation", stale_first_query)
        observation = provider.reconcile(request)

        with pytest.raises(
            OwnerConflict,
            match="writing_delivery_runtime_effect_stale",
        ):
            authority.record_provider_observation(
                operation.operation_ref,
                observation,
                reconciliation=True,
                runtime_effect=first_effect,
            )

        current = original_query(operation.operation_ref)
        assert current is not None
        assert current.status == "executing"
        assert current.reconciliation_generation == 2
        with database.read() as connection:
            responsibilities = connection.execute(
                text(
                    "SELECT responsibility_ref, status, boundary FROM "
                    "ar_execution_responsibilities WHERE operation_ref = "
                    ":operation_ref ORDER BY created_at, responsibility_ref"
                ),
                {"operation_ref": operation.provider_operation_ref},
            ).all()
            boundary_count = connection.execute(
                text(
                    "SELECT COUNT(*) FROM ar_runtime_boundary_receipts WHERE "
                    "operation_ref = :operation_ref"
                ),
                {"operation_ref": operation.provider_operation_ref},
            ).scalar_one()
        assert len(responsibilities) == 2
        assert {row.status for row in responsibilities} == {"active"}
        assert {row.boundary for row in responsibilities} == {None}
        assert boundary_count == 0
        assert len(inhibitor.active) == 1
    finally:
        database.close()


def test_execution_does_not_start_after_reconciliation_claims_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "delivery-protection-late-execution.sqlite3"
    upgrade_database(path)
    _seed_writing_run(path)
    payload = _payload(tmp_path, nonce="protected-late-execution")
    provider = _CompletedProvider()
    inhibitor = _RecordingInhibitor()
    database, _protection, authority = _protected_delivery_runtime(
        path=path,
        payload=payload,
        provider=provider,
        inhibitor=inhibitor,
    )
    try:
        operation = _admit_test_delivery(
            authority, payload, suffix="protected-late-execution"
        )
        original_claim = authority.claim

        def claim_then_lose_generation(
            operation_ref: str,
            *,
            provider_request_hash: str,
        ):
            original_claim(
                operation_ref,
                provider_request_hash=provider_request_hash,
            )
            return authority._begin_reconciliation(
                operation_ref,
                provider_request_hash=provider_request_hash,
            )

        monkeypatch.setattr(authority, "claim", claim_then_lose_generation)

        with pytest.raises(
            OwnerConflict,
            match="writing_delivery_runtime_effect_stale",
        ):
            authority.execute_once(operation.operation_ref, artifact=b"rendered")

        assert provider.execute_calls == 0
        current = authority.query_operation(operation.operation_ref)
        assert current is not None
        assert current.status == "executing"
        assert current.attempt_count == 1
        assert current.reconciliation_generation == 1
        with database.read() as connection:
            responsibility_count = connection.execute(
                text(
                    "SELECT COUNT(*) FROM ar_execution_responsibilities WHERE "
                    "operation_ref = :operation_ref"
                ),
                {"operation_ref": operation.provider_operation_ref},
            ).scalar_one()
        assert responsibility_count == 0
        assert inhibitor.active == set()
    finally:
        database.close()


def test_completed_delivery_recovers_when_runtime_finish_ack_is_lost(
    tmp_path: Path,
) -> None:
    path = tmp_path / "delivery-protection-finish-ack.sqlite3"
    upgrade_database(path)
    _seed_writing_run(path)
    payload = _payload(tmp_path, nonce="protected-finish-ack")
    provider = _CompletedProvider()
    inhibitor = _RecordingInhibitor()
    database = Database(path)
    feed = DurableFeed(database)
    first_protection = RuntimeProtection(
        database=database,
        feed=feed,
        inhibitor=inhibitor,
        event_logger=RuntimeEventLogger(path.with_suffix(".runtime.jsonl")),
    )
    authority = SQLiteWritingDeliveryRuntime(
        database,
        feed,
        _HumanConfirmationVerifier(payload),
        WritingDeliveryProviderRegistry((provider,)),
        production_mode=False,
        runtime_protection=_LoseFinishAck(first_protection),  # type: ignore[arg-type]
    )
    authority.bind_binding_verifier(_BindingVerifier())
    try:
        operation = _admit_test_delivery(
            authority, payload, suffix="protected-finish-ack"
        )
        with pytest.raises(OwnerConflict, match="runtime_finish_ack_lost"):
            authority.execute_once(operation.operation_ref, artifact=b"rendered")

        committed = authority.query_operation(operation.operation_ref)
        assert committed is not None and committed.status == "completed"
        assert provider.execute_calls == 1
        assert len(inhibitor.active) == 1
        with database.read() as connection:
            responsibility = connection.execute(
                text(
                    "SELECT responsibility.status, receipt.boundary FROM "
                    "ar_execution_responsibilities AS responsibility JOIN "
                    "ar_runtime_boundary_receipts AS receipt ON "
                    "receipt.responsibility_ref = responsibility.responsibility_ref "
                    "WHERE responsibility.operation_ref = :operation_ref"
                ),
                {"operation_ref": operation.provider_operation_ref},
            ).one()
        assert responsibility.status == "active"
        assert responsibility.boundary == "terminal"

        restarted = RuntimeProtection(
            database=database,
            feed=feed,
            inhibitor=inhibitor,
            event_logger=RuntimeEventLogger(path.with_suffix(".runtime.jsonl")),
        )
        replay_authority = SQLiteWritingDeliveryRuntime(
            database,
            feed,
            _HumanConfirmationVerifier(payload),
            WritingDeliveryProviderRegistry((provider,)),
            production_mode=False,
            runtime_protection=restarted,
        )
        replay = replay_authority.execute_once(
            operation.operation_ref, artifact=b"rendered"
        )
        assert replay.status == "completed"
        assert provider.execute_calls == 1
        assert inhibitor.active == set()
    finally:
        database.close()


def test_runnable_selector_honors_backoff_exclusions_and_created_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "scheduler-selector.sqlite3"
    upgrade_database(path)
    _seed_writing_run(path)
    clock = [1.0]
    monkeypatch.setattr(
        writing_delivery_runtime_module.time,
        "time",
        lambda: clock[0],
    )
    provider = InMemoryWritingDeliveryProvider()
    provider.provider_ref = "local-filesystem"
    first_payload = _payload(tmp_path, nonce="scheduler-operation-1")
    human = _HumanConfirmationVerifier(first_payload)
    database = Database(path)
    authority = SQLiteWritingDeliveryRuntime(
        database,
        DurableFeed(database),
        human,
        WritingDeliveryProviderRegistry((provider,)),
        production_mode=False,
    )
    authority.bind_binding_verifier(_BindingVerifier())

    def admit(index: int, created_at: float):
        clock[0] = created_at
        payload = _payload(tmp_path, nonce=f"scheduler-operation-{index}")
        human.payload = payload
        confirmation = AcceptanceReceipt(
            issuer="human_collaboration",
            kind="command_confirmed",
            receipt_ref=f"hc-confirmation:scheduler-{index}",
            subject_ref=f"writing-intent:scheduler-{index}",
            payload_hash=f"{index}" * 64,
        )
        return authority.admit(
            payload,
            intent_id=f"writing-intent:scheduler-{index}",
            draft_revision=1,
            draft_hash=f"{index}" * 64,
            preview_ref=f"preview:scheduler-{index}",
            preview_hash=f"{index}" * 64,
            confirmation=confirmation,
            idempotency_key=f"delivery-scheduler-admit-{index}",
        )

    try:
        operations = tuple(admit(index, index * 10.0) for index in range(1, 6))
        completed, partial, unknown, admitted, later = operations
        clock[0] = 60.0
        assert authority.execute_once(
            completed.operation_ref, artifact=b"rendered"
        ).status == "completed"
        clock[0] = 100.0
        authority.record_preflight_failure(
            partial.operation_ref,
            reason_code="asset_custody_unavailable",
        )
        clock[0] = 80.0
        authority.mark_outcome_unknown(
            unknown.operation_ref,
            reason_code="provider_ack_lost",
        )

        def fail_if_operation_is_hydrated(*_args, **_kwargs):
            raise AssertionError("selector hydrated a delivery operation")

        monkeypatch.setattr(
            authority, "query_operations", fail_if_operation_is_hydrated
        )
        monkeypatch.setattr(
            authority, "query_operation", fail_if_operation_is_hydrated
        )

        assert authority.next_runnable_operation_ref(retry_cutoff=70.0) == (
            admitted.operation_ref
        )
        assert authority.next_runnable_operation_ref(retry_cutoff=80.0) == (
            unknown.operation_ref
        )
        assert authority.next_runnable_operation_ref(retry_cutoff=100.0) == (
            partial.operation_ref
        )
        assert authority.next_runnable_operation_ref(
            retry_cutoff=100.0,
            excluded_operation_refs=frozenset({partial.operation_ref}),
        ) == unknown.operation_ref
        assert authority.next_runnable_operation_ref(
            retry_cutoff=100.0,
            excluded_operation_refs=frozenset(
                {
                    partial.operation_ref,
                    unknown.operation_ref,
                    admitted.operation_ref,
                }
            ),
        ) == later.operation_ref
        assert authority.next_runnable_operation_ref(
            retry_cutoff=100.0,
            excluded_operation_refs=frozenset(
                {
                    partial.operation_ref,
                    unknown.operation_ref,
                    admitted.operation_ref,
                    later.operation_ref,
                }
            ),
        ) is None

        with pytest.raises(
            OwnerConflict, match="writing_delivery_operation_ref_invalid"
        ):
            authority.next_runnable_operation_ref(
                retry_cutoff=100.0,
                excluded_operation_refs=frozenset(
                    {"writing_delivery:" + "z" * 48}
                ),
            )
        with pytest.raises(
            OwnerConflict, match="writing_delivery_retry_cutoff_invalid"
        ):
            authority.next_runnable_operation_ref(retry_cutoff=float("nan"))
    finally:
        database.close()


def test_ack_loss_reconciles_same_operation_before_any_second_effect(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.sqlite3"
    upgrade_database(path)
    _seed_writing_run(path)
    payload = _payload(tmp_path)
    human = _HumanConfirmationVerifier(payload)
    provider = _AckLostOnceProvider()
    registry = WritingDeliveryProviderRegistry((provider,))
    database = Database(path)
    authority = SQLiteWritingDeliveryRuntime(
        database,
        DurableFeed(database),
        human,
        registry,
        production_mode=False,
    )
    bindings = _BindingVerifier()
    authority.bind_binding_verifier(bindings)
    confirmation = AcceptanceReceipt(
        issuer="human_collaboration",
        kind="command_confirmed",
        receipt_ref="hc-confirmation:delivery-1",
        subject_ref="writing-intent:delivery-1",
        payload_hash="8" * 64,
    )
    try:
        admitted = authority.admit(
            payload,
            intent_id="writing-intent:delivery-1",
            draft_revision=1,
            draft_hash="9" * 64,
            preview_ref="preview:delivery-1",
            preview_hash="a" * 64,
            confirmation=confirmation,
            idempotency_key="delivery-admit-1",
        )
        assert admitted.status == "admitted"
        assert admitted.operation_ref == payload["operation_ref"]
        assert admitted.operation_receipt.issuer == "agent_runtime"
        assert admitted.provider_observations == ()
        assert bindings.calls == [payload]

        unknown = authority.execute_once(
            admitted.operation_ref,
            artifact=b"rendered",
        )
        assert unknown.status == "outcome_unknown"
        assert unknown.execution_receipt is not None
        assert unknown.provider_observations == ()
        assert provider.execute_calls == 1

        completed = authority.execute_once(
            admitted.operation_ref,
            artifact=b"rendered",
        )
        assert completed.status == "completed"
        assert completed.operation_ref == admitted.operation_ref
        assert completed.reconciliation_receipt is not None
        assert completed.execution_receipt is not None
        assert completed.provider_observations[-1].outcome == "completed"
        assert provider.reconcile_calls == 1
        assert provider.execute_calls == 1

        with pytest.raises(
            OwnerConflict, match="writing_delivery_provider_observation_invalid"
        ):
            authority.record_provider_observation(
                completed.operation_ref,
                WritingDeliveryProviderObservation(
                    observation_ref="provider-observation:secret",
                    provider_ref="local-filesystem",
                    provider_operation_ref=completed.provider_operation_ref,
                    outcome="completed",
                    observed_at=1.0,
                    details={"access_token": "not-safe-to-persist"},
                ),
                reconciliation=True,
            )

        with pytest.raises(
            OwnerConflict, match="writing_delivery_provider_observation_invalid"
        ):
            authority.record_provider_observation(
                completed.operation_ref,
                WritingDeliveryProviderObservation(
                    observation_ref="sk-1234567890abcdefghijklmnop",
                    provider_ref="local-filesystem",
                    provider_operation_ref=completed.provider_operation_ref,
                    outcome="completed",
                    observed_at=1.0,
                    details={"target_hash": "1" * 64},
                ),
                reconciliation=True,
            )

        replay = authority.admit(
            payload,
            intent_id="writing-intent:delivery-1",
            draft_revision=1,
            draft_hash="9" * 64,
            preview_ref="preview:delivery-1",
            preview_hash="a" * 64,
            confirmation=confirmation,
            idempotency_key="delivery-admit-1",
        )
        assert replay == completed
        assert len(human.calls) == 1

        preflight_payload = _payload(tmp_path, nonce="delivery-request-2")
        human.payload = preflight_payload
        preflight_confirmation = AcceptanceReceipt(
            issuer="human_collaboration",
            kind="command_confirmed",
            receipt_ref="hc-confirmation:delivery-2",
            subject_ref="writing-intent:delivery-2",
            payload_hash="b" * 64,
        )
        preflight = authority.admit(
            preflight_payload,
            intent_id="writing-intent:delivery-2",
            draft_revision=1,
            draft_hash="c" * 64,
            preview_ref="preview:delivery-2",
            preview_hash="d" * 64,
            confirmation=preflight_confirmation,
            idempotency_key="delivery-admit-2",
        )
        partial = authority.record_preflight_failure(
            preflight.operation_ref,
            reason_code="asset_custody_unavailable",
        )
        repeated = authority.record_preflight_failure(
            preflight.operation_ref,
            reason_code="asset_custody_unavailable",
        )
        assert repeated == partial
        assert partial.status == "partial"
        assert partial.attempt_count == 0
        assert partial.execution_receipt is not None
        assert partial.provider_observations == ()
        assert provider.execute_calls == 1

        observed_partial = authority.record_provider_observation(
            preflight.operation_ref,
            WritingDeliveryProviderObservation(
                observation_ref="provider-observation:partial-1",
                provider_ref="local-filesystem",
                provider_operation_ref=preflight.provider_operation_ref,
                outcome="partial",
                observed_at=1.0,
                details={"target_hash": "1" * 64},
            ),
            reconciliation=True,
        )
        duplicate_partial = authority.record_provider_observation(
            preflight.operation_ref,
            WritingDeliveryProviderObservation(
                observation_ref="provider-observation:partial-2",
                provider_ref="local-filesystem",
                provider_operation_ref=preflight.provider_operation_ref,
                outcome="partial",
                observed_at=2.0,
                details={"target_hash": "1" * 64},
            ),
            reconciliation=True,
        )
        assert len(observed_partial.provider_observations) == 1
        assert duplicate_partial.provider_observations == (
            observed_partial.provider_observations
        )
        assert duplicate_partial.reconciliation_receipt == (
            observed_partial.reconciliation_receipt
        )
        assert duplicate_partial.updated_at >= observed_partial.updated_at

        secret_payload = _payload(tmp_path, nonce="delivery-request-3")
        human.payload = secret_payload
        secret_confirmation = AcceptanceReceipt(
            issuer="human_collaboration",
            kind="command_confirmed",
            receipt_ref="hc-confirmation:delivery-3",
            subject_ref="writing-intent:delivery-3",
            payload_hash="e" * 64,
        )
        secret_operation = authority.admit(
            secret_payload,
            intent_id="writing-intent:delivery-3",
            draft_revision=1,
            draft_hash="f" * 64,
            preview_ref="preview:delivery-3",
            preview_hash="0" * 64,
            confirmation=secret_confirmation,
            idempotency_key="delivery-admit-3",
        )

        def leak_provider_secret(_request):
            raise ConnectionError(
                "Authorization: Bearer sk-test-secret-value-1234567890"
            )

        provider.execute = leak_provider_secret
        hidden = authority.execute_once(
            secret_operation.operation_ref,
            artifact=b"rendered",
        )
        assert hidden.status == "outcome_unknown"
        assert hidden.failure_code == "provider_connection_failed"
        assert "sk-test-secret" not in canonical_json(hidden.as_public_dict())
    finally:
        database.close()

    persisted = path.read_bytes()
    assert b"sk-1234567890abcdefghijklmnop" not in persisted
    assert b"sk-test-secret-value-1234567890" not in persisted


def test_sandbox_provider_is_never_a_production_capability() -> None:
    provider = InMemoryWritingDeliveryProvider()
    registry = WritingDeliveryProviderRegistry((provider,))

    assert provider.production_ready is False
    with pytest.raises(OwnerConflict, match="writing_delivery_provider_not_production"):
        registry.require(provider.provider_ref, production=True)


def test_provider_registry_rejects_an_adapter_without_request() -> None:
    with pytest.raises(OwnerConflict, match="writing_delivery_provider_invalid"):
        WritingDeliveryProviderRegistry((_MissingRequestProvider(),))


@pytest.mark.parametrize(
    ("phase", "expected_status", "expected_code"),
    (
        ("request", "partial", "provider_request_failed"),
        ("execute", "outcome_unknown", "provider_outcome_unknown"),
        ("reconcile", "outcome_unknown", "provider_outcome_unknown"),
    ),
)
def test_provider_boundary_maps_custom_exceptions_without_persisting_secrets(
    tmp_path: Path,
    phase: str,
    expected_status: str,
    expected_code: str,
) -> None:
    path = tmp_path / f"runtime-{phase}.sqlite3"
    upgrade_database(path)
    _seed_writing_run(path)
    payload = _payload(tmp_path, nonce=f"delivery-{phase}")
    human = _HumanConfirmationVerifier(payload)
    secret = f"Authorization: Bearer sk-{phase}-secret-recipient@example.invalid"
    provider = _ExplodingProvider(phase, secret)
    database = Database(path)
    authority = SQLiteWritingDeliveryRuntime(
        database,
        DurableFeed(database),
        human,
        WritingDeliveryProviderRegistry((provider,)),
        production_mode=False,
    )
    authority.bind_binding_verifier(_BindingVerifier())
    confirmation = AcceptanceReceipt(
        issuer="human_collaboration",
        kind="command_confirmed",
        receipt_ref=f"hc-confirmation:{phase}",
        subject_ref=f"writing-intent:{phase}",
        payload_hash="8" * 64,
    )
    try:
        admitted = authority.admit(
            payload,
            intent_id=f"writing-intent:{phase}",
            draft_revision=1,
            draft_hash="9" * 64,
            preview_ref=f"preview:{phase}",
            preview_hash="a" * 64,
            confirmation=confirmation,
            idempotency_key=f"delivery-admit-{phase}",
        )
        first = authority.execute_once(
            admitted.operation_ref, artifact=b"rendered"
        )
        result = (
            authority.execute_once(admitted.operation_ref, artifact=b"rendered")
            if phase == "reconcile"
            else first
        )

        assert result.status == expected_status
        assert result.failure_code == expected_code
        assert secret not in canonical_json(result.as_public_dict())
    finally:
        database.close()

    assert secret.encode("utf-8") not in path.read_bytes()
