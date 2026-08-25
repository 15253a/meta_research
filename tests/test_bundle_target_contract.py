from __future__ import annotations

import copy
import hashlib
from dataclasses import fields

import pytest

from meta_research.bundle_protocol import ExperimentBrief, TargetCandidate

from meta_research.bundle_target_contract import (
    FORMAL_STRATEGY_UPDATE_SCHEMA_REF,
    FORMAL_TARGET_CANDIDATE_SCHEMA_REF,
    MEASUREMENT_CONTRACT_CANDIDATE_SCHEMA_REF,
    PROTOCOL_VERSION_CANDIDATE_SCHEMA_REF,
    ROLLING_STRATEGY_STATE_SCHEMA_REF,
    BundleTargetContractError,
    LegacyV2TargetSpec,
    apply_strategy_update,
    build_normalized_completion_contract,
    formal_target_candidate_from_dict,
    formal_target_candidate_to_dict,
    measurement_contract_hash,
    normalized_completion_contract_from_dict,
    normalized_completion_contract_to_dict,
    parse_legacy_v2_target_spec,
    rolling_strategy_state_from_dict,
    rolling_strategy_state_to_dict,
    start_rolling_strategy,
    strategy_update_from_dict,
)
from meta_research.plan_contract import PLAN_DOCUMENT_SCHEMA_REF


