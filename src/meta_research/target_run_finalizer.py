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
import shutil
import stat
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal, Protocol, cast

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from meta_research.bundle_protocol import (
    BUNDLE_CANONICAL_INTEGER_MAX_ABS,
    TargetWorkHandle,
    projection_plain_value,
)
from meta_research.target_execution_contract import (
    TargetMetricValue, valid_target_metric_value, validate_target_result_tree,
)
from meta_research.database import Database
from meta_research.research_notes import (
    FINAL_STATEMENT_PATH, research_note_metadata, research_note_metadata_from_path,
)
from meta_research.query_timing import measured_owner_operation
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
    AssetExportDescription,
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
    validate_bundle_relative_path,
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
# Small historical bundles remain readable; file-backed artifacts have no
# research-size ceiling. This threshold only selects a bounded inline encoding.
TARGET_ROOT_INLINE_ARTIFACT_BYTES = 64 * 1024
TARGET_ROOT_MAX_RESULT_DOCUMENT_BYTES = 256 * 1024
# The formal protocol admits at most 64 required plus 64 optional metric
# definitions, and requires those two key sets to be disjoint.
TARGET_ROOT_MAX_RESULT_METRICS = 2 * 64

_SYSTEM_TARGET_COMPLETION_REQUIRED_ARTIFACTS = (
    ("implementation", "implementation"),
    ("result", "outputs/result.json"),
)
_SYSTEM_TARGET_COMPLETION_OPTIONAL_ARTIFACTS = (
    ("data", "outputs/data"),
    ("checkpoint", "outputs/checkpoints"),
    ("analysis", "outputs/analysis"),
    ("log", "logs"),
)

