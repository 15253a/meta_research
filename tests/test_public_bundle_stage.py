from __future__ import annotations

import time
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy import text

from meta_research.bundle_skill import (
    BundleDispatchRequest,
    BundleDispatchResult,
    BundleSkillDraft,
    BundleSkillRequest,
    BundleSkillResult,
    BundleSkillUnavailable,
    BundleTargetBatchRequest,
    BundleTargetBatchResult,
    bind_bundle_runtime_to_full_conformance,
)
from meta_research.bundle_stage import _public_target_graph_rejection
from meta_research.bundle_target_contract import (
    FORMAL_STRATEGY_UPDATE_SCHEMA_REF,
    FORMAL_TARGET_CANDIDATE_SCHEMA_REF,
    MEASUREMENT_CONTRACT_CANDIDATE_SCHEMA_REF,
    PROTOCOL_VERSION_CANDIDATE_SCHEMA_REF,
    build_normalized_completion_contract,
    normalized_completion_contract_to_dict,
)
from meta_research.composition import build_production_runtime
from meta_research.harness import HarnessProbeRequest
from meta_research.owners.agent_runtime import BundleRuntimeBinding
from meta_research.owners.agent_runtime_harness import TargetRootCompletionEvidence
from meta_research.owners.common import (
    AcceptanceReceipt,
    OwnerConflict,
    canonical_hash,
    canonical_json,
)
from meta_research.owners.research_graph import SQLiteResearchGraph
from meta_research.owners.research_memory import AssetIntakeRequest
from meta_research.paths import prepare_data_root
from meta_research.runtime_protection import (
    InhibitorLease,
    RuntimeProtectionUnavailable,
)
from meta_research.semantic_owner_gateway import (
    BUNDLE_ROOT_SEMANTIC_OPERATION_IDS,
)
from meta_research.target_run_finalizer import (
    SQLiteTargetRootCompletionMemoryAuthority,
    TargetRunFinalizer,
)
from meta_research.target_run_runtime_contract import (
    decode_target_completion_handoff,
)
from test_public_plan_stage import (
    _DeterministicDraftingAdapter,
    _DeterministicIdeaSkill,
    _DeterministicPlanSkill,
    _DeterministicProbe,
    _confirm_direct_quest,
    _finish_idea_stage,
    _runtime,
)
from test_harness_full_conformance import (
    _FullConformanceAdapter,
    _full_request,
)


def _proof_receipt(receipt_ref: str, subject_ref: str) -> dict[str, object]:
    return {
        "receipt_ref": receipt_ref,
        "subject_ref": subject_ref,
        "verified": True,
        "currentness_known": True,
        "current": True,
    }


def test_bundle_rejection_projection_includes_actionable_feedback() -> None:
    receipt = AcceptanceReceipt(
        issuer="research_graph",
        kind="target_graph_rejected",
        receipt_ref="rg-target-rejection-receipt:1",
        subject_ref="bundle-submission:rejected",
        payload_hash="a" * 64,
    )
    projection = _public_target_graph_rejection(
        SimpleNamespace(
            submission_ref="bundle-submission:rejected",
            target_plan_hash="b" * 64,
            reason_code="target_candidate_owner_proof_unverified",
            feedback=("Attach accepted Owner proofs to every candidate.",),
            receipt=receipt,
        )
    )

    assert projection == {
        "status": "rejected",
        "targets": [],
        "frontier": [],
        "submission_ref": "bundle-submission:rejected",
        "target_plan_hash": "b" * 64,
        "rejection": {
            "code": "target_candidate_owner_proof_unverified",
            "feedback": ["Attach accepted Owner proofs to every candidate."],
            "receipt": receipt.as_public_dict(),
        },
    }


def _formal_candidate(
    *,
    completion_document: dict[str, object],
    label: str,
    experiment_key: str,
    cell: str,
    depends_on: tuple[str, ...] = (),
    risk_class: str = "normal",
) -> dict[str, object]:
    experiments = cast(
        list[dict[str, object]], completion_document["experiments"]
    )
    semantic = next(
        cast(dict[str, object], row["semantic_inputs"])
        for row in experiments
        if cast(dict[str, object], row["brief"])["experiment_key"]
        == experiment_key
    )
    implementation_ref = f"implementation:{label}"
    implementation_hash = canonical_hash({"implementation": label})
    part_keys = [f"part:{label}:first", f"part:{label}:second"]
    return {
        "schema_ref": FORMAL_TARGET_CANDIDATE_SCHEMA_REF,
        "candidate": {
            "local_label": label,
            "experiment_keys": [experiment_key],
            "measurement_unit_keys": [cell],
            "held_fixed_bindings": [],
            "implementation_revision_ref": implementation_ref,
            "code_changed": False,
            "reuse_trace": {
                "tier_decisions": [
                    {
                        "tier": "self-implementation",
                        "disposition": "selected",
                        "reason_ref": f"reuse-reason:{label}",
                        "source_proofs": [
                            {
                                "source_ref": f"source:{label}",
                                "exact_version_ref": f"source-version:{label}",
                                "implementation_revision_ref": implementation_ref,
                                "eligible_tier": "self-implementation",
                                "verification_receipt": _proof_receipt(
                                    f"source-receipt:{label}",
                                    f"source-version:{label}",
                                ),
                                "implementation_binding": {
                                    "subject_ref": implementation_ref,
                                    "content_hash_ref": implementation_hash,
                                },
                                "implementation_acceptance_receipt": (
                                    _proof_receipt(
                                        f"implementation-receipt:{label}",
                                        implementation_hash,
                                    )
                                ),
                                "eligibility_anchor_ref": None,
                                "eligibility_binding": None,
                                "eligibility_receipt": None,
                                "license_ref": None,
                                "content_hash_ref": None,
                                "patch_ref": None,
                            }
                        ],
                    }
                ],
                "greenfield_exception": "simple-implementation",
            },
            "routes": [
                {
                    "route_ref": f"route:{label}",
                    "known_external_operation_refs": [],
                }
            ],
            "depends_on_labels": list(depends_on),
            "direct_accepted_input_asset_refs": [],
        },
        "semantic_inputs": [deepcopy(semantic)],
        "measurement_contract": {
            "schema_ref": MEASUREMENT_CONTRACT_CANDIDATE_SCHEMA_REF,
            "experiment_keys": [experiment_key],
            "measurement_unit_key": cell,
            "baseline_forward_contract": {
                "schema_ref": "test/baseline-forward/v1",
                "input_role": "accepted baseline",
                "output_role": "frozen prediction",
            },
            "variant_recipe": {
                "schema_ref": "test/variant-recipe/v1",
                "semantic_delta": semantic["semantic_delta"],
            },
            "evaluation_protocol_lineage": {
                "schema_ref": "test/evaluation-protocol-lineage/v1",
                "family_ref": f"protocol-family:{label}",
            },
            "protocol_version": {
                "schema_ref": PROTOCOL_VERSION_CANDIDATE_SCHEMA_REF,
                "evaluation_data": {
                    "dataset_ref": f"dataset:{label}",
                    "selection": "frozen",
                },
                "split": {
                    "split_ref": f"split:{label}",
                    "kind": "fixed",
                },
                "preprocessing": {
                    "pipeline_ref": f"preprocessing:{label}",
                    "steps": ["validate", "normalize"],
                },
                "required_metrics": [
                    {
                        "metric_key": "metric:effect",
                        "definition": {
                            "formula": "variant - baseline",
                            "units": "score",
                            "direction": "maximize",
                            "value_schema": {"type": "number"},
                        },
                    }
                ],
                "optional_metrics": [
                    {
                        "metric_key": "metric:uncertainty",
                        "definition": {
                            "formula": "standard error",
                            "units": "score",
                            "direction": "minimize",
                            "value_schema": {
                                "type": "number",
                                "minimum": 0,
                            },
                        },
                    }
                ],
                "internal_part_keys": part_keys,
                "aggregation": {
                    "rule_ref": f"aggregation:{label}",
                    "rule": {
                        "kind": "ordered_mean",
                        "ordered_part_keys": part_keys,
                    },
                },
                "preregistered_stop_rules": [
                    {
                        "rule_ref": f"stop:{label}",
                        "rule": {
                            "kind": "fixed_budget",
                            "maximum_observations": 2,
                        },
                    }
                ],
            },
            "checkpoint_policy": "optional",
            "result_schema_ref": "test/target-result/v1",
            "result_schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "metrics": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "metric:effect": {"type": "number"},
                            "metric:uncertainty": {"type": "number"},
                        },
                        "required": ["metric:effect"],
                    }
                },
                "required": ["metrics"],
            },
        },
        "risk_class": risk_class,
    }


def _formal_target_plan(
    request: BundleSkillRequest,
    *,
    cells_by_experiment: dict[str, tuple[str, ...]],
    candidates: tuple[
        tuple[str, str, str, tuple[str, ...], str], ...
    ],
    strategy_complete: bool = False,
) -> dict[str, object]:
    briefs = cast(
        list[dict[str, object]], request.plan_document["experiment_briefs"]
    )
    completion = build_normalized_completion_contract(
        request.plan_document,
        tuple(
            {
                "experiment_key": cast(str, brief["experiment_key"]),
                "held_fixed_slots": [],
                "required_measurement_unit_keys": list(
                    cells_by_experiment[cast(str, brief["experiment_key"])]
                ),
            }
            for brief in briefs
        ),
    )
    completion_document = normalized_completion_contract_to_dict(completion)
    formal_candidates = [
        _formal_candidate(
            completion_document=completion_document,
            label=label,
            experiment_key=experiment_key,
            cell=cell,
            depends_on=depends_on,
            risk_class=risk_class,
        )
        for label, experiment_key, cell, depends_on, risk_class in candidates
    ]
    return {
        "schema_ref": "meta-research/target-plan/v3",
        "kind": "TargetPlan",
        "formal_plan_ref": request.formal_plan_ref,
        "context_pack_ref": request.context_pack_ref,
        "completion_contract": completion_document,
        "initial_strategy_update": {
            "schema_ref": FORMAL_STRATEGY_UPDATE_SCHEMA_REF,
            "revision": 1,
            "candidates": formal_candidates,
            "requires_accepted_labels": [],
            "strategy_complete": strategy_complete,
        },
        "source_bindings": {
            "formal_plan_ref": request.formal_plan_ref,
            "plan_document_hash": canonical_hash(request.plan_document),
            "context_pack_ref": request.context_pack_ref,
            "context_pack_hash": request.context_pack_hash,
        },
    }


