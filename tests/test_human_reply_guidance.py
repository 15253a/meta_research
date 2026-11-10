import json
import re
from pathlib import Path

import pytest

from meta_research.bundle_skill import _bundle_skill_resources
from meta_research.idea_skill import _idea_skill_resources
from meta_research.owners.common import canonical_hash
from meta_research.root_capabilities import ROOT_AGENT_KINDS, root_capability_profile


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
    linked_resources = {target.partition("#")[0] for target in re.findall(r"\]\(([^)]+)\)", skill)}
    assert "../references/materials-and-custody.md" in linked_resources
    actual = (source.parent / "../references/materials-and-custody.md").read_text(encoding="utf-8")
    assert actual == resources["references/materials-and-custody.md"]
    assert actual not in developer
    assert "human_request.open.reconcile" in actual
    assert "无 Quest 时须传精确 request_ref 与 response_ref" in actual
    assert "delivery.reply_file 与 uploaded_files" in actual
    assert "linked_locators" in actual
    assert "provided_material" in actual
    assert "判断有复用价值时，再沿既有 RM／RG 接纳与登记流程处理" in actual
    assert "提交回应不自动登记 Literature、Dataset、Environment 或 ResearchAsset" in actual
