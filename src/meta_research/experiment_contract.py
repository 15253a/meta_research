from __future__ import annotations

import base64
import hashlib
import math
from dataclasses import dataclass
from typing import Callable, Literal, cast

from meta_research.owners.common import (
    AcceptedAssetBinding,
    AcceptanceReceipt,
    OwnerConflict,
    canonical_hash,
)


EXPERIMENT_INPUT_BINDING_SCHEMA = "meta-research/experiment-input-binding/v1"
EXPERIMENT_RUNTIME_BINDING_SCHEMA = "meta-research/experiment-runtime-binding/v1"
EXPERIMENT_RESULT_SCHEMA = "meta-research/micro-experiment-result/v1"
PROTOCOL_EXPERIMENT_INTENT_SCHEMA = "meta-research/protocol-experiment-intent/v2"
PROTOCOL_EXPERIMENT_DEFINITION_SCHEMA = (
    "meta-research/protocol-experiment-definition/v2"
)
PROTOCOL_EXPERIMENT_RESULT_SCHEMA = "meta-research/protocol-experiment-result/v2"
EXPERIMENT_RESULT_COMPONENT_MANIFEST_SCHEMA = (
    "meta-research/experiment-result-component-manifest/v1"
)
EXPERIMENT_REQUIRED_METRICS = (
    "baseline_mean",
    "variant_mean",
    "mean_delta",
)
EXPERIMENT_RESULT_DISPOSITIONS = frozenset(
    {"positive", "negative", "zero", "nonsignificant", "denied", "uncertain"}
)
EXPERIMENT_RETRYABLE_PROVIDER_FAILURES = frozenset(
    {
        "experiment_provider_failed",
        "experiment_provider_launch_failed",
        "experiment_provider_stopped",
        "experiment_provider_timeout",
    }
)


class ExperimentProviderUnavailable(OwnerConflict):
    """Typed provider failure with an explicit durable-effect outcome."""

    def __init__(
        self,
        code: str,
        *,
        durable_outcome: Literal["unknown", "pending", "terminal"] = "unknown",
    ) -> None:
        super().__init__(code)
        self.durable_outcome = durable_outcome


@dataclass(frozen=True)
class ExperimentIntent:
    """One Web-authorized semantic state formation and measurement request."""

    execution_request_ref: str
    quest_ref: str
    title: str
    hypothesis: str
    variant_parameter: float
    sample_count: int
    wall_time_budget_seconds: float = 300.0
    request_kind: Literal["retrain", "remeasure"] = "retrain"
    source_variant_run_ref: str | None = None
    selected_checkpoint_role_refs: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        self.validate()
        return {
            "execution_request_ref": self.execution_request_ref,
            "quest_ref": self.quest_ref,
            "title": self.title,
            "hypothesis": self.hypothesis,
            "variant_parameter": self.variant_parameter,
            "sample_count": self.sample_count,
            "wall_time_budget_seconds": self.wall_time_budget_seconds,
            "request_kind": self.request_kind,
            "source_variant_run_ref": self.source_variant_run_ref,
            "selected_checkpoint_role_refs": list(self.selected_checkpoint_role_refs),
        }

    def validate(self) -> None:
        if not self.execution_request_ref or len(self.execution_request_ref) > 96:
            raise OwnerConflict("experiment_execution_request_ref_invalid")
        if not self.quest_ref or len(self.quest_ref) > 96:
            raise OwnerConflict("experiment_quest_ref_invalid")
        if not self.title.strip() or len(self.title) > 512:
            raise OwnerConflict("experiment_title_invalid")
        if not self.hypothesis.strip() or len(self.hypothesis) > 4000:
            raise OwnerConflict("experiment_hypothesis_invalid")
        if isinstance(self.variant_parameter, bool) or not math.isfinite(
            self.variant_parameter
        ):
            raise OwnerConflict("experiment_variant_parameter_invalid")
        if isinstance(self.sample_count, bool) or not 4 <= self.sample_count <= 4096:
            raise OwnerConflict("experiment_sample_count_invalid")
        if (
            isinstance(self.wall_time_budget_seconds, bool)
            or not isinstance(self.wall_time_budget_seconds, (int, float))
            or not math.isfinite(float(self.wall_time_budget_seconds))
            or not 1 <= float(self.wall_time_budget_seconds) <= 24 * 60 * 60
        ):
            raise OwnerConflict("experiment_wall_time_budget_invalid")
        if self.request_kind not in {"retrain", "remeasure"}:
            raise OwnerConflict("experiment_request_kind_invalid")
        if self.request_kind == "retrain":
            if self.source_variant_run_ref is not None:
                raise OwnerConflict("experiment_source_variant_run_forbidden")
            if self.selected_checkpoint_role_refs:
                raise OwnerConflict("experiment_checkpoint_selection_forbidden")
        elif not self.source_variant_run_ref:
            raise OwnerConflict("experiment_source_variant_run_required")
        if (
            self.source_variant_run_ref is not None
            and len(self.source_variant_run_ref) > 96
        ):
            raise OwnerConflict("experiment_source_variant_run_invalid")
        checkpoints = self.selected_checkpoint_role_refs
        if (
            len(checkpoints) > 32
            or any(
                not isinstance(ref, str) or not ref or len(ref) > 96
                for ref in checkpoints
            )
            or len(set(checkpoints)) != len(checkpoints)
        ):
            raise OwnerConflict("experiment_checkpoint_selection_invalid")

    @property
    def content_hash(self) -> str:
        return canonical_hash(self.as_dict())

    @property
    def forms_new_variant(self) -> bool:
        return self.request_kind == "retrain"

    @property
    def checkpoint_policy(self) -> Literal["forbidden", "optional", "required"]:
        return "required" if self.forms_new_variant else "forbidden"

    @property
    def result_schema_ref(self) -> str:
        return EXPERIMENT_RESULT_SCHEMA


