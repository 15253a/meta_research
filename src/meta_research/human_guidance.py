from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from typing import Literal

from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json, decoded_object


GuidanceRootKind = Literal["idea", "plan", "bundle", "reasoning", "target"]
GUIDANCE_ROOT_KINDS = ("idea", "plan", "bundle", "reasoning", "target")
GUIDANCE_DOCUMENT_MAX_BYTES = 65536
GUIDANCE_OPERATION_IDS = (
    "human_guidance.read", "human_guidance.read.reconcile",
    "human_guidance.feedback", "human_guidance.feedback.reconcile",
)


def normalize_guidance_scope(scope: object, *, quest_ref: str) -> dict[str, object]:
    fields = {
        "quest": {"kind", "quest_ref"},
        "question": {"kind", "quest_ref", "question_ref"},
        "cycle": {"kind", "quest_ref", "question_ref", "cycle_ref"},
        "target": {"kind", "quest_ref", "question_ref", "cycle_ref", "target_ref"},
    }
    if (not isinstance(scope, dict) or not isinstance(scope.get("kind"), str)
            or scope["kind"] not in fields or set(scope) != fields[scope["kind"]]
            or scope.get("quest_ref") != quest_ref
            or any(not isinstance(value, str) or not value or len(value) > 256
                for value in scope.values())):
        raise OwnerConflict("guidance_scope_invalid")
    return dict(scope)


@dataclass(frozen=True)
class GuidanceOperationIdentity:
    root_kind: GuidanceRootKind
    run_ref: str
    operation_ref: str


@dataclass(frozen=True)
class GuidanceRuntimeScope:
    root_kind: GuidanceRootKind
    run_ref: str
    attempt_ref: str
    root_session_ref: str
    fence_ref: str
    runtime_binding_hash: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class StageGuidanceOperation:
    scope: GuidanceRuntimeScope
    job_ref: str
    operation_name: str
    unit_ref: str | None = None

    @property
    def identity(self) -> GuidanceOperationIdentity:
        return GuidanceOperationIdentity(
            self.scope.root_kind, self.scope.run_ref,
            "guidance_op_" + canonical_hash({
                "run_ref": self.scope.run_ref, "job_ref": self.job_ref,
                "operation_name": self.operation_name,
            }),
        )


@dataclass(frozen=True)
class TargetGuidanceOperation:
    scope: GuidanceRuntimeScope
    operation_ref: str
    generation: int
    resume: bool

    @property
    def identity(self) -> GuidanceOperationIdentity:
        return GuidanceOperationIdentity("target", self.scope.run_ref, self.operation_ref)


@dataclass(frozen=True)
class VerifiedGuidanceWork:
    root_kind: GuidanceRootKind
    run_ref: str
    quest_ref: str
    provenance_ref: str


@dataclass(frozen=True)
class FrozenGuidanceBinding:
    identity: GuidanceOperationIdentity
    quest_ref: str
    snapshot_ref: str
    snapshot_hash: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class FrozenGuidanceDelivery:
    delivery_ref: str
    guide_json: str
    needs_treatment: bool
    prior_treatment_json: str | None
    applies_to_work: bool = True
    scope_confirmation: str = "legacy_unconfirmed"
    guidance_status: str = "active"

    def as_dict(self) -> dict[str, object]:
        return {
            "delivery_ref": self.delivery_ref,
            "guide": decoded_object(self.guide_json),
            "needs_treatment": self.needs_treatment,
            "prior_treatment": None if self.prior_treatment_json is None
            else decoded_object(self.prior_treatment_json),
            "applies_to_work": self.applies_to_work,
            "scope_confirmation": self.scope_confirmation,
            "guidance_status": self.guidance_status,
        }


@dataclass(frozen=True)
class FrozenGuidanceCut:
    binding: FrozenGuidanceBinding
    provenance_ref: str
    deliveries: tuple[FrozenGuidanceDelivery, ...]
    direction_cut: dict[str, object] | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "binding": self.binding.as_dict(), "provenance_ref": self.provenance_ref,
            "deliveries": [item.as_dict() for item in self.deliveries],
            **({} if self.direction_cut is None else {"direction_cut": self.direction_cut}),
        }


