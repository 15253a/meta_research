"""Quest clock observations reach providers without changing an issued operation."""

import json
import sys

import pytest

from meta_research.harness import HarnessAdmissionError
from meta_research.harness_adapters import CodexHarnessAdapter, HarnessAdapterUnavailable
from meta_research.runtime_conditions import split_runtime_prompt
from test_harness_frozen_terminal_recovery import interrupted_root  # noqa: F401
from meta_research.bundle_skill import CodexBundleSkillAdapter
from meta_research.idea_skill import CodexIdeaSkillAdapter
from meta_research.plan_skill import CodexPlanSkillAdapter
from meta_research.reasoning_skill import CodexReasoningSkillAdapter
from meta_research.paths import prepare_data_root
from test_idea_owner_integrity import _confirm_question_with_prefix
from test_idea_stage_recovery import _IdeaProvider, _runtime
from test_runtime_conditions_provider_prompts import _SignedRunner


def test_target_unknown_outcome_before_transport_keeps_first_budget_observation(request, monkeypatch):
    observations = ["Quest shared budget: used=10; remaining=50; observed_at=1000; revision=initial"]
    monkeypatch.setattr(
        "meta_research.harness.render_runtime_conditions", lambda *_args, **_scope: observations[0],
    )
    invoke = CodexHarnessAdapter.invoke
    invoke_terminal = CodexHarnessAdapter.invoke_terminal
    original_invocations = []
    reconciliations = []

    def lost_before_transport(_adapter, _invocation):
        original_invocations.append(_invocation)
        raise HarnessAdapterUnavailable("provider_io_unavailable", durable_outcome="unknown")

    def read_original_terminal(adapter, invocation):
        reconciliations.append(invocation)
        return invoke_terminal(adapter, invocation)

    monkeypatch.setattr(CodexHarnessAdapter, "invoke", lost_before_transport)
    _database, owner, target, runner, runtime = request.getfixturevalue("interrupted_root")
    original = owner.latest_operation(owner.query_run(target.request_ref).run_ref)
    assert runner.calls == 0
    observations[0] = "Quest shared budget: used=35; remaining=25; observed_at=1025; revision=initial"
    monkeypatch.setattr(CodexHarnessAdapter, "invoke", invoke)
    monkeypatch.setattr(CodexHarnessAdapter, "invoke_terminal", read_original_terminal)

    with pytest.raises(HarnessAdmissionError, match="provider_outcome_unknown"):
        runtime().run_or_resume_target_root(
            target.request_ref, prompt="Original frozen Target work.",
            mcp_base_url="http://127.0.0.1:8999",
        )

    recovered = owner.latest_operation(original.run_ref)
    assert recovered.operation_ref == original.operation_ref
    assert recovered.invocation_hash == original.invocation_hash
    assert recovered.generation == 1
    assert recovered.status == "unknown_outcome"
    assert recovered.reconciliation_generation == 1
    assert len(reconciliations) == 1
    retained = reconciliations[0]
    assert retained.entry_path == "recovery"
    assert retained.provider_operation_ref == original.operation_ref
    assert retained.owner_invocation_hash == original.invocation_hash
    assert retained.prompt == original_invocations[0].prompt
    # An unknown outcome without its signed receipt cannot authorize another
    # physical call just to reconstruct the spool or refresh the observation.
    assert runner.calls == 0
    assert runner.request_path is None
    conditions, business = split_runtime_prompt(retained.prompt)
    assert conditions == "Quest shared budget: used=10; remaining=50; observed_at=1000; revision=initial"
    assert business == "Original frozen Target work."


