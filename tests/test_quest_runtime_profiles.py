"""Strict per-quest runtime-profile ledger."""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from orchestrator.quest_runtime_profiles import (
    DEFAULT_PROFILE,
    QuestRuntimeSettings,
    RuntimeProfileConflictError,
    RuntimeSettingsCorruptError,
    normalize_profile,
    public_options,
)


CPU_OFF = {
    "version": 1,
    "compute_profile_id": "local-cpu",
    "review_intensity": "off",
}

GPU_POOL = {
    "version": 2,
    "compute_profile_id": "local-gpu",
    "review_intensity": "once",
    "gpu_device_indices": [0, 2, 6],
}

CPU_V2 = {
    "version": 2,
    "compute_profile_id": "local-cpu",
    "review_intensity": "off",
    "gpu_device_indices": [],
}

GPU_EXACT = {
    "version": 3,
    "compute_profile_id": "local-gpu",
    "review_intensity": "once",
    "gpu_device_indices": [0, 2, 6],
}

CPU_V3 = {
    "version": 3,
    "compute_profile_id": "local-cpu",
    "review_intensity": "off",
    "gpu_device_indices": [],
}


def _work(tmp_path: Path) -> Path:
    work = tmp_path / "quest"
    work.mkdir(mode=0o700, parents=True)
    (work / "state").mkdir(mode=0o700)
    return work


