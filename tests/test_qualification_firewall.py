"""CP11.4c.3c.1 sealed-holdout / fold / one-shot firewall attacks."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
import yaml

from orchestrator import run as run_module
from orchestrator.execution_sandbox import DockerExecutionSandbox
from orchestrator.qualification_firewall import (
    CLAIM_PROTOCOL,
    CONTRACT_PROTOCOL,
    QualificationFinalizedError,
    QualificationFirewallError,
    _read_regular,
    consume_final,
    install_contract,
    load_qualification_firewall,
    publish_claim_lock,
)


SYSTEM_ROOT = Path(__file__).resolve().parent.parent
POLICY = yaml.safe_load(
    (SYSTEM_ROOT / "policies" / "policy.yaml").read_text(encoding="utf-8"))


def _canonical(value):
    return (json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _sha(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _view(path: Path, *, task: str, role: str, dataset: str, fold=None,
          adapter: str) -> str:
    path.mkdir(parents=True, mode=0o755)
    payload = path / "payload.bin"
    payload.write_bytes(b"safe-view\n")
    payload.chmod(0o444)
    files = [{
        "path": "payload.bin", "sha256": _sha(payload.read_bytes()),
        "bytes": payload.stat().st_size,
    }]
    if task == "T1" and role == "sealed_holdout":
        manifest = _canonical({
            "label_rule": {
                "score": "valence", "threshold": 3.0,
                "comparison": "higher_is_positive", "neutral_policy": "drop",
            },
        })
        manifest_path = path / "manifest.json"
        manifest_path.write_bytes(manifest)
        manifest_path.chmod(0o444)
        files.append({
            "path": "manifest.json", "sha256": _sha(manifest),
            "bytes": len(manifest),
        })
    files.sort(key=lambda item: item["path"])
    receipt = _canonical({
        "version": 1, "protocol": "meta-research-qualification-view/v1",
        "task": task, "role": role, "dataset": dataset, "fold": fold,
        "adapter": adapter, "adapter_version": 1, "files": files,
    })
    target = path / "qualification-view.json"
    target.write_bytes(receipt)
    target.chmod(0o444)
    path.chmod(0o555)
    return _sha(receipt)


def _truth(path: Path):
    raw = _canonical({"version": 1, "secret": "labels-never-enter-research-mount"})
    path.write_bytes(raw)
    path.chmod(0o400)
    evaluator_uid = 65534 if os.geteuid() != 65534 else 65533
    if os.geteuid() != 0:
        pytest.skip("truth-owner boundary test requires root test runner")
    os.chown(path, evaluator_uid, os.getegid())
    return evaluator_uid, _sha(raw)


def _t1_contract(tmp_path: Path):
    mounts = []
    for dataset in ("SEED", "SEED-IV", "FACED"):
        root = tmp_path / f"explore-{dataset}"
        root.mkdir(mode=0o755)
        marker = root / "data.bin"
        marker.write_bytes(dataset.encode("utf-8"))
        marker.chmod(0o444)
        root.chmod(0o555)
        mounts.append({
            "path": str(root), "role": "explore", "dataset": dataset,
            "fold": None, "view_receipt_sha256": None,
        })
    dreamer = tmp_path / "safe-dreamer-x"
    dreamer_hash = _view(
        dreamer, task="T1", role="sealed_holdout", dataset="DREAMER",
        adapter="meta-research-dreamer-public-view")
    mounts.append({
        "path": str(dreamer), "role": "sealed_holdout", "dataset": "DREAMER",
        "fold": None, "view_receipt_sha256": dreamer_hash,
    })
    truth = tmp_path / "sealed-dreamer-truth.json"
    evaluator_uid, truth_hash = _truth(truth)
    return {
        "version": 1, "protocol": CONTRACT_PROTOCOL, "task": "T1",
        "research_uid": os.geteuid(), "evaluator_uid": evaluator_uid,
        "forbid_code_imports": True, "mounts": mounts,
        "sealed_truth": {"path": str(truth), "sha256": truth_hash},
        "final": {
            "classes": 2, "seeds": [], "folds": [], "unit_ids": ["dreamer"],
            "gpu_required": False,
        },
    }


def _t2_contract(tmp_path: Path):
    mounts = []
    for fold in range(1, 16):
        root = tmp_path / f"fold-{fold:02d}"
        receipt_hash = _view(
            root, task="T2", role="fold", dataset="SEED", fold=fold,
            adapter="meta-research-seed-public-view")
        mounts.append({
            "path": str(root), "role": "fold", "dataset": "SEED", "fold": fold,
            "view_receipt_sha256": receipt_hash,
        })
    truth = tmp_path / "sealed-seed-truth.json"
    evaluator_uid, truth_hash = _truth(truth)
    return {
        "version": 1, "protocol": CONTRACT_PROTOCOL, "task": "T2",
        "research_uid": os.geteuid(), "evaluator_uid": evaluator_uid,
        "forbid_code_imports": True, "mounts": mounts,
        "sealed_truth": {"path": str(truth), "sha256": truth_hash},
        "final": {
            "classes": 3, "seeds": [7, 17, 29], "folds": list(range(1, 16)),
            "unit_ids": [], "gpu_required": False,
        },
    }


def _policy(contract):
    value = json.loads(json.dumps(POLICY))
    paths = [item["path"] for item in contract["mounts"]]
    value["execution"]["path_allowlist"] = paths
    value["execution"]["sandbox"]["readonly_mounts"] = paths
    return value


def _claim(task: str):
    controls = (
        ["majority", "class-prior-random", "matched-random", "label-permutation",
         "subject-id-only", "dataset-id-only", "trial-id-only", "source-only-linear",
         "confidence-only", "preprocessing-consistency", "leakage-probe"]
        if task == "T1" else
        ["majority", "source-prior-random", "source-only-linear", "source-only-mlp",
         "source-only-deep", "single-best-source", "confidence-only", "label-shuffle",
         "trial-id-only"])
    datasets = ({
        "exploration": ["SEED", "SEED-IV", "FACED"],
        "confirmatory_lodo": ["SEED", "SEED-IV", "FACED"],
        "sealed_holdout": {
            "dataset": "DREAMER", "score": "valence",
            "comparison": "higher_is_positive",
            "threshold": 3, "neutral_policy": "drop",
        },
    } if task == "T1" else {
        "dataset": "SEED", "subjects": list(range(1, 16)), "classes": 3,
        "input": "1s-nonoverlap-DE-62x5", "normalization": "per-fold",
        "hpo_labels": "source-inner-loso", "final_seeds": [7, 17, 29],
        "final_folds": list(range(1, 16)),
    })
    command = ["python", "{src}/predict.py", "--data", "{data}"]
    if task == "T2":
        command += ["--seed", "{seed}", "--fold", "{fold}"]
    return {
        "version": 1, "protocol": CLAIM_PROTOCOL, "task": task,
        "claims": [{"id": "c1", "text": "frozen claim"}],
        "feature_operator": {"name": "frozen"}, "label_mapping": {"name": "locked"},
        "model": {"name": "locked"}, "preprocessing": {"name": "locked"},
        "hpo": {"space": "locked"}, "search_space": {"budget": 1},
        "primary_metrics": ["accuracy"], "statistical_tests": {"name": "locked"},
        "multiple_testing": {"method": "Holm"}, "exclusion_rules": {"rules": ["none"]},
        "controls": controls, "datasets": datasets,
        "final_command": {
            "argv": command, "output": "predictions.json", "gpu_required": False,
        },
    }


def test_t1_holdout_is_invisible_until_irreversible_final(tmp_path):
    work = tmp_path / "work"
    contract = _t1_contract(tmp_path)
    firewall = install_contract(work, contract)
    firewall.validate_policy(_policy(contract))
    explores = [item["path"] for item in contract["mounts"] if item["role"] == "explore"]
    holdout = [item["path"] for item in contract["mounts"]
               if item["role"] == "sealed_holdout"][0]
    firewall.authorize_mounts(explores, execution_context={"phase": "train"})
    with pytest.raises(QualificationFirewallError, match="DREAMER"):
        firewall.authorize_mounts([holdout], execution_context={"phase": "train"})

    claim = _claim("T1")
    locked = publish_claim_lock(work, claim)
    assert locked["claim_sha256"].startswith("sha256:")
    marker = consume_final(
        work, source_tree_sha256="sha256:" + "a" * 64,
        runtime_identity_sha256="sha256:" + "c" * 64, now=1.0)
    assert marker["units"] == [{"fold": None, "seed": None, "unit_id": "dreamer"}]
    firewall.authorize_mounts(
        [holdout], execution_context={"phase": "qualification-final"})
    with pytest.raises(QualificationFinalizedError, match="禁止恢复"):
        firewall.assert_research_open()
    with pytest.raises(QualificationFinalizedError, match="只允许"):
        firewall.authorize_mounts([holdout], execution_context={"phase": "eval"})
    with pytest.raises(QualificationFinalizedError, match="另一冻结输入"):
        consume_final(
            work, source_tree_sha256="sha256:" + "b" * 64,
            runtime_identity_sha256="sha256:" + "c" * 64, now=2.0)


def test_t2_only_one_fold_view_can_enter_any_candidate_container(tmp_path):
    work = tmp_path / "work"
    contract = _t2_contract(tmp_path)
    firewall = install_contract(work, contract)
    firewall.validate_policy(_policy(contract))
    folds = [item["path"] for item in contract["mounts"]]
    with pytest.raises(QualificationFirewallError, match="final folds"):
        firewall.authorize_mounts([folds[0]], execution_context={"phase": "train"})
    publish_claim_lock(work, _claim("T2"))
    consume_final(
        work, source_tree_sha256="sha256:" + "a" * 64,
        runtime_identity_sha256="sha256:" + "c" * 64, now=1.0)
    firewall.authorize_mounts(
        [folds[0]], execution_context={"phase": "qualification-final"})
    with pytest.raises(QualificationFirewallError, match="只允许一个"):
        firewall.authorize_mounts(
            folds[:2], execution_context={"phase": "qualification-final"})


def test_claim_lock_rejects_fourth_claim_missing_control_and_protocol_drift(tmp_path):
    work = tmp_path / "work"
    contract = _t1_contract(tmp_path)
    install_contract(work, contract)
    claim = _claim("T1")
    claim["claims"] *= 4
    with pytest.raises(QualificationFirewallError, match="1..3"):
        publish_claim_lock(work, claim)
    claim = _claim("T1")
    claim["controls"].remove("trial-id-only")
    with pytest.raises(QualificationFirewallError, match="mandatory"):
        publish_claim_lock(work, claim)
    claim = _claim("T1")
    claim["datasets"]["sealed_holdout"]["dataset"] = "DEAP"
    with pytest.raises(QualificationFirewallError, match="DREAMER"):
        publish_claim_lock(work, claim)


def test_contract_refuses_whole_root_overlap_policy_drift_and_view_tamper(tmp_path):
    contract = _t1_contract(tmp_path)
    # Mounting an ancestor of the sealed truth would make the label capability
    # visible regardless of argv role names.
    contract["mounts"][0]["path"] = str(tmp_path)
    with pytest.raises(QualificationFirewallError, match="祖先|sealed truth|只读|view"):
        install_contract(tmp_path / "work-overlap", contract)

    contract = _t2_contract(tmp_path / "second")
    work = tmp_path / "work-policy"
    firewall = install_contract(work, contract)
    policy = _policy(contract)
    policy["execution"]["path_allowlist"].append(str(tmp_path))
    with pytest.raises(QualificationFirewallError, match="精确 research mounts"):
        firewall.validate_policy(policy)
    receipt = Path(contract["mounts"][0]["path"]) / "qualification-view.json"
    receipt.chmod(0o644)
    receipt.write_text('{"tampered":true}\n', encoding="utf-8")
    with pytest.raises(QualificationFirewallError, match="hash 不符|身份/权限"):
        load_qualification_firewall(work)

    contract = _t2_contract(tmp_path / "third")
    work = tmp_path / "work-payload"
    install_contract(work, contract)
    payload = Path(contract["mounts"][0]["path"]) / "payload.bin"
    payload.chmod(0o644)
    payload.write_bytes(b"tampered\n")
    payload.chmod(0o444)
    with pytest.raises(QualificationFirewallError, match="exact ledger"):
        load_qualification_firewall(work)


def test_sandbox_spec_contains_only_referenced_fold_not_all_fifteen(tmp_path):
    work = tmp_path / "work"
    contract = _t2_contract(tmp_path)
    install_contract(work, contract)
    publish_claim_lock(work, _claim("T2"))
    consume_final(
        work, source_tree_sha256="sha256:" + "a" * 64,
        runtime_identity_sha256="sha256:" + "c" * 64, now=1.0)
    config = _policy(contract)["execution"]["sandbox"]
    sandbox = DockerExecutionSandbox(
        work_root=work, config=config, system_root=SYSTEM_ROOT)
    sandbox._preflight_done = True
    sandbox._resource_mode = config["resource_mode"]
    first, second = contract["mounts"][0]["path"], contract["mounts"][1]["path"]
    invocation = sandbox.prepare(
        ["python", "-c", "pass", first], staging_dir=work / "run",
        log_name="fold.log", env=None, timeout_s=10,
        execution_context={"phase": "qualification-final", "log_name": "fold.log"})
    try:
        invocation.spec_file.seek(0)
        spec = json.loads(invocation.spec_file.read())
        assert spec["readonly_mounts"] == [
            {"source": first, "target": "/mr/readonly/0"}]
        assert spec["argv"][-1] == "/mr/readonly/0"
    finally:
        invocation.close()
    with pytest.raises(QualificationFirewallError, match="只允许一个"):
        sandbox.prepare(
            ["python", "-c", "pass", first, second], staging_dir=work / "bad",
            log_name="bad.log", env=None, timeout_s=10,
            execution_context={"phase": "qualification-final", "log_name": "bad.log"})


def test_publish_fallback_reconciles_exact_crash_left_hardlink(tmp_path):
    parent = tmp_path / "state"
    parent.mkdir()
    final = parent / "marker.json"
    temp = parent / ".marker.json.123.0123456789abcdef.tmp"
    raw = _canonical({"version": 1})
    temp.write_bytes(raw)
    temp.chmod(0o400)
    os.link(temp, final)
    assert os.lstat(final).st_nlink == 2

    assert _read_regular(
        final, label="test marker", expected_owner=os.geteuid(),
        expected_mode=0o400) == raw
    assert not temp.exists() and os.lstat(final).st_nlink == 1


def test_qualification_rejects_custom_runner_and_finalized_startup_before_docker(
        tmp_path, monkeypatch):
    work = tmp_path / "work"
    contract = _t1_contract(tmp_path)
    install_contract(work, contract)
    policy = _policy(contract)
    assembly = {
        "root": SYSTEM_ROOT, "work": work, "policy": policy, "schemas": None,
        "attack": False, "outbound_config": None,
        "import_search_provider": None, "reference_snapshot_provider": None,
        "instance_lease": None, "resource_closers": [],
    }
    with pytest.raises(ValueError, match="runner_factory"):
        run_module._assemble_system(
            **assembly, runner_factory=lambda *_args: object())

    publish_claim_lock(work, _claim("T1"))
    consume_final(
        work, source_tree_sha256="sha256:" + "a" * 64,
        runtime_identity_sha256="sha256:" + "c" * 64, now=1.0)
    monkeypatch.setattr(
        run_module, "DockerExecutionSandbox",
        lambda **_kwargs: pytest.fail("Docker constructed after final consumption"))
    assembly["attack"] = True
    with pytest.raises(QualificationFinalizedError, match="禁止恢复"):
        run_module._assemble_system(**assembly, runner_factory=None)
