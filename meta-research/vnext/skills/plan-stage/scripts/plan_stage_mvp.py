#!/usr/bin/env python3
"""Deterministic Plan Stage semantic reference model.

Every identity and receipt in this module is an explicit fixture. The module
implements no State Owner, persistence, retrieval system, transport, Runtime,
Target lifecycle, or Stage advancement.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import asdict, dataclass, fields, is_dataclass
from typing import Any, Dict, List, Optional, Protocol, Sequence, Set, Tuple


HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
REF_RE = re.compile(r"^fixture:[A-Za-z0-9._:/-]+$")
MUTABLE_ALIASES = frozenset(("latest", "best", "canonical"))
QUESTION_TRACE_FIELDS = frozenset(
    ("unknown_statement", "answer_shape", "applicability_scope")
)
IDEA_ROLES = frozenset(("query_lens", "experiment_lens", "not_relevant"))
QUERY_STATUSES = frozenset(("ok", "stale", "unavailable", "outcome_unknown"))
OWNER_STATUSES = frozenset(
    (
        "accepted",
        "rejected",
        "stale",
        "needs_input",
        "outcome_unknown",
        "technical_blocker",
        "idempotency_conflict",
        "already_sealed",
    )
)


class ContractViolation(ValueError):
    """The fixture violated a fail-closed Plan contract."""


class PlanBlocked(RuntimeError):
    """A typed recoverable condition prevents a trustworthy Plan decision."""


def _require_text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ContractViolation("{} must be non-empty text".format(label))


def _require_ref(value: str, label: str) -> None:
    _require_text(value, label)
    if not REF_RE.match(value):
        raise ContractViolation("{} must be an explicit fixture ref".format(label))
    segments = set(re.split(r"[/:._-]+", value.lower()))
    if segments & MUTABLE_ALIASES:
        raise ContractViolation("{} cannot use a mutable alias".format(label))


def _require_hash(value: str, label: str) -> None:
    if not isinstance(value, str) or not HASH_RE.match(value):
        raise ContractViolation("{} must be a sha256 content hash".format(label))


def _require_text_tuple(values: Tuple[str, ...], label: str) -> None:
    if not isinstance(values, tuple):
        raise ContractViolation("{} must be a frozen tuple".format(label))
    for value in values:
        _require_text(value, label + " item")


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        _jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class QuestionSemantics:
    unknown_statement: str
    answer_shape: str
    applicability_scope: str

    def validate(self) -> None:
        _require_text(self.unknown_statement, "Question unknown_statement")
        _require_text(self.answer_shape, "Question answer_shape")
        _require_text(self.applicability_scope, "Question applicability_scope")


@dataclass(frozen=True)
class AcceptedQuestionBinding:
    question_ref: str
    quest_ref: str
    content_ref: str
    content_hash: str
    schema_ref: str
    rm_content_receipt_ref: str
    rg_question_receipt_ref: str
    semantics: QuestionSemantics

    def validate(self) -> None:
        for label, value in (
            ("Question ref", self.question_ref),
            ("Quest ref", self.quest_ref),
            ("Question content ref", self.content_ref),
            ("Question schema ref", self.schema_ref),
            ("Question RM receipt", self.rm_content_receipt_ref),
            ("Question RG receipt", self.rg_question_receipt_ref),
        ):
            _require_ref(value, label)
        _require_hash(self.content_hash, "Question content hash")
        self.semantics.validate()
        if canonical_hash(self.semantics) != self.content_hash:
            raise ContractViolation("accepted Question content hash drifted")


@dataclass(frozen=True)
class IdeaCandidate:
    idea_ref: str
    mechanism: str
    conditions: Tuple[str, ...]
    intervention_axis: str
    comparison_structure: str
    falsification_boundary: str

    def validate(self) -> None:
        _require_ref(self.idea_ref, "Idea ref")
        _require_text(self.mechanism, "Idea mechanism")
        _require_text_tuple(self.conditions, "Idea conditions")
        _require_text(self.intervention_axis, "Idea intervention axis")
        _require_text(self.comparison_structure, "Idea comparison structure")
        _require_text(self.falsification_boundary, "Idea falsification boundary")


@dataclass(frozen=True)
class IdeaSetContent:
    ideas: Tuple[IdeaCandidate, ...]

    def validate(self) -> None:
        if not self.ideas:
            raise ContractViolation("complete IdeaSet must contain at least one Idea")
        seen: Set[str] = set()
        for idea in self.ideas:
            idea.validate()
            if idea.idea_ref in seen:
                raise ContractViolation("IdeaSet contains duplicate Idea refs")
            seen.add(idea.idea_ref)


@dataclass(frozen=True)
class AcceptedIdeaSetBinding:
    idea_set_ref: str
    question_ref: str
    quest_ref: str
    content_ref: str
    content_hash: str
    rm_content_receipt_ref: str
    rg_idea_outcome_receipt_ref: str
    idea_stage_commit_ref: str
    content: IdeaSetContent

    def validate(self) -> None:
        for label, value in (
            ("IdeaSet ref", self.idea_set_ref),
            ("IdeaSet Question ref", self.question_ref),
            ("IdeaSet Quest ref", self.quest_ref),
            ("IdeaSet content ref", self.content_ref),
            ("IdeaSet RM receipt", self.rm_content_receipt_ref),
            ("IdeaSet RG receipt", self.rg_idea_outcome_receipt_ref),
            ("Idea StageCommit ref", self.idea_stage_commit_ref),
        ):
            _require_ref(value, label)
        _require_hash(self.content_hash, "IdeaSet content hash")
        self.content.validate()
        if canonical_hash(self.content) != self.content_hash:
            raise ContractViolation("accepted IdeaSet content hash drifted")


@dataclass(frozen=True)
class PlanStageRunRequest:
    request_ref: str
    cycle_ref: str
    foreground_epoch_ref: str
    runtime_binding_ref: str
    execution_fence_ref: str
    context_pack_ref: str
    context_pack_hash: str
    search_boundary_ref: str
    search_boundary_hash: str
    currentness_observation_ref: str
    question: AcceptedQuestionBinding
    idea_set: AcceptedIdeaSetBinding
    typed: bool
    current: bool
    execution_fence_current: bool

    def validate(self) -> None:
        for label, value in (
            ("Plan StageRunRequest ref", self.request_ref),
            ("Cycle ref", self.cycle_ref),
            ("Foreground Epoch ref", self.foreground_epoch_ref),
            ("Runtime binding ref", self.runtime_binding_ref),
            ("Execution Fence ref", self.execution_fence_ref),
            ("ContextPack ref", self.context_pack_ref),
            ("search boundary ref", self.search_boundary_ref),
            ("currentness observation ref", self.currentness_observation_ref),
        ):
            _require_ref(value, label)
        _require_hash(self.context_pack_hash, "ContextPack hash")
        _require_hash(self.search_boundary_hash, "search boundary hash")
        if not self.typed:
            raise ContractViolation("Plan StageRunRequest is not typed")
        if not self.current:
            raise ContractViolation("Plan StageRunRequest is stale or unknown")
        if not self.execution_fence_current:
            raise ContractViolation("root Execution Fence is stale or unknown")
        self.question.validate()
        self.idea_set.validate()
        if self.question.question_ref != self.idea_set.question_ref:
            raise ContractViolation("Question and IdeaSet bindings do not match")
        if self.question.quest_ref != self.idea_set.quest_ref:
            raise ContractViolation("Quest bindings do not match")


@dataclass(frozen=True)
class IdeaRelevance:
    idea_ref: str
    role: str
    rationale: str

    def validate(self) -> None:
        _require_ref(self.idea_ref, "Idea relevance ref")
        if self.role not in IDEA_ROLES:
            raise ContractViolation("Idea relevance role is invalid")
        _require_text(self.rationale, "Idea relevance rationale")


@dataclass(frozen=True)
class EvidenceObligation:
    obligation_key: str
    statement: str
    minimum_support: str
    question_trace: Tuple[str, ...]
    idea_relevance: Tuple[IdeaRelevance, ...]

    def validate(self, idea_refs: Set[str]) -> None:
        _require_text(self.obligation_key, "obligation key")
        _require_text(self.statement, "obligation statement")
        _require_text(self.minimum_support, "obligation minimum support")
        if not self.question_trace:
            raise ContractViolation("obligation has no Question trace")
        if not set(self.question_trace) <= QUESTION_TRACE_FIELDS:
            raise ContractViolation("obligation Question trace is invalid")
        if "answer_shape" not in self.question_trace:
            raise ContractViolation("obligation must trace to Question answer_shape")
        if not set(self.question_trace) & {
            "unknown_statement",
            "applicability_scope",
        }:
            raise ContractViolation(
                "obligation must also trace to unknown or applicability scope"
            )
        seen: Set[str] = set()
        for relevance in self.idea_relevance:
            relevance.validate()
            if relevance.idea_ref in seen:
                raise ContractViolation("obligation repeats an Idea relevance pair")
            seen.add(relevance.idea_ref)
        if seen != idea_refs:
            raise ContractViolation(
                "every obligation must account for the complete IdeaSet"
            )


@dataclass(frozen=True)
class AnswerContract:
    source_question_ref: str
    source_idea_set_ref: str
    obligations: Tuple[EvidenceObligation, ...]

    def validate(self, request: PlanStageRunRequest) -> None:
        if self.source_question_ref != request.question.question_ref:
            raise ContractViolation("AnswerContract Question ref drifted")
        if self.source_idea_set_ref != request.idea_set.idea_set_ref:
            raise ContractViolation("AnswerContract IdeaSet ref drifted")
        if not self.obligations:
            raise ContractViolation("AnswerContract must contain obligations")
        idea_refs = {idea.idea_ref for idea in request.idea_set.content.ideas}
        seen: Set[str] = set()
        for obligation in self.obligations:
            obligation.validate(idea_refs)
            if obligation.obligation_key in seen:
                raise ContractViolation("AnswerContract repeats an obligation key")
            seen.add(obligation.obligation_key)

    @property
    def contract_hash(self) -> str:
        return canonical_hash(self)


@dataclass(frozen=True)
class EvidenceRef:
    evidence_ref: str
    source_kind: str
    asset_version_ref: str
    target_commit_root_ref: str
    provenance_closure_refs: Tuple[str, ...]
    capabilities: Tuple[str, ...]
    eligibility_token_ref: str
    integrity_receipt_ref: str
    availability_receipt_ref: str
    currentness_receipt_ref: str

    def validate(self) -> None:
        for label, value in (
            ("Evidence ref", self.evidence_ref),
            ("Evidence AssetVersion ref", self.asset_version_ref),
            ("Evidence TargetCommit root ref", self.target_commit_root_ref),
            ("Evidence eligibility token", self.eligibility_token_ref),
            ("Evidence integrity receipt", self.integrity_receipt_ref),
            ("Evidence availability receipt", self.availability_receipt_ref),
            ("Evidence currentness receipt", self.currentness_receipt_ref),
        ):
            _require_ref(value, label)
        _require_text(self.source_kind, "Evidence source kind")
        if self.source_kind in {"CandidateCard", "DynamicPreview", "LocalPath"}:
            raise ContractViolation("navigation projection cannot be EvidenceRef")
        if not self.provenance_closure_refs:
            raise ContractViolation("EvidenceRef has no provenance closure")
        for ref in self.provenance_closure_refs:
            _require_ref(ref, "Evidence provenance ref")
        _require_text_tuple(self.capabilities, "Evidence capabilities")
        if not self.capabilities:
            raise ContractViolation("EvidenceRef has no declared capability")


@dataclass(frozen=True)
class EvidenceQuery:
    mode: str
    stage_request_ref: str
    answer_contract_hash: str
    obligation_key: str
    statement: str
    idea_lens_refs: Tuple[str, ...]
    search_snapshot_token: Optional[str]

    def validate(self) -> None:
        if self.mode not in {"open", "follow", "refresh"}:
            raise ContractViolation("evidence query mode is invalid")
        _require_ref(self.stage_request_ref, "query StageRunRequest ref")
        _require_hash(self.answer_contract_hash, "query AnswerContract hash")
        _require_text(self.obligation_key, "query obligation key")
        _require_text(self.statement, "query obligation statement")
        for ref in self.idea_lens_refs:
            _require_ref(ref, "query Idea lens ref")
        if self.mode == "open" and self.search_snapshot_token is not None:
            raise ContractViolation("open query cannot supply a snapshot token")
        if self.mode in {"follow", "refresh"}:
            if self.search_snapshot_token is None:
                raise ContractViolation("follow or refresh query needs a snapshot token")
            _require_ref(self.search_snapshot_token, "search snapshot token")


@dataclass(frozen=True)
class EvidenceQueryResult:
    status: str
    search_snapshot_token: str
    evidence: Tuple[EvidenceRef, ...] = ()
    reason: str = ""

    def validate(self) -> None:
        if self.status not in QUERY_STATUSES:
            raise ContractViolation("evidence query status is invalid")
        _require_ref(self.search_snapshot_token, "result search snapshot token")
        if self.status == "ok":
            for item in self.evidence:
                item.validate()
        else:
            if self.evidence:
                raise ContractViolation("non-ok query result cannot carry evidence")
            _require_text(self.reason, "non-ok query reason")


@dataclass(frozen=True)
class EvidenceUse:
    evidence_ref: str
    supported_claim: str
    support_boundary: str
    contributing_idea_refs: Tuple[str, ...]

    def validate(self) -> None:
        _require_ref(self.evidence_ref, "EvidenceUse evidence ref")
        _require_text(self.supported_claim, "EvidenceUse supported claim")
        _require_text(self.support_boundary, "EvidenceUse support boundary")
        for ref in self.contributing_idea_refs:
            _require_ref(ref, "EvidenceUse contributing Idea ref")


@dataclass(frozen=True)
class CoverageDecision:
    obligation_key: str
    disposition: str
    evidence_uses: Tuple[EvidenceUse, ...]
    insufficiency: Optional[str] = None


@dataclass(frozen=True)
class ExperimentBrief:
    experiment_key: str
    gap_obligation_keys: Tuple[str, ...]
    goal: str
    characteristics: str
    boundary_constraints: str
    semantic_delta: str
    contributing_idea_refs: Tuple[str, ...]

    def validate(self) -> None:
        _require_text(self.experiment_key, "ExperimentKey")
        _require_text_tuple(self.gap_obligation_keys, "Brief gap obligations")
        if not self.gap_obligation_keys:
            raise ContractViolation("ExperimentBrief must serve a gap")
        _require_text(self.goal, "ExperimentBrief Goal")
        _require_text(self.characteristics, "ExperimentBrief Characteristics")
        _require_text(self.boundary_constraints, "ExperimentBrief BoundaryConstraints")
        _require_text(self.semantic_delta, "ExperimentBrief SemanticDelta")
        for ref in self.contributing_idea_refs:
            _require_ref(ref, "ExperimentBrief contributing Idea ref")


@dataclass(frozen=True)
class ReviewFinding:
    finding_id: str
    category: str
    message: str


@dataclass(frozen=True)
class FindingDisposition:
    finding_id: str
    action: str
    rationale: str


@dataclass(frozen=True)
class AdvisoryReviewRecord:
    reviewer_session_ref: str
    reviewed_draft_hash: str
    findings: Tuple[ReviewFinding, ...]
    dispositions: Tuple[FindingDisposition, ...]
    final_plan_hash: str

    def validate(self, expected_final_hash: str) -> None:
        _require_ref(self.reviewer_session_ref, "reviewer Session ref")
        _require_hash(self.reviewed_draft_hash, "reviewed draft hash")
        _require_hash(self.final_plan_hash, "final Plan hash")
        if self.final_plan_hash != expected_final_hash:
            raise ContractViolation("review final hash does not match Plan")
        finding_ids: Set[str] = set()
        for finding in self.findings:
            _require_text(finding.finding_id, "review finding id")
            _require_text(finding.category, "review finding category")
            _require_text(finding.message, "review finding message")
            if finding.finding_id in finding_ids:
                raise ContractViolation("duplicate review finding id")
            finding_ids.add(finding.finding_id)
        disposition_ids: Set[str] = set()
        revised = False
        for disposition in self.dispositions:
            _require_text(disposition.finding_id, "review disposition id")
            if disposition.action not in {"revised", "not_adopted"}:
                raise ContractViolation("review disposition action is invalid")
            _require_text(disposition.rationale, "review disposition rationale")
            if disposition.finding_id in disposition_ids:
                raise ContractViolation("duplicate review disposition")
            disposition_ids.add(disposition.finding_id)
            revised = revised or disposition.action == "revised"
        if disposition_ids != finding_ids:
            raise ContractViolation("every review finding needs one disposition")
        if revised and self.reviewed_draft_hash == self.final_plan_hash:
            raise ContractViolation("claimed review revision did not change Plan hash")


@dataclass(frozen=True)
class CompiledPlan:
    answer_contract: AnswerContract
    coverage: Tuple[CoverageDecision, ...]
    experiment_briefs: Tuple[ExperimentBrief, ...]
    evidence_reuse_set: Tuple[str, ...]
    gap_set: Tuple[str, ...]
    bundle_disposition: str
    search_snapshot_token: str
    semantic_hash: str
    review: AdvisoryReviewRecord

    def normalized_content(self) -> Dict[str, Any]:
        return {
            "answer_contract": _jsonable(self.answer_contract),
            "answer_contract_hash": self.answer_contract.contract_hash,
            "coverage": _jsonable(self.coverage),
            "evidence_reuse_set": list(self.evidence_reuse_set),
            "gap_set": list(self.gap_set),
            "experiment_briefs": _jsonable(self.experiment_briefs),
            "bundle_disposition": self.bundle_disposition,
            "idea_trace": {
                obligation.obligation_key: _jsonable(obligation.idea_relevance)
                for obligation in self.answer_contract.obligations
            },
            "review": _jsonable(self.review),
            "semantic_hash": self.semantic_hash,
        }


@dataclass(frozen=True)
class OwnerResult:
    status: str
    object_ref: Optional[str]
    receipt_ref: str
    detail: str = ""

    def validate(self) -> None:
        if self.status not in OWNER_STATUSES:
            raise ContractViolation("Owner result status is invalid")
        _require_ref(self.receipt_ref, "Owner result receipt")
        if self.status == "accepted":
            if self.object_ref is None:
                raise ContractViolation("accepted Owner result needs an object ref")
            _require_ref(self.object_ref, "accepted Owner object ref")
        elif self.object_ref is not None:
            _require_ref(self.object_ref, "Owner result object ref")


@dataclass(frozen=True)
class BundleSkipBasisCandidate:
    formal_plan_ref: str
    answer_contract_hash: str
    disposition: str
    rm_plan_content_receipt_ref: str
    rg_formal_plan_receipt_ref: str
    currentness_observation_ref: str


@dataclass(frozen=True)
class PlanRunResult:
    status: str
    plan_content_ref: Optional[str]
    rm_plan_content_receipt_ref: Optional[str]
    formal_plan_ref: Optional[str]
    rg_formal_plan_receipt_ref: Optional[str]
    bundle_skip_basis: Optional[BundleSkipBasisCandidate]
    semantic_hash: str


class PlanPort(Protocol):
    """Replaceable semantic seam; fake implementations have no authority."""

    def query_evidence(self, query: EvidenceQuery) -> EvidenceQueryResult:
        """TODO-IMPL(plan_interface.query_evidence; source=#62)"""

    def submit_plan_content(
        self, submission_identity: str, normalized_plan: Dict[str, Any]
    ) -> OwnerResult:
        """TODO-IMPL(research_memory.accept_plan_content; source=#62)"""

    def submit_formal_plan(
        self,
        submission_identity: str,
        request: PlanStageRunRequest,
        plan: CompiledPlan,
        content_ref: str,
        rm_receipt_ref: str,
    ) -> OwnerResult:
        """TODO-IMPL(research_graph.submit_formal_plan; source=#62)"""

    def get_submission(self, submission_identity: str) -> OwnerResult:
        """TODO-IMPL(plan_interface.get_submission; source=#62)"""


def _query_evidence(
    request: PlanStageRunRequest,
    contract: AnswerContract,
    port: PlanPort,
) -> Tuple[Dict[str, Tuple[EvidenceRef, ...]], str]:
    snapshot_token: Optional[str] = None
    refreshes = 0
    idea_by_ref = {idea.idea_ref: idea for idea in request.idea_set.content.ideas}

    while True:
        collected: Dict[str, Tuple[EvidenceRef, ...]] = {}
        restart = False
        for obligation in contract.obligations:
            query_lenses = tuple(
                relevance.idea_ref
                for relevance in obligation.idea_relevance
                if relevance.role == "query_lens"
            )
            if not set(query_lenses) <= set(idea_by_ref):
                raise ContractViolation("query lens references an unknown Idea")
            query = EvidenceQuery(
                mode="open" if snapshot_token is None else "follow",
                stage_request_ref=request.request_ref,
                answer_contract_hash=contract.contract_hash,
                obligation_key=obligation.obligation_key,
                statement=obligation.statement,
                idea_lens_refs=query_lenses,
                search_snapshot_token=snapshot_token,
            )
            query.validate()
            result = port.query_evidence(query)
            result.validate()
            if result.status == "stale":
                if refreshes >= 3:
                    raise PlanBlocked("fixture search snapshot remained stale")
                stale_token = result.search_snapshot_token
                refresh = EvidenceQuery(
                    mode="refresh",
                    stage_request_ref=request.request_ref,
                    answer_contract_hash=contract.contract_hash,
                    obligation_key=obligation.obligation_key,
                    statement=obligation.statement,
                    idea_lens_refs=query_lenses,
                    search_snapshot_token=stale_token,
                )
                refresh.validate()
                refreshed = port.query_evidence(refresh)
                refreshed.validate()
                if refreshed.status != "ok" or refreshed.evidence:
                    raise PlanBlocked("evidence snapshot refresh did not reconcile cleanly")
                snapshot_token = refreshed.search_snapshot_token
                refreshes += 1
                restart = True
                break
            if result.status != "ok":
                raise PlanBlocked(
                    "evidence query is {}: {}".format(result.status, result.reason)
                )
            if snapshot_token is None:
                snapshot_token = result.search_snapshot_token
            elif result.search_snapshot_token != snapshot_token:
                raise ContractViolation("evidence query silently changed snapshot")
            collected[obligation.obligation_key] = result.evidence
        if restart:
            continue
        if snapshot_token is None:
            raise ContractViolation("evidence query produced no search snapshot")
        return collected, snapshot_token


def _semantic_plan_hash(
    contract: AnswerContract,
    coverage: Tuple[CoverageDecision, ...],
    briefs: Tuple[ExperimentBrief, ...],
) -> str:
    return canonical_hash(
        {
            "answer_contract": contract,
            "coverage": coverage,
            "experiment_briefs": briefs,
        }
    )


def compile_plan(
    request: PlanStageRunRequest,
    contract: AnswerContract,
    coverage: Tuple[CoverageDecision, ...],
    briefs: Tuple[ExperimentBrief, ...],
    review: AdvisoryReviewRecord,
    port: PlanPort,
) -> CompiledPlan:
    request.validate()
    contract.validate(request)
    available_by_obligation, snapshot_token = _query_evidence(request, contract, port)

    obligation_by_key = {
        obligation.obligation_key: obligation for obligation in contract.obligations
    }
    decision_by_key: Dict[str, CoverageDecision] = {}
    reuse_refs: Set[str] = set()
    gap_keys: Set[str] = set()

    for decision in coverage:
        if decision.obligation_key in decision_by_key:
            raise ContractViolation("coverage repeats an obligation")
        if decision.obligation_key not in obligation_by_key:
            raise ContractViolation("coverage references an unknown obligation")
        if decision.disposition not in {"covered", "gap"}:
            raise ContractViolation("coverage disposition is invalid")
        available = {
            evidence.evidence_ref: evidence
            for evidence in available_by_obligation[decision.obligation_key]
        }
        allowed_idea_refs = {
            relevance.idea_ref
            for relevance in obligation_by_key[
                decision.obligation_key
            ].idea_relevance
            if relevance.role == "query_lens"
        }
        for use in decision.evidence_uses:
            use.validate()
            if use.evidence_ref not in available:
                raise ContractViolation(
                    "EvidenceUse was not returned for its obligation"
                )
            if not set(use.contributing_idea_refs) <= allowed_idea_refs:
                raise ContractViolation(
                    "EvidenceUse cites an Idea that was not a query lens"
                )
            reuse_refs.add(use.evidence_ref)
        if decision.disposition == "covered":
            if not decision.evidence_uses:
                raise ContractViolation("covered obligation needs exact evidence")
            if decision.insufficiency is not None:
                raise ContractViolation("covered obligation cannot claim insufficiency")
        else:
            if decision.insufficiency is None:
                raise ContractViolation("gap obligation needs an insufficiency statement")
            _require_text(decision.insufficiency, "gap insufficiency")
            gap_keys.add(decision.obligation_key)
        decision_by_key[decision.obligation_key] = decision

    if set(decision_by_key) != set(obligation_by_key):
        raise ContractViolation("coverage must decide every obligation exactly once")

    brief_keys: Set[str] = set()
    brief_gap_keys: Set[str] = set()
    all_idea_refs = {idea.idea_ref for idea in request.idea_set.content.ideas}
    for brief in briefs:
        brief.validate()
        if brief.experiment_key in brief_keys:
            raise ContractViolation("duplicate ExperimentKey")
        brief_keys.add(brief.experiment_key)
        if not set(brief.gap_obligation_keys) <= gap_keys:
            raise ContractViolation("ExperimentBrief serves a covered or unknown obligation")
        if not set(brief.contributing_idea_refs) <= all_idea_refs:
            raise ContractViolation("ExperimentBrief cites an unknown Idea")
        for gap_key in brief.gap_obligation_keys:
            roles = {
                relevance.idea_ref: relevance.role
                for relevance in obligation_by_key[gap_key].idea_relevance
            }
            if any(roles[ref] == "not_relevant" for ref in brief.contributing_idea_refs):
                raise ContractViolation(
                    "ExperimentBrief cites an Idea marked not_relevant"
                )
        brief_gap_keys.update(brief.gap_obligation_keys)

    if brief_gap_keys != gap_keys:
        raise ContractViolation("every gap and only gaps need ExperimentBrief coverage")
    if not gap_keys and briefs:
        raise ContractViolation("covered Plan cannot contain ExperimentBriefs")

    semantic_hash = _semantic_plan_hash(contract, coverage, briefs)
    review.validate(semantic_hash)
    disposition = (
        "experiments_required" if gap_keys else "no_new_experiment_required"
    )
    return CompiledPlan(
        answer_contract=contract,
        coverage=coverage,
        experiment_briefs=briefs,
        evidence_reuse_set=tuple(sorted(reuse_refs)),
        gap_set=tuple(sorted(gap_keys)),
        bundle_disposition=disposition,
        search_snapshot_token=snapshot_token,
        semantic_hash=semantic_hash,
        review=review,
    )


def submit_plan(
    submission_identity: str,
    request: PlanStageRunRequest,
    plan: CompiledPlan,
    port: PlanPort,
) -> PlanRunResult:
    _require_ref(submission_identity, "Plan submission identity")
    request.validate()
    rm_result = port.submit_plan_content(
        submission_identity, plan.normalized_content()
    )
    rm_result.validate()
    if rm_result.status != "accepted":
        return PlanRunResult(
            status="rm_{}".format(rm_result.status),
            plan_content_ref=rm_result.object_ref,
            rm_plan_content_receipt_ref=rm_result.receipt_ref,
            formal_plan_ref=None,
            rg_formal_plan_receipt_ref=None,
            bundle_skip_basis=None,
            semantic_hash=plan.semantic_hash,
        )

    assert rm_result.object_ref is not None
    rg_result = port.submit_formal_plan(
        submission_identity,
        request,
        plan,
        rm_result.object_ref,
        rm_result.receipt_ref,
    )
    rg_result.validate()
    if rg_result.status != "accepted":
        return PlanRunResult(
            status="rg_{}".format(rg_result.status),
            plan_content_ref=rm_result.object_ref,
            rm_plan_content_receipt_ref=rm_result.receipt_ref,
            formal_plan_ref=rg_result.object_ref,
            rg_formal_plan_receipt_ref=rg_result.receipt_ref,
            bundle_skip_basis=None,
            semantic_hash=plan.semantic_hash,
        )

    assert rg_result.object_ref is not None
    skip_basis: Optional[BundleSkipBasisCandidate] = None
    if plan.bundle_disposition == "no_new_experiment_required":
        skip_basis = BundleSkipBasisCandidate(
            formal_plan_ref=rg_result.object_ref,
            answer_contract_hash=plan.answer_contract.contract_hash,
            disposition=plan.bundle_disposition,
            rm_plan_content_receipt_ref=rm_result.receipt_ref,
            rg_formal_plan_receipt_ref=rg_result.receipt_ref,
            currentness_observation_ref=request.currentness_observation_ref,
        )
    return PlanRunResult(
        status="accepted",
        plan_content_ref=rm_result.object_ref,
        rm_plan_content_receipt_ref=rm_result.receipt_ref,
        formal_plan_ref=rg_result.object_ref,
        rg_formal_plan_receipt_ref=rg_result.receipt_ref,
        bundle_skip_basis=skip_basis,
        semantic_hash=plan.semantic_hash,
    )


class FakePlanPort:
    """Deterministic fixture port; never a production Adapter or Owner."""

    def __init__(
        self,
        query_results: Sequence[EvidenceQueryResult],
        rm_result: OwnerResult,
        rg_result: OwnerResult,
    ) -> None:
        self._query_results = iter(tuple(query_results))
        self.rm_result = rm_result
        self.rg_result = rg_result
        self.queries: List[EvidenceQuery] = []
        self.calls: List[str] = []

    def query_evidence(self, query: EvidenceQuery) -> EvidenceQueryResult:
        query.validate()
        self.queries.append(query)
        self.calls.append("query_evidence:{}".format(query.mode))
        try:
            return next(self._query_results)
        except StopIteration as exc:
            raise PlanBlocked("fixture query stream ended") from exc

    def submit_plan_content(
        self, submission_identity: str, normalized_plan: Dict[str, Any]
    ) -> OwnerResult:
        _require_ref(submission_identity, "RM submission identity")
        if not normalized_plan:
            raise ContractViolation("RM submission has no Plan content")
        self.calls.append("submit_plan_content")
        return self.rm_result

    def submit_formal_plan(
        self,
        submission_identity: str,
        request: PlanStageRunRequest,
        plan: CompiledPlan,
        content_ref: str,
        rm_receipt_ref: str,
    ) -> OwnerResult:
        _require_ref(submission_identity, "RG submission identity")
        request.validate()
        _require_ref(content_ref, "RG Plan content ref")
        _require_ref(rm_receipt_ref, "RG RM receipt ref")
        if content_ref != self.rm_result.object_ref:
            raise ContractViolation("RG did not receive exact RM content ref")
        if rm_receipt_ref != self.rm_result.receipt_ref:
            raise ContractViolation("RG did not receive exact RM receipt")
        self.calls.append("submit_formal_plan")
        return self.rg_result

    def get_submission(self, submission_identity: str) -> OwnerResult:
        _require_ref(submission_identity, "reconciliation identity")
        self.calls.append("get_submission")
        return self.rg_result


def fixture_hash(seed: str) -> str:
    return "sha256:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()


def make_request() -> PlanStageRunRequest:
    question_semantics = QuestionSemantics(
        unknown_statement="Which mechanism improves robust cross-domain inference?",
        answer_shape="A bounded comparison with evidence for effect and limits.",
        applicability_scope="Small labelled source domains; excludes online adaptation.",
    )
    question = AcceptedQuestionBinding(
        question_ref="fixture:rg-question:q1",
        quest_ref="fixture:rg-quest:quest1",
        content_ref="fixture:rm-question-content:q1-v1",
        content_hash=canonical_hash(question_semantics),
        schema_ref="fixture:schema:formal-question-v1",
        rm_content_receipt_ref="fixture:rm-receipt:question-q1",
        rg_question_receipt_ref="fixture:rg-receipt:question-q1",
        semantics=question_semantics,
    )
    ideas = IdeaSetContent(
        ideas=(
            IdeaCandidate(
                idea_ref="fixture:rg-idea:invariance",
                mechanism="learn domain-invariant causal features",
                conditions=("stable causal features exist",),
                intervention_axis="remove domain-specific nuisance signal",
                comparison_structure="matched capacity with and without invariance",
                falsification_boundary="no paired-domain improvement under held-fixed data",
            ),
            IdeaCandidate(
                idea_ref="fixture:rg-idea:calibration",
                mechanism="calibrate confidence under domain shift",
                conditions=("confidence drift is measurable",),
                intervention_axis="post-hoc calibration",
                comparison_structure="calibrated versus uncalibrated predictions",
                falsification_boundary="calibration error does not improve",
            ),
        )
    )
    idea_binding = AcceptedIdeaSetBinding(
        idea_set_ref="fixture:rg-idea-set:q1-v1",
        question_ref=question.question_ref,
        quest_ref=question.quest_ref,
        content_ref="fixture:rm-idea-set:q1-v1",
        content_hash=canonical_hash(ideas),
        rm_content_receipt_ref="fixture:rm-receipt:idea-set-q1",
        rg_idea_outcome_receipt_ref="fixture:rg-receipt:idea-set-q1",
        idea_stage_commit_ref="fixture:ae-stage-commit:idea-q1",
        content=ideas,
    )
    return PlanStageRunRequest(
        request_ref="fixture:ae-stage-request:plan-q1",
        cycle_ref="fixture:rg-cycle:c1",
        foreground_epoch_ref="fixture:ae-epoch:e1",
        runtime_binding_ref="fixture:ar-binding:plan-q1",
        execution_fence_ref="fixture:ar-fence:plan-q1-a1",
        context_pack_ref="fixture:context-pack:plan-q1",
        context_pack_hash=fixture_hash("plan-context-pack-q1"),
        search_boundary_ref="fixture:plan-search-boundary:q1-v1",
        search_boundary_hash=fixture_hash("plan-search-boundary-q1-v1"),
        currentness_observation_ref="fixture:ae-observation:plan-q1-current",
        question=question,
        idea_set=idea_binding,
        typed=True,
        current=True,
        execution_fence_current=True,
    )


def make_answer_contract(request: PlanStageRunRequest) -> AnswerContract:
    invariance_ref = request.idea_set.content.ideas[0].idea_ref
    calibration_ref = request.idea_set.content.ideas[1].idea_ref
    return AnswerContract(
        source_question_ref=request.question.question_ref,
        source_idea_set_ref=request.idea_set.idea_set_ref,
        obligations=(
            EvidenceObligation(
                obligation_key="effect",
                statement="Estimate the robust cross-domain effect under a matched comparison.",
                minimum_support="Accepted paired-domain measurements with uncertainty.",
                question_trace=("unknown_statement", "answer_shape"),
                idea_relevance=(
                    IdeaRelevance(
                        invariance_ref,
                        "query_lens",
                        "The mechanism defines the primary matched comparison.",
                    ),
                    IdeaRelevance(
                        calibration_ref,
                        "not_relevant",
                        "Calibration does not establish feature invariance effect.",
                    ),
                ),
            ),
            EvidenceObligation(
                obligation_key="limits",
                statement="Bound where the observed answer applies and fails.",
                minimum_support="Accepted domain-stratified limits or explicit missing strata.",
                question_trace=("answer_shape", "applicability_scope"),
                idea_relevance=(
                    IdeaRelevance(
                        invariance_ref,
                        "query_lens",
                        "Domain strata test the invariance boundary.",
                    ),
                    IdeaRelevance(
                        calibration_ref,
                        "experiment_lens",
                        "Calibration may explain a remaining confidence-boundary gap.",
                    ),
                ),
            ),
        ),
    )


def make_evidence(key: str, capability: str) -> EvidenceRef:
    return EvidenceRef(
        evidence_ref="fixture:rg-evidence:{}".format(key),
        source_kind="MetricResult",
        asset_version_ref="fixture:rm-asset:{}-v1".format(key),
        target_commit_root_ref="fixture:rg-target-commit:{}".format(key),
        provenance_closure_refs=("fixture:rg-provenance:{}".format(key),),
        capabilities=(capability,),
        eligibility_token_ref="fixture:rg-eligibility:{}".format(key),
        integrity_receipt_ref="fixture:rm-integrity:{}".format(key),
        availability_receipt_ref="fixture:rm-availability:{}".format(key),
        currentness_receipt_ref="fixture:rg-currentness:{}".format(key),
    )


def accepted_owner_results() -> Tuple[OwnerResult, OwnerResult]:
    return (
        OwnerResult(
            status="accepted",
            object_ref="fixture:rm-plan-content:plan-q1-v1",
            receipt_ref="fixture:rm-receipt:plan-q1-v1",
        ),
        OwnerResult(
            status="accepted",
            object_ref="fixture:rg-formal-plan:plan-q1",
            receipt_ref="fixture:rg-receipt:plan-q1",
        ),
    )


def make_review(
    contract: AnswerContract,
    coverage: Tuple[CoverageDecision, ...],
    briefs: Tuple[ExperimentBrief, ...],
) -> AdvisoryReviewRecord:
    semantic_hash = _semantic_plan_hash(contract, coverage, briefs)
    return AdvisoryReviewRecord(
        reviewer_session_ref="fixture:ar-session:plan-review-q1",
        reviewed_draft_hash=semantic_hash,
        findings=(),
        dispositions=(),
        final_plan_hash=semantic_hash,
    )


def all_covered_scenario() -> Tuple[PlanRunResult, CompiledPlan, FakePlanPort]:
    request = make_request()
    contract = make_answer_contract(request)
    effect = make_evidence("effect", "paired_effect")
    limits = make_evidence("limits", "applicability_boundary")
    coverage = (
        CoverageDecision(
            obligation_key="effect",
            disposition="covered",
            evidence_uses=(
                EvidenceUse(
                    effect.evidence_ref,
                    "Matched evidence estimates the effect.",
                    "Small labelled source domains only.",
                    (request.idea_set.content.ideas[0].idea_ref,),
                ),
            ),
        ),
        CoverageDecision(
            obligation_key="limits",
            disposition="covered",
            evidence_uses=(
                EvidenceUse(
                    limits.evidence_ref,
                    "Domain strata bound the result.",
                    "No online-adaptation evidence.",
                    (request.idea_set.content.ideas[0].idea_ref,),
                ),
            ),
        ),
    )
    briefs: Tuple[ExperimentBrief, ...] = ()
    rm_result, rg_result = accepted_owner_results()
    snapshot = "fixture:search-snapshot:s1"
    port = FakePlanPort(
        query_results=(
            EvidenceQueryResult("ok", snapshot, (effect,)),
            EvidenceQueryResult("ok", snapshot, (limits,)),
        ),
        rm_result=rm_result,
        rg_result=rg_result,
    )
    plan = compile_plan(
        request, contract, coverage, briefs, make_review(contract, coverage, briefs), port
    )
    result = submit_plan("fixture:plan-submission:q1-v1", request, plan, port)
    return result, plan, port


def gap_scenario() -> Tuple[PlanRunResult, CompiledPlan, FakePlanPort]:
    request = make_request()
    contract = make_answer_contract(request)
    effect = make_evidence("effect", "paired_effect")
    coverage = (
        CoverageDecision(
            obligation_key="effect",
            disposition="covered",
            evidence_uses=(
                EvidenceUse(
                    effect.evidence_ref,
                    "Matched evidence estimates the effect.",
                    "Small labelled source domains only.",
                    (request.idea_set.content.ideas[0].idea_ref,),
                ),
            ),
        ),
        CoverageDecision(
            obligation_key="limits",
            disposition="gap",
            evidence_uses=(),
            insufficiency="No accepted domain-stratified boundary evidence exists.",
        ),
    )
    briefs = (
        ExperimentBrief(
            experiment_key="limits-calibration-boundary",
            gap_obligation_keys=("limits",),
            goal="Obtain evidence for applicability and failure boundaries.",
            characteristics="Compare invariant features with and without calibration across held-out domains.",
            boundary_constraints="Hold data split and feature capacity fixed; vary calibration only.",
            semantic_delta="Add an Evaluation comparison for confidence calibration.",
            contributing_idea_refs=(request.idea_set.content.ideas[1].idea_ref,),
        ),
    )
    rm_result, rg_result = accepted_owner_results()
    snapshot = "fixture:search-snapshot:s-gap"
    port = FakePlanPort(
        query_results=(
            EvidenceQueryResult("ok", snapshot, (effect,)),
            EvidenceQueryResult("ok", snapshot, ()),
        ),
        rm_result=rm_result,
        rg_result=rg_result,
    )
    plan = compile_plan(
        request, contract, coverage, briefs, make_review(contract, coverage, briefs), port
    )
    result = submit_plan("fixture:plan-submission:q1-gap", request, plan, port)
    return result, plan, port


def stale_repair_scenario() -> Tuple[PlanRunResult, CompiledPlan, FakePlanPort]:
    request = make_request()
    contract = make_answer_contract(request)
    effect = make_evidence("effect-refreshed", "paired_effect")
    limits = make_evidence("limits-refreshed", "applicability_boundary")
    coverage = (
        CoverageDecision(
            obligation_key="effect",
            disposition="covered",
            evidence_uses=(
                EvidenceUse(
                    effect.evidence_ref,
                    "Refreshed exact evidence estimates the effect.",
                    "Small labelled source domains only.",
                    (request.idea_set.content.ideas[0].idea_ref,),
                ),
            ),
        ),
        CoverageDecision(
            obligation_key="limits",
            disposition="covered",
            evidence_uses=(
                EvidenceUse(
                    limits.evidence_ref,
                    "Refreshed exact evidence bounds the result.",
                    "No online-adaptation evidence.",
                    (request.idea_set.content.ideas[0].idea_ref,),
                ),
            ),
        ),
    )
    briefs: Tuple[ExperimentBrief, ...] = ()
    rm_result, rg_result = accepted_owner_results()
    old_snapshot = "fixture:search-snapshot:stale-s1"
    new_snapshot = "fixture:search-snapshot:fresh-s2"
    port = FakePlanPort(
        query_results=(
            EvidenceQueryResult(
                "stale", old_snapshot, (), "selected snapshot expired"
            ),
            EvidenceQueryResult("ok", new_snapshot, ()),
            EvidenceQueryResult("ok", new_snapshot, (effect,)),
            EvidenceQueryResult("ok", new_snapshot, (limits,)),
        ),
        rm_result=rm_result,
        rg_result=rg_result,
    )
    plan = compile_plan(
        request, contract, coverage, briefs, make_review(contract, coverage, briefs), port
    )
    result = submit_plan("fixture:plan-submission:q1-stale", request, plan, port)
    return result, plan, port


def _render_scenario(name: str) -> Dict[str, Any]:
    if name == "all-covered":
        result, plan, port = all_covered_scenario()
    elif name == "gap":
        result, plan, port = gap_scenario()
    elif name == "stale-repair":
        result, plan, port = stale_repair_scenario()
    else:
        raise ContractViolation("unknown fixture scenario")
    return {
        "fixture_only": True,
        "scenario": name,
        "result": asdict(result),
        "formal_plan_candidate": plan.normalized_content(),
        "call_ledger": list(port.calls),
        "query_modes": [query.mode for query in port.queries],
        "stage_request_fields": [field.name for field in fields(PlanStageRunRequest)],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        choices=("all-covered", "gap", "stale-repair"),
        required=True,
    )
    args = parser.parse_args()
    print(json.dumps(_render_scenario(args.scenario), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