def _canonical(value: dict) -> bytes:
    return (json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def test_profile_schema_and_public_options_are_strict_and_path_free():
    assert normalize_profile(DEFAULT_PROFILE) == DEFAULT_PROFILE
    assert normalize_profile(CPU_OFF) == CPU_OFF
    assert normalize_profile(GPU_POOL) == GPU_POOL
    assert normalize_profile(CPU_V2) == CPU_V2
    assert normalize_profile(GPU_EXACT) == GPU_EXACT
    assert normalize_profile(CPU_V3) == CPU_V3
    assert "gpu_device_indices" not in normalize_profile(DEFAULT_PROFILE)
    for invalid in (
        {},
        {**DEFAULT_PROFILE, "version": True},
        {**DEFAULT_PROFILE, "version": 2},
        {**DEFAULT_PROFILE, "compute_profile_id": "gpu-7"},
        {**DEFAULT_PROFILE, "review_intensity": 1},
        {**DEFAULT_PROFILE, "device_indices": [7]},
        {**DEFAULT_PROFILE, "gpu_device_indices": [0]},
        {**GPU_POOL, "gpu_device_indices": None},
        {**GPU_POOL, "gpu_device_indices": (0, 2)},
        {**GPU_POOL, "gpu_device_indices": []},
        {**GPU_POOL, "gpu_device_indices": [True]},
        {**GPU_POOL, "gpu_device_indices": [0, "1"]},
        {**GPU_POOL, "gpu_device_indices": [-1]},
        {**GPU_POOL, "gpu_device_indices": [4096]},
        {**GPU_POOL, "gpu_device_indices": [0, 0]},
        {**GPU_POOL, "gpu_device_indices": [2, 0]},
        {**GPU_POOL, "gpu_device_indices": list(range(65))},
        {**CPU_V2, "gpu_device_indices": [0]},
        {key: value for key, value in GPU_EXACT.items()
         if key != "gpu_device_indices"},
        {**GPU_EXACT, "unexpected": False},
        {**GPU_EXACT, "gpu_device_indices": None},
        {**GPU_EXACT, "gpu_device_indices": (0, 2)},
        {**GPU_EXACT, "gpu_device_indices": []},
        {**GPU_EXACT, "gpu_device_indices": [True]},
        {**GPU_EXACT, "gpu_device_indices": [0, "1"]},
        {**GPU_EXACT, "gpu_device_indices": [-1]},
        {**GPU_EXACT, "gpu_device_indices": [4096]},
        {**GPU_EXACT, "gpu_device_indices": [0, 0]},
        {**GPU_EXACT, "gpu_device_indices": [2, 0]},
        {**GPU_EXACT, "gpu_device_indices": list(range(65))},
        {**CPU_V3, "gpu_device_indices": [0]},
    ):
        with pytest.raises(ValueError):
            normalize_profile(invalid)

    options = public_options()
    assert options == {
        "version": 1,
        "compute_profiles": [
            {
                "id": "local-gpu",
                "label": "本机 GPU / Conda / 联网",
                "recommended": True,
            },
            {
                "id": "local-cpu",
                "label": "本机 CPU / Conda / 联网",
                "recommended": False,
            },
        ],
        "review_intensities": [
            {
                "id": "once",
                "label": "每个评审点 1 次",
                "recommended": True,
            },
            {
                "id": "off",
                "label": "关闭",
                "recommended": False,
            },
        ],
        "default_profile": DEFAULT_PROFILE,
    }
    assert [row["id"] for row in options["compute_profiles"]] == [
        "local-gpu", "local-cpu"]
    assert [row["id"] for row in options["review_intensities"]] == [
        "once", "off"]
    assert [row["label"] for row in options["compute_profiles"]] == [
        "本机 GPU / Conda / 联网", "本机 CPU / Conda / 联网"]
    assert [row["label"] for row in options["review_intensities"]] == [
        "每个评审点 1 次", "关闭"]
    rendered = json.dumps(options, ensure_ascii=False)
    assert "GPU / Conda / 联网" in rendered
    assert "CPU / Conda / 联网" in rendered
    assert "device" not in rendered and "/root" not in rendered


def test_public_options_projects_only_the_trusted_gpu_candidate_catalog():
    trusted = list(range(7))
    options = public_options(
        allowed_gpu_indices=trusted, requested_gpu_count=1)

    assert options == {
        **public_options(),
        "version": 2,
        "default_profile": {
            "version": 2,
            "compute_profile_id": "local-gpu",
            "review_intensity": "once",
            "gpu_device_indices": trusted,
        },
        "gpu_devices": [
            {"index": index, "label": f"GPU {index}"}
            for index in trusted
        ],
        "gpu_selection": {"requested_count": 1},
    }
    assert options["version"] == 2
    assert options["default_profile"] == {
        "version": 2,
        "compute_profile_id": "local-gpu",
        "review_intensity": "once",
        "gpu_device_indices": trusted,
    }
    assert options["gpu_devices"] == [
        {"index": index, "label": f"GPU {index}"}
        for index in trusted
    ]
    assert options["gpu_selection"] == {"requested_count": 1}
    rendered = json.dumps(options, ensure_ascii=False, sort_keys=True)
    assert "GPU-" not in rendered and "/root" not in rendered

    # Returned profiles/catalog rows are copies, not mutable aliases to the
    # trusted caller's policy list.
    trusted.append(7)
    assert options["default_profile"]["gpu_device_indices"] == list(range(7))
    assert [row["index"] for row in options["gpu_devices"]] == list(range(7))

    invalid_catalogs = (
        {"allowed_gpu_indices": [0], "requested_gpu_count": None},
        {"allowed_gpu_indices": None, "requested_gpu_count": 1},
        {"allowed_gpu_indices": [], "requested_gpu_count": 1},
        {"allowed_gpu_indices": [1, 0], "requested_gpu_count": 1},
        {"allowed_gpu_indices": [0, 0], "requested_gpu_count": 1},
        {"allowed_gpu_indices": [True], "requested_gpu_count": 1},
        {"allowed_gpu_indices": [4096], "requested_gpu_count": 1},
        {"allowed_gpu_indices": [0], "requested_gpu_count": True},
        {"allowed_gpu_indices": [0], "requested_gpu_count": 0},
        {"allowed_gpu_indices": [0], "requested_gpu_count": 2},
        {"allowed_gpu_indices": list(range(65)), "requested_gpu_count": 1},
    )
    for kwargs in invalid_catalogs:
        with pytest.raises(ValueError):
            public_options(**kwargs)


def test_public_options_v3_selects_exact_default_from_trusted_detection():
    gib = 1024 ** 3
    trusted = list(range(7))
    detected = [
        {
            "index": 0,
            "model": "NVIDIA A100-SXM4-80GB",
            "memory_bytes": 80 * gib,
        },
        {
            "index": 2,
            "model": "NVIDIA A100-SXM4-80GB",
            "memory_bytes": 80 * gib,
        },
        {
            "index": 5,
            "model": "NVIDIA RTX 6000 Ada Generation",
            "memory_bytes": 48 * gib,
        },
    ]

    options = public_options(
        allowed_gpu_indices=trusted,
        requested_gpu_count=2,
        exact_multi_gpu=True,
        gpu_device_labels=detected,
    )

    assert options["version"] == 3
    assert options["default_profile"] == {
        "version": 3,
        "compute_profile_id": "local-gpu",
        "review_intensity": "once",
        "gpu_device_indices": [0, 2, 5],
    }
    assert options["gpu_selection"] == {
        "mode": "exact",
        "default_count": 3,
        "min_count": 1,
        "max_count": 3,
    }
    assert options["gpu_devices"] == [
        {
            **row,
            "label": (
                f"GPU {row['index']} · {row['model']} · "
                f"{row['memory_bytes'] // gib} GiB"),
        }
        for row in detected
    ]
    rendered = json.dumps(options, ensure_ascii=False, sort_keys=True)
    assert "GPU-" not in rendered
    assert "/root" not in rendered
    assert "uuid" not in rendered.lower()

    # Both the exact profile and public rows are copies, not aliases to probe
    # results or the deployment allowlist.
    trusted.append(7)
    detected[0]["model"] = "mutated"
    assert options["default_profile"]["gpu_device_indices"] == [0, 2, 5]
    assert options["gpu_devices"][0]["model"] == "NVIDIA A100-SXM4-80GB"


def test_public_options_v3_without_labels_uses_the_trusted_allowlist():
    options = public_options(
        allowed_gpu_indices=[0, 2, 6],
        requested_gpu_count=1,
        exact_multi_gpu=True,
    )
    assert options["version"] == 3
    assert options["default_profile"]["gpu_device_indices"] == [0, 2, 6]
    assert options["gpu_devices"] == [
        {"index": 0, "label": "GPU 0"},
        {"index": 2, "label": "GPU 2"},
        {"index": 6, "label": "GPU 6"},
    ]
    assert options["gpu_selection"] == {
        "mode": "exact",
        "default_count": 3,
        "min_count": 1,
        "max_count": 3,
    }


@pytest.mark.parametrize("kwargs", [
    {"exact_multi_gpu": 1},
    {"exact_multi_gpu": True},
    {
        "allowed_gpu_indices": [0],
        "requested_gpu_count": 1,
        "gpu_device_labels": [],
    },
    {
        "allowed_gpu_indices": [0],
        "requested_gpu_count": 1,
        "exact_multi_gpu": True,
        "gpu_device_labels": {},
    },
    {
        "allowed_gpu_indices": [0],
        "requested_gpu_count": 1,
        "exact_multi_gpu": True,
        "gpu_device_labels": [],
    },
    {
        "allowed_gpu_indices": [0],
        "requested_gpu_count": 65,
        "exact_multi_gpu": True,
    },
])
def test_public_options_v3_rejects_invalid_catalog_envelopes(kwargs):
    with pytest.raises(ValueError):
        public_options(**kwargs)


@pytest.mark.parametrize("rows", [
    [None],
    [{"index": 0, "model": "NVIDIA A100"}],
    [{
        "index": 0,
        "model": "NVIDIA A100",
        "memory_bytes": 80 * 1024 ** 3,
        "uuid": "GPU-01234567-89ab-cdef-0123-456789abcdef",
    }],
    [{"index": 1, "model": "NVIDIA A100", "memory_bytes": 1}],
    [
        {"index": 2, "model": "NVIDIA A100", "memory_bytes": 1},
        {"index": 0, "model": "NVIDIA A100", "memory_bytes": 1},
    ],
    [
        {"index": 0, "model": "NVIDIA A100", "memory_bytes": 1},
        {"index": 0, "model": "NVIDIA A100", "memory_bytes": 1},
    ],
    [{"index": 0, "model": "", "memory_bytes": 1}],
    [{"index": 0, "model": " NVIDIA A100", "memory_bytes": 1}],
    [{"index": 0, "model": "NVIDIA\nA100", "memory_bytes": 1}],
    [{"index": 0, "model": "../../dev/nvidia0", "memory_bytes": 1}],
    [{"index": 0, "model": r"C:\\GPU", "memory_bytes": 1}],
    [{
        "index": 0,
        "model": "GPU-01234567-89ab-cdef-0123-456789abcdef",
        "memory_bytes": 1,
    }],
    [{
        "index": 0,
        "model": "01234567-89ab-cdef-0123-456789abcdef",
        "memory_bytes": 1,
    }],
    [{"index": 0, "model": "x" * 257, "memory_bytes": 1}],
    [{"index": 0, "model": 7, "memory_bytes": 1}],
    [{"index": 0, "model": "NVIDIA A100", "memory_bytes": True}],
    [{"index": 0, "model": "NVIDIA A100", "memory_bytes": 0}],
    [{"index": 0, "model": "NVIDIA A100", "memory_bytes": -1}],
    [{"index": 0, "model": "NVIDIA A100", "memory_bytes": 1.5}],
    [{
        "index": 0,
        "model": "NVIDIA A100",
        "memory_bytes": 9007199254740992,
    }],
])
def test_public_options_v3_rejects_unsafe_or_untrusted_device_rows(rows):
    with pytest.raises(ValueError):
        public_options(
            allowed_gpu_indices=[0, 2],
            requested_gpu_count=1,
            exact_multi_gpu=True,
            gpu_device_labels=rows,
        )


def test_public_options_v3_default_is_independent_of_legacy_requested_count():
    options = public_options(
        allowed_gpu_indices=[0, 1],
        requested_gpu_count=8,
        exact_multi_gpu=True,
        gpu_device_labels=[{
            "index": 0,
            "model": "NVIDIA A100",
            "memory_bytes": 1,
        }],
    )
    assert options["default_profile"]["gpu_device_indices"] == [0]
    assert options["gpu_selection"] == {
        "mode": "exact",
        "default_count": 1,
        "min_count": 1,
        "max_count": 1,
    }


def test_legacy_read_is_side_effect_free_and_initialize_is_idempotent(tmp_path):
    work = _work(tmp_path)
    settings = QuestRuntimeSettings(work, "alpha")
    assert settings.current() == {
        "quest_id": "alpha",
        "revision": 0,
        "profile": DEFAULT_PROFILE,
        "record_sha256": None,
        "source": "legacy-default",
    }
    assert not (work / "state" / "runtime-settings").exists()

    first = settings.initialize(DEFAULT_PROFILE, "1" * 32)
    assert first["revision"] == 1 and first["source"] == "ledger"
    assert first["record_sha256"].startswith("sha256:")
    assert settings.initialize(DEFAULT_PROFILE, "1" * 32) == first
    # A distinct initialize key against the same value is a read-only replay,
    # not a second initialization revision.
    assert settings.initialize(DEFAULT_PROFILE, "2" * 32) == first
    assert len(settings.history()) == 1
    with pytest.raises(RuntimeProfileConflictError):
        settings.initialize(CPU_OFF, "3" * 32)

    root = work / "state" / "runtime-settings"
    revision = root / "revisions" / "00000000000000000001.json"
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "revisions").stat().st_mode) == 0o700
    assert stat.S_IMODE((root / ".lock").stat().st_mode) == 0o600
    assert stat.S_IMODE(revision.stat().st_mode) == 0o600
    raw = revision.read_bytes()
    assert raw == _canonical(json.loads(raw))
    assert first["record_sha256"] == "sha256:" + hashlib.sha256(raw).hexdigest()


