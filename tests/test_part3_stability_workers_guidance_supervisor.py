from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from contextlib import contextmanager, suppress
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import text

import meta_research.web as web_mod
from meta_research.composition import build_production_runtime
from meta_research.deepfetch import (
    CodexDeepFetchAdapter,
    DEEPFETCH_PROVIDER_STREAM_MAX_BYTES,
    DeepFetchRuntimeBinding,
    DeepFetchUnavailable,
    PROVIDER_RESULT_MAX_BYTES,
    _deepfetch_output_schema,
)
from meta_research.owners.research_memory import AssetIntakeRequest
from meta_research.quest_goal import (
    EvolutionDecision,
    ResearchRoot,
    WorkDecision,
    WorkHandle,
)
from meta_research.human_guidance import (
    GuidanceRuntimeScope,
    StageGuidanceOperation,
)
from meta_research.native_codex_bridge import NativeCodexBridge
from meta_research.native_guidance import (
    _GuidancePump,
    native_human_input_history,
)
from meta_research.native_session_cleanup import (
    _LAUNCH_SCRATCH,
    _paths_overlap,
    _resolve_protected_paths,
)
from meta_research.owners.agent_runtime import (
    BundleRuntimeBinding,
    IdeaRuntimeBinding,
    PlanRuntimeBinding,
)
from meta_research.owners.common import (
    OwnerConflict,
    canonical_hash,
    canonical_json,
)
from meta_research.owners.live_guidance import prepare_live_guidance
from meta_research.paths import prepare_data_root
from meta_research.provider_call_observations import observe_provider_files
from meta_research.reasoning_skill import ReasoningAutonomousCheckpointResult
from meta_research.provider_supervisor import (
    SUPERVISOR_EXIT_SCHEMA,
    SUPERVISOR_EXIT_SCHEMA_V2,
    SUPERVISOR_REQUEST_SCHEMA,
    SUPERVISOR_REQUEST_SCHEMA_V2,
    ensure_transport_key,
    read_transport_envelope,
    supervise,
    supervisor_host_identity,
    write_exit_receipt,
    write_supervisor_request,
    write_transport_envelope,
)
from meta_research.quest_drafting import _remove_durable_job
from meta_research.root_capabilities import root_capability_profile
from meta_research.root_session_usage import (
    RootSessionUsageError,
    _root_index_directory,
    read_root_session_observations,
    write_root_session_observation_index,
)
from meta_research.runtime_binding_compatibility import bundle_bindings_compatible
from meta_research.target_workspace_cleanup import _protected_originals

from test_deepfetch_adapter import (
    PROTOTYPE_ACQUIRE,
    PROTOTYPE_EMPTY_FINAL,
    PROTOTYPE_FINAL,
    RecordingAcquisitionClient,
    SequencedPrototypeRunner,
    _bind_acquisition,
    _request as _base_deepfetch_request,
)
from test_guidance_scope_alignment import _confirm_guidance
from test_harness_full_conformance import (
    _FullConformanceAdapter,
    _full_request,
)
from test_public_autonomous_creation import (
    _AutonomousReasoningSkill,
    _ReadyAutonomousAcquisitionProvider,
)
from test_public_bundle_stage import (
    _AcceptingTargetCandidateProofVerifier,
    _DeterministicBundleSkill,
)
from test_public_idea_stage import _DeterministicIdeaSkill
from test_public_plan_stage import (
    _DeterministicIdeaSkill as _DeterministicPlanIdeaSkill,
    _DeterministicPlanSkill,
)
from test_public_quest_completion import (
    _CandidateCompletionReasoningSkill,
    _DeterministicDraftingAdapter,
    _DeterministicProbe,
    _NoViableIdeaSkill,
)


def _confirm_direct_quest_with_prefix(runtime, prefix: str) -> dict[str, object]:
    human = runtime.owners.human_collaboration
    opened = human.create_quest({}, f"{prefix}-open")
    initialization_id = str(opened["initialization_id"])
    probed = human.observe_host_compute(
        initialization_id,
        ["GPU-completion-test"],
        f"{prefix}-probe",
    )
    draft = dict(probed["quest_draft"]["value"])
    draft.update(
        {
            "goal": f"Verify stability isolation for {prefix}.",
            "completion_criteria": "Produce bounded conclusion or completion candidate.",
            "time_budget": "30d",
            "route": "direct",
            "literature": {
                "mode": "oa_only",
                "library_entry_url": "",
                "scope_exclusions": "",
                "accepted_material_bindings": [],
            },
            "background_and_initial_direction": f"Baseline {prefix}.",
        }
    )
    revised = human.revise_quest_draft(
        initialization_id,
        draft,
        str(probed["quest_draft"]["hash"]),
        f"{prefix}-draft",
        int(probed["quest_draft"]["revision"]),
    )
    human.generate_question_proposal(
        initialization_id,
        str(revised["quest_draft"]["hash"]),
        f"{prefix}-proposal",
        int(revised["quest_draft"]["revision"]),
    )
    assert human.process_drafting_once()
    proposed = human.query_quest_creation(initialization_id)
    previewed = human.preview_confirmation(
        initialization_id,
        quest_draft_revision=int(proposed["quest_draft"]["revision"]),
        quest_draft_hash=str(proposed["quest_draft"]["hash"]),
        proposal_ref=str(proposed["proposal"]["ref"]),
        proposal_hash=str(proposed["proposal"]["hash"]),
        idempotency_key=f"{prefix}-preview",
    )
    human.confirm_quest(
        initialization_id,
        quest_draft_revision=int(proposed["quest_draft"]["revision"]),
        quest_draft_hash=str(proposed["quest_draft"]["hash"]),
        proposal_ref=str(proposed["proposal"]["ref"]),
        proposal_hash=str(proposed["proposal"]["hash"]),
        preview_ref=str(previewed["confirmation_preview"]["ref"]),
        preview_hash=str(previewed["confirmation_preview"]["hash"]),
        idempotency_key=f"{prefix}-confirm",
    )
    for _ in range(16):
        completed = human.query_quest_creation(initialization_id)
        if completed["status"] == "completed":
            return completed
        assert human.reconcile_once()
    raise AssertionError(f"Quest initialization {prefix} did not complete")


def _unique_draft(draft, request):
    if request.native_session_ref:
        return draft
    return replace(draft, primary_session_ref=f"{draft.primary_session_ref}:{request.run_ref}")


class _UniqueIdeaSkill(_DeterministicIdeaSkill):
    def generate_draft(self, request):
        return _unique_draft(super().generate_draft(request), request)


class _UniquePlanIdeaSkill(_DeterministicPlanIdeaSkill):
    def generate_draft(self, request):
        return _unique_draft(super().generate_draft(request), request)


class _UniquePlanSkill(_DeterministicPlanSkill):
    def generate_draft(self, request):
        return _unique_draft(super().generate_draft(request), request)


class _UniqueBundleSkill(_DeterministicBundleSkill):
    def generate_draft(self, request):
        return _unique_draft(super().generate_draft(request), request)


class _ValidNoViableIdeaSkill(_NoViableIdeaSkill):
    def generate_draft(self, request):
        return _unique_draft(super().generate_draft(request), request)

    def finalize_draft(self, request, draft):
        return replace(
            super().finalize_draft(request, draft),
            review_mode="advisory_unobserved",
            reviewer_agent_ref=None,
        )


class _ValidCompletionReasoningSkill(_CandidateCompletionReasoningSkill):
    def generate_draft(self, request):
        return _unique_draft(super().generate_draft(request), request)

    def finalize_draft(self, request, draft):
        return replace(
            super().finalize_draft(request, draft),
            review_mode="advisory_unobserved",
            reviewer_agent_ref=None,
        )


class _UniqueAutonomousReasoningSkill(_AutonomousReasoningSkill):
    def generate_draft(self, request):
        self.source_question_ref = None
        return _unique_draft(super().generate_draft(request), request)

    def finalize_draft(self, request, draft):
        assert draft.draft in self.checkpoints
        return ReasoningAutonomousCheckpointResult(
            primary_draft=draft.draft,
            reviewed_checkpoint=draft.draft,
            primary_session_ref=draft.primary_session_ref,
            review_mode="advisory_unobserved",
            reviewer_agent_ref=None,
            adapter_kind=draft.adapter_kind,
        )


def _stage_runtime(
    tmp_path: Path,
    name: str,
    *,
    idea_skill=None,
    plan_skill=None,
    reasoning_skill=None,
    acquisition=None,
):
    drafting = _DeterministicDraftingAdapter()
    return build_production_runtime(
        prepare_data_root(tmp_path / name),
        proposal_drafter=drafting,
        intent_drafting_provider=drafting,
        host_compute_probe=_DeterministicProbe(),
        idea_skill_provider=idea_skill or _ValidNoViableIdeaSkill(),
        plan_skill_provider=plan_skill,
        reasoning_skill_provider=reasoning_skill or _ValidCompletionReasoningSkill(),
        acquisition_provider=acquisition,
    )


def _active_foreground_by_quest(runtime, quest_ref: str) -> dict[str, object]:
    for row in runtime.owners.advancement_engine.query_active_foregrounds():
        if row["quest_ref"] == quest_ref:
            return row
    raise AssertionError(f"Missing active foreground for {quest_ref}")


