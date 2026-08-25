"""Issuer-owned records for the formal-v3 TargetRun production seam.

These records do not replace the canonical Bundle protocol.  They preserve the
Owner facts needed to construct and re-verify that protocol while keeping the
domain TargetRun and its protected Experiment execution as distinct identities.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import cast

from meta_research.bundle_protocol import (
    AcceptedInputAssetProof,
    CodeReviewRecord,
    ContentBindingProof,
    ExecutionInputBindingProof,
    FormalPlan,
    ProtocolAggregationProof,
    ProtocolPart,
    ReceiptProof,
    ResultReviewRecord,
    TargetCandidate,
    TargetExecutionPreflight,
    TargetWorkHandle,
)
from meta_research.bundle_target_contract import NormalizedCompletionContract
from meta_research.owners.common import AcceptanceReceipt, AcceptedAssetBinding


TARGET_COMPLETION_HANDOFF_SCHEMA = "meta-research/target-completion-handoff/v1"
TARGET_COMPLETION_ARTIFACT_ROLES = frozenset(
    {"implementation", "checkpoint", "result", "log", "analysis"}
)
_TARGET_COMPLETION_MAX_ARTIFACTS = 64
_TARGET_COMPLETION_MAX_REF_BYTES = 512
_TARGET_COMPLETION_MAX_SUMMARY_BYTES = 16_384


class TargetCompletionHandoffError(ValueError):
    """Typed fail-closed decoder error for a Target root's final handoff."""

    def __init__(self, code: str = "target_completion_handoff_invalid") -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class TargetCompletionArtifact:
    role: str
    relative_path: str


@dataclass(frozen=True, slots=True)
class TargetCompletionHandoff:
    """One final, closed handoff after the Target root finishes its lifecycle."""

    schema_ref: str
    target_ref: str
    target_run_ref: str
    status: str
    artifacts: tuple[TargetCompletionArtifact, ...]
    result_document_path: str
    summary: str


def decode_target_completion_handoff(document: str) -> TargetCompletionHandoff:
    """Decode the Target root's closed final handoff document."""

    def exact_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise TargetCompletionHandoffError()
            value[key] = item
        return value

    def reject_constant(_value: str) -> object:
        raise TargetCompletionHandoffError()

    if type(document) is not str or not document:
        raise TargetCompletionHandoffError()
    try:
        value = json.loads(
            document,
            object_pairs_hook=exact_object,
            parse_constant=reject_constant,
        )
    except (TypeError, ValueError, RecursionError, json.JSONDecodeError) as error:
        raise TargetCompletionHandoffError() from error
    if type(value) is not dict:
        raise TargetCompletionHandoffError()
    required_fields = {
        "schema_ref",
        "target_ref",
        "target_run_ref",
        "status",
        "artifacts",
        "result_document_path",
        "summary",
    }
    if set(value) != required_fields or type(value["artifacts"]) is not list:
        raise TargetCompletionHandoffError()
    artifacts: list[TargetCompletionArtifact] = []
    for raw_artifact in value["artifacts"]:
        if type(raw_artifact) is not dict or set(raw_artifact) != {
            "role",
            "relative_path",
        }:
            raise TargetCompletionHandoffError()
        artifacts.append(
            TargetCompletionArtifact(
                role=raw_artifact["role"],
                relative_path=raw_artifact["relative_path"],
            )
        )
    return validate_target_completion_handoff(
        TargetCompletionHandoff(
            schema_ref=value["schema_ref"],
            target_ref=value["target_ref"],
            target_run_ref=value["target_run_ref"],
            status=value["status"],
            artifacts=tuple(artifacts),
            result_document_path=value["result_document_path"],
            summary=value["summary"],
        )
    )


