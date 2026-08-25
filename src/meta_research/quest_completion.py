from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, cast

from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.reasoning_contract import CANDIDATE_COMPLETION_SCHEMA_REF


class QuestCompletionHumanCollaboration(Protocol):
    """Human-owned preparation, preview, and decision seam."""

    def prepare_quest_completion(
        self,
        *,
        source: dict[str, object],
        candidate_completion: dict[str, object],
        candidate_completion_ref: str,
        candidate_completion_hash: str,
        goal_revision: dict[str, object],
        idempotency_key: str,
    ) -> dict[str, object]: ...

    def query_current_quest_completion(self) -> dict[str, object] | None: ...

    def query_quest_completion(
        self, context_ref: str
    ) -> dict[str, object] | None: ...

    def query_quest_completion_contexts(
        self,
    ) -> tuple[dict[str, object], ...]: ...

    def preview_quest_completion(
        self, context_ref: str, *, idempotency_key: str
    ) -> dict[str, object]: ...


class QuestCompletionResearchGraph(Protocol):
    """Research-meaning seam used by the completion coordinator."""

    def query_candidate_completion(
        self, *, source_outcome_ref: str, candidate_completion_ref: str
    ) -> dict[str, object] | None: ...

    def query_current_quest_goal_revision(
        self, quest_ref: str
    ) -> dict[str, object] | None: ...

    def query_quest_completion_acceptance(
        self, candidate_completion_ref: str
    ) -> dict[str, object] | None: ...

    def accept_quest_completion(
        self,
        *,
        context_ref: str,
        source_outcome_ref: str,
        candidate_completion_ref: str,
        candidate_completion_hash: str,
        goal_revision: dict[str, object],
        human_confirmation: dict[str, object],
        idempotency_key: str,
    ) -> dict[str, object]: ...


class QuestCompletionAdvancementEngine(Protocol):
    """Current StageCommit and Quest-ending authority seam."""

    def query_foreground(self, quest_ref: str) -> dict[str, object] | None: ...

    def query_reasoning_stage_commit(self, request_ref: str) -> object | None: ...

    def query_quest_ending(self, quest_ref: str) -> dict[str, object] | None: ...

    def end_quest(
        self,
        *,
        quest_ref: str,
        cycle_ref: str,
        foreground_epoch: int,
        reasoning_stage_run_request_ref: str,
        candidate_completion_ref: str,
        completion_ref: str,
        completion_receipt: object,
        idempotency_key: str,
    ) -> dict[str, object]: ...


@dataclass(frozen=True)
class _CompletionFacts:
    context: dict[str, object]
    context_ref: str
    candidate: dict[str, object]
    candidate_ref: str
    candidate_hash: str
    source: dict[str, object]
    goal_revision: dict[str, object]
    preview: dict[str, object] | None
    decision: dict[str, object] | None
    domain_acceptance: dict[str, object] | None
    ending: dict[str, object] | None
    quest_is_ended: bool
    source_is_current: bool