@dataclass(frozen=True)
class ProtocolExperimentIntent:
    """Versioned, provider-neutral state formation and measurement request.

    The four semantic documents remain owned by Research Graph.  Only the
    versioned ``execution`` envelope is interpreted by the installed provider.
    This keeps a packaged example runner out of the product Target contract.
    """

    execution_request_ref: str
    quest_ref: str
    title: str
    objective: str
    baseline_forward_contract: dict[str, object]
    variant_recipe: dict[str, object]
    evaluation_protocol_lineage: dict[str, object]
    protocol_version: dict[str, object]
    execution: dict[str, object]
    checkpoint_policy: Literal["forbidden", "optional", "required"]
    request_kind: Literal["new_variant", "remeasure"] = "new_variant"
    source_variant_run_ref: str | None = None
    selected_checkpoint_role_refs: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_ref": PROTOCOL_EXPERIMENT_INTENT_SCHEMA,
            "execution_request_ref": self.execution_request_ref,
            "quest_ref": self.quest_ref,
            "title": self.title,
            "objective": self.objective,
            "request_kind": self.request_kind,
            "source_variant_run_ref": self.source_variant_run_ref,
            "selected_checkpoint_role_refs": list(
                self.selected_checkpoint_role_refs
            ),
            "checkpoint_policy": self.checkpoint_policy,
            "baseline_forward_contract": self.baseline_forward_contract,
            "variant_recipe": self.variant_recipe,
            "evaluation_protocol_lineage": self.evaluation_protocol_lineage,
            "protocol_version": self.protocol_version,
            "execution": self.execution,
        }

    def validate(self) -> None:
        if not self.execution_request_ref or len(self.execution_request_ref) > 96:
            raise OwnerConflict("experiment_execution_request_ref_invalid")
        if not self.quest_ref or len(self.quest_ref) > 96:
            raise OwnerConflict("experiment_quest_ref_invalid")
        if not self.title.strip() or len(self.title) > 512:
            raise OwnerConflict("experiment_title_invalid")
        if not self.objective.strip() or len(self.objective) > 4000:
            raise OwnerConflict("experiment_objective_invalid")
        if self.request_kind not in {"new_variant", "remeasure"}:
            raise OwnerConflict("experiment_request_kind_invalid")
        if self.checkpoint_policy not in {"forbidden", "optional", "required"}:
            raise OwnerConflict("experiment_checkpoint_policy_invalid")
        if self.request_kind == "new_variant":
            if self.source_variant_run_ref is not None:
                raise OwnerConflict("experiment_source_variant_run_forbidden")
            if self.selected_checkpoint_role_refs:
                raise OwnerConflict("experiment_checkpoint_selection_forbidden")
        else:
            if not self.source_variant_run_ref:
                raise OwnerConflict("experiment_source_variant_run_required")
            if self.checkpoint_policy != "forbidden":
                raise OwnerConflict("experiment_checkpoint_policy_invalid")
        if (
            self.source_variant_run_ref is not None
            and len(self.source_variant_run_ref) > 96
        ):
            raise OwnerConflict("experiment_source_variant_run_invalid")
        checkpoints = self.selected_checkpoint_role_refs
        if (
            len(checkpoints) > 32
            or any(
                not isinstance(ref, str) or not ref or len(ref) > 96
                for ref in checkpoints
            )
            or len(set(checkpoints)) != len(checkpoints)
        ):
            raise OwnerConflict("experiment_checkpoint_selection_invalid")
        documents = (
            self.baseline_forward_contract,
            self.variant_recipe,
            self.evaluation_protocol_lineage,
            self.protocol_version,
            self.execution,
        )
        if any(not isinstance(value, dict) or not value for value in documents):
            raise OwnerConflict("experiment_definition_invalid")
        for document in documents:
            canonical_hash(document)
        required = self.protocol_version.get("required_metrics")
        optional = self.protocol_version.get("optional_metrics", [])
        if (
            not isinstance(required, list)
            or not required
            or len(required) > 64
            or not all(
                isinstance(value, str) and bool(value.strip()) and len(value) <= 128
                for value in required
            )
            or len(required) != len(set(required))
            or not isinstance(optional, list)
            or len(optional) > 64
            or not all(
                isinstance(value, str) and bool(value.strip()) and len(value) <= 128
                for value in optional
            )
            or len(optional) != len(set(optional))
            or set(required) & set(optional)
        ):
            raise OwnerConflict("experiment_required_metrics_invalid")
        if set(self.execution) != {"adapter_kind", "schema_ref", "payload"}:
            raise OwnerConflict("experiment_execution_adapter_invalid")
        if (
            not isinstance(self.execution.get("adapter_kind"), str)
            or not cast(str, self.execution["adapter_kind"]).strip()
            or len(cast(str, self.execution["adapter_kind"])) > 128
            or not isinstance(self.execution.get("schema_ref"), str)
            or not cast(str, self.execution["schema_ref"]).strip()
            or len(cast(str, self.execution["schema_ref"])) > 256
            or not isinstance(self.execution.get("payload"), dict)
        ):
            raise OwnerConflict("experiment_execution_adapter_invalid")

    @property
    def content_hash(self) -> str:
        return canonical_hash(self.as_dict())

    @property
    def forms_new_variant(self) -> bool:
        return self.request_kind == "new_variant"

    @property
    def result_schema_ref(self) -> str:
        return PROTOCOL_EXPERIMENT_RESULT_SCHEMA

    @property
    def required_metrics(self) -> tuple[str, ...]:
        self.validate()
        return tuple(cast(list[str], self.protocol_version["required_metrics"]))

    @property
    def optional_metrics(self) -> tuple[str, ...]:
        self.validate()
        return tuple(cast(list[str], self.protocol_version.get("optional_metrics", [])))


