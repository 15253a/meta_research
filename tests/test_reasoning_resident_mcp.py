from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from meta_research.composition import build_production_runtime
from meta_research.harness import HarnessAdmissionError, ResidentMcpChannel
from meta_research.paths import prepare_data_root
from meta_research.reasoning_skill import (
    CodexReasoningSkillAdapter,
    REASONING_ROOT_SEMANTIC_OPERATION_IDS,
    ReasoningSkillUnavailable,
)

from test_reasoning_skill_adapter import (
    _FullConformanceAuthority,
    _SequenceRunner,
    _fake_codex,
    _request,
    _stage_output,
)
from test_harness_full_conformance import _FullConformanceAdapter, _full_request
from test_public_plan_stage import (
    _DeterministicDraftingAdapter,
    _DeterministicIdeaSkill,
    _DeterministicPlanSkill,
    _DeterministicProbe,
    _confirm_direct_quest,
    _finish_idea_stage,
)
from test_public_reasoning_stage import (
    _DeterministicReasoningSkill,
    _confirm_deepfetch_quest,
    _reasoning_runtime,
)


def _adapter(
    tmp_path: Path,
    runner: _SequenceRunner,
) -> CodexReasoningSkillAdapter:
    tmp_path.mkdir(parents=True, exist_ok=True)
    return CodexReasoningSkillAdapter(
        tmp_path / "provider",
        executable=str(_fake_codex(tmp_path / "codex")),
        model_ref="test-model",
        process_runner=runner,
    )


def test_missing_resident_mcp_fails_before_provider(tmp_path: Path) -> None:
    runner = _SequenceRunner([_stage_output()])
    adapter = _adapter(tmp_path, runner)
    request = replace(_request(), runtime_binding=adapter.runtime_binding())

    with pytest.raises(
        ReasoningSkillUnavailable,
        match="reasoning_semantic_mcp_unavailable",
    ):
        adapter.generate_draft(request)

    assert runner.calls == []


def test_missing_required_operation_fails_before_provider(tmp_path: Path) -> None:
    runner = _SequenceRunner([_stage_output()])
    authority = _FullConformanceAuthority()
    authority.binding = replace(
        authority.binding,
        required_operation_ids=REASONING_ROOT_SEMANTIC_OPERATION_IDS[:-1],
    )
    adapter = _adapter(tmp_path, runner)
    adapter.bind_full_conformance_authority(authority)

    with pytest.raises(
        ReasoningSkillUnavailable,
        match="reasoning_semantic_mcp_conformance_incomplete",
    ):
        adapter.runtime_binding()

    assert runner.calls == []
    assert authority.issued == []


class _MalformedChannelAuthority(_FullConformanceAuthority):
    def __init__(self, mutation: str) -> None:
        super().__init__()
        self._mutation = mutation

    def issue_resident_mcp_channel(self, **kwargs: object) -> ResidentMcpChannel:
        channel = super().issue_resident_mcp_channel(**kwargs)  # type: ignore[arg-type]
        bindings = list(channel.binding.operation_bindings)
        if self._mutation == "missing_currentness":
            bindings = bindings[1:]
        elif self._mutation == "effect_without_reconcile":
            bindings[1] = {
                **bindings[1],
                "access_mode": "effect",
                "reconciliation_operation_id": None,
            }
        else:  # pragma: no cover - protects the test double itself
            raise AssertionError(self._mutation)
        return replace(
            channel,
            binding=replace(
                channel.binding,
                operation_bindings=tuple(bindings),
            ),
        )


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("missing_currentness", "reasoning_semantic_mcp_currentness_unavailable"),
        (
            "effect_without_reconcile",
            "reasoning_semantic_mcp_reconciliation_unavailable",
        ),
    ],
)
def test_malformed_effect_admission_fails_before_provider(
    tmp_path: Path,
    mutation: str,
    code: str,
) -> None:
    runner = _SequenceRunner([_stage_output()])
    authority = _MalformedChannelAuthority(mutation)
    adapter = _adapter(tmp_path, runner)
    adapter.bind_full_conformance_authority(authority)
    adapter.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
    request = replace(_request(), runtime_binding=adapter.runtime_binding())

    with pytest.raises(ReasoningSkillUnavailable, match=code):
        adapter.generate_draft(request)

    assert runner.calls == []
    assert len(authority.revoked) == 1


