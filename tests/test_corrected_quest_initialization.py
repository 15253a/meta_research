from __future__ import annotations

import json
import subprocess
import time
import threading
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from meta_research.composition import build_production_runtime
from meta_research.owners.common import OwnerConflict, canonical_json
from meta_research.paths import prepare_data_root
from meta_research.quest_drafting import (
    CodexDraftingAdapter,
    DraftingUnavailable,
    HostComputeDevice,
    HostComputeSnapshot,
    INTENT_MESSAGE_MAX_LENGTH,
    INTENT_REPLY_MAX_LENGTH,
    IntentTurnRequest,
    IntentTurnResult,
    ProposalDraftRequest,
    ProposalDraftResult,
)
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
        self.draft_calls = 0

    def draft(self, request: ProposalDraftRequest) -> ProposalDraftResult:
        self.draft_calls += 1
        return ProposalDraftResult(content=QUESTION, adapter_kind="test_deterministic")

    def reply(self, request: IntentTurnRequest) -> IntentTurnResult:
        self.intent_requests.append(request)
        return IntentTurnResult(
            reply=f"建议先把完成标准具体化：{request.message}",
            native_session_ref=request.native_session_ref or "test-native-session",
            adapter_kind="test_deterministic",
        )


class DeterministicProbe:
    def observe(self) -> HostComputeSnapshot:
        return HostComputeSnapshot(
            status="ready",
            observed_at=1720000000.0,
            devices=(
                HostComputeDevice(
                    uuid="GPU-test-1",
                    name="Test GPU",
                    memory_total_mib=81920,
                ),
            ),
            adapter_kind="test_probe",
        )


class BlockingCountingProbe(DeterministicProbe):
    def __init__(self) -> None:
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()

    def observe(self) -> HostComputeSnapshot:
        with self._lock:
            self.calls += 1
            self.started.set()
        if not self.release.wait(timeout=3):
            raise AssertionError("host compute probe was not released")
        return super().observe()


class BlockingIntentAdapter(DeterministicDraftingAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()
        self.cancel_calls = 0

    def cancel_active(self) -> None:
        self.cancel_calls += 1
        self.release.set()

    def reply(self, request: IntentTurnRequest) -> IntentTurnResult:
        self.intent_requests.append(request)
        self.started.set()
        if not self.release.wait(timeout=3):
            raise AssertionError("blocking intent adapter was not released")
        return IntentTurnResult(
            reply="late reply must not cross a cancellation fence",
            native_session_ref="late-native-session",
            adapter_kind="test_blocking",
        )


class BlockingProposalAdapter(DeterministicDraftingAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def draft(self, request: ProposalDraftRequest) -> ProposalDraftResult:
        self.started.set()
        if not self.release.wait(timeout=3):
            raise AssertionError("blocking proposal adapter was not released")
        return super().draft(request)


class ExpiringProposalAdapter(DeterministicDraftingAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.started = (threading.Event(), threading.Event())
        self.release = (threading.Event(), threading.Event())
        self._lock = threading.Lock()

    def draft(self, request: ProposalDraftRequest) -> ProposalDraftResult:
        with self._lock:
            attempt = self.draft_calls
            self.draft_calls += 1
        self.started[attempt].set()
        if not self.release[attempt].wait(timeout=3):
            raise AssertionError("leased proposal attempt was not released")
        return ProposalDraftResult(
            content={
                **QUESTION,
                "title": "expired-old" if attempt == 0 else "replacement-new",
            },
            adapter_kind="test_lease_fence",
        )


class ExpiringIntentAdapter(DeterministicDraftingAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.started = (threading.Event(), threading.Event())
        self.release = (threading.Event(), threading.Event())
        self._calls = 0
        self._lock = threading.Lock()

    def reply(self, request: IntentTurnRequest) -> IntentTurnResult:
        with self._lock:
            attempt = self._calls
            self._calls += 1
        self.started[attempt].set()
        if not self.release[attempt].wait(timeout=3):
            raise AssertionError("leased intent attempt was not released")
        return IntentTurnResult(
            reply="expired-old" if attempt == 0 else "replacement-new",
            native_session_ref=f"lease-native-{attempt}",
            adapter_kind="test_lease_fence",
        )


class OversizedProposalAdapter(DeterministicDraftingAdapter):
    def draft(self, request: ProposalDraftRequest) -> ProposalDraftResult:
        self.draft_calls += 1
        return ProposalDraftResult(
            content={**QUESTION, "title": "题" * 501},
            adapter_kind="test_oversized",
        )


class OversizedIntentAdapter(DeterministicDraftingAdapter):
    def reply(self, request: IntentTurnRequest) -> IntentTurnResult:
        self.intent_requests.append(request)
        return IntentTurnResult(
            reply="答" * (INTENT_REPLY_MAX_LENGTH + 1),
            native_session_ref="oversized-native-session",
            adapter_kind="test_oversized",
        )


class InterruptedOnceAdapter(DeterministicDraftingAdapter):
    def __init__(self) -> None:
        super().__init__()
        self._interrupt_proposal = True
        self._interrupt_intent = True

    def draft(self, request: ProposalDraftRequest) -> ProposalDraftResult:
        if self._interrupt_proposal:
            self._interrupt_proposal = False
            raise DraftingUnavailable("codex_cli_stopped")
        return super().draft(request)

    def reply(self, request: IntentTurnRequest) -> IntentTurnResult:
        if self._interrupt_intent:
            self._interrupt_intent = False
            raise DraftingUnavailable("codex_cli_stopped")
        return super().reply(request)


class ClaimToSpawnBarrierRunner:
    """Expose the exact post-claim/pre-spawn window to a terminal command."""

    def __init__(self, result: dict[str, object], stdout: str = "") -> None:
        self._result = result
        self._stdout = stdout
        self.before_spawn = threading.Event()
        self.allow_spawn = threading.Event()
        self._lock = threading.Lock()
        self.cancelled_refs: set[str] = set()
        self.spawned_refs: list[str] = []
        self.global_cancel_calls = 0

    def __call__(
        self, argv: list[str], input_text: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        return self._run("unscoped", argv)

    def run_job(
        self, job_ref: str, argv: list[str], input_text: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        return self._run(job_ref, argv)

    def _run(
        self, job_ref: str, argv: list[str]
    ) -> subprocess.CompletedProcess[str]:
        self.before_spawn.set()
        if not self.allow_spawn.wait(timeout=3):
            raise AssertionError("claim-to-spawn barrier was not released")
        with self._lock:
            if job_ref in self.cancelled_refs:
                raise DraftingUnavailable("codex_cli_stopped")
            self.spawned_refs.append(job_ref)
        result_path = Path(argv[argv.index("--output-last-message") + 1])
        result_path.write_text(json.dumps(self._result), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, stdout=self._stdout, stderr="")

    def cancel_job(self, job_ref: str) -> None:
        with self._lock:
            self.cancelled_refs.add(job_ref)

    def finish_job(self, job_ref: str) -> None:
        with self._lock:
            self.cancelled_refs.discard(job_ref)

    def cancel_active(self) -> None:
        # A global best-effort cancellation cannot close the pre-registration gap.
        self.global_cancel_calls += 1


def _authenticated_client(runtime) -> tuple[TestClient, dict[str, str]]:
    base_url = "http://testserver"
    client = TestClient(
        create_app(runtime, base_url=base_url, control_key="control-secret"),
        base_url=base_url,
    )
    bootstrap = runtime.authentication.issue_bootstrap_token()
    response = client.post(
        "/auth/bootstrap", headers={"Origin": base_url}, json={"token": bootstrap}
    )
    assert response.status_code == 200
    return client, {
        "Origin": base_url,
        "X-CSRF-Token": response.json()["csrf_token"],
    }


def _poll(client: TestClient, initialization_id: str, wanted: str):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        view = client.get(
            f"/api/v1/quest-initializations/{initialization_id}"
        ).json()
        if view["status"] == wanted:
            return view
        time.sleep(0.02)
    raise AssertionError(f"Quest creation did not reach {wanted}: {view}")


def _ready_v2(runtime, prefix: str) -> dict[str, object]:
    hc = runtime.owners.human_collaboration
    opened = hc.create_quest({}, f"{prefix}-open")
    probed = hc.observe_host_compute(
        opened["initialization_id"], ["GPU-test-1"], f"{prefix}-probe"
    )
    draft = dict(probed["quest_draft"]["value"])
    draft.update(
        {
            "goal": "验证 stale Proposal 必须由显式复核重新绑定。",
            "completion_criteria": "普通自动保存不能静默恢复 currentness。",
            "time_budget": "30d",
        }
    )
    saved = hc.revise_quest_draft(
        opened["initialization_id"],
        draft,
        probed["quest_draft"]["hash"],
        f"{prefix}-save",
        probed["quest_draft"]["revision"],
    )
    hc.generate_question_proposal(
        opened["initialization_id"],
        saved["quest_draft"]["hash"],
        f"{prefix}-generate",
        saved["quest_draft"]["revision"],
    )
    assert hc.process_drafting_once()
    ready = hc.query_quest_creation(opened["initialization_id"])
    assert ready["status"] == "proposal_ready"
    return ready


@pytest.mark.parametrize("command", ["revise", "generate", "save"])
def test_v2_owner_commands_require_revision_as_well_as_hash(
    tmp_path: Path,
    command: str,
) -> None:
    adapter = DeterministicDraftingAdapter()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / f"v2-revision-required-{command}"),
        proposal_drafter=adapter,
        intent_drafting_provider=adapter,
        host_compute_probe=DeterministicProbe(),
    )
    hc = runtime.owners.human_collaboration
    try:
        ready = _ready_v2(runtime, f"v2-revision-required-{command}")
        if command == "revise":
            action = lambda: hc.revise_quest_draft(
                ready["initialization_id"],
                {**ready["quest_draft"]["value"], "goal": "stale hash-only writer"},
                ready["quest_draft"]["hash"],
                "v2-hash-only-revise",
            )
        elif command == "generate":
            action = lambda: hc.generate_question_proposal(
                ready["initialization_id"],
                ready["quest_draft"]["hash"],
                "v2-hash-only-generate",
            )
        else:
            action = lambda: hc.save_question_proposal(
                ready["initialization_id"],
                ready["quest_draft"]["hash"],
                ready["proposal"]["content"],
                "v2-hash-only-save",
                expected_proposal_ref=ready["proposal"]["ref"],
                expected_proposal_hash=ready["proposal"]["hash"],
            )

        with pytest.raises(OwnerConflict, match="quest_draft_revision_required"):
            action()

        unchanged = hc.query_quest_creation(ready["initialization_id"])
        assert unchanged["quest_draft"] == ready["quest_draft"]
        assert unchanged["proposal"] == ready["proposal"]
        assert unchanged["proposal_generation"] == ready["proposal_generation"]
    finally:
        runtime.close()


@pytest.mark.parametrize("artifact", ["draft", "proposal"])
def test_hc_authoritative_json_must_match_its_bound_hash_before_query_or_confirm(
    tmp_path: Path,
    artifact: str,
) -> None:
    adapter = DeterministicDraftingAdapter()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / f"hc-json-integrity-{artifact}"),
        proposal_drafter=adapter,
        intent_drafting_provider=adapter,
        host_compute_probe=DeterministicProbe(),
    )
    hc = runtime.owners.human_collaboration
    try:
        ready = _ready_v2(runtime, f"hc-json-integrity-{artifact}")
        preview = ready["confirmation_preview"]
        with runtime._database.write() as connection:
            if artifact == "draft":
                tampered = {
                    **ready["quest_draft"]["value"],
                    "goal": "TAMPERED WITHOUT REHASHING",
                }
                connection.execute(
                    text(
                        "UPDATE hc_quest_initializations SET draft_json = "
                        ":payload WHERE initialization_id = :initialization_id"
                    ),
                    {
                        "payload": canonical_json(tampered),
                        "initialization_id": ready["initialization_id"],
                    },
                )
            else:
                tampered = {
                    **ready["proposal"]["content"],
                    "title": "TAMPERED WITHOUT REHASHING",
                }
                connection.execute(
                    text(
                        "UPDATE hc_quest_initializations SET proposal_json = "
                        ":payload WHERE initialization_id = :initialization_id"
                    ),
                    {
                        "payload": canonical_json(tampered),
                        "initialization_id": ready["initialization_id"],
                    },
                )

        with pytest.raises(
            OwnerConflict, match="quest_initialization_artifact_invalid"
        ):
            hc.query_quest_creation(ready["initialization_id"])
        with pytest.raises(
            OwnerConflict, match="quest_initialization_artifact_invalid"
        ):
            hc.confirm_quest(
                ready["initialization_id"],
                quest_draft_revision=ready["quest_draft"]["revision"],
                quest_draft_hash=ready["quest_draft"]["hash"],
                proposal_ref=ready["proposal"]["ref"],
                proposal_hash=ready["proposal"]["hash"],
                preview_ref=preview["ref"],
                preview_hash=preview["hash"],
                idempotency_key=f"hc-json-integrity-{artifact}-confirm",
            )
        with runtime._database.read() as connection:
            assert connection.execute(
                text(
                    "SELECT confirmation_ref FROM hc_quest_initializations WHERE "
                    "initialization_id = :initialization_id"
                ),
                {"initialization_id": ready["initialization_id"]},
            ).scalar_one_or_none() is None
    finally:
        runtime.close()


def test_intent_transcript_content_must_match_its_bound_hash(tmp_path: Path) -> None:
    adapter = DeterministicDraftingAdapter()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "intent-transcript-integrity"),
        proposal_drafter=adapter,
        intent_drafting_provider=adapter,
        host_compute_probe=DeterministicProbe(),
    )
    hc = runtime.owners.human_collaboration
    try:
        opened = hc.create_quest({}, "intent-transcript-integrity-open")
        queued = hc.send_intent_message(
            opened["initialization_id"],
            expected_draft_revision=opened["quest_draft"]["revision"],
            expected_draft_hash=opened["quest_draft"]["hash"],
            message="这是一条绑定哈希的用户消息。",
            idempotency_key="intent-transcript-integrity-send",
        )
        turn_ref = queued["intent_session"]["turns"][-1]["ref"]
        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE hc_intent_drafting_turns SET user_content = "
                    "'TAMPERED WITHOUT REHASHING' WHERE turn_ref = :turn_ref"
                ),
                {"turn_ref": turn_ref},
            )

        with pytest.raises(OwnerConflict, match="intent_transcript_integrity_invalid"):
            hc.query_quest_creation(opened["initialization_id"])
    finally:
        runtime.close()


