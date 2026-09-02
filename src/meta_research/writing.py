from __future__ import annotations

import difflib
import hashlib
import inspect
import time
from typing import cast

from meta_research.owners.advancement_engine import AdvancementEngineInterface
from meta_research.owners.agent_runtime import AgentRuntimeInterface, WritingRun
from meta_research.owners.common import AcceptanceReceipt, OwnerConflict, canonical_hash
from meta_research.owners.human_collaboration import HumanCollaborationInterface
from meta_research.owners.research_graph import ResearchGraphInterface
from meta_research.owners.research_memory import (
    AssetIntakeRequest,
    AssetIntakeResult,
    ResearchMemoryInterface,
)
from meta_research.owners.secret_detection import contains_secret
from meta_research.runtime_protection import RuntimeProtectionUnavailable
from meta_research.writing_contract import (
    WRITING_DOCUMENT_TYPES,
    WRITING_RESEARCH_SNAPSHOT_SCHEMA,
    WritingIntentBinding,
    default_writing_execution_budget,
    normalize_writing_intent,
    writing_document_profile,
    writing_intent_schema,
)
from meta_research.writing_snapshot import WritingResearchSnapshotReader
from meta_research.writing_renderer import (
    WritingRenderedArtifact,
    WritingRendererRegistry,
    default_writing_renderer_registry,
)
from meta_research.writing_delivery import (
    WRITING_DELIVERY_SCHEMA,
    derive_writing_delivery_operation_ref,
    normalize_writing_delivery_payload,
    normalize_writing_delivery_target,
    writing_delivery_effects,
)
from meta_research.writing_skill import (
    WritingSkillDraft,
    WritingSkillProvider,
    WritingSkillRequest,
    WritingSourceMaterial,
    WritingSkillUnavailable,
    WRITING_MAX_SOURCE_BYTES,
    validate_writing_skill_draft,
    validate_writing_skill_result,
)


_WRITING_PROVIDER_UNIT_KINDS = ("writing_primary", "writing_review")
_PROVIDER_RECONCILIATION_PENDING = "codex_operation_reconciliation_pending"
_WRITING_PROVIDER_CONTRACT_CORRECTIONS = frozenset(
    {
        "writing_adapter_kind_invalid",
        "writing_child_review_result_invalid",
        "writing_child_review_result_missing",
        "writing_child_review_trace_invalid",
        "writing_citation_source_unaccepted",
        "writing_citations_invalid",
        "writing_markdown_invalid",
        "writing_native_session_invalid",
        "writing_paper_structure_invalid",
        "writing_presentation_structure_invalid",
        "writing_review_invalid",
        "writing_review_mode_invalid",
        "writing_review_not_independent",
        "writing_review_revision_not_material",
        "writing_review_task_invalid",
        "writing_reviewed_checkpoint_mismatch",
        "writing_reviewer_invalid",
    }
)
# Persisted operation timestamps enforce this across daemon restarts.  One
# second prevents a tight reconciliation loop without hiding an ambiguous
# user-visible delivery behind a long interactive delay.
_WRITING_DELIVERY_RETRY_BACKOFF_SECONDS = 1.0