def _advance_quest_to_stage(runtime, quest_ref: str, target_stage: str) -> None:
    stages = {
        "idea": runtime.idea_stage,
        "plan": runtime.plan_stage,
        "bundle": runtime.bundle_stage,
        "reasoning": runtime.reasoning_stage,
    }
    fg = None
    worker = None
    for _ in range(50):
        fg = runtime.owners.advancement_engine.query_foreground(quest_ref)
        if fg is not None and fg["stage"] == target_stage:
            return
        if fg is None:
            return
        worker = stages.get(str(fg["stage"]))
        assert worker is not None
        worker.process_once()
    raise AssertionError(
        f"Quest {quest_ref} did not reach stage {target_stage}: fg={fg}, transient={getattr(worker, 'transient_error', None)}"
    )


def _drive_reasoning_commit(runtime, cycle_ref: str):
    req = None
    for _ in range(24):
        req = runtime.owners.advancement_engine.query_reasoning_stage_request(cycle_ref)
        commit = (
            None
            if req is None
            else runtime.owners.advancement_engine.query_reasoning_stage_commit(req.request_ref)
        )
        if commit is not None:
            return req
        runtime.reasoning_stage.process_once()
    return req


def test_stage_workers_isolate_owner_conflict_and_rotate_cursor(tmp_path: Path, caplog) -> None:
    caplog.set_level(logging.WARNING)
    rt_idea = _stage_runtime(tmp_path, "idea", idea_skill=_UniqueIdeaSkill())
    try:
        qa = str(_confirm_direct_quest_with_prefix(rt_idea, "idea-a")["quest_ref"])
        qb = str(_confirm_direct_quest_with_prefix(rt_idea, "idea-b")["quest_ref"])
        fga = _active_foreground_by_quest(rt_idea, qa)
        fgb = _active_foreground_by_quest(rt_idea, qb)

        orig_query_q = rt_idea.owners.research_graph.query_question_by_ref

        def flaky_discovery(qref: str):
            if qref == fga["question_ref"]:
                return SimpleNamespace(quest_ref="other-quest", question_ref=qref)
            return orig_query_q(qref)

        rt_idea.owners.research_graph.query_question_by_ref = flaky_discovery
        try:
            caplog.clear()
            assert rt_idea.idea_stage.process_once() is True
            assert any(
                qa in rec.message and "idea_cycle_index_invalid" in rec.message
                for rec in caplog.records
            )
            fg_b_now = rt_idea.owners.advancement_engine.query_foreground(qb)
            assert fg_b_now["stage"] != "idea" or (
                rt_idea.owners.advancement_engine.query_idea_stage_request(str(fgb["cycle_ref"])) is not None
            )
        finally:
            rt_idea.owners.research_graph.query_question_by_ref = orig_query_q

        orig_query_fg = rt_idea.owners.advancement_engine.query_foreground

        def poisoned_fg(qref: str):
            if qref == qa:
                raise OwnerConflict("idea_poisoned", details={"quest_ref": qa})
            return orig_query_fg(qref)

        rt_idea.idea_stage._provider_cursor_cycle_ref = str(fgb["cycle_ref"])
        rt_idea.owners.advancement_engine.query_foreground = poisoned_fg
        try:
            caplog.clear()
            assert rt_idea.idea_stage.process_once() is True
            assert any(
                qa in rec.message and "idea_poisoned" in rec.message
                for rec in caplog.records
            )
            assert rt_idea.idea_stage.transient_error == "idea_poisoned"
            assert rt_idea.idea_stage._provider_cursor_cycle_ref != str(fga["cycle_ref"])
        finally:
            rt_idea.owners.advancement_engine.query_foreground = orig_query_fg
    finally:
        rt_idea.close()

    draft_calls = 0

    class _CountingPostFailureIdeaSkill(_UniqueIdeaSkill):
        def generate_draft(self, request):
            nonlocal draft_calls
            draft_calls += 1
            return super().generate_draft(request)

    rt_post = _stage_runtime(tmp_path, "idea-post-provider", idea_skill=_CountingPostFailureIdeaSkill())
    try:
        qa = str(_confirm_direct_quest_with_prefix(rt_post, "post-a")["quest_ref"])
        qb = str(_confirm_direct_quest_with_prefix(rt_post, "post-b")["quest_ref"])
        fga = _active_foreground_by_quest(rt_post, qa)
        fgb = _active_foreground_by_quest(rt_post, qb)
        for _ in range(10):
            req_a = rt_post.owners.advancement_engine.query_idea_stage_request(str(fga["cycle_ref"]))
            req_b = rt_post.owners.advancement_engine.query_idea_stage_request(str(fgb["cycle_ref"]))
            run_a = None if req_a is None else rt_post.owners.agent_runtime.query_idea_stage_run(req_a.request_ref)
            run_b = None if req_b is None else rt_post.owners.agent_runtime.query_idea_stage_run(req_b.request_ref)
            if run_a is not None and run_b is not None:
                break
            rt_post.idea_stage.process_once()
        assert run_a is not None and run_b is not None

        orig_record = rt_post.owners.agent_runtime.record_idea_primary_draft

        def post_provider_boom(**kwargs):
            if kwargs.get("run_ref") == run_a.run_ref:
                raise OwnerConflict("post_provider_boom", details={"quest_ref": qa})
            return orig_record(**kwargs)

        rt_post.idea_stage._provider_cursor_cycle_ref = str(fgb["cycle_ref"])
        rt_post.owners.agent_runtime.record_idea_primary_draft = post_provider_boom
        draft_calls = 0
        try:
            caplog.clear()
            assert rt_post.idea_stage.process_once() is False
            assert draft_calls == 1
            assert any(
                qa in rec.message and "post_provider_boom" in rec.message
                for rec in caplog.records
            )
            assert rt_post.idea_stage.transient_error == "post_provider_boom"
            assert rt_post.idea_stage.process_once() is True
            assert draft_calls == 2
            updated_b = rt_post.owners.agent_runtime.query_idea_stage_run(req_b.request_ref)
            assert updated_b is not None and updated_b.primary_draft is not None
        finally:
            rt_post.owners.agent_runtime.record_idea_primary_draft = orig_record
    finally:
        rt_post.close()

    rt_stages = _stage_runtime(
        tmp_path,
        "plan",
        idea_skill=_UniquePlanIdeaSkill(),
        plan_skill=_UniquePlanSkill(no_gap=False),
    )
    try:
        qa = str(_confirm_direct_quest_with_prefix(rt_stages, "pr-a")["quest_ref"])
        qb = str(_confirm_direct_quest_with_prefix(rt_stages, "pr-b")["quest_ref"])
        _advance_quest_to_stage(rt_stages, qa, "plan")
        _advance_quest_to_stage(rt_stages, qb, "plan")
        fga = _active_foreground_by_quest(rt_stages, qa)
        fgb = _active_foreground_by_quest(rt_stages, qb)

        orig_fg = rt_stages.owners.advancement_engine.query_foreground

        def poisoned_plan(qref: str):
            if qref == qa:
                raise OwnerConflict("plan_poisoned", details={"quest_ref": qa})
            return orig_fg(qref)

        rt_stages.plan_stage._provider_cursor_cycle_ref = str(fgb["cycle_ref"])
        rt_stages.owners.advancement_engine.query_foreground = poisoned_plan
        try:
            caplog.clear()
            assert rt_stages.plan_stage.process_once() is True
            assert any(
                qa in rec.message and "plan_poisoned" in rec.message
                for rec in caplog.records
            )
            assert rt_stages.plan_stage.transient_error == "plan_poisoned"
            assert rt_stages.owners.advancement_engine.query_plan_stage_request(str(fgb["cycle_ref"])) is not None
        finally:
            rt_stages.owners.advancement_engine.query_foreground = orig_fg
    finally:
        rt_stages.close()

    rt_reason = _stage_runtime(tmp_path, "reasoning")
    try:
        qa = str(_confirm_direct_quest_with_prefix(rt_reason, "rs-a")["quest_ref"])
        qb = str(_confirm_direct_quest_with_prefix(rt_reason, "rs-b")["quest_ref"])
        _advance_quest_to_stage(rt_reason, qa, "reasoning")
        _advance_quest_to_stage(rt_reason, qb, "reasoning")
        fga = _active_foreground_by_quest(rt_reason, qa)
        fgb = _active_foreground_by_quest(rt_reason, qb)

        orig_fg = rt_reason.owners.advancement_engine.query_foreground

        def poisoned_reasoning(qref: str):
            if qref == qa:
                raise OwnerConflict("reasoning_poisoned", details={"quest_ref": qa})
            return orig_fg(qref)

        rt_reason.reasoning_stage._provider_cursor_cycle_ref = str(fgb["cycle_ref"])
        rt_reason.owners.advancement_engine.query_foreground = poisoned_reasoning
        try:
            caplog.clear()
            assert rt_reason.reasoning_stage.process_once() is True
            assert any(
                qa in rec.message and "reasoning_poisoned" in rec.message
                for rec in caplog.records
            )
            assert rt_reason.reasoning_stage.transient_error == "reasoning_poisoned"
            assert rt_reason.owners.advancement_engine.query_reasoning_stage_request(str(fgb["cycle_ref"])) is not None
        finally:
            rt_reason.owners.advancement_engine.query_foreground = orig_fg
    finally:
        rt_reason.close()


