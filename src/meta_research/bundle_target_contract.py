"""Formal Bundle Target candidates derived from the fixed Bundle prototype.

This module is deliberately pure.  It freezes the Plan semantics, parses the
complete prototype TargetCandidate, and validates append-only rolling strategy
updates.  Execution choices belong to the TargetRun after review/preflight and
are deliberately absent from the Bundle planner contract.  Receipt values are
structurally checked here; the calling Owner must still verify issuer,
signature, subject content and currentness against live Owner state.

``meta-research/target-plan/v2`` is a legacy/minimal product seam.  It is not a
formal completion candidate and is intentionally rejected by the parsers in
this module.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from dataclasses import dataclass
from typing import Mapping, cast

from meta_research.bundle_protocol import (
    BUNDLE_CANONICAL_INTEGER_MAX_ABS,
    BUNDLE_PROJECTION_MAX_TUPLE_ITEMS,
    BUNDLE_PROJECTION_STRING_MAX_UTF8_BYTES,
    BUNDLE_ROOT_MAX_NODES,
    BUNDLE_ROOT_MAX_SERIALIZED_BYTES,
    GREENFIELD_EXCEPTIONS,
    REUSE_TIER_ORDER,
    REUSE_TIERS,
    BundleProtocolError,
    ContentBindingProof,
    ExperimentBrief,
    HeldFixedBinding,
    ReceiptProof,
    ReuseSourceProof,
    ReuseTierDecision,
    ReuseTrace,
    RouteSpec,
    StrategyUpdate,
    TargetCandidate,
    projection_plain_value,
    validate_closed_bundle_projection,
    validate_receipt_proof,
)
from meta_research.plan_contract import PLAN_DOCUMENT_SCHEMA_REF


NORMALIZED_COMPLETION_CONTRACT_SCHEMA_REF = (
    "meta-research/bundle-normalized-completion-contract/v1"
)
FORMAL_TARGET_CANDIDATE_SCHEMA_REF = (
    "meta-research/bundle-formal-target-candidate/v2"
)
FORMAL_STRATEGY_UPDATE_SCHEMA_REF = (
    "meta-research/bundle-formal-strategy-update/v2"
)
ROLLING_STRATEGY_STATE_SCHEMA_REF = (
    "meta-research/bundle-rolling-strategy-state/v2"
)
MEASUREMENT_CONTRACT_CANDIDATE_SCHEMA_REF = (
    "meta-research/target-measurement-contract-candidate/v1"
)
PROTOCOL_VERSION_CANDIDATE_SCHEMA_REF = (
    "meta-research/target-protocol-version-candidate/v1"
)
LEGACY_TARGET_PLAN_SCHEMA_REF = "meta-research/target-plan/v2"

_BUNDLE_ROOT_MAX_DEPTH = 64
_METRIC_DEFINITION_MAX_ITEMS = 64
_DOMAIN_ROUTING_FIELDS = frozenset(
    {
        "adapter",
        "adapter_kind",
        "argv",
        "command",
        "container_image",
        "entrypoint",
        "execution",
        "execution_payload",
        "image",
        "provider",
        "provider_registry",
        "runtime_binding",
    }
)

_OWNER_ELIGIBLE_REUSE_TIERS = frozenset(
    {"accepted-local", "related-history", "global-baseline-pool"}
)
_REUSE_DISPOSITIONS = frozenset(
    {"selected", "rejected", "not_found", "not_applicable"}
)
_PLAN_BRIEF_FIELDS = frozenset(
    {
        "experiment_key",
        "gap_obligation_keys",
        "goal",
        "characteristics",
        "boundary_constraints",
        "semantic_delta",
        "contributing_idea_refs",
    }
)
_TARGET_CANDIDATE_FIELDS = frozenset(
    {
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
    }
)


class BundleTargetContractError(BundleProtocolError):
    """The formal Target candidate or its completion contract is incomplete."""


@dataclass(frozen=True, slots=True)
class FrozenJsonObject:
    """Deeply immutable JSON object represented by canonical UTF-8 JSON."""

    canonical_json: str

    def as_dict(self) -> dict[str, object]:
        value = json.loads(self.canonical_json)
        if type(value) is not dict:  # pragma: no cover - constructor invariant
            raise BundleTargetContractError("frozen_json_object_invalid")
        return cast(dict[str, object], value)


@dataclass(frozen=True, slots=True)
class FrozenSemanticInputs:
    """Exact inputs from one accepted production ExperimentBrief."""

    experiment_key: str
    goal: str
    characteristics: str
    boundary_constraints: str
    semantic_delta: str


@dataclass(frozen=True, slots=True)
class NormalizedExperimentCompletion:
    semantic_inputs: FrozenSemanticInputs
    brief: ExperimentBrief


@dataclass(frozen=True, slots=True)
class NormalizedCompletionContract:
    plan_document_hash: str
    experiments: tuple[NormalizedExperimentCompletion, ...]
    schema_ref: str = NORMALIZED_COMPLETION_CONTRACT_SCHEMA_REF


@dataclass(frozen=True, slots=True)
class FrozenAggregationRule:
    """Exact, provider-neutral aggregation rule selected for Protocol parts."""

    rule_ref: str
    rule: FrozenJsonObject


@dataclass(frozen=True, slots=True)
class FrozenStopRule:
    """Exact preregistered stop rule; observations and receipts come later."""

    rule_ref: str
    rule: FrozenJsonObject


@dataclass(frozen=True, slots=True)
class FrozenMetricDefinition:
    """One ordered Metric identity plus its complete frozen domain meaning."""

    metric_key: str
    definition: FrozenJsonObject


@dataclass(frozen=True, slots=True)
class FrozenProtocolVersionCandidate:
    """Complete Plan-bound ProtocolVersion content, before Owner acceptance."""

    evaluation_data: FrozenJsonObject
    split: FrozenJsonObject
    preprocessing: FrozenJsonObject
    required_metrics: tuple[FrozenMetricDefinition, ...]
    optional_metrics: tuple[FrozenMetricDefinition, ...]
    internal_part_keys: tuple[str, ...]
    aggregation: FrozenAggregationRule | None
    preregistered_stop_rules: tuple[FrozenStopRule, ...]
    schema_ref: str = PROTOCOL_VERSION_CANDIDATE_SCHEMA_REF

    @property
    def required_metric_keys(self) -> tuple[str, ...]:
        return tuple(metric.metric_key for metric in self.required_metrics)

    @property
    def optional_metric_keys(self) -> tuple[str, ...]:
        return tuple(metric.metric_key for metric in self.optional_metrics)


@dataclass(frozen=True, slots=True)
class TargetMeasurementContractCandidate:
    """Pure domain contract later projected into native RG measurement roles."""

    experiment_keys: tuple[str, ...]
    measurement_unit_key: str
    baseline_forward_contract: FrozenJsonObject
    variant_recipe: FrozenJsonObject
    evaluation_protocol_lineage: FrozenJsonObject
    protocol_version: FrozenProtocolVersionCandidate
    checkpoint_policy: str
    result_schema_ref: str
    result_schema: FrozenJsonObject
    schema_ref: str = MEASUREMENT_CONTRACT_CANDIDATE_SCHEMA_REF


@dataclass(frozen=True, slots=True)
class FormalTargetCandidate:
    """Production wrapper around the unchanged fixed prototype candidate."""

    candidate: TargetCandidate
    semantic_inputs: tuple[FrozenSemanticInputs, ...]
    measurement_contract: TargetMeasurementContractCandidate
    # Production admission seam retained outside the fixed TargetCandidate.
    # Missing/unknown risk must fail closed; it is never defaulted to normal.
    risk_class: str
    schema_ref: str = FORMAL_TARGET_CANDIDATE_SCHEMA_REF


@dataclass(frozen=True, slots=True)
class FormalStrategyUpdate:
    """Complete candidates plus the prototype's canonical StrategyUpdate."""

    update: StrategyUpdate
    candidates: tuple[FormalTargetCandidate, ...]
    schema_ref: str = FORMAL_STRATEGY_UPDATE_SCHEMA_REF


@dataclass(frozen=True, slots=True)
class RollingStrategyState:
    completion_contract_hash: str
    strategy: StrategyUpdate
    candidates: tuple[FormalTargetCandidate, ...]
    schema_ref: str = ROLLING_STRATEGY_STATE_SCHEMA_REF


@dataclass(frozen=True, slots=True)
class LegacyV2TargetSpec:
    """Read-only v2 payload.  This type has no formal-complete conversion."""

    payload: FrozenJsonObject