class _DeterministicBundleSkill:
    def bind_full_conformance_authority(self, authority) -> None:
        self._full_conformance_authority = authority

    def runtime_binding(self) -> BundleRuntimeBinding:
        authority = getattr(self, "_full_conformance_authority", None)
        binding = BundleRuntimeBinding(
            packaged_skill_bundle_hash=canonical_hash({"skill": "bundle-public"}),
            instruction_set_hash=canonical_hash({"instructions": "bundle-public"}),
            model_ref="test-model-v1",
            harness_adapter_ref=(
                "codex-cli/test-operation-bound"
                if authority is not None
                else "test-deterministic-v1"
            ),
            mcp_bindings=(),
            capability_bindings=(),
            resource_bindings=(),
        )
        if authority is None:
            return binding
        return bind_bundle_runtime_to_full_conformance(
            binding,
            authority.require_operation_binding(
                harness_family="codex",
                required_operation_ids=BUNDLE_ROOT_SEMANTIC_OPERATION_IDS,
                required_capabilities=("semantic_mcp",),
            ),
            required_operation_ids=BUNDLE_ROOT_SEMANTIC_OPERATION_IDS,
        )

    def _target_plan(self, request: BundleSkillRequest) -> dict[str, object]:
        briefs = cast(
            list[dict[str, object]], request.plan_document["experiment_briefs"]
        )
        cells = {
            cast(str, brief["experiment_key"]): (
                f"measurement:{brief['experiment_key']}",
            )
            for brief in briefs
        }
        first_key = cast(str, briefs[0]["experiment_key"])
        return _formal_target_plan(
            request,
            cells_by_experiment=cells,
            candidates=(
                (
                    "rare-morphology-comparison",
                    first_key,
                    cells[first_key][0],
                    (),
                    "normal",
                ),
            ),
        )

    def generate_draft(self, request: BundleSkillRequest) -> BundleSkillDraft:
        return BundleSkillDraft(
            draft=self._target_plan(request),
            primary_session_ref=request.native_session_ref or "bundle-primary-1",
            adapter_kind="test_deterministic",
        )

    def review_draft(
        self, request: BundleSkillRequest, draft: BundleSkillDraft
    ) -> BundleSkillResult:
        return BundleSkillResult(
            reviewed_draft=draft.draft,
            final_target_plan=draft.draft,
            findings=(),
            dispositions=(),
            primary_session_ref=draft.primary_session_ref,
            review_mode="advisory_unobserved",
            reviewer_agent_ref=None,
            adapter_kind=draft.adapter_kind,
        )

    def execute(self, request: BundleSkillRequest) -> BundleSkillResult:
        draft = self.generate_draft(request)
        return self.review_draft(request, draft)

    def schedule_target(self, request: BundleDispatchRequest) -> BundleDispatchResult:
        selected = (
            None if not request.frontier else str(request.frontier[-1]["target_ref"])
        )
        return BundleDispatchResult(
            action="wait" if selected is None else "dispatch",
            selected_target_ref=selected,
            rationale=(
                "No currently dispatchable Target."
                if selected is None
                else "Select the highest-priority durable frontier item."
            ),
            native_session_ref=request.native_session_ref,
            adapter_kind="test_deterministic",
        )

    def propose_target_batch(
        self, request: BundleTargetBatchRequest
    ) -> BundleTargetBatchResult:
        return BundleTargetBatchResult(
            strategy_update={
                "schema_ref": FORMAL_STRATEGY_UPDATE_SCHEMA_REF,
                "revision": request.base_generation + 2,
                "candidates": [],
                "requires_accepted_labels": [],
                "strategy_complete": True,
            },
            rationale="The frozen FormalPlan is fully covered by committed Targets.",
            native_session_ref=request.native_session_ref,
            adapter_kind="test_deterministic",
        )


class _TwoTargetBundleSkill(_DeterministicBundleSkill):
    def _second_dependencies(self) -> tuple[str, ...]:
        return ("rare-morphology-comparison",)

    def _target_plan(self, request: BundleSkillRequest) -> dict[str, object]:
        briefs = cast(
            list[dict[str, object]], request.plan_document["experiment_briefs"]
        )
        assert len(briefs) == 1
        experiment_key = cast(str, briefs[0]["experiment_key"])
        cells = ("measurement:primary", "measurement:replication")
        return _formal_target_plan(
            request,
            cells_by_experiment={experiment_key: cells},
            candidates=(
                (
                    "rare-morphology-comparison",
                    experiment_key,
                    cells[0],
                    (),
                    "normal",
                ),
                (
                    "rare-morphology-replication",
                    experiment_key,
                    cells[1],
                    self._second_dependencies(),
                    "normal",
                ),
            ),
        )


class _ParallelTwoTargetBundleSkill(_TwoTargetBundleSkill):
    def _second_dependencies(self) -> tuple[str, ...]:
        return ()


class _TwoGapPlanSkill(_DeterministicPlanSkill):
    def __init__(self) -> None:
        super().__init__(no_gap=False)

    def _document(self, request):
        document = super()._document(request)
        idea_ref = request.accepted_idea_set["candidates"][0]["candidate_key"]
        second_key = "artifact-boundary-comparison"
        second_obligation = {
            **deepcopy(document["answer_contract"]["obligations"][0]),
            "obligation_key": second_key,
            "statement": "比较伪影边界在冻结协议下是否可复查。",
        }
        obligations = [
            *document["answer_contract"]["obligations"],
            second_obligation,
        ]
        contract_without_hash = {
            "source_question_ref": document["answer_contract"][
                "source_question_ref"
            ],
            "source_idea_set_ref": document["answer_contract"][
                "source_idea_set_ref"
            ],
            "obligations": obligations,
        }
        document["answer_contract"] = {
            **contract_without_hash,
            "answer_contract_hash": canonical_hash(contract_without_hash),
        }
        document["coverage"].append(
            {
                "obligation_key": second_key,
                "disposition": "gap",
                "evidence_uses": [],
                "insufficiency": "当前证据没有伪影边界的条件级结果。",
            }
        )
        document["gap_set"].append(second_key)
        document["experiment_briefs"].append(
            {
                "experiment_key": "compare-artifact-boundary",
                "gap_obligation_keys": [second_key],
                "goal": "比较冻结条件下的伪影边界。",
                "characteristics": "固定输入并报告伪影边界指标。",
                "boundary_constraints": "固定预算、数据和评价协议。",
                "semantic_delta": "只改变伪影处理策略。",
                "contributing_idea_refs": [idea_ref],
            }
        )
        document["idea_trace"][0]["obligation_roles"].append(
            {"obligation_key": second_key, "role": "experiment_lens"}
        )
        return document


class _RollingBundleSkill(_DeterministicBundleSkill):
    def __init__(self) -> None:
        self.batch_requests: list[BundleTargetBatchRequest] = []

    def propose_target_batch(
        self, request: BundleTargetBatchRequest
    ) -> BundleTargetBatchResult:
        self.batch_requests.append(request)
        if request.base_generation == 0:
            assert len(request.current_targets) == 1
            assert len(request.target_commits) == 1
            current_spec = request.current_targets[0]["spec"]
            assert isinstance(current_spec, dict)
            second_brief = request.plan_document["experiment_briefs"][1]
            assert isinstance(second_brief, dict)
            current_candidate = cast(dict[str, object], current_spec["candidate"])
            completion = cast(
                dict[str, object],
                request.initial_target_plan["completion_contract"],
            )
            second_key = cast(str, second_brief["experiment_key"])
            second_spec = _formal_candidate(
                completion_document=completion,
                label="artifact-boundary-followup",
                experiment_key=second_key,
                cell=f"measurement:{second_key}",
                depends_on=(cast(str, current_candidate["local_label"]),),
            )
            return BundleTargetBatchResult(
                strategy_update={
                    "schema_ref": FORMAL_STRATEGY_UPDATE_SCHEMA_REF,
                    "revision": 2,
                    "candidates": [second_spec],
                    "requires_accepted_labels": [
                        cast(str, current_candidate["local_label"])
                    ],
                    "strategy_complete": True,
                },
                rationale="The second frozen gap still needs its own TargetCommit.",
                native_session_ref=request.native_session_ref,
                adapter_kind="test_rolling",
            )
        raise AssertionError("The non-empty revision-2 append seals the strategy")


class _RemeasureBundleSkill(_DeterministicBundleSkill):
    def __init__(self) -> None:
        self.source_variant_run_ref: str | None = None
        self.checkpoint_role_refs: tuple[str, ...] = ()

    def _target_plan(self, request: BundleSkillRequest) -> dict[str, object]:
        if self.source_variant_run_ref is None:
            raise AssertionError("Remeasure source must be accepted before Bundle")
        briefs = cast(
            list[dict[str, object]], request.plan_document["experiment_briefs"]
        )
        assert len(briefs) == 1
        experiment_key = cast(str, briefs[0]["experiment_key"])
        cell = f"measurement:{experiment_key}"
        return _formal_target_plan(
            request,
            cells_by_experiment={experiment_key: (cell,)},
            candidates=(
                (
                    "accepted-variant-remeasurement",
                    experiment_key,
                    cell,
                    (),
                    "normal",
                ),
            ),
        )


class _HighRiskBundleSkill(_DeterministicBundleSkill):
    def _target_plan(self, request: BundleSkillRequest) -> dict[str, object]:
        target_plan = super()._target_plan(request)
        update = target_plan["initial_strategy_update"]
        assert isinstance(update, dict)
        candidates = update["candidates"]
        assert isinstance(candidates, list)
        target = candidates[0]
        assert isinstance(target, dict)
        target["risk_class"] = "high"
        return target_plan


class _NonDispatchingBundleSkill(_DeterministicBundleSkill):
    def __init__(self, action: str) -> None:
        self.action = action
        self.schedule_calls = 0

    def schedule_target(self, request: BundleDispatchRequest) -> BundleDispatchResult:
        self.schedule_calls += 1
        return BundleDispatchResult(
            action=self.action,
            selected_target_ref=None,
            rationale="Pause despite an executable Target.",
            native_session_ref=request.native_session_ref,
            adapter_kind="test_non_dispatching",
        )


