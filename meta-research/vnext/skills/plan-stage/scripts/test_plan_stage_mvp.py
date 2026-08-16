#!/usr/bin/env python3
"""Contract tests for the deterministic Plan Stage reference model."""

from __future__ import annotations

from dataclasses import fields, replace

from plan_stage_mvp import (
    AcceptedIdeaSetBinding,
    AdvisoryReviewRecord,
    AnswerContract,
    ContractViolation,
    CoverageDecision,
    EvidenceQueryResult,
    EvidenceRef,
    EvidenceUse,
    ExperimentBrief,
    FakePlanPort,
    FindingDisposition,
    OwnerResult,
    PlanBlocked,
    PlanStageRunRequest,
    ReviewFinding,
    _semantic_plan_hash,
    accepted_owner_results,
    all_covered_scenario,
    compile_plan,
    gap_scenario,
    make_answer_contract,
    make_evidence,
    make_request,
    make_review,
    stale_repair_scenario,
    submit_plan,
)


def expect_failure(action, error_type, expected_text: str) -> None:
    try:
        action()
    except error_type as exc:
        assert expected_text in str(exc), (expected_text, str(exc))
    else:
        raise AssertionError("expected {}".format(error_type.__name__))


def test_all_covered_forms_skip_basis_after_both_owner_receipts() -> None:
    result, plan, port = all_covered_scenario()
    assert result.status == "accepted"
    assert plan.bundle_disposition == "no_new_experiment_required"
    assert plan.gap_set == ()
    assert plan.experiment_briefs == ()
    assert result.bundle_skip_basis is not None
    assert (
        result.bundle_skip_basis.rm_plan_content_receipt_ref
        == result.rm_plan_content_receipt_ref
    )
    assert (
        result.bundle_skip_basis.rg_formal_plan_receipt_ref
        == result.rg_formal_plan_receipt_ref
    )
    assert port.calls[-2:] == ["submit_plan_content", "submit_formal_plan"]
    assert not any("target" in call.lower() for call in port.calls)


def test_gap_generates_brief_only_for_gap_and_no_skip_basis() -> None:
    result, plan, port = gap_scenario()
    assert result.status == "accepted"
    assert result.bundle_skip_basis is None
    assert plan.bundle_disposition == "experiments_required"
    assert plan.gap_set == ("limits",)
    assert len(plan.experiment_briefs) == 1
    assert plan.experiment_briefs[0].gap_obligation_keys == ("limits",)
    normalized = plan.normalized_content()
    assert "targets" not in normalized
    assert "selected_idea" not in normalized
    assert not any("target" in call.lower() for call in port.calls)


def test_stale_snapshot_repairs_in_same_contract_and_request() -> None:
    result, plan, port = stale_repair_scenario()
    assert result.status == "accepted"
    assert [query.mode for query in port.queries] == [
        "open",
        "refresh",
        "follow",
        "follow",
    ]
    assert len({query.stage_request_ref for query in port.queries}) == 1
    assert len({query.answer_contract_hash for query in port.queries}) == 1
    assert plan.search_snapshot_token == "fixture:search-snapshot:fresh-s2"


def test_stage_request_freezes_sources_not_answer_contract() -> None:
    field_names = {field.name for field in fields(PlanStageRunRequest)}
    assert "answer_contract" not in field_names
    assert "obligations" not in field_names
    assert {"question", "idea_set", "search_boundary_ref"} <= field_names


def test_every_obligation_accounts_for_complete_idea_set() -> None:
    request = make_request()
    contract = make_answer_contract(request)
    incomplete = replace(
        contract.obligations[0],
        idea_relevance=(contract.obligations[0].idea_relevance[0],),
    )
    invalid = replace(contract, obligations=(incomplete, contract.obligations[1]))
    expect_failure(
        lambda: invalid.validate(request),
        ContractViolation,
        "complete IdeaSet",
    )


def test_evidence_queries_use_only_query_lens_ideas() -> None:
    _, _, port = all_covered_scenario()
    assert port.queries[0].idea_lens_refs == ("fixture:rg-idea:invariance",)
    assert port.queries[1].idea_lens_refs == ("fixture:rg-idea:invariance",)
    assert "fixture:rg-idea:calibration" not in port.queries[1].idea_lens_refs


