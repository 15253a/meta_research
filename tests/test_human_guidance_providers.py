from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from meta_research.bundle_skill import CodexBundleSkillAdapter
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