def build_normalized_completion_contract(
    plan_document: dict[str, object],
    normalization_inputs: tuple[dict[str, object], ...],
) -> NormalizedCompletionContract:
    """Freeze all Plan briefs; a caller cannot select only the convenient gaps.

    Each normalization input has exactly ``experiment_key``,
    ``held_fixed_slots`` and ``required_measurement_unit_keys``.  Plan semantics
    are copied verbatim.  Each formal Target wrapper later freezes a complete
    pure-domain measurement-contract candidate.  Research Graph graph/append
    acceptance later turns that exact content into formal native identity and
    current proof; TargetRun consumes those accepted facts.  Runtime routing
    and execution alone remain later TargetRun/generic-port responsibilities.
    """

    plan_hash, plan_briefs = _plan_briefs(plan_document)
    if type(normalization_inputs) is not tuple:
        raise BundleTargetContractError("completion_normalization_not_canonical")
    by_key: dict[str, dict[str, object]] = {}
    for value in normalization_inputs:
        item = _exact_dict(
            value,
            {
                "experiment_key",
                "held_fixed_slots",
                "required_measurement_unit_keys",
            },
            "completion_normalization_invalid",
        )
        key = _ref(item["experiment_key"], "ExperimentKey")
        if key in by_key:
            raise BundleTargetContractError("completion_normalization_duplicate")
        by_key[key] = item
    if set(by_key) != set(plan_briefs):
        raise BundleTargetContractError("completion_contract_experiment_set_incomplete")

    experiments: list[NormalizedExperimentCompletion] = []
    for key, plan_brief in plan_briefs.items():
        normalized = by_key[key]
        slots = _string_tuple(
            normalized["held_fixed_slots"],
            "held_fixed_slots",
            allow_empty=True,
        )
        cells = _string_tuple(
            normalized["required_measurement_unit_keys"],
            "required_measurement_unit_keys",
        )
        semantic = FrozenSemanticInputs(
            experiment_key=key,
            goal=_text(plan_brief["goal"], "Goal"),
            characteristics=_text(
                plan_brief["characteristics"], "Characteristics"
            ),
            boundary_constraints=_text(
                plan_brief["boundary_constraints"], "BoundaryConstraints"
            ),
            semantic_delta=_text(
                plan_brief["semantic_delta"], "SemanticDelta"
            ),
        )
        experiments.append(
            NormalizedExperimentCompletion(
                semantic_inputs=semantic,
                brief=ExperimentBrief(
                    experiment_key=key,
                    semantic_delta=semantic.semantic_delta,
                    held_fixed_slots=slots,
                    required_measurement_unit_keys=cells,
                ),
            )
        )
    contract = NormalizedCompletionContract(
        plan_document_hash=plan_hash,
        experiments=tuple(experiments),
    )
    _validate_completion_contract(contract, plan_document)
    return contract


def normalized_completion_contract_to_dict(
    contract: NormalizedCompletionContract,
) -> dict[str, object]:
    value = {
        "schema_ref": contract.schema_ref,
        "plan_document_hash": contract.plan_document_hash,
        "experiments": [
            {
                "semantic_inputs": _semantic_to_dict(item.semantic_inputs),
                "brief": projection_plain_value(item.brief),
            }
            for item in contract.experiments
        ],
    }
    _validate_json_root(value, "NormalizedCompletionContract")
    return value


def normalized_completion_contract_from_dict(
    value: object,
    *,
    plan_document: dict[str, object],
) -> NormalizedCompletionContract:
    document = _exact_dict(
        value,
        {"schema_ref", "plan_document_hash", "experiments"},
        "completion_contract_invalid",
    )
    if document["schema_ref"] != NORMALIZED_COMPLETION_CONTRACT_SCHEMA_REF:
        raise BundleTargetContractError("completion_contract_schema_invalid")
    rows = _object_list(document["experiments"], "completion_contract_invalid")
    experiments: list[NormalizedExperimentCompletion] = []
    for row_value in rows:
        row = _exact_dict(
            row_value,
            {"semantic_inputs", "brief"},
            "completion_contract_invalid",
        )
        experiments.append(
            NormalizedExperimentCompletion(
                semantic_inputs=_semantic_from_dict(row["semantic_inputs"]),
                brief=_brief_from_dict(row["brief"]),
            )
        )
    contract = NormalizedCompletionContract(
        plan_document_hash=_sha256(
            document["plan_document_hash"], "PlanDocument hash"
        ),
        experiments=tuple(experiments),
    )
    _validate_json_root(value, "NormalizedCompletionContract")
    _validate_completion_contract(contract, plan_document)
    return contract


def completion_contract_hash(contract: NormalizedCompletionContract) -> str:
    return _canonical_hash(
        normalized_completion_contract_to_dict(contract),
        "NormalizedCompletionContract",
    )


def measurement_contract_from_dict(
    value: object,
) -> TargetMeasurementContractCandidate:
    """Parse one complete, closed, provider-neutral measurement candidate."""

    document = _exact_dict(
        value,
        {
            "schema_ref",
            "experiment_keys",
            "measurement_unit_key",
            "baseline_forward_contract",
            "variant_recipe",
            "evaluation_protocol_lineage",
            "protocol_version",
            "checkpoint_policy",
            "result_schema_ref",
            "result_schema",
        },
        "measurement_contract_invalid",
    )
    if document["schema_ref"] != MEASUREMENT_CONTRACT_CANDIDATE_SCHEMA_REF:
        raise BundleTargetContractError("measurement_contract_schema_invalid")
    result = TargetMeasurementContractCandidate(
        experiment_keys=_string_tuple(
            document["experiment_keys"], "measurement contract ExperimentKeys"
        ),
        measurement_unit_key=_ref(
            document["measurement_unit_key"], "measurement contract cell"
        ),
        baseline_forward_contract=_domain_document(
            document["baseline_forward_contract"], "baseline forward contract"
        ),
        variant_recipe=_domain_document(
            document["variant_recipe"], "variant recipe"
        ),
        evaluation_protocol_lineage=_domain_document(
            document["evaluation_protocol_lineage"],
            "evaluation protocol lineage",
        ),
        protocol_version=_protocol_version_from_dict(document["protocol_version"]),
        checkpoint_policy=_checkpoint_policy(document["checkpoint_policy"]),
        result_schema_ref=_ref(
            document["result_schema_ref"], "measurement result schema"
        ),
        result_schema=_domain_document(
            document["result_schema"], "measurement result schema"
        ),
    )
    _validate_json_root(value, "TargetMeasurementContractCandidate")
    _validate_measurement_contract(result)
    return result


def measurement_contract_to_dict(
    contract: TargetMeasurementContractCandidate,
) -> dict[str, object]:
    _validate_measurement_contract(contract)
    value = _measurement_contract_to_unvalidated_dict(contract)
    _validate_json_root(value, "TargetMeasurementContractCandidate")
    return value


def _measurement_contract_to_unvalidated_dict(
    contract: TargetMeasurementContractCandidate,
) -> dict[str, object]:
    return {
        "schema_ref": contract.schema_ref,
        "experiment_keys": list(contract.experiment_keys),
        "measurement_unit_key": contract.measurement_unit_key,
        "baseline_forward_contract": contract.baseline_forward_contract.as_dict(),
        "variant_recipe": contract.variant_recipe.as_dict(),
        "evaluation_protocol_lineage": (
            contract.evaluation_protocol_lineage.as_dict()
        ),
        "protocol_version": _protocol_version_to_dict(contract.protocol_version),
        "checkpoint_policy": contract.checkpoint_policy,
        "result_schema_ref": contract.result_schema_ref,
        "result_schema": contract.result_schema.as_dict(),
    }


def measurement_contract_hash(
    contract: TargetMeasurementContractCandidate,
) -> str:
    return _canonical_hash(
        measurement_contract_to_dict(contract),
        "TargetMeasurementContractCandidate",
    )


def _protocol_version_from_dict(value: object) -> FrozenProtocolVersionCandidate:
    document = _exact_dict(
        value,
        {
            "schema_ref",
            "evaluation_data",
            "split",
            "preprocessing",
            "required_metrics",
            "optional_metrics",
            "internal_part_keys",
            "aggregation",
            "preregistered_stop_rules",
        },
        "protocol_version_candidate_invalid",
    )
    if document["schema_ref"] != PROTOCOL_VERSION_CANDIDATE_SCHEMA_REF:
        raise BundleTargetContractError("protocol_version_candidate_schema_invalid")
    aggregation_value = document["aggregation"]
    aggregation = (
        None
        if aggregation_value is None
        else _aggregation_rule_from_dict(aggregation_value)
    )
    protocol = FrozenProtocolVersionCandidate(
        evaluation_data=_domain_document(
            document["evaluation_data"], "protocol evaluation data"
        ),
        split=_domain_document(document["split"], "protocol split"),
        preprocessing=_domain_document(
            document["preprocessing"], "protocol preprocessing"
        ),
        required_metrics=tuple(
            _metric_definition_from_dict(item)
            for item in _object_list(
                document["required_metrics"], "required_metrics_invalid"
            )
        ),
        optional_metrics=tuple(
            _metric_definition_from_dict(item)
            for item in _object_list(
                document["optional_metrics"], "optional_metrics_invalid"
            )
        ),
        internal_part_keys=_string_tuple(
            document["internal_part_keys"],
            "protocol internal part keys",
            allow_empty=True,
        ),
        aggregation=aggregation,
        preregistered_stop_rules=tuple(
            _stop_rule_from_dict(item)
            for item in _object_list(
                document["preregistered_stop_rules"],
                "preregistered_stop_rules_invalid",
            )
        ),
    )
    _validate_protocol_version(protocol)
    return protocol