def _hash(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _receipt(label: str, subject: str) -> dict[str, object]:
    return {
        "receipt_ref": f"receipt-{label}",
        "subject_ref": subject,
        "verified": True,
        "currentness_known": True,
        "current": True,
    }


def _execution() -> dict[str, object]:
    return {"legacy_adapter": "diagnostic-only"}


def _plan(*, second_experiment: bool = False) -> dict[str, object]:
    briefs: list[dict[str, object]] = [
        {
            "experiment_key": "experiment-a",
            "gap_obligation_keys": ["gap-a"],
            "goal": "回答 gap-a。",
            "characteristics": "两个独立比较 cell。",
            "boundary_constraints": "shared-model 必须 held fixed。",
            "semantic_delta": "只改变规则路线。",
            "contributing_idea_refs": ["idea-a"],
        }
    ]
    gaps = ["gap-a"]
    if second_experiment:
        briefs.append(
            {
                "experiment_key": "experiment-b",
                "gap_obligation_keys": ["gap-b"],
                "goal": "回答 gap-b。",
                "characteristics": "独立确认。",
                "boundary_constraints": "shared-model 必须 held fixed。",
                "semantic_delta": "只改变确认路线。",
                "contributing_idea_refs": ["idea-b"],
            }
        )
        gaps.append("gap-b")
    return {
        "schema_ref": PLAN_DOCUMENT_SCHEMA_REF,
        "kind": "PlanDocument",
        "question_ref": "question-1",
        "idea_set_ref": "idea-set-1",
        "context_pack_ref": "plan-context-1",
        "answer_contract": {"answer_contract_hash": _hash("answer")},
        "evidence_reuse_set": [],
        "coverage": [],
        "gap_set": gaps,
        "experiment_briefs": briefs,
        "idea_trace": [],
        "bundle_disposition": "experiments_required",
        "source_bindings": {"accepted": True},
    }


def _normalizations(
    *, second_experiment: bool = False
) -> tuple[dict[str, object], ...]:
    result = [
        {
            "experiment_key": "experiment-a",
            "held_fixed_slots": ["shared-model"],
            "required_measurement_unit_keys": ["cell-a", "cell-b"],
        }
    ]
    if second_experiment:
        result.append(
            {
                "experiment_key": "experiment-b",
                "held_fixed_slots": ["shared-model"],
                "required_measurement_unit_keys": ["cell-c"],
            }
        )
    return tuple(result)


def _contract(*, second_experiment: bool = False):
    plan = _plan(second_experiment=second_experiment)
    return plan, build_normalized_completion_contract(
        plan,
        _normalizations(second_experiment=second_experiment),
    )


def _source(
    label: str,
    implementation_ref: str,
    *,
    tier: str = "self-implementation",
) -> dict[str, object]:
    implementation_hash = _hash(f"implementation:{label}")
    source: dict[str, object] = {
        "source_ref": f"source-{label}",
        "exact_version_ref": f"version-{label}",
        "implementation_revision_ref": implementation_ref,
        "eligible_tier": tier,
        "verification_receipt": _receipt(
            f"source-{label}", f"version-{label}"
        ),
        "implementation_binding": {
            "subject_ref": implementation_ref,
            "content_hash_ref": implementation_hash,
        },
        "implementation_acceptance_receipt": _receipt(
            f"implementation-{label}", implementation_hash
        ),
        "eligibility_anchor_ref": None,
        "eligibility_binding": None,
        "eligibility_receipt": None,
        "license_ref": None,
        "content_hash_ref": None,
        "patch_ref": None,
    }
    if tier in {"accepted-local", "related-history", "global-baseline-pool"}:
        eligibility_hash = _hash(f"eligibility:{label}")
        source.update(
            {
                "eligibility_anchor_ref": f"target-commit-{label}",
                "eligibility_binding": {
                    "subject_ref": f"eligibility-{label}",
                    "content_hash_ref": eligibility_hash,
                },
                "eligibility_receipt": _receipt(
                    f"eligibility-{label}", eligibility_hash
                ),
            }
        )
    if tier == "mature-external":
        source.update(
            {
                "license_ref": f"license-{label}",
                "content_hash_ref": _hash(f"source-content:{label}"),
                "patch_ref": f"patch-{label}",
            }
        )
    return source


def _measurement_contract(
    label: str,
    cell: str,
    *,
    experiment_keys: tuple[str, ...],
) -> dict[str, object]:
    part_keys = [f"part:{label}:second", f"part:{label}:first"]
    return {
        "schema_ref": MEASUREMENT_CONTRACT_CANDIDATE_SCHEMA_REF,
        "experiment_keys": list(experiment_keys),
        "measurement_unit_key": cell,
        "baseline_forward_contract": {
            "schema_ref": "test/baseline-forward/v1",
            "input_role": "accepted baseline",
            "output_role": "frozen prediction",
        },
        "variant_recipe": {
            "schema_ref": "test/variant-recipe/v1",
            "semantic_delta": f"delta:{label}",
        },
        "evaluation_protocol_lineage": {
            "schema_ref": "test/evaluation-lineage/v1",
            "parent_ref": f"protocol-family:{label}",
        },
        "protocol_version": {
            "schema_ref": PROTOCOL_VERSION_CANDIDATE_SCHEMA_REF,
            "evaluation_data": {
                "dataset_ref": f"dataset:{label}",
                "selection": "frozen",
            },
            "split": {
                "split_ref": f"split:{label}",
                "kind": "fixed",
            },
            "preprocessing": {
                "pipeline_ref": f"preprocessing:{label}",
                "steps": ["normalize", "validate"],
            },
            "required_metrics": [
                {
                    "metric_key": "metric:zeta",
                    "definition": {
                        "formula": "correct / total",
                        "units": "ratio",
                        "direction": "maximize",
                        "value_schema": {"type": "number", "minimum": 0},
                    },
                },
                {
                    "metric_key": "metric:alpha",
                    "definition": {
                        "formula": "conflict count",
                        "units": "count",
                        "direction": "minimize",
                        "value_schema": {"type": "integer", "minimum": 0},
                    },
                },
            ],
            "optional_metrics": [
                {
                    "metric_key": "metric:diagnostic",
                    "definition": {
                        "formula": "covered / total",
                        "units": "ratio",
                        "direction": "maximize",
                        "value_schema": {"type": "number", "minimum": 0},
                    },
                }
            ],
            "internal_part_keys": part_keys,
            "aggregation": {
                "rule_ref": f"aggregation:{label}:mean-v1",
                "rule": {
                    "kind": "arithmetic_mean",
                    "ordered_part_keys": part_keys,
                },
            },
            "preregistered_stop_rules": [
                {
                    "rule_ref": f"stop:{label}:fixed-budget-v1",
                    "rule": {
                        "kind": "fixed_budget",
                        "maximum_observations": 2,
                    },
                }
            ],
        },
        "checkpoint_policy": "forbidden",
        "result_schema_ref": "test/measurement-result/v1",
        "result_schema": {
            "type": "object",
            "required": ["metric_values"],
            "properties": {
                "metric_values": {
                    "type": "array",
                    "ordered_by": "required_metrics",
                }
            },
        },
    }


def _candidate(
    contract,
    label: str,
    cell: str,
    *,
    experiment_keys: tuple[str, ...] = ("experiment-a",),
    held_revision: str = "implementation-shared",
    depends_on: tuple[str, ...] = (),
    tier: str = "self-implementation",
) -> dict[str, object]:
    implementation_ref = f"implementation-{label}"
    contract_doc = normalized_completion_contract_to_dict(contract)
    semantics_by_key = {
        row["brief"]["experiment_key"]: row["semantic_inputs"]
        for row in contract_doc["experiments"]
    }
    decisions: list[dict[str, object]] = []
    tier_index = (
        "accepted-local",
        "related-history",
        "global-baseline-pool",
        "mature-external",
        "self-implementation",
    ).index(tier)
    for prior in (
        "accepted-local",
        "related-history",
        "global-baseline-pool",
        "mature-external",
        "self-implementation",
    )[:tier_index]:
        decisions.append(
            {
                "tier": prior,
                "disposition": "not_found",
                "reason_ref": f"reason-{label}-{prior}",
                "source_proofs": [],
            }
        )
    decisions.append(
        {
            "tier": tier,
            "disposition": "selected",
            "reason_ref": f"reason-{label}-{tier}",
            "source_proofs": [_source(label, implementation_ref, tier=tier)],
        }
    )
    return {
        "schema_ref": FORMAL_TARGET_CANDIDATE_SCHEMA_REF,
        "candidate": {
            "local_label": label,
            "experiment_keys": list(experiment_keys),
            "measurement_unit_keys": [cell],
            "held_fixed_bindings": [
                {
                    "semantic_slot": "shared-model",
                    "implementation_revision_ref": held_revision,
                }
            ],
            "implementation_revision_ref": implementation_ref,
            "code_changed": False,
            "reuse_trace": {
                "tier_decisions": decisions,
                "greenfield_exception": (
                    "simple-implementation"
                    if tier == "self-implementation"
                    else None
                ),
            },
            "routes": [
                {
                    "route_ref": f"route-{label}",
                    "known_external_operation_refs": [],
                }
            ],
            "depends_on_labels": list(depends_on),
            "direct_accepted_input_asset_refs": [f"asset-{label}"],
        },
        "semantic_inputs": [
            copy.deepcopy(semantics_by_key[key]) for key in experiment_keys
        ],
        "measurement_contract": _measurement_contract(
            label,
            cell,
            experiment_keys=experiment_keys,
        ),
        "risk_class": "normal",
    }


def _update(
    revision: int,
    candidates: list[dict[str, object]],
    *,
    complete: bool,
    requires: tuple[str, ...] = (),
) -> dict[str, object]:
    return {
        "schema_ref": FORMAL_STRATEGY_UPDATE_SCHEMA_REF,
        "revision": revision,
        "candidates": candidates,
        "requires_accepted_labels": list(requires),
        "strategy_complete": complete,
    }


def test_measurement_wrapper_does_not_extend_fixed_prototype_records() -> None:
    assert tuple(field.name for field in fields(ExperimentBrief)) == (
        "experiment_key",
        "semantic_delta",
        "held_fixed_slots",
        "required_measurement_unit_keys",
    )
    assert tuple(field.name for field in fields(TargetCandidate)) == (
        "local_label",
        "experiment_keys",
        "measurement_unit_keys",
        "held_fixed_bindings",
        "implementation_revision_ref",
        "code_changed",
        "reuse_trace",
        "routes",
        "depends_on_labels",
        "direct_accepted_input_asset_refs",
    )


def test_normalization_freezes_all_plan_semantics_and_cells() -> None:
    plan, contract = _contract(second_experiment=True)
    document = normalized_completion_contract_to_dict(contract)
    restored = normalized_completion_contract_from_dict(
        copy.deepcopy(document), plan_document=plan
    )

    assert restored == contract
    first = restored.experiments[0]
    assert first.semantic_inputs.goal == "回答 gap-a。"
    assert first.semantic_inputs.characteristics == "两个独立比较 cell。"
    assert first.semantic_inputs.boundary_constraints.startswith("shared-model")
    assert first.semantic_inputs.semantic_delta == "只改变规则路线。"
    assert first.brief.required_measurement_unit_keys == ("cell-a", "cell-b")

    partial = _normalizations(second_experiment=True)[:1]
    with pytest.raises(
        BundleTargetContractError, match="experiment_set_incomplete"
    ):
        build_normalized_completion_contract(plan, partial)


def test_formal_planner_rejects_execution_provider_and_metric_routing_fields() -> None:
    plan = _plan()
    routed_normalization = dict(_normalizations()[0])
    routed_normalization["execution"] = {"provider": "micro"}
    with pytest.raises(
        BundleTargetContractError,
        match="completion_normalization_invalid",
    ):
        build_normalized_completion_contract(plan, (routed_normalization,))

    _plan_value, contract = _contract()
    completion_document = normalized_completion_contract_to_dict(contract)
    completion_document["experiments"][0]["semantic_inputs"][
        "required_metrics"
    ] = ["planner-selected-metric"]
    with pytest.raises(
        BundleTargetContractError,
        match="semantic_normalization_invalid",
    ):
        normalized_completion_contract_from_dict(
            completion_document,
            plan_document=plan,
        )

    routed_candidate = _candidate(contract, "target-a", "cell-a")
    routed_candidate["execution"] = {"provider": "micro"}
    with pytest.raises(
        BundleTargetContractError,
        match="formal_target_candidate_invalid",
    ):
        formal_target_candidate_from_dict(
            routed_candidate,
            completion_contract=contract,
        )


def test_full_candidate_round_trip_uses_canonical_prototype_records() -> None:
    _, contract = _contract()
    document = _candidate(contract, "target-a", "cell-a")
    candidate = formal_target_candidate_from_dict(
        document, completion_contract=contract
    )

    assert candidate.candidate.measurement_unit_keys == ("cell-a",)
    assert candidate.candidate.reuse_trace.tier_decisions[-1].tier == (
        "self-implementation"
    )
    assert candidate.measurement_contract.protocol_version.required_metric_keys == (
        "metric:zeta",
        "metric:alpha",
    )
    assert candidate.measurement_contract.protocol_version.internal_part_keys == (
        "part:target-a:second",
        "part:target-a:first",
    )
    assert formal_target_candidate_to_dict(
        candidate, completion_contract=contract
    ) == document


def test_measurement_contract_is_closed_and_exactly_bound_to_candidate_cell() -> None:
    _, contract = _contract()
    extra = _candidate(contract, "target-a", "cell-a")
    extra["measurement_contract"]["execution"] = {"provider": "micro"}
    with pytest.raises(BundleTargetContractError, match="measurement_contract_invalid"):
        formal_target_candidate_from_dict(extra, completion_contract=contract)

    wrong_cell = _candidate(contract, "target-a", "cell-a")
    wrong_cell["measurement_contract"]["measurement_unit_key"] = "cell-b"
    with pytest.raises(BundleTargetContractError, match="binding_drift"):
        formal_target_candidate_from_dict(wrong_cell, completion_contract=contract)

    wrong_experiment = _candidate(contract, "target-a", "cell-a")
    wrong_experiment["measurement_contract"]["experiment_keys"] = [
        "experiment-b"
    ]
    with pytest.raises(BundleTargetContractError, match="binding_drift"):
        formal_target_candidate_from_dict(
            wrong_experiment,
            completion_contract=contract,
        )


def test_multi_experiment_target_has_one_contract_with_the_exact_key_order() -> None:
    plan = _plan(second_experiment=True)
    contract = build_normalized_completion_contract(
        plan,
        (
            {
                "experiment_key": "experiment-a",
                "held_fixed_slots": ["shared-model"],
                "required_measurement_unit_keys": ["cell:shared"],
            },
            {
                "experiment_key": "experiment-b",
                "held_fixed_slots": ["shared-model"],
                "required_measurement_unit_keys": ["cell:shared"],
            },
        ),
    )
    document = _candidate(
        contract,
        "target-shared",
        "cell:shared",
        experiment_keys=("experiment-b", "experiment-a"),
    )
    parsed = formal_target_candidate_from_dict(
        document,
        completion_contract=contract,
    )
    assert parsed.measurement_contract.experiment_keys == (
        "experiment-b",
        "experiment-a",
    )

    reordered = copy.deepcopy(document)
    reordered["measurement_contract"]["experiment_keys"] = [
        "experiment-a",
        "experiment-b",
    ]
    with pytest.raises(BundleTargetContractError, match="binding_drift"):
        formal_target_candidate_from_dict(
            reordered,
            completion_contract=contract,
        )


@pytest.mark.parametrize(
    ("document_key", "routing_key"),
    [
        ("baseline_forward_contract", "execution"),
        ("variant_recipe", "provider"),
        ("evaluation_protocol_lineage", "adapter_kind"),
        ("result_schema", "command"),
    ],
)
def test_measurement_domain_documents_reject_runtime_routing_at_the_root(
    document_key: str,
    routing_key: str,
) -> None:
    _, contract = _contract()
    candidate = _candidate(contract, "target-a", "cell-a")
    candidate["measurement_contract"][document_key][routing_key] = "forbidden"
    with pytest.raises(BundleTargetContractError, match="runtime_routing"):
        formal_target_candidate_from_dict(candidate, completion_contract=contract)


@pytest.mark.parametrize(
    "routing_key",
    ["provider", "adapter_kind", "image", "command", "execution"],
)
def test_measurement_domain_documents_reject_nested_runtime_routing_keys(
    routing_key: str,
) -> None:
    _, contract = _contract()
    candidate = _candidate(contract, "target-a", "cell-a")
    candidate["measurement_contract"]["protocol_version"]["evaluation_data"][
        "nested"
    ] = {"domain_note": "ordinary text remains allowed", routing_key: "forbidden"}
    with pytest.raises(BundleTargetContractError, match="runtime_routing"):
        formal_target_candidate_from_dict(candidate, completion_contract=contract)


def test_metric_definitions_are_ordered_complete_and_hash_bound() -> None:
    _, contract = _contract()
    original = _candidate(contract, "target-a", "cell-a")
    parsed = formal_target_candidate_from_dict(
        original,
        completion_contract=contract,
    )
    original_hash = measurement_contract_hash(parsed.measurement_contract)

    changed = copy.deepcopy(original)
    required = changed["measurement_contract"]["protocol_version"][
        "required_metrics"
    ]
    required[0]["definition"]["formula"] = "weighted correct / total"
    reparsed = formal_target_candidate_from_dict(
        changed,
        completion_contract=contract,
    )
    assert measurement_contract_hash(reparsed.measurement_contract) != original_hash
    assert [item["metric_key"] for item in required] == [
        "metric:zeta",
        "metric:alpha",
    ]

    duplicate = copy.deepcopy(original)
    duplicate_metrics = duplicate["measurement_contract"]["protocol_version"][
        "required_metrics"
    ]
    duplicate_metrics.append(copy.deepcopy(duplicate_metrics[0]))
    with pytest.raises(BundleTargetContractError, match="metric_key_duplicate"):
        formal_target_candidate_from_dict(duplicate, completion_contract=contract)

    overlap = copy.deepcopy(original)
    protocol = overlap["measurement_contract"]["protocol_version"]
    protocol["optional_metrics"][0]["metric_key"] = "metric:zeta"
    with pytest.raises(BundleTargetContractError, match="metric_sets_overlap"):
        formal_target_candidate_from_dict(overlap, completion_contract=contract)


def test_protocol_parts_aggregation_and_stop_rules_are_exact_and_closed() -> None:
    _, contract = _contract()
    candidate = _candidate(contract, "target-a", "cell-a")
    parsed = formal_target_candidate_from_dict(candidate, completion_contract=contract)
    protocol = parsed.measurement_contract.protocol_version
    assert protocol.aggregation is not None
    assert protocol.aggregation.rule.as_dict()["ordered_part_keys"] == [
        "part:target-a:second",
        "part:target-a:first",
    ]
    assert protocol.preregistered_stop_rules[0].rule.as_dict() == {
        "kind": "fixed_budget",
        "maximum_observations": 2,
    }

    missing_aggregation = copy.deepcopy(candidate)
    missing_aggregation["measurement_contract"]["protocol_version"][
        "aggregation"
    ] = None
    with pytest.raises(BundleTargetContractError, match="parts_aggregation_incomplete"):
        formal_target_candidate_from_dict(
            missing_aggregation,
            completion_contract=contract,
        )

    aggregation_without_parts = copy.deepcopy(candidate)
    aggregation_without_parts["measurement_contract"]["protocol_version"][
        "internal_part_keys"
    ] = []
    with pytest.raises(BundleTargetContractError, match="parts_aggregation_incomplete"):
        formal_target_candidate_from_dict(
            aggregation_without_parts,
            completion_contract=contract,
        )


def test_formal_wrapper_requires_explicit_owner_admission_risk() -> None:
    _, contract = _contract()
    missing = _candidate(contract, "target-a", "cell-a")
    del missing["risk_class"]
    with pytest.raises(BundleTargetContractError):
        formal_target_candidate_from_dict(missing, completion_contract=contract)

    unknown = _candidate(contract, "target-a", "cell-a")
    unknown["risk_class"] = "unknown"
    with pytest.raises(BundleTargetContractError, match="risk_class_invalid"):
        formal_target_candidate_from_dict(unknown, completion_contract=contract)


def test_candidate_rejects_multi_cell_unknown_dependency_and_semantic_drift() -> None:
    _, contract = _contract()
    multi = _candidate(contract, "target-a", "cell-a")
    multi["candidate"]["measurement_unit_keys"] = ["cell-a", "cell-b"]
    with pytest.raises(BundleTargetContractError, match="exactly_one_measurement_cell"):
        formal_target_candidate_from_dict(multi, completion_contract=contract)

    semantic_drift = _candidate(contract, "target-a", "cell-a")
    semantic_drift["semantic_inputs"][0]["goal"] = "缩小后的目标"
    with pytest.raises(BundleTargetContractError, match="semantic_input_drift"):
        formal_target_candidate_from_dict(
            semantic_drift, completion_contract=contract
        )

    unknown = strategy_update_from_dict(
        _update(
            1,
            [
                _candidate(
                    contract,
                    "target-a",
                    "cell-a",
                    depends_on=("missing-target",),
                )
            ],
            complete=False,
        ),
        completion_contract=contract,
    )
    with pytest.raises(BundleTargetContractError, match="dependency_unknown"):
        apply_strategy_update(
            start_rolling_strategy(contract),
            unknown,
            completion_contract=contract,
        )


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("exact_version_ref", "reuse version"),
        ("license_ref", "mature external license"),
        ("content_hash_ref", "mature external source content hash"),
        ("patch_ref", "mature external patch"),
    ],
)
def test_external_reuse_requires_version_license_content_and_patch(
    field: str, expected: str
) -> None:
    _, contract = _contract()
    document = _candidate(
        contract, "external", "cell-a", tier="mature-external"
    )
    selected = document["candidate"]["reuse_trace"]["tier_decisions"][-1]
    selected["source_proofs"][0][field] = None if field != "exact_version_ref" else ""
    with pytest.raises(BundleTargetContractError, match=expected):
        formal_target_candidate_from_dict(document, completion_contract=contract)