@pytest.mark.parametrize("adapter_type", (
    CodexIdeaSkillAdapter, CodexPlanSkillAdapter, CodexBundleSkillAdapter, CodexReasoningSkillAdapter,
))
def test_research_provider_observes_shared_clock_at_new_operation_only(tmp_path, monkeypatch, adapter_type):
    now = [1000.0]
    monkeypatch.setattr("time.time", lambda: now[0])
    data = prepare_data_root(tmp_path / "clock")
    runtime = _runtime(data, _IdeaProvider())
    try:
        ae = runtime.owners.advancement_engine
        ae.start_research_time_service()
        accepted = _confirm_question_with_prefix(runtime, "provider-time")
        runtime.idea_stage.start("provider-time-start")
        run = runtime.idea_stage.query_current()["run"]
        runner = _SignedRunner()
        adapter = adapter_type(data.root / "clock-provider", executable=sys.executable, process_runner=runner)

        def invoke(job, native=None):
            return adapter._invoke_root_operation(
                operation_name="primary", prompt="Carry out the unchanged scientific task.",
                schema={"type": "object", "properties": {"ok": {"type": "boolean"}},
                        "required": ["ok"], "additionalProperties": False},
                native_session_ref=native, job_ref=job, run_ref=run["run_ref"],
                attempt_ref=None, root_session_ref=run["root_session_ref"], fence_ref=None,
            )

        now[0] = 1060.0
        first = invoke("clock:first")
        first_conditions, business = split_runtime_prompt(runner.calls[0][1])
        first_snapshot = json.loads(first_conditions.splitlines()[-1])["research_time"]
        assert first_snapshot == ae.query_research_time(accepted["quest_ref"])
        assert first_snapshot["used_seconds"] == 60.0
        assert first_snapshot["remaining_seconds"] == 2591940.0
        assert first_snapshot["budget_policy"] == "advisory"

        now[0] = 1120.0
        assert invoke("clock:first") == first
        assert len(runner.calls) == 1
        invoke("clock:next", first[1])
        next_conditions, next_business = split_runtime_prompt(runner.calls[1][1])
        next_snapshot = json.loads(next_conditions.splitlines()[-1])["research_time"]
        assert next_snapshot["used_seconds"] == 120.0
        assert next_snapshot["remaining_seconds"] == 2591880.0
        assert next_snapshot["budget_revision"] == first_snapshot["budget_revision"]
        assert next_snapshot["observed_at"] == 1120.0
        assert next_snapshot["budget_policy"] == "advisory"
        assert "1200 秒" in next_conditions
        assert "不强制停止、收口或先申请延期" in next_conditions
        assert next_business == business
    finally:
        runtime.close()


def test_bundle_old_budget_prompt_reconciles_unchanged_then_new_resume_gets_advisory_policy(tmp_path, monkeypatch):
    from meta_research.owners.common import canonical_hash

    monkeypatch.setattr("time.time", lambda: 1000.0)
    data = prepare_data_root(tmp_path / "policy-refresh")
    runtime = _runtime(data, _IdeaProvider())
    try:
        runtime.owners.advancement_engine.start_research_time_service()
        _confirm_question_with_prefix(runtime, "old-policy")
        runtime.idea_stage.start("old-policy-start")
        run = runtime.idea_stage.query_current()["run"]
        runner = _SignedRunner()
        workspace = data.root / "bundle-policy-provider"
        adapter = CodexBundleSkillAdapter(workspace, executable=sys.executable, process_runner=runner)

        def invoke(job, native=None):
            return adapter._invoke_root_operation(
                operation_name="primary", prompt="Keep original work and evidence.",
                schema={"type": "object", "properties": {"ok": {"type": "boolean"}},
                        "required": ["ok"], "additionalProperties": False},
                native_session_ref=native, job_ref=job, run_ref=run["run_ref"],
                attempt_ref=None, root_session_ref=run["root_session_ref"], fence_ref=None,
            )

        old_conditions = 'Historical budget policy: extension first.\n{"research_time":{"approved_seconds":14400}}'
        with monkeypatch.context() as old_release:
            old_release.setattr("meta_research.idea_skill.render_runtime_conditions", lambda *_args, **_kwargs: old_conditions)
            first = invoke("old-budget-operation")
        directory = workspace / "provider-operations" / canonical_hash({"job_ref": "old-budget-operation"}) / "primary"
        original_files = {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}
        assert invoke("old-budget-operation") == first
        assert len(runner.calls) == 1
        assert {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()} == original_files
        assert split_runtime_prompt(runner.calls[0][1])[0] == old_conditions
        invoke("new-budget-decision", first[1])
        assert len(runner.calls) == 2
        current, business = split_runtime_prompt(runner.calls[1][1])
        assert json.loads(current.splitlines()[-1])["research_time"]["budget_policy"] == "advisory"
        assert "新决策使用当前建议预算语义" in current
        assert "1200 秒" in current
        assert "extension first" not in current
        assert business == "Keep original work and evidence."
        invocation = json.loads((directory.parent.parent / canonical_hash({"job_ref": "new-budget-decision"}) / "primary" / "invocation.json").read_text())
        assert invocation["payload"]["native_session_ref"] == first[1]
    finally:
        runtime.close()


