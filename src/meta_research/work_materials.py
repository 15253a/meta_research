"""HC reception receipts and reads of lightweight server originals."""
from __future__ import annotations

from dataclasses import dataclass
import json
import time
from sqlalchemy import text

from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json, new_ref
from meta_research.semantic_mcp import SemanticMcpError, SemanticOperation
from meta_research.work_material_contract import WORK_MATERIAL_OPERATION_IDS


@dataclass(frozen=True)
class MaterialSubmission:
    receiver: dict[str, object]
    selections: tuple[dict[str, object], ...]
    description: str

    @classmethod
    def parse(cls, value):
        if not isinstance(value, dict) or set(value) != {"receiver", "selections", "description"}:
            raise OwnerConflict("material_submission_invalid")
        receiver, selections, description = value["receiver"], value["selections"], value["description"]
        if (not isinstance(receiver, dict) or receiver.get("kind") not in {"creation", "manual", "current", "request"}
            or not isinstance(selections, list) or not 1 <= len(selections) <= 100
            or not isinstance(description, str) or len(description) > 4000):
            raise OwnerConflict("material_submission_invalid")
        return cls(receiver, tuple(selections), description)

    def as_dict(self):
        return {"receiver": self.receiver, "selections": list(self.selections), "description": self.description}


def material_reference(database, reference_ref):
    with database.read() as connection:
        row = connection.execute(text("SELECT r.*, s.receiver_json, s.command_json, s.command_hash, s.state, s.created_at "
            "FROM hc_work_material_references r JOIN hc_work_material_submissions s USING (submission_ref) "
            "WHERE r.reference_ref=:ref"), {"ref": reference_ref}).first()
        if row is None or row.state != "ready":
            raise OwnerConflict("material_reference_not_found")
        selection = json.loads(row.selection_json)
        command = json.loads(row.command_json)
        receiver = json.loads(row.receiver_json)
        if canonical_hash(selection) != row.selection_hash or canonical_hash(command) != row.command_hash or command["receiver"] != receiver:
            raise OwnerConflict("material_receipt_invalid")
        accesses = connection.execute(text("SELECT result_json FROM hc_work_material_accesses WHERE reference_ref=:ref ORDER BY created_at,access_ref"), {"ref": reference_ref}).all()
    facts = [json.loads(access.result_json) for access in accesses]
    ranges = [fact for fact in facts if fact["operation"] == "read" and "error" not in fact]
    return {"reference_ref": row.reference_ref, "submission_ref": row.submission_ref, "receiver": receiver,
            "source": selection, "description": command["description"], "registered_at": row.created_at,
            "availability": facts[-1].get("availability", "available") if facts else "available",
            "read_state": "read" if ranges else "not_read", "read_ranges": ranges,
            "failures": [fact for fact in facts if "error" in fact], "unexpanded": selection["kind"] == "directory",
            "reader": {"reference_ref": row.reference_ref}}


