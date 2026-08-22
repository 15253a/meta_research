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
    canonical_json,
)
from meta_research.paths import prepare_data_root
from meta_research.quest_drafting import (
    HostComputeSnapshot,
    IntentTurnRequest,
    IntentTurnResult,
    ProposalDraftRequest,
    ProposalDraftResult,
)


_QUESTION = {
    "title": "跨 Owner receipt 的验证边界",
    "unknown_statement": "尚不明确哪些跨 Owner 验证是充分且必要的。",
    "answer_shape": "形成可复核的 lineage 与失败边界。",
    "applicability_scope": "当前 SQLite Owner Interfaces。",
    "background_context": "验证 receipt 不能由调用者伪造。",
    "requirements_constraints": "保持 Owner 权限与精确 binding。",
}


class _DeterministicDraftingAdapter:
    def draft(self, request: ProposalDraftRequest) -> ProposalDraftResult:
        return ProposalDraftResult(_QUESTION, "test_deterministic")

    def reply(self, request: IntentTurnRequest) -> IntentTurnResult:
        return IntentTurnResult("测试回复", "test-session", "test_deterministic")


class _UnavailableProbe:
    def observe(self) -> HostComputeSnapshot:
        return HostComputeSnapshot(
            status="unavailable",
            observed_at=0.0,
            devices=(),
            adapter_kind="test_probe",
            reason_code="test_unavailable",
        )


def _runtime(path: Path):
    adapter = _DeterministicDraftingAdapter()
    return build_production_runtime(
        prepare_data_root(path),
        proposal_drafter=adapter,
        intent_drafting_provider=adapter,
        host_compute_probe=_UnavailableProbe(),
    )


def _generate(hc, created: dict[str, object], key: str) -> dict[str, object]:
    hc.generate_question_proposal(
        created["initialization_id"], created["quest_draft"]["hash"], key
    )
    assert hc.process_drafting_once()
    return hc.query_quest_creation(created["initialization_id"])


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
    proposed = _generate(hc, created, f"{prefix}-proposal")
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


def test_unified_legacy_asset_tamper_fails_every_public_consumer_closed(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "legacy-asset-consumers")
    hc = runtime.owners.human_collaboration
    rm = runtime.owners.research_memory
    rg = runtime.owners.research_graph
    try:
        created = _confirm_direct_quest(runtime, "legacy-asset")
        assert hc.reconcile_once()
        assert hc.reconcile_once()
        content = rm.query_question_content(created["initialization_id"])
        assert content is not None

        owner_referenced = rm.assess_release_eligibility(
            content.content_ref,
            expected_reference_revision=rg.query_asset_reference_revision(),
            idempotency_key="legacy-asset-owner-reference",
        )
        assert owner_referenced.eligible is False
        assert owner_referenced.active_reference_refs == (
            f"rm-formal-content:{content.content_ref}",
        )
        assert owner_referenced.reason_codes == ("semantic_reference_active",)

        tampered_hash = "f" * 64
        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE rm_asset_versions SET receipt_hash = :receipt_hash "
                    "WHERE version_ref = :version_ref"
                ),
                {
                    "receipt_hash": tampered_hash,
                    "version_ref": content.content_ref,
                },
            )
            connection.execute(
                text(
                    "UPDATE rm_asset_custodies SET receipt_hash = :receipt_hash "
                    "WHERE version_ref = :version_ref"
                ),
                {
                    "receipt_hash": tampered_hash,
                    "version_ref": content.content_ref,
                },
            )

        with pytest.raises(OwnerConflict, match="asset_receipt_invalid"):
            rm.materialize_asset(content.content_ref)
        with pytest.raises(OwnerConflict, match="asset_receipt_invalid"):
            rm.handoff_asset_to_managed(
                content.content_ref,
                idempotency_key="legacy-asset-handoff",
            )
        assessment = rm.assess_release_eligibility(
            content.content_ref,
            expected_reference_revision=rg.query_asset_reference_revision(),
            idempotency_key="legacy-asset-release",
        )
        assert assessment.eligible is False
        assert assessment.reason_codes == (
            "asset_state_uncertain",
            "semantic_reference_active",
        )
    finally:
        runtime.close()


