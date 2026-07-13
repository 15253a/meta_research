"""CP11.4c.3c.1 sealed-holdout / fold / one-shot firewall attacks."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
import yaml

from orchestrator import run as run_module
from orchestrator import database as database_module
from orchestrator.execution_sandbox import DockerExecutionSandbox
from orchestrator.instance_lease import InstanceBusyError, InstanceLease
from orchestrator.qualification_firewall import (
    CLAIM_BOUNDARY_PROTOCOL,
    CLAIM_PROTOCOL,
    CONTRACT_PROTOCOL,
    QualificationClaimLockedError,
    QualificationFinalizedError,
    QualificationFirewallError,
    _publish_once,
    _read_regular,
    consume_final,
    install_contract,
    load_qualification_firewall,
    publish_claim_boundary,
    publish_claim_lock,
)
from orchestrator.storage_governance import CycleSnapshotPublisher


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


def _terminal_snapshot(
        work: Path, *, add_inflight: bool = False,
        add_unsnapshotted_terminal: bool = False, drift_status: bool = False,
        add_active_runner: bool = False):
    writer = database_module.connect(work / "research.sqlite")
    writer.execute(
        "INSERT INTO goal(id,version,text,predicate_json) VALUES (1,1,'g','{}')")
    writer.execute(
        "INSERT INTO cycle(id,goal_id,goal_ver,status,policy_version,finished_at) "
        "VALUES (1,1,1,'done','v0','2026-07-13T00:00:00Z')")
    writer.commit()
    publisher = CycleSnapshotPublisher(
        db_path=work / "research.sqlite", work_root=work)
    assert publisher.reconcile(startup=True) == ["c1"]
    if add_inflight:
        writer.execute(
            "INSERT INTO cycle(id,goal_id,goal_ver,status,policy_version) "
            "VALUES (2,1,1,'idea','v0')")
        writer.commit()
    if add_unsnapshotted_terminal:
        writer.execute(
            "INSERT INTO cycle(id,goal_id,goal_ver,status,policy_version,finished_at) "
            "VALUES (2,1,1,'done','v0','2026-07-13T00:01:00Z')")
        writer.commit()
    if drift_status:
        writer.execute("UPDATE cycle SET status='failed' WHERE id=1")
        writer.commit()
    if add_active_runner:
        writer.execute(
            "INSERT INTO runner_call(id,cycle_id,phase,purpose,status) "
            "VALUES (1,1,'reasoning','qualification-test','running')")
        writer.commit()
    writer.close()


def _boundary_inputs(tmp_path: Path, contract):
    source = tmp_path / "frozen-source"
    source.mkdir()
    script = source / "confirm.py"
    script.write_text("# frozen confirmatory batch\n", encoding="utf-8")
    script.chmod(0o444)
    command = {
        "argv": [
            "python", "{src}/confirm.py",
            *[token for mount in contract["mounts"] if mount["role"] == "explore"
              for token in ("--data", mount["path"])],
        ],
        "output": "confirmatory.json",
        "gpu_required": False,
    }
    return source, command


def _publish_fixture_boundary(work: Path, claim, *, source_hash=None):
    firewall = load_qualification_firewall(work)
    source_hash = source_hash or "sha256:" + "a" * 64
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
    return publish_claim_lock(work, claim)


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
    locked = _publish_fixture_boundary(work, claim)
    assert locked["claim_sha256"].startswith("sha256:")
    with pytest.raises(QualificationClaimLockedError, match="claim boundary"):
        firewall.assert_research_open()
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
    with pytest.raises(QualificationFirewallError, match="claim boundary"):
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
    _publish_fixture_boundary(work, _claim("T2"))
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


def test_claim_boundary_binds_source_snapshot_views_and_is_replay_safe(tmp_path):
    work = tmp_path / "work"
    contract = _t1_contract(tmp_path)
    install_contract(work, contract)
    _terminal_snapshot(work)
    source, command = _boundary_inputs(tmp_path, contract)
    claim = _claim("T1")
    held = InstanceLease.acquire(work)
    try:
        with pytest.raises(InstanceBusyError):
            publish_claim_boundary(
                work, claim, source_root=source, confirmatory_command=command)
    finally:
        assert held.close() is None
    first = publish_claim_boundary(
        work, claim, source_root=source, confirmatory_command=command)
    replayed = publish_claim_boundary(
        work, claim, source_root=source, confirmatory_command=command)

    assert replayed == first
    assert first["a_high_water"]["cycle_id"] == "c1"
    firewall = load_qualification_firewall(work)
    boundary, raw = firewall.read_claim_boundary()
    assert boundary["protocol"] == CLAIM_BOUNDARY_PROTOCOL
    assert boundary["claim_sha256"] == first["claim_sha256"]
    assert boundary["source_tree_sha256"] == first["source_tree_sha256"]
    assert len(boundary["explore_views"]) == 3
    assert len({item["tree_sha256"] for item in boundary["explore_views"]}) == 3
    assert first["claim_boundary_sha256"] == _sha(raw)
    with pytest.raises(QualificationClaimLockedError, match="boundary"):
        firewall.assert_research_open()

    source_file = source / "confirm.py"
    source_file.chmod(0o644)
    source_file.write_text("# drift\n", encoding="utf-8")
    source_file.chmod(0o444)
    with pytest.raises(QualificationFirewallError, match="冻结输入发生漂移"):
        publish_claim_boundary(
            work, claim, source_root=source, confirmatory_command=command)


def test_claim_boundary_rejects_inflight_a_before_publication(tmp_path):
    work = tmp_path / "work"
    contract = _t1_contract(tmp_path)
    install_contract(work, contract)
    _terminal_snapshot(work, add_inflight=True)
    source, command = _boundary_inputs(tmp_path, contract)
    with pytest.raises(QualificationFirewallError, match="在途 cycle"):
        publish_claim_boundary(
            work, _claim("T1"), source_root=source,
            confirmatory_command=command)
    firewall = load_qualification_firewall(work)
    assert not os.path.lexists(firewall.claim_boundary_path)
    assert not os.path.lexists(firewall.claim_path)


def test_claim_boundary_rejects_active_execution_after_terminal_cycle(tmp_path):
    work = tmp_path / "work"
    contract = _t1_contract(tmp_path)
    install_contract(work, contract)
    _terminal_snapshot(work, add_active_runner=True)
    source, command = _boundary_inputs(tmp_path, contract)
    with pytest.raises(QualificationFirewallError, match="在途 runner_call"):
        publish_claim_boundary(
            work, _claim("T1"), source_root=source,
            confirmatory_command=command)


@pytest.mark.parametrize(
    ("snapshot_kwargs", "message"),
    [
        ({"add_unsnapshotted_terminal": True}, "high-water 不一致"),
        ({"drift_status": True}, "status 与 durable snapshot"),
    ],
)
def test_claim_boundary_rejects_live_db_snapshot_drift(
        tmp_path, snapshot_kwargs, message):
    work = tmp_path / "work"
    contract = _t1_contract(tmp_path)
    install_contract(work, contract)
    _terminal_snapshot(work, **snapshot_kwargs)
    source, command = _boundary_inputs(tmp_path, contract)
    with pytest.raises(QualificationFirewallError, match=message):
        publish_claim_boundary(
            work, _claim("T1"), source_root=source,
            confirmatory_command=command)
    firewall = load_qualification_firewall(work)
    assert not os.path.lexists(firewall.claim_boundary_path)


def test_unbound_claim_and_final_are_rejected(tmp_path):
    import orchestrator.qualification_firewall as qualification_module

    work = tmp_path / "work"
    contract = _t1_contract(tmp_path)
    firewall = install_contract(work, contract)
    claim = _claim("T1")
    with pytest.raises(QualificationFirewallError, match="必须由已验证的 claim boundary"):
        publish_claim_lock(work, claim)

    _publish_once(firewall.claim_path, _canonical(claim))
    with pytest.raises(QualificationFirewallError, match="缺 claim boundary"):
        qualification_module.main(["--work-root", str(work), "verify"])
    with pytest.raises(QualificationFirewallError, match="claim boundary"):
        consume_final(
            work, source_tree_sha256="sha256:" + "a" * 64,
            runtime_identity_sha256="sha256:" + "c" * 64, now=1.0)
    assert not os.path.lexists(firewall.final_path)


def test_boundary_first_crash_closes_research_and_exact_replay_completes_claim(
        tmp_path, monkeypatch):
    import orchestrator.qualification_firewall as qualification_module

    work = tmp_path / "work"
    contract = _t1_contract(tmp_path)
    install_contract(work, contract)
    _terminal_snapshot(work)
    source, command = _boundary_inputs(tmp_path, contract)
    claim = _claim("T1")
    original_publish = qualification_module._publish_once
    fail_claim_once = {"armed": True}

    def crash_after_boundary(path, payload, **kwargs):
        if path.name == "claim-lock.json" and fail_claim_once["armed"]:
            fail_claim_once["armed"] = False
            raise RuntimeError("simulated crash after boundary")
        return original_publish(path, payload, **kwargs)

    monkeypatch.setattr(qualification_module, "_publish_once", crash_after_boundary)
    with pytest.raises(RuntimeError, match="simulated crash"):
        publish_claim_boundary(
            work, claim, source_root=source, confirmatory_command=command)
    firewall = load_qualification_firewall(work)
    assert os.path.lexists(firewall.claim_boundary_path)
    assert not os.path.lexists(firewall.claim_path)
    with pytest.raises(QualificationClaimLockedError, match="boundary"):
        firewall.assert_research_open()

    changed = _claim("T1")
    changed["claims"] = [{"id": "different", "text": "frozen claim"}]
    with pytest.raises(QualificationFirewallError, match="boundary hash"):
        publish_claim_lock(work, changed)

    result = publish_claim_boundary(
        work, claim, source_root=source, confirmatory_command=command)
    assert result["claim_sha256"].startswith("sha256:")
    firewall.read_claim_lock()


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
    _publish_fixture_boundary(work, _claim("T2"))
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


def test_qualification_rejects_custom_runner_and_post_claim_startup_before_docker(
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

    _publish_fixture_boundary(work, _claim("T1"))
    monkeypatch.setattr(
        run_module, "DockerExecutionSandbox",
        lambda **_kwargs: pytest.fail("Docker constructed after claim lock"))
    assembly["attack"] = True
    with pytest.raises(QualificationClaimLockedError, match="claim boundary"):
        run_module._assemble_system(**assembly, runner_factory=None)


def test_already_assembled_system_rechecks_boundary_before_advancing(tmp_path):
    work = tmp_path / "work"
    contract = _t1_contract(tmp_path)
    firewall = install_contract(work, contract)

    class NeverAdvance:
        last_stop_reason = None
        last_block_reason = None

        def run_cycles(self, _max_cycles):
            pytest.fail("advancer ran after claim boundary publication")

    system = run_module.System(
        advancer=NeverAdvance(), state=object(), daemon=object(),
        dual_mode="A", work_root=work,
        research_open_guard=firewall.assert_research_open)
    _publish_fixture_boundary(work, _claim("T1"))

    with pytest.raises(QualificationClaimLockedError, match="claim boundary"):
        system.run(1)
