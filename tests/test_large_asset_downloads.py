"""The existing authenticated download route exports full exact RM versions."""
import asyncio
import hashlib
import io
from pathlib import Path
import threading
import zipfile

from fastapi.responses import FileResponse
import pytest

from meta_research.asset_download import AssetDownloadResponse, _prepare_download
import meta_research.asset_download as download_module
from meta_research.composition import build_production_runtime
from meta_research.owners.common import OwnerConflict
from meta_research.owners.research_memory import AssetIntakeRequest
from meta_research.paths import prepare_data_root
from test_public_research_asset_web import _authenticated_client


@pytest.fixture
def asset_runtime(tmp_path):
    runtime = build_production_runtime(prepare_data_root(tmp_path / "service-data"))
    try:
        yield runtime
    finally:
        runtime.close()


def _retain(runtime, path, key):
    accepted = runtime.owners.research_memory.submit_asset_intake(
        AssetIntakeRequest(source_kind="local_path", custody_mode="managed",
                           display_name=path.name, source_locator=str(path)),
        idempotency_key=key)
    assert accepted.status == "accepted"
    return accepted.asset


@pytest.mark.parametrize("directory", [False, True], ids=["file", "natural-tree"])
def test_large_exact_asset_download_uses_bounded_file_response(
    tmp_path, monkeypatch, asset_runtime, directory,
):
    runtime = asset_runtime
    source = tmp_path / ("collected-data" if directory else "checkpoint.bin")
    if directory:
        (source / "empty").mkdir(parents=True)
        (source / "partition").mkdir()
    payload = source / "partition/observations.bin" if directory else source
    size = 65 * 1024 * 1024 + 7
    with payload.open("wb") as stream:
        stream.seek(size - 1)
        stream.write(b"x")
    with payload.open("rb") as stream:
        expected_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    asset = _retain(runtime, source, "download-large")
    version_ref, content_hash = asset.version_ref, asset.content_hash
    description = runtime.owners.research_memory.describe_asset_export(version_ref)
    payload.write_bytes(b"the current acquisition source has changed")
    root = runtime.data_root.run / "asset-downloads"
    chunks = []
    original_call = FileResponse.__call__

    async def file_response(self, scope, receive, send):
        assert Path(self.path).is_relative_to(root)
        assert Path(self.path).stat().st_size > 64 * 1024 * 1024
        async def observed_send(message):
            if message["type"] == "http.response.body":
                chunks.append(len(message.get("body", b"")))
            await send(message)
        await original_call(self, scope, receive, observed_send)

    monkeypatch.setattr(FileResponse, "__call__", file_response)
    def no_materialization(*_args, **_kwargs):
        raise AssertionError("HTTP download must not materialize a bytes corpus")
    monkeypatch.setattr(runtime.owners.research_memory, "materialize_asset", no_materialization)
    client, _headers = _authenticated_client(runtime)
    try:
        # TestClient collects the body; the production response sends chunks.
        response = client.get(f"/api/v1/research-assets/{version_ref}/content")
        assert response.status_code == 200, response.text[:200]
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["content-disposition"].startswith("attachment; filename*=UTF-8''")
        assert 0 < max(chunks) <= FileResponse.chunk_size
        if directory:
            assert response.headers["content-type"] == "application/zip"
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                assert set(archive.namelist()) == {"empty/", "partition/", "partition/observations.bin"}
                with archive.open("partition/observations.bin") as stream:
                    assert hashlib.file_digest(stream, "sha256").hexdigest() == expected_hash
                assert archive.getinfo("partition/observations.bin").file_size == size
        else:
            assert hashlib.sha256(response.content).hexdigest() == expected_hash
            assert len(response.content) == size
        assert not list(root.iterdir())
        assert runtime.owners.research_memory.describe_asset_export(version_ref) == description
        assert description.content_hash == content_hash
    finally:
        client.close()


@pytest.mark.parametrize("failure", ["export", "zip"])
def test_download_preparation_failure_removes_service_staging(
    tmp_path, monkeypatch, asset_runtime, failure,
):
    runtime = asset_runtime
    source = tmp_path / "records"
    source.mkdir()
    (source / "report.md").write_text("Actual observations", encoding="utf-8")
    asset = _retain(runtime, source, "download-failure")
    root = runtime.data_root.run / "asset-downloads"
    if failure == "export":
        def broken_export(_ref, destination):
            destination.mkdir()
            (destination / "partial").write_bytes(b"partial")
            raise OwnerConflict("asset_custody_unavailable")
        monkeypatch.setattr(runtime.owners.research_memory, "export_asset", broken_export)
    else:
        def broken_zip(*_args, **_kwargs):
            raise OSError("storage interrupted")
        monkeypatch.setattr(zipfile.ZipFile, "open", broken_zip)
    with pytest.raises((OwnerConflict, OSError)):
        _prepare_download(runtime.owners.research_memory, asset.version_ref, root)
    assert not list(root.iterdir())