def test_each_downstream_owner_rejects_forged_upstream_receipts(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "receipt-boundary")
    hc = runtime.owners.human_collaboration
    rg = runtime.owners.research_graph
    rm = runtime.owners.research_memory
    ae = runtime.owners.advancement_engine
    try:
        created = hc.create_quest(_draft(), "owner-create")
        proposal_view = _generate(hc, created, "owner-generate")
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
        mirrored = rm.query_asset_version(content.content_ref)
        assert mirrored is not None
        assert mirrored.asset_ref == content.content_ref
        assert mirrored.version_ref == content.content_ref
        assert mirrored.source_kind == "formal_question"
        assert mirrored.content_hash == content.content_hash
        assert mirrored.receipt == content.receipt
        assert tuple(
            item.memory_ref for item in rm.query_asset_inventory()
        ) == (content.content_ref,)
        assert rm.query_snapshot().facts["object_count"] == 1
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
        question_release = rm.assess_release_eligibility(
            content.content_ref,
            expected_reference_revision=rg.query_asset_reference_revision(),
            idempotency_key="owner-question-release-check",
        )
        assert question_release.eligible is False
        assert question_release.reason_codes == ("semantic_reference_active",)
        assert question_release.active_reference_refs == (
            f"rm-formal-content:{content.content_ref}",
            f"formal-question:{question.question_ref}",
        )
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
        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE rm_asset_verification_observations SET "
                    "next_verify_at = 0 WHERE version_ref = :version_ref"
                ),
                {"version_ref": content.content_ref},
            )
        assert rm.verify_asset_inventory_once()
        assert runtime.projection.query_snapshot()["readiness"]["status"] == (
            "unavailable"
        )
    finally:
        runtime.close()


def test_research_graph_goal_custody_rejects_tampered_authoritative_json(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "rg-goal-custody")
    hc = runtime.owners.human_collaboration
    rg = runtime.owners.research_graph
    try:
        created = _confirm_direct_quest(runtime, "rg-goal-custody")
        assert hc.reconcile_once()
        initialization_id = created["initialization_id"]
        quest = rg.query_quest(initialization_id)
        assert quest is not None

        tampered_draft = {
            **_draft(),
            "goal": "绕过已接纳 draft hash 的篡改目标。",
        }
        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE rg_quests SET goal_json = :goal_json "
                    "WHERE initialization_id = :initialization_id"
                ),
                {
                    "goal_json": canonical_json(tampered_draft),
                    "initialization_id": initialization_id,
                },
            )

        with pytest.raises(OwnerConflict, match="quest_receipt_invalid"):
            rg.query_quest(initialization_id)
        with pytest.raises(OwnerConflict, match="quest_receipt_invalid"):
            rg.verify_quest_receipt(
                initialization_id=initialization_id,
                quest_ref=quest.quest_ref,
                proposal_ref=quest.proposal_ref,
                proposal_hash=quest.proposal_hash,
                confirmation_ref=quest.confirmation.receipt_ref,
                receipt=quest.receipt,
            )

        assert not hc.reconcile_once()
        assert hc.query_quest_creation(initialization_id)["receipts"]["quest_goal"] == {
            "status": "rejected",
            "reason": {"code": "quest_receipt_invalid"},
        }
    finally:
        runtime.close()


def test_research_graph_idempotent_replay_revalidates_authoritative_goal_json(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "rg-goal-replay")
    hc = runtime.owners.human_collaboration
    rg = runtime.owners.research_graph
    try:
        created = _confirm_direct_quest(runtime, "rg-goal-replay")
        assert hc.reconcile_once()
        initialization_id = created["initialization_id"]
        quest = rg.query_quest(initialization_id)
        assert quest is not None

        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE rg_quests SET goal_json = :goal_json "
                    "WHERE initialization_id = :initialization_id"
                ),
                {
                    "goal_json": canonical_json(
                        {**_draft(), "goal": "重放前被篡改的目标。"}
                    ),
                    "initialization_id": initialization_id,
                },
            )

        with pytest.raises(OwnerConflict, match="quest_receipt_invalid"):
            rg.accept_quest(
                initialization_id=initialization_id,
                draft=created["quest_draft"]["value"],
                draft_revision=quest.draft_revision,
                draft_hash=quest.draft_hash,
                proposal_ref=quest.proposal_ref,
                proposal_hash=quest.proposal_hash,
                preview_ref=quest.preview_ref,
                preview_hash=quest.preview_hash,
                confirmation=quest.confirmation,
            )
    finally:
        runtime.close()


