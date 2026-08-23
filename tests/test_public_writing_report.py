from __future__ import annotations

import threading
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from meta_research.composition import build_production_runtime
from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.owners.research_memory import (
    AssetIntakeRequest,
    AssetIntakeResult,
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
from meta_research.writing_contract import (
    WritingRuntimeBinding,
    validate_writing_claim_inventory,
)
from meta_research.writing_skill import WritingSkillProvider
from meta_research.writing_skill import (
    WritingSkillDraft,
    WritingSkillRequest,
    WritingSkillResult,
    WritingSkillUnavailable,
    writing_review_task_hash,
)
from meta_research.writing_snapshot import WritingResearchSnapshotReader


_QUESTION = {
    "title": "低照度显微图像中的稀有形态保真",
    "unknown_statement": "尚不明确哪种去噪条件能保留稀有形态。",
    "answer_shape": "形成带证据边界的比较结论。",
    "applicability_scope": "低照度荧光显微公开数据。",
    "background_context": "研究稀有细胞形态。",
    "requirements_constraints": "两周内完成阶段性报告。",
}


class _DeterministicDraftingAdapter:
    def draft(self, request: ProposalDraftRequest) -> ProposalDraftResult:
        del request
        return ProposalDraftResult(_QUESTION, "test_deterministic")

    def reply(self, request: IntentTurnRequest) -> IntentTurnResult:
        return IntentTurnResult(
            "测试回复",
            request.native_session_ref or "intent-session",
            "test_deterministic",
        )


class _DeterministicProbe:
    def observe(self) -> HostComputeSnapshot:
        return HostComputeSnapshot(
            status="ready",
            observed_at=1720000000.0,
            devices=(
                HostComputeDevice(
                    uuid="GPU-writing-test",
                    name="Writing Test GPU",
                    memory_total_mib=81920,
                ),
            ),
            adapter_kind="test_probe",
        )


class _DeterministicWritingSkill(WritingSkillProvider):
    def __init__(self) -> None:
        self.draft_requests: list[WritingSkillRequest] = []

    def runtime_binding(self) -> WritingRuntimeBinding:
        return WritingRuntimeBinding(
            packaged_skill_bundle_hash=canonical_hash({"skill": "writing-public"}),
            instruction_set_hash=canonical_hash({"instructions": "writing-public"}),
            model_ref="test-model-v1",
            harness_adapter_ref="test-deterministic-v1",
            mcp_bindings=(),
            capability_bindings=(),
            resource_bindings=(),
        )

    def generate_draft(self, request: WritingSkillRequest) -> WritingSkillDraft:
        self.draft_requests.append(request)
        return WritingSkillDraft(
            markdown="# 初稿\n\n当前证据尚不足。\n",
            citations=(),
            primary_session_ref=(
                request.native_session_ref or f"writing-session:{request.run_ref}"
            ),
            adapter_kind="test_deterministic",
        )

    def review_draft(
        self, request: WritingSkillRequest, draft: WritingSkillDraft
    ) -> WritingSkillResult:
        return WritingSkillResult(
            reviewed_markdown=draft.markdown,
            final_markdown=(
                "# 阶段性形态保真报告\n\n"
                "<!-- meta-research-structure -->\n"
                "## 结论\n\n"
                "<!-- meta-research-claim:evidence-gap -->\n"
                "**Evidence gap:** "
                f"第 {request.revision} 版：当前 Snapshot 尚无可引用研究资产，"
                "不能形成确定性结论。\n"
            ),
            citations=(),
            findings=(
                {
                    "category": "evidence_boundary",
                    "finding": "初稿没有明确说明 Snapshot 中不存在可引用研究资产。",
                },
            ),
            dispositions=(
                {
                    "category": "evidence_boundary",
                    "action": "revised",
                    "reason": "在结论中明确证据缺口。",
                },
            ),
            primary_session_ref=draft.primary_session_ref,
            review_mode="harness_child_agent",
            reviewer_agent_ref="writing-reviewer-1",
            review_task_hash=writing_review_task_hash(request, draft),
            adapter_kind=draft.adapter_kind,
        )


def _runtime(path: Path, writing_skill: WritingSkillProvider | None = None):
    drafting = _DeterministicDraftingAdapter()
    return build_production_runtime(
        prepare_data_root(path),
        proposal_drafter=drafting,
        intent_drafting_provider=drafting,
        host_compute_probe=_DeterministicProbe(),
        writing_skill_provider=writing_skill or _DeterministicWritingSkill(),
    )


def _confirm_direct_quest(runtime) -> dict[str, object]:
    human = runtime.owners.human_collaboration
    opened = human.create_quest({}, "writing-quest-open")
    probed = human.observe_host_compute(
        opened["initialization_id"],
        ["GPU-writing-test"],
        "writing-compute-probe",
    )
    draft = dict(probed["quest_draft"]["value"])
    draft.update(
        {
            "goal": "判断低照度显微图像去噪能否保留稀有形态。",
            "completion_criteria": "形成带证据边界的比较结论。",
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
    human.revise_quest_draft(
        opened["initialization_id"],
        draft,
        probed["quest_draft"]["hash"],
        "writing-quest-draft",
        probed["quest_draft"]["revision"],
    )
    drafted = human.query_quest_creation(opened["initialization_id"])
    human.generate_question_proposal(
        opened["initialization_id"],
        drafted["quest_draft"]["hash"],
        "writing-proposal",
        drafted["quest_draft"]["revision"],
    )
    assert human.process_drafting_once()
    proposed = human.query_quest_creation(opened["initialization_id"])
    previewed = human.preview_confirmation(
        opened["initialization_id"],
        quest_draft_revision=proposed["quest_draft"]["revision"],
        quest_draft_hash=proposed["quest_draft"]["hash"],
        proposal_ref=proposed["proposal"]["ref"],
        proposal_hash=proposed["proposal"]["hash"],
        idempotency_key="writing-quest-preview",
    )
    human.confirm_quest(
        opened["initialization_id"],
        quest_draft_revision=proposed["quest_draft"]["revision"],
        quest_draft_hash=proposed["quest_draft"]["hash"],
        proposal_ref=proposed["proposal"]["ref"],
        proposal_hash=proposed["proposal"]["hash"],
        preview_ref=previewed["confirmation_preview"]["ref"],
        preview_hash=previewed["confirmation_preview"]["hash"],
        idempotency_key="writing-quest-confirm",
    )
    for _step in range(8):
        if not human.reconcile_once():
            break
    completed = human.query_quest_creation(opened["initialization_id"])
    assert completed["status"] == "completed"
    return completed


def test_report_intent_freezes_snapshot_before_independent_run_admission(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "writing-admission")
    try:
        quest = _confirm_direct_quest(runtime)
        stage_before = runtime.idea_stage.query_current()

        drafted = runtime.writing.create_report_intent(
            quest_ref=quest["quest_ref"],
            title="阶段性形态保真报告",
            audience="研究负责人",
            purpose="复核当前证据与未知边界",
            instructions="突出可证伪结论与局限。",
            idempotency_key="writing-intent-create",
        )

        assert drafted["status"] == "draft"
        assert drafted["document_type"] == "report"
        assert drafted["snapshot"]["quest_ref"] == quest["quest_ref"]
        assert drafted["snapshot"]["snapshot_hash"]
        assert drafted["run"] is None

        previewed = runtime.writing.preview_report_intent(
            drafted["intent_id"],
            idempotency_key="writing-intent-preview",
        )
        preview = previewed["impact_preview"]
        assert preview["status"] == "current"
        assert preview["snapshot_hash"] == drafted["snapshot"]["snapshot_hash"]
        assert "主 Quest Stage 不会被暂停或推进" in preview["will_not_happen"]

        admitted = runtime.writing.confirm_report_intent(
            drafted["intent_id"],
            draft_revision=previewed["draft_revision"],
            draft_hash=previewed["draft_hash"],
            preview_ref=preview["preview_ref"],
            preview_hash=preview["preview_hash"],
            idempotency_key="writing-intent-confirm",
        )

        assert admitted["status"] == "running"
        assert admitted["run"]["status"] == "active"
        assert admitted["run"]["root_session_ref"]
        assert admitted["run"]["attempt_ref"]
        assert admitted["run"]["fence_ref"]
        assert admitted["execution"] == {
            "status": "admitted",
            "checkpoint": None,
            "receipt": None,
        }
        assert admitted["deliverable"] == {"status": "not_attempted"}
        assert admitted["citation"] == {"status": "not_attempted"}
        assert admitted["renderer"] == {"status": "not_attempted"}
        assert runtime.idea_stage.query_current() == stage_before
    finally:
        runtime.close()

def test_writing_run_checkpoints_then_separates_execution_rm_rg_and_rendering(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "writing-core-loop")
    try:
        quest = _confirm_direct_quest(runtime)
        drafted = runtime.writing.create_report_intent(
            quest_ref=quest["quest_ref"],
            title="阶段性形态保真报告",
            audience="研究负责人",
            purpose="复核当前证据与未知边界",
            instructions="突出可证伪结论与局限。",
            idempotency_key="writing-loop-create",
        )
        previewed = runtime.writing.preview_report_intent(
            drafted["intent_id"], idempotency_key="writing-loop-preview"
        )
        preview = previewed["impact_preview"]
        admitted = runtime.writing.confirm_report_intent(
            drafted["intent_id"],
            draft_revision=previewed["draft_revision"],
            draft_hash=previewed["draft_hash"],
            preview_ref=preview["preview_ref"],
            preview_hash=preview["preview_hash"],
            idempotency_key="writing-loop-confirm",
        )
        run_ref = admitted["run"]["run_ref"]

        assert runtime.writing.process_once()
        checkpointed = runtime.writing.query_writing_report(run_ref)
        assert checkpointed["execution"]["status"] == "running"
        assert checkpointed["execution"]["checkpoint"]["markdown_hash"]
        assert checkpointed["execution"]["receipt"] is None
        assert checkpointed["deliverable"] == {"status": "not_attempted"}

        assert runtime.writing.process_once()
        executed = runtime.writing.query_writing_report(run_ref)
        assert executed["execution"]["status"] == "completed"
        assert executed["execution"]["receipt"]["issuer"] == "agent_runtime"
        assert executed["deliverable"] == {"status": "not_attempted"}

        assert runtime.writing.process_once()
        remembered = runtime.writing.query_writing_report(run_ref)
        assert remembered["deliverable"]["status"] == "accepted"
        assert remembered["deliverable"]["receipt"]["issuer"] == "research_memory"
        assert remembered["deliverable"]["version_number"] == 1
        assert remembered["citation"] == {"status": "not_attempted"}
        assert remembered["renderer"] == {"status": "not_attempted"}

        assert runtime.writing.process_once()
        accepted = runtime.writing.query_writing_report(run_ref)
        assert accepted["citation"]["status"] == "accepted"
        assert accepted["citation"]["receipt"]["issuer"] == "research_graph"
        assert accepted["renderer"]["status"] == "ready"

        inventory_before = runtime.owners.research_memory.query_asset_inventory()
        first = runtime.writing.render_report(run_ref, format="markdown")
        second = runtime.writing.render_report(run_ref, format="markdown")
        assert first == second
        assert first["content"].startswith("# 阶段性形态保真报告".encode())
        assert first["version_ref"] == accepted["deliverable"]["version_ref"]
        assert [
            item.version_ref
            for item in runtime.owners.research_memory.query_asset_inventory()
        ] == [item.version_ref for item in inventory_before]

        first_version_ref = accepted["deliverable"]["version_ref"]
        first_bytes = first["content"]
        runtime.writing.request_revision(
            run_ref,
            feedback=("把证据缺口明确标为第二版复核结果。",),
            idempotency_key="writing-loop-revision",
        )
        for _step in range(4):
            assert runtime.writing.process_once()
        successor = runtime.writing.query_writing_report(run_ref)
        assert successor["citation"]["status"] == "accepted"
        assert successor["deliverable"]["version_number"] == 2

        historical_first = runtime.writing.render_report(
            run_ref,
            version_ref=first_version_ref,
            format="markdown",
        )
        historical_second = runtime.writing.render_report(
            run_ref,
            version_ref=first_version_ref,
            format="markdown",
        )
        assert historical_first == historical_second
        assert historical_first["version_ref"] == first_version_ref
        assert historical_first["content"] == first_bytes
    finally:
        runtime.close()


def test_historical_rg_acceptance_survives_current_rm_custody_loss(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "writing-historical-custody")
    try:
        quest = _confirm_direct_quest(runtime)
        admitted = _admit_report(
            runtime, quest["quest_ref"], "writing-historical-custody"
        )
        run_ref = admitted["run"]["run_ref"]
        for _step in range(4):
            assert runtime.writing.process_once()
        accepted = runtime.writing.query_writing_report(run_ref)
        version_ref = accepted["deliverable"]["version_ref"]
        content_hash = accepted["deliverable"]["content_hash"]
        managed_object = (
            runtime.data_root.objects
            / "assets"
            / content_hash[:2]
            / content_hash
        )
        assert managed_object.is_file()
        managed_bytes = managed_object.read_bytes()

        managed_object.unlink()

        history = runtime.owners.research_graph.query_writing_citation_history(
            run_ref
        )
        assert len(history) == 1
        assert history[0].decision == "accepted"
        projected = runtime.writing.query_writing_report(run_ref)
        assert projected["versions"][0]["version_ref"] == version_ref
        assert projected["versions"][0]["citation_status"] == "accepted"
        assert projected["versions"][0]["integrity"] == "failed"
        assert projected["versions"][0]["availability"] == "unavailable"
        assert projected["deliverable"]["status"] == "unavailable"
        assert projected["deliverable"]["acceptance_status"] == "accepted"
        assert projected["deliverable"]["failure"] == {
            "code": "asset_custody_unavailable"
        }
        assert projected["citation"]["status"] == "accepted"
        assert projected["renderer"] == {
            "status": "unavailable",
            "reason": {"code": "asset_custody_unavailable"},
        }
        with pytest.raises(OwnerConflict, match="asset_custody_unavailable"):
            runtime.owners.research_memory.materialize_asset(version_ref)
        with pytest.raises(OwnerConflict, match="asset_custody_unavailable"):
            runtime.writing.render_report(run_ref, version_ref=version_ref)

        managed_object.write_bytes(managed_bytes)
        recovered = runtime.writing.query_writing_report(run_ref)
        assert recovered["deliverable"]["status"] == "accepted"
        assert recovered["deliverable"]["integrity"] == "verified"
        assert recovered["deliverable"]["availability"] == "available"
        assert recovered["renderer"] == {"status": "ready"}
        assert runtime.writing.render_report(
            run_ref, version_ref=version_ref
        )["content"] == managed_bytes
    finally:
        runtime.close()


def test_rg_refuses_citation_source_not_accepted_for_the_quest(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "writing-rg-source-boundary")
    try:
        quest = _confirm_direct_quest(runtime)
        admitted = _admit_report(
            runtime, quest["quest_ref"], "writing-rg-source-boundary"
        )
        run_ref = admitted["run"]["run_ref"]
        for _step in range(3):
            assert runtime.writing.process_once()

        current = runtime.writing.query_writing_report(run_ref)
        run = runtime.owners.agent_runtime.query_writing_report(run_ref)
        assert run is not None and run.execution is not None
        deliverable = runtime.owners.research_memory.query_asset_version(
            current["deliverable"]["version_ref"]
        )
        assert deliverable is not None
        with pytest.raises(
            OwnerConflict, match="writing_citation_source_unaccepted"
        ):
            runtime.owners.research_graph.decide_writing_citations(
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                quest_ref=run.quest_ref,
                snapshot_ref=admitted["snapshot"]["snapshot_ref"],
                snapshot_hash=admitted["snapshot"]["snapshot_hash"],
                allowed_source_version_refs=("forged_source_version",),
                binding=deliverable.as_binding(),
                citations=run.execution.citations,
                final_markdown_hash=run.execution.final_markdown_hash,
                citations_hash=run.execution.citations_hash,
                execution_receipt=run.execution.receipt,
            )
    finally:
        runtime.close()


def test_rg_refuses_cross_run_deliverable_and_snapshot_receipt_splicing(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "writing-rg-chain-splice")
    try:
        quest = _confirm_direct_quest(runtime)
        first = _admit_report(
            runtime, quest["quest_ref"], "writing-chain-first", title="报告 A"
        )
        for _step in range(4):
            assert runtime.writing.process_once()
        second = _admit_report(
            runtime, quest["quest_ref"], "writing-chain-second", title="报告 B"
        )
        for _step in range(3):
            assert runtime.writing.process_once()

        first_run = runtime.owners.agent_runtime.query_writing_report(
            first["run"]["run_ref"]
        )
        second_view = runtime.writing.query_writing_report(
            second["run"]["run_ref"]
        )
        assert first_run is not None and first_run.execution is not None
        second_asset = runtime.owners.research_memory.query_asset_version(
            second_view["deliverable"]["version_ref"]
        )
        assert second_asset is not None
        rg = runtime.owners.research_graph
        common = {
            "run_ref": first_run.run_ref,
            "attempt_ref": first_run.attempt_ref,
            "fence_ref": first_run.fence_ref,
            "quest_ref": first_run.quest_ref,
            "snapshot_hash": first["snapshot"]["snapshot_hash"],
            "allowed_source_version_refs": (),
            "citations": first_run.execution.citations,
            "final_markdown_hash": first_run.execution.final_markdown_hash,
            "citations_hash": first_run.execution.citations_hash,
            "execution_receipt": first_run.execution.receipt,
        }
        with pytest.raises(
            OwnerConflict, match="writing_deliverable_execution_mismatch"
        ):
            rg.decide_writing_citations(
                **common,
                snapshot_ref=first["snapshot"]["snapshot_ref"],
                binding=second_asset.as_binding(),
            )
        with pytest.raises(
            OwnerConflict, match="writing_citation_admission_binding_mismatch"
        ):
            rg.decide_writing_citations(
                **common,
                snapshot_ref="writing_snapshot_forged",
                binding=second_asset.as_binding(),
            )
    finally:
        runtime.close()


def test_public_asset_intake_cannot_preempt_internal_writing_delivery_key(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "writing-rm-namespace")
    try:
        quest = _confirm_direct_quest(runtime)
        admitted = _admit_report(
            runtime,
            quest["quest_ref"],
            "writing-rm-namespace",
        )
        run_ref = admitted["run"]["run_ref"]
        attempt_ref = admitted["run"]["attempt_ref"]
        for _boundary in range(2):
            current = runtime.owners.agent_runtime.query_writing_report(
                run_ref
            )
            assert current is not None
            assert runtime.writing.process_once(
                expected_run_ref=current.run_ref,
                expected_attempt_ref=current.attempt_ref,
                expected_fence_ref=current.fence_ref,
            )

        internal_key = "writing:" + canonical_hash(
            ["writing-deliverable", run_ref, attempt_ref]
        )
        owner_run = runtime.owners.agent_runtime.query_writing_report(run_ref)
        assert owner_run is not None and owner_run.execution is not None
        legitimate = runtime.writing._deliverable_request(owner_run)
        attacker = runtime.owners.research_memory.submit_asset_intake(
            replace(
                legitimate,
                display_name="forged-writing-deliverable.md",
            ),
            idempotency_key=internal_key,
        )
        assert attacker.status == "accepted"

        current = runtime.owners.agent_runtime.query_writing_report(
            run_ref
        )
        assert current is not None
        assert runtime.writing.process_once(
            expected_run_ref=current.run_ref,
            expected_attempt_ref=current.attempt_ref,
            expected_fence_ref=current.fence_ref,
        )
        delivered = runtime.writing.query_writing_report(run_ref)
        assert delivered["status"] == "running"
        assert delivered["deliverable"]["status"] == "accepted"
        assert delivered["deliverable"]["version_ref"] != attacker.asset.version_ref
    finally:
        runtime.close()


class _RevisionWritingSkill(_DeterministicWritingSkill):
    source_version_ref: str | None = None

    def generate_draft(self, request: WritingSkillRequest) -> WritingSkillDraft:
        self.draft_requests.append(request)
        assert self.source_version_ref is not None
        assert len(request.source_materials) == 1
        assert request.source_materials[0].version_ref == self.source_version_ref
        assert request.source_materials[0].content == (
            b"rare morphology remains visible\n"
        )
        return WritingSkillDraft(
            markdown=(
                f"# 修订 {request.revision}\n\n"
                "<!-- meta-research-claim:supported "
                f"refs=citation-{request.revision} -->\n"
                "rare morphology remains visible "
                f"[[citation:citation-{request.revision}]]\n"
            ),
            citations=(
                {
                    "citation_ref": f"citation-{request.revision}",
                    "source_version_ref": self.source_version_ref,
                    "locator": (
                        "missing:table-1" if request.revision == 1 else "line:1"
                    ),
                    "claim": "rare morphology remains visible",
                    "source_quote": "rare morphology remains visible",
                },
            ),
            primary_session_ref=request.native_session_ref or "revision-session",
            adapter_kind="test_revision",
        )

    def review_draft(
        self, request: WritingSkillRequest, draft: WritingSkillDraft
    ) -> WritingSkillResult:
        return WritingSkillResult(
            reviewed_markdown=draft.markdown,
            final_markdown=(
                draft.markdown
                + (
                    "\n<!-- meta-research-claim:uncertainty -->\n"
                    "**Uncertainty:** 引用位置仍待验证。\n"
                    if request.revision == 1
                    else (
                        "\n<!-- meta-research-claim:inference -->\n"
                        "**Inference:** 对原文的中文概括是“稀有形态仍然可辨识”；"
                        "已按 RG feedback 修正到可验证行号。\n"
                    )
                )
            ),
            citations=draft.citations,
            findings=(
                {"category": "citation", "finding": "需要明确引用定位。"},
            ),
            dispositions=(
                {
                    "category": "citation",
                    "action": "revised",
                    "reason": "补充引用状态说明。",
                },
            ),
            primary_session_ref=draft.primary_session_ref,
            review_mode="harness_child_agent",
            reviewer_agent_ref=f"revision-reviewer-{request.revision}",
            review_task_hash=writing_review_task_hash(request, draft),
            adapter_kind=draft.adapter_kind,
        )


class _LostAckWritingSkill(_DeterministicWritingSkill):
    def __init__(self) -> None:
        super().__init__()
        self._lose_ack = True

    def generate_draft(self, request: WritingSkillRequest) -> WritingSkillDraft:
        draft = super().generate_draft(request)
        if self._lose_ack:
            self._lose_ack = False
            raise RuntimeError("simulated_writing_provider_ack_loss")
        return draft


class _CollidingSessionWritingSkill(_DeterministicWritingSkill):
    def generate_draft(self, request: WritingSkillRequest) -> WritingSkillDraft:
        draft = super().generate_draft(request)
        if len(self.draft_requests) <= 2:
            return replace(draft, primary_session_ref="colliding-native-session")
        return draft


class _UnclassifiedClaimWritingSkill(_RevisionWritingSkill):
    def generate_draft(self, request: WritingSkillRequest) -> WritingSkillDraft:
        draft = super().generate_draft(request)
        return replace(
            draft,
            citations=tuple(
                {**citation, "locator": "line:1"}
                for citation in draft.citations
            ),
        )

    def review_draft(
        self, request: WritingSkillRequest, draft: WritingSkillDraft
    ) -> WritingSkillResult:
        result = super().review_draft(request, draft)
        return replace(
            result,
            final_markdown=(
                result.final_markdown
                + "\nThe moon is made of cheese, without evidence.\n"
            ),
        )


class _UnsupportedCitationWritingSkill(_RevisionWritingSkill):
    def generate_draft(self, request: WritingSkillRequest) -> WritingSkillDraft:
        draft = super().generate_draft(request)
        citation_ref = f"citation-{request.revision}"
        citation = {
            **draft.citations[0],
            "locator": "line:1",
            "claim": "the moon is made of cheese",
        }
        return replace(
            draft,
            markdown=(
                f"# 修订 {request.revision}\n\n"
                "<!-- meta-research-claim:supported "
                f"refs={citation_ref} -->\n"
                f"the moon is made of cheese [[citation:{citation_ref}]]\n"
            ),
            citations=(citation,),
        )


def _one_page_text_pdf(text_value: str) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=200)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = writer._add_object(font)
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {NameObject("/F1"): font_ref}
            )
        }
    )
    stream = DecodedStreamObject()
    stream.set_data(
        f"BT /F1 12 Tf 20 100 Td ({text_value}) Tj ET".encode("ascii")
    )
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def test_rm_resolves_directory_lines_and_pdf_pages_for_writing_citations(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "writing-locators")
    directory = tmp_path / "accepted-directory"
    (directory / "notes").mkdir(parents=True)
    (directory / "notes" / "observation.txt").write_text(
        "rare morphology remains visible\n", encoding="utf-8"
    )
    try:
        locator_verifier = create_research_memory_receipt_verifier(
            runtime._database, runtime.data_root.objects
        )
        directory_result = runtime.owners.research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="directory",
                custody_mode="managed",
                display_name="accepted-directory",
                media_type="application/x-directory",
                source_locator=str(directory.resolve()),
            ),
            idempotency_key="writing-directory-locator",
        )
        assert directory_result.asset is not None
        assert locator_verifier.verify_writing_source_locator(
            version_ref=directory_result.asset.version_ref,
            locator="path:notes/observation.txt#line:1",
        ) == "rare morphology remains visible"
        with pytest.raises(
            OwnerConflict, match="writing_citation_locator_unverifiable"
        ):
            locator_verifier.verify_writing_source_locator(
                version_ref=directory_result.asset.version_ref,
                locator="path:../observation.txt#line:1",
            )

        pdf_result = runtime.owners.research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="file",
                custody_mode="managed",
                display_name="accepted-observation.pdf",
                media_type="application/pdf",
                content=_one_page_text_pdf("rare morphology remains visible"),
            ),
            idempotency_key="writing-pdf-locator",
        )
        assert pdf_result.asset is not None
        assert "rare morphology remains visible" in (
            locator_verifier.verify_writing_source_locator(
                version_ref=pdf_result.asset.version_ref,
                locator="page:1",
            )
        )
    finally:
        runtime.close()


