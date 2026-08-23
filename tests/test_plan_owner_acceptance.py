from __future__ import annotations

from pathlib import Path

from meta_research.owners.common import canonical_hash
from test_public_plan_stage import (
    _DeterministicIdeaSkill,
    _DeterministicPlanSkill,
    _confirm_direct_quest,
    _finish_idea_stage,
    _runtime,
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
