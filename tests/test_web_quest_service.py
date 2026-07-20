"""Product boundary tests for the Web-first quest setup service.

These tests intentionally exercise the transport-independent service: browser
inputs are relative display names plus bytes, while every host path and the
qualification contract remain server capabilities.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import stat
import threading
import time
from pathlib import Path

import pytest

from orchestrator import web_quest_service as service_module
from orchestrator.qualification_profiles import QualificationProfileRegistry
from orchestrator.quest_drafts import QuestDraftRegistry
from orchestrator.quest_registry import QuestRegistry
from orchestrator.quest_runtime_profiles import (
    DEFAULT_PROFILE,
    QuestRuntimeSettings,
    public_options,
)
from orchestrator.web_quest_service import (
    RequestUploadPublication,
    WebQuestConflictError,
    WebQuestNotReadyError,
    WebQuestRetryableError,
    WebQuestService,
    WebQuestServiceError,
)


SYSTEM_ROOT = Path(__file__).resolve().parent.parent


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


class _ProcessStub:
    """Small redacted process boundary; lifecycle itself has separate tests."""

    def __init__(self) -> None:
        self.started: list[tuple[str, str]] = []
        self.terminated: list[tuple[str, str]] = []
        self.closed = False

    @staticmethod
    def _status(quest_id: str, state: str = "inactive") -> dict:
        return {
            "quest_id": quest_id,
            "state": state,
            "active": state == "running",
            "managed_by_web": state == "running",
            "terminable": state == "running",
            "exit_code": None,
            "owner_state": None,
            "heartbeat_age_s": None,
            "log_ref": "state/web-owner.log",
        }

    def status(self, quest_id: str) -> dict:
        return self._status(quest_id)

    def start(self, quest_id: str, key: str) -> dict:
        self.started.append((quest_id, key))
        return self._status(quest_id, "running")

    def terminate(self, quest_id: str, key: str) -> dict:
        self.terminated.append((quest_id, key))
        return self._status(quest_id)

    def close(self) -> None:
        self.closed = True


def _service(tmp_path: Path) -> tuple[
        WebQuestService, QuestRegistry, QuestDraftRegistry, _ProcessStub]:
    registry = QuestRegistry(tmp_path / "product", SYSTEM_ROOT)
    drafts = QuestDraftRegistry(registry.state_dir / "quest-drafts")
    profiles = QualificationProfileRegistry(tmp_path / "no-profiles")
    processes = _ProcessStub()
    service = WebQuestService(
        registry=registry, drafts=drafts, profiles=profiles,
        processes=processes,  # type: ignore[arg-type]
    )
    return service, registry, drafts, processes


def _create_draft(
        drafts: QuestDraftRegistry, *, quest_id: str = "web-research",
        template_id: str = "toy-gauss-smoke", key: str = "1" * 32,
        runtime_profile: dict | None = None) -> dict:
    spec = {
        "quest_id": quest_id,
        "title": "Browser created research",
        "template_id": template_id,
    }
    if runtime_profile is not None:
        spec["runtime_profile"] = runtime_profile
    return drafts.create(spec, key)


def _upload(
        registry: QuestDraftRegistry, draft_id: str, relative: str,
        payload: bytes) -> None:
    assert registry.begin_file(draft_id, relative, len(payload))["path"] == relative
    registry.append_chunk(
        draft_id, relative, 0, payload, _digest(payload))
    completed = registry.finalize_file(draft_id, relative, _digest(payload))
    assert completed["status"] == "complete"


def _all_strings(value):  # noqa: ANN001 - recursive JSON projection helper
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _all_strings(key)
            yield from _all_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _all_strings(item)


def test_browser_draft_upload_preflight_publish_is_ready_and_path_redacted(tmp_path):
    service, registry, drafts, processes = _service(tmp_path)
    draft = _create_draft(drafts)
    payload = b"managed EEG candidate bytes\x00\x01"
    _upload(drafts, draft["draft_id"], "SEED/session-01/subject-01.mat", payload)

    setup = service.setup_public()
    assert setup["product_flow"] == "web-only-after-deployment"
    templates = {item["template_id"]: item for item in setup["templates"]}
    assert set(templates) == {"eeg-local-lodo", "toy-gauss-smoke"}
    assert templates["eeg-local-lodo"]["category"] == "真实研究"
    assert templates["toy-gauss-smoke"]["category"] == "系统自检"
    assert templates["eeg-local-lodo"]["display_title"].startswith("真实研究｜")
    assert templates["toy-gauss-smoke"]["display_title"].startswith("系统自检｜")
    assert all(item["summary"] for item in templates.values())
    assert setup["qualification_profiles"] == []
    runtime_options = setup["runtime_profile_options"]
    assert runtime_options["default_profile"] == DEFAULT_PROFILE
    assert [row["id"] for row in runtime_options["compute_profiles"]] == [
        "local-gpu", "local-cpu"]
    assert [row["id"] for row in runtime_options["review_intensities"]] == [
        "once", "off"]
    assert "allowed_device_indices" not in json.dumps(runtime_options)
    assert setup["upload"]["browser_host_paths_accepted"] is True
    assert setup["upload"]["local_directory_attachment"] is True
    assert "data_contract" not in setup
    preflight = service.preflight(draft["draft_id"])
    assert preflight["research_input_status"] == "preflighted"
    assert "scientific_qualification_status" not in preflight
    assert "t1_requirements" not in preflight
    assert "qualification firewall" not in json.dumps(
        preflight, ensure_ascii=False)

    published = service.publish(
        draft["draft_id"], start=False, idempotency_key="2" * 32)
    assert published["quest"]["quest_id"] == "web-research"
    assert published["quest"]["qualification"] is None
    assert published["quest"]["runtime_profile"]["profile"] == DEFAULT_PROFILE
    assert published["quest"]["runtime_profile"]["revision"] == 1
    assert published["setup"]["ready"] is True
    assert published["setup"]["corpus"] == {
        "file_count": 1,
        "total_bytes": len(payload),
        "manifest_sha256": published["setup"]["corpus"]["manifest_sha256"],
    }
    assert published["runtime"]["state"] == "inactive"
    assert processes.started == []

    quest = registry.get("web-research")
    assert "runtime_profile" not in json.loads(
        (quest.work_root / "quest.json").read_text(encoding="utf-8"))
    corpus = quest.work_root / "input" / "corpus"
    stored = corpus / "SEED" / "session-01" / "subject-01.mat"
    assert stored.read_bytes() == payload
    assert stat.S_IMODE(stored.stat().st_mode) == 0o400
    assert stat.S_IMODE(corpus.stat().st_mode) == 0o500
    assert not (drafts.drafts_dir / draft["draft_id"]).exists()

    # Everything reachable by a browser is a redacted projection.  In
    # particular neither the product root nor qualification authority appears.
    browser_values = [setup, preflight, published, service.ready("web-research")]
    serialized = json.dumps(browser_values, ensure_ascii=False, sort_keys=True)
    assert str(tmp_path) not in serialized
    assert str(quest.work_root) not in serialized
    assert "/qualification/" not in serialized
    assert "sealed_truth" not in serialized
    assert "research_uid" not in serialized
    assert "evaluator_uid" not in serialized
    assert not any(os.path.isabs(item) for value in browser_values
                   for item in _all_strings(value))


def test_setup_and_publish_enforce_trusted_gpu_candidate_subset(tmp_path):
    service, registry, drafts, processes = _service(tmp_path)
    trusted = [0, 2, 6]
    processes.runtime_profile_options = lambda: public_options(  # type: ignore[attr-defined]
        allowed_gpu_indices=trusted, requested_gpu_count=2)

    options = service.setup_public()["runtime_profile_options"]
    assert options["version"] == 2
    assert options["gpu_devices"] == [
        {"index": index, "label": f"GPU {index}"} for index in trusted
    ]
    assert options["gpu_selection"] == {"requested_count": 2}
    assert options["default_profile"]["gpu_device_indices"] == trusted
    assert "allowed_device_indices" not in json.dumps(options)

    selected = {
        "version": 2,
        "compute_profile_id": "local-gpu",
        "review_intensity": "once",
        "gpu_device_indices": [0, 6],
    }
    assert service.validate_runtime_profile(selected) == selected
    draft = _create_draft(
        drafts, quest_id="gpu-subset", runtime_profile=selected)
    published = service.publish(
        draft["draft_id"], start=False, idempotency_key="2" * 32)
    assert published["quest"]["runtime_profile"]["profile"] == selected
    assert QuestRuntimeSettings(
        registry.get("gpu-subset").work_root, "gpu-subset"
    ).current()["profile"] == selected

    outside = {**selected, "gpu_device_indices": [0, 7]}
    insufficient = {**selected, "gpu_device_indices": [2]}
    for rejected in (outside, insufficient):
        with pytest.raises(
                ValueError,
                match="GPU 候选必须来自服务端允许列表|不少于任务请求数量"):
            service.validate_runtime_profile(rejected)

    # A caller that bypasses the HTTP precheck still cannot publish an
    # out-of-policy draft into a quest/runtime ledger.
    rejected_draft = _create_draft(
        drafts, quest_id="gpu-outside", key="3" * 32,
        runtime_profile=outside)
    with pytest.raises(ValueError, match="GPU 候选必须来自服务端允许列表"):
        service.publish(
            rejected_draft["draft_id"], start=False,
            idempotency_key="4" * 32)
    with pytest.raises(KeyError):
        registry.get("gpu-outside")


def test_service_v3_exact_gpu_selection_uses_current_detected_catalog(tmp_path):
    service, registry, drafts, processes = _service(tmp_path)
    detected = [
        {"index": 0, "model": "NVIDIA A100", "memory_bytes": 80 * 1024 ** 3},
        {"index": 2, "model": "NVIDIA H100", "memory_bytes": 96 * 1024 ** 3},
        {"index": 6, "model": "NVIDIA L40S", "memory_bytes": 48 * 1024 ** 3},
    ]

    def options():
        return public_options(
            allowed_gpu_indices=[row["index"] for row in detected],
            requested_gpu_count=2,
            exact_multi_gpu=True,
            gpu_device_labels=detected)

    processes.runtime_profile_options = options  # type: ignore[attr-defined]
    processes.runtime_profile_legacy_gpu_count = lambda: 2  # type: ignore[attr-defined]
    setup = service.setup_public()["runtime_profile_options"]
    assert setup["version"] == 3
    assert setup["default_profile"] == {
        "version": 3,
        "compute_profile_id": "local-gpu",
        "review_intensity": "once",
        "gpu_device_indices": [0, 2, 6],
    }
    assert setup["gpu_selection"] == {
        "mode": "exact", "default_count": 3,
        "min_count": 1, "max_count": 3,
    }

    exact_one = {
        "version": 3,
        "compute_profile_id": "local-gpu",
        "review_intensity": "once",
        "gpu_device_indices": [2],
    }
    assert service.validate_runtime_profile(exact_one) == exact_one
    draft = _create_draft(
        drafts, quest_id="gpu-exact-one", runtime_profile=exact_one)
    published = service.publish(
        draft["draft_id"], start=False, idempotency_key="5" * 32)
    assert published["quest"]["runtime_profile"]["profile"] == exact_one
    assert QuestRuntimeSettings(
        registry.get("gpu-exact-one").work_root, "gpu-exact-one"
    ).current()["profile"] == exact_one

    # v2 remains a candidate-pool contract with the private base count even
    # though the same current setup advertises v3 exact semantics.
    v2_pool = {
        "version": 2,
        "compute_profile_id": "local-gpu",
        "review_intensity": "once",
        "gpu_device_indices": [0, 6],
    }
    assert service.validate_runtime_profile(v2_pool) == v2_pool
    with pytest.raises(ValueError, match="不少于任务请求数量"):
        service.validate_runtime_profile({
            **v2_pool, "gpu_device_indices": [0],
        })

    with pytest.raises(ValueError, match="exact GPU 选择"):
        service.validate_runtime_profile({
            **exact_one, "gpu_device_indices": [7],
        })

    # Validation is against a fresh detected catalog, not a stale setup copy.
    detected[:] = detected[:1]
    with pytest.raises(ValueError, match="exact GPU 选择"):
        service.validate_runtime_profile(exact_one)


def test_publish_replay_and_global_operation_binding_are_idempotent(tmp_path):
    service, _registry, drafts, processes = _service(tmp_path)
    draft = _create_draft(drafts, quest_id="idempotent")
    _upload(drafts, draft["draft_id"], "notes/input.txt", b"one input")
    key = "3" * 32

    first = service.publish(draft["draft_id"], start=False, idempotency_key=key)
    replay = service.publish(draft["draft_id"], start=False, idempotency_key=key)
    assert replay == first
    assert processes.started == []

    with pytest.raises(WebQuestConflictError, match="不同 Web 操作"):
        service.publish(draft["draft_id"], start=True, idempotency_key=key)
    with pytest.raises(WebQuestConflictError, match="不同 Web 操作"):
        service.bind_operation(key, "/api/quest-control", {
            "quest_id": "idempotent", "action": "start",
        })


def test_legacy_draft_operation_replay_accepts_only_explicit_default(tmp_path):
    service, _registry, _drafts, _processes = _service(tmp_path)
    key = "f" * 32
    legacy = {
        "quest_id": "legacy-operation", "title": "Legacy",
        "template_id": "toy-gauss-smoke",
    }
    service.bind_operation(key, "/api/quest-drafts", legacy)
    service.bind_operation(key, "/api/quest-drafts", {
        **legacy, "runtime_profile": DEFAULT_PROFILE,
    })
    with pytest.raises(WebQuestConflictError, match="不同 Web 操作"):
        service.bind_operation(key, "/api/quest-drafts", {
            **legacy,
            "runtime_profile": {
                "version": 1, "compute_profile_id": "local-cpu",
                "review_intensity": "once",
            },
        })


def test_finalize_journal_recovers_after_files_move_before_ready_receipt(
        tmp_path, monkeypatch):
    service, registry, drafts, processes = _service(tmp_path)
    draft = _create_draft(drafts, quest_id="crash-recovery")
    payload = b"must survive a publication crash"
    _upload(drafts, draft["draft_id"], "folder/data.bin", payload)
    key = "4" * 32

    real_freeze = service_module._freeze_tree
    calls = 0

    def fail_once(root: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("simulated crash after rename")
        real_freeze(root)

    monkeypatch.setattr(service_module, "_freeze_tree", fail_once)
    with pytest.raises(RuntimeError, match="simulated crash"):
        service.publish(draft["draft_id"], start=False, idempotency_key=key)

    quest = registry.get("crash-recovery")
    assert (quest.work_root / "input" / "corpus" / "folder" / "data.bin").read_bytes() == payload
    assert service.ready("crash-recovery")["ready"] is False
    assert service.runtime_profile("crash-recovery")["revision"] == 1

    # Model an upgrade from a v1 journal that predates runtime_profile.  Its
    # missing field has exactly the documented default meaning.
    journal_path = service.finalize_journals / f"{draft['draft_id']}.json"
    legacy_journal = json.loads(journal_path.read_text(encoding="utf-8"))
    legacy_journal["version"] = 1
    legacy_journal.pop("runtime_profile")
    journal_path.write_bytes(service_module._canonical(legacy_journal))

    # Reconstruct the coordinator to model a Web server restart.  Only its
    # durable journal and already-published quest are used for recovery.
    restarted = WebQuestService(
        registry=registry, drafts=QuestDraftRegistry(drafts.root),
        profiles=QualificationProfileRegistry(tmp_path / "no-profiles"),
        processes=processes,  # type: ignore[arg-type]
    )
    recovered = restarted.publish(
        draft["draft_id"], start=False, idempotency_key=key)
    assert recovered["setup"]["ready"] is True
    assert stat.S_IMODE(
        (quest.work_root / "input" / "corpus" / "folder" / "data.bin").stat().st_mode
    ) == 0o400
    assert not (drafts.drafts_dir / draft["draft_id"]).exists()


def test_publish_and_inactive_updates_use_append_only_runtime_profile(tmp_path):
    service, registry, drafts, _processes = _service(tmp_path)
    cpu_off = {
        "version": 1, "compute_profile_id": "local-cpu",
        "review_intensity": "off",
    }
    draft = _create_draft(
        drafts, quest_id="runtime-choice", runtime_profile=cpu_off)
    published = service.publish(
        draft["draft_id"], start=False, idempotency_key="2" * 32)
    assert published["quest"]["runtime_profile"]["profile"] == cpu_off
    quest = registry.get("runtime-choice")
    ledger = QuestRuntimeSettings(quest.work_root, quest.quest_id)
    assert ledger.history()[0]["operation"] == "initialize"
    journal = json.loads((
        service.finalize_journals / f"{draft['draft_id']}.json"
    ).read_text(encoding="utf-8"))
    assert journal["version"] == 2
    assert journal["runtime_profile"] == cpu_off

    updated = service.update_runtime_profile(
        "runtime-choice", DEFAULT_PROFILE, "3" * 32)
    assert updated["runtime_profile"]["revision"] == 2
    assert updated["runtime_profile"]["profile"] == DEFAULT_PROFILE
    assert updated["restart_pending"] is False
    assert updated["apply_boundary"] == "cycle"
    assert set(updated) == {
        "runtime_profile", "runtime", "restart_pending", "apply_boundary",
    }
    assert service.ready("runtime-choice")["runtime_profile"] == (
        updated["runtime_profile"])
    assert service.update_runtime_profile(
        "runtime-choice", DEFAULT_PROFILE, "3" * 32
    )["runtime_profile"] == updated["runtime_profile"]
    same_value_new_key = service.update_runtime_profile(
        "runtime-choice", DEFAULT_PROFILE, "8" * 32)
    assert same_value_new_key["runtime_profile"] == updated["runtime_profile"]
    assert same_value_new_key["restart_pending"] is False
    assert len(ledger.history()) == 2
    replayed_publish = service.publish(
        draft["draft_id"], start=False, idempotency_key="2" * 32)
    assert replayed_publish["quest"]["runtime_profile"] == (
        updated["runtime_profile"])


def test_runtime_update_replay_of_historical_noop_never_reverts_current(tmp_path):
    service, registry, drafts, _processes = _service(tmp_path)
    draft = _create_draft(drafts, quest_id="historical-noop")
    published = service.publish(
        draft["draft_id"], start=False, idempotency_key="1" * 32)
    original = published["quest"]["runtime_profile"]
    noop = service.update_runtime_profile(
        "historical-noop", DEFAULT_PROFILE, "a" * 32)
    assert noop["runtime_profile"] == original

    changed = service.update_runtime_profile(
        "historical-noop", {
            "version": 1, "compute_profile_id": "local-cpu",
            "review_intensity": "off",
        }, "b" * 32)
    replay = service.update_runtime_profile(
        "historical-noop", DEFAULT_PROFILE, "a" * 32)
    assert replay["runtime_profile"] == original
    assert replay["restart_pending"] is False
    assert service.runtime_profile("historical-noop") == changed["runtime_profile"]
    ledger = QuestRuntimeSettings(
        registry.get("historical-noop").work_root, "historical-noop")
    assert len(ledger.history()) == 2


def test_committed_update_retries_schedule_with_same_key_after_first_failure(
        tmp_path):
    service, registry, drafts, _processes = _service(tmp_path)
    draft = _create_draft(drafts, quest_id="schedule-retry")
    service.publish(draft["draft_id"], start=False, idempotency_key="1" * 32)

    class FailOnceManaged(_ProcessStub):
        def __init__(self):
            super().__init__()
            self.schedule_calls = []
            self.failed_once = False

        def status(self, quest_id: str) -> dict:
            if self.failed_once:
                return self._status(quest_id)
            value = self._status(quest_id, "running")
            value["applied_runtime_profile_revision"] = 1
            value["runtime_profile_restart_pending"] = False
            return value

        def schedule_runtime_profile_restart(self, quest_id, idempotency_key):
            self.schedule_calls.append((quest_id, idempotency_key))
            if len(self.schedule_calls) == 1:
                self.failed_once = True
                raise RuntimeError("injected schedule failure")
            value = self.status(quest_id)
            value["runtime_profile_restart"] = "scheduled"
            value["runtime_profile_restart_pending"] = True
            return value

    processes = FailOnceManaged()
    service.processes = processes
    profile = {
        "version": 1, "compute_profile_id": "local-cpu",
        "review_intensity": "once",
    }
    key = "c" * 32
    with pytest.raises(WebQuestServiceError, match="已保存.*同一 key"):
        service.update_runtime_profile("schedule-retry", profile, key)
    ledger = QuestRuntimeSettings(
        registry.get("schedule-retry").work_root, "schedule-retry")
    committed = ledger.current()
    assert committed["revision"] == 2 and committed["profile"] == profile
    pending = ledger.runtime_update_operation(profile, key)
    assert pending is not None and pending["status"] == "accepted"

    recovered = service.update_runtime_profile("schedule-retry", profile, key)
    assert recovered["runtime_profile"] == committed
    assert recovered["restart_pending"] is True
    assert recovered["runtime"]["runtime_profile_restart"] == "scheduled"
    assert len(ledger.history()) == 2
    assert ledger.runtime_update_operation(profile, key)["status"] == "accepted"
    assert processes.schedule_calls == [
        ("schedule-retry", key), ("schedule-retry", key)]


def test_service_recovers_ledger_to_receipt_crash_gap_without_new_revision(tmp_path):
    service, registry, drafts, _processes = _service(tmp_path)
    draft = _create_draft(drafts, quest_id="receipt-gap")
    service.publish(draft["draft_id"], start=False, idempotency_key="1" * 32)
    profile = {
        "version": 1, "compute_profile_id": "local-cpu",
        "review_intensity": "off",
    }
    key = "d" * 32
    service.bind_operation(key, "/api/quest-runtime-profile", {
        "quest_id": "receipt-gap", "runtime_profile": profile,
    })
    ledger = QuestRuntimeSettings(
        registry.get("receipt-gap").work_root, "receipt-gap")
    committed = ledger.update(profile, key)
    recovered = service.update_runtime_profile("receipt-gap", profile, key)
    assert recovered["runtime_profile"] == committed
    assert recovered["restart_pending"] is False
    assert len(ledger.history()) == 2
    assert ledger.runtime_update_operation(profile, key)["status"] == "not-required"


def test_pending_inactive_stale_binding_is_accepted_before_recovery_spawn(tmp_path):
    service, registry, drafts, _processes = _service(tmp_path)
    draft = _create_draft(drafts, quest_id="pending-bound-recovery")
    published = service.publish(
        draft["draft_id"], start=False, idempotency_key="1" * 32)
    old = published["quest"]["runtime_profile"]
    settings = QuestRuntimeSettings(
        registry.get("pending-bound-recovery").work_root,
        "pending-bound-recovery")
    settings.bind_cycle_profile(old)
    profile = {
        "version": 1, "compute_profile_id": "local-cpu",
        "review_intensity": "off",
    }
    key = "7" * 32
    pending = settings.begin_runtime_update(profile, key)
    assert pending["status"] == "pending"

    class ReconstructedRecovery(_ProcessStub):
        def __init__(self):
            super().__init__()
            self.spawned = []

        def schedule_runtime_profile_restart(self, quest_id, idempotency_key):
            operation = settings.runtime_update_operation_by_key(idempotency_key)
            assert operation is not None and operation["status"] == "accepted"
            self.spawned.append((quest_id, idempotency_key))
            value = self._status(quest_id, "running")
            value["runtime_profile_restart"] = "scheduled"
            value["runtime_profile_restart_pending"] = True
            return value

    processes = ReconstructedRecovery()
    service.processes = processes
    recovered = service.update_runtime_profile(
        "pending-bound-recovery", profile, key)
    assert recovered["restart_pending"] is True
    assert processes.spawned == [("pending-bound-recovery", key)]
    assert settings.runtime_update_operation_by_key(key)["status"] == "accepted"


def test_publish_initialization_serializes_concurrent_runtime_update(
        tmp_path, monkeypatch):
    service, _registry, drafts, _processes = _service(tmp_path)
    draft = _create_draft(drafts, quest_id="publish-update-race")
    initialize_entered = threading.Event()
    release_initialize = threading.Event()
    original_initialize = QuestRuntimeSettings.initialize

    def blocked_initialize(settings, profile, idempotency_key):
        initialize_entered.set()
        assert release_initialize.wait(timeout=5)
        return original_initialize(settings, profile, idempotency_key)

    monkeypatch.setattr(
        QuestRuntimeSettings, "initialize", blocked_initialize)
    results = {}
    errors = []

    def publish():
        try:
            results["publish"] = service.publish(
                draft["draft_id"], start=False,
                idempotency_key="1" * 32)
        except BaseException as error:  # surfaced in the parent test thread
            errors.append(error)

    def update():
        try:
            results["update"] = service.update_runtime_profile(
                "publish-update-race", {
                    "version": 1, "compute_profile_id": "local-cpu",
                    "review_intensity": "once",
                }, "2" * 32)
        except BaseException as error:  # surfaced in the parent test thread
            errors.append(error)

    publisher = threading.Thread(target=publish)
    publisher.start()
    assert initialize_entered.wait(timeout=5)
    updater = threading.Thread(target=update)
    updater.start()
    time.sleep(0.05)
    assert updater.is_alive()
    release_initialize.set()
    publisher.join(timeout=10)
    updater.join(timeout=10)
    assert not publisher.is_alive() and not updater.is_alive()
    assert errors == []
    assert results["publish"]["quest"]["runtime_profile"]["revision"] == 1
    assert results["update"]["runtime_profile"]["revision"] == 2
    assert service.runtime_profile("publish-update-race") == (
        results["update"]["runtime_profile"])


def test_active_profile_update_requires_managed_restart_authority(tmp_path):
    service, registry, drafts, _processes = _service(tmp_path)
    draft = _create_draft(drafts, quest_id="active-profile")
    service.publish(draft["draft_id"], start=False, idempotency_key="4" * 32)
    before = service.runtime_profile("active-profile")

    class ActiveWithoutScheduler(_ProcessStub):
        def status(self, quest_id: str) -> dict:
            value = self._status(quest_id, "running")
            value["managed_by_web"] = True
            return value

    service.processes = ActiveWithoutScheduler()
    no_op = service.update_runtime_profile(
        "active-profile", DEFAULT_PROFILE, "a" * 32)
    assert no_op["runtime_profile"] == before
    assert no_op["restart_pending"] is False
    assert no_op["runtime"]["active"] is True
    with pytest.raises(WebQuestConflictError, match="调度能力"):
        service.update_runtime_profile(
            "active-profile", {
                "version": 1, "compute_profile_id": "local-cpu",
                "review_intensity": "once",
            }, "5" * 32)
    assert service.runtime_profile("active-profile") == before

    class ManagedActive(ActiveWithoutScheduler):
        def __init__(self):
            super().__init__()
            self.scheduled = []

        def schedule_runtime_profile_restart(self, quest_id, idempotency_key):
            self.scheduled.append((quest_id, idempotency_key))
            return {
                "runtime_profile_restart": "scheduled",
                "runtime_profile_restart_pending": True,
            }

    managed = ManagedActive()
    service.processes = managed
    result = service.update_runtime_profile(
        "active-profile", {
            "version": 1, "compute_profile_id": "local-cpu",
            "review_intensity": "once",
        }, "6" * 32)
    assert result["restart_pending"] is True
    assert result["runtime"]["runtime_profile_restart"] == "scheduled"
    assert result["runtime"]["runtime_profile_restart_pending"] is True
    assert result["apply_boundary"] == "cycle"
    assert set(result) == {
        "runtime_profile", "runtime", "restart_pending", "apply_boundary",
    }
    assert managed.scheduled == [("active-profile", "6" * 32)]

    class ExternalActive(ActiveWithoutScheduler):
        def status(self, quest_id: str) -> dict:
            value = super().status(quest_id)
            value["managed_by_web"] = False
            return value

    service.processes = ExternalActive()
    revision = service.runtime_profile("active-profile")["revision"]
    with pytest.raises(WebQuestConflictError, match="外部 owner"):
        service.update_runtime_profile(
            "active-profile", DEFAULT_PROFILE, "7" * 32)
    assert service.runtime_profile("active-profile")["revision"] == revision


def test_post_commit_status_reread_schedules_owner_started_in_update_race(tmp_path):
    service, _registry, drafts, _processes = _service(tmp_path)
    draft = _create_draft(drafts, quest_id="start-race")
    service.publish(draft["draft_id"], start=False, idempotency_key="1" * 32)

    class StartsAfterCommit(_ProcessStub):
        def __init__(self):
            super().__init__()
            self.status_calls = 0
            self.scheduled = []

        def status(self, quest_id: str) -> dict:
            self.status_calls += 1
            if self.status_calls == 1:
                return self._status(quest_id)
            value = self._status(quest_id, "running")
            value["applied_runtime_profile_revision"] = 1
            value["runtime_profile_restart_pending"] = False
            return value

        def schedule_runtime_profile_restart(self, quest_id, idempotency_key):
            self.scheduled.append((quest_id, idempotency_key))
            value = self.status(quest_id)
            value["runtime_profile_restart"] = "scheduled"
            value["runtime_profile_restart_pending"] = True
            return value

    processes = StartsAfterCommit()
    service.processes = processes
    key = "e" * 32
    result = service.update_runtime_profile(
        "start-race", {
            "version": 1, "compute_profile_id": "local-cpu",
            "review_intensity": "once",
        }, key)
    assert result["restart_pending"] is True
    assert processes.scheduled == [("start-race", key)]


def test_post_commit_owner_with_latest_revision_needs_no_scheduler(tmp_path):
    service, _registry, drafts, _processes = _service(tmp_path)
    draft = _create_draft(drafts, quest_id="latest-start-race")
    service.publish(draft["draft_id"], start=False, idempotency_key="1" * 32)

    class CapturesLatestAfterCommit(_ProcessStub):
        def __init__(self):
            super().__init__()
            self.status_calls = 0

        def status(self, quest_id: str) -> dict:
            self.status_calls += 1
            if self.status_calls == 1:
                return self._status(quest_id)
            value = self._status(quest_id, "running")
            value["applied_runtime_profile_revision"] = 2
            value["runtime_profile_restart_pending"] = False
            return value

    processes = CapturesLatestAfterCommit()
    service.processes = processes
    result = service.update_runtime_profile(
        "latest-start-race", {
            "version": 1, "compute_profile_id": "local-cpu",
            "review_intensity": "once",
        }, "f" * 32)
    assert result["restart_pending"] is False
    assert result["runtime"]["applied_runtime_profile_revision"] == 2


def test_external_owner_winning_post_commit_race_reports_saved_and_recovers(tmp_path):
    service, registry, drafts, _processes = _service(tmp_path)
    draft = _create_draft(drafts, quest_id="external-race")
    service.publish(draft["draft_id"], start=False, idempotency_key="1" * 32)

    class ExternalAfterCommit(_ProcessStub):
        def __init__(self):
            super().__init__()
            self.status_calls = 0

        def status(self, quest_id: str) -> dict:
            self.status_calls += 1
            if self.status_calls == 1:
                return self._status(quest_id)
            value = self._status(quest_id, "running")
            value["managed_by_web"] = False
            return value

    profile = {
        "version": 1, "compute_profile_id": "local-cpu",
        "review_intensity": "off",
    }
    key = "9" * 32
    service.processes = ExternalAfterCommit()
    with pytest.raises(WebQuestRetryableError, match="已保存.*外部 owner"):
        service.update_runtime_profile("external-race", profile, key)
    ledger = QuestRuntimeSettings(
        registry.get("external-race").work_root, "external-race")
    assert ledger.current()["profile"] == profile
    assert ledger.runtime_update_operation(profile, key)["status"] == "pending"

    class RecoverManaged(_ProcessStub):
        def __init__(self):
            super().__init__()
            self.scheduled = 0

        def status(self, quest_id: str) -> dict:
            value = self._status(quest_id, "running")
            value["applied_runtime_profile_revision"] = 1
            value["runtime_profile_restart_pending"] = False
            return value

        def schedule_runtime_profile_restart(self, quest_id, idempotency_key):
            self.scheduled += 1
            value = self.status(quest_id)
            value["runtime_profile_restart"] = "scheduled"
            value["runtime_profile_restart_pending"] = True
            return value

    managed = RecoverManaged()
    service.processes = managed
    assert service.update_runtime_profile(
        "external-race", profile, key)["restart_pending"] is True
    assert managed.scheduled == 1


def test_t1_never_accepts_browser_contract_and_missing_internal_profile_fails_closed(
        tmp_path):
    service, registry, drafts, _processes = _service(tmp_path)
    with pytest.raises(WebQuestNotReadyError, match="默认 runtime profile"):
        service._runtime_profile_for_spec({
            "template_id": "t1-eeg-universal",
            "runtime_profile": {
                "version": 1, "compute_profile_id": "local-cpu",
                "review_intensity": "once",
            },
        })
    browser_contract = {
        "task": "T1",
        "research_uid": os.geteuid(),
        "sealed_truth": {"path": "/browser/chosen/truth.json"},
    }
    with pytest.raises(ValueError, match="字段闭包"):
        drafts.create({
            "quest_id": "browser-contract",
            "title": "Must reject contract authority",
            "template_id": "t1-eeg-universal",
            "qualification_contract": browser_contract,
        }, "5" * 32)

    draft = _create_draft(
        drafts, quest_id="t1-without-profile",
        template_id="t1-eeg-universal", key="6" * 32)
    _upload(drafts, draft["draft_id"], "DREAMER/DREAMER.mat", b"candidate only")
    t1_preflight = service.preflight(draft["draft_id"])
    assert t1_preflight["scientific_qualification_status"] == "not_assessed"
    assert t1_preflight["t1_requirements"]["task"] == "T1"
    assert "research_input_status" not in t1_preflight
    with pytest.raises(WebQuestNotReadyError, match="安全数据准备服务"):
        service.publish(
            draft["draft_id"], start=False, idempotency_key="7" * 32)
    with pytest.raises(KeyError):
        registry.get("t1-without-profile")
    assert drafts.get(draft["draft_id"])["file_count"] == 1


def test_qualification_quest_rejects_runtime_profile_update(
        tmp_path, monkeypatch):
    service, registry, drafts, _processes = _service(tmp_path)
    draft = _create_draft(drafts, quest_id="qualification-update")
    service.publish(draft["draft_id"], start=False, idempotency_key="7" * 32)
    ordinary = registry.get("qualification-update")
    qualified = dataclasses.replace(
        ordinary, qualification_profile_id="sealed-profile")
    real_get = registry.get
    monkeypatch.setattr(
        registry, "get",
        lambda quest_id: qualified if quest_id == "qualification-update"
        else real_get(quest_id))
    before = QuestRuntimeSettings(
        ordinary.work_root, ordinary.quest_id).current()
    with pytest.raises(WebQuestConflictError, match="qualification"):
        service.update_runtime_profile(
            "qualification-update", DEFAULT_PROFILE, "8" * 32)
    assert QuestRuntimeSettings(
        ordinary.work_root, ordinary.quest_id).current() == before


def test_local_dataset_and_references_are_web_attached_verified_and_mounted_readonly(
        tmp_path):
    from orchestrator.run import _web_local_source_mounts

    service, registry, drafts, processes = _service(tmp_path)
    draft = _create_draft(drafts, quest_id="local-folders")
    dataset = tmp_path / "user-data" / "SEED"
    references = tmp_path / "user-references"
    dataset.mkdir(parents=True)
    references.mkdir()
    (dataset / "subject-01.mat").write_bytes(b"eeg bytes")
    (references / "paper.txt").write_text("reference", encoding="utf-8")

    attached_data = service.attach_local_source(
        draft["draft_id"], "dataset", str(dataset), "8" * 32)
    attached_refs = service.attach_local_source(
        draft["draft_id"], "references", str(references), "9" * 32)
    public = json.dumps([attached_data, attached_refs], ensure_ascii=False)
    assert str(tmp_path) not in public
    assert {attached_data["kind"], attached_refs["kind"]} == {
        "dataset", "references"}

    preflight = service.preflight(draft["draft_id"])
    assert {item["dataset"] for item in preflight["candidates"]} >= {"SEED"}
    assert preflight["local_sources"]["file_count"] == 2
    assert str(tmp_path) not in json.dumps(preflight, ensure_ascii=False)

    published = service.publish(
        draft["draft_id"], start=False, idempotency_key="a" * 32)
    quest = registry.get("local-folders")
    internal = json.loads(
        (quest.work_root / "input" / "local-sources.json").read_text(
            encoding="utf-8"))
    assert {item["source_root"] for item in internal["sources"]} == {
        str(dataset), str(references)}
    assert all(item["files"][0]["sha256"].startswith("sha256:")
               for item in internal["sources"])
    assert str(tmp_path) not in json.dumps(published, ensure_ascii=False)
    assert {item["path"] for item in _web_local_source_mounts(quest.work_root)} == {
        str(dataset), str(references)}

    (dataset / "subject-01.mat").write_bytes(b"changed after publication")
    with pytest.raises(WebQuestNotReadyError, match="发生变化"):
        service.start("local-folders", "b" * 32)
    assert processes.started == []


def test_runtime_file_request_upload_publishes_internal_capability_and_replays(
        tmp_path):
    service, registry, drafts, _processes = _service(tmp_path)
    draft = _create_draft(drafts, quest_id="needs-file")
    _upload(drafts, draft["draft_id"], "initial/readme.txt", b"initial")
    service.publish(draft["draft_id"], start=False, idempotency_key="8" * 32)

    created = service.create_request_upload("needs-file", 17, "9" * 32)
    upload_id = created["upload_id"]
    payload = b"user supplied at runtime"
    assert service.request_begin_file(
        "needs-file", 17, upload_id, "answer.csv", len(payload)
    )["path"] == "1/answer.csv"
    service.request_append_chunk(
        "needs-file", 17, upload_id, "answer.csv", 0,
        payload, _digest(payload))
    service.request_finalize_file(
        "needs-file", 17, upload_id, "answer.csv", _digest(payload))

    key = "a" * 32
    publication = service.publish_request_upload(
        "needs-file", 17, upload_id, key)
    assert isinstance(publication, RequestUploadPublication)
    assert publication.quest_id == "needs-file"
    assert publication.request_id == 17
    assert publication.upload_id == upload_id
    assert publication.source_ref == f"work/uploads/web-r17-{upload_id}"
    assert not os.path.isabs(publication.source_ref)
    assert not hasattr(publication, "public_dict")
    assert str(tmp_path) not in json.dumps(dataclasses.asdict(publication))

    quest = registry.get("needs-file")
    stored = quest.work_root / "uploads" / f"web-r17-{upload_id}" / "1" / "answer.csv"
    assert stored.read_bytes() == payload
    assert stat.S_IMODE(stored.stat().st_mode) == 0o400

    # HTTP retries must not turn a completed upload into a corrupt draft or a
    # second target.  The service must replay the durable internal capability.
    assert service.publish_request_upload(
        "needs-file", 17, upload_id, key) == publication


@pytest.mark.parametrize("relative", [
    "../escape.txt",
    "/etc/passwd",
    "folder/../../escape.txt",
    "C:\\Windows\\secret.txt",
    ".hidden/data.txt",
])
def test_browser_relative_paths_cannot_escape_managed_storage(tmp_path, relative):
    service, registry, drafts, _processes = _service(tmp_path)
    draft = _create_draft(drafts, quest_id="path-closure")
    with pytest.raises(ValueError):
        drafts.begin_file(draft["draft_id"], relative, 1)

    # The same path boundary applies to runtime file-request uploads.
    _upload(drafts, draft["draft_id"], "safe/data.txt", b"safe")
    service.publish(draft["draft_id"], start=False, idempotency_key="b" * 32)
    upload = service.create_request_upload("path-closure", 1, "c" * 32)
    with pytest.raises(ValueError):
        service.request_begin_file(
            "path-closure", 1, upload["upload_id"], relative, 1)

    assert not (registry.get("path-closure").work_root / "escape.txt").exists()


def test_combined_browser_and_local_inputs_share_one_draft_budget():
    report = {
        "manifest_sha256": "sha256:" + "0" * 64,
        "candidates": [], "warnings": [],
        "scan": {
            "file_count": 100_000, "directory_count": 1,
            "total_bytes": 256 * 1024 ** 3,
            "archive_count": 0, "archive_member_count": 0,
        },
    }
    local = {
        "status": "preflighted", "file_count": 1, "total_bytes": 1,
        "sources": [],
    }
    with pytest.raises(WebQuestNotReadyError, match="合计超过"):
        WebQuestService._merge_preflight_reports(
            [report], local_public=local, local_identity=[],
            skipped_local_files=0)


def test_local_directory_publish_runs_as_resumable_background_job(tmp_path):
    service, registry, drafts, _processes = _service(tmp_path)
    source = tmp_path / "outside" / "dataset"
    source.mkdir(parents=True)
    (source / "sample.bin").write_bytes(b"real-data")
    draft = _create_draft(drafts, quest_id="background-local")
    service.attach_local_source(
        draft["draft_id"], "dataset", str(source), "d" * 32)
    assert service.publish_needs_background(draft["draft_id"]) is True
    submitted = service.submit_publish(
        draft["draft_id"], start=False, idempotency_key="e" * 32)
    assert submitted["status"] in {"running", "succeeded"}
    deadline = time.monotonic() + 10
    while True:
        status = service.publish_job_status("e" * 32)
        if status["status"] in {"succeeded", "failed"}:
            break
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert status["status"] == "succeeded", status.get("error")
    assert status["result"]["quest"]["quest_id"] == "background-local"
    assert registry.get("background-local").work_root.is_dir()
