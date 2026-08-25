from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from typing import Protocol, cast

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from meta_research.database import Database
from meta_research.feed import DurableFeed
from meta_research.owners.common import (
    AcceptanceReceipt,
    OwnerConflict,
    canonical_hash,
    canonical_json,
    decoded_object,
)
from meta_research.owners.human_requests import HumanResponseVerifier
from meta_research.owners.secret_detection import contains_secret
from meta_research.writing_delivery import (
    WritingDeliveryOutcomeUnknown,
    WritingDeliveryProviderObservation,
    WritingDeliveryProviderRegistry,
    normalize_writing_delivery_payload,
)


AR_OWNER = "agent_runtime"
_RECEIPT_SCHEMA = "meta-research/owner-acceptance-receipt/v1"
_PROVIDER_OBSERVATION_REF = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._:-]{0,95}\Z"
)


class WritingDeliveryBindingVerifier(Protocol):
    """Read-only RM/RG/renderer proof verifier injected into AR admission."""

    def verify_writing_delivery_binding(self, payload: dict[str, object]) -> None: ...


@dataclass(frozen=True)
class WritingDeliveryObservation:
    observation_ref: str
    provider_ref: str
    provider_operation_ref: str
    outcome: str
    observed_at: float
    details: dict[str, object]
    observation_hash: str

    def as_public_dict(self) -> dict[str, object]:
        return {
            "observation_ref": self.observation_ref,
            "provider_ref": self.provider_ref,
            "provider_operation_ref": self.provider_operation_ref,
            "outcome": self.outcome,
            "observed_at": self.observed_at,
            "details": self.details,
            "observation_hash": self.observation_hash,
        }


@dataclass(frozen=True)
class WritingDeliveryOperation:
    operation_ref: str
    payload: dict[str, object]
    payload_hash: str
    status: str
    attempt_count: int
    provider_operation_ref: str
    provider_request_hash: str | None
    operation_receipt: AcceptanceReceipt
    execution_receipt: AcceptanceReceipt | None
    reconciliation_receipt: AcceptanceReceipt | None
    provider_observations: tuple[WritingDeliveryObservation, ...]
    failure_code: str | None
    created_at: float
    updated_at: float
    completed_at: float | None

    def as_public_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "operation_ref": self.operation_ref,
            "payload": self.payload,
            "payload_hash": self.payload_hash,
            "status": self.status,
            "attempt_count": self.attempt_count,
            "provider_operation_ref": self.provider_operation_ref,
            "provider_request_hash": self.provider_request_hash,
            "operation_receipt": self.operation_receipt.as_public_dict(),
            "execution_receipt": (
                None
                if self.execution_receipt is None
                else self.execution_receipt.as_public_dict()
            ),
            "reconciliation_receipt": (
                None
                if self.reconciliation_receipt is None
                else self.reconciliation_receipt.as_public_dict()
            ),
            "provider_observations": [
                item.as_public_dict() for item in self.provider_observations
            ],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
        }
        if self.failure_code is not None:
            value["failure"] = {"code": self.failure_code}
        return value


class WritingDeliveryAuthority(Protocol):
    def bind_binding_verifier(self, verifier: WritingDeliveryBindingVerifier) -> None: ...

    def provider_capabilities(self) -> tuple[dict[str, object], ...]: ...

    def capabilities(self) -> tuple[dict[str, object], ...]: ...

    def verify_target_current(
        self,
        provider_ref: str,
        action: str,
        target: object,
        *,
        target_binding: object | None = None,
    ) -> dict[str, object]: ...

    def admit(
        self,
        payload: dict[str, object],
        *,
        intent_id: str,
        draft_revision: int,
        draft_hash: str,
        preview_ref: str,
        preview_hash: str,
        confirmation: AcceptanceReceipt,
        idempotency_key: str,
    ) -> WritingDeliveryOperation: ...

    def claim(
        self, operation_ref: str, *, provider_request_hash: str
    ) -> WritingDeliveryOperation: ...

    def execute_once(
        self, operation_ref: str, *, artifact: bytes | None
    ) -> WritingDeliveryOperation: ...

    def record_preflight_failure(
        self, operation_ref: str, *, reason_code: str
    ) -> WritingDeliveryOperation: ...

    def reconcile(self, operation_ref: str, *, artifact: bytes | None) -> WritingDeliveryOperation: ...

    def query_operation(self, operation_ref: str) -> WritingDeliveryOperation | None: ...

    def query_operations(
        self, run_ref: str | None = None
    ) -> tuple[WritingDeliveryOperation, ...]: ...

    def next_runnable_operation_ref(
        self,
        *,
        retry_cutoff: float,
        excluded_operation_refs: frozenset[str] = frozenset(),
    ) -> str | None: ...


