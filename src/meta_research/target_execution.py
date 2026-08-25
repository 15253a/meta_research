from __future__ import annotations

import math
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast

from meta_research.experiment_contract import (
    ExperimentIntent,
    ExperimentIntentLike,
    PROTOCOL_EXPERIMENT_DEFINITION_SCHEMA,
    PROTOCOL_EXPERIMENT_INTENT_SCHEMA,
    ProtocolExperimentIntent,
)
from meta_research.owners.common import OwnerConflict, canonical_json

if TYPE_CHECKING:
    from meta_research.owners.research_graph import AcceptedTarget


EXPERIMENT_RETRAIN_ADAPTER = "experiment_retrain"
EXPERIMENT_REMEASURE_ADAPTER = "experiment_remeasure"
EXPERIMENT_RETRAIN_SCHEMA_REF = (
    "meta-research/target-execution/experiment-retrain/v1"
)
EXPERIMENT_REMEASURE_SCHEMA_REF = (
    "meta-research/target-execution/experiment-remeasure/v1"
)
PROTOCOL_EVALUATION_ADAPTER = "protocol_evaluation"
PROTOCOL_EVALUATION_SCHEMA_REF = (
    "meta-research/target-execution/protocol-evaluation/v2"
)
LEGACY_MICRO_EXPERIMENT_ADAPTER = "legacy_micro_experiment"
EXPERIMENT_PROVIDER_CAPABILITY_CATALOG_SCHEMA = (
    "meta-research/experiment-provider-capability-catalog/v1"
)
MICRO_EXPERIMENT_DEFINITION_SCHEMA = (
    "meta-research/micro-experiment-definition/v1"
)


class TargetExecutionContractError(ValueError):
    pass


class ExperimentCoordinator(Protocol):
    def start(
        self,
        intent: ExperimentIntentLike,
        idempotency_key: str,
        *,
        require_idle: bool = False,
    ) -> dict[str, object]: ...


class TargetExecutionAdapter(Protocol):
    """Translate one versioned Target execution payload into a domain intent."""

    adapter_kind: str
    schema_ref: str

    def validate(self, execution: dict[str, object]) -> None: ...

    def intent(
        self,
        *,
        quest_ref: str,
        target_ref: str,
        target_spec: dict[str, object],
    ) -> ExperimentIntentLike: ...

    def json_schema(self) -> dict[str, object]: ...


@dataclass(frozen=True)
class TargetExecutionStart:
    adapter_kind: str
    target_run_ref: str
    evaluation_attempt_ref: str
    execution_request_ref: str
    definition_hash: str
    public_execution: dict[str, object]


@dataclass(frozen=True)
class _ExperimentRetrainAdapter:
    adapter_kind: str = EXPERIMENT_RETRAIN_ADAPTER
    schema_ref: str = EXPERIMENT_RETRAIN_SCHEMA_REF

    def validate(self, execution: dict[str, object]) -> None:
        request = _validate_envelope(execution, self.adapter_kind, self.schema_ref)
        _exact_keys(
            request,
            {"hypothesis", "variant_parameter", "sample_count"},
        )
        _validate_common_request(request)

    def intent(
        self,
        *,
        quest_ref: str,
        target_ref: str,
        target_spec: dict[str, object],
    ) -> ExperimentIntent:
        execution = _execution(target_spec)
        self.validate(execution)
        request = cast(dict[str, object], execution["request"])
        return ExperimentIntent(
            execution_request_ref=f"bundle-target-{target_ref}",
            quest_ref=quest_ref,
            title=_target_title(target_spec),
            hypothesis=cast(str, request["hypothesis"]),
            variant_parameter=float(request["variant_parameter"]),
            sample_count=cast(int, request["sample_count"]),
            request_kind="retrain",
        )

    def json_schema(self) -> dict[str, object]:
        return _execution_schema(
            adapter_kind=self.adapter_kind,
            schema_ref=self.schema_ref,
            request_properties=_common_request_schema(),
            required=("hypothesis", "variant_parameter", "sample_count"),
        )


