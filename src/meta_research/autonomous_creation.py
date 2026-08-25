from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, cast

from meta_research.acquisition import AcquisitionProvider
from meta_research.owners.common import AcceptanceReceipt, OwnerConflict, canonical_hash


class AutonomousHumanCollaboration(Protocol):
    def query_broad_research_authorization(
        self, quest_ref: str
    ) -> dict[str, object] | None: ...

    def prepare_autonomous_creation(self, **values: object) -> dict[str, object]: ...

    def query_current_autonomous_creation(self) -> dict[str, object] | None: ...

    def query_autonomous_creation(
        self, reasoning_checkpoint_ref: str
    ) -> dict[str, object] | None: ...

    def query_autonomous_creation_context(
        self, context_ref: str
    ) -> dict[str, object] | None: ...

    def query_autonomous_creation_contexts(
        self,
    ) -> tuple[dict[str, object], ...]: ...

    def form_autonomous_question_proposal(
        self,
        context_ref: str,
        *,
        literature_snapshot_ref: str,
        idempotency_key: str,
    ) -> dict[str, object]: ...

    def select_autonomous_question_content(
        self,
        context_ref: str,
        *,
        content_ref: str,
        content_hash: str,
        content_receipt: AcceptanceReceipt,
        idempotency_key: str,
    ) -> dict[str, object]: ...


class AutonomousAgentRuntime(Protocol):
    def prepare_acquisition_session(
        self,
        *,
        initialization_id: str,
        draft_revision: int,
        config: dict[str, object],
        provider: AcquisitionProvider,
    ) -> object: ...

    def query_acquisition_session(
        self,
        *,
        initialization_id: str | None = None,
        session_ref: str | None = None,
        quest_ref: str | None = None,
    ) -> object | None: ...

    def query_deepfetch_run(self, request_ref: str) -> object | None: ...

    def query_human_requests(self, **values: object) -> tuple[dict[str, object], ...]: ...

    def query_reasoning_stage_run(self, request_ref: str) -> object | None: ...

    def query_reasoning_autonomous_checkpoint(
        self, checkpoint_ref: str
    ) -> object | None: ...

    def bind_acquisition_session_to_quest(
        self, initialization_id: str, quest_ref: str
    ) -> object | None: ...


class AutonomousResearchMemory(Protocol):
    def query_reasoning_scientific_candidate_by_checkpoint_ref(
        self, checkpoint_ref: str
    ) -> object | None: ...

    def query_literature_snapshot_for_request(self, request_ref: str) -> object | None: ...

    def query_autonomous_question_content_by_checkpoint_ref(
        self, checkpoint_ref: str
    ) -> object | None: ...

    def accept_autonomous_question_content(self, **values: object) -> object: ...

    def ensure_question_literature_revision(self, **values: object) -> object: ...

    def query_current_question_literature_revision(
        self, question_ref: str
    ) -> dict[str, object] | None: ...


class AutonomousResearchGraph(Protocol):
    def query_quest_by_ref(self, quest_ref: str) -> object | None: ...

    def query_reasoning_scientific_decision_by_outcome_ref(
        self, outcome_ref: str
    ) -> object | None: ...

    def query_autonomous_question_by_checkpoint_ref(
        self, checkpoint_ref: str
    ) -> object | None: ...

    def accept_autonomous_question(self, **values: object) -> object: ...


class AutonomousAdvancementEngine(Protocol):
    def query_foreground(self, quest_ref: str) -> dict[str, object] | None: ...

    def query_active_foregrounds(
        self, *, stage: str | None = None
    ) -> tuple[dict[str, object], ...]: ...

    def query_reasoning_stage_request(self, cycle_ref: str) -> object | None: ...

    def issue_autonomous_deepfetch_request(self, **values: object) -> object: ...

    def query_autonomous_deepfetch_request(self, context_ref: str) -> object | None: ...

    def record_autonomous_deepfetch_succeeded(self, **values: object) -> None: ...

    def record_autonomous_deepfetch_failed(self, **values: object) -> None: ...

    def authorize_autonomous_question_dispatch(self, **values: object) -> object: ...

    def query_autonomous_question_dispatch(self, context_ref: str) -> object | None: ...


