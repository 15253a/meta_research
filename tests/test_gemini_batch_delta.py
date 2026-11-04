from copy import deepcopy
from dataclasses import replace
import json

import pytest

from meta_research.bundle_batch_view import render_delta_batch_reading_context
from meta_research.bundle_target_contract import (
    FORMAL_TARGET_CANDIDATE_SCHEMA_REF, build_normalized_completion_contract,
    normalized_completion_contract_to_dict,
)
from meta_research.owners.common import canonical_hash, canonical_json
from meta_research.semantic_mcp import SemanticMcpError
from meta_research.bundle_skill import BundleSkillUnavailable
from test_bundle_input_drift_recovery import _adapter, _changed, _output, _request
from test_bundle_skill_adapter import _measurement_contract, _plan_document


def _source_readers(request):
    def pointer(source, ref, document, path=None, selected=None):
        return {"semantic_operation_id": "research_memory.stage_context.read",
                "arguments": {"context_pack_ref": request.context_pack_ref,
                              "source": source, "source_ref": ref,
                              "version_ref": canonical_hash(document), "path": path or [],
                              "offset": 0, "limit": 16384},
                "content_hash": canonical_hash(document if selected is None else selected)}
    initial=request.initial_target_plan
    return {
        "initial_target_plan": pointer("bundle_initial_target_plan", request.graph_ref, initial),
        "completion_contract": pointer("bundle_initial_target_plan", request.graph_ref, initial,
                                       ["completion_contract"], initial["completion_contract"]),
        "current_targets": {r["target_ref"]: pointer("bundle_target", r["target_ref"], r)
                            for r in request.current_targets},
        "target_commits": {r["commit_ref"]: pointer("bundle_target_commit", r["commit_ref"], r)
                           for r in request.target_commits},
    }


