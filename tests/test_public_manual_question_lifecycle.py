"""Red public-contract tests for the follow-up ManualCreation lifecycle.

The agreed seam is ``ProductionRuntime.owners.human_collaboration`` only.  These
tests intentionally do not query owner tables or call Research Memory/Research
Graph implementations directly.  They specify the following public commands:

* ``record_manual_deepfetch_waiver(context_ref, expected_seed_ref=...,
  expected_seed_hash=..., idempotency_key=...)`` records a distinct explicit
  waiver; a Seed's ``deepfetch_preference`` is never itself a waiver.
* ``save_manual_question_proposal(context_ref, content=...,
  expected_basis_hash=..., idempotency_key=...)`` freezes one exact six-field
  Proposal at the HC boundary.
* ``confirm_manual_question_proposal(context_ref, proposal_ref=...,
  proposal_hash=..., idempotency_key=...)`` records the user's exact Proposal
  confirmation without claiming RM or RG acceptance.
* ``cancel_manual_question_creation(context_ref, idempotency_key)`` is the only
  terminal close operation.  Closing a Web window has no HC command; callers
  observe that distinction by querying the same durable context again.

All command results are observed again through
``query_manual_question_creation``.  ``reconcile_once`` advances at most the
first missing Owner boundary so RM content acceptance remains observable before
RG identity/parent/binding acceptance.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from meta_research.acquisition import (
    AcquisitionPreflightResult,
    AcquisitionRuntimeBinding,
)
from meta_research.composition import build_production_runtime
from meta_research.deepfetch import (
    DeepFetchProviderRequest,
    DeepFetchResult,
    DeepFetchRuntimeBinding,
    DeepFetchUnavailable,
)
from meta_research.manual_creation import _manual_drafting_runtime_effect
from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.paths import prepare_data_root
from meta_research.quest_drafting import (
    DraftingUnavailable,
    HostComputeDevice,
    HostComputeSnapshot,
    IntentTurnRequest,
    IntentTurnResult,
    ProposalDraftRequest,
    ProposalDraftResult,
)
from meta_research.runtime_protection import (
    InhibitorLease,
    RuntimeProtectionUnavailable,
)
from meta_research.semantic_mcp import ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS
from meta_research.web import create_app


QUESTION = {
    "title": "低照度显微图像中的稀有形态保真",
    "unknown_statement": "尚不明确哪种自监督去噪条件能保留稀有形态。",
    "answer_shape": "形成带反例和证据边界的比较结论。",
    "applicability_scope": "低照度荧光显微公开数据。",
    "background_context": "研究稀有细胞形态。",
    "requirements_constraints": "两周内，使用获准 GPU。",
}


class DeterministicDraftingAdapter:
    def __init__(self) -> None:
        self.intent_requests: list[IntentTurnRequest] = []

    def draft(self, request: ProposalDraftRequest) -> ProposalDraftResult:
        return ProposalDraftResult(dict(QUESTION), "test_deterministic")

    def reply(self, request: IntentTurnRequest) -> IntentTurnResult:
        self.intent_requests.append(request)
        return IntentTurnResult(
            "先明确真正未知和答案边界。",
            request.native_session_ref or "manual-lifecycle-drafting-session",
            "test_deterministic",
        )

    def cancel_job(self, job_ref: str) -> bool:
        del job_ref
        return True

    def finish_job(self, job_ref: str) -> None:
        del job_ref


class RuntimeAwareDraftingAdapter(DeterministicDraftingAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.reply_failure: str | None = None
        self.cancelled_job_refs: list[str] = []
        self.finished_job_refs: list[str] = []
        self.finish_active_operations: list[tuple[str, ...]] = []
        self.cancel_acknowledged = True
        self.runtime = None

    def reply(self, request: IntentTurnRequest) -> IntentTurnResult:
        self.intent_requests.append(request)
        if self.reply_failure is not None:
            code = self.reply_failure
            if code == "codex_cli_stopped":
                self.reply_failure = None
            raise DraftingUnavailable(code)
        return IntentTurnResult(
            "先明确真正未知和答案边界。",
            request.native_session_ref or "manual-lifecycle-drafting-session",
            "test_deterministic",
        )

    def cancel_job(self, job_ref: str) -> bool:
        self.cancelled_job_refs.append(job_ref)
        return self.cancel_acknowledged

    def finish_job(self, job_ref: str) -> None:
        self.finished_job_refs.append(job_ref)
        if self.runtime is None:
            return
        active = tuple(
            str(item["operation_ref"])
            for item in self.runtime.query_runtime_observability()[
                "responsibilities"
            ]
            if item["operation_ref"] == job_ref
        )
        self.finish_active_operations.append(active)


class SwitchablePowerInhibitor:
    kind = "test_switchable"

    def __init__(self) -> None:
        self.available = True
        self._active: set[str] = set()

    def acquire(self, *, holder_ref: str, reason: str) -> InhibitorLease:
        del reason
        if not self.available:
            raise RuntimeProtectionUnavailable("power_inhibitor_test_unavailable")
        self._active.add(holder_ref)
        return InhibitorLease(
            holder_ref=holder_ref,
            backend=self.kind,
            scope="sleep",
            acquired_at=time.time(),
            native_holder_ref=f"test-native:{holder_ref}",
        )

    def is_confirmed(self, lease: InhibitorLease) -> bool:
        return lease.holder_ref in self._active

    def release(self, lease: InhibitorLease) -> None:
        self._active.discard(lease.holder_ref)


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


def _command_headers(base: dict[str, str], key: str) -> dict[str, str]:
    return {**base, "Idempotency-Key": key}


class DeterministicProbe:
    def observe(self) -> HostComputeSnapshot:
        return HostComputeSnapshot(
            status="ready",
            observed_at=1_720_000_000.0,
            devices=(
                HostComputeDevice(
                    uuid="GPU-manual-lifecycle",
                    name="Manual Lifecycle GPU",
                    memory_total_mib=81_920,
                ),
            ),
            adapter_kind="test_probe",
        )


class DeterministicManualDeepFetchProvider:
    def __init__(self) -> None:
        self.requests: list[DeepFetchProviderRequest] = []

    def runtime_binding(self) -> DeepFetchRuntimeBinding:
        return DeepFetchRuntimeBinding(
            provider_ref="test/manual-deepfetch-provider",
            provider_version="1",
            model_ref="test-model",
            harness_ref="test-harness",
            capability_bindings=("web-search-live", "web-fetch-live"),
        )

    def execute(self, request: DeepFetchProviderRequest) -> DeepFetchResult:
        self.requests.append(request)
        return DeepFetchResult(
            completion="honest_empty",
            summary="实时检索完成，但没有找到满足当前纳入边界的精确论文。",
            papers=(),
            fulltexts=(),
            limitations=("检索未形成可纳入的精确论文。",),
            native_session_ref="native-manual-deepfetch-session",
            adapter_kind="test_manual_deepfetch",
            web_evidence={
                "schema_ref": "meta-research/deepfetch-web-evidence/v1",
                "search_event_count": 1,
                "fetch_event_count": 1,
                "trace_hash": "8" * 64,
            },
        )


class HumanRequestManualDeepFetchProvider(DeterministicManualDeepFetchProvider):
    def __init__(self, *, invalid_first_result: bool = False) -> None:
        super().__init__()
        self.owner = None
        self.human_request: dict[str, object] | None = None
        self.invalid_first_result = invalid_first_result

    def execute(self, request: DeepFetchProviderRequest) -> DeepFetchResult:
        if self.human_request is None:
            assert self.owner is not None
            quest_ref = request.scope["quest_ref"]
            binding = {
                "quest_ref": quest_ref,
                "task_ref": request.run_ref,
                "root_session_ref": request.root_session_ref,
                "operation_id": ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[0],
                "attempt_ref": request.attempt_ref,
                "generation": request.attempt_generation,
                "request_owner": "agent_runtime",
                "root_kind": "deepfetch",
                "phase": "turn-1",
                "fence_ref": request.fence_ref,
                "runtime_binding_hash": canonical_hash(
                    request.runtime_binding.as_dict()
                ),
            }
            target = {
                "schema_ref": "meta-research/root-agent-human-request-target/v1",
                "root": {
                    "run_kind": "deepfetch",
                    "run_ref": request.run_ref,
                    "attempt_ref": request.attempt_ref,
                    "root_session_ref": request.root_session_ref,
                    "fence_ref": request.fence_ref,
                    "waiter_generation": request.attempt_generation,
                },
                "condition": {
                    "operator_choice": "continue_without_optional_input"
                },
            }
            self.human_request = self.owner.open_human_request_effect(
                effect_key="mcp-effect:manual-deepfetch-yield",
                effect_id="manual-deepfetch-yield",
                operation_binding=binding,
                predecessor_request_ref=None,
                request_kind="offline_action",
                obligation="Choose whether this exact DeepFetch should continue.",
                business_purpose="Resume only this exact Quest-bound DeepFetch.",
                target_assertion=target,
                acceptance_conditions=(
                    "The operator records an exact disposition.",
                ),
                direct_waiter={
                    "waiter_ref": f"root_run:{request.run_ref}",
                    "generation": request.attempt_generation,
                    "target_assertion": target,
                    "wait_scope": "local",
                    "other_blockers": [],
                },
                quest_ref=quest_ref,
            )
        result = super().execute(request)
        if self.invalid_first_result and len(self.requests) == 1:
            return DeepFetchResult(
                completion=result.completion,
                summary="",
                papers=result.papers,
                fulltexts=result.fulltexts,
                limitations=result.limitations,
                native_session_ref=result.native_session_ref,
                adapter_kind=result.adapter_kind,
                web_evidence=result.web_evidence,
                papers_ledger=result.papers_ledger,
            )
        return result


class DeterministicAcquisitionProvider:
    def runtime_binding(self) -> AcquisitionRuntimeBinding:
        return AcquisitionRuntimeBinding(
            provider_ref="test/manual-acquisition",
            provider_version="1",
            capability_bindings=(
                "browser-context-reuse",
                "lawful-fulltext-routing",
                "private-manifest",
            ),
        )

    def preflight(self, _request) -> AcquisitionPreflightResult:
        return AcquisitionPreflightResult(
            status="ready",
            browser_context_ref="browser-context-manual-test",
            reason_code=None,
            evidence={"route": "test-ready"},
        )

    def acquire(self, _request):
        raise AssertionError("honest-empty DeepFetch must not request acquisition")


class FailOnceManualDeepFetchProvider(DeterministicManualDeepFetchProvider):
    def execute(self, request: DeepFetchProviderRequest) -> DeepFetchResult:
        if not self.requests:
            self.requests.append(request)
            raise DeepFetchUnavailable("web_search_temporarily_unavailable")
        return super().execute(request)


class ArtifactDriftOnceManualDeepFetchProvider(
    DeterministicManualDeepFetchProvider
):
    @property
    def requires_verified_terminal_retry(self) -> bool:
        return True

    def execute(self, request: DeepFetchProviderRequest) -> DeepFetchResult:
        if not self.requests:
            self.requests.append(request)
            raise DeepFetchUnavailable(
                "deepfetch_acquisition_artifact_drift",
                durable_outcome="pending",
                native_session_ref="native-manual-artifact-drift",
            )
        return super().execute(request)


def _build_runtime(
    data_root: Path,
    *,
    deepfetch_provider=None,
    acquisition_provider=None,
    drafting=None,
    power_inhibitor=None,
):
    drafting = drafting or DeterministicDraftingAdapter()
    return build_production_runtime(
        prepare_data_root(data_root),
        proposal_drafter=drafting,
        intent_drafting_provider=drafting,
        host_compute_probe=DeterministicProbe(),
        deepfetch_provider=deepfetch_provider,
        acquisition_provider=acquisition_provider,
        power_inhibitor=power_inhibitor,
    )


def _accept_root_question(runtime, key_prefix: str) -> tuple[str, str]:
    human = runtime.owners.human_collaboration
    opened = human.create_quest({}, f"{key_prefix}-quest-open")
    probed = human.observe_host_compute(
        opened["initialization_id"],
        ["GPU-manual-lifecycle"],
        f"{key_prefix}-compute",
    )
    draft = dict(probed["quest_draft"]["value"])
    draft.update(
        {
            "goal": "判断低照度显微图像去噪能否保留稀有形态。",
            "completion_criteria": "形成带反例和证据边界的比较结论。",
            "time_budget": "30d",
            "route": "direct",
            "literature": {
                "mode": "oa_only",
                "library_entry_url": "",
                "scope_exclusions": "",
                "accepted_material_bindings": [],
            },
            "background_and_initial_direction": "比较自监督和监督基线。",
        }
    )
    saved = human.revise_quest_draft(
        opened["initialization_id"],
        draft,
        probed["quest_draft"]["hash"],
        f"{key_prefix}-quest-draft",
        probed["quest_draft"]["revision"],
    )
    human.generate_question_proposal(
        saved["initialization_id"],
        saved["quest_draft"]["hash"],
        f"{key_prefix}-root-proposal",
        saved["quest_draft"]["revision"],
    )
    assert human.process_drafting_once()
    proposed = human.query_quest_creation(saved["initialization_id"])
    previewed = human.preview_confirmation(
        proposed["initialization_id"],
        quest_draft_revision=proposed["quest_draft"]["revision"],
        quest_draft_hash=proposed["quest_draft"]["hash"],
        proposal_ref=proposed["proposal"]["ref"],
        proposal_hash=proposed["proposal"]["hash"],
        idempotency_key=f"{key_prefix}-root-preview",
    )
    human.confirm_quest(
        proposed["initialization_id"],
        quest_draft_revision=proposed["quest_draft"]["revision"],
        quest_draft_hash=proposed["quest_draft"]["hash"],
        proposal_ref=proposed["proposal"]["ref"],
        proposal_hash=proposed["proposal"]["hash"],
        preview_ref=previewed["confirmation_preview"]["ref"],
        preview_hash=previewed["confirmation_preview"]["hash"],
        idempotency_key=f"{key_prefix}-root-confirm",
    )
    for _step in range(8):
        if not human.reconcile_once():
            break
    completed = human.query_quest_creation(opened["initialization_id"])
    assert completed["status"] == "completed"
    return str(completed["quest_ref"]), str(completed["question_ref"])


def _queue_initial_question_deepfetch(human, key_prefix: str) -> dict[str, object]:
    opened = human.create_quest({}, f"{key_prefix}-quest-open")
    probed = human.observe_host_compute(
        opened["initialization_id"],
        ["GPU-manual-lifecycle"],
        f"{key_prefix}-compute",
    )
    draft = dict(probed["quest_draft"]["value"])
    draft.update(
        {
            "goal": "验证另一个 Quest 的等待状态不会阻塞后续问题检索。",
            "completion_criteria": "形成一份独立检索快照。",
            "time_budget": "30d",
            "route": "deepfetch",
            "literature": {
                "mode": "oa_only",
                "library_entry_url": "",
                "scope_exclusions": "",
                "accepted_material_bindings": [],
            },
            "background_and_initial_direction": "检索独立的排队公平性证据。",
        }
    )
    saved = human.revise_quest_draft(
        opened["initialization_id"],
        draft,
        probed["quest_draft"]["hash"],
        f"{key_prefix}-quest-draft",
        probed["quest_draft"]["revision"],
    )
    human.prepare_acquisition_session(
        saved["initialization_id"],
        saved["quest_draft"]["hash"],
        f"{key_prefix}-acquisition",
        saved["quest_draft"]["revision"],
    )
    return human.generate_question_proposal(
        saved["initialization_id"],
        saved["quest_draft"]["hash"],
        f"{key_prefix}-deepfetch",
        saved["quest_draft"]["revision"],
    )


def _seed_value(*, deepfetch_preference: str) -> dict[str, object]:
    return {
        "intent": "我想知道压缩推理轨迹是否会遗忘前面的关键信息。",
        "fields": {
            "title": "",
            "unknown_statement": "压缩后是否遗忘关键前文？",
            "answer_shape": "",
            "applicability_scope": "",
            "background_context": "显存有限。",
            "requirements_constraints": "",
        },
        "accepted_material_bindings": [],
        "deepfetch_preference": deepfetch_preference,
    }


def _open_and_confirm_seed(
    human,
    *,
    quest_ref: str,
    parent_question_ref: str,
    key_prefix: str,
    deepfetch_preference: str = "skip",
) -> dict[str, object]:
    opened = human.open_manual_question_creation(
        quest_ref=quest_ref,
        parent_question_ref=parent_question_ref,
        idempotency_key=f"{key_prefix}-manual-open",
    )
    return human.confirm_manual_creation_seed(
        opened["context_ref"],
        seed=_seed_value(deepfetch_preference=deepfetch_preference),
        idempotency_key=f"{key_prefix}-seed-confirm",
    )


def _queue_manual_drafting_turn(
    runtime, *, key_prefix: str
) -> tuple[dict[str, object], dict[str, object]]:
    quest_ref, parent_question_ref = _accept_root_question(runtime, key_prefix)
    human = runtime.owners.human_collaboration
    seeded = _open_and_confirm_seed(
        human,
        quest_ref=quest_ref,
        parent_question_ref=parent_question_ref,
        key_prefix=key_prefix,
    )
    queued = human.send_manual_drafting_message(
        seeded["context_ref"],
        expected_basis_hash=seeded["seed"]["hash"],
        message="请收紧后续问题的未知边界。",
        idempotency_key=f"{key_prefix}-drafting-message",
    )
    return seeded, queued["drafting_session"]["turns"][-1]


def _confirm_waived_manual_question(
    human,
    *,
    quest_ref: str,
    parent_question_ref: str,
    key_prefix: str,
) -> dict[str, object]:
    seeded = _open_and_confirm_seed(
        human,
        quest_ref=quest_ref,
        parent_question_ref=parent_question_ref,
        key_prefix=key_prefix,
        deepfetch_preference="skip",
    )
    prepared = human.record_manual_deepfetch_waiver(
        seeded["context_ref"],
        expected_seed_ref=seeded["seed"]["ref"],
        expected_seed_hash=seeded["seed"]["hash"],
        idempotency_key=f"{key_prefix}-waiver",
    )
    saved = human.save_manual_question_proposal(
        seeded["context_ref"],
        content=dict(QUESTION),
        expected_basis_hash=prepared["research_path"]["basis_hash"],
        idempotency_key=f"{key_prefix}-proposal",
    )
    human.confirm_manual_question_proposal(
        seeded["context_ref"],
        proposal_ref=saved["proposal"]["ref"],
        proposal_hash=saved["proposal"]["hash"],
        idempotency_key=f"{key_prefix}-confirmation",
    )
    return seeded


def test_reconcile_rejects_a_stale_captured_parent_receipt_before_rm_or_rg(
    tmp_path: Path,
) -> None:
    runtime = _build_runtime(tmp_path / "manual-stale-target-reconcile")
    try:
        quest_ref, parent_question_ref = _accept_root_question(
            runtime, "manual-stale-target-reconcile"
        )
        human = runtime.owners.human_collaboration
        seeded = _confirm_waived_manual_question(
            human,
            quest_ref=quest_ref,
            parent_question_ref=parent_question_ref,
            key_prefix="manual-stale-target-reconcile",
        )
        before = runtime.projection.query_snapshot()
        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE hc_manual_question_creations SET "
                    "parent_question_receipt_hash = :invalid WHERE context_ref = "
                    ":context_ref"
                ),
                {
                    "context_ref": seeded["context_ref"],
                    "invalid": "f" * 64,
                },
            )

        assert human.reconcile_once()
        assert runtime.owners.research_memory.query_manual_question_content(
            seeded["context_ref"]
        ) is None
        after = runtime.projection.query_snapshot()
        assert len(after["question_tree"]["items"]) == len(
            before["question_tree"]["items"]
        )
        with pytest.raises(OwnerConflict, match="manual_creation_target_stale"):
            human.query_manual_question_creation(seeded["context_ref"])
    finally:
        runtime.close()


def test_cancelled_context_rejects_a_corrupt_cancellation_receipt(
    tmp_path: Path,
) -> None:
    runtime = _build_runtime(tmp_path / "manual-corrupt-cancellation")
    try:
        quest_ref, parent_question_ref = _accept_root_question(
            runtime, "manual-corrupt-cancellation"
        )
        human = runtime.owners.human_collaboration
        opened = human.open_manual_question_creation(
            quest_ref=quest_ref,
            parent_question_ref=parent_question_ref,
            idempotency_key="manual-corrupt-cancellation-open",
        )
        cancelled = human.cancel_manual_question_creation(
            opened["context_ref"],
            "manual-corrupt-cancellation-cancel",
        )
        assert cancelled["cancellation"]["status"] == "accepted"
        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE hc_manual_question_creations SET cancel_receipt_hash = "
                    ":invalid WHERE context_ref = :context_ref"
                ),
                {
                    "context_ref": opened["context_ref"],
                    "invalid": "f" * 64,
                },
            )

        with pytest.raises(
            OwnerConflict,
            match="manual_question_cancellation_receipt_invalid",
        ):
            human.query_manual_question_creation(opened["context_ref"])
    finally:
        runtime.close()


def test_waiting_initial_question_deepfetch_does_not_starve_manual_queue(
    tmp_path: Path,
) -> None:
    provider = DeterministicManualDeepFetchProvider()
    runtime = _build_runtime(
        tmp_path / "manual-deepfetch-fairness",
        deepfetch_provider=provider,
        acquisition_provider=DeterministicAcquisitionProvider(),
    )
    try:
        human = runtime.owners.human_collaboration
        quest_ref, parent_question_ref = _accept_root_question(
            runtime, "manual-deepfetch-fairness-parent"
        )
        accepted_quest = runtime.owners.research_graph.query_quest_by_ref(quest_ref)
        assert accepted_quest is not None
        literature = accepted_quest.draft["literature"]
        assert isinstance(literature, dict)
        runtime.owners.agent_runtime.prepare_acquisition_session(
            initialization_id=accepted_quest.initialization_id,
            draft_revision=accepted_quest.draft_revision,
            config={
                "mode": literature["mode"],
                "library_entry_url": literature["library_entry_url"],
            },
            provider=DeterministicAcquisitionProvider(),
        )
        bound_session = runtime.owners.agent_runtime.bind_acquisition_session_to_quest(
            accepted_quest.initialization_id,
            accepted_quest.quest_ref,
        )
        assert bound_session.status == "ready"
        queued_root = _queue_initial_question_deepfetch(
            human, "manual-deepfetch-fairness-root"
        )
        root_request_ref = queued_root["deepfetch"]["request_ref"]
        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE ar_acquisition_sessions SET status = 'waiting_user', "
                    "reason_code = 'institutional_login_required' WHERE "
                    "initialization_id = :initialization_id"
                ),
                {"initialization_id": queued_root["initialization_id"]},
            )
        seeded = _open_and_confirm_seed(
            human,
            quest_ref=quest_ref,
            parent_question_ref=parent_question_ref,
            key_prefix="manual-deepfetch-fairness",
            deepfetch_preference="use",
        )
        queued_manual = human.start_manual_creation_deepfetch(
            seeded["context_ref"],
            expected_seed_ref=seeded["seed"]["ref"],
            expected_seed_hash=seeded["seed"]["hash"],
            idempotency_key="manual-deepfetch-fairness-start",
        )
        manual_request_ref = queued_manual["research_path"]["deepfetch"][
            "request_ref"
        ]

        assert runtime.deepfetch.process_once()
        root_after = human.query_quest_creation(queued_root["initialization_id"])
        assert root_after["deepfetch"]["status"] == "queued", root_after["deepfetch"]
        completed = human.query_manual_question_creation(seeded["context_ref"])
        assert completed["research_path"]["status"] == "ready"
        assert [request.request_ref for request in provider.requests] == [
            manual_request_ref
        ]
        assert human.query_deepfetch_request(root_request_ref) is not None
    finally:
        runtime.close()


def test_manual_drafting_turns_are_durably_claimed_recovered_and_serialized(
    tmp_path: Path,
) -> None:
    drafting = DeterministicDraftingAdapter()
    runtime = _build_runtime(
        tmp_path / "manual-drafting-durable",
        drafting=drafting,
    )
    try:
        quest_ref, parent_question_ref = _accept_root_question(
            runtime, "manual-drafting-durable"
        )
        human = runtime.owners.human_collaboration
        seeded = _open_and_confirm_seed(
            human,
            quest_ref=quest_ref,
            parent_question_ref=parent_question_ref,
            key_prefix="manual-drafting-durable",
        )
        basis_hash = seeded["seed"]["hash"]

        def send(index: int) -> dict[str, object]:
            return human.send_manual_drafting_message(
                seeded["context_ref"],
                expected_basis_hash=basis_hash,
                message=f"durable message {index}",
                idempotency_key=f"manual-drafting-durable-{index}",
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            queued_views = list(executor.map(send, (1, 2)))

        assert drafting.intent_requests == []
        queued = human.query_manual_question_creation(seeded["context_ref"])
        turns = queued["drafting_session"]["turns"]
        assert [turn["ordinal"] for turn in turns] == [1, 2]
        assert [turn["assistant_status"] for turn in turns] == ["queued", "queued"]
        assert all(view["drafting_session"]["turns"] for view in queued_views)

        # Simulate a daemon crash after durable claim but before provider result.
        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE hc_manual_drafting_turns SET assistant_status = "
                    "'running', assistant_attempt_count = 1, assistant_started_at = 0 "
                    "WHERE turn_ref = :turn_ref"
                ),
                {"turn_ref": turns[0]["ref"]},
            )

        assert human.process_drafting_once()
        first_completed = human.query_manual_question_creation(
            seeded["context_ref"]
        )["drafting_session"]["turns"]
        assert [turn["assistant_status"] for turn in first_completed] == [
            "completed",
            "queued",
        ]
        assert drafting.intent_requests[0].job_ref == f"{turns[0]['ref']}:claim:2"

        assert human.process_drafting_once()
        completed = human.query_manual_question_creation(seeded["context_ref"])
        assert [
            turn["assistant_status"]
            for turn in completed["drafting_session"]["turns"]
        ] == ["completed", "completed"]
        assert drafting.intent_requests[1].native_session_ref == (
            "manual-lifecycle-drafting-session"
        )

        waived = human.record_manual_deepfetch_waiver(
            seeded["context_ref"],
            expected_seed_ref=seeded["seed"]["ref"],
            expected_seed_hash=seeded["seed"]["hash"],
            idempotency_key="manual-drafting-durable-waiver",
        )
        research_basis_hash = waived["research_path"]["basis_hash"]
        first_proposal = human.save_manual_question_proposal(
            seeded["context_ref"],
            content=QUESTION,
            expected_basis_hash=research_basis_hash,
            idempotency_key="manual-drafting-durable-proposal-1",
        )
        human.send_manual_drafting_message(
            seeded["context_ref"],
            expected_basis_hash=research_basis_hash,
            message="queued before proposal replacement",
            idempotency_key="manual-drafting-durable-stale-proposal",
        )
        replacement = dict(QUESTION)
        replacement["title"] = "替换后的精确问题提案"
        human.save_manual_question_proposal(
            seeded["context_ref"],
            content=replacement,
            expected_basis_hash=research_basis_hash,
            expected_proposal_ref=first_proposal["proposal"]["ref"],
            expected_proposal_hash=first_proposal["proposal"]["hash"],
            idempotency_key="manual-drafting-durable-proposal-2",
        )
        assert human.process_drafting_once()
        stale_turn = human.query_manual_question_creation(
            seeded["context_ref"]
        )["drafting_session"]["turns"][-1]
        assert stale_turn["assistant_status"] == "failed"
        assert stale_turn["reason"] == {
            "code": "manual_drafting_context_invalid"
        }
        assert len(drafting.intent_requests) == 2

        human.send_manual_drafting_message(
            seeded["context_ref"],
            expected_basis_hash=research_basis_hash,
            message="queued before terminal cancellation",
            idempotency_key="manual-drafting-durable-terminal",
        )
        cancelled = human.cancel_manual_question_creation(
            seeded["context_ref"],
            idempotency_key="manual-drafting-durable-cancel",
        )
        assert cancelled["drafting_session"]["status"] == "closed"
        assert cancelled["drafting_session"]["turns"][-1][
            "assistant_status"
        ] == "failed"
        assert cancelled["drafting_session"]["turns"][-1]["reason"] == {
            "code": "manual_creation_cancelled"
        }
        assert len(drafting.intent_requests) == 2

        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE hc_manual_drafting_turns SET assistant_content = "
                    "'tampered durable reply' WHERE turn_ref = :turn_ref"
                ),
                {"turn_ref": turns[0]["ref"]},
            )
        with pytest.raises(OwnerConflict, match="manual_drafting_turn_invalid"):
            human.query_manual_question_creation(seeded["context_ref"])
    finally:
        runtime.close()


def test_manual_drafting_power_failure_finishes_the_waiting_responsibility(
    tmp_path: Path,
) -> None:
    drafting = RuntimeAwareDraftingAdapter()
    inhibitor = SwitchablePowerInhibitor()
    runtime = _build_runtime(
        tmp_path / "manual-drafting-power-failure",
        drafting=drafting,
        power_inhibitor=inhibitor,
    )
    try:
        seeded, turn = _queue_manual_drafting_turn(
            runtime, key_prefix="manual-drafting-power-failure"
        )
        drafting.runtime = runtime
        drafting.finished_job_refs.clear()
        drafting.finish_active_operations.clear()
        inhibitor.available = False

        assert runtime.owners.human_collaboration.process_drafting_once()

        settled = runtime.owners.human_collaboration.query_manual_question_creation(
            seeded["context_ref"]
        )["drafting_session"]["turns"][-1]
        assert settled["ref"] == turn["ref"]
        assert settled["assistant_status"] == "unavailable"
        assert settled["reason"] == {
            "code": "power_inhibitor_test_unavailable"
        }
        evidence = runtime.query_runtime_observability()
        assert evidence["responsibilities"] == []
        assert evidence["durable_waiting"] == []
        assert drafting.intent_requests == []
        assert drafting.finish_active_operations == [()]
    finally:
        runtime.close()


def test_manual_drafting_stopped_process_is_fenced_before_spool_cleanup(
    tmp_path: Path,
) -> None:
    drafting = RuntimeAwareDraftingAdapter()
    drafting.reply_failure = "codex_cli_stopped"
    runtime = _build_runtime(
        tmp_path / "manual-drafting-stopped",
        drafting=drafting,
    )
    try:
        seeded, turn = _queue_manual_drafting_turn(
            runtime, key_prefix="manual-drafting-stopped"
        )
        drafting.runtime = runtime
        drafting.finished_job_refs.clear()
        drafting.finish_active_operations.clear()

        assert runtime.owners.human_collaboration.process_drafting_once()

        requeued = runtime.owners.human_collaboration.query_manual_question_creation(
            seeded["context_ref"]
        )["drafting_session"]["turns"][-1]
        first_job_ref = f"{turn['ref']}:claim:1"
        assert requeued["assistant_status"] == "queued"
        assert drafting.finished_job_refs == [first_job_ref]
        assert drafting.finish_active_operations == [()]
        assert runtime.query_runtime_observability()["responsibilities"] == []

        assert runtime.owners.human_collaboration.process_drafting_once()
        completed = runtime.owners.human_collaboration.query_manual_question_creation(
            seeded["context_ref"]
        )["drafting_session"]["turns"][-1]
        assert completed["assistant_status"] == "completed"
    finally:
        runtime.close()


def test_manual_drafting_unknown_outcome_keeps_hold_and_spool_for_reconciliation(
    tmp_path: Path,
) -> None:
    drafting = RuntimeAwareDraftingAdapter()
    drafting.reply_failure = "codex_job_outcome_unknown"
    runtime = _build_runtime(
        tmp_path / "manual-drafting-unknown-outcome",
        drafting=drafting,
    )
    try:
        seeded, turn = _queue_manual_drafting_turn(
            runtime, key_prefix="manual-drafting-unknown-outcome"
        )
        drafting.runtime = runtime
        drafting.finished_job_refs.clear()
        drafting.finish_active_operations.clear()

        assert runtime.owners.human_collaboration.process_drafting_once()

        unresolved = runtime.owners.human_collaboration.query_manual_question_creation(
            seeded["context_ref"]
        )["drafting_session"]["turns"][-1]
        job_ref = f"{turn['ref']}:claim:1"
        assert unresolved["assistant_status"] == "running"
        assert drafting.finished_job_refs == []
        assert [
            item["operation_ref"]
            for item in runtime.query_runtime_observability()["responsibilities"]
        ] == [job_ref]
    finally:
        runtime.close()


def test_expired_manual_drafting_claim_is_cancelled_fenced_then_replaced(
    tmp_path: Path,
) -> None:
    drafting = RuntimeAwareDraftingAdapter()
    runtime = _build_runtime(
        tmp_path / "manual-drafting-expired-protection",
        drafting=drafting,
    )
    try:
        seeded, turn = _queue_manual_drafting_turn(
            runtime, key_prefix="manual-drafting-expired-protection"
        )
        job_ref = f"{turn['ref']}:claim:1"
        effect = _manual_drafting_runtime_effect(
            context_ref=str(seeded["context_ref"]),
            turn_ref=str(turn["ref"]),
            claim_attempt=1,
            job_ref=job_ref,
        )
        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE hc_manual_drafting_turns SET assistant_status = "
                    "'running', assistant_attempt_count = 1, assistant_started_at = 0 "
                    "WHERE turn_ref = :turn_ref"
                ),
                {"turn_ref": turn["ref"]},
            )
        runtime.runtime_protection.acquire(effect)
        drafting.runtime = runtime
        drafting.cancelled_job_refs.clear()
        drafting.finished_job_refs.clear()
        drafting.finish_active_operations.clear()

        assert runtime.owners.human_collaboration.process_drafting_once()

        completed = runtime.owners.human_collaboration.query_manual_question_creation(
            seeded["context_ref"]
        )["drafting_session"]["turns"][-1]
        assert completed["assistant_status"] == "completed"
        assert drafting.cancelled_job_refs == [job_ref]
        assert drafting.finished_job_refs[0] == job_ref
        assert drafting.finish_active_operations[0] == ()
        assert runtime.query_runtime_observability()["responsibilities"] == []
    finally:
        runtime.close()


def test_expired_manual_drafting_claim_stays_running_without_cancel_proof(
    tmp_path: Path,
) -> None:
    drafting = RuntimeAwareDraftingAdapter()
    runtime = _build_runtime(
        tmp_path / "manual-drafting-expired-unresolved",
        drafting=drafting,
    )
    try:
        seeded, turn = _queue_manual_drafting_turn(
            runtime, key_prefix="manual-drafting-expired-unresolved"
        )
        job_ref = f"{turn['ref']}:claim:1"
        effect = _manual_drafting_runtime_effect(
            context_ref=str(seeded["context_ref"]),
            turn_ref=str(turn["ref"]),
            claim_attempt=1,
            job_ref=job_ref,
        )
        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE hc_manual_drafting_turns SET assistant_status = "
                    "'running', assistant_attempt_count = 1, assistant_started_at = 0 "
                    "WHERE turn_ref = :turn_ref"
                ),
                {"turn_ref": turn["ref"]},
            )
        runtime.runtime_protection.acquire(effect)
        drafting.runtime = runtime
        drafting.cancel_acknowledged = False
        drafting.finished_job_refs.clear()

        assert not runtime.owners.human_collaboration.process_drafting_once()

        unresolved = runtime.owners.human_collaboration.query_manual_question_creation(
            seeded["context_ref"]
        )["drafting_session"]["turns"][-1]
        assert unresolved["assistant_status"] == "running"
        assert drafting.finished_job_refs == []
        assert [
            item["operation_ref"]
            for item in runtime.query_runtime_observability()["responsibilities"]
        ] == [job_ref]
    finally:
        runtime.close()


def test_startup_cancels_and_fences_interrupted_manual_drafting_claim(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "manual-drafting-startup-protection"
    drafting = RuntimeAwareDraftingAdapter()
    inhibitor = SwitchablePowerInhibitor()
    original = _build_runtime(
        data_root,
        drafting=drafting,
        power_inhibitor=inhibitor,
    )
    restarted = None
    try:
        seeded, turn = _queue_manual_drafting_turn(
            original, key_prefix="manual-drafting-startup-protection"
        )
        job_ref = f"{turn['ref']}:claim:1"
        effect = _manual_drafting_runtime_effect(
            context_ref=str(seeded["context_ref"]),
            turn_ref=str(turn["ref"]),
            claim_attempt=1,
            job_ref=job_ref,
        )
        with original._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE hc_manual_drafting_turns SET assistant_status = "
                    "'running', assistant_attempt_count = 1, "
                    "assistant_started_at = :now WHERE turn_ref = :turn_ref"
                ),
                {"turn_ref": turn["ref"], "now": time.time()},
            )
        original.runtime_protection.acquire(effect)
        drafting.cancelled_job_refs.clear()
        drafting.finished_job_refs.clear()

        restarted = _build_runtime(
            data_root,
            drafting=drafting,
            power_inhibitor=inhibitor,
        )
        drafting.runtime = restarted

        recovered = restarted.owners.human_collaboration.query_manual_question_creation(
            seeded["context_ref"]
        )["drafting_session"]["turns"][-1]
        assert recovered["assistant_status"] == "queued"
        assert drafting.cancelled_job_refs == [job_ref]
        assert drafting.finished_job_refs == [job_ref]
        assert restarted.query_runtime_observability()["responsibilities"] == []
    finally:
        if restarted is not None:
            restarted.close()
        original.close()


def test_manual_deepfetch_state_artifact_is_fail_closed(
    tmp_path: Path,
) -> None:
    runtime = _build_runtime(tmp_path / "manual-deepfetch-artifact")
    try:
        quest_ref, parent_question_ref = _accept_root_question(
            runtime, "manual-deepfetch-artifact"
        )
        human = runtime.owners.human_collaboration
        seeded = _open_and_confirm_seed(
            human,
            quest_ref=quest_ref,
            parent_question_ref=parent_question_ref,
            key_prefix="manual-deepfetch-artifact",
            deepfetch_preference="use",
        )
        queued = human.start_manual_creation_deepfetch(
            seeded["context_ref"],
            expected_seed_ref=seeded["seed"]["ref"],
            expected_seed_hash=seeded["seed"]["hash"],
            idempotency_key="manual-deepfetch-artifact-start",
        )
        request_ref = queued["research_path"]["deepfetch"]["request_ref"]

        with runtime._database.write() as connection:
            connection.exec_driver_sql("PRAGMA ignore_check_constraints = ON")
            connection.execute(
                text(
                    "UPDATE hc_manual_deepfetch_requests SET status = 'succeeded' "
                    "WHERE request_ref = :request_ref"
                ),
                {"request_ref": request_ref},
            )
            connection.exec_driver_sql("PRAGMA ignore_check_constraints = OFF")

        with pytest.raises(OwnerConflict, match="manual_deepfetch_request_invalid"):
            human.query_manual_question_creation(seeded["context_ref"])
    finally:
        runtime.close()


def test_explicit_waiver_and_exact_proposal_reconcile_rm_before_rg(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "manual-lifecycle"
    runtime = _build_runtime(data_root)
    try:
        quest_ref, parent_question_ref = _accept_root_question(runtime, "lifecycle")
        human = runtime.owners.human_collaboration
        seeded = _open_and_confirm_seed(
            human,
            quest_ref=quest_ref,
            parent_question_ref=parent_question_ref,
            key_prefix="lifecycle",
            deepfetch_preference="skip",
        )

        # A Seed preference remains non-authoritative until this separate command.
        assert seeded["research_path"] == {
            "status": "not_selected",
            "deepfetch": None,
            "waiver": None,
        }
        waived = human.record_manual_deepfetch_waiver(
            seeded["context_ref"],
            expected_seed_ref=seeded["seed"]["ref"],
            expected_seed_hash=seeded["seed"]["hash"],
            idempotency_key="lifecycle-explicit-waiver",
        )
        waiver = waived["research_path"]["waiver"]
        assert waived["research_path"]["status"] == "waived"
        assert waiver["ref"] != seeded["seed"]["ref"]
        assert waiver["receipt"] == {
            "status": "accepted",
            "issuer": "human_collaboration",
            "kind": "manual_creation_deepfetch_waiver",
            "receipt_ref": waiver["receipt"]["receipt_ref"],
            "subject_ref": waiver["ref"],
            "payload_hash": waiver["hash"],
        }

        saved = human.save_manual_question_proposal(
            seeded["context_ref"],
            content=dict(QUESTION),
            expected_basis_hash=waived["research_path"]["basis_hash"],
            idempotency_key="lifecycle-save-proposal",
        )
        assert saved["proposal"]["content"] == QUESTION
        assert saved["proposal"]["basis_hash"] == (
            waived["research_path"]["basis_hash"]
        )
        assert saved["confirmation"] is None

        confirmed = human.confirm_manual_question_proposal(
            seeded["context_ref"],
            proposal_ref=saved["proposal"]["ref"],
            proposal_hash=saved["proposal"]["hash"],
            idempotency_key="lifecycle-confirm-proposal",
        )
        assert confirmed["proposal"]["content"] == QUESTION
        assert confirmed["proposal"]["ref"] == saved["proposal"]["ref"]
        assert confirmed["proposal"]["hash"] == saved["proposal"]["hash"]
        assert confirmed["confirmation"]["proposal_ref"] == saved["proposal"]["ref"]
        assert confirmed["confirmation"]["proposal_hash"] == saved["proposal"]["hash"]
        assert confirmed["confirmation"]["receipt"] == {
            "status": "accepted",
            "issuer": "human_collaboration",
            "kind": "manual_question_proposal_confirmation",
            "receipt_ref": confirmed["confirmation"]["receipt"]["receipt_ref"],
            "subject_ref": saved["proposal"]["ref"],
            "payload_hash": confirmed["confirmation"]["hash"],
        }
        assert confirmed["receipts"]["content"] == {"status": "not_attempted"}
        assert confirmed["receipts"]["question"] == {"status": "not_attempted"}
        assert confirmed["question_anchor"] is None

        assert human.reconcile_once()
        content_accepted = human.query_manual_question_creation(
            seeded["context_ref"]
        )
        assert content_accepted["receipts"]["content"]["status"] == "accepted"
        assert content_accepted["receipts"]["content"]["issuer"] == (
            "research_memory"
        )
        assert content_accepted["receipts"]["content"]["kind"] == (
            "manual_question_content_acceptance"
        )
        assert content_accepted["receipts"]["question"] == {
            "status": "not_attempted"
        }
        assert content_accepted["question_anchor"] is None
        content_receipt = dict(content_accepted["receipts"]["content"])
    finally:
        runtime.close()

    restarted = _build_runtime(data_root)
    try:
        human = restarted.owners.human_collaboration
        recovered = human.query_manual_question_creation(seeded["context_ref"])
        assert recovered["proposal"] == confirmed["proposal"]
        assert recovered["confirmation"] == confirmed["confirmation"]
        assert recovered["receipts"]["content"] == content_receipt
        assert recovered["receipts"]["question"] == {"status": "not_attempted"}
        assert recovered["question_anchor"] is None

        assert human.reconcile_once()
        completed = human.query_manual_question_creation(seeded["context_ref"])
        question_receipt = completed["receipts"]["question"]
        assert completed["status"] == "completed"
        assert question_receipt["status"] == "accepted"
        assert question_receipt["issuer"] == "research_graph"
        assert completed["question_anchor"] == {
            "question_ref": question_receipt["subject_ref"],
            "quest_ref": quest_ref,
            "parent_question_ref": parent_question_ref,
            "content_ref": content_receipt["subject_ref"],
            "content_hash": canonical_hash(QUESTION),
            "schema_ref": "meta-research/formal-question-content/v1",
            "content_receipt_ref": content_receipt["receipt_ref"],
            "question_receipt_ref": question_receipt["receipt_ref"],
        }
        stable_anchor = dict(completed["question_anchor"])
        assert not human.reconcile_once()
        assert human.query_manual_question_creation(seeded["context_ref"])[
            "question_anchor"
        ] == stable_anchor
    finally:
        restarted.close()

    verified = _build_runtime(data_root)
    try:
        assert verified.owners.human_collaboration.query_manual_question_creation(
            seeded["context_ref"]
        )["question_anchor"] == stable_anchor
        with verified._database.write() as connection:
            connection.execute(
                text(
                    "DELETE FROM rg_manual_questions WHERE question_ref = "
                    ":question_ref"
                ),
                {"question_ref": stable_anchor["question_ref"]},
            )
        with pytest.raises(
            OwnerConflict,
            match="manual_question_owner_binding_missing",
        ):
            verified.owners.human_collaboration.query_manual_question_creation(
                seeded["context_ref"]
            )
    finally:
        verified.close()


def test_manual_waiver_idempotency_replays_and_conflicting_payload_fails_closed(
    tmp_path: Path,
) -> None:
    runtime = _build_runtime(tmp_path / "manual-waiver-idempotency")
    try:
        quest_ref, parent_question_ref = _accept_root_question(runtime, "waiver-idem")
        human = runtime.owners.human_collaboration
        seeded = _open_and_confirm_seed(
            human,
            quest_ref=quest_ref,
            parent_question_ref=parent_question_ref,
            key_prefix="waiver-idem",
            deepfetch_preference="skip",
        )

        waived = human.record_manual_deepfetch_waiver(
            seeded["context_ref"],
            expected_seed_ref=seeded["seed"]["ref"],
            expected_seed_hash=seeded["seed"]["hash"],
            idempotency_key="same-waiver-command",
        )
        replay = human.record_manual_deepfetch_waiver(
            seeded["context_ref"],
            expected_seed_ref=seeded["seed"]["ref"],
            expected_seed_hash=seeded["seed"]["hash"],
            idempotency_key="same-waiver-command",
        )
        assert replay == waived

        with pytest.raises(
            OwnerConflict,
            match="manual_creation_waiver_idempotency_conflict",
        ):
            human.record_manual_deepfetch_waiver(
                seeded["context_ref"],
                expected_seed_ref=seeded["seed"]["ref"],
                expected_seed_hash="f" * 64,
                idempotency_key="same-waiver-command",
            )

        durable = human.query_manual_question_creation(seeded["context_ref"])
        assert durable["research_path"] == waived["research_path"]
        assert durable["proposal"] is None
        assert durable["question_anchor"] is None
    finally:
        runtime.close()


def test_window_close_keeps_the_context_but_explicit_cancel_is_terminal(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "manual-cancel"
    runtime = _build_runtime(data_root)
    try:
        quest_ref, parent_question_ref = _accept_root_question(runtime, "cancel")
        human = runtime.owners.human_collaboration
        seeded = _open_and_confirm_seed(
            human,
            quest_ref=quest_ref,
            parent_question_ref=parent_question_ref,
            key_prefix="cancel",
            deepfetch_preference="later",
        )

        # Closing the Web modal sends no HC command. Querying is the public proof
        # that the durable active context remains available for reopening.
        reopened = human.query_manual_question_creation(seeded["context_ref"])
        assert reopened["status"] == "seed_confirmed"
        assert reopened["seed"] == seeded["seed"]
        assert human.query_current_manual_question_creation(
            quest_ref=quest_ref,
            parent_question_ref=parent_question_ref,
        )["context_ref"] == seeded["context_ref"]

        cancelled = human.cancel_manual_question_creation(
            seeded["context_ref"],
            "explicit-cancel",
        )
        replay = human.cancel_manual_question_creation(
            seeded["context_ref"],
            "explicit-cancel",
        )
        assert replay == cancelled
        assert cancelled["status"] == "cancelled"
        assert cancelled["seed"] == seeded["seed"]
        assert cancelled["question_anchor"] is None
        assert (
            human.query_current_manual_question_creation(
                quest_ref=quest_ref,
                parent_question_ref=parent_question_ref,
            )
            is None
        )
        assert human.query_manual_question_creation(seeded["context_ref"]) == (
            cancelled
        )
    finally:
        runtime.close()

    restarted = _build_runtime(data_root)
    try:
        durable = restarted.owners.human_collaboration.query_manual_question_creation(
            seeded["context_ref"]
        )
        assert durable["status"] == "cancelled"
        assert durable["seed"] == seeded["seed"]
        assert durable["question_anchor"] is None
    finally:
        restarted.close()


def test_manual_deepfetch_reuses_the_quest_session_and_returns_an_rm_receipt(
    tmp_path: Path,
) -> None:
    provider = DeterministicManualDeepFetchProvider()
    runtime = _build_runtime(
        tmp_path / "manual-deepfetch",
        deepfetch_provider=provider,
    )
    try:
        quest_ref, parent_question_ref = _accept_root_question(
            runtime, "manual-deepfetch"
        )
        human = runtime.owners.human_collaboration
        seeded = _open_and_confirm_seed(
            human,
            quest_ref=quest_ref,
            parent_question_ref=parent_question_ref,
            key_prefix="manual-deepfetch",
            deepfetch_preference="use",
        )
        queued = human.start_manual_creation_deepfetch(
            seeded["context_ref"],
            expected_seed_ref=seeded["seed"]["ref"],
            expected_seed_hash=seeded["seed"]["hash"],
            idempotency_key="manual-deepfetch-start",
        )
        assert queued["research_path"]["status"] == "queued"
        assert queued["research_path"]["waiver"] is None
        assert queued["receipts"]["research"] == {"status": "pending"}
        assert queued["proposal"] is None

        authorized = human.query_next_deepfetch_request()
        assert authorized is not None
        assert authorized.creation_context_kind == "manual_question_creation"
        assert authorized.creation_context_ref == seeded["context_ref"]
        assert authorized.context_generation == seeded["generation"]
        assert authorized.quest_ref == quest_ref
        assert authorized.parent_question_ref == parent_question_ref
        assert authorized.result_route == "same_manual_question_creation_proposal"

        assert runtime.deepfetch.process_once()
        researched = human.query_manual_question_creation(seeded["context_ref"])
        assert researched["status"] == "research_ready", researched["research_path"]
        assert researched["research_path"]["status"] == "ready"
        assert researched["research_path"]["waiver"] is None
        deepfetch = researched["research_path"]["deepfetch"]
        assert deepfetch["status"] == "succeeded"
        assert deepfetch["snapshot_ref"]
        snapshot = runtime.owners.research_memory.query_literature_snapshot(
            deepfetch["snapshot_ref"]
        )
        assert snapshot is not None
        assert snapshot.creation_context_kind == "manual_question_creation"
        assert snapshot.creation_context_ref == seeded["context_ref"]
        assert snapshot.quest_ref == quest_ref
        quest = runtime.owners.research_graph.query_quest_by_ref(quest_ref)
        assert quest is not None
        assert (
            runtime.owners.research_memory.query_literature_snapshot_for_basis(
                quest.initialization_id,
                quest.draft_revision,
                quest.draft_hash,
            )
            is None
        )
        assert researched["receipts"]["research"] == snapshot.receipt.as_public_dict()
        assert researched["research_path"]["deepfetch"][
            "literature_snapshot"
        ]["receipt"] == snapshot.receipt.as_public_dict()
        assert len(provider.requests) == 1
        acquisition = runtime.owners.agent_runtime.query_acquisition_session(
            quest_ref=quest_ref
        )
        assert acquisition is not None
        assert acquisition.session_ref == authorized.acquisition_session_ref
    finally:
        runtime.close()


@pytest.mark.parametrize("invalid_first_result", [False, True])
def test_manual_deepfetch_resumes_same_managed_run_after_human_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_first_result: bool,
) -> None:
    provider = HumanRequestManualDeepFetchProvider(
        invalid_first_result=invalid_first_result
    )
    runtime = _build_runtime(
        tmp_path / "manual-deepfetch-root-human-request",
        deepfetch_provider=provider,
        acquisition_provider=DeterministicAcquisitionProvider(),
    )
    owner = runtime.owners.agent_runtime
    provider.owner = owner
    try:
        quest_ref, parent_question_ref = _accept_root_question(
            runtime, "manual-deepfetch-root-human-request"
        )
        human = runtime.owners.human_collaboration
        seeded = _open_and_confirm_seed(
            human,
            quest_ref=quest_ref,
            parent_question_ref=parent_question_ref,
            key_prefix="manual-deepfetch-root-human-request",
            deepfetch_preference="use",
        )
        queued = human.start_manual_creation_deepfetch(
            seeded["context_ref"],
            expected_seed_ref=seeded["seed"]["ref"],
            expected_seed_hash=seeded["seed"]["hash"],
            idempotency_key="manual-deepfetch-root-human-request-start",
        )
        request_ref = queued["research_path"]["deepfetch"]["request_ref"]

        assert runtime.deepfetch.process_once()
        assert provider.human_request is not None
        human_request_ref = provider.human_request["request_ref"]
        first_run = owner.query_deepfetch_run(request_ref)
        assert first_run is not None and first_run.status == "running"
        assert first_run.attempt_generation == 1
        assert first_run.failure_code == "human_request_wait"
        suspended = owner.query_managed_run(first_run.run_ref)
        assert suspended is not None and suspended["status"] == "suspended"

        human.respond_to_human_request(
            human_request_ref,
            decision="deferred",
            facts={},
            note="Continue without the optional input.",
            idempotency_key="manual-deepfetch-root-human-request-response",
        )
        disposed = owner.query_human_request(human_request_ref)
        assert disposed is not None and disposed["status"] == "unsatisfied"
        assert disposed["direct_waiters"][0]["status"] == "consumed"
        resumed = owner.query_managed_run(first_run.run_ref)
        assert resumed is not None and resumed["status"] == "running"

        observed_errors: list[str] = []
        execute_deepfetch = owner.execute_deepfetch

        def observe_execute(request, selected_provider):
            try:
                return execute_deepfetch(request, selected_provider)
            except OwnerConflict as error:
                observed_errors.append(error.code)
                raise

        monkeypatch.setattr(owner, "execute_deepfetch", observe_execute)
        progressed = runtime.deepfetch.process_once()

        assert "deepfetch_run_busy" not in observed_errors
        assert progressed
        completed = owner.query_deepfetch_run(request_ref)
        assert completed is not None and completed.status == "executed"
        assert completed.run_ref == first_run.run_ref
        assert completed.root_session_ref == first_run.root_session_ref
        assert completed.attempt_generation == 2
        assert completed.provider_operation_generation == 2
        assert len(provider.requests) == 2
        assert provider.requests[1].human_request_resume == {
            "effect_id": provider.human_request["open_effect"]["effect_id"],
            "request_ref": human_request_ref,
            "phase": "turn-1",
        }
        researched = human.query_manual_question_creation(seeded["context_ref"])
        assert researched["research_path"]["status"] == "ready"
    finally:
        runtime.close()


@pytest.mark.parametrize("research_path", ["waiver", "deepfetch"])
def test_reconcile_requires_the_exact_research_receipt_lineage(
    tmp_path: Path,
    research_path: str,
) -> None:
    provider = DeterministicManualDeepFetchProvider()
    runtime = _build_runtime(
        tmp_path / f"manual-research-lineage-{research_path}",
        deepfetch_provider=provider,
    )
    try:
        quest_ref, parent_question_ref = _accept_root_question(
            runtime, f"manual-research-lineage-{research_path}"
        )
        human = runtime.owners.human_collaboration
        seeded = _open_and_confirm_seed(
            human,
            quest_ref=quest_ref,
            parent_question_ref=parent_question_ref,
            key_prefix=f"manual-research-lineage-{research_path}",
            deepfetch_preference="skip" if research_path == "waiver" else "use",
        )
        if research_path == "waiver":
            prepared = human.record_manual_deepfetch_waiver(
                seeded["context_ref"],
                expected_seed_ref=seeded["seed"]["ref"],
                expected_seed_hash=seeded["seed"]["hash"],
                idempotency_key="manual-research-lineage-waiver",
            )
        else:
            human.start_manual_creation_deepfetch(
                seeded["context_ref"],
                expected_seed_ref=seeded["seed"]["ref"],
                expected_seed_hash=seeded["seed"]["hash"],
                idempotency_key="manual-research-lineage-deepfetch",
            )
            assert runtime.deepfetch.process_once()
            prepared = human.query_manual_question_creation(seeded["context_ref"])

        saved = human.save_manual_question_proposal(
            seeded["context_ref"],
            content=dict(QUESTION),
            expected_basis_hash=prepared["research_path"]["basis_hash"],
            idempotency_key=f"manual-research-lineage-{research_path}-proposal",
        )
        human.confirm_manual_question_proposal(
            seeded["context_ref"],
            proposal_ref=saved["proposal"]["ref"],
            proposal_hash=saved["proposal"]["hash"],
            idempotency_key=f"manual-research-lineage-{research_path}-confirm",
        )

        with runtime._database.write() as connection:
            if research_path == "waiver":
                connection.execute(
                    text(
                        "UPDATE hc_manual_question_creations SET "
                        "waiver_receipt_hash = :invalid WHERE context_ref = "
                        ":context_ref"
                    ),
                    {
                        "context_ref": seeded["context_ref"],
                        "invalid": "f" * 64,
                    },
                )
            else:
                connection.execute(
                    text(
                        "DELETE FROM rm_literature_snapshots WHERE snapshot_ref = "
                        ":snapshot_ref"
                    ),
                    {
                        "snapshot_ref": prepared["research_path"]["deepfetch"][
                            "snapshot_ref"
                        ]
                    },
                )

        assert human.reconcile_once()
        assert (
            runtime.owners.research_memory.query_manual_question_content(
                seeded["context_ref"]
            )
            is None
        )
        with pytest.raises(
            OwnerConflict,
            match="manual_question_research_lineage_invalid",
        ):
            human.query_manual_question_creation(seeded["context_ref"])
    finally:
        runtime.close()


def test_explicit_cancel_fences_a_queued_manual_deepfetch_without_a_waiver(
    tmp_path: Path,
) -> None:
    provider = DeterministicManualDeepFetchProvider()
    runtime = _build_runtime(
        tmp_path / "manual-deepfetch-cancel",
        deepfetch_provider=provider,
    )
    try:
        quest_ref, parent_question_ref = _accept_root_question(
            runtime, "manual-deepfetch-cancel"
        )
        human = runtime.owners.human_collaboration
        seeded = _open_and_confirm_seed(
            human,
            quest_ref=quest_ref,
            parent_question_ref=parent_question_ref,
            key_prefix="manual-deepfetch-cancel",
            deepfetch_preference="use",
        )
        queued = human.start_manual_creation_deepfetch(
            seeded["context_ref"],
            expected_seed_ref=seeded["seed"]["ref"],
            expected_seed_hash=seeded["seed"]["hash"],
            idempotency_key="manual-deepfetch-cancel-start",
        )
        request_ref = queued["research_path"]["deepfetch"]["request_ref"]
        cancelled = human.cancel_manual_question_creation(
            seeded["context_ref"],
            "manual-deepfetch-cancel-command",
        )
        assert cancelled["status"] == "cancelled"
        assert cancelled["research_path"]["status"] == "cancelled"
        assert cancelled["research_path"]["waiver"] is None
        assert cancelled["receipts"]["research"] == {"status": "pending"}
        assert cancelled["question_anchor"] is None
        assert runtime.owners.agent_runtime.query_deepfetch_run(request_ref) is None
        assert not runtime.deepfetch.process_once()
        assert provider.requests == []
    finally:
        runtime.close()


def test_failed_manual_deepfetch_is_not_a_waiver_and_retries_one_request(
    tmp_path: Path,
) -> None:
    provider = FailOnceManualDeepFetchProvider()
    runtime = _build_runtime(
        tmp_path / "manual-deepfetch-retry",
        deepfetch_provider=provider,
    )
    try:
        quest_ref, parent_question_ref = _accept_root_question(
            runtime, "manual-deepfetch-retry"
        )
        human = runtime.owners.human_collaboration
        seeded = _open_and_confirm_seed(
            human,
            quest_ref=quest_ref,
            parent_question_ref=parent_question_ref,
            key_prefix="manual-deepfetch-retry",
            deepfetch_preference="use",
        )
        queued = human.start_manual_creation_deepfetch(
            seeded["context_ref"],
            expected_seed_ref=seeded["seed"]["ref"],
            expected_seed_hash=seeded["seed"]["hash"],
            idempotency_key="manual-deepfetch-retry-start",
        )
        request_ref = queued["research_path"]["deepfetch"]["request_ref"]
        assert runtime.deepfetch.process_once()
        failed = human.query_manual_question_creation(seeded["context_ref"])
        assert failed["research_path"]["status"] == "failed"
        assert failed["research_path"]["waiver"] is None
        assert failed["research_path"]["deepfetch"]["failure"] == {
            "code": "web_search_temporarily_unavailable"
        }
        first_run = runtime.owners.agent_runtime.query_deepfetch_run(request_ref)
        assert first_run is not None
        assert first_run.attempt_generation == 1

        retried = human.start_manual_creation_deepfetch(
            seeded["context_ref"],
            expected_seed_ref=seeded["seed"]["ref"],
            expected_seed_hash=seeded["seed"]["hash"],
            idempotency_key="manual-deepfetch-retry-again",
        )
        assert retried["research_path"]["deepfetch"]["request_ref"] == request_ref
        assert retried["research_path"]["status"] == "queued"
        assert runtime.deepfetch.process_once()
        succeeded = human.query_manual_question_creation(seeded["context_ref"])
        assert succeeded["research_path"]["status"] == "ready"
        second_run = runtime.owners.agent_runtime.query_deepfetch_run(request_ref)
        assert second_run is not None
        assert second_run.run_ref == first_run.run_ref
        assert second_run.attempt_generation == 2
        assert len(provider.requests) == 2
    finally:
        runtime.close()


def test_nonretryable_manual_deepfetch_requires_cancelled_context_successor(
    tmp_path: Path,
) -> None:
    provider = ArtifactDriftOnceManualDeepFetchProvider()
    runtime = _build_runtime(
        tmp_path / "manual-deepfetch-artifact-successor",
        deepfetch_provider=provider,
    )
    client, write_headers = _authenticated_client(runtime)
    try:
        quest_ref, parent_question_ref = _accept_root_question(
            runtime, "manual-deepfetch-artifact-successor"
        )
        human = runtime.owners.human_collaboration
        seeded = _open_and_confirm_seed(
            human,
            quest_ref=quest_ref,
            parent_question_ref=parent_question_ref,
            key_prefix="manual-deepfetch-artifact-successor",
            deepfetch_preference="use",
        )
        queued = human.start_manual_creation_deepfetch(
            seeded["context_ref"],
            expected_seed_ref=seeded["seed"]["ref"],
            expected_seed_hash=seeded["seed"]["hash"],
            idempotency_key="manual-deepfetch-artifact-start",
        )
        old_context_ref = seeded["context_ref"]
        old_request_ref = queued["research_path"]["deepfetch"]["request_ref"]

        assert runtime.deepfetch.process_once()
        failed = human.query_manual_question_creation(old_context_ref)
        assert failed["research_path"]["status"] == "failed"
        assert failed["research_path"]["deepfetch"]["failure"] == {
            "code": "deepfetch_acquisition_artifact_drift"
        }
        old_run = runtime.owners.agent_runtime.query_deepfetch_run(old_request_ref)
        assert old_run is not None
        assert old_run.provider_operation_retry_permitted is False

        same_basis = client.post(
            f"/api/v1/manual-question-creations/{old_context_ref}/deepfetch",
            headers=_command_headers(
                write_headers, "manual-deepfetch-artifact-same-basis"
            ),
            json={
                "expected_seed_ref": seeded["seed"]["ref"],
                "expected_seed_hash": seeded["seed"]["hash"],
            },
        )

        assert same_basis.status_code == 409
        assert same_basis.json()["detail"]["code"] == (
            "deepfetch_successor_required"
        )
        assert human.query_manual_question_creation(old_context_ref) == failed
        assert len(provider.requests) == 1

        cancelled_response = client.post(
            f"/api/v1/manual-question-creations/{old_context_ref}/cancel",
            headers=_command_headers(
                write_headers, "manual-deepfetch-artifact-cancel"
            ),
            json={},
        )
        assert cancelled_response.status_code == 200
        assert cancelled_response.json()["status"] == "cancelled"

        opened_response = client.post(
            "/api/v1/manual-question-creations",
            headers=_command_headers(
                write_headers, "manual-deepfetch-artifact-successor-open"
            ),
            json={
                "quest_ref": quest_ref,
                "parent_question_ref": parent_question_ref,
            },
        )
        assert opened_response.status_code == 201
        opened = opened_response.json()
        assert opened["context_ref"] != old_context_ref
        assert opened["generation"] == seeded["generation"] + 1

        seeded_response = client.post(
            f"/api/v1/manual-question-creations/{opened['context_ref']}"
            "/seed-confirmation",
            headers=_command_headers(
                write_headers, "manual-deepfetch-artifact-successor-seed"
            ),
            json={"seed": _seed_value(deepfetch_preference="use")},
        )
        assert seeded_response.status_code == 201
        successor_seed = seeded_response.json()
        started_response = client.post(
            f"/api/v1/manual-question-creations/{opened['context_ref']}/deepfetch",
            headers=_command_headers(
                write_headers, "manual-deepfetch-artifact-successor-start"
            ),
            json={
                "expected_seed_ref": successor_seed["seed"]["ref"],
                "expected_seed_hash": successor_seed["seed"]["hash"],
            },
        )
        assert started_response.status_code == 202
        successor = started_response.json()
        new_request_ref = successor["research_path"]["deepfetch"]["request_ref"]
        assert new_request_ref != old_request_ref

        assert runtime.deepfetch.process_once()
        succeeded = client.get(
            f"/api/v1/manual-question-creations/{opened['context_ref']}"
        ).json()
        assert succeeded["research_path"]["status"] == "ready"
        new_run = runtime.owners.agent_runtime.query_deepfetch_run(new_request_ref)
        assert new_run is not None
        assert new_run.run_ref != old_run.run_ref
        assert new_run.attempt_generation == 1
        assert [request.request_ref for request in provider.requests] == [
            old_request_ref,
            new_request_ref,
        ]
    finally:
        client.close()
        runtime.close()


def test_failed_manual_deepfetch_requires_a_separate_explicit_waiver(
    tmp_path: Path,
) -> None:
    runtime = _build_runtime(
        tmp_path / "manual-deepfetch-failure-waiver",
        deepfetch_provider=FailOnceManualDeepFetchProvider(),
    )
    try:
        quest_ref, parent_question_ref = _accept_root_question(
            runtime, "manual-deepfetch-failure-waiver"
        )
        human = runtime.owners.human_collaboration
        seeded = _open_and_confirm_seed(
            human,
            quest_ref=quest_ref,
            parent_question_ref=parent_question_ref,
            key_prefix="manual-deepfetch-failure-waiver",
            deepfetch_preference="use",
        )
        human.start_manual_creation_deepfetch(
            seeded["context_ref"],
            expected_seed_ref=seeded["seed"]["ref"],
            expected_seed_hash=seeded["seed"]["hash"],
            idempotency_key="manual-deepfetch-failure-waiver-start",
        )
        assert runtime.deepfetch.process_once()
        failed = human.query_manual_question_creation(seeded["context_ref"])
        assert failed["research_path"]["status"] == "failed"
        assert failed["research_path"]["waiver"] is None

        waived = human.record_manual_deepfetch_waiver(
            seeded["context_ref"],
            expected_seed_ref=seeded["seed"]["ref"],
            expected_seed_hash=seeded["seed"]["hash"],
            idempotency_key="manual-deepfetch-failure-explicit-waiver",
        )
        assert waived["research_path"]["status"] == "waived"
        assert waived["research_path"]["waiver"]["receipt"]["status"] == (
            "accepted"
        )
        assert waived["research_path"]["deepfetch"]["status"] == "failed"
        saved = human.save_manual_question_proposal(
            seeded["context_ref"],
            content=dict(QUESTION),
            expected_basis_hash=waived["research_path"]["basis_hash"],
            idempotency_key="manual-deepfetch-failure-waiver-proposal",
        )
        assert saved["proposal"]["content"] == QUESTION
    finally:
        runtime.close()


def test_authenticated_manual_api_publishes_only_the_stable_child_anchor(
    tmp_path: Path,
) -> None:
    runtime = _build_runtime(tmp_path / "manual-public-api")
    client, write_headers = _authenticated_client(runtime)
    try:
        quest_ref, parent_question_ref = _accept_root_question(runtime, "manual-api")
        initial_snapshot = client.get("/api/v1/snapshot").json()
        assert initial_snapshot["research_space"]["question_count"] == 1

        opened_response = client.post(
            "/api/v1/manual-question-creations",
            headers=_command_headers(write_headers, "manual-api-open"),
            json={
                "quest_ref": quest_ref,
                "parent_question_ref": parent_question_ref,
            },
        )
        assert opened_response.status_code == 201
        opened = opened_response.json()
        context_ref = opened["context_ref"]
        current = client.get(
            "/api/v1/manual-question-creations/current",
            params={
                "quest_ref": quest_ref,
                "parent_question_ref": parent_question_ref,
            },
        )
        assert current.status_code == 200
        assert current.json()["context_ref"] == context_ref

        seeded_response = client.post(
            f"/api/v1/manual-question-creations/{context_ref}/seed-confirmation",
            headers=_command_headers(write_headers, "manual-api-seed"),
            json={
                "seed": {
                    "intent": "从当前问题继续追问稀有形态保真的边界。",
                    "fields": QUESTION,
                    "accepted_material_bindings": [],
                    "deepfetch_preference": "skip",
                }
            },
        )
        assert seeded_response.status_code == 201
        seeded = seeded_response.json()
        research_response = client.post(
            f"/api/v1/manual-question-creations/{context_ref}/deepfetch-waiver",
            headers=_command_headers(write_headers, "manual-api-waiver"),
            json={
                "expected_seed_ref": seeded["seed"]["ref"],
                "expected_seed_hash": seeded["seed"]["hash"],
            },
        )
        assert research_response.status_code == 201
        researched = research_response.json()
        saved_response = client.put(
            f"/api/v1/manual-question-creations/{context_ref}/proposal",
            headers=_command_headers(write_headers, "manual-api-proposal"),
            json={
                "expected_basis_hash": researched["research_path"]["basis_hash"],
                "expected_proposal_ref": None,
                "expected_proposal_hash": None,
                "content": QUESTION,
            },
        )
        assert saved_response.status_code == 200
        saved = saved_response.json()
        confirmed_response = client.post(
            f"/api/v1/manual-question-creations/{context_ref}/proposal-confirmation",
            headers=_command_headers(write_headers, "manual-api-confirm"),
            json={
                "proposal_ref": saved["proposal"]["ref"],
                "proposal_hash": saved["proposal"]["hash"],
            },
        )
        assert confirmed_response.status_code == 202
        assert confirmed_response.json()["question_anchor"] is None

        # Confirmation is not a provisional graph write, nor a Stage advance.
        before_reconcile = client.get("/api/v1/snapshot").json()
        assert before_reconcile["research_space"]["question_count"] == 1
        assert before_reconcile["research_space"]["foreground_cycle_count"] == 1

        assert runtime.owners.human_collaboration.reconcile_once()
        after_memory_response = client.get("/api/v1/snapshot")
        assert after_memory_response.status_code == 200, after_memory_response.json()
        after_memory = after_memory_response.json()
        assert after_memory["research_space"]["question_count"] == 1
        assert runtime.owners.human_collaboration.reconcile_once()

        completed = client.get(
            f"/api/v1/manual-question-creations/{context_ref}"
        ).json()
        assert completed["status"] == "completed"
        child_ref = completed["question_anchor"]["question_ref"]
        final_snapshot = client.get("/api/v1/snapshot").json()
        assert final_snapshot["research_space"]["question_count"] == 2
        assert final_snapshot["research_space"]["foreground_cycle_count"] == 1
        assert final_snapshot["manual_question_creation"]["status"] == "ready"
        by_ref = {
            item["question_ref"]: item
            for item in final_snapshot["question_tree"]["items"]
        }
        assert by_ref[parent_question_ref]["parent_question_ref"] is None
        assert by_ref[child_ref]["parent_question_ref"] == parent_question_ref
    finally:
        client.close()
        runtime.close()


def test_authenticated_manual_detail_reports_missing_context_as_not_found(
    tmp_path: Path,
) -> None:
    runtime = _build_runtime(tmp_path / "manual-missing-api")
    client, _write_headers = _authenticated_client(runtime)
    try:
        response = client.get(
            "/api/v1/manual-question-creations/manual-creation-missing"
        )

        assert response.status_code == 404
        assert response.json() == {
            "detail": {"code": "manual_question_creation_not_found"}
        }
    finally:
        client.close()
        runtime.close()
