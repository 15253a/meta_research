from __future__ import annotations

import copy
import json
import subprocess
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
from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.paths import prepare_data_root
from meta_research.quest_drafting import (
    HostComputeDevice,
    HostComputeSnapshot,
    ProposalDraftRequest,
    ProposalDraftResult,
)
from meta_research.runtime_protection import (
    InhibitorLease,
    RuntimeProtectionUnavailable,
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
            native_holder_ref=f"native:{holder_ref}",
        )

    def is_confirmed(self, lease: InhibitorLease) -> bool:
        return lease.holder_ref in self.live_holders

    def release(self, lease: InhibitorLease) -> None:
        self.live_holders.discard(lease.holder_ref)


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


class OversizedProposalEvidenceProvider(DeterministicDeepFetchProvider):
    def execute(self, request: DeepFetchProviderRequest) -> DeepFetchResult:
        self.requests.append(request)
        result = self.result()
        ledger = copy.deepcopy(DETERMINISTIC_LEDGER)
        ledger["topic"]["interpretation"] = "超出投影上限的证据" * 40_000
        return replace(result, papers_ledger=ledger)


class HonestEmptyDeepFetchProvider(DeterministicDeepFetchProvider):
    def execute(self, request: DeepFetchProviderRequest) -> DeepFetchResult:
        self.requests.append(request)
        return DeepFetchResult(
            completion="honest_empty",
            summary="本轮检索未形成可纳入的精确论文。",
            papers=(),
            fulltexts=(),
            limitations=("检索未形成可纳入的精确论文。",),
            native_session_ref="native-honest-empty",
            adapter_kind="test_deepfetch",
            web_evidence={
                "schema_ref": "meta-research/deepfetch-web-evidence/v1",
                "search_event_count": 1,
                "fetch_event_count": 1,
                "trace_hash": "e" * 64,
            },
            papers_ledger=copy.deepcopy(PROTOTYPE_EMPTY_LEDGER),
        )


class LocalPathEvidenceProvider(DeterministicDeepFetchProvider):
    def execute(self, request: DeepFetchProviderRequest) -> DeepFetchResult:
        self.requests.append(request)
        result = self.result()
        return replace(
            result,
            web_evidence={
                **result.web_evidence,
                "prototype": {
                    "schema_ref": "meta-research/deepfetch-prototype-evidence/v4",
                    "prototype_commit": PROTOTYPE_COMMIT,
                    "acquisition_session_ref": "acquisition-session-test",
                    "acquisition_request_ids": [],
                    "main_agent_status": "complete",
                    "reader_assignments": [],
                    "finalize_status": "passed",
                    "papers_json_hash": "a" * 64,
                    "summary_md_hash": "b" * 64,
                    "fulltext_files": [
                        {
                            "path": "fulltext/paper-one.pdf",
                            "sha256": "c" * 64,
                            "bytes": 41,
                        }
                    ],
                },
            },
        )


class ExtraLocatorEvidenceProvider(LocalPathEvidenceProvider):
    def execute(self, request: DeepFetchProviderRequest) -> DeepFetchResult:
        result = super().execute(request)
        web_evidence = copy.deepcopy(result.web_evidence)
        web_evidence["prototype"]["fulltext_files"][0]["absolute_path"] = (
            "/private/custody/escape.pdf"
        )
        return replace(result, web_evidence=web_evidence)


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


class LegacyWorkspaceCodexDeepFetchAdapter(CodexDeepFetchAdapter):
    """The exact pre-permission-upgrade binding persisted by deployed Runs."""

    _sandbox_mode = "workspace-write"

    def runtime_binding(self) -> DeepFetchRuntimeBinding:
        current = super().runtime_binding()
        return replace(
            current,
            capability_bindings=tuple(
                capability
                for capability in current.capability_bindings
                if capability
                not in {
                    "filesystem-danger-full-access",
                    "sandbox-policy:danger-full-access",
                }
            ),
        )


class AckLossLegacyCodexDeepFetchAdapter(LegacyWorkspaceCodexDeepFetchAdapter):
    """Expose a sealed predecessor outcome while simulating an AR ACK loss."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._lose_next_owner_ack = True

    def execute(self, request: DeepFetchProviderRequest) -> DeepFetchResult:
        try:
            result = super().execute(request)
        except DeepFetchUnavailable as error:
            if self._lose_next_owner_ack and error.durable_outcome == "terminal":
                self._lose_next_owner_ack = False
                raise DeepFetchUnavailable(
                    "deepfetch_provider_reconciliation_pending",
                    durable_outcome="pending",
                    native_session_ref=error.native_session_ref,
                ) from error
            raise
        if self._lose_next_owner_ack:
            self._lose_next_owner_ack = False
            raise DeepFetchUnavailable(
                "deepfetch_provider_reconciliation_pending",
                durable_outcome="pending",
                native_session_ref=result.native_session_ref,
            )
        return result


class UnpartitionedAckLossCodexDeepFetchAdapter(
    AckLossLegacyCodexDeepFetchAdapter
):
    """The deployed pre-0038 filesystem shape, produced without renaming."""

    def runtime_binding(self) -> DeepFetchRuntimeBinding:
        current = super().runtime_binding()
        return replace(
            current,
            capability_bindings=tuple(
                capability
                for capability in current.capability_bindings
                if capability
                != "agent-workspace-policy:dedicated-research-workspace-v1"
                and not capability.startswith("provider-output-limits:")
            ),
        )

    def _protocol_run_root(self, run_key: str, runtime_binding_hash: str) -> Path:
        del runtime_binding_hash
        return self._workspace / "runs" / run_key

    def _provider_operation_root(
        self, job_ref: str, runtime_binding_hash: str
    ) -> Path:
        del runtime_binding_hash
        return self._workspace / "provider-operations" / canonical_hash(
            {"job_ref": job_ref}
        )

    def _durable_argv(
        self,
        *,
        schema_path: Path,
        result_path: Path,
        native_session_ref: str | None,
    ) -> list[str]:
        arguments = super()._durable_argv(
            schema_path=schema_path,
            result_path=result_path,
            native_session_ref=native_session_ref,
        )
        arguments[arguments.index("--cd") + 1] = str(self._workspace)
        return arguments


class PreDispatchPendingCodexDeepFetchAdapter(
    UnpartitionedAckLossCodexDeepFetchAdapter
):
    """Crash after protocol checkpoint publication but before provider dispatch."""

    def _execute_protocol(self, *_args, **_kwargs) -> DeepFetchResult:
        raise DeepFetchUnavailable(
            "deepfetch_provider_reconciliation_pending",
            durable_outcome="pending",
        )


class BoundDeepFetchProvider(DeterministicDeepFetchProvider):
    def __init__(
        self,
        binding: DeepFetchRuntimeBinding,
        *,
        outcome: str = "executed",
    ) -> None:
        super().__init__()
        self._binding = binding
        self._outcome = outcome

    @property
    def requires_verified_terminal_retry(self) -> bool:
        return self._outcome == "unverified_failure"

    def runtime_binding(self) -> DeepFetchRuntimeBinding:
        return self._binding

    def execute(self, request: DeepFetchProviderRequest) -> DeepFetchResult:
        self.requests.append(request)
        if self._outcome == "waiting_user":
            raise DeepFetchUnavailable(
                "deepfetch_acquisition_waiting_user",
                durable_outcome="pending",
                native_session_ref="native-old-binding",
            )
        if self._outcome == "unverified_failure":
            raise DeepFetchUnavailable("codex_deepfetch_failed")
        return self.result(native_session_ref="native-old-binding")


class BlockingBoundDeepFetchProvider(BoundDeepFetchProvider):
    def __init__(self, binding: DeepFetchRuntimeBinding) -> None:
        super().__init__(binding)
        self.started = threading.Event()
        self.release = threading.Event()

    def execute(self, request: DeepFetchProviderRequest) -> DeepFetchResult:
        self.requests.append(request)
        self.started.set()
        assert self.release.wait(timeout=10)
        return self.result(native_session_ref="native-old-binding")


class PendingBoundDeepFetchProvider(BoundDeepFetchProvider):
    """Leave one old-binding durable operation awaiting Owner reconciliation."""

    def execute(self, request: DeepFetchProviderRequest) -> DeepFetchResult:
        self.requests.append(request)
        raise DeepFetchUnavailable(
            "deepfetch_provider_reconciliation_pending",
            durable_outcome="pending",
            native_session_ref="native-old-binding",
        )


class UpgradedReconciliationProvider(BoundDeepFetchProvider):
    """A new adapter able to read the predecessor's sealed durable outcome."""

    def __init__(
        self,
        old_binding: DeepFetchRuntimeBinding,
        new_binding: DeepFetchRuntimeBinding,
        *,
        old_outcome: str,
    ) -> None:
        super().__init__(new_binding)
        self._old_binding = old_binding
        self._new_binding = new_binding
        self._old_outcome = old_outcome

    @property
    def requires_verified_terminal_retry(self) -> bool:
        return True

    def execute(self, request: DeepFetchProviderRequest) -> DeepFetchResult:
        self.requests.append(request)
        if request.runtime_binding == self._old_binding:
            if self._old_outcome == "success":
                return self.result(native_session_ref="native-old-binding")
            if self._old_outcome == "terminal_failure":
                raise DeepFetchUnavailable(
                    "codex_deepfetch_failed",
                    durable_outcome="terminal",
                    native_session_ref="native-old-binding",
                )
        if request.runtime_binding == self._new_binding:
            return self.result(native_session_ref="native-new-binding")
        raise AssertionError("unexpected DeepFetch reconciliation binding")