# Retain old rejection text so historical signed rejections remain replayable.
# New path-backed artifacts do not use the historical byte/directory ceilings.
_RM_RECOVERABLE_CANDIDATE_FEEDBACK = {
    "target_root_artifact_intake_failed": (
        "Research Memory could not retain a selected artifact after storage retries. "
        "Inspect the failed intake and available storage, preserve the research "
        "outputs, and resolve the actual storage problem before completing another turn."
    ),
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
        "The declared result document is not valid unambiguous UTF-8 JSON for the Target "
        "result schema. Rewrite outputs/result.json and complete another root turn."
    ),
    "target_root_result_document_too_large": (
        "The declared result document exceeds the bounded Target result schema "
        "limit. Reduce it and complete another root turn."
    ),
    "target_root_result_metrics_invalid": (
        "The declared result document contains invalid metric names or values. "
        "Correct the metrics and complete another root turn."
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

for _role, _path in (("checkpoint", "outputs/checkpoints"),
                     ("analysis", "outputs/analysis"), ("log", "logs"),
                     ("result", "outputs/result.json")):
    _RM_RECOVERABLE_CANDIDATE_FEEDBACK["target_root_" + _role + "_directory_invalid"] = (
        f"The {_role} artifact at {_path} exceeds a directory limit or contains an "
        "unsupported entry. Limits: 16777216 bytes per file, 67108864 bytes "
        "uncompressed total, 4096 entries; only regular files and directories "
        "without links. Correct this artifact; retain valid research evidence."
    )


@dataclass(frozen=True, slots=True)
class TargetRootResultDocument:
    schema_ref: str
    metrics: dict[str, TargetMetricValue]
    result_disposition: str
    content_hash: str
    domain_fields: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_ref": self.schema_ref,
            **self.domain_fields,
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
    research_note: dict[str, object] | None = None

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
            **({"research_note": self.research_note} if self.research_note is not None else {}),
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

    def target_input_commit_sources(self, handle: TargetWorkHandle) -> dict[str, tuple[str, ...]]: ...

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

    def verify_asset_projection_binding(self, **values: object) -> None: ...

    def materialize_asset(self, memory_ref: str) -> object: ...

    def describe_asset_export(self, memory_ref: str) -> object: ...

    def export_asset(self, memory_ref: str, destination: Path) -> object: ...


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
    content: bytes | None
    content_hash: str
    tree_hash: str
    source_path: Path | None = None
    source_byte_count: int = 0

    @property
    def byte_count(self) -> int:
        return len(self.content) if self.content is not None else self.source_byte_count

    def snapshot_value(self) -> dict[str, object]:
        return {
            "ordinal": self.ordinal,
            "role": self.role,
            "declared_relative_path": self.declared_relative_path,
            "artifact_kind": self.artifact_kind,
            "media_type": self.media_type,
            "byte_count": self.byte_count,
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

    def accept_historical_research_note(self, *, manifest_ref: str,
                                       evidence: TargetRootCompletionEvidence,
                                       evidence_reader: TargetRootCompletionEvidenceReader):
        """Archive an original verified final statement without rewriting history."""
        manifest = self.query(manifest_ref)
        if manifest is None:
            raise OwnerConflict("target_root_manifest_integrity_invalid")
        completion = self._lifecycle.query_completion_by_ref(manifest.completion_ref)
        if completion is None:
            raise OwnerConflict("target_root_completion_evidence_invalid")
        _validate_evidence(evidence, handle=completion.handle)
        source_hash = evidence_reader.verify_target_root_completion_evidence(
            handle=completion.handle, evidence=evidence, handoff=evidence.handoff)
        if (evidence.final_text is None or source_hash != completion.evidence_content_hash
            or evidence.evidence_ref != completion.evidence_ref
            or evidence.operation_ref != completion.harness_operation_ref):
            raise OwnerConflict("target_root_completion_evidence_invalid")
        key = "historical-research-note:" + canonical_hash({
            "completion_ref": completion.completion_ref, "source_evidence_hash": source_hash})
        body = evidence.final_text.encode("utf-8")
        intake = self._asset_memory.submit_asset_intake(AssetIntakeRequest(
            source_kind="file", custody_mode="managed", display_name="research-note.md",
            media_type="text/markdown; charset=utf-8", content=body,
            provenance={"schema_ref": "meta-research/historical-research-note/v1",
                "target_ref": manifest.target_ref, "completion_ref": completion.completion_ref,
                "manifest_ref": manifest_ref, "source_evidence_ref": evidence.evidence_ref,
                "source_evidence_hash": source_hash, "source_bytes_sha256": evidence.final_text_sha256}),
            idempotency_key=key)
        asset = getattr(intake, "asset", None)
        if getattr(intake, "status", None) != "accepted" or asset is None:
            raise OwnerConflict("target_root_artifact_intake_unavailable")
        self._verify_binding(asset.as_binding(), expected=body)
        note = {**research_note_metadata(role="analysis", declared_relative_path=FINAL_STATEMENT_PATH,
                    artifact_kind="file", content=body, tree_hash=evidence.final_text_sha256),
                "manifest_ref": manifest_ref, "completion_ref": completion.completion_ref,
                "target_ref": manifest.target_ref, "historical_source": True,
                "source_evidence_ref": evidence.evidence_ref,
                "declared_relative_path": FINAL_STATEMENT_PATH,
                "asset_ref": asset.asset_ref, "version_ref": asset.version_ref,
                "asset_content_hash": asset.content_hash, "asset_manifest_hash": asset.manifest_hash}
        with self._database.fenced_write() as connection:
            old = connection.execute(text("SELECT note_json FROM rm_target_research_notes "
                "WHERE idempotency_key=:key"), {"key": key}).first()
            if old is not None:
                if old.note_json != canonical_json(note):
                    raise OwnerConflict("research_note_idempotency_conflict")
            else:
                connection.execute(text("INSERT INTO rm_target_research_notes "
                    "(note_ref,target_ref,completion_ref,manifest_ref,version_ref,source_evidence_ref,"
                    "source_evidence_hash,note_json,note_hash,idempotency_key,accepted_at) VALUES "
                    "(:note_ref,:target_ref,:completion_ref,:manifest_ref,:version_ref,:source_evidence_ref,"
                    ":source_evidence_hash,:note_json,:note_hash,:idempotency_key,:accepted_at)"),
                    {"note_ref": new_ref("research_note"), "target_ref": manifest.target_ref,
                     "completion_ref": completion.completion_ref, "manifest_ref": manifest_ref,
                     "version_ref": asset.version_ref, "source_evidence_ref": evidence.evidence_ref,
                     "source_evidence_hash": source_hash, "note_json": canonical_json(note),
                     "note_hash": canonical_hash(note), "idempotency_key": key, "accepted_at": time.time()})
        return note

    def query_research_notes_for_target(self, target_ref: str):
        from meta_research.research_notes import query_target_research_notes
        return query_target_research_notes(self._database, self._asset_memory, target_ref)

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

        result_artifacts = tuple(
            artifact for artifact in frozen.artifacts
            if artifact.role == "result"
            and artifact.declared_relative_path == completion.handoff.result_document_path
        )
        if (len(result_artifacts) != 1
                or result_artifacts[0].artifact_kind != "file"
                or type(result_artifacts[0].content) is not bytes):
            raise OwnerConflict("target_root_result_document_invalid")
        result_artifact = result_artifacts[0]
        result_document = _decode_result_document_bytes(result_artifact.content)
        if (result_document != frozen.result_document
                or result_document.content_hash != completion.result_document_hash
                or hashlib.sha256(result_artifact.content).hexdigest()
                != result_artifact.content_hash):
            raise OwnerConflict("target_root_result_document_invalid")

        entries: list[TargetRootCompletionManifestEntry] = []
        for artifact in frozen.artifacts:
            predecessor = self._research_note_predecessor(completion, artifact)
            intake_key = "target-root-artifact:" + canonical_hash(
                {
                    "completion_ref": completion.completion_ref,
                    **artifact.snapshot_value(),
                }
            )
            try:
                intake = self._asset_memory.submit_asset_intake(
                    AssetIntakeRequest(
                        source_kind=("local_path" if artifact.source_path is not None else "file"),
                        custody_mode="managed",
                        display_name=f"target-root-artifact-{artifact.ordinal:04d}",
                        media_type=artifact.media_type,
                        content=artifact.content,
                        source_locator=str(artifact.source_path) if artifact.source_path else None,
                        asset_ref=predecessor.get("asset_ref") if predecessor else None,
                        provenance={
                            "schema_ref": (
                                "meta-research/target-root-artifact-provenance/v1"
                            ),
                            "completion_ref": completion.completion_ref,
                            "target_ref": completion.handle.target_ref,
                            "target_run_ref": completion.handle.target_run_ref,
                            **artifact.snapshot_value(),
                            **({"predecessor_version_ref": predecessor["version_ref"]}
                               if predecessor else {}),
                        },
                    ),
                    idempotency_key=intake_key,
                )
                asset = getattr(intake, "asset", None)
                if getattr(intake, "status", None) == "failed":
                    raise OwnerConflict("target_root_artifact_intake_failed")
                if getattr(intake, "status", None) != "accepted" or asset is None:
                    raise OwnerConflict("target_root_artifact_intake_unavailable")
                binding = asset.as_binding()
                self._verify_binding_hash(binding, expected_hash=artifact.content_hash)
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
                    byte_count=artifact.byte_count,
                    content_hash=artifact.content_hash,
                    tree_hash=artifact.tree_hash,
                    binding=binding,
                    research_note=research_note_metadata(
                        role=artifact.role,
                        declared_relative_path=artifact.declared_relative_path,
                        artifact_kind=artifact.artifact_kind,
                        content=artifact.content, tree_hash=artifact.tree_hash)
                        if artifact.content is not None else research_note_metadata_from_path(
                            role=artifact.role, declared_relative_path=artifact.declared_relative_path,
                            artifact_kind=artifact.artifact_kind, source_path=artifact.source_path,
                            tree_hash=artifact.tree_hash),
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

    def _research_note_predecessor(self, completion, artifact):
        """Version the same Target's explanation without changing old manifests."""
        if artifact.role != "analysis" or artifact.declared_relative_path not in {
            "outputs/analysis", "outputs/analysis/research-note.md", FINAL_STATEMENT_PATH,
        }:
            return None
        with self._database.read() as connection:
            row = connection.execute(text(
                "SELECT entries_json FROM rm_target_root_completion_manifests "
                "WHERE target_ref=:target_ref AND completion_ref != :completion_ref "
                "ORDER BY accepted_at DESC, manifest_ref DESC LIMIT 1"),
                {"target_ref": completion.handle.target_ref,
                 "completion_ref": completion.completion_ref}).first()
        if row is None:
            return None
        for entry in json.loads(row.entries_json):
            if (entry.get("role") == artifact.role and
                entry.get("declared_relative_path") == artifact.declared_relative_path):
                return entry["binding"]
        return None

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
        """Resolve exact custody descriptions without loading large outputs."""

        current = self.query(manifest.manifest_ref)
        if current != manifest or not target_commit_ref:
            raise OwnerConflict("target_root_upstream_input_invalid")
        artifacts: list[FrozenTargetCommitInputArtifact] = []
        for entry in current.entries:
            try:
                description = self._describe_manifest_entry(entry)
            except Exception as error:
                raise OwnerConflict("target_root_upstream_input_invalid") from error
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
                    export_description=description,
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
                target_root_manifest_projection(current)
            ).encode("utf-8"),
            artifacts=tuple(artifacts),
        )

    def query(
        self, manifest_ref: str
    ) -> AcceptedTargetRootCompletionManifest | None:
        """Read the accepted ledger without reopening its artifact contents.

        The completion freeze and managed intake validate bytes before this
        receipt is issued. Delivery checks bytes while exporting; observation
        of accepted history must not repeat that work.
        """
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
            self._describe_manifest_entry(entry)
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
        if len(result_entries) != 1 or result_entries[0].artifact_kind != "file":
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

    def _describe_manifest_entry(
        self, entry: TargetRootCompletionManifestEntry,
    ) -> AssetExportDescription:
        """Match exact accepted metadata, including historical ZIP custody."""
        self._verify_binding_hash(entry.binding, expected_hash=entry.content_hash)
        description = self._asset_memory.describe_asset_export(entry.binding.version_ref)
        archived_directory = (
            entry.artifact_kind == "directory" and entry.media_type == "application/zip"
        )
        if (description.memory_ref != entry.binding.version_ref
                or description.kind != ("file" if archived_directory else entry.artifact_kind)
                or description.content_hash != entry.content_hash
                or description.manifest_hash != entry.binding.manifest_hash
                or description.byte_count != entry.byte_count
                or (not archived_directory and entry.tree_hash != entry.content_hash)):
            raise OwnerConflict("target_root_manifest_integrity_invalid")
        if entry.research_note is not None and not archived_directory:
            note = entry.research_note
            source_path = note.get("entry_path")
            source = (
                next(iter(description.entries), None)
                if description.kind == "file" and source_path is None
                else next((item for item in description.entries if item.path == source_path), None)
            )
            if (source is None or entry.role != "analysis"
                    or note.get("source_bytes_sha256") != source.sha256
                    or note.get("source_utf8_bytes") != source.size):
                raise OwnerConflict("target_root_manifest_integrity_invalid")
        return description

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

    def _verify_binding_hash(self, binding: AcceptedAssetBinding, *, expected_hash: str) -> None:
        if type(binding) is not AcceptedAssetBinding or binding.content_hash != expected_hash:
            raise OwnerConflict("target_root_artifact_integrity_invalid")
        self._asset_memory.verify_asset_projection_binding(
            asset_ref=binding.asset_ref, version_ref=binding.version_ref,
            content_hash=binding.content_hash, manifest_hash=binding.manifest_hash,
            receipt=binding.receipt,
        )


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

    @measured_owner_operation("target_root_finalization")
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

        # The Harness verifier proves the provider/process tree is drained.
        # Before accepting a new snapshot (including an AR-only retry), check
        # exact input custody and bytes. An exact completion with an accepted
        # RM manifest already passed this boundary; its immutable snapshot is
        # authoritative for replay, independent of later workspace changes.
        if manifest is None and (
            handle.accepted_input_target_commit_refs
            or handle.accepted_input_asset_proofs
        ):
            accepted_inputs = self._resolve_target_commit_inputs(handle)
            self._workspace_resolver.verify_target_workspace_inputs(
                handle=handle,
                accepted_target_commit_inputs=accepted_inputs,
            )

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
            with _completion_staging_directory(pinned_workspace) as staging_directory:
                try:
                    frozen = self._freeze(
                        handle=handle,
                        handoff=handoff,
                        resolved_workspace=pinned_workspace,
                        system_owned=evidence.handoff is None,
                        final_text=evidence.final_text,
                        staging_directory=Path(staging_directory),
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
                frozen = _persist_frozen_sources(frozen, completion, pinned_workspace.path.parent)
                memory_result = None
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
                finally:
                    if memory_result is not None:
                        _remove_frozen_sources(frozen)
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
        source_reader = getattr(self._workspace_resolver, "target_input_commit_sources", None)
        required = (tuple(source_reader(handle)) if callable(source_reader)
                    else handle.accepted_input_target_commit_refs)
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
        final_text: str | None = None,
        staging_directory: Path | None = None,
    ) -> _FrozenWorkspace:
        workspace_ref = resolved_workspace.workspace_ref
        root_descriptor = resolved_workspace.descriptor
        if system_owned:
            declared_implementation = tuple(
                artifact
                for artifact in handoff.artifacts
                if artifact.role == "implementation"
            )
            if not declared_implementation or any(
                artifact.relative_path != "implementation"
                and not artifact.relative_path.startswith("implementation/")
                for artifact in declared_implementation
            ):
                raise OwnerConflict("target_implementation_workspace_invalid")
        frozen_artifacts: list[_FrozenArtifact] = []
        for ordinal, artifact in enumerate(handoff.artifacts):
            frozen_artifact = _freeze_artifact(
                root_descriptor,
                ordinal,
                artifact.role,
                artifact.relative_path,
                staging_directory=staging_directory,
            )
            frozen_artifacts.append(frozen_artifact)
        if system_owned and final_text is not None:
            # These are verified Harness bytes, not a filesystem claim. Keep
            # the original final statement as an RM asset even when the root
            # left no research-note.md or the native session later compacts.
            content = final_text.encode("utf-8")
            digest = hashlib.sha256(content).hexdigest()
            frozen_artifacts.append(_FrozenArtifact(
                ordinal=len(frozen_artifacts), role="analysis",
                declared_relative_path=FINAL_STATEMENT_PATH, artifact_kind="file",
                media_type="text/markdown; charset=utf-8", content=content,
                content_hash=digest, tree_hash=digest))
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
        if system_owned and any(item.artifact_kind != "directory" for item in implementation):
            raise OwnerConflict("target_implementation_workspace_invalid")
        if not implementation:
            raise OwnerConflict("target_root_implementation_missing")
        from meta_research.formal_run_bindings import implementation_set_hash
        implementation_tree_hash = implementation_set_hash([item.snapshot_value() for item in implementation])
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