def test_owner_rejects_an_oversized_intent_message_before_it_is_durable(
    tmp_path: Path,
) -> None:
    adapter = DeterministicDraftingAdapter()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "oversized-intent-message"),
        proposal_drafter=adapter,
        intent_drafting_provider=adapter,
        host_compute_probe=DeterministicProbe(),
    )
    hc = runtime.owners.human_collaboration
    try:
        opened = hc.create_quest({}, "oversized-intent-open")
        with pytest.raises(OwnerConflict, match="intent_message_too_long"):
            hc.send_intent_message(
                opened["initialization_id"],
                expected_draft_revision=opened["quest_draft"]["revision"],
                expected_draft_hash=opened["quest_draft"]["hash"],
                message="界" * (INTENT_MESSAGE_MAX_LENGTH + 1),
                idempotency_key="oversized-intent-send",
            )
        assert hc.query_quest_creation(opened["initialization_id"])[
            "intent_session"
        ]["turns"] == []
    finally:
        runtime.close()


def test_runtime_reopen_never_rewrites_a_confirmed_v1_receipt(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "confirmed-v1-preserved")
    adapter = DeterministicDraftingAdapter()
    runtime = build_production_runtime(
        data_root,
        proposal_drafter=adapter,
        intent_drafting_provider=adapter,
        host_compute_probe=DeterministicProbe(),
    )
    hc = runtime.owners.human_collaboration
    try:
        opened = hc.create_quest(
            {
                "goal": "保留已确认的 legacy v1 bundle。",
                "completion_criteria": "重启不能改写 confirmation binding。",
                "key_configuration": "v1 receipt 是 immutable evidence。",
                "literature_scope": "open_access",
                "initial_question_direction": "验证历史 receipt 保持不变。",
                "material_receipts": [],
            },
            "confirmed-v1-open",
        )
        hc.generate_question_proposal(
            opened["initialization_id"],
            opened["quest_draft"]["hash"],
            "confirmed-v1-generate",
            opened["quest_draft"]["revision"],
        )
        assert hc.process_drafting_once()
        ready = hc.query_quest_creation(opened["initialization_id"])
        previewed = hc.preview_confirmation(
            ready["initialization_id"],
            quest_draft_revision=ready["quest_draft"]["revision"],
            quest_draft_hash=ready["quest_draft"]["hash"],
            proposal_ref=ready["proposal"]["ref"],
            proposal_hash=ready["proposal"]["hash"],
            idempotency_key="confirmed-v1-preview",
        )
        preview = previewed["confirmation_preview"]
        confirmed = hc.confirm_quest(
            ready["initialization_id"],
            quest_draft_revision=ready["quest_draft"]["revision"],
            quest_draft_hash=ready["quest_draft"]["hash"],
            proposal_ref=ready["proposal"]["ref"],
            proposal_hash=ready["proposal"]["hash"],
            preview_ref=preview["ref"],
            preview_hash=preview["hash"],
            idempotency_key="confirmed-v1-confirm",
        )
        preserved_draft = confirmed["quest_draft"]
        preserved_receipt = confirmed["receipts"]["human_confirmation"]
    finally:
        runtime.close()

    reopened_runtime = build_production_runtime(
        data_root,
        proposal_drafter=adapter,
        intent_drafting_provider=adapter,
        host_compute_probe=DeterministicProbe(),
    )
    try:
        reopened = reopened_runtime.owners.human_collaboration.query_quest_creation(
            opened["initialization_id"]
        )
        assert reopened["quest_draft"] == preserved_draft
        assert reopened["quest_draft"]["schema_ref"] == (
            "meta-research/quest-initialization-draft/v1"
        )
        assert reopened["receipts"]["human_confirmation"] == preserved_receipt
        assert reopened["intent_session"]["status"] == "closed"
    finally:
        reopened_runtime.close()


def test_stale_proposal_autosave_requires_a_separate_explicit_review(
    tmp_path: Path,
) -> None:
    adapter = DeterministicDraftingAdapter()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "stale-review"),
        proposal_drafter=adapter,
        intent_drafting_provider=adapter,
        host_compute_probe=DeterministicProbe(),
    )
    hc = runtime.owners.human_collaboration
    try:
        ready = _ready_v2(runtime, "stale-review")
        changed_draft = dict(ready["quest_draft"]["value"])
        changed_draft["goal"] = "新的 Quest basis 必须保持 revision identity。"
        stale = hc.revise_quest_draft(
            ready["initialization_id"],
            changed_draft,
            ready["quest_draft"]["hash"],
            "stale-review-change-basis",
            ready["quest_draft"]["revision"],
        )
        assert stale["status"] == "proposal_stale"
        old_basis_revision = stale["proposal"]["basis_revision"]
        edited_content = {
            **stale["proposal"]["content"],
            "title": "已自动保存、尚未明确复核的旧 Proposal",
        }

        autosaved = hc.save_question_proposal(
            stale["initialization_id"],
            stale["quest_draft"]["hash"],
            edited_content,
            "stale-review-autosave",
            stale["quest_draft"]["revision"],
            stale["proposal"]["ref"],
            stale["proposal"]["hash"],
            explicit_review=False,
        )
        assert autosaved["proposal"]["content"] == edited_content
        assert autosaved["proposal"]["basis_revision"] == old_basis_revision
        assert autosaved["proposal"]["status"] == "stale"
        assert autosaved["status"] == "proposal_stale"
        assert autosaved["confirmation_preview"]["status"] == "stale"

        reviewed = hc.save_question_proposal(
            autosaved["initialization_id"],
            autosaved["quest_draft"]["hash"],
            edited_content,
            "stale-review-explicit",
            autosaved["quest_draft"]["revision"],
            autosaved["proposal"]["ref"],
            autosaved["proposal"]["hash"],
            explicit_review=True,
        )
        assert reviewed["proposal"]["basis_revision"] == reviewed["quest_draft"][
            "revision"
        ]
        assert reviewed["proposal"]["status"] == "current"
        assert reviewed["confirmation_preview"]["status"] == "current"
        assert reviewed["status"] == "proposal_ready"
    finally:
        runtime.close()