def test_rg_rejects_any_unclassified_material_claim_in_the_report(
    tmp_path: Path,
) -> None:
    provider = _UnclassifiedClaimWritingSkill()
    runtime = _runtime(tmp_path / "writing-claim-coverage", provider)
    try:
        quest = _confirm_direct_quest(runtime)
        source = runtime.owners.research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="text",
                custody_mode="managed",
                display_name="accepted-observation.txt",
                media_type="text/plain; charset=utf-8",
                content=b"rare morphology remains visible\n",
            ),
            idempotency_key="writing-claim-coverage-source",
        )
        assert source.asset is not None
        runtime.owners.research_graph.accept_asset_role(
            binding=source.asset.as_binding(),
            role="evidence",
            quest_ref=quest["quest_ref"],
            idempotency_key="writing-claim-coverage-role",
        )
        provider.source_version_ref = source.asset.version_ref
        admitted = _admit_report(
            runtime,
            quest["quest_ref"],
            "writing-claim-coverage",
            title="逐声明覆盖报告",
        )
        for _step in range(4):
            assert runtime.writing.process_once()
        report = runtime.writing.query_writing_report(admitted["run"]["run_ref"])
        assert report["citation"]["status"] == "rejected"
        assert "report:writing_claim_unclassified" in report["citation"][
            "feedback"
        ]
    finally:
        runtime.close()


