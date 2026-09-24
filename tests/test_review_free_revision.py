from meta_research.idea_contract import validate_advisory_review, IDEA_REVIEW_SCHEMA_REF
from meta_research.plan_contract import validate_plan_review, PLAN_REVIEW_SCHEMA_REF
from meta_research.reasoning_skill import _validate_reasoning_review
from meta_research.reasoning_contract import REASONING_REVIEW_SCHEMA_REF
from meta_research.owners.common import canonical_hash
import pytest

@pytest.mark.parametrize("stage", ["idea", "plan", "reasoning"])
def test_finalization_accepts_root_revision_without_review_form(stage):
    draft_hash, final_hash = "a" * 64, "b" * 64
    final_key = {"idea": "final_outcome_hash", "plan": "final_plan_hash", "reasoning": "final_output_hash"}[stage]
    review = {"schema_ref": {"idea": IDEA_REVIEW_SCHEMA_REF, "plan": PLAN_REVIEW_SCHEMA_REF, "reasoning": REASONING_REVIEW_SCHEMA_REF}[stage], "reviewed_draft_hash": draft_hash, final_key: final_hash}
    if stage == "idea":
        actual = validate_advisory_review(review, outcome_hash=final_hash, reviewed_draft_hash=draft_hash)
    elif stage == "plan":
        actual = validate_plan_review(review, reviewed_draft_hash=draft_hash, final_plan_hash=final_hash)
    else:
        actual = _validate_reasoning_review(review, reviewed_draft_hash=draft_hash, final_output_hash=final_hash)
    assert actual == canonical_hash(review)

from test_reasoning_contract import _scientific_outcome, _research_context
from meta_research.reasoning_contract import validate_scientific_outcome

@pytest.mark.parametrize("kind", [None, "AnalysisAsset", "LogAsset", "CheckpointArtifact", "ScientificOutcome"])
def test_scientific_adoption_depends_on_verified_source_not_material_category(kind):
    output = _scientific_outcome()
    output["evidence"] = [] if kind is None else [{"kind": kind, "ref": "basis:exact", "finding": "supporting"}]
    output["causal_interpretation"]["attribution_basis_refs"] = [] if kind is None else ["basis:exact"]
    closure = [] if kind is None else [{"kind": kind, "ref": "basis:exact", "source_subject_ref": "source:1", "owner_acceptance_receipt_ref": "receipt:1"}]
    assert validate_scientific_outcome(output, frozen_evidence_closure=closure, frozen_research_context=_research_context()) == canonical_hash(output)
