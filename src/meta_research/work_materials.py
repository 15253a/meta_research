from __future__ import annotations

from dataclasses import dataclass
import json
import hashlib
import os
from pathlib import Path
import time
from sqlalchemy import text

from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json, new_ref
from meta_research.semantic_mcp import SemanticMcpError, SemanticOperation
from meta_research.work_material_contract import WORK_MATERIAL_OPERATION_IDS, CREATION_MATERIAL_OPERATION_IDS
from meta_research.material_processing import MaterialProcessingMixin, material_treatments, material_processing_operations


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
        if (not isinstance(receiver, dict) or receiver.get("kind") not in {"creation", "manual", "current", "request", "acquired"}
            or not isinstance(selections, list) or not 1 <= len(selections) <= 100
            or not isinstance(description, str) or len(description) > 4000):
            raise OwnerConflict("material_submission_invalid")
        return cls(receiver, tuple(selections), description)

    def as_dict(self):
        return {"receiver": self.receiver, "selections": list(self.selections), "description": self.description}


def material_reference(database, reference_ref):
    with database.read() as connection:
        row = connection.execute(text("SELECT r.*, s.receiver_json, s.command_json, s.command_hash, s.state, s.created_at, s.anchor_kind, s.anchor_ref "
            "FROM hc_work_material_references r JOIN hc_work_material_submissions s USING (submission_ref) "
            "WHERE r.reference_ref=:ref"), {"ref": reference_ref}).first()
        if row is None or row.state != "ready":
            raise OwnerConflict("material_reference_not_found")
        selection = json.loads(row.selection_json)
        command = json.loads(row.command_json)
        receiver = json.loads(row.receiver_json)
        if canonical_hash(selection) != row.selection_hash or canonical_hash(command) != row.command_hash or command["receiver"] != receiver:
            raise OwnerConflict("material_receipt_invalid")
        source_guidance = None
        if row.anchor_kind == "guidance":
            from meta_research.owners.human_collaboration_ladder import guidance_binding_from_row
            guide_row = connection.execute(text("SELECT * FROM hc_soft_constraints WHERE constraint_ref=:ref"),
                {"ref": row.anchor_ref}).first()
            if guide_row is None:
                raise OwnerConflict("material_receipt_invalid")
            binding = guidance_binding_from_row(guide_row)
            guidance = binding["guidance"]
            source_guidance = {
                "constraint_ref": binding["constraint_ref"], "guidance_hash": binding["guidance_hash"],
                **{key: guidance.get(key) for key in ("text", "assistant_understanding", "applies_to", "semantic_scope", "strength")},
                "preserve_conditions": guidance.get("preserve_conditions", []),
                "scope_confirmation": "confirmed" if guidance.get("semantic_scope") is not None else "legacy_unconfirmed",
            }
        accesses = connection.execute(text("SELECT result_json,actor FROM hc_work_material_accesses WHERE reference_ref=:ref ORDER BY created_at,access_ref"), {"ref": reference_ref}).all()
    facts = []
    for access in accesses:
        value = json.loads(access.result_json)
        actor = access.actor if value.get("reader_kind") == "root" else "browser" if value.get("reader_kind") == "browser" else None
        facts.append({**value, "actor": actor})
    ranges = [fact for fact in facts if fact["operation"] == "read" and "error" not in fact]
    return {"reference_ref": row.reference_ref, "submission_ref": row.submission_ref, "receiver": receiver,
            "source": selection, "description": command["description"], "registered_at": row.created_at,
            "availability": facts[-1].get("availability", "available") if facts else "available",
            "read_state": "read" if ranges else "not_read", "read_ranges": ranges,
            "failures": [fact for fact in facts if "error" in fact], "unexpanded": selection["kind"] == "directory",
            "reader": {"reference_ref": row.reference_ref}, "treatments": material_treatments(database, reference_ref=row.reference_ref),
            **({"source_guidance": source_guidance} if source_guidance is not None else {})}