def validate_target_completion_handoff(
    handoff: TargetCompletionHandoff,
    *,
    expected_target_ref: str | None = None,
    expected_target_run_ref: str | None = None,
) -> TargetCompletionHandoff:
    """Validate and optionally bind a final handoff to its admitted Target run."""

    if type(handoff) is not TargetCompletionHandoff:
        raise TargetCompletionHandoffError()
    if handoff.schema_ref != TARGET_COMPLETION_HANDOFF_SCHEMA:
        raise TargetCompletionHandoffError()
    if handoff.status != "completed":
        raise TargetCompletionHandoffError()
    _validate_target_completion_text(
        handoff.target_ref, maximum_bytes=_TARGET_COMPLETION_MAX_REF_BYTES
    )
    _validate_target_completion_text(
        handoff.target_run_ref, maximum_bytes=_TARGET_COMPLETION_MAX_REF_BYTES
    )
    _validate_target_completion_text(
        handoff.summary, maximum_bytes=_TARGET_COMPLETION_MAX_SUMMARY_BYTES
    )
    if expected_target_ref is not None and handoff.target_ref != expected_target_ref:
        raise TargetCompletionHandoffError()
    if (
        expected_target_run_ref is not None
        and handoff.target_run_ref != expected_target_run_ref
    ):
        raise TargetCompletionHandoffError()
    if (
        type(handoff.artifacts) is not tuple
        or not handoff.artifacts
        or len(handoff.artifacts) > _TARGET_COMPLETION_MAX_ARTIFACTS
    ):
        raise TargetCompletionHandoffError()
    artifact_paths: set[str] = set()
    implementation_paths: set[str] = set()
    result_paths: set[str] = set()
    for artifact in handoff.artifacts:
        if type(artifact) is not TargetCompletionArtifact:
            raise TargetCompletionHandoffError()
        if (
            type(artifact.role) is not str
            or artifact.role not in TARGET_COMPLETION_ARTIFACT_ROLES
        ):
            raise TargetCompletionHandoffError()
        _validate_target_completion_relative_path(artifact.relative_path)
        if artifact.relative_path in artifact_paths:
            raise TargetCompletionHandoffError()
        artifact_paths.add(artifact.relative_path)
        if artifact.role == "implementation":
            implementation_paths.add(artifact.relative_path)
        elif artifact.role == "result":
            result_paths.add(artifact.relative_path)
    _validate_target_completion_relative_path(handoff.result_document_path)
    if (
        not implementation_paths
        or handoff.result_document_path not in result_paths
    ):
        raise TargetCompletionHandoffError()
    return handoff


def _validate_target_completion_text(
    value: object, *, maximum_bytes: int
) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or _target_completion_utf8_size(value) > maximum_bytes
        or "\x00" in value
    ):
        raise TargetCompletionHandoffError()
    return cast(str, value)


