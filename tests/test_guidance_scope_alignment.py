from __future__ import annotations
from copy import deepcopy
from dataclasses import replace

from test_human_guidance_providers import _tool, _tool_error
from test_quest_goal_concurrent_roots import _ParallelBundle, _two_admitted_roots
from test_target_root_finalizer import _current_bundle_runtime
from test_public_quest_completion import _accepted_candidate
from test_quest_goal_completion_race import _runtime as _completion_runtime, _completion_command
from test_quest_goal_concurrent_roots import _decision
from meta_research.owners.research_memory import AssetIntakeRequest
from test_public_plan_stage import _runtime as _plan_runtime, _confirm_direct_quest, _DeterministicPlanSkill, _DeterministicIdeaSkill, _finish_idea_stage
from test_public_manual_question_lifecycle import _confirm_waived_manual_question
from test_public_advancement_runtime_control import _confirmed_control, _execute_control
from meta_research.human_guidance import GuidanceRuntimeScope, StageGuidanceOperation
from meta_research.owners.common import canonical_hash


def _confirm_guidance(runtime, quest_ref, scope, *, strength=5, key="local-guidance", text=None):
    human = runtime.owners.human_collaboration
    proposal = human.record_agent_proposal("quest:" + quest_ref, {
        "proposal_kind": "soft_constraint", "work_materials": None,
        "text": text or "本次实验严格只换学习率，总目标不变。",
        "assistant_understanding": "考虑将 Quest 重心转向设备校准。" if text else "只调整所选实验的学习率，保留 Quest 目标。",
        "applies_to": ["仅所选研究工作，保留其边界。"],
        "semantic_scope": scope,
        "preserve_conditions": [] if text else ["总目标不变"],
        "strength": strength,
    }, key + "-proposal")
    return human.convert_agent_proposal_to_soft_constraint(
        proposal["proposal_ref"], expected_scope_ref="quest:" + quest_ref,
        expected_proposal_hash=proposal["proposal_hash"], strength=strength,
        idempotency_key=key + "-confirm",
    )["soft_constraint"]


def test_confirmed_strict_target_guidance_applies_only_to_its_work_and_needs_no_goal_version(tmp_path):
    runtime = _current_bundle_runtime(tmp_path / "local-strict", bundle_skill=_ParallelBundle())
    runtime.configure_resident_mcp_endpoint("http://127.0.0.1:8999")
    try:
        quest_ref, admissions, _ = _two_admitted_roots(runtime)
        selected = runtime.owners.agent_runtime.harness_runs.query_request(admissions[0].run.run_ref)
        foreground = runtime.owners.advancement_engine.query_foreground(quest_ref)
        scope = {"kind": "target", "quest_ref": quest_ref,
            "question_ref": foreground["question_ref"], "cycle_ref": foreground["cycle_ref"],
            "target_ref": selected["target_ref"]}
        confirmed = _confirm_guidance(runtime, quest_ref, scope)
        graph = runtime.owners.research_graph
        original_goal = graph.query_current_quest_goal_revision(quest_ref)
        adapter = runtime.harnesses._adapters["codex"]
        original_invoke = adapter.invoke
        observed = {}

        def invoke(invocation):
            if invocation.root_kind == "target":
                inbox = _tool(runtime, invocation.mcp_token, "human_guidance.read")
                delivery, = inbox["deliveries"]
                exact = _tool(runtime, invocation.mcp_token, "human_guidance.read",
                    delivery_ref=delivery["delivery_ref"], effect_id="read-local")
                observed[invocation.run_ref] = {"delivery": delivery, "exact": exact}
                if invocation.run_ref == admissions[0].run.run_ref:
                    feedback = _tool(runtime, invocation.mcp_token, "human_guidance.feedback",
                        delivery_ref=delivery["delivery_ref"], effect_id="apply-local",
                        understanding="仅本实验修改学习率；整体目标及完成标准不变。",
                        changes="本实验其余设置保持。", continuing_work="继续当前实验。",
                        reasons="用户确认范围限于当前实验。", disposition="applied")
                    observed[invocation.run_ref]["feedback"] = feedback
                else:
                    _tool_error(runtime, invocation.mcp_token, "human_guidance.feedback",
                        "guidance_outside_confirmed_scope", delivery_ref=delivery["delivery_ref"],
                        effect_id="must-not-apply-local", understanding="阅读局部背景。",
                        changes="保持当前实验。", continuing_work="继续自己的实验。",
                        reasons="此指导属于另一实验。", disposition="applied")
            return original_invoke(invocation)

        adapter.invoke = invoke
        for admission in admissions:
            assert runtime.harnesses.run_or_resume_target_root(admission.run.request_ref,
                prompt="按本工作适用指导继续。", mcp_base_url="http://127.0.0.1:8999").status == "executed"
        applicable = observed[admissions[0].run.run_ref]
        background = observed[admissions[1].run.run_ref]
        assert applicable["exact"]["original_text"] == confirmed["guidance"]["text"]
        assert applicable["exact"]["semantic_scope"] == scope
        assert applicable["delivery"]["needs_treatment"] is True
        assert applicable["feedback"]["goal_update_pending"] is False
        assert background["delivery"]["applies_to_work"] is False
        assert background["delivery"]["needs_treatment"] is False
        delivered = runtime.owners.human_collaboration.query_guidance_deliveries(confirmed["constraint_ref"])
        background_record = next(item for item in delivered if item["run_ref"] == admissions[1].run.run_ref)
        assert background_record["background_only"] is True
        assert graph.query_quest_goal_view(quest_ref)["guidance_alignment"] == []
        assert graph.query_current_quest_goal_revision(quest_ref) == original_goal
    finally:
        runtime.close()


