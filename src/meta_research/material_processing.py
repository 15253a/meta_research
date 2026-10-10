"""Receiving roots declare material use; RM and RG retain their authorities."""
import json
import time
import hashlib
import mimetypes
import os
from pathlib import Path
import stat
from contextlib import contextmanager

from sqlalchemy import text

from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json
from meta_research.semantic_mcp import SemanticMcpError, SemanticOperation
from meta_research.work_material_contract import PROCESSING_OPERATION_IDS


def material_treatments(database, *, reference_ref=None, quest_ref=None, offset=0, limit=50, selected_only=False):
    with database.read() as connection:
        rows = connection.execute(text("SELECT result_json FROM hc_material_processing_effects "
            "WHERE action='feedback' AND state='ready' "
            "AND (:reference IS NULL OR reference_ref=:reference) "
            "AND (:quest IS NULL OR quest_ref=:quest) "
            "AND (:selected=0 OR json_array_length(result_json,'$.selections')>0) "
            "ORDER BY created_at,effect_key LIMIT :limit OFFSET :offset"),
            {"reference": reference_ref, "quest": quest_ref, "offset": offset, "limit": limit, "selected": selected_only}).all()
    return [json.loads(row.result_json) for row in rows]


class MaterialProcessingMixin:
    def _reserve_material_effect(self, context, key, action, command, reference_ref=None):
        facts = self._material_scope(context)
        with self._database.fenced_write() as connection:
            self._material_scope(context)
            connection.execute(text("INSERT INTO hc_material_processing_effects "
                "(effect_key,action,root_kind,run_ref,root_session_ref,quest_ref,reference_ref,"
                "command_hash,command_json,state,created_at) VALUES "
                "(:key,:action,:kind,:run,:session,:quest,:reference,:hash,:command,'pending',:now) "
                "ON CONFLICT(effect_key) DO NOTHING"),
                {"key": key, "action": action, "kind": context.root_kind, "run": context.run_ref,
                 "session": context.root_session_ref, "quest": facts.get("quest_ref"), "reference": reference_ref,
                 "hash": canonical_hash(command), "command": canonical_json(command), "now": time.time()})
        return self._material_effect(key, command)

    def _complete_material_effect(self, context, key, result, event):
        with self._database.fenced_write() as connection:
            self._material_scope(context)
            changed = connection.execute(text("UPDATE hc_material_processing_effects SET state='ready',result_json=:result "
                "WHERE effect_key=:key AND state='pending'"), {"key": key, "result": canonical_json(result)})
            if changed.rowcount:
                self._feed.record(connection, event, {"reference_ref": result.get("reference_ref"),
                    "feedback_ref": key, "quest_ref": result.get("processed_by", {}).get("quest_ref")})
        return self._material_effect(key)

    def _material_scope(self, context, *, reconcile=False):
        verifier = (self._agent_runtime.verify_root_agent_human_request_reconcile_scope
            if reconcile else self._agent_runtime.verify_root_agent_runtime_scope)
        return verifier(root_kind=context.root_kind, run_ref=context.run_ref,
            attempt_ref=context.attempt_ref, root_session_ref=context.root_session_ref,
            fence_ref=context.fence_ref, runtime_binding_hash=context.capability_binding_hash)

    def _material_effect_key(self, context, effect_id, action):
        if not isinstance(effect_id, str) or not 1 <= len(effect_id) <= 128:
            raise OwnerConflict("material_effect_id_invalid")
        return "material_effect:" + canonical_hash({"root_kind": context.root_kind,
            "run_ref": context.run_ref, "root_session_ref": context.root_session_ref,
            "action": action, "effect_id": effect_id})

    def _material_effect(self, key, command=None):
        with self._database.read() as connection:
            row = connection.execute(text("SELECT * FROM hc_material_processing_effects WHERE effect_key=:key"), {"key": key}).first()
        if row is None:
            return None
        if command is not None and row.command_hash != canonical_hash(command):
            raise OwnerConflict("material_effect_conflict")
        return ({"state": row.state} if row.state != "ready" else json.loads(row.result_json))

    def feedback_work_material(self, *, context, effect_id, reference_ref=None,
            understanding=None, disposition=None, changes=None, continuing_work=None,
            reasons=None, limitations=None, selections=None, reconcile=False):
        self._material_scope(context, reconcile=reconcile)
        key = self._material_effect_key(context, effect_id, "feedback")
        command = None if reconcile else {"reference_ref": reference_ref,
            "understanding": understanding, "disposition": disposition, "changes": changes,
            "continuing_work": continuing_work, "reasons": reasons, "limitations": limitations,
            "selections": selections}
        replay = self._material_effect(key, command)
        if replay is not None and (reconcile or replay.get("state") != "pending"):
            return replay
        if reconcile:
            return {"state": "absent"}
        if (any(not isinstance(value, str) or not value.strip() or len(value) > 8192
                for value in (understanding, changes, continuing_work, reasons, limitations))
            or disposition not in {"adopted", "considered", "deferred", "not_used"}
            or not isinstance(selections, list) or len(selections) > 100):
            raise OwnerConflict("material_feedback_invalid")
        reference = self.query_work_material(reference_ref)
        self._authorize_material(reference, context)
        if disposition == "adopted" and not any(item.get("actor") == context.run_ref for item in reference["read_ranges"]):
            raise OwnerConflict("material_root_read_required")
        location = self._root_workspaces.bind_runtime(context).location
        validated = [self._selected_material_source(context, reference, selected) for selected in selections]
        self._reserve_material_effect(context, key, "feedback", command, reference_ref)
        guidance_context = ({"source_guidance": reference["source_guidance"]}
            if "source_guidance" in reference else {})
        retained = []
        for ordinal, (selected, locator, expected_hash) in enumerate(validated):
            from meta_research.owners.research_memory import AssetIntakeRequest
            if not location.quest_ref:
                raise OwnerConflict("content_quest_scope_required")
            def effect_scope():
                self._material_scope(context)
                self._authorize_material(reference, context)
                self._selected_material_source(context, reference, selected)
            intake_key = "material-selected:" + canonical_hash({"effect": key, "ordinal": ordinal})
            accepted = self._research_memory.submit_asset_intake(AssetIntakeRequest(
                source_kind="local_path", custody_mode=selected["custody"], display_name=Path(locator).name,
                source_locator=locator, media_type=mimetypes.guess_type(locator)[0] or "application/octet-stream",
                provenance={"kind": "work_material_selection", "reference_ref": reference_ref,
                    "receiver": reference["receiver"], "source": selected["source"], "purpose": selected["purpose"],
                    "processed_by": location.source(), "feedback_ref": key, **guidance_context},
                origin_quest_ref=location.quest_ref, asynchronous=True, effect_scope_required=True),
                idempotency_key=intake_key, effect_scope=effect_scope)
            if accepted.status == "queued":
                self._research_memory._process_asset_job(accepted.job_ref, effect_scope=effect_scope)
                accepted = self._research_memory.query_asset_intake(accepted.job_ref)
            if accepted.status != "accepted" or accepted.asset is None:
                raise OwnerConflict("material_selection_not_accepted")
            if expected_hash is not None and accepted.asset.content_hash != expected_hash:
                raise OwnerConflict("workspace_content_changed")
            binding = accepted.asset.as_binding()
            role = self._research_graph.accept_asset_role(binding=binding, role="quest_source_material",
                quest_ref=location.quest_ref, idempotency_key="material-role:" + canonical_hash({"effect": key, "ordinal": ordinal}),
                effect_scope=effect_scope)
            retained.append({**selected, "asset_binding": binding.as_dict(), "role_ref": role.role_ref,
                "reader": {"source_ref": binding.version_ref, "version_ref": binding.version_ref}})
        result = {**command, "feedback_ref": key, "receiver": reference["receiver"],
            "processed_by": {**location.source(), "run_ref": context.run_ref}, "declared_by_root": True,
            "selections": retained, "created_at": time.time(), **guidance_context}
        return self._complete_material_effect(context, key, result, "human_collaboration.work_material_treated")

    def _selected_material_source(self, context, reference, selected):
        from meta_research.server_materials import relative_parts
        if (not isinstance(selected, dict) or set(selected) != {"source", "custody", "purpose"}
            or selected["custody"] not in {"managed", "linked_local"}
            or not isinstance(selected["purpose"], str) or not selected["purpose"].strip() or len(selected["purpose"]) > 8192):
            raise OwnerConflict("material_selection_invalid")
        source = selected["source"]
        if not isinstance(source, dict):
            raise OwnerConflict("material_selection_invalid")
        expected = None
        if source.get("kind") == "original_file" and set(source) == {"kind", "path", "observation_ref"}:
            relative_parts(source["path"])
            with self._root_workspaces.server_files._open_reference(reference["source"], source["path"]) as (_, observed):
                if observed["observation_ref"] != source["observation_ref"]:
                    raise OwnerConflict("material_source_changed")
            locator = str(Path(reference["source"]["absolute_path"]).joinpath(*relative_parts(source["path"])))
        elif source.get("kind") == "workspace_file" and set(source) == {"kind", "workspace_ref", "path", "expected_sha256"}:
            parts = relative_parts(source["path"])
            if not parts or any(part.startswith(".delivery-") or part == ".delivery.json" for part in parts) or parts[0] == "inputs":
                raise OwnerConflict("workspace_file_unsafe")
            location = next((item for item in self._root_workspaces._visible(context)
                if item.workspace_ref == source["workspace_ref"]), None)
            if location is None:
                raise OwnerConflict("workspace_not_visible")
            locator = str(location.directory.joinpath(*parts))
            with self._root_workspaces.server_files._open(locator) as (descriptor, observed):
                if observed["kind"] != "file":
                    raise OwnerConflict("material_selection_invalid")
                with os.fdopen(os.dup(descriptor), "rb") as stream:
                    expected = hashlib.file_digest(stream, "sha256").hexdigest()
                if expected != source["expected_sha256"]:
                    raise OwnerConflict("workspace_content_changed")
        else:
            raise OwnerConflict("material_selection_invalid")
        return selected, locator, expected

    def acquire_work_material(self, *, context, effect_id, selection=None, absolute_path=None, description=None, reconcile=False):
        self._material_scope(context, reconcile=reconcile)
        key = self._material_effect_key(context, effect_id, "acquire")
        command = None if reconcile else {"selection": selection, "absolute_path": absolute_path, "description": description}
        replay = self._material_effect(key, command)
        if replay is not None and (reconcile or replay.get("state") != "pending"):
            return replay
        if reconcile:
            return {"state": "absent"}
        if not isinstance(description, str) or not description.strip() or len(description) > 4000:
            raise OwnerConflict("material_acquisition_description_invalid")
        if (selection is None) == (absolute_path is None):
            raise OwnerConflict("material_acquisition_source_invalid")
        if absolute_path is not None:
            selection = self._root_workspaces.server_files.inspect(absolute_path, description=description)
        self._root_workspaces.server_files.validate_selection(selection)
        location = self._root_workspaces.bind_runtime(context).location
        receiver = {"kind": "acquired", "quest_ref": location.quest_ref, "roots": [location.source()]}
        submission = {"receiver": receiver, "selections": [selection], "description": description}
        self._reserve_material_effect(context, key, "acquire", command)
        with self._database.fenced_write() as connection:
            self._material_scope(context)
            ref = self._insert_material_submission(connection, command=submission, key=key,
                anchor_kind="acquired", anchor_ref=context.run_ref, validate_receiver=False)
        result = self.query_material_submission(ref)
        return self._complete_material_effect(context, key, result, "human_collaboration.work_materials_submitted")