def test_lost_rm_custody_preserves_the_accepted_empty_quest_and_blocks_downstream(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "partial-custody")
    hc = runtime.owners.human_collaboration
    try:
        created = hc.create_quest(_draft(), "partial-create")
        generated = _generate(hc, created, "partial-generate")
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
            "idea_outcome_count": 0,
            "idea_rejection_count": 0,
            "asset_role_count": 0,
            "evidence_role_count": 0,
            "source_material_role_count": 0,
            "experiment_baseline_count": 0,
            "experiment_variant_count": 0,
            "evaluation_protocol_count": 0,
            "protocol_version_count": 0,
            "evaluation_count": 0,
            "variant_run_count": 0,
            "evaluation_attempt_count": 0,
            "experiment_input_binding_count": 0,
            "experiment_asset_role_count": 0,
            "formal_measurement_count": 0,
        }
        assert runtime.owners.advancement_engine.query_snapshot().facts[
            "foreground_cycle_count"
        ] == 0
    finally:
        runtime.close()


def test_damaged_completed_quest_is_unavailable_without_reentering_active_queue(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "completed-custody-preempts-new-draft")
    hc = runtime.owners.human_collaboration
    try:
        first = _confirm_direct_quest(runtime, "completed-custody-first")
        for _step in range(4):
            assert hc.reconcile_once()
        assert hc.query_quest_creation(first["initialization_id"])["status"] == (
            "completed"
        )

        second = hc.create_quest({}, "completed-custody-second")
        assert second["initialization_id"] != first["initialization_id"]
        next(
            runtime.data_root.objects.glob("formal-question-content/*/*.json")
        ).unlink()

        damaged = hc.query_quest_creation(first["initialization_id"])
        assert damaged["status"] == "unavailable"
        assert hc.query_quest_creation(second["initialization_id"])["status"] == (
            "draft"
        )
        current = hc.query_current_quest_creation()
        assert current is not None
        assert current["initialization_id"] == second["initialization_id"]
        assert current["status"] == "draft"

        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE hc_reconciliation_checkpoints SET updated_at = 0 WHERE "
                    "initialization_id = :initialization_id"
                ),
                {"initialization_id": first["initialization_id"]},
            )
        assert not hc.reconcile_once()
        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE hc_reconciliation_checkpoints SET next_retry_at = 0 "
                    "WHERE initialization_id = :initialization_id"
                ),
                {"initialization_id": first["initialization_id"]},
            )
        assert hc.reconcile_once()
        assert hc.query_quest_creation(first["initialization_id"])["status"] == (
            "completed"
        )
        resumed = hc.query_current_quest_creation()
        assert resumed is not None
        assert resumed["initialization_id"] == second["initialization_id"]
        assert resumed["status"] == "draft"
    finally:
        runtime.close()


def test_public_receipt_projection_fails_closed_when_owner_evidence_is_tampered(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "tampered-receipts")
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
    runtime = _runtime(tmp_path / "rm-io-failure")
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
        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE hc_reconciliation_checkpoints SET next_retry_at = 0 "
                    "WHERE initialization_id = :initialization_id"
                ),
                {"initialization_id": created["initialization_id"]},
            )
        assert hc.reconcile_once()
        recovered = hc.query_quest_creation(created["initialization_id"])
        assert recovered["receipts"]["question_content"]["status"] == "accepted"
    finally:
        runtime.close()


