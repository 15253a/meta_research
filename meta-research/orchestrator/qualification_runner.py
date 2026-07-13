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
from .execution_sandbox import DockerExecutionSandbox, sandbox_session_id
from .instance_lease import InstanceLease, InstanceLeaseError, LOCK_NAME
from .process_supervisor import (
    ExecutionSupervisor,
    ExecutionSupervisorError,
    validate_execution_receipt,
)
from .qualification_firewall import (
    CONFIRMATORY_AUDIT_CHECKS,
    CONFIRMATORY_AUDIT_PROTOCOL,
    CONFIRMATORY_AUDIT_REF_PROTOCOL,
    QualificationFirewall,
    QualificationFirewallError,
    _MAX_JSON_BYTES,
    _canonical,
    _explore_view_identities,
    _fsync_directory,
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
CONFIRMATORY_SPENT_PROTOCOL = (
    "meta-research-qualification-confirmatory-spent/v1")
CONFIRMATORY_OUTPUT_PROTOCOL = (
    "meta-research-qualification-confirmatory-output/v1")
CONFIRMATORY_RESULT_PROTOCOL = (
    "meta-research-qualification-confirmatory-result/v1")
CONFIRMATORY_AUDIT_INPUT_PROTOCOL = (
    "meta-research-qualification-confirmatory-audit-input/v1")
CONFIRMATORY_AUDIT_DECISION_PROTOCOL = (
    "meta-research-qualification-confirmatory-audit-decision/v1")
GPU_CANARY_CANDIDATE_PROTOCOL = (
    "meta-research-qualification-gpu-canary-candidate/v1")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_SOURCE_FILES = 10_000
_MAX_SOURCE_BYTES = 1024 * 1024 * 1024
_MAX_PREDICTION_BYTES = 8 * 1024 * 1024
_MAX_BATCH_PREDICTION_BYTES = 256 * 1024 * 1024
_MAX_CONFIRMATORY_BYTES = 32 * 1024 * 1024
_MAX_AUDIT_EVIDENCE = 256
_MAX_AUDIT_TEXT_BYTES = 16 * 1024
_MAX_EVALUATOR_ARTIFACT_BYTES = _MAX_JSON_BYTES
_CONFIRMATORY_DECISION_DIR = ".meta-research-qualification-decisions-v1"
_SAFE_REL = re.compile(r"^[^\x00-\x1f\x7f\\]+$")

_CONFIRMATORY_MECHANICAL_SCOPE = {
    "frozen_claim_source_and_views": True,
    "single_batch_spent_before_spawn": True,
    "dreamer_excluded": True,
    "heldout_label_isolation_verified": False,
    "metric_correctness_verified": False,
}
_CONFIRMATORY_AUDIT_CHECKS = CONFIRMATORY_AUDIT_CHECKS


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


def _confirmatory_paths(
        firewall: QualificationFirewall) -> tuple[Path, Path, Path, Path, Path]:
    base = firewall.work_root / "state" / "qualification" / "confirmatory"
    return (
        base / "spent.json",
        base / "result.json",
        base / "run",
        base / "audit-ref.json",
        base / "audit-input.json",
    )


def _confirmatory_context() -> Dict[str, Any]:
    return {
        "phase": "qualification-confirmatory",
        "qualification_task": "t1",
        "unit_id": "confirmatory-lodo",
        "db_owner_kind": "qualification_confirmatory",
        "db_owner_id": 1,
    }


def _confirmatory_promotion_path(firewall: QualificationFirewall) -> Path:
    context = {**_confirmatory_context(), "log_name": "confirmatory.log"}
    session_id = sandbox_session_id("confirmatory.log", context)
    return (firewall.work_root / "state" / "qualification" / "confirmatory"
            / "run" / ".sandbox-meta" / f"{session_id}.promoted.json")


def _read_confirmatory_promotion(
        reference: Mapping[str, Any], *, firewall: QualificationFirewall,
        output: Mapping[str, Any]) -> Dict[str, Any]:
    expected_path = _confirmatory_promotion_path(firewall)
    if (not isinstance(reference, dict)
            or set(reference) != {"path", "sha256"}
            or reference.get("path") != str(expected_path)
            or not isinstance(reference.get("sha256"), str)
            or _SHA256_RE.fullmatch(reference["sha256"]) is None):
        raise QualificationRunnerError(
            "confirmatory sandbox promotion reference 字段/路径非法")
    raw = _read_regular(
        expected_path, label="confirmatory sandbox promoted receipt",
        expected_owner=firewall.research_uid, expected_mode=0o600)
    if _hash_bytes(raw) != reference["sha256"]:
        raise QualificationRunnerError(
            "confirmatory sandbox promoted receipt hash 漂移")
    value = _strict_json(raw, label="confirmatory sandbox promoted receipt")
    context = {**_confirmatory_context(), "log_name": "confirmatory.log"}
    session_id = sandbox_session_id("confirmatory.log", context)
    expected_file = {
        "path": "confirmatory.json", "sha256": output.get("sha256"),
        "bytes": output.get("bytes"),
    }
    if (raw != _canonical(value)
            or set(value) != {
                "version", "session_id", "exit_code", "promoted",
                "container_drained", "output_manifest_hash", "files"}
            or type(value.get("version")) is not int
            or value.get("version") != 1
            or value.get("session_id") != session_id
            or value.get("exit_code") != 0
            or value.get("promoted") is not True
            or value.get("container_drained") is not True
            or value.get("files") != [expected_file]
            or value.get("output_manifest_hash") != _hash_bytes(
                _canonical({"files": [expected_file]}))):
        raise QualificationRunnerError(
            "confirmatory sandbox promotion 未证明 exact 本次输出")
    return value


def _confirmatory_promotion_reference(
        *, firewall: QualificationFirewall,
        output: Mapping[str, Any]) -> Dict[str, str]:
    path = _confirmatory_promotion_path(firewall)
    raw = _read_regular(
        path, label="confirmatory sandbox promoted receipt",
        expected_owner=firewall.research_uid, expected_mode=0o600)
    reference = {"path": str(path), "sha256": _hash_bytes(raw)}
    _read_confirmatory_promotion(
        reference, firewall=firewall, output=output)
    return reference


def _render_confirmatory_command(
        argv: Sequence[str], *, source_proc: str) -> list[str]:
    rendered = []
    for original in argv:
        token = original.replace("{src}", source_proc)
        if re.search(r"\{[a-z_]+\}", token):
            raise QualificationRunnerError(
                "confirmatory command 留有未知 placeholder")
        rendered.append(token)
    return rendered


def _validate_confirmatory_output(
        path: Path, *, claim: Mapping[str, Any]) -> tuple[Dict[str, Any], str, int]:
    raw = read_artifact_bytes(
        path, max_bytes=_MAX_CONFIRMATORY_BYTES,
        label="qualification confirmatory output")
    value = _strict_json(
        raw, label="qualification confirmatory output",
        max_bytes=_MAX_CONFIRMATORY_BYTES)
    if raw != _canonical(value):
        raise QualificationRunnerError(
            "confirmatory.json 须为 canonical JSON + newline")
    if (set(value) != {
            "version", "protocol", "folds", "aggregate", "audit_material"}
            or type(value.get("version")) is not int
            or value.get("version") != 1
            or value.get("protocol") != CONFIRMATORY_OUTPUT_PROTOCOL
            or not isinstance(value.get("folds"), list)
            or not isinstance(value.get("aggregate"), dict)
            or not value["aggregate"]
            or not isinstance(value.get("audit_material"), dict)
            or not value["audit_material"]):
        raise QualificationRunnerError(
            "confirmatory.json 字段闭包/协议/汇总材料非法")
    expected = sorted(
        claim["datasets"]["confirmatory_lodo"],
        key=lambda item: (item.casefold(), item),
    )
    if len(value["folds"]) != len(expected):
        raise QualificationRunnerError("confirmatory LODO fold 数量非法")
    identities = []
    for fold in value["folds"]:
        if (not isinstance(fold, dict)
                or set(fold) != {
                    "held_out_dataset", "status", "metrics", "failure"}
                or fold.get("status") not in {"success", "failed"}
                or not isinstance(fold.get("held_out_dataset"), str)):
            raise QualificationRunnerError("confirmatory LODO fold 字段非法")
        identities.append(fold["held_out_dataset"])
        if fold["status"] == "success":
            if (not isinstance(fold.get("metrics"), dict) or not fold["metrics"]
                    or fold.get("failure") is not None):
                raise QualificationRunnerError(
                    "confirmatory 成功 fold 缺 metrics 或携 failure")
        elif (fold.get("metrics") is not None
              or not isinstance(fold.get("failure"), str)
              or not fold["failure"]
              or len(fold["failure"].encode("utf-8")) > 4096):
            raise QualificationRunnerError(
                "confirmatory 失败 fold 须给有界 failure 且不得携 metrics")
    if identities != expected:
        raise QualificationRunnerError(
            "confirmatory LODO folds 必须精确覆盖冻结数据集并规范排序")
    failed = [fold["held_out_dataset"] for fold in value["folds"]
              if fold["status"] == "failed"]
    if failed:
        raise QualificationRunnerError(
            f"confirmatory LODO fold reported failure: {failed}")
    return value, _hash_bytes(raw), len(raw)


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


def _output_authority(
        path: Path, *, expected_owner: int, label: str, seal: bool = False) -> None:
    expected = Path(os.path.abspath(os.fspath(path)))
    info = os.lstat(expected)
    if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_nlink != 1 or info.st_uid != expected_owner
            or info.st_mode & 0o022):
        raise QualificationRunnerError(f"{label} authority owner/type/link/mode 非法")
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
        raise QualificationRunnerError(f"published {label} authority 须为 0400")


