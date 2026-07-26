from __future__ import annotations

import copy

import pytest

from orchestrator.scientific_contract import (
    ScientificContractError,
    build_scientific_decision_payload,
    canonical_hash,
    evaluate_scientific_contract,
    normalize_scientific_contract,
    resolve_plan_scientific_contract,
    validate_scientific_decision_payload,
)


def _validity_gates() -> list[dict]:
    return [
        {"gate_id": "required", "kind": "required_metrics_present"},
        {"gate_id": "parser", "kind": "parser_not_suspect"},
        {
            "gate_id": "independent_review",
            "kind":
                "independent_code_plan_data_boundary_review_receipt_present",
        },
    ]


def _review_receipt() -> dict:
    return {
        "protocol": "native-review-receipt-v1",
        "decision_id": 41,
        "review_kind": "bundle_code",
        "review_scope": "code_plan_data_boundary",
        "subject_hash": "a" * 64,
        "receipt_hash": "b" * 64,
    }


def _decision_payload() -> dict:
    return build_scientific_decision_payload(
        build_target_id=7,
        evaluation_id=13,
        evaluation_attempt_id=17,
        contract=_contract(),
        execution_status="succeeded",
        required_metrics={(1, 1)},
        metric_results=[{
            "metric_id": 1, "metric_ver": 1, "value": 0.9,
            "scope": "aggregate",
        }],
        eval_log_hash="c" * 64,
        parser={
            "version": "obs-v1",
            "policy_hash": "d" * 64,
            "fields": {"loss": 0.1},
            "suspect": False,
        },
        independent_review_receipt=_review_receipt(),
    )


def _contract(*, threshold: float = 0.8) -> dict:
    return {
        "validity_gates": _validity_gates(),
        "outcome_rules": [{
            "rule_id": "primary",
            "metric_id": 1,
            "metric_ver": 1,
            "operator": "ge",
            "threshold": threshold,
            "if_true": "supported",
            "if_false": "refuted",
        }],
    }


def test_failed_validity_gate_cannot_be_overridden_by_good_metric():
    result = evaluate_scientific_contract(
        _contract(),
        execution_status="succeeded",
        required_metrics={(1, 1)},
        metric_results=[{
            "metric_id": 1, "metric_ver": 1,
            "value": 0.99, "scope": "aggregate",
        }],
        parser_suspect=True,
        independent_code_plan_data_boundary_review_receipt_present=True,
    )

    assert result["execution_status"] == "succeeded"
    assert result["validity_status"] == "invalid"
    assert result["scientific_outcome"] == "unavailable"
    assert result["pool_eligibility"] == "ineligible"
    assert result["failed_gate_ids"] == ["parser"]


def test_valid_negative_result_is_pool_eligible():
    result = evaluate_scientific_contract(
        _contract(),
        execution_status="succeeded",
        required_metrics={(1, 1)},
        metric_results=[{
            "metric_id": 1, "metric_ver": 1,
            "value": 0.4, "scope": "aggregate",
        }],
        parser_suspect=False,
        independent_code_plan_data_boundary_review_receipt_present=True,
    )

    assert result["validity_status"] == "valid"
    assert result["scientific_outcome"] == "refuted"
    assert result["pool_eligibility"] == "eligible"


def test_valid_result_without_outcome_rule_is_inconclusive_and_eligible():
    result = evaluate_scientific_contract(
        {
            "validity_gates": _validity_gates(),
            "outcome_rules": [],
        },
        execution_status="succeeded",
        required_metrics={(1, 1)},
        metric_results=[{
            "metric_id": 1, "metric_ver": 1,
            "value": 0.4, "scope": "aggregate",
        }],
        parser_suspect=False,
        independent_code_plan_data_boundary_review_receipt_present=True,
    )

    assert result["validity_status"] == "valid"
    assert result["scientific_outcome"] == "inconclusive"
    assert result["pool_eligibility"] == "eligible"