class _TogglePowerInhibitor:
    kind = "test_toggle_inhibitor"

    def __init__(self) -> None:
        self.available = True
        self._active: set[str] = set()

    def acquire(self, *, holder_ref: str, reason: str) -> InhibitorLease:
        del reason
        if not self.available:
            raise RuntimeProtectionUnavailable("power_inhibitor_acquisition_failed")
        self._active.add(holder_ref)
        return InhibitorLease(
            holder_ref=holder_ref,
            backend=self.kind,
            scope="sleep",
            acquired_at=time.time(),
            native_holder_ref=f"test-native:{holder_ref}",
        )

    def is_confirmed(self, lease: InhibitorLease) -> bool:
        return lease.holder_ref in self._active

    def release(self, lease: InhibitorLease) -> None:
        self._active.discard(lease.holder_ref)


class _ProtectedRollingBundleSkill(_RollingBundleSkill):
    def __init__(self) -> None:
        super().__init__()
        self.schedule_requests: list[BundleDispatchRequest] = []

    def schedule_target(self, request: BundleDispatchRequest) -> BundleDispatchResult:
        self.schedule_requests.append(request)
        return super().schedule_target(request)


class _UnknownDispatchBundleSkill(_ProtectedRollingBundleSkill):
    def schedule_target(self, request: BundleDispatchRequest) -> BundleDispatchResult:
        self.schedule_requests.append(request)
        raise BundleSkillUnavailable("codex_operation_reconciliation_pending")


class _ProtectedSealingBundleSkill(_DeterministicBundleSkill):
    def __init__(self) -> None:
        self.batch_requests: list[BundleTargetBatchRequest] = []

    def propose_target_batch(
        self, request: BundleTargetBatchRequest
    ) -> BundleTargetBatchResult:
        self.batch_requests.append(request)
        return super().propose_target_batch(request)


class _FixtureTargetCommitEvidenceAuthority:
    """Accepted upstream evidence fixture for the isolated no-gap branch."""

    def __init__(self) -> None:
        self.quest_ref: str | None = None
        self.catalog: tuple[dict[str, object], ...] = ()

    def install(self, *, quest_ref: str, evidence: dict[str, object]) -> None:
        self.quest_ref = quest_ref
        self.catalog = (evidence,)

    def query_plan_evidence_catalog(
        self, *, quest_ref: str
    ) -> tuple[int, tuple[dict[str, object], ...]]:
        if self.quest_ref is None:
            return 0, ()
        if quest_ref != self.quest_ref:
            raise OwnerConflict("fixture_evidence_quest_invalid")
        return len(self.catalog), self.catalog

    def verify_plan_evidence_catalog(
        self,
        *,
        quest_ref: str,
        evidence_catalog: list[dict[str, object]],
        expected_reference_revision: int,
        require_current: bool = True,
        require_complete: bool = True,
        selected_evidence_refs: frozenset[str] | None = None,
    ) -> None:
        revision, catalog = self.query_plan_evidence_catalog(quest_ref=quest_ref)
        refs = {str(item["evidence_ref"]) for item in evidence_catalog}
        if (
            expected_reference_revision != revision
            or tuple(evidence_catalog) != catalog
            or (
                selected_evidence_refs is not None
                and not selected_evidence_refs.issubset(refs)
            )
        ):
            raise OwnerConflict("fixture_evidence_catalog_invalid")


class _AcceptingTargetCandidateProofVerifier:
    """Test Owner seam; production composition intentionally has no fallback."""

    def verify_reuse_source_receipt(self, **_values) -> None:
        return None

    def verify_reuse_content_receipt(self, **_values) -> None:
        return None

    def verify_reuse_eligibility_receipt(self, **_values) -> None:
        return None


def _bundle_runtime(
    path: Path,
    *,
    no_gap: bool = False,
    target_commit_evidence_authority=None,
    bundle_skill_provider=None,
    plan_skill_provider=None,
    harness_ready: bool = True,
    verify_target_candidate_proofs: bool = True,
    target_execution_sandbox_config=None,
    power_inhibitor=None,
):
    drafting = _DeterministicDraftingAdapter()
    runtime = build_production_runtime(
        prepare_data_root(path),
        proposal_drafter=drafting,
        intent_drafting_provider=drafting,
        host_compute_probe=_DeterministicProbe(),
        idea_skill_provider=_DeterministicIdeaSkill(),
        plan_skill_provider=(
            plan_skill_provider or _DeterministicPlanSkill(no_gap=no_gap)
        ),
        bundle_skill_provider=(bundle_skill_provider or _DeterministicBundleSkill()),
        target_commit_evidence_authority=target_commit_evidence_authority,
        harness_adapters=(
            _FullConformanceAdapter("codex"),
            _FullConformanceAdapter("claude"),
        ),
        power_inhibitor=power_inhibitor,
    )
    if verify_target_candidate_proofs:
        runtime.owners.research_graph._target_candidate_proof_verifier = (  # type: ignore[attr-defined]
            _AcceptingTargetCandidateProofVerifier()
        )
    else:
        runtime.owners.research_graph._target_candidate_proof_verifier = None  # type: ignore[attr-defined]
    if harness_ready and runtime.harnesses.query_status()["status"] != "ready":
        runtime.harnesses.start_full_conformance(_full_request())
        for _turn in range(4):
            if runtime.harnesses.query_status()["status"] == "ready":
                break
            assert runtime.harnesses.advance_full_conformance(
                mcp_base_url="http://127.0.0.1:0"
            )
        assert runtime.harnesses.query_status()["status"] == "ready"
    return runtime


def _accept_real_target_root_commit(runtime):
    # Import lazily because the shared target-root fixture imports this module
    # for the production composition helper above.
    from test_target_root_finalizer import (
        _EvidenceReader,
        _admit_independent_target_root,
    )

    target, candidate, formal_plan, admission, handle = (
        _admit_independent_target_root(runtime)
    )
    launch = runtime.owners.agent_runtime.query_admitted_target_launch(
        target.target_ref
    )
    assert launch is not None
    lifecycle = runtime.target_root_lifecycle
    lifecycle.activate(
        launch_ref=launch.launch_ref,
        handle=handle,
        candidate=candidate,
        formal_plan=formal_plan,
        idempotency_key=f"activate-protected-bundle-root:{target.target_ref}",
    )
    memory = SQLiteTargetRootCompletionMemoryAuthority(
        runtime._database,
        runtime.feed,
        runtime.owners.research_memory,
        lifecycle,
    )
    authority = (
        runtime.owners.research_graph.query_target_measurement_domain_authority(
            target.target_ref
        )
    )
    assert authority is not None
    _workspace_ref, workspace = (
        runtime.target_run_authorities.agent_runtime.resolve_target_workspace(
            target_ref=handle.target_ref,
            target_run_ref=handle.target_run_ref,
            root_session_ref=handle.root_session_ref,
            attempt_ref=handle.execution_attempt_ref,
            fence_ref=handle.execution_fence_ref,
        )
    )
    (workspace / "implementation" / "train.py").write_text(
        "print('train')\n", encoding="utf-8"
    )
    (workspace / "outputs").mkdir()
    (workspace / "logs").mkdir()
    metrics = {
        key: float(index + 1)
        for index, key in enumerate(
            authority.measurement_contract.protocol_version.required_metric_keys
        )
    }
    result_document = {
        "metrics": metrics,
        "result_disposition": "positive",
        "schema_ref": authority.measurement_contract.result_schema_ref,
    }
    (workspace / "outputs" / "metrics.json").write_text(
        canonical_json(result_document), encoding="utf-8"
    )
    (workspace / "logs" / "train.log").write_text(
        "epoch 1 complete\n", encoding="utf-8"
    )
    artifacts: list[dict[str, str]] = [
        {"role": "implementation", "relative_path": "implementation"},
        {"role": "result", "relative_path": "outputs/metrics.json"},
        {"role": "log", "relative_path": "logs/train.log"},
    ]
    if authority.measurement_contract.checkpoint_policy == "required":
        (workspace / "outputs" / "final.ckpt").write_bytes(b"checkpoint-v1")
        artifacts.insert(
            1,
            {"role": "checkpoint", "relative_path": "outputs/final.ckpt"},
        )
    handoff = canonical_json(
        {
            "artifacts": artifacts,
            "result_document_path": "outputs/metrics.json",
            "schema_ref": "meta-research/target-completion-handoff/v1",
            "status": "completed",
            "summary": "Root finished implementation and training.",
            "target_ref": handle.target_ref,
            "target_run_ref": handle.target_run_ref,
        }
    )
    evidence = TargetRootCompletionEvidence(
        target_ref=handle.target_ref,
        target_run_ref=handle.target_run_ref,
        attempt_ref=handle.execution_attempt_ref,
        attempt_generation=admission.run.attempt_generation,
        root_session_ref=handle.root_session_ref,
        native_session_ref="native_protected_bundle_root",
        fence_ref=handle.execution_fence_ref,
        operation_ref="harness_protected_bundle_root_final_turn",
        operation_generation=1,
        evidence_ref="harness_evidence_protected_bundle_root_final_turn",
        evidence_sequence=10,
        handoff=decode_target_completion_handoff(handoff),
        observed_at=1.0,
    )
    result = TargetRunFinalizer(
        lifecycle=lifecycle,
        memory=memory,
        workspace_resolver=runtime.target_run_authorities.agent_runtime,
        evidence_reader=_EvidenceReader(evidence),
        measurement_authority=runtime.owners.research_graph,
        graph_authority=runtime.owners.research_graph,
    ).finalize(handle=handle, evidence=evidence)
    assert result.status == "completed"
    assert result.target_commit_ref is not None
    published = runtime.owners.agent_runtime.publish_target_root_completion(
        target_ref=target.target_ref,
        completion_ref=result.completion_ref,
        target_commit_ref=result.target_commit_ref,
    )
    assert published.terminal is not None
    lifecycle.mark_completed(
        target_ref=target.target_ref,
        completion_ref=result.completion_ref,
    )
    commits = runtime.owners.research_graph.query_target_commits(launch.graph_ref)
    assert [commit.commit_ref for commit in commits] == [result.target_commit_ref]
    return target, launch.graph_ref


def _target_commit_count(runtime) -> int:
    with runtime._database.read() as connection:
        return int(
            connection.execute(
                text("SELECT COUNT(*) FROM rg_target_commits")
            ).scalar_one()
        )


