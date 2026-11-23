from __future__ import annotations

import os
from pathlib import Path
import threading
import time

import pytest

import meta_research.provider_supervisor as provider_supervisor
from meta_research.provider_supervisor import (
    _write_signed_replacement,
    read_transport_envelope,
)


@pytest.mark.skipif(os.name != "nt", reason="Windows file-sharing behavior")
def test_signed_replacement_waits_for_reader_to_release_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "supervisor-heartbeat.json"
    key = b"k" * 32
    _write_signed_replacement(path, {"schema_ref": "test", "version": 1}, key)
    ready = threading.Event()
    release = threading.Event()

    def hold_reader() -> None:
        with path.open("rb"):
            ready.set()
            release.wait(timeout=5)

    holder = threading.Thread(target=hold_reader)
    holder.start()
    assert ready.wait(timeout=5)
    errors: list[BaseException] = []

    def replace_snapshot() -> None:
        try:
            _write_signed_replacement(path, {"schema_ref": "test", "version": 2}, key)
        except BaseException as error:  # pragma: no cover - failure is asserted below
            errors.append(error)

    writer = threading.Thread(target=replace_snapshot)
    writer.start()
    time.sleep(0.05)
    release.set()
    writer.join(timeout=5)
    holder.join(timeout=5)
    assert not writer.is_alive()
    assert not holder.is_alive()
    assert errors == []
    assert read_transport_envelope(path, key)["version"] == 2


@pytest.mark.skipif(os.name != "nt", reason="Windows file-sharing behavior")
def test_signed_replacement_retries_transient_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "supervisor-heartbeat.json"
    key = b"k" * 32
    _write_signed_replacement(path, {"schema_ref": "test", "version": 1}, key)
    real_replace = provider_supervisor.os.replace
    attempts = 0

    def replace_with_one_transient_failure(source: str, destination: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("reader still holds the old snapshot")
        real_replace(source, destination)

    monkeypatch.setattr(
        provider_supervisor.os, "replace", replace_with_one_transient_failure
    )
    _write_signed_replacement(path, {"schema_ref": "test", "version": 2}, key)

    assert attempts == 2
    assert read_transport_envelope(path, key)["version"] == 2