def _prediction_authority(path: Path, *, expected_owner: int, seal: bool = False) -> None:
    _output_authority(
        path, expected_owner=expected_owner, label="prediction", seal=seal)


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


def _read_confirmatory_execution_receipt(
        reference: Mapping[str, Any], *, firewall: QualificationFirewall,
        require_success: bool) -> Dict[str, Any]:
    if (not isinstance(reference, dict)
            or set(reference) != {"path", "sha256"}
            or not isinstance(reference.get("path"), str)
            or not os.path.isabs(reference["path"])
            or os.path.normpath(reference["path"]) != reference["path"]
            or not isinstance(reference.get("sha256"), str)
            or _SHA256_RE.fullmatch(reference["sha256"]) is None):
        raise QualificationRunnerError(
            "confirmatory execution reference 字段非法")
    path = Path(reference["path"])
    expected_parent = firewall.work_root / "state" / "executions"
    if path.parent != expected_parent or os.path.realpath(path) != str(path):
        raise QualificationRunnerError(
            "confirmatory execution receipt 不在 exact guardian 目录")
    raw = _read_regular(
        path, label="confirmatory guardian receipt",
        expected_owner=firewall.research_uid, expected_mode=0o600)
    if _hash_bytes(raw) != reference["sha256"]:
        raise QualificationRunnerError(
            "confirmatory execution receipt hash 漂移")
    receipt = _strict_json(raw, label="confirmatory guardian receipt")
    try:
        validate_execution_receipt(receipt, path)
    except (OSError, TypeError, ValueError) as error:
        raise QualificationRunnerError(
            "confirmatory guardian receipt authority 非法: "
            + _bounded_error(error)) from error
    expected_context = {**_confirmatory_context(), "log_name": "confirmatory.log"}
    if (receipt.get("kind") != "qualification-confirmatory"
            or receipt.get("context") != expected_context
            or receipt.get("state") != "terminal"
            or receipt.get("group_drained") is not True
            or receipt.get("containment") != "docker-container-v1"
            or not isinstance(receipt.get("sandbox"), dict)
            or receipt["sandbox"].get("container_drained") is not True
            or (require_success and (
                receipt.get("outcome") != "exit"
                or receipt.get("returncode") != 0))):
        raise QualificationRunnerError(
            "confirmatory guardian receipt kind/context/terminal 边界非法")
    return receipt


def _confirmatory_execution_reference(
        run: Mapping[str, Any], *, firewall: QualificationFirewall,
        require_success: bool) -> Dict[str, str]:
    path = run.get("process_receipt_path")
    if not isinstance(path, str):
        raise QualificationRunnerError(
            "confirmatory execution 缺 guardian receipt path")
    raw = _read_regular(
        Path(path), label="confirmatory guardian receipt",
        expected_owner=firewall.research_uid, expected_mode=0o600)
    reference = {"path": path, "sha256": _hash_bytes(raw)}
    receipt = _read_confirmatory_execution_receipt(
        reference, firewall=firewall, require_success=require_success)
    embedded = run.get("process_receipt")
    if embedded is not None and embedded != receipt:
        raise QualificationRunnerError(
            "confirmatory run 返回 receipt 与 durable authority 不一致")
    exit_code = run.get("exit_code")
    if (receipt.get("outcome") == "exit"
            and (isinstance(exit_code, bool) or not isinstance(exit_code, int)
                 or receipt.get("returncode") != exit_code)):
        raise QualificationRunnerError(
            "confirmatory run exit 与 guardian receipt 不一致")
    return reference


def _validate_confirmatory_spent(
        value: Mapping[str, Any], *, firewall: QualificationFirewall,
        claim_sha256: str, boundary_sha256: str, source_sha256: str,
        runtime_sha256: str, gpu_canary_sha256: Optional[str]) -> None:
    if (set(value) != {
            "version", "protocol", "task", "contract_sha256", "claim_sha256",
            "claim_boundary_sha256", "source_tree_sha256",
            "runtime_identity_sha256", "gpu_canary_sha256",
            "execution_context", "spent_at_unix"}
            or type(value.get("version")) is not int
            or value.get("version") != 1
            or value.get("protocol") != CONFIRMATORY_SPENT_PROTOCOL
            or value.get("task") != "T1"
            or value.get("contract_sha256") != firewall.contract_sha256
            or value.get("claim_sha256") != claim_sha256
            or value.get("claim_boundary_sha256") != boundary_sha256
            or value.get("source_tree_sha256") != source_sha256
            or value.get("runtime_identity_sha256") != runtime_sha256
            or value.get("gpu_canary_sha256") != gpu_canary_sha256
            or value.get("execution_context") != _confirmatory_context()
            or isinstance(value.get("spent_at_unix"), bool)
            or not isinstance(value.get("spent_at_unix"), (int, float))
            or not math.isfinite(float(value["spent_at_unix"]))
            or float(value["spent_at_unix"]) <= 0):
        raise QualificationRunnerError(
            "confirmatory spent 字段/冻结输入/时间非法")


def _read_confirmatory_spent(
        path: Path, *, firewall: QualificationFirewall,
        claim_sha256: str, boundary_sha256: str, source_sha256: str,
        runtime_sha256: str, gpu_canary_sha256: Optional[str],
        finished_at_unix: Optional[float] = None) -> Dict[str, Any]:
    _reconcile_publish_link(path)
    raw = _read_regular(
        path, label="qualification confirmatory spent",
        expected_owner=firewall.research_uid, expected_mode=0o400)
    value = _strict_json(raw, label="qualification confirmatory spent")
    if raw != _canonical(value):
        raise QualificationRunnerError("confirmatory spent 非 canonical")
    _validate_confirmatory_spent(
        value, firewall=firewall, claim_sha256=claim_sha256,
        boundary_sha256=boundary_sha256, source_sha256=source_sha256,
        runtime_sha256=runtime_sha256,
        gpu_canary_sha256=gpu_canary_sha256)
    if (finished_at_unix is not None
            and float(value["spent_at_unix"]) > float(finished_at_unix)):
        raise QualificationRunnerError(
            "confirmatory spent 时间晚于 terminal result")
    return value


