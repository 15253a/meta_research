from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import text

from meta_research.idea_contract import IdeaContractError, validate_idea_content
from meta_research.owners.advancement_engine import (
    create_advancement_engine_receipt_verifier,
)
from meta_research.owners.agent_runtime import (
    IdeaRuntimeBinding,
    create_host_compute_observation_reader,
)
from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.paths import prepare_data_root
from test_idea_stage_recovery import (
    _IdeaProvider,
    _confirm_question,
    _runtime,
)


def _confirm_question_with_prefix(runtime, prefix: str) -> dict[str, object]:
    human = runtime.owners.human_collaboration
    opened = human.create_quest({}, f"{prefix}-open")
    probed = human.observe_host_compute(
        opened["initialization_id"],
        ["GPU-test-1"],
        f"{prefix}-probe",
    )
    draft = dict(probed["quest_draft"]["value"])
    draft.update(
        {
            "goal": f"{prefix}：判断低照度去噪能否保留稀有形态。",
            "completion_criteria": "形成带反例和证据边界的比较结论。",
            "time_budget": "30d",
            "route": "direct",
            "literature": {
                "mode": "oa_only",
                "library_entry_url": "",
                "scope_exclusions": "",
                "accepted_material_bindings": [],
            },
            "background_and_initial_direction": "比较自监督和监督基线。",
        }
    )
    saved = human.revise_quest_draft(
        opened["initialization_id"],
        draft,
        probed["quest_draft"]["hash"],
        f"{prefix}-draft",
        probed["quest_draft"]["revision"],
    )
    human.generate_question_proposal(
        opened["initialization_id"],
        saved["quest_draft"]["hash"],
        f"{prefix}-proposal",
        saved["quest_draft"]["revision"],
    )
    assert human.process_drafting_once()
    proposed = human.query_quest_creation(opened["initialization_id"])
    previewed = human.preview_confirmation(
        opened["initialization_id"],
        quest_draft_revision=proposed["quest_draft"]["revision"],
        quest_draft_hash=proposed["quest_draft"]["hash"],
        proposal_ref=proposed["proposal"]["ref"],
        proposal_hash=proposed["proposal"]["hash"],
        idempotency_key=f"{prefix}-preview",
    )
    human.confirm_quest(
        opened["initialization_id"],
        quest_draft_revision=proposed["quest_draft"]["revision"],
        quest_draft_hash=proposed["quest_draft"]["hash"],
        proposal_ref=proposed["proposal"]["ref"],
        proposal_hash=proposed["proposal"]["hash"],
        preview_ref=previewed["confirmation_preview"]["ref"],
        preview_hash=previewed["confirmation_preview"]["hash"],
        idempotency_key=f"{prefix}-confirm",
    )
    for _boundary in range(5):
        if not human.reconcile_once():
            break
    completed = human.query_quest_creation(opened["initialization_id"])
    assert completed["status"] == "completed"
    return completed


def _prepare_direct_idea_request(runtime, completed: dict[str, object], prefix: str):
    question = runtime.owners.research_graph.query_question(
        completed["initialization_id"]
    )
    assert question is not None
    context_pack = {
        "schema_ref": "meta-research/idea-context-pack/v1",
        "cycle_ref": completed["cycle_ref"],
        "accepted_question_binding": question.as_binding().as_dict(),
        "accepted_evidence_refs": [],
        "literature_binding": None,
        "prior_accepted_bindings": [],
        "active_guidance_bindings": [],
    }
    request = runtime.owners.advancement_engine.ensure_idea_stage_request(
        cycle_ref=completed["cycle_ref"],
        accepted_question=question.as_binding(),
        context_pack=context_pack,
        idempotency_key=f"{prefix}-request",
    )
    return question, request


def _admit_direct_idea_request(runtime, completed: dict[str, object], prefix: str):
    question, request = _prepare_direct_idea_request(runtime, completed, prefix)
    run = runtime.owners.agent_runtime.admit_idea_stage(
        request,
        f"{prefix}-admit",
        runtime_binding=_runtime_binding(prefix),
    )
    return question, request, run


def _runtime_binding(prefix: str) -> IdeaRuntimeBinding:
    return IdeaRuntimeBinding(
        packaged_skill_bundle_hash=canonical_hash({"skill": prefix}),
        instruction_set_hash=canonical_hash({"instructions": prefix}),
        model_ref="test-model-v1",
        harness_adapter_ref="test-harness-v1",
        mcp_bindings=(),
        capability_bindings=(),
        resource_bindings=(),
    )


def _outcome() -> dict[str, object]:
    return {
        "kind": "IdeaSet",
        "question_ref": "question-1",
        "context_pack_ref": "context-1",
        "candidates": [
            {
                "candidate_key": "topology",
                "direction": "比较拓扑一致性与像素重建。",
                "rationale": "两类目标函数具有不同的结构偏置。",
                "assumptions": ["受控增强保持形态拓扑。"],
                "risks": ["约束可能保留传感器伪影。"],
                "evidence_boundary": {
                    "accepted_evidence_refs": ["asset-1"],
                    "supported": "材料限定了低照度场景。",
                    "inferred": "拓扑约束可能改善形态保真。",
                    "unknown": "跨设备稳健性未知。",
                },
                "falsification_hint": {
                    "test": "比较形态召回率。",
                    "would_refute": "召回率未改善。",
                },
                "material_difference": {
                    "from_history": "历史中没有同一机制。",
                    "from_peers": "干预轴是拓扑一致性。",
                    "plan_commitment_change": "Plan 比较两类目标函数。",
                },
            }
        ],
        "recommendation": None,
    }


