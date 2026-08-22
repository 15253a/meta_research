from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass, replace
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from meta_research.database import Database
from meta_research.deepfetch import (
    DeepFetchProvider,
    DeepFetchProviderRequest,
    DeepFetchRunRequest,
    DeepFetchRuntimeBinding,
    DeepFetchUnavailable,
    validate_deepfetch_result,
    validate_runtime_binding,
)
from meta_research.feed import DurableFeed
from meta_research.idea_contract import (
    IDEA_REVIEW_SCHEMA_REF,
    IDEA_REVIEW_SCHEMA_V1_REF,
    material_outcome_hash,
)
from meta_research.owners._sqlite_snapshot import (
    OwnerSnapshotQuery,
    SQLiteOwnerSnapshot,
)
from meta_research.owners.common import (
    AcceptanceReceipt,
    DeepFetchRunRequestVerifier,
    IdeaOutcomeDecisionVerifier,
    OwnerConflict,
    OwnerSnapshot,
    StageRunRequestVerifier,
    canonical_hash,
    canonical_json,
    decoded_object,
    new_ref,
)
from meta_research.owners.advancement_engine import StageRunRequest
from meta_research.quest_drafting import (
    HostComputeDevice,
    HostComputeProbe,
    HostComputeSnapshot,
)


@dataclass(frozen=True)
class HostComputeObservation:
    snapshot_ref: str
    status: str
    observed_at: float
    devices: tuple[HostComputeDevice, ...]
    adapter_kind: str
    capabilities_hash: str
    reason_code: str | None = None

    @property
    def capabilities(self) -> dict[str, object]:
        return {"devices": [device.as_dict() for device in self.devices]}


AR_OWNER = "agent_runtime"
ATTEMPT_EXECUTION_SCHEMA = "meta-research/idea-attempt-execution/v2"
IDEA_RUNTIME_BINDING_SCHEMA = "meta-research/idea-runtime-binding/v1"
ATTEMPT_EXECUTION_RECEIPT_KIND = "idea_attempt_execution"
RUN_COMPLETION_RECEIPT_KIND = "run_execution_completed"
DEEPFETCH_EXECUTION_RECEIPT_KIND = "deepfetch_execution_completed"
RECEIPT_SCHEMA = "meta-research/owner-acceptance-receipt/v1"
_IDEA_SAFE_CAPABILITIES = {
    "approval-policy-never",
    "filesystem-danger-full-access",
    "global-config-ignored",
    "harness-child-agent-review",
    "mcp-config-empty",
    "native-session-resume",
    "shell-tool-enabled",
    "structured-output-json-schema",
    "trusted-local-quest-authorization",
    "web-search-live",
}
_IDEA_SAFE_RESOURCE_PREFIXES = (
    "adapter-source:",
    "codex-config:",
    "disabled-codex-config:",
    "disabled-codex-features:",
    "harness-artifact:",
    "output-route:",
    "output-schema:",
    "package:",
    "provider-output-limits:",
    "provider-timeout-seconds:",
    "runtime-policy:",
    "sandbox-policy:",
    "transport-seal-key:",
)


@dataclass(frozen=True)
class IdeaRuntimeBinding:
    packaged_skill_bundle_hash: str
    instruction_set_hash: str
    model_ref: str
    harness_adapter_ref: str
    mcp_bindings: tuple[str, ...]
    capability_bindings: tuple[str, ...]
    resource_bindings: tuple[str, ...]
    schema_ref: str = IDEA_RUNTIME_BINDING_SCHEMA

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_ref": self.schema_ref,
            "packaged_skill_bundle_hash": self.packaged_skill_bundle_hash,
            "instruction_set_hash": self.instruction_set_hash,
            "model_ref": self.model_ref,
            "harness_adapter_ref": self.harness_adapter_ref,
            "mcp_bindings": list(self.mcp_bindings),
            "capability_bindings": list(self.capability_bindings),
            "resource_bindings": list(self.resource_bindings),
        }


@dataclass(frozen=True)
class AttemptExecution:
    request_ref: str
    run_ref: str
    attempt_ref: str
    fence_ref: str
    submission_ref: str
    native_session_ref: str
    runtime_binding: IdeaRuntimeBinding
    runtime_binding_hash: str
    payload_hash: str
    payload_json: str
    material_outcome_hash: str
    outcome: dict[str, object]
    reviewed_draft: dict[str, object]
    reviewed_draft_hash: str
    review: dict[str, object]
    receipt: AcceptanceReceipt
    predecessor_attempt_ref: str | None = None
    predecessor_outcome_hash: str | None = None
    predecessor_material_outcome_hash: str | None = None
    predecessor_rejection_receipt: AcceptanceReceipt | None = None


@dataclass(frozen=True)
class IdeaProviderInvocation:
    invocation_ref: str
    request_ref: str
    run_ref: str
    attempt_ref: str
    fence_ref: str
    phase: str
    request_hash: str
    runtime_binding_hash: str
    status: str
    response_hash: str | None


@dataclass(frozen=True)
class IdeaPrimaryDraft:
    request_ref: str
    run_ref: str
    attempt_ref: str
    fence_ref: str
    native_session_ref: str
    runtime_binding_hash: str
    draft: dict[str, object]
    draft_hash: str
    adapter_kind: str


@dataclass(frozen=True)
class RunCompletion:
    request_ref: str
    run_ref: str
    attempt_ref: str
    outcome_ref: str
    decision_receipt: AcceptanceReceipt
    receipt: AcceptanceReceipt


@dataclass(frozen=True)
class IdeaStageRun:
    request_ref: str
    run_ref: str
    cycle_ref: str
    stage: str
    epoch: int
    status: str
    attempt_ref: str
    attempt_generation: int
    root_session_ref: str
    native_session_ref: str | None
    runtime_binding: IdeaRuntimeBinding
    runtime_binding_hash: str
    fence_ref: str
    primary_invocation: IdeaProviderInvocation
    review_invocation: IdeaProviderInvocation
    primary_draft: IdeaPrimaryDraft | None
    execution: AttemptExecution | None
    predecessor_execution: AttemptExecution | None
    rejection_receipt: AcceptanceReceipt | None
    completion: RunCompletion | None

    @property
    def attempt_execution_receipt(self) -> AcceptanceReceipt | None:
        return None if self.execution is None else self.execution.receipt

    @property
    def completion_receipt(self) -> AcceptanceReceipt | None:
        return None if self.completion is None else self.completion.receipt


@dataclass(frozen=True)
class DeepFetchRun:
    request_ref: str
    run_ref: str
    correlation_ref: str
    status: str
    attempt_ref: str | None
    attempt_generation: int
    root_session_ref: str
    native_session_ref: str | None
    fence_ref: str | None
    runtime_binding: DeepFetchRuntimeBinding
    runtime_binding_hash: str
    result: dict[str, object] | None
    result_hash: str | None
    execution_receipt: AcceptanceReceipt | None
    failure_code: str | None

    def as_public_dict(self) -> dict[str, object]:
        return {
            "run_ref": self.run_ref,
            "status": self.status,
            "attempt_ref": self.attempt_ref,
            "attempt_generation": self.attempt_generation,
            "root_session_ref": self.root_session_ref,
            "native_session_ref": self.native_session_ref,
            "fence_ref": self.fence_ref,
            "runtime_binding_hash": self.runtime_binding_hash,
            "execution_receipt": (
                None
                if self.execution_receipt is None
                else self.execution_receipt.as_public_dict()
            ),
            "failure": (
                None
                if self.failure_code is None
                else {"code": self.failure_code}
            ),
        }


class HostComputeObservationReader(Protocol):
    """Read-only AR seam for already persisted host observations."""

    def query_host_compute(self, snapshot_ref: str) -> HostComputeObservation: ...


class AgentRuntimeInterface(Protocol):
    """Whole public Interface for Run, Attempt, Session, Fence, and host facts."""

    def query_snapshot(self) -> OwnerSnapshot: ...

    def observe_host_compute(self, idempotency_key: str) -> HostComputeObservation: ...

    def query_host_compute(self, snapshot_ref: str) -> HostComputeObservation: ...

    def execute_deepfetch(
        self,
        request: DeepFetchRunRequest,
        provider: DeepFetchProvider,
    ) -> DeepFetchRun: ...

    def query_deepfetch_run(self, request_ref: str) -> DeepFetchRun | None: ...

    def cancel_deepfetch(self, request_ref: str) -> DeepFetchRun | None: ...

    def admit_idea_stage(
        self,
        request: StageRunRequest,
        idempotency_key: str,
        *,
        runtime_binding: IdeaRuntimeBinding,
    ) -> IdeaStageRun: ...

    def query_idea_stage_run(self, request_ref: str) -> IdeaStageRun | None: ...

    def record_idea_primary_draft(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        native_session_ref: str,
        runtime_binding: IdeaRuntimeBinding,
        draft: dict[str, object],
        adapter_kind: str,
        idempotency_key: str,
    ) -> IdeaPrimaryDraft: ...

    def record_idea_attempt_execution(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        submission_ref: str,
        native_session_ref: str,
        runtime_binding: IdeaRuntimeBinding,
        outcome: dict[str, object],
        review: dict[str, object],
        idempotency_key: str,
        reviewed_draft: dict[str, object] | None = None,
    ) -> AttemptExecution: ...

    def query_idea_attempt_execution(
        self, submission_ref: str
    ) -> AttemptExecution | None: ...

    def continue_after_idea_rejection(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        decision_receipt: AcceptanceReceipt,
        idempotency_key: str,
    ) -> IdeaStageRun: ...

    def complete_idea_run(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        outcome_ref: str,
        decision_receipt: AcceptanceReceipt,
        idempotency_key: str,
    ) -> RunCompletion: ...

    def query_idea_run_completion(self, run_ref: str) -> RunCompletion | None: ...

    def verify_attempt_execution_receipt(self, **values) -> None: ...

    def verify_run_completion_receipt(self, **values) -> None: ...

    def verify_deepfetch_execution_receipt(self, **values) -> None: ...


_SNAPSHOT = OwnerSnapshotQuery(
    owner=AR_OWNER,
    statement=text(
        "SELECT revision, active_run_count, stage_run_count, completed_run_count, "
        "attempt_count, session_count, deepfetch_run_count, "
        "deepfetch_completed_run_count, deepfetch_attempt_count, "
        "deepfetch_session_count "
        "FROM agent_runtime_state WHERE singleton = 'owner'"
    ),
    fact_names=(
        "active_run_count",
        "stage_run_count",
        "completed_run_count",
        "attempt_count",
        "session_count",
        "deepfetch_run_count",
        "deepfetch_completed_run_count",
        "deepfetch_attempt_count",
        "deepfetch_session_count",
    ),
)

# The production probe is bounded at five seconds. The wider lease prevents a
# healthy claimant from being replaced during probe cleanup and finalization.
_HOST_COMPUTE_CLAIM_LEASE_SECONDS = 15.0
_HOST_COMPUTE_CLAIM_POLL_SECONDS = 0.02


class SQLiteHostComputeObservationReader:
    def __init__(self, database: Database) -> None:
        self._database = database

    def query_host_compute(self, snapshot_ref: str) -> HostComputeObservation:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ar_host_capability_snapshots WHERE snapshot_ref = "
                    ":snapshot_ref"
                ),
                {"snapshot_ref": snapshot_ref},
            ).first()
        if row is None:
            raise OwnerConflict("host_compute_snapshot_not_found")
        return _observation_from_row(row)


