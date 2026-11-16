from __future__ import annotations

from dataclasses import replace
import subprocess

import pytest

from meta_research.owners.common import canonical_json
from resume_stage_capture import capture_primary, capture_autonomous, make_adapter, continuation_request
import test_idea_skill_contract as idea
import test_plan_skill_adapter as plan
import test_reasoning_skill_adapter as reasoning


@pytest.mark.parametrize("stage", ["idea", "plan", "reasoning"])
def test_primary_resume_delivers_only_current_event_at_provider_boundary(tmp_path, stage):
    samples = capture_primary(stage, tmp_path / stage)
    module = {"idea": idea, "plan": plan, "reasoning": reasoning}[stage]
    question = canonical_json(module._request().accepted_question_content)
    for name in ("initial", "replacement"):
        prompt = samples[name]["prompt"]
        assert question in prompt
        assert "provider_context_view=" in prompt
    assert "REPLACEMENT_INPUT" in samples["replacement"]["prompt"]
    for name in ("continue", "correction"):
        prompt = samples[name]["prompt"]
        assert question not in prompt
        assert "provider_context_view=" not in prompt
        assert "runtime_binding=" not in prompt
        assert "完整 IdeaSet=" not in prompt
        assert "context_pack_ref=" in prompt
        assert "resume" in samples[name]["argv"]
        assert samples[name]["schema"] == samples["initial"]["schema"]
        assert [value for value in samples[name]["argv"] if value.startswith("developer_instructions=")]
    assert "CURRENT_FEEDBACK 请补充实际证据。" in samples["correction"]["prompt"]
    assert "submission:rejected" in samples["correction"]["prompt"]


def test_reasoning_continuation_preserves_new_facts_without_repeating_material(tmp_path):
    samples = capture_autonomous(tmp_path)
    assert "checkpoint=" in samples["summary-first"]["prompt"]
    assert "NEW_FACTS" in samples["summary-first"]["prompt"]
    assert "NEW_SUMMARY " * 80 in samples["summary-first"]["prompt"]
    corrected = samples["summary-correction"]["prompt"]
    assert "SUMMARY_FEEDBACK" in corrected
    assert all(name + "=" not in corrected for name in ("checkpoint", "facts", "summary"))
    assert "CHANGED_FACTS" in samples["facts-changed"]["prompt"]
    assert "CHANGED_SUMMARY " * 90 in samples["facts-changed"]["prompt"]
    assert "SUMMARY_FEEDBACK" not in samples["facts-changed"]["prompt"]
    assert "checkpoint=" not in samples["facts-changed"]["prompt"]
    assert "CHANGED_CHECKPOINT" in samples["checkpoint-changed"]["prompt"]
    assert "summary=" not in samples["checkpoint-changed"]["prompt"]
    assert "facts=" not in samples["checkpoint-changed"]["prompt"]
    assert "creation_result=" in samples["creation-first"]["prompt"]
    assert "CREATION_FEEDBACK" in samples["creation-correction"]["prompt"]
    assert "creation_result=" not in samples["creation-correction"]["prompt"]
    assert "checkpoint=" not in samples["creation-correction"]["prompt"]
    assert "NEW_CREATION_RESULT" in samples["creation-changed"]["prompt"]


@pytest.mark.parametrize("previous", [
    "missing-operation", "different-root", "different-native-session",
    "altered-business-input", "altered-stdout",
])
def test_reasoning_missing_or_untrusted_prior_input_delivers_full_material(tmp_path, previous, monkeypatch):
    from meta_research.owners.common import canonical_hash
    checkpoint, result, review = reasoning._autonomous_resume_fixture(reasoning._request())
    adapter, request, runner = make_adapter("reasoning", tmp_path, [review, review])
    request = replace(request, native_session_ref="provider-session:1")
    prior = continuation_request(request, "prior")
    adapter.resume_after_autonomous_creation(prior, checkpoint, result)
    directory = adapter._workspace / "provider-operations" / canonical_hash({"job_ref": "prior"}) / "autonomous-resume"
    if previous == "altered-business-input":
        source = directory / "business-input.json"
        source.write_text("{}", encoding="utf-8")
    elif previous == "altered-stdout":
        (directory / "stdout.jsonl").write_text("{}", encoding="utf-8")
    current = continuation_request(request, "next", "missing" if previous == "missing-operation" else "prior", ("FIX_CURRENT",))
    if previous == "different-root":
        current = replace(current, root_session_ref="replacement-root:1")
    elif previous == "different-native-session":
        current = replace(current, native_session_ref="provider-session:replacement")
        run = type(runner).__call__

        def replacement_run(self, *args, **kwargs):
            completed = run(self, *args, **kwargs)
            completed.stdout = completed.stdout.replace("provider-session:1", "provider-session:replacement")
            return completed

        monkeypatch.setattr(type(runner), "__call__", replacement_run)
    adapter.resume_after_autonomous_creation(current, checkpoint, result)
    prompt = runner.calls[-1][1]
    assert canonical_json(checkpoint) in prompt
    assert canonical_json(result) in prompt


def test_unknown_reasoning_operation_reconciles_original_sealed_rendering(tmp_path, monkeypatch):
    from meta_research.owners.common import canonical_hash

    checkpoint, result, review = reasoning._autonomous_resume_fixture(reasoning._request())
    adapter, request, runner = make_adapter("reasoning", tmp_path, [review])
    request = continuation_request(replace(request, native_session_ref="provider-session:1"), "uncertain")
    native_inputs = []
    run = type(runner).__call__

    def unknown_outcome(self, argv, prompt, timeout, environment=None):
        native_inputs.append(prompt)
        raise subprocess.TimeoutExpired("codex", timeout)

    monkeypatch.setattr(type(runner), "__call__", unknown_outcome)
    with pytest.raises(reasoning.ReasoningSkillUnavailable, match="codex_cli_timeout"):
        adapter.resume_after_autonomous_creation(request, checkpoint, result)
    sealed = adapter._workspace / "provider-operations" / canonical_hash({"job_ref": "uncertain"}) / "autonomous-resume" / "prompt.txt"
    original = sealed.read_bytes()
    monkeypatch.setattr(type(runner), "__call__", run)
    rerendered = replace(request, current_continuation_feedback=("UNDELIVERED_RENDER_HINT",),
                         continuation_predecessor_job_ref="missing")
    with pytest.raises(reasoning.ReasoningSkillUnavailable, match="codex_operation_reconciliation_pending"):
        adapter.resume_after_autonomous_creation(rerendered, checkpoint, result)
    assert len(native_inputs) == 1 and runner.calls == []
    assert sealed.read_bytes() == original
    assert "UNDELIVERED_RENDER_HINT" not in original.decode("utf-8")
