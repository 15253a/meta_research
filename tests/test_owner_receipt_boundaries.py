from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import text

from meta_research.composition import build_production_runtime
from meta_research.owners.common import (
    AcceptanceReceipt,
    OwnerConflict,
    canonical_hash,
)
from meta_research.paths import prepare_data_root


def _draft() -> dict[str, object]:
    return {
        "goal": "证明跨 Owner receipt 不能由调用者伪造。",
        "completion_criteria": "所有下游接纳都验证真实发行方与精确 lineage。",
        "key_configuration": "真实 SQLite Owner Interfaces。",
        "literature_scope": "open_access",
        "initial_question_direction": "哪些 receipt 验证是必要的？",
        "material_receipts": [],
    }


def _confirm_direct_quest(runtime, prefix: str) -> dict[str, object]:
    hc = runtime.owners.human_collaboration
    created = hc.create_quest(_draft(), f"{prefix}-create")
    proposed = hc.generate_question_proposal(
        created["initialization_id"],
        created["quest_draft"]["hash"],
        f"{prefix}-proposal",
    )
    previewed = hc.preview_confirmation(
        created["initialization_id"],
        quest_draft_revision=created["quest_draft"]["revision"],
        quest_draft_hash=created["quest_draft"]["hash"],
        proposal_ref=proposed["proposal"]["ref"],
        proposal_hash=proposed["proposal"]["hash"],
        idempotency_key=f"{prefix}-preview",
    )
    hc.confirm_quest(
        created["initialization_id"],
        quest_draft_revision=created["quest_draft"]["revision"],
        quest_draft_hash=created["quest_draft"]["hash"],
        proposal_ref=proposed["proposal"]["ref"],
        proposal_hash=proposed["proposal"]["hash"],
        preview_ref=previewed["confirmation_preview"]["ref"],
        preview_hash=previewed["confirmation_preview"]["hash"],
        idempotency_key=f"{prefix}-confirm",
    )
    return created


def test_each_downstream_owner_rejects_forged_upstream_receipts(
    tmp_path: Path,
) -> None:
    runtime = build_production_runtime(prepare_data_root(tmp_path / "receipt-boundary"))
    hc = runtime.owners.human_collaboration
    rg = runtime.owners.research_graph
    rm = runtime.owners.research_memory
    ae = runtime.owners.advancement_engine
    try:
        created = hc.create_quest(_draft(), "owner-create")
        proposal_view = hc.generate_question_proposal(
            created["initialization_id"],
            created["quest_draft"]["hash"],
            "owner-generate",
        )
        proposal = proposal_view["proposal"]

        fake_confirmation = AcceptanceReceipt(
            issuer="human_collaboration",
            kind="quest_bundle_confirmation",
            receipt_ref="hc_confirmation_forged",
            subject_ref=created["initialization_id"],
            payload_hash="0" * 64,
        )
        with pytest.raises(OwnerConflict, match="bundle_confirmation_receipt_invalid"):
            rg.accept_quest(
                initialization_id=created["initialization_id"],
                draft=created["quest_draft"]["value"],
                draft_revision=created["quest_draft"]["revision"],
                draft_hash=created["quest_draft"]["hash"],
                proposal_ref=proposal["ref"],
                proposal_hash=proposal["hash"],
                preview_ref="hc_preview_forged",
                preview_hash="1" * 64,
                confirmation=fake_confirmation,
            )
        assert rg.query_snapshot().facts["quest_count"] == 0

        previewed = hc.preview_confirmation(
            created["initialization_id"],
            quest_draft_revision=created["quest_draft"]["revision"],
            quest_draft_hash=created["quest_draft"]["hash"],
            proposal_ref=proposal["ref"],
            proposal_hash=proposal["hash"],
            idempotency_key="owner-preview",
        )
        preview = previewed["confirmation_preview"]
        hc.confirm_quest(
            created["initialization_id"],
            quest_draft_revision=created["quest_draft"]["revision"],
            quest_draft_hash=created["quest_draft"]["hash"],
            proposal_ref=proposal["ref"],
            proposal_hash=proposal["hash"],
            preview_ref=preview["ref"],
            preview_hash=preview["hash"],
            idempotency_key="owner-confirm",
        )

        assert hc.reconcile_once()
        quest = rg.query_quest(created["initialization_id"])
        assert quest is not None
        tampered_content = {
            **proposal["content"],
            "title": "未被 Human Confirmation 绑定的替换问题",
        }
        with pytest.raises(OwnerConflict, match="question_content_proposal_mismatch"):
            rm.accept_question_content(
                initialization_id=created["initialization_id"],
                quest=quest,
                content=tampered_content,
                content_hash=canonical_hash(tampered_content),
            )
        assert rm.query_snapshot().facts["formal_content_count"] == 0
        with pytest.raises(OwnerConflict):
            rm.accept_question_content(
                initialization_id=created["initialization_id"],
                quest=replace(quest, proposal_hash="2" * 64),
                content=proposal["content"],
                content_hash=canonical_hash(proposal["content"]),
            )
        assert rm.query_snapshot().facts["formal_content_count"] == 0

        assert hc.reconcile_once()
        content = rm.query_question_content(created["initialization_id"])
        assert content is not None
        forged_rm_receipt = replace(content.receipt, issuer="human_collaboration")
        with pytest.raises(OwnerConflict, match="question_content_receipt_issuer_invalid"):
            rg.accept_root_question(
                initialization_id=created["initialization_id"],
                quest=quest,
                content_ref=content.content_ref,
                content_hash=content.content_hash,
                schema_ref=content.schema_ref,
                content_receipt=forged_rm_receipt,
            )
        assert rg.query_snapshot().facts["question_count"] == 0

        assert hc.reconcile_once()
        question = rg.query_question(created["initialization_id"])
        assert question is not None
        forged_rg_receipt = replace(question.receipt, issuer="research_memory")
        with pytest.raises(OwnerConflict, match="root_question_receipt_issuer_invalid"):
            ae.activate_initial_cycle(
                initialization_id=created["initialization_id"],
                quest=quest,
                question=replace(question, receipt=forged_rg_receipt),
            )
        assert ae.query_snapshot().facts["foreground_cycle_count"] == 0

        stored_object = next(
            runtime.data_root.objects.glob("formal-question-content/*/*.json")
        )
        stored_object.unlink()
        assert rm.query_snapshot().status == "unavailable"
        with pytest.raises(
            OwnerConflict, match="question_content_custody_unavailable"
        ):
            rm.query_question_content(created["initialization_id"])
        with pytest.raises(
            OwnerConflict, match="question_content_custody_unavailable"
        ):
            ae.activate_initial_cycle(
                initialization_id=created["initialization_id"],
                quest=quest,
                question=question,
            )
        assert ae.query_snapshot().facts["foreground_cycle_count"] == 0
        assert runtime.projection.query_snapshot()["readiness"]["status"] == (
            "unavailable"
        )
    finally:
        runtime.close()


