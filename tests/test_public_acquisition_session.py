from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import meta_research.owners.agent_runtime as agent_runtime_module
from meta_research.acquisition import (
    AcquisitionBatchRequest,
    AcquisitionItemResult,
    AcquisitionPaper,
    AcquisitionPreflightResult,
    AcquisitionRuntimeBinding,
    AcquisitionUnavailable,
    canonical_hash as acquisition_hash,
    canonical_json as acquisition_json,
    validate_batch_request,
    validate_item_results,
)
from meta_research.composition import build_production_runtime
from meta_research.paths import prepare_data_root
from meta_research.owners.common import OwnerConflict
from meta_research.quest_drafting import HostComputeDevice, HostComputeSnapshot
from meta_research.runtime_protection import (
    InhibitorLease,
    RuntimeProtectionUnavailable,
)
from meta_research.semantic_mcp import ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS
from meta_research.web import create_app


class NoCompute:
    def observe(self) -> HostComputeSnapshot:
        return HostComputeSnapshot(
            status="ready",
            observed_at=1_720_000_000.0,
            devices=(
                HostComputeDevice(
                    uuid="GPU-acquisition-1",
                    name="Acquisition Test GPU",
                    memory_total_mib=24_576,
                ),
            ),
            adapter_kind="test_probe",
        )


class RecordingAcquisitionProvider:
    def __init__(self) -> None:
        self.preflights = []
        self.batches: list[AcquisitionBatchRequest] = []
        self.waiting_request_ids: set[str] = set()

    def runtime_binding(self) -> AcquisitionRuntimeBinding:
        return AcquisitionRuntimeBinding(
            provider_ref="test/nature-downloader",
            provider_version="cb369c938da835bcd07202e03ccc770551984070",
            capability_bindings=(
                "browser-context-reuse",
                "lawful-fulltext-routing",
                "private-manifest",
            ),
        )

    def preflight(self, request) -> AcquisitionPreflightResult:
        self.preflights.append(request)
        return AcquisitionPreflightResult(
            status="ready",
            browser_context_ref="browser-context-authenticated-1",
            reason_code=None,
            evidence={
                "configuration_health": "ready",
                "browser_control": "functional",
                "authorized_resource": "verified",
            },
        )

    def acquire(self, request: AcquisitionBatchRequest):
        self.batches.append(request)
        paper = request.papers[0]
        if paper.paper_id == "paper:waiting" and request.request_id not in self.waiting_request_ids:
            self.waiting_request_ids.add(request.request_id)
            return (
                AcquisitionItemResult(
                    paper_id=paper.paper_id,
                    status="waiting_user",
                    path=None,
                    format=None,
                    failure={
                        "code": "institutional_login_required",
                        "detail": "请在既有受控浏览器中恢复图书馆登录。",
                    },
                ),
            )
        return tuple(
            AcquisitionItemResult(
                paper_id=item.paper_id,
                status="missing",
                path=None,
                format=None,
                failure={
                    "code": "oa_fulltext_not_found",
                    "detail": "适用合法路线已穷尽。",
                },
            )
            for item in request.papers
        )


@pytest.mark.parametrize(
    ("code_length", "detail_length", "accepted"),
    ((256, 4_000, True), (257, 1, False), (1, 4_001, False)),
)
def test_acquisition_failure_fits_the_public_effect_contract(
    code_length: int,
    detail_length: int,
    accepted: bool,
) -> None:
    request = AcquisitionBatchRequest(
        request_id="failure-public-effect-contract",
        route_policy="oa_first_then_institution",
        papers=(
            AcquisitionPaper(
                paper_id="paper:failure-contract",
                title="Failure contract",
                doi=None,
                arxiv_id=None,
                source_urls=(),
            ),
        ),
    )
    results = (
        AcquisitionItemResult(
            paper_id="paper:failure-contract",
            status="missing",
            path=None,
            format=None,
            failure={"code": "c" * code_length, "detail": "d" * detail_length},
        ),
    )

    if accepted:
        assert validate_item_results(request, results) == results
    else:
        with pytest.raises(AcquisitionUnavailable, match="acquisition_result_invalid"):
            validate_item_results(request, results)


class ObtainingAcquisitionProvider(RecordingAcquisitionProvider):
    content = b"owner-frozen-acquisition-artifact"

    def acquire(self, request: AcquisitionBatchRequest):
        self.batches.append(request)
        target = Path(request.target_dir) / "owner-frozen.html"
        target.write_bytes(self.content)
        return tuple(
            AcquisitionItemResult(
                paper_id=item.paper_id,
                status="obtained",
                path=str(target.resolve()),
                format="html",
                failure=None,
            )
            for item in request.papers
        )


class HumanRequestAcquisitionProvider(RecordingAcquisitionProvider):
    def __init__(self) -> None:
        super().__init__()
        self.owner = None
        self.human_request: dict[str, object] | None = None
        self.reconciliations: list[AcquisitionBatchRequest] = []

    @staticmethod
    def _missing(
        request: AcquisitionBatchRequest,
    ) -> tuple[AcquisitionItemResult, ...]:
        return tuple(
            AcquisitionItemResult(
                paper_id=item.paper_id,
                status="missing",
                path=None,
                format=None,
                failure={
                    "code": "oa_fulltext_not_found",
                    "detail": "The exact lawful route was exhausted.",
                },
            )
            for item in request.papers
        )

    def acquire(self, request: AcquisitionBatchRequest):
        self.batches.append(request)
        assert self.owner is not None
        scope = request.root_runtime_scope
        assert scope is not None
        generation = int(scope["generation"])
        target = {
            "schema_ref": "meta-research/root-agent-human-request-target/v1",
            "root": {
                "run_kind": "acquisition",
                "run_ref": scope["run_ref"],
                "attempt_ref": scope["attempt_ref"],
                "root_session_ref": scope["root_session_ref"],
                "fence_ref": scope["fence_ref"],
                "waiter_generation": generation,
            },
            "condition": {"operator_choice": "continue_without_optional_input"},
        }
        binding = {
            "quest_ref": scope["quest_ref"],
            "task_ref": scope["run_ref"],
            "root_session_ref": scope["root_session_ref"],
            "operation_id": ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[0],
            "attempt_ref": scope["attempt_ref"],
            "generation": generation,
            "request_owner": "agent_runtime",
            "root_kind": "acquisition",
            "phase": "batch",
            "fence_ref": scope["fence_ref"],
            "runtime_binding_hash": scope["runtime_binding_hash"],
        }
        self.human_request = self.owner.open_human_request_effect(
            effect_key="mcp-effect:"
            + acquisition_hash(
                {
                    "operation_binding": binding,
                    "effect_id": "acquisition-needs-operator-choice",
                }
            ),
            effect_id="acquisition-needs-operator-choice",
            operation_binding=binding,
            predecessor_request_ref=None,
            request_kind="offline_action",
            obligation="Choose whether this exact Acquisition task should continue.",
            business_purpose="Resume only this exact Quest-bound acquisition task.",
            target_assertion=target,
            acceptance_conditions=("The operator records an exact disposition.",),
            direct_waiter={
                "waiter_ref": f"root_run:{scope['run_ref']}",
                "generation": generation,
                "target_assertion": target,
                "wait_scope": "local",
                "other_blockers": [],
            },
            quest_ref=str(scope["quest_ref"]),
        )
        return self._missing(request)

    def reconcile(self, request: AcquisitionBatchRequest):
        self.reconciliations.append(request)
        return self._missing(request)


class LibraryReconnectHumanRequestAcquisitionProvider(
    RecordingAcquisitionProvider
):
    def __init__(self) -> None:
        super().__init__()
        self.owner = None
        self.human_request: dict[str, object] | None = None

    def acquire(self, request: AcquisitionBatchRequest):
        assert self.owner is not None
        scope = request.root_runtime_scope
        assert scope is not None
        generation = int(scope["generation"])
        binding = {
            "quest_ref": scope["quest_ref"],
            "task_ref": scope["run_ref"],
            "root_session_ref": scope["root_session_ref"],
            "operation_id": ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[0],
            "attempt_ref": scope["attempt_ref"],
            "generation": generation,
            "request_owner": "agent_runtime",
            "root_kind": "acquisition",
            "phase": "batch",
            "fence_ref": scope["fence_ref"],
            "runtime_binding_hash": scope["runtime_binding_hash"],
        }
        target = {
            "schema_ref": "meta-research/root-agent-human-request-target/v1",
            "root": {
                "run_kind": "acquisition",
                "run_ref": scope["run_ref"],
                "attempt_ref": scope["attempt_ref"],
                "root_session_ref": scope["root_session_ref"],
                "fence_ref": scope["fence_ref"],
                "waiter_generation": generation,
            },
            "condition": {
                "route": "literature_access",
                "session_ref": "ui-stale-session-must-not-be-trusted",
            },
        }
        self.human_request = self.owner.open_human_request_effect(
            effect_key="mcp-effect:root-library-reconnect-repreflight",
            effect_id="root-library-reconnect-repreflight",
            operation_binding=binding,
            predecessor_request_ref=None,
            request_kind="library_reconnect",
            obligation="Restore the institution route for this exact task.",
            business_purpose="Resume the exact Quest-bound acquisition task.",
            target_assertion=target,
            acceptance_conditions=(
                "The current Quest acquisition preflight is ready.",
            ),
            direct_waiter={
                "waiter_ref": f"root_run:{scope['run_ref']}",
                "generation": generation,
                "target_assertion": target,
                "wait_scope": "local",
                "other_blockers": [],
            },
            quest_ref=str(scope["quest_ref"]),
        )
        return super().acquire(request)


@pytest.mark.parametrize("request_id", ["/tmp/x", "../x", "a/b"])
def test_acquisition_request_id_must_be_a_path_safe_token(request_id: str) -> None:
    request = AcquisitionBatchRequest(
        request_id=request_id,
        route_policy="oa_first_then_institution",
        papers=(
            AcquisitionPaper(
                paper_id="paper:path-safe-request-id",
                title="Path-safe acquisition identity",
                doi=None,
                arxiv_id=None,
                source_urls=(),
            ),
        ),
    )

    with pytest.raises(
        AcquisitionUnavailable,
        match="acquisition_batch_request_invalid",
    ):
        validate_batch_request(request)


class RejectingPowerInhibitor:
    kind = "test_rejecting_inhibitor"

    def acquire(self, *, holder_ref: str, reason: str):
        del holder_ref, reason
        raise RuntimeProtectionUnavailable("power_inhibitor_test_rejected")

    def is_confirmed(self, lease) -> bool:
        del lease
        return False

    def release(self, lease) -> None:
        del lease


class SwitchablePowerInhibitor:
    kind = "test_switchable_inhibitor"

    def __init__(self) -> None:
        self.fail = False
        self.live_holders: set[str] = set()

    def acquire(self, *, holder_ref: str, reason: str) -> InhibitorLease:
        del reason
        if self.fail:
            raise RuntimeProtectionUnavailable("power_inhibitor_test_rejected")
        self.live_holders.add(holder_ref)
        return InhibitorLease(
            holder_ref=holder_ref,
            backend=self.kind,
            scope="sleep",
            acquired_at=1_720_000_000.0,
            native_holder_ref="test-switchable-holder",
        )

    def is_confirmed(self, lease: InhibitorLease) -> bool:
        return lease.holder_ref in self.live_holders

    def release(self, lease: InhibitorLease) -> None:
        self.live_holders.discard(lease.holder_ref)


class BlockingConfirmationPowerInhibitor:
    kind = "test_blocking_confirmation_inhibitor"

    def __init__(self) -> None:
        self.block_next_confirmation = False
        self.confirmation_started = threading.Event()
        self.continue_confirmation = threading.Event()
        self.live_holders: set[str] = set()

    def acquire(self, *, holder_ref: str, reason: str) -> InhibitorLease:
        del reason
        self.live_holders.add(holder_ref)
        return InhibitorLease(
            holder_ref=holder_ref,
            backend=self.kind,
            scope="sleep",
            acquired_at=1_720_000_000.0,
            native_holder_ref="test-blocking-confirmation-holder",
        )

    def query_hold(self, lease: InhibitorLease) -> str:
        if self.block_next_confirmation:
            self.block_next_confirmation = False
            self.confirmation_started.set()
            assert self.continue_confirmation.wait(timeout=5)
            return "unknown"
        return "confirmed" if lease.holder_ref in self.live_holders else "absent"

    def is_confirmed(self, lease: InhibitorLease) -> bool:
        return lease.holder_ref in self.live_holders

    def release(self, lease: InhibitorLease) -> None:
        self.live_holders.discard(lease.holder_ref)


class BlockingRepreflightAcquisitionProvider(RecordingAcquisitionProvider):
    def __init__(self) -> None:
        super().__init__()
        self.block_next_preflight = False
        self.preflight_started = threading.Event()
        self.continue_preflight = threading.Event()

    def preflight(self, request) -> AcquisitionPreflightResult:
        if self.block_next_preflight:
            self.block_next_preflight = False
            self.preflight_started.set()
            assert self.continue_preflight.wait(timeout=5)
        return super().preflight(request)


