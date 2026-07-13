"""CP11.4c.3c.3 · canonical evidence pack + offline resume proof."""
from __future__ import annotations

import json
import os
import hashlib
import shutil
from pathlib import Path

import pytest

from orchestrator.evidence_pack import (
    EvidencePackError,
    create_evidence_pack,
    main,
    verify_evidence_pack,
)
import orchestrator.evidence_pack as evidence_pack_module
from orchestrator.interfaces import Artifact, CallUsage
from orchestrator.import_materialization_contract import spec_ref
from orchestrator import database as db
from orchestrator.run import build_system
from orchestrator.shared_fs_canary import run_local_canary
from orchestrator.storage_governance import CycleSnapshotPublisher
from orchestrator.storage_ops import main as storage_main
import conftest


SYSTEM_ROOT = str(Path(__file__).resolve().parent.parent)
BOOT = {
    "tree_ops.json": {
        "ops": [{"op": "create_root", "text": "恢复探针根问题", "local_key": "root"}]},
    "selection.json": {
        "next_question_id": "root", "next_intent": "decompose",
        "scores": [{"question_id": "root", "score": 0.8, "est_cost": 1.0}],
    },
}
FINISH = {
    "tree_ops.json": {"ops": [{
        "op": "add_children", "parent_question_id": "q1",
        "children": [{"local_key": "child", "text": "恢复后新增子问题"}],
    }]},
    "selection.json": {
        "next_question_id": None, "next_intent": "terminate",
        "scores": [{"question_id": "child", "score": 0.2, "est_cost": 1.0}],
        "terminate_reason_md": "单轮恢复探针完成",
    },
}
CONTINUE = {
    "tree_ops.json": {"ops": [{
        "op": "add_children", "parent_question_id": "q1",
        "children": [{"local_key": "child", "text": "第一轮恢复后继续"}],
    }]},
    "selection.json": {
        "next_question_id": "child", "next_intent": "decompose",
        "scores": [{"question_id": "child", "score": 0.7, "est_cost": 1.0}],
    },
}
FINISH_SECOND = {
    "tree_ops.json": {"ops": [{
        "op": "add_children", "parent_question_id": "q2",
        "children": [{"local_key": "grandchild", "text": "第二轮恢复后结束"}],
    }]},
    "selection.json": {
        "next_question_id": None, "next_intent": "terminate",
        "scores": [{"question_id": "grandchild", "score": 0.2, "est_cost": 1.0}],
        "terminate_reason_md": "两轮恢复探针完成",
    },
}


def _factory(values):
    queue = list(values)

    class Runner:
        def run_task(self, *, system_prompt, skill, context_pack):
            return Artifact(
                stage=context_pack.stage, files=queue.pop(0), md="",
                usage=CallUsage(tokens_known=True))

    return lambda _transcripts, _purpose: Runner()


def _source_and_restored(
        tmp_path: Path, *, advance: bool = True,
        mutate_before_start: bool = False):
    source = tmp_path / "source"
    first = build_system(
        SYSTEM_ROOT, str(source), runner_factory=_factory([BOOT]), attack=False)
    try:
        assert first.run(1) == ["c1"]
    finally:
        assert first.close() is None

    target = tmp_path / "clean-target"
    assert storage_main([
        "--work-root", str(source),
        "restore-with-import-materializations", "--target", str(target),
    ]) == 0
    if mutate_before_start:
        changed = db.connect(target / "research.sqlite")
        changed.execute("UPDATE question SET text='restore 后带外篡改' WHERE id=1")
        changed.commit()
        changed.close()
    if advance:
        resumed = build_system(
            SYSTEM_ROOT, str(target), runner_factory=_factory([FINISH]), attack=False)
        try:
            assert resumed.run(1) == ["c2"]
        finally:
            assert resumed.close() is None
    return source, target


def _pack(tmp_path: Path):
    source, target = _source_and_restored(tmp_path)
    output = tmp_path / "packs"
    output.mkdir(mode=0o700)
    result = create_evidence_pack(
        source_work_root=source, resume_work_root=target,
        output_parent=output)
    return source, target, Path(result["pack_path"]), result


