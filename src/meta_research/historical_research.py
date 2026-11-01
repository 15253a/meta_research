"""Bounded historical navigation over existing RG and AE records."""
from __future__ import annotations

from sqlalchemy import text

from meta_research.owners.common import OwnerConflict


# Receipt verification needs identities, not the potentially large closure JSON.
_COMMIT_COLUMNS = (
    "commit_ref,request_ref,cycle_ref,stage,epoch,run_ref,outcome_ref,outcome_kind,disposition,"
    "run_completion_receipt_ref,run_completion_receipt_hash,outcome_receipt_ref,outcome_receipt_hash,"
    "basis_kind,basis_ref,basis_receipt_issuer,basis_receipt_kind,basis_receipt_subject_ref,"
    "basis_receipt_ref,basis_receipt_hash,closure_hash,receipt_hash"
)

_STAGE_SOURCES = {
    "idea": ("rg_idea_outcome_decisions", "outcome_ref", "idea_content_ref"),
    "plan": ("rg_formal_plan_decisions", "formal_plan_ref", "plan_content_ref"),
    "reasoning": ("rg_reasoning_outcome_decisions", "outcome_ref", "reasoning_content_ref"),
}


def discover_question_cycles(graph, *, quest_ref, question_ref, offset=0, limit=12,
                             query_revision=None):
    """Page a Question's Cycles without materializing the Quest's stage history."""
    from meta_research.research_pagination import bind_page, page_state

    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 12:
        raise OwnerConflict("research_history_page_invalid")
    with graph._database.read_snapshot() as connection:
        question = graph.query_question_history_by_ref(question_ref)
        if question is None or question.quest_ref != quest_ref:
            raise OwnerConflict("historical_question_not_visible")
        params = {"quest": quest_ref, "question": question_ref, "limit": limit + 1, "offset": offset}
        # Only revision metadata is read across this Question. Scientific bodies
        # and unrelated Questions/Quests never participate in page validation.
        cycles = connection.execute(text(
            "SELECT cycle_ref,status,created_at,updated_at FROM ae_cycles "
            "WHERE quest_ref=:quest AND question_ref=:question ORDER BY created_at DESC,cycle_ref"
        ), params).all()
        revisions = connection.execute(text(
            "SELECT s.commit_ref AS ref,s.receipt_hash AS revision FROM ae_stage_commits s "
            "JOIN ae_cycles c ON c.cycle_ref=s.cycle_ref "
            "WHERE c.quest_ref=:quest AND c.question_ref=:question "
            "UNION ALL SELECT r.request_ref,r.receipt_hash FROM ae_stage_run_requests r "
            "JOIN ae_cycles c ON c.cycle_ref=r.cycle_ref "
            "WHERE c.quest_ref=:quest AND c.question_ref=:question ORDER BY ref"
        ), params).all()
        token = bind_page({"operation": "question_cycles", "quest_ref": quest_ref,
                           "question_ref": question_ref},
                          [list(row) for row in cycles] + [list(row) for row in revisions],
                          offset=offset, query_revision=query_revision)
        items = []
        for cycle in cycles[offset:offset + limit]:
            commits = connection.execute(text(
                "SELECT " + _COMMIT_COLUMNS + " FROM ae_stage_commits "
                "WHERE cycle_ref=:cycle ORDER BY epoch,stage,commit_ref"
            ), {"cycle": cycle.cycle_ref}).all()
            stages = [_stage_summary(connection, row) for row in commits]
            requests = connection.execute(text(
                "SELECT request_ref,stage,epoch FROM ae_stage_run_requests r WHERE cycle_ref=:cycle "
                "AND NOT EXISTS (SELECT 1 FROM ae_stage_commits c WHERE c.request_ref=r.request_ref) "
                "ORDER BY epoch,stage,request_ref"
            ), {"cycle": cycle.cycle_ref}).all()
            stages.extend({"request_ref": row.request_ref, "stage": row.stage, "epoch": row.epoch,
                           "disposition": "requested", "content_state": "not_committed"} for row in requests)
            items.append({"cycle_ref": cycle.cycle_ref, "question_ref": question_ref,
                          "status": cycle.status, "stages": stages})
    next_offset = offset + len(items) if offset + len(items) < len(cycles) else None
    return {"items": items, "offset": offset, "next_offset": next_offset,
            "query_revision": token, "summary_only": True,
            **page_state(items, next_offset, offset=offset)}