class PartialAcquisitionProvider(RecordingAcquisitionProvider):
    def acquire(self, request: AcquisitionBatchRequest):
        self.batches.append(request)
        target_root = Path(request.target_dir)
        target_root.mkdir(parents=True, exist_ok=True)
        paths = {
            item.paper_id: target_root / (
                item.paper_id.replace(":", "-") + ".pdf"
            )
            for item in request.papers
        }
        for path in paths.values():
            path.write_bytes(b"%PDF-1.4\nfixture acquisition artifact\n")
        if len(self.batches) == 1:
            return tuple(
                AcquisitionItemResult(
                    paper_id=item.paper_id,
                    status="obtained" if item.paper_id == "paper:obtained" else "waiting_user",
                    path=(
                        str(paths[item.paper_id].resolve())
                        if item.paper_id == "paper:obtained"
                        else None
                    ),
                    format=("pdf" if item.paper_id == "paper:obtained" else None),
                    failure=(
                        None
                        if item.paper_id == "paper:obtained"
                        else {
                            "code": "institutional_login_required",
                            "detail": "请恢复机构登录。",
                        }
                    ),
                )
                for item in request.papers
            )
        return tuple(
            AcquisitionItemResult(
                paper_id=item.paper_id,
                status="obtained",
                path=str(paths[item.paper_id].resolve()),
                format="pdf",
                failure=None,
            )
            for item in request.papers
        )


class AllWaitingAcquisitionProvider(RecordingAcquisitionProvider):
    def acquire(self, request: AcquisitionBatchRequest):
        if self.batches:
            raise AssertionError("accepted material must not replay the provider")
        self.batches.append(request)
        return tuple(
            AcquisitionItemResult(
                paper_id=item.paper_id,
                status="waiting_user",
                path=None,
                format=None,
                failure={
                    "code": "institutional_login_required",
                    "detail": "Provide an exact lawful copy for this item.",
                },
            )
            for item in request.papers
        )


class BlockingAcquisitionProvider(RecordingAcquisitionProvider):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def acquire(self, request: AcquisitionBatchRequest):
        self.batches.append(request)
        self.started.set()
        assert self.release.wait(timeout=5)
        return tuple(
            AcquisitionItemResult(
                paper_id=item.paper_id,
                status="missing",
                path=None,
                format=None,
                failure={"code": "route_exhausted", "detail": "路线已穷尽。"},
            )
            for item in request.papers
        )


class ReconcilingAcquisitionProvider(RecordingAcquisitionProvider):
    def __init__(self) -> None:
        super().__init__()
        self.acquire_calls = 0
        self.reconcile_calls: list[AcquisitionBatchRequest] = []

    def acquire(self, request: AcquisitionBatchRequest):
        self.acquire_calls += 1
        raise AssertionError("unknown operation must not be replayed")

    def reconcile(self, request: AcquisitionBatchRequest):
        self.reconcile_calls.append(request)
        return tuple(
            AcquisitionItemResult(
                paper_id=item.paper_id,
                status="missing",
                path=None,
                format=None,
                failure={
                    "code": "route_exhausted",
                    "detail": "私有 manifest 已验证全部路线终止。",
                },
            )
            for item in request.papers
        )


class RepeatedPendingAcquisitionProvider(RecordingAcquisitionProvider):
    def __init__(self, *, pending_reconciliations: int = 1) -> None:
        super().__init__()
        self.pending_reconciliations = pending_reconciliations
        self.acquire_calls = 0
        self.reconcile_calls: list[AcquisitionBatchRequest] = []

    @staticmethod
    def _pending(request: AcquisitionBatchRequest):
        return tuple(
            AcquisitionItemResult(
                paper_id=item.paper_id,
                status="waiting_user",
                path=None,
                format=None,
                failure={
                    "code": "acquisition_reconciliation_required",
                    "detail": "The existing operation has no terminal receipt yet.",
                },
            )
            for item in request.papers
        )

    def acquire(self, request: AcquisitionBatchRequest):
        self.acquire_calls += 1
        return self._pending(request)

    def reconcile(self, request: AcquisitionBatchRequest):
        self.reconcile_calls.append(request)
        if len(self.reconcile_calls) <= self.pending_reconciliations:
            return self._pending(request)
        return tuple(
            AcquisitionItemResult(
                paper_id=item.paper_id,
                status="missing",
                path=None,
                format=None,
                failure={
                    "code": "route_exhausted",
                    "detail": "The exact operation now has a terminal manifest.",
                },
            )
            for item in request.papers
        )


class InvalidArtifactAfterReconcileProvider(RecordingAcquisitionProvider):
    def __init__(self) -> None:
        super().__init__()
        self.reconcile_calls: list[AcquisitionBatchRequest] = []

    def acquire(self, request: AcquisitionBatchRequest):
        self.batches.append(request)
        return tuple(
            AcquisitionItemResult(
                paper_id=item.paper_id,
                status="waiting_user",
                path=None,
                format=None,
                failure={
                    "code": "acquisition_reconciliation_required",
                    "detail": "The provider operation requires exact reconciliation.",
                },
            )
            for item in request.papers
        )

    def reconcile(self, request: AcquisitionBatchRequest):
        self.reconcile_calls.append(request)
        return tuple(
            AcquisitionItemResult(
                paper_id=item.paper_id,
                status="obtained",
                path=str((Path(request.target_dir) / "missing.pdf").resolve()),
                format="pdf",
                failure=None,
            )
            for item in request.papers
        )


class CrashAfterConsumedWaiterProvider(RecordingAcquisitionProvider):
    def __init__(self) -> None:
        super().__init__()
        self.acquire_calls = 0
        self.reconcile_calls: list[AcquisitionBatchRequest] = []

    def acquire(self, request: AcquisitionBatchRequest):
        self.acquire_calls += 1
        if self.acquire_calls == 1:
            return super().acquire(request)
        raise KeyboardInterrupt("crash after the resume receipt was consumed")

    def reconcile(self, request: AcquisitionBatchRequest):
        self.reconcile_calls.append(request)
        return tuple(
            AcquisitionItemResult(
                paper_id=item.paper_id,
                status="missing",
                path=None,
                format=None,
                failure={
                    "code": "route_exhausted",
                    "detail": "The exact interrupted operation is now terminal.",
                },
            )
            for item in request.papers
        )


def test_provider_waiting_user_does_not_create_a_human_request(
    tmp_path: Path,
) -> None:
    provider = RecordingAcquisitionProvider()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "provider-waiting-no-human-request"),
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
    )
    client, write_headers = _authenticated_client(runtime)
    try:
        _, _, session_ref = _open_ready_acquisition(
            client,
            write_headers,
            prefix="provider-waiting-no-human-request",
        )
        before = runtime.owners.agent_runtime.query_human_requests()
        execution = runtime.owners.agent_runtime.acquire_literature(
            session_ref,
            AcquisitionBatchRequest(
                request_id="provider-waiting-no-human-request",
                route_policy="oa_first_then_institution",
                papers=(
                    AcquisitionPaper(
                        paper_id="paper:waiting",
                        title="Needs a human-selected next action",
                        doi=None,
                        arxiv_id=None,
                        source_urls=(),
                    ),
                ),
            ),
            provider,
        )

        assert execution.status == "waiting_user"
        assert runtime.owners.agent_runtime.query_human_requests() == before == ()
    finally:
        client.close()
        runtime.close()


@pytest.mark.parametrize("restart_after_response", (False, True))
def test_explicit_root_library_reconnect_repreflights_quest_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restart_after_response: bool,
) -> None:
    provider = LibraryReconnectHumanRequestAcquisitionProvider()
    data_root = prepare_data_root(
        tmp_path / f"root-library-reconnect-{restart_after_response}"
    )
    runtime = build_production_runtime(
        data_root,
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
    )
    provider.owner = runtime.owners.agent_runtime
    client, write_headers = _authenticated_client(runtime)
    owner = runtime.owners.agent_runtime
    try:
        initialization_id, _saved, session_ref = _open_ready_acquisition(
            client,
            write_headers,
            prefix="root-library-reconnect-repreflight",
        )
        quest_ref = "quest_root_library_reconnect_repreflight"
        owner.bind_acquisition_session_to_quest(initialization_id, quest_ref)
        request = AcquisitionBatchRequest(
            request_id="root-library-reconnect-repreflight-batch",
            route_policy="oa_first_then_institution",
            papers=(
                AcquisitionPaper(
                    paper_id="paper:waiting",
                    title="Institution login is required",
                    doi=None,
                    arxiv_id=None,
                    source_urls=(),
                ),
            ),
        )

        waiting = owner.acquire_literature(session_ref, request, provider)
        assert waiting.status == "waiting_user"
        assert len(provider.preflights) == 1
        [provider_request] = provider.batches
        scope = provider_request.root_runtime_scope
        assert scope is not None
        assert provider.human_request is not None
        human_request = provider.human_request
        assert human_request["open_effect"]["operation_binding"][
            "quest_ref"
        ] == quest_ref
        assert owner.query_managed_run(str(scope["run_ref"]))[
            "status"
        ] == "suspended"

        if restart_after_response:
            def crash_after_response_commit(_request_ref: str) -> None:
                raise KeyboardInterrupt("crash before acquisition re-preflight")

            monkeypatch.setattr(
                runtime.owners.human_collaboration,
                "_reconcile_issuing_owner_human_request",
                crash_after_response_commit,
            )
            with pytest.raises(KeyboardInterrupt):
                runtime.owners.human_collaboration.respond_to_human_request(
                    str(human_request["request_ref"]),
                    decision="provided",
                    facts={"route": "institutional_browser_reconnected"},
                    note="The institution browser is reconnected.",
                    idempotency_key="root-library-reconnect-repreflight-response",
                )
            assert len(provider.preflights) == 1
            runtime.close()
            runtime = build_production_runtime(
                data_root,
                acquisition_provider=provider,
                host_compute_probe=NoCompute(),
            )
            owner = runtime.owners.agent_runtime
        else:
            runtime.owners.human_collaboration.respond_to_human_request(
                str(human_request["request_ref"]),
                decision="provided",
                facts={"route": "institutional_browser_reconnected"},
                note="The institution browser is reconnected.",
                idempotency_key="root-library-reconnect-repreflight-response",
            )

        assert len(provider.preflights) == 2
        refreshed = owner.query_acquisition_session(quest_ref=quest_ref)
        assert refreshed is not None
        assert refreshed.session_ref == session_ref
        assert refreshed.status == "ready"
        current = owner.query_human_request(str(human_request["request_ref"]))
        assert current is not None
        assert len(current["responses"]) == 1
        assert current["responses"][0]["decision"] == "provided"
        assert current["status"] == "satisfied"
        assert current["direct_waiters"][0]["status"] == "consumed"
        assert owner.query_managed_run(str(scope["run_ref"]))[
            "status"
        ] == "running"
        replayed = runtime.owners.human_collaboration.respond_to_human_request(
            str(human_request["request_ref"]),
            decision="provided",
            facts={"route": "institutional_browser_reconnected"},
            note="The institution browser is reconnected.",
            idempotency_key="root-library-reconnect-repreflight-response",
        )
        assert replayed == current["responses"][0]
        assert len(provider.preflights) == 2
    finally:
        client.close()
        runtime.close()


class CrashAfterOaResumeProvider(RecordingAcquisitionProvider):
    def __init__(self) -> None:
        super().__init__()
        self.acquire_calls = 0
        self.reconcile_calls: list[AcquisitionBatchRequest] = []

    def acquire(self, request: AcquisitionBatchRequest):
        self.acquire_calls += 1
        if self.acquire_calls == 1:
            return super().acquire(request)
        assert request.session_mode == "oa_only"
        assert request.browser_context_ref is None
        raise KeyboardInterrupt("crash after OA route consumption")

    def reconcile(self, request: AcquisitionBatchRequest):
        self.reconcile_calls.append(request)
        return tuple(
            AcquisitionItemResult(
                paper_id=item.paper_id,
                status="missing",
                path=None,
                format=None,
                failure={
                    "code": "route_exhausted",
                    "detail": "The exact OA-only operation is now terminal.",
                },
            )
            for item in request.papers
        )


def _authenticated_client(runtime) -> tuple[TestClient, dict[str, str]]:
    base_url = "http://testserver"
    client = TestClient(
        create_app(runtime, base_url=base_url, control_key="control-secret"),
        base_url=base_url,
    )
    bootstrap = runtime.authentication.issue_bootstrap_token()
    exchanged = client.post(
        "/auth/bootstrap",
        headers={"Origin": base_url},
        json={"token": bootstrap},
    )
    assert exchanged.status_code == 200
    return client, {
        "Origin": base_url,
        "X-CSRF-Token": exchanged.json()["csrf_token"],
    }


def _write_headers(base: dict[str, str], key: str) -> dict[str, str]:
    return {**base, "Idempotency-Key": key}


def _open_ready_acquisition(
    client: TestClient,
    write_headers: dict[str, str],
    *,
    prefix: str,
) -> tuple[str, dict[str, object], str]:
    opened = client.post(
        "/api/v1/quest-initializations",
        headers=_write_headers(write_headers, f"{prefix}-open"),
        json={},
    ).json()
    initialization_id = opened["initialization_id"]
    draft = dict(opened["quest_draft"]["value"])
    draft["literature"] = {
        **draft["literature"],
        "mode": "oa_then_institution",
        "library_entry_url": "https://library.example.edu/resources",
    }
    saved_response = client.put(
        f"/api/v1/quest-initializations/{initialization_id}/draft",
        headers=_write_headers(write_headers, f"{prefix}-save"),
        json={
            "expected_draft_revision": opened["quest_draft"]["revision"],
            "expected_draft_hash": opened["quest_draft"]["hash"],
            "draft": draft,
        },
    )
    saved_response.raise_for_status()
    saved = saved_response.json()
    prepared_response = client.post(
        f"/api/v1/quest-initializations/{initialization_id}/acquisition-session",
        headers=_write_headers(write_headers, f"{prefix}-preflight"),
        json={
            "expected_draft_revision": saved["quest_draft"]["revision"],
            "expected_draft_hash": saved["quest_draft"]["hash"],
        },
    )
    prepared_response.raise_for_status()
    return (
        initialization_id,
        saved,
        prepared_response.json()["acquisition_session"]["session_ref"],
    )