def test_budget_reader_uses_authenticated_root_scope_and_live_owner_clock(tmp_path, monkeypatch):
    from jsonschema import Draft202012Validator, ValidationError
    from fastapi.testclient import TestClient
    from meta_research.web import create_app
    from test_root_human_request_lifecycle import (
        _runtime as bundle_runtime, _bundle_root_channel, _initialized_child, _tool_call,
    )

    now = [1000.0]
    monkeypatch.setattr("time.time", lambda: now[0])
    runtime = bundle_runtime(tmp_path / "budget-reader")
    try:
        ae = runtime.owners.advancement_engine
        ae.start_research_time_service()
        run, channel = _bundle_root_channel(runtime)
        quest = runtime.owners.agent_runtime.query_managed_run(run.run_ref)["quest_ref"]
        client = TestClient(create_app(runtime, base_url="http://testserver", control_key="control-key"))
        try:
            headers = _initialized_child(client, channel.connection.token)
            now[0] = 1060.0
            result = _tool_call(client, headers, operation_id="advancement_engine.research_time.read",
                                arguments={}, request_id=2)
            assert result["isError"] is False
            assert result["structuredContent"] == ae.query_research_time(quest)
            assert result["structuredContent"]["budget_policy"] == "advisory"
            contract = _tool_call(client, headers, operation_id="tools.contracts.read",
                arguments={"operation_id": "advancement_engine.research_time.read"}, request_id=20)
            schema = contract["structuredContent"]["outputSchema"]

            def assert_closed(value):
                if value.get("type") == "object":
                    assert value.get("additionalProperties") is False
                    for child in value.get("properties", {}).values():
                        assert_closed(child)
                elif value.get("type") == "array":
                    assert_closed(value["items"])

            assert_closed(schema)
            validator = Draft202012Validator(schema)
            validator.validate(result["structuredContent"])
            with pytest.raises(ValidationError):
                validator.validate({**result["structuredContent"], "invented_balance": 10})
            interval = {"start": 1000.0, "end": 1050.0, "reason": "service_stop_unobserved"}
            unknown = {**result["structuredContent"], "accuracy": "unknown", "used_seconds": None,
                       "remaining_seconds": None, "overrun_seconds": None, "unknown_intervals": [interval]}
            validator.validate(unknown)
            with pytest.raises(ValidationError):
                validator.validate({**unknown, "unknown_intervals": [{**interval, "invented_duration": 50}]})
            now[0] = 1120.0
            later = _tool_call(client, headers, operation_id="advancement_engine.research_time.read",
                               arguments={}, request_id=3)
            assert later["structuredContent"]["used_seconds"] == 120.0
            denied = _tool_call(client, headers, operation_id="advancement_engine.research_time.read",
                                arguments={"quest_ref": "another-quest"}, request_id=4)
            assert denied["isError"] is True
            assert denied["structuredContent"]["code"] == "semantic_input_schema_mismatch"
        finally:
            client.close()
    finally:
        runtime.close()