def _read_confirmatory_result(
        path: Path, *, firewall: QualificationFirewall,
        claim: Mapping[str, Any], claim_sha256: str, boundary_sha256: str,
        verify_execution: bool = True) -> Dict[str, Any]:
    value = _load_canonical(
        path, label="qualification confirmatory result",
        max_bytes=2 * 1024 * 1024)
    if (set(value) != {
            "version", "protocol", "task", "status", "contract_sha256",
            "claim_sha256", "claim_boundary_sha256", "source_tree_sha256",
            "runtime_identity_sha256", "gpu_canary_sha256", "mechanical_scope",
            "output", "execution", "promotion", "failure", "finished_at_unix"}
            or type(value.get("version")) is not int
            or value.get("version") != 1
            or value.get("protocol") != CONFIRMATORY_RESULT_PROTOCOL
            or value.get("task") != "T1"
            or value.get("status") not in {"success", "failed"}
            or value.get("contract_sha256") != firewall.contract_sha256
            or value.get("claim_sha256") != claim_sha256
            or value.get("claim_boundary_sha256") != boundary_sha256
            or value.get("source_tree_sha256") != (
                firewall.read_claim_boundary()[0]["source_tree_sha256"])
            or not isinstance(value.get("runtime_identity_sha256"), str)
            or _SHA256_RE.fullmatch(value["runtime_identity_sha256"]) is None
            or (firewall.final["gpu_required"] and (
                not isinstance(value.get("gpu_canary_sha256"), str)
                or _SHA256_RE.fullmatch(value["gpu_canary_sha256"]) is None))
            or (not firewall.final["gpu_required"]
                and value.get("gpu_canary_sha256") is not None)
            or value.get("mechanical_scope") != _CONFIRMATORY_MECHANICAL_SCOPE
            or any(type(item) is not bool for item in (
                value.get("mechanical_scope") or {}).values())
            or isinstance(value.get("finished_at_unix"), bool)
            or not isinstance(value.get("finished_at_unix"), (int, float))
            or not math.isfinite(float(value["finished_at_unix"]))
            or float(value["finished_at_unix"]) <= 0):
        raise QualificationRunnerError(
            "confirmatory result 字段/冻结输入/时间非法")
    execution = value.get("execution")
    if execution is not None and verify_execution:
        _read_confirmatory_execution_receipt(
            execution, firewall=firewall,
            require_success=value["status"] == "success")
    if value["status"] == "success":
        output = value.get("output")
        _spent, _result, run_root, _audit_ref, _audit_input = (
            _confirmatory_paths(firewall))
        expected_path = run_root / "confirmatory.json"
        if (not isinstance(output, dict)
                or set(output) != {"path", "sha256", "bytes"}
                or output.get("path") != str(expected_path)
                or not isinstance(output.get("sha256"), str)
                or _SHA256_RE.fullmatch(output["sha256"]) is None
                or isinstance(output.get("bytes"), bool)
                or not isinstance(output.get("bytes"), int)
                or output["bytes"] <= 0
                or not isinstance(execution, dict)
                or not isinstance(value.get("promotion"), dict)
                or value.get("failure") is not None):
            raise QualificationRunnerError(
                "confirmatory success output/execution authority 非法")
        _output_authority(
            expected_path, expected_owner=firewall.research_uid,
            label="confirmatory output")
        _parsed, digest, size = _validate_confirmatory_output(
            expected_path, claim=claim)
        if (digest, size) != (output["sha256"], output["bytes"]):
            raise QualificationRunnerError(
                "confirmatory output/result hash 漂移")
        _read_confirmatory_promotion(
            value["promotion"], firewall=firewall, output=output)
    elif (value.get("output") is not None
          or not isinstance(value.get("failure"), str)
          or not value["failure"]
          or len(value["failure"].encode("utf-8")) > 4096
          or (execution is not None and not isinstance(execution, dict))
          or value.get("promotion") is not None):
        raise QualificationRunnerError(
            "confirmatory failed result 字段非法")
    return value


