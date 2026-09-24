"""RM literature body custody and bounded pages from immutable metadata indexes."""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
import tempfile

from meta_research.owners.common import OwnerConflict
from meta_research.owners.verified_content_pages import VerifiedContentPages


def validate_body(body):
    if (not isinstance(body, dict) or set(body) != {"path", "sha256", "size"}
        or not isinstance(body["sha256"], str) or len(body["sha256"]) != 64
        or any(c not in "0123456789abcdef" for c in body["sha256"])
        or type(body["size"]) is not int or body["size"] < 0
        or body["path"] != f"literature-snapshot/body/{body['sha256'][:2]}/{body['sha256']}.utf8"):
        raise OwnerConflict("literature_content_index_invalid")
    return body


def store_body(object_store: Path, content: str):
    data = content.encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    body = {"path": f"literature-snapshot/body/{digest[:2]}/{digest}.utf8", "sha256": digest, "size": len(data)}
    destination = object_store / body["path"]
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.exists():
        reader = LiteratureContentPageReader(object_store)
        reader.read_page(body, offset=0, limit=1)
        return body
    descriptor, temporary_name = tempfile.mkstemp(prefix=".body-", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, destination)
        directory = os.open(destination.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
    return body


class LiteratureContentPageReader:
    def __init__(self, object_store: Path):
        self._object_store = object_store.resolve()
        # Reuse RM's bounded streaming verifier. It caches only file signatures,
        # and no literature source bytes or acceptance receipts.
        self._pages = VerifiedContentPages()

    def _path(self, body):
        validate_body(body)
        candidate = self._object_store / body["path"]
        for part in (candidate, *candidate.parents):
            if part == self._object_store:
                break
            if part.is_symlink():
                raise OwnerConflict("literature_content_unavailable")
        if not candidate.is_file():
            raise OwnerConflict("literature_content_unavailable")
        return candidate

    def read_page(self, body, *, offset=0, limit=8192):
        from meta_research.owners.research_memory import _open_asset_regular_file
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 65536:
            raise OwnerConflict("content_page_invalid")
        path = self._path(body)
        try:
            with _open_asset_regular_file(path, "literature_content_unavailable") as source:
                data = self._pages.read_page(source, path, body, offset, limit)
        except OSError as error:
            raise OwnerConflict("literature_content_unavailable") from error
        encoding = "utf-8"
        try:
            value = data.decode("utf-8")
        except UnicodeDecodeError as error:
            if error.reason == "unexpected end of data" and error.start > 0:
                data = data[:error.start]
                value = data.decode("utf-8")
            else:
                encoding = "base64"
                value = base64.b64encode(data).decode("ascii")
        end = offset + len(data)
        return {"text": value, "encoding": encoding, "entry_hash": body["sha256"],
                "offset": offset, "offset_unit": "bytes", "returned_bytes": len(data),
                "total_bytes": body["size"], "next_offset": end if end < body["size"] else None,
                "complete": end >= body["size"], "access_path": str(path)}

    def read_all(self, body):
        # Only the explicitly full snapshot API uses this method.
        from meta_research.owners.research_memory import _open_asset_regular_file
        path = self._path(body)
        try:
            with _open_asset_regular_file(path, "literature_content_unavailable") as source:
                self._pages.read_page(source, path, body, 0, 1)
                before = self._pages.signature(path, source, body["sha256"])
                source.seek(0)
                data = source.read(body["size"] + 1)
                if (len(data) != body["size"]
                    or hashlib.sha256(data).hexdigest() != body["sha256"]
                    or self._pages.signature(path, source, body["sha256"]) != before):
                    raise OwnerConflict("content_drifted")
                return data.decode("utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise OwnerConflict("literature_content_unavailable") from error