@dataclass(frozen=True)
class _ExperimentRemeasureAdapter:
    adapter_kind: str = EXPERIMENT_REMEASURE_ADAPTER
    schema_ref: str = EXPERIMENT_REMEASURE_SCHEMA_REF

    def validate(self, execution: dict[str, object]) -> None:
        request = _validate_envelope(execution, self.adapter_kind, self.schema_ref)
        _exact_keys(
            request,
            {
                "hypothesis",
                "variant_parameter",
                "sample_count",
                "source_variant_run_ref",
                "selected_checkpoint_role_refs",
            },
        )
        _validate_common_request(request)
        source_ref = request.get("source_variant_run_ref")
        checkpoint_refs = request.get("selected_checkpoint_role_refs")
        if (
            not _bounded_text(source_ref, 96)
            or not isinstance(checkpoint_refs, list)
            or len(checkpoint_refs) > 32
            or not all(_bounded_text(value, 96) for value in checkpoint_refs)
            or len(checkpoint_refs) != len(set(checkpoint_refs))
        ):
            raise TargetExecutionContractError("target_execution_request_invalid")

    def intent(
        self,
        *,
        quest_ref: str,
        target_ref: str,
        target_spec: dict[str, object],
    ) -> ExperimentIntent:
        execution = _execution(target_spec)
        self.validate(execution)
        request = cast(dict[str, object], execution["request"])
        return ExperimentIntent(
            execution_request_ref=f"bundle-target-{target_ref}",
            quest_ref=quest_ref,
            title=_target_title(target_spec),
            hypothesis=cast(str, request["hypothesis"]),
            variant_parameter=float(request["variant_parameter"]),
            sample_count=cast(int, request["sample_count"]),
            request_kind="remeasure",
            source_variant_run_ref=cast(str, request["source_variant_run_ref"]),
            selected_checkpoint_role_refs=tuple(
                cast(list[str], request["selected_checkpoint_role_refs"])
            ),
        )

    def json_schema(self) -> dict[str, object]:
        properties = {
            **_common_request_schema(),
            "source_variant_run_ref": {
                "type": "string",
                "minLength": 1,
                "maxLength": 96,
            },
            "selected_checkpoint_role_refs": {
                "type": "array",
                "maxItems": 32,
                "uniqueItems": True,
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 96,
                },
            },
        }
        return _execution_schema(
            adapter_kind=self.adapter_kind,
            schema_ref=self.schema_ref,
            request_properties=properties,
            required=(
                "hypothesis",
                "variant_parameter",
                "sample_count",
                "source_variant_run_ref",
                "selected_checkpoint_role_refs",
            ),
        )