def _publish_confirmatory_result(
        path: Path, *, firewall: QualificationFirewall,
        claim: Mapping[str, Any], claim_sha256: str, boundary_sha256: str,
        source_sha256: str, runtime_sha256: str,
        gpu_canary_sha256: Optional[str], status: str,
        output: Optional[Mapping[str, Any]], execution: Optional[Mapping[str, Any]],
        failure: Optional[str],
        promotion: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    value = {
        "version": 1, "protocol": CONFIRMATORY_RESULT_PROTOCOL,
        "task": "T1", "status": status,
        "contract_sha256": firewall.contract_sha256,
        "claim_sha256": claim_sha256,
        "claim_boundary_sha256": boundary_sha256,
        "source_tree_sha256": source_sha256,
        "runtime_identity_sha256": runtime_sha256,
        "gpu_canary_sha256": gpu_canary_sha256,
        "mechanical_scope": dict(_CONFIRMATORY_MECHANICAL_SCOPE),
        "output": None if output is None else dict(output),
        "execution": None if execution is None else dict(execution),
        "promotion": None if promotion is None else dict(promotion),
        "failure": failure,
        "finished_at_unix": time.time(),
    }
    _publish_once(path, _canonical(value))
    return _read_confirmatory_result(
        path, firewall=firewall, claim=claim, claim_sha256=claim_sha256,
        boundary_sha256=boundary_sha256)


def _validate_confirmatory_output_allowlist(run_root: Path) -> None:
    allowed = {
        "confirmatory.json", "confirmatory.log", "confirmatory.log.exit",
        "confirmatory.log.process.json", ".sandbox-meta", ".sandbox-output",
    }
    extras = sorted(item.name for item in run_root.iterdir()
                    if item.name not in allowed)
    if extras:
        raise QualificationRunnerError(
            f"confirmatory candidate 输出含 allowlist 外文件: {extras[:10]}")


def _recover_confirmatory(
        *, firewall: QualificationFirewall, claim: Mapping[str, Any],
        claim_sha256: str, boundary_sha256: str, source_sha256: str,
        runtime_sha256: str, gpu_canary_sha256: Optional[str],
        run_root: Path, result_path: Path, sandbox: DockerExecutionSandbox,
        supervisor: ExecutionSupervisor) -> Dict[str, Any]:
    if os.path.lexists(result_path):
        return _read_confirmatory_result(
            result_path, firewall=firewall, claim=claim,
            claim_sha256=claim_sha256, boundary_sha256=boundary_sha256)
    try:
        recovered = H.recover_staged_result(
            staging_dir=str(run_root), log_name="confirmatory.log",
            execution_supervisor=supervisor,
            execution_kind="qualification-confirmatory",
            execution_context=_confirmatory_context(),
            execution_sandbox=sandbox, recover_completed=True,
            return_terminal_failure=True)
    except (InstanceLeaseError, ExecutionSupervisorError):
        raise
    except Exception as error:
        return _publish_confirmatory_result(
            result_path, firewall=firewall, claim=claim,
            claim_sha256=claim_sha256, boundary_sha256=boundary_sha256,
            source_sha256=source_sha256, runtime_sha256=runtime_sha256,
            gpu_canary_sha256=gpu_canary_sha256, status="failed",
            output=None, execution=None,
            failure="spent recovery failed: " + _bounded_error(error))
    if recovered is None:
        return _publish_confirmatory_result(
            result_path, firewall=firewall, claim=claim,
            claim_sha256=claim_sha256, boundary_sha256=boundary_sha256,
            source_sha256=source_sha256, runtime_sha256=runtime_sha256,
            gpu_canary_sha256=gpu_canary_sha256, status="failed",
            output=None, execution=None,
            failure="confirmatory spent before a recoverable batch start")
    execution = None
    try:
        execution = _confirmatory_execution_reference(
            recovered, firewall=firewall,
            require_success=recovered.get("exit_code") == 0)
        if recovered.get("exit_code") != 0:
            outcome = recovered.get("failure_outcome")
            return _publish_confirmatory_result(
                result_path, firewall=firewall, claim=claim,
                claim_sha256=claim_sha256, boundary_sha256=boundary_sha256,
                source_sha256=source_sha256, runtime_sha256=runtime_sha256,
                gpu_canary_sha256=gpu_canary_sha256, status="failed",
                output=None, execution=execution,
                failure=(f"prior terminal outcome={outcome}" if outcome
                         else f"confirmatory exit={recovered.get('exit_code')}"))
        _validate_confirmatory_output_allowlist(run_root)
        _parsed, digest, size = _validate_confirmatory_output(
            run_root / "confirmatory.json", claim=claim)
        output = {
            "path": str(run_root / "confirmatory.json"),
            "sha256": digest, "bytes": size,
        }
        promotion = _confirmatory_promotion_reference(
            firewall=firewall, output=output)
        _output_authority(
            run_root / "confirmatory.json",
            expected_owner=firewall.research_uid,
            label="confirmatory output", seal=True)
    except Exception as error:
        return _publish_confirmatory_result(
            result_path, firewall=firewall, claim=claim,
            claim_sha256=claim_sha256, boundary_sha256=boundary_sha256,
            source_sha256=source_sha256, runtime_sha256=runtime_sha256,
            gpu_canary_sha256=gpu_canary_sha256, status="failed",
            output=None, execution=execution,
            failure="confirmatory recovery rejected: " + _bounded_error(error))
    return _publish_confirmatory_result(
        result_path, firewall=firewall, claim=claim,
        claim_sha256=claim_sha256, boundary_sha256=boundary_sha256,
        source_sha256=source_sha256, runtime_sha256=runtime_sha256,
        gpu_canary_sha256=gpu_canary_sha256, status="success",
        output=output, execution=execution, failure=None,
        promotion=promotion)


def run_confirmatory(
        *, system_root: Path | str, work_root: Path | str,
        source_root: Path | str,
        gpu_contract_path: Optional[Path] = None) -> Dict[str, Any]:
    """Run T1 stage C once; a spent batch is recovered or failed, never rerun."""
    system = Path(os.path.abspath(os.fspath(system_root)))
    work = Path(os.path.abspath(os.fspath(work_root)))
    policy = yaml.safe_load(
        (system / "policies" / "policy.yaml").read_text(encoding="utf-8"))
    lease = InstanceLease.acquire(work)
    supervisor = None
    primary: Optional[BaseException] = None
    try:
        firewall = load_qualification_firewall(
            work, policy=policy, require_research_uid=True)
        if firewall is None:
            raise QualificationRunnerError(
                "work_root 未安装 qualification contract")
        if firewall.task != "T1":
            raise QualificationRunnerError("T2 不存在 confirmatory stage C")
        if os.path.lexists(firewall.final_path):
            firewall.read_final_marker()
            raise QualificationRunnerError(
                "T1 final 已消费，不能事后补 confirmatory")
        claim, claim_raw = firewall.read_claim_lock()
        boundary, boundary_raw = firewall.read_claim_boundary()
        claim_hash = _hash_bytes(claim_raw)
        boundary_hash = _hash_bytes(boundary_raw)
        ledger, source_hash = freeze_source_tree(source_root)
        if (boundary["claim_sha256"] != claim_hash
                or boundary["source_tree_sha256"] != source_hash
                or boundary.get("confirmatory_command") is None):
            raise QualificationRunnerError(
                "confirmatory claim/source/command 与 claim boundary 不一致")
        if boundary["explore_views"] != _explore_view_identities(firewall):
            raise QualificationRunnerError(
                "confirmatory explore views 与 B boundary 冻结树发生漂移")
        gpu_contract = _load_gpu_contract(gpu_contract_path)
        gpu_required = boundary["confirmatory_command"]["gpu_required"]
        if gpu_required is not (gpu_contract is not None):
            raise QualificationRunnerError(
                "confirmatory GPU 模式与 --gpu-contract 必须精确一致")

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

        spent_path, result_path, run_root, audit_ref_path, _audit_input_path = (
            _confirmatory_paths(firewall))
        spent_existing = (
            _load_canonical(spent_path, label="confirmatory spent")
            if os.path.lexists(spent_path) else None)
        if os.path.lexists(result_path) and spent_existing is None:
            raise QualificationRunnerError(
                "confirmatory result 存在但 spent authority 缺失")
        if spent_existing is None and os.path.lexists(run_root):
            raise QualificationRunnerError(
                "confirmatory 首次执行发现预置 candidate-output namespace")
        if os.path.lexists(audit_ref_path) and not os.path.lexists(result_path):
            raise QualificationRunnerError(
                "confirmatory audit ref 存在但 C result 缺失")

        runtime_hash = sandbox.runtime_identity_hash
        canary_path = (
            work / "state" / "qualification" / "confirmatory" / "gpu-canary.json")
        canary_hash = None
        if gpu_required:
            canary = None
            if spent_existing is not None and not os.path.lexists(canary_path):
                raise QualificationRunnerError(
                    "GPU confirmatory spent 缺原始 canary authority")
            if os.path.lexists(canary_path):
                canary_raw = _read_regular(
                    canary_path, label="confirmatory GPU canary",
                    expected_owner=firewall.research_uid, expected_mode=0o400)
                canary = _strict_json(canary_raw, label="confirmatory GPU canary")
                if canary_raw != _canonical(canary):
                    raise QualificationRunnerError(
                        "confirmatory GPU canary 非 canonical")
                if spent_existing is not None:
                    validation_time = float(spent_existing["spent_at_unix"])
                    _validate_gpu_canary(
                        canary, work=work, sandbox=sandbox,
                        claim_sha256=claim_hash,
                        source_tree_sha256=source_hash,
                        validation_time=validation_time)
                else:
                    checked = canary.get("checked_at_unix")
                    if (isinstance(checked, bool)
                            or not isinstance(checked, (int, float))
                            or not math.isfinite(float(checked))
                            or float(checked) <= 0):
                        raise QualificationRunnerError(
                            "confirmatory GPU canary checked_at 非法")
                    _validate_gpu_canary(
                        canary, work=work, sandbox=sandbox,
                        claim_sha256=claim_hash,
                        source_tree_sha256=source_hash,
                        validation_time=float(checked))
                    if not 0 <= time.time() - float(checked) <= 60:
                        canary_path.unlink()
                        _fsync_directory(canary_path.parent)
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
        elif os.path.lexists(canary_path):
            raise QualificationRunnerError(
                "CPU confirmatory 存在意外 GPU canary authority")

        spent = {
            "version": 1, "protocol": CONFIRMATORY_SPENT_PROTOCOL,
            "task": "T1", "contract_sha256": firewall.contract_sha256,
            "claim_sha256": claim_hash,
            "claim_boundary_sha256": boundary_hash,
            "source_tree_sha256": source_hash,
            "runtime_identity_sha256": runtime_hash,
            "gpu_canary_sha256": canary_hash,
            "execution_context": _confirmatory_context(),
            "spent_at_unix": time.time(),
        }
        if spent_existing is not None:
            stable_existing = {
                key: value for key, value in spent_existing.items()
                if key != "spent_at_unix"}
            stable_expected = {
                key: value for key, value in spent.items()
                if key != "spent_at_unix"}
            if stable_existing != stable_expected:
                raise QualificationRunnerError(
                    "confirmatory spent identity 与当前冻结输入冲突")
            _read_confirmatory_spent(
                spent_path, firewall=firewall, claim_sha256=claim_hash,
                boundary_sha256=boundary_hash, source_sha256=source_hash,
                runtime_sha256=runtime_hash,
                gpu_canary_sha256=canary_hash)
            return _recover_confirmatory(
                firewall=firewall, claim=claim, claim_sha256=claim_hash,
                boundary_sha256=boundary_hash, source_sha256=source_hash,
                runtime_sha256=runtime_hash,
                gpu_canary_sha256=canary_hash, run_root=run_root,
                result_path=result_path, sandbox=sandbox,
                supervisor=supervisor)

        _publish_once(spent_path, _canonical(spent))
        _validate_confirmatory_spent(
            spent, firewall=firewall, claim_sha256=claim_hash,
            boundary_sha256=boundary_hash, source_sha256=source_hash,
            runtime_sha256=runtime_hash, gpu_canary_sha256=canary_hash)
        source_fd = -1
        invocation = None
        execution = None
        try:
            source_fd = open_directory(
                source_root, label="qualification confirmatory source")
            verify_tree_fd(
                source_fd, ledger, label="qualification confirmatory source",
                exact=True)
            command = _render_confirmatory_command(
                boundary["confirmatory_command"]["argv"],
                source_proc=f"/proc/self/fd/{source_fd}")
            context = _confirmatory_context()
            invocation = sandbox.prepare(
                command, staging_dir=run_root, log_name="confirmatory.log",
                env=None, timeout_s=float(policy["execution"]["max_timeout_s"]),
                tree_expectations=((source_fd, ledger, ()),),
                execution_context={**context, "log_name": "confirmatory.log"},
                execution_supervisor=supervisor, gpu_required=gpu_required)
            run = H.run_staged(
                invocation.argv, staging_dir=str(run_root),
                log_name="confirmatory.log",
                timeout_s=float(policy["execution"]["max_timeout_s"]),
                env=invocation.env, pass_fds=invocation.pass_fds,
                execution_supervisor=supervisor,
                execution_kind="qualification-confirmatory",
                execution_context=context, sandbox_invocation=invocation)
            execution = _confirmatory_execution_reference(
                run, firewall=firewall, require_success=run.get("exit_code") == 0)
            if run.get("exit_code") != 0:
                result = _publish_confirmatory_result(
                    result_path, firewall=firewall, claim=claim,
                    claim_sha256=claim_hash, boundary_sha256=boundary_hash,
                    source_sha256=source_hash, runtime_sha256=runtime_hash,
                    gpu_canary_sha256=canary_hash, status="failed",
                    output=None, execution=execution,
                    failure=f"confirmatory exit={run.get('exit_code')}")
            else:
                _validate_confirmatory_output_allowlist(run_root)
                _parsed, digest, size = _validate_confirmatory_output(
                    run_root / "confirmatory.json", claim=claim)
                output = {
                    "path": str(run_root / "confirmatory.json"),
                    "sha256": digest, "bytes": size,
                }
                promotion = _confirmatory_promotion_reference(
                    firewall=firewall, output=output)
                _output_authority(
                    run_root / "confirmatory.json",
                    expected_owner=firewall.research_uid,
                    label="confirmatory output", seal=True)
                result = _publish_confirmatory_result(
                    result_path, firewall=firewall, claim=claim,
                    claim_sha256=claim_hash, boundary_sha256=boundary_hash,
                    source_sha256=source_hash, runtime_sha256=runtime_hash,
                    gpu_canary_sha256=canary_hash, status="success",
                    output=output, execution=execution, failure=None,
                    promotion=promotion)
        except (InstanceLeaseError, ExecutionSupervisorError):
            raise
        except Exception as error:
            result = _publish_confirmatory_result(
                result_path, firewall=firewall, claim=claim,
                claim_sha256=claim_hash, boundary_sha256=boundary_hash,
                source_sha256=source_hash, runtime_sha256=runtime_hash,
                gpu_canary_sha256=canary_hash, status="failed",
                output=None, execution=execution,
                failure=f"{type(error).__name__}: {_bounded_error(error)}")
        finally:
            if invocation is not None:
                invocation.close()
            if source_fd >= 0:
                os.close(source_fd)
        return result
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
                        note(f"confirmatory runner cleanup: {error}")
            else:
                raise close_errors[0]


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
        boundary, boundary_raw = firewall.read_claim_boundary()
        if (boundary["claim_sha256"] != claim_hash
                or boundary["source_tree_sha256"] != source_hash):
            raise QualificationRunnerError(
                "final claim/source 与 claim boundary 冻结输入不一致")
        confirmatory_audit_hash = _require_confirmatory_admission(
            firewall=firewall, claim_sha256=claim_hash,
            boundary_sha256=_hash_bytes(boundary_raw))
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
            if existing_marker is not None and not os.path.lexists(canary_path):
                raise QualificationRunnerError(
                    "GPU final marker 缺原始 canary authority")
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
            confirmatory_audit_sha256=confirmatory_audit_hash,
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


def _preflight_research_publication(
        path: Path, payload: bytes, *, research_uid: int, label: str) -> bool:
    if not 2 <= len(payload) <= _MAX_EVALUATOR_ARTIFACT_BYTES:
        raise QualificationRunnerError(f"{label} 大小超出 evaluator artifact 上限")
    if os.geteuid() != 0:
        raise QualificationRunnerError(
            f"{label} 必须由 privileged root evaluator 发布")
    parent = path.parent
    parent_info = os.lstat(parent)
    if (not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(parent_info.st_mode)
            or parent_info.st_uid != research_uid
            or os.path.realpath(parent) != str(parent)):
        raise QualificationRunnerError(f"{label} 发布目录 owner 非 research_uid")
    if os.path.lexists(path):
        current = _read_regular(
            path, label=label, expected_owner=research_uid,
            expected_mode=0o400)
        if current != payload:
            raise QualificationRunnerError(f"{label} 已存在且冲突")
        return True
    return False


def _publish_for_research_owner(
        path: Path, payload: bytes, *, research_uid: int, label: str) -> None:
    if _preflight_research_publication(
            path, payload, research_uid=research_uid, label=label):
        return
    parent = path.parent
    parent_info = os.lstat(parent)
    tmp = parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    fd = os.open(
        tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError(f"{label} short write")
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
            path, label=label,
            expected_owner=research_uid, expected_mode=0o400)
        if current != payload:
            raise QualificationRunnerError(f"{label} 并发发布内容冲突")
    finally:
        tmp.unlink(missing_ok=True)


def _validate_root_authority_path(
        path: Path, *, firewall: QualificationFirewall, expected_mode: int,
        label: str) -> bytes:
    supplied = os.fspath(path)
    authority = Path(os.path.abspath(supplied))
    if (not os.path.isabs(supplied)
            or os.path.normpath(supplied) != supplied
            or os.path.realpath(authority) != str(authority)
            or os.path.commonpath((str(authority), str(firewall.work_root)))
            == str(firewall.work_root)):
        raise QualificationRunnerError(
            f"{label} 必须位于 work_root 外的 canonical root authority path")
    raw = _read_regular(
        authority, label=label, expected_owner=0,
        expected_mode=expected_mode)
    if firewall.research_uid != 0:
        current = authority.parent
        while True:
            info = os.lstat(current)
            if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                    or info.st_uid != 0 or info.st_mode & 0o022):
                raise QualificationRunnerError(
                    f"{label} ancestor 不是 root-owned non-writable: {current}")
            if current == current.parent:
                break
            current = current.parent
    return raw


