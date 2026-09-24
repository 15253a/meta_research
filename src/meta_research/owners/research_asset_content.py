"""Bounded exact asset pages owned by RM, without exporting original custody."""
from __future__ import annotations

import base64
from dataclasses import asdict
from pathlib import Path

from meta_research.owners.common import OwnerConflict
from meta_research.owners.verified_content_pages import VerifiedContentPages


class AssetContentPageReader:
    """Cache only stable verified file signatures, never source bytes or receipts.

    Every page revalidates RM metadata and receipts and opens the actual source.
    Its first unchanged signature is hashed in full on that descriptor. Later
    pages read only their byte range, checking fstat before and after the read.
    Inode replacement and same-size writes (including restored mtime) therefore
    require a fresh whole-file verification. The cache dies with this RM Owner.
    """

    def __init__(self, object_store: Path, receipt_verifier, *, max_verified_files=256):
        self._object_store = object_store
        self._receipt_verifier = receipt_verifier
        self._pages = VerifiedContentPages(max_verified_files=max_verified_files)

    def read_page(self, memory_ref: str, *, entry_path=None, offset=0, limit=8192):
        from meta_research.owners.research_memory import (
            _accepted_asset, _asset_entry_sources, _open_asset_regular_file,
        )

        if (type(offset) is not int or offset < 0 or type(limit) is not int
                or not 1 <= limit <= 65536):
            raise OwnerConflict("content_page_invalid")
        # No snapshot/byte cache participates in this immutable receipt check.
        description, row, custodies, manifest = self._receipt_verifier._asset_export_source(memory_ref)
        binding = _accepted_asset(row, custodies).as_binding().as_dict()
        entries = manifest["entries"]
        if entry_path is None and len(entries) != 1:
            count = min(limit, 100)
            return {"kind": "directory", "version_ref": memory_ref, "asset_binding": binding,
                    "content_hash": description.content_hash, "manifest_hash": description.manifest_hash,
                    "entries": [asdict(entry) for entry in description.entries[offset:offset + count]],
                    "offset_unit": "entries", "next_offset": offset + count if offset + count < len(entries) else None}
        selected = [entry for entry in entries if entry_path is None or entry["path"] == entry_path]
        if len(selected) != 1:
            raise OwnerConflict("content_entry_invalid")
        entry = selected[0]
        drifted = False
        for candidate in _asset_entry_sources(self._object_store, row, custodies, manifest, entry):
            try:
                with _open_asset_regular_file(candidate, "content_unavailable") as source:
                    data = self._pages.read_page(source, candidate, entry, offset, limit)
            except OSError:
                continue
            except OwnerConflict as error:
                if error.code == "content_drifted":
                    drifted = True
                elif error.code not in {"content_unavailable", "asset_source_entry_unsupported"}:
                    raise
                continue
            encoding = "utf-8"
            try:
                body = data.decode("utf-8")
            except UnicodeDecodeError as error:
                if error.reason == "unexpected end of data" and error.start > 0:
                    data = data[:error.start]
                    body = data.decode("utf-8")
                else:
                    encoding = "base64"
                    body = base64.b64encode(data).decode("ascii")
            next_offset = offset + len(data)
            return {"source_ref": memory_ref, "version_ref": memory_ref, "asset_binding": binding,
                    "entry_path": entry["path"],
                    "content_hash": description.content_hash, "entry_hash": entry["sha256"],
                    "encoding": encoding, "text": body, "offset": offset, "offset_unit": "bytes",
                    "returned_bytes": len(data), "total_bytes": entry["size"], "next_offset": next_offset,
                    "complete": next_offset >= entry["size"],
                    "custody_modes": [custody.custody_mode for custody in custodies],
                    "access_path": str(candidate)}
        raise OwnerConflict("content_drifted" if drifted else "content_unavailable")