def test_incomplete_current_basis_proposal_is_not_ready_or_preview_current(
    tmp_path: Path,
) -> None:
    adapter = DeterministicDraftingAdapter()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "incomplete-proposal"),
        proposal_drafter=adapter,
        intent_drafting_provider=adapter,
        host_compute_probe=DeterministicProbe(),
    )
    hc = runtime.owners.human_collaboration
    try:
        ready = _ready_v2(runtime, "incomplete-proposal")
        incomplete_content = {**ready["proposal"]["content"], "title": ""}

        incomplete = hc.save_question_proposal(
            ready["initialization_id"],
            ready["quest_draft"]["hash"],
            incomplete_content,
            "incomplete-proposal-clear-required",
            ready["quest_draft"]["revision"],
            ready["proposal"]["ref"],
            ready["proposal"]["hash"],
        )

        assert incomplete["proposal"]["content"] == incomplete_content
        assert incomplete["proposal"]["status"] == "incomplete"
        assert incomplete["status"] == "draft"
        assert incomplete["confirmation_preview"]["status"] == "stale"
    finally:
        runtime.close()


def test_preview_currentness_reads_other_owner_revisions_through_their_interfaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = DeterministicDraftingAdapter()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "owner-revision-interface"),
        proposal_drafter=adapter,
        intent_drafting_provider=adapter,
        host_compute_probe=DeterministicProbe(),
    )
    hc = runtime.owners.human_collaboration
    try:
        ready = _ready_v2(runtime, "owner-revision-interface")
        graph_snapshot = runtime.owners.research_graph.query_snapshot()
        monkeypatch.setattr(
            runtime.owners.research_graph,
            "query_snapshot",
            lambda: replace(graph_snapshot, revision=graph_snapshot.revision + 1),
        )

        with pytest.raises(OwnerConflict, match="confirmation_preview_stale"):
            hc.confirm_quest(
                ready["initialization_id"],
                quest_draft_revision=ready["quest_draft"]["revision"],
                quest_draft_hash=ready["quest_draft"]["hash"],
                proposal_ref=ready["proposal"]["ref"],
                proposal_hash=ready["proposal"]["hash"],
                preview_ref=ready["confirmation_preview"]["ref"],
                preview_hash=ready["confirmation_preview"]["hash"],
                idempotency_key="owner-revision-interface-confirm",
            )
    finally:
        runtime.close()


def test_shutdown_interruption_requeues_nonterminal_proposal_and_intent_work(
    tmp_path: Path,
) -> None:
    adapter = InterruptedOnceAdapter()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "drafting-interrupted"),
        proposal_drafter=adapter,
        intent_drafting_provider=adapter,
        host_compute_probe=DeterministicProbe(),
    )
    hc = runtime.owners.human_collaboration
    try:
        opened = hc.create_quest({}, "drafting-interrupted-open")
        probed = hc.observe_host_compute(
            opened["initialization_id"],
            ["GPU-test-1"],
            "drafting-interrupted-probe",
        )
        draft = dict(probed["quest_draft"]["value"])
        draft.update(
            {
                "goal": "验证正常停机不会丢失在途 Drafter 工作。",
                "completion_criteria": "重启后仍能从 queued 状态继续。",
            }
        )
        saved = hc.revise_quest_draft(
            opened["initialization_id"],
            draft,
            probed["quest_draft"]["hash"],
            "drafting-interrupted-save",
            probed["quest_draft"]["revision"],
        )
        hc.generate_question_proposal(
            opened["initialization_id"],
            saved["quest_draft"]["hash"],
            "drafting-interrupted-generate",
            saved["quest_draft"]["revision"],
        )

        assert hc.process_drafting_once()
        interrupted_proposal = hc.query_quest_creation(opened["initialization_id"])
        assert interrupted_proposal["proposal_generation"]["status"] == "queued"
        assert hc.process_drafting_once()
        assert hc.query_quest_creation(opened["initialization_id"])["status"] == (
            "proposal_ready"
        )

        hc.send_intent_message(
            opened["initialization_id"],
            expected_draft_revision=saved["quest_draft"]["revision"],
            expected_draft_hash=saved["quest_draft"]["hash"],
            message="停机后还会继续回复吗？",
            idempotency_key="drafting-interrupted-intent",
        )
        assert hc.process_drafting_once()
        interrupted_turn = hc.query_quest_creation(opened["initialization_id"])[
            "intent_session"
        ]["turns"][-1]
        assert interrupted_turn["assistant_status"] == "queued"
        assert hc.process_drafting_once()
        completed_turn = hc.query_quest_creation(opened["initialization_id"])[
            "intent_session"
        ]["turns"][-1]
        assert completed_turn["assistant_status"] == "completed"
    finally:
        runtime.close()


def test_cancel_closes_intent_session_and_rejects_a_late_provider_reply(
    tmp_path: Path,
) -> None:
    adapter = BlockingIntentAdapter()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "cancel-intent"),
        proposal_drafter=adapter,
        intent_drafting_provider=adapter,
        host_compute_probe=DeterministicProbe(),
    )
    hc = runtime.owners.human_collaboration
    worker: threading.Thread | None = None
    try:
        opened = hc.create_quest({}, "cancel-intent-open")
        hc.send_intent_message(
            opened["initialization_id"],
            expected_draft_revision=opened["quest_draft"]["revision"],
            expected_draft_hash=opened["quest_draft"]["hash"],
            message="这个回复不应越过取消边界。",
            idempotency_key="cancel-intent-message",
        )
        worker = threading.Thread(target=hc.process_drafting_once)
        worker.start()
        assert adapter.started.wait(timeout=2)

        cancelled = hc.cancel_quest(
            opened["initialization_id"], "cancel-intent-command"
        )
        assert cancelled["status"] == "cancelled"
        assert cancelled["intent_session"]["status"] == "closed"
        worker.join(timeout=3)
        assert not worker.is_alive()
        assert adapter.cancel_calls == 1

        settled = hc.query_quest_creation(opened["initialization_id"])
        turn = settled["intent_session"]["turns"][0]
        assert turn["assistant_status"] == "failed"
        assert turn["assistant_content"] is None
        assert turn["reason"] == {"code": "intent_session_closed"}
        assert all(
            receipt["status"] == "not_attempted"
            for receipt in settled["receipts"].values()
        )
        assert not any(
            event.event_type == "human_collaboration.intent_reply_recorded"
            for event in runtime.feed.read_after(0).events
        )
    finally:
        adapter.release.set()
        if worker is not None:
            worker.join(timeout=3)
        runtime.close()


def test_cancel_fences_a_claimed_proposal_before_provider_spawn(
    tmp_path: Path,
) -> None:
    runner = ClaimToSpawnBarrierRunner(QUESTION)
    adapter = CodexDraftingAdapter(
        tmp_path / "cancel-before-proposal-spawn-provider",
        process_runner=runner,
    )
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "cancel-before-proposal-spawn"),
        proposal_drafter=adapter,
        intent_drafting_provider=adapter,
        host_compute_probe=DeterministicProbe(),
    )
    hc = runtime.owners.human_collaboration
    worker: threading.Thread | None = None
    try:
        opened = hc.create_quest({}, "cancel-before-proposal-spawn-open")
        probed = hc.observe_host_compute(
            opened["initialization_id"],
            ["GPU-test-1"],
            "cancel-before-proposal-spawn-probe",
        )
        draft = dict(probed["quest_draft"]["value"])
        draft.update(
            {
                "goal": "终态必须拦住尚未 Popen 的 proposal job。",
                "completion_criteria": "取消返回后 provider 不会启动。",
            }
        )
        saved = hc.revise_quest_draft(
            opened["initialization_id"],
            draft,
            probed["quest_draft"]["hash"],
            "cancel-before-proposal-spawn-save",
            probed["quest_draft"]["revision"],
        )
        queued = hc.generate_question_proposal(
            opened["initialization_id"],
            saved["quest_draft"]["hash"],
            "cancel-before-proposal-spawn-generate",
            saved["quest_draft"]["revision"],
        )
        generation_ref = queued["proposal_generation"]["ref"]

        worker = threading.Thread(target=hc.process_drafting_once)
        worker.start()
        assert runner.before_spawn.wait(timeout=2)
        claimed = hc.query_quest_creation(opened["initialization_id"])
        assert claimed["proposal_generation"]["status"] == "running"

        cancelled = hc.cancel_quest(
            opened["initialization_id"], "cancel-before-proposal-spawn-command"
        )
        assert cancelled["status"] == "cancelled"
        runner.allow_spawn.set()
        worker.join(timeout=3)
        assert not worker.is_alive()

        assert runner.spawned_refs == []
        assert runner.cancelled_refs == set()
        assert generation_ref not in runner.cancelled_refs
        settled = hc.query_quest_creation(opened["initialization_id"])
        assert settled["proposal_generation"]["status"] == "failed"
        assert settled["proposal_generation"]["failure"] == {
            "code": "initialization_cancelled"
        }
    finally:
        runner.allow_spawn.set()
        if worker is not None:
            worker.join(timeout=3)
        runtime.close()