def _preflight_root_publication(
        path: Path, payload: bytes, *, firewall: QualificationFirewall,
        label: str) -> Path:
    if not 2 <= len(payload) <= _MAX_EVALUATOR_ARTIFACT_BYTES:
        raise QualificationRunnerError(f"{label} 大小超出 evaluator artifact 上限")
    if os.geteuid() != 0:
        raise QualificationRunnerError(f"{label} 只能由 root evaluator 发布")
    supplied = os.fspath(path)
    authority = Path(os.path.abspath(supplied))
    parent = authority.parent
    if not os.path.isabs(supplied) or os.path.normpath(supplied) != supplied:
        raise QualificationRunnerError(f"{label} path 须为 canonical absolute path")
    if os.path.lexists(authority):
        current = _validate_root_authority_path(
            authority, firewall=firewall, expected_mode=0o444,
            label=label)
        if current != payload:
            raise QualificationRunnerError(f"{label} 已存在且内容冲突")
        return authority
    parent_info = os.lstat(parent)
    if (not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(parent_info.st_mode)
            or parent_info.st_uid != 0 or parent_info.st_mode & 0o022
            or os.path.realpath(parent) != str(parent)
            or os.path.commonpath((str(authority), str(firewall.work_root)))
            == str(firewall.work_root)):
        raise QualificationRunnerError(
            f"{label} parent 须为 work_root 外 root-owned non-writable 目录")
    if firewall.research_uid != 0:
        current = parent
        while True:
            info = os.lstat(current)
            if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                    or info.st_uid != 0 or info.st_mode & 0o022):
                raise QualificationRunnerError(
                    f"{label} ancestor 非 root-owned non-writable")
            if current == current.parent:
                break
            current = current.parent
    return authority


