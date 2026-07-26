"""Deterministic Bundle validity and scientific-outcome classification.

The evaluator keeps execution, validity, outcome, and pool eligibility
orthogonal.  It consumes only a Plan-frozen contract plus owner-derived facts;
model prose and reviewer verdicts are never validity inputs.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Dict, Iterable, Mapping, Sequence


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EXECUTION = {
    "succeeded", "failed", "skipped", "engineering_blocked",
}
MANDATORY_VALIDITY_GATE_KINDS = frozenset({
    "required_metrics_present",
    "parser_not_suspect",
    "independent_code_plan_data_boundary_review_receipt_present",
})
_VALIDITY_GATES = MANDATORY_VALIDITY_GATE_KINDS
_OUTCOMES = {"supported", "refuted", "inconclusive"}
_OPERATORS = {
    "gt": lambda value, threshold: value > threshold,
    "ge": lambda value, threshold: value >= threshold,
    "lt": lambda value, threshold: value < threshold,
    "le": lambda value, threshold: value <= threshold,
    "eq": lambda value, threshold: value == threshold,
    "ne": lambda value, threshold: value != threshold,
}


class ScientificContractError(ValueError):
    """The Plan contract or owner facts are malformed."""


def default_scientific_contract() -> Dict[str, Any]:
    """Compatibility default for plans created before the explicit contract."""
    return {
        "validity_gates": [
            {
                "gate_id": "required_metrics",
                "kind": "required_metrics_present",
            },
            {
                "gate_id": "parser_health",
                "kind": "parser_not_suspect",
            },
            {
                "gate_id": "independent_review",
                "kind":
                    "independent_code_plan_data_boundary_review_receipt_present",
            },
        ],
        "outcome_rules": [],
    }


def _positive_int(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ScientificContractError(f"{label} 须为正整数")
    return value


def normalize_scientific_contract(contract: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate and copy the closed Plan contract."""
    if not isinstance(contract, Mapping) or set(contract) != {
            "validity_gates", "outcome_rules"}:
        raise ScientificContractError(
            "scientific contract 字段须恰为 validity_gates/outcome_rules")
    raw_gates = contract.get("validity_gates")
    raw_rules = contract.get("outcome_rules")
    if (not isinstance(raw_gates, list) or not 1 <= len(raw_gates) <= 16
            or not isinstance(raw_rules, list) or len(raw_rules) > 16):
        raise ScientificContractError(
            "scientific contract gate/rule 数量非法")

    gates = []
    gate_ids = set()
    gate_kinds = set()
    for gate in raw_gates:
        if not isinstance(gate, Mapping) or set(gate) != {"gate_id", "kind"}:
            raise ScientificContractError("validity gate 字段闭包非法")
        gate_id = gate.get("gate_id")
        kind = gate.get("kind")
        if (not isinstance(gate_id, str)
                or _SAFE_ID.fullmatch(gate_id) is None
                or gate_id in gate_ids
                or kind not in _VALIDITY_GATES
                or kind in gate_kinds):
            raise ScientificContractError(
                "validity gate id/kind 非法或重复")
        gate_ids.add(gate_id)
        gate_kinds.add(kind)
        gates.append({"gate_id": gate_id, "kind": kind})
    missing = sorted(MANDATORY_VALIDITY_GATE_KINDS - gate_kinds)
    if missing:
        raise ScientificContractError(
            f"mandatory validity gate 缺失: {missing}")

    rules = []
    rule_ids = set()
    for rule in raw_rules:
        required = {
            "rule_id", "metric_id", "metric_ver", "operator",
            "threshold", "if_true", "if_false",
        }
        if not isinstance(rule, Mapping) or set(rule) != required:
            raise ScientificContractError("outcome rule 字段闭包非法")
        rule_id = rule.get("rule_id")
        threshold = rule.get("threshold")
        if (not isinstance(rule_id, str)
                or _SAFE_ID.fullmatch(rule_id) is None
                or rule_id in rule_ids
                or rule.get("operator") not in _OPERATORS
                or rule.get("if_true") not in _OUTCOMES
                or rule.get("if_false") not in _OUTCOMES
                or isinstance(threshold, bool)
                or not isinstance(threshold, (int, float))
                or not math.isfinite(float(threshold))):
            raise ScientificContractError(
                "outcome rule id/operator/outcome/threshold 非法或重复")
        rule_ids.add(rule_id)
        rules.append({
            "rule_id": rule_id,
            "metric_id": _positive_int(
                rule.get("metric_id"), label="outcome metric_id"),
            "metric_ver": _positive_int(
                rule.get("metric_ver"), label="outcome metric_ver"),
            "operator": rule["operator"],
            "threshold": float(threshold),
            "if_true": rule["if_true"],
            "if_false": rule["if_false"],
        })
    return {"validity_gates": gates, "outcome_rules": rules}


