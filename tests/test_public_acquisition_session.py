from __future__ import annotations

import hashlib
import threading
from dataclasses import replace
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
)
from meta_research.composition import build_production_runtime
from meta_research.deepfetch import (
    DeepFetchProviderRequest,
    DeepFetchResult,
    DeepFetchRuntimeBinding,
    DeepFetchUnavailable,
)
from meta_research.paths import prepare_data_root
from meta_research.owners.common import OwnerConflict
from meta_research.owners.research_memory import AssetIntakeRequest
from meta_research.quest_drafting import HostComputeDevice, HostComputeSnapshot
from meta_research.runtime_protection import (
    InhibitorLease,
    RuntimeProtectionUnavailable,
)
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


class AcquisitionWaitingDeepFetchProvider:
    """Test seam that exercises the hosted Acquisition port through the worker."""

    def __init__(self, acquisition_provider: RecordingAcquisitionProvider) -> None:
        self.acquisition_provider = acquisition_provider
        self.agent_runtime = None
        self.requests: list[DeepFetchProviderRequest] = []

    def runtime_binding(self) -> DeepFetchRuntimeBinding:
        return DeepFetchRuntimeBinding(
            provider_ref="test/acquisition-aware-deepfetch",
            provider_version="1",
            model_ref="test-model",
            harness_ref="test-harness",
            capability_bindings=("web-search-live", "web-fetch-live"),
        )

    def execute(self, request: DeepFetchProviderRequest) -> DeepFetchResult:
        assert self.agent_runtime is not None
        self.requests.append(request)
        batch = AcquisitionBatchRequest(
            request_id="deepfetch-exact-batch-1",
            route_policy="oa_first_then_institution",
            papers=(
                AcquisitionPaper(
                    paper_id="paper:waiting",
                    title="Institutional full text candidate",
                    doi="10.1000/waiting",
                    arxiv_id=None,
                    source_urls=("https://example.org/paper",),
                ),
            ),
        )
        execution = self.agent_runtime.acquire_literature(
            request.acquisition_session_ref,
            batch,
            self.acquisition_provider,
        )
        native_session_ref = (
            request.native_session_ref or "native-acquisition-recovery-1"
        )
        if execution.status == "waiting_user":
            raise DeepFetchUnavailable(
                "deepfetch_acquisition_waiting_user",
                durable_outcome="pending",
                native_session_ref=native_session_ref,
            )
        return DeepFetchResult(
            completion="limited",
            summary="已核查一篇候选论文，但合法全文路由未取得正文。",
            papers=(
                {
                    "title": "Institutional full text candidate",
                    "url": "https://example.org/paper",
                    "doi": "10.1000/waiting",
                    "source_kind": "publisher",
                    "fulltext_status": "unavailable",
                    "retrieved_at": "2026-08-22T00:00:00Z",
                },
            ),
            fulltexts=(),
            limitations=("合法全文路由未返回可用正文。",),
            native_session_ref=native_session_ref,
            adapter_kind="test_acquisition_aware_deepfetch",
            web_evidence={
                "schema_ref": "meta-research/deepfetch-web-evidence/v1",
                "search_event_count": 1,
                "fetch_event_count": 1,
                "trace_hash": "e" * 64,
            },
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


def _respond_to_acquisition_human_request(
    runtime,
    *,
    session_ref: str,
    request_id: str,
    key: str,
) -> dict[str, object]:
    requests = [
        item
        for item in runtime.owners.agent_runtime.query_human_requests()
        if item["status"] == "open"
        and item["kind"] == "library_reconnect"
        and item["target_assertion"].get("session_ref") == session_ref
        and item["target_assertion"].get("acquisition_request_id") == request_id
    ]
    assert len(requests) == 1
    runtime.owners.human_collaboration.respond_to_human_request(
        requests[0]["request_ref"],
        decision="provided",
        facts={"route": "institutional_browser_reconnected"},
        note="The existing controlled browser login was restored.",
        idempotency_key=f"{key}-response",
    )
    return requests[0]


def _current_acquisition_human_request(runtime, session_ref: str, request_id: str):
    requests = [
        item
        for item in runtime.owners.agent_runtime.query_human_requests()
        if item["kind"] == "library_reconnect"
        and item["target_assertion"].get("session_ref") == session_ref
        and item["target_assertion"].get("acquisition_request_id") == request_id
    ]
    assert len(requests) == 1
    return requests[0]


def _current_acquisition_human_requests(runtime, session_ref: str, request_id: str):
    return [
        item
        for item in runtime.owners.agent_runtime.query_human_requests()
        if item["kind"] == "library_reconnect"
        and item["target_assertion"].get("session_ref") == session_ref
        and item["target_assertion"].get("acquisition_request_id") == request_id
    ]


def _respond_with_accepted_material(
    runtime,
    human_request: dict[str, object],
    *,
    paper_id: str,
    key: str,
    content: bytes,
) -> None:
    intake = runtime.owners.research_memory.submit_asset_intake(
        AssetIntakeRequest(
            source_kind="file",
            custody_mode="managed",
            display_name=f"{key}.pdf",
            media_type="application/pdf",
            content=content,
            provenance={"human_request_ref": human_request["request_ref"]},
            asynchronous=False,
        ),
        idempotency_key=f"{key}-intake",
    )
    assert intake.asset is not None
    asset = intake.asset
    runtime.owners.human_collaboration.respond_to_human_request(
        human_request["request_ref"],
        decision="provided",
        facts={
            "acquisition_paper_id": paper_id,
            "material_source_ref": asset.memory_ref,
            "material_version_ref": asset.version_ref,
            "material_content_hash": asset.content_hash,
            "material_manifest_hash": asset.manifest_hash,
            "material_acceptance_receipt_ref": asset.receipt.receipt_ref,
        },
        note="Use this accepted copy for the exact blocked item.",
        idempotency_key=f"{key}-response",
    )


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
        _initialization_id, _saved, session_ref = _open_ready_acquisition(
            client,
            write_headers,
            prefix="query-completed-acquisition",
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


def test_quest_acquisition_session_reuses_identity_batches_and_browser_after_restart(
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
        with pytest.raises(
            OwnerConflict, match="acquisition_human_request_not_released"
        ):
            runtime.owners.agent_runtime.acquire_literature(
                session_ref,
                replace(second_batch.request, request_id="acq-waiting"),
                provider,
            )
        assert [batch.request_id for batch in provider.batches] == [
            "acq-one",
            "acq-waiting",
        ]
        _respond_to_acquisition_human_request(
            runtime,
            session_ref=session_ref,
            request_id="acq-waiting",
            key="restore-waiting-library",
        )
        restored = client.post(
            f"/api/v1/quest-initializations/{initialization_id}/acquisition-session",
            headers=_write_headers(write_headers, "restore-waiting-library"),
            json={
                "expected_draft_revision": saved["quest_draft"]["revision"],
                "expected_draft_hash": saved["quest_draft"]["hash"],
            },
        )
        restored.raise_for_status()
        assert restored.json()["acquisition_session"]["status"] == "ready"
        resumed = runtime.owners.agent_runtime.acquire_literature(
            session_ref,
            replace(second_batch.request, request_id="acq-waiting"),
            provider,
        )
        assert resumed.status == "missing"
        assert resumed.request_id == "acq-waiting"
        assert [batch.request_id for batch in provider.batches] == [
            "acq-one",
            "acq-waiting",
            "acq-waiting",
        ]
        assert all(
            batch.route_policy == "oa_first_then_institution"
            for batch in provider.batches
        )
        assert all(
            batch.session_ref == session_ref
            and batch.browser_context_ref == "browser-context-authenticated-1"
            for batch in provider.batches
        )
        assert runtime.owners.agent_runtime.query_snapshot().facts[
            "acquisition_active_slot_count"
        ] == 0
    finally:
        client.close()
        runtime.close()

    restarted = build_production_runtime(
        data_root,
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
    )
    try:
        recovered = restarted.owners.agent_runtime.query_acquisition_session(
            initialization_id=initialization_id
        )
        assert recovered is not None
        assert recovered.session_ref == session_ref
        assert recovered.browser_context_ref == "browser-context-authenticated-1"
        assert recovered.slot_held is False
        assert recovered.request_count == 2
        assert restarted.owners.agent_runtime.query_snapshot().facts[
            "acquisition_active_slot_count"
        ] == 0
    finally:
        restarted.close()


def test_waiting_batch_retries_only_the_affected_item(
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
        initialization_id, saved, session_ref = _open_ready_acquisition(
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

        _respond_to_acquisition_human_request(
            runtime,
            session_ref=session_ref,
            request_id="partial-batch",
            key="partial-restore",
        )

        restored = client.post(
            f"/api/v1/quest-initializations/{initialization_id}/acquisition-session",
            headers=_write_headers(write_headers, "partial-restore"),
            json={
                "expected_draft_revision": saved["quest_draft"]["revision"],
                "expected_draft_hash": saved["quest_draft"]["hash"],
            },
        )
        restored.raise_for_status()
        resumed = runtime.owners.agent_runtime.acquire_literature(
            session_ref, request, provider
        )

        assert resumed.status == "obtained"
        assert [paper.paper_id for paper in provider.batches[0].papers] == [
            "paper:obtained",
            "paper:waiting",
        ]
        assert [paper.paper_id for paper in provider.batches[1].papers] == [
            "paper:waiting"
        ]
        assert resumed.results[0] == first.results[0]
        assert resumed.results[1].status == "obtained"
    finally:
        client.close()
        runtime.close()


def test_mixed_legacy_wait_resume_freezes_proof_for_previous_obtained_item(
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
    initialization_id, saved, session_ref = _open_ready_acquisition(
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
        human_request = _current_acquisition_human_request(
            runtime, session_ref, request.request_id
        )
        legacy_results = [
            {
                "paper_id": "paper:obtained",
                "status": "obtained",
                "path": first.results[0].path,
                "format": "pdf",
                "failure": None,
            },
            {
                "paper_id": "paper:waiting",
                "status": "waiting_user",
                "path": None,
                "format": None,
                "failure": {
                    "code": "institutional_login_required",
                    "detail": "请恢复机构登录。",
                },
            },
        ]
        with runtime.owners.agent_runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE ar_acquisition_requests SET results_json = :results_json, "
                    "results_hash = :results_hash WHERE request_id = :request_id"
                ),
                {
                    "results_json": acquisition_json(legacy_results),
                    "results_hash": acquisition_hash(legacy_results),
                    "request_id": request.request_id,
                },
            )
    finally:
        client.close()
        runtime.close()

    restarted = build_production_runtime(
        data_root,
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
    )
    restarted_client, restarted_headers = _authenticated_client(restarted)
    try:
        response = restarted_client.post(
            f"/api/v1/human-requests/{human_request['request_ref']}/responses",
            headers=_write_headers(
                restarted_headers, "mixed-legacy-obtained-proof-response"
            ),
            json={
                "decision": "provided",
                "facts": {"route": "institutional_browser_reconnected"},
                "note": "The existing controlled browser login was restored.",
            },
        )
        assert response.status_code == 201
        restored = restarted_client.post(
            f"/api/v1/quest-initializations/{initialization_id}/acquisition-session",
            headers=_write_headers(
                restarted_headers, "mixed-legacy-obtained-proof-preflight"
            ),
            json={
                "expected_draft_revision": saved["quest_draft"]["revision"],
                "expected_draft_hash": saved["quest_draft"]["hash"],
            },
        )
        restored.raise_for_status()

        resumed = restarted.owners.agent_runtime.acquire_literature(
            session_ref, request, provider
        )

        assert resumed.status == "obtained"
        assert resumed.results[0].content_sha256 == hashlib.sha256(
            b"%PDF-1.4\nfixture acquisition artifact\n"
        ).hexdigest()
        assert resumed.results[0].content_bytes == 38
        queried = restarted.owners.agent_runtime.query_acquisition_execution(
            session_ref, request.request_id
        )
        assert queried == resumed
        consumed = restarted.owners.agent_runtime.query_human_request(
            human_request["request_ref"]
        )
        assert consumed is not None
        assert consumed["revision"] == human_request["revision"]
        assert consumed["direct_waiters"][0]["status"] == "consumed"
        assert [paper.paper_id for paper in provider.batches[1].papers] == [
            "paper:waiting"
        ]
    finally:
        restarted_client.close()
        restarted.close()


def test_mixed_wait_resume_terminalizes_previous_obtained_artifact_drift(
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
        initialization_id, saved, session_ref = _open_ready_acquisition(
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

        _respond_to_acquisition_human_request(
            runtime,
            session_ref=session_ref,
            request_id=request.request_id,
            key="mixed-obtained-artifact-drift",
        )
        restored = client.post(
            f"/api/v1/quest-initializations/{initialization_id}/acquisition-session",
            headers=_write_headers(
                write_headers, "mixed-obtained-artifact-drift-preflight"
            ),
            json={
                "expected_draft_revision": saved["quest_draft"]["revision"],
                "expected_draft_hash": saved["quest_draft"]["hash"],
            },
        )
        restored.raise_for_status()

        resumed = runtime.owners.agent_runtime.acquire_literature(
            session_ref, request, provider
        )

        assert resumed.status == "partial"
        assert resumed.results[0] == AcquisitionItemResult(
            paper_id="paper:obtained",
            status="missing",
            path=None,
            format=None,
            failure={
                "code": "acquisition_artifact_drift",
                "detail": (
                    "Acquisition artifact bytes 与 Owner 已冻结 proof 不一致；"
                    "拒绝重新签名且不会自动重放 Provider。"
                ),
            },
        )
        assert resumed.results[1].status == "obtained"
        assert resumed.results[1].content_sha256 is not None
        assert runtime.owners.agent_runtime.query_acquisition_execution(
            session_ref, request.request_id
        ) == resumed
        replay = runtime.owners.agent_runtime.acquire_literature(
            session_ref, request, provider
        )
        assert replay == resumed
        assert len(provider.batches) == 2
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


def test_upgrade_preserves_legacy_waiting_item_hash_for_public_reconnect(
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
    initialization_id, saved, session_ref = _open_ready_acquisition(
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
        human_request = _current_acquisition_human_request(
            runtime, session_ref, request.request_id
        )

        # Freeze the exact persisted shape emitted before artifact proofs were
        # introduced: five item fields in results_json, and the HumanRequest
        # target bound to that five-field canonical item hash.
        legacy_result = {
            "paper_id": "paper:waiting",
            "status": "waiting_user",
            "path": None,
            "format": None,
            "failure": {
                "code": "institutional_login_required",
                "detail": "请在既有受控浏览器中恢复图书馆登录。",
            },
        }
        legacy_results = [legacy_result]
        legacy_target = {
            **human_request["target_assertion"],
            "acquisition_item_hash": acquisition_hash(legacy_result),
        }
        legacy_contract = {
            "quest_ref": human_request["quest_ref"],
            "kind": human_request["kind"],
            "obligation": human_request["obligation"],
            "business_purpose": human_request["business_purpose"],
            "target_assertion": legacy_target,
            "acceptance_conditions": human_request["acceptance_conditions"],
            "required_authorization": human_request["required_authorization"],
            "expires_at": human_request["expires_at"],
        }
        with runtime.owners.agent_runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE ar_acquisition_requests SET results_json = :results_json, "
                    "results_hash = :results_hash WHERE request_id = :request_id"
                ),
                {
                    "results_json": acquisition_json(legacy_results),
                    "results_hash": acquisition_hash(legacy_results),
                    "request_id": request.request_id,
                },
            )
            connection.execute(
                text(
                    "UPDATE owner_human_requests SET target_assertion_json = "
                    ":target_json, target_assertion_hash = :target_hash, "
                    "identity_hash = :identity_hash WHERE request_ref = :request_ref"
                ),
                {
                    "target_json": acquisition_json(legacy_target),
                    "target_hash": acquisition_hash(legacy_target),
                    "identity_hash": acquisition_hash(legacy_contract),
                    "request_ref": human_request["request_ref"],
                },
            )
            connection.execute(
                text(
                    "UPDATE owner_human_request_waiters SET target_assertion_json = "
                    ":target_json, target_assertion_hash = :target_hash WHERE "
                    "request_ref = :request_ref"
                ),
                {
                    "target_json": acquisition_json(legacy_target),
                    "target_hash": acquisition_hash(legacy_target),
                    "request_ref": human_request["request_ref"],
                },
            )
    finally:
        client.close()
        runtime.close()

    restarted = build_production_runtime(
        data_root,
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
    )
    restarted_client, restarted_headers = _authenticated_client(restarted)
    try:
        response = restarted_client.post(
            f"/api/v1/human-requests/{human_request['request_ref']}/responses",
            headers=_write_headers(
                restarted_headers, "legacy-waiting-item-hash-response"
            ),
            json={
                "decision": "provided",
                "facts": {"route": "institutional_browser_reconnected"},
                "note": "The existing controlled browser login was restored.",
            },
        )
        assert response.status_code == 201

        restored = restarted_client.post(
            f"/api/v1/quest-initializations/{initialization_id}/acquisition-session",
            headers=_write_headers(
                restarted_headers, "legacy-waiting-item-hash-preflight"
            ),
            json={
                "expected_draft_revision": saved["quest_draft"]["revision"],
                "expected_draft_hash": saved["quest_draft"]["hash"],
            },
        )
        restored.raise_for_status()
        resumed = restarted.owners.agent_runtime.acquire_literature(
            session_ref, request, provider
        )
        assert resumed.status == "missing"
        assert len(provider.batches) == 2
        current = restarted.owners.agent_runtime.query_human_request(
            human_request["request_ref"]
        )
        assert current is not None
        assert current["revision"] == human_request["revision"]
        assert current["direct_waiters"][0]["status"] == "consumed"
    finally:
        restarted_client.close()
        restarted.close()


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


def test_restart_after_waiter_consumption_reconciles_without_new_human_request(
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
    initialization_id, saved, session_ref = _open_ready_acquisition(
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
        original = _respond_to_acquisition_human_request(
            runtime,
            session_ref=session_ref,
            request_id=request.request_id,
            key="consumed-crash-login",
        )
        restored = client.post(
            f"/api/v1/quest-initializations/{initialization_id}/acquisition-session",
            headers=_write_headers(write_headers, "consumed-crash-preflight"),
            json={
                "expected_draft_revision": saved["quest_draft"]["revision"],
                "expected_draft_hash": saved["quest_draft"]["hash"],
            },
        )
        restored.raise_for_status()

        with pytest.raises(KeyboardInterrupt):
            runtime.owners.agent_runtime.acquire_literature(
                session_ref, request, provider
            )
        consumed = runtime.owners.agent_runtime.query_human_request(
            original["request_ref"]
        )
        assert consumed is not None
        assert consumed["direct_waiters"][0]["status"] == "consumed"
        assert consumed["direct_waiters"][0]["resume_validation"][
            "started_work"
        ] is True
    finally:
        client.close()
        runtime.close()

    restarted = build_production_runtime(
        data_root,
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
    )
    try:
        before = restarted.owners.agent_runtime.query_human_requests()
        assert [item["request_ref"] for item in before] == [original["request_ref"]]
        recovered = restarted.owners.agent_runtime.acquire_literature(
            session_ref, request, provider
        )
        assert recovered.status == "missing"
        assert provider.acquire_calls == 2
        assert len(provider.reconcile_calls) == 1
        after = restarted.owners.agent_runtime.query_human_requests()
        assert [item["request_ref"] for item in after] == [original["request_ref"]]
        assert after[0]["direct_waiters"][0]["status"] == "consumed"
    finally:
        restarted.close()


def test_oa_only_response_is_an_ar_owned_narrow_route_and_resumes_without_relogin(
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
        human_request = _current_acquisition_human_request(
            runtime, session_ref, request.request_id
        )
        response = runtime.owners.human_collaboration.respond_to_human_request(
            human_request["request_ref"],
            decision="provided",
            facts={"route": "oa_only"},
            note="Continue with openly accessible sources only.",
            idempotency_key="oa-route-response",
        )
        current = runtime.owners.agent_runtime.query_human_request(
            human_request["request_ref"]
        )
        assert current is not None
        assert current["status"] == "satisfied"
        assert current["evaluation"]["response_refs"] == [
            response["response_ref"]
        ]
        assert current["evaluation"]["reason"] == {
            "code": "oa_only_route_selected"
        }
        assert current["disposition"]["decision"] == "satisfied"
        assert current["direct_waiters"][0]["status"] == "released"

        resumed = runtime.owners.agent_runtime.acquire_literature(
            session_ref, request, provider
        )
        assert resumed.status == "missing"
        assert len(provider.batches) == 2
        assert provider.batches[1].session_mode == "oa_only"
        assert provider.batches[1].browser_context_ref is None
        consumed = runtime.owners.agent_runtime.query_human_request(
            human_request["request_ref"]
        )
        assert consumed is not None
        assert consumed["direct_waiters"][0]["status"] == "consumed"
    finally:
        client.close()
        runtime.close()


def test_acquisition_config_drift_requires_a_new_exact_human_response(
    tmp_path: Path,
) -> None:
    provider = RecordingAcquisitionProvider()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "oa-route-config-drift"),
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
    )
    client, write_headers = _authenticated_client(runtime)
    initialization_id, saved, session_ref = _open_ready_acquisition(
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
        original = _current_acquisition_human_request(
            runtime, session_ref, request.request_id
        )
        runtime.owners.human_collaboration.respond_to_human_request(
            original["request_ref"],
            decision="provided",
            facts={"route": "oa_only"},
            note="Use OA only for the current exact configuration.",
            idempotency_key="oa-route-config-drift-first-response",
        )
        released = runtime.owners.agent_runtime.query_human_request(
            original["request_ref"]
        )
        assert released is not None
        assert released["direct_waiters"][0]["status"] == "released"

        current_creation = client.get(
            f"/api/v1/quest-initializations/{initialization_id}"
        ).json()
        changed_draft = dict(current_creation["quest_draft"]["value"])
        changed_draft["literature"] = {
            **changed_draft["literature"],
            "library_entry_url": "https://library.example.edu/changed-entry",
        }
        changed_response = client.put(
            f"/api/v1/quest-initializations/{initialization_id}/draft",
            headers=_write_headers(write_headers, "oa-route-config-drift-resave"),
            json={
                "expected_draft_revision": current_creation["quest_draft"][
                    "revision"
                ],
                "expected_draft_hash": current_creation["quest_draft"]["hash"],
                "draft": changed_draft,
            },
        )
        assert changed_response.status_code == 200, changed_response.json()
        changed = changed_response.json()
        reprobed = client.post(
            f"/api/v1/quest-initializations/{initialization_id}/acquisition-session",
            headers=_write_headers(
                write_headers, "oa-route-config-drift-repreflight"
            ),
            json={
                "expected_draft_revision": changed["quest_draft"]["revision"],
                "expected_draft_hash": changed["quest_draft"]["hash"],
            },
        )
        assert reprobed.status_code == 200, reprobed.json()
        current_session = runtime.owners.agent_runtime.query_acquisition_session(
            session_ref=session_ref
        )
        assert current_session is not None
        new_config_hash = current_session.config_hash

        with pytest.raises(
            OwnerConflict, match="acquisition_human_request_not_released"
        ):
            runtime.owners.agent_runtime.acquire_literature(
                session_ref, request, provider
            )
        requests = [
            item
            for item in runtime.owners.agent_runtime.query_human_requests()
            if item["target_assertion"].get("acquisition_request_id")
            == request.request_id
        ]
        assert len(requests) == 2
        successor = next(item for item in requests if item["status"] == "open")
        assert successor["target_assertion"]["config_hash"] == new_config_hash
        assert original["request_ref"] != successor["request_ref"]

        runtime.owners.human_collaboration.respond_to_human_request(
            successor["request_ref"],
            decision="provided",
            facts={"route": "oa_only"},
            note="Use OA only for this new exact configuration.",
            idempotency_key="oa-route-config-drift-second-response",
        )
        resumed = runtime.owners.agent_runtime.acquire_literature(
            session_ref, request, provider
        )
        assert resumed.status == "missing"
        assert provider.batches[-1].session_mode == "oa_only"
    finally:
        client.close()
        runtime.close()


def test_accepted_material_response_is_verified_by_rm_and_satisfies_exact_waiter(
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
        human_request = _current_acquisition_human_request(
            runtime, session_ref, request.request_id
        )
        intake = runtime.owners.research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="file",
                custody_mode="managed",
                display_name="lawful-copy.pdf",
                media_type="application/pdf",
                content=b"%PDF-1.7\naccepted lawful copy\n",
                provenance={
                    "submitted_via": "human_request_response",
                    "human_request_ref": human_request["request_ref"],
                },
                asynchronous=False,
            ),
            idempotency_key="provided-material-intake",
        )
        assert intake.status == "accepted"
        assert intake.asset is not None
        asset = intake.asset
        response = runtime.owners.human_collaboration.respond_to_human_request(
            human_request["request_ref"],
            decision="provided",
            facts={
                "acquisition_paper_id": "paper:waiting",
                "material_source_ref": asset.memory_ref,
                "material_version_ref": asset.version_ref,
                "material_content_hash": asset.content_hash,
                "material_manifest_hash": asset.manifest_hash,
                "material_acceptance_receipt_ref": asset.receipt.receipt_ref,
            },
            note="Use this accepted copy for the exact blocked item.",
            idempotency_key="provided-material-response",
        )
        current = runtime.owners.agent_runtime.query_human_request(
            human_request["request_ref"]
        )
        assert current is not None
        assert current["status"] == "satisfied"
        assert current["evaluation"]["response_refs"] == [
            response["response_ref"]
        ]
        assert current["evaluation"]["reason"] == {
            "code": "accepted_material_bound"
        }
        assert current["direct_waiters"][0]["status"] == "released"

        resumed = runtime.owners.agent_runtime.acquire_literature(
            session_ref, request, provider
        )
        assert resumed.status == "obtained"
        assert len(provider.batches) == 1
        result = resumed.results[0]
        assert result.paper_id == "paper:waiting"
        assert result.status == "obtained"
        assert result.format == "pdf"
        assert result.path is not None
        assert Path(result.path).read_bytes() == b"%PDF-1.7\naccepted lawful copy\n"
        consumed = runtime.owners.agent_runtime.query_human_request(
            human_request["request_ref"]
        )
        assert consumed is not None
        assert consumed["direct_waiters"][0]["status"] == "consumed"
    finally:
        client.close()
        runtime.close()


def test_two_waiting_items_resume_independently_with_exact_material_and_restart(
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
        assert {
            item["target_assertion"]["acquisition_paper_id"]
            for item in human_requests
        } == {"paper:first", "paper:second"}
        assert all(len(item["direct_waiters"]) == 1 for item in human_requests)
        first_request = next(
            item
            for item in human_requests
            if item["target_assertion"]["acquisition_paper_id"] == "paper:first"
        )
        second_request = next(
            item
            for item in human_requests
            if item["target_assertion"]["acquisition_paper_id"] == "paper:second"
        )
        _respond_with_accepted_material(
            runtime,
            first_request,
            paper_id="paper:first",
            key="provided-material-first",
            content=b"%PDF-1.7\nfirst exact copy\n",
        )
        before_resume = _current_acquisition_human_requests(
            runtime, session_ref, request.request_id
        )
        assert next(
            item
            for item in before_resume
            if item["request_ref"] == first_request["request_ref"]
        )["direct_waiters"][0]["status"] == "released"
        assert next(
            item
            for item in before_resume
            if item["request_ref"] == second_request["request_ref"]
        )["direct_waiters"][0]["status"] == "blocked"

        partial = runtime.owners.agent_runtime.acquire_literature(
            session_ref, request, provider
        )
        assert partial.status == "waiting_user"
        assert [item.status for item in partial.results] == [
            "obtained",
            "waiting_user",
        ]
        assert len(provider.batches) == 1
        after_partial = _current_acquisition_human_requests(
            runtime, session_ref, request.request_id
        )
        assert next(
            item
            for item in after_partial
            if item["request_ref"] == first_request["request_ref"]
        )["direct_waiters"][0]["status"] == "consumed"
        remaining = next(
            item
            for item in after_partial
            if item["target_assertion"]["acquisition_paper_id"] == "paper:second"
            and item["status"] == "open"
        )
        remaining_ref = remaining["request_ref"]
    finally:
        client.close()
        runtime.close()

    restarted = build_production_runtime(
        data_root,
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
    )
    try:
        remaining = restarted.owners.agent_runtime.query_human_request(remaining_ref)
        assert remaining is not None
        assert remaining["direct_waiters"][0]["status"] == "blocked"
        _respond_with_accepted_material(
            restarted,
            remaining,
            paper_id="paper:second",
            key="provided-material-second",
            content=b"%PDF-1.7\nsecond exact copy\n",
        )
        completed = restarted.owners.agent_runtime.acquire_literature(
            session_ref, request, provider
        )
        assert completed.status == "obtained"
        assert len(provider.batches) == 1
        assert [Path(item.path).read_bytes() for item in completed.results] == [
            b"%PDF-1.7\nfirst exact copy\n",
            b"%PDF-1.7\nsecond exact copy\n",
        ]
        consumed = restarted.owners.agent_runtime.query_human_request(remaining_ref)
        assert consumed is not None
        assert consumed["direct_waiters"][0]["status"] == "consumed"
    finally:
        restarted.close()


def test_oa_only_resume_route_survives_unknown_outcome_and_restart(
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
        human_request = _current_acquisition_human_request(
            runtime, session_ref, request.request_id
        )
        runtime.owners.human_collaboration.respond_to_human_request(
            human_request["request_ref"],
            decision="provided",
            facts={"route": "oa_only"},
            note="Continue with OA only.",
            idempotency_key="oa-route-crash-response",
        )
        with pytest.raises(KeyboardInterrupt):
            runtime.owners.agent_runtime.acquire_literature(
                session_ref, request, provider
            )
        with runtime._database.read() as connection:
            route = connection.execute(
                text(
                    "SELECT effective_mode, route_json FROM "
                    "ar_acquisition_resume_routes WHERE request_id = :request_id"
                ),
                {"request_id": request.request_id},
            ).one()
        assert route.effective_mode == "oa_only"
        assert '"route":"oa_only"' in route.route_json
    finally:
        client.close()
        runtime.close()

    restarted = build_production_runtime(
        data_root,
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
    )
    try:
        assert restarted.owners.agent_runtime.query_human_requests()[0][
            "direct_waiters"
        ][0]["status"] == "consumed"
        recovered = restarted.owners.agent_runtime.acquire_literature(
            session_ref, request, provider
        )
        assert recovered.status == "missing"
        assert provider.acquire_calls == 2
        assert len(provider.reconcile_calls) == 1
        assert provider.reconcile_calls[0].session_mode == "oa_only"
        assert provider.reconcile_calls[0].browser_context_ref is None
        assert len(restarted.owners.agent_runtime.query_human_requests()) == 1
    finally:
        restarted.close()


def test_accepted_material_route_recovers_after_consumption_without_provider_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meta_research.owners.agent_runtime as agent_runtime_module

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
    original_validate = agent_runtime_module.validate_item_results
    crash_once = True

    def crash_after_consumption(batch_request, results):
        nonlocal crash_once
        if crash_once and any(item.status == "obtained" for item in results):
            crash_once = False
            raise KeyboardInterrupt("crash before accepted material finalization")
        return original_validate(batch_request, results)

    try:
        assert runtime.owners.agent_runtime.acquire_literature(
            session_ref, request, provider
        ).status == "waiting_user"
        human_request = _current_acquisition_human_request(
            runtime, session_ref, request.request_id
        )
        intake = runtime.owners.research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="file",
                custody_mode="managed",
                display_name="recovery-copy.pdf",
                media_type="application/pdf",
                content=b"%PDF-1.7\nrecovery copy\n",
                provenance={"human_request_ref": human_request["request_ref"]},
                asynchronous=False,
            ),
            idempotency_key="material-route-crash-intake",
        )
        assert intake.asset is not None
        asset = intake.asset
        runtime.owners.human_collaboration.respond_to_human_request(
            human_request["request_ref"],
            decision="provided",
            facts={
                "acquisition_paper_id": "paper:waiting",
                "material_source_ref": asset.memory_ref,
                "material_version_ref": asset.version_ref,
                "material_content_hash": asset.content_hash,
                "material_manifest_hash": asset.manifest_hash,
                "material_acceptance_receipt_ref": asset.receipt.receipt_ref,
            },
            note="Use the accepted exact copy.",
            idempotency_key="material-route-crash-response",
        )
        monkeypatch.setattr(
            agent_runtime_module, "validate_item_results", crash_after_consumption
        )
        with pytest.raises(KeyboardInterrupt):
            runtime.owners.agent_runtime.acquire_literature(
                session_ref, request, provider
            )
    finally:
        client.close()
        runtime.close()

    restarted = build_production_runtime(
        data_root,
        acquisition_provider=provider,
        host_compute_probe=NoCompute(),
    )
    try:
        recovered = restarted.owners.agent_runtime.acquire_literature(
            session_ref, request, provider
        )
        assert recovered.status == "obtained"
        assert len(provider.batches) == 1
        assert recovered.results[0].path is not None
        assert Path(recovered.results[0].path).read_bytes() == (
            b"%PDF-1.7\nrecovery copy\n"
        )
        requests = restarted.owners.agent_runtime.query_human_requests()
        assert len(requests) == 1
        assert requests[0]["direct_waiters"][0]["status"] == "consumed"
    finally:
        restarted.close()


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


def test_deepfetch_waits_for_login_then_replays_the_same_hosted_batch(
    tmp_path: Path,
) -> None:
    acquisition_provider = RecordingAcquisitionProvider()
    deepfetch_provider = AcquisitionWaitingDeepFetchProvider(acquisition_provider)
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "data"),
        deepfetch_provider=deepfetch_provider,
        acquisition_provider=acquisition_provider,
        host_compute_probe=NoCompute(),
    )
    deepfetch_provider.agent_runtime = runtime.owners.agent_runtime
    client, write_headers = _authenticated_client(runtime)
    try:
        opened = client.post(
            "/api/v1/quest-initializations",
            headers=_write_headers(write_headers, "waiting-open"),
            json={},
        ).json()
        initialization_id = opened["initialization_id"]
        probed = client.post(
            f"/api/v1/quest-initializations/{initialization_id}/compute-probe",
            headers=_write_headers(write_headers, "waiting-compute"),
            json={"selected_device_uuids": ["GPU-acquisition-1"]},
        ).json()
        draft = dict(probed["quest_draft"]["value"])
        draft.update(
            {
                "goal": "核查一个需要机构全文的首问题。",
                "completion_criteria": "返回可审计的论文账本和全文状态。",
                "route": "deepfetch",
                "literature": {
                    **draft["literature"],
                    "mode": "oa_then_institution",
                    "library_entry_url": "https://library.example.edu/resources",
                },
            }
        )
        saved_response = client.put(
            f"/api/v1/quest-initializations/{initialization_id}/draft",
            headers=_write_headers(write_headers, "waiting-draft"),
            json={
                "expected_draft_revision": probed["quest_draft"]["revision"],
                "expected_draft_hash": probed["quest_draft"]["hash"],
                "draft": draft,
            },
        )
        saved_response.raise_for_status()
        saved = saved_response.json()
        prepared_response = client.post(
            f"/api/v1/quest-initializations/{initialization_id}/acquisition-session",
            headers=_write_headers(write_headers, "waiting-preflight"),
            json={
                "expected_draft_revision": saved["quest_draft"]["revision"],
                "expected_draft_hash": saved["quest_draft"]["hash"],
            },
        )
        prepared_response.raise_for_status()
        session_ref = prepared_response.json()["acquisition_session"]["session_ref"]
        queued_response = client.post(
            f"/api/v1/quest-initializations/{initialization_id}/proposal-generations",
            headers=_write_headers(write_headers, "waiting-start"),
            json={
                "expected_draft_revision": saved["quest_draft"]["revision"],
                "expected_draft_hash": saved["quest_draft"]["hash"],
            },
        )
        assert queued_response.status_code == 202
        request_ref = queued_response.json()["deepfetch"]["request_ref"]

        assert runtime.deepfetch.process_once() is False
        waiting = client.get(
            f"/api/v1/quest-initializations/{initialization_id}"
        ).json()
        assert waiting["deepfetch"]["status"] == "queued"
        assert waiting["deepfetch"]["failure"] is None
        assert waiting["acquisition_session"]["status"] == "waiting_user"
        assert waiting["acquisition_session"]["current_request_id"] == (
            "deepfetch-exact-batch-1"
        )
        run = runtime.owners.agent_runtime.query_deepfetch_run(request_ref)
        assert run is not None
        assert run.status == "admitted"
        assert run.native_session_ref == "native-acquisition-recovery-1"
        assert len(deepfetch_provider.requests) == 1

        # Polling the queued worker while login is pending must not consume the
        # request, fail Human Collaboration, or repeat the provider side effect.
        assert runtime.deepfetch.process_once() is False
        assert len(deepfetch_provider.requests) == 1
        still_waiting = client.get(
            f"/api/v1/quest-initializations/{initialization_id}"
        ).json()
        assert still_waiting["deepfetch"]["status"] == "queued"
        assert still_waiting["deepfetch"]["failure"] is None

        collaboration = client.get("/api/v1/snapshot").json()[
            "human_collaboration"
        ]
        library_requests = [
            item
            for item in collaboration["human_requests"]["items"]
            if item["issuer"] == "agent_runtime"
            and item["kind"] == "library_reconnect"
            and item["target_assertion"].get("session_ref") == session_ref
            and item["target_assertion"].get("acquisition_request_id")
            == "deepfetch-exact-batch-1"
        ]
        assert len(library_requests) == 1
        library_request = library_requests[0]
        assert library_request["status"] == "open"
        assert library_request["evaluation"] is None
        assert library_request["disposition"] is None
        assert library_request["direct_waiters"][0]["status"] == "blocked"

        responded = client.post(
            "/api/v1/human-requests/"
            f"{library_request['request_ref']}/responses",
            headers=_write_headers(write_headers, "waiting-login-response"),
            json={
                "decision": "provided",
                "facts": {"route": "institutional_browser_reconnected"},
                "note": "The existing controlled browser login was restored.",
            },
        )
        responded.raise_for_status()
        responded_only = runtime.owners.agent_runtime.query_human_request(
            library_request["request_ref"]
        )
        assert responded_only is not None
        assert len(responded_only["responses"]) == 1
        assert responded_only["evaluation"]["decision"] == "satisfied"
        assert responded_only["disposition"]["decision"] == "satisfied"
        assert responded_only["direct_waiters"][0]["status"] == "released"

        resumed_session = runtime.owners.agent_runtime.query_acquisition_session(
            session_ref=session_ref
        )
        assert resumed_session is not None
        assert resumed_session.session_ref == session_ref
        assert resumed_session.status == "ready"

        released = runtime.owners.agent_runtime.query_human_request(
            library_request["request_ref"]
        )
        assert released is not None
        assert released["evaluation"]["decision"] == "satisfied"
        assert released["disposition"]["decision"] == "satisfied"
        assert released["direct_waiters"][0]["status"] == "released"

        assert runtime.deepfetch.process_once() is True
        consumed = runtime.owners.agent_runtime.query_human_request(
            library_request["request_ref"]
        )
        assert consumed is not None
        consumed_waiter = consumed["direct_waiters"][0]
        assert consumed_waiter["status"] == "consumed"
        assert consumed_waiter["resume_validation"]["started_work"] is True
        assert consumed_waiter["resume_validation"]["consumption"][
            "work_ref"
        ].startswith("acquisition_item:")
        assert consumed_waiter["resume_validation"]["consumption"]["receipt"][
            "kind"
        ] == "human_request_resume_consumption"
        completed = client.get(
            f"/api/v1/quest-initializations/{initialization_id}"
        ).json()
        assert completed["deepfetch"]["status"] == "succeeded"
        assert completed["deepfetch"]["failure"] is None
        assert [batch.request_id for batch in acquisition_provider.batches] == [
            "deepfetch-exact-batch-1",
            "deepfetch-exact-batch-1",
        ]
        assert acquisition_provider.batches[0].identity_payload() == (
            acquisition_provider.batches[1].identity_payload()
        )
        assert len(deepfetch_provider.requests) == 2
        assert deepfetch_provider.requests[1].native_session_ref == (
            "native-acquisition-recovery-1"
        )
        assert deepfetch_provider.requests[0].job_ref == (
            deepfetch_provider.requests[1].job_ref
        )
    finally:
        client.close()
        runtime.close()
