from __future__ import annotations

import io
import json
import os
import stat
import tarfile
import zipfile
from pathlib import Path

import pytest

from orchestrator.dataset_preflight import (
    DatasetPreflightError,
    DatasetPreflightLimits,
    ManagedDraftFile,
    ManagedDraftManifest,
    PREFLIGHT_PROTOCOL,
    preflight_managed_datasets,
)


def _entry(path: Path, *, file_id: str, bundle_id: str,
           display: str, root: Path):
    return {
        "file_id": file_id,
        "bundle_id": bundle_id,
        "stored_relpath": path.relative_to(root).as_posix(),
        "display_relpath": display,
        "size_bytes": path.stat().st_size,
    }


def _manifest(entries):
    return {"version": 1, "files": entries}


def _write_zip(path: Path, members, *, compression=zipfile.ZIP_STORED):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=compression) as archive:
        for name, body in members:
            archive.writestr(name, body)
    return path


def _write_tar(path: Path, members):
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "w:gz" if path.name.endswith((".tgz", ".tar.gz")) else "w"
    with tarfile.open(path, mode) as archive:
        for name, body in members:
            info = tarfile.TarInfo(name)
            info.size = len(body)
            archive.addfile(info, io.BytesIO(body))
    return path


def test_recognizes_all_supported_datasets_and_t1_candidate_requirements(tmp_path):
    root = tmp_path / "managed" / "files"
    entries = []

    dreamer = _write_zip(root / "objects" / "f1", [
        ("DREAMER/DREAMER.mat", b"mat"), ("DREAMER/DREAMER.pdf", b"pdf")])
    entries.append(_entry(
        dreamer, file_id="f1", bundle_id="dreamer-upload",
        display="DREAMER.zip", root=root))

    # One selected SEED directory becomes one bundle even though it has
    # several independently managed files.
    for number, display in enumerate((
            "SEED/Preprocessed_EEG/label.mat",
            "SEED/Preprocessed_EEG/1_20131027.mat"), start=2):
        path = root / "objects" / f"f{number}"
        path.write_bytes(b"mat")
        entries.append(_entry(
            path, file_id=f"f{number}", bundle_id="seed-directory",
            display=display, root=root))

    seed_iv = _write_zip(root / "objects" / "f4", [
        ("SEED_IV/eeg_feature_smooth/1/1_20160518.mat", b"mat")])
    entries.append(_entry(
        seed_iv, file_id="f4", bundle_id="seed-iv-upload",
        display="eeg_feature_smooth.zip", root=root))

    faced = root / "objects" / "f5"
    faced.write_bytes(b"opaque")
    entries.append(_entry(
        faced, file_id="f5", bundle_id="faced-upload",
        display="FACED/Processed_data/sub001.pkl", root=root))

    deap = _write_zip(root / "objects" / "f6", [
        ("data_preprocessed_python/s01.dat", b"one"),
        ("data_preprocessed_python/s02.dat", b"two")])
    entries.append(_entry(
        deap, file_id="f6", bundle_id="deap-upload",
        display="download.zip", root=root))

    mped = _write_tar(root / "objects" / "f7", [
        ("MPED/features/subject_01.mat", b"mat")])
    entries.append(_entry(
        mped, file_id="f7", bundle_id="mped-upload",
        display="features.tar", root=root))

    report = preflight_managed_datasets(root, _manifest(entries))
    recognized = {candidate.dataset for candidate in report.candidates}
    assert recognized == {"DREAMER", "SEED", "SEED-IV", "FACED", "DEAP", "MPED"}
    assert len([item for item in report.candidates if item.dataset == "SEED"]) == 1
    assert all(item.confidence >= 0.75 and item.reasons for item in report.candidates)
    assert report.t1.sealed_holdout_exactly_one
    assert report.t1.explore_datasets == ("SEED", "SEED-IV", "FACED", "DEAP", "MPED")
    assert report.t1.explore_distinct_at_least_three
    assert report.t1.candidate_requirements_met
    assert report.archive_count == 4
    assert report.archive_member_count == 6

    public = report.public_dict()
    assert public["protocol"] == PREFLIGHT_PROTOCOL
    assert public["scientific_qualification_status"] == "not_assessed"
    assert public["t1_requirements"]["scientific_qualification_status"] == "not_assessed"
    assert "不验证" in public["warnings"][0]
    encoded = json.dumps(public, ensure_ascii=False)
    assert str(tmp_path) not in encoded
    assert "stored_relpath" not in encoded
    assert "objects/f1" not in encoded