ExperimentIntentLike = ExperimentIntent | ProtocolExperimentIntent


@dataclass(frozen=True)
class ExperimentRuntimeBinding:
    runner_bundle_hash: str
    adapter_ref: str
    interpreter_ref: str
    capability_bindings: tuple[str, ...]
    resource_bindings: tuple[str, ...]
    schema_ref: str = EXPERIMENT_RUNTIME_BINDING_SCHEMA

    def as_dict(self) -> dict[str, object]:
        document = {
            "schema_ref": self.schema_ref,
            "runner_bundle_hash": self.runner_bundle_hash,
            "adapter_ref": self.adapter_ref,
            "interpreter_ref": self.interpreter_ref,
            "capability_bindings": list(self.capability_bindings),
            "resource_bindings": list(self.resource_bindings),
        }
        validate_experiment_runtime_binding(self)
        return document


@dataclass(frozen=True)
class AcceptedExperimentInputBinding:
    binding_ref: str
    subject_kind: Literal["variant_run", "evaluation_attempt"]
    subject_ref: str
    inputs: dict[str, object]
    inputs_hash: str
    receipt: AcceptanceReceipt

    def as_public_dict(self) -> dict[str, object]:
        return {
            "binding_ref": self.binding_ref,
            "subject_kind": self.subject_kind,
            "subject_ref": self.subject_ref,
            "hash": self.inputs_hash,
            "receipt": self.receipt.as_public_dict(),
        }


@dataclass(frozen=True)
class ExperimentIdentitySet:
    baseline_ref: str
    variant_ref: str
    evaluation_protocol_ref: str
    protocol_version_ref: str
    evaluation_ref: str
    variant_run_ref: str
    evaluation_attempt_ref: str

    def as_dict(self) -> dict[str, str]:
        return {
            "baseline_ref": self.baseline_ref,
            "variant_ref": self.variant_ref,
            "evaluation_protocol_ref": self.evaluation_protocol_ref,
            "protocol_version_ref": self.protocol_version_ref,
            "evaluation_ref": self.evaluation_ref,
            "variant_run_ref": self.variant_run_ref,
            "evaluation_attempt_ref": self.evaluation_attempt_ref,
        }