def test_confirm_fences_a_claimed_intent_turn_before_provider_spawn(
    tmp_path: Path,
) -> None:
    proposal_adapter = DeterministicDraftingAdapter()
    runner = ClaimToSpawnBarrierRunner(
        {"reply": "this reply must never require a provider spawn"},
        stdout=(
            '{"type":"thread.started","thread_id":"barrier-native-session"}\n'
        ),
    )
    intent_adapter = CodexDraftingAdapter(
        tmp_path / "confirm-before-intent-spawn-provider",
        process_runner=runner,
    )
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "confirm-before-intent-spawn"),
        proposal_drafter=proposal_adapter,
        intent_drafting_provider=intent_adapter,
        host_compute_probe=DeterministicProbe(),
    )
    hc = runtime.owners.human_collaboration
    worker: threading.Thread | None = None
    try:
        ready = _ready_v2(runtime, "confirm-before-intent-spawn")
        queued = hc.send_intent_message(
            ready["initialization_id"],
            expected_draft_revision=ready["quest_draft"]["revision"],
            expected_draft_hash=ready["quest_draft"]["hash"],
            message="这条消息已 claim，但确认后不得启动 provider。",
            idempotency_key="confirm-before-intent-spawn-message",
        )
        turn_ref = queued["intent_session"]["turns"][-1]["ref"]

        worker = threading.Thread(target=hc.process_drafting_once)
        worker.start()
        assert runner.before_spawn.wait(timeout=2)
        claimed = hc.query_quest_creation(ready["initialization_id"])
        assert claimed["intent_session"]["turns"][-1]["assistant_status"] == (
            "running"
        )
        previewed = hc.preview_confirmation(
            ready["initialization_id"],
            quest_draft_revision=ready["quest_draft"]["revision"],
            quest_draft_hash=ready["quest_draft"]["hash"],
            proposal_ref=ready["proposal"]["ref"],
            proposal_hash=ready["proposal"]["hash"],
            idempotency_key="confirm-before-intent-spawn-preview",
        )
        preview = previewed["confirmation_preview"]

        confirmed = hc.confirm_quest(
            ready["initialization_id"],
            quest_draft_revision=ready["quest_draft"]["revision"],
            quest_draft_hash=ready["quest_draft"]["hash"],
            proposal_ref=ready["proposal"]["ref"],
            proposal_hash=ready["proposal"]["hash"],
            preview_ref=preview["ref"],
            preview_hash=preview["hash"],
            idempotency_key="confirm-before-intent-spawn-command",
        )
        assert confirmed["status"] == "dispatching"
        runner.allow_spawn.set()
        worker.join(timeout=3)
        assert not worker.is_alive()

        assert runner.spawned_refs == []
        assert runner.cancelled_refs == set()
        assert turn_ref not in runner.cancelled_refs
        settled = hc.query_quest_creation(ready["initialization_id"])
        turn = settled["intent_session"]["turns"][-1]
        assert turn["assistant_status"] == "failed"
        assert turn["reason"] == {"code": "intent_session_confirmed"}
        assert not any(
            event.event_type == "human_collaboration.intent_reply_recorded"
            for event in runtime.feed.read_after(0).events
        )
    finally:
        runner.allow_spawn.set()
        if worker is not None:
            worker.join(timeout=3)
        runtime.close()


def test_proposal_generation_has_a_single_atomic_worker_claim(tmp_path: Path) -> None:
    adapter = BlockingProposalAdapter()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "atomic-proposal-claim"),
        proposal_drafter=adapter,
        intent_drafting_provider=adapter,
        host_compute_probe=DeterministicProbe(),
    )
    hc = runtime.owners.human_collaboration
    workers: list[threading.Thread] = []
    try:
        opened = hc.create_quest({}, "atomic-proposal-open")
        probed = hc.observe_host_compute(
            opened["initialization_id"],
            ["GPU-test-1"],
            "atomic-proposal-probe",
        )
        changed = dict(probed["quest_draft"]["value"])
        changed.update(
            {
                "goal": "同一 durable generation 只能被一个 worker 领取。",
                "completion_criteria": "Provider 只执行一次。",
            }
        )
        saved = hc.revise_quest_draft(
            opened["initialization_id"],
            changed,
            probed["quest_draft"]["hash"],
            "atomic-proposal-change",
            probed["quest_draft"]["revision"],
        )
        hc.generate_question_proposal(
            saved["initialization_id"],
            saved["quest_draft"]["hash"],
            "atomic-proposal-generate",
            saved["quest_draft"]["revision"],
        )
        workers = [threading.Thread(target=hc.process_drafting_once) for _ in range(2)]
        workers[0].start()
        assert adapter.started.wait(timeout=2)
        workers[1].start()
        workers[1].join(timeout=2)
        assert not workers[1].is_alive()
        adapter.release.set()
        workers[0].join(timeout=3)
        assert not workers[0].is_alive()

        settled = hc.query_quest_creation(saved["initialization_id"])
        assert adapter.draft_calls == 1
        assert settled["proposal_generation"]["status"] == "succeeded"
    finally:
        adapter.release.set()
        for worker in workers:
            worker.join(timeout=3)
        runtime.close()


def test_same_basis_cannot_queue_two_active_proposal_generations(
    tmp_path: Path,
) -> None:
    adapter = DeterministicDraftingAdapter()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "one-active-generation"),
        proposal_drafter=adapter,
        intent_drafting_provider=adapter,
        host_compute_probe=DeterministicProbe(),
    )
    hc = runtime.owners.human_collaboration
    try:
        opened = hc.create_quest({}, "one-generation-open")
        probed = hc.observe_host_compute(
            opened["initialization_id"], ["GPU-test-1"], "one-generation-probe"
        )
        draft = dict(probed["quest_draft"]["value"])
        draft.update(
            {
                "goal": "同一 basis 只能有一个 active generation。",
                "completion_criteria": "不同幂等键不会排入重复 provider 工作。",
            }
        )
        saved = hc.revise_quest_draft(
            opened["initialization_id"],
            draft,
            probed["quest_draft"]["hash"],
            "one-generation-save",
            probed["quest_draft"]["revision"],
        )
        first = hc.generate_question_proposal(
            saved["initialization_id"],
            saved["quest_draft"]["hash"],
            "one-generation-first",
            saved["quest_draft"]["revision"],
        )
        second = hc.generate_question_proposal(
            saved["initialization_id"],
            saved["quest_draft"]["hash"],
            "one-generation-second",
            saved["quest_draft"]["revision"],
        )
        assert second["proposal_generation"]["ref"] == first[
            "proposal_generation"
        ]["ref"]
        with runtime._database.read() as connection:
            assert connection.execute(
                text(
                    "SELECT COUNT(*) FROM hc_proposal_generation_attempts WHERE "
                    "initialization_id = :initialization_id AND status IN "
                    "('queued', 'running')"
                ),
                {"initialization_id": saved["initialization_id"]},
            ).scalar_one() == 1
        assert hc.process_drafting_once()
        assert adapter.draft_calls == 1
        settled = hc.query_quest_creation(saved["initialization_id"])
        assert settled["proposal_generation"]["status"] == "succeeded"
    finally:
        runtime.close()


def test_late_generation_cannot_overwrite_a_newer_human_proposal_edit(
    tmp_path: Path,
) -> None:
    adapter = DeterministicDraftingAdapter()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "generation-human-cas"),
        proposal_drafter=adapter,
        intent_drafting_provider=adapter,
        host_compute_probe=DeterministicProbe(),
    )
    hc = runtime.owners.human_collaboration
    try:
        ready = _ready_v2(runtime, "generation-human-cas")
        queued = hc.generate_question_proposal(
            ready["initialization_id"],
            ready["quest_draft"]["hash"],
            "generation-human-regenerate",
            ready["quest_draft"]["revision"],
        )
        human_content = {
            **ready["proposal"]["content"],
            "title": "人工复核后的 Proposal 不得被迟到生成覆盖",
        }
        saved = hc.save_question_proposal(
            ready["initialization_id"],
            ready["quest_draft"]["hash"],
            human_content,
            "generation-human-save",
            ready["quest_draft"]["revision"],
            ready["proposal"]["ref"],
            ready["proposal"]["hash"],
        )

        assert hc.process_drafting_once()
        settled = hc.query_quest_creation(ready["initialization_id"])
        assert settled["proposal"]["ref"] == saved["proposal"]["ref"]
        assert settled["proposal"]["content"] == human_content
        assert settled["proposal_generation"]["ref"] == queued[
            "proposal_generation"
        ]["ref"]
        assert settled["proposal_generation"]["status"] == "failed"
        assert settled["proposal_generation"]["failure"] == {
            "code": "proposal_changed_during_generation"
        }
    finally:
        runtime.close()


def test_expired_running_generation_claim_is_released_without_a_restart(
    tmp_path: Path,
) -> None:
    adapter = DeterministicDraftingAdapter()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "expired-generation-lease"),
        proposal_drafter=adapter,
        intent_drafting_provider=adapter,
        host_compute_probe=DeterministicProbe(),
    )
    hc = runtime.owners.human_collaboration
    try:
        opened = hc.create_quest({}, "expired-lease-open")
        probed = hc.observe_host_compute(
            opened["initialization_id"], ["GPU-test-1"], "expired-lease-probe"
        )
        draft = dict(probed["quest_draft"]["value"])
        draft.update(
            {
                "goal": "瞬时落库失败不能永久遗留 running claim。",
                "completion_criteria": "过期 lease 在同一进程内可重新领取。",
            }
        )
        saved = hc.revise_quest_draft(
            opened["initialization_id"],
            draft,
            probed["quest_draft"]["hash"],
            "expired-lease-save",
            probed["quest_draft"]["revision"],
        )
        queued = hc.generate_question_proposal(
            saved["initialization_id"],
            saved["quest_draft"]["hash"],
            "expired-lease-generate",
            saved["quest_draft"]["revision"],
        )
        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE hc_proposal_generation_attempts SET status = 'running', "
                    "attempt_count = 1, started_at = 0 WHERE generation_ref = "
                    ":generation_ref"
                ),
                {"generation_ref": queued["proposal_generation"]["ref"]},
            )

        assert hc.process_drafting_once()
        settled = hc.query_quest_creation(saved["initialization_id"])
        assert settled["proposal_generation"]["status"] == "succeeded"
        assert settled["proposal_generation"]["attempt_count"] == 2
        assert adapter.draft_calls == 1
    finally:
        runtime.close()


