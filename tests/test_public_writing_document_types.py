from __future__ import annotations

from pathlib import Path
import zipfile
from io import BytesIO

import pytest

from meta_research.writing_skill import (
    WritingSkillDraft,
    WritingSkillRequest,
    WritingSkillResult,
    writing_review_task_hash,
)
from test_public_writing_report import (
    _DeterministicWritingSkill,
    _confirm_direct_quest,
    _runtime,
)


class _DocumentWritingSkill(_DeterministicWritingSkill):
    def _document(self, request: WritingSkillRequest) -> str:
        if request.document_type == "paper":
            blocks: list[str] = [f"# Paper revision {request.revision}"]
            for role, heading in (
                ("abstract", "Abstract"),
                ("framing", "Introduction"),
                ("methods", "Methods"),
                ("results", "Results"),
                ("discussion", "Discussion"),
                ("conclusion", "Conclusion"),
            ):
                blocks.extend(
                    (
                        "<!-- meta-research-paper-section "
                        f"role={role} -->\n## {heading}",
                        "<!-- meta-research-claim:uncertainty -->\n"
                        f"**Uncertainty:** {heading} remains bounded by the frozen Snapshot.",
                    )
                )
            return "\n\n".join(blocks) + "\n"
        if request.document_type == "presentation":
            return (
                f"# Deck revision {request.revision}\n\n"
                "<!-- meta-research-structure -->\n"
                "## Slide 1: Evidence boundary\n\n"
                "<!-- meta-research-claim:evidence-gap -->\n"
                "**Evidence gap:** The frozen Snapshot has no accepted source.\n\n"
                "<!-- meta-research-structure -->\n"
                "## Slide 2: Decision\n\n"
                "<!-- meta-research-claim:uncertainty -->\n"
                "**Uncertainty:** No external decision is justified yet.\n"
            )
        return super().generate_draft(request).markdown

    def generate_draft(self, request: WritingSkillRequest) -> WritingSkillDraft:
        self.draft_requests.append(request)
        return WritingSkillDraft(
            markdown=self._document(request),
            citations=(),
            primary_session_ref=request.native_session_ref or "document-session",
            adapter_kind="test_document_profile",
        )

    def review_draft(
        self, request: WritingSkillRequest, draft: WritingSkillDraft
    ) -> WritingSkillResult:
        return WritingSkillResult(
            reviewed_markdown=draft.markdown,
            final_markdown=draft.markdown,
            citations=draft.citations,
            findings=(),
            dispositions=(),
            primary_session_ref=draft.primary_session_ref,
            review_mode="harness_child_agent",
            reviewer_agent_ref=f"document-reviewer-{request.revision}",
            review_task_hash=writing_review_task_hash(request, draft),
            adapter_kind=draft.adapter_kind,
        )


