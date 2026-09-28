from dataclasses import replace
from types import SimpleNamespace

import pytest

from meta_research.owners.common import AcceptanceReceipt, OwnerConflict, canonical_hash
from meta_research.research_overview import ResearchOverviewReader


@pytest.mark.parametrize("mismatch", [None, "formal_plan_ref", "plan_document_hash", "receipt"])
def test_bundle_history_keeps_rm_and_rg_plan_acceptances_distinct(mismatch):
    """The request carries RM custody; the verified report carries RG acceptance."""
    plan_hash = canonical_hash({"plan": "exact-plan"})
    def receipt(issuer, kind, subject):
        return AcceptanceReceipt(issuer=issuer, kind=kind, receipt_ref=kind + ":receipt",
                                 subject_ref=subject, payload_hash=canonical_hash([issuer, kind, subject]))
    rm_receipt = receipt("research_memory", "plan_document_content_acceptance", "plan-content")
    rg_receipt = receipt("research_graph", "formal_plan_content_accepted", plan_hash)
    report_receipt = receipt("agent_runtime", "bundle_report_accepted", "bundle-report")
    completion_receipt = receipt("agent_runtime", "run_completed", "bundle-run")
    accepted_plan = SimpleNamespace(formal_plan_ref="plan", plan_document_hash=plan_hash,
                                    content_receipt=rm_receipt, as_dict=lambda: {"formal_plan_ref": "plan"})
    request = SimpleNamespace(request_ref="request", cycle_ref="cycle", stage="bundle", epoch=1,
                              accepted_formal_plan=accepted_plan,
                              accepted_question=SimpleNamespace(quest_ref="quest", question_ref="question"),
                              receipt=receipt("advancement_engine", "stage_run_requested", "request"))
    report = SimpleNamespace(request_ref="request", run_ref="bundle-run", report_ref="bundle-report",
                             formal_plan_ref="plan", plan_document_hash=plan_hash,
                             formal_plan_content_receipt=rg_receipt, receipt=report_receipt,
                             report=SimpleNamespace(stage_request_ref="request", formal_plan_ref="plan"))
    completion = SimpleNamespace(request_ref="request", run_ref="bundle-run", attempt_ref="attempt",
                                 outcome_ref="bundle-report", decision_receipt=report_receipt,
                                 receipt=completion_receipt)
    stage_run = SimpleNamespace(request_ref="request", cycle_ref="cycle", stage="bundle", epoch=1,
                                run_ref="bundle-run", completion=completion)
    commit = SimpleNamespace(commit_ref="commit", request_ref="request", cycle_ref="cycle", stage="bundle",
                             epoch=1, disposition="completed", run_ref="bundle-run", outcome_ref="bundle-report",
                             outcome_kind="bundle_report", outcome_receipt=report_receipt,
                             run_completion_receipt=completion_receipt)
    if mismatch == "formal_plan_ref":
        report.formal_plan_ref = report.report.formal_plan_ref = "different-plan"
    elif mismatch == "plan_document_hash":
        report.plan_document_hash = canonical_hash({"plan": "different-content"})
    elif mismatch == "receipt":
        report.receipt = replace(report_receipt, payload_hash="0" * 64)
    verified_calls = []
    def verify_report(**values):
        verified_calls.append(values)
        if values["receipt"] != report_receipt:
            raise OwnerConflict("bundle_report_receipt_invalid")
        return report
    ar = SimpleNamespace(query_bundle_stage_run=lambda ref: stage_run,
                         query_bundle_run_report=lambda ref: report,
                         verify_bundle_report_receipt=verify_report)
    ae = SimpleNamespace(query_bundle_report_disposition=lambda ref: None)
    reader = ResearchOverviewReader(None, ae, None, ar)
    # This test isolates the acceptance binding; complete report serialization
    # is separately covered by the public Writing and real historical replays.
    reader._bundle_report_value = lambda value: {"run_ref": value.run_ref, "report_ref": value.report_ref,
                                                "report_hash": canonical_hash({"report": "accepted"}),
                                                "report": {"disposition": "realized"}}
    reader._commit_value = lambda value: {"commit_ref": value.commit_ref}
    artifact = reader._artifact("quest", SimpleNamespace(cycle_ref="cycle", question_ref="question"), request, commit)
    assert verified_calls == [{"report_ref": "bundle-report", "receipt": report.receipt}]
    assert rm_receipt != rg_receipt
    if mismatch is None:
        assert artifact["status"] == "accepted"
        assert artifact["source"]["outcome_ref"] == "bundle-report"
        assert artifact["content"] == {"disposition": "realized"}
    else:
        assert artifact["status"] == "unavailable"
        assert artifact["content"] is None
        assert artifact["reason"]["code"] == (
            "bundle_report_receipt_invalid" if mismatch == "receipt" else "writing_bundle_result_invalid"
        )