def _install_fixture_plan_evidence(
    runtime,
    authority: _FixtureTargetCommitEvidenceAuthority,
    *,
    quest_ref: str,
) -> None:
    intake = runtime.owners.research_memory.submit_asset_intake(
        AssetIntakeRequest(
            source_kind="text",
            custody_mode="managed",
            display_name="accepted-target-commit-evidence.json",
            media_type=("application/vnd.meta-research.target-commit-evidence+json"),
            content=b'{"accepted":true}\n',
            provenance={
                "target_commit_root_ref": "target_commit_fixture",
                "provenance_closure_refs": ["target_fixture"],
                "capabilities": ["query_support"],
            },
        ),
        idempotency_key="bundle-no-gap-evidence-intake",
    )
    assert intake.asset is not None
    role = runtime.owners.research_graph.accept_asset_role(
        binding=intake.asset.as_binding(),
        role="evidence",
        quest_ref=quest_ref,
        idempotency_key="bundle-no-gap-evidence-role",
    )
    authority.install(
        quest_ref=quest_ref,
        evidence={
            "schema_ref": "meta-research/evidence-ref/v1",
            "evidence_ref": "evidence_target_commit_fixture",
            "asset_version_ref": intake.asset.version_ref,
            "asset_ref": intake.asset.asset_ref,
            "content_hash": intake.asset.content_hash,
            "manifest_hash": intake.asset.manifest_hash,
            "target_commit_root_ref": "target_commit_fixture",
            "provenance_closure_refs": ["target_fixture"],
            "capabilities": ["query_support"],
            "eligibility_token_ref": role.receipt.receipt_ref,
            "integrity_receipt_ref": intake.asset.receipt.receipt_ref,
            "availability_receipt_ref": intake.asset.receipt.receipt_ref,
            "currentness_receipt_ref": role.receipt.receipt_ref,
            "asset_receipt": intake.asset.receipt.as_public_dict(),
            "role_ref": role.role_ref,
            "role_receipt": role.receipt.as_public_dict(),
        },
    )


def _finish_plan_stage(runtime) -> dict[str, object]:
    for _step in range(16):
        current = runtime.plan_stage.query_current()
        if current["stage_commit"] is not None:
            return current
        assert runtime.plan_stage.process_once()
    raise AssertionError("Plan Stage did not reach StageCommit")


def _prepare_bundle_request(runtime) -> None:
    _confirm_direct_quest(runtime)
    _finish_idea_stage(runtime)
    _finish_plan_stage(runtime)
    assert runtime.bundle_stage.process_once()
    assert runtime.bundle_stage.query_current()["stage_run_request"] is not None


def _assert_public_bundle_execution(current: dict[str, object]) -> dict[str, object]:
    run = current["run"]
    assert isinstance(run, dict)
    assert run["status"] == "awaiting_acceptance"
    assert run["primary_draft_checkpoint"]["status"] == "recorded"
    assert run["provider_operations"]["primary"]["status"] == "completed"
    assert run["provider_operations"]["review"]["status"] == "prepared"
    assert run["attempt_execution_receipt"]["status"] == "accepted"
    assert current["target_graph"]["status"] == "not_attempted"
    assert current["stage_commit"] is None
    return run


def _advance_to_public_bundle_execution(runtime) -> dict[str, object]:
    for _step in range(3):
        current = runtime.bundle_stage.query_current()
        run = current["run"]
        if isinstance(run, dict) and run["attempt_execution_receipt"] is not None:
            return _assert_public_bundle_execution(current)
        assert runtime.bundle_stage.process_once()
    return _assert_public_bundle_execution(runtime.bundle_stage.query_current())


def test_bundle_admission_uses_operation_binding_without_global_conformance(
    tmp_path: Path,
) -> None:
    runtime = _bundle_runtime(
        tmp_path / "bundle-harness-unavailable",
        bundle_skill_provider=_DeterministicBundleSkill(),
        harness_ready=False,
    )
    try:
        admission = runtime.harnesses.admit_probe(
            HarnessProbeRequest(
                request_ref="legacy-bundle-partial-probe",
                harness_family="codex",
                model_ref="gpt-partial",
                auth_profile_ref="harness-profile:codex-default",
                required_operation_ids=("research_graph.snapshot.read",),
                required_capabilities=("native_session", "stream"),
            ),
            idempotency_key="legacy-bundle-partial-probe",
        )
        runtime.harnesses.execute_probe(
            admission.run.request_ref,
            prompt="Run only the legacy diagnostic subset.",
            mcp_base_url="http://127.0.0.1:0",
        )
        _prepare_bundle_request(runtime)

        assert runtime.bundle_stage.process_once()
        admitted = runtime.bundle_stage.query_current()
        assert admitted["run"]["status"] == "admitted"
        assert admitted["run"]["attempt_execution_receipt"] is None
        _advance_to_public_bundle_execution(runtime)
        assert runtime.bundle_stage.transient_error is None
    finally:
        runtime.close()


def test_partial_global_conformance_does_not_gate_bundle_operation_binding(
    tmp_path: Path,
) -> None:
    runtime = _bundle_runtime(
        tmp_path / "bundle-harness-partial",
        bundle_skill_provider=_DeterministicBundleSkill(),
        harness_ready=False,
    )
    try:
        runtime.harnesses.start_full_conformance(_full_request())
        assert runtime.harnesses.advance_full_conformance(
            mcp_base_url="http://127.0.0.1:0"
        )
        _prepare_bundle_request(runtime)

        assert runtime.bundle_stage.process_once()
        admitted = runtime.bundle_stage.query_current()
        assert admitted["run"]["status"] == "admitted"
        assert admitted["run"]["attempt_execution_receipt"] is None
        _advance_to_public_bundle_execution(runtime)
        assert runtime.bundle_stage.transient_error is None
    finally:
        runtime.close()


def test_bundle_freezes_operation_binding_independently_of_global_conformance(
    tmp_path: Path,
) -> None:
    runtime = _bundle_runtime(
        tmp_path / "bundle-harness-drift",
        bundle_skill_provider=_DeterministicBundleSkill(),
    )
    try:
        _prepare_bundle_request(runtime)
        assert runtime.bundle_stage.process_once()
        admitted = runtime.bundle_stage.query_current()
        binding = admitted["run"]["runtime_binding"]
        assert binding["capability_bindings"] == [
            "harness-operation-binding-v1",
            "semantic-mcp-resident",
        ]
        assert len(binding["mcp_bindings"]) == 2
        assert len(binding["resource_bindings"]) == 2
        assert all(
            item.startswith("harness-artifact:operation-binding-")
            for item in binding["resource_bindings"]
        )
        frozen_binding = binding

        runtime.harnesses.start_full_conformance(_full_request())
        executed = _advance_to_public_bundle_execution(runtime)
        execution_identity = {
            key: executed[key]
            for key in (
                "run_ref",
                "attempt_ref",
                "submission_ref",
                "attempt_execution_receipt",
            )
        }
        for _turn in range(4):
            if runtime.harnesses.query_status()["status"] == "ready":
                break
            assert runtime.harnesses.advance_full_conformance(
                mcp_base_url="http://127.0.0.1:0"
            )
        assert runtime.harnesses.query_status()["status"] == "ready"
        assert runtime.bundle_stage.process_once()
        projected = runtime.bundle_stage.query_current()
        assert {
            key: projected["run"][key]
            for key in execution_identity
        } == execution_identity
        assert projected["run"]["runtime_binding"] == frozen_binding
        assert runtime.bundle_stage.transient_error is None
    finally:
        runtime.close()


def test_bundle_ready_full_conformance_allows_provider_entry(tmp_path: Path) -> None:
    runtime = _bundle_runtime(
        tmp_path / "bundle-harness-ready",
        bundle_skill_provider=_DeterministicBundleSkill(),
    )
    try:
        _prepare_bundle_request(runtime)
        assert runtime.bundle_stage.process_once()
        _advance_to_public_bundle_execution(runtime)
    finally:
        runtime.close()


def _grant_request_capability(runtime, request: dict[str, object]) -> dict[str, object]:
    requirement = request["required_authorization"]
    assert isinstance(requirement, dict)
    capability = requirement["capability"]
    capability_scope = requirement["scope"]
    quest_ref = request["quest_ref"]
    assert isinstance(capability, str)
    assert isinstance(capability_scope, dict)
    assert isinstance(quest_ref, str)
    response = runtime.owners.human_collaboration.respond_to_human_request(
        request["request_ref"],
        decision="provided",
        facts={"authorization_decision": "allow_once"},
        note="Grant only the exact Target described by this request.",
        idempotency_key="bundle-high-risk-response",
    )
    drafted = runtime.owners.human_collaboration.create_command_draft(
        f"quest:{quest_ref}",
        {
            "command_kind": "capability_authorization",
            "payload": {
                "capability": capability,
                "decision": "granted",
                "scope": capability_scope,
            },
        },
        "bundle-high-risk-command-draft",
    )
    preview = runtime.owners.human_collaboration.preview_command(
        drafted["intent_id"],
        drafted["draft_revision"],
        drafted["draft_hash"],
        "bundle-high-risk-command-preview",
    )["impact_preview"]
    confirmed = runtime.owners.human_collaboration.confirm_command(
        drafted["intent_id"],
        drafted["draft_revision"],
        drafted["draft_hash"],
        preview["preview_ref"],
        preview["preview_hash"],
        "bundle-high-risk-command-confirm",
    )
    authorization = runtime.owners.human_collaboration.decide_capability_authorization(
        f"quest:{quest_ref}",
        {
            **requirement,
            "decision": "granted",
            "confirmation_receipt_ref": confirmed["confirmation_receipt"][
                "receipt_ref"
            ],
        },
        "bundle-risk-auth-record",
    )
    assert response["decision"] == "provided"
    return authorization


def test_gap_plan_becomes_publicly_eligible_for_bundle_without_early_truth(
    tmp_path: Path,
) -> None:
    runtime = _runtime(
        tmp_path / "bundle-gap-eligibility",
        idea_skill=_DeterministicIdeaSkill(),
        plan_skill=_DeterministicPlanSkill(no_gap=False),
    )
    try:
        _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        plan = _finish_plan_stage(runtime)
        assert plan["plan_acceptance"]["bundle_disposition"] == ("experiments_required")

        eligible = runtime.bundle_stage.query_current()
        assert eligible == {
            "eligibility": {
                "status": "eligible",
                "cycle_ref": plan["eligibility"]["cycle_ref"],
                "question_ref": plan["eligibility"]["question_ref"],
                "formal_plan_ref": plan["stage_commit"]["outcome_ref"],
                "reason": None,
                "next_stage": "Bundle",
            },
            "stage_run_request": None,
            "run": None,
            "target_graph": {
                "status": "not_attempted",
                "targets": [],
                "frontier": [],
            },
            "target_commits": [],
            "baseline_pool": [],
            "bundle_report": None,
            "disposition": {"status": "not_attempted"},
            "stage_commit": None,
        }
    finally:
        runtime.close()


