"""Qualification profile catalog safety and redaction contract."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from orchestrator.qualification_firewall import CONTRACT_PROTOCOL
from orchestrator.qualification_profiles import (
    QualificationProfileError,
    QualificationProfileRegistry,
)


def _canonical(value) -> bytes:
    return (json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False) + "\n").encode("utf-8")


def _contract(*, task="T1", gpu=False, suffix=""):
    mounts = [
        {"path": f"/qualification/SEED{suffix}", "role": "explore",
         "dataset": "SEED", "fold": None, "view_receipt_sha256": None},
        {"path": f"/qualification/FACED{suffix}", "role": "explore",
         "dataset": "FACED", "fold": None, "view_receipt_sha256": None},
        {"path": f"/qualification/DREAMER{suffix}", "role": "sealed_holdout",
         "dataset": "DREAMER", "fold": None,
         "view_receipt_sha256": "sha256:" + "1" * 64},
    ]
    return {
        "version": 1,
        "protocol": CONTRACT_PROTOCOL,
        "task": task,
        "research_uid": 1000,
        "evaluator_uid": 0,
        "forbid_code_imports": True,
        "mounts": mounts,
        "sealed_truth": {
            "path": f"/qualification/sealed{suffix}/truth.json",
            "sha256": "sha256:" + "2" * 64,
        },
        "final": {
            "classes": 2, "seeds": [], "folds": [],
            "unit_ids": ["dreamer"], "gpu_required": gpu,
        },
    }


def _profile(profile_id="t1-cpu", *, gpu=False, suffix=""):
    return {
        "version": 1,
        "profile_id": profile_id,
        "title": "T1 CPU" if not gpu else "T1 GPU",
        "template_id": "t1-eeg-universal",
        "contract": _contract(gpu=gpu, suffix=suffix),
    }


def _write(path: Path, value) -> None:
    path.write_bytes(_canonical(value))
    path.chmod(0o600)


def test_missing_and_empty_catalog_are_empty_and_get_missing_is_keyerror(tmp_path):
    missing = QualificationProfileRegistry(tmp_path / "missing")
    assert missing.list() == []
    assert len(missing) == 0
    with pytest.raises(KeyError, match="absent"):
        missing.get("absent")

    empty_root = tmp_path / "empty"
    empty_root.mkdir()
    (empty_root / "README.txt").write_text("ignored\n", encoding="utf-8")
    assert QualificationProfileRegistry(empty_root).list() == []


def test_load_freezes_contract_returns_deep_copies_and_redacts_public_projection(tmp_path):
    root = tmp_path / "profiles"
    root.mkdir()
    source = _profile()
    path = root / "t1.json"
    _write(path, source)

    registry = QualificationProfileRegistry(root)
    loaded = registry.get("t1-cpu")
    expected_contract_raw = _canonical(source["contract"])
    assert loaded.contract_sha256 == "sha256:" + hashlib.sha256(expected_contract_raw).hexdigest()
    assert loaded.task == "T1"
    assert registry.list() == [loaded]

    first = loaded.contract()
    first["mounts"][0]["path"] = "/mutated"
    assert loaded.contract()["mounts"][0]["path"] == "/qualification/SEED"

    public = loaded.public_dict()
    assert set(public) == {
        "profile_id", "title", "template_id", "task", "contract_sha256",
        "datasets", "gpu_required",
    }
    assert public["datasets"] == ["DREAMER", "FACED", "SEED"]
    assert public["gpu_required"] is False
    serialized = json.dumps(public, ensure_ascii=False)
    assert "research_uid" not in serialized
    assert "evaluator_uid" not in serialized
    assert "sealed_truth" not in serialized
    assert "/qualification/" not in serialized

    # Catalog membership and bytes are frozen at registry construction.
    changed = _profile(gpu=True)
    _write(path, changed)
    _write(root / "later.json", _profile("later", suffix="-later"))
    assert registry.get("t1-cpu").contract()["final"]["gpu_required"] is False
    assert [item.profile_id for item in registry.list()] == ["t1-cpu"]


def test_catalog_is_sorted_by_profile_id_not_filename(tmp_path):
    root = tmp_path / "profiles"
    root.mkdir()
    _write(root / "01-z.json", _profile("z-profile", suffix="-z"))
    _write(root / "99-a.json", _profile("a-profile", gpu=True, suffix="-a"))
    profiles = QualificationProfileRegistry(root).list()
    assert [item.profile_id for item in profiles] == ["a-profile", "z-profile"]
    assert profiles[0].public_dict()["gpu_required"] is True


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(extra=True),
        lambda value: value.update(version=True),
        lambda value: value.update(profile_id="Bad_Profile"),
        lambda value: value["contract"].update(extra=True),
        lambda value: value["contract"]["mounts"][0].update(extra=True),
        lambda value: value["contract"]["sealed_truth"].update(extra=True),
        lambda value: value["contract"]["final"].update(extra=True),
        lambda value: value["contract"].update(protocol="wrong/v1"),
        lambda value: value["contract"].update(research_uid=0),
        lambda value: value["contract"]["final"].update(gpu_required="false"),
    ],
)
def test_profile_and_contract_closed_structure_fail_fast_without_touching_views(
        tmp_path, mutation):
    root = tmp_path / "profiles"
    root.mkdir()
    value = _profile()
    mutation(value)
    _write(root / "bad.json", value)
    with pytest.raises(ValueError):
        QualificationProfileRegistry(root)


def test_duplicate_profile_ids_and_noncanonical_json_are_rejected(tmp_path):
    root = tmp_path / "profiles"
    root.mkdir()
    _write(root / "one.json", _profile("same", suffix="-one"))
    _write(root / "two.json", _profile("same", suffix="-two"))
    with pytest.raises(QualificationProfileError, match="profile_id 重复"):
        QualificationProfileRegistry(root)

    root2 = tmp_path / "noncanonical"
    root2.mkdir()
    (root2 / "pretty.json").write_text(
        json.dumps(_profile(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(QualificationProfileError, match="canonical JSON"):
        QualificationProfileRegistry(root2)


def test_profile_file_must_be_regular_safe_owned_bounded_and_not_writable(tmp_path):
    target = tmp_path / "target.json"
    _write(target, _profile())
    symlink_root = tmp_path / "symlink-profiles"
    symlink_root.mkdir()
    (symlink_root / "profile.json").symlink_to(target)
    with pytest.raises(QualificationProfileError, match="安全打开"):
        QualificationProfileRegistry(symlink_root)

    writable_root = tmp_path / "writable-profiles"
    writable_root.mkdir()
    writable = writable_root / "profile.json"
    _write(writable, _profile())
    writable.chmod(0o620)
    with pytest.raises(QualificationProfileError, match="owner/权限/大小"):
        QualificationProfileRegistry(writable_root)

    oversized_root = tmp_path / "oversized-profiles"
    oversized_root.mkdir()
    oversized = oversized_root / "profile.json"
    oversized.write_bytes(b" " * (256 * 1024 + 1))
    oversized.chmod(0o600)
    with pytest.raises(QualificationProfileError, match="owner/权限/大小"):
        QualificationProfileRegistry(oversized_root)

    directory_root = tmp_path / "directory-profiles"
    directory_root.mkdir()
    (directory_root / "not-a-file.json").mkdir()
    with pytest.raises(QualificationProfileError):
        QualificationProfileRegistry(directory_root)


@pytest.mark.skipif(os.geteuid() != 0, reason="changing file owner requires root")
def test_profile_file_rejects_owner_other_than_root_or_current_euid(tmp_path):
    root = tmp_path / "profiles"
    root.mkdir()
    path = root / "profile.json"
    _write(path, _profile())
    os.chown(path, 65534, -1)
    with pytest.raises(QualificationProfileError, match="owner/权限/大小"):
        QualificationProfileRegistry(root)


def test_contract_validation_is_structural_and_defers_external_view_semantics(tmp_path):
    root = tmp_path / "profiles"
    root.mkdir()
    value = _profile()
    # None of these paths exists and the T1 profile intentionally has only two
    # distinct explore identities.  Registry loading still succeeds: owner,
    # receipt, path existence and full T1 semantics belong to install_contract.
    _write(root / "profile.json", value)
    loaded = QualificationProfileRegistry(root).get("t1-cpu")
    assert loaded.contract()["sealed_truth"]["path"].endswith("/truth.json")
