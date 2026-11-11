"""Review-only regression: real Owner HR resume followed by local completion."""
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

    def review_draft(self, request, draft):
        # The baseline calls this same local wrapping step by its old name.
        return replace(super().review_draft(request, draft),
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
            try:
                progressed = runtime.reasoning_stage.process_once()
            except OwnerConflict as failure:
                ref = runtime.reasoning_stage.query_current()["stage_run_request"]["request_ref"]
                run = runtime.owners.agent_runtime.query_reasoning_stage_run(ref)
                print({"caught": failure.code, "primary_operation_ref": run.primary_invocation.operation_ref,
                    "primary_status": run.primary_invocation.status,
                    "has_persisted_primary": run.primary_draft is not None,
                    "has_execution": run.execution is not None,
                    "attempt_ref": run.attempt_ref, "jobs": [x.job_ref for x in provider.requests]}, flush=True)
                for retry in range(2):
                    try:
                        runtime.reasoning_stage.process_once()
                    except OwnerConflict as repeated:
                        print({"retry": retry, "same_error": repeated.code,
                            "generate_count": len(provider.requests)}, flush=True)
                raise
            error = runtime.reasoning_stage.transient_error
            print({"step": step, "progressed": progressed, "error": error,
                "jobs": [x.job_ref for x in provider.requests]}, flush=True)
            if error:
                ref = runtime.reasoning_stage.query_current()["stage_run_request"]["request_ref"]
                run = runtime.owners.agent_runtime.query_reasoning_stage_run(ref)
                print({"primary_operation_ref": run.primary_invocation.operation_ref,
                    "primary_status": run.primary_invocation.status,
                    "has_persisted_primary": run.primary_draft is not None,
                    "has_execution": run.execution is not None,
                    "attempt_ref": run.attempt_ref}, flush=True)
            assert progressed, error
        raise AssertionError("Reasoning did not accept completion")
    finally:
        runtime.close()
