"""Packaged references participate in the same runtime identity as instructions."""
from pathlib import Path
from io import StringIO

import pytest

from meta_research.plan_skill import CodexPlanSkillAdapter, PlanSkillUnavailable
from meta_research.root_capabilities import root_capability_profile
from test_plan_skill_adapter import _fake_codex, _request, _SequenceRunner


def test_shared_reference_change_rejects_a_previously_bound_plan_before_provider(tmp_path, monkeypatch):
    runner = _SequenceRunner([])
    adapter = CodexPlanSkillAdapter(tmp_path / "provider", executable=str(_fake_codex(tmp_path / "codex")), process_runner=runner)
    request = _request(runtime_binding=adapter.runtime_binding())
    target_profile = root_capability_profile("target").digest
    original_open = Path.open

    def updated_reference(path, *args, **kwargs):
        if path.name == "research-guidance.md" and path.parent.name == "references":
            with original_open(path, *args, **kwargs) as stream:
                return StringIO(stream.read() + "\nAn updated shared research rule.\n")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", updated_reference)
    changed = adapter.runtime_binding()
    assert changed.packaged_skill_bundle_hash != request.runtime_binding.packaged_skill_bundle_hash
    assert changed.resource_bindings != request.runtime_binding.resource_bindings
    assert root_capability_profile("target").digest != target_profile
    with pytest.raises(PlanSkillUnavailable, match="plan_runtime_binding_drift"):
        adapter.generate_draft(request)
    assert runner.calls == []


def test_missing_shared_reference_fails_before_provider_and_can_be_retried(tmp_path, monkeypatch):
    runner = _SequenceRunner([])
    adapter = CodexPlanSkillAdapter(tmp_path / "provider", executable=str(_fake_codex(tmp_path / "codex")), process_runner=runner)
    original_open = Path.open

    def missing_reference(path, *args, **kwargs):
        if path.name == "materials-and-custody.md":
            raise FileNotFoundError("required packaged reference unavailable")
        return original_open(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", missing_reference)
        with pytest.raises(PlanSkillUnavailable, match="plan_skill_resource_unavailable"):
            adapter.runtime_binding()
    assert runner.calls == []
    assert adapter.runtime_binding().packaged_skill_bundle_hash