@pytest.mark.parametrize("during_preparation", [False, True], ids=["body-disconnect", "export-cancel"])
def test_download_disconnect_retains_cleanup_ownership(
    tmp_path, monkeypatch, asset_runtime, during_preparation,
):
    runtime = asset_runtime
    source = tmp_path / "report.md"
    source.write_text("Actual observations", encoding="utf-8")
    asset = _retain(runtime, source, "download-disconnect")
    root = runtime.data_root.run / "asset-downloads"
    started, release = threading.Event(), threading.Event()
    original_export = runtime.owners.research_memory.export_asset
    if during_preparation:
        def delayed_export(*args):
            started.set()
            assert release.wait(timeout=5)
            return original_export(*args)
        monkeypatch.setattr(runtime.owners.research_memory, "export_asset", delayed_export)

    async def check():
        slots = asyncio.Semaphore(1)
        response = AssetDownloadResponse(memory=runtime.owners.research_memory,
            memory_ref=asset.version_ref, root=root, slots=slots)
        async def receive():
            return {"type": "http.disconnect"}
        async def send(message):
            if message["type"] == "http.response.body":
                raise asyncio.CancelledError()
        task = asyncio.create_task(response(
            {"type": "http", "method": "GET", "headers": [], "extensions": {},
             "asgi": {"spec_version": "2.4"}}, receive, send))
        if during_preparation:
            assert await asyncio.to_thread(started.wait, 5)
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        await asyncio.wait_for(slots.acquire(), timeout=5)
        assert not list(root.iterdir())
    try:
        asyncio.run(check())
    finally:
        release.set()


@pytest.mark.parametrize("source_kind", ["text", "link"])
def test_existing_text_and_link_downloads_keep_exact_bytes(asset_runtime, source_kind):
    runtime = asset_runtime
    payload = b"https://example.org/materials/42" if source_kind == "link" else b"# Exact notes\n"
    accepted = runtime.owners.research_memory.submit_asset_intake(
        AssetIntakeRequest(source_kind=source_kind, custody_mode="managed",
            display_name="source.url" if source_kind == "link" else "notes.md",
            media_type="text/uri-list" if source_kind == "link" else "text/markdown",
            source_locator=payload.decode() if source_kind == "link" else None,
            content=payload if source_kind == "text" else None),
        idempotency_key="download-existing")
    assert accepted.status == "accepted"
    client, _headers = _authenticated_client(runtime)
    try:
        response = client.get(f"/api/v1/research-assets/{accepted.asset.version_ref}/content")
        assert response.status_code == 200
        assert response.content == payload
        assert response.headers["content-type"].startswith(accepted.asset.media_type)
        assert response.headers["cache-control"] == "no-store"
        assert not list((runtime.data_root.run / "asset-downloads").iterdir())
    finally:
        client.close()


@pytest.mark.parametrize("cleanup_failure", [False, True], ids=["slow-cleanup", "failed-cleanup"])
def test_download_cleanup_is_off_loop_and_always_releases_slot(
    tmp_path, monkeypatch, asset_runtime, cleanup_failure,
):
    runtime = asset_runtime
    source = tmp_path / "notes.md"
    source.write_text("Actual observations", encoding="utf-8")
    asset = _retain(runtime, source, "download-cleanup")
    root = runtime.data_root.run / "asset-downloads"
    original_close = download_module._PreparedDownload.close
    started, release = threading.Event(), threading.Event()
    def close(prepared):
        started.set()
        assert release.wait(timeout=5)
        original_close(prepared)
        if cleanup_failure:
            raise OSError("cleanup storage failure")
    monkeypatch.setattr(download_module._PreparedDownload, "close", close)

    async def check():
        slots = asyncio.Semaphore(1)
        response = AssetDownloadResponse(memory=runtime.owners.research_memory,
            memory_ref=asset.version_ref, root=root, slots=slots)
        async def receive():
            return {"type": "http.disconnect"}
        async def send(_message):
            pass
        task = asyncio.create_task(response(
            {"type": "http", "method": "GET", "headers": [], "extensions": {},
             "asgi": {"spec_version": "2.4"}}, receive, send))
        assert await asyncio.to_thread(started.wait, 5)
        # A thread is still deleting; the event loop can independently run.
        assert not task.done()
        assert slots.locked()
        await asyncio.sleep(0)
        release.set()
        if cleanup_failure:
            with pytest.raises(OSError, match="cleanup storage failure"):
                await task
        else:
            await task
        await asyncio.wait_for(slots.acquire(), timeout=1)
        assert not list(root.iterdir())
    try:
        asyncio.run(check())
    finally:
        release.set()