class PendingDurableRunner:
    def __init__(self) -> None:
        self.calls = 0

    def run_durable_job(self, *_args, **_kwargs) -> None:
        self.calls += 1
        raise subprocess.TimeoutExpired("fake-codex", 10)


class SnapshotAwareProposalDrafter:
    def __init__(self, expected_completion: str = "limited") -> None:
        self.requests: list[ProposalDraftRequest] = []
        self.expected_completion = expected_completion

    def draft(self, request: ProposalDraftRequest) -> ProposalDraftResult:
        self.requests.append(request)
        assert request.literature_snapshot is not None
        assert request.literature_snapshot["schema_ref"] == (
            "meta-research/proposal-literature-evidence/v1"
        )
        assert request.literature_snapshot["completion"] == self.expected_completion
        if self.expected_completion == "limited":
            assert request.literature_snapshot["summary"].startswith(
                "两篇可核查论文"
            )
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
        assert (
            after_research["deepfetch"]["run"][
                "provider_operation_retry_permitted"
            ]
            is False
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
        proposal_evidence = proposal_drafter.requests[0].literature_snapshot
        assert proposal_evidence is not None
        assert proposal_evidence["schema_ref"] == (
            "meta-research/proposal-literature-evidence/v1"
        )
        projection_payload = dict(proposal_evidence)
        projection_hash = projection_payload.pop("projection_hash")
        assert projection_hash == canonical_hash(projection_payload)
        source = proposal_evidence["source_snapshot"]
        assert source["snapshot_ref"] == snapshot["snapshot_ref"]
        assert source["snapshot_hash"] == snapshot["snapshot_hash"]
        assert source["receipt"] == snapshot["receipt"]
        assert canonical_hash(source["binding"]) == source["snapshot_hash"]
        assert source["binding"]["initialization_id"] == opened[
            "initialization_id"
        ]
        assert source["binding"]["draft_revision"] == saved["quest_draft"][
            "revision"
        ]
        assert source["binding"]["draft_hash"] == saved["quest_draft"]["hash"]
        assert proposal_evidence["papers_ledger"]["papers"][
            "doi:10.1000/example.one"
        ]["reading"]["key_claims"][0]["evidence_locators"] == ["loc-1"]
        assert proposal_evidence["limitations"] == [
            "第二篇论文没有可合法获取的开放全文。"
        ]
        assert proposal_evidence["fulltexts"] == [
            {
                "paper_url": "https://example.org/papers/one",
                "media_type": "text/plain",
                "content_hash": snapshot_response.json()["fulltexts"][0][
                    "content_hash"
                ],
            }
        ]
        serialized_evidence = json.dumps(
            proposal_evidence, ensure_ascii=False, sort_keys=True
        )
        assert "Verified open full text for paper one." not in serialized_evidence
        assert "fulltext_path" not in serialized_evidence
        assert len(serialized_evidence.encode("utf-8")) <= 192 * 1024
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
        assert first_run["provider_operation_retry_permitted"] is True
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
        assert second_run["provider_operation_retry_permitted"] is False
        assert succeeded["deepfetch"]["status"] == "succeeded"
        assert len(provider.requests) == 2
    finally:
        client.close()
        runtime.close()


def test_oversized_proposal_evidence_fails_closed_without_calling_the_drafter(
    tmp_path: Path,
) -> None:
    proposal_drafter = SnapshotAwareProposalDrafter()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "data"),
        proposal_drafter=proposal_drafter,
        deepfetch_provider=OversizedProposalEvidenceProvider(),
        host_compute_probe=DeterministicProbe(),
    )
    client, write_headers = _authenticate(runtime)
    try:
        initialization_id, _queued = _open_and_queue_deepfetch(
            client, write_headers, key_prefix="oversized-proposal-evidence"
        )
        assert runtime.deepfetch.process_once()

        snapshot = runtime.owners.human_collaboration.query_quest_creation(
            initialization_id
        )["deepfetch"]["literature_snapshot"]
        assert snapshot is not None
        with pytest.raises(
            OwnerConflict, match="codex_proposal_evidence_too_large"
        ):
            runtime.owners.research_memory.read_literature_proposal_evidence(
                snapshot["snapshot_ref"]
            )

        assert runtime.owners.human_collaboration.process_drafting_once()
        failed = runtime.owners.human_collaboration.query_quest_creation(
            initialization_id
        )
        assert failed["proposal_generation"]["status"] == "failed"
        assert failed["proposal_generation"]["failure"] == {
            "code": "codex_proposal_evidence_too_large"
        }
        assert proposal_drafter.requests == []
    finally:
        client.close()
        runtime.close()


def test_honest_empty_proposal_evidence_preserves_the_empty_result_and_limit(
    tmp_path: Path,
) -> None:
    proposal_drafter = SnapshotAwareProposalDrafter(
        expected_completion="honest_empty"
    )
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "data"),
        proposal_drafter=proposal_drafter,
        deepfetch_provider=HonestEmptyDeepFetchProvider(),
        host_compute_probe=DeterministicProbe(),
    )
    client, write_headers = _authenticate(runtime)
    try:
        initialization_id, _queued = _open_and_queue_deepfetch(
            client, write_headers, key_prefix="honest-empty-proposal-evidence"
        )
        assert runtime.deepfetch.process_once()
        assert runtime.owners.human_collaboration.process_drafting_once()

        evidence = proposal_drafter.requests[0].literature_snapshot
        assert evidence is not None
        assert evidence["completion"] == "honest_empty"
        assert evidence["source_snapshot"]["binding"]["completion"] == (
            "honest_empty"
        )
        assert evidence["papers"] == []
        assert evidence["fulltexts"] == []
        assert evidence["limitations"] == ["检索未形成可纳入的精确论文。"]
        current = runtime.owners.human_collaboration.query_quest_creation(
            initialization_id
        )
        assert current["proposal_generation"]["status"] == "succeeded"
    finally:
        client.close()
        runtime.close()