def test_rg_does_not_treat_an_unrelated_locator_quote_as_claim_support(
    tmp_path: Path,
) -> None:
    provider = _UnsupportedCitationWritingSkill()
    runtime = _runtime(tmp_path / "writing-citation-entailment", provider)
    try:
        quest = _confirm_direct_quest(runtime)
        source = runtime.owners.research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="text",
                custody_mode="managed",
                display_name="accepted-observation.txt",
                media_type="text/plain; charset=utf-8",
                content=b"rare morphology remains visible\n",
            ),
            idempotency_key="writing-citation-entailment-source",
        )
        assert source.asset is not None
        runtime.owners.research_graph.accept_asset_role(
            binding=source.asset.as_binding(),
            role="evidence",
            quest_ref=quest["quest_ref"],
            idempotency_key="writing-citation-entailment-role",
        )
        provider.source_version_ref = source.asset.version_ref
        admitted = _admit_report(
            runtime,
            quest["quest_ref"],
            "writing-citation-entailment",
            title="引用支持关系报告",
        )
        for _step in range(4):
            assert runtime.writing.process_once()
        report = runtime.writing.query_writing_report(admitted["run"]["run_ref"])
        assert report["citation"]["status"] == "rejected"
        assert (
            "citation[0]:claim_not_exact_source_quote"
            in report["citation"]["feedback"]
        )
    finally:
        runtime.close()


def test_claim_inventory_does_not_exempt_an_assertive_heading() -> None:
    with pytest.raises(OwnerConflict, match="writing_claim_unclassified"):
        validate_writing_claim_inventory(
            "# Report\n\n## The moon is made of cheese",
            (),
        )


def test_rg_rejection_creates_successor_version_in_same_session_and_fences_old_attempt(
    tmp_path: Path,
) -> None:
    provider = _RevisionWritingSkill()
    runtime = _runtime(tmp_path / "writing-revision", provider)
    try:
        quest = _confirm_direct_quest(runtime)
        source = runtime.owners.research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="text",
                custody_mode="managed",
                display_name="accepted-observation.txt",
                media_type="text/plain; charset=utf-8",
                content=b"rare morphology remains visible\n",
            ),
            idempotency_key="writing-revision-source",
        )
        assert source.asset is not None
        runtime.owners.research_graph.accept_asset_role(
            binding=source.asset.as_binding(),
            role="evidence",
            quest_ref=quest["quest_ref"],
            idempotency_key="writing-revision-evidence-role",
        )
        provider.source_version_ref = source.asset.version_ref
        drafted = runtime.writing.create_report_intent(
            quest_ref=quest["quest_ref"],
            title="引用修订报告",
            audience="研究负责人",
            purpose="验证引用闭环",
            instructions="引用必须可回读。",
            idempotency_key="writing-revision-create",
        )
        previewed = runtime.writing.preview_report_intent(
            drafted["intent_id"], idempotency_key="writing-revision-preview"
        )
        preview = previewed["impact_preview"]
        admitted = runtime.writing.confirm_report_intent(
            drafted["intent_id"],
            draft_revision=previewed["draft_revision"],
            draft_hash=previewed["draft_hash"],
            preview_ref=preview["preview_ref"],
            preview_hash=preview["preview_hash"],
            idempotency_key="writing-revision-confirm",
        )
        run_ref = admitted["run"]["run_ref"]
        for _step in range(4):
            assert runtime.writing.process_once()

        rejected = runtime.writing.query_writing_report(run_ref)
        assert rejected["citation"]["status"] == "rejected"
        assert rejected["renderer"] == {"status": "not_attempted"}
        old_run = runtime.owners.agent_runtime.query_writing_report(run_ref)
        assert old_run is not None and old_run.checkpoint is not None
        first_version_ref = rejected["deliverable"]["version_ref"]
        first_bytes = runtime.owners.research_memory.materialize_asset(
            first_version_ref
        ).content
        viewed = runtime.writing.view_report_version(
            run_ref, version_ref=first_version_ref
        )
        assert viewed["content"] == first_bytes
        assert viewed["citation_status"] == "rejected"
        assert viewed["formal_renderer"] is False
        with pytest.raises(OwnerConflict, match="writing_render_not_ready"):
            runtime.writing.render_report(run_ref, version_ref=first_version_ref)

        assert runtime.writing.process_once()
        successor = runtime.owners.agent_runtime.query_writing_report(run_ref)
        assert successor is not None
        assert successor.attempt_generation == 2
        assert successor.attempt_ref != old_run.attempt_ref
        assert successor.fence_ref != old_run.fence_ref
        assert successor.root_session_ref == old_run.root_session_ref
        assert successor.native_session_ref == old_run.native_session_ref
        assert successor.predecessor_version_ref == first_version_ref
        assert "citation[0]:locator_unverifiable" in successor.feedback

        with pytest.raises(OwnerConflict, match="writing_fence_stale"):
            runtime.owners.agent_runtime.record_writing_checkpoint(
                run_ref=old_run.run_ref,
                attempt_ref=old_run.attempt_ref,
                fence_ref=old_run.fence_ref,
                native_session_ref=old_run.checkpoint.native_session_ref,
                markdown=old_run.checkpoint.markdown,
                citations=old_run.checkpoint.citations,
                runtime_binding=old_run.runtime_binding,
                idempotency_key="writing-old-fence-must-fail",
            )

        for _step in range(4):
            assert runtime.writing.process_once()
        accepted = runtime.writing.query_writing_report(run_ref)
        assert accepted["citation"]["status"] == "accepted"
        assert accepted["deliverable"]["version_number"] == 2
        assert accepted["deliverable"]["asset_ref"] == rejected["deliverable"][
            "asset_ref"
        ]
        assert (
            runtime.owners.research_memory.materialize_asset(first_version_ref).content
            == first_bytes
        )

        compared = runtime.writing.compare_report_versions(
            run_ref,
            left_version_ref=first_version_ref,
            right_version_ref=accepted["deliverable"]["version_ref"],
        )
        assert compared["content"]["changed"] is True
        assert "-# 修订 1" in compared["content"]["unified_diff"]
        assert "+# 修订 2" in compared["content"]["unified_diff"]
        assert compared["evidence"]["changed"] is False
        assert compared["evidence"]["added_source_version_refs"] == []
        assert compared["evidence"]["removed_source_version_refs"] == []
        assert compared["citation"]["left_status"] == "rejected"
        assert compared["citation"]["right_status"] == "accepted"
        assert compared["citation"]["added_citation_refs"] == ["citation-2"]
        assert compared["citation"]["removed_citation_refs"] == ["citation-1"]
        assert compared["citation"]["changed_citations"] == []
        assert compared["stale"] is False

    finally:
        runtime.close()


