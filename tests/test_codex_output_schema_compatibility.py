from __future__ import annotations

from collections.abc import Iterator

import pytest

from meta_research.bundle_skill import (
    _dispatch_schema,
    _exhaustion_review_schema,
    _review_schema as _bundle_review_schema,
    _schema_template_request as _bundle_template_request,
    _target_batch_schema,
    _target_plan_envelope_schema,
    _target_plan_review_schema,
)
from meta_research.deepfetch import (
    _deepfetch_output_schema,
    _deepfetch_web_evidence_gate_output_schema,
)
from meta_research.idea_skill import (
    IdeaSkillUnavailable,
    _CODEX_JSON_OBJECT_STRING_MARKER,
    _compile_codex_output_schema,
    _decode_codex_provider_output,
    _outcome_envelope_schema,
    _review_finalization_schema as _idea_review_schema,
    _unwrap_codex_root_output,
    _validate_codex_output_schema_dialect,
)
from meta_research.plan_skill import (
    _plan_envelope_schema,
    _review_finalization_schema as _plan_review_schema,
    _schema_template_request as _plan_template_request,
)
from meta_research.reasoning_skill import (
    _reasoning_autonomous_checkpoint_schema,
    _reasoning_primary_output_schema,
    _reasoning_review_response_schema,
    _reasoning_stage_output_schema,
    _schema_template_request as _reasoning_template_request,
)


_FORBIDDEN_PROVIDER_KEYWORDS = frozenset(
    {
        "allOf",
        "contains",
        "dependentRequired",
        "dependentSchemas",
        "else",
        "if",
        "maxContains",
        "maxProperties",
        "minProperties",
        "minContains",
        "not",
        "oneOf",
        "patternProperties",
        "prefixItems",
        "propertyNames",
        "then",
        "unevaluatedItems",
        "unevaluatedProperties",
        "uniqueItems",
    }
)


def _actual_operation_schemas() -> Iterator[tuple[str, dict[str, object]]]:
    yield (
        "deepfetch-web-evidence-gate",
        _deepfetch_web_evidence_gate_output_schema(),
    )
    yield "deepfetch", _deepfetch_output_schema()

    yield (
        "idea-primary",
        _outcome_envelope_schema("question:template", "context:template"),
    )
    yield (
        "idea-review",
        _idea_review_schema("question:template", "context:template"),
    )

    plan = _plan_template_request()
    yield "plan-primary", _plan_envelope_schema(plan)
    yield "plan-review", _plan_review_schema(plan)

    bundle = _bundle_template_request()
    yield "bundle-primary", _target_plan_envelope_schema(bundle)
    yield "bundle-target-review", _target_plan_review_schema(bundle)
    yield "bundle-exhaustion-review", _exhaustion_review_schema(
        reviewed_assessment_hash="a" * 64
    )
    yield "bundle-dispatch", _dispatch_schema(("target:template",))
    yield "bundle-rolling-batch", _target_batch_schema(bundle)
    # This union is frozen into RuntimeBinding even though actual review turns
    # select one of the two concrete schemas above.
    yield "bundle-review-binding", _bundle_review_schema(bundle)

    reasoning = _reasoning_template_request()
    closed = _reasoning_stage_output_schema(reasoning)
    autonomous = _reasoning_autonomous_checkpoint_schema(reasoning)
    yield "reasoning-primary", _reasoning_primary_output_schema(reasoning)
    yield "reasoning-review", _reasoning_review_response_schema(
        reasoning, closed
    )
    yield "reasoning-autonomous-review", _reasoning_review_response_schema(
        reasoning, autonomous
    )
    yield "reasoning-autonomous-resume", _reasoning_review_response_schema(
        reasoning, closed
    )


def _schema_nodes(
    node: object, path: tuple[object, ...] = ()
) -> Iterator[tuple[tuple[object, ...], dict[str, object]]]:
    if not isinstance(node, dict):
        return
    yield path, node
    properties = node.get("properties")
    if isinstance(properties, dict):
        for name, child in properties.items():
            yield from _schema_nodes(child, (*path, "properties", name))
    for key in ("$defs", "definitions"):
        children = node.get(key)
        if isinstance(children, dict):
            for name, child in children.items():
                yield from _schema_nodes(child, (*path, key, name))
    for key in ("additionalProperties", "items"):
        child = node.get(key)
        if isinstance(child, dict):
            yield from _schema_nodes(child, (*path, key))
    for key in ("anyOf",):
        children = node.get(key)
        if isinstance(children, list):
            for index, child in enumerate(children):
                yield from _schema_nodes(child, (*path, key, index))


def _assert_strict_provider_schema(
    operation: str, schema: dict[str, object]
) -> None:
    assert schema.get("type") == "object", operation
    assert "anyOf" not in schema, f"{operation}: root anyOf"
    for path, node in _schema_nodes(schema):
        forbidden = _FORBIDDEN_PROVIDER_KEYWORDS.intersection(node)
        assert not forbidden, f"{operation}:{path}: {sorted(forbidden)}"
        if "const" in node:
            assert "type" in node, f"{operation}:{path}: const without type"
        if node.get("type") != "object":
            continue
        properties = node.get("properties")
        assert isinstance(properties, dict), f"{operation}:{path}: properties"
        assert node.get("additionalProperties") is False, (
            f"{operation}:{path}: additionalProperties"
        )
        required = node.get("required")
        assert isinstance(required, list), f"{operation}:{path}: required"
        assert set(required) == set(properties), (
            f"{operation}:{path}: every property must be required"
        )