def _publish_root_authority(
        path: Path, payload: bytes, *, firewall: QualificationFirewall,
        label: str = "confirmatory audit authority") -> None:
    authority = _preflight_root_publication(
        path, payload, firewall=firewall, label=label)
    if os.path.lexists(authority):
        return
    parent = authority.parent
    tmp = parent / f".{authority.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    fd = os.open(
        tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0), 0o444)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError(f"{label} short write")
            view = view[written:]
        os.fchmod(fd, 0o444)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        _rename_noreplace(tmp, authority)
        _fsync_directory(parent)
    except FileExistsError:
        current = _validate_root_authority_path(
            authority, firewall=firewall, expected_mode=0o444,
            label=label)
        if current != payload:
            raise QualificationRunnerError(f"{label} 并发发布内容冲突")
    finally:
        tmp.unlink(missing_ok=True)


def _confirmatory_decision_directory(
        firewall: QualificationFirewall, *, create: bool) -> Path:
    parent = firewall.sealed_truth_path.parent
    try:
        inside_work = (
            os.path.commonpath((str(parent), str(firewall.work_root)))
            == str(firewall.work_root))
    except ValueError as error:
        raise QualificationRunnerError(
            "confirmatory decision ledger parent 不可比较") from error
    parent_info = os.lstat(parent)
    if (not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(parent_info.st_mode)
            or parent_info.st_uid != 0 or parent_info.st_mode & 0o022
            or os.path.realpath(parent) != str(parent) or inside_work):
        raise QualificationRunnerError(
            "sealed-truth parent 须为 work_root 外 root-owned non-writable ledger root")
    if firewall.research_uid != 0:
        current = parent
        while True:
            info = os.lstat(current)
            if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                    or info.st_uid != 0 or info.st_mode & 0o022):
                raise QualificationRunnerError(
                    "confirmatory decision ledger ancestor 非 root-owned non-writable")
            if current == current.parent:
                break
            current = current.parent
    directory = parent / _CONFIRMATORY_DECISION_DIR
    if not os.path.lexists(directory) and create:
        if os.geteuid() != 0:
            raise QualificationRunnerError(
                "confirmatory decision ledger 只能由 root evaluator 创建")
        try:
            os.mkdir(directory, mode=0o555)
            os.chmod(directory, 0o555, follow_symlinks=False)
            _fsync_directory(parent)
        except FileExistsError:
            pass
    if os.path.lexists(directory):
        info = os.lstat(directory)
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o555
                or os.path.realpath(directory) != str(directory)):
            raise QualificationRunnerError(
                "confirmatory decision ledger 目录身份/权限非法")
    elif create:
        raise QualificationRunnerError(
            "confirmatory decision ledger 创建后缺失")
    return directory


def _confirmatory_decision_path(
        firewall: QualificationFirewall, *, create_directory: bool) -> Path:
    directory = _confirmatory_decision_directory(
        firewall, create=create_directory)
    key = _hash_bytes(_canonical({
        "protocol": CONFIRMATORY_AUDIT_DECISION_PROTOCOL + "/key",
        "work_root": str(firewall.work_root),
    })).removeprefix("sha256:")
    return directory / f"{key}.json"


def _confirmatory_decision_value(
        *, firewall: QualificationFirewall, claim_sha256: str,
        boundary_sha256: str, result_sha256: str,
        authority_path: Path, authority_sha256: str,
        audit: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "version": 1,
        "protocol": CONFIRMATORY_AUDIT_DECISION_PROTOCOL,
        "task": "T1",
        "work_root": str(firewall.work_root),
        "contract_sha256": firewall.contract_sha256,
        "sealed_truth_sha256": firewall.sealed_truth_sha256,
        "claim_sha256": claim_sha256,
        "claim_boundary_sha256": boundary_sha256,
        "confirmatory_result_sha256": result_sha256,
        "status": audit.get("status"),
        "authority_path": str(authority_path),
        "authority_sha256": authority_sha256,
        "decided_at_unix": audit.get("reviewed_at_unix"),
    }


def _read_confirmatory_decision(
        *, firewall: QualificationFirewall, claim_sha256: str,
        boundary_sha256: str, result_sha256: str,
        authority_path: Path, authority_sha256: str,
        audit: Mapping[str, Any]) -> Dict[str, Any]:
    path = _confirmatory_decision_path(
        firewall, create_directory=False)
    raw = _validate_root_authority_path(
        path, firewall=firewall, expected_mode=0o444,
        label="confirmatory root decision ledger")
    value = _strict_json(raw, label="confirmatory root decision ledger")
    expected = _confirmatory_decision_value(
        firewall=firewall, claim_sha256=claim_sha256,
        boundary_sha256=boundary_sha256, result_sha256=result_sha256,
        authority_path=authority_path, authority_sha256=authority_sha256,
        audit=audit)
    if raw != _canonical(value) or value != expected:
        raise QualificationRunnerError(
            "confirmatory root decision ledger 与 C/audit authority 错配")
    return value


def _bounded_audit_text(value: Any, *, label: str, allow_empty: bool = False) -> str:
    if (not isinstance(value, str) or (not allow_empty and not value)
            or "\x00" in value
            or len(value.encode("utf-8")) > _MAX_AUDIT_TEXT_BYTES):
        raise QualificationRunnerError(f"{label} 须为有界 UTF-8 文本")
    return value


def _validate_confirmatory_audit_input(
        value: Mapping[str, Any], *, boundary_sha256: str,
        result_sha256: str,
        result_finished_at_unix: Optional[float] = None) -> None:
    if (set(value) != {
            "version", "protocol", "task", "claim_boundary_sha256",
            "confirmatory_result_sha256", "auditor", "checks", "evidence",
            "notes", "reviewed_at_unix"}
            or type(value.get("version")) is not int
            or value.get("version") != 1
            or value.get("protocol") != CONFIRMATORY_AUDIT_INPUT_PROTOCOL
            or value.get("task") != "T1"
            or value.get("claim_boundary_sha256") != boundary_sha256
            or value.get("confirmatory_result_sha256") != result_sha256
            or not isinstance(value.get("checks"), dict)
            or set(value["checks"]) != _CONFIRMATORY_AUDIT_CHECKS
            or any(type(item) is not bool for item in value["checks"].values())
            or not isinstance(value.get("evidence"), list)
            or not 1 <= len(value["evidence"]) <= _MAX_AUDIT_EVIDENCE
            or isinstance(value.get("reviewed_at_unix"), bool)
            or not isinstance(value.get("reviewed_at_unix"), (int, float))
            or not math.isfinite(float(value["reviewed_at_unix"]))
            or float(value["reviewed_at_unix"]) <= 0):
        raise QualificationRunnerError(
            "confirmatory audit input 字段/绑定/checks/时间非法")
    _bounded_audit_text(value.get("auditor"), label="audit auditor")
    _bounded_audit_text(value.get("notes"), label="audit notes")
    if (result_finished_at_unix is not None
            and float(value["reviewed_at_unix"])
            < float(result_finished_at_unix)):
        raise QualificationRunnerError(
            "confirmatory audit reviewed_at 早于 terminal result")
    seen = set()
    covered = set()
    for item in value["evidence"]:
        if (not isinstance(item, dict)
                or set(item) != {"check", "ref", "sha256"}
                or item.get("check") not in _CONFIRMATORY_AUDIT_CHECKS
                or not isinstance(item.get("sha256"), str)
                or _SHA256_RE.fullmatch(item["sha256"]) is None):
            raise QualificationRunnerError(
                "confirmatory audit evidence 字段非法")
        ref = _bounded_audit_text(
            item.get("ref"), label="audit evidence ref")
        identity = (item["check"], ref)
        if identity in seen:
            raise QualificationRunnerError(
                "confirmatory audit evidence 含重复 check/ref")
        seen.add(identity)
        covered.add(item["check"])
    if covered != _CONFIRMATORY_AUDIT_CHECKS:
        raise QualificationRunnerError(
            "confirmatory audit 每个 required check 都须有 evidence")


