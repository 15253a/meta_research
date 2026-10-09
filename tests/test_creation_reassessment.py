import copy
import os

from fastapi.testclient import TestClient
import pytest

from meta_research.creation_basis import (
    InitializationUnderstandingRequest, InitializationUnderstandingResult,
    empty_manifest, empty_understanding,
)
from meta_research.owners.common import OwnerConflict
from meta_research.web import create_app
from meta_research.work_material_contract import CREATION_MATERIAL_OPERATION_IDS
from test_creation_material_consumption import _call, _open, _value
from test_creation_material_copy import _native
from test_root_workspace import _make


pytestmark = pytest.mark.skipif(os.name != "posix" or os.geteuid() != 0, reason="requires protected native execution")


def read_materials(runtime, binding, inputs, operation_ref, reference, paths):
    work = runtime.root_workspaces.protected_creation(binding, inputs, operation_ref=operation_ref)
    assert _native(work, "true").returncode == 0
    access = work.channel()
    class Connection:
        token = access.token
    gateway = runtime.harnesses._gateway
    listing = _value(_call(gateway, Connection(), CREATION_MATERIAL_OPERATION_IDS[0], reference_ref=reference))
    citations = {}
    for path in paths:
        entry = next(item for item in listing["entries"] if item.get("name") == path)
        page = _value(_call(gateway, Connection(), CREATION_MATERIAL_OPERATION_IDS[1],
            reference_ref=reference, path=path, observation_ref=entry["observation"]["observation_ref"], max_bytes=65536))
        citations[path] = {"witness_ref": page["read_witness"]["witness_ref"], "location": path}
    identity = work.seal()
    request = InitializationUnderstandingRequest(inputs.anchor.ref, inputs.anchor.draft_revision,
        inputs.anchor.draft_hash, {"goal": "Compare the measurements."}, empty_manifest(), operation_ref,
        binding.location.root_session_ref, None, inputs=inputs)
    return request, work, identity, citations


def statement(ref, text, citation):
    return {"ref": ref, "text": text, "kind": "reported_work", "conditions": ["Small supplied trial."], "sources": [citation]}


def reassessment_delta(memory, predecessor, reference, citations, dispositions=None):
    dispositions = dispositions or {"a": "retain", "b": "replace"}
    additions = empty_understanding()
    additions["coverage"] = [{"material_key": reference, "kind": "partial", "read_ranges": list(citations.values()),
        "unread_description": "c.txt has not been read. It remains future research."}]
    if dispositions.get("b") == "replace":
        additions["claims_and_conditions"] = [statement("b-new", "B reports 36.", citations["b.txt"])]
    decisions = [{"prior_statement_ref": ref, "disposition": disposition, "explanation": "Local change assessment.",
        "applicable_conditions": ["Small supplied trial."], "affected_scope": "" if disposition == "retain" else "b.txt",
        "creation_limit": "future_research" if disposition in {"needs_recheck", "out_of_scope"} else "none",
        "replacement_refs": ["b-new"] if disposition == "replace" else []} for ref, disposition in dispositions.items()]
    original = {item["witness_ref"]: item for item in predecessor["input_identity"]["consumed"]}
    return {"predecessor": memory.reference(predecessor), "change_assessment": "Only B changed; C remains unread.",
        "decisions": decisions, "additions": additions,
        "inherited_evidence": [{"kind": "live_source", "witness_ref": citation["witness_ref"], "selected_material_key": None}
            for citation in citations.values() if citation["witness_ref"] in original],
        "inherited_selection_keys": [], "literature_decisions": []}


