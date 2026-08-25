from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from meta_research.database import Database
from meta_research.deepfetch import DeepFetchRunRequest, DeepFetchRuntimeBinding
from meta_research.feed import DurableFeed
from meta_research.migration import upgrade_database
from meta_research.owners.agent_runtime import (
    DEEPFETCH_EXECUTION_RECEIPT_KIND,
    REASONING_ATTEMPT_EXECUTION_RECEIPT_KIND,
    REASONING_ATTEMPT_EXECUTION_SCHEMA,
    DeepFetchRun,
)
from meta_research.owners.common import (
    AcceptedQuestionBinding,
    AcceptanceReceipt,
    OwnerConflict,
    QUESTION_PROPOSAL_SCHEMA,
    canonical_hash,
)
from meta_research.owners.research_graph import (
    REASONING_ACCEPTED_RECEIPT_KIND,
    REASONING_REJECTED_RECEIPT_KIND,
    REASONING_SCIENTIFIC_ACCEPTED_RECEIPT_KIND,
    REASONING_SCIENTIFIC_REJECTED_RECEIPT_KIND,
    SQLiteResearchGraph,
    SQLiteResearchGraphReceiptVerifier,
)
from meta_research.owners.research_memory import (
    REASONING_AUTONOMOUS_CHECKPOINT_SCHEMA,
    REASONING_CONTENT_RECEIPT_KIND,
    REASONING_SCIENTIFIC_CANDIDATE_RECEIPT_KIND,
    SQLiteResearchMemory,
    SQLiteResearchMemoryReceiptVerifier,
)
from meta_research.paths import prepare_data_root
from meta_research.reasoning_contract import (
    AUTONOMOUS_QUESTION_SCOPE_SCHEMA_REF,
    CANDIDATE_COMPLETION_SCHEMA_REF,
    NEXT_CYCLE_PROPOSAL_SCHEMA_REF,
    REASONING_REVIEW_SCHEMA_REF,
    REASONING_STAGE_OUTPUT_SCHEMA_REF,
    SCIENTIFIC_OUTCOME_SCHEMA_REF,
)


class _ExecutionVerifier:
    def verify_deepfetch_execution_receipt(self, **values: object) -> None:
        receipt = values["receipt"]
        assert isinstance(receipt, AcceptanceReceipt)
        if (
            receipt.issuer != "agent_runtime"
            or receipt.kind != DEEPFETCH_EXECUTION_RECEIPT_KIND
            or receipt.subject_ref != values["run_ref"]
        ):
            raise OwnerConflict("deepfetch_execution_receipt_invalid")

    def verify_attempt_execution_receipt(self, **values: object) -> str:
        receipt = values["receipt"]
        assert isinstance(receipt, AcceptanceReceipt)
        if (
            receipt.issuer != "agent_runtime"
            or receipt.kind != REASONING_ATTEMPT_EXECUTION_RECEIPT_KIND
            or receipt.subject_ref != values["submission_ref"]
            or not isinstance(values["payload_hash"], str)
        ):
            raise OwnerConflict("attempt_execution_receipt_invalid")
        return str(values["payload_hash"])

    def verify_reasoning_autonomous_checkpoint_receipt(
        self, **values: object
    ) -> None:
        receipt = values["receipt"]
        assert isinstance(receipt, AcceptanceReceipt)
        if (
            receipt.issuer != "agent_runtime"
            or receipt.kind != "reasoning_autonomous_checkpoint"
            or receipt.subject_ref != values["checkpoint_ref"]
            or not isinstance(values["checkpoint_hash"], str)
            or not isinstance(values["review_hash"], str)
        ):
            raise OwnerConflict("reasoning_autonomous_checkpoint_receipt_invalid")


class _StageRequestVerifier:
    def verify_stage_run_request(self, **values: object) -> None:
        receipt = values["receipt"]
        assert isinstance(receipt, AcceptanceReceipt)
        if (
            receipt.issuer != "advancement_engine"
            or receipt.kind != "stage_run_request"
            or receipt.subject_ref != values["request_ref"]
            or not isinstance(values["context_pack_hash"], str)
        ):
            raise OwnerConflict("stage_run_request_invalid")


class _UnusedVerifier:
    pass


class _ConfirmationVerifier:
    def verify_bundle_confirmation(self, **values: object) -> None:
        receipt = values["receipt"]
        assert isinstance(receipt, AcceptanceReceipt)
        if (
            receipt.issuer != "human_collaboration"
            or receipt.kind != "quest_bundle_confirmation"
            or receipt.subject_ref != values["initialization_id"]
        ):
            raise OwnerConflict("quest_confirmation_invalid")


class _DispatchVerifier:
    def __init__(self, receipt: AcceptanceReceipt) -> None:
        self.receipt = receipt

    def verify_autonomous_question_dispatch_eligibility(
        self,
        context_ref: str,
        reasoning_checkpoint_ref: str,
        reasoning_checkpoint_hash: str,
        reasoning_stage_run_request_ref: str,
        foreground_epoch: int,
        content_ref: str,
        content_hash: str,
        receipt: AcceptanceReceipt,
    ) -> None:
        if (
            receipt != self.receipt
            or not context_ref
            or not reasoning_checkpoint_ref
            or len(reasoning_checkpoint_hash) != 64
            or not reasoning_stage_run_request_ref
            or foreground_epoch < 1
            or not content_ref
            or len(content_hash) != 64
        ):
            raise OwnerConflict("autonomous_question_dispatch_invalid")


class _QuestVerifier:
    """Keep the pre-existing synthetic Reasoning fixture, verify all real facts."""

    def __init__(self, receipts: SQLiteResearchGraphReceiptVerifier) -> None:
        self._receipts = receipts

    def verify_accepted_question_binding(
        self, binding: AcceptedQuestionBinding
    ) -> None:
        if binding == _question():
            return
        self._receipts.verify_accepted_question_binding(binding)

    def verify_quest_receipt(self, **values: object) -> None:
        self._receipts.verify_quest_receipt(**values)

    def verify_reasoning_scientific_decision(self, *args, **kwargs) -> None:
        self._receipts.verify_reasoning_scientific_decision(*args, **kwargs)


def _receipt(
    issuer: str,
    kind: str,
    subject_ref: str,
    suffix: str,
) -> AcceptanceReceipt:
    return AcceptanceReceipt(
        issuer=issuer,
        kind=kind,
        receipt_ref=f"{kind}:{suffix}",
        subject_ref=subject_ref,
        payload_hash=canonical_hash(
            {"issuer": issuer, "kind": kind, "subject_ref": subject_ref, "suffix": suffix}
        ),
    )


def _question() -> AcceptedQuestionBinding:
    return AcceptedQuestionBinding(
        initialization_id="initialization:reasoning",
        quest_ref="quest:reasoning",
        question_ref="question:reasoning",
        content_ref="question-content:reasoning",
        content_hash="1" * 64,
        schema_ref="meta-research/formal-question-content/v1",
        content_receipt=_receipt(
            "research_memory",
            "question_content_acceptance",
            "question-content:reasoning",
            "question-content",
        ),
        question_receipt=_receipt(
            "research_graph",
            "root_question_acceptance",
            "question:reasoning",
            "question",
        ),
    )


