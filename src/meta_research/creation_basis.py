from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sqlalchemy import text

from meta_research.context_presentation import context_read_page
from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json
from meta_research.semantic_mcp import SemanticMcpError, SemanticOperation


UNDERSTANDING_FIELDS = (
    "material_composition", "work_already_done", "claims_and_conditions",
    "conflicts", "gaps", "unfinished_questions",
)


@dataclass(frozen=True)
class InitializationUnderstandingRequest:
    initialization_id: str
    draft_revision: int
    draft_hash: str
    draft: dict[str, object]
    manifest: dict[str, object]
    job_ref: str
    root_session_ref: str
    companion_native_session_ref: str | None
    creation_context_kind: str = "quest_initialization"
    creation_context_ref: str | None = None
    context_generation: int | None = None


@dataclass(frozen=True)
class InitializationUnderstandingResult:
    understanding: dict[str, object]
    companion_native_session_ref: str | None = None


@dataclass(frozen=True)
class FirstQuestionSynthesisRequest:
    initialization_id: str
    draft_revision: int
    draft_hash: str
    draft: dict[str, object]
    job_ref: str
    root_session_ref: str
    companion_native_session_ref: str | None
    basis: dict[str, object]
    context: dict[str, object]
    literature_snapshot: dict[str, object] | None
    creation_context_kind: str = "quest_initialization"
    creation_context_ref: str | None = None
    context_generation: int | None = None


@dataclass(frozen=True)
class FirstQuestionSynthesisResult:
    content: dict[str, str]
    adapter_kind: str
    companion_native_session_ref: str | None
    proposal_fork_native_session_ref: str | None
    revision: dict[str, object] | None = None


def empty_manifest() -> dict[str, object]:
    return {"entries": [], "submissions": []}


def empty_understanding() -> dict[str, object]:
    return {**{key: [] for key in UNDERSTANDING_FIELDS}, "coverage": [], "selection": []}


def first_creation_instructions():
    root = Path(__file__).parent / "skills" / "first_creation"
    paths = ("SKILL.md", "references/source-evidence.md", "references/literature-corrections.md")
    contents = {name: (root / name).read_text(encoding="utf-8") for name in paths}
    return {"bundle_hash": canonical_hash(contents), "files": list(paths), "instructions": "\n\n".join(contents.values())}


def understanding_schema() -> dict[str, object]:
    citation = {"type": "object", "additionalProperties": False, "properties": {
        "material_key": {"type": "string"}, "offset": {"type": "integer", "minimum": 0},
        "length": {"type": "integer", "minimum": 1, "maximum": 65536},
        "chunk_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "location": {"type": "string", "minLength": 1}},
        "required": ["material_key", "offset", "length", "chunk_sha256", "location"]}
    statement = {"type": "object", "additionalProperties": False, "properties": {
        "ref": {"type": "string", "minLength": 1}, "text": {"type": "string", "minLength": 1},
        "kind": {"type": "string", "enum": ["reported_work", "agent_inference"]},
        "conditions": {"type": "array", "items": {"type": "string"}},
        "sources": {"type": "array", "items": citation}},
        "required": ["ref", "text", "kind", "conditions", "sources"]}
    coverage = {"type": "object", "additionalProperties": False, "properties": {
        "material_key": {"type": "string"}, "kind": {"type": "string", "enum": ["read", "partial", "unread"]},
        "read_ranges": {"type": "array", "items": citation},
        "unread_description": {"type": "string"}},
        "required": ["material_key", "kind", "read_ranges", "unread_description"]}
    selection = {"type": "object", "additionalProperties": False, "properties": {
        "material_key": {"type": "string"}, "reason": {"type": "string", "minLength": 1}},
        "required": ["material_key", "reason"]}
    return {"type": "object", "additionalProperties": False, "properties": {
        **{key: {"type": "array", "items": statement} for key in UNDERSTANDING_FIELDS},
        "coverage": {"type": "array", "items": coverage},
        "selection": {"type": "array", "items": selection}},
        "required": [*UNDERSTANDING_FIELDS, "coverage", "selection"]}


def revision_schema() -> dict[str, object]:
    return {"type": "object", "additionalProperties": False, "properties": {
        "understanding": understanding_schema(),
        "corrections": {"type": "array", "items": {"type": "object", "additionalProperties": False,
            "properties": {"prior_statement_ref": {"type": "string"},
                "disposition": {"type": "string", "enum": ["confirmed", "qualified", "contradicted", "unresolved"]},
                "explanation": {"type": "string", "minLength": 1},
                "original_sources": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "literature_sources": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                    "properties": {"paper_id": {"type": "string", "minLength": 1},
                        "locator": {"type": "string", "minLength": 1}}, "required": ["paper_id", "locator"]}, "minItems": 1}},
            "required": ["prior_statement_ref", "disposition", "explanation", "original_sources", "literature_sources"]}},
        "search_assessment": {"type": "string", "minLength": 1}},
        "required": ["understanding", "corrections", "search_assessment"]}


