"""Adversarial coverage for the trusted EEG qualification view adapters."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import zipfile
from pathlib import Path

import numpy as np
import pytest
from scipy.io import savemat

from orchestrator import qualification_data as qd


SECRET = b"qualification-test-secret-32-bytes!!"


def _mat_bytes(payload) -> bytes:
    out = io.BytesIO()
    savemat(out, payload)
    return out.getvalue()


def _seed_zip(path: Path, *, omit=None, bad_shape=None, missing_trial=None,
              extra_name=None, duplicate_name=None, label_values=None) -> Path:
    if label_values is None:
        label_values = [-1, 0, 1] * 5
    labels = np.asarray([label_values])
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("ExtractedFeatures/label.mat", _mat_bytes({"label": labels}))
        for subject in range(1, 16):
            for session in range(1, 4):
                if omit == (subject, session):
                    continue
                payload = {}
                for trial in range(1, 16):
                    if missing_trial == (subject, session, trial):
                        continue
                    shape = ((61, 1, 5) if bad_shape == (subject, session, trial)
                             else (62, 1, 5))
                    payload[f"de_LDS{trial}"] = np.full(
                        shape, subject * 1000 + session * 100 + trial,
                        dtype=np.float64)
                name = f"ExtractedFeatures/{subject}_{20200100 + session:08d}.mat"
                body = _mat_bytes(payload)
                archive.writestr(name, body)
                if duplicate_name == (subject, session):
                    archive.writestr(name, body)
        archive.writestr("ExtractedFeatures/readme.txt", b"official sidecar\n")
        if extra_name is not None:
            archive.writestr(extra_name, b"x")
    return path


def _dreamer_payload(*, subjects=23, records=18, bad_eeg=None):
    data = []
    for subject in range(1, subjects + 1):
        baselines = []
        stimuli = []
        for record in range(1, records + 1):
            channels = 13 if bad_eeg == (subject, record) else 14
            baselines.append(np.full((2, channels), subject + record / 100, dtype=np.float64))
            stimuli.append(np.full((3, channels), subject + record / 10, dtype=np.float64))
        baseline_cells = np.empty(records, dtype=object)
        stimulus_cells = np.empty(records, dtype=object)
        baseline_cells[:] = baselines
        stimulus_cells[:] = stimuli
        scores = np.asarray([
            ((subject - 1 + record - 1) % 5) + 1
            for record in range(1, records + 1)
        ], dtype=np.float64).reshape(-1, 1)
        data.append({
            "EEG": {"baseline": baseline_cells, "stimuli": stimulus_cells},
            "ScoreValence": scores,
            "ScoreArousal": 6 - scores,
            "ScoreDominance": scores,
        })
    return {"DREAMER": {
        "Data": data,
        "EEG_SamplingRate": 128,
        "EEG_Electrodes": np.asarray(qd._DREAMER_ELECTRODES, dtype=object),
        "noOfSubjects": subjects,
        "noOfVideoSequences": records,
    }}


def _dreamer_zip(path: Path, **payload_kwargs) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("DREAMER.mat", _mat_bytes(_dreamer_payload(**payload_kwargs)))
        archive.writestr("DREAMER.pdf", b"official sidecar")
    return path


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def _canonical(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n").encode()


def _assert_view_receipt(root: Path, *, task, role, dataset, fold, adapter):
    path = root / qd.VIEW_RECEIPT_NAME
    value = json.loads(path.read_text())
    assert set(value) == {
        "version", "protocol", "task", "role", "dataset", "fold",
        "adapter", "adapter_version", "files"}
    assert value | {} == {
        "version": 1,
        "protocol": qd.VIEW_PROTOCOL,
        "task": task,
        "role": role,
        "dataset": dataset,
        "fold": fold,
        "adapter": adapter,
        "adapter_version": 1,
        "files": value["files"],
    }
    expected = []
    for current, dirs, files in os.walk(root):
        dirs.sort()
        files.sort()
        for name in files:
            item = Path(current) / name
            relative = item.relative_to(root).as_posix()
            if relative == qd.VIEW_RECEIPT_NAME:
                continue
            raw = item.read_bytes()
            expected.append({
                "path": relative,
                "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw),
            })
    assert value["files"] == expected
    assert path.read_bytes() == _canonical(value)
    assert _mode(path) == 0o444
    return value


def test_prepare_seed_views_separates_truth_shuffles_and_hardlinks(tmp_path):
    archive = _seed_zip(tmp_path / "SEED.zip")
    public, sealed = tmp_path / "public", tmp_path / "sealed"
    receipt = qd.prepare_seed_views(archive, public, sealed, SECRET)

    assert receipt["adapter"] == qd.SEED_ADAPTER
    assert receipt["input_sha256"] == "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest()
    assert sorted(path.name for path in public.glob("fold-*")) == [f"fold-{n:02d}" for n in range(1, 16)]
    truth = json.loads((sealed / "truth.json").read_text())
    assert set(truth) == {"version", "task", "classes", "folds"}
    assert truth["version"] == 1 and truth["task"] == "T2" and truth["classes"] == 3
    truth_by_fold = {item["fold"]: item for item in truth["folds"]}
    assert set(truth_by_fold) == set(range(1, 16))
    for held_out in range(1, 16):
        fold = public / f"fold-{held_out:02d}"
        sources = sorted((fold / "source").iterdir())
        assert len(sources) == 14
        assert f"subject-{held_out:02d}" not in {path.name for path in sources}
        assert (fold / "target" / "x.npy").is_file()
        assert (fold / "target" / "sample_ids.json").is_file()
        assert not (fold / "target" / "y.npy").exists()
        protocol = json.loads((fold / "protocol.json").read_text())
        assert set(protocol) == {
            "adapter", "adapter_version", "profile", "feature", "dtype",
            "sample_shape", "classes", "source_subjects", "target_file",
            "target_sample_ids_file"}
        lowered = (fold / "protocol.json").read_text().lower()
        assert not any(word in lowered for word in ("trial", "stimulus", "order", "session"))
        target_x = np.load(fold / "target" / "x.npy", allow_pickle=False)
        sample_manifest = json.loads(
            (fold / "target" / "sample_ids.json").read_text())
        assert set(sample_manifest) == {"version", "fold", "sample_ids"}
        assert sample_manifest["version"] == 1 and sample_manifest["fold"] == held_out
        target_ids = sample_manifest["sample_ids"]
        target_y = truth_by_fold[held_out]["labels"]
        assert target_x.shape == (45, 62, 5) and target_x.dtype == np.float32
        assert len(target_ids) == len(target_y) == 45
        assert target_ids == truth_by_fold[held_out]["sample_ids"]
        assert len(set(target_ids)) == 45
        assert all(re.fullmatch(r"[0-9a-f]{64}", value) for value in target_ids)
        assert set(target_y) == {0, 1, 2}
        _assert_view_receipt(
            fold, task="T2", role="fold", dataset="SEED",
            fold=held_out, adapter=qd.SEED_ADAPTER)

    # subject-02 has one byte identity across its target and fourteen source views.
    source_x = public / "fold-01" / "source" / "subject-02" / "x.npy"
    target_x = public / "fold-02" / "target" / "x.npy"
    assert os.stat(source_x).st_ino == os.stat(target_x).st_ino
    assert os.stat(source_x).st_nlink == 15
    source_y_a = public / "fold-01" / "source" / "subject-02" / "y.npy"
    source_y_b = public / "fold-03" / "source" / "subject-02" / "y.npy"
    assert os.stat(source_y_a).st_ino == os.stat(source_y_b).st_ino

    assert _mode(public) == 0o555 and not (public / "receipt.json").exists()
    assert _mode(sealed) == 0o711 and _mode(sealed / "receipt.json") == 0o400
    assert _mode(sealed / "truth.json") == 0o400
    assert os.lstat(public).st_uid == os.geteuid()
    assert os.lstat(sealed / "truth.json").st_uid == os.geteuid()
    assert (sealed / "truth.json").read_bytes() == _canonical(truth)
    assert (sealed / "receipt.json").read_bytes() == _canonical(receipt)
    assert all(row["path"] != "receipt.json" for row in receipt["public"]["files"])


def test_seed_shuffle_is_full_subject_hmac_order_and_secret_sensitive():
    labels = np.asarray([0, 1, 2] * 5, dtype=np.uint8)
    sessions = []
    for session in range(1, 4):
        sessions.append({
            f"de_LDS{trial}": np.full((62, 2, 5), session * 100 + trial * 10 + time,
                                       dtype=np.float32)
            for trial in range(1, 16)
            for time in [0]
        })
        # Encode the two time positions distinctly after the comprehension.
        for trial in range(1, 16):
            sessions[-1][f"de_LDS{trial}"][:, 1, :] += 1
    x, y, sample_ids = qd._seed_subject_arrays(1, sessions, labels, SECRET)
    keys = []
    values = []
    expected_y = []
    for session in range(1, 4):
        for trial in range(1, 16):
            for time_index in range(2):
                keys.append(qd._seed_order_key(SECRET, 1, session, trial, time_index))
                values.append(session * 100 + trial * 10 + time_index)
                expected_y.append(labels[trial - 1])
    order = sorted(range(len(keys)), key=keys.__getitem__)
    assert x[:, 0, 0].tolist() == [values[index] for index in order]
    assert y.tolist() == [int(expected_y[index]) for index in order]
    expected_ids = [
        qd._seed_sample_id(SECRET, 1, session, trial, time_index)
        for session in range(1, 4)
        for trial in range(1, 16)
        for time_index in range(2)
    ]
    assert sample_ids == [expected_ids[index] for index in order]
    x2, _, ids2 = qd._seed_subject_arrays(1, sessions, labels, b"Z" * 32)
    assert not np.array_equal(x, x2)
    assert sample_ids != ids2


@pytest.mark.parametrize("mutation,match", [
    ({"omit": (15, 3)}, "15×3|3 个"),
    ({"bad_shape": (1, 1, 1)}, "62×T×5"),
    ({"missing_trial": (1, 1, 15)}, "trial 闭包"),
    ({"label_values": [255, 0, 1] * 5}, "-1/0/1"),
    ({"extra_name": "../escape.mat"}, "路径穿越|canonical"),
    ({"extra_name": "ExtractedFeatures/evil.bin"}, "未知外围文件"),
])
def test_seed_rejects_archive_drift_without_publishing(tmp_path, mutation, match):
    archive = _seed_zip(tmp_path / "bad.zip", **mutation)
    public, sealed = tmp_path / "public", tmp_path / "sealed"
    with pytest.raises(qd.QualificationDataError, match=match):
        qd.prepare_seed_views(archive, public, sealed, SECRET)
    assert not public.exists() and not sealed.exists()
    assert not list(tmp_path.glob(".*.staging-*"))


def test_seed_rejects_short_secret_and_existing_target(tmp_path):
    archive = _seed_zip(tmp_path / "SEED.zip")
    with pytest.raises(qd.QualificationDataError, match="secret"):
        qd.prepare_seed_views(archive, tmp_path / "p", tmp_path / "s", b"short")
    with pytest.raises(qd.QualificationDataError, match="UID"):
        qd.prepare_seed_views(
            archive, tmp_path / "p", tmp_path / "s", SECRET,
            research_uid=True, evaluator_uid=-1)
    (tmp_path / "p").mkdir()
    with pytest.raises(qd.QualificationDataError, match="已存在"):
        qd.prepare_seed_views(archive, tmp_path / "p", tmp_path / "s", SECRET)


def test_prepare_dreamer_view_has_only_safe_unlabeled_public_records(tmp_path):
    archive = _dreamer_zip(tmp_path / "DREAMER.zip")
    public, sealed = tmp_path / "public", tmp_path / "sealed"
    rule = {
        "score": "valence", "threshold": 3,
        "comparison": "higher_is_positive", "neutral_policy": "drop"}
    receipt = qd.prepare_dreamer_view(archive, public, sealed, SECRET, rule)

    manifest = json.loads((public / "manifest.json").read_text())
    assert set(manifest) == {
        "adapter", "adapter_version", "profile", "record_format", "record_count",
        "arrays", "sampling_rate_hz", "electrodes", "label_rule",
        "sample_ids", "records"}
    assert manifest["label_rule"] == {
        "score": "valence", "threshold": 3.0,
        "comparison": "higher_is_positive", "neutral_policy": "drop"}
    assert 0 < manifest["record_count"] < 23 * 18
    assert manifest["records"] == sorted(manifest["records"])
    assert len(set(manifest["records"])) == manifest["record_count"]
    assert [name.removesuffix(".npz") for name in manifest["records"]] == manifest["sample_ids"]
    assert all(re.fullmatch(r"[0-9a-f]{64}\.npz", name) for name in manifest["records"])
    first = public / "records" / manifest["records"][0]
    with np.load(first, allow_pickle=False) as record:
        assert set(record.files) == {"baseline", "stimuli", "sampling_rate", "electrodes"}
        assert record["baseline"].dtype == np.float32
        assert record["stimuli"].dtype == np.float32
        assert not any(record[name].dtype.hasobject for name in record.files)
        assert record["sampling_rate"].tolist() == [128]
        assert len(record["electrodes"]) == 14

    truth = json.loads((sealed / "truth.json").read_text())
    assert set(truth) == {"version", "task", "classes", "label_rule", "units"}
    assert truth["label_rule"] == manifest["label_rule"]
    assert truth["version"] == 1 and truth["task"] == "T1" and truth["classes"] == 2
    assert len(truth["units"]) == 1
    unit = truth["units"][0]
    assert set(unit) == {"unit_id", "sample_ids", "labels", "groups"}
    assert unit["unit_id"] == "dreamer"
    assert unit["sample_ids"] == manifest["sample_ids"]
    assert len(unit["sample_ids"]) == len(unit["labels"]) == len(unit["groups"])
    assert set(unit["labels"]) == {0, 1}
    assert set(unit["groups"]) == set(range(1, 24))
    assert not (public / "labels.npy").exists()
    assert not (public / "groups.npy").exists()
    assert not (sealed / "opaque_ids.npy").exists()
    assert (_mode(first), _mode(sealed), _mode(sealed / "truth.json")) == (
        0o444, 0o711, 0o400)
    assert os.lstat(public).st_uid == os.geteuid()
    assert os.lstat(sealed / "truth.json").st_uid == os.geteuid()
    assert (sealed / "truth.json").read_bytes() == _canonical(truth)
    _assert_view_receipt(
        public, task="T1", role="sealed_holdout", dataset="DREAMER",
        fold=None, adapter=qd.DREAMER_ADAPTER)
    assert (sealed / "receipt.json").read_bytes() == _canonical(receipt)


@pytest.mark.skipif(os.geteuid() != 0, reason="cross-UID chown requires root")
def test_operator_can_split_research_and_evaluator_ownership(tmp_path):
    tmp_path.parent.chmod(0o755)
    tmp_path.chmod(0o755)
    archive = _dreamer_zip(tmp_path / "DREAMER.zip")
    public, sealed = tmp_path / "public", tmp_path / "sealed"
    qd.prepare_dreamer_view(
        archive, public, sealed, SECRET, {
            "score": "valence", "threshold": 3,
            "comparison": "higher_is_positive", "neutral_policy": "drop",
        }, research_uid=65534, evaluator_uid=0)

    assert os.lstat(public).st_uid == 65534
    assert os.lstat(public / qd.VIEW_RECEIPT_NAME).st_uid == 65534
    assert os.lstat(next((public / "records").iterdir())).st_uid == 65534
    assert os.lstat(sealed).st_uid == 0
    assert os.lstat(sealed / "truth.json").st_uid == 0
    assert os.lstat(sealed / "receipt.json").st_uid == 0

    child = os.fork()
    if child == 0:  # pragma: no cover - assertion is the child's exit status
        try:
            os.setgid(65534)
            os.setuid(65534)
            (public / "manifest.json").read_bytes()
            try:
                (sealed / "truth.json").read_bytes()
            except PermissionError:
                os._exit(0)
            os._exit(2)
        except BaseException:
            os._exit(3)
    _pid, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 0


@pytest.mark.skipif(os.geteuid() != 0, reason="parent-owner boundary requires root")
def test_prepare_rejects_research_owned_publication_parent(tmp_path):
    archive = _dreamer_zip(tmp_path / "DREAMER.zip")
    untrusted_parent = tmp_path / "research-owned"
    untrusted_parent.mkdir(mode=0o755)
    os.chown(untrusted_parent, 65534, 65534)
    with pytest.raises(qd.QualificationDataError, match="parent"):
        qd.prepare_dreamer_view(
            archive, untrusted_parent / "public", untrusted_parent / "sealed",
            SECRET, {
                "score": "valence", "threshold": 3,
                "comparison": "higher_is_positive", "neutral_policy": "drop",
            }, research_uid=65534, evaluator_uid=0)


@pytest.mark.skipif(os.geteuid() != 0, reason="ancestor-owner boundary requires root")
def test_prepare_rejects_untrusted_publication_ancestor(tmp_path):
    archive = _dreamer_zip(tmp_path / "DREAMER.zip")
    untrusted_ancestor = tmp_path / "research-owned-ancestor"
    untrusted_ancestor.mkdir(mode=0o755)
    trusted_parent = untrusted_ancestor / "operator-parent"
    trusted_parent.mkdir(mode=0o755)
    os.chown(untrusted_ancestor, 65534, 65534)
    with pytest.raises(qd.QualificationDataError, match="ancestor"):
        qd.prepare_dreamer_view(
            archive, trusted_parent / "public", trusted_parent / "sealed",
            SECRET, {
                "score": "valence", "threshold": 3,
                "comparison": "higher_is_positive", "neutral_policy": "drop",
            }, research_uid=65534, evaluator_uid=0)


@pytest.mark.skipif(os.geteuid() != 0, reason="production prepare CLI requires root")
def test_cli_reads_secret_and_rule_from_nofollow_bounded_files(tmp_path, capfd):
    tmp_path.parent.chmod(0o755)
    tmp_path.chmod(0o755)
    archive = _dreamer_zip(tmp_path / "DREAMER.zip")
    secret_file = tmp_path / "secret.bin"
    secret_file.write_bytes(SECRET)
    secret_file.chmod(0o600)
    rule_file = tmp_path / "rule.json"
    rule_file.write_bytes(_canonical({
        "score": "valence", "threshold": 3,
        "comparison": "higher_is_positive", "neutral_policy": "drop",
    }))
    public, sealed = tmp_path / "public", tmp_path / "sealed"
    argv = [
        "prepare-dreamer",
        "--archive", str(archive),
        "--public-root", str(public),
        "--sealed-root", str(sealed),
        "--secret-file", str(secret_file),
        "--research-uid", "65534",
        "--evaluator-uid", "0",
        "--label-rule", str(rule_file),
    ]
    assert SECRET.decode("ascii") not in argv
    assert qd._main(argv) == 0
    stdout = capfd.readouterr().out.encode()
    assert stdout == _canonical(json.loads(stdout))
    assert (public / qd.VIEW_RECEIPT_NAME).is_file()
    assert (sealed / "truth.json").is_file()

    secret_file.chmod(0o644)
    with pytest.raises(qd.QualificationDataError, match="bytes|常规文件"):
        qd._read_bounded_file(
            secret_file, label="secret file", maximum_bytes=qd._MAX_SECRET_BYTES,
            expected_owner=0, allowed_modes=frozenset({0o400, 0o600}))
    secret_file.chmod(0o600)

    linked = tmp_path / "linked-secret"
    linked.symlink_to(secret_file)
    with pytest.raises(qd.QualificationDataError, match="安全打开"):
        qd._read_bounded_file(
            linked, label="secret file", maximum_bytes=qd._MAX_SECRET_BYTES)
    oversized = tmp_path / "oversized-secret"
    oversized.write_bytes(b"x" * (qd._MAX_SECRET_BYTES + 1))
    with pytest.raises(qd.QualificationDataError, match="bytes"):
        qd._read_bounded_file(
            oversized, label="secret file", maximum_bytes=qd._MAX_SECRET_BYTES)


@pytest.mark.parametrize("rule", [
    {"score": "valence", "threshold": 3, "comparison": "higher_is_positive"},
    {"score": "unknown", "threshold": 3, "comparison": "higher_is_positive", "neutral_policy": "drop"},
    {"score": "valence", "threshold": float("nan"), "comparison": "higher_is_positive", "neutral_policy": "drop"},
    {"score": "valence", "threshold": 3, "comparison": ">", "neutral_policy": "drop"},
    {"score": "valence", "threshold": 3, "comparison": "higher_is_positive", "neutral_policy": "maybe"},
])
def test_dreamer_label_rule_is_explicit_and_closed(tmp_path, rule):
    archive = _dreamer_zip(tmp_path / "DREAMER.zip")
    with pytest.raises(qd.QualificationDataError, match="label_rule"):
        qd.prepare_dreamer_view(archive, tmp_path / "p", tmp_path / "s", SECRET, rule)


@pytest.mark.parametrize("kwargs,match", [
    ({"subjects": 22}, "23 subjects|Data"),
    ({"records": 17}, "18 records|18 条"),
    ({"bad_eeg": (1, 1)}, "samples×14"),
])
def test_dreamer_rejects_count_and_shape_drift_atomically(tmp_path, kwargs, match):
    archive = _dreamer_zip(tmp_path / "bad.zip", **kwargs)
    with pytest.raises(qd.QualificationDataError, match=match):
        qd.prepare_dreamer_view(
            archive, tmp_path / "p", tmp_path / "s", SECRET, {
                "score": "valence", "threshold": 3,
                "comparison": "higher_is_positive", "neutral_policy": "negative"})
    assert not (tmp_path / "p").exists() and not (tmp_path / "s").exists()


def test_dreamer_zip_path_traversal_and_existing_target_are_rejected(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as value:
        value.writestr("../DREAMER.mat", b"x")
    with pytest.raises(qd.QualificationDataError, match="路径穿越|canonical"):
        qd.prepare_dreamer_view(
            archive, tmp_path / "p", tmp_path / "s", SECRET, {
                "score": "valence", "threshold": 3,
                "comparison": "higher_is_positive", "neutral_policy": "drop"})

    good = _dreamer_zip(tmp_path / "good.zip")
    (tmp_path / "p").mkdir()
    with pytest.raises(qd.QualificationDataError, match="已存在"):
        qd.prepare_dreamer_view(
            good, tmp_path / "p", tmp_path / "s", SECRET, {
                "score": "valence", "threshold": 3,
                "comparison": "higher_is_positive", "neutral_policy": "drop"})
