from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from meta_research.owners.common import OwnerConflict, canonical_hash
from test_plan_generic_sources import _GenericPlan
from test_public_plan_stage import (
    _DeterministicIdeaSkill,
    _DeterministicPlanSkill,
    _confirm_direct_quest,
    _finish_idea_stage,
    _runtime,
)
from test_public_reasoning_stage import (
    _DeterministicReasoningSkill,
    _confirm_deepfetch_quest,
    _reasoning_runtime,
)


def test_plan_owner_public_types_are_available() -> None:
    from meta_research.owners.research_graph import FormalPlanDecision
    from meta_research.owners.research_memory import AcceptedPlanDocument

    assert AcceptedPlanDocument.__name__ == "AcceptedPlanDocument"
    assert FormalPlanDecision.__name__ == "FormalPlanDecision"


class _QuestionRestatingPlanSkill(_DeterministicPlanSkill):
    def __init__(self) -> None:
        super().__init__(no_gap=False)

    def _document(self, request):
        document = super()._document(request)
        contract = document["answer_contract"]
        contract["obligations"][0]["statement"] = request.accepted_question_content[
            "unknown_statement"
        ]
        without_hash = {
            key: value
            for key, value in contract.items()
            if key != "answer_contract_hash"
        }
        contract["answer_contract_hash"] = canonical_hash(without_hash)
        return document


def _advance_to_domain_decision(runtime):
    for _step in range(10):
        current = runtime.plan_stage.query_current()
        status = current["plan_acceptance"]["domain"]["status"]
        if status in {"accepted", "rejected"}:
            request_ref = current["stage_run_request"]["request_ref"]
            run = runtime.owners.agent_runtime.query_plan_stage_run(request_ref)
            assert run is not None and run.execution is not None
            decision = runtime.owners.research_graph.query_formal_plan_decision(
                run.execution.submission_ref
            )
            assert decision is not None
            return current, decision, run.execution.submission_ref
        assert runtime.plan_stage.process_once()
    raise AssertionError("Plan domain decision was not reached")


def test_plan_document_and_formal_plan_are_separate_issuer_verified_facts(
    tmp_path: Path,
) -> None:
    runtime = _runtime(
        tmp_path / "accepted-plan",
        idea_skill=_DeterministicIdeaSkill(),
        plan_skill=_DeterministicPlanSkill(no_gap=False),
    )
    try:
        _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)

        current, decision, submission_ref = _advance_to_domain_decision(runtime)
        plan = runtime.owners.research_memory.query_plan_document(submission_ref)

        assert current["plan_acceptance"]["content"]["status"] == "accepted"
        assert current["plan_acceptance"]["domain"]["status"] == "accepted"
        assert plan is not None
        assert plan.receipt.issuer == "research_memory"
        assert plan.receipt.kind == "plan_document_content_acceptance"
        assert decision.decision == "accepted"
        assert decision.formal_plan_ref is not None
        assert decision.receipt.issuer == "research_graph"
        assert decision.receipt.kind == "formal_plan_accepted"
        assert decision.plan_document_hash == plan.plan_document_hash
        assert runtime.owners.research_memory.query_snapshot().facts[
            "plan_content_count"
        ] == 1
        assert runtime.owners.research_graph.query_snapshot().facts[
            "formal_plan_count"
        ] == 1
    finally:
        runtime.close()


def test_rg_rejects_question_restatement_with_structured_feedback(
    tmp_path: Path,
) -> None:
    runtime = _runtime(
        tmp_path / "rejected-plan",
        idea_skill=_DeterministicIdeaSkill(),
        plan_skill=_QuestionRestatingPlanSkill(),
    )
    try:
        _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)

        current, decision, submission_ref = _advance_to_domain_decision(runtime)
        plan = runtime.owners.research_memory.query_plan_document(submission_ref)

        assert plan is not None
        assert current["plan_acceptance"]["content"]["status"] == "accepted"
        assert current["plan_acceptance"]["domain"]["status"] == "rejected"
        assert decision.decision == "rejected"
        assert decision.formal_plan_ref is None
        assert decision.reason_code == "question_obligation_restatement"
        assert decision.feedback == (
            "AnswerContract obligation merely restates an accepted Question field; "
            "rewrite it as a concrete answer obligation with a distinct support "
            "threshold.",
        )
        assert decision.receipt.kind == "formal_plan_rejected"
        assert runtime.owners.research_graph.query_snapshot().facts[
            "plan_rejection_count"
        ] == 1
        assert runtime.owners.research_graph.query_snapshot().facts[
            "formal_plan_count"
        ] == 0
    finally:
        runtime.close()