def test_conflicting_outcome_rules_conservatively_classify_inconclusive():
    contract = _contract()
    contract["outcome_rules"].append({
        "rule_id": "secondary",
        "metric_id": 2,
        "metric_ver": 1,
        "operator": "ge",
        "threshold": 0.8,
        "if_true": "refuted",
        "if_false": "supported",
    })
    result = evaluate_scientific_contract(
        contract,
        execution_status="succeeded",
        required_metrics={(1, 1), (2, 1)},
        metric_results=[
            {"metric_id": 1, "metric_ver": 1, "value": 0.9},
            {"metric_id": 2, "metric_ver": 1, "value": 0.9},
        ],
        parser_suspect=False,
        independent_code_plan_data_boundary_review_receipt_present=True,
    )

    assert result["validity_status"] == "valid"
    assert result["scientific_outcome"] == "inconclusive"
    assert result["pool_eligibility"] == "eligible"


def test_unknown_validity_gate_fails_closed():
    with pytest.raises(ScientificContractError, match="validity gate"):
        evaluate_scientific_contract(
            {
                "validity_gates": [{"gate_id": "x", "kind": "model_says_ok"}],
                "outcome_rules": [],
            },
            execution_status="succeeded",
            required_metrics=set(),
            metric_results=[],
            parser_suspect=False,
            independent_code_plan_data_boundary_review_receipt_present=True,
        )


@pytest.mark.parametrize(
    "missing_kind",
    [
        "required_metrics_present",
        "parser_not_suspect",
        "independent_code_plan_data_boundary_review_receipt_present",
    ],
)
def test_explicit_contract_cannot_omit_mandatory_validity_gate(missing_kind):
    contract = {
        "validity_gates": [
            gate for gate in _validity_gates()
            if gate["kind"] != missing_kind
        ],
        "outcome_rules": [],
    }

    with pytest.raises(ScientificContractError, match="mandatory"):
        normalize_scientific_contract(contract)


def test_missing_independent_review_receipt_is_invalid_not_no_leak_claim():
    result = evaluate_scientific_contract(
        _contract(),
        execution_status="succeeded",
        required_metrics={(1, 1)},
        metric_results=[{
            "metric_id": 1, "metric_ver": 1,
            "value": 0.99, "scope": "aggregate",
        }],
        parser_suspect=False,
        independent_code_plan_data_boundary_review_receipt_present=False,
    )

    assert result["validity_status"] == "invalid"
    assert result["scientific_outcome"] == "unavailable"
    assert result["pool_eligibility"] == "ineligible"
    assert result["failed_gate_ids"] == ["independent_review"]


def test_scientific_decision_payload_canonically_binds_owner_facts():
    payload = build_scientific_decision_payload(
        build_target_id=7,
        evaluation_id=13,
        evaluation_attempt_id=17,
        contract=_contract(),
        execution_status="succeeded",
        required_metrics={(2, 1), (1, 1)},
        metric_results=[
            {
                "metric_id": 2, "metric_ver": 1, "value": 0.3,
                "scope": "aggregate",
            },
            {
                "metric_id": 2, "metric_ver": 1, "value": 0.2,
                "scope": "fold", "checkpoint_id": 23,
            },
            {
                "metric_id": 1, "metric_ver": 1, "value": 0.9,
                "scope": "aggregate",
            },
        ],
        eval_log_hash="c" * 64,
        parser={
            "version": "obs-v1",
            "policy_hash": "d" * 64,
            "fields": {"loss": 0.1},
            "suspect": False,
        },
        independent_review_receipt=_review_receipt(),
    )

    assert payload["protocol"] == "bundle-scientific-contract-v1"
    assert payload["contract_hash"] == canonical_hash(payload["contract"])
    assert payload["facts_hash"] == canonical_hash(payload["facts"])
    assert payload["facts"]["required_metrics"] == [
        {"metric_id": 1, "metric_ver": 1},
        {"metric_id": 2, "metric_ver": 1},
    ]
    assert payload["facts"]["metric_results"][0]["metric_id"] == 1
    assert payload["facts"]["eval_log_hash"] == "c" * 64
    assert (
        payload["facts"]["independent_review_receipt"]
        == _review_receipt()
    )
    assert payload["validity_status"] == "valid"