def _protocol_version_to_dict(
    protocol: FrozenProtocolVersionCandidate,
) -> dict[str, object]:
    return {
        "schema_ref": protocol.schema_ref,
        "evaluation_data": protocol.evaluation_data.as_dict(),
        "split": protocol.split.as_dict(),
        "preprocessing": protocol.preprocessing.as_dict(),
        "required_metrics": [
            {
                "metric_key": metric.metric_key,
                "definition": metric.definition.as_dict(),
            }
            for metric in protocol.required_metrics
        ],
        "optional_metrics": [
            {
                "metric_key": metric.metric_key,
                "definition": metric.definition.as_dict(),
            }
            for metric in protocol.optional_metrics
        ],
        "internal_part_keys": list(protocol.internal_part_keys),
        "aggregation": (
            None
            if protocol.aggregation is None
            else {
                "rule_ref": protocol.aggregation.rule_ref,
                "rule": protocol.aggregation.rule.as_dict(),
            }
        ),
        "preregistered_stop_rules": [
            {"rule_ref": rule.rule_ref, "rule": rule.rule.as_dict()}
            for rule in protocol.preregistered_stop_rules
        ],
    }


def _aggregation_rule_from_dict(value: object) -> FrozenAggregationRule:
    document = _exact_dict(
        value,
        {"rule_ref", "rule"},
        "protocol_aggregation_rule_invalid",
    )
    result = FrozenAggregationRule(
        rule_ref=_ref(document["rule_ref"], "protocol aggregation rule"),
        rule=_domain_document(document["rule"], "protocol aggregation rule"),
    )
    _validate_aggregation_rule(result)
    return result


def _metric_definition_from_dict(value: object) -> FrozenMetricDefinition:
    document = _exact_dict(
        value,
        {"metric_key", "definition"},
        "metric_definition_invalid",
    )
    return FrozenMetricDefinition(
        metric_key=_metric_key(document["metric_key"]),
        definition=_domain_document(
            document["definition"], "Metric definition"
        ),
    )


def _stop_rule_from_dict(value: object) -> FrozenStopRule:
    document = _exact_dict(
        value,
        {"rule_ref", "rule"},
        "preregistered_stop_rule_invalid",
    )
    return FrozenStopRule(
        rule_ref=_ref(document["rule_ref"], "preregistered stop rule"),
        rule=_domain_document(document["rule"], "preregistered stop rule"),
    )


def formal_target_candidate_from_dict(
    value: object,
    *,
    completion_contract: NormalizedCompletionContract,
) -> FormalTargetCandidate:
    document = _exact_dict(
        value,
        {
            "schema_ref",
            "candidate",
            "semantic_inputs",
            "measurement_contract",
            "risk_class",
        },
        "formal_target_candidate_invalid",
    )
    if document["schema_ref"] != FORMAL_TARGET_CANDIDATE_SCHEMA_REF:
        raise BundleTargetContractError("formal_target_candidate_schema_invalid")
    semantic_inputs = tuple(
        _semantic_from_dict(item)
        for item in _object_list(
            document["semantic_inputs"], "formal_target_candidate_invalid"
        )
    )
    result = FormalTargetCandidate(
        candidate=_candidate_from_dict(document["candidate"]),
        semantic_inputs=semantic_inputs,
        measurement_contract=measurement_contract_from_dict(
            document["measurement_contract"]
        ),
        risk_class=_risk_class(document["risk_class"]),
    )
    _validate_json_root(value, "FormalTargetCandidate")
    _validate_formal_candidate(result, completion_contract)
    return result


def formal_target_candidate_to_dict(
    candidate: FormalTargetCandidate,
    *,
    completion_contract: NormalizedCompletionContract,
) -> dict[str, object]:
    _validate_formal_candidate(candidate, completion_contract)
    value = {
        "schema_ref": candidate.schema_ref,
        "candidate": projection_plain_value(candidate.candidate),
        "semantic_inputs": [
            _semantic_to_dict(item) for item in candidate.semantic_inputs
        ],
        "measurement_contract": measurement_contract_to_dict(
            candidate.measurement_contract
        ),
        "risk_class": candidate.risk_class,
    }
    _validate_json_root(value, "FormalTargetCandidate")
    return value


def strategy_update_from_dict(
    value: object,
    *,
    completion_contract: NormalizedCompletionContract,
) -> FormalStrategyUpdate:
    document = _exact_dict(
        value,
        {
            "schema_ref",
            "revision",
            "candidates",
            "requires_accepted_labels",
            "strategy_complete",
        },
        "formal_strategy_update_invalid",
    )
    if document["schema_ref"] != FORMAL_STRATEGY_UPDATE_SCHEMA_REF:
        raise BundleTargetContractError("formal_strategy_update_schema_invalid")
    revision = _positive_int(document["revision"], "strategy revision")
    complete = _exact_bool(document["strategy_complete"], "strategy complete")
    candidates = tuple(
        formal_target_candidate_from_dict(
            item,
            completion_contract=completion_contract,
        )
        for item in _object_list(
            document["candidates"], "formal_strategy_update_invalid"
        )
    )
    required = _string_tuple(
        document["requires_accepted_labels"],
        "requires_accepted_labels",
        allow_empty=True,
    )
    update = StrategyUpdate(
        revision=revision,
        candidates=tuple(item.candidate for item in candidates),
        requires_accepted_labels=required,
        strategy_complete=complete,
    )
    validate_closed_bundle_projection(update, "StrategyUpdate")
    result = FormalStrategyUpdate(update=update, candidates=candidates)
    _validate_json_root(value, "FormalStrategyUpdate")
    return result


def strategy_update_to_dict(
    update: FormalStrategyUpdate,
    *,
    completion_contract: NormalizedCompletionContract,
) -> dict[str, object]:
    _validate_update_pair(update, completion_contract)
    value = {
        "schema_ref": update.schema_ref,
        "revision": update.update.revision,
        "candidates": [
            formal_target_candidate_to_dict(
                item, completion_contract=completion_contract
            )
            for item in update.candidates
        ],
        "requires_accepted_labels": list(update.update.requires_accepted_labels),
        "strategy_complete": update.update.strategy_complete,
    }
    _validate_json_root(value, "FormalStrategyUpdate")
    return value


def start_rolling_strategy(
    completion_contract: NormalizedCompletionContract,
) -> RollingStrategyState:
    return RollingStrategyState(
        completion_contract_hash=completion_contract_hash(completion_contract),
        strategy=StrategyUpdate(revision=0, candidates=()),
        candidates=(),
    )


def apply_strategy_update(
    state: RollingStrategyState,
    update: FormalStrategyUpdate,
    *,
    completion_contract: NormalizedCompletionContract,
    accepted_labels: frozenset[str] = frozenset(),
) -> RollingStrategyState:
    """Append one prototype rolling update; sealing runs the full cell gate."""

    _validate_state(state, completion_contract)
    _validate_update_pair(update, completion_contract)
    if state.strategy.strategy_complete:
        raise BundleTargetContractError("completed_strategy_is_immutable")
    if update.update.revision <= state.strategy.revision:
        raise BundleTargetContractError("strategy_revision_not_monotonic")
    if not update.candidates and not update.update.strategy_complete:
        raise BundleTargetContractError("strategy_update_has_no_actionable_work")
    known_labels = {item.candidate.local_label for item in state.candidates}
    if not set(accepted_labels) <= known_labels:
        raise BundleTargetContractError("accepted_strategy_label_unknown")
    if not set(update.update.requires_accepted_labels) <= set(accepted_labels):
        raise BundleTargetContractError("strategy_consumed_unaccepted_result")
    new_labels: set[str] = set()
    for item in update.candidates:
        label = item.candidate.local_label
        if label in known_labels or label in new_labels:
            raise BundleTargetContractError("strategy_repeats_target_label")
        if not set(update.update.requires_accepted_labels) <= set(
            item.candidate.depends_on_labels
        ):
            raise BundleTargetContractError("adaptive_input_missing_dependency")
        new_labels.add(label)
    merged = state.candidates + update.candidates
    _verify_candidate_set(merged, completion_contract)
    if update.update.strategy_complete:
        _verify_completion_cells(merged, completion_contract)
    result = RollingStrategyState(
        completion_contract_hash=state.completion_contract_hash,
        strategy=StrategyUpdate(
            revision=update.update.revision,
            candidates=tuple(item.candidate for item in merged),
            strategy_complete=update.update.strategy_complete,
        ),
        candidates=merged,
    )
    _validate_state(result, completion_contract)
    return result