def test_owner_tier_reuse_requires_eligibility_and_trace_cannot_be_empty() -> None:
    _, contract = _contract()
    owner_source = _candidate(
        contract, "owner-source", "cell-a", tier="accepted-local"
    )
    source = owner_source["candidate"]["reuse_trace"]["tier_decisions"][0][
        "source_proofs"
    ][0]
    source["eligibility_receipt"] = None
    with pytest.raises(BundleTargetContractError, match="eligibility_missing"):
        formal_target_candidate_from_dict(
            owner_source, completion_contract=contract
        )

    no_trace = _candidate(contract, "no-trace", "cell-a")
    no_trace["candidate"]["reuse_trace"]["tier_decisions"] = []
    with pytest.raises(BundleTargetContractError, match="reuse_trace_missing"):
        formal_target_candidate_from_dict(no_trace, completion_contract=contract)


def test_rolling_initial_append_and_empty_seal_cover_exact_cells() -> None:
    _, contract = _contract()
    state = start_rolling_strategy(contract)
    first = strategy_update_from_dict(
        _update(
            1,
            [_candidate(contract, "target-a", "cell-a")],
            complete=False,
        ),
        completion_contract=contract,
    )
    state = apply_strategy_update(state, first, completion_contract=contract)
    second = strategy_update_from_dict(
        _update(
            2,
            [
                _candidate(
                    contract,
                    "target-b",
                    "cell-b",
                    depends_on=("target-a",),
                )
            ],
            complete=False,
            requires=("target-a",),
        ),
        completion_contract=contract,
    )
    state = apply_strategy_update(
        state,
        second,
        completion_contract=contract,
        accepted_labels=frozenset({"target-a"}),
    )
    seal = strategy_update_from_dict(
        _update(3, [], complete=True), completion_contract=contract
    )
    state = apply_strategy_update(state, seal, completion_contract=contract)

    assert state.strategy.strategy_complete is True
    assert tuple(item.candidate.local_label for item in state.candidates) == (
        "target-a",
        "target-b",
    )
    restored = rolling_strategy_state_from_dict(
        rolling_strategy_state_to_dict(state, completion_contract=contract),
        completion_contract=contract,
    )
    assert restored == state