def test_seed_iv_token_does_not_also_create_seed_candidate(tmp_path):
    root = tmp_path / "files"
    path = root / "safe"
    path.parent.mkdir()
    path.write_bytes(b"x")
    report = preflight_managed_datasets(root, _manifest([
        _entry(path, file_id="f1", bundle_id="b1",
               display="SEED_IV/Preprocessed_EEG/label.mat", root=root)]))
    assert [item.dataset for item in report.candidates] == ["SEED-IV"]


def test_unknown_is_explicit_and_never_claims_qualification(tmp_path):
    root = tmp_path / "files"
    path = root / "asset"
    root.mkdir()
    path.write_bytes(b"opaque")
    report = preflight_managed_datasets(root, _manifest([
        _entry(path, file_id="f1", bundle_id="mystery",
               display="download/data.bin", root=root)]))
    candidate = report.candidates[0]
    assert candidate.dataset == "unknown"
    assert candidate.confidence == 0.0
    assert candidate.reasons
    assert not report.t1.candidate_requirements_met
    assert report.public_dict()["scientific_qualification_status"] == "not_assessed"


def test_t1_counts_dreamer_bundles_but_explore_dataset_identities_are_distinct(tmp_path):
    root = tmp_path / "files"
    root.mkdir()
    entries = []
    displays = ["DREAMER.zip", "copy/DREAMER.zip", "SEED.zip", "copy/SEED.zip"]
    for number, display in enumerate(displays, start=1):
        path = root / f"asset-{number}"
        path.write_bytes(b"not actually an archive")
        entries.append(_entry(
            path, file_id=f"f{number}", bundle_id=f"b{number}",
            display=display, root=root))
    report = preflight_managed_datasets(root, _manifest(entries))
    assert report.t1.sealed_holdout_candidate_count == 2
    assert not report.t1.sealed_holdout_exactly_one
    assert report.t1.explore_datasets == ("SEED",)
    assert not report.t1.explore_distinct_at_least_three
    assert not report.t1.candidate_requirements_met
    assert all(candidate.warnings for candidate in report.candidates)


@pytest.mark.parametrize("bad", [
    "/etc/passwd",
    "../DREAMER.zip",
    "folder/../DREAMER.zip",
    r"C:\\fakepath\\DREAMER.zip",
    "folder//DREAMER.zip",
    "./DREAMER.zip",
])
def test_manifest_rejects_browser_host_absolute_or_noncanonical_paths(tmp_path, bad):
    root = tmp_path / "files"
    root.mkdir()
    value = _manifest([{
        "file_id": "f1", "bundle_id": "b1", "stored_relpath": "asset-1",
        "display_relpath": bad, "size_bytes": 1,
    }])
    with pytest.raises(DatasetPreflightError, match="路径|绝对|空段"):
        preflight_managed_datasets(root, value)


def test_manifest_is_closed_and_rejects_duplicate_identity(tmp_path):
    root = tmp_path / "files"
    root.mkdir()
    base = {
        "file_id": "f1", "bundle_id": "b1", "stored_relpath": "asset-1",
        "display_relpath": "DREAMER.zip", "size_bytes": 1,
    }
    with pytest.raises(DatasetPreflightError, match="字段"):
        preflight_managed_datasets(root, {"version": 1, "files": [], "host_path": "/tmp"})
    with pytest.raises(DatasetPreflightError, match="字段闭包"):
        preflight_managed_datasets(root, _manifest([{**base, "host_path": "/tmp/x"}]))
    with pytest.raises(DatasetPreflightError, match="file_id 重复"):
        preflight_managed_datasets(root, _manifest([base, {
            **base, "stored_relpath": "asset-2", "display_relpath": "SEED.zip"}]))
    with pytest.raises(DatasetPreflightError, match="stored_relpath 重复"):
        preflight_managed_datasets(root, _manifest([base, {
            **base, "file_id": "f2", "display_relpath": "SEED.zip"}]))
    with pytest.raises(DatasetPreflightError, match="version"):
        preflight_managed_datasets(root, {"version": True, "files": []})