def test_expired_proposal_claim_cannot_commit_after_replacement_claim(
    tmp_path: Path,
) -> None:
    adapter = ExpiringProposalAdapter()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "expired-proposal-fence"),
        proposal_drafter=adapter,
        intent_drafting_provider=adapter,
        host_compute_probe=DeterministicProbe(),
    )
    hc = runtime.owners.human_collaboration
    workers: list[threading.Thread] = []
    try:
        opened = hc.create_quest({}, "expired-proposal-fence-open")
        probed = hc.observe_host_compute(
            opened["initialization_id"],
            ["GPU-test-1"],
            "expired-proposal-fence-probe",
        )
        draft = {
            **probed["quest_draft"]["value"],
            "goal": "过期 Proposal claim 不得写回。",
            "completion_criteria": "只有 replacement claim 可以提交结果。",
        }
        saved = hc.revise_quest_draft(
            opened["initialization_id"],
            draft,
            probed["quest_draft"]["hash"],
            "expired-proposal-fence-save",
            probed["quest_draft"]["revision"],
        )
        queued = hc.generate_question_proposal(
            opened["initialization_id"],
            saved["quest_draft"]["hash"],
            "expired-proposal-fence-generate",
            saved["quest_draft"]["revision"],
        )

        first = threading.Thread(target=hc.process_drafting_once)
        first.start()
        workers.append(first)
        assert adapter.started[0].wait(timeout=1)
        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE hc_proposal_generation_attempts SET started_at = 0 "
                    "WHERE generation_ref = :generation_ref"
                ),
                {"generation_ref": queued["proposal_generation"]["ref"]},
            )
        second = threading.Thread(target=hc.process_drafting_once)
        second.start()
        workers.append(second)
        assert adapter.started[1].wait(timeout=1)

        adapter.release[0].set()
        first.join(timeout=2)
        interim = hc.query_quest_creation(opened["initialization_id"])
        assert interim["proposal_generation"]["status"] == "running"
        assert interim["proposal"] is None

        adapter.release[1].set()
        second.join(timeout=2)
        settled = hc.query_quest_creation(opened["initialization_id"])
        assert settled["proposal_generation"]["attempt_count"] == 2
        assert settled["proposal_generation"]["status"] == "succeeded"
        assert settled["proposal"]["content"]["title"] == "replacement-new"
    finally:
        for release in adapter.release:
            release.set()
        for worker in workers:
            worker.join(timeout=1)
        runtime.close()


def test_expired_intent_claim_cannot_commit_after_replacement_claim(
    tmp_path: Path,
) -> None:
    adapter = ExpiringIntentAdapter()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "expired-intent-fence"),
        proposal_drafter=adapter,
        intent_drafting_provider=adapter,
        host_compute_probe=DeterministicProbe(),
    )
    hc = runtime.owners.human_collaboration
    workers: list[threading.Thread] = []
    try:
        opened = hc.create_quest({}, "expired-intent-fence-open")
        queued = hc.send_intent_message(
            opened["initialization_id"],
            expected_draft_revision=opened["quest_draft"]["revision"],
            expected_draft_hash=opened["quest_draft"]["hash"],
            message="过期 Intent claim 不得写回。",
            idempotency_key="expired-intent-fence-send",
        )
        turn_ref = queued["intent_session"]["turns"][-1]["ref"]

        first = threading.Thread(target=hc.process_drafting_once)
        first.start()
        workers.append(first)
        assert adapter.started[0].wait(timeout=1)
        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE hc_intent_drafting_turns SET assistant_started_at = 0 "
                    "WHERE turn_ref = :turn_ref"
                ),
                {"turn_ref": turn_ref},
            )
        second = threading.Thread(target=hc.process_drafting_once)
        second.start()
        workers.append(second)
        assert adapter.started[1].wait(timeout=1)

        adapter.release[0].set()
        first.join(timeout=2)
        interim = hc.query_quest_creation(opened["initialization_id"])
        interim_turn = interim["intent_session"]["turns"][-1]
        assert interim_turn["assistant_status"] == "running"
        assert interim_turn["assistant_content"] is None

        adapter.release[1].set()
        second.join(timeout=2)
        settled_turn = hc.query_quest_creation(opened["initialization_id"])[
            "intent_session"
        ]["turns"][-1]
        assert settled_turn["assistant_status"] == "completed"
        assert settled_turn["assistant_content"] == "replacement-new"
    finally:
        for release in adapter.release:
            release.set()
        for worker in workers:
            worker.join(timeout=1)
        runtime.close()


def test_intent_reply_has_a_single_atomic_worker_claim(tmp_path: Path) -> None:
    adapter = BlockingIntentAdapter()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "atomic-intent-claim"),
        proposal_drafter=adapter,
        intent_drafting_provider=adapter,
        host_compute_probe=DeterministicProbe(),
    )
    hc = runtime.owners.human_collaboration
    workers: list[threading.Thread] = []
    try:
        opened = hc.create_quest({}, "atomic-intent-open")
        hc.send_intent_message(
            opened["initialization_id"],
            expected_draft_revision=opened["quest_draft"]["revision"],
            expected_draft_hash=opened["quest_draft"]["hash"],
            message="同一 turn 只能调用一次 provider。",
            idempotency_key="atomic-intent-message",
        )
        workers = [threading.Thread(target=hc.process_drafting_once) for _ in range(2)]
        workers[0].start()
        assert adapter.started.wait(timeout=2)
        workers[1].start()
        workers[1].join(timeout=2)
        assert not workers[1].is_alive()
        adapter.release.set()
        workers[0].join(timeout=3)
        assert not workers[0].is_alive()

        settled = hc.query_quest_creation(opened["initialization_id"])
        assert len(adapter.intent_requests) == 1
        assert settled["intent_session"]["turns"][0]["assistant_status"] == (
            "completed"
        )
    finally:
        adapter.release.set()
        for worker in workers:
            worker.join(timeout=3)
        runtime.close()


def test_reprobe_without_a_selected_gpu_invalidates_the_current_envelope(
    tmp_path: Path,
) -> None:
    adapter = DeterministicDraftingAdapter()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "reprobe-clears-envelope"),
        proposal_drafter=adapter,
        intent_drafting_provider=adapter,
        host_compute_probe=DeterministicProbe(),
    )
    hc = runtime.owners.human_collaboration
    try:
        ready = _ready_v2(runtime, "reprobe")
        old_envelope = ready["resource_envelope"]
        old_revision = ready["quest_draft"]["revision"]

        reprobed = hc.observe_host_compute(
            ready["initialization_id"], [], "reprobe-without-selection"
        )

        assert reprobed["compute"]["status"] == "ready"
        assert reprobed["resource_envelope"] is None
        assert reprobed["quest_draft"]["revision"] == old_revision + 1
        assert reprobed["quest_draft"]["value"]["resource_envelope_ref"] is None
        assert reprobed["quest_draft"]["value"]["resource_envelope_hash"] is None
        assert reprobed["proposal"]["status"] == "stale"
        assert reprobed["confirmation_preview"]["status"] == "stale"
        with runtime._database.read() as connection:
            assert connection.execute(
                text(
                    "SELECT envelope_hash FROM hc_resource_envelopes WHERE "
                    "envelope_ref = :envelope_ref"
                ),
                {"envelope_ref": old_envelope["ref"]},
            ).scalar_one() == old_envelope["hash"]
    finally:
        runtime.close()


def test_public_preview_command_rebuilds_a_confirmable_v2_preview(
    tmp_path: Path,
) -> None:
    adapter = DeterministicDraftingAdapter()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "public-v2-preview"),
        proposal_drafter=adapter,
        intent_drafting_provider=adapter,
        host_compute_probe=DeterministicProbe(),
    )
    hc = runtime.owners.human_collaboration
    try:
        ready = _ready_v2(runtime, "public-preview")
        old_preview_ref = ready["confirmation_preview"]["ref"]
        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "DELETE FROM hc_confirmation_preview_bindings WHERE "
                    "preview_ref = :preview_ref"
                ),
                {"preview_ref": old_preview_ref},
            )
            connection.execute(
                text(
                    "UPDATE hc_quest_initializations SET preview_ref = NULL, "
                    "preview_hash = NULL, preview_json = NULL, "
                    "preview_basis_revision = NULL, preview_basis_hash = NULL, "
                    "preview_proposal_ref = NULL, preview_proposal_hash = NULL WHERE "
                    "initialization_id = :initialization_id"
                ),
                {"initialization_id": ready["initialization_id"]},
            )

        previewed = hc.preview_confirmation(
            ready["initialization_id"],
            quest_draft_revision=ready["quest_draft"]["revision"],
            quest_draft_hash=ready["quest_draft"]["hash"],
            proposal_ref=ready["proposal"]["ref"],
            proposal_hash=ready["proposal"]["hash"],
            idempotency_key="public-preview-rebuild",
        )
        preview = previewed["confirmation_preview"]
        assert preview["status"] == "current"
        assert preview["schema_ref"] == (
            "meta-research/quest-initialization-impact-preview/v2"
        )
        confirmed = hc.confirm_quest(
            ready["initialization_id"],
            quest_draft_revision=ready["quest_draft"]["revision"],
            quest_draft_hash=ready["quest_draft"]["hash"],
            proposal_ref=ready["proposal"]["ref"],
            proposal_hash=ready["proposal"]["hash"],
            preview_ref=preview["ref"],
            preview_hash=preview["hash"],
            idempotency_key="public-preview-confirm",
        )
        assert confirmed["status"] == "dispatching"
    finally:
        runtime.close()