def _stage_summary(connection, row):
    from meta_research.owners.advancement_engine import _stage_commit_receipt_hash

    if row.receipt_hash != _stage_commit_receipt_hash(row):
        raise OwnerConflict("content_drifted")
    if row.request_ref is not None:
        bound = connection.execute(text(
            "SELECT r.request_ref FROM ae_stage_run_requests r JOIN ae_cycles c ON c.cycle_ref=r.cycle_ref "
            "WHERE r.request_ref=:request AND r.cycle_ref=:cycle AND r.stage=:stage AND r.epoch=:epoch "
            "AND r.quest_ref=c.quest_ref AND r.question_ref=c.question_ref"
        ), {"request": row.request_ref, "cycle": row.cycle_ref, "stage": row.stage, "epoch": row.epoch}).first()
        if bound is None:
            raise OwnerConflict("content_drifted")
    item = {key: getattr(row, key) for key in (
        "commit_ref", "request_ref", "stage", "epoch", "disposition", "outcome_ref", "outcome_kind")}
    item["reader"] = {"source_ref": row.commit_ref, "version_ref": row.receipt_hash}
    item["content_state"] = "available"
    if row.disposition == "completed" and row.stage in _STAGE_SOURCES:
        table, key, content_key = _STAGE_SOURCES[row.stage]
        scientific_ref = ",scientific_outcome_ref" if row.stage == "reasoning" else ""
        source = connection.execute(text(
            f"SELECT {content_key} AS content_ref,payload_hash{scientific_ref} FROM {table} "
            f"WHERE {key}=:outcome AND request_ref=:request AND decision='accepted'"
        ), {"outcome": row.outcome_ref, "request": row.request_ref}).first()
        if source is None:
            item["content_state"] = "content_unavailable"
        elif row.stage == "reasoning":
            item["scientific_outcome_ref"] = source.scientific_outcome_ref
            item["reader"] = {"source_ref": source.scientific_outcome_ref,
                              "version_ref": source.scientific_outcome_ref}
        else:
            item["reader"] = {"source_ref": source.content_ref, "version_ref": source.payload_hash}
    if row.basis_ref is not None:
        item["basis_kind"] = row.basis_kind
        item["basis_ref"] = row.basis_ref
        source = connection.execute(text(
            "SELECT receipt_hash FROM ae_stage_commits WHERE commit_ref=:ref"
        ), {"ref": row.basis_ref}).first()
        if source is not None:
            item["basis_reader"] = {"source_ref": row.basis_ref, "version_ref": source.receipt_hash}
    return item


def read_historical_stage_content(memory, *, quest_ref, source_ref, version_ref):
    """Read an exact AE commit closure, independent of the current native root."""
    from meta_research.owners.advancement_engine import (
        _stage_commit, _stage_commit_receipt_hash, _stage_request,
    )

    with memory._database.read_snapshot() as connection:
        row = connection.execute(text("SELECT * FROM ae_stage_commits WHERE commit_ref=:ref"),
                                 {"ref": source_ref}).first()
        if row is None:
            return None
        cycle = connection.execute(text("SELECT quest_ref,question_ref FROM ae_cycles WHERE cycle_ref=:ref"),
                                   {"ref": row.cycle_ref}).first()
        if cycle is None or cycle.quest_ref != quest_ref:
            raise OwnerConflict("content_source_unbound")
        if version_ref != row.receipt_hash:
            raise OwnerConflict("content_version_unbound")
        if row.receipt_hash != _stage_commit_receipt_hash(row):
            raise OwnerConflict("content_drifted")
        try:
            commit = _stage_commit(row)
        except OwnerConflict as error:
            raise OwnerConflict("content_drifted") from error
        if row.request_ref is not None:
            request_row = connection.execute(text("SELECT * FROM ae_stage_run_requests WHERE request_ref=:ref"),
                                             {"ref": row.request_ref}).first()
            if request_row is None:
                raise OwnerConflict("content_unavailable")
            try:
                request = _stage_request(request_row)
            except OwnerConflict as error:
                raise OwnerConflict("content_drifted") from error
            if (request.cycle_ref != row.cycle_ref or request.stage != row.stage or request.epoch != row.epoch
                    or request.accepted_question.quest_ref != quest_ref
                    or request.accepted_question.question_ref != cycle.question_ref):
                raise OwnerConflict("content_drifted")
        if row.stage == "bundle" and row.disposition == "completed" and commit.closure is None:
            raise OwnerConflict("content_unavailable")
        result = {"commit_ref": row.commit_ref, "cycle_ref": row.cycle_ref, "stage": row.stage,
                  "disposition": row.disposition, "outcome_ref": row.outcome_ref,
                  "receipt": commit.receipt.as_public_dict()}
        if commit.closure is not None:
            result["closure"] = commit.closure
        if row.disposition == "completed" and row.stage in _STAGE_SOURCES:
            summary = _stage_summary(connection, row)
            result["content_state"] = summary["content_state"]
            if summary["reader"]["source_ref"] != row.commit_ref:
                result["content_reader"] = summary["reader"]
        if commit.basis_ref is not None:
            result.update(basis_ref=commit.basis_ref, basis_kind=commit.basis_kind)
        return result
