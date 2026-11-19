import pytest
from sqlalchemy import text

from meta_research.owners.common import OwnerConflict

from test_public_plan_stage import (
    _DeterministicIdeaSkill, _DeterministicPlanSkill, _confirm_direct_quest,
    _finish_idea_stage, _runtime,
)


def test_only_contiguous_uncheckpointed_plan_attempts_can_supply_their_primary(tmp_path):
    def open_runtime():
        return _runtime(tmp_path, idea_skill=_DeterministicIdeaSkill(),
                        plan_skill=_DeterministicPlanSkill(no_gap=False))

    runtime = open_runtime()
    try:
        _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        assert runtime.plan_stage.process_once()  # AE request
        assert runtime.plan_stage.process_once()  # AR admission
        request = runtime.plan_stage.query_current()["stage_run_request"]["request_ref"]
        retired = []
        for _ in range(2):
            owner = runtime.owners.agent_runtime
            run = owner.query_plan_stage_run(request)
            assert run.replayable_primary_attempts == tuple(reversed(retired))
            retired.append((run.attempt_ref, run.fence_ref, run.attempt_generation))
            owner.begin_provider_unit(unit_ref=run.primary_invocation.invocation_ref,
                operation_ref=run.primary_invocation.operation_ref, run_ref=run.run_ref,
                attempt_ref=run.attempt_ref, fence_ref=run.fence_ref, unit_kind="plan_primary")
            runtime.close()
            runtime = open_runtime()
        owner = runtime.owners.agent_runtime
        run = owner.query_plan_stage_run(request)
        assert run.replayable_primary_attempts == tuple(reversed(retired))
        for old_attempt, old_fence, _generation in retired:
            with pytest.raises(OwnerConflict, match="^runtime_provider_unit_stale$"):
                owner.begin_provider_unit(unit_ref="stale-replay-writer", operation_ref=run.primary_invocation.operation_ref,
                    run_ref=run.run_ref, attempt_ref=old_attempt, fence_ref=old_fence, unit_kind="plan_primary")

        # An older operation cannot be imported through a newer eligible edge.
        with runtime._database.write() as connection:
            connection.execute(text("UPDATE ar_stage_provider_invocations SET operation_ref='other-operation' "
                                    "WHERE attempt_ref=:ref AND phase='primary'"), {"ref": retired[0][0]})
        assert owner.query_plan_stage_run(request).replayable_primary_attempts == (retired[1],)
        # Owner rejection starts a different decision, even if the reference is reused.
        with runtime._database.write() as connection:
            connection.execute(text("UPDATE ar_stage_attempt_replacements SET reason_code='provider_result_correction' "
                                    "WHERE replacement_attempt_ref=:ref"), {"ref": run.attempt_ref})
        assert owner.query_plan_stage_run(request).replayable_primary_attempts == ()
    finally:
        runtime.close()
