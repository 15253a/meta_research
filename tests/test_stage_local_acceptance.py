from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text

from meta_research.owners.common import OwnerConflict
from test_public_idea_stage import _DeterministicIdeaSkill, _confirm_direct_quest, _runtime


def _prepared_local_rejection(runtime):
    _confirm_direct_quest(runtime)
    started = runtime.idea_stage.start("local-rejection-start")
    assert runtime.idea_stage.process_once()
    owner = runtime.owners.agent_runtime
    run = owner.query_idea_stage_run(started["stage_run_request"]["request_ref"])
    assert run.primary_draft is not None
    assert run.primary_invocation.status == "completed"
    command = dict(
        unit_ref=run.primary_invocation.invocation_ref, run_ref=run.run_ref,
        attempt_ref=run.attempt_ref, fence_ref=run.fence_ref,
        primary_draft_hash=run.primary_draft.draft_hash,
        native_session_ref=run.native_session_ref,
        candidate={"phase": "local", "result": {"final_outcome": run.primary_draft.draft}},
        reason_code="idea_local_result_contract_invalid",
        detail_code="codex_output_invalid",
        feedback=("Correct the candidate while retaining the same Root Session.",),
    )
    return owner, run, command


def _provider_rows(runtime):
    with runtime._database.read() as connection:
        return tuple(connection.execute(text(
            "SELECT invocation_ref, phase, status, response_hash FROM ar_stage_provider_invocations "
            "ORDER BY invocation_ref"
        )).all())


@pytest.mark.parametrize("field,value", [
    ("primary_draft_hash", "0" * 64),
    ("unit_ref", "provider-invocation:foreign"),
    ("native_session_ref", "native-session:foreign"),
    ("fence_ref", "fence:foreign"),
])
def test_local_rejection_requires_exact_sealed_primary_without_mutation(tmp_path, field, value):
    runtime = _runtime(tmp_path / "exact-primary", _DeterministicIdeaSkill())
    try:
        owner, run, command = _prepared_local_rejection(runtime)
        original = _provider_rows(runtime)
        command[field] = value
        with pytest.raises(OwnerConflict, match="stage_completion_rejection_stale"):
            owner.reject_stage_local_candidate(**command)
        assert _provider_rows(runtime) == original
        assert owner.query_stage_completion_rejection(run.attempt_ref) is None
        assert owner.query_idea_stage_run(run.request_ref).attempt_ref == run.attempt_ref
    finally:
        runtime.close()


def test_concurrent_local_rejection_preserves_primary_and_has_one_successor(tmp_path):
    provider = _DeterministicIdeaSkill()
    runtime = _runtime(tmp_path / "concurrent-local", provider)
    second = None
    try:
        owner, run, command = _prepared_local_rejection(runtime)
        original = _provider_rows(runtime)
        second = _runtime(tmp_path / "concurrent-local", _DeterministicIdeaSkill())
        def reject(active_owner):
            try:
                return active_owner.reject_stage_local_candidate(**command)
            except OwnerConflict as error:
                return error
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = tuple(executor.map(reject, (owner, second.owners.agent_runtime)))
        accepted = [value for value in outcomes if not isinstance(value, OwnerConflict)]
        refused = [value for value in outcomes if isinstance(value, OwnerConflict)]
        assert len(accepted) == len(refused) == 1
        assert refused[0].code == "stage_completion_rejection_stale"
        rejection = accepted[0]
        successor = owner.query_idea_stage_run(run.request_ref)
        assert successor.root_session_ref == run.root_session_ref
        assert successor.attempt_generation == 2
        assert successor.primary_invocation.status == "prepared"
        assert successor.primary_draft is None
        assert tuple(row for row in _provider_rows(runtime) if row[0] == run.primary_invocation.invocation_ref) == original
        assert {row[1] for row in _provider_rows(runtime)} == {"primary"}
        assert len(provider.requests) == 1
        assert owner.query_stage_completion_rejection(run.attempt_ref) == rejection
    finally:
        if second is not None:
            second.close()
        runtime.close()


