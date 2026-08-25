"""Read-only wire values for retired Target execution records.

This module deliberately contains no process launch, filesystem spool,
supervision, reconciliation, or cancellation implementation.  It exists only
so already-persisted v1 Owner records can be decoded and audited after the
Target root lifecycle replaced the former execution Port.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import cast

from meta_research.bundle_protocol import (
    ReceiptProof,
    TargetWorkHandle,
    projection_plain_value,
)


class LegacyTargetExecutionError(RuntimeError):
    """Legacy decode/verification error; never launches an operation."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class TargetExecutionInputSpec:
    asset_ref: str
    version_ref: str
    relative_path: str
    mode: int
    byte_count: int
    content_sha256: str


@dataclass(frozen=True, slots=True)
class AcceptedTargetExecutionInputFile:
    asset_ref: str
    version_ref: str
    relative_path: str
    mode: int
    byte_count: int
    content_sha256: str
    content: bytes

    @property
    def spec(self) -> TargetExecutionInputSpec:
        return TargetExecutionInputSpec(
            asset_ref=self.asset_ref,
            version_ref=self.version_ref,
            relative_path=self.relative_path,
            mode=self.mode,
            byte_count=self.byte_count,
            content_sha256=self.content_sha256,
        )


@dataclass(frozen=True, slots=True)
class TargetExecutionOutputSpec:
    relative_path: str
    role: str
    max_bytes: int


@dataclass(frozen=True, slots=True)
class TargetExecutionOutputFile:
    relative_path: str
    role: str
    byte_count: int
    content_sha256: str
    content: bytes


@dataclass(frozen=True, slots=True)
class TargetExecutionOutputManifestEntry:
    relative_path: str
    role: str
    byte_count: int
    content_sha256: str


@dataclass(frozen=True, slots=True)
class TargetExecutionOutputManifest:
    schema_ref: str
    entries: tuple[TargetExecutionOutputManifestEntry, ...]
    total_bytes: int
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class TargetExecutionBudget:
    wall_timeout_seconds: float
    cpu_time_seconds: int
    cpu_millis: int
    memory_bytes: int
    stdout_max_bytes: int
    event_max_count: int
    event_max_bytes: int
    output_max_bytes: int = 64 * 1024 * 1024
    output_max_files: int = 256
    pids_max: int = 256
    tmpfs_max_bytes: int = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class TargetMeasurementAuthorityBinding:
    authority_ref: str
    acceptance_receipt: ReceiptProof


@dataclass(frozen=True, slots=True)
class TargetExecutionRequest:
    quest_ref: str
    execution_request_ref: str
    handle: TargetWorkHandle
    execution_eligibility_ref: str
    execution_eligibility_receipt: ReceiptProof
    measurement_authority: TargetMeasurementAuthorityBinding
    implementation_artifact_ref: str
    implementation_tree_sha256: str
    implementation_bundle_sha256: str
    entrypoint: str
    input_files: tuple[TargetExecutionInputSpec, ...]
    input_manifest_sha256: str
    stdout_role: str
    output_files: tuple[TargetExecutionOutputSpec, ...]
    output_spec_sha256: str
    mode: str
    argv: tuple[str, ...]
    budget: TargetExecutionBudget
    container_image_digest: str | None = None


@dataclass(frozen=True, slots=True)
class AcceptedTargetExecutionAdmission:
    handle: TargetWorkHandle
    execution_eligibility_ref: str
    execution_eligibility_receipt: ReceiptProof
    measurement_authority: TargetMeasurementAuthorityBinding
    implementation_artifact_ref: str
    implementation_tree_sha256: str
    implementation_bundle_sha256: str
    implementation_bytes: bytes
    accepted_input_files: tuple[AcceptedTargetExecutionInputFile, ...]


@dataclass(frozen=True, slots=True)
class TargetOperationHandle:
    token: str


@dataclass(frozen=True, slots=True)
class TargetExecutionExitReceipt:
    receipt_ref: str
    issuer: str
    kind: str
    subject_ref: str
    operation: TargetOperationHandle
    invocation_sha256: str
    request_sha256: str
    command_spec_sha256: str
    execution_eligibility_ref: str
    execution_eligibility_receipt_sha256: str
    execution_input_binding_ref: str
    execution_input_binding_receipt_sha256: str
    implementation_artifact_ref: str
    implementation_tree_sha256: str
    implementation_bundle_sha256: str
    input_manifest_sha256: str
    output_spec_sha256: str
    output_manifest_sha256: str
    output_file_count: int
    output_total_bytes: int
    status: str
    termination_reason: str
    returncode: int
    stdout_sha256: str
    stdout_bytes: int
    event_log_sha256: str
    event_log_bytes: int
    event_count: int
    terminal_log_sha256: str
    terminal_log_bytes: int
    process_tree_drained: bool
    container_absent: bool | None
    started_at: float
    completed_at: float

    def as_dict(self) -> dict[str, object]:
        return cast(dict[str, object], projection_plain_value(self))


