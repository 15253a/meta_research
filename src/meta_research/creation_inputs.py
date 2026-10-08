from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import secrets
import time
from typing import Literal

from sqlalchemy import text

from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json


@dataclass(frozen=True)
class CreationAnchor:
    kind: Literal["quest_initialization", "manual_question_creation"]
    ref: str
    generation: int | None
    draft_revision: int
    draft_hash: str

    def as_dict(self):
        return {"kind": self.kind, "ref": self.ref, "generation": self.generation,
                "draft_revision": self.draft_revision, "draft_hash": self.draft_hash}

    @property
    def receiver_kind(self):
        return "creation" if self.kind == "quest_initialization" else "manual"


@dataclass(frozen=True)
class MaterialSet:
    anchor: CreationAnchor
    references: tuple[dict[str, object], ...]

    @property
    def set_hash(self):
        return canonical_hash(list(self.references))

    def as_dict(self):
        return {"anchor": self.anchor.as_dict(), "references": list(self.references),
                "set_hash": self.set_hash}


@dataclass(frozen=True)
class ReadWitness:
    witness_ref: str
    operation_ref: str
    reference_ref: str
    path: str
    observation_ref: str
    offset: int
    length: int
    chunk_sha256: str

    def as_dict(self):
        return {"witness_ref": self.witness_ref, "operation_ref": self.operation_ref,
                "reference_ref": self.reference_ref, "path": self.path,
                "observation_ref": self.observation_ref, "offset": self.offset,
                "length": self.length, "chunk_sha256": self.chunk_sha256}


@dataclass(frozen=True)
class OriginalFile:
    reference_ref: str
    path: str
    observation_ref: str

    def as_dict(self):
        return {"kind": "original_file", "reference_ref": self.reference_ref,
                "path": self.path, "observation_ref": self.observation_ref}


@dataclass(frozen=True)
class WorkFile:
    work_ref: str
    path: str
    trial_ref: str | None = None

    def as_dict(self):
        return {"kind": "work_file", "work_ref": self.work_ref,
                "path": self.path, "trial_ref": self.trial_ref}


@dataclass(frozen=True)
class Selection:
    source: OriginalFile | WorkFile
    custody: Literal["managed", "linked_local"]
    reason: str

    def as_dict(self):
        return {"source": self.source.as_dict(), "custody": self.custody,
                "reason": self.reason}


@dataclass(frozen=True)
class CreationInputIdentity:
    anchor: CreationAnchor
    material_set_hash: str
    consumed: tuple[ReadWitness, ...]

    @classmethod
    def from_dict(cls, value):
        return cls(CreationAnchor(**value["anchor"]), value["material_set_hash"],
                   tuple(ReadWitness(**item) for item in value["consumed"]))

    def as_dict(self):
        return {"anchor": self.anchor.as_dict(), "material_set_hash": self.material_set_hash,
                "consumed": [item.as_dict() for item in self.consumed]}

    @property
    def digest(self):
        return canonical_hash(self.as_dict())


def material_bytes(page):
    return (base64.b64decode(page["base64"]) if page["encoding"] == "base64"
            else page["text"].encode("utf-8"))


def material_snapshot(human, anchor):
    references = [
        {"reference_ref": item["reference_ref"], "submission_ref": item["submission_ref"],
         "source": item["source"], "description": item["description"]}
        for submission in human.query_work_materials(anchor.receiver_kind, anchor.ref)
        for item in submission["references"]
    ]
    return MaterialSet(anchor, tuple(sorted(references, key=lambda item: item["reference_ref"])))


def require_anchor(human, anchor, *, require_open=False):
    with human._database.read() as connection:
        if anchor.kind == "quest_initialization":
            row = human._require_initialization(connection, anchor.ref)
            current = row.draft_revision, row.draft_hash
            expected = anchor.draft_revision, anchor.draft_hash
            closed = row.status in {"completed", "cancelled"}
        else:
            row = human._manual_creation._require_context(connection, anchor.ref)
            current, expected = row.generation, anchor.generation
            closed = row.terminal_decision is not None
    if current != expected:
        raise OwnerConflict("creation_input_stale")
    if require_open and closed:
        raise OwnerConflict("material_receiver_closed")


def require_identity(human, identity):
    anchor = identity.anchor
    require_anchor(human, anchor)
    inputs = material_snapshot(human, anchor)
    if inputs.set_hash != identity.material_set_hash:
        raise OwnerConflict("creation_input_stale")
    references = {item["reference_ref"]: item for item in inputs.references}
    for witness in identity.consumed:
        reference = references.get(witness.reference_ref)
        if reference is None:
            raise OwnerConflict("creation_input_stale")
        try:
            page = human._root_workspaces.server_files.read(reference["source"],
                path=witness.path, observation_ref=witness.observation_ref,
                offset=witness.offset, max_bytes=max(1, witness.length))
        except OwnerConflict as error:
            raise OwnerConflict("creation_input_stale") from error
        content = material_bytes(page)
        if len(content) != witness.length or hashlib.sha256(content).hexdigest() != witness.chunk_sha256:
            raise OwnerConflict("creation_input_stale")
    return inputs