@pytest.mark.parametrize(
    ("document_type", "output_format", "package_member"),
    (
        ("paper", "docx", "word/document.xml"),
        ("presentation", "pptx", "ppt/presentation.xml"),
    ),
)
def test_paper_and_presentation_share_one_writing_run_and_render_real_artifacts(
    tmp_path: Path,
    document_type: str,
    output_format: str,
    package_member: str,
) -> None:
    provider = _DocumentWritingSkill()
    runtime = _runtime(tmp_path / document_type, provider)
    try:
        quest = _confirm_direct_quest(runtime)
        drafted = runtime.writing.create_intent(
            document_type=document_type,
            quest_ref=quest["quest_ref"],
            title=f"Exact {document_type} title",
            audience="Exact review audience",
            purpose="Exact evidence-bounded purpose",
            instructions="Preserve the frozen Snapshot and visible uncertainty.",
            idempotency_key=f"{document_type}-create",
        )
        previewed = runtime.writing.preview_intent(
            drafted["intent_id"], idempotency_key=f"{document_type}-preview"
        )
        preview = previewed["impact_preview"]
        admitted = runtime.writing.confirm_intent(
            drafted["intent_id"],
            draft_revision=previewed["draft_revision"],
            draft_hash=previewed["draft_hash"],
            preview_ref=preview["preview_ref"],
            preview_hash=preview["preview_hash"],
            idempotency_key=f"{document_type}-confirm",
        )
        run_ref = admitted["run"]["run_ref"]
        root_session_ref = admitted["run"]["root_session_ref"]

        for _step in range(5):
            assert runtime.writing.process_once()
        completed = runtime.writing.query_writing_report(run_ref)

        assert completed["document_type"] == document_type
        assert completed["run"]["document_type"] == document_type
        assert completed["run"]["root_session_ref"] == root_session_ref
        assert completed["citation"]["status"] == "accepted"
        assert completed["renderer"]["status"] == "ready"
        assert completed["renderer"]["default_format"] == output_format
        assert completed["renderer"]["artifact"]["receipt"]["issuer"] == (
            "research_memory"
        )
        assert provider.draft_requests[0].document_type == document_type

        inventory_before = tuple(
            (item.version_ref, item.receipt)
            for item in runtime.owners.research_memory.query_asset_inventory()
        )
        first = runtime.writing.render_report(run_ref)
        second = runtime.writing.render_report(run_ref, format=output_format)
        assert first == second
        assert first["format"] == output_format
        assert first["content"].startswith(b"PK")
        with zipfile.ZipFile(BytesIO(first["content"])) as package:
            assert package_member in package.namelist()
        assert tuple(
            (item.version_ref, item.receipt)
            for item in runtime.owners.research_memory.query_asset_inventory()
        ) == inventory_before
    finally:
        runtime.close()


def test_document_type_is_not_a_second_session_or_future_version_authority(
    tmp_path: Path,
) -> None:
    provider = _DocumentWritingSkill()
    runtime = _runtime(tmp_path / "paper-revision", provider)
    try:
        quest = _confirm_direct_quest(runtime)
        drafted = runtime.writing.create_intent(
            document_type="paper",
            quest_ref=quest["quest_ref"],
            title="Revision paper",
            audience="Reviewer",
            purpose="Test one-session lineage",
            instructions="Keep exact lineage.",
            idempotency_key="paper-revision-create",
        )
        previewed = runtime.writing.preview_intent(
            drafted["intent_id"], idempotency_key="paper-revision-preview"
        )
        preview = previewed["impact_preview"]
        admitted = runtime.writing.confirm_intent(
            drafted["intent_id"],
            draft_revision=previewed["draft_revision"],
            draft_hash=previewed["draft_hash"],
            preview_ref=preview["preview_ref"],
            preview_hash=preview["preview_hash"],
            idempotency_key="paper-revision-confirm",
        )
        run_ref = admitted["run"]["run_ref"]
        for _step in range(5):
            assert runtime.writing.process_once()
        first = runtime.writing.query_writing_report(run_ref)
        first_render = runtime.writing.render_report(
            run_ref,
            version_ref=first["deliverable"]["version_ref"],
        )
        runtime.writing.request_revision(
            run_ref,
            feedback=("Make the bounded result explicit in revision two.",),
            idempotency_key="paper-revision-request",
        )

        # Starting a successor attempt must not make the immutable, accepted
        # predecessor impossible to render while the new attempt is still in
        # progress.
        historical_during_revision = runtime.writing.render_report(
            run_ref,
            version_ref=first["deliverable"]["version_ref"],
        )
        assert historical_during_revision == first_render

        for _step in range(5):
            assert runtime.writing.process_once()
        second = runtime.writing.query_writing_report(run_ref)

        assert second["run"]["run_ref"] == first["run"]["run_ref"]
        assert second["run"]["root_session_ref"] == first["run"]["root_session_ref"]
        assert second["run"]["native_session_ref"] == first["run"]["native_session_ref"]
        assert second["deliverable"]["version_number"] == 2
        compared = runtime.writing.compare_report_versions(
            run_ref,
            left_version_ref=first["deliverable"]["version_ref"],
            right_version_ref=second["deliverable"]["version_ref"],
        )
        assert compared["content"]["changed"] is True
        assert compared["evidence"]["changed"] is False
        assert compared["citation"]["changed"] is False
    finally:
        runtime.close()