def test_idea_set_has_no_selected_or_whole_set_combined_fact() -> None:
    field_names = {field.name for field in fields(AcceptedIdeaSetBinding)}
    assert "selected_idea" not in field_names
    assert "winner" not in field_names
    assert "combined" not in field_names


def test_candidate_card_cannot_become_evidence_ref() -> None:
    invalid = replace(make_evidence("card", "preview"), source_kind="CandidateCard")
    expect_failure(
        invalid.validate,
        ContractViolation,
        "navigation projection",
    )


def test_mutable_latest_evidence_ref_fails_closed() -> None:
    invalid = replace(
        make_evidence("latest-alias", "paired_effect"),
        evidence_ref="fixture:rg-evidence:latest",
    )
    expect_failure(
        invalid.validate,
        ContractViolation,
        "mutable alias",
    )


def test_covered_obligation_without_evidence_fails_closed() -> None:
    request = make_request()
    base = make_answer_contract(request)
    contract = AnswerContract(
        base.source_question_ref,
        base.source_idea_set_ref,
        (base.obligations[0],),
    )
    coverage = (CoverageDecision("effect", "covered", ()),)
    briefs = ()
    rm_result, rg_result = accepted_owner_results()
    port = FakePlanPort(
        (EvidenceQueryResult("ok", "fixture:search-snapshot:no-evidence", ()),),
        rm_result,
        rg_result,
    )
    review = make_review(contract, coverage, briefs)
    expect_failure(
        lambda: compile_plan(request, contract, coverage, briefs, review, port),
        ContractViolation,
        "needs exact evidence",
    )


def test_gap_without_brief_fails_closed() -> None:
    request = make_request()
    base = make_answer_contract(request)
    contract = AnswerContract(
        base.source_question_ref,
        base.source_idea_set_ref,
        (base.obligations[0],),
    )
    coverage = (
        CoverageDecision(
            "effect",
            "gap",
            (),
            "No accepted measurement exists.",
        ),
    )
    briefs = ()
    rm_result, rg_result = accepted_owner_results()
    port = FakePlanPort(
        (EvidenceQueryResult("ok", "fixture:search-snapshot:gap-empty", ()),),
        rm_result,
        rg_result,
    )
    review = make_review(contract, coverage, briefs)
    expect_failure(
        lambda: compile_plan(request, contract, coverage, briefs, review, port),
        ContractViolation,
        "every gap",
    )


def test_unavailable_query_is_blocker_not_gap_evidence() -> None:
    request = make_request()
    base = make_answer_contract(request)
    contract = AnswerContract(
        base.source_question_ref,
        base.source_idea_set_ref,
        (base.obligations[0],),
    )
    coverage = (
        CoverageDecision("effect", "gap", (), "No accepted evidence."),
    )
    brief = ExperimentBrief(
        "effect-new-evidence",
        ("effect",),
        "Measure the effect.",
        "Matched comparison.",
        "Hold data fixed.",
        "Add one Evaluation comparison.",
        ("fixture:rg-idea:invariance",),
    )
    briefs = (brief,)
    rm_result, rg_result = accepted_owner_results()
    port = FakePlanPort(
        (
            EvidenceQueryResult(
                "unavailable",
                "fixture:search-snapshot:unavailable",
                (),
                "evidence owner unavailable",
            ),
        ),
        rm_result,
        rg_result,
    )
    review = make_review(contract, coverage, briefs)
    expect_failure(
        lambda: compile_plan(request, contract, coverage, briefs, review, port),
        PlanBlocked,
        "unavailable",
    )


