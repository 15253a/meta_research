from __future__ import annotations

from dataclasses import dataclass

from meta_research.autonomous_creation import AutonomousCreationService
from meta_research.owners.common import AcceptanceReceipt, canonical_hash


def _receipt(issuer: str, kind: str, subject_ref: str) -> AcceptanceReceipt:
    return AcceptanceReceipt(
        issuer=issuer,
        kind=kind,
        receipt_ref=f"{issuer}-receipt:{subject_ref}",
        subject_ref=subject_ref,
        payload_hash=canonical_hash({"issuer": issuer, "subject": subject_ref}),
    )


def _context(name: str) -> dict[str, object]:
    checkpoint_ref = f"reasoning-checkpoint:{name}"
    checkpoint_hash = canonical_hash({"checkpoint": checkpoint_ref})
    quest_ref = f"quest:{name}"
    source = {
        "quest_ref": quest_ref,
        "cycle_ref": f"cycle:{name}",
        "reasoning_stage_run_request_ref": f"reasoning-request:{name}",
        "scientific_outcome_ref": f"scientific-outcome:{name}",
        "question_ref": f"question:{name}",
        "foreground_epoch": 7,
        "reasoning_checkpoint_ref": checkpoint_ref,
        "reasoning_checkpoint_hash": checkpoint_hash,
        "autonomous_scope_content_acceptance_receipt_ref": f"rm:{name}",
        "preliminary_scientific_acceptance_receipt_ref": f"rg:{name}",
    }
    return {
        "context_ref": f"autonomous-context:{name}",
        "generation": 1,
        "checkpoint": {"ref": checkpoint_ref, "hash": checkpoint_hash},
        "source": source,
        "scientific_outcome": {
            "outcome_ref": source["scientific_outcome_ref"],
        },
        "scope": {"name": name},
        "scope_hash": canonical_hash({"name": name}),
        "broad_authorization": {
            "status": "granted",
            "receipt_ref": f"authorization:{name}",
        },
        "proposal": None,
        "selection": None,
        "receipt": _receipt(
            "human_collaboration",
            "autonomous_creation_context",
            f"autonomous-context:{name}",
        ).as_public_dict(),
    }