def test_lost_rm_custody_preserves_the_accepted_empty_quest_and_blocks_downstream(
    tmp_path: Path,
) -> None:
    runtime = build_production_runtime(prepare_data_root(tmp_path / "partial-custody"))
    hc = runtime.owners.human_collaboration
    try:
        created = hc.create_quest(_draft(), "partial-create")
        generated = hc.generate_question_proposal(
            created["initialization_id"],
            created["quest_draft"]["hash"],
            "partial-generate",
        )
        proposal = generated["proposal"]
        previewed = hc.preview_confirmation(
            created["initialization_id"],
            quest_draft_revision=created["quest_draft"]["revision"],
            quest_draft_hash=created["quest_draft"]["hash"],
            proposal_ref=proposal["ref"],
            proposal_hash=proposal["hash"],
            idempotency_key="partial-preview",
        )
        preview = previewed["confirmation_preview"]
        hc.confirm_quest(
            created["initialization_id"],
            quest_draft_revision=created["quest_draft"]["revision"],
            quest_draft_hash=created["quest_draft"]["hash"],
            proposal_ref=proposal["ref"],
            proposal_hash=proposal["hash"],
            preview_ref=preview["ref"],
            preview_hash=preview["hash"],
            idempotency_key="partial-confirm",
        )
        assert hc.reconcile_once()  # RG Quest
        assert hc.reconcile_once()  # RM content

        next(runtime.data_root.objects.glob("formal-question-content/*/*.json")).unlink()
        assert not hc.reconcile_once()
        view = hc.query_quest_creation(created["initialization_id"])

        assert view["canonical_empty_advancement"] is True
        assert view["receipts"]["quest_goal"]["status"] == "accepted"
        assert view["receipts"]["question_content"] == {
            "status": "rejected",
            "reason": {"code": "question_content_custody_unavailable"},
        }
        assert view["receipts"]["question_identity"] == {
            "status": "not_attempted",
            "reason": {
                "code": "upstream_not_accepted",
                "upstream_step": "question_content",
            },
        }
        assert view["receipts"]["cycle_activation"] == {
            "status": "not_attempted",
            "reason": {
                "code": "upstream_not_accepted",
                "upstream_step": "question_content",
            },
        }
        assert runtime.owners.research_graph.query_snapshot().facts == {
            "quest_count": 1,
            "question_count": 0,
        }
        assert runtime.owners.advancement_engine.query_snapshot().facts[
            "foreground_cycle_count"
        ] == 0
    finally:
        runtime.close()