@pytest.fixture
def scenario(tmp_path, request):
    custody = getattr(request, "param", None)
    if getattr(request.node, "callspec", None) and request.node.callspec.params.get("invalid") == "duplicate_inheritance":
        custody = "managed"
    source = tmp_path / "original"
    source.mkdir()
    for name, content in {"a.txt": "A reports 16.", "b.txt": "B reports 25.", "c.txt": "Unexamined."}.items():
        (source / name).write_text(content)
    runtime = _make(tmp_path / "runtime")
    try:
        with TestClient(create_app(runtime, base_url="http://testserver", control_key="control")) as client:
            anchor, binding, reference = _open(client, runtime, source, "reassessment")
            runtime.root_workspaces.configure_creation_runtime(executable="/bin/bash", credentials_home=tmp_path / "credentials")
            inputs = runtime.owners.human_collaboration.creation_material_snapshot(anchor)
            request, work, identity, citations = read_materials(runtime, binding, inputs, "original-understanding", reference["reference_ref"], ["a.txt", "b.txt"])
            understanding = empty_understanding()
            understanding["coverage"] = [{"material_key": reference["reference_ref"], "kind": "partial",
                "read_ranges": list(citations.values()), "unread_description": "c.txt remains unread."}]
            understanding["claims_and_conditions"] = [statement("a", "A reports 16.", citations["a.txt"]), statement("b", "B reports 25.", citations["b.txt"])]
            if custody is not None:
                observed = next(item for item in identity.consumed if item.path == "a.txt")
                understanding["selection"] = [{"source": {"kind": "original_file", "reference_ref": reference["reference_ref"],
                    "path": "a.txt", "observation_ref": observed.observation_ref}, "custody": custody, "reason": "Keep the exact historical A trial."}]
            memory = runtime.owners.research_memory.creation_bases
            basis = memory.accept_reference_prepared(request, InitializationUnderstandingResult(understanding, input_identity=identity, work=work))
            yield runtime, client, source, binding, inputs, reference, basis, citations
    finally:
        runtime.close()


def changed_result(scenario):
    runtime, _, source, binding, inputs, reference, predecessor, old_citations = scenario
    (source / "b.txt").write_text("B reports 36.")
    request, work, identity, citations = read_materials(runtime, binding, inputs, "changed-understanding", reference["reference_ref"], ["b.txt"])
    memory = runtime.owners.research_memory.creation_bases
    context = memory.reassessment_context(inputs)
    request = InitializationUnderstandingRequest(**{**request.__dict__, "reassessment": context})
    citations["a.txt"] = old_citations["a.txt"]
    delta = reassessment_delta(memory, predecessor, reference["reference_ref"], citations)
    return request, InitializationUnderstandingResult({}, input_identity=identity, work=work, reassessment=delta)


def test_only_changed_child_is_read_and_original_operation_is_preserved(scenario):
    runtime, _, _, _, inputs, reference, predecessor, _ = scenario
    memory = runtime.owners.research_memory.creation_bases
    request, result = changed_result(scenario)
    accepted = memory.accept_reference_prepared(request, result)
    assert [item["path"] for item in accepted["input_identity"]["consumed"]] == ["b.txt"]
    assert accepted["understanding"]["claims_and_conditions"][1] == predecessor["understanding"]["claims_and_conditions"][0]
    assert accepted["inherited_evidence"][0]["witness"]["operation_ref"] == "original-understanding"
    assert accepted["predecessor"] == memory.reference(predecessor)
    assert accepted["understanding"]["coverage"][0]["kind"] == "partial"
    assert accepted["understanding"]["coverage"][0]["unread_description"] == "c.txt has not been read. It remains future research."
    assert memory.require_current(accepted) == inputs
    assert memory.query(predecessor["basis_ref"], predecessor["basis_hash"])["understanding"]["claims_and_conditions"][1]["text"] == "B reports 25."
    view = memory.understanding_view(accepted)
    assert view["claims_and_conditions"][1]["sources"][0]["provenance"]["kind"] == "live_source"
    assert view["applicability"]["decisions"][1]["replacement_refs"] == ["b-new"]


@pytest.mark.parametrize("invalid", ["predecessor", "witness", "missing_decision", "changed_inheritance", "directory_read", "race",
    "needs_recheck_replacement", "out_of_scope_replacement", "duplicate_inheritance"])
