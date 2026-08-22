from __future__ import annotations

import threading
from pathlib import Path

from fastapi.testclient import TestClient

from meta_research.composition import build_production_runtime
from meta_research.deepfetch import (
    DeepFetchProviderRequest,
    DeepFetchResult,
    DeepFetchRuntimeBinding,
    DeepFetchUnavailable,
)
from meta_research.paths import prepare_data_root
from meta_research.quest_drafting import (
    HostComputeDevice,
    HostComputeSnapshot,
    ProposalDraftRequest,
    ProposalDraftResult,
)
from meta_research.web import create_app


QUESTION = {
    "title": "低照度显微图像中的稀有形态保真",
    "unknown_statement": "尚不明确哪种自监督去噪条件能保留稀有形态。",
    "answer_shape": "形成带反例和证据边界的比较结论。",
    "applicability_scope": "低照度荧光显微公开数据。",
    "background_context": "DeepFetch 找到两篇可核查论文，其中一篇没有开放全文。",
    "requirements_constraints": "两周内，使用获准 GPU；保留缺全文限制。",
}


class DeterministicProbe:
    def observe(self) -> HostComputeSnapshot:
        return HostComputeSnapshot(
            status="ready",
            observed_at=1_720_000_000.0,
            devices=(
                HostComputeDevice(
                    uuid="GPU-deepfetch-1",
                    name="DeepFetch Test GPU",
                    memory_total_mib=81_920,
                ),
            ),
            adapter_kind="test_probe",
        )


class DeterministicDeepFetchProvider:
    def __init__(self) -> None:
        self.requests: list[DeepFetchProviderRequest] = []

    def runtime_binding(self) -> DeepFetchRuntimeBinding:
        return DeepFetchRuntimeBinding(
            provider_ref="test/deepfetch-provider",
            provider_version="1",
            model_ref="test-model",
            harness_ref="test-harness",
            capability_bindings=("web-search-live", "web-fetch-live"),
        )

    def execute(self, request: DeepFetchProviderRequest) -> DeepFetchResult:
        self.requests.append(request)
        return self.result()

    @staticmethod
    def result(
        *, native_session_ref: str = "native-deepfetch-session-1"
    ) -> DeepFetchResult:
        return DeepFetchResult(
            completion="limited",
            summary=(
                "两篇可核查论文比较了低照度显微去噪；公开全文只覆盖其中一篇。"
            ),
            papers=(
                {
                    "title": "Self-supervised denoising for fluorescence microscopy",
                    "url": "https://example.org/papers/one",
                    "doi": "10.1000/example.one",
                    "source_kind": "publisher",
                    "fulltext_status": "accepted",
                    "retrieved_at": "2026-08-22T00:00:00Z",
                },
                {
                    "title": "Rare morphology preservation under low light",
                    "url": "https://example.org/papers/two",
                    "doi": None,
                    "source_kind": "publisher",
                    "fulltext_status": "unavailable",
                    "retrieved_at": "2026-08-22T00:00:01Z",
                },
            ),
            fulltexts=(
                {
                    "paper_url": "https://example.org/papers/one",
                    "media_type": "text/plain",
                    "content": "Verified open full text for paper one.",
                },
            ),
            limitations=("第二篇论文没有可合法获取的开放全文。",),
            native_session_ref=native_session_ref,
            adapter_kind="test_deepfetch",
        )


class FailOnceDeepFetchProvider(DeterministicDeepFetchProvider):
    def execute(self, request: DeepFetchProviderRequest) -> DeepFetchResult:
        self.requests.append(request)
        if len(self.requests) == 1:
            raise DeepFetchUnavailable("web_search_temporarily_unavailable")
        return self.result()


class SensitiveResultProvider(DeterministicDeepFetchProvider):
    def execute(self, request: DeepFetchProviderRequest) -> DeepFetchResult:
        self.requests.append(request)
        result = self.result()
        paper = dict(result.papers[0])
        paper["url"] = "https://example.org/paper?access_token=must-not-persist"
        return DeepFetchResult(
            completion=result.completion,
            summary=result.summary,
            papers=(paper, *result.papers[1:]),
            fulltexts=result.fulltexts,
            limitations=result.limitations,
            native_session_ref=result.native_session_ref,
            adapter_kind=result.adapter_kind,
        )