@dataclass(frozen=True)
class _ProtocolEvaluationAdapter:
    """Provider-neutral Adapter for result-bearing rule, model, or agent work."""

    adapter_kind: str = PROTOCOL_EVALUATION_ADAPTER
    schema_ref: str = PROTOCOL_EVALUATION_SCHEMA_REF

    def validate(self, execution: dict[str, object]) -> None:
        request = _validate_envelope(execution, self.adapter_kind, self.schema_ref)
        _exact_keys(
            request,
            {
                "objective",
                "variant_source",
                "baseline_forward_contract",
                "variant_recipe",
                "evaluation_protocol_lineage",
                "protocol_version",
                "checkpoint_policy",
                "provider",
            },
        )
        if not _bounded_text(request.get("objective"), 4000):
            raise TargetExecutionContractError("target_execution_request_invalid")
        documents = (
            request.get("baseline_forward_contract"),
            request.get("variant_recipe"),
            request.get("evaluation_protocol_lineage"),
            request.get("protocol_version"),
            request.get("provider"),
        )
        if any(not isinstance(value, dict) or not value for value in documents):
            raise TargetExecutionContractError("target_execution_request_invalid")
        if len(canonical_json(request).encode("utf-8")) > 512_000:
            raise TargetExecutionContractError("target_execution_request_too_large")
        source = request.get("variant_source")
        policy = request.get("checkpoint_policy")
        if not isinstance(source, dict) or policy not in {
            "forbidden",
            "optional",
            "required",
        }:
            raise TargetExecutionContractError("target_execution_request_invalid")
        source_kind = source.get("kind")
        if source_kind == "new":
            _exact_keys(source, {"kind"})
            request_kind = "new_variant"
            source_ref = None
            checkpoint_refs: tuple[str, ...] = ()
        elif source_kind == "existing":
            _exact_keys(
                source,
                {"kind", "source_variant_run_ref", "selected_checkpoint_role_refs"},
            )
            source_ref = source.get("source_variant_run_ref")
            selected = source.get("selected_checkpoint_role_refs")
            if (
                not _bounded_text(source_ref, 96)
                or not isinstance(selected, list)
                or len(selected) > 32
                or not all(_bounded_text(value, 96) for value in selected)
                or len(selected) != len(set(selected))
                or policy != "forbidden"
            ):
                raise TargetExecutionContractError("target_execution_request_invalid")
            request_kind = "remeasure"
            checkpoint_refs = tuple(cast(list[str], selected))
        else:
            raise TargetExecutionContractError("target_execution_request_invalid")
        provider = cast(dict[str, object], request["provider"])
        if set(provider) != {"adapter_kind", "schema_ref", "payload"}:
            raise TargetExecutionContractError("target_execution_request_invalid")
        try:
            ProtocolExperimentIntent(
                execution_request_ref="contract-validation",
                quest_ref="contract-validation",
                title="contract-validation",
                objective=cast(str, request["objective"]),
                baseline_forward_contract=cast(
                    dict[str, object], request["baseline_forward_contract"]
                ),
                variant_recipe=cast(dict[str, object], request["variant_recipe"]),
                evaluation_protocol_lineage=cast(
                    dict[str, object], request["evaluation_protocol_lineage"]
                ),
                protocol_version=cast(
                    dict[str, object], request["protocol_version"]
                ),
                execution=provider,
                checkpoint_policy=cast(
                    str, policy
                ),  # runtime validation narrows the literal
                request_kind=cast(str, request_kind),
                source_variant_run_ref=cast(str | None, source_ref),
                selected_checkpoint_role_refs=checkpoint_refs,
            ).validate()
        except (OwnerConflict, TypeError, ValueError) as error:
            raise TargetExecutionContractError(str(error)) from error

    def intent(
        self,
        *,
        quest_ref: str,
        target_ref: str,
        target_spec: dict[str, object],
    ) -> ProtocolExperimentIntent:
        execution = _execution(target_spec)
        self.validate(execution)
        request = cast(dict[str, object], execution["request"])
        source = cast(dict[str, object], request["variant_source"])
        existing = source["kind"] == "existing"
        return ProtocolExperimentIntent(
            execution_request_ref=f"bundle-target-{target_ref}",
            quest_ref=quest_ref,
            title=_target_title(target_spec),
            objective=cast(str, request["objective"]),
            baseline_forward_contract=cast(
                dict[str, object], request["baseline_forward_contract"]
            ),
            variant_recipe=cast(dict[str, object], request["variant_recipe"]),
            evaluation_protocol_lineage=cast(
                dict[str, object], request["evaluation_protocol_lineage"]
            ),
            protocol_version=cast(dict[str, object], request["protocol_version"]),
            execution=cast(dict[str, object], request["provider"]),
            checkpoint_policy=cast(str, request["checkpoint_policy"]),
            request_kind="remeasure" if existing else "new_variant",
            source_variant_run_ref=(
                cast(str, source["source_variant_run_ref"]) if existing else None
            ),
            selected_checkpoint_role_refs=(
                tuple(cast(list[str], source["selected_checkpoint_role_refs"]))
                if existing
                else ()
            ),
        )

    def json_schema(self) -> dict[str, object]:
        document = {"type": "object", "minProperties": 1}
        provider = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "adapter_kind": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                },
                "schema_ref": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 256,
                },
                "payload": {"type": "object"},
            },
            "required": ["adapter_kind", "schema_ref", "payload"],
        }
        source = {
            "oneOf": [
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"kind": {"const": "new"}},
                    "required": ["kind"],
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "kind": {"const": "existing"},
                        "source_variant_run_ref": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 96,
                        },
                        "selected_checkpoint_role_refs": {
                            "type": "array",
                            "maxItems": 32,
                            "uniqueItems": True,
                            "items": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 96,
                            },
                        },
                    },
                    "required": [
                        "kind",
                        "source_variant_run_ref",
                        "selected_checkpoint_role_refs",
                    ],
                },
            ]
        }
        return _execution_schema(
            adapter_kind=self.adapter_kind,
            schema_ref=self.schema_ref,
            request_properties={
                "objective": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 4000,
                },
                "variant_source": source,
                "baseline_forward_contract": document,
                "variant_recipe": document,
                "evaluation_protocol_lineage": document,
                "protocol_version": document,
                "checkpoint_policy": {
                    "type": "string",
                    "enum": ["forbidden", "optional", "required"],
                },
                "provider": provider,
            },
            required=(
                "objective",
                "variant_source",
                "baseline_forward_contract",
                "variant_recipe",
                "evaluation_protocol_lineage",
                "protocol_version",
                "checkpoint_policy",
                "provider",
            ),
        )


