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
from orchestrator.process_supervisor import atomic_write_receipt
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
        "explore_views": QR._explore_view_identities(firewall),
        "confirmatory_command": command,
    }
    _publish_once(firewall.claim_boundary_path, _canonical(boundary))
    publish_claim_lock(work, claim)


def _confirmatory_output(claim):
    datasets = sorted(
        claim["datasets"]["confirmatory_lodo"],
        key=lambda item: (item.casefold(), item))
    return {
        "version": 1, "protocol": QR.CONFIRMATORY_OUTPUT_PROTOCOL,
        "folds": [
            {
                "held_out_dataset": dataset, "status": "success",
                "metrics": {"effect": 0.1, "ci95": [0.01, 0.2]},
                "failure": None,
            }
            for dataset in datasets
        ],
        "aggregate": {"direction_consistent": True, "meta_effect": 0.1},
        "audit_material": {
            "controls": "frozen-control-ledger",
            "novelty": "post-lock-query-ledger",
        },
    }


def _write_confirmatory_promotion(
        firewall, output_raw: bytes) -> dict[str, str]:
    path = QR._confirmatory_promotion_path(firewall)
    files = [{
        "path": "confirmatory.json", "sha256": _sha(output_raw),
        "bytes": len(output_raw),
    }]
    context = {**QR._confirmatory_context(), "log_name": "confirmatory.log"}
    receipt = {
        "version": 1,
        "session_id": QR.sandbox_session_id("confirmatory.log", context),
        "exit_code": 0,
        "promoted": True,
        "container_drained": True,
        "output_manifest_hash": _sha(_canonical({"files": files})),
        "files": files,
    }
    atomic_write_receipt(path, receipt)
    raw = path.read_bytes()
    return {"path": str(path), "sha256": _sha(raw)}


def _seed_confirmatory_admission(work: Path):
    """Seed a root-authorized C receipt for tests whose subject is stage D."""
    firewall = load_qualification_firewall(work)
    claim, claim_raw = firewall.read_claim_lock()
    _boundary, boundary_raw = firewall.read_claim_boundary()
    claim_hash = _sha(claim_raw)
    boundary_hash = _sha(boundary_raw)
    spent_path, result_path, run_root, _audit_ref_path, _audit_input_path = (
        QR._confirmatory_paths(firewall))
    run_root.mkdir(parents=True)
    output_raw = _canonical(_confirmatory_output(claim))
    output_path = run_root / "confirmatory.json"
    output_path.write_bytes(output_raw)
    output_path.chmod(0o400)
    promotion = _write_confirmatory_promotion(firewall, output_raw)
    receipt_dir = work / "state" / "executions"
    receipt_dir.mkdir(parents=True, mode=0o700)
    receipt_dir.chmod(0o700)
    receipt_info = receipt_dir.stat()
    operation_id = "exec-" + "8" * 32
    receipt_path = receipt_dir / f"execution-{operation_id}.json"
    receipt = {
        "version": 1,
        "operation_id": operation_id,
        "owner_id": "fixture-confirmatory-owner",
        "kind": "qualification-confirmatory",
        "backend": "linux-subreaper-session-v1",
        "containment": "docker-container-v1",
        "spec_sha256": "sha256:" + "8" * 64,
        "timeout_s": 10.0,
        "term_grace_s": 1.0,
        "prepared_at_unix": 0.25,
        "receipt_dir_dev": receipt_info.st_dev,
        "receipt_dir_ino": receipt_info.st_ino,
        "fenced_by_instance_lease": True,
        "context": {
            **QR._confirmatory_context(), "log_name": "confirmatory.log",
        },
        "state": "terminal",
        "outcome": "exit",
        "returncode": 0,
        "group_drained": True,
        "term_sent": False,
        "kill_sent": False,
        "finished_at_unix": 0.75,
        "sandbox": {
            "backend": "docker-container-v1",
            "engine_path": "/usr/bin/docker",
            "engine_host": "unix:///var/run/docker.sock",
            "engine_sha256": "sha256:" + "7" * 64,
            "container_name": "mr-confirmatory-fixture",
            "token": "7" * 32,
            "spec_sha256": "sha256:" + "8" * 64,
            "network_mode": "none",
            "rootfs_readonly": True,
            "no_new_privileges": True,
            "cap_drop_all": True,
            "pid_namespace": True,
            "resource_mode": "rlimit-fallback",
            "container_drained": True,
        },
    }
    atomic_write_receipt(receipt_path, receipt)
    receipt_raw = receipt_path.read_bytes()
    QR.validate_execution_receipt(receipt, receipt_path)
    result = {
        "version": 1, "protocol": QR.CONFIRMATORY_RESULT_PROTOCOL,
        "task": "T1", "status": "success",
        "contract_sha256": firewall.contract_sha256,
        "claim_sha256": claim_hash,
        "claim_boundary_sha256": boundary_hash,
        "source_tree_sha256": _boundary["source_tree_sha256"],
        "runtime_identity_sha256": _FakeSandbox.runtime_identity_hash,
        "gpu_canary_sha256": (
            "sha256:" + "9" * 64 if firewall.final["gpu_required"] else None),
        "mechanical_scope": dict(QR._CONFIRMATORY_MECHANICAL_SCOPE),
        "output": {
            "path": str(output_path), "sha256": _sha(output_raw),
            "bytes": len(output_raw),
        },
        "execution": {
            "path": str(receipt_path), "sha256": _sha(receipt_raw),
        },
        "promotion": promotion,
        "failure": None, "finished_at_unix": 1.0,
    }
    result_raw = _canonical(result)
    _publish_once(spent_path, _canonical({
        "version": 1, "protocol": QR.CONFIRMATORY_SPENT_PROTOCOL,
        "task": "T1", "contract_sha256": firewall.contract_sha256,
        "claim_sha256": claim_hash,
        "claim_boundary_sha256": boundary_hash,
        "source_tree_sha256": result["source_tree_sha256"],
        "runtime_identity_sha256": result["runtime_identity_sha256"],
        "gpu_canary_sha256": result["gpu_canary_sha256"],
        "execution_context": QR._confirmatory_context(),
        "spent_at_unix": 0.5,
    }))
    _publish_once(result_path, result_raw)
    checks = {name: True for name in QR._CONFIRMATORY_AUDIT_CHECKS}
    audit_input = {
        "version": 1, "protocol": QR.CONFIRMATORY_AUDIT_INPUT_PROTOCOL,
        "task": "T1", "claim_boundary_sha256": boundary_hash,
        "confirmatory_result_sha256": _sha(result_raw),
        "auditor": "fixture root evaluator", "checks": checks,
        "evidence": [
            {"check": name, "ref": f"fixture://{name}",
             "sha256": "sha256:" + hashlib.sha256(name.encode()).hexdigest()}
            for name in sorted(checks)
        ],
        "notes": "fixture admission for final-boundary tests",
        "reviewed_at_unix": 1.0,
    }
    audit_input_raw = _canonical(audit_input)
    operator_input = work.parent / "fixture-confirmatory-audit-input.json"
    operator_input.write_bytes(audit_input_raw)
    operator_input.chmod(0o400)
    authority = work.parent / "confirmatory-audit-authority.json"
    lease = QR.InstanceLease.acquire(work)
    assert lease.close() is None
    checked = QR.audit_confirmatory(
        work_root=work, audit_input_path=operator_input,
        authority_path=authority)
    assert checked["status"] == "passed"


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