def target_root_manifest_projection(manifest):
    """Preserve the exact historical manifest shape when no note was recorded."""
    value = projection_plain_value(manifest)
    for entry in value["entries"]:
        if entry.get("research_note") is None:
            entry.pop("research_note", None)
    return value


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
    descriptor, info = _open_workspace_artifact(root_descriptor, "outputs/result.json")
    try:
        try:
            document = json.loads(_read_stable_regular_file(descriptor, info))
        except (ValueError, UnicodeDecodeError) as error:
            raise OwnerConflict('target_root_result_document_invalid') from error
        if not isinstance(document, dict) or not isinstance(document.get('formal_runs', []), list):
            raise OwnerConflict('target_root_result_document_invalid')
    finally:
        os.close(descriptor)
    selected_paths = []
    for run in document.get('formal_runs', []):
        if not isinstance(run, dict) or run.get('variant_run_ref') or run.get('status', 'executed') not in {'executed', 'failed'}:
            continue
        paths = run.get('implementation_paths')
        if paths is None:
            continue
        if (not isinstance(paths, list) or not paths or any(not isinstance(path, str)
                or (path != 'implementation' and not path.startswith('implementation/'))
                or any(part in {'', '.', '..'} for part in path.split('/')) for path in paths)):
            raise OwnerConflict('target_formal_implementation_paths_invalid')
        selected_paths.extend(paths)
    if selected_paths:
        selected_paths = sorted(set(selected_paths))
        if any(path.startswith(other + '/') for path in selected_paths for other in selected_paths if path != other):
            raise OwnerConflict('target_formal_implementation_paths_overlap')
        artifacts = [artifact for artifact in artifacts if artifact.role != 'implementation']
        artifacts[0:0] = [TargetCompletionArtifact(role='implementation', relative_path=path) for path in selected_paths]
    for role, relative_path in _SYSTEM_TARGET_COMPLETION_OPTIONAL_ARTIFACTS:
        if _workspace_artifact_exists(root_descriptor, relative_path):
            boundaries = _declared_artifact_boundaries(document, relative_path, role)
            paths = (
                _declared_boundary_artifact_paths(root_descriptor, relative_path, boundaries)
                if boundaries else _subject_artifact_paths(root_descriptor, relative_path, role)
            )
            artifacts.extend(
                TargetCompletionArtifact(
                    role=role,
                    relative_path=path,
                )
                for path in paths
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


def _declared_artifact_boundaries(document, relative_path, role):
    """Honor actual producers' exact selections; RG owns semantic rejection."""
    if role not in {'data', 'analysis', 'log'}:
        return ()
    selected = []
    for run in document.get('formal_runs', []):
        if not isinstance(run, dict) or run.get('status', 'executed') not in {'executed', 'failed'}:
            continue
        producers = [] if run.get('variant_run_ref') else [run]
        evaluations = run.get('evaluations', [])
        if isinstance(evaluations, list):
            producers.extend(attempt for attempt in evaluations if isinstance(attempt, dict)
                and attempt.get('status', 'executed') in {'executed', 'failed'}
                and not attempt.get('evaluation_attempt_ref'))
        for producer in producers:
            declared = producer.get('artifact_paths')
            if isinstance(declared, list) and len(declared) <= 100:
                selected.extend(declared)
    for field in ('dataset_candidates', 'environment_candidates'):
        candidates = document.get(field, [])
        if role in {'data', 'analysis'} and isinstance(candidates, list) and len(candidates) <= 100:
            selected.extend(candidate.get('artifact_path') for candidate in candidates
                            if isinstance(candidate, dict))
    paths = set()
    for path in selected:
        try:
            path = validate_bundle_relative_path(path)
        except TargetImplementationBundleError:
            continue
        if path == relative_path or path.startswith(relative_path + '/'):
            paths.add(path)
    return tuple(sorted(paths))


def _declared_boundary_artifact_paths(root_descriptor, relative_path, boundaries):
    selected = set(_dataset_boundary_artifact_paths(root_descriptor, relative_path, boundaries))
    # Different producers may explicitly select a collection and an exact child.
    # Keep both exact assets; equal paths still share one manifest entry.
    for path in boundaries:
        if path in selected:
            continue
        try:
            exists = _workspace_artifact_exists(root_descriptor, path)
        except OwnerConflict as error:
            if error.code != 'target_root_artifact_type_unsupported':
                raise
            # An existing file cannot contain the declared child. Keep the
            # discovered file for freezing; RG can return selection feedback.
            continue
        if exists:
            selected.add(path)
    return tuple(sorted(selected))


def _dataset_boundary_artifact_paths(root_descriptor, relative_path, boundaries):
    """Split only selected ancestors, retaining every other branch exactly once."""
    if relative_path in boundaries:
        return (relative_path,)
    if not any(path.startswith(relative_path + '/') for path in boundaries):
        return (relative_path,)
    try:
        descriptor, info = _open_workspace_artifact(root_descriptor, relative_path)
    except OwnerConflict:
        # The ordinary freezer will reject unsafe links or missing artifacts
        # through its existing recoverable candidate boundary.
        return (relative_path,)
    try:
        if not stat.S_ISDIR(info.st_mode):
            return (relative_path,)
        names = sorted(os.listdir(descriptor))
    finally:
        os.close(descriptor)
    if not names:
        return (relative_path,)
    return tuple(
        selected
        for name in names
        for selected in _dataset_boundary_artifact_paths(
            root_descriptor, relative_path + '/' + name, boundaries,
        )
    )


def _subject_artifact_paths(root_descriptor: int, relative_path: str, role: str) -> tuple[str, ...]:
    """Keep selected states, execution logs and reports individually addressable.

    A bounded top-level split preserves all bytes, including child directories.
    Larger collections stay one exact directory asset rather than exploding
    the completion graph. Descriptor-based freezing retains path protections.
    """
    if role not in {'checkpoint', 'log', 'analysis', 'data'}:
        return (relative_path,)
    try:
        descriptor, info = _open_workspace_artifact(root_descriptor, relative_path)
    except OwnerConflict:
        return (relative_path,)
    try:
        if not stat.S_ISDIR(info.st_mode):
            return (relative_path,)
        names = sorted(os.listdir(descriptor))
        if role == 'checkpoint' and not names:
            return ()
        if not names or len(names) > 30:
            return (relative_path,)
        return tuple(relative_path + '/' + name for name in names)
    finally:
        os.close(descriptor)


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
    root_descriptor: int, ordinal: int, role: str, relative_path: str,
    *, staging_directory: Path | None = None,
) -> _FrozenArtifact:
    descriptor, info = _open_workspace_artifact(
        root_descriptor, relative_path
    )
    try:
        if stat.S_ISREG(info.st_mode):
            if role == "result" and info.st_size > TARGET_ROOT_MAX_RESULT_DOCUMENT_BYTES:
                raise OwnerConflict("target_root_result_document_too_large")
            if info.st_size > TARGET_ROOT_INLINE_ARTIFACT_BYTES and role != "result":
                return _freeze_streamed_artifact(descriptor, info, ordinal, role,
                                                relative_path, staging_directory)
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
                code = error.code
                if code in {"target_implementation_workspace_entry_unsupported",
                            "target_implementation_bundle_too_large"}:
                    return _freeze_streamed_artifact(descriptor, info, ordinal, role,
                                                    relative_path, staging_directory)
                raise OwnerConflict(code) from error
            if first.bundle_bytes != second.bundle_bytes:
                raise OwnerConflict("target_root_workspace_changed")
            if len(first.bundle_bytes) > MAX_ASSET_BYTES:
                return _freeze_streamed_artifact(descriptor, info, ordinal, role,
                                                relative_path, staging_directory)
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