@dataclass(frozen=True)
class ExperimentDomainAdmission:
    intent: ExperimentIntentLike
    execution_request: "AcceptedExperimentExecutionRequest"
    identities: ExperimentIdentitySet
    variant_run_binding: AcceptedExperimentInputBinding
    evaluation_attempt_binding: AcceptedExperimentInputBinding
    required_metrics: tuple[str, ...]
    formal_measurement_status: Literal["not_attempted", "accepted", "rejected"]
    formal_rejection_code: str | None
    created_at: float


@dataclass(frozen=True)
class AcceptedExperimentExecutionRequest:
    execution_request_ref: str
    quest_ref: str
    definition_binding: AcceptedAssetBinding
    implementation_binding: AcceptedAssetBinding
    definition: dict[str, object]
    definition_hash: str
    receipt: AcceptanceReceipt

    def as_public_dict(self) -> dict[str, object]:
        return {
            "execution_request_ref": self.execution_request_ref,
            "quest_ref": self.quest_ref,
            "definition": {
                "asset_ref": self.definition_binding.asset_ref,
                "version_ref": self.definition_binding.version_ref,
                "content_hash": self.definition_binding.content_hash,
                "manifest_hash": self.definition_binding.manifest_hash,
                "receipt": self.definition_binding.receipt.as_public_dict(),
            },
            "implementation": {
                "asset_ref": self.implementation_binding.asset_ref,
                "version_ref": self.implementation_binding.version_ref,
                "content_hash": self.implementation_binding.content_hash,
                "manifest_hash": self.implementation_binding.manifest_hash,
                "receipt": self.implementation_binding.receipt.as_public_dict(),
            },
            "receipt": self.receipt.as_public_dict(),
        }


@dataclass(frozen=True)
class AcceptedExperimentAssetRole:
    role_ref: str
    subject_kind: str
    subject_ref: str
    role: str
    ordinal: int
    binding: AcceptedAssetBinding
    receipt: AcceptanceReceipt

    def as_public_dict(self) -> dict[str, object]:
        return {
            "role_ref": self.role_ref,
            "subject_kind": self.subject_kind,
            "subject_ref": self.subject_ref,
            "role": self.role,
            "ordinal": self.ordinal,
            "asset_ref": self.binding.asset_ref,
            "version_ref": self.binding.version_ref,
            "content_hash": self.binding.content_hash,
            "manifest_hash": self.binding.manifest_hash,
            "asset_receipt": self.binding.receipt.as_public_dict(),
            "receipt": self.receipt.as_public_dict(),
        }


@dataclass(frozen=True)
class FormalMetricResult:
    metric_result_ref: str
    evaluation_attempt_ref: str
    result_role_ref: str
    metrics: dict[str, float]
    metrics_hash: str
    receipt: AcceptanceReceipt

    def as_public_dict(self) -> dict[str, object]:
        return {
            "metric_result_ref": self.metric_result_ref,
            "evaluation_attempt_ref": self.evaluation_attempt_ref,
            "result_role_ref": self.result_role_ref,
            "metrics": self.metrics,
            "metrics_hash": self.metrics_hash,
            "receipt": self.receipt.as_public_dict(),
        }


@dataclass(frozen=True)
class ExperimentObservation:
    kind: Literal["stdout", "telemetry", "status"]
    payload: dict[str, object]
    observed_at: float


ExperimentObserver = Callable[[ExperimentObservation], None]


@dataclass(frozen=True)
class MaterializedExperimentCheckpoint:
    """One ordered, receipt-backed RM AssetVersion supplied to execution."""

    ordinal: int
    role_ref: str
    binding: AcceptedAssetBinding
    role_receipt: AcceptanceReceipt
    content: bytes

    @property
    def asset_ref(self) -> str:
        return self.binding.asset_ref

    @property
    def version_ref(self) -> str:
        return self.binding.version_ref

    @property
    def content_hash(self) -> str:
        return self.binding.content_hash

    def validate(self) -> None:
        if (
            isinstance(self.ordinal, bool)
            or self.ordinal < 0
            or not self.role_ref
            or len(self.role_ref) > 96
            or not self.content
            or len(self.content) > 8 * 1024 * 1024
            or hashlib.sha256(self.content).hexdigest() != self.binding.content_hash
            or self.role_receipt.issuer != "research_graph"
            or self.role_receipt.kind != "experiment_asset_role_acceptance"
            or self.role_receipt.subject_ref != self.role_ref
        ):
            raise OwnerConflict("experiment_checkpoint_materialization_invalid")

    def as_invocation_dict(self) -> dict[str, object]:
        self.validate()
        return {
            "ordinal": self.ordinal,
            "role_ref": self.role_ref,
            "asset": self.binding.as_dict(),
            "role_receipt": self.role_receipt.as_public_dict(),
            "content_base64": base64.b64encode(self.content).decode("ascii"),
        }


