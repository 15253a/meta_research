"""Exact asset downloads staged on service storage, with bounded-memory I/O."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from pathlib import Path
import shutil
import stat
from tempfile import TemporaryDirectory
from urllib.parse import quote
import zipfile

from fastapi import HTTPException
from fastapi.responses import FileResponse, Response

from meta_research.owners.common import OwnerConflict

LOGGER = logging.getLogger(__name__)
_cleanup_tasks: set[asyncio.Task] = set()


@dataclass
class _PreparedDownload:
    temporary: TemporaryDirectory
    path: Path
    file_name: str
    media_type: str

    def close(self) -> None:
        self.temporary.cleanup()


def _prepare_download(memory, memory_ref: str, root: Path) -> _PreparedDownload:
    if any(path.is_symlink() for path in (root, *root.parents)):
        raise OwnerConflict("asset_export_destination_invalid")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = TemporaryDirectory(prefix="download-", dir=root)
    try:
        exported = memory.export_asset(memory_ref, Path(temporary.name) / "content")
        description = exported.description
        if description.kind == "file":
            return _PreparedDownload(temporary, exported.path,
                                     description.file_name, description.media_type)
        archive_path = Path(temporary.name) / "content.zip"
        # Write each member through a bounded copy. ZIP64 supports a retained
        # file or corpus above 4 GiB; explicit directory entries retain empties.
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED,
                             allowZip64=True) as archive:
            for directory in description.directories:
                info = zipfile.ZipInfo(directory + "/", date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (stat.S_IFDIR | 0o700) << 16
                archive.writestr(info, b"")
            for entry in description.entries:
                info = zipfile.ZipInfo(entry.path, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o600 << 16
                info.file_size = entry.size
                with (exported.path / entry.path).open("rb") as source:
                    with archive.open(info, "w", force_zip64=entry.size >= zipfile.ZIP64_LIMIT) as output:
                        shutil.copyfileobj(source, output, length=1024 * 1024)
        name = description.file_name
        return _PreparedDownload(temporary, archive_path,
                                 name if name.lower().endswith(".zip") else name + ".zip",
                                 "application/zip")
    except BaseException:
        temporary.cleanup()
        raise


class AssetDownloadResponse(Response):
    """Own staging until FileResponse finishes, including a disconnected send."""

    def __init__(self, *, memory, memory_ref: str, root: Path, slots: asyncio.Semaphore):
        super().__init__()
        self._memory = memory
        self._memory_ref = memory_ref
        self._root = root
        self._slots = slots

    async def __call__(self, scope, receive, send):
        try:
            await asyncio.wait_for(self._slots.acquire(), timeout=0.05)
        except TimeoutError as error:
            raise HTTPException(status_code=503, detail={"code": "asset_io_busy"}) from error
        prepared = None
        preparation = asyncio.create_task(asyncio.to_thread(
            _prepare_download, self._memory, self._memory_ref, self._root))
        try:
            # A large, healthy copy may take longer than a metadata watchdog.
            # Shield its worker so cancellation cannot lose its cleanup owner.
            prepared = await asyncio.shield(preparation)
            response = FileResponse(prepared.path, media_type=prepared.media_type,
                headers={"Content-Disposition": "attachment; filename*=UTF-8''"
                         + quote(prepared.file_name, safe="")})
            # Keep ownership through the final body chunk rather than handing
            # the temporary path to a server extension that could outlive us.
            file_scope = {**scope, "extensions": {
                key: value for key, value in scope.get("extensions", {}).items()
                if key != "http.response.pathsend"
            }}
            await response(file_scope, receive, send)
        finally:
            async def clean_up():
                try:
                    try:
                        result = await asyncio.shield(preparation)
                    except BaseException:
                        # The preparation worker removes its partial outputs.
                        return
                    await asyncio.to_thread(result.close)
                finally:
                    self._slots.release()

            # Cleanup outlives a cancelled response and remains off the event
            # loop even for a corpus with many files. Its slot is released if
            # deletion itself fails, so one storage fault cannot lock downloads.
            cleanup = asyncio.create_task(clean_up())
            _cleanup_tasks.add(cleanup)
            def finished(completed):
                _cleanup_tasks.discard(completed)
                if not completed.cancelled() and completed.exception() is not None:
                    error = completed.exception()
                    LOGGER.error("Asset download cleanup failed",
                                 exc_info=(type(error), error, error.__traceback__))
            cleanup.add_done_callback(finished)
            if preparation.done():
                await asyncio.shield(cleanup)
