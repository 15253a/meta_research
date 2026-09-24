"""A full disk at Target handoff is a storage fault, never corrupt evidence."""
import errno
from pathlib import Path

import pytest

from meta_research.owners.common import OwnerConflict
from test_target_input_startup_cost import (
    accepted_input, registered_input, _streamed_workspace,
)
from test_target_root_frozen_inputs import _fixture


@pytest.mark.parametrize("error_number", [errno.ENOSPC, errno.EDQUOT])
def test_real_input_export_preserves_destination_storage_failure(
    tmp_path, accepted_input, monkeypatch, error_number,
):
    handle, frozen, workspace = _streamed_workspace(tmp_path, accepted_input)
    original_open = Path.open
    writes = []

    class FullDestination:
        def __init__(self, output):
            self.output = output

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            self.output.close()

        def __getattr__(self, name):
            return getattr(self.output, name)

        def write(self, _content):
            writes.append(error_number)
            raise OSError(error_number, "isolated destination is full")

    def open_destination(path, mode="r", *args, **kwargs):
        output = original_open(path, mode, *args, **kwargs)
        if mode == "wb" and path.parent.name.startswith(".asset-export-"):
            return FullDestination(output)
        return output

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "open", open_destination)
        with pytest.raises(OwnerConflict, match="target_run_workspace_input_storage_unavailable") as raised:
            workspace.materialize_target_workspace_inputs(
                handle=handle, accepted_target_commit_inputs=(frozen,),
            )
    assert writes == [error_number], "A full destination must not trigger source fallback"
    cause = raised.value
    while cause.__cause__ is not None:
        cause = cause.__cause__
    assert isinstance(cause, OSError) and cause.errno == error_number
    assert not list(tmp_path.rglob(".asset-export-*")), "Atomic export must clean staging"

    # The same accepted inputs can resume as soon as destination storage works.
    paths = workspace.materialize_target_workspace_inputs(
        handle=handle, accepted_target_commit_inputs=(frozen,),
    )
    assert paths and Path(paths[0]).is_file()
    workspace.verify_target_workspace_inputs(
        handle=handle, accepted_target_commit_inputs=(frozen,),
    )


@pytest.mark.parametrize("write_scope", ["frozen", "pointer"])
def test_partial_input_metadata_write_can_resume_without_repair(
    tmp_path, monkeypatch, write_scope,
):
    handle, frozen, workspace, *_rest = _fixture(tmp_path)
    original_open = Path.open
    writes = []

    class InterruptedDestination:
        def __init__(self, output):
            self.output = output

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            self.output.close()

        def __getattr__(self, name):
            return getattr(self.output, name)

        def write(self, content):
            self.output.write(content[:7])
            self.output.flush()
            writes.append(len(content))
            raise OSError(errno.EDQUOT, "interrupted isolated metadata write")

    def open_destination(path, mode="r", *args, **kwargs):
        output = original_open(path, mode, *args, **kwargs)
        pointer = path.is_relative_to(workspace._root)
        if mode == "wb" and pointer == (write_scope == "pointer"):
            return InterruptedDestination(output)
        return output

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "open", open_destination)
        with pytest.raises((OwnerConflict, OSError)) as raised:
            workspace.materialize_target_workspace_inputs(
                handle=handle, accepted_target_commit_inputs=(frozen,),
            )
    assert len(writes) == 1
    assert not list(tmp_path.rglob(".input-metadata-*"))

    # A partially written manifest must never be published at its final path:
    # the next ordinary wake must work without deleting or editing evidence.
    paths = workspace.materialize_target_workspace_inputs(
        handle=handle, accepted_target_commit_inputs=(frozen,),
    )
    assert paths
    workspace.verify_target_workspace_inputs(
        handle=handle, accepted_target_commit_inputs=(frozen,),
    )
    assert isinstance(raised.value, OwnerConflict)
    assert raised.value.code == "target_run_workspace_input_storage_unavailable"
    assert isinstance(raised.value.__cause__, OSError)
    assert raised.value.__cause__.errno == errno.EDQUOT