def test_public_draft_rejects_non_finite_nested_material_bindings(
    tmp_path: Path,
) -> None:
    adapter = DeterministicDraftingAdapter()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "finite-json"),
        proposal_drafter=adapter,
        intent_drafting_provider=adapter,
        host_compute_probe=DeterministicProbe(),
    )
    client, headers = _authenticated_client(runtime)
    try:
        with client:
            opened_response = client.post(
                "/api/v1/quest-initializations",
                headers={**headers, "Idempotency-Key": "finite-json-open"},
                json={},
            )
            assert opened_response.status_code == 201
            opened = opened_response.json()
            draft = dict(opened["quest_draft"]["value"])
            draft["literature"] = {
                **draft["literature"],
                "accepted_material_bindings": [{"score": float("nan")}],
            }
            rejected = client.put(
                f"/api/v1/quest-initializations/{opened['initialization_id']}/draft",
                headers={
                    **headers,
                    "Idempotency-Key": "finite-json-invalid",
                    "Content-Type": "application/json",
                },
                content=json.dumps(
                    {
                        "expected_draft_revision": opened["quest_draft"]["revision"],
                        "expected_draft_hash": opened["quest_draft"]["hash"],
                        "draft": draft,
                    },
                    allow_nan=True,
                ),
            )
            assert rejected.status_code == 409
            assert rejected.json()["detail"]["code"] == (
                "accepted_material_bindings_invalid"
            )
            current = client.get(
                f"/api/v1/quest-initializations/{opened['initialization_id']}"
            )
            assert current.status_code == 200
            assert "NaN" not in current.text
            current.json()
    finally:
        runtime.close()


def test_public_v2_rejects_unverifiable_material_binding_before_persisting_it(
    tmp_path: Path,
) -> None:
    adapter = DeterministicDraftingAdapter()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "material-basis-unavailable"),
        proposal_drafter=adapter,
        intent_drafting_provider=adapter,
        host_compute_probe=DeterministicProbe(),
    )
    client, headers = _authenticated_client(runtime)
    try:
        with client:
            opened = client.post(
                "/api/v1/quest-initializations",
                headers={**headers, "Idempotency-Key": "material-basis-open"},
                json={},
            ).json()
            draft = dict(opened["quest_draft"]["value"])
            draft["literature"] = {
                **draft["literature"],
                "mode": "provided_only",
                "accepted_material_bindings": [
                    {"memory_ref": "fake", "path": "/tmp/raw.pdf"}
                ],
            }

            rejected = client.put(
                f"/api/v1/quest-initializations/{opened['initialization_id']}/draft",
                headers={**headers, "Idempotency-Key": "material-basis-reject"},
                json={
                    "expected_draft_revision": opened["quest_draft"]["revision"],
                    "expected_draft_hash": opened["quest_draft"]["hash"],
                    "draft": draft,
                },
            )

            assert rejected.status_code == 409
            assert rejected.json()["detail"] == {
                "code": "accepted_material_bindings_invalid"
            }
            current = client.get(
                f"/api/v1/quest-initializations/{opened['initialization_id']}"
            ).json()
            assert current["quest_draft"]["revision"] == opened["quest_draft"][
                "revision"
            ]
            assert current["quest_draft"]["hash"] == opened["quest_draft"]["hash"]
    finally:
        runtime.close()


def test_public_v2_rejects_credentials_in_the_library_entry_url(
    tmp_path: Path,
) -> None:
    adapter = DeterministicDraftingAdapter()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "library-url-secret"),
        proposal_drafter=adapter,
        intent_drafting_provider=adapter,
        host_compute_probe=DeterministicProbe(),
    )
    client, headers = _authenticated_client(runtime)
    try:
        with client:
            opened = client.post(
                "/api/v1/quest-initializations",
                headers={**headers, "Idempotency-Key": "library-url-open"},
                json={},
            ).json()
            draft = dict(opened["quest_draft"]["value"])
            draft["literature"] = {
                **draft["literature"],
                "library_entry_url": (
                    "https://alice:secret@library.example/search?token=TOPSECRET"
                ),
            }

            rejected = client.put(
                f"/api/v1/quest-initializations/{opened['initialization_id']}/draft",
                headers={**headers, "Idempotency-Key": "library-url-reject"},
                json={
                    "expected_draft_revision": opened["quest_draft"]["revision"],
                    "expected_draft_hash": opened["quest_draft"]["hash"],
                    "draft": draft,
                },
            )

            assert rejected.status_code == 409
            assert rejected.json()["detail"]["code"] == (
                "library_entry_url_credentials_forbidden"
            )
            current = client.get(
                f"/api/v1/quest-initializations/{opened['initialization_id']}"
            ).json()
            assert current["quest_draft"]["revision"] == opened["quest_draft"][
                "revision"
            ]
            assert "TOPSECRET" not in json.dumps(current, ensure_ascii=False)
    finally:
        runtime.close()


def test_public_create_does_not_expose_the_legacy_v1_workflow(tmp_path: Path) -> None:
    adapter = DeterministicDraftingAdapter()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "no-public-v1"),
        proposal_drafter=adapter,
        intent_drafting_provider=adapter,
        host_compute_probe=DeterministicProbe(),
    )
    client, headers = _authenticated_client(runtime)
    try:
        with client:
            rejected = client.post(
                "/api/v1/quest-initializations",
                headers={**headers, "Idempotency-Key": "legacy-public-create"},
                json={
                    "goal": "绕过 v2 Resource Envelope",
                    "completion_criteria": "不应允许",
                    "key_configuration": "legacy",
                    "literature_scope": "open_access",
                    "initial_question_direction": "legacy",
                    "material_receipts": [],
                },
            )
            assert rejected.status_code == 422
            assert client.get(
                "/api/v1/quest-initializations/current"
            ).json() is None
            opened = client.post(
                "/api/v1/quest-initializations",
                headers={**headers, "Idempotency-Key": "v2-open-before-legacy-put"},
                json={},
            ).json()
            legacy_revision = client.put(
                f"/api/v1/quest-initializations/{opened['initialization_id']}/draft",
                headers={**headers, "Idempotency-Key": "legacy-public-revise"},
                json={
                    "expected_draft_revision": opened["quest_draft"]["revision"],
                    "expected_draft_hash": opened["quest_draft"]["hash"],
                    "goal": "绕过 v2 Resource Envelope",
                    "completion_criteria": "不应允许",
                    "key_configuration": "legacy",
                    "literature_scope": "open_access",
                    "initial_question_direction": "legacy",
                    "material_receipts": [],
                },
            )
            assert legacy_revision.status_code == 422
    finally:
        runtime.close()


def test_agent_runtime_owns_and_verifies_host_compute_observations(
    tmp_path: Path,
) -> None:
    adapter = DeterministicDraftingAdapter()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "agent-runtime-compute-owner"),
        proposal_drafter=adapter,
        intent_drafting_provider=adapter,
        host_compute_probe=DeterministicProbe(),
    )
    hc = runtime.owners.human_collaboration
    agent_runtime = runtime.owners.agent_runtime
    try:
        opened = hc.create_quest({}, "ar-owner-open")
        ar_revision = agent_runtime.query_snapshot().revision
        observed = hc.observe_host_compute(
            opened["initialization_id"], [], "ar-owner-observe"
        )
        snapshot_ref = observed["compute"]["snapshot_ref"]
        assert agent_runtime.query_snapshot().revision == ar_revision + 1
        record = agent_runtime.query_host_compute(snapshot_ref)
        assert record.snapshot_ref == snapshot_ref
        assert record.devices[0].uuid == "GPU-test-1"
        event_types = [event.event_type for event in runtime.feed.read_after(0).events]
        assert "agent_runtime.host_compute_observed" in event_types
        assert "human_collaboration.host_compute_observed" in event_types

        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE ar_host_capability_snapshots SET capabilities_hash = "
                    ":tampered WHERE snapshot_ref = :snapshot_ref"
                ),
                {"snapshot_ref": snapshot_ref, "tampered": "f" * 64},
            )
        with pytest.raises(OwnerConflict, match="host_compute_snapshot_invalid"):
            agent_runtime.query_host_compute(snapshot_ref)
    finally:
        runtime.close()


def test_concurrent_compute_replay_records_one_agent_runtime_observation(
    tmp_path: Path,
) -> None:
    adapter = DeterministicDraftingAdapter()
    probe = BlockingCountingProbe()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "agent-runtime-compute-idempotency"),
        proposal_drafter=adapter,
        intent_drafting_provider=adapter,
        host_compute_probe=probe,
    )
    hc = runtime.owners.human_collaboration
    agent_runtime = runtime.owners.agent_runtime
    results: list[dict[str, object]] = []
    errors: list[BaseException] = []
    try:
        opened = hc.create_quest({}, "ar-idempotent-open")
        ar_revision = agent_runtime.query_snapshot().revision

        def observe() -> None:
            try:
                results.append(
                    hc.observe_host_compute(
                        opened["initialization_id"],
                        [],
                        "ar-idempotent-observe",
                    )
                )
            except BaseException as error:
                errors.append(error)

        workers = [threading.Thread(target=observe) for _index in range(2)]
        for worker in workers:
            worker.start()
        assert probe.started.wait(timeout=1)
        time.sleep(0.1)
        probe.release.set()
        for worker in workers:
            worker.join(timeout=3)

        assert all(not worker.is_alive() for worker in workers)
        assert errors == []
        assert len(results) == 2
        assert probe.calls == 1
        assert {
            result["compute"]["snapshot_ref"] for result in results
        } == {results[0]["compute"]["snapshot_ref"]}
        assert agent_runtime.query_snapshot().revision == ar_revision + 1
        event_types = [event.event_type for event in runtime.feed.read_after(0).events]
        assert event_types.count("agent_runtime.host_compute_observed") == 1
        assert event_types.count("human_collaboration.host_compute_observed") == 1
    finally:
        probe.release.set()
        for worker in locals().get("workers", []):
            worker.join(timeout=1)
        runtime.close()


