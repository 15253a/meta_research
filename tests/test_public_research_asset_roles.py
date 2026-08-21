from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import text

from meta_research.composition import build_production_runtime
from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.owners.research_memory import AssetIntakeRequest
from meta_research.paths import prepare_data_root
from meta_research.quest_drafting import (
    HostComputeDevice,
    HostComputeSnapshot,
    IntentTurnRequest,
    IntentTurnResult,
    ProposalDraftRequest,
    ProposalDraftResult,
)


_QUESTION = {
    "title": "证据角色与资产保管分离",
    "unknown_statement": "尚不明确精确资产版本能否安全进入 ContextPack。",
    "answer_shape": "形成带可核验 Evidence 引用的结论。",
    "applicability_scope": "当前本地 Quest。",
    "background_context": "验证 RG 只拥有语义角色。",
    "requirements_constraints": "不得把角色写回 RM。",
}


class _Drafting:
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


class _ReadyProbe:
    def observe(self) -> HostComputeSnapshot:
        return HostComputeSnapshot(
            status="ready",
            observed_at=1720000000.0,
            devices=(
                HostComputeDevice(
                    uuid="GPU-material-test",
                    name="Material Test GPU",
                    memory_total_mib=24576,
                ),
            ),
            adapter_kind="test_probe",
        )


def _runtime(path: Path, probe=None):
    drafting = _Drafting()
    return build_production_runtime(
        prepare_data_root(path),
        proposal_drafter=drafting,
        intent_drafting_provider=drafting,
        host_compute_probe=probe or _UnavailableProbe(),
    )


def _accepted_quest(runtime):
    human = runtime.owners.human_collaboration
    created = human.create_quest(
        {
            "goal": "验证精确 Evidence 版本。",
            "completion_criteria": "RG 能验证当前 RM binding。",
            "key_configuration": "真实 SQLite Owner Interfaces。",
            "literature_scope": "open_access",
            "initial_question_direction": "哪些版本可作为证据？",
            "material_receipts": [],
        },
        "role-create",
    )
    human.generate_question_proposal(
        created["initialization_id"],
        created["quest_draft"]["hash"],
        "role-proposal",
    )
    assert human.process_drafting_once()
    proposed = human.query_quest_creation(created["initialization_id"])
    previewed = human.preview_confirmation(
        created["initialization_id"],
        quest_draft_revision=proposed["quest_draft"]["revision"],
        quest_draft_hash=proposed["quest_draft"]["hash"],
        proposal_ref=proposed["proposal"]["ref"],
        proposal_hash=proposed["proposal"]["hash"],
        idempotency_key="role-preview",
    )
    human.confirm_quest(
        created["initialization_id"],
        quest_draft_revision=proposed["quest_draft"]["revision"],
        quest_draft_hash=proposed["quest_draft"]["hash"],
        proposal_ref=proposed["proposal"]["ref"],
        proposal_hash=proposed["proposal"]["hash"],
        preview_ref=previewed["confirmation_preview"]["ref"],
        preview_hash=previewed["confirmation_preview"]["hash"],
        idempotency_key="role-confirm",
    )
    assert human.reconcile_once()
    quest = runtime.owners.research_graph.query_quest(created["initialization_id"])
    assert quest is not None
    return quest


