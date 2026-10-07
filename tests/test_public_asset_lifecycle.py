from __future__ import annotations

from pathlib import Path

import pytest

from meta_research.composition import build_production_runtime
from meta_research.owners.research_memory import AssetIntakeRequest
from meta_research.owners.common import OwnerConflict
from meta_research.paths import prepare_data_root


def intake(memory, content, key, **values):
    result = memory.submit_asset_intake(AssetIntakeRequest(
        source_kind="text", custody_mode="managed", display_name="observation.txt",
        content=content, **values), idempotency_key=key)
    assert result.status == "accepted", result.failure_code
    assert result.asset is not None
    return result.asset


def test_supplement_selects_current_without_rewriting_exact_history(tmp_path: Path):
    data_root = prepare_data_root(tmp_path / "lifecycle")
    runtime = build_production_runtime(data_root)
    try:
        memory = runtime.owners.research_memory
        first = intake(memory, b"first observation\n", "first")
        state = memory.query_asset_lifecycle(first.asset_ref)
        assert state["current_version_ref"] == first.version_ref
        assert state["revision"] == 1
        second = intake(memory, b"first observation\nsecond observation\n", "second",
            asset_ref=first.asset_ref, change={"kind": "supplement",
                "predecessor_version_ref": first.version_ref, "expected_revision": 1,
                "explanation": "Adds the second independent observation."})
        assert memory.query_current_asset(first.asset_ref).version_ref == second.version_ref
        assert memory.materialize_asset(first.version_ref).content == b"first observation\n"
        state = memory.query_asset_lifecycle(first.asset_ref)
        assert [(v["version_ref"], v["state"]) for v in state["versions"]] == [
            (first.version_ref, "superseded"), (second.version_ref, "current")]
        assert state["changes"][-1]["explanation"] == "Adds the second independent observation."
        assert memory.query_asset_version(first.version_ref).receipt == first.receipt
    finally:
        runtime.close()
    reopened = build_production_runtime(data_root)
    try:
        assert reopened.owners.research_memory.query_current_asset(first.asset_ref).version_ref == second.version_ref
        assert reopened.owners.research_memory.materialize_asset(first.version_ref).content == b"first observation\n"
    finally:
        reopened.close()


def retire(memory, graph, asset, key, **values):
    return memory.retire_asset_version(asset.version_ref,
        expected_revision=memory.query_asset_lifecycle(asset.asset_ref)["revision"],
        expected_reference_revision=graph.query_asset_reference_revision(),
        explanation="The draft contains a verified unit error, is obsolete and has no remaining research or explanatory value.",
        low_value=True, obsolete=True, incorrect=True, impact_understood=True,
        has_explanation_value=False, idempotency_key=key, **values)


def test_retirement_is_a_persisted_state_and_retains_shared_and_linked_bytes(tmp_path: Path):
    root = prepare_data_root(tmp_path / "retirement")
    runtime = build_production_runtime(root)
    try:
        memory, graph = runtime.owners.research_memory, runtime.owners.research_graph
        first = intake(memory, b"obsolete unit error\n", "obsolete")
        shared = intake(memory, b"obsolete unit error\n", "independent")
        request = dict(expected_revision=1, expected_reference_revision=graph.query_asset_reference_revision(),
            explanation="Verified obsolete unit error. No research depends on this disposable draft.",
            low_value=True, obsolete=True, incorrect=True, impact_understood=True, has_explanation_value=False,
            idempotency_key="retire")
        fact = memory.retire_asset_version(first.version_ref, **request)
        assert memory.retire_asset_version(first.version_ref, **request) == fact
        assert fact["kind"] == "retirement"
        assert memory.query_current_asset(first.asset_ref) is None
        assert memory.query_asset_lifecycle(first.asset_ref)["versions"][0]["state"] == "retired"
        assert memory.materialize_asset(first.version_ref).content == b"obsolete unit error\n"
        assert memory.materialize_asset(shared.version_ref).content == b"obsolete unit error\n"
        with pytest.raises(OwnerConflict, match="asset_retirement_idempotency_conflict"):
            memory.retire_asset_version(first.version_ref, **{**request, "explanation": "Different reason"})
        source = tmp_path / "external.txt"
        source.write_bytes(b"external original\n")
        linked = memory.submit_asset_intake(AssetIntakeRequest(source_kind="local_path", custody_mode="linked_local",
            source_locator=str(source), display_name="external.txt"), idempotency_key="linked").asset
        retire(memory, graph, linked, "retire-linked")
        assert source.read_bytes() == b"external original\n"
        assert memory.materialize_asset(linked.version_ref).content == b"external original\n"
    finally:
        runtime.close()
    reopened = build_production_runtime(root)
    try:
        assert reopened.owners.research_memory.query_current_asset(first.asset_ref) is None
        assert reopened.owners.research_memory.query_asset_lifecycle(first.asset_ref)["changes"][-1] == fact
    finally:
        reopened.close()


def test_fresh_hold_and_unknown_impact_block_retirement(tmp_path: Path):
    runtime = build_production_runtime(prepare_data_root(tmp_path / "holds"))
    try:
        memory, graph = runtime.owners.research_memory, runtime.owners.research_graph
        asset = intake(memory, b"draft\n", "draft")
        assessment = memory.assess_release_eligibility(asset.version_ref,
            expected_reference_revision=graph.query_asset_reference_revision(), idempotency_key="old-assessment")
        assert assessment.eligible is True
        hold = memory.place_asset_hold(asset.version_ref, reason="Retain until unit investigation finishes.", idempotency_key="fresh-hold")
        with pytest.raises(OwnerConflict) as error:
            retire(memory, graph, asset, "blocked")
        assert error.value.details["reasons"] == ["active_holds"]
        assert error.value.details["active_hold_refs"] == [hold.hold_ref]
        assert memory.query_current_asset(asset.asset_ref).version_ref == asset.version_ref
        memory.release_asset_hold(hold.hold_ref, idempotency_key="release")
        with pytest.raises(OwnerConflict) as error:
            memory.retire_asset_version(asset.version_ref, expected_revision=1,
                expected_reference_revision=graph.query_asset_reference_revision(), explanation="Impact remains unknown.",
                low_value=True, obsolete=True, incorrect=True, impact_understood=False,
                has_explanation_value=True, idempotency_key="unknown")
        assert error.value.details["reasons"] == ["impact_uncertain", "explanation_value_retained"]
        assert len(memory.query_asset_lifecycle(asset.asset_ref)["changes"]) == 1
    finally:
        runtime.close()