def _current_acquisition_human_requests(runtime, session_ref: str, request_id: str):
    return [
        item
        for item in runtime.owners.agent_runtime.query_human_requests()
        if item["kind"] == "library_reconnect"
        and item["target_assertion"].get("session_ref") == session_ref
        and item["target_assertion"].get("acquisition_request_id") == request_id
    ]


def _assert_waiting_without_automatic_human_request(
    runtime, *, session_ref: str, request_id: str
) -> None:
    assert runtime.owners.agent_runtime.query_human_requests() == ()
    session = runtime.owners.agent_runtime.query_acquisition_session(
        session_ref=session_ref
    )
    assert session is not None
    assert session.status == "waiting_user"
    assert session.current_request_id == request_id
    assert session.slot_held is False


def test_owner_rejects_path_unsafe_request_ids_before_target_or_provider(
    tmp_path: Path,
) -> None:
    provider = RecordingAcquisitionProvider()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "path-unsafe-acquisition-request"),
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
    )
    client, write_headers = _authenticated_client(runtime)
    try:
        _initialization_id, _saved, session_ref = _open_ready_acquisition(
            client,
            write_headers,
            prefix="path-unsafe-acquisition-request",
        )
        paper = AcquisitionPaper(
            paper_id="paper:path-unsafe-request",
            title="Reject an unsafe acquisition identity",
            doi=None,
            arxiv_id=None,
            source_urls=(),
        )

        for request_id in ("/tmp/x", "../x", "a/b"):
            with pytest.raises(
                OwnerConflict,
                match="acquisition_batch_request_invalid",
            ):
                runtime.owners.agent_runtime.acquire_literature(
                    session_ref,
                    AcquisitionBatchRequest(
                        request_id=request_id,
                        route_policy="oa_first_then_institution",
                        papers=(paper,),
                    ),
                    provider,
                )

        assert provider.batches == []
        assert not (
            runtime.owners.agent_runtime._acquisition_private_root
            / session_ref
            / "requests"
        ).exists()
    finally:
        client.close()
        runtime.close()


def test_owner_rejects_precreated_acquisition_target_symlink_before_provider(
    tmp_path: Path,
) -> None:
    provider = RecordingAcquisitionProvider()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "symlink-acquisition-target"),
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
    )
    client, write_headers = _authenticated_client(runtime)
    try:
        _initialization_id, _saved, session_ref = _open_ready_acquisition(
            client,
            write_headers,
            prefix="symlink-acquisition-target",
        )
        request = AcquisitionBatchRequest(
            request_id="precreated-target-symlink",
            route_policy="oa_first_then_institution",
            papers=(
                AcquisitionPaper(
                    paper_id="paper:symlink-target",
                    title="Reject a symlinked acquisition target",
                    doi=None,
                    arxiv_id=None,
                    source_urls=(),
                ),
            ),
        )
        requests_root = (
            runtime.owners.agent_runtime._acquisition_private_root
            / session_ref
            / "requests"
        )
        requests_root.mkdir(parents=True, exist_ok=True)
        outside = tmp_path / "outside-owner-target"
        outside.mkdir()
        (requests_root / request.request_id).symlink_to(
            outside,
            target_is_directory=True,
        )

        with pytest.raises(
            OwnerConflict,
            match="acquisition_artifact_target_invalid",
        ):
            runtime.owners.agent_runtime.acquire_literature(
                session_ref,
                request,
                provider,
            )

        assert provider.batches == []
        assert runtime.owners.agent_runtime.query_acquisition_execution(
            session_ref,
            request.request_id,
        ) is None
        session = runtime.owners.agent_runtime.query_acquisition_session(
            session_ref=session_ref
        )
        assert session is not None
        assert session.status == "ready"
        assert session.slot_held is False
        assert session.current_request_id is None
    finally:
        client.close()
        runtime.close()


def test_acquisition_preflight_never_starts_without_confirmed_power_hold(
    tmp_path: Path,
) -> None:
    provider = RecordingAcquisitionProvider()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "preflight-power-fail-closed"),
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
        power_inhibitor=RejectingPowerInhibitor(),
    )
    client, write_headers = _authenticated_client(runtime)
    try:
        initialization_id, _saved, session_ref = _open_ready_acquisition(
            client,
            write_headers,
            prefix="preflight-power-fail-closed",
        )

        session = runtime.owners.agent_runtime.query_acquisition_session(
            initialization_id=initialization_id
        )
        assert session is not None
        assert session.session_ref == session_ref
        assert session.status == "unavailable"
        assert session.reason_code == "power_inhibitor_test_rejected"
        assert provider.preflights == []
        observability = runtime.query_runtime_observability()
        assert observability["inhibitor"]["active_count"] == 0
        assert observability["responsibilities"] == []
    finally:
        runtime.close()


def test_acquisition_batch_retries_with_a_new_attempt_after_power_wait(
    tmp_path: Path,
) -> None:
    provider = RecordingAcquisitionProvider()
    inhibitor = SwitchablePowerInhibitor()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "batch-power-fail-closed"),
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
        power_inhibitor=inhibitor,
    )
    client, write_headers = _authenticated_client(runtime)
    try:
        _initialization_id, _saved, session_ref = _open_ready_acquisition(
            client,
            write_headers,
            prefix="batch-power-fail-closed",
        )
        request = AcquisitionBatchRequest(
            request_id="batch-power-fail-closed",
            route_policy="oa_first_then_institution",
            papers=(
                AcquisitionPaper(
                    paper_id="paper:power-fail-closed",
                    title="Power fail closed",
                    doi=None,
                    arxiv_id=None,
                    source_urls=(),
                ),
            ),
        )

        inhibitor.fail = True
        with pytest.raises(OwnerConflict, match="power_inhibitor_test_rejected"):
            runtime.owners.agent_runtime.acquire_literature(
                session_ref, request, provider
            )
        assert provider.batches == []
        observability = runtime.query_runtime_observability()
        assert observability["inhibitor"]["active_count"] == 0
        assert observability["responsibilities"] == []

        inhibitor.fail = False
        completed = runtime.owners.agent_runtime.acquire_literature(
            session_ref, request, provider
        )
        assert completed.status == "missing"
        assert [batch.request_id for batch in provider.batches] == [
            "batch-power-fail-closed"
        ]
        with runtime._database.read() as connection:
            assert connection.execute(
                text(
                    "SELECT attempt_count FROM ar_acquisition_requests WHERE "
                    "request_id = :request_id"
                ),
                {"request_id": request.request_id},
            ).scalar_one() == 2
    finally:
        runtime.close()


def test_completed_acquisition_execution_is_queryable_without_provider_replay(
    tmp_path: Path,
) -> None:
    provider = RecordingAcquisitionProvider()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "query-completed-acquisition"),
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
    )
    client, write_headers = _authenticated_client(runtime)
    try:
        initialization_id, _saved, session_ref = _open_ready_acquisition(
            client,
            write_headers,
            prefix="query-completed-acquisition",
        )
        quest_ref = "quest_query_completed_acquisition"
        runtime.owners.agent_runtime.bind_acquisition_session_to_quest(
            initialization_id,
            quest_ref,
        )
        request = AcquisitionBatchRequest(
            request_id="query-completed-acquisition-batch",
            route_policy="oa_first_then_institution",
            papers=(
                AcquisitionPaper(
                    paper_id="paper:query-completed",
                    title="Query completed acquisition",
                    doi=None,
                    arxiv_id=None,
                    source_urls=(),
                ),
            ),
        )
        assert runtime.owners.agent_runtime.query_acquisition_execution(
            session_ref,
            request.request_id,
        ) is None

        completed = runtime.owners.agent_runtime.acquire_literature(
            session_ref,
            request,
            provider,
        )
        queried = runtime.owners.agent_runtime.query_acquisition_execution(
            session_ref,
            request.request_id,
        )

        assert queried == completed
        assert [batch.request_id for batch in provider.batches] == [
            request.request_id
        ]
        root_scope = provider.batches[0].root_runtime_scope
        assert root_scope is not None
        assert root_scope["quest_ref"] == quest_ref
        assert root_scope["run_ref"] == request.request_id
        assert root_scope["root_session_ref"] == session_ref
        assert root_scope["generation"] == 1
        managed = runtime.owners.agent_runtime.query_managed_run(
            str(root_scope["run_ref"])
        )
        assert managed is not None and managed["status"] == "completed"
    finally:
        client.close()
        runtime.close()


def test_waiting_acquisition_request_is_queryable_without_provider_replay(
    tmp_path: Path,
) -> None:
    provider = RecordingAcquisitionProvider()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "query-waiting-acquisition"),
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
    )
    client, write_headers = _authenticated_client(runtime)
    try:
        _initialization_id, _saved, session_ref = _open_ready_acquisition(
            client,
            write_headers,
            prefix="query-waiting-acquisition",
        )
        request = AcquisitionBatchRequest(
            request_id="query-waiting-acquisition-batch",
            route_policy="oa_first_then_institution",
            papers=(
                AcquisitionPaper(
                    paper_id="paper:waiting",
                    title="Query waiting acquisition",
                    doi=None,
                    arxiv_id=None,
                    source_urls=(),
                ),
            ),
        )

        waiting = runtime.owners.agent_runtime.acquire_literature(
            session_ref,
            request,
            provider,
        )
        queried = runtime.owners.agent_runtime.query_acquisition_request(
            session_ref,
            request.request_id,
        )

        assert waiting.status == "waiting_user"
        assert queried == waiting
        assert runtime.owners.agent_runtime.query_acquisition_execution(
            session_ref,
            request.request_id,
        ) is None
        assert runtime.owners.agent_runtime.query_acquisition_request(
            session_ref,
            "query-waiting-acquisition-absent",
        ) is None
        assert [batch.request_id for batch in provider.batches] == [
            request.request_id
        ]
    finally:
        client.close()
        runtime.close()


def test_quest_deletion_terminates_acquisition_without_provider_replay(
    tmp_path: Path,
) -> None:
    provider = RecordingAcquisitionProvider()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "quest-deleted-acquisition"),
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
    )
    client, write_headers = _authenticated_client(runtime)
    owner = runtime.owners.agent_runtime
    try:
        initialization_id, _saved, session_ref = _open_ready_acquisition(
            client,
            write_headers,
            prefix="quest-deleted-acquisition",
        )
        quest_ref = "quest_deleted_acquisition"
        owner.bind_acquisition_session_to_quest(initialization_id, quest_ref)
        request = AcquisitionBatchRequest(
            request_id="quest-deleted-acquisition-batch",
            route_policy="oa_first_then_institution",
            papers=(
                AcquisitionPaper(
                    paper_id="paper:waiting",
                    title="A request terminated with its Quest",
                    doi=None,
                    arxiv_id=None,
                    source_urls=(),
                ),
            ),
        )

        waiting = owner.acquire_literature(session_ref, request, provider)
        assert waiting.status == "waiting_user"
        assert len(provider.batches) == 1
        assert len(provider.preflights) == 1

        terminated = owner.terminate_acquisition_session(quest_ref)
        assert terminated is not None
        assert terminated.session_ref == session_ref
        assert terminated.status == "cancelled"
        assert terminated.current_request_id is None
        assert terminated.slot_held is False
        assert terminated.reason_code == "quest_deleted"

        cancelled_request = owner.query_acquisition_request(
            session_ref,
            request.request_id,
        )
        assert cancelled_request is not None
        assert cancelled_request.status == "missing"
        [cancelled_item] = cancelled_request.results
        assert cancelled_item.status == "missing"
        assert cancelled_item.path is None
        assert cancelled_item.format is None
        assert cancelled_item.failure == {
            "code": "acquisition_session_cancelled",
            "detail": "Quest 已删除；该正文获取请求不会恢复。",
        }

        assert owner.terminate_acquisition_session(quest_ref) == terminated
        with pytest.raises(
            OwnerConflict,
            match="acquisition_session_cancelled",
        ):
            owner.acquire_literature(session_ref, request, provider)
        with pytest.raises(
            OwnerConflict,
            match="acquisition_session_cancelled",
        ):
            owner.prepare_acquisition_session(
                initialization_id=initialization_id,
                draft_revision=2,
                config={
                    "mode": "oa_then_institution",
                    "library_entry_url": (
                        "https://library.example.edu/resources"
                    ),
                },
                provider=provider,
            )
        assert len(provider.batches) == 1
        assert len(provider.preflights) == 1
    finally:
        client.close()
        runtime.close()


