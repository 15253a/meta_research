from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from meta_research.bundle_skill import CodexBundleSkillAdapter
from meta_research.bundle_protocol import GoalWorkDisposition
from meta_research.idea_skill import CodexIdeaSkillAdapter
from meta_research.plan_skill import CodexPlanSkillAdapter
from meta_research.reasoning_skill import CodexReasoningSkillAdapter
from meta_research.owners.common import canonical_hash
from meta_research.semantic_owner_gateway import ROOT_AGENT_SEMANTIC_OPERATION_IDS
from test_bundle_skill_adapter import _fake_codex
from test_runtime_conditions_provider_prompts import _SignedRunner
from test_public_plan_stage import (
    _runtime as _plan_runtime, _confirm_direct_quest, _finish_idea_stage,
    _DeterministicIdeaSkill, _DeterministicPlanSkill,
)
from test_public_bundle_stage import (
    _bundle_runtime, _DeterministicBundleSkill, _advance_to_public_bundle_execution,
    _finish_plan_stage,
)
from test_public_reasoning_stage import (
    _reasoning_runtime, _confirm_deepfetch_quest, _tick_reasoning,
    _DeterministicReasoningSkill,
)
from test_target_root_finalizer import _current_bundle_runtime, _admit_independent_target_root


_SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"], "additionalProperties": False}


def _tool(runtime, token, operation, **arguments):
    status, response, _ = runtime.harnesses.dispatch_mcp_http(token, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": operation, "arguments": arguments},
    }, mcp_session_id=None)
    assert status == 200, response
    result = response["result"]
    assert not result.get("isError"), result
    return result["structuredContent"]


def _tool_error(runtime, token, operation, expected_code, **arguments):
    status, response, _ = runtime.harnesses.dispatch_mcp_http(token, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": operation, "arguments": arguments},
    }, mcp_session_id=None)
    assert status == 200, response
    result = response["result"]
    assert result.get("isError") is True, result
    assert result["structuredContent"]["code"] == expected_code


class _GuidanceRunner(_SignedRunner):
    def __init__(self):
        super().__init__()
        self.inboxes = []
        self.receipts = []
        self.runtime = None
        self.after_first = None

    def run_command(self, argv, timeout):
        return subprocess.CompletedProcess(argv, 0,
            "hooks stable true\nmulti_agent stable true\nplugins stable true\n"
            "remote_plugin stable true\nshell_tool stable true\nskill_search stable true\nunified_exec stable true", "")

    def __call__(self, argv, prompt, timeout, environment=None):
        token = environment["META_RESEARCH_MCP_TOKEN"]
        status, catalog = self.runtime.harnesses.dispatch_mcp(token, {
            "jsonrpc": "2.0", "id": 0, "method": "tools/list"})
        assert status == 200
        assert {item["name"] for item in catalog["result"]["tools"]} >= {
            "human_guidance.read", "human_guidance.feedback",
            "human_guidance.read.reconcile", "human_guidance.feedback.reconcile"}
        inbox = _tool(self.runtime, token, "human_guidance.read")
        self.inboxes.append(inbox)
        for delivery in inbox["deliveries"]:
            exact = _tool(self.runtime, token, "human_guidance.read",
                delivery_ref=delivery["delivery_ref"], effect_id="read-" + delivery["delivery_ref"])
            assert exact["full_read"] is True
            assert json.loads(exact["text"])["text"] == "Keep the device audit."
            if delivery["needs_treatment"]:
                receipt = _tool(self.runtime, token, "human_guidance.feedback",
                    delivery_ref=delivery["delivery_ref"], effect_id="feedback-" + delivery["delivery_ref"],
                    understanding="Keep an independent device audit.", changes="Separate each device.",
                    continuing_work="Continue the accepted research question.",
                    reasons="Device drift can obscure morphology.",
                    disposition="goal_alignment_pending" if exact["strength"] == 5 else "applied")
                self.receipts.append(receipt)
        if len(self.inboxes) == 1 and self.after_first is not None:
            self.after_first(inbox)
        return super().__call__(argv, prompt, timeout, environment)


def _shared_call(adapter, request, name):
    value, _, _ = adapter._invoke_root_operation(operation_name=name,
        prompt="Read and handle this operation's formal guidance.", schema=_SCHEMA,
        native_session_ref=None if name == "primary" else "runtime-native",
        job_ref=request.job_ref, run_ref=request.run_ref, attempt_ref=request.attempt_ref,
        root_session_ref=request.root_session_ref, fence_ref=request.fence_ref,
        runtime_binding=request.runtime_binding.as_dict())
    assert value == {"ok": True}


