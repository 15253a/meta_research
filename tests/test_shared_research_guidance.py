from __future__ import annotations

import importlib

import pytest

from meta_research.owners.common import canonical_hash
from meta_research.research_guidance import shared_research_guidance


@pytest.mark.parametrize("stage", ("idea", "plan", "bundle", "reasoning"))
def test_shared_guidance_is_in_prompt_and_fingerprinted_resources(stage, monkeypatch):
    module = importlib.import_module(f"meta_research.{stage}_skill")
    resources = getattr(module, f"_{stage}_skill_resources")
    instructions = getattr(module, f"_{stage}_skill_instructions")
    guidance = shared_research_guidance()
    original_resources = resources()
    assert original_resources["research-guidance.md"] == guidance
    assert instructions().count(guidance) == 1

    # A policy update must reach the provider input and invalidate the bundle
    # fingerprint together, including stages with explicit resource ordering.
    updated = guidance + "\nupdated shared research policy"
    if "shared_research_guidance" in vars(module):
        monkeypatch.setattr(module, "shared_research_guidance", lambda: updated)
    monkeypatch.setattr("meta_research.research_guidance.shared_research_guidance", lambda: updated)
    assert instructions().count(updated) == 1
    assert canonical_hash(resources()) != canonical_hash(original_resources)
