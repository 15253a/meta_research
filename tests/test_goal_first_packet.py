"""Current Goal survives presentation at the actual provider boundary."""
import json
from copy import deepcopy
from dataclasses import replace

import pytest

from meta_research.owners.common import canonical_hash
from meta_research.reasoning_skill import CodexReasoningSkillAdapter
from test_plan_skill_adapter import _fake_codex
from test_reasoning_skill_adapter import (
    _FullConformanceAuthority, _SequenceRunner, _request, _stage_output,
)
from meta_research.bundle_skill import CodexBundleSkillAdapter
from meta_research.human_guidance import GuidanceOperationIdentity
from meta_research.owners.common import canonical_json
from meta_research.paths import prepare_data_root
from meta_research.runtime_conditions import read_runtime_conditions, save_runtime_conditions
from test_human_guidance_providers import (
    _adapter, _bundle_runtime, _reasoning_runtime, _confirm_direct_quest,
    _confirm_deepfetch_quest, _finish_idea_stage, _finish_plan_stage,
    _BundleTransport, _ReasoningTransport,
)
from test_human_guidance_continuations import _configure


@pytest.mark.parametrize("repetitions", [180, 5000], ids=["preview-limit", "aggregate-limit"])
def test_reasoning_first_provider_receives_complete_goal_without_expanding_history(
    tmp_path, repetitions,
):
    goal_text = "审慎比较研究证据，保留负结果。" * repetitions + "尾部条件：不得删除失败实验。"
    goal = {"kind": "QuestGoalRevision", "quest_ref": "quest:1",
            "goal_revision_ref": "goal-revision:1",
            "goal": {"goal": goal_text, "completion_criteria": "交付完整分析与限制。"}}
    context = deepcopy(_request().context_pack)
    context["research_context"]["quest_goal_revision"] = goal
    history = "非当前历史正文。" * 10000
    context["question_literature_input"]["notes"] = history
    runner = _SequenceRunner([_stage_output()])
    adapter = CodexReasoningSkillAdapter(
        tmp_path / "provider", executable=str(_fake_codex(tmp_path / "codex")),
        process_runner=runner,
    )
    adapter.bind_full_conformance_authority(_FullConformanceAuthority())
    adapter.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
    request = replace(_request(), context_pack=context,
                      context_pack_hash=canonical_hash(context),
                      runtime_binding=adapter.runtime_binding())

    adapter.generate_draft(request)

    prompt = runner.calls[0][1]
    view = json.loads(next(line.removeprefix("provider_context_view=")
                          for line in prompt.splitlines()
                          if line.startswith("provider_context_view=")))
    assert view["required_material"]["quest_goal_revision"] == goal
    assert prompt.count(goal_text) == 1
    assert history not in prompt
    assert context["research_context"]["quest_goal_revision"] == goal


@pytest.mark.parametrize("stage", ["bundle", "reasoning"])
def test_first_stage_provider_receives_owner_frozen_goal_and_enduring_conditions(tmp_path, stage):
    adapter_type = CodexBundleSkillAdapter if stage == "bundle" else CodexReasoningSkillAdapter
    provider, runner = _adapter(adapter_type, tmp_path)
    root = tmp_path / "runtime"
    runtime = (_bundle_runtime(root, bundle_skill_provider=provider) if stage == "bundle"
               else _reasoning_runtime(root, reasoning_skill=provider))
    runner.runtime = runtime
    _configure(runtime)
    try:
        quest = (_confirm_direct_quest(runtime) if stage == "bundle"
                 else _confirm_deepfetch_quest(runtime))["quest_ref"]
        condition_text = "Keep all authorized measurement controls. " * 200 + "TAIL: no unapproved device changes."
        before = read_runtime_conditions(root, quest)
        save_runtime_conditions(root, quest, text=condition_text,
                                expected_revision=before["revision"])
        _finish_idea_stage(runtime)
        if stage == "bundle":
            _finish_plan_stage(runtime)
        worker = runtime.bundle_stage if stage == "bundle" else runtime.reasoning_stage
        for _ in range(16):
            worker.process_once()
            if runner.calls:
                break
        assert len(runner.calls) == 1, (worker.transient_error, worker.query_current())
        # A deliberately non-scientific controlled response ends the probe;
        # assertions concern the real first request, not model success.
        prompt = runner.calls[0][1]
        binding = runner.inboxes[0]["binding"]
        cut = runtime.owners.human_collaboration.recover_operation_guidance(
            GuidanceOperationIdentity(**binding["identity"]))
        expected = {"goal": cut.direction_cut["goal"],
                    "conditions": cut.direction_cut["conditions"]}
        assert "current_goal_and_conditions=" + canonical_json(expected) in prompt
        assert condition_text in prompt
        assert canonical_json(cut.direction_cut["work"]) not in prompt
    finally:
        runtime.close()


class _ReplayPrimary:
    def generate_draft(self, request):
        result = super().generate_draft(request)
        self.call(request, "primary")
        return result


class _ReplayingBundle(_ReplayPrimary, _BundleTransport):
    pass


class _ReplayingReasoning(_ReplayPrimary, _ReasoningTransport):
    pass


@pytest.mark.parametrize("stage", ["bundle", "reasoning"])
def test_direction_is_frozen_for_replay_and_not_reinjected_on_same_root(tmp_path, stage):
    root = tmp_path / "runtime"
    prepare_data_root(root)
    provider, runner = _adapter(
        _ReplayingBundle if stage == "bundle" else _ReplayingReasoning, root)
    runtime = (_bundle_runtime(root, bundle_skill_provider=provider) if stage == "bundle"
               else _reasoning_runtime(root, reasoning_skill=provider))
    runner.runtime = runtime
    _configure(runtime)
    try:
        quest = (_confirm_direct_quest(runtime) if stage == "bundle"
                 else _confirm_deepfetch_quest(runtime))["quest_ref"]
        original = read_runtime_conditions(root, quest)
        def update_conditions(_):
            save_runtime_conditions(root, quest, text="Later condition: use device B only.",
                                    expected_revision=original["revision"])
        runner.after_first = update_conditions
        _finish_idea_stage(runtime)
        if stage == "bundle":
            _finish_plan_stage(runtime)
        worker = runtime.bundle_stage if stage == "bundle" else runtime.reasoning_stage
        for _ in range(16):
            worker.process_once()
            if len(runner.calls) == 2:
                break
        assert len(runner.calls) == 2, (worker.transient_error, worker.query_current())
        first, resumed = (call[1] for call in runner.calls)
        assert "current_goal_and_conditions=" in first
        assert "Later condition: use device B only." not in first
        assert "current_goal_and_conditions=" not in resumed
        # The same sealed primary was replayed after the edit with no new
        # provider call. A new continuation still gets current runtime limits.
        assert "Later condition: use device B only." in resumed
    finally:
        runtime.close()