def _setup(
        tmp_path, *, truth_threshold=3.0, gpu_required=False,
        confirmatory_admitted=True):
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
    (source / "confirm.py").write_text("# frozen confirmatory batch\n", encoding="utf-8")
    (source / "confirm.py").chmod(0o444)
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
    if confirmatory_admitted:
        _seed_confirmatory_admission(work)
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


class _ConfirmatoryFakeSandbox(_FakeSandbox):
    def prepare(self, command, **kwargs):
        context = kwargs["execution_context"]
        if context["phase"] == "qualification-confirmatory":
            assert context == {
                **QR._confirmatory_context(), "log_name": "confirmatory.log",
            }
            assert kwargs["gpu_required"] is False
            assert command.count("--data") == 3
            assert not any("dreamer-x" in token.casefold() for token in command)
            return _FakeInvocation()
        return super().prepare(command, **kwargs)


def _stub_confirmatory_execution_receipt(monkeypatch, work: Path):
    reference = {
        "path": str(
            work / "state" / "executions" / "execution-confirmatory.json"),
        "sha256": "sha256:" + "7" * 64,
    }
    monkeypatch.setattr(
        QR, "_confirmatory_execution_reference",
        lambda *_args, **_kwargs: dict(reference))
    monkeypatch.setattr(
        QR, "_read_confirmatory_execution_receipt",
        lambda *_args, **_kwargs: {
            "kind": "qualification-confirmatory", "state": "terminal",
            "outcome": "exit", "returncode": 0,
        })


def _stage_run(claim, calls, *, confirmatory_output=None):
    output = (
        _confirmatory_output(claim)
        if confirmatory_output is None else confirmatory_output)

    def fake_run(argv, *, staging_dir, **kwargs):
        phase = kwargs["execution_context"]["phase"]
        calls.append(phase)
        if phase == "qualification-confirmatory":
            root = Path(staging_dir)
            root.mkdir(parents=True, exist_ok=True)
            output_raw = _canonical(output)
            (root / "confirmatory.json").write_bytes(output_raw)
            firewall = load_qualification_firewall(root.parents[3])
            _write_confirmatory_promotion(firewall, output_raw)
            (root / "confirmatory.log").write_text(
                "confirmatory batch complete\n", encoding="utf-8")
            return {
                "exit_code": 0,
                "log_path": str(root / "confirmatory.log"),
                "log_sha256": "0" * 64,
                "process_receipt_path": "fixture-receipt.json",
            }
        return _fake_successful_predictor(
            argv, staging_dir=staging_dir, **kwargs)

    return fake_run