def _rich_multi_cell_fixture(tmp_path):
    adapter, _ = _adapter(tmp_path, _output("batch"))
    base_req = _request(adapter, "batch")

    plan = deepcopy(_plan_document())
    plan.update(
        {
            "answer_contract": {
                "answer_contract_hash": "a" * 64,
                "obligations": ["unfulfilled cross-device comparison"],
            },
            "coverage": [
                {"obligation": "source-peak", "insufficiency": "four seeds missing"}
            ],
            "gap_set": ["gap:structure", "not-yet-dispatched"],
            "evidence_reuse_set": [{"original_source": "previous accepted evidence"}],
            "other_scientific_constraint": "do not merge distinct lambda bytes",
        }
    )

    completion = build_normalized_completion_contract(
        plan,
        (
            {
                "experiment_key": "experiment:structure",
                "held_fixed_slots": ["shared-model"],
                "required_measurement_unit_keys": [
                    "cell:structure-primary",
                    "cell:structure-replication",
                    "cell:structure-undispatched",
                ],
            },
        ),
    )
    completion_doc = normalized_completion_contract_to_dict(completion)

    def _make_target(idx: int, label: str, cell: str) -> dict[str, object]:
        spec = {
            "schema_ref": FORMAL_TARGET_CANDIDATE_SCHEMA_REF,
            "candidate": {
                "local_label": label,
                "experiment_keys": ["experiment:structure"],
                "measurement_unit_keys": [cell],
                "held_fixed_bindings": [
                    {
                        "semantic_slot": "shared-model",
                        "implementation_revision_ref": "implementation:shared-model",
                    }
                ],
                "implementation_revision_ref": f"implementation:{label}",
                "code_changed": False,
                "routes": [
                    {
                        "route_ref": f"route:{label}",
                        "known_external_operation_refs": [],
                    }
                ],
                "depends_on_labels": [],
                "direct_accepted_input_asset_refs": [],
            },
            "semantic_inputs": [
                deepcopy(completion_doc["experiments"][0]["semantic_inputs"])
            ],
            "measurement_contract": _measurement_contract(label, cell),
            "risk_class": "normal",
        }
        return {
            "target_ref": f"target:{idx}",
            "target_key": label,
            "spec_hash": canonical_hash(spec),
            "spec": spec,
            "dependency_refs": [],
            "receipt": {"receipt_ref": f"target-receipt:{idx}", "status": "accepted"},
        }

    target_1 = _make_target(1, "target:structure-1", "cell:structure-primary")
    target_2 = _make_target(2, "target:structure-2", "cell:structure-replication")

    commit_1 = {
        "commit_ref": "commit:1",
        "target_ref": "target:1",
        "experiment_keys": ["experiment:structure"],
        "measurement_unit_key": "cell:structure-primary",
        "closure_hash": "8" * 64,
        "status": "accepted",
        "metric_values": [{"agreement": 0.95, "conflicts": 0}],
    }
    commit_2_scientific = {
        "commit_ref": "commit:2",
        "target_ref": "target:2",
        "experiment_keys": ["experiment:structure"],
        "measurement_unit_key": "cell:structure-replication",
        "closure_hash": "9" * 64,
        "status": "accepted_with_diagnostics",
        "lambda": ["0.001", "0.0010000000000000002"],
        "raw_missing_row": {"row": 42, "valid_prediction": False},
        "valid_results": 600,
        "nominal_zeros": 40,
        "failures_outside_valid": 192,
        "stable_spectrum": 560,
        "failure": "exit zero but nonfinite factors",
        "arbitrary_unknown_nested": {
            "late_counterexample": "seed-17-diverged",
            "applicable_guidance": "retain exact float strings",
            "withdrawal_scope_change": {"withdrawn_route": "route:legacy"},
        },
    }

    target_plan = deepcopy(base_req.initial_target_plan)
    target_plan["completion_contract"] = completion_doc
    target_plan["initial_strategy_update"]["candidates"] = [deepcopy(target_1["spec"])]
    target_plan["source_bindings"]["plan_document_hash"] = canonical_hash(plan)

    request = replace(
        base_req,
        base_generation=1,
        plan_document=plan,
        initial_target_plan=target_plan,
        current_targets=(target_1, target_2),
        target_commits=(commit_1, commit_2_scientific),
    )

    prompt = {
        "stage_request_ref": request.stage_request_ref,
        "run_ref": request.run_ref,
        "attempt_ref": request.attempt_ref,
        "fence_ref": request.fence_ref,
        "root_session_ref": request.root_session_ref,
        "runtime_binding_hash": canonical_hash(request.runtime_binding.as_dict()),
        "graph_ref": request.graph_ref,
        "formal_plan_ref": request.formal_plan_ref,
        "context_pack_ref": request.context_pack_ref,
        "context_pack_hash": request.context_pack_hash,
        "base_generation": request.base_generation,
        "base_head_receipt": request.base_head_receipt,
        "inbox_checkpoint": request.inbox_checkpoint,
        "initial_target_plan_hash": canonical_hash(request.initial_target_plan),
        "initial_source_bindings": request.initial_target_plan.get("source_bindings"),
        "formal_plan": request.plan_document,
        "current_targets": list(request.current_targets),
        "target_commits": list(request.target_commits),
        "provider_feedback": request.provider_feedback,
    }

    plan_reader = {
        "operation": "research_memory.stage_context.read",
        "semantic_operation_id": "research_memory.stage_context.read",
        "arguments": {
            "context_pack_ref": request.context_pack_ref,
            "source": "context_pack",
            "path": ["accepted_formal_plan_binding", "plan_document"],
            "offset": 0,
            "limit": 16384,
        },
        "context_pack_ref": request.context_pack_ref,
        "context_pack_hash": request.context_pack_hash,
        "formal_plan_ref": request.formal_plan_ref,
        "plan_hash": canonical_hash(request.plan_document),
        "content_hash": canonical_hash(request.plan_document),
        "native_session_ref": request.native_session_ref,
    }

    baseline = {
        "operation_name": "target-batch-1", "invocation_hash": "b" * 64,
        "prompt_hash": "c" * 64, "base_generation": 0,
        "current_targets": [deepcopy(target_1)], "target_commits": [deepcopy(commit_1)],
    }
    return request, prompt, plan_reader, _source_readers(request), baseline