def test_owner_rejects_provider_output_that_exceeds_the_public_field_contract(
    tmp_path: Path,
) -> None:
    adapter = OversizedProposalAdapter()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "oversized-provider-output"),
        proposal_drafter=adapter,
        intent_drafting_provider=adapter,
        host_compute_probe=DeterministicProbe(),
    )
    hc = runtime.owners.human_collaboration
    try:
        opened = hc.create_quest({}, "oversized-open")
        probed = hc.observe_host_compute(
            opened["initialization_id"], ["GPU-test-1"], "oversized-probe"
        )
        draft = dict(probed["quest_draft"]["value"])
        draft.update(
            {
                "goal": "Provider 与 HTTP 使用同一字段长度合同。",
                "completion_criteria": "超长输出不会持久化为 Proposal。",
            }
        )
        saved = hc.revise_quest_draft(
            opened["initialization_id"],
            draft,
            probed["quest_draft"]["hash"],
            "oversized-save",
            probed["quest_draft"]["revision"],
        )
        hc.generate_question_proposal(
            saved["initialization_id"],
            saved["quest_draft"]["hash"],
            "oversized-generate",
            saved["quest_draft"]["revision"],
        )
        assert hc.process_drafting_once()
        rejected = hc.query_quest_creation(saved["initialization_id"])
        assert rejected["proposal"] is None
        assert rejected["proposal_generation"]["status"] == "failed"
        assert rejected["proposal_generation"]["failure"] == {
            "code": "proposal_output_invalid"
        }
    finally:
        runtime.close()


def test_owner_rejects_an_intent_reply_over_the_shared_length_limit(
    tmp_path: Path,
) -> None:
    adapter = OversizedIntentAdapter()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "oversized-intent-output"),
        proposal_drafter=adapter,
        intent_drafting_provider=adapter,
        host_compute_probe=DeterministicProbe(),
    )
    hc = runtime.owners.human_collaboration
    try:
        opened = hc.create_quest({}, "oversized-intent-open")
        hc.send_intent_message(
            opened["initialization_id"],
            expected_draft_revision=opened["quest_draft"]["revision"],
            expected_draft_hash=opened["quest_draft"]["hash"],
            message="拒绝超长回复。",
            idempotency_key="oversized-intent-message",
        )

        assert hc.process_drafting_once()
        turn = hc.query_quest_creation(opened["initialization_id"])[
            "intent_session"
        ]["turns"][0]
        assert turn["assistant_status"] == "failed"
        assert turn["assistant_content"] is None
        assert turn["reason"] == {"code": "intent_reply_invalid"}
    finally:
        runtime.close()


def test_succeeded_proposal_job_survives_preview_failure_without_provider_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = DeterministicDraftingAdapter()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "proposal-preview-crash"),
        proposal_drafter=adapter,
        intent_drafting_provider=adapter,
        host_compute_probe=DeterministicProbe(),
    )
    hc = runtime.owners.human_collaboration
    try:
        opened = hc.create_quest({}, "preview-crash-open")
        probed = hc.observe_host_compute(
            opened["initialization_id"], ["GPU-test-1"], "preview-crash-probe"
        )
        draft = dict(probed["quest_draft"]["value"])
        draft.update(
            {
                "goal": "Provider 结果与成功 job 必须原子提交。",
                "completion_criteria": "Preview 崩溃后不得再次调用 provider。",
            }
        )
        saved = hc.revise_quest_draft(
            opened["initialization_id"],
            draft,
            probed["quest_draft"]["hash"],
            "preview-crash-save",
            probed["quest_draft"]["revision"],
        )
        hc.generate_question_proposal(
            saved["initialization_id"],
            saved["quest_draft"]["hash"],
            "preview-crash-generate",
            saved["quest_draft"]["revision"],
        )
        original_preview = hc._auto_refresh_preview  # type: ignore[attr-defined]

        def crash_preview(_initialization_id: str) -> bool:
            raise RuntimeError("simulated preview crash")

        monkeypatch.setattr(hc, "_auto_refresh_preview", crash_preview)
        with pytest.raises(RuntimeError, match="simulated preview crash"):
            hc.process_drafting_once()
        after_provider = hc.query_quest_creation(saved["initialization_id"])
        assert after_provider["proposal_generation"]["status"] == "succeeded"
        assert after_provider["proposal"] is not None
        assert adapter.draft_calls == 1

        monkeypatch.setattr(hc, "_auto_refresh_preview", original_preview)
        assert hc.process_drafting_once()
        settled = hc.query_quest_creation(saved["initialization_id"])
        assert settled["status"] == "proposal_ready"
        assert adapter.draft_calls == 1
    finally:
        runtime.close()


def test_succeeded_intent_job_survives_preview_failure_without_provider_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = DeterministicDraftingAdapter()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "intent-preview-boundary"),
        proposal_drafter=adapter,
        intent_drafting_provider=adapter,
        host_compute_probe=DeterministicProbe(),
    )
    hc = runtime.owners.human_collaboration
    try:
        opened = hc.create_quest({}, "intent-preview-boundary-open")
        hc.send_intent_message(
            opened["initialization_id"],
            expected_draft_revision=opened["quest_draft"]["revision"],
            expected_draft_hash=opened["quest_draft"]["hash"],
            message="Provider receipt 形成后，本轮立即结束。",
            idempotency_key="intent-preview-boundary-message",
        )
        original_preview = hc._auto_refresh_preview  # type: ignore[attr-defined]
        monkeypatch.setattr(
            hc,
            "_auto_refresh_preview",
            lambda _initialization_id: (_ for _ in ()).throw(
                RuntimeError("preview must run on a later pass")
            ),
        )

        with pytest.raises(RuntimeError, match="preview must run on a later pass"):
            hc.process_drafting_once()
        turn = hc.query_quest_creation(opened["initialization_id"])[
            "intent_session"
        ]["turns"][0]
        assert turn["assistant_status"] == "completed"
        assert len(adapter.intent_requests) == 1
        monkeypatch.setattr(hc, "_auto_refresh_preview", original_preview)
        assert not hc.process_drafting_once()
        assert len(adapter.intent_requests) == 1
    finally:
        runtime.close()