def rolling_strategy_state_to_dict(
    state: RollingStrategyState,
    *,
    completion_contract: NormalizedCompletionContract,
) -> dict[str, object]:
    _validate_state(state, completion_contract)
    value = {
        "schema_ref": state.schema_ref,
        "completion_contract_hash": state.completion_contract_hash,
        "revision": state.strategy.revision,
        "candidates": [
            formal_target_candidate_to_dict(
                item, completion_contract=completion_contract
            )
            for item in state.candidates
        ],
        "strategy_complete": state.strategy.strategy_complete,
    }
    _validate_json_root(value, "RollingStrategyState")
    return value


def rolling_strategy_state_from_dict(
    value: object,
    *,
    completion_contract: NormalizedCompletionContract,
    previous_state: RollingStrategyState | None = None,
) -> RollingStrategyState:
    """Parse a durable snapshot, rejecting candidate removal or replacement."""

    document = _exact_dict(
        value,
        {
            "schema_ref",
            "completion_contract_hash",
            "revision",
            "candidates",
            "strategy_complete",
        },
        "rolling_strategy_state_invalid",
    )
    if document["schema_ref"] != ROLLING_STRATEGY_STATE_SCHEMA_REF:
        raise BundleTargetContractError("rolling_strategy_state_schema_invalid")
    candidates = tuple(
        formal_target_candidate_from_dict(
            item, completion_contract=completion_contract
        )
        for item in _object_list(
            document["candidates"], "rolling_strategy_state_invalid"
        )
    )
    result = RollingStrategyState(
        completion_contract_hash=_sha256(
            document["completion_contract_hash"], "completion contract hash"
        ),
        strategy=StrategyUpdate(
            revision=_nonnegative_int(document["revision"], "strategy revision"),
            candidates=tuple(item.candidate for item in candidates),
            strategy_complete=_exact_bool(
                document["strategy_complete"], "strategy complete"
            ),
        ),
        candidates=candidates,
    )
    _validate_json_root(value, "RollingStrategyState")
    _validate_state(result, completion_contract)
    if previous_state is not None:
        _validate_state(previous_state, completion_contract)
        if previous_state.strategy.strategy_complete:
            raise BundleTargetContractError("completed_strategy_is_immutable")
        if result.strategy.revision <= previous_state.strategy.revision:
            raise BundleTargetContractError("strategy_revision_not_monotonic")
        old = tuple(
            formal_target_candidate_to_dict(
                item, completion_contract=completion_contract
            )
            for item in previous_state.candidates
        )
        new_prefix = tuple(
            formal_target_candidate_to_dict(
                item, completion_contract=completion_contract
            )
            for item in result.candidates[: len(old)]
        )
        if len(result.candidates) < len(old) or new_prefix != old:
            raise BundleTargetContractError("rolling_candidate_list_shrank_or_changed")
    return result


def parse_legacy_v2_target_spec(value: object) -> LegacyV2TargetSpec:
    """Parse the old minimal TargetSpec for read compatibility only."""

    target = _exact_dict(
        value,
        {
            "target_key",
            "title",
            "experiment_key",
            "gap_obligation_keys",
            "depends_on",
            "goal",
            "characteristics",
            "boundary_constraints",
            "semantic_delta",
            "contributing_idea_refs",
            "risk_class",
            "execution",
        },
        "legacy_v2_target_spec_invalid",
    )
    for field in (
        "target_key",
        "title",
        "experiment_key",
        "goal",
        "characteristics",
        "boundary_constraints",
        "semantic_delta",
    ):
        _text(target[field], field)
    _string_tuple(target["gap_obligation_keys"], "gap_obligation_keys")
    _string_tuple(target["depends_on"], "depends_on", allow_empty=True)
    _string_tuple(
        target["contributing_idea_refs"],
        "contributing_idea_refs",
        allow_empty=True,
    )
    if target["risk_class"] not in {"normal", "high"}:
        raise BundleTargetContractError("legacy_v2_target_spec_invalid")
    _freeze_legacy_execution(target["execution"])
    _validate_json_root(value, "LegacyV2TargetSpec")
    return LegacyV2TargetSpec(payload=_freeze_object(target, "LegacyV2TargetSpec"))


# ---------------------------------------------------------------------------
# Contract validation


def _validate_completion_contract(
    contract: NormalizedCompletionContract,
    plan_document: dict[str, object],
) -> None:
    if contract.schema_ref != NORMALIZED_COMPLETION_CONTRACT_SCHEMA_REF:
        raise BundleTargetContractError("completion_contract_schema_invalid")
    plan_hash, plan_briefs = _plan_briefs(plan_document)
    if contract.plan_document_hash != plan_hash:
        raise BundleTargetContractError("completion_contract_plan_drift")
    by_key: dict[str, NormalizedExperimentCompletion] = {}
    for item in contract.experiments:
        key = item.semantic_inputs.experiment_key
        if key in by_key:
            raise BundleTargetContractError("completion_contract_experiment_duplicate")
        by_key[key] = item
        validate_closed_bundle_projection(item.brief, "ExperimentBrief")
        plan_brief = plan_briefs.get(key)
        if plan_brief is None:
            raise BundleTargetContractError("completion_contract_experiment_unknown")
        semantic = item.semantic_inputs
        if (
            semantic.goal != plan_brief["goal"]
            or semantic.characteristics != plan_brief["characteristics"]
            or semantic.boundary_constraints != plan_brief["boundary_constraints"]
            or semantic.semantic_delta != plan_brief["semantic_delta"]
            or item.brief.experiment_key != key
            or item.brief.semantic_delta != semantic.semantic_delta
        ):
            raise BundleTargetContractError("completion_contract_semantic_drift")
        _string_tuple(
            list(item.brief.held_fixed_slots),
            "held_fixed_slots",
            allow_empty=True,
        )
        _string_tuple(
            list(item.brief.required_measurement_unit_keys),
            "required_measurement_unit_keys",
        )
    if set(by_key) != set(plan_briefs):
        raise BundleTargetContractError("completion_contract_experiment_set_incomplete")
    _validate_json_root(
        normalized_completion_contract_to_dict(contract),
        "NormalizedCompletionContract",
    )


def _validate_formal_candidate(
    formal: FormalTargetCandidate,
    contract: NormalizedCompletionContract,
) -> None:
    if formal.schema_ref != FORMAL_TARGET_CANDIDATE_SCHEMA_REF:
        raise BundleTargetContractError("formal_target_candidate_schema_invalid")
    _risk_class(formal.risk_class)
    contract_by_key = {
        item.brief.experiment_key: item for item in contract.experiments
    }
    candidate = formal.candidate
    _verify_candidate(
        candidate,
        {key: item.brief for key, item in contract_by_key.items()},
    )
    _validate_measurement_contract(formal.measurement_contract)
    if (
        formal.measurement_contract.experiment_keys != candidate.experiment_keys
        or formal.measurement_contract.measurement_unit_key
        != candidate.measurement_unit_keys[0]
    ):
        raise BundleTargetContractError("candidate_measurement_contract_binding_drift")
    semantic_by_key: dict[str, FrozenSemanticInputs] = {}
    for semantic in formal.semantic_inputs:
        if semantic.experiment_key in semantic_by_key:
            raise BundleTargetContractError("candidate_semantic_input_duplicate")
        semantic_by_key[semantic.experiment_key] = semantic
    if set(semantic_by_key) != set(candidate.experiment_keys):
        raise BundleTargetContractError("candidate_semantic_input_set_invalid")
    for key in candidate.experiment_keys:
        expected = contract_by_key[key].semantic_inputs
        if semantic_by_key[key] != expected:
            raise BundleTargetContractError("candidate_semantic_input_drift")