def test_quest_deletion_fences_an_inflight_acquisition_completion(
    tmp_path: Path,
) -> None:
    provider = BlockingAcquisitionProvider()
    inhibitor = SwitchablePowerInhibitor()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "quest-deleted-inflight-acquisition"),
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
        power_inhibitor=inhibitor,
        startup_power_probe=False,
    )
    client, write_headers = _authenticated_client(runtime)
    owner = runtime.owners.agent_runtime
    failures: list[BaseException] = []
    try:
        initialization_id, _saved, session_ref = _open_ready_acquisition(
            client,
            write_headers,
            prefix="quest-deleted-inflight-acquisition",
        )
        quest_ref = "quest_deleted_inflight_acquisition"
        owner.bind_acquisition_session_to_quest(initialization_id, quest_ref)
        request = AcquisitionBatchRequest(
            request_id="quest-deleted-inflight-acquisition-batch",
            route_policy="oa_first_then_institution",
            papers=(
                AcquisitionPaper(
                    paper_id="paper:deleted-inflight",
                    title="An inflight request fenced by Quest deletion",
                    doi=None,
                    arxiv_id=None,
                    source_urls=(),
                ),
            ),
        )

        def acquire() -> None:
            try:
                owner.acquire_literature(session_ref, request, provider)
            except BaseException as error:  # pragma: no cover - asserted below
                failures.append(error)

        thread = threading.Thread(target=acquire)
        thread.start()
        assert provider.started.wait(timeout=5)

        terminated = owner.terminate_acquisition_session(quest_ref)
        assert terminated is not None and terminated.status == "cancelled"
        managed = owner.query_managed_run(request.request_id)
        assert managed is not None and managed["status"] == "terminated"

        provider.release.set()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert len(failures) == 1
        assert isinstance(failures[0], OwnerConflict)
        assert str(failures[0]) == "acquisition_request_fence_stale"

        still_terminated = owner.query_acquisition_session(quest_ref=quest_ref)
        assert still_terminated is not None
        assert still_terminated.status == "cancelled"
        cancelled_request = owner.query_acquisition_request(
            session_ref,
            request.request_id,
        )
        assert cancelled_request is not None
        assert cancelled_request.status == "missing"
        assert cancelled_request.results[0].failure is not None
        assert (
            cancelled_request.results[0].failure["code"]
            == "acquisition_session_cancelled"
        )
        assert owner.query_managed_run(request.request_id)["status"] == "terminated"
        assert len(provider.batches) == 1
        assert owner.query_snapshot().facts["acquisition_active_slot_count"] == 0
        with runtime._database.read() as connection:
            responsibility = connection.execute(
                text(
                    "SELECT status, boundary, checkpoint_ref FROM "
                    "ar_execution_responsibilities WHERE effect_kind = "
                    "'acquisition' AND operation_ref = :request_id"
                ),
                {"request_id": request.request_id},
            ).one()
        assert responsibility.status == "finished"
        assert responsibility.boundary == "permanent_fence"
        assert responsibility.checkpoint_ref is None
        assert inhibitor.live_holders == set()
    finally:
        provider.release.set()
        client.close()
        runtime.close()


def test_quest_deletion_terminates_a_late_acquisition_root_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = RecordingAcquisitionProvider()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "quest-deleted-late-root-registration"),
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
    )
    client, write_headers = _authenticated_client(runtime)
    owner = runtime.owners.agent_runtime
    registration_started = threading.Event()
    continue_registration = threading.Event()
    failures: list[BaseException] = []
    try:
        initialization_id, _saved, session_ref = _open_ready_acquisition(
            client,
            write_headers,
            prefix="quest-deleted-late-root-registration",
        )
        quest_ref = "quest_deleted_late_root_registration"
        owner.bind_acquisition_session_to_quest(initialization_id, quest_ref)
        request = AcquisitionBatchRequest(
            request_id="quest-deleted-late-root-registration-batch",
            route_policy="oa_first_then_institution",
            papers=(
                AcquisitionPaper(
                    paper_id="paper:deleted-before-root-registration",
                    title="A request deleted before its Root run registration",
                    doi=None,
                    arxiv_id=None,
                    source_urls=(),
                ),
            ),
        )
        original_register = owner.register_external_root_task_scope

        def delayed_register(**values):
            registration_started.set()
            assert continue_registration.wait(timeout=5)
            return original_register(**values)

        monkeypatch.setattr(
            owner,
            "register_external_root_task_scope",
            delayed_register,
        )

        def acquire() -> None:
            try:
                owner.acquire_literature(session_ref, request, provider)
            except BaseException as error:  # pragma: no cover - asserted below
                failures.append(error)

        thread = threading.Thread(target=acquire)
        thread.start()
        assert registration_started.wait(timeout=5)

        terminated = owner.terminate_acquisition_session(quest_ref)
        assert terminated is not None and terminated.status == "cancelled"
        continue_registration.set()
        thread.join(timeout=5)

        assert not thread.is_alive()
        assert len(failures) == 1
        assert isinstance(failures[0], OwnerConflict)
        assert str(failures[0]) == "acquisition_request_fence_stale"
        assert provider.batches == []
        managed = owner.query_managed_run(request.request_id)
        assert managed is not None
        assert managed["status"] == "terminated"
        assert managed["terminal_reason"] == "acquisition_session_cancelled"
        with runtime._database.read() as connection:
            responsibility_count = connection.execute(
                text(
                    "SELECT COUNT(*) FROM ar_execution_responsibilities WHERE "
                    "root_run_ref = :session_ref AND operation_ref = :request_id"
                ),
                {
                    "session_ref": session_ref,
                    "request_id": request.request_id,
                },
            ).scalar_one()
        assert responsibility_count == 0
    finally:
        continue_registration.set()
        client.close()
        runtime.close()


def test_quest_deletion_finishes_waiting_acquisition_human_request(
    tmp_path: Path,
) -> None:
    provider = HumanRequestAcquisitionProvider()
    inhibitor = SwitchablePowerInhibitor()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "quest-deleted-waiting-acquisition"),
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
        power_inhibitor=inhibitor,
        startup_power_probe=False,
    )
    provider.owner = runtime.owners.agent_runtime
    client, write_headers = _authenticated_client(runtime)
    owner = runtime.owners.agent_runtime
    try:
        initialization_id, _saved, session_ref = _open_ready_acquisition(
            client,
            write_headers,
            prefix="quest-deleted-waiting-acquisition",
        )
        quest_ref = "quest_deleted_waiting_acquisition"
        owner.bind_acquisition_session_to_quest(initialization_id, quest_ref)
        request = AcquisitionBatchRequest(
            request_id="quest-deleted-waiting-acquisition-batch",
            route_policy="oa_first_then_institution",
            papers=(
                AcquisitionPaper(
                    paper_id="paper:deleted-waiting",
                    title="A waiting request terminated with its Quest",
                    doi=None,
                    arxiv_id=None,
                    source_urls=(),
                ),
            ),
        )

        waiting = owner.acquire_literature(session_ref, request, provider)
        assert waiting.status == "waiting_user"
        assert provider.human_request is not None
        request_ref = str(provider.human_request["request_ref"])
        open_request = owner.query_human_request(request_ref)
        assert open_request is not None
        assert open_request["status"] == "open"
        assert open_request["direct_waiters"][0]["status"] == "blocked"
        assert inhibitor.live_holders

        terminated = owner.terminate_acquisition_session(quest_ref)
        assert terminated is not None and terminated.status == "cancelled"

        withdrawn = owner.query_human_request(request_ref)
        assert withdrawn is not None
        assert withdrawn["status"] == "withdrawn"
        assert withdrawn["disposition"]["decision"] == "withdrawn"
        assert withdrawn["direct_waiters"][0]["status"] == "cancelled"
        cancelled_request = owner.query_acquisition_request(
            session_ref,
            request.request_id,
        )
        assert cancelled_request is not None
        assert cancelled_request.status == "missing"
        with runtime._database.read() as connection:
            responsibility = connection.execute(
                text(
                    "SELECT status, boundary, checkpoint_ref FROM "
                    "ar_execution_responsibilities WHERE effect_kind = "
                    "'acquisition' AND root_run_ref = :session_ref AND "
                    "operation_ref = :request_id"
                ),
                {
                    "session_ref": session_ref,
                    "request_id": request.request_id,
                },
            ).one()
        assert responsibility.status == "finished"
        assert responsibility.boundary == "permanent_fence"
        assert responsibility.checkpoint_ref is None
        assert inhibitor.live_holders == set()
    finally:
        client.close()
        runtime.close()


def test_quest_deletion_fences_failed_protection_before_provider(
    tmp_path: Path,
) -> None:
    provider = RecordingAcquisitionProvider()
    inhibitor = BlockingConfirmationPowerInhibitor()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "quest-deleted-protection-acquire"),
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
        power_inhibitor=inhibitor,
        startup_power_probe=False,
    )
    client, write_headers = _authenticated_client(runtime)
    owner = runtime.owners.agent_runtime
    failures: list[BaseException] = []
    try:
        initialization_id, _saved, session_ref = _open_ready_acquisition(
            client,
            write_headers,
            prefix="quest-deleted-protection-acquire",
        )
        quest_ref = "quest_deleted_protection_acquire"
        owner.bind_acquisition_session_to_quest(initialization_id, quest_ref)
        request = AcquisitionBatchRequest(
            request_id="quest-deleted-protection-acquire-batch",
            route_policy="oa_first_then_institution",
            papers=(
                AcquisitionPaper(
                    paper_id="paper:deleted-before-provider",
                    title="A request deleted before Provider admission",
                    doi=None,
                    arxiv_id=None,
                    source_urls=(),
                ),
            ),
        )
        inhibitor.block_next_confirmation = True

        def acquire() -> None:
            try:
                owner.acquire_literature(session_ref, request, provider)
            except BaseException as error:  # pragma: no cover - asserted below
                failures.append(error)

        thread = threading.Thread(target=acquire)
        thread.start()
        assert inhibitor.confirmation_started.wait(timeout=5)

        terminated = owner.terminate_acquisition_session(quest_ref)
        assert terminated is not None and terminated.status == "cancelled"
        inhibitor.continue_confirmation.set()
        thread.join(timeout=5)

        assert not thread.is_alive()
        assert len(failures) == 1
        assert isinstance(failures[0], OwnerConflict)
        assert str(failures[0]) == "acquisition_request_fence_stale"
        assert provider.batches == []
        with runtime._database.read() as connection:
            responsibility = connection.execute(
                text(
                    "SELECT status, boundary, checkpoint_ref FROM "
                    "ar_execution_responsibilities WHERE root_run_ref = "
                    ":session_ref AND operation_ref = :request_id"
                ),
                {
                    "session_ref": session_ref,
                    "request_id": request.request_id,
                },
            ).one()
        assert responsibility.status == "finished"
        assert responsibility.boundary == "permanent_fence"
        assert responsibility.checkpoint_ref is None
        assert inhibitor.live_holders == set()
    finally:
        inhibitor.continue_confirmation.set()
        client.close()
        runtime.close()


def test_quest_deletion_fences_inflight_acquisition_repreflight(
    tmp_path: Path,
) -> None:
    provider = BlockingRepreflightAcquisitionProvider()
    inhibitor = SwitchablePowerInhibitor()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "quest-deleted-repreflight"),
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
        power_inhibitor=inhibitor,
        startup_power_probe=False,
    )
    client, write_headers = _authenticated_client(runtime)
    owner = runtime.owners.agent_runtime
    failures: list[BaseException] = []
    try:
        initialization_id, _saved, session_ref = _open_ready_acquisition(
            client,
            write_headers,
            prefix="quest-deleted-repreflight",
        )
        quest_ref = "quest_deleted_repreflight"
        owner.bind_acquisition_session_to_quest(initialization_id, quest_ref)
        provider.block_next_preflight = True

        def repreflight() -> None:
            try:
                owner.prepare_acquisition_session(
                    initialization_id=initialization_id,
                    draft_revision=2,
                    config={
                        "mode": "oa_only",
                        "library_entry_url": (
                            "https://library.example.edu/resources"
                        ),
                    },
                    provider=provider,
                )
            except BaseException as error:  # pragma: no cover - asserted below
                failures.append(error)

        thread = threading.Thread(target=repreflight)
        thread.start()
        assert provider.preflight_started.wait(timeout=5)

        terminated = owner.terminate_acquisition_session(quest_ref)
        assert terminated is not None and terminated.status == "cancelled"
        provider.continue_preflight.set()
        thread.join(timeout=5)

        assert not thread.is_alive()
        assert len(failures) == 1
        assert isinstance(failures[0], OwnerConflict)
        assert str(failures[0]) == "acquisition_preflight_fence_stale"
        assert len(provider.preflights) == 2
        with runtime._database.read() as connection:
            responsibility = connection.execute(
                text(
                    "SELECT status, boundary, checkpoint_ref FROM "
                    "ar_execution_responsibilities WHERE root_run_ref = "
                    ":session_ref AND attempt_ref = 'acquisition_preflight_2'"
                ),
                {"session_ref": session_ref},
            ).one()
        assert responsibility.status == "finished"
        assert responsibility.boundary == "permanent_fence"
        assert responsibility.checkpoint_ref is None
        assert inhibitor.live_holders == set()
    finally:
        provider.continue_preflight.set()
        client.close()
        runtime.close()


def test_quest_deletion_replays_waiting_acquisition_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = RecordingAcquisitionProvider()
    inhibitor = SwitchablePowerInhibitor()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "quest-deleted-waiting-checkpoint"),
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
        power_inhibitor=inhibitor,
        startup_power_probe=False,
    )
    client, write_headers = _authenticated_client(runtime)
    owner = runtime.owners.agent_runtime
    try:
        initialization_id, _saved, session_ref = _open_ready_acquisition(
            client,
            write_headers,
            prefix="quest-deleted-waiting-checkpoint",
        )
        quest_ref = "quest_deleted_waiting_checkpoint"
        owner.bind_acquisition_session_to_quest(initialization_id, quest_ref)
        request = AcquisitionBatchRequest(
            request_id="quest-deleted-waiting-checkpoint-batch",
            route_policy="oa_first_then_institution",
            papers=(
                AcquisitionPaper(
                    paper_id="paper:waiting",
                    title="A waiting request with a committed checkpoint",
                    doi=None,
                    arxiv_id=None,
                    source_urls=(),
                ),
            ),
        )
        original_finish = runtime.runtime_protection.finish

        def lose_finish_ack(*_args, **_kwargs) -> None:
            raise RuntimeProtectionUnavailable(
                "simulated_acquisition_finish_ack_loss"
            )

        monkeypatch.setattr(runtime.runtime_protection, "finish", lose_finish_ack)
        with pytest.raises(
            RuntimeProtectionUnavailable,
            match="simulated_acquisition_finish_ack_loss",
        ):
            owner.acquire_literature(session_ref, request, provider)
        monkeypatch.setattr(runtime.runtime_protection, "finish", original_finish)

        waiting = owner.query_acquisition_session(session_ref=session_ref)
        assert waiting is not None and waiting.status == "waiting_user"
        terminated = owner.terminate_acquisition_session(quest_ref)
        assert terminated is not None and terminated.status == "cancelled"
        with runtime._database.read() as connection:
            responsibility = connection.execute(
                text(
                    "SELECT status, boundary, checkpoint_ref FROM "
                    "ar_execution_responsibilities WHERE root_run_ref = "
                    ":session_ref AND operation_ref = :request_id"
                ),
                {
                    "session_ref": session_ref,
                    "request_id": request.request_id,
                },
            ).one()
        assert responsibility.status == "finished"
        assert responsibility.boundary == "checkpoint"
        assert responsibility.checkpoint_ref is not None
        assert inhibitor.live_holders == set()
    finally:
        client.close()
        runtime.close()