class _IdeaTransport(CodexIdeaSkillAdapter):
    def generate_draft(self, request):
        _shared_call(self, request, "primary")
        return _DeterministicIdeaSkill().generate_draft(request)

    def review_draft(self, request, draft):
        _shared_call(self, request, "review")
        return _DeterministicIdeaSkill().review_draft(request, draft)


class _PlanTransport(CodexPlanSkillAdapter):
    def generate_draft(self, request):
        _shared_call(self, request, "primary")
        return _DeterministicPlanSkill(no_gap=False).generate_draft(request)

    def review_draft(self, request, draft):
        _shared_call(self, request, "review")
        return _DeterministicPlanSkill(no_gap=False).review_draft(request, draft)


class _BundleTransport(CodexBundleSkillAdapter):
    def call(self, request, name):
        value, _, _ = self._invoke_with_resident_mcp(run_ref=request.run_ref,
            attempt_ref=request.attempt_ref, root_session_ref=request.root_session_ref,
            fence_ref=request.fence_ref, runtime_binding=request.runtime_binding,
            operation_name=name, prompt="Read and handle formal guidance.", schema=_SCHEMA,
            native_session_ref=None if name == "primary" else "runtime-native", job_ref=request.job_ref)
        assert value == {"ok": True}

    def generate_draft(self, request):
        self.call(request, "primary")
        return _DeterministicBundleSkill().generate_draft(request)

    def review_draft(self, request, draft):
        return _DeterministicBundleSkill().review_draft(request, draft)

    def schedule_target(self, request):
        self.call(request, f"dispatch-{request.generation}")
        return _DeterministicBundleSkill().schedule_target(request)

    def propose_target_batch(self, request):
        self.call(request, f"target-batch-{request.base_generation + 1}")
        return _DeterministicBundleSkill().propose_target_batch(request)


class _ReasoningTransport(CodexReasoningSkillAdapter):
    def call(self, request, name):
        value, _, _ = self._invoke_with_resident_mcp(request=request,
            operation_name=name, prompt="Read and handle formal guidance.", schema=_SCHEMA,
            native_session_ref=None if name == "primary" else "runtime-native")
        assert value == {"ok": True}

    def generate_draft(self, request):
        self.call(request, "primary")
        return _DeterministicReasoningSkill().generate_draft(request)

    def review_draft(self, request, draft):
        self.call(request, "review")
        return _DeterministicReasoningSkill().review_draft(request, draft)


def _adapter(adapter_type, tmp_path):
    runner = _GuidanceRunner()
    adapter = adapter_type(tmp_path / adapter_type.__name__,
        executable=str(_fake_codex(tmp_path / (adapter_type.__name__ + "-codex"))), process_runner=runner)
    return adapter, runner


def _submit(runtime, quest, key="guidance", strength=3):
    return runtime.owners.human_collaboration.submit_human_guidance(
        quest_ref=quest, original_text="Keep the device audit.", strength=strength, idempotency_key=key)


def _seals(adapter):
    return [json.loads(path.read_text())["payload"] for path in
        (adapter._workspace / "provider-operations").glob("*/*/invocation.json")]


def test_actual_idea_and_plan_transport_freeze_independent_stage_responsibility(tmp_path):
    idea, idea_runner = _adapter(_IdeaTransport, tmp_path)
    plan, plan_runner = _adapter(_PlanTransport, tmp_path)
    runtime = _plan_runtime(tmp_path / "runtime", idea_skill=idea, plan_skill=plan)
    idea_runner.runtime = plan_runner.runtime = runtime
    runtime.configure_resident_mcp_endpoint("http://127.0.0.1:8999")
    runtime.bundle_stage.configure_resident_mcp_endpoint("http://127.0.0.1:8999")
    runtime.reasoning_stage.configure_resident_mcp_endpoint("http://127.0.0.1:8999")
    try:
        quest = _confirm_direct_quest(runtime)["quest_ref"]
        _submit(runtime, quest, strength=5)
        _finish_idea_stage(runtime)
        for _ in range(8):
            current = runtime.plan_stage.query_current()
            if current["stage_commit"] is not None:
                break
            runtime.plan_stage.process_once()
        assert [inbox["deliveries"][0]["needs_treatment"] for inbox in idea_runner.inboxes] == [True, False]
        assert [inbox["deliveries"][0]["needs_treatment"] for inbox in plan_runner.inboxes] == [True, False]
        assert len(idea_runner.receipts) == len(plan_runner.receipts) == 1
        assert idea_runner.receipts[0]["goal_update_pending"] is True
        assert plan_runner.receipts[0]["goal_update_pending"] is True
        assert {seal["guidance_binding"]["identity"]["root_kind"] for seal in _seals(idea)} == {"idea"}
        assert {seal["guidance_binding"]["identity"]["root_kind"] for seal in _seals(plan)} == {"plan"}
        assert idea_runner.inboxes[0]["binding"]["identity"]["run_ref"] != plan_runner.inboxes[0]["binding"]["identity"]["run_ref"]
    finally:
        runtime.close()