def test_completion_autonomous_and_goal_reconciler_isolate_owner_conflict(tmp_path: Path, caplog) -> None:
    caplog.set_level(logging.WARNING)
    rt_cmp = _stage_runtime(tmp_path, "completion")
    try:
        starts = []
        for idx, prefix in enumerate(("cmp-a", "cmp-b")):
            q = _confirm_direct_quest_with_prefix(rt_cmp, prefix)
            qref = str(q["quest_ref"])
            _advance_quest_to_stage(rt_cmp, qref, "reasoning")
            cycle_ref = str(_active_foreground_by_quest(rt_cmp, qref)["cycle_ref"])
            req = _drive_reasoning_commit(rt_cmp, cycle_ref)
            run = rt_cmp.owners.agent_runtime.query_reasoning_stage_run(req.request_ref)
            assert run is not None and run.execution is not None
            decision = rt_cmp.owners.research_graph.query_reasoning_outcome_decision(
                run.execution.submission_ref
            )
            assert decision is not None and decision.outcome_ref is not None
            binding = rt_cmp.owners.research_graph.query_reasoning_transition_binding(
                decision.outcome_ref,
                decision.receipt,
            )
            outcome_ref = str(decision.outcome_ref)
            cand_ref = str(binding["transition_ref"])
            starts.append(
                (
                    qref,
                    rt_cmp.quest_completion.start(
                        source_outcome_ref=outcome_ref,
                        candidate_completion_ref=cand_ref,
                        idempotency_key=f"cmp-start-{idx}",
                    ),
                )
            )
        qa_ref, start_a = starts[0]
        _qb_ref, start_b = starts[1]
        ctx_a = str(start_a["context_ref"])
        orig_query_hc = rt_cmp.owners.human_collaboration.query_quest_completion

        def poisoned_cmp(context_ref: str):
            if context_ref == ctx_a:
                raise OwnerConflict("completion_poisoned", details={"quest_ref": qa_ref})
            return orig_query_hc(context_ref)

        rt_cmp.owners.human_collaboration.query_quest_completion = poisoned_cmp
        try:
            caplog.clear()
            assert rt_cmp.quest_completion.process_once() is True
            assert any(
                qa_ref in rec.message and "completion_poisoned" in rec.message
                for rec in caplog.records
            )
            assert rt_cmp.quest_completion.transient_error == "completion_poisoned"
            view_b = rt_cmp.quest_completion.query(str(start_b["context_ref"]))
            assert view_b["status"] == "awaiting_human_confirmation"
        finally:
            rt_cmp.owners.human_collaboration.query_quest_completion = orig_query_hc
    finally:
        rt_cmp.close()

    rt_auto = _stage_runtime(
        tmp_path,
        "autonomous",
        reasoning_skill=_UniqueAutonomousReasoningSkill(require_source_literature=False),
        acquisition=_ReadyAutonomousAcquisitionProvider(),
    )
    try:
        qa = str(_confirm_direct_quest_with_prefix(rt_auto, "auto-a")["quest_ref"])
        qb = str(_confirm_direct_quest_with_prefix(rt_auto, "auto-b")["quest_ref"])
        for qref in (qa, qb):
            _advance_quest_to_stage(rt_auto, qref, "reasoning")
        for _ in range(40):
            ready = 0
            for qref in (qa, qb):
                cyc = str(_active_foreground_by_quest(rt_auto, qref)["cycle_ref"])
                req = rt_auto.owners.advancement_engine.query_reasoning_stage_request(cyc)
                run = (
                    None
                    if req is None
                    else rt_auto.owners.agent_runtime.query_reasoning_stage_run(req.request_ref)
                )
                if run is not None and run.autonomous_checkpoint is not None:
                    ready += 1
            if ready >= 2:
                break
            rt_auto.reasoning_stage.process_once()
        assert rt_auto.autonomous_creation.process_once() is True
        assert rt_auto.autonomous_creation.process_once() is True
        ctxs = rt_auto.owners.human_collaboration.query_autonomous_creation_contexts()
        assert len(ctxs) >= 2
        ctx_b = next(c["context_ref"] for c in ctxs if c["source"]["quest_ref"] == qb)
        rt_auto.autonomous_creation._scheduler_cursor = f"context:{ctx_b}"

        orig_prepare = rt_auto.owners.agent_runtime.prepare_acquisition_session

        def poisoned_auto(*, initialization_id, draft_revision, config, provider):
            quest_a_init = rt_auto.owners.research_graph.query_quest_by_ref(qa).initialization_id
            if initialization_id == quest_a_init:
                raise OwnerConflict("autonomous_poisoned", details={"quest_ref": qa})
            return orig_prepare(
                initialization_id=initialization_id,
                draft_revision=draft_revision,
                config=config,
                provider=provider,
            )

        rt_auto.owners.agent_runtime.prepare_acquisition_session = poisoned_auto
        try:
            caplog.clear()
            assert rt_auto.autonomous_creation.process_once() is True
            assert any(
                qa in rec.message and "autonomous_poisoned" in rec.message
                for rec in caplog.records
            )
            assert rt_auto.autonomous_creation.transient_error == "autonomous_poisoned"
            qb_session = rt_auto.owners.agent_runtime.query_acquisition_session(quest_ref=qb)
            assert qb_session is not None
        finally:
            rt_auto.owners.agent_runtime.prepare_acquisition_session = orig_prepare
    finally:
        rt_auto.close()

    rt_goal = _stage_runtime(tmp_path, "goal-reconcile")
    try:
        qa = str(_confirm_direct_quest_with_prefix(rt_goal, "gr-a")["quest_ref"])
        qb = str(_confirm_direct_quest_with_prefix(rt_goal, "gr-b")["quest_ref"])
        for idx, qref in enumerate((qa, qb), start=1):
            _advance_quest_to_stage(rt_goal, qref, "reasoning")
            cycle_ref = str(_active_foreground_by_quest(rt_goal, qref)["cycle_ref"])
            _drive_reasoning_commit(rt_goal, cycle_ref)
            _evolve_goal_for_quest(rt_goal, qref, idx)

        orig_reassess = rt_goal.owners.advancement_engine.ensure_goal_reasoning_reassessment

        def poisoned_reassess(*, quest_ref, goal_revision_ref, idempotency_key):
            if quest_ref == qa:
                raise OwnerConflict("reassessment_poisoned", details={"quest_ref": qa})
            return orig_reassess(
                quest_ref=quest_ref,
                goal_revision_ref=goal_revision_ref,
                idempotency_key=idempotency_key,
            )

        rt_goal.goal_work_reconciler._reassessment_cursor = qb
        rt_goal.owners.advancement_engine.ensure_goal_reasoning_reassessment = poisoned_reassess
        try:
            caplog.clear()
            assert rt_goal.goal_work_reconciler.process_once() is True
            assert any(
                qa in rec.message and "reassessment_poisoned" in rec.message
                for rec in caplog.records
            )
            assert rt_goal.goal_work_reconciler.transient_error == "reassessment_poisoned"
            fgb_after = _active_foreground_by_quest(rt_goal, qb)
            assert int(fgb_after["epoch"]) > 1
        finally:
            rt_goal.owners.advancement_engine.ensure_goal_reasoning_reassessment = orig_reassess
    finally:
        rt_goal.close()


def _evolve_goal_for_quest(runtime, quest_ref: str, idx: int) -> None:
    fg = _active_foreground_by_quest(runtime, quest_ref)
    cycle_ref = str(fg["cycle_ref"])
    req = runtime.owners.advancement_engine.query_reasoning_stage_request(cycle_ref)
    assert req is not None
    run = runtime.owners.agent_runtime.query_reasoning_stage_run(req.request_ref)
    assert run is not None
    with runtime._database.read() as conn:
        op_row = conn.execute(
            text(
                "SELECT operation_ref FROM ar_stage_provider_invocations "
                "WHERE run_ref=:run_ref AND phase='primary' "
                "ORDER BY completed_at DESC,invocation_ref DESC LIMIT 1"
            ),
            {"run_ref": run.run_ref},
        ).first()
    assert op_row is not None
    intake = runtime.owners.research_memory.submit_asset_intake(
        AssetIntakeRequest(
            source_kind="text",
            custody_mode="managed",
            display_name=f"gr-ev-{idx}.txt",
            media_type="text/plain",
            content=f"New observation for quest {idx}\n".encode("utf-8"),
            origin_quest_ref=quest_ref,
        ),
        idempotency_key=f"gr-intake-{idx}",
    )
    assert intake.status == "accepted" and intake.asset is not None
    asset = intake.asset
    runtime.owners.research_graph.accept_asset_role(
        binding=asset.as_binding(),
        role="evidence",
        quest_ref=quest_ref,
        idempotency_key=f"gr-role-{idx}",
    )
    author = ResearchRoot(
        kind="reasoning",
        run_ref=run.run_ref,
        attempt_ref=run.attempt_ref,
        root_session_ref=run.root_session_ref,
        fence_ref=run.fence_ref,
        runtime_binding_hash=run.runtime_binding_hash,
        operation_ref=op_row.operation_ref,
    )
    with runtime._database.fenced_write() as conn:
        direction_cut = runtime.owners.research_graph.freeze_quest_direction(
            connection=conn,
            quest_ref=quest_ref,
            author=author,
        )
    conditions = direction_cut["conditions"]
    work = tuple(WorkHandle(**item) for item in direction_cut["work"]["items"])
    decision = EvolutionDecision(
        expected_revision=direction_cut["goal"]["goal_revision_ref"],
        conditions_basis=conditions["basis_ref"],
        work_basis=direction_cut["work"]["basis_ref"],
        cause={
            "kind": "evidence",
            "evidence": [{"source_ref": asset.asset_ref, "version_ref": asset.version_ref}],
        },
        replacement={
            "goal": f"Reassess goal {idx}",
            "completion_criteria": "Verify evolved criteria cleanly.",
        },
        criteria_review="Update completion criteria.",
        judgment="Reassess under new observation.",
        conditions={
            "runtime_conditions_ref": conditions["runtime_conditions"]["revision"],
            "assessments": [
                {
                    "condition_ref": item["condition_ref"],
                    "disposition": "preserved",
                    "explanation": "Preserved.",
                }
                for item in conditions["enduring"]
            ],
            "newly_identified": [],
        },
        arrangements=tuple(
            WorkDecision(
                kind="finish_stage_boundary",
                work=item,
                reason="Finish current boundary.",
            )
            for item in work
        ),
        following_direction="Reassess on higher Reasoning epoch.",
    )
    runtime.owners.research_graph.evolve_quest_goal(
        author=author,
        quest_ref=quest_ref,
        direction_cut=direction_cut,
        effect_id=f"gr-evolve-{idx}",
        decision=decision,
    )


