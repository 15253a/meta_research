from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Callable

from sqlalchemy import text

from meta_research.owners.common import (
    OwnerConflict,
    canonical_hash,
    canonical_json,
    decoded_object,
    new_ref,
)
from meta_research.quest_goal import (
    GOAL_DIRECTION_SCHEMA_REF,
    GOAL_REVISION_RECEIPT_KIND,
    EvolutionDecision,
    ResearchRoot,
    WorkHandle,
    parse_evolution_decision,
    parse_research_root,
    work_basis,
)
from meta_research.runtime_conditions import (
    read_runtime_conditions,
    read_runtime_conditions_in_transaction,
)


_RG_OWNER = "research_graph"
COMPLETION_CONDITION_SOURCE_FIELDS = {
    "runtime_conditions_revision", "conditions_snapshot_ref", "conditions_snapshot_hash",
}


def completion_condition_source(connection, reasoning) -> dict[str, str]:
    row = connection.execute(text(
        "SELECT * FROM hc_reasoning_completion_conditions WHERE submission_ref=:ref"
    ), {"ref": reasoning.submission_ref}).first()
    if row is None:
        return {}
    binding = decoded_object(row.binding_json)
    scope = binding.get("scope")
    guidance = binding.get("guidance_binding")
    if (canonical_hash(binding) != row.binding_hash
            or not isinstance(scope, dict) or not isinstance(guidance, dict)
            or scope.get("root_kind") != "reasoning"
            or scope.get("run_ref") != reasoning.run_ref
            or scope.get("attempt_ref") != reasoning.attempt_ref
            or scope.get("fence_ref") != reasoning.fence_ref
            or binding.get("payload_hash") != reasoning.payload_hash):
        raise OwnerConflict("candidate_completion_conditions_invalid")
    snapshot = connection.execute(text(
        "SELECT * FROM hc_guidance_snapshots WHERE snapshot_ref=:ref"
    ), {"ref": guidance.get("snapshot_ref")}).first()
    if snapshot is None:
        raise OwnerConflict("candidate_completion_conditions_invalid")
    payload = decoded_object(snapshot.snapshot_json)
    direction = payload.get("direction_cut")
    candidate = decoded_object(reasoning.transition_json)
    if (canonical_hash(payload) != snapshot.snapshot_hash
            or guidance != {
                "identity": payload.get("identity"),
                "quest_ref": payload.get("quest_ref"),
                "snapshot_ref": snapshot.snapshot_ref,
                "snapshot_hash": snapshot.snapshot_hash,
            }
            or not isinstance(direction, dict)
            or payload.get("quest_ref") != candidate.get("current_quest_ref")
            or direction.get("conditions") != binding.get("conditions")):
        raise OwnerConflict("candidate_completion_conditions_invalid")
    conditions = binding["conditions"]
    runtime = conditions["runtime_conditions"]
    frozen = read_runtime_conditions_in_transaction(
        connection, str(guidance["quest_ref"]), revision=str(runtime["revision"])
    )
    if frozen != runtime:
        raise OwnerConflict("candidate_completion_conditions_invalid")
    return {
        "runtime_conditions_revision": frozen["revision"],
        "conditions_snapshot_ref": snapshot.snapshot_ref,
        "conditions_snapshot_hash": snapshot.snapshot_hash,
    }


def initial_goal_revision_ref(row) -> str:
    return "quest_goal_revision_" + canonical_hash(
        {
            "quest_ref": row.quest_ref,
            "draft_revision": int(row.draft_revision),
            "draft_hash": row.draft_hash,
        }
    )[:32]


def initial_goal_binding(row) -> dict[str, object]:
    try:
        goal = decoded_object(row.goal_json)
    except (TypeError, ValueError) as error:
        raise OwnerConflict("quest_goal_revision_invalid") from error
    if not isinstance(goal, dict) or canonical_hash(goal) != row.draft_hash:
        raise OwnerConflict("quest_goal_revision_invalid")
    identity = {
        "quest_ref": row.quest_ref,
        "draft_revision": int(row.draft_revision),
        "draft_hash": row.draft_hash,
    }
    return {
        "kind": "QuestGoalRevision",
        "goal_revision_ref": initial_goal_revision_ref(row),
        **identity,
        "goal": goal,
        "rg_quest_acceptance_receipt_ref": row.receipt_ref,
    }


def seed_goal_head(connection, *, quest_ref: str, revision_ref: str) -> None:
    connection.execute(
        text(
            "INSERT INTO rg_quest_goal_heads "
            "(quest_ref,current_revision_ref,current_sequence,status,updated_at) "
            "VALUES (:quest_ref,:revision_ref,0,'open',:now)"
        ),
        {"quest_ref": quest_ref, "revision_ref": revision_ref, "now": time.time()},
    )


def query_goal_revision_in_transaction(
    connection, *, quest_ref: str, revision_ref: str | None = None
) -> dict[str, object] | None:
    quest = connection.execute(
        text("SELECT * FROM rg_quests WHERE quest_ref=:quest_ref"),
        {"quest_ref": quest_ref},
    ).first()
    if quest is None:
        return None
    initial = initial_goal_binding(quest)
    if revision_ref is None:
        head = connection.execute(
            text("SELECT * FROM rg_quest_goal_heads WHERE quest_ref=:quest_ref"),
            {"quest_ref": quest_ref},
        ).first()
        if head is None:
            raise OwnerConflict("quest_goal_head_missing")
        revision_ref = head.current_revision_ref
    if revision_ref == initial["goal_revision_ref"]:
        return initial
    row = connection.execute(
        text(
            "SELECT * FROM rg_quest_goal_revisions WHERE quest_ref=:quest_ref "
            "AND revision_ref=:revision_ref"
        ),
        {"quest_ref": quest_ref, "revision_ref": revision_ref},
    ).first()
    if row is None:
        return None
    return _evolved_goal_binding(row, quest)


def query_goal_revision_by_ref_in_transaction(
    connection, revision_ref: str
) -> dict[str, object] | None:
    row = connection.execute(
        text(
            "SELECT quest_ref FROM rg_quest_goal_revisions WHERE "
            "revision_ref=:revision_ref"
        ),
        {"revision_ref": revision_ref},
    ).first()
    if row is not None:
        return query_goal_revision_in_transaction(
            connection, quest_ref=row.quest_ref, revision_ref=revision_ref
        )
    quests = connection.execute(text("SELECT * FROM rg_quests")).all()
    for quest in quests:
        if initial_goal_revision_ref(quest) == revision_ref:
            return initial_goal_binding(quest)
    return None