def _write_audit_review(
        tmp_path: Path, work: Path, *, passed: bool = True,
        suffix: str = "review"):
    firewall = load_qualification_firewall(work)
    _boundary, boundary_raw = firewall.read_claim_boundary()
    _spent, result_path, _run, _ref, _copy = QR._confirmatory_paths(firewall)
    result_raw = result_path.read_bytes()
    result = json.loads(result_raw)
    checks = {name: True for name in QR._CONFIRMATORY_AUDIT_CHECKS}
    if not passed:
        checks["heldout_label_isolation_verified"] = False
    review = {
        "version": 1, "protocol": QR.CONFIRMATORY_AUDIT_INPUT_PROTOCOL,
        "task": "T1", "claim_boundary_sha256": _sha(boundary_raw),
        "confirmatory_result_sha256": _sha(result_raw),
        "auditor": "root scientific evaluator", "checks": checks,
        "evidence": [
            {
                "check": name, "ref": f"evidence://{suffix}/{name}",
                "sha256": "sha256:" + hashlib.sha256(
                    f"{suffix}:{name}".encode()).hexdigest(),
            }
            for name in sorted(checks)
        ],
        "notes": "pre-D scientific audit fixture",
        "reviewed_at_unix": float(result["finished_at_unix"]) + 0.001,
    }
    input_path = tmp_path / f"audit-input-{suffix}.json"
    input_path.write_bytes(_canonical(review))
    input_path.chmod(0o400)
    return input_path, tmp_path / f"audit-authority-{suffix}.json"


def test_t1_confirmatory_runs_once_audits_then_admits_sealed_final(
        tmp_path, monkeypatch):
    system, work, source, _holdout = _setup(
        tmp_path, confirmatory_admitted=False)
    firewall = load_qualification_firewall(work)
    claim, _claim_raw = firewall.read_claim_lock()
    calls = []
    monkeypatch.setattr(QR, "DockerExecutionSandbox", _ConfirmatoryFakeSandbox)
    monkeypatch.setattr(QR.H, "run_staged", _stage_run(claim, calls))
    _stub_confirmatory_execution_receipt(monkeypatch, work)

    first = QR.run_confirmatory(
        system_root=system, work_root=work, source_root=source)
    replayed = QR.run_confirmatory(
        system_root=system, work_root=work, source_root=source)
    assert first["status"] == "success"
    assert replayed == first
    assert calls == ["qualification-confirmatory"]

    audit_input, authority = _write_audit_review(tmp_path, work)
    audit = QR.audit_confirmatory(
        work_root=work, audit_input_path=audit_input,
        authority_path=authority)
    assert audit["status"] == "passed"

    final = QR.run_final(
        system_root=system, work_root=work, source_root=source)
    assert final["success_count"] == 1
    assert calls == ["qualification-confirmatory", "qualification-final"]
    marker = firewall.read_final_marker()
    assert marker["confirmatory_audit_sha256"] == _sha(authority.read_bytes())


def test_t1_successful_confirmatory_without_audit_rejects_final_before_sandbox(
        tmp_path, monkeypatch):
    system, work, source, _holdout = _setup(
        tmp_path, confirmatory_admitted=False)
    firewall = load_qualification_firewall(work)
    claim, _claim_raw = firewall.read_claim_lock()
    calls = []
    monkeypatch.setattr(QR, "DockerExecutionSandbox", _ConfirmatoryFakeSandbox)
    monkeypatch.setattr(QR.H, "run_staged", _stage_run(claim, calls))
    _stub_confirmatory_execution_receipt(monkeypatch, work)
    assert QR.run_confirmatory(
        system_root=system, work_root=work,
        source_root=source)["status"] == "success"

    monkeypatch.setattr(
        QR, "DockerExecutionSandbox",
        lambda **_kwargs: pytest.fail("stage D sandbox constructed before audit"))
    with pytest.raises(QR.QualificationRunnerError, match="audit"):
        QR.run_final(system_root=system, work_root=work, source_root=source)
    assert calls == ["qualification-confirmatory"]