@dataclass(frozen=True, slots=True)
class TargetExecutionTerminalResult:
    exit_receipt: TargetExecutionExitReceipt
    stdout_content: bytes
    terminal_log_content: bytes
    output_manifest: TargetExecutionOutputManifest
    output_files: tuple[TargetExecutionOutputFile, ...]


@dataclass(frozen=True, slots=True)
class RecoveredTargetOperation:
    operation: TargetOperationHandle
    request: TargetExecutionRequest
    request_sha256: str
    terminal_result: TargetExecutionTerminalResult | None


@dataclass(frozen=True, slots=True)
class TargetOperationInventoryScope:
    scope_kind: str
    scope_ref: str

    def as_dict(self) -> dict[str, object]:
        return cast(dict[str, object], projection_plain_value(self))


@dataclass(frozen=True, slots=True)
class TargetOperationInventoryItem:
    operation: TargetOperationHandle
    quest_ref: str | None
    execution_request_ref: str | None
    target_ref: str | None
    target_run_ref: str | None
    execution_attempt_ref: str | None
    execution_fence_ref: str | None
    request_sha256: str | None
    command_spec_sha256: str | None
    execution_eligibility_ref: str | None
    execution_eligibility_receipt_sha256: str | None
    measurement_authority_ref: str | None
    measurement_authority_receipt_ref: str | None
    measurement_authority_receipt_subject_ref: str | None
    execution_input_binding_ref: str | None
    execution_input_binding_receipt_sha256: str | None
    implementation_artifact_ref: str | None
    implementation_tree_sha256: str | None
    implementation_bundle_sha256: str | None
    input_manifest_sha256: str | None
    output_spec_sha256: str | None
    output_manifest_sha256: str | None
    fence_current: bool | None
    reconcile_status: str
    terminal_status: str | None
    process_tree_drained: bool | None
    container_absent: bool | None
    exit_receipt_ref: str | None
    exit_receipt_subject_ref: str | None
    exit_receipt_sha256: str | None

    def as_dict(self) -> dict[str, object]:
        return cast(dict[str, object], projection_plain_value(self))


@dataclass(frozen=True, slots=True)
class TargetOperationInventoryReceipt:
    issuer: str
    kind: str
    subject_ref: str
    content_sha256: str
    receipt_ref: str
    signature: str

    def as_dict(self) -> dict[str, object]:
        return cast(dict[str, object], projection_plain_value(self))


@dataclass(frozen=True, slots=True)
class TargetOperationInventory:
    scope: TargetOperationInventoryScope
    items: tuple[TargetOperationInventoryItem, ...]
    observed_at: float
    receipt: TargetOperationInventoryReceipt

    def as_dict(self) -> dict[str, object]:
        return cast(dict[str, object], projection_plain_value(self))


class TargetOperationRecoveryStatus(str, Enum):
    ABSENT = "absent"
    CURRENT_NONTERMINAL = "current_nonterminal"
    CURRENT_TERMINAL = "current_terminal"
    OUTCOME_UNKNOWN = "outcome_unknown"


@dataclass(frozen=True, slots=True)
class TargetExecutionOutcomeUnknownFact:
    fact_ref: str
    fact_sha256: str
    handle: TargetWorkHandle
    measurement_authority: TargetMeasurementAuthorityBinding
    inventory: TargetOperationInventory


@dataclass(frozen=True, slots=True)
class RecoveredTargetOperationState:
    status: TargetOperationRecoveryStatus
    handle: TargetWorkHandle
    measurement_authority: TargetMeasurementAuthorityBinding
    operation: RecoveredTargetOperation | None = None
    outcome_unknown: TargetExecutionOutcomeUnknownFact | None = None


def target_execution_input_manifest_sha256(
    inputs: tuple[TargetExecutionInputSpec, ...],
) -> str:
    """Recompute the retired v1 manifest hash without loading an executor."""

    value = {
        "schema_ref": "meta-research/target-execution-input-manifest/v1",
        "entries": projection_plain_value(inputs),
    }
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "AcceptedTargetExecutionAdmission",
    "AcceptedTargetExecutionInputFile",
    "RecoveredTargetOperation",
    "RecoveredTargetOperationState",
    "TargetExecutionExitReceipt",
    "TargetExecutionOutcomeUnknownFact",
    "LegacyTargetExecutionError",
    "TargetExecutionRequest",
    "TargetExecutionTerminalResult",
    "TargetMeasurementAuthorityBinding",
    "TargetOperationHandle",
    "TargetOperationRecoveryStatus",
    "target_execution_input_manifest_sha256",
]