def _review(outcome_hash: str) -> dict[str, object]:
    return {
        "schema_ref": "meta-research/idea-advisory-review/v2",
        "review_mode": "harness_child_agent",
        "reviewer_agent_ref": "reviewer-agent-1",
        "reviewed_draft_hash": outcome_hash,
        "findings": [],
        "dispositions": [],
        "final_outcome_hash": outcome_hash,
        "independent": True,
        "advisory_only": True,
    }


def _legacy_review(outcome_hash: str) -> dict[str, object]:
    return {
        "schema_ref": "meta-research/idea-advisory-review/v1",
        "reviewer_session_ref": "legacy-reviewer-session",
        "reviewed_draft_hash": outcome_hash,
        "findings": [],
        "dispositions": [],
        "final_outcome_hash": outcome_hash,
        "independent": True,
        "advisory_only": True,
    }


def test_legacy_review_payload_remains_readable_without_rewriting() -> None:
    outcome = _outcome()
    outcome_hash, review_hash = validate_idea_content(
        outcome,
        _legacy_review(canonical_hash(outcome)),
        reviewed_draft=outcome,
    )

    assert outcome_hash == canonical_hash(outcome)
    assert review_hash == canonical_hash(
        _legacy_review(canonical_hash(outcome))
    )


def _record_direct_execution(runtime, **values):
    """Drive the public AR seam through its mandatory primary checkpoint."""

    agent_runtime = runtime.owners.agent_runtime
    run = agent_runtime._query_idea_run_by_ref(values["run_ref"])
    reviewed_draft = values.get("reviewed_draft") or values["outcome"]
    if run.primary_draft is None:
        agent_runtime.record_idea_primary_draft(
            run_ref=run.run_ref,
            attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref,
            native_session_ref=values["native_session_ref"],
            runtime_binding=run.runtime_binding,
            draft=reviewed_draft,
            adapter_kind="test_direct",
            idempotency_key=f"{values['idempotency_key']}-primary",
        )
    return agent_runtime.record_idea_attempt_execution(**values)


def test_provider_invocation_identity_is_bound_by_owner_hash(tmp_path: Path) -> None:
    runtime = _runtime(
        prepare_data_root(tmp_path / "provider-invocation-identity"),
        _IdeaProvider(),
    )
    try:
        completed = _confirm_question(runtime)
        _question, _request, run = _admit_direct_idea_request(
            runtime,
            completed,
            "provider-invocation-identity",
        )
        forged_ref = "idea_primary_invocation_" + "f" * 32
        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE ar_idea_provider_invocations SET invocation_ref = "
                    ":forged_ref WHERE invocation_ref = :invocation_ref"
                ),
                {
                    "forged_ref": forged_ref,
                    "invocation_ref": run.primary_invocation.invocation_ref,
                },
            )

        with pytest.raises(
            OwnerConflict,
            match="idea_provider_invocation_invalid",
        ):
            runtime.owners.agent_runtime.query_idea_stage_run(run.request_ref)
    finally:
        runtime.close()


def _bound_outcome(question_ref: str, context_pack_ref: str) -> dict[str, object]:
    outcome = _outcome()
    outcome["question_ref"] = question_ref
    outcome["context_pack_ref"] = context_pack_ref
    candidate = outcome["candidates"][0]  # type: ignore[index]
    candidate["evidence_boundary"]["accepted_evidence_refs"] = []  # type: ignore[index]
    return outcome


def _no_viable_outcome(
    question_ref: str,
    context_pack_ref: str,
) -> dict[str, object]:
    return {
        "kind": "NoViableCandidate",
        "question_ref": question_ref,
        "context_pack_ref": context_pack_ref,
        "exploration_scope": "当前 accepted Question 与空 Evidence closure。",
        "candidate_families_considered": [
            {
                "family": "拓扑一致性自监督",
                "why_not_viable": "当前 closure 无法支持可证伪机制差异。",
                "evidence_refs": [],
            }
        ],
        "evidence_boundary": {
            "accepted_evidence_refs": [],
            "supported": "仅支持 Question 所声明的研究范围。",
            "inferred": "现有 closure 不足以形成候选。",
            "unknown": "新增 accepted Evidence 后是否可形成候选未知。",
        },
        "overturn_conditions": ["接纳能区分机制的新 Evidence。"],
        "why_plan_cannot_proceed": "没有可供 Plan 消费的研究方向。",
    }


def test_idea_contract_rejects_malformed_content_before_an_owner_can_sign_it() -> None:
    malformed = _outcome()
    del malformed["candidates"]

    with pytest.raises(IdeaContractError, match="idea_set_shape_invalid"):
        validate_idea_content(
            malformed,
            _review("0" * 64),
            question_ref="question-1",
            context_pack_ref="context-1",
            accepted_evidence_refs={"asset-1"},
        )


