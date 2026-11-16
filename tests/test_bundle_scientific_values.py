"""Scientific values use exact JSON types; control records keep their limits."""
from dataclasses import replace
import hashlib
import json

import pytest

from meta_research.bundle_protocol import (
    BUNDLE_HANDOFF_MAX_SERIALIZED_BYTES,
    BUNDLE_INBOX_BATCH_MAX_SERIALIZED_BYTES,
    BUNDLE_ROOT_MAX_SERIALIZED_BYTES,
    BundleProtocolError,
    canonical_projection_bytes,
    decode_target_metric_values,
    projection_plain_value,
    valid_target_metric_value,
    validate_closed_bundle_projection,
)
from meta_research.bundle_target_contract import formal_target_candidate_from_dict
from test_bundle_completion_contract import _closure
from test_bundle_target_contract import _candidate, _contract


@pytest.mark.parametrize("value", [-(2**63), 2**64 - 1, 2**100, "x" * 5000,
    [0] * 65_537, {"seed": 14748774831839608929}],
    ids=["negative-63", "unsigned-64", "positive-100", "long-text", "many-nodes", "seed"])
def test_scientific_values_survive_bundle_and_json_readback(value):
    closure = _closure("target-a", ("experiment-a",), "measurement-a", metric_values=(value,))
    assert valid_target_metric_value(value)
    digest = validate_closed_bundle_projection(closure)
    encoded = canonical_projection_bytes(closure)
    assert digest == hashlib.sha256(encoded).hexdigest()
    restored = decode_target_metric_values(json.loads(encoded)["metric_values"])
    assert restored == (value,)
    assert type(restored[0]) is type(value)


def test_scientific_outer_tuple_is_not_a_control_collection():
    values = tuple(range(1025))
    closure = _closure("target-a", ("experiment-a",), "measurement-a", metric_values=values)
    validate_closed_bundle_projection(closure)
    assert projection_plain_value(closure)["metric_values"][-1] == 1024
    with pytest.raises(BundleProtocolError, match="oversized tuple"):
        validate_closed_bundle_projection(replace(closure, checkpoint_artifact_refs=("ref",) * 1025))


def test_control_integer_and_text_limits_remain():
    closure = _closure("target-a", ("experiment-a",), "measurement-a")
    with pytest.raises(BundleProtocolError, match="oversized integer"):
        validate_closed_bundle_projection(2**63)
    with pytest.raises(BundleProtocolError, match="oversized text"):
        validate_closed_bundle_projection(replace(closure, target_ref="x" * 4097))


@pytest.mark.parametrize("budget", [BUNDLE_HANDOFF_MAX_SERIALIZED_BYTES,
    BUNDLE_INBOX_BATCH_MAX_SERIALIZED_BYTES, BUNDLE_ROOT_MAX_SERIALIZED_BYTES])
def test_scientific_content_still_counts_towards_each_package_budget(budget):
    closure = _closure("target-a", ("experiment-a",), "measurement-a", metric_values=("x" * budget,))
    with pytest.raises(BundleProtocolError, match="byte budget") as caught:
        validate_closed_bundle_projection(closure, max_serialized_bytes=budget)
    assert caught.value.details["category"] == "technical_capacity"
    assert caught.value.details["actual_bytes"] > budget
    assert caught.value.details["limit"] == budget
    assert str(budget) in str(caught.value)


def test_150_protocol_metric_definitions_are_accepted():
    _, contract = _contract()
    candidate = _candidate(contract, "target-a", "cell-a")
    protocol = candidate["measurement_contract"]["protocol_version"]
    for category in ("required_metrics", "optional_metrics"):
        protocol[category] = [
            {"metric_key": f"{category}:{index}", "definition": {"meaning": "observation"}}
            for index in range(150)
        ]
    parsed = formal_target_candidate_from_dict(candidate, completion_contract=contract)
    assert len(parsed.measurement_contract.protocol_version.required_metrics) == 150
    assert len(parsed.measurement_contract.protocol_version.optional_metrics) == 150


def test_cycles_and_depth_still_rejected_and_shared_subtrees_do_not_expand():
    cyclic = []
    cyclic.append(cyclic)
    assert not valid_target_metric_value(cyclic)
    shared = 0
    for _ in range(40):
        shared = [shared, shared]
    assert valid_target_metric_value(shared)
    closure = _closure("target-a", ("experiment-a",), "measurement-a", metric_values=(shared,))
    with pytest.raises(BundleProtocolError, match="byte budget"):
        validate_closed_bundle_projection(closure)
    with pytest.raises(BundleProtocolError, match="byte budget"):
        decode_target_metric_values([shared])
    too_deep = shared
    for _ in range(25):
        too_deep = [too_deep]
    assert not valid_target_metric_value(too_deep)


def test_supported_small_values_keep_canonical_bytes_and_hash():
    closure = _closure("target-a", ("experiment-a",), "measurement-a", metric_values=(0, 1.25, True, None, "观察"))
    expected = json.dumps(projection_plain_value(closure), ensure_ascii=False,
        sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    assert canonical_projection_bytes(closure) == expected
    assert validate_closed_bundle_projection(closure) == hashlib.sha256(expected).hexdigest()


def test_runtime_storage_decoders_preserve_exact_scientific_types():
    from meta_research.bundle_protocol import AcceptedMeasurementClosure
    from meta_research.owners.agent_runtime import _stored_bundle_record
    from meta_research.owners.target_run_runtime import _decode_stored_record

    values = tuple(range(1025)) + (-(2**63), 2**64 - 1, 2**100,
        {"seed": 14748774831839608929}, "x" * 5000)
    closure = _closure("target-a", ("experiment-a",), "measurement-a", metric_values=values)
    closure = replace(closure, root_completion_receipt=closure.rg_target_commit_receipt)
    digest = validate_closed_bundle_projection(closure)
    document = canonical_projection_bytes(closure).decode("utf-8")
    for restored in (
        _stored_bundle_record(document, digest, AcceptedMeasurementClosure, "invalid"),
        _decode_stored_record(document, digest, AcceptedMeasurementClosure),
    ):
        assert restored == closure
        assert tuple(type(item) for item in restored.metric_values) == tuple(type(item) for item in values)


def test_protocol_tool_schema_accepts_150_metric_definitions():
    from jsonschema import validate
    from meta_research.bundle_skill import _protocol_version_candidate_schema

    _, contract = _contract()
    protocol = _candidate(contract, "target-a", "cell-a")["measurement_contract"]["protocol_version"]
    for category in ("required_metrics", "optional_metrics"):
        protocol[category] = [
            {"metric_key": f"{category}:{index}", "definition": {"meaning": "observation"}}
            for index in range(150)
        ]
    validate(protocol, _protocol_version_candidate_schema())
