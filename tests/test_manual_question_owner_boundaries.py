from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from meta_research.composition import build_production_runtime
from meta_research.owners.common import (
    QUESTION_PROPOSAL_SCHEMA,
    AcceptanceReceipt,
    OwnerConflict,
    canonical_hash,
)
from meta_research.owners.human_collaboration import (
    create_bundle_confirmation_verifier,
)
from meta_research.owners.research_graph import (
    create_research_graph_interface,
    create_research_graph_receipt_verifier,
)
from meta_research.owners.research_memory import (
    create_research_memory_interface,
    create_research_memory_receipt_verifier,
)
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
    "title": "压缩推理轨迹的上下文保真",
    "unknown_statement": "尚不清楚压缩推理轨迹何时会遗忘关键前文。",
    "answer_shape": "形成带反例和适用边界的比较结论。",
    "applicability_scope": "长上下文工具调用型研究任务。",
    "background_context": "显存和上下文窗口均受限。",
    "requirements_constraints": "只使用可复现实验与获准材料。",
}


class DeterministicDraftingAdapter:
    def draft(self, request: ProposalDraftRequest) -> ProposalDraftResult:
        return ProposalDraftResult(dict(QUESTION), "test_deterministic")

    def reply(self, request: IntentTurnRequest) -> IntentTurnResult:
        return IntentTurnResult(
            "先明确真正未知和答案边界。",
            request.native_session_ref or "manual-owner-test-session",
            "test_deterministic",
        )


class DeterministicProbe:
    def observe(self) -> HostComputeSnapshot:
        return HostComputeSnapshot(
            status="ready",
            observed_at=1_720_000_000.0,
            devices=(
                HostComputeDevice(
                    uuid="GPU-manual-owner-test",
                    name="Manual Owner Test GPU",
                    memory_total_mib=81_920,
                ),
            ),
            adapter_kind="test_probe",
        )


class DeterministicManualConfirmationAuthority:
    """A test HC adapter that issues and verifies exact confirmation receipts."""

    def __init__(self) -> None:
        self._confirmations: dict[str, dict[str, object]] = {}

    def confirm(
        self,
        *,
        context_ref: str,
        quest_ref: str,
        parent_question_ref: str,
        content: dict[str, object],
        basis_hash: str,
        revision: int = 1,
    ) -> tuple[str, str, str, AcceptanceReceipt]:
        normalized = {
            field: str(content[field]).strip()
            for field in (
                "title",
                "unknown_statement",
                "answer_shape",
                "applicability_scope",
                "background_context",
                "requirements_constraints",
            )
        }
        content_hash = canonical_hash(normalized)
        proposal_ref = f"manual_proposal_{context_ref}"
        proposal_binding = {
            "schema_ref": QUESTION_PROPOSAL_SCHEMA,
            "context_ref": context_ref,
            "quest_ref": quest_ref,
            "parent_question_ref": parent_question_ref,
            "basis_hash": basis_hash,
            "revision": revision,
            "content": normalized,
        }
        proposal_hash = canonical_hash(proposal_binding)
        receipt_ref = f"hc_manual_confirmation_{context_ref}"
        receipt_binding = {
            "context_ref": context_ref,
            "quest_ref": quest_ref,
            "parent_question_ref": parent_question_ref,
            "proposal_ref": proposal_ref,
            "proposal_hash": proposal_hash,
            "content_hash": content_hash,
        }
        receipt = AcceptanceReceipt(
            issuer="human_collaboration",
            kind="manual_question_proposal_confirmation",
            receipt_ref=receipt_ref,
            subject_ref=proposal_ref,
            payload_hash=canonical_hash(
                {
                    "schema_ref": "meta-research/owner-acceptance-receipt/v1",
                    "issuer": "human_collaboration",
                    "kind": "manual_question_proposal_confirmation",
                    "subject_ref": proposal_ref,
                    "bindings": receipt_binding,
                }
            ),
        )
        self._confirmations[context_ref] = {
            **receipt_binding,
            "basis_hash": basis_hash,
            "revision": revision,
            "content": normalized,
            "receipt": receipt,
        }
        return proposal_ref, proposal_hash, content_hash, receipt

    def verify_manual_question_confirmation(
        self,
        *,
        context_ref: str,
        quest_ref: str,
        parent_question_ref: str,
        proposal_ref: str,
        proposal_hash: str,
        content_hash: str,
        receipt: AcceptanceReceipt,
    ) -> None:
        accepted = self._confirmations.get(context_ref)
        if accepted is None:
            raise OwnerConflict("manual_question_confirmation_receipt_invalid")
        proposal_binding = {
            "schema_ref": QUESTION_PROPOSAL_SCHEMA,
            "context_ref": context_ref,
            "quest_ref": quest_ref,
            "parent_question_ref": parent_question_ref,
            "basis_hash": accepted["basis_hash"],
            "revision": accepted["revision"],
            "content": accepted["content"],
        }
        if (
            accepted["quest_ref"] != quest_ref
            or accepted["parent_question_ref"] != parent_question_ref
            or accepted["proposal_ref"] != proposal_ref
            or accepted["proposal_hash"] != proposal_hash
            or accepted["content_hash"] != content_hash
            or canonical_hash(accepted["content"]) != content_hash
            or canonical_hash(proposal_binding) != proposal_hash
            or accepted["receipt"] != receipt
        ):
            raise OwnerConflict("manual_question_confirmation_receipt_invalid")


