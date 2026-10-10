import os
import copy
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from meta_research.companion import CodexCompanionAdapter
from meta_research.creation_basis import FirstQuestionSynthesisRequest, InitializationUnderstandingRequest, empty_manifest, empty_understanding, first_creation_instructions
from meta_research.protected_creation_runtime import NativeCreationCall
from meta_research.quest_drafting import DraftingUnavailable
from meta_research.owners.common import OwnerConflict
from meta_research.web import create_app
from meta_research.work_material_contract import CREATION_MATERIAL_OPERATION_IDS
from test_creation_material_consumption import _call, _open, _value
from test_root_workspace import _make


pytestmark = pytest.mark.skipif(os.name != "posix" or os.geteuid() != 0, reason="requires actual protected creation runtime")

QUESTION = {"title": "Compare the calibration", "unknown_statement": "Does the cold condition reproduce the effect?",
    "answer_shape": "A controlled comparison", "applicability_scope": "Small calibration experiment",
    "background_context": "Reported original16 and trial25", "requirements_constraints": "Keep conditions explicit"}


class ProtectedCreationFixture(CodexCompanionAdapter):
    external_model_calls = False

    def __init__(self, workspace, *, selection="original", original_custody="managed"):
        super().__init__(workspace, executable="/bin/bash")
        self.selection = selection
        self.original_custody = original_custody
        self.calls = []

    def _invoke(self, **arguments):
        runner = arguments["invocation_runner"]
        work = runner.work
        self.calls.append((arguments, work))
        guidance = runner.read_only_inputs[:3]
        assert len(runner.read_only_inputs) >= 3
        completed = work.run(NativeCreationCall(("/bin/bash", "-c",
            f"cat \"$@\"; mkdir -p /workspace/{work.relative_directory}; printf 'trial 25\\n' > /workspace/{work.relative_directory}/trial.txt", "guidance", *(str(path) for path in guidance)),
            arguments["prompt"], None, {}, runner.read_only_inputs, ()))
        assert completed.returncode == 0
        assert "# Understand existing work" in completed.stdout
        for path in guidance:
            assert path.read_text(encoding="utf-8") in completed.stdout
        if arguments["operation_name"] == "initialization-understanding":
            prompt = arguments["prompt"]
            loaded = json.JSONDecoder().raw_decode(prompt[prompt.index("{"):])[0]
            assert loaded["instruction_bundle"] == first_creation_instructions()
        assert arguments["operation_name"] in {"initialization-understanding", "first-question-synthesis", "companion-turn"}
        assert "third-party Python scientific packages" in runner.runtime_conditions
        assert "protected_creation" not in str(self._runner)
        class Connection:
            token = arguments["mcp_token"]
        gateway = self._workspaces._creation_gateway
        if work.read_basis is not None and not work.read_basis_historical:
            references = []
        else:
            listing = _value(_call(gateway, Connection(), CREATION_MATERIAL_OPERATION_IDS[0]))
            references = listing["references"]
        reassessment = None
        if work.read_basis_historical:
            prompt = arguments["prompt"]
            reassessment = json.JSONDecoder().raw_decode(prompt[prompt.index("{"):])[0]["reassessment"]
        inherited = []
        old = None if reassessment is None else reassessment["prior_understanding"]
        retained_refs = set()
        understanding = empty_understanding()
        for reference in references:
            ref = reference["reference_ref"]
            eligible = [] if reassessment is None else [item["witness"] for item in reassessment["evidence_facts"]
                if item["live_source_eligible"] and item["witness"]["reference_ref"] == ref]
            if eligible:
                prior_coverage = next(item for item in old["coverage"] if item["material_key"] == ref)
                old_refs = {item["witness_ref"] for item in eligible}
                if all(citation["witness_ref"] in old_refs for citation in prior_coverage["read_ranges"]):
                    understanding["coverage"].append(copy.deepcopy(prior_coverage))
                    inherited.extend({"kind": "live_source", "witness_ref": item["witness_ref"], "selected_material_key": None} for item in eligible)
                    retained_refs.update(item["ref"] for field in ("material_composition", "work_already_done", "claims_and_conditions", "conflicts", "gaps", "unfinished_questions")
                        for item in old[field] if all(citation["witness_ref"] in old_refs for citation in item["sources"]))
                    continue
            entries = _value(_call(gateway, Connection(), CREATION_MATERIAL_OPERATION_IDS[0], reference_ref=ref))["entries"]
            entry = entries[0]
            path = entry["path"]
            if reference["source"]["kind"] == "directory":
                entry = next(item for item in entries if item["path"] == "note.txt")
                path = "note.txt"
            page = _value(_call(gateway, Connection(), CREATION_MATERIAL_OPERATION_IDS[1], reference_ref=ref,
                path=path, observation_ref=entry["observation"]["observation_ref"], max_bytes=65536))
            citation = {"witness_ref": page["read_witness"]["witness_ref"], "location": "original note"}
            understanding["coverage"].append({"material_key": ref, "kind": "partial" if reference["source"]["kind"] == "directory" else "read",
                "read_ranges": [citation], "unread_description": "Large sibling was not read." if reference["source"]["kind"] == "directory" else ""})
            statement_ref = ref if reassessment is None else ref + ":" + work.operation.operation_ref
            understanding["claims_and_conditions"].append({"ref": statement_ref, "text": page["text"], "kind": "reported_work",
                "conditions": ["Calibration condition recorded in the note."], "sources": [citation]})
            if self.selection in {"original", "both"}:
                understanding["selection"].append({"source": {"kind": "original_file", "reference_ref": ref,
                    "path": path, "observation_ref": entry["observation"]["observation_ref"]}, "custody": self.original_custody, "reason": "Keep the original."})
        if self.selection in {"result", "both"} and reassessment is None:
            understanding["selection"].append({"source": {"kind": "work_file", "work_ref": work.work_ref, "path": "trial.txt", "trial_ref": None},
                "custody": "linked_local", "reason": "Keep the selected trial."})
        if arguments["operation_name"] == "initialization-understanding":
            from jsonschema import Draft202012Validator
            if reassessment is not None:
                keys = []
                for source in reassessment["prior_sources"]:
                    if source["binding"] is None:
                        continue
                    try:
                        self._workspaces._hc._research_memory.creation_bases.read_source(
                            self._workspaces._hc._research_memory.creation_bases.query(work.read_basis["basis_ref"]), source["material_key"], 0, 1)
                    except OwnerConflict:
                        continue
                    if not any(item["source"] == source["source"] for item in understanding["selection"]):
                        keys.append(source["material_key"])
                replacements = [item["ref"] for item in understanding["claims_and_conditions"]]
                delta = {"predecessor": reassessment["predecessor"], "change_assessment": "Apply the current goal and read only changed entrances.",
                    "decisions": [{"prior_statement_ref": item["ref"], "disposition": "retain" if item["ref"] in retained_refs else "replace",
                        "explanation": "Original observations remain valid." if item["ref"] in retained_refs else "Changed material was read in this operation.",
                        "applicable_conditions": item["conditions"], "affected_scope": "" if item["ref"] in retained_refs else "Changed material",
                        "creation_limit": "none", "replacement_refs": [] if item["ref"] in retained_refs else replacements}
                        for field in ("material_composition", "work_already_done", "claims_and_conditions", "conflicts", "gaps", "unfinished_questions") for item in old[field]],
                    "additions": understanding, "inherited_evidence": inherited, "inherited_selection_keys": keys,
                    "literature_decisions": [{"snapshot_ref": ref, "snapshot_hash": item["snapshot"]["snapshot_hash"],
                        "disposition": "retain", "applicable_conditions": ["Existing condition-specific literature."],
                        "affected_scope": "Current goal still needs a controlled comparison.", "limitations": "Existing snapshot only, no new search."}
                        for ref, item in reassessment["prior_literature"].items()]}
                Draft202012Validator(arguments["schema"]).validate(delta)
                return delta, "native-parent", ""
            Draft202012Validator(arguments["schema"]).validate(understanding)
            return understanding, "native-parent", ""
        if arguments["operation_name"] == "companion-turn" and work.read_basis is None:
            return {"reply": "The bounded notes support an editable calibration comparison."}, "native-parent", ""
        basis = self._workspaces._hc._research_memory.creation_bases.query(work.read_basis["basis_ref"], work.read_basis["basis_hash"])
        value = _value(_call(gateway, Connection(), "research_memory.creation_basis.read", basis_ref=basis["basis_ref"],
            expected_basis_hash=basis["basis_hash"], view="understanding", limit=16384))
        assert value["basis_hash"] == basis["basis_hash"]
        for source in basis["sources"]:
            if source["binding"] is not None:
                version = source["binding"]["version_ref"]
                page = _value(_call(gateway, Connection(), "research_memory.content.read", source_ref=version, version_ref=version))
                assert page["text"]
        for inherited_snapshot in work.read_inherited_literature:
            version = inherited_snapshot["snapshot_ref"]
            page = _value(_call(gateway, Connection(), "research_memory.content.read", source_ref=version, version_ref=version))
            assert page["version_ref"] == version
        denied = _call(gateway, Connection(), "research_memory.content.read", source_ref="asset_version_foreign", version_ref="asset_version_foreign")
        assert denied["isError"]
        revision = None
        if work.read_literature is not None:
            snapshot = work.read_literature["snapshot_ref"]
            summary = _value(_call(gateway, Connection(), "research_memory.content.read", source_ref=snapshot, version_ref=snapshot))
            assert summary
            revised = copy.deepcopy(basis["understanding"])
            claim = revised["claims_and_conditions"][0]
            claim["text"] = "The literature counterproof qualifies the original16 claim: a condition-specific comparison remains necessary."
            claim["conditions"].append("The accepted paper does not establish transfer to the cold condition.")
            original_ref = next(item["reference_ref"] for item in basis["material_references"]["references"])
            revision = {"understanding": revised, "corrections": [{"prior_statement_ref": claim["ref"],
                "disposition": "qualified", "explanation": "The accepted paper supplies a counterexample to unrestricted transfer.",
                "original_sources": [original_ref], "literature_sources": [{"paper_id": "doi:10.1000/example.one", "locator": "loc-1"}]}],
                "search_assessment": "One accepted paper qualifies transfer; the second has no accepted full text."}
        if arguments["operation_name"] == "companion-turn":
            return {"reply": "The bounded notes and accepted evidence support an editable calibration comparison.",
                **({} if revision is None else {"revision": revision})}, "native-parent", ""
        return {"proposal_fork_native_session_ref": "native-child", "result": {"content": QUESTION, "revision": revision}}, "native-parent", ""