class WorkMaterialsMixin:
    def material_receiver(self, kind, anchor_ref):
        if kind == "creation":
            view = self.query_quest_creation(anchor_ref)
            return {"kind": kind, "initialization_id": anchor_ref, "draft_revision": view["quest_draft"]["revision"],
                    "draft_hash": view["quest_draft"]["hash"], "root_session_ref": view["intent_session"]["ref"]}
        if kind == "manual":
            view = self.query_manual_question_creation(anchor_ref)
            return {"kind": kind, "context_ref": anchor_ref, "quest_ref": view["quest_ref"],
                    "parent_question_ref": view["parent_question_ref"], "generation": view["generation"]}
        raise OwnerConflict("material_receiver_invalid")

    def current_material_receiver(self, quest_ref, question_ref=None):
        return self._root_workspaces.current_material_receiver(quest_ref, question_ref)

    def _validate_material_receiver(self, receiver):
        kind = receiver["kind"]
        if kind == "creation":
            view = self.query_quest_creation(receiver.get("initialization_id"))
            if view["status"] in {"completed", "cancelled"}:
                raise OwnerConflict("material_receiver_closed")
            current = self.material_receiver(kind, receiver["initialization_id"])
        elif kind == "manual":
            view = self.query_manual_question_creation(receiver.get("context_ref"))
            if view["status"] in {"completed", "cancelled"}:
                raise OwnerConflict("material_receiver_closed")
            current = self.material_receiver(kind, receiver["context_ref"])
        elif kind == "current":
            current = self.current_material_receiver(receiver.get("quest_ref"), receiver.get("question_ref"))
        else:
            raise OwnerConflict("material_receiver_invalid")
        if receiver != current:
            raise OwnerConflict("material_receiver_stale")

    def _material_replay(self, connection, key, command):
        row = connection.execute(text("SELECT * FROM hc_work_material_submissions WHERE idempotency_key=:key"), {"key": key}).first()
        if row is not None and row.command_hash != canonical_hash(command):
            raise OwnerConflict("material_idempotency_conflict")
        return row

    def _prepare_material_submission(self, value):
        submission = MaterialSubmission.parse(value)
        paths = set()
        for selection in submission.selections:
            self._root_workspaces.server_files.validate_selection(selection)
            if selection["absolute_path"] in paths:
                raise OwnerConflict("material_selection_duplicate")
            paths.add(selection["absolute_path"])
        return submission

    def _insert_material_submission(self, connection, *, command, key, anchor_kind, anchor_ref,
                                    state="ready", validate_receiver=True):
        replay = self._material_replay(connection, key, command)
        if replay is not None:
            return replay.submission_ref
        submission = MaterialSubmission.parse(command)
        if validate_receiver:
            self._validate_material_receiver(submission.receiver)
        reference = "material_submission:" + canonical_hash({"key": key})
        connection.execute(text("INSERT INTO hc_work_material_submissions (submission_ref,idempotency_key,command_hash,command_json,receiver_json,anchor_kind,anchor_ref,state,created_at) "
            "VALUES (:ref,:key,:hash,:command,:receiver,:kind,:anchor,:state,:now)"),
            {"ref": reference, "key": key, "hash": canonical_hash(command), "command": canonical_json(command),
             "receiver": canonical_json(submission.receiver), "kind": anchor_kind, "anchor": anchor_ref, "state": state, "now": time.time()})
        for ordinal, selection in enumerate(submission.selections):
            ref = "material:" + canonical_hash({"submission": reference, "ordinal": ordinal})
            connection.execute(text("INSERT INTO hc_work_material_references (reference_ref,submission_ref,ordinal,selection_json,selection_hash) VALUES (:ref,:submission,:ordinal,:selection,:hash)"),
                {"ref": ref, "submission": reference, "ordinal": ordinal, "selection": canonical_json(selection), "hash": canonical_hash(selection)})
        for root in submission.receiver.get("roots", []):
            connection.execute(text("INSERT INTO hc_work_material_roots (submission_ref,workspace_ref) VALUES (:ref,:workspace)"),
                {"ref": reference, "workspace": root["workspace_ref"]})
        self._feed.record(connection, "human_collaboration.work_materials_submitted", {"submission_ref": reference, "anchor_kind": anchor_kind, "anchor_ref": anchor_ref})
        return reference

    def register_work_materials(self, *, anchor_kind, anchor_ref, command, idempotency_key):
        if anchor_kind not in {"creation", "manual"} or not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128:
            raise OwnerConflict("material_submission_invalid")
        submission = MaterialSubmission.parse(command)
        receiver_anchor = submission.receiver.get("initialization_id" if anchor_kind == "creation" else "context_ref")
        if submission.receiver["kind"] != anchor_kind or receiver_anchor != anchor_ref:
            raise OwnerConflict("material_receiver_invalid")
        with self._database.read() as connection:
            replay = self._material_replay(connection, idempotency_key, command)
        if replay is not None:
            return self.query_material_submission(replay.submission_ref)
        self._prepare_material_submission(command)
        with self._database.fenced_write() as connection:
            ref = self._insert_material_submission(connection, command=command, key=idempotency_key,
                anchor_kind=anchor_kind, anchor_ref=anchor_ref)
        return self.query_material_submission(ref)

    def query_material_submission(self, submission_ref):
        with self._database.read() as connection:
            row = connection.execute(text("SELECT * FROM hc_work_material_submissions WHERE submission_ref=:ref"), {"ref": submission_ref}).first()
            if row is None or row.state != "ready":
                raise OwnerConflict("material_reference_not_found")
            references = connection.execute(text("SELECT reference_ref FROM hc_work_material_references WHERE submission_ref=:ref ORDER BY ordinal"), {"ref": submission_ref}).all()
        return {"submission_ref": submission_ref, "receiver": json.loads(row.receiver_json),
                "references": [material_reference(self._database, item.reference_ref) for item in references],
                "description": json.loads(row.command_json)["description"], "state": "saved", "created_at": row.created_at}

    def query_work_materials(self, anchor_kind, anchor_ref):
        with self._database.read() as connection:
            rows = connection.execute(text("SELECT submission_ref FROM hc_work_material_submissions WHERE anchor_kind=:kind AND anchor_ref=:anchor AND state='ready' ORDER BY created_at,submission_ref"), {"kind": anchor_kind, "anchor": anchor_ref}).all()
        return [self.query_material_submission(row.submission_ref) for row in rows]

    def query_work_material(self, reference_ref):
        return material_reference(self._database, reference_ref)

    def _authorize_material(self, reference, context):
        if context is None:
            return
        visible = {location.workspace_ref for location in self._root_workspaces._visible(context)}
        receiver = reference["receiver"]
        if not visible.intersection(root["workspace_ref"] for root in receiver.get("roots", [])):
            raise OwnerConflict("material_not_visible")

    def _record_material_access(self, reference_ref, actor, path, value):
        with self._database.fenced_write() as connection:
            connection.execute(text("INSERT INTO hc_work_material_accesses (access_ref,reference_ref,path,actor,result_json,created_at) VALUES (:ref,:reference,:path,:actor,:result,:now)"),
                {"ref": new_ref("material_access"), "reference": reference_ref, "path": path, "actor": actor,
                 "result": canonical_json(value), "now": time.time()})

    def discover_work_materials(self, *, reference_ref=None, path="", cursor=None, limit=50, context=None, actor="browser", offset=0):
        if reference_ref is None:
            if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
                raise OwnerConflict("material_page_invalid")
            visible = None if context is None else [location.workspace_ref for location in self._root_workspaces._visible(context)]
            with self._database.read() as connection:
                query = "SELECT r.reference_ref FROM hc_work_material_references r JOIN hc_work_material_submissions s USING(submission_ref) WHERE s.state='ready'"
                params = {"offset": offset, "limit": limit + 1}
                if visible is not None:
                    query += " AND EXISTS (SELECT 1 FROM hc_work_material_roots w WHERE w.submission_ref=s.submission_ref AND w.workspace_ref IN (SELECT value FROM json_each(:visible)))"
                    params["visible"] = json.dumps(visible)
                rows = connection.execute(text(query + " ORDER BY s.created_at,r.reference_ref LIMIT :limit OFFSET :offset"), params).all()
            return {"references": [self.query_work_material(row.reference_ref) for row in rows[:limit]],
                    "next_offset": offset + limit if len(rows) > limit else None}
        reference = self.query_work_material(reference_ref)
        self._authorize_material(reference, context)
        actor = context.run_ref if context is not None else actor
        try:
            result = self._root_workspaces.server_files.discover(reference["source"], actor=actor + ":" + reference_ref, path=path, cursor=cursor, limit=limit)
        except OwnerConflict as error:
            self._record_material_access(reference_ref, actor, path, {"operation": "discover", "error": error.code, "availability": error.code, "path": path})
            raise
        return {"reference_ref": reference_ref, **result, "read_state": reference["read_state"]}

    def read_work_material(self, *, reference_ref, path, observation_ref, offset=0, max_bytes=65536, context=None, actor="browser"):
        reference = self.query_work_material(reference_ref)
        self._authorize_material(reference, context)
        actor = context.run_ref if context is not None else actor
        try:
            value = self._root_workspaces.server_files.read(reference["source"], path=path, observation_ref=observation_ref, offset=offset, max_bytes=max_bytes)
        except OwnerConflict as error:
            self._record_material_access(reference_ref, actor, path, {"operation": "read", "error": error.code, "availability": error.code, "path": path})
            raise
        self._record_material_access(reference_ref, actor, path, {"operation": "read", "path": path,
            "observation_ref": value["observation"]["observation_ref"], "offset": offset, "bytes": value["bytes"],
            "eof": value["eof"], "availability": "available"})
        return {"reference_ref": reference_ref, **value}

    def discover_creation_materials(self, *, initialization_id=None, context_ref=None, **arguments):
        kind, anchor = ("creation", initialization_id) if initialization_id is not None else ("manual", context_ref)
        self.material_receiver(kind, anchor)
        allowed = {ref["reference_ref"] for item in self.query_work_materials(kind, anchor) for ref in item["references"]}
        reference = arguments.get("reference_ref")
        if reference is None:
            return {"references": [self.query_work_material(ref) for ref in sorted(allowed)], "next_offset": None}
        if reference not in allowed:
            raise OwnerConflict("material_not_visible")
        return self.discover_work_materials(**arguments)

    def read_creation_material(self, *, initialization_id=None, context_ref=None, **arguments):
        self.discover_creation_materials(initialization_id=initialization_id, context_ref=context_ref,
            reference_ref=arguments["reference_ref"], limit=1)
        return self.read_work_material(**arguments)


