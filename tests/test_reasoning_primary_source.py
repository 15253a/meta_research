from dataclasses import replace
import pytest
from sqlalchemy import text
from meta_research.composition import build_production_runtime
from meta_research.paths import prepare_data_root
from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.reasoning_skill import ReasoningSkillUnavailable
from meta_research.semantic_owner_gateway import ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS
from test_public_quest_completion import (
    _CandidateCompletionReasoningSkill, _DeterministicDraftingAdapter,
    _DeterministicProbe, _confirm_direct_quest, _finish_idea_stage,
)
from test_quest_goal_completion_race import _AdvisoryIdeaSkill


class CompletionAfterHR(_CandidateCompletionReasoningSkill):
    def __init__(self, wait):
        self.wait = wait
        self.requests = []
        self.opened = None

    def generate_draft(self, request):
        self.requests.append(request)
        draft = super().generate_draft(request)
        if self.wait and self.opened is None:
            owner = self.runtime.owners.agent_runtime
            run = owner.query_reasoning_stage_run(request.stage_request_ref)
            binding = {
                "quest_ref": request.quest_ref, "task_ref": request.run_ref,
                "root_session_ref": request.root_session_ref,
                "operation_id": ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[0],
                "attempt_ref": request.attempt_ref, "generation": run.attempt_generation,
                "request_owner": "agent_runtime", "root_kind": "reasoning",
                "phase": "primary", "fence_ref": request.fence_ref,
                "runtime_binding_hash": canonical_hash(request.runtime_binding.as_dict()),
            }
            target = {
                "schema_ref": "meta-research/root-agent-human-request-target/v1",
                "root": {
                    "run_kind": "reasoning", "run_ref": request.run_ref,
                    "attempt_ref": request.attempt_ref,
                    "root_session_ref": request.root_session_ref,
                    "fence_ref": request.fence_ref,
                    "waiter_generation": run.attempt_generation,
                },
                "condition": {"operator_choice": "continue_without_optional_input"},
            }
            self.opened = owner.open_human_request_effect(
                effect_key="mcp-effect:review-hr", effect_id="review-hr",
                operation_binding=binding, predecessor_request_ref=None,
                request_kind="offline_action", obligation="Choose whether to continue.",
                business_purpose="Continue this exact research task after the operator reply.",
                target_assertion=target,
                acceptance_conditions=("The operator supplies a disposition.",),
                direct_waiter={"waiter_ref": f"root_run:{request.run_ref}",
                    "generation": run.attempt_generation, "target_assertion": target,
                    "wait_scope": "local", "other_blockers": []},
                quest_ref=request.quest_ref,
            )
            raise ReasoningSkillUnavailable("codex_operation_waiting_for_user", native_session_ref=draft.primary_session_ref)
        return draft

    def finalize_draft(self, request, draft):
        return replace(super().finalize_draft(request, draft),
            review_mode="advisory_unobserved", reviewer_agent_ref=None)

@pytest.mark.parametrize("wait", [False, True])
def test_reasoning_local_completion_after_real_hr(tmp_path, wait):
    provider = CompletionAfterHR(wait)
    drafting = _DeterministicDraftingAdapter()
    runtime = build_production_runtime(prepare_data_root(tmp_path / "runtime"),
        proposal_drafter=drafting, intent_drafting_provider=drafting,
        host_compute_probe=_DeterministicProbe(), idea_skill_provider=_AdvisoryIdeaSkill(),
        reasoning_skill_provider=provider)
    provider.runtime = runtime
    resumed = False
    try:
        _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        for step in range(15):
            view = runtime.reasoning_stage.query_current()
            if view["reasoning_acceptance"]["status"] == "accepted":
                return
            if provider.opened and not resumed:
                runtime.owners.human_collaboration.respond_to_human_request(
                    str(provider.opened["request_ref"]), decision="deferred", facts={},
                    note="Continue without optional input.", idempotency_key="review-reply")
                resumed = True
            assert runtime.reasoning_stage.process_once(), runtime.reasoning_stage.transient_error
        raise AssertionError("Reasoning did not accept completion")
    finally:
        runtime.close()


def _runtime(root, provider):
    drafting = _DeterministicDraftingAdapter()
    runtime = build_production_runtime(prepare_data_root(root),
        proposal_drafter=drafting, intent_drafting_provider=drafting,
        host_compute_probe=_DeterministicProbe(), idea_skill_provider=_AdvisoryIdeaSkill(),
        reasoning_skill_provider=provider)
    provider.runtime = runtime
    return runtime