def test_dataclass_manifest_is_revalidated_not_trusted(tmp_path):
    root = tmp_path / "files"
    root.mkdir()
    manifest = ManagedDraftManifest(version=1, files=(ManagedDraftFile(
        file_id="f1", bundle_id="b1", stored_relpath="../escape",
        display_relpath="DREAMER.zip", size_bytes=1),))
    with pytest.raises(DatasetPreflightError, match=r"\. / \.\."):
        preflight_managed_datasets(root, manifest)
    with pytest.raises(DatasetPreflightError, match="pathlib.Path"):
        preflight_managed_datasets(str(root), _manifest([]))  # type: ignore[arg-type]


def test_manifest_and_tree_must_be_exactly_reconciled(tmp_path):
    root = tmp_path / "files"
    root.mkdir()
    first = root / "a"
    first.write_bytes(b"x")
    missing = _entry(first, file_id="f1", bundle_id="b1",
                     display="DREAMER.zip", root=root)
    missing["stored_relpath"] = "missing"
    with pytest.raises(DatasetPreflightError, match="未登记"):
        preflight_managed_datasets(root, _manifest([missing]))

    first.unlink()
    with pytest.raises(DatasetPreflightError, match="缺失"):
        preflight_managed_datasets(root, _manifest([missing]))

    first.write_bytes(b"xx")
    mismatch = _entry(first, file_id="f1", bundle_id="b1",
                      display="DREAMER.zip", root=root)
    mismatch["size_bytes"] = 1
    with pytest.raises(DatasetPreflightError, match="大小"):
        preflight_managed_datasets(root, _manifest([mismatch]))