def resolve_plan_scientific_contract(
        contract: Mapping[str, Any], *,
        metric_ids: Mapping[str, int],
        declared_metrics: Iterable[tuple[str, int]],
        required_metrics: Iterable[tuple[int, int]],
        ) -> Dict[str, Any]:
    """Resolve Plan-local metric keys to frozen DB ids for one target.

    Outcome rules may only consume a metric version declared by the Plan and
    required by this exact target.  This makes the later Bundle classifier
    total over the target's pre-registered measurement contract instead of
    silently depending on an optional or unrelated metric.
    """
    if not isinstance(contract, Mapping):
        raise ScientificContractError("scientific contract 须为 object")
    declared = set(declared_metrics)
    required = _required_set(required_metrics)
    raw_rules = contract.get("outcome_rules")
    if not isinstance(raw_rules, list):
        raise ScientificContractError("outcome_rules 须为数组")
    resolved_rules = []
    for rule in raw_rules:
        if not isinstance(rule, Mapping):
            raise ScientificContractError("outcome rule 须为 object")
        metric_key = rule.get("metric_id")
        metric_ver = rule.get("metric_ver")
        if (not isinstance(metric_key, str) or metric_key not in metric_ids
                or (metric_key, metric_ver) not in declared):
            raise ScientificContractError(
                f"outcome metric {metric_key!r}@{metric_ver!r} 未由 Plan 声明")
        resolved_pair = (metric_ids[metric_key], metric_ver)
        if resolved_pair not in required:
            raise ScientificContractError(
                f"outcome metric {metric_key}@{metric_ver} 不在该 target required metrics")
        resolved_rules.append({**dict(rule), "metric_id": resolved_pair[0]})
    return normalize_scientific_contract({
        "validity_gates": contract.get("validity_gates"),
        "outcome_rules": resolved_rules,
    })


def _metric_index(
        metric_results: Sequence[Mapping[str, Any]],
        ) -> Dict[tuple[int, int], float]:
    if not isinstance(metric_results, Sequence) or isinstance(
            metric_results, (str, bytes, bytearray)):
        raise ScientificContractError("metric_results 须为数组")
    indexed: Dict[tuple[int, int], float] = {}
    for item in metric_results:
        if not isinstance(item, Mapping):
            raise ScientificContractError("metric_result 须为 object")
        if item.get("scope", "aggregate") != "aggregate":
            continue
        metric_id = _positive_int(
            item.get("metric_id"), label="metric_result.metric_id")
        metric_ver = _positive_int(
            item.get("metric_ver"), label="metric_result.metric_ver")
        value = item.get("value")
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(float(value))):
            raise ScientificContractError(
                "aggregate metric_result.value 须为有限数")
        key = (metric_id, metric_ver)
        if key in indexed:
            raise ScientificContractError(
                f"aggregate metric_result 重复: {metric_id}@{metric_ver}")
        indexed[key] = float(value)
    return indexed


def _required_set(values: Iterable[tuple[int, int]]) -> set[tuple[int, int]]:
    try:
        normalized = {
            (
                _positive_int(item[0], label="required metric_id"),
                _positive_int(item[1], label="required metric_ver"),
            )
            for item in values
        }
    except (TypeError, IndexError) as error:
        raise ScientificContractError(
            "required_metrics 须为 (metric_id,metric_ver) 集合") from error
    return normalized