def test_actual_adapter_methods_share_current_protected_gateway_and_readers(tmp_path):
    source = tmp_path / "original"
    source.mkdir()
    (source / "note.txt").write_text("Calibration16 only applies in room conditions.")
    with (source / "large.bin").open("wb") as stream:
        stream.truncate(4 * 1024**3)
    runtime = _make(tmp_path / "runtime")
    try:
        with TestClient(create_app(runtime, base_url="http://testserver", control_key="control")) as client:
            anchor, binding, _ = _open(client, runtime, source, "adapter")
            roots = runtime.root_workspaces
            roots.configure_creation_runtime(executable="/bin/bash", credentials_home=tmp_path / "credentials")
            adapter = ProtectedCreationFixture(tmp_path / "adapter", selection="both")
            adapter.bind_workspaces(roots)
            inputs = runtime.owners.human_collaboration.creation_material_snapshot(anchor)
            request = InitializationUnderstandingRequest(anchor.ref, anchor.draft_revision, anchor.draft_hash, {}, empty_manifest(),
                "adapter-understanding", binding.location.root_session_ref, None, inputs=inputs)
            result = adapter.understand_initialization(request)
            assert len(result.input_identity.consumed) == 1
            memory = runtime.owners.research_memory.creation_bases
            basis = memory.accept_reference_prepared(request, result)
            context = memory.project(basis, None, binding)
            synthesis = adapter.synthesize_first_question(FirstQuestionSynthesisRequest(anchor.ref, anchor.draft_revision,
                anchor.draft_hash, {}, "adapter-synthesis", binding.location.root_session_ref, result.companion_native_session_ref,
                basis, context, None))
            assert synthesis.content == QUESTION
            assert synthesis.input_identity.consumed == ()
            assert synthesis.work.runtime.jail_root == result.work.runtime.jail_root
            assert {Path(path).name for path in adapter.calls[1][0]["invocation_runner"].read_only_inputs} >= {"sources.json", "basis.json"}
            assert len(adapter.calls[1][0]["authorized_operation_ids"]) == 6
            assert not adapter._creation_calls
            assert adapter.cancel_job("adapter-synthesis") is True
            assert (source / "large.bin").stat().st_blocks == 0
    finally:
        runtime.close()


