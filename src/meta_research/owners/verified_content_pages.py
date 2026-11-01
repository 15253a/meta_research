"""Bounded bytes verified against the accepted identity on the opened source."""
from __future__ import annotations

import hashlib
import os

from meta_research.owners.common import OwnerConflict


class VerifiedContentPages:
    """Verify the same bytes that supply the page on every new open.

    Callers own source custody and acceptance validation. Every read compares
    the opened descriptor before and after I/O. Metadata stability is only a
    race check, never proof of content equality: even ctime may be coarse.
    Capture the requested range during verification, avoiding a second read.
    """

    @staticmethod
    def signature(path, source, expected_hash):
        info = os.fstat(source.fileno())
        return (str(path), info.st_dev, info.st_ino, info.st_size,
                info.st_mtime_ns, info.st_ctime_ns, expected_hash)

    def read_page(self, source, path, entry, offset, limit):
        expected_size = entry["size"]
        before = self.signature(path, source, entry["sha256"])
        if before[3] != expected_size:
            raise OwnerConflict("content_drifted")
        if offset > expected_size:
            raise OwnerConflict("content_offset_invalid")
        digest = hashlib.sha256()
        size = 0
        parts = []
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            start = max(0, offset - size)
            end = min(len(chunk), offset + limit - size)
            if end > start:
                parts.append(chunk[start:end])
            size += len(chunk)
        if size != expected_size or digest.hexdigest() != entry["sha256"]:
            raise OwnerConflict("content_drifted")
        data = b"".join(parts)
        if (self.signature(path, source, entry["sha256"]) != before
                or len(data) != min(limit, expected_size - offset)):
            raise OwnerConflict("content_drifted")
        return data