@dataclass(frozen=True)
class ExperimentProviderRequest:
    identities: ExperimentIdentitySet
    variant_run_binding: AcceptedExperimentInputBinding
    evaluation_attempt_binding: AcceptedExperimentInputBinding
    required_metrics: tuple[str, ...]
    provider_operation_ref: str = ""
    provider_operation_generation: int = 1
    request_kind: Literal["retrain", "new_variant", "remeasure"] = "retrain"
    selected_checkpoints: tuple[MaterializedExperimentCheckpoint, ...] = ()
    definition: dict[str, object] | None = None
    checkpoint_policy: Literal["forbidden", "optional", "required"] | None = None
    wall_time_budget_seconds: float = 300.0

    def validate(self) -> None:
        if (
            not self.provider_operation_ref
            or len(self.provider_operation_ref) > 128
            or isinstance(self.provider_operation_generation, bool)
            or self.provider_operation_generation < 1
        ):
            raise OwnerConflict("experiment_provider_operation_ref_invalid")
        if (
            isinstance(self.wall_time_budget_seconds, bool)
            or not isinstance(self.wall_time_budget_seconds, (int, float))
            or not math.isfinite(float(self.wall_time_budget_seconds))
            or not 1 <= float(self.wall_time_budget_seconds) <= 24 * 60 * 60
        ):
            raise OwnerConflict("experiment_wall_time_budget_invalid")
        if self.request_kind not in {"retrain", "new_variant", "remeasure"}:
            raise OwnerConflict("experiment_provider_request_invalid")
        if self.request_kind in {"retrain", "new_variant"} and self.selected_checkpoints:
            raise OwnerConflict("experiment_provider_request_invalid")
        policy = self.effective_checkpoint_policy
        if policy not in {"forbidden", "optional", "required"}:
            raise OwnerConflict("experiment_checkpoint_policy_invalid")
        if self.request_kind == "remeasure" and policy != "forbidden":
            raise OwnerConflict("experiment_checkpoint_policy_invalid")
        if self.definition is not None:
            canonical_hash(self.definition)
            intent_document = self.definition.get("intent")
            if (
                isinstance(intent_document, dict)
                and "wall_time_budget_seconds" in intent_document
                and (
                    not isinstance(
                        intent_document.get("wall_time_budget_seconds"),
                        (int, float),
                    )
                    or isinstance(
                        intent_document.get("wall_time_budget_seconds"), bool
                    )
                    or float(intent_document["wall_time_budget_seconds"])
                    != float(self.wall_time_budget_seconds)
                )
            ):
                raise OwnerConflict("experiment_wall_time_budget_invalid")
        if len(self.selected_checkpoints) > 32 or tuple(
            checkpoint.ordinal for checkpoint in self.selected_checkpoints
        ) != tuple(range(len(self.selected_checkpoints))):
            raise OwnerConflict("experiment_checkpoint_materialization_invalid")
        for checkpoint in self.selected_checkpoints:
            checkpoint.validate()

    @property
    def effective_checkpoint_policy(
        self,
    ) -> Literal["forbidden", "optional", "required"]:
        if self.checkpoint_policy is not None:
            return self.checkpoint_policy
        return "required" if self.request_kind == "retrain" else "forbidden"


@dataclass(frozen=True)
class ExperimentProviderResult:
    checkpoint_content: bytes | None
    analysis: dict[str, object]
    result_content: dict[str, object]
    adapter_kind: str
    additional_checkpoint_contents: tuple[bytes, ...] = ()
    schema_ref: str = EXPERIMENT_RESULT_SCHEMA

    def as_document(self) -> dict[str, object]:
        validate_experiment_provider_result(self)
        return {
            "schema_ref": self.schema_ref,
            "checkpoint_content_base64": (
                None
                if self.checkpoint_content is None
                else base64.b64encode(self.checkpoint_content).decode("ascii")
            ),
            "additional_checkpoint_contents_base64": [
                base64.b64encode(content).decode("ascii")
                for content in self.additional_checkpoint_contents
            ],
            "analysis": self.analysis,
            "result_content": self.result_content,
            "adapter_kind": self.adapter_kind,
        }

    @classmethod
    def from_document(cls, value: dict[str, object]) -> "ExperimentProviderResult":
        schema_ref = value.get("schema_ref")
        if schema_ref not in {
            EXPERIMENT_RESULT_SCHEMA,
            PROTOCOL_EXPERIMENT_RESULT_SCHEMA,
        }:
            raise OwnerConflict("experiment_result_invalid")
        encoded = value.get("checkpoint_content_base64")
        analysis = value.get("analysis")
        result_content = value.get("result_content")
        adapter_kind = value.get("adapter_kind")
        additional_encoded = value.get("additional_checkpoint_contents_base64", [])
        if (
            (encoded is not None and not isinstance(encoded, str))
            or not isinstance(analysis, dict)
            or not isinstance(result_content, dict)
            or not isinstance(adapter_kind, str)
            or not isinstance(additional_encoded, list)
            or any(not isinstance(item, str) for item in additional_encoded)
        ):
            raise OwnerConflict("experiment_result_invalid")
        try:
            checkpoint = (
                None if encoded is None else base64.b64decode(encoded, validate=True)
            )
            additional = tuple(
                base64.b64decode(item, validate=True) for item in additional_encoded
            )
        except ValueError as error:
            raise OwnerConflict("experiment_result_invalid") from error
        result = cls(
            checkpoint,
            analysis,
            result_content,
            adapter_kind,
            additional,
            cast(str, schema_ref),
        )
        validate_experiment_provider_result(result)
        return result