def evaluate_scientific_contract(
        contract: Mapping[str, Any], *, execution_status: str,
        required_metrics: Iterable[tuple[int, int]],
        metric_results: Sequence[Mapping[str, Any]],
        parser_suspect: bool,
        independent_code_plan_data_boundary_review_receipt_present: bool,
        ) -> Dict[str, Any]:
    """Classify owner facts without allowing outcome to override validity."""
    normalized = normalize_scientific_contract(contract)
    if execution_status not in _EXECUTION:
        raise ScientificContractError("execution_status 非法")
    if type(parser_suspect) is not bool:
        raise ScientificContractError("parser_suspect 须为 bool")
    if type(
            independent_code_plan_data_boundary_review_receipt_present
            ) is not bool:
        raise ScientificContractError(
            "independent code/plan/data-boundary review receipt fact 须为 bool")
    metrics = _metric_index(metric_results)
    required = _required_set(required_metrics)
    if execution_status != "succeeded":
        return {
            "execution_status": execution_status,
            "validity_status": "not_assessed",
            "scientific_outcome": "unavailable",
            "pool_eligibility": "ineligible",
            "gate_results": [],
            "failed_gate_ids": [],
            "outcome_rule_results": [],
        }

    gate_results = []
    for gate in normalized["validity_gates"]:
        if gate["kind"] == "required_metrics_present":
            passed = required.issubset(metrics)
        elif gate["kind"] == "parser_not_suspect":
            passed = not parser_suspect
        elif gate["kind"] == (
                "independent_code_plan_data_boundary_review_receipt_present"):
            passed = (
                independent_code_plan_data_boundary_review_receipt_present)
        else:  # normalize_scientific_contract already rejects this.
            raise ScientificContractError(
                f"未知 validity gate: {gate['kind']}")
        gate_results.append({
            "gate_id": gate["gate_id"],
            "kind": gate["kind"],
            "passed": passed,
        })
    failed = [
        item["gate_id"] for item in gate_results if not item["passed"]]
    if failed:
        return {
            "execution_status": "succeeded",
            "validity_status": "invalid",
            "scientific_outcome": "unavailable",
            "pool_eligibility": "ineligible",
            "gate_results": gate_results,
            "failed_gate_ids": failed,
            "outcome_rule_results": [],
        }

    rule_results = []
    classified = []
    for rule in normalized["outcome_rules"]:
        key = (rule["metric_id"], rule["metric_ver"])
        value = metrics.get(key)
        if value is None:
            outcome = "inconclusive"
            matched = None
        else:
            matched = bool(_OPERATORS[rule["operator"]](
                value, rule["threshold"]))
            outcome = rule["if_true"] if matched else rule["if_false"]
        classified.append(outcome)
        rule_results.append({
            "rule_id": rule["rule_id"],
            "metric_id": rule["metric_id"],
            "metric_ver": rule["metric_ver"],
            "value": value,
            "matched": matched,
            "outcome": outcome,
        })
    outcome = (
        classified[0]
        if classified and len(set(classified)) == 1
        else "inconclusive")
    return {
        "execution_status": "succeeded",
        "validity_status": "valid",
        "scientific_outcome": outcome,
        "pool_eligibility": "eligible",
        "gate_results": gate_results,
        "failed_gate_ids": [],
        "outcome_rule_results": rule_results,
    }


def _canonical_json(value: Any, *, label: str) -> str:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ScientificContractError(
            f"{label} 不是可 canonicalize 的有限 JSON") from error


def canonical_hash(value: Any) -> str:
    """Return the bare sha256 of finite canonical JSON."""
    return hashlib.sha256(
        _canonical_json(value, label="scientific value").encode("utf-8")
    ).hexdigest()


def _json_copy(value: Any, *, label: str) -> Any:
    return json.loads(_canonical_json(value, label=label))


