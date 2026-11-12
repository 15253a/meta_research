"""Directory creation references navigate only to files already retained in their basis."""
import copy
import os

import pytest

from meta_research.creation_basis import InitializationUnderstandingResult
from meta_research.creation_inputs import OriginalFile
from test_creation_reference_basis import _understood
from test_dataset_effect_scope_recovery import _scope
from test_public_first_question_deepfetch import _authenticate, _write_headers
from test_public_quest_initialization import _confirmation_payload
from test_public_reference_creation import complete, reference_ready, reference_runtime
from test_root_workspace import _accepted, _call, _channel, _external_context


pytestmark = pytest.mark.skipif(os.name != "posix" or os.geteuid() != 0,
    reason="requires actual protected creation runtime")


def test_directory_reference_navigates_retained_files_through_exact_authorized_readers(tmp_path):
    source = tmp_path / "original"
    source.mkdir()
    original = b"Original16 applies only at room temperature.\n"
    (source / "note.txt").write_bytes(original)
    (source / "unselected.txt").write_text("Not selected for custody.")
    runtime, _ = reference_runtime(tmp_path / "runtime", selection="both")
    client, headers = _authenticate(runtime)
    try:
        ready = reference_ready(runtime, client, headers, source)
        endpoint = f"/api/v1/quest-initializations/{ready['initialization_id']}"
        confirmed = client.post(endpoint + "/confirmation", headers=_write_headers(headers, "confirm"),
            json=_confirmation_payload(ready))
        assert confirmed.status_code == 202, confirmed.text
        completed = complete(runtime, client, endpoint)
        memory = runtime.owners.research_memory.creation_bases
        basis = memory.for_question(completed["question_ref"], completed["quest_ref"])
        before = copy.deepcopy(basis)
        views = memory.source_views(basis)
        directory = next(item for item in views if item["binding"] is None)
        selected = next(item for item in views if item.get("source", {}).get("kind") == "original_file")
        channel = _channel(runtime, _external_context(_scope(runtime, quest_ref=completed["quest_ref"])))
        arguments = {"basis_ref": basis["basis_ref"], "expected_basis_hash": basis["basis_hash"],
            "view": "source", "material_key": directory["material_key"]}

        # Replay the real failing source call before asserting corrected navigation.
        refused = _call(runtime, channel, "research_memory.creation_basis.read", **arguments)
        assert refused["isError"] and "creation_source_file_required" in str(refused)
        assert directory["reader"] is None, "A directory cannot advertise a file-content reader."
        assert directory["read_status"] == "directory_reference"
        retained, = directory["retained_files"]
        assert retained == {key: selected[key] for key in ("material_key", "relative_path", "binding", "reader")}
        assert "unselected.txt" not in str(retained) and "trial.txt" not in str(retained)
        assert selected["material_key"] in str(refused)
        for entry_path in ("note.txt", "unselected.txt", "../outside.txt", str(tmp_path / "outside.txt")):
            rejected = _call(runtime, channel, "research_memory.creation_basis.read", **arguments, entry_path=entry_path)
            assert rejected["isError"] and "creation_source_file_required" in str(rejected)
            assert selected["material_key"] in str(rejected)

        reader = dict(retained["reader"])
        operation = reader.pop("operation")
        assert _accepted(_call(runtime, channel, operation, **reader))["text"] == original.decode()
        selected_page = _accepted(_call(runtime, channel, "research_memory.creation_basis.read",
            **{**arguments, "material_key": selected["material_key"]}))
        assert selected_page["text"] == original.decode()
        invalid_hash = _call(runtime, channel, "research_memory.creation_basis.read",
            **{**arguments, "expected_basis_hash": "0" * 64})
        assert invalid_hash["isError"] and "creation_basis_hash_invalid" in str(invalid_hash)
        foreign = _channel(runtime, _external_context(_scope(runtime, quest_ref="other-quest",
            run_ref="foreign-root", root_ref="foreign-root")))
        rejected = _call(runtime, foreign, "research_memory.creation_basis.read", **arguments)
        assert rejected["isError"] and "creation_basis_unbound" in str(rejected)
        rejected = _call(runtime, foreign, operation, **reader)
        assert rejected["isError"] and "asset_quest_scope_invalid" in str(rejected)

        # Other original files are separate references, even when their relative path matches.
        foreign_source = tmp_path / "other-original"
        foreign_source.mkdir()
        (foreign_source / "note.txt").write_text("Different original and creation basis.")
        request, understanding, identity, work, reference, entry, _ = _understood(
            runtime, client, foreign_source, "other-basis")
        understanding["selection"] = [{"source": OriginalFile(reference["reference_ref"], "note.txt",
            entry["observation"]["observation_ref"]).as_dict(), "custody": "managed", "reason": "Other basis only."}]
        other_basis = memory.accept_reference_prepared(request,
            InitializationUnderstandingResult(understanding, input_identity=identity, work=work))
        other_file = next(item for item in other_basis["sources"] if item.get("source", {}).get("kind") == "original_file")
        assert other_file["material_key"] not in str(directory["retained_files"])
        rejected = _call(runtime, channel, "research_memory.creation_basis.read", **{
            **arguments, "basis_ref": other_basis["basis_ref"], "expected_basis_hash": other_basis["basis_hash"],
            "material_key": other_file["material_key"]})
        assert rejected["isError"] and "creation_basis_unbound" in str(rejected)
        rejected = _call(runtime, channel, "research_memory.creation_basis.read",
            **{**arguments, "material_key": other_file["material_key"]})
        assert rejected["isError"] and "creation_source_unbound" in str(rejected)
        assert memory.query(basis["basis_ref"], basis["basis_hash"]) == before
    finally:
        client.close()
        runtime.close()


def test_directory_without_selected_original_keeps_coverage_without_inventing_a_reader(tmp_path):
    source = tmp_path / "original"
    source.mkdir()
    (source / "note.txt").write_text("Original16; deliberately not retained.")
    runtime, _ = reference_runtime(tmp_path / "runtime", selection="result")
    client, headers = _authenticate(runtime)
    try:
        ready = reference_ready(runtime, client, headers, source)
        directory = next(item for item in ready["creation_basis"]["sources"] if item["binding"] is None)
        assert directory["reader"] is None
        assert directory["retained_files"] == []
        assert directory["coverage"]["kind"] == "partial"
        assert directory["coverage"]["read_ranges"]
        assert directory["coverage"]["unread_description"]
    finally:
        client.close()
        runtime.close()