def test_bundle_worker_freezes_the_exact_formal_plan_before_any_run(
    tmp_path: Path,
) -> None:
    runtime = _runtime(
        tmp_path / "bundle-request",
        idea_skill=_DeterministicIdeaSkill(),
        plan_skill=_DeterministicPlanSkill(no_gap=False),
    )
    try:
        _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        plan = _finish_plan_stage(runtime)

        assert runtime.bundle_stage.process_once()

        current = runtime.bundle_stage.query_current()
        request = current["stage_run_request"]
        assert request["status"] == "current"
        assert request["stage"] == "Bundle"
        assert request["cycle_ref"] == plan["eligibility"]["cycle_ref"]
        assert (
            request["accepted_formal_plan_binding"]["formal_plan_ref"]
            == (plan["stage_commit"]["formal_plan_ref"])
        )
        assert (
            request["accepted_formal_plan_binding"]["stage_commit_ref"]
            == (plan["stage_commit"]["stage_commit_ref"])
        )
        assert request["accepted_formal_plan_binding"]["plan_document"]["gap_set"] == [
            "rare-morphology-comparison"
        ]
        assert current["run"] is None
        assert current["target_graph"]["status"] == "not_attempted"
    finally:
        runtime.close()


def test_bundle_root_run_accepts_a_distinct_target_dag(
    tmp_path: Path,
) -> None:
    runtime = _bundle_runtime(tmp_path / "bundle-target-graph")
    try:
        _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        _finish_plan_stage(runtime)

        for _step in range(8):
            assert runtime.bundle_stage.process_once()
            current = runtime.bundle_stage.query_current()
            if current["target_graph"]["status"] == "accepted":
                break
        else:
            raise AssertionError("Bundle did not accept a Target DAG")

        run = current["run"]
        target = current["target_graph"]["targets"][0]
        assert run["root_session_ref"].startswith("bundle_session_")
        assert run["native_session_ref"] == "bundle-primary-1"
        assert run["root_session_ref"] != run["native_session_ref"]
        assert run["review"]["review_mode"] == "advisory_unobserved"
        assert "reviewer_agent_ref" not in run["review"]
        assert target["target_ref"].startswith("target_")
        assert target["target_ref"] not in {
            run["root_session_ref"],
            run["native_session_ref"],
        }
        assert current["target_graph"]["frontier"] == [target["target_ref"]]
        assert current["stage_commit"] is None
    finally:
        runtime.close()


def test_formal_target_graph_fails_closed_without_owner_proof_verifier(
    tmp_path: Path,
) -> None:
    runtime = _bundle_runtime(
        tmp_path / "bundle-target-proof-unverified",
        verify_target_candidate_proofs=False,
    )
    try:
        _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        _finish_plan_stage(runtime)
        for _step in range(8):
            try:
                runtime.bundle_stage.process_once()
            except OwnerConflict as error:
                assert error.code == "target_candidate_owner_proof_unverified"
                break
        else:
            raise AssertionError("Formal candidate was not rejected fail-closed")
        current = runtime.bundle_stage.query_current()
        request_ref = current["stage_run_request"]["request_ref"]
        assert runtime.owners.research_graph.query_target_graph(request_ref) is None
    finally:
        runtime.close()


def test_rolling_target_heads_are_append_only_current_and_sealed_before_commit(
    tmp_path: Path,
) -> None:
    runtime = _bundle_runtime(
        tmp_path / "bundle-rolling-targets",
        plan_skill_provider=_TwoGapPlanSkill(),
    )
    try:
        _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        _finish_plan_stage(runtime)
        for _step in range(10):
            assert runtime.bundle_stage.process_once()
            current = runtime.bundle_stage.query_current()
            if current["target_graph"]["status"] == "accepted":
                break
        else:
            raise AssertionError("Bundle did not accept the initial Target batch")

        request_ref = current["stage_run_request"]["request_ref"]
        run = runtime.owners.agent_runtime.query_bundle_stage_run(request_ref)
        graph = runtime.owners.research_graph.query_target_graph(request_ref)
        assert run is not None and run.execution is not None
        assert run.native_session_ref is not None
        assert graph is not None
        assert graph.head_generation == 0
        assert graph.strategy_complete is False
        initial_target = graph.targets[0]

        # Head growth is an RG concern and must not need the retired
        # Target->Experiment carrier.  Keep this proof entirely before any
        # execution authority is selected.
        with runtime._database.read() as connection:
            target_before = dict(
                connection.execute(
                    text("SELECT * FROM rg_targets WHERE target_ref = :target_ref"),
                    {"target_ref": initial_target.target_ref},
                ).one()._mapping
            )

        def propose(
            *,
            key: str,
            base_graph,
            candidates: tuple[dict[str, object], ...],
            complete: bool,
            requires: tuple[str, ...] = (),
        ):
            inbox_checkpoint = (
                runtime.owners.agent_runtime.query_bundle_inbox_checkpoint(
                    run.run_ref
                )
            )
            assert inbox_checkpoint is not None
            return runtime.owners.agent_runtime.record_bundle_target_proposal(
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                native_session_ref=run.native_session_ref,
                graph_ref=graph.graph_ref,
                base_generation=base_graph.head_generation,
                base_head_receipt=base_graph.head_receipt,
                strategy_update={
                    "schema_ref": FORMAL_STRATEGY_UPDATE_SCHEMA_REF,
                    "revision": base_graph.head_generation + 2,
                    "candidates": list(candidates),
                    "requires_accepted_labels": list(requires),
                    "strategy_complete": complete,
                },
                inbox_checkpoint=inbox_checkpoint,
                idempotency_key=key,
            )

        incomplete_seal = propose(
            key="bundle-rolling-incomplete-seal",
            base_graph=graph,
            candidates=(),
            complete=True,
        )
        with pytest.raises(
            OwnerConflict,
            match="completed_strategy_cell_coverage_invalid",
        ):
            runtime.owners.research_graph.append_target_batch(
                graph_ref=graph.graph_ref,
                proposal_ref=incomplete_seal.proposal_ref,
                proposal=incomplete_seal.proposal,
                proposal_hash=incomplete_seal.proposal_hash,
                proposal_receipt=incomplete_seal.receipt,
            )
        assert (
            runtime.owners.advancement_engine.query_bundle_stage_commit(request_ref)
            is None
        )

        accepted_plan = current["stage_run_request"]["accepted_formal_plan_binding"][
            "plan_document"
        ]
        second_brief = accepted_plan["experiment_briefs"][1]
        assert isinstance(second_brief, dict)
        second_key = cast(str, second_brief["experiment_key"])
        second_spec = _formal_candidate(
            completion_document=cast(
                dict[str, object], graph.target_plan["completion_contract"]
            ),
            label="artifact-boundary-followup",
            experiment_key=second_key,
            cell=f"measurement:{second_key}",
            depends_on=(),
        )
        append = propose(
            key="bundle-rolling-append",
            base_graph=graph,
            candidates=(second_spec,),
            complete=True,
            requires=(),
        )
        stale = propose(
            key="bundle-rolling-stale",
            base_graph=graph,
            candidates=(
                {
                    **deepcopy(second_spec),
                    "candidate": {
                        **deepcopy(second_spec["candidate"]),
                        "local_label": "stale-artifact-boundary-followup",
                    },
                },
            ),
            complete=True,
            requires=(),
        )
        head_one = runtime.owners.research_graph.append_target_batch(
            graph_ref=graph.graph_ref,
            proposal_ref=append.proposal_ref,
            proposal=append.proposal,
            proposal_hash=append.proposal_hash,
            proposal_receipt=append.receipt,
        )
        assert head_one.generation == 1
        assert head_one.strategy_complete is True
        with pytest.raises(OwnerConflict, match="target_graph_append_base_stale"):
            runtime.owners.research_graph.append_target_batch(
                graph_ref=graph.graph_ref,
                proposal_ref=stale.proposal_ref,
                proposal=stale.proposal,
                proposal_hash=stale.proposal_hash,
                proposal_receipt=stale.receipt,
            )

        with runtime._database.read() as connection:
            target_after = dict(
                connection.execute(
                    text("SELECT * FROM rg_targets WHERE target_ref = :target_ref"),
                    {"target_ref": initial_target.target_ref},
                ).one()._mapping
            )
        assert target_after == target_before
        assert target_after["append_ref"] is None
        assert runtime.owners.research_graph.query_target_commits(graph.graph_ref) == ()

        graph_one = runtime.owners.research_graph.query_target_graph(request_ref)
        assert graph_one is not None and len(graph_one.targets) == 2
        final_head = head_one

        graph_two = runtime.owners.research_graph.query_target_graph(request_ref)
        assert graph_two is not None
        after_seal = propose(
            key="bundle-rolling-after-seal",
            base_graph=graph_two,
            candidates=(
                {
                    **deepcopy(second_spec),
                    "candidate": {
                        **deepcopy(second_spec["candidate"]),
                        "local_label": "forbidden-after-seal",
                        "depends_on_labels": ["artifact-boundary-followup"],
                    },
                },
            ),
            complete=False,
        )
        with pytest.raises(OwnerConflict, match="target_graph_strategy_complete"):
            runtime.owners.research_graph.append_target_batch(
                graph_ref=graph.graph_ref,
                proposal_ref=after_seal.proposal_ref,
                proposal=after_seal.proposal,
                proposal_hash=after_seal.proposal_hash,
                proposal_receipt=after_seal.receipt,
            )

        reread = runtime.owners.research_graph.query_target_graph(request_ref)
        assert reread is not None
        assert reread.head_generation == 1
        assert reread.head_receipt == final_head.receipt
        assert reread.target_set_hash == final_head.target_set_hash
        assert reread.coverage_hash == final_head.coverage_hash
        assert runtime.owners.advancement_engine.query_bundle_stage_commit(
            request_ref
        ) is None
    finally:
        runtime.close()


