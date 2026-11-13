"""An in-flight managed intake does not extend the three-Cycle workspace window."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
from threading import Event

import meta_research.owners.research_memory as memory_module
from meta_research.owners.research_memory import AssetIntakeRequest
from test_completed_workspace_cleanup import _complete, _next_foreground, _target_cleanup


def test_processing_rm_read_does_not_extend_the_workspace_window(tmp_path, monkeypatch):
    runtime, _, manifest, workspace, _, _, _ = _complete(tmp_path, out_of_window=False)
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
        assert (retained["action"], retained["reason"]) == ("skipped", "recent_cycle_window"), retained
        assert source_path.read_bytes() == content
        assert not worker.done()

        _next_foreground(runtime)
        _next_foreground(runtime)
        released, = _target_cleanup(runtime, dry_run=False)
        assert released['action'] == 'removed', released
        assert not workspace.exists()
        assert not worker.done()

        resume_read.set()
        assert worker.result(timeout=30)
        completed_read = memory.query_asset_intake(job.job_ref)
        # The worker may finish through its open descriptor or schedule the
        # existing retry after the source disappears. Neither pins scratch.
        assert completed_read.status in {'accepted', 'failed', 'queued'}
        if completed_read.status == 'queued':
            with runtime._database.read() as connection:
                retry = connection.exec_driver_sql(
                    'SELECT attempt_count,started_at,next_attempt_at,updated_at FROM rm_asset_intakes WHERE job_ref=?',
                    (job.job_ref,)).one()
            assert retry.attempt_count == 1 and retry.started_at is None
            assert retry.next_attempt_at > retry.updated_at
        if completed_read.status == 'accepted':
            assert memory.materialize_asset(completed_read.asset.version_ref).content == content
        retained_asset = next(entry for entry in manifest.entries
                              if entry.declared_relative_path == 'outputs/data/run1.txt')
        assert memory.materialize_asset(retained_asset.binding.version_ref).content == b'36\n'
        repeated, = _target_cleanup(runtime, dry_run=False)
        assert repeated["reason"] == "already_removed", repeated
        print("GC_READING_DEPENDENCY " + json.dumps({
            "job_ref": job.job_ref, "intake_status": completed_read.status,
            "source_sha256": hashlib.sha256(content).hexdigest(),
            "source_bytes": len(content), "observed_read": observed,
            "retained": retained, "released": released, "repeated": repeated,
            "previously_accepted_asset_survives_cleanup": True,
        }, sort_keys=True))
    finally:
        resume_read.set()
        executor.shutdown(wait=True)
        runtime.close()
