from pathlib import Path
import json

from meta_research import idea_skill, plan_skill, bundle_skill, reasoning_skill
from meta_research.research_guidance import shared_human_guidance
from meta_research.target_execution_contract import target_execution_skill_text
from meta_research.owners.common import canonical_hash
from meta_research.root_capabilities import root_capability_profile
from meta_research.runtime_binding_compatibility import reviewed_historical_root_profile_hashes
from test_human_guidance_providers import _adapter, _reasoning_runtime


def test_shared_guidance_is_loaded_once_by_every_working_root_and_changes_fingerprints(tmp_path, monkeypatch):
    modules = (idea_skill, plan_skill, bundle_skill, reasoning_skill)
    adapters = [_adapter(getattr(module, "Codex" + name + "SkillAdapter"), tmp_path)[0]
        for module, name in zip(modules, ("Idea", "Plan", "Bundle", "Reasoning"))]
    runtime = _reasoning_runtime(tmp_path / "runtime", idea_skill=adapters[0],
        plan_skill=adapters[1], bundle_skill=adapters[2], reasoning_skill=adapters[3])
    try:
        original = shared_human_guidance()
        texts = [getattr(module, "_" + name + "_skill_instructions")()
            for module, name in zip(modules, ("idea", "plan", "bundle", "reasoning"))]
        texts.append(target_execution_skill_text())
        for text in texts:
            assert text.count(original) == 1
            assert "human_guidance.read" in text
            assert "human_guidance.feedback" in text
            assert "needs_treatment=false" in text
            assert "goal_alignment_pending" in text
        before = [adapter.runtime_binding() for adapter in adapters]
        from meta_research import research_guidance
        for module in (idea_skill, plan_skill, reasoning_skill, research_guidance):
            monkeypatch.setattr(module, "shared_human_guidance", lambda: original + "\nAdditional exact guidance instruction.\n")
        after = [adapter.runtime_binding() for adapter in adapters]
        for previous, changed in zip(before, after):
            assert previous.packaged_skill_bundle_hash != changed.packaged_skill_bundle_hash
            assert previous.instruction_set_hash != changed.instruction_set_hash
        from meta_research import research_guidance
        monkeypatch.setattr(research_guidance, "shared_human_guidance", lambda: original + "\nTarget instruction change.\n")
        assert "Target instruction change." in target_execution_skill_text()
    finally:
        runtime.close()


def test_system_instruction_profile_keeps_exact_historical_read_identity():
    fixture = Path(__file__).parent / "fixtures" / "human_guidance_20261008" / "profiles.json"
    document = json.loads(fixture.read_text())
    before, after = document["before_profile"], document["after_profile"]
    assert canonical_hash(before) == document["before_profile_hash"]
    assert canonical_hash(after) == document["after_profile_hash"]
    assert [key for key in before if before[key] != after[key]] == ["research_system_prompt_hash"]
    for kind in ("idea", "plan", "bundle", "reasoning", "target"):
        current = root_capability_profile(kind)
        assert {key: value for key, value in current.as_dict().items() if key != "research_system_prompt_hash"} == {
            key: value for key, value in after.items() if key != "research_system_prompt_hash"
        }
        assert document["before_profile_hash"] in reviewed_historical_root_profile_hashes(document["after_profile_hash"])
        assert document["after_profile_hash"] in reviewed_historical_root_profile_hashes(current.digest)