def test_cancelled_quest_replays_terminal_acquisition_finish_ack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = RecordingAcquisitionProvider()
    inhibitor = SwitchablePowerInhibitor()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "quest-deleted-terminal-checkpoint"),
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
        power_inhibitor=inhibitor,
        startup_power_probe=False,
    )
    client, write_headers = _authenticated_client(runtime)
    owner = runtime.owners.agent_runtime
    try:
        initialization_id, _saved, session_ref = _open_ready_acquisition(
            client,
            write_headers,
            prefix="quest-deleted-terminal-checkpoint",
        )
        quest_ref = "quest_deleted_terminal_checkpoint"
        owner.bind_acquisition_session_to_quest(initialization_id, quest_ref)
        request = AcquisitionBatchRequest(
            request_id="quest-deleted-terminal-checkpoint-batch",
            route_policy="oa_first_then_institution",
            papers=(
                AcquisitionPaper(
                    paper_id="paper:terminal-before-delete",
                    title="A terminal request with a lost finish ACK",
                    doi=None,
                    arxiv_id=None,
                    source_urls=(),
                ),
            ),
        )
        original_finish = runtime.runtime_protection.finish

        def lose_finish_ack(*_args, **_kwargs) -> None:
            raise RuntimeProtectionUnavailable(
                "simulated_acquisition_finish_ack_loss"
            )

        monkeypatch.setattr(runtime.runtime_protection, "finish", lose_finish_ack)
        with pytest.raises(
            RuntimeProtectionUnavailable,
            match="simulated_acquisition_finish_ack_loss",
        ):
            owner.acquire_literature(session_ref, request, provider)
        ready = owner.query_acquisition_session(session_ref=session_ref)
        assert ready is not None
        assert ready.status == "ready"
        assert ready.current_request_id is None

        with pytest.raises(
            OwnerConflict,
            match="simulated_acquisition_finish_ack_loss",
        ):
            owner.terminate_acquisition_session(quest_ref)
        committed = owner.query_acquisition_session(quest_ref=quest_ref)
        assert committed is not None and committed.status == "cancelled"

        monkeypatch.setattr(runtime.runtime_protection, "finish", original_finish)
        replayed = owner.terminate_acquisition_session(quest_ref)
        assert replayed == committed
        with runtime._database.read() as connection:
            responsibility = connection.execute(
                text(
                    "SELECT status, boundary, checkpoint_ref FROM "
                    "ar_execution_responsibilities WHERE root_run_ref = "
                    ":session_ref AND operation_ref = :request_id"
                ),
                {
                    "session_ref": session_ref,
                    "request_id": request.request_id,
                },
            ).one()
        assert responsibility.status == "finished"
        assert responsibility.boundary == "checkpoint"
        assert responsibility.checkpoint_ref is not None
        assert inhibitor.live_holders == set()
    finally:
        client.close()
        runtime.close()


def test_acquisition_request_reconcile_replays_lost_finish_ack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = RecordingAcquisitionProvider()
    inhibitor = SwitchablePowerInhibitor()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "acquisition-reconcile-finish-ack"),
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
        power_inhibitor=inhibitor,
        startup_power_probe=False,
    )
    client, write_headers = _authenticated_client(runtime)
    owner = runtime.owners.agent_runtime
    try:
        initialization_id, _saved, session_ref = _open_ready_acquisition(
            client,
            write_headers,
            prefix="acquisition-reconcile-finish-ack",
        )
        quest_ref = "quest_acquisition_reconcile_finish_ack"
        owner.bind_acquisition_session_to_quest(initialization_id, quest_ref)
        request = AcquisitionBatchRequest(
            request_id="acquisition-reconcile-finish-ack-batch",
            route_policy="oa_first_then_institution",
            papers=(
                AcquisitionPaper(
                    paper_id="paper:reconcile-finish-ack",
                    title="A terminal request reconciled after a lost finish ACK",
                    doi=None,
                    arxiv_id=None,
                    source_urls=(),
                ),
            ),
        )
        original_finish = runtime.runtime_protection.finish

        def lose_finish_ack(*_args, **_kwargs) -> None:
            raise RuntimeProtectionUnavailable(
                "simulated_acquisition_finish_ack_loss"
            )

        monkeypatch.setattr(runtime.runtime_protection, "finish", lose_finish_ack)
        with pytest.raises(
            RuntimeProtectionUnavailable,
            match="simulated_acquisition_finish_ack_loss",
        ):
            owner.acquire_literature(session_ref, request, provider)
        assert inhibitor.live_holders
        managed = owner.query_managed_run(request.request_id)
        assert managed is not None and managed["status"] == "running"

        monkeypatch.setattr(runtime.runtime_protection, "finish", original_finish)
        reconciled = owner.query_acquisition_request(
            session_ref,
            request.request_id,
        )
        assert reconciled is not None and reconciled.status == "missing"
        assert len(provider.batches) == 1
        completed = owner.query_managed_run(request.request_id)
        assert completed is not None and completed["status"] == "completed"
        assert inhibitor.live_holders == set()
    finally:
        client.close()
        runtime.close()


def test_terminal_acquisition_reconcile_replays_predecessor_finish_ack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = RepeatedPendingAcquisitionProvider(pending_reconciliations=0)
    inhibitor = SwitchablePowerInhibitor()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "acquisition-predecessor-finish-ack"),
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
        power_inhibitor=inhibitor,
        startup_power_probe=False,
    )
    client, write_headers = _authenticated_client(runtime)
    owner = runtime.owners.agent_runtime
    try:
        initialization_id, _saved, session_ref = _open_ready_acquisition(
            client,
            write_headers,
            prefix="acquisition-predecessor-finish-ack",
        )
        quest_ref = "quest_acquisition_predecessor_finish_ack"
        owner.bind_acquisition_session_to_quest(initialization_id, quest_ref)
        request = AcquisitionBatchRequest(
            request_id="acquisition-predecessor-finish-ack-batch",
            route_policy="oa_first_then_institution",
            papers=(
                AcquisitionPaper(
                    paper_id="paper:predecessor-finish-ack",
                    title="A terminal reconciliation with a lost predecessor ACK",
                    doi=None,
                    arxiv_id=None,
                    source_urls=(),
                ),
            ),
        )

        pending = owner.acquire_literature(session_ref, request, provider)
        assert pending.status == "waiting_user"
        original_finish = runtime.runtime_protection.finish
        finish_calls = 0

        def lose_predecessor_finish(*args, **kwargs) -> None:
            nonlocal finish_calls
            finish_calls += 1
            if finish_calls == 2:
                raise RuntimeProtectionUnavailable(
                    "simulated_predecessor_finish_ack_loss"
                )
            original_finish(*args, **kwargs)

        monkeypatch.setattr(
            runtime.runtime_protection,
            "finish",
            lose_predecessor_finish,
        )
        with pytest.raises(
            RuntimeProtectionUnavailable,
            match="simulated_predecessor_finish_ack_loss",
        ):
            owner.acquire_literature(session_ref, request, provider)
        monkeypatch.setattr(runtime.runtime_protection, "finish", original_finish)

        reconciled = owner.query_acquisition_request(
            session_ref,
            request.request_id,
        )
        assert reconciled is not None and reconciled.status == "missing"
        completed = owner.query_managed_run(request.request_id)
        assert completed is not None and completed["status"] == "completed"
        with runtime._database.read() as connection:
            responsibilities = connection.execute(
                text(
                    "SELECT status, boundary FROM "
                    "ar_execution_responsibilities WHERE root_run_ref = "
                    ":session_ref AND operation_ref = :request_id ORDER BY "
                    "created_at"
                ),
                {
                    "session_ref": session_ref,
                    "request_id": request.request_id,
                },
            ).all()
        assert responsibilities == [
            ("finished", "permanent_fence"),
            ("finished", "checkpoint"),
        ]
        assert inhibitor.live_holders == set()
    finally:
        client.close()
        runtime.close()


def test_quest_bound_acquisition_human_request_resumes_same_task_next_attempt(
    tmp_path: Path,
) -> None:
    provider = HumanRequestAcquisitionProvider()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "acquisition-root-human-request"),
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
    )
    provider.owner = runtime.owners.agent_runtime
    client, write_headers = _authenticated_client(runtime)
    try:
        initialization_id, _saved, session_ref = _open_ready_acquisition(
            client,
            write_headers,
            prefix="acquisition-root-human-request",
        )
        quest_ref = "quest_acquisition_root_human_request"
        runtime.owners.agent_runtime.bind_acquisition_session_to_quest(
            initialization_id,
            quest_ref,
        )
        request = AcquisitionBatchRequest(
            request_id="acquisition-root-human-request-batch",
            route_policy="oa_first_then_institution",
            papers=(
                AcquisitionPaper(
                    paper_id="paper:root-human-request",
                    title="Root HumanRequest lifecycle",
                    doi=None,
                    arxiv_id=None,
                    source_urls=(),
                ),
            ),
        )

        waiting = runtime.owners.agent_runtime.acquire_literature(
            session_ref,
            request,
            provider,
        )
        assert waiting.status == "waiting_user"
        [first_provider_request] = provider.batches
        first_scope = first_provider_request.root_runtime_scope
        assert first_scope is not None
        assert first_scope["run_ref"] == request.request_id
        assert first_scope["root_session_ref"] == session_ref
        assert first_scope["generation"] == 1
        suspended = runtime.owners.agent_runtime.query_managed_run(
            str(first_scope["run_ref"])
        )
        assert suspended is not None and suspended["status"] == "suspended"

        replayed_waiting = runtime.owners.agent_runtime.acquire_literature(
            session_ref,
            request,
            provider,
        )
        assert replayed_waiting == waiting
        assert len(provider.batches) == 1
        assert provider.reconciliations == []
        unchanged_session = runtime.owners.agent_runtime.query_acquisition_session(
            session_ref=session_ref
        )
        assert unchanged_session is not None
        assert unchanged_session.status == "waiting_user"
        assert unchanged_session.slot_held is False
        assert unchanged_session.current_request_id == request.request_id
        unchanged_scope = runtime.owners.agent_runtime.query_managed_run(
            str(first_scope["run_ref"])
        )
        assert unchanged_scope is not None
        assert unchanged_scope["status"] == "suspended"
        assert unchanged_scope["attempt_generation"] == 1

        assert provider.human_request is not None
        request_ref = str(provider.human_request["request_ref"])
        runtime.owners.human_collaboration.respond_to_human_request(
            request_ref,
            decision="deferred",
            facts={},
            note="Continue this exact task without the optional input.",
            idempotency_key="acquisition-root-human-request-response",
        )
        resumed = runtime.owners.agent_runtime.query_managed_run(
            str(first_scope["run_ref"])
        )
        assert resumed is not None and resumed["status"] == "running"

        settled = runtime.owners.agent_runtime.acquire_literature(
            session_ref,
            request,
            provider,
        )
        assert settled.status == "missing"
        [reconcile_request] = provider.reconciliations
        next_scope = reconcile_request.root_runtime_scope
        assert next_scope is not None
        assert next_scope["run_ref"] == first_scope["run_ref"]
        assert next_scope["root_session_ref"] == first_scope["root_session_ref"]
        assert next_scope["runtime_binding_hash"] == first_scope[
            "runtime_binding_hash"
        ]
        assert next_scope["generation"] == 2
        assert next_scope["attempt_ref"] != first_scope["attempt_ref"]
        assert next_scope["fence_ref"] != first_scope["fence_ref"]
        completed = runtime.owners.agent_runtime.query_managed_run(
            str(next_scope["run_ref"])
        )
        assert completed is not None and completed["status"] == "completed"
        disposition = runtime.owners.agent_runtime.query_human_request(request_ref)
        assert disposition is not None
        assert disposition["status"] == "unsatisfied"
        assert disposition["direct_waiters"][0]["status"] == "consumed"
    finally:
        client.close()
        runtime.close()