def _rewrite_pack_manifest(pack: Path, manifest: dict) -> Path:
    raw = (json.dumps(
        manifest, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False) + "\n").encode()
    digest = hashlib.sha256(raw).hexdigest()
    manifest_path = pack / "manifest.json"
    ready_path = pack / "READY.json"
    manifest_path.chmod(0o600)
    manifest_path.write_bytes(raw)
    manifest_path.chmod(0o400)
    ready_path.chmod(0o600)
    ready_path.write_text(json.dumps({
        "version": 1,
        "protocol": "meta-research-evidence-pack-ready/v1",
        "manifest_sha256": digest,
    }, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    ready_path.chmod(0o400)
    destination = pack.parent / f"{digest}.evidence"
    pack.rename(destination)
    return destination


def _replace_json_item(pack: Path, manifest: dict, logical_id: str, value: dict):
    raw = (json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False) + "\n").encode()
    digest = hashlib.sha256(raw).hexdigest()
    item = next(
        candidate for candidate in manifest["items"]
        if candidate["logical_id"] == logical_id)
    old_digest = item["sha256"]
    destination = pack / "objects" / "sha256" / digest
    if not destination.exists():
        destination.write_bytes(raw)
        destination.chmod(0o400)
    item.update({"sha256": digest, "bytes": len(raw)})
    if (old_digest != digest
            and not any(candidate["sha256"] == old_digest
                        for candidate in manifest["items"])):
        (pack / "objects" / "sha256" / old_digest).unlink()
    return digest


def test_pack_proves_exact_one_cycle_and_verifies_without_original_roots(tmp_path):
    source, target, pack, result = _pack(tmp_path)
    assert result["status"] == "verified"
    assert result["pack_integrity_verified"] is True
    assert result["one_cycle_resume_probe_verified"] is True
    assert result["real_codex_resume_verified"] is False
    assert result["full_restore_verified"] is False
    assert result["qualification_receipts_verified"] is False
    assert pack.name == result["manifest_sha256"] + ".evidence"

    manifest = json.loads((pack / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest) == {
        "version", "protocol", "source_work_root", "resume_probe", "items"}
    assert manifest["resume_probe"] == {
        "protocol": "meta-research-one-cycle-resume-probe/v1"}
    assert result["unresolved_registered_assets"] == 0

    # Offline means the verifier must not reopen absolute source/target paths
    # retained as diagnostic provenance in restore receipts.
    source.rename(tmp_path / "source-offline")
    target.rename(tmp_path / "target-offline")
    again = verify_evidence_pack(pack)
    assert again["manifest_sha256"] == result["manifest_sha256"]
    assert again["one_cycle_resume_probe_verified"] is True


def test_same_evidence_is_content_addressed_and_idempotent(tmp_path):
    source, target, pack, first = _pack(tmp_path)
    assert first["reused"] is False
    second = create_evidence_pack(
        source_work_root=source, resume_work_root=target,
        output_parent=pack.parent)
    assert second["pack_path"] == str(pack)
    assert second["manifest_sha256"] == first["manifest_sha256"]
    assert second["reused"] is True
    assert len(list(pack.parent.glob("*.evidence"))) == 1
    assert not list(pack.parent.glob(".evidence-pack-*"))


def test_restore_exit_boundary_without_new_done_cycle_is_not_a_probe(tmp_path):
    source, target = _source_and_restored(tmp_path, advance=False)
    output = tmp_path / "packs"
    output.mkdir(mode=0o700)
    with pytest.raises((EvidencePackError, RuntimeError), match="snapshot|resume|布局"):
        create_evidence_pack(
            source_work_root=source, resume_work_root=target,
            output_parent=output)
    assert not list(output.iterdir())


def test_two_post_restore_cycles_cannot_masquerade_as_exact_one_cycle(tmp_path):
    source, target = _source_and_restored(tmp_path, advance=False)
    resumed = build_system(
        SYSTEM_ROOT, str(target),
        runner_factory=_factory([CONTINUE, FINISH_SECOND]), attack=False)
    try:
        assert resumed.run(2) == ["c2", "c3"]
    finally:
        assert resumed.close() is None
    output = tmp_path / "packs"
    output.mkdir(mode=0o700)
    with pytest.raises(EvidencePackError, match="精确新增一轮"):
        create_evidence_pack(
            source_work_root=source, resume_work_root=target,
            output_parent=output)
    assert not list(output.iterdir())


def test_manually_published_done_row_without_runner_ledger_is_not_resume(tmp_path):
    source, target = _source_and_restored(tmp_path, advance=False)
    publisher = CycleSnapshotPublisher(
        db_path=target / "research.sqlite", work_root=target)
    assert publisher.reconcile(startup=True) == ["c1"]
    connection = db.connect(target / "research.sqlite")
    connection.execute(
        "INSERT INTO cycle(id,goal_id,goal_ver,route,status,policy_version,finished_at) "
        "VALUES (2,1,1,'decompose','done','v0','2026-07-12T00:00:00Z')")
    connection.commit()
    connection.close()
    assert publisher.reconcile() == ["c2"]
    output = tmp_path / "packs"
    output.mkdir(mode=0o700)
    with pytest.raises(EvidencePackError, match="runner_call"):
        create_evidence_pack(
            source_work_root=source, resume_work_root=target,
            output_parent=output)


def test_qualification_source_cannot_silently_resume_as_ordinary_mode(tmp_path):
    source, target = _source_and_restored(tmp_path)
    qualification = source / "state" / "qualification"
    qualification.mkdir(mode=0o700)
    (qualification / "contract.json").write_text("{}\n", encoding="utf-8")
    (qualification / "contract.json").chmod(0o400)
    output = tmp_path / "packs"
    output.mkdir(mode=0o700)
    with pytest.raises(EvidencePackError, match="qualification"):
        create_evidence_pack(
            source_work_root=source, resume_work_root=target,
            output_parent=output)


def test_receipt_only_qualification_and_fault_honesty_are_packaged(
        tmp_path, monkeypatch):
    source = tmp_path / "source"
    system = build_system(
        SYSTEM_ROOT, str(source), runner_factory=_factory([BOOT]), attack=False)
    try:
        assert system.run(1) == ["c1"]
    finally:
        assert system.close() is None

    qualification = source / "state" / "qualification"
    qualification.mkdir(mode=0o700)
    contract = {"version": 1, "protocol": "test-qualification-receipt/v1"}
    (qualification / "contract.json").write_text(
        json.dumps(contract, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8")
    (qualification / "contract.json").chmod(0o400)

    schedule_id = "0123456789abcdef0123456789abcdef"
    schedule_root = source / "state" / "fault-schedules" / schedule_id
    schedule_root.mkdir(parents=True, mode=0o700)
    final = {
        "status": "complete", "signal_exactly_once": False,
        "recovery_verified": False,
    }
    for name, value in (("schedule.json", {"version": 1}), ("final.json", final)):
        path = schedule_root / name
        path.write_text(
            json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8")
        path.chmod(0o400)

    class Firewall:
        task = "T1"

    monkeypatch.setattr(
        evidence_pack_module, "load_qualification_firewall",
        lambda *_args, **_kwargs: Firewall())
    monkeypatch.setattr(
        evidence_pack_module, "verify_fault_schedule", lambda _path: final)
    output = tmp_path / "packs"
    output.mkdir(mode=0o700)
    result = create_evidence_pack(source_work_root=source, output_parent=output)
    verified = verify_evidence_pack(result["pack_path"])
    assert verified["qualification_receipts_verified"] is False
    manifest = json.loads(
        (Path(result["pack_path"]) / "manifest.json").read_text(encoding="utf-8"))
    logical_ids = {value["logical_id"] for value in manifest["items"]}
    assert "source/qualification/verify.json" in logical_ids
    assert f"source/fault-schedules/{schedule_id}/verify.json" in logical_ids


def test_pre_start_restored_db_mutation_cannot_masquerade_as_resume(tmp_path):
    source, target = _source_and_restored(tmp_path, mutate_before_start=True)
    output = tmp_path / "packs"
    output.mkdir(mode=0o700)
    with pytest.raises(EvidencePackError, match="adoption/post-cycle"):
        create_evidence_pack(
            source_work_root=source, resume_work_root=target,
            output_parent=output)


def test_object_tamper_extra_and_symlink_are_rejected(tmp_path):
    _source, _target, pack, _result = _pack(tmp_path)
    objects = pack / "objects" / "sha256"
    first = sorted(objects.iterdir())[0]
    original = first.read_bytes()
    first.chmod(0o600)
    first.write_bytes(original + b"x")
    first.chmod(0o400)
    with pytest.raises(EvidencePackError, match="hash/bytes|authority/bytes|大小"):
        verify_evidence_pack(pack)

    first.chmod(0o600)
    first.write_bytes(original)
    first.chmod(0o400)
    extra = objects / ("f" * 64)
    extra.write_bytes(b"extra")
    extra.chmod(0o400)
    with pytest.raises(EvidencePackError, match="缺失/多余"):
        verify_evidence_pack(pack)
    extra.unlink()

    first.chmod(0o600)
    with pytest.raises(EvidencePackError, match="authority/bytes"):
        verify_evidence_pack(pack)
    first.chmod(0o400)

    other = sorted(objects.iterdir())[1]
    first.unlink()
    os.link(other, first)
    with pytest.raises(EvidencePackError, match="authority/bytes"):
        verify_evidence_pack(pack)
    first.unlink()
    first.symlink_to(other.name)
    with pytest.raises(EvidencePackError, match="authority|不可安全打开"):
        verify_evidence_pack(pack)


def test_manifest_ready_and_unknown_root_entries_fail_closed(tmp_path):
    _source, _target, pack, _result = _pack(tmp_path)
    ready = pack / "READY.json"
    ready.chmod(0o600)
    value = json.loads(ready.read_text(encoding="utf-8"))
    value["manifest_sha256"] = "0" * 64
    ready.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8")
    ready.chmod(0o400)
    with pytest.raises(EvidencePackError, match="绑定"):
        verify_evidence_pack(pack)

    # Restore READY, then prove an unlisted file is not silently ignored.
    value["manifest_sha256"] = pack.name.removesuffix(".evidence")
    ready.chmod(0o600)
    ready.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8")
    ready.chmod(0o400)
    unknown = pack / "extra"
    unknown.write_text("x", encoding="utf-8")
    unknown.chmod(0o400)
    with pytest.raises(EvidencePackError, match="根目录闭包"):
        verify_evidence_pack(pack)


def test_verifier_rejects_control_file_drift_during_same_call(tmp_path, monkeypatch):
    _source, _target, pack, _result = _pack(tmp_path)
    original = evidence_pack_module._verify_pack_file  # noqa: SLF001
    first = True

    def mutate_after_first_verification(*args, **kwargs):
        nonlocal first
        identity = original(*args, **kwargs)
        if first:
            first = False
            manifest_path = pack / "manifest.json"
            manifest_path.chmod(0o600)
            manifest_path.write_bytes(manifest_path.read_bytes() + b" ")
            manifest_path.chmod(0o400)
        return identity

    monkeypatch.setattr(
        evidence_pack_module, "_verify_pack_file", mutate_after_first_verification)
    with pytest.raises(EvidencePackError, match="验证期间文件身份漂移"):
        verify_evidence_pack(pack)


def test_verifier_pins_root_across_transient_parent_swap(tmp_path, monkeypatch):
    _source, _target, pack, _result = _pack(tmp_path)
    parent = pack.parent
    malicious_parent = tmp_path / "malicious-packs"
    shutil.copytree(parent, malicious_parent)
    malicious_pack = malicious_parent / pack.name
    manifest = json.loads(
        (malicious_pack / "manifest.json").read_text(encoding="utf-8"))
    report_item = next(
        value for value in manifest["items"]
        if value["logical_id"] == "source/log-mirrors/verify.json")
    report_path = (
        malicious_pack / "objects" / "sha256" / report_item["sha256"])
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report.pop("orphan_mirror_objects")
    report_path.chmod(0o600)
    report_path.write_text(
        json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8")
    report_path.chmod(0o400)

    original = evidence_pack_module._packed_json  # noqa: SLF001
    swapped = False
    observed_original = False

    def swap_parent_for_one_semantic_read(
            pack_arg, items, logical_id, *, kind, label):
        nonlocal swapped, observed_original
        if logical_id != "source/log-mirrors/verify.json" or swapped:
            return original(
                pack_arg, items, logical_id, kind=kind, label=label)
        swapped = True
        aside = tmp_path / "packs-aside"
        parent.rename(aside)
        malicious_parent.rename(parent)
        try:
            value = original(
                pack_arg, items, logical_id, kind=kind, label=label)
            observed_original = "orphan_mirror_objects" in value
            return value
        finally:
            parent.rename(malicious_parent)
            aside.rename(parent)

    monkeypatch.setattr(
        evidence_pack_module, "_packed_json", swap_parent_for_one_semantic_read)
    verified = verify_evidence_pack(pack)
    assert verified["status"] == "verified"
    assert swapped is True and observed_original is True


def test_unknown_logical_domain_is_rejected_while_inventory_stays_closed(tmp_path):
    _source, _target, pack, _result = _pack(tmp_path)
    manifest = json.loads((pack / "manifest.json").read_text(encoding="utf-8"))
    report = next(
        value for value in manifest["items"]
        if value["logical_id"] == "source/storage/verify.json")
    report["logical_id"] = "source/unknown/verify.json"
    manifest["items"].sort(key=lambda value: value["logical_id"])
    rewritten = _rewrite_pack_manifest(pack, manifest)
    with pytest.raises(EvidencePackError, match="逻辑域"):
        verify_evidence_pack(rewritten)


def test_explicit_capacity_limit_fails_without_publishing_half_pack(tmp_path):
    source, target = _source_and_restored(tmp_path)
    output = tmp_path / "packs"
    output.mkdir(mode=0o700)
    with pytest.raises(EvidencePackError, match="总字节"):
        create_evidence_pack(
            source_work_root=source, resume_work_root=target,
            output_parent=output, max_bytes=1)
    assert not list(output.iterdir())

    with pytest.raises(EvidencePackError, match="文件数"):
        create_evidence_pack(
            source_work_root=source, resume_work_root=target,
            output_parent=output, max_files=1)
    assert not list(output.iterdir())


def test_staged_semantic_failure_never_publishes_final_pack(tmp_path, monkeypatch):
    source, target = _source_and_restored(tmp_path)
    output = tmp_path / "packs"
    output.mkdir(mode=0o700)

    def reject_staging(*_args, **_kwargs):
        raise EvidencePackError("injected staged semantic failure")

    monkeypatch.setattr(evidence_pack_module, "verify_evidence_pack", reject_staging)
    with pytest.raises(EvidencePackError, match="staged semantic"):
        create_evidence_pack(
            source_work_root=source, resume_work_root=target,
            output_parent=output)
    assert not list(output.iterdir())


def test_post_rename_seal_failure_removes_final_pack(tmp_path, monkeypatch):
    source, target = _source_and_restored(tmp_path)
    output = tmp_path / "packs"
    output.mkdir(mode=0o700)
    original = evidence_pack_module._assert_pack_unchanged  # noqa: SLF001

    def fail_only_final_name(pack, identities, **kwargs):
        if str(pack).endswith(".evidence"):
            raise EvidencePackError("injected post-rename seal failure")
        return original(pack, identities, **kwargs)

    monkeypatch.setattr(
        evidence_pack_module, "_assert_pack_unchanged", fail_only_final_name)
    with pytest.raises(EvidencePackError, match="post-rename seal"):
        create_evidence_pack(
            source_work_root=source, resume_work_root=target,
            output_parent=output)
    assert not list(output.iterdir())


def test_resume_import_completion_is_bound_to_source_closure(tmp_path):
    _source, _target, pack, _result = _pack(tmp_path)
    manifest = json.loads((pack / "manifest.json").read_text(encoding="utf-8"))
    logical_id = "resume/import-materializations/storage-restore.json"
    item = next(
        value for value in manifest["items"] if value["logical_id"] == logical_id)
    completion = json.loads(
        (pack / "objects" / "sha256" / item["sha256"]).read_text())
    completion["repository_objects"] = ["sha256:" + "f" * 64]
    _replace_json_item(pack, manifest, logical_id, completion)
    rewritten = _rewrite_pack_manifest(pack, manifest)
    with pytest.raises(EvidencePackError, match="import restore completion 绑定"):
        verify_evidence_pack(rewritten)


def test_cli_verify_and_unsafe_exit_codes(tmp_path, capsys):
    source, target, pack, result = _pack(tmp_path)
    capsys.readouterr()  # discard the existing storage restore CLI receipt
    assert main([
        "pack", "--source-work-root", str(source),
        "--resume-work-root", str(target),
        "--output-parent", str(pack.parent),
    ]) == 0
    packed = json.loads(capsys.readouterr().out)
    assert packed["manifest_sha256"] == result["manifest_sha256"]
    assert packed["reused"] is True
    assert main(["verify", "--pack", str(pack)]) == 0
    stdout = json.loads(capsys.readouterr().out)
    assert stdout["manifest_sha256"] == result["manifest_sha256"]
    assert main(["verify", "--pack", str(pack.parent / "missing.evidence")]) == 3
    stderr = json.loads(capsys.readouterr().err)
    assert stderr["status"] == "unsafe"


def test_pack_rejects_nested_source_and_resume(tmp_path):
    source = tmp_path / "work"
    nested = source / "nested"
    nested.mkdir(parents=True, mode=0o700)
    output = tmp_path / "packs"
    output.mkdir(mode=0o700)
    with pytest.raises(EvidencePackError, match="互不嵌套"):
        create_evidence_pack(
            source_work_root=source, resume_work_root=nested,
            output_parent=output)


def test_output_parent_symlink_ancestor_and_partial_temp_are_rejected(
        tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir(mode=0o700)
    inside = source / "inside"
    inside.mkdir(mode=0o700)
    through_alias = inside / "packs"
    through_alias.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(inside, target_is_directory=True)
    with pytest.raises(EvidencePackError, match="symlink ancestor|authority"):
        create_evidence_pack(
            source_work_root=source, output_parent=alias / "packs")
    assert not list(through_alias.iterdir())

    output = tmp_path / "packs"
    output.mkdir(mode=0o700)
    original_mkdir = Path.mkdir

    # Match any private pack's direct `objects` child without perturbing the
    # already-created source/output directories.
    def fail_private_objects(path, *args, **kwargs):
        if path.name == "objects" and path.parent.name.startswith(".evidence-pack-"):
            raise OSError("injected mkdir failure")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_private_objects)
    with pytest.raises(OSError, match="injected"):
        create_evidence_pack(source_work_root=source, output_parent=output)
    assert not list(output.iterdir())


def test_pack_objects_are_private_regular_single_link(tmp_path):
    _source, _target, pack, _result = _pack(tmp_path)
    for path in (pack / "objects" / "sha256").iterdir():
        info = path.lstat()
        assert stat_is_regular(info.st_mode)
        assert info.st_nlink == 1
        assert info.st_mode & 0o777 == 0o400
        assert info.st_uid == os.geteuid()


def test_log_mirror_bytes_are_verified_offline_without_original_path(tmp_path):
    source = tmp_path / "log-source"
    source.mkdir(mode=0o700)
    connection = db.connect(source / "research.sqlite")
    conftest.seed_minimal(connection)
    raw = b"registered raw log\x00\xff\n"
    logs = source / "registered-logs"
    logs.mkdir(mode=0o700)
    original = logs / "train.log"
    original.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    connection.execute(
        "INSERT INTO execution_log(id,run_id,cycle_id,log_kind,ref,content_hash,bytes) "
        "VALUES (1,1,1,'train',?,?,?)",
        ("registered-logs/../registered-logs/train.log", digest, len(raw)))
    connection.execute(
        "UPDATE cycle SET status='done',route='attack',"
        "finished_at='2026-07-12T00:00:00Z' WHERE id=1")
    connection.commit()
    publisher = CycleSnapshotPublisher(
        db_path=source / "research.sqlite", work_root=source)
    assert publisher.reconcile(startup=True) == ["c1"]
    connection.close()
    assert storage_main(["--work-root", str(source), "mirror-logs"]) == 0

    output = tmp_path / "packs"
    output.mkdir(mode=0o700)
    result = create_evidence_pack(
        source_work_root=source, output_parent=output)
    pack = Path(result["pack_path"])
    source.rename(tmp_path / "log-source-offline")
    verified = verify_evidence_pack(pack)
    assert verified["log_mirrors_verified"] == 1
    assert verified["one_cycle_resume_probe_verified"] is False
    assert verified["unresolved_registered_assets"] == 2  # checkpoint + cold log original

    # A second self-consistent-looking index/report must still be rejected
    # because the packed SQLite snapshot registers only execution_log id=1.
    manifest = json.loads((pack / "manifest.json").read_text(encoding="utf-8"))
    item_by_id = {value["logical_id"]: value for value in manifest["items"]}
    first_index = item_by_id["source/log-mirrors/indexes/execution-log-1.json"]
    duplicate = dict(first_index)
    duplicate["logical_id"] = "source/log-mirrors/indexes/execution-log-2.json"
    manifest["items"].append(duplicate)
    manifest["items"].sort(key=lambda value: value["logical_id"])
    report_item = item_by_id["source/log-mirrors/verify.json"]
    old_report_object = pack / "objects" / "sha256" / report_item["sha256"]
    report = json.loads(old_report_object.read_text(encoding="utf-8"))
    report["registered_logs"] = 2
    report["originals_verified"] = 2
    report["mirrors_verified"] = 2
    report_raw = (json.dumps(
        report, sort_keys=True, separators=(",", ":")) + "\n").encode()
    report_digest = hashlib.sha256(report_raw).hexdigest()
    new_report_object = pack / "objects" / "sha256" / report_digest
    new_report_object.write_bytes(report_raw)
    new_report_object.chmod(0o400)
    report_item.update({"sha256": report_digest, "bytes": len(report_raw)})
    if not any(value["sha256"] == old_report_object.name
               for value in manifest["items"]):
        old_report_object.unlink()
    rewritten = _rewrite_pack_manifest(pack, manifest)
    with pytest.raises(EvidencePackError, match="id 重复/未登记"):
        verify_evidence_pack(rewritten)


def test_storage_asset_inventory_is_cross_checked_against_packed_sqlite(tmp_path):
    source = tmp_path / "asset-source"
    source.mkdir(mode=0o700)
    connection = db.connect(source / "research.sqlite")
    conftest.seed_minimal(connection)
    connection.execute(
        "UPDATE cycle SET status='done',route='attack',"
        "finished_at='2026-07-12T00:00:00Z' WHERE id=1")
    connection.commit()
    publisher = CycleSnapshotPublisher(
        db_path=source / "research.sqlite", work_root=source)
    assert publisher.reconcile(startup=True) == ["c1"]
    connection.close()
    output = tmp_path / "packs"
    output.mkdir(mode=0o700)
    result = create_evidence_pack(source_work_root=source, output_parent=output)
    pack = Path(result["pack_path"])
    manifest = json.loads((pack / "manifest.json").read_text(encoding="utf-8"))
    items = {value["logical_id"]: value for value in manifest["items"]}

    storage_id = "source/storage/c1.manifest.json"
    storage = json.loads(
        (pack / "objects" / "sha256" / items[storage_id]["sha256"]).read_text())
    storage["assets"] = [
        value for value in storage["assets"] if value["owner"] != "checkpoint"]
    assets_raw = (json.dumps(
        storage["assets"], ensure_ascii=False, sort_keys=True,
        separators=(",", ":")) + "\n").encode()
    storage["asset_inventory_sha256"] = hashlib.sha256(assets_raw).hexdigest()
    storage_digest = _replace_json_item(pack, manifest, storage_id, storage)

    pointer_id = "source/storage/c1.pointer.json"
    pointer = json.loads(
        (pack / "objects" / "sha256" / items[pointer_id]["sha256"]).read_text())
    pointer["manifest_sha256"] = storage_digest
    pointer["manifest_path"] = (
        f"state/storage/manifests/sha256/{storage_digest}.json")
    _replace_json_item(pack, manifest, pointer_id, pointer)
    for report_id in (
            "source/storage/verify.json", "source/log-mirrors/verify.json",
            "source/import-materializations/verify.json"):
        report = json.loads(
            (pack / "objects" / "sha256"
             / items[report_id]["sha256"]).read_text())
        report["high_water_manifest_sha256"] = storage_digest
        _replace_json_item(pack, manifest, report_id, report)

    rewritten = _rewrite_pack_manifest(pack, manifest)
    with pytest.raises(EvidencePackError, match="asset inventory.*SQLite"):
        verify_evidence_pack(rewritten)


def test_local_canary_is_packaged_without_upgrading_two_node_claim(tmp_path):
    source = tmp_path / "source"
    system = build_system(
        SYSTEM_ROOT, str(source), runner_factory=_factory([BOOT]), attack=False)
    try:
        assert system.run(1) == ["c1"]
    finally:
        assert system.close() is None
    canary = tmp_path / "canary"
    run_id = "0123456789abcdef0123456789abcdef"
    final = run_local_canary(
        canary_root=canary, run_id=run_id,
        timeout_s=10, guardian_grace_s=0.1)
    assert final["two_node_verified"] is False
    output = tmp_path / "packs"
    output.mkdir(mode=0o700)
    result = create_evidence_pack(
        source_work_root=source, output_parent=output,
        canary_root=canary, canary_run_id=run_id)
    verified = verify_evidence_pack(result["pack_path"])
    assert verified["pack_integrity_verified"] is True
    manifest = json.loads(
        (Path(result["pack_path"]) / "manifest.json").read_text(encoding="utf-8"))
    report_item = next(
        value for value in manifest["items"]
        if value["logical_id"] == "shared-fs-canary/verify.json")
    report = json.loads((Path(result["pack_path"]) / "objects" / "sha256"
                         / report_item["sha256"]).read_text())
    assert report["two_node_verified"] is False
    assert report["infrastructure_fence_verified"] is False


def stat_is_regular(mode: int) -> bool:
    # Keep this test independent from pathlib's follow-symlink behaviour.
    import stat
    return stat.S_ISREG(mode)


def test_logical_inventory_accepts_safe_repository_names():
    logical = evidence_pack_module._logical_id  # noqa: SLF001 - protocol boundary test
    assert logical("source/import-materializations/objects/a/.gitignore")
    assert logical("source/import-materializations/objects/a/模型 权重.bin")
    assert logical(
        "source/import-materializations/objects/a/" + "/".join(
            f"d{index}" for index in range(128)))
    for unsafe in ("/absolute", "a/../b", "a//b", "a\\b", "a/\x00b", "a/\nb"):
        with pytest.raises(EvidencePackError):
            logical(unsafe)


def test_nonempty_import_and_dependency_closure_accepts_utf8_dotfiles(
        tmp_path, monkeypatch):
    import test_dependency_image as dependency_fixtures
    import test_storage_imports as storage_import_fixtures

    source = storage_import_fixtures._source(  # noqa: SLF001 - audited fixture
        tmp_path / "fixture", dependency=True)
    dependency_object, capability, _artifacts = (
        dependency_fixtures._pure_dependency_image_object(  # noqa: SLF001
            tmp_path / "real-dependency"))
    for current, directories, _files in os.walk(dependency_object):
        os.chmod(current, 0o700)
        for name in directories:
            os.chmod(Path(current) / name, 0o700)
    receipt_path = dependency_object / "receipt.json"
    os.chmod(receipt_path, 0o600)
    dependency_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    dependency_receipt["lock"]["path"] = "deps/python-wheel-lock.json"
    receipt_path.write_bytes(
        evidence_pack_module._canonical(dependency_receipt))  # noqa: SLF001
    capability = {
        **capability,
        "receipt_hash": "sha256:" + hashlib.sha256(
            evidence_pack_module._canonical(dependency_receipt)  # noqa: SLF001
        ).hexdigest(),
    }
    diagnostic_payloads = {
        "install/install.log": b"installed\n",
        "install/install.log.exit": b"0",
        "install/install.log.process.json": b"{}\n",
        "build/image.id": (dependency_receipt["result_image_id"] + "\n").encode(),
        "build/build.log": b"built\n",
        "build/build.log.exit": b"0",
        "build/build.log.process.json": b"{}\n",
        "save/save.log": b"saved\n",
        "save/save.log.exit": b"0",
        "save/save.log.process.json": b"{}\n",
        "runtime/runtime.log.process.json": b"{}\n",
        "check/pip-check.log.process.json": b"{}\n",
        "install/.sandbox-meta/session.json": b"{}\n",
        "install/.sandbox-meta/session.promotion-plan.json": b"{}\n",
        "install/.sandbox-meta/session.promoted.json": b"{}\n",
    }
    for relative, raw in diagnostic_payloads.items():
        destination = dependency_object / relative
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination.write_bytes(raw)
    context_root = dependency_object / "context"
    for current, directories, files in os.walk(
            dependency_object, topdown=False, followlinks=False):
        current_path = Path(current)
        for name in files:
            path = current_path / name
            os.chmod(path, 0o444 if context_root in path.parents else 0o400)
        for name in directories:
            path = current_path / name
            in_context = path == context_root or context_root in path.parents
            os.chmod(path, 0o555 if in_context else 0o500)
        in_context = current_path == context_root or context_root in current_path.parents
        os.chmod(current_path, 0o555 if in_context else 0o500)
    dependency_digest = dependency_object.name
    dependency_root = source["work"] / "state" / "dependency-images" / "objects"
    shutil.rmtree(dependency_root)
    dependency_root.mkdir(parents=True)
    shutil.copytree(
        dependency_object, dependency_root / dependency_digest,
        copy_function=shutil.copy2)
    source.update({
        "capability": capability, "dependency_digest": dependency_digest})

    repository = (source["work"] / "state" / "import-materializations"
                  / "objects" / source["repository_digest"])
    shutil.rmtree(repository)
    tree = repository / "tree"
    tree.mkdir(parents=True)
    payloads = {
        ".gitignore": b"*.tmp\n", "模型 权重.bin": b"weights",
        "artifact.bin": b"repository object",
    }
    ledger = []
    repository_name = "example/research-model"
    revision = "1" * 40
    for path, raw in sorted(payloads.items()):
        destination = tree / path
        destination.write_bytes(raw)
        value = {
            "path": path, "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw), "git_mode": "100644",
            "git_blob_sha1": hashlib.sha1(
                b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw
            ).hexdigest(),
            "repository": repository_name, "revision": revision,
        }
        if path == "模型 权重.bin":
            value["lfs"] = {
                "oid": value["sha256"], "size": value["bytes"],
                "pointer_sha256": "sha256:" + "2" * 64,
                "pointer_bytes": 128,
            }
        ledger.append(value)
    spec = dict(source["inspection"]["spec"])
    spec.update({
        "execution_image": capability,
        "env_hash": capability["environment_hash"],
    })
    result_value = {
        **spec, "source_tree": str(tree), "file_ledger": ledger,
        "snapshot_receipt": str(repository / "receipt.json"),
        "repository_snapshot_hash": "sha256:" + source["repository_digest"],
    }
    transport = []
    receipt = {
        **source["inspection"]["receipt"],
        "file_ledger_hash": "sha256:" + hashlib.sha256(
            evidence_pack_module._canonical(ledger)).hexdigest(),  # noqa: SLF001
        "file_count": len(ledger),
        "total_bytes": sum(value["bytes"] for value in ledger),
        "spec_hash": "sha256:" + hashlib.sha256(
            evidence_pack_module._canonical(spec)).hexdigest(),  # noqa: SLF001
        "transport_evidence_hash": "sha256:" + hashlib.sha256(
            evidence_pack_module._canonical(transport)).hexdigest(),  # noqa: SLF001
    }
    for name, value in (
            ("ledger.json", ledger), ("spec.json", spec),
            ("transport.json", transport), ("receipt.json", receipt)):
        (repository / name).write_bytes(
            evidence_pack_module._canonical(value))  # noqa: SLF001
    source["inspection"] = {
        "receipt": receipt, "ledger": ledger, "spec": spec,
        "transport": transport, "result": result_value,
    }
    writer = db.connect(source["work"] / "research.sqlite")
    writer.execute(
        "UPDATE build_target SET plan_ref=? WHERE target_kind='import'",
        (json.dumps(spec_ref(result_value), ensure_ascii=False, sort_keys=True),))
    writer.commit()
    writer.close()
    shutil.rmtree(source["work"] / "state" / "storage")
    shutil.rmtree(source["work"] / "views")
    publisher = CycleSnapshotPublisher(
        db_path=source["work"] / "research.sqlite", work_root=source["work"])
    assert publisher.reconcile(startup=True) == ["c1"]
    storage_import_fixtures._fake_inspectors(monkeypatch, source)  # noqa: SLF001

    output = tmp_path / "packs"
    output.mkdir(mode=0o700)
    result = create_evidence_pack(
        source_work_root=source["work"], output_parent=output)
    verified = verify_evidence_pack(result["pack_path"])
    assert verified["pack_integrity_verified"] is True
    manifest = json.loads(
        (Path(result["pack_path"]) / "manifest.json").read_text(encoding="utf-8"))
    logical_ids = {value["logical_id"] for value in manifest["items"]}
    prefix = f"source/import-materializations/objects/{source['repository_digest']}"
    assert f"{prefix}/tree/.gitignore" in logical_ids
    assert f"{prefix}/tree/模型 权重.bin" in logical_ids
    assert any("dependency-images/objects" in value for value in logical_ids)
    dependency_prefix = (
        "source/import-materializations/dependency-images/objects/"
        f"{dependency_digest}")
    assert f"{dependency_prefix}/python-wheel-lock.json" in logical_ids
    assert f"{dependency_prefix}/install/install.log" not in logical_ids
    assert not any("/.sandbox-meta/" in value for value in logical_ids)

    pack = Path(result["pack_path"])
    dependency_copy_parent = tmp_path / "dependency-copy"
    dependency_copy_parent.mkdir(mode=0o700)
    dependency_copy = dependency_copy_parent / pack.name
    shutil.copytree(pack, dependency_copy, copy_function=shutil.copy2)

    missing_id = f"{prefix}/tree/模型 权重.bin"
    missing = next(
        value for value in manifest["items"] if value["logical_id"] == missing_id)
    manifest["items"].remove(missing)
    if not any(value["sha256"] == missing["sha256"] for value in manifest["items"]):
        (pack / "objects" / "sha256" / missing["sha256"]).unlink()
    rewritten = _rewrite_pack_manifest(pack, manifest)
    with pytest.raises(EvidencePackError, match="repository.*闭包"):
        verify_evidence_pack(rewritten)

    dependency_manifest = json.loads(
        (dependency_copy / "manifest.json").read_text(encoding="utf-8"))
    image = next(
        value for value in dependency_manifest["items"]
        if value["logical_id"].endswith(f"/{dependency_digest}/image.tar"))
    dependency_manifest["items"].remove(image)
    if not any(value["sha256"] == image["sha256"]
               for value in dependency_manifest["items"]):
        (dependency_copy / "objects" / "sha256" / image["sha256"]).unlink()
    dependency_rewritten = _rewrite_pack_manifest(
        dependency_copy, dependency_manifest)
    with pytest.raises(EvidencePackError, match="dependency.*image.tar"):
        verify_evidence_pack(dependency_rewritten)
