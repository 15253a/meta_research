from pathlib import Path
import json

from meta_research import idea_skill, plan_skill, bundle_skill, reasoning_skill
from meta_research.research_guidance import shared_reference_resources
from meta_research.target_execution_contract import target_execution_skill_text
from meta_research.owners.common import canonical_hash
from meta_research.root_capabilities import root_capability_profile
from test_human_guidance_providers import _adapter, _reasoning_runtime


def test_shared_guidance_is_disclosed_by_every_working_root_and_changes_fingerprints(tmp_path, monkeypatch):
    from io import StringIO
    modules = (idea_skill, plan_skill, bundle_skill, reasoning_skill)
    adapters = [_adapter(getattr(module, "Codex" + name + "SkillAdapter"), tmp_path)[0]
        for module, name in zip(modules, ("Idea", "Plan", "Bundle", "Reasoning"))]
    runtime = _reasoning_runtime(tmp_path / "runtime", idea_skill=adapters[0],
        plan_skill=adapters[1], bundle_skill=adapters[2], reasoning_skill=adapters[3])
    try:
        original = shared_reference_resources()["references/human-guidance.md"]
        texts = [getattr(module, "_" + name + "_skill_instructions")()
            for module, name in zip(modules, ("idea", "plan", "bundle", "reasoning"))]
        texts.append(target_execution_skill_text())
        assert all(original not in text for text in texts)
        assert "human_guidance.read" in original
        assert "human_guidance.feedback" in original
        before = [adapter.runtime_binding() for adapter in adapters]
        for binding in before:
            for name in shared_reference_resources():
                assert any(item.startswith(f"package:meta_research.skills/{name}@sha256:") for item in binding.resource_bindings)
        target_before = root_capability_profile("target").digest
        original_open = Path.open

        def updated_reference(path, *args, **kwargs):
            if path.name == "human-guidance.md" and path.parent.name == "references":
                with original_open(path, *args, **kwargs) as stream:
                    return StringIO(stream.read() + "\nAdditional exact guidance instruction.\n")
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", updated_reference)
        after = [adapter.runtime_binding() for adapter in adapters]
        for previous, changed in zip(before, after):
            assert previous.packaged_skill_bundle_hash != changed.packaged_skill_bundle_hash
            assert previous.resource_bindings != changed.resource_bindings
        assert root_capability_profile("target").digest != target_before
    finally:
        runtime.close()