def _local_stage_runtime(path, role, *, provider=None, initialize=True):
    from test_public_plan_stage import (
        _DeterministicIdeaSkill as PlanIdea, _DeterministicPlanSkill,
        _runtime as plan_runtime, _confirm_direct_quest as confirm_plan, _finish_idea_stage,
    )
    if role == "idea":
        provider = provider or _DeterministicIdeaSkill()
        runtime = _runtime(path, provider)
        if initialize:
            _confirm_direct_quest(runtime)
    elif role == "plan":
        provider = provider or _DeterministicPlanSkill(no_gap=False)
        runtime = plan_runtime(path, idea_skill=PlanIdea(), plan_skill=provider)
        if initialize:
            confirm_plan(runtime)
            _finish_idea_stage(runtime)
    elif role in {"bundle", "exhaustion"}:
        from test_public_bundle_stage import _bundle_runtime, _prepare_bundle_request, _DeterministicBundleSkill
        from test_bundle_exhaustion_owner import _ExhaustionBundleSkill
        provider = provider or (_ExhaustionBundleSkill() if role == "exhaustion" else _DeterministicBundleSkill())
        runtime = _bundle_runtime(path, bundle_skill_provider=provider, harness_ready=False)
        if initialize:
            _prepare_bundle_request(runtime)
    else:
        from test_public_reasoning_stage import _reasoning_runtime, _DeterministicReasoningSkill, _confirm_deepfetch_quest
        provider = provider or _DeterministicReasoningSkill()
        runtime = _reasoning_runtime(path, reasoning_skill=provider)
        if initialize:
            _confirm_deepfetch_quest(runtime)
            _finish_idea_stage(runtime)
    return runtime, provider


def _real_provider_state(runtime, run_ref):
    with runtime._database.read() as connection:
        return tuple(tuple(tuple(row) for row in connection.execute(text(query), {"run": run_ref}))
            for query in (
                "SELECT * FROM ar_stage_provider_invocations WHERE run_ref=:run ORDER BY invocation_ref",
                "SELECT * FROM ar_provider_units WHERE run_ref=:run ORDER BY unit_ref",
            ))


@pytest.mark.parametrize("role", ["idea", "plan", "bundle", "exhaustion", "reasoning"])
@pytest.mark.parametrize("action", ["resume", "cancel"])
def test_each_local_stage_restarts_without_provider_and_obeys_current_control(tmp_path, monkeypatch, role, action):
    from test_reasoning_operator_pause_resume import _control
    path = tmp_path / role
    runtime, provider = _local_stage_runtime(path, role)
    kind = "bundle" if role == "exhaustion" else role
    stage = getattr(runtime, kind + "_stage")
    try:
        for _ in range(8):
            view = stage.query_current()
            run = (None if view["stage_run_request"] is None else
                   getattr(runtime.owners.agent_runtime, "query_" + kind + "_stage_run")(view["stage_run_request"]["request_ref"]))
            if run is not None and run.primary_draft is not None:
                break
            assert stage.process_once()
        assert run is not None and run.primary_draft is not None
        before = _real_provider_state(runtime, run.run_ref)
        assert len(before[0]) == len(before[1]) == 1
        assert run.primary_invocation.status == "completed"
        if action == "resume":
            assert _control(runtime, run, "pause")["executed"] is True
        else:
            assert _control(runtime, run, "cancel")["executed"] is True
    finally:
        runtime.close()

    runtime, provider = _local_stage_runtime(path, role, provider=provider, initialize=False)
    stage = getattr(runtime, kind + "_stage")
    try:
        calls = []
        finalize = provider.finalize_draft
        def no_provider(*_args, **_kwargs):
            raise AssertionError("persisted primary must never be generated again")
        def local(request, draft):
            calls.append(request.job_ref)
            assert request.job_ref == run.primary_invocation.operation_ref
            assert _real_provider_state(runtime, run.run_ref) == before
            return finalize(request, draft)
        monkeypatch.setattr(provider, "generate_draft", no_provider)
        monkeypatch.setattr(provider, "finalize_draft", local)
        assert not stage.process_once()
        if action == "resume":
            assert _control(runtime, run, "resume")["executed"] is True
            assert stage.process_once()
            assert calls == [run.primary_invocation.operation_ref]
        else:
            assert calls == []
        assert _real_provider_state(runtime, run.run_ref) == before
    finally:
        runtime.close()