def _validate_measurement_contract(
    contract: TargetMeasurementContractCandidate,
) -> None:
    if type(contract) is not TargetMeasurementContractCandidate:
        raise BundleTargetContractError("measurement_contract_invalid")
    if contract.schema_ref != MEASUREMENT_CONTRACT_CANDIDATE_SCHEMA_REF:
        raise BundleTargetContractError("measurement_contract_schema_invalid")
    _canonical_string_tuple(
        contract.experiment_keys,
        "measurement contract ExperimentKeys",
    )
    _ref(contract.measurement_unit_key, "measurement contract cell")
    for value, name in (
        (contract.baseline_forward_contract, "baseline forward contract"),
        (contract.variant_recipe, "variant recipe"),
        (contract.evaluation_protocol_lineage, "evaluation protocol lineage"),
    ):
        _validate_frozen_domain_document(value, name)
    _validate_protocol_version(contract.protocol_version)
    _checkpoint_policy(contract.checkpoint_policy)
    _ref(contract.result_schema_ref, "measurement result schema")
    _validate_frozen_domain_document(
        contract.result_schema,
        "measurement result schema",
    )
    _validate_json_root(
        _measurement_contract_to_unvalidated_dict(contract),
        "TargetMeasurementContractCandidate",
    )


def _validate_protocol_version(
    protocol: FrozenProtocolVersionCandidate,
) -> None:
    if type(protocol) is not FrozenProtocolVersionCandidate:
        raise BundleTargetContractError("protocol_version_candidate_invalid")
    if protocol.schema_ref != PROTOCOL_VERSION_CANDIDATE_SCHEMA_REF:
        raise BundleTargetContractError("protocol_version_candidate_schema_invalid")
    for value, name in (
        (protocol.evaluation_data, "protocol evaluation data"),
        (protocol.split, "protocol split"),
        (protocol.preprocessing, "protocol preprocessing"),
    ):
        _validate_frozen_domain_document(value, name)
    required = _validate_metric_definitions(
        protocol.required_metrics,
        "required_metrics",
        allow_empty=False,
    )
    optional = _validate_metric_definitions(
        protocol.optional_metrics,
        "optional_metrics",
        allow_empty=True,
    )
    if set(required) & set(optional):
        raise BundleTargetContractError("protocol_metric_sets_overlap")
    parts = _canonical_string_tuple(
        protocol.internal_part_keys,
        "protocol internal part keys",
        allow_empty=True,
    )
    if bool(parts) != (protocol.aggregation is not None):
        raise BundleTargetContractError("protocol_parts_aggregation_incomplete")
    if protocol.aggregation is not None:
        _validate_aggregation_rule(protocol.aggregation)
    stop_refs: set[str] = set()
    if (
        type(protocol.preregistered_stop_rules) is not tuple
        or len(protocol.preregistered_stop_rules)
        > BUNDLE_PROJECTION_MAX_TUPLE_ITEMS
    ):
        raise BundleTargetContractError("preregistered_stop_rules_invalid")
    for rule in protocol.preregistered_stop_rules:
        if type(rule) is not FrozenStopRule:
            raise BundleTargetContractError("preregistered_stop_rules_invalid")
        _ref(rule.rule_ref, "preregistered stop rule")
        if rule.rule_ref in stop_refs:
            raise BundleTargetContractError("preregistered_stop_rule_duplicate")
        stop_refs.add(rule.rule_ref)
        _validate_frozen_domain_document(rule.rule, "preregistered stop rule")


def _validate_aggregation_rule(rule: FrozenAggregationRule) -> None:
    if type(rule) is not FrozenAggregationRule:
        raise BundleTargetContractError("protocol_aggregation_rule_invalid")
    _ref(rule.rule_ref, "protocol aggregation rule")
    _validate_frozen_domain_document(rule.rule, "protocol aggregation rule")


def _validate_metric_definitions(
    metrics: tuple[FrozenMetricDefinition, ...],
    name: str,
    *,
    allow_empty: bool,
) -> tuple[str, ...]:
    if (
        type(metrics) is not tuple
        or (not metrics and not allow_empty)
        or len(metrics) > _METRIC_DEFINITION_MAX_ITEMS
    ):
        raise BundleTargetContractError(f"{name}_invalid")
    keys: list[str] = []
    for metric in metrics:
        if type(metric) is not FrozenMetricDefinition:
            raise BundleTargetContractError(f"{name}_invalid")
        keys.append(_metric_key(metric.metric_key))
        _validate_frozen_domain_document(metric.definition, "Metric definition")
    if len(keys) != len(set(keys)):
        raise BundleTargetContractError("protocol_metric_key_duplicate")
    return tuple(keys)


def _validate_update_pair(
    update: FormalStrategyUpdate,
    contract: NormalizedCompletionContract,
) -> None:
    if update.schema_ref != FORMAL_STRATEGY_UPDATE_SCHEMA_REF:
        raise BundleTargetContractError("formal_strategy_update_schema_invalid")
    _positive_int(update.update.revision, "strategy revision")
    _exact_bool(update.update.strategy_complete, "strategy complete")
    if update.update.candidates != tuple(item.candidate for item in update.candidates):
        raise BundleTargetContractError("strategy_update_candidate_projection_drift")
    _string_tuple(
        list(update.update.requires_accepted_labels),
        "requires_accepted_labels",
        allow_empty=True,
    )
    for item in update.candidates:
        _validate_formal_candidate(item, contract)
    validate_closed_bundle_projection(update.update, "StrategyUpdate")


def _validate_state(
    state: RollingStrategyState,
    contract: NormalizedCompletionContract,
) -> None:
    if state.schema_ref != ROLLING_STRATEGY_STATE_SCHEMA_REF:
        raise BundleTargetContractError("rolling_strategy_state_schema_invalid")
    if state.completion_contract_hash != completion_contract_hash(contract):
        raise BundleTargetContractError("rolling_strategy_completion_contract_drift")
    _nonnegative_int(state.strategy.revision, "strategy revision")
    _exact_bool(state.strategy.strategy_complete, "strategy complete")
    if state.strategy.candidates != tuple(item.candidate for item in state.candidates):
        raise BundleTargetContractError("rolling_strategy_candidate_projection_drift")
    if state.strategy.requires_accepted_labels:
        raise BundleTargetContractError("rolling_state_retained_session_gate")
    for item in state.candidates:
        _validate_formal_candidate(item, contract)
    _verify_candidate_set(state.candidates, contract)
    if state.strategy.strategy_complete:
        _verify_completion_cells(state.candidates, contract)


def _verify_candidate(
    candidate: TargetCandidate,
    briefs_by_key: Mapping[str, ExperimentBrief],
) -> dict[str, str]:
    """Production spelling of the fixed prototype's ``_verify_candidate``."""

    validate_closed_bundle_projection(candidate, "TargetCandidate")
    _exact_bool(candidate.code_changed, "code_changed")
    _ref(candidate.local_label, "Target local label")
    experiment_keys = _canonical_string_tuple(
        candidate.experiment_keys, "candidate ExperimentKeys"
    )
    if not set(experiment_keys) <= set(briefs_by_key):
        raise BundleTargetContractError("candidate_references_unknown_experiment")
    if len(candidate.measurement_unit_keys) != 1:
        raise BundleTargetContractError("candidate_must_cover_exactly_one_measurement_cell")
    unit = _ref(candidate.measurement_unit_keys[0], "measurement cell")
    dependencies = _canonical_string_tuple(
        candidate.depends_on_labels, "candidate dependencies", allow_empty=True
    )
    if candidate.local_label in dependencies:
        raise BundleTargetContractError("candidate_self_dependency")
    for key in experiment_keys:
        if unit not in briefs_by_key[key].required_measurement_unit_keys:
            raise BundleTargetContractError("candidate_measurement_cell_not_required")
    expected_slots = {
        slot
        for key in experiment_keys
        for slot in briefs_by_key[key].held_fixed_slots
    }
    bindings = _binding_map(candidate.held_fixed_bindings)
    if set(bindings) != expected_slots:
        raise BundleTargetContractError("candidate_held_fixed_binding_incomplete")
    implementation = _ref(
        candidate.implementation_revision_ref, "ImplementationRevisionRef"
    )
    _verify_reuse_trace(candidate.reuse_trace, implementation)
    route_refs = tuple(route.route_ref for route in candidate.routes)
    if not route_refs or len(route_refs) != len(set(route_refs)):
        raise BundleTargetContractError("candidate_routes_incomplete")
    for route in candidate.routes:
        _ref(route.route_ref, "semantic route")
        _canonical_string_tuple(
            route.known_external_operation_refs,
            "route external operations",
            allow_empty=True,
        )
    _canonical_string_tuple(
        candidate.direct_accepted_input_asset_refs,
        "direct accepted assets",
        allow_empty=True,
    )
    return bindings


