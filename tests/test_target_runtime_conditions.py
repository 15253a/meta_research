"""A configuration edit changes the next Target turn, never its sealed recovery."""

import pytest

from meta_research.harness import HarnessAdmissionError
from meta_research.harness_adapters import CodexHarnessAdapter, HarnessAdapterUnavailable
from meta_research.runtime_conditions import split_runtime_prompt
from test_harness_frozen_terminal_recovery import interrupted_root  # noqa: F401


def test_target_conditions_survive_recovery_and_refresh_on_next_turn(request, monkeypatch):
    conditions = ["用户选择 GPU-A100-1，预算30d"]
    scopes = []

    def current(_workspace, **scope):
        scopes.append(scope)
        return conditions[0]

    monkeypatch.setattr("meta_research.harness.render_runtime_conditions", current)
    _db, owner, target_request, runner, runtime = request.getfixturevalue("interrupted_root")
    path = runner.request_path.with_name("prompt.txt")
    frozen = path.read_text(encoding="utf-8")
    assert split_runtime_prompt(frozen) == (conditions[0], "Original frozen Target work.")
    run = owner.query_run(target_request.request_ref)
    first = owner.latest_operation(run.run_ref)
    assert scopes == [{"run_ref": run.run_ref, "target_ref": target_request.target_ref}]

    conditions[0] = "当前仅使用 GPU-A100-2，预算7d"
    with pytest.raises(HarnessAdmissionError, match="provider_stopped"):
        runtime().run_or_resume_target_root(
            target_request.request_ref, prompt="Original frozen Target work.",
            mcp_base_url="http://127.0.0.1:8999",
        )
    assert runner.calls == 1
    assert path.read_text(encoding="utf-8") == frozen
    assert owner.latest_operation(run.run_ref).invocation_hash == first.invocation_hash
    assert len(scopes) == 1

    monkeypatch.setattr("meta_research.owners.agent_runtime_harness._TARGET_ROOT_RETRY_BASE_SECONDS", 0.0)
    resumed = runtime()
    resumed.recover_failed_target_root(target_request.request_ref)
    with pytest.raises(HarnessAdmissionError, match="provider_io_unavailable"):
        resumed.run_or_resume_target_root(
            target_request.request_ref, prompt="Original frozen Target work.",
            mcp_base_url="http://127.0.0.1:8999",
        )
    assert split_runtime_prompt(runner.continuation_prompt) == (
        conditions[0], "Original frozen Target work.",
    )
    assert runner.continuation_argv[-3:] == ["resume", "retained-native-session", "-"]
    assert runner.calls == 2
    assert owner.latest_operation(run.run_ref).generation == 2


@pytest.mark.parametrize("changed", (None, "conditions", "mcp_endpoint"))
def test_target_recovers_before_transport_only_with_exact_original_invocation(
    request, monkeypatch, changed,
):
    conditions = ["Selected GPU: GPU-A; time budget: 30d"]
    monkeypatch.setattr(
        "meta_research.harness.render_runtime_conditions",
        lambda _workspace, **_scope: conditions[0],
    )
    invoke = CodexHarnessAdapter.invoke

    def fail_before_transport(_adapter, _invocation):
        raise HarnessAdapterUnavailable("provider_io_unavailable", durable_outcome="unknown")

    monkeypatch.setattr(CodexHarnessAdapter, "invoke", fail_before_transport)
    _db, owner, target_request, runner, runtime = request.getfixturevalue("interrupted_root")
    run = owner.query_run(target_request.request_ref)
    original = owner.latest_operation(run.run_ref)
    assert original.status == "unknown_outcome"
    assert runner.calls == 0
    assert runner.request_path is None
    monkeypatch.setattr(CodexHarnessAdapter, "invoke", invoke)
    if changed == "conditions":
        conditions[0] = "Selected GPU: GPU-B; time budget: 7d"
    endpoint = "http://127.0.0.1:8998" if changed == "mcp_endpoint" else "http://127.0.0.1:8999"
    expected = "harness_operation_conflict" if changed else "provider_outcome_unknown"
    with pytest.raises(HarnessAdmissionError, match=expected):
        runtime().run_or_resume_target_root(
            target_request.request_ref, prompt="Original frozen Target work.",
            mcp_base_url=endpoint,
        )
    recovered = owner.latest_operation(run.run_ref)
    assert recovered.operation_ref == original.operation_ref
    assert recovered.invocation_hash == original.invocation_hash
    assert recovered.generation == original.generation == 1
    if changed:
        assert runner.calls == 0
        assert runner.request_path is None
        assert recovered.reconciliation_generation == 0
        return
    assert runner.calls == 1
    assert recovered.reconciliation_generation == 1
    assert split_runtime_prompt(runner.request_path.with_name("prompt.txt").read_text()) == (
        conditions[0], "Original frozen Target work.",
    )

    def no_fresh_conditions(*_args, **_kwargs):
        raise AssertionError("Signed terminal replay must not read current conditions")

    monkeypatch.setattr("meta_research.harness.render_runtime_conditions", no_fresh_conditions)
    with pytest.raises(HarnessAdmissionError, match="provider_stopped"):
        runtime().run_or_resume_target_root(
            target_request.request_ref, prompt="Original frozen Target work.",
            mcp_base_url=endpoint,
        )
    assert owner.query_run(target_request.request_ref).native_session_ref == "retained-native-session"
    assert owner.latest_operation(run.run_ref).operation_ref == original.operation_ref
    assert runner.calls == 1
