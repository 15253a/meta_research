from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Protocol, TypeAlias, cast


OwnerFact: TypeAlias = int | str | bool | None
QUESTION_PROPOSAL_SCHEMA = "meta-research/question-proposal/v1"


@dataclass(frozen=True)
class OwnerSnapshot:
    owner: str
    revision: int
    facts: dict[str, OwnerFact]
    status: str = "ready"

    def as_public_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "revision": self.revision,
            "facts": self.facts,
        }


@dataclass(frozen=True)
class AcceptanceReceipt:
    issuer: str
    kind: str
    receipt_ref: str
    subject_ref: str
    payload_hash: str

    def as_public_dict(self) -> dict[str, str]:
        return {
            "status": "accepted",
            "issuer": self.issuer,
            "kind": self.kind,
            "receipt_ref": self.receipt_ref,
            "subject_ref": self.subject_ref,
            "payload_hash": self.payload_hash,
        }


@dataclass(frozen=True)
class AcceptedAssetBinding:
    """Exact immutable RM AssetVersion binding consumed across Owner seams."""

    asset_ref: str
    version_ref: str
    content_hash: str
    manifest_hash: str
    receipt: AcceptanceReceipt

    def as_dict(self) -> dict[str, object]:
        return {
            "asset_ref": self.asset_ref,
            "version_ref": self.version_ref,
            "content_hash": self.content_hash,
            "manifest_hash": self.manifest_hash,
            "receipt": self.receipt.as_public_dict(),
        }


class AssetBindingVerifier(Protocol):
    def verify_asset_receipt(
        self,
        *,
        asset_ref: str,
        version_ref: str,
        content_hash: str,
        manifest_hash: str,
        receipt: AcceptanceReceipt,
    ) -> None: ...

    def verify_asset_binding(
        self,
        *,
        asset_ref: str,
        version_ref: str,
        content_hash: str,
        manifest_hash: str,
        receipt: AcceptanceReceipt,
    ) -> None: ...

    def verify_plan_evidence_binding(
        self,
        *,
        asset_ref: str,
        version_ref: str,
        content_hash: str,
        manifest_hash: str,
        target_commit_root_ref: str,
        provenance_closure_refs: tuple[str, ...],
        receipt: AcceptanceReceipt,
        require_current: bool = True,
    ) -> None: ...


class EvidenceRefVerifier(Protocol):
    def verify_evidence_refs(
        self,
        *,
        quest_ref: str,
        version_refs: tuple[str, ...],
        expected_reference_revision: int | None = None,
        require_current: bool = False,
    ) -> None: ...

    def assert_evidence_state(
        self,
        *,
        quest_ref: str,
        version_refs: tuple[str, ...],
        expected_reference_revision: int,
    ) -> None: ...

    def verify_plan_evidence_catalog(
        self,
        *,
        quest_ref: str,
        evidence_catalog: list[dict[str, object]],
        expected_reference_revision: int,
        require_current: bool = True,
    ) -> None: ...


class AssetReferenceReader(Protocol):
    def query_asset_reference_revision(self) -> int: ...

    def query_asset_references(self, version_ref: str) -> tuple[str, ...]: ...

    def query_asset_reference_state(
        self, version_ref: str
    ) -> tuple[int, tuple[str, ...]]: ...


@dataclass(frozen=True)
class AcceptedQuestionBinding:
    """Exact cross-Owner input frozen into a Stage invocation.

    The binding deliberately carries only stable identities and issuer receipts.
    Question content remains opaque data supplied separately by Research Memory.
    """

    initialization_id: str
    quest_ref: str
    question_ref: str
    content_ref: str
    content_hash: str
    schema_ref: str
    content_receipt: AcceptanceReceipt
    question_receipt: AcceptanceReceipt

    def as_dict(self) -> dict[str, object]:
        return {
            "initialization_id": self.initialization_id,
            "quest_ref": self.quest_ref,
            "question_ref": self.question_ref,
            "content_ref": self.content_ref,
            "content_hash": self.content_hash,
            "schema_ref": self.schema_ref,
            "content_receipt": self.content_receipt.as_public_dict(),
            "question_receipt": self.question_receipt.as_public_dict(),
        }


