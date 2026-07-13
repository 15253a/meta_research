"""One-shot predictor runner and independent scorer for qualification views.

The research-side command consumes the final capability before the first
predictor starts.  Each T1/T2 unit is also marked spent before spawn, so a
crash can recover durable output but can never silently execute that scientific
unit twice. Candidate code produces only canonical probabilities. A privileged
root evaluator later recomputes metrics from the sealed truth with trusted
in-process code; candidate stdout is never a metric authority.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import stat
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

import yaml

from . import harness as H
from .artifact_capability import (
    open_artifact,
    open_directory,
    read_artifact_bytes,
    verify_tree_fd,
)
from .deployment_preflight import validate_gpu_canary_evidence
from .execution_sandbox import DockerExecutionSandbox
from .instance_lease import InstanceLease, InstanceLeaseError, LOCK_NAME
from .process_supervisor import ExecutionSupervisor, ExecutionSupervisorError
from .qualification_firewall import (
    QualificationFirewall,
    QualificationFirewallError,
    _canonical,
    _hash_bytes,
    _publish_once,
    _read_regular,
    _reconcile_publish_link,
    _rename_noreplace,
    _strict_json,
    consume_final,
    final_units,
    load_qualification_firewall,
)


RUN_PROTOCOL = "meta-research-qualification-final-run/v1"
UNIT_SPENT_PROTOCOL = "meta-research-qualification-unit-spent/v1"
UNIT_RESULT_PROTOCOL = "meta-research-qualification-unit-result/v1"
SCORE_PROTOCOL = "meta-research-qualification-final-score/v1"
GPU_CANARY_CANDIDATE_PROTOCOL = (
    "meta-research-qualification-gpu-canary-candidate/v1")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_SOURCE_FILES = 10_000
_MAX_SOURCE_BYTES = 1024 * 1024 * 1024
_MAX_PREDICTION_BYTES = 8 * 1024 * 1024
_MAX_BATCH_PREDICTION_BYTES = 256 * 1024 * 1024
_SAFE_REL = re.compile(r"^[^\x00-\x1f\x7f\\]+$")


class QualificationRunnerError(QualificationFirewallError):
    """The frozen final batch cannot be safely executed or scored."""


def _bounded_error(error: BaseException | str) -> str:
    return str(error).replace("\x00", "?").replace("\r", " ").replace("\n", " ")[:1000]


def freeze_source_tree(root: Path | str) -> tuple[Dict[str, str], str]:
    """Hash one small, immutable, non-repository predictor source tree."""
    path = Path(os.path.abspath(os.fspath(root)))
    try:
        root_info = os.lstat(path)
    except OSError as error:
        raise QualificationRunnerError("final source tree 缺失") from error
    if (not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode)
            or os.path.realpath(path) != str(path) or root_info.st_uid != os.geteuid()
            or root_info.st_mode & 0o022):
        raise QualificationRunnerError("final source tree owner/路径/写权限非法")
    ledger: Dict[str, str] = {}
    total = 0
    for current, directories, files in os.walk(path, topdown=True, followlinks=False):
        current_path = Path(current)
        relative_dir = current_path.relative_to(path)
        if len(relative_dir.parts) > 32:
            raise QualificationRunnerError("final source tree 超目录深度")
        for name in sorted(directories):
            child = current_path / name
            info = os.lstat(child)
            if (name == ".git" or not _SAFE_REL.fullmatch(name)
                    or not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                    or info.st_uid != os.geteuid() or info.st_mode & 0o022):
                raise QualificationRunnerError("final source tree 含 repo/symlink/可写目录")
        for name in sorted(files):
            child = current_path / name
            rel = child.relative_to(path).as_posix()
            info = os.lstat(child)
            if (not _SAFE_REL.fullmatch(rel) or not stat.S_ISREG(info.st_mode)
                    or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.geteuid() or info.st_mode & 0o022):
                raise QualificationRunnerError("final source tree 含不安全文件")
            if len(ledger) >= _MAX_SOURCE_FILES or total + info.st_size > _MAX_SOURCE_BYTES:
                raise QualificationRunnerError("final source tree 超文件/bytes 上限")
            with open_artifact(child, label=f"qualification source:{rel}") as capability:
                ledger[rel] = capability.identity.content_hash
                total += capability.identity.size_bytes
    if not ledger:
        raise QualificationRunnerError("final source tree 不得为空")
    source_hash = _hash_bytes(_canonical({
        "files": [{"path": rel, "sha256": digest} for rel, digest in sorted(ledger.items())],
        "total_bytes": total,
    }))
    return ledger, source_hash


def _unit_data_root(firewall: QualificationFirewall, unit: Mapping[str, Any]) -> Path:
    if firewall.task == "T1":
        matches = [item.path for item in firewall.mounts if item.role == "sealed_holdout"]
    else:
        matches = [item.path for item in firewall.mounts if item.fold == unit["fold"]]
    if len(matches) != 1:
        raise QualificationRunnerError("final unit 未绑定唯一安全 data view")
    return matches[0]


def _render_command(
        argv: Sequence[str], *, source_proc: str, data_root: Path,
        unit: Mapping[str, Any]) -> list[str]:
    replacements = {
        "{src}": source_proc, "{data}": str(data_root),
        "{unit_id}": str(unit["unit_id"]),
        "{seed}": "" if unit["seed"] is None else str(unit["seed"]),
        "{fold}": "" if unit["fold"] is None else str(unit["fold"]),
    }
    result = []
    for token in argv:
        for source, target in replacements.items():
            token = token.replace(source, target)
        if re.search(r"\{[a-z_]+\}", token):
            raise QualificationRunnerError("final command 留有未知 placeholder")
        result.append(token)
    return result


def _unit_paths(firewall: QualificationFirewall, unit_id: str) -> tuple[Path, Path, Path]:
    base = firewall.work_root / "state" / "qualification" / "final"
    return (
        base / "units" / f"{unit_id}.spent.json",
        base / "units" / f"{unit_id}.result.json",
        base / "runs" / unit_id,
    )


def _unit_context(
        firewall: QualificationFirewall, unit: Mapping[str, Any]) -> Dict[str, Any]:
    units = final_units(firewall)
    try:
        owner_id = units.index(dict(unit)) + 1
    except ValueError as error:
        raise QualificationRunnerError("final unit 不在冻结全集") from error
    return {
        "phase": "qualification-final",
        "qualification_task": firewall.task.lower(),
        "unit_id": unit["unit_id"],
        "db_owner_kind": "qualification_final_unit",
        "db_owner_id": owner_id,
    }


def _load_canonical(path: Path, *, label: str, max_bytes: int = 256 * 1024) -> Dict[str, Any]:
    _reconcile_publish_link(path)
    raw = read_artifact_bytes(path, max_bytes=max_bytes, label=label)
    value = _strict_json(raw, label=label, max_bytes=max_bytes)
    if raw != _canonical(value):
        raise QualificationRunnerError(f"{label} 非 canonical")
    return value


def _public_sample_ids(
        firewall: QualificationFirewall, unit: Mapping[str, Any]) -> tuple[str, ...]:
    data_root = _unit_data_root(firewall, unit)
    if firewall.task == "T1":
        value = _load_canonical(
            data_root / "manifest.json", label="DREAMER public manifest",
            max_bytes=2 * 1024 * 1024)
        raw_ids = value.get("sample_ids")
        if (value.get("adapter") != "meta-research-dreamer-public-view"
                or value.get("adapter_version") != 1
                or value.get("record_count") != (
                    len(raw_ids) if isinstance(raw_ids, list) else -1)):
            raise QualificationRunnerError("DREAMER public manifest 身份/计数非法")
    else:
        value = _load_canonical(
            data_root / "target" / "sample_ids.json",
            label="SEED target sample ids", max_bytes=8 * 1024 * 1024)
        if set(value) != {"version", "fold", "sample_ids"}:
            raise QualificationRunnerError("SEED target sample-id manifest 字段闭包非法")
        if value.get("version") != 1 or value.get("fold") != unit["fold"]:
            raise QualificationRunnerError("SEED target sample-id manifest fold 错配")
        raw_ids = value["sample_ids"]
    if (not isinstance(raw_ids, list) or not raw_ids
            or any(not isinstance(item, str)
                   or re.fullmatch(r"[0-9a-f]{64}", item) is None for item in raw_ids)
            or len(set(raw_ids)) != len(raw_ids)):
        raise QualificationRunnerError("public sample IDs 非法")
    return tuple(raw_ids)


def _prediction_authority(path: Path, *, expected_owner: int, seal: bool = False) -> None:
    expected = Path(os.path.abspath(os.fspath(path)))
    info = os.lstat(expected)
    if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_nlink != 1 or info.st_uid != expected_owner
            or info.st_mode & 0o022):
        raise QualificationRunnerError("prediction authority owner/type/link/mode 非法")
    if seal:
        os.chmod(expected, 0o400, follow_symlinks=False)
        fd = os.open(
            expected, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        parent_fd = os.open(
            expected.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0))
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    elif stat.S_IMODE(info.st_mode) != 0o400:
        raise QualificationRunnerError("published prediction authority 须为 0400")


def _validate_prediction(
        path: Path, *, firewall: QualificationFirewall,
        unit: Mapping[str, Any]) -> tuple[Dict[str, Any], str, int]:
    expected_ids = _public_sample_ids(firewall, unit)
    classes = firewall.final["classes"]
    dynamic_limit = min(
        _MAX_PREDICTION_BYTES,
        64 * 1024 + len(expected_ids) * (96 + classes * 32))
    raw = read_artifact_bytes(
        path, max_bytes=dynamic_limit, label="qualification predictions")
    value = _strict_json(
        raw, label="qualification predictions", max_bytes=dynamic_limit)
    if raw != _canonical(value):
        raise QualificationRunnerError("predictions.json 须为 canonical JSON + newline")
    if (set(value) != {
            "version", "unit_id", "seed", "fold", "sample_ids", "probabilities"}
            or value.get("version") != 1 or value.get("unit_id") != unit["unit_id"]
            or value.get("seed") != unit["seed"] or value.get("fold") != unit["fold"]):
        raise QualificationRunnerError("predictions.json unit 身份/字段闭包非法")
    probabilities = value["probabilities"]
    sample_ids = value["sample_ids"]
    if (not isinstance(probabilities, list) or not probabilities
            or not isinstance(sample_ids, list) or len(sample_ids) != len(probabilities)
            or any(not isinstance(item, str)
                   or re.fullmatch(r"[0-9a-f]{64}", item) is None for item in sample_ids)
            or len(set(sample_ids)) != len(sample_ids)):
        raise QualificationRunnerError("predictions probabilities 须为非空数组")
    if len(sample_ids) != len(expected_ids) or set(sample_ids) != set(expected_ids):
        raise QualificationRunnerError(
            "prediction sample IDs 必须精确等于 public unit IDs")
    for row in probabilities:
        if not isinstance(row, list) or len(row) != classes:
            raise QualificationRunnerError("prediction row classes 错配")
        numbers = []
        for item in row:
            if type(item) is not float or not math.isfinite(item) or item < 0 or item > 1:
                raise QualificationRunnerError(
                    "prediction probability 须为 [0,1] 内有限 JSON float")
            numbers.append(item)
        if abs(sum(numbers) - 1.0) > 1e-6:
            raise QualificationRunnerError("prediction probability row 和不为 1")
    return value, _hash_bytes(raw), len(raw)


def _validate_output_allowlist(run_root: Path) -> None:
    allowed = {
        "predictions.json", "final.log", "final.log.exit", "final.log.process.json",
        ".sandbox-meta", ".sandbox-output",
    }
    extras = sorted(item.name for item in run_root.iterdir() if item.name not in allowed)
    if extras:
        raise QualificationRunnerError(
            f"final candidate 输出含 allowlist 外文件: {extras[:10]}")


def _read_unit_result(path: Path, *, firewall: QualificationFirewall,
                      unit: Mapping[str, Any]) -> Dict[str, Any]:
    value = _load_canonical(path, label="qualification unit result")
    if (set(value) != {
            "version", "protocol", "task", "unit", "status", "prediction",
            "execution", "failure", "finished_at_unix"}
            or value.get("version") != 1 or value.get("protocol") != UNIT_RESULT_PROTOCOL
            or value.get("task") != firewall.task or value.get("unit") != dict(unit)
            or value.get("status") not in {"success", "failed"}):
        raise QualificationRunnerError("qualification unit result 身份非法")
    if value["status"] == "success":
        prediction = value.get("prediction")
        _spent_path, _result_path, run_root = _unit_paths(
            firewall, str(unit["unit_id"]))
        expected_path = run_root / "predictions.json"
        if (not isinstance(prediction, dict)
                or set(prediction) != {"path", "sha256", "bytes"}
                or prediction.get("path") != str(expected_path)
                or not isinstance(prediction["sha256"], str)
                or _SHA256_RE.fullmatch(prediction["sha256"]) is None
                or isinstance(prediction.get("bytes"), bool)
                or not isinstance(prediction.get("bytes"), int)
                or prediction["bytes"] <= 0
                or value.get("failure") is not None):
            raise QualificationRunnerError("qualification success prediction authority 非法")
        _prediction_authority(
            expected_path, expected_owner=firewall.research_uid)
        parsed, digest, size = _validate_prediction(
            expected_path, firewall=firewall, unit=unit)
        del parsed
        if (digest, size) != (prediction["sha256"], prediction["bytes"]):
            raise QualificationRunnerError("qualification prediction/result hash 漂移")
    elif (value.get("prediction") is not None
          or not isinstance(value.get("failure"), str) or not value["failure"]):
        raise QualificationRunnerError("qualification failed unit result 字段非法")
    return value


def _publish_unit_result(
        path: Path, *, firewall: QualificationFirewall, unit: Mapping[str, Any],
        status: str, prediction: Optional[Mapping[str, Any]],
        execution: Optional[Mapping[str, Any]], failure: Optional[str]) -> Dict[str, Any]:
    value = {
        "version": 1, "protocol": UNIT_RESULT_PROTOCOL, "task": firewall.task,
        "unit": dict(unit), "status": status,
        "prediction": None if prediction is None else dict(prediction),
        "execution": None if execution is None else dict(execution),
        "failure": failure, "finished_at_unix": time.time(),
    }
    _publish_once(path, _canonical(value))
    return _read_unit_result(path, firewall=firewall, unit=unit)


def _validate_batch(
        value: Mapping[str, Any], *, firewall: QualificationFirewall,
        marker: Mapping[str, Any], claim_sha256: str) -> None:
    expected_keys = {
        "version", "protocol", "task", "contract_sha256", "claim_sha256",
        "source_tree_sha256", "runtime_identity_sha256", "gpu_canary_sha256",
        "final_marker_sha256", "units", "success_count", "failure_count",
        "finished_at_unix",
    }
    units = value.get("units")
    if (set(value) != expected_keys or value.get("version") != 1
            or value.get("protocol") != RUN_PROTOCOL
            or value.get("task") != firewall.task
            or value.get("contract_sha256") != firewall.contract_sha256
            or value.get("claim_sha256") != claim_sha256
            or value.get("source_tree_sha256") != marker["source_tree_sha256"]
            or value.get("runtime_identity_sha256") != marker["runtime_identity_sha256"]
            or value.get("gpu_canary_sha256") != marker["gpu_canary_sha256"]
            or value.get("final_marker_sha256") != _hash_bytes(_canonical(marker))
            or not isinstance(units, list)
            or len(units) != len(final_units(firewall))
            or isinstance(value.get("success_count"), bool)
            or not isinstance(value.get("success_count"), int)
            or isinstance(value.get("failure_count"), bool)
            or not isinstance(value.get("failure_count"), int)
            or value.get("success_count") != sum(
                isinstance(item, dict) and item.get("status") == "success"
                for item in units)
            or value.get("failure_count") != sum(
                isinstance(item, dict) and item.get("status") == "failed"
                for item in units)
            or value["success_count"] + value["failure_count"] != len(units)
            or isinstance(value.get("finished_at_unix"), bool)
            or not isinstance(value.get("finished_at_unix"), (int, float))
            or not math.isfinite(float(value["finished_at_unix"]))
            or float(value["finished_at_unix"]) <= 0):
        raise QualificationRunnerError("qualification final batch 字段/绑定/计数非法")


def _recover_spent_unit(
        *, firewall: QualificationFirewall, unit: Mapping[str, Any], run_root: Path,
        result_path: Path, sandbox: DockerExecutionSandbox,
        supervisor: ExecutionSupervisor) -> Dict[str, Any]:
    if os.path.lexists(result_path):
        return _read_unit_result(result_path, firewall=firewall, unit=unit)
    context = _unit_context(firewall, unit)
    try:
        recovered = H.recover_staged_result(
            staging_dir=str(run_root), log_name="final.log",
            execution_supervisor=supervisor, execution_kind="qualification-final",
            execution_context=context, execution_sandbox=sandbox,
            recover_completed=True, return_terminal_failure=True)
    except (InstanceLeaseError, ExecutionSupervisorError):
        raise
    except Exception as error:
        return _publish_unit_result(
            result_path, firewall=firewall, unit=unit, status="failed",
            prediction=None, execution=None,
            failure="spent recovery failed: " + _bounded_error(error))
    if recovered is None:
        return _publish_unit_result(
            result_path, firewall=firewall, unit=unit, status="failed",
            prediction=None, execution=None,
            failure="unit spent before a recoverable predictor start")
    execution = {
        "exit_code": recovered.get("exit_code"),
        "log_path": recovered.get("log_path"),
        "log_sha256": recovered.get("log_sha256"),
        "process_receipt_path": recovered.get("process_receipt_path"),
    }
    if recovered.get("exit_code") != 0:
        outcome = recovered.get("failure_outcome")
        return _publish_unit_result(
            result_path, firewall=firewall, unit=unit, status="failed",
            prediction=None, execution=execution,
            failure=(f"prior terminal outcome={outcome}"
                     if outcome else f"predictor exit={recovered.get('exit_code')}"))
    try:
        _validate_output_allowlist(run_root)
        _parsed, digest, size = _validate_prediction(
            run_root / "predictions.json", firewall=firewall, unit=unit)
        _prediction_authority(
            run_root / "predictions.json",
            expected_owner=firewall.research_uid, seal=True)
    except Exception as error:
        return _publish_unit_result(
            result_path, firewall=firewall, unit=unit, status="failed",
            prediction=None, execution=execution,
            failure="prediction rejected: " + _bounded_error(error))
    return _publish_unit_result(
        result_path, firewall=firewall, unit=unit, status="success",
        prediction={
            "path": str(run_root / "predictions.json"),
            "sha256": digest, "bytes": size,
        }, execution=execution, failure=None)


def _load_gpu_contract(path: Optional[Path]) -> Optional[Dict[str, Any]]:
    if path is None:
        return None
    return _load_canonical(path, label="qualification GPU contract")


def _gpu_canary_candidate_hash(
        *, claim_sha256: str, source_tree_sha256: str,
        runtime_identity_sha256: str, gpu_contract_sha256: str,
        owner_id: str) -> str:
    return _hash_bytes(_canonical({
        "protocol": GPU_CANARY_CANDIDATE_PROTOCOL,
        "claim_sha256": claim_sha256,
        "source_tree_sha256": source_tree_sha256,
        "runtime_identity_sha256": runtime_identity_sha256,
        "gpu_contract_sha256": gpu_contract_sha256,
        "owner_id": owner_id,
    }))


def _validate_gpu_canary(
        canary: Mapping[str, Any], *, work: Path, sandbox: Any,
        claim_sha256: str, source_tree_sha256: str,
        validation_time: float, expected_owner_id: Optional[str] = None
        ) -> Dict[str, Any]:
    candidate_hash = canary.get("candidate_hash")
    contract = getattr(sandbox, "gpu_contract", None)
    contract_hash = getattr(sandbox, "gpu_contract_hash", None)
    if (not isinstance(candidate_hash, str) or not isinstance(contract, Mapping)
            or not isinstance(contract_hash, str)):
        raise QualificationRunnerError("qualification GPU canary 缺 exact binding")
    try:
        receipt = validate_gpu_canary_evidence(
            canary, work_root=work, owner_id=expected_owner_id,
            sandbox_config=sandbox.config, contract=contract,
            candidate_hash=candidate_hash, now=validation_time,
            require_fence=True)
    except (OSError, TypeError, ValueError) as error:
        raise QualificationRunnerError(
            "qualification GPU canary authority 非法: "
            + _bounded_error(error)) from error
    expected_candidate = _gpu_canary_candidate_hash(
        claim_sha256=claim_sha256,
        source_tree_sha256=source_tree_sha256,
        runtime_identity_sha256=sandbox.runtime_identity_hash,
        gpu_contract_sha256=contract_hash,
        owner_id=receipt["owner_id"])
    if candidate_hash != expected_candidate:
        raise QualificationRunnerError(
            "qualification GPU canary 未绑定冻结输入/guardian owner")
    return receipt


def run_final(
        *, system_root: Path | str, work_root: Path | str, source_root: Path | str,
        gpu_contract_path: Optional[Path] = None) -> Dict[str, Any]:
    system = Path(os.path.abspath(os.fspath(system_root)))
    work = Path(os.path.abspath(os.fspath(work_root)))
    policy = yaml.safe_load((system / "policies" / "policy.yaml").read_text(encoding="utf-8"))
    lease = InstanceLease.acquire(work)
    supervisor = None
    primary: Optional[BaseException] = None
    try:
        firewall = load_qualification_firewall(
            work, policy=policy, require_research_uid=True)
        if firewall is None:
            raise QualificationRunnerError("work_root 未安装 qualification contract")
        claim, claim_raw = firewall.read_claim_lock()
        claim_hash = _hash_bytes(claim_raw)
        ledger, source_hash = freeze_source_tree(source_root)
        gpu_contract = _load_gpu_contract(gpu_contract_path)
        gpu_required = claim["final_command"]["gpu_required"]
        if gpu_required is not (gpu_contract is not None):
            raise QualificationRunnerError(
                "final GPU 模式与 --gpu-contract 必须精确一致")
        supervisor = ExecutionSupervisor(
            receipt_dir=work / "state" / "executions", owner_id=lease.owner_id,
            owner_guard=lease.assert_owned,
            fence_context_factory=lease.delegate_owner_fence)
        sandbox = DockerExecutionSandbox(
            work_root=work, config=policy["execution"]["sandbox"],
            owner_guard=lease.assert_owned, system_root=system,
            gpu_contract=gpu_contract, qualification_firewall=firewall)
        sandbox.preflight()
        supervisor.recover_previous_generation()
        sandbox.recover_terminal_sessions(supervisor)

        runtime_hash = sandbox.runtime_identity_hash
        canary_path = work / "state" / "qualification" / "final" / "gpu-canary.json"
        canary_hash = None
        final_marker_time = None
        if gpu_required:
            existing_marker = (
                firewall.read_final_marker()
                if os.path.lexists(firewall.final_path) else None)
            canary = None
            if os.path.lexists(canary_path):
                canary_raw = _read_regular(
                    canary_path, label="qualification GPU canary",
                    expected_owner=firewall.research_uid, expected_mode=0o400)
                canary = _strict_json(
                    canary_raw, label="qualification GPU canary")
                if canary_raw != _canonical(canary):
                    raise QualificationRunnerError(
                        "qualification GPU canary 非 canonical")
                if existing_marker is not None:
                    final_marker_time = float(existing_marker["consumed_at_unix"])
                    if _hash_bytes(canary_raw) != existing_marker["gpu_canary_sha256"]:
                        raise QualificationRunnerError(
                            "qualification GPU canary 与 final marker hash 错配")
                    _validate_gpu_canary(
                        canary, work=work, sandbox=sandbox,
                        claim_sha256=claim_hash,
                        source_tree_sha256=source_hash,
                        validation_time=final_marker_time)
                else:
                    checked = canary.get("checked_at_unix")
                    if (isinstance(checked, bool)
                            or not isinstance(checked, (int, float))
                            or not math.isfinite(float(checked))
                            or float(checked) <= 0):
                        raise QualificationRunnerError(
                            "qualification GPU canary checked_at 非法")
                    _validate_gpu_canary(
                        canary, work=work, sandbox=sandbox,
                        claim_sha256=claim_hash,
                        source_tree_sha256=source_hash,
                        validation_time=float(checked))
                    age = time.time() - float(checked)
                    if not 0 <= age <= 60:
                        # A fully authenticated but stale pre-final canary is
                        # safe to replace: no scientific unit has been spent.
                        canary_path.unlink()
                        parent_fd = os.open(
                            canary_path.parent,
                            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                            | getattr(os, "O_CLOEXEC", 0))
                        try:
                            os.fsync(parent_fd)
                        finally:
                            os.close(parent_fd)
                        canary = None
            if canary is None:
                candidate_hash = _gpu_canary_candidate_hash(
                    claim_sha256=claim_hash,
                    source_tree_sha256=source_hash,
                    runtime_identity_sha256=runtime_hash,
                    gpu_contract_sha256=sandbox.gpu_contract_hash,
                    owner_id=lease.owner_id)
                canary = sandbox.run_gpu_canary(
                    execution_supervisor=supervisor,
                    candidate_hash=candidate_hash)
                _validate_gpu_canary(
                    canary, work=work, sandbox=sandbox,
                    claim_sha256=claim_hash,
                    source_tree_sha256=source_hash,
                    validation_time=time.time(),
                    expected_owner_id=lease.owner_id)
                _publish_once(canary_path, _canonical(canary))
            canary_hash = _hash_bytes(_canonical(canary))
            if existing_marker is None:
                final_marker_time = time.time()
                _validate_gpu_canary(
                    canary, work=work, sandbox=sandbox,
                    claim_sha256=claim_hash,
                    source_tree_sha256=source_hash,
                    validation_time=final_marker_time)
        elif os.path.lexists(canary_path):
            raise QualificationRunnerError("CPU qualification 存在意外 GPU canary authority")

        marker = consume_final(
            work, source_tree_sha256=source_hash,
            runtime_identity_sha256=runtime_hash,
            gpu_canary_sha256=canary_hash,
            now=final_marker_time,
            validated_firewall=firewall)
        marker_hash = _hash_bytes(_canonical(marker))
        batch_path = work / "state" / "qualification" / "final" / "batch.json"
        if os.path.lexists(batch_path):
            existing_batch = _load_canonical(
                batch_path, label="qualification final batch", max_bytes=2 * 1024 * 1024)
            _validate_batch(
                existing_batch, firewall=firewall, marker=marker,
                claim_sha256=claim_hash)
            if (existing_batch.get("protocol") != RUN_PROTOCOL
                    or existing_batch.get("task") != firewall.task
                    or existing_batch.get("contract_sha256") != firewall.contract_sha256
                    or existing_batch.get("claim_sha256") != claim_hash
                    or existing_batch.get("source_tree_sha256") != source_hash
                    or existing_batch.get("runtime_identity_sha256") != runtime_hash
                    or existing_batch.get("gpu_canary_sha256") != canary_hash
                    or existing_batch.get("final_marker_sha256") != marker_hash):
                raise QualificationRunnerError("既有 qualification batch 与冻结输入冲突")
            verified_results = []
            for unit in final_units(firewall):
                _spent, result_path, _run_root = _unit_paths(
                    firewall, unit["unit_id"])
                verified_results.append(_read_unit_result(
                    result_path, firewall=firewall, unit=unit))
            if existing_batch.get("units") != verified_results:
                raise QualificationRunnerError("既有 qualification batch/unit receipts 漂移")
            return existing_batch
        results = []
        for unit in final_units(firewall):
            spent_path, result_path, run_root = _unit_paths(firewall, unit["unit_id"])
            spent = {
                "version": 1, "protocol": UNIT_SPENT_PROTOCOL, "task": firewall.task,
                "unit": unit, "final_marker_sha256": marker_hash,
                "claim_sha256": claim_hash,
                "source_tree_sha256": source_hash,
                "runtime_identity_sha256": runtime_hash,
                "gpu_canary_sha256": canary_hash,
                "execution_context": _unit_context(firewall, unit),
                "spent_at_unix": time.time(),
            }
            if os.path.lexists(spent_path):
                existing = _load_canonical(spent_path, label="qualification unit spent")
                stable_existing = {key: value for key, value in existing.items()
                                   if key != "spent_at_unix"}
                stable_new = {key: value for key, value in spent.items()
                              if key != "spent_at_unix"}
                if stable_existing != stable_new:
                    raise QualificationRunnerError("qualification unit spent identity 冲突")
                results.append(_recover_spent_unit(
                    firewall=firewall, unit=unit, run_root=run_root,
                    result_path=result_path, sandbox=sandbox, supervisor=supervisor))
                continue
            _publish_once(spent_path, _canonical(spent))
            source_fd = -1
            invocation = None
            context = _unit_context(firewall, unit)
            try:
                data_root = _unit_data_root(firewall, unit)
                source_fd = open_directory(
                    source_root, label="qualification final source")
                verify_tree_fd(
                    source_fd, ledger, label="qualification final source", exact=True)
                command = _render_command(
                    claim["final_command"]["argv"],
                    source_proc=f"/proc/self/fd/{source_fd}",
                    data_root=data_root, unit=unit)
                prepared_context = {**context, "log_name": "final.log"}
                invocation = sandbox.prepare(
                    command, staging_dir=run_root, log_name="final.log", env=None,
                    timeout_s=float(policy["execution"]["max_timeout_s"]),
                    tree_expectations=((source_fd, ledger, ()),),
                    execution_context=prepared_context,
                    execution_supervisor=supervisor,
                    gpu_required=gpu_required)
                run = H.run_staged(
                    invocation.argv, staging_dir=str(run_root), log_name="final.log",
                    timeout_s=float(policy["execution"]["max_timeout_s"]),
                    env=invocation.env, pass_fds=invocation.pass_fds,
                    execution_supervisor=supervisor,
                    execution_kind="qualification-final", execution_context=context,
                    sandbox_invocation=invocation)
                execution = {
                    "exit_code": run.get("exit_code"), "log_path": run.get("log_path"),
                    "log_sha256": run.get("log_sha256"),
                    "process_receipt_path": run.get("process_receipt_path"),
                }
                if run.get("exit_code") != 0:
                    result = _publish_unit_result(
                        result_path, firewall=firewall, unit=unit, status="failed",
                        prediction=None, execution=execution,
                        failure=f"predictor exit={run.get('exit_code')}")
                else:
                    _validate_output_allowlist(run_root)
                    _parsed, digest, size = _validate_prediction(
                        run_root / "predictions.json", firewall=firewall, unit=unit)
                    _prediction_authority(
                        run_root / "predictions.json",
                        expected_owner=firewall.research_uid, seal=True)
                    result = _publish_unit_result(
                        result_path, firewall=firewall, unit=unit, status="success",
                        prediction={
                            "path": str(run_root / "predictions.json"),
                            "sha256": digest, "bytes": size,
                        }, execution=execution, failure=None)
            except (InstanceLeaseError, ExecutionSupervisorError):
                raise
            except Exception as error:
                result = _publish_unit_result(
                    result_path, firewall=firewall, unit=unit, status="failed",
                    prediction=None, execution=None,
                    failure=f"{type(error).__name__}: {_bounded_error(error)}")
            finally:
                if invocation is not None:
                    invocation.close()
                if source_fd >= 0:
                    os.close(source_fd)
            results.append(result)

        batch = {
            "version": 1, "protocol": RUN_PROTOCOL, "task": firewall.task,
            "contract_sha256": firewall.contract_sha256,
            "claim_sha256": claim_hash,
            "source_tree_sha256": source_hash,
            "runtime_identity_sha256": runtime_hash,
            "gpu_canary_sha256": canary_hash,
            "final_marker_sha256": marker_hash,
            "units": results,
            "success_count": sum(item["status"] == "success" for item in results),
            "failure_count": sum(item["status"] == "failed" for item in results),
            "finished_at_unix": time.time(),
        }
        _publish_once(batch_path, _canonical(batch))
        published_batch = _load_canonical(
            batch_path, label="qualification final batch",
            max_bytes=2 * 1024 * 1024)
        _validate_batch(
            published_batch, firewall=firewall, marker=marker,
            claim_sha256=claim_hash)
        return published_batch
    except BaseException as error:
        primary = error
        raise
    finally:
        close_errors = []
        if supervisor is not None:
            try:
                supervisor.close(timeout_s=10.0)
            except BaseException as error:
                close_errors.append(error)
        lease_error = lease.close()
        if lease_error is not None:
            close_errors.append(lease_error)
        if close_errors:
            if primary is not None:
                note = getattr(primary, "add_note", None)
                if callable(note):
                    for error in close_errors:
                        note(f"qualification runner cleanup: {error}")
            else:
                raise close_errors[0]


def _score_lock(work_root: Path) -> int:
    work_info = os.lstat(work_root)
    if not stat.S_ISDIR(work_info.st_mode) or stat.S_ISLNK(work_info.st_mode):
        raise QualificationRunnerError("score work_root 非安全目录")
    fd = os.open(
        work_root / LOCK_NAME,
        os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        os.close(fd)
        raise QualificationRunnerError("final predictor/研究 owner 仍活跃，拒绝并发 score")
    return fd


def _publish_for_research_owner(
        path: Path, payload: bytes, *, research_uid: int) -> None:
    if os.geteuid() != 0:
        raise QualificationRunnerError("score-final 必须由 privileged root evaluator 执行")
    parent = path.parent
    parent_info = os.lstat(parent)
    if (not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(parent_info.st_mode)
            or parent_info.st_uid != research_uid):
        raise QualificationRunnerError("final score 发布目录 owner 非 research_uid")
    if os.path.lexists(path):
        current = _read_regular(
            path, label="qualification final score", expected_owner=research_uid,
            expected_mode=0o400)
        if current != payload:
            raise QualificationRunnerError("qualification final score 已存在且冲突")
        return
    tmp = parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    fd = os.open(
        tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("final score short write")
            view = view[written:]
        os.fchown(fd, research_uid, parent_info.st_gid)
        os.fchmod(fd, 0o400)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        _rename_noreplace(tmp, path)
        directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except FileExistsError:
        current = _read_regular(
            path, label="qualification final score",
            expected_owner=research_uid, expected_mode=0o400)
        if current != payload:
            raise QualificationRunnerError(
                "qualification final score 并发发布内容冲突")
    finally:
        tmp.unlink(missing_ok=True)


def _validate_score_result(
        value: Mapping[str, Any], *, firewall: QualificationFirewall,
        marker_hash: str, batch_hash: str, truth_hash: str) -> None:
    if (set(value) != {
            "version", "protocol", "task", "status", "contract_sha256",
            "final_marker_sha256", "batch_sha256", "truth_sha256",
            "prediction_hashes", "failed_units", "failed_unit_count",
            "metrics", "evaluation_error", "scored_at_unix"}
            or value.get("version") != 1 or value.get("protocol") != SCORE_PROTOCOL
            or value.get("task") != firewall.task
            or value.get("status") not in {"success", "failed"}
            or value.get("contract_sha256") != firewall.contract_sha256
            or value.get("final_marker_sha256") != marker_hash
            or value.get("batch_sha256") != batch_hash
            or value.get("truth_sha256") != truth_hash
            or not isinstance(value.get("prediction_hashes"), list)
            or not isinstance(value.get("failed_units"), list)
            or isinstance(value.get("failed_unit_count"), bool)
            or not isinstance(value.get("failed_unit_count"), int)
            or value["failed_unit_count"] != len(value["failed_units"])
            or isinstance(value.get("scored_at_unix"), bool)
            or not isinstance(value.get("scored_at_unix"), (int, float))
            or not math.isfinite(float(value["scored_at_unix"]))
            or float(value["scored_at_unix"]) <= 0
            or (value["status"] == "success" and (
                not isinstance(value.get("metrics"), dict)
                or value.get("evaluation_error") is not None
                or value["failed_unit_count"] != 0))
            or (value["status"] == "failed" and value.get("metrics") is not None)
            or (value.get("evaluation_error") is not None
                and (not isinstance(value["evaluation_error"], str)
                     or not value["evaluation_error"]))):
        raise QualificationRunnerError("qualification final score receipt 非法")


def score_final(*, work_root: Path | str) -> Dict[str, Any]:
    work = Path(os.path.abspath(os.fspath(work_root)))
    firewall = load_qualification_firewall(
        work, require_research_uid=False)
    if firewall is None:
        raise QualificationRunnerError("work_root 未安装 qualification contract")
    if os.geteuid() != 0:
        raise QualificationRunnerError("score-final 必须由 privileged root evaluator 执行")
    lock_fd = _score_lock(work)
    try:
        marker = firewall.read_final_marker()
        if marker["gpu_canary_sha256"] is not None:
            canary_path = work / "state" / "qualification" / "final" / "gpu-canary.json"
            canary_raw = _read_regular(
                canary_path, label="qualification GPU canary",
                expected_owner=firewall.research_uid, expected_mode=0o400)
            canary = _strict_json(canary_raw, label="qualification GPU canary")
            if (canary_raw != _canonical(canary)
                    or _hash_bytes(canary_raw) != marker["gpu_canary_sha256"]
                    or canary.get("ok") is not True
                    or canary.get("runtime_identity_hash")
                    != marker["runtime_identity_sha256"]):
                raise QualificationRunnerError(
                    "qualification GPU canary authority 与 final marker 错配")
        claim, claim_raw = firewall.read_claim_lock()
        claim_hash = _hash_bytes(claim_raw)
        if marker["claim_sha256"] != claim_hash:
            raise QualificationRunnerError("final marker 与当前 claim-lock hash 错配")
        batch_path = work / "state" / "qualification" / "final" / "batch.json"
        batch = _load_canonical(
            batch_path, label="qualification final batch", max_bytes=2 * 1024 * 1024)
        _validate_batch(
            batch, firewall=firewall, marker=marker,
            claim_sha256=claim_hash)
        truth_raw = read_artifact_bytes(
            firewall.sealed_truth_path, expected_hash=firewall.sealed_truth_sha256,
            max_bytes=256 * 1024 * 1024, label="sealed qualification truth")
        marker_hash = _hash_bytes(_canonical(marker))
        batch_hash = _hash_bytes(_canonical(batch))
        truth_hash = _hash_bytes(truth_raw)
        result_path = work / "state" / "qualification" / "final-result.json"
        existing = None
        if os.path.lexists(result_path):
            existing = _load_canonical(
                result_path, label="qualification final score",
                max_bytes=2 * 1024 * 1024)
            info = os.lstat(result_path)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != firewall.research_uid
                    or stat.S_IMODE(info.st_mode) != 0o400):
                raise QualificationRunnerError("qualification final score authority 非法")
            _validate_score_result(
                existing, firewall=firewall, marker_hash=marker_hash,
                batch_hash=batch_hash, truth_hash=truth_hash)

        expected_units = {item["unit_id"]: item for item in final_units(firewall)}
        verified_units = []
        for unit in final_units(firewall):
            _spent, unit_result_path, _run_root = _unit_paths(
                firewall, unit["unit_id"])
            verified_units.append(_read_unit_result(
                unit_result_path, firewall=firewall, unit=unit))
        if batch["units"] != verified_units:
            raise QualificationRunnerError("batch 与 immutable unit result receipts 错配")
        failures = [item for item in verified_units if item["status"] != "success"]
        prediction_bytes = sum(
            item["prediction"]["bytes"] for item in verified_units
            if item["status"] == "success")
        if prediction_bytes > _MAX_BATCH_PREDICTION_BYTES:
            raise QualificationRunnerError("qualification predictions aggregate 超上限")
        prediction_hashes = []
        predictions = []
        for item in verified_units:
            if item.get("status") != "success":
                continue
            prediction = item["prediction"]
            value = _load_canonical(
                Path(prediction["path"]), label="qualification scored prediction",
                max_bytes=_MAX_PREDICTION_BYTES)
            raw = _canonical(value)
            if (_hash_bytes(raw), len(raw)) != (prediction["sha256"], prediction["bytes"]):
                raise QualificationRunnerError("scored prediction 与 batch hash 漂移")
            if value.get("unit_id") not in expected_units:
                raise QualificationRunnerError("scored prediction unit_id 不在冻结全集")
            expected_unit = expected_units[value["unit_id"]]
            _validate_prediction(
                Path(prediction["path"]), firewall=firewall, unit=expected_unit)
            predictions.append(value)
            prediction_hashes.append({
                "unit_id": value["unit_id"], "sha256": prediction["sha256"],
                "bytes": prediction["bytes"],
            })
        metrics = None
        status = "failed"
        evaluation_error = None
        if not failures:
            try:
                truth = _strict_json(
                    truth_raw, label="sealed qualification truth",
                    max_bytes=256 * 1024 * 1024)
                if truth_raw != _canonical(truth):
                    raise QualificationRunnerError(
                        "sealed qualification truth 非 canonical")
                metric_truth = dict(truth)
                if firewall.task == "T1":
                    expected_rule = dict(claim["datasets"]["sealed_holdout"])
                    expected_rule.pop("dataset")
                    expected_rule["threshold"] = float(expected_rule["threshold"])
                    if truth.get("label_rule") != expected_rule:
                        raise QualificationRunnerError(
                            "sealed truth label_rule 与 claim-lock 不一致")
                    metric_truth.pop("label_rule", None)
                from .qualification_metrics import (
                    QualificationMetricsError, score_qualification)
                try:
                    metrics = score_qualification(metric_truth, predictions)
                except QualificationMetricsError:
                    raise
                status = "success"
            except (QualificationFirewallError, ValueError) as error:
                evaluation_error = (
                    f"{type(error).__name__}: {_bounded_error(error)}")
        result = {
            "version": 1, "protocol": SCORE_PROTOCOL, "task": firewall.task,
            "status": status, "contract_sha256": firewall.contract_sha256,
            "final_marker_sha256": marker_hash,
            "batch_sha256": batch_hash,
            "truth_sha256": truth_hash,
            "prediction_hashes": prediction_hashes,
            "failed_units": [item["unit"] for item in failures],
            "failed_unit_count": len(failures), "metrics": metrics,
            "evaluation_error": evaluation_error,
            "scored_at_unix": time.time(),
        }
        _validate_score_result(
            result, firewall=firewall, marker_hash=marker_hash,
            batch_hash=batch_hash, truth_hash=truth_hash)
        if existing is not None:
            comparable = dict(result)
            comparable["scored_at_unix"] = existing["scored_at_unix"]
            if comparable != existing:
                raise QualificationRunnerError(
                    "既有 qualification final score 与重新计算结果冲突")
            return existing
        _publish_for_research_owner(
            result_path, _canonical(result), research_uid=firewall.research_uid)
        published = _load_canonical(
            result_path, label="qualification final score", max_bytes=2 * 1024 * 1024)
        _validate_score_result(
            published, firewall=firewall, marker_hash=marker_hash,
            batch_hash=batch_hash, truth_hash=truth_hash)
        return published
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="meta-research one-shot qualification runner")
    parser.add_argument("--work-root", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run-final")
    run.add_argument("--system-root", required=True)
    run.add_argument("--source-root", required=True)
    run.add_argument("--gpu-contract")
    sub.add_parser("score-final")
    args = parser.parse_args(argv)
    if args.command == "run-final":
        result = run_final(
            system_root=args.system_root, work_root=args.work_root,
            source_root=args.source_root,
            gpu_contract_path=(Path(args.gpu_contract) if args.gpu_contract else None))
    else:
        result = score_final(work_root=args.work_root)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    # Library callers need the immutable failure receipt for audit/replay, but
    # an operator pipeline must not confuse "failure was recorded" with a
    # successful qualification.
    if args.command == "run-final":
        return 0 if result["failure_count"] == 0 else 3
    return 0 if result["status"] == "success" else 3


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "QualificationRunnerError", "freeze_source_tree", "run_final", "score_final",
]
