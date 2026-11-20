"""A sealed provider result replays its full diagnostic without new model work."""

from dataclasses import replace

import pytest

from meta_research.owners.common import canonical_hash, new_ref
from meta_research.reasoning_contract import ReasoningContractError, assemble_reasoning_authoritative_metadata
from meta_research.reasoning_skill import CodexReasoningSkillAdapter, ReasoningSkillUnavailable
from test_reasoning_skill_adapter import (
    _FullConformanceAuthority, _SequenceRunner, _fake_codex, _request, _stage_output,
)


@pytest.mark.parametrize("extra_ref", ["question:other", "question:parent"])
def test_registered_parent_and_other_question_receive_different_corrections(extra_ref):
    request = _request()
    frozen = request.context_pack["research_context"]
    graph = frozen["graph_binding"]
    parent_ref = "question:parent"
    graph["active_question_refs"] = sorted([request.question_ref, parent_ref, "question:other"])
    graph["parent_question_bindings"] = [{
        "question_ref": parent_ref, "parent_question_ref": None, "question_receipt_ref": "parent-receipt:1",
    }]
    candidate = _stage_output()
    parents = candidate["scientific_outcome"]["research_synthesis"]["parent_questions"]
    parents.append({"question_ref": parent_ref, "impact": "unknown", "statement": "No conclusion yet."})
    valid = assemble_reasoning_authoritative_metadata(candidate, frozen_research_context=frozen)
    assert valid["scientific_outcome"]["research_synthesis"]["parent_questions"] == parents

    parents.append({"question_ref": extra_ref, "impact": "unknown", "statement": "Still uncertain."})
    with pytest.raises(ReasoningContractError) as rejected:
        assemble_reasoning_authoritative_metadata(candidate, frozen_research_context=frozen)
    diagnostic = rejected.value.feedback
    assert "parent_questions[1].question_ref" in diagnostic
    assert rejected.value.detail_code == "scientific_outcome_authoritative_metadata_conflict"
    if extra_ref == parent_ref:
        assert "repeats a registered parent" in diagnostic
        assert "quest.impact" not in diagnostic
    else:
        assert "graph_binding.parent_question_bindings" in diagnostic
        assert "quest.impact" in diagnostic


@pytest.mark.parametrize("invalid_content", ["missing_parent", "non_parent", "evidence_kind", "catalog_ref"])
def test_sealed_diagnostic_replays_after_adapter_restart_without_provider_call(tmp_path, invalid_content):
    request = _request()
    output = _stage_output()
    if invalid_content == "missing_parent":
        parent = new_ref("question")
        graph = request.context_pack["research_context"]["graph_binding"]
        graph["active_question_refs"] = sorted([request.question_ref, parent])
        graph["parent_question_bindings"] = [{
            "question_ref": parent, "parent_question_ref": None,
            "question_receipt_ref": "parent-receipt:1",
        }]
        expected_code = "scientific_outcome_research_synthesis_invalid"
        expected_diagnostic = (
            "scientific_outcome_research_synthesis_invalid:"
            "research_synthesis.parent_questions:missing_impact_for:" + parent
        )
    elif invalid_content == "non_parent":
        output["scientific_outcome"]["research_synthesis"]["parent_questions"] = [{
            "question_ref": "question:another-route", "impact": "unknown", "statement": "Its applicability is uncertain."
        }]
        expected_code = "scientific_outcome_authoritative_metadata_conflict"
        expected_diagnostic = "The frozen parent list is empty. Use parent_questions=[] for this submission."
    elif invalid_content == "evidence_kind":
        output["scientific_outcome"]["evidence"][0]["kind"] = "LogAsset"
        expected_code = "scientific_outcome_evidence_invalid"
        expected_diagnostic = (
            "$.scientific_outcome.evidence[0].kind does not match an authorized selected evidence leaf. "
            "Use the exact ref and kind supplied by the frozen closure or authorized content reader; "
            "catalog and container references do not substitute for a selected evidence leaf."
        )
    else:
        output["scientific_outcome"]["evidence"][0]["ref"] = "evidence_catalog_example"
        expected_code = "scientific_outcome_evidence_invalid"
        expected_diagnostic = (
            "For an EvidenceReuseLeaf in current_target_evidence_closure or "
            "plan_evidence_input.evidence_reuse_closure, use its evidence_item_ref as ref "
            "and its role as kind."
        )

    executable = _fake_codex(tmp_path / "codex")

    def configured(runner):
        adapter = CodexReasoningSkillAdapter(
            tmp_path / "provider", executable=str(executable), process_runner=runner,
        )
        adapter.bind_full_conformance_authority(_FullConformanceAuthority())
        adapter.configure_resident_mcp_endpoint("http://semantic-mcp.invalid")
        return adapter

    first_runner = _SequenceRunner([output])
    adapter = configured(first_runner)
    request = replace(request, runtime_binding=adapter.runtime_binding(),
                      context_pack_hash=canonical_hash(request.context_pack),
                      job_ref="sealed-diagnostic-" + invalid_content)
    with pytest.raises(ReasoningSkillUnavailable) as initial:
        adapter.generate_draft(request)
    assert len(first_runner.calls) == 1

    # Reconstruct the real adapter on the same operation spool. An empty runner
    # would fail if recovery re-executed the already completed provider operation.
    replay_runner = _SequenceRunner([])
    replay = configured(replay_runner)
    with pytest.raises(ReasoningSkillUnavailable) as recovered:
        replay.generate_draft(replace(request, runtime_binding=replay.runtime_binding()))
    assert replay_runner.calls == []

    for error in (initial.value, recovered.value):
        assert error.code == "reasoning_primary_result_contract_invalid"
        assert error.rejected_detail_code == expected_code
        assert error.rejected_candidate == output
        assert error.rejected_native_session_ref == "provider-session:1"
        assert expected_diagnostic in "\n".join(error.rejected_feedback)
        if invalid_content == "non_parent":
            feedback = "\n".join(error.rejected_feedback)
            assert "parent_questions[0].question_ref" in feedback
            assert "supported by evidence you used" in feedback
            assert "research_synthesis.quest.impact" in feedback
        assert error.recovery_checkpoint["contract_failure_detail_code"] == expected_code
        assert error.recovery_checkpoint["termination_reason"] == "completed"
    assert recovered.value.recovery_checkpoint == initial.value.recovery_checkpoint
    assert recovered.value.rejected_feedback == initial.value.rejected_feedback