def test_rolling_worker_waits_for_native_commit_without_legacy_execution(
    tmp_path: Path,
) -> None:
    bundle_skill = _RollingBundleSkill()
    runtime = _bundle_runtime(
        tmp_path / "bundle-worker-rolling-targets",
        plan_skill_provider=_TwoGapPlanSkill(),
        bundle_skill_provider=bundle_skill,
    )
    try:
        _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        _finish_plan_stage(runtime)
        before = _target_commit_count(runtime)
        for _step in range(20):
            runtime.bundle_stage.process_once()
            current = runtime.bundle_stage.query_current()
            targets = current["target_graph"].get("targets", [])
            if targets and runtime.owners.agent_runtime.query_target_launch_ack(
                targets[0]["target_ref"]
            ) is not None:
                break
        else:
            raise AssertionError("Rolling Bundle worker did not admit native launch")

        request_ref = current["stage_run_request"]["request_ref"]
        run = runtime.owners.agent_runtime.query_bundle_stage_run(request_ref)
        graph = runtime.owners.research_graph.query_target_graph(request_ref)
        assert run is not None and run.native_session_ref is not None
        assert graph is not None
        assert graph.head_generation == 0
        assert graph.strategy_complete is False
        assert len(graph.targets) == 1
        assert runtime.owners.research_graph.query_target_commits(graph.graph_ref) == ()
        assert bundle_skill.batch_requests == []
        assert runtime.owners.advancement_engine.query_bundle_stage_commit(
            request_ref
        ) is None
        assert _target_commit_count(runtime) == before
    finally:
        runtime.close()


def test_target_graph_must_match_the_exact_executed_target_plan(
    tmp_path: Path,
) -> None:
    runtime = _bundle_runtime(tmp_path / "bundle-executed-plan-binding")
    try:
        _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        _finish_plan_stage(runtime)
        for _step in range(8):
            assert runtime.bundle_stage.process_once()
            current = runtime.bundle_stage.query_current()
            request_projection = current["stage_run_request"]
            if request_projection is None:
                continue
            run = runtime.owners.agent_runtime.query_bundle_stage_run(
                request_projection["request_ref"]
            )
            if run is not None and run.execution is not None:
                break
        else:
            raise AssertionError("Bundle attempt did not execute TargetPlan")

        assert runtime.owners.research_graph.query_target_graph(run.request_ref) is None
        forged_plan = deepcopy(run.execution.outcome)
        forged_update = forged_plan["initial_strategy_update"]
        assert isinstance(forged_update, dict)
        forged_candidates = forged_update["candidates"]
        assert isinstance(forged_candidates, list)
        forged_target = forged_candidates[0]
        assert isinstance(forged_target, dict)
        forged_target["risk_class"] = "high"
        with pytest.raises(
            OwnerConflict, match="target_plan_execution_binding_invalid"
        ):
            runtime.owners.research_graph.accept_target_graph(
                request_ref=run.request_ref,
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                submission_ref=run.execution.submission_ref,
                context_pack_ref=request_projection["context_pack_ref"],
                target_plan=forged_plan,
                target_plan_hash=canonical_hash(forged_plan),
                execution_payload_hash=run.execution.payload_hash,
                execution_receipt=run.execution.receipt,
            )
        assert runtime.owners.research_graph.query_target_graph(run.request_ref) is None
        assert runtime.bundle_stage.process_once()
        assert runtime.bundle_stage.query_current()["target_graph"]["status"] == (
            "accepted"
        )
    finally:
        runtime.close()


def test_bundle_root_session_selects_from_the_durable_parallel_frontier(
    tmp_path: Path,
) -> None:
    runtime = _bundle_runtime(
        tmp_path / "bundle-root-frontier",
        bundle_skill_provider=_ParallelTwoTargetBundleSkill(),
    )
    try:
        _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        _finish_plan_stage(runtime)

        for _step in range(12):
            assert runtime.bundle_stage.process_once()
            current = runtime.bundle_stage.query_current()
            run = runtime.owners.agent_runtime.query_bundle_stage_run(
                current["stage_run_request"]["request_ref"]
            )
            if run is None:
                continue
            decisions = runtime.owners.agent_runtime.query_bundle_dispatch_decisions(
                run.run_ref
            )
            if decisions:
                break
        else:
            raise AssertionError("Bundle root did not persist a dispatch decision")

        targets = current["target_graph"]["targets"]
        assert len(current["target_graph"]["frontier"]) == 2
        assert decisions[-1].selected_target_ref == targets[-1]["target_ref"]
        assert decisions[-1].selected_target_ref != targets[0]["target_ref"]
        assert decisions[-1].native_session_ref == current["run"]["native_session_ref"]

        for _step in range(4):
            assert runtime.bundle_stage.process_once()
            after_dispatch = runtime.bundle_stage.query_current()
            selected_ref = after_dispatch["target_graph"]["targets"][-1][
                "target_ref"
            ]
            ack = runtime.owners.agent_runtime.query_target_launch_ack(selected_ref)
            if ack is not None:
                break
        else:
            raise AssertionError("Selected frontier Target was not admitted")
        first, selected = after_dispatch["target_graph"]["targets"]
        assert first["target_run_ref"] is None
        assert runtime.owners.agent_runtime.query_target_launch_ack(
            first["target_ref"]
        ) is None
        admitted = runtime.owners.agent_runtime.query_admitted_target_launch(
            selected["target_ref"]
        )
        assert admitted is not None
        assert admitted.ack == ack
        assert admitted.target_run_ref != selected["target_ref"]
        assert not admitted.target_run_ref.startswith("experiment_run_")
    finally:
        runtime.close()


@pytest.mark.parametrize("action", ("wait", "replan_required"))
def test_bundle_rejects_non_dispatch_decisions_for_an_executable_frontier(
    tmp_path: Path,
    action: str,
) -> None:
    bundle_provider = _NonDispatchingBundleSkill(action)
    runtime = _bundle_runtime(
        tmp_path / f"bundle-invalid-{action}",
        bundle_skill_provider=bundle_provider,
    )
    try:
        _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        _finish_plan_stage(runtime)
        for _step in range(10):
            assert runtime.bundle_stage.process_once()
            current = runtime.bundle_stage.query_current()
            if current["target_graph"]["status"] == "accepted":
                break
        else:
            raise AssertionError("Bundle did not accept TargetGraph")

        run = runtime.owners.agent_runtime.query_bundle_stage_run(
            current["stage_run_request"]["request_ref"]
        )
        assert run is not None
        assert current["target_graph"]["frontier"]
        for _step in range(4):
            changed = runtime.bundle_stage.process_once()
            if bundle_provider.schedule_calls:
                assert not changed
                break
            assert changed
        else:
            raise AssertionError("Bundle root did not evaluate its frontier")
        assert bundle_provider.schedule_calls == 1
        assert (
            runtime.owners.agent_runtime.query_bundle_dispatch_decisions(run.run_ref)
            == ()
        )

        # Free-text wait/replan output is not a durable blocker. The same root
        # Session re-evaluates without creating launch or execution authority.
        assert not runtime.bundle_stage.process_once()
        assert bundle_provider.schedule_calls == 2
        after_retry = runtime.bundle_stage.query_current()
        assert (
            after_retry["target_graph"]["frontier"]
            == current["target_graph"]["frontier"]
        )
        assert all(
            target["target_run_ref"] is None
            for target in after_retry["target_graph"]["targets"]
        )
        assert all(
            runtime.owners.agent_runtime.query_target_launch_ack(target["target_ref"])
            is None
            for target in after_retry["target_graph"]["targets"]
        )
    finally:
        runtime.close()


def test_bundle_dispatch_is_fail_closed_until_runtime_protection_is_confirmed(
    tmp_path: Path,
) -> None:
    inhibitor = _TogglePowerInhibitor()
    provider = _ProtectedRollingBundleSkill()
    runtime = _bundle_runtime(
        tmp_path / "bundle-protected-dispatch",
        bundle_skill_provider=provider,
        power_inhibitor=inhibitor,
    )
    try:
        _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        _finish_plan_stage(runtime)
        for _step in range(10):
            assert runtime.bundle_stage.process_once()
            current = runtime.bundle_stage.query_current()
            if current["target_graph"]["status"] == "accepted":
                break
        else:
            raise AssertionError("Bundle did not accept TargetGraph")

        request_ref = current["stage_run_request"]["request_ref"]
        run = runtime.owners.agent_runtime.query_bundle_stage_run(request_ref)
        assert run is not None
        assert runtime.query_runtime_observability()["status"] == "ready"

        inhibitor.available = False
        for _step in range(4):
            changed = runtime.bundle_stage.process_once()
            if runtime.bundle_stage.transient_error == (
                "power_inhibitor_acquisition_failed"
            ):
                assert not changed
                break
            assert changed
        else:
            raise AssertionError("Bundle dispatch did not reach protection acquire")

        assert provider.schedule_requests == []
        assert runtime.owners.agent_runtime.query_bundle_dispatch_decisions(
            run.run_ref
        ) == ()
        waiting = runtime.query_runtime_observability()["durable_waiting"]
        assert waiting == [
            {
                "responsibility_ref": waiting[0]["responsibility_ref"],
                "operation_ref": run.review_invocation.operation_ref,
                "effect_kind": "provider_unit",
                "reason": {"code": "power_inhibitor_acquisition_failed"},
            }
        ]

        inhibitor.available = True
        assert runtime.bundle_stage.process_once()
        assert len(provider.schedule_requests) == 1
        assert (
            provider.schedule_requests[0].job_ref
            == run.review_invocation.operation_ref
        )
        assert len(
            runtime.owners.agent_runtime.query_bundle_dispatch_decisions(run.run_ref)
        ) == 1
        observability = runtime.query_runtime_observability()
        assert observability["durable_waiting"] == []
        assert observability["inhibitor"]["status"] == "idle"
    finally:
        runtime.close()


def test_bundle_dispatch_recovers_owner_commit_before_runtime_finish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _ProtectedRollingBundleSkill()
    runtime = _bundle_runtime(
        tmp_path / "bundle-dispatch-finish-recovery",
        bundle_skill_provider=provider,
    )
    try:
        _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        _finish_plan_stage(runtime)
        for _step in range(10):
            assert runtime.bundle_stage.process_once()
            current = runtime.bundle_stage.query_current()
            if current["target_graph"]["status"] == "accepted":
                break
        else:
            raise AssertionError("Bundle did not accept TargetGraph")

        request_ref = current["stage_run_request"]["request_ref"]
        run = runtime.owners.agent_runtime.query_bundle_stage_run(request_ref)
        assert run is not None
        acknowledge = runtime.owners.agent_runtime.acknowledge_provider_safe_point

        def lose_finish_ack(**_values) -> None:
            raise OwnerConflict("injected_runtime_finish_ack_loss")

        monkeypatch.setattr(
            runtime.owners.agent_runtime,
            "acknowledge_provider_safe_point",
            lose_finish_ack,
        )
        for _step in range(4):
            try:
                changed = runtime.bundle_stage.process_once()
            except OwnerConflict as error:
                assert error.code == "injected_runtime_finish_ack_loss"
                break
            assert changed
        else:
            raise AssertionError("Bundle dispatch did not reach the finish boundary")

        assert len(provider.schedule_requests) == 1
        assert len(
            runtime.owners.agent_runtime.query_bundle_dispatch_decisions(run.run_ref)
        ) == 1
        assert len(runtime.query_runtime_observability()["responsibilities"]) == 1

        monkeypatch.setattr(
            runtime.owners.agent_runtime,
            "acknowledge_provider_safe_point",
            acknowledge,
        )
        assert runtime.bundle_stage.process_once()
        assert len(provider.schedule_requests) == 1
        assert runtime.query_runtime_observability()["responsibilities"] == []
        assert runtime.query_runtime_observability()["inhibitor"]["status"] == "idle"
    finally:
        runtime.close()