class WritingReportService:
    """Owner-neutral Writing orchestration composed only through public Interfaces."""

    def __init__(
        self,
        research_graph: ResearchGraphInterface,
        advancement_engine: AdvancementEngineInterface,
        research_memory: ResearchMemoryInterface,
        agent_runtime: AgentRuntimeInterface,
        human_collaboration: HumanCollaborationInterface,
        provider: WritingSkillProvider,
        renderer_registry: WritingRendererRegistry | None = None,
    ) -> None:
        self._research_graph = research_graph
        self._advancement_engine = advancement_engine
        self._research_memory = research_memory
        self._agent_runtime = agent_runtime
        self._human_collaboration = human_collaboration
        self._provider = provider
        self._renderer_registry = (
            renderer_registry or default_writing_renderer_registry()
        )
        self._snapshot_reader = WritingResearchSnapshotReader(
            research_graph,
            advancement_engine,
            research_memory,
            agent_runtime,
        )

    def create_report_intent(
        self,
        *,
        quest_ref: str,
        title: str,
        audience: str,
        purpose: str,
        instructions: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        return self.create_intent(
            document_type="report",
            quest_ref=quest_ref,
            title=title,
            audience=audience,
            purpose=purpose,
            instructions=instructions,
            idempotency_key=idempotency_key,
        )

    def create_intent(
        self,
        *,
        document_type: str,
        quest_ref: str,
        title: str,
        audience: str,
        purpose: str,
        instructions: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        writing_document_profile(document_type)
        intent = normalize_writing_intent(
            document_type,
            {
                "schema_ref": writing_intent_schema(document_type),
                "title": title,
                "audience": audience,
                "purpose": purpose,
                "instructions": instructions,
            }
        )
        replay = self._human_collaboration.query_command_by_idempotency_key(
            idempotency_key, command_kind="command_draft"
        )
        if replay is not None:
            replay_payload = self._writing_payload(replay)
            if (
                replay_payload.get("quest_ref") != quest_ref
                or replay_payload.get("document_type") != document_type
                or replay_payload.get("intent") != intent
            ):
                raise OwnerConflict("idempotency_conflict")
            return self._public_intent(replay)
        snapshot = self._capture_snapshot(quest_ref)
        command = self._human_collaboration.create_command_draft(
            f"quest:{quest_ref}",
            {
                "command_kind": "writing_report_start",
                "payload": {
                    "document_type": document_type,
                    "quest_ref": quest_ref,
                    "intent": intent,
                    "intent_hash": canonical_hash(intent),
                    "snapshot": snapshot,
                    "snapshot_ref": snapshot["snapshot_ref"],
                    "snapshot_hash": snapshot["snapshot_hash"],
                    "execution_budget": default_writing_execution_budget(),
                },
            },
            idempotency_key,
        )
        return self._public_intent(command)

    def preview_report_intent(
        self, intent_id: str, *, idempotency_key: str
    ) -> dict[str, object]:
        return self.preview_intent(intent_id, idempotency_key=idempotency_key)

    def preview_intent(
        self, intent_id: str, *, idempotency_key: str
    ) -> dict[str, object]:
        command = self._human_collaboration.query_command(intent_id)
        self._writing_payload(command)
        previewed = self._human_collaboration.preview_command(
            intent_id,
            cast(int, command["draft_revision"]),
            cast(str, command["draft_hash"]),
            idempotency_key,
        )
        return self._public_intent(previewed)

    def confirm_report_intent(
        self,
        intent_id: str,
        *,
        draft_revision: int,
        draft_hash: str,
        preview_ref: str,
        preview_hash: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        return self.confirm_intent(
            intent_id,
            draft_revision=draft_revision,
            draft_hash=draft_hash,
            preview_ref=preview_ref,
            preview_hash=preview_hash,
            idempotency_key=idempotency_key,
        )

    def confirm_intent(
        self,
        intent_id: str,
        *,
        draft_revision: int,
        draft_hash: str,
        preview_ref: str,
        preview_hash: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        command = self._human_collaboration.query_command(intent_id)
        payload = self._writing_payload(command)
        existing_run = self._agent_runtime.query_writing_report_by_intent(intent_id)
        if command.get("confirmation_receipt") is None:
            confirmed = self._human_collaboration.confirm_command(
                intent_id,
                draft_revision,
                draft_hash,
                preview_ref,
                preview_hash,
                idempotency_key,
            )
        else:
            # A confirmation is a durable fact. Reconciliation after a lost ACK
            # must reuse the same frozen Snapshot and never recapture or replace
            # it with later Quest research.
            confirmed = command
        if existing_run is not None:
            return self._public_intent(confirmed)
        payload = self._writing_payload(confirmed)
        frozen_snapshot = cast(dict[str, object], payload["snapshot"])
        confirmation = _receipt(confirmed.get("confirmation_receipt"))
        preview = confirmed.get("impact_preview")
        if not isinstance(preview, dict):
            raise OwnerConflict("writing_preview_missing")
        binding = WritingIntentBinding(
            intent_id=intent_id,
            quest_ref=cast(str, payload["quest_ref"]),
            document_type=cast(str, payload["document_type"]),
            intent=cast(dict[str, object], payload["intent"]),
            intent_hash=cast(str, payload["intent_hash"]),
            snapshot=frozen_snapshot,
            snapshot_ref=cast(str, payload["snapshot_ref"]),
            snapshot_hash=cast(str, payload["snapshot_hash"]),
            execution_budget=cast(dict[str, object], payload["execution_budget"]),
            draft_revision=cast(int, confirmed["draft_revision"]),
            draft_hash=cast(str, confirmed["draft_hash"]),
            preview_ref=cast(str, preview["preview_ref"]),
            preview_hash=cast(str, preview["preview_hash"]),
            confirmation=confirmation,
        )
        runtime_binding_method = self._provider.runtime_binding
        runtime_binding = (
            runtime_binding_method(document_type=cast(str, payload["document_type"]))
            if "document_type" in inspect.signature(runtime_binding_method).parameters
            else runtime_binding_method()
        )
        runtime_binding.validate()
        self._agent_runtime.admit_writing_report(
            binding,
            runtime_binding=runtime_binding,
            idempotency_key=_operation_key(
                "writing-admission", intent_id, confirmation.receipt_ref
            ),
        )
        return self._public_intent(confirmed)

    def query_report_intent(self, intent_id: str) -> dict[str, object]:
        return self.query_intent(intent_id)

    def query_intent(self, intent_id: str) -> dict[str, object]:
        return self._public_intent(self._human_collaboration.query_command(intent_id))

    def query_writing_report(self, run_ref: str) -> dict[str, object]:
        run = self._agent_runtime.query_writing_report(run_ref)
        if run is None:
            raise OwnerConflict("writing_run_not_found")
        command = self._human_collaboration.query_command(run.intent_id)
        return self._public_intent(command)

    def query_overview(self) -> dict[str, object]:
        commands = self._human_collaboration.query_commands(
            command_kind="writing_report_start"
        )
        return {
            "status": "ready",
            "document_types": list(WRITING_DOCUMENT_TYPES),
            "delivery_capabilities": {
                "providers": list(
                    self._agent_runtime.writing_delivery.provider_capabilities()
                ),
                "renderers": [
                    {
                        "document_type": document_type,
                        "default_format": self._renderer_registry.default_format(
                            document_type
                        ),
                        "formats": list(
                            self._renderer_registry.formats(document_type)
                        ),
                    }
                    for document_type in WRITING_DOCUMENT_TYPES
                ],
            },
            # HC is the Owner of pre-admission intents. Unioning its command
            # ledger with AR-backed runs keeps draft/previewed/confirmed work
            # recoverable after reload or a crash between Owners.
            "runs": [self._public_intent(command) for command in commands],
        }

    def create_delivery_intent(
        self,
        run_ref: str,
        *,
        action: str,
        provider_ref: str,
        target: dict[str, object],
        output_format: str | None,
        idempotency_key: str,
    ) -> dict[str, object]:
        """Freeze one exact, independently confirmable external side effect."""

        normalized_target = normalize_writing_delivery_target(
            provider_ref, action, target
        )
        replay = self._human_collaboration.query_command_by_idempotency_key(
            idempotency_key, command_kind="command_draft"
        )
        if replay is not None:
            replay_payload = self._delivery_payload(replay)
            if (
                replay_payload["run_ref"] != run_ref
                or replay_payload["action"] != action
                or replay_payload["provider_ref"] != provider_ref
                or replay_payload["target"] != normalized_target
                or (
                    output_format is not None
                    and replay_payload["renderer_format"] != output_format
                )
            ):
                raise OwnerConflict("idempotency_conflict")
            return self._public_delivery_intent(replay)

        # Reject unavailable providers/actions and a non-current external
        # target before creating the separately receipted renderer asset.
        target_binding = self._agent_runtime.writing_delivery.verify_target_current(
            provider_ref,
            action,
            normalized_target,
        )
        run = self._agent_runtime.query_writing_report(run_ref)
        if run is None:
            raise OwnerConflict("writing_run_not_found")
        selected_format = output_format or self._renderer_registry.default_format(
            run.document_type
        )
        decision, renderer = self._current_delivery_artifact(
            run, output_format=selected_format, ensure=True
        )
        renderer_asset = cast(dict[str, object], renderer["asset"])
        renderer_artifact = cast(
            WritingRenderedArtifact, renderer["artifact"]
        )
        payload_without_ref: dict[str, object] = {
            "schema_ref": WRITING_DELIVERY_SCHEMA,
            "request_nonce": (
                "writing_delivery_request:"
                + canonical_hash({"idempotency_key": idempotency_key})[:48]
            ),
            "action": action,
            "provider_ref": provider_ref,
            "target": normalized_target,
            "target_binding": target_binding,
            "effects": writing_delivery_effects(
                provider_ref, action, normalized_target
            ),
            "run_ref": run.run_ref,
            "document_type": run.document_type,
            "asset_ref": decision.asset.asset_ref,
            "version_ref": decision.asset.version_ref,
            "content_hash": decision.asset.content_hash,
            "manifest_hash": decision.asset.manifest_hash,
            "version_receipt": decision.asset.receipt.as_public_dict(),
            "citation_decision_ref": decision.decision_ref,
            "citation_receipt": decision.receipt.as_public_dict(),
            "renderer_asset_ref": renderer_asset["asset_ref"],
            "renderer_version_ref": renderer_asset["version_ref"],
            "renderer_content_hash": renderer_asset["content_hash"],
            "renderer_manifest_hash": renderer_asset["manifest_hash"],
            "renderer_artifact_sha256": renderer_artifact.content_hash,
            "renderer_format": renderer_artifact.output_format,
            "renderer_media_type": renderer_artifact.media_type,
            "renderer_receipt": renderer_asset["receipt"],
        }
        payload = normalize_writing_delivery_payload(
            {
                **payload_without_ref,
                "operation_ref": derive_writing_delivery_operation_ref(
                    payload_without_ref
                ),
            }
        )
        # The provider owns target semantics. This is checked again by HC at
        # Preview/Confirmation, by AR at admission, and immediately before the
        # provider call.
        self.verify_writing_delivery_binding(payload)
        command = self._human_collaboration.create_command_draft(
            f"writing_run:{run_ref}",
            {
                "command_kind": "writing_external_delivery",
                "payload": payload,
            },
            idempotency_key,
        )
        return self._public_delivery_intent(command)

    def preview_delivery_intent(
        self, intent_id: str, *, idempotency_key: str
    ) -> dict[str, object]:
        command = self._human_collaboration.query_command(intent_id)
        self._delivery_payload(command)
        previewed = self._human_collaboration.preview_command(
            intent_id,
            cast(int, command["draft_revision"]),
            cast(str, command["draft_hash"]),
            idempotency_key,
        )
        return self._public_delivery_intent(previewed)

    def confirm_delivery_intent(
        self,
        intent_id: str,
        *,
        draft_revision: int,
        draft_hash: str,
        preview_ref: str,
        preview_hash: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        command = self._human_collaboration.query_command(intent_id)
        payload = self._delivery_payload(command)
        existing = self._agent_runtime.writing_delivery.query_operation(
            cast(str, payload["operation_ref"])
        )
        if existing is not None:
            return self._public_delivery_intent(command)
        if command.get("confirmation_receipt") is None:
            try:
                confirmed = self._human_collaboration.confirm_command(
                    intent_id,
                    draft_revision,
                    draft_hash,
                    preview_ref,
                    preview_hash,
                    idempotency_key,
                )
            except OwnerConflict as error:
                # A caller/idempotency/verifier error does not make an otherwise
                # current Preview stale.  Persist invalidation only when HC has
                # rejected Preview currentness itself or the exact frozen
                # delivery binding no longer verifies.
                stale = error.code == "command_preview_stale"
                if not stale:
                    try:
                        self.verify_writing_delivery_binding(payload)
                    except OwnerConflict:
                        stale = True
                if stale:
                    self._human_collaboration.invalidate_command_preview(
                        intent_id,
                        draft_revision,
                        draft_hash,
                        preview_ref,
                        preview_hash,
                    )
                raise
        else:
            # Reconcile a lost service ACK without minting another per-use
            # confirmation or changing the stable external operation identity.
            confirmed = command
        payload = self._delivery_payload(confirmed)
        preview = confirmed.get("impact_preview")
        if not isinstance(preview, dict):
            raise OwnerConflict("writing_delivery_preview_missing")
        confirmation = _receipt(confirmed.get("confirmation_receipt"))
        self._agent_runtime.writing_delivery.admit(
            payload,
            intent_id=intent_id,
            draft_revision=cast(int, confirmed["draft_revision"]),
            draft_hash=cast(str, confirmed["draft_hash"]),
            preview_ref=cast(str, preview["preview_ref"]),
            preview_hash=cast(str, preview["preview_hash"]),
            confirmation=confirmation,
            idempotency_key=_operation_key(
                "writing-delivery-admission",
                intent_id,
                confirmation.receipt_ref,
            ),
        )
        return self._public_delivery_intent(confirmed)

    def query_delivery_intent(self, intent_id: str) -> dict[str, object]:
        return self._public_delivery_intent(
            self._human_collaboration.query_command(intent_id)
        )

    def query_delivery_operation(self, operation_ref: str) -> dict[str, object]:
        operation = self._agent_runtime.writing_delivery.query_operation(
            operation_ref
        )
        if operation is None:
            raise OwnerConflict("writing_delivery_operation_missing")
        return _public_delivery_operation(operation)

    def verify_writing_delivery_binding(
        self, payload: dict[str, object]
    ) -> None:
        """Fail closed against current RM/RG/renderer/provider truth."""

        normalized = normalize_writing_delivery_payload(payload)
        self._verified_delivery_artifact(normalized, verify_target=True)

    def next_runnable_delivery_operation_ref(
        self,
        *,
        excluded_operation_refs: frozenset[str] = frozenset(),
    ) -> str | None:
        """Return the oldest delivery not completed, backed off, or quarantined."""

        return self._agent_runtime.writing_delivery.next_runnable_operation_ref(
            retry_cutoff=(
                time.time() - _WRITING_DELIVERY_RETRY_BACKOFF_SECONDS
            ),
            excluded_operation_refs=excluded_operation_refs,
        )

    def process_delivery_once(
        self,
        *,
        expected_operation_ref: str | None = None,
        excluded_operation_refs: frozenset[str] = frozenset(),
    ) -> bool:
        """Execute or reconcile one exact confirmed AR-owned delivery operation."""

        now = time.time()
        if expected_operation_ref is None:
            operations = self._agent_runtime.writing_delivery.query_operations()
        else:
            expected = self._agent_runtime.writing_delivery.query_operation(
                expected_operation_ref
            )
            operations = () if expected is None else (expected,)
        for operation in operations:
            if (
                operation.operation_ref in excluded_operation_refs
                or operation.status == "completed"
            ):
                continue
            if (
                operation.status in {"partial", "outcome_unknown"}
                and now
                < operation.updated_at + _WRITING_DELIVERY_RETRY_BACKOFF_SECONDS
            ):
                continue
            try:
                artifact = self._verified_delivery_artifact(
                    operation.payload,
                    verify_target=operation.status in {"admitted", "partial"},
                    # Confirmation freezes one immutable version. A later
                    # successor may become current without widening or
                    # cancelling the already-authorized external effect.
                    require_current_binding=False,
                )
            except OwnerConflict as error:
                if operation.status in {
                    "executing",
                    "partial",
                    "outcome_unknown",
                } and error.code in {
                    "asset_custody_unavailable",
                    "writing_renderer_artifact_not_ready",
                    "writing_renderer_artifact_integrity_invalid",
                }:
                    # An ambiguous external effect must still be reconciled.
                    # Provider reconciliation binds the frozen artifact hash
                    # and does not need current RM bytes; it never re-executes
                    # the effect while custody is unavailable.
                    before = operation
                    after = self._agent_runtime.writing_delivery.reconcile(
                        operation.operation_ref,
                        artifact=None,
                    )
                    if after != before:
                        return True
                    continue
                if (
                    operation.status == "partial"
                    and operation.failure_code == error.code
                ):
                    continue
                self._agent_runtime.writing_delivery.record_preflight_failure(
                    operation.operation_ref, reason_code=error.code
                )
                return True
            content = None if operation.payload["action"] == "delete" else artifact
            before = operation
            after = self._agent_runtime.writing_delivery.execute_once(
                operation.operation_ref, artifact=content
            )
            if after != before:
                return True
        return False

    def next_runnable_claim(
        self,
        *,
        excluded_claims: frozenset[tuple[str, str, str]] = frozenset(),
    ) -> tuple[str, str, str] | None:
        """Return the exact oldest Attempt that still has a durable boundary."""

        self._agent_runtime.reconcile_pending_provider_cleanup(
            self._provider,
            unit_kinds=_WRITING_PROVIDER_UNIT_KINDS,
        )
        for run in self._agent_runtime.query_active_writing_reports():
            if self._agent_runtime.provider_quiescence_requested(run.run_ref):
                continue
            claim = (run.run_ref, run.attempt_ref, run.fence_ref)
            if claim in excluded_claims:
                continue
            if run.execution is None:
                return claim
            try:
                delivery = self._query_deliverable(run)
            except OwnerConflict:
                return claim
            if delivery is None:
                return claim
            if delivery.status in {"queued", "processing"}:
                # RM owns this durable retry.  It is not a runnable Writing
                # boundary and must not keep later Runs out of the queue.
                continue
            if delivery.status != "accepted" or delivery.asset is None:
                return claim
            try:
                decision = self._research_graph.query_writing_citation_decision(
                    run_ref=run.run_ref,
                    attempt_ref=run.attempt_ref,
                )
            except OwnerConflict:
                return claim
            if decision is None:
                return claim
            if decision.decision == "rejected":
                # Citation/content review is a durable warning while research
                # continues. It becomes a hard gate only if the user asks to
                # render for publication or external delivery.
                continue
            if run.document_type != "report":
                try:
                    renderer = self._query_renderer_artifact(
                        run,
                        decision=decision,
                        deliverable=delivery.asset,
                        output_format=self._renderer_registry.default_format(
                            run.document_type
                        ),
                    )
                except OwnerConflict as error:
                    if error.code == "asset_custody_unavailable":
                        continue
                    return claim
                if renderer is None:
                    return claim
        return None

    def process_once(
        self,
        *,
        expected_run_ref: str | None = None,
        expected_attempt_ref: str | None = None,
        expected_fence_ref: str | None = None,
    ) -> bool:
        """Cross exactly one durable boundary for the oldest active Writing Run."""

        expected = (expected_run_ref, expected_attempt_ref, expected_fence_ref)
        if any(item is not None for item in expected):
            if not all(isinstance(item, str) and item for item in expected):
                raise OwnerConflict("writing_worker_claim_invalid")
            observed = self._agent_runtime.query_writing_report(
                cast(str, expected_run_ref)
            )
            if (
                observed is None
                or (
                    observed.status != "active"
                    and not (
                        observed.status == "blocked"
                        and self._is_provider_contract_correction(
                            observed.failure_code or ""
                        )
                    )
                )
                or observed.attempt_ref != expected_attempt_ref
                or observed.fence_ref != expected_fence_ref
            ):
                return False
            runs = (observed,)
        else:
            runs = self._agent_runtime.query_active_writing_reports()
        for run in runs:
            if run.status == "blocked":
                self._resume_contract_correction(run)
                return True
            try:
                self._agent_runtime.verify_current_writing_attempt(
                    run_ref=run.run_ref,
                    attempt_ref=run.attempt_ref,
                    fence_ref=run.fence_ref,
                )
            except OwnerConflict as error:
                self._block_if_still_current(run, error.code)
                return True
            if run.execution is not None:
                self._acknowledge_durable_provider_boundary(
                    run, "writing_review"
                )
            elif run.checkpoint is not None:
                self._acknowledge_durable_provider_boundary(
                    run, "writing_primary"
                )
            try:
                command = self._human_collaboration.query_command(run.intent_id)
                payload = self._writing_payload(command)
                content_revision = self._content_revision(run)
                phase = (
                    "writing-primary"
                    if run.checkpoint is None
                    else "writing-review"
                )
                job_ref = self._agent_runtime.root_provider_continuation_job_ref(
                    root_kind="writing",
                    phase=phase,
                    run_ref=run.run_ref,
                    root_session_ref=run.root_session_ref,
                    base_job_ref=self._writing_job_ref(run, content_revision),
                )
                request = None
                if run.execution is None:
                    request = WritingSkillRequest(
                        run_ref=run.run_ref,
                        attempt_ref=run.attempt_ref,
                        fence_ref=run.fence_ref,
                        intent=cast(dict[str, object], payload["intent"]),
                        snapshot=cast(dict[str, object], payload["snapshot"]),
                        root_session_ref=run.root_session_ref,
                        revision=content_revision,
                        runtime_binding=run.runtime_binding,
                        native_session_ref=run.native_session_ref,
                        predecessor_version_ref=run.predecessor_version_ref,
                        predecessor_markdown_hash=run.predecessor_markdown_hash,
                        feedback=run.feedback,
                        source_materials=self._materialize_sources(payload),
                        job_ref=job_ref,
                        document_type=run.document_type,
                        profile_ref=writing_document_profile(
                            run.document_type
                        ).profile_ref,
                    )
            except OwnerConflict as error:
                self._block_if_still_current(run, error.code)
                return True
            if run.checkpoint is None:
                assert request is not None
                unit_ref: str | None = None
                provider_safe = True
                try:
                    unit_ref = self._begin_provider_unit(
                        run, "writing_primary", provider_job_ref=job_ref
                    )
                    draft = self._provider.generate_draft(request)
                    validate_writing_skill_draft(request, draft)
                    if self._agent_runtime.park_root_provider_session_for_human_request(
                        root_kind="writing",
                        phase="writing-primary",
                        run_ref=run.run_ref,
                        attempt_ref=run.attempt_ref,
                        fence_ref=run.fence_ref,
                        native_session_ref=draft.primary_session_ref,
                        runtime_binding_hash=run.runtime_binding_hash,
                    ):
                        self._finish_provider_job(job_ref)
                        return True
                    self._agent_runtime.record_writing_checkpoint(
                        run_ref=run.run_ref,
                        attempt_ref=run.attempt_ref,
                        fence_ref=run.fence_ref,
                        native_session_ref=draft.primary_session_ref,
                        markdown=draft.markdown,
                        citations=draft.citations,
                        runtime_binding=run.runtime_binding,
                        idempotency_key=_operation_key(
                            "writing-checkpoint", run.run_ref, run.attempt_ref
                        ),
                    )
                except (WritingSkillUnavailable, OwnerConflict) as error:
                    provider_safe = error.code != _PROVIDER_RECONCILIATION_PENDING
                    native_session_ref = getattr(error, "native_session_ref", None)
                    if (
                        isinstance(native_session_ref, str)
                        and self._agent_runtime.park_root_provider_session_for_human_request(
                            root_kind="writing",
                            phase="writing-primary",
                            run_ref=run.run_ref,
                            attempt_ref=run.attempt_ref,
                            fence_ref=run.fence_ref,
                            native_session_ref=native_session_ref,
                            runtime_binding_hash=run.runtime_binding_hash,
                        )
                    ):
                        self._finish_provider_job(job_ref)
                        return True
                    if (
                        isinstance(error, OwnerConflict)
                        and error.code == "runtime_run_suspended"
                    ):
                        self._finish_provider_job(job_ref)
                        return True
                    if self._agent_runtime.provider_quiescence_requested(run.run_ref):
                        return True
                    if not provider_safe:
                        raise
                    if not self._defer_provider_start_if_protection_wait(
                        run,
                        "writing_primary",
                        error,
                        provider_job_ref=job_ref,
                    ):
                        if self._is_provider_contract_correction(error.code):
                            if unit_ref is not None:
                                self._acknowledge_provider_unit(run, unit_ref)
                                unit_ref = None
                            self._schedule_correction_if_still_current(
                                run, error.code
                            )
                        else:
                            self._block_if_still_current(run, error.code)
                finally:
                    if unit_ref is not None and provider_safe:
                        self._acknowledge_provider_unit(run, unit_ref)
                return True
            if run.execution is None:
                assert request is not None
                draft = WritingSkillDraft(
                    markdown=run.checkpoint.markdown,
                    citations=run.checkpoint.citations,
                    primary_session_ref=run.checkpoint.native_session_ref,
                    adapter_kind="persisted_checkpoint",
                )
                resumed_request = WritingSkillRequest(
                    **{
                        **request.__dict__,
                        "native_session_ref": run.checkpoint.native_session_ref,
                    }
                )
                unit_ref = None
                provider_safe = True
                try:
                    unit_ref = self._begin_provider_unit(
                        run, "writing_review", provider_job_ref=job_ref
                    )
                    result = self._provider.review_draft(resumed_request, draft)
                    final_hash, citations_hash, review_hash = (
                        validate_writing_skill_result(resumed_request, draft, result)
                    )
                    if self._agent_runtime.park_root_provider_session_for_human_request(
                        root_kind="writing",
                        phase="writing-review",
                        run_ref=run.run_ref,
                        attempt_ref=run.attempt_ref,
                        fence_ref=run.fence_ref,
                        native_session_ref=result.primary_session_ref,
                        runtime_binding_hash=run.runtime_binding_hash,
                    ):
                        self._finish_provider_job(job_ref)
                        return True
                    review = {
                        "review_mode": result.review_mode,
                        "reviewer_agent_ref": result.reviewer_agent_ref,
                        "review_task_hash": result.review_task_hash,
                        "reviewed_markdown_hash": run.checkpoint.markdown_hash,
                        "findings": list(result.findings),
                        "dispositions": list(result.dispositions),
                        "final_markdown_hash": final_hash,
                        "citations_hash": citations_hash,
                        "review_hash": review_hash,
                    }
                    self._agent_runtime.record_writing_execution(
                        run_ref=run.run_ref,
                        attempt_ref=run.attempt_ref,
                        fence_ref=run.fence_ref,
                        reviewed_markdown=result.reviewed_markdown,
                        final_markdown=result.final_markdown,
                        citations=result.citations,
                        review=review,
                        runtime_binding=run.runtime_binding,
                        idempotency_key=_operation_key(
                            "writing-execution", run.run_ref, run.attempt_ref
                        ),
                    )
                except (WritingSkillUnavailable, OwnerConflict) as error:
                    provider_safe = error.code != _PROVIDER_RECONCILIATION_PENDING
                    review_session_ref = (
                        getattr(error, "native_session_ref", None)
                        or run.native_session_ref
                    )
                    if (
                        review_session_ref is not None
                        and self._agent_runtime.park_root_provider_session_for_human_request(
                            root_kind="writing",
                            phase="writing-review",
                            run_ref=run.run_ref,
                            attempt_ref=run.attempt_ref,
                            fence_ref=run.fence_ref,
                            native_session_ref=review_session_ref,
                            runtime_binding_hash=run.runtime_binding_hash,
                        )
                    ):
                        self._finish_provider_job(job_ref)
                        return True
                    if (
                        isinstance(error, OwnerConflict)
                        and error.code == "runtime_run_suspended"
                    ):
                        self._finish_provider_job(job_ref)
                        return True
                    if self._agent_runtime.provider_quiescence_requested(run.run_ref):
                        return True
                    if not provider_safe:
                        raise
                    if not self._defer_provider_start_if_protection_wait(
                        run,
                        "writing_review",
                        error,
                        provider_job_ref=job_ref,
                    ):
                        if self._is_provider_contract_correction(error.code):
                            if unit_ref is not None:
                                self._acknowledge_provider_unit(run, unit_ref)
                                unit_ref = None
                            self._schedule_correction_if_still_current(
                                run, error.code
                            )
                        else:
                            self._block_if_still_current(run, error.code)
                    return True
                finally:
                    if unit_ref is not None and provider_safe:
                        self._acknowledge_provider_unit(run, unit_ref)
                self._finish_provider_job(job_ref)
                return True
            try:
                delivery = self._query_deliverable(run)
            except OwnerConflict as error:
                self._block_if_still_current(run, error.code)
                return True
            if delivery is None:
                try:
                    accepted = self._research_memory.submit_asset_intake(
                        self._deliverable_request(run),
                        idempotency_key=_operation_key(
                            "writing-deliverable", run.run_ref, run.attempt_ref
                        ),
                        operation_namespace="writing_deliverable",
                    )
                except OwnerConflict as error:
                    self._block_if_still_current(run, error.code)
                    return True
                if accepted.status in {"queued", "processing"}:
                    return True
                if accepted.status != "accepted" or accepted.asset is None:
                    self._block_if_still_current(
                        run, self._deliverable_failure_code(accepted)
                    )
                return True
            if delivery.status in {"queued", "processing"}:
                continue
            if delivery.status != "accepted" or delivery.asset is None:
                self._block_if_still_current(
                    run, self._deliverable_failure_code(delivery)
                )
                return True
            try:
                decision = self._research_graph.query_writing_citation_decision(
                    run_ref=run.run_ref, attempt_ref=run.attempt_ref
                )
            except OwnerConflict as error:
                self._block_if_still_current(run, error.code)
                return True
            if decision is None:
                sources = cast(list[dict[str, object]], payload["snapshot"].get(
                    "accepted_sources", []
                ))
                allowed = tuple(
                    sorted(
                        {
                            cast(str, source["version_ref"])
                            for source in sources
                            if isinstance(source, dict)
                            and isinstance(source.get("version_ref"), str)
                        }
                    )
                )
                try:
                    self._research_graph.decide_writing_citations(
                        run_ref=run.run_ref,
                        attempt_ref=run.attempt_ref,
                        fence_ref=run.fence_ref,
                        quest_ref=run.quest_ref,
                        snapshot_ref=cast(str, payload["snapshot_ref"]),
                        snapshot_hash=cast(str, payload["snapshot_hash"]),
                        allowed_source_version_refs=allowed,
                        binding=delivery.asset.as_binding(),
                        citations=run.execution.citations,
                        final_markdown_hash=run.execution.final_markdown_hash,
                        citations_hash=run.execution.citations_hash,
                        execution_receipt=run.execution.receipt,
                    )
                except OwnerConflict as error:
                    self._block_if_still_current(run, error.code)
                return True
            if decision.decision == "rejected":
                # Keep the exact deliverable and RG feedback visible. Automatic
                # research progress is not fenced by citation quality; a person
                # may request another revision, while publish/delivery paths below
                # continue to require an accepted decision.
                continue
            if run.document_type != "report":
                output_format = self._renderer_registry.default_format(
                    run.document_type
                )
                existing_renderer = self._query_renderer_artifact(
                    run,
                    decision=decision,
                    deliverable=delivery.asset,
                    output_format=output_format,
                )
                if existing_renderer is None:
                    try:
                        self._ensure_renderer_artifact(
                            run,
                            decision=decision,
                            deliverable=delivery.asset,
                            output_format=output_format,
                        )
                    except OwnerConflict as error:
                        self._block_if_still_current(run, error.code)
                    return True
        return False

    def block_oldest_active(self) -> None:
        """Retire a watchdog-stalled claim without blocking later Writing Runs."""

        claim = self.next_runnable_claim()
        if claim is None:
            return
        self.block_writing_claim(
            run_ref=claim[0],
            attempt_ref=claim[1],
            fence_ref=claim[2],
        )

    def block_writing_claim(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
    ) -> None:
        """Persist a timeout only if the originally claimed Fence is current."""

        try:
            blocked = self._agent_runtime.fail_writing_report(
                run_ref=run_ref,
                attempt_ref=attempt_ref,
                fence_ref=fence_ref,
                failure_code="writing_operation_timeout",
            )
        except OwnerConflict:
            # A concurrent control or late completion already retired this
            # Fence; in either case the watchdog claim is no longer runnable.
            return
        if blocked.attempt_ref != attempt_ref or blocked.fence_ref != fence_ref:
            # No provider unit was ever claimed.  AR fenced the timed-out
            # technical Attempt and installed its retryable successor without
            # inventing a physical cancellation effect.
            return
        cancel_job = getattr(self._provider, "cancel_job", None)
        if callable(cancel_job):
            cancel_job(blocked.provider_job_ref)

    def control_report(
        self,
        run_ref: str,
        *,
        action: str,
        idempotency_key: str,
        expected_attempt_ref: str | None = None,
        expected_fence_ref: str | None = None,
    ) -> dict[str, object]:
        if action == "cancel":
            raise OwnerConflict("writing_cancel_confirmation_required")
        observed = self._agent_runtime.query_writing_report(run_ref)
        if observed is None:
            raise OwnerConflict("writing_run_not_found")
        controlled = self._agent_runtime.control_writing_report(
            run_ref,
            action=action,
            expected_attempt_ref=(
                observed.attempt_ref
                if expected_attempt_ref is None
                else expected_attempt_ref
            ),
            expected_fence_ref=(
                observed.fence_ref
                if expected_fence_ref is None
                else expected_fence_ref
            ),
            idempotency_key=_operation_key(
                "public-writing-control", run_ref, action, idempotency_key
            ),
        )
        if action in {"pause", "cancel"}:
            cancel_job = getattr(self._provider, "cancel_job", None)
            if callable(cancel_job):
                cancel_job(
                    self._writing_job_ref(
                        controlled, self._content_revision(controlled)
                    )
                )
        return self.query_writing_report(run_ref)

    def preview_report_cancellation(
        self, run_ref: str, *, idempotency_key: str
    ) -> dict[str, object]:
        run = self._agent_runtime.query_writing_report(run_ref)
        if run is None:
            raise OwnerConflict("writing_run_not_found")
        if run.status in {"cancelled", "completed"}:
            raise OwnerConflict("writing_run_terminal")
        create_key = _operation_key("writing-cancel-intent", run_ref, idempotency_key)
        command = self._human_collaboration.query_command_by_idempotency_key(
            create_key, command_kind="command_draft"
        )
        expected = {
            "command_kind": "writing_report_cancel",
            "payload": {
                "quest_ref": run.quest_ref,
                "run_ref": run.run_ref,
                "attempt_ref": run.attempt_ref,
                "fence_ref": run.fence_ref,
                "effect": "terminal_cancel_preserve_history",
            },
        }
        if command is None:
            command = self._human_collaboration.create_command_draft(
                f"writing-run:{run_ref}", expected, create_key
            )
        elif command.get("draft") != expected:
            raise OwnerConflict("idempotency_conflict")
        previewed = self._human_collaboration.preview_command(
            cast(str, command["intent_id"]),
            cast(int, command["draft_revision"]),
            cast(str, command["draft_hash"]),
            _operation_key("writing-cancel-preview", run_ref, idempotency_key),
        )
        return self._public_cancellation(previewed)

    def confirm_report_cancellation(
        self,
        run_ref: str,
        cancellation_intent_id: str,
        *,
        draft_revision: int,
        draft_hash: str,
        preview_ref: str,
        preview_hash: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        command = self._human_collaboration.query_command(cancellation_intent_id)
        if command.get("confirmation_receipt") is None:
            command = self._human_collaboration.confirm_command(
                cancellation_intent_id,
                draft_revision,
                draft_hash,
                preview_ref,
                preview_hash,
                _operation_key(
                    "writing-cancel-confirm", run_ref, idempotency_key
                ),
            )
        draft = command.get("draft")
        if not isinstance(draft, dict) or draft.get("command_kind") != (
            "writing_report_cancel"
        ):
            raise OwnerConflict("writing_cancel_confirmation_invalid")
        confirmation = _receipt(command.get("confirmation_receipt"))
        preview = command.get("impact_preview")
        if not isinstance(preview, dict):
            raise OwnerConflict("writing_cancel_confirmation_invalid")
        controlled = self._agent_runtime.control_writing_report(
            run_ref,
            action="cancel",
            idempotency_key=_operation_key(
                "public-writing-cancel", run_ref, idempotency_key
            ),
            cancellation_confirmation={
                "intent_id": cancellation_intent_id,
                "draft_revision": command["draft_revision"],
                "draft_hash": command["draft_hash"],
                "preview_ref": preview["preview_ref"],
                "preview_hash": preview["preview_hash"],
                **confirmation.as_public_dict(),
            },
        )
        cancel_job = getattr(self._provider, "cancel_job", None)
        if callable(cancel_job):
            cancel_job(self._writing_job_ref(controlled, self._content_revision(controlled)))
        return self.query_writing_report(run_ref)

    @staticmethod
    def _public_cancellation(command: dict[str, object]) -> dict[str, object]:
        preview = command.get("impact_preview")
        owner_preview: dict[str, object] = {}
        if isinstance(preview, dict):
            values = preview.get("owner_previews")
            if isinstance(values, list) and values and isinstance(values[0], dict):
                owner_preview = cast(dict[str, object], values[0])
        return {
            "intent_id": command["intent_id"],
            "draft_revision": command["draft_revision"],
            "draft_hash": command["draft_hash"],
            "impact_preview": (
                None
                if not isinstance(preview, dict)
                else {
                    "preview_ref": preview["preview_ref"],
                    "preview_hash": preview["preview_hash"],
                    "will_happen": owner_preview.get("will_happen", []),
                    "will_not_happen": owner_preview.get("will_not_happen", []),
                    "risks": owner_preview.get("risks", []),
                    "stale_conditions": owner_preview.get("stale_conditions", []),
                }
            ),
        }

    def request_revision(
        self,
        run_ref: str,
        *,
        feedback: tuple[str, ...],
        idempotency_key: str,
    ) -> dict[str, object]:
        if contains_secret(feedback):
            raise OwnerConflict("writing_revision_secret_forbidden")
        operation_key = _operation_key(
            "public-writing-revision", run_ref, idempotency_key
        )
        replay = self._agent_runtime.query_writing_revision_replay(
            run_ref=run_ref,
            feedback=feedback,
            idempotency_key=operation_key,
        )
        if replay is not None:
            return self.query_writing_report(replay.run_ref)
        run = self._agent_runtime.query_writing_report(run_ref)
        if run is None:
            raise OwnerConflict("writing_run_not_found")
        if run.status in {"cancelled", "completed"}:
            raise OwnerConflict("writing_run_terminal")
        if run.status != "active":
            raise OwnerConflict("writing_run_not_active")
        decision = self._research_graph.query_writing_citation_decision(
            run_ref=run_ref, attempt_ref=run.attempt_ref
        )
        if decision is None:
            raise OwnerConflict("writing_revision_basis_unavailable")
        revised = self._agent_runtime.begin_writing_revision(
            run_ref=run.run_ref,
            attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref,
            predecessor_version_ref=decision.asset.version_ref,
            feedback=feedback,
            decision_receipt=decision.receipt,
            decision_status=decision.decision,
            idempotency_key=operation_key,
        )
        return self.query_writing_report(revised.run_ref)

    def view_report_version(
        self,
        run_ref: str,
        *,
        version_ref: str,
    ) -> dict[str, object]:
        """Materialize an immutable RM deliverable without upgrading its status.

        This read seam deliberately accepts both RG-accepted and RG-rejected
        versions.  It proves only current RM availability; the separate formal
        renderer below remains gated on an accepted RG citation decision.
        """

        run = self._agent_runtime.query_writing_report(run_ref)
        if run is None:
            raise OwnerConflict("writing_run_not_found")
        decision = next(
            (
                item
                for item in self._research_graph.query_writing_citation_history(
                    run_ref
                )
                if item.asset.version_ref == version_ref
            ),
            None,
        )
        if decision is None:
            raise OwnerConflict("writing_version_not_found")
        materialized = self._research_memory.materialize_asset(version_ref)
        if hashlib.sha256(materialized.content).hexdigest() != decision.asset.content_hash:
            raise OwnerConflict("writing_deliverable_integrity_invalid")
        return {
            "format": "markdown",
            "media_type": "text/markdown; charset=utf-8",
            "version_ref": version_ref,
            "content_hash": decision.asset.content_hash,
            "citation_status": decision.decision,
            "formal_renderer": False,
            "content": materialized.content,
        }

    def render_report(
        self,
        run_ref: str,
        *,
        version_ref: str | None = None,
        format: str | None = None,
    ) -> dict[str, object]:
        run = self._agent_runtime.query_writing_report(run_ref)
        if run is None:
            raise OwnerConflict("writing_run_not_found")
        selected_format = format or self._renderer_registry.default_format(
            run.document_type
        )
        history = self._research_graph.query_writing_citation_history(run_ref)
        if version_ref is None:
            decision = next(
                (
                    item
                    for item in history
                    if item.attempt_ref == run.attempt_ref
                ),
                None,
            )
        else:
            decision = next(
                (
                    item
                    for item in history
                    if item.asset.version_ref == version_ref
                ),
                None,
            )
        if decision is None:
            if version_ref is not None:
                raise OwnerConflict("writing_version_not_found")
            raise OwnerConflict("writing_render_not_ready")
        if decision.decision != "accepted":
            raise OwnerConflict("writing_render_not_ready")
        materialized = self._research_memory.materialize_asset(
            decision.asset.version_ref
        )
        content = materialized.content
        try:
            markdown = content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise OwnerConflict("writing_deliverable_integrity_invalid") from error
        if run.document_type == "report":
            if selected_format != "markdown":
                raise OwnerConflict("writing_render_format_unsupported")
            # Preserve #125's accepted renderer identity and its no-new-asset
            # behavior.  A report renderer artifact is materialized only when
            # an external delivery preview needs a separately receipted RM
            # binding.
            render_hash = hashlib.sha256(
                b"meta-research/report-render/markdown/v1\0"
                + decision.asset.version_ref.encode("utf-8")
                + b"\0"
                + content
            ).hexdigest()
            return {
                "format": "markdown",
                "media_type": "text/markdown; charset=utf-8",
                "file_name": f"{run.run_ref}-report.md",
                "version_ref": decision.asset.version_ref,
                "content_hash": decision.asset.content_hash,
                "citation_status": decision.decision,
                "render_hash": render_hash,
                "content": content,
            }
        renderer = self._ensure_renderer_artifact(
            run,
            decision=decision,
            deliverable=decision.asset,
            output_format=selected_format,
            markdown=markdown,
        )
        asset = renderer["asset"]
        artifact = renderer["artifact"]
        materialized_artifact = self._research_memory.materialize_asset(
            cast(str, asset["version_ref"])
        )
        if hashlib.sha256(materialized_artifact.content).hexdigest() != artifact.content_hash:
            raise OwnerConflict("writing_renderer_artifact_integrity_invalid")
        return {
            "format": artifact.output_format,
            "media_type": artifact.media_type,
            "file_name": (
                f"{run.run_ref}-{run.document_type}{artifact.file_extension}"
            ),
            "version_ref": decision.asset.version_ref,
            "content_hash": decision.asset.content_hash,
            "citation_status": decision.decision,
            "render_hash": artifact.artifact_hash,
            "renderer_artifact": asset,
            "content": materialized_artifact.content,
        }

    def compare_report_versions(
        self,
        run_ref: str,
        *,
        left_version_ref: str,
        right_version_ref: str,
    ) -> dict[str, object]:
        run = self._agent_runtime.query_writing_report(run_ref)
        if run is None:
            raise OwnerConflict("writing_run_not_found")
        history = self._research_graph.query_writing_citation_history(run_ref)
        by_version = {decision.asset.version_ref: decision for decision in history}
        left = by_version.get(left_version_ref)
        right = by_version.get(right_version_ref)
        if left is None or right is None:
            raise OwnerConflict("writing_version_not_found")
        left_asset = self._research_memory.query_asset_version(left_version_ref)
        right_asset = self._research_memory.query_asset_version(right_version_ref)
        if left_asset is None or right_asset is None:
            raise OwnerConflict("writing_version_not_found")
        left_markdown = self._research_memory.materialize_asset(
            left_version_ref
        ).content.decode("utf-8")
        right_markdown = self._research_memory.materialize_asset(
            right_version_ref
        ).content.decode("utf-8")
        left_evidence = tuple(
            sorted({citation["source_version_ref"] for citation in left.citations})
        )
        right_evidence = tuple(
            sorted({citation["source_version_ref"] for citation in right.citations})
        )
        left_citations = {
            citation["citation_ref"]: citation for citation in left.citations
        }
        right_citations = {
            citation["citation_ref"]: citation for citation in right.citations
        }
        added_citation_refs = sorted(right_citations.keys() - left_citations.keys())
        removed_citation_refs = sorted(left_citations.keys() - right_citations.keys())
        changed_citations = [
            {
                "citation_ref": citation_ref,
                "left": left_citations[citation_ref],
                "right": right_citations[citation_ref],
            }
            for citation_ref in sorted(left_citations.keys() & right_citations.keys())
            if left_citations[citation_ref] != right_citations[citation_ref]
        ]
        command = self._human_collaboration.query_command(run.intent_id)
        payload = self._writing_payload(command)
        return {
            "run_ref": run_ref,
            "left_version_ref": left_version_ref,
            "right_version_ref": right_version_ref,
            "content": {
                "changed": left_asset.content_hash != right_asset.content_hash,
                "left_hash": left_asset.content_hash,
                "right_hash": right_asset.content_hash,
                "unified_diff": "".join(
                    difflib.unified_diff(
                        left_markdown.splitlines(keepends=True),
                        right_markdown.splitlines(keepends=True),
                        fromfile=left_version_ref,
                        tofile=right_version_ref,
                    )
                ),
            },
            "evidence": {
                "changed": left_evidence != right_evidence,
                "left_source_version_refs": list(left_evidence),
                "right_source_version_refs": list(right_evidence),
                "added_source_version_refs": sorted(
                    set(right_evidence) - set(left_evidence)
                ),
                "removed_source_version_refs": sorted(
                    set(left_evidence) - set(right_evidence)
                ),
            },
            "citation": {
                "changed": (
                    left.decision != right.decision
                    or left.citations != right.citations
                ),
                "left_status": left.decision,
                "right_status": right.decision,
                "left_citations": list(left.citations),
                "right_citations": list(right.citations),
                "added_citation_refs": added_citation_refs,
                "removed_citation_refs": removed_citation_refs,
                "changed_citations": changed_citations,
            },
            "snapshot": {
                "mode": "frozen",
                "snapshot_ref": payload["snapshot_ref"],
                "snapshot_hash": payload["snapshot_hash"],
            },
        }

    def _capture_snapshot(self, quest_ref: str) -> dict[str, object]:
        return self._snapshot_reader.capture(quest_ref)

    def _advancement_snapshot(self, initialization_id: str) -> dict[str, object]:
        cycle = self._advancement_engine.query_initial_cycle(initialization_id)
        if cycle is None:
            return {"cycle": None, "idea": None, "plan": None}

        def stage_value(stage: str) -> dict[str, object] | None:
            request = (
                self._advancement_engine.query_idea_stage_request(cycle.cycle_ref)
                if stage == "idea"
                else self._advancement_engine.query_plan_stage_request(cycle.cycle_ref)
            )
            if request is None:
                return None
            commit = (
                self._advancement_engine.query_idea_stage_commit(request.request_ref)
                if stage == "idea"
                else self._advancement_engine.query_plan_stage_commit(request.request_ref)
            )
            if commit is None:
                # Pending execution is not accepted report input and may advance
                # independently while the user confirms Writing.
                return None
            stage_run = (
                self._agent_runtime.query_idea_stage_run(request.request_ref)
                if stage == "idea"
                else self._agent_runtime.query_plan_stage_run(request.request_ref)
            )
            if (
                stage_run is None
                or stage_run.execution is None
                or stage_run.run_ref != commit.run_ref
            ):
                raise OwnerConflict("writing_stage_result_missing")
            submission_ref = stage_run.execution.submission_ref
            if stage == "idea":
                content = self._research_memory.query_idea_outcome_content(
                    submission_ref
                )
                decision = self._research_graph.query_idea_outcome_decision(
                    submission_ref
                )
                if (
                    content is None
                    or decision is None
                    or decision.decision != "accepted"
                    or decision.outcome_ref != commit.outcome_ref
                    or content.outcome_hash != decision.outcome_hash
                ):
                    raise OwnerConflict("writing_stage_result_invalid")
                result = {
                    "content_ref": content.content_ref,
                    "content_hash": content.payload_hash,
                    "outcome_hash": content.outcome_hash,
                    "outcome": content.outcome,
                    "content_receipt": content.receipt.as_public_dict(),
                    "acceptance_receipt": decision.receipt.as_public_dict(),
                }
            else:
                plan = self._research_memory.query_plan_document(submission_ref)
                decision = self._research_graph.query_formal_plan_decision(
                    submission_ref
                )
                if (
                    plan is None
                    or decision is None
                    or decision.decision != "accepted"
                    or decision.formal_plan_ref != commit.outcome_ref
                    or plan.plan_document_hash != decision.plan_document_hash
                ):
                    raise OwnerConflict("writing_stage_result_invalid")
                result = {
                    "content_ref": plan.content_ref,
                    "content_hash": plan.payload_hash,
                    "plan_document_hash": plan.plan_document_hash,
                    "plan_document": plan.plan_document,
                    "content_receipt": plan.receipt.as_public_dict(),
                    "acceptance_receipt": decision.receipt.as_public_dict(),
                }
            return {
                "commit_ref": commit.commit_ref,
                "request_ref": request.request_ref,
                "epoch": request.epoch,
                "outcome_ref": commit.outcome_ref,
                "outcome_kind": commit.outcome_kind,
                "disposition": commit.disposition,
                "receipt": commit.receipt.as_public_dict(),
                "result": result,
            }

        return {
            "cycle": {
                "cycle_ref": cycle.cycle_ref,
                "receipt": cycle.receipt.as_public_dict(),
            },
            "idea": stage_value("idea"),
            "plan": stage_value("plan"),
        }

    def _current_delivery_artifact(
        self,
        run: WritingRun,
        *,
        output_format: str,
        ensure: bool,
    ):
        decision = self._research_graph.query_writing_citation_decision(
            run_ref=run.run_ref, attempt_ref=run.attempt_ref
        )
        if decision is None or decision.decision != "accepted":
            raise OwnerConflict("writing_delivery_citation_not_accepted")
        delivery = self._query_deliverable(run)
        if delivery is None or delivery.status != "accepted" or delivery.asset is None:
            raise OwnerConflict("writing_delivery_version_not_accepted")
        if delivery.asset.as_binding() != decision.asset:
            raise OwnerConflict("writing_delivery_citation_binding_stale")
        source = self._research_memory.query_asset_inventory_item(
            decision.asset.version_ref
        )
        if source is None:
            raise OwnerConflict("writing_version_not_found")
        if (
            source.asset_ref != decision.asset.asset_ref
            or source.content_hash != decision.asset.content_hash
            or source.manifest_hash != decision.asset.manifest_hash
            or source.receipt != decision.asset.receipt
        ):
            raise OwnerConflict("writing_delivery_version_binding_stale")
        if source.integrity != "verified" or source.availability != "available":
            raise OwnerConflict("asset_custody_unavailable")
        renderer = (
            self._ensure_renderer_artifact(
                run,
                decision=decision,
                deliverable=decision.asset,
                output_format=output_format,
            )
            if ensure
            else self._query_renderer_artifact(
                run,
                decision=decision,
                deliverable=decision.asset,
                output_format=output_format,
            )
        )
        if renderer is None:
            raise OwnerConflict("writing_renderer_artifact_not_ready")
        return decision, renderer

    def _verified_delivery_artifact(
        self,
        payload: dict[str, object],
        *,
        verify_target: bool,
        require_current_binding: bool = True,
    ) -> bytes:
        normalized = normalize_writing_delivery_payload(payload)
        run = self._agent_runtime.query_writing_report(
            cast(str, normalized["run_ref"])
        )
        if run is None:
            raise OwnerConflict("writing_run_not_found")
        if run.document_type != normalized["document_type"]:
            raise OwnerConflict("writing_delivery_run_binding_stale")
        if require_current_binding:
            decision, renderer = self._current_delivery_artifact(
                run,
                output_format=cast(str, normalized["renderer_format"]),
                ensure=False,
            )
        else:
            decision = next(
                (
                    item
                    for item in self._research_graph.query_writing_citation_history(
                        run.run_ref
                    )
                    if item.decision_ref == normalized["citation_decision_ref"]
                ),
                None,
            )
            if decision is None or decision.decision != "accepted":
                raise OwnerConflict("writing_delivery_citation_not_accepted")
            source = self._research_memory.query_asset_inventory_item(
                decision.asset.version_ref
            )
            if source is None:
                raise OwnerConflict("writing_version_not_found")
            if (
                source.asset_ref != decision.asset.asset_ref
                or source.content_hash != decision.asset.content_hash
                or source.manifest_hash != decision.asset.manifest_hash
                or source.receipt != decision.asset.receipt
            ):
                raise OwnerConflict("writing_delivery_version_binding_stale")
            if source.integrity != "verified" or source.availability != "available":
                raise OwnerConflict("asset_custody_unavailable")
            renderer_asset = self._research_memory.query_asset_inventory_item(
                cast(str, normalized["renderer_version_ref"])
            )
            if renderer_asset is None:
                raise OwnerConflict("writing_renderer_artifact_not_ready")
            provenance = renderer_asset.provenance
            if (
                renderer_asset.asset_ref != normalized["renderer_asset_ref"]
                or renderer_asset.content_hash
                != normalized["renderer_content_hash"]
                or renderer_asset.manifest_hash
                != normalized["renderer_manifest_hash"]
                or renderer_asset.receipt.as_public_dict()
                != normalized["renderer_receipt"]
                or renderer_asset.media_type != normalized["renderer_media_type"]
                or provenance.get("schema_ref")
                != "meta-research/writing-renderer-artifact/v1"
                or provenance.get("run_ref") != normalized["run_ref"]
                or provenance.get("document_type") != normalized["document_type"]
                or provenance.get("source_asset_ref") != normalized["asset_ref"]
                or provenance.get("source_version_ref") != normalized["version_ref"]
                or provenance.get("source_content_hash") != normalized["content_hash"]
                or provenance.get("source_manifest_hash") != normalized["manifest_hash"]
                or provenance.get("source_receipt") != normalized["version_receipt"]
                or provenance.get("citation_decision_ref")
                != normalized["citation_decision_ref"]
                or provenance.get("citation_receipt")
                != normalized["citation_receipt"]
                or provenance.get("artifact_sha256")
                != normalized["renderer_artifact_sha256"]
                or provenance.get("output_format")
                != normalized["renderer_format"]
                or provenance.get("media_type")
                != normalized["renderer_media_type"]
            ):
                raise OwnerConflict("writing_delivery_binding_stale")
            if (
                renderer_asset.integrity != "verified"
                or renderer_asset.availability != "available"
            ):
                raise OwnerConflict("asset_custody_unavailable")
            if verify_target:
                self._agent_runtime.writing_delivery.verify_target_current(
                    cast(str, normalized["provider_ref"]),
                    cast(str, normalized["action"]),
                    cast(dict[str, object], normalized["target"]),
                    target_binding=normalized["target_binding"],
                )
            materialized = self._research_memory.materialize_asset(
                renderer_asset.version_ref
            )
            if (
                hashlib.sha256(materialized.content).hexdigest()
                != normalized["renderer_artifact_sha256"]
            ):
                raise OwnerConflict("writing_renderer_artifact_integrity_invalid")
            return materialized.content
        renderer_asset = cast(dict[str, object], renderer["asset"])
        artifact = cast(WritingRenderedArtifact, renderer["artifact"])
        expected = {
            "asset_ref": decision.asset.asset_ref,
            "version_ref": decision.asset.version_ref,
            "content_hash": decision.asset.content_hash,
            "manifest_hash": decision.asset.manifest_hash,
            "version_receipt": decision.asset.receipt.as_public_dict(),
            "citation_decision_ref": decision.decision_ref,
            "citation_receipt": decision.receipt.as_public_dict(),
            "renderer_asset_ref": renderer_asset["asset_ref"],
            "renderer_version_ref": renderer_asset["version_ref"],
            "renderer_content_hash": renderer_asset["content_hash"],
            "renderer_manifest_hash": renderer_asset["manifest_hash"],
            "renderer_artifact_sha256": artifact.content_hash,
            "renderer_format": artifact.output_format,
            "renderer_media_type": artifact.media_type,
            "renderer_receipt": renderer_asset["receipt"],
        }
        if any(normalized[key] != value for key, value in expected.items()):
            raise OwnerConflict("writing_delivery_binding_stale")
        if verify_target:
            self._agent_runtime.writing_delivery.verify_target_current(
                cast(str, normalized["provider_ref"]),
                cast(str, normalized["action"]),
                cast(dict[str, object], normalized["target"]),
                target_binding=normalized["target_binding"],
            )
        materialized = self._research_memory.materialize_asset(
            cast(str, normalized["renderer_version_ref"])
        )
        if hashlib.sha256(materialized.content).hexdigest() != artifact.content_hash:
            raise OwnerConflict("writing_renderer_artifact_integrity_invalid")
        return materialized.content

    def _public_delivery_intent(
        self, command: dict[str, object]
    ) -> dict[str, object]:
        payload = self._delivery_payload(command)
        operation = self._agent_runtime.writing_delivery.query_operation(
            cast(str, payload["operation_ref"])
        )
        preview = command.get("impact_preview")
        public_preview = None
        if isinstance(preview, dict):
            owner_previews = preview.get("owner_previews")
            owner_preview = (
                owner_previews[0]
                if isinstance(owner_previews, list) and owner_previews
                else {}
            )
            public_preview = {
                "preview_ref": preview.get("preview_ref"),
                "preview_hash": preview.get("preview_hash"),
                "status": preview.get("status"),
                "owner_revisions": preview.get("owner_revisions"),
                "target_assertion": owner_preview.get("target_assertion"),
                "will_happen": owner_preview.get("will_happen", []),
                "will_not_happen": owner_preview.get("will_not_happen", []),
                "risks": owner_preview.get("risks", []),
                "stale_conditions": owner_preview.get("stale_conditions", []),
            }
        public_operation = (
            None if operation is None else _public_delivery_operation(operation)
        )
        return {
            "intent_id": command["intent_id"],
            "status": (
                "not_attempted"
                if public_operation is None
                else public_operation["status"]
            ),
            "confirmation_status": command["status"],
            "draft_revision": command["draft_revision"],
            "draft_hash": command["draft_hash"],
            "payload": payload,
            "impact_preview": public_preview,
            "confirmation_receipt": command.get("confirmation_receipt"),
            "operation": public_operation,
        }

    def _public_intent(self, command: dict[str, object]) -> dict[str, object]:
        payload = self._writing_payload(command)
        intent_id = cast(str, command["intent_id"])
        run = self._agent_runtime.query_writing_report_by_intent(intent_id)
        preview = command.get("impact_preview")
        public_preview = None
        if isinstance(preview, dict):
            owner_previews = preview.get("owner_previews")
            owner_preview = (
                owner_previews[0]
                if isinstance(owner_previews, list) and owner_previews
                else {}
            )
            public_preview = {
                "preview_ref": preview.get("preview_ref"),
                "preview_hash": preview.get("preview_hash"),
                "status": preview.get("status"),
                "owner_revisions": preview.get("owner_revisions"),
                "target_assertion": owner_preview.get("target_assertion"),
                "snapshot_hash": payload["snapshot_hash"],
                "will_happen": owner_preview.get("will_happen", []),
                "will_not_happen": owner_preview.get("will_not_happen", []),
                "risks": owner_preview.get("risks", []),
                "stale_conditions": owner_preview.get("stale_conditions", []),
            }
        result = {
            "intent_id": intent_id,
            "status": (
                command["status"]
                if run is None
                else {
                    "active": "running",
                    "paused": "paused",
                    "blocked": "blocked",
                    "cancelled": "cancelled",
                    "completed": "completed",
                }[run.status]
            ),
            "document_type": payload["document_type"],
            "draft_revision": command["draft_revision"],
            "draft_hash": command["draft_hash"],
            "intent": payload["intent"],
            "snapshot": payload["snapshot"],
            "impact_preview": public_preview,
            "confirmation_receipt": command.get("confirmation_receipt"),
            "run": (
                None
                if run is None
                else _public_run(run, content_revision=self._content_revision(run))
            ),
            "execution": {"status": "not_attempted"},
            "deliverable": {"status": "not_attempted"},
            "citation": {"status": "not_attempted"},
            "renderer": {"status": "not_attempted"},
        }
        if run is not None:
            result.update(self._public_layers(run))
        return result

    def _public_layers(self, run: WritingRun) -> dict[str, object]:
        checkpoint = None
        if run.checkpoint is not None:
            checkpoint = {
                "checkpoint_ref": run.checkpoint.checkpoint_ref,
                "markdown_hash": run.checkpoint.markdown_hash,
                "citations_hash": run.checkpoint.citations_hash,
                "native_session_ref": run.checkpoint.native_session_ref,
            }
        if run.execution is None:
            execution = {
                "status": "running" if checkpoint is not None else "admitted",
                "checkpoint": checkpoint,
                "receipt": None,
            }
        else:
            execution = {
                "status": "completed",
                "checkpoint": checkpoint,
                "final_markdown_hash": run.execution.final_markdown_hash,
                "citations_hash": run.execution.citations_hash,
                "receipt": run.execution.receipt.as_public_dict(),
            }
        delivery = self._query_deliverable(run)
        deliverable: dict[str, object] = {"status": "not_attempted"}
        deliverable_current = False
        if delivery is not None:
            if delivery.status != "accepted" or delivery.asset is None:
                deliverable = {
                    "status": delivery.status,
                    "failure": (
                        None
                        if delivery.failure_code is None
                        else {"code": delivery.failure_code}
                    ),
                }
            else:
                current_asset = self._research_memory.query_asset_inventory_item(
                    delivery.asset.version_ref
                )
                if current_asset is None:
                    raise OwnerConflict("writing_version_not_found")
                deliverable_current = (
                    current_asset.integrity == "verified"
                    and current_asset.availability == "available"
                )
                deliverable = {
                    "status": (
                        "accepted" if deliverable_current else "unavailable"
                    ),
                    "acceptance_status": "accepted",
                    **delivery.asset.as_public_dict(),
                    "integrity": current_asset.integrity,
                    "availability": current_asset.availability,
                    "failure": (
                        None
                        if deliverable_current
                        else {"code": "asset_custody_unavailable"}
                    ),
                }
        decision = self._research_graph.query_writing_citation_decision(
            run_ref=run.run_ref, attempt_ref=run.attempt_ref
        )
        citation: dict[str, object] = {"status": "not_attempted"}
        renderer: dict[str, object] = {"status": "not_attempted"}
        if decision is not None:
            citation = decision.as_public_dict()
            if decision.decision == "accepted":
                if not deliverable_current:
                    renderer = {
                        "status": "unavailable",
                        "reason": {"code": "asset_custody_unavailable"},
                    }
                elif run.document_type == "report":
                    # Exact #125 projection compatibility.
                    renderer = {"status": "ready"}
                else:
                    output_format = self._renderer_registry.default_format(
                        run.document_type
                    )
                    try:
                        rendered = self._query_renderer_artifact(
                            run,
                            decision=decision,
                            deliverable=decision.asset,
                            output_format=output_format,
                        )
                    except OwnerConflict as error:
                        renderer = {
                            "status": "unavailable",
                            "reason": {"code": error.code},
                            "default_format": output_format,
                            "formats": list(
                                self._renderer_registry.formats(
                                    run.document_type
                                )
                            ),
                        }
                    else:
                        renderer = {
                            "status": (
                                "pending" if rendered is None else "ready"
                            ),
                            "default_format": output_format,
                            "formats": list(
                                self._renderer_registry.formats(
                                    run.document_type
                                )
                            ),
                            "artifact": (
                                None if rendered is None else rendered["asset"]
                            ),
                        }
        versions: list[dict[str, object]] = []
        for historical in self._research_graph.query_writing_citation_history(
            run.run_ref
        ):
            asset = self._research_memory.query_asset_inventory_item(
                historical.asset.version_ref
            )
            if asset is None:
                raise OwnerConflict("writing_version_not_found")
            versions.append(
                {
                    "version_ref": asset.version_ref,
                    "asset_ref": asset.asset_ref,
                    "version_number": asset.version_number,
                    "content_hash": asset.content_hash,
                    "accepted_at": asset.accepted_at,
                    "integrity": asset.integrity,
                    "availability": asset.availability,
                    "citation_status": historical.decision,
                    "citations": list(historical.citations),
                    "citation_feedback": list(historical.feedback),
                    "deliverable_receipt": asset.receipt.as_public_dict(),
                    "citation_receipt": historical.receipt.as_public_dict(),
                }
            )
        return {
            "execution": execution,
            "deliverable": deliverable,
            "citation": citation,
            "renderer": renderer,
            "versions": versions,
            "deliveries": [
                self._public_delivery_intent(command)
                for command in self._human_collaboration.query_commands(
                    command_kind="writing_external_delivery"
                )
                if self._delivery_payload(command)["run_ref"] == run.run_ref
            ],
        }

    def _deliverable_request(self, run: WritingRun) -> AssetIntakeRequest:
        if run.execution is None:
            raise OwnerConflict("writing_execution_not_completed")
        asset_ref = None
        if run.predecessor_version_ref is not None:
            predecessor = self._research_memory.query_asset_version(
                run.predecessor_version_ref
            )
            if predecessor is None:
                raise OwnerConflict("writing_predecessor_version_missing")
            asset_ref = predecessor.asset_ref
        payload = self._writing_payload(
            self._human_collaboration.query_command(run.intent_id)
        )
        return AssetIntakeRequest(
            source_kind="text",
            custody_mode="managed",
            display_name=(
                f"{run.run_ref}-{run.document_type}-"
                f"r{self._content_revision(run)}.md"
            ),
            media_type="text/markdown; charset=utf-8",
            content=run.execution.final_markdown.encode("utf-8"),
            provenance={
                "schema_ref": "meta-research/writing-deliverable-provenance/v1",
                "run_ref": run.run_ref,
                "attempt_ref": run.attempt_ref,
                "fence_ref": run.fence_ref,
                "quest_ref": run.quest_ref,
                "snapshot_ref": payload["snapshot_ref"],
                "snapshot_hash": payload["snapshot_hash"],
                "final_markdown_hash": run.execution.final_markdown_hash,
                "citations_hash": run.execution.citations_hash,
                "execution_receipt": run.execution.receipt.as_public_dict(),
                "predecessor_version_ref": run.predecessor_version_ref,
            },
            asset_ref=asset_ref,
        )

    def _query_deliverable(self, run: WritingRun) -> AssetIntakeResult | None:
        if run.execution is None:
            return None
        return self._research_memory.query_asset_intake_by_idempotency_key(
            _operation_key("writing-deliverable", run.run_ref, run.attempt_ref),
            self._deliverable_request(run),
            operation_namespace="writing_deliverable",
        )

    def _renderer_artifact_request(
        self,
        run: WritingRun,
        *,
        decision,
        deliverable,
        artifact: WritingRenderedArtifact,
    ) -> AssetIntakeRequest:
        revision = next(
            (
                index
                for index, item in enumerate(
                    self._research_graph.query_writing_citation_history(
                        run.run_ref
                    ),
                    start=1,
                )
                if item.decision_ref == decision.decision_ref
            ),
            None,
        )
        if revision is None:
            raise OwnerConflict("writing_version_not_found")
        return AssetIntakeRequest(
            source_kind="file",
            custody_mode="managed",
            display_name=(
                f"{run.run_ref}-{run.document_type}-"
                f"r{revision}{artifact.file_extension}"
            ),
            media_type=artifact.media_type,
            content=artifact.content,
            provenance={
                "schema_ref": "meta-research/writing-renderer-artifact/v1",
                "run_ref": run.run_ref,
                "attempt_ref": decision.attempt_ref,
                "document_type": run.document_type,
                "source_asset_ref": deliverable.asset_ref,
                "source_version_ref": deliverable.version_ref,
                "source_content_hash": deliverable.content_hash,
                "source_manifest_hash": deliverable.manifest_hash,
                "source_receipt": deliverable.receipt.as_public_dict(),
                "citation_decision_ref": decision.decision_ref,
                "citation_receipt": decision.receipt.as_public_dict(),
                "profile_ref": artifact.profile_ref,
                "renderer_ref": artifact.renderer_ref,
                "renderer_input_hash": artifact.renderer_input_hash,
                "renderer_artifact_hash": artifact.artifact_hash,
                "artifact_sha256": artifact.content_hash,
                "output_format": artifact.output_format,
                "media_type": artifact.media_type,
            },
        )

    def _rendered_artifact(
        self,
        run: WritingRun,
        *,
        decision,
        output_format: str,
        markdown: str | None = None,
    ) -> WritingRenderedArtifact:
        if decision.decision != "accepted":
            raise OwnerConflict("writing_render_not_ready")
        if markdown is None:
            materialized = self._research_memory.materialize_asset(
                decision.asset.version_ref
            )
            if hashlib.sha256(materialized.content).hexdigest() != decision.asset.content_hash:
                raise OwnerConflict("writing_deliverable_integrity_invalid")
            try:
                markdown = materialized.content.decode("utf-8")
            except UnicodeDecodeError as error:
                raise OwnerConflict("writing_deliverable_integrity_invalid") from error
        return self._renderer_registry.render(
            run.document_type,
            markdown,
            decision.citations,
            output_format=output_format,
        )

    def _query_renderer_artifact(
        self,
        run: WritingRun,
        *,
        decision,
        deliverable,
        output_format: str,
    ) -> dict[str, object] | None:
        artifact = self._rendered_artifact(
            run,
            decision=decision,
            output_format=output_format,
        )
        request = self._renderer_artifact_request(
            run,
            decision=decision,
            deliverable=deliverable,
            artifact=artifact,
        )
        result = self._research_memory.query_asset_intake_by_idempotency_key(
            _operation_key(
                "writing-renderer",
                decision.asset.version_ref,
                artifact.renderer_input_hash,
            ),
            request,
        )
        if result is None:
            return None
        return self._accepted_renderer_artifact(result, artifact)

    def _ensure_renderer_artifact(
        self,
        run: WritingRun,
        *,
        decision,
        deliverable,
        output_format: str,
        markdown: str | None = None,
    ) -> dict[str, object]:
        artifact = self._rendered_artifact(
            run,
            decision=decision,
            output_format=output_format,
            markdown=markdown,
        )
        request = self._renderer_artifact_request(
            run,
            decision=decision,
            deliverable=deliverable,
            artifact=artifact,
        )
        key = _operation_key(
            "writing-renderer",
            decision.asset.version_ref,
            artifact.renderer_input_hash,
        )
        result = self._research_memory.query_asset_intake_by_idempotency_key(
            key, request
        )
        if result is None:
            result = self._research_memory.submit_asset_intake(
                request,
                idempotency_key=key,
            )
        return self._accepted_renderer_artifact(result, artifact)

    def _accepted_renderer_artifact(
        self,
        result: AssetIntakeResult,
        artifact: WritingRenderedArtifact,
    ) -> dict[str, object]:
        if result.status != "accepted" or result.asset is None:
            code = result.failure_code or f"asset_intake_{result.status}"
            raise OwnerConflict(f"writing_renderer:{code}"[:128])
        current = self._research_memory.query_asset_inventory_item(
            result.asset.version_ref
        )
        if current is None:
            raise OwnerConflict("writing_renderer_artifact_not_found")
        if (
            current.integrity != "verified"
            or current.availability != "available"
        ):
            raise OwnerConflict("asset_custody_unavailable")
        materialized = self._research_memory.materialize_asset(
            result.asset.version_ref
        )
        if hashlib.sha256(materialized.content).hexdigest() != artifact.content_hash:
            raise OwnerConflict("writing_renderer_artifact_integrity_invalid")
        return {
            "artifact": artifact,
            "asset": {
                **result.asset.as_public_dict(),
                "artifact_sha256": artifact.content_hash,
                "renderer_artifact_hash": artifact.artifact_hash,
                "renderer_input_hash": artifact.renderer_input_hash,
                "renderer_ref": artifact.renderer_ref,
                "format": artifact.output_format,
                "media_type": artifact.media_type,
                "integrity": current.integrity,
                "availability": current.availability,
            },
        }

    def _content_revision(self, run: WritingRun) -> int:
        history = self._research_graph.query_writing_citation_history(run.run_ref)
        for revision, decision in enumerate(history, start=1):
            if decision.attempt_ref == run.attempt_ref:
                return revision
        return len(history) + 1

    def _materialize_sources(
        self, payload: dict[str, object]
    ) -> tuple[WritingSourceMaterial, ...]:
        snapshot = payload.get("snapshot")
        if not isinstance(snapshot, dict):
            raise OwnerConflict("writing_snapshot_invalid")
        sources = snapshot.get("accepted_sources")
        if not isinstance(sources, list):
            raise OwnerConflict("writing_snapshot_invalid")
        materials: list[WritingSourceMaterial] = []
        bindings_by_version: dict[str, tuple[object, object, object]] = {}
        total_source_bytes = 0
        for source in sources:
            if not isinstance(source, dict):
                raise OwnerConflict("writing_snapshot_invalid")
            version_ref = source.get("version_ref")
            if not isinstance(version_ref, str):
                raise OwnerConflict("writing_snapshot_invalid")
            frozen_binding = (
                source.get("content_hash"),
                source.get("manifest_hash"),
                source.get("asset_receipt"),
            )
            prior_binding = bindings_by_version.get(version_ref)
            if prior_binding is not None:
                if prior_binding != frozen_binding:
                    raise OwnerConflict("writing_snapshot_invalid")
                # A version may legitimately carry several RG roles. It is one
                # immutable byte source and must be staged exactly once.
                continue
            bindings_by_version[version_ref] = frozen_binding
            asset = self._research_memory.query_asset_version(version_ref)
            if asset is None or (
                asset.content_hash != source.get("content_hash")
                or asset.manifest_hash != source.get("manifest_hash")
                or asset.receipt.as_public_dict() != source.get("asset_receipt")
            ):
                raise OwnerConflict("writing_snapshot_asset_drift")
            total_source_bytes += asset.byte_count
            if total_source_bytes > WRITING_MAX_SOURCE_BYTES:
                raise OwnerConflict("writing_source_material_too_large")
            materialized = self._research_memory.materialize_asset(version_ref)
            if materialized.memory_ref != version_ref:
                raise OwnerConflict("writing_source_material_invalid")
            materials.append(
                WritingSourceMaterial(
                    version_ref=version_ref,
                    content_hash=asset.content_hash,
                    file_name=materialized.file_name,
                    media_type=materialized.media_type,
                    content=materialized.content,
                    materialized_sha256=hashlib.sha256(
                        materialized.content
                    ).hexdigest(),
                )
            )
        return tuple(materials)

    @staticmethod
    def _writing_job_ref(run: WritingRun, content_revision: int) -> str:
        del content_revision
        return run.provider_job_ref

    @staticmethod
    def _provider_unit_ref(
        run: WritingRun,
        unit_kind: str,
        *,
        provider_job_ref: str | None = None,
    ) -> str:
        return "provider_unit_" + canonical_hash(
            {
                "provider_job_ref": provider_job_ref or run.provider_job_ref,
                "attempt_ref": run.attempt_ref,
                "unit_kind": unit_kind,
            }
        )[:64]

    def _finish_provider_job(self, job_ref: str) -> None:
        finish_job = getattr(self._provider, "finish_job", None)
        if callable(finish_job):
            finish_job(job_ref)

    def _begin_provider_unit(
        self,
        run: WritingRun,
        unit_kind: str,
        *,
        provider_job_ref: str | None = None,
    ) -> str:
        operation_ref = provider_job_ref or run.provider_job_ref
        unit_ref = self._provider_unit_ref(
            run, unit_kind, provider_job_ref=operation_ref
        )
        self._agent_runtime.begin_provider_unit(
            unit_ref=unit_ref,
            operation_ref=operation_ref,
            run_ref=run.run_ref,
            attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref,
            unit_kind=unit_kind,
        )
        return unit_ref

    def _defer_provider_start_if_protection_wait(
        self,
        run: WritingRun,
        unit_kind: str,
        error: WritingSkillUnavailable | OwnerConflict,
        *,
        provider_job_ref: str | None = None,
    ) -> bool:
        if not (
            isinstance(error, OwnerConflict)
            and isinstance(error.__cause__, RuntimeProtectionUnavailable)
        ):
            return False
        self._agent_runtime.record_writing_provider_not_started(
            unit_ref=self._provider_unit_ref(
                run, unit_kind, provider_job_ref=provider_job_ref
            ),
            run_ref=run.run_ref,
            attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref,
            reason_code=error.code,
        )
        return True

    def _acknowledge_provider_unit(self, run: WritingRun, unit_ref: str) -> None:
        self._agent_runtime.acknowledge_provider_safe_point(
            unit_ref=unit_ref,
            run_ref=run.run_ref,
            attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref,
        )

    def _acknowledge_durable_provider_boundary(
        self, run: WritingRun, unit_kind: str
    ) -> None:
        try:
            self._acknowledge_provider_unit(
                run,
                self._provider_unit_ref(run, unit_kind),
            )
        except OwnerConflict as error:
            if error.code != "runtime_provider_unit_not_found":
                raise

    @staticmethod
    def _deliverable_failure_code(result: AssetIntakeResult) -> str:
        reason = result.failure_code or f"asset_intake_{result.status}"
        return f"writing_deliverable:{reason}"[:128]

    def _block_if_still_current(self, run: WritingRun, code: str) -> None:
        current = self._agent_runtime.query_writing_report(run.run_ref)
        if (
            current is not None
            and current.status == "active"
            and current.attempt_ref == run.attempt_ref
            and current.fence_ref == run.fence_ref
        ):
            self._agent_runtime.fail_writing_report(
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                failure_code=code,
            )

    @staticmethod
    def _is_provider_contract_correction(code: str) -> bool:
        return code in _WRITING_PROVIDER_CONTRACT_CORRECTIONS or code.startswith(
            "codex_child_review_"
        )

    def _schedule_correction_if_still_current(
        self, run: WritingRun, code: str
    ) -> None:
        """Retain the failed Attempt and immediately install its successor."""

        current = self._agent_runtime.query_writing_report(run.run_ref)
        if (
            current is None
            or current.status != "active"
            or current.attempt_ref != run.attempt_ref
            or current.fence_ref != run.fence_ref
        ):
            return
        blocked = self._agent_runtime.fail_writing_report(
            run_ref=run.run_ref,
            attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref,
            failure_code=code,
            recoverable_contract=True,
        )
        if blocked.status != "blocked":
            return
        self._resume_contract_correction(blocked)

    def _resume_contract_correction(self, run: WritingRun) -> None:
        code = run.failure_code or ""
        if run.status != "blocked" or not self._is_provider_contract_correction(
            code
        ):
            return
        self._agent_runtime.control_writing_report(
            run.run_ref,
            action="resume",
            idempotency_key=_operation_key(
                "writing-contract-correction",
                run.run_ref,
                run.attempt_ref,
                code,
            ),
            expected_attempt_ref=run.attempt_ref,
            expected_fence_ref=run.fence_ref,
        )

    @staticmethod
    def _writing_payload(command: dict[str, object]) -> dict[str, object]:
        draft = command.get("draft")
        if not isinstance(draft, dict) or draft.get("command_kind") != "writing_report_start":
            raise OwnerConflict("writing_intent_not_found")
        payload = draft.get("payload")
        if not isinstance(payload, dict):
            raise OwnerConflict("writing_intent_invalid")
        return payload

    @staticmethod
    def _delivery_payload(command: dict[str, object]) -> dict[str, object]:
        draft = command.get("draft")
        if (
            not isinstance(draft, dict)
            or draft.get("command_kind") != "writing_external_delivery"
        ):
            raise OwnerConflict("writing_delivery_intent_not_found")
        return normalize_writing_delivery_payload(draft.get("payload"))


def _public_delivery_operation(operation) -> dict[str, object]:
    value = operation.as_public_dict()
    authority_status = cast(str, value["status"])
    value["authority_status"] = authority_status
    value["status"] = {
        "admitted": "not_attempted",
        "executing": "partial",
        "partial": "partial",
        "outcome_unknown": "outcome_unknown",
        "completed": "completed",
    }[authority_status]
    return value


def _public_run(
    run: WritingRun, *, content_revision: int
) -> dict[str, object]:
    return {
        "run_ref": run.run_ref,
        "document_type": run.document_type,
        "status": run.status,
        "attempt_ref": run.attempt_ref,
        "attempt_generation": run.attempt_generation,
        "content_revision": content_revision,
        "root_session_ref": run.root_session_ref,
        "native_session_ref": run.native_session_ref,
        "fence_ref": run.fence_ref,
        "runtime_binding_hash": run.runtime_binding_hash,
        "execution_budget": run.execution_budget,
        "output_bytes": run.output_bytes,
        "blocker": (
            None if run.failure_code is None else {"code": run.failure_code}
        ),
    }


def _receipt(value: object) -> AcceptanceReceipt:
    if not isinstance(value, dict):
        raise OwnerConflict("writing_confirmation_receipt_missing")
    try:
        return AcceptanceReceipt(
            issuer=cast(str, value["issuer"]),
            kind=cast(str, value["kind"]),
            receipt_ref=cast(str, value["receipt_ref"]),
            subject_ref=cast(str, value["subject_ref"]),
            payload_hash=cast(str, value["payload_hash"]),
        )
    except (KeyError, TypeError) as error:
        raise OwnerConflict("writing_confirmation_receipt_invalid") from error


def _operation_key(*parts: str) -> str:
    return "writing:" + canonical_hash(list(parts))