def test_stage_request_verifier_requires_exact_question_and_context_binding(
    tmp_path: Path,
) -> None:
    runtime = _runtime(prepare_data_root(tmp_path / "exact-stage-request"), _IdeaProvider())
    try:
        completed = _confirm_question(runtime)
        runtime.idea_stage.start("exact-stage-request-start")
        request = runtime.owners.advancement_engine.query_idea_stage_request(
            completed["cycle_ref"]
        )
        assert request is not None
        verifier = create_advancement_engine_receipt_verifier(runtime._database)

        verified = verifier.verify_idea_stage_request_binding(
            request_ref=request.request_ref,
            accepted_question=request.accepted_question,
            context_pack_ref=request.context_pack_ref,
        )
        assert verified.context_pack == request.context_pack

        with pytest.raises(OwnerConflict, match="stage_run_request_binding_invalid"):
            verifier.verify_idea_stage_request_binding(
                request_ref=request.request_ref,
                accepted_question=replace(
                    request.accepted_question,
                    content_hash="f" * 64,
                ),
                context_pack_ref=request.context_pack_ref,
            )
        with pytest.raises(OwnerConflict, match="stage_run_request_binding_invalid"):
            verifier.verify_idea_stage_request_binding(
                request_ref=request.request_ref,
                accepted_question=request.accepted_question,
                context_pack_ref="context_forged",
            )
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "forgery",
    (
        {"schema_ref": "meta-research/idea-context-pack/forged"},
        {"cycle_ref": "cycle_forged"},
        {"accepted_question_binding": {"question_ref": "question_forged"}},
        {"accepted_evidence_refs": ["evidence_without_owner_receipt"]},
        {"literature_binding": {"asset_ref": "literature_without_owner_receipt"}},
        {"prior_accepted_bindings": ["idea_without_owner_receipt"]},
        {"active_guidance_bindings": ["guidance_without_owner_receipt"]},
        {"unexpected": True},
    ),
)
def test_advancement_engine_refuses_unverified_context_pack_bindings(
    tmp_path: Path,
    forgery: dict[str, object],
) -> None:
    runtime = _runtime(prepare_data_root(tmp_path / "forged-context"), _IdeaProvider())
    try:
        completed = _confirm_question(runtime)
        question = runtime.owners.research_graph.query_question(
            completed["initialization_id"]
        )
        assert question is not None
        context_pack = {
            "schema_ref": "meta-research/idea-context-pack/v1",
            "cycle_ref": completed["cycle_ref"],
            "accepted_question_binding": question.as_binding().as_dict(),
            "accepted_evidence_refs": [],
            "literature_binding": None,
            "prior_accepted_bindings": [],
            "active_guidance_bindings": [],
        }
        context_pack.update(forgery)
        before = runtime.owners.advancement_engine.query_snapshot()

        with pytest.raises(OwnerConflict, match="idea_context_pack_invalid"):
            runtime.owners.advancement_engine.ensure_idea_stage_request(
                cycle_ref=completed["cycle_ref"],
                accepted_question=question.as_binding(),
                context_pack=context_pack,
                idempotency_key="forged-context-request",
            )

        after = runtime.owners.advancement_engine.query_snapshot()
        assert after.revision == before.revision
        assert after.facts["stage_request_count"] == 0
        assert runtime.owners.advancement_engine.query_idea_stage_request(
            completed["cycle_ref"]
        ) is None
    finally:
        runtime.close()


def test_research_memory_rejects_malformed_execution_content_before_signing(
    tmp_path: Path,
) -> None:
    runtime = _runtime(prepare_data_root(tmp_path / "rm-contract-seam"), _IdeaProvider())
    try:
        completed = _confirm_question(runtime)
        question, request, run = _admit_direct_idea_request(
            runtime,
            completed,
            "rm-contract",
        )
        malformed = {
            "kind": "IdeaSet",
            "question_ref": question.question_ref,
            "context_pack_ref": request.context_pack_ref,
            "candidates": [],
            "recommendation": None,
        }
        execution = _record_direct_execution(
            runtime,
            run_ref=run.run_ref,
            attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref,
            submission_ref="rm-malformed-submission",
            native_session_ref="rm-primary-session",
            runtime_binding=run.runtime_binding,
            outcome=malformed,
            review=_review(canonical_hash(malformed)),
            idempotency_key="rm-malformed-execution",
        )

        memory = runtime.owners.research_memory
        before = memory.query_snapshot()
        with pytest.raises(OwnerConflict, match="idea_set_empty"):
            memory.accept_idea_outcome_content(
                request_ref=request.request_ref,
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                submission_ref=execution.submission_ref,
                outcome=execution.outcome,
                review=execution.review,
                execution_receipt=execution.receipt,
            )
        after = memory.query_snapshot()
        assert after.revision == before.revision
        assert after.facts["idea_content_count"] == 0
        assert memory.query_idea_outcome_content(execution.submission_ref) is None
    finally:
        runtime.close()


def test_agent_runtime_requires_primary_checkpoint_before_execution(
    tmp_path: Path,
) -> None:
    runtime = _runtime(
        prepare_data_root(tmp_path / "execution-requires-primary"),
        _IdeaProvider(),
    )
    try:
        completed = _confirm_question(runtime)
        question, request, run = _admit_direct_idea_request(
            runtime,
            completed,
            "execution-requires-primary",
        )
        outcome = _bound_outcome(question.question_ref, request.context_pack_ref)

        with pytest.raises(OwnerConflict, match="idea_primary_draft_required"):
            runtime.owners.agent_runtime.record_idea_attempt_execution(
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                submission_ref="execution-without-primary",
                native_session_ref="execution-without-primary-native",
                runtime_binding=run.runtime_binding,
                outcome=outcome,
                reviewed_draft=outcome,
                review=_review(canonical_hash(outcome)),
                idempotency_key="execution-without-primary",
            )
    finally:
        runtime.close()


