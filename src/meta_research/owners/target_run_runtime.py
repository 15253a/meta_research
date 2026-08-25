"""Issuer-owned authority seams for independent formal-v3 TargetRuns.

The fixed Bundle protocol remains the only cross-module value contract.  These
small Owner submodules persist and re-verify the facts that cannot be inferred
from that value contract: actual RM artifact bytes, asset-role projections,
Harness child-review evidence, and the TargetRun-to-Experiment bridge.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass, fields, is_dataclass, replace
from pathlib import Path
from types import UnionType
from typing import (
    Any,
    Protocol,
    Union,
    get_args,
    get_origin,
    get_type_hints,
)

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from meta_research.bundle_protocol import (
    AcceptedInputAssetProof,
    CodeReviewRecord,
    CodeReviewScope,
    ContentBindingProof,
    ExecutionInputBindingProof,
    ExperimentBrief,
    FormalPlan,
    ProtocolAggregationProof,
    ProtocolPart,
    ReceiptProof,
    ResultReviewRecord,
    StopDecisionProof,
    TargetCandidate,
    TargetExecutionPreflight,
    TargetWorkHandle,
    TechnicalBlocker,
    canonical_projection_bytes,
    projection_plain_value,
)
from meta_research.bundle_target_contract import (
    BundleTargetContractError,
    NormalizedCompletionContract,
    completion_contract_hash,
    formal_target_candidate_from_dict,
    normalized_completion_contract_to_dict,
)
from meta_research.database import Database
from meta_research.experiment_contract import (
    AcceptedExperimentAssetRole,
    EXPERIMENT_RESULT_DISPOSITIONS,
    ExperimentDomainAdmission,
    ExperimentProviderResult,
    ProtocolExperimentIntent,
    experiment_result_schema_ref,
)
from meta_research.feed import DurableFeed
from meta_research.owners.agent_runtime_harness import (
    AgentRuntimeHarnessInterface,
    AgentRuntimeTargetChildSession,
    AgentRuntimeTargetSuccessorReservation,
)
from meta_research.owners.common import (
    AcceptanceReceipt,
    AcceptedTargetCommitTransition,
    AcceptedAssetBinding,
    OwnerConflict,
    canonical_hash,
    canonical_json,
    new_ref,
)
from meta_research.owners.research_graph import (
    EXPERIMENT_EXECUTION_REQUEST_RECEIPT_KIND,
    TARGET_SPEC_CONTENT_RECEIPT_KIND,
    AcceptedFormalPlanContent,
    AcceptedAssetRole,
    AcceptedTargetGraph,
)
from meta_research.owners.research_memory import (
    IMPLEMENTATION_CONTENT_RECEIPT_KIND,
    REUSE_SOURCE_VERSION_RECEIPT_KIND,
    AcceptedImplementationRevisionContent,
    AssetIntakeRequest,
)
from meta_research.target_run_contract import (
    TargetRunContractError,
    _bundle_escalation_digest,
    validate_code_review,
    validate_protected_execution_admission,
    validate_result_review,
    validate_target_work_handle,
)
from meta_research.target_execution_legacy import (
    AcceptedTargetExecutionAdmission,
    AcceptedTargetExecutionInputFile,
    RecoveredTargetOperation,
    RecoveredTargetOperationState,
    TargetExecutionExitReceipt,
    TargetExecutionOutcomeUnknownFact,
    LegacyTargetExecutionError,
    TargetExecutionRequest,
    TargetExecutionTerminalResult,
    TargetMeasurementAuthorityBinding,
    TargetOperationHandle,
    TargetOperationRecoveryStatus,
    target_execution_input_manifest_sha256,
)
from meta_research.target_implementation_bundle import (
    TargetImplementationBundleError,
    parse_target_implementation_bundle,
    parse_target_single_file_bundle,
    validate_bundle_relative_path,
)
from meta_research.target_run_runtime_contract import (
    AcceptedTargetCodeReview,
    AcceptedTargetCandidateProjection,
    AcceptedTargetExecutionClosure,
    AcceptedTargetExecutionEligibility,
    AcceptedTargetExecutionInputBinding,
    AcceptedTargetGenericExecutionClosure,
    AcceptedTargetGenericMeasurement,
    AcceptedTargetGenericResultManifest,
    AcceptedTargetMeasurementAttempt,
    AcceptedTargetNativeExecutionClosure,
    AcceptedTargetImplementationArtifact,
    AcceptedTargetImplementationBundle,
    AcceptedTargetImplementationBundleUsage,
    AcceptedTargetInputAssetProjection,
    AcceptedTargetFormalPlanProjection,
    AcceptedTargetResultReview,
    AcceptedTargetResultManifest,
    TargetHarnessAdmission,
    TargetGenericExecutionBinding,
    TargetGenericResultAsset,
    TargetProtectedExecutionBinding,
    TargetReviewTurnEvidence,
    TargetResultManifestEntry,
    TargetRunWorkspace,
    receipt_proof,
)

@dataclass(frozen=True, slots=True)
class TargetExecutionFailureProjection:
    """Issuer-reverified failed operation and its canonical recovery facts."""

    operation: TargetOperationHandle
    request_sha256: str
    exit_receipt: TargetExecutionExitReceipt
    generic_binding_ref: str
    generic_binding_receipt: AcceptanceReceipt
    failure_ref: str
    blocker: TechnicalBlocker | None
    stop_decision: StopDecisionProof | None
    replacement_handle: TargetWorkHandle | None


@dataclass(frozen=True, slots=True)
class _TargetExecutionFailureBasis:
    """Exact immutable terminal basis, before a successor can exist."""

    recovered: RecoveredTargetOperation
    generic_binding: TargetGenericExecutionBinding
    blocker_ref: str
    blocker_reason: str
    stop_decision: StopDecisionProof | None


@dataclass(frozen=True, slots=True)
class TargetExecutionOutcomeUnknownProjection:
    """Issuer-authenticated undecidable operation and terminal blocker."""

    fact: TargetExecutionOutcomeUnknownFact
    blocker: TechnicalBlocker


RM_IMPLEMENTATION_ARTIFACT_RECEIPT_KIND = (
    "target_implementation_artifact_accepted"
)
RM_TARGET_IMPLEMENTATION_BUNDLE_RECEIPT_KIND = (
    "target_implementation_bundle_accepted"
)
RM_TARGET_IMPLEMENTATION_BUNDLE_USAGE_RECEIPT_KIND = (
    "target_implementation_bundle_usage_accepted"
)
RM_TARGET_INPUT_ASSET_RECEIPT_KIND = "target_input_asset_accepted"
RM_TARGET_RESULT_MANIFEST_RECEIPT_KIND = "target_result_manifest_accepted"
RM_TARGET_GENERIC_RESULT_MANIFEST_RECEIPT_KIND = (
    "target_generic_result_manifest_accepted"
)
RG_TARGET_INPUT_ASSET_ROLE_RECEIPT_KIND = "target_input_asset_role_accepted"
RG_TARGET_FORMAL_PLAN_PROJECTION_RECEIPT_KIND = (
    "target_formal_plan_projection_accepted"
)
RG_TARGET_CANDIDATE_PROJECTION_RECEIPT_KIND = (
    "target_candidate_projection_accepted"
)
RG_TARGET_EXECUTION_INPUT_RECEIPT_KIND = "target_execution_input_accepted"
RG_TARGET_PROTECTED_EXECUTION_RECEIPT_KIND = "target_protected_execution_accepted"
RG_TARGET_GENERIC_EXECUTION_RECEIPT_KIND = (
    "target_generic_execution_accepted"
)
RG_TARGET_GENERIC_MEASUREMENT_RECEIPT_KIND = (
    "target_generic_measurement_accepted"
)
RG_TARGET_VARIANT_INPUT_RECEIPT_KIND = "target_variant_input_accepted"
RG_TARGET_EVALUATION_INPUT_RECEIPT_KIND = "target_evaluation_input_accepted"
RG_TARGET_PROTOCOL_AGGREGATION_RECEIPT_KIND = (
    "target_protocol_aggregation_accepted"
)
AR_TARGET_CODE_REVIEW_RECEIPT_KIND = "target_code_review_accepted"
AR_TARGET_RESULT_REVIEW_RECEIPT_KIND = "target_result_review_accepted"
AR_TARGET_EXECUTION_ELIGIBILITY_RECEIPT_KIND = (
    "target_execution_eligibility_accepted"
)
AR_TARGET_EXECUTION_CLOSURE_RECEIPT_KIND = "target_execution_closure_accepted"
AR_TARGET_GENERIC_EXECUTION_CLOSURE_RECEIPT_KIND = (
    "target_generic_execution_closure_accepted"
)
AR_TARGET_NATIVE_EXECUTION_CLOSURE_RECEIPT_KIND = (
    "target_native_execution_closure_accepted"
)
AR_TARGET_RUN_WORKSPACE_RECEIPT_KIND = "target_run_workspace_reserved"


@dataclass(frozen=True, slots=True)
class FrozenTargetCommitInputArtifact:
    """One exact RM-owned upstream artifact delivered to a Target root."""

    ordinal: int
    role: str
    declared_relative_path: str
    artifact_kind: str
    media_type: str
    version_ref: str
    content_hash: str
    tree_hash: str
    content: bytes


@dataclass(frozen=True, slots=True)
class FrozenTargetCommitInput:
    """Issuer-reverified TargetCommit manifest plus its exact RM bytes."""

    target_commit_ref: str
    target_ref: str
    target_run_ref: str
    manifest_ref: str
    manifest_payload_hash: str
    manifest_receipt_ref: str
    manifest_content: bytes
    artifacts: tuple[FrozenTargetCommitInputArtifact, ...]


def canonical_target_scope_binding(
    *,
    target_ref: str,
    target_run_ref: str,
    target_spec_hash: str,
    candidate: TargetCandidate,
    formal_plan: FormalPlan,
    accepted_input_refs: tuple[str, ...],
) -> dict[str, object]:
    if accepted_input_refs != tuple(sorted(set(accepted_input_refs))):
        raise OwnerConflict("target_scope_binding_invalid")
    return {
        "schema_ref": "meta-research/target-run-scope/v1",
        "target_ref": target_ref,
        "target_run_ref": target_run_ref,
        "target_spec_hash": target_spec_hash,
        "candidate": projection_plain_value(candidate),
        "formal_plan": projection_plain_value(formal_plan),
        "accepted_input_refs": list(accepted_input_refs),
    }


def canonical_formal_plan_projection_digest(
    *, formal_plan_ref: str, briefs: tuple[ExperimentBrief, ...]
) -> str:
    """Return the fixed prototype digest, not the source PlanDocument hash."""

    return canonical_hash(
        {
            "formal_plan_ref": formal_plan_ref,
            "briefs": projection_plain_value(briefs),
        }
    )


def canonical_target_candidate_projection_digest(
    *, target_ref: str, candidate: TargetCandidate
) -> str:
    """Return the fixed TargetRun candidate-content receipt subject."""

    return canonical_hash(
        {
            "target_ref": target_ref,
            "candidate": projection_plain_value(candidate),
        }
    )


class ResearchMemoryTargetVerifier(Protocol):
    def verify_implementation_content(self, **values: object) -> None: ...

    def verify_asset_binding(self, **values: object) -> None: ...

    def materialize_asset(self, memory_ref: str) -> object: ...

    def submit_asset_intake(
        self, request: AssetIntakeRequest, *, idempotency_key: str
    ) -> object: ...


class TargetImplementationBundleRevisionVerifier(Protocol):
    def verify_implementation_bundle_revision(
        self,
        *,
        target_ref: str,
        implementation_revision_ref: str,
    ) -> tuple[str, AcceptanceReceipt]: ...


class TargetGenericResultVerifier(Protocol):
    def query_generic_execution_terminal(
        self, binding_ref: str
    ) -> tuple[
        TargetGenericExecutionBinding,
        TargetExecutionRequest,
        TargetExecutionTerminalResult,
        AcceptedTargetExecutionInputBinding,
    ] | None: ...


class TargetMeasurementDomainAuthority(Protocol):
    """Read-only RG seam resolving one opaque Target authority binding."""

    def query_target_measurement_domain_authority(
        self,
        target_ref: str,
    ) -> object | None: ...


class ResearchGraphTargetVerifier(Protocol):
    def verify_asset_role_receipt(self, **values: object) -> None: ...

    def query_target_candidate_projection_source(
        self, *, target_ref: str
    ) -> dict[str, object]: ...


class ResearchGraphTargetReader(Protocol):
    def query_target_graph(self, request_ref: str) -> object | None: ...

    def query_formal_plan_content_acceptance(
        self, formal_plan_ref: str
    ) -> object | None: ...

    def verify_formal_plan_content_acceptance(self, **values: object) -> None: ...

    def query_target_formal_plan_projection_source(
        self, **values: object
    ) -> dict[str, object]: ...

    def query_target_launch_request(self, target_ref: str) -> object: ...

    def query_target_measurement_domain_authority(
        self, target_ref: str
    ) -> object | None: ...

    def verify_target_spec_content_receipt(self, **values: object) -> None: ...

    def query_experiment(self, evaluation_attempt_ref: str) -> object | None: ...

    def query_experiment_asset_roles(
        self, evaluation_attempt_ref: str
    ) -> tuple[AcceptedExperimentAssetRole, ...]: ...

    def query_formal_metric_result(
        self, evaluation_attempt_ref: str
    ) -> object | None: ...

    def query_target_measurement_attempt(
        self, evaluation_attempt_ref: str
    ) -> AcceptedTargetMeasurementAttempt | None: ...

    def query_target_formal_metric_result(
        self, evaluation_attempt_ref: str
    ) -> object | None: ...


class AgentRuntimeExperimentVerifier(Protocol):
    def query_experiment_run(self, evaluation_attempt_ref: str) -> object | None: ...

    def query_target_frontier_entry(self, target_ref: str) -> object | None: ...

    def verify_target_recovery_preflight_reuse(
        self,
        *,
        current_handle: TargetWorkHandle,
        preflight: TargetExecutionPreflight,
    ) -> TargetWorkHandle: ...

    def verify_experiment_execution_receipt(self, **values: object) -> object: ...


def _receipt(
    issuer: str,
    kind: str,
    receipt_ref: str,
    subject_ref: str,
    bindings: dict[str, object],
) -> AcceptanceReceipt:
    return AcceptanceReceipt(
        issuer=issuer,
        kind=kind,
        receipt_ref=receipt_ref,
        subject_ref=subject_ref,
        payload_hash=canonical_hash(
            {
                "issuer": issuer,
                "kind": kind,
                "receipt_ref": receipt_ref,
                "subject_ref": subject_ref,
                "bindings": bindings,
            }
        ),
    )


def _proof(receipt: AcceptanceReceipt) -> ReceiptProof:
    return receipt_proof(receipt, subject_ref=receipt.subject_ref)


def _decode_bundle_value(value: object, annotation: object) -> object:
    origin = get_origin(annotation)
    if origin in {Union, UnionType}:
        matches = []
        for option in get_args(annotation):
            try:
                matches.append(_decode_bundle_value(value, option))
            except (TypeError, ValueError):
                continue
        if len(matches) != 1:
            raise TypeError("non-canonical union")
        return matches[0]
    if origin is tuple:
        if type(value) is not list:
            raise TypeError("non-canonical tuple")
        arguments = get_args(annotation)
        if len(arguments) == 2 and arguments[1] is Ellipsis:
            return tuple(_decode_bundle_value(item, arguments[0]) for item in value)
        if len(arguments) != len(value):
            raise TypeError("non-canonical fixed tuple")
        return tuple(
            _decode_bundle_value(item, expected)
            for item, expected in zip(value, arguments, strict=True)
        )
    if annotation is Any:
        return value
    if annotation in {str, int, bool, float, type(None)}:
        if type(value) is not annotation:
            raise TypeError("non-canonical primitive")
        return value
    if isinstance(annotation, type) and is_dataclass(annotation):
        if type(value) is not dict:
            raise TypeError("non-canonical record")
        record_fields = fields(annotation)
        if set(value) != {item.name for item in record_fields}:
            raise TypeError("record fields changed")
        hints = get_type_hints(annotation)
        return annotation(
            **{
                item.name: _decode_bundle_value(value[item.name], hints[item.name])
                for item in record_fields
            }
        )
    raise TypeError("unsupported canonical annotation")


def _decode_stored_record(
    document: str,
    expected_hash: str,
    record_type: type[Any],
) -> object:
    value = json.loads(document)
    record = _decode_bundle_value(value, record_type)
    if (
        type(record) is not record_type
        or canonical_json(projection_plain_value(record)) != document
        or canonical_hash(value) != expected_hash
    ):
        raise ValueError("stored record hash drift")
    return record


class SQLiteTargetRunMemoryAuthority:
    """Research Memory's 0022 immutable TargetRun artifacts and projections."""

    def __init__(
        self,
        database: Database,
        feed: DurableFeed,
        verifier: ResearchMemoryTargetVerifier,
    ) -> None:
        self._database = database
        self._feed = feed
        self._verifier = verifier
        self._implementation_bundle_revision_verifier: (
            TargetImplementationBundleRevisionVerifier | None
        ) = None
        self._generic_result_verifier: TargetGenericResultVerifier | None = None

    def bind_implementation_bundle_revision_verifier(
        self,
        verifier: TargetImplementationBundleRevisionVerifier,
    ) -> None:
        current = self._implementation_bundle_revision_verifier
        if current is not None and current is not verifier:
            raise OwnerConflict(
                "target_implementation_bundle_verifier_already_bound"
            )
        self._implementation_bundle_revision_verifier = verifier

    def bind_generic_result_verifier(
        self, verifier: TargetGenericResultVerifier
    ) -> None:
        current = self._generic_result_verifier
        if current is not None and current is not verifier:
            raise OwnerConflict("target_generic_result_verifier_already_bound")
        self._generic_result_verifier = verifier

    def accept_implementation_artifact(
        self,
        *,
        implementation: AcceptedImplementationRevisionContent,
        artifact: AcceptedAssetBinding,
        idempotency_key: str,
    ) -> AcceptedTargetImplementationArtifact:
        self._verify_implementation(implementation)
        self._verify_asset(artifact)
        payload = {
            "implementation_revision_ref": implementation.implementation_revision_ref,
            "metadata_content_hash_ref": implementation.content_hash_ref,
            "artifact": artifact.as_dict(),
        }
        payload_hash = canonical_hash(payload)
        request_hash = canonical_hash({"command": "accept", "payload": payload})
        now = time.time()
        with self._database.write() as connection:
            replay = connection.execute(
                text(
                    "SELECT * FROM rm_target_implementation_artifacts WHERE "
                    "idempotency_key = :key OR implementation_revision_ref = :revision"
                ),
                {
                    "key": idempotency_key,
                    "revision": implementation.implementation_revision_ref,
                },
            ).first()
            if replay is not None:
                if replay.request_hash != request_hash:
                    raise OwnerConflict("target_implementation_artifact_conflict")
            else:
                receipt_ref = new_ref("rm_target_implementation_receipt")
                bindings = {
                    **payload,
                    "payload_hash": payload_hash,
                    "asset_receipt_ref": artifact.receipt.receipt_ref,
                    "asset_receipt_hash": artifact.receipt.payload_hash,
                }
                receipt = _receipt(
                    "research_memory",
                    RM_IMPLEMENTATION_ARTIFACT_RECEIPT_KIND,
                    receipt_ref,
                    implementation.content_hash_ref,
                    bindings,
                )
                try:
                    connection.execute(
                        text(
                            "INSERT INTO rm_target_implementation_artifacts "
                            "(implementation_revision_ref, metadata_content_hash_ref, "
                            "asset_ref, version_ref, artifact_content_hash, "
                            "artifact_manifest_hash, asset_receipt_ref, "
                            "asset_receipt_hash, payload_json, payload_hash, "
                            "idempotency_key, request_hash, receipt_ref, "
                            "receipt_hash, accepted_at) VALUES (:revision, "
                            ":metadata_hash, :asset_ref, :version_ref, "
                            ":content_hash, :manifest_hash, :asset_receipt_ref, "
                            ":asset_receipt_hash, :payload_json, :payload_hash, "
                            ":idempotency_key, :request_hash, :receipt_ref, "
                            ":receipt_hash, :accepted_at)"
                        ),
                        {
                            "revision": implementation.implementation_revision_ref,
                            "metadata_hash": implementation.content_hash_ref,
                            "asset_ref": artifact.asset_ref,
                            "version_ref": artifact.version_ref,
                            "content_hash": artifact.content_hash,
                            "manifest_hash": artifact.manifest_hash,
                            "asset_receipt_ref": artifact.receipt.receipt_ref,
                            "asset_receipt_hash": artifact.receipt.payload_hash,
                            "payload_json": canonical_json(payload),
                            "payload_hash": payload_hash,
                            "idempotency_key": idempotency_key,
                            "request_hash": request_hash,
                            "receipt_ref": receipt.receipt_ref,
                            "receipt_hash": receipt.payload_hash,
                            "accepted_at": now,
                        },
                    )
                except IntegrityError as error:
                    raise OwnerConflict(
                        "target_implementation_artifact_conflict"
                    ) from error
                connection.execute(
                    text(
                        "UPDATE research_memory_state SET revision = revision + 1, "
                        "target_implementation_artifact_count = "
                        "target_implementation_artifact_count + 1 WHERE singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "research_memory.target_implementation_artifact_accepted",
                    {
                        "implementation_revision_ref": (
                            implementation.implementation_revision_ref
                        ),
                        "asset_ref": artifact.asset_ref,
                        "receipt_ref": receipt.receipt_ref,
                    },
                )
        accepted = self.query_implementation_artifact(
            implementation.implementation_revision_ref
        )
        if accepted is None:
            raise OwnerConflict("target_implementation_artifact_missing")
        return accepted

    def accept_implementation_bundle(
        self,
        *,
        implementation_revision_ref: str,
        artifact: AcceptedAssetBinding,
        idempotency_key: str,
    ) -> AcceptedTargetImplementationBundle:
        """Accept global immutable bytes for an Implementation Revision.

        Target provenance/currentness is deliberately accepted by the
        separate usage seam below.  This permits one immutable revision to be
        reused by multiple Targets without rewriting its RM receipt.
        """

        if (
            not isinstance(implementation_revision_ref, str)
            or not implementation_revision_ref
            or len(implementation_revision_ref) > 256
        ):
            raise OwnerConflict("target_implementation_bundle_revision_invalid")
        self._verify_asset(artifact)
        payload = {
            "implementation_revision_ref": implementation_revision_ref,
            "bundle_content_hash_ref": artifact.content_hash,
            "artifact": artifact.as_dict(),
        }
        payload_hash = canonical_hash(payload)
        request_hash = canonical_hash(
            {"command": "accept_target_implementation_bundle", **payload}
        )
        now = time.time()
        with self._database.write() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rm_target_implementation_bundles WHERE "
                    "idempotency_key = :key OR implementation_revision_ref = "
                    ":revision_ref"
                ),
                {
                    "key": idempotency_key,
                    "revision_ref": implementation_revision_ref,
                },
            ).first()
            if row is not None:
                if row.request_hash != request_hash:
                    raise OwnerConflict("target_implementation_bundle_conflict")
            else:
                receipt = _receipt(
                    "research_memory",
                    RM_TARGET_IMPLEMENTATION_BUNDLE_RECEIPT_KIND,
                    new_ref("rm_target_implementation_bundle_receipt"),
                    artifact.content_hash,
                    {**payload, "payload_hash": payload_hash},
                )
                connection.execute(
                    text(
                        "INSERT INTO rm_target_implementation_bundles "
                        "(implementation_revision_ref, bundle_content_hash, "
                        "asset_ref, version_ref, "
                        "artifact_manifest_hash, asset_receipt_ref, "
                        "asset_receipt_hash, payload_json, "
                        "payload_hash, idempotency_key, request_hash, "
                        "receipt_ref, receipt_hash, accepted_at) VALUES "
                        "(:revision_ref, :bundle_hash, :asset_ref, :version_ref, "
                        ":manifest_hash, :asset_receipt_ref, "
                        ":asset_receipt_hash, :payload_json, :payload_hash, "
                        ":idempotency_key, :request_hash, :receipt_ref, "
                        ":receipt_hash, :accepted_at)"
                    ),
                    {
                        "revision_ref": implementation_revision_ref,
                        "bundle_hash": artifact.content_hash,
                        "asset_ref": artifact.asset_ref,
                        "version_ref": artifact.version_ref,
                        "manifest_hash": artifact.manifest_hash,
                        "asset_receipt_ref": artifact.receipt.receipt_ref,
                        "asset_receipt_hash": artifact.receipt.payload_hash,
                        "payload_json": canonical_json(payload),
                        "payload_hash": payload_hash,
                        "idempotency_key": idempotency_key,
                        "request_hash": request_hash,
                        "receipt_ref": receipt.receipt_ref,
                        "receipt_hash": receipt.payload_hash,
                        "accepted_at": now,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE research_memory_state SET revision = "
                        "revision + 1, target_implementation_bundle_count = "
                        "target_implementation_bundle_count + 1 WHERE "
                        "singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "research_memory.target_implementation_bundle_accepted",
                    {
                        "implementation_revision_ref": implementation_revision_ref,
                        "bundle_content_hash_ref": artifact.content_hash,
                        "receipt_ref": receipt.receipt_ref,
                    },
                )
        accepted = self.query_implementation_bundle(implementation_revision_ref)
        if accepted is None:
            raise OwnerConflict("target_implementation_bundle_missing")
        return accepted

    def accept_implementation_bundle_usage(
        self,
        *,
        target_ref: str,
        implementation_revision_ref: str,
        idempotency_key: str,
    ) -> AcceptedTargetImplementationBundleUsage:
        """Accept one Target's exact use of an already immutable revision."""

        bundle = self.query_implementation_bundle(implementation_revision_ref)
        if bundle is None:
            raise OwnerConflict("target_implementation_bundle_missing")
        verifier = self._implementation_bundle_revision_verifier
        if verifier is None:
            raise OwnerConflict("target_implementation_bundle_verifier_unavailable")
        origin_kind, authority_receipt = verifier.verify_implementation_bundle_revision(
            target_ref=target_ref,
            implementation_revision_ref=implementation_revision_ref,
        )
        if origin_kind not in {"reused", "greenfield", "recovery"}:
            raise OwnerConflict("target_implementation_bundle_revision_invalid")
        payload = {
            "target_ref": target_ref,
            "implementation_revision_ref": implementation_revision_ref,
            "origin_kind": origin_kind,
            "bundle_receipt": bundle.receipt.as_public_dict(),
            "revision_authority_receipt": authority_receipt.as_public_dict(),
        }
        payload_hash = canonical_hash(payload)
        request_hash = canonical_hash(
            {"command": "accept_target_implementation_bundle_usage", **payload}
        )
        now = time.time()
        with self._database.write() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rm_target_implementation_bundle_usages WHERE "
                    "idempotency_key = :key OR (target_ref = :target_ref AND "
                    "implementation_revision_ref = :revision_ref)"
                ),
                {
                    "key": idempotency_key,
                    "target_ref": target_ref,
                    "revision_ref": implementation_revision_ref,
                },
            ).first()
            if row is not None:
                if row.request_hash != request_hash:
                    raise OwnerConflict("target_implementation_bundle_usage_conflict")
                usage_ref = row.usage_ref
            else:
                usage_ref = new_ref("target_implementation_bundle_usage")
                receipt = _receipt(
                    "research_memory",
                    RM_TARGET_IMPLEMENTATION_BUNDLE_USAGE_RECEIPT_KIND,
                    new_ref("rm_target_implementation_bundle_usage_receipt"),
                    usage_ref,
                    {**payload, "usage_ref": usage_ref, "payload_hash": payload_hash},
                )
                connection.execute(
                    text(
                        "INSERT INTO rm_target_implementation_bundle_usages "
                        "(usage_ref, target_ref, implementation_revision_ref, "
                        "origin_kind, revision_authority_receipt_ref, "
                        "revision_authority_receipt_hash, payload_json, payload_hash, "
                        "idempotency_key, request_hash, receipt_ref, receipt_hash, "
                        "accepted_at) VALUES (:usage_ref, :target_ref, :revision_ref, "
                        ":origin_kind, :authority_ref, :authority_hash, :payload_json, "
                        ":payload_hash, :idempotency_key, :request_hash, :receipt_ref, "
                        ":receipt_hash, :accepted_at)"
                    ),
                    {
                        "usage_ref": usage_ref,
                        "target_ref": target_ref,
                        "revision_ref": implementation_revision_ref,
                        "origin_kind": origin_kind,
                        "authority_ref": authority_receipt.receipt_ref,
                        "authority_hash": authority_receipt.payload_hash,
                        "payload_json": canonical_json(payload),
                        "payload_hash": payload_hash,
                        "idempotency_key": idempotency_key,
                        "request_hash": request_hash,
                        "receipt_ref": receipt.receipt_ref,
                        "receipt_hash": receipt.payload_hash,
                        "accepted_at": now,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE research_memory_state SET revision = revision + 1, "
                        "target_implementation_bundle_usage_count = "
                        "target_implementation_bundle_usage_count + 1 WHERE "
                        "singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "research_memory.target_implementation_bundle_usage_accepted",
                    {
                        "usage_ref": usage_ref,
                        "target_ref": target_ref,
                        "implementation_revision_ref": implementation_revision_ref,
                        "receipt_ref": receipt.receipt_ref,
                    },
                )
        accepted = self.query_implementation_bundle_usage(
            target_ref=target_ref,
            implementation_revision_ref=implementation_revision_ref,
        )
        if accepted is None:
            raise OwnerConflict("target_implementation_bundle_usage_missing")
        return accepted

    def query_implementation_bundle(
        self,
        implementation_revision_ref: str,
    ) -> AcceptedTargetImplementationBundle | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rm_target_implementation_bundles WHERE "
                    "implementation_revision_ref = :revision_ref"
                ),
                {"revision_ref": implementation_revision_ref},
            ).first()
        if row is None:
            return None
        artifact = AcceptedAssetBinding(
            asset_ref=row.asset_ref,
            version_ref=row.version_ref,
            content_hash=row.bundle_content_hash,
            manifest_hash=row.artifact_manifest_hash,
            receipt=AcceptanceReceipt(
                issuer="research_memory",
                kind=_asset_receipt_kind(self._database, row.version_ref),
                receipt_ref=row.asset_receipt_ref,
                subject_ref=row.version_ref,
                payload_hash=row.asset_receipt_hash,
            ),
        )
        self._verify_asset(artifact)
        payload = {
            "implementation_revision_ref": implementation_revision_ref,
            "bundle_content_hash_ref": artifact.content_hash,
            "artifact": artifact.as_dict(),
        }
        payload_hash = canonical_hash(payload)
        request_hash = canonical_hash(
            {"command": "accept_target_implementation_bundle", **payload}
        )
        receipt = _receipt(
            "research_memory",
            RM_TARGET_IMPLEMENTATION_BUNDLE_RECEIPT_KIND,
            row.receipt_ref,
            artifact.content_hash,
            {**payload, "payload_hash": payload_hash},
        )
        if (
            row.payload_json != canonical_json(payload)
            or row.payload_hash != payload_hash
            or row.request_hash != request_hash
            or row.receipt_hash != receipt.payload_hash
        ):
            raise OwnerConflict("target_implementation_bundle_integrity_invalid")
        return AcceptedTargetImplementationBundle(
            implementation_revision_ref=implementation_revision_ref,
            bundle_content_hash_ref=artifact.content_hash,
            artifact=artifact,
            payload_hash=payload_hash,
            receipt=receipt,
            accepted_at=float(row.accepted_at),
        )

    def query_implementation_bundle_usage(
        self,
        *,
        target_ref: str,
        implementation_revision_ref: str,
    ) -> AcceptedTargetImplementationBundleUsage | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rm_target_implementation_bundle_usages WHERE "
                    "target_ref = :target_ref AND implementation_revision_ref = "
                    ":revision_ref"
                ),
                {"target_ref": target_ref, "revision_ref": implementation_revision_ref},
            ).first()
        if row is None:
            return None
        bundle = self.query_implementation_bundle(implementation_revision_ref)
        if bundle is None:
            raise OwnerConflict("target_implementation_bundle_usage_integrity_invalid")
        verifier = self._implementation_bundle_revision_verifier
        if verifier is None:
            raise OwnerConflict("target_implementation_bundle_verifier_unavailable")
        origin_kind, authority_receipt = verifier.verify_implementation_bundle_revision(
            target_ref=target_ref,
            implementation_revision_ref=implementation_revision_ref,
        )
        payload = {
            "target_ref": target_ref,
            "implementation_revision_ref": implementation_revision_ref,
            "origin_kind": origin_kind,
            "bundle_receipt": bundle.receipt.as_public_dict(),
            "revision_authority_receipt": authority_receipt.as_public_dict(),
        }
        payload_hash = canonical_hash(payload)
        request_hash = canonical_hash(
            {"command": "accept_target_implementation_bundle_usage", **payload}
        )
        receipt = _receipt(
            "research_memory",
            RM_TARGET_IMPLEMENTATION_BUNDLE_USAGE_RECEIPT_KIND,
            row.receipt_ref,
            row.usage_ref,
            {**payload, "usage_ref": row.usage_ref, "payload_hash": payload_hash},
        )
        if (
            row.origin_kind != origin_kind
            or row.revision_authority_receipt_ref != authority_receipt.receipt_ref
            or row.revision_authority_receipt_hash != authority_receipt.payload_hash
            or row.payload_json != canonical_json(payload)
            or row.payload_hash != payload_hash
            or row.request_hash != request_hash
            or row.receipt_hash != receipt.payload_hash
        ):
            raise OwnerConflict("target_implementation_bundle_usage_integrity_invalid")
        return AcceptedTargetImplementationBundleUsage(
            usage_ref=row.usage_ref,
            target_ref=target_ref,
            implementation_revision_ref=implementation_revision_ref,
            origin_kind=origin_kind,
            bundle=bundle,
            revision_authority_receipt=authority_receipt,
            payload_hash=payload_hash,
            receipt=receipt,
            accepted_at=float(row.accepted_at),
        )

    def materialize_implementation_bundle(
        self,
        implementation_revision_ref: str,
    ) -> tuple[AcceptedTargetImplementationBundle, object]:
        """Return RM's exact materialization; the execution port validates it."""

        accepted = self.query_implementation_bundle(
            implementation_revision_ref
        )
        if accepted is None:
            raise OwnerConflict("target_implementation_bundle_missing")
        try:
            materialized = self._verifier.materialize_asset(
                accepted.artifact.version_ref
            )
        except Exception as error:
            raise OwnerConflict(
                "target_implementation_bundle_unavailable"
            ) from error
        if (
            getattr(materialized, "memory_ref", None)
            != accepted.artifact.version_ref
            or not isinstance(getattr(materialized, "content", None), bytes)
        ):
            raise OwnerConflict("target_implementation_bundle_integrity_invalid")
        return accepted, materialized

    def _accept_owner_workspace_implementation(
        self,
        *,
        target_ref: str,
        implementation_revision_ref: str,
        source_directory: Path,
        workspace_ref: str,
        idempotency_key: str,
    ) -> tuple[
        AcceptedTargetImplementationBundle,
        AcceptedTargetImplementationBundleUsage,
    ]:
        """Freeze an Owner-resolved private tree; never accepts a caller path."""

        if (
            not source_directory.is_absolute()
            or not source_directory.is_dir()
            or source_directory.is_symlink()
        ):
            raise OwnerConflict("target_implementation_workspace_invalid")
        intake_key = "target-workspace-intake:" + canonical_hash(
            {
                "workspace_ref": workspace_ref,
                "target_ref": target_ref,
                "implementation_revision_ref": implementation_revision_ref,
            }
        )
        try:
            intake = self._verifier.submit_asset_intake(
                AssetIntakeRequest(
                    source_kind="directory",
                    custody_mode="managed",
                    display_name="target-implementation",
                    media_type="application/vnd.meta-research.implementation-tree",
                    source_locator=str(source_directory),
                    provenance={
                        "workspace_ref": workspace_ref,
                        "target_ref": target_ref,
                        "implementation_revision_ref": implementation_revision_ref,
                    },
                ),
                idempotency_key=intake_key,
            )
            asset = getattr(intake, "asset")
            artifact = asset.as_binding()
        except Exception as error:
            if isinstance(error, OwnerConflict):
                raise
            raise OwnerConflict("target_implementation_workspace_invalid") from error
        bundle = self.accept_implementation_bundle(
            implementation_revision_ref=implementation_revision_ref,
            artifact=artifact,
            idempotency_key="target-bundle:" + canonical_hash(
                {
                    "implementation_revision_ref": implementation_revision_ref,
                    "workspace_ref": workspace_ref,
                    "request_key": idempotency_key,
                }
            ),
        )
        usage = self.accept_implementation_bundle_usage(
            target_ref=target_ref,
            implementation_revision_ref=implementation_revision_ref,
            idempotency_key="target-bundle-usage:" + canonical_hash(
                {
                    "target_ref": target_ref,
                    "implementation_revision_ref": implementation_revision_ref,
                    "workspace_ref": workspace_ref,
                    "request_key": idempotency_key,
                }
            ),
        )
        return bundle, usage

    def query_implementation_artifact(
        self, implementation_revision_ref: str
    ) -> AcceptedTargetImplementationArtifact | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT a.*, c.* FROM rm_target_implementation_artifacts a "
                    "JOIN rm_implementation_revision_contents c ON "
                    "c.implementation_revision_ref = a.implementation_revision_ref "
                    "WHERE a.implementation_revision_ref = :revision"
                ),
                {"revision": implementation_revision_ref},
            ).first()
        if row is None:
            return None
        implementation = _implementation_from_joined_row(row)
        self._verify_implementation(implementation)
        artifact = AcceptedAssetBinding(
            asset_ref=row.asset_ref,
            version_ref=row.version_ref,
            content_hash=row.artifact_content_hash,
            manifest_hash=row.artifact_manifest_hash,
            receipt=AcceptanceReceipt(
                issuer="research_memory",
                kind=_asset_receipt_kind(self._database, row.version_ref),
                receipt_ref=row.asset_receipt_ref,
                subject_ref=row.version_ref,
                payload_hash=row.asset_receipt_hash,
            ),
        )
        self._verify_asset(artifact)
        payload = {
            "implementation_revision_ref": implementation_revision_ref,
            "metadata_content_hash_ref": row.metadata_content_hash_ref,
            "artifact": artifact.as_dict(),
        }
        bindings = {
            **payload,
            "payload_hash": canonical_hash(payload),
            "asset_receipt_ref": artifact.receipt.receipt_ref,
            "asset_receipt_hash": artifact.receipt.payload_hash,
        }
        receipt = _receipt(
            "research_memory",
            RM_IMPLEMENTATION_ARTIFACT_RECEIPT_KIND,
            row.receipt_ref,
            row.metadata_content_hash_ref,
            bindings,
        )
        if (
            row.metadata_content_hash_ref != implementation.content_hash_ref
            or row.payload_json != canonical_json(payload)
            or row.payload_hash != canonical_hash(payload)
            or row.receipt_hash != receipt.payload_hash
        ):
            raise OwnerConflict("target_implementation_artifact_integrity_invalid")
        return AcceptedTargetImplementationArtifact(
            implementation_revision_ref=implementation_revision_ref,
            metadata_content_hash_ref=row.metadata_content_hash_ref,
            artifact=artifact,
            payload_hash=row.payload_hash,
            receipt=receipt,
            accepted_at=float(row.accepted_at),
        )

    def materialize_implementation_artifact(
        self, implementation_revision_ref: str
    ) -> tuple[AcceptedTargetImplementationArtifact, bytes]:
        accepted = self.query_implementation_artifact(
            implementation_revision_ref
        )
        if accepted is None:
            raise OwnerConflict("target_implementation_artifact_missing")
        try:
            materialized = self._verifier.materialize_asset(
                accepted.artifact.version_ref
            )
            content = getattr(materialized, "content")
            memory_ref = getattr(materialized, "memory_ref")
        except Exception as error:
            raise OwnerConflict(
                "target_implementation_artifact_unavailable"
            ) from error
        if (
            not isinstance(content, bytes)
            or memory_ref != accepted.artifact.version_ref
            or hashlib.sha256(content).hexdigest()
            != accepted.artifact.content_hash
        ):
            raise OwnerConflict("target_implementation_artifact_integrity_invalid")
        return accepted, content

    def accept_input_asset(
        self,
        *,
        target_ref: str,
        asset: AcceptedAssetBinding,
        idempotency_key: str,
    ) -> AcceptanceReceipt:
        self._verify_asset(asset)
        bindings = {"target_ref": target_ref, "asset": asset.as_dict()}
        request_hash = canonical_hash(bindings)
        now = time.time()
        with self._database.write() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rm_target_input_asset_proofs WHERE "
                    "idempotency_key = :key OR (target_ref = :target_ref AND "
                    "asset_ref = :asset_ref)"
                ),
                {
                    "key": idempotency_key,
                    "target_ref": target_ref,
                    "asset_ref": asset.asset_ref,
                },
            ).first()
            if row is not None:
                if row.request_hash != request_hash:
                    raise OwnerConflict("target_input_asset_proof_conflict")
                return self._rm_asset_proof_receipt(row, asset)
            proof_ref = new_ref("rm_target_input_asset_proof")
            receipt_ref = new_ref("rm_target_input_asset_receipt")
            receipt = _receipt(
                "research_memory",
                RM_TARGET_INPUT_ASSET_RECEIPT_KIND,
                receipt_ref,
                asset.asset_ref,
                {**bindings, "proof_ref": proof_ref},
            )
            connection.execute(
                text(
                    "INSERT INTO rm_target_input_asset_proofs (proof_ref, "
                    "target_ref, asset_ref, version_ref, content_hash, "
                    "manifest_hash, source_receipt_ref, source_receipt_hash, "
                    "idempotency_key, request_hash, receipt_ref, receipt_hash, "
                    "accepted_at) VALUES (:proof_ref, :target_ref, :asset_ref, "
                    ":version_ref, :content_hash, :manifest_hash, "
                    ":source_receipt_ref, :source_receipt_hash, :idempotency_key, "
                    ":request_hash, :receipt_ref, :receipt_hash, :accepted_at)"
                ),
                {
                    "proof_ref": proof_ref,
                    "target_ref": target_ref,
                    "asset_ref": asset.asset_ref,
                    "version_ref": asset.version_ref,
                    "content_hash": asset.content_hash,
                    "manifest_hash": asset.manifest_hash,
                    "source_receipt_ref": asset.receipt.receipt_ref,
                    "source_receipt_hash": asset.receipt.payload_hash,
                    "idempotency_key": idempotency_key,
                    "request_hash": request_hash,
                    "receipt_ref": receipt.receipt_ref,
                    "receipt_hash": receipt.payload_hash,
                    "accepted_at": now,
                },
            )
            connection.execute(
                text(
                    "UPDATE research_memory_state SET revision = revision + 1, "
                    "target_input_asset_proof_count = "
                    "target_input_asset_proof_count + 1 WHERE singleton = 'owner'"
                )
            )
            return receipt

    def accept_generic_result_manifest(
        self,
        *,
        generic_binding_ref: str,
        idempotency_key: str,
    ) -> AcceptedTargetGenericResultManifest:
        """Accept only bytes re-read from a signed terminal generic operation."""

        verifier = self._generic_result_verifier
        if (
            verifier is None
            or not isinstance(generic_binding_ref, str)
            or not generic_binding_ref
            or not isinstance(idempotency_key, str)
            or not idempotency_key
            or len(idempotency_key) > 128
        ):
            raise OwnerConflict("target_generic_result_manifest_invalid")
        facts = verifier.query_generic_execution_terminal(generic_binding_ref)
        if facts is None:
            raise OwnerConflict("target_generic_result_execution_missing")
        binding, request, terminal, _input_binding = facts
        if (
            binding.terminal_status != "succeeded"
            or terminal.exit_receipt.status != "succeeded"
            or not terminal.exit_receipt.process_tree_drained
        ):
            raise OwnerConflict("target_generic_result_execution_incomplete")
        sources = _generic_terminal_result_sources(request, terminal)
        if not sources:
            raise OwnerConflict("target_generic_result_manifest_invalid")
        entries: list[TargetGenericResultAsset] = []
        for ordinal, (role, relative_path, content) in enumerate(sources):
            source_hash = hashlib.sha256(content).hexdigest()
            intake_key = "target-generic-result-asset:" + canonical_hash(
                {
                    "binding_ref": binding.binding_ref,
                    "operation_handle": binding.operation_handle,
                    "role": role,
                    "ordinal": ordinal,
                    "relative_path": relative_path,
                    "content_sha256": source_hash,
                }
            )
            try:
                intake = self._verifier.submit_asset_intake(
                    AssetIntakeRequest(
                        source_kind="file",
                        custody_mode="managed",
                        display_name=(
                            "target-result-" + str(ordinal) + "-" + role
                        ),
                        media_type=(
                            "application/json"
                            if role == "result_content"
                            else "application/octet-stream"
                        ),
                        content=content,
                        provenance={
                            "schema_ref": (
                                "meta-research/target-generic-result-asset/v1"
                            ),
                            "target_ref": binding.target_ref,
                            "target_run_ref": binding.target_run_ref,
                            "target_attempt_ref": binding.target_attempt_ref,
                            "target_fence_ref": binding.target_fence_ref,
                            "generic_binding_ref": binding.binding_ref,
                            "operation_handle": binding.operation_handle,
                            "exit_receipt_ref": binding.exit_receipt_ref,
                            "role": role,
                            "ordinal": ordinal,
                            "relative_path": relative_path,
                            "content_sha256": source_hash,
                        },
                    ),
                    idempotency_key=intake_key,
                )
                asset = getattr(intake, "asset", None)
                if asset is None or getattr(intake, "status", None) != "accepted":
                    raise OwnerConflict("target_generic_result_asset_unavailable")
                accepted_binding = asset.as_binding()
                self._verify_asset(accepted_binding)
            except OwnerConflict:
                raise
            except Exception as error:
                raise OwnerConflict("target_generic_result_asset_unavailable") from error
            if accepted_binding.content_hash != source_hash:
                raise OwnerConflict("target_generic_result_asset_integrity_invalid")
            entries.append(
                TargetGenericResultAsset(
                    role=role,
                    ordinal=ordinal,
                    relative_path=relative_path,
                    binding=accepted_binding,
                )
            )
        frozen_entries = tuple(entries)
        roles_value = projection_plain_value(frozen_entries)
        roles_hash = canonical_hash(roles_value)
        payload = {
            "target_ref": binding.target_ref,
            "target_run_ref": binding.target_run_ref,
            "target_attempt_ref": binding.target_attempt_ref,
            "target_fence_ref": binding.target_fence_ref,
            "generic_binding_ref": binding.binding_ref,
            "operation_handle": binding.operation_handle,
            "output_manifest_sha256": (
                terminal.exit_receipt.output_manifest_sha256
            ),
            "entries": roles_value,
        }
        payload_hash = canonical_hash(payload)
        request_hash = canonical_hash(
            {"command": "accept_target_generic_result_manifest", "payload": payload}
        )
        now = time.time()
        with self._database.fenced_write() as connection:
            # Asset intake above is deliberately idempotent and non-authority.
            # Recheck the exact AR frontier before the first manifest/counter
            # write so a concurrent recovery can leave at most orphaned RM
            # assets, never an old-Attempt result manifest.
            from meta_research.owners.agent_runtime import (
                verify_current_target_run_frontier_in_transaction,
            )

            verify_current_target_run_frontier_in_transaction(
                connection,
                request.handle,
            )
            row = connection.execute(
                text(
                    "SELECT * FROM rm_target_generic_result_manifests WHERE "
                    "idempotency_key = :key OR generic_binding_ref = :binding_ref"
                ),
                {"key": idempotency_key, "binding_ref": binding.binding_ref},
            ).first()
            if row is not None:
                if row.request_hash != request_hash:
                    raise OwnerConflict("target_generic_result_manifest_conflict")
                manifest_ref = row.manifest_ref
            else:
                manifest_ref = new_ref("target_generic_result_manifest")
                receipt = _receipt(
                    "research_memory",
                    RM_TARGET_GENERIC_RESULT_MANIFEST_RECEIPT_KIND,
                    new_ref("rm_target_generic_result_manifest_receipt"),
                    manifest_ref,
                    {
                        "manifest_ref": manifest_ref,
                        "roles_hash": roles_hash,
                        "payload_hash": payload_hash,
                        **payload,
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO rm_target_generic_result_manifests "
                        "(manifest_ref, target_ref, target_run_ref, "
                        "target_attempt_ref, target_fence_ref, generic_binding_ref, "
                        "operation_handle, roles_json, roles_hash, payload_json, "
                        "payload_hash, idempotency_key, request_hash, receipt_ref, "
                        "receipt_hash, accepted_at) VALUES (:manifest_ref, "
                        ":target_ref, :target_run_ref, :target_attempt_ref, "
                        ":target_fence_ref, :generic_binding_ref, :operation_handle, "
                        ":roles_json, :roles_hash, :payload_json, :payload_hash, "
                        ":idempotency_key, :request_hash, :receipt_ref, "
                        ":receipt_hash, :accepted_at)"
                    ),
                    {
                        "manifest_ref": manifest_ref,
                        "target_ref": binding.target_ref,
                        "target_run_ref": binding.target_run_ref,
                        "target_attempt_ref": binding.target_attempt_ref,
                        "target_fence_ref": binding.target_fence_ref,
                        "generic_binding_ref": binding.binding_ref,
                        "operation_handle": binding.operation_handle,
                        "roles_json": canonical_json(roles_value),
                        "roles_hash": roles_hash,
                        "payload_json": canonical_json(payload),
                        "payload_hash": payload_hash,
                        "idempotency_key": idempotency_key,
                        "request_hash": request_hash,
                        "receipt_ref": receipt.receipt_ref,
                        "receipt_hash": receipt.payload_hash,
                        "accepted_at": now,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE research_memory_state SET revision = revision + 1, "
                        "target_generic_result_manifest_count = "
                        "target_generic_result_manifest_count + 1 WHERE singleton = "
                        "'owner'"
                    )
                )
        accepted = self.query_generic_result_manifest(manifest_ref)
        if accepted is None:
            raise OwnerConflict("target_generic_result_manifest_missing")
        return accepted

    def query_generic_result_manifest(
        self, manifest_ref: str
    ) -> AcceptedTargetGenericResultManifest | None:
        verifier = self._generic_result_verifier
        if verifier is None:
            raise OwnerConflict("target_generic_result_verifier_unavailable")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rm_target_generic_result_manifests WHERE "
                    "manifest_ref = :manifest_ref"
                ),
                {"manifest_ref": manifest_ref},
            ).first()
        if row is None:
            return None
        facts = verifier.query_generic_execution_terminal(row.generic_binding_ref)
        if facts is None:
            raise OwnerConflict("target_generic_result_manifest_integrity_invalid")
        binding, request, terminal, _input_binding = facts
        try:
            roles_value = json.loads(row.roles_json)
            payload = json.loads(row.payload_json)
            entries = _decode_bundle_value(
                roles_value, tuple[TargetGenericResultAsset, ...]
            )
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise OwnerConflict("target_generic_result_manifest_integrity_invalid") from error
        if type(entries) is not tuple or not entries:
            raise OwnerConflict("target_generic_result_manifest_integrity_invalid")
        expected_sources = _generic_terminal_result_sources(request, terminal)
        if len(entries) != len(expected_sources):
            raise OwnerConflict("target_generic_result_manifest_integrity_invalid")
        for ordinal, (entry, source) in enumerate(
            zip(entries, expected_sources, strict=True)
        ):
            role, relative_path, content = source
            try:
                self._verify_asset(entry.binding)
                materialized = self._verifier.materialize_asset(
                    entry.binding.version_ref
                )
            except Exception as error:
                raise OwnerConflict(
                    "target_generic_result_manifest_integrity_invalid"
                ) from error
            if (
                entry.role != role
                or entry.ordinal != ordinal
                or entry.relative_path != relative_path
                or getattr(materialized, "memory_ref", None)
                != entry.binding.version_ref
                or getattr(materialized, "content", None) != content
                or hashlib.sha256(content).hexdigest()
                != entry.binding.content_hash
            ):
                raise OwnerConflict("target_generic_result_manifest_integrity_invalid")
        expected_payload = {
            "target_ref": binding.target_ref,
            "target_run_ref": binding.target_run_ref,
            "target_attempt_ref": binding.target_attempt_ref,
            "target_fence_ref": binding.target_fence_ref,
            "generic_binding_ref": binding.binding_ref,
            "operation_handle": binding.operation_handle,
            "output_manifest_sha256": terminal.exit_receipt.output_manifest_sha256,
            "entries": roles_value,
        }
        roles_hash = canonical_hash(roles_value)
        payload_hash = canonical_hash(expected_payload)
        request_hash = canonical_hash(
            {
                "command": "accept_target_generic_result_manifest",
                "payload": expected_payload,
            }
        )
        receipt = _receipt(
            "research_memory",
            RM_TARGET_GENERIC_RESULT_MANIFEST_RECEIPT_KIND,
            row.receipt_ref,
            manifest_ref,
            {
                "manifest_ref": manifest_ref,
                "roles_hash": roles_hash,
                "payload_hash": payload_hash,
                **expected_payload,
            },
        )
        if (
            row.target_ref != binding.target_ref
            or row.target_run_ref != binding.target_run_ref
            or row.target_attempt_ref != binding.target_attempt_ref
            or row.target_fence_ref != binding.target_fence_ref
            or row.operation_handle != binding.operation_handle
            or row.roles_hash != roles_hash
            or payload != expected_payload
            or row.payload_hash != payload_hash
            or row.request_hash != request_hash
            or row.receipt_hash != receipt.payload_hash
        ):
            raise OwnerConflict("target_generic_result_manifest_integrity_invalid")
        return AcceptedTargetGenericResultManifest(
            manifest_ref=manifest_ref,
            target_ref=binding.target_ref,
            target_run_ref=binding.target_run_ref,
            target_attempt_ref=binding.target_attempt_ref,
            target_fence_ref=binding.target_fence_ref,
            generic_binding_ref=binding.binding_ref,
            operation_handle=binding.operation_handle,
            output_manifest_sha256=terminal.exit_receipt.output_manifest_sha256,
            entries=entries,
            payload_hash=payload_hash,
            receipt=receipt,
            accepted_at=float(row.accepted_at),
        )

    def query_generic_result_manifest_for_binding(
        self,
        generic_binding_ref: str,
    ) -> AcceptedTargetGenericResultManifest | None:
        """Reconcile the unique RM manifest for one generic operation."""

        if type(generic_binding_ref) is not str or not generic_binding_ref:
            raise OwnerConflict("target_generic_result_manifest_invalid")
        with self._database.read() as connection:
            rows = connection.execute(
                text(
                    "SELECT manifest_ref FROM "
                    "rm_target_generic_result_manifests WHERE "
                    "generic_binding_ref = :generic_binding_ref"
                ),
                {"generic_binding_ref": generic_binding_ref},
            ).all()
        if not rows:
            return None
        if len(rows) != 1:
            raise OwnerConflict(
                "target_generic_result_manifest_integrity_invalid"
            )
        return self.query_generic_result_manifest(rows[0].manifest_ref)

    def materialize_generic_result_asset(
        self,
        *,
        manifest_ref: str,
        version_ref: str,
    ) -> bytes:
        manifest = self.query_generic_result_manifest(manifest_ref)
        if manifest is None:
            raise OwnerConflict("target_generic_result_manifest_missing")
        matches = [entry for entry in manifest.entries if entry.binding.version_ref == version_ref]
        if len(matches) != 1:
            raise OwnerConflict("target_generic_result_asset_missing")
        materialized = self._verifier.materialize_asset(version_ref)
        content = getattr(materialized, "content", None)
        if (
            not isinstance(content, bytes)
            or hashlib.sha256(content).hexdigest() != matches[0].binding.content_hash
        ):
            raise OwnerConflict("target_generic_result_asset_integrity_invalid")
        return content

    def accept_result_manifest(
        self,
        *,
        target_ref: str,
        target_run_ref: str,
        variant_run_ref: str,
        evaluation_attempt_ref: str,
        metric_result_ref: str,
        experiment_run_ref: str,
        experiment_attempt_ref: str,
        experiment_fence_ref: str,
        roles: tuple[AcceptedExperimentAssetRole, ...],
        idempotency_key: str,
    ) -> AcceptedTargetResultManifest:
        raise OwnerConflict("legacy_target_result_manifest_write_forbidden")

    def query_result_manifest(
        self, manifest_ref: str
    ) -> AcceptedTargetResultManifest | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rm_target_result_manifests WHERE "
                    "manifest_ref = :manifest_ref"
                ),
                {"manifest_ref": manifest_ref},
            ).first()
        if row is None:
            return None
        try:
            role_values = json.loads(row.roles_json)
            payload = json.loads(row.payload_json)
        except (TypeError, ValueError) as error:
            raise OwnerConflict("target_result_manifest_integrity_invalid") from error
        if not isinstance(role_values, list) or not isinstance(payload, dict):
            raise OwnerConflict("target_result_manifest_integrity_invalid")
        entries: list[TargetResultManifestEntry] = []
        for value in role_values:
            if not isinstance(value, dict):
                raise OwnerConflict("target_result_manifest_integrity_invalid")
            asset_receipt_value = value.get("asset_receipt")
            role_receipt_value = value.get("receipt")
            if not isinstance(asset_receipt_value, dict) or not isinstance(
                role_receipt_value, dict
            ):
                raise OwnerConflict("target_result_manifest_integrity_invalid")
            asset = AcceptedAssetBinding(
                asset_ref=str(value["asset_ref"]),
                version_ref=str(value["version_ref"]),
                content_hash=str(value["content_hash"]),
                manifest_hash=str(value["manifest_hash"]),
                receipt=_receipt_from_public(asset_receipt_value),
            )
            self._verify_asset(asset)
            entries.append(
                TargetResultManifestEntry(
                    role=str(value["role"]),
                    ordinal=int(value["ordinal"]),
                    role_ref=str(value["role_ref"]),
                    subject_kind=str(value["subject_kind"]),
                    subject_ref=str(value["subject_ref"]),
                    asset_ref=asset.asset_ref,
                    version_ref=asset.version_ref,
                    content_hash=asset.content_hash,
                    manifest_hash=asset.manifest_hash,
                    asset_receipt_ref=asset.receipt.receipt_ref,
                    role_receipt_ref=str(role_receipt_value["receipt_ref"]),
                )
            )
        expected_payload = {
            "target_ref": row.target_ref,
            "target_run_ref": row.target_run_ref,
            "variant_run_ref": row.variant_run_ref,
            "evaluation_attempt_ref": row.evaluation_attempt_ref,
            "metric_result_ref": row.metric_result_ref,
            "experiment_run_ref": row.experiment_run_ref,
            "experiment_attempt_ref": row.experiment_attempt_ref,
            "experiment_fence_ref": row.experiment_fence_ref,
            "roles": role_values,
        }
        receipt = _receipt(
            "research_memory",
            RM_TARGET_RESULT_MANIFEST_RECEIPT_KIND,
            row.receipt_ref,
            manifest_ref,
            {**expected_payload, "manifest_ref": manifest_ref, "payload_hash": row.payload_hash},
        )
        if (
            payload != expected_payload
            or row.roles_hash != canonical_hash(role_values)
            or row.payload_hash != canonical_hash(payload)
            or row.request_hash
            != canonical_hash({"command": "accept", "payload": payload})
            or row.receipt_hash != receipt.payload_hash
        ):
            raise OwnerConflict("target_result_manifest_integrity_invalid")
        return AcceptedTargetResultManifest(
            manifest_ref=manifest_ref,
            target_ref=row.target_ref,
            target_run_ref=row.target_run_ref,
            variant_run_ref=row.variant_run_ref,
            evaluation_attempt_ref=row.evaluation_attempt_ref,
            metric_result_ref=row.metric_result_ref,
            experiment_run_ref=row.experiment_run_ref,
            experiment_attempt_ref=row.experiment_attempt_ref,
            experiment_fence_ref=row.experiment_fence_ref,
            entries=tuple(entries),
            payload_hash=row.payload_hash,
            receipt=receipt,
            accepted_at=float(row.accepted_at),
        )

    def materialize_result_content(
        self, manifest_ref: str
    ) -> tuple[AcceptedTargetResultManifest, TargetResultManifestEntry, dict[str, object]]:
        """Read the exact immutable provider result owned by one RM manifest."""

        manifest = self.query_result_manifest(manifest_ref)
        entries = (
            ()
            if manifest is None
            else tuple(
                entry
                for entry in manifest.entries
                if entry.role == "result_content"
            )
        )
        if manifest is None or len(entries) != 1:
            raise OwnerConflict("target_result_content_manifest_invalid")
        entry = entries[0]
        try:
            materialized = self._verifier.materialize_asset(entry.version_ref)
            content = getattr(materialized, "content")
        except Exception as error:
            raise OwnerConflict("target_result_content_unavailable") from error
        if not isinstance(content, bytes):
            raise OwnerConflict("target_result_content_unavailable")
        try:
            value = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise OwnerConflict("target_result_content_invalid") from error
        if (
            not isinstance(value, dict)
            or content != canonical_json(value).encode("utf-8")
            or hashlib.sha256(content).hexdigest() != entry.content_hash
            or canonical_hash(value) != entry.content_hash
        ):
            raise OwnerConflict("target_result_content_invalid")
        return manifest, entry, value

    def query_input_asset(
        self, *, target_ref: str, asset_ref: str
    ) -> tuple[AcceptedAssetBinding, AcceptanceReceipt] | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT p.*, v.acceptance_kind FROM "
                    "rm_target_input_asset_proofs p JOIN rm_asset_versions v ON "
                    "v.version_ref = p.version_ref WHERE p.target_ref = "
                    ":target_ref AND p.asset_ref = :asset_ref"
                ),
                {"target_ref": target_ref, "asset_ref": asset_ref},
            ).first()
        if row is None:
            return None
        asset = AcceptedAssetBinding(
            asset_ref=row.asset_ref,
            version_ref=row.version_ref,
            content_hash=row.content_hash,
            manifest_hash=row.manifest_hash,
            receipt=AcceptanceReceipt(
                issuer="research_memory",
                kind=row.acceptance_kind,
                receipt_ref=row.source_receipt_ref,
                subject_ref=row.version_ref,
                payload_hash=row.source_receipt_hash,
            ),
        )
        self._verify_asset(asset)
        return asset, self._rm_asset_proof_receipt(row, asset)

    def materialize_input_asset(
        self,
        *,
        target_ref: str,
        asset_ref: str,
    ) -> tuple[AcceptedAssetBinding, AcceptanceReceipt, str, bytes]:
        """Return one issuer-reverified direct Target input.

        The caller supplies only domain identities.  RM resolves the accepted
        version and re-materializes its exact bytes; no host path or caller
        content crosses this seam.
        """

        accepted = self.query_input_asset(
            target_ref=target_ref,
            asset_ref=asset_ref,
        )
        if accepted is None:
            raise OwnerConflict("target_input_asset_proof_missing")
        asset, proof_receipt = accepted
        try:
            materialized = self._verifier.materialize_asset(asset.version_ref)
        except Exception as error:
            raise OwnerConflict("target_input_asset_unavailable") from error
        content = getattr(materialized, "content", None)
        file_name = getattr(materialized, "file_name", None)
        if (
            getattr(materialized, "memory_ref", None) != asset.version_ref
            or type(content) is not bytes
            or not isinstance(file_name, str)
            or not file_name
        ):
            raise OwnerConflict("target_input_asset_integrity_invalid")
        return asset, proof_receipt, file_name, content

    def _rm_asset_proof_receipt(
        self, row: object, asset: AcceptedAssetBinding
    ) -> AcceptanceReceipt:
        receipt = _receipt(
            "research_memory",
            RM_TARGET_INPUT_ASSET_RECEIPT_KIND,
            row.receipt_ref,
            asset.asset_ref,
            {
                "target_ref": row.target_ref,
                "asset": asset.as_dict(),
                "proof_ref": row.proof_ref,
            },
        )
        if row.receipt_hash != receipt.payload_hash:
            raise OwnerConflict("target_input_asset_proof_integrity_invalid")
        return receipt

    def _verify_implementation(
        self, implementation: AcceptedImplementationRevisionContent
    ) -> None:
        self._verifier.verify_implementation_content(
            source_ref=implementation.source_ref,
            exact_version_ref=implementation.exact_version_ref,
            implementation_revision_ref=implementation.implementation_revision_ref,
            license_ref=implementation.license_ref,
            source_content_hash_ref=implementation.source_content_hash_ref,
            patch_ref=implementation.patch_ref,
            content_hash_ref=implementation.content_hash_ref,
            receipt_ref=implementation.content_acceptance_receipt.receipt_ref,
            receipt_subject_ref=(
                implementation.content_acceptance_receipt.subject_ref
            ),
        )

    def _verify_asset(self, asset: AcceptedAssetBinding) -> None:
        self._verifier.verify_asset_binding(
            asset_ref=asset.asset_ref,
            version_ref=asset.version_ref,
            content_hash=asset.content_hash,
            manifest_hash=asset.manifest_hash,
            receipt=asset.receipt,
        )


class SQLiteTargetRunGraphAuthority:
    """Research Graph's accepted inputs and protected execution bridge."""

    def __init__(
        self,
        database: Database,
        feed: DurableFeed,
        verifier: ResearchGraphTargetVerifier,
        memory: SQLiteTargetRunMemoryAuthority,
        domain_reader: ResearchGraphTargetReader,
        execution_verifier: AgentRuntimeExperimentVerifier,
    ) -> None:
        self._database = database
        self._feed = feed
        self._verifier = verifier
        self._memory = memory
        self._domain_reader = domain_reader
        self._execution_verifier = execution_verifier
        self._generic_admission_verifier: (
            SQLiteTargetExecutionAdmissionVerifier | None
        ) = None
        self._generic_execution_port: object | None = None

    def bind_generic_execution_authority(
        self,
        *,
        admission_verifier: SQLiteTargetExecutionAdmissionVerifier,
        execution_port: object,
    ) -> None:
        del admission_verifier, execution_port
        raise OwnerConflict("legacy_target_execution_port_retired")

    def accept_formal_plan_projection(
        self,
        *,
        graph_ref: str,
        idempotency_key: str,
    ) -> AcceptedTargetFormalPlanProjection:
        """Accept the fixed FormalPlan projection without relabelling 0021.

        The source receipt continues to own the complete PlanDocument hash.
        This RG receipt instead owns the prototype's canonical digest of the
        FormalPlanRef plus every normalized ExperimentBrief.
        """

        if (
            not isinstance(graph_ref, str)
            or not graph_ref
            or not isinstance(idempotency_key, str)
            or not idempotency_key
            or len(idempotency_key) > 128
        ):
            raise OwnerConflict("target_formal_plan_projection_invalid")
        graph, source, completion, briefs = self._current_formal_plan_facts(
            graph_ref
        )
        completion_value = normalized_completion_contract_to_dict(completion)
        completion_hash = completion_contract_hash(completion)
        briefs_value = projection_plain_value(briefs)
        briefs_hash = canonical_hash(briefs_value)
        projection_digest = canonical_formal_plan_projection_digest(
            formal_plan_ref=graph.formal_plan_ref,
            briefs=briefs,
        )
        bindings = {
            "graph_ref": graph_ref,
            "formal_plan_ref": graph.formal_plan_ref,
            "plan_document_hash": source.plan_document_hash,
            "source_acceptance_receipt_ref": source.receipt.receipt_ref,
            "source_acceptance_receipt_hash": source.receipt.payload_hash,
            "completion_contract": completion_value,
            "completion_contract_hash": completion_hash,
            "briefs": briefs_value,
            "briefs_hash": briefs_hash,
            "projection_digest": projection_digest,
        }
        request_hash = canonical_hash(
            {"command": "accept_target_formal_plan_projection", **bindings}
        )
        with self._database.read() as connection:
            replay = connection.execute(
                text(
                    "SELECT * FROM rg_target_formal_plan_projections WHERE "
                    "idempotency_key = :idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
            existing = connection.execute(
                text(
                    "SELECT * FROM rg_target_formal_plan_projections WHERE "
                    "graph_ref = :graph_ref OR formal_plan_ref = :formal_plan_ref"
                ),
                {
                    "graph_ref": graph_ref,
                    "formal_plan_ref": graph.formal_plan_ref,
                },
            ).first()
        selected = replay or existing
        if selected is not None:
            if selected.request_hash != request_hash:
                raise OwnerConflict("target_formal_plan_projection_conflict")
            accepted = self.query_formal_plan_projection(graph_ref=graph_ref)
            if accepted is None or accepted.receipt.receipt_ref != selected.receipt_ref:
                raise OwnerConflict("target_formal_plan_projection_invalid")
            return accepted

        receipt = _receipt(
            "research_graph",
            RG_TARGET_FORMAL_PLAN_PROJECTION_RECEIPT_KIND,
            new_ref("rg_target_formal_plan_projection_receipt"),
            projection_digest,
            bindings,
        )
        now = time.time()
        try:
            with self._database.write() as connection:
                connection.execute(
                    text(
                        "INSERT INTO rg_target_formal_plan_projections "
                        "(graph_ref, formal_plan_ref, plan_document_hash, "
                        "source_acceptance_receipt_ref, "
                        "source_acceptance_receipt_hash, "
                        "completion_contract_json, completion_contract_hash, "
                        "briefs_json, briefs_hash, content_hash, "
                        "idempotency_key, request_hash, receipt_ref, "
                        "receipt_hash, accepted_at) VALUES (:graph_ref, "
                        ":formal_plan_ref, :plan_document_hash, "
                        ":source_acceptance_receipt_ref, "
                        ":source_acceptance_receipt_hash, "
                        ":completion_contract_json, :completion_contract_hash, "
                        ":briefs_json, :briefs_hash, :content_hash, "
                        ":idempotency_key, :request_hash, :receipt_ref, "
                        ":receipt_hash, :accepted_at)"
                    ),
                    {
                        **bindings,
                        "completion_contract_json": canonical_json(
                            projection_plain_value(completion)
                        ),
                        "briefs_json": canonical_json(briefs_value),
                        "content_hash": projection_digest,
                        "idempotency_key": idempotency_key,
                        "request_hash": request_hash,
                        "receipt_ref": receipt.receipt_ref,
                        "receipt_hash": receipt.payload_hash,
                        "accepted_at": now,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE research_graph_state SET revision = revision + 1, "
                        "target_formal_plan_projection_count = "
                        "target_formal_plan_projection_count + 1 WHERE "
                        "singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "research_graph.target_formal_plan_projection_accepted",
                    {
                        "graph_ref": graph_ref,
                        "formal_plan_ref": graph.formal_plan_ref,
                        "plan_document_hash": source.plan_document_hash,
                        "projection_digest": projection_digest,
                        "receipt_ref": receipt.receipt_ref,
                    },
                )
        except IntegrityError as error:
            raise OwnerConflict("target_formal_plan_projection_conflict") from error
        accepted = self.query_formal_plan_projection(graph_ref=graph_ref)
        if accepted is None:
            raise OwnerConflict("target_formal_plan_projection_missing_after_commit")
        return accepted

    def query_formal_plan_projection(
        self, *, graph_ref: str
    ) -> AcceptedTargetFormalPlanProjection | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rg_target_formal_plan_projections WHERE "
                    "graph_ref = :graph_ref"
                ),
                {"graph_ref": graph_ref},
            ).first()
        if row is None:
            return None
        graph, source, current_completion, current_briefs = (
            self._current_formal_plan_facts(graph_ref)
        )
        try:
            completion_value = json.loads(row.completion_contract_json)
            completion = _decode_bundle_value(
                completion_value, NormalizedCompletionContract
            )
            briefs_value = json.loads(row.briefs_json)
            briefs = _decode_bundle_value(
                briefs_value, tuple[ExperimentBrief, ...]
            )
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise OwnerConflict("target_formal_plan_projection_integrity_invalid") from error
        if (
            type(completion) is not NormalizedCompletionContract
            or type(briefs) is not tuple
            or canonical_json(projection_plain_value(completion))
            != row.completion_contract_json
            or canonical_json(projection_plain_value(briefs)) != row.briefs_json
            or completion != current_completion
            or briefs != current_briefs
        ):
            raise OwnerConflict("target_formal_plan_projection_integrity_invalid")
        completion_hash = completion_contract_hash(completion)
        completion_plain = normalized_completion_contract_to_dict(completion)
        briefs_hash = canonical_hash(projection_plain_value(briefs))
        projection_digest = canonical_formal_plan_projection_digest(
            formal_plan_ref=graph.formal_plan_ref,
            briefs=briefs,
        )
        bindings = {
            "graph_ref": graph_ref,
            "formal_plan_ref": graph.formal_plan_ref,
            "plan_document_hash": source.plan_document_hash,
            "source_acceptance_receipt_ref": source.receipt.receipt_ref,
            "source_acceptance_receipt_hash": source.receipt.payload_hash,
            "completion_contract": completion_plain,
            "completion_contract_hash": completion_hash,
            "briefs": projection_plain_value(briefs),
            "briefs_hash": briefs_hash,
            "projection_digest": projection_digest,
        }
        request_hash = canonical_hash(
            {"command": "accept_target_formal_plan_projection", **bindings}
        )
        receipt = _receipt(
            "research_graph",
            RG_TARGET_FORMAL_PLAN_PROJECTION_RECEIPT_KIND,
            row.receipt_ref,
            projection_digest,
            bindings,
        )
        if (
            row.formal_plan_ref != graph.formal_plan_ref
            or row.plan_document_hash != source.plan_document_hash
            or row.source_acceptance_receipt_ref != source.receipt.receipt_ref
            or row.source_acceptance_receipt_hash != source.receipt.payload_hash
            or row.completion_contract_hash != completion_hash
            or row.briefs_hash != briefs_hash
            or row.content_hash != projection_digest
            or row.request_hash != request_hash
            or row.receipt_hash != receipt.payload_hash
        ):
            raise OwnerConflict("target_formal_plan_projection_integrity_invalid")
        formal_plan = FormalPlan(
            formal_plan_ref=graph.formal_plan_ref,
            briefs=briefs,
            content_binding=ContentBindingProof(
                subject_ref=graph.formal_plan_ref,
                content_hash_ref=projection_digest,
            ),
            acceptance_receipt=receipt_proof(
                receipt,
                subject_ref=projection_digest,
            ),
        )
        return AcceptedTargetFormalPlanProjection(
            graph_ref=graph_ref,
            formal_plan=formal_plan,
            plan_document_hash=source.plan_document_hash,
            source_acceptance_receipt=source.receipt,
            completion_contract=completion,
            completion_contract_hash=completion_hash,
            briefs_hash=briefs_hash,
            projection_digest=projection_digest,
            receipt=receipt,
            accepted_at=float(row.accepted_at),
        )

    def verify_formal_plan_projection(
        self,
        *,
        graph_ref: str,
        formal_plan: FormalPlan,
        plan_document_hash: str,
        source_acceptance_receipt: AcceptanceReceipt,
        completion_contract_hash: str,
        receipt: AcceptanceReceipt,
    ) -> None:
        accepted = self.query_formal_plan_projection(graph_ref=graph_ref)
        if accepted is None or (
            accepted.formal_plan != formal_plan
            or accepted.plan_document_hash != plan_document_hash
            or accepted.source_acceptance_receipt != source_acceptance_receipt
            or accepted.completion_contract_hash != completion_contract_hash
            or accepted.receipt != receipt
        ):
            raise OwnerConflict("target_formal_plan_projection_invalid")

    def _current_formal_plan_facts(
        self, graph_ref: str
    ) -> tuple[
        AcceptedTargetGraph,
        AcceptedFormalPlanContent,
        NormalizedCompletionContract,
        tuple[ExperimentBrief, ...],
    ]:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT request_ref FROM rg_target_graphs WHERE "
                    "graph_ref = :graph_ref"
                ),
                {"graph_ref": graph_ref},
            ).first()
        if row is None:
            raise OwnerConflict("target_formal_plan_projection_source_missing")
        graph = self._domain_reader.query_target_graph(row.request_ref)
        if type(graph) is not AcceptedTargetGraph or graph.graph_ref != graph_ref:
            raise OwnerConflict("target_formal_plan_projection_source_invalid")
        source = self._domain_reader.query_formal_plan_content_acceptance(
            graph.formal_plan_ref
        )
        if type(source) is not AcceptedFormalPlanContent:
            raise OwnerConflict("target_formal_plan_projection_source_missing")
        self._domain_reader.verify_formal_plan_content_acceptance(
            formal_plan_ref=graph.formal_plan_ref,
            plan_document_hash=source.plan_document_hash,
            receipt=source.receipt,
        )
        if source.plan_document_hash != graph.plan_document_hash:
            raise OwnerConflict("target_formal_plan_projection_source_invalid")
        contract = self._domain_reader.query_target_formal_plan_projection_source(
            request_ref=graph.request_ref,
            run_ref=graph.run_ref,
            graph_ref=graph.graph_ref,
            head_receipt=graph.head_receipt,
            formal_plan_content_receipt=source.receipt,
        )
        formal_plan_ref = contract.get("formal_plan_ref")
        completion = contract.get("completion_contract")
        if (
            formal_plan_ref != graph.formal_plan_ref
            or type(completion) is not NormalizedCompletionContract
        ):
            raise OwnerConflict("target_formal_plan_projection_source_invalid")
        briefs = tuple(item.brief for item in completion.experiments)
        if (
            contract.get("briefs") != briefs
            or completion.plan_document_hash != source.plan_document_hash
        ):
            raise OwnerConflict("target_formal_plan_projection_source_invalid")
        return graph, source, completion, briefs

    def accept_candidate_projection(
        self,
        *,
        target_ref: str,
        idempotency_key: str,
    ) -> AcceptedTargetCandidateProjection:
        if (
            not isinstance(target_ref, str)
            or not target_ref
            or not isinstance(idempotency_key, str)
            or not idempotency_key
            or len(idempotency_key) > 128
        ):
            raise OwnerConflict("target_candidate_projection_invalid")
        source, candidate = self._current_candidate_projection_facts(target_ref)
        source_receipt = source["source_acceptance_receipt"]
        if not isinstance(source_receipt, AcceptanceReceipt):
            raise OwnerConflict("target_candidate_projection_source_invalid")
        digest = canonical_target_candidate_projection_digest(
            target_ref=target_ref,
            candidate=candidate,
        )
        candidate_value = projection_plain_value(candidate)
        candidate_hash = canonical_hash(candidate_value)
        bindings = {
            "target_ref": target_ref,
            "graph_ref": source["graph_ref"],
            "source_spec_hash": source["source_spec_hash"],
            "source_acceptance_receipt_ref": source_receipt.receipt_ref,
            "source_acceptance_receipt_hash": source_receipt.payload_hash,
            "candidate": candidate_value,
            "candidate_hash": candidate_hash,
            "projection_digest": digest,
        }
        request_hash = canonical_hash(
            {"command": "accept_target_candidate_projection", **bindings}
        )
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rg_target_candidate_projections WHERE "
                    "idempotency_key = :key OR target_ref = :target_ref"
                ),
                {"key": idempotency_key, "target_ref": target_ref},
            ).first()
        if row is not None:
            if row.request_hash != request_hash:
                raise OwnerConflict("target_candidate_projection_conflict")
            accepted = self.query_candidate_projection(target_ref=target_ref)
            if accepted is None:
                raise OwnerConflict("target_candidate_projection_integrity_invalid")
            return accepted

        receipt = _receipt(
            "research_graph",
            RG_TARGET_CANDIDATE_PROJECTION_RECEIPT_KIND,
            new_ref("rg_target_candidate_projection_receipt"),
            digest,
            bindings,
        )
        now = time.time()
        try:
            with self._database.write() as connection:
                connection.execute(
                    text(
                        "INSERT INTO rg_target_candidate_projections "
                        "(target_ref, graph_ref, source_spec_hash, "
                        "source_acceptance_receipt_ref, "
                        "source_acceptance_receipt_hash, candidate_json, "
                        "candidate_hash, projection_digest, idempotency_key, "
                        "request_hash, receipt_ref, receipt_hash, accepted_at) "
                        "VALUES (:target_ref, :graph_ref, :source_spec_hash, "
                        ":source_acceptance_receipt_ref, "
                        ":source_acceptance_receipt_hash, :candidate_json, "
                        ":candidate_hash, :projection_digest, :idempotency_key, "
                        ":request_hash, :receipt_ref, :receipt_hash, :accepted_at)"
                    ),
                    {
                        **bindings,
                        "candidate_json": canonical_json(candidate_value),
                        "idempotency_key": idempotency_key,
                        "request_hash": request_hash,
                        "receipt_ref": receipt.receipt_ref,
                        "receipt_hash": receipt.payload_hash,
                        "accepted_at": now,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE research_graph_state SET revision = revision + 1, "
                        "target_candidate_projection_count = "
                        "target_candidate_projection_count + 1 WHERE "
                        "singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "research_graph.target_candidate_projection_accepted",
                    {
                        "target_ref": target_ref,
                        "graph_ref": source["graph_ref"],
                        "source_spec_hash": source["source_spec_hash"],
                        "projection_digest": digest,
                        "receipt_ref": receipt.receipt_ref,
                    },
                )
        except IntegrityError as error:
            raise OwnerConflict("target_candidate_projection_conflict") from error
        accepted = self.query_candidate_projection(target_ref=target_ref)
        if accepted is None:
            raise OwnerConflict("target_candidate_projection_missing_after_commit")
        return accepted

    def query_candidate_projection(
        self, *, target_ref: str
    ) -> AcceptedTargetCandidateProjection | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rg_target_candidate_projections WHERE "
                    "target_ref = :target_ref"
                ),
                {"target_ref": target_ref},
            ).first()
        if row is None:
            return None
        source, current_candidate = self._current_candidate_projection_facts(
            target_ref
        )
        source_receipt = source["source_acceptance_receipt"]
        if not isinstance(source_receipt, AcceptanceReceipt):
            raise OwnerConflict("target_candidate_projection_integrity_invalid")
        try:
            candidate_value = json.loads(row.candidate_json)
            candidate = _decode_bundle_value(candidate_value, TargetCandidate)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise OwnerConflict(
                "target_candidate_projection_integrity_invalid"
            ) from error
        if type(candidate) is not TargetCandidate or candidate != current_candidate:
            raise OwnerConflict("target_candidate_projection_integrity_invalid")
        digest = canonical_target_candidate_projection_digest(
            target_ref=target_ref,
            candidate=candidate,
        )
        candidate_hash = canonical_hash(candidate_value)
        bindings = {
            "target_ref": target_ref,
            "graph_ref": source["graph_ref"],
            "source_spec_hash": source["source_spec_hash"],
            "source_acceptance_receipt_ref": source_receipt.receipt_ref,
            "source_acceptance_receipt_hash": source_receipt.payload_hash,
            "candidate": candidate_value,
            "candidate_hash": candidate_hash,
            "projection_digest": digest,
        }
        request_hash = canonical_hash(
            {"command": "accept_target_candidate_projection", **bindings}
        )
        receipt = _receipt(
            "research_graph",
            RG_TARGET_CANDIDATE_PROJECTION_RECEIPT_KIND,
            row.receipt_ref,
            digest,
            bindings,
        )
        if (
            row.graph_ref != source["graph_ref"]
            or row.source_spec_hash != source["source_spec_hash"]
            or row.source_acceptance_receipt_ref != source_receipt.receipt_ref
            or row.source_acceptance_receipt_hash != source_receipt.payload_hash
            or canonical_json(candidate_value) != row.candidate_json
            or row.candidate_hash != candidate_hash
            or row.projection_digest != digest
            or row.request_hash != request_hash
            or row.receipt_hash != receipt.payload_hash
        ):
            raise OwnerConflict("target_candidate_projection_integrity_invalid")
        return AcceptedTargetCandidateProjection(
            target_ref=target_ref,
            graph_ref=str(source["graph_ref"]),
            candidate=candidate,
            source_spec_hash=str(source["source_spec_hash"]),
            source_acceptance_receipt=source_receipt,
            projection_digest=digest,
            receipt=receipt,
            accepted_at=float(row.accepted_at),
        )

    def verify_candidate_projection(
        self,
        *,
        target_ref: str,
        candidate: TargetCandidate,
        source_spec_hash: str,
        source_acceptance_receipt: AcceptanceReceipt,
        receipt: AcceptanceReceipt,
    ) -> None:
        accepted = self.query_candidate_projection(target_ref=target_ref)
        if accepted is None or (
            accepted.candidate != candidate
            or accepted.source_spec_hash != source_spec_hash
            or accepted.source_acceptance_receipt != source_acceptance_receipt
            or accepted.receipt != receipt
        ):
            raise OwnerConflict("target_candidate_projection_invalid")

    def verify_implementation_bundle_revision(
        self,
        *,
        target_ref: str,
        implementation_revision_ref: str,
    ) -> tuple[str, AcceptanceReceipt]:
        """Resolve an initial revision to the exact canonical Candidate fact.

        The Candidate projection has already reverified either the independent
        reuse proof chain or the explicit greenfield exception.  This method
        returns that receipt under its own subject; RM issues a second receipt
        for the actual code bundle and never relabels the projection receipt.
        Recovery replacements use a separate AR declaration path.
        """

        projection = self.query_candidate_projection(target_ref=target_ref)
        if projection is None:
            raise OwnerConflict("target_implementation_bundle_revision_invalid")
        candidate = projection.candidate
        if (
            candidate.implementation_revision_ref
            != implementation_revision_ref
        ):
            raise OwnerConflict("target_implementation_bundle_revision_invalid")
        origin_kind = (
            "greenfield"
            if candidate.reuse_trace.greenfield_exception is not None
            else "reused"
        )
        return origin_kind, projection.receipt

    def _current_candidate_projection_facts(
        self, target_ref: str
    ) -> tuple[dict[str, object], TargetCandidate]:
        source = self._verifier.query_target_candidate_projection_source(
            target_ref=target_ref
        )
        if set(source) != {
            "target_ref",
            "graph_ref",
            "spec",
            "source_spec_hash",
            "source_acceptance_receipt",
        } or source.get("target_ref") != target_ref:
            raise OwnerConflict("target_candidate_projection_source_invalid")
        graph_ref = source.get("graph_ref")
        spec = source.get("spec")
        source_hash = source.get("source_spec_hash")
        source_receipt = source.get("source_acceptance_receipt")
        if (
            not isinstance(graph_ref, str)
            or not graph_ref
            or type(spec) is not dict
            or not isinstance(source_hash, str)
            or canonical_hash(spec) != source_hash
            or not isinstance(source_receipt, AcceptanceReceipt)
            or source_receipt.subject_ref != source_hash
            or source_receipt.kind != TARGET_SPEC_CONTENT_RECEIPT_KIND
        ):
            raise OwnerConflict("target_candidate_projection_source_invalid")
        plan = self.query_formal_plan_projection(graph_ref=graph_ref)
        if plan is None:
            raise OwnerConflict("target_formal_plan_projection_required")
        try:
            formal = formal_target_candidate_from_dict(
                spec,
                completion_contract=plan.completion_contract,
            )
        except (BundleTargetContractError, TypeError, ValueError) as error:
            raise OwnerConflict("target_candidate_projection_source_invalid") from error
        return source, formal.candidate

    def accept_protocol_aggregation_from_result(
        self,
        *,
        target_ref: str,
        protected_binding_ref: str,
        result_manifest_ref: str,
        idempotency_key: str,
    ) -> tuple[tuple[ProtocolPart, ...], ProtocolAggregationProof]:
        """Forbid the legacy result-authored formal aggregation projection.

        Existing rows remain queryable as diagnostic history.  New formal-v3
        aggregation must come from the Plan-bound measurement-attempt
        authority, never from a protected Experiment result document.
        """

        del target_ref, protected_binding_ref, result_manifest_ref, idempotency_key
        raise OwnerConflict(
            "target_result_authored_protocol_aggregation_forbidden"
        )

    def query_protocol_aggregation(
        self,
        *,
        target_ref: str,
        protected_binding_ref: str,
        result_manifest_ref: str,
    ) -> tuple[tuple[ProtocolPart, ...], ProtocolAggregationProof] | None:
        facts = self._current_protocol_aggregation_facts(
            target_ref=target_ref,
            protected_binding_ref=protected_binding_ref,
            result_manifest_ref=result_manifest_ref,
        )
        declared = facts[6]
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rg_target_protocol_aggregations WHERE "
                    "protected_binding_ref = :protected_binding_ref"
                ),
                {"protected_binding_ref": protected_binding_ref},
            ).first()
        if row is None:
            return None
        if declared is None:
            raise OwnerConflict("target_protocol_aggregation_integrity_invalid")
        try:
            part_keys = tuple(json.loads(row.part_keys_json))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise OwnerConflict(
                "target_protocol_aggregation_integrity_invalid"
            ) from error
        payload_hash = canonical_hash(declared)
        bindings = {
            "target_ref": target_ref,
            "protected_binding_ref": protected_binding_ref,
            "result_manifest_ref": result_manifest_ref,
            "evaluation_attempt_ref": facts[0].evaluation_attempt_ref,
            "result_content_version_ref": facts[2].version_ref,
            "result_content_hash": facts[2].content_hash,
            **declared,
            "payload_hash": payload_hash,
        }
        request_hash = canonical_hash(
            {"command": "accept_protocol_aggregation_from_result", **bindings}
        )
        receipt = _receipt(
            "research_graph",
            RG_TARGET_PROTOCOL_AGGREGATION_RECEIPT_KIND,
            row.receipt_ref,
            payload_hash,
            {"aggregation_ref": row.aggregation_ref, **bindings},
        )
        if (
            row.target_ref != target_ref
            or row.result_manifest_ref != result_manifest_ref
            or row.evaluation_attempt_ref != facts[0].evaluation_attempt_ref
            or row.result_content_version_ref != facts[2].version_ref
            or row.result_content_hash != facts[2].content_hash
            or row.protocol_version_ref != declared["protocol_version_ref"]
            or part_keys != tuple(declared["part_keys"])
            or canonical_json(list(part_keys)) != row.part_keys_json
            or row.part_keys_hash != canonical_hash(list(part_keys))
            or row.aggregation_rule_ref != declared["aggregation_rule_ref"]
            or row.payload_hash != payload_hash
            or row.request_hash != request_hash
            or row.receipt_hash != receipt.payload_hash
        ):
            raise OwnerConflict("target_protocol_aggregation_integrity_invalid")
        parts = tuple(
            ProtocolPart(
                part_key=part_key,
                protocol_version_ref=str(declared["protocol_version_ref"]),
            )
            for part_key in part_keys
        )
        proof = ProtocolAggregationProof(
            protocol_version_ref=str(declared["protocol_version_ref"]),
            part_keys=part_keys,
            aggregation_rule_ref=str(declared["aggregation_rule_ref"]),
            aggregation_evidence_binding=ContentBindingProof(
                subject_ref=row.aggregation_ref,
                content_hash_ref=payload_hash,
            ),
            aggregation_evidence_receipt=receipt_proof(
                receipt,
                subject_ref=payload_hash,
            ),
        )
        return parts, proof

    def verify_protocol_aggregation(
        self,
        *,
        target_ref: str,
        protected_binding_ref: str,
        result_manifest_ref: str,
        parts: tuple[ProtocolPart, ...],
        proof: ProtocolAggregationProof | None,
    ) -> None:
        facts = self._current_protocol_aggregation_facts(
            target_ref=target_ref,
            protected_binding_ref=protected_binding_ref,
            result_manifest_ref=result_manifest_ref,
        )
        if facts[6] is None:
            if parts or proof is not None:
                raise OwnerConflict("target_protocol_aggregation_unexpected")
            return
        accepted = self.query_protocol_aggregation(
            target_ref=target_ref,
            protected_binding_ref=protected_binding_ref,
            result_manifest_ref=result_manifest_ref,
        )
        if accepted is None:
            raise OwnerConflict("target_protocol_aggregation_authority_unavailable")
        if accepted != (parts, proof):
            raise OwnerConflict("target_protocol_aggregation_invalid")

    def _current_protocol_aggregation_facts(
        self,
        *,
        target_ref: str,
        protected_binding_ref: str,
        result_manifest_ref: str,
    ) -> tuple[
        TargetProtectedExecutionBinding,
        AcceptedTargetResultManifest,
        TargetResultManifestEntry,
        dict[str, object],
        ExperimentDomainAdmission,
        object,
        dict[str, object] | None,
    ]:
        protected = self.query_protected_execution(protected_binding_ref)
        manifest, result_entry, result_content = (
            self._memory.materialize_result_content(result_manifest_ref)
        )
        domain = self._domain_reader.query_experiment(
            manifest.evaluation_attempt_ref
        )
        metric = self._domain_reader.query_formal_metric_result(
            manifest.evaluation_attempt_ref
        )
        roles = self._domain_reader.query_experiment_asset_roles(
            manifest.evaluation_attempt_ref
        )
        experiment_run = self._execution_verifier.query_experiment_run(
            manifest.evaluation_attempt_ref
        )
        if (
            protected is None
            or protected.target_ref != target_ref
            or manifest.target_ref != target_ref
            or manifest.target_run_ref != protected.target_run_ref
            or manifest.evaluation_attempt_ref != protected.evaluation_attempt_ref
            or manifest.experiment_run_ref != protected.experiment_run_ref
            or manifest.experiment_attempt_ref != protected.experiment_attempt_ref
            or manifest.experiment_fence_ref != protected.experiment_fence_ref
            or type(domain) is not ExperimentDomainAdmission
            or type(domain.intent) is not ProtocolExperimentIntent
            or domain.formal_measurement_status != "accepted"
            or getattr(domain.identities, "evaluation_attempt_ref", None)
            != protected.evaluation_attempt_ref
            or metric is None
            or getattr(metric, "metric_result_ref", None)
            != manifest.metric_result_ref
            or getattr(metric, "result_role_ref", None) != result_entry.role_ref
            or experiment_run is None
            or getattr(experiment_run, "status", None) != "executed"
            or getattr(experiment_run, "run_ref", None)
            != protected.experiment_run_ref
            or getattr(experiment_run, "attempt_ref", None)
            != protected.experiment_attempt_ref
            or getattr(experiment_run, "fence_ref", None)
            != protected.experiment_fence_ref
            or getattr(experiment_run, "result_hash", None) is None
            or getattr(experiment_run, "execution_receipt", None) is None
        ):
            raise OwnerConflict("target_protocol_aggregation_source_invalid")
        provider_result = ExperimentProviderResult.from_document(
            getattr(experiment_run, "result", None) or {}
        )
        execution_manifest = (
            self._execution_verifier.verify_experiment_execution_receipt(
                run_ref=protected.experiment_run_ref,
                attempt_ref=protected.experiment_attempt_ref,
                fence_ref=protected.experiment_fence_ref,
                evaluation_attempt_ref=protected.evaluation_attempt_ref,
                result_hash=experiment_run.result_hash,
                receipt=experiment_run.execution_receipt,
            )
        )
        role_entries = tuple(
            (
                role.role,
                role.ordinal,
                role.role_ref,
                role.subject_kind,
                role.subject_ref,
                role.binding.asset_ref,
                role.binding.version_ref,
                role.binding.content_hash,
                role.binding.manifest_hash,
                role.binding.receipt.receipt_ref,
                role.receipt.receipt_ref,
            )
            for role in sorted(
                roles, key=lambda item: (item.role, item.ordinal, item.role_ref)
            )
        )
        manifest_entries = tuple(
            (
                entry.role,
                entry.ordinal,
                entry.role_ref,
                entry.subject_kind,
                entry.subject_ref,
                entry.asset_ref,
                entry.version_ref,
                entry.content_hash,
                entry.manifest_hash,
                entry.asset_receipt_ref,
                entry.role_receipt_ref,
            )
            for entry in manifest.entries
        )
        if (
            provider_result.result_content != result_content
            or getattr(execution_manifest, "result_content_hash", None)
            != result_entry.content_hash
            or role_entries != manifest_entries
            or result_content.get("schema_ref")
            != experiment_result_schema_ref(domain.intent)
            or result_content.get("metrics") != getattr(metric, "metrics", None)
        ):
            raise OwnerConflict("target_protocol_aggregation_source_invalid")

        raw = result_content.get("protocol_aggregation")
        if raw is None:
            return (
                protected,
                manifest,
                result_entry,
                result_content,
                domain,
                metric,
                None,
            )
        if not isinstance(raw, dict) or set(raw) != {
            "protocol_version_ref",
            "part_keys",
            "aggregation_rule_ref",
        }:
            raise OwnerConflict("target_protocol_aggregation_result_invalid")
        protocol_version_ref = raw.get("protocol_version_ref")
        part_keys = raw.get("part_keys")
        aggregation_rule_ref = raw.get("aggregation_rule_ref")
        if (
            protocol_version_ref != domain.identities.protocol_version_ref
            or not isinstance(part_keys, list)
            or not part_keys
            or len(part_keys) > 64
            or any(
                not isinstance(part_key, str)
                or not part_key
                or len(part_key) > 256
                for part_key in part_keys
            )
            or len(part_keys) != len(set(part_keys))
            or not isinstance(aggregation_rule_ref, str)
            or not aggregation_rule_ref
            or len(aggregation_rule_ref) > 256
        ):
            raise OwnerConflict("target_protocol_aggregation_result_invalid")
        declared = {
            "protocol_version_ref": protocol_version_ref,
            "part_keys": part_keys,
            "aggregation_rule_ref": aggregation_rule_ref,
        }
        canonical_hash(declared)
        return (
            protected,
            manifest,
            result_entry,
            result_content,
            domain,
            metric,
            declared,
        )

    def accept_input_asset_role(
        self,
        *,
        target_ref: str,
        role: AcceptedAssetRole,
        rm_proof_receipt: AcceptanceReceipt,
        idempotency_key: str,
    ) -> AcceptedTargetInputAssetProjection:
        rm_value = self._memory.query_input_asset(
            target_ref=target_ref, asset_ref=role.asset_ref
        )
        if rm_value is None or rm_value[1] != rm_proof_receipt:
            raise OwnerConflict("target_input_asset_rm_proof_invalid")
        asset, _receipt_value = rm_value
        if role.asset_binding() != asset:
            raise OwnerConflict("target_input_asset_role_binding_invalid")
        self._verifier.verify_asset_role_receipt(
            role_ref=role.role_ref,
            version_ref=role.version_ref,
            role=role.role,
            quest_ref=role.quest_ref,
            receipt=role.receipt,
        )
        with self._database.read() as connection:
            target = connection.execute(
                text(
                    "SELECT g.quest_ref, t.spec_json FROM rg_targets t JOIN "
                    "rg_target_graphs g ON g.graph_ref = t.graph_ref WHERE "
                    "t.target_ref = :target_ref"
                ),
                {"target_ref": target_ref},
            ).first()
        if target is None or target.quest_ref != role.quest_ref:
            raise OwnerConflict("target_input_asset_role_binding_invalid")
        bindings = {
            "target_ref": target_ref,
            "asset_ref": asset.asset_ref,
            "source_role_ref": role.role_ref,
            "source_role_receipt_ref": role.receipt.receipt_ref,
            "source_role_receipt_hash": role.receipt.payload_hash,
            "rm_proof_receipt_ref": rm_proof_receipt.receipt_ref,
            "rm_proof_receipt_hash": rm_proof_receipt.payload_hash,
        }
        request_hash = canonical_hash(bindings)
        now = time.time()
        with self._database.write() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rg_target_input_asset_role_proofs WHERE "
                    "idempotency_key = :key OR (target_ref = :target_ref AND "
                    "asset_ref = :asset_ref)"
                ),
                {
                    "key": idempotency_key,
                    "target_ref": target_ref,
                    "asset_ref": asset.asset_ref,
                },
            ).first()
            if row is not None:
                if row.request_hash != request_hash:
                    raise OwnerConflict("target_input_asset_role_conflict")
            else:
                proof_ref = new_ref("rg_target_input_asset_role_proof")
                receipt_ref = new_ref("rg_target_input_asset_role_receipt")
                receipt = _receipt(
                    "research_graph",
                    RG_TARGET_INPUT_ASSET_ROLE_RECEIPT_KIND,
                    receipt_ref,
                    asset.asset_ref,
                    {**bindings, "proof_ref": proof_ref},
                )
                connection.execute(
                    text(
                        "INSERT INTO rg_target_input_asset_role_proofs "
                        "(proof_ref, target_ref, asset_ref, source_role_ref, "
                        "source_role_receipt_ref, source_role_receipt_hash, "
                        "rm_proof_receipt_ref, rm_proof_receipt_hash, "
                        "idempotency_key, request_hash, receipt_ref, "
                        "receipt_hash, accepted_at) VALUES (:proof_ref, "
                        ":target_ref, :asset_ref, :source_role_ref, "
                        ":source_role_receipt_ref, :source_role_receipt_hash, "
                        ":rm_proof_receipt_ref, :rm_proof_receipt_hash, "
                        ":idempotency_key, :request_hash, :receipt_ref, "
                        ":receipt_hash, :accepted_at)"
                    ),
                    {
                        **bindings,
                        "proof_ref": proof_ref,
                        "idempotency_key": idempotency_key,
                        "request_hash": request_hash,
                        "receipt_ref": receipt.receipt_ref,
                        "receipt_hash": receipt.payload_hash,
                        "accepted_at": now,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE research_graph_state SET revision = revision + 1, "
                        "target_input_asset_role_proof_count = "
                        "target_input_asset_role_proof_count + 1 WHERE singleton = 'owner'"
                    )
                )
        accepted = self.query_input_asset_projection(
            target_ref=target_ref, asset_ref=asset.asset_ref
        )
        if accepted is None:
            raise OwnerConflict("target_input_asset_role_missing")
        return accepted

    def query_input_asset_projection(
        self, *, target_ref: str, asset_ref: str
    ) -> AcceptedTargetInputAssetProjection | None:
        rm_value = self._memory.query_input_asset(
            target_ref=target_ref, asset_ref=asset_ref
        )
        if rm_value is None:
            return None
        asset, rm_receipt = rm_value
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT p.*, r.version_ref, r.role, r.quest_ref, "
                    "r.receipt_ref AS actual_role_receipt_ref, "
                    "r.receipt_hash AS actual_role_receipt_hash FROM "
                    "rg_target_input_asset_role_proofs p JOIN rg_asset_roles r "
                    "ON r.role_ref = p.source_role_ref WHERE p.target_ref = "
                    ":target_ref AND p.asset_ref = :asset_ref"
                ),
                {"target_ref": target_ref, "asset_ref": asset_ref},
            ).first()
        if row is None:
            return None
        source_receipt = AcceptanceReceipt(
            issuer="research_graph",
            kind="asset_role_acceptance",
            receipt_ref=row.source_role_receipt_ref,
            subject_ref=row.source_role_ref,
            payload_hash=row.source_role_receipt_hash,
        )
        self._verifier.verify_asset_role_receipt(
            role_ref=row.source_role_ref,
            version_ref=row.version_ref,
            role=row.role,
            quest_ref=row.quest_ref,
            receipt=source_receipt,
        )
        bindings = {
            "target_ref": target_ref,
            "asset_ref": asset_ref,
            "source_role_ref": row.source_role_ref,
            "source_role_receipt_ref": source_receipt.receipt_ref,
            "source_role_receipt_hash": source_receipt.payload_hash,
            "rm_proof_receipt_ref": rm_receipt.receipt_ref,
            "rm_proof_receipt_hash": rm_receipt.payload_hash,
        }
        receipt = _receipt(
            "research_graph",
            RG_TARGET_INPUT_ASSET_ROLE_RECEIPT_KIND,
            row.receipt_ref,
            asset_ref,
            {**bindings, "proof_ref": row.proof_ref},
        )
        if (
            row.rm_proof_receipt_ref != rm_receipt.receipt_ref
            or row.rm_proof_receipt_hash != rm_receipt.payload_hash
            or row.receipt_hash != receipt.payload_hash
        ):
            raise OwnerConflict("target_input_asset_role_integrity_invalid")
        return AcceptedTargetInputAssetProjection(
            target_ref=target_ref,
            asset=asset,
            rm_proof_receipt=rm_receipt,
            source_role_ref=row.source_role_ref,
            source_role_receipt=source_receipt,
            rg_proof_receipt=receipt,
        )

    def query_bundle_input_asset_proof(
        self, *, target_ref: str, asset_ref: str
    ) -> AcceptedInputAssetProof | None:
        projection = self.query_input_asset_projection(
            target_ref=target_ref,
            asset_ref=asset_ref,
        )
        return None if projection is None else projection.as_bundle_proof()

    def accept_execution_input_binding(
        self,
        *,
        target_ref: str,
        target_run_ref: str,
        target_attempt_ref: str,
        target_fence_ref: str,
        target_spec_hash: str,
        target_scope_binding_hash: str,
        input_refs: tuple[str, ...],
        idempotency_key: str,
    ) -> AcceptedTargetExecutionInputBinding:
        if input_refs != tuple(sorted(set(input_refs))):
            raise OwnerConflict("target_execution_input_binding_invalid")
        binding_ref = new_ref("target_execution_input_binding")
        bindings = {
            "target_ref": target_ref,
            "target_run_ref": target_run_ref,
            "target_attempt_ref": target_attempt_ref,
            "target_fence_ref": target_fence_ref,
            "target_spec_hash": target_spec_hash,
            "target_scope_binding_hash": target_scope_binding_hash,
            "input_refs": list(input_refs),
        }
        request_hash = canonical_hash(bindings)
        now = time.time()
        with self._database.write() as connection:
            replay = connection.execute(
                text(
                    "SELECT * FROM rg_target_execution_input_bindings WHERE "
                    "idempotency_key = :key OR (target_run_ref = :run_ref AND "
                    "target_attempt_ref = :attempt_ref)"
                ),
                {
                    "key": idempotency_key,
                    "run_ref": target_run_ref,
                    "attempt_ref": target_attempt_ref,
                },
            ).first()
            if replay is not None:
                if replay.request_hash != request_hash:
                    raise OwnerConflict("target_execution_input_binding_conflict")
                binding_ref = replay.binding_ref
            else:
                receipt_ref = new_ref("rg_target_execution_input_receipt")
                receipt = _receipt(
                    "research_graph",
                    RG_TARGET_EXECUTION_INPUT_RECEIPT_KIND,
                    receipt_ref,
                    binding_ref,
                    {**bindings, "binding_ref": binding_ref},
                )
                connection.execute(
                    text(
                        "INSERT INTO rg_target_execution_input_bindings "
                        "(binding_ref, target_ref, target_run_ref, "
                        "target_attempt_ref, target_fence_ref, target_spec_hash, "
                        "target_scope_binding_hash, "
                        "input_refs_json, input_refs_hash, idempotency_key, "
                        "request_hash, receipt_ref, receipt_hash, accepted_at) "
                        "VALUES (:binding_ref, :target_ref, :target_run_ref, "
                        ":target_attempt_ref, :target_fence_ref, "
                        ":target_spec_hash, :target_scope_binding_hash, "
                        ":input_refs_json, :input_refs_hash, "
                        ":idempotency_key, :request_hash, :receipt_ref, "
                        ":receipt_hash, :accepted_at)"
                    ),
                    {
                        **bindings,
                        "binding_ref": binding_ref,
                        "input_refs_json": canonical_json(list(input_refs)),
                        "input_refs_hash": canonical_hash(list(input_refs)),
                        "idempotency_key": idempotency_key,
                        "request_hash": request_hash,
                        "receipt_ref": receipt.receipt_ref,
                        "receipt_hash": receipt.payload_hash,
                        "accepted_at": now,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE research_graph_state SET revision = revision + 1, "
                        "target_execution_input_binding_count = "
                        "target_execution_input_binding_count + 1 WHERE singleton = 'owner'"
                    )
                )
        accepted = self.query_execution_input_binding(binding_ref)
        if accepted is None:
            raise OwnerConflict("target_execution_input_binding_missing")
        return accepted

    def query_execution_input_binding(
        self, binding_ref: str
    ) -> AcceptedTargetExecutionInputBinding | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rg_target_execution_input_bindings WHERE "
                    "binding_ref = :binding_ref"
                ),
                {"binding_ref": binding_ref},
            ).first()
        if row is None:
            return None
        try:
            input_refs = tuple(json.loads(row.input_refs_json))
        except (TypeError, ValueError) as error:
            raise OwnerConflict("target_execution_input_binding_integrity_invalid") from error
        bindings = {
            "target_ref": row.target_ref,
            "target_run_ref": row.target_run_ref,
            "target_attempt_ref": row.target_attempt_ref,
            "target_fence_ref": row.target_fence_ref,
            "target_spec_hash": row.target_spec_hash,
            "target_scope_binding_hash": row.target_scope_binding_hash,
            "input_refs": list(input_refs),
        }
        receipt = _receipt(
            "research_graph",
            RG_TARGET_EXECUTION_INPUT_RECEIPT_KIND,
            row.receipt_ref,
            binding_ref,
            {**bindings, "binding_ref": binding_ref},
        )
        if (
            input_refs != tuple(sorted(set(input_refs)))
            or row.input_refs_hash != canonical_hash(list(input_refs))
            or row.request_hash != canonical_hash(bindings)
            or row.receipt_hash != receipt.payload_hash
        ):
            raise OwnerConflict("target_execution_input_binding_integrity_invalid")
        proof = ExecutionInputBindingProof(
            binding_ref=binding_ref,
            subject_ref=row.target_attempt_ref,
            input_refs=input_refs,
            acceptance_receipt=_proof(receipt),
        )
        return AcceptedTargetExecutionInputBinding(
            target_ref=row.target_ref,
            target_run_ref=row.target_run_ref,
            target_attempt_ref=row.target_attempt_ref,
            target_fence_ref=row.target_fence_ref,
            target_spec_hash=row.target_spec_hash,
            target_scope_binding_hash=row.target_scope_binding_hash,
            proof=proof,
            accepted_at=float(row.accepted_at),
        )

    def query_execution_input_binding_for_target_run(
        self,
        *,
        target_ref: str,
        target_run_ref: str,
    ) -> AcceptedTargetExecutionInputBinding | None:
        """Reconcile the canonical input binding without guessing its ref."""

        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT binding_ref FROM rg_target_execution_input_bindings "
                    "WHERE target_ref = :target_ref AND target_run_ref = "
                    ":target_run_ref"
                ),
                {
                    "target_ref": target_ref,
                    "target_run_ref": target_run_ref,
                },
            ).first()
        return (
            None
            if row is None
            else self.query_execution_input_binding(row.binding_ref)
        )

    def query_execution_input_binding_for_attempt(
        self,
        *,
        target_ref: str,
        target_run_ref: str,
        target_attempt_ref: str,
        target_fence_ref: str,
    ) -> AcceptedTargetExecutionInputBinding | None:
        """Resolve one exact recovery generation without choosing a sibling."""

        with self._database.read() as connection:
            rows = connection.execute(
                text(
                    "SELECT binding_ref FROM rg_target_execution_input_bindings "
                    "WHERE target_ref = :target_ref AND target_run_ref = "
                    ":target_run_ref AND target_attempt_ref = "
                    ":target_attempt_ref AND target_fence_ref = "
                    ":target_fence_ref"
                ),
                {
                    "target_ref": target_ref,
                    "target_run_ref": target_run_ref,
                    "target_attempt_ref": target_attempt_ref,
                    "target_fence_ref": target_fence_ref,
                },
            ).all()
        if not rows:
            return None
        if len(rows) != 1:
            raise OwnerConflict("target_execution_input_binding_integrity_invalid")
        return self.query_execution_input_binding(rows[0].binding_ref)

    def recover_generic_operation_for_handle(
        self,
        handle: TargetWorkHandle,
    ) -> RecoveredTargetOperation | None:
        """Compatibility reader over current state or accepted retired exit."""


        accepted = self.query_generic_execution_binding_for_retiring_handle(
            handle
        )
        if accepted is not None:
            facts = self.query_generic_execution_terminal(
                accepted.binding_ref,
                retiring_handle=handle,
            )
            if facts is None:
                raise OwnerConflict("target_generic_execution_integrity_invalid")
            binding, request, terminal, _input = facts
            return RecoveredTargetOperation(
                operation=TargetOperationHandle(binding.operation_handle),
                request=request,
                request_sha256=binding.request_hash,
                terminal_result=terminal,
            )

        state = self.query_generic_operation_state_for_handle(handle)
        if state.status is TargetOperationRecoveryStatus.ABSENT:
            return None
        if state.status is TargetOperationRecoveryStatus.OUTCOME_UNKNOWN:
            raise OwnerConflict("target_execution_operation_recovery_unknown")
        if state.operation is None:
            raise OwnerConflict("target_execution_operation_recovery_invalid")
        return state.operation

    def query_generic_operation_state_for_handle(
        self,
        handle: TargetWorkHandle,
    ) -> RecoveredTargetOperationState:
        """Return the port's exhaustive issuer-signed current state."""


        port = self._generic_execution_port
        if port is None:
            raise OwnerConflict("target_generic_execution_authority_unavailable")
        authority = self._domain_reader.query_target_measurement_domain_authority(
            handle.target_ref
        )
        if authority is None:
            raise OwnerConflict("target_measurement_domain_authority_required")
        try:
            binding = TargetMeasurementAuthorityBinding(
                authority_ref=authority.authority_ref,
                acceptance_receipt=receipt_proof(
                    authority.receipt,
                    subject_ref=authority.authority_hash,
                ),
            )
            return port.query_current_operation_state(
                current_handle=handle,
                measurement_authority=binding,
            )
        except LegacyTargetExecutionError as error:
            raise OwnerConflict(error.code) from error

    def recover_historical_generic_operation_outcome_unknown(
        self,
        handle: TargetWorkHandle,
    ) -> TargetExecutionOutcomeUnknownFact | None:
        """Reopen one signed unknown fact without granting execution rights."""


        port = self._generic_execution_port
        if port is None:
            raise OwnerConflict("target_generic_execution_authority_unavailable")
        authority = self._domain_reader.query_target_measurement_domain_authority(
            handle.target_ref
        )
        if authority is None:
            raise OwnerConflict("target_measurement_domain_authority_required")
        try:
            binding = TargetMeasurementAuthorityBinding(
                authority_ref=authority.authority_ref,
                acceptance_receipt=receipt_proof(
                    authority.receipt,
                    subject_ref=authority.authority_hash,
                ),
            )
            fact = port.recover_historical_outcome_unknown(
                historical_handle=handle,
                measurement_authority=binding,
            )
        except LegacyTargetExecutionError as error:
            raise OwnerConflict(error.code) from error
        if fact is not None and (
            fact.handle != handle or fact.measurement_authority != binding
        ):
            raise OwnerConflict("target_execution_historical_unknown_invalid")
        return fact

    def accept_protected_execution(
        self,
        *,
        target_ref: str,
        target_run_ref: str,
        target_attempt_ref: str,
        target_fence_ref: str,
        input_binding_ref: str,
        experiment_run_ref: str,
        experiment_attempt_ref: str,
        experiment_fence_ref: str,
        evaluation_attempt_ref: str,
        execution_request_ref: str,
        definition_hash: str,
        experiment_request_receipt: AcceptanceReceipt,
        idempotency_key: str,
    ) -> TargetProtectedExecutionBinding:
        raise OwnerConflict("legacy_target_protected_execution_write_forbidden")

    def query_protected_execution(
        self, binding_ref: str
    ) -> TargetProtectedExecutionBinding | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rg_target_protected_execution_bindings "
                    "WHERE binding_ref = :binding_ref"
                ),
                {"binding_ref": binding_ref},
            ).first()
        if row is None:
            return None
        experiment_receipt = AcceptanceReceipt(
            issuer="research_graph",
            kind=EXPERIMENT_EXECUTION_REQUEST_RECEIPT_KIND,
            receipt_ref=row.experiment_request_receipt_ref,
            subject_ref=row.execution_request_ref,
            payload_hash=row.experiment_request_receipt_hash,
        )
        bindings = {
            "target_ref": row.target_ref,
            "target_run_ref": row.target_run_ref,
            "target_attempt_ref": row.target_attempt_ref,
            "target_fence_ref": row.target_fence_ref,
            "input_binding_ref": row.input_binding_ref,
            "experiment_run_ref": row.experiment_run_ref,
            "experiment_attempt_ref": row.experiment_attempt_ref,
            "experiment_fence_ref": row.experiment_fence_ref,
            "evaluation_attempt_ref": row.evaluation_attempt_ref,
            "execution_request_ref": row.execution_request_ref,
            "definition_hash": row.definition_hash,
            "experiment_request_receipt": experiment_receipt.as_public_dict(),
        }
        domain = self._domain_reader.query_experiment(row.evaluation_attempt_ref)
        identities = getattr(domain, "identities", None)
        execution_request = getattr(domain, "execution_request", None)
        if (
            domain is None
            or identities is None
            or execution_request is None
            or getattr(identities, "evaluation_attempt_ref", None)
            != row.evaluation_attempt_ref
            or getattr(execution_request, "execution_request_ref", None)
            != row.execution_request_ref
            or getattr(execution_request, "definition_hash", None)
            != row.definition_hash
            or getattr(execution_request, "receipt", None) != experiment_receipt
        ):
            raise OwnerConflict("target_protected_execution_domain_invalid")
        receipt = _receipt(
            "research_graph",
            RG_TARGET_PROTECTED_EXECUTION_RECEIPT_KIND,
            row.receipt_ref,
            binding_ref,
            {**bindings, "binding_ref": binding_ref, "ordinal": int(row.ordinal)},
        )
        if row.request_hash != canonical_hash(bindings) or (
            row.receipt_hash != receipt.payload_hash
        ):
            raise OwnerConflict("target_protected_execution_integrity_invalid")
        return TargetProtectedExecutionBinding(
            binding_ref=binding_ref,
            target_ref=row.target_ref,
            ordinal=int(row.ordinal),
            target_run_ref=row.target_run_ref,
            target_attempt_ref=row.target_attempt_ref,
            target_fence_ref=row.target_fence_ref,
            input_binding_ref=row.input_binding_ref,
            experiment_run_ref=row.experiment_run_ref,
            experiment_attempt_ref=row.experiment_attempt_ref,
            experiment_fence_ref=row.experiment_fence_ref,
            evaluation_attempt_ref=row.evaluation_attempt_ref,
            execution_request_ref=row.execution_request_ref,
            definition_hash=row.definition_hash,
            experiment_request_receipt=experiment_receipt,
            receipt=receipt,
            accepted_at=float(row.accepted_at),
        )

    def accept_generic_execution_binding(
        self,
        *,
        request: TargetExecutionRequest,
        operation: TargetOperationHandle,
        exit_receipt: TargetExecutionExitReceipt,
        idempotency_key: str,
    ) -> TargetGenericExecutionBinding:
        """Accept one terminal generic operation as formal-v3 execution.

        This path never reads or creates an Experiment Run.  The port rereads
        its signed invocation/exit spool while the AR/RM admission verifier
        rechecks the current Target Fence, execution eligibility, and exact
        implementation bytes before RG writes its terminal binding.
        """


        if (
            type(request) is not TargetExecutionRequest
            or type(operation) is not TargetOperationHandle
            or type(exit_receipt) is not TargetExecutionExitReceipt
            or not isinstance(idempotency_key, str)
            or not idempotency_key
            or len(idempotency_key) > 128
        ):
            raise OwnerConflict("target_generic_execution_invalid")
        input_binding, terminal = self._generic_execution_facts(
            request=request,
            operation=operation,
            exit_receipt=exit_receipt,
        )
        handle = request.handle
        request_value = projection_plain_value(request)
        request_content_hash = canonical_hash(request_value)
        exit_value = projection_plain_value(terminal.exit_receipt)
        exit_hash = canonical_hash(exit_value)
        eligibility_receipt_hash = canonical_hash(
            projection_plain_value(request.execution_eligibility_receipt)
        )
        input_receipt_hash = canonical_hash(
            projection_plain_value(handle.execution_input_binding_receipt)
        )
        payload = {
            "target_ref": handle.target_ref,
            "target_run_ref": handle.target_run_ref,
            "target_attempt_ref": handle.execution_attempt_ref,
            "target_fence_ref": handle.execution_fence_ref,
            "input_binding_ref": handle.execution_input_binding_ref,
            "input_binding_receipt": projection_plain_value(
                handle.execution_input_binding_receipt
            ),
            "execution_eligibility_ref": request.execution_eligibility_ref,
            "execution_eligibility_receipt": projection_plain_value(
                request.execution_eligibility_receipt
            ),
            "operation_handle": operation.token,
            "execution_request_ref": request.execution_request_ref,
            "operation_request_hash": request_content_hash,
            "command_spec_hash": terminal.exit_receipt.command_spec_sha256,
            "terminal_status": terminal.exit_receipt.status,
            "exit_receipt": exit_value,
            "exit_receipt_hash": exit_hash,
            "process_tree_drained": True,
            "currentness_known": True,
            "current": True,
        }
        payload_hash = canonical_hash(payload)
        request_hash = canonical_hash(
            {"command": "accept_target_generic_execution", "payload": payload}
        )
        now = time.time()
        with self._database.fenced_write() as connection:
            from meta_research.owners.agent_runtime import (
                verify_current_target_run_frontier_in_transaction,
            )

            verify_current_target_run_frontier_in_transaction(
                connection,
                request.handle,
            )
            replay = connection.execute(
                text(
                    "SELECT * FROM rg_target_generic_execution_bindings_v3 WHERE "
                    "idempotency_key = :key OR operation_handle = :operation"
                ),
                {"key": idempotency_key, "operation": operation.token},
            ).first()
            if replay is not None:
                if replay.request_hash != request_hash:
                    raise OwnerConflict("target_generic_execution_conflict")
                binding_ref = replay.binding_ref
            else:
                ordinal = int(
                    connection.execute(
                        text(
                            "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM "
                            "rg_target_generic_execution_bindings_v3 WHERE "
                            "target_ref = :target_ref"
                        ),
                        {"target_ref": handle.target_ref},
                    ).scalar_one()
                )
                binding_ref = new_ref("target_generic_execution")
                receipt = _receipt(
                    "research_graph",
                    RG_TARGET_GENERIC_EXECUTION_RECEIPT_KIND,
                    new_ref("rg_target_generic_execution_receipt"),
                    binding_ref,
                    {
                        "binding_ref": binding_ref,
                        "ordinal": ordinal,
                        "payload_hash": payload_hash,
                        **payload,
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO rg_target_generic_execution_bindings_v3 "
                        "(binding_ref, target_ref, ordinal, target_run_ref, "
                        "target_attempt_ref, target_fence_ref, "
                        "input_binding_ref, input_binding_receipt_ref, "
                        "input_binding_receipt_hash, execution_eligibility_ref, "
                        "execution_eligibility_receipt_ref, "
                        "execution_eligibility_receipt_hash, operation_handle, "
                        "execution_request_ref, operation_request_json, "
                        "operation_request_hash, command_spec_hash, "
                        "terminal_status, exit_receipt_ref, exit_receipt_json, "
                        "exit_receipt_hash, process_tree_drained, "
                        "currentness_known, current, payload_json, payload_hash, "
                        "idempotency_key, request_hash, receipt_ref, "
                        "receipt_hash, accepted_at) VALUES (:binding_ref, "
                        ":target_ref, :ordinal, :target_run_ref, "
                        ":target_attempt_ref, :target_fence_ref, "
                        ":input_binding_ref, :input_binding_receipt_ref, "
                        ":input_binding_receipt_hash, :eligibility_ref, "
                        ":eligibility_receipt_ref, :eligibility_receipt_hash, "
                        ":operation_handle, :execution_request_ref, "
                        ":operation_request_json, :operation_request_hash, "
                        ":command_spec_hash, :terminal_status, "
                        ":exit_receipt_ref, :exit_receipt_json, "
                        ":exit_receipt_hash, 1, 1, 1, :payload_json, "
                        ":payload_hash, :idempotency_key, :request_hash, "
                        ":receipt_ref, :receipt_hash, :accepted_at)"
                    ),
                    {
                        "binding_ref": binding_ref,
                        "target_ref": handle.target_ref,
                        "ordinal": ordinal,
                        "target_run_ref": handle.target_run_ref,
                        "target_attempt_ref": handle.execution_attempt_ref,
                        "target_fence_ref": handle.execution_fence_ref,
                        "input_binding_ref": handle.execution_input_binding_ref,
                        "input_binding_receipt_ref": (
                            handle.execution_input_binding_receipt.receipt_ref
                        ),
                        "input_binding_receipt_hash": input_receipt_hash,
                        "eligibility_ref": request.execution_eligibility_ref,
                        "eligibility_receipt_ref": (
                            request.execution_eligibility_receipt.receipt_ref
                        ),
                        "eligibility_receipt_hash": eligibility_receipt_hash,
                        "operation_handle": operation.token,
                        "execution_request_ref": request.execution_request_ref,
                        "operation_request_json": canonical_json(request_value),
                        "operation_request_hash": request_content_hash,
                        "command_spec_hash": (
                            terminal.exit_receipt.command_spec_sha256
                        ),
                        "terminal_status": terminal.exit_receipt.status,
                        "exit_receipt_ref": terminal.exit_receipt.receipt_ref,
                        "exit_receipt_json": canonical_json(exit_value),
                        "exit_receipt_hash": exit_hash,
                        "payload_json": canonical_json(payload),
                        "payload_hash": payload_hash,
                        "idempotency_key": idempotency_key,
                        "request_hash": request_hash,
                        "receipt_ref": receipt.receipt_ref,
                        "receipt_hash": receipt.payload_hash,
                        "accepted_at": now,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE research_graph_state SET revision = revision + 1, "
                        "target_generic_execution_binding_count = "
                        "target_generic_execution_binding_count + 1 WHERE "
                        "singleton = 'owner'"
                    )
                )
        accepted = self.query_generic_execution_binding(binding_ref)
        if accepted is None:
            raise OwnerConflict("target_generic_execution_missing")
        return accepted

    def query_generic_execution_binding(
        self,
        binding_ref: str,
        *,
        retiring_handle: TargetWorkHandle | None = None,
    ) -> TargetGenericExecutionBinding | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rg_target_generic_execution_bindings_v3 WHERE "
                    "binding_ref = :binding_ref"
                ),
                {"binding_ref": binding_ref},
            ).first()
        if row is None:
            return None
        try:
            request = _decode_stored_record(
                row.operation_request_json,
                row.operation_request_hash,
                TargetExecutionRequest,
            )
            exit_receipt = _decode_stored_record(
                row.exit_receipt_json,
                row.exit_receipt_hash,
                TargetExecutionExitReceipt,
            )
            payload = json.loads(row.payload_json)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise OwnerConflict("target_generic_execution_integrity_invalid") from error
        if (
            type(request) is not TargetExecutionRequest
            or type(exit_receipt) is not TargetExecutionExitReceipt
            or not isinstance(payload, dict)
        ):
            raise OwnerConflict("target_generic_execution_integrity_invalid")
        input_binding, terminal = self._generic_execution_facts(
            request=request,
            operation=TargetOperationHandle(row.operation_handle),
            exit_receipt=exit_receipt,
            retiring_handle=retiring_handle,
        )
        handle = request.handle
        expected_payload = {
            "target_ref": handle.target_ref,
            "target_run_ref": handle.target_run_ref,
            "target_attempt_ref": handle.execution_attempt_ref,
            "target_fence_ref": handle.execution_fence_ref,
            "input_binding_ref": handle.execution_input_binding_ref,
            "input_binding_receipt": projection_plain_value(
                handle.execution_input_binding_receipt
            ),
            "execution_eligibility_ref": request.execution_eligibility_ref,
            "execution_eligibility_receipt": projection_plain_value(
                request.execution_eligibility_receipt
            ),
            "operation_handle": row.operation_handle,
            "execution_request_ref": request.execution_request_ref,
            "operation_request_hash": row.operation_request_hash,
            "command_spec_hash": terminal.exit_receipt.command_spec_sha256,
            "terminal_status": terminal.exit_receipt.status,
            "exit_receipt": projection_plain_value(terminal.exit_receipt),
            "exit_receipt_hash": row.exit_receipt_hash,
            "process_tree_drained": True,
            "currentness_known": True,
            "current": True,
        }
        expected_payload_hash = canonical_hash(expected_payload)
        expected_request_hash = canonical_hash(
            {
                "command": "accept_target_generic_execution",
                "payload": expected_payload,
            }
        )
        receipt = _receipt(
            "research_graph",
            RG_TARGET_GENERIC_EXECUTION_RECEIPT_KIND,
            row.receipt_ref,
            binding_ref,
            {
                "binding_ref": binding_ref,
                "ordinal": int(row.ordinal),
                "payload_hash": expected_payload_hash,
                **expected_payload,
            },
        )
        eligibility_receipt_hash = canonical_hash(
            projection_plain_value(request.execution_eligibility_receipt)
        )
        input_receipt_hash = canonical_hash(
            projection_plain_value(handle.execution_input_binding_receipt)
        )
        if (
            input_binding.proof.binding_ref != handle.execution_input_binding_ref
            or row.target_ref != handle.target_ref
            or row.target_run_ref != handle.target_run_ref
            or row.target_attempt_ref != handle.execution_attempt_ref
            or row.target_fence_ref != handle.execution_fence_ref
            or row.input_binding_ref != handle.execution_input_binding_ref
            or row.input_binding_receipt_ref
            != handle.execution_input_binding_receipt.receipt_ref
            or row.input_binding_receipt_hash != input_receipt_hash
            or row.execution_eligibility_ref
            != request.execution_eligibility_ref
            or row.execution_eligibility_receipt_ref
            != request.execution_eligibility_receipt.receipt_ref
            or row.execution_eligibility_receipt_hash
            != eligibility_receipt_hash
            or row.execution_request_ref != request.execution_request_ref
            or row.command_spec_hash != terminal.exit_receipt.command_spec_sha256
            or row.terminal_status != terminal.exit_receipt.status
            or row.exit_receipt_ref != terminal.exit_receipt.receipt_ref
            or row.process_tree_drained != 1
            or row.currentness_known != 1
            or row.current != 1
            or payload != expected_payload
            or row.payload_hash != expected_payload_hash
            or row.request_hash != expected_request_hash
            or row.receipt_hash != receipt.payload_hash
        ):
            raise OwnerConflict("target_generic_execution_integrity_invalid")
        return TargetGenericExecutionBinding(
            binding_ref=binding_ref,
            target_ref=handle.target_ref,
            ordinal=int(row.ordinal),
            target_run_ref=handle.target_run_ref,
            target_attempt_ref=handle.execution_attempt_ref,
            target_fence_ref=handle.execution_fence_ref,
            input_binding_ref=handle.execution_input_binding_ref,
            input_binding_receipt=handle.execution_input_binding_receipt,
            execution_eligibility_ref=request.execution_eligibility_ref,
            execution_eligibility_receipt=(
                request.execution_eligibility_receipt
            ),
            operation_handle=row.operation_handle,
            execution_request_ref=request.execution_request_ref,
            request_hash=row.operation_request_hash,
            command_spec_hash=terminal.exit_receipt.command_spec_sha256,
            terminal_status=terminal.exit_receipt.status,
            exit_receipt_ref=terminal.exit_receipt.receipt_ref,
            exit_receipt_hash=row.exit_receipt_hash,
            process_tree_drained=True,
            currentness_known=True,
            current=True,
            receipt=receipt,
            accepted_at=float(row.accepted_at),
        )

    def query_generic_execution_binding_by_idempotency(
        self,
        idempotency_key: str,
    ) -> TargetGenericExecutionBinding | None:
        """Reconcile an accepted generic operation after response loss."""

        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT binding_ref FROM "
                    "rg_target_generic_execution_bindings_v3 WHERE "
                    "idempotency_key = :idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
        return (
            None
            if row is None
            else self.query_generic_execution_binding(row.binding_ref)
        )

    def query_generic_execution_binding_for_target_run(
        self,
        *,
        target_ref: str,
        target_run_ref: str,
    ) -> TargetGenericExecutionBinding | None:
        """Resolve the exact AR-current Attempt/Fence, never scan siblings."""

        verifier = self._generic_admission_verifier
        if verifier is None:
            raise OwnerConflict("target_generic_execution_authority_unavailable")
        query_current = getattr(verifier, "query_current_work_handle", None)
        if not callable(query_current):
            raise OwnerConflict("target_generic_execution_authority_unavailable")
        handle = query_current(target_ref)
        if handle is None:
            return None
        if handle.target_run_ref != target_run_ref:
            raise OwnerConflict("target_generic_execution_integrity_invalid")
        return self.query_generic_execution_binding_for_handle(handle)

    def query_generic_execution_binding_for_handle(
        self,
        handle: TargetWorkHandle,
    ) -> TargetGenericExecutionBinding | None:
        """Read the one binding for an exact current Attempt/Fence."""

        verifier = self._generic_admission_verifier
        if verifier is None:
            raise OwnerConflict("target_generic_execution_authority_unavailable")
        try:
            if verifier.verify_current_work_handle(handle) != handle:
                raise OwnerConflict("target_run_frontier_not_current")
        except Exception as error:
            if isinstance(error, OwnerConflict):
                raise
            raise OwnerConflict("target_generic_execution_authority_invalid") from error
        with self._database.read() as connection:
            rows = connection.execute(
                text(
                    "SELECT binding_ref FROM "
                    "rg_target_generic_execution_bindings_v3 WHERE target_ref = "
                    ":target_ref AND target_run_ref = :target_run_ref AND "
                    "target_attempt_ref = :attempt_ref AND target_fence_ref = "
                    ":fence_ref"
                ),
                {
                    "target_ref": handle.target_ref,
                    "target_run_ref": handle.target_run_ref,
                    "attempt_ref": handle.execution_attempt_ref,
                    "fence_ref": handle.execution_fence_ref,
                },
            ).all()
        if not rows:
            return None
        if len(rows) != 1:
            raise OwnerConflict("target_generic_execution_integrity_invalid")
        return self.query_generic_execution_binding(rows[0].binding_ref)

    def query_generic_execution_binding_for_retiring_handle(
        self,
        handle: TargetWorkHandle,
    ) -> TargetGenericExecutionBinding | None:
        """Read one exact signed terminal without granting execution currentness."""

        verifier = self._generic_admission_verifier
        verify_retiring = (
            None
            if verifier is None
            else getattr(verifier, "verify_retiring_work_handle", None)
        )
        if not callable(verify_retiring) or verify_retiring(handle) != handle:
            raise OwnerConflict("target_generic_execution_authority_invalid")
        with self._database.read() as connection:
            rows = connection.execute(
                text(
                    "SELECT binding_ref FROM "
                    "rg_target_generic_execution_bindings_v3 WHERE target_ref = "
                    ":target_ref AND target_run_ref = :target_run_ref AND "
                    "target_attempt_ref = :attempt_ref AND target_fence_ref = "
                    ":fence_ref"
                ),
                {
                    "target_ref": handle.target_ref,
                    "target_run_ref": handle.target_run_ref,
                    "attempt_ref": handle.execution_attempt_ref,
                    "fence_ref": handle.execution_fence_ref,
                },
            ).all()
        if not rows:
            return None
        if len(rows) != 1:
            raise OwnerConflict("target_generic_execution_integrity_invalid")
        return self.query_generic_execution_binding(
            rows[0].binding_ref,
            retiring_handle=handle,
        )

    def query_generic_execution_terminal(
        self,
        binding_ref: str,
        *,
        retiring_handle: TargetWorkHandle | None = None,
    ) -> tuple[
        TargetGenericExecutionBinding,
        TargetExecutionRequest,
        TargetExecutionTerminalResult,
        AcceptedTargetExecutionInputBinding,
    ] | None:
        """Re-read the signed invocation and terminal bytes behind one binding."""


        binding = self.query_generic_execution_binding(
            binding_ref,
            retiring_handle=retiring_handle,
        )
        if binding is None:
            return None
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT operation_request_json, operation_request_hash, "
                    "exit_receipt_json, exit_receipt_hash FROM "
                    "rg_target_generic_execution_bindings_v3 WHERE "
                    "binding_ref = :binding_ref"
                ),
                {"binding_ref": binding_ref},
            ).one()
        try:
            request = _decode_stored_record(
                row.operation_request_json,
                row.operation_request_hash,
                TargetExecutionRequest,
            )
            exit_receipt = _decode_stored_record(
                row.exit_receipt_json,
                row.exit_receipt_hash,
                TargetExecutionExitReceipt,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise OwnerConflict("target_generic_execution_integrity_invalid") from error
        if (
            type(request) is not TargetExecutionRequest
            or type(exit_receipt) is not TargetExecutionExitReceipt
        ):
            raise OwnerConflict("target_generic_execution_integrity_invalid")
        input_binding, terminal = self._generic_execution_facts(
            request=request,
            operation=TargetOperationHandle(binding.operation_handle),
            exit_receipt=exit_receipt,
            retiring_handle=retiring_handle,
        )
        if (
            binding.request_hash != row.operation_request_hash
            or binding.exit_receipt_hash != row.exit_receipt_hash
            or terminal.exit_receipt != exit_receipt
        ):
            raise OwnerConflict("target_generic_execution_integrity_invalid")
        return binding, request, terminal, input_binding

    def query_generic_execution_input_assets(
        self,
        binding_ref: str,
    ) -> tuple[AcceptedAssetBinding, ...]:
        """Resolve exact RM bindings behind an opaque current operation.

        ``TargetWorkHandle`` intentionally exposes only compact Bundle proofs.
        Measurement reuse must nevertheless compare the complete immutable
        AssetVersion binding, so this issuer-side seam reopens every accepted
        input projection and rejects receipt drift before returning it.
        """

        facts = self.query_generic_execution_terminal(binding_ref)
        if facts is None:
            raise OwnerConflict("target_generic_execution_missing")
        binding, request, _terminal, target_input = facts
        if (
            target_input.proof.binding_ref != binding.input_binding_ref
            or target_input.proof.input_refs
            != tuple(sorted(set(target_input.proof.input_refs)))
        ):
            raise OwnerConflict("target_generic_execution_input_invalid")
        accepted: list[AcceptedAssetBinding] = []
        for proof in request.handle.accepted_input_asset_proofs:
            projection = self.query_input_asset_projection(
                target_ref=binding.target_ref,
                asset_ref=proof.asset_ref,
            )
            if projection is None or projection.as_bundle_proof() != proof:
                raise OwnerConflict("target_generic_execution_input_invalid")
            if projection.asset.asset_ref not in target_input.proof.input_refs:
                raise OwnerConflict("target_generic_execution_input_invalid")
            accepted.append(projection.asset)
        if len({asset.asset_ref for asset in accepted}) != len(accepted):
            raise OwnerConflict("target_generic_execution_input_invalid")
        return tuple(accepted)

    def accept_generic_measurement(
        self,
        *,
        target_ref: str,
        generic_binding_ref: str,
        result_manifest_ref: str,
        idempotency_key: str,
    ) -> AcceptedTargetGenericMeasurement:
        """Reject the superseded result-authored measurement projection.

        Rows created by development snapshots remain issuer-verified through
        :meth:`query_generic_measurement` for diagnostics only.  A formal
        Target result cannot choose its own ProtocolVersion, required Metric
        set, atomic parts, or aggregation rule.  Until RG exposes the
        Plan-bound typed protocol authority required by the fixed contract,
        this write path is deliberately unavailable.
        """

        del target_ref, generic_binding_ref, result_manifest_ref, idempotency_key
        raise OwnerConflict("target_generic_measurement_shadow_write_forbidden")

    def query_generic_measurement(
        self, measurement_ref: str
    ) -> AcceptedTargetGenericMeasurement | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rg_target_generic_measurements WHERE "
                    "measurement_ref = :measurement_ref"
                ),
                {"measurement_ref": measurement_ref},
            ).first()
        if row is None:
            return None
        terminal_facts = self.query_generic_execution_terminal(row.generic_binding_ref)
        manifest = self._memory.query_generic_result_manifest(row.manifest_ref)
        candidate_projection = self.query_candidate_projection(
            target_ref=row.target_ref
        )
        if terminal_facts is None or manifest is None or candidate_projection is None:
            raise OwnerConflict("target_generic_measurement_integrity_invalid")
        try:
            payload = json.loads(row.payload_json)
            experiment_keys = tuple(json.loads(row.experiment_keys_json))
            metric_values = tuple(json.loads(row.metrics_json))
            variant_binding = _decode_stored_record(
                row.variant_input_binding_json,
                row.variant_input_binding_hash,
                ExecutionInputBindingProof,
            )
            evaluation_binding = _decode_stored_record(
                row.evaluation_input_binding_json,
                row.evaluation_input_binding_hash,
                ExecutionInputBindingProof,
            )
            parts = _decode_bundle_value(
                payload["protocol_internal_parts"], tuple[ProtocolPart, ...]
            )
            aggregation = _decode_bundle_value(
                payload["protocol_aggregation_proof"],
                ProtocolAggregationProof | None,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise OwnerConflict("target_generic_measurement_integrity_invalid") from error
        if (
            type(variant_binding) is not ExecutionInputBindingProof
            or type(evaluation_binding) is not ExecutionInputBindingProof
            or type(parts) is not tuple
            or type(experiment_keys) is not tuple
            or type(metric_values) is not tuple
        ):
            raise OwnerConflict("target_generic_measurement_integrity_invalid")
        expected_payload = {
            **payload,
            "experiment_keys": list(experiment_keys),
            "metric_values": list(metric_values),
            "variant_run_input_binding": projection_plain_value(variant_binding),
            "evaluation_attempt_input_binding": projection_plain_value(
                evaluation_binding
            ),
            "protocol_internal_parts": projection_plain_value(parts),
            "protocol_aggregation_proof": projection_plain_value(aggregation),
        }
        payload_hash = canonical_hash(expected_payload)
        receipt = _receipt(
            "research_graph",
            RG_TARGET_GENERIC_MEASUREMENT_RECEIPT_KIND,
            row.receipt_ref,
            row.evaluation_attempt_ref,
            {
                "measurement_ref": measurement_ref,
                "payload_hash": payload_hash,
                **expected_payload,
            },
        )
        if (
            payload != expected_payload
            or row.experiment_keys_hash != canonical_hash(list(experiment_keys))
            or row.metrics_hash != canonical_hash(list(metric_values))
            or row.payload_hash != payload_hash
            or row.request_hash
            != canonical_hash(
                {
                    "command": "accept_target_generic_measurement",
                    "payload": expected_payload,
                }
            )
            or row.receipt_hash != receipt.payload_hash
            or manifest.generic_binding_ref != row.generic_binding_ref
            or candidate_projection.candidate.experiment_keys != experiment_keys
            or candidate_projection.candidate.measurement_unit_keys
            != (row.measurement_unit_key,)
        ):
            raise OwnerConflict("target_generic_measurement_integrity_invalid")
        return AcceptedTargetGenericMeasurement(
            measurement_ref=measurement_ref,
            target_ref=row.target_ref,
            target_run_ref=row.target_run_ref,
            target_attempt_ref=row.target_attempt_ref,
            target_fence_ref=row.target_fence_ref,
            generic_binding_ref=row.generic_binding_ref,
            manifest_ref=row.manifest_ref,
            measurement_source_version_ref=payload["measurement_source_version_ref"],
            experiment_keys=experiment_keys,
            measurement_unit_key=row.measurement_unit_key,
            variant_run_ref=row.variant_run_ref,
            evaluation_ref=row.evaluation_ref,
            protocol_version_ref=row.protocol_version_ref,
            evaluation_attempt_ref=row.evaluation_attempt_ref,
            metric_result_ref=row.metric_result_ref,
            metric_values=metric_values,
            result_disposition=payload["result_disposition"],
            checkpoint_artifact_refs=tuple(payload["checkpoint_artifact_refs"]),
            variant_run_input_binding=variant_binding,
            evaluation_attempt_input_binding=evaluation_binding,
            protocol_internal_parts=parts,
            protocol_aggregation_proof=aggregation,
            payload_hash=payload_hash,
            receipt=receipt,
            accepted_at=float(row.accepted_at),
        )

    def _generic_execution_facts(
        self,
        *,
        request: TargetExecutionRequest,
        operation: TargetOperationHandle,
        exit_receipt: TargetExecutionExitReceipt,
        retiring_handle: TargetWorkHandle | None = None,
    ) -> tuple[
        AcceptedTargetExecutionInputBinding,
        TargetExecutionTerminalResult,
    ]:
        admission_verifier = self._generic_admission_verifier
        execution_port = self._generic_execution_port
        if admission_verifier is None or execution_port is None:
            raise OwnerConflict("target_generic_execution_authority_unavailable")
        try:
            if retiring_handle is None:
                admission = admission_verifier.verify_execution_admission(request)
                verified_handle = admission.handle
                terminal = execution_port.verify_terminal_operation(
                    operation,
                    expected_request_sha256=canonical_hash(
                        projection_plain_value(request)
                    ),
                    expected_exit_receipt=exit_receipt,
                )
            else:
                verify_retiring = getattr(
                    admission_verifier, "verify_retiring_work_handle", None
                )
                if (
                    request.handle != retiring_handle
                    or not callable(verify_retiring)
                    or verify_retiring(retiring_handle) != retiring_handle
                ):
                    raise OwnerConflict("target_run_history_handle_invalid")
                verified_handle = retiring_handle
                recovered_terminal = (
                    execution_port.verify_retiring_terminal_operation(
                    operation,
                    retiring_handle=retiring_handle,
                    measurement_authority=request.measurement_authority,
                    expected_request_sha256=canonical_hash(
                        projection_plain_value(request)
                    ),
                    expected_exit_receipt=exit_receipt,
                )
                )
                terminal = recovered_terminal.terminal_result
                if terminal is None:
                    raise OwnerConflict("target_execution_terminal_required")
        except Exception as error:
            raise OwnerConflict("target_generic_execution_authority_invalid") from error
        input_binding = self.query_execution_input_binding(
            request.handle.execution_input_binding_ref
        )
        if input_binding is None or (
            verified_handle,
            input_binding.target_ref,
            input_binding.target_run_ref,
            input_binding.target_attempt_ref,
            input_binding.target_fence_ref,
            input_binding.proof.acceptance_receipt,
            exit_receipt.issuer,
            exit_receipt.kind,
            exit_receipt.subject_ref,
            exit_receipt.operation,
            exit_receipt.execution_eligibility_ref,
            exit_receipt.execution_eligibility_receipt_sha256,
            exit_receipt.execution_input_binding_ref,
            exit_receipt.execution_input_binding_receipt_sha256,
            exit_receipt.process_tree_drained,
        ) != (
            request.handle,
            request.handle.target_ref,
            request.handle.target_run_ref,
            request.handle.execution_attempt_ref,
            request.handle.execution_fence_ref,
            request.handle.execution_input_binding_receipt,
            "TargetExecutionPort",
            "target_execution_exit",
            operation.token,
            operation,
            request.execution_eligibility_ref,
            canonical_hash(
                projection_plain_value(request.execution_eligibility_receipt)
            ),
            request.handle.execution_input_binding_ref,
            canonical_hash(
                projection_plain_value(
                    request.handle.execution_input_binding_receipt
                )
            ),
            True,
        ):
            raise OwnerConflict("target_generic_execution_authority_invalid")
        return input_binding, terminal

class SQLiteTargetRunAgentAuthority:
    """Agent Runtime's current Harness handle and child-review verifier."""

    def __init__(
        self,
        database: Database,
        feed: DurableFeed,
        harness: AgentRuntimeHarnessInterface,
        graph: SQLiteTargetRunGraphAuthority,
        memory: SQLiteTargetRunMemoryAuthority,
        domain_reader: ResearchGraphTargetReader,
        execution_verifier: AgentRuntimeExperimentVerifier,
        workspace_root: Path | None = None,
    ) -> None:
        self._database = database
        self._feed = feed
        self._harness = harness
        self._graph = graph
        self._memory = memory
        self._domain_reader = domain_reader
        self._execution_verifier = execution_verifier
        self._workspace_root = (
            None if workspace_root is None else workspace_root.resolve()
        )

    def reserve_target_workspace(
        self,
        *,
        handle: TargetWorkHandle,
        idempotency_key: str,
    ) -> TargetRunWorkspace:
        """Reserve one private Harness cwd from the current domain handle."""

        self.verify_current_target_run_handle(handle)
        if (
            self._workspace_root is None
            or not isinstance(idempotency_key, str)
            or not idempotency_key
            or len(idempotency_key) > 128
        ):
            raise OwnerConflict("target_run_workspace_unavailable")
        payload = {
            "target_ref": handle.target_ref,
            "target_run_ref": handle.target_run_ref,
            "root_session_ref": handle.root_session_ref,
            "target_attempt_ref": handle.execution_attempt_ref,
            "target_fence_ref": handle.execution_fence_ref,
            "implementation_relative_path": "implementation",
            "inputs_relative_path": "inputs",
        }
        payload_hash = canonical_hash(payload)
        request_hash = canonical_hash(
            {"command": "reserve_target_run_workspace", **payload}
        )
        now = time.time()
        with self._database.write() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ar_target_run_workspaces WHERE "
                    "idempotency_key = :key OR target_attempt_ref = :attempt_ref"
                ),
                {"key": idempotency_key, "attempt_ref": handle.execution_attempt_ref},
            ).first()
            if row is not None:
                if row.request_hash != request_hash or row.status != "active":
                    raise OwnerConflict("target_run_workspace_conflict")
                workspace_ref = row.workspace_ref
            else:
                active = connection.execute(
                    text(
                        "SELECT workspace_ref FROM ar_target_run_workspaces WHERE "
                        "target_run_ref = :run_ref AND status = 'active'"
                    ),
                    {"run_ref": handle.target_run_ref},
                ).all()
                if active:
                    raise OwnerConflict("target_run_workspace_recovery_required")
                ordinal = int(
                    connection.execute(
                        text(
                            "SELECT COUNT(*) FROM ar_target_run_workspaces WHERE "
                            "target_run_ref = :run_ref"
                        ),
                        {"run_ref": handle.target_run_ref},
                    ).scalar_one()
                ) + 1
                workspace_ref = new_ref("target_run_workspace")
                root_name = canonical_hash({"workspace_ref": workspace_ref})
                receipt = _receipt(
                    "agent_runtime",
                    AR_TARGET_RUN_WORKSPACE_RECEIPT_KIND,
                    new_ref("ar_target_run_workspace_receipt"),
                    workspace_ref,
                    {
                        **payload,
                        "workspace_ref": workspace_ref,
                        "ordinal": ordinal,
                        "root_name": root_name,
                        "payload_hash": payload_hash,
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO ar_target_run_workspaces (workspace_ref, "
                        "target_ref, target_run_ref, root_session_ref, "
                        "target_attempt_ref, target_fence_ref, ordinal, root_name, "
                        "status, payload_json, payload_hash, idempotency_key, "
                        "request_hash, receipt_ref, receipt_hash, created_at) VALUES "
                        "(:workspace_ref, :target_ref, :target_run_ref, "
                        ":root_session_ref, :target_attempt_ref, :target_fence_ref, "
                        ":ordinal, :root_name, 'active', :payload_json, :payload_hash, "
                        ":idempotency_key, :request_hash, :receipt_ref, "
                        ":receipt_hash, :created_at)"
                    ),
                    {
                        **payload,
                        "workspace_ref": workspace_ref,
                        "ordinal": ordinal,
                        "root_name": root_name,
                        "payload_json": canonical_json(payload),
                        "payload_hash": payload_hash,
                        "idempotency_key": idempotency_key,
                        "request_hash": request_hash,
                        "receipt_ref": receipt.receipt_ref,
                        "receipt_hash": receipt.payload_hash,
                        "created_at": now,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE agent_runtime_state SET revision = revision + 1, "
                        "target_run_workspace_count = target_run_workspace_count + 1 "
                        "WHERE singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "agent_runtime.target_run_workspace_reserved",
                    {
                        "workspace_ref": workspace_ref,
                        "target_ref": handle.target_ref,
                        "target_run_ref": handle.target_run_ref,
                        "target_attempt_ref": handle.execution_attempt_ref,
                        "receipt_ref": receipt.receipt_ref,
                    },
                )
        workspace = self.query_target_workspace(handle.target_run_ref)
        if workspace is None or workspace.workspace_ref != workspace_ref:
            raise OwnerConflict("target_run_workspace_integrity_invalid")
        self._ensure_target_workspace_layout(workspace)
        return workspace

    def query_target_workspace(
        self, target_run_ref: str
    ) -> TargetRunWorkspace | None:
        with self._database.read() as connection:
            rows = connection.execute(
                text(
                    "SELECT * FROM ar_target_run_workspaces WHERE "
                    "target_run_ref = :run_ref AND status = 'active' ORDER BY "
                    "ordinal DESC"
                ),
                {"run_ref": target_run_ref},
            ).all()
        if not rows:
            return None
        if len(rows) != 1:
            raise OwnerConflict("target_run_workspace_integrity_invalid")
        row = rows[0]
        run = self._harness.query_target_run_by_ref(target_run_ref)
        payload = {
            "target_ref": row.target_ref,
            "target_run_ref": row.target_run_ref,
            "root_session_ref": row.root_session_ref,
            "target_attempt_ref": row.target_attempt_ref,
            "target_fence_ref": row.target_fence_ref,
            "implementation_relative_path": "implementation",
            "inputs_relative_path": "inputs",
        }
        payload_hash = canonical_hash(payload)
        receipt = _receipt(
            "agent_runtime",
            AR_TARGET_RUN_WORKSPACE_RECEIPT_KIND,
            row.receipt_ref,
            row.workspace_ref,
            {
                **payload,
                "workspace_ref": row.workspace_ref,
                "ordinal": int(row.ordinal),
                "root_name": row.root_name,
                "payload_hash": payload_hash,
            },
        )
        if (
            run is None
            or (run.run_ref, run.root_session_ref, run.attempt_ref, run.fence_ref)
            != (
                row.target_run_ref,
                row.root_session_ref,
                row.target_attempt_ref,
                row.target_fence_ref,
            )
            or run.request.get("target_ref") != row.target_ref
            or row.root_name != canonical_hash({"workspace_ref": row.workspace_ref})
            or row.payload_json != canonical_json(payload)
            or row.payload_hash != payload_hash
            or row.request_hash
            != canonical_hash({"command": "reserve_target_run_workspace", **payload})
            or row.receipt_hash != receipt.payload_hash
        ):
            raise OwnerConflict("target_run_workspace_integrity_invalid")
        return TargetRunWorkspace(
            workspace_ref=row.workspace_ref,
            target_ref=row.target_ref,
            target_run_ref=row.target_run_ref,
            root_session_ref=row.root_session_ref,
            target_attempt_ref=row.target_attempt_ref,
            target_fence_ref=row.target_fence_ref,
            ordinal=int(row.ordinal),
            implementation_relative_path="implementation",
            inputs_relative_path="inputs",
            payload_hash=payload_hash,
            receipt=receipt,
            status=row.status,
            created_at=float(row.created_at),
        )

    def resolve_target_workspace(
        self,
        *,
        target_ref: str,
        target_run_ref: str,
        root_session_ref: str,
        attempt_ref: str,
        fence_ref: str,
    ) -> tuple[str, Path]:
        workspace = self.query_target_workspace(target_run_ref)
        if workspace is None or (
            workspace.target_ref,
            workspace.root_session_ref,
            workspace.target_attempt_ref,
            workspace.target_fence_ref,
        ) != (target_ref, root_session_ref, attempt_ref, fence_ref):
            raise OwnerConflict("target_run_workspace_unavailable")
        path = self._ensure_target_workspace_layout(workspace)
        return workspace.workspace_ref, path

    def materialize_target_workspace_inputs(
        self,
        *,
        handle: TargetWorkHandle,
        accepted_target_commit_inputs: tuple[FrozenTargetCommitInput, ...] = (),
    ) -> tuple[str, ...]:
        """Populate the root's read-only input directory from accepted RM bytes.

        This is delivery of already accepted context, not acceptance of a
        Target output.  It runs before the root Session starts and is exact
        replay: a pre-existing path must contain the same bytes.
        """

        self.verify_current_target_run_handle(handle)
        self._validate_target_commit_inputs(
            handle=handle,
            accepted=accepted_target_commit_inputs,
        )
        workspace = self.query_target_workspace(handle.target_run_ref)
        if workspace is None:
            raise OwnerConflict("target_run_workspace_unavailable")
        root = self._ensure_target_workspace_layout(workspace)
        inputs = root / workspace.inputs_relative_path
        frozen_inputs = self._ensure_frozen_input_root(workspace)
        expected, entries = self._target_workspace_input_projection(
            handle=handle,
            accepted_target_commit_inputs=accepted_target_commit_inputs,
        )
        manifest_value = {
            "schema_ref": "meta-research/target-root-input-manifest/v1",
            "target_ref": handle.target_ref,
            "target_run_ref": handle.target_run_ref,
            "entries": entries,
        }
        expected["manifest.json"] = canonical_json(manifest_value).encode("utf-8")
        try:
            self._write_target_input_tree(
                root=frozen_inputs,
                expected=expected,
            )
            self._verify_target_workspace_input_tree(
                inputs=frozen_inputs,
                expected=expected,
            )
        finally:
            self._lock_input_directories(frozen_inputs)
        pointer = canonical_json(
            {
                "schema_ref": "meta-research/target-root-input-pointer/v1",
                "read_only_manifest_path": str(frozen_inputs / "manifest.json"),
                "manifest_sha256": hashlib.sha256(
                    expected["manifest.json"]
                ).hexdigest(),
            }
        ).encode("utf-8")
        try:
            self._write_target_input_tree(
                root=inputs,
                expected={"manifest.json": pointer},
            )
            self._verify_target_workspace_input_tree(
                inputs=inputs,
                expected={"manifest.json": pointer},
            )
        finally:
            self._lock_input_directories(inputs)
        ordered = ("manifest.json",) + tuple(
            sorted(path for path in expected if path != "manifest.json")
        )
        return tuple(str(frozen_inputs / path) for path in ordered)

    def verify_target_workspace_inputs(
        self,
        *,
        handle: TargetWorkHandle,
        accepted_target_commit_inputs: tuple[FrozenTargetCommitInput, ...] = (),
    ) -> None:
        """Re-read sources and reject any root-writable projection drift."""

        self.verify_current_target_run_handle(handle)
        self._validate_target_commit_inputs(
            handle=handle,
            accepted=accepted_target_commit_inputs,
        )
        workspace = self.query_target_workspace(handle.target_run_ref)
        if workspace is None:
            raise OwnerConflict("target_run_workspace_unavailable")
        root = self._ensure_target_workspace_layout(workspace)
        inputs = root / workspace.inputs_relative_path
        frozen_inputs = self._ensure_frozen_input_root(workspace)
        expected, entries = self._target_workspace_input_projection(
            handle=handle,
            accepted_target_commit_inputs=accepted_target_commit_inputs,
        )
        expected["manifest.json"] = canonical_json(
            {
                "schema_ref": "meta-research/target-root-input-manifest/v1",
                "target_ref": handle.target_ref,
                "target_run_ref": handle.target_run_ref,
                "entries": entries,
            }
        ).encode("utf-8")
        self._verify_target_workspace_input_tree(
            inputs=frozen_inputs,
            expected=expected,
        )
        pointer = canonical_json(
            {
                "schema_ref": "meta-research/target-root-input-pointer/v1",
                "read_only_manifest_path": str(frozen_inputs / "manifest.json"),
                "manifest_sha256": hashlib.sha256(
                    expected["manifest.json"]
                ).hexdigest(),
            }
        ).encode("utf-8")
        self._verify_target_workspace_input_tree(
            inputs=inputs,
            expected={"manifest.json": pointer},
        )

    def query_target_workspace_quest_ref(self, handle: TargetWorkHandle) -> str:
        """Return the AR-admitted Quest identity for issuer-scoped RG reads."""

        self.verify_current_target_run_handle(handle)
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT quest_ref, target_run_ref FROM ar_target_launches "
                    "WHERE target_ref = :target_ref"
                ),
                {"target_ref": handle.target_ref},
            ).first()
        if (
            row is None
            or row.target_run_ref != handle.target_run_ref
            or not row.quest_ref
        ):
            raise OwnerConflict("target_run_input_quest_invalid")
        return str(row.quest_ref)

    def resolve_generic_target_commit_input(
        self,
        transition: AcceptedTargetCommitTransition,
    ) -> FrozenTargetCommitInput | None:
        """Resolve an older formal-v3 RM manifest without weakening ownership."""

        if type(transition) is not AcceptedTargetCommitTransition:
            raise OwnerConflict("target_root_upstream_input_invalid")
        manifest_ref = transition.canonical_terminal.asset_manifest_ref
        manifest = self._memory.query_generic_result_manifest(manifest_ref)
        if manifest is None:
            return None
        artifacts: list[FrozenTargetCommitInputArtifact] = []
        for entry in manifest.entries:
            content = self._memory.materialize_generic_result_asset(
                manifest_ref=manifest_ref,
                version_ref=entry.binding.version_ref,
            )
            artifacts.append(
                FrozenTargetCommitInputArtifact(
                    ordinal=entry.ordinal,
                    role=entry.role,
                    declared_relative_path=entry.relative_path,
                    artifact_kind="file",
                    media_type=(
                        "application/json"
                        if entry.role == "result_content"
                        else "application/octet-stream"
                    ),
                    version_ref=entry.binding.version_ref,
                    content_hash=entry.binding.content_hash,
                    tree_hash=entry.binding.content_hash,
                    content=content,
                )
            )
        manifest_content = canonical_json(projection_plain_value(manifest)).encode(
            "utf-8"
        )
        return FrozenTargetCommitInput(
            target_commit_ref=transition.target_commit_ref,
            target_ref=transition.target_ref,
            target_run_ref=transition.target_run_ref,
            manifest_ref=manifest.manifest_ref,
            manifest_payload_hash=manifest.payload_hash,
            manifest_receipt_ref=manifest.receipt.receipt_ref,
            manifest_content=manifest_content,
            artifacts=tuple(artifacts),
        )

    def _target_workspace_input_projection(
        self,
        *,
        handle: TargetWorkHandle,
        accepted_target_commit_inputs: tuple[FrozenTargetCommitInput, ...],
    ) -> tuple[dict[str, bytes], list[dict[str, object]]]:
        expected: dict[str, bytes] = {}
        entries: list[dict[str, object]] = []
        for ordinal, proof in enumerate(handle.accepted_input_asset_proofs, start=1):
            asset, _proof_receipt, file_name, content = (
                self._memory.materialize_input_asset(
                    target_ref=handle.target_ref,
                    asset_ref=proof.asset_ref,
                )
            )
            suffix = _safe_input_suffix(file_name)
            relative_path = (
                "direct/"
                f"{ordinal:04d}-"
                f"{hashlib.sha256(asset.asset_ref.encode('utf-8')).hexdigest()[:16]}"
                f"{suffix}"
            )
            expected[relative_path] = content
            entries.append(
                {
                    "kind": "direct_asset",
                    "relative_path": relative_path,
                    "asset_ref": asset.asset_ref,
                    "version_ref": asset.version_ref,
                    "content_sha256": hashlib.sha256(content).hexdigest(),
                    "byte_count": len(content),
                }
            )
        for commit_ordinal, accepted in enumerate(
            accepted_target_commit_inputs, start=1
        ):
            commit_digest = hashlib.sha256(
                accepted.target_commit_ref.encode("utf-8")
            ).hexdigest()
            prefix = (
                "upstream/"
                f"{commit_ordinal:04d}-"
                f"{commit_digest[:16]}"
            )
            manifest_path = prefix + "/manifest.json"
            expected[manifest_path] = accepted.manifest_content
            artifact_entries: list[dict[str, object]] = []
            for artifact in accepted.artifacts:
                suffix = _safe_input_suffix(artifact.declared_relative_path)
                if artifact.artifact_kind == "directory":
                    suffix = ".zip"
                artifact_path = (
                    prefix
                    + "/artifacts/"
                    + f"{artifact.ordinal:04d}-{artifact.role}"
                    + suffix
                )
                if artifact_path in expected:
                    raise OwnerConflict(
                        "target_run_workspace_input_integrity_invalid"
                    )
                expected[artifact_path] = artifact.content
                artifact_entries.append(
                    {
                        "relative_path": artifact_path,
                        "role": artifact.role,
                        "declared_relative_path": artifact.declared_relative_path,
                        "artifact_kind": artifact.artifact_kind,
                        "media_type": artifact.media_type,
                        "version_ref": artifact.version_ref,
                        "content_sha256": artifact.content_hash,
                        "tree_sha256": artifact.tree_hash,
                        "byte_count": len(artifact.content),
                    }
                )
            entries.append(
                {
                    "kind": "target_commit",
                    "target_commit_ref": accepted.target_commit_ref,
                    "target_ref": accepted.target_ref,
                    "target_run_ref": accepted.target_run_ref,
                    "manifest_ref": accepted.manifest_ref,
                    "manifest_payload_hash": accepted.manifest_payload_hash,
                    "manifest_receipt_ref": accepted.manifest_receipt_ref,
                    "manifest_relative_path": manifest_path,
                    "artifacts": artifact_entries,
                }
            )
        return expected, entries

    @staticmethod
    def _validate_target_commit_inputs(
        *,
        handle: TargetWorkHandle,
        accepted: tuple[FrozenTargetCommitInput, ...],
    ) -> None:
        if type(accepted) is not tuple or tuple(
            item.target_commit_ref for item in accepted
        ) != handle.accepted_input_target_commit_refs:
            raise OwnerConflict("target_root_upstream_input_invalid")
        for item in accepted:
            if type(item) is not FrozenTargetCommitInput:
                raise OwnerConflict("target_root_upstream_input_invalid")
            try:
                manifest_value = json.loads(item.manifest_content)
            except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise OwnerConflict("target_root_upstream_input_invalid") from error
            if (
                not item.artifacts
                or any(
                    type(value) is not str or not value
                    for value in (
                        item.target_commit_ref,
                        item.target_ref,
                        item.target_run_ref,
                        item.manifest_ref,
                        item.manifest_payload_hash,
                        item.manifest_receipt_ref,
                    )
                )
                or len(item.manifest_payload_hash) != 64
                or type(item.manifest_content) is not bytes
                or canonical_json(manifest_value).encode("utf-8")
                != item.manifest_content
                or any(
                    type(artifact) is not FrozenTargetCommitInputArtifact
                    or hashlib.sha256(artifact.content).hexdigest()
                    != artifact.content_hash
                    for artifact in item.artifacts
                )
            ):
                raise OwnerConflict("target_root_upstream_input_invalid")

    @staticmethod
    def _ensure_input_parent_directories(inputs: Path, parent: Path) -> None:
        relative = parent.relative_to(inputs)
        current = inputs
        for part in relative.parts:
            current = current / part
            if current.exists() and (current.is_symlink() or not current.is_dir()):
                raise OwnerConflict("target_run_workspace_input_integrity_invalid")
            current.mkdir(mode=0o700, exist_ok=True)
            current.chmod(0o700)

    def _ensure_frozen_input_root(self, workspace: TargetRunWorkspace) -> Path:
        workspace_root = self._workspace_root
        if workspace_root is None:
            raise OwnerConflict("target_run_workspace_unavailable")
        frozen_base = workspace_root.parent / "target-frozen-inputs"
        frozen_base.mkdir(mode=0o700, parents=True, exist_ok=True)
        if (
            frozen_base.is_symlink()
            or frozen_base.resolve().parent != workspace_root.parent
        ):
            raise OwnerConflict("target_run_workspace_input_integrity_invalid")
        frozen_root = frozen_base / canonical_hash(
            {"workspace_ref": workspace.workspace_ref}
        )
        if frozen_root.exists() and frozen_root.is_symlink():
            raise OwnerConflict("target_run_workspace_input_integrity_invalid")
        frozen_root.mkdir(mode=0o700, exist_ok=True)
        if frozen_root.resolve().parent != frozen_base.resolve():
            raise OwnerConflict("target_run_workspace_input_integrity_invalid")
        return frozen_root

    @classmethod
    def _write_target_input_tree(
        cls, *, root: Path, expected: dict[str, bytes]
    ) -> None:
        root.chmod(0o700)
        for relative_path, content in sorted(expected.items()):
            destination = root.joinpath(*relative_path.split("/"))
            cls._ensure_input_parent_directories(root, destination.parent)
            if destination.exists():
                if (
                    destination.is_symlink()
                    or not destination.is_file()
                    or destination.read_bytes() != content
                ):
                    raise OwnerConflict(
                        "target_run_workspace_input_integrity_invalid"
                    )
            else:
                destination.write_bytes(content)
            destination.chmod(0o400)

    @staticmethod
    def _lock_input_directories(inputs: Path) -> None:
        directories = [path for path in inputs.rglob("*") if path.is_dir()]
        for directory in sorted(
            directories, key=lambda path: len(path.parts), reverse=True
        ):
            if directory.is_symlink():
                raise OwnerConflict("target_run_workspace_input_integrity_invalid")
            directory.chmod(0o500)
        inputs.chmod(0o500)

    @staticmethod
    def _verify_target_workspace_input_tree(
        *, inputs: Path, expected: dict[str, bytes]
    ) -> None:
        if inputs.is_symlink() or not inputs.is_dir():
            raise OwnerConflict("target_run_workspace_input_integrity_invalid")
        actual: dict[str, bytes] = {}
        actual_directories: set[str] = set()
        for path in inputs.rglob("*"):
            if path.is_symlink():
                raise OwnerConflict("target_run_workspace_input_integrity_invalid")
            relative_path = path.relative_to(inputs).as_posix()
            if path.is_dir():
                actual_directories.add(relative_path)
                continue
            if not path.is_file():
                raise OwnerConflict("target_run_workspace_input_integrity_invalid")
            expected_content = expected.get(relative_path)
            try:
                before = path.lstat()
            except OSError as error:
                raise OwnerConflict(
                    "target_run_workspace_input_integrity_invalid"
                ) from error
            if expected_content is None or before.st_size != len(expected_content):
                raise OwnerConflict("target_run_workspace_input_integrity_invalid")
            first = path.read_bytes()
            second = path.read_bytes()
            try:
                after = path.lstat()
            except OSError as error:
                raise OwnerConflict(
                    "target_run_workspace_input_integrity_invalid"
                ) from error
            stable_identity = (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) == (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            if not stable_identity or first != second:
                raise OwnerConflict("target_run_workspace_input_integrity_invalid")
            actual[relative_path] = first
        expected_directories: set[str] = set()
        for relative_path in expected:
            parent = Path(relative_path).parent
            while parent != Path("."):
                expected_directories.add(parent.as_posix())
                parent = parent.parent
        if actual != expected or actual_directories != expected_directories:
            raise OwnerConflict("target_run_workspace_input_integrity_invalid")

    def freeze_target_workspace_implementation(
        self,
        *,
        handle: TargetWorkHandle,
        implementation_revision_ref: str,
        idempotency_key: str,
    ) -> tuple[
        AcceptedTargetImplementationBundle,
        AcceptedTargetImplementationBundleUsage,
    ]:
        """Post-turn daemon scan and RM acceptance of the actual code tree."""

        self.verify_current_target_run_handle(handle)
        if (
            not isinstance(idempotency_key, str)
            or not idempotency_key
            or len(idempotency_key) > 128
        ):
            raise OwnerConflict("target_implementation_workspace_invalid")
        workspace = self.query_target_workspace(handle.target_run_ref)
        if workspace is None:
            raise OwnerConflict("target_run_workspace_unavailable")
        root = self._ensure_target_workspace_layout(workspace)
        implementation = root / workspace.implementation_relative_path
        return self._memory._accept_owner_workspace_implementation(
            target_ref=handle.target_ref,
            implementation_revision_ref=implementation_revision_ref,
            source_directory=implementation,
            workspace_ref=workspace.workspace_ref,
            idempotency_key=idempotency_key,
        )

    def _ensure_target_workspace_layout(
        self, workspace: TargetRunWorkspace
    ) -> Path:
        root_base = self._workspace_root
        if root_base is None:
            raise OwnerConflict("target_run_workspace_unavailable")
        root_base.mkdir(parents=True, exist_ok=True, mode=0o700)
        if root_base.is_symlink():
            raise OwnerConflict("target_run_workspace_integrity_invalid")
        root_name = canonical_hash({"workspace_ref": workspace.workspace_ref})
        root = root_base / root_name
        if root.exists() and root.is_symlink():
            raise OwnerConflict("target_run_workspace_integrity_invalid")
        root.mkdir(mode=0o700, exist_ok=True)
        if root.resolve().parent != root_base:
            raise OwnerConflict("target_run_workspace_integrity_invalid")
        implementation = root / workspace.implementation_relative_path
        inputs = root / workspace.inputs_relative_path
        for directory in (implementation, inputs):
            if directory.exists() and directory.is_symlink():
                raise OwnerConflict("target_run_workspace_integrity_invalid")
            directory.mkdir(mode=0o700, exist_ok=True)
        implementation.chmod(0o700)
        inputs.chmod(0o500)
        return root

    def verify_current_target_run_handle(
        self, handle: TargetWorkHandle
    ) -> TargetWorkHandle:
        validate_target_work_handle(
            handle,
            target_ref=handle.target_ref,
            accepted_input_target_commit_refs=(
                handle.accepted_input_target_commit_refs
            ),
            accepted_input_asset_refs=tuple(
                proof.asset_ref for proof in handle.accepted_input_asset_proofs
            ),
        )
        run = self._harness.query_target_run_by_ref(handle.target_run_ref)
        input_binding = self._graph.query_execution_input_binding(
            handle.execution_input_binding_ref
        )
        with self._database.read() as connection:
            frontier_row = connection.execute(
                text(
                    "SELECT * FROM ar_target_frontier_entries WHERE target_ref = "
                    ":target_ref"
                ),
                {"target_ref": handle.target_ref},
            ).first()
            retired = connection.execute(
                text(
                    "SELECT identity_ref FROM ar_target_retired_identities WHERE "
                    "identity_ref IN (:root_ref, :attempt_ref, :fence_ref)"
                ),
                {
                    "root_ref": handle.root_session_ref,
                    "attempt_ref": handle.execution_attempt_ref,
                    "fence_ref": handle.execution_fence_ref,
                },
            ).all()
            if frontier_row is not None:
                root_table = connection.execute(
                    text(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                        "AND name = 'ar_target_root_lifecycles'"
                    )
                ).first()
                root_lifecycle = (
                    None
                    if root_table is None
                    else connection.execute(
                        text(
                            "SELECT * FROM ar_target_root_lifecycles WHERE "
                            "target_ref = :target_ref"
                        ),
                        {"target_ref": handle.target_ref},
                    ).first()
                )
                if root_lifecycle is None:
                    from meta_research.owners.agent_runtime import (
                        verify_current_target_run_frontier_in_transaction,
                    )

                    verify_current_target_run_frontier_in_transaction(
                        connection, handle
                    )
                elif (
                    root_lifecycle.status not in {"running", "finalizing"}
                    or root_lifecycle.target_run_ref != handle.target_run_ref
                    or root_lifecycle.root_session_ref != handle.root_session_ref
                    or root_lifecycle.target_attempt_ref
                    != handle.execution_attempt_ref
                    or root_lifecycle.target_fence_ref
                    != handle.execution_fence_ref
                    or frontier_row.state != "running"
                    or frontier_row.terminal_fact_ref is not None
                    or frontier_row.current_handle_json
                    != canonical_json(projection_plain_value(handle))
                    or frontier_row.current_handle_hash
                    != canonical_hash(projection_plain_value(handle))
                    or bool(frontier_row.currentness_known) is not True
                    or bool(frontier_row.current) is not True
                ):
                    raise OwnerConflict("target_run_frontier_not_current")
        run_identity = None if run is None else (
            run.run_ref,
            run.root_session_ref,
            run.attempt_ref,
            run.fence_ref,
        )
        handle_identity = (
            handle.target_run_ref,
            handle.root_session_ref,
            handle.execution_attempt_ref,
            handle.execution_fence_ref,
        )
        if run is None or input_binding is None or retired or (
            input_binding.target_ref,
            input_binding.target_run_ref,
            input_binding.target_attempt_ref,
            input_binding.target_fence_ref,
            input_binding.target_scope_binding_hash,
            input_binding.proof.acceptance_receipt,
        ) != (
            handle.target_ref,
            handle.target_run_ref,
            handle.execution_attempt_ref,
            handle.execution_fence_ref,
            run.request.get("target_scope_binding_hash"),
            handle.execution_input_binding_receipt,
        ) or run_identity != handle_identity or run.status not in {
            "admitted",
            "running",
            "executed",
        }:
            raise OwnerConflict("target_run_harness_identity_invalid")
        for proof in handle.accepted_input_asset_proofs:
            projection = self._graph.query_input_asset_projection(
                target_ref=handle.target_ref, asset_ref=proof.asset_ref
            )
            if projection is None or projection.as_bundle_proof() != proof:
                raise OwnerConflict("target_run_input_asset_proof_invalid")
        return handle

    def verify_retiring_failed_handle(
        self, handle: TargetWorkHandle
    ) -> TargetWorkHandle:
        """Read-only authority for a signed terminal on recorded AR history.

        This seam is intentionally not wired to execution admission or start;
        it can only support re-reading an immutable already-created terminal.
        """

        verify_history = getattr(
            self._execution_verifier, "verify_target_run_history_handle", None
        )
        if not callable(verify_history) or verify_history(handle) != handle:
            raise OwnerConflict("target_run_history_handle_invalid")
        binding = self._graph.query_execution_input_binding(
            handle.execution_input_binding_ref
        )
        if binding is None or (
            binding.target_ref,
            binding.target_run_ref,
            binding.target_attempt_ref,
            binding.target_fence_ref,
            binding.proof.acceptance_receipt,
        ) != (
            handle.target_ref,
            handle.target_run_ref,
            handle.execution_attempt_ref,
            handle.execution_fence_ref,
            handle.execution_input_binding_receipt,
        ):
            raise OwnerConflict("target_run_history_handle_invalid")
        return handle

    def verify_target_run_recovery_successor(
        self,
        old_handle: TargetWorkHandle,
        replacement_handle: TargetWorkHandle,
        recovery_ref: str,
    ) -> TargetWorkHandle:
        """Verify a Harness successor while AR still owns the old frontier."""

        validate_target_work_handle(
            replacement_handle,
            target_ref=old_handle.target_ref,
            accepted_input_target_commit_refs=(
                old_handle.accepted_input_target_commit_refs
            ),
            accepted_input_asset_refs=tuple(
                proof.asset_ref for proof in old_handle.accepted_input_asset_proofs
            ),
        )
        if (
            replacement_handle.target_run_ref != old_handle.target_run_ref
            or replacement_handle.accepted_input_target_commit_refs
            != old_handle.accepted_input_target_commit_refs
            or replacement_handle.accepted_input_asset_proofs
            != old_handle.accepted_input_asset_proofs
            or replacement_handle.root_session_ref == old_handle.root_session_ref
            or replacement_handle.execution_attempt_ref
            == old_handle.execution_attempt_ref
            or replacement_handle.execution_fence_ref
            == old_handle.execution_fence_ref
        ):
            raise OwnerConflict("target_run_recovery_successor_invalid")
        run = self._harness.query_target_run_by_ref(old_handle.target_run_ref)
        try:
            reservation = self._harness.verify_target_successor_reservation(
                old_handle=old_handle,
                recovery_ref=recovery_ref,
            )
        except Exception as error:
            raise OwnerConflict(
                "target_run_recovery_reservation_invalid"
            ) from error
        binding = self._graph.query_execution_input_binding(
            replacement_handle.execution_input_binding_ref
        )
        query_launch = getattr(
            self._execution_verifier, "query_admitted_target_launch", None
        )
        launch = None if not callable(query_launch) else query_launch(
            old_handle.target_ref
        )
        expected_input_refs = tuple(
            sorted(
                (
                    *old_handle.accepted_input_target_commit_refs,
                    *(proof.asset_ref for proof in old_handle.accepted_input_asset_proofs),
                )
            )
        )
        with self._database.read() as connection:
            from meta_research.owners.agent_runtime import (
                verify_current_target_run_frontier_in_transaction,
            )

            verify_current_target_run_frontier_in_transaction(
                connection, old_handle
            )
            retired = {
                row.identity_ref
                for row in connection.execute(
                    text("SELECT identity_ref FROM ar_target_retired_identities")
                ).all()
            }
        if run is None or binding is None or launch is None or (
            run.run_ref,
            run.root_session_ref,
            run.attempt_ref,
            run.fence_ref,
            run.status,
            reservation.new_root_session_ref,
            reservation.new_attempt_ref,
            reservation.new_fence_ref,
            binding.target_ref,
            binding.target_run_ref,
            binding.target_attempt_ref,
            binding.target_fence_ref,
            binding.target_spec_hash,
            binding.target_scope_binding_hash,
            binding.proof.subject_ref,
            binding.proof.input_refs,
            binding.proof.acceptance_receipt,
        ) != (
            replacement_handle.target_run_ref,
            replacement_handle.root_session_ref,
            replacement_handle.execution_attempt_ref,
            replacement_handle.execution_fence_ref,
            "admitted",
            replacement_handle.root_session_ref,
            replacement_handle.execution_attempt_ref,
            replacement_handle.execution_fence_ref,
            replacement_handle.target_ref,
            replacement_handle.target_run_ref,
            replacement_handle.execution_attempt_ref,
            replacement_handle.execution_fence_ref,
            launch.request.target_spec_binding.content_hash_ref,
            run.request.get("target_scope_binding_hash"),
            replacement_handle.execution_attempt_ref,
            expected_input_refs,
            replacement_handle.execution_input_binding_receipt,
        ) or any(
            value in retired
            for value in (
                replacement_handle.root_session_ref,
                replacement_handle.execution_attempt_ref,
                replacement_handle.execution_fence_ref,
            )
        ):
            raise OwnerConflict("target_run_recovery_successor_invalid")
        return replacement_handle

    def query_target_execution_failure(
        self,
        handle: TargetWorkHandle,
    ) -> TargetExecutionFailureProjection | None:
        """Project a blocker only after its exact successor is ready.

        The immutable signed failure basis may be observed before successor
        reservation.  It is deliberately not exposed as a mutable
        ``TechnicalBlocker``: the formal blocker identity is constructed once,
        after Harness admission and the successor input receipt both exist.
        """

        basis = self._query_target_execution_failure_basis(handle)
        if basis is None:
            return None
        run = self._harness.query_target_run_by_ref(handle.target_run_ref)
        replacement_handle: TargetWorkHandle | None = None
        if run is not None and (
            run.root_session_ref,
            run.attempt_ref,
            run.fence_ref,
        ) != (
            handle.root_session_ref,
            handle.execution_attempt_ref,
            handle.execution_fence_ref,
        ) and run.status == "admitted":
            binding = self._graph.query_execution_input_binding_for_attempt(
                target_ref=handle.target_ref,
                target_run_ref=handle.target_run_ref,
                target_attempt_ref=run.attempt_ref,
                target_fence_ref=run.fence_ref,
            )
            if binding is not None:
                replacement_handle = TargetWorkHandle(
                    target_ref=handle.target_ref,
                    target_run_ref=handle.target_run_ref,
                    root_session_ref=run.root_session_ref,
                    execution_attempt_ref=run.attempt_ref,
                    execution_fence_ref=run.fence_ref,
                    execution_input_binding_ref=binding.proof.binding_ref,
                    execution_input_binding_receipt=(
                        binding.proof.acceptance_receipt
                    ),
                    accepted_input_target_commit_refs=(
                        handle.accepted_input_target_commit_refs
                    ),
                    accepted_input_asset_proofs=(
                        handle.accepted_input_asset_proofs
                    ),
                    recoverable=handle.recoverable,
                )
                self.verify_target_run_recovery_successor(
                    handle, replacement_handle, basis.blocker_ref
                )
        blocker = (
            self._canonical_recovery_blocker(handle, basis)
            if replacement_handle is not None
            else None
        )
        return TargetExecutionFailureProjection(
            operation=basis.recovered.operation,
            request_sha256=basis.recovered.request_sha256,
            exit_receipt=basis.recovered.terminal_result.exit_receipt,
            generic_binding_ref=basis.generic_binding.binding_ref,
            generic_binding_receipt=basis.generic_binding.receipt,
            failure_ref=basis.blocker_ref,
            blocker=blocker,
            stop_decision=basis.stop_decision,
            replacement_handle=replacement_handle,
        )

    def query_target_execution_operation_state(
        self, handle: TargetWorkHandle
    ) -> RecoveredTargetOperationState:
        """Read the exhaustive current port state through Owner authority."""


        self.verify_current_target_run_handle(handle)
        state = self._graph.query_generic_operation_state_for_handle(handle)
        if state.handle != handle:
            raise OwnerConflict("target_execution_operation_recovery_invalid")
        return state

    def query_target_execution_outcome_unknown(
        self, handle: TargetWorkHandle
    ) -> TargetExecutionOutcomeUnknownProjection | None:
        """Project only a signed, stable unknown-outcome fact."""


        state = self.query_target_execution_operation_state(handle)
        if state.status is not TargetOperationRecoveryStatus.OUTCOME_UNKNOWN:
            return None
        fact = state.outcome_unknown
        if fact is None or fact.handle != handle:
            raise OwnerConflict("target_execution_outcome_unknown_invalid")
        blocker = self._canonical_outcome_unknown_blocker(handle, fact)
        # Re-read once more before exposing a formal terminal projection.  The
        # inventory receipt can acquire a fresh observation timestamp, but its
        # stable signed fact identity and all trusted bindings may not drift.
        rechecked = self._graph.query_generic_operation_state_for_handle(handle)
        current_fact = rechecked.outcome_unknown
        if (
            rechecked.status is not TargetOperationRecoveryStatus.OUTCOME_UNKNOWN
            or current_fact is None
            or (
                current_fact.fact_ref,
                current_fact.fact_sha256,
                current_fact.handle,
                current_fact.measurement_authority,
            )
            != (
                fact.fact_ref,
                fact.fact_sha256,
                fact.handle,
                fact.measurement_authority,
            )
        ):
            raise OwnerConflict("target_execution_outcome_unknown_stale")
        return TargetExecutionOutcomeUnknownProjection(
            fact=current_fact,
            blocker=blocker,
        )

    def verify_target_execution_terminal_blocker(
        self,
        *,
        handle: TargetWorkHandle,
        blocker: TechnicalBlocker,
    ) -> TechnicalBlocker:
        """Reissue an outcome-unknown blocker from the signed port fact."""

        frontier = self._execution_verifier.query_target_frontier_entry(
            handle.target_ref
        )
        if frontier is not None and frontier.state == "terminal":
            if (
                frontier.current_handle != handle
                or frontier.terminal_fact_ref != blocker.blocker_ref
            ):
                raise OwnerConflict("target_execution_outcome_unknown_invalid")
            self.verify_retiring_failed_handle(handle)
            fact = self._graph.recover_historical_generic_operation_outcome_unknown(
                handle
            )
            if fact is None:
                projection = None
            else:
                projection = TargetExecutionOutcomeUnknownProjection(
                    fact=fact,
                    blocker=self._canonical_outcome_unknown_blocker(handle, fact),
                )
                self.verify_retiring_failed_handle(handle)
                rechecked = (
                    self._graph.recover_historical_generic_operation_outcome_unknown(
                        handle
                    )
                )
                if rechecked is None or (
                    rechecked.fact_ref,
                    rechecked.fact_sha256,
                    rechecked.handle,
                    rechecked.measurement_authority,
                ) != (
                    fact.fact_ref,
                    fact.fact_sha256,
                    fact.handle,
                    fact.measurement_authority,
                ):
                    raise OwnerConflict(
                        "target_execution_outcome_unknown_stale"
                    )
        else:
            projection = self.query_target_execution_outcome_unknown(handle)
        if projection is None or projection.blocker != blocker:
            raise OwnerConflict("target_execution_outcome_unknown_invalid")
        return projection.blocker

    @staticmethod
    def _canonical_outcome_unknown_blocker(
        handle: TargetWorkHandle,
        fact: TargetExecutionOutcomeUnknownFact,
    ) -> TechnicalBlocker:
        blocker_ref = "target-outcome-unknown:" + fact.fact_sha256
        base = TechnicalBlocker(
            target_ref=handle.target_ref,
            target_run_ref=handle.target_run_ref,
            execution_attempt_ref=handle.execution_attempt_ref,
            execution_fence_ref=handle.execution_fence_ref,
            blocker_ref=blocker_ref,
            blocker_receipt=ReceiptProof(
                receipt_ref="target-unknown-blocker-receipt:" + fact.fact_sha256,
                subject_ref=blocker_ref,
                verified=True,
                currentness_known=True,
                current=True,
            ),
            reason="target_execution_outcome_unknown",
            recovery_ready=False,
            old_session_fenced=False,
            recovery_pack_complete=False,
            recovery_receipt=None,
            bundle_decision_required=True,
            escalation_scope="authority",
            pending_obligation_refs=(
                "target-outcome-resolution:" + fact.fact_sha256,
            ),
        )
        escalation_hash = _bundle_escalation_digest(base)
        return replace(
            base,
            escalation_evidence=ContentBindingProof(
                subject_ref=fact.fact_ref,
                content_hash_ref=escalation_hash,
            ),
            escalation_receipt=ReceiptProof(
                receipt_ref=(
                    "target-unknown-escalation-receipt:" + fact.fact_sha256
                ),
                subject_ref=escalation_hash,
                verified=True,
                currentness_known=True,
                current=True,
            ),
        )

    def verify_target_execution_recovery_history(
        self,
        *,
        old_handle: TargetWorkHandle,
        replacement_handle: TargetWorkHandle,
        blocker: TechnicalBlocker,
        stop_decision: StopDecisionProof | None,
        generic_binding_ref: str,
        generic_binding_receipt_ref: str,
        generic_binding_receipt_hash: str,
        successor_reservation: AgentRuntimeTargetSuccessorReservation,
    ) -> TargetExecutionFailureProjection:
        """Reverify one persisted recovery against its signed terminal."""

        basis = self._query_target_execution_failure_basis(old_handle)
        if basis is None:
            raise OwnerConflict("target_execution_recovery_history_invalid")
        expected_blocker = self._canonical_recovery_blocker(old_handle, basis)
        try:
            reservation = (
                self._harness.verify_target_successor_reservation_evidence(
                    successor_reservation,
                    old_handle=old_handle,
                    recovery_ref=basis.blocker_ref,
                )
            )
        except Exception as error:
            raise OwnerConflict(
                "target_execution_recovery_history_invalid"
            ) from error
        if (
            blocker != expected_blocker
            or stop_decision != basis.stop_decision
            or generic_binding_ref != basis.generic_binding.binding_ref
            or generic_binding_receipt_ref
            != basis.generic_binding.receipt.receipt_ref
            or generic_binding_receipt_hash
            != basis.generic_binding.receipt.payload_hash
            or (
                reservation.new_root_session_ref,
                reservation.new_attempt_ref,
                reservation.new_fence_ref,
            )
            != (
                replacement_handle.root_session_ref,
                replacement_handle.execution_attempt_ref,
                replacement_handle.execution_fence_ref,
            )
            or replacement_handle.target_ref != old_handle.target_ref
            or replacement_handle.target_run_ref != old_handle.target_run_ref
            or replacement_handle.accepted_input_target_commit_refs
            != old_handle.accepted_input_target_commit_refs
            or replacement_handle.accepted_input_asset_proofs
            != old_handle.accepted_input_asset_proofs
        ):
            raise OwnerConflict("target_execution_recovery_history_invalid")
        replacement_binding = self._graph.query_execution_input_binding(
            replacement_handle.execution_input_binding_ref
        )
        if replacement_binding is None or (
            replacement_binding.target_ref,
            replacement_binding.target_run_ref,
            replacement_binding.target_attempt_ref,
            replacement_binding.target_fence_ref,
            replacement_binding.proof.acceptance_receipt,
        ) != (
            replacement_handle.target_ref,
            replacement_handle.target_run_ref,
            replacement_handle.execution_attempt_ref,
            replacement_handle.execution_fence_ref,
            replacement_handle.execution_input_binding_receipt,
        ):
            raise OwnerConflict("target_execution_recovery_history_invalid")
        return TargetExecutionFailureProjection(
            operation=basis.recovered.operation,
            request_sha256=basis.recovered.request_sha256,
            exit_receipt=basis.recovered.terminal_result.exit_receipt,
            generic_binding_ref=basis.generic_binding.binding_ref,
            generic_binding_receipt=basis.generic_binding.receipt,
            failure_ref=basis.blocker_ref,
            blocker=expected_blocker,
            stop_decision=basis.stop_decision,
            replacement_handle=replacement_handle,
        )

    def verify_target_execution_stop_history(
        self,
        *,
        handle: TargetWorkHandle,
        stop_decision: StopDecisionProof,
        generic_binding_ref: str,
        generic_binding_receipt_ref: str,
        generic_binding_receipt_hash: str,
    ) -> StopDecisionProof:
        """Reverify one accepted stop against the exact signed stopped exit."""

        basis = self._query_target_execution_failure_basis(handle)
        if basis is None or basis.stop_decision is None or (
            basis.stop_decision != stop_decision
            or basis.generic_binding.binding_ref != generic_binding_ref
            or basis.generic_binding.receipt.receipt_ref
            != generic_binding_receipt_ref
            or basis.generic_binding.receipt.payload_hash
            != generic_binding_receipt_hash
        ):
            raise OwnerConflict("target_stop_decision_authority_invalid")
        return basis.stop_decision

    def _query_target_execution_failure_basis(
        self, handle: TargetWorkHandle
    ) -> _TargetExecutionFailureBasis | None:
        generic_binding = (
            self._graph.query_generic_execution_binding_for_retiring_handle(
                handle
            )
        )
        if generic_binding is None:
            return None
        terminal_facts = self._graph.query_generic_execution_terminal(
            generic_binding.binding_ref,
            retiring_handle=handle,
        )
        if terminal_facts is None:
            raise OwnerConflict("target_execution_failure_invalid")
        verified_binding, request, terminal, _input_binding = terminal_facts
        if verified_binding != generic_binding:
            raise OwnerConflict("target_execution_failure_invalid")
        recovered = RecoveredTargetOperation(
            operation=TargetOperationHandle(generic_binding.operation_handle),
            request=request,
            request_sha256=generic_binding.request_hash,
            terminal_result=terminal,
        )
        exit_receipt = terminal.exit_receipt
        if (
            exit_receipt.status == "succeeded"
            and exit_receipt.process_tree_drained is True
        ):
            return None
        if (
            exit_receipt.status not in {"failed", "stopped", "timed_out"}
            or exit_receipt.process_tree_drained is not True
        ):
            raise OwnerConflict("target_execution_failure_invalid")
        digest = canonical_hash(exit_receipt.as_dict())
        blocker_ref = "target-blocker:" + digest
        reason_by_terminal = {
            ("failed", "output_limit"): "target_execution_output_limit",
            ("failed", "descendant_process"): (
                "target_execution_descendant_process"
            ),
            ("failed", "launch_failed"): "target_execution_launch_failed",
            ("timed_out", "timeout"): "target_execution_timed_out",
            ("stopped", "stopped"): "target_execution_stopped",
        }
        reason = reason_by_terminal.get(
            (exit_receipt.status, exit_receipt.termination_reason),
            "target_execution_failed",
        )
        stop = None
        if exit_receipt.status == "stopped":
            decision_ref = "target-stop:" + digest
            stop = StopDecisionProof(
                stop_basis="engineering_anomaly",
                decision_ref=decision_ref,
                target_ref=handle.target_ref,
                target_run_ref=handle.target_run_ref,
                execution_attempt_ref=handle.execution_attempt_ref,
                frozen_rule_ref=None,
                protocol_version_ref=None,
                termination_receipt=ReceiptProof(
                    receipt_ref="target-stop-receipt:" + digest,
                    subject_ref=decision_ref,
                    verified=True,
                    currentness_known=True,
                    current=True,
                ),
                process_tree_drained=True,
            )
        return _TargetExecutionFailureBasis(
            recovered=recovered,
            generic_binding=generic_binding,
            blocker_ref=blocker_ref,
            blocker_reason=reason,
            stop_decision=stop,
        )

    @staticmethod
    def _canonical_recovery_blocker(
        handle: TargetWorkHandle,
        basis: _TargetExecutionFailureBasis,
    ) -> TechnicalBlocker:
        digest = basis.blocker_ref.removeprefix("target-blocker:")
        return TechnicalBlocker(
            target_ref=handle.target_ref,
            target_run_ref=handle.target_run_ref,
            execution_attempt_ref=handle.execution_attempt_ref,
            execution_fence_ref=handle.execution_fence_ref,
            blocker_ref=basis.blocker_ref,
            blocker_receipt=ReceiptProof(
                receipt_ref="target-blocker-receipt:" + digest,
                subject_ref=basis.blocker_ref,
                verified=True,
                currentness_known=True,
                current=True,
            ),
            reason=basis.blocker_reason,
            recovery_ready=True,
            old_session_fenced=True,
            recovery_pack_complete=True,
            recovery_receipt=ReceiptProof(
                receipt_ref="target-recovery-receipt:" + digest,
                subject_ref=basis.blocker_ref,
                verified=True,
                currentness_known=True,
                current=True,
            ),
        )

    def reserve_target_execution_recovery(
        self,
        handle: TargetWorkHandle,
    ) -> TargetHarnessAdmission:
        """Reserve one successor only for a reverified signed failure."""

        failure = self.query_target_execution_failure(handle)
        if failure is None or failure.replacement_handle is not None:
            raise OwnerConflict("target_execution_recovery_invalid")
        try:
            self._harness.reserve_target_successor(
                old_handle=handle,
                recovery_ref=failure.failure_ref,
            )
        except Exception as error:
            code = getattr(error, "code", "target_harness_recovery_invalid")
            raise OwnerConflict(str(code)) from error
        successor = self.query_target_harness_admission(handle.target_ref)
        if successor is None:
            raise OwnerConflict("target_harness_recovery_invalid")
        return successor

    def query_target_harness_admission(
        self, target_ref: str
    ) -> TargetHarnessAdmission | None:
        """Read the independent Harness root without allocating identities.

        This projection is deliberately usable before AR activation.  The
        Target daemon needs to distinguish an admitted launch from an already
        admitted Harness root after a crash, while Bundle must never inspect
        the native Session itself.
        """

        with self._database.read() as connection:
            launch = connection.execute(
                text(
                    "SELECT target_run_ref FROM ar_target_launches WHERE "
                    "target_ref = :target_ref"
                ),
                {"target_ref": target_ref},
            ).first()
        if launch is None:
            return None
        run = self._harness.query_target_run_by_ref(launch.target_run_ref)
        if run is None:
            return None
        request = run.request
        full_conformance = request.get("full_conformance_binding")
        full_hash = request.get("full_conformance_binding_hash")
        scope_hash = request.get("target_scope_binding_hash")
        if (
            request.get("target_ref") != target_ref
            or request.get("target_run_ref") != launch.target_run_ref
            or not isinstance(full_conformance, dict)
            or not isinstance(full_hash, str)
            or canonical_hash(full_conformance) != full_hash
            or not isinstance(scope_hash, str)
            or len(scope_hash) != 64
        ):
            raise OwnerConflict("target_harness_admission_integrity_invalid")
        return TargetHarnessAdmission(
            target_ref=target_ref,
            target_run_ref=run.run_ref,
            harness_request_ref=run.request_ref,
            harness_family=run.harness_family,
            model_ref=run.model_ref,
            auth_profile_ref=run.auth_profile_ref,
            root_session_ref=run.root_session_ref,
            execution_attempt_ref=run.attempt_ref,
            execution_fence_ref=run.fence_ref,
            native_session_ref=run.native_session_ref,
            capability_binding_hash=run.capability_binding_hash,
            full_conformance_binding_hash=full_hash,
            status=run.status,
            failure_code=run.failure_code,
        )

    def query_pending_target_review_session(
        self,
        *,
        target_run_ref: str,
        review_kind: str,
    ) -> AgentRuntimeTargetChildSession | None:
        """Return the single reserved/bound review operation, if any."""

        if review_kind not in {"code", "result"}:
            raise OwnerConflict("target_review_kind_invalid")
        with self._database.read() as connection:
            rows = connection.execute(
                text(
                    "SELECT harness_operation_ref FROM "
                    "ar_target_harness_child_sessions WHERE target_run_ref = "
                    ":target_run_ref AND review_kind = :review_kind ORDER BY "
                    "reserved_at DESC"
                ),
                {
                    "target_run_ref": target_run_ref,
                    "review_kind": review_kind,
                },
            ).all()
        if not rows:
            return None
        # One code review is allowed per revision/recovery attempt.  Until a
        # recovery authority exposes the replacement revision, multiple
        # unretired reservations are an integrity error rather than a choice.
        sessions = tuple(
            self._harness.query_target_child_session(row.harness_operation_ref)
            for row in rows
        )
        active = tuple(session for session in sessions if session is not None)
        if len(active) != 1:
            raise OwnerConflict("target_review_session_integrity_invalid")
        return active[0]

    def query_current_target_work_handle(
        self, target_ref: str
    ) -> TargetWorkHandle | None:
        """Reconstruct the exact current handle from issuer-owned facts."""

        with self._database.read() as connection:
            frontier = connection.execute(
                text(
                    "SELECT state, current_handle_json, current_handle_hash FROM "
                    "ar_target_frontier_entries WHERE target_ref = :target_ref"
                ),
                {"target_ref": target_ref},
            ).first()
        if frontier is not None:
            try:
                current = _decode_stored_record(
                    frontier.current_handle_json,
                    frontier.current_handle_hash,
                    TargetWorkHandle,
                )
            except (TypeError, ValueError) as error:
                raise OwnerConflict("target_run_handle_integrity_invalid") from error
            if type(current) is not TargetWorkHandle:
                raise OwnerConflict("target_run_handle_integrity_invalid")
            if frontier.state == "terminal":
                return current
            return self.verify_current_target_run_handle(current)

        admission = self.query_target_harness_admission(target_ref)
        if admission is None:
            return None
        binding = self._graph.query_execution_input_binding_for_target_run(
            target_ref=target_ref,
            target_run_ref=admission.target_run_ref,
        )
        if binding is None:
            return None
        with self._database.read() as connection:
            launch = connection.execute(
                text(
                    "SELECT accepted_input_target_commit_refs_json, "
                    "accepted_input_asset_refs_json FROM ar_target_launches "
                    "WHERE target_ref = :target_ref"
                ),
                {"target_ref": target_ref},
            ).one()
        try:
            commit_refs = tuple(
                json.loads(launch.accepted_input_target_commit_refs_json)
            )
            asset_refs = tuple(json.loads(launch.accepted_input_asset_refs_json))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise OwnerConflict("target_run_handle_integrity_invalid") from error
        asset_proofs = []
        for asset_ref in asset_refs:
            projection = self._graph.query_input_asset_projection(
                target_ref=target_ref,
                asset_ref=asset_ref,
            )
            if projection is None:
                raise OwnerConflict("target_run_input_asset_proof_invalid")
            asset_proofs.append(projection.as_bundle_proof())
        handle = TargetWorkHandle(
            target_ref=target_ref,
            target_run_ref=admission.target_run_ref,
            root_session_ref=admission.root_session_ref,
            execution_attempt_ref=admission.execution_attempt_ref,
            execution_fence_ref=admission.execution_fence_ref,
            execution_input_binding_ref=binding.proof.binding_ref,
            execution_input_binding_receipt=(
                binding.proof.acceptance_receipt
            ),
            accepted_input_target_commit_refs=commit_refs,
            accepted_input_asset_proofs=tuple(asset_proofs),
            recoverable=True,
        )
        return self.verify_current_target_run_handle(handle)

    def verify_target_semantic_context(
        self,
        *,
        target_ref: str,
        run_ref: str,
        attempt_ref: str,
        root_session_ref: str,
        fence_ref: str,
        capability_binding_hash: str,
    ) -> TargetCandidate:
        """Resolve one TARGET_RUN MCP call to its canonical candidate.

        This gate is usable before AR activation because implementation and
        self-check happen inside the independently admitted Target root.  It
        nevertheless re-reads the exact Harness Target admission and RG
        candidate projection on every call.
        """

        run = self._harness.query_target_run_by_ref(run_ref)
        projection = self._graph.query_candidate_projection(target_ref)
        if run is None or projection is None or (
            run.run_ref,
            run.attempt_ref,
            run.root_session_ref,
            run.fence_ref,
            run.capability_binding_hash,
            run.request.get("target_ref"),
            run.request.get("target_run_ref"),
        ) != (
            run_ref,
            attempt_ref,
            root_session_ref,
            fence_ref,
            capability_binding_hash,
            target_ref,
            run_ref,
        ) or run.status not in {"admitted", "running", "executed"}:
            raise OwnerConflict("target_semantic_context_invalid")
        return projection.candidate

    def verify_current_target_run_quest(
        self,
        *,
        handle: TargetWorkHandle,
        quest_ref: str,
    ) -> None:
        """Bind a generic execution request to the admitted Quest.

        ``execution_request_ref`` is only a correlation identity owned by the
        generic port's signed invocation.  The Quest itself is reread from the
        admitted Target launch and therefore is never accepted from caller
        self-consistency.
        """

        self.verify_current_target_run_handle(handle)
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT quest_ref, target_run_ref FROM ar_target_launches "
                    "WHERE target_ref = :target_ref"
                ),
                {"target_ref": handle.target_ref},
            ).first()
        if row is None or (row.quest_ref, row.target_run_ref) != (
            quest_ref,
            handle.target_run_ref,
        ):
            raise OwnerConflict("target_execution_quest_invalid")

    def verify_current_target_run_scope(
        self,
        *,
        handle: TargetWorkHandle,
        candidate: TargetCandidate,
        formal_plan: FormalPlan,
    ) -> None:
        self.verify_current_target_run_handle(handle)
        with self._database.read() as connection:
            launch = connection.execute(
                text(
                    "SELECT target_spec_content_hash_ref, "
                    "accepted_input_target_commit_refs_json, "
                    "accepted_input_asset_refs_json FROM ar_target_launches "
                    "WHERE target_ref = :target_ref"
                ),
                {"target_ref": handle.target_ref},
            ).first()
        if launch is None:
            raise OwnerConflict("target_scope_binding_invalid")
        try:
            commit_refs = tuple(
                json.loads(launch.accepted_input_target_commit_refs_json)
            )
            asset_refs = tuple(json.loads(launch.accepted_input_asset_refs_json))
        except (TypeError, ValueError) as error:
            raise OwnerConflict("target_scope_binding_invalid") from error
        scope = canonical_target_scope_binding(
            target_ref=handle.target_ref,
            target_run_ref=handle.target_run_ref,
            target_spec_hash=launch.target_spec_content_hash_ref,
            candidate=candidate,
            formal_plan=formal_plan,
            accepted_input_refs=tuple(sorted((*commit_refs, *asset_refs))),
        )
        run = self._harness.query_target_run_by_ref(handle.target_run_ref)
        binding = self._graph.query_execution_input_binding(
            handle.execution_input_binding_ref
        )
        scope_hash = canonical_hash(scope)
        if (
            run is None
            or binding is None
            or run.request.get("target_scope_binding_hash") != scope_hash
            or binding.target_scope_binding_hash != scope_hash
        ):
            raise OwnerConflict("target_scope_binding_invalid")

    def accept_code_review(
        self,
        *,
        handle: TargetWorkHandle,
        review: CodeReviewRecord,
        review_scope: object,
        harness_operation_ref: str,
        candidate_ready_evidence_ref: str,
        self_check_evidence_refs: tuple[str, ...],
        idempotency_key: str,
    ) -> AcceptedTargetCodeReview:
        # The fixed contract has two distinct hashes here.  The validator
        # returns the closed CodeReviewRecord digest, while the protected
        # execution preflight binds the review *and* its complete review
        # scope.  Do not collapse those two subjects into one self-consistency
        # check.
        validate_code_review(
            review,
            implementation_revision_ref=review.candidate_revision_ref,
            target_root_session_ref=handle.root_session_ref,
        )
        if not review.code_changed:
            self._verify_root_implementation_evidence(
                handle=handle,
                operation_ref=harness_operation_ref,
                implementation_revision_ref=review.candidate_revision_ref,
                candidate_ready_evidence_ref=candidate_ready_evidence_ref,
                self_check_evidence_refs=self_check_evidence_refs,
                child_spawn_evidence_ref=None,
                review_evidence=None,
            )
            return AcceptedTargetCodeReview(
                target_ref=handle.target_ref,
                target_run_ref=handle.target_run_ref,
                harness_operation_ref=harness_operation_ref,
                reviewer_completion_evidence_ref="",
                review=review,
                evidence_binding=None,
                evidence_receipt=None,
            )
        evidence = self._review_evidence(
            handle=handle,
            operation_ref=harness_operation_ref,
            review_kind="code",
            expected_review=projection_plain_value(review),
            expected_scope=projection_plain_value(review_scope),
        )
        self._verify_root_implementation_evidence(
            handle=handle,
            operation_ref=harness_operation_ref,
            implementation_revision_ref=review.candidate_revision_ref,
            candidate_ready_evidence_ref=candidate_ready_evidence_ref,
            self_check_evidence_refs=self_check_evidence_refs,
            child_spawn_evidence_ref=str(evidence["spawn_evidence_ref"]),
            review_evidence=evidence,
        )
        evidence_content = {
            "review": projection_plain_value(review),
            "complete_review_scope": projection_plain_value(review_scope),
        }
        evidence_content_hash = canonical_hash(evidence_content)
        payload = {
            **evidence_content,
            "candidate_ready_evidence_ref": candidate_ready_evidence_ref,
            "self_check_evidence_refs": list(self_check_evidence_refs),
        }
        payload_hash = canonical_hash(payload)
        accepted = self._accept_review_row(
            handle=handle,
            review_kind="code",
            subject_ref=review.candidate_revision_ref,
            harness_operation_ref=harness_operation_ref,
            evidence=evidence,
            payload=payload,
            payload_hash=payload_hash,
            evidence_content_hash=evidence_content_hash,
            idempotency_key=idempotency_key,
        )
        return AcceptedTargetCodeReview(
            target_ref=handle.target_ref,
            target_run_ref=handle.target_run_ref,
            harness_operation_ref=harness_operation_ref,
            reviewer_completion_evidence_ref=str(
                evidence["completion_evidence_ref"]
            ),
            review=review,
            evidence_binding=ContentBindingProof(
                subject_ref=review.review_ref or "",
                content_hash_ref=evidence_content_hash,
            ),
            evidence_receipt=_proof(accepted),
            review_scope=review_scope,
            candidate_ready_evidence_ref=candidate_ready_evidence_ref,
            self_check_evidence_refs=self_check_evidence_refs,
        )

    def query_target_review_turn_evidence(
        self,
        *,
        handle: TargetWorkHandle,
        harness_operation_ref: str,
        review_kind: str,
    ) -> TargetReviewTurnEvidence | None:
        """Read one completed native review turn for the post-turn daemon.

        This is a read-only discovery seam.  It does not bind the reserved
        domain child Session or accept a review; the corresponding acceptance
        method repeats the complete Harness/ledger verification before its
        first write.
        """

        if review_kind not in {"code", "result"}:
            raise OwnerConflict("target_review_kind_invalid")
        self.verify_current_target_run_handle(handle)
        reservation = self._harness.query_target_child_session(
            harness_operation_ref
        )
        if reservation is None:
            return None
        if (
            reservation.target_run_ref != handle.target_run_ref
            or reservation.review_kind != review_kind
            or reservation.parent_root_session_ref != handle.root_session_ref
        ):
            raise OwnerConflict("target_review_domain_session_invalid")
        profile = self._harness.query_profile(handle.target_run_ref)
        if not isinstance(profile, dict):
            return None
        if profile.get("sandbox_mode") != "workspace-write":
            raise OwnerConflict("target_review_harness_invalid")
        values = profile.get("subagent_evidence")
        if not isinstance(values, list):
            return None
        candidates = [
            value
            for value in values
            if isinstance(value, dict)
            and value.get("provider_operation_ref") == harness_operation_ref
            and isinstance(value.get("payload"), dict)
            and value["payload"].get("review_kind") == review_kind
            and value.get("payload_hash")
            == canonical_hash(value["payload"])
        ]
        if not candidates:
            return None
        if len(candidates) != 1:
            raise OwnerConflict("target_review_child_evidence_invalid")
        payload = candidates[0]["payload"]
        try:
            if review_kind == "code":
                review = _decode_bundle_value(
                    payload.get("review"), CodeReviewRecord
                )
                scope = _decode_bundle_value(
                    payload.get("scope"), CodeReviewScope
                )
            else:
                review = _decode_bundle_value(
                    payload.get("review"), ResultReviewRecord
                )
                scope = None
        except (TypeError, ValueError) as error:
            raise OwnerConflict("target_review_child_evidence_invalid") from error
        if review_kind == "code":
            if (
                type(review) is not CodeReviewRecord
                or type(scope) is not CodeReviewScope
            ):
                raise OwnerConflict("target_review_child_evidence_invalid")
            evidence = candidates[0]
            candidate_ref_value = evidence.get("candidate_ready_evidence_ref")
            self_check_values = evidence.get("self_check_evidence_refs")
            if (
                not isinstance(candidate_ref_value, str)
                or not candidate_ref_value
                or not isinstance(self_check_values, list)
                or not self_check_values
                or any(
                    not isinstance(value, str) or not value
                    for value in self_check_values
                )
            ):
                raise OwnerConflict("target_code_review_root_evidence_invalid")
            candidate_ref: str | None = candidate_ref_value
            self_check_refs = tuple(self_check_values)
        else:
            if type(review) is not ResultReviewRecord:
                raise OwnerConflict("target_review_child_evidence_invalid")
            candidate_ref = None
            self_check_refs = ()
        return TargetReviewTurnEvidence(
            target_ref=handle.target_ref,
            target_run_ref=handle.target_run_ref,
            review_kind=review_kind,
            harness_operation_ref=harness_operation_ref,
            review=review,
            review_scope=scope,
            candidate_ready_evidence_ref=candidate_ref,
            self_check_evidence_refs=self_check_refs,
        )

    def query_accepted_code_review(
        self,
        *,
        handle: TargetWorkHandle,
        implementation_revision_ref: str,
    ) -> AcceptedTargetCodeReview | None:
        """Re-read the complete immutable code-review acceptance payload."""

        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ar_target_review_evidence WHERE "
                    "target_run_ref = :target_run_ref AND review_kind = "
                    "'code' AND subject_ref = :revision_ref"
                ),
                {
                    "target_run_ref": handle.target_run_ref,
                    "revision_ref": implementation_revision_ref,
                },
            ).first()
        if row is None:
            return None
        try:
            payload = json.loads(row.payload_json)
            review = _decode_bundle_value(payload["review"], CodeReviewRecord)
            scope = _decode_bundle_value(
                payload["complete_review_scope"], CodeReviewScope
            )
            candidate_ref = payload["candidate_ready_evidence_ref"]
            self_check_refs = tuple(payload["self_check_evidence_refs"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise OwnerConflict("target_review_integrity_invalid") from error
        if (
            type(review) is not CodeReviewRecord
            or type(scope) is not CodeReviewScope
            or review.candidate_revision_ref != implementation_revision_ref
            or not isinstance(candidate_ref, str)
            or not candidate_ref
            or not self_check_refs
            or any(not isinstance(value, str) or not value for value in self_check_refs)
        ):
            raise OwnerConflict("target_review_integrity_invalid")
        evidence = self._review_evidence(
            handle=handle,
            operation_ref=row.harness_operation_ref,
            review_kind="code",
            expected_review=projection_plain_value(review),
            expected_scope=projection_plain_value(scope),
            bind_domain_session=False,
        )
        accepted_row, accepted_payload, receipt = self._review_acceptance(
            target_run_ref=handle.target_run_ref,
            review_kind="code",
            subject_ref=implementation_revision_ref,
        )
        expected_payload = {
            "review": projection_plain_value(review),
            "complete_review_scope": projection_plain_value(scope),
            "candidate_ready_evidence_ref": candidate_ref,
            "self_check_evidence_refs": list(self_check_refs),
        }
        evidence_content = {
            "review": projection_plain_value(review),
            "complete_review_scope": projection_plain_value(scope),
        }
        if (
            accepted_row.review_ref != row.review_ref
            or accepted_payload != expected_payload
            or row.reviewer_session_ref != evidence["domain_child_session_ref"]
        ):
            raise OwnerConflict("target_review_integrity_invalid")
        return AcceptedTargetCodeReview(
            target_ref=handle.target_ref,
            target_run_ref=handle.target_run_ref,
            harness_operation_ref=row.harness_operation_ref,
            reviewer_completion_evidence_ref=(
                row.reviewer_completion_evidence_ref
            ),
            review=review,
            evidence_binding=ContentBindingProof(
                subject_ref=review.review_ref or "",
                content_hash_ref=canonical_hash(evidence_content),
            ),
            evidence_receipt=_proof(receipt),
            review_scope=scope,
            candidate_ready_evidence_ref=candidate_ref,
            self_check_evidence_refs=self_check_refs,
        )

    def accept_result_review(
        self,
        *,
        handle: TargetWorkHandle,
        review: ResultReviewRecord,
        code_review_preflights: tuple[object, ...],
        harness_operation_ref: str,
        idempotency_key: str,
    ) -> AcceptedTargetResultReview:
        payload_hash = validate_result_review(
            review,
            target_root_session_ref=handle.root_session_ref,
            evaluation_attempt_ref=review.reviewed_evaluation_attempt_ref,
            metric_result_ref=review.reviewed_metric_result_ref,
            asset_manifest_ref=review.reviewed_asset_manifest_ref,
            code_review_preflights=code_review_preflights,
        )
        evidence = self._review_evidence(
            handle=handle,
            operation_ref=harness_operation_ref,
            review_kind="result",
            expected_review=projection_plain_value(review),
        )
        accepted = self._accept_review_row(
            handle=handle,
            review_kind="result",
            subject_ref=review.reviewed_evaluation_attempt_ref,
            harness_operation_ref=harness_operation_ref,
            evidence=evidence,
            payload=projection_plain_value(review),
            payload_hash=payload_hash,
            evidence_content_hash=payload_hash,
            idempotency_key=idempotency_key,
        )
        return AcceptedTargetResultReview(
            target_ref=handle.target_ref,
            target_run_ref=handle.target_run_ref,
            harness_operation_ref=harness_operation_ref,
            reviewer_completion_evidence_ref=str(
                evidence["completion_evidence_ref"]
            ),
            review=review,
            evidence_binding=ContentBindingProof(
                subject_ref=review.review_ref,
                content_hash_ref=payload_hash,
            ),
            evidence_receipt=_proof(accepted),
        )

    def query_code_review_by_idempotency(
        self,
        idempotency_key: str,
        *,
        handle: TargetWorkHandle,
        review: CodeReviewRecord,
        review_scope: CodeReviewScope,
        harness_operation_ref: str,
        candidate_ready_evidence_ref: str,
        self_check_evidence_refs: tuple[str, ...],
    ) -> AcceptedTargetCodeReview | None:
        """Reconcile a non-empty-diff code review and recheck its Harness trace."""

        row = self._review_row_by_idempotency(idempotency_key, "code")
        if row is None:
            return None
        try:
            payload = json.loads(row.payload_json)
            stored_review = _decode_bundle_value(payload["review"], CodeReviewRecord)
            stored_scope = _decode_bundle_value(
                payload["complete_review_scope"], CodeReviewScope
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise OwnerConflict("target_review_integrity_invalid") from error
        if (
            type(stored_review) is not CodeReviewRecord
            or type(stored_scope) is not CodeReviewScope
            or stored_review != review
            or stored_scope != review_scope
            or row.harness_operation_ref != harness_operation_ref
        ):
            raise OwnerConflict("target_review_integrity_invalid")
        evidence = self._review_evidence(
            handle=handle,
            operation_ref=row.harness_operation_ref,
            review_kind="code",
            expected_review=projection_plain_value(stored_review),
            expected_scope=projection_plain_value(stored_scope),
            bind_domain_session=False,
        )
        accepted_row, accepted_payload, receipt = self._review_acceptance(
            target_run_ref=handle.target_run_ref,
            review_kind="code",
            subject_ref=stored_review.candidate_revision_ref,
        )
        expected_content = {
            "review": projection_plain_value(stored_review),
            "complete_review_scope": projection_plain_value(stored_scope),
        }
        candidate_ready_ref = payload.get("candidate_ready_evidence_ref")
        self_check_refs = payload.get("self_check_evidence_refs")
        expected_payload = {
            **expected_content,
            "candidate_ready_evidence_ref": candidate_ready_ref,
            "self_check_evidence_refs": self_check_refs,
        }
        self._verify_root_implementation_evidence(
            handle=handle,
            operation_ref=row.harness_operation_ref,
            implementation_revision_ref=stored_review.candidate_revision_ref,
            candidate_ready_evidence_ref=candidate_ready_evidence_ref,
            self_check_evidence_refs=self_check_evidence_refs,
            child_spawn_evidence_ref=str(evidence["spawn_evidence_ref"]),
            review_evidence=evidence,
        )
        if (
            accepted_row.review_ref != row.review_ref
            or accepted_payload != expected_payload
            or row.target_ref != handle.target_ref
            or row.reviewer_session_ref != evidence["domain_child_session_ref"]
            or not isinstance(candidate_ready_ref, str)
            or candidate_ready_ref != candidate_ready_evidence_ref
            or not isinstance(self_check_refs, list)
            or not self_check_refs
            or tuple(self_check_refs) != self_check_evidence_refs
            or any(not isinstance(ref, str) for ref in self_check_refs)
            or row.evidence_content_hash != canonical_hash(expected_content)
        ):
            raise OwnerConflict("target_review_integrity_invalid")
        return AcceptedTargetCodeReview(
            target_ref=handle.target_ref,
            target_run_ref=handle.target_run_ref,
            harness_operation_ref=row.harness_operation_ref,
            reviewer_completion_evidence_ref=row.reviewer_completion_evidence_ref,
            review=stored_review,
            evidence_binding=ContentBindingProof(
                subject_ref=stored_review.review_ref or "",
                content_hash_ref=canonical_hash(expected_content),
            ),
            evidence_receipt=_proof(receipt),
            review_scope=stored_scope,
            candidate_ready_evidence_ref=candidate_ready_ref,
            self_check_evidence_refs=tuple(self_check_refs),
        )

    def _verify_root_implementation_evidence(
        self,
        *,
        handle: TargetWorkHandle,
        operation_ref: str,
        implementation_revision_ref: str,
        candidate_ready_evidence_ref: str,
        self_check_evidence_refs: tuple[str, ...],
        child_spawn_evidence_ref: str | None,
        review_evidence: dict[str, object] | None,
    ) -> None:
        """Verify revision-bound root evidence precedes any review child."""

        if not self_check_evidence_refs or len(self_check_evidence_refs) != len(
            set(self_check_evidence_refs)
        ):
            raise OwnerConflict("target_code_review_root_evidence_invalid")
        run = self._harness.query_target_run_by_ref(handle.target_run_ref)
        events = self._operation_evidence_events(
            operation_ref,
            run_ref=handle.target_run_ref,
        )
        candidate_event = events.get(candidate_ready_evidence_ref)
        check_events = tuple(events.get(ref) for ref in self_check_evidence_refs)
        spawn = (
            None
            if child_spawn_evidence_ref is None
            else events.get(child_spawn_evidence_ref)
        )
        if (
            run is None
            or run.root_session_ref != handle.root_session_ref
            or candidate_event is None
            or any(item is None for item in check_events)
            or (child_spawn_evidence_ref is not None and spawn is None)
            or review_evidence is None
        ):
            raise OwnerConflict("target_code_review_root_evidence_invalid")
        candidate = review_evidence.get("candidate_ready")
        self_check = review_evidence.get("self_check")
        evidence_self_check_refs = review_evidence.get("self_check_evidence_refs")
        bundle = self._memory.query_implementation_bundle(
            implementation_revision_ref
        )
        candidate_sequence = candidate_event[0]
        typed_checks = tuple(item for item in check_events if item is not None)
        check_sequences = tuple(item[0] for item in typed_checks)
        expected_common = {
            "target_ref": handle.target_ref,
            "target_run_ref": handle.target_run_ref,
            "implementation_revision_ref": implementation_revision_ref,
            "expected_tree_hash": (
                None if bundle is None else bundle.bundle_content_hash_ref
            ),
        }
        candidate_common = (
            {}
            if not isinstance(candidate, dict)
            else {key: candidate.get(key) for key in expected_common}
        )
        self_check_common = (
            {}
            if not isinstance(self_check, dict)
            else {key: self_check.get(key) for key in expected_common}
        )
        command_ids = review_evidence.get("successful_command_item_ids")
        command_hashes = review_evidence.get("successful_command_exit_hashes")
        if (
            bundle is None
            or review_evidence.get("candidate_ready_evidence_ref")
            != candidate_ready_evidence_ref
            or review_evidence.get("self_check_evidence_ref")
            != self_check_evidence_refs[0]
            or evidence_self_check_refs != list(self_check_evidence_refs)
            or not isinstance(candidate, dict)
            or set(candidate)
            != {
                "schema_ref",
                "target_ref",
                "target_run_ref",
                "implementation_revision_ref",
                "expected_tree_hash",
            }
            or candidate.get("schema_ref")
            != "meta-research/target-candidate-ready-evidence/v1"
            or candidate_common != expected_common
            or not isinstance(self_check, dict)
            or set(self_check)
            != {
                "schema_ref",
                "target_ref",
                "target_run_ref",
                "implementation_revision_ref",
                "expected_tree_hash",
                "status",
            }
            or self_check.get("schema_ref")
            != "meta-research/target-self-check-evidence/v1"
            or self_check.get("status") != "passed"
            or self_check_common != expected_common
            or not isinstance(command_ids, list)
            or not isinstance(command_hashes, list)
            or not command_ids
            or len(command_ids) != len(command_hashes)
            or len(command_ids) != len(set(command_ids))
            or any(not isinstance(value, str) or not value for value in command_ids)
            or any(
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
                for value in command_hashes
            )
            or check_sequences != tuple(sorted(check_sequences))
            or any(sequence <= candidate_sequence for sequence in check_sequences)
            or (
                spawn is not None
                and any(sequence >= spawn[0] for sequence in check_sequences)
            )
        ):
            raise OwnerConflict("target_code_review_root_evidence_invalid")

    def query_result_review_by_idempotency(
        self,
        idempotency_key: str,
        *,
        handle: TargetWorkHandle,
        review: ResultReviewRecord,
        harness_operation_ref: str,
    ) -> AcceptedTargetResultReview | None:
        """Reconcile a result review and recheck its independent child trace."""

        row = self._review_row_by_idempotency(idempotency_key, "result")
        if row is None:
            return None
        try:
            payload = json.loads(row.payload_json)
            stored_review = _decode_bundle_value(payload, ResultReviewRecord)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise OwnerConflict("target_review_integrity_invalid") from error
        if (
            type(stored_review) is not ResultReviewRecord
            or stored_review != review
            or row.harness_operation_ref != harness_operation_ref
        ):
            raise OwnerConflict("target_review_integrity_invalid")
        evidence = self._review_evidence(
            handle=handle,
            operation_ref=row.harness_operation_ref,
            review_kind="result",
            expected_review=projection_plain_value(stored_review),
            bind_domain_session=False,
        )
        accepted_row, accepted_payload, receipt = self._review_acceptance(
            target_run_ref=handle.target_run_ref,
            review_kind="result",
            subject_ref=stored_review.reviewed_evaluation_attempt_ref,
        )
        if (
            accepted_row.review_ref != row.review_ref
            or accepted_payload != projection_plain_value(stored_review)
            or row.target_ref != handle.target_ref
            or row.reviewer_session_ref != evidence["domain_child_session_ref"]
        ):
            raise OwnerConflict("target_review_integrity_invalid")
        return AcceptedTargetResultReview(
            target_ref=handle.target_ref,
            target_run_ref=handle.target_run_ref,
            harness_operation_ref=row.harness_operation_ref,
            reviewer_completion_evidence_ref=row.reviewer_completion_evidence_ref,
            review=stored_review,
            evidence_binding=ContentBindingProof(
                subject_ref=stored_review.review_ref,
                content_hash_ref=row.payload_hash,
            ),
            evidence_receipt=_proof(receipt),
        )

    def _review_row_by_idempotency(
        self,
        idempotency_key: str,
        review_kind: str,
    ) -> object | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ar_target_review_evidence WHERE "
                    "idempotency_key = :idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
        if row is not None and row.review_kind != review_kind:
            raise OwnerConflict("target_review_conflict")
        return row

    def accept_execution_eligibility(
        self,
        *,
        handle: TargetWorkHandle,
        preflight: TargetExecutionPreflight,
        harness_operation_ref: str,
        idempotency_key: str,
    ) -> AcceptedTargetExecutionEligibility:
        """Accept the reviewed implementation closure consumed by execution.

        RM content intake, a Harness review, and a caller-assembled preflight
        are individually insufficient.  This boundary re-verifies their exact
        conjunction against the current activated TargetRun before issuing the
        only receipt the generic execution port accepts.
        """

        if (
            not isinstance(idempotency_key, str)
            or not idempotency_key
            or len(idempotency_key) > 128
            or not isinstance(harness_operation_ref, str)
            or not harness_operation_ref
            or len(harness_operation_ref) > 256
        ):
            raise OwnerConflict("target_execution_eligibility_invalid")
        bundle, usage, review_receipt = self._execution_eligibility_facts(
            handle=handle,
            preflight=preflight,
            harness_operation_ref=harness_operation_ref,
            require_running=True,
        )
        payload = {
            "handle": projection_plain_value(handle),
            "implementation_bundle": projection_plain_value(bundle),
            "implementation_bundle_usage": projection_plain_value(usage),
            "preflight": projection_plain_value(preflight),
            "code_review_acceptance_receipt": (
                None
                if review_receipt is None
                else review_receipt.as_public_dict()
            ),
            "harness_operation_ref": harness_operation_ref,
        }
        payload_hash = canonical_hash(payload)
        request_hash = canonical_hash(
            {"command": "accept_target_execution_eligibility", **payload}
        )
        handle_json = canonical_json(projection_plain_value(handle))
        handle_hash = canonical_hash(projection_plain_value(handle))
        preflight_json = canonical_json(projection_plain_value(preflight))
        preflight_hash = canonical_hash(projection_plain_value(preflight))
        now = time.time()
        with self._database.fenced_write() as connection:
            # Close the race between the issuer reads above and the first
            # eligibility write.  A Harness successor reservation or AR CAS
            # makes the old Fence fail before row/counter/feed mutation.
            from meta_research.owners.agent_runtime import (
                verify_current_target_run_frontier_in_transaction,
            )

            verify_current_target_run_frontier_in_transaction(connection, handle)
            self.verify_current_target_run_handle(handle)
            row = connection.execute(
                text(
                    "SELECT * FROM ar_target_execution_eligibilities_v3 WHERE "
                    "idempotency_key = :key OR (target_run_ref = :run_ref AND "
                    "target_attempt_ref = :attempt_ref AND "
                    "implementation_revision_ref = :revision_ref)"
                ),
                {
                    "key": idempotency_key,
                    "run_ref": handle.target_run_ref,
                    "attempt_ref": handle.execution_attempt_ref,
                    "revision_ref": preflight.implementation_revision_ref,
                },
            ).first()
            if row is not None:
                if row.request_hash != request_hash:
                    raise OwnerConflict("target_execution_eligibility_conflict")
                eligibility_ref = row.eligibility_ref
            else:
                eligibility_ref = new_ref("target_execution_eligibility")
                receipt = _receipt(
                    "agent_runtime",
                    AR_TARGET_EXECUTION_ELIGIBILITY_RECEIPT_KIND,
                    new_ref("ar_target_execution_eligibility_receipt"),
                    eligibility_ref,
                    {
                        **payload,
                        "eligibility_ref": eligibility_ref,
                        "payload_hash": payload_hash,
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO ar_target_execution_eligibilities_v3 "
                        "(eligibility_ref, target_ref, target_run_ref, "
                        "target_attempt_ref, target_fence_ref, "
                        "implementation_revision_ref, "
                        "implementation_bundle_receipt_ref, "
                        "implementation_bundle_receipt_hash, "
                        "implementation_bundle_usage_ref, "
                        "implementation_usage_receipt_ref, "
                        "implementation_usage_receipt_hash, "
                        "code_review_receipt_ref, code_review_receipt_hash, "
                        "harness_operation_ref, handle_json, handle_hash, "
                        "preflight_json, preflight_hash, payload_json, "
                        "payload_hash, idempotency_key, request_hash, "
                        "receipt_ref, receipt_hash, accepted_at) VALUES "
                        "(:eligibility_ref, :target_ref, :target_run_ref, "
                        ":target_attempt_ref, :target_fence_ref, "
                        ":implementation_revision_ref, "
                        ":implementation_bundle_receipt_ref, "
                        ":implementation_bundle_receipt_hash, "
                        ":implementation_bundle_usage_ref, "
                        ":implementation_usage_receipt_ref, "
                        ":implementation_usage_receipt_hash, "
                        ":code_review_receipt_ref, :code_review_receipt_hash, "
                        ":harness_operation_ref, :handle_json, :handle_hash, "
                        ":preflight_json, :preflight_hash, :payload_json, "
                        ":payload_hash, :idempotency_key, :request_hash, "
                        ":receipt_ref, :receipt_hash, :accepted_at)"
                    ),
                    {
                        "eligibility_ref": eligibility_ref,
                        "target_ref": handle.target_ref,
                        "target_run_ref": handle.target_run_ref,
                        "target_attempt_ref": handle.execution_attempt_ref,
                        "target_fence_ref": handle.execution_fence_ref,
                        "implementation_revision_ref": (
                            preflight.implementation_revision_ref
                        ),
                        "implementation_bundle_receipt_ref": (
                            bundle.receipt.receipt_ref
                        ),
                        "implementation_bundle_receipt_hash": (
                            bundle.receipt.payload_hash
                        ),
                        "implementation_bundle_usage_ref": usage.usage_ref,
                        "implementation_usage_receipt_ref": (
                            usage.receipt.receipt_ref
                        ),
                        "implementation_usage_receipt_hash": (
                            usage.receipt.payload_hash
                        ),
                        "code_review_receipt_ref": (
                            None
                            if review_receipt is None
                            else review_receipt.receipt_ref
                        ),
                        "code_review_receipt_hash": (
                            None
                            if review_receipt is None
                            else review_receipt.payload_hash
                        ),
                        "harness_operation_ref": harness_operation_ref,
                        "handle_json": handle_json,
                        "handle_hash": handle_hash,
                        "preflight_json": preflight_json,
                        "preflight_hash": preflight_hash,
                        "payload_json": canonical_json(payload),
                        "payload_hash": payload_hash,
                        "idempotency_key": idempotency_key,
                        "request_hash": request_hash,
                        "receipt_ref": receipt.receipt_ref,
                        "receipt_hash": receipt.payload_hash,
                        "accepted_at": now,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE agent_runtime_state SET revision = revision + 1, "
                        "target_execution_eligibility_count = "
                        "target_execution_eligibility_count + 1 WHERE "
                        "singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "agent_runtime.target_execution_eligibility_accepted",
                    {
                        "eligibility_ref": eligibility_ref,
                        "target_ref": handle.target_ref,
                        "target_run_ref": handle.target_run_ref,
                        "target_attempt_ref": handle.execution_attempt_ref,
                        "implementation_revision_ref": (
                            preflight.implementation_revision_ref
                        ),
                        "receipt_ref": receipt.receipt_ref,
                    },
                )
        accepted = self.query_execution_eligibility(eligibility_ref)
        if accepted is None:
            raise OwnerConflict("target_execution_eligibility_missing")
        return accepted

    def query_execution_eligibility(
        self, eligibility_ref: str
    ) -> AcceptedTargetExecutionEligibility | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ar_target_execution_eligibilities_v3 WHERE "
                    "eligibility_ref = :eligibility_ref"
                ),
                {"eligibility_ref": eligibility_ref},
            ).first()
        if row is None:
            return None
        try:
            handle = _decode_stored_record(
                row.handle_json, row.handle_hash, TargetWorkHandle
            )
            preflight = _decode_stored_record(
                row.preflight_json, row.preflight_hash, TargetExecutionPreflight
            )
            payload = json.loads(row.payload_json)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise OwnerConflict(
                "target_execution_eligibility_integrity_invalid"
            ) from error
        if type(handle) is not TargetWorkHandle or type(preflight) is not TargetExecutionPreflight:
            raise OwnerConflict("target_execution_eligibility_integrity_invalid")
        bundle, usage, review_receipt = self._execution_eligibility_facts(
            handle=handle,
            preflight=preflight,
            harness_operation_ref=row.harness_operation_ref,
            require_running=False,
        )
        expected_payload = {
            "handle": projection_plain_value(handle),
            "implementation_bundle": projection_plain_value(bundle),
            "implementation_bundle_usage": projection_plain_value(usage),
            "preflight": projection_plain_value(preflight),
            "code_review_acceptance_receipt": (
                None
                if review_receipt is None
                else review_receipt.as_public_dict()
            ),
            "harness_operation_ref": row.harness_operation_ref,
        }
        expected_request_hash = canonical_hash(
            {"command": "accept_target_execution_eligibility", **expected_payload}
        )
        receipt = _receipt(
            "agent_runtime",
            AR_TARGET_EXECUTION_ELIGIBILITY_RECEIPT_KIND,
            row.receipt_ref,
            eligibility_ref,
            {
                **expected_payload,
                "eligibility_ref": eligibility_ref,
                "payload_hash": canonical_hash(expected_payload),
            },
        )
        if (
            row.target_ref != handle.target_ref
            or row.target_run_ref != handle.target_run_ref
            or row.target_attempt_ref != handle.execution_attempt_ref
            or row.target_fence_ref != handle.execution_fence_ref
            or row.implementation_revision_ref
            != preflight.implementation_revision_ref
            or row.implementation_bundle_receipt_ref
            != bundle.receipt.receipt_ref
            or row.implementation_bundle_receipt_hash
            != bundle.receipt.payload_hash
            or row.implementation_bundle_usage_ref != usage.usage_ref
            or row.implementation_usage_receipt_ref
            != usage.receipt.receipt_ref
            or row.implementation_usage_receipt_hash
            != usage.receipt.payload_hash
            or row.code_review_receipt_ref
            != (None if review_receipt is None else review_receipt.receipt_ref)
            or row.code_review_receipt_hash
            != (None if review_receipt is None else review_receipt.payload_hash)
            or payload != expected_payload
            or row.payload_hash != canonical_hash(expected_payload)
            or row.request_hash != expected_request_hash
            or row.receipt_hash != receipt.payload_hash
        ):
            raise OwnerConflict("target_execution_eligibility_integrity_invalid")
        return AcceptedTargetExecutionEligibility(
            eligibility_ref=eligibility_ref,
            handle=handle,
            implementation_bundle=bundle,
            implementation_bundle_usage=usage,
            preflight=preflight,
            code_review_acceptance_receipt=review_receipt,
            harness_operation_ref=row.harness_operation_ref,
            payload_hash=row.payload_hash,
            receipt=receipt,
            accepted_at=float(row.accepted_at),
        )

    def query_execution_eligibility_by_idempotency(
        self,
        idempotency_key: str,
    ) -> AcceptedTargetExecutionEligibility | None:
        """Reconcile the execution gate without depending on its random ref."""

        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT eligibility_ref FROM "
                    "ar_target_execution_eligibilities_v3 WHERE "
                    "idempotency_key = :idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
        return (
            None
            if row is None
            else self.query_execution_eligibility(row.eligibility_ref)
        )

    def query_execution_harness_operation_for_preflight(
        self,
        *,
        target_ref: str,
        preflight: TargetExecutionPreflight,
    ) -> str:
        """Resolve the immutable review/self-check operation after recovery."""

        with self._database.read() as connection:
            stored = connection.execute(
                text(
                    "SELECT preflight_json, preflight_hash FROM "
                    "ar_target_run_preflights WHERE target_ref = :target_ref "
                    "AND implementation_revision_ref = :revision_ref"
                ),
                {
                    "target_ref": target_ref,
                    "revision_ref": preflight.implementation_revision_ref,
                },
            ).first()
            review_row = connection.execute(
                text(
                    "SELECT harness_operation_ref FROM "
                    "ar_target_review_evidence WHERE target_ref = :target_ref "
                    "AND target_run_ref = :target_run_ref AND review_kind = "
                    "'code' AND subject_ref = :revision_ref"
                ),
                {
                    "target_ref": target_ref,
                    "target_run_ref": preflight.target_run_ref,
                    "revision_ref": preflight.implementation_revision_ref,
                },
            ).first()
            eligibility_row = connection.execute(
                text(
                    "SELECT harness_operation_ref FROM "
                    "ar_target_execution_eligibilities_v3 WHERE target_ref = "
                    ":target_ref AND target_run_ref = :target_run_ref AND "
                    "implementation_revision_ref = :revision_ref ORDER BY "
                    "accepted_at LIMIT 1"
                ),
                {
                    "target_ref": target_ref,
                    "target_run_ref": preflight.target_run_ref,
                    "revision_ref": preflight.implementation_revision_ref,
                },
            ).first()
        if stored is None:
            raise OwnerConflict("target_execution_preflight_history_invalid")
        try:
            stored_preflight = _decode_stored_record(
                stored.preflight_json,
                stored.preflight_hash,
                TargetExecutionPreflight,
            )
        except (TypeError, ValueError) as error:
            raise OwnerConflict(
                "target_execution_preflight_history_invalid"
            ) from error
        if stored_preflight != preflight:
            raise OwnerConflict("target_execution_preflight_history_invalid")
        row = review_row if preflight.code_review.code_changed else eligibility_row
        if row is None or not isinstance(row.harness_operation_ref, str):
            raise OwnerConflict("target_execution_preflight_history_invalid")
        if review_row is not None:
            self._review_acceptance(
                target_run_ref=preflight.target_run_ref,
                review_kind="code",
                subject_ref=preflight.implementation_revision_ref,
            )
        return row.harness_operation_ref

    def verify_execution_eligibility(
        self,
        *,
        handle: TargetWorkHandle,
        eligibility_ref: str,
        receipt: ReceiptProof,
    ) -> AcceptedTargetExecutionEligibility:
        accepted = self.query_execution_eligibility(eligibility_ref)
        if accepted is None or accepted.handle != handle or receipt != _proof(
            accepted.receipt
        ):
            raise OwnerConflict("target_execution_eligibility_invalid")
        self.verify_current_target_run_handle(handle)
        return accepted

    def _execution_eligibility_facts(
        self,
        *,
        handle: TargetWorkHandle,
        preflight: TargetExecutionPreflight,
        harness_operation_ref: str,
        require_running: bool,
    ) -> tuple[
        AcceptedTargetImplementationBundle,
        AcceptedTargetImplementationBundleUsage,
        AcceptanceReceipt | None,
    ]:
        self.verify_current_target_run_handle(handle)
        with self._database.read() as connection:
            frontier = connection.execute(
                text(
                    "SELECT * FROM ar_target_frontier_entries WHERE "
                    "target_ref = :target_ref"
                ),
                {"target_ref": handle.target_ref},
            ).first()
            activation = connection.execute(
                text(
                    "SELECT * FROM ar_target_run_activations WHERE "
                    "target_ref = :target_ref"
                ),
                {"target_ref": handle.target_ref},
            ).first()
            preflight_row = connection.execute(
                text(
                    "SELECT * FROM ar_target_run_preflights WHERE "
                    "target_ref = :target_ref ORDER BY ordinal DESC LIMIT 1"
                ),
                {"target_ref": handle.target_ref},
            ).first()
        if frontier is None or activation is None or preflight_row is None:
            raise OwnerConflict("target_execution_eligibility_activation_invalid")
        try:
            current_handle = _decode_stored_record(
                frontier.current_handle_json,
                frontier.current_handle_hash,
                TargetWorkHandle,
            )
            stored_preflight = _decode_stored_record(
                preflight_row.preflight_json,
                preflight_row.preflight_hash,
                TargetExecutionPreflight,
            )
            initial_handle = _decode_stored_record(
                activation.initial_handle_json,
                activation.initial_handle_hash,
                TargetWorkHandle,
            )
            candidate = _decode_stored_record(
                activation.candidate_json,
                activation.candidate_hash,
                TargetCandidate,
            )
            formal_plan = _decode_stored_record(
                activation.formal_plan_json,
                activation.formal_plan_hash,
                FormalPlan,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise OwnerConflict(
                "target_execution_eligibility_activation_invalid"
            ) from error
        if (
            current_handle != handle
            or stored_preflight != preflight
            or type(initial_handle) is not TargetWorkHandle
            or type(candidate) is not TargetCandidate
            or type(formal_plan) is not FormalPlan
            or frontier.currentness_known != 1
            or frontier.current != 1
            or frontier.state not in {"running", "terminal"}
            or (require_running and frontier.state != "running")
            or preflight.target_ref != handle.target_ref
            or preflight.target_run_ref != handle.target_run_ref
        ):
            raise OwnerConflict("target_execution_eligibility_activation_invalid")
        self.verify_current_target_run_scope(
            handle=handle,
            candidate=candidate,
            formal_plan=formal_plan,
        )
        preflight_handle = handle
        if initial_handle != handle:
            verify_reuse = getattr(
                self._execution_verifier,
                "verify_target_recovery_preflight_reuse",
                None,
            )
            if not callable(verify_reuse):
                raise OwnerConflict(
                    "target_recovery_preflight_reuse_verifier_unavailable"
                )
            try:
                preflight_handle = verify_reuse(
                    current_handle=handle,
                    preflight=preflight,
                )
            except Exception as error:
                raise OwnerConflict(
                    "target_recovery_preflight_reuse_invalid"
                ) from error
            if type(preflight_handle) is not TargetWorkHandle:
                raise OwnerConflict("target_recovery_preflight_reuse_invalid")
        try:
            validate_protected_execution_admission(
                preflight_handle,
                preflight,
                expected_review_scope=preflight.review_scope,
                expected_implementation_revision_ref=(
                    candidate.implementation_revision_ref
                ),
                expected_code_changed=candidate.code_changed,
            )
        except (TargetRunContractError, TypeError, ValueError) as error:
            raise OwnerConflict("target_execution_eligibility_preflight_invalid") from error
        bundle = self._memory.query_implementation_bundle(
            preflight.implementation_revision_ref
        )
        usage = self._memory.query_implementation_bundle_usage(
            target_ref=handle.target_ref,
            implementation_revision_ref=preflight.implementation_revision_ref,
        )
        if bundle is None or (
            usage is None
            or usage.bundle != bundle
            or bundle.implementation_revision_ref
            != preflight.implementation_revision_ref
            or preflight.review_scope.candidate_revision_binding.subject_ref
            != preflight.implementation_revision_ref
            or preflight.review_scope.candidate_revision_binding.content_hash_ref
            != bundle.bundle_content_hash_ref
            or preflight.implementation_acceptance_receipt
            != receipt_proof(
                bundle.receipt,
                subject_ref=bundle.bundle_content_hash_ref,
            )
        ):
            raise OwnerConflict(
                "target_execution_eligibility_implementation_invalid"
            )
        evidence_refs = self._operation_evidence_refs(
            harness_operation_ref,
            run_ref=handle.target_run_ref,
        )
        if (
            preflight.candidate_ready_evidence.evidence_ref not in evidence_refs
            or not preflight.self_check_evidence
            or any(
                evidence.evidence_ref not in evidence_refs
                for evidence in preflight.self_check_evidence
            )
        ):
            raise OwnerConflict("target_execution_eligibility_self_check_invalid")
        review_receipt: AcceptanceReceipt | None = None
        if preflight.code_review.code_changed:
            review_row, review_payload, review_receipt = self._review_acceptance(
                target_run_ref=handle.target_run_ref,
                review_kind="code",
                subject_ref=preflight.implementation_revision_ref,
            )
            expected_review_content = {
                "review": projection_plain_value(preflight.code_review),
                "complete_review_scope": projection_plain_value(
                    preflight.review_scope
                ),
            }
            expected_review_payload = {
                **expected_review_content,
                "candidate_ready_evidence_ref": (
                    preflight.candidate_ready_evidence.evidence_ref
                ),
                "self_check_evidence_refs": [
                    evidence.evidence_ref
                    for evidence in preflight.self_check_evidence
                ],
            }
            if (
                review_row.harness_operation_ref != harness_operation_ref
                or review_payload != expected_review_payload
                or preflight.code_review_evidence_binding
                != ContentBindingProof(
                    subject_ref=preflight.code_review.review_ref or "",
                    content_hash_ref=canonical_hash(expected_review_content),
                )
                or preflight.code_review_evidence_receipt
                != _proof(review_receipt)
            ):
                raise OwnerConflict("target_execution_eligibility_review_invalid")
        elif (
            preflight.code_review_evidence_binding is not None
            or preflight.code_review_evidence_receipt is not None
        ):
            raise OwnerConflict("target_execution_eligibility_review_invalid")
        return bundle, usage, review_receipt

    def accept_execution_closure(
        self,
        *,
        handle: TargetWorkHandle,
        protected_binding_ref: str,
        result_manifest_ref: str,
        formal_metric_ref: str,
        experiment_result_hash: str,
        result_review: ResultReviewRecord,
        experiment_execution_receipt: AcceptanceReceipt,
        idempotency_key: str,
    ) -> AcceptedTargetExecutionClosure:
        """Accept the TargetRun execution receipt only after issuer rechecks.

        The receipt subject remains the TargetRun Attempt.  The independently
        owned Experiment Run/Attempt/Fence and EvaluationAttempt are retained
        in the payload and are never relabelled as TargetRun identities.
        """

        raise OwnerConflict("legacy_target_execution_closure_write_forbidden")

    def accept_generic_execution_closure(
        self,
        *,
        handle: TargetWorkHandle,
        generic_binding_ref: str,
        result_manifest_ref: str,
        measurement_ref: str,
        result_review: ResultReviewRecord,
        idempotency_key: str,
    ) -> AcceptedTargetGenericExecutionClosure:
        """Close one formal Target attempt from generic-operation facts only."""

        raise OwnerConflict("target_generic_measurement_shadow_write_forbidden")

    def query_generic_execution_closure(
        self, closure_ref: str
    ) -> AcceptedTargetGenericExecutionClosure | None:
        """Reconcile a generic closure through all three issuing Owners."""

        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ar_target_generic_execution_closures WHERE "
                    "closure_ref = :closure_ref"
                ),
                {"closure_ref": closure_ref},
            ).first()
        if row is None:
            return None
        try:
            payload = json.loads(row.payload_json)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise OwnerConflict(
                "target_generic_execution_closure_integrity_invalid"
            ) from error
        binding = self._graph.query_generic_execution_binding(
            row.generic_binding_ref
        )
        manifest = self._memory.query_generic_result_manifest(row.manifest_ref)
        measurement = self._graph.query_generic_measurement(row.measurement_ref)
        review_row, review_payload, review_receipt = self._review_acceptance(
            target_run_ref=row.target_run_ref,
            review_kind="result",
            subject_ref=(
                ""
                if measurement is None
                else measurement.evaluation_attempt_ref
            ),
        )
        if binding is None or manifest is None or measurement is None:
            raise OwnerConflict(
                "target_generic_execution_closure_integrity_invalid"
            )
        expected_payload = {
            "target_ref": row.target_ref,
            "target_run_ref": row.target_run_ref,
            "target_attempt_ref": row.target_attempt_ref,
            "target_fence_ref": row.target_fence_ref,
            "generic_execution": projection_plain_value(binding),
            "result_manifest": projection_plain_value(manifest),
            "measurement": projection_plain_value(measurement),
            "result_review": review_payload,
            "result_review_acceptance_receipt": review_receipt.as_public_dict(),
        }
        payload_hash = canonical_hash(expected_payload)
        receipt = _receipt(
            "agent_runtime",
            AR_TARGET_GENERIC_EXECUTION_CLOSURE_RECEIPT_KIND,
            row.receipt_ref,
            row.target_attempt_ref,
            {
                "closure_ref": closure_ref,
                "payload_hash": payload_hash,
                **expected_payload,
            },
        )
        if (
            payload != expected_payload
            or row.payload_hash != payload_hash
            or row.request_hash
            != canonical_hash(
                {
                    "command": "accept_target_generic_execution_closure",
                    "payload": expected_payload,
                }
            )
            or row.receipt_hash != receipt.payload_hash
            or binding.target_ref != row.target_ref
            or manifest.generic_binding_ref != binding.binding_ref
            or measurement.generic_binding_ref != binding.binding_ref
            or measurement.manifest_ref != manifest.manifest_ref
            or review_row.review_ref != row.result_review_ref
        ):
            raise OwnerConflict(
                "target_generic_execution_closure_integrity_invalid"
            )
        return AcceptedTargetGenericExecutionClosure(
            closure_ref=closure_ref,
            target_ref=row.target_ref,
            target_run_ref=row.target_run_ref,
            target_attempt_ref=row.target_attempt_ref,
            target_fence_ref=row.target_fence_ref,
            generic_binding_ref=row.generic_binding_ref,
            result_manifest_ref=row.manifest_ref,
            measurement_ref=row.measurement_ref,
            result_review_ref=row.result_review_ref,
            payload_hash=payload_hash,
            receipt=receipt,
            accepted_at=float(row.accepted_at),
        )

    def _native_closure_source_snapshot(
        self,
        connection,
        *,
        handle: TargetWorkHandle,
        evaluation_attempt_ref: str,
    ) -> tuple[str, str, str]:
        """Hash every raw issuer row consumed by native closure admission.

        This helper deliberately performs no Owner query and no child-session
        binding.  Callers supply one SQLite connection so every row-set comes
        from the same transaction snapshot and the closure writer can compare
        the exact evidence cut that passed the higher-level semantic checks.
        """

        def plain_rows(rows) -> list[dict[str, object]]:
            return [
                {
                    str(key): row._mapping[key]
                    for key in sorted(row._mapping, key=str)
                }
                for row in rows
            ]

        parameters = {
            "target_ref": handle.target_ref,
            "target_run_ref": handle.target_run_ref,
            "evaluation_attempt_ref": evaluation_attempt_ref,
        }
        result_reviews = connection.execute(
            text(
                "SELECT review_ref, target_ref, target_run_ref, review_kind, "
                "subject_ref, parent_session_ref, reviewer_session_ref, "
                "reviewer_spawn_evidence_ref, "
                "reviewer_completion_evidence_ref, harness_operation_ref, "
                "payload_json, payload_hash, evidence_content_hash, "
                "idempotency_key, request_hash, receipt_ref, receipt_hash, "
                "accepted_at FROM ar_target_review_evidence WHERE "
                "target_ref = :target_ref AND target_run_ref = "
                ":target_run_ref AND review_kind = 'result' AND subject_ref = "
                ":evaluation_attempt_ref ORDER BY review_ref"
            ),
            parameters,
        ).all()
        if len(result_reviews) != 1:
            raise OwnerConflict("target_native_execution_closure_source_missing")
        result_review = result_reviews[0]
        operation_ref = result_review.harness_operation_ref

        measurement_attempts = connection.execute(
            text(
                "SELECT * FROM rg_target_measurement_attempt_bindings WHERE "
                "target_ref = :target_ref AND target_run_ref = "
                ":target_run_ref AND evaluation_attempt_ref = "
                ":evaluation_attempt_ref ORDER BY attempt_binding_ref"
            ),
            parameters,
        ).all()
        if len(measurement_attempts) != 1:
            raise OwnerConflict("target_native_execution_closure_source_missing")
        measurement_attempt = measurement_attempts[0]
        native_parameters = {
            **parameters,
            "authority_ref": measurement_attempt.authority_ref,
            "generic_binding_ref": measurement_attempt.generic_binding_ref,
            "manifest_ref": measurement_attempt.manifest_ref,
            "variant_run_ref": measurement_attempt.variant_run_ref,
            "variant_input_binding_ref": (
                measurement_attempt.variant_input_binding_ref
            ),
            "evaluation_input_binding_ref": (
                measurement_attempt.evaluation_input_binding_ref
            ),
        }
        generic_bindings = connection.execute(
            text(
                "SELECT * FROM rg_target_generic_execution_bindings_v3 WHERE "
                "binding_ref = :generic_binding_ref"
            ),
            native_parameters,
        ).all()
        result_manifests = connection.execute(
            text(
                "SELECT * FROM rm_target_generic_result_manifests WHERE "
                "manifest_ref = :manifest_ref"
            ),
            native_parameters,
        ).all()
        metric_results = connection.execute(
            text(
                "SELECT * FROM rg_metric_results WHERE "
                "evaluation_attempt_ref = :evaluation_attempt_ref ORDER BY "
                "metric_result_ref"
            ),
            native_parameters,
        ).all()
        measurement_authorities = connection.execute(
            text(
                "SELECT * FROM rg_target_measurement_domain_authorities WHERE "
                "authority_ref = :authority_ref AND target_ref = :target_ref"
            ),
            native_parameters,
        ).all()
        variant_runs = connection.execute(
            text(
                "SELECT * FROM rg_variant_runs WHERE variant_run_ref = "
                ":variant_run_ref"
            ),
            native_parameters,
        ).all()
        evaluation_attempts = connection.execute(
            text(
                "SELECT * FROM rg_evaluation_attempts WHERE "
                "evaluation_attempt_ref = :evaluation_attempt_ref"
            ),
            native_parameters,
        ).all()
        experiment_input_bindings = connection.execute(
            text(
                "SELECT * FROM rg_experiment_input_bindings WHERE binding_ref "
                "IN (:variant_input_binding_ref, "
                ":evaluation_input_binding_ref) ORDER BY binding_ref"
            ),
            native_parameters,
        ).all()
        asset_roles = connection.execute(
            text(
                "SELECT * FROM rg_experiment_asset_roles WHERE subject_ref = "
                ":evaluation_attempt_ref OR (subject_ref = :variant_run_ref "
                "AND role = 'checkpoint_artifact') ORDER BY role, ordinal, "
                "role_ref"
            ),
            native_parameters,
        ).all()
        checkpoint_roles = connection.execute(
            text(
                "SELECT * FROM rg_evaluation_attempt_checkpoints WHERE "
                "evaluation_attempt_ref = :evaluation_attempt_ref ORDER BY "
                "ordinal, checkpoint_role_ref"
            ),
            native_parameters,
        ).all()
        generic_binding = generic_bindings[0] if len(generic_bindings) == 1 else None
        execution_inputs = (
            []
            if generic_binding is None
            else connection.execute(
                text(
                    "SELECT * FROM rg_target_execution_input_bindings WHERE "
                    "binding_ref = :binding_ref"
                ),
                {"binding_ref": generic_binding.input_binding_ref},
            ).all()
        )
        execution_eligibilities = (
            []
            if generic_binding is None
            else connection.execute(
                text(
                    "SELECT * FROM ar_target_execution_eligibilities_v3 WHERE "
                    "eligibility_ref = :eligibility_ref"
                ),
                {"eligibility_ref": generic_binding.execution_eligibility_ref},
            ).all()
        )
        accepted_assets = connection.execute(
            text(
                "SELECT versions.* FROM rm_asset_versions versions WHERE "
                "versions.version_ref IN (SELECT roles.version_ref FROM "
                "rg_experiment_asset_roles roles WHERE roles.subject_ref = "
                ":evaluation_attempt_ref OR (roles.subject_ref = "
                ":variant_run_ref AND roles.role = 'checkpoint_artifact')) "
                "ORDER BY versions.version_ref"
            ),
            native_parameters,
        ).all()
        asset_custodies = connection.execute(
            text(
                "SELECT custodies.* FROM rm_asset_custodies custodies WHERE "
                "custodies.version_ref IN (SELECT roles.version_ref FROM "
                "rg_experiment_asset_roles roles WHERE roles.subject_ref = "
                ":evaluation_attempt_ref OR (roles.subject_ref = "
                ":variant_run_ref AND roles.role = 'checkpoint_artifact')) "
                "ORDER BY custodies.version_ref, custodies.custody_mode"
            ),
            native_parameters,
        ).all()
        asset_roots = connection.execute(
            text(
                "SELECT assets.* FROM rm_assets assets WHERE assets.asset_ref "
                "IN (SELECT roles.asset_ref FROM rg_experiment_asset_roles "
                "roles WHERE roles.subject_ref = :evaluation_attempt_ref OR "
                "(roles.subject_ref = :variant_run_ref AND roles.role = "
                "'checkpoint_artifact')) ORDER BY assets.asset_ref"
            ),
            native_parameters,
        ).all()
        authority = (
            measurement_authorities[0]
            if len(measurement_authorities) == 1
            else None
        )
        authority_native_rows: dict[str, list[object]] = {}
        if authority is not None:
            for name, statement, value in (
                (
                    "baseline",
                    text(
                        "SELECT * FROM rg_experiment_baselines WHERE "
                        "baseline_ref = :value"
                    ),
                    authority.baseline_ref,
                ),
                (
                    "variant",
                    text(
                        "SELECT * FROM rg_experiment_variants WHERE "
                        "variant_ref = :value"
                    ),
                    authority.variant_ref,
                ),
                (
                    "evaluation_protocol",
                    text(
                        "SELECT * FROM rg_evaluation_protocols WHERE "
                        "evaluation_protocol_ref = :value"
                    ),
                    authority.evaluation_protocol_ref,
                ),
                (
                    "protocol_version",
                    text(
                        "SELECT * FROM rg_protocol_versions WHERE "
                        "protocol_version_ref = :value"
                    ),
                    authority.protocol_version_ref,
                ),
                (
                    "evaluation",
                    text(
                        "SELECT * FROM rg_evaluations WHERE "
                        "evaluation_ref = :value"
                    ),
                    authority.evaluation_ref,
                ),
            ):
                authority_native_rows[name] = connection.execute(
                    statement,
                    {"value": value},
                ).all()
        target_rows = connection.execute(
            text("SELECT * FROM rg_targets WHERE target_ref = :target_ref"),
            native_parameters,
        ).all()
        target_spec_rows = connection.execute(
            text(
                "SELECT * FROM rg_target_spec_acceptances WHERE target_ref = "
                ":target_ref"
            ),
            native_parameters,
        ).all()
        target_graphs = (
            []
            if not target_rows
            else connection.execute(
                text(
                    "SELECT * FROM rg_target_graphs WHERE graph_ref = "
                    ":graph_ref"
                ),
                {"graph_ref": target_rows[0].graph_ref},
            ).all()
        )
        target_graph_appends = (
            []
            if not target_rows or target_rows[0].append_ref is None
            else connection.execute(
                text(
                    "SELECT * FROM rg_target_graph_appends WHERE append_ref = "
                    ":append_ref"
                ),
                {"append_ref": target_rows[0].append_ref},
            ).all()
        )
        plan_documents = (
            []
            if not target_graphs
            else connection.execute(
                text(
                    "SELECT * FROM rm_plan_documents WHERE content_ref = "
                    ":content_ref"
                ),
                {"content_ref": target_graphs[0].plan_content_ref},
            ).all()
        )
        if any(
            len(rows) != 1
            for rows in (
                generic_bindings,
                result_manifests,
                metric_results,
                measurement_authorities,
                variant_runs,
                evaluation_attempts,
                execution_inputs,
                execution_eligibilities,
                target_rows,
                target_spec_rows,
                target_graphs,
                plan_documents,
            )
        ) or any(len(rows) != 1 for rows in authority_native_rows.values()):
            raise OwnerConflict("target_native_execution_closure_source_missing")

        harness_runs = connection.execute(
            text(
                "SELECT request_ref, idempotency_key, request_json, "
                "request_hash, run_ref, attempt_ref, attempt_generation, "
                "root_session_ref, native_session_ref, fence_ref, "
                "harness_family, model_ref, auth_profile_ref, "
                "capability_binding_hash, mcp_binding_json, mcp_binding_hash, "
                "profile_json, profile_hash, failure_code, status, created_at, "
                "updated_at, completed_at, pending_recovery_ref, "
                "pending_recovery_old_handle_json, "
                "pending_recovery_old_handle_hash, "
                "pending_recovery_generation, pending_recovery_binding_hash "
                "FROM ar_harness_runs WHERE run_ref = :target_run_ref"
            ),
            parameters,
        ).all()
        admissions = connection.execute(
            text(
                "SELECT target_run_ref, target_ref, harness_request_ref, "
                "harness_family, model_ref, auth_profile_ref, "
                "full_conformance_binding_json, "
                "full_conformance_binding_hash, target_scope_binding_hash, "
                "idempotency_key, request_hash, admitted_at FROM "
                "ar_target_harness_admissions WHERE target_run_ref = "
                ":target_run_ref"
            ),
            parameters,
        ).all()
        launches = connection.execute(
            text(
                "SELECT launch_ref, target_ref, target_run_ref, status FROM "
                "ar_target_launches WHERE target_ref = :target_ref"
            ),
            parameters,
        ).all()
        provider_operations = connection.execute(
            text(
                "SELECT operation_ref, run_ref, generation, invocation_hash, "
                "status, outcome_code, created_at, completed_at FROM "
                "ar_harness_provider_operations WHERE operation_ref = "
                ":operation_ref"
            ),
            {"operation_ref": operation_ref},
        ).all()
        if any(
            len(rows) != 1
            for rows in (
                harness_runs,
                admissions,
                launches,
                provider_operations,
            )
        ):
            raise OwnerConflict("target_native_execution_closure_source_missing")

        workspaces = connection.execute(
            text(
                "SELECT workspace_ref, target_ref, target_run_ref, "
                "root_session_ref, target_attempt_ref, target_fence_ref, "
                "ordinal, root_name, status, payload_json, payload_hash, "
                "idempotency_key, request_hash, receipt_ref, receipt_hash, "
                "created_at FROM ar_target_run_workspaces WHERE "
                "target_run_ref = :target_run_ref ORDER BY ordinal, "
                "workspace_ref"
            ),
            parameters,
        ).all()
        evidence_events = connection.execute(
            text(
                "SELECT event_ref, operation_ref, sequence, summary_json, "
                "summary_hash, recorded_at FROM ar_harness_evidence_events "
                "WHERE operation_ref = :operation_ref ORDER BY sequence, "
                "event_ref"
            ),
            {"operation_ref": operation_ref},
        ).all()
        child_sessions = connection.execute(
            text(
                "SELECT child_session_ref, target_run_ref, review_kind, "
                "harness_operation_ref, parent_root_session_ref, "
                "native_parent_session_ref, native_child_session_ref, "
                "spawn_evidence_ref, completion_evidence_ref, payload_hash, "
                "status, reserved_at, bound_at FROM "
                "ar_target_harness_child_sessions WHERE target_run_ref = "
                ":target_run_ref AND review_kind IN ('code', 'result') ORDER "
                "BY review_kind, child_session_ref"
            ),
            parameters,
        ).all()
        preflights = connection.execute(
            text(
                "SELECT target_ref, ordinal, implementation_revision_ref, "
                "preflight_json, preflight_hash, review_scope_json, "
                "review_scope_hash, recorded_at FROM ar_target_run_preflights "
                "WHERE target_ref = :target_ref ORDER BY ordinal"
            ),
            parameters,
        ).all()
        code_reviews = connection.execute(
            text(
                "SELECT review_ref, target_ref, target_run_ref, review_kind, "
                "subject_ref, parent_session_ref, reviewer_session_ref, "
                "reviewer_spawn_evidence_ref, "
                "reviewer_completion_evidence_ref, harness_operation_ref, "
                "payload_json, payload_hash, evidence_content_hash, "
                "idempotency_key, request_hash, receipt_ref, receipt_hash, "
                "accepted_at FROM ar_target_review_evidence WHERE "
                "target_run_ref = :target_run_ref AND review_kind = 'code' "
                "ORDER BY review_ref"
            ),
            parameters,
        ).all()
        token = canonical_hash(
            {
                "schema_ref": (
                    "meta-research/target-native-closure-source/v1"
                ),
                "target_ref": handle.target_ref,
                "target_run_ref": handle.target_run_ref,
                "evaluation_attempt_ref": evaluation_attempt_ref,
                "generic_execution_binding": plain_rows(generic_bindings),
                "generic_result_manifest": plain_rows(result_manifests),
                "measurement_attempt": plain_rows(measurement_attempts),
                "formal_metric": plain_rows(metric_results),
                "measurement_authority": plain_rows(measurement_authorities),
                "authority_native_rows": {
                    name: plain_rows(rows)
                    for name, rows in sorted(authority_native_rows.items())
                },
                "variant_run": plain_rows(variant_runs),
                "evaluation_attempt": plain_rows(evaluation_attempts),
                "experiment_input_bindings": plain_rows(
                    experiment_input_bindings
                ),
                "asset_roles": plain_rows(asset_roles),
                "checkpoint_roles": plain_rows(checkpoint_roles),
                "execution_input": plain_rows(execution_inputs),
                "execution_eligibility": plain_rows(
                    execution_eligibilities
                ),
                "accepted_assets": plain_rows(accepted_assets),
                "asset_custodies": plain_rows(asset_custodies),
                "asset_roots": plain_rows(asset_roots),
                "target": plain_rows(target_rows),
                "target_spec_acceptance": plain_rows(target_spec_rows),
                "target_graph": plain_rows(target_graphs),
                "target_graph_append": plain_rows(target_graph_appends),
                "plan_document": plain_rows(plan_documents),
                "result_review": plain_rows(result_reviews),
                "harness_run": plain_rows(harness_runs),
                "harness_admission": plain_rows(admissions),
                "launch_binding": plain_rows(launches),
                "workspaces": plain_rows(workspaces),
                "provider_operation": plain_rows(provider_operations),
                "evidence_events": plain_rows(evidence_events),
                "child_sessions": plain_rows(child_sessions),
                "preflights": plain_rows(preflights),
                "code_reviews": plain_rows(code_reviews),
            }
        )
        return str(result_review.review_ref), str(operation_ref), token

    def _native_closure_cached_sources_match(
        self,
        connection,
        *,
        binding,
        manifest,
        attempt,
        metric,
        review_row,
        review_payload: object,
        review_receipt: AcceptanceReceipt,
    ) -> bool:
        """Bind cached public projections to exact rows in the write snapshot."""

        generic_source = connection.execute(
            text(
                "SELECT * FROM rg_target_generic_execution_bindings_v3 WHERE "
                "binding_ref = :binding_ref"
            ),
            {"binding_ref": binding.binding_ref},
        ).first()
        manifest_source = connection.execute(
            text(
                "SELECT * FROM rm_target_generic_result_manifests WHERE "
                "manifest_ref = :manifest_ref"
            ),
            {"manifest_ref": manifest.manifest_ref},
        ).first()
        attempt_source = connection.execute(
            text(
                "SELECT * FROM rg_target_measurement_attempt_bindings WHERE "
                "attempt_binding_ref = :attempt_binding_ref"
            ),
            {"attempt_binding_ref": attempt.attempt_binding_ref},
        ).first()
        metric_source = connection.execute(
            text(
                "SELECT * FROM rg_metric_results WHERE metric_result_ref = "
                ":metric_result_ref"
            ),
            {"metric_result_ref": metric.metric_result_ref},
        ).first()
        current_review_source = connection.execute(
            text(
                "SELECT * FROM ar_target_review_evidence WHERE review_ref = "
                ":review_ref AND review_kind = 'result'"
            ),
            {"review_ref": review_row.review_ref},
        ).first()
        if any(
            row is None
            for row in (
                generic_source,
                manifest_source,
                attempt_source,
                metric_source,
                current_review_source,
            )
        ):
            return False
        return not (
            generic_source.binding_ref != binding.binding_ref
            or int(generic_source.ordinal) != binding.ordinal
            or generic_source.target_ref != binding.target_ref
            or generic_source.target_run_ref != binding.target_run_ref
            or generic_source.target_attempt_ref != binding.target_attempt_ref
            or generic_source.target_fence_ref != binding.target_fence_ref
            or generic_source.input_binding_ref != binding.input_binding_ref
            or generic_source.input_binding_receipt_ref
            != binding.input_binding_receipt.receipt_ref
            or generic_source.input_binding_receipt_hash
            != canonical_hash(
                projection_plain_value(binding.input_binding_receipt)
            )
            or generic_source.execution_eligibility_ref
            != binding.execution_eligibility_ref
            or generic_source.execution_eligibility_receipt_ref
            != binding.execution_eligibility_receipt.receipt_ref
            or generic_source.execution_eligibility_receipt_hash
            != canonical_hash(
                projection_plain_value(binding.execution_eligibility_receipt)
            )
            or generic_source.operation_handle != binding.operation_handle
            or generic_source.execution_request_ref
            != binding.execution_request_ref
            or generic_source.operation_request_hash != binding.request_hash
            or generic_source.command_spec_hash != binding.command_spec_hash
            or generic_source.terminal_status != binding.terminal_status
            or generic_source.exit_receipt_ref != binding.exit_receipt_ref
            or generic_source.exit_receipt_hash != binding.exit_receipt_hash
            or bool(generic_source.process_tree_drained)
            is not binding.process_tree_drained
            or bool(generic_source.currentness_known)
            is not binding.currentness_known
            or bool(generic_source.current) is not binding.current
            or generic_source.receipt_ref != binding.receipt.receipt_ref
            or generic_source.receipt_hash != binding.receipt.payload_hash
            or float(generic_source.accepted_at) != binding.accepted_at
            or manifest_source.manifest_ref != manifest.manifest_ref
            or manifest_source.target_ref != manifest.target_ref
            or manifest_source.target_run_ref != manifest.target_run_ref
            or manifest_source.target_attempt_ref != manifest.target_attempt_ref
            or manifest_source.target_fence_ref != manifest.target_fence_ref
            or manifest_source.generic_binding_ref
            != manifest.generic_binding_ref
            or manifest_source.operation_handle != manifest.operation_handle
            or manifest_source.roles_json
            != canonical_json(projection_plain_value(manifest.entries))
            or manifest_source.roles_hash
            != canonical_hash(projection_plain_value(manifest.entries))
            or manifest_source.payload_hash != manifest.payload_hash
            or manifest_source.receipt_ref != manifest.receipt.receipt_ref
            or manifest_source.receipt_hash != manifest.receipt.payload_hash
            or float(manifest_source.accepted_at) != manifest.accepted_at
            or attempt_source.attempt_binding_ref
            != attempt.attempt_binding_ref
            or attempt_source.target_ref != attempt.target_ref
            or attempt_source.target_run_ref != attempt.target_run_ref
            or attempt_source.target_attempt_ref != attempt.target_attempt_ref
            or attempt_source.target_fence_ref != attempt.target_fence_ref
            or attempt_source.authority_ref != attempt.authority_ref
            or attempt_source.authority_hash != attempt.authority_hash
            or attempt_source.generic_binding_ref
            != attempt.generic_binding_ref
            or attempt_source.manifest_ref != attempt.manifest_ref
            or attempt_source.variant_run_ref != attempt.variant_run_ref
            or attempt_source.variant_run_disposition
            != attempt.variant_run_disposition
            or attempt_source.evaluation_attempt_ref
            != attempt.evaluation_attempt_ref
            or attempt_source.variant_input_binding_ref
            != attempt.variant_run_input_binding.binding_ref
            or attempt_source.evaluation_input_binding_ref
            != attempt.evaluation_attempt_input_binding.binding_ref
            or attempt_source.checkpoint_role_refs_json
            != canonical_json(list(attempt.checkpoint_role_refs))
            or attempt_source.checkpoint_role_refs_hash
            != canonical_hash(list(attempt.checkpoint_role_refs))
            or attempt_source.result_role_ref != attempt.result_role_ref
            or attempt_source.payload_hash != attempt.payload_hash
            or attempt_source.receipt_ref != attempt.receipt.receipt_ref
            or attempt_source.receipt_hash != attempt.receipt.payload_hash
            or float(attempt_source.accepted_at) != attempt.accepted_at
            or metric_source.metric_result_ref != metric.metric_result_ref
            or metric_source.evaluation_attempt_ref
            != metric.evaluation_attempt_ref
            or metric_source.result_role_ref != metric.result_role_ref
            or metric_source.metrics_json != canonical_json(metric.metrics)
            or metric_source.metrics_hash != metric.metrics_hash
            or metric_source.receipt_ref != metric.receipt.receipt_ref
            or metric_source.receipt_hash != metric.receipt.payload_hash
            or tuple(current_review_source) != tuple(review_row)
            or current_review_source.payload_json
            != canonical_json(review_payload)
            or current_review_source.payload_hash
            != canonical_hash(review_payload)
            or current_review_source.receipt_ref != review_receipt.receipt_ref
            or current_review_source.receipt_hash
            != review_receipt.payload_hash
        )

    def accept_target_native_execution_closure(
        self,
        *,
        target_ref: str,
        evaluation_attempt_ref: str,
        idempotency_key: str,
    ) -> AcceptedTargetNativeExecutionClosure:
        """Close one current Target attempt over native RG measurement facts."""

        if (
            type(target_ref) is not str
            or not target_ref
            or type(evaluation_attempt_ref) is not str
            or not evaluation_attempt_ref
            or type(idempotency_key) is not str
            or not idempotency_key
            or len(idempotency_key.encode("utf-8")) > 128
        ):
            raise OwnerConflict("target_native_execution_closure_invalid")
        with self._database.read() as connection:
            replay = connection.execute(
                text(
                    "SELECT closure_ref FROM "
                    "ar_target_native_execution_closures WHERE "
                    "idempotency_key = :idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
        if replay is not None:
            accepted = self.query_target_native_execution_closure(
                replay.closure_ref
            )
            if accepted is None:
                raise OwnerConflict(
                    "target_native_execution_closure_integrity_invalid"
                )
            if (
                accepted.target_ref != target_ref
                or accepted.evaluation_attempt_ref != evaluation_attempt_ref
            ):
                raise OwnerConflict("target_native_execution_closure_conflict")
            return accepted
        handle = self.query_current_target_work_handle(target_ref)
        if handle is None:
            raise OwnerConflict("target_native_execution_closure_source_missing")
        with self._database.read() as connection:
            connection.exec_driver_sql("BEGIN")
            source_before = self._native_closure_source_snapshot(
                connection,
                handle=handle,
                evaluation_attempt_ref=evaluation_attempt_ref,
            )
        attempt = self._domain_reader.query_target_measurement_attempt(
            evaluation_attempt_ref
        )
        metric = self._domain_reader.query_target_formal_metric_result(
            evaluation_attempt_ref
        )
        if attempt is None or metric is None:
            raise OwnerConflict("target_native_execution_closure_source_missing")
        binding = self._graph.query_generic_execution_binding(
            attempt.generic_binding_ref
        )
        manifest = self._memory.query_generic_result_manifest(
            attempt.manifest_ref
        )
        if (
            binding is None
            or manifest is None
            or (
                attempt.target_ref,
                attempt.target_run_ref,
                attempt.target_attempt_ref,
                attempt.target_fence_ref,
                attempt.evaluation_attempt_ref,
            )
            != (
                handle.target_ref,
                handle.target_run_ref,
                handle.execution_attempt_ref,
                handle.execution_fence_ref,
                evaluation_attempt_ref,
            )
            or binding.target_ref != handle.target_ref
            or binding.target_run_ref != handle.target_run_ref
            or binding.target_attempt_ref != handle.execution_attempt_ref
            or binding.target_fence_ref != handle.execution_fence_ref
            or binding.input_binding_ref
            != handle.execution_input_binding_ref
            or manifest.generic_binding_ref != binding.binding_ref
            or manifest.manifest_ref != attempt.manifest_ref
            or getattr(metric, "evaluation_attempt_ref", None)
            != evaluation_attempt_ref
            or getattr(metric, "result_role_ref", None)
            != attempt.result_role_ref
        ):
            raise OwnerConflict("target_native_execution_closure_binding_invalid")
        preflights = self._accepted_preflights(target_ref)
        review_row, review_payload, review_receipt = self._review_acceptance(
            target_run_ref=handle.target_run_ref,
            review_kind="result",
            subject_ref=evaluation_attempt_ref,
        )
        try:
            result_review = _decode_bundle_value(
                review_payload,
                ResultReviewRecord,
            )
        except (TypeError, ValueError) as error:
            raise OwnerConflict(
                "target_native_execution_closure_review_invalid"
            ) from error
        if type(result_review) is not ResultReviewRecord:
            raise OwnerConflict("target_native_execution_closure_review_invalid")
        validate_result_review(
            result_review,
            target_root_session_ref=handle.root_session_ref,
            evaluation_attempt_ref=evaluation_attempt_ref,
            metric_result_ref=getattr(metric, "metric_result_ref"),
            asset_manifest_ref=manifest.manifest_ref,
            code_review_preflights=preflights,
        )
        review_evidence = self._review_evidence(
            handle=handle,
            operation_ref=review_row.harness_operation_ref,
            review_kind="result",
            expected_review=review_payload,
            bind_domain_session=False,
        )
        if (
            review_row.target_ref != handle.target_ref
            or review_row.target_run_ref != handle.target_run_ref
            or review_row.review_kind != "result"
            or review_row.subject_ref != evaluation_attempt_ref
            or review_row.parent_session_ref != handle.root_session_ref
            or review_row.reviewer_session_ref
            != review_evidence["domain_child_session_ref"]
            or review_row.reviewer_spawn_evidence_ref
            != review_evidence["spawn_evidence_ref"]
            or review_row.reviewer_completion_evidence_ref
            != review_evidence["completion_evidence_ref"]
        ):
            raise OwnerConflict("target_native_execution_closure_review_invalid")
        with self._database.read() as connection:
            code_review_rows = connection.execute(
                text(
                    "SELECT reviewer_session_ref, reviewer_spawn_evidence_ref "
                    "FROM ar_target_review_evidence WHERE target_run_ref = "
                    ":run_ref AND review_kind = 'code'"
                ),
                {"run_ref": handle.target_run_ref},
            ).all()
        if any(
            result_review.reviewer_session_ref == row.reviewer_session_ref
            or result_review.reviewer_spawn_evidence_ref
            == row.reviewer_spawn_evidence_ref
            for row in code_review_rows
        ):
            raise OwnerConflict("target_native_execution_closure_review_invalid")
        with self._database.read() as connection:
            connection.exec_driver_sql("BEGIN")
            verified_source = self._native_closure_source_snapshot(
                connection,
                handle=handle,
                evaluation_attempt_ref=evaluation_attempt_ref,
            )
        if (
            verified_source != source_before
            or verified_source[0] != review_row.review_ref
            or verified_source[1] != review_row.harness_operation_ref
        ):
            raise OwnerConflict("target_native_execution_closure_stale")
        payload = {
            "target_ref": handle.target_ref,
            "target_run_ref": handle.target_run_ref,
            "target_attempt_ref": handle.execution_attempt_ref,
            "target_fence_ref": handle.execution_fence_ref,
            "generic_execution": projection_plain_value(binding),
            "result_manifest": projection_plain_value(manifest),
            "measurement_attempt": projection_plain_value(attempt),
            "formal_metric": metric.as_public_dict(),
            "result_review": review_payload,
            "result_review_acceptance_receipt": review_receipt.as_public_dict(),
        }
        payload_hash = canonical_hash(payload)
        request_hash = canonical_hash(
            {
                "command": "accept_target_native_execution_closure",
                "payload": payload,
            }
        )
        now = time.time()
        from sqlalchemy.exc import OperationalError

        try:
            with self._database.fenced_write() as connection:
                from meta_research.owners.agent_runtime import (
                    verify_current_target_run_frontier_in_transaction,
                )

                verify_current_target_run_frontier_in_transaction(
                    connection,
                    handle,
                )
                if self._native_closure_source_snapshot(
                    connection,
                    handle=handle,
                    evaluation_attempt_ref=evaluation_attempt_ref,
                ) != verified_source:
                    raise OwnerConflict("target_native_execution_closure_stale")
                if not self._native_closure_cached_sources_match(
                    connection,
                    binding=binding,
                    manifest=manifest,
                    attempt=attempt,
                    metric=metric,
                    review_row=review_row,
                    review_payload=review_payload,
                    review_receipt=review_receipt,
                ):
                    raise OwnerConflict("target_native_execution_closure_stale")
                child_binding = connection.execute(
                    text(
                        "SELECT * FROM ar_target_harness_child_sessions WHERE "
                        "harness_operation_ref = :operation_ref"
                    ),
                    {"operation_ref": review_row.harness_operation_ref},
                ).first()
                if (
                    child_binding is None
                    or child_binding.status != "bound"
                    or child_binding.target_run_ref != handle.target_run_ref
                    or child_binding.review_kind != "result"
                    or child_binding.parent_root_session_ref
                    != handle.root_session_ref
                    or child_binding.child_session_ref
                    != review_evidence["domain_child_session_ref"]
                    or child_binding.native_parent_session_ref
                    != review_evidence["parent_session_ref"]
                    or child_binding.native_child_session_ref
                    != review_evidence["child_session_ref"]
                    or child_binding.native_child_session_ref
                    != review_evidence["review_actor_session_ref"]
                    or child_binding.spawn_evidence_ref
                    != review_evidence["spawn_evidence_ref"]
                    or child_binding.completion_evidence_ref
                    != review_evidence["completion_evidence_ref"]
                    or child_binding.payload_hash != review_evidence["payload_hash"]
                ):
                    raise OwnerConflict("target_native_execution_closure_stale")
                row = connection.execute(
                    text(
                        "SELECT * FROM ar_target_native_execution_closures "
                        "WHERE idempotency_key = :key OR generic_binding_ref = "
                        ":generic_binding_ref OR attempt_binding_ref = "
                        ":attempt_binding_ref OR evaluation_attempt_ref = "
                        ":evaluation_attempt_ref OR metric_result_ref = "
                        ":metric_result_ref OR result_review_ref = "
                        ":result_review_ref"
                    ),
                    {
                        "key": idempotency_key,
                        "generic_binding_ref": binding.binding_ref,
                        "attempt_binding_ref": attempt.attempt_binding_ref,
                        "evaluation_attempt_ref": evaluation_attempt_ref,
                        "metric_result_ref": metric.metric_result_ref,
                        "result_review_ref": review_row.review_ref,
                    },
                ).first()
                if row is not None:
                    if row.request_hash != request_hash:
                        raise OwnerConflict(
                            "target_native_execution_closure_conflict"
                        )
                    closure_ref = row.closure_ref
                else:
                    bridge_row = connection.execute(
                        text(
                            "SELECT target_ref, target_run_ref, "
                            "target_attempt_ref, target_fence_ref, "
                            "generic_binding_ref, manifest_ref, result_role_ref "
                            "FROM rg_target_measurement_attempt_bindings WHERE "
                            "attempt_binding_ref = :attempt_binding_ref"
                        ),
                        {"attempt_binding_ref": attempt.attempt_binding_ref},
                    ).first()
                    metric_row = connection.execute(
                        text(
                            "SELECT metric_result_ref, result_role_ref, "
                            "metrics_hash, run_ref, execution_attempt_ref, "
                            "fence_ref, receipt_hash FROM rg_metric_results "
                            "WHERE evaluation_attempt_ref = "
                            ":evaluation_attempt_ref"
                        ),
                        {"evaluation_attempt_ref": evaluation_attempt_ref},
                    ).first()
                    review_source = connection.execute(
                        text(
                            "SELECT review_ref, payload_hash, receipt_hash FROM "
                            "ar_target_review_evidence WHERE review_ref = "
                            ":review_ref AND review_kind = 'result'"
                        ),
                        {"review_ref": review_row.review_ref},
                    ).first()
                    if (
                        bridge_row is None
                        or bridge_row.target_ref != handle.target_ref
                        or bridge_row.target_run_ref != handle.target_run_ref
                        or bridge_row.target_attempt_ref
                        != handle.execution_attempt_ref
                        or bridge_row.target_fence_ref
                        != handle.execution_fence_ref
                        or bridge_row.generic_binding_ref != binding.binding_ref
                        or bridge_row.manifest_ref != manifest.manifest_ref
                        or bridge_row.result_role_ref != metric.result_role_ref
                        or metric_row is None
                        or metric_row.metric_result_ref != metric.metric_result_ref
                        or metric_row.result_role_ref != metric.result_role_ref
                        or metric_row.metrics_hash != metric.metrics_hash
                        or metric_row.run_ref != handle.target_run_ref
                        or metric_row.execution_attempt_ref
                        != handle.execution_attempt_ref
                        or metric_row.fence_ref != handle.execution_fence_ref
                        or metric_row.receipt_hash != metric.receipt.payload_hash
                        or review_source is None
                        or review_source.payload_hash
                        != canonical_hash(review_payload)
                        or review_source.receipt_hash != review_receipt.payload_hash
                    ):
                        raise OwnerConflict("target_native_execution_closure_stale")
                    closure_ref = new_ref("target_native_execution_closure")
                    receipt = _receipt(
                        "agent_runtime",
                        AR_TARGET_NATIVE_EXECUTION_CLOSURE_RECEIPT_KIND,
                        new_ref("ar_target_native_execution_closure_receipt"),
                        handle.execution_attempt_ref,
                        {
                            "closure_ref": closure_ref,
                            "payload_hash": payload_hash,
                            **payload,
                        },
                    )
                    connection.execute(
                        text(
                            "INSERT INTO ar_target_native_execution_closures "
                            "(closure_ref, target_ref, target_run_ref, "
                            "target_attempt_ref, target_fence_ref, "
                            "generic_binding_ref, manifest_ref, "
                            "attempt_binding_ref, evaluation_attempt_ref, "
                            "metric_result_ref, result_review_ref, payload_json, "
                            "payload_hash, idempotency_key, request_hash, "
                            "receipt_ref, receipt_hash, accepted_at) VALUES "
                            "(:closure_ref, :target_ref, :target_run_ref, "
                            ":target_attempt_ref, :target_fence_ref, "
                            ":generic_binding_ref, :manifest_ref, "
                            ":attempt_binding_ref, :evaluation_attempt_ref, "
                            ":metric_result_ref, :result_review_ref, "
                            ":payload_json, :payload_hash, :idempotency_key, "
                            ":request_hash, :receipt_ref, :receipt_hash, "
                            ":accepted_at)"
                        ),
                        {
                            "closure_ref": closure_ref,
                            "target_ref": handle.target_ref,
                            "target_run_ref": handle.target_run_ref,
                            "target_attempt_ref": handle.execution_attempt_ref,
                            "target_fence_ref": handle.execution_fence_ref,
                            "generic_binding_ref": binding.binding_ref,
                            "manifest_ref": manifest.manifest_ref,
                            "attempt_binding_ref": attempt.attempt_binding_ref,
                            "evaluation_attempt_ref": evaluation_attempt_ref,
                            "metric_result_ref": metric.metric_result_ref,
                            "result_review_ref": review_row.review_ref,
                            "payload_json": canonical_json(payload),
                            "payload_hash": payload_hash,
                            "idempotency_key": idempotency_key,
                            "request_hash": request_hash,
                            "receipt_ref": receipt.receipt_ref,
                            "receipt_hash": receipt.payload_hash,
                            "accepted_at": now,
                        },
                    )
                    connection.execute(
                        text(
                            "UPDATE agent_runtime_state SET revision = "
                            "revision + 1, "
                            "target_native_execution_closure_count = "
                            "target_native_execution_closure_count + 1 WHERE "
                            "singleton = 'owner'"
                        )
                    )
                    self._feed.record(
                        connection,
                        "agent_runtime.target_native_execution_closed",
                        {
                            "closure_ref": closure_ref,
                            "target_ref": target_ref,
                            "evaluation_attempt_ref": evaluation_attempt_ref,
                            "metric_result_ref": metric.metric_result_ref,
                            "receipt_ref": receipt.receipt_ref,
                        },
                    )
        except IntegrityError as error:
            raise OwnerConflict("target_native_execution_closure_conflict") from error
        except OperationalError as error:
            import sqlite3

            sqlite_error_code = getattr(error.orig, "sqlite_errorcode", None)
            if sqlite_error_code in {
                sqlite3.SQLITE_BUSY,
                getattr(sqlite3, "SQLITE_BUSY_SNAPSHOT", 517),
            }:
                raise OwnerConflict(
                    "target_native_execution_closure_stale"
                ) from error
            raise
        accepted = self.query_target_native_execution_closure(closure_ref)
        if accepted is None:
            raise OwnerConflict("target_native_execution_closure_missing")
        return accepted

    def query_target_native_execution_closure(
        self,
        closure_ref: str,
    ) -> AcceptedTargetNativeExecutionClosure | None:
        if type(closure_ref) is not str or not closure_ref:
            raise OwnerConflict("target_native_execution_closure_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ar_target_native_execution_closures WHERE "
                    "closure_ref = :closure_ref"
                ),
                {"closure_ref": closure_ref},
            ).first()
        if row is None:
            return None
        handle = self.query_current_target_work_handle(row.target_ref)
        attempt = self._domain_reader.query_target_measurement_attempt(
            row.evaluation_attempt_ref
        )
        metric = self._domain_reader.query_target_formal_metric_result(
            row.evaluation_attempt_ref
        )
        if handle is None or attempt is None or metric is None:
            raise OwnerConflict(
                "target_native_execution_closure_integrity_invalid"
            )
        binding = self._graph.query_generic_execution_binding(
            row.generic_binding_ref
        )
        manifest = self._memory.query_generic_result_manifest(row.manifest_ref)
        review_row, review_payload, review_receipt = self._review_acceptance(
            target_run_ref=row.target_run_ref,
            review_kind="result",
            subject_ref=row.evaluation_attempt_ref,
        )
        try:
            stored_payload = json.loads(row.payload_json)
            result_review = _decode_bundle_value(
                review_payload,
                ResultReviewRecord,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise OwnerConflict(
                "target_native_execution_closure_integrity_invalid"
            ) from error
        if binding is None or manifest is None or type(result_review) is not ResultReviewRecord:
            raise OwnerConflict(
                "target_native_execution_closure_integrity_invalid"
            )
        review_evidence = self._review_evidence(
            handle=handle,
            operation_ref=review_row.harness_operation_ref,
            review_kind="result",
            expected_review=review_payload,
            bind_domain_session=False,
        )
        validate_result_review(
            result_review,
            target_root_session_ref=handle.root_session_ref,
            evaluation_attempt_ref=row.evaluation_attempt_ref,
            metric_result_ref=metric.metric_result_ref,
            asset_manifest_ref=manifest.manifest_ref,
            code_review_preflights=self._accepted_preflights(row.target_ref),
        )
        expected_payload = {
            "target_ref": handle.target_ref,
            "target_run_ref": handle.target_run_ref,
            "target_attempt_ref": handle.execution_attempt_ref,
            "target_fence_ref": handle.execution_fence_ref,
            "generic_execution": projection_plain_value(binding),
            "result_manifest": projection_plain_value(manifest),
            "measurement_attempt": projection_plain_value(attempt),
            "formal_metric": metric.as_public_dict(),
            "result_review": review_payload,
            "result_review_acceptance_receipt": review_receipt.as_public_dict(),
        }
        payload_hash = canonical_hash(expected_payload)
        receipt = _receipt(
            "agent_runtime",
            AR_TARGET_NATIVE_EXECUTION_CLOSURE_RECEIPT_KIND,
            row.receipt_ref,
            row.target_attempt_ref,
            {
                "closure_ref": closure_ref,
                "payload_hash": payload_hash,
                **expected_payload,
            },
        )
        if (
            stored_payload != expected_payload
            or row.payload_hash != payload_hash
            or row.request_hash
            != canonical_hash(
                {
                    "command": "accept_target_native_execution_closure",
                    "payload": expected_payload,
                }
            )
            or row.receipt_hash != receipt.payload_hash
            or row.target_ref != handle.target_ref
            or row.target_run_ref != handle.target_run_ref
            or row.target_attempt_ref != handle.execution_attempt_ref
            or row.target_fence_ref != handle.execution_fence_ref
            or row.generic_binding_ref != binding.binding_ref
            or row.manifest_ref != manifest.manifest_ref
            or row.attempt_binding_ref != attempt.attempt_binding_ref
            or row.evaluation_attempt_ref != attempt.evaluation_attempt_ref
            or row.metric_result_ref != metric.metric_result_ref
            or row.result_review_ref != review_row.review_ref
            or review_row.target_ref != handle.target_ref
            or review_row.reviewer_session_ref
            != review_evidence["domain_child_session_ref"]
            or review_row.reviewer_spawn_evidence_ref
            != review_evidence["spawn_evidence_ref"]
            or review_row.reviewer_completion_evidence_ref
            != review_evidence["completion_evidence_ref"]
        ):
            raise OwnerConflict(
                "target_native_execution_closure_integrity_invalid"
            )
        return AcceptedTargetNativeExecutionClosure(
            closure_ref=closure_ref,
            target_ref=row.target_ref,
            target_run_ref=row.target_run_ref,
            target_attempt_ref=row.target_attempt_ref,
            target_fence_ref=row.target_fence_ref,
            generic_binding_ref=row.generic_binding_ref,
            result_manifest_ref=row.manifest_ref,
            attempt_binding_ref=row.attempt_binding_ref,
            evaluation_attempt_ref=row.evaluation_attempt_ref,
            metric_result_ref=row.metric_result_ref,
            result_review_ref=row.result_review_ref,
            payload_hash=payload_hash,
            receipt=receipt,
            accepted_at=float(row.accepted_at),
        )

    def query_target_native_execution_closure_for_attempt(
        self,
        evaluation_attempt_ref: str,
    ) -> AcceptedTargetNativeExecutionClosure | None:
        """Reconcile the unique native closure for one EvaluationAttempt."""

        if type(evaluation_attempt_ref) is not str or not evaluation_attempt_ref:
            raise OwnerConflict("target_native_execution_closure_invalid")
        with self._database.read() as connection:
            rows = connection.execute(
                text(
                    "SELECT closure_ref FROM "
                    "ar_target_native_execution_closures WHERE "
                    "evaluation_attempt_ref = :evaluation_attempt_ref"
                ),
                {"evaluation_attempt_ref": evaluation_attempt_ref},
            ).all()
        if not rows:
            return None
        if len(rows) != 1:
            raise OwnerConflict(
                "target_native_execution_closure_integrity_invalid"
            )
        return self.query_target_native_execution_closure(rows[0].closure_ref)

    def _accepted_preflights(
        self, target_ref: str
    ) -> tuple[TargetExecutionPreflight, ...]:
        with self._database.read() as connection:
            rows = connection.execute(
                text(
                    "SELECT preflight_json, preflight_hash FROM "
                    "ar_target_run_preflights WHERE target_ref = :target_ref "
                    "ORDER BY ordinal"
                ),
                {"target_ref": target_ref},
            ).all()
        try:
            values = tuple(
                _decode_stored_record(
                    row.preflight_json,
                    row.preflight_hash,
                    TargetExecutionPreflight,
                )
                for row in rows
            )
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise OwnerConflict("target_preflight_integrity_invalid") from error
        if not values or any(type(item) is not TargetExecutionPreflight for item in values):
            raise OwnerConflict("target_preflight_integrity_invalid")
        return values

    def query_execution_closure(
        self, closure_ref: str
    ) -> AcceptedTargetExecutionClosure | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ar_target_execution_closures WHERE "
                    "closure_ref = :closure_ref"
                ),
                {"closure_ref": closure_ref},
            ).first()
        if row is None:
            return None
        try:
            payload = json.loads(row.payload_json)
        except (TypeError, ValueError) as error:
            raise OwnerConflict("target_execution_closure_integrity_invalid") from error
        if not isinstance(payload, dict):
            raise OwnerConflict("target_execution_closure_integrity_invalid")
        receipt = _receipt(
            "agent_runtime",
            AR_TARGET_EXECUTION_CLOSURE_RECEIPT_KIND,
            row.receipt_ref,
            row.target_attempt_ref,
            {"closure_ref": closure_ref, "payload_hash": row.payload_hash, **payload},
        )
        if (
            row.payload_hash != canonical_hash(payload)
            or row.request_hash
            != canonical_hash({"command": "accept", "payload": payload})
            or row.receipt_hash != receipt.payload_hash
        ):
            raise OwnerConflict("target_execution_closure_integrity_invalid")
        protected = self._graph.query_protected_execution(row.protected_binding_ref)
        manifest = self._memory.query_result_manifest(row.result_manifest_ref)
        review_row, _review_payload, _review_receipt = self._review_acceptance(
            target_run_ref=row.target_run_ref,
            review_kind="result",
            subject_ref=row.evaluation_attempt_ref,
        )
        metric = self._domain_reader.query_formal_metric_result(
            row.evaluation_attempt_ref
        )
        experiment_run = self._execution_verifier.query_experiment_run(
            row.evaluation_attempt_ref
        )
        execution_receipt = getattr(experiment_run, "execution_receipt", None)
        if (
            protected is None
            or manifest is None
            or metric is None
            or experiment_run is None
            or review_row.review_ref != row.result_review_ref
            or protected.binding_ref != row.protected_binding_ref
            or manifest.manifest_ref != row.result_manifest_ref
            or getattr(metric, "metric_result_ref", None) != row.formal_metric_ref
            or getattr(experiment_run, "run_ref", None) != row.experiment_run_ref
            or getattr(experiment_run, "attempt_ref", None)
            != row.experiment_attempt_ref
            or getattr(experiment_run, "fence_ref", None) != row.experiment_fence_ref
            or getattr(experiment_run, "result_hash", None)
            != row.experiment_result_hash
            or not isinstance(execution_receipt, AcceptanceReceipt)
        ):
            raise OwnerConflict("target_execution_closure_integrity_invalid")
        self._execution_verifier.verify_experiment_execution_receipt(
            run_ref=row.experiment_run_ref,
            attempt_ref=row.experiment_attempt_ref,
            fence_ref=row.experiment_fence_ref,
            evaluation_attempt_ref=row.evaluation_attempt_ref,
            result_hash=row.experiment_result_hash,
            receipt=execution_receipt,
        )
        return AcceptedTargetExecutionClosure(
            closure_ref=closure_ref,
            target_ref=row.target_ref,
            target_run_ref=row.target_run_ref,
            target_attempt_ref=row.target_attempt_ref,
            target_fence_ref=row.target_fence_ref,
            protected_binding_ref=row.protected_binding_ref,
            experiment_run_ref=row.experiment_run_ref,
            experiment_attempt_ref=row.experiment_attempt_ref,
            experiment_fence_ref=row.experiment_fence_ref,
            evaluation_attempt_ref=row.evaluation_attempt_ref,
            experiment_result_hash=row.experiment_result_hash,
            result_manifest_ref=row.result_manifest_ref,
            formal_metric_ref=row.formal_metric_ref,
            result_review_ref=row.result_review_ref,
            payload_hash=row.payload_hash,
            receipt=receipt,
            accepted_at=float(row.accepted_at),
        )

    def verify_execution_closure(
        self,
        *,
        closure_ref: str,
        receipt: AcceptanceReceipt,
    ) -> dict[str, object]:
        """Return issuer-revalidated closure facts for RG formal-v3 commit."""

        native = self.query_target_native_execution_closure(closure_ref)
        if native is not None:
            if native.receipt != receipt:
                raise OwnerConflict("target_execution_closure_receipt_invalid")
            handle = self.query_current_target_work_handle(native.target_ref)
            attempt = self._domain_reader.query_target_measurement_attempt(
                native.evaluation_attempt_ref
            )
            metric = self._domain_reader.query_target_formal_metric_result(
                native.evaluation_attempt_ref
            )
            authority = self._domain_reader.query_target_measurement_domain_authority(
                native.target_ref
            )
            binding = self._graph.query_generic_execution_binding(
                native.generic_binding_ref
            )
            manifest = self._memory.query_generic_result_manifest(
                native.result_manifest_ref
            )
            if any(
                value is None
                for value in (
                    handle,
                    attempt,
                    metric,
                    authority,
                    binding,
                    manifest,
                )
            ):
                raise OwnerConflict(
                    "target_native_execution_closure_integrity_invalid"
                )
            preflights = self._accepted_preflights(native.target_ref)
            eligibility = self.query_execution_eligibility(
                binding.execution_eligibility_ref
            )
            target_input = self._graph.query_execution_input_binding(
                binding.input_binding_ref
            )
            with self._database.read() as connection:
                activation = connection.execute(
                    text(
                        "SELECT candidate_json, candidate_hash, formal_plan_json, "
                        "formal_plan_hash FROM ar_target_run_activations WHERE "
                        "target_ref = :target_ref"
                    ),
                    {"target_ref": native.target_ref},
                ).first()
            try:
                if activation is None:
                    raise ValueError("activation absent")
                candidate = _decode_stored_record(
                    activation.candidate_json,
                    activation.candidate_hash,
                    TargetCandidate,
                )
                formal_plan = _decode_stored_record(
                    activation.formal_plan_json,
                    activation.formal_plan_hash,
                    FormalPlan,
                )
                _review_row, review_payload, review_receipt = (
                    self._review_acceptance(
                        target_run_ref=native.target_run_ref,
                        review_kind="result",
                        subject_ref=native.evaluation_attempt_ref,
                    )
                )
                result_review = _decode_bundle_value(
                    review_payload,
                    ResultReviewRecord,
                )
                result_entries = tuple(
                    entry
                    for entry in manifest.entries
                    if entry.role == "result_content"
                )
                if len(result_entries) != 1:
                    raise ValueError("result content absent")
                result_content = json.loads(
                    self._memory.materialize_generic_result_asset(
                        manifest_ref=manifest.manifest_ref,
                        version_ref=result_entries[0].binding.version_ref,
                    )
                )
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise OwnerConflict(
                    "target_native_execution_closure_integrity_invalid"
                ) from error
            implementation_bundles = tuple(
                self._memory.query_implementation_bundle(
                    preflight.implementation_revision_ref
                )
                for preflight in preflights
            )
            if (
                type(handle) is not TargetWorkHandle
                or type(candidate) is not TargetCandidate
                or type(formal_plan) is not FormalPlan
                or type(result_review) is not ResultReviewRecord
                or type(result_content) is not dict
                or eligibility is None
                or eligibility.handle != handle
                or eligibility.preflight != preflights[-1]
                or target_input is None
                or target_input.proof.binding_ref
                != handle.execution_input_binding_ref
                or binding.input_binding_ref
                != handle.execution_input_binding_ref
                or binding.target_attempt_ref
                != handle.execution_attempt_ref
                or binding.target_fence_ref != handle.execution_fence_ref
                or manifest.generic_binding_ref != binding.binding_ref
                or attempt.attempt_binding_ref != native.attempt_binding_ref
                or attempt.generic_binding_ref != binding.binding_ref
                or attempt.manifest_ref != manifest.manifest_ref
                or metric.metric_result_ref != native.metric_result_ref
                or metric.evaluation_attempt_ref
                != native.evaluation_attempt_ref
                or result_review.reviewed_metric_result_ref
                != metric.metric_result_ref
                or result_review.reviewed_asset_manifest_ref
                != manifest.manifest_ref
                or any(bundle is None for bundle in implementation_bundles)
            ):
                raise OwnerConflict(
                    "target_native_execution_closure_integrity_invalid"
                )
            return {
                "closure": native,
                "handle": handle,
                "candidate": candidate,
                "formal_plan": formal_plan,
                "preflights": preflights,
                "execution_eligibility": eligibility,
                "implementation_bundles": implementation_bundles,
                "generic_execution": binding,
                "target_execution_input": target_input,
                "result_manifest": manifest,
                "measurement_attempt": attempt,
                "formal_metric": metric,
                "measurement_authority": authority,
                "result_content": result_content,
                "result_review": result_review,
                "result_review_acceptance_receipt": review_receipt,
            }

        generic = self.query_generic_execution_closure(closure_ref)
        if generic is not None:
            if generic.receipt != receipt:
                raise OwnerConflict("target_execution_closure_receipt_invalid")
            handle = self.query_current_target_work_handle(generic.target_ref)
            binding = self._graph.query_generic_execution_binding(
                generic.generic_binding_ref
            )
            manifest = self._memory.query_generic_result_manifest(
                generic.result_manifest_ref
            )
            measurement = self._graph.query_generic_measurement(
                generic.measurement_ref
            )
            if handle is None or binding is None or manifest is None or measurement is None:
                raise OwnerConflict("target_generic_execution_closure_integrity_invalid")
            if measurement.receipt.kind == RG_TARGET_GENERIC_MEASUREMENT_RECEIPT_KIND:
                raise OwnerConflict("target_generic_measurement_shadow_read_only")
            eligibility = self.query_execution_eligibility(
                binding.execution_eligibility_ref
            )
            preflights = self._accepted_preflights(generic.target_ref)
            with self._database.read() as connection:
                activation = connection.execute(
                    text(
                        "SELECT candidate_json, candidate_hash, formal_plan_json, "
                        "formal_plan_hash FROM ar_target_run_activations WHERE "
                        "target_ref = :target_ref"
                    ),
                    {"target_ref": generic.target_ref},
                ).first()
            try:
                if activation is None:
                    raise ValueError("activation absent")
                candidate = _decode_stored_record(
                    activation.candidate_json,
                    activation.candidate_hash,
                    TargetCandidate,
                )
                formal_plan = _decode_stored_record(
                    activation.formal_plan_json,
                    activation.formal_plan_hash,
                    FormalPlan,
                )
                _review_row, review_payload, review_receipt = (
                    self._review_acceptance(
                        target_run_ref=generic.target_run_ref,
                        review_kind="result",
                        subject_ref=measurement.evaluation_attempt_ref,
                    )
                )
                result_review = _decode_bundle_value(
                    review_payload, ResultReviewRecord
                )
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise OwnerConflict(
                    "target_generic_execution_closure_integrity_invalid"
                ) from error
            implementation_bundles = tuple(
                self._memory.query_implementation_bundle(
                    preflight.implementation_revision_ref
                )
                for preflight in preflights
            )
            target_input = self._graph.query_execution_input_binding(
                binding.input_binding_ref
            )
            result_entries = tuple(
                entry
                for entry in manifest.entries
                if entry.role == "result_content"
            )
            try:
                if len(result_entries) != 1:
                    raise ValueError("result content absent")
                result_content = json.loads(
                    self._memory.materialize_generic_result_asset(
                        manifest_ref=manifest.manifest_ref,
                        version_ref=result_entries[0].binding.version_ref,
                    )
                )
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise OwnerConflict(
                    "target_generic_execution_closure_integrity_invalid"
                ) from error
            if (
                type(candidate) is not TargetCandidate
                or type(formal_plan) is not FormalPlan
                or type(result_review) is not ResultReviewRecord
                or eligibility is None
                or eligibility.preflight != preflights[-1]
                or eligibility.handle != handle
                or binding.execution_eligibility_ref != eligibility.eligibility_ref
                or binding.input_binding_ref != handle.execution_input_binding_ref
                or manifest.generic_binding_ref != binding.binding_ref
                or measurement.generic_binding_ref != binding.binding_ref
                or measurement.manifest_ref != manifest.manifest_ref
                or measurement.measurement_source_version_ref
                != result_entries[0].binding.version_ref
                or type(result_content) is not dict
                or target_input is None
                or any(bundle is None for bundle in implementation_bundles)
            ):
                raise OwnerConflict(
                    "target_generic_execution_closure_integrity_invalid"
                )
            return {
                "closure": generic,
                "candidate": candidate,
                "formal_plan": formal_plan,
                "preflights": preflights,
                "execution_eligibility": eligibility,
                "implementation_bundles": implementation_bundles,
                "generic_execution": binding,
                "target_execution_input": target_input,
                "result_manifest": manifest,
                "measurement": measurement,
                "result_content": result_content,
                "result_review": result_review,
                "result_review_acceptance_receipt": review_receipt,
            }

        accepted = self.query_execution_closure(closure_ref)
        if accepted is None or accepted.receipt != receipt:
            raise OwnerConflict("target_execution_closure_receipt_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT payload_json FROM ar_target_execution_closures WHERE "
                    "closure_ref = :closure_ref"
                ),
                {"closure_ref": closure_ref},
            ).one()
            preflight_rows = connection.execute(
                text(
                    "SELECT preflight_json, preflight_hash FROM "
                    "ar_target_run_preflights WHERE target_ref = :target_ref "
                    "ORDER BY ordinal"
                ),
                {"target_ref": accepted.target_ref},
            ).all()
            activation = connection.execute(
                text(
                    "SELECT candidate_json, candidate_hash, formal_plan_json, "
                    "formal_plan_hash FROM ar_target_run_activations WHERE "
                    "target_ref = :target_ref"
                ),
                {"target_ref": accepted.target_ref},
            ).first()
        try:
            payload = json.loads(row.payload_json)
            if activation is None:
                raise ValueError("activation absent")
            candidate = _decode_stored_record(
                activation.candidate_json,
                activation.candidate_hash,
                TargetCandidate,
            )
            formal_plan = _decode_stored_record(
                activation.formal_plan_json,
                activation.formal_plan_hash,
                FormalPlan,
            )
            preflights = tuple(
                _decode_stored_record(
                    item.preflight_json,
                    item.preflight_hash,
                    TargetExecutionPreflight,
                )
                for item in preflight_rows
            )
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise OwnerConflict("target_execution_closure_integrity_invalid") from error
        if (
            not isinstance(payload, dict)
            or not preflights
            or type(candidate) is not TargetCandidate
            or type(formal_plan) is not FormalPlan
        ):
            raise OwnerConflict("target_execution_closure_integrity_invalid")
        implementation_artifacts: list[AcceptedTargetImplementationArtifact] = []
        for preflight in preflights:
            artifact = self._memory.query_implementation_artifact(
                preflight.implementation_revision_ref
            )
            if artifact is None or (
                preflight.review_scope.candidate_revision_binding
                != ContentBindingProof(
                    subject_ref=artifact.implementation_revision_ref,
                    content_hash_ref=artifact.metadata_content_hash_ref,
                )
                or preflight.implementation_acceptance_receipt
                != receipt_proof(
                    artifact.receipt,
                    subject_ref=artifact.metadata_content_hash_ref,
                )
            ):
                raise OwnerConflict("target_execution_closure_implementation_invalid")
            if preflight.code_review.code_changed:
                _code_row, code_payload, code_receipt = self._review_acceptance(
                    target_run_ref=accepted.target_run_ref,
                    review_kind="code",
                    subject_ref=preflight.implementation_revision_ref,
                )
                expected_code_content = {
                    "review": projection_plain_value(preflight.code_review),
                    "complete_review_scope": projection_plain_value(
                        preflight.review_scope
                    ),
                }
                expected_code_payload = {
                    **expected_code_content,
                    "candidate_ready_evidence_ref": (
                        preflight.candidate_ready_evidence.evidence_ref
                    ),
                    "self_check_evidence_refs": [
                        evidence.evidence_ref
                        for evidence in preflight.self_check_evidence
                    ],
                }
                if (
                    code_payload != expected_code_payload
                    or preflight.code_review_evidence_binding
                    != ContentBindingProof(
                        subject_ref=preflight.code_review.review_ref or "",
                        content_hash_ref=canonical_hash(expected_code_content),
                    )
                    or preflight.code_review_evidence_receipt
                    != receipt_proof(
                        code_receipt,
                        subject_ref=canonical_hash(expected_code_content),
                    )
                ):
                    raise OwnerConflict("target_execution_closure_code_review_invalid")
            elif (
                preflight.code_review_evidence_binding is not None
                or preflight.code_review_evidence_receipt is not None
            ):
                raise OwnerConflict("target_execution_closure_code_review_invalid")
            implementation_artifacts.append(artifact)
        protected = self._graph.query_protected_execution(
            accepted.protected_binding_ref
        )
        target_execution_input = (
            None
            if protected is None
            else self._graph.query_execution_input_binding(
                protected.input_binding_ref
            )
        )
        manifest = self._memory.query_result_manifest(accepted.result_manifest_ref)
        domain = self._domain_reader.query_experiment(
            accepted.evaluation_attempt_ref
        )
        metric = self._domain_reader.query_formal_metric_result(
            accepted.evaluation_attempt_ref
        )
        review_row, review_payload, review_receipt = self._review_acceptance(
            target_run_ref=accepted.target_run_ref,
            review_kind="result",
            subject_ref=accepted.evaluation_attempt_ref,
        )
        if any(
            value is None
            for value in (
                protected,
                target_execution_input,
                manifest,
                domain,
                metric,
            )
        ):
            raise OwnerConflict("target_execution_closure_integrity_invalid")
        return {
            "closure": accepted,
            "payload": payload,
            "candidate": candidate,
            "formal_plan": formal_plan,
            "preflights": preflights,
            "implementation_artifacts": tuple(implementation_artifacts),
            "protected_execution": protected,
            "target_execution_input": target_execution_input,
            "result_manifest": manifest,
            "domain": domain,
            "formal_metric": metric,
            "result_review": review_payload,
            "result_review_acceptance_ref": review_row.review_ref,
            "result_review_acceptance_receipt": review_receipt,
        }

    def _review_acceptance(
        self,
        *,
        target_run_ref: str,
        review_kind: str,
        subject_ref: str,
    ) -> tuple[object, object, AcceptanceReceipt]:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ar_target_review_evidence WHERE "
                    "target_run_ref = :run_ref AND review_kind = :kind AND "
                    "subject_ref = :subject_ref"
                ),
                {
                    "run_ref": target_run_ref,
                    "kind": review_kind,
                    "subject_ref": subject_ref,
                },
            ).first()
        if row is None:
            raise OwnerConflict("target_review_acceptance_missing")
        try:
            payload = json.loads(row.payload_json)
        except (TypeError, ValueError) as error:
            raise OwnerConflict("target_review_integrity_invalid") from error
        receipt_bindings = {
            "review_ref": row.review_ref,
            "target_ref": row.target_ref,
            "target_run_ref": row.target_run_ref,
            "review_kind": row.review_kind,
            "subject_ref": row.subject_ref,
            "payload_hash": row.payload_hash,
            "harness_operation_ref": row.harness_operation_ref,
            "reviewer_session_ref": row.reviewer_session_ref,
            "spawn_evidence_ref": row.reviewer_spawn_evidence_ref,
            "completion_evidence_ref": row.reviewer_completion_evidence_ref,
        }
        if row.evidence_content_hash is not None:
            receipt_bindings["evidence_content_hash"] = row.evidence_content_hash
        receipt = _receipt(
            "agent_runtime",
            (
                AR_TARGET_CODE_REVIEW_RECEIPT_KIND
                if review_kind == "code"
                else AR_TARGET_RESULT_REVIEW_RECEIPT_KIND
            ),
            row.receipt_ref,
            (
                row.evidence_content_hash
                if row.evidence_content_hash is not None
                else row.payload_hash
            ),
            receipt_bindings,
        )
        if (
            row.payload_hash != canonical_hash(payload)
            or row.receipt_hash != receipt.payload_hash
        ):
            raise OwnerConflict("target_review_integrity_invalid")
        return row, payload, receipt

    def _review_evidence(
        self,
        *,
        handle: TargetWorkHandle,
        operation_ref: str,
        review_kind: str,
        expected_review: object,
        expected_scope: object | None = None,
        bind_domain_session: bool = True,
    ) -> dict[str, object]:
        run = self._harness.query_target_run_by_ref(handle.target_run_ref)
        if run is None or run.root_session_ref != handle.root_session_ref:
            raise OwnerConflict("target_review_harness_invalid")
        profile = self._harness.query_profile(handle.target_run_ref)
        if not isinstance(profile, dict):
            raise OwnerConflict("target_review_harness_invalid")
        evidence_values = profile.get("subagent_evidence")
        operation_refs = profile.get("provider_operation_refs")
        if (
            not isinstance(evidence_values, list)
            or not isinstance(operation_refs, list)
            or operation_ref not in operation_refs
            or profile.get("sandbox_mode") != "workspace-write"
        ):
            raise OwnerConflict("target_review_harness_invalid")
        matches = []
        for value in evidence_values:
            if not isinstance(value, dict):
                continue
            payload = value.get("payload")
            if (
                value.get("parent_session_ref") == run.native_session_ref
                and value.get("provider_operation_ref") == operation_ref
                and isinstance(payload, dict)
                and payload.get("review_kind") == review_kind
                and payload.get("review") == expected_review
                and (
                    expected_scope is None
                    or payload.get("scope") == expected_scope
                )
                and value.get("payload_hash") == canonical_hash(payload)
            ):
                matches.append(value)
        if len(matches) != 1:
            raise OwnerConflict("target_review_child_evidence_invalid")
        evidence = dict(matches[0])
        if evidence.get("child_session_ref") == run.native_session_ref:
            raise OwnerConflict("target_review_child_evidence_invalid")
        reservation = self._harness.query_target_child_session(operation_ref)
        if (
            reservation is None
            or reservation.target_run_ref != handle.target_run_ref
            or reservation.review_kind != review_kind
            or reservation.parent_root_session_ref != handle.root_session_ref
            or expected_review.get("reviewer_session_ref")
            != reservation.child_session_ref
            or expected_review.get("review_parent_session_ref")
            != handle.root_session_ref
            or expected_review.get("reviewer_spawn_evidence_ref")
            != evidence.get("spawn_evidence_ref")
        ):
            raise OwnerConflict("target_review_domain_session_invalid")
        if review_kind == "code":
            self._verify_code_review_skill_invocation(
                handle=handle,
                operation_ref=operation_ref,
                profile=profile,
                evidence=evidence,
            )
        else:
            self._verify_result_review_native_child(
                handle=handle,
                operation_ref=operation_ref,
                profile=profile,
                evidence=evidence,
                expected_review=expected_review,
            )
        if bind_domain_session:
            try:
                bound = self._harness.bind_target_child_session(
                    harness_operation_ref=operation_ref,
                    native_parent_session_ref=str(evidence["parent_session_ref"]),
                    native_child_session_ref=str(evidence["child_session_ref"]),
                    spawn_evidence_ref=str(evidence["spawn_evidence_ref"]),
                    completion_evidence_ref=str(
                        evidence["completion_evidence_ref"]
                    ),
                    payload_hash=str(evidence["payload_hash"]),
                )
            except Exception as error:
                raise OwnerConflict(
                    "target_review_domain_session_invalid"
                ) from error
        else:
            # Issuer query/verification paths are side-effect free.  A review
            # that was accepted before its native child binding became durable
            # is incomplete evidence, not something a later read may repair.
            bound = reservation
        if (
            bound.status != "bound"
            or bound.harness_operation_ref != operation_ref
            or bound.target_run_ref != handle.target_run_ref
            or bound.review_kind != review_kind
            or bound.parent_root_session_ref != handle.root_session_ref
            or bound.native_parent_session_ref != evidence["parent_session_ref"]
            or bound.native_child_session_ref != evidence["child_session_ref"]
            or bound.native_child_session_ref
            != evidence["review_actor_session_ref"]
            or bound.spawn_evidence_ref != evidence["spawn_evidence_ref"]
            or bound.completion_evidence_ref
            != evidence["completion_evidence_ref"]
            or bound.payload_hash != evidence["payload_hash"]
        ):
            raise OwnerConflict("target_review_domain_session_invalid")
        evidence["domain_child_session_ref"] = bound.child_session_ref
        return evidence

    def _verify_result_review_native_child(
        self,
        *,
        handle: TargetWorkHandle,
        operation_ref: str,
        profile: dict[str, object],
        evidence: dict[str, object],
        expected_review: object,
    ) -> None:
        """Re-verify the fresh native Codex child that reviewed one result.

        The Harness adapter is responsible for reading the native child
        ledger.  AR nevertheless rechecks its complete, content-addressed
        projection before binding the reserved domain child Session or
        accepting a review.  Result review is deliberately not authorized by
        the ``$code-review`` Skill: it is an independent, result-bound child
        task with its own exact request marker and terminal receipt.
        """

        run = self._harness.query_target_run_by_ref(handle.target_run_ref)
        workspace = self.query_target_workspace(handle.target_run_ref)
        workspace_root = self._workspace_root
        expected_cwd = (
            None
            if workspace is None or workspace_root is None
            else str(
                (
                    workspace_root
                    / canonical_hash({"workspace_ref": workspace.workspace_ref})
                ).resolve()
            )
        )
        payload = evidence.get("payload")
        child_session_ref = evidence.get("child_session_ref")
        parent_session_ref = evidence.get("parent_session_ref")
        spawn_ref = evidence.get("spawn_evidence_ref")
        terminal_wait_ref = evidence.get("completion_evidence_ref")
        payload_hash = evidence.get("payload_hash")
        review_actor_session_ref = evidence.get("review_actor_session_ref")
        review_completion_ref = evidence.get(
            "review_completion_evidence_ref"
        )
        spawn_prompt_hash = evidence.get("spawn_prompt_hash")
        terminal_output_hash = evidence.get("child_terminal_output_hash")
        lineage = evidence.get("child_ledger_lineage")
        request = evidence.get("result_review_request")
        if not isinstance(expected_review, dict):
            raise OwnerConflict("target_result_review_native_evidence_invalid")
        expected_request = {
            "schema_ref": "meta-research/target-result-review-request/v1",
            "review_kind": "result",
            "target_ref": handle.target_ref,
            "target_run_ref": handle.target_run_ref,
            "reviewed_evaluation_attempt_ref": expected_review.get(
                "reviewed_evaluation_attempt_ref"
            ),
            "reviewed_metric_result_ref": expected_review.get(
                "reviewed_metric_result_ref"
            ),
            "reviewed_asset_manifest_ref": expected_review.get(
                "reviewed_asset_manifest_ref"
            ),
        }
        expected_payload = {
            "schema_ref": "meta-research/target-review-evidence/v1",
            "review_kind": "result",
            "review": expected_review,
        }
        provider_version = profile.get("provider_version")
        expected_lineage_base = {
            "session_id": child_session_ref,
            "parent_session_id": None if run is None else run.native_session_ref,
            "thread_source": "subagent",
            "cwd": expected_cwd,
            "originator": "codex_exec",
            "cli_version": provider_version,
        }
        lineage_sandbox = (
            lineage.get("sandbox_mode") if isinstance(lineage, dict) else None
        )
        expected_lineage = {
            **expected_lineage_base,
            "sandbox_mode": lineage_sandbox,
        }
        valid_hashes = all(
            isinstance(value, str)
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
            for value in (spawn_prompt_hash, terminal_output_hash, payload_hash)
        )
        expected_completion_ref = (
            None
            if not isinstance(child_session_ref, str)
            or not isinstance(parent_session_ref, str)
            or not isinstance(terminal_output_hash, str)
            else "codex_child_terminal:"
            + canonical_hash(
                {
                    "child_session_ref": child_session_ref,
                    "parent_session_ref": parent_session_ref,
                    "terminal_output_hash": terminal_output_hash,
                }
            )
        )
        events = self._operation_evidence_events(
            operation_ref,
            run_ref=handle.target_run_ref,
        )
        spawn = events.get(spawn_ref) if isinstance(spawn_ref, str) else None
        terminal_wait = (
            events.get(terminal_wait_ref)
            if isinstance(terminal_wait_ref, str)
            else None
        )
        if (
            run is None
            or workspace is None
            or expected_cwd is None
            or profile.get("harness_family") != "codex"
            or profile.get("status") != "executed"
            or profile.get("run_ref") != handle.target_run_ref
            or profile.get("attempt_ref") != handle.execution_attempt_ref
            or profile.get("root_session_ref") != handle.root_session_ref
            or profile.get("fence_ref") != handle.execution_fence_ref
            or profile.get("native_session_ref") != run.native_session_ref
            or not isinstance(provider_version, str)
            or not provider_version
            or profile.get("locked_version") != provider_version
            or not isinstance(child_session_ref, str)
            or not child_session_ref
            or parent_session_ref != run.native_session_ref
            or child_session_ref == parent_session_ref
            or review_actor_session_ref != child_session_ref
            or not isinstance(spawn_ref, str)
            or not spawn_ref
            or not isinstance(terminal_wait_ref, str)
            or not terminal_wait_ref
            or payload != expected_payload
            or payload_hash != canonical_hash(expected_payload)
            or request != expected_request
            or any(
                not isinstance(expected_request[field], str)
                or not expected_request[field]
                for field in (
                    "target_ref",
                    "target_run_ref",
                    "reviewed_evaluation_attempt_ref",
                    "reviewed_metric_result_ref",
                    "reviewed_asset_manifest_ref",
                )
            )
            or lineage_sandbox not in {"read-only", "workspace-write"}
            or lineage != expected_lineage
            or not valid_hashes
            or review_completion_ref != expected_completion_ref
            or spawn is None
            or terminal_wait is None
        ):
            raise OwnerConflict("target_result_review_native_evidence_invalid")
        spawn_sequence, spawn_summary = spawn
        terminal_wait_sequence, terminal_wait_summary = terminal_wait
        if (
            spawn_sequence >= terminal_wait_sequence
            or spawn_summary.get("kind") != "item.completed"
            or spawn_summary.get("item_kind") != "collab_tool_call"
            or spawn_summary.get("tool_kind") != "spawn_agent"
            or terminal_wait_summary.get("kind") != "item.completed"
            or terminal_wait_summary.get("item_kind")
            != "collab_tool_call"
            or terminal_wait_summary.get("tool_kind") != "wait"
        ):
            raise OwnerConflict("target_result_review_native_evidence_invalid")

        # A new domain child reference cannot hide reuse of the native child
        # that produced an accepted code review for this TargetRun.
        with self._database.read() as connection:
            code_reviewers = connection.execute(
                text(
                    "SELECT sessions.native_child_session_ref FROM "
                    "ar_target_review_evidence reviews JOIN "
                    "ar_target_harness_child_sessions sessions ON "
                    "sessions.harness_operation_ref = "
                    "reviews.harness_operation_ref WHERE "
                    "reviews.target_run_ref = :run_ref AND "
                    "reviews.review_kind = 'code' AND sessions.status = "
                    "'bound'"
                ),
                {"run_ref": handle.target_run_ref},
            ).all()
        if any(
            row.native_child_session_ref == child_session_ref
            for row in code_reviewers
        ):
            raise OwnerConflict("target_result_review_native_evidence_invalid")

    def _verify_code_review_skill_invocation(
        self,
        *,
        handle: TargetWorkHandle,
        operation_ref: str,
        profile: dict[str, object],
        evidence: dict[str, object],
    ) -> None:
        """Re-verify the exact `$code-review` event before binding its child.

        A generic Harness ``skill`` capability or a prompt mentioning the
        Skill is not review authority.  The invocation, its completion, and
        the reserved child spawn must be evidence events from this exact
        provider operation, in causal order.
        """

        invocation_ref = evidence.get("skill_invocation_evidence_ref")
        completion_ref = evidence.get("skill_completion_evidence_ref")
        spawn_ref = evidence.get("spawn_evidence_ref")
        child_session_ref = evidence.get("child_session_ref")
        terminal_wait_ref = evidence.get("completion_evidence_ref")
        if (
            evidence.get("skill_name") != "code-review"
            or evidence.get("skill_actor_session_ref") != child_session_ref
            or not isinstance(child_session_ref, str)
            or not child_session_ref
            or not isinstance(invocation_ref, str)
            or not invocation_ref
            or not isinstance(completion_ref, str)
            or not completion_ref
            or not isinstance(spawn_ref, str)
            or not spawn_ref
            or not isinstance(terminal_wait_ref, str)
            or not terminal_wait_ref
        ):
            raise OwnerConflict("target_code_review_skill_evidence_invalid")
        events = self._operation_evidence_events(
            operation_ref,
            run_ref=handle.target_run_ref,
        )
        run = self._harness.query_target_run_by_ref(handle.target_run_ref)
        family = profile.get("harness_family")
        invocation = events.get(invocation_ref)
        completion = events.get(completion_ref)
        spawn = events.get(spawn_ref)
        terminal_wait = events.get(terminal_wait_ref)
        if (
            run is None
            or spawn is None
            or terminal_wait is None
            or (
                family != "codex"
                and (invocation is None or completion is None)
            )
        ):
            raise OwnerConflict("target_code_review_skill_evidence_invalid")
        invocation_sequence, invocation_summary = (
            (-1, {}) if invocation is None else invocation
        )
        completion_sequence, completion_summary = (
            (-1, {}) if completion is None else completion
        )
        spawn_sequence, spawn_summary = spawn
        terminal_wait_sequence, terminal_wait_summary = terminal_wait
        invocation_actor = invocation_summary.get(
            "actor_session_ref",
            invocation_summary.get("native_session_ref"),
        )
        if family == "codex":
            # Codex's parent JSONL proves native spawn and terminal wait.  The
            # child-local Skill injection and completion live in Codex's
            # durable child-session ledger and are projected by the trusted
            # adapter as content-addressed evidence.  They must never be
            # looked up as if they were root-stream events: doing so would
            # either reject the real `/spawn` trace or accept a root self-
            # review before spawn.
            lineage = evidence.get("child_ledger_lineage")
            skill_path_value = evidence.get("skill_package_path")
            skill_package_hash = evidence.get("skill_package_hash")
            spawn_prompt_hash = evidence.get("spawn_prompt_hash")
            terminal_output_hash = evidence.get("child_terminal_output_hash")
            workspace = self.query_target_workspace(handle.target_run_ref)
            workspace_root = self._workspace_root
            expected_cwd = (
                None
                if workspace is None or workspace_root is None
                else str(
                    (
                        workspace_root
                        / canonical_hash(
                            {"workspace_ref": workspace.workspace_ref}
                        )
                    ).resolve()
                )
            )
            skill_path = (
                Path(skill_path_value)
                if isinstance(skill_path_value, str)
                else None
            )
            expected_invocation_ref = (
                None
                if skill_path is None
                or not isinstance(skill_package_hash, str)
                else "codex_child_skill:"
                + canonical_hash(
                    {
                        "child_session_ref": child_session_ref,
                        "parent_session_ref": run.native_session_ref,
                        "skill_path": skill_path_value,
                        "skill_package_hash": skill_package_hash,
                    }
                )
            )
            expected_completion_ref = (
                None
                if not isinstance(terminal_output_hash, str)
                else "codex_child_terminal:"
                + canonical_hash(
                    {
                        "child_session_ref": child_session_ref,
                        "parent_session_ref": run.native_session_ref,
                        "terminal_output_hash": terminal_output_hash,
                    }
                )
            )
            expected_lineage = {
                "session_id": child_session_ref,
                "parent_session_id": run.native_session_ref,
                "thread_source": "subagent",
                "cwd": expected_cwd,
                "originator": "codex_exec",
                "cli_version": profile.get("provider_version"),
            }
            valid_hashes = all(
                isinstance(value, str)
                and len(value) == 64
                and all(character in "0123456789abcdef" for character in value)
                for value in (
                    skill_package_hash,
                    spawn_prompt_hash,
                    terminal_output_hash,
                )
            )
            valid = (
                spawn_sequence < terminal_wait_sequence
                and spawn_summary.get("kind") == "item.completed"
                and spawn_summary.get("item_kind") == "collab_tool_call"
                and spawn_summary.get("tool_kind") == "spawn_agent"
                and terminal_wait_summary.get("kind") == "item.completed"
                and terminal_wait_summary.get("item_kind")
                == "collab_tool_call"
                and terminal_wait_summary.get("tool_kind") == "wait"
                and invocation_ref == expected_invocation_ref
                and completion_ref == expected_completion_ref
                and invocation_ref not in events
                and completion_ref not in events
                and isinstance(skill_path, Path)
                and skill_path.is_absolute()
                and skill_path.name == "SKILL.md"
                and skill_path.parent.name == "code-review"
                and valid_hashes
                and expected_cwd is not None
                and lineage == expected_lineage
                and evidence.get("skill_actor_session_ref")
                == child_session_ref
            )
        elif family == "claude":
            skill_tool_use_id = evidence.get("skill_tool_use_id")
            valid = (
                isinstance(skill_tool_use_id, str)
                and bool(skill_tool_use_id)
                and spawn_sequence
                < invocation_sequence
                < completion_sequence
                < terminal_wait_sequence
                and invocation_summary.get("native_session_ref")
                == child_session_ref
                and completion_summary.get("native_session_ref")
                == child_session_ref
                and {
                    "tool_use_id": skill_tool_use_id,
                    "skill_name": "code-review",
                }
                in invocation_summary.get("skill_invocations", [])
                and skill_tool_use_id
                in completion_summary.get("successful_tool_result_ids", [])
            )
        else:
            valid = False
        if not valid:
            raise OwnerConflict("target_code_review_skill_evidence_invalid")

    def _operation_evidence_events(
        self, operation_ref: str, *, run_ref: str
    ) -> dict[str, tuple[int, dict[str, object]]]:
        with self._database.read() as connection:
            operation = connection.execute(
                text(
                    "SELECT status FROM ar_harness_provider_operations WHERE "
                    "operation_ref = :operation_ref AND run_ref = :run_ref"
                ),
                {"operation_ref": operation_ref, "run_ref": run_ref},
            ).first()
            rows = connection.execute(
                text(
                    "SELECT event_ref, sequence, summary_json, summary_hash "
                    "FROM ar_harness_evidence_events WHERE operation_ref = "
                    ":operation_ref"
                ),
                {"operation_ref": operation_ref},
            ).all()
        if operation is None or operation.status != "executed":
            raise OwnerConflict("target_review_harness_operation_invalid")
        events: dict[str, tuple[int, dict[str, object]]] = {}
        try:
            for row in rows:
                summary = json.loads(row.summary_json)
                if (
                    not isinstance(summary, dict)
                    or canonical_hash(summary) != row.summary_hash
                ):
                    raise OwnerConflict("target_review_harness_evidence_invalid")
                events[str(row.event_ref)] = (int(row.sequence), summary)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise OwnerConflict("target_review_harness_evidence_invalid") from error
        return events

    def _operation_evidence_refs(
        self, operation_ref: str, *, run_ref: str
    ) -> set[str]:
        return set(
            self._operation_evidence_events(
                operation_ref, run_ref=run_ref
            )
        )

    def _accept_review_row(
        self,
        *,
        handle: TargetWorkHandle,
        review_kind: str,
        subject_ref: str,
        harness_operation_ref: str,
        evidence: dict[str, object],
        payload: object,
        payload_hash: str,
        evidence_content_hash: str,
        idempotency_key: str,
    ) -> AcceptanceReceipt:
        request_hash = canonical_hash(
            {
                "target_ref": handle.target_ref,
                "target_run_ref": handle.target_run_ref,
                "review_kind": review_kind,
                "subject_ref": subject_ref,
                "harness_operation_ref": harness_operation_ref,
                "evidence": evidence,
                "payload": payload,
                "evidence_content_hash": evidence_content_hash,
            }
        )
        now = time.time()
        with self._database.write() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ar_target_review_evidence WHERE "
                    "idempotency_key = :key OR (target_run_ref = :run_ref AND "
                    "review_kind = :kind AND subject_ref = :subject_ref)"
                ),
                {
                    "key": idempotency_key,
                    "run_ref": handle.target_run_ref,
                    "kind": review_kind,
                    "subject_ref": subject_ref,
                },
            ).first()
            if row is not None:
                if row.request_hash != request_hash:
                    raise OwnerConflict("target_review_conflict")
                receipt_ref = row.receipt_ref
            else:
                review_ref = new_ref(f"target_{review_kind}_review")
                receipt_ref = new_ref(f"ar_target_{review_kind}_review_receipt")
                receipt = _receipt(
                    "agent_runtime",
                    (
                        AR_TARGET_CODE_REVIEW_RECEIPT_KIND
                        if review_kind == "code"
                        else AR_TARGET_RESULT_REVIEW_RECEIPT_KIND
                    ),
                    receipt_ref,
                    evidence_content_hash,
                    {
                        "review_ref": review_ref,
                        "target_ref": handle.target_ref,
                        "target_run_ref": handle.target_run_ref,
                        "review_kind": review_kind,
                        "subject_ref": subject_ref,
                        "payload_hash": payload_hash,
                        "evidence_content_hash": evidence_content_hash,
                        "harness_operation_ref": harness_operation_ref,
                        "reviewer_session_ref": evidence[
                            "domain_child_session_ref"
                        ],
                        "spawn_evidence_ref": evidence["spawn_evidence_ref"],
                        "completion_evidence_ref": evidence[
                            "completion_evidence_ref"
                        ],
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO ar_target_review_evidence (review_ref, "
                        "target_ref, target_run_ref, review_kind, subject_ref, "
                        "parent_session_ref, reviewer_session_ref, "
                        "reviewer_spawn_evidence_ref, "
                        "reviewer_completion_evidence_ref, "
                        "harness_operation_ref, payload_json, payload_hash, "
                        "evidence_content_hash, "
                        "idempotency_key, request_hash, receipt_ref, "
                        "receipt_hash, accepted_at) VALUES (:review_ref, "
                        ":target_ref, :target_run_ref, :review_kind, "
                        ":subject_ref, :parent_session_ref, "
                        ":reviewer_session_ref, :spawn_ref, :completion_ref, "
                        ":operation_ref, :payload_json, :payload_hash, "
                        ":evidence_content_hash, "
                        ":idempotency_key, :request_hash, :receipt_ref, "
                        ":receipt_hash, :accepted_at)"
                    ),
                    {
                        "review_ref": review_ref,
                        "target_ref": handle.target_ref,
                        "target_run_ref": handle.target_run_ref,
                        "review_kind": review_kind,
                        "subject_ref": subject_ref,
                        "parent_session_ref": handle.root_session_ref,
                        "reviewer_session_ref": evidence[
                            "domain_child_session_ref"
                        ],
                        "spawn_ref": evidence["spawn_evidence_ref"],
                        "completion_ref": evidence[
                            "completion_evidence_ref"
                        ],
                        "operation_ref": harness_operation_ref,
                        "payload_json": canonical_json(payload),
                        "payload_hash": payload_hash,
                        "evidence_content_hash": evidence_content_hash,
                        "idempotency_key": idempotency_key,
                        "request_hash": request_hash,
                        "receipt_ref": receipt.receipt_ref,
                        "receipt_hash": receipt.payload_hash,
                        "accepted_at": now,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE agent_runtime_state SET revision = revision + 1, "
                        "target_review_count = target_review_count + 1 WHERE "
                        "singleton = 'owner'"
                    )
                )
        with self._database.read() as connection:
            persisted = connection.execute(
                text(
                    "SELECT * FROM ar_target_review_evidence WHERE receipt_ref = "
                    ":receipt_ref"
                ),
                {"receipt_ref": receipt_ref},
            ).one()
        return _receipt(
            "agent_runtime",
            (
                AR_TARGET_CODE_REVIEW_RECEIPT_KIND
                if review_kind == "code"
                else AR_TARGET_RESULT_REVIEW_RECEIPT_KIND
            ),
            persisted.receipt_ref,
            (
                persisted.evidence_content_hash
                if persisted.evidence_content_hash is not None
                else persisted.payload_hash
            ),
            {
                "review_ref": persisted.review_ref,
                "target_ref": persisted.target_ref,
                "target_run_ref": persisted.target_run_ref,
                "review_kind": persisted.review_kind,
                "subject_ref": persisted.subject_ref,
                "payload_hash": persisted.payload_hash,
                "evidence_content_hash": (
                    persisted.evidence_content_hash
                    if persisted.evidence_content_hash is not None
                    else persisted.payload_hash
                ),
                "harness_operation_ref": persisted.harness_operation_ref,
                "reviewer_session_ref": persisted.reviewer_session_ref,
                "spawn_evidence_ref": persisted.reviewer_spawn_evidence_ref,
                "completion_evidence_ref": (
                    persisted.reviewer_completion_evidence_ref
                ),
            },
        )


class SQLiteTargetExecutionAdmissionVerifier:
    """Concrete issuer adapter consumed by the generic execution port."""

    def __init__(
        self,
        agent_runtime: SQLiteTargetRunAgentAuthority,
        research_memory: SQLiteTargetRunMemoryAuthority,
        measurement_authority: TargetMeasurementDomainAuthority,
    ) -> None:
        self._agent_runtime = agent_runtime
        self._research_memory = research_memory
        self._measurement_authority = measurement_authority

    def verify_execution_admission(
        self,
        request: TargetExecutionRequest,
    ) -> AcceptedTargetExecutionAdmission:
        try:
            authority = (
                self._measurement_authority.query_target_measurement_domain_authority(
                    request.handle.target_ref
                )
            )
            authority_ref = getattr(authority, "authority_ref", None)
            authority_hash = getattr(authority, "authority_hash", None)
            authority_target_ref = getattr(authority, "target_ref", None)
            authority_receipt = getattr(authority, "receipt", None)
            if (
                authority is None
                or type(authority_ref) is not str
                or type(authority_hash) is not str
                or authority_target_ref != request.handle.target_ref
                or type(authority_receipt) is not AcceptanceReceipt
            ):
                raise OwnerConflict("target_measurement_authority_invalid")
            accepted_authority = TargetMeasurementAuthorityBinding(
                authority_ref=authority_ref,
                acceptance_receipt=receipt_proof(
                    authority_receipt,
                    subject_ref=authority_hash,
                ),
            )
            if request.measurement_authority != accepted_authority:
                raise OwnerConflict("target_measurement_authority_invalid")
        except LegacyTargetExecutionError:
            raise
        except Exception as error:
            raise LegacyTargetExecutionError(
                "target_measurement_authority_invalid"
            ) from error
        self._agent_runtime.verify_current_target_run_quest(
            handle=request.handle,
            quest_ref=request.quest_ref,
        )
        accepted = self._agent_runtime.verify_execution_eligibility(
            handle=request.handle,
            eligibility_ref=request.execution_eligibility_ref,
            receipt=request.execution_eligibility_receipt,
        )
        bundle, materialized = (
            self._research_memory.materialize_implementation_bundle(
                accepted.preflight.implementation_revision_ref
            )
        )
        implementation_bytes = getattr(materialized, "content", None)
        implementation_bundle_sha256 = (
            None
            if type(implementation_bytes) is not bytes
            else hashlib.sha256(implementation_bytes).hexdigest()
        )
        if (
            accepted.implementation_bundle != bundle
            or accepted.implementation_bundle_usage.bundle != bundle
            or accepted.implementation_bundle_usage.target_ref
            != request.handle.target_ref
            or request.implementation_artifact_ref
            != bundle.artifact.version_ref
            or request.implementation_tree_sha256
            != bundle.bundle_content_hash_ref
            or type(implementation_bytes) is not bytes
            or implementation_bundle_sha256
            != request.implementation_bundle_sha256
        ):
            raise OwnerConflict("target_execution_implementation_invalid")
        try:
            parsed = parse_target_implementation_bundle(
                implementation_bytes,
                expected_tree_sha256=bundle.bundle_content_hash_ref,
            )
        except TargetImplementationBundleError as directory_error:
            if implementation_bundle_sha256 != bundle.bundle_content_hash_ref:
                raise OwnerConflict(
                    "target_execution_implementation_invalid"
                ) from directory_error
            try:
                parsed = parse_target_single_file_bundle(
                    implementation_bytes,
                    entrypoint=request.entrypoint,
                    expected_content_sha256=bundle.bundle_content_hash_ref,
                )
            except TargetImplementationBundleError as error:
                raise OwnerConflict(
                    "target_execution_implementation_invalid"
                ) from error
        try:
            parsed.entry(request.entrypoint)
        except TargetImplementationBundleError as error:
            raise OwnerConflict("target_execution_entrypoint_invalid") from error
        accepted_input_files = self.resolve_accepted_input_files(request.handle)
        accepted_input_specs = tuple(item.spec for item in accepted_input_files)
        if (
            request.input_files != accepted_input_specs
            or request.input_manifest_sha256
            != target_execution_input_manifest_sha256(accepted_input_specs)
        ):
            raise OwnerConflict("target_execution_input_manifest_invalid")
        return AcceptedTargetExecutionAdmission(
            handle=request.handle,
            execution_eligibility_ref=accepted.eligibility_ref,
            execution_eligibility_receipt=request.execution_eligibility_receipt,
            measurement_authority=accepted_authority,
            implementation_artifact_ref=bundle.artifact.version_ref,
            implementation_tree_sha256=bundle.bundle_content_hash_ref,
            implementation_bundle_sha256=implementation_bundle_sha256,
            implementation_bytes=implementation_bytes,
            accepted_input_files=accepted_input_files,
        )

    def resolve_accepted_input_files(
        self,
        handle: TargetWorkHandle,
    ) -> tuple[AcceptedTargetExecutionInputFile, ...]:
        """Resolve the exact read-only input files for one current handle.

        Direct RM assets are materialized from their issuer-owned version and
        assigned deterministic internal names.  Upstream TargetCommit inputs
        require the generic formal-v3 result-manifest authority; until that
        authority can resolve every output byte, this seam fails closed rather
        than silently dropping a dependency.
        """


        self._agent_runtime.verify_current_target_run_handle(handle)
        if handle.accepted_input_target_commit_refs:
            raise OwnerConflict("target_execution_upstream_input_unavailable")
        resolved: list[AcceptedTargetExecutionInputFile] = []
        for ordinal, proof in enumerate(handle.accepted_input_asset_proofs, 1):
            asset, _proof_receipt, _file_name, content = (
                self._research_memory.materialize_input_asset(
                    target_ref=handle.target_ref,
                    asset_ref=proof.asset_ref,
                )
            )
            relative_path = (
                "direct/"
                f"{ordinal:04d}-"
                f"{hashlib.sha256(asset.asset_ref.encode('utf-8')).hexdigest()[:16]}"
            )
            try:
                validate_bundle_relative_path(relative_path)
            except TargetImplementationBundleError as error:
                raise OwnerConflict(
                    "target_execution_input_manifest_invalid"
                ) from error
            resolved.append(
                AcceptedTargetExecutionInputFile(
                    asset_ref=asset.asset_ref,
                    version_ref=asset.version_ref,
                    relative_path=relative_path,
                    mode=0o444,
                    byte_count=len(content),
                    content_sha256=hashlib.sha256(content).hexdigest(),
                    content=content,
                )
            )
        result = tuple(sorted(resolved, key=lambda item: item.relative_path))
        # Calling the canonical helper here catches aggregate limits/collisions
        # before the execution port sees the request.
        target_execution_input_manifest_sha256(tuple(item.spec for item in result))
        return result

    def verify_current_work_handle(
        self,
        handle: TargetWorkHandle,
    ) -> TargetWorkHandle:
        return self._agent_runtime.verify_current_target_run_handle(handle)

    def query_current_work_handle(
        self, target_ref: str
    ) -> TargetWorkHandle | None:
        return self._agent_runtime.query_current_target_work_handle(target_ref)

    def verify_retiring_work_handle(
        self, handle: TargetWorkHandle
    ) -> TargetWorkHandle:
        return self._agent_runtime.verify_retiring_failed_handle(handle)


def _generic_terminal_result_sources(
    request: TargetExecutionRequest,
    terminal: TargetExecutionTerminalResult,
) -> tuple[tuple[str, str, bytes], ...]:
    """Project only request-declared terminal bytes into exact RM roles."""


    if (
        type(request) is not TargetExecutionRequest
        or type(terminal) is not TargetExecutionTerminalResult
    ):
        raise OwnerConflict("target_generic_result_execution_invalid")
    sources: list[tuple[str, str, bytes]] = []
    if terminal.stdout_content:
        sources.append((request.stdout_role, "$stdout", terminal.stdout_content))
    for output in terminal.output_files:
        sources.append((output.role, output.relative_path, output.content))
    if sum(role == "result_content" for role, _path, _content in sources) != 1:
        raise OwnerConflict("target_generic_result_content_missing")
    if len({path for _role, path, _content in sources}) != len(sources):
        raise OwnerConflict("target_generic_result_manifest_invalid")
    return tuple(sources)


def _safe_input_suffix(value: str) -> str:
    suffix = Path(value).suffix
    if len(suffix) > 16 or any(
        character
        not in ".-_abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        for character in suffix
    ):
        return ""
    return suffix


def _asset_receipt_kind(database: Database, version_ref: str) -> str:
    with database.read() as connection:
        value = connection.execute(
            text(
                "SELECT acceptance_kind FROM rm_asset_versions WHERE "
                "version_ref = :version_ref"
            ),
            {"version_ref": version_ref},
        ).scalar_one()
    return str(value)


def _receipt_from_public(value: dict[str, object]) -> AcceptanceReceipt:
    try:
        return AcceptanceReceipt(
            issuer=str(value["issuer"]),
            kind=str(value["kind"]),
            receipt_ref=str(value["receipt_ref"]),
            subject_ref=str(value["subject_ref"]),
            payload_hash=str(value["payload_hash"]),
        )
    except KeyError as error:
        raise OwnerConflict("owner_receipt_integrity_invalid") from error


def _implementation_from_joined_row(row: object) -> AcceptedImplementationRevisionContent:
    try:
        content = json.loads(row.content_json)
    except (TypeError, ValueError) as error:
        raise OwnerConflict("target_implementation_artifact_integrity_invalid") from error
    if not isinstance(content, dict) or canonical_hash(content) != row.content_hash_ref:
        raise OwnerConflict("target_implementation_artifact_integrity_invalid")
    return AcceptedImplementationRevisionContent(
        implementation_revision_ref=row.implementation_revision_ref,
        source_ref=row.source_ref,
        exact_version_ref=row.exact_version_ref,
        license_ref=row.license_ref,
        source_content_hash_ref=row.source_content_hash_ref,
        patch_ref=row.patch_ref,
        verification_evidence_ref=row.verification_evidence_ref,
        content=content,
        content_hash_ref=row.content_hash_ref,
        accepted_at=float(row.accepted_at),
        source_verification_receipt=AcceptanceReceipt(
            issuer="research_memory",
            kind=REUSE_SOURCE_VERSION_RECEIPT_KIND,
            receipt_ref=row.source_receipt_ref,
            subject_ref=row.exact_version_ref,
            payload_hash=row.source_receipt_hash,
        ),
        content_acceptance_receipt=AcceptanceReceipt(
            issuer="research_memory",
            kind=IMPLEMENTATION_CONTENT_RECEIPT_KIND,
            receipt_ref=row.content_receipt_ref,
            subject_ref=row.content_hash_ref,
            payload_hash=row.content_receipt_hash,
        ),
    )


__all__ = [
    "FrozenTargetCommitInput",
    "FrozenTargetCommitInputArtifact",
    "SQLiteTargetExecutionAdmissionVerifier",
    "SQLiteTargetRunAgentAuthority",
    "SQLiteTargetRunGraphAuthority",
    "SQLiteTargetRunMemoryAuthority",
    "canonical_target_scope_binding",
]