class SQLiteAgentRuntime:
    """Agent Runtime owns durable host-capability observations and their integrity."""

    def __init__(
        self,
        database: Database,
        feed: DurableFeed,
        host_compute_probe: HostComputeProbe,
        stage_request_verifier: StageRunRequestVerifier | None = None,
        outcome_verifier: IdeaOutcomeDecisionVerifier | None = None,
        deepfetch_request_verifier: DeepFetchRunRequestVerifier | None = None,
    ) -> None:
        self._database = database
        self._feed = feed
        self._host_compute_probe = host_compute_probe
        self._stage_request_verifier = stage_request_verifier
        self._outcome_verifier = outcome_verifier
        self._deepfetch_request_verifier = deepfetch_request_verifier
        self._receipt_verifier = SQLiteAgentRuntimeReceiptVerifier(
            database, stage_request_verifier
        )
        self._host_compute_reader = SQLiteHostComputeObservationReader(database)
        self._snapshot = SQLiteOwnerSnapshot(database, _SNAPSHOT)
        self._deepfetch_provider_lock = threading.Lock()
        self._deepfetch_providers: dict[str, DeepFetchProvider] = {}
        self._recover_interrupted_deepfetch()

    def query_snapshot(self) -> OwnerSnapshot:
        return self._snapshot.query_snapshot()

    def observe_host_compute(self, idempotency_key: str) -> HostComputeObservation:
        if not idempotency_key or len(idempotency_key) > 128:
            raise OwnerConflict("idempotency_key_invalid")
        request_hash = canonical_hash(
            {"command": "observe_host_compute", "schema": "v1"}
        )
        while True:
            replay, claim_token = self._claim_or_replay(
                idempotency_key, request_hash
            )
            if replay is not None:
                return replay
            if claim_token is None:
                time.sleep(_HOST_COMPUTE_CLAIM_POLL_SECONDS)
                continue

            try:
                snapshot = self._host_compute_probe.observe()
                _validate_probe_snapshot(snapshot)
                capabilities = {
                    "devices": [device.as_dict() for device in snapshot.devices]
                }
                capabilities_hash = canonical_hash(capabilities)
                observation = self._complete_claim(
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    claim_token=claim_token,
                    snapshot=snapshot,
                    capabilities=capabilities,
                    capabilities_hash=capabilities_hash,
                )
            except BaseException:
                self._release_claim(idempotency_key, claim_token)
                raise
            if observation is not None:
                return observation

    def _claim_or_replay(
        self, idempotency_key: str, request_hash: str
    ) -> tuple[HostComputeObservation | None, str | None]:
        now = time.time()
        with self._database.read() as connection:
            stage_command = connection.execute(
                text(
                    "SELECT 1 FROM ar_stage_commands WHERE idempotency_key = "
                    ":idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
            replay = connection.execute(
                text(
                    "SELECT * FROM ar_host_capability_snapshots WHERE "
                    "idempotency_key = :idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
            claim = connection.execute(
                text(
                    "SELECT * FROM ar_host_compute_observation_claims WHERE "
                    "idempotency_key = :idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
        if stage_command is not None:
            raise OwnerConflict("idempotency_conflict")
        if replay is not None:
            if replay.request_hash != request_hash or (
                claim is not None and claim.request_hash != request_hash
            ):
                raise OwnerConflict("idempotency_conflict")
            return _observation_from_row(replay), None
        if claim is not None:
            if claim.request_hash != request_hash:
                raise OwnerConflict("idempotency_conflict")
            if float(claim.lease_expires_at) > now:
                return None, None

        claim_token = new_ref("host_claim")
        with self._database.write() as connection:
            # Serialize only the durable claim transition across daemon processes.
            # The external probe itself runs after this transaction commits.
            connection.execute(
                text(
                    "UPDATE agent_runtime_state SET revision = revision "
                    "WHERE singleton = 'owner'"
                )
            )
            claim_now = time.time()
            claim_deadline = claim_now + _HOST_COMPUTE_CLAIM_LEASE_SECONDS
            stage_command = connection.execute(
                text(
                    "SELECT 1 FROM ar_stage_commands WHERE idempotency_key = "
                    ":idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
            if stage_command is not None:
                raise OwnerConflict("idempotency_conflict")
            replay = connection.execute(
                text(
                    "SELECT * FROM ar_host_capability_snapshots WHERE "
                    "idempotency_key = :idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
            if replay is not None:
                if replay.request_hash != request_hash:
                    raise OwnerConflict("idempotency_conflict")
                return _observation_from_row(replay), None

            claim = connection.execute(
                text(
                    "SELECT * FROM ar_host_compute_observation_claims WHERE "
                    "idempotency_key = :idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
            if claim is not None:
                if claim.request_hash != request_hash:
                    raise OwnerConflict("idempotency_conflict")
                if float(claim.lease_expires_at) > claim_now:
                    return None, None
                connection.execute(
                    text(
                        "UPDATE ar_host_compute_observation_claims SET "
                        "claim_token = :claim_token, "
                        "attempt_count = attempt_count + 1, "
                        "claimed_at = :claimed_at, "
                        "lease_expires_at = :lease_expires_at "
                        "WHERE idempotency_key = :idempotency_key"
                    ),
                    {
                        "claim_token": claim_token,
                        "claimed_at": claim_now,
                        "lease_expires_at": claim_deadline,
                        "idempotency_key": idempotency_key,
                    },
                )
                return None, claim_token

            connection.execute(
                text(
                    "INSERT INTO ar_host_compute_observation_claims "
                    "(idempotency_key, request_hash, claim_token, attempt_count, "
                    "claimed_at, lease_expires_at) VALUES "
                    "(:idempotency_key, :request_hash, :claim_token, 1, "
                    ":claimed_at, :lease_expires_at)"
                ),
                {
                    "idempotency_key": idempotency_key,
                    "request_hash": request_hash,
                    "claim_token": claim_token,
                    "claimed_at": claim_now,
                    "lease_expires_at": claim_deadline,
                },
            )
        return None, claim_token

    def _complete_claim(
        self,
        *,
        idempotency_key: str,
        request_hash: str,
        claim_token: str,
        snapshot: HostComputeSnapshot,
        capabilities: dict[str, object],
        capabilities_hash: str,
    ) -> HostComputeObservation | None:
        snapshot_ref = new_ref("host_snapshot")
        with self._database.write() as connection:
            # Avoid a cross-process SQLite read-to-write upgrade race between
            # unrelated observations that finish probing at the same time.
            connection.execute(
                text(
                    "UPDATE agent_runtime_state SET revision = revision "
                    "WHERE singleton = 'owner'"
                )
            )
            replay = connection.execute(
                text(
                    "SELECT * FROM ar_host_capability_snapshots WHERE "
                    "idempotency_key = :idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
            if replay is not None:
                if replay.request_hash != request_hash:
                    raise OwnerConflict("idempotency_conflict")
                return _observation_from_row(replay)

            claim = connection.execute(
                text(
                    "SELECT * FROM ar_host_compute_observation_claims WHERE "
                    "idempotency_key = :idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
            if claim is None or claim.claim_token != claim_token:
                return None
            if claim.request_hash != request_hash:
                raise OwnerConflict("idempotency_conflict")

            connection.execute(
                text(
                    "INSERT INTO ar_host_capability_snapshots "
                    "(snapshot_ref, idempotency_key, request_hash, adapter_kind, "
                    "status, capabilities_json, capabilities_hash, reason_code, "
                    "observed_at) VALUES (:snapshot_ref, :idempotency_key, "
                    ":request_hash, :adapter_kind, :status, :capabilities_json, "
                    ":capabilities_hash, :reason_code, :observed_at)"
                ),
                {
                    "snapshot_ref": snapshot_ref,
                    "idempotency_key": idempotency_key,
                    "request_hash": request_hash,
                    "adapter_kind": snapshot.adapter_kind,
                    "status": snapshot.status,
                    "capabilities_json": canonical_json(capabilities),
                    "capabilities_hash": capabilities_hash,
                    "reason_code": snapshot.reason_code,
                    "observed_at": snapshot.observed_at,
                },
            )
            connection.execute(
                text(
                    "DELETE FROM ar_host_compute_observation_claims WHERE "
                    "idempotency_key = :idempotency_key AND claim_token = :claim_token"
                ),
                {
                    "idempotency_key": idempotency_key,
                    "claim_token": claim_token,
                },
            )
            connection.execute(
                text(
                    "UPDATE agent_runtime_state SET revision = revision + 1 WHERE "
                    "singleton = 'owner'"
                )
            )
            self._feed.record(
                connection,
                "agent_runtime.host_compute_observed",
                {
                    "snapshot_ref": snapshot_ref,
                    "status": snapshot.status,
                    "capabilities_hash": capabilities_hash,
                },
            )
        return HostComputeObservation(
            snapshot_ref=snapshot_ref,
            status=snapshot.status,
            observed_at=snapshot.observed_at,
            devices=snapshot.devices,
            adapter_kind=snapshot.adapter_kind,
            capabilities_hash=capabilities_hash,
            reason_code=snapshot.reason_code,
        )

    def _release_claim(self, idempotency_key: str, claim_token: str) -> None:
        try:
            with self._database.write() as connection:
                connection.execute(
                    text(
                        "DELETE FROM ar_host_compute_observation_claims WHERE "
                        "idempotency_key = :idempotency_key "
                        "AND claim_token = :claim_token"
                    ),
                    {
                        "idempotency_key": idempotency_key,
                        "claim_token": claim_token,
                    },
                )
        except Exception:
            # A persisted lease is the recovery path when immediate cleanup cannot
            # acquire the database after a probe/finalization failure.
            pass

    def query_host_compute(self, snapshot_ref: str) -> HostComputeObservation:
        return self._host_compute_reader.query_host_compute(snapshot_ref)

    def _recover_interrupted_deepfetch(self) -> None:
        now = time.time()
        with self._database.write() as connection:
            attempts = connection.execute(
                text(
                    "UPDATE ar_deepfetch_attempts SET status = 'superseded', "
                    "failure_code = 'daemon_restarted', completed_at = :now "
                    "WHERE status = 'running'"
                ),
                {"now": now},
            )
            runs = connection.execute(
                text(
                    "UPDATE ar_deepfetch_runs SET status = 'admitted', "
                    "current_attempt_ref = NULL, failure_code = NULL, "
                    "completed_at = NULL, updated_at = :now WHERE status = 'running'"
                ),
                {"now": now},
            )
            recovered = (attempts.rowcount or 0) + (runs.rowcount or 0)
            if recovered:
                connection.execute(
                    text(
                        "UPDATE agent_runtime_state SET revision = revision + 1 "
                        "WHERE singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "agent_runtime.deepfetch_recovered",
                    {"recovered_record_count": recovered},
                )

    def execute_deepfetch(
        self,
        request: DeepFetchRunRequest,
        provider: DeepFetchProvider,
    ) -> DeepFetchRun:
        if self._deepfetch_request_verifier is None:
            raise OwnerConflict("deepfetch_request_verifier_unavailable")
        self._deepfetch_request_verifier.verify_deepfetch_run_request(
            request_ref=request.request_ref,
            initialization_id=request.initialization_id,
            correlation_ref=request.correlation_ref,
            draft_revision=request.draft_revision,
            draft_hash=request.draft_hash,
            scope_hash=request.scope_hash,
            resource_envelope_ref=request.resource_envelope_ref,
            resource_envelope_hash=request.resource_envelope_hash,
            result_route=request.result_route,
            receipt=request.authorization_receipt,
        )
        if (
            request.result_route != "same_quest_initialization_proposal"
            or canonical_hash(request.scope) != request.scope_hash
            or request.draft_revision < 1
            or canonical_hash(request.draft) != request.draft_hash
        ):
            raise OwnerConflict("deepfetch_run_request_invalid")
        try:
            runtime_binding = provider.runtime_binding()
            runtime_binding_hash = validate_runtime_binding(runtime_binding)
        except DeepFetchUnavailable as error:
            raise OwnerConflict(error.code) from error
        runtime_binding_json = canonical_json(runtime_binding.as_dict())
        request_hash = canonical_hash(request.payload())

        existing = self.query_deepfetch_run(request.request_ref)
        if existing is not None and existing.status == "executed":
            if (
                existing.correlation_ref != request.correlation_ref
                or existing.runtime_binding_hash != runtime_binding_hash
            ):
                raise OwnerConflict("deepfetch_run_identity_conflict")
            return existing

        run_ref, root_session_ref, attempt_ref, generation, fence_ref, native_ref = (
            self._start_deepfetch_attempt(
                request=request,
                request_hash=request_hash,
                runtime_binding_json=runtime_binding_json,
                runtime_binding_hash=runtime_binding_hash,
            )
        )
        job_ref = f"{run_ref}:attempt:{generation}"
        provider_request = DeepFetchProviderRequest(
            request_ref=request.request_ref,
            initialization_id=request.initialization_id,
            correlation_ref=request.correlation_ref,
            draft_revision=request.draft_revision,
            draft_hash=request.draft_hash,
            scope=request.scope,
            scope_hash=request.scope_hash,
            accepted_material_bindings=request.accepted_material_bindings,
            authorization_receipt=request.authorization_receipt,
            runtime_binding=runtime_binding,
            run_ref=run_ref,
            root_session_ref=root_session_ref,
            attempt_ref=attempt_ref,
            attempt_generation=generation,
            fence_ref=fence_ref,
            native_session_ref=native_ref,
            job_ref=job_ref,
        )
        with self._deepfetch_provider_lock:
            self._deepfetch_providers[request.request_ref] = provider
        try:
            result = provider.execute(provider_request)
            result_payload, result_hash = validate_deepfetch_result(
                provider_request, result
            )
            return self._complete_deepfetch_attempt(
                request=request,
                run_ref=run_ref,
                attempt_ref=attempt_ref,
                generation=generation,
                fence_ref=fence_ref,
                runtime_binding_hash=runtime_binding_hash,
                result_payload=result_payload,
                result_hash=result_hash,
            )
        except DeepFetchUnavailable as error:
            self._fail_deepfetch_attempt(
                run_ref=run_ref,
                attempt_ref=attempt_ref,
                generation=generation,
                fence_ref=fence_ref,
                failure_code=error.code,
            )
            raise
        except BaseException:
            self._fail_deepfetch_attempt(
                run_ref=run_ref,
                attempt_ref=attempt_ref,
                generation=generation,
                fence_ref=fence_ref,
                failure_code="deepfetch_provider_error",
            )
            raise
        finally:
            with self._deepfetch_provider_lock:
                current_provider = self._deepfetch_providers.get(request.request_ref)
                if current_provider is provider:
                    self._deepfetch_providers.pop(request.request_ref, None)
            finish_job = getattr(provider, "finish_job", None)
            if callable(finish_job):
                finish_job(job_ref)

    def _start_deepfetch_attempt(
        self,
        *,
        request: DeepFetchRunRequest,
        request_hash: str,
        runtime_binding_json: str,
        runtime_binding_hash: str,
    ) -> tuple[str, str, str, int, str, str | None]:
        now = time.time()
        with self._database.write() as connection:
            run = connection.execute(
                text(
                    "SELECT * FROM ar_deepfetch_runs WHERE request_ref = :request_ref"
                ),
                {"request_ref": request.request_ref},
            ).first()
            is_new = run is None
            was_failed = run is not None and run.status == "failed"
            if run is not None:
                if (
                    run.request_hash != request_hash
                    or run.correlation_ref != request.correlation_ref
                    or run.runtime_binding_json != runtime_binding_json
                    or run.runtime_binding_hash != runtime_binding_hash
                ):
                    raise OwnerConflict("deepfetch_run_identity_conflict")
                if run.status == "executed":
                    raise OwnerConflict("deepfetch_run_already_executed")
                if run.status == "running":
                    raise OwnerConflict("deepfetch_run_busy")
                if run.status == "cancelled":
                    raise OwnerConflict("deepfetch_run_cancelled")
                run_ref = str(run.run_ref)
                generation = int(run.attempt_generation) + 1
                session = connection.execute(
                    text(
                        "SELECT * FROM ar_deepfetch_sessions WHERE run_ref = :run_ref"
                    ),
                    {"run_ref": run_ref},
                ).one()
                root_session_ref = str(session.root_session_ref)
                native_session_ref = (
                    None
                    if session.native_session_ref is None
                    else str(session.native_session_ref)
                )
            else:
                run_ref = new_ref("deepfetch_run")
                root_session_ref = new_ref("deepfetch_session")
                native_session_ref = None
                generation = 1
                connection.execute(
                    text(
                        "INSERT INTO ar_deepfetch_runs (run_ref, request_ref, "
                        "correlation_ref, request_hash, runtime_binding_json, "
                        "runtime_binding_hash, status, attempt_generation, created_at, "
                        "updated_at) VALUES (:run_ref, :request_ref, :correlation_ref, "
                        ":request_hash, :runtime_binding_json, :runtime_binding_hash, "
                        "'admitted', 0, :now, :now)"
                    ),
                    {
                        "run_ref": run_ref,
                        "request_ref": request.request_ref,
                        "correlation_ref": request.correlation_ref,
                        "request_hash": request_hash,
                        "runtime_binding_json": runtime_binding_json,
                        "runtime_binding_hash": runtime_binding_hash,
                        "now": now,
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO ar_deepfetch_sessions (root_session_ref, run_ref, "
                        "status, created_at, updated_at) VALUES (:root_session_ref, "
                        ":run_ref, 'open', :now, :now)"
                    ),
                    {
                        "root_session_ref": root_session_ref,
                        "run_ref": run_ref,
                        "now": now,
                    },
                )
            attempt_ref = new_ref("deepfetch_attempt")
            fence_ref = new_ref("deepfetch_fence")
            connection.execute(
                text(
                    "INSERT INTO ar_deepfetch_attempts (attempt_ref, run_ref, "
                    "generation, root_session_ref, fence_ref, status, started_at) "
                    "VALUES (:attempt_ref, :run_ref, :generation, :root_session_ref, "
                    ":fence_ref, 'running', :now)"
                ),
                {
                    "attempt_ref": attempt_ref,
                    "run_ref": run_ref,
                    "generation": generation,
                    "root_session_ref": root_session_ref,
                    "fence_ref": fence_ref,
                    "now": now,
                },
            )
            connection.execute(
                text(
                    "UPDATE ar_deepfetch_runs SET status = 'running', "
                    "current_attempt_ref = :attempt_ref, "
                    "attempt_generation = :generation, failure_code = NULL, "
                    "completed_at = NULL, updated_at = :now WHERE run_ref = :run_ref"
                ),
                {
                    "run_ref": run_ref,
                    "attempt_ref": attempt_ref,
                    "generation": generation,
                    "now": now,
                },
            )
            connection.execute(
                text(
                    "UPDATE agent_runtime_state SET revision = revision + 1, "
                    "active_run_count = active_run_count + :active_increment, "
                    "deepfetch_run_count = deepfetch_run_count + :run_increment, "
                    "deepfetch_attempt_count = deepfetch_attempt_count + 1, "
                    "deepfetch_session_count = deepfetch_session_count + "
                    ":session_increment WHERE singleton = 'owner'"
                ),
                {
                    "active_increment": 1 if is_new or was_failed else 0,
                    "run_increment": 1 if is_new else 0,
                    "session_increment": 1 if is_new else 0,
                },
            )
            self._feed.record(
                connection,
                "agent_runtime.deepfetch_attempt_started",
                {
                    "request_ref": request.request_ref,
                    "run_ref": run_ref,
                    "attempt_ref": attempt_ref,
                    "attempt_generation": generation,
                    "fence_ref": fence_ref,
                },
            )
        return (
            run_ref,
            root_session_ref,
            attempt_ref,
            generation,
            fence_ref,
            native_session_ref,
        )

    def _complete_deepfetch_attempt(
        self,
        *,
        request: DeepFetchRunRequest,
        run_ref: str,
        attempt_ref: str,
        generation: int,
        fence_ref: str,
        runtime_binding_hash: str,
        result_payload: dict[str, object],
        result_hash: str,
    ) -> DeepFetchRun:
        native_session_ref = str(result_payload["native_session_ref"])
        receipt_ref = new_ref("ar_receipt")
        receipt_bindings = {
            "request_ref": request.request_ref,
            "run_ref": run_ref,
            "attempt_ref": attempt_ref,
            "attempt_generation": generation,
            "fence_ref": fence_ref,
            "native_session_ref": native_session_ref,
            "runtime_binding_hash": runtime_binding_hash,
            "result_hash": result_hash,
        }
        receipt_hash = _owner_receipt_hash(
            DEEPFETCH_EXECUTION_RECEIPT_KIND,
            run_ref,
            receipt_bindings,
        )
        now = time.time()
        with self._database.write() as connection:
            run = connection.execute(
                text(
                    "SELECT * FROM ar_deepfetch_runs WHERE run_ref = :run_ref"
                ),
                {"run_ref": run_ref},
            ).one()
            attempt = connection.execute(
                text(
                    "SELECT * FROM ar_deepfetch_attempts WHERE "
                    "attempt_ref = :attempt_ref"
                ),
                {"attempt_ref": attempt_ref},
            ).one()
            session = connection.execute(
                text(
                    "SELECT * FROM ar_deepfetch_sessions WHERE run_ref = :run_ref"
                ),
                {"run_ref": run_ref},
            ).one()
            if (
                run.status != "running"
                or run.current_attempt_ref != attempt_ref
                or int(run.attempt_generation) != generation
                or attempt.status != "running"
                or attempt.fence_ref != fence_ref
                or session.status != "open"
            ):
                raise OwnerConflict("deepfetch_attempt_fence_stale")
            if session.native_session_ref is not None and (
                session.native_session_ref != native_session_ref
            ):
                raise OwnerConflict("deepfetch_native_session_changed")
            connection.execute(
                text(
                    "UPDATE ar_deepfetch_sessions SET native_session_ref = "
                    ":native_session_ref, status = 'completed', updated_at = :now "
                    "WHERE run_ref = :run_ref"
                ),
                {
                    "run_ref": run_ref,
                    "native_session_ref": native_session_ref,
                    "now": now,
                },
            )
            connection.execute(
                text(
                    "UPDATE ar_deepfetch_attempts SET status = 'executed', "
                    "result_hash = :result_hash, completed_at = :now WHERE "
                    "attempt_ref = :attempt_ref AND status = 'running'"
                ),
                {
                    "attempt_ref": attempt_ref,
                    "result_hash": result_hash,
                    "now": now,
                },
            )
            connection.execute(
                text(
                    "UPDATE ar_deepfetch_runs SET status = 'executed', "
                    "result_json = :result_json, result_hash = :result_hash, "
                    "execution_receipt_ref = :receipt_ref, "
                    "execution_receipt_hash = :receipt_hash, failure_code = NULL, "
                    "updated_at = :now, completed_at = :now WHERE run_ref = :run_ref"
                ),
                {
                    "run_ref": run_ref,
                    "result_json": canonical_json(result_payload),
                    "result_hash": result_hash,
                    "receipt_ref": receipt_ref,
                    "receipt_hash": receipt_hash,
                    "now": now,
                },
            )
            connection.execute(
                text(
                    "UPDATE agent_runtime_state SET revision = revision + 1, "
                    "active_run_count = active_run_count - 1, "
                    "deepfetch_completed_run_count = "
                    "deepfetch_completed_run_count + 1 WHERE singleton = 'owner' "
                    "AND active_run_count > 0"
                )
            )
            self._feed.record(
                connection,
                "agent_runtime.deepfetch_executed",
                {
                    "request_ref": request.request_ref,
                    "run_ref": run_ref,
                    "attempt_ref": attempt_ref,
                    "result_hash": result_hash,
                    "receipt_ref": receipt_ref,
                },
            )
        completed = self.query_deepfetch_run(request.request_ref)
        assert completed is not None
        return completed

    def _fail_deepfetch_attempt(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        generation: int,
        fence_ref: str,
        failure_code: str,
    ) -> None:
        now = time.time()
        with self._database.write() as connection:
            run = connection.execute(
                text(
                    "SELECT status, current_attempt_ref, attempt_generation, "
                    "request_ref FROM ar_deepfetch_runs WHERE run_ref = :run_ref"
                ),
                {"run_ref": run_ref},
            ).first()
            attempt = connection.execute(
                text(
                    "SELECT status, fence_ref FROM ar_deepfetch_attempts WHERE "
                    "attempt_ref = :attempt_ref"
                ),
                {"attempt_ref": attempt_ref},
            ).first()
            if run is None or attempt is None or (
                run.status != "running"
                or run.current_attempt_ref != attempt_ref
                or int(run.attempt_generation) != generation
                or attempt.status != "running"
                or attempt.fence_ref != fence_ref
            ):
                return
            connection.execute(
                text(
                    "UPDATE ar_deepfetch_attempts SET status = 'failed', "
                    "failure_code = :failure_code, completed_at = :now WHERE "
                    "attempt_ref = :attempt_ref"
                ),
                {
                    "attempt_ref": attempt_ref,
                    "failure_code": failure_code,
                    "now": now,
                },
            )
            connection.execute(
                text(
                    "UPDATE ar_deepfetch_runs SET status = 'failed', "
                    "failure_code = :failure_code, updated_at = :now, "
                    "completed_at = :now WHERE run_ref = :run_ref"
                ),
                {
                    "run_ref": run_ref,
                    "failure_code": failure_code,
                    "now": now,
                },
            )
            connection.execute(
                text(
                    "UPDATE agent_runtime_state SET revision = revision + 1, "
                    "active_run_count = active_run_count - 1 WHERE "
                    "singleton = 'owner' AND active_run_count > 0"
                )
            )
            self._feed.record(
                connection,
                "agent_runtime.deepfetch_failed",
                {
                    "request_ref": run.request_ref,
                    "run_ref": run_ref,
                    "attempt_ref": attempt_ref,
                    "reason_code": failure_code,
                },
            )

    def query_deepfetch_run(self, request_ref: str) -> DeepFetchRun | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT r.*, s.root_session_ref, s.native_session_ref, "
                    "s.status AS session_status, a.attempt_ref AS joined_attempt_ref, "
                    "a.generation AS joined_generation, a.fence_ref, "
                    "a.status AS attempt_status FROM ar_deepfetch_runs r "
                    "JOIN ar_deepfetch_sessions s ON s.run_ref = r.run_ref "
                    "LEFT JOIN ar_deepfetch_attempts a ON a.attempt_ref = "
                    "r.current_attempt_ref WHERE r.request_ref = :request_ref"
                ),
                {"request_ref": request_ref},
            ).first()
        return None if row is None else _deepfetch_run_from_row(row)

    def cancel_deepfetch(self, request_ref: str) -> DeepFetchRun | None:
        current_before_cancel = self.query_deepfetch_run(request_ref)
        with self._deepfetch_provider_lock:
            provider = self._deepfetch_providers.get(request_ref)
        if provider is not None:
            cancel_job = getattr(provider, "cancel_job", None)
            if callable(cancel_job):
                current = current_before_cancel
                if current is not None and current.attempt_generation > 0:
                    cancel_job(
                        f"{current.run_ref}:attempt:{current.attempt_generation}"
                    )
        now = time.time()
        with self._database.write() as connection:
            run = connection.execute(
                text(
                    "SELECT * FROM ar_deepfetch_runs WHERE request_ref = :request_ref"
                ),
                {"request_ref": request_ref},
            ).first()
            if run is None or run.status in {"executed", "cancelled"}:
                return current_before_cancel
            was_active = run.status in {"admitted", "running"}
            if run.current_attempt_ref is not None:
                connection.execute(
                    text(
                        "UPDATE ar_deepfetch_attempts SET status = 'cancelled', "
                        "failure_code = 'deepfetch_cancelled', completed_at = :now "
                        "WHERE attempt_ref = :attempt_ref AND status = 'running'"
                    ),
                    {"attempt_ref": run.current_attempt_ref, "now": now},
                )
            connection.execute(
                text(
                    "UPDATE ar_deepfetch_sessions SET status = 'cancelled', "
                    "updated_at = :now WHERE run_ref = :run_ref"
                ),
                {"run_ref": run.run_ref, "now": now},
            )
            connection.execute(
                text(
                    "UPDATE ar_deepfetch_runs SET status = 'cancelled', "
                    "failure_code = 'deepfetch_cancelled', updated_at = :now, "
                    "completed_at = :now WHERE run_ref = :run_ref"
                ),
                {"run_ref": run.run_ref, "now": now},
            )
            connection.execute(
                text(
                    "UPDATE agent_runtime_state SET revision = revision + 1, "
                    "active_run_count = active_run_count - :active_decrement WHERE "
                    "singleton = 'owner' AND active_run_count >= :active_decrement"
                ),
                {"active_decrement": 1 if was_active else 0},
            )
            self._feed.record(
                connection,
                "agent_runtime.deepfetch_cancelled",
                {"request_ref": request_ref, "run_ref": run.run_ref},
            )
        return self.query_deepfetch_run(request_ref)

    def admit_idea_stage(
        self,
        request: StageRunRequest,
        idempotency_key: str,
        *,
        runtime_binding: IdeaRuntimeBinding,
    ) -> IdeaStageRun:
        _validate_stage_idempotency_key(idempotency_key)
        runtime_binding, runtime_binding_json, runtime_binding_hash = (
            _validated_runtime_binding(runtime_binding)
        )
        command_hash = canonical_hash(
            {
                "command": "admit_idea_stage",
                "request_ref": request.request_ref,
                "cycle_ref": request.cycle_ref,
                "stage": request.stage,
                "epoch": request.epoch,
                "context_pack_ref": request.context_pack_ref,
                "context_pack_hash": request.context_pack_hash,
                "request_receipt": request.receipt.as_public_dict(),
                "runtime_binding": runtime_binding.as_dict(),
                "runtime_binding_hash": runtime_binding_hash,
            }
        )
        _query_stage_command(
            self._database,
            idempotency_key,
            "admit_idea_stage",
            command_hash,
        )
        if self._stage_request_verifier is None:
            raise OwnerConflict("stage_request_verifier_unavailable")
        if (
            request.stage != "idea"
            or request.epoch < 1
            or canonical_hash(request.context_pack) != request.context_pack_hash
        ):
            raise OwnerConflict("stage_run_request_invalid")
        self._stage_request_verifier.verify_stage_run_request(
            request_ref=request.request_ref,
            cycle_ref=request.cycle_ref,
            epoch=request.epoch,
            context_pack_ref=request.context_pack_ref,
            context_pack_hash=request.context_pack_hash,
            receipt=request.receipt,
        )
        with self._database.write() as connection:
            replay_ref = _stage_command_replay(
                connection, idempotency_key, "admit_idea_stage", command_hash
            )
            if replay_ref is not None:
                row = connection.execute(
                    text("SELECT request_ref FROM ar_stage_runs WHERE run_ref = :run_ref"),
                    {"run_ref": replay_ref},
                ).first()
                if row is None:
                    raise OwnerConflict("stage_command_result_missing")
                replay_request_ref = row.request_ref
            else:
                replay_request_ref = None
            if replay_request_ref is None:
                existing = connection.execute(
                    text(
                        "SELECT * FROM ar_stage_runs WHERE request_ref = :request_ref"
                    ),
                    {"request_ref": request.request_ref},
                ).first()
                if existing is not None:
                    if existing.admission_hash != command_hash:
                        raise OwnerConflict("stage_run_admission_conflict")
                    _record_stage_command(
                        connection,
                        idempotency_key,
                        "admit_idea_stage",
                        command_hash,
                        existing.run_ref,
                    )
                    replay_request_ref = existing.request_ref
                else:
                    now = time.time()
                    run_ref = new_ref("idea_run")
                    attempt_ref = new_ref("idea_attempt")
                    session_ref = new_ref("idea_session")
                    fence_ref = new_ref("idea_fence")
                    connection.execute(
                        text(
                            "INSERT INTO ar_stage_runs (run_ref, request_ref, "
                            "cycle_ref, stage, epoch, context_pack_ref, "
                            "context_pack_hash, runtime_binding_json, "
                            "runtime_binding_hash, request_receipt_ref, "
                            "request_receipt_hash, status, current_attempt_ref, "
                            "root_session_ref, current_fence_ref, admission_key, "
                            "admission_hash, created_at, updated_at) VALUES "
                            "(:run_ref, :request_ref, :cycle_ref, :stage, :epoch, "
                            ":context_pack_ref, :context_pack_hash, "
                            ":runtime_binding_json, :runtime_binding_hash, "
                            ":request_receipt_ref, :request_receipt_hash, 'running', "
                            ":attempt_ref, :session_ref, :fence_ref, :admission_key, "
                            ":admission_hash, :created_at, :updated_at)"
                        ),
                        {
                            "run_ref": run_ref,
                            "request_ref": request.request_ref,
                            "cycle_ref": request.cycle_ref,
                            "stage": request.stage,
                            "epoch": request.epoch,
                            "context_pack_ref": request.context_pack_ref,
                            "context_pack_hash": request.context_pack_hash,
                            "runtime_binding_json": runtime_binding_json,
                            "runtime_binding_hash": runtime_binding_hash,
                            "request_receipt_ref": request.receipt.receipt_ref,
                            "request_receipt_hash": request.receipt.payload_hash,
                            "attempt_ref": attempt_ref,
                            "session_ref": session_ref,
                            "fence_ref": fence_ref,
                            "admission_key": idempotency_key,
                            "admission_hash": command_hash,
                            "created_at": now,
                            "updated_at": now,
                        },
                    )
                    connection.execute(
                        text(
                            "INSERT INTO ar_stage_sessions (session_ref, run_ref, "
                            "native_session_ref, status, created_at, updated_at) "
                            "VALUES (:session_ref, :run_ref, NULL, 'active', "
                            ":created_at, :updated_at)"
                        ),
                        {
                            "session_ref": session_ref,
                            "run_ref": run_ref,
                            "created_at": now,
                            "updated_at": now,
                        },
                    )
                    connection.execute(
                        text(
                            "INSERT INTO ar_stage_attempts (attempt_ref, run_ref, "
                            "generation, root_session_ref, fence_ref, status, "
                            "created_at) VALUES (:attempt_ref, :run_ref, 1, "
                            ":session_ref, :fence_ref, 'running', :created_at)"
                        ),
                        {
                            "attempt_ref": attempt_ref,
                            "run_ref": run_ref,
                            "session_ref": session_ref,
                            "fence_ref": fence_ref,
                            "created_at": now,
                        },
                    )
                    connection.execute(
                        text(
                            "INSERT INTO ar_execution_fences (fence_ref, run_ref, "
                            "attempt_ref, generation, status, issued_at) VALUES "
                            "(:fence_ref, :run_ref, :attempt_ref, 1, 'current', "
                            ":issued_at)"
                        ),
                        {
                            "fence_ref": fence_ref,
                            "run_ref": run_ref,
                            "attempt_ref": attempt_ref,
                            "issued_at": now,
                        },
                    )
                    _insert_provider_invocations(
                        connection,
                        request_ref=request.request_ref,
                        run_ref=run_ref,
                        attempt_ref=attempt_ref,
                        generation=1,
                        root_session_ref=session_ref,
                        fence_ref=fence_ref,
                        context_pack_ref=request.context_pack_ref,
                        context_pack_hash=request.context_pack_hash,
                        runtime_binding_hash=runtime_binding_hash,
                        predecessor_attempt_ref=None,
                        prepared_at=now,
                    )
                    _record_stage_command(
                        connection,
                        idempotency_key,
                        "admit_idea_stage",
                        command_hash,
                        run_ref,
                    )
                    connection.execute(
                        text(
                            "UPDATE agent_runtime_state SET revision = revision + 1, "
                            "active_run_count = active_run_count + 1, "
                            "stage_run_count = stage_run_count + 1, "
                            "attempt_count = attempt_count + 1, "
                            "session_count = session_count + 1 "
                            "WHERE singleton = 'owner'"
                        )
                    )
                    self._feed.record(
                        connection,
                        "agent_runtime.stage_run_admitted",
                        {
                            "request_ref": request.request_ref,
                            "run_ref": run_ref,
                            "attempt_ref": attempt_ref,
                            "root_session_ref": session_ref,
                            "fence_ref": fence_ref,
                            "stage": request.stage,
                            "epoch": request.epoch,
                            "runtime_binding_hash": runtime_binding_hash,
                        },
                    )
                    replay_request_ref = request.request_ref
        admitted = self.query_idea_stage_run(replay_request_ref)
        if admitted is None:
            raise OwnerConflict("stage_run_missing_after_admission")
        return admitted

    def query_idea_stage_run(self, request_ref: str) -> IdeaStageRun | None:
        with self._database.read() as connection:
            run = connection.execute(
                text("SELECT * FROM ar_stage_runs WHERE request_ref = :request_ref"),
                {"request_ref": request_ref},
            ).first()
        if run is None:
            return None
        return self._idea_stage_run_from_row(run)

    def _idea_stage_run_from_row(self, run) -> IdeaStageRun:
        runtime_binding = _runtime_binding_from_row(run)
        with self._database.read() as connection:
            session = connection.execute(
                text(
                    "SELECT * FROM ar_stage_sessions WHERE session_ref = "
                    ":session_ref AND run_ref = :run_ref"
                ),
                {"session_ref": run.root_session_ref, "run_ref": run.run_ref},
            ).first()
            attempt = connection.execute(
                text(
                    "SELECT * FROM ar_stage_attempts WHERE attempt_ref = "
                    ":attempt_ref AND run_ref = :run_ref"
                ),
                {"attempt_ref": run.current_attempt_ref, "run_ref": run.run_ref},
            ).first()
            fence = connection.execute(
                text(
                    "SELECT * FROM ar_execution_fences WHERE fence_ref = "
                    ":fence_ref AND attempt_ref = :attempt_ref"
                ),
                {
                    "fence_ref": run.current_fence_ref,
                    "attempt_ref": run.current_attempt_ref,
                },
            ).first()
            predecessor = None
            if attempt is not None and attempt.predecessor_attempt_ref is not None:
                predecessor = connection.execute(
                    text(
                        "SELECT * FROM ar_stage_attempts WHERE attempt_ref = "
                        ":attempt_ref AND run_ref = :run_ref"
                    ),
                    {
                        "attempt_ref": attempt.predecessor_attempt_ref,
                        "run_ref": run.run_ref,
                    },
                ).first()
        if session is None or attempt is None or fence is None or (
            attempt.root_session_ref != session.session_ref
            or attempt.fence_ref != fence.fence_ref
            or int(attempt.generation) != int(fence.generation)
        ):
            raise OwnerConflict("stage_run_integrity_invalid")
        with self._database.read() as connection:
            primary_invocation, review_invocation = _provider_invocations(
                connection, run, attempt, fence
            )
        if self._stage_request_verifier is not None:
            self._stage_request_verifier.verify_stage_run_request(
                request_ref=run.request_ref,
                cycle_ref=run.cycle_ref,
                epoch=int(run.epoch),
                context_pack_ref=run.context_pack_ref,
                context_pack_hash=run.context_pack_hash,
                receipt=AcceptanceReceipt(
                    issuer="advancement_engine",
                    kind="stage_run_request",
                    receipt_ref=run.request_receipt_ref,
                    subject_ref=run.request_ref,
                    payload_hash=run.request_receipt_hash,
                ),
            )
        execution = None
        if attempt.submission_ref is not None:
            execution = _attempt_execution(run, attempt, session)
            self._receipt_verifier.verify_attempt_execution_receipt(
                request_ref=run.request_ref,
                run_ref=run.run_ref,
                attempt_ref=attempt.attempt_ref,
                fence_ref=attempt.fence_ref,
                submission_ref=attempt.submission_ref,
                payload_hash=attempt.payload_hash,
                receipt=execution.receipt,
            )
        primary_draft = _primary_draft(run, attempt, session)
        if primary_draft is None:
            if primary_invocation.status != "prepared":
                raise OwnerConflict("idea_provider_invocation_invalid")
        elif (
            primary_invocation.status != "completed"
            or primary_invocation.response_hash
            != _primary_provider_response_hash(
                native_session_ref=primary_draft.native_session_ref,
                draft=primary_draft.draft,
                adapter_kind=primary_draft.adapter_kind,
            )
        ):
            raise OwnerConflict("idea_provider_invocation_invalid")
        if execution is None:
            if review_invocation.status != "prepared":
                raise OwnerConflict("idea_provider_invocation_invalid")
        elif (
            primary_draft is None
            or review_invocation.status != "completed"
            or review_invocation.response_hash
            != _review_provider_response_hash(
                native_session_ref=execution.native_session_ref,
                reviewed_draft=execution.reviewed_draft,
                outcome=execution.outcome,
                review=execution.review,
            )
        ):
            raise OwnerConflict("idea_provider_invocation_invalid")
        predecessor_execution = None
        rejection_receipt = None
        if predecessor is not None:
            predecessor_execution = _attempt_execution(run, predecessor, session)
            self._receipt_verifier.verify_attempt_execution_receipt(
                request_ref=run.request_ref,
                run_ref=run.run_ref,
                attempt_ref=predecessor.attempt_ref,
                fence_ref=predecessor.fence_ref,
                submission_ref=predecessor.submission_ref,
                payload_hash=predecessor.payload_hash,
                receipt=predecessor_execution.receipt,
            )
            if (
                predecessor.status != "rejected"
                or predecessor.run_ref != run.run_ref
                or predecessor.root_session_ref != session.session_ref
                or int(attempt.generation) != int(predecessor.generation) + 1
                or predecessor.decision_receipt_ref is None
                or predecessor.decision_receipt_subject_ref is None
                or predecessor.decision_receipt_hash is None
            ):
                raise OwnerConflict("rejection_lineage_invalid")
            rejection_receipt = AcceptanceReceipt(
                issuer="research_graph",
                kind="idea_outcome_rejected",
                receipt_ref=predecessor.decision_receipt_ref,
                subject_ref=predecessor.decision_receipt_subject_ref,
                payload_hash=predecessor.decision_receipt_hash,
            )
            if self._outcome_verifier is None:
                raise OwnerConflict("idea_outcome_verifier_unavailable")
            self._outcome_verifier.verify_idea_outcome_decision(
                request_ref=run.request_ref,
                submission_ref=predecessor.submission_ref,
                decision="rejected",
                outcome_ref=None,
                receipt=rejection_receipt,
            )
            if execution is not None:
                with self._database.read() as connection:
                    _successor_execution_lineage(
                        connection,
                        run,
                        attempt,
                        session,
                        native_session_ref=session.native_session_ref,
                        outcome=execution.outcome,
                    )
        completion = _run_completion(run, attempt) if run.status == "completed" else None
        if completion is not None:
            self._receipt_verifier.verify_run_completion_receipt(
                request_ref=run.request_ref,
                run_ref=run.run_ref,
                attempt_ref=attempt.attempt_ref,
                outcome_ref=completion.outcome_ref,
                receipt=completion.receipt,
            )
        return IdeaStageRun(
            request_ref=run.request_ref,
            run_ref=run.run_ref,
            cycle_ref=run.cycle_ref,
            stage=run.stage,
            epoch=int(run.epoch),
            status=run.status,
            attempt_ref=attempt.attempt_ref,
            attempt_generation=int(attempt.generation),
            root_session_ref=session.session_ref,
            native_session_ref=session.native_session_ref,
            runtime_binding=runtime_binding,
            runtime_binding_hash=run.runtime_binding_hash,
            fence_ref=fence.fence_ref,
            primary_invocation=primary_invocation,
            review_invocation=review_invocation,
            primary_draft=primary_draft,
            execution=execution,
            predecessor_execution=predecessor_execution,
            rejection_receipt=rejection_receipt,
            completion=completion,
        )

    def record_idea_primary_draft(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        native_session_ref: str,
        runtime_binding: IdeaRuntimeBinding,
        draft: dict[str, object],
        adapter_kind: str,
        idempotency_key: str,
    ) -> IdeaPrimaryDraft:
        """Bind the real native Session immediately after primary generation.

        Child-agent review is a later provider turn in this same native
        Session. Persisting this transport checkpoint prevents review failure
        or daemon restart from creating a second managed Session for the same
        Attempt/Fence.
        """

        _validate_stage_idempotency_key(idempotency_key)
        if (
            not native_session_ref
            or not isinstance(draft, dict)
            or not adapter_kind
            or len(adapter_kind) > 64
        ):
            raise OwnerConflict("idea_primary_draft_invalid")
        runtime_binding, _binding_json, runtime_binding_hash = (
            _validated_runtime_binding(runtime_binding)
        )
        draft_json = canonical_json(draft)
        draft_hash = canonical_hash(draft)
        provider_response_hash = _primary_provider_response_hash(
            native_session_ref=native_session_ref,
            draft=draft,
            adapter_kind=adapter_kind,
        )
        command_hash = canonical_hash(
            {
                "command": "record_idea_primary_draft",
                "run_ref": run_ref,
                "attempt_ref": attempt_ref,
                "fence_ref": fence_ref,
                "native_session_ref": native_session_ref,
                "runtime_binding_hash": runtime_binding_hash,
                "draft_hash": draft_hash,
                "adapter_kind": adapter_kind,
                "provider_response_hash": provider_response_hash,
            }
        )
        _query_stage_command(
            self._database,
            idempotency_key,
            "record_idea_primary_draft",
            command_hash,
        )
        with self._database.write() as connection:
            replay_ref = _stage_command_replay(
                connection,
                idempotency_key,
                "record_idea_primary_draft",
                command_hash,
            )
            run, attempt, session, fence = _load_stage_fence(
                connection, run_ref, attempt_ref, fence_ref
            )
            if (
                _runtime_binding_from_row(run) != runtime_binding
                or run.runtime_binding_hash != runtime_binding_hash
            ):
                raise OwnerConflict("idea_runtime_binding_drift")
            primary_invocation = _complete_provider_invocation(
                connection,
                run,
                attempt,
                fence,
                phase="primary",
                response_hash=provider_response_hash,
            )
            existing = _primary_draft(run, attempt, session)
            if replay_ref is not None:
                if replay_ref != attempt_ref or existing is None:
                    raise OwnerConflict("stage_command_result_missing")
            elif existing is not None:
                if (
                    existing.native_session_ref != native_session_ref
                    or existing.draft_hash != draft_hash
                    or existing.adapter_kind != adapter_kind
                    or existing.runtime_binding_hash != runtime_binding_hash
                ):
                    raise OwnerConflict("idea_primary_draft_conflict")
                _record_stage_command(
                    connection,
                    idempotency_key,
                    "record_idea_primary_draft",
                    command_hash,
                    attempt_ref,
                )
            else:
                _require_current_fence(run, attempt, fence, "running", "current")
                if native_session_ref == session.session_ref or (
                    session.native_session_ref not in {None, native_session_ref}
                ):
                    raise OwnerConflict("native_session_conflict")
                conflicting_session = connection.execute(
                    text(
                        "SELECT session_ref FROM ar_stage_sessions WHERE "
                        "native_session_ref = :native_session_ref AND "
                        "session_ref != :session_ref LIMIT 1"
                    ),
                    {
                        "native_session_ref": native_session_ref,
                        "session_ref": session.session_ref,
                    },
                ).first()
                if conflicting_session is not None:
                    raise OwnerConflict("native_session_conflict")
                now = time.time()
                try:
                    connection.execute(
                        text(
                            "UPDATE ar_stage_sessions SET native_session_ref = "
                            ":native_session_ref, updated_at = :updated_at WHERE "
                            "session_ref = :session_ref"
                        ),
                        {
                            "native_session_ref": native_session_ref,
                            "updated_at": now,
                            "session_ref": session.session_ref,
                        },
                    )
                except IntegrityError as error:
                    raise OwnerConflict("native_session_conflict") from error
                connection.execute(
                    text(
                        "UPDATE ar_stage_attempts SET primary_draft_json = "
                        ":draft_json, primary_draft_hash = :draft_hash, "
                        "primary_adapter_kind = :adapter_kind, "
                        "primary_recorded_at = :recorded_at WHERE attempt_ref = "
                        ":attempt_ref"
                    ),
                    {
                        "draft_json": draft_json,
                        "draft_hash": draft_hash,
                        "adapter_kind": adapter_kind,
                        "recorded_at": now,
                        "attempt_ref": attempt_ref,
                    },
                )
                _record_stage_command(
                    connection,
                    idempotency_key,
                    "record_idea_primary_draft",
                    command_hash,
                    attempt_ref,
                )
                connection.execute(
                    text(
                        "UPDATE agent_runtime_state SET revision = revision + 1 "
                        "WHERE singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "agent_runtime.primary_draft_recorded",
                    {
                        "request_ref": run.request_ref,
                        "run_ref": run_ref,
                        "attempt_ref": attempt_ref,
                        "fence_ref": fence_ref,
                        "native_session_ref": native_session_ref,
                        "runtime_binding_hash": runtime_binding_hash,
                        "draft_hash": draft_hash,
                        "invocation_ref": primary_invocation.invocation_ref,
                        "provider_response_hash": provider_response_hash,
                    },
                )
        current = self.query_idea_stage_run(run.request_ref)
        if current is None or current.primary_draft is None:
            raise OwnerConflict("idea_primary_draft_missing_after_commit")
        return current.primary_draft

    def record_idea_attempt_execution(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        submission_ref: str,
        native_session_ref: str,
        runtime_binding: IdeaRuntimeBinding,
        outcome: dict[str, object],
        review: dict[str, object],
        idempotency_key: str,
        reviewed_draft: dict[str, object] | None = None,
    ) -> AttemptExecution:
        _validate_stage_idempotency_key(idempotency_key)
        if not submission_ref or not native_session_ref:
            raise OwnerConflict("attempt_execution_identity_invalid")
        runtime_binding, _runtime_binding_json, runtime_binding_hash = (
            _validated_runtime_binding(runtime_binding)
        )
        reviewer_agent_ref = _validate_attempt_review_for_write(
            review, native_session_ref=native_session_ref
        )
        reviewed_draft = _resolved_reviewed_draft(
            outcome,
            review,
            reviewed_draft,
        )
        outcome_material_hash = material_outcome_hash(outcome)
        payload = {
            "schema_ref": ATTEMPT_EXECUTION_SCHEMA,
            "outcome": outcome,
            "reviewed_draft": reviewed_draft,
            "review": review,
        }
        payload_json = canonical_json(payload)
        payload_hash = canonical_hash(payload)
        provider_response_hash = _review_provider_response_hash(
            native_session_ref=native_session_ref,
            reviewed_draft=reviewed_draft,
            outcome=outcome,
            review=review,
        )
        command_hash = canonical_hash(
            {
                "command": "record_idea_attempt_execution",
                "run_ref": run_ref,
                "attempt_ref": attempt_ref,
                "fence_ref": fence_ref,
                "submission_ref": submission_ref,
                "native_session_ref": native_session_ref,
                "runtime_binding_hash": runtime_binding_hash,
                "payload_hash": payload_hash,
                "material_outcome_hash": outcome_material_hash,
                "provider_response_hash": provider_response_hash,
            }
        )
        _query_stage_command(
            self._database,
            idempotency_key,
            "record_idea_attempt_execution",
            command_hash,
        )
        with self._database.read() as connection:
            preview_run, preview_attempt, preview_session, preview_fence = (
                _load_stage_fence(connection, run_ref, attempt_ref, fence_ref)
            )
            if reviewer_agent_ref == preview_session.session_ref:
                raise OwnerConflict("attempt_review_independence_invalid")
            if (
                _runtime_binding_from_row(preview_run) != runtime_binding
                or preview_run.runtime_binding_hash != runtime_binding_hash
            ):
                raise OwnerConflict("idea_runtime_binding_drift")
            primary_draft = _primary_draft(
                preview_run, preview_attempt, preview_session
            )
            if primary_draft is None:
                raise OwnerConflict("idea_primary_draft_required")
            if (
                primary_draft.native_session_ref != native_session_ref
                or primary_draft.draft_hash != canonical_hash(reviewed_draft)
            ):
                raise OwnerConflict("idea_primary_draft_conflict")
            primary_invocation, review_invocation = _provider_invocations(
                connection, preview_run, preview_attempt, preview_fence
            )
            if (
                primary_invocation.status != "completed"
                or primary_invocation.response_hash
                != _primary_provider_response_hash(
                    native_session_ref=primary_draft.native_session_ref,
                    draft=primary_draft.draft,
                    adapter_kind=primary_draft.adapter_kind,
                )
                or review_invocation.status == "completed"
                and review_invocation.response_hash != provider_response_hash
            ):
                raise OwnerConflict("idea_provider_invocation_invalid")
            _, predecessor_receipt, predecessor_submission_ref = (
                _successor_execution_lineage(
                    connection,
                    preview_run,
                    preview_attempt,
                    preview_session,
                    native_session_ref=native_session_ref,
                    outcome=outcome,
                )
            )
        if predecessor_receipt is not None:
            if self._outcome_verifier is None:
                raise OwnerConflict("idea_outcome_verifier_unavailable")
            self._outcome_verifier.verify_idea_outcome_decision(
                request_ref=preview_run.request_ref,
                submission_ref=predecessor_submission_ref,
                decision="rejected",
                outcome_ref=None,
                receipt=predecessor_receipt,
            )
        with self._database.write() as connection:
            replay_ref = _stage_command_replay(
                connection,
                idempotency_key,
                "record_idea_attempt_execution",
                command_hash,
            )
            if replay_ref is not None:
                replay_submission_ref = replay_ref
            else:
                replay_submission_ref = None
            run, attempt, session, fence = _load_stage_fence(
                connection, run_ref, attempt_ref, fence_ref
            )
            if reviewer_agent_ref == session.session_ref:
                raise OwnerConflict("attempt_review_independence_invalid")
            if (
                _runtime_binding_from_row(run) != runtime_binding
                or run.runtime_binding_hash != runtime_binding_hash
            ):
                raise OwnerConflict("idea_runtime_binding_drift")
            current_primary_draft = _primary_draft(run, attempt, session)
            if current_primary_draft is None:
                raise OwnerConflict("idea_primary_draft_required")
            if (
                current_primary_draft.native_session_ref != native_session_ref
                or current_primary_draft.draft_hash
                != canonical_hash(reviewed_draft)
            ):
                raise OwnerConflict("idea_primary_draft_conflict")
            _complete_provider_invocation(
                connection,
                run,
                attempt,
                fence,
                phase="review",
                response_hash=provider_response_hash,
            )
            lineage, current_predecessor_receipt, current_predecessor_submission = (
                _successor_execution_lineage(
                    connection,
                    run,
                    attempt,
                    session,
                    native_session_ref=native_session_ref,
                    outcome=outcome,
                )
            )
            if (
                current_predecessor_receipt != predecessor_receipt
                or current_predecessor_submission != predecessor_submission_ref
            ):
                raise OwnerConflict("attempt_successor_lineage_changed")
            if replay_submission_ref is None and attempt.status != "running":
                if (
                    attempt.submission_ref == submission_ref
                    and attempt.payload_hash == payload_hash
                    and session.native_session_ref == native_session_ref
                ):
                    replay_submission_ref = submission_ref
                    _record_stage_command(
                        connection,
                        idempotency_key,
                        "record_idea_attempt_execution",
                        command_hash,
                        submission_ref,
                    )
                else:
                    raise OwnerConflict("attempt_fence_stale")
            if replay_submission_ref is None:
                _require_current_fence(run, attempt, fence, "running", "current")
                if session.native_session_ref not in {None, native_session_ref}:
                    raise OwnerConflict("native_session_conflict")
                conflicting_session = connection.execute(
                    text(
                        "SELECT session_ref FROM ar_stage_sessions WHERE "
                        "native_session_ref = :native_session_ref AND "
                        "session_ref != :session_ref LIMIT 1"
                    ),
                    {
                        "native_session_ref": native_session_ref,
                        "session_ref": session.session_ref,
                    },
                ).first()
                if conflicting_session is not None:
                    raise OwnerConflict("native_session_conflict")
                now = time.time()
                receipt_ref = new_ref("ar_execution_receipt")
                bindings = {
                    "request_ref": run.request_ref,
                    "run_ref": run_ref,
                    "attempt_ref": attempt_ref,
                    "fence_ref": fence_ref,
                    "submission_ref": submission_ref,
                    "native_session_ref": native_session_ref,
                    "runtime_binding_hash": run.runtime_binding_hash,
                    "payload_hash": payload_hash,
                    "material_outcome_hash": outcome_material_hash,
                    **lineage,
                }
                receipt_hash = _owner_receipt_hash(
                    ATTEMPT_EXECUTION_RECEIPT_KIND, submission_ref, bindings
                )
                try:
                    connection.execute(
                        text(
                            "UPDATE ar_stage_sessions SET native_session_ref = "
                            ":native_session_ref, updated_at = :updated_at WHERE "
                            "session_ref = :session_ref"
                        ),
                        {
                            "native_session_ref": native_session_ref,
                            "updated_at": now,
                            "session_ref": session.session_ref,
                        },
                    )
                except IntegrityError as error:
                    raise OwnerConflict("native_session_conflict") from error
                connection.execute(
                    text(
                        "UPDATE ar_stage_attempts SET status = 'executed', "
                        "submission_ref = :submission_ref, payload_json = "
                        ":payload_json, payload_hash = :payload_hash, "
                        "material_outcome_hash = :material_outcome_hash, "
                        "execution_receipt_ref = :receipt_ref, "
                        "execution_receipt_hash = :receipt_hash, "
                        "predecessor_outcome_hash = :predecessor_outcome_hash, "
                        "predecessor_material_outcome_hash = "
                        ":predecessor_material_outcome_hash, "
                        "predecessor_rejection_receipt_ref = "
                        ":predecessor_rejection_receipt_ref, "
                        "predecessor_rejection_receipt_subject_ref = "
                        ":predecessor_rejection_receipt_subject_ref, "
                        "predecessor_rejection_receipt_hash = "
                        ":predecessor_rejection_receipt_hash, executed_at = "
                        ":executed_at WHERE attempt_ref = :attempt_ref"
                    ),
                    {
                        "submission_ref": submission_ref,
                        "payload_json": payload_json,
                        "payload_hash": payload_hash,
                        "material_outcome_hash": outcome_material_hash,
                        "receipt_ref": receipt_ref,
                        "receipt_hash": receipt_hash,
                        **lineage,
                        "executed_at": now,
                        "attempt_ref": attempt_ref,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE ar_execution_fences SET status = 'submitted' "
                        "WHERE fence_ref = :fence_ref"
                    ),
                    {"fence_ref": fence_ref},
                )
                connection.execute(
                    text(
                        "UPDATE ar_stage_runs SET status = 'awaiting_acceptance', "
                        "updated_at = :updated_at WHERE run_ref = :run_ref"
                    ),
                    {"updated_at": now, "run_ref": run_ref},
                )
                _record_stage_command(
                    connection,
                    idempotency_key,
                    "record_idea_attempt_execution",
                    command_hash,
                    submission_ref,
                )
                connection.execute(
                    text(
                        "UPDATE agent_runtime_state SET revision = revision + 1 "
                        "WHERE singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "agent_runtime.attempt_executed",
                    {
                        "request_ref": run.request_ref,
                        "run_ref": run_ref,
                        "attempt_ref": attempt_ref,
                        "fence_ref": fence_ref,
                        "submission_ref": submission_ref,
                        "payload_hash": payload_hash,
                        "material_outcome_hash": outcome_material_hash,
                        "runtime_binding_hash": runtime_binding_hash,
                        "provider_response_hash": provider_response_hash,
                        "receipt_ref": receipt_ref,
                    },
                )
                replay_submission_ref = submission_ref
        executed = self.query_idea_attempt_execution(replay_submission_ref)
        if executed is None:
            raise OwnerConflict("attempt_execution_missing_after_commit")
        return executed

    def query_idea_attempt_execution(
        self, submission_ref: str
    ) -> AttemptExecution | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT a.*, r.request_ref, r.cycle_ref, r.epoch, "
                    "r.context_pack_ref, r.context_pack_hash, "
                    "r.runtime_binding_json, r.runtime_binding_hash, "
                    "r.stage, r.request_receipt_ref, r.request_receipt_hash, "
                    "r.admission_hash, "
                    "s.native_session_ref FROM ar_stage_attempts a "
                    "JOIN ar_stage_runs r ON r.run_ref = a.run_ref "
                    "JOIN ar_stage_sessions s ON s.session_ref = a.root_session_ref "
                    "WHERE a.submission_ref = :submission_ref"
                ),
                {"submission_ref": submission_ref},
            ).first()
            if row is None:
                return None
            executed = _attempt_execution(row, row, row)
            _verify_provider_execution_chain(connection, row, executed)
        self._receipt_verifier.verify_attempt_execution_receipt(
            request_ref=row.request_ref,
            run_ref=row.run_ref,
            attempt_ref=row.attempt_ref,
            fence_ref=row.fence_ref,
            submission_ref=row.submission_ref,
            payload_hash=row.payload_hash,
            receipt=executed.receipt,
        )
        return executed

    def continue_after_idea_rejection(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        decision_receipt: AcceptanceReceipt,
        idempotency_key: str,
    ) -> IdeaStageRun:
        _validate_stage_idempotency_key(idempotency_key)
        command_hash = canonical_hash(
            {
                "command": "continue_after_idea_rejection",
                "run_ref": run_ref,
                "attempt_ref": attempt_ref,
                "fence_ref": fence_ref,
                "decision_receipt": decision_receipt.as_public_dict(),
            }
        )
        replay = _query_stage_command(
            self._database,
            idempotency_key,
            "continue_after_idea_rejection",
            command_hash,
        )
        if replay is not None:
            return self._query_idea_run_by_ref(run_ref)
        if self._outcome_verifier is None:
            raise OwnerConflict("idea_outcome_verifier_unavailable")
        with self._database.read() as connection:
            run, attempt, _session, fence = _load_stage_fence(
                connection, run_ref, attempt_ref, fence_ref
            )
            _require_current_fence(run, attempt, fence, "executed", "submitted")
            request_ref = run.request_ref
            submission_ref = attempt.submission_ref
        self._outcome_verifier.verify_idea_outcome_decision(
            request_ref=request_ref,
            submission_ref=submission_ref,
            decision="rejected",
            outcome_ref=None,
            receipt=decision_receipt,
        )
        with self._database.write() as connection:
            replay_ref = _stage_command_replay(
                connection,
                idempotency_key,
                "continue_after_idea_rejection",
                command_hash,
            )
            if replay_ref is None:
                run, attempt, session, fence = _load_stage_fence(
                    connection, run_ref, attempt_ref, fence_ref
                )
                _require_current_fence(run, attempt, fence, "executed", "submitted")
                if session.native_session_ref is None:
                    raise OwnerConflict("native_session_missing")
                now = time.time()
                next_generation = int(attempt.generation) + 1
                successor_ref = new_ref("idea_attempt")
                successor_fence_ref = new_ref("idea_fence")
                connection.execute(
                    text(
                        "UPDATE ar_stage_attempts SET status = 'rejected', "
                        "decision_receipt_ref = :decision_receipt_ref, "
                        "decision_receipt_subject_ref = "
                        ":decision_receipt_subject_ref, "
                        "decision_receipt_hash = :decision_receipt_hash, "
                        "closed_at = :closed_at WHERE attempt_ref = :attempt_ref"
                    ),
                    {
                        "decision_receipt_ref": decision_receipt.receipt_ref,
                        "decision_receipt_subject_ref": decision_receipt.subject_ref,
                        "decision_receipt_hash": decision_receipt.payload_hash,
                        "closed_at": now,
                        "attempt_ref": attempt_ref,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE ar_execution_fences SET status = 'rejected', "
                        "closed_at = :closed_at WHERE fence_ref = :fence_ref"
                    ),
                    {"closed_at": now, "fence_ref": fence_ref},
                )
                connection.execute(
                    text(
                        "INSERT INTO ar_stage_attempts (attempt_ref, run_ref, "
                        "generation, root_session_ref, fence_ref, "
                        "predecessor_attempt_ref, status, created_at) VALUES "
                        "(:attempt_ref, :run_ref, :generation, :session_ref, "
                        ":fence_ref, :predecessor_attempt_ref, 'running', "
                        ":created_at)"
                    ),
                    {
                        "attempt_ref": successor_ref,
                        "run_ref": run_ref,
                        "generation": next_generation,
                        "session_ref": session.session_ref,
                        "fence_ref": successor_fence_ref,
                        "predecessor_attempt_ref": attempt_ref,
                        "created_at": now,
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO ar_execution_fences (fence_ref, run_ref, "
                        "attempt_ref, generation, status, issued_at) VALUES "
                        "(:fence_ref, :run_ref, :attempt_ref, :generation, "
                        "'current', :issued_at)"
                    ),
                    {
                        "fence_ref": successor_fence_ref,
                        "run_ref": run_ref,
                        "attempt_ref": successor_ref,
                        "generation": next_generation,
                        "issued_at": now,
                    },
                )
                _insert_provider_invocations(
                    connection,
                    request_ref=run.request_ref,
                    run_ref=run_ref,
                    attempt_ref=successor_ref,
                    generation=next_generation,
                    root_session_ref=session.session_ref,
                    fence_ref=successor_fence_ref,
                    context_pack_ref=run.context_pack_ref,
                    context_pack_hash=run.context_pack_hash,
                    runtime_binding_hash=run.runtime_binding_hash,
                    predecessor_attempt_ref=attempt_ref,
                    prepared_at=now,
                )
                connection.execute(
                    text(
                        "UPDATE ar_stage_runs SET status = 'running', "
                        "current_attempt_ref = :attempt_ref, current_fence_ref = "
                        ":fence_ref, updated_at = :updated_at WHERE run_ref = :run_ref"
                    ),
                    {
                        "attempt_ref": successor_ref,
                        "fence_ref": successor_fence_ref,
                        "updated_at": now,
                        "run_ref": run_ref,
                    },
                )
                _record_stage_command(
                    connection,
                    idempotency_key,
                    "continue_after_idea_rejection",
                    command_hash,
                    successor_ref,
                )
                connection.execute(
                    text(
                        "UPDATE agent_runtime_state SET revision = revision + 1, "
                        "attempt_count = attempt_count + 1 WHERE singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "agent_runtime.attempt_rejected",
                    {
                        "request_ref": run.request_ref,
                        "run_ref": run_ref,
                        "attempt_ref": attempt_ref,
                        "decision_receipt_ref": decision_receipt.receipt_ref,
                        "successor_attempt_ref": successor_ref,
                        "root_session_ref": session.session_ref,
                        "native_session_ref": session.native_session_ref,
                        "fence_ref": successor_fence_ref,
                    },
                )
        return self._query_idea_run_by_ref(run_ref)

    def complete_idea_run(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        outcome_ref: str,
        decision_receipt: AcceptanceReceipt,
        idempotency_key: str,
    ) -> RunCompletion:
        _validate_stage_idempotency_key(idempotency_key)
        if not outcome_ref:
            raise OwnerConflict("outcome_ref_invalid")
        command_hash = canonical_hash(
            {
                "command": "complete_idea_run",
                "run_ref": run_ref,
                "attempt_ref": attempt_ref,
                "fence_ref": fence_ref,
                "outcome_ref": outcome_ref,
                "decision_receipt": decision_receipt.as_public_dict(),
            }
        )
        replay = _query_stage_command(
            self._database,
            idempotency_key,
            "complete_idea_run",
            command_hash,
        )
        if replay is not None:
            completed = self.query_idea_run_completion(run_ref)
            if completed is None:
                raise OwnerConflict("run_completion_missing")
            return completed
        if self._outcome_verifier is None:
            raise OwnerConflict("idea_outcome_verifier_unavailable")
        with self._database.read() as connection:
            run, attempt, _session, fence = _load_stage_fence(
                connection, run_ref, attempt_ref, fence_ref
            )
            _require_current_fence(run, attempt, fence, "executed", "submitted")
            request_ref = run.request_ref
            submission_ref = attempt.submission_ref
        self._outcome_verifier.verify_idea_outcome_decision(
            request_ref=request_ref,
            submission_ref=submission_ref,
            decision="accepted",
            outcome_ref=outcome_ref,
            receipt=decision_receipt,
        )
        with self._database.write() as connection:
            replay_ref = _stage_command_replay(
                connection,
                idempotency_key,
                "complete_idea_run",
                command_hash,
            )
            if replay_ref is None:
                run, attempt, session, fence = _load_stage_fence(
                    connection, run_ref, attempt_ref, fence_ref
                )
                _require_current_fence(run, attempt, fence, "executed", "submitted")
                now = time.time()
                receipt_ref = new_ref("ar_completion_receipt")
                bindings = {
                    "request_ref": run.request_ref,
                    "run_ref": run_ref,
                    "attempt_ref": attempt_ref,
                    "runtime_binding_hash": run.runtime_binding_hash,
                    "outcome_ref": outcome_ref,
                    "decision_receipt_ref": decision_receipt.receipt_ref,
                    "decision_receipt_subject_ref": decision_receipt.subject_ref,
                    "decision_receipt_hash": decision_receipt.payload_hash,
                }
                receipt_hash = _owner_receipt_hash(
                    RUN_COMPLETION_RECEIPT_KIND, run_ref, bindings
                )
                connection.execute(
                    text(
                        "UPDATE ar_stage_attempts SET status = 'completed', "
                        "decision_receipt_ref = :decision_receipt_ref, "
                        "decision_receipt_subject_ref = "
                        ":decision_receipt_subject_ref, "
                        "decision_receipt_hash = :decision_receipt_hash, "
                        "closed_at = :closed_at WHERE attempt_ref = :attempt_ref"
                    ),
                    {
                        "decision_receipt_ref": decision_receipt.receipt_ref,
                        "decision_receipt_subject_ref": decision_receipt.subject_ref,
                        "decision_receipt_hash": decision_receipt.payload_hash,
                        "closed_at": now,
                        "attempt_ref": attempt_ref,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE ar_execution_fences SET status = 'completed', "
                        "closed_at = :closed_at WHERE fence_ref = :fence_ref"
                    ),
                    {"closed_at": now, "fence_ref": fence_ref},
                )
                connection.execute(
                    text(
                        "UPDATE ar_stage_sessions SET status = 'completed', "
                        "updated_at = :updated_at WHERE session_ref = :session_ref"
                    ),
                    {"updated_at": now, "session_ref": session.session_ref},
                )
                connection.execute(
                    text(
                        "UPDATE ar_stage_runs SET status = 'completed', "
                        "completion_receipt_ref = :receipt_ref, "
                        "completion_receipt_hash = :receipt_hash, outcome_ref = "
                        ":outcome_ref, updated_at = :updated_at WHERE run_ref = :run_ref"
                    ),
                    {
                        "receipt_ref": receipt_ref,
                        "receipt_hash": receipt_hash,
                        "outcome_ref": outcome_ref,
                        "updated_at": now,
                        "run_ref": run_ref,
                    },
                )
                _record_stage_command(
                    connection,
                    idempotency_key,
                    "complete_idea_run",
                    command_hash,
                    run_ref,
                )
                connection.execute(
                    text(
                        "UPDATE agent_runtime_state SET revision = revision + 1, "
                        "active_run_count = active_run_count - 1, "
                        "completed_run_count = completed_run_count + 1 "
                        "WHERE singleton = 'owner' AND active_run_count > 0"
                    )
                )
                self._feed.record(
                    connection,
                    "agent_runtime.stage_run_completed",
                    {
                        "request_ref": run.request_ref,
                        "run_ref": run_ref,
                        "attempt_ref": attempt_ref,
                        "outcome_ref": outcome_ref,
                        "decision_receipt_ref": decision_receipt.receipt_ref,
                        "receipt_ref": receipt_ref,
                    },
                )
        completed = self.query_idea_run_completion(run_ref)
        if completed is None:
            raise OwnerConflict("run_completion_missing_after_commit")
        return completed

    def query_idea_run_completion(self, run_ref: str) -> RunCompletion | None:
        with self._database.read() as connection:
            run = connection.execute(
                text("SELECT * FROM ar_stage_runs WHERE run_ref = :run_ref"),
                {"run_ref": run_ref},
            ).first()
            if run is None or run.status != "completed":
                return None
            attempt = connection.execute(
                text(
                    "SELECT * FROM ar_stage_attempts WHERE attempt_ref = "
                    ":attempt_ref"
                ),
                {"attempt_ref": run.current_attempt_ref},
            ).first()
        if attempt is None:
            raise OwnerConflict("run_completion_invalid")
        completed = _run_completion(run, attempt)
        self._receipt_verifier.verify_run_completion_receipt(
            request_ref=run.request_ref,
            run_ref=run.run_ref,
            attempt_ref=attempt.attempt_ref,
            outcome_ref=completed.outcome_ref,
            receipt=completed.receipt,
        )
        return completed

    def _query_idea_run_by_ref(self, run_ref: str) -> IdeaStageRun:
        with self._database.read() as connection:
            row = connection.execute(
                text("SELECT * FROM ar_stage_runs WHERE run_ref = :run_ref"),
                {"run_ref": run_ref},
            ).first()
        if row is None:
            raise OwnerConflict("stage_run_not_found")
        return self._idea_stage_run_from_row(row)

    def verify_attempt_execution_receipt(self, **values) -> None:
        self._receipt_verifier.verify_attempt_execution_receipt(**values)

    def verify_run_completion_receipt(self, **values) -> None:
        self._receipt_verifier.verify_run_completion_receipt(**values)

    def verify_deepfetch_execution_receipt(self, **values) -> None:
        self._receipt_verifier.verify_deepfetch_execution_receipt(**values)


class SQLiteAgentRuntimeReceiptVerifier:
    """Narrow AR issuer verifier with optional AE provenance verification."""

    def __init__(
        self,
        database: Database,
        stage_request_verifier: StageRunRequestVerifier | None = None,
    ) -> None:
        self._database = database
        self._stage_request_verifier = stage_request_verifier

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
    ) -> None:
        if (
            receipt.issuer != AR_OWNER
            or receipt.kind != ATTEMPT_EXECUTION_RECEIPT_KIND
            or receipt.subject_ref != submission_ref
        ):
            raise OwnerConflict("attempt_execution_receipt_issuer_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT a.*, r.request_ref, r.cycle_ref, r.epoch, "
                    "r.context_pack_ref, r.context_pack_hash, "
                    "r.runtime_binding_json, r.runtime_binding_hash, "
                    "r.stage, r.request_receipt_ref, r.request_receipt_hash, "
                    "r.admission_hash, "
                    "s.native_session_ref FROM ar_stage_attempts a "
                    "JOIN ar_stage_runs r ON r.run_ref = a.run_ref "
                    "JOIN ar_stage_sessions s ON s.session_ref = a.root_session_ref "
                    "WHERE a.execution_receipt_ref = :receipt_ref"
                ),
                {"receipt_ref": receipt.receipt_ref},
            ).first()
            if row is not None:
                executed = _attempt_execution(row, row, row)
                _verify_provider_execution_chain(connection, row, executed)
        if row is None or (
            row.request_ref != request_ref
            or row.run_ref != run_ref
            or row.attempt_ref != attempt_ref
            or row.fence_ref != fence_ref
            or row.submission_ref != submission_ref
            or row.payload_hash != payload_hash
            or row.execution_receipt_hash != receipt.payload_hash
        ):
            raise OwnerConflict("attempt_execution_receipt_invalid")
        outcome = executed.outcome
        _verify_persisted_successor_lineage(self._database, row, outcome)
        if self._stage_request_verifier is not None:
            self._stage_request_verifier.verify_stage_run_request(
                request_ref=row.request_ref,
                cycle_ref=row.cycle_ref,
                epoch=int(row.epoch),
                context_pack_ref=row.context_pack_ref,
                context_pack_hash=row.context_pack_hash,
                receipt=AcceptanceReceipt(
                    issuer="advancement_engine",
                    kind="stage_run_request",
                    receipt_ref=row.request_receipt_ref,
                    subject_ref=row.request_ref,
                    payload_hash=row.request_receipt_hash,
                ),
            )

    def verify_run_completion_receipt(
        self,
        *,
        request_ref: str,
        run_ref: str,
        attempt_ref: str | None,
        outcome_ref: str,
        receipt: AcceptanceReceipt,
    ) -> None:
        if (
            receipt.issuer != AR_OWNER
            or receipt.kind != RUN_COMPLETION_RECEIPT_KIND
            or receipt.subject_ref != run_ref
        ):
            raise OwnerConflict("run_completion_receipt_issuer_invalid")
        with self._database.read() as connection:
            run = connection.execute(
                text(
                    "SELECT * FROM ar_stage_runs WHERE completion_receipt_ref = "
                    ":receipt_ref"
                ),
                {"receipt_ref": receipt.receipt_ref},
            ).first()
            if run is None:
                raise OwnerConflict("run_completion_receipt_invalid")
            attempt = connection.execute(
                text(
                    "SELECT * FROM ar_stage_attempts WHERE attempt_ref = "
                    ":attempt_ref"
                ),
                {"attempt_ref": run.current_attempt_ref},
            ).first()
        if attempt is None or (
            run.request_ref != request_ref
            or run.run_ref != run_ref
            or (attempt_ref is not None and attempt.attempt_ref != attempt_ref)
            or run.outcome_ref != outcome_ref
            or run.status != "completed"
            or attempt.status != "completed"
            or attempt.decision_receipt_subject_ref != run.outcome_ref
            or run.completion_receipt_hash != receipt.payload_hash
            or run.completion_receipt_hash != _run_completion_receipt_hash(run, attempt)
        ):
            raise OwnerConflict("run_completion_receipt_invalid")
        _runtime_binding_from_row(run)
        if self._stage_request_verifier is not None:
            self._stage_request_verifier.verify_stage_run_request(
                request_ref=run.request_ref,
                cycle_ref=run.cycle_ref,
                epoch=int(run.epoch),
                context_pack_ref=run.context_pack_ref,
                context_pack_hash=run.context_pack_hash,
                receipt=AcceptanceReceipt(
                    issuer="advancement_engine",
                    kind="stage_run_request",
                    receipt_ref=run.request_receipt_ref,
                    subject_ref=run.request_ref,
                    payload_hash=run.request_receipt_hash,
                ),
            )

    def verify_deepfetch_execution_receipt(
        self,
        *,
        request_ref: str,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        result_hash: str,
        receipt: AcceptanceReceipt,
    ) -> None:
        if (
            receipt.issuer != AR_OWNER
            or receipt.kind != DEEPFETCH_EXECUTION_RECEIPT_KIND
            or receipt.subject_ref != run_ref
        ):
            raise OwnerConflict("deepfetch_execution_receipt_issuer_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT r.*, s.root_session_ref, s.native_session_ref, "
                    "a.attempt_ref AS joined_attempt_ref, "
                    "a.generation AS joined_generation, a.fence_ref, "
                    "a.status AS attempt_status, a.result_hash AS "
                    "attempt_result_hash FROM ar_deepfetch_runs r JOIN "
                    "ar_deepfetch_sessions s ON s.run_ref = r.run_ref JOIN "
                    "ar_deepfetch_attempts a ON a.attempt_ref = "
                    "r.current_attempt_ref WHERE r.execution_receipt_ref = "
                    ":receipt_ref"
                ),
                {"receipt_ref": receipt.receipt_ref},
            ).first()
        if row is None:
            raise OwnerConflict("deepfetch_execution_receipt_invalid")
        run = _deepfetch_run_from_row(row)
        expected_hash = _owner_receipt_hash(
            DEEPFETCH_EXECUTION_RECEIPT_KIND,
            run.run_ref,
            {
                "request_ref": run.request_ref,
                "run_ref": run.run_ref,
                "attempt_ref": run.attempt_ref,
                "attempt_generation": run.attempt_generation,
                "fence_ref": run.fence_ref,
                "native_session_ref": run.native_session_ref,
                "runtime_binding_hash": run.runtime_binding_hash,
                "result_hash": run.result_hash,
            },
        )
        if (
            run.status != "executed"
            or row.attempt_status != "executed"
            or run.request_ref != request_ref
            or run.run_ref != run_ref
            or run.attempt_ref != attempt_ref
            or run.fence_ref != fence_ref
            or run.result_hash != result_hash
            or row.attempt_result_hash != result_hash
            or row.execution_receipt_hash != receipt.payload_hash
            or receipt.payload_hash != expected_hash
        ):
            raise OwnerConflict("deepfetch_execution_receipt_invalid")


def _deepfetch_runtime_binding(value: str) -> DeepFetchRuntimeBinding:
    try:
        decoded = decoded_object(value)
        if set(decoded) != {
            "schema_ref",
            "provider_ref",
            "provider_version",
            "model_ref",
            "harness_ref",
            "capability_bindings",
        } or decoded["schema_ref"] != "meta-research/deepfetch-runtime-binding/v1":
            raise TypeError("runtime binding")
        capabilities = decoded["capability_bindings"]
        if not isinstance(capabilities, list) or any(
            not isinstance(item, str) for item in capabilities
        ):
            raise TypeError("capability bindings")
        binding = DeepFetchRuntimeBinding(
            provider_ref=str(decoded["provider_ref"]),
            provider_version=str(decoded["provider_version"]),
            model_ref=str(decoded["model_ref"]),
            harness_ref=str(decoded["harness_ref"]),
            capability_bindings=tuple(capabilities),
        )
        validate_runtime_binding(binding)
        return binding
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, DeepFetchUnavailable) as error:
        raise OwnerConflict("deepfetch_runtime_binding_invalid") from error


def _deepfetch_run_from_row(row) -> DeepFetchRun:
    runtime_binding = _deepfetch_runtime_binding(row.runtime_binding_json)
    if (
        canonical_json(runtime_binding.as_dict()) != row.runtime_binding_json
        or canonical_hash(runtime_binding.as_dict()) != row.runtime_binding_hash
    ):
        raise OwnerConflict("deepfetch_runtime_binding_invalid")
    result: dict[str, object] | None = None
    receipt: AcceptanceReceipt | None = None
    if row.result_json is not None:
        try:
            result = decoded_object(row.result_json)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise OwnerConflict("deepfetch_result_invalid") from error
        if (
            canonical_json(result) != row.result_json
            or canonical_hash(result) != row.result_hash
        ):
            raise OwnerConflict("deepfetch_result_invalid")
    if row.execution_receipt_ref is not None:
        receipt = AcceptanceReceipt(
            issuer=AR_OWNER,
            kind=DEEPFETCH_EXECUTION_RECEIPT_KIND,
            receipt_ref=row.execution_receipt_ref,
            subject_ref=row.run_ref,
            payload_hash=row.execution_receipt_hash,
        )
    attempt_ref = getattr(row, "joined_attempt_ref", None)
    fence_ref = getattr(row, "fence_ref", None)
    if row.status in {"running", "executed"} and (
        attempt_ref is None or fence_ref is None
    ):
        raise OwnerConflict("deepfetch_attempt_invalid")
    return DeepFetchRun(
        request_ref=row.request_ref,
        run_ref=row.run_ref,
        correlation_ref=row.correlation_ref,
        status=row.status,
        attempt_ref=attempt_ref,
        attempt_generation=int(row.attempt_generation),
        root_session_ref=row.root_session_ref,
        native_session_ref=row.native_session_ref,
        fence_ref=fence_ref,
        runtime_binding=runtime_binding,
        runtime_binding_hash=row.runtime_binding_hash,
        result=result,
        result_hash=row.result_hash,
        execution_receipt=receipt,
        failure_code=row.failure_code,
    )


def _owner_receipt_hash(
    kind: str, subject_ref: str, bindings: dict[str, object]
) -> str:
    return canonical_hash(
        {
            "schema_ref": RECEIPT_SCHEMA,
            "issuer": AR_OWNER,
            "kind": kind,
            "subject_ref": subject_ref,
            "bindings": bindings,
        }
    )


def _execution_bindings(row) -> dict[str, object]:
    return {
        "request_ref": row.request_ref,
        "run_ref": row.run_ref,
        "attempt_ref": row.attempt_ref,
        "fence_ref": row.fence_ref,
        "submission_ref": row.submission_ref,
        "native_session_ref": row.native_session_ref,
        "runtime_binding_hash": row.runtime_binding_hash,
        "payload_hash": row.payload_hash,
        "material_outcome_hash": row.material_outcome_hash,
        "predecessor_attempt_ref": row.predecessor_attempt_ref,
        "predecessor_outcome_hash": row.predecessor_outcome_hash,
        "predecessor_material_outcome_hash": (
            row.predecessor_material_outcome_hash
        ),
        "predecessor_rejection_receipt_ref": (
            row.predecessor_rejection_receipt_ref
        ),
        "predecessor_rejection_receipt_subject_ref": (
            row.predecessor_rejection_receipt_subject_ref
        ),
        "predecessor_rejection_receipt_hash": (
            row.predecessor_rejection_receipt_hash
        ),
    }


def _verify_execution_row(
    row,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    _runtime_binding_from_row(row)
    try:
        payload = decoded_object(row.payload_json)
        outcome = payload["outcome"]
        reviewed_draft = payload["reviewed_draft"]
        review = payload["review"]
        if (
            set(payload)
            != {"schema_ref", "outcome", "reviewed_draft", "review"}
            or payload["schema_ref"] != ATTEMPT_EXECUTION_SCHEMA
            or not isinstance(outcome, dict)
            or not isinstance(reviewed_draft, dict)
            or not isinstance(review, dict)
        ):
            raise TypeError("payload")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise OwnerConflict("attempt_execution_payload_invalid") from error
    predecessor_values = (
        row.predecessor_outcome_hash,
        row.predecessor_material_outcome_hash,
        row.predecessor_rejection_receipt_ref,
        row.predecessor_rejection_receipt_subject_ref,
        row.predecessor_rejection_receipt_hash,
    )
    lineage_invalid = (
        row.predecessor_attempt_ref is None
        and (int(row.generation) != 1 or any(value is not None for value in predecessor_values))
    ) or (
        row.predecessor_attempt_ref is not None
        and (
            int(row.generation) <= 1
            or any(not isinstance(value, str) or not value for value in predecessor_values)
        )
    )
    if (
        not row.native_session_ref
        or lineage_invalid
        or canonical_json(payload) != row.payload_json
        or canonical_hash(payload) != row.payload_hash
        or material_outcome_hash(outcome) != row.material_outcome_hash
        or row.execution_receipt_hash
        != _owner_receipt_hash(
            ATTEMPT_EXECUTION_RECEIPT_KIND,
            row.submission_ref,
            _execution_bindings(row),
        )
    ):
        raise OwnerConflict("attempt_execution_payload_invalid")
    return outcome, reviewed_draft, review


def _primary_draft(run, attempt, session) -> IdeaPrimaryDraft | None:
    values = (
        attempt.primary_draft_json,
        attempt.primary_draft_hash,
        attempt.primary_adapter_kind,
        attempt.primary_recorded_at,
    )
    if all(value is None for value in values):
        return None
    if any(value is None for value in values) or session.native_session_ref is None:
        raise OwnerConflict("idea_primary_draft_invalid")
    try:
        draft = decoded_object(attempt.primary_draft_json)
    except (TypeError, ValueError) as error:
        raise OwnerConflict("idea_primary_draft_invalid") from error
    if (
        canonical_json(draft) != attempt.primary_draft_json
        or canonical_hash(draft) != attempt.primary_draft_hash
        or not isinstance(attempt.primary_adapter_kind, str)
        or not attempt.primary_adapter_kind
        or len(attempt.primary_adapter_kind) > 64
    ):
        raise OwnerConflict("idea_primary_draft_invalid")
    return IdeaPrimaryDraft(
        request_ref=run.request_ref,
        run_ref=run.run_ref,
        attempt_ref=attempt.attempt_ref,
        fence_ref=attempt.fence_ref,
        native_session_ref=session.native_session_ref,
        runtime_binding_hash=run.runtime_binding_hash,
        draft=draft,
        draft_hash=attempt.primary_draft_hash,
        adapter_kind=attempt.primary_adapter_kind,
    )


def _provider_invocation_request_hash(
    *,
    invocation_ref: str,
    phase: str,
    request_ref: str,
    run_ref: str,
    attempt_ref: str,
    generation: int,
    root_session_ref: str,
    fence_ref: str,
    context_pack_ref: str,
    context_pack_hash: str,
    runtime_binding_hash: str,
    predecessor_attempt_ref: str | None,
) -> str:
    return canonical_hash(
        {
            "schema_ref": "meta-research/idea-provider-invocation/v1",
            "invocation_ref": invocation_ref,
            "phase": phase,
            "request_ref": request_ref,
            "run_ref": run_ref,
            "attempt_ref": attempt_ref,
            "attempt_generation": generation,
            "root_session_ref": root_session_ref,
            "fence_ref": fence_ref,
            "context_pack_ref": context_pack_ref,
            "context_pack_hash": context_pack_hash,
            "runtime_binding_hash": runtime_binding_hash,
            "predecessor_attempt_ref": predecessor_attempt_ref,
        }
    )


def _primary_provider_response_hash(
    *,
    native_session_ref: str,
    draft: dict[str, object],
    adapter_kind: str,
) -> str:
    return canonical_hash(
        {
            "schema_ref": "meta-research/idea-primary-provider-response/v1",
            "native_session_ref": native_session_ref,
            "draft": draft,
            "adapter_kind": adapter_kind,
        }
    )


def _review_provider_response_hash(
    *,
    native_session_ref: str,
    reviewed_draft: dict[str, object],
    outcome: dict[str, object],
    review: dict[str, object],
) -> str:
    return canonical_hash(
        {
            "schema_ref": "meta-research/idea-review-provider-response/v1",
            "native_session_ref": native_session_ref,
            "reviewed_draft": reviewed_draft,
            "outcome": outcome,
            "review": review,
        }
    )


def _insert_provider_invocations(
    connection,
    *,
    request_ref: str,
    run_ref: str,
    attempt_ref: str,
    generation: int,
    root_session_ref: str,
    fence_ref: str,
    context_pack_ref: str,
    context_pack_hash: str,
    runtime_binding_hash: str,
    predecessor_attempt_ref: str | None,
    prepared_at: float,
) -> None:
    for phase in ("primary", "review"):
        invocation_ref = new_ref(f"idea_{phase}_invocation")
        connection.execute(
            text(
                "INSERT INTO ar_idea_provider_invocations (invocation_ref, "
                "run_ref, attempt_ref, fence_ref, phase, request_hash, "
                "runtime_binding_hash, status, prepared_at) VALUES "
                "(:invocation_ref, :run_ref, :attempt_ref, :fence_ref, :phase, "
                ":request_hash, :runtime_binding_hash, 'prepared', :prepared_at)"
            ),
            {
                "invocation_ref": invocation_ref,
                "run_ref": run_ref,
                "attempt_ref": attempt_ref,
                "fence_ref": fence_ref,
                "phase": phase,
                "request_hash": _provider_invocation_request_hash(
                    invocation_ref=invocation_ref,
                    phase=phase,
                    request_ref=request_ref,
                    run_ref=run_ref,
                    attempt_ref=attempt_ref,
                    generation=generation,
                    root_session_ref=root_session_ref,
                    fence_ref=fence_ref,
                    context_pack_ref=context_pack_ref,
                    context_pack_hash=context_pack_hash,
                    runtime_binding_hash=runtime_binding_hash,
                    predecessor_attempt_ref=predecessor_attempt_ref,
                ),
                "runtime_binding_hash": runtime_binding_hash,
                "prepared_at": prepared_at,
            },
        )


def _provider_invocations(
    connection, run, attempt, fence
) -> tuple[IdeaProviderInvocation, IdeaProviderInvocation]:
    rows = connection.execute(
        text(
            "SELECT * FROM ar_idea_provider_invocations WHERE "
            "attempt_ref = :attempt_ref ORDER BY phase"
        ),
        {"attempt_ref": attempt.attempt_ref},
    ).all()
    by_phase = {row.phase: row for row in rows}
    if len(rows) != 2 or set(by_phase) != {"primary", "review"}:
        raise OwnerConflict("idea_provider_invocation_invalid")
    values: list[IdeaProviderInvocation] = []
    for phase in ("primary", "review"):
        row = by_phase[phase]
        expected_hash = _provider_invocation_request_hash(
            invocation_ref=row.invocation_ref,
            phase=phase,
            request_ref=run.request_ref,
            run_ref=run.run_ref,
            attempt_ref=attempt.attempt_ref,
            generation=int(attempt.generation),
            root_session_ref=attempt.root_session_ref,
            fence_ref=fence.fence_ref,
            context_pack_ref=run.context_pack_ref,
            context_pack_hash=run.context_pack_hash,
            runtime_binding_hash=run.runtime_binding_hash,
            predecessor_attempt_ref=attempt.predecessor_attempt_ref,
        )
        if (
            row.run_ref != run.run_ref
            or row.fence_ref != fence.fence_ref
            or row.request_hash != expected_hash
            or row.runtime_binding_hash != run.runtime_binding_hash
            or row.status not in {"prepared", "completed"}
            or row.status == "prepared"
            and (row.response_hash is not None or row.completed_at is not None)
            or row.status == "completed"
            and (not _is_sha256(row.response_hash) or row.completed_at is None)
        ):
            raise OwnerConflict("idea_provider_invocation_invalid")
        values.append(
            IdeaProviderInvocation(
                invocation_ref=row.invocation_ref,
                request_ref=run.request_ref,
                run_ref=run.run_ref,
                attempt_ref=attempt.attempt_ref,
                fence_ref=fence.fence_ref,
                phase=phase,
                request_hash=row.request_hash,
                runtime_binding_hash=row.runtime_binding_hash,
                status=row.status,
                response_hash=row.response_hash,
            )
        )
    return values[0], values[1]


def _complete_provider_invocation(
    connection,
    run,
    attempt,
    fence,
    *,
    phase: str,
    response_hash: str,
) -> IdeaProviderInvocation:
    primary, review = _provider_invocations(connection, run, attempt, fence)
    invocation = primary if phase == "primary" else review
    if invocation.status == "completed":
        if invocation.response_hash != response_hash:
            raise OwnerConflict("idea_provider_response_conflict")
        return invocation
    now = time.time()
    connection.execute(
        text(
            "UPDATE ar_idea_provider_invocations SET status = 'completed', "
            "response_hash = :response_hash, completed_at = :completed_at WHERE "
            "invocation_ref = :invocation_ref AND status = 'prepared'"
        ),
        {
            "response_hash": response_hash,
            "completed_at": now,
            "invocation_ref": invocation.invocation_ref,
        },
    )
    return replace(
        invocation,
        status="completed",
        response_hash=response_hash,
    )


def _verify_provider_execution_chain(
    connection, row, execution: AttemptExecution
) -> None:
    primary, review = _provider_invocations(connection, row, row, row)
    checkpoint = _primary_draft(row, row, row)
    if checkpoint is None:
        raise OwnerConflict("idea_primary_draft_required")
    if (
        primary.status != "completed"
        or primary.response_hash
        != _primary_provider_response_hash(
            native_session_ref=checkpoint.native_session_ref,
            draft=checkpoint.draft,
            adapter_kind=checkpoint.adapter_kind,
        )
        or review.status != "completed"
        or review.response_hash
        != _review_provider_response_hash(
            native_session_ref=execution.native_session_ref,
            reviewed_draft=execution.reviewed_draft,
            outcome=execution.outcome,
            review=execution.review,
        )
    ):
        raise OwnerConflict("idea_provider_invocation_invalid")


def _attempt_execution(run, attempt, session) -> AttemptExecution:
    row = (
        _ExecutionRow(run, attempt, session)
        if run is not attempt or attempt is not session
        else attempt
    )
    outcome, reviewed_draft, review = _verify_execution_row(row)
    runtime_binding = _runtime_binding_from_row(row)
    return AttemptExecution(
        request_ref=row.request_ref,
        run_ref=row.run_ref,
        attempt_ref=row.attempt_ref,
        fence_ref=row.fence_ref,
        submission_ref=row.submission_ref,
        native_session_ref=row.native_session_ref,
        runtime_binding=runtime_binding,
        runtime_binding_hash=row.runtime_binding_hash,
        payload_hash=row.payload_hash,
        payload_json=row.payload_json,
        material_outcome_hash=row.material_outcome_hash,
        outcome=outcome,
        reviewed_draft=reviewed_draft,
        reviewed_draft_hash=canonical_hash(reviewed_draft),
        review=review,
        receipt=AcceptanceReceipt(
            issuer=AR_OWNER,
            kind=ATTEMPT_EXECUTION_RECEIPT_KIND,
            receipt_ref=row.execution_receipt_ref,
            subject_ref=row.submission_ref,
            payload_hash=row.execution_receipt_hash,
        ),
        predecessor_attempt_ref=row.predecessor_attempt_ref,
        predecessor_outcome_hash=row.predecessor_outcome_hash,
        predecessor_material_outcome_hash=(
            row.predecessor_material_outcome_hash
        ),
        predecessor_rejection_receipt=(
            None
            if row.predecessor_attempt_ref is None
            else AcceptanceReceipt(
                issuer="research_graph",
                kind="idea_outcome_rejected",
                receipt_ref=row.predecessor_rejection_receipt_ref,
                subject_ref=row.predecessor_rejection_receipt_subject_ref,
                payload_hash=row.predecessor_rejection_receipt_hash,
            )
        ),
    )


class _ExecutionRow:
    """Small adapter over separately queried run/attempt/session rows."""

    def __init__(self, run, attempt, session) -> None:
        for name in (
            "attempt_ref",
            "generation",
            "fence_ref",
            "predecessor_attempt_ref",
            "predecessor_outcome_hash",
            "predecessor_material_outcome_hash",
            "predecessor_rejection_receipt_ref",
            "predecessor_rejection_receipt_subject_ref",
            "predecessor_rejection_receipt_hash",
            "submission_ref",
            "payload_json",
            "payload_hash",
            "material_outcome_hash",
            "execution_receipt_ref",
            "execution_receipt_hash",
        ):
            setattr(self, name, getattr(attempt, name))
        self.run_ref = attempt.run_ref
        self.request_ref = run.request_ref
        self.native_session_ref = session.native_session_ref
        for name in (
            "cycle_ref",
            "stage",
            "epoch",
            "context_pack_ref",
            "context_pack_hash",
            "runtime_binding_json",
            "runtime_binding_hash",
            "request_receipt_ref",
            "request_receipt_hash",
            "admission_hash",
        ):
            setattr(self, name, getattr(run, name))


def _successor_execution_lineage(
    connection,
    run,
    attempt,
    session,
    *,
    native_session_ref: str,
    outcome: dict[str, object],
) -> tuple[dict[str, object], AcceptanceReceipt | None, str | None]:
    empty = {
        "predecessor_attempt_ref": None,
        "predecessor_outcome_hash": None,
        "predecessor_material_outcome_hash": None,
        "predecessor_rejection_receipt_ref": None,
        "predecessor_rejection_receipt_subject_ref": None,
        "predecessor_rejection_receipt_hash": None,
    }
    if attempt.predecessor_attempt_ref is None:
        if int(attempt.generation) != 1 or any(
            getattr(attempt, key) is not None
            for key in empty
            if key != "predecessor_attempt_ref"
        ):
            raise OwnerConflict("attempt_successor_lineage_invalid")
        return empty, None, None

    predecessor = connection.execute(
        text(
            "SELECT * FROM ar_stage_attempts WHERE attempt_ref = :attempt_ref "
            "AND run_ref = :run_ref"
        ),
        {
            "attempt_ref": attempt.predecessor_attempt_ref,
            "run_ref": run.run_ref,
        },
    ).first()
    if predecessor is None or (
        predecessor.status != "rejected"
        or predecessor.run_ref != attempt.run_ref
        or predecessor.root_session_ref != attempt.root_session_ref
        or attempt.root_session_ref != session.session_ref
        or int(attempt.generation) != int(predecessor.generation) + 1
        or session.native_session_ref != native_session_ref
        or predecessor.submission_ref is None
        or predecessor.decision_receipt_ref is None
        or predecessor.decision_receipt_subject_ref is None
        or predecessor.decision_receipt_hash is None
    ):
        raise OwnerConflict("attempt_successor_lineage_invalid")
    predecessor_execution = _attempt_execution(run, predecessor, session)
    predecessor_outcome_hash = canonical_hash(predecessor_execution.outcome)
    predecessor_material_hash = predecessor_execution.material_outcome_hash
    if material_outcome_hash(outcome) == predecessor_material_hash:
        raise OwnerConflict("attempt_successor_outcome_unchanged")
    receipt = AcceptanceReceipt(
        issuer="research_graph",
        kind="idea_outcome_rejected",
        receipt_ref=predecessor.decision_receipt_ref,
        subject_ref=predecessor.decision_receipt_subject_ref,
        payload_hash=predecessor.decision_receipt_hash,
    )
    lineage = {
        "predecessor_attempt_ref": predecessor.attempt_ref,
        "predecessor_outcome_hash": predecessor_outcome_hash,
        "predecessor_material_outcome_hash": predecessor_material_hash,
        "predecessor_rejection_receipt_ref": receipt.receipt_ref,
        "predecessor_rejection_receipt_subject_ref": receipt.subject_ref,
        "predecessor_rejection_receipt_hash": receipt.payload_hash,
    }
    stored = {
        key: getattr(attempt, key)
        for key in lineage
        if key != "predecessor_attempt_ref"
    }
    if attempt.status == "running":
        if any(value is not None for value in stored.values()):
            raise OwnerConflict("attempt_successor_lineage_invalid")
    elif any(stored[key] != lineage[key] for key in stored):
        raise OwnerConflict("attempt_successor_lineage_invalid")
    return lineage, receipt, predecessor.submission_ref


def _verify_persisted_successor_lineage(
    database: Database,
    row,
    outcome: dict[str, object],
) -> None:
    if row.predecessor_attempt_ref is None:
        return
    with database.read() as connection:
        predecessor = connection.execute(
            text(
                "SELECT p.*, r.request_ref, r.cycle_ref, r.stage, r.epoch, "
                "r.context_pack_ref, r.context_pack_hash, "
                "r.runtime_binding_json, r.runtime_binding_hash, "
                "r.request_receipt_ref, r.request_receipt_hash, "
                "r.admission_hash, s.native_session_ref FROM "
                "ar_stage_attempts p JOIN ar_stage_runs r ON r.run_ref = p.run_ref "
                "JOIN ar_stage_sessions s ON s.session_ref = p.root_session_ref "
                "WHERE p.attempt_ref = :attempt_ref"
            ),
            {"attempt_ref": row.predecessor_attempt_ref},
        ).first()
    if predecessor is None:
        raise OwnerConflict("attempt_successor_lineage_invalid")
    predecessor_outcome, _predecessor_draft, _predecessor_review = (
        _verify_execution_row(predecessor)
    )
    if (
        predecessor.status != "rejected"
        or predecessor.run_ref != row.run_ref
        or predecessor.root_session_ref != row.root_session_ref
        or predecessor.native_session_ref != row.native_session_ref
        or int(row.generation) != int(predecessor.generation) + 1
        or canonical_hash(predecessor_outcome) != row.predecessor_outcome_hash
        or material_outcome_hash(predecessor_outcome)
        != row.predecessor_material_outcome_hash
        or material_outcome_hash(outcome)
        == row.predecessor_material_outcome_hash
        or predecessor.decision_receipt_ref
        != row.predecessor_rejection_receipt_ref
        or predecessor.decision_receipt_subject_ref
        != row.predecessor_rejection_receipt_subject_ref
        or predecessor.decision_receipt_hash
        != row.predecessor_rejection_receipt_hash
    ):
        raise OwnerConflict("attempt_successor_lineage_invalid")


def _run_completion_bindings(run, attempt) -> dict[str, object]:
    return {
        "request_ref": run.request_ref,
        "run_ref": run.run_ref,
        "attempt_ref": attempt.attempt_ref,
        "runtime_binding_hash": run.runtime_binding_hash,
        "outcome_ref": run.outcome_ref,
        "decision_receipt_ref": attempt.decision_receipt_ref,
        "decision_receipt_subject_ref": attempt.decision_receipt_subject_ref,
        "decision_receipt_hash": attempt.decision_receipt_hash,
    }


def _run_completion_receipt_hash(run, attempt) -> str:
    return _owner_receipt_hash(
        RUN_COMPLETION_RECEIPT_KIND,
        run.run_ref,
        _run_completion_bindings(run, attempt),
    )


def _run_completion(run, attempt) -> RunCompletion:
    if (
        run.status != "completed"
        or attempt.status != "completed"
        or not run.outcome_ref
        or attempt.decision_receipt_subject_ref != run.outcome_ref
        or run.completion_receipt_hash != _run_completion_receipt_hash(run, attempt)
    ):
        raise OwnerConflict("run_completion_invalid")
    return RunCompletion(
        request_ref=run.request_ref,
        run_ref=run.run_ref,
        attempt_ref=attempt.attempt_ref,
        outcome_ref=run.outcome_ref,
        decision_receipt=AcceptanceReceipt(
            issuer="research_graph",
            kind="idea_outcome_accepted",
            receipt_ref=attempt.decision_receipt_ref,
            subject_ref=run.outcome_ref,
            payload_hash=attempt.decision_receipt_hash,
        ),
        receipt=AcceptanceReceipt(
            issuer=AR_OWNER,
            kind=RUN_COMPLETION_RECEIPT_KIND,
            receipt_ref=run.completion_receipt_ref,
            subject_ref=run.run_ref,
            payload_hash=run.completion_receipt_hash,
        ),
    )


def _load_stage_fence(connection, run_ref: str, attempt_ref: str, fence_ref: str):
    run = connection.execute(
        text("SELECT * FROM ar_stage_runs WHERE run_ref = :run_ref"),
        {"run_ref": run_ref},
    ).first()
    attempt = connection.execute(
        text("SELECT * FROM ar_stage_attempts WHERE attempt_ref = :attempt_ref"),
        {"attempt_ref": attempt_ref},
    ).first()
    fence = connection.execute(
        text("SELECT * FROM ar_execution_fences WHERE fence_ref = :fence_ref"),
        {"fence_ref": fence_ref},
    ).first()
    if run is None or attempt is None or fence is None:
        raise OwnerConflict("attempt_fence_invalid")
    _runtime_binding_from_row(run)
    session = connection.execute(
        text("SELECT * FROM ar_stage_sessions WHERE session_ref = :session_ref"),
        {"session_ref": run.root_session_ref},
    ).first()
    if session is None or (
        attempt.run_ref != run_ref
        or attempt.root_session_ref != session.session_ref
        or fence.run_ref != run_ref
        or fence.attempt_ref != attempt_ref
    ):
        raise OwnerConflict("attempt_fence_invalid")
    return run, attempt, session, fence


def _require_current_fence(
    run, attempt, fence, attempt_status: str, fence_status: str
) -> None:
    expected_run_status = (
        "running" if attempt_status == "running" else "awaiting_acceptance"
    )
    if (
        run.status != expected_run_status
        or run.current_attempt_ref != attempt.attempt_ref
        or run.current_fence_ref != fence.fence_ref
        or attempt.fence_ref != fence.fence_ref
        or attempt.status != attempt_status
        or fence.status != fence_status
        or int(attempt.generation) != int(fence.generation)
    ):
        raise OwnerConflict("attempt_fence_stale")


def _validate_stage_idempotency_key(value: str) -> None:
    if not value or len(value) > 128:
        raise OwnerConflict("idempotency_key_invalid")


def _validated_runtime_binding(
    binding: IdeaRuntimeBinding,
) -> tuple[IdeaRuntimeBinding, str, str]:
    if not isinstance(binding, IdeaRuntimeBinding):
        raise OwnerConflict("idea_runtime_binding_invalid")
    value = binding.as_dict()
    if (
        set(value)
        != {
            "schema_ref",
            "packaged_skill_bundle_hash",
            "instruction_set_hash",
            "model_ref",
            "harness_adapter_ref",
            "mcp_bindings",
            "capability_bindings",
            "resource_bindings",
        }
        or binding.schema_ref != IDEA_RUNTIME_BINDING_SCHEMA
        or not _is_sha256(binding.packaged_skill_bundle_hash)
        or not _is_sha256(binding.instruction_set_hash)
        or not _runtime_ref(binding.model_ref)
        or not _runtime_ref(binding.harness_adapter_ref)
    ):
        raise OwnerConflict("idea_runtime_binding_invalid")
    for values in (
        binding.mcp_bindings,
        binding.capability_bindings,
        binding.resource_bindings,
    ):
        if (
            not isinstance(values, tuple)
            or not all(_runtime_ref(item) for item in values)
            or len(values) != len(set(values))
        ):
            raise OwnerConflict("idea_runtime_binding_invalid")
    if (
        binding.mcp_bindings
        or any(
            capability not in _IDEA_SAFE_CAPABILITIES
            for capability in binding.capability_bindings
        )
        or any(
            not resource.startswith(_IDEA_SAFE_RESOURCE_PREFIXES)
            or "\n" in resource
            or "\r" in resource
            or "../" in resource
            for resource in binding.resource_bindings
        )
    ):
        raise OwnerConflict("idea_runtime_binding_unauthorized")
    binding_json = canonical_json(value)
    return binding, binding_json, canonical_hash(value)


def _resolved_reviewed_draft(
    outcome: dict[str, object],
    review: dict[str, object],
    reviewed_draft: dict[str, object] | None,
) -> dict[str, object]:
    if reviewed_draft is not None:
        if not isinstance(reviewed_draft, dict):
            raise OwnerConflict("reviewed_draft_invalid")
        return reviewed_draft
    # Compatibility for pre-v2 callers is unambiguous only when the final
    # outcome is itself the reviewed draft. Revised production executions fail
    # closed until their full draft is supplied.
    if review.get("reviewed_draft_hash") != canonical_hash(outcome):
        raise OwnerConflict("reviewed_draft_missing")
    return outcome


def _validate_attempt_review_for_write(
    review: dict[str, object], *, native_session_ref: str
) -> str:
    """Accept only the child-agent review contract for new AR executions.

    Historical v1 reviews remain readable from their immutable execution
    payloads.  They are not a production write format: their
    ``reviewer_session_ref`` encoded the retired extra-Session topology.
    """

    if review.get("schema_ref") == IDEA_REVIEW_SCHEMA_V1_REF:
        raise OwnerConflict("attempt_review_legacy_read_only")
    reviewer_agent_ref = review.get("reviewer_agent_ref")
    if (
        review.get("schema_ref") != IDEA_REVIEW_SCHEMA_REF
        or review.get("review_mode") != "harness_child_agent"
        or not isinstance(reviewer_agent_ref, str)
        or not reviewer_agent_ref.strip()
        or len(reviewer_agent_ref) > 512
        or reviewer_agent_ref == native_session_ref
        or review.get("independent") is not True
        or review.get("advisory_only") is not True
    ):
        raise OwnerConflict("attempt_review_independence_invalid")
    return reviewer_agent_ref


def _runtime_binding_from_row(row) -> IdeaRuntimeBinding:
    try:
        value = decoded_object(row.runtime_binding_json)
        if set(value) != {
            "schema_ref",
            "packaged_skill_bundle_hash",
            "instruction_set_hash",
            "model_ref",
            "harness_adapter_ref",
            "mcp_bindings",
            "capability_bindings",
            "resource_bindings",
        }:
            raise TypeError("runtime binding")
        for field in (
            "mcp_bindings",
            "capability_bindings",
            "resource_bindings",
        ):
            if not isinstance(value[field], list):
                raise TypeError(field)
        binding = IdeaRuntimeBinding(
            packaged_skill_bundle_hash=value["packaged_skill_bundle_hash"],
            instruction_set_hash=value["instruction_set_hash"],
            model_ref=value["model_ref"],
            harness_adapter_ref=value["harness_adapter_ref"],
            mcp_bindings=tuple(value["mcp_bindings"]),
            capability_bindings=tuple(value["capability_bindings"]),
            resource_bindings=tuple(value["resource_bindings"]),
            schema_ref=value["schema_ref"],
        )
        binding, binding_json, binding_hash = _validated_runtime_binding(binding)
    except (KeyError, TypeError, ValueError) as error:
        raise OwnerConflict("idea_runtime_binding_invalid") from error
    expected_admission_hash = canonical_hash(
        {
            "command": "admit_idea_stage",
            "request_ref": row.request_ref,
            "cycle_ref": row.cycle_ref,
            "stage": row.stage,
            "epoch": int(row.epoch),
            "context_pack_ref": row.context_pack_ref,
            "context_pack_hash": row.context_pack_hash,
            "request_receipt": AcceptanceReceipt(
                issuer="advancement_engine",
                kind="stage_run_request",
                receipt_ref=row.request_receipt_ref,
                subject_ref=row.request_ref,
                payload_hash=row.request_receipt_hash,
            ).as_public_dict(),
            "runtime_binding": binding.as_dict(),
            "runtime_binding_hash": binding_hash,
        }
    )
    if (
        binding_json != row.runtime_binding_json
        or binding_hash != row.runtime_binding_hash
        or expected_admission_hash != row.admission_hash
    ):
        raise OwnerConflict("idea_runtime_binding_invalid")
    return binding


def _runtime_ref(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 512


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _stage_command_replay(
    connection, idempotency_key: str, command_kind: str, request_hash: str
) -> str | None:
    row = connection.execute(
        text(
            "SELECT * FROM ar_stage_commands WHERE idempotency_key = "
            ":idempotency_key"
        ),
        {"idempotency_key": idempotency_key},
    ).first()
    if row is not None:
        if row.command_kind != command_kind or row.request_hash != request_hash:
            raise OwnerConflict("idempotency_conflict")
        return row.result_ref
    host_snapshot = connection.execute(
        text(
            "SELECT 1 FROM ar_host_capability_snapshots WHERE idempotency_key = "
            ":idempotency_key UNION ALL SELECT 1 FROM "
            "ar_host_compute_observation_claims WHERE idempotency_key = "
            ":idempotency_key LIMIT 1"
        ),
        {"idempotency_key": idempotency_key},
    ).first()
    if host_snapshot is not None:
        raise OwnerConflict("idempotency_conflict")
    return None


def _query_stage_command(
    database: Database,
    idempotency_key: str,
    command_kind: str,
    request_hash: str,
) -> str | None:
    with database.read() as connection:
        return _stage_command_replay(
            connection, idempotency_key, command_kind, request_hash
        )


def _record_stage_command(
    connection,
    idempotency_key: str,
    command_kind: str,
    request_hash: str,
    result_ref: str,
) -> None:
    connection.execute(
        text(
            "INSERT INTO ar_stage_commands (idempotency_key, command_kind, "
            "request_hash, result_ref, recorded_at) VALUES (:idempotency_key, "
            ":command_kind, :request_hash, :result_ref, :recorded_at)"
        ),
        {
            "idempotency_key": idempotency_key,
            "command_kind": command_kind,
            "request_hash": request_hash,
            "result_ref": result_ref,
            "recorded_at": time.time(),
        },
    )


def _observation_from_row(row) -> HostComputeObservation:
    try:
        capabilities = decoded_object(row.capabilities_json)
        devices_value = capabilities.get("devices")
        if not isinstance(devices_value, list):
            raise TypeError("devices")
        devices = tuple(
            HostComputeDevice(
                uuid=device["uuid"],
                name=device["name"],
                memory_total_mib=device["memory_total_mib"],
            )
            for device in devices_value
            if isinstance(device, dict)
        )
        if len(devices) != len(devices_value):
            raise TypeError("device")
        snapshot = HostComputeSnapshot(
            status=row.status,
            observed_at=float(row.observed_at),
            devices=devices,
            adapter_kind=row.adapter_kind,
            reason_code=row.reason_code,
        )
        _validate_probe_snapshot(snapshot)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise OwnerConflict("host_compute_snapshot_invalid") from error
    if canonical_hash(capabilities) != row.capabilities_hash:
        raise OwnerConflict("host_compute_snapshot_invalid")
    return HostComputeObservation(
        snapshot_ref=row.snapshot_ref,
        status=snapshot.status,
        observed_at=snapshot.observed_at,
        devices=snapshot.devices,
        adapter_kind=snapshot.adapter_kind,
        capabilities_hash=row.capabilities_hash,
        reason_code=snapshot.reason_code,
    )


def _validate_probe_snapshot(snapshot: HostComputeSnapshot) -> None:
    if snapshot.status not in {"ready", "unavailable"}:
        raise OwnerConflict("host_compute_snapshot_invalid")
    if snapshot.status == "ready" and snapshot.reason_code is not None:
        raise OwnerConflict("host_compute_snapshot_invalid")
    if snapshot.status == "unavailable" and not snapshot.reason_code:
        raise OwnerConflict("host_compute_snapshot_invalid")
    if not math.isfinite(snapshot.observed_at) or not snapshot.adapter_kind:
        raise OwnerConflict("host_compute_snapshot_invalid")
    uuids: set[str] = set()
    for device in snapshot.devices:
        if (
            not device.uuid
            or device.uuid in uuids
            or not device.name
            or device.memory_total_mib <= 0
        ):
            raise OwnerConflict("host_compute_snapshot_invalid")
        uuids.add(device.uuid)


def create_agent_runtime_interface(
    database: Database,
    feed: DurableFeed,
    host_compute_probe: HostComputeProbe,
    stage_request_verifier: StageRunRequestVerifier | None = None,
    outcome_verifier: IdeaOutcomeDecisionVerifier | None = None,
    deepfetch_request_verifier: DeepFetchRunRequestVerifier | None = None,
) -> AgentRuntimeInterface:
    return SQLiteAgentRuntime(
        database,
        feed,
        host_compute_probe,
        stage_request_verifier,
        outcome_verifier,
        deepfetch_request_verifier,
    )


def create_agent_runtime_receipt_verifier(
    database: Database,
    stage_request_verifier: StageRunRequestVerifier | None = None,
) -> SQLiteAgentRuntimeReceiptVerifier:
    return SQLiteAgentRuntimeReceiptVerifier(database, stage_request_verifier)


def create_host_compute_observation_reader(
    database: Database,
) -> HostComputeObservationReader:
    return SQLiteHostComputeObservationReader(database)
