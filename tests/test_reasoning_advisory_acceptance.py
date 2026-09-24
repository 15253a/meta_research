"""Current RM -> RG acceptance after a real accepted summary and AR decision.

Scientific candidate cases use composed SQLite Owners and deterministic
providers in isolated roots. The final-outcome case retains its direct Owner
fixture. Review forms never substitute for source, receipt or fence checks.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import test_public_reasoning_owners as fixture
from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.owners.research_graph import (
    REASONING_ACCEPTED_RECEIPT_KIND,
    REASONING_SCIENTIFIC_ACCEPTED_RECEIPT_KIND,
)
from meta_research.owners.research_memory import (
    REASONING_AUTONOMOUS_CHECKPOINT_SCHEMA,
)


def _advisory_review(output):
    return {"schema_ref": fixture.REASONING_REVIEW_SCHEMA_REF,
            "reviewed_draft_hash": canonical_hash(output), "final_output_hash": canonical_hash(output)}


@pytest.fixture
def current_candidate(tmp_path):
    from test_reasoning_summary_decision_flow import runtime_at, seed_checkpoint, SummarySkill
    from reasoning_current_fixtures import summary_candidate_values

    runtime = runtime_at(tmp_path / "summary-advisory", SummarySkill("create"))
    request, checkpoint = seed_checkpoint(runtime)
    values = summary_candidate_values(runtime, request, checkpoint)
    yield runtime, values
    runtime.close()


@pytest.mark.parametrize("revised", [False, True], ids=["unchanged-final", "revised-final-without-form"])
def test_scientific_checkpoint_accepts_advisory_review(tmp_path: Path, revised: bool):
    from test_reasoning_summary_decision_flow import runtime_at, seed_checkpoint, SummarySkill
    from reasoning_current_fixtures import summary_candidate_values

    runtime = runtime_at(tmp_path / "accepted", SummarySkill("create"))
    try:
        request, checkpoint = seed_checkpoint(runtime)
        memory, graph = runtime.owners.research_memory, runtime.owners.research_graph
        assert memory.query_reasoning_scientific_candidate_by_checkpoint_ref(checkpoint.checkpoint_ref) is None
        values = summary_candidate_values(runtime, request, checkpoint, revised=revised)
        candidate = memory.accept_reasoning_scientific_candidate(**values)
        decision = graph.decide_reasoning_scientific_candidate(content=candidate)
        assert decision.decision == "accepted", (decision.reason_code, decision.feedback)
        assert decision.receipt.kind == REASONING_SCIENTIFIC_ACCEPTED_RECEIPT_KIND
        assert decision.outcome_ref == candidate.scientific_outcome_ref and decision.feedback == ()
        assert set(candidate.review) == {"schema_ref", "reviewed_draft_hash", "final_output_hash"}
        assert graph.decide_reasoning_scientific_candidate(content=candidate) == decision
    finally:
        runtime.close()


def test_final_outcome_accepts_advisory_review(tmp_path: Path, monkeypatch):
    database, memory, _receipts, graph, graph_receipts = fixture._owners(tmp_path)
    try:
        monkeypatch.setattr(fixture, "_review", _advisory_review)
        content = fixture._accept_real_root_next_cycle_content(
            memory, graph,
            submission_ref="reasoning-submission:advisory-final",
            outcome_ref="scientific-outcome:advisory-final",
        )
        decision = graph.decide_reasoning_outcome(content=content)
        assert decision.decision == "accepted", (decision.reason_code, decision.feedback)
        assert decision.receipt.kind == REASONING_ACCEPTED_RECEIPT_KIND
        assert decision.outcome_ref == content.scientific_outcome["outcome_ref"]
        graph_receipts.verify_reasoning_outcome_decision(
            request_ref=content.request_ref,
            submission_ref=content.submission_ref,
            decision="accepted",
            outcome_ref=decision.outcome_ref,
            receipt=decision.receipt,
        )
        assert graph.decide_reasoning_outcome(content=content) == decision
    finally:
        database.close()


@pytest.mark.parametrize(
    "damage,expected",
    [
        ("evidence", "scientific_outcome_evidence_invalid"),
        ("context_hash", "reasoning_scientific_candidate_lineage_invalid"),
        ("review_hash", "reasoning_review_binding_invalid"),
        ("review_schema", "reasoning_review_binding_invalid"),
        ("review_form", "reasoning_review_binding_invalid"),
        ("identity", "stage_run_request_receipt_invalid"),
        ("fence", "reasoning_continuation_receipt_invalid"),
        ("checkpoint_receipt", "reasoning_continuation_receipt_invalid"),
        ("draft_receipt", "reasoning_scientific_candidate_lineage_invalid"),
    ],
)
def test_advisory_review_does_not_bypass_rm_contracts(current_candidate, damage: str, expected: str):
    runtime, values = current_candidate
    memory, graph = runtime.owners.research_memory, runtime.owners.research_graph
    outcome_ref = values["checkpoint"]["scientific_outcome"]["outcome_ref"]
    if damage == "evidence":
        values["checkpoint"]["scientific_outcome"]["evidence"] = [
            {"kind": "LiteratureRecord", "ref": "literature:unfrozen", "finding": "supporting"}
        ]
        values["review"]["final_output_hash"] = canonical_hash(values["checkpoint"])
    elif damage == "context_hash":
        values["context_pack_hash"] = "0" * 64
    elif damage == "review_hash":
        values["review"]["final_output_hash"] = "0" * 64
    elif damage == "review_schema":
        values["review"]["schema_ref"] = "unrecognized/review/v99"
    elif damage == "review_form":
        values["review"]["dispositions"] = [{"finding_id": "unrecorded", "action": "adopted"}]
    elif damage == "identity":
        values["request_ref"] = "reasoning-request:wrong-owner"
        values["stage_request_receipt"] = replace(values["stage_request_receipt"], subject_ref=values["request_ref"])
    elif damage == "fence":
        values["fence_ref"] = "fence:forged"
    elif damage == "checkpoint_receipt":
        values["checkpoint_receipt"] = replace(values["checkpoint_receipt"], payload_hash="0" * 64)
    elif damage == "draft_receipt":
        values["checkpoint_receipt"] = replace(values["checkpoint_receipt"], kind="reasoning_autonomous_checkpoint")
    with pytest.raises(OwnerConflict, match=f"^{expected}$"):
        memory.accept_reasoning_scientific_candidate(**values)
    assert graph.query_reasoning_scientific_decision_by_outcome_ref(outcome_ref) is None


def test_advisory_review_does_not_bypass_rg_content_receipt(current_candidate):
    runtime, values = current_candidate
    memory, graph = runtime.owners.research_memory, runtime.owners.research_graph
    candidate = memory.accept_reasoning_scientific_candidate(**values)
    forged = replace(candidate, receipt=replace(candidate.receipt, payload_hash="0" * 64))
    with pytest.raises(OwnerConflict, match="reasoning_scientific_candidate_receipt_invalid"):
        graph.decide_reasoning_scientific_candidate(content=forged)
    assert graph.query_reasoning_scientific_decision_by_outcome_ref(candidate.scientific_outcome_ref) is None
