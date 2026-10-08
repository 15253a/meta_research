from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from meta_research.owners.common import OwnerConflict, canonical_hash


GOAL_OPERATION_IDS = (
    "research_graph.quest_goal.read",
    "research_graph.quest_goal.evolve",
    "research_graph.quest_goal.evolve.reconcile",
)
GOAL_ROOT_KINDS = frozenset({"idea", "plan", "bundle", "target", "reasoning"})
GOAL_REVISION_RECEIPT_KIND = "quest_goal_evolution_accepted"
GOAL_DIRECTION_SCHEMA_REF = "meta-research/frozen-direction-cut/v1"


@dataclass(frozen=True, slots=True)
class ResearchRoot:
    kind: str
    run_ref: str
    attempt_ref: str
    root_session_ref: str
    fence_ref: str
    runtime_binding_hash: str
    operation_ref: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class WorkHandle:
    work_ref: str
    kind: Literal["stage", "target"]
    cycle_ref: str
    run_ref: str | None
    attempt_ref: str | None
    lifecycle_generation: int | None
    state_revision: int
    frozen_contract_ref: str
    state: str
    target_ref: str | None = None
    workspace_ref: str | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class WorkDecision:
    kind: Literal["continue", "stop", "do_not_start", "finish_stage_boundary"]
    work: WorkHandle
    reason: str
    retention: dict[str, object] | None = None

    def as_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "kind": self.kind,
            "work": self.work.as_dict(),
            "reason": self.reason,
        }
        if self.retention is not None:
            value["retention"] = self.retention
        return value


@dataclass(frozen=True, slots=True)
class EvolutionDecision:
    expected_revision: str
    conditions_basis: str
    work_basis: str
    cause: dict[str, object]
    replacement: dict[str, str]
    criteria_review: str
    judgment: str
    conditions: dict[str, object]
    arrangements: tuple[WorkDecision, ...]
    following_direction: str

    def as_dict(self) -> dict[str, object]:
        return {
            "expected_revision": self.expected_revision,
            "conditions_basis": self.conditions_basis,
            "work_basis": self.work_basis,
            "cause": self.cause,
            "replacement": self.replacement,
            "criteria_review": self.criteria_review,
            "judgment": self.judgment,
            "conditions": self.conditions,
            "arrangements": {
                "decisions": [item.as_dict() for item in self.arrangements]
            },
            "following_direction": self.following_direction,
        }