def _owners(tmp_path: Path):
    data_root = prepare_data_root(tmp_path / "reasoning-owners")
    upgrade_database(data_root.database)
    database = Database(data_root.database)
    feed = DurableFeed(database)
    feed.ensure_initialized()
    execution_verifier = _ExecutionVerifier()
    stage_verifier = _StageRequestVerifier()
    confirmation_verifier = _ConfirmationVerifier()
    rm_receipts = SQLiteResearchMemoryReceiptVerifier(
        database,
        data_root.objects,
        execution_verifier,
        stage_verifier,
    )
    rg_receipts = SQLiteResearchGraphReceiptVerifier(
        database,
        confirmation_verifier,
        rm_receipts,
        _UnusedVerifier(),
        reasoning_content_verifier=rm_receipts,
    )
    rm_receipts.bind_reasoning_scientific_decision_verifier(rg_receipts)
    quest_verifier = _QuestVerifier(rg_receipts)
    memory = SQLiteResearchMemory(
        database,
        data_root.objects,
        feed,
        confirmation_verifier,
        quest_verifier,
        rm_receipts,
        execution_verifier=execution_verifier,
        stage_request_verifier=stage_verifier,
    )
    graph = SQLiteResearchGraph(
        database,
        feed,
        confirmation_verifier,
        rm_receipts,
        _UnusedVerifier(),
        rg_receipts,
        reasoning_content_verifier=rm_receipts,
    )
    return database, memory, rm_receipts, graph, rg_receipts


def _accepted_snapshot(memory: SQLiteResearchMemory):
    question = _question()
    draft = {"question": question.question_ref}
    scope = {"topic": "reasoning evidence"}
    request = DeepFetchRunRequest(
        request_ref="deepfetch-request:reasoning",
        initialization_id=question.initialization_id,
        correlation_ref="deepfetch-correlation:reasoning",
        draft_revision=1,
        draft_hash=canonical_hash(draft),
        draft=draft,
        scope=scope,
        scope_hash=canonical_hash(scope),
        resource_envelope_ref="resource-envelope:reasoning",
        resource_envelope_hash="2" * 64,
        acquisition_session_ref="acquisition-session:reasoning",
        acquisition_config_hash="3" * 64,
        acquisition_runtime_binding_hash="4" * 64,
        accepted_material_bindings=(),
        result_route="first_question",
        authorization_receipt=_receipt(
            "human_collaboration",
            "deepfetch_authorization",
            "deepfetch-request:reasoning",
            "authorization",
        ),
    )
    fulltext = {
        "media_type": "text/plain",
        "content": "The frozen full text supports the bounded claim.",
    }
    result = {
        "schema_ref": "meta-research/first-question-deepfetch-result/v1",
        "request_ref": request.request_ref,
        "initialization_id": request.initialization_id,
        "correlation_ref": request.correlation_ref,
        "draft_revision": request.draft_revision,
        "draft_hash": request.draft_hash,
        "scope_hash": request.scope_hash,
        "completion": "complete",
        "summary": "One accepted full-text record.",
        "papers": [
            {
                "title": "Bounded reasoning evidence",
                "url": "https://example.test/reasoning",
                "doi": "10.1000/reasoning",
                "source_kind": "publisher",
                "fulltext_status": "retrieved",
                "retrieved_at": "2026-08-25T00:00:00Z",
            }
        ],
        "fulltexts": [
            {
                "paper_url": "https://example.test/reasoning",
                **fulltext,
                "content_hash": canonical_hash(fulltext),
            }
        ],
        "limitations": [],
        "native_session_ref": "deepfetch-native:reasoning",
        "adapter_kind": "deterministic-test",
        "web_evidence": [],
    }
    result_hash = canonical_hash(result)
    run_ref = "deepfetch-run:reasoning"
    run = DeepFetchRun(
        request_ref=request.request_ref,
        run_ref=run_ref,
        correlation_ref=request.correlation_ref,
        status="executed",
        attempt_ref="deepfetch-attempt:reasoning",
        attempt_generation=1,
        provider_operation_ref="deepfetch-operation:reasoning",
        provider_operation_generation=1,
        root_session_ref="deepfetch-root:reasoning",
        native_session_ref="deepfetch-native:reasoning",
        fence_ref="deepfetch-fence:reasoning",
        runtime_binding=DeepFetchRuntimeBinding(
            provider_ref="test-provider",
            provider_version="v1",
            model_ref="test-model",
            harness_ref="test-harness",
            capability_bindings=(),
        ),
        runtime_binding_hash="5" * 64,
        result=result,
        result_hash=result_hash,
        execution_receipt=_receipt(
            "agent_runtime",
            DEEPFETCH_EXECUTION_RECEIPT_KIND,
            run_ref,
            "deepfetch-execution",
        ),
        failure_code=None,
    )
    return memory.accept_literature_snapshot(request, run)


def _revision(memory: SQLiteResearchMemory) -> dict[str, object]:
    snapshot = _accepted_snapshot(memory)
    return memory.ensure_question_literature_revision(
        question_binding=_question(),
        source_snapshot_binding=snapshot.as_context_binding(),
        idempotency_key="question-literature:reasoning:v1",
    )


def _context_pack(revision: dict[str, object]) -> dict[str, object]:
    question = _question()
    return {
        "schema_ref": "meta-research/reasoning-context-pack/v1",
        "cycle_ref": "cycle:reasoning",
        "foreground_epoch": 7,
        "accepted_question_binding": question.as_dict(),
        "question_literature_input": {
            "kind": "revision",
            "revision_ref": revision["revision_ref"],
            "binding": revision,
        },
        "upstream_stage_closure": [
            {"stage": "idea", "commit_ref": "stage-commit:idea"},
            {"stage": "plan", "commit_ref": "stage-commit:plan"},
            {"stage": "bundle", "commit_ref": "stage-commit:bundle"},
        ],
        "plan_evidence_input": {
            "kind": "none",
            "basis_stage_commit_refs": [
                "stage-commit:idea", "stage-commit:plan", "stage-commit:bundle"
            ],
        },
        "accepted_target_commit_closures": [],
        "research_context": {
            "schema_ref": "meta-research/reasoning-research-context/v2",
            "cycle_ref": "cycle:reasoning",
            "quest_ref": question.quest_ref,
            "question_ref": question.question_ref,
            "goal_revision_ref": "goal-revision:reasoning",
            "quest_goal_revision": {
                "kind": "QuestGoalRevision",
                "goal_revision_ref": "goal-revision:reasoning",
                "quest_ref": question.quest_ref,
            },
            "graph_binding": {
                "schema_ref": "meta-research/reasoning-graph-context/v1",
                "issuer": "research_graph",
                "quest_ref": question.quest_ref,
                "question_ref": question.question_ref,
                "graph_revision_ref": "graph-revision:reasoning",
                "active_question_refs": [question.question_ref],
                "parent_question_bindings": [],
                "prior_current_question_outcomes": [],
                "binding_ref": "reasoning-graph-context:reasoning",
                "binding_hash": "a" * 64,
            },
            "causal_context": {
                "target_commit_refs": [], "changed_axis_fact_refs": [],
                "held_fixed_fact_refs": [], "provenance_refs": [],
            },
            "upstream_stage_commit_refs": [
                "stage-commit:idea",
                "stage-commit:plan",
                "stage-commit:bundle",
            ],
        },
    }