def material_submission(database, submission_ref):
    with database.read() as connection:
        row = connection.execute(text("SELECT * FROM hc_work_material_submissions WHERE submission_ref=:ref"), {"ref": submission_ref}).first()
        if row is None or row.state != "ready":
            raise OwnerConflict("material_reference_not_found")
        references = connection.execute(text("SELECT reference_ref FROM hc_work_material_references WHERE submission_ref=:ref ORDER BY ordinal"), {"ref": submission_ref}).all()
    return {"submission_ref": submission_ref, "receiver": json.loads(row.receiver_json),
            "references": [material_reference(database, item.reference_ref) for item in references],
            "description": json.loads(row.command_json)["description"], "state": "saved", "created_at": row.created_at}


def material_submissions_for(database, anchor_kind, anchor_ref):
    with database.read() as connection:
        rows = connection.execute(text("SELECT submission_ref FROM hc_work_material_submissions WHERE anchor_kind=:kind AND anchor_ref=:anchor AND state='ready' ORDER BY created_at,submission_ref"), {"kind": anchor_kind, "anchor": anchor_ref}).all()
    return [material_submission(database, row.submission_ref) for row in rows]


class WorkMaterialsMixin(MaterialProcessingMixin):
    def creation_material_snapshot(self, anchor):
        from meta_research.creation_inputs import material_snapshot
        return material_snapshot(self, anchor)

    def begin_creation_material_operation(self, inputs, binding, operation_ref):
        from meta_research.creation_inputs import CreationMaterialOperation
        operation = CreationMaterialOperation(self, inputs, binding, operation_ref)
        if not hasattr(self, "_creation_material_operations"):
            self._creation_material_operations = {}
        self._creation_material_operations[operation_ref] = operation
        return operation

    def _creation_material_operation(self, context):
        operation = getattr(self, "_creation_material_operations", {}).get(context.run_ref)
        if operation is None:
            raise OwnerConflict("creation_material_scope_invalid")
        operation.authorize(context)
        return operation

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
        return material_submission(self._database, submission_ref)

    def query_work_materials(self, anchor_kind, anchor_ref):
        return material_submissions_for(self._database, anchor_kind, anchor_ref)

    def query_work_material(self, reference_ref):
        return material_reference(self._database, reference_ref)

    def _material_visible_workspaces(self, context):
        locations = self._root_workspaces._visible(context)
        visible = {location.workspace_ref for location in locations}
        if context.root_kind != "companion":
            return visible
        companion = self.query_companion_work_context(context.root_session_ref)
        if companion is None or not companion.get("quest_ref"):
            return visible
        quest_ref = companion["quest_ref"]
        if not any(location.quest_ref == quest_ref for location in locations):
            return visible
        # Only registered inputs gain visibility. Their frozen receiver stays the
        # original research root; generic workspace access is unchanged.
        with self._database.read() as connection:
            rows = connection.execute(text(
                "SELECT DISTINCT w.workspace_ref FROM hc_work_material_roots w "
                "JOIN hc_work_material_submissions s USING(submission_ref), "
                "json_each(s.receiver_json, '$.roots') root "
                "WHERE s.state='ready' AND json_extract(root.value, '$.workspace_ref')=w.workspace_ref "
                "AND json_extract(root.value, '$.quest_ref')=:quest"
            ), {"quest": quest_ref}).all()
        visible.update(row.workspace_ref for row in rows)
        return visible

    def _authorize_material(self, reference, context):
        if context is None:
            return
        if context.phase == "creation_materials":
            self._creation_material_operation(context).authorize(context, reference["reference_ref"])
            return
        visible = self._material_visible_workspaces(context)
        receiver = reference["receiver"]
        if not visible.intersection(root["workspace_ref"] for root in receiver.get("roots", [])):
            raise OwnerConflict("material_not_visible")

    def _record_material_access(self, reference_ref, actor, path, value):
        with self._database.fenced_write() as connection:
            connection.execute(text("INSERT INTO hc_work_material_accesses (access_ref,reference_ref,path,actor,result_json,created_at) VALUES (:ref,:reference,:path,:actor,:result,:now)"),
                {"ref": new_ref("material_access"), "reference": reference_ref, "path": path, "actor": actor,
                 "result": canonical_json(value), "now": time.time()})
            if value.get("operation") == "read" and "error" not in value:
                self._feed.record(connection, "human_collaboration.work_material_read", {"reference_ref": reference_ref,
                    "actor": actor if value.get("reader_kind") == "root" else "browser"})

    def discover_work_materials(self, *, reference_ref=None, path="", cursor=None, limit=50, context=None, actor="browser", offset=0):
        if reference_ref is None:
            if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
                raise OwnerConflict("material_page_invalid")
            if context is not None and context.phase == "creation_materials":
                operation = self._creation_material_operation(context)
                refs = operation.inputs.references[offset:offset + limit]
                return {"references": [operation.reference_view(item["reference_ref"]) for item in refs],
                        "next_offset": offset + limit if len(operation.inputs.references) > offset + limit else None,
                        "material_set_hash": operation.inputs.set_hash}
            visible = None if context is None else sorted(self._material_visible_workspaces(context))
            with self._database.read() as connection:
                query = "SELECT r.reference_ref FROM hc_work_material_references r JOIN hc_work_material_submissions s USING(submission_ref) WHERE s.state='ready'"
                params = {"offset": offset, "limit": limit + 1}
                if visible is not None:
                    query += " AND EXISTS (SELECT 1 FROM hc_work_material_roots w WHERE w.submission_ref=s.submission_ref AND w.workspace_ref IN (SELECT value FROM json_each(:visible)))"
                    params["visible"] = json.dumps(visible)
                rows = connection.execute(text(query + " ORDER BY s.created_at,r.reference_ref LIMIT :limit OFFSET :offset"), params).all()
            retained = []
            if context is not None:
                quest = self._material_scope(context).get("quest_ref")
                if quest:
                    retained = material_treatments(self._database, quest_ref=quest, offset=offset, limit=limit + 1, selected_only=True)
            return {"references": [self.query_work_material(row.reference_ref) for row in rows[:limit]],
                    "next_offset": offset + limit if len(rows) > limit or len(retained) > limit else None,
                    "retained_treatments": retained[:limit]}
        reference = self.query_work_material(reference_ref)
        self._authorize_material(reference, context)
        if context is not None and context.phase == "creation_materials":
            reference = self._creation_material_operation(context).reference_view(reference_ref)
        actor = context.run_ref if context is not None else actor
        try:
            result = self._root_workspaces.server_files.discover(reference["source"], actor=actor + ":" + reference_ref, path=path, cursor=cursor, limit=limit)
        except OwnerConflict as error:
            self._record_material_access(reference_ref, actor, path, {"operation": "discover", "error": error.code, "availability": error.code, "path": path})
            raise
        self._record_material_access(reference_ref, actor, path, {"operation": "discover", "availability": "available", "path": path})
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
        witness = None
        if context is not None and context.phase == "creation_materials":
            operation = self._creation_material_operation(context)
            witness = operation.witness(reference_ref, path, value)
        self._record_material_access(reference_ref, actor, path, {"operation": "read", "path": path,
            "reader_kind": "root" if context is not None else "browser",
            "observation_ref": value["observation"]["observation_ref"], "offset": offset, "bytes": value["bytes"],
            "eof": value["eof"], "availability": "available",
            **({"witness": witness.as_dict(), "fence_ref": operation.fence_ref} if witness else {})})
        return {"reference_ref": reference_ref, **value,
                **({"read_witness": witness.as_dict()} if witness else {})}

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
        kind, anchor = ("creation", initialization_id) if initialization_id is not None else ("manual", context_ref)
        self.material_receiver(kind, anchor)
        allowed = {ref["reference_ref"] for item in self.query_work_materials(kind, anchor) for ref in item["references"]}
        if arguments["reference_ref"] not in allowed:
            raise OwnerConflict("material_not_visible")
        return self.read_work_material(**arguments)

    def copy_creation_material(self, *, context, effect_id, reference_ref, path, observation_ref, max_bytes=1048576):
        from meta_research.creation_inputs import material_bytes
        from meta_research.material_processing import RuntimeMaterialOperation
        creation = context.phase == "creation_materials"
        operation = self._creation_material_operation(context) if creation else RuntimeMaterialOperation(self, context)
        operation.authorize(context, reference_ref)
        if type(max_bytes) is not int or not 1 <= max_bytes <= 8 * 1024**2:
            raise OwnerConflict("creation_copy_limit_invalid")
        request = {"reference_ref": reference_ref, "path": path,
                   "observation_ref": observation_ref, "max_bytes": max_bytes}
        key = context.effect_key(effect_id) if creation else self._material_effect_key(context, effect_id, "copy")
        request_hash = canonical_hash(request)
        with self._database.fenced_write() as connection:
            if creation:
                active = connection.execute(text("SELECT state,fence_ref FROM hc_creation_material_operations WHERE operation_ref=:ref"),
                    {"ref": operation.operation_ref}).one()
                if active.state != "active" or active.fence_ref != operation.fence_ref:
                    raise OwnerConflict("creation_material_scope_invalid")
            row = connection.execute(text("SELECT * FROM hc_creation_material_copies WHERE effect_key=:key"), {"key": key}).first()
            if row is not None:
                if row.request_hash != request_hash:
                    raise OwnerConflict("creation_copy_identity_conflict")
                return self.reconcile_creation_copy(context=context, effect_id=effect_id)
            connection.execute(text("INSERT INTO hc_creation_material_copies "
                "(effect_key,operation_ref,fence_ref,request_hash,state) VALUES (:key,:operation,:fence,:hash,'pending')"),
                {"key": key, "operation": operation.operation_ref, "fence": operation.fence_ref, "hash": request_hash})
        try:
            reference = self.query_work_material(reference_ref)
            with self._root_workspaces.server_files._open_reference(reference["source"], path) as (_, observed):
                if observed["kind"] != "file" or observed["observation_ref"] != observation_ref:
                    raise OwnerConflict("material_source_changed")
                if int(observed["size"]) > max_bytes:
                    raise OwnerConflict("creation_copy_too_large")
            suffix = Path(path or reference["source"]["absolute_path"]).suffix
            if len(suffix) > 24 or not all(character.isalnum() or character == "." for character in suffix):
                suffix = ""
            digest, witnesses, offset = hashlib.sha256(), [], 0
            with operation.work.new_copy(key, suffix) as (stream, name):
                while True:
                    page = self.read_work_material(context=context, reference_ref=reference_ref,
                        path=path, observation_ref=observation_ref, offset=offset, max_bytes=min(65536, max_bytes - offset + 1))
                    content = material_bytes(page)
                    offset += len(content)
                    if offset > max_bytes:
                        raise OwnerConflict("creation_copy_too_large")
                    stream.write(content)
                    digest.update(content)
                    witnesses.append(page.get("read_witness") or {"reference_ref": reference_ref, "path": path,
                        "observation_ref": observation_ref, "offset": page["offset"], "length": len(content), "actor": context.run_ref})
                    if page["eof"]:
                        break
                    if not content:
                        raise OwnerConflict("material_source_changed")
                details = os.fstat(stream.fileno())
                receipt = {"state": "ready", "effect_key": key,
                    "work_file": {"kind": "work_file", "work_ref": operation.work.work_ref, "path": name, "trial_ref": key},
                    "working_path": ("/workspace/" + operation.work.relative_directory + "/" + name if creation
                        else str(operation.work.location.directory / name)),
                    "source": {"kind": "original_file", "reference_ref": reference_ref, "path": path, "observation_ref": observation_ref},
                    "bytes": offset, "sha256": digest.hexdigest(), "read_witnesses": witnesses,
                    "device": details.st_dev, "inode": details.st_ino}
            operation.authorize(context, reference_ref)
            with self._database.fenced_write() as connection:
                connection.execute(text("UPDATE hc_creation_material_copies SET state='ready',receipt_json=:receipt WHERE effect_key=:key AND state='pending'"),
                    {"key": key, "receipt": canonical_json(receipt)})
            return receipt
        except BaseException:
            with self._database.fenced_write() as connection:
                connection.execute(text("UPDATE hc_creation_material_copies SET state='failed' WHERE effect_key=:key AND state='pending'"), {"key": key})
            raise

    def reconcile_creation_copy(self, *, context, effect_id):
        from meta_research.material_processing import RuntimeMaterialOperation
        creation = context.phase == "creation_materials"
        operation = self._creation_material_operation(context) if creation else RuntimeMaterialOperation(self, context)
        key = context.effect_key(effect_id) if creation else self._material_effect_key(context, effect_id, "copy")
        with self._database.read() as connection:
            row = connection.execute(text("SELECT * FROM hc_creation_material_copies WHERE effect_key=:key"), {"key": key}).first()
        if row is None:
            return {"state": "absent"}
        if row.operation_ref != operation.operation_ref or row.fence_ref != operation.fence_ref:
            raise OwnerConflict("creation_material_scope_invalid")
        if row.state != "ready":
            return {"state": row.state}
        receipt = json.loads(row.receipt_json)
        operation.work.copy_receipt_available(receipt)
        return receipt