@pytest.mark.parametrize("role", ["idea", "plan", "bundle", "exhaustion", "reasoning"])
def test_concurrent_local_acceptance_replays_one_exact_execution(tmp_path, role):
    import threading
    path = tmp_path / "concurrent-acceptance"
    runtime, provider = _local_stage_runtime(path, role)
    second = None
    kind = "bundle" if role == "exhaustion" else role
    stage = getattr(runtime, kind + "_stage")
    try:
        for _ in range(8):
            view = stage.query_current()
            run = (None if view["stage_run_request"] is None else
                   getattr(runtime.owners.agent_runtime, "query_" + kind + "_stage_run")(view["stage_run_request"]["request_ref"]))
            if run is not None and run.primary_draft is not None:
                break
            assert stage.process_once()
        assert run is not None and run.primary_draft is not None
        before = _real_provider_state(runtime, run.run_ref)
        second, second_provider = _local_stage_runtime(path, role, initialize=False)
        barrier = threading.Barrier(2)
        for active in (provider, second_provider):
            finalize = active.finalize_draft
            def synchronized(request, draft, original=finalize):
                result = original(request, draft)
                barrier.wait(timeout=10)
                return result
            active.finalize_draft = synchronized
            def no_provider(*_args, **_kwargs):
                raise AssertionError("concurrent local resume must not generate primary")
            active.generate_draft = no_provider
        other_stage = getattr(second, kind + "_stage")
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(executor.map(lambda worker: worker.process_once(), (stage, other_stage)))
        assert results == (True, True)
        owner = runtime.owners.agent_runtime
        first = getattr(owner, "query_" + kind + "_stage_run")(run.request_ref)
        other = getattr(second.owners.agent_runtime, "query_" + kind + "_stage_run")(run.request_ref)
        assert first.attempt_ref == other.attempt_ref == run.attempt_ref
        assert _real_provider_state(runtime, run.run_ref) == before
        if role == "exhaustion":
            evidence = owner.query_bundle_exhaustion_evidence_for_run(run_ref=run.run_ref, attempt_ref=run.attempt_ref)
            replay = second.owners.agent_runtime.query_bundle_exhaustion_evidence(evidence.evidence_ref)
            assert evidence == replay
            with runtime._database.read() as connection:
                assert connection.execute(text(
                    "SELECT COUNT(*) FROM ar_bundle_exhaustion_evidence WHERE run_ref=:run"
                ), {"run": run.run_ref}).scalar_one() == 1
            with pytest.raises(OwnerConflict, match="bundle_exhaustion_evidence_conflict"):
                owner.reconcile_bundle_exhaustion_evidence(
                    evidence_identity=evidence.evidence.evidence_identity, expected_evidence_hash="0" * 64,
                )
        else:
            assert first.execution == other.execution
            assert first.execution is not None
            with runtime._database.read() as connection:
                assert connection.execute(text(
                    "SELECT COUNT(*) FROM ar_stage_attempts WHERE run_ref=:run AND execution_receipt_ref IS NOT NULL"
                ), {"run": run.run_ref}).scalar_one() == 1
                assert connection.execute(text(
                    "SELECT COUNT(*) FROM ar_stage_commands WHERE command_kind=:kind AND result_ref=:submission"
                ), {"kind": "record_"+kind+"_attempt_execution", "submission": first.execution.submission_ref}).scalar_one() == 1
                key = connection.execute(text(
                    "SELECT idempotency_key FROM ar_stage_commands WHERE command_kind=:kind AND result_ref=:submission"
                ), {"kind": "record_"+kind+"_attempt_execution", "submission": first.execution.submission_ref}).scalar_one()
            from dataclasses import replace
            with pytest.raises(OwnerConflict, match="idempotency_conflict"):
                getattr(owner, "record_" + kind + "_attempt_execution")(
                    run_ref=run.run_ref, attempt_ref=run.attempt_ref, fence_ref=run.fence_ref,
                    submission_ref=first.execution.submission_ref,
                    native_session_ref=first.execution.native_session_ref,
                    runtime_binding=replace(first.execution.runtime_binding, model_ref="foreign-model-v1"),
                    **{{"plan": "plan", "bundle": "target_plan"}.get(kind, "outcome"): first.execution.outcome},
                    reviewed_draft=first.execution.reviewed_draft,
                    review=first.execution.review, idempotency_key=key,
                )
            assert getattr(owner, "query_" + kind + "_stage_run")(run.request_ref).execution == first.execution
    finally:
        if second is not None:
            second.close()
        runtime.close()