def test_reconciliation_honors_backoff_and_counts_repeated_failures(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "reconciliation-backoff")
    hc = runtime.owners.human_collaboration
    try:
        created = _confirm_direct_quest(runtime, "backoff")
        assert hc.reconcile_once()  # RG Quest accepted.
        blocked_directory = runtime.data_root.objects / "formal-question-content"
        blocked_directory.write_text("not a directory", encoding="utf-8")

        assert not hc.reconcile_once()
        first = hc.query_quest_creation(created["initialization_id"])["recovery"]
        assert first["attempt_count"] == 1
        assert first["next_retry_at"] is not None

        assert not hc.reconcile_once()
        unchanged = hc.query_quest_creation(created["initialization_id"])["recovery"]
        assert unchanged["attempt_count"] == 1
        assert unchanged["next_retry_at"] == first["next_retry_at"]

        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE hc_reconciliation_checkpoints SET next_retry_at = 0 "
                    "WHERE initialization_id = :initialization_id"
                ),
                {"initialization_id": created["initialization_id"]},
            )
        assert not hc.reconcile_once()
        repeated = hc.query_quest_creation(created["initialization_id"])["recovery"]
        assert repeated["attempt_count"] == 2
        assert repeated["next_retry_at"] > first["next_retry_at"]
        with runtime._database.read() as connection:
            assert connection.execute(
                text(
                    "SELECT COUNT(*) FROM hc_reconciliation_attempts WHERE "
                    "initialization_id = :initialization_id AND step = "
                    "'question_content'"
                ),
                {"initialization_id": created["initialization_id"]},
            ).scalar_one() == 2
    finally:
        runtime.close()


def test_cycle_commit_is_not_publicly_complete_until_hc_completion_is_durable(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "completion-window")
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
        assert hc.query_current_quest_creation()["initialization_id"] == created[
            "initialization_id"
        ]
        resumed = hc.create_quest({}, "resume-completion-window")
        assert resumed["initialization_id"] == created["initialization_id"]
        with pytest.raises(OwnerConflict, match="quest_initialization_already_active"):
            hc.create_quest({**_draft(), "goal": "第二个 Quest"}, "second-too-soon")

        assert hc.reconcile_once()
        assert hc.query_quest_creation(created["initialization_id"])["status"] == (
            "completed"
        )
        assert hc.query_current_quest_creation() is None
        second = hc.create_quest(
            {**_draft(), "goal": "第二个 Quest"}, "second-after-complete"
        )
        assert second["status"] == "draft"

        replayed_first = hc.create_quest(_draft(), "completion-window-create")
        assert replayed_first["initialization_id"] == created["initialization_id"]
        assert replayed_first["status"] == "completed"
        with pytest.raises(OwnerConflict, match="idempotency_conflict"):
            hc.create_quest(
                {**_draft(), "goal": "第二个 Quest"},
                "completion-window-create",
            )
    finally:
        runtime.close()


def test_completed_status_fails_closed_when_research_memory_custody_is_lost(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "completed-custody-loss")
    hc = runtime.owners.human_collaboration
    try:
        created = _confirm_direct_quest(runtime, "completed-custody-loss")
        while hc.query_quest_creation(created["initialization_id"])["status"] != (
            "completed"
        ):
            assert hc.reconcile_once()
        with runtime._database.read() as connection:
            object_path = connection.execute(
                text(
                    "SELECT object_path FROM rm_formal_question_contents WHERE "
                    "initialization_id = :initialization_id"
                ),
                {"initialization_id": created["initialization_id"]},
            ).scalar_one()
        (runtime.data_root.objects / object_path).unlink()

        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE hc_reconciliation_checkpoints SET updated_at = 0 WHERE "
                    "initialization_id = :initialization_id"
                ),
                {"initialization_id": created["initialization_id"]},
            )

        broken = hc.query_quest_creation(created["initialization_id"])
        assert broken["status"] == "unavailable"
        assert hc.query_current_quest_creation() is None
        resumed = hc.create_quest({}, "start-after-damaged-completed")
        assert resumed["initialization_id"] != created["initialization_id"]
        assert resumed["status"] == "draft"
        assert broken["receipts"]["question_content"] == {
            "status": "rejected",
            "reason": {"code": "question_content_custody_unavailable"},
        }
        for layer in ("question_identity", "cycle_activation"):
            assert broken["receipts"][layer] == {
                "status": "not_attempted",
                "reason": {
                    "code": "upstream_not_accepted",
                    "upstream_step": "question_content",
                },
            }

        assert not hc.reconcile_once()
        still_broken = hc.query_quest_creation(created["initialization_id"])
        assert still_broken["status"] == "unavailable"
        assert still_broken["recovery"]["state"] == "partial"
        with runtime._database.read() as connection:
            assert connection.execute(
                text("SELECT COUNT(*) FROM hc_quest_initializations")
            ).scalar_one() == 2
    finally:
        runtime.close()
