from __future__ import annotations

import pytest

from meta_research.experiment_contract import ProtocolExperimentIntent
from meta_research.owners.common import canonical_json
from meta_research.target_execution import (
    PROTOCOL_EVALUATION_ADAPTER,
    PROTOCOL_EVALUATION_REQUEST_MAX_BYTES,
    PROTOCOL_EVALUATION_SCHEMA_REF,
    TargetExecutionContractError,
    target_execution_json_schema,
    target_experiment_intent,
    validate_target_execution,
)


def _execution() -> dict[str, object]:
    return {
        "adapter_kind": PROTOCOL_EVALUATION_ADAPTER,
        "schema_ref": PROTOCOL_EVALUATION_SCHEMA_REF,
        "request": {
            "objective": "比较两组规则输出的一致率。",
            "variant_source": {"kind": "new"},
            "baseline_forward_contract": {
                "schema_ref": "test/rule-forward/v1",
                "input": "identifier set",
                "output": "normalized set",
            },
            "variant_recipe": {
                "schema_ref": "test/rule-variant/v1",
                "operation": "normalize",
            },
            "evaluation_protocol_lineage": {
                "schema_ref": "test/rule-protocol-lineage/v1",
                "name": "agreement",
            },
            "protocol_version": {
                "schema_ref": "test/rule-protocol/v2",
                "required_metrics": ["agreement_rate", "conflict_count"],
                "optional_metrics": [],
            },
            "checkpoint_policy": "forbidden",
            "provider": {
                "adapter_kind": "installed_rule_provider",
                "schema_ref": "test/installed-rule-provider/v1",
                "payload": {"rule": "normalize-and-compare"},
            },
        },
    }


def test_protocol_adapter_builds_a_formal_measurement_intent() -> None:
    execution = _execution()
    assert validate_target_execution(execution) == PROTOCOL_EVALUATION_ADAPTER
    target = {
        "title": "规则一致率 Target",
        "execution": execution,
    }

    intent = target_experiment_intent(
        quest_ref="quest-protocol",
        target_ref="target-protocol",
        target_spec=target,
    )

    assert isinstance(intent, ProtocolExperimentIntent)
    assert intent.execution_request_ref == "bundle-target-target-protocol"
    assert intent.required_metrics == ("agreement_rate", "conflict_count")
    assert intent.checkpoint_policy == "forbidden"
    assert intent.execution == execution["request"]["provider"]
    assert "variant_parameter" not in intent.as_dict()
    assert "sample_count" not in intent.as_dict()
    adapter_kinds = {
        item["properties"]["adapter_kind"]["const"]
        for item in target_execution_json_schema()["oneOf"]
    }
    assert PROTOCOL_EVALUATION_ADAPTER in adapter_kinds


def test_protocol_request_accepts_context_above_legacy_512k_limit() -> None:
    execution = _execution()
    request = execution["request"]
    assert isinstance(request, dict)
    forward_contract = request["baseline_forward_contract"]
    assert isinstance(forward_contract, dict)
    forward_contract["context"] = "x" * 600_000

    assert validate_target_execution(execution) == PROTOCOL_EVALUATION_ADAPTER
    request_bytes = len(canonical_json(request).encode("utf-8"))
    assert 512_000 < request_bytes < PROTOCOL_EVALUATION_REQUEST_MAX_BYTES


def test_protocol_projection_uses_canonical_candidate_label() -> None:
    execution = _execution()
    intent = target_experiment_intent(
        quest_ref="quest-protocol",
        target_ref="target-protocol",
        target_spec={
            "candidate": {"local_label": "formal-protocol-target"},
            "semantic_inputs": [],
            "execution": execution,
            "risk_class": "normal",
        },
    )

    assert intent.title == "formal-protocol-target"


def test_protocol_target_rejects_untyped_or_inconsistent_provider_inputs() -> None:
    execution = _execution()
    execution["request"]["variant_source"] = {
        "kind": "existing",
        "source_variant_run_ref": "variant-run-one",
        "selected_checkpoint_role_refs": [],
    }
    execution["request"]["checkpoint_policy"] = "optional"

    with pytest.raises(
        TargetExecutionContractError, match="target_execution_request_invalid"
    ):
        validate_target_execution(execution)


def test_protocol_schema_exposes_only_installed_provider_capabilities() -> None:
    protocol_catalog = {
        "schema_ref": "meta-research/experiment-provider-capability-catalog/v1",
        "capabilities": [
            {
                "intent_schema_ref": "meta-research/protocol-experiment-intent/v2",
                "definition_schema_ref": (
                    "meta-research/protocol-experiment-definition/v2"
                ),
                "request_kinds": ["new_variant"],
                "execution_adapter_kind": "installed_rule_provider",
                "execution_schema_ref": "test/installed-rule-provider/v1",
            }
        ],
    }
    protocol_schema = target_execution_json_schema(protocol_catalog)
    assert len(protocol_schema["oneOf"]) == 1
    request = protocol_schema["oneOf"][0]["properties"]["request"]
    provider = request["properties"]["provider"]
    assert provider["properties"]["adapter_kind"] == {
        "const": "installed_rule_provider"
    }
    assert provider["properties"]["schema_ref"] == {
        "const": "test/installed-rule-provider/v1"
    }
    sources = request["properties"]["variant_source"]["oneOf"]
    assert [source["properties"]["kind"]["const"] for source in sources] == [
        "new"
    ]
