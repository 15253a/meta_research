"""Initial result shapes guide research; Owner JSON and metrics rules still apply."""
from copy import deepcopy
import json

import pytest

from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.owners.research_graph import (
    _decode_target_result_content,
    _validate_target_result_schema,
)
from meta_research.target_run_finalizer import _decode_result_document_bytes


INITIAL_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "metrics": {"type": "object", "properties": {"count": {"type": "number"}},
                    "required": ["count"], "additionalProperties": False},
    },
    "required": ["summary", "metrics"],
    "additionalProperties": False,
}
INITIAL_RESULT = {
    "schema_ref": "test/research-result/v1", "result_disposition": "uncertain",
    "summary": "Seven eligible records observed.", "metrics": {"count": 7},
}


@pytest.mark.parametrize("change", [
    "extra_table", "renamed_field", "missing_initial_field", "changed_field_type",
    "changed_metric_type", "metrics_omitted_by_initial_schema",
])
def test_initial_schema_allows_research_result_shape_to_evolve_without_rewriting_inputs(change):
    schema = deepcopy(INITIAL_SCHEMA)
    document = deepcopy(INITIAL_RESULT)
    if change == "extra_table":
        document["discovered_cohort_table"] = [{"cohort": "held-out", "eligible": False}]
    elif change == "renamed_field":
        document["conclusion"] = document.pop("summary")
    elif change == "missing_initial_field":
        del document["summary"]
    elif change == "changed_field_type":
        document["summary"] = {"supported": ["seven observed"], "uncertain": ["generalization"]}
    elif change == "changed_metric_type":
        document["metrics"]["count"] = {"observed": 7, "qualified": None}
    elif change == "metrics_omitted_by_initial_schema":
        del schema["properties"]["metrics"]
        schema["required"] = ["summary"]
    original_schema = deepcopy(schema)
    original_schema_hash = canonical_hash(schema)
    raw = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode()
    original_raw = bytes(raw)
    accepted = _decode_result_document_bytes(raw)
    assert accepted.as_dict() == document
    rg_document = _decode_target_result_content(raw)
    _validate_target_result_schema(schema=schema, result_content=rg_document)
    assert rg_document == document
    assert schema == original_schema and canonical_hash(schema) == original_schema_hash
    assert raw == original_raw


@pytest.mark.parametrize("value,value_schema", [
    (0.75, {"type": "number"}),
    ([{"cohort": "a", "score": None}], {"type": "array", "items": {"type": "object"}}),
    ({"supported": 7, "uncertain": ["cohort-b"]}, {"type": "object"}),
])
def test_matching_scalar_array_and_object_results_still_pass(value, value_schema):
    document = deepcopy(INITIAL_RESULT)
    document["metrics"]["count"] = value
    schema = deepcopy(INITIAL_SCHEMA)
    schema["properties"]["metrics"]["properties"]["count"] = value_schema
    raw = json.dumps(document).encode()
    assert _decode_result_document_bytes(raw).metrics == document["metrics"]
    _validate_target_result_schema(schema=schema, result_content=_decode_target_result_content(raw))


@pytest.mark.parametrize("number", [float("nan"), float("inf"), float("-inf"), 2**53])
def test_result_json_numeric_safety_is_not_relaxed(number):
    document = deepcopy(INITIAL_RESULT)
    document["metrics"]["count"] = number
    with pytest.raises(OwnerConflict, match="target_measurement_result_number_invalid"):
        _validate_target_result_schema(schema=deepcopy(INITIAL_SCHEMA), result_content=document)


@pytest.mark.parametrize("schema,code", [
    ({"type": "invented-type"}, "target_measurement_result_schema_invalid"),
    ({"required": "summary"}, "target_measurement_result_schema_invalid"),
    ({"minimum": float("nan")}, "target_measurement_result_number_invalid"),
    ({"$ref": "https://example.invalid/remote-schema"}, "target_measurement_result_schema_ref_forbidden"),
    ({"properties": {"x": {"$dynamicRef": "file:///tmp/schema.json"}}}, "target_measurement_result_schema_ref_forbidden"),
    ({"allOf": [{"$recursiveRef": "relative-schema.json"}]}, "target_measurement_result_schema_ref_forbidden"),
])
def test_invalid_schema_and_external_references_remain_rejected(schema, code):
    with pytest.raises(OwnerConflict, match=code):
        _validate_target_result_schema(schema=schema, result_content=deepcopy(INITIAL_RESULT))


@pytest.mark.parametrize("raw", [b'{', b'[]', b'{"a":1,"a":2}', b'{"a":NaN}', b'\xff'])
def test_malformed_result_json_still_fails_before_schema_guidance(raw):
    with pytest.raises(OwnerConflict, match="target_measurement_result_content_invalid"):
        _decode_target_result_content(raw)


@pytest.mark.parametrize("metrics", [None, {}, [], "not-metrics"])
def test_rm_requires_a_present_nonempty_metrics_object(metrics):
    document = deepcopy(INITIAL_RESULT)
    if metrics is None:
        del document["metrics"]
    else:
        document["metrics"] = metrics
    with pytest.raises(OwnerConflict, match="target_root_result_document_invalid"):
        _decode_result_document_bytes(json.dumps(document).encode())