def _stage_output(
    evidence_ref: str,
    *,
    outcome_ref: str,
    completion: bool = False,
) -> dict[str, object]:
    scientific_outcome: dict[str, object] = {
        "schema_ref": SCIENTIFIC_OUTCOME_SCHEMA_REF,
        "kind": "ScientificOutcomeCandidate",
        "outcome_ref": outcome_ref,
        "stage_run_request_ref": "reasoning-request:1",
        "cycle_ref": "cycle:reasoning",
        "question_ref": "question:reasoning",
        "quest_ref": "quest:reasoning",
        "goal_revision_ref": "goal-revision:reasoning",
        "foreground_epoch": 7,
        "disposition": "affirmed",
        "claim": "The accepted full text supports the bounded claim.",
        "evidence": [
            {"kind": "LiteratureRecord", "ref": evidence_ref, "finding": "supporting"}
        ],
        "missing_evidence": [],
        "uncertainty_basis": [],
        "support_scope": ["The accepted Question within the frozen context."],
        "limitations": ["No inference outside the frozen applicability scope."],
        "causal_interpretation": {
            "target_commit_refs": [], "changed_axis_fact_refs": [],
            "held_fixed_fact_refs": [], "provenance_refs": [],
            "attribution_basis_refs": [evidence_ref],
            "claim_scope": "The bounded accepted literature association.",
            "statement": "The record supports association, not intervention.",
            "sufficiency_rationale": "No causal TargetCommit was frozen.",
            "confounders": ["No controlled intervention was frozen."],
        },
        "research_synthesis": {
            "cycle": {"cycle_ref": "cycle:reasoning", "impact": "One bounded finding."},
            "current_question": {
                "question_ref": "question:reasoning",
                "prior_accepted_outcome_refs": [],
                "progress": "The Question gains bounded support.",
            },
            "parent_questions": [],
            "quest": {
                "quest_ref": "quest:reasoning",
                "goal_revision_ref": "goal-revision:reasoning",
                "graph_revision_ref": "graph-revision:reasoning",
                "impact": "The frozen Goal gains bounded support.",
            },
        },
        "is_authoritative": False,
    }
    source = {
        "source_quest_ref": "quest:reasoning",
        "source_cycle_ref": "cycle:reasoning",
        "source_reasoning_stage_run_request_ref": "reasoning-request:1",
        "source_scientific_outcome_ref": outcome_ref,
        "source_question_ref": "question:reasoning",
        "source_foreground_epoch": 7,
    }
    if completion:
        transition = {
            "schema_ref": CANDIDATE_COMPLETION_SCHEMA_REF,
            "kind": "CandidateCompletion",
            **source,
            "current_quest_ref": "quest:reasoning",
            "current_goal_revision_ref": "goal-revision:reasoning",
            "completion_milestone_basis_refs": [
                "stage-commit:idea",
                "stage-commit:plan",
                "stage-commit:bundle",
            ],
            "rationale": "The bounded goal is satisfied.",
            "is_authoritative": False,
        }
        return {
            "schema_ref": REASONING_STAGE_OUTPUT_SCHEMA_REF,
            "scientific_outcome": scientific_outcome,
            "next_cycle_proposal": None,
            "candidate_completion": transition,
        }
    transition = {
        "schema_ref": NEXT_CYCLE_PROPOSAL_SCHEMA_REF,
        "kind": "NextCycleProposal",
        **source,
        "target_question_ref": "question:reasoning",
        "target_question_anchor_ref": "question-anchor:reasoning",
        "entry_stage": "idea",
        "typed_skip_basis_refs_by_stage": {},
        "is_authoritative": False,
    }
    return {
        "schema_ref": REASONING_STAGE_OUTPUT_SCHEMA_REF,
        "scientific_outcome": scientific_outcome,
        "next_cycle_proposal": transition,
        "candidate_completion": None,
    }


def _review(
    output: dict[str, object],
    *,
    unresolved: bool = False,
) -> dict[str, object]:
    findings = (
        [
            {
                "finding_id": "finding:unresolved",
                "category": "research_synthesis",
                "message": "The synthesis remains semantically ambiguous.",
            }
        ]
        if unresolved
        else []
    )
    dispositions = (
        [
            {
                "finding_id": "finding:unresolved",
                "action": "not_adopted",
                "rationale": "The final output retained the disputed synthesis.",
            }
        ]
        if unresolved
        else []
    )
    return {
        "schema_ref": REASONING_REVIEW_SCHEMA_REF,
        "review_mode": "harness_child_agent",
        "reviewer_agent_ref": "reasoning-child:1",
        "reviewed_draft_hash": canonical_hash(output),
        "findings": findings,
        "dispositions": dispositions,
        "final_output_hash": canonical_hash(output),
        "independent": True,
        "advisory_only": True,
    }


def _revised_review(
    reviewed_draft: dict[str, object], final_output: dict[str, object]
) -> dict[str, object]:
    return {
        "schema_ref": REASONING_REVIEW_SCHEMA_REF,
        "review_mode": "harness_child_agent",
        "reviewer_agent_ref": "reasoning-child:resume",
        "reviewed_draft_hash": canonical_hash(reviewed_draft),
        "findings": [
            {
                "finding_id": "finding:resume-transition",
                "category": "owner_boundary",
                "message": "Close the staged science with one outward transition.",
            }
        ],
        "dispositions": [
            {
                "finding_id": "finding:resume-transition",
                "action": "revised",
                "rationale": "The final output adds the unique accepted transition.",
            }
        ],
        "final_output_hash": canonical_hash(final_output),
        "independent": True,
        "advisory_only": True,
    }


def _accept_content(
    memory: SQLiteResearchMemory,
    revision: dict[str, object],
    *,
    submission_ref: str,
    outcome_ref: str,
    unresolved: bool = False,
    completion: bool = False,
):
    records = revision["records"]
    assert isinstance(records, list) and records
    output = _stage_output(
        str(records[0]["ref"]),
        outcome_ref=outcome_ref,
        completion=completion,
    )
    review = _review(output, unresolved=unresolved)
    context_pack = _context_pack(revision)
    return memory.accept_reasoning_content(
        request_ref="reasoning-request:1",
        cycle_ref="cycle:reasoning",
        foreground_epoch=7,
        context_pack_ref="reasoning-context:1",
        context_pack_hash=canonical_hash(context_pack),
        context_pack=context_pack,
        stage_request_receipt=_receipt(
            "advancement_engine",
            "stage_run_request",
            "reasoning-request:1",
            "reasoning-stage-request",
        ),
        run_ref="reasoning-run:1",
        attempt_ref=f"reasoning-attempt:{submission_ref}",
        fence_ref=f"reasoning-fence:{submission_ref}",
        submission_ref=submission_ref,
        outcome=output,
        reviewed_draft=output,
        review=review,
        execution_receipt=_receipt(
            "agent_runtime",
            REASONING_ATTEMPT_EXECUTION_RECEIPT_KIND,
            submission_ref,
            submission_ref,
        ),
    )