def test_rg_accepts_precise_asset_roles_and_revalidates_current_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "evidence.csv"
    source.write_bytes(b"group,score\ncontrol,0.82\n")
    runtime = _runtime(tmp_path / "asset-roles")
    try:
        quest = _accepted_quest(runtime)
        intake = runtime.owners.research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="local_path",
                custody_mode="linked_local",
                display_name=source.name,
                media_type="text/csv",
                source_locator=str(source),
            ),
            idempotency_key="role-asset",
        )
        assert intake.asset is not None
        binding = intake.asset.as_binding()
        reference_revision_before_role = (
            runtime.owners.research_graph.query_asset_reference_revision()
        )

        evidence = runtime.owners.research_graph.accept_asset_role(
            binding=binding,
            role="evidence",
            quest_ref=quest.quest_ref,
            idempotency_key="role-evidence",
        )
        replay = runtime.owners.research_graph.accept_asset_role(
            binding=binding,
            role="evidence",
            quest_ref=quest.quest_ref,
            idempotency_key="role-evidence",
        )
        semantic_replay = runtime.owners.research_graph.accept_asset_role(
            binding=binding,
            role="evidence",
            quest_ref=quest.quest_ref,
            idempotency_key="role-evidence-alias",
        )

        assert replay == evidence
        assert semantic_replay == evidence
        with pytest.raises(OwnerConflict, match="asset_role_idempotency_conflict"):
            runtime.owners.research_graph.accept_asset_role(
                binding=binding,
                role="quest_source_material",
                quest_ref=quest.quest_ref,
                idempotency_key="role-evidence-alias",
            )
        assert evidence.role == "evidence"
        assert evidence.version_ref == binding.version_ref
        assert evidence.receipt.issuer == "research_graph"
        assert runtime.owners.research_graph.query_evidence_refs(
            quest.quest_ref
        ) == (binding.version_ref,)
        assert runtime.owners.research_graph.query_asset_roles(
            quest_ref=quest.quest_ref
        ) == (evidence,)
        stale_assessment = (
            runtime.owners.research_memory.assess_release_eligibility(
                binding.version_ref,
                expected_reference_revision=reference_revision_before_role,
                idempotency_key="role-release-stale",
            )
        )
        assert stale_assessment.eligible is False
        assert stale_assessment.reason_codes == ("reference_revision_stale",)
        referenced = runtime.owners.research_memory.assess_release_eligibility(
            binding.version_ref,
            expected_reference_revision=(
                runtime.owners.research_graph.query_asset_reference_revision()
            ),
            idempotency_key="role-release-referenced",
        )
        assert referenced.eligible is False
        assert referenced.reason_codes == ("semantic_reference_active",)
        assert referenced.active_reference_refs == (
            f"asset-role:{evidence.role_ref}",
        )

        human = runtime.owners.human_collaboration
        for _step in range(4):
            if not human.reconcile_once():
                break
        completed = human.query_quest_creation(quest.initialization_id)
        assert completed["status"] == "completed"
        accepted_question = runtime.owners.research_graph.query_question(
            quest.initialization_id
        )
        assert accepted_question is not None
        with pytest.raises(OwnerConflict, match="idea_context_pack_stale"):
            runtime.owners.advancement_engine.ensure_idea_stage_request(
                cycle_ref=completed["cycle_ref"],
                accepted_question=accepted_question.as_binding(),
                context_pack={
                    "schema_ref": "meta-research/idea-context-pack/v2",
                    "cycle_ref": completed["cycle_ref"],
                    "accepted_question_binding": (
                        accepted_question.as_binding().as_dict()
                    ),
                    "accepted_evidence_refs": [],
                    "evidence_reference_revision": (
                        runtime.owners.research_graph
                        .query_evidence_state(quest.quest_ref)[0]
                    ),
                    "literature_binding": None,
                    "prior_accepted_bindings": [],
                    "active_guidance_bindings": [],
                },
                idempotency_key="role-idea-omit-evidence",
            )
        evidence_checks: list[str] = []
        verifier = runtime.owners.advancement_engine._evidence_verifier
        assert verifier is not None
        original_verify = verifier.verify_evidence_refs
        original_assert = verifier.assert_evidence_state

        def verify_without_writer(**kwargs) -> None:
            assert not runtime._database._write_lock._is_owned()
            evidence_checks.append("deep")
            original_verify(**kwargs)

        def assert_with_writer(**kwargs) -> None:
            assert runtime._database._write_lock._is_owned()
            evidence_checks.append("cas")
            original_assert(**kwargs)

        monkeypatch.setattr(verifier, "verify_evidence_refs", verify_without_writer)
        monkeypatch.setattr(verifier, "assert_evidence_state", assert_with_writer)
        runtime.idea_stage.start("role-idea-start")
        stage_request = runtime.owners.advancement_engine.query_idea_stage_request(
            completed["cycle_ref"]
        )
        assert stage_request is not None
        assert stage_request.context_pack["accepted_evidence_refs"] == [
            binding.version_ref
        ]
        assert stage_request.context_pack["schema_ref"] == (
            "meta-research/idea-context-pack/v2"
        )
        assert stage_request.context_pack["evidence_reference_revision"] == (
            runtime.owners.research_graph.query_evidence_state(quest.quest_ref)[0]
        )
        assert "deep" in evidence_checks
        assert "cas" in evidence_checks
        later_asset = runtime.owners.research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="text",
                custody_mode="managed",
                display_name="later-evidence.txt",
                content=b"accepted after the StageRunRequest was frozen\n",
            ),
            idempotency_key="role-later-evidence-asset",
        )
        assert later_asset.asset is not None
        later_evidence = runtime.owners.research_graph.accept_asset_role(
            binding=later_asset.asset.as_binding(),
            role="evidence",
            quest_ref=quest.quest_ref,
            idempotency_key="role-later-evidence",
        )
        original_request_key = (
            "idea-request:"
            + canonical_hash([completed["cycle_ref"], "role-idea-start"])
        )
        assert runtime.owners.advancement_engine.ensure_idea_stage_request(
            cycle_ref=completed["cycle_ref"],
            accepted_question=accepted_question.as_binding(),
            context_pack=stage_request.context_pack,
            idempotency_key=original_request_key,
        ) == stage_request
        assert runtime.owners.advancement_engine.ensure_idea_stage_request(
            cycle_ref=completed["cycle_ref"],
            accepted_question=accepted_question.as_binding(),
            context_pack=stage_request.context_pack,
            idempotency_key="role-idea-historical-alias",
        ) == stage_request

        with pytest.raises(OwnerConflict, match="asset_receipt_invalid"):
            runtime.owners.research_graph.accept_asset_role(
                binding=replace(binding, content_hash="f" * 64),
                role="quest_source_material",
                quest_ref=quest.quest_ref,
                idempotency_key="role-forged",
            )

        source.write_bytes(b"group,score\ncontrol,0.12\n")
        assert runtime.owners.research_graph.query_asset_roles(
            quest_ref=quest.quest_ref
        ) == (evidence, later_evidence)
        with pytest.raises(OwnerConflict, match="asset_custody_unavailable"):
            runtime.owners.research_graph.query_evidence_refs(quest.quest_ref)
    finally:
        runtime.close()