def test_bundle_dispatch_restart_reuses_job_and_reconciles_committed_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = tmp_path / "bundle-dispatch-restart-reconciliation"
    provider = _ProtectedRollingBundleSkill()
    runtime = _bundle_runtime(data_root, bundle_skill_provider=provider)
    try:
        _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        _finish_plan_stage(runtime)
        for _step in range(10):
            assert runtime.bundle_stage.process_once()
            current = runtime.bundle_stage.query_current()
            if current["target_graph"]["status"] == "accepted":
                break
        else:
            raise AssertionError("Bundle did not accept TargetGraph")

        request_ref = current["stage_run_request"]["request_ref"]
        target_ref = current["target_graph"]["targets"][0]["target_ref"]
        run = runtime.owners.agent_runtime.query_bundle_stage_run(request_ref)
        assert run is not None

        def lose_finish_ack(**_values) -> None:
            raise OwnerConflict("injected_runtime_finish_ack_loss")

        monkeypatch.setattr(
            runtime.owners.agent_runtime,
            "acknowledge_provider_safe_point",
            lose_finish_ack,
        )
        for _step in range(4):
            try:
                changed = runtime.bundle_stage.process_once()
            except OwnerConflict as error:
                assert error.code == "injected_runtime_finish_ack_loss"
                break
            assert changed
        else:
            raise AssertionError("Bundle dispatch did not reach the finish boundary")

        assert len(provider.schedule_requests) == 1
        stable_job_ref = run.review_invocation.operation_ref
        assert provider.schedule_requests[0].job_ref == stable_job_ref
        assert len(runtime.query_runtime_observability()["responsibilities"]) == 1
    finally:
        runtime.close()

    restarted_provider = _ProtectedRollingBundleSkill()
    restarted = _bundle_runtime(
        data_root,
        bundle_skill_provider=restarted_provider,
    )
    try:
        recovered_run = restarted.owners.agent_runtime.query_bundle_stage_run(
            request_ref
        )
        assert recovered_run is not None
        assert recovered_run.review_invocation.operation_ref == stable_job_ref
        for _step in range(6):
            changed = restarted.bundle_stage.process_once()
            if (
                restarted.owners.agent_runtime.query_target_launch_ack(target_ref)
                is not None
            ):
                break
            assert changed
        else:
            raise AssertionError("Restart did not replay the committed dispatch")

        assert restarted_provider.schedule_requests == []
        assert len(
            restarted.owners.agent_runtime.query_bundle_dispatch_decisions(
                recovered_run.run_ref
            )
        ) == 1
        observability = restarted.query_runtime_observability()
        assert observability["responsibilities"] == []
        assert observability["inhibitor"]["status"] == "idle"
    finally:
        restarted.close()


