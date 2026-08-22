from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import os
import stat
import tempfile
import threading
import time
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Literal, Protocol
from urllib.parse import urlsplit

from sqlalchemy import text

from meta_research.database import Database
from meta_research.deepfetch import DeepFetchRunRequest
from meta_research.feed import DurableFeed
from meta_research.idea_contract import IdeaContractError, validate_idea_content
from meta_research.plan_contract import (
    PlanContractError,
    validate_plan_context_pack,
    validate_plan_document,
    validate_plan_review,
)
from meta_research.owners._sqlite_snapshot import (
    OwnerSnapshotQuery,
    SQLiteOwnerSnapshot,
)
from meta_research.owners.common import (
    AcceptedAssetBinding,
    AcceptedIdeaSetBinding,
    AcceptedQuestionBinding,
    AcceptanceReceipt,
    AssetReferenceReader,
    AttemptExecutionReceiptVerifier,
    BundleConfirmationVerifier,
    OwnerConflict,
    OwnerSnapshot,
    QUESTION_PROPOSAL_SCHEMA,
    QuestReceiptVerifier,
    StageRunRequestVerifier,
    canonical_hash,
    canonical_json,
    decoded_object,
    new_ref,
)
from meta_research.owners.agent_runtime import (
    ATTEMPT_EXECUTION_SCHEMA,
    DEEPFETCH_EXECUTION_RECEIPT_KIND,
    PLAN_ATTEMPT_EXECUTION_RECEIPT_KIND,
    PLAN_ATTEMPT_EXECUTION_SCHEMA,
    DeepFetchRun,
)
from meta_research.owners.research_graph import AcceptedQuest


QUESTION_CONTENT_SCHEMA = "meta-research/formal-question-content/v1"
ASSET_MANIFEST_SCHEMA = "meta-research/asset-manifest/v1"
RM_OWNER = "research_memory"
CONTENT_RECEIPT_KIND = "question_content_acceptance"
IDEA_CONTENT_RECEIPT_KIND = "idea_outcome_content_acceptance"
PLAN_CONTENT_RECEIPT_KIND = "plan_document_content_acceptance"
LITERATURE_SNAPSHOT_RECEIPT_KIND = "literature_snapshot_acceptance"
ASSET_RECEIPT_KIND = "asset_acceptance"
ASSET_CUSTODY_ESTABLISHED_RECEIPT_KIND = "asset_custody_established"
ASSET_CUSTODY_LOCATOR_MIGRATED_RECEIPT_KIND = (
    "asset_custody_locator_migrated"
)
MAX_ASSET_BYTES = 64 * 1024 * 1024
MAX_ASSET_FILES = 10_000
MAX_ASSET_PROVENANCE_BYTES = 64 * 1024
MAX_ASSET_PROVENANCE_DEPTH = 8
MAX_ASSET_PROVENANCE_NODES = 1_024
ASSET_PROJECTION_PAGE_SIZE = 50
ASSET_PROJECTION_MAX_PAGE_SIZE = 100
ASSET_PROJECTION_HISTORY_PER_VERSION = 20
ASSET_HISTORY_QUERY_MAX_PAGE_SIZE = 100
MAX_ACTIVE_ASSET_HOLDS_PER_VERSION = 20
MAX_PENDING_ASSET_INTAKES = 32
MAX_PENDING_ASSET_REQUEST_BYTES = 256 * 1024 * 1024
ASSET_HASH_CHUNK_BYTES = 1024 * 1024
ASSET_CUSTODY_RECEIPT_KIND = "asset_custody_handoff"
ASSET_HOLD_PLACED_RECEIPT_KIND = "asset_hold_placed"
ASSET_HOLD_RELEASED_RECEIPT_KIND = "asset_hold_released"
RELEASE_ELIGIBILITY_RECEIPT_KIND = "release_eligibility_assessment"
RECEIPT_SCHEMA = "meta-research/owner-acceptance-receipt/v1"
ASSET_INTAKE_LEASE_SECONDS = 3600.0
ASSET_INTAKE_MAX_ATTEMPTS = 5
ASSET_INTAKE_RETRY_BASE_SECONDS = 1.0
ASSET_VERIFICATION_INTERVAL_SECONDS = 300.0
TRANSIENT_ASSET_INTAKE_CONFLICTS = {
    "asset_source_unavailable",
    "asset_source_changed_during_intake",
}

AssetSourceKind = Literal[
    "text",
    "file",
    "directory",
    "local_path",
    "repository",
    "link",
    "system_artifact",
]
AssetCustodyMode = Literal["managed", "linked_local"]


@dataclass(frozen=True)
class AssetIntakeRequest:
    """One exact request at the Research Memory Asset Intake seam."""

    source_kind: AssetSourceKind
    custody_mode: AssetCustodyMode
    display_name: str
    media_type: str = "application/octet-stream"
    content: bytes | None = None
    source_locator: str | None = None
    provenance: dict[str, object] | None = None
    asset_ref: str | None = None
    asynchronous: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "source_kind": self.source_kind,
            "custody_mode": self.custody_mode,
            "display_name": self.display_name,
            "media_type": self.media_type,
            "content": self.content,
            "source_locator": self.source_locator,
            "provenance": self.provenance,
            "asset_ref": self.asset_ref,
            "asynchronous": self.asynchronous,
        }

    def validate(self) -> None:
        _asset_request_document(self)


@dataclass(frozen=True)
class AcceptedAssetVersion:
    asset_ref: str
    version_ref: str
    memory_ref: str
    version_number: int
    source_kind: str
    display_name: str
    media_type: str
    content_hash: str
    manifest_hash: str
    byte_count: int
    provenance: dict[str, object]
    custody_modes: tuple[str, ...]
    accepted_at: float
    receipt: AcceptanceReceipt

    def as_binding(self) -> AcceptedAssetBinding:
        return AcceptedAssetBinding(
            asset_ref=self.asset_ref,
            version_ref=self.version_ref,
            content_hash=self.content_hash,
            manifest_hash=self.manifest_hash,
            receipt=self.receipt,
        )

    def as_public_dict(self) -> dict[str, object]:
        return {
            "asset_ref": self.asset_ref,
            "version_ref": self.version_ref,
            "memory_ref": self.memory_ref,
            "version_number": self.version_number,
            "source_kind": self.source_kind,
            "display_name": self.display_name,
            "media_type": self.media_type,
            "content_hash": self.content_hash,
            "manifest_hash": self.manifest_hash,
            "byte_count": self.byte_count,
            "provenance": self.provenance,
            "custody_modes": list(self.custody_modes),
            "accepted_at": self.accepted_at,
            "receipt": self.receipt.as_public_dict(),
        }


@dataclass(frozen=True)
class AssetIntakeResult:
    job_ref: str
    status: str
    source_kind: str
    custody_mode: str
    attempt_count: int
    asset: AcceptedAssetVersion | None
    failure_code: str | None

    def as_public_dict(self) -> dict[str, object]:
        return {
            "job_ref": self.job_ref,
            "status": self.status,
            "source_kind": self.source_kind,
            "custody_mode": self.custody_mode,
            "attempt_count": self.attempt_count,
            "asset": None if self.asset is None else self.asset.as_public_dict(),
            "failure": (
                None if self.failure_code is None else {"code": self.failure_code}
            ),
        }


@dataclass(frozen=True)
class AssetInventoryItem:
    asset_ref: str
    version_ref: str
    memory_ref: str
    version_number: int
    source_kind: str
    display_name: str
    media_type: str
    content_hash: str
    manifest_hash: str
    byte_count: int
    provenance: dict[str, object]
    custody_modes: tuple[str, ...]
    integrity: str
    availability: str
    verification_observed_at: float | None
    verification_pending: bool
    accepted_at: float
    receipt: AcceptanceReceipt

    def as_public_dict(self) -> dict[str, object]:
        return {
            "asset_ref": self.asset_ref,
            "version_ref": self.version_ref,
            "memory_ref": self.memory_ref,
            "version_number": self.version_number,
            "source_kind": self.source_kind,
            "display_name": self.display_name,
            "media_type": self.media_type,
            "content_hash": self.content_hash,
            "manifest_hash": self.manifest_hash,
            "byte_count": self.byte_count,
            "provenance": self.provenance,
            "custody_modes": list(self.custody_modes),
            "integrity": self.integrity,
            "availability": self.availability,
            "verification_observed_at": self.verification_observed_at,
            "verification_pending": self.verification_pending,
            "accepted_at": self.accepted_at,
            "receipt": self.receipt.as_public_dict(),
        }


@dataclass(frozen=True)
class MaterializedAsset:
    memory_ref: str
    file_name: str
    media_type: str
    content: bytes


@dataclass(frozen=True)
class AcceptedAssetCustody:
    version_ref: str
    custody_ref: str
    custody_mode: str
    source_locator: str | None
    locator_receipted: bool
    locator_bound_at: float | None
    locator_receipt: AcceptanceReceipt | None
    established_at: float
    receipt: AcceptanceReceipt

    def as_public_dict(self) -> dict[str, object]:
        return {
            "version_ref": self.version_ref,
            "custody_ref": self.custody_ref,
            "custody_mode": self.custody_mode,
            "source_locator": self.source_locator,
            "locator_receipted": self.locator_receipted,
            "locator_bound_at": self.locator_bound_at,
            "locator_receipt": (
                None
                if self.locator_receipt is None
                else self.locator_receipt.as_public_dict()
            ),
            "established_at": self.established_at,
            "receipt": self.receipt.as_public_dict(),
        }


@dataclass(frozen=True)
class AcceptedAssetHold:
    hold_ref: str
    version_ref: str
    reason: str
    active: bool
    placed_at: float
    released_at: float | None
    placement_receipt: AcceptanceReceipt
    release_receipt: AcceptanceReceipt | None

    def as_public_dict(self) -> dict[str, object]:
        return {
            "hold_ref": self.hold_ref,
            "version_ref": self.version_ref,
            "reason": self.reason,
            "active": self.active,
            "placed_at": self.placed_at,
            "released_at": self.released_at,
            "placement_receipt": self.placement_receipt.as_public_dict(),
            "release_receipt": (
                None
                if self.release_receipt is None
                else self.release_receipt.as_public_dict()
            ),
        }


@dataclass(frozen=True)
class ReleaseEligibilityAssessment:
    assessment_ref: str
    version_ref: str
    expected_reference_revision: int | None
    observed_reference_revision: int | None
    active_reference_refs: tuple[str, ...]
    active_hold_refs: tuple[str, ...]
    eligible: bool
    reason_codes: tuple[str, ...]
    assessed_at: float
    receipt: AcceptanceReceipt

    def as_public_dict(self) -> dict[str, object]:
        return {
            "assessment_ref": self.assessment_ref,
            "version_ref": self.version_ref,
            "expected_reference_revision": self.expected_reference_revision,
            "observed_reference_revision": self.observed_reference_revision,
            "active_reference_refs": list(self.active_reference_refs),
            "active_hold_refs": list(self.active_hold_refs),
            "eligible": self.eligible,
            "reason_codes": list(self.reason_codes),
            "assessed_at": self.assessed_at,
            "receipt": self.receipt.as_public_dict(),
        }


@dataclass(frozen=True)
class _PreparedAsset:
    manifest: dict[str, object]
    content_hash: str
    byte_count: int


@dataclass(frozen=True)
class AcceptedQuestionContent:
    initialization_id: str
    content_ref: str
    content_hash: str
    schema_ref: str
    proposal_ref: str
    proposal_hash: str
    confirmation_ref: str
    receipt: AcceptanceReceipt


@dataclass(frozen=True)
class AcceptedIdeaOutcomeContent:
    request_ref: str
    run_ref: str
    attempt_ref: str
    fence_ref: str
    submission_ref: str
    content_ref: str
    outcome_kind: str
    payload_hash: str
    outcome_hash: str
    reviewed_draft_hash: str
    review_hash: str
    outcome: dict[str, object]
    reviewed_draft: dict[str, object]
    review: dict[str, object]
    execution_receipt: AcceptanceReceipt
    receipt: AcceptanceReceipt


@dataclass(frozen=True)
class AcceptedPlanDocument:
    request_ref: str
    run_ref: str
    attempt_ref: str
    fence_ref: str
    submission_ref: str
    initialization_id: str
    quest_ref: str
    question_ref: str
    context_pack_ref: str
    question_content_ref: str
    question_content_hash: str
    question_content_receipt: AcceptanceReceipt
    question_receipt: AcceptanceReceipt
    idea_outcome_ref: str
    idea_content_ref: str
    idea_content_hash: str
    idea_content_receipt: AcceptanceReceipt
    idea_outcome_receipt: AcceptanceReceipt
    idea_stage_commit_ref: str
    idea_stage_commit_receipt: AcceptanceReceipt
    content_ref: str
    payload_hash: str
    plan_document_hash: str
    answer_contract_hash: str
    reviewed_draft_hash: str
    review_hash: str
    plan_document: dict[str, object]
    reviewed_draft: dict[str, object]
    review: dict[str, object]
    execution_receipt: AcceptanceReceipt
    receipt: AcceptanceReceipt


@dataclass(frozen=True)
class AcceptedLiteratureSnapshot:
    snapshot_ref: str
    request_ref: str
    initialization_id: str
    draft_revision: int
    draft_hash: str
    scope_hash: str
    run_ref: str
    attempt_ref: str
    fence_ref: str
    result_hash: str
    completion: str
    summary_ref: str
    summary_hash: str
    papers_ref: str
    papers_hash: str
    fulltexts_ref: str
    fulltexts_hash: str
    limitations: tuple[str, ...]
    web_evidence_hash: str
    snapshot_hash: str
    paper_count: int
    fulltext_count: int
    execution_receipt: AcceptanceReceipt
    receipt: AcceptanceReceipt

    def as_public_dict(self) -> dict[str, object]:
        return {
            "status": "accepted",
            "snapshot_ref": self.snapshot_ref,
            "request_ref": self.request_ref,
            "initialization_id": self.initialization_id,
            "draft_revision": self.draft_revision,
            "draft_hash": self.draft_hash,
            "scope_hash": self.scope_hash,
            "completion": self.completion,
            "summary_ref": self.summary_ref,
            "summary_hash": self.summary_hash,
            "papers_ref": self.papers_ref,
            "papers_hash": self.papers_hash,
            "fulltexts_ref": self.fulltexts_ref,
            "fulltexts_hash": self.fulltexts_hash,
            "limitations": list(self.limitations),
            "web_evidence_hash": self.web_evidence_hash,
            "snapshot_hash": self.snapshot_hash,
            "paper_count": self.paper_count,
            "fulltext_count": self.fulltext_count,
            "receipt": self.receipt.as_public_dict(),
        }

    def as_context_binding(self) -> dict[str, object]:
        """Return the compact issuer-owned binding carried into Idea."""

        return {
            "schema_ref": "meta-research/idea-literature-binding/v1",
            "snapshot_ref": self.snapshot_ref,
            "snapshot_hash": self.snapshot_hash,
            "initialization_id": self.initialization_id,
            "draft_revision": self.draft_revision,
            "draft_hash": self.draft_hash,
            "receipt": self.receipt.as_public_dict(),
        }


class ResearchMemoryInterface(Protocol):
    """Whole public Interface for immutable content identity and custody."""

    def query_snapshot(self) -> OwnerSnapshot: ...

    def query_projection_snapshot(self) -> OwnerSnapshot: ...

    def submit_asset_intake(
        self, request: AssetIntakeRequest, *, idempotency_key: str
    ) -> AssetIntakeResult: ...

    def process_asset_intake_once(self) -> bool: ...

    def verify_asset_inventory_once(self) -> bool: ...

    def query_asset_intake(self, job_ref: str) -> AssetIntakeResult: ...

    def query_asset_intake_by_idempotency_key(
        self, idempotency_key: str, request: AssetIntakeRequest
    ) -> AssetIntakeResult | None: ...

    def query_asset_version(
        self, memory_ref: str
    ) -> AcceptedAssetVersion | None: ...

    def query_asset_inventory(self) -> tuple[AssetInventoryItem, ...]: ...

    def query_asset_projection_inventory(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[AssetInventoryItem, ...]: ...

    def query_asset_inventory_item(
        self, memory_ref: str
    ) -> AssetInventoryItem | None: ...

    def query_asset_projection_inventory_item(
        self, memory_ref: str
    ) -> AssetInventoryItem | None: ...

    def materialize_asset(self, memory_ref: str) -> MaterializedAsset: ...

    def handoff_asset_to_managed(
        self, memory_ref: str, *, idempotency_key: str
    ) -> AcceptedAssetCustody: ...

    def query_asset_custodies(
        self,
        memory_ref: str | None = None,
        *,
        memory_refs: tuple[str, ...] | None = None,
    ) -> tuple[AcceptedAssetCustody, ...]: ...

    def verify_asset_custody_receipt(
        self,
        *,
        custody_ref: str,
        version_ref: str,
        custody_mode: str,
        receipt: AcceptanceReceipt,
    ) -> None: ...

    def place_asset_hold(
        self, memory_ref: str, *, reason: str, idempotency_key: str
    ) -> AcceptedAssetHold: ...

    def release_asset_hold(
        self, hold_ref: str, *, idempotency_key: str
    ) -> AcceptedAssetHold: ...

    def query_asset_holds(
        self,
        memory_ref: str | None = None,
        *,
        memory_refs: tuple[str, ...] | None = None,
        limit_per_version: int | None = None,
        limit: int | None = None,
        offset: int = 0,
        newest_first: bool = False,
        before_timestamp: float | None = None,
        before_ref: str | None = None,
    ) -> tuple[AcceptedAssetHold, ...]: ...

    def assess_release_eligibility(
        self,
        memory_ref: str,
        *,
        expected_reference_revision: int | None,
        idempotency_key: str,
    ) -> ReleaseEligibilityAssessment: ...

    def query_release_eligibility_assessments(
        self,
        memory_ref: str | None = None,
        *,
        memory_refs: tuple[str, ...] | None = None,
        limit_per_version: int | None = None,
        limit: int | None = None,
        offset: int = 0,
        newest_first: bool = False,
        before_timestamp: float | None = None,
        before_ref: str | None = None,
    ) -> tuple[ReleaseEligibilityAssessment, ...]: ...

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

    def verify_asset_projection_binding(
        self,
        *,
        asset_ref: str,
        version_ref: str,
        content_hash: str,
        manifest_hash: str,
        receipt: AcceptanceReceipt,
    ) -> None: ...

    def preview_question_content_acceptance(
        self,
        *,
        initialization_id: str,
        proposal_ref: str,
        proposal_hash: str,
    ) -> dict[str, object]: ...

    def query_question_content(
        self, initialization_id: str
    ) -> AcceptedQuestionContent | None: ...

    def read_question_content(
        self, content_ref: str, expected_hash: str
    ) -> dict[str, object]: ...

    def accept_question_content(
        self,
        *,
        initialization_id: str,
        quest: AcceptedQuest,
        content: dict[str, object],
        content_hash: str,
    ) -> AcceptedQuestionContent: ...

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

    def accept_idea_outcome_content(
        self,
        *,
        request_ref: str,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        submission_ref: str,
        outcome: dict[str, object],
        review: dict[str, object],
        execution_receipt: AcceptanceReceipt,
        reviewed_draft: dict[str, object] | None = None,
    ) -> AcceptedIdeaOutcomeContent: ...

    def query_idea_outcome_content(
        self, submission_ref: str
    ) -> AcceptedIdeaOutcomeContent | None: ...

    def verify_idea_content_receipt(self, **values) -> None: ...

    def accept_plan_document(
        self,
        *,
        accepted_question: AcceptedQuestionBinding,
        accepted_idea_set: AcceptedIdeaSetBinding,
        context_pack_ref: str,
        request_ref: str,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        submission_ref: str,
        plan_document: dict[str, object],
        review: dict[str, object],
        execution_receipt: AcceptanceReceipt,
        reviewed_draft: dict[str, object] | None = None,
    ) -> AcceptedPlanDocument: ...

    def query_plan_document(
        self, submission_ref: str
    ) -> AcceptedPlanDocument | None: ...

    def verify_plan_content_receipt(self, **values) -> None: ...

    def accept_literature_snapshot(
        self,
        request: DeepFetchRunRequest,
        run: DeepFetchRun,
    ) -> AcceptedLiteratureSnapshot: ...

    def query_literature_snapshot(
        self, snapshot_ref: str
    ) -> AcceptedLiteratureSnapshot | None: ...

    def query_literature_snapshot_for_request(
        self, request_ref: str
    ) -> AcceptedLiteratureSnapshot | None: ...

    def query_literature_snapshot_for_basis(
        self, initialization_id: str, draft_revision: int, draft_hash: str
    ) -> AcceptedLiteratureSnapshot | None: ...

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

    def read_literature_snapshot(self, snapshot_ref: str) -> dict[str, object]: ...


_SNAPSHOT = OwnerSnapshotQuery(
    owner=RM_OWNER,
    statement=text(
        "SELECT revision, asset_count, object_count, formal_content_count, "
        "idea_content_count, plan_content_count, asset_version_count, "
        "pending_intake_count, hold_count, "
        "literature_snapshot_count "
        "FROM research_memory_state WHERE singleton = 'owner'"
    ),
    fact_names=(
        "asset_count",
        "object_count",
        "formal_content_count",
        "idea_content_count",
        "plan_content_count",
        "asset_version_count",
        "pending_intake_count",
        "hold_count",
        "literature_snapshot_count",
    ),
)


class SQLiteResearchMemoryReceiptVerifier:
    """Narrow RM issuer verifier for historical receipts and current bindings."""

    def __init__(
        self,
        database: Database,
        object_store: Path,
        execution_verifier: AttemptExecutionReceiptVerifier | None = None,
        stage_request_verifier: StageRunRequestVerifier | None = None,
    ) -> None:
        self._database = database
        self._object_store = object_store
        self._execution_verifier = execution_verifier
        self._stage_request_verifier = stage_request_verifier

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
    ) -> None:
        if receipt.issuer != RM_OWNER or receipt.kind != CONTENT_RECEIPT_KIND:
            raise OwnerConflict("question_content_receipt_issuer_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rm_formal_question_contents WHERE "
                    "initialization_id = :initialization_id AND content_ref = :content_ref"
                ),
                {
                    "initialization_id": initialization_id,
                    "content_ref": content_ref,
                },
            ).first()
        if row is None or (
            row.content_hash != content_hash
            or row.schema_ref != schema_ref
            or row.proposal_ref != proposal_ref
            or row.proposal_hash != proposal_hash
            or row.confirmation_ref != confirmation_ref
            or row.receipt_ref != receipt.receipt_ref
            or row.receipt_hash != receipt.payload_hash
            or receipt.subject_ref != content_ref
            or row.receipt_hash != _content_receipt_hash(row)
        ):
            raise OwnerConflict("question_content_receipt_invalid")
        _verify_object(self._object_store, row)

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
    ) -> None:
        if (
            receipt.issuer != RM_OWNER
            or receipt.kind != IDEA_CONTENT_RECEIPT_KIND
            or receipt.subject_ref != content_ref
        ):
            raise OwnerConflict("idea_content_receipt_issuer_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rm_idea_outcome_contents WHERE content_ref = "
                    ":content_ref AND submission_ref = :submission_ref"
                ),
                {"content_ref": content_ref, "submission_ref": submission_ref},
            ).first()
        if row is None or (
            row.request_ref != request_ref
            or row.payload_hash != payload_hash
            or row.outcome_hash != outcome_hash
            or row.reviewed_draft_hash != reviewed_draft_hash
            or row.review_hash != review_hash
            or row.receipt_ref != receipt.receipt_ref
            or row.receipt_hash != receipt.payload_hash
            or row.receipt_hash != _idea_content_receipt_hash(row)
        ):
            raise OwnerConflict("idea_content_receipt_invalid")
        _verify_idea_object(self._object_store, row)
        _verify_idea_payload(row)
        if self._execution_verifier is not None:
            self._execution_verifier.verify_attempt_execution_receipt(
                request_ref=row.request_ref,
                run_ref=row.run_ref,
                attempt_ref=row.attempt_ref,
                fence_ref=row.fence_ref,
                submission_ref=row.submission_ref,
                payload_hash=row.payload_hash,
                receipt=AcceptanceReceipt(
                    issuer="agent_runtime",
                    kind="idea_attempt_execution",
                    receipt_ref=row.execution_receipt_ref,
                    subject_ref=row.submission_ref,
                    payload_hash=row.execution_receipt_hash,
                ),
            )

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
    ) -> None:
        if (
            receipt.issuer != RM_OWNER
            or receipt.kind != PLAN_CONTENT_RECEIPT_KIND
            or receipt.subject_ref != content_ref
        ):
            raise OwnerConflict("plan_content_receipt_issuer_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rm_plan_documents WHERE content_ref = "
                    ":content_ref AND submission_ref = :submission_ref"
                ),
                {"content_ref": content_ref, "submission_ref": submission_ref},
            ).first()
        if row is None or (
            row.request_ref != request_ref
            or row.payload_hash != payload_hash
            or row.plan_document_hash != plan_hash
            or row.reviewed_draft_hash != reviewed_draft_hash
            or row.review_hash != review_hash
            or row.receipt_ref != receipt.receipt_ref
            or row.receipt_hash != receipt.payload_hash
            or row.receipt_hash != _plan_content_receipt_hash(row)
        ):
            raise OwnerConflict("plan_content_receipt_invalid")
        if self._stage_request_verifier is None:
            raise OwnerConflict("stage_request_verifier_unavailable")
        verified_request = (
            self._stage_request_verifier.query_verified_plan_stage_request(
                request_ref=request_ref,
                context_pack_ref=row.context_pack_ref,
            )
        )
        _verify_plan_object(self._object_store, row)
        _verify_plan_payload(row, verified_request)
        if self._execution_verifier is not None:
            self._execution_verifier.verify_attempt_execution_receipt(
                request_ref=row.request_ref,
                run_ref=row.run_ref,
                attempt_ref=row.attempt_ref,
                fence_ref=row.fence_ref,
                submission_ref=row.submission_ref,
                payload_hash=row.payload_hash,
                receipt=AcceptanceReceipt(
                    issuer="agent_runtime",
                    kind=PLAN_ATTEMPT_EXECUTION_RECEIPT_KIND,
                    receipt_ref=row.execution_receipt_ref,
                    subject_ref=row.submission_ref,
                    payload_hash=row.execution_receipt_hash,
                ),
            )

    def verify_asset_receipt(
        self,
        *,
        asset_ref: str,
        version_ref: str,
        content_hash: str,
        manifest_hash: str,
        receipt: AcceptanceReceipt,
    ) -> None:
        if receipt.issuer != RM_OWNER or receipt.subject_ref != version_ref:
            raise OwnerConflict("asset_receipt_issuer_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rm_asset_versions WHERE version_ref = "
                    ":version_ref"
                ),
                {"version_ref": version_ref},
            ).first()
            custodies = connection.execute(
                text(
                    "SELECT * FROM rm_asset_custodies WHERE version_ref = "
                    ":version_ref ORDER BY custody_mode"
                ),
                {"version_ref": version_ref},
            ).all()
        if row is None or (
            row.asset_ref != asset_ref
            or row.content_hash != content_hash
            or row.manifest_hash != manifest_hash
            or row.acceptance_kind != receipt.kind
            or row.receipt_ref != receipt.receipt_ref
            or row.receipt_hash != receipt.payload_hash
        ):
            raise OwnerConflict("asset_receipt_invalid")
        _verify_asset_metadata(row, custodies, require_portable_paths=False)
        if row.acceptance_kind == ASSET_RECEIPT_KIND:
            if row.receipt_hash != _asset_receipt_hash(row, custodies):
                raise OwnerConflict("asset_receipt_invalid")
        elif row.acceptance_kind == CONTENT_RECEIPT_KIND:
            legacy = _legacy_question_row(self._database, version_ref)
            if (
                legacy.content_hash != content_hash
                or legacy.receipt_ref != receipt.receipt_ref
                or legacy.receipt_hash != receipt.payload_hash
                or legacy.receipt_hash != _content_receipt_hash(legacy)
            ):
                raise OwnerConflict("asset_receipt_invalid")
        elif row.acceptance_kind == IDEA_CONTENT_RECEIPT_KIND:
            legacy = _legacy_idea_row(self._database, version_ref)
            if (
                legacy.payload_hash != content_hash
                or legacy.receipt_ref != receipt.receipt_ref
                or legacy.receipt_hash != receipt.payload_hash
                or legacy.receipt_hash != _idea_content_receipt_hash(legacy)
            ):
                raise OwnerConflict("asset_receipt_invalid")
            _verify_idea_payload(legacy)
            if self._execution_verifier is not None:
                self._execution_verifier.verify_attempt_execution_receipt(
                    request_ref=legacy.request_ref,
                    run_ref=legacy.run_ref,
                    attempt_ref=legacy.attempt_ref,
                    fence_ref=legacy.fence_ref,
                    submission_ref=legacy.submission_ref,
                    payload_hash=legacy.payload_hash,
                    receipt=AcceptanceReceipt(
                        issuer="agent_runtime",
                        kind="idea_attempt_execution",
                        receipt_ref=legacy.execution_receipt_ref,
                        subject_ref=legacy.submission_ref,
                        payload_hash=legacy.execution_receipt_hash,
                    ),
                )
        elif row.acceptance_kind == PLAN_CONTENT_RECEIPT_KIND:
            plan = _plan_row(self._database, version_ref)
            if (
                plan.payload_hash != content_hash
                or plan.receipt_ref != receipt.receipt_ref
                or plan.receipt_hash != receipt.payload_hash
                or plan.receipt_hash != _plan_content_receipt_hash(plan)
            ):
                raise OwnerConflict("asset_receipt_invalid")
            _verify_plan_payload(plan, None)
            if self._execution_verifier is not None:
                self._execution_verifier.verify_attempt_execution_receipt(
                    request_ref=plan.request_ref,
                    run_ref=plan.run_ref,
                    attempt_ref=plan.attempt_ref,
                    fence_ref=plan.fence_ref,
                    submission_ref=plan.submission_ref,
                    payload_hash=plan.payload_hash,
                    receipt=AcceptanceReceipt(
                        issuer="agent_runtime",
                        kind=PLAN_ATTEMPT_EXECUTION_RECEIPT_KIND,
                        receipt_ref=plan.execution_receipt_ref,
                        subject_ref=plan.submission_ref,
                        payload_hash=plan.execution_receipt_hash,
                    ),
                )
        else:
            raise OwnerConflict("asset_receipt_kind_invalid")

    def verify_asset_binding(self, **values) -> None:
        self.verify_asset_receipt(**values)
        version_ref = values.get("version_ref")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rm_asset_versions WHERE version_ref = "
                    ":version_ref"
                ),
                {"version_ref": version_ref},
            ).first()
            custodies = connection.execute(
                text(
                    "SELECT * FROM rm_asset_custodies WHERE version_ref = "
                    ":version_ref ORDER BY custody_mode"
                ),
                {"version_ref": version_ref},
            ).all()
        if row is None:
            raise OwnerConflict("asset_receipt_invalid")
        integrity, availability = _asset_current_state(
            self._object_store, row, custodies
        )
        if integrity != "verified" or availability != "available":
            raise OwnerConflict("asset_custody_unavailable")

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
    ) -> None:
        verify = self.verify_asset_binding if require_current else self.verify_asset_receipt
        verify(
            asset_ref=asset_ref,
            version_ref=version_ref,
            content_hash=content_hash,
            manifest_hash=manifest_hash,
            receipt=receipt,
        )
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rm_asset_versions WHERE version_ref = "
                    ":version_ref"
                ),
                {"version_ref": version_ref},
            ).first()
        if row is None:
            raise OwnerConflict("plan_evidence_binding_invalid")
        target, closure = _plan_evidence_provenance(row)
        if (
            row.asset_ref != asset_ref
            or row.content_hash != content_hash
            or row.manifest_hash != manifest_hash
            or target != target_commit_root_ref
            or closure != provenance_closure_refs
        ):
            raise OwnerConflict("plan_evidence_binding_invalid")


