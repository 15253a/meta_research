"""Immutable views of verified RM parents, shared by indexing and execution.

A selection is an address, not a new AssetVersion. Its content fingerprint
describes only the selected tree; parent receipts keep their original meaning.
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from meta_research.owners.common import (
    AcceptanceReceipt, AcceptedAssetBinding, OwnerConflict, canonical_hash,
)
from meta_research.target_implementation_bundle import (
    TargetImplementationBundleError,
    validate_bundle_relative_path,
)


def selection_path(value: object) -> str | None:
    if value is None:
        return None
    try:
        path = validate_bundle_relative_path(value)
        if ":" in path:
            raise TargetImplementationBundleError()
        return path
    except (TypeError, TargetImplementationBundleError) as error:
        raise OwnerConflict("content_selection_path_invalid") from error


@dataclass(frozen=True)
class ContentSelection:
    asset_binding: AcceptedAssetBinding
    path: str | None = None

    def __post_init__(self):
        if type(self.asset_binding) is not AcceptedAssetBinding:
            raise OwnerConflict("content_selection_binding_invalid")
        selection_path(self.path)

    def as_dict(self) -> dict[str, object]:
        return {"asset_binding": self.asset_binding.as_dict(), "path": self.path}

    @classmethod
    def from_dict(cls, value: object) -> ContentSelection:
        if type(value) is not dict or set(value) != {"asset_binding", "path"}:
            raise OwnerConflict("content_selection_invalid")
        binding = value["asset_binding"]
        if type(binding) is not dict or set(binding) != {
            "asset_ref", "version_ref", "content_hash", "manifest_hash", "receipt",
        }:
            raise OwnerConflict("content_selection_binding_invalid")
        receipt = binding["receipt"]
        if type(receipt) is not dict or set(receipt) != {
            "status", "issuer", "kind", "receipt_ref", "subject_ref", "payload_hash",
        } or receipt["status"] != "accepted":
            raise OwnerConflict("content_selection_binding_invalid")
        if not all(type(binding[key]) is str and binding[key] for key in (
            "asset_ref", "version_ref", "content_hash", "manifest_hash",
        )) or not all(type(receipt[key]) is str and receipt[key] for key in receipt):
            raise OwnerConflict("content_selection_binding_invalid")
        return cls(AcceptedAssetBinding(
            asset_ref=binding["asset_ref"], version_ref=binding["version_ref"],
            content_hash=binding["content_hash"], manifest_hash=binding["manifest_hash"],
            receipt=AcceptanceReceipt(**{key: item for key, item in receipt.items() if key != "status"}),
        ), selection_path(value["path"]))


@dataclass(frozen=True)
class SelectionTree:
    kind: str
    entries: tuple
    directories: tuple[str, ...]
    selected_hash: str

    @property
    def byte_count(self):
        return sum(entry.size for entry in self.entries)


def selection_tree(kind, entries, directories=(), path=None) -> SelectionTree:
    """Select verified metadata, including empty directories, without live I/O.

    Entries are AssetExportEntry-shaped values (path, sha256, size), or mappings
    with those three fields. Callers authenticate the containing tree first.
    """
    from meta_research.owners.research_memory import AssetExportEntry

    path = selection_path(path)
    entries = tuple(AssetExportEntry(**entry) if type(entry) is dict else entry for entry in entries)
    if kind not in {"file", "directory"} or kind == "file" and len(entries) != 1:
        raise OwnerConflict("content_selection_parent_invalid")
    directory_set = set(directories)
    if path is not None:
        if kind != "directory":
            raise OwnerConflict("content_selection_not_found")
        matched = next((entry for entry in entries if entry.path == path), None)
        if matched is not None:
            kind = "file"
            entries = (AssetExportEntry(PurePosixPath(path).name, matched.sha256, matched.size),)
            directory_set = set()
        elif path in directory_set:
            prefix = path + "/"
            entries = tuple(AssetExportEntry(entry.path[len(prefix):], entry.sha256, entry.size)
                            for entry in entries if entry.path.startswith(prefix))
            directory_set = {directory[len(prefix):] for directory in directory_set if directory.startswith(prefix)}
        else:
            raise OwnerConflict("content_selection_not_found")
    entries = tuple(sorted(entries, key=lambda entry: entry.path))
    directories = tuple(sorted(directory_set))
    selected_hash = (entries[0].sha256 if kind == "file" else canonical_hash({
        "kind": "directory", "directories": list(directories),
        "entries": [asdict(entry) for entry in entries],
    }))
    return SelectionTree(kind, entries, directories, selected_hash)


@dataclass(frozen=True)
class SelectionDescription(SelectionTree):
    asset_binding: AcceptedAssetBinding
    path: str | None
    parent_description: object

    @property
    def content_hash(self):
        return self.selected_hash

    @property
    def memory_ref(self):
        return self.asset_binding.version_ref

    @property
    def manifest_hash(self):
        return canonical_hash({"kind": self.kind, "directories": list(self.directories),
                               "entries": [asdict(entry) for entry in self.entries]})

    @property
    def file_name(self):
        return PurePosixPath(self.path).name if self.path is not None else self.parent_description.file_name

    @property
    def media_type(self):
        return self.parent_description.media_type if self.path is None else "application/octet-stream"

    def as_dict(self):
        return {"selection": ContentSelection(self.asset_binding, self.path).as_dict(),
                "kind": self.kind, "selected_hash": self.selected_hash,
                "directories": list(self.directories), "entries": [asdict(entry) for entry in self.entries],
                "byte_count": self.byte_count}


class ContentSelectionReader:
    """Read native RM files and directory trees through one selection contract."""

    def __init__(self, verifier):
        self._verifier = verifier

    def _source(self, binding, path):
        selection = ContentSelection(binding, path)
        verifier = self._verifier
        source = verifier._asset_export_source(binding.version_ref)
        description, row, _custodies, _manifest = source
        # The source resolver already verifies the persisted receipt and manifest.
        # Compare the caller's complete identity without repeating that verification.
        accepted = AcceptedAssetBinding(row.asset_ref, row.version_ref, row.content_hash, row.manifest_hash,
            AcceptanceReceipt("research_memory", row.acceptance_kind, row.receipt_ref, row.version_ref, row.receipt_hash))
        if binding != accepted:
            raise OwnerConflict("content_selection_binding_invalid")
        tree = selection_tree(description.kind, description.entries, description.directories, selection.path)
        return SelectionDescription(tree.kind, tree.entries, tree.directories, tree.selected_hash,
                                    binding, path, description), source

    def describe(self, binding, path=None):
        return self._source(binding, path)[0]

    def read_page(self, binding, path=None, *, entry_path=None, offset=0, limit=8192):
        from meta_research.owners.research_asset_content import AssetContentPageReader
        if (type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 65536):
            raise OwnerConflict("content_page_invalid")
        description, source = self._source(binding, path)
        entry_path = selection_path(entry_path)
        common = {"source_ref": binding.version_ref, "version_ref": binding.version_ref,
                  "asset_binding": binding.as_dict(), "selection": ContentSelection(binding, path).as_dict(),
                  "content_hash": binding.content_hash, "manifest_hash": binding.manifest_hash,
                  "selected_hash": description.selected_hash}
        if description.kind == "directory" and (entry_path is None or entry_path in description.directories):
            listing = description if entry_path is None else selection_tree(
                description.kind, description.entries, description.directories, entry_path)
            prefix = "" if entry_path is None else entry_path + "/"
            members = sorted(
                [{"kind": "directory", "path": prefix + directory} for directory in listing.directories]
                + [{"kind": "file", **asdict(entry), "path": prefix + entry.path} for entry in listing.entries],
                key=lambda entry: entry["path"],
            )
            if offset > len(members):
                raise OwnerConflict("content_offset_invalid")
            count = min(limit, 100)
            page = members[offset:offset + count]
            return {**common, "kind": "directory", "entry_path": entry_path,
                    "entries": page,
                    "directories": [entry["path"] for entry in page if entry["kind"] == "directory"],
                    "offset": offset, "offset_unit": "entries",
                    "next_offset": offset + count if offset + count < len(members) else None}
        if description.kind == "file":
            if entry_path is not None:
                raise OwnerConflict("content_entry_invalid")
            entry = description.entries[0]
            parent_path = path
        else:
            entry = next((entry for entry in description.entries if entry.path == entry_path), None)
            if entry is None:
                raise OwnerConflict("content_entry_invalid")
            parent_path = "/".join(part for part in (path, entry_path) if part is not None)
        result = AssetContentPageReader(self._verifier._object_store, self._verifier)._read_verified_page(
            binding.version_ref, source, entry_path=parent_path, offset=offset, limit=limit)
        return {**result, **common, "kind": "file", "entry_path": entry_path}

    def export(self, binding, path, destination):
        from meta_research.owners.research_memory import ExportedAsset, _export_asset_entry
        description, source = self._source(binding, path)
        parent, row, custodies, manifest = source
        parent_entries = {entry["path"]: entry for entry in manifest["entries"]}
        destination = Path(destination)
        if destination.exists() or destination.is_symlink():
            raise OwnerConflict("asset_export_destination_exists")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            with tempfile.TemporaryDirectory(prefix=".selection-export-", dir=destination.parent) as temporary:
                staged = Path(temporary) / "content"
                if description.kind == "directory":
                    staged.mkdir(mode=0o700)
                    for directory in description.directories:
                        (staged / directory).mkdir(parents=True, exist_ok=True, mode=0o700)
                for entry in description.entries:
                    output = staged / entry.path if description.kind == "directory" else staged
                    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    parent_path = ("/".join(part for part in (path, entry.path) if part is not None)
                                   if description.kind == "directory" else path)
                    parent_entry = (manifest["entries"][0] if parent.kind == "file" else parent_entries[parent_path])
                    _export_asset_entry(self._verifier._object_store, row, custodies, manifest, parent_entry, output)
                    output.chmod(0o600)
                if destination.exists() or destination.is_symlink():
                    raise OwnerConflict("asset_export_destination_exists")
                if description.kind == "file":
                    os.link(staged, destination)
                else:
                    os.rename(staged, destination)
        except OSError as error:
            raise OwnerConflict("asset_export_destination_unavailable") from error
        return ExportedAsset(description, destination)