def _runtime(data_root: Path):
    drafting = DeterministicDraftingAdapter()
    return build_production_runtime(
        prepare_data_root(data_root),
        proposal_drafter=drafting,
        intent_drafting_provider=drafting,
        host_compute_probe=DeterministicProbe(),
    )


def _accept_root_question(runtime) -> tuple[str, str, str]:
    human = runtime.owners.human_collaboration
    opened = human.create_quest({}, "manual-owner-prerequisite-open")
    probed = human.observe_host_compute(
        opened["initialization_id"],
        ["GPU-manual-owner-test"],
        "manual-owner-prerequisite-compute",
    )
    draft = dict(probed["quest_draft"]["value"])
    draft.update(
        {
            "goal": "判断压缩推理轨迹能否保留关键上下文。",
            "completion_criteria": "形成带反例和适用边界的比较结论。",
            "time_budget": "30d",
            "route": "direct",
            "literature": {
                "mode": "oa_only",
                "library_entry_url": "",
                "scope_exclusions": "",
                "accepted_material_bindings": [],
            },
            "background_and_initial_direction": "比较不同压缩策略。",
        }
    )
    saved = human.revise_quest_draft(
        opened["initialization_id"],
        draft,
        probed["quest_draft"]["hash"],
        "manual-owner-prerequisite-draft",
        probed["quest_draft"]["revision"],
    )
    human.generate_question_proposal(
        saved["initialization_id"],
        saved["quest_draft"]["hash"],
        "manual-owner-prerequisite-proposal",
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
        idempotency_key="manual-owner-prerequisite-preview",
    )
    human.confirm_quest(
        proposed["initialization_id"],
        quest_draft_revision=proposed["quest_draft"]["revision"],
        quest_draft_hash=proposed["quest_draft"]["hash"],
        proposal_ref=proposed["proposal"]["ref"],
        proposal_hash=proposed["proposal"]["hash"],
        preview_ref=previewed["confirmation_preview"]["ref"],
        preview_hash=previewed["confirmation_preview"]["hash"],
        idempotency_key="manual-owner-prerequisite-confirm",
    )
    for _attempt in range(8):
        if not human.reconcile_once():
            break
    completed = human.query_quest_creation(opened["initialization_id"])
    assert completed["status"] == "completed"
    return (
        str(completed["initialization_id"]),
        str(completed["quest_ref"]),
        str(completed["question_ref"]),
    )


def _manual_owner_interfaces(runtime, authority):
    bundle_confirmations = create_bundle_confirmation_verifier(
        runtime._database, runtime.owners.agent_runtime
    )
    memory_receipts = create_research_memory_receipt_verifier(
        runtime._database,
        runtime.data_root.objects,
        runtime.owners.agent_runtime,
    )
    memory = create_research_memory_interface(
        runtime._database,
        runtime.data_root.objects,
        runtime.feed,
        bundle_confirmations,
        runtime.owners.research_graph,
        memory_receipts,
        runtime.owners.agent_runtime,
        runtime.owners.research_graph,
        manual_confirmation_verifier=authority,
    )
    graph_receipts = create_research_graph_receipt_verifier(
        runtime._database,
        bundle_confirmations,
        memory,
        memory,
        manual_confirmation_verifier=authority,
    )
    graph = create_research_graph_interface(
        runtime._database,
        runtime.feed,
        bundle_confirmations,
        memory,
        memory,
        graph_receipts,
        manual_confirmation_verifier=authority,
    )
    return memory, graph