def _admit_report(
    runtime,
    quest_ref: str,
    prefix: str,
    *,
    title: str = "可恢复 Writing 报告",
) -> dict[str, object]:
    drafted = runtime.writing.create_report_intent(
        quest_ref=quest_ref,
        title=title,
        audience="研究负责人",
        purpose="验证控制与重启恢复",
        instructions="保留精确版本历史。",
        idempotency_key=f"{prefix}-create",
    )
    previewed = runtime.writing.preview_report_intent(
        drafted["intent_id"], idempotency_key=f"{prefix}-preview"
    )
    preview = previewed["impact_preview"]
    return runtime.writing.confirm_report_intent(
        drafted["intent_id"],
        draft_revision=previewed["draft_revision"],
        draft_hash=previewed["draft_hash"],
        preview_ref=preview["preview_ref"],
        preview_hash=preview["preview_hash"],
        idempotency_key=f"{prefix}-confirm",
    )


def test_long_pause_does_not_consume_an_implicit_wall_clock_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _runtime(tmp_path / "writing-long-pause")
    try:
        quest = _confirm_direct_quest(runtime)
        admitted = _admit_report(runtime, quest["quest_ref"], "writing-long-pause")
        run_ref = admitted["run"]["run_ref"]
        assert "max_wall_seconds" not in admitted["run"]["execution_budget"]
        assert "deadline_at" not in admitted["run"]
        runtime.writing.control_report(
            run_ref,
            action="pause",
            idempotency_key="writing-long-pause-pause",
        )
        future_clock = SimpleNamespace(time=lambda: 2_000_000_000.0)
        monkeypatch.setattr("meta_research.writing.time", future_clock)
        monkeypatch.setattr("meta_research.owners.agent_runtime.time", future_clock)

        resumed = runtime.writing.control_report(
            run_ref,
            action="resume",
            idempotency_key="writing-long-pause-resume",
        )
        assert resumed["status"] == "running"
        assert runtime.writing.process_once()
        checkpointed = runtime.writing.query_writing_report(run_ref)
        assert checkpointed["execution"]["status"] == "running"
        assert checkpointed["run"]["blocker"] is None
    finally:
        runtime.close()


def test_revision_feedback_rejects_secrets_at_service_and_owner_boundaries(
    tmp_path: Path,
) -> None:
    provider = _DeterministicWritingSkill()
    runtime = _runtime(tmp_path / "writing-revision-secret", provider)
    try:
        quest = _confirm_direct_quest(runtime)
        admitted = _admit_report(
            runtime, quest["quest_ref"], "writing-revision-secret"
        )
        run_ref = admitted["run"]["run_ref"]
        for _step in range(4):
            assert runtime.writing.process_once()
        before = runtime.owners.agent_runtime.query_writing_report(run_ref)
        assert before is not None
        decision = runtime.owners.research_graph.query_writing_citation_decision(
            run_ref=run_ref,
            attempt_ref=before.attempt_ref,
        )
        assert decision is not None and decision.decision == "accepted"
        secret_feedback = ("api_key=sk-1234567890abcdefghijklmnop",)

        with pytest.raises(
            OwnerConflict, match="writing_revision_secret_forbidden"
        ):
            runtime.writing.request_revision(
                run_ref,
                feedback=secret_feedback,
                idempotency_key="writing-revision-secret-service",
            )
        with pytest.raises(
            OwnerConflict, match="writing_revision_secret_forbidden"
        ):
            runtime.owners.agent_runtime.begin_writing_revision(
                run_ref=run_ref,
                attempt_ref=before.attempt_ref,
                fence_ref=before.fence_ref,
                predecessor_version_ref=decision.asset.version_ref,
                feedback=secret_feedback,
                decision_receipt=decision.receipt,
                decision_status="accepted",
                idempotency_key="writing-revision-secret-owner",
            )

        after = runtime.owners.agent_runtime.query_writing_report(run_ref)
        assert after == before
        assert len(provider.draft_requests) == 1
    finally:
        runtime.close()


def test_two_intents_can_share_one_unchanged_research_snapshot(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "writing-shared-snapshot")
    try:
        quest = _confirm_direct_quest(runtime)
        first = _admit_report(
            runtime,
            quest["quest_ref"],
            "writing-shared-snapshot-first",
            title="同一切面报告 A",
        )
        second = _admit_report(
            runtime,
            quest["quest_ref"],
            "writing-shared-snapshot-second",
            title="同一切面报告 B",
        )

        assert first["snapshot"]["snapshot_ref"] == second["snapshot"][
            "snapshot_ref"
        ]
        assert first["snapshot"]["snapshot_hash"] == second["snapshot"][
            "snapshot_hash"
        ]
        assert first["run"]["run_ref"] != second["run"]["run_ref"]
    finally:
        runtime.close()


def test_cross_run_native_session_collision_blocks_only_the_offending_run(
    tmp_path: Path,
) -> None:
    provider = _CollidingSessionWritingSkill()
    runtime = _runtime(tmp_path / "writing-session-collision", provider)
    try:
        quest = _confirm_direct_quest(runtime)
        first = _admit_report(
            runtime, quest["quest_ref"], "writing-session-collision-first"
        )
        for _step in range(4):
            assert runtime.writing.process_once()
        assert runtime.writing.query_writing_report(first["run"]["run_ref"])[
            "citation"
        ]["status"] == "accepted"

        second = _admit_report(
            runtime, quest["quest_ref"], "writing-session-collision-second"
        )
        assert runtime.writing.process_once()
        blocked = runtime.writing.query_writing_report(second["run"]["run_ref"])
        assert blocked["status"] == "blocked"
        assert blocked["run"]["blocker"] == {
            "code": "writing_native_session_conflict"
        }

        third = _admit_report(
            runtime, quest["quest_ref"], "writing-session-collision-third"
        )
        for _step in range(4):
            assert runtime.writing.process_once()
        assert runtime.writing.query_writing_report(third["run"]["run_ref"])[
            "citation"
        ]["status"] == "accepted"
    finally:
        runtime.close()


def test_revoked_broad_authorization_blocks_an_admitted_run_before_provider_effect(
    tmp_path: Path,
) -> None:
    provider = _DeterministicWritingSkill()
    runtime = _runtime(tmp_path / "writing-revoked-authorization", provider)
    try:
        quest = _confirm_direct_quest(runtime)
        admitted = _admit_report(
            runtime, quest["quest_ref"], "writing-revoked-authorization"
        )
        human = runtime.owners.human_collaboration
        scope_ref = f"quest:{quest['quest_ref']}"
        command = human.create_command_draft(
            scope_ref,
            {
                "command_kind": "capability_authorization",
                "payload": {
                    "capability": "broad_research",
                    "decision": "revoked",
                    "scope": {"quest_ref": quest["quest_ref"]},
                },
            },
            "writing-revoke-draft",
        )
        preview = human.preview_command(
            command["intent_id"],
            command["draft_revision"],
            command["draft_hash"],
            "writing-revoke-preview",
        )["impact_preview"]
        confirmed = human.confirm_command(
            command["intent_id"],
            command["draft_revision"],
            command["draft_hash"],
            preview["preview_ref"],
            preview["preview_hash"],
            "writing-revoke-confirm",
        )
        human.decide_capability_authorization(
            scope_ref,
            {
                "capability": "broad_research",
                "decision": "revoked",
                "scope": {"quest_ref": quest["quest_ref"]},
                "confirmation_receipt_ref": confirmed[
                    "confirmation_receipt"
                ]["receipt_ref"],
            },
            "writing-revoke-authorization",
        )

        assert runtime.writing.process_once()
        blocked = runtime.writing.query_writing_report(
            admitted["run"]["run_ref"]
        )
        assert blocked["run"]["status"] == "blocked"
        assert blocked["run"]["blocker"] == {
            "code": "broad_research_authorization_revoked"
        }
        assert provider.draft_requests == []
    finally:
        runtime.close()