def test_v2_and_v3_updates_never_rewrite_existing_record_bytes(tmp_path):
    settings = QuestRuntimeSettings(_work(tmp_path), "alpha")
    first = settings.initialize(DEFAULT_PROFILE, "1" * 32)
    first_path = (
        settings.revisions_dir / "00000000000000000001.json")
    first_raw = first_path.read_bytes()

    second = settings.update(GPU_POOL, "2" * 32)
    second_path = (
        settings.revisions_dir / "00000000000000000002.json")
    second_raw = second_path.read_bytes()

    assert first_path.read_bytes() == first_raw
    assert first["record_sha256"] == (
        "sha256:" + hashlib.sha256(first_raw).hexdigest())
    assert settings.current() == second
    assert second["revision"] == 2
    assert second["profile"] == GPU_POOL

    third = settings.update(GPU_EXACT, "3" * 32)

    assert first_path.read_bytes() == first_raw
    assert second_path.read_bytes() == second_raw
    assert second["record_sha256"] == (
        "sha256:" + hashlib.sha256(second_raw).hexdigest())
    assert settings.current() == third
    assert third["revision"] == 3
    assert third["profile"] == GPU_EXACT
    history = settings.history()
    assert history[0]["profile"] == DEFAULT_PROFILE
    assert history[1]["profile"] == GPU_POOL
    assert history[1]["previous_sha256"] == first["record_sha256"]
    assert history[2]["profile"] == GPU_EXACT
    assert history[2]["previous_sha256"] == second["record_sha256"]