def test_scientific_decision_payload_rejects_unbound_review_receipt():
    receipt = _review_receipt()
    receipt["receipt_hash"] = "not-a-hash"

    with pytest.raises(ScientificContractError, match="review receipt"):
        build_scientific_decision_payload(
            build_target_id=7,
            evaluation_id=13,
            evaluation_attempt_id=17,
            contract=_contract(),
            execution_status="succeeded",
            required_metrics={(1, 1)},
            metric_results=[{
                "metric_id": 1, "metric_ver": 1, "value": 0.9,
                "scope": "aggregate",
            }],
            eval_log_hash="c" * 64,
            parser={
                "version": "obs-v1",
                "policy_hash": "d" * 64,
                "fields": {},
                "suspect": False,
            },
            independent_review_receipt=receipt,
        )


def test_scientific_decision_payload_strict_validation_round_trip():
    payload = _decision_payload()

    assert validate_scientific_decision_payload(
        payload,
        expected_build_target_id=7,
        expected_evaluation_id=13,
        expected_attempt_id=17,
    ) == payload


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("contract_hash", "0" * 64),
        ("facts_hash", "0" * 64),
        ("execution_status", "failed"),
        ("validity_status", "invalid"),
        ("scientific_outcome", "refuted"),
        ("pool_eligibility", "ineligible"),
    ],
)
def test_scientific_decision_payload_rejects_hash_or_axis_tampering(
        field, value):
    payload = _decision_payload()
    payload[field] = value

    with pytest.raises(ScientificContractError):
        validate_scientific_decision_payload(payload)


def test_scientific_decision_payload_rejects_unknown_field():
    payload = _decision_payload()
    payload["model_verdict"] = "pass"

    with pytest.raises(ScientificContractError, match="字段闭包"):
        validate_scientific_decision_payload(payload)


def test_scientific_decision_payload_rejects_duplicate_required_fact():
    payload = copy.deepcopy(_decision_payload())
    payload["facts"]["required_metrics"].append(
        {"metric_id": 1, "metric_ver": 1})
    payload["facts_hash"] = canonical_hash(payload["facts"])

    with pytest.raises(ScientificContractError, match="重复"):
        validate_scientific_decision_payload(payload)


def test_scientific_decision_payload_rejects_expected_scope_mismatch():
    with pytest.raises(ScientificContractError, match="scope"):
        validate_scientific_decision_payload(
            _decision_payload(), expected_attempt_id=999)


def test_plan_contract_resolves_only_required_declared_metric():
    resolved = resolve_plan_scientific_contract(
        {
            "validity_gates": _validity_gates(),
            "outcome_rules": [{
                "rule_id": "primary",
                "metric_id": "m_acc",
                "metric_ver": 1,
                "operator": "ge",
                "threshold": 0.8,
                "if_true": "supported",
                "if_false": "refuted",
            }],
        },
        metric_ids={"m_acc": 17},
        declared_metrics={("m_acc", 1)},
        required_metrics={(17, 1)},
    )

    assert resolved["outcome_rules"][0]["metric_id"] == 17


def test_plan_contract_rejects_outcome_metric_not_required_by_target():
    with pytest.raises(ScientificContractError, match="required"):
        resolve_plan_scientific_contract(
            {
                "validity_gates": _validity_gates(),
                "outcome_rules": [{
                    "rule_id": "primary",
                    "metric_id": "m_acc",
                    "metric_ver": 1,
                    "operator": "ge",
                    "threshold": 0.8,
                    "if_true": "supported",
                    "if_false": "refuted",
                }],
            },
            metric_ids={"m_acc": 17},
            declared_metrics={("m_acc", 1)},
            required_metrics=set(),
        )