def test_agent_runtime_only_writes_child_agent_review_v2(tmp_path: Path) -> None:
    runtime = _runtime(
        prepare_data_root(tmp_path / "execution-review-identity"),
        _IdeaProvider(),
    )
    try:
        completed = _confirm_question(runtime)
        question, request, run = _admit_direct_idea_request(
            runtime,
            completed,
            "execution-review-identity",
        )
        outcome = _bound_outcome(question.question_ref, request.context_pack_ref)
        invalid_mode = _review(canonical_hash(outcome))
        invalid_mode["review_mode"] = "external_session"

        with pytest.raises(
            OwnerConflict, match="attempt_review_independence_invalid"
        ):
            _record_direct_execution(
                runtime,
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                submission_ref="invalid-review-mode",
                native_session_ref="execution-review-native",
                runtime_binding=run.runtime_binding,
                outcome=outcome,
                reviewed_draft=outcome,
                review=invalid_mode,
                idempotency_key="invalid-review-mode",
            )

        parent_as_reviewer = _review(canonical_hash(outcome))
        parent_as_reviewer["reviewer_agent_ref"] = (
            "execution-review-native"
        )
        with pytest.raises(
            OwnerConflict, match="attempt_review_independence_invalid"
        ):
            _record_direct_execution(
                runtime,
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                submission_ref="parent-as-reviewer",
                native_session_ref="execution-review-native",
                runtime_binding=run.runtime_binding,
                outcome=outcome,
                reviewed_draft=outcome,
                review=parent_as_reviewer,
                idempotency_key="parent-as-reviewer",
            )

        with pytest.raises(
            OwnerConflict, match="attempt_review_legacy_read_only"
        ):
            _record_direct_execution(
                runtime,
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                submission_ref="legacy-review-write",
                native_session_ref="execution-review-native",
                runtime_binding=run.runtime_binding,
                outcome=outcome,
                reviewed_draft=outcome,
                review=_legacy_review(canonical_hash(outcome)),
                idempotency_key="legacy-review-write",
            )

        current = runtime.owners.agent_runtime.query_idea_stage_run(
            request.request_ref
        )
        assert current is not None
        assert current.native_session_ref == "execution-review-native"
        assert current.execution is None
    finally:
        runtime.close()


def test_research_memory_recomputes_reviewed_draft_hash_at_acceptance_seam(
    tmp_path: Path,
) -> None:
    runtime = _runtime(prepare_data_root(tmp_path / "rm-reviewed-draft"), _IdeaProvider())
    try:
        completed = _confirm_question(runtime)
        question, request, run = _admit_direct_idea_request(
            runtime,
            completed,
            "rm-reviewed-draft",
        )
        reviewed_draft = _bound_outcome(
            question.question_ref,
            request.context_pack_ref,
        )
        outcome = _bound_outcome(question.question_ref, request.context_pack_ref)
        outcome["candidates"][0]["direction"] = (  # type: ignore[index]
            "经独立审查后的可证伪拓扑干预。"
        )
        review = {
            "schema_ref": "meta-research/idea-advisory-review/v2",
            "review_mode": "harness_child_agent",
            "reviewer_agent_ref": "rm-independent-reviewer-agent",
            # This is the old exploit: a well-shaped but invented digest, with
            # no immutable reviewed bytes available to the accepting Owner.
            "reviewed_draft_hash": "f" * 64,
            "findings": [
                {
                    "finding_id": "finding-1",
                    "category": "falsifiability",
                    "message": "需要更明确的反驳条件。",
                }
            ],
            "dispositions": [
                {
                    "finding_id": "finding-1",
                    "action": "revised",
                    "rationale": "已改写干预方向。",
                }
            ],
            "final_outcome_hash": canonical_hash(outcome),
            "independent": True,
            "advisory_only": True,
        }
        execution = _record_direct_execution(
            runtime,
            run_ref=run.run_ref,
            attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref,
            submission_ref="rm-reviewed-draft-submission",
            native_session_ref="rm-reviewed-draft-primary",
            runtime_binding=run.runtime_binding,
            outcome=outcome,
            review=review,
            idempotency_key="rm-reviewed-draft-execution",
            reviewed_draft=reviewed_draft,
        )
        assert execution.reviewed_draft == reviewed_draft
        assert execution.reviewed_draft_hash == canonical_hash(reviewed_draft)

        memory = runtime.owners.research_memory
        before = memory.query_snapshot()
        with pytest.raises(OwnerConflict, match="idea_review_draft_hash_mismatch"):
            memory.accept_idea_outcome_content(
                request_ref=request.request_ref,
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                submission_ref=execution.submission_ref,
                outcome=execution.outcome,
                review=execution.review,
                execution_receipt=execution.receipt,
                reviewed_draft=execution.reviewed_draft,
            )
        after = memory.query_snapshot()
        assert after.revision == before.revision
        assert after.facts["idea_content_count"] == 0
        assert memory.query_idea_outcome_content(execution.submission_ref) is None
    finally:
        runtime.close()


