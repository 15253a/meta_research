"""Automatic completion discovery preserves independently addressable products."""
from dataclasses import asdict
import hashlib
import json
import os
from types import SimpleNamespace

import pytest

from meta_research.target_run_finalizer import (
    _pin_workspace_root,
    _system_target_completion_handoff,
)
from meta_research.target_run_runtime_contract import (
    decode_target_completion_handoff,
    validate_target_completion_handoff,
)


@pytest.mark.parametrize("analysis_count,log_count", [(3, 2), (30, 30)])
def test_discovery_preserves_exact_producer_paths_above_64_entries(
    tmp_path, analysis_count, log_count,
):
    (tmp_path / "implementation").mkdir()
    (tmp_path / "implementation/method.md").write_text("Collection method", encoding="utf-8")
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs/result.json").write_text('{"metrics": {}}', encoding="utf-8")
    expected = [("implementation", "implementation"), ("result", "outputs/result.json")]
    for role, root, count in (
        ("data", "outputs/data", 30),
        ("checkpoint", "outputs/checkpoints", 30),
        ("analysis", "outputs/analysis", analysis_count),
        ("log", "logs", log_count),
    ):
        for ordinal in range(count):
            relative_path = f"{root}/producer-{ordinal:02d}"
            directory = tmp_path / relative_path
            directory.mkdir(parents=True)
            (directory / "retained.txt").write_text(f"{role} {ordinal}", encoding="utf-8")
            expected.append((role, relative_path))

    handle = SimpleNamespace(target_ref="target:collection", target_run_ref="run:collection")
    final_text = "Retained the selected research products."
    evidence = SimpleNamespace(final_text=final_text,
                               final_text_sha256=hashlib.sha256(final_text.encode()).hexdigest())
    pinned = _pin_workspace_root("workspace:collection", tmp_path)
    try:
        handoff = _system_target_completion_handoff(
            handle=handle, evidence=evidence, root_descriptor=pinned.descriptor,
        )
    finally:
        os.close(pinned.descriptor)

    assert len(handoff.artifacts) == 62 + analysis_count + log_count > 64
    assert [(entry.role, entry.relative_path) for entry in handoff.artifacts] == expected
    assert validate_target_completion_handoff(handoff,
        expected_target_ref=handle.target_ref,
        expected_target_run_ref=handle.target_run_ref) == handoff
    assert decode_target_completion_handoff(json.dumps(asdict(handoff))) == handoff
    selected_files = set()
    for entry in handoff.artifacts:
        path = tmp_path / entry.relative_path
        selected_files.update(path.rglob("*") if path.is_dir() else (path,))
    assert {path for path in selected_files if path.is_file()} == {
        path for path in tmp_path.rglob("*") if path.is_file()
    }
