"""Bounded bytes from an opened immutable source; no source or receipt cache."""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import os
import threading

from meta_research.owners.common import OwnerConflict


class VerifiedContentPages:
    """Hash once per stable file signature; retain only a bounded LRU of signatures.

    Callers own source custody and acceptance validation. Every read compares
    the opened descriptor before and after I/O, including ctime to detect a
    same-size mutation whose mtime was restored.
    """

    def __init__(self, *, max_verified_files=256):
        if type(max_verified_files) is not int or max_verified_files < 1:
            raise ValueError("content_signature_cache_limit_invalid")
        self._max_verified_files = max_verified_files
        self._verified: OrderedDict[tuple, None] = OrderedDict()
        self._lock = threading.Lock()

    def _contains(self, key):
        with self._lock:
            if key not in self._verified:
                return False
            self._verified.move_to_end(key)
            return True

    def _remember(self, key):
        with self._lock:
            self._verified[key] = None
            self._verified.move_to_end(key)
            while len(self._verified) > self._max_verified_files:
                self._verified.popitem(last=False)

    def _forget(self, key):
        with self._lock:
            self._verified.pop(key, None)

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
        if self._contains(before):
            source.seek(offset)
            data = source.read(min(limit, expected_size - offset))
        else:
            # Collect the requested page while hashing so its first read does
            # not perform another seek/read or retain the entire large asset.
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
            self._forget(before)
            raise OwnerConflict("content_drifted")
        self._remember(before)
        return data