def material_operations(human):
    def call(context, arguments):
        try:
            if context.operation_id == WORK_MATERIAL_OPERATION_IDS[0]:
                return human.discover_work_materials(context=context, **arguments)
            if context.operation_id == WORK_MATERIAL_OPERATION_IDS[1]:
                return human.read_work_material(context=context, **arguments)
            if context.operation_id == CREATION_MATERIAL_OPERATION_IDS[2]:
                return human.copy_creation_material(context=context, **arguments)
            return human.reconcile_creation_copy(context=context, **arguments)
        except OwnerConflict as error:
            raise SemanticMcpError(error.code) from error
    properties = {"reference_ref": {"type": "string", "maxLength": 96}, "path": {"type": "string", "maxLength": 4096}}
    return (*material_processing_operations(human), SemanticOperation(WORK_MATERIAL_OPERATION_IDS[0], "human_collaboration",
        "Discover saved work material references visible to this actual root and eligible earlier work. A registered Quest Companion can read submitted inputs for the same Quest while their original receiver remains unchanged. Expand one original server directory at a time without preloading, hashing, copying or formal RM intake. Entries and unexpanded scope do not prove reading or understanding. Use observation_ref from an entry for a bounded read.", call,
        {"type": "object", "properties": {**properties, "cursor": {"type": "string"}, "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, "additionalProperties": False}, {"type": "object"}),
        SemanticOperation(WORK_MATERIAL_OPERATION_IDS[1], "human_collaboration",
        "Read at most 65536 requested bytes from a saved server material reference with its discovered observation_ref. Recheck original source identity and current root visibility. Returns UTF-8 or base64, exact byte interval and EOF; records successful ranges and failures without claiming comprehension. Changed originals require fresh discovery. No RM binding is required.", call,
        {"type": "object", "properties": {**properties, "observation_ref": {"type": "string", "minLength": 1}, "offset": {"type": "integer", "minimum": 0}, "max_bytes": {"type": "integer", "minimum": 1, "maximum": 65536}}, "required": ["reference_ref", "path", "observation_ref"], "additionalProperties": False}, {"type": "object"}),
        SemanticOperation(CREATION_MATERIAL_OPERATION_IDS[2], "human_collaboration",
        "Copy one explicitly requested observed original file into independent editable work using the shared observed-file copier. Research roots write under .work-materials, outside Target's automatic output custody. Creation operations keep their isolated work. Reads bounded original ranges; default 1MiB, maximum8MiB; no directory recursion. Retry unchanged effect_id or reconcile; edited copies are never overwritten. Neither copies nor new outputs are automatically retained.", call,
        {"type": "object", "properties": {**properties, "observation_ref": {"type": "string", "minLength": 1}, "effect_id": {"type": "string", "minLength": 1, "maxLength": 128}, "max_bytes": {"type": "integer", "minimum": 1, "maximum": 8388608}},
         "required": ["reference_ref", "path", "observation_ref", "effect_id"], "additionalProperties": False}, {"type": "object"}, "effect", CREATION_MATERIAL_OPERATION_IDS[3]),
        SemanticOperation(CREATION_MATERIAL_OPERATION_IDS[3], "human_collaboration",
        "Read this creation operation's exact copy receipt. Pending or failed copies remain explicit; no implicit retry or replacement of edited work.", call,
        {"type": "object", "properties": {"effect_id": {"type": "string", "minLength": 1, "maxLength": 128}}, "required": ["effect_id"], "additionalProperties": False}, {"type": "object"}, "reconcile"))
