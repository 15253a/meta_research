from copy import deepcopy

import pytest

from meta_research.bundle_target_contract import (
    BUNDLE_ROOT_MAX_SERIALIZED_BYTES,
    strategy_update_from_dict,
    strategy_update_to_dict,
)
from meta_research.idea_skill import IdeaSkillUnavailable
from test_bundle_input_drift_recovery import _adapter, _output, _request as _legacy_request


def _request(adapter, kind):
    request = _legacy_request(adapter, kind)
    # The shared older fixture still carries the retired prototype field.
    for wrapper in request.initial_target_plan["initial_strategy_update"]["candidates"]:
        wrapper["candidate"].pop("reuse_trace", None)
    return request


def test_completed_batch_retains_long_chinese_notes_without_repeating_provider(tmp_path):
    output = _output("batch")
    notes = "本轮三个工作单元均已接纳，科学缺口留给后继研究。\n" * 90
    assert len(notes) < 4096 < len(notes.encode("utf-8"))
    output["strategy_update"]["notes"] = notes
    adapter, runner = _adapter(tmp_path, output)
    request = _request(adapter, "batch")
    result = adapter.propose_target_batch(request)
    assert result.strategy_update == output["strategy_update"]
    assert adapter.propose_target_batch(request) == result
    assert len(runner.calls) == 1
    # Both parsing and serialization preserve the exact free research prose.
    parsed = strategy_update_from_dict(result.strategy_update, completion_contract=None)
    assert strategy_update_to_dict(parsed, completion_contract=None) == result.strategy_update


def test_notes_remain_subject_to_complete_document_byte_budget(tmp_path):
    output = _output("batch")
    output["strategy_update"]["notes"] = "x" * BUNDLE_ROOT_MAX_SERIALIZED_BYTES
    adapter, runner = _adapter(tmp_path, output)
    request = _request(adapter, "batch")
    with pytest.raises(IdeaSkillUnavailable) as caught:
        adapter.propose_target_batch(request)
    assert caught.value.code == "bundle_review_result_contract_invalid"
    assert caught.value.recovery_checkpoint["contract_failure_detail_code"] == (
        "formal_strategy_update_byte_budget_exceeded"
    )
    assert isinstance(caught.value.__cause__, ValueError)
    assert caught.value.__cause__.__notes__ == ["FormalStrategyUpdate exceeds byte budget"]
    assert len(runner.calls) == 1


def test_long_non_note_reference_still_rejected(tmp_path):
    output = _output("batch")
    output["strategy_update"]["requires_accepted_labels"] = ["x" * 4097]
    adapter, runner = _adapter(tmp_path, output)
    with pytest.raises(IdeaSkillUnavailable) as caught:
        adapter.propose_target_batch(_request(adapter, "batch"))
    assert caught.value.code == "bundle_review_result_contract_invalid"
    assert caught.value.recovery_checkpoint["contract_failure_detail_code"] == (
        "requires_accepted_labels_invalid"
    )
    assert len(runner.calls) == 1


@pytest.mark.parametrize("diagnostic, expected", [
    ("FormalStrategyUpdate has oversized text", "formal_strategy_update_text_too_large"),
    ("An unknown validation detail: 请核对原结果", "bundle_result_contract_invalid"),
])
def test_domain_diagnostic_does_not_prevent_sealed_correction(tmp_path, monkeypatch, diagnostic, expected):
    import meta_research.bundle_skill as skill

    output = _output("batch")
    adapter, runner = _adapter(tmp_path, deepcopy(output))
    request = _request(adapter, "batch")

    def reject(_request, _result):
        raise skill.BundleSkillContractError(diagnostic)

    monkeypatch.setattr(skill, "validate_bundle_target_batch_result", reject)
    with pytest.raises(IdeaSkillUnavailable) as caught:
        adapter.propose_target_batch(request)
    assert caught.value.code == "bundle_review_result_contract_invalid"
    assert caught.value.recovery_checkpoint["contract_failure_detail_code"] == expected
    assert caught.value.__cause__.__notes__ == [diagnostic]
    assert len(runner.calls) == 1