def test_actual_bundle_transport_binds_rolling_dispatch_separately(tmp_path):
    provider, runner = _adapter(_BundleTransport, tmp_path)
    runtime = _bundle_runtime(tmp_path / "runtime", bundle_skill_provider=provider)
    runner.runtime = runtime
    runtime.configure_resident_mcp_endpoint("http://127.0.0.1:8999")
    runtime.bundle_stage.configure_resident_mcp_endpoint("http://127.0.0.1:8999")
    runtime.reasoning_stage.configure_resident_mcp_endpoint("http://127.0.0.1:8999")
    try:
        quest = _confirm_direct_quest(runtime)["quest_ref"]
        _submit(runtime, quest)
        _finish_idea_stage(runtime)
        _finish_plan_stage(runtime)
        assert runtime.bundle_stage.process_once()
        for _ in range(4):
            if runtime.bundle_stage.query_current()["run"] is not None and runtime.bundle_stage.query_current()["run"]["attempt_execution_receipt"] is not None:
                break
            assert runtime.bundle_stage.process_once(), (runtime.bundle_stage.transient_error, runtime.bundle_stage.query_current())
        for _ in range(5):
            runtime.bundle_stage.process_once()
            if any(seal["operation_name"].startswith("dispatch-") for seal in _seals(provider)):
                break
        seals = _seals(provider)
        assert {seal["operation_name"] for seal in seals} >= {"primary", "dispatch-1"}
        assert len({seal["guidance_binding"]["identity"]["operation_ref"] for seal in seals}) == len(seals)
        assert len(runner.receipts) == 1
        assert runner.inboxes[-1]["deliveries"][0]["needs_treatment"] is False
    finally:
        runtime.close()


def test_actual_reasoning_transport_gets_guidance_before_provider_seal(tmp_path):
    provider, runner = _adapter(_ReasoningTransport, tmp_path)
    runtime = _reasoning_runtime(tmp_path / "runtime", reasoning_skill=provider)
    runner.runtime = runtime
    runtime.configure_resident_mcp_endpoint("http://127.0.0.1:8999")
    runtime.bundle_stage.configure_resident_mcp_endpoint("http://127.0.0.1:8999")
    runtime.reasoning_stage.configure_resident_mcp_endpoint("http://127.0.0.1:8999")
    try:
        quest = _confirm_deepfetch_quest(runtime)["quest_ref"]
        _submit(runtime, quest)
        _finish_idea_stage(runtime)
        for _ in range(16):
            runtime.reasoning_stage.process_once()
            if len(runner.inboxes) >= 2:
                break
        assert [item["deliveries"][0]["needs_treatment"] for item in runner.inboxes[:2]] == [True, False], (runtime.reasoning_stage.transient_error, runtime.reasoning_stage.query_current())
        assert len(runner.receipts) == 1
        assert {seal["guidance_binding"]["identity"]["root_kind"] for seal in _seals(provider)} == {"reasoning"}
    finally:
        runtime.close()


