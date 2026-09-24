"""Related Question facts owned by RG, with the existing Reasoning run as source."""
from __future__ import annotations

import time
from dataclasses import asdict
from sqlalchemy import text

from meta_research.owners.common import OwnerConflict, canonical_hash, new_ref
from meta_research.root_capabilities import ROOT_AGENT_KINDS
from meta_research.semantic_mcp import SemanticMcpError, SemanticOperation

QUESTION_RELATION_OPERATION_IDS = (
    "research_graph.question_relations.read",
    "research_graph.question_relations.record",
    "research_graph.question_relations.record.reconcile",
)

QUESTION_HISTORY_OPERATION_ID = "research_graph.question_history.read"


class QuestionRelationOwnerMixin:
    def record_question_relation(self, *, quest_ref, source_run_ref, effect_id,
                                 left_question_ref, right_question_ref, notes):
        if (not isinstance(effect_id, str) or not 1 <= len(effect_id) <= 128
            or not isinstance(notes, str) or not notes.strip() or len(notes) > 8000
            or not isinstance(left_question_ref, str) or not isinstance(right_question_ref, str)
            or left_question_ref == right_question_ref):
            raise OwnerConflict("question_relation_invalid")
        left_question_ref, right_question_ref = sorted((left_question_ref, right_question_ref))
        payload = {"quest_ref": quest_ref, "source_run_ref": source_run_ref,
                   "effect_id": effect_id, "left_question_ref": left_question_ref,
                   "right_question_ref": right_question_ref, "notes": notes}
        with self._database.fenced_write() as connection:
            source = connection.execute(text(
                "SELECT r.request_ref FROM ar_stage_runs r JOIN ae_stage_run_requests q "
                "ON q.request_ref=r.request_ref WHERE r.run_ref=:source_run_ref "
                "AND r.stage='reasoning' AND q.quest_ref=:quest_ref"
            ), payload).first()
            if source is None:
                raise OwnerConflict("question_relation_source_invalid")
            payload["source_request_ref"] = source.request_ref
            digest = canonical_hash(payload)
            previous = connection.execute(text("SELECT * FROM rg_question_relations WHERE source_run_ref=:source_run_ref AND effect_id=:effect_id"), payload).first()
            if previous is not None:
                result = _relation(previous)
                if result["content_hash"] != digest:
                    raise OwnerConflict("question_relation_identity_conflict")
                return result
            for question_ref in (left_question_ref, right_question_ref):
                question = _question(self, question_ref)
                if question is None or question.quest_ref != quest_ref:
                    raise OwnerConflict("question_relation_question_unbound")
            values = {**payload, "source_request_ref": source.request_ref,
                      "relation_ref": new_ref("question_relation"), "content_hash": digest,
                      "created_at": time.time()}
            connection.execute(text(
                "INSERT INTO rg_question_relations (relation_ref,quest_ref,left_question_ref,right_question_ref,"
                "source_run_ref,source_request_ref,effect_id,notes,content_hash,created_at) VALUES "
                "(:relation_ref,:quest_ref,:left_question_ref,:right_question_ref,:source_run_ref,"
                ":source_request_ref,:effect_id,:notes,:content_hash,:created_at)"), values)
            self._feed.record(connection, "research_graph.question_relation_recorded",
                              {key: values[key] for key in ("relation_ref", "quest_ref", "left_question_ref", "right_question_ref", "source_run_ref")})
        return {**values, "kind": "related"}

    def query_question_relations(self, *, quest_ref, question_ref=None, offset=0,
                                 source_run_ref=None, effect_id=None):
        if type(offset) is not int or offset < 0:
            raise OwnerConflict("question_relation_page_invalid")
        parameters = {"quest_ref": quest_ref, "question_ref": question_ref, "offset": offset,
                      "source_run_ref": source_run_ref, "effect_id": effect_id}
        where = "quest_ref=:quest_ref"
        if question_ref is not None:
            question = _question(self, question_ref)
            if question is None or question.quest_ref != quest_ref:
                raise OwnerConflict("question_relation_question_unbound")
            where += " AND (left_question_ref=:question_ref OR right_question_ref=:question_ref)"
        if effect_id is not None:
            where += " AND source_run_ref=:source_run_ref AND effect_id=:effect_id"
        with self._database.read() as connection:
            rows = connection.execute(text("SELECT * FROM rg_question_relations WHERE " + where
                + " ORDER BY created_at,relation_ref LIMIT 13 OFFSET :offset"), parameters).all()
        return {"items": [_relation(row) for row in rows[:12]], "offset": offset,
                "next_offset": offset + 12 if len(rows) > 12 else None}


def _relation(row):
    value = dict(row._mapping)
    payload = {key: value[key] for key in ("quest_ref", "source_run_ref", "source_request_ref", "effect_id",
                                          "left_question_ref", "right_question_ref", "notes")}
    if canonical_hash(payload) != value["content_hash"]:
        raise OwnerConflict("question_relation_content_invalid")
    return {**value, "kind": "related"}