def test_bundle_dispatch_restart_does_not_ack_unknown_turn_as_completed_review(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "bundle-dispatch-unknown-restart"
    provider = _UnknownDispatchBundleSkill()
    runtime = _bundle_runtime(data_root, bundle_skill_provider=provider)
    try:
        _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        _finish_plan_stage(runtime)
        for _step in range(10):
            assert runtime.bundle_stage.process_once()
            current = runtime.bundle_stage.query_current()
            if current["target_graph"]["status"] == "accepted":
                break
        else:
            raise AssertionError("Bundle did not accept TargetGraph")

        request_ref = current["stage_run_request"]["request_ref"]
        run = runtime.owners.agent_runtime.query_bundle_stage_run(request_ref)
        assert run is not None
        for _step in range(4):
            changed = runtime.bundle_stage.process_once()
            if runtime.bundle_stage.transient_error == (
                "codex_operation_reconciliation_pending"
            ):
                assert not changed
                break
            assert changed
        else:
            raise AssertionError("Bundle dispatch did not enter unknown outcome")

        assert len(provider.schedule_requests) == 1
        stable_job_ref = run.review_invocation.operation_ref
        assert provider.schedule_requests[0].job_ref == stable_job_ref
        with runtime._database.read() as connection:
            old_unit = connection.execute(
                text(
                    "SELECT unit_ref, operation_ref, status FROM "
                    "ar_provider_units WHERE run_ref = :run_ref AND status = "
                    "'active'"
                ),
                {"run_ref": run.run_ref},
            ).one()
        assert old_unit.operation_ref == stable_job_ref
        assert old_unit.unit_ref != run.review_invocation.invocation_ref
        old_attempt_ref = run.attempt_ref
        old_invocation_ref = run.review_invocation.invocation_ref
    finally:
        runtime.close()

    restarted_provider = _UnknownDispatchBundleSkill()
    restarted = _bundle_runtime(
        data_root,
        bundle_skill_provider=restarted_provider,
    )
    try:
        recovered = restarted.owners.agent_runtime.query_bundle_stage_run(request_ref)
        assert recovered is not None
        assert recovered.attempt_ref != old_attempt_ref
        assert recovered.review_invocation.invocation_ref != old_invocation_ref
        assert recovered.review_invocation.operation_ref == stable_job_ref
        with restarted._database.read() as connection:
            persisted_old_unit = connection.execute(
                text(
                    "SELECT operation_ref, status FROM ar_provider_units WHERE "
                    "unit_ref = :unit_ref"
                ),
                {"unit_ref": old_unit.unit_ref},
            ).one()
        assert persisted_old_unit.operation_ref == stable_job_ref
        assert persisted_old_unit.status == "revocation_pending"
        responsibilities = restarted.query_runtime_observability()[
            "responsibilities"
        ]
        assert len(responsibilities) == 1
        assert responsibilities[0]["operation_ref"] == stable_job_ref
        assert responsibilities[0]["status"] == "interrupted"
        assert restarted_provider.schedule_requests == []
    finally:
        restarted.close()


def test_bundle_target_batch_is_fail_closed_until_runtime_protection_is_confirmed(
    tmp_path: Path,
) -> None:
    inhibitor = _TogglePowerInhibitor()
    provider = _ProtectedSealingBundleSkill()
    runtime = _bundle_runtime(
        tmp_path / "bundle-protected-target-batch",
        bundle_skill_provider=provider,
        power_inhibitor=inhibitor,
    )
    try:
        target, graph_ref = _accept_real_target_root_commit(runtime)
        current = runtime.bundle_stage.query_current()
        request_ref = current["stage_run_request"]["request_ref"]
        run = runtime.owners.agent_runtime.query_bundle_stage_run(request_ref)
        graph = runtime.owners.research_graph.query_target_graph(request_ref)
        assert run is not None and graph is not None
        assert graph.graph_ref == graph_ref
        commits = runtime.owners.research_graph.query_target_commits(graph_ref)
        assert len(commits) == 1
        assert commits[0].target_ref == target.target_ref

        inhibitor.available = False
        for _step in range(4):
            changed = runtime.bundle_stage.process_once()
            if runtime.bundle_stage.transient_error == (
                "power_inhibitor_acquisition_failed"
            ):
                assert not changed
                break
            assert changed
        else:
            raise AssertionError("Bundle target batch did not reach protection acquire")

        assert provider.batch_requests == []
        assert runtime.owners.agent_runtime.query_bundle_target_proposals(
            run.run_ref
        ) == ()
        waiting = runtime.query_runtime_observability()["durable_waiting"]
        assert waiting == [
            {
                "responsibility_ref": waiting[0]["responsibility_ref"],
                "operation_ref": run.review_invocation.operation_ref,
                "effect_kind": "provider_unit",
                "reason": {"code": "power_inhibitor_acquisition_failed"},
            }
        ]

        inhibitor.available = True
        assert runtime.bundle_stage.process_once()
        assert len(provider.batch_requests) == 1
        assert provider.batch_requests[0].job_ref == (
            run.review_invocation.operation_ref
        )
        assert len(
            runtime.owners.agent_runtime.query_bundle_target_proposals(run.run_ref)
        ) == 1
        observability = runtime.query_runtime_observability()
        assert observability["durable_waiting"] == []
        assert observability["inhibitor"]["status"] == "idle"
    finally:
        runtime.close()


def test_bundle_target_batch_restart_reconciles_committed_owner_proposal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = tmp_path / "bundle-target-batch-restart-reconciliation"
    provider = _ProtectedSealingBundleSkill()
    runtime = _bundle_runtime(data_root, bundle_skill_provider=provider)
    try:
        _target, graph_ref = _accept_real_target_root_commit(runtime)
        current = runtime.bundle_stage.query_current()
        request_ref = current["stage_run_request"]["request_ref"]
        run = runtime.owners.agent_runtime.query_bundle_stage_run(request_ref)
        graph = runtime.owners.research_graph.query_target_graph(request_ref)
        assert run is not None and graph is not None
        assert graph.graph_ref == graph_ref
        base_generation = graph.head_generation

        def lose_finish_ack(**_values) -> None:
            raise OwnerConflict("injected_runtime_finish_ack_loss")

        monkeypatch.setattr(
            runtime.owners.agent_runtime,
            "acknowledge_provider_safe_point",
            lose_finish_ack,
        )
        for _step in range(4):
            try:
                changed = runtime.bundle_stage.process_once()
            except OwnerConflict as error:
                assert error.code == "injected_runtime_finish_ack_loss"
                break
            assert changed
        else:
            raise AssertionError(
                "Bundle target batch did not reach the finish boundary"
            )

        assert len(provider.batch_requests) == 1
        stable_job_ref = run.review_invocation.operation_ref
        assert provider.batch_requests[0].job_ref == stable_job_ref
        assert len(
            runtime.owners.agent_runtime.query_bundle_target_proposals(run.run_ref)
        ) == 1
        assert len(runtime.query_runtime_observability()["responsibilities"]) == 1
    finally:
        runtime.close()

    restarted_provider = _ProtectedSealingBundleSkill()
    restarted = _bundle_runtime(
        data_root,
        bundle_skill_provider=restarted_provider,
    )
    try:
        recovered_run = restarted.owners.agent_runtime.query_bundle_stage_run(
            request_ref
        )
        assert recovered_run is not None
        assert recovered_run.review_invocation.operation_ref == stable_job_ref
        for _step in range(4):
            changed = restarted.bundle_stage.process_once()
            recovered_graph = restarted.owners.research_graph.query_target_graph(
                request_ref
            )
            assert recovered_graph is not None
            if recovered_graph.head_generation > base_generation:
                break
            assert changed
        else:
            raise AssertionError("Restart did not append the committed target batch")

        assert restarted_provider.batch_requests == []
        assert len(
            restarted.owners.agent_runtime.query_bundle_target_proposals(
                recovered_run.run_ref
            )
        ) == 1
        observability = restarted.query_runtime_observability()
        assert observability["responsibilities"] == []
        assert observability["inhibitor"]["status"] == "idle"
    finally:
        restarted.close()


def test_high_risk_target_waits_for_exact_human_confirmation_then_resumes(
    tmp_path: Path,
) -> None:
    runtime = _bundle_runtime(
        tmp_path / "bundle-high-risk",
        bundle_skill_provider=_HighRiskBundleSkill(),
    )
    try:
        _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        _finish_plan_stage(runtime)

        for _step in range(12):
            changed = runtime.bundle_stage.process_once()
            current = runtime.bundle_stage.query_current()
            requests = runtime.owners.agent_runtime.query_human_requests(
                include_history=True,
            )
            if requests:
                break
            assert changed
        else:
            raise AssertionError("High-risk Target did not open HumanRequest")

        # The request is local to the exact Target and does not create a
        # TargetRun merely because the Quest has broad authorization.
        target = current["target_graph"]["targets"][0]
        assert target["status"] == "blocked"
        assert target["blocker"] == {"code": "target_high_risk_authorization_required"}
        assert target["target_run_ref"] is None
        request = requests[0]
        assert request["target_assertion"]["target_ref"] == target["target_ref"]
        projection = runtime.owners.research_graph.query_target_candidate_projection(
            target_ref=target["target_ref"]
        )
        assert projection is not None
        assert request["target_assertion"]["target_spec_hash"] == (
            projection.projection_digest
        )

        assert runtime.owners.agent_runtime.query_target_launch_ack(
            target["target_ref"]
        ) is None

        authorization = _grant_request_capability(runtime, request)
        for _step in range(12):
            runtime.bundle_stage.process_once()
            current = runtime.bundle_stage.query_current()
            target = current["target_graph"]["targets"][0]
            ack = runtime.owners.agent_runtime.query_target_launch_ack(
                target["target_ref"]
            )
            if ack is not None:
                break
        else:
            raise AssertionError("Authorized Target did not resume")

        admitted = runtime.owners.agent_runtime.query_admitted_target_launch(
            target["target_ref"]
        )
        assert admitted is not None
        assert admitted.ack == ack
        assert admitted.target_run_ref != target["target_ref"]
        bundle_run = runtime.owners.agent_runtime.query_bundle_stage_run(
            current["stage_run_request"]["request_ref"]
        )
        assert bundle_run is not None
        dispatches = runtime.owners.agent_runtime.query_bundle_dispatch_decisions(
            bundle_run.run_ref
        )
        assert dispatches
        dispatch = dispatches[-1]
        assert dispatch.selected_target_ref == target["target_ref"]
        persisted = runtime.owners.agent_runtime.query_human_request(
            request["request_ref"]
        )
        assert persisted is not None
        assert persisted["direct_waiters"][0]["status"] == "consumed"
        assert authorization["receipt_ref"] == persisted["direct_waiters"][0][
            "resume_validation"
        ]["authorization_receipt_ref"]
        assert runtime.owners.agent_runtime.query_target_run_admission(
            target["target_ref"]
        ) is None
        assert runtime.owners.research_graph.query_target_run_binding(
            target["target_ref"]
        ) is None
        assert runtime.owners.agent_runtime.query_target_launch_ack(
            target["target_ref"]
        ) == ack
    finally:
        runtime.close()


def test_bundle_cannot_stage_commit_while_native_target_is_pending(
    tmp_path: Path,
) -> None:
    runtime = _bundle_runtime(tmp_path / "bundle-native-target-pending")
    try:
        _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        _finish_plan_stage(runtime)
        before = _target_commit_count(runtime)
        for _step in range(20):
            runtime.bundle_stage.process_once()
            current = runtime.bundle_stage.query_current()
            targets = current["target_graph"].get("targets", [])
            if targets and runtime.owners.agent_runtime.query_target_launch_ack(
                targets[0]["target_ref"]
            ) is not None:
                break
        else:
            raise AssertionError("Bundle did not admit the native Target launch")

        assert current["run"]["status"] in {"running", "awaiting_acceptance"}
        assert current["target_commits"] == []
        assert current["stage_commit"] is None
        assert _target_commit_count(runtime) == before
    finally:
        runtime.close()


def test_legacy_target_commit_is_rejected_before_receipt_or_fence_checks(
    tmp_path: Path,
) -> None:
    runtime = _bundle_runtime(tmp_path / "bundle-legacy-target-commit-rejected")
    try:
        _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        _finish_plan_stage(runtime)
        for _step in range(12):
            runtime.bundle_stage.process_once()
            current = runtime.bundle_stage.query_current()
            if current["target_graph"]["status"] == "accepted":
                break
        else:
            raise AssertionError("Bundle did not accept its Target graph")

        request_ref = current["stage_run_request"]["request_ref"]
        graph = runtime.owners.research_graph.query_target_graph(request_ref)
        assert graph is not None
        target = graph.targets[0]
        before = _target_commit_count(runtime)
        before_rg = runtime.owners.research_graph.query_snapshot()
        before_feed = runtime.feed.current_revision()
        for fence_ref in ("stale-fence", "apparently-current-fence"):
            with pytest.raises(
                OwnerConflict, match="legacy_target_commit_write_forbidden"
            ):
                runtime.owners.research_graph.accept_target_commit(
                    target_ref=target.target_ref,
                    target_run_ref="legacy-target-run",
                    execution_attempt_ref="legacy-target-attempt",
                    fence_ref=fence_ref,
                    execution_result_hash="0" * 64,
                    execution_receipt=graph.head_receipt,
                    result_content={"result_disposition": "negative"},
                )
        assert _target_commit_count(runtime) == before
        assert runtime.owners.research_graph.query_snapshot() == before_rg
        assert runtime.feed.current_revision() == before_feed
        assert runtime.owners.research_graph.query_target_commits(graph.graph_ref) == ()
        assert (
            runtime.owners.advancement_engine.query_bundle_stage_commit(request_ref)
            is None
        )
    finally:
        runtime.close()


def test_empty_gap_set_records_skipped_without_bundle_run_or_target(
    tmp_path: Path,
) -> None:
    authority = _FixtureTargetCommitEvidenceAuthority()
    runtime = _bundle_runtime(
        tmp_path / "bundle-skipped",
        no_gap=True,
        target_commit_evidence_authority=authority,
    )
    try:
        quest = _confirm_direct_quest(runtime)
        _install_fixture_plan_evidence(
            runtime,
            authority,
            quest_ref=quest["quest_ref"],
        )
        _finish_idea_stage(runtime)
        plan = _finish_plan_stage(runtime)

        for _step in range(4):
            assert runtime.bundle_stage.process_once()
            current = runtime.bundle_stage.query_current()
            if current["stage_commit"] is not None:
                break
        else:
            raise AssertionError("Empty Bundle did not record skipped")

        assert plan["plan_acceptance"]["bundle_disposition"] == (
            "no_new_experiment_required"
        )
        assert current["stage_run_request"] is not None
        assert current["run"] is None
        assert current["target_graph"] == {
            "status": "not_attempted",
            "targets": [],
            "frontier": [],
        }
        assert current["target_commits"] == []
        assert current["disposition"] == {
            "status": "skipped",
            "reason": {"code": "no_bundle_run_required"},
        }
        assert current["stage_commit"]["status"] == "Skipped"
        assert current["stage_commit"]["run_ref"] is None
        assert current["stage_commit"]["target_commit_refs"] == []
    finally:
        runtime.close()


def test_restart_preserves_native_launch_identity_without_legacy_recovery(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "bundle-restart"
    runtime = _bundle_runtime(data_root)
    try:
        _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        _finish_plan_stage(runtime)
        for _step in range(20):
            runtime.bundle_stage.process_once()
            current = runtime.bundle_stage.query_current()
            targets = current["target_graph"]["targets"]
            if targets:
                launch_ack = runtime.owners.agent_runtime.query_target_launch_ack(
                    targets[0]["target_ref"]
                )
            else:
                launch_ack = None
            if launch_ack is not None:
                break
        else:
            raise AssertionError("Bundle did not persist a native Target launch")
        graph_ref = current["target_graph"]["graph_ref"]
        target_ref = targets[0]["target_ref"]
        admitted = runtime.owners.agent_runtime.query_admitted_target_launch(target_ref)
        assert admitted is not None
        target_run_ref = admitted.target_run_ref
        before = _target_commit_count(runtime)
        assert current["target_commits"] == []
    finally:
        runtime.close()

    restarted = _bundle_runtime(data_root)
    try:
        recovered = restarted.bundle_stage.query_current()
        assert recovered["target_graph"]["graph_ref"] == graph_ref
        assert recovered["target_graph"]["targets"][0]["target_ref"] == (target_ref)
        assert restarted.owners.agent_runtime.query_target_launch_ack(target_ref) == (
            launch_ack
        )
        recovered_admission = (
            restarted.owners.agent_runtime.query_admitted_target_launch(target_ref)
        )
        assert recovered_admission is not None
        assert recovered_admission.target_run_ref == target_run_ref
        assert recovered["target_commits"] == []
        assert restarted.owners.research_graph.query_target_commits(graph_ref) == ()
        assert _target_commit_count(restarted) == before
    finally:
        restarted.close()


def test_legacy_target_run_binding_write_entry_is_absent() -> None:
    assert not hasattr(SQLiteResearchGraph, "bind_target_run")
    assert not hasattr(SQLiteResearchGraph, "query_target_run_binding")