def test_update_appends_hash_chained_revisions_and_binds_idempotency(tmp_path):
    settings = QuestRuntimeSettings(_work(tmp_path), "alpha")
    first = settings.initialize(DEFAULT_PROFILE, "1" * 32)
    second = settings.update(CPU_OFF, "2" * 32)
    assert second == settings.current()
    assert second["revision"] == 2 and second["profile"] == CPU_OFF
    history = settings.history()
    assert [row["revision"] for row in history] == [1, 2]
    assert history[1]["previous_sha256"] == first["record_sha256"]
    assert history[1]["operation"] == "update"
    assert settings.update(CPU_OFF, "2" * 32) == second
    with pytest.raises(RuntimeProfileConflictError, match="不同"):
        settings.update(DEFAULT_PROFILE, "2" * 32)


def test_initialize_same_value_new_key_is_durably_stable_after_later_update(
        tmp_path):
    settings = QuestRuntimeSettings(_work(tmp_path), "alpha")
    first = settings.initialize(DEFAULT_PROFILE, "1" * 32)
    replay_key = "2" * 32
    assert settings.initialize(DEFAULT_PROFILE, replay_key) == first
    receipt_path = settings.operations_dir / f"{replay_key}.json"
    assert receipt_path.exists()
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["operation"] == "initialize"
    assert receipt["changed"] is False

    changed = settings.update(CPU_OFF, "3" * 32)
    assert settings.initialize(DEFAULT_PROFILE, replay_key) == first
    assert settings.current() == changed
    with pytest.raises(RuntimeProfileConflictError, match="不同 runtime profile 操作"):
        settings.begin_runtime_update(DEFAULT_PROFILE, replay_key)


