from meta_research.bundle_skill import (
    _formal_target_candidate_schema,
    _normalized_completion_contract_schema,
    _schema_template_request,
)
from copy import deepcopy
import pytest
from meta_research.bundle_output_assembly import assemble_bundle_primary, assemble_strategy_update
from meta_research.bundle_target_contract import BundleTargetContractError
from test_bundle_skill_adapter import _plan_document, _target_plan


def _compact(output):
    result = deepcopy(output)
    if "target_plan" in result:
        plan = result["target_plan"]
        for row in plan["completion_contract"]["experiments"]:
            row.pop("semantic_inputs")
            row["brief"].pop("semantic_delta")
        update = plan["initial_strategy_update"]
    else:
        update = result.get("strategy_update", {})
    for candidate in update.get("candidates", []):
        candidate.pop("semantic_inputs")
    return result


def test_bundle_output_schema_does_not_require_copying_frozen_brief():
    request = _schema_template_request()
    experiment = _normalized_completion_contract_schema(request)["properties"]["experiments"]["items"]
    assert "semantic_inputs" not in experiment["properties"]
    assert "semantic_delta" not in experiment["properties"]["brief"]["properties"]
    candidate = _formal_target_candidate_schema(request)
    assert "semantic_inputs" not in candidate["properties"]
    assert "held_fixed_slots" in experiment["properties"]["brief"]["properties"]
    assert "required_measurement_unit_keys" in experiment["properties"]["brief"]["properties"]
    assert "measurement_contract" in candidate["properties"]


def test_assembly_copies_only_exact_plan_prose_and_keeps_root_choices():
    plan = _plan_document()
    original = {"target_plan": _target_plan(plan, "7" * 64)}
    compact = _compact(original)
    saved = deepcopy(compact)
    result = assemble_bundle_primary(compact, plan)
    assert result == original
    assert compact == saved
    assert assemble_bundle_primary(result, plan) == result
    wrong_version = deepcopy(compact)
    wrong_version["target_plan"]["completion_contract"]["plan_document_hash"] = "0" * 64
    with pytest.raises(BundleTargetContractError, match="plan_hash_mismatch"):
        assemble_bundle_primary(wrong_version, plan)


@pytest.mark.parametrize("field", ["semantic_inputs", "semantic_delta"])
def test_assembly_rejects_attempt_to_change_frozen_science(field):
    plan = _plan_document()
    output = {"target_plan": _target_plan(plan, "7" * 64)}
    row = output["target_plan"]["completion_contract"]["experiments"][0]
    if field == "semantic_inputs":
        row[field]["boundary_constraints"] = "changed"
    else:
        row["brief"][field] = "changed"
    with pytest.raises(BundleTargetContractError, match="semantic_inputs_conflict"):
        assemble_bundle_primary(output, plan)


def test_strategy_assembly_cannot_select_unknown_experiment_or_rewrite_semantics():
    plan = _plan_document()
    update = _target_plan(plan, "7" * 64)["initial_strategy_update"]
    compact = _compact({"strategy_update": update})["strategy_update"]
    assert assemble_strategy_update(compact, plan) == update
    compact["candidates"][0]["candidate"]["experiment_keys"] = ["foreign"]
    with pytest.raises(BundleTargetContractError, match="experiment_invalid"):
        assemble_strategy_update(compact, plan)
    update["candidates"][0]["semantic_inputs"][0]["goal"] = "different"
    with pytest.raises(BundleTargetContractError, match="semantic_conflict"):
        assemble_strategy_update(update, plan)


def test_output_rollback_only_changes_new_schema_and_preserves_sealed_compact_call(tmp_path, monkeypatch):
    import json
    from dataclasses import replace
    from test_bundle_input_drift_recovery import _adapter, _request, _output
    adapter, runner = _adapter(tmp_path, _output("batch"), durable=True)
    request = _request(adapter, "batch")
    binding = adapter.runtime_binding()
    adapter.propose_target_batch(request)
    operation = next((tmp_path / "provider/provider-operations").glob("*/*"))
    saved = {path.name: path.read_bytes() for path in operation.iterdir() if path.is_file()}
    monkeypatch.setenv("META_RESEARCH_BUNDLE_OUTPUT_MODE", "full")
    assert adapter.runtime_binding() == binding
    adapter.propose_target_batch(request)
    assert len(runner.calls) == 1
    assert {name: (operation / name).read_bytes() for name in saved} == saved
    adapter.propose_target_batch(replace(request, job_ref="new-full-output-operation"))
    assert len(runner.calls) == 2
    schema = runner.calls[-1][2]
    assert "semantic_inputs" in schema["properties"]["strategy_update"]["properties"]["candidates"]["items"]["properties"]


def test_output_rollback_reconciles_unknown_compact_operation_without_dispatch(tmp_path, monkeypatch):
    from meta_research.idea_skill import IdeaSkillUnavailable
    from test_bundle_input_drift_recovery import _adapter, _request, _output
    adapter, runner = _adapter(tmp_path, _output("batch"), durable=True)
    request = _request(adapter, "batch")
    adapter.propose_target_batch(request)
    operation = next((tmp_path / "provider/provider-operations").glob("*/*"))
    for name in ("exit.json", "completed.json", "supervisor-exit.json"):
        (operation / name).unlink(missing_ok=True)
    saved = {path.name: path.read_bytes() for path in operation.iterdir() if path.is_file()}
    monkeypatch.setenv("META_RESEARCH_BUNDLE_OUTPUT_MODE", "full")
    with pytest.raises(IdeaSkillUnavailable, match="codex_operation_reconciliation_pending"):
        adapter.propose_target_batch(request)
    assert len(runner.calls) == 1
    assert {path.name: path.read_bytes() for path in operation.iterdir() if path.is_file()} == saved


def test_compact_output_runs_through_real_adapter(tmp_path, monkeypatch):
    import test_bundle_skill_adapter as suite
    original_runner = suite._SequenceRunner
    class CompactRunner(original_runner):
        def __init__(self, outputs, **kwargs):
            super().__init__([_compact(output) for output in outputs], **kwargs)
    monkeypatch.setattr(suite, "_SequenceRunner", CompactRunner)
    suite.test_production_adapter_freezes_skill_without_requiring_child_choreography(tmp_path)


def test_owner_accepts_assembled_output_with_full_target_local_contract(tmp_path):
    from test_public_bundle_stage import _DeterministicBundleSkill
    from test_target_root_finalizer import _current_bundle_runtime
    from test_target_launch_admission import _ready_launch
    class AssemblingProvider(_DeterministicBundleSkill):
        def _target_plan(self, request):
            full = super()._target_plan(request)
            assembled = assemble_bundle_primary(_compact({"target_plan": full}), request.plan_document)
            assert assembled["target_plan"] == full
            return assembled["target_plan"]
    runtime = _current_bundle_runtime(tmp_path, bundle_skill=AssemblingProvider())
    try:
        graph, target, _, _, launch = _ready_launch(runtime)
        assert graph.receipt is not None
        assert target.spec["semantic_inputs"]
        assert target.spec["measurement_contract"]
        assert launch is not None
    finally:
        runtime.close()