@dataclass(frozen=True)
class _LegacyMicroExperimentAdapter:
    """Read/execute compatibility for already-receipted TargetPlan v1 rows."""

    adapter_kind: str = LEGACY_MICRO_EXPERIMENT_ADAPTER
    schema_ref: str = "meta-research/target-plan/v1#micro_experiment"

    def validate(self, execution: dict[str, object]) -> None:
        raise TargetExecutionContractError("legacy_target_execution_is_read_only")

    def intent(
        self,
        *,
        quest_ref: str,
        target_ref: str,
        target_spec: dict[str, object],
    ) -> ExperimentIntent:
        if target_spec.get("target_type") != "micro_experiment":
            raise TargetExecutionContractError("legacy_target_execution_invalid")
        try:
            intent = ExperimentIntent(
                execution_request_ref=f"bundle-target-{target_ref}",
                quest_ref=quest_ref,
                title=cast(str, target_spec["title"]),
                hypothesis=cast(str, target_spec["hypothesis"]),
                variant_parameter=float(target_spec["variant_parameter"]),
                sample_count=cast(int, target_spec["sample_count"]),
            )
            intent.validate()
        except (KeyError, TypeError, ValueError, OwnerConflict) as error:
            raise TargetExecutionContractError(
                "legacy_target_execution_invalid"
            ) from error
        return intent

    def json_schema(self) -> dict[str, object]:
        raise TargetExecutionContractError("legacy_target_execution_is_read_only")


_ADAPTERS: tuple[TargetExecutionAdapter, ...] = (
    _ExperimentRetrainAdapter(),
    _ExperimentRemeasureAdapter(),
    _ProtocolEvaluationAdapter(),
)
_ADAPTER_BY_KIND = {adapter.adapter_kind: adapter for adapter in _ADAPTERS}
_LEGACY_ADAPTER = _LegacyMicroExperimentAdapter()


class TargetExecutionCoordinator:
    """Deep Bundle seam hiding adapter selection and execution result parsing."""

    def __init__(self, experiment: ExperimentCoordinator) -> None:
        self._experiment = experiment

    def start(
        self,
        *,
        quest_ref: str,
        target: "AcceptedTarget",
        idempotency_key: str,
    ) -> TargetExecutionStart:
        adapter = target_execution_adapter(target.spec)
        execution = self._experiment.start(
            adapter.intent(
                quest_ref=quest_ref,
                target_ref=target.target_ref,
                target_spec=target.spec,
            ),
            idempotency_key,
        )
        identities = execution.get("identities")
        runtime = execution.get("execution")
        intent = execution.get("intent")
        execution_request = execution.get("execution_request")
        definition = (
            execution_request.get("definition")
            if isinstance(execution_request, dict)
            else None
        )
        if (
            not isinstance(identities, dict)
            or not isinstance(runtime, dict)
            or not isinstance(intent, dict)
            or not isinstance(identities.get("evaluation_attempt_ref"), str)
            or not isinstance(runtime.get("run_ref"), str)
            or not isinstance(intent.get("execution_request_ref"), str)
            or not isinstance(definition, dict)
            or not isinstance(definition.get("content_hash"), str)
        ):
            raise OwnerConflict("target_run_admission_invalid")
        return TargetExecutionStart(
            adapter_kind=adapter.adapter_kind,
            target_run_ref=cast(str, runtime["run_ref"]),
            evaluation_attempt_ref=cast(
                str, identities["evaluation_attempt_ref"]
            ),
            execution_request_ref=cast(str, intent["execution_request_ref"]),
            definition_hash=cast(
                str,
                definition["content_hash"],
            ),
            public_execution=execution,
        )


def target_execution_adapter(
    target_spec: dict[str, object],
) -> TargetExecutionAdapter:
    if "execution" not in target_spec:
        if target_spec.get("target_type") == "micro_experiment":
            return _LEGACY_ADAPTER
        raise TargetExecutionContractError("target_execution_invalid")
    execution = _execution(target_spec)
    kind = execution.get("adapter_kind")
    adapter = _ADAPTER_BY_KIND.get(kind) if isinstance(kind, str) else None
    if adapter is None:
        raise TargetExecutionContractError("target_execution_adapter_unavailable")
    adapter.validate(execution)
    return adapter


def target_execution_kind(target_spec: dict[str, object]) -> str:
    return target_execution_adapter(target_spec).adapter_kind


def target_experiment_intent(
    *,
    quest_ref: str,
    target_ref: str,
    target_spec: dict[str, object],
) -> ExperimentIntentLike:
    return target_execution_adapter(target_spec).intent(
        quest_ref=quest_ref,
        target_ref=target_ref,
        target_spec=target_spec,
    )