def test_turn_steer_recovers_sending_without_receipt_on_terminal_reconciled_turn(tmp_path: Path, monkeypatch) -> None:
    if os.name != "posix":
        pytest.skip("Production durable provider process boundary runs on Linux")
    workspace = tmp_path / "workspace"
    op_dir = workspace / "provider-operations" / "aa" / ("1" * 64)
    (op_dir / "live-guidance").mkdir(parents=True)
    _key_path, key = ensure_transport_key(workspace)

    sent_payload = {
        "id": "g-lost-receipt",
        "thread_id": "native-root-1",
        "turn_id": "turn-1",
        "text": "Should not be forged as delivered",
    }
    write_transport_envelope(
        op_dir / "live-guidance" / "g-lost-receipt.sending",
        sent_payload,
        key,
    )
    reconciled_payload = {
        "schema_ref": "meta-research/codex-native-reconciled/v1",
        "thread_id": "native-root-1",
        "turn": {
            "id": "turn-1",
            "status": "completed",
            "items": [
                {"id": "initial-msg", "type": "userMessage", "clientId": "init-1"},
                {"id": "agent-msg", "type": "agentMessage", "text": "{}"},
            ],
        },
    }
    write_transport_envelope(op_dir / "native-reconciled.json", reconciled_payload, key)
    monkeypatch.setattr("meta_research.native_codex_bridge.reconcile_native_operation", lambda _d: True)

    observed = native_human_input_history(workspace, "native-root-1")
    assert observed == ()
    receipt_path = op_dir / "live-guidance" / "g-lost-receipt.receipt"
    assert receipt_path.is_file()
    receipt = read_transport_envelope(receipt_path, key)
    assert receipt["status"] == "not_delivered"
    assert receipt["reconciled"] is True
    assert native_human_input_history(workspace, "native-root-1") == ()

    bridge = object.__new__(NativeCodexBridge)
    bdir = tmp_path / "bridge-op"
    (bdir / "live-guidance").mkdir(parents=True)
    _bk_path, bkey = ensure_transport_key(tmp_path)
    for idx in range(1, 3):
        cmd_id = f"cmd-{idx:03d}"
        write_transport_envelope(
            bdir / "live-guidance" / f"{cmd_id}.json",
            {
                "id": cmd_id,
                "message": {
                    "kind": "confirmed_guidance_change",
                    "prepared_at": float(idx),
                    "instruction": "Follow guidance.",
                    "change": {
                        "status": "active",
                        "guide": {
                            "constraint_ref": f"c-{idx}",
                            "revision": 1,
                            "guidance": {"text": f"msg-{idx}"},
                        },
                    },
                    "deliveries": [
                        {
                            "delivery_ref": f"del-{idx}",
                            "guide": {"guidance": {"text": f"msg-{idx}"}},
                        }
                    ],
                },
            },
            bkey,
        )
    dispatched: list[str] = []
    bridge.directory = bdir
    bridge.key = bkey
    bridge.thread_id = "native-root-1"
    bridge.turn_id = "turn-1"
    bridge.terminal = None
    bridge._seen = set()
    bridge._next_reconcile = {}

    def fake_request(method, params=None):
        if method == "turn/steer":
            dispatched.append(str(params["clientUserMessageId"]))
            bridge.terminal = {"id": "turn-1", "status": "completed"}
            return {"turnId": "turn-1"}
        raise AssertionError(f"Unexpected RPC {method}")

    bridge.request = fake_request
    bridge.poll_guidance()
    assert dispatched == ["cmd-001"]


def test_live_guidance_read_fast_path_and_pump_cache_eliminate_idle_fenced_writes(tmp_path: Path) -> None:
    runtime = _stage_runtime(tmp_path, "live-guidance", idea_skill=_DeterministicIdeaSkill())
    try:
        quest = _confirm_direct_quest_with_prefix(runtime, "lg")
        quest_ref = str(quest["quest_ref"])
        cycle_ref = str(quest["cycle_ref"])
        cycles = list(runtime.idea_stage._discover_cycles())
        cur = next(c for c in cycles if c.cycle_ref == cycle_ref)
        ae = runtime.owners.advancement_engine
        ar = runtime.owners.agent_runtime
        req = ae.ensure_idea_stage_request(
            cycle_ref=cycle_ref,
            accepted_question=cur.question.as_binding(),
            context_pack=runtime.idea_stage._context_pack(cur),
            idempotency_key="lg-idea-req",
        )
        run = ar.admit_idea_stage(
            req,
            "lg-idea-admit",
            runtime_binding=runtime.idea_stage._provider.runtime_binding(),
        )
        ar.begin_provider_unit(
            unit_ref=run.primary_invocation.invocation_ref,
            operation_ref=run.primary_invocation.operation_ref,
            run_ref=run.run_ref,
            attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref,
            unit_kind="idea_primary",
        )
        scope = GuidanceRuntimeScope(
            "idea",
            run.run_ref,
            run.attempt_ref,
            run.root_session_ref,
            run.fence_ref,
            run.runtime_binding_hash,
        )
        op = StageGuidanceOperation(
            scope,
            run.primary_invocation.operation_ref,
            "primary",
            run.primary_invocation.invocation_ref,
        )
        cut = runtime.owners.human_collaboration.freeze_operation_guidance(op)
        binding = cut.binding

        write_calls = 0
        orig_fw = runtime._database.fenced_write

        @contextmanager
        def counting_fw():
            nonlocal write_calls
            write_calls += 1
            with orig_fw() as conn:
                yield conn

        runtime._database.fenced_write = counting_fw
        try:
            write_calls = 0
            for _ in range(10):
                idle = prepare_live_guidance(
                    runtime.owners.human_collaboration, scope=scope, binding=binding
                )
                assert idle == []
            assert write_calls == 0

            _confirm_guidance(
                runtime,
                quest_ref,
                {"kind": "quest", "quest_ref": quest_ref},
                key="lg-confirm-1",
                text="Focus on low-noise baseline first.",
            )
            write_calls = 0
            updated = prepare_live_guidance(
                runtime.owners.human_collaboration, scope=scope, binding=binding
            )
            assert write_calls == 1
            assert len(updated) == 1

            write_calls = 0
            for _ in range(5):
                again = prepare_live_guidance(
                    runtime.owners.human_collaboration, scope=scope, binding=binding
                )
                assert len(again) == 1
            assert write_calls == 0
        finally:
            runtime._database.fenced_write = orig_fw

        op_dir = tmp_path / "pump-op"
        op_dir.mkdir(parents=True)
        _pk, pkey = ensure_transport_key(tmp_path / "pump-ws")
        pump = _GuidancePump(
            runtime.owners.human_collaboration, scope, binding, op_dir, pkey
        )
        delays: list[float] = []
        polls = 0

        class _FakeStop:
            def wait(self, timeout: float) -> bool:
                nonlocal polls
                delays.append(timeout)
                polls += 1
                return polls >= 4

        pump._stop = _FakeStop()
        pump._run()
        assert len(pump._written) == 1
        assert delays[1] == pytest.approx(0.1)
        assert delays[2] == pytest.approx(0.15)
        assert delays[3] == pytest.approx(0.15)
    finally:
        runtime.close()


def test_offline_linked_local_does_not_block_target_workspace_or_native_session_cleanup(tmp_path: Path, caplog) -> None:
    if os.name != "posix":
        pytest.skip("POSIX O_NOFOLLOW boundary test runs on Linux")
    caplog.set_level(logging.WARNING)
    offline = "/nonexistent-offline-mount/linked/paper.pdf"
    loop_a = tmp_path / "loop_a"
    loop_b = tmp_path / "loop_b"
    loop_a.symlink_to(loop_b)
    loop_b.symlink_to(loop_a)
    live_dir = tmp_path / "live"
    live_dir.mkdir()
    live_file = live_dir / "real.pdf"
    live_file.write_bytes(b"%PDF-1.4\n")

    protected = _protected_originals((offline, str(loop_a), str(live_file)))
    assert Path(os.path.normpath(offline)) in protected
    assert Path(os.path.normpath(str(loop_a))) in protected
    assert live_file.resolve() in protected
    assert any("loop_a" in rec.message for rec in caplog.records)

    with pytest.raises(OwnerConflict) as invalid_err:
        _protected_originals(("relative/paper.pdf",))
    assert invalid_err.value.code == "linked_local_locator_unavailable"

    resolved = _resolve_protected_paths((offline, str(loop_a), str(live_file)))
    assert resolved
    assert _paths_overlap(Path(os.path.normpath(offline)), Path(os.path.normpath(offline)))
    assert "provider-stderr.log" in _LAUNCH_SCRATCH
    assert "root-session-observation-gap.json" in _LAUNCH_SCRATCH