def test_strict_local_guidance_does_not_block_actual_quest_completion_acceptance(tmp_path):
    runtime = _completion_runtime(tmp_path / "local-completion")
    try:
        quest, _, decision, binding = _accepted_candidate(runtime)
        quest_ref = quest["quest_ref"]
        foreground = runtime.owners.advancement_engine.query_foreground(quest_ref)
        initial_goal = runtime.owners.research_graph.query_current_quest_goal_revision(quest_ref)
        _confirm_guidance(runtime, quest_ref, {"kind": "question", "quest_ref": quest_ref,
            "question_ref": foreground["question_ref"]})
        command, _ = _completion_command(runtime, binding, decision)
        accepted = runtime.owners.research_graph.accept_quest_completion(**command)
        assert accepted["quest_ref"] == quest_ref
        assert runtime.owners.research_graph.query_current_quest_goal_revision(quest_ref) == initial_goal
    finally:
        runtime.close()


def test_low_strength_quest_guidance_keeps_real_goal_alignment_then_evolves_criteria(tmp_path):
    runtime = _current_bundle_runtime(tmp_path / "global-direction", bundle_skill=_ParallelBundle())
    runtime.configure_resident_mcp_endpoint("http://127.0.0.1:8999")
    try:
        quest_ref, admissions, _ = _two_admitted_roots(runtime)
        _confirm_guidance(runtime, quest_ref, {"kind": "quest", "quest_ref": quest_ref},
            strength=2, text="供参考：可把 Quest 重心转向设备校准，同时重审整体完成标准。")
        graph = runtime.owners.research_graph
        assert graph.query_quest_goal_view(quest_ref)["guidance_alignment"][0]["status"] == "pending"
        adapter = runtime.harnesses._adapters["codex"]
        original_invoke = adapter.invoke
        observed = {}

        def invoke(invocation):
            if invocation.root_kind == "target":
                _, decision = _decision(runtime, invocation, goal="Audit devices with calibration controls.")
                _tool(runtime, invocation.mcp_token, "human_guidance.feedback",
                    delivery_ref=decision["cause"]["delivery_ref"], effect_id="global-impact",
                    understanding="低力度建议在本研究证据下确实改变整体重心。",
                    changes="同时重审整体目标与完成标准。", continuing_work="保留有价值的并行比较。",
                    reasons="设备校准影响所有比较的可解释性。", disposition="goal_alignment_pending",
                    goal_impact="requires_evolution")
                observed.update(_tool(runtime, invocation.mcp_token, "research_graph.quest_goal.evolve",
                    effect_id="global-evolution", decision=decision))
            return original_invoke(invocation)

        adapter.invoke = invoke
        runtime.harnesses.run_or_resume_target_root(admissions[0].run.request_ref,
            prompt="判断低力度全局建议并保持必要目标演化。", mcp_base_url="http://127.0.0.1:8999")
        current = graph.query_quest_goal_view(quest_ref)
        assert current["current"]["sequence"] == 1
        assert current["current"]["goal"]["goal"] == "Audit devices with calibration controls."
        assert current["current"]["goal"]["completion_criteria"] == "Report the reproducible device audit and calibration comparison."
        assert current["guidance_alignment"][0]["status"] == "aligned"
    finally:
        runtime.close()