def test_confirmatory_rejects_explore_view_drift_before_sandbox(
        tmp_path, monkeypatch):
    system, work, source, _holdout = _setup(
        tmp_path, confirmatory_admitted=False)
    payload = tmp_path / "SEED" / "data.bin"
    payload.chmod(0o600)
    payload.write_bytes(b"drifted after B\n")
    payload.chmod(0o400)
    monkeypatch.setattr(
        QR, "DockerExecutionSandbox",
        lambda **_kwargs: pytest.fail("sandbox constructed after explore drift"))

    with pytest.raises(QR.QualificationRunnerError, match="explore views.*漂移"):
        QR.run_confirmatory(
            system_root=system, work_root=work, source_root=source)


def test_confirmatory_rejects_preseeded_candidate_output_before_spend(
        tmp_path, monkeypatch):
    system, work, source, _holdout = _setup(
        tmp_path, confirmatory_admitted=False)
    firewall = load_qualification_firewall(work)
    claim, _claim_raw = firewall.read_claim_lock()
    spent_path, _result, run_root, _ref, _copy = (
        QR._confirmatory_paths(firewall))
    run_root.mkdir(parents=True)
    (run_root / "confirmatory.json").write_bytes(
        _canonical(_confirmatory_output(claim)))
    monkeypatch.setattr(QR, "DockerExecutionSandbox", _ConfirmatoryFakeSandbox)
    monkeypatch.setattr(
        QR.H, "run_staged",
        lambda *_args, **_kwargs: pytest.fail("preseeded C reached spawn"))

    with pytest.raises(QR.QualificationRunnerError, match="预置 candidate-output"):
        QR.run_confirmatory(
            system_root=system, work_root=work, source_root=source)
    assert not spent_path.exists()


def test_confirmatory_requires_exact_sandbox_promotion_ledger(
        tmp_path, monkeypatch):
    system, work, source, _holdout = _setup(
        tmp_path, confirmatory_admitted=False)
    firewall = load_qualification_firewall(work)
    claim, _claim_raw = firewall.read_claim_lock()
    calls = []

    def fake_run(_argv, *, staging_dir, **kwargs):
        calls.append(kwargs["execution_context"]["phase"])
        root = Path(staging_dir)
        root.mkdir(parents=True, exist_ok=True)
        (root / "confirmatory.json").write_bytes(
            _canonical(_confirmatory_output(claim)))
        (root / "confirmatory.log").write_text("no promotion receipt\n")
        return {
            "exit_code": 0, "log_path": str(root / "confirmatory.log"),
            "log_sha256": "0" * 64,
            "process_receipt_path": "fixture-receipt.json",
        }

    monkeypatch.setattr(QR, "DockerExecutionSandbox", _ConfirmatoryFakeSandbox)
    monkeypatch.setattr(QR.H, "run_staged", fake_run)
    _stub_confirmatory_execution_receipt(monkeypatch, work)
    result = QR.run_confirmatory(
        system_root=system, work_root=work, source_root=source)
    assert result["status"] == "failed"
    assert "promoted receipt" in result["failure"]
    assert QR.run_confirmatory(
        system_root=system, work_root=work, source_root=source) == result
    assert calls == ["qualification-confirmatory"]


def test_failed_confirmatory_audit_is_immutable_and_rejects_final(
        tmp_path, monkeypatch):
    system, work, source, _holdout = _setup(
        tmp_path, confirmatory_admitted=False)
    firewall = load_qualification_firewall(work)
    claim, _claim_raw = firewall.read_claim_lock()
    calls = []
    monkeypatch.setattr(QR, "DockerExecutionSandbox", _ConfirmatoryFakeSandbox)
    monkeypatch.setattr(QR.H, "run_staged", _stage_run(claim, calls))
    _stub_confirmatory_execution_receipt(monkeypatch, work)
    QR.run_confirmatory(
        system_root=system, work_root=work, source_root=source)
    audit_input, authority = _write_audit_review(
        tmp_path, work, passed=False, suffix="failed")
    audit = QR.audit_confirmatory(
        work_root=work, audit_input_path=audit_input,
        authority_path=authority)
    assert audit["status"] == "failed"

    monkeypatch.setattr(
        QR, "DockerExecutionSandbox",
        lambda **_kwargs: pytest.fail("failed audit reached stage D sandbox"))
    with pytest.raises(QR.QualificationRunnerError, match="永久拒绝"):
        QR.run_final(system_root=system, work_root=work, source_root=source)

    passed_input, replacement_authority = _write_audit_review(
        tmp_path, work, passed=True, suffix="replacement")
    _spent, _result, _run, audit_ref_path, audit_copy_path = (
        QR._confirmatory_paths(firewall))
    audit_ref_path.unlink()
    audit_copy_path.unlink()
    decision_path = QR._confirmatory_decision_path(
        firewall, create_directory=False)
    assert json.loads(decision_path.read_text(encoding="utf-8"))["status"] == "failed"

    with pytest.raises(
            QR.QualificationRunnerError,
            match="root decision ledger 已存在且内容冲突"):
        QR.audit_confirmatory(
            work_root=work, audit_input_path=passed_input,
            authority_path=replacement_authority)
    assert not replacement_authority.exists()
    assert not audit_ref_path.exists()
    assert not audit_copy_path.exists()
    assert json.loads(decision_path.read_text(encoding="utf-8"))["status"] == "failed"