def _accept_real_root_next_cycle_content(
    memory: SQLiteResearchMemory,
    graph: SQLiteResearchGraph,
    *,
    submission_ref: str,
    outcome_ref: str,
):
    initialization_id = f"initialization:{submission_ref}"
    draft = {"goal": "Continue the accepted root Question."}
    draft_hash = canonical_hash(draft)
    question_content = {
        "title": "A real issuer-owned root Question",
        "unknown_statement": "Which bounded result remains unresolved?",
        "answer_shape": "A comparison with explicit evidence boundaries.",
        "applicability_scope": "The accepted local research scope.",
        "background_context": "The current root Question remains open.",
        "requirements_constraints": "Use only accepted evidence.",
    }
    proposal = {
        "schema_ref": QUESTION_PROPOSAL_SCHEMA,
        "basis_revision": 1,
        "basis_hash": draft_hash,
        "content": question_content,
    }
    proposal_hash = canonical_hash(proposal)
    confirmation = _receipt(
        "human_collaboration",
        "quest_bundle_confirmation",
        initialization_id,
        submission_ref,
    )
    quest = graph.accept_quest(
        initialization_id=initialization_id,
        draft=draft,
        draft_revision=1,
        draft_hash=draft_hash,
        proposal_ref=f"proposal:{submission_ref}",
        proposal_hash=proposal_hash,
        preview_ref=f"preview:{submission_ref}",
        preview_hash=canonical_hash({"preview": submission_ref}),
        confirmation=confirmation,
    )
    content = memory.accept_question_content(
        initialization_id=initialization_id,
        quest=quest,
        content=question_content,
        content_hash=canonical_hash(question_content),
    )
    question = graph.accept_root_question(
        initialization_id=initialization_id,
        quest=quest,
        content_ref=content.content_ref,
        content_hash=content.content_hash,
        schema_ref=content.schema_ref,
        content_receipt=content.receipt,
    )
    goal_revision = graph.query_current_quest_goal_revision(quest.quest_ref)
    assert goal_revision is not None
    graph_binding = graph.query_reasoning_research_context(
        quest_ref=quest.quest_ref,
        question_ref=question.question_ref,
    )
    assert graph_binding is not None
    request_ref = f"reasoning-request:{submission_ref}"
    cycle_ref = f"cycle:{submission_ref}"
    context_pack = {
        "schema_ref": "meta-research/reasoning-context-pack/v1",
        "cycle_ref": cycle_ref,
        "foreground_epoch": 7,
        "accepted_question_binding": question.as_binding().as_dict(),
        "question_literature_input": {"kind": "none"},
        "upstream_stage_closure": [
            {"stage": "idea", "commit_ref": f"stage-commit:{submission_ref}:idea"},
            {"stage": "plan", "commit_ref": f"stage-commit:{submission_ref}:plan"},
            {"stage": "bundle", "commit_ref": f"stage-commit:{submission_ref}:bundle"},
        ],
        "plan_evidence_input": {
            "kind": "none",
            "basis_stage_commit_refs": [
                f"stage-commit:{submission_ref}:idea",
                f"stage-commit:{submission_ref}:plan",
                f"stage-commit:{submission_ref}:bundle",
            ],
        },
        "accepted_target_commit_closures": [],
        "research_context": {
            "schema_ref": "meta-research/reasoning-research-context/v2",
            "cycle_ref": cycle_ref,
            "quest_ref": quest.quest_ref,
            "question_ref": question.question_ref,
            "goal_revision_ref": goal_revision["goal_revision_ref"],
            "quest_goal_revision": goal_revision,
            "graph_binding": graph_binding,
            "causal_context": {
                "target_commit_refs": [], "changed_axis_fact_refs": [],
                "held_fixed_fact_refs": [], "provenance_refs": [],
            },
            "upstream_stage_commit_refs": [
                f"stage-commit:{submission_ref}:idea",
                f"stage-commit:{submission_ref}:plan",
                f"stage-commit:{submission_ref}:bundle",
            ],
        },
    }
    scientific_outcome = {
        "schema_ref": SCIENTIFIC_OUTCOME_SCHEMA_REF,
        "kind": "ScientificOutcomeCandidate",
        "outcome_ref": outcome_ref,
        "stage_run_request_ref": request_ref,
        "cycle_ref": cycle_ref,
        "question_ref": question.question_ref,
        "quest_ref": quest.quest_ref,
        "goal_revision_ref": goal_revision["goal_revision_ref"],
        "foreground_epoch": 7,
        "disposition": "insufficient_evidence",
        "claim": None,
        "evidence": [],
        "missing_evidence": ["The accepted root Question needs another Cycle."],
        "uncertainty_basis": [],
        "support_scope": ["The accepted Question within the frozen context."],
        "limitations": ["No substantive evidence was frozen."],
        "causal_interpretation": {
            "target_commit_refs": [], "changed_axis_fact_refs": [],
            "held_fixed_fact_refs": [], "provenance_refs": [],
            "attribution_basis_refs": [], "claim_scope": "No causal claim.",
            "statement": "No causal interpretation is made.",
            "sufficiency_rationale": "Substantive evidence is missing.",
            "confounders": [],
        },
        "research_synthesis": {
            "cycle": {"cycle_ref": cycle_ref, "impact": "Evidence remains missing."},
            "current_question": {
                "question_ref": question.question_ref,
                "prior_accepted_outcome_refs": [
                    item["outcome_ref"]
                    for item in graph_binding["prior_current_question_outcomes"]
                ],
                "progress": "The missing evidence is now explicit.",
            },
            "parent_questions": [
                {"question_ref": item["question_ref"], "impact": "unknown", "statement": "No parent impact is supported."}
                for item in graph_binding["parent_question_bindings"]
            ],
            "quest": {
                "quest_ref": quest.quest_ref,
                "goal_revision_ref": goal_revision["goal_revision_ref"],
                "graph_revision_ref": graph_binding["graph_revision_ref"],
                "impact": "The frozen Goal remains open.",
            },
        },
        "is_authoritative": False,
    }
    source = {
        "source_quest_ref": quest.quest_ref,
        "source_cycle_ref": cycle_ref,
        "source_reasoning_stage_run_request_ref": request_ref,
        "source_scientific_outcome_ref": outcome_ref,
        "source_question_ref": question.question_ref,
        "source_foreground_epoch": 7,
    }
    output = {
        "schema_ref": REASONING_STAGE_OUTPUT_SCHEMA_REF,
        "scientific_outcome": scientific_outcome,
        "next_cycle_proposal": {
            "schema_ref": NEXT_CYCLE_PROPOSAL_SCHEMA_REF,
            "kind": "NextCycleProposal",
            **source,
            "target_question_ref": question.question_ref,
            "target_question_anchor_ref": question.question_ref,
            "entry_stage": "idea",
            "typed_skip_basis_refs_by_stage": {},
            "is_authoritative": False,
        },
        "candidate_completion": None,
    }
    return memory.accept_reasoning_content(
        request_ref=request_ref,
        cycle_ref=cycle_ref,
        foreground_epoch=7,
        context_pack_ref=f"reasoning-context:{submission_ref}",
        context_pack_hash=canonical_hash(context_pack),
        context_pack=context_pack,
        stage_request_receipt=_receipt(
            "advancement_engine",
            "stage_run_request",
            request_ref,
            submission_ref,
        ),
        run_ref=f"reasoning-run:{submission_ref}",
        attempt_ref=f"reasoning-attempt:{submission_ref}",
        fence_ref=f"reasoning-fence:{submission_ref}",
        submission_ref=submission_ref,
        outcome=output,
        reviewed_draft=output,
        review=_review(output),
        execution_receipt=_receipt(
            "agent_runtime",
            REASONING_ATTEMPT_EXECUTION_RECEIPT_KIND,
            submission_ref,
            submission_ref,
        ),
    )