def _completion_staging_directory(workspace: _PinnedWorkspaceRoot):
    try:
        return TemporaryDirectory(prefix=".target-completion-", dir=workspace.path.parent)
    except OSError as error:
        os.close(workspace.descriptor)
        raise OwnerConflict("target_root_artifact_storage_unavailable") from error


def _persist_frozen_sources(frozen: _FrozenWorkspace, completion: AcceptedTargetRootCompletion,
                            workspace_parent: Path) -> _FrozenWorkspace:
    try:
        return _publish_frozen_sources(frozen, completion, workspace_parent)
    except OSError as error:
        raise OwnerConflict("target_root_artifact_storage_unavailable") from error


def _publish_frozen_sources(frozen: _FrozenWorkspace, completion: AcceptedTargetRootCompletion,
                            workspace_parent: Path) -> _FrozenWorkspace:
    """Publish stable intake locators that survive partial acceptance and retry.

    A queued RM job may still need its source after this call returns. Keep
    those service-owned files until RM has accepted the complete manifest or
    issued a terminal candidate rejection. Content and completion identity bind
    the path, so recreating a snapshot never changes the intake request hash.
    """
    if not any(artifact.source_path for artifact in frozen.artifacts):
        return frozen
    parent = workspace_parent / ".target-completion-intakes"
    root = parent / canonical_hash({"completion_ref": completion.completion_ref,
                                   "artifact_snapshot_hash": frozen.artifact_snapshot_hash})
    for directory in (parent, root):
        directory.mkdir(mode=0o700, exist_ok=True)
        if not stat.S_ISDIR(directory.lstat().st_mode):
            raise OwnerConflict("target_root_artifact_storage_unavailable")
    artifacts = []
    for artifact in frozen.artifacts:
        if artifact.source_path is None:
            artifacts.append(artifact)
            continue
        destination = root / f"artifact-{artifact.ordinal:04d}"
        try:
            existing = destination.lstat()
        except FileNotFoundError:
            try:
                if artifact.artifact_kind == "file":
                    os.link(artifact.source_path, destination, follow_symlinks=False)
                    artifact.source_path.unlink()
                else:
                    artifact.source_path.rename(destination)
            except FileExistsError:
                pass
            existing = destination.lstat()
        valid_type = stat.S_ISDIR(existing.st_mode) if artifact.artifact_kind == "directory" else stat.S_ISREG(existing.st_mode)
        if not valid_type:
            raise OwnerConflict("target_root_artifact_storage_unavailable")
        artifacts.append(replace(artifact, source_path=destination))
    return replace(frozen, artifacts=tuple(artifacts))


