from __future__ import annotations

import copy
import json
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from meta_research.composition import build_production_runtime
from meta_research.deepfetch import (
    CodexDeepFetchAdapter,
    DeepFetchProviderRequest,
    DeepFetchResult,
    DeepFetchRuntimeBinding,
    DeepFetchUnavailable,
)
from meta_research.owners.common import OwnerConflict
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

PROTOTYPE_COMMIT = "cb369c938da835bcd07202e03ccc770551984070"
PROTOTYPE_EMPTY_LEDGER = {
    "schema_version": "deepfetch.papers.v4",
    "topic": {
        "input": "低照度显微图像中的稀有形态保真",
        "interpretation": "寻找可核查的代表性研究。",
        "search_concepts": ["low-light microscopy denoising"],
        "scope_notes": [],
    },
    "run": {
        "intensity": "medium",
        "active_search_budget_minutes": 13,
        "active_search_elapsed_seconds": 42,
        "dimensions_used": [
            "text_queries",
            "literature_roles",
            "citation_graph",
        ],
        "stopping_reason": "coverage_saturated",
    },
    "paper_order": [],
    "papers": {},
    "missing_fulltexts": [],
    "limitations": ["检索未形成可纳入的精确论文。"],
}
PROTOTYPE_EMPTY_FINAL = {
    "action": "finalize",
    "acquisition_request": None,
    "completion": "honest_empty",
    "limitations": ["检索未形成可纳入的精确论文。"],
    "workflow": {
        "prototype_commit": PROTOTYPE_COMMIT,
        "main_agent_status": "complete",
        "reader_assignments": [],
        "finalize_status": "passed",
        "finalized_at": "2026-08-22T00:00:00Z",
    },
}
DETERMINISTIC_LEDGER = {
    "schema_version": "deepfetch.papers.v4",
    "topic": {
        "input": "低照度显微图像中的稀有形态保真",
        "interpretation": "比较两篇代表性研究。",
        "search_concepts": ["low-light microscopy"],
        "scope_notes": [],
    },
    "run": {
        "intensity": "medium",
        "active_search_budget_minutes": 13,
        "active_search_elapsed_seconds": 42,
        "dimensions_used": ["text_queries", "literature_roles", "citation_graph"],
        "stopping_reason": "coverage_saturated",
    },
    "paper_order": ["doi:10.1000/example.one", "title:rare-morphology"],
    "papers": {
        "doi:10.1000/example.one": {
            "identity": {
                "paper_id": "doi:10.1000/example.one",
                "title": "Self-supervised denoising for fluorescence microscopy",
                "doi": "10.1000/example.one",
                "arxiv_id": None,
                "openalex_id": "W1",
            },
            "metadata": {"authors": ["A. Researcher"], "source_urls": ["https://example.org/papers/one"]},
            "pre_understanding": {"summary": "摘要支持纳入。", "evidence_level": "abstract_supported"},
            "fulltext_path": "fulltext/one.html",
            "reading": {
                "status": "complete",
                "understanding_summary": "Reader 保留了完整理解。",
                "methods": ["self-supervision"],
                "experimental_setup": {"datasets_samples": ["microscopy"]},
                "key_claims": [{"claim": "保留稀有形态", "evidence_locators": ["loc-1"]}],
                "limitations": [{"description": "样本有限", "source": "reader"}],
                "artifacts": {"code": {"reported": True, "items": []}},
                "credibility": {"score": 4, "assessment_confidence": "medium"},
                "evidence_locators": [{"id": "loc-1", "section": "Results"}],
                "notes": [],
            },
        },
        "title:rare-morphology": {
            "identity": {
                "paper_id": "title:rare-morphology",
                "title": "Rare morphology preservation under low light",
                "doi": None,
                "arxiv_id": None,
                "openalex_id": None,
            },
            "metadata": {"authors": [], "source_urls": ["https://example.org/papers/two"]},
            "pre_understanding": {"summary": "题名支持候选。", "evidence_level": "title_only"},
            "fulltext_path": None,
            "reading": {"status": "not_read"},
        },
    },
    "missing_fulltexts": ["title:rare-morphology"],
    "limitations": ["第二篇论文没有可合法获取的开放全文。"],
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
            summary=("两篇可核查论文比较了低照度显微去噪；公开全文只覆盖其中一篇。"),
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
            web_evidence={
                "schema_ref": "meta-research/deepfetch-web-evidence/v1",
                "search_event_count": 1,
                "fetch_event_count": 2,
                "trace_hash": "d" * 64,
            },
            papers_ledger=copy.deepcopy(DETERMINISTIC_LEDGER),
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


class CancellableBlockingDeepFetchProvider(BlockingDeepFetchProvider):
    def __init__(self) -> None:
        super().__init__()
        self.cancelled_job_refs: list[str] = []

    def cancel_job(self, job_ref: str) -> None:
        self.cancelled_job_refs.append(job_ref)
        self.release.set()
        raise RuntimeError("provider cancellation callback failed after release")


class PendingOnceDeepFetchProvider(DeterministicDeepFetchProvider):
    def execute(self, request: DeepFetchProviderRequest) -> DeepFetchResult:
        self.requests.append(request)
        if len(self.requests) == 1:
            raise DeepFetchUnavailable("deepfetch_provider_reconciliation_pending")
        return self.result()


class AlwaysPendingDeepFetchProvider(DeterministicDeepFetchProvider):
    def execute(self, request: DeepFetchProviderRequest) -> DeepFetchResult:
        self.requests.append(request)
        raise DeepFetchUnavailable("deepfetch_provider_reconciliation_pending")


class SnapshotAwareProposalDrafter:
    def __init__(self) -> None:
        self.requests: list[ProposalDraftRequest] = []

    def draft(self, request: ProposalDraftRequest) -> ProposalDraftResult:
        self.requests.append(request)
        assert request.literature_snapshot is not None
        assert request.literature_snapshot["completion"] == "limited"
        assert request.literature_snapshot["summary"].startswith("两篇可核查论文")
        content = dict(QUESTION)
        if len(self.requests) > 1:
            content["background_context"] += f"（第 {len(self.requests)} 次生成）"
        return ProposalDraftResult(content=content, adapter_kind="test_drafter")


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
            "literature": {
                **draft["literature"],
                "mode": "oa_only",
                "library_entry_url": "",
            },
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
    prepared_response = client.post(
        f"/api/v1/quest-initializations/{initialization_id}/acquisition-session",
        headers=_write_headers(write_headers, f"{key_prefix}-acquisition"),
        json={
            "expected_draft_revision": saved["quest_draft"]["revision"],
            "expected_draft_hash": saved["quest_draft"]["hash"],
        },
    )
    prepared_response.raise_for_status()
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

        prepared_response = client.post(
            f"/api/v1/quest-initializations/{opened['initialization_id']}"
            "/acquisition-session",
            headers=_write_headers(write_headers, "deepfetch-acquisition"),
            json={
                "expected_draft_revision": saved["quest_draft"]["revision"],
                "expected_draft_hash": saved["quest_draft"]["hash"],
            },
        )
        prepared_response.raise_for_status()

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
        assert (
            after_research["deepfetch"]["run"]["execution_receipt"]["status"]
            == "accepted"
        )
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
        assert snapshot_response.json()["papers_ledger"] == DETERMINISTIC_LEDGER
        assert snapshot_response.json()["papers_ledger"]["papers"][
            "doi:10.1000/example.one"
        ]["reading"]["key_claims"][0]["evidence_locators"] == ["loc-1"]
        assert snapshot_response.json()["web_evidence"] == {
            "schema_ref": "meta-research/deepfetch-web-evidence/v1",
            "search_event_count": 1,
            "fetch_event_count": 2,
            "trace_hash": "d" * 64,
        }

        assert runtime.owners.human_collaboration.process_drafting_once()
        ready = client.get(
            f"/api/v1/quest-initializations/{opened['initialization_id']}"
        ).json()
        assert ready["status"] == "proposal_ready"
        assert ready["proposal"]["content"] == QUESTION
        assert ready["proposal"]["literature_snapshot_ref"] == snapshot["snapshot_ref"]
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
        regenerating = replay.json()
        assert regenerating["status"] == "proposal_generating"
        assert regenerating["deepfetch"]["run"]["run_ref"] == (
            ready["deepfetch"]["run"]["run_ref"]
        )
        assert len(deepfetch_provider.requests) == 1
        assert len(proposal_drafter.requests) == 1

        stale_preview = ready["confirmation_preview"]
        stale_confirmation = client.post(
            f"/api/v1/quest-initializations/{opened['initialization_id']}"
            "/confirmation",
            headers=_write_headers(write_headers, "deepfetch-stale-confirm"),
            json={
                "quest_draft_revision": ready["quest_draft"]["revision"],
                "quest_draft_hash": ready["quest_draft"]["hash"],
                "proposal_ref": ready["proposal"]["ref"],
                "proposal_hash": ready["proposal"]["hash"],
                "preview_ref": stale_preview["ref"],
                "preview_hash": stale_preview["hash"],
            },
        )
        assert stale_confirmation.status_code == 409
        assert stale_confirmation.json()["detail"]["code"] == (
            "confirmation_preview_stale"
        )

        assert runtime.owners.human_collaboration.process_drafting_once()
        regenerated = client.get(
            f"/api/v1/quest-initializations/{opened['initialization_id']}"
        ).json()
        assert len(deepfetch_provider.requests) == 1
        assert len(proposal_drafter.requests) == 2
        assert regenerated["proposal"]["ref"] != ready["proposal"]["ref"]
        assert regenerated["proposal"]["hash"] != ready["proposal"]["hash"]
        assert regenerated["proposal"]["revision"] == ready["proposal"]["revision"] + 1
        assert regenerated["proposal"]["literature_snapshot_ref"] == (
            ready["proposal"]["literature_snapshot_ref"]
        )
        assert regenerated["deepfetch"]["literature_snapshot"]["snapshot_ref"] == (
            snapshot["snapshot_ref"]
        )
        assert regenerated["confirmation_preview"]["ref"] != (
            ready["confirmation_preview"]["ref"]
        )
        provider_request = deepfetch_provider.requests[0]
        assert provider_request.draft_revision == saved["quest_draft"]["revision"]
        assert provider_request.draft_hash == saved["quest_draft"]["hash"]
        assert provider_request.authorization_receipt.issuer == ("human_collaboration")

        ready = regenerated
        preview = regenerated["confirmation_preview"]
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
        assert confirmed_response.status_code == 202, confirmed_response.json()
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
        assert (
            completed["proposal"]["literature_snapshot_ref"] == snapshot["snapshot_ref"]
        )
        started = runtime.idea_stage.start("deepfetch-idea-stage-start")
        stage_request = runtime.owners.advancement_engine.query_idea_stage_request(
            str(completed["cycle_ref"])
        )
        assert started["stage_run_request"] is not None
        assert stage_request is not None
        literature_binding = stage_request.context_pack["literature_binding"]
        assert literature_binding == {
            "schema_ref": "meta-research/idea-literature-binding/v1",
            "snapshot_ref": snapshot["snapshot_ref"],
            "snapshot_hash": snapshot["snapshot_hash"],
            "initialization_id": opened["initialization_id"],
            "draft_revision": ready["quest_draft"]["revision"],
            "draft_hash": ready["quest_draft"]["hash"],
            "receipt": snapshot["receipt"],
        }
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
        failed = client.get(f"/api/v1/quest-initializations/{initialization_id}").json()
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


@pytest.mark.parametrize(
    ("failure_mode", "failure_code"),
    [
        ("nonzero", "codex_deepfetch_failed"),
        ("invalid_result", "codex_deepfetch_output_invalid"),
        ("missing_web_evidence", "deepfetch_web_evidence_invalid"),
    ],
)
def test_terminal_codex_failure_retries_a_new_operation_in_the_same_native_session(
    tmp_path: Path, failure_mode: str, failure_code: str
) -> None:
    executable = tmp_path / "fake-codex"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import pathlib
import sys

arguments = sys.argv[1:]
counter_path = pathlib.Path(__file__).with_suffix('.count')
arguments_path = pathlib.Path(__file__).with_suffix('.arguments')
count = int(counter_path.read_text()) + 1 if counter_path.exists() else 1
counter_path.write_text(str(count), encoding='utf-8')
with arguments_path.open('a', encoding='utf-8') as stream:
    stream.write(json.dumps(arguments) + '\\n')
prompt = sys.stdin.read()
thread_ref = 'native-terminal-retry'
print(json.dumps({'type': 'thread.started', 'thread_id': thread_ref}), flush=True)
failure_mode = '__FAILURE_MODE__'
if count == 1 and failure_mode == 'nonzero':
    raise SystemExit(7)
if count > 1 and ('resume' not in arguments or thread_ref not in arguments):
    raise SystemExit(8)
if count > 1 or failure_mode != 'missing_web_evidence':
    print(json.dumps({'type': 'item.completed', 'item': {
        'id': 'search-terminal', 'type': 'web_search', 'query': 'paper',
        'action': {'type': 'search'}}}), flush=True)
    print(json.dumps({'type': 'item.completed', 'item': {
        'id': 'open-terminal', 'type': 'web_search', 'query': '',
        'action': {'type': 'other'}}}), flush=True)
result_path = pathlib.Path(arguments[arguments.index('--output-last-message') + 1])
output_root = pathlib.Path(next(
    line.split('=', 1)[1] for line in prompt.splitlines()
    if line.startswith('public_output_root=')
))
(output_root / 'fulltext').mkdir(parents=True, exist_ok=True)
(output_root / 'papers.json').write_text(
    json.dumps(__LEDGER__, ensure_ascii=False), encoding='utf-8')
(output_root / 'summary.md').write_text(
    '# 范围\\n\\n本轮检索未形成可纳入的精确论文。\\n', encoding='utf-8')
payload = {} if count == 1 and failure_mode == 'invalid_result' else __FINAL__
result_path.write_text(json.dumps(payload), encoding='utf-8')
""".replace("__LEDGER__", repr(PROTOTYPE_EMPTY_LEDGER)).replace(
            "__FINAL__", repr(PROTOTYPE_EMPTY_FINAL)
        ).replace("__FAILURE_MODE__", failure_mode),
        encoding="utf-8",
    )
    executable.chmod(0o700)
    data_root = prepare_data_root(tmp_path / "data")
    adapter = CodexDeepFetchAdapter(
        data_root.run / "deepfetch-provider",
        executable=str(executable),
        model_ref="gpt-test",
        timeout_seconds=10,
    )
    runtime = build_production_runtime(
        data_root,
        proposal_drafter=SnapshotAwareProposalDrafter(),
        deepfetch_provider=adapter,
        host_compute_probe=DeterministicProbe(),
    )
    client, write_headers = _authenticate(runtime)
    try:
        initialization_id, queued = _open_and_queue_deepfetch(
            client, write_headers, key_prefix="terminal-retry"
        )
        request_ref = queued["deepfetch"]["request_ref"]
        assert runtime.deepfetch.process_once()
        failed = client.get(f"/api/v1/quest-initializations/{initialization_id}").json()
        first_run = failed["deepfetch"]["run"]
        assert failed["deepfetch"]["failure"] == {"code": failure_code}
        assert first_run["native_session_ref"] == "native-terminal-retry"
        with runtime._database.read() as connection:
            first_operation = connection.execute(
                text(
                    "SELECT provider_operation_ref, provider_operation_generation "
                    "FROM ar_deepfetch_runs WHERE request_ref = :request_ref"
                ),
                {"request_ref": request_ref},
            ).one()
        assert int(first_operation.provider_operation_generation) == 1

        retry = client.post(
            f"/api/v1/quest-initializations/{initialization_id}"
            "/proposal-generations",
            headers=_write_headers(write_headers, "terminal-retry-again"),
            json={
                "expected_draft_revision": failed["quest_draft"]["revision"],
                "expected_draft_hash": failed["quest_draft"]["hash"],
            },
        )
        retry.raise_for_status()
        assert runtime.deepfetch.process_once()
        succeeded = client.get(
            f"/api/v1/quest-initializations/{initialization_id}"
        ).json()
        second_run = succeeded["deepfetch"]["run"]
        assert succeeded["deepfetch"]["status"] == "succeeded"
        assert second_run["run_ref"] == first_run["run_ref"]
        assert second_run["root_session_ref"] == first_run["root_session_ref"]
        assert second_run["native_session_ref"] == "native-terminal-retry"
        assert second_run["attempt_generation"] == 2
        with runtime._database.read() as connection:
            second_operation = connection.execute(
                text(
                    "SELECT provider_operation_ref, provider_operation_generation "
                    "FROM ar_deepfetch_runs WHERE request_ref = :request_ref"
                ),
                {"request_ref": request_ref},
            ).one()
        assert int(second_operation.provider_operation_generation) == 2
        assert second_operation.provider_operation_ref != (
            first_operation.provider_operation_ref
        )
        assert executable.with_suffix(".count").read_text(encoding="utf-8") == "2"
        arguments = [
            json.loads(line)
            for line in executable.with_suffix(".arguments")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        assert "resume" not in arguments[0]
        assert arguments[1][-3:] == ["resume", "native-terminal-retry", "-"]
    finally:
        client.close()
        runtime.close()


def test_reconciliation_pending_uses_persisted_backoff_without_attempt_churn(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "data")
    provider = PendingOnceDeepFetchProvider()
    runtime = build_production_runtime(
        data_root,
        proposal_drafter=SnapshotAwareProposalDrafter(),
        deepfetch_provider=provider,
        host_compute_probe=DeterministicProbe(),
    )
    client, write_headers = _authenticate(runtime)
    initialization_id, queued = _open_and_queue_deepfetch(
        client, write_headers, key_prefix="reconcile-backoff"
    )
    request_ref = queued["deepfetch"]["request_ref"]
    try:
        assert not runtime.deepfetch.process_once()
        with runtime._database.read() as connection:
            before = connection.execute(
                text(
                    "SELECT attempt_generation, reconciliation_attempt_count, "
                    "next_reconcile_at, provider_operation_ref FROM "
                    "ar_deepfetch_runs WHERE request_ref = :request_ref"
                ),
                {"request_ref": request_ref},
            ).one()
            feed_count = connection.execute(
                text("SELECT count(*) FROM durable_feed")
            ).scalar_one()
        assert int(before.attempt_generation) == 1
        assert int(before.reconciliation_attempt_count) == 1
        assert before.next_reconcile_at is not None
    finally:
        client.close()
        runtime.close()

    restarted = build_production_runtime(
        data_root,
        proposal_drafter=SnapshotAwareProposalDrafter(),
        deepfetch_provider=provider,
        host_compute_probe=DeterministicProbe(),
    )
    try:
        assert not restarted.deepfetch.process_once()
        with restarted._database.read() as connection:
            waiting = connection.execute(
                text(
                    "SELECT attempt_generation, reconciliation_attempt_count, "
                    "provider_operation_ref FROM ar_deepfetch_runs WHERE "
                    "request_ref = :request_ref"
                ),
                {"request_ref": request_ref},
            ).one()
            waiting_feed_count = connection.execute(
                text("SELECT count(*) FROM durable_feed")
            ).scalar_one()
        assert int(waiting.attempt_generation) == 1
        assert int(waiting.reconciliation_attempt_count) == 1
        assert waiting.provider_operation_ref == before.provider_operation_ref
        assert waiting_feed_count == feed_count
        assert len(provider.requests) == 1

        time.sleep(0.6)
        assert restarted.deepfetch.process_once()
        completed = restarted.owners.human_collaboration.query_quest_creation(
            initialization_id
        )
        assert completed["deepfetch"]["status"] == "succeeded"
        assert len(provider.requests) == 2
        assert provider.requests[1].job_ref == provider.requests[0].job_ref
    finally:
        restarted.close()


def test_reconciliation_without_a_receipt_becomes_a_bounded_typed_blocker(
    tmp_path: Path,
) -> None:
    provider = AlwaysPendingDeepFetchProvider()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "data"),
        proposal_drafter=SnapshotAwareProposalDrafter(),
        deepfetch_provider=provider,
        host_compute_probe=DeterministicProbe(),
    )
    client, write_headers = _authenticate(runtime)
    try:
        initialization_id, queued = _open_and_queue_deepfetch(
            client, write_headers, key_prefix="reconcile-bounded"
        )
        request_ref = queued["deepfetch"]["request_ref"]
        assert not runtime.deepfetch.process_once()
        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE ar_deepfetch_runs SET reconciliation_attempt_count = 39, "
                    "next_reconcile_at = NULL WHERE request_ref = :request_ref"
                ),
                {"request_ref": request_ref},
            )

        assert runtime.deepfetch.process_once()
        blocked = client.get(
            f"/api/v1/quest-initializations/{initialization_id}"
        ).json()
        assert blocked["deepfetch"]["status"] == "failed"
        assert blocked["deepfetch"]["failure"] == {
            "code": "deepfetch_provider_outcome_unknown"
        }
        with runtime._database.read() as connection:
            run = connection.execute(
                text(
                    "SELECT provider_operation_retry_permitted, attempt_generation "
                    "FROM ar_deepfetch_runs WHERE request_ref = :request_ref"
                ),
                {"request_ref": request_ref},
            ).one()
        assert int(run.provider_operation_retry_permitted) == 0
        assert int(run.attempt_generation) == 2
    finally:
        client.close()
        runtime.close()


def test_running_deepfetch_cancel_targets_the_persisted_provider_operation(
    tmp_path: Path,
) -> None:
    provider = CancellableBlockingDeepFetchProvider()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "data"),
        proposal_drafter=SnapshotAwareProposalDrafter(),
        deepfetch_provider=provider,
        host_compute_probe=DeterministicProbe(),
    )
    client, write_headers = _authenticate(runtime)
    errors: list[BaseException] = []
    try:
        initialization_id, _queued = _open_and_queue_deepfetch(
            client, write_headers, key_prefix="cancel-running"
        )

        def execute() -> None:
            try:
                runtime.deepfetch.process_once()
            except BaseException as error:  # pragma: no branch - asserted below
                errors.append(error)

        worker = threading.Thread(target=execute, daemon=True)
        worker.start()
        assert provider.started.wait(timeout=5)
        expected_job_ref = provider.requests[0].job_ref
        assert expected_job_ref is not None

        cancelled_response = client.post(
            f"/api/v1/quest-initializations/{initialization_id}/cancel",
            headers=_write_headers(write_headers, "cancel-running-command"),
            json={},
        )
        cancelled_response.raise_for_status()
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert errors == []
        assert provider.cancelled_job_refs == [expected_job_ref]
        assert cancelled_response.json()["status"] == "cancelled"
        cancelled_run = runtime.owners.agent_runtime.query_deepfetch_run(
            provider.requests[0].request_ref
        )
        assert cancelled_run is not None
        assert cancelled_run.status == "cancelled"
        assert (
            runtime.owners.research_memory.query_snapshot().facts[
                "literature_snapshot_count"
            ]
            == 0
        )
        assert (
            runtime.owners.human_collaboration.query_quest_creation(initialization_id)[
                "deepfetch"
            ]["literature_snapshot"]
            is None
        )
    finally:
        provider.release.set()
        client.close()
        runtime.close()


def test_agent_runtime_rejects_materials_not_signed_by_human_collaboration(
    tmp_path: Path,
) -> None:
    provider = DeterministicDeepFetchProvider()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "data"),
        proposal_drafter=SnapshotAwareProposalDrafter(),
        deepfetch_provider=provider,
        host_compute_probe=DeterministicProbe(),
    )
    client, write_headers = _authenticate(runtime)
    try:
        _initialization_id, queued = _open_and_queue_deepfetch(
            client, write_headers, key_prefix="forged-material"
        )
        request = runtime.owners.human_collaboration.query_deepfetch_request(
            str(queued["deepfetch"]["request_ref"])
        )
        assert request is not None
        forged = replace(
            request,
            accepted_material_bindings=({"version_ref": "forged"},),
        )

        with pytest.raises(OwnerConflict, match="deepfetch_request_receipt_invalid"):
            runtime.owners.agent_runtime.execute_deepfetch(forged, provider)

        assert provider.requests == []
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
        old_snapshot_ref = original["deepfetch"]["literature_snapshot"]["snapshot_ref"]
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
        assert (
            old_snapshot_response.json()["draft_hash"]
            == original["quest_draft"]["hash"]
        )

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


def test_snapshot_is_part_of_the_immutable_question_proposal_identity(
    tmp_path: Path,
) -> None:
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "data"),
        proposal_drafter=SnapshotAwareProposalDrafter(),
        deepfetch_provider=DeterministicDeepFetchProvider(),
        host_compute_probe=DeterministicProbe(),
    )
    client, write_headers = _authenticate(runtime)
    try:
        initialization_id, _queued = _open_and_queue_deepfetch(
            client, write_headers, key_prefix="proposal-snapshot-tamper"
        )
        assert runtime.deepfetch.process_once()
        assert runtime.owners.human_collaboration.process_drafting_once()
        ready = runtime.owners.human_collaboration.query_quest_creation(
            initialization_id
        )
        proposal = ready["proposal"]
        preview = ready["confirmation_preview"]
        assert isinstance(proposal, dict)
        assert isinstance(preview, dict)
        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE hc_question_proposals SET literature_snapshot_ref = "
                    "NULL, literature_snapshot_hash = NULL WHERE proposal_ref = "
                    ":proposal_ref"
                ),
                {"proposal_ref": proposal["ref"]},
            )

        response = client.post(
            f"/api/v1/quest-initializations/{initialization_id}/confirmation",
            headers=_write_headers(write_headers, "proposal-snapshot-confirm"),
            json={
                "quest_draft_revision": ready["quest_draft"]["revision"],
                "quest_draft_hash": ready["quest_draft"]["hash"],
                "proposal_ref": proposal["ref"],
                "proposal_hash": proposal["hash"],
                "preview_ref": preview["ref"],
                "preview_hash": preview["hash"],
            },
        )

        assert response.status_code == 409
        assert response.json()["detail"]["code"] == (
            "quest_initialization_artifact_invalid"
        )
        assert runtime.owners.research_graph.query_quest(initialization_id) is None
        assert runtime.owners.research_graph.query_question(initialization_id) is None
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
        assert cancelled["deepfetch"]["failure"] == {"code": "initialization_cancelled"}
        assert not runtime.deepfetch.process_once()
        assert cancelled["proposal"] is None
        assert cancelled.get("quest_ref") is None
        assert provider.requests == []
    finally:
        client.close()
        runtime.close()


def test_cancel_after_worker_reads_request_prevents_runtime_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = DeterministicDeepFetchProvider()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "data"),
        proposal_drafter=SnapshotAwareProposalDrafter(),
        deepfetch_provider=provider,
        host_compute_probe=DeterministicProbe(),
    )
    client, write_headers = _authenticate(runtime)
    preflight_verified = threading.Event()
    resume_worker = threading.Event()
    errors: list[BaseException] = []
    try:
        initialization_id, queued = _open_and_queue_deepfetch(
            client, write_headers, key_prefix="cancel-after-read"
        )
        request_ref = str(queued["deepfetch"]["request_ref"])
        verifier = runtime.owners.agent_runtime._deepfetch_request_verifier
        assert verifier is not None
        verify_request = verifier.verify_deepfetch_run_request

        def verify_then_pause(**values: object) -> None:
            verify_request(**values)
            if not bool(values.get("require_active", False)):
                preflight_verified.set()
                assert resume_worker.wait(timeout=10)

        monkeypatch.setattr(
            verifier,
            "verify_deepfetch_run_request",
            verify_then_pause,
        )

        def execute() -> None:
            try:
                runtime.deepfetch.process_once()
            except BaseException as error:  # pragma: no branch - asserted below
                errors.append(error)

        worker = threading.Thread(target=execute, daemon=True)
        worker.start()
        assert preflight_verified.wait(timeout=5)

        cancelled_response = client.post(
            f"/api/v1/quest-initializations/{initialization_id}/cancel",
            headers=_write_headers(write_headers, "cancel-after-read-command"),
            json={},
        )
        cancelled_response.raise_for_status()
        resume_worker.set()
        worker.join(timeout=5)

        assert not worker.is_alive()
        assert errors == []
        assert provider.requests == []
        assert runtime.owners.agent_runtime.query_deepfetch_run(request_ref) is None
        assert (
            runtime.owners.research_memory.query_snapshot().facts[
                "literature_snapshot_count"
            ]
            == 0
        )
        assert cancelled_response.json()["deepfetch"]["status"] == "cancelled"
    finally:
        resume_worker.set()
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
        failed = client.get(f"/api/v1/quest-initializations/{initialization_id}").json()
        assert failed["deepfetch"]["status"] == "failed"
        assert failed["deepfetch"]["failure"] == {"code": "deepfetch_paper_url_invalid"}
        assert failed["deepfetch"]["literature_snapshot"] is None
        assert failed["proposal"] is None
        assert (
            runtime.owners.research_memory.query_snapshot().facts[
                "literature_snapshot_count"
            ]
            == 0
        )
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

        restarted_provider = BlockingDeepFetchProvider()
        restarted_runtime = build_production_runtime(
            prepare_data_root(tmp_path / "data"),
            proposal_drafter=SnapshotAwareProposalDrafter(),
            deepfetch_provider=restarted_provider,
            host_compute_probe=DeterministicProbe(),
        )
        successor_errors: list[BaseException] = []

        def run_successor_worker() -> None:
            try:
                restarted_runtime.deepfetch.process_once()
            except BaseException as error:  # pragma: no cover - asserted below
                successor_errors.append(error)

        successor = threading.Thread(target=run_successor_worker, daemon=True)
        successor.start()
        assert restarted_provider.started.wait(timeout=5)

        # Attempt 1 returns while Attempt 2 is still running.  Its old Fence
        # must be ignored instead of changing the HC request to failed.
        blocking_provider.release.set()
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert worker_errors == []
        during_successor = (
            restarted_runtime.owners.human_collaboration.query_quest_creation(
                initialization_id
            )
        )
        assert during_successor["deepfetch"]["status"] == "running"
        assert during_successor["deepfetch"]["failure"] is None
        assert during_successor["deepfetch"]["run"]["status"] == "running"
        assert during_successor["deepfetch"]["run"]["attempt_generation"] == 2

        restarted_provider.release.set()
        successor.join(timeout=5)
        assert not successor.is_alive()
        assert successor_errors == []
        recovered = restarted_runtime.owners.human_collaboration.query_quest_creation(
            initialization_id
        )
        recovered_run = recovered["deepfetch"]["run"]
        assert recovered["deepfetch"]["status"] == "succeeded"
        assert recovered_run["run_ref"] == first_run["run_ref"]
        assert recovered_run["root_session_ref"] == first_run["root_session_ref"]
        assert recovered_run["attempt_generation"] == 2
        assert recovered_run["native_session_ref"] == "native-late-result"
        after_late_result = (
            restarted_runtime.owners.human_collaboration.query_quest_creation(
                initialization_id
            )
        )
        assert after_late_result["deepfetch"]["run"]["attempt_generation"] == 2
        assert after_late_result["deepfetch"]["run"]["native_session_ref"] == (
            "native-late-result"
        )
        assert (
            restarted_runtime.owners.research_memory.query_snapshot().facts[
                "literature_snapshot_count"
            ]
            == 1
        )
    finally:
        blocking_provider.release.set()
        if restarted_runtime is not None:
            restarted_provider.release.set()
        if worker is not None:
            worker.join(timeout=5)
        client.close()
        first_runtime.close()
        if restarted_runtime is not None:
            restarted_runtime.close()