def _autonomous_scope(
    scientific_outcome: dict[str, object],
) -> dict[str, object]:
    return {
        "schema_ref": AUTONOMOUS_QUESTION_SCOPE_SCHEMA_REF,
        "kind": "AutonomousQuestionScope",
        "creation_mode": "AutonomousCreation",
        "mode": "new",
        "source_quest_ref": scientific_outcome["quest_ref"],
        "source_cycle_ref": scientific_outcome["cycle_ref"],
        "source_reasoning_stage_run_request_ref": scientific_outcome[
            "stage_run_request_ref"
        ],
        "source_scientific_outcome_ref": scientific_outcome["outcome_ref"],
        "source_question_ref": scientific_outcome["question_ref"],
        "source_foreground_epoch": scientific_outcome["foreground_epoch"],
        "question_blueprint": {
            "title": "A bounded autonomous follow-up",
            "unknown_statement": "Whether the bounded result transfers is unknown.",
            "answer_shape": "A comparison with explicit counterexamples.",
            "applicability_scope": "Two authorized public data domains.",
            "background_context": "The source result covered one domain.",
            "requirements_constraints": "Preserve labels and report domain shift.",
        },
        "parent_question_ref": None,
        "decomposition_basis_refs": [],
        "entry_stage": "idea",
        "typed_skip_basis_refs_by_stage": {},
        "is_authoritative": False,
    }


def _accept_scientific_candidate(
    memory: SQLiteResearchMemory,
    revision: dict[str, object],
    *,
    submission_ref: str,
    outcome_ref: str,
    unresolved: bool = False,
):
    records = revision["records"]
    assert isinstance(records, list) and records
    scientific_outcome = _stage_output(
        str(records[0]["ref"]),
        outcome_ref=outcome_ref,
    )["scientific_outcome"]
    assert isinstance(scientific_outcome, dict)
    checkpoint = {
        "schema_ref": REASONING_AUTONOMOUS_CHECKPOINT_SCHEMA,
        "scientific_outcome": scientific_outcome,
        "autonomous_scope": _autonomous_scope(scientific_outcome),
    }
    review = _review(checkpoint, unresolved=unresolved)
    context_pack = _context_pack(revision)
    checkpoint_ref = f"reasoning-checkpoint:{submission_ref}"
    return memory.accept_reasoning_scientific_candidate(
        request_ref="reasoning-request:1",
        cycle_ref="cycle:reasoning",
        foreground_epoch=7,
        context_pack_ref="reasoning-context:1",
        context_pack_hash=canonical_hash(context_pack),
        context_pack=context_pack,
        stage_request_receipt=_receipt(
            "advancement_engine",
            "stage_run_request",
            "reasoning-request:1",
            "reasoning-stage-request",
        ),
        run_ref="reasoning-run:1",
        attempt_ref=f"reasoning-attempt:{submission_ref}",
        fence_ref=f"reasoning-fence:{submission_ref}",
        submission_ref=submission_ref,
        checkpoint_ref=checkpoint_ref,
        checkpoint=checkpoint,
        review=review,
        checkpoint_receipt=_receipt(
            "agent_runtime",
            "reasoning_autonomous_checkpoint",
            checkpoint_ref,
            submission_ref,
        ),
    )


def _accepted_autonomous_snapshot(
    memory: SQLiteResearchMemory,
    candidate,
    *,
    context_ref: str,
):
    blueprint = candidate.autonomous_scope["question_blueprint"]
    scope = {
        "schema_ref": "meta-research/autonomous-question-deepfetch-scope/v1",
        "context_ref": context_ref,
        "generation": 1,
        "reasoning_checkpoint_ref": candidate.checkpoint_ref,
        "reasoning_checkpoint_hash": candidate.checkpoint_hash,
        "source_scientific_outcome_ref": candidate.scientific_outcome_ref,
        "question_blueprint": blueprint,
    }
    basis_hash = canonical_hash(
        {
            "reasoning_checkpoint_ref": candidate.checkpoint_ref,
            "reasoning_checkpoint_hash": candidate.checkpoint_hash,
            "source_scientific_outcome_ref": candidate.scientific_outcome_ref,
            "autonomous_scope_hash": candidate.autonomous_scope_hash,
        }
    )
    request = DeepFetchRunRequest(
        request_ref="deepfetch-request:autonomous",
        initialization_id="initialization:autonomous",
        correlation_ref=context_ref,
        draft_revision=1,
        draft_hash=canonical_hash(blueprint),
        draft=blueprint,
        scope=scope,
        scope_hash=canonical_hash(scope),
        resource_envelope_ref="resource-envelope:autonomous",
        resource_envelope_hash="a" * 64,
        acquisition_session_ref="acquisition-session:autonomous",
        acquisition_config_hash="b" * 64,
        acquisition_runtime_binding_hash="c" * 64,
        accepted_material_bindings=(),
        result_route="same_autonomous_question_creation",
        authorization_receipt=_receipt(
            "advancement_engine",
            "autonomous_deepfetch_eligibility",
            "deepfetch-request:autonomous",
            "autonomous-authorization",
        ),
        creation_context_kind="autonomous_question_creation",
        creation_context_ref=context_ref,
        context_generation=1,
        quest_ref=candidate.scientific_outcome["quest_ref"],
        context_basis_hash=basis_hash,
    )
    fulltext = {
        "media_type": "text/plain",
        "content": "The follow-up literature establishes a bounded comparison.",
    }
    result = {
        "schema_ref": "meta-research/first-question-deepfetch-result/v1",
        "request_ref": request.request_ref,
        "initialization_id": request.initialization_id,
        "correlation_ref": request.correlation_ref,
        "draft_revision": request.draft_revision,
        "draft_hash": request.draft_hash,
        "scope_hash": request.scope_hash,
        "completion": "complete",
        "summary": "Autonomous follow-up literature.",
        "papers": [
            {
                "title": "Autonomous follow-up evidence",
                "url": "https://example.test/autonomous",
                "doi": "10.1000/autonomous",
                "source_kind": "publisher",
                "fulltext_status": "retrieved",
                "retrieved_at": "2026-08-25T00:00:00Z",
            }
        ],
        "fulltexts": [
            {
                "paper_url": "https://example.test/autonomous",
                **fulltext,
                "content_hash": canonical_hash(fulltext),
            }
        ],
        "limitations": [],
        "native_session_ref": "deepfetch-native:autonomous",
        "adapter_kind": "deterministic-test",
        "web_evidence": [],
    }
    run_ref = "deepfetch-run:autonomous"
    run = DeepFetchRun(
        request_ref=request.request_ref,
        run_ref=run_ref,
        correlation_ref=request.correlation_ref,
        status="executed",
        attempt_ref="deepfetch-attempt:autonomous",
        attempt_generation=1,
        provider_operation_ref="deepfetch-operation:autonomous",
        provider_operation_generation=1,
        root_session_ref="deepfetch-root:autonomous",
        native_session_ref="deepfetch-native:autonomous",
        fence_ref="deepfetch-fence:autonomous",
        runtime_binding=DeepFetchRuntimeBinding(
            provider_ref="test-provider",
            provider_version="v1",
            model_ref="test-model",
            harness_ref="test-harness",
            capability_bindings=(),
        ),
        runtime_binding_hash="d" * 64,
        result=result,
        result_hash=canonical_hash(result),
        execution_receipt=_receipt(
            "agent_runtime",
            DEEPFETCH_EXECUTION_RECEIPT_KIND,
            run_ref,
            "autonomous-deepfetch-execution",
        ),
        failure_code=None,
    )
    return memory.accept_literature_snapshot(request, run)