def test_pause_restart_feedback_revision_and_cancel_are_durable_and_idempotent(
    tmp_path: Path,
) -> None:
    root = tmp_path / "writing-recovery"
    provider = _DeterministicWritingSkill()
    runtime = _runtime(root, provider)
    quest = _confirm_direct_quest(runtime)
    admitted = _admit_report(runtime, quest["quest_ref"], "writing-recovery")
    run_ref = admitted["run"]["run_ref"]
    admitted_attempt_ref = admitted["run"]["attempt_ref"]
    admitted_fence_ref = admitted["run"]["fence_ref"]
    admitted_owner_run = runtime.owners.agent_runtime.query_writing_report(run_ref)
    assert admitted_owner_run is not None

    paused = runtime.writing.control_report(
        run_ref, action="pause", idempotency_key="writing-recovery-pause"
    )
    assert paused["status"] == "paused"
    assert runtime.writing.process_once() is False
    resumed = runtime.writing.control_report(
        run_ref, action="resume", idempotency_key="writing-recovery-resume"
    )
    assert resumed["status"] == "running"
    assert resumed["run"]["attempt_ref"] != admitted_attempt_ref
    assert resumed["run"]["fence_ref"] != admitted_fence_ref
    assert resumed["run"]["attempt_generation"] == 2
    resumed_owner_run = runtime.owners.agent_runtime.query_writing_report(run_ref)
    assert resumed_owner_run is not None
    assert resumed_owner_run.provider_job_ref != admitted_owner_run.provider_job_ref
    with pytest.raises(OwnerConflict, match="writing_control_stale"):
        runtime.writing.control_report(
            run_ref,
            action="pause",
            expected_attempt_ref=admitted_attempt_ref,
            expected_fence_ref=admitted_fence_ref,
            idempotency_key="writing-recovery-stale-pause",
        )
    with pytest.raises(OwnerConflict, match="writing_fence_stale"):
        runtime.owners.agent_runtime.verify_current_writing_attempt(
            run_ref=run_ref,
            attempt_ref=admitted_attempt_ref,
            fence_ref=admitted_fence_ref,
        )
    assert runtime.writing.process_once()
    before_restart = runtime.owners.agent_runtime.query_writing_report(run_ref)
    assert before_restart is not None and before_restart.checkpoint is not None
    first_job_ref = provider.draft_requests[-1].job_ref
    runtime.close()

    restarted = _runtime(root, provider)
    try:
        recovered = restarted.owners.agent_runtime.query_writing_report(run_ref)
        assert recovered is not None and recovered.checkpoint is not None
        assert recovered.attempt_generation == before_restart.attempt_generation + 1
        assert recovered.attempt_ref != before_restart.attempt_ref
        assert recovered.fence_ref != before_restart.fence_ref
        assert recovered.root_session_ref == before_restart.root_session_ref
        assert recovered.provider_job_ref == before_restart.provider_job_ref
        assert recovered.native_session_ref == before_restart.native_session_ref
        assert recovered.checkpoint.markdown_hash == before_restart.checkpoint.markdown_hash
        assert restarted.writing.query_writing_report(run_ref)["run"][
            "content_revision"
        ] == 1

        with pytest.raises(OwnerConflict, match="writing_fence_stale"):
            restarted.owners.agent_runtime.record_writing_checkpoint(
                run_ref=before_restart.run_ref,
                attempt_ref=before_restart.attempt_ref,
                fence_ref=before_restart.fence_ref,
                native_session_ref=before_restart.checkpoint.native_session_ref,
                markdown=before_restart.checkpoint.markdown,
                citations=before_restart.checkpoint.citations,
                runtime_binding=before_restart.runtime_binding,
                idempotency_key="writing-recovery-old-fence",
            )

        for _step in range(3):
            assert restarted.writing.process_once()
        accepted = restarted.writing.query_writing_report(run_ref)
        assert accepted["citation"]["status"] == "accepted"
        assert accepted["deliverable"]["version_number"] == 1
        assert restarted.writing.process_once() is False
        assert provider.draft_requests[-1].revision == 1
        assert provider.draft_requests[-1].job_ref == first_job_ref
        assert len(
            restarted.owners.research_graph.query_writing_citation_history(run_ref)
        ) == 1

        revised = restarted.writing.request_revision(
            run_ref,
            feedback=("请把证据缺口改写为明确的后续验证问题。",),
            idempotency_key="writing-recovery-feedback",
        )
        assert revised["run"]["attempt_generation"] == (
            recovered.attempt_generation + 1
        )
        assert revised["run"]["content_revision"] == 2
        assert revised["run"]["root_session_ref"] == before_restart.root_session_ref
        assert revised["run"]["native_session_ref"] == (
            before_restart.checkpoint.native_session_ref
        )

        cancellation = restarted.writing.preview_report_cancellation(
            run_ref, idempotency_key="writing-recovery-cancel-preview"
        )
        cancellation_preview = cancellation["impact_preview"]
        assert cancellation_preview is not None
        cancelled = restarted.writing.confirm_report_cancellation(
            run_ref,
            cancellation["intent_id"],
            draft_revision=cancellation["draft_revision"],
            draft_hash=cancellation["draft_hash"],
            preview_ref=cancellation_preview["preview_ref"],
            preview_hash=cancellation_preview["preview_hash"],
            idempotency_key="writing-recovery-cancel-confirm",
        )
        assert cancelled["status"] == "cancelled"
        assert restarted.writing.process_once() is False
        with pytest.raises(OwnerConflict, match="writing_run_terminal"):
            restarted.writing.request_revision(
                run_ref,
                feedback=("终态后不得重开。",),
                idempotency_key="writing-recovery-after-cancel",
            )
    finally:
        restarted.close()


def test_terminal_cancel_requires_an_exact_human_confirmation(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "writing-cancel-confirmation")
    try:
        quest = _confirm_direct_quest(runtime)
        admitted = _admit_report(
            runtime, quest["quest_ref"], "writing-cancel-confirmation"
        )
        run_ref = admitted["run"]["run_ref"]

        with pytest.raises(
            OwnerConflict, match="writing_cancel_confirmation_required"
        ):
            runtime.writing.control_report(
                run_ref,
                action="cancel",
                idempotency_key="writing-cancel-without-confirmation",
            )

        cancellation = runtime.writing.preview_report_cancellation(
            run_ref, idempotency_key="writing-cancel-preview"
        )
        preview = cancellation["impact_preview"]
        assert preview is not None
        assert "Writing Run 进入终态后不能继续或再修订" in preview["risks"]
        cancelled = runtime.writing.confirm_report_cancellation(
            run_ref,
            cancellation["intent_id"],
            draft_revision=cancellation["draft_revision"],
            draft_hash=cancellation["draft_hash"],
            preview_ref=preview["preview_ref"],
            preview_hash=preview["preview_hash"],
            idempotency_key="writing-cancel-confirm",
        )
        assert cancelled["status"] == "cancelled"
    finally:
        runtime.close()


def test_restart_reconciles_lost_provider_ack_without_duplicate_version(
    tmp_path: Path,
) -> None:
    root = tmp_path / "writing-lost-ack"
    provider = _LostAckWritingSkill()
    runtime = _runtime(root, provider)
    quest = _confirm_direct_quest(runtime)
    admitted = _admit_report(runtime, quest["quest_ref"], "writing-lost-ack")
    run_ref = admitted["run"]["run_ref"]

    with pytest.raises(RuntimeError, match="simulated_writing_provider_ack_loss"):
        runtime.writing.process_once()
    interrupted = runtime.owners.agent_runtime.query_writing_report(run_ref)
    assert interrupted is not None and interrupted.checkpoint is None
    first_request = provider.draft_requests[-1]
    runtime.close()

    restarted = _runtime(root, provider)
    try:
        recovered = restarted.owners.agent_runtime.query_writing_report(run_ref)
        assert recovered is not None and recovered.checkpoint is None
        assert recovered.attempt_ref != interrupted.attempt_ref
        assert recovered.fence_ref != interrupted.fence_ref

        for _step in range(4):
            assert restarted.writing.process_once()
        accepted = restarted.writing.query_writing_report(run_ref)
        assert accepted["citation"]["status"] == "accepted"
        assert accepted["deliverable"]["version_number"] == 1
        assert len(provider.draft_requests) == 2
        assert provider.draft_requests[-1].job_ref == first_request.job_ref
        assert provider.draft_requests[-1].revision == first_request.revision == 1
        assert len(
            restarted.owners.research_graph.query_writing_citation_history(run_ref)
        ) == 1
    finally:
        restarted.close()


def test_confirmation_fails_closed_when_frozen_research_snapshot_is_stale(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "writing-stale")
    try:
        quest = _confirm_direct_quest(runtime)
        drafted = runtime.writing.create_report_intent(
            quest_ref=quest["quest_ref"],
            title="过期 Snapshot 报告",
            audience="研究负责人",
            purpose="验证过期保护",
            instructions="不得静默换入新证据。",
            idempotency_key="writing-stale-create",
        )
        previewed = runtime.writing.preview_report_intent(
            drafted["intent_id"], idempotency_key="writing-stale-preview"
        )
        source = runtime.owners.research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="text",
                custody_mode="managed",
                display_name="late-evidence.txt",
                content=b"late accepted evidence\n",
            ),
            idempotency_key="writing-stale-source",
        )
        assert source.asset is not None
        runtime.owners.research_graph.accept_asset_role(
            binding=source.asset.as_binding(),
            role="evidence",
            quest_ref=quest["quest_ref"],
            idempotency_key="writing-stale-role",
        )
        preview = previewed["impact_preview"]
        with pytest.raises(OwnerConflict, match="writing_snapshot_stale"):
            runtime.writing.confirm_report_intent(
                drafted["intent_id"],
                draft_revision=previewed["draft_revision"],
                draft_hash=previewed["draft_hash"],
                preview_ref=preview["preview_ref"],
                preview_hash=preview["preview_hash"],
                idempotency_key="writing-stale-confirm",
            )
        assert runtime.owners.agent_runtime.query_writing_report_by_intent(
            drafted["intent_id"]
        ) is None
    finally:
        runtime.close()


def test_confirmation_ignores_unrelated_owner_revision_bookkeeping(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "writing-bookkeeping-revision")
    try:
        quest = _confirm_direct_quest(runtime)
        drafted = runtime.writing.create_report_intent(
            quest_ref=quest["quest_ref"],
            title="稳定 Snapshot 报告",
            audience="研究负责人",
            purpose="区分研究 basis 与 Owner bookkeeping",
            instructions="不得把无关资产误判为 Snapshot 变化。",
            idempotency_key="writing-bookkeeping-create",
        )
        previewed = runtime.writing.preview_report_intent(
            drafted["intent_id"], idempotency_key="writing-bookkeeping-preview"
        )
        unrelated = runtime.owners.research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="text",
                custody_mode="managed",
                display_name="unrelated-owner-bookkeeping.txt",
                content=b"not accepted into this Quest\n",
            ),
            idempotency_key="writing-bookkeeping-unrelated-asset",
        )
        assert unrelated.asset is not None

        preview = previewed["impact_preview"]
        confirmed = runtime.writing.confirm_report_intent(
            drafted["intent_id"],
            draft_revision=previewed["draft_revision"],
            draft_hash=previewed["draft_hash"],
            preview_ref=preview["preview_ref"],
            preview_hash=preview["preview_hash"],
            idempotency_key="writing-bookkeeping-confirm",
        )
        assert confirmed["run"]["status"] == "active"
    finally:
        runtime.close()