@dataclass(frozen=True)
class _AutonomousFacts:
    context: dict[str, object]
    checkpoint_ref: str
    source: dict[str, object]
    scope: dict[str, object]
    proposal: dict[str, object] | None
    request: object | None
    run: object | None
    snapshot: object | None
    content: object | None
    dispatch: object | None
    accepted_question: object | None
    literature_revision: dict[str, object] | None
    human_request: dict[str, object] | None


class AutonomousCreationService:
    """Recoverable create_question coordinator for one Reasoning checkpoint.

    The service owns no scientific, content, graph, or lifecycle truth.  Each
    pass performs at most one durable Owner write and every restart resumes by
    querying the first missing issuer-owned boundary.
    """

    def __init__(
        self,
        human_collaboration: AutonomousHumanCollaboration,
        advancement_engine: AutonomousAdvancementEngine,
        agent_runtime: AutonomousAgentRuntime,
        research_memory: AutonomousResearchMemory,
        research_graph: AutonomousResearchGraph,
        acquisition_provider: AcquisitionProvider,
    ) -> None:
        self._human_collaboration = human_collaboration
        self._advancement_engine = advancement_engine
        self._agent_runtime = agent_runtime
        self._research_memory = research_memory
        self._research_graph = research_graph
        self._acquisition_provider = acquisition_provider
        self._scheduler_cursor: str | None = None

    def start(
        self,
        *,
        reasoning_checkpoint_ref: str,
        source_scientific_outcome_ref: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        _require_ref(reasoning_checkpoint_ref, "reasoning_checkpoint_ref_invalid")
        _require_ref(
            source_scientific_outcome_ref,
            "source_scientific_outcome_ref_invalid",
        )
        _require_key(idempotency_key)
        candidate = (
            self._research_memory.query_reasoning_scientific_candidate_by_checkpoint_ref(
                reasoning_checkpoint_ref
            )
        )
        if candidate is None:
            raise OwnerConflict("reasoning_autonomous_checkpoint_not_accepted")
        outcome = _mapping_field(candidate, "scientific_outcome")
        scope = _mapping_field(candidate, "autonomous_scope")
        if outcome.get("outcome_ref") != source_scientific_outcome_ref:
            raise OwnerConflict("autonomous_creation_source_invalid")
        decision = self._query_scientific_decision(source_scientific_outcome_ref)
        if decision is None or _field(decision, "decision") != "accepted":
            raise OwnerConflict("reasoning_scientific_candidate_not_accepted")
        if _field(decision, "scientific_outcome_ref") not in {
            None,
            source_scientific_outcome_ref,
        }:
            raise OwnerConflict("autonomous_creation_source_invalid")

        checkpoint_hash = _require_ref(
            _field(candidate, "checkpoint_hash"),
            "reasoning_checkpoint_hash_invalid",
        )
        candidate_receipt = _receipt(_field(candidate, "receipt"), "research_memory")
        decision_receipt = _receipt(_field(decision, "receipt"), "research_graph")
        source = {
            "quest_ref": outcome["quest_ref"],
            "cycle_ref": outcome["cycle_ref"],
            "reasoning_stage_run_request_ref": outcome["stage_run_request_ref"],
            "scientific_outcome_ref": outcome["outcome_ref"],
            "question_ref": outcome["question_ref"],
            "foreground_epoch": outcome["foreground_epoch"],
            "reasoning_checkpoint_ref": reasoning_checkpoint_ref,
            "reasoning_checkpoint_hash": checkpoint_hash,
            "autonomous_scope_content_acceptance_receipt_ref": (
                candidate_receipt.receipt_ref
            ),
            "preliminary_scientific_acceptance_receipt_ref": (
                decision_receipt.receipt_ref
            ),
        }
        existing = self._human_collaboration.query_autonomous_creation(
            reasoning_checkpoint_ref
        )
        if existing is not None:
            if (
                existing.get("checkpoint")
                != {"ref": reasoning_checkpoint_ref, "hash": checkpoint_hash}
                or existing.get("source") != source
                or existing.get("scientific_outcome") != outcome
                or existing.get("scope") != scope
            ):
                raise OwnerConflict("autonomous_creation_identity_conflict")
            current = self.query(reasoning_checkpoint_ref)
            if current is None:
                raise OwnerConflict("autonomous_creation_missing_after_prepare")
            return current

        authorization = self._human_collaboration.query_broad_research_authorization(
            cast(str, source["quest_ref"])
        )
        if authorization is None or authorization.get("status") != "granted":
            raise OwnerConflict("broad_research_authorization_required")
        self._assert_source_current(source)
        self._human_collaboration.prepare_autonomous_creation(
            source=source,
            scientific_outcome=outcome,
            reasoning_checkpoint_ref=reasoning_checkpoint_ref,
            reasoning_checkpoint_hash=checkpoint_hash,
            autonomous_scope=scope,
            autonomous_scope_hash=_require_ref(
                _field(candidate, "autonomous_scope_hash"),
                "autonomous_scope_hash_invalid",
            ),
            broad_authorization=authorization,
            idempotency_key=idempotency_key,
        )
        current = self.query(reasoning_checkpoint_ref)
        if current is None:
            raise OwnerConflict("autonomous_creation_missing_after_prepare")
        return current

    def query_current(self) -> dict[str, object] | None:
        return self._query()

    def query(
        self, reasoning_checkpoint_ref: str
    ) -> dict[str, object] | None:
        _require_ref(reasoning_checkpoint_ref, "reasoning_checkpoint_ref_invalid")
        return self._query(reasoning_checkpoint_ref=reasoning_checkpoint_ref)

    def query_context(self, context_ref: str) -> dict[str, object] | None:
        _require_ref(context_ref, "autonomous_context_ref_invalid")
        return self._query(context_ref=context_ref)

    def _query(
        self,
        *,
        reasoning_checkpoint_ref: str | None = None,
        context_ref: str | None = None,
    ) -> dict[str, object] | None:
        facts = self._facts(
            reasoning_checkpoint_ref=reasoning_checkpoint_ref,
            context_ref=context_ref,
        )
        if facts is None:
            return None
        authorization = _mapping_field(facts.context, "broad_authorization")
        authorization_receipt_ref = _authorization_receipt_ref(authorization)
        request_receipt = _receipt_public(_field(facts.request, "authorization_receipt"))
        deepfetch: dict[str, object] = {
            "required": True,
            "waiver_allowed": False,
            "human_authorization_required": False,
            "authorization_receipt_ref": authorization_receipt_ref,
            "status": _deepfetch_status(facts.request, facts.run, facts.snapshot),
            "request_ref": _field(facts.request, "request_ref"),
            "run_ref": _field(facts.run, "run_ref"),
            "literature_snapshot_ref": _field(facts.snapshot, "snapshot_ref"),
        }
        if request_receipt is not None:
            deepfetch["request_receipt"] = request_receipt
        if facts.snapshot is not None:
            deepfetch["literature_snapshot_receipt"] = _receipt_public(
                _field(facts.snapshot, "receipt")
            )

        content_acceptance: dict[str, object]
        if facts.content is None:
            content_acceptance = {"status": "not_attempted"}
        else:
            content_acceptance = {
                "status": "accepted",
                "content_ref": _field(facts.content, "content_ref"),
                "content_hash": _field(facts.content, "content_hash"),
                "receipt": _receipt_public(_field(facts.content, "receipt")),
            }
        anchor = _public_component(facts.accepted_question, "question_anchor")
        presence = _public_component(
            facts.accepted_question, "graph_presence_fact"
        )
        research_state = _public_component(
            facts.accepted_question, "question_research_state_fact"
        )
        status = _autonomous_status(facts)
        return {
            "context_ref": facts.context["context_ref"],
            "generation": facts.context["generation"],
            "creation_mode": "AutonomousCreation",
            "status": status,
            "checkpoint": dict(_mapping_field(facts.context, "checkpoint")),
            "source": dict(facts.source),
            "scope": dict(facts.scope),
            "proposal": None if facts.proposal is None else dict(facts.proposal),
            "deepfetch": deepfetch,
            "waiver": None,
            "human_confirmation": None,
            "human_request": facts.human_request,
            "content_acceptance": content_acceptance,
            "dispatch_eligibility": (
                {"status": "not_attempted"}
                if facts.dispatch is None
                else _public_object(facts.dispatch)
            ),
            "question_anchor": anchor,
            "graph_presence_fact": presence,
            "question_research_state_fact": research_state,
            "literature_revision": facts.literature_revision,
            "next_cycle_proposal": None,
            "successor_cycle": None,
        }

    def process_once(self) -> bool:
        """Advance one fair work item across at most one Owner boundary."""

        for kind, ref in self._scheduled_work_items():
            if kind == "checkpoint":
                started = self._start_checkpoint_once(ref)
                if started is None:
                    continue
                self._scheduler_cursor = "context:" + cast(
                    str, started["context_ref"]
                )
                return True
            facts = self._facts(context_ref=ref)
            if facts is None or not self._process_facts_once(facts):
                continue
            self._scheduler_cursor = f"context:{ref}"
            return True
        return False

    def _process_facts_once(self, facts: _AutonomousFacts) -> bool:
        if facts.literature_revision is not None:
            return False
        if facts.human_request is not None:
            return False
        if not self._source_is_current(facts.source):
            return False

        if facts.request is None:
            quest_ref = cast(str, facts.source["quest_ref"])
            session = self._agent_runtime.query_acquisition_session(
                quest_ref=quest_ref
            )
            prepared_or_bound = False
            if session is None:
                quest = self._research_graph.query_quest_by_ref(quest_ref)
                if quest is None:
                    raise OwnerConflict("autonomous_deepfetch_quest_unavailable")
                draft = _field(quest, "draft")
                if not isinstance(draft, dict):
                    raise OwnerConflict("autonomous_deepfetch_quest_policy_unavailable")
                literature = draft.get("literature")
                if not isinstance(literature, dict):
                    raise OwnerConflict("autonomous_deepfetch_quest_policy_unavailable")
                config = {
                    "mode": literature.get("mode"),
                    "library_entry_url": literature.get("library_entry_url"),
                }
                initialization_id = _require_ref(
                    _field(quest, "initialization_id"),
                    "autonomous_deepfetch_quest_invalid",
                )
                draft_revision = _field(quest, "draft_revision")
                if type(draft_revision) is not int or cast(int, draft_revision) < 1:
                    raise OwnerConflict("autonomous_deepfetch_quest_invalid")
                self._agent_runtime.prepare_acquisition_session(
                    initialization_id=initialization_id,
                    draft_revision=cast(int, draft_revision),
                    config=config,
                    provider=self._acquisition_provider,
                )
                session = self._agent_runtime.bind_acquisition_session_to_quest(
                    initialization_id, quest_ref
                )
                prepared_or_bound = True
            if prepared_or_bound:
                # Preparation and Quest binding are one AR-owned boundary.  A
                # later coordinator pass may issue the AE DeepFetch command;
                # never combine both Owner writes in one pass.
                return True
            if (
                session is None
                or _field(session, "quest_ref") != quest_ref
                or _field(session, "status") != "ready"
                or _field(session, "slot_held") is not False
            ):
                return False
            self._advancement_engine.issue_autonomous_deepfetch_request(
                context=facts.context,
                acquisition_session=session,
                idempotency_key=_key(
                    "autonomous-deepfetch",
                    cast(str, facts.context["context_ref"]),
                    facts.checkpoint_ref,
                ),
            )
            return True
        if facts.snapshot is None:
            return False
        if facts.proposal is None:
            self._human_collaboration.form_autonomous_question_proposal(
                cast(str, facts.context["context_ref"]),
                literature_snapshot_ref=cast(str, _field(facts.snapshot, "snapshot_ref")),
                idempotency_key=_key(
                    "autonomous-proposal",
                    cast(str, facts.context["context_ref"]),
                    cast(str, _field(facts.snapshot, "snapshot_ref")),
                ),
            )
            return True
        if facts.content is None:
            decision = self._query_scientific_decision(
                cast(str, facts.source["scientific_outcome_ref"])
            )
            if decision is None:
                raise OwnerConflict("reasoning_scientific_candidate_not_accepted")
            self._research_memory.accept_autonomous_question_content(
                reasoning_checkpoint_ref=facts.checkpoint_ref,
                source_scientific_outcome_ref=facts.source[
                    "scientific_outcome_ref"
                ],
                scientific_decision_receipt=_field(decision, "receipt"),
                literature_snapshot_ref=_field(facts.snapshot, "snapshot_ref"),
                idempotency_key=_key(
                    "autonomous-content",
                    cast(str, facts.context["context_ref"]),
                    cast(str, facts.proposal["hash"]),
                ),
            )
            return True
        if facts.context.get("selection") is None:
            self._human_collaboration.select_autonomous_question_content(
                cast(str, facts.context["context_ref"]),
                content_ref=cast(str, _field(facts.content, "content_ref")),
                content_hash=cast(str, _field(facts.content, "content_hash")),
                content_receipt=_receipt(
                    _field(facts.content, "receipt"), "research_memory"
                ),
                idempotency_key=_key(
                    "autonomous-select",
                    cast(str, facts.context["context_ref"]),
                    cast(str, _field(facts.content, "content_ref")),
                ),
            )
            return True
        if facts.dispatch is None:
            self._advancement_engine.authorize_autonomous_question_dispatch(
                context=facts.context,
                content=facts.content,
                idempotency_key=_key(
                    "autonomous-dispatch",
                    cast(str, facts.context["context_ref"]),
                    cast(str, _field(facts.content, "content_ref")),
                ),
            )
            return True
        if facts.accepted_question is None:
            self._research_graph.accept_autonomous_question(
                content=facts.content,
                dispatch_receipt=_field(facts.dispatch, "receipt"),
                idempotency_key=_key(
                    "autonomous-question",
                    cast(str, facts.context["context_ref"]),
                    cast(str, _field(facts.content, "content_ref")),
                ),
            )
            return True
        accepted_binding = _field(
            facts.accepted_question, "accepted_question_binding"
        )
        if accepted_binding is None:
            raise OwnerConflict("autonomous_question_binding_invalid")
        snapshot_binding = _snapshot_binding(facts.snapshot)
        self._research_memory.ensure_question_literature_revision(
            question_binding=accepted_binding,
            source_snapshot_binding=snapshot_binding,
            idempotency_key=_key(
                "autonomous-literature-revision",
                cast(str, facts.context["context_ref"]),
                cast(str, _field(facts.accepted_question, "graph_revision_ref")),
            ),
        )
        return True

    def _scheduled_work_items(self) -> tuple[tuple[str, str], ...]:
        contexts = self._human_collaboration.query_autonomous_creation_contexts()
        items: list[tuple[str, str]] = []
        known_checkpoints: set[str] = set()
        for context in contexts:
            context_ref = _require_ref(
                context.get("context_ref"), "autonomous_context_ref_invalid"
            )
            checkpoint = _mapping_field(context, "checkpoint")
            checkpoint_ref = _require_ref(
                checkpoint.get("ref"), "reasoning_checkpoint_ref_invalid"
            )
            known_checkpoints.add(checkpoint_ref)
            items.append(("context", context_ref))

        for foreground in self._advancement_engine.query_active_foregrounds(
            stage="reasoning"
        ):
            cycle_ref = _require_ref(
                foreground.get("cycle_ref"), "reasoning_cycle_ref_invalid"
            )
            request = self._advancement_engine.query_reasoning_stage_request(
                cycle_ref
            )
            request_ref = _field(request, "request_ref")
            if not isinstance(request_ref, str) or not request_ref:
                continue
            run = self._agent_runtime.query_reasoning_stage_run(request_ref)
            checkpoint = _field(run, "autonomous_checkpoint")
            checkpoint_ref = _field(checkpoint, "checkpoint_ref")
            if (
                isinstance(checkpoint_ref, str)
                and checkpoint_ref
                and checkpoint_ref not in known_checkpoints
            ):
                known_checkpoints.add(checkpoint_ref)
                items.append(("checkpoint", checkpoint_ref))

        keys = [f"{kind}:{ref}" for kind, ref in items]
        if self._scheduler_cursor in keys:
            index = keys.index(cast(str, self._scheduler_cursor))
            items = items[index + 1 :] + items[: index + 1]
        return tuple(items)

    def _start_checkpoint_once(
        self, checkpoint_ref: str
    ) -> dict[str, object] | None:
        if self._human_collaboration.query_autonomous_creation(
            checkpoint_ref
        ) is not None:
            return None
        checkpoint = self._agent_runtime.query_reasoning_autonomous_checkpoint(
            checkpoint_ref
        )
        if checkpoint is None:
            return None
        candidate = (
            self._research_memory.query_reasoning_scientific_candidate_by_checkpoint_ref(
                checkpoint_ref
            )
        )
        if candidate is None:
            return None
        outcome = _mapping_field(candidate, "scientific_outcome")
        outcome_ref = _require_ref(
            outcome.get("outcome_ref"), "scientific_outcome_ref_invalid"
        )
        decision = self._query_scientific_decision(outcome_ref)
        if decision is None or _field(decision, "decision") != "accepted":
            return None
        return self.start(
            reasoning_checkpoint_ref=checkpoint_ref,
            source_scientific_outcome_ref=outcome_ref,
            idempotency_key=f"daemon-autonomous-{checkpoint_ref}",
        )

    def _facts(
        self,
        *,
        reasoning_checkpoint_ref: str | None = None,
        context_ref: str | None = None,
    ) -> _AutonomousFacts | None:
        if reasoning_checkpoint_ref is not None and context_ref is not None:
            raise OwnerConflict("autonomous_context_query_invalid")
        if context_ref is not None:
            context = self._human_collaboration.query_autonomous_creation_context(
                context_ref
            )
        elif reasoning_checkpoint_ref is not None:
            context = self._human_collaboration.query_autonomous_creation(
                reasoning_checkpoint_ref
            )
        else:
            context = self._human_collaboration.query_current_autonomous_creation()
        if context is None:
            return None
        checkpoint = _mapping_field(context, "checkpoint")
        checkpoint_ref = _require_ref(
            checkpoint.get("ref"), "reasoning_checkpoint_ref_invalid"
        )
        source = _mapping_field(context, "source")
        scope = _mapping_field(context, "scope")
        proposal = _optional_mapping(context.get("proposal"))
        request = self._advancement_engine.query_autonomous_deepfetch_request(
            cast(str, context["context_ref"])
        )
        run = (
            None
            if request is None
            else self._agent_runtime.query_deepfetch_run(
                cast(str, _field(request, "request_ref"))
            )
        )
        snapshot = (
            None
            if request is None
            else self._research_memory.query_literature_snapshot_for_request(
                cast(str, _field(request, "request_ref"))
            )
        )
        content = (
            self._research_memory.query_autonomous_question_content_by_checkpoint_ref(
                checkpoint_ref
            )
        )
        dispatch = self._advancement_engine.query_autonomous_question_dispatch(
            cast(str, context["context_ref"])
        )
        accepted = self._research_graph.query_autonomous_question_by_checkpoint_ref(
            checkpoint_ref
        )
        question_ref = _accepted_question_ref(accepted)
        literature_revision = (
            None
            if question_ref is None
            else self._research_memory.query_current_question_literature_revision(
                question_ref
            )
        )
        human_request = self._blocking_human_request(
            cast(str, source["quest_ref"]), cast(str, context["context_ref"])
        )
        return _AutonomousFacts(
            context=context,
            checkpoint_ref=checkpoint_ref,
            source=source,
            scope=scope,
            proposal=proposal,
            request=request,
            run=run,
            snapshot=snapshot,
            content=content,
            dispatch=dispatch,
            accepted_question=accepted,
            literature_revision=literature_revision,
            human_request=human_request,
        )

    def _query_scientific_decision(self, outcome_ref: str) -> object | None:
        query = getattr(
            self._research_graph,
            "query_reasoning_scientific_decision_by_outcome_ref",
            None,
        )
        if not callable(query):
            query = getattr(
                self._research_graph,
                "query_reasoning_scientific_decision_by_outcome",
                None,
            )
        if not callable(query):
            raise OwnerConflict("reasoning_scientific_decision_query_unavailable")
        return query(outcome_ref)

    def _blocking_human_request(
        self, quest_ref: str, context_ref: str
    ) -> dict[str, object] | None:
        waiter_ref = f"autonomous_deepfetch:{context_ref}"
        for request in self._agent_runtime.query_human_requests(
            quest_ref=quest_ref, include_history=False
        ):
            waiters = request.get("direct_waiters")
            if not isinstance(waiters, list):
                continue
            if any(
                isinstance(waiter, dict)
                and waiter.get("waiter_ref") == waiter_ref
                and waiter.get("status") != "released"
                for waiter in waiters
            ):
                return request
        return None

    def _assert_source_current(self, source: dict[str, object]) -> None:
        if not self._source_is_current(source):
            raise OwnerConflict("autonomous_creation_source_stale")

    def _source_is_current(self, source: dict[str, object]) -> bool:
        foreground = self._advancement_engine.query_foreground(
            cast(str, source["quest_ref"])
        )
        return bool(
            isinstance(foreground, dict)
            and foreground.get("cycle_ref") == source.get("cycle_ref")
            and foreground.get("stage") == "reasoning"
            and foreground.get("epoch") == source.get("foreground_epoch")
            and foreground.get("status") == "active"
        )


def _field(value: object, field: str) -> object:
    if isinstance(value, dict):
        return value.get(field)
    return getattr(value, field, None)


def _mapping_field(value: object, field: str) -> dict[str, object]:
    result = _field(value, field)
    if not isinstance(result, dict):
        raise OwnerConflict(f"autonomous_{field}_invalid")
    return result


def _optional_mapping(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise OwnerConflict("autonomous_projection_invalid")
    return value


def _require_ref(value: object, code: str) -> str:
    if not isinstance(value, str) or not value:
        raise OwnerConflict(code)
    return value


def _require_key(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise OwnerConflict("idempotency_key_invalid")
    return value


def _receipt(value: object, issuer: str) -> AcceptanceReceipt:
    if isinstance(value, AcceptanceReceipt):
        receipt = value
    elif isinstance(value, dict):
        try:
            receipt = AcceptanceReceipt(
                issuer=cast(str, value["issuer"]),
                kind=cast(str, value["kind"]),
                receipt_ref=cast(str, value["receipt_ref"]),
                subject_ref=cast(str, value["subject_ref"]),
                payload_hash=cast(str, value["payload_hash"]),
            )
        except (KeyError, TypeError) as error:
            raise OwnerConflict("autonomous_receipt_invalid") from error
    else:
        raise OwnerConflict("autonomous_receipt_invalid")
    if receipt.issuer != issuer:
        raise OwnerConflict("autonomous_receipt_invalid")
    return receipt


def _receipt_public(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    if isinstance(value, AcceptanceReceipt):
        return value.as_public_dict()
    if isinstance(value, dict):
        return dict(value)
    as_public = getattr(value, "as_public_dict", None)
    if callable(as_public):
        result = as_public()
        if isinstance(result, dict):
            return result
    raise OwnerConflict("autonomous_receipt_invalid")


def _authorization_receipt_ref(authorization: dict[str, object]) -> str:
    direct = authorization.get("receipt_ref")
    if isinstance(direct, str) and direct:
        return direct
    receipt = authorization.get("receipt")
    if isinstance(receipt, dict):
        return _require_ref(
            receipt.get("receipt_ref"), "broad_research_authorization_invalid"
        )
    raise OwnerConflict("broad_research_authorization_invalid")


def _public_object(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        result = dict(value)
    else:
        as_public = getattr(value, "as_public_dict", None)
        if callable(as_public):
            result = as_public()
        else:
            result = {
                key: item
                for key, item in vars(value).items()
                if not key.startswith("_")
            }
    for key, item in tuple(result.items()):
        public = getattr(item, "as_public_dict", None)
        if callable(public):
            result[key] = public()
    return result


def _public_component(value: object | None, field: str) -> dict[str, object] | None:
    component = _field(value, field)
    return None if component is None else _public_object(component)


def _accepted_question_ref(value: object | None) -> str | None:
    binding = _field(value, "accepted_question_binding")
    question_ref = _field(binding, "question_ref")
    return question_ref if isinstance(question_ref, str) else None


def _snapshot_binding(snapshot: object) -> object:
    as_context_binding = getattr(snapshot, "as_context_binding", None)
    if not callable(as_context_binding):
        raise OwnerConflict("autonomous_question_literature_snapshot_invalid")
    return as_context_binding()


def _deepfetch_status(request: object, run: object, snapshot: object) -> str:
    if snapshot is not None:
        return "succeeded"
    if run is not None:
        status = _field(run, "status")
        if status == "executed":
            return "accepting_snapshot"
        if status in {"failed", "cancelled"}:
            return cast(str, status)
        return "running"
    return "not_started" if request is None else "queued"


def _autonomous_status(facts: _AutonomousFacts) -> str:
    if facts.literature_revision is not None:
        return "ready_for_reasoning_resume"
    if facts.human_request is not None:
        return "waiting_human"
    if facts.accepted_question is not None:
        return "binding_literature_revision"
    if facts.dispatch is not None:
        return "dispatch_authorized"
    if facts.content is not None:
        return "content_accepted"
    if facts.proposal is not None:
        return "proposal_formed"
    if facts.snapshot is not None:
        return "literature_accepted"
    if facts.request is not None:
        return "deepfetch_running"
    return "prepared"


def _key(prefix: str, *values: str) -> str:
    return f"{prefix}:{canonical_hash(list(values))}"