def material_operations(human):
    def call(context, arguments):
        try:
            if context.operation_id == WORK_MATERIAL_OPERATION_IDS[0]:
                return human.discover_work_materials(context=context, **arguments)
            return human.read_work_material(context=context, **arguments)
        except OwnerConflict as error:
            raise SemanticMcpError(error.code) from error
    properties = {"reference_ref": {"type": "string", "maxLength": 96}, "path": {"type": "string", "maxLength": 4096}}
    return (SemanticOperation(WORK_MATERIAL_OPERATION_IDS[0], "human_collaboration",
        "Discover saved work material references visible to this actual root and eligible earlier work. Expand one original server directory at a time without preloading, hashing, copying or formal RM intake. Entries and unexpanded scope do not prove reading or understanding. Use observation_ref from an entry for a bounded read.", call,
        {"type": "object", "properties": {**properties, "cursor": {"type": "string"}, "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, "additionalProperties": False}, {"type": "object"}),
        SemanticOperation(WORK_MATERIAL_OPERATION_IDS[1], "human_collaboration",
        "Read at most 65536 requested bytes from a saved server material reference with its discovered observation_ref. Recheck original source identity and current root visibility. Returns UTF-8 or base64, exact byte interval and EOF; records successful ranges and failures without claiming comprehension. Changed originals require fresh discovery. No RM binding is required.", call,
        {"type": "object", "properties": {**properties, "observation_ref": {"type": "string", "minLength": 1}, "offset": {"type": "integer", "minimum": 0}, "max_bytes": {"type": "integer", "minimum": 1, "maximum": 65536}}, "required": ["reference_ref", "path", "observation_ref"], "additionalProperties": False}, {"type": "object"}))