@pytest.mark.parametrize(
    "corrupted_column",
    ("request_json", "request_hash", "route_policy"),
)
def test_completed_acquisition_query_rejects_corrupted_owner_request_identity(
    tmp_path: Path,
    corrupted_column: str,
) -> None:
    provider = RecordingAcquisitionProvider()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / f"corrupt-{corrupted_column}"),
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
    )
    client, write_headers = _authenticated_client(runtime)
    try:
        _initialization_id, _saved, session_ref = _open_ready_acquisition(
            client,
            write_headers,
            prefix=f"corrupt-{corrupted_column}",
        )
        request = AcquisitionBatchRequest(
            request_id=f"corrupt-{corrupted_column}-batch",
            route_policy="oa_first_then_institution",
            papers=(
                AcquisitionPaper(
                    paper_id="paper:corrupted-owner-identity",
                    title="Corrupted Owner request identity",
                    doi=None,
                    arxiv_id=None,
                    source_urls=(),
                ),
            ),
        )
        runtime.owners.agent_runtime.acquire_literature(
            session_ref,
            request,
            provider,
        )

        with runtime._database.write() as connection:
            if corrupted_column == "request_json":
                stored_json = connection.execute(
                    text(
                        "SELECT request_json FROM ar_acquisition_requests WHERE "
                        "request_id = :request_id"
                    ),
                    {"request_id": request.request_id},
                ).scalar_one()
                connection.execute(
                    text(
                        "UPDATE ar_acquisition_requests SET request_json = "
                        ":request_json WHERE request_id = :request_id"
                    ),
                    {
                        "request_id": request.request_id,
                        "request_json": " " + stored_json,
                    },
                )
            elif corrupted_column == "request_hash":
                connection.execute(
                    text(
                        "UPDATE ar_acquisition_requests SET request_hash = "
                        ":request_hash WHERE request_id = :request_id"
                    ),
                    {
                        "request_id": request.request_id,
                        "request_hash": "f" * 64,
                    },
                )
            else:
                connection.execute(text("PRAGMA ignore_check_constraints = ON"))
                connection.execute(
                    text(
                        "UPDATE ar_acquisition_requests SET route_policy = "
                        ":route_policy WHERE request_id = :request_id"
                    ),
                    {
                        "request_id": request.request_id,
                        "route_policy": "corrupted_route_policy",
                    },
                )

        with pytest.raises(
            OwnerConflict,
            match="^acquisition_request_identity_conflict$",
        ):
            runtime.owners.agent_runtime.query_acquisition_execution(
                session_ref,
                request.request_id,
            )
        assert [batch.request_id for batch in provider.batches] == [
            request.request_id
        ]
    finally:
        client.close()
        runtime.close()


def test_terminal_acquisition_commit_freezes_artifact_digest_and_size(
    tmp_path: Path,
) -> None:
    provider = ObtainingAcquisitionProvider()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "frozen-acquisition-proof"),
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
    )
    client, write_headers = _authenticated_client(runtime)
    try:
        _initialization_id, _saved, session_ref = _open_ready_acquisition(
            client,
            write_headers,
            prefix="frozen-acquisition-proof",
        )
        request = AcquisitionBatchRequest(
            request_id="frozen-acquisition-proof-batch",
            route_policy="oa_first_then_institution",
            papers=(
                AcquisitionPaper(
                    paper_id="paper:frozen-acquisition-proof",
                    title="Frozen acquisition proof",
                    doi=None,
                    arxiv_id=None,
                    source_urls=(),
                ),
            ),
        )

        completed = runtime.owners.agent_runtime.acquire_literature(
            session_ref,
            request,
            provider,
        )
        item = completed.results[0]

        assert item.content_sha256 == hashlib.sha256(provider.content).hexdigest()
        assert item.content_bytes == len(provider.content)
        with runtime._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT results_json, results_hash FROM "
                    "ar_acquisition_requests WHERE request_id = :request_id"
                ),
                {"request_id": request.request_id},
            ).one()
        persisted = acquisition_json([item.as_dict()])
        assert row.results_json == persisted
        assert row.results_hash == acquisition_hash([item.as_dict()])

        assert item.path is not None
        Path(item.path).write_bytes(b"mutated-after-terminal-owner-commit")
        queried = runtime.owners.agent_runtime.query_acquisition_execution(
            session_ref,
            request.request_id,
        )
        assert queried is not None
        assert queried.results[0].content_sha256 == item.content_sha256
        assert queried.results[0].content_bytes == item.content_bytes
        assert len(provider.batches) == 1
    finally:
        client.close()
        runtime.close()


def test_quest_acquisition_session_waits_without_automatic_human_request(
    tmp_path: Path,
) -> None:
    provider = RecordingAcquisitionProvider()
    data_root = prepare_data_root(tmp_path / "data")
    runtime = build_production_runtime(
        data_root,
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
    )
    client, write_headers = _authenticated_client(runtime)
    try:
        opened = client.post(
            "/api/v1/quest-initializations",
            headers=_write_headers(write_headers, "open"),
            json={},
        ).json()
        initialization_id = opened["initialization_id"]
        draft = dict(opened["quest_draft"]["value"])
        draft["literature"] = {
            **draft["literature"],
            "mode": "oa_then_institution",
            "library_entry_url": "https://library.example.edu/resources",
        }
        saved_response = client.put(
            f"/api/v1/quest-initializations/{initialization_id}/draft",
            headers=_write_headers(write_headers, "save-library"),
            json={
                "expected_draft_revision": opened["quest_draft"]["revision"],
                "expected_draft_hash": opened["quest_draft"]["hash"],
                "draft": draft,
            },
        )
        saved_response.raise_for_status()
        saved = saved_response.json()

        prepared_response = client.post(
            f"/api/v1/quest-initializations/{initialization_id}/acquisition-session",
            headers=_write_headers(write_headers, "prepare-library"),
            json={
                "expected_draft_revision": saved["quest_draft"]["revision"],
                "expected_draft_hash": saved["quest_draft"]["hash"],
            },
        )
        prepared_response.raise_for_status()
        prepared = prepared_response.json()
        session = prepared["acquisition_session"]
        assert session["status"] == "ready"
        assert session["freshness"] == "current"
        assert session["browser_context"] == "verified"
        assert session["slot_held"] is False
        session_ref = session["session_ref"]
        assert len(provider.preflights) == 1

        replay = client.post(
            f"/api/v1/quest-initializations/{initialization_id}/acquisition-session",
            headers=_write_headers(write_headers, "prepare-library-replay"),
            json={
                "expected_draft_revision": saved["quest_draft"]["revision"],
                "expected_draft_hash": saved["quest_draft"]["hash"],
            },
        )
        replay.raise_for_status()
        assert replay.json()["acquisition_session"]["session_ref"] == session_ref
        assert len(provider.preflights) == 1

        first_batch = runtime.owners.agent_runtime.acquire_literature(
            session_ref,
            AcquisitionBatchRequest(
                request_id="acq-one",
                route_policy="oa_first_then_institution",
                papers=(
                    AcquisitionPaper(
                        paper_id="paper:one",
                        title="One",
                        doi=None,
                        arxiv_id=None,
                        source_urls=(),
                    ),
                ),
            ),
            provider,
        )
        assert first_batch.status == "missing"
        second_batch = runtime.owners.agent_runtime.acquire_literature(
            session_ref,
            AcquisitionBatchRequest(
                request_id="acq-waiting",
                route_policy="oa_first_then_institution",
                papers=(
                    AcquisitionPaper(
                        paper_id="paper:waiting",
                        title="Waiting",
                        doi=None,
                        arxiv_id=None,
                        source_urls=(),
                    ),
                ),
            ),
            provider,
        )
        assert second_batch.status == "waiting_user"
        assert second_batch.request_id == "acq-waiting"
        _assert_waiting_without_automatic_human_request(
            runtime,
            session_ref=session_ref,
            request_id=second_batch.request_id,
        )
        assert [batch.request_id for batch in provider.batches] == [
            "acq-one",
            "acq-waiting",
        ]
    finally:
        client.close()
        runtime.close()


def test_mixed_waiting_batch_keeps_obtained_item_without_automatic_human_request(
    tmp_path: Path,
) -> None:
    provider = PartialAcquisitionProvider()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "data"),
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
    )
    client, write_headers = _authenticated_client(runtime)
    try:
        _, _, session_ref = _open_ready_acquisition(
            client, write_headers, prefix="partial"
        )
        request = AcquisitionBatchRequest(
            request_id="partial-batch",
            route_policy="oa_first_then_institution",
            papers=(
                AcquisitionPaper(
                    paper_id="paper:obtained",
                    title="Already obtained",
                    doi="10.1000/obtained",
                    arxiv_id=None,
                    source_urls=(),
                ),
                AcquisitionPaper(
                    paper_id="paper:waiting",
                    title="Needs login",
                    doi="10.1000/waiting",
                    arxiv_id=None,
                    source_urls=(),
                ),
            ),
        )
        first = runtime.owners.agent_runtime.acquire_literature(
            session_ref, request, provider
        )
        assert first.status == "waiting_user"
        assert [item.status for item in first.results] == [
            "obtained",
            "waiting_user",
        ]
        _assert_waiting_without_automatic_human_request(
            runtime, session_ref=session_ref, request_id=request.request_id
        )
        assert len(provider.batches) == 1
    finally:
        client.close()
        runtime.close()


def test_mixed_wait_freezes_proof_without_automatic_human_request(
    tmp_path: Path,
) -> None:
    provider = PartialAcquisitionProvider()
    data_root = prepare_data_root(tmp_path / "mixed-legacy-obtained-proof")
    runtime = build_production_runtime(
        data_root,
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
    )
    client, write_headers = _authenticated_client(runtime)
    _, _, session_ref = _open_ready_acquisition(
        client, write_headers, prefix="mixed-legacy-obtained-proof"
    )
    request = AcquisitionBatchRequest(
        request_id="mixed-legacy-obtained-proof-batch",
        route_policy="oa_first_then_institution",
        papers=(
            AcquisitionPaper(
                paper_id="paper:obtained",
                title="Legacy obtained item",
                doi="10.1000/legacy-obtained-proof",
                arxiv_id=None,
                source_urls=(),
            ),
            AcquisitionPaper(
                paper_id="paper:waiting",
                title="Waiting item",
                doi="10.1000/legacy-waiting-proof",
                arxiv_id=None,
                source_urls=(),
            ),
        ),
    )
    try:
        first = runtime.owners.agent_runtime.acquire_literature(
            session_ref, request, provider
        )
        assert first.status == "waiting_user"
        assert [item.status for item in first.results] == [
            "obtained",
            "waiting_user",
        ]
        assert first.results[0].content_sha256 == hashlib.sha256(
            b"%PDF-1.4\nfixture acquisition artifact\n"
        ).hexdigest()
        _assert_waiting_without_automatic_human_request(
            runtime, session_ref=session_ref, request_id=request.request_id
        )
        assert len(provider.batches) == 1
    finally:
        client.close()
        runtime.close()


def test_mixed_wait_keeps_technical_state_without_automatic_human_request(
    tmp_path: Path,
) -> None:
    provider = PartialAcquisitionProvider()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "mixed-obtained-artifact-drift"),
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
    )
    client, write_headers = _authenticated_client(runtime)
    try:
        _, _, session_ref = _open_ready_acquisition(
            client, write_headers, prefix="mixed-obtained-artifact-drift"
        )
        request = AcquisitionBatchRequest(
            request_id="mixed-obtained-artifact-drift-batch",
            route_policy="oa_first_then_institution",
            papers=(
                AcquisitionPaper(
                    paper_id="paper:obtained",
                    title="Frozen obtained item",
                    doi="10.1000/frozen-obtained-drift",
                    arxiv_id=None,
                    source_urls=(),
                ),
                AcquisitionPaper(
                    paper_id="paper:waiting",
                    title="Waiting item",
                    doi="10.1000/waiting-drift",
                    arxiv_id=None,
                    source_urls=(),
                ),
            ),
        )
        first = runtime.owners.agent_runtime.acquire_literature(
            session_ref, request, provider
        )
        assert first.status == "waiting_user"
        assert first.results[0].content_sha256 == hashlib.sha256(
            b"%PDF-1.4\nfixture acquisition artifact\n"
        ).hexdigest()
        assert first.results[0].path is not None
        Path(first.results[0].path).write_bytes(b"mutated after Owner proof")
        _assert_waiting_without_automatic_human_request(
            runtime, session_ref=session_ref, request_id=request.request_id
        )
        assert len(provider.batches) == 1
    finally:
        client.close()
        runtime.close()


def test_reconciled_invalid_artifact_is_terminal_and_never_replayed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = InvalidArtifactAfterReconcileProvider()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "reconciled-invalid-artifact"),
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
    )
    client, write_headers = _authenticated_client(runtime)
    try:
        _, _, session_ref = _open_ready_acquisition(
            client, write_headers, prefix="reconciled-invalid-artifact"
        )
        request = AcquisitionBatchRequest(
            request_id="reconciled-invalid-artifact-batch",
            route_policy="oa_first_then_institution",
            papers=(
                AcquisitionPaper(
                    paper_id="paper:invalid-artifact",
                    title="Invalid reconciled artifact",
                    doi="10.1000/reconciled-invalid-artifact",
                    arxiv_id=None,
                    source_urls=(),
                ),
            ),
        )
        first = runtime.owners.agent_runtime.acquire_literature(
            session_ref, request, provider
        )
        assert first.status == "waiting_user"

        recorded_boundaries: list[tuple[str, str]] = []
        original_record_runtime_boundary = (
            agent_runtime_module.record_runtime_boundary
        )

        def record_terminal_boundary(connection, **values) -> None:
            recorded_boundaries.append(
                (values["identity"].responsibility_ref, values["boundary"])
            )
            original_record_runtime_boundary(connection, **values)

        monkeypatch.setattr(
            agent_runtime_module,
            "record_runtime_boundary",
            record_terminal_boundary,
        )
        terminal = runtime.owners.agent_runtime.acquire_literature(
            session_ref, request, provider
        )

        assert terminal.status == "missing"
        assert terminal.results == (
            AcquisitionItemResult(
                paper_id="paper:invalid-artifact",
                status="missing",
                path=None,
                format=None,
                failure={
                    "code": "acquisition_artifact_invalid",
                    "detail": (
                        "Owner 无法验证 Acquisition artifact bytes；"
                        "该 item 已终止且不会自动重放 Provider。"
                    ),
                },
            ),
        )
        assert {boundary for _ref, boundary in recorded_boundaries} == {
            "checkpoint",
            "permanent_fence",
        }
        assert runtime.owners.agent_runtime.query_acquisition_execution(
            session_ref, request.request_id
        ) == terminal

        replay = runtime.owners.agent_runtime.acquire_literature(
            session_ref, request, provider
        )
        assert replay == terminal
        assert len(provider.batches) == 1
        assert len(provider.reconcile_calls) == 1
    finally:
        client.close()
        runtime.close()