def test_legacy_first_update_is_allowed(tmp_path):
    settings = QuestRuntimeSettings(_work(tmp_path), "legacy")
    current = settings.update(CPU_OFF, "4" * 32)
    assert current["revision"] == 1
    assert settings.history()[0]["operation"] == "update"


def test_hash_chain_tamper_and_late_initialize_fail_closed(tmp_path):
    work = _work(tmp_path)
    settings = QuestRuntimeSettings(work, "alpha")
    settings.initialize(DEFAULT_PROFILE, "1" * 32)
    settings.update(CPU_OFF, "2" * 32)
    first_path = (
        work / "state" / "runtime-settings" / "revisions"
        / "00000000000000000001.json")
    first = json.loads(first_path.read_text(encoding="utf-8"))
    first["recorded_at"] = "2020-01-01T00:00:00Z"
    first_path.write_bytes(_canonical(first))
    with pytest.raises(RuntimeSettingsCorruptError, match="previous_sha256"):
        settings.current()

    other = QuestRuntimeSettings(_work(tmp_path / "other"), "beta")
    other.initialize(DEFAULT_PROFILE, "3" * 32)
    other.update(CPU_OFF, "4" * 32)
    second_path = (
        other.work_root / "state" / "runtime-settings" / "revisions"
        / "00000000000000000002.json")
    second = json.loads(second_path.read_text(encoding="utf-8"))
    second["operation"] = "initialize"
    second_path.write_bytes(_canonical(second))
    with pytest.raises(RuntimeSettingsCorruptError, match="首条"):
        other.current()