class RuntimeMaterialOperation:
    """Adapt the common observed-file copy to an authenticated research work."""
    def __init__(self, human, context):
        self.human, self.context = human, context
        self.operation_ref = context.run_ref
        self.fence_ref = canonical_hash({"run_ref": context.run_ref, "root_session_ref": context.root_session_ref})
        self.work = RuntimeMaterialWork(human._root_workspaces.bind_runtime(context).location)

    def authorize(self, context, reference_ref):
        self.human._material_scope(context)
        self.human._authorize_material(self.human.query_work_material(reference_ref), context)


class RuntimeMaterialWork:
    def __init__(self, location):
        self.location = location
        self.work_ref = location.work_ref
        self.relative_directory = ".work-materials"

    @contextmanager
    def new_copy(self, effect_key, suffix):
        from meta_research.creation_work import owned_directory
        with owned_directory(self.location.directory) as root:
            try:
                os.mkdir(self.relative_directory, 0o700, dir_fd=root)
            except FileExistsError:
                pass
            directory = os.open(self.relative_directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
            name = canonical_hash({"effect_key": effect_key}) + suffix
            try:
                descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                try:
                    with os.fdopen(descriptor, "wb") as stream:
                        yield stream, self.relative_directory + "/" + name
                        stream.flush()
                        os.fsync(stream.fileno())
                        if os.fstat(stream.fileno()).st_nlink != 1:
                            raise OwnerConflict("workspace_file_unsafe")
                except BaseException:
                    os.unlink(name, dir_fd=directory)
                    raise
            except OSError as error:
                raise OwnerConflict("workspace_file_unsafe") from error
            finally:
                os.close(directory)

    def copy_receipt_available(self, receipt):
        from meta_research.server_materials import ServerFiles
        path = self.location.directory / receipt["work_file"]["path"]
        with ServerFiles()._open(str(path)) as (_, details):
            if (int(details["device"]), int(details["inode"])) != (receipt["device"], receipt["inode"]):
                raise OwnerConflict("workspace_content_changed")


def material_processing_operations(human):
    strings = {key: {"type": "string", "minLength": 1, "maxLength": 8192}
        for key in ("understanding", "changes", "continuing_work", "reasons", "limitations")}
    effect = {"effect_id": {"type": "string", "minLength": 1, "maxLength": 128}}
    properties = {**effect, **strings, "reference_ref": {"type": "string", "minLength": 1, "maxLength": 96},
        "disposition": {"type": "string", "enum": ["adopted", "considered", "deferred", "not_used"]},
        "selections": {"type": "array", "maxItems": 100, "items": {"type": "object",
            "properties": {"source": {"type": "object", "properties": {
                "kind": {"type": "string", "enum": ["original_file", "workspace_file"]},
                "path": {"type": "string"}, "observation_ref": {"type": "string", "minLength": 1},
                "workspace_ref": {"type": "string"}, "expected_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"}},
                "required": ["kind", "path"], "additionalProperties": False},
                "custody": {"type": "string", "enum": ["managed", "linked_local"]},
                "purpose": {"type": "string", "minLength": 1, "maxLength": 8192}},
            "required": ["source", "custody", "purpose"], "additionalProperties": False}}}
    def call(context, arguments):
        try:
            method = human.acquire_work_material if ".acquire" in context.operation_id else human.feedback_work_material
            return method(context=context,
                reconcile=context.operation_id.endswith(".reconcile"), **arguments)
        except OwnerConflict as error:
            raise SemanticMcpError(error.code) from error
    return (
        SemanticOperation(PROCESSING_OPERATION_IDS[0], "human_collaboration",
            "Declare the material's actual understanding, adopted/considered/deferred/not_used disposition, "
            "research changes, continuing work, reasons and limitations. Receipt and reading do not prove scientific support. "
            "Choose original_file (relative path plus observation_ref) and/or workspace_file (workspace_ref,path,expected_sha256) "
            "with custody managed or linked_local and actual purpose. Selected content passes existing RM intake and RG source-material "
            "role acceptance; the receipt supplies exact content readers and use. An empty list retains neither. "
            "Adopted requires this root's actual read; the declaration itself proves no conclusion. Retry unchanged effect_id or reconcile.",
            call, {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False},
            {"type": "object"}, "effect", PROCESSING_OPERATION_IDS[1]),
        SemanticOperation(PROCESSING_OPERATION_IDS[1], "human_collaboration",
            "Recover this root's exact committed material feedback without rebinding its original receiver.",
            call, {"type": "object", "properties": effect, "required": ["effect_id"], "additionalProperties": False},
            {"type": "object"}, "reconcile"),
        SemanticOperation(PROCESSING_OPERATION_IDS[2], "human_collaboration",
            "Register an Agent-acquired observed server source as readable work material for this actual root. "
            "Supply absolute_path for the existing safe server inspector, or a complete inspected ServerSelection, and explain "
            "the real acquisition route. The server derives observations; do not fabricate them. No HumanRequest, "
            "foreground rerouting, RM intake, recursive reading or copying occurs.", call,
            {"type": "object", "properties": {**effect, "selection": {"type": "object"}, "absolute_path": {"type": "string", "maxLength": 16000},
                "description": {"type": "string", "minLength": 1, "maxLength": 4000}},
             "required": ["effect_id", "description"], "additionalProperties": False},
            {"type": "object"}, "effect", PROCESSING_OPERATION_IDS[3]),
        SemanticOperation(PROCESSING_OPERATION_IDS[3], "human_collaboration",
            "Recover this root's original acquired material receipt without rechecking or rebinding its source.", call,
            {"type": "object", "properties": effect, "required": ["effect_id"], "additionalProperties": False},
            {"type": "object"}, "reconcile"),
    )