def _evolved_goal_binding(row, quest) -> dict[str, object]:
    try:
        decision = decoded_object(row.decision_json)
        author = decoded_object(row.author_json)
        cause = decoded_object(row.cause_json)
        receipt = decoded_object(row.receipt_json)
    except (TypeError, ValueError) as error:
        raise OwnerConflict("quest_goal_revision_invalid") from error
    if (
        not isinstance(decision, dict)
        or not isinstance(author, dict)
        or not isinstance(cause, dict)
        or not isinstance(receipt, dict)
        or canonical_hash(decision) != row.decision_hash
        or canonical_hash(author) != row.author_hash
        or canonical_hash(cause) != row.cause_hash
        or canonical_hash(receipt) != row.receipt_hash
        or decision.get("expected_revision") != row.parent_ref
        or decision.get("cause") != cause
    ):
        raise OwnerConflict("quest_goal_revision_invalid")
    replacement = decision.get("replacement")
    if not isinstance(replacement, dict):
        raise OwnerConflict("quest_goal_revision_invalid")
    binding = {
        "kind": "QuestGoalRevision",
        "goal_revision_ref": row.revision_ref,
        "quest_ref": row.quest_ref,
        "draft_revision": int(quest.draft_revision),
        "draft_hash": quest.draft_hash,
        "goal": replacement,
        "rg_quest_acceptance_receipt_ref": quest.receipt_ref,
        "origin": "evolution",
        "sequence": int(row.sequence),
        "parent_ref": row.parent_ref,
        "criteria_review": decision.get("criteria_review"),
        "judgment": decision.get("judgment"),
        "following_direction": decision.get("following_direction"),
        "author": author,
        "cause": cause,
        "receipt": receipt,
    }
    expected_receipt = _goal_receipt(
        receipt_ref=row.receipt_ref,
        revision_ref=row.revision_ref,
        quest_ref=row.quest_ref,
        parent_ref=row.parent_ref,
        sequence=int(row.sequence),
        decision_hash=row.decision_hash,
        author_hash=row.author_hash,
        cause_hash=row.cause_hash,
    )
    if receipt != expected_receipt:
        raise OwnerConflict("quest_goal_revision_invalid")
    return binding


def _goal_receipt(
    *,
    receipt_ref: str,
    revision_ref: str,
    quest_ref: str,
    parent_ref: str,
    sequence: int,
    decision_hash: str,
    author_hash: str,
    cause_hash: str,
) -> dict[str, object]:
    payload = {
        "quest_ref": quest_ref,
        "parent_ref": parent_ref,
        "sequence": sequence,
        "decision_hash": decision_hash,
        "author_hash": author_hash,
        "cause_hash": cause_hash,
    }
    return {
        "status": "accepted",
        "issuer": _RG_OWNER,
        "kind": GOAL_REVISION_RECEIPT_KIND,
        "receipt_ref": receipt_ref,
        "subject_ref": revision_ref,
        "payload_hash": canonical_hash(payload),
    }


def verify_goal_revision_ancestry_in_transaction(
    connection, binding: dict[str, object]
) -> None:
    """Verify the immutable revision chain behind one public goal binding."""

    quest_ref = binding.get("quest_ref")
    revision_ref = binding.get("goal_revision_ref")
    quest = connection.execute(
        text("SELECT * FROM rg_quests WHERE quest_ref=:quest_ref"),
        {"quest_ref": quest_ref},
    ).first()
    if quest is None:
        raise OwnerConflict("quest_goal_revision_invalid")
    initial_ref = initial_goal_revision_ref(quest)
    if revision_ref == initial_ref:
        if binding != initial_goal_binding(quest):
            raise OwnerConflict("quest_goal_revision_invalid")
        return
    row = connection.execute(
        text(
            "SELECT * FROM rg_quest_goal_revisions WHERE quest_ref=:quest_ref "
            "AND revision_ref=:revision_ref"
        ),
        {"quest_ref": quest_ref, "revision_ref": revision_ref},
    ).first()
    if row is None or int(row.sequence) < 1:
        raise OwnerConflict("quest_goal_revision_invalid")
    try:
        decision = parse_evolution_decision(decoded_object(row.decision_json))
        author = parse_research_root(decoded_object(row.author_json))
    except (TypeError, ValueError, OwnerConflict) as error:
        raise OwnerConflict("quest_goal_revision_invalid") from error
    if int(row.sequence) == 1:
        expected_parent = initial_ref
    else:
        parent = connection.execute(
            text(
                "SELECT revision_ref FROM rg_quest_goal_revisions WHERE "
                "quest_ref=:quest_ref AND sequence=:sequence"
            ),
            {"quest_ref": quest_ref, "sequence": int(row.sequence) - 1},
        ).first()
        expected_parent = None if parent is None else parent.revision_ref
    expected_ref = "quest_goal_revision_" + canonical_hash(
        {
            "quest_ref": quest_ref,
            "parent_ref": row.parent_ref,
            "sequence": int(row.sequence),
            "decision_hash": canonical_hash(decision.as_dict()),
            "author_hash": canonical_hash(author.as_dict()),
        }
    )[:48]
    if (
        expected_parent is None
        or row.parent_ref != expected_parent
        or decision.expected_revision != expected_parent
        or row.revision_ref != expected_ref
    ):
        raise OwnerConflict("quest_goal_revision_invalid")