def test_confirmed_quest_material_binding_becomes_a_separate_rg_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path / "quest-source-material", _ReadyProbe())
    human = runtime.owners.human_collaboration
    source = tmp_path / "provided-methods.md"
    source.write_bytes(b"# Provided method\nExact accepted bytes.\n")
    try:
        intake = runtime.owners.research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="local_path",
                custody_mode="linked_local",
                display_name=source.name,
                media_type="text/markdown",
                source_locator=str(source.resolve()),
            ),
            idempotency_key="quest-material-asset",
        )
        assert intake.asset is not None
        binding = intake.asset.as_binding()
        opened = human.create_quest({}, "quest-material-open")
        observed = human.observe_host_compute(
            opened["initialization_id"],
            ["GPU-material-test"],
            "quest-material-compute",
        )
        draft = dict(observed["quest_draft"]["value"])
        draft.update(
            {
                "goal": "只使用已接纳材料形成可核验问题。",
                "completion_criteria": "Quest Source Material 有独立 RG receipt。",
                "time_budget": "30d",
                "route": "direct",
                "literature": {
                    "mode": "provided_only",
                    "library_entry_url": "",
                    "scope_exclusions": "",
                    "accepted_material_bindings": [binding.as_dict()],
                },
                "background_and_initial_direction": "验证材料角色边界。",
            }
        )
        forged = dict(draft)
        forged["literature"] = {
            **draft["literature"],
            "accepted_material_bindings": [
                replace(binding, content_hash="f" * 64).as_dict()
            ],
        }
        with pytest.raises(OwnerConflict, match="asset_receipt_invalid"):
            human.revise_quest_draft(
                opened["initialization_id"],
                forged,
                observed["quest_draft"]["hash"],
                "quest-material-forged",
                observed["quest_draft"]["revision"],
            )

        revised = human.revise_quest_draft(
            opened["initialization_id"],
            draft,
            observed["quest_draft"]["hash"],
            "quest-material-draft",
            observed["quest_draft"]["revision"],
        )
        human.generate_question_proposal(
            opened["initialization_id"],
            revised["quest_draft"]["hash"],
            "quest-material-proposal",
            revised["quest_draft"]["revision"],
        )
        assert human.process_drafting_once()
        proposed = human.query_quest_creation(opened["initialization_id"])
        previewed = human.preview_confirmation(
            opened["initialization_id"],
            quest_draft_revision=proposed["quest_draft"]["revision"],
            quest_draft_hash=proposed["quest_draft"]["hash"],
            proposal_ref=proposed["proposal"]["ref"],
            proposal_hash=proposed["proposal"]["hash"],
            idempotency_key="quest-material-preview",
        )
        material_assertions = [
            assertion
            for assertion in previewed["confirmation_preview"]["target_assertions"]
            if assertion["operation"] == "accept_asset_roles"
        ]
        assert len(material_assertions) == 1
        material_assertion = material_assertions[0]
        assert material_assertion["owner"] == "research_graph"
        assert material_assertion["bindings"] == {
            "initialization_id": opened["initialization_id"],
            "role": "quest_source_material",
            "assets": [binding.as_dict()],
        }
        assert material_assertion["target_hash"] == canonical_hash(
            {
                key: value
                for key, value in material_assertion.items()
                if key != "target_hash"
            }
        )
        assert any(
            "1 个精确 Quest Source Material 角色" in item
            for item in previewed["confirmation_preview"]["will_happen"]
        )
        original_projection_verifier = (
            runtime.owners.research_memory.verify_asset_projection_binding
        )
        healthy_projection_checks: list[str] = []
        with monkeypatch.context() as query_patch:
            query_patch.setattr(
                runtime.owners.research_memory,
                "verify_asset_binding",
                lambda **_values: (_ for _ in ()).throw(
                    AssertionError("public Query must not deep-hash material bytes")
                ),
            )
            query_patch.setattr(
                runtime.owners.research_memory,
                "verify_asset_projection_binding",
                lambda **values: (
                    healthy_projection_checks.append(str(values["version_ref"])),
                    original_projection_verifier(**values),
                )[-1],
            )
            assert human.query_quest_creation(opened["initialization_id"])[
                "confirmation_preview"
            ]["status"] == "current"
            healthy_projection_checks.clear()
            assert not human.process_drafting_once()
            assert not human.process_drafting_once()
            assert healthy_projection_checks == []
        source.write_bytes(b"# Drifted before Human confirmation\n")
        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE rm_asset_verification_observations SET "
                    "next_verify_at = 0 WHERE version_ref = :version_ref"
                ),
                {"version_ref": binding.version_ref},
            )
        assert runtime.owners.research_memory.verify_asset_inventory_once()
        stale_preview = human.query_quest_creation(opened["initialization_id"])
        assert stale_preview["confirmation_preview"]["status"] == "stale"
        drifted_projection_checks: list[str] = []
        with monkeypatch.context() as idle_patch:
            idle_patch.setattr(
                runtime.owners.research_memory,
                "verify_asset_binding",
                lambda **_values: (_ for _ in ()).throw(
                    AssertionError(
                        "idle Preview refresh must not deep-hash drifted material"
                    )
                ),
            )
            idle_patch.setattr(
                runtime.owners.research_memory,
                "verify_asset_projection_binding",
                lambda **values: (
                    drifted_projection_checks.append(str(values["version_ref"])),
                    original_projection_verifier(**values),
                )[-1],
            )
            assert not human.process_drafting_once()
            assert not human.process_drafting_once()
            assert drifted_projection_checks == [binding.version_ref]
        with pytest.raises(OwnerConflict, match="asset_custody_unavailable"):
            human.confirm_quest(
                opened["initialization_id"],
                quest_draft_revision=proposed["quest_draft"]["revision"],
                quest_draft_hash=proposed["quest_draft"]["hash"],
                proposal_ref=proposed["proposal"]["ref"],
                proposal_hash=proposed["proposal"]["hash"],
                preview_ref=previewed["confirmation_preview"]["ref"],
                preview_hash=previewed["confirmation_preview"]["hash"],
                idempotency_key="quest-material-confirm-while-drifted",
            )
        source.write_bytes(b"# Provided method\nExact accepted bytes.\n")
        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE rm_asset_verification_observations SET "
                    "next_verify_at = 0 WHERE version_ref = :version_ref"
                ),
                {"version_ref": binding.version_ref},
            )
        assert runtime.owners.research_memory.verify_asset_inventory_once()
        recovered_projection_checks: list[str] = []
        with monkeypatch.context() as recovered_patch:
            recovered_patch.setattr(
                runtime.owners.research_memory,
                "verify_asset_projection_binding",
                lambda **values: (
                    recovered_projection_checks.append(str(values["version_ref"])),
                    original_projection_verifier(**values),
                )[-1],
            )
            assert human.process_drafting_once()
        assert recovered_projection_checks == [binding.version_ref]
        recovered = human.query_quest_creation(opened["initialization_id"])
        recovered_preview = recovered["confirmation_preview"]
        assert recovered_preview["status"] == "current"
        assert recovered_preview["ref"] != previewed["confirmation_preview"]["ref"]
        human.confirm_quest(
            opened["initialization_id"],
            quest_draft_revision=proposed["quest_draft"]["revision"],
            quest_draft_hash=proposed["quest_draft"]["hash"],
            proposal_ref=proposed["proposal"]["ref"],
            proposal_hash=proposed["proposal"]["hash"],
            preview_ref=recovered_preview["ref"],
            preview_hash=recovered_preview["hash"],
            idempotency_key="quest-material-confirm",
        )
        assert human.reconcile_once()
        original_accept_role = runtime.owners.research_graph.accept_asset_role

        def reject_material_role(**_values):
            raise OwnerConflict("quest_source_material_unavailable")

        monkeypatch.setattr(
            runtime.owners.research_graph,
            "accept_asset_role",
            reject_material_role,
        )
        assert not human.reconcile_once()
        partial = human.query_quest_creation(opened["initialization_id"])
        assert partial["receipts"]["quest_goal"]["status"] == "accepted"
        assert partial["receipts"]["quest_source_material"] == {
            "status": "rejected",
            "reason": {"code": "quest_source_material_unavailable"},
        }
        assert partial["receipts"]["question_content"] == {
            "status": "not_attempted",
            "reason": {
                "code": "upstream_not_accepted",
                "upstream_step": "quest_source_material",
            },
        }
        assert partial["recovery"]["first_missing_step"] == (
            "quest_source_material"
        )
        monkeypatch.setattr(
            runtime.owners.research_graph,
            "accept_asset_role",
            original_accept_role,
        )
        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE hc_reconciliation_checkpoints SET next_retry_at = 0 "
                    "WHERE initialization_id = :initialization_id"
                ),
                {"initialization_id": opened["initialization_id"]},
            )
        assert human.reconcile_once()
        source.write_bytes(b"# Drifted after RG role acceptance\n")
        for _step in range(7):
            if not human.reconcile_once():
                break

        completed = human.query_quest_creation(opened["initialization_id"])
        assert completed["status"] == "completed"
        assert completed["receipts"]["quest_source_material"]["status"] == (
            "accepted"
        )
        roles = runtime.owners.research_graph.query_asset_roles(
            quest_ref=completed["quest_ref"], role="quest_source_material"
        )
        assert len(roles) == 1
        assert roles[0].version_ref == binding.version_ref
        assert roles[0].asset_receipt == binding.receipt
        assert roles[0].receipt.kind == "asset_role_acceptance"
        extra = runtime.owners.research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="text",
                custody_mode="managed",
                display_name="extra-global-material.md",
                content=b"extra role outside the confirmed Quest draft\n",
            ),
            idempotency_key="quest-material-extra-intake",
        )
        assert extra.asset is not None
        runtime.owners.research_graph.accept_asset_role(
            binding=extra.asset.as_binding(),
            role="quest_source_material",
            quest_ref=completed["quest_ref"],
            idempotency_key="quest-material-extra-role",
        )
        exact_view = human.query_quest_creation(opened["initialization_id"])
        assert exact_view["receipts"]["quest_source_material"]["role_refs"] == [
            roles[0].role_ref
        ]
        assert runtime.owners.research_graph.query_snapshot().facts[
            "source_material_role_count"
        ] == 2
        inventory = runtime.owners.research_memory.query_asset_inventory()
        material_item = next(
            item for item in inventory if item.version_ref == binding.version_ref
        )
        assert material_item.availability == "drifted"
    finally:
        runtime.close()