class AcceptedQuestionBindingVerifier(Protocol):
    def verify_accepted_question_binding(
        self, binding: AcceptedQuestionBinding
    ) -> None: ...


@dataclass(frozen=True)
class AcceptedIdeaSetBinding:
    """Exact accepted IdeaSet closure consumed by Plan.

    The complete immutable IdeaSet remains data. Its RM, RG, and AE receipts
    independently prove content acceptance, domain acceptance, and stage
    advancement; none substitutes for another.
    """

    outcome_ref: str
    content_ref: str
    payload_hash: str
    outcome_hash: str
    content_receipt: AcceptanceReceipt
    outcome_receipt: AcceptanceReceipt
    stage_commit_ref: str
    stage_commit_receipt: AcceptanceReceipt
    idea_set: dict[str, object]
    outcome_kind: str = "idea_set"

    def as_dict(self) -> dict[str, object]:
        return {
            "outcome_ref": self.outcome_ref,
            "outcome_kind": self.outcome_kind,
            "content_ref": self.content_ref,
            "payload_hash": self.payload_hash,
            "outcome_hash": self.outcome_hash,
            "content_receipt": self.content_receipt.as_public_dict(),
            "outcome_receipt": self.outcome_receipt.as_public_dict(),
            "stage_commit_ref": self.stage_commit_ref,
            "stage_commit_receipt": self.stage_commit_receipt.as_public_dict(),
            "idea_set": self.idea_set,
        }


@dataclass(frozen=True)
class VerifiedStageRunRequestBinding:
    request_ref: str
    cycle_ref: str
    epoch: int
    accepted_question: AcceptedQuestionBinding
    context_pack_ref: str
    context_pack_hash: str
    context_pack: dict[str, object]
    receipt: AcceptanceReceipt
    accepted_idea_set: AcceptedIdeaSetBinding | None = None


class BundleConfirmationVerifier(Protocol):
    def verify_bundle_confirmation(
        self,
        *,
        initialization_id: str,
        draft_revision: int,
        draft_hash: str,
        proposal_ref: str,
        proposal_hash: str,
        preview_ref: str,
        preview_hash: str,
        receipt: AcceptanceReceipt,
    ) -> None: ...


class LiteratureSnapshotVerifier(Protocol):
    def verify_literature_snapshot_binding(
        self,
        *,
        snapshot_ref: str,
        snapshot_hash: str,
        initialization_id: str,
        draft_revision: int,
        draft_hash: str,
        receipt: AcceptanceReceipt | None = None,
    ) -> None: ...


class QuestReceiptVerifier(Protocol):
    def verify_quest_receipt(
        self,
        *,
        initialization_id: str,
        quest_ref: str,
        proposal_ref: str,
        proposal_hash: str,
        confirmation_ref: str,
        receipt: AcceptanceReceipt,
    ) -> None: ...


class QuestionContentReceiptVerifier(Protocol):
    def verify_question_content_receipt(
        self,
        *,
        initialization_id: str,
        content_ref: str,
        content_hash: str,
        schema_ref: str,
        proposal_ref: str,
        proposal_hash: str,
        confirmation_ref: str,
        receipt: AcceptanceReceipt,
    ) -> None: ...


class RootQuestionReceiptVerifier(Protocol):
    def verify_root_question_receipt(
        self,
        *,
        initialization_id: str,
        quest_ref: str,
        question_ref: str,
        receipt: AcceptanceReceipt,
    ) -> None: ...