def test_reviewed_draft_hash_remains_verifiable_through_rm_and_rg(
    tmp_path: Path,
) -> None:
    runtime = _runtime(prepare_data_root(tmp_path / "reviewed-draft-chain"), _IdeaProvider())
    try:
        completed = _confirm_question(runtime)
        question, request, run = _admit_direct_idea_request(
            runtime,
            completed,
            "reviewed-draft-chain",
        )
        reviewed_draft = _bound_outcome(question.question_ref, request.context_pack_ref)
        outcome = deepcopy(reviewed_draft)
        outcome["candidates"][0]["direction"] = (  # type: ignore[index]
            "以跨增强拓扑稳定性作为可证伪干预轴。"
        )
        review = {
            "schema_ref": "meta-research/idea-advisory-review/v2",
            "review_mode": "harness_child_agent",
            "reviewer_agent_ref": "reviewed-draft-chain-reviewer-agent",
            "reviewed_draft_hash": canonical_hash(reviewed_draft),
            "findings": [
                {
                    "finding_id": "finding-1",
                    "category": "falsifiability",
                    "message": "原方向缺少明确干预轴。",
                }
            ],
            "dispositions": [
                {
                    "finding_id": "finding-1",
                    "action": "revised",
                    "rationale": "最终版本已明确拓扑稳定性干预。",
                }
            ],
            "final_outcome_hash": canonical_hash(outcome),
            "independent": True,
            "advisory_only": True,
        }
        execution = _record_direct_execution(
            runtime,
            run_ref=run.run_ref,
            attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref,
            submission_ref="reviewed-draft-chain-submission",
            native_session_ref="reviewed-draft-chain-native",
            runtime_binding=run.runtime_binding,
            outcome=outcome,
            reviewed_draft=reviewed_draft,
            review=review,
            idempotency_key="reviewed-draft-chain-execution",
        )
        content = runtime.owners.research_memory.accept_idea_outcome_content(
            request_ref=request.request_ref,
            run_ref=run.run_ref,
            attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref,
            submission_ref=execution.submission_ref,
            outcome=execution.outcome,
            reviewed_draft=execution.reviewed_draft,
            review=execution.review,
            execution_receipt=execution.receipt,
        )
        assert content.reviewed_draft == reviewed_draft
        assert content.reviewed_draft_hash == canonical_hash(reviewed_draft)
        question_content = runtime.owners.research_memory.read_question_content(
            question.content_ref,
            question.content_hash,
        )
        decision = runtime.owners.research_graph.decide_idea_outcome(
            accepted_question=question.as_binding(),
            question_content=question_content,
            content=content,
            execution_receipt=execution.receipt,
        )
        assert decision.decision == "accepted"
        assert decision.reviewed_draft_hash == content.reviewed_draft_hash
        assert runtime.owners.research_graph.query_idea_outcome_decision(
            execution.submission_ref
        ) == decision
    finally:
        runtime.close()


def test_native_session_ref_cannot_be_reused_by_another_run(tmp_path: Path) -> None:
    runtime = _runtime(prepare_data_root(tmp_path / "native-session-global"), _IdeaProvider())
    try:
        completed_a = _confirm_question_with_prefix(runtime, "native-a")
        completed_b = _confirm_question_with_prefix(runtime, "native-b")
        question_a, request_a, run_a = _admit_direct_idea_request(
            runtime,
            completed_a,
            "native-a",
        )
        question_b, request_b, run_b = _admit_direct_idea_request(
            runtime,
            completed_b,
            "native-b",
        )
        outcome_a = _bound_outcome(question_a.question_ref, request_a.context_pack_ref)
        outcome_b = _bound_outcome(question_b.question_ref, request_b.context_pack_ref)
        shared_native_ref = "provider-session-must-be-global"
        _record_direct_execution(
            runtime,
            run_ref=run_a.run_ref,
            attempt_ref=run_a.attempt_ref,
            fence_ref=run_a.fence_ref,
            submission_ref="native-a-submission",
            native_session_ref=shared_native_ref,
            runtime_binding=run_a.runtime_binding,
            outcome=outcome_a,
            review=_review(canonical_hash(outcome_a)),
            idempotency_key="native-a-execution",
            reviewed_draft=outcome_a,
        )
        with runtime._database.read() as connection:
            native_index = connection.execute(
                text(
                    "SELECT sql FROM sqlite_master WHERE type = 'index' AND "
                    "name = 'uq_ar_stage_sessions_native_session_ref'"
                )
            ).scalar_one()
        assert "UNIQUE INDEX" in native_index
        assert "native_session_ref IS NOT NULL" in native_index

        with pytest.raises(OwnerConflict, match="native_session_conflict"):
            _record_direct_execution(
                runtime,
                run_ref=run_b.run_ref,
                attempt_ref=run_b.attempt_ref,
                fence_ref=run_b.fence_ref,
                submission_ref="native-b-submission",
                native_session_ref=shared_native_ref,
                runtime_binding=run_b.runtime_binding,
                outcome=outcome_b,
                review=_review(canonical_hash(outcome_b)),
                idempotency_key="native-b-execution",
                reviewed_draft=outcome_b,
            )
        untouched = runtime.owners.agent_runtime.query_idea_stage_run(
            request_b.request_ref
        )
        assert untouched is not None
        assert untouched.status == "running"
        assert untouched.native_session_ref is None
        assert untouched.execution is None
    finally:
        runtime.close()


def test_attempt_execution_rejects_runtime_binding_drift(tmp_path: Path) -> None:
    runtime = _runtime(prepare_data_root(tmp_path / "runtime-binding-drift"), _IdeaProvider())
    try:
        completed = _confirm_question(runtime)
        question, request, run = _admit_direct_idea_request(
            runtime,
            completed,
            "runtime-binding-drift",
        )
        assert run.runtime_binding_hash == canonical_hash(run.runtime_binding.as_dict())
        drifted = replace(run.runtime_binding, model_ref="different-model-v2")
        outcome = _bound_outcome(question.question_ref, request.context_pack_ref)

        with pytest.raises(OwnerConflict, match="idea_runtime_binding_drift"):
            _record_direct_execution(
                runtime,
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                submission_ref="runtime-binding-drift-submission",
                native_session_ref="runtime-binding-drift-native",
                runtime_binding=drifted,
                outcome=outcome,
                review=_review(canonical_hash(outcome)),
                idempotency_key="runtime-binding-drift-execution",
                reviewed_draft=outcome,
            )
        unchanged = runtime.owners.agent_runtime.query_idea_stage_run(
            request.request_ref
        )
        assert unchanged is not None
        assert unchanged.status == "running"
        assert unchanged.native_session_ref == "runtime-binding-drift-native"
        assert unchanged.primary_draft is not None
        assert unchanged.execution is None
    finally:
        runtime.close()


