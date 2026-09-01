"""One final AR -> RM -> RG boundary for a root-owned Target lifecycle.

The Target root may edit, train, inspect, and retry freely inside one native
Session.  This module sees none of those internal steps.  It consumes only the
root's final closed handoff, freezes the Owner-resolved workspace bytes once,
and makes every downstream acceptance idempotent.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from meta_research.bundle_protocol import (
    BUNDLE_CANONICAL_INTEGER_MAX_ABS,
    TargetWorkHandle,
    projection_plain_value,
)
from meta_research.database import Database
from meta_research.experiment_contract import EXPERIMENT_RESULT_DISPOSITIONS
from meta_research.feed import DurableFeed
from meta_research.owners.common import (
    AcceptanceReceipt,
    AcceptedAssetBinding,
    OwnerConflict,
    canonical_hash,
    canonical_json,
    new_ref,
)
from meta_research.owners.research_memory import (
    MAX_ASSET_BYTES,
    AssetIntakeRequest,
)
from meta_research.owners.agent_runtime_harness import (
    TargetRootCompletionEvidence,
)
from meta_research.owners.target_root_lifecycle import (
    AcceptedTargetRootCompletion,
    AcceptedTargetRootCompletionRejection,
    SQLiteTargetRootLifecycleAuthority,
)
from meta_research.owners.target_run_runtime import (
    FrozenTargetCommitInput,
    FrozenTargetCommitInputArtifact,
)
from meta_research.target_implementation_bundle import (
    TargetImplementationBundleError,
    build_target_implementation_bundle_from_open_directory,
    parse_target_implementation_bundle,
)
from meta_research.target_run_runtime_contract import (
    TARGET_COMPLETION_ARTIFACT_ROLES,
    TargetCompletionArtifact,
    TargetCompletionHandoff,
    validate_target_completion_handoff,
)


TARGET_ROOT_RESULT_DOCUMENT_FIELDS = frozenset(
    {"schema_ref", "metrics", "result_disposition"}
)
RM_TARGET_ROOT_COMPLETION_MANIFEST_RECEIPT_KIND = (
    "target_root_completion_manifest_accepted"
)
TARGET_ROOT_RG_PENDING_CODE = "target_root_graph_acceptance_unavailable"
TARGET_ROOT_MAX_ARTIFACT_SET_BYTES = 256 * 1024 * 1024
TARGET_ROOT_MAX_RESULT_DOCUMENT_BYTES = 256 * 1024
# The formal protocol admits at most 64 required plus 64 optional metric
# definitions, and requires those two key sets to be disjoint.
TARGET_ROOT_MAX_RESULT_METRICS = 2 * 64

_SYSTEM_TARGET_COMPLETION_REQUIRED_ARTIFACTS = (
    ("implementation", "implementation"),
    ("result", "outputs/result.json"),
)
_SYSTEM_TARGET_COMPLETION_OPTIONAL_ARTIFACTS = (
    ("checkpoint", "outputs/checkpoints"),
    ("analysis", "outputs/analysis"),
    ("log", "logs"),
)

_RM_RECOVERABLE_CANDIDATE_FEEDBACK = {
    "target_root_artifact_missing": (
        "A required conventional completion path is missing from the Target "
        "workspace. Create implementation/ and outputs/result.json as needed, "
        "then complete another root turn."
    ),
    "target_root_artifact_too_large": (
        "A declared completion artifact exceeds the Research Memory intake "
        "limit. Reduce or split it and complete another root turn."
    ),
    "target_root_artifact_set_too_large": (
        "The declared completion artifact set exceeds the bounded finalization "
        "budget. Reduce the conventional artifacts and complete another root turn."
    ),
    "target_root_artifact_type_unsupported": (
        "A declared completion artifact has an unsupported filesystem type. "
        "Replace it with a regular file or directory and complete another root "
        "turn."
    ),
    "target_root_artifact_symlink_forbidden": (
        "A conventional completion path is a symbolic link. Replace it with a "
        "regular file or directory inside the Target workspace and complete "
        "another root turn."
    ),
    "target_implementation_workspace_invalid": (
        "The declared implementation artifact is not a valid directory. Correct "
        "implementation/ and complete another root turn."
    ),
    "target_implementation_workspace_entry_unsupported": (
        "The implementation artifact contains an unsupported entry or an "
        "oversized file. Correct it and complete another root turn."
    ),
    "target_implementation_bundle_too_large": (
        "The implementation artifact exceeds the accepted bundle limits. Reduce "
        "it and complete another root turn."
    ),
    "target_root_result_document_invalid": (
        "The declared result document is not valid canonical JSON for the Target "
        "result schema. Rewrite outputs/result.json and complete another root turn."
    ),
    "target_root_result_document_too_large": (
        "The declared result document exceeds the bounded Target result schema "
        "limit. Reduce it and complete another root turn."
    ),
    "target_root_result_document_noncanonical": (
        "The declared result document is valid JSON but not in canonical form. "
        "Rewrite it in canonical form and complete another root turn."
    ),
    "target_root_result_metrics_invalid": (
        "The declared result document contains invalid metric names or values. "
        "Correct the metrics and complete another root turn."
    ),
    "target_root_checkpoint_policy_invalid": (
        "The conventional checkpoint path does not match the Target checkpoint "
        "policy. Create or remove outputs/checkpoints as directed and complete "
        "another root turn."
    ),
    "asset_content_too_large": (
        "A completion artifact exceeds the Research Memory managed-content limit. "
        "Reduce or split it and complete another root turn."
    ),
    "asset_provenance_too_large": (
        "The completion artifact set produces provenance beyond the Research "
        "Memory limit. Reduce the conventional artifacts and complete another root "
        "turn."
    ),
}


@dataclass(frozen=True, slots=True)
class TargetRootResultDocument:
    schema_ref: str
    metrics: dict[str, int | float]
    result_disposition: str
    content_hash: str

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_ref": self.schema_ref,
            "metrics": self.metrics,
            "result_disposition": self.result_disposition,
        }


@dataclass(frozen=True, slots=True)
class TargetRootCompletionManifestEntry:
    ordinal: int
    role: str
    declared_relative_path: str
    artifact_kind: str
    media_type: str
    byte_count: int
    content_hash: str
    tree_hash: str
    binding: AcceptedAssetBinding

    def as_dict(self) -> dict[str, object]:
        return {
            "ordinal": self.ordinal,
            "role": self.role,
            "declared_relative_path": self.declared_relative_path,
            "artifact_kind": self.artifact_kind,
            "media_type": self.media_type,
            "byte_count": self.byte_count,
            "content_hash": self.content_hash,
            "tree_hash": self.tree_hash,
            "binding": self.binding.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class AcceptedTargetRootCompletionManifest:
    manifest_ref: str
    completion_ref: str
    target_ref: str
    target_run_ref: str
    workspace_ref: str
    implementation_revision_ref: str
    implementation_tree_hash: str
    result_document_path: str
    result_document: TargetRootResultDocument
    result_document_hash: str
    artifact_snapshot_hash: str
    entries: tuple[TargetRootCompletionManifestEntry, ...]
    payload_hash: str
    receipt: AcceptanceReceipt
    accepted_at: float


@dataclass(frozen=True, slots=True)
class TargetRootGraphAcceptance:
    """Minimal RG response; the RG adapter remains the TargetCommit authority."""

    target_ref: str
    target_run_ref: str
    target_commit_ref: str
    receipt: AcceptanceReceipt


@dataclass(frozen=True, slots=True)
class TargetRootOwnerRejection:
    """Recoverable RM/RG response for one immutable completion generation."""

    issuer: Literal["research_memory", "research_graph"]
    rejection_ref: str
    code: str
    feedback: str
    receipt: AcceptanceReceipt


@dataclass(frozen=True, slots=True)
class TargetRootFinalizationResult:
    status: Literal["rm_accepted", "revision_required", "completed"]
    target_ref: str
    target_run_ref: str
    completion_ref: str
    manifest_ref: str | None
    target_commit_ref: str | None
    pending_code: str | None
    completion_generation: int
    rejection_ref: str | None = None
    rejection_issuer: str | None = None
    rejection_feedback: str | None = None


class TargetRootWorkspaceResolver(Protocol):
    def resolve_target_workspace(
        self,
        *,
        target_ref: str,
        target_run_ref: str,
        root_session_ref: str,
        attempt_ref: str,
        fence_ref: str,
    ) -> tuple[str, Path]: ...

    def query_target_workspace_quest_ref(self, handle: TargetWorkHandle) -> str: ...

    def materialize_target_workspace_inputs(
        self,
        *,
        handle: TargetWorkHandle,
        accepted_target_commit_inputs: tuple[FrozenTargetCommitInput, ...] = (),
    ) -> tuple[str, ...]: ...

    def verify_target_workspace_inputs(
        self,
        *,
        handle: TargetWorkHandle,
        accepted_target_commit_inputs: tuple[FrozenTargetCommitInput, ...] = (),
    ) -> None: ...

    def resolve_generic_target_commit_input(
        self, transition: object
    ) -> FrozenTargetCommitInput | None: ...


class TargetRootCompletionEvidenceReader(Protocol):
    def verify_target_root_completion_evidence(
        self,
        *,
        handle: TargetWorkHandle,
        evidence: TargetRootCompletionEvidence,
        handoff: TargetCompletionHandoff | None,
    ) -> str: ...


class TargetRootAssetMemory(Protocol):
    def submit_asset_intake(
        self,
        request: AssetIntakeRequest,
        *,
        idempotency_key: str,
        operation_namespace: str | None = None,
    ) -> object: ...

    def verify_asset_binding(self, **values: object) -> None: ...

    def materialize_asset(self, memory_ref: str) -> object: ...


class TargetMeasurementAuthorityReader(Protocol):
    def query_target_measurement_domain_authority(
        self, target_ref: str
    ) -> object | None: ...


class TargetRootGraphFinalizationAuthority(Protocol):
    def query_target_commits_for_quest(
        self, quest_ref: str
    ) -> tuple[object, ...]: ...

    def query_target_frontier_commit_transition(
        self, target_ref: str
    ) -> object | None: ...

    def accept_target_commit_from_root_completion(
        self,
        *,
        completion: AcceptedTargetRootCompletion,
        manifest: AcceptedTargetRootCompletionManifest,
        result_document: TargetRootResultDocument,
        idempotency_key: str,
    ) -> TargetRootGraphAcceptance | TargetRootOwnerRejection: ...


@dataclass(frozen=True, slots=True)
class _FrozenArtifact:
    ordinal: int
    role: str
    declared_relative_path: str
    artifact_kind: str
    media_type: str
    content: bytes
    content_hash: str
    tree_hash: str

    def snapshot_value(self) -> dict[str, object]:
        return {
            "ordinal": self.ordinal,
            "role": self.role,
            "declared_relative_path": self.declared_relative_path,
            "artifact_kind": self.artifact_kind,
            "media_type": self.media_type,
            "byte_count": len(self.content),
            "content_hash": self.content_hash,
            "tree_hash": self.tree_hash,
        }


@dataclass(frozen=True, slots=True)
class _FrozenWorkspace:
    workspace_ref: str
    artifacts: tuple[_FrozenArtifact, ...]
    implementation_revision_ref: str
    implementation_tree_hash: str
    result_document: TargetRootResultDocument
    result_document_hash: str
    artifact_snapshot_hash: str


@dataclass(frozen=True, slots=True)
class _PinnedWorkspaceRoot:
    workspace_ref: str
    path: Path
    descriptor: int


class SQLiteTargetRootCompletionMemoryAuthority:
    """RM accepts only the bytes already covered by one AR completion."""

    def __init__(
        self,
        database: Database,
        feed: DurableFeed,
        asset_memory: TargetRootAssetMemory,
        lifecycle: SQLiteTargetRootLifecycleAuthority,
    ) -> None:
        self._database = database
        self._feed = feed
        self._asset_memory = asset_memory
        self._lifecycle = lifecycle

    def accept(
        self,
        *,
        completion: AcceptedTargetRootCompletion,
        frozen: _FrozenWorkspace,
    ) -> AcceptedTargetRootCompletionManifest:
        current = self._lifecycle.query_completion(completion.handle.target_ref)
        if current != completion or (
            completion.candidate_rejection_code is not None
            or completion.candidate_rejection_feedback is not None
            or completion.implementation_revision_ref is None
            or completion.implementation_tree_hash is None
            or completion.result_document_hash is None
            or completion.artifact_snapshot_hash is None
            or completion.workspace_ref != frozen.workspace_ref
            or completion.implementation_revision_ref
            != frozen.implementation_revision_ref
            or completion.implementation_tree_hash
            != frozen.implementation_tree_hash
            or completion.result_document_hash != frozen.result_document_hash
            or completion.artifact_snapshot_hash != frozen.artifact_snapshot_hash
        ):
            raise OwnerConflict("target_root_rm_completion_binding_invalid")

        entries: list[TargetRootCompletionManifestEntry] = []
        for artifact in frozen.artifacts:
            intake_key = "target-root-artifact:" + canonical_hash(
                {
                    "completion_ref": completion.completion_ref,
                    **artifact.snapshot_value(),
                }
            )
            try:
                intake = self._asset_memory.submit_asset_intake(
                    AssetIntakeRequest(
                        source_kind="file",
                        custody_mode="managed",
                        display_name=f"target-root-artifact-{artifact.ordinal:04d}",
                        media_type=artifact.media_type,
                        content=artifact.content,
                        provenance={
                            "schema_ref": (
                                "meta-research/target-root-artifact-provenance/v1"
                            ),
                            "completion_ref": completion.completion_ref,
                            "target_ref": completion.handle.target_ref,
                            "target_run_ref": completion.handle.target_run_ref,
                            **artifact.snapshot_value(),
                        },
                    ),
                    idempotency_key=intake_key,
                )
                asset = getattr(intake, "asset", None)
                if getattr(intake, "status", None) != "accepted" or asset is None:
                    raise OwnerConflict("target_root_artifact_intake_unavailable")
                binding = asset.as_binding()
                self._verify_binding(binding, expected=artifact.content)
            except OwnerConflict:
                raise
            except Exception as error:
                raise OwnerConflict(
                    "target_root_artifact_intake_unavailable"
                ) from error
            entries.append(
                TargetRootCompletionManifestEntry(
                    ordinal=artifact.ordinal,
                    role=artifact.role,
                    declared_relative_path=artifact.declared_relative_path,
                    artifact_kind=artifact.artifact_kind,
                    media_type=artifact.media_type,
                    byte_count=len(artifact.content),
                    content_hash=artifact.content_hash,
                    tree_hash=artifact.tree_hash,
                    binding=binding,
                )
            )

        frozen_entries = tuple(entries)
        entries_value = [entry.as_dict() for entry in frozen_entries]
        payload = {
            "completion_ref": completion.completion_ref,
            "target_ref": completion.handle.target_ref,
            "target_run_ref": completion.handle.target_run_ref,
            "workspace_ref": frozen.workspace_ref,
            "implementation_revision_ref": frozen.implementation_revision_ref,
            "implementation_tree_hash": frozen.implementation_tree_hash,
            "result_document_path": completion.handoff.result_document_path,
            "result_document": frozen.result_document.as_dict(),
            "result_document_hash": frozen.result_document_hash,
            "artifact_snapshot_hash": frozen.artifact_snapshot_hash,
            "entries": entries_value,
            "completion_receipt": completion.receipt.as_public_dict(),
        }
        payload_hash = canonical_hash(payload)
        request_hash = canonical_hash(
            {"command": "accept_target_root_completion_manifest", **payload}
        )
        idempotency_key = "target-root-manifest:" + canonical_hash(
            {
                "completion_ref": completion.completion_ref,
                "artifact_snapshot_hash": frozen.artifact_snapshot_hash,
            }
        )
        now = time.time()
        try:
            with self._database.fenced_write() as connection:
                completion_row = connection.execute(
                    text(
                        "SELECT payload_hash, receipt_ref, receipt_hash FROM "
                        "ar_target_root_completions WHERE completion_ref = "
                        ":completion_ref"
                    ),
                    {"completion_ref": completion.completion_ref},
                ).first()
                if completion_row is None or (
                    completion_row.payload_hash != completion.payload_hash
                    or completion_row.receipt_ref
                    != completion.receipt.receipt_ref
                    or completion_row.receipt_hash
                    != completion.receipt.payload_hash
                ):
                    raise OwnerConflict("target_root_rm_completion_binding_invalid")
                row = connection.execute(
                    text(
                        "SELECT * FROM rm_target_root_completion_manifests "
                        "WHERE idempotency_key = :key OR completion_ref = "
                        ":completion_ref"
                    ),
                    {
                        "key": idempotency_key,
                        "completion_ref": completion.completion_ref,
                    },
                ).first()
                if row is not None:
                    if row.request_hash != request_hash:
                        raise OwnerConflict("target_root_manifest_conflict")
                    manifest_ref = str(row.manifest_ref)
                else:
                    manifest_ref = new_ref("target_root_manifest")
                    receipt = _receipt(
                        "research_memory",
                        RM_TARGET_ROOT_COMPLETION_MANIFEST_RECEIPT_KIND,
                        new_ref("rm_target_root_manifest_receipt"),
                        manifest_ref,
                        {
                            "manifest_ref": manifest_ref,
                            "payload_hash": payload_hash,
                            **payload,
                        },
                    )
                    connection.execute(
                        text(
                            "INSERT INTO rm_target_root_completion_manifests "
                            "(manifest_ref, completion_ref, target_ref, "
                            "target_run_ref, workspace_ref, "
                            "implementation_revision_ref, "
                            "implementation_tree_hash, result_document_path, "
                            "result_document_json, result_document_hash, "
                            "artifact_snapshot_hash, entries_json, entries_hash, "
                            "completion_receipt_ref, completion_receipt_hash, "
                            "payload_json, payload_hash, idempotency_key, "
                            "request_hash, receipt_ref, receipt_hash, accepted_at) "
                            "VALUES (:manifest_ref, :completion_ref, :target_ref, "
                            ":target_run_ref, :workspace_ref, "
                            ":implementation_revision_ref, "
                            ":implementation_tree_hash, :result_document_path, "
                            ":result_document_json, :result_document_hash, "
                            ":artifact_snapshot_hash, :entries_json, "
                            ":entries_hash, :completion_receipt_ref, "
                            ":completion_receipt_hash, :payload_json, "
                            ":payload_hash, :idempotency_key, :request_hash, "
                            ":receipt_ref, :receipt_hash, :accepted_at)"
                        ),
                        {
                            **payload,
                            "manifest_ref": manifest_ref,
                            "result_document_json": canonical_json(
                                frozen.result_document.as_dict()
                            ),
                            "entries_json": canonical_json(entries_value),
                            "entries_hash": canonical_hash(entries_value),
                            "completion_receipt_ref": (
                                completion.receipt.receipt_ref
                            ),
                            "completion_receipt_hash": (
                                completion.receipt.payload_hash
                            ),
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
                            "revision + 1, target_root_completion_manifest_count "
                            "= target_root_completion_manifest_count + 1 WHERE "
                            "singleton = 'owner'"
                        )
                    )
                    self._feed.record(
                        connection,
                        "research_memory.target_root_manifest_accepted",
                        {
                            "manifest_ref": manifest_ref,
                            "completion_ref": completion.completion_ref,
                            "target_ref": completion.handle.target_ref,
                            "receipt_ref": receipt.receipt_ref,
                        },
                    )
        except IntegrityError as error:
            raise OwnerConflict("target_root_manifest_conflict") from error
        accepted = self.query(manifest_ref)
        if accepted is None:
            raise OwnerConflict("target_root_manifest_integrity_invalid")
        return accepted

    @staticmethod
    def candidate_rejection_feedback(code: str) -> str | None:
        """Describe only root-correctable RM candidate failures."""

        return _RM_RECOVERABLE_CANDIDATE_FEEDBACK.get(code)

    def issue_candidate_rejection(
        self,
        completion: AcceptedTargetRootCompletion,
        *,
        code: str | None = None,
        feedback: str | None = None,
    ) -> TargetRootOwnerRejection:
        """Issue the exact RM rejection stored with a failed candidate."""

        current = self._lifecycle.query_completion_by_ref(
            completion.completion_ref
        )
        stored_code = completion.candidate_rejection_code
        stored_feedback = completion.candidate_rejection_feedback
        if stored_code is not None or stored_feedback is not None:
            if code is not None or feedback is not None:
                raise OwnerConflict("target_root_completion_rejection_invalid")
            code = stored_code
            feedback = stored_feedback
        if (
            current != completion
            or type(code) is not str
            or type(feedback) is not str
            or _RM_RECOVERABLE_CANDIDATE_FEEDBACK.get(code) != feedback
            or (
                stored_code is not None
                and (
                    completion.implementation_revision_ref is not None
                    or completion.implementation_tree_hash is not None
                    or completion.result_document_hash is not None
                    or completion.artifact_snapshot_hash is not None
                )
            )
        ):
            raise OwnerConflict("target_root_completion_rejection_invalid")
        identity_hash = canonical_hash(
            {
                "schema_ref": (
                    "meta-research/rm-target-root-candidate-rejection/v1"
                ),
                "completion_ref": completion.completion_ref,
                "code": code,
                "feedback": feedback,
            }
        )
        rejection_ref = "rm_target_root_rejection_" + identity_hash[:40]
        return TargetRootOwnerRejection(
            issuer="research_memory",
            rejection_ref=rejection_ref,
            code=code,
            feedback=feedback,
            receipt=_receipt(
                "research_memory",
                "target_root_completion_rejected",
                "rm_target_root_rejection_receipt_" + identity_hash[:40],
                completion.completion_ref,
                {
                    "rejection_ref": rejection_ref,
                    "completion_ref": completion.completion_ref,
                    "code": code,
                    "feedback": feedback,
                },
            ),
        )

    def query_for_completion(
        self, completion_ref: str
    ) -> AcceptedTargetRootCompletionManifest | None:
        with self._database.read() as connection:
            rows = connection.execute(
                text(
                    "SELECT manifest_ref FROM "
                    "rm_target_root_completion_manifests WHERE "
                    "completion_ref = :completion_ref"
                ),
                {"completion_ref": completion_ref},
            ).all()
        if not rows:
            return None
        if len(rows) != 1:
            raise OwnerConflict("target_root_manifest_integrity_invalid")
        return self.query(str(rows[0].manifest_ref))

    def materialize_target_commit_input(
        self,
        *,
        target_commit_ref: str,
        manifest: AcceptedTargetRootCompletionManifest,
    ) -> FrozenTargetCommitInput:
        """Return one root manifest with every byte re-read from RM custody."""

        current = self.query(manifest.manifest_ref)
        if current != manifest or not target_commit_ref:
            raise OwnerConflict("target_root_upstream_input_invalid")
        artifacts: list[FrozenTargetCommitInputArtifact] = []
        for entry in current.entries:
            try:
                materialized = self._asset_memory.materialize_asset(
                    entry.binding.version_ref
                )
            except Exception as error:
                raise OwnerConflict("target_root_upstream_input_invalid") from error
            content = getattr(materialized, "content", None)
            try:
                self._verify_binding(entry.binding, expected=content)
            except OwnerConflict as error:
                raise OwnerConflict("target_root_upstream_input_invalid") from error
            if (
                type(content) is not bytes
                or len(content) != entry.byte_count
                or hashlib.sha256(content).hexdigest() != entry.content_hash
            ):
                raise OwnerConflict("target_root_upstream_input_invalid")
            artifacts.append(
                FrozenTargetCommitInputArtifact(
                    ordinal=entry.ordinal,
                    role=entry.role,
                    declared_relative_path=entry.declared_relative_path,
                    artifact_kind=entry.artifact_kind,
                    media_type=entry.media_type,
                    version_ref=entry.binding.version_ref,
                    content_hash=entry.content_hash,
                    tree_hash=entry.tree_hash,
                    content=content,
                )
            )
        return FrozenTargetCommitInput(
            target_commit_ref=target_commit_ref,
            target_ref=current.target_ref,
            target_run_ref=current.target_run_ref,
            manifest_ref=current.manifest_ref,
            manifest_payload_hash=current.payload_hash,
            manifest_receipt_ref=current.receipt.receipt_ref,
            manifest_content=canonical_json(
                projection_plain_value(current)
            ).encode("utf-8"),
            artifacts=tuple(artifacts),
        )

    def query(
        self, manifest_ref: str
    ) -> AcceptedTargetRootCompletionManifest | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rm_target_root_completion_manifests WHERE "
                    "manifest_ref = :manifest_ref"
                ),
                {"manifest_ref": manifest_ref},
            ).first()
        if row is None:
            return None
        completion = self._lifecycle.query_completion_by_ref(
            str(row.completion_ref)
        )
        if completion is None or completion.completion_ref != row.completion_ref:
            raise OwnerConflict("target_root_manifest_integrity_invalid")
        try:
            entries_value = json.loads(row.entries_json)
            result_value = json.loads(row.result_document_json)
            payload_value = json.loads(row.payload_json)
            result_document = _decode_result_document_value(result_value)
            entries = tuple(
                _entry_from_value(value) for value in cast(list[object], entries_value)
            )
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise OwnerConflict("target_root_manifest_integrity_invalid") from error
        if (
            type(entries_value) is not list
            or not entries
            or tuple(entry.ordinal for entry in entries) != tuple(range(len(entries)))
            or len({entry.declared_relative_path for entry in entries})
            != len(entries)
        ):
            raise OwnerConflict("target_root_manifest_integrity_invalid")
        for entry in entries:
            try:
                materialized = self._asset_memory.materialize_asset(
                    entry.binding.version_ref
                )
                content = getattr(materialized, "content", None)
                self._verify_binding(entry.binding, expected=content)
            except OwnerConflict:
                raise
            except Exception as error:
                raise OwnerConflict(
                    "target_root_manifest_integrity_invalid"
                ) from error
            if (
                type(content) is not bytes
                or len(content) != entry.byte_count
                or hashlib.sha256(content).hexdigest() != entry.content_hash
            ):
                raise OwnerConflict("target_root_manifest_integrity_invalid")
            if entry.artifact_kind == "directory":
                try:
                    parsed = parse_target_implementation_bundle(
                        content, expected_tree_sha256=entry.tree_hash
                    )
                except TargetImplementationBundleError as error:
                    raise OwnerConflict(
                        "target_root_manifest_integrity_invalid"
                    ) from error
                if parsed.bundle_sha256 != entry.content_hash:
                    raise OwnerConflict("target_root_manifest_integrity_invalid")
            elif entry.tree_hash != entry.content_hash:
                raise OwnerConflict("target_root_manifest_integrity_invalid")
        entries_document = [entry.as_dict() for entry in entries]
        payload = {
            "completion_ref": completion.completion_ref,
            "target_ref": completion.handle.target_ref,
            "target_run_ref": completion.handle.target_run_ref,
            "workspace_ref": completion.workspace_ref,
            "implementation_revision_ref": completion.implementation_revision_ref,
            "implementation_tree_hash": completion.implementation_tree_hash,
            "result_document_path": completion.handoff.result_document_path,
            "result_document": result_document.as_dict(),
            "result_document_hash": completion.result_document_hash,
            "artifact_snapshot_hash": completion.artifact_snapshot_hash,
            "entries": entries_document,
            "completion_receipt": completion.receipt.as_public_dict(),
        }
        payload_hash = canonical_hash(payload)
        request_hash = canonical_hash(
            {"command": "accept_target_root_completion_manifest", **payload}
        )
        idempotency_key = "target-root-manifest:" + canonical_hash(
            {
                "completion_ref": completion.completion_ref,
                "artifact_snapshot_hash": completion.artifact_snapshot_hash,
            }
        )
        receipt = _receipt(
            "research_memory",
            RM_TARGET_ROOT_COMPLETION_MANIFEST_RECEIPT_KIND,
            str(row.receipt_ref),
            str(row.manifest_ref),
            {
                "manifest_ref": row.manifest_ref,
                "payload_hash": payload_hash,
                **payload,
            },
        )
        if (
            row.target_run_ref != completion.handle.target_run_ref
            or row.workspace_ref != completion.workspace_ref
            or row.implementation_revision_ref
            != completion.implementation_revision_ref
            or row.implementation_tree_hash != completion.implementation_tree_hash
            or row.result_document_path != completion.handoff.result_document_path
            or canonical_json(result_value) != row.result_document_json
            or row.result_document_hash != completion.result_document_hash
            or result_document.content_hash != completion.result_document_hash
            or row.artifact_snapshot_hash != completion.artifact_snapshot_hash
            or canonical_json(entries_document) != row.entries_json
            or row.entries_hash != canonical_hash(entries_document)
            or row.completion_receipt_ref != completion.receipt.receipt_ref
            or row.completion_receipt_hash != completion.receipt.payload_hash
            or payload_value != payload
            or row.payload_json != canonical_json(payload)
            or row.payload_hash != payload_hash
            or row.idempotency_key != idempotency_key
            or row.request_hash != request_hash
            or row.receipt_hash != receipt.payload_hash
        ):
            raise OwnerConflict("target_root_manifest_integrity_invalid")
        result_entries = tuple(
            entry
            for entry in entries
            if entry.declared_relative_path
            == completion.handoff.result_document_path
            and entry.role == "result"
        )
        if len(result_entries) != 1:
            raise OwnerConflict("target_root_manifest_integrity_invalid")
        result_bytes = getattr(
            self._asset_memory.materialize_asset(
                result_entries[0].binding.version_ref
            ),
            "content",
            None,
        )
        if result_bytes != canonical_json(result_document.as_dict()).encode("utf-8"):
            raise OwnerConflict("target_root_manifest_integrity_invalid")
        return AcceptedTargetRootCompletionManifest(
            manifest_ref=str(row.manifest_ref),
            completion_ref=completion.completion_ref,
            target_ref=completion.handle.target_ref,
            target_run_ref=completion.handle.target_run_ref,
            workspace_ref=completion.workspace_ref,
            implementation_revision_ref=completion.implementation_revision_ref,
            implementation_tree_hash=completion.implementation_tree_hash,
            result_document_path=completion.handoff.result_document_path,
            result_document=result_document,
            result_document_hash=completion.result_document_hash,
            artifact_snapshot_hash=completion.artifact_snapshot_hash,
            entries=entries,
            payload_hash=payload_hash,
            receipt=receipt,
            accepted_at=float(row.accepted_at),
        )

    def _verify_binding(
        self, binding: AcceptedAssetBinding, *, expected: object
    ) -> None:
        if type(binding) is not AcceptedAssetBinding or type(expected) is not bytes:
            raise OwnerConflict("target_root_artifact_integrity_invalid")
        self._asset_memory.verify_asset_binding(
            asset_ref=binding.asset_ref,
            version_ref=binding.version_ref,
            content_hash=binding.content_hash,
            manifest_hash=binding.manifest_hash,
            receipt=binding.receipt,
        )
        if hashlib.sha256(expected).hexdigest() != binding.content_hash:
            raise OwnerConflict("target_root_artifact_integrity_invalid")


class TargetRunFinalizer:
    """Deep, replayable finalization API used by the light daemon."""

    def __init__(
        self,
        *,
        lifecycle: SQLiteTargetRootLifecycleAuthority,
        memory: SQLiteTargetRootCompletionMemoryAuthority,
        workspace_resolver: TargetRootWorkspaceResolver,
        evidence_reader: TargetRootCompletionEvidenceReader,
        measurement_authority: TargetMeasurementAuthorityReader | None = None,
        graph_authority: TargetRootGraphFinalizationAuthority | None = None,
    ) -> None:
        self._lifecycle = lifecycle
        self._memory = memory
        self._workspace_resolver = workspace_resolver
        self._evidence_reader = evidence_reader
        self._measurement_authority = measurement_authority
        self._graph_authority = graph_authority

    def materialize_inputs(self, *, handle: TargetWorkHandle) -> tuple[str, ...]:
        """Project issuer-owned inputs into the root workspace before startup."""

        accepted = self._resolve_target_commit_inputs(handle)
        return self._workspace_resolver.materialize_target_workspace_inputs(
            handle=handle,
            accepted_target_commit_inputs=accepted,
        )

    def finalize(
        self,
        *,
        handle: TargetWorkHandle,
        evidence: TargetRootCompletionEvidence,
    ) -> TargetRootFinalizationResult:
        """Freeze once, then idempotently advance the sole RM/RG handoff."""

        _validate_evidence(evidence, handle=handle)
        try:
            evidence_content_hash = (
                self._evidence_reader.verify_target_root_completion_evidence(
                    handle=handle,
                    evidence=evidence,
                    handoff=evidence.handoff,
                )
            )
        except OwnerConflict:
            raise
        except Exception as error:
            raise OwnerConflict("target_root_completion_evidence_invalid") from error
        if (
            type(evidence_content_hash) is not str
            or len(evidence_content_hash) != 64
        ):
            raise OwnerConflict("target_root_completion_evidence_invalid")

        # The Harness verifier above proves the root provider/process tree is
        # drained.  Re-resolve RG -> RM input custody now and compare the exact
        # workspace projection before the first AR completion or RM/RG write.
        if (
            handle.accepted_input_target_commit_refs
            or handle.accepted_input_asset_proofs
        ):
            accepted_inputs = self._resolve_target_commit_inputs(handle)
            self._workspace_resolver.verify_target_workspace_inputs(
                handle=handle,
                accepted_target_commit_inputs=accepted_inputs,
            )

        latest = self._lifecycle.query_completion(handle.target_ref)
        completion = None
        manifest = None
        handoff: TargetCompletionHandoff | None = None
        pinned_workspace: _PinnedWorkspaceRoot | None = None
        if latest is not None:
            rejection = self._lifecycle.query_completion_rejection(
                latest.completion_ref
            )
            matches_latest = (
                latest.handle == handle
                and latest.harness_operation_ref == evidence.operation_ref
                and latest.evidence_ref == evidence.evidence_ref
                and latest.evidence_content_hash == evidence_content_hash
                and (
                    evidence.handoff is None
                    or latest.handoff == evidence.handoff
                )
                and (
                    evidence.workspace_ref is None
                    or latest.workspace_ref == evidence.workspace_ref
                )
            )
            if matches_latest:
                completion = latest
                handoff = latest.handoff
                if rejection is not None:
                    return self._revision_result(rejection)
                if completion.candidate_rejection_code is not None:
                    owner_rejection = self._memory.issue_candidate_rejection(
                        completion
                    )
                    recorded = self._record_rejection(
                        completion=completion,
                        manifest_ref=None,
                        rejection=owner_rejection,
                    )
                    return self._revision_result(recorded)
                manifest = self._memory.query_for_completion(
                    completion.completion_ref
                )
            elif rejection is None:
                raise OwnerConflict("target_root_completion_conflict")

        if handoff is None:
            try:
                if evidence.handoff is not None:
                    handoff = evidence.handoff
                else:
                    pinned_workspace = self._resolve_workspace(handle)
                    if evidence.workspace_ref != pinned_workspace.workspace_ref:
                        raise OwnerConflict(
                            "target_root_completion_evidence_invalid"
                        )
                    handoff = _system_target_completion_handoff(
                        handle=handle,
                        evidence=evidence,
                        root_descriptor=pinned_workspace.descriptor,
                    )
                validate_target_completion_handoff(
                    handoff,
                    expected_target_ref=handle.target_ref,
                    expected_target_run_ref=handle.target_run_ref,
                )
                canonical_json(projection_plain_value(handoff))
            except OwnerConflict:
                if pinned_workspace is not None:
                    os.close(pinned_workspace.descriptor)
                raise
            except Exception as error:
                if pinned_workspace is not None:
                    os.close(pinned_workspace.descriptor)
                raise OwnerConflict(
                    "target_root_completion_evidence_invalid"
                ) from error

        if manifest is None:
            if pinned_workspace is None:
                pinned_workspace = self._resolve_workspace(handle)
                if (
                    evidence.workspace_ref is not None
                    and evidence.workspace_ref != pinned_workspace.workspace_ref
                ):
                    os.close(pinned_workspace.descriptor)
                    raise OwnerConflict(
                        "target_root_completion_evidence_invalid"
                    )
            workspace_ref = pinned_workspace.workspace_ref
            try:
                frozen = self._freeze(
                    handle=handle,
                    handoff=handoff,
                    resolved_workspace=pinned_workspace,
                    system_owned=evidence.handoff is None,
                )
            except OwnerConflict as error:
                feedback = self._memory.candidate_rejection_feedback(error.code)
                if feedback is None:
                    raise
                completion = self._lifecycle.accept_completion(
                    handle=handle,
                    handoff=handoff,
                    harness_operation_ref=evidence.operation_ref,
                    evidence_ref=evidence.evidence_ref,
                    evidence_content_hash=evidence_content_hash,
                    workspace_ref=workspace_ref,
                    implementation_revision_ref=None,
                    implementation_tree_hash=None,
                    result_document_hash=None,
                    artifact_snapshot_hash=None,
                    candidate_rejection_code=error.code,
                    candidate_rejection_feedback=feedback,
                    idempotency_key="target-root-completion:"
                    + canonical_hash(
                        {
                            "target_ref": handle.target_ref,
                            "target_run_ref": handle.target_run_ref,
                            "evidence_content_hash": evidence_content_hash,
                        }
                    ),
                )
                owner_rejection = self._memory.issue_candidate_rejection(
                    completion
                )
                rejection = self._record_rejection(
                    completion=completion,
                    manifest_ref=None,
                    rejection=owner_rejection,
                )
                return self._revision_result(rejection)
            finally:
                os.close(pinned_workspace.descriptor)
            completion = self._lifecycle.accept_completion(
                handle=handle,
                handoff=handoff,
                harness_operation_ref=evidence.operation_ref,
                evidence_ref=evidence.evidence_ref,
                evidence_content_hash=evidence_content_hash,
                workspace_ref=frozen.workspace_ref,
                implementation_revision_ref=frozen.implementation_revision_ref,
                implementation_tree_hash=frozen.implementation_tree_hash,
                result_document_hash=frozen.result_document_hash,
                artifact_snapshot_hash=frozen.artifact_snapshot_hash,
                idempotency_key="target-root-completion:"
                + canonical_hash(
                    {
                        "target_ref": handle.target_ref,
                        "target_run_ref": handle.target_run_ref,
                        "evidence_content_hash": evidence_content_hash,
                    }
                ),
            )
            try:
                memory_result = self._memory.accept(
                    completion=completion, frozen=frozen
                )
            except OwnerConflict as error:
                feedback = self._memory.candidate_rejection_feedback(error.code)
                if feedback is None:
                    raise
                memory_result = self._memory.issue_candidate_rejection(
                    completion,
                    code=error.code,
                    feedback=feedback,
                )
            if type(memory_result) is TargetRootOwnerRejection:
                rejection = self._record_rejection(
                    completion=completion,
                    manifest_ref=None,
                    rejection=memory_result,
                )
                return self._revision_result(rejection)
            manifest = memory_result

        if completion is None:
            raise OwnerConflict("target_root_completion_integrity_invalid")
        graph = self._graph_authority
        if graph is None:
            return TargetRootFinalizationResult(
                status="rm_accepted",
                target_ref=handle.target_ref,
                target_run_ref=handle.target_run_ref,
                completion_ref=completion.completion_ref,
                manifest_ref=manifest.manifest_ref,
                target_commit_ref=None,
                pending_code=TARGET_ROOT_RG_PENDING_CODE,
                completion_generation=completion.generation,
            )
        try:
            accepted = graph.accept_target_commit_from_root_completion(
                completion=completion,
                manifest=manifest,
                result_document=manifest.result_document,
                idempotency_key="target-root-commit:"
                + canonical_hash(
                    {
                        "completion_ref": completion.completion_ref,
                        "manifest_ref": manifest.manifest_ref,
                        "artifact_snapshot_hash": manifest.artifact_snapshot_hash,
                    }
                ),
            )
        except OwnerConflict as error:
            if not error.code.endswith("_unavailable"):
                raise
            return TargetRootFinalizationResult(
                status="rm_accepted",
                target_ref=handle.target_ref,
                target_run_ref=handle.target_run_ref,
                completion_ref=completion.completion_ref,
                manifest_ref=manifest.manifest_ref,
                target_commit_ref=None,
                pending_code=error.code,
                completion_generation=completion.generation,
            )
        if type(accepted) is TargetRootOwnerRejection:
            rejection = self._record_rejection(
                completion=completion,
                manifest_ref=manifest.manifest_ref,
                rejection=accepted,
            )
            return self._revision_result(rejection)
        if (
            type(accepted) is not TargetRootGraphAcceptance
            or accepted.target_ref != handle.target_ref
            or accepted.target_run_ref != handle.target_run_ref
            or not accepted.target_commit_ref
            or accepted.receipt.issuer != "research_graph"
            or accepted.receipt.subject_ref != accepted.target_commit_ref
        ):
            raise OwnerConflict("target_root_graph_acceptance_invalid")
        return TargetRootFinalizationResult(
            status="completed",
            target_ref=handle.target_ref,
            target_run_ref=handle.target_run_ref,
            completion_ref=completion.completion_ref,
            manifest_ref=manifest.manifest_ref,
            target_commit_ref=accepted.target_commit_ref,
            pending_code=None,
            completion_generation=completion.generation,
        )

    def _record_rejection(
        self,
        *,
        completion: AcceptedTargetRootCompletion,
        manifest_ref: str | None,
        rejection: TargetRootOwnerRejection,
    ) -> AcceptedTargetRootCompletionRejection:
        if (
            rejection.issuer not in {"research_memory", "research_graph"}
            or not rejection.rejection_ref
            or not rejection.code
            or not rejection.feedback
            or rejection.receipt.issuer != rejection.issuer
            or rejection.receipt.kind != "target_root_completion_rejected"
            or rejection.receipt.subject_ref != completion.completion_ref
        ):
            raise OwnerConflict("target_root_completion_rejection_invalid")
        return self._lifecycle.reject_completion(
            completion=completion,
            manifest_ref=manifest_ref,
            issuer=rejection.issuer,
            rejection_ref=rejection.rejection_ref,
            code=rejection.code,
            feedback=rejection.feedback,
            receipt=rejection.receipt,
            idempotency_key="target-root-rejection:"
            + canonical_hash(
                {
                    "completion_ref": completion.completion_ref,
                    "rejection_ref": rejection.rejection_ref,
                    "issuer_receipt_ref": rejection.receipt.receipt_ref,
                }
            ),
        )

    @staticmethod
    def _revision_result(
        rejection: AcceptedTargetRootCompletionRejection,
    ) -> TargetRootFinalizationResult:
        return TargetRootFinalizationResult(
            status="revision_required",
            target_ref=rejection.target_ref,
            target_run_ref=rejection.target_run_ref,
            completion_ref=rejection.completion_ref,
            manifest_ref=rejection.manifest_ref,
            target_commit_ref=None,
            pending_code=rejection.code,
            completion_generation=rejection.generation,
            rejection_ref=rejection.rejection_ref,
            rejection_issuer=rejection.issuer,
            rejection_feedback=rejection.feedback,
        )

    def _resolve_target_commit_inputs(
        self, handle: TargetWorkHandle
    ) -> tuple[FrozenTargetCommitInput, ...]:
        required = handle.accepted_input_target_commit_refs
        if not required:
            return ()
        graph = self._graph_authority
        if graph is None:
            raise OwnerConflict("target_root_upstream_input_authority_unavailable")
        quest_ref = self._workspace_resolver.query_target_workspace_quest_ref(handle)
        try:
            commits = graph.query_target_commits_for_quest(quest_ref)
        except Exception as error:
            raise OwnerConflict(
                "target_root_upstream_input_authority_invalid"
            ) from error
        by_ref: dict[str, object] = {}
        for commit in commits:
            commit_ref = getattr(commit, "commit_ref", None)
            if type(commit_ref) is not str or not commit_ref or commit_ref in by_ref:
                raise OwnerConflict("target_root_upstream_input_authority_invalid")
            by_ref[commit_ref] = commit
        accepted: list[FrozenTargetCommitInput] = []
        for commit_ref in required:
            commit = by_ref.get(commit_ref)
            target_ref = getattr(commit, "target_ref", None)
            receipt = getattr(commit, "receipt", None)
            if (
                commit is None
                or type(target_ref) is not str
                or not target_ref
                or type(receipt) is not AcceptanceReceipt
                or receipt.issuer != "research_graph"
                or receipt.kind != "target_commit_accepted"
                or receipt.subject_ref != commit_ref
            ):
                raise OwnerConflict("target_root_upstream_input_invalid")
            transition = graph.query_target_frontier_commit_transition(target_ref)
            terminal = getattr(transition, "canonical_terminal", None)
            manifest_ref = getattr(terminal, "asset_manifest_ref", None)
            if (
                transition is None
                or getattr(transition, "target_commit_ref", None) != commit_ref
                or getattr(transition, "target_ref", None) != target_ref
                or getattr(transition, "target_run_ref", None)
                != getattr(commit, "target_run_ref", None)
                or getattr(transition, "issuer_receipt", None) != receipt
                or type(manifest_ref) is not str
                or not manifest_ref
            ):
                raise OwnerConflict("target_root_upstream_input_invalid")
            root_manifest = self._memory.query(manifest_ref)
            if root_manifest is not None:
                frozen = self._memory.materialize_target_commit_input(
                    target_commit_ref=commit_ref,
                    manifest=root_manifest,
                )
            else:
                frozen = (
                    self._workspace_resolver.resolve_generic_target_commit_input(
                        transition
                    )
                )
            if (
                frozen is None
                or frozen.target_commit_ref != commit_ref
                or frozen.target_ref != target_ref
                or frozen.target_run_ref != getattr(commit, "target_run_ref", None)
                or frozen.manifest_ref != manifest_ref
            ):
                raise OwnerConflict("target_root_upstream_input_invalid")
            accepted.append(frozen)
        return tuple(accepted)

    def _resolve_workspace(
        self, handle: TargetWorkHandle
    ) -> _PinnedWorkspaceRoot:
        """Resolve the stable workspace identity before candidate validation."""

        try:
            workspace_ref, workspace_root = (
                self._workspace_resolver.resolve_target_workspace(
                    target_ref=handle.target_ref,
                    target_run_ref=handle.target_run_ref,
                    root_session_ref=handle.root_session_ref,
                    attempt_ref=handle.execution_attempt_ref,
                    fence_ref=handle.execution_fence_ref,
                )
            )
        except OwnerConflict:
            raise
        except Exception as error:
            raise OwnerConflict("target_root_workspace_unavailable") from error
        if (
            type(workspace_ref) is not str
            or not workspace_ref
            or not isinstance(workspace_root, Path)
            or not workspace_root.is_absolute()
        ):
            raise OwnerConflict("target_root_workspace_unavailable")
        return _pin_workspace_root(workspace_ref, workspace_root)

    def _freeze(
        self,
        *,
        handle: TargetWorkHandle,
        handoff: TargetCompletionHandoff,
        resolved_workspace: _PinnedWorkspaceRoot,
        system_owned: bool,
    ) -> _FrozenWorkspace:
        workspace_ref = resolved_workspace.workspace_ref
        root_descriptor = resolved_workspace.descriptor
        if system_owned:
            declared_implementation = tuple(
                artifact
                for artifact in handoff.artifacts
                if artifact.role == "implementation"
            )
            if (
                len(declared_implementation) != 1
                or declared_implementation[0].relative_path != "implementation"
            ):
                raise OwnerConflict("target_implementation_workspace_invalid")
        frozen_artifacts: list[_FrozenArtifact] = []
        total_artifact_bytes = 0
        for ordinal, artifact in enumerate(handoff.artifacts):
            frozen_artifact = _freeze_artifact(
                root_descriptor,
                ordinal,
                artifact.role,
                artifact.relative_path,
            )
            total_artifact_bytes += len(frozen_artifact.content)
            if total_artifact_bytes > TARGET_ROOT_MAX_ARTIFACT_SET_BYTES:
                raise OwnerConflict("target_root_artifact_set_too_large")
            frozen_artifacts.append(frozen_artifact)
        artifacts = tuple(frozen_artifacts)
        snapshot_hash = canonical_hash(
            {
                "schema_ref": "meta-research/target-root-artifact-snapshot/v1",
                "target_ref": handle.target_ref,
                "target_run_ref": handle.target_run_ref,
                "workspace_ref": workspace_ref,
                "artifacts": [item.snapshot_value() for item in artifacts],
            }
        )
        implementation = tuple(
            item for item in artifacts if item.role == "implementation"
        )
        if system_owned and (
            len(implementation) != 1
            or implementation[0].declared_relative_path != "implementation"
            or implementation[0].artifact_kind != "directory"
        ):
            raise OwnerConflict("target_implementation_workspace_invalid")
        if not implementation:
            raise OwnerConflict("target_root_implementation_missing")
        implementation_tree_hash = (
            implementation[0].tree_hash
            if len(implementation) == 1
            else canonical_hash(
                {
                    "schema_ref": (
                        "meta-research/target-root-implementation-set/v1"
                    ),
                    "artifacts": [item.snapshot_value() for item in implementation],
                }
            )
        )
        implementation_revision_ref = (
            "target_impl_" + implementation_tree_hash
        )
        result_matches = tuple(
            item
            for item in artifacts
            if item.role == "result"
            and item.declared_relative_path == handoff.result_document_path
        )
        if len(result_matches) != 1 or result_matches[0].artifact_kind != "file":
            raise OwnerConflict("target_root_result_document_invalid")
        result = _decode_result_document_bytes(result_matches[0].content)
        return _FrozenWorkspace(
            workspace_ref=workspace_ref,
            artifacts=artifacts,
            implementation_revision_ref=implementation_revision_ref,
            implementation_tree_hash=implementation_tree_hash,
            result_document=result,
            result_document_hash=result.content_hash,
            artifact_snapshot_hash=snapshot_hash,
        )

    def _verify_result_semantics(
        self,
        target_ref: str,
        handoff: TargetCompletionHandoff,
        result: TargetRootResultDocument,
    ) -> None:
        reader = self._measurement_authority
        if reader is None:
            return
        authority = reader.query_target_measurement_domain_authority(target_ref)
        contract = getattr(authority, "measurement_contract", None)
        protocol = getattr(contract, "protocol_version", None)
        required = getattr(protocol, "required_metric_keys", None)
        optional = getattr(protocol, "optional_metric_keys", None)
        checkpoint_policy = getattr(contract, "checkpoint_policy", None)
        if (
            authority is None
            or getattr(authority, "target_ref", None) != target_ref
            or getattr(contract, "result_schema_ref", None) != result.schema_ref
            or type(required) is not tuple
            or type(optional) is not tuple
            or checkpoint_policy not in {"forbidden", "optional", "required"}
        ):
            raise OwnerConflict("target_root_measurement_authority_invalid")
        metric_keys = set(result.metrics)
        if not set(required) <= metric_keys or not metric_keys <= set(
            (*required, *optional)
        ):
            raise OwnerConflict("target_root_result_metrics_invalid")
        checkpoint_count = sum(
            artifact.role == "checkpoint" for artifact in handoff.artifacts
        )
        if (checkpoint_policy == "required" and checkpoint_count == 0) or (
            checkpoint_policy == "forbidden" and checkpoint_count != 0
        ):
            raise OwnerConflict("target_root_checkpoint_policy_invalid")


def _system_target_completion_handoff(
    *,
    handle: TargetWorkHandle,
    evidence: TargetRootCompletionEvidence,
    root_descriptor: int,
) -> TargetCompletionHandoff:
    """Derive the internal handoff from fixed, descriptor-safe Owner paths."""

    if evidence.final_text is None or evidence.final_text_sha256 is None:
        raise OwnerConflict("target_root_completion_evidence_invalid")
    artifacts = [
        TargetCompletionArtifact(role=role, relative_path=relative_path)
        for role, relative_path in _SYSTEM_TARGET_COMPLETION_REQUIRED_ARTIFACTS
    ]
    for role, relative_path in _SYSTEM_TARGET_COMPLETION_OPTIONAL_ARTIFACTS:
        if _workspace_artifact_exists(root_descriptor, relative_path):
            artifacts.append(
                TargetCompletionArtifact(
                    role=role,
                    relative_path=relative_path,
                )
            )
    final_text_bytes = evidence.final_text.encode("utf-8")
    summary = (
        "System-bound Target root completion; final_text_sha256="
        f"{evidence.final_text_sha256}; "
        f"final_text_utf8_bytes={len(final_text_bytes)}."
    )
    return TargetCompletionHandoff(
        schema_ref="meta-research/target-completion-handoff/v1",
        target_ref=handle.target_ref,
        target_run_ref=handle.target_run_ref,
        status="completed",
        artifacts=tuple(artifacts),
        result_document_path="outputs/result.json",
        summary=summary,
    )


def _workspace_artifact_exists(
    root_descriptor: int,
    relative_path: str,
) -> bool:
    """Probe one fixed path without following a workspace symlink."""

    try:
        descriptor, _info = _open_workspace_artifact(
            root_descriptor,
            relative_path,
        )
    except OwnerConflict as error:
        if error.code == "target_root_artifact_missing":
            return False
        if error.code == "target_root_artifact_symlink_forbidden":
            # Include the fixed declaration so the normal freeze path records
            # the same fail-closed error as a recoverable candidate rejection.
            return True
        raise
    os.close(descriptor)
    return True


def _completion_evidence_mode_is_valid(
    evidence: TargetRootCompletionEvidence,
) -> bool:
    if evidence.handoff is not None:
        return (
            type(evidence.handoff) is TargetCompletionHandoff
            and evidence.workspace_ref is None
            and evidence.final_text is None
            and evidence.final_text_sha256 is None
        )
    if (
        type(evidence.workspace_ref) is not str
        or not evidence.workspace_ref
        or type(evidence.final_text) is not str
        or not evidence.final_text
        or type(evidence.final_text_sha256) is not str
        or len(evidence.final_text_sha256) != 64
    ):
        return False
    try:
        final_text_bytes = evidence.final_text.encode("utf-8")
    except UnicodeError:
        return False
    return hashlib.sha256(final_text_bytes).hexdigest() == (
        evidence.final_text_sha256
    )


def _validate_evidence(
    evidence: TargetRootCompletionEvidence, *, handle: TargetWorkHandle
) -> None:
    if (
        type(evidence) is not TargetRootCompletionEvidence
        or evidence.target_ref != handle.target_ref
        or evidence.target_run_ref != handle.target_run_ref
        or evidence.attempt_ref != handle.execution_attempt_ref
        or evidence.root_session_ref != handle.root_session_ref
        or evidence.fence_ref != handle.execution_fence_ref
        or type(evidence.native_session_ref) is not str
        or not evidence.native_session_ref
        or type(evidence.operation_ref) is not str
        or not evidence.operation_ref
        or type(evidence.evidence_ref) is not str
        or not evidence.evidence_ref
        or type(evidence.attempt_generation) is not int
        or isinstance(evidence.attempt_generation, bool)
        or evidence.attempt_generation < 1
        or type(evidence.operation_generation) is not int
        or isinstance(evidence.operation_generation, bool)
        or evidence.operation_generation < 1
        or type(evidence.evidence_sequence) is not int
        or isinstance(evidence.evidence_sequence, bool)
        or evidence.evidence_sequence < 0
        or type(evidence.observed_at) is not float
        or not math.isfinite(evidence.observed_at)
        or not _completion_evidence_mode_is_valid(evidence)
    ):
        raise OwnerConflict("target_root_completion_evidence_invalid")


def _pin_workspace_root(workspace_ref: str, value: Path) -> _PinnedWorkspaceRoot:
    if any(part in {"", ".", ".."} for part in value.parts[1:]):
        raise OwnerConflict("target_root_workspace_invalid")
    try:
        descriptor = os.open("/", _directory_open_flags())
    except OSError as error:
        raise OwnerConflict("target_root_workspace_unavailable") from error
    try:
        for part in value.parts[1:]:
            next_descriptor = _open_directory_component(
                descriptor,
                part,
                missing_code="target_root_workspace_unavailable",
                symlink_code="target_root_workspace_invalid",
                invalid_code="target_root_workspace_invalid",
            )
            os.close(descriptor)
            descriptor = next_descriptor
        return _PinnedWorkspaceRoot(
            workspace_ref=workspace_ref,
            path=value,
            descriptor=descriptor,
        )
    except Exception:
        os.close(descriptor)
        raise


def _freeze_artifact(
    root_descriptor: int, ordinal: int, role: str, relative_path: str
) -> _FrozenArtifact:
    descriptor, info = _open_workspace_artifact(
        root_descriptor, relative_path
    )
    try:
        if stat.S_ISREG(info.st_mode):
            content = _read_stable_regular_file(descriptor, info)
            content_hash = hashlib.sha256(content).hexdigest()
            return _FrozenArtifact(
                ordinal=ordinal,
                role=role,
                declared_relative_path=relative_path,
                artifact_kind="file",
                media_type=(
                    "application/json"
                    if role == "result"
                    else "application/octet-stream"
                ),
                content=content,
                content_hash=content_hash,
                tree_hash=content_hash,
            )
        if stat.S_ISDIR(info.st_mode):
            try:
                first = build_target_implementation_bundle_from_open_directory(
                    descriptor
                )
                second = build_target_implementation_bundle_from_open_directory(
                    descriptor
                )
            except TargetImplementationBundleError as error:
                raise OwnerConflict(error.code) from error
            if first.bundle_bytes != second.bundle_bytes:
                raise OwnerConflict("target_root_workspace_changed")
            if len(first.bundle_bytes) > MAX_ASSET_BYTES:
                raise OwnerConflict("target_root_artifact_too_large")
            return _FrozenArtifact(
                ordinal=ordinal,
                role=role,
                declared_relative_path=relative_path,
                artifact_kind="directory",
                media_type="application/zip",
                content=first.bundle_bytes,
                content_hash=first.bundle_sha256,
                tree_hash=first.tree_sha256,
            )
        raise OwnerConflict("target_root_artifact_type_unsupported")
    finally:
        os.close(descriptor)


def _open_workspace_artifact(
    root_descriptor: int, relative_path: str
) -> tuple[int, os.stat_result]:
    parts = relative_path.split("/")
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise OwnerConflict("target_root_artifact_path_invalid")
    try:
        descriptor = os.open(
            ".", _directory_open_flags(), dir_fd=root_descriptor
        )
    except OSError as error:
        raise OwnerConflict("target_root_workspace_unavailable") from error
    try:
        for part in parts[:-1]:
            next_descriptor = _open_directory_component(
                descriptor,
                part,
                missing_code="target_root_artifact_missing",
                symlink_code="target_root_artifact_symlink_forbidden",
                invalid_code="target_root_artifact_type_unsupported",
            )
            os.close(descriptor)
            descriptor = next_descriptor
        artifact_descriptor, info = _open_artifact_component(
            descriptor, parts[-1]
        )
    except Exception:
        os.close(descriptor)
        raise
    os.close(descriptor)
    return artifact_descriptor, info


def _open_directory_component(
    parent_descriptor: int,
    name: str,
    *,
    missing_code: str,
    symlink_code: str,
    invalid_code: str,
) -> int:
    try:
        descriptor = os.open(
            name,
            _directory_open_flags(),
            dir_fd=parent_descriptor,
        )
    except FileNotFoundError as error:
        raise OwnerConflict(missing_code) from error
    except OSError as error:
        _raise_open_component_error(
            parent_descriptor,
            name,
            error,
            missing_code=missing_code,
            symlink_code=symlink_code,
            invalid_code=invalid_code,
        )
    try:
        opened = os.fstat(descriptor)
    except OSError as error:
        os.close(descriptor)
        raise OwnerConflict("target_root_artifact_unavailable") from error
    if not stat.S_ISDIR(opened.st_mode):
        os.close(descriptor)
        raise OwnerConflict(invalid_code)
    return descriptor


def _open_artifact_component(
    parent_descriptor: int, name: str
) -> tuple[int, os.stat_result]:
    try:
        listed = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError as error:
        raise OwnerConflict("target_root_artifact_missing") from error
    except OSError as error:
        raise OwnerConflict("target_root_artifact_unavailable") from error
    if stat.S_ISLNK(listed.st_mode):
        raise OwnerConflict("target_root_artifact_symlink_forbidden")
    flags = (
        _directory_open_flags()
        if stat.S_ISDIR(listed.st_mode)
        else os.O_RDONLY
        | os.O_CLOEXEC
        | os.O_NOFOLLOW
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except FileNotFoundError as error:
        raise OwnerConflict("target_root_artifact_missing") from error
    except OSError as error:
        try:
            current = os.stat(
                name, dir_fd=parent_descriptor, follow_symlinks=False
            )
        except FileNotFoundError as missing:
            raise OwnerConflict("target_root_artifact_missing") from missing
        except OSError as unavailable:
            raise OwnerConflict(
                "target_root_artifact_unavailable"
            ) from unavailable
        if stat.S_ISLNK(current.st_mode):
            raise OwnerConflict("target_root_artifact_symlink_forbidden") from error
        if _descriptor_stat_identity(listed) != _descriptor_stat_identity(current):
            raise OwnerConflict("target_root_workspace_changed") from error
        raise OwnerConflict("target_root_artifact_unavailable") from error
    try:
        opened = os.fstat(descriptor)
    except OSError as error:
        os.close(descriptor)
        raise OwnerConflict("target_root_artifact_unavailable") from error
    if _descriptor_stat_identity(listed) != _descriptor_stat_identity(opened):
        os.close(descriptor)
        raise OwnerConflict("target_root_workspace_changed")
    return descriptor, opened


def _raise_open_component_error(
    parent_descriptor: int,
    name: str,
    error: OSError,
    *,
    missing_code: str,
    symlink_code: str,
    invalid_code: str,
) -> None:
    try:
        current = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError as missing:
        raise OwnerConflict(missing_code) from missing
    except OSError as unavailable:
        raise OwnerConflict("target_root_artifact_unavailable") from unavailable
    if stat.S_ISLNK(current.st_mode):
        raise OwnerConflict(symlink_code) from error
    if not stat.S_ISDIR(current.st_mode):
        raise OwnerConflict(invalid_code) from error
    raise OwnerConflict("target_root_artifact_unavailable") from error


def _read_stable_regular_file(
    descriptor: int, before: os.stat_result
) -> bytes:
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise OwnerConflict("target_root_artifact_type_unsupported")
    if before.st_size > MAX_ASSET_BYTES:
        raise OwnerConflict("target_root_artifact_too_large")
    try:
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
    except OSError as error:
        raise OwnerConflict("target_root_artifact_unavailable") from error
    content = b"".join(chunks)
    if (
        _descriptor_stat_identity(before) != _descriptor_stat_identity(after)
        or len(content) != before.st_size
    ):
        raise OwnerConflict("target_root_workspace_changed")
    return content


def _directory_open_flags() -> int:
    return os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW


def _descriptor_stat_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _decode_result_document_bytes(content: bytes) -> TargetRootResultDocument:
    if len(content) > TARGET_ROOT_MAX_RESULT_DOCUMENT_BYTES:
        raise OwnerConflict("target_root_result_document_too_large")
    try:
        value = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_exact_json_object,
            parse_constant=_reject_json_constant,
        )
    except (
        UnicodeDecodeError,
        ValueError,
        RecursionError,
        json.JSONDecodeError,
    ) as error:
        raise OwnerConflict("target_root_result_document_invalid") from error
    result = _decode_result_document_value(value)
    if content != canonical_json(result.as_dict()).encode("utf-8"):
        raise OwnerConflict("target_root_result_document_noncanonical")
    return result


def _target_root_utf8_size(value: str, *, invalid_code: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeError as error:
        raise OwnerConflict(invalid_code) from error


def _decode_result_document_value(value: object) -> TargetRootResultDocument:
    if type(value) is not dict or set(value) != TARGET_ROOT_RESULT_DOCUMENT_FIELDS:
        raise OwnerConflict("target_root_result_document_invalid")
    schema_ref = value.get("schema_ref")
    metrics = value.get("metrics")
    disposition = value.get("result_disposition")
    if (
        type(schema_ref) is not str
        or not schema_ref
        or schema_ref != schema_ref.strip()
        or (
            _target_root_utf8_size(
                schema_ref,
                invalid_code="target_root_result_document_invalid",
            )
            > 256
        )
        or type(metrics) is not dict
        or not metrics
        or disposition not in EXPERIMENT_RESULT_DISPOSITIONS
    ):
        raise OwnerConflict("target_root_result_document_invalid")
    if len(metrics) > TARGET_ROOT_MAX_RESULT_METRICS:
        raise OwnerConflict("target_root_result_metrics_invalid")
    normalized: dict[str, int | float] = {}
    for name, metric in metrics.items():
        if (
            type(name) is not str
            or not name
            or name != name.strip()
            or (
                _target_root_utf8_size(
                    name,
                    invalid_code="target_root_result_metrics_invalid",
                )
                > 256
            )
            or isinstance(metric, bool)
            or not isinstance(metric, (int, float))
        ):
            raise OwnerConflict("target_root_result_metrics_invalid")
        if (
            type(metric) is int
            and abs(metric) > BUNDLE_CANONICAL_INTEGER_MAX_ABS
        ):
            raise OwnerConflict("target_root_result_metrics_invalid")
        try:
            finite = math.isfinite(float(metric))
        except OverflowError as error:
            raise OwnerConflict("target_root_result_metrics_invalid") from error
        if not finite:
            raise OwnerConflict("target_root_result_metrics_invalid")
        normalized[name] = metric
    document = {
        "schema_ref": schema_ref,
        "metrics": normalized,
        "result_disposition": disposition,
    }
    return TargetRootResultDocument(
        schema_ref=schema_ref,
        metrics=normalized,
        result_disposition=cast(str, disposition),
        content_hash=canonical_hash(document),
    )


def _exact_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def _reject_json_constant(_value: str) -> object:
    raise ValueError("non-finite JSON number")


def _entry_from_value(value: object) -> TargetRootCompletionManifestEntry:
    fields = {
        "ordinal",
        "role",
        "declared_relative_path",
        "artifact_kind",
        "media_type",
        "byte_count",
        "content_hash",
        "tree_hash",
        "binding",
    }
    if type(value) is not dict or set(value) != fields:
        raise ValueError("invalid manifest entry")
    binding_value = value["binding"]
    if type(binding_value) is not dict or set(binding_value) != {
        "asset_ref",
        "version_ref",
        "content_hash",
        "manifest_hash",
        "receipt",
    }:
        raise ValueError("invalid asset binding")
    receipt = _receipt_from_public(binding_value["receipt"])
    entry = TargetRootCompletionManifestEntry(
        ordinal=value["ordinal"],
        role=value["role"],
        declared_relative_path=value["declared_relative_path"],
        artifact_kind=value["artifact_kind"],
        media_type=value["media_type"],
        byte_count=value["byte_count"],
        content_hash=value["content_hash"],
        tree_hash=value["tree_hash"],
        binding=AcceptedAssetBinding(
            asset_ref=binding_value["asset_ref"],
            version_ref=binding_value["version_ref"],
            content_hash=binding_value["content_hash"],
            manifest_hash=binding_value["manifest_hash"],
            receipt=receipt,
        ),
    )
    if (
        type(entry.ordinal) is not int
        or isinstance(entry.ordinal, bool)
        or entry.ordinal < 0
        or entry.role not in TARGET_COMPLETION_ARTIFACT_ROLES
        or type(entry.declared_relative_path) is not str
        or not entry.declared_relative_path
        or entry.artifact_kind not in {"file", "directory"}
        or type(entry.media_type) is not str
        or not entry.media_type
        or type(entry.byte_count) is not int
        or isinstance(entry.byte_count, bool)
        or entry.byte_count < 0
        or len(entry.content_hash) != 64
        or len(entry.tree_hash) != 64
        or entry.binding.content_hash != entry.content_hash
    ):
        raise ValueError("invalid manifest entry")
    return entry


def _receipt_from_public(value: object) -> AcceptanceReceipt:
    if type(value) is not dict or set(value) != {
        "status",
        "issuer",
        "kind",
        "receipt_ref",
        "subject_ref",
        "payload_hash",
    } or value.get("status") != "accepted":
        raise ValueError("invalid receipt")
    fields = ("issuer", "kind", "receipt_ref", "subject_ref", "payload_hash")
    if any(type(value.get(name)) is not str or not value[name] for name in fields):
        raise ValueError("invalid receipt")
    return AcceptanceReceipt(
        issuer=value["issuer"],
        kind=value["kind"],
        receipt_ref=value["receipt_ref"],
        subject_ref=value["subject_ref"],
        payload_hash=value["payload_hash"],
    )


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


__all__ = [
    "AcceptedTargetRootCompletionManifest",
    "RM_TARGET_ROOT_COMPLETION_MANIFEST_RECEIPT_KIND",
    "SQLiteTargetRootCompletionMemoryAuthority",
    "TARGET_ROOT_RG_PENDING_CODE",
    "TargetRootCompletionManifestEntry",
    "TargetRootFinalizationResult",
    "TargetRootGraphAcceptance",
    "TargetRootOwnerRejection",
    "TargetRootResultDocument",
    "TargetRunFinalizer",
]
