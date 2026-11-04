from dataclasses import replace
import json
from types import SimpleNamespace as NS

import pytest

from meta_research.bundle_batch_delivery import verified_batch_delivery_baseline
from meta_research.idea_skill import IdeaSkillUnavailable
from test_bundle_input_drift_recovery import _adapter, _request, _output


def _previous(tmp_path):
    adapter, runner = _adapter(tmp_path, _output("batch"), durable=True)
    request = _request(adapter, "batch")
    adapter.propose_target_batch(request)
    operation = next((tmp_path / "provider/provider-operations").glob("*/*"))
    return adapter, runner, request, operation


def _proposals(request):
    return (NS(base_generation=request.base_generation,
               base_head_receipt=NS(as_public_dict=lambda: request.base_head_receipt),
               strategy_update=_output("batch")["strategy_update"],
               proposal_ref="proposal:accepted"),)


def test_baseline_proves_actual_prompt_and_success_without_dispatch(tmp_path):
    adapter, runner, request, operation = _previous(tmp_path)
    original = {p.name: p.read_bytes() for p in operation.iterdir() if p.is_file()}
    baseline = verified_batch_delivery_baseline(adapter, replace(request, base_generation=1),
        verified_proposals=_proposals(request),
        verified_proposal_checkpoints={"proposal:accepted": request.inbox_checkpoint})
    assert baseline["current_targets"] == list(request.current_targets)
    assert baseline["target_commits"] == list(request.target_commits)
    assert baseline["operation_name"] == "target-batch-1"
    assert baseline["prompt_hash"] == json.loads(original["invocation.json"])["payload"]["prompt_hash"]
    assert len(runner.calls) == 1
    assert {name: (operation / name).read_bytes() for name in original} == original


@pytest.mark.parametrize("name", ["prompt.txt", "business-input.json", "invocation.json", "supervisor-exit.json",
                                "output-schema.json", "stdout.jsonl", "last-message.json", "completed.json", "exit.json"])
def test_corruption_is_not_a_full_text_fallback(tmp_path, name):
    adapter, runner, request, operation = _previous(tmp_path)
    (operation / name).write_text("{}")
    with pytest.raises(IdeaSkillUnavailable):
        verified_batch_delivery_baseline(adapter, replace(request, base_generation=1))
    assert len(runner.calls) == 1


def test_unresolved_prior_result_requires_reconciliation(tmp_path):
    adapter, runner, request, operation = _previous(tmp_path)
    for name in ("exit.json", "completed.json", "supervisor-exit.json"):
        (operation / name).unlink()
    with pytest.raises(IdeaSkillUnavailable, match="codex_operation_reconciliation_pending"):
        verified_batch_delivery_baseline(adapter, replace(request, base_generation=1))
    assert len(runner.calls) == 1


def test_different_native_or_plan_cannot_supply_basis(tmp_path):
    adapter, _, request, _ = _previous(tmp_path)
    for changed in (replace(request, native_session_ref="foreign"),
                    replace(request, plan_document={**request.plan_document, "notes": "changed"})):
        with pytest.raises(IdeaSkillUnavailable, match="codex_operation_identity_conflict"):
            verified_batch_delivery_baseline(adapter, replace(changed, base_generation=1))


def test_no_prior_operation_is_explicitly_unproven(tmp_path):
    adapter, _ = _adapter(tmp_path, _output("batch"), durable=True)
    assert verified_batch_delivery_baseline(adapter, _request(adapter, "batch")) is None


def test_success_without_owner_accepted_proposal_is_not_a_basis(tmp_path):
    adapter, _, request, _ = _previous(tmp_path)
    assert verified_batch_delivery_baseline(adapter, replace(request, base_generation=1)) is None


def test_same_proposal_under_another_inbox_is_not_a_basis(tmp_path):
    adapter, _, request, _ = _previous(tmp_path)
    assert verified_batch_delivery_baseline(adapter, replace(request, base_generation=1),
        verified_proposals=_proposals(request),
        verified_proposal_checkpoints={"proposal:accepted": {"checkpoint_ref": "other"}}) is None