def validate_understanding(value, manifest, read_witness) -> dict[str, object]:
    from jsonschema import Draft202012Validator
    try:
        Draft202012Validator(understanding_schema()).validate(value)
        entries = {entry["material_key"]: entry for entry in manifest["entries"]}
        coverage = {entry["material_key"]: entry for entry in value["coverage"]}
        if len(coverage) != len(value["coverage"]) or set(coverage) != set(entries):
            raise ValueError("coverage")
        checked = set()

        def witness(citation):
            entry = entries[citation["material_key"]]
            offset, length = citation["offset"], citation["length"]
            if offset + length > entry["bytes"]:
                raise ValueError("range")
            key = canonical_hash(citation)
            if key not in checked:
                content = read_witness(entry, offset, length)
                if len(content) != length or hashlib.sha256(content).hexdigest() != citation["chunk_sha256"]:
                    raise ValueError("witness")
                checked.add(key)

        for key, item in coverage.items():
            ranges = item["read_ranges"]
            if item["kind"] == "unread" and ranges or item["kind"] != "unread" and not ranges:
                raise ValueError("coverage")
            if item["kind"] != "read" and not item["unread_description"].strip():
                raise ValueError("unread")
            for citation in ranges:
                if citation["material_key"] != key:
                    raise ValueError("coverage source")
                witness(citation)
            ordered = sorted((c["offset"], c["offset"] + c["length"]) for c in ranges)
            if any(end > following for (_, end), (following, _) in zip(ordered, ordered[1:])):
                raise ValueError("overlap")
            if item["kind"] == "read" and (ordered[0][0] != 0 or ordered[-1][1] != entries[key]["bytes"]
                    or any(end != following for (_, end), (following, _) in zip(ordered, ordered[1:]))):
                raise ValueError("incomplete read")
        refs = set()
        for field in UNDERSTANDING_FIELDS:
            for statement in value[field]:
                if statement["ref"] in refs:
                    raise ValueError("duplicate statement")
                refs.add(statement["ref"])
                if field not in {"material_composition", "gaps", "unfinished_questions"} and not statement["sources"]:
                    raise ValueError("unsupported content")
                for citation in statement["sources"]:
                    if coverage[citation["material_key"]]["kind"] == "unread":
                        raise ValueError("unread claim")
                    witness(citation)
        selection = [item["material_key"] for item in value["selection"]]
        if len(set(selection)) != len(selection) or not set(selection).issubset(entries):
            raise ValueError("selection")
    except Exception as error:
        if isinstance(error, (OwnerConflict, SemanticMcpError)):
            raise
        raise OwnerConflict("creation_understanding_invalid") from error
    return value


