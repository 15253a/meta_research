from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from meta_research.bundle_target_contract import (
    BundleTargetContractError,
    formal_target_candidate_from_dict,
    formal_target_candidate_to_dict,
    measurement_contract_hash,
)
from meta_research.owners.common import OwnerConflict, canonical_json
from meta_research.owners.research_graph import _validate_target_result_schema
from meta_research.target_implementation_bundle import (
    build_target_implementation_bundle_from_directory,
    parse_target_implementation_bundle,
)
from meta_research.target_run_finalizer import _decode_result_document_bytes
from test_bundle_target_contract import _candidate, _contract


def _research_candidate(method: str):
    _, completion_contract = _contract()
    candidate = _candidate(completion_contract, "research-a", "cell-a")
    measurement = candidate["measurement_contract"]
    measurement["baseline_forward_contract"] = {
        "method_key": method.replace(" ", "_"),
        "method_version": "1",
        "method_contract": {
            "method": method,
            "input_meaning": "frozen source observations and assumptions",
            "output_meaning": "bounded, inspectable support for the stated claim",
        },
    }
    measurement["variant_recipe"] = {
        "procedure": "apply the stated method to each included source",
        "evidence_record": "record supporting and contrary observations",
    }
    protocol = measurement["protocol_version"]
    protocol.update({
        "evaluation_data": {"materials": "the frozen source set"},
        "split": {"applicable": False, "reason": "no training or heldout samples"},
        "preprocessing": {"applicable": False, "reason": "use source records verbatim"},
        "required_metrics": [{
            "metric_key": "support",
            "definition": {
                "meaning": "whether the included evidence supports the bounded claim",
                "value_schema": {"enum": ["supported", "contradicted", "unresolved"]},
            },
        }],
        "optional_metrics": [],
        "internal_part_keys": [],
        "aggregation": None,
        "preregistered_stop_rules": [],
    })
    measurement["checkpoint_policy"] = "forbidden"
    measurement["result_schema"] = {
        "type": "object",
        "properties": {
            "schema_ref": {"const": measurement["result_schema_ref"]},
            "metrics": {
                "type": "object",
                "properties": {"support": {"enum": ["supported", "contradicted", "unresolved"]}},
                "required": ["support"],
                "additionalProperties": False,
            },
            "result_disposition": {"type": "string"},
            "observations": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["schema_ref", "metrics", "result_disposition", "observations"],
        "additionalProperties": False,
    }
    return candidate, completion_contract


@pytest.mark.parametrize("method", [
    "archival comparison", "qualitative interview analysis", "theoretical derivation",
    "observational study", "dataset construction and validation",
])
def test_non_code_method_preserves_target_hierarchy_and_qualitative_result(method: str):
    candidate, completion_contract = _research_candidate(method)
    parsed = formal_target_candidate_from_dict(candidate, completion_contract=completion_contract)
    assert formal_target_candidate_to_dict(parsed, completion_contract=completion_contract) == candidate
    measurement = parsed.measurement_contract
    assert measurement.checkpoint_policy == "forbidden"
    assert measurement.protocol_version.required_metric_keys == ("support",)

    document = {
        "schema_ref": measurement.result_schema_ref,
        "metrics": {"support": "unresolved"},
        "result_disposition": "uncertain",
        "observations": ["The two included sources disagree about the key event."],
    }
    result = _decode_result_document_bytes(canonical_json(document).encode())
    _validate_target_result_schema(schema=measurement.result_schema.as_dict(), result_content=result.as_dict())
    assert result.metrics["support"] == "unresolved"
    assert result.as_dict()["observations"] == document["observations"]
    invalid = deepcopy(document)
    invalid["metrics"]["support"] = "pipeline_completed"
    with pytest.raises(OwnerConflict):
        _validate_target_result_schema(schema=measurement.result_schema.as_dict(), result_content=invalid)


@pytest.mark.parametrize("field", ["image", "command", "provider", "execution"])
def test_result_schema_domain_field_names_do_not_become_runtime_routing(field: str):
    candidate, completion_contract = _research_candidate("archival comparison")
    original = formal_target_candidate_from_dict(candidate, completion_contract=completion_contract)
    schema = candidate["measurement_contract"]["result_schema"]
    schema["properties"][field] = {"type": "string", "description": "source material metadata"}
    parsed = formal_target_candidate_from_dict(candidate, completion_contract=completion_contract)
    assert parsed.measurement_contract.result_schema.as_dict() == schema
    assert measurement_contract_hash(parsed.measurement_contract) != measurement_contract_hash(original.measurement_contract)

    # An actual routing key attached to the contract root remains rejected.
    candidate["measurement_contract"]["result_schema"][field] = "runtime selection"
    with pytest.raises(BundleTargetContractError, match="runtime_routing"):
        formal_target_candidate_from_dict(candidate, completion_contract=completion_contract)


def test_non_code_method_material_is_an_accepted_implementation_tree(tmp_path: Path):
    implementation = tmp_path / "implementation"
    implementation.mkdir()
    procedure = b"Compare each source against the same question and record contradictions.\n"
    (implementation / "procedure.md").write_bytes(procedure)
    (implementation / "coding-frame.json").write_text('{"categories":["supports","contradicts"]}', encoding="utf-8")
    bundle = build_target_implementation_bundle_from_directory(implementation)
    accepted = parse_target_implementation_bundle(bundle.bundle_bytes, expected_tree_sha256=bundle.tree_sha256)
    assert accepted.entry("procedure.md").content == procedure
    assert {entry.relative_path for entry in accepted.entries} == {"procedure.md", "coding-frame.json"}


def test_native_target_owner_accepts_and_replays_qualitative_evidence(tmp_path: Path, monkeypatch):
    from dataclasses import replace

    import test_public_bundle_stage as bundle_fixtures
    from meta_research.target_run_finalizer import TargetRunFinalizer
    from test_target_root_finalizer import _EvidenceReader, _root_finalizer_fixture

    original_candidate = bundle_fixtures._formal_candidate

    def qualitative_candidate(*args, **kwargs):
        candidate = original_candidate(*args, **kwargs)
        measurement = candidate["measurement_contract"]
        qualitative, _ = _research_candidate("archival comparison")
        research = qualitative["measurement_contract"]
        for key in ("baseline_forward_contract", "variant_recipe", "protocol_version", "checkpoint_policy", "result_schema"):
            measurement[key] = deepcopy(research[key])
        measurement["result_schema"]["properties"]["schema_ref"] = {"const": measurement["result_schema_ref"]}
        return candidate

    monkeypatch.setattr(bundle_fixtures, "_formal_candidate", qualitative_candidate)
    runtime, lifecycle, memory, authority, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        (workspace / "implementation" / "train.py").unlink()
        (workspace / "implementation" / "method.md").write_text("Compare the frozen archives and record contradictions.", encoding="utf-8")
        (workspace / "logs" / "train.log").unlink()
        document = {
            "schema_ref": authority.measurement_contract.result_schema_ref,
            "metrics": {"support": "unresolved"},
            "result_disposition": "uncertain",
            "observations": ["The two archives record incompatible dates for the same event."],
        }
        (workspace / "outputs" / "metrics.json").write_text(canonical_json(document), encoding="utf-8")
        evidence = replace(evidence, handoff=replace(
            evidence.handoff,
            artifacts=tuple(artifact for artifact in evidence.handoff.artifacts if artifact.role != "log"),
        ))
        finalizer = TargetRunFinalizer(
            lifecycle=lifecycle,
            memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_EvidenceReader(evidence),
            measurement_authority=runtime.owners.research_graph,
        )
        seeded = finalizer.finalize(handle=handle, evidence=evidence)
        completion = lifecycle.query_completion(handle.target_ref)
        manifest = memory.query(seeded.manifest_ref)
        assert completion is not None and manifest is not None
        graph = runtime.owners.research_graph
        accepted = graph.accept_target_commit_from_root_completion(
            completion=completion, manifest=manifest, result_document=manifest.result_document,
            idempotency_key="accept-qualitative-root",
        )
        replay = graph.accept_target_commit_from_root_completion(
            completion=completion, manifest=manifest, result_document=manifest.result_document,
            idempotency_key="accept-qualitative-root",
        )
        assert replay == accepted
        closure = graph.query_target_frontier_commit_transition(handle.target_ref).canonical_terminal
        assert closure.metric_values == ("unresolved",)
        assert closure.checkpoint_artifact_refs == ()
        assert closure.variant_run_ref and closure.evaluation_attempt_ref
        assert manifest.result_document.as_dict()["observations"] == document["observations"]
    finally:
        runtime.close()