def test_seal_rejects_partial_experiment_set_and_held_fixed_drift() -> None:
    _, contract = _contract(second_experiment=True)
    partial = strategy_update_from_dict(
        _update(
            1,
            [
                _candidate(contract, "target-a", "cell-a"),
                _candidate(contract, "target-b", "cell-b"),
            ],
            complete=True,
        ),
        completion_contract=contract,
    )
    with pytest.raises(BundleTargetContractError, match="cell_coverage_invalid"):
        apply_strategy_update(
            start_rolling_strategy(contract),
            partial,
            completion_contract=contract,
        )

    _, one_contract = _contract()
    drift = strategy_update_from_dict(
        _update(
            1,
            [
                _candidate(
                    one_contract,
                    "target-a",
                    "cell-a",
                    held_revision="implementation-shared-a",
                ),
                _candidate(
                    one_contract,
                    "target-b",
                    "cell-b",
                    held_revision="implementation-shared-b",
                ),
            ],
            complete=True,
        ),
        completion_contract=one_contract,
    )
    with pytest.raises(BundleTargetContractError, match="held_fixed_binding_drift"):
        apply_strategy_update(
            start_rolling_strategy(one_contract),
            drift,
            completion_contract=one_contract,
        )


def test_durable_snapshot_cannot_shrink_candidates_to_claim_complete() -> None:
    _, contract = _contract()
    state = start_rolling_strategy(contract)
    update = strategy_update_from_dict(
        _update(
            1,
            [
                _candidate(contract, "target-a", "cell-a"),
                _candidate(contract, "target-b", "cell-b"),
            ],
            complete=False,
        ),
        completion_contract=contract,
    )
    state = apply_strategy_update(state, update, completion_contract=contract)
    shrunk = rolling_strategy_state_to_dict(state, completion_contract=contract)
    shrunk["revision"] = 2
    shrunk["candidates"] = shrunk["candidates"][:1]
    shrunk["strategy_complete"] = True
    with pytest.raises(BundleTargetContractError):
        rolling_strategy_state_from_dict(
            shrunk,
            completion_contract=contract,
            previous_state=state,
        )