def test_actual_target_preparation_and_per_call_channel_preserve_admission_grant(tmp_path):
    runtime = _current_bundle_runtime(tmp_path / "runtime")
    runtime.configure_resident_mcp_endpoint("http://127.0.0.1:8999")
    try:
        target, _, _, admission, _ = _admit_independent_target_root(runtime)
        launch = runtime.owners.agent_runtime.query_admitted_target_launch(target.target_ref)
        _submit(runtime, launch.quest_ref)
        adapter = runtime.harnesses._adapters["codex"]
        invoke = adapter.invoke
        observed = []
        def read_then_invoke(invocation):
            if invocation.root_kind == "target":
                assert invocation.mcp_token != admission.connection.token
                inbox = _tool(runtime, invocation.mcp_token, "human_guidance.read")
                observed.append(inbox)
                delivery, = inbox["deliveries"]
                exact = _tool(runtime, invocation.mcp_token, "human_guidance.read",
                    delivery_ref=delivery["delivery_ref"], effect_id="target-read")
                assert exact["original_text"] == "Keep the device audit."
            return invoke(invocation)
        adapter.invoke = read_then_invoke
        result = runtime.harnesses.run_or_resume_target_root(
            admission.run.request_ref, prompt="Continue the target audit.", mcp_base_url="http://127.0.0.1:8999")
        assert result.status == "executed"
        assert observed[0]["binding"]["identity"]["operation_ref"] == adapter.invocations[-1].provider_operation_ref
        assert admission.run.mcp_binding.guidance_binding is None
        status, payload = runtime.harnesses.dispatch_mcp(admission.connection.token, {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "human_guidance.read", "arguments": {}},
        })
        assert status == 200
        assert payload["result"]["structuredContent"]["code"] == "guidance_snapshot_missing"
    finally:
        runtime.close()


def test_guidance_catalog_is_available_only_to_the_five_working_roles(tmp_path):
    for kind, operation_ids in ROOT_AGENT_SEMANTIC_OPERATION_IDS.items():
        available = set(operation_ids) & {"human_guidance.read", "human_guidance.feedback"}
        assert available == ({"human_guidance.read", "human_guidance.feedback"}
            if kind in {"idea", "plan", "bundle", "reasoning", "target"} else set())


def test_actual_empty_operation_retry_keeps_original_cut_after_late_submission(tmp_path):
    class RetryingIdea(_IdeaTransport):
        def generate_draft(self, request):
            _shared_call(self, request, "primary")
            _shared_call(self, request, "primary")
            return _DeterministicIdeaSkill().generate_draft(request)

    provider, runner = _adapter(RetryingIdea, tmp_path)
    runtime = _plan_runtime(tmp_path / "runtime", idea_skill=provider,
        plan_skill=_DeterministicPlanSkill(no_gap=False))
    runner.runtime = runtime
    runtime.configure_resident_mcp_endpoint("http://127.0.0.1:8999")
    try:
        quest = _confirm_direct_quest(runtime)["quest_ref"]
        late = {}
        runner.after_first = lambda _: late.update(_submit(runtime, quest))
        _finish_idea_stage(runtime)
        assert runner.inboxes[0]["deliveries"] == []
        assert len(runner.inboxes) == 2
        assert runner.inboxes[1]["deliveries"][0]["needs_treatment"] is True
        primary = next(item for item in _seals(provider) if item["operation_name"] == "primary")
        rows = runtime.owners.human_collaboration.query_guidance_deliveries(late["constraint_ref"])
        assert len(rows) == 1
        assert rows[0]["operation_ref"] != primary["guidance_binding"]["identity"]["operation_ref"]
    finally:
        runtime.close()