def test_confirmatory_audit_rejects_pre_result_time_and_relative_authority(
        tmp_path, monkeypatch):
    system, work, source, _holdout = _setup(
        tmp_path, confirmatory_admitted=False)
    firewall = load_qualification_firewall(work)
    claim, _claim_raw = firewall.read_claim_lock()
    calls = []
    monkeypatch.setattr(QR, "DockerExecutionSandbox", _ConfirmatoryFakeSandbox)
    monkeypatch.setattr(QR.H, "run_staged", _stage_run(claim, calls))
    _stub_confirmatory_execution_receipt(monkeypatch, work)
    QR.run_confirmatory(
        system_root=system, work_root=work, source_root=source)

    early_input, _authority = _write_audit_review(
        tmp_path, work, suffix="early")
    early = json.loads(early_input.read_text(encoding="utf-8"))
    early["reviewed_at_unix"] = 1.0
    early_input.chmod(0o600)
    early_input.write_bytes(_canonical(early))
    early_input.chmod(0o400)
    with pytest.raises(QR.QualificationRunnerError, match="早于 terminal result"):
        QR.audit_confirmatory(
            work_root=work, audit_input_path=early_input,
            authority_path=tmp_path / "unused-authority.json")

    valid_input, _authority = _write_audit_review(
        tmp_path, work, suffix="relative")
    with pytest.raises(QR.QualificationRunnerError, match="canonical absolute"):
        QR.audit_confirmatory(
            work_root=work, audit_input_path=valid_input,
            authority_path=Path("relative-audit-authority.json"))
    _spent, _result, _run, audit_ref_path, audit_copy_path = (
        QR._confirmatory_paths(firewall))
    assert not audit_ref_path.exists()
    assert not audit_copy_path.exists()
    decision_directory = QR._confirmatory_decision_directory(
        firewall, create=False)
    assert not decision_directory.exists()


def test_confirmatory_audit_conflicting_authority_leaves_no_local_chain(
        tmp_path, monkeypatch):
    system, work, source, _holdout = _setup(
        tmp_path, confirmatory_admitted=False)
    firewall = load_qualification_firewall(work)
    claim, _claim_raw = firewall.read_claim_lock()
    monkeypatch.setattr(QR, "DockerExecutionSandbox", _ConfirmatoryFakeSandbox)
    monkeypatch.setattr(QR.H, "run_staged", _stage_run(claim, []))
    _stub_confirmatory_execution_receipt(monkeypatch, work)
    QR.run_confirmatory(
        system_root=system, work_root=work, source_root=source)

    audit_input, authority = _write_audit_review(
        tmp_path, work, suffix="authority-conflict")
    authority.write_bytes(_canonical({"unrelated": "root authority"}))
    authority.chmod(0o444)
    with pytest.raises(QR.QualificationRunnerError, match="内容冲突"):
        QR.audit_confirmatory(
            work_root=work, audit_input_path=audit_input,
            authority_path=authority)

    _spent, _result, _run, audit_ref_path, audit_copy_path = (
        QR._confirmatory_paths(firewall))
    assert not audit_ref_path.exists()
    assert not audit_copy_path.exists()
    assert not QR._confirmatory_decision_directory(
        firewall, create=False).exists()


def test_failed_audit_decision_precedes_authority_and_survives_crash(
        tmp_path, monkeypatch):
    system, work, source, _holdout = _setup(
        tmp_path, confirmatory_admitted=False)
    firewall = load_qualification_firewall(work)
    claim, _claim_raw = firewall.read_claim_lock()
    monkeypatch.setattr(QR, "DockerExecutionSandbox", _ConfirmatoryFakeSandbox)
    monkeypatch.setattr(QR.H, "run_staged", _stage_run(claim, []))
    _stub_confirmatory_execution_receipt(monkeypatch, work)
    QR.run_confirmatory(
        system_root=system, work_root=work, source_root=source)

    failed_input, failed_authority = _write_audit_review(
        tmp_path, work, passed=False, suffix="failed-before-crash")
    original_publish = QR._publish_root_authority

    def crash_before_external(path, payload, *, firewall, label=(
            "confirmatory audit authority")):
        if label == "confirmatory audit authority":
            raise RuntimeError("fixture crash before external authority")
        return original_publish(
            path, payload, firewall=firewall, label=label)

    monkeypatch.setattr(QR, "_publish_root_authority", crash_before_external)
    with pytest.raises(RuntimeError, match="fixture crash"):
        QR.audit_confirmatory(
            work_root=work, audit_input_path=failed_input,
            authority_path=failed_authority)

    decision_path = QR._confirmatory_decision_path(
        firewall, create_directory=False)
    assert json.loads(decision_path.read_text(encoding="utf-8"))["status"] == "failed"
    assert not failed_authority.exists()
    _spent, _result, _run, audit_ref_path, audit_copy_path = (
        QR._confirmatory_paths(firewall))
    assert not audit_ref_path.exists()
    assert not audit_copy_path.exists()

    monkeypatch.setattr(QR, "_publish_root_authority", original_publish)
    passed_input, replacement_authority = _write_audit_review(
        tmp_path, work, passed=True, suffix="passed-after-crash")
    with pytest.raises(
            QR.QualificationRunnerError,
            match="root decision ledger 已存在且内容冲突"):
        QR.audit_confirmatory(
            work_root=work, audit_input_path=passed_input,
            authority_path=replacement_authority)
    assert not replacement_authority.exists()

    repaired = QR.audit_confirmatory(
        work_root=work, audit_input_path=failed_input,
        authority_path=failed_authority)
    assert repaired["status"] == "failed"
    assert failed_authority.exists()
    assert audit_ref_path.exists()
    assert audit_copy_path.exists()