def normalize_independent_review_receipt(
        receipt: Mapping[str, Any]) -> Dict[str, Any]:
    """Normalize a verified review reference without claiming runtime proof.

    The receipt says only that an independent code/Plan/data-boundary review
    was completed and durably bound to the reviewed subject.  It deliberately
    does not assert that runtime instrumentation proved absence of leakage.
    """
    required = {
        "protocol", "decision_id", "review_kind", "review_scope",
        "subject_hash", "receipt_hash",
    }
    if not isinstance(receipt, Mapping) or set(receipt) != required:
        raise ScientificContractError(
            "independent review receipt 字段闭包非法")
    protocol = receipt.get("protocol")
    decision_id = receipt.get("decision_id")
    if not isinstance(protocol, str) or not protocol:
        raise ScientificContractError(
            "independent review receipt protocol 非法")
    if (isinstance(decision_id, bool) or not isinstance(decision_id, int)
            or decision_id <= 0):
        raise ScientificContractError(
            "independent review receipt decision_id 非法")
    if (receipt.get("review_kind") != "bundle_code"
            or receipt.get("review_scope") != "code_plan_data_boundary"):
        raise ScientificContractError(
            "independent review receipt kind/scope 非法")
    for key in ("subject_hash", "receipt_hash"):
        if (_SHA256.fullmatch(str(receipt.get(key) or "")) is None):
            raise ScientificContractError(
                f"independent review receipt {key} 非法")
    return {
        "protocol": protocol,
        "decision_id": decision_id,
        "review_kind": "bundle_code",
        "review_scope": "code_plan_data_boundary",
        "subject_hash": receipt["subject_hash"],
        "receipt_hash": receipt["receipt_hash"],
    }


def _canonical_metric_results(
        metric_results: Sequence[Mapping[str, Any]],
        ) -> list[Dict[str, Any]]:
    if not isinstance(metric_results, Sequence) or isinstance(
            metric_results, (str, bytes, bytearray)):
        raise ScientificContractError("metric_results 须为数组")
    normalized = []
    seen = set()
    for item in metric_results:
        if not isinstance(item, Mapping):
            raise ScientificContractError("metric_result 须为 object")
        allowed = {
            "metric_id", "metric_ver", "value", "scope", "checkpoint_id"}
        if not {"metric_id", "metric_ver", "value"}.issubset(item):
            raise ScientificContractError("metric_result 缺必要字段")
        if set(item) - allowed:
            raise ScientificContractError("metric_result 含未知字段")
        metric_id = _positive_int(
            item.get("metric_id"), label="metric_result.metric_id")
        metric_ver = _positive_int(
            item.get("metric_ver"), label="metric_result.metric_ver")
        value = item.get("value")
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(float(value))):
            raise ScientificContractError(
                "metric_result.value 须为有限数")
        scope = item.get("scope", "aggregate")
        checkpoint_id = item.get("checkpoint_id")
        if scope not in {"aggregate", "fold"}:
            raise ScientificContractError("metric_result.scope 非法")
        if scope == "aggregate":
            if checkpoint_id is not None:
                raise ScientificContractError(
                    "aggregate metric_result 不得绑定 checkpoint")
        else:
            checkpoint_id = _positive_int(
                checkpoint_id, label="fold metric_result.checkpoint_id")
        identity = (metric_id, metric_ver, scope, checkpoint_id)
        if identity in seen:
            raise ScientificContractError(
                "metric_result canonical identity 重复")
        seen.add(identity)
        result = {
            "metric_id": metric_id,
            "metric_ver": metric_ver,
            "value": float(value),
            "scope": scope,
        }
        if checkpoint_id is not None:
            result["checkpoint_id"] = checkpoint_id
        normalized.append(result)
    return sorted(normalized, key=lambda item: (
        item["metric_id"], item["metric_ver"], item["scope"],
        item.get("checkpoint_id") or 0,
    ))