class CreationBasisMemory:
    def __init__(self, owner):
        self._owner = owner
        self._database = owner._database
        self.workspaces = None

    def query(self, basis_ref, expected_hash=None):
        with self._database.read() as connection:
            row = connection.execute(text("SELECT body_json,basis_hash FROM rm_creation_bases WHERE basis_ref=:ref"), {"ref": basis_ref}).first()
        if row is None:
            raise OwnerConflict("creation_basis_missing")
        body = json.loads(row.body_json)
        if canonical_hash(body) != row.basis_hash or expected_hash is not None and row.basis_hash != expected_hash:
            raise OwnerConflict("creation_basis_hash_invalid")
        return {**body, "basis_ref": basis_ref, "basis_hash": row.basis_hash}

    def prepared(self, initialization_id, draft_revision, draft_hash):
        with self._database.read() as connection:
            ref = connection.execute(text("SELECT basis_ref FROM rm_creation_bases WHERE initialization_id=:id AND draft_revision=:rev AND draft_hash=:hash AND kind='prepared'"),
                {"id": initialization_id, "rev": draft_revision, "hash": draft_hash}).scalar_one_or_none()
        return None if ref is None else self.query(ref)

    def _accept(self, body):
        digest = canonical_hash(body)
        ref = "creation_basis_" + digest[:32]
        with self._database.write() as connection:
            connection.execute(text("INSERT INTO rm_creation_bases (basis_ref,basis_hash,initialization_id,draft_revision,draft_hash,kind,body_json) VALUES (:ref,:hash,:id,:revision,:draft_hash,:kind,:body) ON CONFLICT(basis_ref) DO NOTHING"),
                {"ref": ref, "hash": digest, "id": body["draft"]["initialization_id"], "revision": body["draft"]["revision"],
                    "draft_hash": body["draft"]["hash"], "kind": body["kind"], "body": canonical_json(body)})
        return self.query(ref, digest)

    def read_workspace(self, body, entry, offset=0, limit=65536):
        return self.workspaces.read_initialization(body["draft"]["initialization_id"], body["root_session_ref"],
            workspace_ref=entry["workspace_ref"], path=entry["path"], expected_sha256=entry["sha256"], offset=offset, max_bytes=limit)

    def accept_prepared(self, request, understanding):
        from meta_research.owners.research_memory import AssetIntakeRequest
        binding = {"initialization_id": request.initialization_id, "revision": request.draft_revision, "hash": request.draft_hash}
        body = {"schema_ref": "meta-research/creation-research-basis/v1", "kind": "prepared", "draft": binding,
            "root_session_ref": request.root_session_ref, "manifest": request.manifest,
            "manifest_hash": canonical_hash(request.manifest), "understanding": understanding,
            "sources": [], "predecessor": None, "literature_snapshot": None, "corrections": [], "search_assessment": None}
        validate_understanding(understanding, request.manifest,
            lambda entry, offset, length: self.read_workspace(body, entry, offset, length)["content"])
        selection = {item["material_key"]: item["reason"] for item in understanding["selection"]}
        for entry in request.manifest["entries"]:
            source = {**entry, "binding": None, "selection_reason": selection.get(entry["material_key"]),
                "coverage": next(item for item in understanding["coverage"] if item["material_key"] == entry["material_key"])}
            if entry["material_key"] in selection:
                chunks = []
                for offset in range(0, entry["bytes"], 65536):
                    chunks.append(self.read_workspace(body, entry, offset)["content"])
                content = b"".join(chunks)
                if hashlib.sha256(content).hexdigest() != entry["sha256"]:
                    raise OwnerConflict("creation_source_changed")
                accepted = self._owner.submit_asset_intake(AssetIntakeRequest(source_kind="file", custody_mode="managed",
                    display_name=Path(entry["relative_path"]).name, media_type=mimetypes.guess_type(entry["relative_path"])[0] or "application/octet-stream",
                    content=content, provenance={"kind": "external_existing_work", "origin": entry["origin"],
                        "initialization_id": request.initialization_id, "material_key": entry["material_key"]}, origin_quest_ref=None),
                    idempotency_key="creation-source:" + canonical_hash({"id": request.initialization_id, "key": entry["material_key"]}))
                if accepted.status != "accepted" or accepted.asset is None:
                    raise OwnerConflict("creation_source_not_accepted")
                source["binding"] = accepted.asset.as_binding().as_dict()
            body["sources"].append(source)
        return self._accept(body)

    def accept_revision(self, predecessor, snapshot, revision):
        from jsonschema import Draft202012Validator
        try:
            Draft202012Validator(revision_schema()).validate(revision)
            validate_understanding(revision["understanding"], predecessor["manifest"],
                lambda entry, offset, length: self.read_source(predecessor, entry["material_key"], offset, length)["content"])
            if revision["understanding"]["selection"] != predecessor["understanding"]["selection"]:
                raise ValueError("selection changed")
            metadata = self._owner.read_literature_snapshot_metadata(snapshot["snapshot_ref"])
            ledger = self._owner.read_literature_proposal_evidence(snapshot["snapshot_ref"])
            ledger_body = metadata.get("papers_ledger") or ledger.get("papers_ledger") or {}
            ledger_papers = ledger_body.get("papers", {}) if isinstance(ledger_body, dict) else {}
            refs = {statement["ref"] for key in UNDERSTANDING_FIELDS for statement in predecessor["understanding"][key]}
            keys = {item["material_key"] for item in predecessor["sources"]}
            for correction in revision["corrections"]:
                if correction["prior_statement_ref"] not in refs or not set(correction["original_sources"]).issubset(keys):
                    raise ValueError("correction source")
                for citation in correction["literature_sources"]:
                    paper = ledger_papers.get(citation["paper_id"])
                    if paper is None:
                        raise ValueError("paper")
                    locators = paper.get("reading", {}).get("evidence_locators", [])
                    if not any(citation["locator"] == item.get("id") for item in locators):
                        raise ValueError("locator")
            if metadata["completion"] == "honest_empty" and revision["corrections"]:
                raise ValueError("empty correction")
        except OwnerConflict:
            raise
        except Exception as error:
            raise OwnerConflict("creation_revision_invalid") from error
        coverage = {item["material_key"]: item for item in revision["understanding"]["coverage"]}
        body = {key: value for key, value in predecessor.items() if key not in {"basis_ref", "basis_hash"}}
        body.update(kind="literature_revised", predecessor=self.reference(predecessor),
            literature_snapshot=snapshot, understanding=revision["understanding"], corrections=revision["corrections"],
            sources=[{**source, "coverage": coverage[source["material_key"]]} for source in predecessor["sources"]],
            search_assessment={"assessment": revision["search_assessment"], "completion": metadata["completion"], "limitations": metadata["limitations"]})
        return self._accept(body)

    @staticmethod
    def reference(basis):
        return {"basis_ref": basis["basis_ref"], "basis_hash": basis["basis_hash"], "kind": basis["kind"]}

    def associate_question(self, question, basis):
        exact = self.query(basis["basis_ref"], basis["basis_hash"])
        self._owner._quest_verifier.verify_accepted_question_binding(question)
        question = question.as_dict()
        payload = {"question": question, "basis": self.reference(exact)}
        with self._database.write() as connection:
            row = connection.execute(text("SELECT binding_json FROM rm_question_creation_bases WHERE question_ref=:question"), {"question": question["question_ref"]}).first()
            if row is not None and json.loads(row.binding_json) != payload:
                raise OwnerConflict("question_creation_basis_conflict")
            connection.execute(text("INSERT INTO rm_question_creation_bases(question_ref,quest_ref,basis_ref,binding_json) VALUES(:question,:quest,:basis,:body) ON CONFLICT(question_ref) DO NOTHING"),
                {"question": question["question_ref"], "quest": question["quest_ref"], "basis": exact["basis_ref"], "body": canonical_json(payload)})
        return self.reference(exact)

    def for_question(self, question_ref, quest_ref=None):
        with self._database.read() as connection:
            row = connection.execute(text("SELECT * FROM rm_question_creation_bases WHERE question_ref=:question"), {"question": question_ref}).first()
        if row is None:
            return None
        if quest_ref is not None and row.quest_ref != quest_ref:
            raise OwnerConflict("creation_basis_quest_unbound")
        value = json.loads(row.binding_json)
        return self.query(value["basis"]["basis_ref"], value["basis"]["basis_hash"])

    def read_source(self, basis, material_key, offset=0, limit=65536):
        source = next((entry for entry in basis["sources"] if entry["material_key"] == material_key), None)
        if source is None:
            raise OwnerConflict("creation_source_unbound")
        if source["binding"] is None:
            return self.read_workspace(basis, source, offset, limit)
        page = self._owner.read_asset_content_page(source["binding"]["version_ref"], offset=offset, limit=limit)
        content = page.get("text")
        if isinstance(content, str):
            content = base64.b64decode(content) if page.get("encoding") == "base64" else content.encode("utf-8")
        elif "content_base64" in page:
            content = base64.b64decode(page["content_base64"])
        elif "content" in page:
            content = page["content"]
            if isinstance(content, str):
                content = content.encode("utf-8")
        return {**page, "content": content}

    def source_views(self, basis):
        views = []
        for source in basis["sources"]:
            binding = source["binding"]
            reader = ({"operation": "research_memory.content.read", "source_ref": binding["version_ref"], "version_ref": binding["version_ref"]}
                if binding is not None else {"operation": "research_memory.creation_basis.read", "basis_ref": basis["basis_ref"],
                    "expected_basis_hash": basis["basis_hash"], "view": "source", "material_key": source["material_key"]})
            views.append({**source, "reader": reader})
        return views

    def project(self, basis, literature, binding):
        payload = {"basis": self.reference(basis), "literature": literature}
        documents = {"basis.json": canonical_json(basis).encode(), "understanding.json": canonical_json(basis["understanding"]).encode()}
        if literature is not None:
            documents["literature.json"] = canonical_json(literature).encode()
            snapshot_ref = literature["source_snapshot"]["snapshot_ref"]
            metadata = self._owner.read_literature_snapshot_metadata(snapshot_ref)
            exact = self._owner.read_literature_snapshot(snapshot_ref)
            documents["papers.json"] = canonical_json(metadata.get("papers_ledger") or metadata["papers"]).encode()
            documents["summary.md"] = exact["summary"].encode()
            for fulltext in exact["fulltexts"]:
                documents["fulltexts/" + fulltext["content_hash"]] = fulltext["content"].encode()
        for source in basis["sources"]:
            if source["binding"] is None:
                continue
            chunks = []
            offset = 0
            while offset < source["bytes"]:
                page = self.read_source(basis, source["material_key"], offset)
                chunks.append(page["content"])
                offset += len(page["content"])
                if not page["content"]:
                    raise OwnerConflict("creation_source_unavailable")
            documents["sources/" + source["material_key"] + "/" + Path(source["relative_path"]).name] = b"".join(chunks)
        manifest = []
        destination = self.workspaces.destination_for_initialization(basis["draft"]["initialization_id"], basis["root_session_ref"])
        items = [(".creation-context/" + path, content) for path, content in sorted(documents.items())]
        for start in range(0, len(items), 100):
            receipt = self.workspaces.deliver(destination, delivery_ref="creation-context:" + canonical_hash({"basis": payload, "start": start}), files=tuple(items[start:start + 100]))
            manifest.extend(receipt["files"])
        basis_path = next(item["path"] for item in manifest if item["path"].endswith("/.creation-context/basis.json"))
        return {"relative_root": basis_path.removesuffix("/basis.json"), "manifest": manifest, "context_hash": canonical_hash(manifest)}