def guidance_prompt(cut: FrozenGuidanceCut, *, resident: bool = True,
                    include_body: bool = False, include_direction: bool = False,
                    previous_prompts: tuple[str, ...] = ()) -> str:
    seen = human_input_identities(previous_prompts)
    events = [_guidance_event(decoded_object(item.guide_json),
                  item.guidance_status if item.applies_to_work else "outside_scope",
                  [item.delivery_ref], scope_confirmation=item.scope_confirmation)
              for item in cut.deliveries]
    rendered = ""
    for item, event in zip(cut.deliveries, events, strict=True):
        if event["status"] == "outside_scope" and not any(
                identity[0] == "guidance" and identity[1] == event["constraint_ref"] for identity in seen):
            continue
        if _human_input_identity(event) not in seen:
            rendered += "\n" + _human_event_prompt(event)
        elif (item.needs_treatment and item.applies_to_work
                and not any(canonical_json(item.delivery_ref) in prompt for prompt in previous_prompts)):
            rendered += "\n待处理指导的本轮反馈关联：\ncurrent_guidance_delivery=" + canonical_json({
                "constraint_ref": event["constraint_ref"], "revision": event["revision"],
                "delivery_ref": item.delivery_ref,
            })
    if cut.direction_cut is not None:
        prior_directions = [
            line
            for prompt in previous_prompts
            for line in prompt.splitlines()
            if line.startswith("current_goal_and_conditions=")
        ]
        goal_evolved = False
        if prior_directions:
            try:
                last_payload = json.loads(
                    prior_directions[-1].removeprefix("current_goal_and_conditions=")
                )
                goal_evolved = (
                    isinstance(last_payload, dict)
                    and last_payload.get("goal") != cut.direction_cut.get("goal")
                )
            except ValueError:
                goal_evolved = False
        if include_direction or include_body or not resident or goal_evolved:
            direction = "current_goal_and_conditions=" + canonical_json({
                "goal": cut.direction_cut["goal"], "conditions": cut.direction_cut["conditions"],
            })
            if not prior_directions or direction != prior_directions[-1]:
                rendered += "\n" + direction
    return rendered


def live_guidance_prompt(message: dict[str, object]) -> str:
    if message["kind"] == "human_request_response":
        return _human_event_prompt(message)
    change = message["change"]
    return _human_event_prompt(_guidance_event(change["guide"], change["status"],
        [item["delivery_ref"] for item in message["deliveries"]],
        scope_confirmation=next((item["scope_confirmation"] for item in message["deliveries"]
            if "scope_confirmation" in item), None)))


def _guidance_event(guide, status, delivery_refs, *, scope_confirmation=None):
    guidance = guide["guidance"]
    if not isinstance(guidance, dict):
        raise ValueError("human_guidance_body_invalid")
    event = {
        "kind": "confirmed_guidance_change",
        "constraint_ref": guide["constraint_ref"],
        "revision": guide["revision"],
        "status": status,
        "scope_confirmation": scope_confirmation or (
            "confirmed" if guidance.get("semantic_scope") is not None else "legacy_unconfirmed"),
        **({"semantic_scope": guidance["semantic_scope"]} if "semantic_scope" in guidance else {}),
    }
    if status == "active":
        event.update({field: guidance[field] for field in (
            "text", "strength", "applies_to", "preserve_conditions", "work_materials",
        ) if field in guidance})
        event["delivery_refs"] = delivery_refs
    return event


def _human_event_prompt(event):
    event = {name: value for name, value in event.items() if name != "prepared_at"}
    if event.get("status") == "outside_scope":
        instruction = "此指导已退出本会话适用范围，不再作为本工作要求。"
    elif event.get("status") == "withdrawn":
        instruction = "此指导已撤回。撤回本身不要求停止实验、删除成果或回滚目标。"
    else:
        instruction = "本次人类输入如下。"
    return instruction + "\nhuman_input=" + canonical_json(event)


def _human_input_identity(event):
    if event.get("kind") == "human_request_response":
        if not all(isinstance(event.get(name), str) and event[name]
                   for name in ("request_ref", "response_ref")):
            raise ValueError("human_input_identity_invalid")
        return ("response", event["request_ref"], event["response_ref"])
    if (event.get("kind") != "confirmed_guidance_change"
            or not isinstance(event.get("constraint_ref"), str)
            or type(event.get("revision")) is not int
            or event.get("status") not in {"active", "withdrawn", "outside_scope", "superseded"}):
        raise ValueError("human_input_identity_invalid")
    return ("guidance", event["constraint_ref"], event["revision"], event["status"])


def human_input_identities(prompts):
    import json

    identities = set()
    for prompt in prompts:
        for line in prompt.splitlines():
            marked = line.startswith("human_input=")
            if not marked and not line.startswith("{"):
                continue
            try:
                value = json.loads(line.removeprefix("human_input=") if marked else line)
                if not isinstance(value, dict):
                    continue
                if marked:
                    identities.add(_human_input_identity(value))
                elif value.get("kind") == "confirmed_guidance_change" and isinstance(value.get("change"), dict):
                    change = value["change"]
                    identities.add(_human_input_identity(_guidance_event(change["guide"], change["status"], [])))
                elif "binding" in value and isinstance(value.get("deliveries"), list):
                    for item in value["deliveries"]:
                        if isinstance(item, dict) and "guide" in item:
                            identities.add(_human_input_identity(_guidance_event(item["guide"],
                                item.get("guidance_status", "active") if item.get("applies_to_work", True)
                                else "outside_scope", [])))
            except (KeyError, TypeError, ValueError):
                continue
    return identities


def human_response_prompt(authority, scope, previous_prompts=()):
    if authority is None:
        return ""
    seen = human_input_identities(previous_prompts)
    return "".join("\n" + _human_event_prompt(event)
        for event in authority.human_response_events(scope=scope)
        if _human_input_identity(event) not in seen)