def test_local_guidance_cannot_authorize_global_change_but_accepted_evidence_remains_independent(tmp_path):
    runtime = _current_bundle_runtime(tmp_path / "evidence-direction", bundle_skill=_ParallelBundle())
    runtime.configure_resident_mcp_endpoint("http://127.0.0.1:8999")
    try:
        quest_ref, admissions, _ = _two_admitted_roots(runtime)
        foreground = runtime.owners.advancement_engine.query_foreground(quest_ref)
        _confirm_guidance(runtime, quest_ref, {"kind": "question", "quest_ref": quest_ref,
            "question_ref": foreground["question_ref"]})
        asset = runtime.owners.research_memory.submit_asset_intake(AssetIntakeRequest(
            source_kind="text", custody_mode="managed", display_name="calibration.txt",
            media_type="text/plain", content=b"Accepted calibration evidence changes the useful research direction.\n",
            origin_quest_ref=quest_ref), idempotency_key="independent-evidence").asset
        assert asset is not None
        runtime.owners.research_graph.accept_asset_role(binding=asset.as_binding(), role="evidence",
            quest_ref=quest_ref, idempotency_key="independent-evidence-role")
        adapter = runtime.harnesses._adapters["codex"]
        original_invoke = adapter.invoke
        observed = {}

        def invoke(invocation):
            if invocation.root_kind == "target":
                _, decision = _decision(runtime, invocation, goal="Follow the accepted calibration evidence.")
                _tool_error(runtime, invocation.mcp_token, "research_graph.quest_goal.evolve",
                    "guidance_goal_scope_confirmation_required", effect_id="reject-local-authorization", decision=decision)
                decision_local_delivery = decision["cause"]["delivery_ref"]
                decision["cause"] = {"kind": "evidence", "evidence": [
                    {"source_ref": asset.asset_ref, "version_ref": asset.version_ref}]}
                local_condition = deepcopy(decision)
                original = "本次实验严格只换学习率，总目标不变。"
                clause = "总目标不变"
                local_condition["conditions"]["newly_identified"] = [{
                    "delivery_ref": decision_local_delivery,
                    "start": original.index(clause), "end": original.index(clause) + len(clause),
                    "text": clause, "meaning": "把局部原文扩大为整体持续条件。"}]
                _tool_error(runtime, invocation.mcp_token, "research_graph.quest_goal.evolve",
                    "guidance_goal_scope_confirmation_required", effect_id="reject-local-condition",
                    decision=local_condition)
                observed.update(_tool(runtime, invocation.mcp_token, "research_graph.quest_goal.evolve",
                    effect_id="autonomous-evidence", decision=decision))
            return original_invoke(invocation)

        adapter.invoke = invoke
        runtime.harnesses.run_or_resume_target_root(admissions[0].run.request_ref,
            prompt="尊重局部范围，并独立判断已接纳研究证据。", mcp_base_url="http://127.0.0.1:8999")
        assert observed["goal"]["cause"]["kind"] == "evidence"
        assert observed["goal"]["sequence"] == 1
    finally:
        runtime.close()