@dataclass(frozen=True)
class ExperimentResultComponentManifest:
    """Exact content hashes emitted by one fenced execution Attempt."""

    checkpoint_content_hashes: tuple[str, ...]
    analysis_content_hash: str
    result_content_hash: str
    log_content_hash: str
    observation_content_hash: str
    event_count: int
    schema_ref: str = EXPERIMENT_RESULT_COMPONENT_MANIFEST_SCHEMA

    def as_dict(self) -> dict[str, object]:
        value = {
            "schema_ref": self.schema_ref,
            "checkpoint_content_hashes": list(self.checkpoint_content_hashes),
            "analysis_content_hash": self.analysis_content_hash,
            "result_content_hash": self.result_content_hash,
            "log_content_hash": self.log_content_hash,
            "observation_content_hash": self.observation_content_hash,
            "event_count": self.event_count,
        }
        if (
            self.schema_ref != EXPERIMENT_RESULT_COMPONENT_MANIFEST_SCHEMA
            or len(self.checkpoint_content_hashes) > 32
            or any(len(value) != 64 for value in self.checkpoint_content_hashes)
            or len(self.analysis_content_hash) != 64
            or len(self.result_content_hash) != 64
            or len(self.log_content_hash) != 64
            or len(self.observation_content_hash) != 64
            or isinstance(self.event_count, bool)
            or self.event_count < 1
        ):
            raise OwnerConflict("experiment_result_component_manifest_invalid")
        return value


def experiment_execution_log_document(
    events: tuple[dict[str, object], ...],
) -> dict[str, object]:
    """Canonical full LogAsset document for one current execution fence."""

    stdout = [
        {
            "sequence": event["sequence"],
            "observed_at": event["observed_at"],
            **event["payload"],
        }
        for event in events
        if event["kind"] == "stdout"
    ]
    return {
        "stdout": stdout,
        "observation": {
            "mode": "raw_stdout",
            "complete": True,
            "truncated": False,
            "dropped": 0,
            "event_count": len(events),
            "stdout_count": len(stdout),
        },
    }


def experiment_result_component_manifest(
    result: ExperimentProviderResult,
    events: tuple[dict[str, object], ...],
) -> ExperimentResultComponentManifest:
    """Hash every durable result component covered by the AR receipt."""

    validate_experiment_provider_result(result)
    checkpoint_contents = (
        ()
        if result.checkpoint_content is None
        else (
            result.checkpoint_content,
            *result.additional_checkpoint_contents,
        )
    )
    manifest = ExperimentResultComponentManifest(
        checkpoint_content_hashes=tuple(
            hashlib.sha256(content).hexdigest() for content in checkpoint_contents
        ),
        analysis_content_hash=canonical_hash(result.analysis),
        result_content_hash=canonical_hash(result.result_content),
        log_content_hash=canonical_hash(experiment_execution_log_document(events)),
        observation_content_hash=canonical_hash(list(events)),
        event_count=len(events),
    )
    manifest.as_dict()
    return manifest


def validate_experiment_runtime_binding(binding: ExperimentRuntimeBinding) -> None:
    if (
        binding.schema_ref != EXPERIMENT_RUNTIME_BINDING_SCHEMA
        or len(binding.runner_bundle_hash) != 64
        or not binding.adapter_ref
        or not binding.interpreter_ref
        or not binding.capability_bindings
        or not binding.resource_bindings
        or tuple(sorted(set(binding.capability_bindings)))
        != binding.capability_bindings
        or tuple(sorted(set(binding.resource_bindings))) != binding.resource_bindings
    ):
        raise OwnerConflict("experiment_runtime_binding_invalid")