def test_experiment_closure_pages_past_256_without_silent_snapshot_truncation() -> None:
    admissions = [
        (f"evaluation_attempt:{index:04d}", float(index + 1))
        for index in range(257)
    ]

    class _ResearchGraph:
        def query_experiment_admission_refs(
            self,
            *,
            after_created_at: float,
            after_evaluation_attempt_ref: str,
            limit: int,
        ) -> tuple[tuple[str, float], ...]:
            cursor = (after_created_at, after_evaluation_attempt_ref)
            return tuple(
                item
                for item in admissions
                if (item[1], item[0]) > cursor
            )[:limit]

        def query_experiment(self, evaluation_attempt_ref: str):
            return SimpleNamespace(
                execution_request=SimpleNamespace(
                    quest_ref="quest:exact-snapshot",
                    as_public_dict=lambda: {
                        "quest_ref": "quest:exact-snapshot",
                        "evaluation_attempt_ref": evaluation_attempt_ref,
                    },
                ),
                formal_measurement_status="not_attempted",
                formal_rejection_code=None,
            )

        def query_experiment_asset_roles(
            self, evaluation_attempt_ref: str
        ) -> tuple[object, ...]:
            del evaluation_attempt_ref
            return ()

    reader = object.__new__(WritingResearchSnapshotReader)
    reader._research_graph = _ResearchGraph()
    reader._agent_runtime = SimpleNamespace(
        query_experiment_run=lambda _evaluation_attempt_ref: None
    )

    closure = reader._experiment_closure("quest:exact-snapshot")

    assert len(closure) == 257
    assert closure[-1]["evaluation_attempt_ref"] == admissions[-1][0]


def test_same_asset_version_can_fill_two_snapshot_roles_without_duplicate_staging(
    tmp_path: Path,
) -> None:
    provider = _RevisionWritingSkill()
    runtime = _runtime(tmp_path / "writing-dual-role", provider)
    try:
        quest = _confirm_direct_quest(runtime)
        source = runtime.owners.research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="text",
                custody_mode="managed",
                display_name="dual-role-source.txt",
                media_type="text/plain; charset=utf-8",
                content=b"rare morphology remains visible\n",
            ),
            idempotency_key="writing-dual-role-source",
        )
        assert source.asset is not None
        for role in ("evidence", "quest_source_material"):
            runtime.owners.research_graph.accept_asset_role(
                binding=source.asset.as_binding(),
                role=role,
                quest_ref=quest["quest_ref"],
                idempotency_key=f"writing-dual-role-{role}",
            )
        provider.source_version_ref = source.asset.version_ref
        admitted = _admit_report(runtime, quest["quest_ref"], "writing-dual-role")

        assert len(admitted["snapshot"]["accepted_sources"]) == 2
        assert runtime.writing.process_once()
        assert len(provider.draft_requests[-1].source_materials) == 1
        assert provider.draft_requests[-1].source_materials[0].version_ref == (
            source.asset.version_ref
        )
    finally:
        runtime.close()


def test_user_revision_replays_after_successful_first_call_loses_its_ack(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "writing-revision-replay")
    try:
        quest = _confirm_direct_quest(runtime)
        admitted = _admit_report(
            runtime, quest["quest_ref"], "writing-revision-replay"
        )
        run_ref = admitted["run"]["run_ref"]
        for _step in range(4):
            assert runtime.writing.process_once()

        feedback = ("把证据缺口改写为下一步验证问题。",)
        first = runtime.writing.request_revision(
            run_ref,
            feedback=feedback,
            idempotency_key="writing-revision-lost-ack",
        )
        replay = runtime.writing.request_revision(
            run_ref,
            feedback=feedback,
            idempotency_key="writing-revision-lost-ack",
        )

        assert replay["run"]["attempt_ref"] == first["run"]["attempt_ref"]
        assert replay["run"]["attempt_generation"] == first["run"][
            "attempt_generation"
        ]
    finally:
        runtime.close()


def test_confirm_reconciles_hc_confirmation_and_full_success_after_lost_ack(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "writing-confirm-reconcile")
    try:
        quest = _confirm_direct_quest(runtime)
        drafted = runtime.writing.create_report_intent(
            quest_ref=quest["quest_ref"],
            title="确认恢复报告",
            audience="研究负责人",
            purpose="验证跨 Owner 恢复",
            instructions="不得换入确认后的新状态。",
            idempotency_key="writing-confirm-reconcile-create",
        )
        previewed = runtime.writing.preview_report_intent(
            drafted["intent_id"],
            idempotency_key="writing-confirm-reconcile-preview",
        )
        preview = previewed["impact_preview"]
        runtime.owners.human_collaboration.confirm_command(
            drafted["intent_id"],
            previewed["draft_revision"],
            previewed["draft_hash"],
            preview["preview_ref"],
            preview["preview_hash"],
            "writing-confirm-reconcile-hc-only",
        )
        late = runtime.owners.research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="text",
                custody_mode="managed",
                display_name="confirmed-later.txt",
                content=b"accepted after human confirmation\n",
            ),
            idempotency_key="writing-confirm-reconcile-late-source",
        )
        assert late.asset is not None
        runtime.owners.research_graph.accept_asset_role(
            binding=late.asset.as_binding(),
            role="evidence",
            quest_ref=quest["quest_ref"],
            idempotency_key="writing-confirm-reconcile-late-role",
        )

        admitted = runtime.writing.confirm_report_intent(
            drafted["intent_id"],
            draft_revision=previewed["draft_revision"],
            draft_hash=previewed["draft_hash"],
            preview_ref=preview["preview_ref"],
            preview_hash=preview["preview_hash"],
            idempotency_key="writing-confirm-reconcile",
        )
        replay = runtime.writing.confirm_report_intent(
            drafted["intent_id"],
            draft_revision=previewed["draft_revision"],
            draft_hash=previewed["draft_hash"],
            preview_ref=preview["preview_ref"],
            preview_hash=preview["preview_hash"],
            idempotency_key="writing-confirm-reconcile",
        )

        assert replay["run"]["run_ref"] == admitted["run"]["run_ref"]
        assert replay["snapshot"]["snapshot_hash"] == drafted["snapshot"][
            "snapshot_hash"
        ]
    finally:
        runtime.close()


class _SelectivelyFailingWritingSkill(_DeterministicWritingSkill):
    def __init__(self) -> None:
        super().__init__()
        self.fail_broken = True

    def generate_draft(self, request: WritingSkillRequest) -> WritingSkillDraft:
        if self.fail_broken and request.intent["title"] == "永久失败的报告":
            raise WritingSkillUnavailable("writing_test_provider_blocked")
        return super().generate_draft(request)


class _AlwaysRejectedWritingSkill(_RevisionWritingSkill):
    def generate_draft(self, request: WritingSkillRequest) -> WritingSkillDraft:
        draft = super().generate_draft(request)
        return WritingSkillDraft(
            markdown=draft.markdown,
            citations=tuple(
                {**citation, "locator": "missing:forever"}
                for citation in draft.citations
            ),
            primary_session_ref=draft.primary_session_ref,
            adapter_kind=draft.adapter_kind,
        )


class _BlockingWritingSkill(_DeterministicWritingSkill):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def generate_draft(self, request: WritingSkillRequest) -> WritingSkillDraft:
        if request.intent["title"] == "永不返回的报告":
            self.started.set()
            self.release.wait(timeout=5)
        return super().generate_draft(request)


class _ControlledBlockingWritingSkill(_DeterministicWritingSkill):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release_terminal = threading.Event()
        self.cancelled_jobs: list[str] = []

    def generate_draft(self, request: WritingSkillRequest) -> WritingSkillDraft:
        self.started.set()
        self.release_terminal.wait(timeout=5)
        raise WritingSkillUnavailable("codex_cli_stopped")

    def cancel_job(self, job_ref: str) -> None:
        self.cancelled_jobs.append(job_ref)

    def reconcile_cancelled_job(self, job_ref: str) -> bool:
        assert job_ref in self.cancelled_jobs
        return self.release_terminal.is_set()


def test_writing_admission_and_provider_phases_use_the_shared_managed_seam(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "writing-managed-provider-units")
    try:
        quest = _confirm_direct_quest(runtime)
        admitted = _admit_report(
            runtime,
            quest["quest_ref"],
            "writing-managed-provider-units",
        )
        run_ref = admitted["run"]["run_ref"]
        managed = runtime.owners.agent_runtime.query_managed_run(run_ref)
        assert managed is not None
        assert managed["run_kind"] == "writing"
        assert managed["quest_ref"] is None
        assert managed["status"] == "running"
        assert managed["attempt_ref"] == admitted["run"]["attempt_ref"]
        assert managed["root_session_ref"] == admitted["run"]["root_session_ref"]
        assert managed["fence_ref"] == admitted["run"]["fence_ref"]
        assert all(
            item["run_ref"] != run_ref
            for item in runtime.owners.agent_runtime.query_managed_runs(
                quest["quest_ref"]
            )
        )

        assert runtime.writing.process_once()
        assert runtime.writing.process_once()
        with runtime._database.read() as connection:
            units = connection.exec_driver_sql(
                "SELECT unit_kind, status, operation_ref FROM ar_provider_units "
                "WHERE run_ref = ? ORDER BY started_at, unit_kind",
                (run_ref,),
            ).fetchall()
        owner_run = runtime.owners.agent_runtime.query_writing_report(run_ref)
        assert owner_run is not None
        assert units == [
            ("writing_primary", "completed", owner_run.provider_job_ref),
            ("writing_review", "completed", owner_run.provider_job_ref),
        ]
    finally:
        runtime.close()


@pytest.mark.parametrize("phase", ["primary", "review"])
def test_writing_retries_a_lost_provider_safe_ack_from_durable_owner_truth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    provider = _DeterministicWritingSkill()
    runtime = _runtime(tmp_path / f"writing-lost-safe-ack-{phase}", provider)
    try:
        quest = _confirm_direct_quest(runtime)
        admitted = _admit_report(
            runtime,
            quest["quest_ref"],
            f"writing-lost-safe-ack-{phase}",
        )
        run_ref = admitted["run"]["run_ref"]
        if phase == "review":
            assert runtime.writing.process_once()

        original_ack = (
            runtime.owners.agent_runtime.acknowledge_provider_safe_point
        )
        current = runtime.owners.agent_runtime.query_writing_report(run_ref)
        assert current is not None
        target_unit_ref = runtime.writing._provider_unit_ref(
            current, f"writing_{phase}"
        )
        lost = False

        def lose_first_ack(**values) -> None:
            nonlocal lost
            if not lost and values.get("unit_ref") == target_unit_ref:
                lost = True
                raise OwnerConflict("simulated_provider_safe_ack_loss")
            original_ack(**values)

        monkeypatch.setattr(
            runtime.owners.agent_runtime,
            "acknowledge_provider_safe_point",
            lose_first_ack,
        )
        with pytest.raises(OwnerConflict, match="simulated_provider_safe_ack_loss"):
            runtime.writing.process_once()
        owner_after_commit = runtime.owners.agent_runtime.query_writing_report(run_ref)
        assert owner_after_commit is not None
        assert owner_after_commit.status == "active"
        if phase == "primary":
            assert owner_after_commit.checkpoint is not None
            assert owner_after_commit.execution is None
        else:
            assert owner_after_commit.execution is not None
        with runtime._database.read() as connection:
            assert connection.exec_driver_sql(
                "SELECT status FROM ar_provider_units WHERE run_ref = ? AND "
                "unit_kind = ?",
                (run_ref, f"writing_{phase}"),
            ).scalar_one() == "active"

        monkeypatch.setattr(
            runtime.owners.agent_runtime,
            "acknowledge_provider_safe_point",
            original_ack,
        )
        assert runtime.writing.process_once()
        recovered = runtime.owners.agent_runtime.query_writing_report(run_ref)
        assert recovered is not None
        assert recovered.status == "active"
        assert recovered.failure_code is None
        with runtime._database.read() as connection:
            assert connection.exec_driver_sql(
                "SELECT status FROM ar_provider_units WHERE run_ref = ? AND "
                "unit_kind = ?",
                (run_ref, f"writing_{phase}"),
            ).scalar_one() == "completed"
        assert len(provider.draft_requests) == 1
    finally:
        runtime.close()