def test_proposal_evidence_keeps_fulltext_proofs_but_removes_local_paths(
    tmp_path: Path,
) -> None:
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "data"),
        proposal_drafter=SnapshotAwareProposalDrafter(),
        deepfetch_provider=LocalPathEvidenceProvider(),
        host_compute_probe=DeterministicProbe(),
    )
    client, write_headers = _authenticate(runtime)
    try:
        initialization_id, _queued = _open_and_queue_deepfetch(
            client, write_headers, key_prefix="local-path-proposal-evidence"
        )
        assert runtime.deepfetch.process_once()
        snapshot = runtime.owners.human_collaboration.query_quest_creation(
            initialization_id
        )["deepfetch"]["literature_snapshot"]
        assert snapshot is not None

        evidence = runtime.owners.research_memory.read_literature_proposal_evidence(
            snapshot["snapshot_ref"]
        )
        fulltext_proofs = evidence["web_evidence"]["prototype"][
            "fulltext_files"
        ]
        assert fulltext_proofs == [{"sha256": "c" * 64, "bytes": 41}]
        assert "fulltext/paper-one.pdf" not in json.dumps(
            evidence, ensure_ascii=False, sort_keys=True
        )
    finally:
        client.close()
        runtime.close()


def test_deepfetch_rejects_a_fulltext_proof_with_an_extra_locator_before_snapshot(
    tmp_path: Path,
) -> None:
    proposal_drafter = SnapshotAwareProposalDrafter()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "data"),
        proposal_drafter=proposal_drafter,
        deepfetch_provider=ExtraLocatorEvidenceProvider(),
        host_compute_probe=DeterministicProbe(),
    )
    client, write_headers = _authenticate(runtime)
    try:
        initialization_id, _queued = _open_and_queue_deepfetch(
            client, write_headers, key_prefix="extra-locator-proposal-evidence"
        )
        assert runtime.deepfetch.process_once()
        failed = runtime.owners.human_collaboration.query_quest_creation(
            initialization_id
        )
        assert failed["deepfetch"]["status"] == "failed"
        assert failed["deepfetch"]["failure"] == {
            "code": "deepfetch_prototype_evidence_invalid"
        }
        assert failed["deepfetch"]["literature_snapshot"] is None
        assert failed["proposal"] is None
        assert proposal_drafter.requests == []
        assert (
            runtime.owners.research_memory.query_snapshot().facts[
                "literature_snapshot_count"
            ]
            == 0
        )
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