class SQLiteResearchMemory:
    def __init__(
        self,
        database: Database,
        object_store: Path,
        feed: DurableFeed,
        confirmation_verifier: BundleConfirmationVerifier,
        quest_verifier: QuestReceiptVerifier,
        receipt_verifier: SQLiteResearchMemoryReceiptVerifier,
        execution_verifier: AttemptExecutionReceiptVerifier | None = None,
        reference_reader: AssetReferenceReader | None = None,
        stage_request_verifier: StageRunRequestVerifier | None = None,
    ) -> None:
        self._database = database
        self._object_store = object_store
        self._feed = feed
        self._confirmation_verifier = confirmation_verifier
        self._quest_verifier = quest_verifier
        self._receipt_verifier = receipt_verifier
        self._execution_verifier = execution_verifier
        self._reference_reader = reference_reader
        self._stage_request_verifier = stage_request_verifier
        self._snapshot = SQLiteOwnerSnapshot(database, _SNAPSHOT)
        # Handoff can perform durable, crash-recoverable object repair. Keep a
        # single in-process performer so timeout followers replay or alias the
        # finished command instead of duplicating object I/O and racing the
        # partial-unique repair intent. A restarted daemon safely adopts the
        # durable processing row with a fresh lock.
        self._asset_handoff_lock = threading.RLock()
        self._recover_asset_intakes()

    def _recover_asset_intakes(self) -> None:
        now = time.time()
        with self._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE rm_asset_intakes SET status = 'queued', started_at = "
                    "NULL, next_attempt_at = :now, updated_at = :now WHERE "
                    "status = 'processing'"
                ),
                {"now": now},
            )

    def submit_asset_intake(
        self, request: AssetIntakeRequest, *, idempotency_key: str
    ) -> AssetIntakeResult:
        if not idempotency_key or len(idempotency_key) > 128:
            raise OwnerConflict("asset_intake_idempotency_key_invalid")
        request_document = _asset_request_document(request)
        request_json = canonical_json(request_document)
        request_hash = canonical_hash(request_document)
        requested_asset_ref = request_document.get("asset_ref")
        if requested_asset_ref is not None:
            with self._database.read() as connection:
                asset_exists = connection.execute(
                    text(
                        "SELECT 1 FROM rm_assets WHERE asset_ref = :asset_ref"
                    ),
                    {"asset_ref": requested_asset_ref},
                ).first()
            if asset_exists is None:
                raise OwnerConflict("asset_not_found")
        sync_claimed = False
        with self._database.write() as connection:
            existing = connection.execute(
                text(
                    "SELECT * FROM rm_asset_intakes WHERE idempotency_key = "
                    ":idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
            if existing is not None:
                if bool(existing.request_payload_scrubbed):
                    _verified_scrubbed_asset_request_summary(existing)
                else:
                    _verify_stored_asset_request_binding(
                        existing.request_json, existing.request_hash
                    )
                if existing.request_hash != request_hash:
                    raise OwnerConflict("asset_intake_idempotency_conflict")
                job_ref = existing.job_ref
                if (
                    not request.asynchronous
                    and existing.status == "queued"
                    and float(existing.next_attempt_at) <= time.time()
                ):
                    now = time.time()
                    connection.execute(
                        text(
                            "UPDATE rm_asset_intakes SET status = 'processing', "
                            "attempt_count = attempt_count + 1, started_at = :now, "
                            "next_attempt_at = :now, updated_at = :now WHERE "
                            "job_ref = :job_ref AND status = 'queued' AND "
                            "next_attempt_at <= :now"
                        ),
                        {"job_ref": job_ref, "now": now},
                    )
                    sync_claimed = True
            else:
                pending = connection.execute(
                    text(
                        "SELECT COUNT(*) AS job_count, "
                        "COALESCE(SUM(length(request_json)), 0) AS request_bytes "
                        "FROM rm_asset_intakes WHERE status IN "
                        "('queued', 'processing')"
                    )
                ).first()
                if pending is None or (
                    int(pending.job_count) >= MAX_PENDING_ASSET_INTAKES
                    or int(pending.request_bytes) + len(request_json.encode("utf-8"))
                    > MAX_PENDING_ASSET_REQUEST_BYTES
                ):
                    raise OwnerConflict("asset_intake_queue_full")
                job_ref = new_ref("asset_intake")
                now = time.time()
                initial_status = (
                    "queued" if request.asynchronous else "processing"
                )
                connection.execute(
                    text(
                        "INSERT INTO rm_asset_intakes (job_ref, idempotency_key, "
                        "request_json, request_hash, request_source_kind, "
                        "request_custody_mode, request_payload_scrubbed, status, "
                        "attempt_count, started_at, next_attempt_at, created_at, "
                        "updated_at) "
                        "VALUES (:job_ref, :idempotency_key, :request_json, "
                        ":request_hash, :request_source_kind, "
                        ":request_custody_mode, 0, :status, :attempt_count, "
                        ":started_at, :now, :now, :now)"
                    ),
                    {
                        "job_ref": job_ref,
                        "idempotency_key": idempotency_key,
                        "request_json": request_json,
                        "request_hash": request_hash,
                        "request_source_kind": request_document["source_kind"],
                        "request_custody_mode": request_document["custody_mode"],
                        "status": initial_status,
                        "attempt_count": 0 if request.asynchronous else 1,
                        "started_at": None if request.asynchronous else now,
                        "now": now,
                    },
                )
                sync_claimed = not request.asynchronous
                connection.execute(
                    text(
                        "UPDATE research_memory_state SET revision = revision + 1, "
                        "pending_intake_count = pending_intake_count + 1 "
                        "WHERE singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "research_memory.asset_intake_queued",
                    {
                        "job_ref": job_ref,
                        "source_kind": request_document["source_kind"],
                        "custody_mode": request_document["custody_mode"],
                    },
                )
        if sync_claimed:
            self._process_asset_job(job_ref, already_claimed=True)
        return self.query_asset_intake(job_ref)

    def process_asset_intake_once(self) -> bool:
        now = time.time()
        with self._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE rm_asset_intakes SET status = 'queued', started_at = "
                    "NULL, next_attempt_at = :now, updated_at = :now WHERE "
                    "status = 'processing' AND started_at IS NOT NULL AND "
                    "started_at <= :expired_before"
                ),
                {
                    "now": now,
                    "expired_before": now - ASSET_INTAKE_LEASE_SECONDS,
                },
            )
            row = connection.execute(
                text(
                    "SELECT job_ref FROM rm_asset_intakes WHERE status = 'queued' "
                    "AND next_attempt_at <= :now ORDER BY next_attempt_at, "
                    "updated_at, created_at, job_ref LIMIT 1"
                ),
                {"now": now},
            ).first()
        if row is None:
            return False
        self._process_asset_job(row.job_ref)
        return True

    def verify_asset_inventory_once(self) -> bool:
        """Deep-verify one due version outside the public Snapshot hot path."""

        now = time.time()
        with self._database.write() as connection:
            due = connection.execute(
                text(
                    "SELECT version_ref FROM "
                    "rm_asset_verification_observations WHERE next_verify_at <= "
                    ":now ORDER BY next_verify_at, version_ref LIMIT 1"
                ),
                {"now": now},
            ).first()
            if due is None:
                return False
            connection.execute(
                text(
                    "UPDATE rm_asset_verification_observations SET "
                    "next_verify_at = :claimed_until WHERE version_ref = "
                    ":version_ref"
                ),
                {
                    "claimed_until": now + 60.0,
                    "version_ref": due.version_ref,
                },
            )
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rm_asset_versions WHERE version_ref = "
                    ":version_ref"
                ),
                {"version_ref": due.version_ref},
            ).first()
            custodies = connection.execute(
                text(
                    "SELECT * FROM rm_asset_custodies WHERE version_ref = "
                    ":version_ref ORDER BY custody_mode, custody_ref"
                ),
                {"version_ref": due.version_ref},
            ).all()
        if row is None:
            return True
        basis_hash = _asset_observation_basis_hash(row, custodies)
        try:
            self._verified_accepted_asset(row, custodies)
            if int(row.byte_count) > MAX_ASSET_BYTES:
                # 0005 allowed larger payloads. Preserve their durable facts,
                # but do not let a legacy GiB-scale object monopolize the
                # bounded background verifier or the intake worker.
                integrity, availability = "unknown", "unavailable"
            else:
                integrity, availability = _asset_current_state(
                    self._object_store, row, custodies
                )
        except (OSError, OwnerConflict):
            integrity, availability = "failed", "unavailable"
        completed_at = time.time()
        with self._database.write() as connection:
            current_row = connection.execute(
                text(
                    "SELECT * FROM rm_asset_versions WHERE version_ref = "
                    ":version_ref"
                ),
                {"version_ref": due.version_ref},
            ).first()
            current_custodies = connection.execute(
                text(
                    "SELECT * FROM rm_asset_custodies WHERE version_ref = "
                    ":version_ref ORDER BY custody_mode, custody_ref"
                ),
                {"version_ref": due.version_ref},
            ).all()
            if current_row is None:
                return True
            if (
                _asset_observation_basis_hash(current_row, current_custodies)
                != basis_hash
            ):
                connection.execute(
                    text(
                        "UPDATE rm_asset_verification_observations SET "
                        "next_verify_at = 0 WHERE version_ref = :version_ref"
                    ),
                    {"version_ref": due.version_ref},
                )
                return True
            observation = connection.execute(
                text(
                    "SELECT integrity, availability, observed_at FROM "
                    "rm_asset_verification_observations WHERE version_ref = "
                    ":version_ref"
                ),
                {"version_ref": due.version_ref},
            ).first()
            changed = observation is None or (
                observation.integrity != integrity
                or observation.availability != availability
            )
            connection.execute(
                text(
                    "UPDATE rm_asset_verification_observations SET integrity = "
                    ":integrity, availability = :availability, observed_at = "
                    ":observed_at, next_verify_at = :next_verify_at WHERE "
                    "version_ref = :version_ref"
                ),
                {
                    "version_ref": due.version_ref,
                    "integrity": integrity,
                    "availability": availability,
                    "observed_at": (
                        completed_at
                        if changed or observation is None
                        else float(observation.observed_at)
                    ),
                    "next_verify_at": (
                        completed_at + ASSET_VERIFICATION_INTERVAL_SECONDS
                    ),
                },
            )
            if changed:
                connection.execute(
                    text(
                        "UPDATE research_memory_state SET revision = revision + "
                        "1 WHERE singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "research_memory.asset_verification_observed",
                    {
                        "version_ref": due.version_ref,
                        "integrity": integrity,
                        "availability": availability,
                    },
                )
        return True

    def _process_asset_job(
        self, job_ref: str, *, already_claimed: bool = False
    ) -> None:
        with self._database.write() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rm_asset_intakes WHERE job_ref = :job_ref"
                ),
                {"job_ref": job_ref},
            ).first()
            if row is None:
                raise OwnerConflict("asset_intake_not_found")
            if row.status in {"accepted", "failed"}:
                return
            if already_claimed:
                if row.status != "processing":
                    return
            else:
                now = time.time()
                if (
                    row.status != "queued"
                    or float(row.next_attempt_at) > now
                ):
                    return
                connection.execute(
                    text(
                        "UPDATE rm_asset_intakes SET status = 'processing', "
                        "attempt_count = attempt_count + 1, started_at = :now, "
                        "next_attempt_at = :now, updated_at = :now WHERE job_ref "
                        "= :job_ref AND status = 'queued' AND next_attempt_at "
                        "<= :now"
                    ),
                    {"job_ref": job_ref, "now": now},
                )
            stored_request_json = row.request_json
            stored_request_hash = row.request_hash
            stored_request_scrubbed = bool(row.request_payload_scrubbed)
        try:
            if stored_request_scrubbed:
                raise OwnerConflict("asset_intake_request_invalid")
            request_document = _validated_stored_asset_request(
                stored_request_json, stored_request_hash
            )
            prepared = self._prepare_asset(request_document)
            self._accept_prepared_asset(job_ref, request_document, prepared)
        except OwnerConflict as error:
            if error.code in TRANSIENT_ASSET_INTAKE_CONFLICTS:
                self._requeue_asset_intake(job_ref)
            else:
                self._fail_asset_intake(job_ref, error.code)
        except ValueError:
            self._fail_asset_intake(job_ref, "asset_intake_io_error")
        except Exception:
            self._requeue_asset_intake(job_ref)
            raise

    def _requeue_asset_intake(self, job_ref: str) -> None:
        """Return a claimed job to the durable queue after transient failure."""

        with self._database.write() as connection:
            row = connection.execute(
                text(
                    "SELECT attempt_count, request_source_kind, "
                    "request_custody_mode FROM rm_asset_intakes WHERE job_ref = "
                    ":job_ref AND status = 'processing'"
                ),
                {"job_ref": job_ref},
            ).first()
            if row is None:
                return
            now = time.time()
            if int(row.attempt_count) >= ASSET_INTAKE_MAX_ATTEMPTS:
                request_summary_json = _scrubbed_asset_request_json(
                    row.request_source_kind, row.request_custody_mode
                )
                connection.execute(
                    text(
                        "UPDATE rm_asset_intakes SET status = 'failed', "
                        "failure_code = 'asset_intake_retry_exhausted', "
                        "request_json = :request_summary_json, "
                        "request_payload_scrubbed = 1, completed_at = :now, "
                        "updated_at = :now WHERE job_ref = :job_ref AND "
                        "status = 'processing'"
                    ),
                    {
                        "job_ref": job_ref,
                        "now": now,
                        "request_summary_json": request_summary_json,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE research_memory_state SET revision = revision + "
                        "1, pending_intake_count = pending_intake_count - 1 "
                        "WHERE singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "research_memory.asset_intake_failed",
                    {
                        "job_ref": job_ref,
                        "failure_code": "asset_intake_retry_exhausted",
                    },
                )
            else:
                retry_delay = ASSET_INTAKE_RETRY_BASE_SECONDS * (
                    2 ** max(0, int(row.attempt_count) - 1)
                )
                connection.execute(
                    text(
                        "UPDATE rm_asset_intakes SET status = 'queued', "
                        "started_at = NULL, next_attempt_at = :next_attempt_at, "
                        "updated_at = :now WHERE job_ref = :job_ref AND status "
                        "= 'processing'"
                    ),
                    {
                        "job_ref": job_ref,
                        "now": now,
                        "next_attempt_at": now + retry_delay,
                    },
                )

    def _prepare_asset(self, request: dict[str, object]) -> _PreparedAsset:
        source_kind = request.get("source_kind")
        custody_mode = request.get("custody_mode")
        if source_kind not in {
            "text",
            "file",
            "directory",
            "local_path",
            "repository",
            "link",
            "system_artifact",
        }:
            raise OwnerConflict("asset_source_kind_not_supported")
        if custody_mode not in {"managed", "linked_local"}:
            raise OwnerConflict("asset_custody_mode_invalid")
        locator = request.get("source_locator")
        encoded = request.get("content_base64")
        if source_kind == "link":
            if custody_mode != "managed" or not isinstance(locator, str):
                raise OwnerConflict("asset_link_invalid")
            parsed = urlsplit(locator)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise OwnerConflict("asset_link_invalid")
            content = locator.encode("utf-8")
            content_hash = hashlib.sha256(content).hexdigest()
            object_path = self._store_asset_object(content_hash, content)
            manifest = {
                "schema_ref": ASSET_MANIFEST_SCHEMA,
                "kind": "file",
                "entries": [
                    {
                        "path": _safe_asset_name(str(request["display_name"])),
                        "sha256": content_hash,
                        "size": len(content),
                        "object_path": object_path,
                    }
                ],
            }
            return _PreparedAsset(
                manifest=manifest,
                content_hash=content_hash,
                byte_count=len(content),
            )
        if isinstance(locator, str):
            source = Path(locator)
            if not source.exists() or source.is_symlink():
                raise OwnerConflict("asset_source_unavailable")
            directory_source = source.is_dir()
            if source_kind in {"directory", "repository"} and not directory_source:
                raise OwnerConflict("asset_source_unavailable")
            if source_kind == "file" and directory_source:
                raise OwnerConflict("asset_source_unavailable")
            if not directory_source and not source.is_file():
                raise OwnerConflict("asset_source_entry_unsupported")
            if directory_source:
                source_directories, source_files = _read_directory_files(
                    source,
                    ignored_top_level=(".git",) if source_kind == "repository" else (),
                )
            else:
                source_directories = ()
                source_files = (
                    (
                        _safe_asset_name(str(request["display_name"])),
                        _read_bounded_source_file(source),
                    ),
                )
            entries: list[dict[str, object]] = []
            byte_count = 0
            for relative_path, content in source_files:
                content_hash = hashlib.sha256(content).hexdigest()
                object_path: str | None = None
                if custody_mode == "managed":
                    object_path = self._store_asset_object(
                        content_hash, content
                    )
                entries.append(
                    {
                        "path": relative_path,
                        "sha256": content_hash,
                        "size": len(content),
                        "object_path": object_path,
                    }
                )
                byte_count += len(content)
            manifest = {
                "schema_ref": ASSET_MANIFEST_SCHEMA,
                "kind": "directory" if directory_source else "file",
                "entries": entries,
            }
            if directory_source:
                manifest["directories"] = list(source_directories)
            if source_kind == "repository":
                manifest["ignored_top_level"] = [".git"]
            content_manifest = {
                "kind": manifest["kind"],
                "directories": list(source_directories),
                "entries": [
                    {
                        "path": entry["path"],
                        "sha256": entry["sha256"],
                        "size": entry["size"],
                    }
                    for entry in entries
                ],
            }
            return _PreparedAsset(
                manifest=manifest,
                content_hash=(
                    canonical_hash(content_manifest)
                    if directory_source
                    else str(entries[0]["sha256"])
                ),
                byte_count=byte_count,
            )
        if custody_mode != "managed":
            raise OwnerConflict("asset_source_locator_required")
        if not isinstance(encoded, str):
            raise OwnerConflict("asset_content_required")
        try:
            content = base64.b64decode(encoded, validate=True)
        except ValueError as error:
            raise OwnerConflict("asset_content_invalid") from error
        content_hash = hashlib.sha256(content).hexdigest()
        object_path = self._store_asset_object(content_hash, content)
        display_name = str(request["display_name"])
        manifest = {
            "schema_ref": ASSET_MANIFEST_SCHEMA,
            "kind": "file",
            "entries": [
                {
                    "path": _safe_asset_name(display_name),
                    "sha256": content_hash,
                    "size": len(content),
                    "object_path": object_path,
                }
            ],
        }
        return _PreparedAsset(
            manifest=manifest,
            content_hash=content_hash,
            byte_count=len(content),
        )

    def _accept_prepared_asset(
        self,
        job_ref: str,
        request: dict[str, object],
        prepared: _PreparedAsset,
    ) -> None:
        if request.get("custody_mode") == "managed":
            _verify_managed_manifest(self._object_store, prepared.manifest)
        else:
            source_locator = request.get("source_locator")
            if not isinstance(source_locator, str):
                raise OwnerConflict("asset_source_unavailable")
            try:
                source_matches = _linked_source_matches(
                    prepared.manifest, Path(source_locator)
                )
            except (OSError, OwnerConflict) as error:
                raise OwnerConflict("asset_source_changed_during_intake") from error
            if not source_matches:
                raise OwnerConflict("asset_source_changed_during_intake")
        verification_observed_at = time.time()
        manifest_json = canonical_json(prepared.manifest)
        manifest_hash = canonical_hash(prepared.manifest)
        provenance = request.get("provenance")
        if not isinstance(provenance, dict):
            provenance = {}
        provenance = {
            **provenance,
            "source_kind": request["source_kind"],
        }
        provenance_json = canonical_json(provenance)
        provenance_hash = canonical_hash(provenance)
        with self._database.write() as connection:
            job = connection.execute(
                text(
                    "SELECT * FROM rm_asset_intakes WHERE job_ref = :job_ref"
                ),
                {"job_ref": job_ref},
            ).first()
            if job is None:
                raise OwnerConflict("asset_intake_not_found")
            if job.status == "accepted":
                return
            if job.status != "processing":
                raise OwnerConflict("asset_intake_not_claimed")
            requested_asset_ref = request.get("asset_ref")
            if requested_asset_ref is None:
                asset_ref = new_ref("asset")
                version_number = 1
                connection.execute(
                    text(
                        "INSERT INTO rm_assets (asset_ref, created_at) VALUES "
                        "(:asset_ref, :created_at)"
                    ),
                    {"asset_ref": asset_ref, "created_at": time.time()},
                )
                asset_delta = 1
            else:
                asset_ref = str(requested_asset_ref)
                asset = connection.execute(
                    text(
                        "SELECT asset_ref FROM rm_assets WHERE asset_ref = :asset_ref"
                    ),
                    {"asset_ref": asset_ref},
                ).first()
                if asset is None:
                    raise OwnerConflict("asset_not_found")
                version_number = int(
                    connection.execute(
                        text(
                            "SELECT COALESCE(MAX(version_number), 0) + 1 FROM "
                            "rm_asset_versions WHERE asset_ref = :asset_ref"
                        ),
                        {"asset_ref": asset_ref},
                    ).scalar_one()
                )
                asset_delta = 0
            version_ref = new_ref("asset_version")
            custody_ref = new_ref("asset_custody")
            receipt_ref = new_ref("rm_asset_receipt")
            bindings = {
                "asset_ref": asset_ref,
                "version_number": version_number,
                "source_kind": request["source_kind"],
                "display_name": request["display_name"],
                "media_type": request["media_type"],
                "content_hash": prepared.content_hash,
                "manifest_hash": manifest_hash,
                "byte_count": prepared.byte_count,
                "provenance_hash": provenance_hash,
                "custody_modes": [request["custody_mode"]],
            }
            receipt_hash = _receipt_hash(
                ASSET_RECEIPT_KIND, version_ref, bindings
            )
            now = time.time()
            custody_receipt_ref = new_ref("rm_asset_custody_receipt")
            custody_receipt_hash = _receipt_hash(
                ASSET_CUSTODY_ESTABLISHED_RECEIPT_KIND,
                custody_ref,
                {
                    "version_ref": version_ref,
                    "content_hash": prepared.content_hash,
                    "manifest_hash": manifest_hash,
                    "custody_mode": request["custody_mode"],
                    "source_locator": request.get("source_locator"),
                },
            )
            connection.execute(
                text(
                    "INSERT INTO rm_asset_versions (version_ref, asset_ref, "
                    "version_number, source_kind, display_name, media_type, "
                    "content_hash, manifest_json, manifest_hash, byte_count, "
                    "provenance_json, provenance_hash, acceptance_kind, receipt_ref, "
                    "receipt_hash, accepted_at) VALUES (:version_ref, :asset_ref, "
                    ":version_number, :source_kind, :display_name, :media_type, "
                    ":content_hash, :manifest_json, :manifest_hash, :byte_count, "
                    ":provenance_json, :provenance_hash, :acceptance_kind, "
                    ":receipt_ref, :receipt_hash, :accepted_at)"
                ),
                {
                    **bindings,
                    "version_ref": version_ref,
                    "manifest_json": manifest_json,
                    "provenance_json": provenance_json,
                    "acceptance_kind": ASSET_RECEIPT_KIND,
                    "receipt_ref": receipt_ref,
                    "receipt_hash": receipt_hash,
                    "accepted_at": now,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO rm_asset_custodies (custody_ref, version_ref, "
                    "custody_mode, source_locator, receipt_kind, receipt_ref, "
                    "receipt_hash, established_at) VALUES (:custody_ref, "
                    ":version_ref, :custody_mode, :source_locator, :receipt_kind, "
                    ":receipt_ref, :receipt_hash, :established_at)"
                ),
                {
                    "custody_ref": custody_ref,
                    "version_ref": version_ref,
                    "custody_mode": request["custody_mode"],
                    "source_locator": request.get("source_locator"),
                    "receipt_kind": ASSET_CUSTODY_ESTABLISHED_RECEIPT_KIND,
                    "receipt_ref": custody_receipt_ref,
                    "receipt_hash": custody_receipt_hash,
                    "established_at": now,
                },
            )
            connection.execute(
                text(
                    "UPDATE rm_asset_verification_observations SET integrity = "
                    "'verified', availability = 'available', observed_at = "
                    ":observed_at, next_verify_at = :next_verify_at WHERE "
                    "version_ref = :version_ref"
                ),
                {
                    "version_ref": version_ref,
                    "observed_at": verification_observed_at,
                    "next_verify_at": (
                        verification_observed_at
                        + ASSET_VERIFICATION_INTERVAL_SECONDS
                    ),
                },
            )
            if request["custody_mode"] == "managed":
                _register_managed_manifest(
                    connection, prepared.manifest, registered_at=now
                )
            connection.execute(
                text(
                    "UPDATE rm_asset_intakes SET status = 'accepted', "
                    "asset_ref = :asset_ref, version_ref = :version_ref, "
                    "failure_code = NULL, request_json = :request_summary_json, "
                    "request_payload_scrubbed = 1, completed_at = :now, "
                    "updated_at = :now WHERE job_ref = :job_ref"
                ),
                {
                    "job_ref": job_ref,
                    "asset_ref": asset_ref,
                    "version_ref": version_ref,
                    "request_summary_json": _scrubbed_asset_request_json(
                        str(request["source_kind"]),
                        str(request["custody_mode"]),
                    ),
                    "now": now,
                },
            )
            connection.execute(
                text(
                    "UPDATE research_memory_state SET revision = revision + 1, "
                    "asset_count = asset_count + :asset_delta, "
                    "asset_version_count = asset_version_count + 1, "
                    "object_count = :object_count, "
                    "pending_intake_count = pending_intake_count - 1 "
                    "WHERE singleton = 'owner'"
                ),
                {
                    "asset_delta": asset_delta,
                    "object_count": _managed_object_count(connection),
                },
            )
            self._feed.record(
                connection,
                "research_memory.asset_accepted",
                {
                    "job_ref": job_ref,
                    "asset_ref": asset_ref,
                    "version_ref": version_ref,
                    "content_hash": prepared.content_hash,
                    "manifest_hash": manifest_hash,
                    "receipt_ref": receipt_ref,
                },
            )

    def _fail_asset_intake(self, job_ref: str, failure_code: str) -> None:
        with self._database.write() as connection:
            row = connection.execute(
                text(
                    "SELECT status, request_source_kind, request_custody_mode "
                    "FROM rm_asset_intakes WHERE job_ref = :job_ref"
                ),
                {"job_ref": job_ref},
            ).first()
            if row is None or row.status in {"accepted", "failed"}:
                return
            now = time.time()
            request_summary_json = _scrubbed_asset_request_json(
                row.request_source_kind, row.request_custody_mode
            )
            connection.execute(
                text(
                    "UPDATE rm_asset_intakes SET status = 'failed', "
                    "failure_code = :failure_code, completed_at = :now, "
                    "request_json = :request_summary_json, "
                    "request_payload_scrubbed = 1, updated_at = :now WHERE "
                    "job_ref = :job_ref"
                ),
                {
                    "job_ref": job_ref,
                    "failure_code": failure_code,
                    "now": now,
                    "request_summary_json": request_summary_json,
                },
            )
            connection.execute(
                text(
                    "UPDATE research_memory_state SET revision = revision + 1, "
                    "pending_intake_count = pending_intake_count - 1 "
                    "WHERE singleton = 'owner'"
                )
            )
            self._feed.record(
                connection,
                "research_memory.asset_intake_failed",
                {"job_ref": job_ref, "reason": {"code": failure_code}},
            )

    def query_asset_intake(self, job_ref: str) -> AssetIntakeResult:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rm_asset_intakes WHERE job_ref = :job_ref"
                ),
                {"job_ref": job_ref},
            ).first()
        if row is None:
            raise OwnerConflict("asset_intake_not_found")
        if bool(row.request_payload_scrubbed):
            request = _verified_scrubbed_asset_request_summary(row)
        else:
            try:
                _verify_stored_asset_request_binding(
                    row.request_json, row.request_hash
                )
                request = decoded_object(row.request_json)
            except (TypeError, ValueError, OwnerConflict):
                if not (
                    row.status == "failed"
                    and row.failure_code == "asset_intake_request_invalid"
                ):
                    raise OwnerConflict("asset_intake_request_invalid")
                request = {
                    "source_kind": row.request_source_kind or "unknown",
                    "custody_mode": row.request_custody_mode or "unknown",
                }
        asset = (
            None
            if row.version_ref is None
            else self.query_asset_version(row.version_ref)
        )
        return AssetIntakeResult(
            job_ref=row.job_ref,
            status=row.status,
            source_kind=str(request.get("source_kind", "unknown")),
            custody_mode=str(request.get("custody_mode", "unknown")),
            attempt_count=int(row.attempt_count),
            asset=asset,
            failure_code=row.failure_code,
        )

    def query_asset_intake_by_idempotency_key(
        self, idempotency_key: str, request: AssetIntakeRequest
    ) -> AssetIntakeResult | None:
        request_hash = canonical_hash(_asset_request_document(request))
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT job_ref, request_hash FROM rm_asset_intakes WHERE "
                    "idempotency_key = :idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
        if row is None:
            return None
        if row.request_hash != request_hash:
            raise OwnerConflict("asset_intake_idempotency_conflict")
        return self.query_asset_intake(row.job_ref)

    def query_asset_version(self, memory_ref: str) -> AcceptedAssetVersion | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rm_asset_versions WHERE version_ref = :memory_ref"
                ),
                {"memory_ref": memory_ref},
            ).first()
            custodies = connection.execute(
                text(
                    "SELECT * FROM rm_asset_custodies WHERE version_ref = "
                    ":memory_ref ORDER BY custody_mode"
                ),
                {"memory_ref": memory_ref},
            ).all()
        if row is None:
            return None
        return self._verified_accepted_asset(row, custodies)

    def query_asset_inventory(self) -> tuple[AssetInventoryItem, ...]:
        with self._database.read() as connection:
            rows = connection.execute(
                text(
                    "SELECT * FROM rm_asset_versions ORDER BY accepted_at DESC, "
                    "version_ref DESC"
                )
            ).all()
            custodies = connection.execute(
                text(
                    "SELECT * FROM rm_asset_custodies ORDER BY version_ref, "
                    "custody_mode"
                )
            ).all()
        by_version: dict[str, list[object]] = {}
        for custody in custodies:
            by_version.setdefault(custody.version_ref, []).append(custody)
        inventory: list[AssetInventoryItem] = []
        for row in rows:
            version_custodies = by_version.get(row.version_ref, [])
            observed_at = time.time()
            integrity, availability = _asset_current_state(
                self._object_store, row, version_custodies
            )
            inventory.append(
                self._asset_inventory_item(
                    row,
                    version_custodies,
                    integrity,
                    availability,
                    verification_observed_at=observed_at,
                    verification_pending=False,
                )
            )
        return tuple(inventory)

    def query_asset_projection_inventory(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[AssetInventoryItem, ...]:
        """Project last durable verification without reading corpus bytes."""

        if offset < 0 or (
            limit is not None
            and (limit < 1 or limit > ASSET_PROJECTION_MAX_PAGE_SIZE)
        ):
            raise OwnerConflict("asset_projection_page_invalid")
        page_clause = ""
        page_parameters: dict[str, object] = {}
        if limit is not None:
            page_clause = " LIMIT :limit OFFSET :offset"
            page_parameters = {"limit": limit, "offset": offset}
        with self._database.read() as connection:
            rows = connection.execute(
                text(
                    "SELECT versions.*, observations.integrity AS "
                    "observed_integrity, observations.availability AS "
                    "observed_availability, observations.observed_at AS "
                    "verification_observed_at, observations.next_verify_at AS "
                    "next_verify_at FROM rm_asset_versions AS versions "
                    "LEFT JOIN rm_asset_verification_observations AS observations "
                    "ON observations.version_ref = versions.version_ref ORDER BY "
                    "versions.accepted_at DESC, versions.version_ref DESC"
                    + page_clause
                ),
                page_parameters,
            ).all()
            version_refs = tuple(row.version_ref for row in rows)
            if version_refs:
                condition, parameters = _version_ref_condition(
                    "version_ref", version_refs
                )
                custodies = connection.execute(
                    text(
                        "SELECT * FROM rm_asset_custodies WHERE "
                        + condition
                        + " ORDER BY version_ref, custody_mode"
                    ),
                    parameters,
                ).all()
            else:
                custodies = []
        by_version: dict[str, list[object]] = {}
        for custody in custodies:
            by_version.setdefault(custody.version_ref, []).append(custody)
        inventory: list[AssetInventoryItem] = []
        for row in rows:
            version_custodies = by_version.get(row.version_ref, [])
            integrity = row.observed_integrity or "unknown"
            availability = row.observed_availability or "unknown"
            inventory.append(
                self._asset_inventory_item(
                    row,
                    version_custodies,
                    integrity,
                    availability,
                    verification_observed_at=row.verification_observed_at,
                    verification_pending=(
                        integrity == "unknown"
                        or availability == "unknown"
                    ),
                    projection=True,
                )
            )
        return tuple(inventory)

    def query_asset_inventory_item(
        self, memory_ref: str
    ) -> AssetInventoryItem | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rm_asset_versions WHERE version_ref = "
                    ":version_ref"
                ),
                {"version_ref": memory_ref},
            ).first()
            custodies = connection.execute(
                text(
                    "SELECT * FROM rm_asset_custodies WHERE version_ref = "
                    ":version_ref ORDER BY custody_mode"
                ),
                {"version_ref": memory_ref},
            ).all()
        if row is None:
            return None
        integrity, availability = _asset_current_state(
            self._object_store, row, custodies
        )
        return self._asset_inventory_item(
            row,
            custodies,
            integrity,
            availability,
            verification_observed_at=time.time(),
            verification_pending=False,
        )

    def query_asset_projection_inventory_item(
        self, memory_ref: str
    ) -> AssetInventoryItem | None:
        """Read one version from durable metadata and verification state only."""

        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT versions.*, observations.integrity AS "
                    "observed_integrity, observations.availability AS "
                    "observed_availability, observations.observed_at AS "
                    "verification_observed_at FROM rm_asset_versions AS "
                    "versions LEFT JOIN rm_asset_verification_observations AS "
                    "observations ON observations.version_ref = "
                    "versions.version_ref WHERE versions.version_ref = "
                    ":version_ref"
                ),
                {"version_ref": memory_ref},
            ).first()
            custodies = connection.execute(
                text(
                    "SELECT * FROM rm_asset_custodies WHERE version_ref = "
                    ":version_ref ORDER BY custody_mode"
                ),
                {"version_ref": memory_ref},
            ).all()
        if row is None:
            return None
        integrity = row.observed_integrity or "unknown"
        availability = row.observed_availability or "unknown"
        return self._asset_inventory_item(
            row,
            custodies,
            integrity,
            availability,
            verification_observed_at=row.verification_observed_at,
            verification_pending=(
                integrity == "unknown" or availability == "unknown"
            ),
            projection=True,
        )

    def _asset_inventory_item(
        self,
        row,
        custodies,
        integrity: str,
        availability: str,
        *,
        verification_observed_at: float | None,
        verification_pending: bool,
        projection: bool = False,
    ) -> AssetInventoryItem:
        accepted = (
            self._verified_projection_asset(row, custodies)
            if projection
            else self._verified_accepted_asset(row, custodies)
        )
        return AssetInventoryItem(
            asset_ref=accepted.asset_ref,
            version_ref=accepted.version_ref,
            memory_ref=accepted.memory_ref,
            version_number=accepted.version_number,
            source_kind=accepted.source_kind,
            display_name=accepted.display_name,
            media_type=accepted.media_type,
            content_hash=accepted.content_hash,
            manifest_hash=accepted.manifest_hash,
            byte_count=accepted.byte_count,
            provenance=accepted.provenance,
            custody_modes=accepted.custody_modes,
            integrity=integrity,
            availability=availability,
            verification_observed_at=verification_observed_at,
            verification_pending=verification_pending,
            accepted_at=accepted.accepted_at,
            receipt=accepted.receipt,
        )

    def _verified_accepted_asset(
        self, row, custodies
    ) -> AcceptedAssetVersion:
        accepted = _accepted_asset(row, custodies)
        if row.acceptance_kind != ASSET_RECEIPT_KIND:
            self._receipt_verifier.verify_asset_receipt(
                asset_ref=accepted.asset_ref,
                version_ref=accepted.version_ref,
                content_hash=accepted.content_hash,
                manifest_hash=accepted.manifest_hash,
                receipt=accepted.receipt,
            )
        return accepted

    def _verified_projection_asset(
        self, row, custodies
    ) -> AcceptedAssetVersion:
        accepted = _accepted_asset(row, custodies, projection=True)
        if row.acceptance_kind != ASSET_RECEIPT_KIND:
            self._receipt_verifier.verify_asset_receipt(
                asset_ref=accepted.asset_ref,
                version_ref=accepted.version_ref,
                content_hash=accepted.content_hash,
                manifest_hash=accepted.manifest_hash,
                receipt=accepted.receipt,
            )
        return accepted

    def materialize_asset(self, memory_ref: str) -> MaterializedAsset:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rm_asset_versions WHERE version_ref = :memory_ref"
                ),
                {"memory_ref": memory_ref},
            ).first()
            custodies = connection.execute(
                text(
                    "SELECT * FROM rm_asset_custodies WHERE version_ref = "
                    ":memory_ref ORDER BY custody_mode"
                ),
                {"memory_ref": memory_ref},
            ).all()
        if row is None:
            raise OwnerConflict("asset_not_found")
        if int(row.byte_count) > MAX_ASSET_BYTES:
            raise OwnerConflict("asset_materialization_unsupported")
        self._verified_accepted_asset(row, custodies)
        try:
            _verify_asset_metadata(row, custodies, require_portable_paths=True)
        except OwnerConflict as error:
            raise OwnerConflict("asset_materialization_unsupported") from error
        integrity, availability = _asset_current_state(
            self._object_store, row, custodies
        )
        if integrity != "verified" or availability != "available":
            raise OwnerConflict("asset_custody_unavailable")
        manifest = decoded_object(row.manifest_json)
        entries = manifest.get("entries")
        if (
            not isinstance(entries, list)
            or not all(isinstance(entry, dict) for entry in entries)
        ):
            raise OwnerConflict("asset_materialization_unsupported")
        if manifest.get("kind") == "file" and len(entries) == 1:
            entry = entries[0]
            content = _materialized_entry_content(
                self._object_store, row, custodies, manifest, entry
            )
            file_name = str(entry["path"])
            media_type = row.media_type
        elif manifest.get("kind") == "directory":
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
                for directory_path in manifest.get("directories", []):
                    info = zipfile.ZipInfo(
                        filename=f"{directory_path}/",
                        date_time=(1980, 1, 1, 0, 0, 0),
                    )
                    info.create_system = 3
                    info.external_attr = (stat.S_IFDIR | 0o700) << 16
                    archive.writestr(info, b"")
                for entry in entries:
                    info = zipfile.ZipInfo(
                        filename=str(entry["path"]),
                        date_time=(1980, 1, 1, 0, 0, 0),
                    )
                    info.create_system = 3
                    info.external_attr = 0o600 << 16
                    archive.writestr(
                        info,
                        _materialized_entry_content(
                            self._object_store, row, custodies, manifest, entry
                        ),
                    )
            content = output.getvalue()
            file_name = (
                row.display_name
                if str(row.display_name).lower().endswith(".zip")
                else f"{row.display_name}.zip"
            )
            media_type = "application/zip"
        else:
            raise OwnerConflict("asset_materialization_unsupported")
        return MaterializedAsset(
            memory_ref=memory_ref,
            file_name=file_name,
            media_type=media_type,
            content=content,
        )

    def handoff_asset_to_managed(
        self, memory_ref: str, *, idempotency_key: str
    ) -> AcceptedAssetCustody:
        if not idempotency_key or len(idempotency_key) > 128:
            raise OwnerConflict("asset_custody_idempotency_key_invalid")
        with self._asset_handoff_lock:
            return self._handoff_asset_to_managed_serialized(
                memory_ref, idempotency_key=idempotency_key
            )

    def _handoff_asset_to_managed_serialized(
        self, memory_ref: str, *, idempotency_key: str
    ) -> AcceptedAssetCustody:
        request_hash = canonical_hash(
            {"version_ref": memory_ref, "target_custody": "managed"}
        )
        replay = self._query_custody_command(idempotency_key, request_hash)
        if replay is not None:
            return replay
        repair_status, repair_custody_ref, repair_replay = (
            self._query_asset_repair_command(idempotency_key, request_hash)
        )
        repair_command_key = idempotency_key
        if repair_replay is not None:
            return repair_replay
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rm_asset_versions WHERE version_ref = :version_ref"
                ),
                {"version_ref": memory_ref},
            ).first()
            custodies = connection.execute(
                text(
                    "SELECT * FROM rm_asset_custodies WHERE version_ref = "
                    ":version_ref ORDER BY custody_mode"
                ),
                {"version_ref": memory_ref},
            ).all()
        if row is None:
            raise OwnerConflict("asset_not_found")
        if int(row.byte_count) > MAX_ASSET_BYTES:
            raise OwnerConflict("asset_custody_unavailable")
        self._verified_accepted_asset(row, custodies)
        manifest = decoded_object(row.manifest_json)
        existing_managed = next(
            (
                custody
                for custody in custodies
                if custody.custody_mode == "managed"
            ),
            None,
        )
        if existing_managed is not None and repair_status == "missing":
            pending_repair = self._query_pending_asset_repair(
                existing_managed.custody_ref
            )
            if pending_repair is not None:
                if pending_repair.request_hash != request_hash:
                    raise OwnerConflict("asset_repair_command_invalid")
                repair_status = "processing"
                repair_custody_ref = pending_repair.custody_ref
                repair_command_key = pending_repair.idempotency_key
        if existing_managed is None:
            if repair_status != "missing":
                raise OwnerConflict("asset_repair_command_invalid")
            integrity, availability = _asset_current_state(
                self._object_store, row, custodies
            )
            if integrity != "verified" or availability != "available":
                raise OwnerConflict("asset_custody_unavailable")
            for entry in manifest["entries"]:
                content = _materialized_entry_content(
                    self._object_store, row, custodies, manifest, entry
                )
                self._store_asset_object(
                    str(entry["sha256"]), content
                )
            _verify_managed_manifest(self._object_store, manifest)
        else:
            if (
                repair_status == "processing"
                and repair_custody_ref != existing_managed.custody_ref
            ):
                raise OwnerConflict("asset_repair_command_invalid")
            try:
                _verify_managed_manifest(self._object_store, manifest)
            except OwnerConflict:
                linked_sources = _receipt_bound_asset_sources(row, custodies)
                if not any(
                    _linked_source_matches(manifest, source)
                    for source in linked_sources
                    if source.exists() and not source.is_symlink()
                ):
                    raise OwnerConflict("asset_custody_unavailable")
                if repair_status == "missing":
                    with self._database.write() as connection:
                        connection.execute(
                            text(
                                "INSERT INTO rm_asset_repair_commands "
                                "(idempotency_key, request_hash, custody_ref, "
                                "status, started_at, completed_at) VALUES "
                                "(:idempotency_key, :request_hash, :custody_ref, "
                                "'processing', :started_at, NULL)"
                            ),
                            {
                                "idempotency_key": idempotency_key,
                                "request_hash": request_hash,
                                "custody_ref": existing_managed.custody_ref,
                                "started_at": time.time(),
                            },
                        )
                    repair_status = "processing"
                    repair_custody_ref = existing_managed.custody_ref
                for entry in manifest["entries"]:
                    content = _materialized_entry_content(
                        self._object_store, row, custodies, manifest, entry
                    )
                    self._replace_asset_object(str(entry["sha256"]), content)
                _verify_managed_manifest(self._object_store, manifest)

        with self._database.write() as connection:
            if repair_status == "processing":
                completed_at = time.time()
                completed = connection.execute(
                    text(
                        "UPDATE rm_asset_repair_commands SET status = 'completed', "
                        "completed_at = :completed_at WHERE idempotency_key = "
                        ":idempotency_key AND request_hash = :request_hash AND "
                        "status = 'processing'"
                    ),
                    {
                        "completed_at": completed_at,
                        "idempotency_key": repair_command_key,
                        "request_hash": request_hash,
                    },
                )
                if completed.rowcount != 1:
                    raise OwnerConflict("asset_repair_command_invalid")
                connection.execute(
                    text(
                        "UPDATE research_memory_state SET revision = revision + "
                        "1 WHERE singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "research_memory.asset_managed_custody_repaired",
                    {
                        "version_ref": memory_ref,
                        "custody_ref": repair_custody_ref,
                        "idempotency_key": repair_command_key,
                    },
                )
            observation = connection.execute(
                text(
                    "SELECT integrity, availability, observed_at FROM "
                    "rm_asset_verification_observations WHERE version_ref = "
                    ":version_ref"
                ),
                {"version_ref": memory_ref},
            ).first()
            observation_changed = observation is None or (
                observation.integrity != "verified"
                or observation.availability != "available"
            )
            observation_time = time.time()
            connection.execute(
                text(
                    "UPDATE rm_asset_verification_observations SET integrity = "
                    "'verified', availability = 'available', observed_at = "
                    ":observed_at, "
                    "next_verify_at = :next_verify_at WHERE version_ref = "
                    ":version_ref"
                ),
                {
                    "version_ref": memory_ref,
                    "observed_at": (
                        observation_time
                        if observation_changed or observation is None
                        else float(observation.observed_at)
                    ),
                    "next_verify_at": (
                        observation_time + ASSET_VERIFICATION_INTERVAL_SECONDS
                    ),
                },
            )
            if observation_changed and repair_status != "processing":
                connection.execute(
                    text(
                        "UPDATE research_memory_state SET revision = revision + "
                        "1 WHERE singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "research_memory.asset_verification_observed",
                    {
                        "version_ref": memory_ref,
                        "integrity": "verified",
                        "availability": "available",
                    },
                )
            command = connection.execute(
                text(
                    "SELECT * FROM rm_asset_custody_commands WHERE "
                    "idempotency_key = :idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
            if command is not None:
                if command.request_hash != request_hash:
                    raise OwnerConflict("asset_custody_idempotency_conflict")
                custody_ref = command.custody_ref
            else:
                managed = connection.execute(
                    text(
                        "SELECT * FROM rm_asset_custodies WHERE version_ref = "
                        ":version_ref AND custody_mode = 'managed'"
                    ),
                    {"version_ref": memory_ref},
                ).first()
                if managed is None:
                    custody_ref = new_ref("asset_custody")
                    receipt_ref = new_ref("rm_asset_custody_receipt")
                    receipt_hash = _receipt_hash(
                        ASSET_CUSTODY_RECEIPT_KIND,
                        custody_ref,
                        {
                            "version_ref": memory_ref,
                            "content_hash": row.content_hash,
                            "manifest_hash": row.manifest_hash,
                            "custody_mode": "managed",
                        },
                    )
                    now = time.time()
                    connection.execute(
                        text(
                            "INSERT INTO rm_asset_custodies (custody_ref, "
                            "version_ref, custody_mode, source_locator, "
                            "receipt_kind, receipt_ref, receipt_hash, "
                            "established_at) VALUES (:custody_ref, :version_ref, "
                            "'managed', NULL, :receipt_kind, :receipt_ref, "
                            ":receipt_hash, :established_at)"
                        ),
                        {
                            "custody_ref": custody_ref,
                            "version_ref": memory_ref,
                            "receipt_kind": ASSET_CUSTODY_RECEIPT_KIND,
                            "receipt_ref": receipt_ref,
                            "receipt_hash": receipt_hash,
                            "established_at": now,
                        },
                    )
                    _register_managed_manifest(
                        connection, manifest, registered_at=now
                    )
                    connection.execute(
                        text(
                            "UPDATE research_memory_state SET revision = "
                            "revision + 1, object_count = :object_count "
                            "WHERE singleton = 'owner'"
                        ),
                        {"object_count": _managed_object_count(connection)},
                    )
                    self._feed.record(
                        connection,
                        "research_memory.asset_custody_handed_off",
                        {
                            "version_ref": memory_ref,
                            "custody_ref": custody_ref,
                            "target_custody": "managed",
                            "receipt_ref": receipt_ref,
                        },
                    )
                else:
                    custody_ref = managed.custody_ref
                connection.execute(
                    text(
                        "INSERT INTO rm_asset_custody_commands "
                        "(idempotency_key, request_hash, custody_ref, recorded_at) "
                        "VALUES (:idempotency_key, :request_hash, :custody_ref, :now)"
                    ),
                    {
                        "idempotency_key": idempotency_key,
                        "request_hash": request_hash,
                        "custody_ref": custody_ref,
                        "now": time.time(),
                    },
                )
        accepted = self._query_asset_custody(custody_ref)
        if accepted is None:
            raise OwnerConflict("asset_custody_unavailable")
        return accepted

    def _query_custody_command(
        self, idempotency_key: str, request_hash: str
    ) -> AcceptedAssetCustody | None:
        with self._database.read() as connection:
            command = connection.execute(
                text(
                    "SELECT * FROM rm_asset_custody_commands WHERE "
                    "idempotency_key = :idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
        if command is None:
            return None
        if command.request_hash != request_hash:
            raise OwnerConflict("asset_custody_idempotency_conflict")
        accepted = self._query_asset_custody(command.custody_ref)
        if accepted is None:
            raise OwnerConflict("asset_custody_unavailable")
        return accepted

    def _query_asset_repair_command(
        self, idempotency_key: str, request_hash: str
    ) -> tuple[str, str | None, AcceptedAssetCustody | None]:
        with self._database.read() as connection:
            command = connection.execute(
                text(
                    "SELECT * FROM rm_asset_repair_commands WHERE "
                    "idempotency_key = :idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
        if command is None:
            return "missing", None, None
        if command.request_hash != request_hash:
            raise OwnerConflict("asset_custody_idempotency_conflict")
        if command.status == "processing":
            return "processing", command.custody_ref, None
        if command.status != "completed" or command.completed_at is None:
            raise OwnerConflict("asset_repair_command_invalid")
        accepted = self._query_asset_custody(command.custody_ref)
        if accepted is None:
            raise OwnerConflict("asset_repair_command_invalid")
        return "completed", command.custody_ref, accepted

    def _query_pending_asset_repair(self, custody_ref: str):
        with self._database.read() as connection:
            return connection.execute(
                text(
                    "SELECT * FROM rm_asset_repair_commands WHERE custody_ref = "
                    ":custody_ref AND status = 'processing' ORDER BY started_at, "
                    "idempotency_key LIMIT 1"
                ),
                {"custody_ref": custody_ref},
            ).first()

    def _query_asset_custody(
        self, custody_ref: str
    ) -> AcceptedAssetCustody | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT version_ref FROM rm_asset_custodies WHERE "
                    "custody_ref = :custody_ref"
                ),
                {"custody_ref": custody_ref},
            ).first()
        if row is None:
            return None
        return next(
            (
                custody
                for custody in self.query_asset_custodies(
                    memory_ref=row.version_ref
                )
                if custody.custody_ref == custody_ref
            ),
            None,
        )

    def query_asset_custodies(
        self,
        memory_ref: str | None = None,
        *,
        memory_refs: tuple[str, ...] | None = None,
    ) -> tuple[AcceptedAssetCustody, ...]:
        if memory_ref is not None and memory_refs is not None:
            raise OwnerConflict("asset_custody_query_invalid")
        if memory_refs == ():
            return ()
        query = "SELECT * FROM rm_asset_custodies"
        parameters: dict[str, object] = {}
        if memory_ref is not None:
            query += " WHERE version_ref = :version_ref"
            parameters["version_ref"] = memory_ref
        elif memory_refs is not None:
            condition, parameters = _version_ref_condition(
                "version_ref", memory_refs
            )
            query += " WHERE " + condition
        query += " ORDER BY established_at, custody_ref"
        with self._database.read() as connection:
            rows = connection.execute(text(query), parameters).all()
            selected_refs = tuple(sorted({row.version_ref for row in rows}))
            if selected_refs:
                condition, version_parameters = _version_ref_condition(
                    "version_ref", selected_refs
                )
                version_rows = connection.execute(
                    text(
                        "SELECT * FROM rm_asset_versions WHERE " + condition
                    ),
                    version_parameters,
                ).all()
            else:
                version_rows = []
            versions = {row.version_ref: row for row in version_rows}
        by_version: dict[str, list[object]] = {}
        for row in rows:
            by_version.setdefault(row.version_ref, []).append(row)
        for version_ref, version_custodies in by_version.items():
            version = versions.get(version_ref)
            if version is None:
                raise OwnerConflict("asset_custody_invalid")
            self._verified_projection_asset(version, version_custodies)
        accepted: list[AcceptedAssetCustody] = []
        for row in rows:
            version = versions[row.version_ref]
            if version is None:
                raise OwnerConflict("asset_custody_invalid")
            subject_ref = (
                row.custody_ref
                if row.receipt_kind
                in {
                    ASSET_CUSTODY_ESTABLISHED_RECEIPT_KIND,
                    ASSET_CUSTODY_RECEIPT_KIND,
                }
                else row.version_ref
            )
            receipt = AcceptanceReceipt(
                issuer=RM_OWNER,
                kind=row.receipt_kind,
                receipt_ref=row.receipt_ref,
                subject_ref=subject_ref,
                payload_hash=row.receipt_hash,
            )
            if (
                row.source_locator is not None
                and row.receipt_kind
                == ASSET_CUSTODY_ESTABLISHED_RECEIPT_KIND
            ):
                locator_receipt = receipt
                locator_bound_at = float(row.established_at)
            elif all(
                value is not None
                for value in (
                    row.locator_binding_kind,
                    row.locator_binding_ref,
                    row.locator_binding_hash,
                    row.locator_binding_request_hash,
                    row.locator_bound_at,
                )
            ):
                locator_receipt = AcceptanceReceipt(
                    issuer=RM_OWNER,
                    kind=row.locator_binding_kind,
                    receipt_ref=row.locator_binding_ref,
                    subject_ref=row.custody_ref,
                    payload_hash=row.locator_binding_hash,
                )
                locator_bound_at = float(row.locator_bound_at)
            elif any(
                value is not None
                for value in (
                    row.locator_binding_kind,
                    row.locator_binding_ref,
                    row.locator_binding_hash,
                    row.locator_binding_request_hash,
                    row.locator_bound_at,
                )
            ):
                raise OwnerConflict("asset_custody_receipt_invalid")
            else:
                locator_receipt = None
                locator_bound_at = None
            custody = AcceptedAssetCustody(
                version_ref=row.version_ref,
                custody_ref=row.custody_ref,
                custody_mode=row.custody_mode,
                source_locator=row.source_locator,
                locator_receipted=(
                    row.source_locator is None or locator_receipt is not None
                ),
                locator_bound_at=locator_bound_at,
                locator_receipt=locator_receipt,
                established_at=float(row.established_at),
                receipt=receipt,
            )
            accepted.append(custody)
        return tuple(accepted)

    def verify_asset_custody_receipt(
        self,
        *,
        custody_ref: str,
        version_ref: str,
        custody_mode: str,
        receipt: AcceptanceReceipt,
    ) -> None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rm_asset_custodies WHERE custody_ref = "
                    ":custody_ref"
                ),
                {"custody_ref": custody_ref},
            ).first()
            version = connection.execute(
                text(
                    "SELECT * FROM rm_asset_versions WHERE version_ref = "
                    ":version_ref"
                ),
                {"version_ref": version_ref},
            ).first()
            custodies = connection.execute(
                text(
                    "SELECT * FROM rm_asset_custodies WHERE version_ref = "
                    ":version_ref ORDER BY custody_mode"
                ),
                {"version_ref": version_ref},
            ).all()
        if row is None or version is None or (
            row.version_ref != version_ref
            or row.custody_mode != custody_mode
            or receipt.issuer != RM_OWNER
            or row.receipt_kind != receipt.kind
            or row.receipt_ref != receipt.receipt_ref
            or row.receipt_hash != receipt.payload_hash
        ):
            raise OwnerConflict("asset_custody_receipt_invalid")
        _verify_asset_custody_rows(version, custodies)
        if row.receipt_kind in {
            ASSET_CUSTODY_ESTABLISHED_RECEIPT_KIND,
            ASSET_CUSTODY_RECEIPT_KIND,
        }:
            bindings = {
                "version_ref": version_ref,
                "content_hash": version.content_hash,
                "manifest_hash": version.manifest_hash,
                "custody_mode": custody_mode,
            }
            if row.receipt_kind == ASSET_CUSTODY_ESTABLISHED_RECEIPT_KIND:
                bindings["source_locator"] = row.source_locator
            expected_hash = _receipt_hash(
                row.receipt_kind,
                custody_ref,
                bindings,
            )
            if receipt.subject_ref != custody_ref or receipt.payload_hash != expected_hash:
                raise OwnerConflict("asset_custody_receipt_invalid")
            return
        if (
            row.receipt_ref != version.receipt_ref
            or row.receipt_hash != version.receipt_hash
            or receipt.subject_ref != version_ref
        ):
            raise OwnerConflict("asset_custody_receipt_invalid")
        self._receipt_verifier.verify_asset_receipt(
            asset_ref=version.asset_ref,
            version_ref=version_ref,
            content_hash=version.content_hash,
            manifest_hash=version.manifest_hash,
            receipt=receipt,
        )

    def place_asset_hold(
        self, memory_ref: str, *, reason: str, idempotency_key: str
    ) -> AcceptedAssetHold:
        reason = reason.strip()
        if not reason or len(reason) > 1024:
            raise OwnerConflict("asset_hold_reason_invalid")
        if not idempotency_key or len(idempotency_key) > 128:
            raise OwnerConflict("asset_hold_idempotency_key_invalid")
        request_hash = canonical_hash(
            {"command": "place_hold", "version_ref": memory_ref, "reason": reason}
        )
        replay = self._query_hold_command(
            idempotency_key, "place_hold", request_hash
        )
        if replay is not None:
            return replay
        with self._database.write() as connection:
            command = connection.execute(
                text(
                    "SELECT * FROM rm_asset_hold_commands WHERE idempotency_key = "
                    ":idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
            if command is not None:
                if (
                    command.command_kind != "place_hold"
                    or command.request_hash != request_hash
                ):
                    raise OwnerConflict("asset_hold_idempotency_conflict")
                hold_ref = command.hold_ref
            else:
                asset = connection.execute(
                    text(
                        "SELECT version_ref FROM rm_asset_versions WHERE "
                        "version_ref = :version_ref"
                    ),
                    {"version_ref": memory_ref},
                ).first()
                if asset is None:
                    raise OwnerConflict("asset_not_found")
                active_hold_count = int(
                    connection.execute(
                        text(
                            "SELECT COUNT(*) FROM rm_asset_holds WHERE "
                            "version_ref = :version_ref AND active = 1"
                        ),
                        {"version_ref": memory_ref},
                    ).scalar_one()
                )
                if active_hold_count >= MAX_ACTIVE_ASSET_HOLDS_PER_VERSION:
                    raise OwnerConflict("active_asset_hold_limit_reached")
                hold_ref = new_ref("asset_hold")
                receipt_ref = new_ref("rm_asset_hold_receipt")
                receipt_hash = _receipt_hash(
                    ASSET_HOLD_PLACED_RECEIPT_KIND,
                    hold_ref,
                    {"version_ref": memory_ref, "reason": reason},
                )
                now = time.time()
                connection.execute(
                    text(
                        "INSERT INTO rm_asset_holds (hold_ref, version_ref, reason, "
                        "active, receipt_ref, receipt_hash, placed_at, released_at, "
                        "release_receipt_ref, release_receipt_hash) VALUES "
                        "(:hold_ref, :version_ref, :reason, 1, :receipt_ref, "
                        ":receipt_hash, :placed_at, NULL, NULL, NULL)"
                    ),
                    {
                        "hold_ref": hold_ref,
                        "version_ref": memory_ref,
                        "reason": reason,
                        "receipt_ref": receipt_ref,
                        "receipt_hash": receipt_hash,
                        "placed_at": now,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE research_memory_state SET revision = revision + 1, "
                        "hold_count = hold_count + 1 WHERE singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "research_memory.asset_hold_placed",
                    {
                        "hold_ref": hold_ref,
                        "version_ref": memory_ref,
                        "receipt_ref": receipt_ref,
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO rm_asset_hold_commands (idempotency_key, "
                        "command_kind, request_hash, hold_ref, recorded_at) VALUES "
                        "(:idempotency_key, 'place_hold', :request_hash, :hold_ref, "
                        ":recorded_at)"
                    ),
                    {
                        "idempotency_key": idempotency_key,
                        "request_hash": request_hash,
                        "hold_ref": hold_ref,
                        "recorded_at": now,
                    },
                )
        accepted = self._query_asset_hold(hold_ref)
        if accepted is None:
            raise OwnerConflict("asset_hold_missing_after_commit")
        return accepted

    def release_asset_hold(
        self, hold_ref: str, *, idempotency_key: str
    ) -> AcceptedAssetHold:
        if not idempotency_key or len(idempotency_key) > 128:
            raise OwnerConflict("asset_hold_idempotency_key_invalid")
        request_hash = canonical_hash(
            {"command": "release_hold", "hold_ref": hold_ref}
        )
        replay = self._query_hold_command(
            idempotency_key, "release_hold", request_hash
        )
        if replay is not None:
            return replay
        with self._database.write() as connection:
            command = connection.execute(
                text(
                    "SELECT * FROM rm_asset_hold_commands WHERE idempotency_key = "
                    ":idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
            if command is not None:
                if (
                    command.command_kind != "release_hold"
                    or command.request_hash != request_hash
                ):
                    raise OwnerConflict("asset_hold_idempotency_conflict")
            else:
                row = connection.execute(
                    text(
                        "SELECT * FROM rm_asset_holds WHERE hold_ref = :hold_ref"
                    ),
                    {"hold_ref": hold_ref},
                ).first()
                if row is None:
                    raise OwnerConflict("asset_hold_not_found")
                # Validate the immutable placement fact before any release
                # update or command-ledger write can become durable.
                _accepted_asset_hold(row)
                now = time.time()
                if bool(row.active):
                    release_receipt_ref = new_ref("rm_asset_hold_release_receipt")
                    release_receipt_hash = _receipt_hash(
                        ASSET_HOLD_RELEASED_RECEIPT_KIND,
                        hold_ref,
                        {
                            "version_ref": row.version_ref,
                            "placement_receipt_ref": row.receipt_ref,
                            "placement_receipt_hash": row.receipt_hash,
                            "released_at": now,
                        },
                    )
                    connection.execute(
                        text(
                            "UPDATE rm_asset_holds SET active = 0, released_at = "
                            ":released_at, release_receipt_ref = :release_receipt_ref, "
                            "release_receipt_hash = :release_receipt_hash WHERE "
                            "hold_ref = :hold_ref AND active = 1"
                        ),
                        {
                            "hold_ref": hold_ref,
                            "released_at": now,
                            "release_receipt_ref": release_receipt_ref,
                            "release_receipt_hash": release_receipt_hash,
                        },
                    )
                    connection.execute(
                        text(
                            "UPDATE research_memory_state SET revision = revision + 1, "
                            "hold_count = hold_count - 1 WHERE singleton = 'owner'"
                        )
                    )
                    self._feed.record(
                        connection,
                        "research_memory.asset_hold_released",
                        {
                            "hold_ref": hold_ref,
                            "version_ref": row.version_ref,
                            "receipt_ref": release_receipt_ref,
                        },
                    )
                connection.execute(
                    text(
                        "INSERT INTO rm_asset_hold_commands (idempotency_key, "
                        "command_kind, request_hash, hold_ref, recorded_at) VALUES "
                        "(:idempotency_key, 'release_hold', :request_hash, :hold_ref, "
                        ":recorded_at)"
                    ),
                    {
                        "idempotency_key": idempotency_key,
                        "request_hash": request_hash,
                        "hold_ref": hold_ref,
                        "recorded_at": now,
                    },
                )
        accepted = self._query_asset_hold(hold_ref)
        if accepted is None:
            raise OwnerConflict("asset_hold_missing_after_commit")
        return accepted

    def _query_hold_command(
        self, idempotency_key: str, command_kind: str, request_hash: str
    ) -> AcceptedAssetHold | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rm_asset_hold_commands WHERE idempotency_key = "
                    ":idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
        if row is None:
            return None
        if row.command_kind != command_kind or row.request_hash != request_hash:
            raise OwnerConflict("asset_hold_idempotency_conflict")
        accepted = self._query_asset_hold(row.hold_ref)
        if accepted is None:
            raise OwnerConflict("asset_hold_missing_after_commit")
        return accepted

    def _query_asset_hold(self, hold_ref: str) -> AcceptedAssetHold | None:
        with self._database.read() as connection:
            row = connection.execute(
                text("SELECT * FROM rm_asset_holds WHERE hold_ref = :hold_ref"),
                {"hold_ref": hold_ref},
            ).first()
        return None if row is None else _accepted_asset_hold(row)

    def query_asset_holds(
        self,
        memory_ref: str | None = None,
        *,
        memory_refs: tuple[str, ...] | None = None,
        limit_per_version: int | None = None,
        limit: int | None = None,
        offset: int = 0,
        newest_first: bool = False,
        before_timestamp: float | None = None,
        before_ref: str | None = None,
    ) -> tuple[AcceptedAssetHold, ...]:
        if offset < 0 or (
            limit is not None
            and (limit < 1 or limit > ASSET_HISTORY_QUERY_MAX_PAGE_SIZE + 1)
        ):
            raise OwnerConflict("asset_hold_query_invalid")
        if limit is not None and limit_per_version is not None:
            raise OwnerConflict("asset_hold_query_invalid")
        if (before_timestamp is None) != (before_ref is None) or (
            before_timestamp is not None and not newest_first
        ):
            raise OwnerConflict("asset_hold_query_invalid")
        if memory_ref is not None and memory_refs is not None:
            raise OwnerConflict("asset_hold_query_invalid")
        if memory_refs == ():
            return ()
        query = "SELECT * FROM rm_asset_holds"
        parameters: dict[str, object] = {}
        conditions: list[str] = []
        if memory_ref is not None:
            conditions.append("version_ref = :version_ref")
            parameters["version_ref"] = memory_ref
        elif memory_refs is not None:
            condition, version_parameters = _version_ref_condition(
                "version_ref", memory_refs
            )
            conditions.append(condition)
            parameters.update(version_parameters)
        if before_timestamp is not None and before_ref is not None:
            conditions.append(
                "(placed_at < :before_timestamp OR (placed_at = "
                ":before_timestamp AND hold_ref < :before_ref))"
            )
            parameters.update(
                {"before_timestamp": before_timestamp, "before_ref": before_ref}
            )
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        if limit_per_version is not None:
            if not 1 <= limit_per_version <= ASSET_PROJECTION_HISTORY_PER_VERSION:
                raise OwnerConflict("asset_projection_page_invalid")
            query = (
                "SELECT * FROM (SELECT holds.*, ROW_NUMBER() OVER (PARTITION BY "
                "version_ref ORDER BY placed_at DESC, hold_ref DESC) AS row_rank "
                "FROM ("
                + query
                + ") AS holds) AS ranked WHERE active = 1 OR row_rank <= "
                ":history_limit"
            )
            parameters["history_limit"] = limit_per_version
        direction = " DESC" if newest_first else ""
        query += f" ORDER BY placed_at{direction}, hold_ref{direction}"
        if limit is not None:
            query += " LIMIT :query_limit OFFSET :query_offset"
            parameters.update({"query_limit": limit, "query_offset": offset})
        with self._database.read() as connection:
            rows = connection.execute(text(query), parameters).all()
        return tuple(_accepted_asset_hold(row) for row in rows)

    def assess_release_eligibility(
        self,
        memory_ref: str,
        *,
        expected_reference_revision: int | None,
        idempotency_key: str,
    ) -> ReleaseEligibilityAssessment:
        if not idempotency_key or len(idempotency_key) > 128:
            raise OwnerConflict("release_eligibility_idempotency_key_invalid")
        if expected_reference_revision is not None and (
            isinstance(expected_reference_revision, bool)
            or expected_reference_revision < 0
        ):
            raise OwnerConflict("reference_revision_invalid")
        request_hash = canonical_hash(
            {
                "version_ref": memory_ref,
                "expected_reference_revision": expected_reference_revision,
            }
        )
        replay = self._query_release_assessment_by_key(
            idempotency_key, request_hash
        )
        if replay is not None:
            return replay
        with self._database.read() as connection:
            preflight_asset = connection.execute(
                text(
                    "SELECT * FROM rm_asset_versions WHERE version_ref = :version_ref"
                ),
                {"version_ref": memory_ref},
            ).first()
            preflight_custodies = connection.execute(
                text(
                    "SELECT * FROM rm_asset_custodies WHERE version_ref = "
                    ":version_ref ORDER BY custody_mode"
                ),
                {"version_ref": memory_ref},
            ).all()
        if preflight_asset is None:
            raise OwnerConflict("asset_not_found")
        preflight_basis_hash = _asset_observation_basis_hash(
            preflight_asset, preflight_custodies
        )
        preflight_asset_valid = True
        try:
            self._verified_accepted_asset(preflight_asset, preflight_custodies)
            preflight_integrity, preflight_availability = _asset_current_state(
                self._object_store, preflight_asset, preflight_custodies
            )
        except (OSError, OwnerConflict):
            preflight_asset_valid = False
            preflight_integrity, preflight_availability = "failed", "unavailable"
        preflight_reference_revision: int | None = None
        preflight_graph_reference_refs: tuple[str, ...] = ()
        preflight_reference_valid = True
        if expected_reference_revision is not None and self._reference_reader is not None:
            try:
                (
                    preflight_reference_revision,
                    preflight_graph_reference_refs,
                ) = self._reference_reader.query_asset_reference_state(memory_ref)
            except (OSError, OwnerConflict):
                preflight_reference_valid = False
        with self._database.write() as connection:
            existing = connection.execute(
                text(
                    "SELECT * FROM rm_release_eligibility_assessments WHERE "
                    "idempotency_key = :idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise OwnerConflict("release_eligibility_idempotency_conflict")
                return _accepted_release_assessment(existing)
            asset = connection.execute(
                text(
                    "SELECT * FROM rm_asset_versions WHERE version_ref = :version_ref"
                ),
                {"version_ref": memory_ref},
            ).first()
            custodies = connection.execute(
                text(
                    "SELECT * FROM rm_asset_custodies WHERE version_ref = "
                    ":version_ref ORDER BY custody_mode"
                ),
                {"version_ref": memory_ref},
            ).all()
            if asset is None:
                raise OwnerConflict("asset_not_found")
            reasons: list[str] = []
            try:
                asset_state_valid = (
                    preflight_asset_valid
                    and _asset_observation_basis_hash(asset, custodies)
                    == preflight_basis_hash
                )
            except OwnerConflict:
                asset_state_valid = False
            if not asset_state_valid:
                reasons.append("asset_state_uncertain")
            owner_reference_refs: tuple[str, ...] = ()
            if asset.acceptance_kind == CONTENT_RECEIPT_KIND:
                owner_reference_refs = (
                    f"rm-formal-content:{memory_ref}",
                )
            elif asset.acceptance_kind == IDEA_CONTENT_RECEIPT_KIND:
                owner_reference_refs = (
                    f"rm-idea-content:{memory_ref}",
                )
            active_reference_refs = owner_reference_refs
            observed_reference_revision: int | None = None
            if expected_reference_revision is None:
                reasons.append("reference_revision_required")
            elif self._reference_reader is None:
                reasons.append("reference_state_uncertain")
            elif not preflight_reference_valid or preflight_reference_revision is None:
                reasons.append("reference_state_uncertain")
            else:
                try:
                    observed_reference_revision = (
                        self._reference_reader.query_asset_reference_revision()
                    )
                    if (
                        observed_reference_revision != preflight_reference_revision
                        or observed_reference_revision
                        != expected_reference_revision
                    ):
                        reasons.append("reference_revision_stale")
                    else:
                        active_reference_refs = tuple(
                            dict.fromkeys(
                                (
                                    *owner_reference_refs,
                                    *preflight_graph_reference_refs,
                                )
                            )
                        )
                except (OSError, OwnerConflict):
                    reasons.append("reference_state_uncertain")
            if active_reference_refs:
                reasons.append("semantic_reference_active")
            if asset_state_valid:
                if preflight_integrity != "verified":
                    reasons.append("asset_state_uncertain")
                if preflight_availability != "available":
                    reasons.append("asset_availability_unavailable")
            try:
                hold_rows = connection.execute(
                    text(
                        "SELECT * FROM rm_asset_holds WHERE version_ref = "
                        ":version_ref AND active = 1 ORDER BY hold_ref"
                    ),
                    {"version_ref": memory_ref},
                ).all()
                accepted_holds = tuple(
                    _accepted_asset_hold(row) for row in hold_rows
                )
                active_hold_refs = tuple(
                    hold.hold_ref
                    for hold in accepted_holds
                    if hold.active
                )
            except OwnerConflict:
                active_hold_refs = ()
                reasons.append("asset_state_uncertain")
            if active_hold_refs:
                reasons.append("active_hold")
            reason_codes = tuple(dict.fromkeys(reasons))
            assessment_ref = new_ref("release_assessment")
            receipt_ref = new_ref("rm_release_assessment_receipt")
            bindings = {
                "version_ref": memory_ref,
                "expected_reference_revision": expected_reference_revision,
                "observed_reference_revision": observed_reference_revision,
                "active_reference_refs": list(active_reference_refs),
                "active_hold_refs": list(active_hold_refs),
                "eligible": not reason_codes,
                "reason_codes": list(reason_codes),
            }
            receipt_hash = _receipt_hash(
                RELEASE_ELIGIBILITY_RECEIPT_KIND,
                assessment_ref,
                bindings,
            )
            now = time.time()
            connection.execute(
                text(
                    "INSERT INTO rm_release_eligibility_assessments "
                    "(assessment_ref, version_ref, expected_reference_revision, "
                    "observed_reference_revision, active_reference_refs_json, "
                    "active_reference_refs_hash, active_hold_refs_json, "
                    "active_hold_refs_hash, eligible, reason_codes_json, "
                    "reason_codes_hash, idempotency_key, request_hash, receipt_ref, "
                    "receipt_hash, assessed_at) VALUES (:assessment_ref, "
                    ":version_ref, :expected_reference_revision, "
                    ":observed_reference_revision, :active_reference_refs_json, "
                    ":active_reference_refs_hash, :active_hold_refs_json, "
                    ":active_hold_refs_hash, :eligible, :reason_codes_json, "
                    ":reason_codes_hash, :idempotency_key, :request_hash, "
                    ":receipt_ref, :receipt_hash, :assessed_at)"
                ),
                {
                    "assessment_ref": assessment_ref,
                    "version_ref": memory_ref,
                    "expected_reference_revision": expected_reference_revision,
                    "observed_reference_revision": observed_reference_revision,
                    "active_reference_refs_json": canonical_json(
                        list(active_reference_refs)
                    ),
                    "active_reference_refs_hash": canonical_hash(
                        list(active_reference_refs)
                    ),
                    "active_hold_refs_json": canonical_json(list(active_hold_refs)),
                    "active_hold_refs_hash": canonical_hash(list(active_hold_refs)),
                    "eligible": not reason_codes,
                    "reason_codes_json": canonical_json(list(reason_codes)),
                    "reason_codes_hash": canonical_hash(list(reason_codes)),
                    "idempotency_key": idempotency_key,
                    "request_hash": request_hash,
                    "receipt_ref": receipt_ref,
                    "receipt_hash": receipt_hash,
                    "assessed_at": now,
                },
            )
            connection.execute(
                text(
                    "UPDATE research_memory_state SET revision = revision + 1 "
                    "WHERE singleton = 'owner'"
                )
            )
            self._feed.record(
                connection,
                "research_memory.release_eligibility_assessed",
                {
                    "assessment_ref": assessment_ref,
                    "version_ref": memory_ref,
                    "eligible": not reason_codes,
                    "receipt_ref": receipt_ref,
                },
            )
        accepted = self._query_release_assessment(assessment_ref)
        if accepted is None:
            raise OwnerConflict("release_eligibility_missing_after_commit")
        return accepted

    def _query_release_assessment_by_key(
        self, idempotency_key: str, request_hash: str
    ) -> ReleaseEligibilityAssessment | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rm_release_eligibility_assessments WHERE "
                    "idempotency_key = :idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
        if row is None:
            return None
        if row.request_hash != request_hash:
            raise OwnerConflict("release_eligibility_idempotency_conflict")
        return _accepted_release_assessment(row)

    def _query_release_assessment(
        self, assessment_ref: str
    ) -> ReleaseEligibilityAssessment | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rm_release_eligibility_assessments WHERE "
                    "assessment_ref = :assessment_ref"
                ),
                {"assessment_ref": assessment_ref},
            ).first()
        return None if row is None else _accepted_release_assessment(row)

    def query_release_eligibility_assessments(
        self,
        memory_ref: str | None = None,
        *,
        memory_refs: tuple[str, ...] | None = None,
        limit_per_version: int | None = None,
        limit: int | None = None,
        offset: int = 0,
        newest_first: bool = False,
        before_timestamp: float | None = None,
        before_ref: str | None = None,
    ) -> tuple[ReleaseEligibilityAssessment, ...]:
        if offset < 0 or (
            limit is not None
            and (limit < 1 or limit > ASSET_HISTORY_QUERY_MAX_PAGE_SIZE + 1)
        ):
            raise OwnerConflict("release_assessment_query_invalid")
        if limit is not None and limit_per_version is not None:
            raise OwnerConflict("release_assessment_query_invalid")
        if (before_timestamp is None) != (before_ref is None) or (
            before_timestamp is not None and not newest_first
        ):
            raise OwnerConflict("release_assessment_query_invalid")
        if memory_ref is not None and memory_refs is not None:
            raise OwnerConflict("release_assessment_query_invalid")
        if memory_refs == ():
            return ()
        query = "SELECT * FROM rm_release_eligibility_assessments"
        parameters: dict[str, object] = {}
        if memory_ref is not None:
            query += " WHERE version_ref = :version_ref"
            parameters["version_ref"] = memory_ref
        elif memory_refs is not None:
            condition, parameters = _version_ref_condition(
                "version_ref", memory_refs
            )
            query += " WHERE " + condition
        if before_timestamp is not None and before_ref is not None:
            cursor_clause = (
                "(assessed_at < :before_timestamp OR (assessed_at = "
                ":before_timestamp AND assessment_ref < :before_ref))"
            )
            query += (" AND " if " WHERE " in query else " WHERE ") + cursor_clause
            parameters.update(
                {"before_timestamp": before_timestamp, "before_ref": before_ref}
            )
        if limit_per_version is not None:
            if not 1 <= limit_per_version <= ASSET_PROJECTION_HISTORY_PER_VERSION:
                raise OwnerConflict("asset_projection_page_invalid")
            query = (
                "SELECT * FROM (SELECT assessments.*, ROW_NUMBER() OVER "
                "(PARTITION BY version_ref ORDER BY assessed_at DESC, "
                "assessment_ref DESC) AS row_rank FROM ("
                + query
                + ") AS assessments) AS ranked WHERE row_rank <= :history_limit"
            )
            parameters["history_limit"] = limit_per_version
        direction = " DESC" if newest_first else ""
        query += f" ORDER BY assessed_at{direction}, assessment_ref{direction}"
        if limit is not None:
            query += " LIMIT :query_limit OFFSET :query_offset"
            parameters.update({"query_limit": limit, "query_offset": offset})
        with self._database.read() as connection:
            rows = connection.execute(text(query), parameters).all()
        return tuple(_accepted_release_assessment(row) for row in rows)

    def verify_asset_receipt(self, **values) -> None:
        self._receipt_verifier.verify_asset_receipt(**values)

    def verify_asset_binding(self, **values) -> None:
        self._receipt_verifier.verify_asset_binding(**values)

    def verify_asset_projection_binding(self, **values) -> None:
        """Verify an exact receipt against the last durable custody observation."""

        self._receipt_verifier.verify_asset_receipt(**values)
        with self._database.read() as connection:
            observation = connection.execute(
                text(
                    "SELECT integrity, availability FROM "
                    "rm_asset_verification_observations WHERE version_ref = "
                    ":version_ref"
                ),
                {"version_ref": values.get("version_ref")},
            ).first()
        if observation is None or (
            observation.integrity != "verified"
            or observation.availability != "available"
        ):
            raise OwnerConflict("asset_custody_observation_unavailable")

    def _store_asset_object(self, object_hash: str, content: bytes) -> str:
        if hashlib.sha256(content).hexdigest() != object_hash:
            raise OwnerConflict("asset_object_hash_conflict")
        directory = self._object_store / "assets" / object_hash[:2]
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = directory / object_hash
        if destination.is_file():
            if not _file_matches(destination, len(content), object_hash):
                return self._replace_asset_object(object_hash, content)
        else:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{object_hash}.", dir=directory
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(content)
                    output.flush()
                    os.fsync(output.fileno())
                temporary.chmod(0o600)
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
        directory_descriptor = os.open(
            directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        return str(destination.relative_to(self._object_store))

    def _replace_asset_object(self, object_hash: str, content: bytes) -> str:
        if hashlib.sha256(content).hexdigest() != object_hash:
            raise OwnerConflict("asset_object_hash_conflict")
        directory = self._object_store / "assets" / object_hash[:2]
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = directory / object_hash
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{object_hash}.repair.", dir=directory
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        directory_descriptor = os.open(
            directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        return str(destination.relative_to(self._object_store))

    def query_snapshot(self) -> OwnerSnapshot:
        snapshot = self.query_projection_snapshot()
        facts = dict(snapshot.facts)
        try:
            with self._database.read() as connection:
                rows = connection.execute(
                    text("SELECT * FROM rm_formal_question_contents")
                ).all()
            for row in rows:
                _verify_object(self._object_store, row)
            with self._database.read() as connection:
                idea_rows = connection.execute(
                    text("SELECT * FROM rm_idea_outcome_contents")
                ).all()
            for row in idea_rows:
                _verify_idea_object(self._object_store, row)
                _verify_idea_payload(row)
            with self._database.read() as connection:
                asset_rows = connection.execute(
                    text("SELECT * FROM rm_asset_versions")
                ).all()
                custody_rows = connection.execute(
                    text(
                        "SELECT * FROM rm_asset_custodies ORDER BY version_ref, "
                        "custody_mode"
                    )
                ).all()
            custodies_by_version: dict[str, list[object]] = {}
            for custody in custody_rows:
                custodies_by_version.setdefault(custody.version_ref, []).append(
                    custody
                )
            if any(
                _asset_current_state(
                    self._object_store,
                    row,
                    custodies_by_version.get(row.version_ref, []),
                )[0]
                != "verified"
                for row in asset_rows
            ):
                facts["asset_integrity"] = "failed"
                return OwnerSnapshot(
                    owner=snapshot.owner,
                    revision=snapshot.revision,
                    facts=facts,
                    status="unavailable",
                )
        except (OSError, OwnerConflict):
            facts["formal_content_custody"] = "unavailable"
            return OwnerSnapshot(
                owner=snapshot.owner,
                revision=snapshot.revision,
                facts=facts,
                status="unavailable",
            )
        return OwnerSnapshot(
            owner=snapshot.owner,
            revision=snapshot.revision,
            facts=facts,
            status=snapshot.status,
        )

    def query_projection_snapshot(self) -> OwnerSnapshot:
        """Return bounded counters plus an indexed aggregate integrity fact."""

        snapshot = self._snapshot.query_snapshot()
        with self._database.read() as connection:
            verification_state = connection.execute(
                text(
                    "SELECT observation_count, uncertain_count FROM "
                    "rm_asset_verification_state WHERE singleton = 'owner'"
                )
            ).first()
        uncertain = verification_state is None or (
            int(verification_state.observation_count)
            != int(snapshot.facts.get("asset_version_count", 0))
            or int(verification_state.uncertain_count) != 0
        )
        asset_integrity = "failed" if uncertain else "verified"
        return OwnerSnapshot(
            owner=snapshot.owner,
            revision=snapshot.revision,
            facts={
                **snapshot.facts,
                "managed_store_available": self._object_store.is_dir(),
                "formal_content_custody": "available",
                "asset_integrity": asset_integrity,
            },
            status=(
                snapshot.status if not uncertain else "unavailable"
            ),
        )

    def preview_question_content_acceptance(
        self,
        *,
        initialization_id: str,
        proposal_ref: str,
        proposal_hash: str,
    ) -> dict[str, object]:
        assertion = {
            "owner": RM_OWNER,
            "operation": "accept_question_content",
            "may_change": ["immutable_question_content", "managed_custody"],
            "will_not_change": ["question_identity", "quest_graph", "research_cycle"],
            "preconditions": ["exact_human_confirmation", "exact_quest_receipt"],
            "risks": ["custody_failure_leaves_the_accepted_quest_empty"],
            "stale_if": ["proposal_changes", "quest_receipt_changes"],
            "bindings": {
                "initialization_id": initialization_id,
                "proposal_ref": proposal_ref,
                "proposal_hash": proposal_hash,
            },
        }
        return {**assertion, "target_hash": canonical_hash(assertion)}

    def query_question_content(
        self, initialization_id: str
    ) -> AcceptedQuestionContent | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rm_formal_question_contents WHERE "
                    "initialization_id = :initialization_id"
                ),
                {"initialization_id": initialization_id},
            ).first()
        if row is None:
            return None
        _verify_object(self._object_store, row)
        accepted = _accepted_content(row)
        self._quest_verifier.verify_quest_receipt(
            initialization_id=initialization_id,
            quest_ref=row.quest_ref,
            proposal_ref=row.proposal_ref,
            proposal_hash=row.proposal_hash,
            confirmation_ref=row.confirmation_ref,
            receipt=AcceptanceReceipt(
                issuer="research_graph",
                kind="quest_acceptance",
                receipt_ref=row.quest_receipt_ref,
                subject_ref=row.quest_ref,
                payload_hash=row.quest_receipt_hash,
            ),
        )
        self._receipt_verifier.verify_question_content_receipt(
            initialization_id=initialization_id,
            content_ref=accepted.content_ref,
            content_hash=accepted.content_hash,
            schema_ref=accepted.schema_ref,
            proposal_ref=accepted.proposal_ref,
            proposal_hash=accepted.proposal_hash,
            confirmation_ref=accepted.confirmation_ref,
            receipt=accepted.receipt,
        )
        return accepted

    def read_question_content(
        self, content_ref: str, expected_hash: str
    ) -> dict[str, object]:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rm_formal_question_contents WHERE content_ref = "
                    ":content_ref"
                ),
                {"content_ref": content_ref},
            ).first()
        if row is None or row.content_hash != expected_hash:
            raise OwnerConflict("question_content_not_found")
        _verify_object(self._object_store, row)
        self._receipt_verifier.verify_question_content_receipt(
            initialization_id=row.initialization_id,
            content_ref=row.content_ref,
            content_hash=row.content_hash,
            schema_ref=row.schema_ref,
            proposal_ref=row.proposal_ref,
            proposal_hash=row.proposal_hash,
            confirmation_ref=row.confirmation_ref,
            receipt=_accepted_content(row).receipt,
        )
        try:
            content = decoded_object(row.content_json)
        except (TypeError, ValueError) as error:
            raise OwnerConflict("question_content_custody_unavailable") from error
        if canonical_hash(content) != expected_hash:
            raise OwnerConflict("question_content_custody_unavailable")
        return content

    def accept_question_content(
        self,
        *,
        initialization_id: str,
        quest: AcceptedQuest,
        content: dict[str, object],
        content_hash: str,
    ) -> AcceptedQuestionContent:
        if canonical_hash(content) != content_hash:
            raise OwnerConflict("question_content_hash_mismatch")
        proposal_binding: dict[str, object] = {
            "schema_ref": QUESTION_PROPOSAL_SCHEMA,
            "basis_revision": quest.draft_revision,
            "basis_hash": quest.draft_hash,
            "content": content,
        }
        legacy_proposal_hash = canonical_hash(proposal_binding)
        literature_snapshot = self.query_literature_snapshot_for_basis(
            initialization_id,
            quest.draft_revision,
            quest.draft_hash,
        )
        proposal_binding.update(
            {
                "binding_schema_ref": (
                    "meta-research/question-proposal-binding/v2"
                ),
                "literature_snapshot_ref": (
                    None
                    if literature_snapshot is None
                    else literature_snapshot.snapshot_ref
                ),
                "literature_snapshot_hash": (
                    None
                    if literature_snapshot is None
                    else literature_snapshot.snapshot_hash
                ),
            }
        )
        if quest.proposal_hash not in {
            legacy_proposal_hash,
            canonical_hash(proposal_binding),
        }:
            raise OwnerConflict("question_content_proposal_mismatch")
        self._confirmation_verifier.verify_bundle_confirmation(
            initialization_id=initialization_id,
            draft_revision=quest.draft_revision,
            draft_hash=quest.draft_hash,
            proposal_ref=quest.proposal_ref,
            proposal_hash=quest.proposal_hash,
            preview_ref=quest.preview_ref,
            preview_hash=quest.preview_hash,
            receipt=quest.confirmation,
        )
        self._quest_verifier.verify_quest_receipt(
            initialization_id=initialization_id,
            quest_ref=quest.quest_ref,
            proposal_ref=quest.proposal_ref,
            proposal_hash=quest.proposal_hash,
            confirmation_ref=quest.confirmation.receipt_ref,
            receipt=quest.receipt,
        )
        bindings = {
            "initialization_id": initialization_id,
            "quest_ref": quest.quest_ref,
            "quest_receipt_ref": quest.receipt.receipt_ref,
            "quest_receipt_hash": quest.receipt.payload_hash,
            "proposal_ref": quest.proposal_ref,
            "proposal_hash": quest.proposal_hash,
            "confirmation_ref": quest.confirmation.receipt_ref,
            "confirmation_hash": quest.confirmation.payload_hash,
            "content_hash": content_hash,
            "schema_ref": QUESTION_CONTENT_SCHEMA,
        }
        content_json = canonical_json(content)
        object_path = self._store_content(content_hash, content_json)
        with self._database.write() as connection:
            existing = connection.execute(
                text(
                    "SELECT * FROM rm_formal_question_contents WHERE "
                    "initialization_id = :initialization_id"
                ),
                {"initialization_id": initialization_id},
            ).first()
            if existing is not None:
                if any(getattr(existing, key) != value for key, value in bindings.items()) or (
                    existing.receipt_hash != _content_receipt_hash(existing)
                ):
                    raise OwnerConflict("question_content_acceptance_conflict")
                _verify_object(self._object_store, existing)
                return _accepted_content(existing)

            content_ref = new_ref("memory_content")
            receipt_ref = new_ref("rm_content_receipt")
            receipt_hash = _receipt_hash(CONTENT_RECEIPT_KIND, content_ref, bindings)
            accepted_at = time.time()
            connection.execute(
                text(
                    "INSERT INTO rm_formal_question_contents (content_ref, "
                    "initialization_id, quest_ref, quest_receipt_ref, "
                    "quest_receipt_hash, proposal_ref, proposal_hash, "
                    "confirmation_ref, confirmation_hash, content_hash, schema_ref, "
                    "content_json, object_path, receipt_ref, receipt_hash, accepted_at) "
                    "VALUES (:content_ref, :initialization_id, :quest_ref, "
                    ":quest_receipt_ref, :quest_receipt_hash, :proposal_ref, "
                    ":proposal_hash, :confirmation_ref, :confirmation_hash, "
                    ":content_hash, :schema_ref, :content_json, :object_path, "
                    ":receipt_ref, :receipt_hash, :accepted_at)"
                ),
                {
                    **bindings,
                    "content_ref": content_ref,
                    "content_json": content_json,
                    "object_path": object_path,
                    "receipt_ref": receipt_ref,
                    "receipt_hash": receipt_hash,
                    "accepted_at": accepted_at,
                },
            )
            _insert_managed_content_asset(
                connection,
                version_ref=content_ref,
                source_kind="formal_question",
                display_name="Formal Question content",
                content_hash=content_hash,
                content_json=content_json,
                object_path=object_path,
                provenance={
                    "source_table": "rm_formal_question_contents",
                    "initialization_id": initialization_id,
                    "quest_ref": quest.quest_ref,
                    "proposal_ref": quest.proposal_ref,
                },
                acceptance_kind=CONTENT_RECEIPT_KIND,
                receipt_ref=receipt_ref,
                receipt_hash=receipt_hash,
                accepted_at=accepted_at,
            )
            connection.execute(
                text(
                    "UPDATE research_memory_state SET revision = revision + 1, "
                    "asset_count = (SELECT COUNT(*) FROM rm_assets), "
                    "asset_version_count = (SELECT COUNT(*) FROM "
                    "rm_asset_versions), object_count = :object_count, "
                    "formal_content_count = formal_content_count + 1 "
                    "WHERE singleton = 'owner'"
                ),
                {"object_count": _managed_object_count(connection)},
            )
            self._feed.record(
                connection,
                "research_memory.question_content_accepted",
                {
                    "initialization_id": initialization_id,
                    "content_ref": content_ref,
                    "content_hash": content_hash,
                    "receipt_ref": receipt_ref,
                },
            )
        accepted = self.query_question_content(initialization_id)
        if accepted is None:
            raise OwnerConflict("question_content_receipt_missing_after_commit")
        return accepted

    def verify_question_content_receipt(self, **values) -> None:
        self._receipt_verifier.verify_question_content_receipt(**values)

    def accept_idea_outcome_content(
        self,
        *,
        request_ref: str,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        submission_ref: str,
        outcome: dict[str, object],
        review: dict[str, object],
        execution_receipt: AcceptanceReceipt,
        reviewed_draft: dict[str, object] | None = None,
    ) -> AcceptedIdeaOutcomeContent:
        if self._execution_verifier is None:
            raise OwnerConflict("attempt_execution_verifier_unavailable")
        for value in (request_ref, run_ref, attempt_ref, fence_ref, submission_ref):
            if not value:
                raise OwnerConflict("idea_content_lineage_invalid")
        kind_value = outcome.get("kind")
        kind = {
            "IdeaSet": "idea_set",
            "NoViableCandidate": "no_viable_candidate",
        }.get(kind_value)
        if kind is None:
            raise OwnerConflict("idea_outcome_kind_invalid")
        reviewed_draft = _resolved_reviewed_draft(
            outcome,
            review,
            reviewed_draft,
        )
        try:
            validated_outcome_hash, validated_review_hash = validate_idea_content(
                outcome,
                review,
                reviewed_draft=reviewed_draft,
            )
        except IdeaContractError as error:
            raise OwnerConflict(str(error)) from error
        payload = {
            "schema_ref": ATTEMPT_EXECUTION_SCHEMA,
            "outcome": outcome,
            "reviewed_draft": reviewed_draft,
            "review": review,
        }
        payload_json = canonical_json(payload)
        payload_hash = canonical_hash(payload)
        outcome_json = canonical_json(outcome)
        outcome_hash = canonical_hash(outcome)
        reviewed_draft_json = canonical_json(reviewed_draft)
        reviewed_draft_hash = canonical_hash(reviewed_draft)
        review_json = canonical_json(review)
        review_hash = canonical_hash(review)
        if (
            outcome_hash != validated_outcome_hash
            or review_hash != validated_review_hash
        ):
            raise OwnerConflict("idea_content_hash_invalid")
        self._execution_verifier.verify_attempt_execution_receipt(
            request_ref=request_ref,
            run_ref=run_ref,
            attempt_ref=attempt_ref,
            fence_ref=fence_ref,
            submission_ref=submission_ref,
            payload_hash=payload_hash,
            receipt=execution_receipt,
        )
        object_path = self._store_idea_content(payload_hash, payload_json)
        bindings = {
            "request_ref": request_ref,
            "run_ref": run_ref,
            "attempt_ref": attempt_ref,
            "fence_ref": fence_ref,
            "submission_ref": submission_ref,
            "outcome_kind": kind,
            "payload_hash": payload_hash,
            "outcome_hash": outcome_hash,
            "reviewed_draft_hash": reviewed_draft_hash,
            "review_hash": review_hash,
            "execution_receipt_ref": execution_receipt.receipt_ref,
            "execution_receipt_hash": execution_receipt.payload_hash,
        }
        with self._database.write() as connection:
            existing = connection.execute(
                text(
                    "SELECT * FROM rm_idea_outcome_contents WHERE submission_ref = "
                    ":submission_ref"
                ),
                {"submission_ref": submission_ref},
            ).first()
            if existing is not None:
                if any(getattr(existing, key) != value for key, value in bindings.items()):
                    raise OwnerConflict("idea_content_acceptance_conflict")
                _verify_idea_object(self._object_store, existing)
                _verify_idea_payload(existing)
                if existing.receipt_hash != _idea_content_receipt_hash(existing):
                    raise OwnerConflict("idea_content_receipt_invalid")
                return _accepted_idea_content(existing)

            content_ref = new_ref("idea_content")
            receipt_ref = new_ref("rm_idea_content_receipt")
            receipt_hash = _receipt_hash(
                IDEA_CONTENT_RECEIPT_KIND, content_ref, bindings
            )
            accepted_at = time.time()
            connection.execute(
                text(
                    "INSERT INTO rm_idea_outcome_contents (content_ref, "
                    "request_ref, run_ref, attempt_ref, fence_ref, submission_ref, "
                    "outcome_kind, outcome_json, outcome_hash, review_json, "
                    "reviewed_draft_json, reviewed_draft_hash, review_hash, "
                    "payload_json, payload_hash, object_path, "
                    "execution_receipt_ref, execution_receipt_hash, receipt_ref, "
                    "receipt_hash, accepted_at) VALUES (:content_ref, :request_ref, "
                    ":run_ref, :attempt_ref, :fence_ref, :submission_ref, "
                    ":outcome_kind, :outcome_json, :outcome_hash, :review_json, "
                    ":reviewed_draft_json, :reviewed_draft_hash, :review_hash, "
                    ":payload_json, :payload_hash, :object_path, "
                    ":execution_receipt_ref, :execution_receipt_hash, :receipt_ref, "
                    ":receipt_hash, :accepted_at)"
                ),
                {
                    **bindings,
                    "content_ref": content_ref,
                    "outcome_json": outcome_json,
                    "reviewed_draft_json": reviewed_draft_json,
                    "review_json": review_json,
                    "payload_json": payload_json,
                    "object_path": object_path,
                    "receipt_ref": receipt_ref,
                    "receipt_hash": receipt_hash,
                    "accepted_at": accepted_at,
                },
            )
            _insert_managed_content_asset(
                connection,
                version_ref=content_ref,
                source_kind="idea_outcome",
                display_name="Idea outcome content",
                content_hash=payload_hash,
                content_json=payload_json,
                object_path=object_path,
                provenance={
                    "source_table": "rm_idea_outcome_contents",
                    "request_ref": request_ref,
                    "run_ref": run_ref,
                    "submission_ref": submission_ref,
                },
                acceptance_kind=IDEA_CONTENT_RECEIPT_KIND,
                receipt_ref=receipt_ref,
                receipt_hash=receipt_hash,
                accepted_at=accepted_at,
            )
            connection.execute(
                text(
                    "UPDATE research_memory_state SET revision = revision + 1, "
                    "asset_count = (SELECT COUNT(*) FROM rm_assets), "
                    "asset_version_count = (SELECT COUNT(*) FROM "
                    "rm_asset_versions), object_count = :object_count, "
                    "idea_content_count = "
                    "idea_content_count + 1 WHERE singleton = 'owner'"
                ),
                {"object_count": _managed_object_count(connection)},
            )
            self._feed.record(
                connection,
                "research_memory.idea_outcome_content_accepted",
                {
                    "request_ref": request_ref,
                    "run_ref": run_ref,
                    "attempt_ref": attempt_ref,
                    "submission_ref": submission_ref,
                    "content_ref": content_ref,
                    "outcome_kind": kind,
                    "payload_hash": payload_hash,
                    "receipt_ref": receipt_ref,
                },
            )
        accepted = self.query_idea_outcome_content(submission_ref)
        if accepted is None:
            raise OwnerConflict("idea_content_missing_after_commit")
        return accepted

    def query_idea_outcome_content(
        self, submission_ref: str
    ) -> AcceptedIdeaOutcomeContent | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rm_idea_outcome_contents WHERE submission_ref = "
                    ":submission_ref"
                ),
                {"submission_ref": submission_ref},
            ).first()
        if row is None:
            return None
        accepted = _accepted_idea_content(row)
        self._receipt_verifier.verify_idea_content_receipt(
            request_ref=row.request_ref,
            submission_ref=row.submission_ref,
            content_ref=row.content_ref,
            payload_hash=row.payload_hash,
            outcome_hash=row.outcome_hash,
            reviewed_draft_hash=row.reviewed_draft_hash,
            review_hash=row.review_hash,
            receipt=accepted.receipt,
        )
        return accepted

    def verify_idea_content_receipt(self, **values) -> None:
        self._receipt_verifier.verify_idea_content_receipt(**values)

    def accept_plan_document(
        self,
        *,
        accepted_question: AcceptedQuestionBinding,
        accepted_idea_set: AcceptedIdeaSetBinding,
        context_pack_ref: str,
        request_ref: str,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        submission_ref: str,
        plan_document: dict[str, object],
        review: dict[str, object],
        execution_receipt: AcceptanceReceipt,
        reviewed_draft: dict[str, object] | None = None,
    ) -> AcceptedPlanDocument:
        if self._execution_verifier is None:
            raise OwnerConflict("attempt_execution_verifier_unavailable")
        if self._stage_request_verifier is None:
            raise OwnerConflict("stage_request_verifier_unavailable")
        for value in (
            request_ref,
            run_ref,
            attempt_ref,
            fence_ref,
            submission_ref,
            context_pack_ref,
        ):
            if not value:
                raise OwnerConflict("plan_content_lineage_invalid")
        if (
            accepted_idea_set.outcome_kind != "idea_set"
            or execution_receipt.kind != PLAN_ATTEMPT_EXECUTION_RECEIPT_KIND
        ):
            raise OwnerConflict("plan_content_lineage_invalid")
        verified_request = (
            self._stage_request_verifier.verify_plan_stage_request_binding(
                request_ref=request_ref,
                accepted_question=accepted_question,
                accepted_idea_set=accepted_idea_set,
                context_pack_ref=context_pack_ref,
            )
        )
        if (
            verified_request.accepted_question != accepted_question
            or verified_request.accepted_idea_set != accepted_idea_set
            or verified_request.context_pack_ref != context_pack_ref
            or canonical_hash(verified_request.context_pack)
            != verified_request.context_pack_hash
        ):
            raise OwnerConflict("plan_content_request_invalid")
        try:
            evidence_by_ref = validate_plan_context_pack(
                verified_request.context_pack,
                cycle_ref=verified_request.cycle_ref,
                accepted_question_binding=accepted_question.as_dict(),
            )
            evidence_revision = verified_request.context_pack.get(
                "evidence_reference_revision"
            )
            if not isinstance(evidence_revision, int) or isinstance(
                evidence_revision, bool
            ):
                raise PlanContractError("plan_evidence_catalog_invalid")
            reviewed_draft = _resolved_reviewed_draft(
                plan_document,
                review,
                reviewed_draft,
            )
            plan_document_hash = validate_plan_document(
                plan_document,
                question_ref=accepted_question.question_ref,
                idea_set_ref=accepted_idea_set.outcome_ref,
                context_pack_ref=context_pack_ref,
                context_pack_hash=verified_request.context_pack_hash,
                accepted_idea_set=accepted_idea_set.idea_set,
                evidence_by_ref=evidence_by_ref,
                evidence_reference_revision=evidence_revision,
            )
            reviewed_draft_hash = validate_plan_document(
                reviewed_draft,
                question_ref=accepted_question.question_ref,
                idea_set_ref=accepted_idea_set.outcome_ref,
                context_pack_ref=context_pack_ref,
                context_pack_hash=verified_request.context_pack_hash,
                accepted_idea_set=accepted_idea_set.idea_set,
                evidence_by_ref=evidence_by_ref,
                evidence_reference_revision=evidence_revision,
            )
            review_hash = validate_plan_review(
                review,
                reviewed_draft_hash=reviewed_draft_hash,
                final_plan_hash=plan_document_hash,
            )
        except PlanContractError as error:
            raise OwnerConflict(str(error)) from error
        answer_contract = plan_document.get("answer_contract")
        if not isinstance(answer_contract, dict):
            raise OwnerConflict("answer_contract_invalid")
        answer_contract_hash = answer_contract.get("answer_contract_hash")
        if not isinstance(answer_contract_hash, str):
            raise OwnerConflict("answer_contract_invalid")
        payload = {
            "schema_ref": PLAN_ATTEMPT_EXECUTION_SCHEMA,
            "outcome": plan_document,
            "reviewed_draft": reviewed_draft,
            "review": review,
        }
        payload_json = canonical_json(payload)
        payload_hash = canonical_hash(payload)
        self._execution_verifier.verify_attempt_execution_receipt(
            request_ref=request_ref,
            run_ref=run_ref,
            attempt_ref=attempt_ref,
            fence_ref=fence_ref,
            submission_ref=submission_ref,
            payload_hash=payload_hash,
            receipt=execution_receipt,
        )
        plan_document_json = canonical_json(plan_document)
        reviewed_draft_json = canonical_json(reviewed_draft)
        review_json = canonical_json(review)
        object_path = self._store_plan_content(payload_hash, payload_json)
        bindings = {
            "request_ref": request_ref,
            "run_ref": run_ref,
            "attempt_ref": attempt_ref,
            "fence_ref": fence_ref,
            "submission_ref": submission_ref,
            "initialization_id": accepted_question.initialization_id,
            "quest_ref": accepted_question.quest_ref,
            "question_ref": accepted_question.question_ref,
            "context_pack_ref": context_pack_ref,
            "question_content_ref": accepted_question.content_ref,
            "question_content_hash": accepted_question.content_hash,
            "question_content_receipt_ref": (
                accepted_question.content_receipt.receipt_ref
            ),
            "question_content_receipt_hash": (
                accepted_question.content_receipt.payload_hash
            ),
            "question_receipt_ref": (
                accepted_question.question_receipt.receipt_ref
            ),
            "question_receipt_hash": (
                accepted_question.question_receipt.payload_hash
            ),
            "idea_outcome_ref": accepted_idea_set.outcome_ref,
            "idea_content_ref": accepted_idea_set.content_ref,
            "idea_content_hash": accepted_idea_set.payload_hash,
            "idea_content_receipt_ref": (
                accepted_idea_set.content_receipt.receipt_ref
            ),
            "idea_content_receipt_hash": (
                accepted_idea_set.content_receipt.payload_hash
            ),
            "idea_outcome_receipt_ref": (
                accepted_idea_set.outcome_receipt.receipt_ref
            ),
            "idea_outcome_receipt_hash": (
                accepted_idea_set.outcome_receipt.payload_hash
            ),
            "idea_stage_commit_ref": accepted_idea_set.stage_commit_ref,
            "idea_stage_commit_receipt_ref": (
                accepted_idea_set.stage_commit_receipt.receipt_ref
            ),
            "idea_stage_commit_receipt_hash": (
                accepted_idea_set.stage_commit_receipt.payload_hash
            ),
            "plan_document_hash": plan_document_hash,
            "answer_contract_hash": answer_contract_hash,
            "reviewed_draft_hash": reviewed_draft_hash,
            "review_hash": review_hash,
            "payload_hash": payload_hash,
            "execution_receipt_ref": execution_receipt.receipt_ref,
            "execution_receipt_hash": execution_receipt.payload_hash,
        }
        with self._database.write() as connection:
            existing = connection.execute(
                text(
                    "SELECT * FROM rm_plan_documents WHERE submission_ref = "
                    ":submission_ref"
                ),
                {"submission_ref": submission_ref},
            ).first()
            if existing is not None:
                if any(
                    getattr(existing, key) != value
                    for key, value in bindings.items()
                ):
                    raise OwnerConflict("plan_content_acceptance_conflict")
                _verify_plan_object(self._object_store, existing)
                _verify_plan_payload(existing, None)
                if existing.receipt_hash != _plan_content_receipt_hash(existing):
                    raise OwnerConflict("plan_content_receipt_invalid")
                return _accepted_plan_document(existing)

            content_ref = new_ref("plan_content")
            receipt_ref = new_ref("rm_plan_content_receipt")
            receipt_hash = _receipt_hash(
                PLAN_CONTENT_RECEIPT_KIND,
                content_ref,
                bindings,
            )
            accepted_at = time.time()
            connection.execute(
                text(
                    "INSERT INTO rm_plan_documents (content_ref, request_ref, "
                    "run_ref, attempt_ref, fence_ref, submission_ref, "
                    "initialization_id, quest_ref, question_ref, context_pack_ref, "
                    "question_content_ref, question_content_hash, "
                    "question_content_receipt_ref, question_content_receipt_hash, "
                    "question_receipt_ref, question_receipt_hash, "
                    "idea_outcome_ref, idea_content_ref, idea_content_hash, "
                    "idea_content_receipt_ref, idea_content_receipt_hash, "
                    "idea_outcome_receipt_ref, idea_outcome_receipt_hash, "
                    "idea_stage_commit_ref, idea_stage_commit_receipt_ref, "
                    "idea_stage_commit_receipt_hash, plan_document_json, "
                    "plan_document_hash, answer_contract_hash, reviewed_draft_json, "
                    "reviewed_draft_hash, review_json, review_hash, payload_json, "
                    "payload_hash, object_path, execution_receipt_ref, "
                    "execution_receipt_hash, receipt_ref, receipt_hash, accepted_at) "
                    "VALUES (:content_ref, :request_ref, :run_ref, :attempt_ref, "
                    ":fence_ref, :submission_ref, :initialization_id, :quest_ref, "
                    ":question_ref, :context_pack_ref, :question_content_ref, "
                    ":question_content_hash, :question_content_receipt_ref, "
                    ":question_content_receipt_hash, :question_receipt_ref, "
                    ":question_receipt_hash, :idea_outcome_ref, :idea_content_ref, "
                    ":idea_content_hash, :idea_content_receipt_ref, "
                    ":idea_content_receipt_hash, :idea_outcome_receipt_ref, "
                    ":idea_outcome_receipt_hash, :idea_stage_commit_ref, "
                    ":idea_stage_commit_receipt_ref, "
                    ":idea_stage_commit_receipt_hash, :plan_document_json, "
                    ":plan_document_hash, :answer_contract_hash, "
                    ":reviewed_draft_json, :reviewed_draft_hash, :review_json, "
                    ":review_hash, :payload_json, :payload_hash, :object_path, "
                    ":execution_receipt_ref, :execution_receipt_hash, "
                    ":receipt_ref, :receipt_hash, :accepted_at)"
                ),
                {
                    **bindings,
                    "content_ref": content_ref,
                    "plan_document_json": plan_document_json,
                    "reviewed_draft_json": reviewed_draft_json,
                    "review_json": review_json,
                    "payload_json": payload_json,
                    "object_path": object_path,
                    "receipt_ref": receipt_ref,
                    "receipt_hash": receipt_hash,
                    "accepted_at": accepted_at,
                },
            )
            _insert_managed_content_asset(
                connection,
                version_ref=content_ref,
                source_kind="system_artifact",
                display_name="Accepted PlanDocument",
                content_hash=payload_hash,
                content_json=payload_json,
                object_path=object_path,
                provenance={
                    "source_table": "rm_plan_documents",
                    "request_ref": request_ref,
                    "run_ref": run_ref,
                    "submission_ref": submission_ref,
                },
                acceptance_kind=PLAN_CONTENT_RECEIPT_KIND,
                receipt_ref=receipt_ref,
                receipt_hash=receipt_hash,
                accepted_at=accepted_at,
            )
            connection.execute(
                text(
                    "UPDATE research_memory_state SET revision = revision + 1, "
                    "asset_count = (SELECT COUNT(*) FROM rm_assets), "
                    "asset_version_count = (SELECT COUNT(*) FROM "
                    "rm_asset_versions), object_count = :object_count, "
                    "plan_content_count = plan_content_count + 1 "
                    "WHERE singleton = 'owner'"
                ),
                {"object_count": _managed_object_count(connection)},
            )
            self._feed.record(
                connection,
                "research_memory.plan_document_accepted",
                {
                    "request_ref": request_ref,
                    "run_ref": run_ref,
                    "attempt_ref": attempt_ref,
                    "submission_ref": submission_ref,
                    "content_ref": content_ref,
                    "plan_document_hash": plan_document_hash,
                    "receipt_ref": receipt_ref,
                },
            )
        accepted = self.query_plan_document(submission_ref)
        if accepted is None:
            raise OwnerConflict("plan_content_missing_after_commit")
        return accepted

    def query_plan_document(
        self, submission_ref: str
    ) -> AcceptedPlanDocument | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rm_plan_documents WHERE submission_ref = "
                    ":submission_ref"
                ),
                {"submission_ref": submission_ref},
            ).first()
        if row is None:
            return None
        accepted = _accepted_plan_document(row)
        self._receipt_verifier.verify_plan_content_receipt(
            request_ref=row.request_ref,
            submission_ref=row.submission_ref,
            content_ref=row.content_ref,
            payload_hash=row.payload_hash,
            plan_hash=row.plan_document_hash,
            reviewed_draft_hash=row.reviewed_draft_hash,
            review_hash=row.review_hash,
            receipt=accepted.receipt,
        )
        return accepted

    def verify_plan_content_receipt(self, **values) -> None:
        self._receipt_verifier.verify_plan_content_receipt(**values)

    def accept_literature_snapshot(
        self,
        request: DeepFetchRunRequest,
        run: DeepFetchRun,
    ) -> AcceptedLiteratureSnapshot:
        if (
            run.status != "executed"
            or run.request_ref != request.request_ref
            or run.correlation_ref != request.correlation_ref
            or run.attempt_ref is None
            or run.fence_ref is None
            or run.result is None
            or run.result_hash is None
            or run.execution_receipt is None
        ):
            raise OwnerConflict("deepfetch_execution_incomplete")
        result = _validated_literature_result(request, run)
        if self._execution_verifier is None:
            raise OwnerConflict("deepfetch_execution_verifier_unavailable")
        self._execution_verifier.verify_deepfetch_execution_receipt(
            request_ref=request.request_ref,
            run_ref=run.run_ref,
            attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref,
            result_hash=run.result_hash,
            receipt=run.execution_receipt,
        )
        existing = self.query_literature_snapshot_for_request(request.request_ref)
        if existing is not None:
            if (
                existing.result_hash != run.result_hash
                or existing.run_ref != run.run_ref
                or existing.attempt_ref != run.attempt_ref
                or existing.fence_ref != run.fence_ref
            ):
                raise OwnerConflict("literature_snapshot_identity_conflict")
            return existing

        summary_document = {
            "schema_ref": "meta-research/literature-summary/v1",
            "request_ref": request.request_ref,
            "summary": result["summary"],
        }
        if result.get("papers_ledger") is None:
            papers_document = {
                "schema_ref": "meta-research/papers-ledger/v1",
                "request_ref": request.request_ref,
                "papers": result["papers"],
            }
        else:
            papers_document = {
                "schema_ref": "meta-research/papers-ledger/v2",
                "request_ref": request.request_ref,
                "ledger": result["papers_ledger"],
                "display_papers": result["papers"],
            }
        fulltexts_document = {
            "schema_ref": "meta-research/fulltext-collection/v1",
            "request_ref": request.request_ref,
            "fulltexts": result["fulltexts"],
        }
        summary_hash = canonical_hash(summary_document)
        papers_hash = canonical_hash(papers_document)
        fulltexts_hash = canonical_hash(fulltexts_document)
        summary_ref = f"literature_summary_{summary_hash[:32]}"
        papers_ref = f"papers_ledger_{papers_hash[:32]}"
        fulltexts_ref = f"fulltexts_{fulltexts_hash[:32]}"
        summary_path = self._store_literature_object(
            "summary", summary_hash, canonical_json(summary_document)
        )
        papers_path = self._store_literature_object(
            "papers", papers_hash, canonical_json(papers_document)
        )
        fulltexts_path = self._store_literature_object(
            "fulltexts", fulltexts_hash, canonical_json(fulltexts_document)
        )
        limitations = tuple(result["limitations"])
        limitations_json = canonical_json(list(limitations))
        limitations_hash = canonical_hash(list(limitations))
        web_evidence = result["web_evidence"]
        web_evidence_json = canonical_json(web_evidence)
        web_evidence_hash = canonical_hash(web_evidence)
        snapshot_ref = new_ref("literature_snapshot")
        snapshot_binding = {
            "schema_ref": "meta-research/literature-snapshot/v1",
            "snapshot_ref": snapshot_ref,
            "request_ref": request.request_ref,
            "initialization_id": request.initialization_id,
            "draft_revision": request.draft_revision,
            "draft_hash": request.draft_hash,
            "scope_hash": request.scope_hash,
            "run_ref": run.run_ref,
            "attempt_ref": run.attempt_ref,
            "fence_ref": run.fence_ref,
            "result_hash": run.result_hash,
            "completion": result["completion"],
            "summary_ref": summary_ref,
            "summary_hash": summary_hash,
            "papers_ref": papers_ref,
            "papers_hash": papers_hash,
            "fulltexts_ref": fulltexts_ref,
            "fulltexts_hash": fulltexts_hash,
            "limitations_hash": limitations_hash,
            "web_evidence_hash": web_evidence_hash,
        }
        snapshot_hash = canonical_hash(snapshot_binding)
        receipt_ref = new_ref("rm_receipt")
        receipt_hash = _literature_snapshot_receipt_hash(
            snapshot_ref=snapshot_ref,
            snapshot_hash=snapshot_hash,
            request_ref=request.request_ref,
            result_hash=run.result_hash,
            execution_receipt=run.execution_receipt,
        )
        now = time.time()
        with self._database.write() as connection:
            existing_row = connection.execute(
                text(
                    "SELECT * FROM rm_literature_snapshots WHERE "
                    "request_ref = :request_ref"
                ),
                {"request_ref": request.request_ref},
            ).first()
            if existing_row is None:
                connection.execute(
                    text(
                        "INSERT INTO rm_literature_snapshots (snapshot_ref, "
                        "request_ref, initialization_id, draft_revision, draft_hash, "
                        "scope_hash, run_ref, attempt_ref, fence_ref, result_hash, "
                        "execution_receipt_ref, execution_receipt_hash, completion, "
                        "summary_ref, summary_hash, summary_object_path, papers_ref, "
                        "papers_hash, papers_object_path, fulltexts_ref, fulltexts_hash, "
                        "fulltexts_object_path, limitations_json, limitations_hash, "
                        "web_evidence_json, web_evidence_hash, "
                        "snapshot_hash, receipt_ref, receipt_hash, accepted_at) VALUES "
                        "(:snapshot_ref, :request_ref, :initialization_id, "
                        ":draft_revision, :draft_hash, :scope_hash, :run_ref, "
                        ":attempt_ref, :fence_ref, :result_hash, "
                        ":execution_receipt_ref, :execution_receipt_hash, :completion, "
                        ":summary_ref, :summary_hash, :summary_object_path, :papers_ref, "
                        ":papers_hash, :papers_object_path, :fulltexts_ref, "
                        ":fulltexts_hash, :fulltexts_object_path, :limitations_json, "
                        ":limitations_hash, :web_evidence_json, "
                        ":web_evidence_hash, :snapshot_hash, :receipt_ref, "
                        ":receipt_hash, "
                        ":accepted_at)"
                    ),
                    {
                        **snapshot_binding,
                        "execution_receipt_ref": run.execution_receipt.receipt_ref,
                        "execution_receipt_hash": run.execution_receipt.payload_hash,
                        "summary_object_path": summary_path,
                        "papers_object_path": papers_path,
                        "fulltexts_object_path": fulltexts_path,
                        "limitations_json": limitations_json,
                        "web_evidence_json": web_evidence_json,
                        "web_evidence_hash": web_evidence_hash,
                        "snapshot_hash": snapshot_hash,
                        "receipt_ref": receipt_ref,
                        "receipt_hash": receipt_hash,
                        "accepted_at": now,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE research_memory_state SET revision = revision + 1, "
                        "object_count = object_count + 3, literature_snapshot_count = "
                        "literature_snapshot_count + 1 WHERE singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "research_memory.literature_snapshot_accepted",
                    {
                        "snapshot_ref": snapshot_ref,
                        "request_ref": request.request_ref,
                        "snapshot_hash": snapshot_hash,
                        "completion": result["completion"],
                    },
                )
            else:
                snapshot_ref = str(existing_row.snapshot_ref)
        accepted = self.query_literature_snapshot(snapshot_ref)
        if accepted is None:
            raise OwnerConflict("literature_snapshot_not_found")
        return accepted

    def query_literature_snapshot(
        self, snapshot_ref: str
    ) -> AcceptedLiteratureSnapshot | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rm_literature_snapshots WHERE "
                    "snapshot_ref = :snapshot_ref"
                ),
                {"snapshot_ref": snapshot_ref},
            ).first()
        return None if row is None else self._accepted_literature_snapshot(row)

    def query_literature_snapshot_for_request(
        self, request_ref: str
    ) -> AcceptedLiteratureSnapshot | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rm_literature_snapshots WHERE "
                    "request_ref = :request_ref"
                ),
                {"request_ref": request_ref},
            ).first()
        return None if row is None else self._accepted_literature_snapshot(row)

    def query_literature_snapshot_for_basis(
        self, initialization_id: str, draft_revision: int, draft_hash: str
    ) -> AcceptedLiteratureSnapshot | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rm_literature_snapshots WHERE "
                    "initialization_id = :initialization_id AND draft_revision = "
                    ":draft_revision AND draft_hash = :draft_hash"
                ),
                {
                    "initialization_id": initialization_id,
                    "draft_revision": draft_revision,
                    "draft_hash": draft_hash,
                },
            ).first()
        return None if row is None else self._accepted_literature_snapshot(row)

    def verify_literature_snapshot_binding(
        self,
        *,
        snapshot_ref: str,
        snapshot_hash: str,
        initialization_id: str,
        draft_revision: int,
        draft_hash: str,
        receipt: AcceptanceReceipt | None = None,
    ) -> None:
        snapshot = self.query_literature_snapshot(snapshot_ref)
        if snapshot is None or (
            snapshot.snapshot_hash != snapshot_hash
            or snapshot.initialization_id != initialization_id
            or snapshot.draft_revision != draft_revision
            or snapshot.draft_hash != draft_hash
            or receipt is not None
            and snapshot.receipt != receipt
        ):
            raise OwnerConflict("literature_snapshot_binding_invalid")

    def _accepted_literature_snapshot(self, row) -> AcceptedLiteratureSnapshot:
        summary_document = self._read_literature_object(
            row.summary_object_path, row.summary_hash
        )
        papers_document = self._read_literature_object(
            row.papers_object_path, row.papers_hash
        )
        fulltexts_document = self._read_literature_object(
            row.fulltexts_object_path, row.fulltexts_hash
        )
        try:
            limitations = json.loads(row.limitations_json)
            web_evidence = json.loads(row.web_evidence_json)
        except json.JSONDecodeError as error:
            raise OwnerConflict("literature_snapshot_invalid") from error
        if (
            not isinstance(limitations, list)
            or any(not isinstance(value, str) for value in limitations)
            or canonical_json(limitations) != row.limitations_json
            or canonical_hash(limitations) != row.limitations_hash
            or canonical_json(web_evidence) != row.web_evidence_json
            or canonical_hash(web_evidence) != row.web_evidence_hash
            or summary_document.get("request_ref") != row.request_ref
            or papers_document.get("request_ref") != row.request_ref
            or fulltexts_document.get("request_ref") != row.request_ref
            or not isinstance(fulltexts_document.get("fulltexts"), list)
        ):
            raise OwnerConflict("literature_snapshot_invalid")
        paper_count = _validated_stored_papers_document(
            papers_document, str(row.request_ref)
        )
        snapshot_binding = {
            "schema_ref": "meta-research/literature-snapshot/v1",
            "snapshot_ref": row.snapshot_ref,
            "request_ref": row.request_ref,
            "initialization_id": row.initialization_id,
            "draft_revision": int(row.draft_revision),
            "draft_hash": row.draft_hash,
            "scope_hash": row.scope_hash,
            "run_ref": row.run_ref,
            "attempt_ref": row.attempt_ref,
            "fence_ref": row.fence_ref,
            "result_hash": row.result_hash,
            "completion": row.completion,
            "summary_ref": row.summary_ref,
            "summary_hash": row.summary_hash,
            "papers_ref": row.papers_ref,
            "papers_hash": row.papers_hash,
            "fulltexts_ref": row.fulltexts_ref,
            "fulltexts_hash": row.fulltexts_hash,
            "limitations_hash": row.limitations_hash,
            "web_evidence_hash": row.web_evidence_hash,
        }
        execution_receipt = AcceptanceReceipt(
            issuer="agent_runtime",
            kind=DEEPFETCH_EXECUTION_RECEIPT_KIND,
            receipt_ref=row.execution_receipt_ref,
            subject_ref=row.run_ref,
            payload_hash=row.execution_receipt_hash,
        )
        receipt = AcceptanceReceipt(
            issuer=RM_OWNER,
            kind=LITERATURE_SNAPSHOT_RECEIPT_KIND,
            receipt_ref=row.receipt_ref,
            subject_ref=row.snapshot_ref,
            payload_hash=row.receipt_hash,
        )
        if (
            canonical_hash(snapshot_binding) != row.snapshot_hash
            or row.receipt_hash
            != _literature_snapshot_receipt_hash(
                snapshot_ref=row.snapshot_ref,
                snapshot_hash=row.snapshot_hash,
                request_ref=row.request_ref,
                result_hash=row.result_hash,
                execution_receipt=execution_receipt,
            )
        ):
            raise OwnerConflict("literature_snapshot_invalid")
        if self._execution_verifier is not None:
            self._execution_verifier.verify_deepfetch_execution_receipt(
                request_ref=row.request_ref,
                run_ref=row.run_ref,
                attempt_ref=row.attempt_ref,
                fence_ref=row.fence_ref,
                result_hash=row.result_hash,
                receipt=execution_receipt,
            )
        return AcceptedLiteratureSnapshot(
            snapshot_ref=row.snapshot_ref,
            request_ref=row.request_ref,
            initialization_id=row.initialization_id,
            draft_revision=int(row.draft_revision),
            draft_hash=row.draft_hash,
            scope_hash=row.scope_hash,
            run_ref=row.run_ref,
            attempt_ref=row.attempt_ref,
            fence_ref=row.fence_ref,
            result_hash=row.result_hash,
            completion=row.completion,
            summary_ref=row.summary_ref,
            summary_hash=row.summary_hash,
            papers_ref=row.papers_ref,
            papers_hash=row.papers_hash,
            fulltexts_ref=row.fulltexts_ref,
            fulltexts_hash=row.fulltexts_hash,
            limitations=tuple(limitations),
            web_evidence_hash=row.web_evidence_hash,
            snapshot_hash=row.snapshot_hash,
            paper_count=paper_count,
            fulltext_count=len(fulltexts_document["fulltexts"]),
            execution_receipt=execution_receipt,
            receipt=receipt,
        )

    def read_literature_snapshot(self, snapshot_ref: str) -> dict[str, object]:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rm_literature_snapshots WHERE "
                    "snapshot_ref = :snapshot_ref"
                ),
                {"snapshot_ref": snapshot_ref},
            ).first()
        if row is None:
            raise OwnerConflict("literature_snapshot_not_found")
        accepted = self._accepted_literature_snapshot(row)
        summary = self._read_literature_object(
            row.summary_object_path, row.summary_hash
        )["summary"]
        papers_document = self._read_literature_object(
            row.papers_object_path, row.papers_hash
        )
        _validated_stored_papers_document(papers_document, str(row.request_ref))
        if papers_document.get("schema_ref") == "meta-research/papers-ledger/v2":
            papers = papers_document["display_papers"]
            papers_ledger = papers_document["ledger"]
        else:
            papers = papers_document["papers"]
            papers_ledger = None
        fulltexts = self._read_literature_object(
            row.fulltexts_object_path, row.fulltexts_hash
        )["fulltexts"]
        try:
            web_evidence = json.loads(row.web_evidence_json)
        except json.JSONDecodeError as error:
            raise OwnerConflict("literature_snapshot_invalid") from error
        if canonical_hash(web_evidence) != row.web_evidence_hash:
            raise OwnerConflict("literature_snapshot_invalid")
        return {
            **accepted.as_public_dict(),
            "summary": summary,
            "papers": papers,
            "papers_ledger": papers_ledger,
            "fulltexts": fulltexts,
            "web_evidence": web_evidence,
        }

    def _store_literature_object(
        self, kind: str, content_hash: str, content_json: str
    ) -> str:
        directory = self._object_store / "literature-snapshot" / kind / content_hash[:2]
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = directory / f"{content_hash}.json"
        expected_bytes = content_json.encode("utf-8")
        if destination.is_file():
            if destination.read_bytes() != expected_bytes:
                raise OwnerConflict("literature_snapshot_custody_conflict")
        else:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{content_hash}.", dir=directory
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(expected_bytes)
                    output.flush()
                    os.fsync(output.fileno())
                temporary.chmod(0o600)
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
        directory_descriptor = os.open(
            directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        return str(destination.relative_to(self._object_store))

    def _read_literature_object(
        self, object_path: str, expected_hash: str
    ) -> dict[str, object]:
        expected_root = (self._object_store / "literature-snapshot").resolve()
        candidate = (self._object_store / object_path).resolve()
        if not candidate.is_relative_to(expected_root) or not candidate.is_file():
            raise OwnerConflict("literature_snapshot_custody_unavailable")
        try:
            decoded = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise OwnerConflict("literature_snapshot_custody_unavailable") from error
        if not isinstance(decoded, dict) or canonical_hash(decoded) != expected_hash:
            raise OwnerConflict("literature_snapshot_custody_unavailable")
        return decoded

    def _store_content(self, content_hash: str, content_json: str) -> str:
        directory = self._object_store / "formal-question-content" / content_hash[:2]
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = directory / f"{content_hash}.json"
        expected_bytes = content_json.encode("utf-8")
        if destination.is_file():
            if destination.read_bytes() != expected_bytes:
                raise OwnerConflict("question_content_custody_conflict")
        else:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{content_hash}.", dir=directory
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(expected_bytes)
                    output.flush()
                    os.fsync(output.fileno())
                temporary.chmod(0o600)
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
        directory_descriptor = os.open(
            directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        return str(destination.relative_to(self._object_store))

    def _store_idea_content(self, payload_hash: str, payload_json: str) -> str:
        directory = self._object_store / "idea-outcome-content" / payload_hash[:2]
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = directory / f"{payload_hash}.json"
        expected_bytes = payload_json.encode("utf-8")
        if destination.is_file():
            if destination.read_bytes() != expected_bytes:
                raise OwnerConflict("idea_content_custody_conflict")
        else:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{payload_hash}.", dir=directory
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(expected_bytes)
                    output.flush()
                    os.fsync(output.fileno())
                temporary.chmod(0o600)
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
        directory_descriptor = os.open(
            directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        return str(destination.relative_to(self._object_store))

    def _store_plan_content(self, payload_hash: str, payload_json: str) -> str:
        directory = self._object_store / "plan-document-content" / payload_hash[:2]
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = directory / f"{payload_hash}.json"
        expected_bytes = payload_json.encode("utf-8")
        if destination.is_file():
            if destination.read_bytes() != expected_bytes:
                raise OwnerConflict("plan_content_custody_conflict")
        else:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{payload_hash}.", dir=directory
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(expected_bytes)
                    output.flush()
                    os.fsync(output.fileno())
                temporary.chmod(0o600)
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
        directory_descriptor = os.open(
            directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        return str(destination.relative_to(self._object_store))


def _validated_literature_result(
    request: DeepFetchRunRequest,
    run: DeepFetchRun,
) -> dict[str, object]:
    result = run.result
    if result is None or run.result_hash is None:
        raise OwnerConflict("deepfetch_execution_incomplete")
    common_fields = {
        "schema_ref",
        "request_ref",
        "initialization_id",
        "correlation_ref",
        "draft_revision",
        "draft_hash",
        "scope_hash",
        "completion",
        "summary",
        "papers",
        "fulltexts",
        "limitations",
        "native_session_ref",
        "adapter_kind",
        "web_evidence",
    }
    schema_ref = result.get("schema_ref")
    expected_fields = (
        {*common_fields, "papers_ledger"}
        if schema_ref == "meta-research/first-question-deepfetch-result/v2"
        else common_fields
    )
    if (
        set(result) != expected_fields
        or schema_ref
        not in {
            "meta-research/first-question-deepfetch-result/v1",
            "meta-research/first-question-deepfetch-result/v2",
        }
        or result.get("request_ref") != request.request_ref
        or result.get("initialization_id") != request.initialization_id
        or result.get("correlation_ref") != request.correlation_ref
        or result.get("draft_revision") != request.draft_revision
        or result.get("draft_hash") != request.draft_hash
        or result.get("scope_hash") != request.scope_hash
        or result.get("completion") not in {"complete", "limited", "honest_empty"}
        or not isinstance(result.get("summary"), str)
        or not isinstance(result.get("papers"), list)
        or not isinstance(result.get("fulltexts"), list)
        or not isinstance(result.get("limitations"), list)
        or any(not isinstance(value, str) for value in result["limitations"])
        or canonical_hash(result) != run.result_hash
    ):
        raise OwnerConflict("literature_snapshot_payload_invalid")
    paper_fields = {
        "title",
        "url",
        "doi",
        "source_kind",
        "fulltext_status",
        "retrieved_at",
    }
    fulltext_fields = {"paper_url", "media_type", "content", "content_hash"}
    papers = result["papers"]
    fulltexts = result["fulltexts"]
    if any(
        not isinstance(value, dict) or set(value) != paper_fields
        for value in papers
    ) or any(
        not isinstance(value, dict) or set(value) != fulltext_fields
        for value in fulltexts
    ):
        raise OwnerConflict("literature_snapshot_payload_invalid")
    if schema_ref == "meta-research/first-question-deepfetch-result/v2":
        ledger = result.get("papers_ledger")
        if ledger is not None:
            _validated_v4_ledger(ledger, expected_count=len(papers))
    for value in fulltexts:
        assert isinstance(value, dict)
        if value.get("content_hash") != canonical_hash(
            {
                "media_type": value.get("media_type"),
                "content": value.get("content"),
            }
        ):
            raise OwnerConflict("literature_snapshot_payload_invalid")
    return result


def _validated_stored_papers_document(
    value: dict[str, object], request_ref: str
) -> int:
    schema_ref = value.get("schema_ref")
    if schema_ref == "meta-research/papers-ledger/v1":
        if (
            set(value) != {"schema_ref", "request_ref", "papers"}
            or value.get("request_ref") != request_ref
            or not isinstance(value.get("papers"), list)
        ):
            raise OwnerConflict("literature_snapshot_invalid")
        return len(value["papers"])
    if schema_ref == "meta-research/papers-ledger/v2":
        display = value.get("display_papers")
        if (
            set(value)
            != {"schema_ref", "request_ref", "ledger", "display_papers"}
            or value.get("request_ref") != request_ref
            or not isinstance(display, list)
        ):
            raise OwnerConflict("literature_snapshot_invalid")
        return _validated_v4_ledger(value.get("ledger"), expected_count=len(display))
    raise OwnerConflict("literature_snapshot_invalid")


def _validated_v4_ledger(value: object, *, expected_count: int) -> int:
    required = {
        "schema_version",
        "topic",
        "run",
        "paper_order",
        "papers",
        "missing_fulltexts",
        "limitations",
    }
    if not isinstance(value, dict):
        raise OwnerConflict("literature_snapshot_payload_invalid")
    paper_order = value.get("paper_order")
    papers = value.get("papers")
    if (
        set(value) != required
        or value.get("schema_version") != "deepfetch.papers.v4"
        or not isinstance(paper_order, list)
        or any(not isinstance(item, str) or not item for item in paper_order)
        or len(set(paper_order)) != len(paper_order)
        or len(paper_order) != expected_count
        or not isinstance(papers, dict)
        or set(papers) != set(paper_order)
    ):
        raise OwnerConflict("literature_snapshot_payload_invalid")
    return len(paper_order)


def _literature_snapshot_receipt_hash(
    *,
    snapshot_ref: str,
    snapshot_hash: str,
    request_ref: str,
    result_hash: str,
    execution_receipt: AcceptanceReceipt,
) -> str:
    return canonical_hash(
        {
            "schema_ref": RECEIPT_SCHEMA,
            "issuer": RM_OWNER,
            "kind": LITERATURE_SNAPSHOT_RECEIPT_KIND,
            "subject_ref": snapshot_ref,
            "bindings": {
                "snapshot_hash": snapshot_hash,
                "request_ref": request_ref,
                "result_hash": result_hash,
                "execution_receipt": execution_receipt.as_public_dict(),
            },
        }
    )


def _asset_request_document(request: AssetIntakeRequest) -> dict[str, object]:
    if request.source_kind not in {
        "text",
        "file",
        "directory",
        "local_path",
        "repository",
        "link",
        "system_artifact",
    }:
        raise OwnerConflict("asset_source_kind_invalid")
    if request.custody_mode not in {"managed", "linked_local"}:
        raise OwnerConflict("asset_custody_mode_invalid")
    display_name = request.display_name.strip()
    if (
        not display_name
        or len(display_name) > 512
        or not _valid_portable_asset_name(display_name)
    ):
        raise OwnerConflict("asset_display_name_invalid")
    media_type = request.media_type.strip()
    if (
        not media_type
        or len(media_type) > 255
        or not _valid_media_type(media_type)
    ):
        raise OwnerConflict("asset_media_type_invalid")
    if request.content is not None and not isinstance(request.content, bytes):
        raise OwnerConflict("asset_content_invalid")
    if request.content is not None and len(request.content) > MAX_ASSET_BYTES:
        raise OwnerConflict("asset_content_too_large")
    source_locator = request.source_locator
    if source_locator is not None:
        source_locator = source_locator.strip()
        if not source_locator or "\x00" in source_locator:
            raise OwnerConflict("asset_source_locator_invalid")
        if request.source_kind != "link":
            if not os.path.isabs(source_locator):
                raise OwnerConflict("asset_source_locator_absolute_required")
            # Admission must remain a pure operation. Resolving even a missing
            # path can perform lstat calls against every prefix, so a stalled
            # mount would otherwise block the event loop before the durable
            # intake job and its filesystem-I/O watchdog exist.
            source_locator = os.path.normpath(source_locator)
    has_content = request.content is not None
    has_locator = source_locator is not None
    if has_content and has_locator:
        raise OwnerConflict("asset_source_payload_ambiguous")
    if request.source_kind in {"text", "file"}:
        if not has_content or has_locator:
            raise OwnerConflict("asset_content_required")
        if request.custody_mode != "managed":
            raise OwnerConflict("asset_source_custody_invalid")
    elif request.source_kind in {
        "directory",
        "local_path",
        "repository",
        "system_artifact",
    }:
        if has_content or not has_locator:
            raise OwnerConflict("asset_source_locator_required")
    elif request.source_kind == "link":
        if has_content or not has_locator:
            raise OwnerConflict("asset_source_locator_required")
        if request.custody_mode != "managed":
            raise OwnerConflict("asset_source_custody_invalid")
    provenance = request.provenance or {}
    if not isinstance(provenance, dict):
        raise OwnerConflict("asset_provenance_invalid")
    try:
        provenance_json = canonical_json(provenance)
    except (TypeError, ValueError) as error:
        raise OwnerConflict("asset_provenance_invalid") from error
    if (
        len(provenance_json.encode("utf-8")) > MAX_ASSET_PROVENANCE_BYTES
        or not _bounded_json_shape(
            provenance,
            max_depth=MAX_ASSET_PROVENANCE_DEPTH,
            max_nodes=MAX_ASSET_PROVENANCE_NODES,
        )
    ):
        raise OwnerConflict("asset_provenance_too_large")
    asset_ref = request.asset_ref
    if asset_ref is not None and (
        not asset_ref.strip() or len(asset_ref) > 64 or "\x00" in asset_ref
    ):
        raise OwnerConflict("asset_ref_invalid")
    return {
        "source_kind": request.source_kind,
        "custody_mode": request.custody_mode,
        "display_name": display_name,
        "media_type": media_type,
        "content_base64": (
            None
            if request.content is None
            else base64.b64encode(request.content).decode("ascii")
        ),
        "source_locator": source_locator,
        "provenance": provenance,
        "asset_ref": None if asset_ref is None else asset_ref.strip(),
        "asynchronous": bool(request.asynchronous),
    }


def _validated_stored_asset_request(
    stored_request_json: str, stored_request_hash: str
) -> dict[str, object]:
    try:
        _verify_stored_asset_request_binding(
            stored_request_json, stored_request_hash
        )
        document = decoded_object(stored_request_json)
        if (
            canonical_json(document) != stored_request_json
            or set(document)
            != {
                "source_kind",
                "custody_mode",
                "display_name",
                "media_type",
                "content_base64",
                "source_locator",
                "provenance",
                "asset_ref",
                "asynchronous",
            }
        ):
            raise ValueError("durable asset request binding mismatch")
        encoded = document["content_base64"]
        if encoded is None:
            content = None
        elif isinstance(encoded, str):
            content = base64.b64decode(encoded, validate=True)
        else:
            raise ValueError("durable asset content encoding invalid")
        if not isinstance(document["asynchronous"], bool):
            raise ValueError("durable asset request mode invalid")
        request = AssetIntakeRequest(
            source_kind=document["source_kind"],
            custody_mode=document["custody_mode"],
            display_name=document["display_name"],
            media_type=document["media_type"],
            content=content,
            source_locator=document["source_locator"],
            provenance=document["provenance"],
            asset_ref=document["asset_ref"],
            asynchronous=document["asynchronous"],
        )
        normalized = _asset_request_document(request)
        if normalized != document:
            raise ValueError("durable asset request is not canonical")
        return normalized
    except (KeyError, TypeError, ValueError, OwnerConflict) as error:
        raise OwnerConflict("asset_intake_request_invalid") from error


def _scrubbed_asset_request_json(
    source_kind: object, custody_mode: object
) -> str:
    return canonical_json(
        {
            "source_kind": (
                source_kind if isinstance(source_kind, str) else "unknown"
            ),
            "custody_mode": (
                custody_mode if isinstance(custody_mode, str) else "unknown"
            ),
            "payload_scrubbed": True,
        }
    )


def _verified_scrubbed_asset_request_summary(row: object) -> dict[str, object]:
    source_kind = getattr(row, "request_source_kind", None)
    custody_mode = getattr(row, "request_custody_mode", None)
    expected_json = _scrubbed_asset_request_json(source_kind, custody_mode)
    stored_json = getattr(row, "request_json", None)
    if stored_json != expected_json:
        raise OwnerConflict("asset_intake_request_invalid")
    try:
        summary = decoded_object(stored_json)
    except (TypeError, ValueError) as error:
        raise OwnerConflict("asset_intake_request_invalid") from error
    if not isinstance(summary, dict):
        raise OwnerConflict("asset_intake_request_invalid")
    return summary


def _bounded_json_shape(
    value: object, *, max_depth: int, max_nodes: int
) -> bool:
    stack: list[tuple[object, int]] = [(value, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > max_nodes or depth > max_depth:
            return False
        if isinstance(current, dict):
            if not all(isinstance(key, str) for key in current):
                return False
            nodes += len(current)
            if nodes > max_nodes:
                return False
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, (list, tuple)):
            stack.extend((item, depth + 1) for item in current)
    return True


def _verify_stored_asset_request_binding(
    stored_request_json: str, stored_request_hash: str
) -> None:
    if (
        not isinstance(stored_request_json, str)
        or not isinstance(stored_request_hash, str)
        or hashlib.sha256(stored_request_json.encode("utf-8")).hexdigest()
        != stored_request_hash
    ):
        raise OwnerConflict("asset_intake_request_invalid")


def _valid_media_type(value: str) -> bool:
    if any(
        not character.isascii()
        or ord(character) < 32
        or ord(character) == 127
        for character in value
    ):
        return False
    pieces = value.split(";")
    type_parts = pieces[0].strip().split("/")
    if len(type_parts) != 2 or not all(_http_token(part) for part in type_parts):
        return False
    for parameter in pieces[1:]:
        if "=" not in parameter:
            return False
        name, raw_value = parameter.split("=", 1)
        name = name.strip()
        raw_value = raw_value.strip()
        if not _http_token(name):
            return False
        if _http_token(raw_value):
            continue
        if not _http_quoted_string(raw_value):
            return False
    return True


def _http_token(value: str) -> bool:
    punctuation = "!#$%&'*+-.^_`|~"
    return bool(value) and all(
        character.isascii()
        and (character.isalnum() or character in punctuation)
        for character in value
    )


def _http_quoted_string(value: str) -> bool:
    if len(value) < 2 or value[0] != '"' or value[-1] != '"':
        return False
    escaped = False
    for character in value[1:-1]:
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == '"':
            return False
    return not escaped


def _safe_asset_name(display_name: str) -> str:
    name = Path(display_name.replace("\\", "/")).name.strip()
    if not name or name in {".", ".."}:
        return "asset.bin"
    return name


def _valid_portable_asset_name(value: str) -> bool:
    return "/" not in value and _valid_portable_asset_path(value)


def _valid_portable_asset_path(value: str) -> bool:
    if not value or "\\" in value or "\x00" in value:
        return False
    components = value.split("/")
    return (
        not PureWindowsPath(value).drive
        and all(_valid_portable_asset_component(component) for component in components)
    )


def _valid_portable_asset_component(value: str) -> bool:
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return (
        value not in {"", ".", ".."}
        and value == value.rstrip(" .")
        and not any(character in '<>:"|?*' for character in value)
        and not any(ord(character) < 32 for character in value)
        and value.split(".", 1)[0].upper() not in reserved
    )


def _read_directory_files(
    source: Path, *, ignored_top_level: tuple[str, ...] = ()
) -> tuple[tuple[str, ...], tuple[tuple[str, bytes], ...]]:
    directories: list[str] = []
    files: list[tuple[str, bytes]] = []
    total_bytes = 0
    entry_count = 0
    for root, directory_names, file_names in os.walk(
        source,
        topdown=True,
        onerror=_raise_asset_walk_error,
        followlinks=False,
    ):
        root_path = Path(root)
        retained_directories: list[str] = []
        portable_names: set[str] = set()
        for name in sorted(directory_names):
            candidate = root_path / name
            if name in ignored_top_level:
                continue
            if candidate.is_symlink():
                raise OwnerConflict("asset_source_symlink_unsupported")
            portable_key = unicodedata.normalize("NFC", name).casefold()
            if portable_key in portable_names:
                raise OwnerConflict("asset_source_entry_unsupported")
            portable_names.add(portable_key)
            retained_directories.append(name)
            relative_directory = candidate.relative_to(source).as_posix()
            if not _valid_portable_asset_path(relative_directory):
                raise OwnerConflict("asset_source_entry_unsupported")
            directories.append(relative_directory)
        directory_names[:] = retained_directories
        entry_count += len(retained_directories)
        if entry_count > MAX_ASSET_FILES:
            raise OwnerConflict("asset_source_too_large")
        for name in sorted(file_names):
            if name in ignored_top_level:
                continue
            candidate = root_path / name
            if candidate.is_symlink():
                raise OwnerConflict("asset_source_symlink_unsupported")
            if not candidate.is_file():
                raise OwnerConflict("asset_source_entry_unsupported")
            portable_key = unicodedata.normalize("NFC", name).casefold()
            if portable_key in portable_names:
                raise OwnerConflict("asset_source_entry_unsupported")
            portable_names.add(portable_key)
            entry_count += 1
            try:
                size = candidate.stat().st_size
            except OSError as error:
                raise OwnerConflict("asset_source_unavailable") from error
            if entry_count > MAX_ASSET_FILES or size > MAX_ASSET_BYTES - total_bytes:
                raise OwnerConflict("asset_source_too_large")
            content = _read_exact_file(
                candidate,
                size,
                unavailable_code="asset_source_unavailable",
                mismatch_code="asset_source_changed_during_intake",
            )
            relative_path = candidate.relative_to(source).as_posix()
            if not _valid_portable_asset_path(relative_path):
                raise OwnerConflict("asset_source_entry_unsupported")
            files.append((relative_path, content))
            total_bytes += len(content)
    directories.sort()
    files.sort(key=lambda item: item[0])
    return tuple(directories), tuple(files)


def _read_bounded_source_file(source: Path) -> bytes:
    try:
        source_stat = source.stat()
    except OSError as error:
        raise OwnerConflict("asset_source_unavailable") from error
    if not stat.S_ISREG(source_stat.st_mode):
        raise OwnerConflict("asset_source_entry_unsupported")
    size = source_stat.st_size
    if size > MAX_ASSET_BYTES:
        raise OwnerConflict("asset_source_too_large")
    return _read_exact_file(
        source,
        size,
        unavailable_code="asset_source_unavailable",
        mismatch_code="asset_source_changed_during_intake",
    )


def _scan_directory_content(
    source: Path, *, ignored_top_level: tuple[str, ...] = ()
) -> tuple[list[str], list[dict[str, object]]]:
    directories: list[str] = []
    entries: list[dict[str, object]] = []
    total_bytes = 0
    entry_count = 0
    for root, directory_names, file_names in os.walk(
        source,
        topdown=True,
        onerror=_raise_asset_walk_error,
        followlinks=False,
    ):
        root_path = Path(root)
        retained_directories: list[str] = []
        for name in sorted(directory_names):
            candidate = root_path / name
            if name in ignored_top_level:
                continue
            if candidate.is_symlink():
                raise OwnerConflict("asset_source_symlink_unsupported")
            retained_directories.append(name)
            relative_directory = candidate.relative_to(source).as_posix()
            if not _valid_portable_asset_path(relative_directory):
                raise OwnerConflict("asset_source_entry_unsupported")
            directories.append(relative_directory)
        directory_names[:] = retained_directories
        entry_count += len(retained_directories)
        if entry_count > MAX_ASSET_FILES:
            raise OwnerConflict("asset_source_too_large")
        for name in sorted(file_names):
            if name in ignored_top_level:
                continue
            candidate = root_path / name
            if candidate.is_symlink():
                raise OwnerConflict("asset_source_symlink_unsupported")
            if not candidate.is_file():
                raise OwnerConflict("asset_source_entry_unsupported")
            entry_count += 1
            try:
                size = candidate.stat().st_size
            except OSError as error:
                raise OwnerConflict("asset_source_unavailable") from error
            if entry_count > MAX_ASSET_FILES or size > MAX_ASSET_BYTES - total_bytes:
                raise OwnerConflict("asset_source_too_large")
            entries.append(
                {
                    "path": candidate.relative_to(source).as_posix(),
                    "sha256": _sha256_exact_file(
                        candidate,
                        size,
                        unavailable_code="asset_source_unavailable",
                        mismatch_code="asset_source_changed_during_intake",
                    ),
                    "size": size,
                }
            )
            if not _valid_portable_asset_path(str(entries[-1]["path"])):
                raise OwnerConflict("asset_source_entry_unsupported")
            total_bytes += size
    directories.sort()
    entries.sort(key=lambda entry: str(entry["path"]))
    return directories, entries


def _raise_asset_walk_error(error: OSError) -> None:
    raise OwnerConflict("asset_source_unavailable") from error


def _file_matches(path: Path, expected_size: int, expected_hash: str) -> bool:
    try:
        return (
            _sha256_exact_file(
                path,
                expected_size,
                unavailable_code="asset_custody_unavailable",
                mismatch_code="asset_custody_unavailable",
            )
            == expected_hash
        )
    except (OSError, OwnerConflict):
        return False


def _sha256_exact_file(
    path: Path,
    expected_size: int,
    *,
    unavailable_code: str,
    mismatch_code: str,
) -> str:
    digest = hashlib.sha256()
    try:
        with _open_asset_regular_file(path, unavailable_code) as source:
            if os.fstat(source.fileno()).st_size != expected_size:
                raise OwnerConflict(mismatch_code)
            remaining = expected_size
            while remaining:
                chunk = source.read(min(ASSET_HASH_CHUNK_BYTES, remaining))
                if not chunk:
                    raise OwnerConflict(mismatch_code)
                remaining -= len(chunk)
                digest.update(chunk)
            if source.read(1) or os.fstat(source.fileno()).st_size != expected_size:
                raise OwnerConflict(mismatch_code)
    except OwnerConflict:
        raise
    except OSError as error:
        raise OwnerConflict(unavailable_code) from error
    return digest.hexdigest()


def _read_exact_file(
    path: Path,
    expected_size: int,
    expected_hash: str | None = None,
    *,
    unavailable_code: str,
    mismatch_code: str,
) -> bytes:
    digest = hashlib.sha256() if expected_hash is not None else None
    chunks: list[bytes] = []
    try:
        with _open_asset_regular_file(path, unavailable_code) as source:
            if os.fstat(source.fileno()).st_size != expected_size:
                raise OwnerConflict(mismatch_code)
            remaining = expected_size
            while remaining:
                chunk = source.read(min(ASSET_HASH_CHUNK_BYTES, remaining))
                if not chunk:
                    raise OwnerConflict(mismatch_code)
                remaining -= len(chunk)
                chunks.append(chunk)
                if digest is not None:
                    digest.update(chunk)
            if source.read(1) or os.fstat(source.fileno()).st_size != expected_size:
                raise OwnerConflict(mismatch_code)
    except OwnerConflict:
        raise
    except OSError as error:
        raise OwnerConflict(unavailable_code) from error
    content = b"".join(chunks)
    if digest is not None and digest.hexdigest() != expected_hash:
        raise OwnerConflict(mismatch_code)
    return content


def _open_asset_regular_file(path: Path, unavailable_code: str):
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OwnerConflict("asset_source_entry_unsupported")
        source = os.fdopen(descriptor, "rb")
        descriptor = None
        return source
    except OwnerConflict:
        raise
    except OSError as error:
        raise OwnerConflict(unavailable_code) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _accepted_asset(
    row, custodies, *, projection: bool = False
) -> AcceptedAssetVersion:
    manifest, provenance = _verify_asset_metadata(
        row,
        custodies,
        require_portable_paths=False,
        summarize_oversized_provenance=projection,
    )
    _verify_asset_custody_rows(row, custodies)
    if row.acceptance_kind == ASSET_RECEIPT_KIND and (
        row.receipt_hash != _asset_receipt_hash(row, custodies)
    ):
        raise OwnerConflict("asset_receipt_invalid")
    return AcceptedAssetVersion(
        asset_ref=row.asset_ref,
        version_ref=row.version_ref,
        memory_ref=row.version_ref,
        version_number=int(row.version_number),
        source_kind=row.source_kind,
        display_name=row.display_name,
        media_type=row.media_type,
        content_hash=row.content_hash,
        manifest_hash=row.manifest_hash,
        byte_count=int(row.byte_count),
        provenance=provenance,
        custody_modes=tuple(sorted(custody.custody_mode for custody in custodies)),
        accepted_at=float(row.accepted_at),
        receipt=AcceptanceReceipt(
            issuer=RM_OWNER,
            kind=row.acceptance_kind,
            receipt_ref=row.receipt_ref,
            subject_ref=row.version_ref,
            payload_hash=row.receipt_hash,
        ),
    )


def _accepted_asset_hold(row) -> AcceptedAssetHold:
    placement_hash = _receipt_hash(
        ASSET_HOLD_PLACED_RECEIPT_KIND,
        row.hold_ref,
        {"version_ref": row.version_ref, "reason": row.reason},
    )
    if row.receipt_hash != placement_hash:
        raise OwnerConflict("asset_hold_receipt_invalid")
    release_receipt: AcceptanceReceipt | None = None
    if bool(row.active):
        if any(
            value is not None
            for value in (
                row.released_at,
                row.release_receipt_ref,
                row.release_receipt_hash,
            )
        ):
            raise OwnerConflict("asset_hold_receipt_invalid")
    else:
        if (
            row.released_at is None
            or row.release_receipt_ref is None
            or row.release_receipt_hash is None
        ):
            raise OwnerConflict("asset_hold_receipt_invalid")
        expected_release_hash = _receipt_hash(
            ASSET_HOLD_RELEASED_RECEIPT_KIND,
            row.hold_ref,
            {
                "version_ref": row.version_ref,
                "placement_receipt_ref": row.receipt_ref,
                "placement_receipt_hash": row.receipt_hash,
                "released_at": float(row.released_at),
            },
        )
        if row.release_receipt_hash != expected_release_hash:
            raise OwnerConflict("asset_hold_receipt_invalid")
        release_receipt = AcceptanceReceipt(
            issuer=RM_OWNER,
            kind=ASSET_HOLD_RELEASED_RECEIPT_KIND,
            receipt_ref=row.release_receipt_ref,
            subject_ref=row.hold_ref,
            payload_hash=row.release_receipt_hash,
        )
    return AcceptedAssetHold(
        hold_ref=row.hold_ref,
        version_ref=row.version_ref,
        reason=row.reason,
        active=bool(row.active),
        placed_at=float(row.placed_at),
        released_at=(None if row.released_at is None else float(row.released_at)),
        placement_receipt=AcceptanceReceipt(
            issuer=RM_OWNER,
            kind=ASSET_HOLD_PLACED_RECEIPT_KIND,
            receipt_ref=row.receipt_ref,
            subject_ref=row.hold_ref,
            payload_hash=row.receipt_hash,
        ),
        release_receipt=release_receipt,
    )


def _accepted_release_assessment(row) -> ReleaseEligibilityAssessment:
    try:
        reference_refs = json.loads(row.active_reference_refs_json)
        hold_refs = json.loads(row.active_hold_refs_json)
        reason_codes = json.loads(row.reason_codes_json)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise OwnerConflict("release_eligibility_receipt_invalid") from error
    for values in (reference_refs, hold_refs, reason_codes):
        if (
            not isinstance(values, list)
            or not all(isinstance(item, str) and item for item in values)
            or values != list(dict.fromkeys(values))
        ):
            raise OwnerConflict("release_eligibility_receipt_invalid")
    bindings = {
        "version_ref": row.version_ref,
        "expected_reference_revision": row.expected_reference_revision,
        "observed_reference_revision": row.observed_reference_revision,
        "active_reference_refs": reference_refs,
        "active_hold_refs": hold_refs,
        "eligible": bool(row.eligible),
        "reason_codes": reason_codes,
    }
    if (
        canonical_json(reference_refs) != row.active_reference_refs_json
        or canonical_hash(reference_refs) != row.active_reference_refs_hash
        or canonical_json(hold_refs) != row.active_hold_refs_json
        or canonical_hash(hold_refs) != row.active_hold_refs_hash
        or canonical_json(reason_codes) != row.reason_codes_json
        or canonical_hash(reason_codes) != row.reason_codes_hash
        or bool(row.eligible) == bool(reason_codes)
        or row.receipt_hash
        != _receipt_hash(
            RELEASE_ELIGIBILITY_RECEIPT_KIND,
            row.assessment_ref,
            bindings,
        )
    ):
        raise OwnerConflict("release_eligibility_receipt_invalid")
    return ReleaseEligibilityAssessment(
        assessment_ref=row.assessment_ref,
        version_ref=row.version_ref,
        expected_reference_revision=row.expected_reference_revision,
        observed_reference_revision=row.observed_reference_revision,
        active_reference_refs=tuple(reference_refs),
        active_hold_refs=tuple(hold_refs),
        eligible=bool(row.eligible),
        reason_codes=tuple(reason_codes),
        assessed_at=float(row.assessed_at),
        receipt=AcceptanceReceipt(
            issuer=RM_OWNER,
            kind=RELEASE_ELIGIBILITY_RECEIPT_KIND,
            receipt_ref=row.receipt_ref,
            subject_ref=row.assessment_ref,
            payload_hash=row.receipt_hash,
        ),
    )


def _verify_asset_metadata(
    row,
    custodies,
    *,
    require_portable_paths: bool = True,
    summarize_oversized_provenance: bool = False,
) -> tuple[dict[str, object], dict[str, object]]:
    try:
        manifest = decoded_object(row.manifest_json)
    except (TypeError, ValueError) as error:
        raise OwnerConflict("asset_metadata_invalid") from error
    provenance_bytes = row.provenance_json.encode("utf-8")
    if (
        summarize_oversized_provenance
        and len(provenance_bytes) > MAX_ASSET_PROVENANCE_BYTES
    ):
        if hashlib.sha256(provenance_bytes).hexdigest() != row.provenance_hash:
            raise OwnerConflict("asset_metadata_invalid")
        provenance = {
            "status": "legacy_oversized",
            "provenance_hash": row.provenance_hash,
        }
        provenance_is_summarized = True
    else:
        try:
            provenance = decoded_object(row.provenance_json)
        except (TypeError, ValueError) as error:
            raise OwnerConflict("asset_metadata_invalid") from error
        provenance_is_summarized = False
    if (
        canonical_json(manifest) != row.manifest_json
        or canonical_hash(manifest) != row.manifest_hash
        or (
            not provenance_is_summarized
            and (
                canonical_json(provenance) != row.provenance_json
                or canonical_hash(provenance) != row.provenance_hash
            )
        )
        or manifest.get("schema_ref") != ASSET_MANIFEST_SCHEMA
        or manifest.get("kind") not in {"file", "directory"}
        or not isinstance(manifest.get("entries"), list)
        or int(row.version_number) < 1
        or int(row.byte_count) < 0
        or len(row.content_hash) != 64
        or not custodies
    ):
        raise OwnerConflict("asset_metadata_invalid")
    entries = manifest["entries"]
    directories_recorded = "directories" in manifest
    directories = manifest.get("directories", [])
    if manifest["kind"] == "file":
        if directories not in (None, []):
            raise OwnerConflict("asset_manifest_invalid")
        directories = []
    if (
        not isinstance(directories, list)
        or not all(
            isinstance(path, str)
            and (
                not require_portable_paths
                or _valid_portable_asset_path(path)
            )
            for path in directories
        )
        or directories != sorted(set(directories))
    ):
        raise OwnerConflict("asset_manifest_invalid")
    total = 0
    paths: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "path",
            "sha256",
            "size",
            "object_path",
        }:
            raise OwnerConflict("asset_manifest_invalid")
        path = entry["path"]
        digest = entry["sha256"]
        size = entry["size"]
        object_path = entry["object_path"]
        if (
            not isinstance(path, str)
            or (
                require_portable_paths
                and not _valid_portable_asset_path(path)
            )
            or path in paths
            or not isinstance(digest, str)
            or len(digest) != 64
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or (object_path is not None and not isinstance(object_path, str))
        ):
            raise OwnerConflict("asset_manifest_invalid")
        paths.add(path)
        total += size
    if total != int(row.byte_count):
        raise OwnerConflict("asset_manifest_invalid")
    content_manifest = {
        "kind": manifest["kind"],
        "entries": [
            {
                "path": entry["path"],
                "sha256": entry["sha256"],
                "size": entry["size"],
            }
            for entry in entries
        ],
    }
    if directories_recorded:
        content_manifest["directories"] = directories
    expected_content_hash = (
        str(entries[0]["sha256"])
        if manifest["kind"] == "file" and len(entries) == 1
        else canonical_hash(content_manifest)
    )
    if expected_content_hash != row.content_hash:
        raise OwnerConflict("asset_manifest_invalid")
    modes: set[str] = set()
    for custody in custodies:
        if (
            custody.version_ref != row.version_ref
            or custody.custody_mode not in {"managed", "linked_local"}
            or custody.custody_mode in modes
            or len(custody.receipt_hash) != 64
        ):
            raise OwnerConflict("asset_custody_invalid")
        modes.add(custody.custody_mode)
    return manifest, provenance


def _asset_receipt_hash(row, custodies) -> str:
    initial_custody_modes = sorted(
        custody.custody_mode
        for custody in custodies
        if custody.receipt_kind == ASSET_CUSTODY_ESTABLISHED_RECEIPT_KIND
        or (
            custody.receipt_ref == row.receipt_ref
            and custody.receipt_hash == row.receipt_hash
        )
    )
    return _receipt_hash(
        ASSET_RECEIPT_KIND,
        row.version_ref,
        {
            "asset_ref": row.asset_ref,
            "version_number": int(row.version_number),
            "source_kind": row.source_kind,
            "display_name": row.display_name,
            "media_type": row.media_type,
            "content_hash": row.content_hash,
            "manifest_hash": row.manifest_hash,
            "byte_count": int(row.byte_count),
            "provenance_hash": row.provenance_hash,
            "custody_modes": initial_custody_modes,
        },
    )


def _verify_asset_custody_rows(row, custodies) -> None:
    for custody in custodies:
        locator_binding_values = (
            custody.locator_binding_kind,
            custody.locator_binding_ref,
            custody.locator_binding_hash,
            custody.locator_binding_request_hash,
            custody.locator_bound_at,
        )
        if any(value is not None for value in locator_binding_values):
            if (
                not all(value is not None for value in locator_binding_values)
                or custody.locator_binding_kind
                != ASSET_CUSTODY_LOCATOR_MIGRATED_RECEIPT_KIND
                or not isinstance(custody.source_locator, str)
                or not custody.source_locator
                or not isinstance(custody.locator_binding_ref, str)
                or len(custody.locator_binding_ref) > 64
                or not isinstance(custody.locator_binding_hash, str)
                or len(custody.locator_binding_hash) != 64
                or not isinstance(
                    custody.locator_binding_request_hash, str
                )
                or len(custody.locator_binding_request_hash) != 64
                or not isinstance(custody.locator_bound_at, (int, float))
                or isinstance(custody.locator_bound_at, bool)
                or not math.isfinite(float(custody.locator_bound_at))
                or custody.locator_binding_hash
                != _receipt_hash(
                    ASSET_CUSTODY_LOCATOR_MIGRATED_RECEIPT_KIND,
                    custody.custody_ref,
                    {
                        "version_ref": row.version_ref,
                        "content_hash": row.content_hash,
                        "manifest_hash": row.manifest_hash,
                        "custody_mode": custody.custody_mode,
                        "source_locator": custody.source_locator,
                        "request_hash": custody.locator_binding_request_hash,
                        "prior_receipt_ref": custody.receipt_ref,
                        "prior_receipt_hash": custody.receipt_hash,
                        "bound_at": float(custody.locator_bound_at),
                    },
                )
            ):
                raise OwnerConflict("asset_custody_receipt_invalid")
        if custody.custody_mode == "linked_local" and (
            not isinstance(custody.source_locator, str)
            or not custody.source_locator
        ):
            raise OwnerConflict("asset_custody_receipt_invalid")

        if custody.receipt_kind in {
            ASSET_CUSTODY_ESTABLISHED_RECEIPT_KIND,
            ASSET_CUSTODY_RECEIPT_KIND,
        }:
            if (
                custody.receipt_kind == ASSET_CUSTODY_RECEIPT_KIND
                and (
                    custody.custody_mode != "managed"
                    or custody.source_locator is not None
                )
            ):
                raise OwnerConflict("asset_custody_receipt_invalid")
            bindings = {
                "version_ref": row.version_ref,
                "content_hash": row.content_hash,
                "manifest_hash": row.manifest_hash,
                "custody_mode": custody.custody_mode,
            }
            if custody.receipt_kind == ASSET_CUSTODY_ESTABLISHED_RECEIPT_KIND:
                bindings["source_locator"] = custody.source_locator
            if custody.receipt_hash != _receipt_hash(
                custody.receipt_kind,
                custody.custody_ref,
                bindings,
            ):
                raise OwnerConflict("asset_custody_receipt_invalid")
            continue
        if (
            custody.receipt_kind != row.acceptance_kind
            or custody.receipt_ref != row.receipt_ref
            or custody.receipt_hash != row.receipt_hash
            or (
                custody.custody_mode == "linked_local"
                and (
                    not isinstance(custody.source_locator, str)
                    or not custody.source_locator
                )
            )
        ):
            raise OwnerConflict("asset_custody_receipt_invalid")


def _asset_current_state(
    object_store: Path, row, custodies
) -> tuple[str, str]:
    try:
        manifest, _provenance = _verify_asset_metadata(
            row, custodies, require_portable_paths=False
        )
        _verify_asset_custody_rows(row, custodies)
    except OwnerConflict:
        return "failed", "unavailable"
    if int(row.byte_count) > MAX_ASSET_BYTES:
        return "unknown", "unavailable"
    entries = manifest["entries"]
    managed_failed = False
    if any(custody.custody_mode == "managed" for custody in custodies):
        try:
            for entry in entries:
                object_path = entry["object_path"]
                if not isinstance(object_path, str):
                    object_path = _managed_asset_object_path(str(entry["sha256"]))
                candidate = _managed_object_candidate(object_store, object_path)
                if not _file_matches(
                    candidate, int(entry["size"]), str(entry["sha256"])
                ):
                    raise OwnerConflict("asset_custody_unavailable")
            return "verified", "available"
        except (OSError, OwnerConflict):
            managed_failed = True
    drifted = False
    for source in _receipt_bound_asset_sources(row, custodies):
        try:
            if not source.exists() or source.is_symlink():
                continue
            if _linked_source_matches(manifest, source):
                return "failed" if managed_failed else "verified", "available"
            drifted = True
        except (OSError, OwnerConflict):
            drifted = source.exists()
    integrity = "failed" if managed_failed else "verified"
    return integrity, "drifted" if drifted else "unavailable"


def _asset_observation_basis_hash(row, custodies) -> str:
    return canonical_hash(
        {
            "version_ref": row.version_ref,
            "manifest_hash": row.manifest_hash,
            "receipt_hash": row.receipt_hash,
            "custodies": [
                {
                    "custody_ref": custody.custody_ref,
                    "custody_mode": custody.custody_mode,
                    "source_locator": custody.source_locator,
                    "receipt_kind": custody.receipt_kind,
                    "receipt_ref": custody.receipt_ref,
                    "receipt_hash": custody.receipt_hash,
                    "locator_binding_kind": custody.locator_binding_kind,
                    "locator_binding_ref": custody.locator_binding_ref,
                    "locator_binding_hash": custody.locator_binding_hash,
                    "locator_binding_request_hash": (
                        custody.locator_binding_request_hash
                    ),
                    "locator_bound_at": custody.locator_bound_at,
                }
                for custody in custodies
            ],
        }
    )


def _receipt_bound_asset_sources(row, custodies) -> list[Path]:
    local_source_kinds = {
        "directory",
        "file",
        "local_path",
        "repository",
        "system_artifact",
    }
    sources: list[Path] = []
    for custody in custodies:
        if not isinstance(custody.source_locator, str) or not custody.source_locator:
            continue
        if (
            custody.receipt_kind == ASSET_CUSTODY_ESTABLISHED_RECEIPT_KIND
            or custody.locator_binding_kind
            == ASSET_CUSTODY_LOCATOR_MIGRATED_RECEIPT_KIND
        ) and row.source_kind in local_source_kinds:
            sources.append(Path(custody.source_locator))
    return sources


def _linked_source_matches(manifest: dict[str, object], source: Path) -> bool:
    entries = manifest["entries"]
    if manifest["kind"] == "file":
        if not source.is_file() or len(entries) != 1:
            return False
        entry = entries[0]
        return _file_matches(
            source, int(entry["size"]), str(entry["sha256"])
        )
    if manifest["kind"] != "directory" or not source.is_dir():
        return False
    ignored = manifest.get("ignored_top_level", [])
    if not isinstance(ignored, list) or not all(
        isinstance(item, str) for item in ignored
    ):
        raise OwnerConflict("asset_manifest_invalid")
    current_directories, current_entries = _scan_directory_content(
        source, ignored_top_level=tuple(ignored)
    )
    frozen_entries = [
        {"path": entry["path"], "sha256": entry["sha256"], "size": entry["size"]}
        for entry in entries
    ]
    return (
        (
            "directories" not in manifest
            or current_directories == manifest.get("directories", [])
        )
        and current_entries == frozen_entries
    )


def _materialized_entry_content(
    object_store: Path,
    row,
    custodies,
    manifest: dict[str, object],
    entry: dict[str, object],
) -> bytes:
    object_path = entry["object_path"]
    if any(custody.custody_mode == "managed" for custody in custodies):
        if not isinstance(object_path, str):
            object_path = _managed_asset_object_path(str(entry["sha256"]))
        try:
            candidate = _managed_object_candidate(object_store, object_path)
            return _read_exact_file(
                candidate,
                int(entry["size"]),
                str(entry["sha256"]),
                unavailable_code="asset_custody_unavailable",
                mismatch_code="asset_custody_unavailable",
            )
        except (OSError, OwnerConflict):
            pass
    for source in _receipt_bound_asset_sources(row, custodies):
        try:
            if source.is_symlink():
                continue
            if manifest["kind"] == "file":
                candidate = source
            else:
                root = source.resolve(strict=True)
                candidate = (root / str(entry["path"])).resolve(strict=True)
                if not candidate.is_relative_to(root):
                    continue
            if candidate.is_symlink() or not candidate.is_file():
                continue
            return _read_exact_file(
                candidate,
                int(entry["size"]),
                str(entry["sha256"]),
                unavailable_code="asset_custody_unavailable",
                mismatch_code="asset_custody_unavailable",
            )
        except (OSError, OwnerConflict):
            continue
    raise OwnerConflict("asset_custody_unavailable")


def _managed_asset_object_path(object_hash: str) -> str:
    return f"assets/{object_hash[:2]}/{object_hash}"


def _version_ref_condition(
    column: str, version_refs: tuple[str, ...]
) -> tuple[str, dict[str, object]]:
    if not version_refs:
        return "1 = 0", {}
    parameters = {
        f"version_ref_{index}": version_ref
        for index, version_ref in enumerate(version_refs)
    }
    placeholders = ", ".join(f":{name}" for name in parameters)
    return f"{column} IN ({placeholders})", parameters


def _insert_managed_content_asset(
    connection,
    *,
    version_ref: str,
    source_kind: str,
    display_name: str,
    content_hash: str,
    content_json: str,
    object_path: str,
    provenance: dict[str, object],
    acceptance_kind: str,
    receipt_ref: str,
    receipt_hash: str,
    accepted_at: float,
) -> None:
    """Mirror an established RM content fact into the unified Asset inventory."""

    content_bytes = content_json.encode("utf-8")
    manifest = {
        "schema_ref": ASSET_MANIFEST_SCHEMA,
        "kind": "file",
        "entries": [
            {
                "path": "content.json",
                "sha256": content_hash,
                "size": len(content_bytes),
                "object_path": object_path,
            }
        ],
    }
    manifest_json = canonical_json(manifest)
    provenance_json = canonical_json(provenance)
    connection.execute(
        text(
            "INSERT INTO rm_assets (asset_ref, created_at) VALUES "
            "(:asset_ref, :created_at)"
        ),
        {"asset_ref": version_ref, "created_at": accepted_at},
    )
    connection.execute(
        text(
            "INSERT INTO rm_asset_versions (version_ref, asset_ref, "
            "version_number, source_kind, display_name, media_type, content_hash, "
            "manifest_json, manifest_hash, byte_count, provenance_json, "
            "provenance_hash, acceptance_kind, receipt_ref, receipt_hash, "
            "accepted_at) VALUES (:version_ref, :version_ref, 1, :source_kind, "
            ":display_name, 'application/json', :content_hash, :manifest_json, "
            ":manifest_hash, :byte_count, :provenance_json, :provenance_hash, "
            ":acceptance_kind, :receipt_ref, :receipt_hash, :accepted_at)"
        ),
        {
            "version_ref": version_ref,
            "source_kind": source_kind,
            "display_name": display_name,
            "content_hash": content_hash,
            "manifest_json": manifest_json,
            "manifest_hash": canonical_hash(manifest),
            "byte_count": len(content_bytes),
            "provenance_json": provenance_json,
            "provenance_hash": canonical_hash(provenance),
            "acceptance_kind": acceptance_kind,
            "receipt_ref": receipt_ref,
            "receipt_hash": receipt_hash,
            "accepted_at": accepted_at,
        },
    )
    connection.execute(
        text(
            "INSERT INTO rm_asset_custodies (custody_ref, version_ref, "
            "custody_mode, source_locator, receipt_kind, receipt_ref, "
            "receipt_hash, established_at) VALUES (:custody_ref, :version_ref, "
            "'managed', NULL, :receipt_kind, :receipt_ref, :receipt_hash, "
            ":established_at)"
        ),
        {
            "custody_ref": f"content-custody:{version_ref}",
            "version_ref": version_ref,
            "receipt_kind": acceptance_kind,
            "receipt_ref": receipt_ref,
            "receipt_hash": receipt_hash,
            "established_at": accepted_at,
        },
    )
    _register_managed_manifest(
        connection, manifest, registered_at=accepted_at
    )
    connection.execute(
        text(
            "UPDATE rm_asset_verification_observations SET integrity = "
            "'verified', availability = 'available', observed_at = "
            ":observed_at, next_verify_at = :next_verify_at WHERE version_ref = "
            ":version_ref"
        ),
        {
            "version_ref": version_ref,
            "observed_at": accepted_at,
            "next_verify_at": (
                accepted_at + ASSET_VERIFICATION_INTERVAL_SECONDS
            ),
        },
    )


def _managed_object_count(connection) -> int:
    return int(
        connection.execute(
            text("SELECT COUNT(*) FROM rm_managed_objects")
        ).scalar_one()
    )


def _register_managed_manifest(
    connection, manifest: dict[str, object], *, registered_at: float
) -> None:
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise OwnerConflict("asset_manifest_invalid")
    for entry in entries:
        if not isinstance(entry, dict):
            raise OwnerConflict("asset_manifest_invalid")
        digest = entry.get("sha256")
        size = entry.get("size")
        object_path = entry.get("object_path")
        if not isinstance(digest, str) or not isinstance(size, int):
            raise OwnerConflict("asset_manifest_invalid")
        if not isinstance(object_path, str):
            object_path = _managed_asset_object_path(digest)
        connection.execute(
            text(
                "INSERT OR IGNORE INTO rm_managed_objects (object_path, "
                "content_hash, byte_count, registered_at) VALUES (:object_path, "
                ":content_hash, :byte_count, :registered_at)"
            ),
            {
                "object_path": object_path,
                "content_hash": digest,
                "byte_count": size,
                "registered_at": registered_at,
            },
        )
        registered = connection.execute(
            text(
                "SELECT content_hash, byte_count FROM rm_managed_objects WHERE "
                "object_path = :object_path"
            ),
            {"object_path": object_path},
        ).one()
        if registered.content_hash != digest or int(registered.byte_count) != size:
            raise OwnerConflict("asset_object_registry_conflict")


def _verify_managed_manifest(
    object_store: Path, manifest: dict[str, object]
) -> None:
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise OwnerConflict("asset_manifest_invalid")
    for entry in entries:
        if not isinstance(entry, dict):
            raise OwnerConflict("asset_manifest_invalid")
        digest = entry.get("sha256")
        size = entry.get("size")
        object_path = entry.get("object_path")
        if not isinstance(digest, str) or not isinstance(size, int):
            raise OwnerConflict("asset_manifest_invalid")
        if not isinstance(object_path, str):
            object_path = _managed_asset_object_path(digest)
        candidate = _managed_object_candidate(object_store, object_path)
        if not _file_matches(candidate, size, digest):
            raise OwnerConflict("asset_custody_unavailable")


def _read_managed_object(object_store: Path, object_path: str) -> bytes:
    candidate = _managed_object_candidate(object_store, object_path)
    try:
        return candidate.read_bytes()
    except OSError as error:
        raise OwnerConflict("asset_custody_unavailable") from error


def _managed_object_candidate(object_store: Path, object_path: str) -> Path:
    root = object_store.resolve()
    candidate = (root / object_path).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise OwnerConflict("asset_custody_unavailable")
    return candidate


def _legacy_question_row(database: Database, content_ref: str):
    with database.read() as connection:
        row = connection.execute(
            text(
                "SELECT * FROM rm_formal_question_contents WHERE content_ref = "
                ":content_ref"
            ),
            {"content_ref": content_ref},
        ).first()
    if row is None:
        raise OwnerConflict("asset_receipt_invalid")
    return row


def _legacy_idea_row(database: Database, content_ref: str):
    with database.read() as connection:
        row = connection.execute(
            text(
                "SELECT * FROM rm_idea_outcome_contents WHERE content_ref = "
                ":content_ref"
            ),
            {"content_ref": content_ref},
        ).first()
    if row is None:
        raise OwnerConflict("asset_receipt_invalid")
    return row


def _plan_row(database: Database, content_ref: str):
    with database.read() as connection:
        row = connection.execute(
            text(
                "SELECT * FROM rm_plan_documents WHERE content_ref = "
                ":content_ref"
            ),
            {"content_ref": content_ref},
        ).first()
    if row is None:
        raise OwnerConflict("asset_receipt_invalid")
    return row


def _plan_evidence_provenance(row) -> tuple[str, tuple[str, ...]]:
    try:
        provenance = decoded_object(row.provenance_json)
    except (TypeError, ValueError) as error:
        raise OwnerConflict("plan_evidence_binding_invalid") from error
    if (
        canonical_json(provenance) != row.provenance_json
        or canonical_hash(provenance) != row.provenance_hash
    ):
        raise OwnerConflict("plan_evidence_binding_invalid")
    target = provenance.get("target_commit_root_ref")
    if not isinstance(target, str) or not target.strip():
        target = row.asset_ref
    closure = provenance.get("provenance_closure_refs")
    if not isinstance(closure, list):
        closure = provenance.get("closure_refs")
    if (
        not isinstance(closure, list)
        or not closure
        or not all(isinstance(value, str) and value.strip() for value in closure)
        or len(closure) != len(set(closure))
    ):
        closure = [f"asset-version:{row.version_ref}"]
    return target, tuple(closure)


def _verify_object(object_store: Path, row) -> None:
    root = object_store.resolve()
    candidate = (root / row.object_path).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise OwnerConflict("question_content_custody_unavailable")
    try:
        payload = candidate.read_bytes()
    except OSError as error:
        raise OwnerConflict("question_content_custody_unavailable") from error
    if (
        hashlib.sha256(payload).hexdigest() != row.content_hash
        or payload != row.content_json.encode("utf-8")
    ):
        raise OwnerConflict("question_content_custody_unavailable")


def _verify_idea_object(object_store: Path, row) -> None:
    root = object_store.resolve()
    candidate = (root / row.object_path).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise OwnerConflict("idea_content_custody_unavailable")
    try:
        payload = candidate.read_bytes()
    except OSError as error:
        raise OwnerConflict("idea_content_custody_unavailable") from error
    if (
        hashlib.sha256(payload).hexdigest() != row.payload_hash
        or payload != row.payload_json.encode("utf-8")
    ):
        raise OwnerConflict("idea_content_custody_unavailable")


def _resolved_reviewed_draft(
    outcome: dict[str, object],
    review: dict[str, object],
    reviewed_draft: dict[str, object] | None,
) -> dict[str, object]:
    if reviewed_draft is not None:
        if not isinstance(reviewed_draft, dict):
            raise OwnerConflict("reviewed_draft_invalid")
        return reviewed_draft
    if review.get("reviewed_draft_hash") != canonical_hash(outcome):
        raise OwnerConflict("reviewed_draft_missing")
    return outcome


def _verify_idea_payload(
    row,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    try:
        payload = decoded_object(row.payload_json)
        outcome = decoded_object(row.outcome_json)
        reviewed_draft = decoded_object(row.reviewed_draft_json)
        review = decoded_object(row.review_json)
    except (TypeError, ValueError) as error:
        raise OwnerConflict("idea_content_invalid") from error
    if (
        payload
        != {
            "schema_ref": ATTEMPT_EXECUTION_SCHEMA,
            "outcome": outcome,
            "reviewed_draft": reviewed_draft,
            "review": review,
        }
        or canonical_json(payload) != row.payload_json
        or canonical_json(outcome) != row.outcome_json
        or canonical_json(reviewed_draft) != row.reviewed_draft_json
        or canonical_json(review) != row.review_json
        or canonical_hash(payload) != row.payload_hash
        or canonical_hash(outcome) != row.outcome_hash
        or canonical_hash(reviewed_draft) != row.reviewed_draft_hash
        or canonical_hash(review) != row.review_hash
        or {"IdeaSet": "idea_set", "NoViableCandidate": "no_viable_candidate"}.get(
            outcome.get("kind")
        )
        != row.outcome_kind
    ):
        raise OwnerConflict("idea_content_invalid")
    try:
        validate_idea_content(
            outcome,
            review,
            reviewed_draft=reviewed_draft,
        )
    except IdeaContractError as error:
        raise OwnerConflict(str(error)) from error
    return outcome, reviewed_draft, review


def _verify_plan_object(object_store: Path, row) -> None:
    root = object_store.resolve()
    candidate = (root / row.object_path).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise OwnerConflict("plan_content_custody_unavailable")
    try:
        payload = candidate.read_bytes()
    except OSError as error:
        raise OwnerConflict("plan_content_custody_unavailable") from error
    if (
        hashlib.sha256(payload).hexdigest() != row.payload_hash
        or payload != row.payload_json.encode("utf-8")
    ):
        raise OwnerConflict("plan_content_custody_unavailable")


def _verify_plan_payload(
    row,
    verified_request,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    try:
        payload = decoded_object(row.payload_json)
        plan_document = decoded_object(row.plan_document_json)
        reviewed_draft = decoded_object(row.reviewed_draft_json)
        review = decoded_object(row.review_json)
    except (TypeError, ValueError) as error:
        raise OwnerConflict("plan_content_invalid") from error
    if (
        payload
        != {
            "schema_ref": PLAN_ATTEMPT_EXECUTION_SCHEMA,
            "outcome": plan_document,
            "reviewed_draft": reviewed_draft,
            "review": review,
        }
        or canonical_json(payload) != row.payload_json
        or canonical_json(plan_document) != row.plan_document_json
        or canonical_json(reviewed_draft) != row.reviewed_draft_json
        or canonical_json(review) != row.review_json
        or canonical_hash(payload) != row.payload_hash
        or canonical_hash(plan_document) != row.plan_document_hash
        or canonical_hash(reviewed_draft) != row.reviewed_draft_hash
        or canonical_hash(review) != row.review_hash
    ):
        raise OwnerConflict("plan_content_invalid")
    answer_contract = plan_document.get("answer_contract")
    if (
        not isinstance(answer_contract, dict)
        or answer_contract.get("answer_contract_hash")
        != row.answer_contract_hash
    ):
        raise OwnerConflict("plan_content_invalid")
    try:
        validate_plan_review(
            review,
            reviewed_draft_hash=row.reviewed_draft_hash,
            final_plan_hash=row.plan_document_hash,
        )
    except PlanContractError as error:
        raise OwnerConflict(str(error)) from error
    if verified_request is None:
        return plan_document, reviewed_draft, review
    try:
        context_pack = verified_request.context_pack
        question_binding = context_pack.get("accepted_question_binding")
        idea_binding = context_pack.get("accepted_idea_set_binding")
        if not isinstance(question_binding, dict) or not isinstance(
            idea_binding, dict
        ):
            raise PlanContractError("plan_context_pack_invalid")
        evidence_by_ref = validate_plan_context_pack(
            context_pack,
            cycle_ref=verified_request.cycle_ref,
            accepted_question_binding=question_binding,
        )
        evidence_revision = context_pack.get("evidence_reference_revision")
        if not isinstance(evidence_revision, int) or isinstance(
            evidence_revision, bool
        ):
            raise PlanContractError("plan_evidence_catalog_invalid")
        content_receipt = idea_binding.get("content_receipt")
        outcome_receipt = idea_binding.get("outcome_receipt")
        stage_commit_receipt = idea_binding.get("stage_commit_receipt")
        question_content_receipt = question_binding.get("content_receipt")
        question_receipt = question_binding.get("question_receipt")
        if (
            not isinstance(content_receipt, dict)
            or not isinstance(outcome_receipt, dict)
            or not isinstance(stage_commit_receipt, dict)
            or not isinstance(question_content_receipt, dict)
            or not isinstance(question_receipt, dict)
            or question_binding != verified_request.accepted_question.as_dict()
            or verified_request.accepted_idea_set is None
            or idea_binding != verified_request.accepted_idea_set.as_dict()
            or canonical_hash(context_pack) != verified_request.context_pack_hash
            or verified_request.context_pack_ref != row.context_pack_ref
            or question_binding.get("initialization_id") != row.initialization_id
            or question_binding.get("quest_ref") != row.quest_ref
            or question_binding.get("question_ref") != row.question_ref
            or question_binding.get("content_ref") != row.question_content_ref
            or question_binding.get("content_hash") != row.question_content_hash
            or question_content_receipt.get("receipt_ref")
            != row.question_content_receipt_ref
            or question_content_receipt.get("payload_hash")
            != row.question_content_receipt_hash
            or question_receipt.get("receipt_ref") != row.question_receipt_ref
            or question_receipt.get("payload_hash") != row.question_receipt_hash
            or idea_binding.get("outcome_ref") != row.idea_outcome_ref
            or idea_binding.get("content_ref") != row.idea_content_ref
            or idea_binding.get("payload_hash") != row.idea_content_hash
            or content_receipt.get("receipt_ref")
            != row.idea_content_receipt_ref
            or content_receipt.get("payload_hash")
            != row.idea_content_receipt_hash
            or outcome_receipt.get("receipt_ref")
            != row.idea_outcome_receipt_ref
            or outcome_receipt.get("payload_hash")
            != row.idea_outcome_receipt_hash
            or idea_binding.get("stage_commit_ref")
            != row.idea_stage_commit_ref
            or stage_commit_receipt.get("receipt_ref")
            != row.idea_stage_commit_receipt_ref
            or stage_commit_receipt.get("payload_hash")
            != row.idea_stage_commit_receipt_hash
        ):
            raise PlanContractError("plan_content_source_invalid")
        idea_set = idea_binding.get("idea_set")
        if not isinstance(idea_set, dict):
            raise PlanContractError("plan_idea_set_binding_invalid")
        validated_plan_hash = validate_plan_document(
            plan_document,
            question_ref=row.question_ref,
            idea_set_ref=row.idea_outcome_ref,
            context_pack_ref=row.context_pack_ref,
            context_pack_hash=verified_request.context_pack_hash,
            accepted_idea_set=idea_set,
            evidence_by_ref=evidence_by_ref,
            evidence_reference_revision=evidence_revision,
        )
        validated_draft_hash = validate_plan_document(
            reviewed_draft,
            question_ref=row.question_ref,
            idea_set_ref=row.idea_outcome_ref,
            context_pack_ref=row.context_pack_ref,
            context_pack_hash=verified_request.context_pack_hash,
            accepted_idea_set=idea_set,
            evidence_by_ref=evidence_by_ref,
            evidence_reference_revision=evidence_revision,
        )
        if (
            validated_plan_hash != row.plan_document_hash
            or validated_draft_hash != row.reviewed_draft_hash
        ):
            raise PlanContractError("plan_content_hash_invalid")
    except PlanContractError as error:
        raise OwnerConflict(str(error)) from error
    return plan_document, reviewed_draft, review


def _receipt_hash(kind: str, subject_ref: str, bindings: dict[str, object]) -> str:
    return canonical_hash(
        {
            "schema_ref": RECEIPT_SCHEMA,
            "issuer": RM_OWNER,
            "kind": kind,
            "subject_ref": subject_ref,
            "bindings": bindings,
        }
    )


def _content_receipt_hash(row) -> str:
    return _receipt_hash(
        CONTENT_RECEIPT_KIND,
        row.content_ref,
        {
            "initialization_id": row.initialization_id,
            "quest_ref": row.quest_ref,
            "quest_receipt_ref": row.quest_receipt_ref,
            "quest_receipt_hash": row.quest_receipt_hash,
            "proposal_ref": row.proposal_ref,
            "proposal_hash": row.proposal_hash,
            "confirmation_ref": row.confirmation_ref,
            "confirmation_hash": row.confirmation_hash,
            "content_hash": row.content_hash,
            "schema_ref": row.schema_ref,
        },
    )


def _idea_content_bindings(row) -> dict[str, object]:
    return {
        "request_ref": row.request_ref,
        "run_ref": row.run_ref,
        "attempt_ref": row.attempt_ref,
        "fence_ref": row.fence_ref,
        "submission_ref": row.submission_ref,
        "outcome_kind": row.outcome_kind,
        "payload_hash": row.payload_hash,
        "outcome_hash": row.outcome_hash,
        "reviewed_draft_hash": row.reviewed_draft_hash,
        "review_hash": row.review_hash,
        "execution_receipt_ref": row.execution_receipt_ref,
        "execution_receipt_hash": row.execution_receipt_hash,
    }


def _idea_content_receipt_hash(row) -> str:
    return _receipt_hash(
        IDEA_CONTENT_RECEIPT_KIND, row.content_ref, _idea_content_bindings(row)
    )


def _plan_content_bindings(row) -> dict[str, object]:
    return {
        "request_ref": row.request_ref,
        "run_ref": row.run_ref,
        "attempt_ref": row.attempt_ref,
        "fence_ref": row.fence_ref,
        "submission_ref": row.submission_ref,
        "initialization_id": row.initialization_id,
        "quest_ref": row.quest_ref,
        "question_ref": row.question_ref,
        "context_pack_ref": row.context_pack_ref,
        "question_content_ref": row.question_content_ref,
        "question_content_hash": row.question_content_hash,
        "question_content_receipt_ref": row.question_content_receipt_ref,
        "question_content_receipt_hash": row.question_content_receipt_hash,
        "question_receipt_ref": row.question_receipt_ref,
        "question_receipt_hash": row.question_receipt_hash,
        "idea_outcome_ref": row.idea_outcome_ref,
        "idea_content_ref": row.idea_content_ref,
        "idea_content_hash": row.idea_content_hash,
        "idea_content_receipt_ref": row.idea_content_receipt_ref,
        "idea_content_receipt_hash": row.idea_content_receipt_hash,
        "idea_outcome_receipt_ref": row.idea_outcome_receipt_ref,
        "idea_outcome_receipt_hash": row.idea_outcome_receipt_hash,
        "idea_stage_commit_ref": row.idea_stage_commit_ref,
        "idea_stage_commit_receipt_ref": row.idea_stage_commit_receipt_ref,
        "idea_stage_commit_receipt_hash": row.idea_stage_commit_receipt_hash,
        "plan_document_hash": row.plan_document_hash,
        "answer_contract_hash": row.answer_contract_hash,
        "reviewed_draft_hash": row.reviewed_draft_hash,
        "review_hash": row.review_hash,
        "payload_hash": row.payload_hash,
        "execution_receipt_ref": row.execution_receipt_ref,
        "execution_receipt_hash": row.execution_receipt_hash,
    }


def _plan_content_receipt_hash(row) -> str:
    return _receipt_hash(
        PLAN_CONTENT_RECEIPT_KIND,
        row.content_ref,
        _plan_content_bindings(row),
    )


def _accepted_content(row) -> AcceptedQuestionContent:
    return AcceptedQuestionContent(
        initialization_id=row.initialization_id,
        content_ref=row.content_ref,
        content_hash=row.content_hash,
        schema_ref=row.schema_ref,
        proposal_ref=row.proposal_ref,
        proposal_hash=row.proposal_hash,
        confirmation_ref=row.confirmation_ref,
        receipt=AcceptanceReceipt(
            issuer=RM_OWNER,
            kind=CONTENT_RECEIPT_KIND,
            receipt_ref=row.receipt_ref,
            subject_ref=row.content_ref,
            payload_hash=row.receipt_hash,
        ),
    )


def _accepted_idea_content(row) -> AcceptedIdeaOutcomeContent:
    outcome, reviewed_draft, review = _verify_idea_payload(row)
    _verify_idea_object_path_shape(row)
    return AcceptedIdeaOutcomeContent(
        request_ref=row.request_ref,
        run_ref=row.run_ref,
        attempt_ref=row.attempt_ref,
        fence_ref=row.fence_ref,
        submission_ref=row.submission_ref,
        content_ref=row.content_ref,
        outcome_kind=row.outcome_kind,
        payload_hash=row.payload_hash,
        outcome_hash=row.outcome_hash,
        reviewed_draft_hash=row.reviewed_draft_hash,
        review_hash=row.review_hash,
        outcome=outcome,
        reviewed_draft=reviewed_draft,
        review=review,
        execution_receipt=AcceptanceReceipt(
            issuer="agent_runtime",
            kind="idea_attempt_execution",
            receipt_ref=row.execution_receipt_ref,
            subject_ref=row.submission_ref,
            payload_hash=row.execution_receipt_hash,
        ),
        receipt=AcceptanceReceipt(
            issuer=RM_OWNER,
            kind=IDEA_CONTENT_RECEIPT_KIND,
            receipt_ref=row.receipt_ref,
            subject_ref=row.content_ref,
            payload_hash=row.receipt_hash,
        ),
    )


def _accepted_plan_document(row) -> AcceptedPlanDocument:
    plan_document, reviewed_draft, review = _verify_plan_payload(row, None)
    _verify_plan_object_path_shape(row)
    return AcceptedPlanDocument(
        request_ref=row.request_ref,
        run_ref=row.run_ref,
        attempt_ref=row.attempt_ref,
        fence_ref=row.fence_ref,
        submission_ref=row.submission_ref,
        initialization_id=row.initialization_id,
        quest_ref=row.quest_ref,
        question_ref=row.question_ref,
        context_pack_ref=row.context_pack_ref,
        question_content_ref=row.question_content_ref,
        question_content_hash=row.question_content_hash,
        question_content_receipt=AcceptanceReceipt(
            issuer=RM_OWNER,
            kind=CONTENT_RECEIPT_KIND,
            receipt_ref=row.question_content_receipt_ref,
            subject_ref=row.question_content_ref,
            payload_hash=row.question_content_receipt_hash,
        ),
        question_receipt=AcceptanceReceipt(
            issuer="research_graph",
            kind="root_question_acceptance",
            receipt_ref=row.question_receipt_ref,
            subject_ref=row.question_ref,
            payload_hash=row.question_receipt_hash,
        ),
        idea_outcome_ref=row.idea_outcome_ref,
        idea_content_ref=row.idea_content_ref,
        idea_content_hash=row.idea_content_hash,
        idea_content_receipt=AcceptanceReceipt(
            issuer=RM_OWNER,
            kind=IDEA_CONTENT_RECEIPT_KIND,
            receipt_ref=row.idea_content_receipt_ref,
            subject_ref=row.idea_content_ref,
            payload_hash=row.idea_content_receipt_hash,
        ),
        idea_outcome_receipt=AcceptanceReceipt(
            issuer="research_graph",
            kind="idea_outcome_accepted",
            receipt_ref=row.idea_outcome_receipt_ref,
            subject_ref=row.idea_outcome_ref,
            payload_hash=row.idea_outcome_receipt_hash,
        ),
        idea_stage_commit_ref=row.idea_stage_commit_ref,
        idea_stage_commit_receipt=AcceptanceReceipt(
            issuer="advancement_engine",
            kind="stage_commit",
            receipt_ref=row.idea_stage_commit_receipt_ref,
            subject_ref=row.idea_stage_commit_ref,
            payload_hash=row.idea_stage_commit_receipt_hash,
        ),
        content_ref=row.content_ref,
        payload_hash=row.payload_hash,
        plan_document_hash=row.plan_document_hash,
        answer_contract_hash=row.answer_contract_hash,
        reviewed_draft_hash=row.reviewed_draft_hash,
        review_hash=row.review_hash,
        plan_document=plan_document,
        reviewed_draft=reviewed_draft,
        review=review,
        execution_receipt=AcceptanceReceipt(
            issuer="agent_runtime",
            kind=PLAN_ATTEMPT_EXECUTION_RECEIPT_KIND,
            receipt_ref=row.execution_receipt_ref,
            subject_ref=row.submission_ref,
            payload_hash=row.execution_receipt_hash,
        ),
        receipt=AcceptanceReceipt(
            issuer=RM_OWNER,
            kind=PLAN_CONTENT_RECEIPT_KIND,
            receipt_ref=row.receipt_ref,
            subject_ref=row.content_ref,
            payload_hash=row.receipt_hash,
        ),
    )


def _verify_idea_object_path_shape(row) -> None:
    expected = f"idea-outcome-content/{row.payload_hash[:2]}/{row.payload_hash}.json"
    if row.object_path != expected:
        raise OwnerConflict("idea_content_custody_unavailable")


def _verify_plan_object_path_shape(row) -> None:
    expected = f"plan-document-content/{row.payload_hash[:2]}/{row.payload_hash}.json"
    if row.object_path != expected:
        raise OwnerConflict("plan_content_custody_unavailable")


def create_research_memory_receipt_verifier(
    database: Database,
    object_store: Path,
    execution_verifier: AttemptExecutionReceiptVerifier | None = None,
    stage_request_verifier: StageRunRequestVerifier | None = None,
) -> SQLiteResearchMemoryReceiptVerifier:
    return SQLiteResearchMemoryReceiptVerifier(
        database,
        object_store,
        execution_verifier,
        stage_request_verifier,
    )


def create_research_memory_interface(
    database: Database,
    object_store: Path,
    feed: DurableFeed,
    confirmation_verifier: BundleConfirmationVerifier,
    quest_verifier: QuestReceiptVerifier,
    receipt_verifier: SQLiteResearchMemoryReceiptVerifier,
    execution_verifier: AttemptExecutionReceiptVerifier | None = None,
    reference_reader: AssetReferenceReader | None = None,
    stage_request_verifier: StageRunRequestVerifier | None = None,
) -> ResearchMemoryInterface:
    return SQLiteResearchMemory(
        database,
        object_store,
        feed,
        confirmation_verifier,
        quest_verifier,
        receipt_verifier,
        execution_verifier,
        reference_reader,
        stage_request_verifier,
    )