def test_admission_rejects_unapproved_runtime_capabilities_and_resources(
    tmp_path: Path,
) -> None:
    runtime = _runtime(
        prepare_data_root(tmp_path / "runtime-binding-permission-ceiling"),
        _IdeaProvider(),
    )
    try:
        completed = _confirm_question(runtime)
        _question, request = _prepare_direct_idea_request(
            runtime,
            completed,
            "runtime-binding-permission-ceiling",
        )
        unsafe = replace(
            _runtime_binding("runtime-binding-permission-ceiling"),
            mcp_bindings=("unapproved-owner-admin",),
            capability_bindings=("danger-full-access", "network-egress"),
            resource_bindings=("filesystem:/", "gpu:any-unbounded"),
        )

        with pytest.raises(
            OwnerConflict, match="idea_runtime_binding_unauthorized"
        ):
            runtime.owners.agent_runtime.admit_idea_stage(
                request,
                "runtime-binding-permission-ceiling-admit",
                runtime_binding=unsafe,
            )
        assert (
            runtime.owners.agent_runtime.query_idea_stage_run(request.request_ref)
            is None
        )
    finally:
        runtime.close()


def test_research_graph_rejects_valid_question_from_another_stage_request(
    tmp_path: Path,
) -> None:
    runtime = _runtime(
        prepare_data_root(tmp_path / "rg-cross-request-question"),
        _IdeaProvider(),
    )
    try:
        completed_a = _confirm_question_with_prefix(runtime, "cross-a")
        completed_b = _confirm_question_with_prefix(runtime, "cross-b")
        _question_a, request_a, run_a = _admit_direct_idea_request(
            runtime,
            completed_a,
            "cross-a-idea",
        )
        question_b = runtime.owners.research_graph.query_question(
            completed_b["initialization_id"]
        )
        assert question_b is not None

        outcome = _outcome()
        outcome["question_ref"] = question_b.question_ref
        outcome["context_pack_ref"] = request_a.context_pack_ref
        outcome_hash = canonical_hash(outcome)
        execution = _record_direct_execution(
            runtime,
            run_ref=run_a.run_ref,
            attempt_ref=run_a.attempt_ref,
            fence_ref=run_a.fence_ref,
            submission_ref="cross-request-submission",
            native_session_ref="cross-request-primary",
            runtime_binding=run_a.runtime_binding,
            outcome=outcome,
            review=_review(outcome_hash),
            idempotency_key="cross-request-execution",
        )
        content = runtime.owners.research_memory.accept_idea_outcome_content(
            request_ref=request_a.request_ref,
            run_ref=run_a.run_ref,
            attempt_ref=run_a.attempt_ref,
            fence_ref=run_a.fence_ref,
            submission_ref=execution.submission_ref,
            outcome=execution.outcome,
            review=execution.review,
            execution_receipt=execution.receipt,
        )
        question_content_b = runtime.owners.research_memory.read_question_content(
            question_b.content_ref,
            question_b.content_hash,
        )

        graph = runtime.owners.research_graph
        before = graph.query_snapshot()
        with pytest.raises(
            OwnerConflict,
            match="stage_run_request_binding_invalid",
        ):
            graph.decide_idea_outcome(
                accepted_question=question_b.as_binding(),
                question_content=question_content_b,
                content=content,
                execution_receipt=execution.receipt,
            )
        after = graph.query_snapshot()
        assert after.revision == before.revision
        assert after.facts["idea_outcome_count"] == 0
        assert after.facts["idea_rejection_count"] == 0
        assert graph.query_idea_outcome_decision(execution.submission_ref) is None
    finally:
        runtime.close()


def test_research_graph_rejects_question_restatement_with_unicode_punctuation(
    tmp_path: Path,
) -> None:
    runtime = _runtime(prepare_data_root(tmp_path / "rg-punctuation-anchor"), _IdeaProvider())
    try:
        completed = _confirm_question(runtime)
        question, request, run = _admit_direct_idea_request(
            runtime,
            completed,
            "rg-punctuation-anchor",
        )
        question_content = runtime.owners.research_memory.read_question_content(
            question.content_ref,
            question.content_hash,
        )
        outcome = _bound_outcome(question.question_ref, request.context_pack_ref)
        outcome["candidates"][0]["candidate_key"] = "cosmetic-key"  # type: ignore[index]
        outcome["candidates"][0]["direction"] = (  # type: ignore[index]
            f"  {question_content['title']}！？ "
        )
        execution = _record_direct_execution(
            runtime,
            run_ref=run.run_ref,
            attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref,
            submission_ref="rg-punctuation-anchor-submission",
            native_session_ref="rg-punctuation-anchor-native",
            runtime_binding=run.runtime_binding,
            outcome=outcome,
            reviewed_draft=outcome,
            review=_review(canonical_hash(outcome)),
            idempotency_key="rg-punctuation-anchor-execution",
        )
        content = runtime.owners.research_memory.accept_idea_outcome_content(
            request_ref=request.request_ref,
            run_ref=run.run_ref,
            attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref,
            submission_ref=execution.submission_ref,
            outcome=execution.outcome,
            reviewed_draft=execution.reviewed_draft,
            review=execution.review,
            execution_receipt=execution.receipt,
        )
        decision = runtime.owners.research_graph.decide_idea_outcome(
            accepted_question=question.as_binding(),
            question_content=question_content,
            content=content,
            execution_receipt=execution.receipt,
        )
        assert decision.decision == "rejected"
        assert decision.reason_code == "question_direction_restatement"
    finally:
        runtime.close()