class QuestGoalOwnerMixin:
    def query_runtime_conditions_revision(self, quest_ref: str) -> str:
        with self._database.read() as connection:
            return read_runtime_conditions_in_transaction(connection, quest_ref)["revision"]

    def query_current_quest_goal_revision(
        self, quest_ref: str
    ) -> dict[str, object] | None:
        _required_ref(quest_ref, "quest_goal_revision_invalid")
        with self._database.read() as connection:
            binding = query_goal_revision_in_transaction(
                connection, quest_ref=quest_ref
            )
        if binding is not None:
            self.verify_quest_goal_revision(binding)
        return binding

    def query_quest_goal_revision(
        self, revision_ref: str
    ) -> dict[str, object] | None:
        _required_ref(revision_ref, "quest_goal_revision_invalid")
        with self._database.read() as connection:
            binding = query_goal_revision_by_ref_in_transaction(
                connection, revision_ref
            )
        if binding is not None:
            self.verify_quest_goal_revision(binding)
        return binding

    def query_quest_goal_revision_detail(
        self, revision_ref: str
    ) -> dict[str, object] | None:
        binding = self.query_quest_goal_revision(revision_ref)
        if binding is None:
            return None
        with self._database.read() as connection:
            return self._quest_goal_revision_detail_in_transaction(
                connection, binding
            )

    def verify_quest_goal_revision(self, binding: dict[str, object]) -> None:
        if not isinstance(binding, dict) or binding.get("kind") != "QuestGoalRevision":
            raise OwnerConflict("quest_goal_revision_invalid")
        quest_ref = binding.get("quest_ref")
        revision_ref = binding.get("goal_revision_ref")
        _required_ref(quest_ref, "quest_goal_revision_invalid")
        _required_ref(revision_ref, "quest_goal_revision_invalid")
        with self._database.read() as connection:
            stored = query_goal_revision_in_transaction(
                connection, quest_ref=quest_ref, revision_ref=revision_ref
            )
            if stored == binding:
                verify_goal_revision_ancestry_in_transaction(connection, binding)
        if stored != binding:
            raise OwnerConflict("quest_goal_revision_invalid")
        accepted = self.query_quest_by_ref(quest_ref)
        if accepted is None:
            raise OwnerConflict("quest_goal_revision_invalid")

    def assert_current_quest_goal_revision(
        self, binding: dict[str, object], *, connection=None
    ) -> None:
        if not isinstance(binding, dict):
            raise OwnerConflict("quest_goal_revision_invalid")
        quest_ref = binding.get("quest_ref")
        revision_ref = binding.get("goal_revision_ref")
        _required_ref(quest_ref, "quest_goal_revision_invalid")
        _required_ref(revision_ref, "quest_goal_revision_invalid")

        def verify(current_connection) -> None:
            stored = query_goal_revision_in_transaction(
                current_connection, quest_ref=quest_ref, revision_ref=revision_ref
            )
            head = current_connection.execute(
                text(
                    "SELECT current_revision_ref,status FROM rg_quest_goal_heads "
                    "WHERE quest_ref=:quest_ref"
                ),
                {"quest_ref": quest_ref},
            ).first()
            if (
                stored != binding
                or head is None
                or head.current_revision_ref != revision_ref
                or head.status != "open"
            ):
                raise OwnerConflict("quest_completion_goal_stale")

        if connection is not None:
            verify(connection)
        else:
            with self._database.read() as current_connection:
                verify(current_connection)

    def freeze_quest_direction(
        self, *, connection, quest_ref: str, author: ResearchRoot
    ) -> dict[str, object]:
        current = query_goal_revision_in_transaction(
            connection, quest_ref=quest_ref
        )
        if current is None:
            raise OwnerConflict("quest_goal_revision_not_found")
        conditions = self._quest_goal_conditions_in_transaction(
            connection, quest_ref=quest_ref,
            revision_ref=str(current["goal_revision_ref"]),
        )
        work = work_basis(
            self._quest_goal_work_in_transaction(
                connection, quest_ref=quest_ref, author=author
            )
        )
        return {
            "schema_ref": GOAL_DIRECTION_SCHEMA_REF,
            "quest_ref": quest_ref,
            "goal": current,
            "conditions": conditions,
            "work": work,
        }

    def evolve_quest_goal(
        self,
        *,
        author: ResearchRoot,
        quest_ref: str,
        direction_cut: dict[str, object],
        effect_id: str,
        decision: EvolutionDecision,
        reconcile: bool = False,
    ) -> dict[str, object]:
        _required_ref(effect_id, "goal_evolution_effect_invalid", maximum=128)
        if (
            not isinstance(direction_cut, dict)
            or direction_cut.get("schema_ref") != GOAL_DIRECTION_SCHEMA_REF
            or direction_cut.get("quest_ref") != quest_ref
        ):
            raise OwnerConflict("goal_direction_cut_invalid")
        command = {
            "quest_ref": quest_ref,
            "author": author.as_dict(),
            "direction_cut": direction_cut,
            "decision": decision.as_dict(),
        }
        effect_key = "goal_evolution_effect_" + canonical_hash(
            {
                "root_kind": author.kind,
                "run_ref": author.run_ref,
                "operation_ref": author.operation_ref,
                "effect_id": effect_id,
            }
        )[:64]
        with self._database.fenced_write() as connection:
            replay = _goal_effect_replay(connection, effect_key, command)
            if replay is not None:
                return {**replay, "replayed": True}
            if reconcile:
                raise OwnerConflict("goal_evolution_effect_not_found")
            head = connection.execute(
                text(
                    "SELECT * FROM rg_quest_goal_heads WHERE quest_ref=:quest_ref"
                ),
                {"quest_ref": quest_ref},
            ).first()
            if head is None or head.status != "open":
                raise OwnerConflict("quest_goal_closed")
            current = query_goal_revision_in_transaction(
                connection, quest_ref=quest_ref
            )
            current_conditions = self._quest_goal_conditions_in_transaction(
                connection,
                quest_ref=quest_ref,
                revision_ref=head.current_revision_ref,
            )
            current_work = work_basis(
                self._quest_goal_work_in_transaction(
                    connection, quest_ref=quest_ref, author=author
                )
            )
            if (
                current is None
                or direction_cut.get("goal") != current
                or direction_cut.get("conditions") != current_conditions
                or direction_cut.get("work") != current_work
                or decision.expected_revision != head.current_revision_ref
                or decision.conditions_basis != current_conditions["basis_ref"]
                or decision.work_basis != current_work["basis_ref"]
                or decision.conditions.get("runtime_conditions_ref")
                != current_conditions["runtime_conditions"]["revision"]
            ):
                raise OwnerConflict("goal_evolution_basis_stale")
            decisions = {item.work.work_ref: item for item in decision.arrangements}
            handles = {item.work_ref: item for item in self._quest_goal_work_in_transaction(
                connection, quest_ref=quest_ref, author=author
            )}
            if set(decisions) != set(handles) or any(
                item.work != handles[work_ref]
                for work_ref, item in decisions.items()
            ):
                raise OwnerConflict("goal_work_coverage_invalid")
            _validate_arrangement_kinds(tuple(decisions.values()))
            cause = self._verify_goal_cause_in_transaction(
                connection,
                quest_ref=quest_ref,
                author=author,
                direction_cut=direction_cut,
                cause=decision.cause,
            )
            conditions = self._verify_goal_conditions_in_transaction(
                connection,
                quest_ref=quest_ref,
                parent_ref=head.current_revision_ref,
                cause=cause,
                conditions=decision.conditions,
            )
            self._verify_goal_retention_in_transaction(
                connection,
                quest_ref=quest_ref,
                decisions=tuple(decisions.values()),
            )
            sequence = int(head.current_sequence) + 1
            decision_document = decision.as_dict()
            decision_hash = canonical_hash(decision_document)
            author_document = author.as_dict()
            author_hash = canonical_hash(author_document)
            cause_hash = canonical_hash(cause)
            revision_ref = "quest_goal_revision_" + canonical_hash(
                {
                    "quest_ref": quest_ref,
                    "parent_ref": head.current_revision_ref,
                    "sequence": sequence,
                    "decision_hash": decision_hash,
                    "author_hash": author_hash,
                }
            )[:48]
            receipt_ref = new_ref("rg_goal_receipt")
            receipt = _goal_receipt(
                receipt_ref=receipt_ref,
                revision_ref=revision_ref,
                quest_ref=quest_ref,
                parent_ref=head.current_revision_ref,
                sequence=sequence,
                decision_hash=decision_hash,
                author_hash=author_hash,
                cause_hash=cause_hash,
            )
            now = time.time()
            connection.execute(
                text(
                    "INSERT INTO rg_quest_goal_revisions "
                    "(revision_ref,quest_ref,parent_ref,sequence,decision_json,"
                    "decision_hash,author_json,author_hash,cause_json,cause_hash,"
                    "receipt_ref,receipt_json,receipt_hash,accepted_at) VALUES "
                    "(:revision_ref,:quest_ref,:parent_ref,:sequence,:decision_json,"
                    ":decision_hash,:author_json,:author_hash,:cause_json,:cause_hash,"
                    ":receipt_ref,:receipt_json,:receipt_hash,:accepted_at)"
                ),
                {
                    "revision_ref": revision_ref,
                    "quest_ref": quest_ref,
                    "parent_ref": head.current_revision_ref,
                    "sequence": sequence,
                    "decision_json": canonical_json(decision_document),
                    "decision_hash": decision_hash,
                    "author_json": canonical_json(author_document),
                    "author_hash": author_hash,
                    "cause_json": canonical_json(cause),
                    "cause_hash": cause_hash,
                    "receipt_ref": receipt_ref,
                    "receipt_json": canonical_json(receipt),
                    "receipt_hash": canonical_hash(receipt),
                    "accepted_at": now,
                },
            )
            for condition in conditions:
                connection.execute(
                    text(
                        "INSERT INTO rg_goal_revision_conditions "
                        "(revision_ref,condition_ref,source_json,source_hash,"
                        "source_text,meaning,status,superseding_source_json) "
                        "VALUES (:revision_ref,:condition_ref,:source_json,"
                        ":source_hash,:source_text,:meaning,:status,"
                        ":superseding_source_json)"
                    ),
                    {"revision_ref": revision_ref, **condition},
                )
            for item in decision.arrangements:
                item_document = item.as_dict()
                intent_ref = "goal_work_intent_" + canonical_hash(
                    {"revision_ref": revision_ref, "work_ref": item.work.work_ref}
                )[:48]
                connection.execute(
                    text(
                        "INSERT INTO rg_goal_work_intents "
                        "(intent_ref,revision_ref,work_ref,decision_kind,work_json,"
                        "work_hash,decision_json,decision_hash,created_at) VALUES "
                        "(:intent_ref,:revision_ref,:work_ref,:decision_kind,"
                        ":work_json,:work_hash,:decision_json,:decision_hash,:created_at)"
                    ),
                    {
                        "intent_ref": intent_ref,
                        "revision_ref": revision_ref,
                        "work_ref": item.work.work_ref,
                        "decision_kind": item.kind,
                        "work_json": canonical_json(item.work.as_dict()),
                        "work_hash": canonical_hash(item.work.as_dict()),
                        "decision_json": canonical_json(item_document),
                        "decision_hash": canonical_hash(item_document),
                        "created_at": now,
                    },
                )
            updated = connection.execute(
                text(
                    "UPDATE rg_quest_goal_heads SET current_revision_ref=:revision_ref,"
                    "current_sequence=:sequence,updated_at=:now WHERE quest_ref=:quest_ref "
                    "AND current_revision_ref=:parent_ref AND current_sequence=:prior "
                    "AND status='open'"
                ),
                {
                    "revision_ref": revision_ref,
                    "sequence": sequence,
                    "now": now,
                    "quest_ref": quest_ref,
                    "parent_ref": head.current_revision_ref,
                    "prior": int(head.current_sequence),
                },
            )
            if updated.rowcount != 1:
                raise OwnerConflict("goal_evolution_basis_stale")
            goal = query_goal_revision_in_transaction(
                connection, quest_ref=quest_ref, revision_ref=revision_ref
            )
            if goal is None:
                raise OwnerConflict("quest_goal_revision_invalid")
            result = {
                "status": "accepted",
                "goal": goal,
                "work": {
                    "basis_ref": current_work["basis_ref"],
                    "intents": [
                        {
                            "intent_ref": "goal_work_intent_"
                            + canonical_hash(
                                {
                                    "revision_ref": revision_ref,
                                    "work_ref": item.work.work_ref,
                                }
                            )[:48],
                            "decision": item.as_dict(),
                            "actual_status": "pending"
                            if item.kind in {"stop", "do_not_start"}
                            else "observed",
                        }
                        for item in decision.arrangements
                    ],
                },
                "replayed": False,
            }
            connection.execute(
                text(
                    "INSERT INTO rg_goal_evolution_effects "
                    "(effect_key,root_kind,run_ref,operation_ref,effect_id,"
                    "command_json,command_hash,revision_ref,receipt_json,receipt_hash,"
                    "created_at) VALUES (:effect_key,:root_kind,:run_ref,"
                    ":operation_ref,:effect_id,:command_json,:command_hash,"
                    ":revision_ref,:receipt_json,:receipt_hash,:created_at)"
                ),
                {
                    "effect_key": effect_key,
                    "root_kind": author.kind,
                    "run_ref": author.run_ref,
                    "operation_ref": author.operation_ref,
                    "effect_id": effect_id,
                    "command_json": canonical_json(command),
                    "command_hash": canonical_hash(command),
                    "revision_ref": revision_ref,
                    "receipt_json": canonical_json(result),
                    "receipt_hash": canonical_hash(result),
                    "created_at": now,
                },
            )
            connection.execute(
                text(
                    "UPDATE research_graph_state SET revision=revision+1 "
                    "WHERE singleton='owner'"
                )
            )
            self._feed.record(
                connection,
                "research_graph.quest_goal_evolved",
                {
                    "quest_ref": quest_ref,
                    "revision_ref": revision_ref,
                    "sequence": sequence,
                    "cause_kind": cause["kind"],
                },
            )
            return result

    def query_quest_goal_history(
        self, quest_ref: str, *, offset: int = 0, limit: int = 50
    ) -> dict[str, object]:
        _required_ref(quest_ref, "quest_goal_history_invalid")
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise OwnerConflict("quest_goal_history_invalid")
        with self._database.read() as connection:
            quest = connection.execute(
                text("SELECT * FROM rg_quests WHERE quest_ref=:quest_ref"),
                {"quest_ref": quest_ref},
            ).first()
            if quest is None:
                return {"items": [], "offset": offset, "next_offset": None}
            items: list[dict[str, object]] = []
            if offset == 0:
                items.append(
                    self._quest_goal_revision_detail_in_transaction(
                        connection, initial_goal_binding(quest)
                    )
                )
                remaining = limit - 1
                evolved_offset = 0
            else:
                remaining = limit
                evolved_offset = offset - 1
            evolved = connection.execute(
                text(
                    "SELECT revision_ref FROM rg_quest_goal_revisions WHERE "
                    "quest_ref=:quest_ref ORDER BY sequence LIMIT :limit OFFSET :offset"
                ),
                {
                    "quest_ref": quest_ref,
                    "limit": remaining + 1,
                    "offset": evolved_offset,
                },
            ).all()
            for row in evolved[:remaining]:
                value = query_goal_revision_in_transaction(
                    connection, quest_ref=quest_ref, revision_ref=row.revision_ref
                )
                if value is None:
                    raise OwnerConflict("quest_goal_revision_invalid")
                verify_goal_revision_ancestry_in_transaction(connection, value)
                items.append(
                    self._quest_goal_revision_detail_in_transaction(
                        connection, value
                    )
                )
            has_more = len(evolved) > remaining
        return {
            "items": items,
            "offset": offset,
            "next_offset": offset + len(items) if has_more else None,
        }

    def _quest_goal_revision_detail_in_transaction(
        self, connection, binding: dict[str, object]
    ) -> dict[str, object]:
        if binding.get("origin") != "evolution":
            return {**binding, "conditions": None, "conditions_review": None}
        row = connection.execute(
            text(
                "SELECT decision_json FROM rg_quest_goal_revisions "
                "WHERE revision_ref=:revision_ref"
            ),
            {"revision_ref": binding["goal_revision_ref"]},
        ).one()
        review = parse_evolution_decision(
            decoded_object(row.decision_json)
        ).conditions
        runtime = read_runtime_conditions_in_transaction(
            connection,
            str(binding["quest_ref"]),
            revision=str(review["runtime_conditions_ref"]),
        )
        rows = connection.execute(
            text(
                "SELECT * FROM rg_goal_revision_conditions WHERE "
                "revision_ref=:revision_ref ORDER BY condition_ref"
            ),
            {"revision_ref": binding["goal_revision_ref"]},
        ).all()
        return {
            **binding,
            "conditions": {
                "runtime_conditions": runtime,
                "enduring": [_condition_public(condition) for condition in rows],
            },
            "conditions_review": review,
        }

    def query_quest_goal_view(
        self,
        quest_ref: str,
        *,
        direction_cut: dict[str, object] | None = None,
        history_offset: int = 0,
        history_limit: int = 20,
    ) -> dict[str, object]:
        read_runtime_conditions(Path(self._database.path).parent, quest_ref)
        current = self.query_current_quest_goal_revision(quest_ref)
        if current is None:
            raise OwnerConflict("quest_goal_revision_not_found")
        with self._database.read() as connection:
            conditions = self._quest_goal_conditions_in_transaction(
                connection,
                quest_ref=quest_ref,
                revision_ref=str(current["goal_revision_ref"]),
            )
            intent_rows = connection.execute(
                text(
                    "SELECT i.* FROM rg_goal_work_intents i JOIN "
                    "rg_quest_goal_revisions r ON r.revision_ref=i.revision_ref "
                    "WHERE r.quest_ref=:quest_ref ORDER BY r.sequence,i.work_ref"
                ),
                {"quest_ref": quest_ref},
            ).all()
            intents = [self._quest_goal_work_intent_view(connection, row) for row in intent_rows]
            alignment = self._quest_goal_alignment_in_transaction(
                connection, quest_ref=quest_ref, current_ref=str(current["goal_revision_ref"])
            )
        return {
            "status": "ready",
            "current": current,
            "operation_basis": direction_cut,
            "conditions": conditions,
            "work": {"intents": intents},
            "guidance_alignment": alignment,
            "history": self.query_quest_goal_history(
                quest_ref, offset=history_offset, limit=history_limit
            ),
        }

    def next_unfulfilled_goal_work_intent(self) -> dict[str, object] | None:
        with self._database.read() as connection:
            rows = connection.execute(
                text(
                    "SELECT i.* FROM rg_goal_work_intents i JOIN "
                    "rg_quest_goal_revisions r ON r.revision_ref=i.revision_ref "
                    "JOIN rg_quest_goal_heads h ON h.quest_ref=r.quest_ref AND "
                    "h.current_revision_ref=i.revision_ref WHERE i.decision_kind "
                    "IN ('stop','do_not_start') ORDER BY i.created_at,i.intent_ref"
                )
            ).all()
            for row in rows:
                decision = decoded_object(row.decision_json)
                if canonical_hash(decision) != row.decision_hash:
                    raise OwnerConflict("goal_work_intent_invalid")
                target_ref = decision["work"]["target_ref"]
                if row.decision_kind == "stop":
                    lifecycle = connection.execute(
                        text(
                            "SELECT status,cancel_goal_intent_ref FROM "
                            "ar_target_root_lifecycles WHERE target_ref=:target_ref"
                        ),
                        {"target_ref": target_ref},
                    ).first()
                    if (
                        lifecycle is not None
                        and lifecycle.status == "running"
                        and lifecycle.cancel_goal_intent_ref is None
                    ):
                        return {
                            "intent_ref": row.intent_ref,
                            "decision_kind": row.decision_kind,
                        }
                else:
                    block = connection.execute(
                        text(
                            "SELECT 1 FROM ar_target_goal_admission_blocks WHERE "
                            "target_ref=:target_ref"
                        ),
                        {"target_ref": target_ref},
                    ).first()
                    activation = connection.execute(
                        text(
                            "SELECT 1 FROM ar_target_run_activations WHERE "
                            "target_ref=:target_ref"
                        ),
                        {"target_ref": target_ref},
                    ).first()
                    if block is None and activation is None:
                        return {
                            "intent_ref": row.intent_ref,
                            "decision_kind": row.decision_kind,
                        }
        return None

    def _quest_goal_conditions_in_transaction(
        self, connection, *, quest_ref: str, revision_ref: str
    ) -> dict[str, object]:
        runtime = read_runtime_conditions_in_transaction(connection, quest_ref)
        rows = connection.execute(
            text(
                "SELECT * FROM rg_goal_revision_conditions WHERE "
                "revision_ref=:revision_ref AND status='active' "
                "ORDER BY condition_ref"
            ),
            {"revision_ref": revision_ref},
        ).all()
        clauses = [_condition_public(row) for row in rows]
        core = {"runtime_conditions": runtime, "enduring": clauses}
        return {**core, "basis_ref": "goal_conditions_" + canonical_hash(core)[:40]}

    def _quest_goal_work_in_transaction(
        self, connection, *, quest_ref: str, author: ResearchRoot
    ) -> tuple[WorkHandle, ...]:
        items: list[WorkHandle] = []
        if author.kind != "target":
            stage = connection.execute(
                text(
                    "SELECT r.*,a.generation FROM ar_stage_runs r JOIN "
                    "ae_stage_run_requests q ON q.request_ref=r.request_ref JOIN "
                    "ar_stage_attempts a ON a.attempt_ref=r.current_attempt_ref "
                    "WHERE r.run_ref=:run_ref AND q.quest_ref=:quest_ref "
                    "AND r.stage=:stage"
                ),
                {"run_ref": author.run_ref, "quest_ref": quest_ref, "stage": author.kind},
            ).first()
            if stage is None:
                raise OwnerConflict("goal_author_work_unbound")
            items.append(
                WorkHandle(
                    work_ref="stage:" + stage.run_ref,
                    kind="stage",
                    cycle_ref=stage.cycle_ref,
                    run_ref=stage.run_ref,
                    attempt_ref=stage.current_attempt_ref,
                    lifecycle_generation=int(stage.generation),
                    state_revision=int(stage.epoch),
                    frozen_contract_ref=stage.context_pack_hash,
                    state=stage.status,
                )
            )
        target_rows = connection.execute(
            text(
                "SELECT t.target_ref,t.spec_hash,g.cycle_ref,l.target_run_ref,"
                "f.state_revision,f.state AS frontier_state,lc.status AS lifecycle_status,"
                "lc.target_attempt_ref,lc.cancel_ref,"
                "(SELECT MAX(h.ordinal) FROM ar_target_run_handles h WHERE "
                "h.target_ref=t.target_ref) AS generation,"
                "(SELECT w.workspace_ref FROM ar_target_run_workspaces w WHERE "
                "w.target_ref=t.target_ref AND w.status='active' ORDER BY w.ordinal "
                "DESC LIMIT 1) AS workspace_ref,"
                "b.intent_ref AS block_intent_ref "
                "FROM rg_targets t JOIN rg_target_graphs g ON g.graph_ref=t.graph_ref "
                "JOIN ar_target_launches l ON l.target_ref=t.target_ref "
                "LEFT JOIN ar_target_frontier_entries f ON f.target_ref=t.target_ref "
                "LEFT JOIN ar_target_root_lifecycles lc ON lc.target_ref=t.target_ref "
                "LEFT JOIN ar_target_goal_admission_blocks b ON b.target_ref=t.target_ref "
                "WHERE g.quest_ref=:quest_ref AND NOT EXISTS "
                "(SELECT 1 FROM rg_target_commits c WHERE c.target_ref=t.target_ref) "
                "ORDER BY t.ordinal,t.target_ref"
            ),
            {"quest_ref": quest_ref},
        ).all()
        for row in target_rows:
            if row.block_intent_ref is not None:
                continue
            if row.lifecycle_status in {"completed", "cancelled", "finalizing"}:
                continue
            state = (
                "cancel_pending"
                if row.lifecycle_status == "running" and row.cancel_ref is not None
                else "running"
                if row.lifecycle_status == "running"
                else "queued"
            )
            items.append(
                WorkHandle(
                    work_ref="target:" + row.target_ref,
                    kind="target",
                    cycle_ref=row.cycle_ref,
                    run_ref=row.target_run_ref,
                    attempt_ref=row.target_attempt_ref,
                    lifecycle_generation=None if row.generation is None else int(row.generation),
                    state_revision=0 if row.state_revision is None else int(row.state_revision),
                    frozen_contract_ref=row.spec_hash,
                    state=state,
                    target_ref=row.target_ref,
                    workspace_ref=row.workspace_ref,
                )
            )
        if author.kind == "target" and not any(
            item.kind == "target" and item.run_ref == author.run_ref for item in items
        ):
            raise OwnerConflict("goal_author_work_unbound")
        return tuple(items)

    def _verify_goal_cause_in_transaction(
        self,
        connection,
        *,
        quest_ref: str,
        author: ResearchRoot,
        direction_cut: dict[str, object],
        cause: dict[str, object],
    ) -> dict[str, object]:
        if cause["kind"] == "human_guidance":
            delivery = _verified_guidance_delivery(
                connection,
                quest_ref=quest_ref,
                author=author,
                delivery_ref=str(cause["delivery_ref"]),
                expected_guide=cause["guide_ref"],
            )
            binding = direction_cut.get("guidance_binding")
            if binding is not None and binding != delivery["binding"]:
                raise OwnerConflict("goal_guidance_cause_unbound")
            return cause
        evidence = cause.get("evidence")
        assert isinstance(evidence, list)
        for item in evidence:
            row = connection.execute(
                text(
                    "SELECT v.version_ref FROM rm_asset_versions v JOIN rg_asset_roles r "
                    "ON r.version_ref=v.version_ref WHERE r.quest_ref=:quest_ref AND "
                    "v.version_ref=:version_ref AND (v.asset_ref=:source_ref OR "
                    "v.version_ref=:source_ref)"
                ),
                {"quest_ref": quest_ref, **item},
            ).first()
            if row is None:
                raise OwnerConflict("goal_evidence_cause_unbound")
        return cause

    def _verify_goal_conditions_in_transaction(
        self,
        connection,
        *,
        quest_ref: str,
        parent_ref: str,
        cause: dict[str, object],
        conditions: dict[str, object],
    ) -> tuple[dict[str, object], ...]:
        prior = connection.execute(
            text(
                "SELECT * FROM rg_goal_revision_conditions WHERE "
                "revision_ref=:revision_ref AND status='active' ORDER BY condition_ref"
            ),
            {"revision_ref": parent_ref},
        ).all()
        assessments = {
            item["condition_ref"]: item for item in conditions["assessments"]
        }
        if set(assessments) != {row.condition_ref for row in prior}:
            raise OwnerConflict("goal_conditions_coverage_invalid")
        result: list[dict[str, object]] = []
        for row in prior:
            assessment = assessments[row.condition_ref]
            if assessment["disposition"] == "preserved":
                result.append(
                    {
                        "condition_ref": row.condition_ref,
                        "source_json": row.source_json,
                        "source_hash": row.source_hash,
                        "source_text": row.source_text,
                        "meaning": row.meaning,
                        "status": "active",
                        "superseding_source_json": None,
                    }
                )
                continue
            delivery_ref = assessment["superseding_delivery_ref"]
            if cause.get("kind") != "human_guidance" or cause.get("delivery_ref") != delivery_ref:
                raise OwnerConflict("goal_condition_supersession_invalid")
            source = decoded_object(row.source_json)
            newer = _guidance_delivery_source(connection, delivery_ref)
            if float(newer["created_at"]) <= float(source.get("created_at", 0)):
                raise OwnerConflict("goal_condition_supersession_invalid")
            result.append(
                {
                    "condition_ref": row.condition_ref,
                    "source_json": row.source_json,
                    "source_hash": row.source_hash,
                    "source_text": row.source_text,
                    "meaning": row.meaning,
                    "status": "human_superseded",
                    "superseding_source_json": canonical_json(
                        {
                            "kind": "human_guidance",
                            "delivery_ref": delivery_ref,
                            "guide_ref": newer["guide_ref"],
                            "guidance_binding": newer["binding"],
                            "original_text": newer["original_text"],
                            "created_at": newer["created_at"],
                        }
                    ),
                }
            )
        for clause in conditions["newly_identified"]:
            source = _guidance_delivery_source(connection, clause["delivery_ref"])
            if source["quest_ref"] != quest_ref or source["read_at"] is None:
                raise OwnerConflict("goal_condition_source_invalid")
            original = source["original_text"]
            start, end = int(clause["start"]), int(clause["end"])
            if end > len(original) or original[start:end] != clause["text"]:
                raise OwnerConflict("goal_condition_source_invalid")
            source_document = {
                "kind": "human_guidance",
                "delivery_ref": clause["delivery_ref"],
                "guide_ref": source["guide_ref"],
                "start": start,
                "end": end,
                "created_at": source["created_at"],
            }
            condition_ref = "goal_condition_" + canonical_hash(source_document)[:48]
            if condition_ref in {item["condition_ref"] for item in result}:
                raise OwnerConflict("goal_condition_duplicate")
            result.append(
                {
                    "condition_ref": condition_ref,
                    "source_json": canonical_json(source_document),
                    "source_hash": canonical_hash(source_document),
                    "source_text": clause["text"],
                    "meaning": clause["meaning"],
                    "status": "active",
                    "superseding_source_json": None,
                }
            )
        return tuple(result)

    def _verify_goal_retention_in_transaction(
        self, connection, *, quest_ref: str, decisions
    ) -> None:
        for item in decisions:
            if item.kind != "stop":
                continue
            retention = item.retention
            assert retention is not None
            if retention["kind"] in {"pending", "inspected_none"}:
                if (
                    item.work.workspace_ref is None
                    or retention["workspace_ref"] != item.work.workspace_ref
                ):
                    raise OwnerConflict("goal_retention_workspace_invalid")
                continue
            memory = getattr(self, "_quest_goal_research_memory", None)
            target_run_agent = getattr(self, "_quest_goal_target_run_agent", None)
            if memory is None or target_run_agent is None:
                raise OwnerConflict("goal_retention_verifier_unavailable")
            locations = target_run_agent.read_target_workspace_locations(
                item.work.run_ref
            )
            if not any(
                workspace.workspace_ref == item.work.workspace_ref
                and workspace.target_ref == item.work.target_ref
                and workspace.target_run_ref == item.work.run_ref
                for workspace, _path in locations
            ):
                raise OwnerConflict("goal_retention_workspace_invalid")
            for asset in retention["assets"]:
                row = connection.execute(
                    text(
                        "SELECT v.*,c.custody_mode FROM rm_asset_versions v JOIN "
                        "rm_asset_custodies c ON c.version_ref=v.version_ref JOIN "
                        "rg_asset_roles r ON r.version_ref=v.version_ref AND "
                        "r.quest_ref=:quest_ref WHERE v.version_ref=:version_ref "
                        "AND v.asset_ref=:asset_ref AND c.custody_mode='managed'"
                    ),
                    {
                        "quest_ref": quest_ref,
                        "version_ref": asset["version_ref"],
                        "asset_ref": asset["asset_ref"],
                    },
                ).first()
                if (
                    row is None
                    or row.content_hash != asset["content_hash"]
                    or row.manifest_hash != asset["manifest_hash"]
                    or row.receipt_ref != asset["receipt"]["receipt_ref"]
                    or row.receipt_hash != asset["receipt"]["payload_hash"]
                ):
                    raise OwnerConflict("goal_retained_asset_invalid")
                provenance = decoded_object(row.provenance_json)
                if (
                    canonical_hash(provenance) != row.provenance_hash
                    or provenance.get("schema_ref")
                    != "meta-research/target-workspace-note/v1"
                    or provenance.get("target_ref") != item.work.target_ref
                    or provenance.get("target_run_ref") != item.work.run_ref
                    or provenance.get("workspace_ref") != item.work.workspace_ref
                    or provenance.get("source_content_hash") != row.content_hash
                    or provenance.get("producer_kind") != "target_workspace"
                ):
                    raise OwnerConflict("goal_retained_asset_producer_invalid")
                accepted = memory.query_asset_version(asset["version_ref"])
                if (
                    accepted is None
                    or accepted.asset_ref != asset["asset_ref"]
                    or accepted.content_hash != asset["content_hash"]
                    or accepted.manifest_hash != asset["manifest_hash"]
                    or accepted.receipt.as_public_dict() != asset["receipt"]
                    or "managed" not in accepted.custody_modes
                ):
                    raise OwnerConflict("goal_retained_asset_invalid")
                content = memory.read_asset_entry_text(asset["version_ref"])
                if hashlib.sha256(content.encode("utf-8")).hexdigest() != asset[
                    "content_hash"
                ]:
                    raise OwnerConflict("goal_retained_asset_invalid")

    def _quest_goal_work_intent_view(self, connection, row) -> dict[str, object]:
        decision = decoded_object(row.decision_json)
        if canonical_hash(decision) != row.decision_hash:
            raise OwnerConflict("goal_work_intent_invalid")
        actual = "observed"
        if row.decision_kind == "stop":
            lifecycle = connection.execute(
                text(
                    "SELECT status,cancel_ref,cancel_goal_intent_ref,target_run_ref,"
                    "target_attempt_ref FROM "
                    "ar_target_root_lifecycles WHERE target_ref=:target_ref"
                ),
                {"target_ref": decision["work"]["target_ref"]},
            ).first()
            generation = connection.execute(
                text(
                    "SELECT MAX(ordinal) FROM ar_target_run_handles WHERE "
                    "target_ref=:target_ref"
                ),
                {"target_ref": decision["work"]["target_ref"]},
            ).scalar_one()
            exact = lifecycle is not None and (
                lifecycle.target_run_ref == decision["work"]["run_ref"]
                and lifecycle.target_attempt_ref == decision["work"]["attempt_ref"]
                and generation == decision["work"]["lifecycle_generation"]
            )
            source_intent_ref = None
            if not exact:
                actual = "obsolete"
            elif lifecycle.cancel_ref is not None:
                source_intent_ref = lifecycle.cancel_goal_intent_ref
                if source_intent_ref != row.intent_ref:
                    actual = "superseded"
                else:
                    actual = (
                        "cancelled"
                        if lifecycle.status == "cancelled"
                        else "cancel_pending"
                    )
            elif lifecycle.status == "running":
                actual = "pending"
            else:
                actual = lifecycle.status
        elif row.decision_kind == "do_not_start":
            block = connection.execute(
                text(
                    "SELECT intent_ref FROM ar_target_goal_admission_blocks "
                    "WHERE target_ref=:target_ref"
                ),
                {"target_ref": decision["work"]["target_ref"]},
            ).first()
            actual = "blocked" if block is not None and block.intent_ref == row.intent_ref else "pending"
        result = {
            "intent_ref": row.intent_ref,
            "revision_ref": row.revision_ref,
            "decision": decision,
            "actual_status": actual,
        }
        if row.decision_kind == "stop" and source_intent_ref is not None:
            result["source_intent_ref"] = source_intent_ref
        return result

    def _quest_goal_alignment_in_transaction(
        self, connection, *, quest_ref: str, current_ref: str
    ) -> list[dict[str, object]]:
        guides = connection.execute(
            text(
                "SELECT constraint_ref,revision,guidance_hash,receipt_ref,receipt_hash "
                "FROM hc_soft_constraints WHERE scope_ref=:scope_ref AND status='active' "
                "AND json_extract(guidance_json,'$.strength')=5 "
                "ORDER BY constraint_ref,revision"
            ),
            {"scope_ref": "quest:" + quest_ref},
        ).all()
        ancestry: set[str] = set()
        cursor = current_ref
        while cursor:
            ancestry.add(cursor)
            row = connection.execute(
                text(
                    "SELECT parent_ref FROM rg_quest_goal_revisions WHERE "
                    "revision_ref=:revision_ref"
                ),
                {"revision_ref": cursor},
            ).first()
            cursor = "" if row is None else row.parent_ref
        values = []
        for guide in guides:
            guide_ref = {
                "constraint_ref": guide.constraint_ref,
                "revision": int(guide.revision),
                "guidance_hash": guide.guidance_hash,
                "receipt_ref": guide.receipt_ref,
                "receipt_hash": guide.receipt_hash,
            }
            rows = connection.execute(
                text(
                    "SELECT revision_ref,cause_json,author_json FROM "
                    "rg_quest_goal_revisions WHERE quest_ref=:quest_ref ORDER BY sequence"
                ),
                {"quest_ref": quest_ref},
            ).all()
            match = next(
                (
                    row
                    for row in rows
                    if row.revision_ref in ancestry
                    and decoded_object(row.cause_json).get("guide_ref") == guide_ref
                ),
                None,
            )
            values.append(
                {
                    "guide_ref": guide_ref,
                    "status": "pending" if match is None else "aligned",
                    "reason": "awaiting_goal_evolution" if match is None else None,
                    "aligned_revision": None if match is None else match.revision_ref,
                    "current_revision": current_ref,
                    "authored_by": None if match is None else decoded_object(match.author_json),
                }
            )
        return values