def _verify_candidate_set(
    candidates: tuple[FormalTargetCandidate, ...],
    contract: NormalizedCompletionContract,
) -> None:
    by_label: dict[str, TargetCandidate] = {}
    used_units: dict[str, str] = {}
    held_by_experiment: dict[str, dict[str, str]] = {}
    briefs = {item.brief.experiment_key: item.brief for item in contract.experiments}
    for formal in candidates:
        candidate = formal.candidate
        if candidate.local_label in by_label:
            raise BundleTargetContractError("strategy_repeats_target_label")
        bindings = _verify_candidate(candidate, briefs)
        by_label[candidate.local_label] = candidate
        unit = candidate.measurement_unit_keys[0]
        previous = used_units.setdefault(unit, candidate.local_label)
        if previous != candidate.local_label:
            raise BundleTargetContractError("measurement_cell_appears_in_two_targets")
        for key in candidate.experiment_keys:
            established = held_by_experiment.setdefault(key, {})
            for slot in briefs[key].held_fixed_slots:
                previous_revision = established.setdefault(slot, bindings[slot])
                if previous_revision != bindings[slot]:
                    raise BundleTargetContractError("held_fixed_binding_drift")
    _verify_acyclic(by_label)


def _verify_completion_cells(
    candidates: tuple[FormalTargetCandidate, ...],
    contract: NormalizedCompletionContract,
) -> None:
    _verify_candidate_set(candidates, contract)
    counts: Counter[tuple[str, str]] = Counter()
    for formal in candidates:
        candidate = formal.candidate
        unit = candidate.measurement_unit_keys[0]
        for key in candidate.experiment_keys:
            counts[(key, unit)] += 1
    expected = {
        (item.brief.experiment_key, unit)
        for item in contract.experiments
        for unit in item.brief.required_measurement_unit_keys
    }
    if set(counts) != expected or any(counts[cell] != 1 for cell in expected):
        raise BundleTargetContractError("completed_strategy_cell_coverage_invalid")


def _verify_acyclic(candidates: Mapping[str, TargetCandidate]) -> None:
    labels = set(candidates)
    for candidate in candidates.values():
        if not set(candidate.depends_on_labels) <= labels:
            raise BundleTargetContractError("strategy_dependency_unknown")
    reachable: set[str] = set()
    while True:
        added = {
            candidate.local_label
            for candidate in candidates.values()
            if set(candidate.depends_on_labels) <= reachable
        } - reachable
        if not added:
            break
        reachable.update(added)
    if reachable != labels:
        raise BundleTargetContractError("strategy_dependency_cycle")


def _verify_reuse_trace(trace: ReuseTrace, implementation_ref: str) -> None:
    validate_closed_bundle_projection(trace, "ReuseTrace")
    if not trace.tier_decisions:
        raise BundleTargetContractError("reuse_trace_missing")
    tiers = tuple(item.tier for item in trace.tier_decisions)
    if not set(tiers) <= REUSE_TIERS or len(tiers) != len(set(tiers)):
        raise BundleTargetContractError("reuse_tier_set_invalid")
    receipt_subjects: dict[str, str] = {}
    content_by_subject: dict[str, str] = {}
    for decision in trace.tier_decisions:
        if decision.disposition not in _REUSE_DISPOSITIONS:
            raise BundleTargetContractError("reuse_disposition_invalid")
        _ref(decision.reason_ref, "reuse reason")
        if decision.disposition in {"not_found", "not_applicable"} and (
            decision.source_proofs
        ):
            raise BundleTargetContractError("reuse_absence_has_source")
        if len(decision.source_proofs) != len(set(decision.source_proofs)):
            raise BundleTargetContractError("reuse_source_proof_duplicate")
        for source in decision.source_proofs:
            _verify_reuse_source(
                source,
                decision=decision,
                implementation_ref=implementation_ref,
                receipt_subjects=receipt_subjects,
                content_by_subject=content_by_subject,
            )
    selected = tuple(
        item for item in trace.tier_decisions if item.disposition == "selected"
    )
    if len(selected) != 1 or not selected[0].source_proofs:
        raise BundleTargetContractError("reuse_selected_source_missing")
    selected_tier = selected[0].tier
    if trace.greenfield_exception is not None:
        if (
            selected_tier != "self-implementation"
            or trace.greenfield_exception not in GREENFIELD_EXCEPTIONS
        ):
            raise BundleTargetContractError("reuse_greenfield_exception_invalid")
    else:
        prior = set(REUSE_TIER_ORDER[: REUSE_TIER_ORDER.index(selected_tier)])
        if not prior <= set(tiers):
            raise BundleTargetContractError("reuse_nearer_tier_skipped")


def _verify_reuse_source(
    source: ReuseSourceProof,
    *,
    decision: ReuseTierDecision,
    implementation_ref: str,
    receipt_subjects: dict[str, str],
    content_by_subject: dict[str, str],
) -> None:
    _ref(source.source_ref, "reuse source")
    _ref(source.exact_version_ref, "reuse exact version")
    _ref(source.implementation_revision_ref, "reuse implementation revision")
    if source.eligible_tier != decision.tier:
        raise BundleTargetContractError("reuse_source_tier_drift")
    if source.implementation_binding.subject_ref != source.implementation_revision_ref:
        raise BundleTargetContractError("reuse_implementation_binding_drift")
    _sha256(
        source.implementation_binding.content_hash_ref,
        "reuse implementation content hash",
    )
    _receipt(
        source.verification_receipt,
        source.exact_version_ref,
        receipt_subjects,
    )
    _receipt(
        source.implementation_acceptance_receipt,
        source.implementation_binding.content_hash_ref,
        receipt_subjects,
    )
    previous_hash = content_by_subject.setdefault(
        source.implementation_binding.subject_ref,
        source.implementation_binding.content_hash_ref,
    )
    if previous_hash != source.implementation_binding.content_hash_ref:
        raise BundleTargetContractError("reuse_content_binding_changed")
    eligibility = (
        source.eligibility_anchor_ref,
        source.eligibility_binding,
        source.eligibility_receipt,
    )
    if source.eligible_tier in _OWNER_ELIGIBLE_REUSE_TIERS:
        if any(value is None for value in eligibility):
            raise BundleTargetContractError("reuse_owner_eligibility_missing")
        assert source.eligibility_anchor_ref is not None
        assert source.eligibility_binding is not None
        assert source.eligibility_receipt is not None
        _ref(source.eligibility_anchor_ref, "eligible TargetCommit")
        _ref(source.eligibility_binding.subject_ref, "eligibility subject")
        _sha256(
            source.eligibility_binding.content_hash_ref,
            "eligibility content hash",
        )
        _receipt(
            source.eligibility_receipt,
            source.eligibility_binding.content_hash_ref,
            receipt_subjects,
        )
        previous_eligibility = content_by_subject.setdefault(
            source.eligibility_binding.subject_ref,
            source.eligibility_binding.content_hash_ref,
        )
        if previous_eligibility != source.eligibility_binding.content_hash_ref:
            raise BundleTargetContractError("reuse_eligibility_binding_changed")
    elif any(value is not None for value in eligibility):
        raise BundleTargetContractError("reuse_false_owner_eligibility")
    if source.eligible_tier == "mature-external":
        for name, value in (
            ("license", source.license_ref),
            ("source content hash", source.content_hash_ref),
            ("patch", source.patch_ref),
        ):
            _ref(value, f"mature external {name}")
        assert source.content_hash_ref is not None
        _sha256(source.content_hash_ref, "mature external source content hash")
    if decision.disposition == "selected" and (
        source.implementation_revision_ref != implementation_ref
    ):
        raise BundleTargetContractError("reuse_selected_revision_not_executed")


# ---------------------------------------------------------------------------
# Canonical parsers


def _candidate_from_dict(value: object) -> TargetCandidate:
    document = _exact_dict(
        value, set(_TARGET_CANDIDATE_FIELDS), "target_candidate_invalid"
    )
    return TargetCandidate(
        local_label=_ref(document["local_label"], "Target local label"),
        experiment_keys=_string_tuple(document["experiment_keys"], "ExperimentKeys"),
        measurement_unit_keys=_string_tuple(
            document["measurement_unit_keys"], "measurement_unit_keys"
        ),
        held_fixed_bindings=tuple(
            _held_fixed_from_dict(item)
            for item in _object_list(
                document["held_fixed_bindings"], "held_fixed_bindings"
            )
        ),
        implementation_revision_ref=_ref(
            document["implementation_revision_ref"], "ImplementationRevisionRef"
        ),
        code_changed=_exact_bool(document["code_changed"], "code_changed"),
        reuse_trace=_reuse_trace_from_dict(document["reuse_trace"]),
        routes=tuple(
            _route_from_dict(item)
            for item in _object_list(document["routes"], "routes")
        ),
        depends_on_labels=_string_tuple(
            document["depends_on_labels"], "depends_on_labels", allow_empty=True
        ),
        direct_accepted_input_asset_refs=_string_tuple(
            document["direct_accepted_input_asset_refs"],
            "direct_accepted_input_asset_refs",
            allow_empty=True,
        ),
    )