def test_question_literature_revision_is_an_independent_verified_fact(
    tmp_path: Path,
) -> None:
    database, memory, receipts, _graph, _graph_receipts = _owners(tmp_path)
    try:
        snapshot = _accepted_snapshot(memory)
        revision = memory.ensure_question_literature_revision(
            question_binding=_question(),
            source_snapshot_binding=snapshot.as_context_binding(),
            idempotency_key="question-literature:reasoning:v1",
        )

        assert set(revision) == {
            "kind",
            "revision_ref",
            "question_ref",
            "literature_snapshot_ref",
            "records",
            "rm_acceptance_receipt_ref",
            "rg_question_association_receipt_ref",
            "receipt",
        }
        assert revision["kind"] == "QuestionLiteratureRevision"
        assert revision["revision_ref"] != snapshot.snapshot_ref
        assert revision["literature_snapshot_ref"] == snapshot.snapshot_ref
        assert revision["records"] == [
            {
                "ref": "doi:10.1000/reasoning",
                "evidence_basis": "verified_fulltext",
                "evidence_basis_ref": canonical_hash(
                    {
                        "media_type": "text/plain",
                        "content": "The frozen full text supports the bounded claim.",
                    }
                ),
            }
        ]
        assert revision["rm_acceptance_receipt_ref"]
        assert revision["receipt"]["issuer"] == "research_memory"
        assert revision["receipt"]["receipt_ref"] == revision[
            "rm_acceptance_receipt_ref"
        ]
        assert revision["rg_question_association_receipt_ref"] == (
            _question().question_receipt.receipt_ref
        )
        assert receipts.query_current_question_literature_revision(
            "question:reasoning"
        ) == revision
        receipts.verify_question_literature_revision(revision)
        assert memory.query_snapshot().facts["question_literature_revision_count"] == 1
    finally:
        database.close()


def test_rm_binds_exact_reasoning_execution_and_exactly_one_transition(
    tmp_path: Path,
) -> None:
    database, memory, receipts, _graph, _graph_receipts = _owners(tmp_path)
    try:
        revision = _revision(memory)
        accepted = _accept_content(
            memory,
            revision,
            submission_ref="reasoning-submission:accepted",
            outcome_ref="scientific-outcome:accepted",
        )

        assert accepted.scientific_outcome["kind"] == "ScientificOutcomeCandidate"
        assert accepted.transition_kind == "next_cycle_proposal"
        assert accepted.transition == accepted.outcome["next_cycle_proposal"]
        assert accepted.receipt.kind == REASONING_CONTENT_RECEIPT_KIND
        assert memory.query_reasoning_content(accepted.submission_ref) == accepted
        receipts.verify_reasoning_content_receipt(
            request_ref=accepted.request_ref,
            submission_ref=accepted.submission_ref,
            content_ref=accepted.content_ref,
            payload_hash=accepted.payload_hash,
            outcome_hash=accepted.outcome_hash,
            transition_hash=accepted.transition_hash,
            reviewed_draft_hash=accepted.reviewed_draft_hash,
            review_hash=accepted.review_hash,
            receipt=accepted.receipt,
        )

        dual = dict(accepted.outcome)
        dual["candidate_completion"] = _stage_output(
            str(revision["records"][0]["ref"]),
            outcome_ref="scientific-outcome:dual",
            completion=True,
        )["candidate_completion"]
        with pytest.raises(OwnerConflict, match="reasoning_transition_xor_invalid"):
            memory.accept_reasoning_content(
                request_ref="reasoning-request:1",
                cycle_ref="cycle:reasoning",
                foreground_epoch=7,
                context_pack_ref="reasoning-context:1",
                context_pack_hash=canonical_hash(_context_pack(revision)),
                context_pack=_context_pack(revision),
                stage_request_receipt=_receipt(
                    "advancement_engine",
                    "stage_run_request",
                    "reasoning-request:1",
                    "reasoning-stage-request",
                ),
                run_ref="reasoning-run:1",
                attempt_ref="reasoning-attempt:dual",
                fence_ref="reasoning-fence:dual",
                submission_ref="reasoning-submission:dual",
                outcome=dual,
                reviewed_draft=dual,
                review=_review(dual),
                execution_receipt=_receipt(
                    "agent_runtime",
                    REASONING_ATTEMPT_EXECUTION_RECEIPT_KIND,
                    "reasoning-submission:dual",
                    "reasoning-submission:dual",
                ),
            )

        forged = replace(
            accepted.execution_receipt,
            issuer="forged-runtime",
        )
        with pytest.raises(OwnerConflict, match="attempt_execution_receipt_invalid"):
            memory.accept_reasoning_content(
                request_ref=accepted.request_ref,
                cycle_ref=accepted.cycle_ref,
                foreground_epoch=accepted.foreground_epoch,
                context_pack_ref=accepted.context_pack_ref,
                context_pack_hash=accepted.context_pack_hash,
                context_pack=accepted.context_pack,
                stage_request_receipt=accepted.stage_request_receipt,
                run_ref=accepted.run_ref,
                attempt_ref="reasoning-attempt:forged",
                fence_ref="reasoning-fence:forged",
                submission_ref="reasoning-submission:forged",
                outcome=accepted.outcome,
                reviewed_draft=accepted.reviewed_draft,
                review=accepted.review,
                execution_receipt=forged,
            )
        assert memory.query_snapshot().facts["reasoning_content_count"] == 1
    finally:
        database.close()


def test_rm_rejects_completion_outside_stage_request_frozen_milestone_basis(
    tmp_path: Path,
) -> None:
    database, memory, _receipts, _graph, _graph_receipts = _owners(tmp_path)
    try:
        revision = _revision(memory)
        records = revision["records"]
        assert isinstance(records, list) and records
        context_pack = _context_pack(revision)
        context_pack["upstream_stage_closure"] = [
            {"stage": "idea", "commit_ref": "stage-commit:idea"},
            {"stage": "plan", "commit_ref": "stage-commit:plan"},
            {"stage": "bundle", "commit_ref": "stage-commit:bundle"},
        ]
        output = _stage_output(
            str(records[0]["ref"]),
            outcome_ref="scientific-outcome:forged-completion-basis",
            completion=True,
        )
        completion = output["candidate_completion"]
        assert isinstance(completion, dict)
        completion["completion_milestone_basis_refs"] = ["stage-commit:forged"]

        with pytest.raises(
            OwnerConflict,
            match="candidate_completion_basis_invalid",
        ):
            memory.accept_reasoning_content(
                request_ref="reasoning-request:1",
                cycle_ref="cycle:reasoning",
                foreground_epoch=7,
                context_pack_ref="reasoning-context:forged-basis",
                context_pack_hash=canonical_hash(context_pack),
                context_pack=context_pack,
                stage_request_receipt=_receipt(
                    "advancement_engine",
                    "stage_run_request",
                    "reasoning-request:1",
                    "reasoning-stage-request",
                ),
                run_ref="reasoning-run:forged-basis",
                attempt_ref="reasoning-attempt:forged-basis",
                fence_ref="reasoning-fence:forged-basis",
                submission_ref="reasoning-submission:forged-basis",
                outcome=output,
                reviewed_draft=output,
                review=_review(output),
                execution_receipt=_receipt(
                    "agent_runtime",
                    REASONING_ATTEMPT_EXECUTION_RECEIPT_KIND,
                    "reasoning-submission:forged-basis",
                    "forged-basis",
                ),
            )
    finally:
        database.close()