@pytest.mark.parametrize(
    ("operation", "raw_schema"),
    tuple(_actual_operation_schemas()),
)
def test_actual_codex_operation_schemas_use_the_supported_strict_dialect(
    operation: str,
    raw_schema: dict[str, object],
) -> None:
    _assert_strict_provider_schema(
        operation, _compile_codex_output_schema(raw_schema)
    )


def _json_object_transport_schema(**changes: int) -> dict[str, object]:
    limits = {
        "max_collection_items": 4,
        "max_depth": 4,
        "max_integer_abs": 100,
        "max_nodes": 16,
        "max_serialized_bytes": 128,
        "max_string_bytes": 16,
    }
    limits.update(changes)
    return {_CODEX_JSON_OBJECT_STRING_MARKER: limits}


def test_canonical_json_object_transport_is_schema_constrained_and_decoded() -> None:
    raw_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"document": _json_object_transport_schema()},
        "required": ["document"],
    }

    provider_schema = _compile_codex_output_schema(raw_schema)
    document_schema = provider_schema["properties"]["document"]
    assert document_schema["type"] == "string"
    assert "canonical JSON object string" in document_schema["description"]
    assert _CODEX_JSON_OBJECT_STRING_MARKER not in document_schema
    assert _decode_codex_provider_output(
        {"document": '{"alpha":[1]}'}, raw_schema
    ) == {"document": {"alpha": [1]}}
    assert _decode_codex_provider_output(
        {"document": '{"alpha":{}}'}, raw_schema
    ) == {"document": {"alpha": {}}}


@pytest.mark.parametrize(
    "encoded",
    [
        '{"alpha":1,"alpha":2}',
        "[]",
        "{}",
        '{"alpha": 1}',
        '{"alpha":{"beta":{"gamma":{"delta":{"epsilon":1}}}}}',
        '{"alpha":[1,2,3,4,5]}',
        '{"alpha":"0123456789abcdefg"}',
        '{"alpha":101}',
        '{"alpha":1e999}',
        '{"alpha":-1e999}',
    ],
)
def test_json_object_transport_rejects_ambiguous_or_out_of_bounds_values(
    encoded: str,
) -> None:
    raw_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"document": _json_object_transport_schema()},
        "required": ["document"],
    }

    with pytest.raises(
        IdeaSkillUnavailable, match="codex_json_object_transport_invalid"
    ):
        _decode_codex_provider_output({"document": encoded}, raw_schema)


def test_provider_root_union_requires_the_exact_frozen_transport_envelope() -> None:
    raw_schema = {
        "oneOf": [
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
            }
        ]
    }

    assert _unwrap_codex_root_output(
        {"provider_output": {"answer": "yes"}}, raw_schema
    ) == {"answer": "yes"}
    for malformed in (
        {"answer": "yes"},
        {"provider_output": {"answer": "yes"}, "extra": True},
        {"provider_output": "yes"},
    ):
        with pytest.raises(
            IdeaSkillUnavailable,
            match="codex_json_object_transport_invalid",
        ):
            _unwrap_codex_root_output(malformed, raw_schema)


def test_production_dialect_gate_rejects_malformed_strict_objects() -> None:
    with pytest.raises(
        IdeaSkillUnavailable, match="codex_output_schema_invalid"
    ):
        _validate_codex_output_schema_dialect(
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {"answer": {"type": "string"}},
                "required": [],
            }
        )


def _nested_object_schema(levels: int) -> dict[str, object]:
    node: dict[str, object] = {"type": "string"}
    for index in range(levels):
        key = f"level_{index}"
        node = {
            "type": "object",
            "additionalProperties": False,
            "properties": {key: node},
            "required": [key],
        }
    return node


@pytest.mark.parametrize(
    "schema",
    [
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                f"field_{index}": {"type": "string"}
                for index in range(5_001)
            },
            "required": [f"field_{index}" for index in range(5_001)],
        },
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "choice": {
                    "type": "string",
                    "enum": [str(index) for index in range(1_001)],
                }
            },
            "required": ["choice"],
        },
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "identity": {"const": "x" * 120_001, "type": "string"}
            },
            "required": ["identity"],
        },
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "choice": {
                    "type": "string",
                    "enum": ["x" * 60 for _index in range(251)],
                }
            },
            "required": ["choice"],
        },
        _nested_object_schema(11),
    ],
)
def test_production_dialect_gate_enforces_global_provider_budgets(
    schema: dict[str, object],
) -> None:
    with pytest.raises(
        IdeaSkillUnavailable, match="codex_output_schema_invalid"
    ):
        _validate_codex_output_schema_dialect(schema)
