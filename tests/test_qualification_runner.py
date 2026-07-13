"""One-shot final predictor semantics without requiring a live Docker daemon."""
from __future__ import annotations

import hashlib
import json
import os
import types
from pathlib import Path

import pytest
import yaml

from orchestrator import qualification_runner as QR
from orchestrator.qualification_firewall import (
    CLAIM_BOUNDARY_PROTOCOL,
    CLAIM_PROTOCOL,
    CONTRACT_PROTOCOL,
    QualificationFinalizedError,
    _canonical as firewall_canonical,
    _hash_bytes as firewall_hash,
    _publish_once,
    consume_final,
    final_units,
    install_contract,
    load_qualification_firewall,
    publish_claim_lock,
)


SYSTEM_ROOT = Path(__file__).resolve().parent.parent
BASE_POLICY = yaml.safe_load(
    (SYSTEM_ROOT / "policies" / "policy.yaml").read_text(encoding="utf-8"))


def _canonical(value):
    return (json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _sha(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _publish_fixture_claim(work: Path, claim, source: Path):
    firewall = load_qualification_firewall(work)
    _ledger, source_hash = QR.freeze_source_tree(source)
    explores = sorted(
        (mount for mount in firewall.mounts if mount.role == "explore"),
        key=lambda mount: (mount.dataset.casefold(), mount.dataset),
    )
    command = None
    if firewall.task == "T1":
        command = {
            "argv": [
                "python", "{src}/confirm.py",
                *[token for mount in explores
                  for token in ("--data", str(mount.path))],
            ],
            "output": "confirmatory.json",
            "gpu_required": firewall.final["gpu_required"],
        }
    boundary = {
        "version": 1,
        "protocol": CLAIM_BOUNDARY_PROTOCOL,
        "task": firewall.task,
        "contract_sha256": firewall.contract_sha256,
        "claim_sha256": _sha(_canonical(claim)),
        "source_tree_sha256": source_hash,
        "a_high_water": {
            "cycle_id": "c1",
            "storage_manifest_sha256": "sha256:" + "b" * 64,
        },
        "explore_views": [
            {"dataset": mount.dataset,
             "tree_sha256": _sha(mount.dataset.encode("utf-8"))}
            for mount in explores
        ],
        "confirmatory_command": command,
    }
    _publish_once(firewall.claim_boundary_path, _canonical(boundary))
    publish_claim_lock(work, claim)


def _view(path):
    path.mkdir(mode=0o755)
    payload = path / "payload.bin"
    payload.write_bytes(b"safe\n")
    payload.chmod(0o444)
    manifest_value = {
        "adapter": "meta-research-dreamer-public-view",
        "adapter_version": 1, "record_count": 2,
        "sample_ids": ["1" * 64, "2" * 64],
        "label_rule": {
            "score": "valence", "threshold": 3.0,
            "comparison": "higher_is_positive", "neutral_policy": "drop",
        },
    }
    manifest = _canonical(manifest_value)
    (path / "manifest.json").write_bytes(manifest)
    (path / "manifest.json").chmod(0o444)
    raw = _canonical({
        "version": 1, "protocol": "meta-research-qualification-view/v1",
        "task": "T1", "role": "sealed_holdout", "dataset": "DREAMER",
        "fold": None, "adapter": "meta-research-dreamer-public-view",
        "adapter_version": 1,
        "files": [
            {"path": "manifest.json", "sha256": _sha(manifest), "bytes": len(manifest)},
            {"path": "payload.bin", "sha256": _sha(b"safe\n"), "bytes": 5},
        ],
    })
    (path / "qualification-view.json").write_bytes(raw)
    (path / "qualification-view.json").chmod(0o444)
    path.chmod(0o555)
    return _sha(raw)


def _setup(tmp_path, *, truth_threshold=3.0, gpu_required=False):
    mounts = []
    for dataset in ("SEED", "SEED-IV", "FACED"):
        path = tmp_path / dataset
        path.mkdir(mode=0o755)
        (path / "data.bin").write_bytes(dataset.encode())
        (path / "data.bin").chmod(0o444)
        path.chmod(0o555)
        mounts.append({
            "path": str(path), "role": "explore", "dataset": dataset,
            "fold": None, "view_receipt_sha256": None,
        })
    holdout = tmp_path / "dreamer-x"
    mounts.append({
        "path": str(holdout), "role": "sealed_holdout", "dataset": "DREAMER",
        "fold": None, "view_receipt_sha256": _view(holdout),
    })
    truth_payload = _canonical({
        "version": 1, "task": "T1", "classes": 2,
        "label_rule": {
            "score": "valence", "threshold": truth_threshold,
            "comparison": "higher_is_positive", "neutral_policy": "drop",
        },
        "units": [{
            "unit_id": "dreamer", "sample_ids": ["1" * 64, "2" * 64],
            "labels": [0, 1], "groups": [1, 1],
        }],
    })
    truth = tmp_path / "truth.json"
    truth.write_bytes(truth_payload)
    truth.chmod(0o400)
    if os.geteuid() != 0:
        pytest.skip("cross-UID truth fixture requires root")
    os.chown(truth, 65534, os.getegid())
    contract = {
        "version": 1, "protocol": CONTRACT_PROTOCOL, "task": "T1",
        "research_uid": os.geteuid(), "evaluator_uid": 65534,
        "forbid_code_imports": True, "mounts": mounts,
        "sealed_truth": {"path": str(truth), "sha256": _sha(truth_payload)},
        "final": {
            "classes": 2, "seeds": [], "folds": [], "unit_ids": ["dreamer"],
            "gpu_required": gpu_required,
        },
    }
    work = tmp_path / "work"
    install_contract(work, contract)
    source = tmp_path / "source"
    source.mkdir(mode=0o755)
    (source / "predict.py").write_text("# frozen predictor\n", encoding="utf-8")
    (source / "predict.py").chmod(0o444)
    claim = {
        "version": 1, "protocol": CLAIM_PROTOCOL, "task": "T1",
        "claims": [{"id": "c1"}], "feature_operator": {"name": "f"},
        "label_mapping": {"name": "binary"}, "model": {"name": "m"},
        "preprocessing": {"name": "p"}, "hpo": {"space": "frozen"},
        "search_space": {"budget": 1}, "primary_metrics": ["accuracy"],
        "statistical_tests": {"test": "locked"},
        "multiple_testing": {"method": "Holm"},
        "exclusion_rules": {"rules": ["none"]},
        "controls": [
            "majority", "class-prior-random", "matched-random", "label-permutation",
            "subject-id-only", "dataset-id-only", "trial-id-only", "source-only-linear",
            "confidence-only", "preprocessing-consistency", "leakage-probe",
        ],
        "datasets": {
            "exploration": ["SEED", "SEED-IV", "FACED"],
            "confirmatory_lodo": ["SEED", "SEED-IV", "FACED"],
            "sealed_holdout": {
                "dataset": "DREAMER", "score": "valence",
                "comparison": "higher_is_positive", "threshold": 3,
                "neutral_policy": "drop",
            },
        },
        "final_command": {
            "argv": ["python", "{src}/predict.py", "--data", "{data}"],
            "output": "predictions.json", "gpu_required": gpu_required,
        },
    }
    _publish_fixture_claim(work, claim, source)
    policy = json.loads(json.dumps(BASE_POLICY))
    paths = [item["path"] for item in mounts]
    policy["execution"]["path_allowlist"] = paths
    policy["execution"]["sandbox"]["readonly_mounts"] = paths
    system = tmp_path / "system"
    (system / "policies").mkdir(parents=True)
    (system / "policies" / "policy.yaml").write_text(
        yaml.safe_dump(policy, allow_unicode=True), encoding="utf-8")
    return system, work, source, holdout


def _t2_view(path, fold):
    path.mkdir(mode=0o755)
    target = path / "target"
    target.mkdir(mode=0o755)
    sample_ids = [
        hashlib.sha256(f"seed-fold-{fold}-sample-{index}".encode()).hexdigest()
        for index in range(3)
    ]
    sample_manifest = _canonical({
        "version": 1, "fold": fold, "sample_ids": sample_ids,
    })
    protocol = _canonical({
        "adapter": "meta-research-seed-public-view",
        "adapter_version": 1,
        "profile": "seed-cross-subject-uda-public-v1",
        "feature": "de_LDS", "dtype": "float32",
        "sample_shape": [62, 5], "classes": [0, 1, 2],
        "source_subjects": [
            f"subject-{subject:02d}" for subject in range(1, 16)
            if subject != fold
        ],
        "target_file": "target/x.npy",
        "target_sample_ids_file": "target/sample_ids.json",
    })
    payloads = {
        "protocol.json": protocol,
        "target/sample_ids.json": sample_manifest,
        "target/x.npy": f"fake-fold-{fold}-features\n".encode(),
    }
    for relative, raw in payloads.items():
        item = path / relative
        item.write_bytes(raw)
        item.chmod(0o444)
    receipt = _canonical({
        "version": 1, "protocol": "meta-research-qualification-view/v1",
        "task": "T2", "role": "fold", "dataset": "SEED", "fold": fold,
        "adapter": "meta-research-seed-public-view", "adapter_version": 1,
        "files": [
            {"path": relative, "sha256": _sha(raw), "bytes": len(raw)}
            for relative, raw in sorted(payloads.items())
        ],
    })
    (path / "qualification-view.json").write_bytes(receipt)
    (path / "qualification-view.json").chmod(0o444)
    target.chmod(0o555)
    path.chmod(0o555)
    return sample_ids, _sha(receipt)


def _setup_t2(tmp_path):
    if os.geteuid() != 0:
        pytest.skip("cross-UID truth fixture requires root")
    mounts = []
    sample_ids_by_fold = {}
    fold_roots = {}
    truth_folds = []
    for fold in range(1, 16):
        root = tmp_path / f"seed-fold-{fold:02d}"
        sample_ids, receipt_hash = _t2_view(root, fold)
        fold_roots[fold] = root
        sample_ids_by_fold[fold] = sample_ids
        truth_folds.append({
            "fold": fold, "sample_ids": sample_ids, "labels": [0, 1, 2],
        })
        mounts.append({
            "path": str(root), "role": "fold", "dataset": "SEED",
            "fold": fold, "view_receipt_sha256": receipt_hash,
        })

    truth_payload = _canonical({
        "version": 1, "task": "T2", "classes": 3, "folds": truth_folds,
    })
    truth = tmp_path / "seed-truth.json"
    truth.write_bytes(truth_payload)
    truth.chmod(0o400)
    os.chown(truth, 65534, os.getegid())
    contract = {
        "version": 1, "protocol": CONTRACT_PROTOCOL, "task": "T2",
        "research_uid": os.geteuid(), "evaluator_uid": 65534,
        "forbid_code_imports": True, "mounts": mounts,
        "sealed_truth": {"path": str(truth), "sha256": _sha(truth_payload)},
        "final": {
            "classes": 3, "seeds": [7, 17, 29],
            "folds": list(range(1, 16)), "unit_ids": [],
            "gpu_required": False,
        },
    }
    work = tmp_path / "work"
    install_contract(work, contract)
    source = tmp_path / "source"
    source.mkdir(mode=0o755)
    (source / "predict.py").write_text("# frozen T2 predictor\n", encoding="utf-8")
    (source / "predict.py").chmod(0o444)
    claim = {
        "version": 1, "protocol": CLAIM_PROTOCOL, "task": "T2",
        "claims": [{"id": "c1", "text": "frozen T2 claim"}],
        "feature_operator": {"name": "frozen"},
        "label_mapping": {"name": "locked"},
        "model": {"name": "locked"},
        "preprocessing": {"name": "per-fold"},
        "hpo": {"space": "locked"},
        "search_space": {"budget": 1},
        "primary_metrics": ["accuracy"],
        "statistical_tests": {"name": "locked"},
        "multiple_testing": {"method": "Holm"},
        "exclusion_rules": {"rules": ["none"]},
        "controls": [
            "majority", "source-prior-random", "source-only-linear",
            "source-only-mlp", "source-only-deep", "single-best-source",
            "confidence-only", "label-shuffle", "trial-id-only",
        ],
        "datasets": {
            "dataset": "SEED", "subjects": list(range(1, 16)), "classes": 3,
            "input": "1s-nonoverlap-DE-62x5", "normalization": "per-fold",
            "hpo_labels": "source-inner-loso", "final_seeds": [7, 17, 29],
            "final_folds": list(range(1, 16)),
        },
        "final_command": {
            "argv": [
                "python", "{src}/predict.py", "--data", "{data}",
                "--seed", "{seed}", "--fold", "{fold}",
            ],
            "output": "predictions.json", "gpu_required": False,
        },
    }
    _publish_fixture_claim(work, claim, source)
    policy = json.loads(json.dumps(BASE_POLICY))
    paths = [item["path"] for item in mounts]
    policy["execution"]["path_allowlist"] = paths
    policy["execution"]["sandbox"]["readonly_mounts"] = paths
    system = tmp_path / "system"
    (system / "policies").mkdir(parents=True)
    (system / "policies" / "policy.yaml").write_text(
        yaml.safe_dump(policy, allow_unicode=True), encoding="utf-8")
    return system, work, source, fold_roots, sample_ids_by_fold


class _FakeInvocation:
    argv = ["fake"]
    env = {}
    pass_fds = ()

    def close(self):
        return None


class _FakeSandbox:
    runtime_identity_hash = "sha256:" + "c" * 64
    gpu_contract_hash = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def preflight(self):
        return None

    def recover_terminal_sessions(self, supervisor):
        return 0

    def prepare(self, command, **kwargs):
        assert kwargs["execution_context"]["phase"] == "qualification-final"
        assert kwargs["gpu_required"] is False
        assert any("dreamer-x" in token for token in command)
        return _FakeInvocation()


def _fake_gpu_contract():
    return {
        "version": 1, "provider": "nvidia", "driver_version": "535.129.03",
        "request": {
            "driver": "nvidia", "capabilities": ["compute", "utility", "gpu"],
            "options": {},
        },
        "devices": [{
            "uuid": "GPU-a", "model": "NVIDIA A100",
            "memory_bytes": 80 * 1024 ** 3, "compute_capability": "8.0",
        }],
    }


def _fake_successful_predictor(_argv, *, staging_dir, **_kwargs):
    root = Path(staging_dir)
    root.mkdir(parents=True, exist_ok=True)
    (root / "predictions.json").write_bytes(_canonical({
        "version": 1, "unit_id": "dreamer", "seed": None, "fold": None,
        "sample_ids": ["1" * 64, "2" * 64],
        "probabilities": [[0.9, 0.1], [0.2, 0.8]],
    }))
    (root / "final.log").write_text("ok\n", encoding="utf-8")
    return {
        "exit_code": 0, "log_path": str(root / "final.log"),
        "log_sha256": "0" * 64, "process_receipt_path": "receipt.json",
    }


def test_gpu_final_uses_full_authority_validator_and_owner_bound_candidate(
        tmp_path, monkeypatch):
    system, work, source, _holdout = _setup(tmp_path, gpu_required=True)
    gpu_contract = _fake_gpu_contract()
    gpu_path = tmp_path / "gpu-contract.json"
    gpu_path.write_bytes(_canonical(gpu_contract))
    candidate_owners = {}
    validations = []

    class FakeGpuSandbox(_FakeSandbox):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.config = dict(kwargs["config"])
            self.gpu_contract = dict(kwargs["gpu_contract"])
            self.gpu_contract_hash = _sha(_canonical(self.gpu_contract))

        def run_gpu_canary(self, *, execution_supervisor, candidate_hash):
            candidate_owners[candidate_hash] = execution_supervisor.owner_id
            return {
                "candidate_hash": candidate_hash,
                "checked_at_unix": QR.time.time(),
                "contract_hash": self.gpu_contract_hash,
                "runtime_identity_hash": self.runtime_identity_hash,
                "ok": True,
            }

        def prepare(self, command, **kwargs):
            assert kwargs["gpu_required"] is True
            assert any("dreamer-x" in token for token in command)
            return _FakeInvocation()

    def fake_validate(evidence, **kwargs):
        validations.append((dict(evidence), dict(kwargs)))
        assert kwargs["require_fence"] is True
        actual_owner = candidate_owners[evidence["candidate_hash"]]
        assert kwargs["owner_id"] in {None, actual_owner}
        return {"owner_id": actual_owner}

    monkeypatch.setattr(QR, "DockerExecutionSandbox", FakeGpuSandbox)
    monkeypatch.setattr(QR, "validate_gpu_canary_evidence", fake_validate)
    monkeypatch.setattr(QR.H, "run_staged", _fake_successful_predictor)
    result = QR.run_final(
        system_root=system, work_root=work, source_root=source,
        gpu_contract_path=gpu_path)

    assert result["success_count"] == 1
    assert len(validations) == 2
    firewall = load_qualification_firewall(work)
    marker = firewall.read_final_marker()
    _boundary, boundary_raw = firewall.read_claim_boundary()
    assert marker["version"] == 2
    assert marker["claim_boundary_sha256"] == _sha(boundary_raw)
    assert marker["gpu_canary_sha256"] is not None
    evidence = validations[0][0]
    assert evidence["candidate_hash"] == QR._gpu_canary_candidate_hash(
        claim_sha256=marker["claim_sha256"],
        source_tree_sha256=marker["source_tree_sha256"],
        runtime_identity_sha256=marker["runtime_identity_sha256"],
        gpu_contract_sha256=_sha(_canonical(gpu_contract)),
        owner_id=candidate_owners[evidence["candidate_hash"]])


def test_gpu_final_rejects_preseed_without_frozen_owner_binding(tmp_path, monkeypatch):
    system, work, source, _holdout = _setup(tmp_path, gpu_required=True)
    gpu_contract = _fake_gpu_contract()
    gpu_path = tmp_path / "gpu-contract.json"
    gpu_path.write_bytes(_canonical(gpu_contract))
    canary_path = work / "state" / "qualification" / "final" / "gpu-canary.json"
    _publish_once(canary_path, _canonical({
        "candidate_hash": "sha256:" + "a" * 64,
        "checked_at_unix": QR.time.time(), "ok": True,
    }))

    class FakeGpuSandbox(_FakeSandbox):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.config = dict(kwargs["config"])
            self.gpu_contract = dict(kwargs["gpu_contract"])
            self.gpu_contract_hash = _sha(_canonical(self.gpu_contract))

        def run_gpu_canary(self, **_kwargs):
            pytest.fail("preseeded canary must not be treated as a reason to run final")

    monkeypatch.setattr(QR, "DockerExecutionSandbox", FakeGpuSandbox)
    monkeypatch.setattr(
        QR, "validate_gpu_canary_evidence",
        lambda _evidence, **_kwargs: {"owner_id": "owner-forged"})
    monkeypatch.setattr(
        QR.H, "run_staged", lambda *_args, **_kwargs: pytest.fail("predictor ran"))

    with pytest.raises(QR.QualificationRunnerError, match="冻结输入/guardian owner"):
        QR.run_final(
            system_root=system, work_root=work, source_root=source,
            gpu_contract_path=gpu_path)
    assert not os.path.lexists(work / "state" / "qualification" / "final-consumed.json")


def test_final_batch_consumes_once_and_replays_receipts_without_second_predictor(
        tmp_path, monkeypatch):
    system, work, source, _holdout = _setup(tmp_path)
    calls = []

    def fake_run(_argv, *, staging_dir, **kwargs):
        calls.append(kwargs["execution_context"]["unit_id"])
        root = Path(staging_dir)
        root.mkdir(parents=True, exist_ok=True)
        prediction = {
            "version": 1, "unit_id": "dreamer", "seed": None, "fold": None,
            "sample_ids": ["1" * 64, "2" * 64],
            "probabilities": [[0.9, 0.1], [0.2, 0.8]],
        }
        (root / "predictions.json").write_bytes(_canonical(prediction))
        (root / "final.log").write_text("candidate metric_value: 999 ignored\n")
        return {
            "exit_code": 0, "log_path": str(root / "final.log"),
            "log_sha256": "0" * 64, "process_receipt_path": "receipt.json",
        }

    monkeypatch.setattr(QR, "DockerExecutionSandbox", _FakeSandbox)
    monkeypatch.setattr(QR.H, "run_staged", fake_run)
    first = QR.run_final(system_root=system, work_root=work, source_root=source)
    assert first["success_count"] == 1 and first["failure_count"] == 0
    assert calls == ["dreamer"]
    firewall = load_qualification_firewall(work)
    with pytest.raises(QualificationFinalizedError):
        firewall.assert_research_open()

    second = QR.run_final(system_root=system, work_root=work, source_root=source)
    assert second == first
    assert calls == ["dreamer"]  # no second scientific execution

    scored = QR.score_final(work_root=work)
    assert scored["status"] == "success"
    assert scored["metrics"]["metrics"]["accuracy"] == 1.0
    assert scored["evaluation_error"] is None
    assert QR.score_final(work_root=work) == scored

    score_path = work / "state" / "qualification" / "final-result.json"
    tampered_score = json.loads(score_path.read_text(encoding="utf-8"))
    tampered_score["metrics"]["metrics"]["accuracy"] = 0.0
    score_path.chmod(0o600)
    score_path.write_bytes(_canonical(tampered_score))
    score_path.chmod(0o400)
    with pytest.raises(QR.QualificationRunnerError, match="重新计算结果冲突"):
        QR.score_final(work_root=work)

    prediction = Path(first["units"][0]["prediction"]["path"])
    prediction.chmod(0o600)
    prediction.write_text("{}\n", encoding="utf-8")
    with pytest.raises(QR.QualificationRunnerError, match="prediction"):
        QR.run_final(system_root=system, work_root=work, source_root=source)


def test_freeze_source_rejects_git_symlink_and_mutable_group_file(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "predict.py").write_text("pass\n")
    _ledger, digest = QR.freeze_source_tree(source)
    assert digest.startswith("sha256:")
    (source / ".git").mkdir()
    with pytest.raises(QR.QualificationRunnerError, match="repo"):
        QR.freeze_source_tree(source)
    (source / ".git").rmdir()
    (source / "linked.py").symlink_to(source / "predict.py")
    with pytest.raises(QR.QualificationRunnerError, match="不安全"):
        QR.freeze_source_tree(source)
    (source / "linked.py").unlink()
    (source / "predict.py").chmod(0o664)
    with pytest.raises(QR.QualificationRunnerError, match="不安全"):
        QR.freeze_source_tree(source)


def test_final_rejects_missing_boundary_before_sandbox(tmp_path, monkeypatch):
    system, work, source, _holdout = _setup(tmp_path)
    firewall = load_qualification_firewall(work)
    firewall.claim_boundary_path.unlink()
    monkeypatch.setattr(
        QR, "DockerExecutionSandbox",
        lambda **_kwargs: pytest.fail("sandbox constructed before boundary validation"))

    with pytest.raises(QR.QualificationFirewallError, match="claim boundary"):
        QR.run_final(system_root=system, work_root=work, source_root=source)


def test_spent_before_spawn_is_counted_failed_and_never_reexecuted(tmp_path, monkeypatch):
    system, work, source, _holdout = _setup(tmp_path)
    firewall = load_qualification_firewall(work)
    _ledger, source_hash = QR.freeze_source_tree(source)
    marker = consume_final(
        work, source_tree_sha256=source_hash,
        runtime_identity_sha256=_FakeSandbox.runtime_identity_hash, now=1.0)
    _claim, claim_raw = firewall.read_claim_lock()
    unit = final_units(firewall)[0]
    spent_path, _result_path, _run_root = QR._unit_paths(firewall, unit["unit_id"])
    _publish_once(spent_path, firewall_canonical({
        "version": 1, "protocol": QR.UNIT_SPENT_PROTOCOL, "task": "T1",
        "unit": unit, "final_marker_sha256": firewall_hash(firewall_canonical(marker)),
        "claim_sha256": firewall_hash(claim_raw), "source_tree_sha256": source_hash,
        "runtime_identity_sha256": _FakeSandbox.runtime_identity_hash,
        "gpu_canary_sha256": None,
        "execution_context": QR._unit_context(firewall, unit),
        "spent_at_unix": 1.0,
    }))
    monkeypatch.setattr(QR, "DockerExecutionSandbox", _FakeSandbox)
    monkeypatch.setattr(QR.H, "recover_staged_result", lambda **kwargs: None)
    monkeypatch.setattr(
        QR.H, "run_staged", lambda *args, **kwargs: pytest.fail("spent unit reran"))
    result = QR.run_final(system_root=system, work_root=work, source_root=source)
    assert result["success_count"] == 0 and result["failure_count"] == 1
    assert "spent before" in result["units"][0]["failure"]


def test_score_validation_failure_is_terminal_and_replay_safe(tmp_path, monkeypatch):
    system, work, source, _holdout = _setup(tmp_path, truth_threshold=4.0)

    def fake_run(_argv, *, staging_dir, **_kwargs):
        root = Path(staging_dir)
        root.mkdir(parents=True, exist_ok=True)
        (root / "predictions.json").write_bytes(_canonical({
            "version": 1, "unit_id": "dreamer", "seed": None, "fold": None,
            "sample_ids": ["1" * 64, "2" * 64],
            "probabilities": [[0.9, 0.1], [0.2, 0.8]],
        }))
        (root / "final.log").write_text("ok\n", encoding="utf-8")
        return {
            "exit_code": 0, "log_path": str(root / "final.log"),
            "log_sha256": "0" * 64, "process_receipt_path": "receipt.json",
        }

    monkeypatch.setattr(QR, "DockerExecutionSandbox", _FakeSandbox)
    monkeypatch.setattr(QR.H, "run_staged", fake_run)
    QR.run_final(system_root=system, work_root=work, source_root=source)
    first = QR.score_final(work_root=work)
    assert first["status"] == "failed" and first["metrics"] is None
    assert "label_rule" in first["evaluation_error"]
    assert QR.score_final(work_root=work) == first


def test_score_rejects_missing_boundary_before_reading_sealed_truth(
        tmp_path, monkeypatch):
    system, work, source, _holdout = _setup(tmp_path)

    monkeypatch.setattr(QR, "DockerExecutionSandbox", _FakeSandbox)
    monkeypatch.setattr(QR.H, "run_staged", _fake_successful_predictor)
    QR.run_final(system_root=system, work_root=work, source_root=source)
    firewall = load_qualification_firewall(work)
    boundary, _boundary_raw = firewall.read_claim_boundary()
    firewall.claim_boundary_path.unlink()
    monkeypatch.setattr(
        QR, "read_artifact_bytes",
        lambda *_args, **_kwargs: pytest.fail("sealed truth read before boundary validation"))

    with pytest.raises(QR.QualificationFirewallError, match="claim boundary"):
        QR.score_final(work_root=work)

    boundary["a_high_water"]["storage_manifest_sha256"] = "sha256:" + "d" * 64
    _publish_once(firewall.claim_boundary_path, _canonical(boundary))
    with pytest.raises(QR.QualificationRunnerError, match="claim boundary"):
        QR.score_final(work_root=work)


def test_t2_final_runs_exactly_45_single_fold_units_and_does_not_retry_failure(
        tmp_path, monkeypatch):
    system, work, source, fold_roots, sample_ids_by_fold = _setup_t2(tmp_path)
    prepared = []
    calls = []
    failed_cell = (17, 8)

    class T2FakeSandbox(_FakeSandbox):
        def prepare(self, command, **kwargs):
            context = kwargs["execution_context"]
            assert set(context) == {
                "phase", "qualification_task", "unit_id", "db_owner_kind",
                "db_owner_id", "log_name",
            }
            assert context["phase"] == "qualification-final"
            assert context["qualification_task"] == "t2"
            assert context["db_owner_kind"] == "qualification_final_unit"
            assert context["log_name"] == "final.log"
            assert kwargs["gpu_required"] is False

            unit_parts = context["unit_id"].split("-")
            assert unit_parts[0] == "seed" and unit_parts[2] == "fold"
            seed, fold = int(unit_parts[1]), int(unit_parts[3])
            assert context["db_owner_id"] == [7, 17, 29].index(seed) * 15 + fold
            assert int(command[command.index("--seed") + 1]) == seed
            assert int(command[command.index("--fold") + 1]) == fold
            data_root = command[command.index("--data") + 1]
            assert data_root == str(fold_roots[fold])
            assert [root for root in fold_roots.values() if str(root) in command] == [
                fold_roots[fold]]
            prepared.append((context["unit_id"], data_root))
            return _FakeInvocation()

    def fake_run(_argv, *, staging_dir, **kwargs):
        context = kwargs["execution_context"]
        assert set(context) == {
            "phase", "qualification_task", "unit_id", "db_owner_kind",
            "db_owner_id",
        }
        unit_parts = context["unit_id"].split("-")
        seed, fold = int(unit_parts[1]), int(unit_parts[3])
        calls.append(context["unit_id"])
        root = Path(staging_dir)
        root.mkdir(parents=True, exist_ok=True)
        (root / "final.log").write_text("predictor output only\n", encoding="utf-8")
        exit_code = 23 if (seed, fold) == failed_cell else 0
        if exit_code == 0:
            (root / "predictions.json").write_bytes(_canonical({
                "version": 1, "unit_id": context["unit_id"],
                "seed": seed, "fold": fold,
                "sample_ids": sample_ids_by_fold[fold],
                "probabilities": [
                    [0.8, 0.1, 0.1],
                    [0.1, 0.8, 0.1],
                    [0.1, 0.1, 0.8],
                ],
            }))
        return {
            "exit_code": exit_code, "log_path": str(root / "final.log"),
            "log_sha256": "0" * 64, "process_receipt_path": "receipt.json",
        }

    monkeypatch.setattr(QR, "DockerExecutionSandbox", T2FakeSandbox)
    monkeypatch.setattr(QR.H, "run_staged", fake_run)
    first = QR.run_final(system_root=system, work_root=work, source_root=source)

    firewall = load_qualification_firewall(work)
    expected_units = final_units(firewall)
    expected_ids = [unit["unit_id"] for unit in expected_units]
    marker = firewall.read_final_marker()
    assert marker["runtime_identity_sha256"] == T2FakeSandbox.runtime_identity_hash
    assert marker["units"] == expected_units
    assert calls == expected_ids
    assert [unit_id for unit_id, _data_root in prepared] == expected_ids
    assert len(calls) == len(set(calls)) == 45
    assert first["success_count"] == 44
    assert first["failure_count"] == 1
    failed = [unit for unit in first["units"] if unit["status"] == "failed"]
    assert len(failed) == 1
    assert failed[0]["unit"] == {
        "unit_id": "seed-17-fold-08", "seed": 17, "fold": 8,
    }
    assert failed[0]["failure"] == "predictor exit=23"
    assert first["units"][-1]["status"] == "success"

    replayed = QR.run_final(system_root=system, work_root=work, source_root=source)
    assert replayed == first
    assert calls == expected_ids
    assert [unit_id for unit_id, _data_root in prepared] == expected_ids


@pytest.mark.parametrize(
    ("command", "result", "expected"),
    [
        ("run-final", {"failure_count": 0}, 0),
        ("run-final", {"failure_count": 1}, 3),
        ("score-final", {"status": "success"}, 0),
        ("score-final", {"status": "failed"}, 3),
    ],
)
def test_cli_exit_code_reflects_scientific_outcome(
        tmp_path, monkeypatch, capsys, command, result, expected):
    if command == "run-final":
        monkeypatch.setattr(QR, "run_final", lambda **_kwargs: result)
        argv = [
            "--work-root", str(tmp_path / "work"), "run-final",
            "--system-root", str(tmp_path / "system"),
            "--source-root", str(tmp_path / "source"),
        ]
    else:
        monkeypatch.setattr(QR, "score_final", lambda **_kwargs: result)
        argv = ["--work-root", str(tmp_path / "work"), "score-final"]

    assert QR.main(argv) == expected
    assert json.loads(capsys.readouterr().out) == result