def test_provider_supervisor_stderr_truncation_and_lifecycle_ordering(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("The production durable provider process boundary runs on Linux")
    workspace = tmp_path / "ws"
    op_dir = workspace / "provider-operations" / "aa" / ("2" * 64)
    op_dir.mkdir(parents=True)
    _key_path, key = ensure_transport_key(workspace)

    child_script = tmp_path / "emit_big_stderr.py"
    child_script.write_text(
        """
import json, sys
sys.stderr.write("E" * (256 * 1024))
sys.stderr.flush()
sys.stdout.write(json.dumps({"type": "turn.completed"}) + "\\n")
sys.stdout.flush()
""",
        encoding="utf-8",
    )
    prompt_path = op_dir / "prompt.txt"
    schema_path = op_dir / "output-schema.json"
    result_path = op_dir / "last-message.json"
    prompt_path.write_text("hello", encoding="utf-8")
    schema_path.write_text("{}", encoding="utf-8")
    stream_limit = 32 * 1024
    argv = [
        sys.executable,
        str(child_script),
        "--output-schema",
        str(schema_path),
        "--output-last-message",
        str(result_path),
        "-",
    ]
    inv_hash = canonical_hash({"argv": argv, "prompt": "hello"})
    req_path = op_dir / "supervisor-request.json"
    write_supervisor_request(
        req_path,
        {
            "schema_ref": SUPERVISOR_REQUEST_SCHEMA_V2,
            "invocation_hash": inv_hash,
            "argv": argv,
            "prompt_path": str(prompt_path),
            "schema_path": str(schema_path),
            "stdout_path": str(op_dir / "stdout.jsonl"),
            "result_path": str(result_path),
            "lock_path": str(op_dir / "supervisor.lock"),
            "ready_path": str(op_dir / "supervisor-ready.json"),
            "started_path": str(op_dir / "provider-started.json"),
            "receipt_path": str(op_dir / "supervisor-exit.json"),
            "stop_path": str(op_dir / "supervisor-stop.json"),
            "timeout_seconds": 30,
            "stream_max_bytes": stream_limit,
            "result_max_bytes": 64 * 1024,
        },
        key,
    )
    proc = subprocess.run(
        [sys.executable, "-m", "meta_research.provider_supervisor", str(req_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    exit_receipt = read_transport_envelope(op_dir / "supervisor-exit.json", key)
    assert exit_receipt["returncode"] == 0
    assert exit_receipt["termination_reason"] == "completed"
    obs = read_transport_envelope(op_dir / "call-observation.json", key)
    assert obs["provider_stderr"]["limited"] is True
    assert (op_dir / "provider-stderr.log").stat().st_size <= stream_limit

    ord_dir = workspace / "provider-operations" / "aa" / ("3" * 64)
    ord_dir.mkdir(parents=True)
    (ord_dir / "prompt.txt").write_text("hi", encoding="utf-8")
    (ord_dir / "output-schema.json").write_text("{}", encoding="utf-8")
    ord_req = ord_dir / "supervisor-request.json"
    ord_argv = [
        sys.executable,
        "-c",
        "pass",
        "--output-schema",
        str(ord_dir / "output-schema.json"),
        "--output-last-message",
        str(ord_dir / "last-message.json"),
        "-",
    ]
    write_supervisor_request(
        ord_req,
        {
            "schema_ref": SUPERVISOR_REQUEST_SCHEMA_V2,
            "invocation_hash": canonical_hash({"argv": ord_argv, "prompt": "hi"}),
            "argv": ord_argv,
            "prompt_path": str(ord_dir / "prompt.txt"),
            "schema_path": str(ord_dir / "output-schema.json"),
            "stdout_path": str(ord_dir / "stdout.jsonl"),
            "result_path": str(ord_dir / "last-message.json"),
            "lock_path": str(ord_dir / "supervisor.lock"),
            "ready_path": str(ord_dir / "supervisor-ready.json"),
            "started_path": str(ord_dir / "provider-started.json"),
            "receipt_path": str(ord_dir / "supervisor-exit.json"),
            "stop_path": str(ord_dir / "supervisor-stop.json"),
            "timeout_seconds": 10,
            "stream_max_bytes": 4096,
            "result_max_bytes": 4096,
        },
        key,
    )
    import meta_research.provider_supervisor as ps_mod

    orig_phase = ps_mod._write_phase_marker

    def fail_ready(path, **kwargs):
        if path.name == "supervisor-ready.json":
            raise RuntimeError("abort_at_ready")
        return orig_phase(path, **kwargs)

    ps_mod._write_phase_marker = fail_ready
    try:
        with pytest.raises(RuntimeError, match="abort_at_ready"):
            supervise(ord_req)
        assert not (ord_dir / "provider-stderr.log").exists()
    finally:
        ps_mod._write_phase_marker = orig_phase


def test_provider_supervisor_exit_observation_ordering_gap_marker_and_cleanup(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("POSIX provider observation ordering test runs on Linux")
    workspace = tmp_path / "ws"
    op_dir = workspace / "provider-operations" / "aa" / ("4" * 64)
    op_dir.mkdir(parents=True)
    _key_path, key = ensure_transport_key(workspace)
    prompt = op_dir / "prompt.txt"
    schema = op_dir / "output-schema.json"
    stdout = op_dir / "stdout.jsonl"
    result = op_dir / "last-message.json"
    stderr = op_dir / "provider-stderr.log"
    prompt.write_text("prompt", encoding="utf-8")
    schema.write_text("{}", encoding="utf-8")
    stdout.write_text('{"type":"turn.completed","usage":{"input_tokens":5,"output_tokens":2}}\n', encoding="utf-8")
    result.write_text("{}", encoding="utf-8")
    stderr.write_bytes(b"")
    inv_hash = "b" * 64
    scope = {
        "quest_ref": "quest-1",
        "run_ref": "run-1",
        "root_session_ref": "root-obs-1",
        "root_kind": "idea",
        "operation_ref": "op-obs-1",
    }
    tree = {
        "schema_ref": "meta-research/provider-process-tree/v1",
        "state": "closed",
        "mechanism": "linux-subreaper-echild",
        **supervisor_host_identity(),
    }

    import meta_research.provider_supervisor as ps_mod
    import meta_research.root_session_usage as rsu_mod

    orig_write_env = ps_mod._write_signed_envelope
    idx_dir = _root_index_directory(workspace, "root-obs-1")
    seen_before_exit: dict[str, bool] = {}

    def spy_envelope(path, payload, k):
        if path.name == "supervisor-exit.json":
            seen_before_exit["call_obs"] = (op_dir / "call-observation.json").is_file()
            seen_before_exit["root_idx"] = idx_dir.is_dir() and any(
                p.suffix == ".json" and not p.name.endswith(".gap.json")
                for p in idx_dir.iterdir()
            )
        return orig_write_env(path, payload, k)

    ps_mod._write_signed_envelope = spy_envelope
    try:
        write_exit_receipt(
            op_dir / "supervisor-exit.json",
            key=key,
            invocation_hash=inv_hash,
            prompt_path=prompt,
            schema_path=schema,
            stdout_path=stdout,
            result_path=result,
            stderr_path=stderr,
            returncode=0,
            input_bytes=6,
            process_tree=tree,
            termination_reason="completed",
            schema_ref=SUPERVISOR_EXIT_SCHEMA_V2,
            operation_scope=scope,
        )
    finally:
        ps_mod._write_signed_envelope = orig_write_env

    assert seen_before_exit == {"call_obs": True, "root_idx": True}

    op_dir2 = workspace / "provider-operations" / "aa" / ("5" * 64)
    op_dir2.mkdir(parents=True)
    for p in (
        op_dir2 / "prompt.txt",
        op_dir2 / "output-schema.json",
        op_dir2 / "stdout.jsonl",
        op_dir2 / "last-message.json",
        op_dir2 / "provider-stderr.log",
    ):
        p.write_text("{}", encoding="utf-8")
    scope2 = {**scope, "operation_ref": "op-obs-gap"}
    orig_idx = rsu_mod.write_root_session_observation_index

    def fail_idx(*args, **kwargs):
        raise RootSessionUsageError("root_session_observation_unavailable")

    rsu_mod.write_root_session_observation_index = fail_idx
    try:
        write_exit_receipt(
            op_dir2 / "supervisor-exit.json",
            key=key,
            invocation_hash="c" * 64,
            prompt_path=op_dir2 / "prompt.txt",
            schema_path=op_dir2 / "output-schema.json",
            stdout_path=op_dir2 / "stdout.jsonl",
            result_path=op_dir2 / "last-message.json",
            stderr_path=op_dir2 / "provider-stderr.log",
            returncode=0,
            input_bytes=2,
            process_tree=tree,
            termination_reason="completed",
            schema_ref=SUPERVISOR_EXIT_SCHEMA_V2,
            operation_scope=scope2,
        )
    finally:
        rsu_mod.write_root_session_observation_index = orig_idx

    assert list(idx_dir.glob("*.gap.json"))
    with pytest.raises(RootSessionUsageError, match="root_session_observation_unavailable"):
        read_root_session_observations(workspace, "root-obs-1")

    obs2 = read_transport_envelope(op_dir2 / "call-observation.json", key)
    write_root_session_observation_index(
        op_dir2,
        key=key,
        invocation_hash="c" * 64,
        observation=obs2,
        operation_scope=scope2,
    )
    assert not list(idx_dir.glob("*.gap.json"))
    assert len(read_root_session_observations(workspace, "root-obs-1")) == 2

    gap_dir = workspace / "provider-operations" / "aa" / ("6" * 64)
    gap_dir.mkdir(parents=True)
    (gap_dir / "root-session-observation-gap.json").write_text("{}", encoding="utf-8")
    _remove_durable_job(gap_dir)
    assert not gap_dir.exists()


def test_rollout_incremental_offset_reading_and_truncation_fallback(tmp_path: Path, monkeypatch) -> None:
    import meta_research.provider_call_observations as pco

    ts_ms = int(datetime(2026, 11, 23, 12, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    session_id = str(UUID(int=(ts_ms << 80) | (0x7 << 76) | (0x2 << 62) | 1))
    codex_home = tmp_path / "codex"
    rollout = codex_home / "sessions" / "2026" / "11" / "23" / f"rollout-20261123-{session_id}.jsonl"
    rollout.parent.mkdir(parents=True)
    cursor_dir = tmp_path / "cursors"
    prompt = tmp_path / "prompt.txt"
    stdout = tmp_path / "stdout.jsonl"
    prompt.write_text("p", encoding="utf-8")

    def _response_line(seq: int, inp: int, out: int) -> str:
        return json.dumps(
            {
                "timestamp": f"2026-11-23T00:00:{seq:02d}Z",
                "type": "token_usage_record",
                "payload": {
                    "thread_id": session_id,
                    "session_id": session_id,
                    "response_id": f"resp-{seq}",
                    "turn_id": "turn-1",
                    "root_turn_id": "root-1",
                    "usage": {
                        "input_tokens": inp,
                        "output_tokens": out,
                        "cached_input_tokens": inp // 2,
                    },
                },
            }
        )

    def _compact_line(seq: int) -> str:
        return json.dumps(
            {"timestamp": f"2026-11-23T00:01:{seq:02d}Z", "type": "compacted", "payload": {}}
        )

    stdout.write_text(
        json.dumps({"type": "thread.started", "thread_id": session_id}) + "\n",
        encoding="utf-8",
    )
    meta_line = json.dumps(
        {"type": "session_meta", "payload": {"id": session_id, "padding": "X" * 1100}}
    )
    lines = [meta_line, _response_line(1, 100, 10)]
    rollout.write_text("\n".join(lines) + "\n", encoding="utf-8")
    pco._NATIVE_ROLLOUT_CACHE.clear()
    obs1 = observe_provider_files(prompt, stdout, codex_home=codex_home, rollout_cursor_dir=cursor_dir)
    assert len(obs1["native_session"]["model_responses"]) == 1

    lines.extend([_response_line(2, 200, 20), _compact_line(2)])
    rollout.write_text("\n".join(lines) + "\n", encoding="utf-8")
    inc_lru = observe_provider_files(prompt, stdout, codex_home=codex_home, rollout_cursor_dir=cursor_dir)
    pco._NATIVE_ROLLOUT_CACHE.clear()
    fresh_obs2 = observe_provider_files(prompt, stdout, codex_home=codex_home, rollout_cursor_dir=None)
    assert {k: v for k, v in inc_lru["native_session"].items() if k != "sampled_at_utc"} == {
        k: v for k, v in fresh_obs2["native_session"].items() if k != "sampled_at_utc"
    }

    lines.append(_response_line(3, 300, 30))
    rollout.write_text("\n".join(lines) + "\n", encoding="utf-8")
    pco._NATIVE_ROLLOUT_CACHE.clear()
    inc_disk = observe_provider_files(prompt, stdout, codex_home=codex_home, rollout_cursor_dir=cursor_dir)
    pco._NATIVE_ROLLOUT_CACHE.clear()
    fresh_obs3 = observe_provider_files(prompt, stdout, codex_home=codex_home, rollout_cursor_dir=None)
    for field in (
        "model_responses",
        "last_token_usage_snapshots",
        "compactions",
        "model_context_windows",
        "observed_prefix_bytes",
        "observed_source_line_count",
        "limited",
    ):
        assert inc_disk["native_session"][field] == fresh_obs3["native_session"][field]
    assert str(inc_disk["native_session"]["observed_prefix_sha256"]).startswith("incremental:")

    rewritten = "\n".join(
        [
            json.dumps({"type": "session_meta", "payload": {"id": session_id, "padding": "Y" * 1100}}),
            _response_line(1, 50, 5),
        ]
    ) + "\n"
    rollout.write_text(rewritten, encoding="utf-8")
    trunc_obs = observe_provider_files(prompt, stdout, codex_home=codex_home, rollout_cursor_dir=cursor_dir)
    assert len(trunc_obs["native_session"]["model_responses"]) == 1
    assert trunc_obs["native_session"]["model_responses"][0]["input_tokens"] == 50

    monkeypatch.setattr(pco, "_NATIVE_PREFIX_LIMIT", len(meta_line.encode("utf-8")) + 10)
    small_obs = observe_provider_files(prompt, stdout, codex_home=codex_home, rollout_cursor_dir=cursor_dir)
    assert small_obs["native_session"]["limited"] is True

    monkeypatch.undo()
    pco._NATIVE_ROLLOUT_CACHE.clear()
    cursor_files = list(cursor_dir.glob("*.json"))
    assert cursor_files
    for cursor_file in cursor_files:
        payload = json.loads(cursor_file.read_text(encoding="utf-8"))
        payload["prefix_sha256"] = "not-valid-hex"
        payload["responses"] = "corrupt"
        cursor_file.write_text(json.dumps(payload), encoding="utf-8")
    recovered_obs = observe_provider_files(prompt, stdout, codex_home=codex_home, rollout_cursor_dir=cursor_dir)
    assert recovered_obs["native_session"]["status"] == "available"
    assert len(recovered_obs["native_session"]["model_responses"]) == 1

    mid_lines = [
        json.dumps({"type": "session_meta", "payload": {"id": session_id, "padding": "H" * 1100}}),
        _response_line(1, 10, 1),
        _response_line(2, 20, 2),
        _response_line(3, 30, 3),
        _response_line(4, 40, 4),
        _response_line(5, 50, 5),
        json.dumps({"type": "token_count_marker", "payload": {"padding": "T" * 1100}}),
    ]
    rollout.write_text("\n".join(mid_lines) + "\n", encoding="utf-8")
    pco._NATIVE_ROLLOUT_CACHE.clear()
    base_mid = observe_provider_files(prompt, stdout, codex_home=codex_home, rollout_cursor_dir=cursor_dir)
    assert len(base_mid["native_session"]["model_responses"]) == 5

    tampered_mid = list(mid_lines)
    tampered_mid[3] = _response_line(3, 9999, 9)
    rollout.write_text("\n".join(tampered_mid) + "\n", encoding="utf-8")
    pco._NATIVE_ROLLOUT_CACHE.clear()
    reread_mid = observe_provider_files(prompt, stdout, codex_home=codex_home, rollout_cursor_dir=cursor_dir)
    inputs_by_resp = {
        item["response_id"]: item["input_tokens"]
        for item in reread_mid["native_session"]["model_responses"]
    }
    assert inputs_by_resp["resp-3"] == 9999


def test_deepfetch_cross_upgrade_operation_scope_reconciliation(tmp_path: Path) -> None:
    workspace = tmp_path / "df-ws"
    acquisition = RecordingAcquisitionClient(tmp_path / "acq")
    adapter = _bind_acquisition(
        CodexDeepFetchAdapter(
            workspace,
            model_ref="gpt-test",
            process_runner=SequencedPrototypeRunner([PROTOTYPE_ACQUIRE, PROTOTYPE_FINAL]),
        ),
        acquisition,
    )
    job_ref = "deepfetch-run:cross-upgrade"
    req = replace(
        _base_deepfetch_request(),
        job_ref=job_ref,
        runtime_binding=adapter.runtime_binding(),
        native_session_ref="native-df-1",
        reconcile_only=True,
    )
    binding_hash = canonical_hash(adapter.runtime_binding().as_dict())
    provider_job_ref = f"{job_ref}:v4-turn:0"
    identity_leaf = canonical_hash({"job_ref": provider_job_ref})
    op_root = workspace / "provider-operations" / f"{binding_hash}-{identity_leaf}"
    seg0 = op_root / "deepfetch-initial"
    seg1 = op_root / "deepfetch-resume-1"
    seg0.mkdir(parents=True)
    seg1.mkdir(parents=True)
    _kp, key = ensure_transport_key(workspace)

    run_root = adapter._protocol_run_root(adapter._provider_execution_key(job_ref), binding_hash)
    public_root = run_root / "public"
    public_root.mkdir(parents=True, exist_ok=True)
    schema_text = canonical_json(_deepfetch_output_schema())
    prompt_text = f"public_output_root={public_root}\ngoal=test\n"
    profile = root_capability_profile("deepfetch")
    base_inv = {
        "schema_ref": "meta-research/deepfetch-provider-operation/v3",
        "job_ref": provider_job_ref,
        "request_ref": req.request_ref,
        "correlation_ref": req.correlation_ref,
        "draft_hash": req.draft_hash,
        "scope_hash": req.scope_hash,
        "runtime_binding_hash": binding_hash,
        "prompt_hash": canonical_hash(prompt_text),
        "output_schema_hash": canonical_hash(_deepfetch_output_schema()),
        "model_ref": req.runtime_binding.model_ref,
        "root_capability_profile": profile.as_dict(),
        "root_capability_profile_hash": profile.digest,
    }
    tree = {
        "schema_ref": "meta-research/provider-process-tree/v1",
        "state": "closed",
        "mechanism": "linux-subreaper-echild",
        **supervisor_host_identity(),
    }

    def _write_seg(directory: Path, seg_name: str, native_ref: str | None, term: str, rc: int | None, result_obj: dict, op_scope: dict | None):
        (directory / "prompt.txt").write_text(prompt_text, encoding="utf-8")
        (directory / "output-schema.json").write_text(schema_text, encoding="utf-8")
        (directory / "last-message.json").write_text(json.dumps(result_obj), encoding="utf-8")
        stdout_lines = [json.dumps({"type": "thread.started", "thread_id": "native-df-1"})]
        if term == "completed":
            stdout_lines.append(json.dumps({"type": "turn.completed"}))
        (directory / "stdout.jsonl").write_text("\n".join(stdout_lines) + "\n", encoding="utf-8")
        inv = {**base_inv, "segment_name": seg_name, "native_session_ref": native_ref}
        write_transport_envelope(directory / "invocation.json", inv, key)
        argv = adapter._durable_argv(
            job_ref=provider_job_ref,
            runtime_binding_hash=binding_hash,
            schema_path=directory / "output-schema.json",
            result_path=directory / "last-message.json",
            native_session_ref=native_ref,
        )
        sup_req = {
            "schema_ref": SUPERVISOR_REQUEST_SCHEMA,
            "invocation_hash": canonical_hash(inv),
            "argv": argv,
            "prompt_path": str(directory / "prompt.txt"),
            "schema_path": str(directory / "output-schema.json"),
            "stdout_path": str(directory / "stdout.jsonl"),
            "result_path": str(directory / "last-message.json"),
            "lock_path": str(directory / "supervisor.lock"),
            "ready_path": str(directory / "supervisor-ready.json"),
            "started_path": str(directory / "provider-started.json"),
            "receipt_path": str(directory / "supervisor-exit.json"),
            "stop_path": str(directory / "supervisor-stop.json"),
            "timeout_seconds": None,
            "stream_max_bytes": DEEPFETCH_PROVIDER_STREAM_MAX_BYTES,
            "result_max_bytes": PROVIDER_RESULT_MAX_BYTES,
        }
        if op_scope is not None:
            sup_req["operation_scope"] = op_scope
        write_transport_envelope(directory / "supervisor-request.json", sup_req, key)
        write_exit_receipt(
            directory / "supervisor-exit.json",
            key=key,
            invocation_hash=canonical_hash(inv),
            prompt_path=directory / "prompt.txt",
            schema_path=directory / "output-schema.json",
            stdout_path=directory / "stdout.jsonl",
            result_path=directory / "last-message.json",
            stderr_path=None,
            returncode=rc,
            input_bytes=(directory / "prompt.txt").stat().st_size,
            process_tree=tree,
            termination_reason=term,
            schema_ref=SUPERVISOR_EXIT_SCHEMA,
        )

    _write_seg(seg0, "initial", None, "stopped", -15, {}, None)
    post_scope = {
        "run_ref": req.run_ref,
        "attempt_ref": "attempt-2",
        "fence_ref": "fence-2",
        "root_session_ref": req.root_session_ref,
        "root_kind": "deepfetch",
        "operation_ref": provider_job_ref,
        "job_ref": provider_job_ref,
        "model_ref": req.runtime_binding.model_ref,
    }
    _write_seg(seg1, "resume-1", "native-df-1", "completed", 0, PROTOTYPE_EMPTY_FINAL, post_scope)

    result_doc, observed_native, _, _ = adapter._read_existing_turn(
        req,
        public_root=public_root,
        provider_job_ref=provider_job_ref,
        runtime_binding_hash=binding_hash,
        expected_native_session_ref=None,
        enforce_native_session_ref=False,
        expected_web_gate=False,
        resident_phase="turn-0",
    )
    assert observed_native == "native-df-1"
    assert result_doc["completion"] == "honest_empty"

    seg2 = op_root / "deepfetch-resume-2"
    seg2.mkdir(parents=True)
    _write_seg(
        seg2,
        "resume-2",
        "native-df-1",
        "completed",
        0,
        PROTOTYPE_EMPTY_FINAL,
        {**post_scope, "run_ref": "other-run"},
    )
    with pytest.raises((ValueError, DeepFetchUnavailable)):
        adapter._read_existing_turn(
            req,
            public_root=public_root,
            provider_job_ref=provider_job_ref,
            runtime_binding_hash=binding_hash,
            expected_native_session_ref=None,
            enforce_native_session_ref=False,
            expected_web_gate=False,
            resident_phase="turn-0",
        )


def test_hot_update_runtime_binding_loosens_skill_and_prompt_hashes_preserving_core_identity(tmp_path: Path) -> None:
    from meta_research.runtime_binding_compatibility import (
        deepfetch_bindings_compatible,
        idea_bindings_compatible,
        plan_bindings_compatible,
    )

    b_old = BundleRuntimeBinding(
        packaged_skill_bundle_hash="h" * 64,
        instruction_set_hash="i" * 64,
        model_ref="gpt-5.4",
        harness_adapter_ref="codex-v1",
        mcp_bindings=("mcp:core",),
        capability_bindings=(
            "root-capability-profile:sha256:" + ("c" * 64),
        ),
        resource_bindings=(
            "package:meta_research.skills.bundle_stage/SKILL.md@sha256:" + ("a" * 64),
            "adapter-source:meta_research.bundle_skill@sha256:" + ("b" * 64),
            "output-schema:dispatch@sha256:" + ("d" * 64),
            "transport-seal-key:sha256:" + ("e" * 64),
        ),
    )
    b_hot = replace(
        b_old,
        packaged_skill_bundle_hash="1" * 64,
        instruction_set_hash="2" * 64,
        capability_bindings=(
            "root-capability-profile:sha256:" + ("5" * 64),
        ),
        resource_bindings=(
            "package:meta_research.skills.bundle_stage/SKILL.md@sha256:" + ("3" * 64),
            "adapter-source:meta_research.bundle_skill@sha256:" + ("4" * 64),
            "output-schema:dispatch@sha256:" + ("d" * 64),
            "transport-seal-key:sha256:" + ("e" * 64),
        ),
    )
    assert bundle_bindings_compatible(b_old, b_hot)
    assert not bundle_bindings_compatible(b_old, replace(b_hot, model_ref="other-model"))
    assert not bundle_bindings_compatible(
        b_old,
        replace(
            b_hot,
            resource_bindings=(
                "package:meta_research.skills.bundle_stage/SKILL.md@sha256:" + ("3" * 64),
                "adapter-source:meta_research.bundle_skill@sha256:" + ("4" * 64),
                "output-schema:dispatch@sha256:" + ("9" * 64),
                "transport-seal-key:sha256:" + ("e" * 64),
            ),
        ),
    )

    df_old = DeepFetchRuntimeBinding(
        provider_ref="codex-deepfetch",
        provider_version="v4",
        model_ref="gpt-5.4",
        harness_ref="codex-v1",
        capability_bindings=(
            "deepfetch-v4-skill-bundle-sha256:" + ("a" * 64),
            "adapter-source:meta_research.deepfetch@sha256:" + ("b" * 64),
            "root-capability-profile:sha256:" + ("c" * 64),
            "web-evidence-gate:v1",
        ),
    )
    df_hot = replace(
        df_old,
        capability_bindings=(
            "deepfetch-v4-skill-bundle-sha256:" + ("1" * 64),
            "adapter-source:meta_research.deepfetch@sha256:" + ("2" * 64),
            "root-capability-profile:sha256:" + ("3" * 64),
            "web-evidence-gate:v1",
        ),
    )
    assert deepfetch_bindings_compatible(df_old, df_hot)
    assert not deepfetch_bindings_compatible(df_old, replace(df_hot, harness_ref="other-harness"))

    idea_old = IdeaRuntimeBinding(
        packaged_skill_bundle_hash="a" * 64,
        instruction_set_hash="b" * 64,
        model_ref="gpt-5.4",
        harness_adapter_ref="codex-v1",
        mcp_bindings=(),
        capability_bindings=(),
        resource_bindings=(),
    )
    idea_hot = replace(idea_old, packaged_skill_bundle_hash="c" * 64, instruction_set_hash="d" * 64)
    assert idea_bindings_compatible(idea_old, idea_hot)
    assert not idea_bindings_compatible(idea_old, replace(idea_hot, model_ref="gpt-other"))

    plan_old = PlanRuntimeBinding(**idea_old.__dict__)
    plan_hot = PlanRuntimeBinding(**idea_hot.__dict__)
    assert plan_bindings_compatible(plan_old, plan_hot)

    class _HotIdeaSkill(_DeterministicIdeaSkill):
        def __init__(self) -> None:
            super().__init__()
            self.skill_version = "v1"

        def runtime_binding(self) -> IdeaRuntimeBinding:
            base = super().runtime_binding()
            return replace(
                base,
                packaged_skill_bundle_hash=canonical_hash({"skill": self.skill_version}),
                instruction_set_hash=canonical_hash({"prompt": self.skill_version}),
            )

    skill = _HotIdeaSkill()
    rt = _stage_runtime(tmp_path, "hot-restart", idea_skill=skill)
    try:
        _confirm_direct_quest_with_prefix(rt, "hot")
        assert rt.idea_stage.process_once()
        skill.skill_version = "v2"
        for _ in range(8):
            view = rt.idea_stage.query_current()
            if view["stage_commit"] is not None:
                break
            assert rt.idea_stage.process_once()
            assert rt.idea_stage.transient_error != "idea_runtime_binding_drift"
        assert rt.idea_stage.query_current()["stage_commit"] is not None
    finally:
        rt.close()


def test_goal_work_intents_isolate_corruption_fulfill_healthy_target_and_escalate_health(
    tmp_path: Path, caplog
) -> None:
    caplog.set_level(logging.WARNING)
    drafting = _DeterministicDraftingAdapter()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "goal-intents"),
        proposal_drafter=drafting,
        intent_drafting_provider=drafting,
        host_compute_probe=_DeterministicProbe(),
        idea_skill_provider=_UniquePlanIdeaSkill(),
        plan_skill_provider=_UniquePlanSkill(no_gap=False),
        bundle_skill_provider=_UniqueBundleSkill(),
        harness_adapters=(_FullConformanceAdapter("codex"),),
    )
    runtime.owners.research_graph._target_candidate_proof_verifier = (
        _AcceptingTargetCandidateProofVerifier()
    )
    try:
        if runtime.harnesses.query_status()["status"] != "ready":
            runtime.harnesses.start_full_conformance(_full_request())
            for _ in range(4):
                if runtime.harnesses.query_status()["status"] == "ready":
                    break
                assert runtime.harnesses.advance_full_conformance(
                    mcp_base_url="http://127.0.0.1:0"
                )
            assert runtime.harnesses.query_status()["status"] == "ready"

        quest_targets: list[tuple[int, str, str, object]] = []
        for idx, prefix in ((1, "gi-a"), (2, "gi-b")):
            qref = str(_confirm_direct_quest_with_prefix(runtime, prefix)["quest_ref"])
            _advance_quest_to_stage(runtime, qref, "bundle")
            fg = _active_foreground_by_quest(runtime, qref)
            cycle_ref = str(fg["cycle_ref"])
            target_ref = None
            bundle_run = None
            for _ in range(20):
                runtime.bundle_stage.process_once()
                req = runtime.owners.advancement_engine.query_bundle_stage_request(cycle_ref)
                if req is None:
                    continue
                bundle_run = runtime.owners.agent_runtime.query_bundle_stage_run(req.request_ref)
                if bundle_run is None:
                    continue
                decisions = runtime.owners.agent_runtime.query_bundle_dispatch_decisions(
                    bundle_run.run_ref
                )
                if not decisions:
                    continue
                graph = runtime.owners.research_graph.query_target_graph(bundle_run.request_ref)
                if graph is None:
                    continue
                selected = decisions[-1].selected_target_refs[0]
                if (
                    runtime.owners.research_graph.query_target_candidate_projection(
                        target_ref=selected
                    )
                    is None
                ):
                    continue
                if runtime.owners.agent_runtime.query_admitted_target_launch(selected) is None:
                    launch_req = runtime.owners.research_graph.query_target_launch_request(
                        selected
                    )
                    runtime.owners.agent_runtime.admit_target_launch(
                        launch_req,
                        dispatch_decision_ref=decisions[-1].decision_ref,
                        idempotency_key=f"admit-{prefix}",
                    )
                target_ref = selected
                break
            assert target_ref is not None and bundle_run is not None
            quest_targets.append((idx, qref, target_ref, bundle_run))

        intent_refs: dict[str, str] = {}
        for idx, qref, target_ref, bundle_run in quest_targets:
            with runtime._database.read() as conn:
                op_row = conn.execute(
                    text(
                        "SELECT operation_ref FROM ar_stage_provider_invocations "
                        "WHERE run_ref=:run_ref AND phase='primary' "
                        "ORDER BY completed_at DESC,invocation_ref DESC LIMIT 1"
                    ),
                    {"run_ref": bundle_run.run_ref},
                ).first()
            assert op_row is not None
            intake = runtime.owners.research_memory.submit_asset_intake(
                AssetIntakeRequest(
                    source_kind="text",
                    custody_mode="managed",
                    display_name=f"gi-ev-{idx}.txt",
                    media_type="text/plain",
                    content=f"Goal evolution evidence {idx}\n".encode("utf-8"),
                    origin_quest_ref=qref,
                ),
                idempotency_key=f"gi-intake-{idx}",
            )
            assert intake.status == "accepted" and intake.asset is not None
            asset = intake.asset
            runtime.owners.research_graph.accept_asset_role(
                binding=asset.as_binding(),
                role="evidence",
                quest_ref=qref,
                idempotency_key=f"gi-role-{idx}",
            )
            author = ResearchRoot(
                kind="bundle",
                run_ref=bundle_run.run_ref,
                attempt_ref=bundle_run.attempt_ref,
                root_session_ref=bundle_run.root_session_ref,
                fence_ref=bundle_run.fence_ref,
                runtime_binding_hash=bundle_run.runtime_binding_hash,
                operation_ref=op_row.operation_ref,
            )
            with runtime._database.fenced_write() as conn:
                direction_cut = runtime.owners.research_graph.freeze_quest_direction(
                    connection=conn,
                    quest_ref=qref,
                    author=author,
                )
            conditions = direction_cut["conditions"]
            work = tuple(WorkHandle(**item) for item in direction_cut["work"]["items"])
            assert any(
                item.kind == "target" and item.target_ref == target_ref and item.state == "queued"
                for item in work
            )
            decision = EvolutionDecision(
                expected_revision=direction_cut["goal"]["goal_revision_ref"],
                conditions_basis=conditions["basis_ref"],
                work_basis=direction_cut["work"]["basis_ref"],
                cause={
                    "kind": "evidence",
                    "evidence": [
                        {"source_ref": asset.asset_ref, "version_ref": asset.version_ref}
                    ],
                },
                replacement={
                    "goal": f"Stop queued target {idx}",
                    "completion_criteria": "Verify do_not_start intent reconciliation.",
                },
                criteria_review="Stop queued work before activation.",
                judgment="Block queued target under updated goal.",
                conditions={
                    "runtime_conditions_ref": conditions["runtime_conditions"]["revision"],
                    "assessments": [
                        {
                            "condition_ref": item["condition_ref"],
                            "disposition": "preserved",
                            "explanation": "Preserved.",
                        }
                        for item in conditions["enduring"]
                    ],
                    "newly_identified": [],
                },
                arrangements=tuple(
                    WorkDecision(
                        kind=("do_not_start" if item.kind == "target" else "finish_stage_boundary"),
                        work=item,
                        reason=(
                            "Do not start queued target after goal change."
                            if item.kind == "target"
                            else "Finish current stage boundary."
                        ),
                    )
                    for item in work
                ),
                following_direction="Continue after blocking queued target.",
            )
            runtime.owners.research_graph.evolve_quest_goal(
                author=author,
                quest_ref=qref,
                direction_cut=direction_cut,
                effect_id=f"gi-evolve-{idx}",
                decision=decision,
            )
            with runtime._database.read() as conn:
                row = conn.execute(
                    text(
                        "SELECT i.intent_ref FROM rg_goal_work_intents i "
                        "JOIN rg_quest_goal_revisions r ON r.revision_ref=i.revision_ref "
                        "WHERE r.quest_ref=:quest_ref AND i.decision_kind='do_not_start' "
                        "ORDER BY i.created_at DESC LIMIT 1"
                    ),
                    {"quest_ref": qref},
                ).first()
            assert row is not None
            intent_refs[qref] = str(row.intent_ref)

        qa = quest_targets[0][1]
        qb = quest_targets[1][1]
        target_b = quest_targets[1][2]
        intent_a = intent_refs[qa]
        intent_b = intent_refs[qb]

        with runtime._database.fenced_write() as conn:
            conn.execute(
                text(
                    "UPDATE rg_goal_work_intents SET decision_json=:bad WHERE intent_ref=:intent_ref"
                ),
                {"bad": '{"corrupt":true}', "intent_ref": intent_a},
            )

        runtime.goal_work_reconciler._intent_cursor = intent_b
        caplog.clear()
        assert runtime.goal_work_reconciler.process_once() is True
        assert any(
            qa in rec.message
            and intent_a in rec.message
            and "goal_work_intent_invalid" in rec.message
            for rec in caplog.records
        )
        assert runtime.goal_work_reconciler.transient_error == "goal_work_intent_invalid"

        with runtime._database.read() as conn:
            block = conn.execute(
                text(
                    "SELECT intent_ref FROM ar_target_goal_admission_blocks WHERE target_ref=:target_ref"
                ),
                {"target_ref": target_b},
            ).first()
        assert block is not None and block.intent_ref == intent_b
        assert runtime.goal_work_reconciler.process_once() is False
        assert runtime.goal_work_reconciler.transient_error == "goal_work_intent_invalid"

        async def _run_worker_once(coro_fn, rt) -> web_mod.ReconciliationHealth:
            health = web_mod.ReconciliationHealth()
            changed = asyncio.Event()
            task = asyncio.create_task(coro_fn(rt, health, on_health_change=changed.set))
            try:
                await asyncio.wait_for(changed.wait(), timeout=5.0)
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            return health

        goal_health = asyncio.run(_run_worker_once(web_mod._process_goal_work, runtime))
        assert goal_health.status == "unavailable"
        assert goal_health.last_error == "goal_work_intent_invalid"

        fake_auto = SimpleNamespace(
            autonomous_creation=SimpleNamespace(
                process_once=lambda: False, transient_error="auto_isolated_error"
            )
        )
        auto_health = asyncio.run(_run_worker_once(web_mod._process_autonomous_creation, fake_auto))
        assert auto_health.status == "unavailable"
        assert auto_health.last_error == "auto_isolated_error"

        fake_cmp = SimpleNamespace(
            quest_completion=SimpleNamespace(
                process_once=lambda: False, transient_error="completion_isolated_error"
            )
        )
        cmp_health = asyncio.run(_run_worker_once(web_mod._process_quest_completion, fake_cmp))
        assert cmp_health.status == "unavailable"
        assert cmp_health.last_error == "completion_isolated_error"
    finally:
        runtime.close()