def test_advancement_engine_completes_an_accepted_no_viable_candidate(
    tmp_path: Path,
) -> None:
    runtime = _runtime(prepare_data_root(tmp_path / "nvc-not-completed"), _IdeaProvider())
    try:
        completed = _confirm_question(runtime)
        question, request, run = _admit_direct_idea_request(
            runtime,
            completed,
            "nvc-not-completed",
        )
        outcome = _no_viable_outcome(
            question.question_ref,
            request.context_pack_ref,
        )
        execution = _record_direct_execution(
            runtime,
            run_ref=run.run_ref,
            attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref,
            submission_ref="nvc-not-completed-submission",
            native_session_ref="nvc-not-completed-native",
            runtime_binding=run.runtime_binding,
            outcome=outcome,
            reviewed_draft=outcome,
            review=_review(canonical_hash(outcome)),
            idempotency_key="nvc-not-completed-execution",
        )
        content = runtime.owners.research_memory.accept_idea_outcome_content(
            request_ref=request.request_ref,
            run_ref=run.run_ref,
            attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref,
            submission_ref=execution.submission_ref,
            outcome=execution.outcome,
            reviewed_draft=execution.reviewed_draft,
            review=execution.review,
            execution_receipt=execution.receipt,
        )
        question_content = runtime.owners.research_memory.read_question_content(
            question.content_ref,
            question.content_hash,
        )
        decision = runtime.owners.research_graph.decide_idea_outcome(
            accepted_question=question.as_binding(),
            question_content=question_content,
            content=content,
            execution_receipt=execution.receipt,
        )
        assert decision.decision == "accepted"
        assert decision.outcome_kind == "no_viable_candidate"
        assert decision.outcome_ref is not None
        completion = runtime.owners.agent_runtime.complete_idea_run(
            run_ref=run.run_ref,
            attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref,
            outcome_ref=decision.outcome_ref,
            decision_receipt=decision.receipt,
            idempotency_key="nvc-not-completed-run-completion",
        )
        assert completion.outcome_ref == decision.outcome_ref

        engine = runtime.owners.advancement_engine
        before = engine.query_snapshot()
        commit = engine.commit_idea_stage(
            request_ref=request.request_ref,
            run_ref=run.run_ref,
            outcome_ref=decision.outcome_ref,
            outcome_kind="no_viable_candidate",
            run_completion_receipt=completion.receipt,
            outcome_receipt=decision.receipt,
            idempotency_key="nvc-real-kind-commit",
        )
        assert commit.outcome_kind == "no_viable_candidate"
        assert commit.disposition == "completed"
        with pytest.raises(OwnerConflict, match="idea_outcome_receipt_invalid"):
            engine.commit_idea_stage(
                request_ref=request.request_ref,
                run_ref=run.run_ref,
                outcome_ref=decision.outcome_ref,
                outcome_kind="idea_set",
                run_completion_receipt=completion.receipt,
                outcome_receipt=decision.receipt,
                idempotency_key="nvc-forged-kind-commit",
            )
        after = engine.query_snapshot()
        assert after.revision == before.revision + 1
        assert after.facts["stage_commit_count"] == 1
        assert engine.query_idea_stage_commit(request.request_ref) == commit
    finally:
        runtime.close()


def test_owner_command_ledgers_capture_natural_key_replays(
    tmp_path: Path,
) -> None:
    runtime = _runtime(prepare_data_root(tmp_path / "command-ledgers"), _IdeaProvider())
    try:
        completed = _confirm_question(runtime)
        runtime.idea_stage.start("ledger-stage-start")
        ae = runtime.owners.advancement_engine
        ar = runtime.owners.agent_runtime
        request = ae.query_idea_stage_request(completed["cycle_ref"])
        assert request is not None

        assert ae.ensure_idea_stage_request(
            cycle_ref=request.cycle_ref,
            accepted_question=request.accepted_question,
            context_pack=request.context_pack,
            idempotency_key="ledger-request-natural-replay",
        ).request_ref == request.request_ref
        with pytest.raises(OwnerConflict, match="idempotency_conflict"):
            ae.ensure_idea_stage_request(
                cycle_ref=request.cycle_ref,
                accepted_question=request.accepted_question,
                context_pack={**request.context_pack, "tampered": True},
                idempotency_key="ledger-request-natural-replay",
            )
        with pytest.raises(OwnerConflict, match="idempotency_conflict"):
            ae.commit_idea_stage(
                request_ref=request.request_ref,
                run_ref="run_not_reached",
                outcome_ref="outcome_not_reached",
                outcome_kind="idea_set",
                run_completion_receipt=request.receipt,
                outcome_receipt=request.receipt,
                idempotency_key="ledger-request-natural-replay",
            )

        admitted = ar.query_idea_stage_run(request.request_ref)
        assert admitted is not None
        assert ar.admit_idea_stage(
            request,
            "ledger-admission-natural-replay",
            runtime_binding=admitted.runtime_binding,
        ).run_ref == admitted.run_ref

        assert runtime.idea_stage.process_once()
        assert runtime.idea_stage.process_once()
        executed_run = ar.query_idea_stage_run(request.request_ref)
        assert executed_run is not None and executed_run.execution is not None
        execution = executed_run.execution
        replayed = ar.record_idea_attempt_execution(
            run_ref=executed_run.run_ref,
            attempt_ref=executed_run.attempt_ref,
            fence_ref=executed_run.fence_ref,
            submission_ref=execution.submission_ref,
            native_session_ref=execution.native_session_ref,
            runtime_binding=execution.runtime_binding,
            outcome=execution.outcome,
            reviewed_draft=execution.reviewed_draft,
            review=execution.review,
            idempotency_key="ledger-execution-natural-replay",
        )
        assert replayed.receipt == execution.receipt
        with pytest.raises(OwnerConflict, match="idempotency_conflict"):
            ar.record_idea_attempt_execution(
                run_ref=executed_run.run_ref,
                attempt_ref=executed_run.attempt_ref,
                fence_ref=executed_run.fence_ref,
                submission_ref=execution.submission_ref,
                native_session_ref=execution.native_session_ref,
                runtime_binding=execution.runtime_binding,
                outcome=execution.outcome,
                reviewed_draft=execution.reviewed_draft,
                review=execution.review,
                idempotency_key="ledger-admission-natural-replay",
            )

        for _boundary in range(4):
            assert runtime.idea_stage.process_once()
        commit = ae.query_idea_stage_commit(request.request_ref)
        assert commit is not None
        assert commit.outcome_kind == "idea_set"
        assert commit.disposition == "completed"
        replayed_commit = ae.commit_idea_stage(
            request_ref=commit.request_ref,
            run_ref=commit.run_ref,
            outcome_ref=commit.outcome_ref,
            outcome_kind=commit.outcome_kind,
            run_completion_receipt=commit.run_completion_receipt,
            outcome_receipt=commit.outcome_receipt,
            idempotency_key="ledger-commit-natural-replay",
        )
        assert replayed_commit.receipt == commit.receipt

        with runtime._database.read() as connection:
            ae_keys = {
                row.idempotency_key
                for row in connection.execute(text("SELECT * FROM ae_stage_commands"))
            }
            ar_keys = {
                row.idempotency_key
                for row in connection.execute(text("SELECT * FROM ar_stage_commands"))
            }
        assert {
            "ledger-request-natural-replay",
            "ledger-commit-natural-replay",
        } <= ae_keys
        assert {
            "ledger-admission-natural-replay",
            "ledger-execution-natural-replay",
        } <= ar_keys
    finally:
        runtime.close()