def validate_target_execution(execution: object) -> str:
    if not isinstance(execution, dict):
        raise TargetExecutionContractError("target_execution_invalid")
    kind = execution.get("adapter_kind")
    adapter = _ADAPTER_BY_KIND.get(kind) if isinstance(kind, str) else None
    if adapter is None:
        raise TargetExecutionContractError("target_execution_adapter_unavailable")
    adapter.validate(execution)
    return adapter.adapter_kind


def target_execution_json_schema(
    capability_catalog: dict[str, object] | None = None,
) -> dict[str, object]:
    """Describe only Target adapters routable by the installed providers.

    With no catalog this remains the provider-neutral contract schema used by
    pure validation tests.  Production Bundle planning supplies the immutable
    registry catalog, so a schema-level adapter option is never mistaken for
    an installed execution capability.
    """

    if capability_catalog is None:
        return {"oneOf": [adapter.json_schema() for adapter in _ADAPTERS]}
    capabilities = _provider_catalog_capabilities(capability_catalog)
    variants: list[dict[str, object]] = []
    for capability in capabilities:
        intent_schema_ref = capability["intent_schema_ref"]
        definition_schema_ref = capability["definition_schema_ref"]
        request_kinds = cast(tuple[str, ...], capability["request_kinds"])
        execution_adapter_kind = capability["execution_adapter_kind"]
        execution_schema_ref = capability["execution_schema_ref"]
        if (
            intent_schema_ref is None
            and definition_schema_ref == MICRO_EXPERIMENT_DEFINITION_SCHEMA
            and execution_adapter_kind is None
            and execution_schema_ref is None
        ):
            if "retrain" in request_kinds:
                variants.append(
                    _ADAPTER_BY_KIND[
                        EXPERIMENT_RETRAIN_ADAPTER
                    ].json_schema()
                )
            if "remeasure" in request_kinds:
                variants.append(
                    _ADAPTER_BY_KIND[EXPERIMENT_REMEASURE_ADAPTER].json_schema()
                )
            continue
        if (
            intent_schema_ref == PROTOCOL_EXPERIMENT_INTENT_SCHEMA
            and definition_schema_ref == PROTOCOL_EXPERIMENT_DEFINITION_SCHEMA
            and isinstance(execution_adapter_kind, str)
            and isinstance(execution_schema_ref, str)
        ):
            supported_sources = {
                "new" if kind == "new_variant" else "existing"
                for kind in request_kinds
                if kind in {"new_variant", "remeasure"}
            }
            if supported_sources:
                variants.append(
                    _protocol_execution_schema_for_capability(
                        execution_adapter_kind=execution_adapter_kind,
                        execution_schema_ref=execution_schema_ref,
                        supported_sources=supported_sources,
                    )
                )
    if not variants:
        raise TargetExecutionContractError(
            "target_execution_capability_catalog_unavailable"
        )
    return {"oneOf": variants}


def _provider_catalog_capabilities(
    catalog: dict[str, object],
) -> tuple[dict[str, object], ...]:
    if (
        type(catalog) is not dict
        or set(catalog) != {"schema_ref", "capabilities"}
        or catalog.get("schema_ref")
        != EXPERIMENT_PROVIDER_CAPABILITY_CATALOG_SCHEMA
        or type(catalog.get("capabilities")) is not list
        or not catalog["capabilities"]
    ):
        raise TargetExecutionContractError(
            "target_execution_capability_catalog_invalid"
        )
    result: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw in cast(list[object], catalog["capabilities"]):
        if type(raw) is not dict or set(raw) != {
            "intent_schema_ref",
            "definition_schema_ref",
            "request_kinds",
            "execution_adapter_kind",
            "execution_schema_ref",
        }:
            raise TargetExecutionContractError(
                "target_execution_capability_catalog_invalid"
            )
        capability = cast(dict[str, object], raw)
        request_kinds = capability["request_kinds"]
        optional = (
            capability["intent_schema_ref"],
            capability["execution_adapter_kind"],
            capability["execution_schema_ref"],
        )
        if (
            not isinstance(capability["definition_schema_ref"], str)
            or not capability["definition_schema_ref"]
            or type(request_kinds) is not list
            or not request_kinds
            or not all(isinstance(value, str) and value for value in request_kinds)
            or len(request_kinds) != len(set(cast(list[str], request_kinds)))
            or any(
                value is not None and not isinstance(value, str)
                for value in optional
            )
            or (capability["execution_adapter_kind"] is None)
            != (capability["execution_schema_ref"] is None)
        ):
            raise TargetExecutionContractError(
                "target_execution_capability_catalog_invalid"
            )
        normalized = {
            **capability,
            "request_kinds": tuple(cast(list[str], request_kinds)),
        }
        identity = canonical_json(capability)
        if identity in seen:
            raise TargetExecutionContractError(
                "target_execution_capability_catalog_invalid"
            )
        seen.add(identity)
        result.append(normalized)
    return tuple(result)