def _target_completion_utf8_size(value: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeError as error:
        raise TargetCompletionHandoffError() from error


def _validate_target_completion_relative_path(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or value in {".", ".."}
        or _target_completion_utf8_size(value) > 1024
        or value != value.strip()
        or "\\" in value
        or "\x00" in value
        or "\r" in value
        or "\n" in value
        or value.startswith("/")
        or value.endswith("/")
        or "//" in value
    ):
        raise TargetCompletionHandoffError()
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or str(path) != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise TargetCompletionHandoffError()
    return cast(str, value)


@dataclass(frozen=True, slots=True)
class AcceptedTargetImplementationArtifact:
    implementation_revision_ref: str
    metadata_content_hash_ref: str
    artifact: AcceptedAssetBinding
    payload_hash: str
    receipt: AcceptanceReceipt
    accepted_at: float


@dataclass(frozen=True, slots=True)
class AcceptedTargetImplementationBundle:
    """Global immutable code tree for one formal Implementation Revision."""

    implementation_revision_ref: str
    bundle_content_hash_ref: str
    artifact: AcceptedAssetBinding
    payload_hash: str
    receipt: AcceptanceReceipt
    accepted_at: float


@dataclass(frozen=True, slots=True)
class AcceptedTargetImplementationBundleUsage:
    """One Target's issuer-verified use of a global immutable revision."""

    usage_ref: str
    target_ref: str
    implementation_revision_ref: str
    origin_kind: str
    bundle: AcceptedTargetImplementationBundle
    revision_authority_receipt: AcceptanceReceipt
    payload_hash: str
    receipt: AcceptanceReceipt
    accepted_at: float


@dataclass(frozen=True, slots=True)
class AcceptedTargetInputAssetProjection:
    target_ref: str
    asset: AcceptedAssetBinding
    rm_proof_receipt: AcceptanceReceipt
    source_role_ref: str
    source_role_receipt: AcceptanceReceipt
    rg_proof_receipt: AcceptanceReceipt

    def as_bundle_proof(self) -> AcceptedInputAssetProof:
        return AcceptedInputAssetProof(
            asset_ref=self.asset.asset_ref,
            rm_acceptance_receipt=receipt_proof(
                self.rm_proof_receipt,
                subject_ref=self.asset.asset_ref,
            ),
            rg_role_receipt=receipt_proof(
                self.rg_proof_receipt,
                subject_ref=self.asset.asset_ref,
            ),
        )


@dataclass(frozen=True, slots=True)
class TargetHarnessAdmission:
    target_ref: str
    target_run_ref: str
    harness_request_ref: str
    harness_family: str
    model_ref: str
    auth_profile_ref: str
    root_session_ref: str
    execution_attempt_ref: str
    execution_fence_ref: str
    native_session_ref: str | None
    capability_binding_hash: str
    full_conformance_binding_hash: str
    status: str
    failure_code: str | None


@dataclass(frozen=True, slots=True)
class TargetRunWorkspace:
    """Opaque Owner lease for one Target Harness attempt's private tree."""

    workspace_ref: str
    target_ref: str
    target_run_ref: str
    root_session_ref: str
    target_attempt_ref: str
    target_fence_ref: str
    ordinal: int
    implementation_relative_path: str
    inputs_relative_path: str
    payload_hash: str
    receipt: AcceptanceReceipt
    status: str
    created_at: float


@dataclass(frozen=True, slots=True)
class AcceptedTargetFormalPlanProjection:
    graph_ref: str
    formal_plan: FormalPlan
    plan_document_hash: str
    source_acceptance_receipt: AcceptanceReceipt
    completion_contract: NormalizedCompletionContract
    completion_contract_hash: str
    briefs_hash: str
    projection_digest: str
    receipt: AcceptanceReceipt
    accepted_at: float


@dataclass(frozen=True, slots=True)
class AcceptedTargetCandidateProjection:
    """RG's canonical fixed-prototype TargetCandidate projection.

    ``source_spec_hash`` and ``source_acceptance_receipt`` retain the complete
    formal-v3 wrapper fact.  ``projection_digest`` is a separate subject for
    exactly ``{target_ref, candidate}``; neither receipt is relabelled.
    """

    target_ref: str
    graph_ref: str
    candidate: TargetCandidate
    source_spec_hash: str
    source_acceptance_receipt: AcceptanceReceipt
    projection_digest: str
    receipt: AcceptanceReceipt
    accepted_at: float


@dataclass(frozen=True, slots=True)
class AcceptedTargetCodeReview:
    target_ref: str
    target_run_ref: str
    harness_operation_ref: str
    reviewer_completion_evidence_ref: str
    review: CodeReviewRecord
    evidence_binding: ContentBindingProof | None
    evidence_receipt: ReceiptProof | None
    review_scope: CodeReviewScope | None = None
    candidate_ready_evidence_ref: str | None = None
    self_check_evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AcceptedTargetResultReview:
    target_ref: str
    target_run_ref: str
    harness_operation_ref: str
    reviewer_completion_evidence_ref: str
    review: ResultReviewRecord
    evidence_binding: ContentBindingProof
    evidence_receipt: ReceiptProof


@dataclass(frozen=True, slots=True)
class TargetReviewTurnEvidence:
    """Issuer-read post-turn review payload and its root evidence refs."""

    target_ref: str
    target_run_ref: str
    review_kind: str
    harness_operation_ref: str
    review: CodeReviewRecord | ResultReviewRecord
    review_scope: CodeReviewScope | None
    candidate_ready_evidence_ref: str | None
    self_check_evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AcceptedTargetExecutionEligibility:
    """AR's execution gate over the complete reviewed implementation closure."""

    eligibility_ref: str
    handle: TargetWorkHandle
    implementation_bundle: AcceptedTargetImplementationBundle
    implementation_bundle_usage: AcceptedTargetImplementationBundleUsage
    preflight: TargetExecutionPreflight
    code_review_acceptance_receipt: AcceptanceReceipt | None
    harness_operation_ref: str
    payload_hash: str
    receipt: AcceptanceReceipt
    accepted_at: float

    @property
    def implementation_artifact(self) -> AcceptedTargetImplementationBundle:
        """Read-only compatibility name; the fact is always a code bundle."""

        return self.implementation_bundle


@dataclass(frozen=True, slots=True)
class AcceptedTargetExecutionInputBinding:
    target_ref: str
    target_run_ref: str
    target_attempt_ref: str
    target_fence_ref: str
    target_spec_hash: str
    target_scope_binding_hash: str
    proof: ExecutionInputBindingProof
    accepted_at: float


@dataclass(frozen=True, slots=True)
class TargetProtectedExecutionBinding:
    """Legacy Experiment-backed bridge; formal-v3 TargetRun must not create it."""

    binding_ref: str
    target_ref: str
    ordinal: int
    target_run_ref: str
    target_attempt_ref: str
    target_fence_ref: str
    input_binding_ref: str
    experiment_run_ref: str
    experiment_attempt_ref: str
    experiment_fence_ref: str
    evaluation_attempt_ref: str
    execution_request_ref: str
    definition_hash: str
    experiment_request_receipt: AcceptanceReceipt
    receipt: AcceptanceReceipt
    accepted_at: float


@dataclass(frozen=True, slots=True)
class TargetGenericExecutionBinding:
    """RG acceptance of one issuer-verified terminal generic operation.

    The generic operation is the execution identity.  Measurement identities
    are accepted only afterwards and therefore do not appear in this record.
    """

    binding_ref: str
    target_ref: str
    ordinal: int
    target_run_ref: str
    target_attempt_ref: str
    target_fence_ref: str
    input_binding_ref: str
    input_binding_receipt: ReceiptProof
    execution_eligibility_ref: str
    execution_eligibility_receipt: ReceiptProof
    operation_handle: str
    execution_request_ref: str
    request_hash: str
    command_spec_hash: str
    terminal_status: str
    exit_receipt_ref: str
    exit_receipt_hash: str
    process_tree_drained: bool
    currentness_known: bool
    current: bool
    receipt: AcceptanceReceipt
    accepted_at: float


@dataclass(frozen=True, slots=True)
class TargetGenericResultAsset:
    """One RM-accepted byte stream from a signed generic operation result."""

    role: str
    ordinal: int
    relative_path: str
    binding: AcceptedAssetBinding


@dataclass(frozen=True, slots=True)
class AcceptedTargetGenericResultManifest:
    """RM manifest reconstructed from the execution port's terminal spool."""

    manifest_ref: str
    target_ref: str
    target_run_ref: str
    target_attempt_ref: str
    target_fence_ref: str
    generic_binding_ref: str
    operation_handle: str
    output_manifest_sha256: str
    entries: tuple[TargetGenericResultAsset, ...]
    payload_hash: str
    receipt: AcceptanceReceipt
    accepted_at: float


@dataclass(frozen=True, slots=True)
class AcceptedTargetGenericMeasurement:
    """RG domain identities derived from one accepted generic result document."""

    measurement_ref: str
    target_ref: str
    target_run_ref: str
    target_attempt_ref: str
    target_fence_ref: str
    generic_binding_ref: str
    manifest_ref: str
    measurement_source_version_ref: str
    experiment_keys: tuple[str, ...]
    measurement_unit_key: str
    variant_run_ref: str
    evaluation_ref: str
    protocol_version_ref: str
    evaluation_attempt_ref: str
    metric_result_ref: str
    metric_values: tuple[int | float, ...]
    result_disposition: str
    checkpoint_artifact_refs: tuple[str, ...]
    variant_run_input_binding: ExecutionInputBindingProof
    evaluation_attempt_input_binding: ExecutionInputBindingProof
    protocol_internal_parts: tuple[ProtocolPart, ...]
    protocol_aggregation_proof: ProtocolAggregationProof | None
    payload_hash: str
    receipt: AcceptanceReceipt
    accepted_at: float


@dataclass(frozen=True, slots=True)
class AcceptedTargetMeasurementAttempt:
    """Native RG identities/roles bound to one accepted Target terminal."""

    attempt_binding_ref: str
    target_ref: str
    target_run_ref: str
    target_attempt_ref: str
    target_fence_ref: str
    authority_ref: str
    authority_hash: str
    generic_binding_ref: str
    manifest_ref: str
    variant_run_ref: str
    variant_run_disposition: str
    evaluation_attempt_ref: str
    variant_run_input_binding: ExecutionInputBindingProof
    evaluation_attempt_input_binding: ExecutionInputBindingProof
    checkpoint_role_refs: tuple[str, ...]
    result_role_ref: str
    payload_hash: str
    receipt: AcceptanceReceipt
    accepted_at: float


@dataclass(frozen=True, slots=True)
class AcceptedTargetGenericExecutionClosure:
    """AR closure after issuer-backed result review of a generic operation."""

    closure_ref: str
    target_ref: str
    target_run_ref: str
    target_attempt_ref: str
    target_fence_ref: str
    generic_binding_ref: str
    result_manifest_ref: str
    measurement_ref: str
    result_review_ref: str
    payload_hash: str
    receipt: AcceptanceReceipt
    accepted_at: float

    def receipt_proof(self) -> ReceiptProof:
        return receipt_proof(self.receipt, subject_ref=self.target_attempt_ref)


@dataclass(frozen=True, slots=True)
class AcceptedTargetNativeExecutionClosure:
    """AR closure over native RG measurement facts and one fresh review."""

    closure_ref: str
    target_ref: str
    target_run_ref: str
    target_attempt_ref: str
    target_fence_ref: str
    generic_binding_ref: str
    result_manifest_ref: str
    attempt_binding_ref: str
    evaluation_attempt_ref: str
    metric_result_ref: str
    result_review_ref: str
    payload_hash: str
    receipt: AcceptanceReceipt
    accepted_at: float

    def receipt_proof(self) -> ReceiptProof:
        return receipt_proof(self.receipt, subject_ref=self.target_attempt_ref)


@dataclass(frozen=True, slots=True)
class TargetResultManifestEntry:
    role: str
    ordinal: int
    role_ref: str
    subject_kind: str
    subject_ref: str
    asset_ref: str
    version_ref: str
    content_hash: str
    manifest_hash: str
    asset_receipt_ref: str
    role_receipt_ref: str


@dataclass(frozen=True, slots=True)
class AcceptedTargetResultManifest:
    manifest_ref: str
    target_ref: str
    target_run_ref: str
    variant_run_ref: str
    evaluation_attempt_ref: str
    metric_result_ref: str
    experiment_run_ref: str
    experiment_attempt_ref: str
    experiment_fence_ref: str
    entries: tuple[TargetResultManifestEntry, ...]
    payload_hash: str
    receipt: AcceptanceReceipt
    accepted_at: float


@dataclass(frozen=True, slots=True)
class AcceptedTargetExecutionClosure:
    closure_ref: str
    target_ref: str
    target_run_ref: str
    target_attempt_ref: str
    target_fence_ref: str
    protected_binding_ref: str
    experiment_run_ref: str
    experiment_attempt_ref: str
    experiment_fence_ref: str
    evaluation_attempt_ref: str
    experiment_result_hash: str
    result_manifest_ref: str
    formal_metric_ref: str
    result_review_ref: str
    payload_hash: str
    receipt: AcceptanceReceipt
    accepted_at: float

    def receipt_proof(self) -> ReceiptProof:
        return receipt_proof(self.receipt, subject_ref=self.target_attempt_ref)


def receipt_proof(
    receipt: AcceptanceReceipt,
    *,
    subject_ref: str,
) -> ReceiptProof:
    """Project a freshly issuer-verified Owner receipt into Bundle form."""

    if receipt.subject_ref != subject_ref:
        raise ValueError("Owner receipt subject drift")
    return ReceiptProof(
        receipt_ref=receipt.receipt_ref,
        subject_ref=subject_ref,
        verified=True,
        currentness_known=True,
        current=True,
    )


__all__ = [
    "AcceptedTargetCodeReview",
    "AcceptedTargetExecutionEligibility",
    "AcceptedTargetImplementationArtifact",
    "AcceptedTargetImplementationBundle",
    "AcceptedTargetImplementationBundleUsage",
    "AcceptedTargetInputAssetProjection",
    "AcceptedTargetFormalPlanProjection",
    "AcceptedTargetExecutionClosure",
    "AcceptedTargetGenericExecutionClosure",
    "AcceptedTargetGenericMeasurement",
    "AcceptedTargetGenericResultManifest",
    "AcceptedTargetExecutionInputBinding",
    "AcceptedTargetResultManifest",
    "AcceptedTargetResultReview",
    "TargetHarnessAdmission",
    "TargetRunWorkspace",
    "TargetReviewTurnEvidence",
    "TargetGenericExecutionBinding",
    "TargetGenericResultAsset",
    "TargetCompletionArtifact",
    "TargetCompletionHandoff",
    "TargetCompletionHandoffError",
    "TARGET_COMPLETION_ARTIFACT_ROLES",
    "TARGET_COMPLETION_HANDOFF_SCHEMA",
    "TargetProtectedExecutionBinding",
    "TargetResultManifestEntry",
    "decode_target_completion_handoff",
    "receipt_proof",
    "validate_target_completion_handoff",
]