def _condition_public(row) -> dict[str, object]:
    source = decoded_object(row.source_json)
    if canonical_hash(source) != row.source_hash:
        raise OwnerConflict("goal_condition_invalid")
    value = {
        "condition_ref": row.condition_ref,
        "source": source,
        "source_text": row.source_text,
        "meaning": row.meaning,
        "status": row.status,
    }
    if row.superseding_source_json is not None:
        value["superseding_source"] = decoded_object(row.superseding_source_json)
    return value


def _goal_effect_replay(connection, effect_key: str, command: dict[str, object]):
    row = connection.execute(
        text("SELECT * FROM rg_goal_evolution_effects WHERE effect_key=:effect_key"),
        {"effect_key": effect_key},
    ).first()
    if row is None:
        return None
    try:
        stored_command = decoded_object(row.command_json)
        receipt = decoded_object(row.receipt_json)
    except (TypeError, ValueError) as error:
        raise OwnerConflict("goal_evolution_effect_invalid") from error
    if (
        stored_command != command
        or canonical_hash(command) != row.command_hash
        or canonical_hash(receipt) != row.receipt_hash
    ):
        raise OwnerConflict("goal_evolution_effect_conflict")
    return receipt


def _validate_arrangement_kinds(decisions) -> None:
    for item in decisions:
        if item.kind == "continue" and item.work.state != "cancel_pending":
            continue
        if (
            item.kind == "stop"
            and item.work.kind == "target"
            and item.work.state in {"running", "cancel_pending"}
        ):
            continue
        if item.kind == "do_not_start" and item.work.kind == "target" and item.work.state == "queued":
            continue
        if item.kind == "finish_stage_boundary" and item.work.kind == "stage":
            continue
        raise OwnerConflict("goal_work_decision_invalid")


