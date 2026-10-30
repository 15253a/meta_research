from dataclasses import replace
from pathlib import Path

import pytest

from meta_research.idea_skill import CodexIdeaSkillAdapter, IdeaSkillUnavailable
from meta_research.owners.common import canonical_hash
from meta_research.plan_skill import CodexPlanSkillAdapter, validate_plan_skill_result
from test_plan_skill_adapter import _request, _plan, _fake_codex, _SequenceRunner


def test_root_finishes_independent_review_before_single_provider_return(tmp_path):
    runner = _SequenceRunner([{"plan": _plan()}])
    adapter = CodexPlanSkillAdapter(tmp_path / "provider", executable=str(_fake_codex(tmp_path / "codex")), process_runner=runner)
    context = _request().context_pack.copy()
    context["cycle_position"] = {"cycle_ref": "cycle:1", "round": 1, "cycle_focus": "Review the exact negative results", "focus_source": {"draft_hash": "a" * 64}}
    request = _request(runtime_binding=adapter.runtime_binding(), context_pack=context, context_pack_hash=canonical_hash(context))
    result = adapter.execute(request)
    assert len(runner.calls) == 1
    prompt = runner.calls[0][1]
    assert "独立" in prompt and "子智能体" in prompt
    assert "下一次 resumed review turn" not in prompt
    assert prompt.startswith("cycle_position=")
    assert result.final_plan == result.reviewed_draft


def test_runtime_preparation_reads_each_shared_source_once_and_rechecks_next_call(tmp_path, monkeypatch):
    adapter = CodexPlanSkillAdapter(tmp_path / "provider", executable=str(_fake_codex(tmp_path / "codex")))
    original_open = Path.open
    reads = []
    def observe(path, *args, **kwargs):
        if path.name in {"idea_skill.py", "root_resident_mcp.py", "provider_supervisor.py", "SKILL.md"}:
            reads.append(str(path.resolve()))
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", observe)
    first = adapter.runtime_binding()
    assert reads and max(reads.count(path) for path in set(reads)) == 1
    count = len(reads)
    assert adapter.runtime_binding() == first
    assert len(reads) == count * 2


def test_equal_length_harness_change_is_rejected_before_provider_execution(tmp_path):
    executable = _fake_codex(tmp_path / "codex")
    runner = _SequenceRunner([])
    adapter = CodexPlanSkillAdapter(tmp_path / "provider", executable=str(executable), process_runner=runner)
    request = _request(runtime_binding=adapter.runtime_binding())
    executable.write_text(executable.read_text().replace("test 1", "test 2"))
    from meta_research.plan_skill import PlanSkillUnavailable
    with pytest.raises(PlanSkillUnavailable, match="plan_runtime_binding_drift"):
        adapter.generate_draft(request)
    assert runner.calls == []


def test_failed_preparation_does_not_cache_a_resource_failure(tmp_path, monkeypatch):
    adapter = CodexPlanSkillAdapter(tmp_path / "provider", executable=str(_fake_codex(tmp_path / "codex")))
    original_open = Path.open
    failed = False
    def unavailable_once(path, *args, **kwargs):
        nonlocal failed
        if path.name == "SKILL.md" and not failed:
            failed = True
            raise FileNotFoundError("temporarily unavailable exact package")
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", unavailable_once)
    from meta_research.plan_skill import PlanSkillUnavailable
    with pytest.raises(PlanSkillUnavailable, match="plan_skill_resource_unavailable"):
        adapter.runtime_binding()
    assert adapter.runtime_binding().packaged_skill_bundle_hash


def test_stage_reconciles_exact_request_after_reading_layout_changes(tmp_path, monkeypatch):
    import meta_research.plan_skill as module
    runner = _SequenceRunner([{"plan": _plan()}])
    adapter = CodexPlanSkillAdapter(tmp_path / "provider", executable=str(_fake_codex(tmp_path / "codex")), process_runner=runner)
    request = _request(runtime_binding=adapter.runtime_binding(), job_ref="exact-stage-input")
    first = adapter.generate_draft(request)
    original = module.stage_context_json
    def rearranged(*args, **kwargs):
        return "{\"layout\":\"new reading arrangement\",\"scientific_input\":" + original(*args, **kwargs) + "}"
    monkeypatch.setattr(module, "stage_context_json", rearranged)
    replay = adapter.generate_draft(request)
    assert replay == first
    assert len(runner.calls) == 1
