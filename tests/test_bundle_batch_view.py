from copy import deepcopy
from dataclasses import replace

import pytest

from meta_research.bundle_batch_view import render_batch_reading_context
from meta_research.owners.common import canonical_hash, canonical_json
from test_bundle_input_drift_recovery import _adapter, _output, _request


def _inputs(tmp_path):
    adapter, _ = _adapter(tmp_path, _output("batch"))
    request = _request(adapter, "batch")
    request = replace(request, plan_document={**request.plan_document,
        "answer_contract": {"obligations": ["unfulfilled cross-device comparison"]},
        "coverage": [{"obligation": "source-peak", "insufficiency": "four seeds missing"}],
        "gap_set": ["not-yet-dispatched"],
        "evidence_reuse_set": [{"original_source": "previous accepted evidence"}],
        "other_scientific_constraint": "do not merge distinct lambda bytes"},
        target_commits=(*request.target_commits,
            {"target_ref": "target:late", "lambda": ["0.001", "0.0010000000000000002"],
             "raw_missing_row": {"row": 42, "valid_prediction": False},
             "denominator": 600, "nominal_zeros": 40, "spectrum_excludes": 40,
             "failure": "exit zero but nonfinite factors"}))
    prompt = {
        "formal_plan": request.plan_document, "run_ref": request.run_ref,
        "current_targets": list(request.current_targets),
        "target_commits": list(request.target_commits),
        "base_head_receipt": request.base_head_receipt,
        "inbox_checkpoint": request.inbox_checkpoint,
    }
    reader = {
        "operation": "research_memory.stage_context.read",
        "arguments": {"context_pack_ref": request.context_pack_ref, "source": "context_pack",
            "path": ["accepted_formal_plan_binding", "plan_document"], "offset": 0, "limit": 16384},
        "context_pack_ref": request.context_pack_ref, "context_pack_hash": request.context_pack_hash,
        "formal_plan_ref": request.formal_plan_ref, "plan_hash": canonical_hash(request.plan_document),
        "native_session_ref": request.native_session_ref,
    }
    return request, prompt, reader


def test_full_fallback_preserves_legacy_bytes(tmp_path):
    request, prompt, _ = _inputs(tmp_path)
    assert canonical_json(render_batch_reading_context(request, prompt)) == canonical_json(
        {key: value for key, value in prompt.items() if key != "run_ref"})


def test_verified_view_retains_all_results_obligations_and_exact_conditions(tmp_path):
    request, prompt, reader = _inputs(tmp_path)
    original = deepcopy(prompt)
    view = render_batch_reading_context(request, prompt, verified_plan_reader=reader)
    assert "formal_plan" not in view
    assert view["formal_plan_reader"]["arguments"] == reader["arguments"]
    assert view["formal_plan_reader"]["content_hash"] == canonical_hash(request.plan_document)
    for key in ("current_targets", "target_commits", "base_head_receipt", "inbox_checkpoint"):
        assert view[key] == prompt[key]
    assert view["plan_responsibilities"]["gap_set"] == ["not-yet-dispatched"]
    assert view["plan_responsibilities"]["answer_contract"]["obligations"] == ["unfulfilled cross-device comparison"]
    assert view["plan_responsibilities"]["coverage"][0]["insufficiency"] == "four seeds missing"
    assert view["plan_responsibilities"]["other_scientific_constraint"] == "do not merge distinct lambda bytes"
    assert view["completion_contract"] == request.initial_target_plan["completion_contract"]
    assert view["cycle_position"] == request.cycle_position
    assert prompt == original
    view["target_commits"][-1]["denominator"] = 560
    assert prompt == original


@pytest.mark.parametrize("field,value", [
    ("plan_hash", "0" * 64), ("formal_plan_ref", "other-plan"),
    ("native_session_ref", "other-thread"), ("context_pack_hash", "0" * 64),
    ("context_pack_ref", "other-context"), ("operation", "read_frozen_formal_plan"),
    ("arguments", {"context_pack_ref": "other"}),
])
def test_mismatched_reader_never_omits_plan(tmp_path, field, value):
    request, prompt, reader = _inputs(tmp_path)
    view = render_batch_reading_context(request, prompt,
        verified_plan_reader={**reader, field: value})
    assert view["formal_plan"] == request.plan_document


@pytest.mark.parametrize("changes", [
    {"native_session_ref": None}, {"native_replacement_context": {}},
    {"provider_feedback": {"reason": "owner rejection"}},
])
def test_other_paths_keep_full_plan(tmp_path, changes):
    request, prompt, reader = _inputs(tmp_path)
    view = render_batch_reading_context(replace(request, **changes), prompt,
        verified_plan_reader=reader)
    assert view["formal_plan"] == request.plan_document


@pytest.mark.parametrize("change", ["different_prose", "missing_brief", "duplicate_brief"])
def test_brief_semantics_must_match_before_deduplication(tmp_path, change):
    request, prompt, reader = _inputs(tmp_path)
    initial = deepcopy(request.initial_target_plan)
    experiments = initial["completion_contract"]["experiments"]
    if change == "different_prose":
        experiments[0]["semantic_inputs"]["goal"] += "changed"
    elif change == "missing_brief":
        experiments.clear()
    else:
        experiments.append(deepcopy(experiments[0]))
    view = render_batch_reading_context(replace(request, initial_target_plan=initial), prompt,
                                       verified_plan_reader=reader)
    assert view["formal_plan"] == request.plan_document


def test_deduplication_keeps_old_evidence_and_unknown_brief_linkage(tmp_path):
    request, prompt, reader = _inputs(tmp_path)
    plan = deepcopy(request.plan_document)
    plan["experiment_briefs"][0]["future_condition"] = "preserve unknown original field"
    request = replace(request, plan_document=plan)
    prompt = {**prompt, "formal_plan": plan}
    reader = {**reader, "plan_hash": canonical_hash(plan)}
    view = render_batch_reading_context(request, prompt, verified_plan_reader=reader)
    responsibility = view["plan_responsibilities"]
    assert responsibility["evidence_reuse_set"] == plan["evidence_reuse_set"]
    assert "experiment_briefs" not in responsibility
    binding = responsibility["brief_plan_bindings"][0]
    semantic = view["completion_contract"]["experiments"][0]["semantic_inputs"]
    assert {**binding, **semantic} == plan["experiment_briefs"][0]
    assert binding["future_condition"] == "preserve unknown original field"