def test_revision_symlink_is_never_followed(tmp_path):
    work = _work(tmp_path)
    settings = QuestRuntimeSettings(work, "alpha")
    settings.initialize(DEFAULT_PROFILE, "1" * 32)
    revisions = work / "state" / "runtime-settings" / "revisions"
    target = tmp_path / "outside.json"
    target.write_text("{}\n", encoding="utf-8")
    (revisions / "00000000000000000002.json").symlink_to(target)
    with pytest.raises(RuntimeSettingsCorruptError):
        settings.current()
    assert target.read_text(encoding="utf-8") == "{}\n"


def test_cycle_binding_is_exact_idempotent_and_clear_is_identity_guarded(tmp_path):
    work = _work(tmp_path)
    settings = QuestRuntimeSettings(work, "alpha")
    first = settings.initialize(DEFAULT_PROFILE, "1" * 32)
    second = settings.update(CPU_OFF, "2" * 32)

    assert settings.record(first["revision"], first["record_sha256"]) == first
    assert settings.bound_cycle_profile() is None
    assert settings.bind_cycle_profile(first) == first
    assert settings.bind_cycle_profile(first) == first
    assert settings.bound_cycle_profile() == first
    binding = work / "state" / "runtime-settings" / "cycle-binding.json"
    assert stat.S_IMODE(binding.stat().st_mode) == 0o600
    assert binding.read_bytes() == _canonical(json.loads(binding.read_bytes()))
    with pytest.raises(RuntimeProfileConflictError, match="clear identity"):
        settings.clear_cycle_profile(second)
    assert settings.bound_cycle_profile() == first
    assert settings.clear_cycle_profile(first) is True
    assert settings.clear_cycle_profile(first) is False
    assert settings.bound_cycle_profile() is None


def test_legacy_cycle_binding_remains_resolvable_after_first_revision(tmp_path):
    settings = QuestRuntimeSettings(_work(tmp_path), "legacy")
    legacy = settings.record(0, None)
    assert settings.bind_cycle_profile(legacy) == legacy
    settings.update(CPU_OFF, "3" * 32)
    assert settings.bound_cycle_profile() == legacy
    assert settings.clear_cycle_profile(legacy) is True