def test_rejects_root_file_and_directory_symlinks(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root_link = tmp_path / "root-link"
    root_link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(DatasetPreflightError, match="symlink"):
        preflight_managed_datasets(root_link, _manifest([]))

    root = tmp_path / "files"
    root.mkdir()
    target = root / "target"
    target.write_bytes(b"x")
    link = root / "asset"
    link.symlink_to(target)
    entries = [_entry(target, file_id="f1", bundle_id="b1",
                      display="plain.bin", root=root)]
    entries.append({
        "file_id": "f2", "bundle_id": "b2", "stored_relpath": "asset",
        "display_relpath": "DREAMER.zip", "size_bytes": 1,
    })
    with pytest.raises(DatasetPreflightError, match="symlink|canonical root"):
        preflight_managed_datasets(root, _manifest(entries))

    link.unlink()
    nested_target = root / "real-dir"
    nested_target.mkdir()
    directory_link = root / "linked-dir"
    directory_link.symlink_to(nested_target, target_is_directory=True)
    with pytest.raises(DatasetPreflightError, match="symlink|canonical root"):
        preflight_managed_datasets(root, _manifest(entries[:1]))


def test_rejects_hardlink_and_fifo_without_opening_them(tmp_path):
    root = tmp_path / "files"
    root.mkdir()
    first = root / "first"
    first.write_bytes(b"x")
    second = root / "second"
    os.link(first, second)
    entries = [
        _entry(first, file_id="f1", bundle_id="b1", display="SEED.mat", root=root),
        _entry(second, file_id="f2", bundle_id="b2", display="FACED.mat", root=root),
    ]
    with pytest.raises(DatasetPreflightError, match="hardlink"):
        preflight_managed_datasets(root, _manifest(entries))

    first.unlink()
    second.unlink()
    fifo = root / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(DatasetPreflightError, match="device/FIFO/socket"):
        preflight_managed_datasets(root, _manifest([]))


def test_finite_tree_limits_cover_files_directories_depth_and_bytes(tmp_path):
    root = tmp_path / "files"
    deep = root / "one" / "two"
    deep.mkdir(parents=True)
    path = deep / "asset"
    path.write_bytes(b"1234")
    entry = _entry(path, file_id="f1", bundle_id="b1",
                   display="DREAMER.mat", root=root)

    with pytest.raises(DatasetPreflightError, match="深度"):
        preflight_managed_datasets(
            root, _manifest([entry]), limits=DatasetPreflightLimits(max_depth=1))
    with pytest.raises(DatasetPreflightError, match="目录数"):
        preflight_managed_datasets(
            root, _manifest([entry]), limits=DatasetPreflightLimits(max_directories=1))
    with pytest.raises(DatasetPreflightError, match="总字节"):
        preflight_managed_datasets(
            root, _manifest([entry]), limits=DatasetPreflightLimits(max_total_bytes=3))
    with pytest.raises(DatasetPreflightError, match="单文件|size_bytes"):
        preflight_managed_datasets(
            root, _manifest([entry]), limits=DatasetPreflightLimits(max_file_bytes=3))


def test_zip_bomb_member_count_ratio_and_directory_limits_are_fail_closed(tmp_path):
    root = tmp_path / "files"
    bomb = _write_zip(
        root / "bomb", [("DREAMER/DREAMER.mat", b"0" * 100_000)],
        compression=zipfile.ZIP_DEFLATED)
    entry = _entry(bomb, file_id="f1", bundle_id="b1",
                   display="DREAMER.zip", root=root)
    with pytest.raises(DatasetPreflightError, match="compression ratio"):
        preflight_managed_datasets(
            root, _manifest([entry]),
            limits=DatasetPreflightLimits(max_compression_ratio=2))

    many = _write_zip(root / "many", [(f"SEED/{i}.mat", b"x") for i in range(3)])
    entry = _entry(many, file_id="f1", bundle_id="b1",
                   display="SEED.zip", root=root)
    bomb.unlink()
    with pytest.raises(DatasetPreflightError, match="member"):
        preflight_managed_datasets(
            root, _manifest([entry]), limits=DatasetPreflightLimits(max_archive_members=2))
    with pytest.raises(DatasetPreflightError, match="central directory"):
        preflight_managed_datasets(
            root, _manifest([entry]),
            limits=DatasetPreflightLimits(max_archive_directory_bytes=10))


def test_archive_traversal_and_link_members_are_ignored_and_never_extracted(tmp_path):
    root = tmp_path / "files"
    archive_path = root / "archive"
    archive_path.parent.mkdir()
    link_info = zipfile.ZipInfo("FACED/link")
    link_info.create_system = 3
    link_info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("../DREAMER.mat", b"not evidence")
        archive.writestr(link_info, "../../outside")
        archive.writestr("safe/DEAP/readme.txt", b"safe")
    entry = _entry(archive_path, file_id="f1", bundle_id="b1",
                   display="download.zip", root=root)
    report = preflight_managed_datasets(root, _manifest([entry]))
    assert [candidate.dataset for candidate in report.candidates] == ["DEAP"]
    assert any("忽略" in warning for warning in report.candidates[0].warnings)
    assert not (tmp_path / "DREAMER.mat").exists()
    assert not (tmp_path / "outside").exists()


def test_tar_member_limits_and_link_warning(tmp_path):
    root = tmp_path / "files"
    archive_path = _write_tar(root / "archive", [("MPED/data.bin", b"12345")])
    with tarfile.open(archive_path, "a") as archive:
        link = tarfile.TarInfo("MPED/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../outside"
        archive.addfile(link)
    entry = _entry(archive_path, file_id="f1", bundle_id="b1",
                   display="dataset.tar", root=root)
    report = preflight_managed_datasets(root, _manifest([entry]))
    assert [item.dataset for item in report.candidates] == ["MPED"]
    assert any("link/device" in warning for warning in report.candidates[0].warnings)
    with pytest.raises(DatasetPreflightError, match="member 声明大小"):
        preflight_managed_datasets(
            root, _manifest([entry]),
            limits=DatasetPreflightLimits(max_archive_member_bytes=4))


def test_empty_managed_draft_is_a_valid_incomplete_preflight(tmp_path):
    root = tmp_path / "files"
    root.mkdir()
    report = preflight_managed_datasets(root, _manifest([]))
    assert report.candidates == ()
    assert report.file_count == report.total_bytes == 0
    assert not report.t1.candidate_requirements_met
    assert report.public_dict()["scan"]["file_count"] == 0