def test_missing_currentness_trace_is_not_accepted_as_skill_output(
    tmp_path: Path,
) -> None:
    runner = _SequenceRunner(
        [_stage_output()],
        observed_operation_ids=REASONING_ROOT_SEMANTIC_OPERATION_IDS[1:],
    )
    authority = _FullConformanceAuthority()
    adapter = _adapter(tmp_path, runner)
    adapter.bind_full_conformance_authority(authority)
    adapter.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
    request = replace(
        _request(),
        runtime_binding=adapter.runtime_binding(),
        job_ref="reasoning-missing-currentness-job",
    )

    with pytest.raises(
        ReasoningSkillUnavailable,
        match="reasoning_primary_result_contract_invalid",
    ) as caught:
        adapter.generate_draft(request)

    assert caught.value.recovery_checkpoint is not None
    assert caught.value.recovery_checkpoint["contract_failure_detail_code"] == (
        "reasoning_semantic_mcp_currentness_unobserved"
    )

    assert len(runner.calls) == 1
    assert len(authority.revoked) == 1


def test_reasoning_observations_must_follow_the_fixed_currentness_order(
    tmp_path: Path,
) -> None:
    runner = _SequenceRunner(
        [_stage_output()],
        observed_operation_ids=(
            REASONING_ROOT_SEMANTIC_OPERATION_IDS[0],
            REASONING_ROOT_SEMANTIC_OPERATION_IDS[2],
            REASONING_ROOT_SEMANTIC_OPERATION_IDS[1],
        ),
    )
    authority = _FullConformanceAuthority()
    adapter = _adapter(tmp_path, runner)
    adapter.bind_full_conformance_authority(authority)
    adapter.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
    request = replace(
        _request(),
        runtime_binding=adapter.runtime_binding(),
        job_ref="reasoning-observation-order-job",
    )

    with pytest.raises(
        ReasoningSkillUnavailable,
        match="reasoning_primary_result_contract_invalid",
    ) as caught:
        adapter.generate_draft(request)

    assert caught.value.recovery_checkpoint is not None
    assert caught.value.recovery_checkpoint["contract_failure_detail_code"] == (
        "reasoning_semantic_mcp_observation_order_invalid"
    )

    assert len(runner.calls) == 1
    assert len(authority.revoked) == 1