def test_signed_target_operation_retains_note_then_stops_exact_work(tmp_path):
    runtime = _current_bundle_runtime(tmp_path / "runtime")
    runtime.configure_resident_mcp_endpoint("http://127.0.0.1:8999")
    try:
        target, candidate, formal_plan, admission, handle = (
            _admit_independent_target_root(runtime)
        )
        launch = runtime.owners.agent_runtime.query_admitted_target_launch(
            target.target_ref
        )
        runtime.target_root_lifecycle.activate(
            launch_ref=launch.launch_ref,
            handle=handle,
            candidate=candidate,
            formal_plan=formal_plan,
            idempotency_key="activate-goal-evolution-target",
        )
        submitted = _submit(runtime, launch.quest_ref, strength=5)
        adapter = runtime.harnesses._adapters["codex"]
        invoke = adapter.invoke
        observed = {}

        def retain_then_stop(invocation):
            if invocation.root_kind != "target" or observed:
                return invoke(invocation)
            workspace, workspace_path = (
                runtime.target_run_authorities.agent_runtime
                .read_target_workspace_location(invocation.run_ref)
            )
            note_path = workspace_path / "outputs" / "analysis" / "research-note.md"
            note_path.parent.mkdir(parents=True, exist_ok=True)
            note = b"# Interrupted comparison\n\nCalibration remained stable.\n"
            note_path.write_bytes(note)
            selector = {
                "target_ref": target.target_ref,
                "target_run_ref": invocation.run_ref,
                "workspace_ref": workspace.workspace_ref,
                "relative_path": "outputs/analysis/research-note.md",
                "expected_content_hash": hashlib.sha256(note).hexdigest(),
            }
            intake_arguments = {
                "effect_id": "retain-target-note",
                "intake": {
                    "source_kind": "file",
                    "custody_mode": "managed",
                    "display_name": "research-note.md",
                    "media_type": "text/markdown",
                    "target_workspace_note": selector,
                },
            }
            for effect_id, replacement, expected_code in (
                (
                    "reject-target-note-hash",
                    {**selector, "expected_content_hash": "0" * 64},
                    "target_note_hash_mismatch",
                ),
                (
                    "reject-target-note-workspace",
                    {**selector, "workspace_ref": "target_workspace_wrong"},
                    "target_note_workspace_invalid",
                ),
                (
                    "reject-target-note-path",
                    {**selector, "relative_path": "outputs/analysis/other.md"},
                    "semantic_input_schema_mismatch",
                ),
            ):
                _tool_error(
                    runtime,
                    invocation.mcp_token,
                    "research_memory.assets.intake",
                    expected_code,
                    effect_id=effect_id,
                    intake={**intake_arguments["intake"], "target_workspace_note": replacement},
                )
            _tool_error(
                runtime,
                invocation.mcp_token,
                "research_memory.assets.intake",
                "target_note_provenance_reserved",
                effect_id="reject-target-note-provenance",
                intake={
                    "source_kind": "text",
                    "custody_mode": "managed",
                    "display_name": "forged-note.md",
                    "media_type": "text/markdown",
                    "text": "caller-selected producer identity",
                    "provenance": {
                        "schema_ref": "meta-research/target-workspace-note/v1",
                        "producer_kind": "target_workspace",
                    },
                },
            )
            linked_path = workspace_path / "handoff" / "final-message.md"
            linked_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                linked_path.symlink_to(note_path)
            except OSError:
                pass
            else:
                _tool_error(
                    runtime,
                    invocation.mcp_token,
                    "research_memory.assets.intake",
                    "target_note_symlink_forbidden",
                    effect_id="reject-target-note-symlink",
                    intake={
                        **intake_arguments["intake"],
                        "target_workspace_note": {
                            **selector,
                            "relative_path": "handoff/final-message.md",
                        },
                    },
                )
                linked_path.unlink()
            asset_result = _tool(
                runtime,
                invocation.mcp_token,
                "research_memory.assets.intake",
                **intake_arguments,
            )
            asset = asset_result["asset"]
            binding = runtime.owners.research_memory.query_asset_version(
                asset["version_ref"]
            )
            role = runtime.owners.research_graph.accept_asset_role(
                binding=binding.as_binding(),
                role="quest_source_material",
                quest_ref=launch.quest_ref,
                idempotency_key="retain-target-note-role",
            )
            inbox = _tool(
                runtime, invocation.mcp_token, "human_guidance.read"
            )
            delivery, = inbox["deliveries"]
            exact = _tool(
                runtime,
                invocation.mcp_token,
                "human_guidance.read",
                delivery_ref=delivery["delivery_ref"],
                effect_id="read-stop-direction",
            )
            _tool(
                runtime,
                invocation.mcp_token,
                "human_guidance.feedback",
                delivery_ref=delivery["delivery_ref"],
                effect_id="feedback-stop-direction",
                understanding="Retain the useful note and stop this comparison.",
                changes="The comparison is no longer part of the whole-Quest direction.",
                continuing_work="Preserve the accepted calibration note.",
                reasons="The exact note remains useful evidence for later work.",
                disposition="goal_alignment_pending",
            )
            view = _tool(
                runtime, invocation.mcp_token, "research_graph.quest_goal.read"
            )
            cut = view["operation_basis"]
            decisions = []
            for work in cut["work"]["items"]:
                if work["target_ref"] == target.target_ref:
                    decisions.append(
                        {
                            "kind": "stop",
                            "work": work,
                            "reason": "The revised direction no longer needs this comparison.",
                            "retention": {
                                "kind": "selected",
                                "assets": [
                                    {
                                        "asset_ref": asset["asset_ref"],
                                        "version_ref": asset["version_ref"],
                                        "content_hash": asset["content_hash"],
                                        "manifest_hash": asset["manifest_hash"],
                                        "receipt": asset["receipt"],
                                        "meaning": "Calibration evidence from the interrupted comparison.",
                                    }
                                ],
                            },
                        }
                    )
                else:
                    decisions.append(
                        {
                            "kind": "continue",
                            "work": work,
                            "reason": "This work remains useful under its frozen contract.",
                        }
                    )
            decision = {
                "expected_revision": cut["goal"]["goal_revision_ref"],
                "conditions_basis": cut["conditions"]["basis_ref"],
                "work_basis": cut["work"]["basis_ref"],
                "cause": {
                    "kind": "human_guidance",
                    "delivery_ref": delivery["delivery_ref"],
                    "guide_ref": exact["guide_ref"],
                },
                "replacement": {
                    "goal": "Concentrate on the remaining calibrated comparison.",
                    "completion_criteria": "The remaining comparison has a reproducible evidence chain.",
                },
                "criteria_review": "The prior criteria required unrelated work and are replaced together.",
                "judgment": "The retained note is useful while this Target is unrelated.",
                "conditions": {
                    "runtime_conditions_ref": cut["conditions"][
                        "runtime_conditions"
                    ]["revision"],
                    "assessments": [
                        {
                            "condition_ref": item["condition_ref"],
                            "disposition": "preserved",
                            "explanation": "The sourced condition remains applicable.",
                        }
                        for item in cut["conditions"]["enduring"]
                    ],
                    "newly_identified": [
                        {
                            "delivery_ref": delivery["delivery_ref"],
                            "start": 0,
                            "end": len(exact["original_text"]),
                            "text": exact["original_text"],
                            "meaning": "Keep the device audit as an enduring condition.",
                        }
                    ],
                },
                "arrangements": {"decisions": decisions},
                "following_direction": "The new goal follows the exact human direction.",
            }
            evolved = _tool(
                runtime,
                invocation.mcp_token,
                "research_graph.quest_goal.evolve",
                effect_id="stop-after-retention",
                decision=decision,
            )
            replay = _tool(
                runtime,
                invocation.mcp_token,
                "research_graph.quest_goal.evolve.reconcile",
                effect_id="stop-after-retention",
                decision=decision,
            )
            assert replay["goal"] == evolved["goal"]
            refreshed = _tool(
                runtime, invocation.mcp_token, "research_graph.quest_goal.read"
            )
            assert refreshed["current"] == evolved["goal"]
            assert refreshed["operation_basis"] == cut
            note_path.unlink()
            asset_replay = _tool(
                runtime,
                invocation.mcp_token,
                "research_memory.assets.intake.reconcile",
                **intake_arguments,
            )
            assert asset_replay["status"] == "accepted"
            assert asset_replay["result"]["asset"] == asset
            observed.update(
                token=invocation.mcp_token,
                note_path=note_path,
                note=note,
                intake_arguments=intake_arguments,
                asset=asset,
                role=role,
                guide_ref=exact["guide_ref"],
                decision=decision,
                evolved=evolved,
                replay=replay,
                asset_replay=asset_replay,
                initial_revision=cut["goal"],
                first_operation_ref=invocation.provider_operation_ref,
            )
            return invoke(invocation)

        adapter.invoke = retain_then_stop
        result = runtime.harnesses.run_or_resume_target_root(
            admission.run.request_ref,
            prompt="Retain useful work before following the revised direction.",
            mcp_base_url="http://127.0.0.1:8999",
        )
        assert result.status == "executed"
        second_observed = {}

        def evolve_from_retained_evidence(invocation):
            if invocation.root_kind != "target" or second_observed:
                return invoke(invocation)
            view = _tool(
                runtime,
                invocation.mcp_token,
                "research_graph.quest_goal.read",
            )
            cut = view["operation_basis"]
            assert cut["goal"] == observed["evolved"]["goal"]
            assert invocation.provider_operation_ref != observed["first_operation_ref"]
            work, = cut["work"]["items"]
            preserved = [
                {
                    "condition_ref": item["condition_ref"],
                    "disposition": "preserved",
                    "explanation": (
                        "The sourced device condition remains applicable."
                    ),
                }
                for item in cut["conditions"]["enduring"]
            ]
            decision = {
                "expected_revision": cut["goal"]["goal_revision_ref"],
                "conditions_basis": cut["conditions"]["basis_ref"],
                "work_basis": cut["work"]["basis_ref"],
                "cause": {
                    "kind": "evidence",
                    "evidence": [
                        {
                            "source_ref": observed["asset"]["asset_ref"],
                            "version_ref": observed["asset"]["version_ref"],
                        }
                    ],
                },
                "replacement": {
                    "goal": "Use retained calibration evidence for the remaining comparison.",
                    "completion_criteria": (
                        "The remaining comparison cites the retained calibration note "
                        "and preserves the device condition."
                    ),
                },
                "criteria_review": (
                    "The accepted note narrows the evidence needed for the successor direction."
                ),
                "judgment": (
                    "Accepted retained evidence supports a more precise direction."
                ),
                "conditions": {
                    "runtime_conditions_ref": cut["conditions"][
                        "runtime_conditions"
                    ]["revision"],
                    "assessments": preserved,
                    "newly_identified": [],
                },
                "arrangements": {
                    "decisions": [
                        {
                            "kind": "stop",
                            "work": work,
                            "reason": (
                                "The evidence-driven successor still excludes this comparison."
                            ),
                            "retention": {
                                "kind": "selected",
                                "assets": [
                                    {
                                        "asset_ref": observed["asset"]["asset_ref"],
                                        "version_ref": observed["asset"]["version_ref"],
                                        "content_hash": observed["asset"]["content_hash"],
                                        "manifest_hash": observed["asset"]["manifest_hash"],
                                        "receipt": observed["asset"]["receipt"],
                                        "meaning": (
                                            "Calibration evidence retained from the interrupted comparison."
                                        ),
                                    }
                                ],
                            },
                        }
                    ]
                },
                "following_direction": (
                    "Reasoning uses the retained evidence under the enduring condition."
                ),
            }
            omitted = json.loads(json.dumps(decision))
            omitted["conditions"]["assessments"] = []
            status, response, _headers = runtime.harnesses.dispatch_mcp_http(
                invocation.mcp_token,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "research_graph.quest_goal.evolve",
                        "arguments": {
                            "effect_id": "omit-enduring-condition",
                            "decision": omitted,
                        },
                    },
                },
                mcp_session_id=None,
            )
            assert status == 200
            assert response["result"]["isError"] is True
            assert response["result"]["structuredContent"]["code"] == (
                "goal_conditions_coverage_invalid"
            )
            evolved = _tool(
                runtime,
                invocation.mcp_token,
                "research_graph.quest_goal.evolve",
                effect_id="evidence-direction-v2",
                decision=decision,
            )
            replay = _tool(
                runtime,
                invocation.mcp_token,
                "research_graph.quest_goal.evolve.reconcile",
                effect_id="evidence-direction-v2",
                decision=decision,
            )
            second_observed.update(
                cut=cut,
                decision=decision,
                evolved=evolved,
                replay=replay,
                operation_ref=invocation.provider_operation_ref,
            )
            return invoke(invocation)

        adapter.invoke = evolve_from_retained_evidence
        second_result = runtime.harnesses.run_or_resume_target_root(
            admission.run.request_ref,
            prompt="Reassess the revised direction from retained evidence.",
            mcp_base_url="http://127.0.0.1:8999",
        )
        assert second_result.status == "executed"
        assert second_observed["replay"]["goal"] == second_observed["evolved"][
            "goal"
        ]
        assert observed["role"].version_ref == observed["asset"]["version_ref"]
        assert runtime.owners.research_memory.read_asset_entry_text(
            observed["asset"]["version_ref"]
        ) == observed["note"].decode("utf-8")
        assert runtime.goal_work_reconciler.process_once()
        cancelled = runtime.target_root_lifecycle.mark_cancelled(
            target_ref=target.target_ref
        )
        notice = runtime.owners.agent_runtime.query_target_work_notice(
            target.target_ref
        )
        assert notice is not None
        handoff = runtime.owners.agent_runtime.read_target_run_handoff(
            notice.handoff_manifest_ref
        )
        assert type(handoff.terminal) is GoalWorkDisposition
        assert handoff.terminal.disposition == "cancelled"
        assert handoff.terminal.terminal_fact_ref == cancelled.cancel_ref
        assert handoff.terminal.retention_kind == "selected"
        assert {
            observed["asset"]["asset_ref"],
            observed["asset"]["version_ref"],
            observed["asset"]["receipt"]["receipt_ref"],
            observed["role"].role_ref,
            observed["role"].receipt.receipt_ref,
        }.issubset(handoff.terminal.custody_refs)
        frontier = runtime.owners.agent_runtime.query_target_frontier_entry(
            target.target_ref
        )
        assert frontier is not None
        assert frontier.state == "terminal"
        assert frontier.terminal_fact_ref == cancelled.cancel_ref
        bundle_run = runtime.owners.agent_runtime.query_bundle_stage_run(
            launch.stage_request_ref
        )
        assert bundle_run is not None
        inbox = runtime.owners.agent_runtime.read_bundle_inbox(
            run_ref=bundle_run.run_ref,
            attempt_ref=bundle_run.attempt_ref,
            fence_ref=bundle_run.fence_ref,
        )
        assert tuple(item.notice_ref for item in inbox.notices) == (
            notice.notice_ref,
        )
        checkpoint = runtime.owners.agent_runtime.acknowledge_bundle_inbox(
            run_ref=bundle_run.run_ref,
            attempt_ref=bundle_run.attempt_ref,
            fence_ref=bundle_run.fence_ref,
            batch=inbox,
            idempotency_key="ack-goal-cancellation-notice",
        )
        assert checkpoint.cursor == notice.sequence
        graph = runtime.owners.research_graph.query_target_graph(
            launch.stage_request_ref
        )
        assert graph is not None
        assert runtime.bundle_stage._bundle_report_disposition_hint(graph) == (
            "replan_required"
        )
        source = (
            runtime.owners.research_graph.query_formal_plan_content_acceptance(
                graph.formal_plan_ref
            )
        )
        projection = (
            runtime.owners.research_graph.query_target_formal_plan_projection(
                graph_ref=graph.graph_ref
            )
        )
        assert source is not None and projection is not None
        report = runtime.owners.agent_runtime.build_bundle_report_candidate(
            run_ref=bundle_run.run_ref,
            attempt_ref=bundle_run.attempt_ref,
            fence_ref=bundle_run.fence_ref,
            disposition="replan_required",
            formal_plan_content_receipt=source.receipt,
            formal_plan_projection_receipt=projection.receipt,
            target_graph_ref=graph.graph_ref,
            target_graph_receipt=graph.receipt,
        )
        assert report.disposition == "replan_required"
        assert handoff.terminal.reason in report.semantic_change_required
        assert handoff.terminal.goal_work_intent_ref in report.evidence_refs
        assert handoff.terminal.goal_revision_receipt_ref in report.owner_receipt_refs
        with runtime._database.read() as connection:
            assert connection.exec_driver_sql(
                "SELECT COUNT(*) FROM ar_target_root_completions WHERE "
                "target_ref = ?",
                (target.target_ref,),
            ).scalar_one() == 0
            assert connection.exec_driver_sql(
                "SELECT COUNT(*) FROM rm_target_root_completion_manifests WHERE "
                "target_ref = ?",
                (target.target_ref,),
            ).scalar_one() == 0
            assert connection.exec_driver_sql(
                "SELECT COUNT(*) FROM rg_target_root_measurements WHERE "
                "target_ref = ?",
                (target.target_ref,),
            ).scalar_one() == 0
            assert connection.exec_driver_sql(
                "SELECT COUNT(*) FROM rg_target_commits WHERE target_ref = ?",
                (target.target_ref,),
            ).scalar_one() == 0
        durable_effect = (
            runtime.owners.research_memory
            .query_target_note_intake_effect_record(
                "mcp-asset:"
                + canonical_hash(
                    {
                        "run_ref": admission.run.run_ref,
                        "root_session_ref": admission.run.root_session_ref,
                        "root_kind": "target",
                        "action": "intake",
                        "effect_id": "retain-target-note",
                    }
                )
            )
        )
        assert durable_effect["result"]["asset"] == observed["asset"]
        historical = runtime.owners.research_graph.query_quest_goal_revision(
            observed["initial_revision"]["goal_revision_ref"]
        )
        assert historical == observed["initial_revision"]
        current = runtime.owners.research_graph.query_quest_goal_view(
            launch.quest_ref
        )
        stops = [
            item
            for item in current["work"]["intents"]
            if item["decision"]["kind"] == "stop"
        ]
        assert [item["actual_status"] for item in stops] == [
            "superseded",
            "cancelled",
        ]
        assert stops[0]["source_intent_ref"] == stops[1]["intent_ref"]
        assert current["current"] == second_observed["evolved"]["goal"]
        assert current["conditions"]["enduring"] == second_observed["cut"][
            "conditions"
        ]["enduring"]
        assert current["guidance_alignment"][0]["status"] == "aligned"
        assert submitted["constraint_ref"] == observed["guide_ref"]["constraint_ref"]
    finally:
        runtime.close()