def test_confirmatory_audit_rejects_oversized_derived_authority_before_publish(
        tmp_path, monkeypatch):
    system, work, source, _holdout = _setup(
        tmp_path, confirmatory_admitted=False)
    firewall = load_qualification_firewall(work)
    claim, claim_raw = firewall.read_claim_lock()
    monkeypatch.setattr(QR, "DockerExecutionSandbox", _ConfirmatoryFakeSandbox)
    monkeypatch.setattr(QR.H, "run_staged", _stage_run(claim, []))
    _stub_confirmatory_execution_receipt(monkeypatch, work)
    result = QR.run_confirmatory(
        system_root=system, work_root=work, source_root=source)

    audit_input, authority = _write_audit_review(
        tmp_path, work, suffix="near-limit")
    review = json.loads(audit_input.read_text(encoding="utf-8"))
    checks = sorted(review["checks"])

    def evidence(index, length):
        prefix = f"evidence://near-limit/{index:03d}/"
        assert len(prefix) <= length <= QR._MAX_AUDIT_TEXT_BYTES
        ref = prefix + "x" * (length - len(prefix))
        return {
            "check": checks[index % len(checks)], "ref": ref,
            "sha256": _sha(ref.encode("utf-8")),
        }

    fixed = [evidence(index, 15_800) for index in range(15)]
    review["notes"] = "n" * QR._MAX_AUDIT_TEXT_BYTES
    limit = QR._MAX_EVALUATOR_ARTIFACT_BYTES
    low, high = len("evidence://near-limit/015/"), QR._MAX_AUDIT_TEXT_BYTES
    input_raw = None
    while low <= high:
        middle = (low + high) // 2
        review["evidence"] = fixed + [evidence(15, middle)]
        candidate = _canonical(review)
        if len(candidate) <= limit:
            input_raw = candidate
            low = middle + 1
        else:
            high = middle - 1
    assert input_raw is not None and limit - 64 <= len(input_raw) <= limit
    review = json.loads(input_raw)

    _boundary, boundary_raw = firewall.read_claim_boundary()
    _spent, result_path, _run, audit_ref_path, audit_copy_path = (
        QR._confirmatory_paths(firewall))
    result_raw = result_path.read_bytes()
    expected_authority = {
        "version": 1, "protocol": QR.CONFIRMATORY_AUDIT_PROTOCOL,
        "task": "T1", "status": "passed",
        "contract_sha256": firewall.contract_sha256,
        "claim_sha256": _sha(claim_raw),
        "claim_boundary_sha256": _sha(boundary_raw),
        "confirmatory_result_sha256": _sha(result_raw),
        "confirmatory_output_sha256": result["output"]["sha256"],
        "audit_input_sha256": _sha(input_raw),
        "auditor": review["auditor"], "checks": review["checks"],
        "evidence": review["evidence"], "notes": review["notes"],
        "reviewed_at_unix": review["reviewed_at_unix"],
    }
    assert len(_canonical(expected_authority)) > limit
    audit_input.chmod(0o600)
    audit_input.write_bytes(input_raw)
    audit_input.chmod(0o400)

    with pytest.raises(QR.QualificationRunnerError, match="派生 authority 大小"):
        QR.audit_confirmatory(
            work_root=work, audit_input_path=audit_input,
            authority_path=authority)
    assert not authority.exists()
    assert not audit_ref_path.exists()
    assert not audit_copy_path.exists()