def creation_basis_operations(memory, runtime, collaboration):
    def read(context, arguments):
        try:
            scope = runtime.verify_root_agent_runtime_scope(root_kind=context.root_kind, run_ref=context.run_ref,
                attempt_ref=context.attempt_ref, root_session_ref=context.root_session_ref,
                fence_ref=context.fence_ref, runtime_binding_hash=context.capability_binding_hash)
            basis = memory.creation_bases.query(arguments["basis_ref"], arguments["expected_basis_hash"])
            if scope.get("quest_ref"):
                with memory._database.read() as connection:
                    visible = connection.execute(text("SELECT 1 FROM rm_question_creation_bases WHERE quest_ref=:quest AND basis_ref=:basis"),
                        {"quest": scope["quest_ref"], "basis": basis["basis_ref"]}).first()
                if visible is None:
                    raise OwnerConflict("creation_basis_unbound")
            else:
                if context.root_kind != "deepfetch":
                    raise OwnerConflict("creation_basis_unbound")
                run = runtime.query_deepfetch_run_by_ref(context.run_ref)
                request = collaboration.query_deepfetch_request(run.request_ref)
                expected = request.scope.get("creation_basis")
                if expected != memory.creation_bases.reference(basis):
                    raise OwnerConflict("creation_basis_unbound")
            if arguments["view"] == "source":
                page = memory.creation_bases.read_source(basis, arguments["material_key"], arguments.get("offset", 0), arguments.get("limit", 8192))
                content = page.pop("content")
                if isinstance(content, bytes):
                    try:
                        page["text"] = content.decode("utf-8")
                    except UnicodeDecodeError:
                        page["content_base64"] = base64.b64encode(content).decode()
                return {**page, "basis_ref": basis["basis_ref"], "basis_hash": basis["basis_hash"]}
            value = {"understanding": basis["understanding"], "sources": memory.creation_bases.source_views(basis),
                "corrections": basis["corrections"], "search_assessment": basis["search_assessment"],
                "literature_snapshot": basis["literature_snapshot"], "predecessor": basis["predecessor"],
                "prepared_understanding": (None if basis["predecessor"] is None else memory.creation_bases.query(
                    basis["predecessor"]["basis_ref"], basis["predecessor"]["basis_hash"])["understanding"])}
            return {**context_read_page(value, path=[], offset=arguments.get("offset", 0), limit=min(arguments.get("limit", 8192), 16384)),
                "basis_ref": basis["basis_ref"], "basis_hash": basis["basis_hash"]}
        except (OwnerConflict, KeyError) as error:
            raise SemanticMcpError(getattr(error, "code", "creation_basis_unbound")) from error
    return (SemanticOperation(semantic_operation_id="research_memory.creation_basis.read", owning_module="research_memory",
        description="Read the exact existing-work basis or a captured source. Before Quest creation only the active DeepFetch request's authorized prepared basis is visible. After creation only bases associated with this Quest are visible. Unselected workspace sources may report changed or unavailable. Imported work is external provenance, never a new Quest Run.",
        input_schema={"type": "object", "additionalProperties": False, "properties": {
            "basis_ref": {"type": "string", "minLength": 1}, "expected_basis_hash": {"type": "string", "minLength": 64, "maxLength": 64},
            "view": {"type": "string", "enum": ["understanding", "source"]}, "material_key": {"type": "string"},
            "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 65536}},
            "required": ["basis_ref", "expected_basis_hash", "view"]}, output_schema={"type": "object"}, handler=read),)