def test_runtime_update_receipt_preserves_historical_noop_across_later_change(
        tmp_path):
    work = _work(tmp_path)
    settings = QuestRuntimeSettings(work, "alpha")
    first = settings.initialize(DEFAULT_PROFILE, "1" * 32)
    noop = settings.begin_runtime_update(DEFAULT_PROFILE, "a" * 32)
    assert noop == {
        "quest_id": "alpha",
        "idempotency_key": "a" * 32,
        "operation": "update",
        "owner_intent_revision": 0,
        "request_profile": DEFAULT_PROFILE,
        "outcome": first,
        "changed": False,
        "schedule_required": False,
        "status": "not-required",
    }
    changed = settings.begin_runtime_update(CPU_OFF, "b" * 32)
    assert changed["changed"] is True
    assert changed["status"] == "pending"
    assert changed["outcome"]["revision"] == 2
    assert settings.begin_runtime_update(DEFAULT_PROFILE, "a" * 32) == noop
    assert settings.current() == changed["outcome"]
    assert len(settings.history()) == 2

    operations = work / "state" / "runtime-settings" / "operations"
    assert stat.S_IMODE(operations.stat().st_mode) == 0o700
    for path in operations.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert path.read_bytes() == _canonical(json.loads(path.read_bytes()))


def test_runtime_update_receipt_recovers_ledger_crash_gap_and_completion(tmp_path):
    settings = QuestRuntimeSettings(_work(tmp_path), "alpha")
    settings.initialize(DEFAULT_PROFILE, "1" * 32)
    committed = settings.update(CPU_OFF, "c" * 32)
    assert settings.runtime_update_operation(CPU_OFF, "c" * 32) is not None
    recovered = settings.runtime_update_operation(CPU_OFF, "c" * 32)
    assert recovered == {
        "quest_id": "alpha",
        "idempotency_key": "c" * 32,
        "operation": "update",
        "owner_intent_revision": 0,
        "request_profile": CPU_OFF,
        "outcome": committed,
        "changed": True,
        "schedule_required": True,
        "status": "pending",
    }
    assert len(settings.history()) == 2
    completed = settings.complete_runtime_update("c" * 32, "scheduled")
    assert completed["status"] == "accepted"
    assert settings.complete_runtime_update(
        "c" * 32, "scheduled") == completed
    assert settings.runtime_update_operation(
        CPU_OFF, "c" * 32) == completed
    settled = settings.settle_runtime_update("c" * 32, "applied")
    assert settled["status"] == "applied"
    with pytest.raises(RuntimeProfileConflictError, match="different|different result|不同结果"):
        settings.settle_runtime_update("c" * 32, "not-required")


def test_runtime_update_receipt_files_fail_closed_on_link_or_unknown_entry(tmp_path):
    work = _work(tmp_path)
    settings = QuestRuntimeSettings(work, "alpha")
    settings.initialize(DEFAULT_PROFILE, "1" * 32)
    settings.begin_runtime_update(DEFAULT_PROFILE, "a" * 32)
    operations = work / "state" / "runtime-settings" / "operations"
    target = tmp_path / "outside.json"
    target.write_text("{}\n", encoding="utf-8")
    (operations / ("b" * 32 + ".json")).symlink_to(target)
    with pytest.raises(RuntimeSettingsCorruptError):
        settings.current()
    assert target.read_text(encoding="utf-8") == "{}\n"


def test_owner_intent_fence_is_durable_and_historical_keys_cannot_reauthorize(
        tmp_path):
    settings = QuestRuntimeSettings(_work(tmp_path), "alpha")
    settings.initialize(DEFAULT_PROFILE, "1" * 32)
    start_key = "a" * 32
    stop_key = "b" * 32
    restart_key = "c" * 32
    assert settings.authorize_explicit_start(start_key)["authorized"] is True
    pending = settings.begin_runtime_update(CPU_OFF, "d" * 32)
    assert pending["status"] == "pending"
    assert settings.record_explicit_stop(stop_key)["applied"] is True
    assert settings.runtime_update_operation_by_key(
        "d" * 32)["status"] == "terminated"
    assert settings.authorize_explicit_start(start_key)["authorized"] is False

    assert settings.authorize_explicit_start(restart_key)["authorized"] is True
    assert settings.record_explicit_stop(stop_key)["applied"] is False
    new_operation = settings.begin_runtime_update(DEFAULT_PROFILE, "e" * 32)
    assert new_operation["owner_intent_revision"] == 3
    assert new_operation["status"] == "pending"