def test_multiple_new_results_keep_failure_and_exact_scientific_fields(tmp_path):
    request, prompt, plan, readers, baseline = _rich_multi_cell_fixture(tmp_path)
    failure={"commit_ref":"commit:3-barrier", "target_ref":"target:2", "kind":"semantic_barrier",
             "reason":"route invalidated by counterexample",
             "unknown_failure_payload":{"exit_code":0,"nonfinite_factors":True}}
    request=replace(request,target_commits=(*request.target_commits,failure))
    prompt["target_commits"]=list(request.target_commits)
    original=deepcopy(prompt)
    view=render_delta_batch_reading_context(request,prompt,verified_plan_reader=plan,
                verified_source_readers=_source_readers(request),verified_delivery_baseline=baseline)
    assert view["target_commits"] == [request.target_commits[1], failure]
    assert view["current_targets"] == [request.current_targets[1]]
    body=canonical_json(view)
    for text in ('"0.001"','"0.0010000000000000002"','"valid_results":600',
                 '"nominal_zeros":40','"failures_outside_valid":192','"stable_spectrum":560',
                 '"row":42','"valid_prediction":false','"seed-17-diverged"','"route:legacy"'):
        assert text in body
    assert '"agreement":0.95' not in body
    assert "formal_plan" not in view
    assert "completion_contract" not in view
    assert "cycle_position" not in view
    assert "plan_responsibilities" not in view
    assert prompt == original
    view["target_commits"][0]["valid_results"]=560
    assert prompt == original


def test_late_revision_is_delivered_in_full(tmp_path):
    request,prompt,plan,readers,baseline=_rich_multi_cell_fixture(tmp_path)
    revised={**request.target_commits[0],"late_revision":"withdrawn_due_to_calibration_drift",
             "metric_values":[{"agreement":0.91,"conflicts":2}]}
    request=replace(request,target_commits=(revised,request.target_commits[1]))
    prompt["target_commits"]=list(request.target_commits)
    view=render_delta_batch_reading_context(request,prompt,verified_plan_reader=plan,
                verified_source_readers=_source_readers(request),verified_delivery_baseline=baseline)
    assert view["target_commits"] == [revised,request.target_commits[1]]
    assert "withdrawn_due_to_calibration_drift" in canonical_json(view)


def test_absent_baseline_keeps_results_without_restoring_stable_prose(tmp_path):
    request,prompt,plan,readers,_=_rich_multi_cell_fixture(tmp_path)
    view=render_delta_batch_reading_context(request,prompt,verified_plan_reader=plan,
                verified_source_readers=readers,verified_delivery_baseline=None)
    assert view["current_targets"] == list(request.current_targets)
    assert view["target_commits"] == list(request.target_commits)
    assert "formal_plan" not in view
    assert "completion_contract" not in view
    assert "plan_responsibilities" not in view


def test_missing_one_reader_retains_only_that_historical_record(tmp_path):
    request,prompt,plan,readers,baseline=_rich_multi_cell_fixture(tmp_path)
    del readers["target_commits"]["commit:1"]
    view=render_delta_batch_reading_context(request,prompt,verified_plan_reader=plan,
                verified_source_readers=readers,verified_delivery_baseline=baseline)
    assert view["current_targets"] == [request.current_targets[1]]
    assert view["target_commits"] == list(request.target_commits)
    assert "formal_plan" not in view


def test_undispatched_cells_and_exact_readers_remain_discoverable(tmp_path):
    request,prompt,plan,readers,baseline=_rich_multi_cell_fixture(tmp_path)
    view=render_delta_batch_reading_context(request,prompt,verified_plan_reader=plan,
                verified_source_readers=readers,verified_delivery_baseline=baseline)
    body=canonical_json(view)
    for text in ("cell:structure-undispatched","cell:structure-primary","target:1","commit:1",
                 "shared-model","implementation:shared-model","research_memory.stage_context.read"):
        assert text in body
    assert canonical_hash(request.initial_target_plan["completion_contract"]) in body
    assert canonical_hash(request.current_targets[0]) in body
    assert canonical_hash(request.target_commits[0]) in body


def test_no_late_facts_in_earlier_frozen_cut(tmp_path):
    request,prompt,plan,readers,baseline=_rich_multi_cell_fixture(tmp_path)
    request=replace(request,base_generation=0,current_targets=(request.current_targets[0],),
                    target_commits=(request.target_commits[0],))
    prompt={**prompt,"base_generation":0,"current_targets":list(request.current_targets),
            "target_commits":list(request.target_commits)}
    view=render_delta_batch_reading_context(request,prompt,verified_plan_reader=plan,
                verified_source_readers=_source_readers(request),verified_delivery_baseline=None)
    assert view["target_commits"] == list(request.target_commits)
    body=canonical_json(view)
    assert "0.0010000000000000002" not in body
    assert "seed-17-diverged" not in body