def test_rm_refuses_a_literature_snapshot_disguised_as_a_revision(
    tmp_path: Path,
) -> None:
    database, memory, _receipts, _graph, _graph_receipts = _owners(tmp_path)
    try:
        snapshot = _accepted_snapshot(memory)
        forged_revision = snapshot.as_context_binding()
        context_pack = _context_pack(
            {
                "kind": "QuestionLiteratureRevision",
                "revision_ref": snapshot.snapshot_ref,
                "question_ref": "question:reasoning",
                "literature_snapshot_ref": snapshot.snapshot_ref,
                "records": [],
                "rm_acceptance_receipt_ref": snapshot.receipt.receipt_ref,
                "rg_question_association_receipt_ref": (
                    _question().question_receipt.receipt_ref
                ),
            }
        )
        context_pack["question_literature_input"] = {
            "kind": "revision",
            "revision_ref": snapshot.snapshot_ref,
            "binding": forged_revision,
        }
        output = _stage_output(
            "nonexistent-record",
            outcome_ref="scientific-outcome:forged-revision",
        )
        with pytest.raises(OwnerConflict, match="question_literature_revision_invalid"):
            memory.accept_reasoning_content(
                request_ref="reasoning-request:1",
                cycle_ref="cycle:reasoning",
                foreground_epoch=7,
                context_pack_ref="reasoning-context:forged",
                context_pack_hash=canonical_hash(context_pack),
                context_pack=context_pack,
                stage_request_receipt=_receipt(
                    "advancement_engine",
                    "stage_run_request",
                    "reasoning-request:1",
                    "reasoning-stage-request",
                ),
                run_ref="reasoning-run:forged",
                attempt_ref="reasoning-attempt:forged-revision",
                fence_ref="reasoning-fence:forged-revision",
                submission_ref="reasoning-submission:forged-revision",
                outcome=output,
                reviewed_draft=output,
                review=_review(output),
                execution_receipt=_receipt(
                    "agent_runtime",
                    REASONING_ATTEMPT_EXECUTION_RECEIPT_KIND,
                    "reasoning-submission:forged-revision",
                    "forged-revision",
                ),
            )
    finally:
        database.close()


def test_rg_persists_accepted_and_rejected_reasoning_decisions(
    tmp_path: Path,
) -> None:
    database, memory, _receipts, graph, graph_receipts = _owners(tmp_path)
    try:
        revision = _revision(memory)
        rejected_content = _accept_content(
            memory,
            revision,
            submission_ref="reasoning-submission:rejected",
            outcome_ref="scientific-outcome:rejected",
            unresolved=True,
        )
        rejected = graph.decide_reasoning_outcome(content=rejected_content)
        assert rejected.decision == "rejected"
        assert rejected.outcome_ref is None
        assert rejected.receipt.kind == REASONING_REJECTED_RECEIPT_KIND
        graph_receipts.verify_reasoning_outcome_decision(
            request_ref=rejected.request_ref,
            submission_ref=rejected.submission_ref,
            decision="rejected",
            outcome_ref=None,
            receipt=rejected.receipt,
        )

        accepted_content = _accept_real_root_next_cycle_content(
            memory,
            graph,
            submission_ref="reasoning-submission:accepted",
            outcome_ref="scientific-outcome:accepted",
        )
        accepted = graph.decide_reasoning_outcome(content=accepted_content)
        assert accepted.decision == "accepted"
        assert accepted.outcome_ref == "scientific-outcome:accepted"
        assert accepted.receipt.kind == REASONING_ACCEPTED_RECEIPT_KIND
        assert graph.query_reasoning_outcome_decision(accepted.submission_ref) == accepted
        graph_receipts.verify_reasoning_outcome_decision(
            request_ref=accepted.request_ref,
            submission_ref=accepted.submission_ref,
            decision="accepted",
            outcome_ref=accepted.outcome_ref,
            receipt=accepted.receipt,
        )
        transition = graph_receipts.query_reasoning_transition_binding(
            accepted.outcome_ref,
            accepted.receipt,
        )
        assert transition == {
            "scientific_disposition": (
                accepted_content.scientific_outcome["disposition"]
            ),
            "scientific_outcome_hash": accepted_content.outcome_hash,
            "transition_kind": accepted_content.transition_kind,
            "transition_ref": accepted_content.transition_ref,
            "transition_hash": accepted_content.transition_hash,
            "transition": accepted_content.transition,
        }
        facts = graph.query_snapshot().facts
        assert facts["reasoning_outcome_count"] == 1
        assert facts["reasoning_rejection_count"] == 1

        forged = replace(
            accepted_content,
            receipt=_receipt(
                "research_memory",
                REASONING_CONTENT_RECEIPT_KIND,
                accepted_content.content_ref,
                "forged-content",
            ),
        )
        with pytest.raises(OwnerConflict, match="reasoning_content_receipt_invalid"):
            graph.decide_reasoning_outcome(content=forged)
    finally:
        database.close()


def test_rg_refuses_next_cycle_target_without_issuer_owned_question_facts(
    tmp_path: Path,
) -> None:
    database, memory, _receipts, graph, _graph_receipts = _owners(tmp_path)
    try:
        revision = _revision(memory)
        content = _accept_content(
            memory,
            revision,
            submission_ref="reasoning-submission:unregistered-target",
            outcome_ref="scientific-outcome:unregistered-target",
        )

        with pytest.raises(
            OwnerConflict,
            match="reasoning_next_cycle_selection_facts_unavailable",
        ):
            graph.decide_reasoning_outcome(content=content)
    finally:
        database.close()