def test_prelaunch_cancellation_is_applied_to_understanding_suboperation(tmp_path):
    source = tmp_path / "original.txt"
    source.write_text("original16")
    runtime = _make(tmp_path / "runtime")
    try:
        with TestClient(create_app(runtime, base_url="http://testserver", control_key="control")) as client:
            anchor, binding, _ = _open(client, runtime, source, "cancel")
            roots = runtime.root_workspaces
            roots.configure_creation_runtime(executable="/bin/bash", credentials_home=tmp_path / "credentials")
            adapter = CodexCompanionAdapter(tmp_path / "adapter", executable="/bin/bash")
            adapter.bind_workspaces(roots)
            adapter.cancel_job("cancelled-parent")
            inputs = runtime.owners.human_collaboration.creation_material_snapshot(anchor)
            request = InitializationUnderstandingRequest(anchor.ref, anchor.draft_revision, anchor.draft_hash, {}, empty_manifest(),
                "cancelled-parent:understanding", binding.location.root_session_ref, None, inputs=inputs)
            with pytest.raises(DraftingUnavailable, match="codex_cli_stopped"):
                adapter.understand_initialization(request)
            assert adapter.cancel_job("cancelled-parent") is True
            assert source.read_text() == "original16"
    finally:
        runtime.close()
