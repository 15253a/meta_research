import json
from pathlib import Path

import pytest

from meta_research.bundle_skill import _bundle_skill_resources
from meta_research.idea_skill import _idea_skill_resources
from meta_research.owners.common import canonical_hash
from meta_research.root_capabilities import ROOT_AGENT_KINDS, root_capability_profile
from meta_research.runtime_binding_compatibility import reviewed_historical_root_profile_hashes


@pytest.mark.parametrize("loader", [_idea_skill_resources, _bundle_skill_resources])
def test_actual_packaged_stage_guidance_selects_reply_readers_and_optional_intake(tmp_path, loader):
    from test_public_guidance_delivery import _developer
    from test_root_capability_floor import _invoke_root_without_tool_activity

    resources = loader()
    root_kind = "idea" if loader is _idea_skill_resources else "bundle"
    _, argv = _invoke_root_without_tool_activity(tmp_path, root_kind=root_kind)
    developer = _developer(argv)
    source = Path(developer.split("Role skill source: ", 1)[1].splitlines()[0])
    skill = source.read_text(encoding="utf-8")
    assert skill == resources["SKILL.md"] and skill.strip() in developer
    assert "(../references/materials-and-custody.md)" in skill
    actual = (source.parent / "../references/materials-and-custody.md").read_text(encoding="utf-8")
    assert actual == resources["references/materials-and-custody.md"]
    assert actual not in developer
    assert "human_request.open.reconcile" in actual
    assert "无 Quest 时须传精确 request_ref 与 response_ref" in actual
    assert "delivery.reply_reader 与 uploaded_readers" in actual
    assert "linked_locators" in actual
    assert "provided_material" in actual
    assert "判断有复用价值时，再沿既有 RM／RG 接纳与登记流程处理" in actual
    assert "提交回应不自动登记 Literature、Dataset、Environment 或 ResearchAsset" in actual


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
    # The frozen reviewed transition remains readable; new guidance identities
    # do not automatically inherit historical compatibility.
    assert document["after_profile_hash"] not in reviewed_historical_root_profile_hashes(current.digest)
    assert [key for key in before if before[key] != after[key]] == ["research_system_prompt_hash"]
    assert reviewed_historical_root_profile_hashes(document["after_profile_hash"]) == frozenset(document["historical_read_hashes"])