def test_rg_candidate_completion_rejects_goal_outside_frozen_reasoning_lineage(
    tmp_path: Path,
) -> None:
    database, memory, _receipts, graph, _graph_receipts = _owners(tmp_path)
    try:
        draft = {"goal": "Close only the exact frozen completion basis."}
        quest = graph.accept_quest(
            initialization_id="initialization:completion-lineage",
            draft=draft,
            draft_revision=1,
            draft_hash=canonical_hash(draft),
            proposal_ref="proposal:completion-lineage",
            proposal_hash=canonical_hash({"proposal": "completion-lineage"}),
            preview_ref="preview:completion-lineage",
            preview_hash=canonical_hash({"preview": "completion-lineage"}),
            confirmation=_receipt(
                "human_collaboration",
                "quest_bundle_confirmation",
                "initialization:completion-lineage",
                "completion-lineage",
            ),
        )
        current_goal = graph.query_current_quest_goal_revision(quest.quest_ref)
        assert current_goal is not None

        revision = _revision(memory)
        records = revision["records"]
        assert isinstance(records, list) and records
        context_pack = _context_pack(revision)
        accepted_question = context_pack["accepted_question_binding"]
        research_context = context_pack["research_context"]
        assert isinstance(accepted_question, dict)
        assert isinstance(research_context, dict)
        accepted_question["quest_ref"] = quest.quest_ref
        research_context["quest_ref"] = quest.quest_ref
        research_context["goal_revision_ref"] = "goal-revision:frozen-stale"
        research_context["quest_goal_revision"] = {
            "kind": "QuestGoalRevision",
            "goal_revision_ref": "goal-revision:frozen-stale",
            "quest_ref": quest.quest_ref,
        }
        context_pack["upstream_stage_closure"] = [
            {"stage": "idea", "commit_ref": "stage-commit:idea"},
            {"stage": "plan", "commit_ref": "stage-commit:plan"},
            {"stage": "bundle", "commit_ref": "stage-commit:bundle"},
        ]

        output = _stage_output(
            str(records[0]["ref"]),
            outcome_ref="scientific-outcome:completion-lineage",
            completion=True,
        )
        scientific_outcome = output["scientific_outcome"]
        completion = output["candidate_completion"]
        assert isinstance(scientific_outcome, dict)
        assert isinstance(completion, dict)
        scientific_outcome["quest_ref"] = quest.quest_ref
        scientific_outcome["goal_revision_ref"] = current_goal[
            "goal_revision_ref"
        ]
        completion["source_quest_ref"] = quest.quest_ref
        completion["current_quest_ref"] = quest.quest_ref
        completion["current_goal_revision_ref"] = current_goal[
            "goal_revision_ref"
        ]
        completion["completion_milestone_basis_refs"] = [
            "stage-commit:idea",
            "stage-commit:plan",
            "stage-commit:bundle",
        ]
        with pytest.raises(
            OwnerConflict,
            match="reasoning_research_context_invalid",
        ):
            memory.accept_reasoning_content(
                request_ref="reasoning-request:1",
                cycle_ref="cycle:reasoning",
                foreground_epoch=7,
                context_pack_ref="reasoning-context:completion-lineage",
                context_pack_hash=canonical_hash(context_pack),
                context_pack=context_pack,
                stage_request_receipt=_receipt(
                    "advancement_engine", "stage_run_request",
                    "reasoning-request:1", "completion-lineage",
                ),
                run_ref="reasoning-run:completion-lineage",
                attempt_ref="reasoning-attempt:completion-lineage",
                fence_ref="reasoning-fence:completion-lineage",
                submission_ref="reasoning-submission:completion-lineage",
                outcome=output,
                reviewed_draft=output,
                review=_review(output),
                execution_receipt=_receipt(
                    "agent_runtime", REASONING_ATTEMPT_EXECUTION_RECEIPT_KIND,
                    "reasoning-submission:completion-lineage", "completion-lineage",
                ),
            )
    finally:
        database.close()


def test_autonomous_science_is_accepted_before_the_unique_final_transition(
    tmp_path: Path,
) -> None:
    database, memory, receipts, graph, graph_receipts = _owners(tmp_path)
    try:
        revision = _revision(memory)
        rejected_content = _accept_scientific_candidate(
            memory,
            revision,
            submission_ref="reasoning-science:rejected",
            outcome_ref="scientific-outcome:staged-rejected",
            unresolved=True,
        )
        assert rejected_content.receipt.kind == (
            REASONING_SCIENTIFIC_CANDIDATE_RECEIPT_KIND
        )
        rejected = graph.decide_reasoning_scientific_candidate(
            content=rejected_content
        )
        assert rejected.decision == "rejected"
        assert rejected.outcome_ref is None
        assert rejected.receipt.kind == REASONING_SCIENTIFIC_REJECTED_RECEIPT_KIND

        candidate = _accept_scientific_candidate(
            memory,
            revision,
            submission_ref="reasoning-science:accepted",
            outcome_ref="scientific-outcome:staged-accepted",
        )
        assert memory.query_reasoning_scientific_candidate_by_outcome_ref(
            candidate.scientific_outcome_ref
        ) == candidate
        receipts.verify_reasoning_scientific_candidate_receipt(
            request_ref=candidate.request_ref,
            submission_ref=candidate.submission_ref,
            content_ref=candidate.content_ref,
            checkpoint_ref=candidate.checkpoint_ref,
            checkpoint_hash=candidate.checkpoint_hash,
            outcome_hash=candidate.outcome_hash,
            autonomous_scope_hash=candidate.autonomous_scope_hash,
            review_hash=candidate.review_hash,
            receipt=candidate.receipt,
        )
        scientific = graph.decide_reasoning_scientific_candidate(
            content=candidate
        )
        assert scientific.decision == "accepted"
        assert scientific.outcome_ref == candidate.scientific_outcome_ref
        assert scientific.receipt.kind == REASONING_SCIENTIFIC_ACCEPTED_RECEIPT_KIND
        assert graph.query_reasoning_scientific_decision_by_outcome_ref(
            candidate.scientific_outcome_ref
        ) == scientific
        graph_receipts.verify_reasoning_scientific_decision(
            request_ref=candidate.request_ref,
            submission_ref=candidate.submission_ref,
            decision="accepted",
            outcome_ref=candidate.scientific_outcome_ref,
            receipt=scientific.receipt,
        )

        records = revision["records"]
        assert isinstance(records, list) and records
        final_output = _stage_output(
            str(records[0]["ref"]),
            outcome_ref=candidate.scientific_outcome_ref,
            completion=True,
        )
        final_output["scientific_outcome"] = candidate.scientific_outcome
        context_pack = _context_pack(revision)
        final_review = _revised_review(candidate.checkpoint, final_output)
        final = memory.accept_reasoning_content(
            request_ref=candidate.request_ref,
            cycle_ref=candidate.cycle_ref,
            foreground_epoch=candidate.foreground_epoch,
            context_pack_ref=candidate.context_pack_ref,
            context_pack_hash=candidate.context_pack_hash,
            context_pack=context_pack,
            stage_request_receipt=candidate.stage_request_receipt,
            run_ref=candidate.run_ref,
            attempt_ref=candidate.attempt_ref,
            fence_ref=candidate.fence_ref,
            submission_ref="reasoning-submission:staged-final",
            outcome=final_output,
            reviewed_draft=candidate.checkpoint,
            review=final_review,
            execution_receipt=_receipt(
                "agent_runtime",
                REASONING_ATTEMPT_EXECUTION_RECEIPT_KIND,
                "reasoning-submission:staged-final",
                "staged-final",
            ),
            scientific_candidate_content_receipt=candidate.receipt,
            scientific_candidate_domain_receipt=scientific.receipt,
        )
        assert final.scientific_candidate_content_receipt == candidate.receipt
        assert final.scientific_candidate_domain_receipt == scientific.receipt
        accepted = graph.decide_reasoning_outcome(content=final)
        assert accepted.decision == "accepted"
        assert accepted.outcome_ref == candidate.scientific_outcome_ref
        assert accepted.feedback == ()
    finally:
        database.close()