def _validate_confirmatory_audit(
        value: Mapping[str, Any], *, firewall: QualificationFirewall,
        claim_sha256: str, boundary_sha256: str, result_sha256: str,
        output_sha256: str, audit_input_sha256: str) -> None:
    if (set(value) != {
            "version", "protocol", "task", "status", "contract_sha256",
            "claim_sha256", "claim_boundary_sha256",
            "confirmatory_result_sha256", "confirmatory_output_sha256",
            "audit_input_sha256", "auditor", "checks", "evidence", "notes",
            "reviewed_at_unix"}
            or type(value.get("version")) is not int
            or value.get("version") != 1
            or value.get("protocol") != CONFIRMATORY_AUDIT_PROTOCOL
            or value.get("task") != "T1"
            or value.get("status") not in {"passed", "failed"}
            or value.get("contract_sha256") != firewall.contract_sha256
            or value.get("claim_sha256") != claim_sha256
            or value.get("claim_boundary_sha256") != boundary_sha256
            or value.get("confirmatory_result_sha256") != result_sha256
            or value.get("confirmatory_output_sha256") != output_sha256
            or value.get("audit_input_sha256") != audit_input_sha256
            or not isinstance(value.get("checks"), dict)
            or set(value["checks"]) != _CONFIRMATORY_AUDIT_CHECKS
            or any(type(item) is not bool for item in value["checks"].values())
            or value["status"] != (
                "passed" if all(value["checks"].values()) else "failed")):
        raise QualificationRunnerError(
            "confirmatory audit authority 字段/绑定/verdict 非法")
    _bounded_audit_text(value.get("auditor"), label="audit authority auditor")
    _bounded_audit_text(value.get("notes"), label="audit authority notes")
    audit_projection = {
        "version": 1, "protocol": CONFIRMATORY_AUDIT_INPUT_PROTOCOL,
        "task": "T1", "claim_boundary_sha256": boundary_sha256,
        "confirmatory_result_sha256": result_sha256,
        "auditor": value["auditor"], "checks": value["checks"],
        "evidence": value.get("evidence"), "notes": value["notes"],
        "reviewed_at_unix": value.get("reviewed_at_unix"),
    }
    _validate_confirmatory_audit_input(
        audit_projection, boundary_sha256=boundary_sha256,
        result_sha256=result_sha256)


def _load_confirmatory_audit_authority(
        *, firewall: QualificationFirewall, claim: Mapping[str, Any],
        claim_sha256: str, boundary_sha256: str) -> tuple[Dict[str, Any], bytes]:
    spent_path, result_path, _run_root, audit_ref_path, audit_input_copy_path = (
        _confirmatory_paths(firewall))
    missing = [
        path.name for path in (
            spent_path, result_path, audit_ref_path, audit_input_copy_path)
        if not os.path.lexists(path)
    ]
    if missing:
        raise QualificationRunnerError(
            "T1-D admission 缺 confirmatory/audit authorities: "
            + ", ".join(missing))
    result = _read_confirmatory_result(
        result_path, firewall=firewall, claim=claim,
        claim_sha256=claim_sha256, boundary_sha256=boundary_sha256,
        verify_execution=True)
    if result["status"] != "success":
        raise QualificationRunnerError(
            "T1-D admission 要求成功 confirmatory result")
    _read_confirmatory_spent(
        spent_path, firewall=firewall, claim_sha256=claim_sha256,
        boundary_sha256=boundary_sha256,
        source_sha256=result["source_tree_sha256"],
        runtime_sha256=result["runtime_identity_sha256"],
        gpu_canary_sha256=result["gpu_canary_sha256"],
        finished_at_unix=float(result["finished_at_unix"]))
    result_raw = _read_regular(
        result_path, label="qualification confirmatory result",
        expected_owner=firewall.research_uid, expected_mode=0o400)
    result_sha256 = _hash_bytes(result_raw)
    audit_input_raw = _read_regular(
        audit_input_copy_path, label="confirmatory audit input copy",
        expected_owner=firewall.research_uid, expected_mode=0o400)
    audit_input = _strict_json(
        audit_input_raw, label="confirmatory audit input copy")
    if audit_input_raw != _canonical(audit_input):
        raise QualificationRunnerError(
            "confirmatory audit input copy 非 canonical")
    _validate_confirmatory_audit_input(
        audit_input, boundary_sha256=boundary_sha256,
        result_sha256=result_sha256,
        result_finished_at_unix=float(result["finished_at_unix"]))
    ref = _load_canonical(
        audit_ref_path, label="confirmatory audit authority ref")
    if (set(ref) != {"version", "protocol", "task", "path", "sha256"}
            or type(ref.get("version")) is not int
            or ref.get("version") != 1
            or ref.get("protocol") != CONFIRMATORY_AUDIT_REF_PROTOCOL
            or ref.get("task") != "T1"
            or not isinstance(ref.get("path"), str)
            or not isinstance(ref.get("sha256"), str)
            or _SHA256_RE.fullmatch(ref["sha256"]) is None):
        raise QualificationRunnerError(
            "confirmatory audit authority ref 字段非法")
    raw = _validate_root_authority_path(
        Path(ref["path"]), firewall=firewall, expected_mode=0o444,
        label="confirmatory audit authority")
    if _hash_bytes(raw) != ref["sha256"]:
        raise QualificationRunnerError(
            "confirmatory audit authority/ref hash 漂移")
    audit = _strict_json(raw, label="confirmatory audit authority")
    if raw != _canonical(audit):
        raise QualificationRunnerError(
            "confirmatory audit authority 非 canonical")
    _validate_confirmatory_audit(
        audit, firewall=firewall, claim_sha256=claim_sha256,
        boundary_sha256=boundary_sha256, result_sha256=result_sha256,
        output_sha256=result["output"]["sha256"],
        audit_input_sha256=_hash_bytes(audit_input_raw))
    if (audit["auditor"] != audit_input["auditor"]
            or audit["checks"] != audit_input["checks"]
            or audit["evidence"] != audit_input["evidence"]
            or audit["notes"] != audit_input["notes"]
            or audit["reviewed_at_unix"] != audit_input["reviewed_at_unix"]):
        raise QualificationRunnerError(
            "confirmatory audit authority 与 durable input copy 不一致")
    _read_confirmatory_decision(
        firewall=firewall, claim_sha256=claim_sha256,
        boundary_sha256=boundary_sha256, result_sha256=result_sha256,
        authority_path=Path(ref["path"]), authority_sha256=ref["sha256"],
        audit=audit)
    return audit, raw


def _require_confirmatory_admission(
        *, firewall: QualificationFirewall, claim_sha256: str,
        boundary_sha256: str) -> Optional[str]:
    if firewall.task != "T1":
        return None
    return verify_confirmatory_admission(
        firewall=firewall, claim_sha256=claim_sha256,
        boundary_sha256=boundary_sha256)


def verify_confirmatory_admission(
        *, firewall: QualificationFirewall, claim_sha256: str,
        boundary_sha256: str) -> str:
    """Verify every durable C/audit authority required to admit T1 stage D."""
    if firewall.task != "T1":
        raise QualificationRunnerError(
            "confirmatory admission verifier 只接受 T1 firewall")
    claim, actual_claim_raw = firewall.read_claim_lock()
    boundary, actual_boundary_raw = firewall.read_claim_boundary()
    if (_hash_bytes(actual_claim_raw) != claim_sha256
            or _hash_bytes(actual_boundary_raw) != boundary_sha256
            or boundary["claim_sha256"] != claim_sha256):
        raise QualificationRunnerError(
            "confirmatory admission claim/boundary 与 durable authority 错配")
    audit, raw = _load_confirmatory_audit_authority(
        firewall=firewall, claim=claim, claim_sha256=claim_sha256,
        boundary_sha256=boundary_sha256)
    if audit["status"] != "passed":
        raise QualificationRunnerError(
            "T1 confirmatory audit 未通过，D sealed final 永久拒绝")
    return _hash_bytes(raw)