def _question(owner, question_ref):
    try:
        # Accepted history remains usable after a Question is temporarily put
        # aside. This public read already verifies the original RG acceptance.
        return owner.query_question_history_by_ref(question_ref)
    except OwnerConflict as error:
        if error.code in {"question_lifecycle_not_found", "question_not_found"}:
            raise OwnerConflict("question_relation_question_unbound") from error
        raise


def _read_scope(agent_runtime, context, *, expected_root_kind=None):
    """Verify a bound Root's scope; reads admit any root, effects pass their own kind."""
    if expected_root_kind is not None:
        if context.root_kind != expected_root_kind:
            raise OwnerConflict("question_relation_scope_invalid")
    elif context.root_kind not in ROOT_AGENT_KINDS:
        raise OwnerConflict("question_relation_scope_invalid")
    scope = agent_runtime.verify_root_agent_runtime_scope(
        root_kind=context.root_kind, run_ref=context.run_ref,
        attempt_ref=context.attempt_ref, root_session_ref=context.root_session_ref,
        fence_ref=context.fence_ref, runtime_binding_hash=context.capability_binding_hash)
    quest_ref = scope.get("quest_ref")
    if not isinstance(quest_ref, str) or not quest_ref:
        raise OwnerConflict("question_relation_scope_invalid")
    return quest_ref


def question_history_operations(*, research_graph, agent_runtime):
    def read(context, arguments):
        try:
            quest_ref = _read_scope(agent_runtime, context)
            question = research_graph.query_question_history_by_ref(
                arguments["question_ref"])
            if question is None or question.quest_ref != quest_ref:
                return {"status": "not_found", "question": None, "items": []}
            # Reads deliberately include pruned questions; history stays exact.
            history = research_graph.query_question_research_history(
                quest_ref=question.quest_ref, question_ref=question.question_ref,
                offset=arguments.get("offset",0), limit=arguments.get("limit",12))
            return {"status": "accepted", "question": asdict(question), **history}
        except OwnerConflict as error:
            raise SemanticMcpError(str(error)) from error

    return SemanticOperation(
        semantic_operation_id=QUESTION_HISTORY_OPERATION_ID, owning_module="research_graph",
        description="Read one exact accepted Question record of this Quest (including a question set aside) plus its accepted reasoning-outcome history refs.",
        input_schema={"type": "object", "properties": {
            "question_ref": {"type": "string", "minLength": 1, "maxLength": 96},
            "offset": {"type":"integer","minimum":0},
            "limit": {"type":"integer","minimum":1,"maximum":12}},
            "required": ["question_ref"], "additionalProperties": False},
        output_schema={"type": "object"}, access_mode="read",
        handler=lambda context, arguments: read(context, arguments))


def question_relation_operations(*, research_graph, agent_runtime):
    def invoke(action, context, arguments):
        try:
            quest_ref = _read_scope(agent_runtime, context,
                expected_root_kind=None if action == "read" else "reasoning")
            if action == "record":
                return research_graph.record_question_relation(quest_ref=quest_ref,
                    source_run_ref=context.run_ref, **arguments)
            if action == "reconcile":
                result = research_graph.query_question_relations(quest_ref=quest_ref,
                    source_run_ref=context.run_ref, effect_id=arguments["effect_id"])
                return {"status": "recorded" if result["items"] else "not_found",
                        "relation": result["items"][0] if result["items"] else None}
            return research_graph.query_question_relations(quest_ref=quest_ref, **arguments)
        except OwnerConflict as error:
            raise SemanticMcpError(str(error)) from error
    ref = {"type": "string", "minLength": 1, "maxLength": 96}
    effect = {"type": "string", "minLength": 1, "maxLength": 128}
    schemas = (
        {"type": "object", "properties": {"question_ref": ref, "offset": {"type": "integer", "minimum": 0}}, "additionalProperties": False},
        {"type": "object", "properties": {"effect_id": effect, "left_question_ref": ref,
         "right_question_ref": ref, "notes": {"type": "string", "minLength": 1, "maxLength": 8000}},
         "required": ["effect_id", "left_question_ref", "right_question_ref", "notes"], "additionalProperties": False},
        {"type": "object", "properties": {"effect_id": effect}, "required": ["effect_id"], "additionalProperties": False},
    )
    descriptions = (
        "Read a page of related accepted Questions in this Quest, optionally focused on one Question.",
        "Record a related/overlapping Question pair and a concise explanation, using exact accepted refs. The current Reasoning run supplies provenance; effect_id has stable retry identity.",
        "Reconcile a Question relation effect recorded by this same Reasoning run, including after a technical retry.",
    )
    return tuple(SemanticOperation(semantic_operation_id=operation, owning_module="research_graph",
        description=description, input_schema=schema, output_schema={"type": "object"},
        access_mode="effect" if action == "record" else "reconcile" if action == "reconcile" else "read",
        reconciliation_operation_id=QUESTION_RELATION_OPERATION_IDS[2] if action == "record" else None,
        handler=lambda context, arguments, action=action: invoke(action, context, arguments))
        for operation, schema, description, action in zip(QUESTION_RELATION_OPERATION_IDS,
            schemas, descriptions, ("read", "record", "reconcile")))