class _DoiInsteadOfSnapshotPlan(_GenericPlan):
    def __init__(self) -> None:
        super().__init__("LiteratureSnapshot")

    def _document(self, request):
        document = super()._document(request)
        if not request.owner_feedback:
            selected = document["source_bindings"]["selected_evidence_catalog"][0]
            wrong_ref = "doi:10.1234/read-paper"
            selected["evidence_ref"] = selected["source_ref"] = wrong_ref
            for use in document["evidence_reuse_set"]:
                use["evidence_ref"] = wrong_ref
            for coverage in document["coverage"]:
                for use in coverage["evidence_uses"]:
                    use["evidence_ref"] = wrong_ref
        return document


def test_invalid_plan_source_is_rejected_and_corrected_in_same_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_skill = _DoiInsteadOfSnapshotPlan()
    runtime = _reasoning_runtime(
        tmp_path / "incorrect-literature-reference",
        reasoning_skill=_DeterministicReasoningSkill(),
        idea_skill=_DeterministicIdeaSkill(),
        plan_skill=plan_skill,
    )
    plan_skill.runtime = runtime
    try:
        _confirm_deepfetch_quest(runtime)
        _finish_idea_stage(runtime)

        current, decision, submission_ref = _advance_to_domain_decision(runtime)
        original_run = current["run"]
        assert decision.decision == "rejected"
        assert decision.reason_code == "plan_evidence_source_invalid"
        assert decision.formal_plan_ref is None
        assert decision.receipt.kind == "formal_plan_rejected"
        assert "literature_snapshot_ref" in " ".join(decision.feedback)
        assert "DOI" in " ".join(decision.feedback)
        rejected_content = runtime.owners.research_memory.query_plan_document(
            submission_ref
        )
        assert rejected_content is not None

        assert runtime.plan_stage.process_once()
        continued = runtime.plan_stage.query_current()["run"]
        assert continued["run_ref"] == original_run["run_ref"]
        assert continued["root_session_ref"] == original_run["root_session_ref"]
        assert continued["native_session_ref"] == original_run["native_session_ref"]
        assert continued["attempt_ref"] != original_run["attempt_ref"]
        assert continued["fence_ref"] != original_run["fence_ref"]
        assert continued["attempt_generation"] == 2
        for _step in range(12):
            completed = runtime.plan_stage.query_current()
            if completed["stage_commit"] is not None:
                break
            assert runtime.plan_stage.process_once()
        else:
            raise AssertionError("Corrected Plan did not commit")

        assert completed["plan_acceptance"]["domain"]["status"] == "accepted"
        assert plan_skill.requests[-1].owner_feedback == decision.feedback
        assert plan_skill.requests[-1].native_session_ref == original_run[
            "native_session_ref"
        ]
        assert runtime.owners.research_memory.query_plan_document(
            submission_ref
        ) == rejected_content
        assert runtime.owners.research_graph.query_formal_plan_decision(
            submission_ref
        ) == decision
        facts = runtime.owners.research_graph.query_snapshot().facts
        assert facts["plan_rejection_count"] == facts["formal_plan_count"] == 1

        graph = runtime.owners.research_graph
        with pytest.raises(OwnerConflict, match="formal_plan_receipt_invalid"):
            graph._receipt_verifier.verify_formal_plan_decision(
                request_ref=decision.request_ref,
                submission_ref=submission_ref,
                decision="rejected",
                formal_plan_ref=None,
                receipt=replace(decision.receipt, payload_hash="0" * 64),
            )

        # Only this source error on its signed rejection may be read back.
        # Accepted decisions and unrelated integrity errors still fail closed.
        def unavailable_source(**_kwargs):
            raise OwnerConflict("plan_evidence_source_invalid")

        with monkeypatch.context() as patch:
            patch.setattr(
                graph._receipt_verifier,
                "verify_plan_evidence_catalog",
                unavailable_source,
            )
            with pytest.raises(OwnerConflict, match="plan_evidence_source_invalid"):
                graph.query_formal_plan_decision(completed["run"]["submission_ref"])

        def invalid_receipt(**_kwargs):
            raise OwnerConflict("literature_snapshot_receipt_invalid")

        with monkeypatch.context() as patch:
            patch.setattr(
                graph._receipt_verifier,
                "verify_plan_evidence_catalog",
                invalid_receipt,
            )
            with pytest.raises(OwnerConflict, match="literature_snapshot_receipt_invalid"):
                graph.query_formal_plan_decision(submission_ref)
    finally:
        runtime.close()