class BlockingDeepFetchProvider(DeterministicDeepFetchProvider):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def execute(self, request: DeepFetchProviderRequest) -> DeepFetchResult:
        self.requests.append(request)
        self.started.set()
        assert self.release.wait(timeout=10)
        return self.result(native_session_ref="native-late-result")


class SnapshotAwareProposalDrafter:
    def __init__(self) -> None:
        self.requests: list[ProposalDraftRequest] = []

    def draft(self, request: ProposalDraftRequest) -> ProposalDraftResult:
        self.requests.append(request)
        assert request.literature_snapshot is not None
        assert request.literature_snapshot["completion"] == "limited"
        assert request.literature_snapshot["summary"].startswith("两篇可核查论文")
        return ProposalDraftResult(content=QUESTION, adapter_kind="test_drafter")


def _authenticate(runtime) -> tuple[TestClient, dict[str, str]]:
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


def _deepfetch_draft(view: dict[str, object]) -> dict[str, object]:
    draft_view = view["quest_draft"]
    assert isinstance(draft_view, dict)
    draft = dict(draft_view["value"])
    draft.update(
        {
            "goal": "判断低照度显微图像去噪能否保留稀有形态。",
            "completion_criteria": "形成带反例和证据边界的比较结论。",
            "time_budget": "30d",
            "route": "deepfetch",
            "background_and_initial_direction": "比较自监督和监督基线。",
        }
    )
    return draft


def _open_and_queue_deepfetch(
    client: TestClient,
    write_headers: dict[str, str],
    *,
    key_prefix: str,
) -> tuple[str, dict[str, object]]:
    opened = client.post(
        "/api/v1/quest-initializations",
        headers=_write_headers(write_headers, f"{key_prefix}-open"),
        json={},
    ).json()
    initialization_id = str(opened["initialization_id"])
    probed = client.post(
        f"/api/v1/quest-initializations/{initialization_id}/compute-probe",
        headers=_write_headers(write_headers, f"{key_prefix}-compute"),
        json={"selected_device_uuids": ["GPU-deepfetch-1"]},
    ).json()
    saved_response = client.put(
        f"/api/v1/quest-initializations/{initialization_id}/draft",
        headers=_write_headers(write_headers, f"{key_prefix}-draft"),
        json={
            "expected_draft_revision": probed["quest_draft"]["revision"],
            "expected_draft_hash": probed["quest_draft"]["hash"],
            "draft": _deepfetch_draft(probed),
        },
    )
    saved_response.raise_for_status()
    saved = saved_response.json()
    queued_response = client.post(
        f"/api/v1/quest-initializations/{initialization_id}/proposal-generations",
        headers=_write_headers(write_headers, f"{key_prefix}-start"),
        json={
            "expected_draft_revision": saved["quest_draft"]["revision"],
            "expected_draft_hash": saved["quest_draft"]["hash"],
        },
    )
    assert queued_response.status_code == 202
    return initialization_id, queued_response.json()