def _held_fixed_from_dict(value: object) -> HeldFixedBinding:
    document = _exact_dict(
        value,
        {"semantic_slot", "implementation_revision_ref"},
        "held_fixed_binding_invalid",
    )
    return HeldFixedBinding(
        semantic_slot=_ref(document["semantic_slot"], "held-fixed slot"),
        implementation_revision_ref=_ref(
            document["implementation_revision_ref"], "held-fixed revision"
        ),
    )


def _route_from_dict(value: object) -> RouteSpec:
    document = _exact_dict(
        value,
        {"route_ref", "known_external_operation_refs"},
        "route_invalid",
    )
    return RouteSpec(
        route_ref=_ref(document["route_ref"], "route ref"),
        known_external_operation_refs=_string_tuple(
            document["known_external_operation_refs"],
            "external operation refs",
            allow_empty=True,
        ),
    )


def _reuse_trace_from_dict(value: object) -> ReuseTrace:
    document = _exact_dict(
        value, {"tier_decisions", "greenfield_exception"}, "reuse_trace_invalid"
    )
    exception = document["greenfield_exception"]
    if exception is not None:
        exception = _ref(exception, "greenfield exception")
    return ReuseTrace(
        tier_decisions=tuple(
            _reuse_decision_from_dict(item)
            for item in _object_list(document["tier_decisions"], "tier_decisions")
        ),
        greenfield_exception=cast(str | None, exception),
    )


def _reuse_decision_from_dict(value: object) -> ReuseTierDecision:
    document = _exact_dict(
        value,
        {"tier", "disposition", "reason_ref", "source_proofs"},
        "reuse_tier_decision_invalid",
    )
    return ReuseTierDecision(
        tier=_ref(document["tier"], "reuse tier"),
        disposition=_ref(document["disposition"], "reuse disposition"),
        reason_ref=_ref(document["reason_ref"], "reuse reason"),
        source_proofs=tuple(
            _reuse_source_from_dict(item)
            for item in _object_list(document["source_proofs"], "source_proofs")
        ),
    )


def _reuse_source_from_dict(value: object) -> ReuseSourceProof:
    fields = {
        "source_ref",
        "exact_version_ref",
        "implementation_revision_ref",
        "eligible_tier",
        "verification_receipt",
        "implementation_binding",
        "implementation_acceptance_receipt",
        "eligibility_anchor_ref",
        "eligibility_binding",
        "eligibility_receipt",
        "license_ref",
        "content_hash_ref",
        "patch_ref",
    }
    document = _exact_dict(value, fields, "reuse_source_proof_invalid")
    return ReuseSourceProof(
        source_ref=_ref(document["source_ref"], "reuse source"),
        exact_version_ref=_ref(document["exact_version_ref"], "reuse version"),
        implementation_revision_ref=_ref(
            document["implementation_revision_ref"], "reuse implementation"
        ),
        eligible_tier=_ref(document["eligible_tier"], "eligible tier"),
        verification_receipt=_receipt_from_dict(document["verification_receipt"]),
        implementation_binding=_binding_from_dict(document["implementation_binding"]),
        implementation_acceptance_receipt=_receipt_from_dict(
            document["implementation_acceptance_receipt"]
        ),
        eligibility_anchor_ref=_optional_ref(
            document["eligibility_anchor_ref"], "eligibility anchor"
        ),
        eligibility_binding=(
            None
            if document["eligibility_binding"] is None
            else _binding_from_dict(document["eligibility_binding"])
        ),
        eligibility_receipt=(
            None
            if document["eligibility_receipt"] is None
            else _receipt_from_dict(document["eligibility_receipt"])
        ),
        license_ref=_optional_ref(document["license_ref"], "license ref"),
        content_hash_ref=_optional_ref(
            document["content_hash_ref"], "source content hash"
        ),
        patch_ref=_optional_ref(document["patch_ref"], "patch ref"),
    )


def _binding_from_dict(value: object) -> ContentBindingProof:
    document = _exact_dict(
        value, {"subject_ref", "content_hash_ref"}, "content_binding_invalid"
    )
    return ContentBindingProof(
        subject_ref=_ref(document["subject_ref"], "content subject"),
        content_hash_ref=_sha256(document["content_hash_ref"], "content hash"),
    )


def _receipt_from_dict(value: object) -> ReceiptProof:
    document = _exact_dict(
        value,
        {"receipt_ref", "subject_ref", "verified", "currentness_known", "current"},
        "receipt_proof_invalid",
    )
    return ReceiptProof(
        receipt_ref=_ref(document["receipt_ref"], "receipt ref"),
        subject_ref=_ref(document["subject_ref"], "receipt subject"),
        verified=_exact_bool(document["verified"], "receipt verified"),
        currentness_known=_exact_bool(
            document["currentness_known"], "receipt currentness known"
        ),
        current=_exact_bool(document["current"], "receipt current"),
    )


def _brief_from_dict(value: object) -> ExperimentBrief:
    document = _exact_dict(
        value,
        {
            "experiment_key",
            "semantic_delta",
            "held_fixed_slots",
            "required_measurement_unit_keys",
        },
        "normalized_experiment_brief_invalid",
    )
    return ExperimentBrief(
        experiment_key=_ref(document["experiment_key"], "ExperimentKey"),
        semantic_delta=_text(document["semantic_delta"], "SemanticDelta"),
        held_fixed_slots=_string_tuple(
            document["held_fixed_slots"], "held_fixed_slots", allow_empty=True
        ),
        required_measurement_unit_keys=_string_tuple(
            document["required_measurement_unit_keys"],
            "required_measurement_unit_keys",
        ),
    )


def _semantic_from_dict(value: object) -> FrozenSemanticInputs:
    document = _exact_dict(
        value,
        {
            "experiment_key",
            "goal",
            "characteristics",
            "boundary_constraints",
            "semantic_delta",
        },
        "semantic_normalization_invalid",
    )
    return FrozenSemanticInputs(
        experiment_key=_ref(document["experiment_key"], "ExperimentKey"),
        goal=_text(document["goal"], "Goal"),
        characteristics=_text(document["characteristics"], "Characteristics"),
        boundary_constraints=_text(
            document["boundary_constraints"], "BoundaryConstraints"
        ),
        semantic_delta=_text(document["semantic_delta"], "SemanticDelta"),
    )


def _semantic_to_dict(value: FrozenSemanticInputs) -> dict[str, object]:
    return {
        "experiment_key": value.experiment_key,
        "goal": value.goal,
        "characteristics": value.characteristics,
        "boundary_constraints": value.boundary_constraints,
        "semantic_delta": value.semantic_delta,
    }


# ---------------------------------------------------------------------------
# Plan/execution projections and primitive closed-value helpers


def _plan_briefs(
    plan_document: dict[str, object],
) -> tuple[str, dict[str, dict[str, object]]]:
    _validate_json_root(plan_document, "PlanDocument")
    if (
        plan_document.get("schema_ref") != PLAN_DOCUMENT_SCHEMA_REF
        or plan_document.get("kind") != "PlanDocument"
        or plan_document.get("bundle_disposition") != "experiments_required"
    ):
        raise BundleTargetContractError("completion_contract_plan_invalid")
    gaps_value = plan_document.get("gap_set")
    briefs_value = plan_document.get("experiment_briefs")
    gaps = set(_string_list(gaps_value, "Plan gap_set"))
    if not gaps or type(briefs_value) is not list or not briefs_value:
        raise BundleTargetContractError("completion_contract_plan_has_no_work")
    result: dict[str, dict[str, object]] = {}
    covered: set[str] = set()
    for value in briefs_value:
        brief = _exact_dict(value, set(_PLAN_BRIEF_FIELDS), "plan_brief_invalid")
        key = _ref(brief["experiment_key"], "ExperimentKey")
        if key in result:
            raise BundleTargetContractError("plan_brief_duplicate")
        brief_gaps = set(_string_list(brief["gap_obligation_keys"], "brief gaps"))
        if not brief_gaps or not brief_gaps <= gaps:
            raise BundleTargetContractError("plan_brief_gap_invalid")
        for field in ("goal", "characteristics", "boundary_constraints", "semantic_delta"):
            _text(brief[field], field)
        _string_list(
            brief["contributing_idea_refs"],
            "contributing idea refs",
            allow_empty=True,
        )
        result[key] = brief
        covered.update(brief_gaps)
    if covered != gaps:
        raise BundleTargetContractError("plan_brief_gap_set_incomplete")
    return _canonical_hash(plan_document, "PlanDocument"), result