def test_upgrade_era_wait_does_not_open_an_automatic_human_request(
    tmp_path: Path,
) -> None:
    provider = RecordingAcquisitionProvider()
    data_root = prepare_data_root(tmp_path / "legacy-waiting-item-hash")
    runtime = build_production_runtime(
        data_root,
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
    )
    client, write_headers = _authenticated_client(runtime)
    _, _, session_ref = _open_ready_acquisition(
        client, write_headers, prefix="legacy-waiting-item-hash"
    )
    request = AcquisitionBatchRequest(
        request_id="legacy-waiting-item-hash-batch",
        route_policy="oa_first_then_institution",
        papers=(
            AcquisitionPaper(
                paper_id="paper:waiting",
                title="Resume an upgrade-era library wait",
                doi="10.1000/legacy-waiting-item-hash",
                arxiv_id=None,
                source_urls=(),
            ),
        ),
    )
    try:
        first = runtime.owners.agent_runtime.acquire_literature(
            session_ref, request, provider
        )
        assert first.status == "waiting_user"
        _assert_waiting_without_automatic_human_request(
            runtime, session_ref=session_ref, request_id=request.request_id
        )
        assert len(provider.batches) == 1
    finally:
        client.close()
        runtime.close()


def test_acquisition_session_claim_is_single_slot_cas(
    tmp_path: Path,
) -> None:
    provider = BlockingAcquisitionProvider()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "data"),
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
    )
    client, write_headers = _authenticated_client(runtime)
    failures: list[BaseException] = []
    try:
        _, _, session_ref = _open_ready_acquisition(
            client, write_headers, prefix="single-slot"
        )

        def first_call() -> None:
            try:
                runtime.owners.agent_runtime.acquire_literature(
                    session_ref,
                    AcquisitionBatchRequest(
                        request_id="slot-one",
                        route_policy="oa_first_then_institution",
                        papers=(
                            AcquisitionPaper(
                                paper_id="paper:slot-one",
                                title="Slot one",
                                doi=None,
                                arxiv_id=None,
                                source_urls=(),
                            ),
                        ),
                    ),
                    provider,
                )
            except BaseException as error:  # pragma: no cover - asserted below
                failures.append(error)

        thread = threading.Thread(target=first_call)
        thread.start()
        assert provider.started.wait(timeout=5)
        with pytest.raises(OwnerConflict, match="acquisition_session_busy"):
            runtime.owners.agent_runtime.acquire_literature(
                session_ref,
                AcquisitionBatchRequest(
                    request_id="slot-two",
                    route_policy="oa_first_then_institution",
                    papers=(
                        AcquisitionPaper(
                            paper_id="paper:slot-two",
                            title="Slot two",
                            doi=None,
                            arxiv_id=None,
                            source_urls=(),
                        ),
                    ),
                ),
                provider,
            )
        provider.release.set()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert failures == []
        assert len(provider.batches) == 1
        assert runtime.owners.agent_runtime.query_snapshot().facts[
            "acquisition_active_slot_count"
        ] == 0
    finally:
        provider.release.set()
        client.close()
        runtime.close()


def test_restart_reconciles_unknown_acquisition_without_replaying_provider(
    tmp_path: Path,
) -> None:
    provider = ReconcilingAcquisitionProvider()
    data_root = prepare_data_root(tmp_path / "data")
    runtime = build_production_runtime(
        data_root,
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
    )
    client, write_headers = _authenticated_client(runtime)
    initialization_id, saved, session_ref = _open_ready_acquisition(
        client, write_headers, prefix="reconcile"
    )
    request = AcquisitionBatchRequest(
        request_id="unknown-batch",
        route_policy="oa_first_then_institution",
        papers=(
            AcquisitionPaper(
                paper_id="paper:unknown",
                title="Unknown in flight",
                doi="10.1000/unknown",
                arxiv_id=None,
                source_urls=(),
            ),
        ),
    )
    checkpoint = [
        {
            "paper_id": "paper:unknown",
            "status": "waiting_user",
            "path": None,
            "format": None,
            "failure": {
                "code": "acquisition_reconciliation_required",
                "detail": "既有下载操作需要对账。",
            },
        }
    ]
    now = 1_725_000_000.0
    with runtime.owners.agent_runtime._database.write() as connection:
        connection.execute(
            text(
                "UPDATE ar_acquisition_sessions SET status = 'acquiring', "
                "slot_held = 1, current_request_id = :request_id WHERE "
                "session_ref = :session_ref"
            ),
            {"session_ref": session_ref, "request_id": request.request_id},
        )
        connection.execute(
            text(
                "INSERT INTO ar_acquisition_requests (request_id, session_ref, "
                "request_json, request_hash, route_policy, status, results_json, "
                "results_hash, attempt_count, created_at, updated_at) VALUES "
                "(:request_id, :session_ref, :request_json, :request_hash, "
                ":route_policy, 'running', :results_json, :results_hash, 1, :now, :now)"
            ),
            {
                "request_id": request.request_id,
                "session_ref": session_ref,
                "request_json": acquisition_json(request.identity_payload()),
                "request_hash": validate_batch_request(request),
                "route_policy": request.route_policy,
                "results_json": acquisition_json(checkpoint),
                "results_hash": acquisition_hash(checkpoint),
                "now": now,
            },
        )
        connection.execute(
            text(
                "UPDATE agent_runtime_state SET acquisition_active_slot_count = 1 "
                "WHERE singleton = 'owner'"
            )
        )
    client.close()
    runtime.close()

    restarted = build_production_runtime(
        data_root,
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
    )
    restarted_client, restarted_headers = _authenticated_client(restarted)
    try:
        # Unknown external outcome is a technical reconciliation boundary.  It
        # must not manufacture a second HumanRequest or require the user to
        # repeat an already consumed login action.
        assert restarted.owners.agent_runtime.query_human_requests() == ()
        execution = restarted.owners.agent_runtime.acquire_literature(
            session_ref, request, provider
        )
        assert execution.status == "missing"
        assert provider.acquire_calls == 0
        assert len(provider.reconcile_calls) == 1
        assert [paper.paper_id for paper in provider.reconcile_calls[0].papers] == [
            "paper:unknown"
        ]
        assert restarted.owners.agent_runtime.query_human_requests() == ()
    finally:
        restarted_client.close()
        restarted.close()


def test_repeated_acquisition_reconciliation_keeps_original_hold_until_terminal(
    tmp_path: Path,
) -> None:
    provider = RepeatedPendingAcquisitionProvider()
    data_root = prepare_data_root(tmp_path / "repeated-reconciliation")
    runtime = build_production_runtime(
        data_root,
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
    )
    client, write_headers = _authenticated_client(runtime)
    _, _, session_ref = _open_ready_acquisition(
        client, write_headers, prefix="repeated-reconciliation"
    )
    request = AcquisitionBatchRequest(
        request_id="repeated-reconciliation-batch",
        route_policy="oa_first_then_institution",
        papers=(
            AcquisitionPaper(
                paper_id="paper:repeated-reconciliation",
                title="Repeated reconciliation",
                doi="10.1000/repeated-reconciliation",
                arxiv_id=None,
                source_urls=(),
            ),
        ),
    )
    try:
        first = runtime.owners.agent_runtime.acquire_literature(
            session_ref, request, provider
        )
        assert first.status == "waiting_user"
        assert runtime.query_runtime_observability()["inhibitor"]["active_count"] == 1

        second = runtime.owners.agent_runtime.acquire_literature(
            session_ref, request, provider
        )
        assert second.status == "waiting_user"
        assert runtime.query_runtime_observability()["inhibitor"]["active_count"] == 1
    finally:
        client.close()
        runtime.close()

    restarted = build_production_runtime(
        data_root,
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
    )
    try:
        terminal = restarted.owners.agent_runtime.acquire_literature(
            session_ref, request, provider
        )
        assert terminal.status == "missing"
        assert provider.acquire_calls == 1
        assert len(provider.reconcile_calls) == 2
        evidence = restarted.query_runtime_observability()
        assert evidence["inhibitor"]["active_count"] == 0
        assert evidence["responsibilities"] == []
    finally:
        restarted.close()


def test_acquisition_terminal_closes_only_the_unsettled_responsibility_frontier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = RepeatedPendingAcquisitionProvider(pending_reconciliations=64)
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "bounded-terminal-frontier"),
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
    )
    client, write_headers = _authenticated_client(runtime)
    _, _, session_ref = _open_ready_acquisition(
        client, write_headers, prefix="bounded-terminal-frontier"
    )
    request = AcquisitionBatchRequest(
        request_id="bounded-terminal-frontier-batch",
        route_policy="oa_first_then_institution",
        papers=(
            AcquisitionPaper(
                paper_id="paper:bounded-terminal-frontier",
                title="Bounded terminal responsibility frontier",
                doi="10.1000/bounded-terminal-frontier",
                arxiv_id=None,
                source_urls=(),
            ),
        ),
    )
    try:
        first = runtime.owners.agent_runtime.acquire_literature(
            session_ref, request, provider
        )
        assert first.status == "waiting_user"
        original_finish = runtime.runtime_protection.finish
        finish_ack_lost = True

        def lose_one_checkpoint_finish(*args, **kwargs) -> None:
            nonlocal finish_ack_lost
            if finish_ack_lost:
                finish_ack_lost = False
                raise RuntimeProtectionUnavailable(
                    "simulated_acquisition_finish_ack_loss"
                )
            original_finish(*args, **kwargs)

        monkeypatch.setattr(
            runtime.runtime_protection, "finish", lose_one_checkpoint_finish
        )
        with pytest.raises(
            RuntimeProtectionUnavailable,
            match="simulated_acquisition_finish_ack_loss",
        ):
            runtime.owners.agent_runtime.acquire_literature(
                session_ref, request, provider
            )
        monkeypatch.setattr(runtime.runtime_protection, "finish", original_finish)

        for _generation in range(63):
            pending = runtime.owners.agent_runtime.acquire_literature(
                session_ref, request, provider
            )
            assert pending.status == "waiting_user"
        before_terminal = runtime.query_runtime_observability()
        assert [
            item["attempt_ref"] for item in before_terminal["responsibilities"]
        ] == ["acquisition_attempt_1"]

        recorded_boundaries: list[tuple[str, str]] = []
        original_record_runtime_boundary = (
            agent_runtime_module.record_runtime_boundary
        )

        def record_terminal_boundary(connection, **values) -> None:
            identity = values["identity"]
            recorded_boundaries.append(
                (identity.responsibility_ref, values["boundary"])
            )
            original_record_runtime_boundary(connection, **values)

        monkeypatch.setattr(
            agent_runtime_module,
            "record_runtime_boundary",
            record_terminal_boundary,
        )
        terminal = runtime.owners.agent_runtime.acquire_literature(
            session_ref, request, provider
        )

        assert terminal.status == "missing"
        assert len(recorded_boundaries) == 2
        assert {boundary for _ref, boundary in recorded_boundaries} == {
            "checkpoint",
            "permanent_fence",
        }
        evidence = runtime.query_runtime_observability()
        assert evidence["responsibilities"] == []
        assert evidence["durable_waiting"] == []
        with runtime._database.read() as connection:
            receipts = connection.execute(
                text(
                    "SELECT responsibility.attempt_ref, receipt.boundary FROM "
                    "ar_execution_responsibilities AS responsibility JOIN "
                    "ar_runtime_boundary_receipts AS receipt ON "
                    "receipt.responsibility_ref = responsibility.responsibility_ref "
                    "WHERE responsibility.root_run_ref = :session_ref AND "
                    "responsibility.operation_ref = :request_id ORDER BY "
                    "responsibility.created_at"
                ),
                {"session_ref": session_ref, "request_id": request.request_id},
            ).all()
        assert len(receipts) == 66
        assert receipts[0] == ("acquisition_attempt_1", "permanent_fence")
        assert all(boundary == "checkpoint" for _attempt, boundary in receipts[1:])
    finally:
        client.close()
        runtime.close()