class StageRunRequestVerifier(Protocol):
    def verify_stage_run_request(
        self,
        *,
        request_ref: str,
        cycle_ref: str,
        epoch: int,
        context_pack_ref: str,
        context_pack_hash: str,
        receipt: AcceptanceReceipt,
    ) -> None: ...

    def verify_idea_stage_request_binding(
        self,
        *,
        request_ref: str,
        accepted_question: AcceptedQuestionBinding,
        context_pack_ref: str,
    ) -> VerifiedStageRunRequestBinding: ...

    def verify_plan_stage_request_binding(
        self,
        *,
        request_ref: str,
        accepted_question: AcceptedQuestionBinding,
        accepted_idea_set: AcceptedIdeaSetBinding,
        context_pack_ref: str,
    ) -> VerifiedStageRunRequestBinding: ...

    def query_verified_plan_stage_request(
        self,
        *,
        request_ref: str,
        context_pack_ref: str,
    ) -> VerifiedStageRunRequestBinding: ...


class DeepFetchRunRequestVerifier(Protocol):
    def verify_deepfetch_run_request(
        self,
        *,
        request_ref: str,
        initialization_id: str,
        correlation_ref: str,
        draft_revision: int,
        draft_hash: str,
        scope_hash: str,
        material_bindings_hash: str,
        resource_envelope_ref: str,
        resource_envelope_hash: str,
        acquisition_session_ref: str,
        acquisition_config_hash: str,
        acquisition_runtime_binding_hash: str,
        result_route: str,
        receipt: AcceptanceReceipt,
        require_active: bool = False,
    ) -> None: ...


class AttemptExecutionReceiptVerifier(Protocol):
    def verify_attempt_execution_receipt(
        self,
        *,
        request_ref: str,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        submission_ref: str,
        payload_hash: str,
        receipt: AcceptanceReceipt,
    ) -> None: ...

    def verify_deepfetch_execution_receipt(
        self,
        *,
        request_ref: str,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        result_hash: str,
        receipt: AcceptanceReceipt,
    ) -> None: ...


class IdeaContentReceiptVerifier(Protocol):
    def verify_idea_content_receipt(
        self,
        *,
        request_ref: str,
        submission_ref: str,
        content_ref: str,
        payload_hash: str,
        outcome_hash: str,
        reviewed_draft_hash: str,
        review_hash: str,
        receipt: AcceptanceReceipt,
    ) -> None: ...


class PlanContentReceiptVerifier(Protocol):
    def verify_plan_content_receipt(
        self,
        *,
        request_ref: str,
        submission_ref: str,
        content_ref: str,
        payload_hash: str,
        plan_hash: str,
        reviewed_draft_hash: str,
        review_hash: str,
        receipt: AcceptanceReceipt,
    ) -> None: ...


class IdeaOutcomeDecisionVerifier(Protocol):
    def verify_accepted_idea_set_binding(
        self, binding: AcceptedIdeaSetBinding
    ) -> None: ...

    def verify_idea_outcome_decision(
        self,
        *,
        request_ref: str,
        submission_ref: str | None,
        decision: str,
        outcome_ref: str | None,
        receipt: AcceptanceReceipt,
        outcome_kind: str | None = None,
    ) -> None: ...


class FormalPlanDecisionVerifier(Protocol):
    def verify_formal_plan_decision(
        self,
        *,
        request_ref: str,
        submission_ref: str | None,
        decision: str,
        formal_plan_ref: str | None,
        receipt: AcceptanceReceipt,
    ) -> None: ...


class RunCompletionReceiptVerifier(Protocol):
    def verify_run_completion_receipt(
        self,
        *,
        request_ref: str,
        run_ref: str,
        attempt_ref: str | None,
        outcome_ref: str,
        receipt: AcceptanceReceipt,
    ) -> None: ...


class OwnerConflict(RuntimeError):
    """An idempotent Owner command was replayed with different semantics."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_hash(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def decoded_object(value: str) -> dict[str, object]:
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise ValueError("stored value is not a JSON object")
    return cast(dict[str, object], decoded)


def new_ref(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"