class SQLiteWritingDeliveryRuntime:
    def __init__(
        self,
        database: Database,
        feed: DurableFeed,
        human_response_verifier: HumanResponseVerifier | None,
        provider_registry: WritingDeliveryProviderRegistry,
        *,
        production_mode: bool = True,
    ) -> None:
        self._database = database
        self._feed = feed
        self._human_response_verifier = human_response_verifier
        self._provider_registry = provider_registry
        self._production_mode = production_mode
        self._binding_verifier: WritingDeliveryBindingVerifier | None = None

    def bind_binding_verifier(self, verifier: WritingDeliveryBindingVerifier) -> None:
        if self._binding_verifier is not None and self._binding_verifier is not verifier:
            raise OwnerConflict("writing_delivery_binding_verifier_already_bound")
        self._binding_verifier = verifier

    def provider_capabilities(self) -> tuple[dict[str, object], ...]:
        return self._provider_registry.capabilities()

    def capabilities(self) -> tuple[dict[str, object], ...]:
        return self.provider_capabilities()

    def verify_target_current(
        self,
        provider_ref: str,
        action: str,
        target: object,
        *,
        target_binding: object | None = None,
    ) -> dict[str, object]:
        return self._provider_registry.verify_target_current(
            provider_ref,
            action,
            target,
            production=self._production_mode,
            target_binding=target_binding,
        )

    def admit(
        self,
        payload: dict[str, object],
        *,
        intent_id: str,
        draft_revision: int,
        draft_hash: str,
        preview_ref: str,
        preview_hash: str,
        confirmation: AcceptanceReceipt,
        idempotency_key: str,
    ) -> WritingDeliveryOperation:
        normalized = normalize_writing_delivery_payload(payload)
        _validate_token(intent_id, "writing_delivery_confirmation_invalid")
        _validate_token(preview_ref, "writing_delivery_confirmation_invalid")
        _validate_hash(draft_hash, "writing_delivery_confirmation_invalid")
        _validate_hash(preview_hash, "writing_delivery_confirmation_invalid")
        _validate_idempotency_key(idempotency_key)
        if type(draft_revision) is not int or draft_revision < 1:
            raise OwnerConflict("writing_delivery_confirmation_invalid")
        _validate_confirmation(confirmation)
        request_document = {
            "command": "admit_writing_delivery",
            "payload": normalized,
            "intent_id": intent_id,
            "draft_revision": draft_revision,
            "draft_hash": draft_hash,
            "preview_ref": preview_ref,
            "preview_hash": preview_hash,
            "confirmation": confirmation.as_public_dict(),
        }
        request_hash = canonical_hash(request_document)
        with self._database.read() as connection:
            replay = connection.execute(
                text(
                    "SELECT operation_ref, request_hash FROM "
                    "ar_writing_delivery_operations WHERE idempotency_key = "
                    ":idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
        if replay is not None:
            if replay.request_hash != request_hash:
                raise OwnerConflict("idempotency_conflict")
            result = self.query_operation(replay.operation_ref)
            if result is None:
                raise OwnerConflict("writing_delivery_operation_missing")
            return result
        provider = self._provider_registry.require(
            cast(str, normalized["provider_ref"]),
            production=self._production_mode,
        )
        if normalized["action"] not in provider.supported_actions:
            raise OwnerConflict("writing_delivery_action_unavailable")
        if self._human_response_verifier is None:
            raise OwnerConflict("writing_delivery_confirmation_verifier_unavailable")
        if self._binding_verifier is None:
            raise OwnerConflict("writing_delivery_binding_verifier_unavailable")
        confirmed_draft = self._human_response_verifier.verify_command_confirmation(
            intent_id=intent_id,
            command_kind="writing_external_delivery",
            draft_revision=draft_revision,
            draft_hash=draft_hash,
            preview_ref=preview_ref,
            preview_hash=preview_hash,
            receipt=confirmation,
        )
        expected_draft = {
            "command_kind": "writing_external_delivery",
            "payload": normalized,
        }
        if confirmed_draft != expected_draft:
            raise OwnerConflict("writing_delivery_confirmation_binding_mismatch")
        self.verify_target_current(
            cast(str, normalized["provider_ref"]),
            cast(str, normalized["action"]),
            normalized["target"],
            target_binding=normalized["target_binding"],
        )
        self._binding_verifier.verify_writing_delivery_binding(normalized)

        operation_ref = cast(str, normalized["operation_ref"])
        payload_hash = canonical_hash(normalized)
        operation_fact = {
            "schema_ref": "meta-research/writing-delivery-operation/v1",
            "operation_ref": operation_ref,
            "payload_hash": payload_hash,
            "confirmation": confirmation.as_public_dict(),
            "status": "admitted",
        }
        operation_receipt = _receipt_for_fact(
            operation_ref,
            "operation",
            "writing_delivery_operation_admitted",
            operation_ref,
            operation_fact,
        )
        now = time.time()
        try:
            with self._database.write() as connection:
                run = connection.execute(
                    text(
                        "SELECT document_type FROM ar_writing_runs WHERE run_ref = "
                        ":run_ref"
                    ),
                    {"run_ref": normalized["run_ref"]},
                ).first()
                if run is None or run.document_type != normalized["document_type"]:
                    raise OwnerConflict("writing_delivery_run_binding_invalid")
                existing = connection.execute(
                    text(
                        "SELECT operation_ref, request_hash FROM "
                        "ar_writing_delivery_operations WHERE operation_ref = "
                        ":operation_ref"
                    ),
                    {"operation_ref": operation_ref},
                ).first()
                if existing is not None:
                    if existing.request_hash != request_hash:
                        raise OwnerConflict("writing_delivery_operation_conflict")
                else:
                    connection.execute(
                        text(
                            "INSERT INTO ar_writing_delivery_operations "
                            "(operation_ref, request_nonce, run_ref, document_type, "
                            "action, provider_ref, provider_operation_ref, payload_json, "
                            "payload_hash, target_json, target_hash, asset_ref, "
                            "version_ref, content_hash, citation_decision_ref, "
                            "renderer_version_ref, renderer_artifact_sha256, intent_id, "
                            "draft_revision, draft_hash, preview_ref, preview_hash, "
                            "confirmation_json, confirmation_hash, idempotency_key, "
                            "request_hash, status, attempt_count, operation_receipt_ref, "
                            "created_at, updated_at) VALUES (:operation_ref, "
                            ":request_nonce, :run_ref, :document_type, :action, "
                            ":provider_ref, :provider_operation_ref, :payload_json, "
                            ":payload_hash, :target_json, :target_hash, :asset_ref, "
                            ":version_ref, :content_hash, :citation_decision_ref, "
                            ":renderer_version_ref, :renderer_artifact_sha256, "
                            ":intent_id, :draft_revision, :draft_hash, :preview_ref, "
                            ":preview_hash, :confirmation_json, :confirmation_hash, "
                            ":idempotency_key, :request_hash, 'admitted', 0, "
                            ":operation_receipt_ref, :now, :now)"
                        ),
                        {
                            "operation_ref": operation_ref,
                            "request_nonce": normalized["request_nonce"],
                            "run_ref": normalized["run_ref"],
                            "document_type": normalized["document_type"],
                            "action": normalized["action"],
                            "provider_ref": normalized["provider_ref"],
                            "provider_operation_ref": operation_ref,
                            "payload_json": canonical_json(normalized),
                            "payload_hash": payload_hash,
                            "target_json": canonical_json(normalized["target"]),
                            "target_hash": canonical_hash(normalized["target"]),
                            "asset_ref": normalized["asset_ref"],
                            "version_ref": normalized["version_ref"],
                            "content_hash": normalized["content_hash"],
                            "citation_decision_ref": normalized[
                                "citation_decision_ref"
                            ],
                            "renderer_version_ref": normalized[
                                "renderer_version_ref"
                            ],
                            "renderer_artifact_sha256": normalized[
                                "renderer_artifact_sha256"
                            ],
                            "intent_id": intent_id,
                            "draft_revision": draft_revision,
                            "draft_hash": draft_hash,
                            "preview_ref": preview_ref,
                            "preview_hash": preview_hash,
                            "confirmation_json": canonical_json(
                                confirmation.as_public_dict()
                            ),
                            "confirmation_hash": canonical_hash(
                                confirmation.as_public_dict()
                            ),
                            "idempotency_key": idempotency_key,
                            "request_hash": request_hash,
                            "operation_receipt_ref": operation_receipt.receipt_ref,
                            "now": now,
                        },
                    )
                    _insert_receipt(connection, operation_ref, operation_receipt, operation_fact)
                    connection.execute(
                        text(
                            "UPDATE agent_runtime_state SET revision = revision + 1, "
                            "writing_delivery_operation_count = "
                            "writing_delivery_operation_count + 1 WHERE singleton = "
                            "'owner'"
                        )
                    )
                    self._feed.record(
                        connection,
                        "agent_runtime.writing_delivery_admitted",
                        {
                            "operation_ref": operation_ref,
                            "run_ref": normalized["run_ref"],
                            "action": normalized["action"],
                            "provider_ref": normalized["provider_ref"],
                            "status": "admitted",
                        },
                    )
        except IntegrityError as error:
            raise OwnerConflict("writing_delivery_operation_conflict") from error
        result = self.query_operation(operation_ref)
        if result is None:
            raise OwnerConflict("writing_delivery_operation_missing")
        return result

    def claim(
        self, operation_ref: str, *, provider_request_hash: str
    ) -> WritingDeliveryOperation:
        _validate_operation_ref(operation_ref)
        _validate_hash(provider_request_hash, "writing_delivery_provider_request_invalid")
        with self._database.write() as connection:
            row = _operation_row(connection, operation_ref)
            if row.status == "completed":
                pass
            elif row.status != "admitted":
                raise OwnerConflict("writing_delivery_reconciliation_required")
            else:
                if (
                    row.provider_request_hash is not None
                    and row.provider_request_hash != provider_request_hash
                ):
                    raise OwnerConflict("writing_delivery_provider_request_drift")
                attempt_count = int(row.attempt_count) + 1
                subject_ref = f"writing_delivery_execution:{canonical_hash({'operation_ref': operation_ref, 'attempt': attempt_count})[:48]}"
                fact = {
                    "schema_ref": "meta-research/writing-delivery-execution/v1",
                    "operation_ref": operation_ref,
                    "provider_operation_ref": row.provider_operation_ref,
                    "provider_request_hash": provider_request_hash,
                    "attempt": attempt_count,
                    "status": "executing",
                }
                receipt = _receipt_for_fact(
                    operation_ref,
                    "execution",
                    "writing_delivery_execution_started",
                    subject_ref,
                    fact,
                )
                now = time.time()
                connection.execute(
                    text(
                        "UPDATE ar_writing_delivery_operations SET status = "
                        "'executing', attempt_count = :attempt_count, "
                        "provider_request_hash = :provider_request_hash, "
                        "execution_receipt_ref = :receipt_ref, failure_code = NULL, "
                        "updated_at = :now WHERE operation_ref = :operation_ref"
                    ),
                    {
                        "attempt_count": attempt_count,
                        "provider_request_hash": provider_request_hash,
                        "receipt_ref": receipt.receipt_ref,
                        "now": now,
                        "operation_ref": operation_ref,
                    },
                )
                _insert_receipt(connection, operation_ref, receipt, fact)
                connection.execute(
                    text(
                        "UPDATE agent_runtime_state SET revision = revision + 1 "
                        "WHERE singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "agent_runtime.writing_delivery_execution_started",
                    {
                        "operation_ref": operation_ref,
                        "attempt_count": attempt_count,
                        "provider_operation_ref": row.provider_operation_ref,
                    },
                )
        result = self.query_operation(operation_ref)
        if result is None:
            raise OwnerConflict("writing_delivery_operation_missing")
        return result

    def execute_once(
        self, operation_ref: str, *, artifact: bytes | None
    ) -> WritingDeliveryOperation:
        operation = self.query_operation(operation_ref)
        if operation is None:
            raise OwnerConflict("writing_delivery_operation_missing")
        if operation.status == "completed":
            return operation
        payload = operation.payload
        provider = self._provider_registry.require(
            cast(str, payload["provider_ref"]), production=self._production_mode
        )
        try:
            request = provider.request(
                operation_ref=operation_ref,
                action=cast(str, payload["action"]),
                target=cast(dict[str, object], payload["target"]),
                target_binding=cast(
                    dict[str, object], payload["target_binding"]
                ),
                artifact=artifact,
                artifact_sha256=cast(
                    str, payload["renderer_artifact_sha256"]
                ),
            )
        except OwnerConflict:
            return self._record_provider_request_failure(
                operation,
                reason_code="provider_request_rejected",
            )
        except Exception:
            return self._record_provider_request_failure(
                operation,
                reason_code="provider_request_failed",
            )
        if operation.provider_request_hash not in {None, request.request_hash}:
            raise OwnerConflict("writing_delivery_provider_request_drift")
        if operation.status in {"executing", "partial", "outcome_unknown"}:
            operation = self._reconcile_request(operation, request, provider)
            if operation.status != "admitted":
                return operation
        if payload["action"] != "delete" and artifact is None:
            # Reconciliation may prove the original effect absent even while
            # RM custody is unavailable. Keep it admitted; execution resumes
            # only after the exact artifact bytes can be materialized again.
            return operation
        operation = self.claim(operation_ref, provider_request_hash=request.request_hash)
        try:
            observation = provider.execute(request)
        except WritingDeliveryOutcomeUnknown as error:
            return self.mark_outcome_unknown(
                operation_ref, reason_code=_exception_code(error)
            )
        except (ConnectionError, TimeoutError, OSError) as error:
            return self.mark_outcome_unknown(
                operation_ref, reason_code=_exception_code(error)
            )
        except OwnerConflict as error:
            return self._record_execution_without_observation(
                operation_ref,
                status="partial",
                reason_code=(
                    error.code
                    if error.code == "writing_delivery_target_stale"
                    else "provider_rejected"
                ),
            )
        except Exception as error:
            return self.mark_outcome_unknown(
                operation_ref, reason_code=_exception_code(error)
            )
        return self.record_provider_observation(
            operation_ref, observation, reconciliation=False
        )

    def reconcile(
        self, operation_ref: str, *, artifact: bytes | None
    ) -> WritingDeliveryOperation:
        operation = self.query_operation(operation_ref)
        if operation is None:
            raise OwnerConflict("writing_delivery_operation_missing")
        if operation.status == "completed":
            return operation
        payload = operation.payload
        provider = self._provider_registry.require(
            cast(str, payload["provider_ref"]), production=self._production_mode
        )
        try:
            request = provider.request(
                operation_ref=operation_ref,
                action=cast(str, payload["action"]),
                target=cast(dict[str, object], payload["target"]),
                target_binding=cast(
                    dict[str, object], payload["target_binding"]
                ),
                artifact=artifact,
                artifact_sha256=cast(
                    str, payload["renderer_artifact_sha256"]
                ),
            )
        except OwnerConflict:
            return self._record_provider_request_failure(
                operation,
                reason_code="provider_request_rejected",
            )
        except Exception:
            return self._record_provider_request_failure(
                operation,
                reason_code="provider_request_failed",
            )
        return self._reconcile_request(operation, request, provider)

    def _reconcile_request(self, operation, request, provider) -> WritingDeliveryOperation:
        try:
            observation = provider.reconcile(request)
        except (WritingDeliveryOutcomeUnknown, ConnectionError, TimeoutError, OSError) as error:
            return self._record_reconciliation_without_observation(
                operation.operation_ref,
                status="outcome_unknown",
                reason_code=_exception_code(error),
            )
        except OwnerConflict as error:
            return self._record_reconciliation_without_observation(
                operation.operation_ref,
                status="partial",
                reason_code=(
                    error.code
                    if error.code == "writing_delivery_target_stale"
                    else "provider_reconciliation_rejected"
                ),
            )
        except Exception as error:
            return self._record_reconciliation_without_observation(
                operation.operation_ref,
                status="outcome_unknown",
                reason_code=_exception_code(error),
            )
        return self.record_provider_observation(
            operation.operation_ref, observation, reconciliation=True
        )

    def record_provider_observation(
        self,
        operation_ref: str,
        observation: WritingDeliveryProviderObservation,
        *,
        reconciliation: bool,
    ) -> WritingDeliveryOperation:
        _validate_operation_ref(operation_ref)
        if type(observation) is not WritingDeliveryProviderObservation:
            raise OwnerConflict("writing_delivery_provider_observation_invalid")
        if (
            not isinstance(observation.observation_ref, str)
            or _PROVIDER_OBSERVATION_REF.fullmatch(
                observation.observation_ref
            )
            is None
            or not isinstance(observation.provider_ref, str)
            or not observation.provider_ref
            or len(observation.provider_ref) > 128
            or not isinstance(observation.provider_operation_ref, str)
            or not observation.provider_operation_ref
            or len(observation.provider_operation_ref) > 96
            or not math.isfinite(observation.observed_at)
            or not isinstance(observation.details, dict)
            or contains_secret(observation.as_dict())
        ):
            raise OwnerConflict("writing_delivery_provider_observation_invalid")
        if observation.outcome not in {
            "completed",
            "not_found",
            "partial",
            "outcome_unknown",
        }:
            raise OwnerConflict("writing_delivery_provider_observation_invalid")
        observation_document = observation.as_dict()
        observation_hash = canonical_hash(observation_document)
        semantic_hash = canonical_hash(
            {
                "provider_ref": observation.provider_ref,
                "provider_operation_ref": observation.provider_operation_ref,
                "outcome": observation.outcome,
                "details": observation.details,
            }
        )
        with self._database.write() as connection:
            row = _operation_row(connection, operation_ref)
            if (
                observation.provider_ref != row.provider_ref
                or observation.provider_operation_ref != row.provider_operation_ref
            ):
                raise OwnerConflict("writing_delivery_provider_observation_invalid")
            existing = connection.execute(
                text(
                    "SELECT operation_ref, observation_hash FROM "
                    "ar_writing_delivery_observations "
                    "WHERE observation_ref = :observation_ref"
                ),
                {"observation_ref": observation.observation_ref},
            ).first()
            if existing is not None and (
                existing.operation_ref != operation_ref
                or existing.observation_hash != observation_hash
            ):
                raise OwnerConflict("writing_delivery_provider_observation_conflict")
            semantic = connection.execute(
                text(
                    "SELECT * FROM ar_writing_delivery_observations WHERE "
                    "operation_ref = :operation_ref AND semantic_hash = :semantic_hash"
                ),
                {"operation_ref": operation_ref, "semantic_hash": semantic_hash},
            ).first()
            if semantic is None:
                connection.execute(
                    text(
                        "INSERT INTO ar_writing_delivery_observations "
                        "(observation_ref, operation_ref, provider_ref, "
                        "provider_operation_ref, outcome, observation_json, "
                        "observation_hash, semantic_hash, observed_at, recorded_at) VALUES "
                        "(:observation_ref, :operation_ref, :provider_ref, "
                        ":provider_operation_ref, :outcome, :observation_json, "
                        ":observation_hash, :semantic_hash, :observed_at, :recorded_at)"
                    ),
                    {
                        "observation_ref": observation.observation_ref,
                        "operation_ref": operation_ref,
                        "provider_ref": observation.provider_ref,
                        "provider_operation_ref": observation.provider_operation_ref,
                        "outcome": observation.outcome,
                        "observation_json": canonical_json(observation_document),
                        "observation_hash": observation_hash,
                        "semantic_hash": semantic_hash,
                        "observed_at": observation.observed_at,
                        "recorded_at": time.time(),
                    },
                )
                provider_observation_ref = observation.observation_ref
                accepted_observation_hash = observation_hash
            else:
                accepted = _observation_from_row(semantic, operation_ref)
                provider_observation_ref = accepted.observation_ref
                accepted_observation_hash = accepted.observation_hash
            status = {
                "completed": "completed",
                "not_found": "admitted",
                "partial": "partial",
                "outcome_unknown": "outcome_unknown",
            }[observation.outcome]
            reason_code = None if status in {"completed", "admitted"} else f"provider_{observation.outcome}"
            receipt_role = "reconciliation" if reconciliation else "execution"
            receipt_kind = (
                f"writing_delivery_{receipt_role}_{observation.outcome}"
            )
            subject_ref = f"writing_delivery_{receipt_role}:{canonical_hash({'operation_ref': operation_ref, 'observation_hash': accepted_observation_hash})[:48]}"
            fact = {
                "schema_ref": f"meta-research/writing-delivery-{receipt_role}/v1",
                "operation_ref": operation_ref,
                "provider_operation_ref": row.provider_operation_ref,
                "provider_observation_ref": provider_observation_ref,
                "provider_observation_hash": accepted_observation_hash,
                "provider_outcome": observation.outcome,
                "status": status,
            }
            receipt = _receipt_for_fact(
                operation_ref, receipt_role, receipt_kind, subject_ref, fact
            )
            now = time.time()
            updates = {
                "operation_ref": operation_ref,
                "status": status,
                "failure_code": reason_code,
                "now": now,
                "completed_at": now if status == "completed" else None,
            }
            receipt_column = (
                "reconciliation_receipt_ref"
                if reconciliation
                else "execution_receipt_ref"
            )
            semantic_replay = (
                semantic is not None
                and row.status == status
                and row.failure_code == reason_code
                and getattr(row, receipt_column) == receipt.receipt_ref
            )
            if semantic_replay:
                if status in {"partial", "outcome_unknown"}:
                    connection.execute(
                        text(
                            "UPDATE ar_writing_delivery_operations SET updated_at = "
                            ":now WHERE operation_ref = :operation_ref"
                        ),
                        {"now": now, "operation_ref": operation_ref},
                    )
            else:
                connection.execute(
                    text(
                        f"UPDATE ar_writing_delivery_operations SET status = :status, "
                        f"{receipt_column} = :receipt_ref, failure_code = :failure_code, "
                        "updated_at = :now, completed_at = :completed_at WHERE "
                        "operation_ref = :operation_ref"
                    ),
                    {**updates, "receipt_ref": receipt.receipt_ref},
                )
                _insert_receipt(connection, operation_ref, receipt, fact)
                if reconciliation and status == "completed":
                    execution_fact = {
                        "schema_ref": "meta-research/writing-delivery-execution/v1",
                        "operation_ref": operation_ref,
                        "provider_operation_ref": row.provider_operation_ref,
                        "reconciliation_receipt": receipt.as_public_dict(),
                        "status": "completed",
                    }
                    execution_receipt = _receipt_for_fact(
                        operation_ref,
                        "execution",
                        "writing_delivery_execution_completed",
                        f"writing_delivery_execution:{canonical_hash(execution_fact)[:48]}",
                        execution_fact,
                    )
                    connection.execute(
                        text(
                            "UPDATE ar_writing_delivery_operations SET "
                            "execution_receipt_ref = :receipt_ref WHERE operation_ref = "
                            ":operation_ref"
                        ),
                        {
                            "receipt_ref": execution_receipt.receipt_ref,
                            "operation_ref": operation_ref,
                        },
                    )
                    _insert_receipt(
                        connection, operation_ref, execution_receipt, execution_fact
                    )
                completed_increment = (
                    1 if status == "completed" and row.status != "completed" else 0
                )
                reconciliation_increment = 1 if reconciliation else 0
                connection.execute(
                    text(
                        "UPDATE agent_runtime_state SET revision = revision + 1, "
                        "writing_delivery_completed_count = "
                        "writing_delivery_completed_count + :completed, "
                        "writing_delivery_reconciliation_count = "
                        "writing_delivery_reconciliation_count + :reconciled WHERE "
                        "singleton = 'owner'"
                    ),
                    {
                        "completed": completed_increment,
                        "reconciled": reconciliation_increment,
                    },
                )
                self._feed.record(
                    connection,
                    "agent_runtime.writing_delivery_provider_observed",
                    {
                        "operation_ref": operation_ref,
                        "provider_observation_ref": provider_observation_ref,
                        "provider_outcome": observation.outcome,
                        "status": status,
                        "reconciliation": reconciliation,
                    },
                )
        result = self.query_operation(operation_ref)
        if result is None:
            raise OwnerConflict("writing_delivery_operation_missing")
        return result

    def mark_outcome_unknown(
        self, operation_ref: str, *, reason_code: str
    ) -> WritingDeliveryOperation:
        return self._record_execution_without_observation(
            operation_ref, status="outcome_unknown", reason_code=reason_code
        )

    def _record_provider_request_failure(
        self,
        operation: WritingDeliveryOperation,
        *,
        reason_code: str,
    ) -> WritingDeliveryOperation:
        if operation.status in {"executing", "outcome_unknown"}:
            return self._record_reconciliation_without_observation(
                operation.operation_ref,
                status="outcome_unknown",
                reason_code=reason_code,
            )
        if operation.status == "partial":
            return self._record_reconciliation_without_observation(
                operation.operation_ref,
                status="partial",
                reason_code=reason_code,
            )
        return self._record_execution_without_observation(
            operation.operation_ref,
            status="partial",
            reason_code=reason_code,
        )

    def record_preflight_failure(
        self, operation_ref: str, *, reason_code: str
    ) -> WritingDeliveryOperation:
        return self._record_execution_without_observation(
            operation_ref,
            status="partial",
            reason_code=reason_code,
        )

    def _record_execution_without_observation(
        self, operation_ref: str, *, status: str, reason_code: str
    ) -> WritingDeliveryOperation:
        return self._record_receipt_only_transition(
            operation_ref,
            role="execution",
            status=status,
            reason_code=reason_code,
        )

    def _record_reconciliation_without_observation(
        self, operation_ref: str, *, status: str, reason_code: str
    ) -> WritingDeliveryOperation:
        return self._record_receipt_only_transition(
            operation_ref,
            role="reconciliation",
            status=status,
            reason_code=reason_code,
        )

    def _record_receipt_only_transition(
        self,
        operation_ref: str,
        *,
        role: str,
        status: str,
        reason_code: str,
    ) -> WritingDeliveryOperation:
        _validate_operation_ref(operation_ref)
        if role not in {"execution", "reconciliation"} or status not in {
            "partial",
            "outcome_unknown",
        }:
            raise OwnerConflict("writing_delivery_transition_invalid")
        _validate_reason_code(reason_code)
        with self._database.write() as connection:
            row = _operation_row(connection, operation_ref)
            if row.status == "completed":
                pass
            else:
                fact = {
                    "schema_ref": f"meta-research/writing-delivery-{role}/v1",
                    "operation_ref": operation_ref,
                    "provider_operation_ref": row.provider_operation_ref,
                    "status": status,
                    "reason_code": reason_code,
                }
                receipt = _receipt_for_fact(
                    operation_ref,
                    role,
                    f"writing_delivery_{role}_{status}",
                    f"writing_delivery_{role}:{canonical_hash(fact)[:48]}",
                    fact,
                )
                column = f"{role}_receipt_ref"
                if not (
                    row.status == status
                    and row.failure_code == reason_code
                    and getattr(row, column) == receipt.receipt_ref
                ):
                    now = time.time()
                    connection.execute(
                        text(
                            f"UPDATE ar_writing_delivery_operations SET status = "
                            f":status, {column} = :receipt_ref, failure_code = "
                            ":reason_code, updated_at = :now, completed_at = NULL "
                            "WHERE operation_ref = :operation_ref"
                        ),
                        {
                            "status": status,
                            "receipt_ref": receipt.receipt_ref,
                            "reason_code": reason_code,
                            "now": now,
                            "operation_ref": operation_ref,
                        },
                    )
                    _insert_receipt(connection, operation_ref, receipt, fact)
                    connection.execute(
                        text(
                            "UPDATE agent_runtime_state SET revision = revision + 1, "
                            "writing_delivery_reconciliation_count = "
                            "writing_delivery_reconciliation_count + :reconciled WHERE "
                            "singleton = 'owner'"
                        ),
                        {"reconciled": 1 if role == "reconciliation" else 0},
                    )
                    self._feed.record(
                        connection,
                        f"agent_runtime.writing_delivery_{role}_{status}",
                        {
                            "operation_ref": operation_ref,
                            "status": status,
                            "reason_code": reason_code,
                        },
                    )
        result = self.query_operation(operation_ref)
        if result is None:
            raise OwnerConflict("writing_delivery_operation_missing")
        return result

    def query_operation(self, operation_ref: str) -> WritingDeliveryOperation | None:
        _validate_operation_ref(operation_ref)
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ar_writing_delivery_operations WHERE "
                    "operation_ref = :operation_ref"
                ),
                {"operation_ref": operation_ref},
            ).first()
            if row is None:
                return None
            observations = connection.execute(
                text(
                    "SELECT * FROM ar_writing_delivery_observations WHERE "
                    "operation_ref = :operation_ref ORDER BY recorded_at, "
                    "observation_ref"
                ),
                {"operation_ref": operation_ref},
            ).all()
            operation_receipt = _query_receipt(connection, row.operation_receipt_ref)
            execution_receipt = _query_optional_receipt(
                connection, row.execution_receipt_ref
            )
            reconciliation_receipt = _query_optional_receipt(
                connection, row.reconciliation_receipt_ref
            )
        try:
            payload = normalize_writing_delivery_payload(
                decoded_object(row.payload_json)
            )
        except (TypeError, ValueError, OwnerConflict) as error:
            raise OwnerConflict("writing_delivery_operation_integrity_invalid") from error
        if (
            canonical_json(payload) != row.payload_json
            or canonical_hash(payload) != row.payload_hash
            or canonical_json(payload["target"]) != row.target_json
            or canonical_hash(payload["target"]) != row.target_hash
            or payload["operation_ref"] != row.operation_ref
            or payload["provider_ref"] != row.provider_ref
            or payload["run_ref"] != row.run_ref
            or payload["document_type"] != row.document_type
            or payload["action"] != row.action
            or payload["request_nonce"] != row.request_nonce
            or row.provider_operation_ref != row.operation_ref
        ):
            raise OwnerConflict("writing_delivery_operation_integrity_invalid")
        projected_observations = tuple(
            _observation_from_row(item, operation_ref) for item in observations
        )
        return WritingDeliveryOperation(
            operation_ref=row.operation_ref,
            payload=payload,
            payload_hash=row.payload_hash,
            status=row.status,
            attempt_count=int(row.attempt_count),
            provider_operation_ref=row.provider_operation_ref,
            provider_request_hash=row.provider_request_hash,
            operation_receipt=operation_receipt,
            execution_receipt=execution_receipt,
            reconciliation_receipt=reconciliation_receipt,
            provider_observations=projected_observations,
            failure_code=row.failure_code,
            created_at=float(row.created_at),
            updated_at=float(row.updated_at),
            completed_at=(
                None if row.completed_at is None else float(row.completed_at)
            ),
        )

    def query_operations(
        self, run_ref: str | None = None
    ) -> tuple[WritingDeliveryOperation, ...]:
        if run_ref is not None:
            _validate_token(run_ref, "writing_delivery_run_ref_invalid")
        with self._database.read() as connection:
            if run_ref is None:
                rows = connection.execute(
                    text(
                        "SELECT operation_ref FROM ar_writing_delivery_operations "
                        "ORDER BY created_at, operation_ref"
                    )
                ).all()
            else:
                rows = connection.execute(
                    text(
                        "SELECT operation_ref FROM ar_writing_delivery_operations "
                        "WHERE run_ref = :run_ref ORDER BY created_at, operation_ref"
                    ),
                    {"run_ref": run_ref},
                ).all()
        results = tuple(self.query_operation(row.operation_ref) for row in rows)
        if any(item is None for item in results):
            raise OwnerConflict("writing_delivery_operation_missing")
        return cast(tuple[WritingDeliveryOperation, ...], results)

    def next_runnable_operation_ref(
        self,
        *,
        retry_cutoff: float,
        excluded_operation_refs: frozenset[str] = frozenset(),
    ) -> str | None:
        """Select one scheduler candidate without hydrating operation history."""

        if (
            isinstance(retry_cutoff, bool)
            or not isinstance(retry_cutoff, (int, float))
            or not math.isfinite(retry_cutoff)
        ):
            raise OwnerConflict("writing_delivery_retry_cutoff_invalid")
        for operation_ref in excluded_operation_refs:
            _validate_operation_ref(operation_ref)
        excluded = tuple(sorted(excluded_operation_refs))

        parameters: dict[str, object] = {"retry_cutoff": float(retry_cutoff)}
        exclusion_clause = ""
        if excluded:
            placeholders: list[str] = []
            for index, operation_ref in enumerate(excluded):
                parameter = f"excluded_operation_ref_{index}"
                placeholders.append(f":{parameter}")
                parameters[parameter] = operation_ref
            exclusion_clause = (
                " AND operation_ref NOT IN (" + ", ".join(placeholders) + ")"
            )

        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT operation_ref FROM ar_writing_delivery_operations WHERE "
                    "(status IN ('admitted', 'executing') OR "
                    "(status IN ('partial', 'outcome_unknown') AND "
                    "updated_at <= :retry_cutoff))"
                    + exclusion_clause
                    + " ORDER BY created_at, operation_ref LIMIT 1"
                ),
                parameters,
            ).first()
        if row is None:
            return None
        _validate_operation_ref(row.operation_ref)
        return cast(str, row.operation_ref)


