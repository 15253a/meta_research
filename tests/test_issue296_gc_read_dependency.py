"""An actual RM read holds expired scratch until managed custody is accepted."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
from threading import Event

import meta_research.owners.research_memory as memory_module
from meta_research.owners.research_memory import AssetIntakeRequest
from test_completed_workspace_cleanup import _complete, _target_cleanup


def test_processing_rm_read_holds_workspace_until_exact_managed_readback(tmp_path, monkeypatch):
    runtime, _, _, workspace, _, _, _ = _complete(tmp_path)
    read_started = Event()
    resume_read = Event()
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        source_path = workspace / "pending-read.bin"
        content = b"research reading dependency\n" * 80000
        assert len(content) > memory_module.ASSET_HASH_CHUNK_BYTES
        source_path.write_bytes(content)
        memory = runtime.owners.research_memory
        job = memory.submit_asset_intake(
            AssetIntakeRequest(source_kind="local_path", custody_mode="managed",
                source_locator=str(source_path), display_name="pending-read.bin", asynchronous=True),
            idempotency_key="actual-reading-dependency",
        )
        assert job.status == "queued"
        open_regular = memory_module._open_asset_regular_file
        observed = {}

        class PausedReader:
            def __init__(self, stream):
                self.stream = stream

            def fileno(self):
                return self.stream.fileno()

            def read(self, size=-1):
                chunk = self.stream.read(size)
                if chunk and not read_started.is_set():
                    # Pause after real bytes have been read, with more still due.
                    observed["first_chunk_size"] = len(chunk)
                    observed["first_chunk_hash"] = hashlib.sha256(chunk).hexdigest()
                    read_started.set()
                    assert resume_read.wait(30), "test did not release the actual RM reader"
                return chunk

        @contextmanager
        def open_with_read_barrier(path, unavailable_code):
            with open_regular(path, unavailable_code) as stream:
                yield PausedReader(stream) if Path(path) == source_path else stream

        monkeypatch.setattr(memory_module, "_open_asset_regular_file", open_with_read_barrier)
        worker = executor.submit(memory.process_asset_intake_once)
        assert read_started.wait(30), "RM did not start reading the scratch source"
        assert not worker.done()
        assert 0 < observed["first_chunk_size"] < len(content)
        assert observed["first_chunk_hash"] == hashlib.sha256(
            content[:observed["first_chunk_size"]]).hexdigest()
        assert memory.query_asset_intake(job.job_ref).status == "processing"

        retained, = _target_cleanup(runtime, dry_run=False)
        assert (retained["action"], retained["reason"]) == ("skipped", "pending_asset_intake"), retained
        assert source_path.read_bytes() == content
        assert not worker.done()

        resume_read.set()
        assert worker.result(timeout=30)
        accepted = memory.query_asset_intake(job.job_ref)
        assert accepted.status == "accepted"
        version_ref = accepted.asset.version_ref
        assert accepted.asset.custody_modes == ("managed",)
        assert memory.materialize_asset(version_ref).content == content

        released, = _target_cleanup(runtime, dry_run=False)
        assert released["action"] == "removed", released
        assert not workspace.exists()
        assert memory.materialize_asset(version_ref).content == content
        repeated, = _target_cleanup(runtime, dry_run=False)
        assert repeated["reason"] == "already_removed", repeated
        print("GC_READING_DEPENDENCY " + json.dumps({
            "job_ref": job.job_ref, "version_ref": version_ref,
            "source_sha256": hashlib.sha256(content).hexdigest(),
            "source_bytes": len(content), "observed_read": observed,
            "retained": retained, "released": released, "repeated": repeated,
            "exact_rm_readback_before_and_after_cleanup": True,
        }, sort_keys=True))
    finally:
        resume_read.set()
        executor.shutdown(wait=True)
        runtime.close()