@pytest.fixture
def saved_primary(tmp_path):
    from meta_research.runtime_conditions import read_runtime_conditions, save_runtime_conditions
    root = tmp_path / "saved-source"
    provider = CompletionAfterHR(True)
    runtime = _runtime(root, provider)
    quest = _confirm_direct_quest(runtime)
    _finish_idea_stage(runtime)
    conditions = read_runtime_conditions(root, quest["quest_ref"])
    save_runtime_conditions(root, quest["quest_ref"], text="Before the operator reply.",
        expected_revision=conditions["revision"])
    resumed = False
    for _ in range(12):
        view = runtime.reasoning_stage.query_current()
        ref = None if view["stage_run_request"] is None else view["stage_run_request"]["request_ref"]
        run = runtime.owners.agent_runtime.query_reasoning_stage_run(ref) if ref else None
        if run is not None and run.primary_draft is not None:
            break
        if provider.opened and not resumed:
            conditions = read_runtime_conditions(root, quest["quest_ref"])
            save_runtime_conditions(root, quest["quest_ref"], text="After the operator reply.",
                expected_revision=conditions["revision"])
            runtime.owners.human_collaboration.respond_to_human_request(
                str(provider.opened["request_ref"]), decision="deferred", facts={},
                note="Continue without optional input.", idempotency_key="source-reply")
            resumed = True
        assert runtime.reasoning_stage.process_once()
    assert run.primary_draft is not None
    assert len(provider.requests) == 2
    try:
        yield root, runtime, provider, run
    finally:
        runtime.close()


def _scope(run):
    from meta_research.human_guidance import GuidanceRuntimeScope
    return GuidanceRuntimeScope("reasoning", run.run_ref, run.attempt_ref,
        run.root_session_ref, run.fence_ref, run.runtime_binding_hash)


def test_saved_source_survives_restart_without_provider(saved_primary, monkeypatch):
    import json
    root, runtime, provider, run = saved_primary
    owner = runtime.owners.agent_runtime
    source = owner.reasoning_primary_completion_operation(_scope(run))[0]
    assert source.job_ref == provider.requests[1].job_ref
    assert source.job_ref != provider.requests[0].job_ref
    cut = runtime.owners.human_collaboration.recover_operation_guidance(source.identity)
    assert cut.direction_cut["conditions"]["runtime_conditions"]["text"] == "After the operator reply."
    runtime.close()
    restarted = _runtime(root, provider)
    assert restarted.owners.agent_runtime.reasoning_primary_completion_operation(_scope(run))[0] == source
    restarted.close()
    restarted = _runtime(root, provider)
    finished = []
    monkeypatch.setattr(provider, "finish_job", finished.append, raising=False)
    monkeypatch.setattr(restarted.owners.agent_runtime, "root_provider_continuation_job_ref",
        lambda **_: "later-unrelated-continuation")
    try:
        assert restarted.owners.agent_runtime.reasoning_primary_completion_operation(_scope(run))[0] == source
        assert restarted.reasoning_stage.process_once()
        with restarted.owners.agent_runtime._database.read() as connection:
            binding = json.loads(connection.execute(text(
                "SELECT binding_json FROM hc_reasoning_completion_conditions"
            )).scalar_one())
        assert binding["conditions"]["runtime_conditions"]["text"] == "After the operator reply."
        for _ in range(8):
            if restarted.reasoning_stage.query_current()["reasoning_acceptance"]["status"] == "accepted":
                break
            assert restarted.reasoning_stage.process_once()
        assert restarted.reasoning_stage.query_current()["reasoning_acceptance"]["status"] == "accepted"
        assert len(provider.requests) == 2
        assert finished == [source.job_ref]
    finally:
        restarted.close()


@pytest.mark.parametrize("change", ["job", "unit", "fence"])
def test_saved_source_rejects_forged_completion(saved_primary, change):
    _, runtime, _, run = saved_primary
    owner = runtime.owners.agent_runtime
    source = owner.reasoning_primary_completion_operation(_scope(run))[0]
    forged = (replace(source, scope=replace(source.scope, fence_ref="fake-fence"))
        if change == "fence" else replace(source, **{change + "_ref": "fake-source"}))
    with pytest.raises(OwnerConflict):
        owner.verify_reasoning_primary_completion_operation(forged)
    assert owner.query_reasoning_stage_run(run.request_ref).primary_draft == run.primary_draft


def test_source_reference_is_part_of_checkpoint_integrity(saved_primary):
    _, runtime, _, run = saved_primary
    owner = runtime.owners.agent_runtime
    with owner._database.write() as connection:
        connection.execute(text("UPDATE ar_stage_attempts SET primary_provider_unit_ref=:ref WHERE attempt_ref=:attempt"),
            {"ref": run.primary_invocation.invocation_ref, "attempt": run.attempt_ref})
    with pytest.raises(OwnerConflict, match="idea_provider_invocation_invalid"):
        owner.query_reasoning_stage_run(run.request_ref)


