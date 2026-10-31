"""Exact readers supply the complete adopted source, including Brief-only assets."""
from copy import deepcopy

import pytest

from meta_research.owners.common import OwnerConflict
from meta_research.owners.research_memory import AssetIntakeRequest
from meta_research.research_content import read_content
from meta_research.target_commit_evidence import TargetCommitEvidenceCatalog
from test_public_plan_stage import _runtime, _confirm_direct_quest, _DeterministicIdeaSkill, _DeterministicPlanSkill


def test_asset_reader_supplies_complete_adoption_and_authority_checks_it(tmp_path):
    runtime = _runtime(tmp_path / "adopted-assets", idea_skill=_DeterministicIdeaSkill(), plan_skill=_DeterministicPlanSkill(no_gap=False))
    try:
        quest = _confirm_direct_quest(runtime)
        graph, memory = runtime.owners.research_graph, runtime.owners.research_memory
        entries = []
        for index in range(33):
            asset = memory.submit_asset_intake(AssetIntakeRequest(
                source_kind="text", custody_mode="managed", display_name=f"script-{index}",
                content=f"script {index}\n".encode()), idempotency_key=f"adopt-{index}").asset
            graph.accept_asset_role(binding=asset.as_binding(), role="quest_source_material",
                quest_ref=quest["quest_ref"], idempotency_key=f"scope-{index}")
            page = read_content(graph, memory, quest_ref=quest["quest_ref"],
                source_ref=asset.version_ref, version_ref=asset.version_ref)
            adopted = page["selected_evidence_binding"]
            assert adopted["source_kind"] == "AssetVersion"
            assert adopted["source_binding"] == asset.as_binding().as_dict()
            assert adopted["reader"] == {"operation": "research_memory.content.read",
                "args": {"source_ref": asset.version_ref, "version_ref": asset.version_ref}}
            entries.append(adopted)
        authority = TargetCommitEvidenceCatalog(graph, memory)
        authority.verify_plan_evidence_catalog(quest_ref=quest["quest_ref"],
            evidence_catalog=entries, expected_reference_revision=33,
            require_complete=False, selected_evidence_refs=frozenset(e["evidence_ref"] for e in entries))
        forged = deepcopy(entries)
        forged[0]["reader"]["args"]["version_ref"] = entries[1]["source_ref"]
        with pytest.raises(OwnerConflict):
            authority.verify_plan_evidence_catalog(quest_ref=quest["quest_ref"],
                evidence_catalog=forged, expected_reference_revision=33, require_complete=False)
        forged = deepcopy(entries)
        forged[0]["source_binding"]["content_hash"] = "0" * 64
        with pytest.raises(OwnerConflict):
            authority.verify_plan_evidence_catalog(quest_ref=quest["quest_ref"],
                evidence_catalog=forged, expected_reference_revision=33, require_complete=False)
        with pytest.raises(OwnerConflict):
            authority.verify_plan_evidence_catalog(quest_ref="foreign-quest",
                evidence_catalog=entries, expected_reference_revision=33, require_complete=False)
    finally:
        runtime.close()