def test_confirmatory_spent_before_spawn_is_failed_and_never_reexecuted(
        tmp_path, monkeypatch):
    system, work, source, _holdout = _setup(
        tmp_path, confirmatory_admitted=False)
    calls = []
    monkeypatch.setattr(QR, "DockerExecutionSandbox", _ConfirmatoryFakeSandbox)

    def interrupted(*_args, **_kwargs):
        calls.append("spawn")
        raise QR.ExecutionSupervisorError("fixture interruption")

    monkeypatch.setattr(QR.H, "run_staged", interrupted)
    with pytest.raises(QR.ExecutionSupervisorError, match="fixture interruption"):
        QR.run_confirmatory(
            system_root=system, work_root=work, source_root=source)
    firewall = load_qualification_firewall(work)
    spent_path, result_path, _run, _ref, _copy = QR._confirmatory_paths(firewall)
    assert spent_path.exists() and not result_path.exists()

    monkeypatch.setattr(
        QR.H, "recover_staged_result", lambda **_kwargs: None)
    result = QR.run_confirmatory(
        system_root=system, work_root=work, source_root=source)
    assert result["status"] == "failed"
    assert "spent before" in result["failure"]
    assert calls == ["spawn"]


def test_confirmatory_malformed_fold_coverage_is_terminal_failure(
        tmp_path, monkeypatch):
    system, work, source, _holdout = _setup(
        tmp_path, confirmatory_admitted=False)
    firewall = load_qualification_firewall(work)
    claim, _claim_raw = firewall.read_claim_lock()
    malformed = _confirmatory_output(claim)
    malformed["folds"] = malformed["folds"][:-1]
    calls = []
    monkeypatch.setattr(QR, "DockerExecutionSandbox", _ConfirmatoryFakeSandbox)
    monkeypatch.setattr(
        QR.H, "run_staged",
        _stage_run(claim, calls, confirmatory_output=malformed))
    _stub_confirmatory_execution_receipt(monkeypatch, work)

    result = QR.run_confirmatory(
        system_root=system, work_root=work, source_root=source)
    assert result["status"] == "failed"
    assert "fold" in result["failure"]
    assert QR.run_confirmatory(
        system_root=system, work_root=work, source_root=source) == result
    assert calls == ["qualification-confirmatory"]


def test_confirmatory_reported_fold_failure_is_terminal(
        tmp_path, monkeypatch):
    system, work, source, _holdout = _setup(
        tmp_path, confirmatory_admitted=False)
    firewall = load_qualification_firewall(work)
    claim, _claim_raw = firewall.read_claim_lock()
    output = _confirmatory_output(claim)
    output["folds"][0].update({
        "status": "failed", "metrics": None, "failure": "fold failed",
    })
    calls = []
    monkeypatch.setattr(QR, "DockerExecutionSandbox", _ConfirmatoryFakeSandbox)
    monkeypatch.setattr(
        QR.H, "run_staged",
        _stage_run(claim, calls, confirmatory_output=output))
    _stub_confirmatory_execution_receipt(monkeypatch, work)

    result = QR.run_confirmatory(
        system_root=system, work_root=work, source_root=source)
    assert result["status"] == "failed"
    assert "reported failure" in result["failure"]
    assert calls == ["qualification-confirmatory"]


def test_tampered_external_confirmatory_audit_rejects_final_before_sandbox(
        tmp_path, monkeypatch):
    system, work, source, _holdout = _setup(tmp_path)
    firewall = load_qualification_firewall(work)
    _spent, _result, _run, audit_ref_path, _copy = (
        QR._confirmatory_paths(firewall))
    ref = json.loads(audit_ref_path.read_text(encoding="utf-8"))
    authority = Path(ref["path"])
    audit = json.loads(authority.read_text(encoding="utf-8"))
    audit["notes"] = "tampered"
    authority.chmod(0o600)
    authority.write_bytes(_canonical(audit))
    authority.chmod(0o444)
    monkeypatch.setattr(
        QR, "DockerExecutionSandbox",
        lambda **_kwargs: pytest.fail("tampered audit reached sandbox"))

    with pytest.raises(QR.QualificationRunnerError, match="hash 漂移"):
        QR.run_final(system_root=system, work_root=work, source_root=source)


def test_t1_d_revalidates_guardian_receipt_after_root_audit(
        tmp_path, monkeypatch):
    system, work, source, _holdout = _setup(tmp_path)
    firewall = load_qualification_firewall(work)
    _spent, result_path, _run, _ref, _copy = QR._confirmatory_paths(firewall)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    Path(result["execution"]["path"]).unlink()
    monkeypatch.setattr(
        QR, "DockerExecutionSandbox",
        lambda **_kwargs: pytest.fail("missing C receipt reached D sandbox"))

    with pytest.raises(QR.QualificationFirewallError, match="guardian receipt"):
        QR.run_final(system_root=system, work_root=work, source_root=source)
    assert not os.path.lexists(firewall.final_path)