def test_writing_startup_closes_a_lost_review_ack_before_fencing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = tmp_path / "writing-lost-review-ack-restart"
    provider = _DeterministicWritingSkill()
    runtime = _runtime(data_root, provider)
    reopened = None
    try:
        quest = _confirm_direct_quest(runtime)
        admitted = _admit_report(
            runtime,
            quest["quest_ref"],
            "writing-lost-review-ack-restart",
        )
        run_ref = admitted["run"]["run_ref"]
        assert runtime.writing.process_once()

        original_ack = (
            runtime.owners.agent_runtime.acknowledge_provider_safe_point
        )
        current = runtime.owners.agent_runtime.query_writing_report(run_ref)
        assert current is not None
        review_unit_ref = runtime.writing._provider_unit_ref(
            current, "writing_review"
        )

        def lose_review_ack(**values) -> None:
            if values.get("unit_ref") == review_unit_ref:
                raise OwnerConflict("simulated_provider_safe_ack_loss")
            original_ack(**values)

        monkeypatch.setattr(
            runtime.owners.agent_runtime,
            "acknowledge_provider_safe_point",
            lose_review_ack,
        )
        with pytest.raises(OwnerConflict, match="simulated_provider_safe_ack_loss"):
            runtime.writing.process_once()
        committed = runtime.owners.agent_runtime.query_writing_report(run_ref)
        assert committed is not None
        assert committed.execution is not None
        attempt_ref = committed.attempt_ref
        fence_ref = committed.fence_ref
        runtime.close()
        runtime = None

        reopened = _runtime(data_root, provider)
        recovered = reopened.owners.agent_runtime.query_writing_report(run_ref)
        assert recovered is not None
        assert recovered.attempt_ref == attempt_ref
        assert recovered.fence_ref == fence_ref
        assert recovered.execution is not None
        assert recovered.failure_code is None
        with reopened._database.read() as connection:
            assert connection.exec_driver_sql(
                "SELECT status FROM ar_provider_units WHERE unit_ref = ?",
                (review_unit_ref,),
            ).scalar_one() == "completed"
            assert connection.exec_driver_sql(
                "SELECT COUNT(*) FROM ar_fence_revocations WHERE fence_ref = ?",
                (fence_ref,),
            ).scalar_one() == 0

        assert reopened.writing.process_once()
        continued = reopened.owners.agent_runtime.query_writing_report(run_ref)
        assert continued is not None
        assert continued.status == "active"
        assert continued.failure_code is None
        assert len(provider.draft_requests) == 1
    finally:
        if runtime is not None:
            runtime.close()
        if reopened is not None:
            reopened.close()


@pytest.mark.parametrize("control_kind", ["pause", "timeout"])
def test_writing_control_waits_for_provider_exit_before_replacing_attempt(
    tmp_path: Path,
    control_kind: str,
) -> None:
    provider = _ControlledBlockingWritingSkill()
    runtime = _runtime(tmp_path / f"writing-provider-stop-{control_kind}", provider)
    try:
        quest = _confirm_direct_quest(runtime)
        admitted = _admit_report(
            runtime,
            quest["quest_ref"],
            f"writing-provider-stop-{control_kind}",
        )
        run_ref = admitted["run"]["run_ref"]
        old_attempt_ref = admitted["run"]["attempt_ref"]
        old_fence_ref = admitted["run"]["fence_ref"]
        owner_before = runtime.owners.agent_runtime.query_writing_report(run_ref)
        assert owner_before is not None
        errors: list[BaseException] = []

        def execute_provider() -> None:
            try:
                runtime.writing.process_once()
            except BaseException as error:  # pragma: no cover - asserted below
                errors.append(error)

        worker = threading.Thread(target=execute_provider, daemon=True)
        worker.start()
        assert provider.started.wait(timeout=2)

        if control_kind == "pause":
            controlled = runtime.writing.control_report(
                run_ref,
                action="pause",
                idempotency_key="writing-managed-provider-pause",
            )
            assert controlled["status"] == "paused"
        else:
            runtime.writing.block_writing_claim(
                run_ref=run_ref,
                attempt_ref=old_attempt_ref,
                fence_ref=old_fence_ref,
            )
            assert runtime.writing.query_writing_report(run_ref)["status"] == "blocked"
        assert provider.cancelled_jobs == [owner_before.provider_job_ref]
        managed = runtime.owners.agent_runtime.query_managed_run(run_ref)
        assert managed is not None
        assert managed["cleanup_status"] == "pending"
        assert managed["status"] in {"suspended_fenced", "reconciliation_required"}
        with runtime._database.read() as connection:
            unit_status = connection.exec_driver_sql(
                "SELECT status FROM ar_provider_units WHERE run_ref = ?",
                (run_ref,),
            ).scalar_one()
            revoked = connection.exec_driver_sql(
                "SELECT reason_code FROM ar_fence_revocations WHERE fence_ref = ?",
                (old_fence_ref,),
            ).scalar_one()
        assert unit_status == "revocation_pending"
        assert revoked in {"writing_paused", "writing_operation_timeout"}
        with pytest.raises(OwnerConflict, match="runtime_quiescence_pending"):
            runtime.writing.control_report(
                run_ref,
                action="resume",
                idempotency_key=f"writing-provider-stop-{control_kind}-early-resume",
            )

        provider.release_terminal.set()
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert errors == []
        cleaned = runtime.owners.agent_runtime.query_managed_run(run_ref)
        assert cleaned is not None
        assert cleaned["cleanup_status"] == "completed"
        with runtime._database.read() as connection:
            assert connection.exec_driver_sql(
                "SELECT status FROM ar_provider_units WHERE run_ref = ?",
                (run_ref,),
            ).scalar_one() == "revoked"

        resumed = runtime.writing.control_report(
            run_ref,
            action="resume",
            idempotency_key=f"writing-provider-stop-{control_kind}-resume",
        )
        assert resumed["run"]["attempt_ref"] != old_attempt_ref
        assert resumed["run"]["fence_ref"] != old_fence_ref
        owner_after = runtime.owners.agent_runtime.query_writing_report(run_ref)
        assert owner_after is not None
        assert owner_after.provider_job_ref != owner_before.provider_job_ref
    finally:
        provider.release_terminal.set()
        runtime.close()


def test_writing_restart_reuses_the_sealed_operation_without_attempt_churn(
    tmp_path: Path,
) -> None:
    root = tmp_path / "writing-managed-restart"
    provider = _DeterministicWritingSkill()
    runtime = _runtime(root, provider)
    quest = _confirm_direct_quest(runtime)
    admitted = _admit_report(runtime, quest["quest_ref"], "writing-managed-restart")
    run_ref = admitted["run"]["run_ref"]
    original = runtime.owners.agent_runtime.query_writing_report(run_ref)
    assert original is not None
    unit_ref = "provider_unit_" + canonical_hash(
        {
            "provider_job_ref": original.provider_job_ref,
            "attempt_ref": original.attempt_ref,
            "unit_kind": "writing_primary",
        }
    )[:64]
    runtime.owners.agent_runtime.begin_provider_unit(
        unit_ref=unit_ref,
        operation_ref=original.provider_job_ref,
        run_ref=run_ref,
        attempt_ref=original.attempt_ref,
        fence_ref=original.fence_ref,
        unit_kind="writing_primary",
    )
    runtime.close()

    restarted = _runtime(root, provider)
    recovered = restarted.owners.agent_runtime.query_writing_report(run_ref)
    assert recovered is not None
    assert recovered.attempt_generation == original.attempt_generation + 1
    assert recovered.provider_job_ref == original.provider_job_ref
    assert recovered.attempt_ref != original.attempt_ref
    assert recovered.fence_ref != original.fence_ref
    assert restarted.owners.agent_runtime.query_managed_run(run_ref)[
        "cleanup_status"
    ] == "none"
    restarted.close()

    restarted_again = _runtime(root, provider)
    try:
        stable = restarted_again.owners.agent_runtime.query_writing_report(run_ref)
        assert stable is not None
        assert stable.attempt_ref == recovered.attempt_ref
        assert stable.attempt_generation == recovered.attempt_generation
        assert stable.provider_job_ref == original.provider_job_ref

        assert restarted_again.writing.process_once()
        with restarted_again._database.read() as connection:
            statuses = connection.exec_driver_sql(
                "SELECT attempt_ref, status, operation_ref FROM ar_provider_units "
                "WHERE run_ref = ? ORDER BY started_at, unit_ref",
                (run_ref,),
            ).fetchall()
        assert statuses == [
            (original.attempt_ref, "revoked", original.provider_job_ref),
            (stable.attempt_ref, "completed", original.provider_job_ref),
        ]
    finally:
        restarted_again.close()


def test_provider_failure_is_durable_does_not_starve_later_runs_and_can_resume(
    tmp_path: Path,
) -> None:
    root = tmp_path / "writing-durable-failure"
    provider = _SelectivelyFailingWritingSkill()
    runtime = _runtime(root, provider)
    try:
        quest = _confirm_direct_quest(runtime)
        broken = _admit_report(
            runtime,
            quest["quest_ref"],
            "writing-failure-broken",
            title="永久失败的报告",
        )
        healthy = _admit_report(
            runtime,
            quest["quest_ref"],
            "writing-failure-healthy",
            title="后续健康报告",
        )
        broken_ref = broken["run"]["run_ref"]
        healthy_ref = healthy["run"]["run_ref"]

        assert runtime.writing.process_once()
        blocked = runtime.writing.query_writing_report(broken_ref)
        assert blocked["status"] == "blocked"
        assert blocked["run"]["blocker"] == {
            "code": "writing_test_provider_blocked"
        }
        assert runtime.writing.process_once()
        assert runtime.writing.query_writing_report(healthy_ref)["execution"][
            "status"
        ] == "running"
    finally:
        runtime.close()

    restarted = _runtime(root, provider)
    try:
        blocked = restarted.writing.query_writing_report(broken_ref)
        assert blocked["status"] == "blocked"
        assert blocked["run"]["blocker"] == {
            "code": "writing_test_provider_blocked"
        }
        provider.fail_broken = False
        resumed = restarted.writing.control_report(
            broken_ref,
            action="resume",
            idempotency_key="writing-failure-resume",
        )
        assert resumed["status"] == "running"
        assert resumed["run"]["attempt_generation"] == 2
        assert resumed["run"]["blocker"] is None
    finally:
        restarted.close()