def test_reassessment_rejects_forgery_and_racing_changes(scenario, invalid):
    runtime, _, source, _, _, _, predecessor, citations = scenario
    memory = runtime.owners.research_memory.creation_bases
    request, result = changed_result(scenario)
    delta = copy.deepcopy(result.reassessment)
    if invalid == "predecessor":
        delta["predecessor"]["basis_hash"] = "0" * 64
    elif invalid == "witness":
        delta["inherited_evidence"][0]["witness_ref"] = "fake-witness"
    elif invalid == "missing_decision":
        delta["decisions"].pop()
    elif invalid == "changed_inheritance":
        delta["inherited_evidence"][0]["witness_ref"] = citations["b.txt"]["witness_ref"]
    elif invalid == "directory_read":
        delta["additions"]["coverage"][0]["kind"] = "read"
    elif invalid == "duplicate_inheritance":
        selected = next(item for item in predecessor["sources"] if item["binding"] is not None)
        delta["inherited_selection_keys"] = [selected["material_key"]]
        delta["inherited_evidence"].append({"kind": "managed_history", "witness_ref": citations["a.txt"]["witness_ref"],
            "selected_material_key": selected["material_key"]})
    elif invalid.endswith("_replacement"):
        delta["decisions"][0].update(disposition=invalid.removesuffix("_replacement"),
            affected_scope="A needs a future check.", creation_limit="future_research", replacement_refs=["invented"])
    else:
        (source / "a.txt").write_text("Changed while accepting.")
    result = InitializationUnderstandingResult({}, input_identity=result.input_identity, work=result.work, reassessment=delta)
    with pytest.raises(OwnerConflict) as failure:
        memory.accept_reference_prepared(request, result)
    if invalid == "duplicate_inheritance":
        assert str(failure.value.__cause__) == "duplicate inherited evidence"
    assert memory.query(predecessor["basis_ref"], predecessor["basis_hash"])["understanding"]["claims_and_conditions"][0]["text"] == "A reports 16."


@pytest.mark.parametrize("scenario", ["managed", "linked_local"], indirect=True)
def test_historical_bytes_require_managed_custody_and_never_count_as_current_reads(scenario, monkeypatch):
    runtime, _, source, binding, inputs, reference, predecessor, citations = scenario
    memory = runtime.owners.research_memory.creation_bases
    selected = next(item for item in predecessor["sources"] if item["binding"] is not None)
    (source / "a.txt").write_text("A now reports 99.")
    request, work, identity, _ = read_materials(runtime, binding, inputs, "historical-understanding", reference["reference_ref"], [])
    context = memory.reassessment_context(inputs)
    request = InitializationUnderstandingRequest(**{**request.__dict__, "reassessment": context})
    delta = reassessment_delta(memory, predecessor, reference["reference_ref"], {"b.txt": citations["b.txt"]}, {"a": "retain", "b": "retain"})
    delta["additions"]["coverage"][0]["unread_description"] = "Current a.txt and c.txt were not read. A is historical evidence only."
    delta["inherited_evidence"].append({"kind": "managed_history", "witness_ref": citations["a.txt"]["witness_ref"], "selected_material_key": selected["material_key"]})
    delta["inherited_selection_keys"] = [selected["material_key"]]
    result = InitializationUnderstandingResult({}, input_identity=identity, work=work, reassessment=delta)
    monkeypatch.setattr(memory._owner, "submit_asset_intake", lambda *args, **kwargs: pytest.fail("Inherited versions must not be intaken again."))
    if selected["custody"] == "linked_local":
        with pytest.raises(OwnerConflict, match="creation_inheritance_invalid"):
            memory.accept_reference_prepared(request, result)
        with pytest.raises(OwnerConflict):
            memory.read_source(predecessor, selected["material_key"])
    else:
        accepted = memory.accept_reference_prepared(request, result)
        assert accepted["input_identity"]["consumed"] == []
        assert memory.read_source(accepted, selected["material_key"])["content"] == b"A reports 16."
        assert next(item for item in accepted["sources"] if item["binding"] is not None)["binding"] == selected["binding"]
        assert [item["witness_ref"] for item in accepted["understanding"]["coverage"][0]["read_ranges"]] == [citations["b.txt"]["witness_ref"]]
        assert memory.understanding_view(accepted)["claims_and_conditions"][0]["sources"][0]["provenance"]["kind"] == "managed_history"
        assert memory.require_current(accepted) == inputs
    assert memory.query(predecessor["basis_ref"], predecessor["basis_hash"])["basis_hash"] == predecessor["basis_hash"]