def test_deepfetch_prepares_one_exact_snapshot_then_returns_to_the_same_proposal(
    tmp_path: Path,
) -> None:
    deepfetch_provider = DeterministicDeepFetchProvider()
    proposal_drafter = SnapshotAwareProposalDrafter()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "data"),
        proposal_drafter=proposal_drafter,
        deepfetch_provider=deepfetch_provider,
        host_compute_probe=DeterministicProbe(),
    )
    client, write_headers = _authenticate(runtime)
    try:
        opened = client.post(
            "/api/v1/quest-initializations",
            headers=_write_headers(write_headers, "deepfetch-open"),
            json={},
        ).json()
        probed = client.post(
            f"/api/v1/quest-initializations/{opened['initialization_id']}"
            "/compute-probe",
            headers=_write_headers(write_headers, "deepfetch-compute"),
            json={"selected_device_uuids": ["GPU-deepfetch-1"]},
        ).json()
        saved_response = client.put(
            f"/api/v1/quest-initializations/{opened['initialization_id']}/draft",
            headers=_write_headers(write_headers, "deepfetch-draft"),
            json={
                "expected_draft_revision": probed["quest_draft"]["revision"],
                "expected_draft_hash": probed["quest_draft"]["hash"],
                "draft": _deepfetch_draft(probed),
            },
        )
        saved_response.raise_for_status()
        saved = saved_response.json()

        queued_response = client.post(
            f"/api/v1/quest-initializations/{opened['initialization_id']}"
            "/proposal-generations",
            headers=_write_headers(write_headers, "deepfetch-start"),
            json={
                "expected_draft_revision": saved["quest_draft"]["revision"],
                "expected_draft_hash": saved["quest_draft"]["hash"],
            },
        )
        assert queued_response.status_code == 202
        queued = queued_response.json()
        assert queued["status"] == "proposal_generating"
        assert queued["deepfetch"]["status"] == "queued"
        assert queued["deepfetch"]["freshness"] == "current"
        assert queued["deepfetch"]["activity"] == "waiting_for_runtime"
        assert queued["proposal"] is None
        assert queued.get("quest_ref") is None
        assert all(
            receipt["status"] == "not_attempted"
            for receipt in queued["receipts"].values()
        )

        assert runtime.deepfetch.process_once()
        after_research = client.get(
            f"/api/v1/quest-initializations/{opened['initialization_id']}"
        ).json()
        assert after_research["deepfetch"]["status"] == "succeeded"
        assert after_research["deepfetch"]["activity"] == "proposal_drafting"
        assert after_research["deepfetch"]["progress"] == {
            "completed": 4,
            "total": 5,
        }
        assert after_research["deepfetch"]["run"]["attempt_generation"] == 1
        assert after_research["deepfetch"]["run"]["root_session_ref"]
        assert after_research["deepfetch"]["run"]["native_session_ref"] == (
            "native-deepfetch-session-1"
        )
        assert after_research["deepfetch"]["run"]["execution_receipt"][
            "status"
        ] == "accepted"
        snapshot = after_research["deepfetch"]["literature_snapshot"]
        assert snapshot["completion"] == "limited"
        assert snapshot["paper_count"] == 2
        assert snapshot["fulltext_count"] == 1
        assert snapshot["receipt"]["status"] == "accepted"

        snapshot_response = client.get(
            f"/api/v1/literature-snapshots/{snapshot['snapshot_ref']}"
        )
        snapshot_response.raise_for_status()
        assert snapshot_response.json()["summary"].startswith("两篇可核查论文")
        assert snapshot_response.json()["limitations"] == [
            "第二篇论文没有可合法获取的开放全文。"
        ]

        assert runtime.owners.human_collaboration.process_drafting_once()
        ready = client.get(
            f"/api/v1/quest-initializations/{opened['initialization_id']}"
        ).json()
        assert ready["status"] == "proposal_ready"
        assert ready["proposal"]["content"] == QUESTION
        assert ready["proposal"]["literature_snapshot_ref"] == snapshot[
            "snapshot_ref"
        ]
        assert ready["deepfetch"]["activity"] == "complete"
        assert ready["deepfetch"]["progress"] == {"completed": 5, "total": 5}
        assert ready.get("quest_ref") is None
        assert all(
            receipt["status"] == "not_attempted"
            for receipt in ready["receipts"].values()
        )

        replay = client.post(
            f"/api/v1/quest-initializations/{opened['initialization_id']}"
            "/proposal-generations",
            headers=_write_headers(write_headers, "deepfetch-replay-new-key"),
            json={
                "expected_draft_revision": ready["quest_draft"]["revision"],
                "expected_draft_hash": ready["quest_draft"]["hash"],
            },
        )
        replay.raise_for_status()
        assert replay.json()["deepfetch"]["run"]["run_ref"] == ready[
            "deepfetch"
        ]["run"]["run_ref"]
        assert len(deepfetch_provider.requests) == 1
        assert len(proposal_drafter.requests) == 1
        provider_request = deepfetch_provider.requests[0]
        assert provider_request.draft_revision == saved["quest_draft"]["revision"]
        assert provider_request.draft_hash == saved["quest_draft"]["hash"]
        assert provider_request.authorization_receipt.issuer == (
            "human_collaboration"
        )

        preview = ready["confirmation_preview"]
        confirmed_response = client.post(
            f"/api/v1/quest-initializations/{opened['initialization_id']}"
            "/confirmation",
            headers=_write_headers(write_headers, "deepfetch-confirm"),
            json={
                "quest_draft_revision": ready["quest_draft"]["revision"],
                "quest_draft_hash": ready["quest_draft"]["hash"],
                "proposal_ref": ready["proposal"]["ref"],
                "proposal_hash": ready["proposal"]["hash"],
                "preview_ref": preview["ref"],
                "preview_hash": preview["hash"],
            },
        )
        assert confirmed_response.status_code == 202
        completed = confirmed_response.json()
        while completed["status"] != "completed":
            assert runtime.owners.human_collaboration.reconcile_once()
            completed = client.get(
                f"/api/v1/quest-initializations/{opened['initialization_id']}"
            ).json()
        assert all(
            receipt["status"] == "accepted"
            for receipt in completed["receipts"].values()
        )
        assert completed["quest_ref"].startswith("quest_")
        assert completed["question_ref"].startswith("question_")
        assert completed["proposal"]["literature_snapshot_ref"] == snapshot[
            "snapshot_ref"
        ]
    finally:
        client.close()
        runtime.close()


