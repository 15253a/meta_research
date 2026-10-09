from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from meta_research.owners.common import canonical_hash, decoded_object


GuidanceRootKind = Literal["idea", "plan", "bundle", "reasoning", "target"]
GUIDANCE_ROOT_KINDS = ("idea", "plan", "bundle", "reasoning", "target")
GUIDANCE_DOCUMENT_MAX_BYTES = 65536
GUIDANCE_OPERATION_IDS = (
    "human_guidance.read", "human_guidance.read.reconcile",
    "human_guidance.feedback", "human_guidance.feedback.reconcile",
)


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

    def as_dict(self) -> dict[str, object]:
        return {
            "delivery_ref": self.delivery_ref,
            "guide": decoded_object(self.guide_json),
            "needs_treatment": self.needs_treatment,
            "prior_treatment": None if self.prior_treatment_json is None
            else decoded_object(self.prior_treatment_json),
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


def guidance_prompt(cut: FrozenGuidanceCut, *, resident: bool = True) -> str:
    items = [{
        "delivery_ref": item.delivery_ref,
        "constraint_ref": decoded_object(item.guide_json)["constraint_ref"],
        "revision": decoded_object(item.guide_json)["revision"],
        "needs_treatment": item.needs_treatment,
    } for item in cut.deliveries]
    from meta_research.owners.common import canonical_json
    rendered = (
        "\n\n## 本次操作的正式人类指导\n"
        + canonical_json({"binding": cut.binding.as_dict(), "deliveries": items})
        + "\n用 human_guidance.read 列表发现本次冻结指导，再精确完整读取各 delivery_ref。"
        "列表与此索引不证明已读原文。needs_treatment=true 的指导在本工作中尚需说明处理，"
        "完整读后用 human_guidance.feedback 说明理解、调整、继续事项及理由。"
        "needs_treatment=false 的指导仍是持续约束，请参考既有反馈，勿重复声明处理。"
        "本次操作之后提交的指导由下一逻辑操作接续。力度5的目标更新仍待目标演化流程，"
        "收到、读取或反馈不表示目标已切换。HumanRequest 答复仍归原请求根。\n"
    )
    if not resident:
        rendered += "\n冻结原文如下。工具通道未建立时，不记录收到、读取或反馈事实。\n" + canonical_json(cut.as_dict())
    return rendered
