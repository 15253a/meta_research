"""New Target captures use native RM directories with exact logical selections."""
import shutil

import pytest
from meta_research.owners.common import OwnerConflict
from meta_research.target_run_finalizer import TargetRunFinalizer
from test_target_root_finalizer import _EvidenceReader, _root_finalizer_fixture


def test_signed_parent_selection_reads_files_single_member_and_empty_directory(tmp_path):
    runtime, lifecycle, manifests, _, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        (workspace / "implementation" / "single").mkdir()
        (workspace / "implementation" / "single" / "requirements.txt").write_bytes(b"numpy\n")
        (workspace / "implementation" / "empty").mkdir()
        finalizer = TargetRunFinalizer(
            lifecycle=lifecycle, memory=manifests,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_EvidenceReader(evidence),
            measurement_authority=runtime.owners.research_graph,
        )
        finalized = finalizer.finalize(handle=handle, evidence=evidence)
        assert finalized.status == "rm_accepted"
        manifest = manifests.query(finalized.manifest_ref)
        parent = next(entry for entry in manifest.entries if entry.declared_relative_path == "implementation")
        memory = runtime.owners.research_memory
        assert memory.describe_asset_export(parent.binding.version_ref).kind == "directory"
        shutil.rmtree(workspace)
        whole = memory.describe_selection(parent.binding)
        assert whole.kind == "directory"
        assert "empty" in whole.directories
        single = memory.describe_selection(parent.binding, "single")
        assert single.kind == "directory" and len(single.entries) == 1
        assert memory.read_selection_page(parent.binding, "single")["kind"] == "directory"
        assert memory.read_selection_page(parent.binding, "single", entry_path="requirements.txt", offset=1, limit=3)["text"] == "ump"
        assert memory.read_selection_page(parent.binding, "single/requirements.txt")["text"] == "numpy\n"
        empty = memory.read_selection_page(parent.binding, "empty")
        assert empty["kind"] == "directory" and empty["entries"] == []
        destination = tmp_path / "selection-export"
        memory.export_selection(parent.binding, "single", destination)
        assert (destination / "requirements.txt").read_bytes() == b"numpy\n"
        assert not (destination / "train.py").exists()
        with pytest.raises(OwnerConflict):
            memory.read_selection_page(parent.binding, "single", entry_path="../train.py")
        from meta_research.owners.research_memory import _managed_object_candidate
        _, _, _, asset_manifest = memory._receipt_verifier._asset_export_source(parent.binding.version_ref)
        leaf = next(entry for entry in asset_manifest["entries"] if entry["path"] == "single/requirements.txt")
        stored = _managed_object_candidate(memory._object_store, leaf["object_path"])
        stored.write_bytes(b"xxxxx\n")
        with pytest.raises(OwnerConflict, match="content_drifted"):
            memory.read_selection_page(parent.binding, "single/requirements.txt")
    finally:
        runtime.close()