def test_failed_deepfetch_retries_the_same_run_with_a_new_fenced_attempt(
    tmp_path: Path,
) -> None:
    provider = FailOnceDeepFetchProvider()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "data"),
        proposal_drafter=SnapshotAwareProposalDrafter(),
        deepfetch_provider=provider,
        host_compute_probe=DeterministicProbe(),
    )
    client, write_headers = _authenticate(runtime)
    try:
        initialization_id, queued = _open_and_queue_deepfetch(
            client, write_headers, key_prefix="retry"
        )
        request_ref = queued["deepfetch"]["request_ref"]
        assert runtime.deepfetch.process_once()
        failed = client.get(
            f"/api/v1/quest-initializations/{initialization_id}"
        ).json()
        first_run = failed["deepfetch"]["run"]
        assert failed["deepfetch"]["status"] == "failed"
        assert failed["deepfetch"]["failure"] == {
            "code": "web_search_temporarily_unavailable"
        }
        assert failed["deepfetch"]["literature_snapshot"] is None
        assert failed["proposal"] is None

        retried_response = client.post(
            f"/api/v1/quest-initializations/{initialization_id}"
            "/proposal-generations",
            headers=_write_headers(write_headers, "retry-again"),
            json={
                "expected_draft_revision": failed["quest_draft"]["revision"],
                "expected_draft_hash": failed["quest_draft"]["hash"],
            },
        )
        retried_response.raise_for_status()
        assert retried_response.json()["deepfetch"]["request_ref"] == request_ref
        assert runtime.deepfetch.process_once()
        succeeded = client.get(
            f"/api/v1/quest-initializations/{initialization_id}"
        ).json()
        second_run = succeeded["deepfetch"]["run"]
        assert second_run["run_ref"] == first_run["run_ref"]
        assert second_run["root_session_ref"] == first_run["root_session_ref"]
        assert second_run["attempt_generation"] == 2
        assert succeeded["deepfetch"]["status"] == "succeeded"
        assert len(provider.requests) == 2
    finally:
        client.close()
        runtime.close()