def test_inactive_question_guidance_waits_for_its_work_and_history_scope_survives_handoff_and_recovery(tmp_path):
    observed = []
    state = {}

    def consume(human, operation):
        cut = human.freeze_operation_guidance(operation)
        inbox = human.read_operation_guidance(scope=operation.scope, binding=cut.binding)
        observed.append({"kind": operation.scope.root_kind, "deliveries": inbox["deliveries"]})
        for delivery in inbox["deliveries"]:
            if not delivery["applies_to_work"] or not delivery["needs_treatment"]:
                continue
            human.read_operation_guidance(scope=operation.scope, binding=cut.binding,
                delivery_ref=delivery["delivery_ref"], effect_id="read-scoped")
            human.feedback_operation_guidance(scope=operation.scope, binding=cut.binding,
                delivery_ref=delivery["delivery_ref"], effect_id="process-scoped", understanding="只在确认问题内执行。",
                changes="保留问题边界。", continuing_work="继续适用研究。", reasons="确认范围保持不变。",
                disposition="applied", goal_impact="none")
        assert human.freeze_operation_guidance(operation) == cut
        state["last_cut"] = cut

    class Plan(_DeterministicPlanSkill):
        def generate_draft(self, request):
            scope = GuidanceRuntimeScope("plan", request.run_ref, request.attempt_ref,
                request.root_session_ref, request.fence_ref, canonical_hash(request.runtime_binding.as_dict()))
            consume(self.runtime.owners.human_collaboration, StageGuidanceOperation(scope, request.job_ref, "primary"))
            return super().generate_draft(request)

    class Idea(_DeterministicIdeaSkill):
        def operation(self, request, name):
            scope = GuidanceRuntimeScope("idea", request.run_ref, request.attempt_ref,
                request.root_session_ref, request.fence_ref, canonical_hash(request.runtime_binding.as_dict()))
            return StageGuidanceOperation(scope, request.job_ref, name)

        def generate_draft(self, request):
            consume(self.runtime.owners.human_collaboration, self.operation(request, "primary"))
            return replace(super().generate_draft(request),
                primary_session_ref="scoped-native-" + request.run_ref)

        def review_draft(self, request, draft):
            consume(self.runtime.owners.human_collaboration, self.operation(request, "review"))
            return super().review_draft(request, draft)

    provider = Idea()
    plan = Plan(no_gap=False)
    data_path = tmp_path / "question-scope"
    runtime = _plan_runtime(data_path, idea_skill=provider, plan_skill=plan)
    provider.runtime = plan.runtime = runtime
    try:
        completed = _confirm_direct_quest(runtime)
        quest_ref = completed["quest_ref"]
        human = runtime.owners.human_collaboration
        initial = runtime.owners.advancement_engine.query_foreground(quest_ref)
        seeded = _confirm_waived_manual_question(human, quest_ref=quest_ref,
            parent_question_ref=initial["question_ref"], key_prefix="future-guidance")
        for _ in range(8):
            human.reconcile_once()
        future = next(item for item in runtime.owners.research_graph.query_question_tree(quest_ref)
            if item.context_ref == seeded["context_ref"])
        waiting = _confirm_guidance(runtime, quest_ref, {"kind": "question", "quest_ref": quest_ref,
            "question_ref": future.question_ref}, key="future-scope")
        assert human.query_guidance_deliveries(waiting["constraint_ref"]) == []
        _finish_idea_stage(runtime)
        for _ in range(8):
            runtime.plan_stage.process_once()
            if any(item["kind"] == "plan" for item in observed):
                break
        assert any(item["kind"] == "plan" for item in observed)
        assert observed and all(not item["deliveries"][0]["applies_to_work"] for item in observed)
        current = runtime.owners.advancement_engine.query_foreground(quest_ref)
        command = _confirmed_control(human, scope_ref="quest:" + quest_ref, payload={
            "action": "forced_switch", "target": {"quest_ref": quest_ref,
                "cycle_ref": current["cycle_ref"], "question_ref": current["question_ref"],
                "epoch": current["epoch"], "target_question_ref": future.question_ref},
            "reason": "operator_requested"}, key="scope-switch")
        assert _execute_control(human, command, "scope-switch")["executed"] is True
        historical = _confirm_guidance(runtime, quest_ref, {"kind": "question", "quest_ref": quest_ref,
            "question_ref": initial["question_ref"]}, key="historical-scope")
        before = len(observed)
        for _ in range(6):
            runtime.idea_stage.process_once()
            if len(observed) > before:
                break
        assert len(observed) > before
        for item in observed[before:]:
            by_ref = {d["constraint_ref"]: d for d in item["deliveries"]}
            assert by_ref[waiting["constraint_ref"]]["applies_to_work"] is True
            assert by_ref[historical["constraint_ref"]]["background_only"] is True
        last_cut = state["last_cut"]
    finally:
        runtime.close()
    restarted = _plan_runtime(data_path, idea_skill=_DeterministicIdeaSkill(),
        plan_skill=_DeterministicPlanSkill(no_gap=False))
    try:
        assert restarted.owners.human_collaboration.recover_operation_guidance(last_cut.binding.identity) == last_cut
    finally:
        restarted.close()