def canonical_scientific_facts(
        *, evaluation_id: int, evaluation_attempt_id: int,
        required_metrics: Iterable[tuple[int, int]],
        metric_results: Sequence[Mapping[str, Any]],
        eval_log_hash: str, parser: Mapping[str, Any],
        independent_review_receipt: Mapping[str, Any] | None,
        ) -> Dict[str, Any]:
    """Build the closed owner-facts object hashed by a science decision."""
    evaluation_id = _positive_int(
        evaluation_id, label="evaluation_id")
    evaluation_attempt_id = _positive_int(
        evaluation_attempt_id, label="evaluation_attempt_id")
    if _SHA256.fullmatch(str(eval_log_hash or "")) is None:
        raise ScientificContractError("eval_log_hash 非 bare sha256")
    parser_keys = {"version", "policy_hash", "fields", "suspect"}
    if not isinstance(parser, Mapping) or set(parser) != parser_keys:
        raise ScientificContractError("parser facts 字段闭包非法")
    if not isinstance(parser.get("version"), str) or not parser["version"]:
        raise ScientificContractError("parser version 非法")
    if _SHA256.fullmatch(str(parser.get("policy_hash") or "")) is None:
        raise ScientificContractError("parser policy_hash 非法")
    if not isinstance(parser.get("fields"), Mapping):
        raise ScientificContractError("parser fields 须为 object")
    if type(parser.get("suspect")) is not bool:
        raise ScientificContractError("parser suspect 须为 bool")

    required = sorted(_required_set(required_metrics))
    review = (
        None if independent_review_receipt is None
        else normalize_independent_review_receipt(
            independent_review_receipt))
    return {
        "evaluation_id": evaluation_id,
        "evaluation_attempt_id": evaluation_attempt_id,
        "eval_log_hash": eval_log_hash,
        "required_metrics": [
            {"metric_id": metric_id, "metric_ver": metric_ver}
            for metric_id, metric_ver in required
        ],
        "metric_results": _canonical_metric_results(metric_results),
        "parser": {
            "version": parser["version"],
            "policy_hash": parser["policy_hash"],
            "fields": _json_copy(
                parser["fields"], label="parser fields"),
            "suspect": parser["suspect"],
        },
        "independent_review_receipt": review,
    }


def build_scientific_decision_payload(
        *, build_target_id: int, evaluation_id: int,
        evaluation_attempt_id: int, contract: Mapping[str, Any],
        execution_status: str,
        required_metrics: Iterable[tuple[int, int]],
        metric_results: Sequence[Mapping[str, Any]],
        eval_log_hash: str, parser: Mapping[str, Any],
        independent_review_receipt: Mapping[str, Any] | None,
        ) -> Dict[str, Any]:
    """Build one replayable decision with its full canonical inputs."""
    build_target_id = _positive_int(
        build_target_id, label="build_target_id")
    normalized_contract = normalize_scientific_contract(contract)
    facts = canonical_scientific_facts(
        evaluation_id=evaluation_id,
        evaluation_attempt_id=evaluation_attempt_id,
        required_metrics=required_metrics,
        metric_results=metric_results,
        eval_log_hash=eval_log_hash,
        parser=parser,
        independent_review_receipt=independent_review_receipt,
    )
    classification = evaluate_scientific_contract(
        normalized_contract,
        execution_status=execution_status,
        required_metrics={
            (item["metric_id"], item["metric_ver"])
            for item in facts["required_metrics"]
        },
        metric_results=facts["metric_results"],
        parser_suspect=facts["parser"]["suspect"],
        independent_code_plan_data_boundary_review_receipt_present=(
            facts["independent_review_receipt"] is not None),
    )
    return {
        "protocol": "bundle-scientific-contract-v1",
        "build_target_id": build_target_id,
        "evaluation_id": facts["evaluation_id"],
        "evaluation_attempt_id": facts["evaluation_attempt_id"],
        "contract": normalized_contract,
        "contract_hash": canonical_hash(normalized_contract),
        "facts": facts,
        "facts_hash": canonical_hash(facts),
        **classification,
    }