def test_terminal_legacy_binding_retry_readmits_with_new_permissions_and_session(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "fake-codex-binding-transition"
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
thread_ref = 'native-legacy-binding' if count == 1 else 'native-new-binding'
print(json.dumps({'type': 'thread.started', 'thread_id': thread_ref}), flush=True)
if count > 1:
    if 'resume' in arguments:
        raise SystemExit(8)
    print(json.dumps({'type': 'item.completed', 'item': {
        'id': 'search-transition', 'type': 'web_search', 'query': 'paper',
        'action': {'type': 'search'}}}), flush=True)
    print(json.dumps({'type': 'item.completed', 'item': {
        'id': 'open-transition', 'type': 'web_search', 'query': '',
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
result_path.write_text(json.dumps(__FINAL__), encoding='utf-8')
""".replace("__LEDGER__", repr(PROTOTYPE_EMPTY_LEDGER)).replace(
            "__FINAL__", repr(PROTOTYPE_EMPTY_FINAL)
        ),
        encoding="utf-8",
    )
    executable.chmod(0o700)
    data_root = prepare_data_root(tmp_path / "data")
    workspace = data_root.run / "deepfetch-provider"
    legacy_adapter = LegacyWorkspaceCodexDeepFetchAdapter(
        workspace,
        executable=str(executable),
        model_ref="gpt-test",
        timeout_seconds=10,
    )
    old_binding = legacy_adapter.runtime_binding()
    old_binding_hash = canonical_hash(old_binding.as_dict())
    runtime = build_production_runtime(
        data_root,
        proposal_drafter=SnapshotAwareProposalDrafter(),
        deepfetch_provider=legacy_adapter,
        host_compute_probe=DeterministicProbe(),
    )
    client, write_headers = _authenticate(runtime)
    initialization_id, _queued = _open_and_queue_deepfetch(
        client, write_headers, key_prefix="binding-transition"
    )
    try:
        assert runtime.deepfetch.process_once()
        failed = client.get(
            f"/api/v1/quest-initializations/{initialization_id}"
        ).json()
        first_run = failed["deepfetch"]["run"]
        assert failed["deepfetch"]["failure"] == {
            "code": "deepfetch_web_evidence_invalid"
        }
        assert first_run["native_session_ref"] == "native-legacy-binding"
        assert first_run["runtime_binding_hash"] == old_binding_hash
        with runtime._database.read() as connection:
            first_attempt = connection.execute(
                text(
                    "SELECT attempt_ref, runtime_binding_json, "
                    "runtime_binding_hash, native_session_ref FROM "
                    "ar_deepfetch_attempts WHERE run_ref = :run_ref"
                ),
                {"run_ref": first_run["run_ref"]},
            ).one()
        assert first_attempt.runtime_binding_hash == old_binding_hash
        assert json.loads(first_attempt.runtime_binding_json) == old_binding.as_dict()
        assert first_attempt.native_session_ref == "native-legacy-binding"
        old_protocol_roots = set((workspace / "runs").iterdir())
        old_spool_roots = set((workspace / "provider-operations").iterdir())
        assert old_protocol_roots
        assert old_spool_roots
    finally:
        client.close()
        runtime.close()

    # Recreate the exact deployed pre-0038 layout. The old adapter keyed the
    # durable roots only by logical request/job identity, without a binding
    # partition in the directory name.
    for old_root in tuple(old_protocol_roots | old_spool_roots):
        if not old_root.is_dir() or not old_root.name.startswith(
            f"{old_binding_hash}-"
        ):
            continue
        legacy_root = old_root.with_name(
            old_root.name.removeprefix(f"{old_binding_hash}-")
        )
        old_root.rename(legacy_root)
    old_protocol_roots = set((workspace / "runs").iterdir())
    old_spool_roots = set((workspace / "provider-operations").iterdir())
    assert all(
        not path.name.startswith(f"{old_binding_hash}-")
        for path in old_protocol_roots | old_spool_roots
    )

    new_adapter = CodexDeepFetchAdapter(
        workspace,
        executable=str(executable),
        model_ref="gpt-test",
        timeout_seconds=10,
    )
    new_binding = new_adapter.runtime_binding()
    new_binding_hash = canonical_hash(new_binding.as_dict())
    assert new_binding_hash != old_binding_hash
    restarted = build_production_runtime(
        data_root,
        proposal_drafter=SnapshotAwareProposalDrafter(),
        deepfetch_provider=new_adapter,
        host_compute_probe=DeterministicProbe(),
    )
    restarted_client, restarted_headers = _authenticate(restarted)
    try:
        failed = restarted_client.get(
            f"/api/v1/quest-initializations/{initialization_id}"
        ).json()
        retry = restarted_client.post(
            f"/api/v1/quest-initializations/{initialization_id}"
            "/proposal-generations",
            headers=_write_headers(restarted_headers, "binding-transition-retry"),
            json={
                "expected_draft_revision": failed["quest_draft"]["revision"],
                "expected_draft_hash": failed["quest_draft"]["hash"],
            },
        )
        retry.raise_for_status()
        assert restarted.deepfetch.process_once()
        succeeded = restarted_client.get(
            f"/api/v1/quest-initializations/{initialization_id}"
        ).json()
        second_run = succeeded["deepfetch"]["run"]
        assert succeeded["deepfetch"]["status"] == "succeeded"
        assert second_run["run_ref"] == first_run["run_ref"]
        assert second_run["root_session_ref"] == first_run["root_session_ref"]
        assert second_run["attempt_generation"] == 2
        assert second_run["runtime_binding_hash"] == new_binding_hash
        assert second_run["native_session_ref"] == "native-new-binding"
        with restarted._database.read() as connection:
            attempts = connection.execute(
                text(
                    "SELECT generation, runtime_binding_json, "
                    "runtime_binding_hash, native_session_ref FROM "
                    "ar_deepfetch_attempts WHERE run_ref = :run_ref "
                    "ORDER BY generation"
                ),
                {"run_ref": first_run["run_ref"]},
            ).all()
        assert [row.runtime_binding_hash for row in attempts] == [
            old_binding_hash,
            new_binding_hash,
        ]
        assert [row.native_session_ref for row in attempts] == [
            "native-legacy-binding",
            "native-new-binding",
        ]
        assert json.loads(attempts[0].runtime_binding_json) == old_binding.as_dict()
        assert json.loads(attempts[1].runtime_binding_json) == new_binding.as_dict()
        transition = next(
            event
            for event in restarted.feed.read_after(0).events
            if event.event_type
            == "agent_runtime.deepfetch_runtime_binding_transitioned"
        )
        assert transition.payload["old_runtime_binding_hash"] == old_binding_hash
        assert transition.payload["new_runtime_binding_hash"] == new_binding_hash
        assert old_protocol_roots < set((workspace / "runs").iterdir())
        assert old_spool_roots < set((workspace / "provider-operations").iterdir())
        assert all(path.exists() for path in old_protocol_roots | old_spool_roots)
        assert any(
            path.name.startswith(f"{new_binding_hash}-")
            for path in (workspace / "runs").iterdir()
        )
        assert any(
            path.name.startswith(f"{new_binding_hash}-")
            for path in (workspace / "provider-operations").iterdir()
        )
        arguments = [
            json.loads(line)
            for line in executable.with_suffix(".arguments")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        assert "resume" not in arguments[0]
        assert "resume" not in arguments[1]
    finally:
        restarted_client.close()
        restarted.close()


@pytest.mark.parametrize("terminal_state", ["executed", "cancelled", "failed"])
def test_runtime_binding_transition_fails_closed_outside_verified_failed_retry(
    tmp_path: Path,
    terminal_state: str,
) -> None:
    old_binding = DeepFetchRuntimeBinding(
        provider_ref="test/deepfetch-binding",
        provider_version="legacy",
        model_ref="test-model",
        harness_ref="test-harness",
        capability_bindings=("web-search-live", "web-fetch-live"),
    )
    new_binding = replace(
        old_binding,
        capability_bindings=(
            "filesystem-danger-full-access",
            "sandbox-policy:danger-full-access",
            "web-search-live",
            "web-fetch-live",
        ),
    )
    old_provider = BoundDeepFetchProvider(
        old_binding,
        outcome=(
            "executed"
            if terminal_state == "executed"
            else "waiting_user"
            if terminal_state == "cancelled"
            else "unverified_failure"
        ),
    )
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / terminal_state),
        proposal_drafter=SnapshotAwareProposalDrafter(),
        deepfetch_provider=old_provider,
        host_compute_probe=DeterministicProbe(),
    )
    client, write_headers = _authenticate(runtime)
    try:
        initialization_id, queued = _open_and_queue_deepfetch(
            client, write_headers, key_prefix=f"unsafe-{terminal_state}"
        )
        request_ref = str(queued["deepfetch"]["request_ref"])
        request = runtime.owners.human_collaboration.query_deepfetch_request(
            request_ref
        )
        assert request is not None
        if terminal_state == "executed":
            runtime.owners.agent_runtime.execute_deepfetch(request, old_provider)
        else:
            with pytest.raises(DeepFetchUnavailable):
                runtime.owners.agent_runtime.execute_deepfetch(request, old_provider)
            if terminal_state == "cancelled":
                runtime.owners.agent_runtime.cancel_deepfetch(request_ref)

        new_provider = BoundDeepFetchProvider(new_binding)
        with pytest.raises(
            OwnerConflict, match="^deepfetch_run_identity_conflict$"
        ):
            runtime.owners.agent_runtime.execute_deepfetch(request, new_provider)

        assert new_provider.requests == []
        persisted = runtime.owners.agent_runtime.query_deepfetch_run(request_ref)
        assert persisted is not None
        assert persisted.runtime_binding_hash == canonical_hash(old_binding.as_dict())
        projected = client.get(
            f"/api/v1/quest-initializations/{initialization_id}"
        ).json()["deepfetch"]["run"]
        assert projected["provider_operation_retry_permitted"] is False
        if terminal_state == "failed":
            assert projected["status"] == "failed"
        with runtime._database.read() as connection:
            attempt_bindings = connection.execute(
                text(
                    "SELECT runtime_binding_hash FROM ar_deepfetch_attempts "
                    "WHERE run_ref = :run_ref ORDER BY generation"
                ),
                {"run_ref": persisted.run_ref},
            ).scalars().all()
        assert attempt_bindings == [canonical_hash(old_binding.as_dict())]
    finally:
        client.close()
        runtime.close()


def test_active_runtime_binding_transition_is_an_identity_conflict(
    tmp_path: Path,
) -> None:
    old_binding = DeepFetchRuntimeBinding(
        provider_ref="test/deepfetch-active-binding",
        provider_version="legacy",
        model_ref="test-model",
        harness_ref="test-harness",
        capability_bindings=("web-search-live", "web-fetch-live"),
    )
    new_binding = replace(
        old_binding,
        capability_bindings=(
            "filesystem-danger-full-access",
            "sandbox-policy:danger-full-access",
            "web-search-live",
            "web-fetch-live",
        ),
    )
    old_provider = BlockingBoundDeepFetchProvider(old_binding)
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "active"),
        proposal_drafter=SnapshotAwareProposalDrafter(),
        deepfetch_provider=old_provider,
        host_compute_probe=DeterministicProbe(),
    )
    client, write_headers = _authenticate(runtime)
    errors: list[BaseException] = []
    try:
        _initialization_id, queued = _open_and_queue_deepfetch(
            client, write_headers, key_prefix="unsafe-active"
        )
        request = runtime.owners.human_collaboration.query_deepfetch_request(
            str(queued["deepfetch"]["request_ref"])
        )
        assert request is not None

        def execute_old_binding() -> None:
            try:
                runtime.owners.agent_runtime.execute_deepfetch(request, old_provider)
            except BaseException as error:  # pragma: no branch - asserted below
                errors.append(error)

        worker = threading.Thread(target=execute_old_binding, daemon=True)
        worker.start()
        assert old_provider.started.wait(timeout=5)
        new_provider = BoundDeepFetchProvider(new_binding)
        with pytest.raises(
            OwnerConflict, match="^deepfetch_run_identity_conflict$"
        ):
            runtime.owners.agent_runtime.execute_deepfetch(request, new_provider)
        assert new_provider.requests == []
    finally:
        old_provider.release.set()
        if "worker" in locals():
            worker.join(timeout=5)
        client.close()
        runtime.close()
    assert errors == []


def test_binding_upgrade_reconciles_ack_lost_success_under_persisted_binding(
    tmp_path: Path,
) -> None:
    old_binding = DeepFetchRuntimeBinding(
        provider_ref="test/deepfetch-ack-loss-binding",
        provider_version="legacy",
        model_ref="test-model",
        harness_ref="test-harness",
        capability_bindings=("web-search-live", "web-fetch-live"),
    )
    new_binding = replace(
        old_binding,
        capability_bindings=(
            "filesystem-danger-full-access",
            "sandbox-policy:danger-full-access",
            "web-search-live",
            "web-fetch-live",
        ),
    )
    data_root = prepare_data_root(tmp_path / "ack-loss-success")
    old_provider = PendingBoundDeepFetchProvider(old_binding)
    runtime = build_production_runtime(
        data_root,
        proposal_drafter=SnapshotAwareProposalDrafter(),
        deepfetch_provider=old_provider,
        host_compute_probe=DeterministicProbe(),
    )
    client, write_headers = _authenticate(runtime)
    initialization_id, queued = _open_and_queue_deepfetch(
        client, write_headers, key_prefix="binding-ack-loss-success"
    )
    request_ref = str(queued["deepfetch"]["request_ref"])
    try:
        assert not runtime.deepfetch.process_once()
        admitted = runtime.owners.agent_runtime.query_deepfetch_run(request_ref)
        assert admitted is not None
        assert admitted.status == "admitted"
        assert admitted.attempt_ref is None
        assert admitted.runtime_binding_hash == canonical_hash(old_binding.as_dict())
    finally:
        client.close()
        runtime.close()

    upgraded_provider = UpgradedReconciliationProvider(
        old_binding,
        new_binding,
        old_outcome="success",
    )
    restarted = build_production_runtime(
        data_root,
        proposal_drafter=SnapshotAwareProposalDrafter(),
        deepfetch_provider=upgraded_provider,
        host_compute_probe=DeterministicProbe(),
    )
    restarted_client, _restarted_headers = _authenticate(restarted)
    try:
        time.sleep(0.6)
        assert restarted.deepfetch.process_once()
        succeeded = restarted_client.get(
            f"/api/v1/quest-initializations/{initialization_id}"
        ).json()
        run = succeeded["deepfetch"]["run"]
        assert succeeded["deepfetch"]["status"] == "succeeded"
        assert run["runtime_binding_hash"] == canonical_hash(old_binding.as_dict())
        assert run["native_session_ref"] == "native-old-binding"
        assert [request.runtime_binding for request in upgraded_provider.requests] == [
            old_binding
        ]
        with restarted._database.read() as connection:
            attempts = connection.execute(
                text(
                    "SELECT status, runtime_binding_hash FROM "
                    "ar_deepfetch_attempts WHERE run_ref = :run_ref "
                    "ORDER BY generation"
                ),
                {"run_ref": run["run_ref"]},
            ).all()
        assert [(row.status, row.runtime_binding_hash) for row in attempts] == [
            ("superseded", canonical_hash(old_binding.as_dict())),
            ("executed", canonical_hash(old_binding.as_dict())),
        ]
        assert not any(
            event.event_type
            == "agent_runtime.deepfetch_runtime_binding_transitioned"
            for event in restarted.feed.read_after(0).events
        )
    finally:
        restarted_client.close()
        restarted.close()


def test_binding_upgrade_waits_for_verified_old_failure_before_transition(
    tmp_path: Path,
) -> None:
    old_binding = DeepFetchRuntimeBinding(
        provider_ref="test/deepfetch-terminal-upgrade-binding",
        provider_version="legacy",
        model_ref="test-model",
        harness_ref="test-harness",
        capability_bindings=("web-search-live", "web-fetch-live"),
    )
    new_binding = replace(
        old_binding,
        capability_bindings=(
            "filesystem-danger-full-access",
            "sandbox-policy:danger-full-access",
            "web-search-live",
            "web-fetch-live",
        ),
    )
    data_root = prepare_data_root(tmp_path / "terminal-upgrade")
    old_provider = PendingBoundDeepFetchProvider(old_binding)
    runtime = build_production_runtime(
        data_root,
        proposal_drafter=SnapshotAwareProposalDrafter(),
        deepfetch_provider=old_provider,
        host_compute_probe=DeterministicProbe(),
    )
    client, write_headers = _authenticate(runtime)
    initialization_id, queued = _open_and_queue_deepfetch(
        client, write_headers, key_prefix="binding-terminal-upgrade"
    )
    request_ref = str(queued["deepfetch"]["request_ref"])
    try:
        assert not runtime.deepfetch.process_once()
    finally:
        client.close()
        runtime.close()

    upgraded_provider = UpgradedReconciliationProvider(
        old_binding,
        new_binding,
        old_outcome="terminal_failure",
    )
    restarted = build_production_runtime(
        data_root,
        proposal_drafter=SnapshotAwareProposalDrafter(),
        deepfetch_provider=upgraded_provider,
        host_compute_probe=DeterministicProbe(),
    )
    restarted_client, restarted_headers = _authenticate(restarted)
    try:
        time.sleep(0.6)
        assert restarted.deepfetch.process_once()
        failed = restarted_client.get(
            f"/api/v1/quest-initializations/{initialization_id}"
        ).json()
        assert failed["deepfetch"]["status"] == "failed"
        assert failed["deepfetch"]["failure"] == {
            "code": "codex_deepfetch_failed"
        }
        failed_run = failed["deepfetch"]["run"]
        assert failed_run["runtime_binding_hash"] == canonical_hash(
            old_binding.as_dict()
        )
        assert [request.runtime_binding for request in upgraded_provider.requests] == [
            old_binding
        ]

        retried = restarted_client.post(
            f"/api/v1/quest-initializations/{initialization_id}"
            "/proposal-generations",
            headers=_write_headers(
                restarted_headers, "binding-terminal-upgrade-retry"
            ),
            json={
                "expected_draft_revision": failed["quest_draft"]["revision"],
                "expected_draft_hash": failed["quest_draft"]["hash"],
            },
        )
        retried.raise_for_status()
        assert restarted.deepfetch.process_once()
        succeeded = restarted_client.get(
            f"/api/v1/quest-initializations/{initialization_id}"
        ).json()
        run = succeeded["deepfetch"]["run"]
        assert succeeded["deepfetch"]["status"] == "succeeded"
        assert run["run_ref"] == failed_run["run_ref"]
        assert run["runtime_binding_hash"] == canonical_hash(new_binding.as_dict())
        assert run["native_session_ref"] == "native-new-binding"
        assert [request.runtime_binding for request in upgraded_provider.requests] == [
            old_binding,
            new_binding,
        ]
        with restarted._database.read() as connection:
            attempts = connection.execute(
                text(
                    "SELECT status, runtime_binding_hash FROM "
                    "ar_deepfetch_attempts WHERE run_ref = :run_ref "
                    "ORDER BY generation"
                ),
                {"run_ref": run["run_ref"]},
            ).all()
        assert [(row.status, row.runtime_binding_hash) for row in attempts] == [
            ("superseded", canonical_hash(old_binding.as_dict())),
            ("failed", canonical_hash(old_binding.as_dict())),
            ("executed", canonical_hash(new_binding.as_dict())),
        ]
        transition = next(
            event
            for event in restarted.feed.read_after(0).events
            if event.event_type
            == "agent_runtime.deepfetch_runtime_binding_transitioned"
        )
        assert transition.payload["previous_attempt_ref"]
        assert transition.payload["old_runtime_binding_hash"] == canonical_hash(
            old_binding.as_dict()
        )
        assert transition.payload["new_runtime_binding_hash"] == canonical_hash(
            new_binding.as_dict()
        )
    finally:
        restarted_client.close()
        restarted.close()


@pytest.mark.parametrize(
    ("root_shape", "tamper_mode"),
    [
        ("unpartitioned", None),
        ("old-binding-prefixed", None),
        ("unpartitioned", "conflicting-run-root"),
        ("unpartitioned", "forged-invocation"),
    ],
)
def test_codex_binding_upgrade_reconciles_signed_ack_loss_without_new_effect(
    tmp_path: Path,
    root_shape: str,
    tamper_mode: str | None,
) -> None:
    executable = tmp_path / f"fake-codex-reconcile-{root_shape}-{tamper_mode}"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import pathlib
import sys

arguments = sys.argv[1:]
counter_path = pathlib.Path(__file__).with_suffix('.count')
count = int(counter_path.read_text()) + 1 if counter_path.exists() else 1
counter_path.write_text(str(count), encoding='utf-8')
prompt = sys.stdin.read()
thread_ref = 'native-signed-old-binding'
print(json.dumps({'type': 'thread.started', 'thread_id': thread_ref}), flush=True)
print(json.dumps({'type': 'item.completed', 'item': {
    'id': 'search-old-binding', 'type': 'web_search', 'query': 'paper',
    'action': {'type': 'search'}}}), flush=True)
print(json.dumps({'type': 'item.completed', 'item': {
    'id': 'open-old-binding', 'type': 'web_search', 'query': '',
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
result_path.write_text(json.dumps(__FINAL__), encoding='utf-8')
""".replace("__LEDGER__", repr(PROTOTYPE_EMPTY_LEDGER)).replace(
            "__FINAL__", repr(PROTOTYPE_EMPTY_FINAL)
        ),
        encoding="utf-8",
    )
    executable.chmod(0o700)
    data_root = prepare_data_root(tmp_path / f"data-{root_shape}")
    workspace = data_root.run / "deepfetch-provider"
    adapter_type = (
        UnpartitionedAckLossCodexDeepFetchAdapter
        if root_shape == "unpartitioned"
        else AckLossLegacyCodexDeepFetchAdapter
    )
    old_adapter = adapter_type(
        workspace,
        executable=str(executable),
        model_ref="gpt-test",
        timeout_seconds=10,
    )
    old_binding = old_adapter.runtime_binding()
    old_binding_hash = canonical_hash(old_binding.as_dict())
    runtime = build_production_runtime(
        data_root,
        proposal_drafter=SnapshotAwareProposalDrafter(),
        deepfetch_provider=old_adapter,
        host_compute_probe=DeterministicProbe(),
    )
    client, write_headers = _authenticate(runtime)
    initialization_id, queued = _open_and_queue_deepfetch(
        client, write_headers, key_prefix=f"signed-ack-loss-{root_shape}"
    )
    request_ref = str(queued["deepfetch"]["request_ref"])
    try:
        assert not runtime.deepfetch.process_once()
        admitted = runtime.owners.agent_runtime.query_deepfetch_run(request_ref)
        assert admitted is not None
        assert admitted.status == "admitted"
        assert admitted.attempt_ref is None
        assert admitted.runtime_binding_hash == old_binding_hash
        assert executable.with_suffix(".count").read_text(encoding="utf-8") == "1"
    finally:
        client.close()
        runtime.close()

    if tamper_mode == "conflicting-run-root":
        legacy_run_root = next(
            path for path in (workspace / "runs").iterdir() if path.is_dir()
        )
        (workspace / "runs" / f"{old_binding_hash}-{legacy_run_root.name}").mkdir()
    elif tamper_mode == "forged-invocation":
        invocation_path = next(
            (workspace / "provider-operations").rglob("invocation.json")
        )
        invocation = json.loads(invocation_path.read_text(encoding="utf-8"))
        invocation["seal"] = "0" * 64
        invocation_path.write_text(
            json.dumps(
                invocation,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    upgraded = CodexDeepFetchAdapter(
        workspace,
        executable=str(executable),
        model_ref="gpt-test",
        timeout_seconds=10,
    )
    assert canonical_hash(upgraded.runtime_binding().as_dict()) != old_binding_hash
    restarted = build_production_runtime(
        data_root,
        proposal_drafter=SnapshotAwareProposalDrafter(),
        deepfetch_provider=upgraded,
        host_compute_probe=DeterministicProbe(),
    )
    restarted_client, _restarted_headers = _authenticate(restarted)
    try:
        time.sleep(0.6)
        if tamper_mode is not None:
            assert not restarted.deepfetch.process_once()
            held = restarted_client.get(
                f"/api/v1/quest-initializations/{initialization_id}"
            ).json()
            assert held["deepfetch"]["status"] == "queued"
            assert held["deepfetch"]["failure"] is None
            assert held["deepfetch"]["run"]["runtime_binding_hash"] == (
                old_binding_hash
            )
            assert executable.with_suffix(".count").read_text(
                encoding="utf-8"
            ) == "1"
            return
        assert restarted.deepfetch.process_once()
        succeeded = restarted_client.get(
            f"/api/v1/quest-initializations/{initialization_id}"
        ).json()
        run = succeeded["deepfetch"]["run"]
        assert succeeded["deepfetch"]["status"] == "succeeded"
        assert run["runtime_binding_hash"] == old_binding_hash
        assert run["native_session_ref"] == "native-signed-old-binding"
        assert run["attempt_generation"] == 2
        # Reconciliation read the sealed predecessor spool; the upgraded
        # adapter never launched a second Codex argv under the old binding.
        assert executable.with_suffix(".count").read_text(encoding="utf-8") == "1"
        with restarted._database.read() as connection:
            attempts = connection.execute(
                text(
                    "SELECT status, runtime_binding_hash FROM "
                    "ar_deepfetch_attempts WHERE run_ref = :run_ref "
                    "ORDER BY generation"
                ),
                {"run_ref": run["run_ref"]},
            ).all()
        assert [(row.status, row.runtime_binding_hash) for row in attempts] == [
            ("superseded", old_binding_hash),
            ("executed", old_binding_hash),
        ]
    finally:
        restarted_client.close()
        restarted.close()


def test_codex_binding_upgrade_requires_signed_old_failure_before_new_effect(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "fake-codex-terminal-reconciliation"
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
thread_ref = 'native-signed-old-failure' if count == 1 else 'native-new-effect'
print(json.dumps({'type': 'thread.started', 'thread_id': thread_ref}), flush=True)
if count == 1:
    raise SystemExit(7)
print(json.dumps({'type': 'item.completed', 'item': {
    'id': 'search-new-binding', 'type': 'web_search', 'query': 'paper',
    'action': {'type': 'search'}}}), flush=True)
print(json.dumps({'type': 'item.completed', 'item': {
    'id': 'open-new-binding', 'type': 'web_search', 'query': '',
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
result_path.write_text(json.dumps(__FINAL__), encoding='utf-8')
""".replace("__LEDGER__", repr(PROTOTYPE_EMPTY_LEDGER)).replace(
            "__FINAL__", repr(PROTOTYPE_EMPTY_FINAL)
        ),
        encoding="utf-8",
    )
    executable.chmod(0o700)
    data_root = prepare_data_root(tmp_path / "terminal-reconciliation")
    workspace = data_root.run / "deepfetch-provider"
    old_adapter = UnpartitionedAckLossCodexDeepFetchAdapter(
        workspace,
        executable=str(executable),
        model_ref="gpt-test",
        timeout_seconds=10,
    )
    old_binding_hash = canonical_hash(old_adapter.runtime_binding().as_dict())
    runtime = build_production_runtime(
        data_root,
        proposal_drafter=SnapshotAwareProposalDrafter(),
        deepfetch_provider=old_adapter,
        host_compute_probe=DeterministicProbe(),
    )
    client, write_headers = _authenticate(runtime)
    initialization_id, queued = _open_and_queue_deepfetch(
        client, write_headers, key_prefix="signed-terminal-upgrade"
    )
    request_ref = str(queued["deepfetch"]["request_ref"])
    try:
        assert not runtime.deepfetch.process_once()
        admitted = runtime.owners.agent_runtime.query_deepfetch_run(request_ref)
        assert admitted is not None
        assert admitted.status == "admitted"
        assert admitted.runtime_binding_hash == old_binding_hash
    finally:
        client.close()
        runtime.close()

    upgraded = CodexDeepFetchAdapter(
        workspace,
        executable=str(executable),
        model_ref="gpt-test",
        timeout_seconds=10,
    )
    new_binding_hash = canonical_hash(upgraded.runtime_binding().as_dict())
    restarted = build_production_runtime(
        data_root,
        proposal_drafter=SnapshotAwareProposalDrafter(),
        deepfetch_provider=upgraded,
        host_compute_probe=DeterministicProbe(),
    )
    restarted_client, restarted_headers = _authenticate(restarted)
    try:
        time.sleep(0.6)
        assert restarted.deepfetch.process_once()
        failed = restarted_client.get(
            f"/api/v1/quest-initializations/{initialization_id}"
        ).json()
        assert failed["deepfetch"]["status"] == "failed"
        assert failed["deepfetch"]["failure"] == {
            "code": "codex_deepfetch_failed"
        }
        assert failed["deepfetch"]["run"]["runtime_binding_hash"] == (
            old_binding_hash
        )
        assert executable.with_suffix(".count").read_text(encoding="utf-8") == "1"

        retried = restarted_client.post(
            f"/api/v1/quest-initializations/{initialization_id}"
            "/proposal-generations",
            headers=_write_headers(
                restarted_headers, "signed-terminal-upgrade-retry"
            ),
            json={
                "expected_draft_revision": failed["quest_draft"]["revision"],
                "expected_draft_hash": failed["quest_draft"]["hash"],
            },
        )
        retried.raise_for_status()
        assert restarted.deepfetch.process_once()
        succeeded = restarted_client.get(
            f"/api/v1/quest-initializations/{initialization_id}"
        ).json()
        run = succeeded["deepfetch"]["run"]
        assert succeeded["deepfetch"]["status"] == "succeeded"
        assert run["runtime_binding_hash"] == new_binding_hash
        assert run["native_session_ref"] == "native-new-effect"
        assert executable.with_suffix(".count").read_text(encoding="utf-8") == "2"
        arguments = [
            json.loads(line)
            for line in executable.with_suffix(".arguments")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        assert "resume" not in arguments[0]
        assert "resume" not in arguments[1]
        with restarted._database.read() as connection:
            attempts = connection.execute(
                text(
                    "SELECT status, runtime_binding_hash FROM "
                    "ar_deepfetch_attempts WHERE run_ref = :run_ref "
                    "ORDER BY generation"
                ),
                {"run_ref": run["run_ref"]},
            ).all()
        assert [(row.status, row.runtime_binding_hash) for row in attempts] == [
            ("superseded", old_binding_hash),
            ("failed", old_binding_hash),
            ("executed", new_binding_hash),
        ]
    finally:
        restarted_client.close()
        restarted.close()


def test_codex_binding_upgrade_keeps_unfinished_old_operation_pending(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "unfinished-old-operation")
    workspace = data_root.run / "deepfetch-provider"
    old_runner = PendingDurableRunner()
    old_adapter = UnpartitionedAckLossCodexDeepFetchAdapter(
        workspace,
        executable="/does/not/run",
        model_ref="gpt-test",
        timeout_seconds=10,
        process_runner=old_runner,
    )
    old_binding_hash = canonical_hash(old_adapter.runtime_binding().as_dict())
    runtime = build_production_runtime(
        data_root,
        proposal_drafter=SnapshotAwareProposalDrafter(),
        deepfetch_provider=old_adapter,
        host_compute_probe=DeterministicProbe(),
    )
    client, write_headers = _authenticate(runtime)
    initialization_id, queued = _open_and_queue_deepfetch(
        client, write_headers, key_prefix="unfinished-old-operation"
    )
    request_ref = str(queued["deepfetch"]["request_ref"])
    try:
        assert not runtime.deepfetch.process_once()
        assert old_runner.calls == 1
    finally:
        client.close()
        runtime.close()

    # Keep the exact sealed request inside its startup uncertainty window.  No
    # signed exit or verified never-started proof exists yet.
    supervisor_request = next(
        (workspace / "provider-operations").rglob("supervisor-request.json")
    )
    supervisor_request.touch()
    upgraded_runner = PendingDurableRunner()
    upgraded = CodexDeepFetchAdapter(
        workspace,
        executable="/does/not/run",
        model_ref="gpt-test",
        timeout_seconds=10,
        process_runner=upgraded_runner,
    )
    restarted = build_production_runtime(
        data_root,
        proposal_drafter=SnapshotAwareProposalDrafter(),
        deepfetch_provider=upgraded,
        host_compute_probe=DeterministicProbe(),
    )
    restarted_client, _restarted_headers = _authenticate(restarted)
    try:
        time.sleep(0.6)
        supervisor_request.touch()
        assert not restarted.deepfetch.process_once()
        held = restarted_client.get(
            f"/api/v1/quest-initializations/{initialization_id}"
        ).json()
        assert held["deepfetch"]["status"] == "queued"
        assert held["deepfetch"]["failure"] is None
        assert held["deepfetch"]["run"]["runtime_binding_hash"] == (
            old_binding_hash
        )
        assert held["deepfetch"]["run"]["attempt_generation"] == 2
        assert upgraded_runner.calls == 0
        persisted = restarted.owners.agent_runtime.query_deepfetch_run(
            request_ref
        )
        assert persisted is not None
        assert persisted.status == "admitted"
        assert persisted.attempt_ref is None
    finally:
        restarted_client.close()
        restarted.close()


def test_codex_binding_upgrade_retires_verified_predispatch_boundary(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "predispatch-boundary")
    workspace = data_root.run / "deepfetch-provider"
    old_adapter = PreDispatchPendingCodexDeepFetchAdapter(
        workspace,
        executable="/must-not-run",
        model_ref="gpt-test",
        timeout_seconds=10,
    )
    old_binding_hash = canonical_hash(old_adapter.runtime_binding().as_dict())
    runtime = build_production_runtime(
        data_root,
        proposal_drafter=SnapshotAwareProposalDrafter(),
        deepfetch_provider=old_adapter,
        host_compute_probe=DeterministicProbe(),
    )
    client, write_headers = _authenticate(runtime)
    initialization_id, queued = _open_and_queue_deepfetch(
        client, write_headers, key_prefix="predispatch-boundary"
    )
    request_ref = str(queued["deepfetch"]["request_ref"])
    try:
        assert not runtime.deepfetch.process_once()
        assert not (workspace / "provider-operations").exists()
    finally:
        client.close()
        runtime.close()

    upgraded = CodexDeepFetchAdapter(
        workspace,
        executable="/must-not-run",
        model_ref="gpt-test",
        timeout_seconds=10,
    )
    restarted = build_production_runtime(
        data_root,
        proposal_drafter=SnapshotAwareProposalDrafter(),
        deepfetch_provider=upgraded,
        host_compute_probe=DeterministicProbe(),
    )
    restarted_client, _restarted_headers = _authenticate(restarted)
    try:
        time.sleep(0.6)
        assert restarted.deepfetch.process_once()
        failed = restarted_client.get(
            f"/api/v1/quest-initializations/{initialization_id}"
        ).json()
        assert failed["deepfetch"]["status"] == "failed"
        assert failed["deepfetch"]["failure"] == {
            "code": "deepfetch_provider_never_started"
        }
        assert failed["deepfetch"]["run"]["runtime_binding_hash"] == (
            old_binding_hash
        )
        with restarted._database.read() as connection:
            retry_permitted = connection.execute(
                text(
                    "SELECT provider_operation_retry_permitted FROM "
                    "ar_deepfetch_runs WHERE request_ref = :request_ref"
                ),
                {"request_ref": request_ref},
            ).scalar_one()
        assert bool(retry_permitted)
        assert not (workspace / "provider-operations").exists()
    finally:
        restarted_client.close()
        restarted.close()


def test_codex_binding_upgrade_retires_signed_acquisition_turn_boundary(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "fake-codex-acquisition-boundary"
    acquisition_reply = {
        "action": "acquire",
        "acquisition_request": {
            "request_id": "binding-upgrade-acquisition-1",
            "route_policy": "oa_first_then_institution",
            "papers": [
                {
                    "paper_id": "doi:10.1000/binding-upgrade",
                    "title": "Binding upgrade boundary",
                    "doi": "10.1000/binding-upgrade",
                    "arxiv_id": None,
                    "source_urls": ["https://example.org/binding-upgrade"],
                }
            ],
        },
        "completion": None,
        "limitations": [],
        "workflow": {
            "prototype_commit": PROTOTYPE_COMMIT,
            "main_agent_status": "running",
            "reader_assignments": [],
            "finalize_status": "pending",
            "finalized_at": None,
        },
    }
    executable.write_text(
        """#!/usr/bin/env python3
import json
import pathlib
import sys

arguments = sys.argv[1:]
counter_path = pathlib.Path(__file__).with_suffix('.count')
count = int(counter_path.read_text()) + 1 if counter_path.exists() else 1
counter_path.write_text(str(count), encoding='utf-8')
sys.stdin.read()
print(json.dumps({'type': 'thread.started', 'thread_id':
                  'native-acquisition-boundary'}), flush=True)
print(json.dumps({'type': 'item.completed', 'item': {
    'id': 'search-boundary', 'type': 'web_search', 'query': 'paper',
    'action': {'type': 'search'}}}), flush=True)
print(json.dumps({'type': 'item.completed', 'item': {
    'id': 'open-boundary', 'type': 'web_search', 'query': '',
    'action': {'type': 'other'}}}), flush=True)
result_path = pathlib.Path(arguments[arguments.index('--output-last-message') + 1])
result_path.write_text(json.dumps(__ACQUIRE__), encoding='utf-8')
""".replace("__ACQUIRE__", repr(acquisition_reply)),
        encoding="utf-8",
    )
    executable.chmod(0o700)
    data_root = prepare_data_root(tmp_path / "acquisition-boundary")
    workspace = data_root.run / "deepfetch-provider"
    old_adapter = UnpartitionedAckLossCodexDeepFetchAdapter(
        workspace,
        executable=str(executable),
        model_ref="gpt-test",
        timeout_seconds=10,
    )
    old_binding_hash = canonical_hash(old_adapter.runtime_binding().as_dict())
    runtime = build_production_runtime(
        data_root,
        proposal_drafter=SnapshotAwareProposalDrafter(),
        deepfetch_provider=old_adapter,
        host_compute_probe=DeterministicProbe(),
    )
    client, write_headers = _authenticate(runtime)
    initialization_id, queued = _open_and_queue_deepfetch(
        client, write_headers, key_prefix="acquisition-turn-boundary"
    )
    request_ref = str(queued["deepfetch"]["request_ref"])
    try:
        assert not runtime.deepfetch.process_once()
        assert executable.with_suffix(".count").read_text(encoding="utf-8") == "1"
    finally:
        client.close()
        runtime.close()

    upgraded = CodexDeepFetchAdapter(
        workspace,
        executable=str(executable),
        model_ref="gpt-test",
        timeout_seconds=10,
    )
    restarted = build_production_runtime(
        data_root,
        proposal_drafter=SnapshotAwareProposalDrafter(),
        deepfetch_provider=upgraded,
        host_compute_probe=DeterministicProbe(),
    )
    restarted_client, _restarted_headers = _authenticate(restarted)
    try:
        time.sleep(0.6)
        assert restarted.deepfetch.process_once()
        failed = restarted_client.get(
            f"/api/v1/quest-initializations/{initialization_id}"
        ).json()
        assert failed["deepfetch"]["status"] == "failed"
        assert failed["deepfetch"]["failure"] == {
            "code": "deepfetch_runtime_binding_transition_required"
        }
        assert failed["deepfetch"]["run"]["runtime_binding_hash"] == (
            old_binding_hash
        )
        assert executable.with_suffix(".count").read_text(encoding="utf-8") == "1"
        with restarted._database.read() as connection:
            retry_permitted = connection.execute(
                text(
                    "SELECT provider_operation_retry_permitted FROM "
                    "ar_deepfetch_runs WHERE request_ref = :request_ref"
                ),
                {"request_ref": request_ref},
            ).scalar_one()
        assert bool(retry_permitted)
    finally:
        restarted_client.close()
        restarted.close()


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
        with runtime._database.read() as connection:
            first_unit = connection.execute(
                text(
                    "SELECT status FROM ar_provider_units WHERE operation_ref = "
                    ":operation_ref"
                ),
                {"operation_ref": before.provider_operation_ref},
            ).one()
        assert first_unit.status == "revocation_pending"
        assert runtime.query_runtime_observability()["inhibitor"]["active_count"] == 1
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
        with restarted._database.read() as connection:
            restarted_feed_count = connection.execute(
                text("SELECT count(*) FROM durable_feed")
            ).scalar_one()
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
        # RuntimeProtection records the daemon-incarnation interruption while
        # constructing the restarted runtime.  The backoff poll itself remains
        # read-only and does not add a second attempt or feed event.
        assert restarted_feed_count >= feed_count
        assert waiting_feed_count == restarted_feed_count
        assert len(provider.requests) == 1

        time.sleep(0.6)
        assert restarted.deepfetch.process_once()
        completed = restarted.owners.human_collaboration.query_quest_creation(
            initialization_id
        )
        assert completed["deepfetch"]["status"] == "succeeded"
        assert len(provider.requests) == 2
        assert provider.requests[1].job_ref == provider.requests[0].job_ref
        with restarted._database.read() as connection:
            provider_units = connection.execute(
                text(
                    "SELECT status FROM ar_provider_units WHERE operation_ref = "
                    ":operation_ref ORDER BY started_at, unit_ref"
                ),
                {"operation_ref": before.provider_operation_ref},
            ).all()
        assert [unit.status for unit in provider_units] == ["revoked", "completed"]
        observability = restarted.query_runtime_observability()
        assert observability["inhibitor"]["active_count"] == 0
        assert observability["responsibilities"] == []
    finally:
        restarted.close()


def test_deepfetch_power_wait_is_retryable_without_restarting_daemon(
    tmp_path: Path,
) -> None:
    inhibitor = SwitchablePowerInhibitor()
    provider = DeterministicDeepFetchProvider()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "data"),
        proposal_drafter=SnapshotAwareProposalDrafter(),
        deepfetch_provider=provider,
        host_compute_probe=DeterministicProbe(),
        power_inhibitor=inhibitor,
    )
    client, write_headers = _authenticate(runtime)
    initialization_id, queued = _open_and_queue_deepfetch(
        client, write_headers, key_prefix="deepfetch-power-wait"
    )
    request_ref = queued["deepfetch"]["request_ref"]
    try:
        inhibitor.fail = True
        assert not runtime.deepfetch.process_once()
        assert provider.requests == []
        waiting = runtime.owners.agent_runtime.query_deepfetch_run(request_ref)
        assert waiting is not None
        assert waiting.status == "admitted"

        inhibitor.fail = False
        assert runtime.deepfetch.process_once()
        completed = runtime.owners.human_collaboration.query_quest_creation(
            initialization_id
        )
        assert completed["deepfetch"]["status"] == "succeeded"
        assert len(provider.requests) == 1
        evidence = runtime.query_runtime_observability()
        assert evidence["inhibitor"]["active_count"] == 0
        assert evidence["responsibilities"] == []
    finally:
        client.close()
        runtime.close()


def test_reconciliation_without_a_receipt_remains_durable_beyond_legacy_ceiling(
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

        assert not runtime.deepfetch.process_once()
        pending = client.get(
            f"/api/v1/quest-initializations/{initialization_id}"
        ).json()
        assert pending["deepfetch"]["status"] == "queued"
        assert pending["deepfetch"]["failure"] is None
        with runtime._database.read() as connection:
            run = connection.execute(
                text(
                    "SELECT status, provider_operation_ref, "
                    "provider_operation_retry_permitted, attempt_generation, "
                    "reconciliation_attempt_count, next_reconcile_at "
                    "FROM ar_deepfetch_runs WHERE request_ref = :request_ref"
                ),
                {"request_ref": request_ref},
            ).one()
            control_status = connection.execute(
                text(
                    "SELECT status FROM ar_run_controls WHERE run_ref = "
                    "(SELECT run_ref FROM ar_deepfetch_runs WHERE request_ref = "
                    ":request_ref)"
                ),
                {"request_ref": request_ref},
            ).scalar_one()
        assert run.status == "admitted"
        assert int(run.provider_operation_retry_permitted) == 0
        assert int(run.attempt_generation) == 2
        assert int(run.reconciliation_attempt_count) == 40
        assert run.next_reconcile_at is not None
        assert control_status == "running"
        assert len(provider.requests) == 2
        assert provider.requests[1].job_ref == provider.requests[0].job_ref
        evidence = runtime.query_runtime_observability()
        assert evidence["inhibitor"]["active_count"] == 2
        assert len(evidence["responsibilities"]) == 2
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
        assert first_run["provider_operation_retry_permitted"] is False

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
        assert (
            during_successor["deepfetch"]["run"][
                "provider_operation_retry_permitted"
            ]
            is False
        )

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
        assert recovered_run["provider_operation_retry_permitted"] is False
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