@pytest.mark.parametrize("changes",[{"native_session_ref":None},{"native_replacement_context":{}},
                                    {"provider_feedback":{"reason":"owner rejection"}}])
def test_ineligible_paths_keep_original_records(tmp_path,changes):
    request,prompt,plan,readers,baseline=_rich_multi_cell_fixture(tmp_path)
    view=render_delta_batch_reading_context(replace(request,**changes),prompt,verified_plan_reader=plan,
                verified_source_readers=readers,verified_delivery_baseline=baseline)
    assert view["formal_plan"] == request.plan_document
    assert view["target_commits"] == list(request.target_commits)


def test_adapter_denial_is_not_full_fallback(tmp_path):
    adapter,runner=_adapter(tmp_path,_output("batch"),durable=True)
    request=_request(adapter,"batch")
    def denied(_):
        raise SemanticMcpError("semantic_mcp_scope_denied")
    adapter._batch_plan_reader=denied
    with pytest.raises(BundleSkillUnavailable,match="semantic_mcp_scope_denied"):
        adapter.propose_target_batch(request)
    assert runner.calls == []


def test_sealed_recovery_ignores_new_reader_and_does_not_redispatch(tmp_path):
    adapter,runner=_adapter(tmp_path,_output("batch"),durable=True)
    request=_request(adapter,"batch")
    first=adapter.propose_target_batch(request)
    operation=next((tmp_path/"provider/provider-operations").glob("*/*"))
    frozen=(operation/"business-input.json").read_bytes()
    assert json.loads(frozen)["request"]["formal_plan"] == request.plan_document
    def denied(_):
        raise AssertionError("sealed operation consulted a new reader")
    adapter._batch_plan_reader=denied
    second=adapter.propose_target_batch(request)
    assert second == first
    assert (operation/"business-input.json").read_bytes() == frozen
    assert len(runner.calls) == 1
    for name in ("exit.json","completed.json","supervisor-exit.json"):
        (operation/name).unlink(missing_ok=True)
    with pytest.raises(BundleSkillUnavailable,match="codex_operation_reconciliation_pending"):
        adapter.propose_target_batch(_changed(request,"batch"))
    assert len(runner.calls) == 1


def test_missing_completion_reader_reconstructs_without_repeating_contract(tmp_path):
    request,prompt,plan,readers,baseline=_rich_multi_cell_fixture(tmp_path)
    del readers["completion_contract"]
    view=render_delta_batch_reading_context(request,prompt,verified_plan_reader=plan,
                verified_source_readers=readers,verified_delivery_baseline=baseline)
    assert "completion_contract" not in view
    assert "semantic_delta" not in view["responsibility_index"]["experiments"][0]
    fallback=view["completion_contract_reconstruction"]
    briefs={b["experiment_key"]: b for b in request.plan_document["experiment_briefs"]}
    experiments=[]
    for indexed in view["responsibility_index"]["experiments"]:
        brief=briefs[indexed["experiment_key"]]
        experiments.append({"brief":{**indexed,"semantic_delta":brief["semantic_delta"]},
            "semantic_inputs":{k:brief[k] for k in ("experiment_key","goal","characteristics","boundary_constraints","semantic_delta")}})
    rebuilt={"schema_ref":fallback["schema_ref"],"plan_document_hash":fallback["plan_document_hash"],"experiments":experiments}
    assert rebuilt == request.initial_target_plan["completion_contract"]
    assert canonical_hash(rebuilt) == view["responsibility_index"]["completion_contract_hash"]


def test_missing_plan_reader_does_not_restore_other_materials(tmp_path):
    request,prompt,plan,readers,baseline=_rich_multi_cell_fixture(tmp_path)
    view=render_delta_batch_reading_context(request,prompt,verified_plan_reader=None,
        verified_source_readers=readers,verified_delivery_baseline=baseline)
    assert view["formal_plan"] == request.plan_document
    assert view["current_targets"] == [request.current_targets[1]]
    assert view["target_commits"] == [request.target_commits[1]]
    assert "completion_contract" not in view
    assert {"field":"formal_plan","reason":"exact_reader_unavailable"} in view["retained_body_reasons"]