def _protocol_execution_schema_for_capability(
    *,
    execution_adapter_kind: str,
    execution_schema_ref: str,
    supported_sources: set[str],
) -> dict[str, object]:
    schema = deepcopy(_ADAPTER_BY_KIND[PROTOCOL_EVALUATION_ADAPTER].json_schema())
    request = cast(
        dict[str, object],
        cast(dict[str, object], schema["properties"])["request"],
    )
    request_properties = cast(dict[str, object], request["properties"])
    request_properties["provider"] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "adapter_kind": {"const": execution_adapter_kind},
            "schema_ref": {"const": execution_schema_ref},
            "payload": {"type": "object"},
        },
        "required": ["adapter_kind", "schema_ref", "payload"],
    }
    source = cast(dict[str, object], request_properties["variant_source"])
    source_variants = cast(list[dict[str, object]], source["oneOf"])
    source["oneOf"] = [
        variant
        for variant in source_variants
        if cast(dict[str, object], variant["properties"])["kind"]["const"]
        in supported_sources
    ]
    return schema


def _execution(target_spec: dict[str, object]) -> dict[str, object]:
    value = target_spec.get("execution")
    if not isinstance(value, dict):
        raise TargetExecutionContractError("target_execution_invalid")
    return cast(dict[str, object], value)


def _target_title(target_spec: dict[str, object]) -> str:
    """Project a display label without adding one to canonical TargetCandidate.

    Formal TargetPlan v3 stores the immutable strategy identity in
    ``candidate.local_label``.  ``title`` remains read compatibility for older
    already-receipted target specs; execution must not require the legacy
    micro-shaped wrapper.
    """

    candidate = target_spec.get("candidate")
    value = (
        candidate.get("local_label")
        if isinstance(candidate, dict)
        else target_spec.get("title")
    )
    if not _bounded_text(value, 256):
        raise TargetExecutionContractError("target_execution_target_label_invalid")
    return cast(str, value)


def _validate_envelope(
    execution: dict[str, object], adapter_kind: str, schema_ref: str
) -> dict[str, object]:
    _exact_keys(execution, {"adapter_kind", "schema_ref", "request"})
    request = execution.get("request")
    if (
        execution.get("adapter_kind") != adapter_kind
        or execution.get("schema_ref") != schema_ref
        or not isinstance(request, dict)
    ):
        raise TargetExecutionContractError("target_execution_invalid")
    return cast(dict[str, object], request)


def _validate_common_request(request: dict[str, object]) -> None:
    hypothesis = request.get("hypothesis")
    variant = request.get("variant_parameter")
    sample_count = request.get("sample_count")
    if (
        not _bounded_text(hypothesis, 4000)
        or not isinstance(variant, (int, float))
        or isinstance(variant, bool)
        or not math.isfinite(float(variant))
        or not isinstance(sample_count, int)
        or isinstance(sample_count, bool)
        or not 4 <= sample_count <= 4096
    ):
        raise TargetExecutionContractError("target_execution_request_invalid")


def _exact_keys(value: dict[str, object], expected: set[str]) -> None:
    if set(value) != expected:
        raise TargetExecutionContractError("target_execution_invalid")


def _bounded_text(value: object, maximum: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum


def _common_request_schema() -> dict[str, object]:
    return {
        "hypothesis": {"type": "string", "minLength": 1, "maxLength": 4000},
        "variant_parameter": {"type": "number"},
        "sample_count": {"type": "integer", "minimum": 4, "maximum": 4096},
    }


def _execution_schema(
    *,
    adapter_kind: str,
    schema_ref: str,
    request_properties: dict[str, object],
    required: tuple[str, ...],
) -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "adapter_kind": {"const": adapter_kind},
            "schema_ref": {"const": schema_ref},
            "request": {
                "type": "object",
                "additionalProperties": False,
                "properties": request_properties,
                "required": list(required),
            },
        },
        "required": ["adapter_kind", "schema_ref", "request"],
    }