def _validate_operation_ref(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value.startswith("writing_delivery:")
        or len(value) != 65
        or any(
            character not in "0123456789abcdef"
            for character in value[len("writing_delivery:") :]
        )
    ):
        raise OwnerConflict("writing_delivery_operation_ref_invalid")


def _validate_token(value: object, code: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or "\n" in value
        or "\r" in value
    ):
        raise OwnerConflict(code)


def _validate_hash(value: object, code: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise OwnerConflict(code)


def _validate_idempotency_key(value: str) -> None:
    _validate_token(value, "idempotency_key_invalid")


def _validate_reason_code(value: str) -> None:
    _validate_token(value, "writing_delivery_reason_invalid")
    if contains_secret(value):
        raise OwnerConflict("writing_delivery_reason_invalid")


def _validate_confirmation(receipt: AcceptanceReceipt) -> None:
    if type(receipt) is not AcceptanceReceipt:
        raise OwnerConflict("writing_delivery_confirmation_invalid")
    for value in (
        receipt.issuer,
        receipt.kind,
        receipt.receipt_ref,
        receipt.subject_ref,
    ):
        _validate_token(value, "writing_delivery_confirmation_invalid")
    _validate_hash(receipt.payload_hash, "writing_delivery_confirmation_invalid")
    if receipt.issuer != "human_collaboration":
        raise OwnerConflict("writing_delivery_confirmation_invalid")


def _operation_row(connection, operation_ref: str):
    row = connection.execute(
        text(
            "SELECT * FROM ar_writing_delivery_operations WHERE operation_ref = "
            ":operation_ref"
        ),
        {"operation_ref": operation_ref},
    ).first()
    if row is None:
        raise OwnerConflict("writing_delivery_operation_missing")
    return row


def _receipt_for_fact(
    operation_ref: str,
    role: str,
    kind: str,
    subject_ref: str,
    fact: dict[str, object],
) -> AcceptanceReceipt:
    fact_hash = canonical_hash(fact)
    receipt_hash = canonical_hash(
        {
            "schema_ref": _RECEIPT_SCHEMA,
            "issuer": AR_OWNER,
            "kind": kind,
            "subject_ref": subject_ref,
            "payload_hash": fact_hash,
        }
    )
    receipt_ref = f"writing_delivery_receipt:{canonical_hash({'operation_ref': operation_ref, 'role': role, 'kind': kind, 'fact_hash': fact_hash})[:48]}"
    return AcceptanceReceipt(
        issuer=AR_OWNER,
        kind=kind,
        receipt_ref=receipt_ref,
        subject_ref=subject_ref,
        payload_hash=receipt_hash,
    )


def _insert_receipt(connection, operation_ref, receipt, fact) -> None:
    role = (
        "operation"
        if "operation_admitted" in receipt.kind
        else "reconciliation"
        if "reconciliation" in receipt.kind
        else "execution"
    )
    fact_json = canonical_json(fact)
    fact_hash = canonical_hash(fact)
    existing = connection.execute(
        text(
            "SELECT * FROM ar_writing_delivery_receipts WHERE receipt_ref = "
            ":receipt_ref"
        ),
        {"receipt_ref": receipt.receipt_ref},
    ).first()
    if existing is not None:
        if (
            existing.operation_ref != operation_ref
            or existing.receipt_role != role
            or existing.receipt_kind != receipt.kind
            or existing.subject_ref != receipt.subject_ref
            or existing.fact_json != fact_json
            or existing.fact_hash != fact_hash
            or existing.receipt_hash != receipt.payload_hash
        ):
            raise OwnerConflict("writing_delivery_receipt_conflict")
        return
    connection.execute(
        text(
            "INSERT INTO ar_writing_delivery_receipts (receipt_ref, operation_ref, "
            "receipt_role, receipt_kind, subject_ref, fact_json, fact_hash, "
            "receipt_hash, recorded_at) VALUES (:receipt_ref, :operation_ref, "
            ":receipt_role, :receipt_kind, :subject_ref, :fact_json, :fact_hash, "
            ":receipt_hash, :recorded_at)"
        ),
        {
            "receipt_ref": receipt.receipt_ref,
            "operation_ref": operation_ref,
            "receipt_role": role,
            "receipt_kind": receipt.kind,
            "subject_ref": receipt.subject_ref,
            "fact_json": fact_json,
            "fact_hash": fact_hash,
            "receipt_hash": receipt.payload_hash,
            "recorded_at": time.time(),
        },
    )


def _query_receipt(connection, receipt_ref: str) -> AcceptanceReceipt:
    row = connection.execute(
        text(
            "SELECT * FROM ar_writing_delivery_receipts WHERE receipt_ref = "
            ":receipt_ref"
        ),
        {"receipt_ref": receipt_ref},
    ).first()
    if row is None:
        raise OwnerConflict("writing_delivery_receipt_missing")
    try:
        fact = decoded_object(row.fact_json)
    except (TypeError, ValueError) as error:
        raise OwnerConflict("writing_delivery_receipt_invalid") from error
    expected = _receipt_for_fact(
        row.operation_ref,
        row.receipt_role,
        row.receipt_kind,
        row.subject_ref,
        cast(dict[str, object], fact),
    )
    if (
        not isinstance(fact, dict)
        or canonical_json(fact) != row.fact_json
        or canonical_hash(fact) != row.fact_hash
        or expected.receipt_ref != row.receipt_ref
        or expected.payload_hash != row.receipt_hash
    ):
        raise OwnerConflict("writing_delivery_receipt_invalid")
    return expected


def _query_optional_receipt(connection, receipt_ref) -> AcceptanceReceipt | None:
    return None if receipt_ref is None else _query_receipt(connection, receipt_ref)


def _observation_from_row(row, operation_ref: str) -> WritingDeliveryObservation:
    try:
        value = decoded_object(row.observation_json)
    except (TypeError, ValueError) as error:
        raise OwnerConflict("writing_delivery_provider_observation_invalid") from error
    details = value.get("details") if isinstance(value, dict) else None
    semantic_hash = canonical_hash(
        {
            "provider_ref": row.provider_ref,
            "provider_operation_ref": row.provider_operation_ref,
            "outcome": row.outcome,
            "details": details,
        }
    )
    if (
        not isinstance(value, dict)
        or canonical_json(value) != row.observation_json
        or canonical_hash(value) != row.observation_hash
        or not isinstance(value.get("observation_ref"), str)
        or _PROVIDER_OBSERVATION_REF.fullmatch(value["observation_ref"])
        is None
        or value.get("observation_ref") != row.observation_ref
        or value.get("provider_ref") != row.provider_ref
        or value.get("provider_operation_ref") != row.provider_operation_ref
        or value.get("outcome") != row.outcome
        or value.get("observed_at") != row.observed_at
        or not isinstance(details, dict)
        or contains_secret(value)
        or semantic_hash != row.semantic_hash
        or row.operation_ref != operation_ref
    ):
        raise OwnerConflict("writing_delivery_provider_observation_invalid")
    return WritingDeliveryObservation(
        observation_ref=row.observation_ref,
        provider_ref=row.provider_ref,
        provider_operation_ref=row.provider_operation_ref,
        outcome=row.outcome,
        observed_at=float(row.observed_at),
        details=cast(dict[str, object], details),
        observation_hash=row.observation_hash,
    )


def _exception_code(error: BaseException) -> str:
    # Provider exception text is untrusted and may contain credentials, URLs,
    # recipient data, or other values that must never enter receipts/feed/Web.
    if isinstance(error, WritingDeliveryOutcomeUnknown):
        return "provider_outcome_unknown"
    if isinstance(error, TimeoutError):
        return "provider_timeout"
    if isinstance(error, ConnectionError):
        return "provider_connection_failed"
    if isinstance(error, OSError):
        return "provider_io_failed"
    return "provider_outcome_unknown"