def test_corrected_blank_draft_gpu_envelope_async_proposal_and_intent_are_durable(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "corrected-quest")
    adapter = DeterministicDraftingAdapter()
    runtime = build_production_runtime(
        data_root,
        proposal_drafter=adapter,
        intent_drafting_provider=adapter,
        host_compute_probe=DeterministicProbe(),
    )
    client, write_headers = _authenticated_client(runtime)
    with client:
        opened_response = client.post(
            "/api/v1/quest-initializations",
            headers={**write_headers, "Idempotency-Key": "open-blank"},
            json={},
        )
        assert opened_response.status_code == 201
        opened = opened_response.json()
        assert opened["status"] == "draft"
        assert opened["quest_draft"]["schema_ref"].endswith("/v2")
        assert opened["quest_draft"]["value"]["goal"] == ""
        assert opened["quest_draft"]["value"]["completion_criteria"] == ""
        assert opened["proposal"] is None
        assert opened["intent_session"]["status"] == "open"
        assert opened["intent_session"]["turns"] == []
        assert all(
            receipt["status"] == "not_attempted"
            for receipt in opened["receipts"].values()
        )
        current_response = client.get("/api/v1/quest-initializations/current")
        assert current_response.status_code == 200
        assert current_response.json()["initialization_id"] == opened[
            "initialization_id"
        ]

        probed_response = client.post(
            f"/api/v1/quest-initializations/{opened['initialization_id']}/compute-probe",
            headers={**write_headers, "Idempotency-Key": "probe-and-select"},
            json={"selected_device_uuids": ["GPU-test-1"]},
        )
        assert probed_response.status_code == 200
        probed = probed_response.json()
        assert probed["compute"]["status"] == "ready"
        assert probed["compute"]["devices"][0]["uuid"] == "GPU-test-1"
        assert probed["resource_envelope"]["selected_device_uuids"] == [
            "GPU-test-1"
        ]
        assert probed["resource_envelope"]["status"] == "current"
        assert probed["resource_envelope"]["time_budget"] == "open"
        assert probed["resource_envelope"]["hard_ceiling"] == {
            "kind": "open_ended",
            "seconds": None,
        }
        assert probed["quest_draft"]["revision"] == 2

        draft = dict(probed["quest_draft"]["value"])
        draft.update(
            {
                "goal": "判断低照度显微图像去噪能否保留稀有形态。",
                "completion_criteria": "形成带反例和证据边界的比较结论。",
                "time_budget": "30d",
                "background_and_initial_direction": "比较自监督和监督基线。",
            }
        )
        saved_response = client.put(
            f"/api/v1/quest-initializations/{opened['initialization_id']}/draft",
            headers={**write_headers, "Idempotency-Key": "autosave-v2"},
            json={
                "expected_draft_revision": probed["quest_draft"]["revision"],
                "expected_draft_hash": probed["quest_draft"]["hash"],
                "draft": draft,
            },
        )
        assert saved_response.status_code == 200
        saved = saved_response.json()
        assert saved["resource_envelope"]["ref"] != probed["resource_envelope"][
            "ref"
        ]
        assert saved["resource_envelope"]["status"] == "current"
        assert saved["resource_envelope"]["time_budget"] == "30d"
        assert saved["resource_envelope"]["hard_ceiling"] == {
            "kind": "wall_clock",
            "seconds": 2_592_000,
        }
        with runtime._database.read() as connection:
            immutable_budgets = [
                value
                for value in connection.execute(
                    text(
                        "SELECT envelope_json FROM hc_resource_envelopes WHERE "
                        "initialization_id = :initialization_id ORDER BY recorded_at"
                    ),
                    {"initialization_id": opened["initialization_id"]},
                ).scalars()
            ]
        assert '"time_budget":"open"' in immutable_budgets[0]
        assert '"time_budget":"30d"' in immutable_budgets[1]

        changed_draft = dict(saved["quest_draft"]["value"])
        changed_draft["goal"] = "临时改写后再恢复原目标。"
        changed = client.put(
            f"/api/v1/quest-initializations/{opened['initialization_id']}/draft",
            headers={**write_headers, "Idempotency-Key": "temporary-v2-change"},
            json={
                "expected_draft_revision": saved["quest_draft"]["revision"],
                "expected_draft_hash": saved["quest_draft"]["hash"],
                "draft": changed_draft,
            },
        ).json()
        reverted = client.put(
            f"/api/v1/quest-initializations/{opened['initialization_id']}/draft",
            headers={**write_headers, "Idempotency-Key": "restore-v2-content"},
            json={
                "expected_draft_revision": changed["quest_draft"]["revision"],
                "expected_draft_hash": changed["quest_draft"]["hash"],
                "draft": saved["quest_draft"]["value"],
            },
        ).json()
        assert reverted["quest_draft"]["hash"] == saved["quest_draft"]["hash"]
        delayed = client.put(
            f"/api/v1/quest-initializations/{opened['initialization_id']}/draft",
            headers={**write_headers, "Idempotency-Key": "delayed-revision-3"},
            json={
                "expected_draft_revision": saved["quest_draft"]["revision"],
                "expected_draft_hash": saved["quest_draft"]["hash"],
                "draft": saved["quest_draft"]["value"],
            },
        )
        assert delayed.status_code == 409
        assert delayed.json()["detail"]["code"] == "quest_draft_stale"
        saved = reverted

        turn_response = client.post(
            f"/api/v1/quest-initializations/{opened['initialization_id']}"
            "/intent-session/messages",
            headers={**write_headers, "Idempotency-Key": "intent-turn-1"},
            json={
                "expected_draft_revision": saved["quest_draft"]["revision"],
                "expected_draft_hash": saved["quest_draft"]["hash"],
                "message": "怎样把问题缩小？",
            },
        )
        assert turn_response.status_code == 202
        runtime.owners.human_collaboration.process_drafting_once()
        deadline = time.monotonic() + 1
        while not adapter.intent_requests and time.monotonic() < deadline:
            time.sleep(0.01)
        assert adapter.intent_requests[-1].draft["goal"] == draft["goal"]
        assert adapter.intent_requests[-1].draft["time_budget"] == "30d"
        assert adapter.intent_requests[-1].draft["resource_envelope_ref"] == saved[
            "resource_envelope"
        ]["ref"]

        generation_response = client.post(
            f"/api/v1/quest-initializations/{opened['initialization_id']}"
            "/proposal-generations",
            headers={**write_headers, "Idempotency-Key": "generate-async-1"},
            json={
                "expected_draft_revision": saved["quest_draft"]["revision"],
                "expected_draft_hash": saved["quest_draft"]["hash"],
            },
        )
        assert generation_response.status_code == 202
        assert generation_response.json()["status"] == "proposal_generating"

        ready = _poll(client, opened["initialization_id"], "proposal_ready")
        assert ready["proposal"]["content"] == QUESTION
        assert ready["confirmation_preview"]["status"] == "current"
        assert ready["confirmation_preview"]["will_happen"]
        assert ready["confirmation_preview"]["will_not_happen"]
        with runtime._database.read() as connection:
            preview_envelope_hash = connection.execute(
                text(
                    "SELECT resource_envelope_hash FROM "
                    "hc_confirmation_preview_bindings WHERE preview_ref = :preview_ref"
                ),
                {"preview_ref": ready["confirmation_preview"]["ref"]},
            ).scalar_one()
        assert preview_envelope_hash == ready["resource_envelope"]["hash"]
        assert ready["intent_session"]["turns"][0]["assistant_status"] in {
            "queued",
            "running",
            "completed",
        }

        edited_content = {
            **ready["proposal"]["content"],
            "title": "经人工编辑的稀有形态保真问题",
        }
        edited_response = client.put(
            f"/api/v1/quest-initializations/{opened['initialization_id']}/proposal",
            headers={**write_headers, "Idempotency-Key": "proposal-autosave-current"},
            json={
                "expected_draft_revision": ready["quest_draft"]["revision"],
                "expected_draft_hash": ready["quest_draft"]["hash"],
                "expected_proposal_ref": ready["proposal"]["ref"],
                "expected_proposal_hash": ready["proposal"]["hash"],
                "content": edited_content,
            },
        )
        assert edited_response.status_code == 200
        edited = edited_response.json()
        assert edited["proposal"]["content"] == edited_content
        delayed_proposal = client.put(
            f"/api/v1/quest-initializations/{opened['initialization_id']}/proposal",
            headers={**write_headers, "Idempotency-Key": "proposal-autosave-late"},
            json={
                "expected_draft_revision": ready["quest_draft"]["revision"],
                "expected_draft_hash": ready["quest_draft"]["hash"],
                "expected_proposal_ref": ready["proposal"]["ref"],
                "expected_proposal_hash": ready["proposal"]["hash"],
                "content": ready["proposal"]["content"],
            },
        )
        assert delayed_proposal.status_code == 409
        assert delayed_proposal.json()["detail"]["code"] == (
            "question_proposal_stale"
        )
        ready = edited

        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE hc_confirmation_preview_bindings SET summary_json = '{}' "
                    "WHERE preview_ref = :preview_ref"
                ),
                {"preview_ref": ready["confirmation_preview"]["ref"]},
            )
        tampered_confirmation = client.post(
            f"/api/v1/quest-initializations/{opened['initialization_id']}"
            "/confirmation",
            headers={**write_headers, "Idempotency-Key": "reject-tampered-preview"},
            json={
                "quest_draft_revision": ready["quest_draft"]["revision"],
                "quest_draft_hash": ready["quest_draft"]["hash"],
                "proposal_ref": ready["proposal"]["ref"],
                "proposal_hash": ready["proposal"]["hash"],
                "preview_ref": ready["confirmation_preview"]["ref"],
                "preview_hash": ready["confirmation_preview"]["hash"],
            },
        )
        assert tampered_confirmation.status_code == 409
        assert tampered_confirmation.json()["detail"]["code"] == (
            "confirmation_preview_stale"
        )

    runtime.close()

    restarted = build_production_runtime(
        prepare_data_root(tmp_path / "corrected-quest"),
        proposal_drafter=adapter,
        intent_drafting_provider=adapter,
        host_compute_probe=DeterministicProbe(),
    )
    assert restarted.owners.human_collaboration.process_drafting_once()
    refreshed = restarted.owners.human_collaboration.query_quest_creation(
        opened["initialization_id"]
    )
    assert refreshed["confirmation_preview"]["ref"] != ready[
        "confirmation_preview"
    ]["ref"]
    assert refreshed["confirmation_preview"]["hash"] != ready[
        "confirmation_preview"
    ]["hash"]
    restarted_client, _headers = _authenticated_client(restarted)
    with restarted_client:
        restored = _poll(
            restarted_client, opened["initialization_id"], "proposal_ready"
        )
        assert restored["resource_envelope"]["ref"] == ready["resource_envelope"]["ref"]
        assert restored["intent_session"]["turns"][0]["user_content"] == (
            "怎样把问题缩小？"
        )
        assert restored["intent_session"]["turns"][0]["assistant_status"] == (
            "completed"
        )
        assert "建议先把完成标准具体化" in restored["intent_session"]["turns"][0][
            "assistant_content"
        ]
    restarted.close()


def test_corrected_v2_confirmation_preserves_the_owner_receipt_chain(
    tmp_path: Path,
) -> None:
    adapter = DeterministicDraftingAdapter()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "corrected-confirmation"),
        proposal_drafter=adapter,
        intent_drafting_provider=adapter,
        host_compute_probe=DeterministicProbe(),
    )
    hc = runtime.owners.human_collaboration
    try:
        opened = hc.create_quest({}, "open-v2-confirm")
        probed = hc.observe_host_compute(
            opened["initialization_id"], ["GPU-test-1"], "select-v2-confirm"
        )
        draft = dict(probed["quest_draft"]["value"])
        draft.update(
            {
                "goal": "验证 corrected v2 receipt 链。",
                "completion_criteria": "六层 receipt 均可独立复核。",
                "background_and_initial_direction": "验证兼容性。",
            }
        )
        saved = hc.revise_quest_draft(
            opened["initialization_id"],
            draft,
            probed["quest_draft"]["hash"],
            "save-v2-confirm",
            probed["quest_draft"]["revision"],
        )
        hc.generate_question_proposal(
            opened["initialization_id"],
            saved["quest_draft"]["hash"],
            "generate-v2-confirm",
            saved["quest_draft"]["revision"],
        )
        assert hc.process_drafting_once()
        ready = hc.query_quest_creation(opened["initialization_id"])
        assert ready["status"] == "proposal_ready"
        confirmed = hc.confirm_quest(
            opened["initialization_id"],
            quest_draft_revision=ready["quest_draft"]["revision"],
            quest_draft_hash=ready["quest_draft"]["hash"],
            proposal_ref=ready["proposal"]["ref"],
            proposal_hash=ready["proposal"]["hash"],
            preview_ref=ready["confirmation_preview"]["ref"],
            preview_hash=ready["confirmation_preview"]["hash"],
            idempotency_key="confirm-v2",
        )
        assert confirmed["receipts"]["human_confirmation"]["status"] == "accepted"
        while confirmed["status"] != "completed":
            assert hc.reconcile_once()
            confirmed = hc.query_quest_creation(opened["initialization_id"])
        assert {
            key: value["status"] for key, value in confirmed["receipts"].items()
        } == {
            "human_confirmation": "accepted",
            "quest_goal": "accepted",
            "broad_research_authorization": "accepted",
            "question_content": "accepted",
            "question_identity": "accepted",
            "cycle_activation": "accepted",
        }
        assert confirmed["recovery"]["state"] == "completed"
        with runtime._database.read() as connection:
            checkpoint_time = connection.execute(
                text(
                    "SELECT updated_at FROM hc_reconciliation_checkpoints WHERE "
                    "initialization_id = :initialization_id"
                ),
                {"initialization_id": opened["initialization_id"]},
            ).scalar_one()
        assert not hc.reconcile_once()
        with runtime._database.read() as connection:
            assert connection.execute(
                text(
                    "SELECT updated_at FROM hc_reconciliation_checkpoints WHERE "
                    "initialization_id = :initialization_id"
                ),
                {"initialization_id": opened["initialization_id"]},
            ).scalar_one() == checkpoint_time
        assert hc.query_current_quest_creation() is None
        second = hc.create_quest({}, "open-second-after-completed")
        assert second["initialization_id"] != opened["initialization_id"]
        assert second["status"] == "draft"
    finally:
        runtime.close()