def test_basis_change_keeps_old_snapshot_exact_but_requires_a_successor_run(
    tmp_path: Path,
) -> None:
    provider = DeterministicDeepFetchProvider()
    drafter = SnapshotAwareProposalDrafter()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "data"),
        proposal_drafter=drafter,
        deepfetch_provider=provider,
        host_compute_probe=DeterministicProbe(),
    )
    client, write_headers = _authenticate(runtime)
    try:
        initialization_id, _queued = _open_and_queue_deepfetch(
            client, write_headers, key_prefix="successor"
        )
        assert runtime.deepfetch.process_once()
        assert runtime.owners.human_collaboration.process_drafting_once()
        original = client.get(
            f"/api/v1/quest-initializations/{initialization_id}"
        ).json()
        old_snapshot_ref = original["deepfetch"]["literature_snapshot"][
            "snapshot_ref"
        ]
        old_request_ref = original["deepfetch"]["request_ref"]

        changed_draft = dict(original["quest_draft"]["value"])
        changed_draft["goal"] = "比较另一种低照度成像条件下的稀有形态保真。"
        changed_response = client.put(
            f"/api/v1/quest-initializations/{initialization_id}/draft",
            headers=_write_headers(write_headers, "successor-change-basis"),
            json={
                "expected_draft_revision": original["quest_draft"]["revision"],
                "expected_draft_hash": original["quest_draft"]["hash"],
                "draft": changed_draft,
            },
        )
        changed_response.raise_for_status()
        changed = changed_response.json()
        assert changed["status"] == "proposal_stale"
        assert changed["deepfetch"]["freshness"] == "stale"

        adopt_old_response = client.put(
            f"/api/v1/quest-initializations/{initialization_id}/proposal",
            headers=_write_headers(write_headers, "successor-adopt-old"),
            json={
                "expected_draft_revision": changed["quest_draft"]["revision"],
                "expected_draft_hash": changed["quest_draft"]["hash"],
                "expected_proposal_ref": changed["proposal"]["ref"],
                "expected_proposal_hash": changed["proposal"]["hash"],
                "explicit_review": True,
                "content": changed["proposal"]["content"],
            },
        )
        assert adopt_old_response.status_code == 409
        assert adopt_old_response.json()["detail"]["code"] == (
            "literature_snapshot_required"
        )
        old_snapshot_response = client.get(
            f"/api/v1/literature-snapshots/{old_snapshot_ref}"
        )
        old_snapshot_response.raise_for_status()
        assert old_snapshot_response.json()["draft_hash"] == original[
            "quest_draft"
        ]["hash"]

        successor_response = client.post(
            f"/api/v1/quest-initializations/{initialization_id}"
            "/proposal-generations",
            headers=_write_headers(write_headers, "successor-start-new"),
            json={
                "expected_draft_revision": changed["quest_draft"]["revision"],
                "expected_draft_hash": changed["quest_draft"]["hash"],
            },
        )
        successor_response.raise_for_status()
        successor = successor_response.json()
        assert successor["deepfetch"]["request_ref"] != old_request_ref
        assert successor["deepfetch"]["freshness"] == "current"
        assert runtime.deepfetch.process_once()
        assert runtime.owners.human_collaboration.process_drafting_once()
        current = client.get(
            f"/api/v1/quest-initializations/{initialization_id}"
        ).json()
        assert current["proposal"]["literature_snapshot_ref"] != old_snapshot_ref
        assert current["deepfetch"]["freshness"] == "current"
        assert len(provider.requests) == 2
        assert len(drafter.requests) == 2
    finally:
        client.close()
        runtime.close()


def test_cancelled_deepfetch_is_not_a_direct_route_waiver(tmp_path: Path) -> None:
    provider = DeterministicDeepFetchProvider()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "data"),
        proposal_drafter=SnapshotAwareProposalDrafter(),
        deepfetch_provider=provider,
        host_compute_probe=DeterministicProbe(),
    )
    client, write_headers = _authenticate(runtime)
    try:
        initialization_id, _queued = _open_and_queue_deepfetch(
            client, write_headers, key_prefix="cancel"
        )
        cancelled_response = client.post(
            f"/api/v1/quest-initializations/{initialization_id}/cancel",
            headers=_write_headers(write_headers, "cancel-command"),
            json={},
        )
        cancelled_response.raise_for_status()
        cancelled = cancelled_response.json()
        assert cancelled["status"] == "cancelled"
        assert cancelled["deepfetch"]["status"] == "cancelled"
        assert cancelled["deepfetch"]["failure"] == {
            "code": "initialization_cancelled"
        }
        assert not runtime.deepfetch.process_once()
        assert cancelled["proposal"] is None
        assert cancelled.get("quest_ref") is None
        assert provider.requests == []
    finally:
        client.close()
        runtime.close()