def test_manual_question_owner_chain_is_exact_idempotent_and_root_compatible(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "manual-owner-chain")
    try:
        initialization_id, quest_ref, parent_ref = _accept_root_question(runtime)
        authority = DeterministicManualConfirmationAuthority()
        memory, graph = _manual_owner_interfaces(runtime, authority)

        quest = graph.query_quest_by_ref(quest_ref)
        assert quest is not None
        assert quest.initialization_id == initialization_id
        assert quest.draft["goal"] == "判断压缩推理轨迹能否保留关键上下文。"
        quest.draft["goal"] = "调用者改写不应进入 RG"
        assert graph.query_quest_by_ref(quest_ref).draft["goal"] == (
            "判断压缩推理轨迹能否保留关键上下文。"
        )
        quest = graph.query_quest_by_ref(quest_ref)
        assert quest is not None

        parent = graph.query_question_by_ref(parent_ref)
        assert parent is not None
        assert parent.context_ref == initialization_id
        assert parent.parent_question_ref is None

        context_ref = "manual_context_owner_chain"
        proposal_ref, proposal_hash, content_hash, confirmation = authority.confirm(
            context_ref=context_ref,
            quest_ref=quest_ref,
            parent_question_ref=parent_ref,
            content=QUESTION,
            basis_hash="a" * 64,
        )
        content = memory.accept_manual_question_content(
            context_ref=context_ref,
            quest=quest,
            parent_question_ref=parent_ref,
            proposal_ref=proposal_ref,
            proposal_hash=proposal_hash,
            confirmation=confirmation,
            content=dict(QUESTION),
            content_hash=content_hash,
        )
        replayed_content = memory.accept_manual_question_content(
            context_ref=context_ref,
            quest=quest,
            parent_question_ref=parent_ref,
            proposal_ref=proposal_ref,
            proposal_hash=proposal_hash,
            confirmation=confirmation,
            content=dict(QUESTION),
            content_hash=content_hash,
        )
        assert replayed_content == content
        assert memory.query_manual_question_content(context_ref) == content
        assert memory.read_question_content(content.content_ref, content_hash) == QUESTION

        question = graph.accept_manual_question(
            context_ref=context_ref,
            quest=quest,
            parent_question=parent,
            content=content,
            confirmation=confirmation,
        )
        replayed_question = graph.accept_manual_question(
            context_ref=context_ref,
            quest=quest,
            parent_question=parent,
            content=content,
            confirmation=confirmation,
        )
        assert replayed_question == question
        assert question.context_ref == context_ref
        assert question.parent_question_ref == parent_ref
        assert question.receipt.kind == "manual_question_acceptance"
        assert graph.query_question_by_ref(question.question_ref) == question
        graph.verify_question_receipt(
            context_ref=context_ref,
            quest_ref=quest_ref,
            parent_question_ref=parent_ref,
            question_ref=question.question_ref,
            receipt=question.receipt,
        )
        assert graph.query_snapshot().facts["question_count"] == 2
        assert memory.query_snapshot().facts["formal_content_count"] == 2
    finally:
        runtime.close()


def test_manual_question_owner_chain_fails_closed_without_a_provisional_row(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "manual-owner-fail-closed")
    try:
        _initialization_id, quest_ref, parent_ref = _accept_root_question(runtime)
        authority = DeterministicManualConfirmationAuthority()
        memory, graph = _manual_owner_interfaces(runtime, authority)
        quest = graph.query_quest_by_ref(quest_ref)
        parent = graph.query_question_by_ref(parent_ref)
        assert quest is not None and parent is not None
        context_ref = "manual_context_fail_closed"
        proposal_ref, proposal_hash, content_hash, confirmation = authority.confirm(
            context_ref=context_ref,
            quest_ref=quest_ref,
            parent_question_ref=parent_ref,
            content=QUESTION,
            basis_hash="b" * 64,
        )

        with pytest.raises(
            OwnerConflict, match="manual_question_confirmation_receipt_invalid"
        ):
            memory.accept_manual_question_content(
                context_ref=context_ref,
                quest=quest,
                parent_question_ref=parent_ref,
                proposal_ref=proposal_ref,
                proposal_hash=proposal_hash,
                confirmation=replace(confirmation, payload_hash="0" * 64),
                content=dict(QUESTION),
                content_hash=content_hash,
            )
        assert memory.query_manual_question_content(context_ref) is None

        content = memory.accept_manual_question_content(
            context_ref=context_ref,
            quest=quest,
            parent_question_ref=parent_ref,
            proposal_ref=proposal_ref,
            proposal_hash=proposal_hash,
            confirmation=confirmation,
            content=dict(QUESTION),
            content_hash=content_hash,
        )
        initial_count = graph.query_snapshot().facts["question_count"]
        forged_content = replace(
            content,
            receipt=replace(content.receipt, payload_hash="1" * 64),
        )
        with pytest.raises(OwnerConflict, match="manual_question_content_receipt_invalid"):
            graph.accept_manual_question(
                context_ref=context_ref,
                quest=quest,
                parent_question=parent,
                content=forged_content,
                confirmation=confirmation,
            )
        assert graph.query_snapshot().facts["question_count"] == initial_count

        mismatched_parent = replace(parent, quest_ref="quest_other")
        with pytest.raises(OwnerConflict):
            graph.accept_manual_question(
                context_ref=context_ref,
                quest=quest,
                parent_question=mismatched_parent,
                content=content,
                confirmation=confirmation,
            )
        assert graph.query_snapshot().facts["question_count"] == initial_count
    finally:
        runtime.close()