def test_rm_acceptance_does_not_imply_formal_plan_acceptance() -> None:
    request = make_request()
    contract = make_answer_contract(request)
    effect = make_evidence("effect-reject", "paired_effect")
    limits = make_evidence("limits-reject", "applicability_boundary")
    coverage = (
        CoverageDecision(
            "effect",
            "covered",
            (
                EvidenceUse(
                    effect.evidence_ref,
                    "effect",
                    "scope",
                    ("fixture:rg-idea:invariance",),
                ),
            ),
        ),
        CoverageDecision(
            "limits",
            "covered",
            (
                EvidenceUse(
                    limits.evidence_ref,
                    "limits",
                    "scope",
                    ("fixture:rg-idea:invariance",),
                ),
            ),
        ),
    )
    briefs = ()
    rm_result, _ = accepted_owner_results()
    rg_rejected = OwnerResult(
        "rejected",
        None,
        "fixture:rg-receipt:plan-rejected",
        "support boundary is incomplete",
    )
    snapshot = "fixture:search-snapshot:rg-reject"
    port = FakePlanPort(
        (
            EvidenceQueryResult("ok", snapshot, (effect,)),
            EvidenceQueryResult("ok", snapshot, (limits,)),
        ),
        rm_result,
        rg_rejected,
    )
    plan = compile_plan(
        request, contract, coverage, briefs, make_review(contract, coverage, briefs), port
    )
    result = submit_plan("fixture:plan-submission:rg-reject", request, plan, port)
    assert result.status == "rg_rejected"
    assert result.plan_content_ref == rm_result.object_ref
    assert result.rm_plan_content_receipt_ref == rm_result.receipt_ref
    assert result.formal_plan_ref is None
    assert result.bundle_skip_basis is None


def test_review_is_advisory_and_revisions_change_hash() -> None:
    request = make_request()
    contract = make_answer_contract(request)
    coverage = (
        CoverageDecision("effect", "gap", (), "missing effect evidence"),
        CoverageDecision("limits", "gap", (), "missing limit evidence"),
    )
    briefs = (
        ExperimentBrief(
            "joint-gap",
            ("effect", "limits"),
            "Measure effect and limits.",
            "Matched domain comparison.",
            "Hold data and capacity fixed.",
            "Add Variant and Evaluation causal axes.",
            (
                "fixture:rg-idea:invariance",
                "fixture:rg-idea:calibration",
            ),
        ),
    )
    final_hash = _semantic_plan_hash(contract, coverage, briefs)
    review = AdvisoryReviewRecord(
        "fixture:ar-session:review-revised",
        "sha256:" + "0" * 64,
        (ReviewFinding("f1", "gap_closure", "Clarify held-fixed data."),),
        (
            FindingDisposition(
                "f1", "revised", "BoundaryConstraints now hold data fixed."
            ),
        ),
        final_hash,
    )
    review.validate(final_hash)
    assert "approval" not in {field.name for field in fields(AdvisoryReviewRecord)}
    assert "verdict" not in {field.name for field in fields(AdvisoryReviewRecord)}


def test_no_todo_contract_and_no_target_authority_in_reference_model() -> None:
    result, plan, port = all_covered_scenario()
    assert result.status == "accepted"
    prohibited = {
        "target_ref",
        "target_spec",
        "target_dag",
        "worker",
        "provider",
        "stage_commit",
    }
    normalized_keys = set(plan.normalized_content())
    assert not prohibited & normalized_keys
    assert not any("target" in call.lower() for call in port.calls)


def main() -> int:
    tests = [
        test_all_covered_forms_skip_basis_after_both_owner_receipts,
        test_gap_generates_brief_only_for_gap_and_no_skip_basis,
        test_stale_snapshot_repairs_in_same_contract_and_request,
        test_stage_request_freezes_sources_not_answer_contract,
        test_every_obligation_accounts_for_complete_idea_set,
        test_evidence_queries_use_only_query_lens_ideas,
        test_idea_set_has_no_selected_or_whole_set_combined_fact,
        test_candidate_card_cannot_become_evidence_ref,
        test_mutable_latest_evidence_ref_fails_closed,
        test_covered_obligation_without_evidence_fails_closed,
        test_gap_without_brief_fails_closed,
        test_unavailable_query_is_blocker_not_gap_evidence,
        test_rm_acceptance_does_not_imply_formal_plan_acceptance,
        test_review_is_advisory_and_revisions_change_hash,
        test_no_todo_contract_and_no_target_authority_in_reference_model,
    ]
    for test in tests:
        test()
        print("PASS {}".format(test.__name__))
    print("PASS {} Plan Stage contract tests".format(len(tests)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