def audit_confirmatory(
        *, work_root: Path | str, audit_input_path: Path | str,
        authority_path: Path | str) -> Dict[str, Any]:
    """Root evaluator converts one reviewed input into external D admission authority."""
    if os.geteuid() != 0:
        raise QualificationRunnerError(
            "audit-confirmatory 必须由 privileged root evaluator 执行")
    work = Path(os.path.abspath(os.fspath(work_root)))
    firewall = load_qualification_firewall(work, require_research_uid=False)
    if firewall is None:
        raise QualificationRunnerError(
            "work_root 未安装 qualification contract")
    if firewall.task != "T1":
        raise QualificationRunnerError("T2 不存在 confirmatory audit")
    lock_fd = _score_lock(work)
    try:
        if os.path.lexists(firewall.final_path):
            firewall.read_final_marker()
            raise QualificationRunnerError(
                "T1 final 已消费，不能事后发布 confirmatory audit")
        claim, claim_raw = firewall.read_claim_lock()
        boundary, boundary_raw = firewall.read_claim_boundary()
        claim_hash = _hash_bytes(claim_raw)
        boundary_hash = _hash_bytes(boundary_raw)
        if boundary["claim_sha256"] != claim_hash:
            raise QualificationRunnerError(
                "confirmatory audit claim/boundary 错配")
        spent_path, result_path, _run_root, audit_ref_path, audit_input_copy_path = (
            _confirmatory_paths(firewall))
        result = _read_confirmatory_result(
            result_path, firewall=firewall, claim=claim,
            claim_sha256=claim_hash, boundary_sha256=boundary_hash,
            verify_execution=True)
        if result["status"] != "success":
            raise QualificationRunnerError(
                "失败的 confirmatory batch 不可签发 audit authority")
        _read_confirmatory_spent(
            spent_path, firewall=firewall, claim_sha256=claim_hash,
            boundary_sha256=boundary_hash,
            source_sha256=result["source_tree_sha256"],
            runtime_sha256=result["runtime_identity_sha256"],
            gpu_canary_sha256=result["gpu_canary_sha256"],
            finished_at_unix=float(result["finished_at_unix"]))
        result_raw = _read_regular(
            result_path, label="qualification confirmatory result",
            expected_owner=firewall.research_uid, expected_mode=0o400)
        result_hash = _hash_bytes(result_raw)
        source_input = Path(os.fspath(audit_input_path))
        input_raw = _validate_root_authority_path(
            source_input, firewall=firewall, expected_mode=0o400,
            label="confirmatory audit operator input")
        audit_input = _strict_json(
            input_raw, label="confirmatory audit operator input")
        if input_raw != _canonical(audit_input):
            raise QualificationRunnerError(
                "confirmatory audit operator input 非 canonical")
        _validate_confirmatory_audit_input(
            audit_input, boundary_sha256=boundary_hash,
            result_sha256=result_hash,
            result_finished_at_unix=float(result["finished_at_unix"]))
        status = (
            "passed" if all(audit_input["checks"].values()) else "failed")
        audit = {
            "version": 1, "protocol": CONFIRMATORY_AUDIT_PROTOCOL,
            "task": "T1", "status": status,
            "contract_sha256": firewall.contract_sha256,
            "claim_sha256": claim_hash,
            "claim_boundary_sha256": boundary_hash,
            "confirmatory_result_sha256": result_hash,
            "confirmatory_output_sha256": result["output"]["sha256"],
            "audit_input_sha256": _hash_bytes(input_raw),
            "auditor": audit_input["auditor"],
            "checks": audit_input["checks"],
            "evidence": audit_input["evidence"],
            "notes": audit_input["notes"],
            "reviewed_at_unix": audit_input["reviewed_at_unix"],
        }
        _validate_confirmatory_audit(
            audit, firewall=firewall, claim_sha256=claim_hash,
            boundary_sha256=boundary_hash, result_sha256=result_hash,
            output_sha256=result["output"]["sha256"],
            audit_input_sha256=_hash_bytes(input_raw))
        authority = Path(os.fspath(authority_path))
        raw = _canonical(audit)
        if len(raw) > _MAX_EVALUATOR_ARTIFACT_BYTES:
            raise QualificationRunnerError(
                "confirmatory audit 派生 authority 大小超出 evaluator artifact 上限；"
                "尚未发布任何 research/root authority")
        authority = _preflight_root_publication(
            authority, raw, firewall=firewall,
            label="confirmatory audit authority")
        ref = {
            "version": 1, "protocol": CONFIRMATORY_AUDIT_REF_PROTOCOL,
            "task": "T1", "path": str(authority),
            "sha256": _hash_bytes(raw),
        }
        ref_raw = _canonical(ref)
        _preflight_research_publication(
            audit_input_copy_path, input_raw,
            research_uid=firewall.research_uid,
            label="confirmatory audit input copy")
        _preflight_research_publication(
            audit_ref_path, ref_raw, research_uid=firewall.research_uid,
            label="confirmatory audit authority ref")
        decision_path = _confirmatory_decision_path(
            firewall, create_directory=True)
        decision = _confirmatory_decision_value(
            firewall=firewall, claim_sha256=claim_hash,
            boundary_sha256=boundary_hash, result_sha256=result_hash,
            authority_path=authority, authority_sha256=_hash_bytes(raw),
            audit=audit)
        decision_raw = _canonical(decision)
        _preflight_root_publication(
            decision_path, decision_raw, firewall=firewall,
            label="confirmatory root decision ledger")

        # The immutable verdict must lead every other publication.  A crash can
        # then leave only fail-closed missing authorities; an exact retry repairs
        # them, while a different verdict conflicts with the durable decision.
        _publish_root_authority(
            decision_path, decision_raw, firewall=firewall,
            label="confirmatory root decision ledger")
        _publish_root_authority(authority, raw, firewall=firewall)
        _publish_for_research_owner(
            audit_input_copy_path, input_raw,
            research_uid=firewall.research_uid,
            label="confirmatory audit input copy")
        _publish_for_research_owner(
            audit_ref_path, ref_raw, research_uid=firewall.research_uid,
            label="confirmatory audit authority ref")
        checked, checked_raw = _load_confirmatory_audit_authority(
            firewall=firewall, claim=claim, claim_sha256=claim_hash,
            boundary_sha256=boundary_hash)
        if checked_raw != raw:
            raise QualificationRunnerError(
                "published confirmatory audit authority bytes 漂移")
        return checked
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


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
        boundary, boundary_raw = firewall.read_claim_boundary()
        if (boundary["claim_sha256"] != claim_hash
                or boundary["source_tree_sha256"] != marker["source_tree_sha256"]
                or _hash_bytes(boundary_raw) != marker["claim_boundary_sha256"]):
            raise QualificationRunnerError(
                "final marker/claim 与 claim boundary 冻结输入不一致")
        confirmatory_audit_hash = _require_confirmatory_admission(
            firewall=firewall, claim_sha256=claim_hash,
            boundary_sha256=_hash_bytes(boundary_raw))
        if marker["confirmatory_audit_sha256"] != confirmatory_audit_hash:
            raise QualificationRunnerError(
                "final marker 与 confirmatory audit authority hash 错配")
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
            result_path, _canonical(result), research_uid=firewall.research_uid,
            label="qualification final score")
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
    confirmatory = sub.add_parser("run-confirmatory")
    confirmatory.add_argument("--system-root", required=True)
    confirmatory.add_argument("--source-root", required=True)
    confirmatory.add_argument("--gpu-contract")
    audit = sub.add_parser("audit-confirmatory")
    audit.add_argument("--audit-input", required=True)
    audit.add_argument("--authority-output", required=True)
    run = sub.add_parser("run-final")
    run.add_argument("--system-root", required=True)
    run.add_argument("--source-root", required=True)
    run.add_argument("--gpu-contract")
    sub.add_parser("score-final")
    args = parser.parse_args(argv)
    if args.command == "run-confirmatory":
        result = run_confirmatory(
            system_root=args.system_root, work_root=args.work_root,
            source_root=args.source_root,
            gpu_contract_path=(
                Path(args.gpu_contract) if args.gpu_contract else None))
    elif args.command == "audit-confirmatory":
        result = audit_confirmatory(
            work_root=args.work_root, audit_input_path=args.audit_input,
            authority_path=args.authority_output)
    elif args.command == "run-final":
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
    if args.command == "run-confirmatory":
        return 0 if result["status"] == "success" else 3
    if args.command == "audit-confirmatory":
        return 0 if result["status"] == "passed" else 3
    if args.command == "run-final":
        return 0 if result["failure_count"] == 0 else 3
    return 0 if result["status"] == "success" else 3


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "QualificationRunnerError", "audit_confirmatory", "freeze_source_tree",
    "run_confirmatory", "run_final", "score_final",
]