def test_real_harness_authority_issues_only_current_reasoning_operations(
    tmp_path: Path,
) -> None:
    drafting = _DeterministicDraftingAdapter()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "real-reasoning-mcp"),
        proposal_drafter=drafting,
        intent_drafting_provider=drafting,
        host_compute_probe=_DeterministicProbe(),
        idea_skill_provider=_DeterministicIdeaSkill(no_viable=True),
        plan_skill_provider=_DeterministicPlanSkill(no_gap=False),
        harness_adapters=(
            _FullConformanceAdapter("codex"),
            _FullConformanceAdapter("claude"),
        ),
    )
    try:
        runtime.harnesses.start_full_conformance(_full_request())
        for _turn in range(4):
            assert runtime.harnesses.advance_full_conformance(
                mcp_base_url="http://127.0.0.1:8765"
            )
        quest = _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        question = runtime.owners.research_graph.query_question_by_ref(
            str(quest["question_ref"])
        )
        assert question is not None
        stage_request = (
            runtime.owners.advancement_engine.ensure_reasoning_stage_request(
                cycle_ref=str(quest["cycle_ref"]),
                accepted_question=question.as_binding(),
                idempotency_key="real-reasoning-stage-request",
            )
        )

        adapter = _adapter(
            tmp_path / "real-adapter",
            _SequenceRunner([]),
        )
        adapter.bind_full_conformance_authority(runtime.harnesses)
        binding = adapter.runtime_binding()
        run = runtime.owners.agent_runtime.admit_reasoning_stage(
            stage_request,
            "real-reasoning-run-admission",
            runtime_binding=binding,
        )

        channel = runtime.harnesses.issue_resident_mcp_channel(
            run_ref=run.run_ref,
            attempt_ref=run.attempt_ref,
            root_session_ref=run.root_session_ref,
            fence_ref=run.fence_ref,
            capability_binding_hash=run.runtime_binding_hash,
            operation_ids=REASONING_ROOT_SEMANTIC_OPERATION_IDS,
        )
        status, response = runtime.harnesses.dispatch_mcp(
            channel.connection.token,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {},
            },
        )
        assert status == 200
        assert response is not None
        assert [
            tool["name"] for tool in response["result"]["tools"]
        ] == list(REASONING_ROOT_SEMANTIC_OPERATION_IDS)

        with pytest.raises(
            HarnessAdmissionError,
            match="attempt_fence_invalid|reasoning_runtime_scope_invalid",
        ):
            runtime.harnesses.issue_resident_mcp_channel(
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                root_session_ref=run.root_session_ref,
                fence_ref="stale-reasoning-fence",
                capability_binding_hash=run.runtime_binding_hash,
                operation_ids=REASONING_ROOT_SEMANTIC_OPERATION_IDS,
            )

        runtime.harnesses.revoke_resident_mcp_channel(channel)
        status, response = runtime.harnesses.dispatch_mcp(
            channel.connection.token,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            },
        )
        assert status == 401
        assert response is not None
        assert response["error"]["code"] == (
            "mcp_channel_authentication_required"
        )
    finally:
        runtime.close()


def test_reasoning_resident_mcp_rejects_post_execution_submission_scope(
    tmp_path: Path,
) -> None:
    runtime = _reasoning_runtime(
        tmp_path / "reasoning-post-execution-mcp",
        reasoning_skill=_DeterministicReasoningSkill(),
    )
    try:
        quest = _confirm_deepfetch_quest(runtime)
        _finish_idea_stage(runtime)
        for _step in range(8):
            assert runtime.reasoning_stage.process_once()
            current = runtime.reasoning_stage.query_current()
            run_view = current["run"]
            if (
                run_view is not None
                and run_view["attempt_execution_receipt"] is not None
            ):
                break
        else:
            raise AssertionError("Reasoning did not reach durable execution")

        request = runtime.owners.advancement_engine.query_reasoning_stage_request(
            str(quest["cycle_ref"])
        )
        assert request is not None
        run = runtime.owners.agent_runtime.query_reasoning_stage_run(
            request.request_ref
        )
        assert run is not None
        with runtime._database.read() as connection:
            statuses = connection.exec_driver_sql(
                "SELECT runs.status AS run_status, "
                "attempts.status AS attempt_status, "
                "fences.status AS fence_status FROM ar_stage_runs runs "
                "JOIN ar_stage_attempts attempts ON attempts.attempt_ref = "
                "runs.current_attempt_ref JOIN ar_execution_fences fences ON "
                "fences.fence_ref = runs.current_fence_ref WHERE runs.run_ref = ?",
                (run.run_ref,),
            ).one()
        assert tuple(statuses) == (
            "awaiting_acceptance",
            "executed",
            "submitted",
        )

        with pytest.raises(
            HarnessAdmissionError,
            match="reasoning_runtime_scope_stale",
        ):
            runtime.harnesses.issue_resident_mcp_channel(
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                root_session_ref=run.root_session_ref,
                fence_ref=run.fence_ref,
                capability_binding_hash=run.runtime_binding_hash,
                operation_ids=REASONING_ROOT_SEMANTIC_OPERATION_IDS,
            )
    finally:
        runtime.close()