def test_public_receipt_projection_fails_closed_when_owner_evidence_is_tampered(
    tmp_path: Path,
) -> None:
    runtime = build_production_runtime(prepare_data_root(tmp_path / "tampered-receipts"))
    hc = runtime.owners.human_collaboration
    try:
        created = _confirm_direct_quest(runtime, "tamper")
        for _step in range(4):
            assert hc.reconcile_once()
        initialization_id = created["initialization_id"]
        cases = (
            (
                "hc_quest_initializations",
                "confirmation_hash",
                "human_confirmation",
                "bundle_confirmation_receipt_invalid",
            ),
            ("rg_quests", "receipt_hash", "quest_goal", "quest_receipt_invalid"),
            (
                "rm_formal_question_contents",
                "receipt_hash",
                "question_content",
                "question_content_receipt_invalid",
            ),
            (
                "rg_questions",
                "receipt_hash",
                "question_identity",
                "root_question_receipt_invalid",
            ),
            (
                "ae_initial_cycles",
                "receipt_hash",
                "cycle_activation",
                "cycle_receipt_invalid",
            ),
        )
        for table, column, layer, reason_code in cases:
            with runtime._database.read() as connection:
                original = connection.execute(
                    text(
                        f"SELECT {column} FROM {table} "
                        "WHERE initialization_id = :initialization_id"
                    ),
                    {"initialization_id": initialization_id},
                ).scalar_one()
            with runtime._database.write() as connection:
                connection.execute(
                    text(
                        f"UPDATE {table} SET {column} = :tampered "
                        "WHERE initialization_id = :initialization_id"
                    ),
                    {"tampered": "f" * 64, "initialization_id": initialization_id},
                )
            view = hc.query_quest_creation(initialization_id)
            assert view["receipts"][layer] == {
                "status": "rejected",
                "reason": {"code": reason_code},
            }
            ordered_layers = (
                "human_confirmation",
                "quest_goal",
                "question_content",
                "question_identity",
                "cycle_activation",
            )
            for downstream in ordered_layers[ordered_layers.index(layer) + 1 :]:
                assert view["receipts"][downstream]["status"] != "accepted"
            with runtime._database.write() as connection:
                connection.execute(
                    text(
                        f"UPDATE {table} SET {column} = :original "
                        "WHERE initialization_id = :initialization_id"
                    ),
                    {"original": original, "initialization_id": initialization_id},
                )
    finally:
        runtime.close()


def test_native_rm_io_failure_is_durable_and_recovers_from_the_same_layer(
    tmp_path: Path,
) -> None:
    runtime = build_production_runtime(prepare_data_root(tmp_path / "rm-io-failure"))
    hc = runtime.owners.human_collaboration
    try:
        created = _confirm_direct_quest(runtime, "rm-io")
        assert hc.reconcile_once()  # RG Quest accepted.
        blocked_directory = runtime.data_root.objects / "formal-question-content"
        blocked_directory.write_text("not a directory", encoding="utf-8")

        assert not hc.reconcile_once()
        view = hc.query_quest_creation(created["initialization_id"])
        assert view["receipts"]["question_content"] == {
            "status": "rejected",
            "reason": {"code": "question_content_custody_unavailable"},
        }
        assert view["receipts"]["question_identity"]["reason"] == {
            "code": "upstream_not_accepted",
            "upstream_step": "question_content",
        }

        blocked_directory.unlink()
        assert hc.reconcile_once()
        recovered = hc.query_quest_creation(created["initialization_id"])
        assert recovered["receipts"]["question_content"]["status"] == "accepted"
    finally:
        runtime.close()


def test_cycle_commit_is_not_publicly_complete_until_hc_completion_is_durable(
    tmp_path: Path,
) -> None:
    runtime = build_production_runtime(prepare_data_root(tmp_path / "completion-window"))
    hc = runtime.owners.human_collaboration
    try:
        created = _confirm_direct_quest(runtime, "completion-window")
        assert hc.reconcile_once()  # RG Quest
        assert hc.reconcile_once()  # RM content
        assert hc.reconcile_once()  # RG Question
        quest = runtime.owners.research_graph.query_quest(created["initialization_id"])
        question = runtime.owners.research_graph.query_question(
            created["initialization_id"]
        )
        assert quest is not None and question is not None
        runtime.owners.advancement_engine.activate_initial_cycle(
            initialization_id=created["initialization_id"],
            quest=quest,
            question=question,
        )

        interrupted = hc.query_quest_creation(created["initialization_id"])
        assert interrupted["status"] == "dispatching"
        with pytest.raises(OwnerConflict, match="quest_initialization_already_active"):
            hc.create_quest({**_draft(), "goal": "第二个 Quest"}, "second-too-soon")

        assert hc.reconcile_once()
        assert hc.query_quest_creation(created["initialization_id"])["status"] == (
            "completed"
        )
        second = hc.create_quest(
            {**_draft(), "goal": "第二个 Quest"}, "second-after-complete"
        )
        assert second["status"] == "draft"
    finally:
        runtime.close()