@pytest.mark.parametrize("successor_kind", ["identical", "punctuation", "recommendation"])
def test_rejection_successor_requires_materially_changed_outcome(
    tmp_path: Path,
    successor_kind: str,
) -> None:
    runtime = _runtime(
        prepare_data_root(tmp_path / f"successor-material-change-{successor_kind}"),
        _IdeaProvider(reject_first=True),
    )
    try:
        _confirm_question(runtime)
        runtime.idea_stage.start("successor-stage-start")
        for _boundary in range(5):
            assert runtime.idea_stage.process_once()
        current = runtime.idea_stage.query_current()
        request_ref = current["stage_run_request"]["request_ref"]
        run = runtime.owners.agent_runtime.query_idea_stage_run(request_ref)
        assert run is not None
        assert run.execution is None
        assert run.predecessor_execution is not None
        assert run.rejection_receipt is not None

        successor = deepcopy(run.predecessor_execution.outcome)
        if successor_kind == "punctuation":
            candidate = successor["candidates"][0]  # type: ignore[index]
            candidate["candidate_key"] = "cosmetic-new-identity"  # type: ignore[index]
            candidate["direction"] = f"{candidate['direction']} ！？"  # type: ignore[index]
        elif successor_kind == "recommendation":
            successor["recommendation"] = {
                "note": "只改变 advisory recommendation 文本。",
                "binding": False,
            }
        with pytest.raises(
            OwnerConflict,
            match="attempt_successor_outcome_unchanged",
        ):
            _record_direct_execution(
                runtime,
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                submission_ref=f"{successor_kind}-successor-submission",
                native_session_ref=run.native_session_ref,
                runtime_binding=run.runtime_binding,
                outcome=successor,
                review=_review(canonical_hash(successor)),
                idempotency_key=f"{successor_kind}-successor-execution",
                reviewed_draft=successor,
            )
    finally:
        runtime.close()


def test_completion_read_fails_closed_when_decision_subject_is_not_outcome(
    tmp_path: Path,
) -> None:
    runtime = _runtime(prepare_data_root(tmp_path / "completion-subject"), _IdeaProvider())
    try:
        _confirm_question(runtime)
        runtime.idea_stage.start("completion-subject-start")
        for _boundary in range(5):
            assert runtime.idea_stage.process_once()
        current = runtime.idea_stage.query_current()
        request_ref = current["stage_run_request"]["request_ref"]
        run = runtime.owners.agent_runtime.query_idea_stage_run(request_ref)
        assert run is not None and run.completion is not None

        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE ar_stage_attempts SET decision_receipt_subject_ref = "
                    ":subject_ref WHERE attempt_ref = :attempt_ref"
                ),
                {
                    "subject_ref": "forged_outcome_subject",
                    "attempt_ref": run.attempt_ref,
                },
            )
        with pytest.raises(OwnerConflict, match="run_completion_invalid"):
            runtime.owners.agent_runtime.query_idea_run_completion(run.run_ref)
    finally:
        runtime.close()


def test_host_compute_reader_recovers_only_persisted_observation(
    tmp_path: Path,
) -> None:
    runtime = _runtime(prepare_data_root(tmp_path / "host-reader"), _IdeaProvider())
    try:
        observation = runtime.owners.agent_runtime.observe_host_compute(
            "host-reader-observe"
        )
        reader = create_host_compute_observation_reader(runtime._database)
        assert reader.query_host_compute(observation.snapshot_ref) == observation
        with pytest.raises(OwnerConflict, match="host_compute_snapshot_not_found"):
            reader.query_host_compute("host_snapshot_missing")
    finally:
        runtime.close()
