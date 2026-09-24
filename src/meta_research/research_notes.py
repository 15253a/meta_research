"""Free-form research explanations, referenced through immutable RM assets."""
from __future__ import annotations

import hashlib
import codecs
import json
import os
from pathlib import Path
import stat
from sqlalchemy import text

from meta_research.context_presentation import bounded_text
from meta_research.target_implementation_bundle import parse_target_implementation_bundle

FINAL_STATEMENT_PATH = "handoff/final-message.md"


def research_note_metadata_from_path(*, role: str, declared_relative_path: str,
                                    artifact_kind: str, source_path: Path,
                                    tree_hash: str) -> dict[str, object] | None:
    """Validate a selected UTF-8 note by streaming; retain only a small excerpt."""
    from meta_research.owners.common import OwnerConflict
    if role != "analysis":
        return None
    entry_path = None
    kind = "research_note"
    if artifact_kind == "directory" and declared_relative_path == "outputs/analysis":
        entry_path = "research-note.md"
        source_path = source_path / entry_path
    elif artifact_kind == "file" and declared_relative_path in {
        "outputs/analysis/research-note.md", FINAL_STATEMENT_PATH,
    }:
        if declared_relative_path == FINAL_STATEMENT_PATH:
            kind = "final_statement"
    else:
        return None
    try:
        descriptor = os.open(source_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                             | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return None
    except OSError as error:
        raise OwnerConflict("research_note_source_invalid") from error
    try:
        with os.fdopen(descriptor, "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise OwnerConflict("research_note_source_invalid")
            decoder = codecs.getincrementaldecoder("utf-8")("strict")
            digest = hashlib.sha256()
            prefix = bytearray()
            byte_count = 0
            while chunk := source.read(1024 * 1024):
                decoder.decode(chunk)
                digest.update(chunk)
                byte_count += len(chunk)
                if len(prefix) < 2048:
                    prefix.extend(chunk[:2048 - len(prefix)])
            decoder.decode(b"", final=True)
            after = os.fstat(source.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns
            ) or byte_count != before.st_size:
                raise OwnerConflict("research_note_source_invalid")
    except UnicodeDecodeError:
        # Non-UTF-8 assets remain valid research outputs without text-note identity.
        return None
    except OSError as error:
        raise OwnerConflict("research_note_source_invalid") from error
    if artifact_kind == "file" and digest.hexdigest() != tree_hash:
        raise OwnerConflict("research_note_source_invalid")
    return {
        "schema_ref": "meta-research/research-note-reference/v1",
        "kind": kind,
        "entry_path": entry_path,
        "source_bytes_sha256": digest.hexdigest(),
        "source_utf8_bytes": byte_count,
        "summary": {"text": prefix.decode("utf-8", errors="ignore"),
                    "source_utf8_bytes": byte_count, "truncated": byte_count > 2048},
        "summary_only": True,
    }


def research_note_metadata(*, role: str, declared_relative_path: str,
                           artifact_kind: str, content: bytes,
                           tree_hash: str) -> dict[str, object] | None:
    """Derive a reading excerpt from exact bytes; never interpret it as a metric."""
    if role != "analysis":
        return None
    entry_path = None
    kind = "research_note"
    if artifact_kind == "directory" and declared_relative_path == "outputs/analysis":
        bundle = parse_target_implementation_bundle(content, expected_tree_sha256=tree_hash)
        entry = next((entry for entry in bundle.entries
                      if entry.relative_path == "research-note.md"), None)
        if entry is None:
            return None
        content, entry_path = entry.content, entry.relative_path
    elif artifact_kind == "file" and declared_relative_path in {
        "outputs/analysis/research-note.md", FINAL_STATEMENT_PATH,
    }:
        if declared_relative_path == FINAL_STATEMENT_PATH:
            kind = "final_statement"
    else:
        return None
    # An old analysis asset may use another encoding. Preserve that asset and
    # omit the optional reading excerpt rather than reject its research result.
    try:
        body = content.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return {
        "schema_ref": "meta-research/research-note-reference/v1",
        "kind": kind,
        "entry_path": entry_path,
        "source_bytes_sha256": hashlib.sha256(content).hexdigest(),
        "source_utf8_bytes": len(content),
        "summary": bounded_text(body, 2048),
        "summary_only": True,
    }


def manifest_research_notes(manifest: object) -> list[dict[str, object]]:
    """Small exact-version references suitable for completion and later stages."""
    return [{**entry.research_note,
             "manifest_ref": manifest.manifest_ref,
             "declared_relative_path": entry.declared_relative_path,
             "asset_ref": entry.binding.asset_ref,
             "version_ref": entry.binding.version_ref,
             "asset_content_hash": entry.binding.content_hash,
             "asset_manifest_hash": entry.binding.manifest_hash}
            for entry in manifest.entries if getattr(entry, "research_note", None)]


def query_target_research_notes(database, memory, target_ref: str, *, version_ref=None):
    """Read a bounded current explanation catalog or one exact historical version."""
    from meta_research.owners.common import OwnerConflict, canonical_hash
    with database.read() as connection:
        historical = connection.execute(text(
            "SELECT n.*, c.evidence_ref AS actual_evidence_ref, "
            "c.evidence_content_hash AS actual_evidence_hash FROM rm_target_research_notes n "
            "JOIN ar_target_root_completions c ON c.completion_ref=n.completion_ref "
            "WHERE n.target_ref=:target AND (:version IS NULL OR n.version_ref=:version) "
            "ORDER BY n.accepted_at DESC LIMIT 3"),
            {"target": target_ref, "version": version_ref}).mappings().all()
        manifests = connection.execute(text(
            "SELECT manifest_ref, entries_json, entries_hash, accepted_at FROM rm_target_root_completion_manifests "
            "WHERE target_ref=:target AND (:version IS NULL OR EXISTS ("
            "SELECT 1 FROM json_each(entries_json) e WHERE "
            "json_extract(e.value,'$.binding.version_ref')=:version)) "
            "ORDER BY accepted_at DESC LIMIT 3"),
            {"target": target_ref, "version": version_ref}).mappings().all()
    notes = []
    for row in historical:
        note = json.loads(row["note_json"])
        if (canonical_hash(note) != row["note_hash"] or note["target_ref"] != target_ref
            or note["version_ref"] != row["version_ref"]
            or row["actual_evidence_ref"] != row["source_evidence_ref"]
            or row["actual_evidence_hash"] != row["source_evidence_hash"]):
            raise OwnerConflict("research_note_source_invalid")
        notes.append((row["accepted_at"], note))
    for row in manifests:
        entries = json.loads(row["entries_json"])
        if canonical_hash(entries) != row["entries_hash"]:
            raise OwnerConflict("research_note_source_invalid")
        for entry in entries:
            metadata = entry.get("research_note")
            binding = entry["binding"]
            if not metadata or version_ref is not None and binding["version_ref"] != version_ref:
                continue
            notes.append((row["accepted_at"], {**metadata, "manifest_ref": row["manifest_ref"], "target_ref": target_ref,
                "declared_relative_path": entry["declared_relative_path"],
                "asset_ref": binding["asset_ref"], "version_ref": binding["version_ref"],
                "asset_content_hash": binding["content_hash"], "asset_manifest_hash": binding["manifest_hash"]}))
    unique = {}
    for _, note in sorted(notes, key=lambda item: item[0], reverse=True):
        unique.setdefault(note["version_ref"], note)
    for note in unique.values():
        asset = memory.query_asset_version(note["version_ref"])
        if asset is None or (asset.asset_ref, asset.content_hash, asset.manifest_hash) != (
            note["asset_ref"], note["asset_content_hash"], note["asset_manifest_hash"]):
            raise OwnerConflict("research_note_asset_invalid")
    return list(unique.values())


def read_note_body(memory, note):
    from meta_research.owners.common import OwnerConflict
    description = _streamed_note_description(memory, note)
    if description is not None:
        try:
            return memory.read_asset_entry_text(
                note["version_ref"], entry_path=note["entry_path"]
            )
        except (OwnerConflict, UnicodeDecodeError) as error:
            raise OwnerConflict("research_note_source_invalid") from error
    content = memory.materialize_asset(note["version_ref"]).content
    if hashlib.sha256(content).hexdigest() != note["asset_content_hash"]:
        raise OwnerConflict("research_note_asset_invalid")
    if note["entry_path"] is not None:
        content = parse_target_implementation_bundle(content).entry(note["entry_path"]).content
    if hashlib.sha256(content).hexdigest() != note["source_bytes_sha256"]:
        raise OwnerConflict("research_note_source_invalid")
    return content.decode("utf-8")


def _streamed_note_description(memory, note):
    """Match note bytes to an exact native asset entry; legacy ZIPs use old readers."""
    from meta_research.owners.common import OwnerConflict
    describe = getattr(memory, "describe_asset_export", None)
    if not callable(describe):
        return None
    description = describe(note["version_ref"])
    if (description.content_hash, description.manifest_hash) != (
        note["asset_content_hash"], note["asset_manifest_hash"]
    ):
        raise OwnerConflict("research_note_asset_invalid")
    if description.kind == "file" and note["entry_path"] is not None:
        return None
    if description.kind == "file":
        digest, byte_count = description.content_hash, description.byte_count
    else:
        entries = [entry for entry in description.entries
                   if entry.path == note["entry_path"]]
        if len(entries) != 1:
            raise OwnerConflict("research_note_source_invalid")
        digest, byte_count = entries[0].sha256, entries[0].size
    if (digest, byte_count) != (note["source_bytes_sha256"], note["source_utf8_bytes"]):
        raise OwnerConflict("research_note_source_invalid")
    return description


def query_target_input_research_notes(database, memory, *, quest_ref, target_ref,
                                     upstream_commit_refs, input_version_refs,
                                     offset=0, version_ref=None):
    """Page exact note versions for an AR-authenticated Target and frozen inputs.

    A selected note asset grants only that version, never its whole source Target.
    All source Targets, including frozen upstream commits, must belong to this Quest.
    """
    from meta_research.owners.common import OwnerConflict
    if type(offset) is not int or offset < 0:
        raise OwnerConflict("research_note_page_invalid")
    with database.read() as connection:
        quest = connection.execute(text(
            "SELECT g.quest_ref FROM rg_targets t JOIN rg_target_graphs g ON g.graph_ref=t.graph_ref "
            "WHERE t.target_ref=:target"), {"target": target_ref}).scalar_one_or_none()
        if quest != quest_ref:
            raise OwnerConflict("research_note_source_unbound")
        sources = [target_ref]
        for commit_ref in dict.fromkeys(upstream_commit_refs):
            row = connection.execute(text(
                "SELECT c.target_ref,g.quest_ref FROM rg_target_commits c "
                "JOIN rg_targets t ON t.target_ref=c.target_ref "
                "JOIN rg_target_graphs g ON g.graph_ref=t.graph_ref WHERE c.commit_ref=:ref"),
                {"ref": commit_ref}).first()
            if row is None or row.quest_ref != quest_ref:
                raise OwnerConflict("research_note_source_unbound")
            sources.append(row.target_ref)
        rows = connection.execute(text(
            "WITH notes AS (SELECT target_ref,version_ref,accepted_at FROM rm_target_research_notes "
            "UNION ALL SELECT m.target_ref,json_extract(e.value,'$.binding.version_ref'),m.accepted_at "
            "FROM rm_target_root_completion_manifests m,json_each(m.entries_json) e "
            "WHERE json_extract(e.value,'$.research_note') IS NOT NULL) "
            "SELECT n.target_ref,n.version_ref,MAX(n.accepted_at) AS accepted_at FROM notes n "
            "JOIN rg_targets t ON t.target_ref=n.target_ref JOIN rg_target_graphs g ON g.graph_ref=t.graph_ref "
            "WHERE g.quest_ref=:quest AND (n.target_ref IN (SELECT value FROM json_each(:targets)) "
            "OR n.version_ref IN (SELECT value FROM json_each(:versions))) "
            "AND (:version IS NULL OR n.version_ref=:version) "
            "GROUP BY n.target_ref,n.version_ref ORDER BY accepted_at DESC,n.version_ref DESC "
            "LIMIT 13 OFFSET :offset"),
            {"quest": quest_ref, "targets": json.dumps(sources),
             "versions": json.dumps(list(input_version_refs)), "version": version_ref,
             "offset": 0 if version_ref is not None else offset}).all()
    if version_ref is not None and len(rows) != 1:
        raise OwnerConflict("research_note_source_unbound")
    notes = []
    for row in rows[:12]:
        exact = query_target_research_notes(database, memory, row.target_ref, version_ref=row.version_ref)
        if len(exact) != 1:
            raise OwnerConflict("research_note_source_invalid")
        notes.append(exact[0])
    if version_ref is not None:
        return {"reference": notes[0], "body": read_note_body(memory, notes[0])}
    return {"summary_only": True, "offset": offset, "limit": 12,
            "next_offset": offset + 12 if len(rows) > 12 else None, "items": notes}


def materialize_note_body(memory, note, directory: Path):
    """Write one exact version outside the frozen execution-input manifest."""
    from meta_research.owners.common import OwnerConflict
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if any(parent.is_symlink() for parent in (directory, *directory.parents)):
        raise OwnerConflict("research_note_path_invalid")
    path = directory / (note["source_bytes_sha256"] + ".md")
    description = _streamed_note_description(memory, note)
    if description is not None:
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_file():
                raise OwnerConflict("research_note_asset_invalid")
            with path.open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            if digest != note["source_bytes_sha256"]:
                raise OwnerConflict("research_note_asset_invalid")
        else:
            memory.export_asset_entry(note["version_ref"], path, entry_path=note["entry_path"])
            path.chmod(0o400)
        return str(path)
    content = read_note_body(memory, note).encode("utf-8")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o400)
    except FileExistsError:
        if path.is_symlink() or path.read_bytes() != content:
            raise OwnerConflict("research_note_asset_invalid")
    else:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
    return str(path)
