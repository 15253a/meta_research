"""A sealed provider result replays its full diagnostic without new model work."""

from dataclasses import replace

import pytest

from meta_research.owners.common import canonical_hash, new_ref
from meta_research.reasoning_skill import CodexReasoningSkillAdapter, ReasoningSkillUnavailable
from test_reasoning_skill_adapter import (
    _FullConformanceAuthority, _SequenceRunner, _fake_codex, _request, _stage_output,
)


@pytest.mark.parametrize("invalid_content", ["missing_parent", "evidence_kind"])
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
    else:
        output["scientific_outcome"]["evidence"][0]["kind"] = "LogAsset"
        expected_code = "scientific_outcome_evidence_invalid"
        expected_diagnostic = (
            "$.scientific_outcome.evidence[0].kind does not match an authorized selected evidence leaf. "
            "Use the exact ref and kind supplied by the frozen closure or authorized content reader; "
            "catalog and container references do not substitute for a selected evidence leaf."
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
        assert error.recovery_checkpoint["contract_failure_detail_code"] == expected_code
        assert error.recovery_checkpoint["termination_reason"] == "completed"
    assert recovered.value.recovery_checkpoint == initial.value.recovery_checkpoint
    assert recovered.value.rejected_feedback == initial.value.rejected_feedback