def _remove_frozen_sources(frozen: _FrozenWorkspace) -> None:
    roots = {artifact.source_path.parent for artifact in frozen.artifacts if artifact.source_path}
    for root in roots:
        if root.parent.name != ".target-completion-intakes" or root.is_symlink():
            raise OwnerConflict("target_root_artifact_storage_unavailable")
        try:
            shutil.rmtree(root)
        except FileNotFoundError:
            pass
        try:
            root.parent.rmdir()
        except OSError:
            # Another completion may still own its pending intake snapshots.
            pass


def _freeze_streamed_artifact(descriptor: int, info: os.stat_result, ordinal: int,
                              role: str, relative_path: str,
                              staging_directory: Path | None) -> _FrozenArtifact:
    """Copy from pinned descriptors to a private snapshot using bounded reads.

    RM receives this service-owned snapshot, never a reopened mutable Target
    path. Its accepted hash must match this independently frozen content. The
    caller keeps staging outside the Target workspace, on its storage volume,
    and removes it after acceptance or failure.
    """
    if staging_directory is None:
        raise OwnerConflict("target_root_artifact_storage_unavailable")
    destination = staging_directory / f"artifact-{ordinal:04d}"
    states: dict[str, tuple[object, ...]] = {}
    directories: list[str] = []
    entries: list[dict[str, object]] = []

    def copy_file(fd: int, before: os.stat_result, output: Path) -> tuple[str, int]:
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise OwnerConflict("target_root_artifact_type_unsupported")
        digest = hashlib.sha256()
        size = 0
        os.lseek(fd, 0, os.SEEK_SET)
        with output.open("xb") as stream:
            while size < before.st_size:
                chunk = os.read(fd, min(1024 * 1024, before.st_size - size))
                if not chunk:
                    break
                stream.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        if size != before.st_size or _descriptor_stat_identity(before) != _descriptor_stat_identity(os.fstat(fd)):
            raise OwnerConflict("target_root_workspace_changed")
        return digest.hexdigest(), size

    def walk(fd: int, before: os.stat_result, prefix: str, *, verify: bool) -> None:
        key = prefix or "."
        identity = _descriptor_stat_identity(before)
        if verify:
            if states.get(key) != identity:
                raise OwnerConflict("target_root_workspace_changed")
        else:
            states[key] = identity
        for name in sorted(os.listdir(fd)):
            path = prefix + "/" + name if prefix else name
            try:
                validate_bundle_relative_path(path)
            except TargetImplementationBundleError as error:
                raise OwnerConflict("target_implementation_workspace_entry_unsupported") from error
            child, child_info = _open_artifact_component(fd, name)
            try:
                if stat.S_ISDIR(child_info.st_mode):
                    if not verify:
                        (destination / path).mkdir()
                        directories.append(path)
                    walk(child, child_info, path, verify=verify)
                elif stat.S_ISREG(child_info.st_mode):
                    if verify:
                        if states.get(path) != _descriptor_stat_identity(child_info):
                            raise OwnerConflict("target_root_workspace_changed")
                    else:
                        digest, size = copy_file(child, child_info, destination / path)
                        states[path] = _descriptor_stat_identity(child_info)
                        entries.append({"path": path, "sha256": digest, "size": size})
                else:
                    raise OwnerConflict("target_root_artifact_type_unsupported")
            finally:
                os.close(child)
        if identity != _descriptor_stat_identity(os.fstat(fd)):
            raise OwnerConflict("target_root_workspace_changed")

    try:
        if stat.S_ISREG(info.st_mode):
            digest, size = copy_file(descriptor, info, destination)
            kind, media_type = "file", "application/octet-stream"
        elif stat.S_ISDIR(info.st_mode):
            destination.mkdir()
            walk(descriptor, info, "", verify=False)
            walk(descriptor, os.fstat(descriptor), "", verify=True)
            if role == "implementation" and not entries:
                raise OwnerConflict("target_implementation_workspace_entry_unsupported")
            entries.sort(key=lambda entry: entry["path"])
            digest = canonical_hash({"kind": "directory", "directories": sorted(directories),
                                     "entries": entries})
            size = sum(entry["size"] for entry in entries)
            kind, media_type = "directory", "application/x-directory"
        else:
            raise OwnerConflict("target_root_artifact_type_unsupported")
    except OSError as error:
        raise OwnerConflict("target_root_artifact_storage_unavailable") from error
    return _FrozenArtifact(ordinal=ordinal, role=role, declared_relative_path=relative_path,
        artifact_kind=kind, media_type=media_type, content=None, content_hash=digest,
        tree_hash=digest, source_path=destination, source_byte_count=size)


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
    # The source artifact and its byte hash stay immutable. Only the decoded
    # result document has a derived canonical hash; formatting is not research.
    return _decode_result_document_value(value)


