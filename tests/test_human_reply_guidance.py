import json
from pathlib import Path

import pytest

from meta_research.bundle_skill import _bundle_skill_resources
from meta_research.idea_skill import _idea_skill_resources
from meta_research.owners.common import canonical_hash
from meta_research.root_capabilities import ROOT_AGENT_KINDS, root_capability_profile
from meta_research.runtime_binding_compatibility import reviewed_historical_root_profile_hashes


@pytest.mark.parametrize("loader", [_idea_skill_resources, _bundle_skill_resources])
def test_actual_packaged_stage_guidance_selects_reply_readers_and_optional_intake(loader):
    resources = loader()
    actual = resources["SKILL.md"]
    assert "human_request.open.reconcile" in actual
    assert "request_ref／response_ref" in actual
    assert "delivery.reply_reader／uploaded_readers" in actual
    assert "linked_locators" in actual
    assert "provided_material" in actual
    assert "判断有复用价值后再用既有 RM／RG 流程" in actual


@pytest.mark.parametrize("kind", ROOT_AGENT_KINDS)
def test_reply_prompt_preserves_exact_historical_read_profiles(kind):
    document = json.loads((Path(__file__).parent / "fixtures/human_reply_20261008/profiles.json").read_text())
    before, after = document["before_profile"], document["after_profile"]
    assert canonical_hash(before) == document["before_profile_hash"]
    assert canonical_hash(after) == document["after_profile_hash"]
    current = root_capability_profile(kind)
    assert {key: value for key, value in current.as_dict().items() if key != "research_system_prompt_hash"} == {
        key: value for key, value in after.items() if key != "research_system_prompt_hash"
    }
    assert document["after_profile_hash"] in reviewed_historical_root_profile_hashes(current.digest)
    assert [key for key in before if before[key] != after[key]] == ["research_system_prompt_hash"]
    assert reviewed_historical_root_profile_hashes(document["after_profile_hash"]) == frozenset(document["historical_read_hashes"])