class CreationMaterialOperation:
    def __init__(self, human, inputs, binding, operation_ref):
        self.human = human
        self.inputs = inputs
        self.binding = binding
        self.operation_ref = operation_ref
        self.fence_ref = "creation_fence:" + secrets.token_hex(16)
        self.binding_hash = canonical_hash({"inputs": inputs.as_dict(), "workspace": binding.seal(),
                                           "operation_ref": operation_ref, "fence_ref": self.fence_ref})
        location = binding.location
        anchor = inputs.anchor
        if (location.root_kind != "companion" or not location.root_session_ref
            or anchor.kind == "quest_initialization" and
                (location.initialization_id != anchor.ref or location.context_generation is not None)
            or anchor.kind == "manual_question_creation" and
                (location.request_ref != anchor.ref or location.context_generation != anchor.generation)):
            raise OwnerConflict("creation_material_scope_invalid")
        with human._database.fenced_write() as connection:
            require_anchor(human, inputs.anchor, require_open=True)
            if material_snapshot(human, inputs.anchor) != inputs:
                raise OwnerConflict("creation_input_stale")
            existing = connection.execute(text("SELECT * FROM hc_creation_material_operations WHERE operation_ref=:ref"),
                                          {"ref": operation_ref}).first()
            if existing is not None:
                if (json.loads(existing.material_set_json) != inputs.as_dict()
                    or existing.workspace_ref != binding.location.workspace_ref
                    or existing.root_session_ref != binding.location.root_session_ref):
                    raise OwnerConflict("creation_operation_identity_invalid")
                if existing.state != "active":
                    raise OwnerConflict("protected_creation_unknown_outcome")
                self.fence_ref = existing.fence_ref
                self.binding_hash = canonical_hash({"inputs": inputs.as_dict(), "workspace": binding.seal(),
                    "operation_ref": operation_ref, "fence_ref": self.fence_ref})
                if self.binding_hash != existing.binding_hash:
                    raise OwnerConflict("creation_operation_identity_invalid")
                return
            connection.execute(text("INSERT INTO hc_creation_material_operations "
                "(operation_ref,fence_ref,binding_hash,workspace_ref,root_session_ref,anchor_json,"
                "material_set_json,material_set_hash,state,created_at) "
                "VALUES (:operation,:fence,:binding,:workspace,:session,:anchor,:inputs,:hash,'active',:now)"),
                {"operation": operation_ref, "fence": self.fence_ref, "binding": self.binding_hash,
                 "workspace": binding.location.workspace_ref, "session": binding.location.root_session_ref,
                 "anchor": canonical_json(inputs.anchor.as_dict()), "inputs": canonical_json(inputs.as_dict()),
                 "hash": inputs.set_hash, "now": time.time()})

    def authorize(self, context, reference_ref=None):
        with self.human._database.read() as connection:
            row = connection.execute(text("SELECT * FROM hc_creation_material_operations WHERE operation_ref=:ref"),
                                     {"ref": self.operation_ref}).first()
        if (row is None or row.state != "active" or context.run_ref != self.operation_ref
            or context.attempt_ref != self.operation_ref or context.fence_ref != row.fence_ref
            or context.capability_binding_hash != row.binding_hash
            or context.root_session_ref != row.root_session_ref or context.root_kind != "companion"
            or context.phase != "creation_materials"):
            raise OwnerConflict("creation_material_scope_invalid")
        require_anchor(self.human, self.inputs.anchor, require_open=True)
        if material_snapshot(self.human, self.inputs.anchor).set_hash != self.inputs.set_hash:
            raise OwnerConflict("creation_input_stale")
        if reference_ref is not None and reference_ref not in {item["reference_ref"] for item in self.inputs.references}:
            raise OwnerConflict("material_not_visible")

    def witness(self, reference_ref, path, page):
        content = material_bytes(page)
        return ReadWitness("read_witness:" + secrets.token_hex(16), self.operation_ref,
            reference_ref, path, page["observation"]["observation_ref"], page["offset"],
            len(content), hashlib.sha256(content).hexdigest())

    def identity(self):
        with self.human._database.read() as connection:
            rows = connection.execute(text("SELECT result_json FROM hc_work_material_accesses "
                "WHERE actor=:actor ORDER BY created_at,access_ref"), {"actor": self.operation_ref}).all()
        consumed = []
        for row in rows:
            value = json.loads(row.result_json)
            if value.get("fence_ref") == self.fence_ref and value.get("witness"):
                consumed.append(ReadWitness(**value["witness"]))
        return CreationInputIdentity(self.inputs.anchor, self.inputs.set_hash, tuple(consumed))

    def reference_view(self, reference_ref):
        reference = self.human.query_work_material(reference_ref)
        consumed = [item.as_dict() for item in self.identity().consumed
                    if item.reference_ref == reference_ref]
        return {**reference, "read_state": "read" if consumed else "not_read",
                "read_ranges": consumed, "failures": []}

    def seal(self):
        identity = self.identity()
        with self.human._database.fenced_write() as connection:
            require_anchor(self.human, self.inputs.anchor, require_open=True)
            require_identity(self.human, identity)
            changed = connection.execute(text("UPDATE hc_creation_material_operations SET state='sealed',"
                "identity_json=:identity WHERE operation_ref=:ref AND fence_ref=:fence AND state='active'"),
                {"identity": canonical_json(identity.as_dict()), "ref": self.operation_ref, "fence": self.fence_ref})
            if changed.rowcount != 1:
                raise OwnerConflict("creation_material_scope_invalid")
        return identity

    def fail(self, *, unknown_outcome=False):
        with self.human._database.fenced_write() as connection:
            connection.execute(text("UPDATE hc_creation_material_operations SET state=:state "
                "WHERE operation_ref=:ref AND fence_ref=:fence AND state='active'"),
                {"state": "unknown_outcome" if unknown_outcome else "failed",
                 "ref": self.operation_ref, "fence": self.fence_ref})