def _target_root_utf8_size(value: str, *, invalid_code: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeError as error:
        raise OwnerConflict(invalid_code) from error


def _decode_result_document_value(value: object) -> TargetRootResultDocument:
    if type(value) is not dict or not TARGET_ROOT_RESULT_DOCUMENT_FIELDS <= set(value):
        raise OwnerConflict("target_root_result_document_invalid")
    schema_ref = value.get("schema_ref")
    metrics = value.get("metrics")
    disposition = value.get("result_disposition")
    from meta_research.formal_entities import _declared_work
    explicit_work = False
    if 'formal_runs' in value:
        try:
            _declared_work(value)
            explicit_work = True
        except OwnerConflict:
            pass
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
        or (not metrics and not explicit_work)
        or disposition not in EXPERIMENT_RESULT_DISPOSITIONS
    ):
        raise OwnerConflict("target_root_result_document_invalid")
    if len(metrics) > TARGET_ROOT_MAX_RESULT_METRICS:
        raise OwnerConflict("target_root_result_metrics_invalid")
    normalized: dict[str, TargetMetricValue] = {}
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
            or not valid_target_metric_value(metric)
        ):
            raise OwnerConflict("target_root_result_metrics_invalid")
        normalized[name] = cast(TargetMetricValue, metric)
    document = dict(value)
    # Bound all additional domain content too, including strings and numbers.
    try:
        validate_target_result_tree(document)
        encoded = canonical_json(document).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise OwnerConflict("target_root_result_document_invalid") from error
    if len(encoded) > TARGET_ROOT_MAX_RESULT_DOCUMENT_BYTES:
        raise OwnerConflict("target_root_result_document_too_large")
    return TargetRootResultDocument(
        schema_ref=schema_ref,
        metrics=normalized,
        result_disposition=cast(str, disposition),
        content_hash=canonical_hash(document),
        domain_fields={key: item for key, item in document.items()
                       if key not in TARGET_ROOT_RESULT_DOCUMENT_FIELDS},
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
    if type(value) is not dict or set(value) not in (fields, fields | {"research_note"}):
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
        research_note=value.get("research_note"),
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
