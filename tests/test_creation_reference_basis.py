import copy
import hashlib
import os

from fastapi.testclient import TestClient
import pytest

from meta_research.creation_basis import InitializationUnderstandingRequest, InitializationUnderstandingResult, empty_manifest, empty_understanding
from meta_research.creation_inputs import CreationInputIdentity, OriginalFile, WorkFile
from meta_research.owners.common import OwnerConflict
from meta_research.work_material_contract import CREATION_MATERIAL_OPERATION_IDS
from meta_research.web import create_app
from test_creation_material_consumption import _call, _open, _value
from test_creation_material_copy import _native
from test_root_workspace import _make


pytestmark = pytest.mark.skipif(os.name != "posix" or os.geteuid() != 0, reason="requires protected native execution")


def _understood(runtime, client, source, key):
    anchor, binding, reference = _open(client, runtime, source, key)
    human, roots = runtime.owners.human_collaboration, runtime.root_workspaces
    roots.configure_creation_runtime(executable="/bin/bash", credentials_home=source.parent / "credentials")
    inputs = human.creation_material_snapshot(anchor)
    work = roots.protected_creation(binding, inputs, operation_ref=key + "-understand")
    assert _native(work, f"mkdir -p /workspace/{work.relative_directory}; printf 'result 25\\n' > /workspace/{work.relative_directory}/result.txt").returncode == 0
    access = work.channel()
    class Connection:
        token = access.token
    gateway = runtime.harnesses._gateway
    listing = _value(_call(gateway, Connection(), CREATION_MATERIAL_OPERATION_IDS[0], reference_ref=reference["reference_ref"]))
    entry = next(item for item in listing["entries"] if item.get("name") == "note.txt") if source.is_dir() else listing["entries"][0]
    path = "note.txt" if source.is_dir() else ""
    page = _value(_call(gateway, Connection(), CREATION_MATERIAL_OPERATION_IDS[1],
        reference_ref=reference["reference_ref"], path=path, observation_ref=entry["observation"]["observation_ref"], max_bytes=65536))
    identity = work.seal()
    citation = {"witness_ref": page["read_witness"]["witness_ref"], "location": "note lines 1-2"}
    understanding = empty_understanding()
    understanding["coverage"] = [{"material_key": reference["reference_ref"], "kind": "partial" if source.is_dir() else "read",
        "read_ranges": [citation], "unread_description": "Other files were not read." if source.is_dir() else ""}]
    understanding["claims_and_conditions"] = [{"ref": "observation", "text": "The original reports 16, while the trial reports 25.",
        "kind": "reported_work", "conditions": ["Small program only."], "sources": [citation]}]
    request = InitializationUnderstandingRequest(anchor.ref, anchor.draft_revision, anchor.draft_hash,
        {}, empty_manifest(), work.operation.operation_ref, binding.location.root_session_ref, None, inputs=inputs)
    return request, understanding, identity, work, reference, entry, binding


@pytest.mark.parametrize("selection", ["original", "result", "both", "neither"])
def test_exact_original_result_selection_custody_and_small_projection(tmp_path, selection):
    source = tmp_path / "original"
    source.mkdir()
    original = b"original 16\n"
    (source / "note.txt").write_bytes(original)
    with (source / "large.bin").open("wb") as stream:
        stream.truncate(4 * 1024**3)
    runtime = _make(tmp_path / "runtime")
    try:
        with TestClient(create_app(runtime, base_url="http://testserver", control_key="control")) as client:
            request, value, identity, work, ref, entry, binding = _understood(runtime, client, source, selection)
            if selection in {"original", "both"}:
                value["selection"].append({"source": OriginalFile(ref["reference_ref"], "note.txt", entry["observation"]["observation_ref"]).as_dict(),
                    "custody": "managed", "reason": "Retain the supplied measurement."})
            if selection in {"result", "both"}:
                value["selection"].append({"source": WorkFile(work.work_ref, "result.txt").as_dict(),
                    "custody": "linked_local", "reason": "Retain the explicitly selected trial result."})
            memory = runtime.owners.research_memory.creation_bases
            basis = memory.accept_reference_prepared(request, InitializationUnderstandingResult(value, input_identity=identity, work=work))
            selected = [item for item in basis["sources"] if item["binding"] is not None]
            assert len(selected) == len(value["selection"])
            assert CreationInputIdentity.from_dict(basis["input_identity"]) == identity
            assert memory.prepared(request.initialization_id, request.draft_revision, request.draft_hash, request.inputs)["basis_hash"] == basis["basis_hash"]
            assert memory.accept_reference_prepared(request, InitializationUnderstandingResult(value, input_identity=identity, work=work))["basis_ref"] == basis["basis_ref"]
            for item in selected:
                expected = original if item["source"]["kind"] == "original_file" else b"result 25\n"
                assert memory.read_source(basis, item["material_key"])["content"] == expected
                assert item["sha256"] == hashlib.sha256(expected).hexdigest()
                assert item["intake_state"] == "accepted"
            projected = memory.project(basis, None, binding)
            assert {item["path"].rsplit("/", 1)[-1] for item in projected["manifest"]} == {"basis.json", "understanding.json", "sources.json"}
            assert not list(binding.directory.glob(".creation-context/sources/*"))
            assert (source / "note.txt").read_bytes() == original
            assert (source / "large.bin").stat().st_blocks == 0
            assert not list(work.runtime.jail_root.rglob("large.bin"))
            copied_value = copy.deepcopy(value)
            copied_value["claims_and_conditions"][0]["sources"][0]["witness_ref"] = "other-session"
            with pytest.raises(OwnerConflict, match="creation_understanding_invalid"):
                memory.accept_reference_prepared(request, InitializationUnderstandingResult(copied_value, input_identity=identity, work=work))
            (source / "note.txt").write_bytes(b"changed 36\n")
            assert memory.prepared(request.initialization_id, request.draft_revision, request.draft_hash, request.inputs) is None
    finally:
        runtime.close()


def test_selected_original_linked_custody_reports_drift_and_work_symlink_is_refused(tmp_path):
    source = tmp_path / "original.txt"
    source.write_bytes(b"original 16\n")
    runtime = _make(tmp_path / "runtime")
    try:
        with TestClient(create_app(runtime, base_url="http://testserver", control_key="control")) as client:
            request, value, identity, work, ref, entry, _ = _understood(runtime, client, source, "linked")
            value["selection"] = [{"source": OriginalFile(ref["reference_ref"], "", entry["observation"]["observation_ref"]).as_dict(),
                "custody": "linked_local", "reason": "Keep the exact original locator."}]
            memory = runtime.owners.research_memory.creation_bases
            basis = memory.accept_reference_prepared(request, InitializationUnderstandingResult(value, input_identity=identity, work=work))
            selected = next(item for item in basis["sources"] if item["binding"] is not None)
            assert memory.read_source(basis, selected["material_key"])["content"] == b"original 16\n"
            os.symlink(source, work.work_directory / "unsafe.txt")
            with pytest.raises(OwnerConflict, match="creation_work_unsafe"):
                work.retain_source(WorkFile(work.work_ref, "unsafe.txt").as_dict())
            assert source.read_bytes() == b"original 16\n"
            source.write_bytes(b"changed 36\n")
            with pytest.raises(OwnerConflict):
                memory.read_source(basis, selected["material_key"])
    finally:
        runtime.close()