def validate_scientific_decision_payload(
        payload: Mapping[str, Any], *,
        expected_build_target_id: int | None = None,
        expected_evaluation_id: int | None = None,
        expected_attempt_id: int | None = None,
        ) -> Dict[str, Any]:
    """Recompute a persisted science decision and reject any drift.

    This validates one payload only.  Callers that load decisions from a store
    remain responsible for requiring exactly one row in the expected scope.
    """
    required_payload_fields = {
        "protocol", "build_target_id", "evaluation_id",
        "evaluation_attempt_id", "contract", "contract_hash",
        "facts", "facts_hash", "execution_status", "validity_status",
        "scientific_outcome", "pool_eligibility", "gate_results",
        "failed_gate_ids", "outcome_rule_results",
    }
    if not isinstance(payload, Mapping) or set(payload) != required_payload_fields:
        raise ScientificContractError(
            "scientific decision payload 字段闭包非法")
    if payload.get("protocol") != "bundle-scientific-contract-v1":
        raise ScientificContractError(
            "scientific decision payload protocol 非法")

    build_target_id = _positive_int(
        payload.get("build_target_id"), label="build_target_id")
    evaluation_id = _positive_int(
        payload.get("evaluation_id"), label="evaluation_id")
    attempt_id = _positive_int(
        payload.get("evaluation_attempt_id"),
        label="evaluation_attempt_id")
    expected = (
        ("build_target_id", expected_build_target_id, build_target_id),
        ("evaluation_id", expected_evaluation_id, evaluation_id),
        ("attempt_id", expected_attempt_id, attempt_id),
    )
    for label, wanted, actual in expected:
        if wanted is None:
            continue
        wanted = _positive_int(wanted, label=f"expected_{label}")
        if actual != wanted:
            raise ScientificContractError(
                f"scientific decision scope {label} 不匹配: "
                f"{actual} != {wanted}")

    contract = payload.get("contract")
    facts = payload.get("facts")
    if not isinstance(contract, Mapping) or not isinstance(facts, Mapping):
        raise ScientificContractError(
            "scientific decision contract/facts 须为 object")
    for hash_field, value in (
            ("contract_hash", contract), ("facts_hash", facts)):
        supplied = payload.get(hash_field)
        if (_SHA256.fullmatch(str(supplied or "")) is None
                or supplied != canonical_hash(value)):
            raise ScientificContractError(
                f"scientific decision {hash_field} 不可复验")

    required_fact_fields = {
        "evaluation_id", "evaluation_attempt_id", "eval_log_hash",
        "required_metrics", "metric_results", "parser",
        "independent_review_receipt",
    }
    if set(facts) != required_fact_fields:
        raise ScientificContractError(
            "scientific decision facts 字段闭包非法")
    raw_required = facts.get("required_metrics")
    if not isinstance(raw_required, list):
        raise ScientificContractError(
            "scientific decision required_metrics 须为数组")
    required_pairs = []
    for item in raw_required:
        if (not isinstance(item, Mapping)
                or set(item) != {"metric_id", "metric_ver"}):
            raise ScientificContractError(
                "scientific decision required metric 字段闭包非法")
        required_pairs.append((
            _positive_int(
                item.get("metric_id"), label="required metric_id"),
            _positive_int(
                item.get("metric_ver"), label="required metric_ver"),
        ))
    if len(required_pairs) != len(set(required_pairs)):
        raise ScientificContractError(
            "scientific decision required metric 重复")

    try:
        rebuilt = build_scientific_decision_payload(
            build_target_id=build_target_id,
            evaluation_id=evaluation_id,
            evaluation_attempt_id=attempt_id,
            contract=contract,
            execution_status=payload.get("execution_status"),
            required_metrics=required_pairs,
            metric_results=facts.get("metric_results"),
            eval_log_hash=facts.get("eval_log_hash"),
            parser=facts.get("parser"),
            independent_review_receipt=facts.get(
                "independent_review_receipt"),
        )
    except ScientificContractError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise ScientificContractError(
            "scientific decision payload 无法确定性复算") from error
    if (_canonical_json(payload, label="scientific decision payload")
            != _canonical_json(
                rebuilt, label="rebuilt scientific decision payload")):
        raise ScientificContractError(
            "scientific decision payload 与 contract/facts 复算结果冲突")
    return rebuilt