def test_failed_rm_delivery_blocks_only_its_run_and_does_not_starve_later_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path / "writing-rm-failure")
    try:
        quest = _confirm_direct_quest(runtime)
        broken = _admit_report(
            runtime,
            quest["quest_ref"],
            "writing-rm-failure-broken",
            title="RM 永久失败报告",
        )
        healthy = _admit_report(
            runtime,
            quest["quest_ref"],
            "writing-rm-failure-healthy",
            title="RM 后续健康报告",
        )
        for _boundary in range(2):
            current = runtime.owners.agent_runtime.query_writing_report(
                broken["run"]["run_ref"]
            )
            assert current is not None
            assert runtime.writing.process_once(
                expected_run_ref=current.run_ref,
                expected_attempt_ref=current.attempt_ref,
                expected_fence_ref=current.fence_ref,
            )
        real_submit = runtime.owners.research_memory.submit_asset_intake
        monkeypatch.setattr(
            runtime.owners.research_memory,
            "submit_asset_intake",
            lambda *_args, **_kwargs: AssetIntakeResult(
                job_ref="asset_job:writing-rm-failed",
                status="failed",
                source_kind="text",
                custody_mode="managed",
                attempt_count=3,
                asset=None,
                failure_code="asset_intake_retry_exhausted",
            ),
        )

        current = runtime.owners.agent_runtime.query_writing_report(
            broken["run"]["run_ref"]
        )
        assert current is not None
        assert runtime.writing.process_once(
            expected_run_ref=current.run_ref,
            expected_attempt_ref=current.attempt_ref,
            expected_fence_ref=current.fence_ref,
        )
        blocked = runtime.writing.query_writing_report(
            broken["run"]["run_ref"]
        )
        assert blocked["status"] == "blocked"
        assert blocked["run"]["blocker"] == {
            "code": "writing_deliverable:asset_intake_retry_exhausted"
        }
        assert runtime.writing.next_runnable_claim() == (
            healthy["run"]["run_ref"],
            healthy["run"]["attempt_ref"],
            healthy["run"]["fence_ref"],
        )

        monkeypatch.setattr(
            runtime.owners.research_memory,
            "submit_asset_intake",
            real_submit,
        )
        assert runtime.writing.process_once()
        assert runtime.writing.query_writing_report(
            healthy["run"]["run_ref"]
        )["execution"]["status"] == "running"
    finally:
        runtime.close()


def test_crash_fence_replacements_do_not_consume_content_revision_budget(
    tmp_path: Path,
) -> None:
    root = tmp_path / "writing-replacement-budget"
    provider = _DeterministicWritingSkill()
    runtime = _runtime(root, provider)
    quest = _confirm_direct_quest(runtime)
    admitted = _admit_report(
        runtime,
        quest["quest_ref"],
        "writing-replacement-budget",
    )
    run_ref = admitted["run"]["run_ref"]
    for _step in range(4):
        assert runtime.writing.process_once()
    assert runtime.writing.query_writing_report(run_ref)["citation"][
        "status"
    ] == "accepted"
    runtime.writing.request_revision(
        run_ref,
        feedback=("形成第二版。",),
        idempotency_key="writing-replacement-budget-r2",
    )
    assert runtime.writing.process_once()

    for _restart in range(5):
        runtime.close()
        runtime = _runtime(root, provider)

    try:
        for _step in range(3):
            assert runtime.writing.process_once()
        second = runtime.writing.query_writing_report(run_ref)
        assert second["citation"]["status"] == "accepted"
        assert len(second["versions"]) == 2

        third = runtime.writing.request_revision(
            run_ref,
            feedback=("形成第三版。",),
            idempotency_key="writing-replacement-budget-r3",
        )
        assert third["run"]["content_revision"] == 3
    finally:
        runtime.close()

def test_watchdog_retires_a_stuck_fence_and_allows_the_next_run_to_advance(
    tmp_path: Path,
) -> None:
    provider = _BlockingWritingSkill()
    runtime = _runtime(tmp_path / "writing-stuck-provider", provider)
    try:
        quest = _confirm_direct_quest(runtime)
        stuck = _admit_report(
            runtime,
            quest["quest_ref"],
            "writing-stuck-provider",
            title="永不返回的报告",
        )
        healthy = _admit_report(
            runtime,
            quest["quest_ref"],
            "writing-after-stuck-provider",
            title="后续可运行报告",
        )
        worker = threading.Thread(target=runtime.writing.process_once, daemon=True)
        worker.start()
        assert provider.started.wait(timeout=1)

        claim = runtime.writing.next_runnable_claim()
        assert claim == (
            stuck["run"]["run_ref"],
            stuck["run"]["attempt_ref"],
            stuck["run"]["fence_ref"],
        )
        runtime.writing.block_writing_claim(
            run_ref=claim[0],
            attempt_ref=claim[1],
            fence_ref=claim[2],
        )
        blocked = runtime.writing.query_writing_report(stuck["run"]["run_ref"])
        assert blocked["status"] == "blocked"
        assert blocked["run"]["blocker"] == {
            "code": "writing_operation_timeout"
        }

        assert runtime.writing.process_once()
        advanced = runtime.writing.query_writing_report(
            healthy["run"]["run_ref"]
        )
        assert advanced["execution"]["status"] == "running"

        provider.release.set()
        worker.join(timeout=1)
        assert not worker.is_alive()
        assert runtime.owners.research_graph.query_writing_citation_history(
            stuck["run"]["run_ref"]
        ) == ()
        assert runtime.writing.query_writing_report(stuck["run"]["run_ref"])[
            "deliverable"
        ] == {"status": "not_attempted"}
    finally:
        provider.release.set()
        runtime.close()


def test_watchdog_timeout_cannot_block_the_next_run_after_claim_is_paused(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "writing-stale-watchdog-claim")
    try:
        quest = _confirm_direct_quest(runtime)
        first = _admit_report(
            runtime,
            quest["quest_ref"],
            "writing-stale-watchdog-first",
            title="即将暂停的报告",
        )
        second = _admit_report(
            runtime,
            quest["quest_ref"],
            "writing-stale-watchdog-second",
            title="不得误伤的报告",
        )
        claim = runtime.writing.next_runnable_claim()
        assert claim == (
            first["run"]["run_ref"],
            first["run"]["attempt_ref"],
            first["run"]["fence_ref"],
        )

        runtime.writing.control_report(
            first["run"]["run_ref"],
            action="pause",
            idempotency_key="writing-stale-watchdog-pause",
        )
        runtime.writing.block_writing_claim(
            run_ref=claim[0],
            attempt_ref=claim[1],
            fence_ref=claim[2],
        )

        untouched = runtime.writing.query_writing_report(
            second["run"]["run_ref"]
        )
        assert untouched["status"] == "running"
        assert untouched["run"]["blocker"] is None
        assert runtime.writing.next_runnable_claim() == (
            second["run"]["run_ref"],
            second["run"]["attempt_ref"],
            second["run"]["fence_ref"],
        )
    finally:
        runtime.close()


def test_rejected_citations_stop_at_the_frozen_revision_budget(
    tmp_path: Path,
) -> None:
    provider = _AlwaysRejectedWritingSkill()
    runtime = _runtime(tmp_path / "writing-revision-budget", provider)
    try:
        quest = _confirm_direct_quest(runtime)
        source = runtime.owners.research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="text",
                custody_mode="managed",
                display_name="revision-budget-source.txt",
                media_type="text/plain; charset=utf-8",
                content=b"rare morphology remains visible\n",
            ),
            idempotency_key="writing-revision-budget-source",
        )
        assert source.asset is not None
        runtime.owners.research_graph.accept_asset_role(
            binding=source.asset.as_binding(),
            role="evidence",
            quest_ref=quest["quest_ref"],
            idempotency_key="writing-revision-budget-role",
        )
        provider.source_version_ref = source.asset.version_ref
        admitted = _admit_report(
            runtime, quest["quest_ref"], "writing-revision-budget"
        )
        run_ref = admitted["run"]["run_ref"]

        for _step in range(40):
            if not runtime.writing.process_once():
                break
            if runtime.writing.query_writing_report(run_ref)["status"] == "blocked":
                break

        blocked = runtime.writing.query_writing_report(run_ref)
        assert blocked["status"] == "blocked"
        assert blocked["run"]["blocker"] == {
            "code": "writing_revision_budget_exhausted"
        }
        assert len(blocked["versions"]) == blocked["run"]["execution_budget"][
            "max_content_revisions"
        ]
        with pytest.raises(
            OwnerConflict, match="writing_revision_budget_exhausted"
        ):
            runtime.writing.control_report(
                run_ref,
                action="resume",
                idempotency_key="writing-revision-budget-resume",
            )
    finally:
        runtime.close()


def test_overview_keeps_unadmitted_human_intents_recoverable(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "writing-pending-overview")
    try:
        quest = _confirm_direct_quest(runtime)
        drafted = runtime.writing.create_report_intent(
            quest_ref=quest["quest_ref"],
            title="待恢复报告",
            audience="研究负责人",
            purpose="验证 HITL 恢复入口",
            instructions="刷新后仍可继续。",
            idempotency_key="writing-pending-overview-create",
        )

        overview = runtime.writing.query_overview()

        pending = next(
            item for item in overview["runs"] if item["intent_id"] == drafted["intent_id"]
        )
        assert pending["status"] == "draft"
        assert pending["run"] is None
    finally:
        runtime.close()


def test_overview_never_drops_an_old_active_run_behind_newer_intents(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "writing-overview-no-truncation")
    try:
        quest = _confirm_direct_quest(runtime)
        admitted = _admit_report(
            runtime,
            quest["quest_ref"],
            "writing-overview-old-run",
            title="必须始终可控制的旧 Run",
        )
        old_run_ref = admitted["run"]["run_ref"]
        for index in range(256):
            runtime.writing.create_report_intent(
                quest_ref=quest["quest_ref"],
                title=f"较新的待确认报告 {index}",
                audience="研究负责人",
                purpose="验证长期 Writing 枚举不截断",
                instructions="保持为待确认 Intent。",
                idempotency_key=f"writing-overview-new-{index}",
            )

        overview = runtime.writing.query_overview()

        projected = next(
            item
            for item in overview["runs"]
            if (item.get("run") or {}).get("run_ref") == old_run_ref
        )
        assert len(overview["runs"]) == 257
        assert projected["status"] == "running"
        assert projected["run"]["run_ref"] == old_run_ref
    finally:
        runtime.close()