def test_t1_d_revalidates_sandbox_promotion_after_root_audit(
        tmp_path, monkeypatch):
    system, work, source, _holdout = _setup(tmp_path)
    firewall = load_qualification_firewall(work)
    QR._confirmatory_promotion_path(firewall).unlink()
    monkeypatch.setattr(
        QR, "DockerExecutionSandbox",
        lambda **_kwargs: pytest.fail("missing C promotion reached D sandbox"))

    with pytest.raises(QR.QualificationFirewallError, match="promoted receipt"):
        QR.run_final(system_root=system, work_root=work, source_root=source)
    assert not os.path.lexists(firewall.final_path)


def test_t1_d_requires_immutable_root_decision_ledger(tmp_path, monkeypatch):
    system, work, source, _holdout = _setup(tmp_path)
    firewall = load_qualification_firewall(work)
    QR._confirmatory_decision_path(
        firewall, create_directory=False).unlink()
    monkeypatch.setattr(
        QR, "DockerExecutionSandbox",
        lambda **_kwargs: pytest.fail("missing root decision reached D sandbox"))

    with pytest.raises(QR.QualificationFirewallError, match="root decision ledger"):
        QR.run_final(system_root=system, work_root=work, source_root=source)
    assert not os.path.lexists(firewall.final_path)


def test_t2_rejects_confirmatory_run_and_audit_before_execution(
        tmp_path, monkeypatch):
    system, work, source, _folds, _sample_ids = _setup_t2(tmp_path)
    monkeypatch.setattr(
        QR, "DockerExecutionSandbox",
        lambda **_kwargs: pytest.fail("T2 constructed confirmatory sandbox"))
    with pytest.raises(QR.QualificationRunnerError, match="T2 不存在"):
        QR.run_confirmatory(
            system_root=system, work_root=work, source_root=source)
    with pytest.raises(QR.QualificationRunnerError, match="T2 不存在"):
        QR.audit_confirmatory(
            work_root=work, audit_input_path=tmp_path / "unused.json",
            authority_path=tmp_path / "unused-authority.json")


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
    assert marker["version"] == 3
    assert marker["claim_boundary_sha256"] == _sha(boundary_raw)
    assert marker["confirmatory_audit_sha256"].startswith("sha256:")
    assert marker["gpu_canary_sha256"] is not None
    evidence = validations[0][0]
    assert evidence["candidate_hash"] == QR._gpu_canary_candidate_hash(
        claim_sha256=marker["claim_sha256"],
        source_tree_sha256=marker["source_tree_sha256"],
        runtime_identity_sha256=marker["runtime_identity_sha256"],
        gpu_contract_sha256=_sha(_canonical(gpu_contract)),
        owner_id=candidate_owners[evidence["candidate_hash"]])

    canary_path = work / "state" / "qualification" / "final" / "gpu-canary.json"
    canary_path.unlink()
    with pytest.raises(QR.QualificationRunnerError, match="缺原始 canary"):
        QR.run_final(
            system_root=system, work_root=work, source_root=source,
            gpu_contract_path=gpu_path)


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
    _claim, claim_raw = firewall.read_claim_lock()
    _boundary, boundary_raw = firewall.read_claim_boundary()
    admission_hash = QR._require_confirmatory_admission(
        firewall=firewall, claim_sha256=_sha(claim_raw),
        boundary_sha256=_sha(boundary_raw))
    marker = consume_final(
        work, source_tree_sha256=source_hash,
        runtime_identity_sha256=_FakeSandbox.runtime_identity_hash,
        confirmatory_audit_sha256=admission_hash, now=1.0)
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
        ("run-confirmatory", {"status": "success"}, 0),
        ("run-confirmatory", {"status": "failed"}, 3),
        ("audit-confirmatory", {"status": "passed"}, 0),
        ("audit-confirmatory", {"status": "failed"}, 3),
        ("run-final", {"failure_count": 0}, 0),
        ("run-final", {"failure_count": 1}, 3),
        ("score-final", {"status": "success"}, 0),
        ("score-final", {"status": "failed"}, 3),
    ],
)
def test_cli_exit_code_reflects_scientific_outcome(
        tmp_path, monkeypatch, capsys, command, result, expected):
    if command == "run-confirmatory":
        monkeypatch.setattr(QR, "run_confirmatory", lambda **_kwargs: result)
        argv = [
            "--work-root", str(tmp_path / "work"), "run-confirmatory",
            "--system-root", str(tmp_path / "system"),
            "--source-root", str(tmp_path / "source"),
        ]
    elif command == "audit-confirmatory":
        monkeypatch.setattr(QR, "audit_confirmatory", lambda **_kwargs: result)
        argv = [
            "--work-root", str(tmp_path / "work"), "audit-confirmatory",
            "--audit-input", str(tmp_path / "audit-input.json"),
            "--authority-output", str(tmp_path / "audit-authority.json"),
        ]
    elif command == "run-final":
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