class _HumanCollaboration:
    def __init__(
        self, contexts: list[dict[str, object]], writes: list[str]
    ) -> None:
        self.contexts = contexts
        self.writes = writes

    def query_broad_research_authorization(
        self, quest_ref: str
    ) -> dict[str, object] | None:
        return {"status": "granted", "receipt_ref": f"authorization:{quest_ref}"}

    def prepare_autonomous_creation(self, **values: object) -> dict[str, object]:
        checkpoint_ref = str(values["reasoning_checkpoint_ref"])
        name = checkpoint_ref.split(":", 1)[1]
        context = _context(name)
        context["source"] = values["source"]
        context["scientific_outcome"] = values["scientific_outcome"]
        context["scope"] = values["autonomous_scope"]
        context["scope_hash"] = values["autonomous_scope_hash"]
        context["broad_authorization"] = values["broad_authorization"]
        self.contexts.append(context)
        self.writes.append(f"human_collaboration.prepare:{name}")
        return context

    def query_current_autonomous_creation(self) -> dict[str, object] | None:
        return None if not self.contexts else self.contexts[-1]

    def query_autonomous_creation(
        self, reasoning_checkpoint_ref: str
    ) -> dict[str, object] | None:
        return next(
            (
                context
                for context in self.contexts
                if context["checkpoint"]["ref"] == reasoning_checkpoint_ref
            ),
            None,
        )

    def query_autonomous_creation_context(
        self, context_ref: str
    ) -> dict[str, object] | None:
        return next(
            (
                context
                for context in self.contexts
                if context["context_ref"] == context_ref
            ),
            None,
        )

    def query_autonomous_creation_contexts(
        self,
    ) -> tuple[dict[str, object], ...]:
        return tuple(self.contexts)

    def form_autonomous_question_proposal(
        self,
        context_ref: str,
        *,
        literature_snapshot_ref: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        del literature_snapshot_ref, idempotency_key
        context = self.query_autonomous_creation_context(context_ref)
        assert context is not None
        context["proposal"] = {"ref": f"proposal:{context_ref}", "hash": "hash"}
        self.writes.append(f"human_collaboration.proposal:{context_ref}")
        return context

    def select_autonomous_question_content(self, *args: object, **kwargs: object):
        raise AssertionError("selection is outside this scheduler test")


class _AgentRuntime:
    def __init__(self, writes: list[str]) -> None:
        self.writes = writes
        self.blocked_quests: set[str] = set()
        self.reasoning_runs: dict[str, dict[str, object]] = {}
        self.checkpoints: dict[str, dict[str, object]] = {}

    def query_acquisition_session(self, **values: object) -> dict[str, object]:
        return {
            "status": "ready",
            "quest_ref": values["quest_ref"],
            "slot_held": False,
        }

    def query_deepfetch_run(self, request_ref: str) -> dict[str, object]:
        return {"status": "executed", "run_ref": f"run:{request_ref}"}

    def query_human_requests(self, **values: object) -> tuple[dict[str, object], ...]:
        quest_ref = str(values["quest_ref"])
        if quest_ref not in self.blocked_quests:
            return ()
        context_name = quest_ref.split(":", 1)[1]
        return (
            {
                "direct_waiters": [
                    {
                        "waiter_ref": (
                            "autonomous_deepfetch:"
                            f"autonomous-context:{context_name}"
                        ),
                        "status": "waiting",
                    }
                ]
            },
        )

    def query_reasoning_stage_run(self, request_ref: str) -> object | None:
        return self.reasoning_runs.get(request_ref)

    def query_reasoning_autonomous_checkpoint(
        self, checkpoint_ref: str
    ) -> object | None:
        return self.checkpoints.get(checkpoint_ref)


@dataclass(frozen=True)
class _Snapshot:
    snapshot_ref: str
    receipt: AcceptanceReceipt

    def as_context_binding(self) -> dict[str, object]:
        return {"snapshot_ref": self.snapshot_ref}


class _ResearchMemory:
    def __init__(self, advancement: "_AdvancementEngine") -> None:
        self.advancement = advancement
        self.terminal_questions: set[str] = set()
        self.candidates: dict[str, dict[str, object]] = {}

    def query_reasoning_scientific_candidate_by_checkpoint_ref(
        self, checkpoint_ref: str
    ) -> object | None:
        return self.candidates.get(checkpoint_ref)

    def query_literature_snapshot_for_request(
        self, request_ref: str
    ) -> object | None:
        if request_ref not in self.advancement.request_refs:
            return None
        return _Snapshot(
            f"snapshot:{request_ref}",
            _receipt("research_memory", "literature_snapshot", request_ref),
        )

    def query_autonomous_question_content_by_checkpoint_ref(
        self, checkpoint_ref: str
    ) -> object | None:
        return None

    def accept_autonomous_question_content(self, **values: object) -> object:
        raise AssertionError("content acceptance is outside this scheduler test")

    def ensure_question_literature_revision(self, **values: object) -> object:
        raise AssertionError("literature binding is outside this scheduler test")

    def query_current_question_literature_revision(
        self, question_ref: str
    ) -> dict[str, object] | None:
        if question_ref in self.terminal_questions:
            return {"revision_ref": f"literature-revision:{question_ref}"}
        return None


class _ResearchGraph:
    def __init__(self) -> None:
        self.decisions: dict[str, dict[str, object]] = {}
        self.accepted_questions: dict[str, dict[str, object]] = {}

    def query_reasoning_scientific_decision_by_outcome_ref(
        self, outcome_ref: str
    ) -> object | None:
        return self.decisions.get(outcome_ref)

    def query_autonomous_question_by_checkpoint_ref(
        self, checkpoint_ref: str
    ) -> object | None:
        return self.accepted_questions.get(checkpoint_ref)

    def accept_autonomous_question(self, **values: object) -> object:
        raise AssertionError("Question acceptance is outside this scheduler test")


class _AdvancementEngine:
    def __init__(self, writes: list[str]) -> None:
        self.writes = writes
        self.foregrounds: dict[str, dict[str, object]] = {}
        self.active: list[dict[str, object]] = []
        self.reasoning_requests: dict[str, dict[str, object]] = {}
        self.requests: dict[str, dict[str, object]] = {}
        self.request_refs: set[str] = set()

    def query_foreground(self, quest_ref: str) -> dict[str, object] | None:
        return self.foregrounds.get(quest_ref)

    def query_active_foregrounds(
        self, *, stage: str | None = None
    ) -> tuple[dict[str, object], ...]:
        assert stage == "reasoning"
        return tuple(self.active)

    def query_reasoning_stage_request(self, cycle_ref: str) -> object | None:
        return self.reasoning_requests.get(cycle_ref)

    def issue_autonomous_deepfetch_request(self, **values: object) -> object:
        context = values["context"]
        assert isinstance(context, dict)
        context_ref = str(context["context_ref"])
        request_ref = f"deepfetch-request:{context_ref}"
        request = {
            "request_ref": request_ref,
            "authorization_receipt": _receipt(
                "advancement_engine", "deepfetch_request", request_ref
            ),
        }
        self.requests[context_ref] = request
        self.request_refs.add(request_ref)
        self.writes.append(f"advancement_engine.deepfetch:{context_ref}")
        return request

    def query_autonomous_deepfetch_request(
        self, context_ref: str
    ) -> object | None:
        return self.requests.get(context_ref)

    def record_autonomous_deepfetch_succeeded(self, **values: object) -> None:
        raise AssertionError("outside scheduler test")

    def record_autonomous_deepfetch_failed(self, **values: object) -> None:
        raise AssertionError("outside scheduler test")

    def authorize_autonomous_question_dispatch(self, **values: object) -> object:
        raise AssertionError("outside scheduler test")

    def query_autonomous_question_dispatch(
        self, context_ref: str
    ) -> object | None:
        return None


def _service(
    names: list[str],
) -> tuple[
    AutonomousCreationService,
    _HumanCollaboration,
    _AdvancementEngine,
    _AgentRuntime,
    _ResearchMemory,
    _ResearchGraph,
    list[str],
]:
    writes: list[str] = []
    contexts = [_context(name) for name in names]
    human = _HumanCollaboration(contexts, writes)
    advancement = _AdvancementEngine(writes)
    agent = _AgentRuntime(writes)
    memory = _ResearchMemory(advancement)
    graph = _ResearchGraph()
    for context in contexts:
        source = context["source"]
        assert isinstance(source, dict)
        advancement.foregrounds[str(source["quest_ref"])] = {
            "quest_ref": source["quest_ref"],
            "cycle_ref": source["cycle_ref"],
            "stage": "reasoning",
            "epoch": source["foreground_epoch"],
            "status": "active",
        }
    return (
        AutonomousCreationService(
            human, advancement, agent, memory, graph, object()  # type: ignore[arg-type]
        ),
        human,
        advancement,
        agent,
        memory,
        graph,
        writes,
    )


def test_restart_skips_terminal_waiting_human_and_stale_contexts() -> None:
    (
        _service_before_restart,
        human,
        advancement,
        agent,
        memory,
        graph,
        writes,
    ) = _service(["terminal", "waiting", "stale", "pending"])
    terminal = human.contexts[0]
    terminal_checkpoint = terminal["checkpoint"]
    assert isinstance(terminal_checkpoint, dict)
    terminal_question = "question:terminal-created"
    graph.accepted_questions[str(terminal_checkpoint["ref"])] = {
        "accepted_question_binding": {"question_ref": terminal_question},
    }
    memory.terminal_questions.add(terminal_question)
    agent.blocked_quests.add("quest:waiting")
    advancement.foregrounds["quest:stale"] = {
        "quest_ref": "quest:stale",
        "cycle_ref": "cycle:replacement",
        "stage": "reasoning",
        "epoch": 8,
        "status": "active",
    }

    restarted = AutonomousCreationService(
        human,
        advancement,
        agent,
        memory,
        graph,
        object(),  # type: ignore[arg-type]
    )
    assert restarted.query_current()["context_ref"] == (
        "autonomous-context:pending"
    )
    assert restarted.process_once()
    assert writes == [
        "advancement_engine.deepfetch:autonomous-context:pending"
    ]


def test_scheduler_round_robins_two_actionable_autonomous_contexts() -> None:
    service, _human, _advancement, _agent, _memory, _graph, writes = _service(
        ["first", "second"]
    )

    assert service.process_once()
    assert writes == [
        "advancement_engine.deepfetch:autonomous-context:first"
    ]
    assert service.process_once()
    assert writes[-1] == (
        "advancement_engine.deepfetch:autonomous-context:second"
    )
    assert not any("proposal:autonomous-context:first" in item for item in writes)


def test_daemon_discovers_each_active_checkpoint_without_global_latest() -> None:
    service, human, advancement, agent, memory, graph, writes = _service([])
    for name in ("not-yet-accepted", "accepted"):
        cycle_ref = f"cycle:{name}"
        request_ref = f"reasoning-request:{name}"
        checkpoint_ref = f"reasoning-checkpoint:{name}"
        outcome_ref = f"scientific-outcome:{name}"
        advancement.active.append(
            {
                "quest_ref": f"quest:{name}",
                "cycle_ref": cycle_ref,
                "stage": "reasoning",
                "epoch": 7,
                "status": "active",
            }
        )
        advancement.foregrounds[f"quest:{name}"] = advancement.active[-1]
        advancement.reasoning_requests[cycle_ref] = {"request_ref": request_ref}
        agent.reasoning_runs[request_ref] = {
            "autonomous_checkpoint": {"checkpoint_ref": checkpoint_ref}
        }
        agent.checkpoints[checkpoint_ref] = {"checkpoint_ref": checkpoint_ref}
        outcome = {
            "outcome_ref": outcome_ref,
            "quest_ref": f"quest:{name}",
            "cycle_ref": cycle_ref,
            "stage_run_request_ref": request_ref,
            "question_ref": f"question:{name}",
            "foreground_epoch": 7,
        }
        scope = {"name": name}
        memory.candidates[checkpoint_ref] = {
            "scientific_outcome": outcome,
            "autonomous_scope": scope,
            "autonomous_scope_hash": canonical_hash(scope),
            "checkpoint_hash": canonical_hash({"checkpoint": checkpoint_ref}),
            "receipt": _receipt(
                "research_memory", "reasoning_scientific_candidate", outcome_ref
            ),
        }
        if name == "accepted":
            graph.decisions[outcome_ref] = {
                "decision": "accepted",
                "scientific_outcome_ref": outcome_ref,
                "receipt": _receipt(
                    "research_graph", "reasoning_scientific_decision", outcome_ref
                ),
            }

    assert service.process_once()
    assert writes == ["human_collaboration.prepare:accepted"]
    assert human.query_autonomous_creation(
        "reasoning-checkpoint:not-yet-accepted"
    ) is None
    assert human.query_autonomous_creation("reasoning-checkpoint:accepted") is not None
