"""Capture reviewed execution identities without opening a production database."""
import json
from pathlib import Path
import sys
import tempfile

from meta_research.idea_skill import CodexIdeaSkillAdapter
from meta_research.plan_skill import CodexPlanSkillAdapter
from meta_research.bundle_skill import CodexBundleSkillAdapter
from meta_research.reasoning_skill import CodexReasoningSkillAdapter
from meta_research.writing_skill import CodexWritingSkillAdapter


with tempfile.TemporaryDirectory(prefix="system-mcp-binding-capture-") as directory:
    root = Path(directory)
    revisions = []
    for adapter_type in (CodexIdeaSkillAdapter, CodexPlanSkillAdapter,
                         CodexBundleSkillAdapter, CodexReasoningSkillAdapter,
                         CodexWritingSkillAdapter):
        adapter = adapter_type(root / adapter_type.__name__)
        profiles = ("report", "paper", "presentation") if adapter_type is CodexWritingSkillAdapter else (None,)
        for profile in profiles:
            binding = adapter.runtime_binding(profile) if profile else adapter.runtime_binding()
            revisions.append({
                "binding_type": type(binding).__name__,
                "profile": profile,
                "instruction_set_hash": binding.instruction_set_hash,
                "sources": [entry for entry in binding.resource_bindings if entry.startswith("adapter-source:")],
            })
Path(sys.argv[1]).write_text(json.dumps(revisions, indent=2) + "\n", encoding="utf-8")