def test_sensitive_provider_urls_are_rejected_before_research_memory_acceptance(
    tmp_path: Path,
) -> None:
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "data"),
        proposal_drafter=SnapshotAwareProposalDrafter(),
        deepfetch_provider=SensitiveResultProvider(),
        host_compute_probe=DeterministicProbe(),
    )
    client, write_headers = _authenticate(runtime)
    try:
        initialization_id, _queued = _open_and_queue_deepfetch(
            client, write_headers, key_prefix="sensitive"
        )
        assert runtime.deepfetch.process_once()
        failed = client.get(
            f"/api/v1/quest-initializations/{initialization_id}"
        ).json()
        assert failed["deepfetch"]["status"] == "failed"
        assert failed["deepfetch"]["failure"] == {
            "code": "deepfetch_paper_url_invalid"
        }
        assert failed["deepfetch"]["literature_snapshot"] is None
        assert failed["proposal"] is None
        assert runtime.owners.research_memory.query_snapshot().facts[
            "literature_snapshot_count"
        ] == 0
    finally:
        client.close()
        runtime.close()


def test_restart_reuses_one_root_session_and_rejects_the_old_fence_result(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "data")
    blocking_provider = BlockingDeepFetchProvider()
    first_runtime = build_production_runtime(
        data_root,
        proposal_drafter=SnapshotAwareProposalDrafter(),
        deepfetch_provider=blocking_provider,
        host_compute_probe=DeterministicProbe(),
    )
    client, write_headers = _authenticate(first_runtime)
    restarted_runtime = None
    worker_errors: list[BaseException] = []
    worker = None
    try:
        initialization_id, _queued = _open_and_queue_deepfetch(
            client, write_headers, key_prefix="restart"
        )

        def run_interrupted_worker() -> None:
            try:
                first_runtime.deepfetch.process_once()
            except BaseException as error:  # pragma: no cover - assertion reports it
                worker_errors.append(error)

        worker = threading.Thread(target=run_interrupted_worker, daemon=True)
        worker.start()
        assert blocking_provider.started.wait(timeout=5)
        running = client.get(
            f"/api/v1/quest-initializations/{initialization_id}"
        ).json()
        first_run = running["deepfetch"]["run"]
        assert first_run["status"] == "running"
        assert first_run["attempt_generation"] == 1

        restarted_provider = DeterministicDeepFetchProvider()
        restarted_runtime = build_production_runtime(
            prepare_data_root(tmp_path / "data"),
            proposal_drafter=SnapshotAwareProposalDrafter(),
            deepfetch_provider=restarted_provider,
            host_compute_probe=DeterministicProbe(),
        )
        assert restarted_runtime.deepfetch.process_once()
        recovered = restarted_runtime.owners.human_collaboration.query_quest_creation(
            initialization_id
        )
        recovered_run = recovered["deepfetch"]["run"]
        assert recovered["deepfetch"]["status"] == "succeeded"
        assert recovered_run["run_ref"] == first_run["run_ref"]
        assert recovered_run["root_session_ref"] == first_run["root_session_ref"]
        assert recovered_run["attempt_generation"] == 2
        assert recovered_run["native_session_ref"] == "native-deepfetch-session-1"

        blocking_provider.release.set()
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert worker_errors == []
        after_late_result = (
            restarted_runtime.owners.human_collaboration.query_quest_creation(
                initialization_id
            )
        )
        assert after_late_result["deepfetch"]["run"]["attempt_generation"] == 2
        assert after_late_result["deepfetch"]["run"]["native_session_ref"] == (
            "native-deepfetch-session-1"
        )
        assert restarted_runtime.owners.research_memory.query_snapshot().facts[
            "literature_snapshot_count"
        ] == 1
    finally:
        blocking_provider.release.set()
        if worker is not None:
            worker.join(timeout=5)
        client.close()
        first_runtime.close()
        if restarted_runtime is not None:
            restarted_runtime.close()
