#!/usr/bin/env python3
"""Idea Stage 确定性契约 fixture。

本模块以明确无生产权威的 fixture ref 与 fake port 演示语义路由；它不是
生产 schema、State Owner、transport 或候选生成算法。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import asdict, dataclass, field, is_dataclass, replace
from typing import Any, Dict, List, Optional, Protocol, Tuple, Union


IDEA_CONTRACT = "meta-research.idea-stage.fixture.v1"
HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
FIXTURE_REF_RE = re.compile(r"^fixture:[A-Za-z0-9._:/-]+$")
OWNER_STATUSES = {
    "accepted",
    "rejected",
    "stale",
    "needs_input",
    "outcome_unknown",
}
REVIEW_CATEGORIES = {
    "question_alignment",
    "material_duplicate",
    "evidence_boundary",
    "falsifiability",
    "plan_usability",
}
DISPOSITION_ACTIONS = {"revised", "not_adopted"}


class ContractViolation(ValueError):
    """Fixture 未通过 fail-closed 契约门禁。"""


class TechnicalPortError(RuntimeError):
    """Fake port 发生类型化技术失败。"""


def _require_text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ContractViolation("{} must be non-empty text".format(label))


def _require_fixture_ref(value: str, label: str) -> None:
    _require_text(value, label)
    if not FIXTURE_REF_RE.match(value):
        raise ContractViolation("{} must be an explicit fixture ref".format(label))
    if "latest" in re.split(r"[/:]", value.lower()):
        raise ContractViolation("{} cannot use a latest alias".format(label))


def _require_hash(value: str, label: str) -> None:
    if not isinstance(value, str) or not HASH_RE.match(value):
        raise ContractViolation("{} must be a sha256 content hash".format(label))


def _require_text_tuple(value: Tuple[str, ...], label: str) -> None:
    if not isinstance(value, tuple):
        raise ContractViolation("{} must be an explicit frozen tuple".format(label))
    for item in value:
        _require_text(item, label + " item")


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def canonical_hash(value: Any) -> str:
    raw = json.dumps(
        _jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def fixture_hash(seed: str) -> str:
    return "sha256:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AcceptedQuestionBinding:
    question_ref: str
    quest_ref: str
    question_content_ref: str
    question_content_hash: str
    question_content_schema_ref: str
    rm_content_accepted_receipt_ref: str
    rg_question_accepted_receipt_ref: str

    def validate(self) -> None:
        for label, value in (
            ("Question ref", self.question_ref),
            ("Question Quest ref", self.quest_ref),
            ("Question content ref", self.question_content_ref),
            ("Question content schema ref", self.question_content_schema_ref),
            (
                "Question content acceptance receipt",
                self.rm_content_accepted_receipt_ref,
            ),
            (
                "Question acceptance receipt",
                self.rg_question_accepted_receipt_ref,
            ),
        ):
            _require_fixture_ref(value, label)
        _require_hash(self.question_content_hash, "Question content hash")


@dataclass(frozen=True)
class QuestGoalContent:
    goal_statement: str
    completion_milestones: Tuple[str, ...]
    exclusions: Tuple[str, ...]

    def validate(self) -> None:
        _require_text(self.goal_statement, "Quest Goal statement")
        _require_text_tuple(self.completion_milestones, "Quest Goal milestones")
        _require_text_tuple(self.exclusions, "Quest Goal exclusions")


@dataclass(frozen=True)
class QuestGoalAnchor:
    quest_ref: str
    goal_revision_ref: str
    goal_content_ref: str
    goal_content_hash: str
    goal_accepted_receipt_ref: str
    content: QuestGoalContent

    def validate(self) -> None:
        for label, value in (
            ("Quest ref", self.quest_ref),
            ("Quest Goal revision ref", self.goal_revision_ref),
            ("Quest Goal content ref", self.goal_content_ref),
            ("Quest Goal acceptance receipt", self.goal_accepted_receipt_ref),
        ):
            _require_fixture_ref(value, label)
        _require_hash(self.goal_content_hash, "Quest Goal content hash")
        if not isinstance(self.content, QuestGoalContent):
            raise ContractViolation("Quest Goal content projection must be typed")
        self.content.validate()
        if canonical_hash(self.content) != self.goal_content_hash:
            raise ContractViolation("Quest Goal anchor copy-by-value hash drifted")


@dataclass(frozen=True)
class NoLiteratureAnchor:
    status: str = field(default="none", init=False)

    def validate(self) -> None:
        if self.status != "none":
            raise ContractViolation("empty literature anchor must use status none")


@dataclass(frozen=True)
class BoundLiteratureAnchor:
    question_literature_revision_ref: str
    revision_content_hash: str
    rm_accepted_receipt_ref: str
    rg_question_association_receipt_ref: str
    summary_ref: str
    papers_ref: str
    fulltext_manifest_ref: str
    status: str = field(default="bound", init=False)

    def validate(self) -> None:
        if self.status != "bound":
            raise ContractViolation("bound literature anchor must use status bound")
        for label, value in (
            ("QuestionLiteratureRevision ref", self.question_literature_revision_ref),
            ("literature RM acceptance receipt", self.rm_accepted_receipt_ref),
            ("literature RG association receipt", self.rg_question_association_receipt_ref),
            ("literature summary ref", self.summary_ref),
            ("literature papers ref", self.papers_ref),
            ("literature fulltext manifest ref", self.fulltext_manifest_ref),
        ):
            _require_fixture_ref(value, label)
        _require_hash(self.revision_content_hash, "literature revision content hash")


LiteratureAnchor = Union[NoLiteratureAnchor, BoundLiteratureAnchor]


@dataclass(frozen=True)
class StableReferenceBinding:
    semantic_role: str
    source_owner: str
    object_ref: str
    content_hash: str
    authority_proof_refs: Tuple[str, ...]

    def validate(self) -> None:
        _require_text(self.semantic_role, "stable reference semantic role")
        _require_text(self.source_owner, "stable reference source Owner")
        _require_fixture_ref(self.object_ref, "stable object ref")
        _require_hash(self.content_hash, "stable object content hash")
        if not isinstance(self.authority_proof_refs, tuple) or not self.authority_proof_refs:
            raise ContractViolation("stable reference requires authority proof")
        for ref in self.authority_proof_refs:
            _require_fixture_ref(ref, "authority proof ref")


@dataclass(frozen=True)
class PriorResearch:
    history_basis_refs: Tuple[StableReferenceBinding, ...]
    accepted_idea_outcomes: Tuple[StableReferenceBinding, ...]
    accepted_formal_plan_refs: Tuple[StableReferenceBinding, ...]
    reasoning_conclusion_refs: Tuple[StableReferenceBinding, ...]
    accepted_evidence_refs: Tuple[StableReferenceBinding, ...]
    prior_stage_commit_refs: Tuple[StableReferenceBinding, ...]

    def validate(self) -> None:
        for label, bindings in (
            ("history basis", self.history_basis_refs),
            ("accepted Idea outcomes", self.accepted_idea_outcomes),
            ("accepted formal Plans", self.accepted_formal_plan_refs),
            ("Reasoning conclusions", self.reasoning_conclusion_refs),
            ("accepted evidence", self.accepted_evidence_refs),
            ("prior StageCommits", self.prior_stage_commit_refs),
        ):
            if not isinstance(bindings, tuple):
                raise ContractViolation(label + " must be an explicit frozen tuple")
            for binding in bindings:
                if not isinstance(binding, StableReferenceBinding):
                    raise ContractViolation(label + " must use stable bindings")
                binding.validate()


@dataclass(frozen=True)
class SoftConstraintBinding:
    constraint_ref: str
    revision_ref: str
    content_hash: str
    scope_ref: str
    active_status_receipt_ref: str
    statement: str

    def validate(self) -> None:
        for label, value in (
            ("soft constraint ref", self.constraint_ref),
            ("soft constraint revision ref", self.revision_ref),
            ("soft constraint scope ref", self.scope_ref),
            ("soft constraint active receipt", self.active_status_receipt_ref),
        ):
            _require_fixture_ref(value, label)
        _require_hash(self.content_hash, "soft constraint content hash")
        _require_text(self.statement, "soft constraint statement")


@dataclass(frozen=True)
class ActiveGuidance:
    soft_constraint_bindings: Tuple[SoftConstraintBinding, ...]

    def validate(self) -> None:
        if not isinstance(self.soft_constraint_bindings, tuple):
            raise ContractViolation("active guidance must be an explicit frozen tuple")
        for binding in self.soft_constraint_bindings:
            if not isinstance(binding, SoftConstraintBinding):
                raise ContractViolation("active guidance bindings must be typed")
            binding.validate()


@dataclass(frozen=True)
class ReadContract:
    semantic_operation_id: str
    content_schema_ref: str
    media_type: str
    unavailable_policy: str

    def validate(self, requiredness: str) -> None:
        _require_text(self.semantic_operation_id, "semantic operation id")
        _require_fixture_ref(self.content_schema_ref, "navigation content schema ref")
        _require_text(self.media_type, "navigation media type")
        if self.unavailable_policy not in {"fail_closed", "preserve_unknown"}:
            raise ContractViolation("navigation unavailable policy is invalid")
        if requiredness == "required" and self.unavailable_policy != "fail_closed":
            raise ContractViolation("required navigation roots must fail closed")


@dataclass(frozen=True)
class NavigationRoot:
    binding_ref: str
    semantic_role: str
    source_owner: str
    object_ref: str
    content_hash: str
    authority_proof_refs: Tuple[str, ...]
    requiredness: str
    data_classification: str
    read_contract: ReadContract

    def validate(self) -> None:
        _require_fixture_ref(self.binding_ref, "navigation binding ref")
        _require_text(self.semantic_role, "navigation semantic role")
        _require_text(self.source_owner, "navigation source Owner")
        _require_fixture_ref(self.object_ref, "navigation object ref")
        _require_hash(self.content_hash, "navigation content hash")
        if not isinstance(self.authority_proof_refs, tuple) or not self.authority_proof_refs:
            raise ContractViolation("navigation root requires authority proof")
        for ref in self.authority_proof_refs:
            _require_fixture_ref(ref, "navigation authority proof ref")
        if self.requiredness not in {"required", "optional"}:
            raise ContractViolation("navigation requiredness is invalid")
        if self.data_classification != "data_only":
            raise ContractViolation("navigation roots must be data_only")
        if not isinstance(self.read_contract, ReadContract):
            raise ContractViolation("navigation read contract must be typed")
        self.read_contract.validate(self.requiredness)


@dataclass(frozen=True)
class ContextPackIdentity:
    schema_ref: str
    schema_version: str
    pack_ref: str
    stage: str
    quest_ref: str
    cycle_ref: str
    question_ref: str
    compilation_basis_refs: Tuple[str, ...]

    def validate(self) -> None:
        for label, value in (
            ("ContextPack schema ref", self.schema_ref),
            ("ContextPack ref", self.pack_ref),
            ("ContextPack Quest ref", self.quest_ref),
            ("ContextPack Cycle ref", self.cycle_ref),
            ("ContextPack Question ref", self.question_ref),
        ):
            _require_fixture_ref(value, label)
        _require_text(self.schema_version, "ContextPack schema version")
        if self.stage != "Idea":
            raise ContractViolation("ContextPack stage must be Idea")
        if not isinstance(self.compilation_basis_refs, tuple) or not self.compilation_basis_refs:
            raise ContractViolation("ContextPack requires exact compilation bases")
        for ref in self.compilation_basis_refs:
            _require_fixture_ref(ref, "ContextPack compilation basis ref")


@dataclass(frozen=True)
class FrozenContextPack:
    identity: ContextPackIdentity
    accepted_question_binding: AcceptedQuestionBinding
    accepted_question_content_data: Any
    quest_goal_anchor: QuestGoalAnchor
    literature_anchor: LiteratureAnchor
    prior_research: PriorResearch
    active_guidance: ActiveGuidance
    navigation_roots: Tuple[NavigationRoot, ...]
    content_sha256: str

    @property
    def ref(self) -> str:
        return self.identity.pack_ref

    @property
    def question_ref(self) -> str:
        return self.identity.question_ref

    @property
    def question_literature_revision_ref(self) -> Optional[str]:
        if isinstance(self.literature_anchor, BoundLiteratureAnchor):
            return self.literature_anchor.question_literature_revision_ref
        return None

    def payload_without_digest(self) -> Dict[str, Any]:
        return {
            "identity": self.identity,
            "accepted_question_binding": self.accepted_question_binding,
            "accepted_question_content_data": self.accepted_question_content_data,
            "quest_goal_anchor": self.quest_goal_anchor,
            "literature_anchor": self.literature_anchor,
            "prior_research": self.prior_research,
            "active_guidance": self.active_guidance,
            "navigation_roots": self.navigation_roots,
        }

    def validate(self) -> None:
        if not isinstance(self.identity, ContextPackIdentity):
            raise ContractViolation("ContextPack identity must be typed")
        self.identity.validate()
        if not isinstance(self.accepted_question_binding, AcceptedQuestionBinding):
            raise ContractViolation("ContextPack accepted Question binding must be typed")
        self.accepted_question_binding.validate()
        if self.accepted_question_binding.question_ref != self.identity.question_ref:
            raise ContractViolation(
                "accepted Question binding does not match ContextPack identity"
            )
        if self.accepted_question_binding.quest_ref != self.identity.quest_ref:
            raise ContractViolation(
                "accepted Question Quest does not match ContextPack identity"
            )
        if self.accepted_question_content_data is None:
            raise ContractViolation("accepted Question content data is required")
        try:
            accepted_content_hash = canonical_hash(
                self.accepted_question_content_data
            )
        except (TypeError, ValueError) as exc:
            raise ContractViolation(
                "accepted Question content data must be canonical JSON data"
            ) from exc
        if (
            accepted_content_hash
            != self.accepted_question_binding.question_content_hash
        ):
            raise ContractViolation(
                "accepted Question content data hash does not match its binding"
            )
        if not isinstance(self.quest_goal_anchor, QuestGoalAnchor):
            raise ContractViolation("ContextPack Quest Goal anchor must be typed")
        self.quest_goal_anchor.validate()
        if self.quest_goal_anchor.quest_ref != self.identity.quest_ref:
            raise ContractViolation("Quest Goal anchor does not match ContextPack identity")
        if not isinstance(self.literature_anchor, (NoLiteratureAnchor, BoundLiteratureAnchor)):
            raise ContractViolation("literature anchor must be a typed none|bound union")
        self.literature_anchor.validate()
        if not isinstance(self.prior_research, PriorResearch):
            raise ContractViolation("prior research must be typed")
        self.prior_research.validate()
        if not isinstance(self.active_guidance, ActiveGuidance):
            raise ContractViolation("active guidance must be typed")
        self.active_guidance.validate()
        if not isinstance(self.navigation_roots, tuple):
            raise ContractViolation("navigation roots must be an explicit frozen tuple")
        for root in self.navigation_roots:
            if not isinstance(root, NavigationRoot):
                raise ContractViolation("navigation roots must be typed")
            root.validate()
        _require_hash(self.content_sha256, "ContextPack content hash")
        if canonical_hash(self.payload_without_digest()) != self.content_sha256:
            raise ContractViolation("ContextPack content hash drifted")


def make_context_pack(
    identity: ContextPackIdentity,
    accepted_question_binding: AcceptedQuestionBinding,
    accepted_question_content_data: Any,
    quest_goal_anchor: QuestGoalAnchor,
    literature_anchor: LiteratureAnchor,
    prior_research: PriorResearch,
    active_guidance: ActiveGuidance,
    navigation_roots: Tuple[NavigationRoot, ...],
) -> FrozenContextPack:
    payload = {
        "identity": identity,
        "accepted_question_binding": accepted_question_binding,
        "accepted_question_content_data": accepted_question_content_data,
        "quest_goal_anchor": quest_goal_anchor,
        "literature_anchor": literature_anchor,
        "prior_research": prior_research,
        "active_guidance": active_guidance,
        "navigation_roots": navigation_roots,
    }
    return FrozenContextPack(
        identity=identity,
        accepted_question_binding=accepted_question_binding,
        accepted_question_content_data=accepted_question_content_data,
        quest_goal_anchor=quest_goal_anchor,
        literature_anchor=literature_anchor,
        prior_research=prior_research,
        active_guidance=active_guidance,
        navigation_roots=navigation_roots,
        content_sha256=canonical_hash(payload),
    )


@dataclass(frozen=True)
class StageRunRequest:
    contract_id: str
    ref: str
    stage: str
    quest_ref: str
    cycle_ref: str
    question_ref: str
    quest_goal_revision_ref: str
    foreground_epoch_ref: str
    context_pack_ref: str
    context_pack_sha256: str
    question_literature_revision_ref: Optional[str]
    def validate(self) -> None:
        if self.contract_id != IDEA_CONTRACT:
            raise ContractViolation("unknown Idea typed contract")
        if self.stage != "Idea":
            raise ContractViolation("StageRunRequest stage must be Idea")
        for label, value in (
            ("StageRunRequest ref", self.ref),
            ("Quest ref", self.quest_ref),
            ("Cycle ref", self.cycle_ref),
            ("Question ref", self.question_ref),
            ("Quest Goal revision ref", self.quest_goal_revision_ref),
            ("Foreground Epoch ref", self.foreground_epoch_ref),
            ("ContextPack ref", self.context_pack_ref),
        ):
            _require_fixture_ref(value, label)
        _require_hash(self.context_pack_sha256, "bound ContextPack hash")
        if self.question_literature_revision_ref is not None:
            _require_fixture_ref(
                self.question_literature_revision_ref,
                "bound QuestionLiteratureRevision ref",
            )


@dataclass(frozen=True)
class IdeaRunBinding:
    ref: str
    run_ref: str
    attempt_ref: str
    root_session_ref: str
    execution_fence_ref: str
    stage_run_request_ref: str
    stage_run_request_hash: str
    context_pack_ref: str
    context_pack_sha256: str
    launch_manifest_ref: str
    runtime_observation_ref: str

    def validate(self, request: StageRunRequest) -> None:
        for label, value in (
            ("IdeaRunBinding ref", self.ref),
            ("Run ref", self.run_ref),
            ("Attempt ref", self.attempt_ref),
            ("root Session ref", self.root_session_ref),
            ("Execution Fence ref", self.execution_fence_ref),
            ("bound StageRunRequest ref", self.stage_run_request_ref),
            ("bound ContextPack ref", self.context_pack_ref),
            ("launch manifest ref", self.launch_manifest_ref),
            ("runtime observation ref", self.runtime_observation_ref),
        ):
            _require_fixture_ref(value, label)
        _require_hash(self.stage_run_request_hash, "bound StageRunRequest hash")
        _require_hash(self.context_pack_sha256, "bound ContextPack hash")
        if self.stage_run_request_ref != request.ref:
            raise ContractViolation("IdeaRunBinding is bound to another request")
        if self.stage_run_request_hash != canonical_hash(request):
            raise ContractViolation("IdeaRunBinding request hash does not match")
        if (
            self.context_pack_ref != request.context_pack_ref
            or self.context_pack_sha256 != request.context_pack_sha256
        ):
            raise ContractViolation("IdeaRunBinding ContextPack does not match request")


def make_run_binding(
    request: StageRunRequest,
    *,
    suffix: str = "idea-1",
) -> IdeaRunBinding:
    return IdeaRunBinding(
        ref="fixture:ar/run-binding/" + suffix,
        run_ref="fixture:ar/run/" + suffix,
        attempt_ref="fixture:ar/attempt/" + suffix,
        root_session_ref="fixture:ar/session/root-" + suffix,
        execution_fence_ref="fixture:ar/fence/" + suffix,
        stage_run_request_ref=request.ref,
        stage_run_request_hash=canonical_hash(request),
        context_pack_ref=request.context_pack_ref,
        context_pack_sha256=request.context_pack_sha256,
        launch_manifest_ref="fixture:ar/launch/" + suffix,
        runtime_observation_ref="fixture:ar/runtime-observation/" + suffix,
    )


@dataclass(frozen=True)
class VerifiedInvocation:
    request: StageRunRequest
    run_binding: IdeaRunBinding
    context_pack: FrozenContextPack


def verify_invocation(
    request: Any, run_binding: Any, context_pack: Any
) -> VerifiedInvocation:
    if not isinstance(request, StageRunRequest):
        raise ContractViolation("invocation must be a typed StageRunRequest")
    if not isinstance(context_pack, FrozenContextPack):
        raise ContractViolation("ContextPack must be an immutable typed fixture")
    request.validate()
    if not isinstance(run_binding, IdeaRunBinding):
        raise ContractViolation("runtime binding must be a typed IdeaRunBinding")
    run_binding.validate(request)
    context_pack.validate()
    if context_pack.ref != request.context_pack_ref:
        raise ContractViolation("ContextPack ref does not match the request")
    if context_pack.content_sha256 != request.context_pack_sha256:
        raise ContractViolation("ContextPack hash does not match the request")
    if context_pack.question_ref != request.question_ref:
        raise ContractViolation("ContextPack Question does not match the request")
    if context_pack.identity.quest_ref != request.quest_ref:
        raise ContractViolation("ContextPack Quest does not match the request")
    if context_pack.identity.cycle_ref != request.cycle_ref:
        raise ContractViolation("ContextPack Cycle does not match the request")
    if context_pack.quest_goal_anchor.goal_revision_ref != request.quest_goal_revision_ref:
        raise ContractViolation("Quest Goal revision does not match the request")
    if (
        context_pack.question_literature_revision_ref
        != request.question_literature_revision_ref
    ):
        raise ContractViolation("literature revision binding does not match the request")
    if (
        run_binding.context_pack_ref != context_pack.ref
        or run_binding.context_pack_sha256 != context_pack.content_sha256
    ):
        raise ContractViolation("runtime binding does not match exact ContextPack")
    return VerifiedInvocation(
        request=request, run_binding=run_binding, context_pack=context_pack
    )


@dataclass(frozen=True)
class EvidenceBoundary:
    accepted_evidence_refs: Tuple[str, ...]
    supported: str
    inferred: str
    unknown: str

    def validate(self) -> None:
        for ref in self.accepted_evidence_refs:
            _require_fixture_ref(ref, "accepted evidence ref")
        _require_text(self.supported, "supported boundary")
        _require_text(self.inferred, "inferred boundary")
        _require_text(self.unknown, "unknown boundary")


@dataclass(frozen=True)
class FalsificationHint:
    test: str
    would_refute: str

    def validate(self) -> None:
        _require_text(self.test, "falsification test")
        _require_text(self.would_refute, "falsification criterion")


@dataclass(frozen=True)
class MaterialDifference:
    from_history: str
    from_peers: str
    plan_commitment_change: str

    def validate(self) -> None:
        _require_text(self.from_history, "difference from history")
        _require_text(self.from_peers, "difference from peers")
        _require_text(
            self.plan_commitment_change, "different Plan commitment explanation"
        )


@dataclass(frozen=True)
class IdeaCandidate:
    candidate_key: str
    direction: str
    rationale: str
    assumptions: Tuple[str, ...]
    risks: Tuple[str, ...]
    evidence_boundary: EvidenceBoundary
    falsification_hint: FalsificationHint
    material_difference: MaterialDifference

    def validate(self) -> None:
        _require_text(self.candidate_key, "candidate key")
        _require_text(self.direction, "candidate direction")
        _require_text(self.rationale, "candidate rationale")
        if not self.assumptions or not all(item.strip() for item in self.assumptions):
            raise ContractViolation("candidate assumptions must be explicit")
        if not self.risks or not all(item.strip() for item in self.risks):
            raise ContractViolation("candidate risks must be explicit")
        self.evidence_boundary.validate()
        self.falsification_hint.validate()
        self.material_difference.validate()


@dataclass(frozen=True)
class AdvisoryRecommendation:
    note: str
    binding: bool = False

    def validate(self) -> None:
        _require_text(self.note, "advisory recommendation")
        if self.binding is not False:
            raise ContractViolation("an Idea recommendation must remain non-binding")


@dataclass(frozen=True)
class IdeaSet:
    question_ref: str
    context_pack_ref: str
    candidates: Tuple[IdeaCandidate, ...]
    recommendation: Optional[AdvisoryRecommendation] = None
    kind: str = field(default="IdeaSet", init=False)

    def validate(self) -> None:
        _require_fixture_ref(self.question_ref, "IdeaSet Question ref")
        _require_fixture_ref(self.context_pack_ref, "IdeaSet ContextPack ref")
        if not isinstance(self.candidates, tuple) or not self.candidates:
            raise ContractViolation("IdeaSet must contain one or more candidates")
        keys = []
        for candidate in self.candidates:
            if not isinstance(candidate, IdeaCandidate):
                raise ContractViolation("IdeaSet candidates must be typed")
            candidate.validate()
            keys.append(candidate.candidate_key)
        if len(keys) != len(set(keys)):
            raise ContractViolation("IdeaSet candidate keys must be unique")
        if self.recommendation is not None:
            self.recommendation.validate()


@dataclass(frozen=True)
class ConsideredFamily:
    family: str
    why_not_viable: str
    evidence_refs: Tuple[str, ...]

    def validate(self) -> None:
        _require_text(self.family, "considered candidate family")
        _require_text(self.why_not_viable, "candidate-family rejection reason")
        for ref in self.evidence_refs:
            _require_fixture_ref(ref, "candidate-family evidence ref")


@dataclass(frozen=True)
class NoViableCandidate:
    question_ref: str
    context_pack_ref: str
    exploration_scope: str
    candidate_families_considered: Tuple[ConsideredFamily, ...]
    evidence_boundary: EvidenceBoundary
    overturn_conditions: Tuple[str, ...]
    why_plan_cannot_proceed: str
    kind: str = field(default="NoViableCandidate", init=False)

    def validate(self) -> None:
        _require_fixture_ref(self.question_ref, "NoViableCandidate Question ref")
        _require_fixture_ref(
            self.context_pack_ref, "NoViableCandidate ContextPack ref"
        )
        _require_text(self.exploration_scope, "exploration scope")
        if not self.candidate_families_considered:
            raise ContractViolation("NoViableCandidate must name considered families")
        for family in self.candidate_families_considered:
            if not isinstance(family, ConsideredFamily):
                raise ContractViolation("considered families must be typed")
            family.validate()
        self.evidence_boundary.validate()
        if not self.overturn_conditions or not all(
            condition.strip() for condition in self.overturn_conditions
        ):
            raise ContractViolation("overturn conditions must be explicit")
        _require_text(self.why_plan_cannot_proceed, "Plan boundary")


IdeaOutcome = Union[IdeaSet, NoViableCandidate]


def validate_outcome(outcome: Any, verified: VerifiedInvocation) -> None:
    if not isinstance(outcome, (IdeaSet, NoViableCandidate)):
        raise ContractViolation("outcome must be typed IdeaSet or NoViableCandidate")
    outcome.validate()
    if outcome.question_ref != verified.request.question_ref:
        raise ContractViolation("outcome Question does not match the request")
    if outcome.context_pack_ref != verified.context_pack.ref:
        raise ContractViolation("outcome ContextPack does not match the request")
    claimed_evidence_refs: Tuple[str, ...] = ()
    if isinstance(outcome, IdeaSet):
        for candidate in outcome.candidates:
            claimed_evidence_refs += candidate.evidence_boundary.accepted_evidence_refs
    else:
        claimed_evidence_refs += outcome.evidence_boundary.accepted_evidence_refs
        for family in outcome.candidate_families_considered:
            claimed_evidence_refs += family.evidence_refs
    bound_evidence_refs = {
        binding.object_ref
        for binding in verified.context_pack.prior_research.accepted_evidence_refs
    }
    if not set(claimed_evidence_refs).issubset(bound_evidence_refs):
        raise ContractViolation(
            "outcome claims accepted evidence without a ContextPack authority binding"
        )


@dataclass(frozen=True)
class ReviewFinding:
    finding_id: str
    category: str
    message: str

    def validate(self) -> None:
        _require_text(self.finding_id, "review finding id")
        if self.category not in REVIEW_CATEGORIES:
            raise ContractViolation("unknown advisory review category")
        _require_text(self.message, "review finding")


@dataclass(frozen=True)
class ReviewDisposition:
    finding_id: str
    action: str
    rationale: str

    def validate(self) -> None:
        _require_text(self.finding_id, "review disposition finding id")
        if self.action not in DISPOSITION_ACTIONS:
            raise ContractViolation("unknown review disposition action")
        _require_text(self.rationale, "review disposition rationale")


@dataclass(frozen=True)
class AdvisoryReviewRecord:
    review_ref: str
    reviewer_session_ref: str
    reviewed_draft_hash: str
    final_outcome_hash: str
    findings: Tuple[ReviewFinding, ...]
    dispositions: Tuple[ReviewDisposition, ...]
    independent: bool = True
    advisory_only: bool = True

    def validate(
        self,
        root_session_ref: str,
        reviewed_draft: IdeaOutcome,
        final_outcome: IdeaOutcome,
    ) -> None:
        _require_fixture_ref(self.review_ref, "review ref")
        _require_fixture_ref(self.reviewer_session_ref, "reviewer Session ref")
        if self.reviewer_session_ref == root_session_ref or self.independent is not True:
            raise ContractViolation("Idea review must be independent from the root Session")
        if self.advisory_only is not True:
            raise ContractViolation("Idea review cannot claim Owner authority")
        _require_hash(self.reviewed_draft_hash, "reviewed draft hash")
        _require_hash(self.final_outcome_hash, "final outcome hash")
        if self.reviewed_draft_hash != canonical_hash(reviewed_draft):
            raise ContractViolation("review does not bind the reviewed draft")
        if self.final_outcome_hash != canonical_hash(final_outcome):
            raise ContractViolation("review record does not bind the final outcome")
        finding_ids = []
        for finding in self.findings:
            if not isinstance(finding, ReviewFinding):
                raise ContractViolation("review findings must be typed")
            finding.validate()
            finding_ids.append(finding.finding_id)
        if len(finding_ids) != len(set(finding_ids)):
            raise ContractViolation("review finding ids must be unique")
        disposition_ids = []
        for disposition in self.dispositions:
            if not isinstance(disposition, ReviewDisposition):
                raise ContractViolation("review dispositions must be typed")
            disposition.validate()
            disposition_ids.append(disposition.finding_id)
        if len(disposition_ids) != len(set(disposition_ids)):
            raise ContractViolation("each finding must have one disposition")
        if set(finding_ids) != set(disposition_ids):
            raise ContractViolation("every finding must have exactly one disposition")
        if any(
            disposition.action == "revised" for disposition in self.dispositions
        ) and self.reviewed_draft_hash == self.final_outcome_hash:
            raise ContractViolation(
                "a revised disposition must bind a materially changed outcome"
            )


@dataclass(frozen=True)
class OwnerFeedbackRevisionRecord:
    revision_ref: str
    prior_review_ref: str
    predecessor_submission_ref: str
    owner_rejection_receipt_ref: str
    final_outcome_hash: str
    root_revision_rationale: str
    supplemental_review_ref: Optional[str] = None
    root_owned: bool = True

    def validate(
        self, submission: "SubmissionIdentity", final_outcome: IdeaOutcome
    ) -> None:
        for label, ref in (
            ("Owner-feedback revision ref", self.revision_ref),
            ("prior advisory review ref", self.prior_review_ref),
            ("predecessor submission ref", self.predecessor_submission_ref),
            ("Owner rejection receipt ref", self.owner_rejection_receipt_ref),
        ):
            _require_fixture_ref(ref, label)
        if self.supplemental_review_ref is not None:
            _require_fixture_ref(
                self.supplemental_review_ref, "supplemental review ref"
            )
        _require_hash(self.final_outcome_hash, "revised final outcome hash")
        _require_text(self.root_revision_rationale, "root revision rationale")
        if self.root_owned is not True:
            raise ContractViolation("Owner-feedback revision remains root-owned")
        if submission.predecessor_submission_ref != self.predecessor_submission_ref:
            raise ContractViolation("revision record links another predecessor")
        if submission.owner_rejection_receipt_ref != self.owner_rejection_receipt_ref:
            raise ContractViolation("revision record links another rejection receipt")
        if self.final_outcome_hash != canonical_hash(final_outcome):
            raise ContractViolation("revision record does not bind the final outcome")


ReviewEvidence = Union[AdvisoryReviewRecord, OwnerFeedbackRevisionRecord]


@dataclass(frozen=True)
class ReviewedOutcome:
    reviewed_draft: IdeaOutcome
    final_outcome: IdeaOutcome
    review: ReviewEvidence

    def validate(
        self, verified: VerifiedInvocation, submission: "SubmissionIdentity"
    ) -> None:
        validate_outcome(self.reviewed_draft, verified)
        validate_outcome(self.final_outcome, verified)
        if isinstance(self.review, AdvisoryReviewRecord):
            self.review.validate(
                verified.run_binding.root_session_ref,
                self.reviewed_draft,
                self.final_outcome,
            )
        elif isinstance(self.review, OwnerFeedbackRevisionRecord):
            self.review.validate(submission, self.final_outcome)
        else:
            raise ContractViolation("review evidence must be a typed record")


@dataclass(frozen=True)
class FixtureOwnerReply:
    status: str
    receipt_ref: Optional[str] = None
    accepted_ref: Optional[str] = None
    feedback: Tuple[str, ...] = ()
    human_request_ref: Optional[str] = None
    submission_ref: Optional[str] = None
    is_owner_fact: bool = False

    def validate(self) -> None:
        if self.status not in OWNER_STATUSES:
            raise ContractViolation("unknown fake Owner status")
        if self.receipt_ref is not None:
            _require_fixture_ref(self.receipt_ref, "fake receipt ref")
        if self.accepted_ref is not None:
            _require_fixture_ref(self.accepted_ref, "fake accepted object ref")
        if self.submission_ref is not None:
            _require_fixture_ref(self.submission_ref, "fake submission ref")
        if self.human_request_ref is not None:
            _require_fixture_ref(self.human_request_ref, "fake HumanRequest ref")
        if self.is_owner_fact is not False:
            raise ContractViolation("fixture replies cannot claim production Owner authority")
        if self.status in {"accepted", "stale", "needs_input"} and self.receipt_ref is None:
            raise ContractViolation(
                "decided fake reply requires a fixture decision receipt"
            )
        if self.status == "accepted" and self.accepted_ref is None:
            raise ContractViolation("accepted fake reply requires an accepted object ref")
        if self.status != "accepted" and self.accepted_ref is not None:
            raise ContractViolation("a non-accepted reply cannot expose an accepted ref")
        if self.status == "rejected" and (
            self.receipt_ref is None or not self.feedback
        ):
            raise ContractViolation("rejected fake reply requires receipt and feedback")
        if self.status == "needs_input" and self.human_request_ref is None:
            raise ContractViolation("needs_input requires a HumanRequest ref")
        if self.status == "outcome_unknown" and (
            self.submission_ref is None or self.receipt_ref is None
        ):
            raise ContractViolation(
                "outcome_unknown requires a submission ref and observation receipt"
            )


@dataclass(frozen=True)
class SubmissionIdentity:
    """一份 exact submission payload 的稳定 identity。

    语义修订留在同一 Run／根 Session，但取得新的 identity，并绑定被拒的
    predecessor 与其 decision receipt。
    """

    ref: str
    predecessor_submission_ref: Optional[str] = None
    owner_rejection_receipt_ref: Optional[str] = None
    owner_needs_input_receipt_ref: Optional[str] = None
    owner_recovery_receipt_ref: Optional[str] = None

    def validate(self) -> None:
        _require_fixture_ref(self.ref, "submission ref")
        has_predecessor = self.predecessor_submission_ref is not None
        has_rejection = self.owner_rejection_receipt_ref is not None
        has_needs_input = self.owner_needs_input_receipt_ref is not None
        has_recovery = self.owner_recovery_receipt_ref is not None
        is_rejection_revision = has_predecessor and has_rejection
        is_needs_input_recovery = (
            has_predecessor and has_needs_input and has_recovery
        )
        if has_rejection and (has_needs_input or has_recovery):
            raise ContractViolation(
                "rejection and needs-input recovery lineage are mutually exclusive"
            )
        if has_predecessor and not (
            is_rejection_revision or is_needs_input_recovery
        ):
            raise ContractViolation(
                "a successor must carry complete rejection or recovery lineage"
            )
        if not has_predecessor and (has_rejection or has_needs_input or has_recovery):
            raise ContractViolation("lineage receipts require a predecessor submission")
        if self.predecessor_submission_ref is not None:
            _require_fixture_ref(
                self.predecessor_submission_ref, "predecessor submission ref"
            )
            if self.predecessor_submission_ref == self.ref:
                raise ContractViolation("a revised payload requires a new submission ref")
        if self.owner_rejection_receipt_ref is not None:
            _require_fixture_ref(
                self.owner_rejection_receipt_ref, "Owner rejection receipt ref"
            )
        if self.owner_needs_input_receipt_ref is not None:
            _require_fixture_ref(
                self.owner_needs_input_receipt_ref,
                "Owner needs-input decision receipt ref",
            )
        if self.owner_recovery_receipt_ref is not None:
            _require_fixture_ref(
                self.owner_recovery_receipt_ref, "Owner recovery receipt ref"
            )

    @property
    def is_rejection_revision(self) -> bool:
        return self.owner_rejection_receipt_ref is not None

    @property
    def is_needs_input_recovery(self) -> bool:
        return self.owner_recovery_receipt_ref is not None


@dataclass
class SubmissionRecord:
    identity: SubmissionIdentity
    payload_hash: str
    feedback_loop_hash: str
    final_outcome_hash: str
    review_anchor_ref: str
    result: Optional["SubmissionResult"] = None
    content_ref: Optional[str] = None
    content_decision_receipt_ref: Optional[str] = None
    content_decision_receipt_refs: Tuple[str, ...] = ()
    human_request_refs: Tuple[str, ...] = ()
    owner_recovery_receipt_refs: Tuple[str, ...] = ()
    blocker_refs: Tuple[str, ...] = ()


@dataclass(frozen=True)
class _SubmissionTransitionProof:
    submission_ref: str
    previous_result: Optional["SubmissionResult"]
    transition_kind: str
    registry_token: object = field(repr=False, compare=False)


@dataclass
class SubmissionIdentityRegistry:
    """只用于 fixture 的 exact replay 与 revision 幂等 registry。"""

    records: Dict[str, SubmissionRecord] = field(default_factory=dict)
    successor_by_ref: Dict[str, str] = field(default_factory=dict)
    head_by_feedback_loop: Dict[str, str] = field(default_factory=dict)
    _transition_token: object = field(default_factory=object, init=False, repr=False)

    def bind(
        self,
        identity: SubmissionIdentity,
        payload_hash: str,
        feedback_loop_hash: str,
        final_outcome_hash: str,
        review: ReviewEvidence,
        observation: "FixtureRunObservation",
    ) -> Optional["SubmissionResult"]:
        identity.validate()
        _require_hash(payload_hash, "submission payload hash")
        _require_hash(feedback_loop_hash, "submission feedback-loop hash")
        _require_hash(final_outcome_hash, "submission final outcome hash")
        if not isinstance(observation, FixtureRunObservation):
            raise ContractViolation("submission binding requires a typed Run observation")
        if isinstance(review, AdvisoryReviewRecord):
            review_anchor_ref = review.review_ref
        elif isinstance(review, OwnerFeedbackRevisionRecord):
            review_anchor_ref = review.prior_review_ref
        else:
            raise ContractViolation("submission review evidence is not typed")
        _require_fixture_ref(review_anchor_ref, "submission review anchor ref")
        existing = self.records.get(identity.ref)
        if existing is not None:
            if (
                existing.identity != identity
                or existing.payload_hash != payload_hash
                or existing.feedback_loop_hash != feedback_loop_hash
                or existing.final_outcome_hash != final_outcome_hash
                or existing.review_anchor_ref != review_anchor_ref
            ):
                raise ContractViolation(
                    "the same submission ref cannot bind a different payload"
                )
            current_head_ref = self.head_by_feedback_loop.get(feedback_loop_hash)
            if (
                existing.result is not None
                and existing.result.status
                in {"stale", "needs_input", "outcome_unknown", "technical_blocker"}
                and current_head_ref != identity.ref
            ):
                raise ContractViolation(
                    "a superseded recoverable identity cannot perform another replay"
                )
            return existing.result
        current_head_ref = self.head_by_feedback_loop.get(feedback_loop_hash)
        if current_head_ref is None and identity.predecessor_submission_ref is not None:
            raise ContractViolation(
                "a first submission cannot claim a predecessor from another loop"
            )
        if current_head_ref is not None:
            current_head = self.records[current_head_ref]
            current_status = (
                current_head.result.status
                if current_head.result is not None
                else None
            )
            if current_status == "rejected" and (
                identity.predecessor_submission_ref != current_head_ref
            ):
                raise ContractViolation(
                    "a new submission identity must link the current rejected head"
                )
            if current_status == "stale" or (
                current_status == "technical_blocker"
                and current_head.result is not None
                and current_head.result.reconciliation_required_phase is None
            ):
                if identity.predecessor_submission_ref is not None:
                    raise ContractViolation(
                        "a stale or definite-no-write technical replacement has no Owner lineage"
                    )
                if current_head.payload_hash == payload_hash:
                    raise ContractViolation(
                        "an unchanged recoverable replay must reuse its submission identity"
                    )
            elif current_status == "needs_input":
                if not identity.is_needs_input_recovery:
                    raise ContractViolation(
                        "a changed payload after needs_input requires recovery lineage"
                    )
                if identity.predecessor_submission_ref != current_head_ref:
                    raise ContractViolation(
                        "needs-input recovery must link the current feedback-loop head"
                    )
                if current_head.payload_hash == payload_hash:
                    raise ContractViolation(
                        "an unchanged recovered payload must reuse its submission identity"
                    )
                if current_head.final_outcome_hash == final_outcome_hash:
                    raise ContractViolation(
                        "unchanged outcome recovery must reuse its submission identity"
                    )
                observation.assert_owner_recovery(
                    current_head.result.human_request_ref,
                    identity.owner_recovery_receipt_ref,
                )
            elif current_status != "rejected":
                raise ContractViolation(
                    "the current submission state blocks a replacement identity"
                )
        if identity.predecessor_submission_ref is not None:
            predecessor = self.records.get(identity.predecessor_submission_ref)
            if predecessor is None:
                raise ContractViolation(
                    "a revised submission must link a previously bound predecessor"
                )
            if predecessor.payload_hash == payload_hash:
                raise ContractViolation(
                    "a successor submission must bind a changed payload"
                )
            if predecessor.feedback_loop_hash != feedback_loop_hash:
                raise ContractViolation(
                    "submission recovery escaped the same Run/root Session/fence"
                )
            if identity.is_rejection_revision:
                if not isinstance(review, OwnerFeedbackRevisionRecord):
                    raise ContractViolation(
                        "a rejection successor requires root-owned revision lineage"
                    )
                if predecessor.final_outcome_hash == final_outcome_hash:
                    raise ContractViolation(
                        "an Owner-rejection successor must change the final outcome"
                    )
                if predecessor.result is None or predecessor.result.status != "rejected":
                    raise ContractViolation(
                        "only a decided Owner rejection can anchor a revision"
                    )
                rejection_receipt_ref = (
                    predecessor.result.domain_decision_receipt_ref
                    or predecessor.result.content_decision_receipt_ref
                )
                if identity.owner_rejection_receipt_ref != rejection_receipt_ref:
                    raise ContractViolation(
                        "revision does not link the predecessor rejection receipt"
                    )
                if (
                    isinstance(review, OwnerFeedbackRevisionRecord)
                    and review.prior_review_ref != predecessor.review_anchor_ref
                ):
                    raise ContractViolation(
                        "revision record does not link the predecessor review lineage"
                    )
            elif identity.is_needs_input_recovery:
                if predecessor.result is None or predecessor.result.status != "needs_input":
                    raise ContractViolation(
                        "only a decided needs_input result can anchor recovery"
                    )
                needs_input_receipt_ref = (
                    predecessor.result.domain_decision_receipt_ref
                    or predecessor.result.content_decision_receipt_ref
                )
                if identity.owner_needs_input_receipt_ref != needs_input_receipt_ref:
                    raise ContractViolation(
                        "recovery does not link the needs_input decision receipt"
                    )
                observation.assert_owner_recovery(
                    predecessor.result.human_request_ref,
                    identity.owner_recovery_receipt_ref,
                )
            successor = self.successor_by_ref.get(identity.predecessor_submission_ref)
            if successor is not None and successor != identity.ref:
                raise ContractViolation("a rejected submission already has a successor")
            self.successor_by_ref[identity.predecessor_submission_ref] = identity.ref
        inherited_human_request_refs: Tuple[str, ...] = ()
        inherited_recovery_receipt_refs: Tuple[str, ...] = ()
        if identity.is_needs_input_recovery:
            predecessor = self.records[identity.predecessor_submission_ref]
            inherited_human_request_refs = predecessor.human_request_refs
            if (
                predecessor.result is not None
                and predecessor.result.human_request_ref is not None
                and predecessor.result.human_request_ref
                not in inherited_human_request_refs
            ):
                inherited_human_request_refs += (
                    predecessor.result.human_request_ref,
                )
            inherited_recovery_receipt_refs = _merge_decision_receipts(
                predecessor.owner_recovery_receipt_refs,
                (identity.owner_recovery_receipt_ref,),
            )
        self.records[identity.ref] = SubmissionRecord(
            identity,
            payload_hash,
            feedback_loop_hash,
            final_outcome_hash,
            review_anchor_ref,
            human_request_refs=inherited_human_request_refs,
            owner_recovery_receipt_refs=inherited_recovery_receipt_refs,
        )
        self.head_by_feedback_loop[feedback_loop_hash] = identity.ref
        return None

    def record_owner_recovery(
        self,
        identity: SubmissionIdentity,
        observation: "FixtureRunObservation",
    ) -> str:
        record = self.records.get(identity.ref)
        if record is None or record.result is None:
            raise ContractViolation("Owner recovery requires a decided submission")
        if record.result.status != "needs_input":
            raise ContractViolation("only needs_input can consume a recovery disposition")
        recovery_receipt_ref = observation.assert_owner_recovery(
            record.result.human_request_ref
        )
        if (
            record.result.human_request_ref is not None
            and record.result.human_request_ref not in record.human_request_refs
        ):
            record.human_request_refs += (record.result.human_request_ref,)
        if recovery_receipt_ref not in record.owner_recovery_receipt_refs:
            record.owner_recovery_receipt_refs += (recovery_receipt_ref,)
        return recovery_receipt_ref

    def audit_histories(
        self, identity: SubmissionIdentity
    ) -> Tuple[Tuple[str, ...], Tuple[str, ...], Tuple[str, ...]]:
        record = self.records.get(identity.ref)
        if record is None:
            raise ContractViolation("cannot read histories for an unbound submission")
        return (
            record.human_request_refs,
            record.owner_recovery_receipt_refs,
            record.blocker_refs,
        )

    def _issue_transition_proof(
        self,
        identity: SubmissionIdentity,
        transition_kind: str,
        recovery_observation: Optional["FixtureRunObservation"] = None,
    ) -> _SubmissionTransitionProof:
        if transition_kind not in {"owner_write", "reconciliation", "technical"}:
            raise ContractViolation("unknown submission transition kind")
        record = self.records.get(identity.ref)
        if record is None:
            raise ContractViolation("cannot transition an unbound submission")
        previous = record.result
        if previous is not None and previous.status in {"accepted", "rejected"}:
            raise ContractViolation("a terminal Owner result is immutable")
        if previous is not None and previous.status == "needs_input":
            if not isinstance(recovery_observation, FixtureRunObservation):
                raise ContractViolation(
                    "needs_input cannot change without Owner-authorized recovery"
                )
            self.record_owner_recovery(identity, recovery_observation)
        if (
            previous is not None
            and previous.reconciliation_required_phase is not None
            and transition_kind == "owner_write"
        ):
            raise ContractViolation(
                "a reconciliation-required submission cannot perform a new write"
            )
        if transition_kind == "reconciliation" and (
            previous is None or previous.reconciliation_required_phase is None
        ):
            raise ContractViolation("reconciliation proof requires an unknown outcome")
        return _SubmissionTransitionProof(
            submission_ref=identity.ref,
            previous_result=previous,
            transition_kind=transition_kind,
            registry_token=self._transition_token,
        )

    def record_content_checkpoint(
        self,
        identity: SubmissionIdentity,
        content_ref: str,
        receipt_ref: str,
        receipt_refs: Tuple[str, ...],
    ) -> None:
        record = self.records.get(identity.ref)
        if record is None:
            raise ContractViolation("cannot checkpoint content for an unbound submission")
        _require_fixture_ref(content_ref, "accepted content ref")
        _require_fixture_ref(receipt_ref, "content acceptance receipt ref")
        for retained_ref in receipt_refs:
            _require_fixture_ref(retained_ref, "content decision receipt ref")
        if receipt_ref not in receipt_refs:
            raise ContractViolation("content checkpoint must retain its acceptance receipt")
        checkpoint = (
            record.content_ref,
            record.content_decision_receipt_ref,
            record.content_decision_receipt_refs,
        )
        incoming = (content_ref, receipt_ref, receipt_refs)
        if record.content_ref is not None and checkpoint != incoming:
            raise ContractViolation(
                "an accepted content checkpoint cannot drift for one submission"
            )
        record.content_ref = content_ref
        record.content_decision_receipt_ref = receipt_ref
        record.content_decision_receipt_refs = receipt_refs

    def content_checkpoint(
        self, identity: SubmissionIdentity
    ) -> Tuple[Optional[str], Optional[str], Tuple[str, ...]]:
        record = self.records.get(identity.ref)
        if record is None:
            raise ContractViolation("cannot read an unbound submission checkpoint")
        return (
            record.content_ref,
            record.content_decision_receipt_ref,
            record.content_decision_receipt_refs,
        )

    def record_result(
        self,
        identity: SubmissionIdentity,
        result: "SubmissionResult",
        transition_proof: Optional[_SubmissionTransitionProof] = None,
    ) -> None:
        record = self.records.get(identity.ref)
        if record is None:
            raise ContractViolation("cannot record an unbound submission result")
        if (
            not isinstance(transition_proof, _SubmissionTransitionProof)
            or transition_proof.registry_token is not self._transition_token
            or transition_proof.submission_ref != identity.ref
            or transition_proof.previous_result != record.result
        ):
            raise ContractViolation(
                "submission result requires the exact in-flight transition proof"
            )
        result.validate(identity)
        if record.content_ref is not None:
            if (
                result.content_ref != record.content_ref
                or result.content_decision_receipt_ref
                != record.content_decision_receipt_ref
                or not set(record.content_decision_receipt_refs).issubset(
                    result.content_decision_receipt_refs
                )
            ):
                raise ContractViolation(
                    "submission result cannot erase or drift accepted content"
                )
        if not set(record.human_request_refs).issubset(result.human_request_refs):
            raise ContractViolation("a result transition cannot erase HumanRequest history")
        if not set(record.owner_recovery_receipt_refs).issubset(
            result.owner_recovery_receipt_refs
        ):
            raise ContractViolation("a result transition cannot erase recovery receipts")
        if not set(record.blocker_refs).issubset(result.blocker_refs):
            raise ContractViolation("a result transition cannot erase blocker history")
        if result.status == "technical_blocker":
            if transition_proof.transition_kind != "technical":
                raise ContractViolation("technical result requires a technical proof")
        elif transition_proof.transition_kind == "technical":
            raise ContractViolation("technical proof cannot record an Owner decision")
        previous = record.result
        if previous is None:
            record.result = result
            record.human_request_refs = result.human_request_refs
            record.owner_recovery_receipt_refs = result.owner_recovery_receipt_refs
            record.blocker_refs = result.blocker_refs
            return
        if previous == result:
            return
        if previous.status in {"accepted", "rejected"}:
            raise ContractViolation("a terminal Owner result is immutable")
        if result.status != "technical_blocker":
            if (
                previous.reconciliation_required_phase is not None
                and transition_proof.transition_kind != "reconciliation"
            ):
                raise ContractViolation(
                    "unknown outcome can only resolve through reconciliation"
                )
            if transition_proof.transition_kind not in {
                "owner_write",
                "reconciliation",
            }:
                raise ContractViolation(
                    "Owner result requires a formal transition proof"
                )
        if not set(previous.content_decision_receipt_refs).issubset(
            result.content_decision_receipt_refs
        ) or not set(previous.domain_decision_receipt_refs).issubset(
            result.domain_decision_receipt_refs
        ):
            raise ContractViolation("a result transition cannot erase Owner receipts")
        reconciliation_advanced_from_content = (
            previous.reconciliation_required_phase == "content"
            and record.content_ref is not None
            and result.decision_phase == "domain"
        )
        if (
            previous.reconciliation_required_phase is not None
            and result.decision_phase != previous.reconciliation_required_phase
            and not reconciliation_advanced_from_content
        ):
            raise ContractViolation(
                "reconciliation must resolve the same original Owner phase"
            )
        record.result = result
        record.human_request_refs = result.human_request_refs
        record.owner_recovery_receipt_refs = result.owner_recovery_receipt_refs
        record.blocker_refs = result.blocker_refs


def submission_payload_hash(
    verified: VerifiedInvocation,
    submission: SubmissionIdentity,
    reviewed: ReviewedOutcome,
) -> str:
    request = verified.request
    run_binding = verified.run_binding
    bindings = {
        "contract_id": request.contract_id,
        "stage_run_request_ref": request.ref,
        "quest_ref": request.quest_ref,
        "cycle_ref": request.cycle_ref,
        "question_ref": request.question_ref,
        "foreground_epoch_ref": request.foreground_epoch_ref,
        "run_binding_ref": run_binding.ref,
        "run_ref": run_binding.run_ref,
        "root_session_ref": run_binding.root_session_ref,
        "execution_fence_ref": run_binding.execution_fence_ref,
        "context_pack_ref": request.context_pack_ref,
        "context_pack_sha256": request.context_pack_sha256,
        "question_literature_revision_ref": (
            request.question_literature_revision_ref
        ),
    }
    transition = {
        "predecessor_submission_ref": submission.predecessor_submission_ref,
        "owner_rejection_receipt_ref": submission.owner_rejection_receipt_ref,
        "owner_needs_input_receipt_ref": submission.owner_needs_input_receipt_ref,
        "owner_recovery_receipt_ref": submission.owner_recovery_receipt_ref,
    }
    return canonical_hash(
        {"bindings": bindings, "transition": transition, "reviewed": reviewed}
    )


def feedback_loop_hash(verified: VerifiedInvocation) -> str:
    request = verified.request
    run_binding = verified.run_binding
    return canonical_hash(
        {
            "contract_id": request.contract_id,
            "stage_run_request_ref": request.ref,
            "stage": request.stage,
            "quest_ref": request.quest_ref,
            "cycle_ref": request.cycle_ref,
            "question_ref": request.question_ref,
            "foreground_epoch_ref": request.foreground_epoch_ref,
            "run_binding_ref": run_binding.ref,
            "run_ref": run_binding.run_ref,
            "root_session_ref": run_binding.root_session_ref,
            "execution_fence_ref": run_binding.execution_fence_ref,
            "context_pack_ref": request.context_pack_ref,
            "context_pack_sha256": request.context_pack_sha256,
            "question_literature_revision_ref": (
                request.question_literature_revision_ref
            ),
        }
    )


@dataclass(frozen=True)
class FixtureRecoveryDisposition:
    human_request_ref: str
    owner_recovery_receipt_ref: str

    def validate(self) -> None:
        _require_fixture_ref(self.human_request_ref, "recovered HumanRequest ref")
        _require_fixture_ref(
            self.owner_recovery_receipt_ref, "Owner recovery receipt ref"
        )


@dataclass(frozen=True)
class FixtureRunObservation:
    run_ref: str
    root_session_ref: str
    execution_fence_ref: str
    prior_submission_refs: Tuple[str, ...] = ()
    owner_rejection_receipt_refs: Tuple[str, ...] = ()
    pending_submission_refs: Tuple[str, ...] = ()
    accepted_unconsumed_outcome_refs: Tuple[str, ...] = ()
    human_request_refs: Tuple[str, ...] = ()
    technical_blocker_refs: Tuple[str, ...] = ()
    outcome_unknown_refs: Tuple[str, ...] = ()
    recovery_dispositions: Tuple[FixtureRecoveryDisposition, ...] = ()
    existing_stage_commit_ref: Optional[str] = None
    run_reconciled: bool = True

    def validate(self, run_binding: IdeaRunBinding) -> None:
        _require_fixture_ref(self.run_ref, "observed Run ref")
        _require_fixture_ref(self.root_session_ref, "observed root Session ref")
        _require_fixture_ref(
            self.execution_fence_ref, "observed Execution Fence ref"
        )
        if (
            self.run_ref != run_binding.run_ref
            or self.root_session_ref != run_binding.root_session_ref
            or self.execution_fence_ref != run_binding.execution_fence_ref
        ):
            raise ContractViolation("Run observation is bound to another root execution")
        for label, refs in (
            ("prior submission", self.prior_submission_refs),
            ("Owner rejection receipt", self.owner_rejection_receipt_refs),
            ("pending submission", self.pending_submission_refs),
            ("accepted unconsumed outcome", self.accepted_unconsumed_outcome_refs),
            ("HumanRequest", self.human_request_refs),
            ("technical blocker", self.technical_blocker_refs),
            ("unknown outcome", self.outcome_unknown_refs),
        ):
            for ref in refs:
                _require_fixture_ref(ref, label + " ref")
        for disposition in self.recovery_dispositions:
            if not isinstance(disposition, FixtureRecoveryDisposition):
                raise ContractViolation("Run recovery dispositions must be typed")
            disposition.validate()
        if self.existing_stage_commit_ref is not None:
            _require_fixture_ref(
                self.existing_stage_commit_ref, "observed StageCommit ref"
            )

    def assert_formal_write_ready(self) -> None:
        if (
            self.pending_submission_refs
            or self.accepted_unconsumed_outcome_refs
            or self.human_request_refs
            or self.technical_blocker_refs
            or self.outcome_unknown_refs
            or self.existing_stage_commit_ref is not None
            or self.run_reconciled is not True
        ):
            raise ContractViolation("Run has an unresolved fact that blocks formal write")

    def assert_reconciliation_ready(self) -> None:
        if self.technical_blocker_refs or self.existing_stage_commit_ref is not None:
            raise ContractViolation(
                "Run has not recovered enough to reconcile the original submission"
            )

    def assert_owner_recovery(
        self,
        human_request_ref: Optional[str],
        owner_recovery_receipt_ref: Optional[str] = None,
    ) -> str:
        if human_request_ref is None:
            raise ContractViolation("needs_input recovery requires a HumanRequest ref")
        matches = tuple(
            disposition
            for disposition in self.recovery_dispositions
            if disposition.human_request_ref == human_request_ref
            and (
                owner_recovery_receipt_ref is None
                or disposition.owner_recovery_receipt_ref
                == owner_recovery_receipt_ref
            )
        )
        if len(matches) != 1:
            raise ContractViolation(
                "needs_input recovery requires one exact Owner disposition receipt"
            )
        if human_request_ref in self.human_request_refs:
            raise ContractViolation("the recovered HumanRequest remains unresolved")
        return matches[0].owner_recovery_receipt_ref


class InvocationPort(Protocol):
    # TODO-IMPL(AdvancementEngine.observe_idea_stage_run; source=#58)
    def observe_idea_stage_run(
        self, stage_run_request_ref: str
    ) -> StageRunRequest:
        ...

    # TODO-IMPL(AgentRuntime.observe_idea_run_binding; source=#71)
    def observe_idea_run_binding(
        self, stage_run_request_ref: str
    ) -> IdeaRunBinding:
        ...

    # TODO-IMPL(AgentRuntime.verify_delivered_context_pack; source=#71)
    def verify_delivered_context_pack(
        self,
        context_pack_ref: str,
        expected_sha256: str,
    ) -> FrozenContextPack:
        ...


class IdeaContentPort(Protocol):
    # TODO-IMPL(ResearchMemory.accept_idea_outcome_content; source=#66)
    def accept_idea_outcome_content(
        self,
        request: StageRunRequest,
        submission: SubmissionIdentity,
        outcome: IdeaOutcome,
        review: ReviewEvidence,
    ) -> FixtureOwnerReply:
        ...

    # TODO-IMPL(ResearchMemory.reconcile_idea_outcome_content; source=#66)
    def reconcile_idea_outcome_content(
        self, request: StageRunRequest, submission: SubmissionIdentity
    ) -> FixtureOwnerReply:
        ...


class IdeaDomainPort(Protocol):
    # TODO-IMPL(ResearchGraph.submit_idea_outcome; source=#101)
    def submit_idea_outcome(
        self,
        request: StageRunRequest,
        submission: SubmissionIdentity,
        content_ref: str,
        content_receipt_ref: str,
        outcome: IdeaOutcome,
    ) -> FixtureOwnerReply:
        ...

    # TODO-IMPL(ResearchGraph.reconcile_idea_outcome; source=#101)
    def reconcile_idea_outcome(
        self, request: StageRunRequest, submission: SubmissionIdentity
    ) -> FixtureOwnerReply:
        ...


class RuntimePort(Protocol):
    # TODO-IMPL(AgentRuntime.observe_run; source=#71)
    def observe_run(self, verified: VerifiedInvocation) -> FixtureRunObservation:
        ...

    # TODO-IMPL(AgentRuntime.report_execution_blocker; source=#90)
    def report_execution_blocker(
        self, request: StageRunRequest, blocker: str
    ) -> str:
        ...


class AdvancementPort(Protocol):
    # TODO-IMPL(AdvancementEngine.submit_exhaustion_proposal; source=#90)
    def submit_exhaustion_proposal(
        self, request: StageRunRequest, proposal: "ExhaustionProposal"
    ) -> FixtureOwnerReply:
        ...

    # TODO-IMPL(AdvancementEngine.reconcile_exhaustion_proposal; source=#90)
    def reconcile_exhaustion_proposal(
        self,
        request: StageRunRequest,
        proposal_submission_ref: str,
        expected_proposal_hash: str,
    ) -> FixtureOwnerReply:
        ...


@dataclass
class CallLedger:
    events: List[str] = field(default_factory=list)

    def record(self, operation: str) -> None:
        self.events.append(operation)


@dataclass
class FakeInvocationPort:
    ledger: CallLedger
    request: StageRunRequest
    context_pack: FrozenContextPack
    observed_requests: Tuple[StageRunRequest, ...] = ()
    request_current: bool = True
    observed_request_currentness: Tuple[bool, ...] = ()
    run_binding: Optional[IdeaRunBinding] = None
    observed_run_bindings: Tuple[IdeaRunBinding, ...] = ()
    run_binding_current: bool = True
    observed_run_binding_currentness: Tuple[bool, ...] = ()
    _observation_index: int = field(default=0, init=False)
    _request_currentness_index: int = field(default=0, init=False)
    _binding_observation_index: int = field(default=0, init=False)
    _binding_currentness_index: int = field(default=0, init=False)

    def observe_idea_stage_run(
        self, stage_run_request_ref: str
    ) -> StageRunRequest:
        _require_fixture_ref(stage_run_request_ref, "StageRunRequest ref")
        self.ledger.record("AdvancementEngine.observe_idea_stage_run")
        if self.observed_requests:
            index = min(self._observation_index, len(self.observed_requests) - 1)
            observed = self.observed_requests[index]
            self._observation_index += 1
        else:
            observed = self.request
        if observed.ref != stage_run_request_ref:
            raise ContractViolation("observed a different StageRunRequest")
        if self.observed_request_currentness:
            currentness_index = min(
                self._request_currentness_index,
                len(self.observed_request_currentness) - 1,
            )
            is_current = self.observed_request_currentness[currentness_index]
            self._request_currentness_index += 1
        else:
            is_current = self.request_current
        if is_current is not True:
            raise ContractViolation("Advancement Engine reports a stale StageRunRequest")
        return observed

    def observe_idea_run_binding(
        self, stage_run_request_ref: str
    ) -> IdeaRunBinding:
        _require_fixture_ref(stage_run_request_ref, "StageRunRequest ref")
        self.ledger.record("AgentRuntime.observe_idea_run_binding")
        if self.observed_run_bindings:
            index = min(
                self._binding_observation_index,
                len(self.observed_run_bindings) - 1,
            )
            binding = self.observed_run_bindings[index]
            self._binding_observation_index += 1
        else:
            binding = self.run_binding or make_run_binding(self.request)
        if binding.stage_run_request_ref != stage_run_request_ref:
            raise ContractViolation("observed a binding for another StageRunRequest")
        if self.observed_run_binding_currentness:
            currentness_index = min(
                self._binding_currentness_index,
                len(self.observed_run_binding_currentness) - 1,
            )
            is_current = self.observed_run_binding_currentness[currentness_index]
            self._binding_currentness_index += 1
        else:
            is_current = self.run_binding_current
        if is_current is not True:
            raise ContractViolation("Agent Runtime reports a stale execution fence")
        return binding

    def verify_delivered_context_pack(
        self,
        context_pack_ref: str,
        expected_sha256: str,
    ) -> FrozenContextPack:
        _require_fixture_ref(context_pack_ref, "ContextPack ref")
        _require_hash(expected_sha256, "expected ContextPack hash")
        self.ledger.record("AgentRuntime.verify_delivered_context_pack")
        if self.context_pack.ref != context_pack_ref:
            raise ContractViolation("read a different ContextPack")
        if self.context_pack.content_sha256 != expected_sha256:
            raise ContractViolation("ContextPack read does not match the expected hash")
        return self.context_pack


def _validate_reply_submission(
    reply: FixtureOwnerReply, submission: SubmissionIdentity
) -> None:
    reply.validate()
    if reply.submission_ref is not None and reply.submission_ref != submission.ref:
        raise ContractViolation("Owner reply is bound to another submission identity")


@dataclass
class FakeContentPort:
    ledger: CallLedger
    reply: FixtureOwnerReply
    technical_failure: Optional[str] = None
    reconcile_reply: Optional[FixtureOwnerReply] = None
    reconcile_technical_failure: Optional[str] = None

    def accept_idea_outcome_content(
        self,
        request: StageRunRequest,
        submission: SubmissionIdentity,
        outcome: IdeaOutcome,
        review: ReviewEvidence,
    ) -> FixtureOwnerReply:
        self.ledger.record("ResearchMemory.accept_idea_outcome_content")
        if self.technical_failure:
            raise TechnicalPortError(self.technical_failure)
        request.validate()
        submission.validate()
        _validate_reply_submission(self.reply, submission)
        return self.reply

    def reconcile_idea_outcome_content(
        self, request: StageRunRequest, submission: SubmissionIdentity
    ) -> FixtureOwnerReply:
        self.ledger.record("ResearchMemory.reconcile_idea_outcome_content")
        if self.reconcile_technical_failure:
            raise TechnicalPortError(self.reconcile_technical_failure)
        request.validate()
        submission.validate()
        reply = self.reconcile_reply or self.reply
        _validate_reply_submission(reply, submission)
        return reply


@dataclass
class FakeDomainPort:
    ledger: CallLedger
    reply: FixtureOwnerReply
    technical_failure: Optional[str] = None
    reconcile_reply: Optional[FixtureOwnerReply] = None
    reconcile_technical_failure: Optional[str] = None

    def submit_idea_outcome(
        self,
        request: StageRunRequest,
        submission: SubmissionIdentity,
        content_ref: str,
        content_receipt_ref: str,
        outcome: IdeaOutcome,
    ) -> FixtureOwnerReply:
        self.ledger.record("ResearchGraph.submit_idea_outcome")
        if self.technical_failure:
            raise TechnicalPortError(self.technical_failure)
        request.validate()
        submission.validate()
        _require_fixture_ref(content_ref, "accepted content ref")
        _require_fixture_ref(content_receipt_ref, "content acceptance receipt ref")
        _validate_reply_submission(self.reply, submission)
        return self.reply

    def reconcile_idea_outcome(
        self, request: StageRunRequest, submission: SubmissionIdentity
    ) -> FixtureOwnerReply:
        self.ledger.record("ResearchGraph.reconcile_idea_outcome")
        if self.reconcile_technical_failure:
            raise TechnicalPortError(self.reconcile_technical_failure)
        request.validate()
        submission.validate()
        reply = self.reconcile_reply or self.reply
        _validate_reply_submission(reply, submission)
        return reply


@dataclass
class FakeRuntimePort:
    ledger: CallLedger
    observation: Optional[FixtureRunObservation] = None
    observations: Tuple[FixtureRunObservation, ...] = ()
    reported_blocker_refs: Tuple[str, ...] = ()
    _observation_index: int = field(default=0, init=False)
    _blocker_index: int = field(default=0, init=False)

    def observe_run(self, verified: VerifiedInvocation) -> FixtureRunObservation:
        self.ledger.record("AgentRuntime.observe_run")
        request = verified.request
        run_binding = verified.run_binding
        request.validate()
        if self.observations:
            index = min(self._observation_index, len(self.observations) - 1)
            observation = self.observations[index]
            self._observation_index += 1
        else:
            observation = self.observation or FixtureRunObservation(
                run_ref=run_binding.run_ref,
                root_session_ref=run_binding.root_session_ref,
                execution_fence_ref=run_binding.execution_fence_ref,
            )
        observation.validate(run_binding)
        return observation

    def report_execution_blocker(
        self, request: StageRunRequest, blocker: str
    ) -> str:
        self.ledger.record("AgentRuntime.report_execution_blocker")
        request.validate()
        _require_text(blocker, "technical blocker")
        if self.reported_blocker_refs:
            index = min(self._blocker_index, len(self.reported_blocker_refs) - 1)
            blocker_ref = self.reported_blocker_refs[index]
            self._blocker_index += 1
        else:
            blocker_ref = "fixture:ar/blocker/1"
        _require_fixture_ref(blocker_ref, "reported blocker ref")
        return blocker_ref


@dataclass
class FakeAdvancementPort:
    ledger: CallLedger
    reply: FixtureOwnerReply
    technical_failure: Optional[str] = None
    reconcile_reply: Optional[FixtureOwnerReply] = None
    reconcile_technical_failure: Optional[str] = None

    def submit_exhaustion_proposal(
        self, request: StageRunRequest, proposal: "ExhaustionProposal"
    ) -> FixtureOwnerReply:
        self.ledger.record("AdvancementEngine.submit_exhaustion_proposal")
        if self.technical_failure:
            raise TechnicalPortError(self.technical_failure)
        request.validate()
        self.reply.validate()
        return self.reply

    def reconcile_exhaustion_proposal(
        self,
        request: StageRunRequest,
        proposal_submission_ref: str,
        expected_proposal_hash: str,
    ) -> FixtureOwnerReply:
        self.ledger.record("AdvancementEngine.reconcile_exhaustion_proposal")
        if self.reconcile_technical_failure:
            raise TechnicalPortError(self.reconcile_technical_failure)
        request.validate()
        _require_fixture_ref(
            proposal_submission_ref, "exhaustion proposal submission ref"
        )
        _require_hash(expected_proposal_hash, "expected exhaustion proposal hash")
        reply = self.reconcile_reply or self.reply
        reply.validate()
        if (
            reply.submission_ref is not None
            and reply.submission_ref != proposal_submission_ref
        ):
            raise ContractViolation(
                "exhaustion reconciliation returned another submission"
            )
        return reply


@dataclass(frozen=True)
class ExhaustionReconciliationBinding:
    contract_id: str
    stage_run_request_ref: str
    run_ref: str
    root_session_ref: str
    execution_fence_ref: str
    context_pack_ref: str
    context_pack_sha256: str
    proposal_sha256: str

    def validate(self) -> None:
        if self.contract_id != IDEA_CONTRACT:
            raise ContractViolation("unknown exhaustion reconciliation contract")
        for label, ref in (
            ("bound StageRunRequest ref", self.stage_run_request_ref),
            ("bound Run ref", self.run_ref),
            ("bound root Session ref", self.root_session_ref),
            ("bound Execution Fence ref", self.execution_fence_ref),
            ("bound ContextPack ref", self.context_pack_ref),
        ):
            _require_fixture_ref(ref, label)
        _require_hash(self.context_pack_sha256, "bound ContextPack hash")
        _require_hash(self.proposal_sha256, "bound exhaustion proposal hash")

    def assert_matches(self, verified: VerifiedInvocation) -> None:
        self.validate()
        expected = (
            verified.request.contract_id,
            verified.request.ref,
            verified.run_binding.run_ref,
            verified.run_binding.root_session_ref,
            verified.run_binding.execution_fence_ref,
            verified.context_pack.ref,
            verified.context_pack.content_sha256,
        )
        actual = (
            self.contract_id,
            self.stage_run_request_ref,
            self.run_ref,
            self.root_session_ref,
            self.execution_fence_ref,
            self.context_pack_ref,
            self.context_pack_sha256,
        )
        if actual != expected:
            raise ContractViolation(
                "exhaustion reconciliation escaped its original execution binding"
            )


@dataclass(frozen=True)
class SubmissionResult:
    status: str
    decision_phase: Optional[str] = None
    content_ref: Optional[str] = None
    outcome_ref: Optional[str] = None
    exhaustion_proposal_ref: Optional[str] = None
    exhaustion_reconciliation_binding: Optional[
        ExhaustionReconciliationBinding
    ] = None
    reconciliation_required_phase: Optional[str] = None
    content_decision_receipt_ref: Optional[str] = None
    domain_decision_receipt_ref: Optional[str] = None
    content_decision_receipt_refs: Tuple[str, ...] = ()
    domain_decision_receipt_refs: Tuple[str, ...] = ()
    advancement_decision_receipt_ref: Optional[str] = None
    advancement_decision_receipt_refs: Tuple[str, ...] = ()
    blocker_ref: Optional[str] = None
    blocker_refs: Tuple[str, ...] = ()
    feedback: Tuple[str, ...] = ()
    human_request_ref: Optional[str] = None
    human_request_refs: Tuple[str, ...] = ()
    owner_recovery_receipt_refs: Tuple[str, ...] = ()
    submission_ref: Optional[str] = None
    simulated_domain_accepted: bool = False
    is_owner_fact: bool = False
    is_stage_advanced: bool = False
    stage_commit_created_by_skill: bool = False

    def validate(self, submission: Optional[SubmissionIdentity] = None) -> None:
        if self.decision_phase not in {"content", "domain", "advancement"}:
            raise ContractViolation("submission result requires a known Owner phase")
        for label, ref in (
            ("content ref", self.content_ref),
            ("outcome ref", self.outcome_ref),
            ("exhaustion proposal ref", self.exhaustion_proposal_ref),
            ("content decision receipt", self.content_decision_receipt_ref),
            ("domain decision receipt", self.domain_decision_receipt_ref),
            ("advancement decision receipt", self.advancement_decision_receipt_ref),
            ("blocker ref", self.blocker_ref),
            ("HumanRequest ref", self.human_request_ref),
            ("submission ref", self.submission_ref),
        ):
            if ref is not None:
                _require_fixture_ref(ref, label)
        for label, refs in (
            ("content decision receipt", self.content_decision_receipt_refs),
            ("domain decision receipt", self.domain_decision_receipt_refs),
            ("advancement decision receipt", self.advancement_decision_receipt_refs),
            ("blocker", self.blocker_refs),
            ("HumanRequest", self.human_request_refs),
            ("Owner recovery receipt", self.owner_recovery_receipt_refs),
        ):
            for ref in refs:
                _require_fixture_ref(ref, label + " ref")
        if (
            self.content_decision_receipt_ref is not None
            and self.content_decision_receipt_ref
            not in self.content_decision_receipt_refs
        ):
            raise ContractViolation("current content receipt is missing from history")
        if (
            self.domain_decision_receipt_ref is not None
            and self.domain_decision_receipt_ref not in self.domain_decision_receipt_refs
        ):
            raise ContractViolation("current domain receipt is missing from history")
        if (
            self.advancement_decision_receipt_ref is not None
            and self.advancement_decision_receipt_ref
            not in self.advancement_decision_receipt_refs
        ):
            raise ContractViolation("current advancement receipt is missing from history")
        if self.blocker_ref is not None and self.blocker_ref not in self.blocker_refs:
            raise ContractViolation("current blocker is missing from history")
        if (
            self.human_request_ref is not None
            and self.human_request_ref not in self.human_request_refs
        ):
            raise ContractViolation("current HumanRequest is missing from history")
        if self.is_owner_fact or self.is_stage_advanced or self.stage_commit_created_by_skill:
            raise ContractViolation("fixture result cannot claim Owner or Stage authority")

        if self.decision_phase == "advancement":
            if not isinstance(
                self.exhaustion_reconciliation_binding,
                ExhaustionReconciliationBinding,
            ):
                raise ContractViolation(
                    "Advancement result requires its immutable proposal binding"
                )
            self.exhaustion_reconciliation_binding.validate()
            owner_status = (
                self.status.removeprefix("exhaustion_proposal_")
                if self.status.startswith("exhaustion_proposal_")
                else None
            )
            if self.status == "technical_blocker":
                if self.blocker_ref is None:
                    raise ContractViolation(
                        "technical exhaustion result requires a blocker ref"
                    )
                if self.reconciliation_required_phase not in {None, "advancement"}:
                    raise ContractViolation(
                        "technical exhaustion result has an invalid recovery phase"
                    )
                if self.reconciliation_required_phase == "advancement" and (
                    self.submission_ref is None
                    or self.advancement_decision_receipt_ref is None
                    or not self.advancement_decision_receipt_refs
                ):
                    raise ContractViolation(
                        "Advancement reconciliation blocker requires the unknown receipt proof"
                    )
            elif owner_status not in OWNER_STATUSES:
                raise ContractViolation("unknown exhaustion proposal result status")
            else:
                if self.advancement_decision_receipt_ref is None:
                    raise ContractViolation(
                        "exhaustion proposal decision requires an AE receipt"
                    )
                if owner_status == "accepted":
                    if self.exhaustion_proposal_ref is None:
                        raise ContractViolation(
                            "accepted exhaustion proposal requires its stable ref"
                        )
                elif self.exhaustion_proposal_ref is not None:
                    raise ContractViolation(
                        "non-accepted exhaustion reply cannot expose a proposal ref"
                    )
                if owner_status == "rejected" and not self.feedback:
                    raise ContractViolation(
                        "rejected exhaustion proposal requires feedback"
                    )
                if owner_status == "needs_input" and self.human_request_ref is None:
                    raise ContractViolation(
                        "needs-input exhaustion proposal requires a HumanRequest"
                    )
                if owner_status == "outcome_unknown":
                    if (
                        self.reconciliation_required_phase != "advancement"
                        or self.submission_ref is None
                    ):
                        raise ContractViolation(
                            "unknown exhaustion proposal requires reconciliation identity"
                        )
                elif self.reconciliation_required_phase is not None:
                    raise ContractViolation(
                        "known exhaustion result cannot require reconciliation"
                    )
            if any(
                value is not None
                for value in (
                    self.content_ref,
                    self.outcome_ref,
                    self.content_decision_receipt_ref,
                    self.domain_decision_receipt_ref,
                )
            ):
                raise ContractViolation(
                    "Advancement result cannot expose content/domain acceptance"
                )
            if self.simulated_domain_accepted:
                raise ContractViolation(
                    "Advancement result cannot claim domain acceptance"
                )
            return

        if self.status not in OWNER_STATUSES | {"technical_blocker"}:
            raise ContractViolation("unknown submission result status")
        if not isinstance(submission, SubmissionIdentity):
            raise ContractViolation("content/domain result requires its identity")
        if self.submission_ref != submission.ref:
            raise ContractViolation("submission result is bound to another identity")
        if (
            self.exhaustion_proposal_ref is not None
            or self.exhaustion_reconciliation_binding is not None
            or self.advancement_decision_receipt_ref is not None
            or self.advancement_decision_receipt_refs
        ):
            raise ContractViolation(
                "content/domain result cannot expose Advancement acceptance"
            )
        if self.status == "technical_blocker":
            if self.blocker_ref is None:
                raise ContractViolation("technical blocker result requires a blocker ref")
            if self.reconciliation_required_phase not in {None, "content", "domain"}:
                raise ContractViolation("technical blocker has an invalid recovery phase")
        else:
            decision_receipt_ref = (
                self.content_decision_receipt_ref
                if self.decision_phase == "content"
                else self.domain_decision_receipt_ref
            )
            if decision_receipt_ref is None:
                raise ContractViolation("Owner decision result requires a receipt")
            if self.blocker_ref is not None:
                raise ContractViolation("Owner decision cannot expose a Runtime blocker")
        if self.status == "accepted":
            if self.decision_phase == "content":
                if self.content_ref is None or self.outcome_ref is not None:
                    raise ContractViolation("content acceptance has invalid accepted refs")
            elif (
                self.content_ref is None
                or self.content_decision_receipt_ref is None
                or self.outcome_ref is None
            ):
                raise ContractViolation(
                    "domain acceptance requires content and outcome accepted refs"
                )
        elif self.outcome_ref is not None:
            raise ContractViolation("a non-accepted result cannot expose an outcome ref")
        if self.status == "needs_input" and self.human_request_ref is None:
            raise ContractViolation("needs_input result requires a HumanRequest ref")
        if self.status == "outcome_unknown":
            if self.reconciliation_required_phase != self.decision_phase:
                raise ContractViolation(
                    "unknown result must retain its reconciliation phase"
                )
        elif self.status != "technical_blocker" and (
            self.reconciliation_required_phase is not None
        ):
            raise ContractViolation(
                "a known Owner result cannot retain reconciliation-required state"
            )
        expected_simulated_acceptance = (
            self.status == "accepted" and self.decision_phase == "domain"
        )
        if self.simulated_domain_accepted is not expected_simulated_acceptance:
            raise ContractViolation("fixture acceptance marker drifted from Owner result")


def _result_from_owner_reply(
    reply: FixtureOwnerReply,
    submission: SubmissionIdentity,
    phase: str,
    content_ref: Optional[str] = None,
    content_decision_receipt_ref: Optional[str] = None,
    content_decision_receipt_refs: Tuple[str, ...] = (),
    domain_decision_receipt_refs: Tuple[str, ...] = (),
    human_request_refs: Tuple[str, ...] = (),
    owner_recovery_receipt_refs: Tuple[str, ...] = (),
    blocker_refs: Tuple[str, ...] = (),
) -> SubmissionResult:
    _validate_reply_submission(reply, submission)
    if phase not in {"content", "domain"}:
        raise ContractViolation("unknown Owner decision phase")
    retained_human_request_refs = human_request_refs
    if (
        reply.human_request_ref is not None
        and reply.human_request_ref not in retained_human_request_refs
    ):
        retained_human_request_refs += (reply.human_request_ref,)
    return SubmissionResult(
        status=reply.status,
        decision_phase=phase,
        content_ref=(
            reply.accepted_ref
            if phase == "content" and reply.status == "accepted"
            else content_ref
        ),
        outcome_ref=(
            reply.accepted_ref
            if phase == "domain" and reply.status == "accepted"
            else None
        ),
        reconciliation_required_phase=(
            phase if reply.status == "outcome_unknown" else None
        ),
        content_decision_receipt_ref=(
            reply.receipt_ref if phase == "content" else content_decision_receipt_ref
        ),
        domain_decision_receipt_ref=(
            reply.receipt_ref if phase == "domain" else None
        ),
        content_decision_receipt_refs=content_decision_receipt_refs,
        domain_decision_receipt_refs=domain_decision_receipt_refs,
        feedback=reply.feedback,
        human_request_ref=reply.human_request_ref,
        human_request_refs=retained_human_request_refs,
        owner_recovery_receipt_refs=owner_recovery_receipt_refs,
        blocker_refs=blocker_refs,
        submission_ref=reply.submission_ref or submission.ref,
        simulated_domain_accepted=(phase == "domain" and reply.status == "accepted"),
    )


def _retain_decision_receipt(
    receipts: Tuple[str, ...], reply: FixtureOwnerReply
) -> Tuple[str, ...]:
    if reply.receipt_ref is None or reply.receipt_ref in receipts:
        return receipts
    return receipts + (reply.receipt_ref,)


def _merge_decision_receipts(
    *receipt_groups: Tuple[str, ...]
) -> Tuple[str, ...]:
    merged: Tuple[str, ...] = ()
    for receipt_group in receipt_groups:
        for receipt_ref in receipt_group:
            if receipt_ref not in merged:
                merged += (receipt_ref,)
    return merged


def verify_same_feedback_loop(
    previous: VerifiedInvocation, current: VerifiedInvocation
) -> None:
    fields = (
        "contract_id",
        "ref",
        "stage",
        "quest_ref",
        "cycle_ref",
        "question_ref",
        "quest_goal_revision_ref",
        "foreground_epoch_ref",
        "context_pack_ref",
        "context_pack_sha256",
        "question_literature_revision_ref",
    )
    for field_name in fields:
        if getattr(previous.request, field_name) != getattr(
            current.request, field_name
        ):
            raise ContractViolation(
                "Owner-feedback revision escaped the same Run/root Session/fence"
            )
    binding_fields = (
        "ref",
        "run_ref",
        "attempt_ref",
        "root_session_ref",
        "execution_fence_ref",
        "stage_run_request_ref",
        "stage_run_request_hash",
        "context_pack_ref",
        "context_pack_sha256",
        "launch_manifest_ref",
        "runtime_observation_ref",
    )
    for field_name in binding_fields:
        if getattr(previous.run_binding, field_name) != getattr(
            current.run_binding, field_name
        ):
            raise ContractViolation(
                "Owner-feedback revision escaped the same Run/root Session/fence"
            )


def load_verified_invocation(
    stage_run_request_ref: Any,
    context_pack_ref: Any,
    expected_context_pack_sha256: Any,
    invocation_port: InvocationPort,
) -> VerifiedInvocation:
    _require_fixture_ref(stage_run_request_ref, "StageRunRequest ref")
    _require_fixture_ref(context_pack_ref, "ContextPack ref")
    _require_hash(expected_context_pack_sha256, "expected ContextPack hash")
    request = invocation_port.observe_idea_stage_run(stage_run_request_ref)
    if not isinstance(request, StageRunRequest):
        raise ContractViolation("observed request must be a typed StageRunRequest")
    request.validate()
    if request.context_pack_ref != context_pack_ref:
        raise ContractViolation("requested ContextPack ref does not match the request")
    if request.context_pack_sha256 != expected_context_pack_sha256:
        raise ContractViolation("requested ContextPack hash does not match the request")
    run_binding = invocation_port.observe_idea_run_binding(stage_run_request_ref)
    context_pack = invocation_port.verify_delivered_context_pack(
        context_pack_ref, expected_context_pack_sha256
    )
    return verify_invocation(request, run_binding, context_pack)


def revalidate_current_root(
    previous: VerifiedInvocation, invocation_port: InvocationPort
) -> VerifiedInvocation:
    current_request = invocation_port.observe_idea_stage_run(previous.request.ref)
    if not isinstance(current_request, StageRunRequest):
        raise ContractViolation("re-observed request must remain typed")
    current_request.validate()
    current_binding = invocation_port.observe_idea_run_binding(
        previous.request.ref
    )
    current = verify_invocation(
        current_request, current_binding, previous.context_pack
    )
    verify_same_feedback_loop(previous, current)
    return current


def submit_reviewed_outcome(
    stage_run_request_ref: Any,
    context_pack_ref: Any,
    expected_context_pack_sha256: Any,
    submission: SubmissionIdentity,
    reviewed: ReviewedOutcome,
    invocation_port: InvocationPort,
    content_port: IdeaContentPort,
    domain_port: IdeaDomainPort,
    runtime_port: RuntimePort,
    identity_registry: SubmissionIdentityRegistry,
) -> SubmissionResult:
    if not isinstance(submission, SubmissionIdentity):
        raise ContractViolation("formal submission requires a typed identity")
    if not isinstance(reviewed, ReviewedOutcome):
        raise ContractViolation("formal submission requires a ReviewedOutcome")
    if not isinstance(
        reviewed.review, (AdvisoryReviewRecord, OwnerFeedbackRevisionRecord)
    ):
        raise ContractViolation("formal submission requires typed review evidence")
    if not isinstance(identity_registry, SubmissionIdentityRegistry):
        raise ContractViolation("formal submission requires an idempotency registry")
    verified = load_verified_invocation(
        stage_run_request_ref,
        context_pack_ref,
        expected_context_pack_sha256,
        invocation_port,
    )
    reviewed.validate(verified, submission)
    observation = runtime_port.observe_run(verified)
    observation.validate(verified.run_binding)
    previous_result = identity_registry.bind(
        submission,
        submission_payload_hash(verified, submission, reviewed),
        feedback_loop_hash(verified),
        canonical_hash(reviewed.final_outcome),
        reviewed.review,
        observation,
    )
    if previous_result is not None and previous_result.status in {
        "accepted",
        "rejected",
    }:
        revalidate_current_root(verified, invocation_port)
        return previous_result
    if previous_result is not None and previous_result.status == "needs_input":
        if not observation.recovery_dispositions:
            revalidate_current_root(verified, invocation_port)
            return previous_result
        observation.assert_owner_recovery(previous_result.human_request_ref)
    if (
        previous_result is not None
        and (
            previous_result.status == "outcome_unknown"
            or previous_result.reconciliation_required_phase is not None
        )
    ):
        observation.assert_reconciliation_ready()
    else:
        observation.assert_formal_write_ready()
    transition_kind = (
        "reconciliation"
        if previous_result is not None
        and (
            previous_result.status == "outcome_unknown"
            or previous_result.reconciliation_required_phase is not None
        )
        else "owner_write"
    )
    transition_proof = identity_registry._issue_transition_proof(
        submission,
        transition_kind,
        recovery_observation=observation,
    )
    current = revalidate_current_root(verified, invocation_port)
    (
        content_ref,
        content_receipt_ref,
        checkpoint_receipt_refs,
    ) = identity_registry.content_checkpoint(submission)
    decision_phase = "domain" if content_ref is not None else "content"
    content_was_reconciled = False
    reconciliation_required_phase = (
        previous_result.reconciliation_required_phase
        if previous_result is not None
        else None
    )
    content_decision_receipt_refs = _merge_decision_receipts(
        checkpoint_receipt_refs,
        (
            previous_result.content_decision_receipt_refs
            if previous_result is not None
            else ()
        ),
    )
    domain_decision_receipt_refs = (
        previous_result.domain_decision_receipt_refs
        if previous_result is not None
        else ()
    )
    (
        human_request_refs,
        owner_recovery_receipt_refs,
        blocker_refs,
    ) = identity_registry.audit_histories(submission)
    try:
        content_reply: Optional[FixtureOwnerReply] = None
        if previous_result is not None and (
            previous_result.status == "outcome_unknown"
            or previous_result.reconciliation_required_phase is not None
        ):
            required_phase = (
                previous_result.reconciliation_required_phase
                or previous_result.decision_phase
            )
            if required_phase == "domain":
                if content_ref is None or content_receipt_ref is None:
                    raise ContractViolation(
                        "domain reconciliation requires accepted content and receipt refs"
                    )
                decision_phase = "domain"
                reconciliation_required_phase = "domain"
                domain_reply = domain_port.reconcile_idea_outcome(
                    current.request, submission
                )
                _validate_reply_submission(domain_reply, submission)
                domain_decision_receipt_refs = _retain_decision_receipt(
                    domain_decision_receipt_refs, domain_reply
                )
                result = _result_from_owner_reply(
                    domain_reply,
                    submission,
                    phase="domain",
                    content_ref=content_ref,
                    content_decision_receipt_ref=content_receipt_ref,
                    content_decision_receipt_refs=(
                        content_decision_receipt_refs
                    ),
                    domain_decision_receipt_refs=domain_decision_receipt_refs,
                    human_request_refs=human_request_refs,
                    owner_recovery_receipt_refs=owner_recovery_receipt_refs,
                    blocker_refs=blocker_refs,
                )
                identity_registry.record_result(
                    submission, result, transition_proof=transition_proof
                )
                return result
            if required_phase != "content":
                raise ContractViolation("unknown result has no reconcilable Owner phase")
            decision_phase = "content"
            reconciliation_required_phase = "content"
            content_reply = content_port.reconcile_idea_outcome_content(
                current.request, submission
            )
            content_was_reconciled = True
        elif content_ref is None:
            content_reply = content_port.accept_idea_outcome_content(
                current.request,
                submission,
                reviewed.final_outcome,
                reviewed.review,
            )
        if content_reply is not None:
            _validate_reply_submission(content_reply, submission)
            content_decision_receipt_refs = _retain_decision_receipt(
                content_decision_receipt_refs, content_reply
            )
            if (
                content_reply.status == "outcome_unknown"
                and not content_was_reconciled
            ):
                reconciliation_required_phase = "content"
                content_reply = content_port.reconcile_idea_outcome_content(
                    current.request, submission
                )
                content_was_reconciled = True
                _validate_reply_submission(content_reply, submission)
                content_decision_receipt_refs = _retain_decision_receipt(
                    content_decision_receipt_refs, content_reply
                )
            if content_was_reconciled and content_reply.status != "outcome_unknown":
                reconciliation_required_phase = None
            if content_reply.status != "accepted":
                result = _result_from_owner_reply(
                    content_reply,
                    submission,
                    phase="content",
                    content_decision_receipt_refs=content_decision_receipt_refs,
                    domain_decision_receipt_refs=domain_decision_receipt_refs,
                    human_request_refs=human_request_refs,
                    owner_recovery_receipt_refs=owner_recovery_receipt_refs,
                    blocker_refs=blocker_refs,
                )
                identity_registry.record_result(
                    submission, result, transition_proof=transition_proof
                )
                return result
            content_ref = content_reply.accepted_ref
            content_receipt_ref = content_reply.receipt_ref
            assert content_ref is not None and content_receipt_ref is not None
            identity_registry.record_content_checkpoint(
                submission,
                content_ref,
                content_receipt_ref,
                content_decision_receipt_refs,
            )
        if content_ref is None or content_receipt_ref is None:
            raise ContractViolation("domain submit requires accepted content checkpoint")
        decision_phase = "domain"
        domain_observation = runtime_port.observe_run(current)
        domain_observation.validate(current.run_binding)
        domain_observation.assert_formal_write_ready()
        current = revalidate_current_root(current, invocation_port)
        domain_reply = domain_port.submit_idea_outcome(
            current.request,
            submission,
            content_ref,
            content_receipt_ref,
            reviewed.final_outcome,
        )
        _validate_reply_submission(domain_reply, submission)
        domain_decision_receipt_refs = _retain_decision_receipt(
            domain_decision_receipt_refs, domain_reply
        )
        if domain_reply.status == "outcome_unknown":
            reconciliation_required_phase = "domain"
            domain_reply = domain_port.reconcile_idea_outcome(
                current.request, submission
            )
            _validate_reply_submission(domain_reply, submission)
            domain_decision_receipt_refs = _retain_decision_receipt(
                domain_decision_receipt_refs, domain_reply
            )
        result = _result_from_owner_reply(
            domain_reply,
            submission,
            phase="domain",
            content_ref=content_ref,
            content_decision_receipt_ref=content_receipt_ref,
            content_decision_receipt_refs=content_decision_receipt_refs,
            domain_decision_receipt_refs=domain_decision_receipt_refs,
            human_request_refs=human_request_refs,
            owner_recovery_receipt_refs=owner_recovery_receipt_refs,
            blocker_refs=blocker_refs,
        )
        identity_registry.record_result(
            submission, result, transition_proof=transition_proof
        )
        return result
    except TechnicalPortError as exc:
        blocker_ref = runtime_port.report_execution_blocker(
            current.request, str(exc)
        )
        if blocker_ref not in blocker_refs:
            blocker_refs += (blocker_ref,)
        result = SubmissionResult(
            status="technical_blocker",
            decision_phase=decision_phase,
            content_ref=content_ref,
            reconciliation_required_phase=reconciliation_required_phase,
            content_decision_receipt_ref=content_receipt_ref,
            content_decision_receipt_refs=content_decision_receipt_refs,
            domain_decision_receipt_refs=domain_decision_receipt_refs,
            submission_ref=submission.ref,
            blocker_ref=blocker_ref,
            blocker_refs=blocker_refs,
            human_request_refs=human_request_refs,
            owner_recovery_receipt_refs=owner_recovery_receipt_refs,
        )
        technical_proof = identity_registry._issue_transition_proof(
            submission,
            "technical",
            recovery_observation=observation,
        )
        identity_registry.record_result(
            submission, result, transition_proof=technical_proof
        )
        return result


@dataclass(frozen=True)
class ExhaustionClosure:
    exploration_record_refs: Tuple[str, ...]
    prior_submission_refs: Tuple[str, ...]
    owner_rejection_receipt_refs: Tuple[str, ...]
    cannot_form_idea_set_reason: str
    cannot_form_no_viable_reason: str
    pending_submission_refs: Tuple[str, ...] = ()
    accepted_unconsumed_outcome_refs: Tuple[str, ...] = ()
    human_request_refs: Tuple[str, ...] = ()
    technical_blocker_refs: Tuple[str, ...] = ()
    outcome_unknown_refs: Tuple[str, ...] = ()
    existing_stage_commit_ref: Optional[str] = None
    defensible_idea_set_available: bool = False
    defensible_no_viable_available: bool = False
    run_reconciled: bool = True

    def validate(
        self, observation: Optional[FixtureRunObservation] = None
    ) -> None:
        if not self.exploration_record_refs:
            raise ContractViolation("exhaustion requires exploration records")
        for label, refs in (
            ("exploration record", self.exploration_record_refs),
            ("prior submission", self.prior_submission_refs),
            ("Owner rejection receipt", self.owner_rejection_receipt_refs),
            ("pending submission", self.pending_submission_refs),
            ("accepted unconsumed outcome", self.accepted_unconsumed_outcome_refs),
            ("HumanRequest", self.human_request_refs),
            ("technical blocker", self.technical_blocker_refs),
            ("unknown outcome", self.outcome_unknown_refs),
        ):
            for ref in refs:
                _require_fixture_ref(ref, label + " ref")
        _require_text(
            self.cannot_form_idea_set_reason, "cannot-form-IdeaSet reason"
        )
        _require_text(
            self.cannot_form_no_viable_reason,
            "cannot-form-NoViableCandidate reason",
        )
        if observation is not None:
            observation_fields = (
                "prior_submission_refs",
                "owner_rejection_receipt_refs",
                "pending_submission_refs",
                "accepted_unconsumed_outcome_refs",
                "human_request_refs",
                "technical_blocker_refs",
                "outcome_unknown_refs",
                "existing_stage_commit_ref",
                "run_reconciled",
            )
            for field_name in observation_fields:
                if getattr(self, field_name) != getattr(observation, field_name):
                    raise ContractViolation(
                        "exhaustion closure disagrees with the observed Run"
                    )
        if self.defensible_idea_set_available or self.defensible_no_viable_available:
            raise ContractViolation("a defensible IdeaOutcome blocks exhaustion")
        if (
            self.pending_submission_refs
            or self.accepted_unconsumed_outcome_refs
            or self.human_request_refs
            or self.technical_blocker_refs
            or self.outcome_unknown_refs
        ):
            raise ContractViolation("an unresolved dependency blocks exhaustion")
        if self.existing_stage_commit_ref is not None:
            _require_fixture_ref(
                self.existing_stage_commit_ref, "existing StageCommit ref"
            )
            raise ContractViolation("an existing StageCommit blocks exhaustion")
        if self.run_reconciled is not True:
            raise ContractViolation("Run must be reconciled before exhaustion")


@dataclass(frozen=True)
class ExhaustionProposal:
    stage_run_request_ref: str
    run_ref: str
    root_session_ref: str
    context_pack_ref: str
    closure: ExhaustionClosure
    authoritative: bool = False
    kind: str = field(default="ExhaustionProposal", init=False)

    def validate(self, verified: VerifiedInvocation) -> None:
        for label, ref in (
            ("proposal StageRunRequest ref", self.stage_run_request_ref),
            ("proposal Run ref", self.run_ref),
            ("proposal root Session ref", self.root_session_ref),
            ("proposal ContextPack ref", self.context_pack_ref),
        ):
            _require_fixture_ref(ref, label)
        if self.stage_run_request_ref != verified.request.ref:
            raise ContractViolation("exhaustion proposal binds a different request")
        if self.run_ref != verified.run_binding.run_ref:
            raise ContractViolation("exhaustion proposal binds a different Run")
        if self.root_session_ref != verified.run_binding.root_session_ref:
            raise ContractViolation("exhaustion proposal binds a different root Session")
        if self.context_pack_ref != verified.context_pack.ref:
            raise ContractViolation("exhaustion proposal binds a different ContextPack")
        if self.authoritative is not False:
            raise ContractViolation("the root Agent cannot make exhaustion authoritative")
        self.closure.validate()


def build_exhaustion_proposal(
    verified: VerifiedInvocation, closure: ExhaustionClosure
) -> ExhaustionProposal:
    proposal = ExhaustionProposal(
        stage_run_request_ref=verified.request.ref,
        run_ref=verified.run_binding.run_ref,
        root_session_ref=verified.run_binding.root_session_ref,
        context_pack_ref=verified.context_pack.ref,
        closure=closure,
    )
    proposal.validate(verified)
    return proposal


def submit_exhaustion_proposal(
    stage_run_request_ref: Any,
    context_pack_ref: Any,
    expected_context_pack_sha256: Any,
    closure: ExhaustionClosure,
    invocation_port: InvocationPort,
    runtime_port: RuntimePort,
    advancement_port: AdvancementPort,
) -> SubmissionResult:
    verified = load_verified_invocation(
        stage_run_request_ref,
        context_pack_ref,
        expected_context_pack_sha256,
        invocation_port,
    )
    observation = runtime_port.observe_run(verified)
    observation.validate(verified.run_binding)
    closure.validate(observation)
    current = revalidate_current_root(verified, invocation_port)
    proposal = build_exhaustion_proposal(current, closure)
    reconciliation_binding = ExhaustionReconciliationBinding(
        contract_id=current.request.contract_id,
        stage_run_request_ref=current.request.ref,
        run_ref=current.run_binding.run_ref,
        root_session_ref=current.run_binding.root_session_ref,
        execution_fence_ref=current.run_binding.execution_fence_ref,
        context_pack_ref=current.context_pack.ref,
        context_pack_sha256=current.context_pack.content_sha256,
        proposal_sha256=canonical_hash(proposal),
    )
    reconciliation_binding.validate()
    decision_receipt_refs: Tuple[str, ...] = ()
    blocker_refs: Tuple[str, ...] = ()
    human_request_refs: Tuple[str, ...] = ()
    proposal_submission_ref: Optional[str] = None
    reconciliation_required_phase: Optional[str] = None
    try:
        reply = advancement_port.submit_exhaustion_proposal(
            current.request, proposal
        )
        reply.validate()
        decision_receipt_refs = _retain_decision_receipt(
            decision_receipt_refs, reply
        )
        if reply.status == "outcome_unknown":
            proposal_submission_ref = reply.submission_ref
            assert proposal_submission_ref is not None
            reconciliation_required_phase = "advancement"
            reply = advancement_port.reconcile_exhaustion_proposal(
                current.request,
                proposal_submission_ref,
                reconciliation_binding.proposal_sha256,
            )
            reply.validate()
            decision_receipt_refs = _retain_decision_receipt(
                decision_receipt_refs, reply
            )
            if reply.status != "outcome_unknown":
                reconciliation_required_phase = None
        if reply.human_request_ref is not None:
            human_request_refs = (reply.human_request_ref,)
        result = SubmissionResult(
            status="exhaustion_proposal_" + reply.status,
            decision_phase="advancement",
            exhaustion_proposal_ref=(
                reply.accepted_ref if reply.status == "accepted" else None
            ),
            exhaustion_reconciliation_binding=reconciliation_binding,
            reconciliation_required_phase=(
                "advancement"
                if reply.status == "outcome_unknown"
                else reconciliation_required_phase
            ),
            advancement_decision_receipt_ref=reply.receipt_ref,
            advancement_decision_receipt_refs=decision_receipt_refs,
            feedback=reply.feedback,
            human_request_ref=reply.human_request_ref,
            human_request_refs=human_request_refs,
            submission_ref=proposal_submission_ref or reply.submission_ref,
        )
        result.validate()
        return result
    except TechnicalPortError as exc:
        blocker_ref = runtime_port.report_execution_blocker(
            current.request, str(exc)
        )
        blocker_refs = (blocker_ref,)
        result = SubmissionResult(
            status="technical_blocker",
            decision_phase="advancement",
            exhaustion_reconciliation_binding=reconciliation_binding,
            reconciliation_required_phase=reconciliation_required_phase,
            advancement_decision_receipt_ref=(
                decision_receipt_refs[-1] if decision_receipt_refs else None
            ),
            advancement_decision_receipt_refs=decision_receipt_refs,
            blocker_ref=blocker_ref,
            blocker_refs=blocker_refs,
            submission_ref=proposal_submission_ref,
        )
        result.validate()
        return result


def reconcile_exhaustion_proposal(
    stage_run_request_ref: Any,
    context_pack_ref: Any,
    expected_context_pack_sha256: Any,
    previous_result: SubmissionResult,
    invocation_port: InvocationPort,
    runtime_port: RuntimePort,
    advancement_port: AdvancementPort,
) -> SubmissionResult:
    if not isinstance(previous_result, SubmissionResult):
        raise ContractViolation("exhaustion reconciliation requires a typed prior result")
    previous_result.validate()
    if (
        previous_result.decision_phase != "advancement"
        or previous_result.reconciliation_required_phase != "advancement"
        or previous_result.submission_ref is None
        or previous_result.status
        not in {"exhaustion_proposal_outcome_unknown", "technical_blocker"}
    ):
        raise ContractViolation(
            "only an unresolved exhaustion proposal can be reconciled"
        )
    verified = load_verified_invocation(
        stage_run_request_ref,
        context_pack_ref,
        expected_context_pack_sha256,
        invocation_port,
    )
    reconciliation_binding = previous_result.exhaustion_reconciliation_binding
    assert isinstance(
        reconciliation_binding, ExhaustionReconciliationBinding
    )
    reconciliation_binding.assert_matches(verified)
    if (
        not previous_result.advancement_decision_receipt_refs
        or previous_result.advancement_decision_receipt_ref is None
    ):
        raise ContractViolation(
            "exhaustion reconciliation requires the prior unknown receipt proof"
        )
    observation = runtime_port.observe_run(verified)
    observation.validate(verified.run_binding)
    observation.assert_reconciliation_ready()
    current = revalidate_current_root(verified, invocation_port)
    decision_receipt_refs = previous_result.advancement_decision_receipt_refs
    blocker_refs = previous_result.blocker_refs
    human_request_refs = previous_result.human_request_refs
    try:
        reply = advancement_port.reconcile_exhaustion_proposal(
            current.request,
            previous_result.submission_ref,
            reconciliation_binding.proposal_sha256,
        )
        reply.validate()
        decision_receipt_refs = _retain_decision_receipt(
            decision_receipt_refs, reply
        )
        if (
            reply.human_request_ref is not None
            and reply.human_request_ref not in human_request_refs
        ):
            human_request_refs += (reply.human_request_ref,)
        result = SubmissionResult(
            status="exhaustion_proposal_" + reply.status,
            decision_phase="advancement",
            exhaustion_proposal_ref=(
                reply.accepted_ref if reply.status == "accepted" else None
            ),
            exhaustion_reconciliation_binding=reconciliation_binding,
            reconciliation_required_phase=(
                "advancement" if reply.status == "outcome_unknown" else None
            ),
            advancement_decision_receipt_ref=reply.receipt_ref,
            advancement_decision_receipt_refs=decision_receipt_refs,
            blocker_refs=blocker_refs,
            feedback=reply.feedback,
            human_request_ref=reply.human_request_ref,
            human_request_refs=human_request_refs,
            submission_ref=previous_result.submission_ref,
        )
        result.validate()
        return result
    except TechnicalPortError as exc:
        blocker_ref = runtime_port.report_execution_blocker(
            current.request, str(exc)
        )
        if blocker_ref not in blocker_refs:
            blocker_refs += (blocker_ref,)
        result = SubmissionResult(
            status="technical_blocker",
            decision_phase="advancement",
            exhaustion_reconciliation_binding=reconciliation_binding,
            reconciliation_required_phase="advancement",
            advancement_decision_receipt_ref=(
                decision_receipt_refs[-1] if decision_receipt_refs else None
            ),
            advancement_decision_receipt_refs=decision_receipt_refs,
            blocker_ref=blocker_ref,
            blocker_refs=blocker_refs,
            human_request_refs=human_request_refs,
            submission_ref=previous_result.submission_ref,
        )
        result.validate()
        return result


def fixture_bound_literature() -> BoundLiteratureAnchor:
    return BoundLiteratureAnchor(
        question_literature_revision_ref="fixture:rg/question-literature/q1-r1",
        revision_content_hash=fixture_hash("question-literature-q1-r1"),
        rm_accepted_receipt_ref="fixture:rm/receipt/literature-q1-r1",
        rg_question_association_receipt_ref="fixture:rg/receipt/literature-q1-r1",
        summary_ref="fixture:rm/literature-summary/q1-r1",
        papers_ref="fixture:rm/literature-papers/q1-r1",
        fulltext_manifest_ref="fixture:rm/fulltext-manifest/q1-r1",
    )


def fixture_request_and_pack(
    *,
    literature_anchor: Optional[LiteratureAnchor] = None,
) -> Tuple[StageRunRequest, FrozenContextPack]:
    request_ref = "fixture:ae/stage-run-request/idea-1"
    quest_ref = "fixture:rg/quest/quest-1"
    cycle_ref = "fixture:ae/cycle/cycle-1"
    question_ref = "fixture:rg/question/q1"
    goal_revision_ref = "fixture:rg/quest-goal/quest-1-r1"
    accepted_question_content_data = {
        "opaque_fixture_key": "accepted-value",
        "opaque_fixture_payload": [17, True, {"nested": "data"}],
    }
    accepted_question_binding = AcceptedQuestionBinding(
        question_ref=question_ref,
        quest_ref=quest_ref,
        question_content_ref="fixture:rm/question-content/q1-v1",
        question_content_hash=canonical_hash(accepted_question_content_data),
        question_content_schema_ref="fixture:schema/question-content/v1",
        rm_content_accepted_receipt_ref=(
            "fixture:rm/receipt/question-content-q1-v1"
        ),
        rg_question_accepted_receipt_ref="fixture:rg/receipt/question-q1",
    )
    goal_content = QuestGoalContent(
        goal_statement="Resolve the bounded research goal for Quest 1.",
        completion_milestones=("Produce an accepted bounded answer.",),
        exclusions=("Do not change the Quest Goal from the Idea stage.",),
    )
    quest_goal_anchor = QuestGoalAnchor(
        quest_ref=quest_ref,
        goal_revision_ref=goal_revision_ref,
        goal_content_ref="fixture:rm/quest-goal/quest-1-r1",
        goal_content_hash=canonical_hash(goal_content),
        goal_accepted_receipt_ref="fixture:rg/receipt/quest-goal-quest-1-r1",
        content=goal_content,
    )
    stable_history = StableReferenceBinding(
        semantic_role="idea.prior_research.history_basis",
        source_owner="ResearchMemory",
        object_ref="fixture:rm/history/q1-v1",
        content_hash=fixture_hash("history-q1-v1"),
        authority_proof_refs=("fixture:rm/receipt/history-q1-v1",),
    )
    accepted_evidence_one = StableReferenceBinding(
        semantic_role="idea.prior_research.accepted_evidence",
        source_owner="ResearchMemory",
        object_ref="fixture:rm/asset/evidence-1",
        content_hash=fixture_hash("accepted-evidence-1"),
        authority_proof_refs=("fixture:rm/receipt/evidence-1",),
    )
    accepted_evidence_two = StableReferenceBinding(
        semantic_role="idea.prior_research.accepted_evidence",
        source_owner="ResearchMemory",
        object_ref="fixture:rm/asset/evidence-2",
        content_hash=fixture_hash("accepted-evidence-2"),
        authority_proof_refs=("fixture:rm/receipt/evidence-2",),
    )
    selected_literature = literature_anchor or NoLiteratureAnchor()
    identity = ContextPackIdentity(
        schema_ref="fixture:schema/idea-context-pack/v1",
        schema_version="1",
        pack_ref="fixture:projection/context-pack/idea-1",
        stage="Idea",
        quest_ref=quest_ref,
        cycle_ref=cycle_ref,
        question_ref=question_ref,
        compilation_basis_refs=(
            "fixture:ae/compilation-basis/idea-1",
            accepted_question_binding.rg_question_accepted_receipt_ref,
            quest_goal_anchor.goal_accepted_receipt_ref,
        ),
    )
    pack = make_context_pack(
        identity=identity,
        accepted_question_binding=accepted_question_binding,
        accepted_question_content_data=accepted_question_content_data,
        quest_goal_anchor=quest_goal_anchor,
        literature_anchor=selected_literature,
        prior_research=PriorResearch(
            history_basis_refs=(stable_history,),
            accepted_idea_outcomes=(),
            accepted_formal_plan_refs=(),
            reasoning_conclusion_refs=(),
            accepted_evidence_refs=(accepted_evidence_one, accepted_evidence_two),
            prior_stage_commit_refs=(),
        ),
        active_guidance=ActiveGuidance(soft_constraint_bindings=()),
        navigation_roots=(
            NavigationRoot(
                binding_ref="fixture:ae/navigation/history-q1-v1",
                semantic_role="idea.navigation.history",
                source_owner="ResearchMemory",
                object_ref=stable_history.object_ref,
                content_hash=stable_history.content_hash,
                authority_proof_refs=stable_history.authority_proof_refs,
                requiredness="optional",
                data_classification="data_only",
                read_contract=ReadContract(
                    semantic_operation_id="research_memory.read_accepted_history",
                    content_schema_ref="fixture:schema/research-history/v1",
                    media_type="application/json",
                    unavailable_policy="preserve_unknown",
                ),
            ),
        ),
    )
    request = StageRunRequest(
        contract_id=IDEA_CONTRACT,
        ref=request_ref,
        stage="Idea",
        quest_ref=quest_ref,
        cycle_ref=cycle_ref,
        question_ref=question_ref,
        quest_goal_revision_ref=goal_revision_ref,
        foreground_epoch_ref="fixture:ae/epoch/epoch-1",
        context_pack_ref=pack.ref,
        context_pack_sha256=pack.content_sha256,
        question_literature_revision_ref=(
            selected_literature.question_literature_revision_ref
            if isinstance(selected_literature, BoundLiteratureAnchor)
            else None
        ),
    )
    return request, pack


def fixture_candidate(key: str = "c1") -> IdeaCandidate:
    return IdeaCandidate(
        candidate_key=key,
        direction=(
            "Suppress subject-identity information adversarially at the intermediate "
            "EEG representation while preserving task-predictive features."
        ),
        rationale=(
            "Subject identity may be a nuisance shortcut; separating it from task "
            "signal could improve transfer to unseen subjects."
        ),
        assumptions=("Subject identity is separable from task-relevant signal.",),
        risks=("Over-suppression may erase task-predictive subject variation.",),
        evidence_boundary=EvidenceBoundary(
            accepted_evidence_refs=("fixture:rm/asset/evidence-1",),
            supported=(
                "The bound accepted evidence establishes subject-linked representation "
                "shift in the baseline."
            ),
            inferred=(
                "Reducing that shift may improve unseen-subject classification; this "
                "causal link is not yet accepted evidence."
            ),
            unknown="Whether task signal survives the suppression remains unknown.",
        ),
        falsification_hint=FalsificationHint(
            test=(
                "Hold the task model family fixed and compare identity leakage, task "
                "signal, and unseen-subject behavior with and without suppression."
            ),
            would_refute=(
                "Identity leakage falls but unseen-subject behavior does not improve, "
                "or task-relevant signal degrades."
            ),
        ),
        material_difference=MaterialDifference(
            from_history=(
                "The bound history describes the shift but contains no intervention "
                "that separates identity from task signal."
            ),
            from_peers="Peer candidates intervene on alignment or normalization instead.",
            plan_commitment_change=(
                "Plan must test both identity suppression and task-signal preservation, "
                "not only aggregate classification change."
            ),
        ),
    )


def fixture_idea_set(candidate_count: int = 1) -> IdeaSet:
    mechanism_axes = (
        "identity-adversarial suppression",
        "subject-invariant contrastive alignment",
        "task-preserving conditional normalization",
        "nuisance-stratified representation gating",
    )
    candidates = tuple(
        replace(
            fixture_candidate("c{}".format(index + 1)),
            direction=(
                "Test {} as the mechanism for improving unseen-subject EEG behavior "
                "without erasing task signal."
            ).format(
                mechanism_axes[index]
                if index < len(mechanism_axes)
                else "mechanism axis {}".format(index + 1)
            ),
            material_difference=MaterialDifference(
                from_history="History has no intervention on axis {}.".format(index + 1),
                from_peers="This candidate changes axis {} rather than another axis.".format(
                    index + 1
                ),
                plan_commitment_change="Plan would freeze semantic delta {}.".format(
                    index + 1
                ),
            ),
        )
        for index in range(candidate_count)
    )
    return IdeaSet(
        question_ref="fixture:rg/question/q1",
        context_pack_ref="fixture:projection/context-pack/idea-1",
        candidates=candidates,
        recommendation=AdvisoryRecommendation(
            note="Candidate c1 appears cheapest to falsify first."
        ),
    )


def fixture_no_viable() -> NoViableCandidate:
    return NoViableCandidate(
        question_ref="fixture:rg/question/q1",
        context_pack_ref="fixture:projection/context-pack/idea-1",
        exploration_scope="All mechanism families allowed by the frozen constraints.",
        candidate_families_considered=(
            ConsideredFamily(
                family="Direct intervention families",
                why_not_viable="Every member violates the frozen safety constraint.",
                evidence_refs=("fixture:rm/asset/evidence-2",),
            ),
        ),
        evidence_boundary=EvidenceBoundary(
            accepted_evidence_refs=("fixture:rm/asset/evidence-2",),
            supported="The current constraint excludes the considered family.",
            inferred="Other families with the same requirement are also excluded.",
            unknown="A future relaxation could admit a new family.",
        ),
        overturn_conditions=("The frozen safety constraint is revised.",),
        why_plan_cannot_proceed="No considered family can satisfy the current contract.",
    )


def fixture_review(outcome: IdeaOutcome) -> ReviewedOutcome:
    finding = ReviewFinding(
        finding_id="f1",
        category="evidence_boundary",
        message="Keep the inferred mechanism separate from accepted evidence.",
    )
    disposition = ReviewDisposition(
        finding_id="f1",
        action="not_adopted",
        rationale="The draft already separates supported, inferred, and unknown claims.",
    )
    record = AdvisoryReviewRecord(
        review_ref="fixture:review/idea-1",
        reviewer_session_ref="fixture:ar/session/reviewer-1",
        reviewed_draft_hash=canonical_hash(outcome),
        final_outcome_hash=canonical_hash(outcome),
        findings=(finding,),
        dispositions=(disposition,),
    )
    return ReviewedOutcome(
        reviewed_draft=outcome,
        final_outcome=outcome,
        review=record,
    )


def run_fixture_scenario(name: str) -> Dict[str, Any]:
    request, pack = fixture_request_and_pack()
    ledger = CallLedger()
    invocation = FakeInvocationPort(ledger, request, pack)
    runtime = FakeRuntimePort(ledger)
    identity_registry = SubmissionIdentityRegistry()
    accepted_content = FixtureOwnerReply(
        status="accepted",
        receipt_ref="fixture:rm/receipt/content-1",
        accepted_ref="fixture:rm/asset/idea-content-1",
    )
    accepted_domain = FixtureOwnerReply(
        status="accepted",
        receipt_ref="fixture:rg/receipt/outcome-1",
        accepted_ref="fixture:rg/idea-outcome/1",
    )

    if name == "exhaustion":
        closure = ExhaustionClosure(
            exploration_record_refs=("fixture:agent/exploration/1",),
            prior_submission_refs=(),
            owner_rejection_receipt_refs=(),
            cannot_form_idea_set_reason="No materially distinct candidate remains.",
            cannot_form_no_viable_reason=(
                "The evidence cannot support a bounded negative outcome."
            ),
        )
        advancement = FakeAdvancementPort(
            ledger,
            FixtureOwnerReply(
                status="accepted",
                receipt_ref="fixture:ae/receipt/exhaustion-proposal-1",
                accepted_ref="fixture:ae/proposal/exhaustion-1",
                submission_ref="fixture:ae/proposal/exhaustion-1",
            ),
        )
        result = submit_exhaustion_proposal(
            request.ref,
            pack.ref,
            pack.content_sha256,
            closure,
            invocation,
            runtime,
            advancement,
        )
    else:
        outcome: IdeaOutcome
        outcome = fixture_no_viable() if name == "no-viable" else fixture_idea_set()
        domain_reply = accepted_domain
        technical_failure = None
        if name == "rejected":
            domain_reply = FixtureOwnerReply(
                status="rejected",
                receipt_ref="fixture:rg/receipt/rejection-1",
                feedback=("Clarify the evidence boundary.",),
            )
        elif name == "stale":
            domain_reply = FixtureOwnerReply(
                status="stale", receipt_ref="fixture:rg/receipt/stale-1"
            )
        elif name == "needs-input":
            domain_reply = FixtureOwnerReply(
                status="needs_input",
                receipt_ref="fixture:rg/receipt/needs-input-1",
                human_request_ref="fixture:hc/human-request/1",
            )
        elif name == "technical":
            technical_failure = "fixture Provider unavailable"
        content_port = FakeContentPort(
            ledger, accepted_content, technical_failure=technical_failure
        )
        domain_port = FakeDomainPort(ledger, domain_reply)
        result = submit_reviewed_outcome(
            request.ref,
            pack.ref,
            pack.content_sha256,
            SubmissionIdentity("fixture:agent/submission/idea-1"),
            fixture_review(outcome),
            invocation,
            content_port,
            domain_port,
            runtime,
            identity_registry,
        )

    forbidden = {
        "ResearchGraph.create_question",
        "AgentRuntime.accept_run",
        "AdvancementEngine.form_stage_commit",
    }
    return {
        "scenario": name,
        "status": result.status,
        "decision_phase": result.decision_phase,
        "content_ref": result.content_ref,
        "outcome_ref": result.outcome_ref,
        "exhaustion_proposal_ref": result.exhaustion_proposal_ref,
        "content_decision_receipt_ref": result.content_decision_receipt_ref,
        "domain_decision_receipt_ref": result.domain_decision_receipt_ref,
        "content_decision_receipt_refs": result.content_decision_receipt_refs,
        "domain_decision_receipt_refs": result.domain_decision_receipt_refs,
        "advancement_decision_receipt_ref": (
            result.advancement_decision_receipt_ref
        ),
        "advancement_decision_receipt_refs": (
            result.advancement_decision_receipt_refs
        ),
        "human_request_refs": result.human_request_refs,
        "owner_recovery_receipt_refs": result.owner_recovery_receipt_refs,
        "blocker_refs": result.blocker_refs,
        "events": ledger.events,
        "forbidden_authority_calls": sorted(forbidden.intersection(ledger.events)),
        "simulated_domain_accepted": result.simulated_domain_accepted,
        "is_owner_fact": result.is_owner_fact,
        "is_stage_advanced": result.is_stage_advanced,
        "stage_commit_created_by_skill": result.stage_commit_created_by_skill,
        "fixture_only": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        choices=(
            "idea-set",
            "no-viable",
            "rejected",
            "stale",
            "needs-input",
            "technical",
            "exhaustion",
        ),
        default="idea-set",
    )
    args = parser.parse_args()
    print(json.dumps(run_fixture_scenario(args.scenario), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