class QuestCompletionService:
    """Recoverable HC -> RG -> AE Quest-completion coordinator.

    CandidateCompletion, human confirmation, RG semantic acceptance, and an
    AE ending transition remain four separate facts.  The service stores no
    authoritative state and never constructs a receipt.  Every call rebuilds
    progress from the public Owner queries, so a lost response or daemon
    restart resumes at the first missing durable boundary.

    ``process_once`` performs at most one Owner write.  Reads may span Owners
    because they are the currentness and reconciliation checks which make each
    subsequent effect safe.
    """

    def __init__(
        self,
        human_collaboration: QuestCompletionHumanCollaboration,
        research_graph: QuestCompletionResearchGraph,
        advancement_engine: QuestCompletionAdvancementEngine,
    ) -> None:
        self._human_collaboration = human_collaboration
        self._research_graph = research_graph
        self._advancement_engine = advancement_engine
        self._scheduler_cursor: str | None = None

    def start(
        self,
        *,
        source_outcome_ref: str,
        candidate_completion_ref: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        """Prepare the exact accepted candidate for a human decision."""

        _require_ref(source_outcome_ref, "candidate_completion_source_invalid")
        _require_ref(
            candidate_completion_ref, "candidate_completion_source_invalid"
        )
        _require_idempotency_key(idempotency_key)
        accepted = self._research_graph.query_candidate_completion(
            source_outcome_ref=source_outcome_ref,
            candidate_completion_ref=candidate_completion_ref,
        )
        if accepted is None:
            raise OwnerConflict("candidate_completion_not_accepted")
        binding = _validated_candidate_binding(
            accepted,
            expected_source_outcome_ref=source_outcome_ref,
            expected_candidate_ref=candidate_completion_ref,
        )
        if not self._source_is_current(binding.source, binding.goal_revision):
            raise OwnerConflict("candidate_completion_stale")

        prepared = self._human_collaboration.prepare_quest_completion(
            source=binding.source,
            candidate_completion=binding.candidate,
            candidate_completion_ref=binding.candidate_ref,
            candidate_completion_hash=binding.candidate_hash,
            goal_revision=binding.goal_revision,
            idempotency_key=idempotency_key,
        )
        context_ref = _mapping_ref(
            prepared, "context_ref", "quest_completion_context_invalid"
        )
        current = self.query(context_ref)
        if current is None:
            raise OwnerConflict("quest_completion_context_missing_after_prepare")
        return current

    def query_current(self) -> dict[str, object] | None:
        """Rebuild the latest completion view exclusively from Owner facts."""

        facts = self._query_facts()
        return None if facts is None else self._public_view(facts)

    def query(self, context_ref: str) -> dict[str, object] | None:
        """Rebuild one exact context without substituting the global latest."""

        _require_ref(context_ref, "quest_completion_context_invalid")
        facts = self._query_facts(context_ref)
        return None if facts is None else self._public_view(facts)

    def _public_view(self, facts: _CompletionFacts) -> dict[str, object]:
        human_status = _human_status(facts.preview, facts.decision)
        domain = (
            {"status": "not_attempted"}
            if facts.domain_acceptance is None
            else _public_mapping(facts.domain_acceptance)
        )
        ending = (
            None if facts.ending is None else _public_mapping(facts.ending)
        )
        status = _completion_status(
            human_status=human_status,
            domain_acceptance=facts.domain_acceptance,
            ending=facts.ending,
            source_is_current=facts.source_is_current,
        )
        return {
            "context_ref": facts.context_ref,
            "status": status,
            "quest": {
                "quest_ref": facts.source["quest_ref"],
                "status": "ended" if facts.quest_is_ended else "active",
            },
            "candidate_completion_ref": facts.candidate_ref,
            "candidate_completion_hash": facts.candidate_hash,
            "candidate_completion": dict(facts.candidate),
            "source": dict(facts.source),
            "goal_revision": dict(facts.goal_revision),
            "human_confirmation": {
                "status": human_status,
                "preview": (
                    None if facts.preview is None else dict(facts.preview)
                ),
                "decision": (
                    None if facts.decision is None else dict(facts.decision)
                ),
            },
            "domain_acceptance": domain,
            "ending_transition": ending,
            # Quest completion has no implicit successor Cycle.  A future
            # cycle is authorized only by the independent NextCycle route.
            "successor_cycle": None,
        }

    def process_once(self) -> bool:
        """Advance at most one durable HC, RG, or AE boundary."""

        contexts = self._human_collaboration.query_quest_completion_contexts()
        refs = [
            _mapping_ref(
                context, "context_ref", "quest_completion_context_invalid"
            )
            for context in contexts
        ]
        if self._scheduler_cursor in refs:
            index = refs.index(cast(str, self._scheduler_cursor))
            refs = refs[index + 1 :] + refs[: index + 1]
        for context_ref in refs:
            facts = self._query_facts(context_ref)
            if facts is None or not self._process_facts_once(facts):
                continue
            self._scheduler_cursor = context_ref
            return True
        return False

    def _process_facts_once(self, facts: _CompletionFacts) -> bool:
        if facts.ending is not None or not facts.source_is_current:
            return False

        if facts.preview is None:
            self._human_collaboration.preview_quest_completion(
                facts.context_ref,
                idempotency_key=_operation_key(
                    "quest-completion-preview",
                    facts.context_ref,
                    facts.candidate_ref,
                    facts.candidate_hash,
                    cast(str, facts.goal_revision["goal_revision_ref"]),
                ),
            )
            return True

        if facts.preview.get("status") != "current":
            return False
        if facts.decision is None:
            return False
        decision = facts.decision.get("decision")
        if decision == "rejected":
            return False
        if decision != "confirmed":
            raise OwnerConflict("quest_completion_decision_invalid")

        if facts.domain_acceptance is None:
            self._research_graph.accept_quest_completion(
                context_ref=facts.context_ref,
                source_outcome_ref=cast(
                    str, facts.source["scientific_outcome_ref"]
                ),
                candidate_completion_ref=facts.candidate_ref,
                candidate_completion_hash=facts.candidate_hash,
                goal_revision=facts.goal_revision,
                human_confirmation=facts.decision,
                idempotency_key=_operation_key(
                    "quest-completion-domain",
                    facts.context_ref,
                    facts.candidate_ref,
                    _receipt_ref(facts.decision["receipt"]),
                ),
            )
            return True

        if facts.domain_acceptance.get("status") != "accepted":
            return False
        commit = self._advancement_engine.query_reasoning_stage_commit(
            cast(str, facts.source["reasoning_stage_run_request_ref"])
        )
        if not _commit_closes_candidate(commit, facts):
            return False
        completion_ref = facts.domain_acceptance.get("completion_ref")
        completion_receipt = facts.domain_acceptance.get("receipt")
        if not isinstance(completion_ref, str) or not completion_ref:
            raise OwnerConflict("quest_completion_acceptance_invalid")
        _validate_receipt(
            completion_receipt,
            expected_issuer="research_graph",
            code="quest_completion_acceptance_invalid",
        )
        self._advancement_engine.end_quest(
            quest_ref=cast(str, facts.source["quest_ref"]),
            cycle_ref=cast(str, facts.source["cycle_ref"]),
            foreground_epoch=cast(int, facts.source["foreground_epoch"]),
            reasoning_stage_run_request_ref=cast(
                str, facts.source["reasoning_stage_run_request_ref"]
            ),
            candidate_completion_ref=facts.candidate_ref,
            completion_ref=completion_ref,
            completion_receipt=completion_receipt,
            idempotency_key=_operation_key(
                "quest-ending",
                facts.context_ref,
                completion_ref,
                _receipt_ref(completion_receipt),
            ),
        )
        return True

    def _query_facts(
        self, context_ref: str | None = None
    ) -> _CompletionFacts | None:
        raw_context = (
            self._human_collaboration.query_current_quest_completion()
            if context_ref is None
            else self._human_collaboration.query_quest_completion(context_ref)
        )
        if raw_context is None:
            return None
        context = _require_mapping(raw_context, "quest_completion_context_invalid")
        context_ref = _mapping_ref(
            context, "context_ref", "quest_completion_context_invalid"
        )
        source = _context_mapping(
            context, "source", "quest_completion_context_invalid"
        )
        source_outcome_ref = _mapping_ref(
            source,
            "scientific_outcome_ref",
            "quest_completion_context_invalid",
        )
        candidate_ref = _mapping_ref(
            context,
            "candidate_completion_ref",
            "quest_completion_context_invalid",
        )
        accepted = self._research_graph.query_candidate_completion(
            source_outcome_ref=source_outcome_ref,
            candidate_completion_ref=candidate_ref,
        )
        if accepted is None:
            raise OwnerConflict("quest_completion_source_unavailable")
        binding = _validated_candidate_binding(
            accepted,
            expected_source_outcome_ref=source_outcome_ref,
            expected_candidate_ref=candidate_ref,
        )
        context_candidate = _context_mapping(
            context,
            "candidate_completion",
            "quest_completion_context_invalid",
        )
        context_goal = _context_mapping(
            context, "goal_revision", "quest_completion_context_invalid"
        )
        context_hash = _mapping_ref(
            context,
            "candidate_completion_hash",
            "quest_completion_context_invalid",
        )
        if (
            source != binding.source
            or context_candidate != binding.candidate
            or context_goal != binding.goal_revision
            or context_hash != binding.candidate_hash
        ):
            raise OwnerConflict("quest_completion_context_binding_invalid")

        human = _context_mapping(
            context, "human_confirmation", "quest_completion_context_invalid"
        )
        preview = _optional_mapping(
            human.get("preview"), "quest_completion_preview_invalid"
        )
        decision = _optional_mapping(
            human.get("decision"), "quest_completion_decision_invalid"
        )
        _validate_human_facts(preview, decision, binding)
        domain = self._research_graph.query_quest_completion_acceptance(
            candidate_ref
        )
        if domain is not None:
            domain = _require_mapping(
                domain, "quest_completion_acceptance_invalid"
            )
            _validate_domain_acceptance(domain, decision, binding)
        quest_ending = self._advancement_engine.query_quest_ending(
            cast(str, binding.source["quest_ref"])
        )
        ending = (
            quest_ending
            if isinstance(quest_ending, dict)
            and quest_ending.get("candidate_completion_ref")
            == binding.candidate_ref
            else None
        )
        if ending is not None:
            ending = _require_mapping(ending, "quest_ending_invalid")
            _validate_ending(ending, domain, binding)
        return _CompletionFacts(
            context=context,
            context_ref=context_ref,
            candidate=binding.candidate,
            candidate_ref=binding.candidate_ref,
            candidate_hash=binding.candidate_hash,
            source=binding.source,
            goal_revision=binding.goal_revision,
            preview=preview,
            decision=decision,
            domain_acceptance=domain,
            ending=ending,
            quest_is_ended=quest_ending is not None,
            source_is_current=(
                ending is not None
                or self._source_is_current(binding.source, binding.goal_revision)
            ),
        )

    def _source_is_current(
        self,
        source: dict[str, object],
        goal_revision: dict[str, object],
    ) -> bool:
        quest_ref = cast(str, source["quest_ref"])
        current_goal = (
            self._research_graph.query_current_quest_goal_revision(quest_ref)
        )
        if current_goal != goal_revision:
            return False
        foreground = self._advancement_engine.query_foreground(quest_ref)
        return bool(
            isinstance(foreground, dict)
            and foreground.get("quest_ref", quest_ref) == quest_ref
            and foreground.get("cycle_ref") == source["cycle_ref"]
            and foreground.get("stage") == "reasoning"
            and foreground.get("epoch") == source["foreground_epoch"]
            and foreground.get("status")
            in {"active", "awaiting_quest_completion"}
        )


@dataclass(frozen=True)
class _CandidateBinding:
    candidate: dict[str, object]
    candidate_ref: str
    candidate_hash: str
    source: dict[str, object]
    goal_revision: dict[str, object]


_SOURCE_FIELDS = {
    "quest_ref",
    "cycle_ref",
    "reasoning_stage_run_request_ref",
    "scientific_outcome_ref",
    "foreground_epoch",
    "reasoning_content_acceptance_receipt_ref",
    "reasoning_domain_acceptance_receipt_ref",
}
_CANDIDATE_FIELDS = {
    "schema_ref",
    "kind",
    "source_quest_ref",
    "source_cycle_ref",
    "source_reasoning_stage_run_request_ref",
    "source_scientific_outcome_ref",
    "source_question_ref",
    "source_foreground_epoch",
    "current_quest_ref",
    "current_goal_revision_ref",
    "completion_milestone_basis_refs",
    "rationale",
    "is_authoritative",
}


def _validated_candidate_binding(
    raw: object,
    *,
    expected_source_outcome_ref: str,
    expected_candidate_ref: str,
) -> _CandidateBinding:
    binding = _require_mapping(raw, "candidate_completion_binding_invalid")
    candidate = _context_mapping(
        binding, "candidate_completion", "candidate_completion_binding_invalid"
    )
    source = _context_mapping(
        binding, "source", "candidate_completion_binding_invalid"
    )
    goal_revision = _context_mapping(
        binding, "goal_revision", "candidate_completion_binding_invalid"
    )
    candidate_ref = _mapping_ref(
        binding,
        "candidate_completion_ref",
        "candidate_completion_binding_invalid",
    )
    candidate_hash = _mapping_ref(
        binding,
        "candidate_completion_hash",
        "candidate_completion_binding_invalid",
    )
    if (
        candidate_ref != expected_candidate_ref
        or candidate_hash != canonical_hash(candidate)
        or set(candidate) != _CANDIDATE_FIELDS
        or candidate.get("schema_ref") != CANDIDATE_COMPLETION_SCHEMA_REF
        or candidate.get("kind") != "CandidateCompletion"
        or candidate.get("is_authoritative") is not False
        or set(source) != _SOURCE_FIELDS
    ):
        raise OwnerConflict("candidate_completion_binding_invalid")
    for field in _SOURCE_FIELDS - {"foreground_epoch"}:
        _mapping_ref(source, field, "candidate_completion_binding_invalid")
    epoch = source.get("foreground_epoch")
    basis_refs = candidate.get("completion_milestone_basis_refs")
    if (
        type(epoch) is not int
        or cast(int, epoch) < 1
        or not isinstance(basis_refs, list)
        or not basis_refs
        or any(not isinstance(value, str) or not value for value in basis_refs)
        or len(basis_refs) != len(set(cast(list[str], basis_refs)))
        or not isinstance(candidate.get("rationale"), str)
        or not cast(str, candidate["rationale"]).strip()
    ):
        raise OwnerConflict("candidate_completion_binding_invalid")
    expected_source = {
        "source_quest_ref": source["quest_ref"],
        "source_cycle_ref": source["cycle_ref"],
        "source_reasoning_stage_run_request_ref": source[
            "reasoning_stage_run_request_ref"
        ],
        "source_scientific_outcome_ref": source["scientific_outcome_ref"],
        "source_foreground_epoch": source["foreground_epoch"],
        "current_quest_ref": source["quest_ref"],
    }
    if any(candidate.get(key) != value for key, value in expected_source.items()):
        raise OwnerConflict("candidate_completion_binding_invalid")
    goal_ref = _mapping_ref(
        goal_revision,
        "goal_revision_ref",
        "candidate_completion_binding_invalid",
    )
    if (
        source["scientific_outcome_ref"] != expected_source_outcome_ref
        or goal_revision.get("kind") != "QuestGoalRevision"
        or goal_revision.get("quest_ref") != source["quest_ref"]
        or candidate.get("current_goal_revision_ref") != goal_ref
    ):
        raise OwnerConflict("candidate_completion_binding_invalid")
    return _CandidateBinding(
        candidate=dict(candidate),
        candidate_ref=candidate_ref,
        candidate_hash=candidate_hash,
        source=dict(source),
        goal_revision=dict(goal_revision),
    )


def _validate_human_facts(
    preview: dict[str, object] | None,
    decision: dict[str, object] | None,
    binding: _CandidateBinding,
) -> None:
    if preview is None:
        if decision is not None:
            raise OwnerConflict("quest_completion_decision_without_preview")
        return
    if preview.get("status") not in {"current", "stale"}:
        raise OwnerConflict("quest_completion_preview_invalid")
    _mapping_ref(preview, "ref", "quest_completion_preview_invalid")
    _mapping_ref(preview, "hash", "quest_completion_preview_invalid")
    if (
        preview.get("candidate_completion_ref") != binding.candidate_ref
        or preview.get("candidate_completion_hash") != binding.candidate_hash
        or preview.get("quest_ref") != binding.source["quest_ref"]
        or preview.get("goal_revision_ref")
        != binding.goal_revision["goal_revision_ref"]
        or preview.get("completion_milestone_basis_refs")
        != binding.candidate["completion_milestone_basis_refs"]
    ):
        raise OwnerConflict("quest_completion_preview_invalid")
    if decision is None:
        return
    if decision.get("decision") not in {"confirmed", "rejected"}:
        raise OwnerConflict("quest_completion_decision_invalid")
    receipt = decision.get("receipt")
    _validate_receipt(
        receipt,
        expected_issuer="human_collaboration",
        code="quest_completion_decision_invalid",
    )


def _validate_domain_acceptance(
    domain: dict[str, object],
    decision: dict[str, object] | None,
    binding: _CandidateBinding,
) -> None:
    status = domain.get("status")
    if status not in {"accepted", "rejected"}:
        raise OwnerConflict("quest_completion_acceptance_invalid")
    if decision is None or decision.get("decision") != "confirmed":
        raise OwnerConflict("quest_completion_acceptance_without_confirmation")
    if domain.get("candidate_completion_ref", binding.candidate_ref) != (
        binding.candidate_ref
    ) or domain.get(
        "goal_revision_ref", binding.goal_revision["goal_revision_ref"]
    ) != binding.goal_revision["goal_revision_ref"]:
        raise OwnerConflict("quest_completion_acceptance_invalid")
    if status == "accepted":
        _mapping_ref(
            domain, "completion_ref", "quest_completion_acceptance_invalid"
        )
        _validate_receipt(
            domain.get("receipt"),
            expected_issuer="research_graph",
            code="quest_completion_acceptance_invalid",
        )


def _validate_ending(
    ending: dict[str, object],
    domain: dict[str, object] | None,
    binding: _CandidateBinding,
) -> None:
    if domain is None or domain.get("status") != "accepted":
        raise OwnerConflict("quest_ending_without_completion_acceptance")
    if (
        ending.get("status") != "ended"
        or ending.get("quest_ref", binding.source["quest_ref"])
        != binding.source["quest_ref"]
        or ending.get("candidate_completion_ref", binding.candidate_ref)
        != binding.candidate_ref
    ):
        raise OwnerConflict("quest_ending_invalid")
    _mapping_ref(ending, "transition_ref", "quest_ending_invalid")
    _validate_receipt(
        ending.get("receipt"),
        expected_issuer="advancement_engine",
        code="quest_ending_invalid",
    )


def _commit_closes_candidate(
    commit: object | None, facts: _CompletionFacts
) -> bool:
    if commit is None:
        return False
    closure = _object_field(commit, "closure")
    if not isinstance(closure, dict):
        return False
    request_ref = _object_field(commit, "request_ref")
    if request_ref is not None and request_ref != facts.source[
        "reasoning_stage_run_request_ref"
    ]:
        return False
    return bool(
        closure.get("transition_kind") == "candidate_completion"
        and closure.get("transition_ref") == facts.candidate_ref
        and closure.get("transition_hash") == facts.candidate_hash
        and closure.get("transition") == facts.candidate
    )


def _completion_status(
    *,
    human_status: str,
    domain_acceptance: dict[str, object] | None,
    ending: dict[str, object] | None,
    source_is_current: bool,
) -> str:
    if ending is not None:
        return "ended"
    if not source_is_current:
        return "stale"
    if domain_acceptance is not None:
        return (
            "domain_accepted"
            if domain_acceptance.get("status") == "accepted"
            else "domain_rejected"
        )
    if human_status == "confirmed":
        return "human_confirmed"
    if human_status == "rejected":
        return "rejected"
    if human_status == "awaiting_response":
        return "awaiting_human_confirmation"
    if human_status == "stale":
        return "stale"
    return "prepared"


def _human_status(
    preview: dict[str, object] | None,
    decision: dict[str, object] | None,
) -> str:
    if preview is None:
        return "not_attempted"
    if preview.get("status") == "stale":
        return "stale"
    if decision is None:
        return "awaiting_response"
    return cast(str, decision["decision"])


def _operation_key(prefix: str, *values: str) -> str:
    return f"{prefix}:{canonical_hash(list(values))}"


def _require_idempotency_key(value: object) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise OwnerConflict("idempotency_key_invalid")


def _require_ref(value: object, code: str) -> str:
    if not isinstance(value, str) or not value:
        raise OwnerConflict(code)
    return value


def _require_mapping(value: object, code: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise OwnerConflict(code)
    return value


def _context_mapping(
    value: dict[str, object], key: str, code: str
) -> dict[str, object]:
    return _require_mapping(value.get(key), code)


def _optional_mapping(value: object, code: str) -> dict[str, object] | None:
    return None if value is None else _require_mapping(value, code)


def _mapping_ref(value: dict[str, object], key: str, code: str) -> str:
    return _require_ref(value.get(key), code)


def _object_field(value: object, key: str) -> object:
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


def _validate_receipt(
    value: object, *, expected_issuer: str, code: str
) -> None:
    issuer = _object_field(value, "issuer")
    if issuer != expected_issuer:
        raise OwnerConflict(code)
    for field in ("kind", "receipt_ref", "subject_ref", "payload_hash"):
        _require_ref(_object_field(value, field), code)


def _receipt_ref(value: object) -> str:
    return _require_ref(
        _object_field(value, "receipt_ref"),
        "quest_completion_receipt_invalid",
    )


def _public_mapping(value: dict[str, object]) -> dict[str, object]:
    public: dict[str, object] = {}
    for key, item in value.items():
        as_public = getattr(item, "as_public_dict", None)
        public[key] = as_public() if callable(as_public) else item
    return public