def experiment_definition_document(
    intent: ExperimentIntentLike, runtime_binding: ExperimentRuntimeBinding
) -> dict[str, object]:
    """Exact RM-owned content from which RG derives semantic identities."""

    intent_document = intent.as_dict()
    runtime_document = runtime_binding.as_dict()
    if isinstance(intent, ProtocolExperimentIntent):
        return {
            "schema_ref": PROTOCOL_EXPERIMENT_DEFINITION_SCHEMA,
            "intent": intent_document,
            "baseline_forward_contract": intent.baseline_forward_contract,
            "variant_recipe": intent.variant_recipe,
            "evaluation_protocol_lineage": intent.evaluation_protocol_lineage,
            "protocol_version": intent.protocol_version,
            "execution": intent.execution,
            "checkpoint_policy": intent.checkpoint_policy,
            "runtime_binding": runtime_document,
        }
    return {
        "schema_ref": "meta-research/micro-experiment-definition/v1",
        "intent": intent_document,
        "baseline_forward_contract": {
            "schema_ref": "meta-research/micro-baseline-forward-contract/v1",
            "input": "finite numeric vector",
            "operation": "identity numeric state",
            "output": "finite numeric vector",
        },
        "variant_recipe": {
            "schema_ref": "meta-research/micro-variant-recipe/v1",
            "training_data": {
                "generator": "centered deterministic sequence/v1",
                "sample_count": intent.sample_count,
            },
            "state_formation": {
                "operation": "additive shift",
                "variant_parameter": intent.variant_parameter,
            },
            "checkpoint_selection": "none required within same execution",
        },
        "evaluation_protocol_lineage": {
            "schema_ref": "meta-research/evaluation-protocol-lineage/v1",
            "name": "fixed-sample arithmetic-mean comparison",
        },
        "protocol_version": {
            "schema_ref": "meta-research/micro-evaluation-protocol/v1",
            "evaluation_data": {
                "generator": "centered deterministic sequence/v1",
                "sample_count": intent.sample_count,
            },
            "preprocessing": "none",
            "required_metrics": list(EXPERIMENT_REQUIRED_METRICS),
            "aggregation": "single fixed sample set; arithmetic mean",
            "stopping_rule": "complete fixed sample set",
        },
        "runtime_binding": runtime_document,
    }


