from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from meta_research.acquisition import (
    AcquisitionBatchRequest,
    AcquisitionItemResult,
    AcquisitionPaper,
    AcquisitionPreflightResult,
    AcquisitionRuntimeBinding,
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
from meta_research.quest_drafting import HostComputeDevice, HostComputeSnapshot
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


class PartialAcquisitionProvider(RecordingAcquisitionProvider):
    def acquire(self, request: AcquisitionBatchRequest):
        self.batches.append(request)
        if len(self.batches) == 1:
            return tuple(
                AcquisitionItemResult(
                    paper_id=item.paper_id,
                    status="obtained" if item.paper_id == "paper:obtained" else "waiting_user",
                    path=("/private/already-obtained.pdf" if item.paper_id == "paper:obtained" else None),
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
                path="/private/resumed.pdf",
                format="pdf",
                failure=None,
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
        restored = restarted_client.post(
            f"/api/v1/quest-initializations/{initialization_id}/acquisition-session",
            headers=_write_headers(restarted_headers, "reconcile-restore"),
            json={
                "expected_draft_revision": saved["quest_draft"]["revision"],
                "expected_draft_hash": saved["quest_draft"]["hash"],
            },
        )
        restored.raise_for_status()
        execution = restarted.owners.agent_runtime.acquire_literature(
            session_ref, request, provider
        )
        assert execution.status == "missing"
        assert provider.acquire_calls == 0
        assert len(provider.reconcile_calls) == 1
        assert [paper.paper_id for paper in provider.reconcile_calls[0].papers] == [
            "paper:unknown"
        ]
    finally:
        restarted_client.close()
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

        resumed_response = client.post(
            f"/api/v1/quest-initializations/{initialization_id}/acquisition-session",
            headers=_write_headers(write_headers, "waiting-login-restored"),
            json={
                "expected_draft_revision": saved["quest_draft"]["revision"],
                "expected_draft_hash": saved["quest_draft"]["hash"],
            },
        )
        resumed_response.raise_for_status()
        resumed_session = resumed_response.json()["acquisition_session"]
        assert resumed_session["session_ref"] == session_ref
        assert resumed_session["status"] == "ready"

        assert runtime.deepfetch.process_once() is True
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
