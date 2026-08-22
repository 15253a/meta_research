from __future__ import annotations

from pathlib import Path

import pytest

from meta_research.composition import build_production_runtime
from meta_research.owners.common import OwnerConflict
from meta_research.paths import prepare_data_root
from meta_research.quest_drafting import (
    HostComputeDevice,
    HostComputeSnapshot,
    IntentTurnRequest,
    IntentTurnResult,
    ProposalDraftRequest,
    ProposalDraftResult,
)


QUESTION = {
    "title": "低照度显微图像中的稀有形态保真",
    "unknown_statement": "尚不明确哪种自监督去噪条件能保留稀有形态。",
    "answer_shape": "形成带反例和证据边界的比较结论。",
    "applicability_scope": "低照度荧光显微公开数据。",
    "background_context": "研究稀有细胞形态。",
    "requirements_constraints": "两周内，使用获准 GPU。",
}


class DeterministicDraftingAdapter:
    def draft(self, request: ProposalDraftRequest) -> ProposalDraftResult:
        return ProposalDraftResult(dict(QUESTION), "test_deterministic")

    def reply(self, request: IntentTurnRequest) -> IntentTurnResult:
        return IntentTurnResult(
            "先明确真正未知和答案边界。",
            request.native_session_ref or "manual-drafting-session",
            "test_deterministic",
        )


class DeterministicProbe:
    def observe(self) -> HostComputeSnapshot:
        return HostComputeSnapshot(
            status="ready",
            observed_at=1_720_000_000.0,
            devices=(
                HostComputeDevice(
                    uuid="GPU-manual-test",
                    name="Manual Test GPU",
                    memory_total_mib=81_920,
                ),
            ),
            adapter_kind="test_probe",
        )


def build_runtime(data_root: Path):
    drafting = DeterministicDraftingAdapter()
    return build_production_runtime(
        prepare_data_root(data_root),
        proposal_drafter=drafting,
        intent_drafting_provider=drafting,
        host_compute_probe=DeterministicProbe(),
    )


def accept_root_question(runtime) -> tuple[str, str, str]:
    human = runtime.owners.human_collaboration
    opened = human.create_quest({}, "manual-prerequisite-open")
    probed = human.observe_host_compute(
        opened["initialization_id"],
        ["GPU-manual-test"],
        "manual-prerequisite-compute",
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
        "manual-prerequisite-draft",
        probed["quest_draft"]["revision"],
    )
    human.generate_question_proposal(
        saved["initialization_id"],
        saved["quest_draft"]["hash"],
        "manual-prerequisite-proposal",
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
        idempotency_key="manual-prerequisite-preview",
    )
    human.confirm_quest(
        proposed["initialization_id"],
        quest_draft_revision=proposed["quest_draft"]["revision"],
        quest_draft_hash=proposed["quest_draft"]["hash"],
        proposal_ref=proposed["proposal"]["ref"],
        proposal_hash=proposed["proposal"]["hash"],
        preview_ref=previewed["confirmation_preview"]["ref"],
        preview_hash=previewed["confirmation_preview"]["hash"],
        idempotency_key="manual-prerequisite-confirm",
    )
    for _attempt in range(5):
        if not human.reconcile_once():
            break
    completed = human.query_quest_creation(opened["initialization_id"])
    assert completed["status"] == "completed"
    return (
        str(completed["initialization_id"]),
        str(completed["quest_ref"]),
        str(completed["question_ref"]),
    )


def test_manual_creation_confirms_an_immutable_user_seed_in_its_own_context(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "manual-seed"
    runtime = build_runtime(data_root)
    try:
        initialization_id, quest_ref, parent_question_ref = accept_root_question(
            runtime
        )
        human = runtime.owners.human_collaboration

        opened = human.open_manual_question_creation(
            quest_ref=quest_ref,
            parent_question_ref=parent_question_ref,
            idempotency_key="manual-open",
        )
        assert opened["schema_ref"] == (
            "meta-research/manual-question-creation/v1"
        )
        assert opened["context_ref"] != initialization_id
        assert opened["creation_mode"] == "ManualCreation"
        assert opened["status"] == "draft"
        assert opened["seed"] is None
        assert opened["proposal"] is None
        assert opened["question_anchor"] is None
        assert human.query_current_quest_creation() is None
        assert human.query_current_manual_question_creation(
            quest_ref=quest_ref,
            parent_question_ref=parent_question_ref,
        )["context_ref"] == opened["context_ref"]

        seed = {
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
            "deepfetch_preference": "later",
        }
        confirmed = human.confirm_manual_creation_seed(
            opened["context_ref"],
            seed=seed,
            idempotency_key="manual-seed-confirm",
        )
        assert confirmed["status"] == "seed_confirmed"
        assert confirmed["seed"]["value"] == seed
        assert confirmed["seed"]["receipt"] == {
            "status": "accepted",
            "issuer": "human_collaboration",
            "kind": "manual_creation_seed_confirmation",
            "receipt_ref": confirmed["seed"]["receipt"]["receipt_ref"],
            "subject_ref": confirmed["seed"]["ref"],
            "payload_hash": confirmed["seed"]["receipt"]["payload_hash"],
        }
        assert confirmed["research_path"] == {
            "status": "not_selected",
            "deepfetch": None,
            "waiver": None,
        }
        assert confirmed["proposal"] is None
        assert confirmed["receipts"]["content"] == {"status": "not_attempted"}
        assert confirmed["receipts"]["question"] == {"status": "not_attempted"}

        seed["intent"] = "调用者后改写，不得影响已确认 Seed"
        with pytest.raises(OwnerConflict, match="manual_creation_seed_immutable"):
            human.confirm_manual_creation_seed(
                opened["context_ref"],
                seed={
                    **confirmed["seed"]["value"],
                    "intent": "另一个 Seed",
                },
                idempotency_key="manual-seed-conflict",
            )
    finally:
        runtime.close()

    restarted = build_runtime(data_root)
    try:
        restored = (
            restarted.owners.human_collaboration.query_manual_question_creation(
                opened["context_ref"]
            )
        )
        assert restored["status"] == "seed_confirmed"
        assert restored["seed"]["value"]["intent"] == (
            "我想知道压缩推理轨迹是否会遗忘前面的关键信息。"
        )
        assert restored["seed"]["receipt"] == confirmed["seed"]["receipt"]
        assert restored["question_anchor"] is None
    finally:
        restarted.close()