def _freeze_legacy_execution(value: object) -> FrozenJsonObject:
    """Preserve already accepted v2 payloads for diagnostics only."""

    document = _exact_object(value, "legacy_target_execution_invalid")
    _validate_json_root(document, "Legacy Target execution")
    return _freeze_object(document, "Legacy Target execution")


def _binding_map(bindings: tuple[HeldFixedBinding, ...]) -> dict[str, str]:
    result: dict[str, str] = {}
    for binding in bindings:
        slot = _ref(binding.semantic_slot, "held-fixed slot")
        revision = _ref(binding.implementation_revision_ref, "held-fixed revision")
        if slot in result:
            raise BundleTargetContractError("held_fixed_slot_duplicate")
        result[slot] = revision
    return result


def _receipt(
    receipt: ReceiptProof,
    subject: str,
    observed: dict[str, str],
) -> None:
    try:
        validate_receipt_proof(receipt, subject_ref=subject)
    except BundleProtocolError as error:
        raise BundleTargetContractError("reuse_receipt_invalid") from error
    previous = observed.setdefault(receipt.receipt_ref, subject)
    if previous != subject:
        raise BundleTargetContractError("reuse_receipt_identity_rebound")


def _freeze_object(value: dict[str, object], name: str) -> FrozenJsonObject:
    return FrozenJsonObject(_canonical_json(value, name))


def _domain_document(value: object, name: str) -> FrozenJsonObject:
    document = _exact_object(value, f"{name}_invalid")
    _validate_json_root(document, name)
    _validate_domain_document_root(document, name)
    return _freeze_object(document, name)


def _validate_frozen_domain_document(
    value: FrozenJsonObject,
    name: str,
) -> None:
    if type(value) is not FrozenJsonObject:
        raise BundleTargetContractError(f"{name}_invalid")
    try:
        document = value.as_dict()
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise BundleTargetContractError(f"{name}_invalid") from error
    canonical = _canonical_json(document, name)
    if value.canonical_json != canonical:
        raise BundleTargetContractError(f"{name}_not_canonical")
    _validate_json_root(document, name)
    _validate_domain_document_root(document, name)


def _validate_domain_document_root(
    document: dict[str, object],
    name: str,
) -> None:
    if not document:
        raise BundleTargetContractError(f"{name}_invalid")
    pending: list[object] = [document]
    while pending:
        value = pending.pop()
        if type(value) is dict:
            nested = cast(dict[str, object], value)
            if any(
                key.casefold().replace("-", "_") in _DOMAIN_ROUTING_FIELDS
                for key in nested
            ):
                raise BundleTargetContractError(
                    f"{name}_contains_runtime_routing"
                )
            pending.extend(nested.values())
        elif type(value) is list:
            pending.extend(cast(list[object], value))


def _validate_json_root(value: object, name: str) -> str:
    state = {"nodes": 0}

    def visit(item: object, depth: int) -> None:
        if depth > _BUNDLE_ROOT_MAX_DEPTH:
            raise BundleTargetContractError(f"{name} exceeds depth budget")
        state["nodes"] += 1
        if state["nodes"] > BUNDLE_ROOT_MAX_NODES:
            raise BundleTargetContractError(f"{name} exceeds node budget")
        if type(item) is dict:
            if len(item) > BUNDLE_PROJECTION_MAX_TUPLE_ITEMS:
                raise BundleTargetContractError(f"{name} has oversized object")
            for key, nested in item.items():
                if type(key) is not str:
                    raise BundleTargetContractError(f"{name} has non-string key")
                _utf8(key, name)
                visit(nested, depth + 1)
            return
        if type(item) is list:
            if len(item) > BUNDLE_PROJECTION_MAX_TUPLE_ITEMS:
                raise BundleTargetContractError(f"{name} has oversized list")
            for nested in item:
                visit(nested, depth + 1)
            return
        if type(item) is str:
            if len(_utf8(item, name)) > BUNDLE_PROJECTION_STRING_MAX_UTF8_BYTES:
                raise BundleTargetContractError(f"{name} has oversized text")
            return
        if type(item) is bool or item is None:
            return
        if type(item) is int:
            if abs(item) > BUNDLE_CANONICAL_INTEGER_MAX_ABS:
                raise BundleTargetContractError(f"{name} has oversized integer")
            return
        if type(item) is float:
            if not math.isfinite(item):
                raise BundleTargetContractError(f"{name} has non-finite number")
            return
        raise BundleTargetContractError(f"{name} has non-canonical value")

    try:
        visit(value, 0)
        encoded = _canonical_json(value, name).encode("utf-8")
    except RecursionError as error:
        raise BundleTargetContractError(f"{name} is recursive") from error
    if len(encoded) > BUNDLE_ROOT_MAX_SERIALIZED_BYTES:
        raise BundleTargetContractError(f"{name} exceeds byte budget")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_json(value: object, name: str) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, OverflowError, UnicodeError) as error:
        raise BundleTargetContractError(f"{name} is not canonical UTF-8 JSON") from error


def _canonical_hash(value: object, name: str) -> str:
    _validate_json_root(value, name)
    return hashlib.sha256(_canonical_json(value, name).encode("utf-8")).hexdigest()


def _exact_dict(value: object, fields: set[str], code: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != fields:
        raise BundleTargetContractError(code)
    return cast(dict[str, object], value)


def _exact_object(value: object, code: str) -> dict[str, object]:
    if type(value) is not dict or not value:
        raise BundleTargetContractError(code)
    return cast(dict[str, object], value)


def _object_list(value: object, code: str) -> list[dict[str, object]]:
    if type(value) is not list or len(value) > BUNDLE_PROJECTION_MAX_TUPLE_ITEMS:
        raise BundleTargetContractError(code)
    if any(type(item) is not dict for item in value):
        raise BundleTargetContractError(code)
    return cast(list[dict[str, object]], value)


def _string_list(value: object, name: str, *, allow_empty: bool = False) -> list[str]:
    if type(value) is not list:
        raise BundleTargetContractError(f"{name}_invalid")
    result = [_ref(item, name) for item in value]
    if (not result and not allow_empty) or len(result) != len(set(result)):
        raise BundleTargetContractError(f"{name}_invalid")
    return result


def _string_tuple(value: object, name: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    return tuple(_string_list(value, name, allow_empty=allow_empty))


def _canonical_string_tuple(
    value: object, name: str, *, allow_empty: bool = False
) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise BundleTargetContractError(f"{name}_invalid")
    result = tuple(_ref(item, name) for item in value)
    if (not result and not allow_empty) or len(result) != len(set(result)):
        raise BundleTargetContractError(f"{name}_invalid")
    return result


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value.strip():
        raise BundleTargetContractError(f"{name}_invalid")
    if len(_utf8(value, name)) > BUNDLE_PROJECTION_STRING_MAX_UTF8_BYTES:
        raise BundleTargetContractError(f"{name}_invalid")
    return value


def _ref(value: object, name: str) -> str:
    result = _text(value, name)
    if result != result.strip() or any(char in result for char in ("\x00", "\r", "\n")):
        raise BundleTargetContractError(f"{name}_invalid")
    return result


def _optional_ref(value: object, name: str) -> str | None:
    return None if value is None else _ref(value, name)


def _metric_key(value: object) -> str:
    result = _ref(value, "MetricKey")
    if len(_utf8(result, "MetricKey")) > 128:
        raise BundleTargetContractError("MetricKey_invalid")
    return result


def _sha256(value: object, name: str) -> str:
    result = _ref(value, name)
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise BundleTargetContractError(f"{name}_invalid")
    return result


def _utf8(value: str, name: str) -> bytes:
    try:
        return value.encode("utf-8")
    except UnicodeError as error:
        raise BundleTargetContractError(f"{name} is not UTF-8") from error


def _exact_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise BundleTargetContractError(f"{name}_invalid")
    return value


def _risk_class(value: object) -> str:
    if type(value) is not str or value not in {"normal", "high"}:
        raise BundleTargetContractError("formal_target_risk_class_invalid")
    return value


def _checkpoint_policy(value: object) -> str:
    if type(value) is not str or value not in {
        "forbidden",
        "optional",
        "required",
    }:
        raise BundleTargetContractError("measurement_checkpoint_policy_invalid")
    return value


def _nonnegative_int(value: object, name: str) -> int:
    if type(value) is not int or value < 0 or value > BUNDLE_CANONICAL_INTEGER_MAX_ABS:
        raise BundleTargetContractError(f"{name}_invalid")
    return value


def _positive_int(value: object, name: str) -> int:
    result = _nonnegative_int(value, name)
    if result < 1:
        raise BundleTargetContractError(f"{name}_invalid")
    return result