def test_legacy_missing_source_remains_readable_and_refuses_completion(saved_primary):
    from meta_research.owners.agent_runtime import _primary_provider_response_hash
    _, runtime, provider, run = saved_primary
    owner = runtime.owners.agent_runtime
    draft = run.primary_draft
    old_hash = _primary_provider_response_hash(native_session_ref=draft.native_session_ref,
        draft=draft.draft, adapter_kind=draft.adapter_kind, stage="reasoning")
    with owner._database.write() as connection:
        connection.execute(text("UPDATE ar_stage_attempts SET primary_provider_unit_ref=NULL WHERE attempt_ref=:attempt"),
            {"attempt": run.attempt_ref})
        connection.execute(text("UPDATE ar_stage_provider_invocations SET response_hash=:hash WHERE attempt_ref=:attempt"),
            {"hash": old_hash, "attempt": run.attempt_ref})
    assert owner.query_reasoning_stage_run(run.request_ref).primary_draft.draft == draft.draft
    for _ in range(2):
        with pytest.raises(OwnerConflict, match="reasoning_primary_source_missing"):
            runtime.reasoning_stage.process_once()
    assert len(provider.requests) == 2


def test_adopted_checkpoint_keeps_source_and_rejects_revoked_scope(saved_primary):
    from test_reasoning_operator_pause_resume import _control
    _, runtime, provider, run = saved_primary
    owner = runtime.owners.agent_runtime
    source = owner.reasoning_primary_completion_operation(_scope(run))[0]
    assert _control(runtime, run, "pause")["executed"] is True
    with pytest.raises(OwnerConflict):
        owner.verify_reasoning_primary_completion_operation(source)
    assert _control(runtime, run, "resume")["executed"] is True
    import time
    now = time.time()
    with owner._database.write() as connection:
        control = connection.execute(text("SELECT * FROM ar_run_controls WHERE run_ref=:run"),
            {"run": run.run_ref}).one()
        connection.execute(text("UPDATE ar_execution_fences SET status='rejected', closed_at=:now WHERE fence_ref=:fence"),
            {"now": now, "fence": run.fence_ref})
        replacement = owner._replace_fenced_managed_attempt(
            connection, control, now, reuse_checkpoint=True,
            reuse_operation_refs=False, preserve_native_session=True,
            replacement_reason_code="provider_result_correction")
        connection.execute(text("UPDATE ar_run_controls SET attempt_ref=:attempt, fence_ref=:fence, "
            "status='running', control_revision=control_revision+1 WHERE run_ref=:run"),
            {"attempt": replacement.attempt_ref, "fence": replacement.fence_ref, "run": run.run_ref})
    adopted = owner.query_reasoning_stage_run(run.request_ref)
    assert adopted.attempt_ref != run.attempt_ref
    recovered = owner.reasoning_primary_completion_operation(_scope(adopted))[0]
    assert recovered.job_ref == source.job_ref
    assert recovered.unit_ref == source.unit_ref
    with pytest.raises(OwnerConflict):
        owner.verify_reasoning_primary_completion_operation(source)
    for _ in range(8):
        if runtime.reasoning_stage.query_current()["reasoning_acceptance"]["status"] == "accepted":
            break
        assert runtime.reasoning_stage.process_once()
    assert runtime.reasoning_stage.query_current()["reasoning_acceptance"]["status"] == "accepted"
    assert len(provider.requests) == 2


def test_primary_write_refuses_missing_and_unrelated_unit(tmp_path, monkeypatch):
    provider = CompletionAfterHR(False)
    runtime = _runtime(tmp_path / "invalid-source", provider)
    owner = runtime.owners.agent_runtime
    record = owner.record_reasoning_primary_draft
    checked = []

    def record_with_invalid_sources(**values):
        for source in (None, "unrelated-physical-unit"):
            with pytest.raises(OwnerConflict, match="reasoning_primary_source_"):
                record(**{**values, "primary_provider_unit_ref": source,
                    "idempotency_key": "invalid-source:" + str(source)})
            checked.append(source)
        return record(**values)

    monkeypatch.setattr(owner, "record_reasoning_primary_draft", record_with_invalid_sources)
    try:
        _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        for _ in range(12):
            if runtime.reasoning_stage.query_current()["reasoning_acceptance"]["status"] == "accepted":
                break
            assert runtime.reasoning_stage.process_once()
        assert runtime.reasoning_stage.query_current()["reasoning_acceptance"]["status"] == "accepted"
        assert checked == [None, "unrelated-physical-unit"]
        assert len(provider.requests) == 1
    finally:
        runtime.close()