def parse_research_root(value: dict[str, object]) -> ResearchRoot:
    required = {
        "kind",
        "run_ref",
        "attempt_ref",
        "root_session_ref",
        "fence_ref",
        "runtime_binding_hash",
        "operation_ref",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise OwnerConflict("goal_author_invalid")
    if value.get("kind") not in GOAL_ROOT_KINDS or any(
        not isinstance(value.get(name), str)
        or not str(value[name])
        or len(str(value[name])) > 256
        for name in required - {"kind"}
    ):
        raise OwnerConflict("goal_author_invalid")
    return ResearchRoot(**value)


def parse_evolution_decision(value: object) -> EvolutionDecision:
    required = {
        "expected_revision",
        "conditions_basis",
        "work_basis",
        "cause",
        "replacement",
        "criteria_review",
        "judgment",
        "conditions",
        "arrangements",
        "following_direction",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise OwnerConflict("goal_evolution_decision_invalid")
    for name in (
        "expected_revision",
        "conditions_basis",
        "work_basis",
        "criteria_review",
        "judgment",
        "following_direction",
    ):
        _bounded_text(value.get(name), name, maximum=16_000)
    replacement = value.get("replacement")
    if not isinstance(replacement, dict) or set(replacement) != {
        "goal",
        "completion_criteria",
    }:
        raise OwnerConflict("goal_evolution_semantics_invalid")
    for name in ("goal", "completion_criteria"):
        _bounded_text(replacement.get(name), name, maximum=24_000)
    cause = _parse_cause(value.get("cause"))
    conditions = _parse_conditions(value.get("conditions"))
    arrangements_value = value.get("arrangements")
    if (
        not isinstance(arrangements_value, dict)
        or set(arrangements_value) != {"decisions"}
        or not isinstance(arrangements_value.get("decisions"), list)
    ):
        raise OwnerConflict("goal_work_arrangements_invalid")
    arrangements = tuple(
        _parse_work_decision(item) for item in arrangements_value["decisions"]
    )
    if not arrangements or len(arrangements) > 512:
        raise OwnerConflict("goal_work_arrangements_invalid")
    refs = [item.work.work_ref for item in arrangements]
    if len(refs) != len(set(refs)):
        raise OwnerConflict("goal_work_arrangements_invalid")
    return EvolutionDecision(
        expected_revision=str(value["expected_revision"]),
        conditions_basis=str(value["conditions_basis"]),
        work_basis=str(value["work_basis"]),
        cause=cause,
        replacement={
            "goal": str(replacement["goal"]),
            "completion_criteria": str(replacement["completion_criteria"]),
        },
        criteria_review=str(value["criteria_review"]),
        judgment=str(value["judgment"]),
        conditions=conditions,
        arrangements=arrangements,
        following_direction=str(value["following_direction"]),
    )


def work_basis(items: tuple[WorkHandle, ...]) -> dict[str, object]:
    values = [item.as_dict() for item in items]
    return {
        "basis_ref": "goal_work_basis_" + canonical_hash(values)[:40],
        "items": values,
    }


def evolution_input_schema() -> dict[str, object]:
    text = {"type": "string", "minLength": 1, "maxLength": 16000}
    reference = {"type": "string", "minLength": 1, "maxLength": 256}
    receipt = {
        "type": "object",
        "properties": {
            **{
                name: reference
                for name in (
                    "issuer",
                    "kind",
                    "receipt_ref",
                    "subject_ref",
                    "payload_hash",
                )
            },
            "status": {"type": "string", "enum": ["accepted"]},
        },
        "required": [
            "status",
            "issuer",
            "kind",
            "receipt_ref",
            "subject_ref",
            "payload_hash",
        ],
        "additionalProperties": False,
    }
    work = {
        "type": "object",
        "properties": {
            "work_ref": reference,
            "kind": {"type": "string", "enum": ["stage", "target"]},
            "cycle_ref": reference,
            "run_ref": {"type": ["string", "null"], "maxLength": 256},
            "attempt_ref": {"type": ["string", "null"], "maxLength": 256},
            "lifecycle_generation": {"type": ["integer", "null"], "minimum": 1},
            "state_revision": {"type": "integer", "minimum": 0},
            "frozen_contract_ref": reference,
            "state": reference,
            "target_ref": {"type": ["string", "null"], "maxLength": 256},
            "workspace_ref": {"type": ["string", "null"], "maxLength": 256},
        },
        "required": [
            "work_ref",
            "kind",
            "cycle_ref",
            "run_ref",
            "attempt_ref",
            "lifecycle_generation",
            "state_revision",
            "frozen_contract_ref",
            "state",
            "target_ref",
            "workspace_ref",
        ],
        "additionalProperties": False,
    }
    asset = {
        "type": "object",
        "properties": {
            "asset_ref": reference,
            "version_ref": reference,
            "content_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "manifest_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "receipt": receipt,
            "meaning": text,
        },
        "required": [
            "asset_ref",
            "version_ref",
            "content_hash",
            "manifest_hash",
            "receipt",
            "meaning",
        ],
        "additionalProperties": False,
    }
    retention = {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["selected", "pending", "inspected_none"],
            },
            "assets": {
                "type": "array",
                "minItems": 1,
                "maxItems": 128,
                "items": asset,
            },
            "workspace_ref": reference,
            "remaining_review": text,
            "explanation": text,
        },
        "additionalProperties": False,
        "oneOf": [
            {
                "type": "object",
                "properties": {
                    "kind": {"const": "selected"},
                    "assets": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 128,
                        "items": asset,
                    },
                },
                "required": ["kind", "assets"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "kind": {"const": "pending"},
                    "workspace_ref": reference,
                    "remaining_review": text,
                },
                "required": ["kind", "workspace_ref", "remaining_review"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "kind": {"const": "inspected_none"},
                    "workspace_ref": reference,
                    "explanation": text,
                },
                "required": ["kind", "workspace_ref", "explanation"],
                "additionalProperties": False,
            },
        ]
    }
    decision = {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": [
                    "continue",
                    "stop",
                    "do_not_start",
                    "finish_stage_boundary",
                ],
            },
            "work": work,
            "reason": text,
            "retention": retention,
        },
        "required": ["kind", "work", "reason"],
        "additionalProperties": False,
        "allOf": [
            {
                "if": {"properties": {"kind": {"const": "stop"}}},
                "then": {"required": ["retention"]},
                "else": {"not": {"required": ["retention"]}},
            }
        ],
    }
    guide_ref = {
        "type": "object",
        "properties": {
            "constraint_ref": reference,
            "revision": {"type": "integer", "minimum": 1},
            "guidance_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "receipt_ref": reference,
            "receipt_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        },
        "required": [
            "constraint_ref",
            "revision",
            "guidance_hash",
            "receipt_ref",
            "receipt_hash",
        ],
        "additionalProperties": False,
    }
    cause = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["human_guidance", "evidence"]},
            "delivery_ref": reference,
            "guide_ref": guide_ref,
            "evidence": {
                "type": "array",
                "minItems": 1,
                "maxItems": 64,
                "items": {
                    "type": "object",
                    "properties": {
                        "source_ref": reference,
                        "version_ref": reference,
                    },
                    "required": ["source_ref", "version_ref"],
                    "additionalProperties": False,
                },
            },
        },
        "additionalProperties": False,
        "oneOf": [
            {
                "type": "object",
                "properties": {
                    "kind": {"const": "human_guidance"},
                    "delivery_ref": reference,
                    "guide_ref": guide_ref,
                },
                "required": ["kind", "delivery_ref", "guide_ref"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "kind": {"const": "evidence"},
                    "evidence": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 64,
                        "items": {
                            "type": "object",
                            "properties": {
                                "source_ref": reference,
                                "version_ref": reference,
                            },
                            "required": ["source_ref", "version_ref"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["kind", "evidence"],
                "additionalProperties": False,
            },
        ]
    }
    conditions = {
        "type": "object",
        "properties": {
            "runtime_conditions_ref": reference,
            "assessments": {
                "type": "array",
                "maxItems": 128,
                "items": {
                    "type": "object",
                    "properties": {
                        "condition_ref": reference,
                        "disposition": {
                            "type": "string",
                            "enum": ["preserved", "human_superseded"],
                        },
                        "explanation": text,
                        "superseding_delivery_ref": reference,
                    },
                    "required": [
                        "condition_ref",
                        "disposition",
                        "explanation",
                    ],
                    "additionalProperties": False,
                },
            },
            "newly_identified": {
                "type": "array",
                "maxItems": 128,
                "items": {
                    "type": "object",
                    "properties": {
                        "delivery_ref": reference,
                        "start": {"type": "integer", "minimum": 0},
                        "end": {"type": "integer", "minimum": 1},
                        "text": text,
                        "meaning": text,
                    },
                    "required": ["delivery_ref", "start", "end", "text", "meaning"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["runtime_conditions_ref", "assessments", "newly_identified"],
        "additionalProperties": False,
    }
    decision_schema = {
        "type": "object",
        "properties": {
            "expected_revision": reference,
            "conditions_basis": reference,
            "work_basis": reference,
            "cause": cause,
            "replacement": {
                "type": "object",
                "properties": {"goal": text, "completion_criteria": text},
                "required": ["goal", "completion_criteria"],
                "additionalProperties": False,
            },
            "criteria_review": text,
            "judgment": text,
            "conditions": conditions,
            "arrangements": {
                "type": "object",
                "properties": {
                    "decisions": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 512,
                        "items": decision,
                    }
                },
                "required": ["decisions"],
                "additionalProperties": False,
            },
            "following_direction": text,
        },
        "required": sorted(
            {
                "expected_revision",
                "conditions_basis",
                "work_basis",
                "cause",
                "replacement",
                "criteria_review",
                "judgment",
                "conditions",
                "arrangements",
                "following_direction",
            }
        ),
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "effect_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "decision": decision_schema,
        },
        "required": ["effect_id", "decision"],
        "additionalProperties": False,
    }


def _parse_cause(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise OwnerConflict("goal_evolution_cause_invalid")
    kind = value.get("kind")
    if kind == "human_guidance":
        if set(value) != {"kind", "delivery_ref", "guide_ref"}:
            raise OwnerConflict("goal_evolution_cause_invalid")
        _bounded_text(value.get("delivery_ref"), "delivery_ref", maximum=256)
        guide = value.get("guide_ref")
        required = {
            "constraint_ref",
            "revision",
            "guidance_hash",
            "receipt_ref",
            "receipt_hash",
        }
        if not isinstance(guide, dict) or set(guide) != required:
            raise OwnerConflict("goal_evolution_cause_invalid")
        if type(guide.get("revision")) is not int or int(guide["revision"]) < 1:
            raise OwnerConflict("goal_evolution_cause_invalid")
        for name in required - {"revision"}:
            _bounded_text(guide.get(name), name, maximum=256)
        for name in ("guidance_hash", "receipt_hash"):
            if len(str(guide[name])) != 64:
                raise OwnerConflict("goal_evolution_cause_invalid")
        return {"kind": kind, "delivery_ref": value["delivery_ref"], "guide_ref": dict(guide)}
    if kind == "evidence":
        if set(value) != {"kind", "evidence"} or not isinstance(value.get("evidence"), list):
            raise OwnerConflict("goal_evolution_cause_invalid")
        evidence = value["evidence"]
        if not evidence or len(evidence) > 64:
            raise OwnerConflict("goal_evolution_cause_invalid")
        normalized = []
        for item in evidence:
            if not isinstance(item, dict) or set(item) != {"source_ref", "version_ref"}:
                raise OwnerConflict("goal_evolution_cause_invalid")
            for name in ("source_ref", "version_ref"):
                _bounded_text(item.get(name), name, maximum=1024)
            normalized.append(dict(item))
        if len({(item["source_ref"], item["version_ref"]) for item in normalized}) != len(normalized):
            raise OwnerConflict("goal_evolution_cause_invalid")
        return {"kind": kind, "evidence": normalized}
    raise OwnerConflict("goal_evolution_cause_invalid")


def _parse_conditions(value: object) -> dict[str, object]:
    required = {"runtime_conditions_ref", "assessments", "newly_identified"}
    if not isinstance(value, dict) or set(value) != required:
        raise OwnerConflict("goal_conditions_review_invalid")
    _bounded_text(value.get("runtime_conditions_ref"), "runtime_conditions_ref", maximum=256)
    assessments = value.get("assessments")
    clauses = value.get("newly_identified")
    if not isinstance(assessments, list) or not isinstance(clauses, list) or len(assessments) > 128 or len(clauses) > 128:
        raise OwnerConflict("goal_conditions_review_invalid")
    normalized_assessments = []
    for item in assessments:
        if not isinstance(item, dict) or set(item) not in (
            {"condition_ref", "disposition", "explanation"},
            {"condition_ref", "disposition", "explanation", "superseding_delivery_ref"},
        ):
            raise OwnerConflict("goal_conditions_review_invalid")
        if item.get("disposition") not in {"preserved", "human_superseded"}:
            raise OwnerConflict("goal_conditions_review_invalid")
        _bounded_text(item.get("condition_ref"), "condition_ref", maximum=256)
        _bounded_text(item.get("explanation"), "explanation", maximum=16_000)
        supplied = item.get("superseding_delivery_ref")
        if (item["disposition"] == "human_superseded") != (supplied is not None):
            raise OwnerConflict("goal_conditions_review_invalid")
        if supplied is not None:
            _bounded_text(supplied, "superseding_delivery_ref", maximum=256)
        normalized_assessments.append(dict(item))
    if len({item["condition_ref"] for item in normalized_assessments}) != len(normalized_assessments):
        raise OwnerConflict("goal_conditions_review_invalid")
    normalized_clauses = []
    for item in clauses:
        required_clause = {"delivery_ref", "start", "end", "text", "meaning"}
        if not isinstance(item, dict) or set(item) != required_clause:
            raise OwnerConflict("goal_conditions_review_invalid")
        if type(item.get("start")) is not int or type(item.get("end")) is not int or item["start"] < 0 or item["end"] <= item["start"]:
            raise OwnerConflict("goal_conditions_review_invalid")
        for name in ("delivery_ref", "text", "meaning"):
            _bounded_text(item.get(name), name, maximum=16_000)
        normalized_clauses.append(dict(item))
    return {
        "runtime_conditions_ref": value["runtime_conditions_ref"],
        "assessments": normalized_assessments,
        "newly_identified": normalized_clauses,
    }


def _parse_work_decision(value: object) -> WorkDecision:
    if not isinstance(value, dict) or set(value) not in (
        {"kind", "work", "reason"},
        {"kind", "work", "reason", "retention"},
    ):
        raise OwnerConflict("goal_work_decision_invalid")
    kind = value.get("kind")
    if kind not in {"continue", "stop", "do_not_start", "finish_stage_boundary"}:
        raise OwnerConflict("goal_work_decision_invalid")
    _bounded_text(value.get("reason"), "reason", maximum=16_000)
    work = _parse_work_handle(value.get("work"))
    retention = value.get("retention")
    if kind == "stop":
        retention = _parse_retention(retention)
    elif retention is not None:
        raise OwnerConflict("goal_work_decision_invalid")
    if (
        (
            kind == "stop"
            and (
                work.kind != "target"
                or work.run_ref is None
                or work.state not in {"running", "cancel_pending"}
            )
        )
        or (
            kind == "do_not_start"
            and (work.kind != "target" or work.state != "queued")
        )
        or (kind == "finish_stage_boundary" and work.kind != "stage")
    ):
        raise OwnerConflict("goal_work_decision_invalid")
    return WorkDecision(kind, work, str(value["reason"]), retention)


def _parse_work_handle(value: object) -> WorkHandle:
    required = set(WorkHandle.__dataclass_fields__)
    if not isinstance(value, dict) or set(value) != required:
        raise OwnerConflict("goal_work_handle_invalid")
    if value.get("kind") not in {"stage", "target"}:
        raise OwnerConflict("goal_work_handle_invalid")
    for name in (
        "work_ref",
        "cycle_ref",
        "frozen_contract_ref",
        "state",
    ):
        _bounded_text(value.get(name), name, maximum=256)
    for name in ("run_ref", "attempt_ref", "target_ref", "workspace_ref"):
        if value.get(name) is not None:
            _bounded_text(value.get(name), name, maximum=256)
    if type(value.get("state_revision")) is not int or value["state_revision"] < 0:
        raise OwnerConflict("goal_work_handle_invalid")
    if value.get("lifecycle_generation") is not None and (
        type(value["lifecycle_generation"]) is not int or value["lifecycle_generation"] < 1
    ):
        raise OwnerConflict("goal_work_handle_invalid")
    return WorkHandle(**value)


def _parse_retention(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise OwnerConflict("goal_retention_invalid")
    kind = value.get("kind")
    if kind == "selected":
        if set(value) != {"kind", "assets"} or not isinstance(value.get("assets"), list):
            raise OwnerConflict("goal_retention_invalid")
        assets = value["assets"]
        if not assets or len(assets) > 128:
            raise OwnerConflict("goal_retention_invalid")
        normalized = []
        required = {
            "asset_ref",
            "version_ref",
            "content_hash",
            "manifest_hash",
            "receipt",
            "meaning",
        }
        for asset in assets:
            if not isinstance(asset, dict) or set(asset) != required:
                raise OwnerConflict("goal_retention_invalid")
            for name in required - {"receipt"}:
                _bounded_text(asset.get(name), name, maximum=16_000)
            if len(asset["content_hash"]) != 64 or len(asset["manifest_hash"]) != 64:
                raise OwnerConflict("goal_retention_invalid")
            receipt = asset.get("receipt")
            receipt_fields = {"issuer", "kind", "receipt_ref", "subject_ref", "payload_hash", "status"}
            if not isinstance(receipt, dict) or set(receipt) != receipt_fields:
                raise OwnerConflict("goal_retention_invalid")
            if receipt.get("status") != "accepted":
                raise OwnerConflict("goal_retention_invalid")
            for name in receipt_fields - {"status"}:
                _bounded_text(receipt.get(name), name, maximum=256)
            normalized.append(dict(asset))
        if len({item["version_ref"] for item in normalized}) != len(normalized):
            raise OwnerConflict("goal_retention_invalid")
        return {"kind": kind, "assets": normalized}
    if kind == "pending":
        if set(value) != {"kind", "workspace_ref", "remaining_review"}:
            raise OwnerConflict("goal_retention_invalid")
        _bounded_text(value.get("workspace_ref"), "workspace_ref", maximum=256)
        _bounded_text(value.get("remaining_review"), "remaining_review", maximum=16_000)
        return dict(value)
    if kind == "inspected_none":
        if set(value) != {"kind", "workspace_ref", "explanation"}:
            raise OwnerConflict("goal_retention_invalid")
        _bounded_text(value.get("workspace_ref"), "workspace_ref", maximum=256)
        _bounded_text(value.get("explanation"), "explanation", maximum=16_000)
        return dict(value)
    raise OwnerConflict("goal_retention_invalid")


def _bounded_text(value: object, _name: str, *, maximum: int) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise OwnerConflict("goal_evolution_decision_invalid")