def test_durable_snapshot_cannot_rewrite_frozen_measurement_content() -> None:
    _, contract = _contract()
    state = apply_strategy_update(
        start_rolling_strategy(contract),
        strategy_update_from_dict(
            _update(
                1,
                [_candidate(contract, "target-a", "cell-a")],
                complete=False,
            ),
            completion_contract=contract,
        ),
        completion_contract=contract,
    )
    rewritten = rolling_strategy_state_to_dict(
        state,
        completion_contract=contract,
    )
    rewritten["revision"] = 2
    rewritten["candidates"][0]["measurement_contract"]["protocol_version"][
        "required_metrics"
    ][0]["definition"]["formula"] = "post-hoc metric rewrite"
    with pytest.raises(
        BundleTargetContractError,
        match="rolling_candidate_list_shrank_or_changed",
    ):
        rolling_strategy_state_from_dict(
            rewritten,
            completion_contract=contract,
            previous_state=state,
        )


def test_legacy_v2_is_read_only_and_never_a_formal_candidate() -> None:
    _, contract = _contract()
    legacy = {
        "target_key": "legacy-a",
        "title": "legacy target",
        "experiment_key": "experiment-a",
        "gap_obligation_keys": ["gap-a"],
        "depends_on": [],
        "goal": "legacy goal",
        "characteristics": "legacy characteristics",
        "boundary_constraints": "legacy boundary",
        "semantic_delta": "legacy delta",
        "contributing_idea_refs": ["idea-a"],
        "risk_class": "normal",
        "execution": _execution(),
    }
    parsed = parse_legacy_v2_target_spec(legacy)
    assert isinstance(parsed, LegacyV2TargetSpec)
    assert not hasattr(parsed, "strategy_complete")
    assert not hasattr(parsed, "candidate")
    with pytest.raises(BundleTargetContractError):
        formal_target_candidate_from_dict(legacy, completion_contract=contract)