def test_technical_reconciliation_without_provider_support_never_opens_a_human_request(
    tmp_path: Path,
) -> None:
    provider = RecordingAcquisitionProvider()
    data_root = prepare_data_root(tmp_path / "unsupported-reconcile")
    runtime = build_production_runtime(
        data_root,
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
    )
    client, write_headers = _authenticated_client(runtime)
    _, _, session_ref = _open_ready_acquisition(
        client, write_headers, prefix="unsupported-reconcile"
    )
    request = AcquisitionBatchRequest(
        request_id="unsupported-reconcile-batch",
        route_policy="oa_first_then_institution",
        papers=(
            AcquisitionPaper(
                paper_id="paper:unsupported-reconcile",
                title="Unknown operation with no provider reconciliation seam",
                doi="10.1000/unsupported-reconcile",
                arxiv_id=None,
                source_urls=(),
            ),
        ),
    )
    checkpoint = [
        {
            "paper_id": "paper:unsupported-reconcile",
            "status": "waiting_user",
            "path": None,
            "format": None,
            "failure": {
                "code": "acquisition_reconciliation_required",
                "detail": "既有下载操作需要对账。",
            },
        }
    ]
    with runtime.owners.agent_runtime._database.write() as connection:
        connection.execute(
            text(
                "UPDATE ar_acquisition_sessions SET status = 'acquiring', "
                "slot_held = 1, current_request_id = :request_id WHERE "
                "session_ref = :session_ref"
            ),
            {"session_ref": session_ref, "request_id": request.request_id},
        )
        connection.execute(
            text(
                "INSERT INTO ar_acquisition_requests (request_id, session_ref, "
                "request_json, request_hash, route_policy, status, results_json, "
                "results_hash, attempt_count, created_at, updated_at) VALUES "
                "(:request_id, :session_ref, :request_json, :request_hash, "
                ":route_policy, 'running', :results_json, :results_hash, 1, "
                ":now, :now)"
            ),
            {
                "request_id": request.request_id,
                "session_ref": session_ref,
                "request_json": acquisition_json(request.identity_payload()),
                "request_hash": validate_batch_request(request),
                "route_policy": request.route_policy,
                "results_json": acquisition_json(checkpoint),
                "results_hash": acquisition_hash(checkpoint),
                "now": 1_725_000_001.0,
            },
        )
        connection.execute(
            text(
                "UPDATE agent_runtime_state SET acquisition_active_slot_count = 1 "
                "WHERE singleton = 'owner'"
            )
        )
    client.close()
    runtime.close()

    restarted = build_production_runtime(
        data_root,
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
    )
    try:
        assert restarted.owners.agent_runtime.query_human_requests() == ()
        first = restarted.owners.agent_runtime.acquire_literature(
            session_ref, request, provider
        )
        assert first.status == "waiting_user"
        assert first.results[0].failure == {
            "code": "acquisition_reconciliation_required",
            "detail": (
                "既有下载操作尚未形成可验证终态；系统将先对账，"
                "不会重复启动下载。"
            ),
        }
        assert provider.batches == []
        assert restarted.owners.agent_runtime.query_human_requests() == ()
    finally:
        restarted.close()

    replay = build_production_runtime(
        data_root,
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
    )
    try:
        assert replay.owners.agent_runtime.query_human_requests() == ()
        second = replay.owners.agent_runtime.acquire_literature(
            session_ref, request, provider
        )
        assert second.status == "waiting_user"
        assert provider.batches == []
        assert replay.owners.agent_runtime.query_human_requests() == ()
    finally:
        replay.close()


def test_waiting_acquisition_has_no_automatic_human_request_before_recovery(
    tmp_path: Path,
) -> None:
    provider = CrashAfterConsumedWaiterProvider()
    data_root = prepare_data_root(tmp_path / "consumed-waiter-crash")
    runtime = build_production_runtime(
        data_root,
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
    )
    client, write_headers = _authenticated_client(runtime)
    _, _, session_ref = _open_ready_acquisition(
        client, write_headers, prefix="consumed-crash"
    )
    request = AcquisitionBatchRequest(
        request_id="consumed-crash-batch",
        route_policy="oa_first_then_institution",
        papers=(
            AcquisitionPaper(
                paper_id="paper:waiting",
                title="Needs one login and then exact reconciliation",
                doi="10.1000/consumed-crash",
                arxiv_id=None,
                source_urls=(),
            ),
        ),
    )
    try:
        first = runtime.owners.agent_runtime.acquire_literature(
            session_ref, request, provider
        )
        assert first.status == "waiting_user"
        _assert_waiting_without_automatic_human_request(
            runtime, session_ref=session_ref, request_id=request.request_id
        )
        assert provider.acquire_calls == 1
        assert provider.reconcile_calls == []
    finally:
        client.close()
        runtime.close()


def test_oa_capable_wait_does_not_open_an_automatic_human_request(
    tmp_path: Path,
) -> None:
    provider = RecordingAcquisitionProvider()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "oa-route"),
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
    )
    client, write_headers = _authenticated_client(runtime)
    _, _, session_ref = _open_ready_acquisition(
        client, write_headers, prefix="oa-route"
    )
    request = AcquisitionBatchRequest(
        request_id="oa-route-batch",
        route_policy="oa_first_then_institution",
        papers=(
            AcquisitionPaper(
                paper_id="paper:waiting",
                title="Use the narrower lawful route",
                doi="10.1000/oa-route",
                arxiv_id=None,
                source_urls=(),
            ),
        ),
    )
    try:
        assert runtime.owners.agent_runtime.acquire_literature(
            session_ref, request, provider
        ).status == "waiting_user"
        _assert_waiting_without_automatic_human_request(
            runtime, session_ref=session_ref, request_id=request.request_id
        )
        assert len(provider.batches) == 1
    finally:
        client.close()
        runtime.close()


def test_waiting_acquisition_config_stays_technical_without_auto_human_request(
    tmp_path: Path,
) -> None:
    provider = RecordingAcquisitionProvider()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "oa-route-config-drift"),
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
    )
    client, write_headers = _authenticated_client(runtime)
    _, _, session_ref = _open_ready_acquisition(
        client, write_headers, prefix="oa-route-config-drift"
    )
    request = AcquisitionBatchRequest(
        request_id="oa-route-config-drift-batch",
        route_policy="oa_first_then_institution",
        papers=(
            AcquisitionPaper(
                paper_id="paper:waiting",
                title="Do not consume a response against drifted configuration",
                doi="10.1000/oa-route-config-drift",
                arxiv_id=None,
                source_urls=(),
            ),
        ),
    )
    try:
        assert runtime.owners.agent_runtime.acquire_literature(
            session_ref, request, provider
        ).status == "waiting_user"
        session = runtime.owners.agent_runtime.query_acquisition_session(
            session_ref=session_ref
        )
        assert session is not None
        assert session.config_hash
        _assert_waiting_without_automatic_human_request(
            runtime, session_ref=session_ref, request_id=request.request_id
        )
        assert len(provider.batches) == 1
    finally:
        client.close()
        runtime.close()


def test_material_capable_wait_does_not_open_an_automatic_human_request(
    tmp_path: Path,
) -> None:
    provider = RecordingAcquisitionProvider()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "provided-material"),
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
    )
    client, write_headers = _authenticated_client(runtime)
    _, _, session_ref = _open_ready_acquisition(
        client, write_headers, prefix="provided-material"
    )
    request = AcquisitionBatchRequest(
        request_id="provided-material-batch",
        route_policy="oa_first_then_institution",
        papers=(
            AcquisitionPaper(
                paper_id="paper:waiting",
                title="Use the exact accepted lawful copy",
                doi="10.1000/provided-material",
                arxiv_id=None,
                source_urls=(),
            ),
        ),
    )
    try:
        assert runtime.owners.agent_runtime.acquire_literature(
            session_ref, request, provider
        ).status == "waiting_user"
        _assert_waiting_without_automatic_human_request(
            runtime, session_ref=session_ref, request_id=request.request_id
        )
        assert len(provider.batches) == 1
    finally:
        client.close()
        runtime.close()


def test_two_waiting_items_do_not_open_automatic_human_requests(
    tmp_path: Path,
) -> None:
    provider = AllWaitingAcquisitionProvider()
    data_root = prepare_data_root(tmp_path / "provided-material-partial")
    runtime = build_production_runtime(
        data_root,
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
    )
    client, write_headers = _authenticated_client(runtime)
    _, _, session_ref = _open_ready_acquisition(
        client, write_headers, prefix="provided-material-partial"
    )
    request = AcquisitionBatchRequest(
        request_id="provided-material-partial-batch",
        route_policy="oa_first_then_institution",
        papers=(
            AcquisitionPaper(
                paper_id="paper:first",
                title="First exact blocked item",
                doi="10.1000/provided-material-first",
                arxiv_id=None,
                source_urls=(),
            ),
            AcquisitionPaper(
                paper_id="paper:second",
                title="Second exact blocked item",
                doi="10.1000/provided-material-second",
                arxiv_id=None,
                source_urls=(),
            ),
        ),
    )
    try:
        first = runtime.owners.agent_runtime.acquire_literature(
            session_ref, request, provider
        )
        assert first.status == "waiting_user"
        human_requests = _current_acquisition_human_requests(
            runtime, session_ref, request.request_id
        )
        assert human_requests == []
        _assert_waiting_without_automatic_human_request(
            runtime, session_ref=session_ref, request_id=request.request_id
        )
        assert [item.status for item in first.results] == [
            "waiting_user",
            "waiting_user",
        ]
        assert len(provider.batches) == 1
    finally:
        client.close()
        runtime.close()


def test_oa_recovery_wait_does_not_open_an_automatic_human_request(
    tmp_path: Path,
) -> None:
    provider = CrashAfterOaResumeProvider()
    data_root = prepare_data_root(tmp_path / "oa-route-crash")
    runtime = build_production_runtime(
        data_root,
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
    )
    client, write_headers = _authenticated_client(runtime)
    _, _, session_ref = _open_ready_acquisition(
        client, write_headers, prefix="oa-route-crash"
    )
    request = AcquisitionBatchRequest(
        request_id="oa-route-crash-batch",
        route_policy="oa_first_then_institution",
        papers=(
            AcquisitionPaper(
                paper_id="paper:waiting",
                title="Keep the exact OA-only route across recovery",
                doi="10.1000/oa-route-crash",
                arxiv_id=None,
                source_urls=(),
            ),
        ),
    )
    try:
        assert runtime.owners.agent_runtime.acquire_literature(
            session_ref, request, provider
        ).status == "waiting_user"
        _assert_waiting_without_automatic_human_request(
            runtime, session_ref=session_ref, request_id=request.request_id
        )
        assert provider.acquire_calls == 1
        assert provider.reconcile_calls == []
    finally:
        client.close()
        runtime.close()


def test_material_recovery_wait_does_not_open_an_automatic_human_request(
    tmp_path: Path,
) -> None:
    provider = RecordingAcquisitionProvider()
    data_root = prepare_data_root(tmp_path / "material-route-crash")
    runtime = build_production_runtime(
        data_root,
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
    )
    client, write_headers = _authenticated_client(runtime)
    _, _, session_ref = _open_ready_acquisition(
        client, write_headers, prefix="material-route-crash"
    )
    request = AcquisitionBatchRequest(
        request_id="material-route-crash-batch",
        route_policy="oa_first_then_institution",
        papers=(
            AcquisitionPaper(
                paper_id="paper:waiting",
                title="Recover the accepted copy",
                doi="10.1000/material-route-crash",
                arxiv_id=None,
                source_urls=(),
            ),
        ),
    )
    try:
        assert runtime.owners.agent_runtime.acquire_literature(
            session_ref, request, provider
        ).status == "waiting_user"
        _assert_waiting_without_automatic_human_request(
            runtime, session_ref=session_ref, request_id=request.request_id
        )
        assert len(provider.batches) == 1
    finally:
        client.close()
        runtime.close()


def test_deepfetch_requires_a_current_ready_acquisition_session(
    tmp_path: Path,
) -> None:
    provider = RecordingAcquisitionProvider()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "data"),
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
    )
    client, write_headers = _authenticated_client(runtime)
    try:
        opened = client.post(
            "/api/v1/quest-initializations",
            headers=_write_headers(write_headers, "deepfetch-open"),
            json={},
        ).json()
        initialization_id = opened["initialization_id"]
        probed_response = client.post(
            f"/api/v1/quest-initializations/{initialization_id}/compute-probe",
            headers=_write_headers(write_headers, "deepfetch-compute"),
            json={"selected_device_uuids": ["GPU-acquisition-1"]},
        )
        probed_response.raise_for_status()
        probed = probed_response.json()
        draft = dict(probed["quest_draft"]["value"])
        draft.update(
            {
                "goal": "核查一个需要真实检索的首问题。",
                "completion_criteria": "返回可审计的论文账本和全文状态。",
                "route": "deepfetch",
                "literature": {
                    **draft["literature"],
                    "mode": "oa_only",
                    "library_entry_url": "",
                },
            }
        )
        saved_response = client.put(
            f"/api/v1/quest-initializations/{initialization_id}/draft",
            headers=_write_headers(write_headers, "deepfetch-draft"),
            json={
                "expected_draft_revision": probed["quest_draft"]["revision"],
                "expected_draft_hash": probed["quest_draft"]["hash"],
                "draft": draft,
            },
        )
        saved_response.raise_for_status()
        saved = saved_response.json()

        rejected = client.post(
            f"/api/v1/quest-initializations/{initialization_id}/proposal-generations",
            headers=_write_headers(write_headers, "deepfetch-before-preflight"),
            json={
                "expected_draft_revision": saved["quest_draft"]["revision"],
                "expected_draft_hash": saved["quest_draft"]["hash"],
            },
        )
        assert rejected.status_code == 409
        assert rejected.json()["detail"]["code"] == "acquisition_session_required"

        prepared_response = client.post(
            f"/api/v1/quest-initializations/{initialization_id}/acquisition-session",
            headers=_write_headers(write_headers, "deepfetch-preflight"),
            json={
                "expected_draft_revision": saved["quest_draft"]["revision"],
                "expected_draft_hash": saved["quest_draft"]["hash"],
            },
        )
        prepared_response.raise_for_status()
        prepared = prepared_response.json()
        session = prepared["acquisition_session"]
        assert session["status"] == "ready"
        assert session["freshness"] == "current"

        queued_response = client.post(
            f"/api/v1/quest-initializations/{initialization_id}/proposal-generations",
            headers=_write_headers(write_headers, "deepfetch-after-preflight"),
            json={
                "expected_draft_revision": saved["quest_draft"]["revision"],
                "expected_draft_hash": saved["quest_draft"]["hash"],
            },
        )
        assert queued_response.status_code == 202
        request = runtime.owners.human_collaboration.query_next_deepfetch_request()
        assert request is not None
        assert request.acquisition_session_ref == session["session_ref"]
        bound_session = runtime.owners.agent_runtime.query_acquisition_session(
            session_ref=session["session_ref"]
        )
        assert bound_session is not None
        assert request.acquisition_config_hash == bound_session.config_hash
        assert (
            request.acquisition_runtime_binding_hash
            == bound_session.runtime_binding_hash
        )
    finally:
        client.close()
        runtime.close()