def _verified_guidance_delivery(
    connection,
    *,
    quest_ref: str,
    author: ResearchRoot,
    delivery_ref: str,
    expected_guide: object,
) -> dict[str, object]:
    source = _guidance_delivery_source(connection, delivery_ref)
    binding = source["binding"]
    if (
        source["quest_ref"] != quest_ref
        or binding["identity"]
        != {
            "root_kind": author.kind,
            "run_ref": author.run_ref,
            "operation_ref": author.operation_ref,
        }
        or source["read_at"] is None
        or source["guide_ref"] != expected_guide
    ):
        raise OwnerConflict("goal_guidance_cause_unbound")
    return source


def _guidance_delivery_source(connection, delivery_ref: str) -> dict[str, object]:
    row = connection.execute(
        text(
            "SELECT d.*,s.root_kind,s.run_ref,s.operation_ref,s.quest_ref,"
            "s.snapshot_ref,s.snapshot_hash,s.snapshot_json FROM "
            "hc_guidance_deliveries d JOIN hc_guidance_snapshots s "
            "ON s.snapshot_ref=d.snapshot_ref WHERE d.delivery_ref=:delivery_ref"
        ),
        {"delivery_ref": delivery_ref},
    ).first()
    if row is None:
        raise OwnerConflict("goal_guidance_cause_unbound")
    payload = decoded_object(row.snapshot_json)
    if canonical_hash(payload) != row.snapshot_hash:
        raise OwnerConflict("goal_guidance_cause_unbound")
    item = next(
        (
            item
            for item in payload.get("deliveries", [])
            if item.get("delivery_ref") == delivery_ref
        ),
        None,
    )
    if not isinstance(item, dict) or not isinstance(item.get("guide"), dict):
        raise OwnerConflict("goal_guidance_cause_unbound")
    guide = item["guide"]
    guidance = guide.get("guidance")
    original = guidance.get("text") if isinstance(guidance, dict) else None
    if not isinstance(original, str):
        raise OwnerConflict("goal_guidance_cause_unbound")
    guide_ref = {
        key: guide[key]
        for key in (
            "constraint_ref",
            "revision",
            "guidance_hash",
            "receipt_ref",
            "receipt_hash",
        )
    }
    binding = {
        "identity": {
            "root_kind": row.root_kind,
            "run_ref": row.run_ref,
            "operation_ref": row.operation_ref,
        },
        "quest_ref": row.quest_ref,
        "snapshot_ref": row.snapshot_ref,
        "snapshot_hash": row.snapshot_hash,
    }
    return {
        "quest_ref": row.quest_ref,
        "binding": binding,
        "guide_ref": guide_ref,
        "original_text": original,
        "read_at": row.read_at,
        "created_at": row.created_at,
    }


def _required_ref(value: object, code: str, *, maximum: int = 256) -> None:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise OwnerConflict(code)
