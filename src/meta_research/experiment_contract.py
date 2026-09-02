from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast

from meta_research.owners.common import (
    AcceptedAssetBinding,
    AcceptanceReceipt,
    OwnerConflict,
    canonical_hash,
)


EXPERIMENT_INPUT_BINDING_SCHEMA = "meta-research/experiment-input-binding/v1"
PROTOCOL_EXPERIMENT_INTENT_SCHEMA = "meta-research/protocol-experiment-intent/v2"
PROTOCOL_EXPERIMENT_DEFINITION_SCHEMA = (
    "meta-research/protocol-experiment-definition/v2"
)
PROTOCOL_EXPERIMENT_RESULT_SCHEMA = "meta-research/protocol-experiment-result/v2"
EXPERIMENT_RESULT_DISPOSITIONS = frozenset(
    {"positive", "negative", "zero", "nonsignificant", "denied", "uncertain"}
)


@dataclass(frozen=True)
class ProtocolExperimentIntent:
    """Provider-neutral Target state-formation and measurement request."""

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