def validate_experiment_provider_result(
    result: ExperimentProviderResult,
    *,
    request_kind: Literal["retrain", "new_variant", "remeasure"] | None = None,
    checkpoint_policy: Literal["forbidden", "optional", "required"] | None = None,
    required_metrics: tuple[str, ...] | None = None,
    result_schema_ref: str | None = None,
) -> None:
    if request_kind not in {None, "retrain", "new_variant", "remeasure"}:
        raise OwnerConflict("experiment_result_invalid")
    if checkpoint_policy is None:
        if request_kind == "retrain":
            checkpoint_policy = "required"
        elif request_kind in {"new_variant", "remeasure"}:
            checkpoint_policy = "forbidden"
    if checkpoint_policy not in {None, "forbidden", "optional", "required"}:
        raise OwnerConflict("experiment_checkpoint_policy_invalid")
    if result.checkpoint_content is None:
        if result.additional_checkpoint_contents:
            raise OwnerConflict("experiment_checkpoint_invalid")
        checkpoints: tuple[bytes, ...] = ()
    else:
        checkpoints = (
            result.checkpoint_content,
            *result.additional_checkpoint_contents,
        )
    if (
        (checkpoint_policy == "required" and not checkpoints)
        or (checkpoint_policy == "forbidden" and checkpoints)
        or len(checkpoints) > 32
        or any(
            not isinstance(content, bytes)
            or not content
            or len(content) > 8 * 1024 * 1024
            for content in checkpoints
        )
        or len(set(checkpoints)) != len(checkpoints)
    ):
        raise OwnerConflict("experiment_checkpoint_invalid")
    if (
        not result.adapter_kind
        or len(result.adapter_kind) > 128
        or result.schema_ref
        not in {EXPERIMENT_RESULT_SCHEMA, PROTOCOL_EXPERIMENT_RESULT_SCHEMA}
        or (result_schema_ref is not None and result.schema_ref != result_schema_ref)
    ):
        raise OwnerConflict("experiment_result_invalid")
    metrics = result.result_content.get("metrics")
    disposition = result.result_content.get("result_disposition")
    if (
        result.result_content.get("schema_ref") != result.schema_ref
        or not isinstance(metrics, dict)
        or (
            disposition is not None
            and disposition not in EXPERIMENT_RESULT_DISPOSITIONS
        )
    ):
        raise OwnerConflict("experiment_result_invalid")
    for name, value in metrics.items():
        if (
            not isinstance(name, str)
            or not name
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise OwnerConflict("experiment_metric_invalid")
    if required_metrics is not None and any(
        name not in metrics for name in required_metrics
    ):
        raise OwnerConflict("formal_measurement_metrics_incomplete")
    # Sign, magnitude, and significance deliberately do not gate validity.
    canonical_hash(result.analysis)
    canonical_hash(result.result_content)


def experiment_intent_from_document(value: dict[str, object]) -> ExperimentIntentLike:
    """Decode old receipt material exactly, while admitting versioned v2 intents."""

    try:
        if value.get("schema_ref") == PROTOCOL_EXPERIMENT_INTENT_SCHEMA:
            checkpoints = value["selected_checkpoint_role_refs"]
            documents = {
                name: value[name]
                for name in (
                    "baseline_forward_contract",
                    "variant_recipe",
                    "evaluation_protocol_lineage",
                    "protocol_version",
                    "execution",
                )
            }
            if not isinstance(checkpoints, list) or any(
                not isinstance(document, dict) for document in documents.values()
            ):
                raise TypeError("protocol experiment intent")
            intent: ExperimentIntentLike = ProtocolExperimentIntent(
                execution_request_ref=str(value["execution_request_ref"]),
                quest_ref=str(value["quest_ref"]),
                title=str(value["title"]),
                objective=str(value["objective"]),
                baseline_forward_contract=cast(
                    dict[str, object], documents["baseline_forward_contract"]
                ),
                variant_recipe=cast(dict[str, object], documents["variant_recipe"]),
                evaluation_protocol_lineage=cast(
                    dict[str, object], documents["evaluation_protocol_lineage"]
                ),
                protocol_version=cast(
                    dict[str, object], documents["protocol_version"]
                ),
                execution=cast(dict[str, object], documents["execution"]),
                checkpoint_policy=cast(
                    Literal["forbidden", "optional", "required"],
                    str(value["checkpoint_policy"]),
                ),
                request_kind=cast(
                    Literal["new_variant", "remeasure"],
                    str(value["request_kind"]),
                ),
                source_variant_run_ref=(
                    None
                    if value["source_variant_run_ref"] is None
                    else str(value["source_variant_run_ref"])
                ),
                selected_checkpoint_role_refs=tuple(
                    str(item) for item in checkpoints
                ),
            )
        else:
            checkpoints = value["selected_checkpoint_role_refs"]
            if not isinstance(checkpoints, list):
                raise TypeError("legacy experiment intent")
            intent = ExperimentIntent(
                execution_request_ref=str(value["execution_request_ref"]),
                quest_ref=str(value["quest_ref"]),
                title=str(value["title"]),
                hypothesis=str(value["hypothesis"]),
                variant_parameter=float(value["variant_parameter"]),
                sample_count=int(value["sample_count"]),
                wall_time_budget_seconds=float(
                    value.get("wall_time_budget_seconds", 300.0)
                ),
                request_kind=cast(
                    Literal["retrain", "remeasure"], str(value["request_kind"])
                ),
                source_variant_run_ref=(
                    None
                    if value["source_variant_run_ref"] is None
                    else str(value["source_variant_run_ref"])
                ),
                selected_checkpoint_role_refs=tuple(
                    str(item) for item in checkpoints
                ),
            )
        intent.validate()
        expected_document = intent.as_dict()
        # Before the budget became user-visible, the built-in provider's
        # documented 300-second value lived only in its runtime binding.
        # Preserve exact historical decoding while every new Web intent carries
        # the field explicitly.
        if "wall_time_budget_seconds" not in value and isinstance(
            intent, ExperimentIntent
        ):
            expected_document.pop("wall_time_budget_seconds", None)
        if expected_document != value:
            raise OwnerConflict("experiment_intent_invalid")
        return intent
    except (KeyError, TypeError, ValueError) as error:
        raise OwnerConflict("experiment_intent_invalid") from error


def experiment_required_metrics(intent: ExperimentIntentLike) -> tuple[str, ...]:
    if isinstance(intent, ProtocolExperimentIntent):
        return intent.required_metrics
    return EXPERIMENT_REQUIRED_METRICS


def experiment_optional_metrics(intent: ExperimentIntentLike) -> tuple[str, ...]:
    if isinstance(intent, ProtocolExperimentIntent):
        return intent.optional_metrics
    return ()


def experiment_checkpoint_policy(
    intent: ExperimentIntentLike,
) -> Literal["forbidden", "optional", "required"]:
    return intent.checkpoint_policy


def experiment_result_schema_ref(intent: ExperimentIntentLike) -> str:
    return intent.result_schema_ref


def experiment_forms_new_variant(intent: ExperimentIntentLike) -> bool:
    return intent.forms_new_variant
