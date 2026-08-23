from __future__ import annotations

import json
import threading
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from meta_research.bundle_skill import (
    BundleDispatchRequest,
    BundleDispatchResult,
    BundleSkillDraft,
    BundleSkillRequest,
    BundleSkillResult,
)
from meta_research.composition import build_production_runtime
from meta_research.experiment_contract import (
    ExperimentIntent,
    ExperimentProviderUnavailable,
)
from meta_research.owners.agent_runtime import BundleRuntimeBinding
from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.owners.research_memory import AssetIntakeRequest
from meta_research.paths import prepare_data_root
from test_public_plan_stage import (
    _DeterministicDraftingAdapter,
    _DeterministicIdeaSkill,
    _DeterministicPlanSkill,
    _DeterministicProbe,
    _confirm_direct_quest,
    _finish_idea_stage,
    _runtime,
)
from test_public_experiment_measurement import (
    _DeterministicExperimentProvider,
)


class _DeterministicBundleSkill:
    def runtime_binding(self) -> BundleRuntimeBinding:
        return BundleRuntimeBinding(
            packaged_skill_bundle_hash=canonical_hash({"skill": "bundle-public"}),
            instruction_set_hash=canonical_hash({"instructions": "bundle-public"}),
            model_ref="test-model-v1",
            harness_adapter_ref="test-deterministic-v1",
            mcp_bindings=(),
            capability_bindings=(),
            resource_bindings=(),
        )

    def _target_plan(self, request: BundleSkillRequest) -> dict[str, object]:
        brief = request.plan_document["experiment_briefs"][0]
        return {
            "schema_ref": "meta-research/target-plan/v1",
            "kind": "TargetPlan",
            "formal_plan_ref": request.formal_plan_ref,
            "context_pack_ref": request.context_pack_ref,
            "targets": [
                {
                    "target_key": "micro-rare-morphology-comparison",
                    "title": "微型稀有形态比较",
                    "target_type": "micro_experiment",
                    "experiment_key": brief["experiment_key"],
                    "gap_obligation_keys": brief["gap_obligation_keys"],
                    "depends_on": [],
                    "goal": brief["goal"],
                    "hypothesis": "固定样本下变体均值不会优于基线。",
                    "variant_parameter": -0.25,
                    "sample_count": 8,
                    "boundary_constraints": brief["boundary_constraints"],
                    "semantic_delta": brief["semantic_delta"],
                    "contributing_idea_refs": brief["contributing_idea_refs"],
                    "risk_class": "normal",
                }
            ],
            "source_bindings": {
                "formal_plan_ref": request.formal_plan_ref,
                "plan_document_hash": canonical_hash(request.plan_document),
                "context_pack_ref": request.context_pack_ref,
                "context_pack_hash": request.context_pack_hash,
            },
        }

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
            review_mode="harness_child_agent",
            reviewer_agent_ref="bundle-child-reviewer-1",
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


class _TwoTargetBundleSkill(_DeterministicBundleSkill):
    def _target_plan(self, request: BundleSkillRequest) -> dict[str, object]:
        target_plan = super()._target_plan(request)
        first = target_plan["targets"][0]
        assert isinstance(first, dict)
        target_plan["targets"] = [
            first,
            {
                **first,
                "target_key": "micro-rare-morphology-replication",
                "title": "微型稀有形态复验",
                "depends_on": [first["target_key"]],
                "hypothesis": "独立复验仍应保留可复查的显式结果。",
                "variant_parameter": 0.25,
            },
        ]
        return target_plan


class _ParallelTwoTargetBundleSkill(_TwoTargetBundleSkill):
    def _target_plan(self, request: BundleSkillRequest) -> dict[str, object]:
        target_plan = super()._target_plan(request)
        second = target_plan["targets"][1]
        assert isinstance(second, dict)
        second["depends_on"] = []
        return target_plan


class _HighRiskBundleSkill(_DeterministicBundleSkill):
    def _target_plan(self, request: BundleSkillRequest) -> dict[str, object]:
        target_plan = super()._target_plan(request)
        target = target_plan["targets"][0]
        assert isinstance(target, dict)
        target["risk_class"] = "high"
        return target_plan


class _FailAfterFirstTargetProvider(_DeterministicExperimentProvider):
    def execute(self, request, observe):
        if self.execute_calls >= 1:
            self.execute_calls += 1
            self.requests.append(request)
            raise ExperimentProviderUnavailable(
                "experiment_provider_failed",
                durable_outcome="terminal",
            )
        return super().execute(request, observe)


class _DispositionExperimentProvider(_DeterministicExperimentProvider):
    def __init__(self, disposition: str) -> None:
        super().__init__()
        self._disposition = disposition

    def execute(self, request, observe):
        result = super().execute(request, observe)
        return replace(
            result,
            result_content={
                **result.result_content,
                "result_disposition": self._disposition,
            },
        )


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


def _bundle_runtime(
    path: Path,
    *,
    no_gap: bool = False,
    target_commit_evidence_authority=None,
    bundle_skill_provider=None,
    experiment_provider=None,
):
    drafting = _DeterministicDraftingAdapter()
    return build_production_runtime(
        prepare_data_root(path),
        proposal_drafter=drafting,
        intent_drafting_provider=drafting,
        host_compute_probe=_DeterministicProbe(),
        idea_skill_provider=_DeterministicIdeaSkill(),
        plan_skill_provider=_DeterministicPlanSkill(no_gap=no_gap),
        bundle_skill_provider=(bundle_skill_provider or _DeterministicBundleSkill()),
        target_commit_evidence_authority=target_commit_evidence_authority,
        experiment_provider=experiment_provider,
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


def _accepted_result_content(runtime, evaluation_attempt_ref: str) -> dict[str, object]:
    roles = runtime.owners.research_graph.query_experiment_asset_roles(
        evaluation_attempt_ref
    )
    result_roles = [role for role in roles if role.role == "result_content"]
    assert len(result_roles) == 1
    materialized = runtime.owners.research_memory.materialize_asset(
        result_roles[0].binding.version_ref
    )
    value = json.loads(materialized.content.decode("utf-8"))
    assert isinstance(value, dict)
    return value


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
        assert run["review"]["reviewer_agent_ref"] == "bundle-child-reviewer-1"
        assert target["target_ref"].startswith("target_")
        assert target["target_ref"] not in {
            run["root_session_ref"],
            run["native_session_ref"],
            run["review"]["reviewer_agent_ref"],
        }
        assert current["target_graph"]["frontier"] == [target["target_ref"]]
        assert current["stage_commit"] is None
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
        forged_target = forged_plan["targets"][0]
        assert isinstance(forged_target, dict)
        forged_target["title"] = "未由该 execution receipt 执行的另一个标题"
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


def test_target_run_admission_rejects_an_unrelated_experiment(
    tmp_path: Path,
) -> None:
    runtime = _bundle_runtime(tmp_path / "bundle-unrelated-target-run")
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
        graph = runtime.owners.research_graph.query_target_graph(
            current["stage_run_request"]["request_ref"]
        )
        assert graph is not None
        target = graph.targets[0]
        unrelated = runtime.experiment.start(
            ExperimentIntent(
                execution_request_ref=f"unrelated-{target.target_ref}",
                quest_ref=graph.quest_ref,
                title="Unrelated accepted experiment",
                hypothesis="This experiment does not implement the TargetSpec.",
                variant_parameter=0.5,
                sample_count=8,
            ),
            "bundle-unrelated-experiment",
        )
        identities = unrelated["identities"]
        execution = unrelated["execution"]
        intent = unrelated["intent"]
        assert isinstance(identities, dict)
        assert isinstance(execution, dict)
        assert isinstance(intent, dict)
        domain = runtime.owners.research_graph.query_experiment(
            identities["evaluation_attempt_ref"]
        )
        assert domain is not None
        with pytest.raises(OwnerConflict, match="target_run_candidate_invalid"):
            runtime.owners.agent_runtime.admit_target_run(
                target_ref=target.target_ref,
                target_spec_hash=target.spec_hash,
                graph_ref=graph.graph_ref,
                stage_request_ref=graph.request_ref,
                quest_ref=graph.quest_ref,
                target_run_ref=execution["run_ref"],
                evaluation_attempt_ref=identities["evaluation_attempt_ref"],
                execution_request_ref=intent["execution_request_ref"],
                definition_hash=domain.execution_request.definition_hash,
                idempotency_key="bundle-unrelated-target-admission",
            )
        assert (
            runtime.owners.research_graph.query_target_run_binding(target.target_ref)
            is None
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

        assert runtime.bundle_stage.process_once()
        after_dispatch = runtime.bundle_stage.query_current()
        first, selected = after_dispatch["target_graph"]["targets"]
        assert first["target_run_ref"] is None
        assert selected["target_run_ref"].startswith("experiment_run_")
    finally:
        runtime.close()


def test_high_risk_target_waits_for_exact_human_confirmation_then_resumes(
    tmp_path: Path,
) -> None:
    provider = _DeterministicExperimentProvider()
    runtime = _bundle_runtime(
        tmp_path / "bundle-high-risk",
        bundle_skill_provider=_HighRiskBundleSkill(),
        experiment_provider=provider,
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
        assert provider.execute_calls == 0
        request = requests[0]
        assert request["target_assertion"]["target_ref"] == target["target_ref"]
        assert request["target_assertion"]["target_spec_hash"] == target["spec_hash"]

        graph = runtime.owners.research_graph.query_target_graph(
            current["stage_run_request"]["request_ref"]
        )
        assert graph is not None
        accepted_target = graph.targets[0]
        unauthorized_execution = runtime.experiment.start(
            ExperimentIntent(
                execution_request_ref=f"bundle-target-{accepted_target.target_ref}",
                quest_ref=graph.quest_ref,
                title=str(accepted_target.spec["title"]),
                hypothesis=str(accepted_target.spec["hypothesis"]),
                variant_parameter=float(accepted_target.spec["variant_parameter"]),
                sample_count=int(accepted_target.spec["sample_count"]),
            ),
            "bundle-high-risk-unauthorized-preflight",
        )
        identities = unauthorized_execution["identities"]
        execution = unauthorized_execution["execution"]
        intent = unauthorized_execution["intent"]
        assert isinstance(identities, dict)
        assert isinstance(execution, dict)
        assert isinstance(intent, dict)
        domain = runtime.owners.research_graph.query_experiment(
            identities["evaluation_attempt_ref"]
        )
        assert domain is not None
        with pytest.raises(OwnerConflict, match="target_run_authorization_invalid"):
            runtime.owners.agent_runtime.admit_target_run(
                target_ref=accepted_target.target_ref,
                target_spec_hash=accepted_target.spec_hash,
                graph_ref=graph.graph_ref,
                stage_request_ref=graph.request_ref,
                quest_ref=graph.quest_ref,
                target_run_ref=execution["run_ref"],
                evaluation_attempt_ref=identities["evaluation_attempt_ref"],
                execution_request_ref=intent["execution_request_ref"],
                definition_hash=domain.execution_request.definition_hash,
                idempotency_key="bundle-high-risk-unauthorized-admission",
            )
        assert provider.execute_calls == 0

        authorization = _grant_request_capability(runtime, request)
        for _step in range(10):
            runtime.bundle_stage.process_once()
            current = runtime.bundle_stage.query_current()
            target = current["target_graph"]["targets"][0]
            if target["target_run_ref"] is not None:
                break
        else:
            raise AssertionError("Authorized Target did not resume")

        binding = runtime.owners.research_graph.query_target_run_binding(
            target["target_ref"]
        )
        admission = runtime.owners.agent_runtime.query_target_run_admission(
            target["target_ref"]
        )
        assert binding is not None
        assert admission is not None
        assert admission.human_request_ref == request["request_ref"]
        assert admission.human_waiter_ref == target["target_ref"]
        assert admission.human_authorization_receipt_ref == authorization["receipt_ref"]
        assert binding.admission_receipt == admission.receipt
        persisted = runtime.owners.agent_runtime.query_human_request(
            request["request_ref"]
        )
        assert persisted is not None
        assert persisted["direct_waiters"][0]["status"] == "consumed"
    finally:
        runtime.close()


def test_one_gap_runs_as_a_formal_target_and_freezes_a_negative_commit(
    tmp_path: Path,
) -> None:
    runtime = _bundle_runtime(tmp_path / "bundle-target-commit")
    try:
        _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        _finish_plan_stage(runtime)

        for _step in range(24):
            runtime.bundle_stage.process_once()
            runtime.experiment.process_once()
            current = runtime.bundle_stage.query_current()
            if current["target_commits"]:
                break
        else:
            raise AssertionError("Bundle did not freeze a TargetCommit")

        target = current["target_graph"]["targets"][0]
        committed = current["target_commits"][0]
        assert target["status"] == "committed"
        assert target["target_run_ref"].startswith("experiment_run_")
        assert target["target_run_ref"] != target["target_ref"]
        assert committed["target_ref"] == target["target_ref"]
        assert committed["target_run_ref"] == target["target_run_ref"]
        assert committed["result_disposition"] == "negative"
        assert committed["status"] == "realized"
        assert committed["closure"]["implementation"]["receipt"]["status"] == (
            "accepted"
        )
        assert committed["closure"]["definition"]["receipt"]["status"] == ("accepted")
        assert committed["closure"]["checkpoint_artifacts"]
        assert committed["closure"]["metric_result"]["receipt"]["status"] == (
            "accepted"
        )
        assert committed["closure"]["log_assets"]
        assert committed["closure"]["analysis_assets"]
        assert (
            committed["closure"]["target_run_binding"]["admission_receipt"]["status"]
            == "accepted"
        )
        assert committed["closure"]["execution_request"]["receipt"]["status"] == (
            "accepted"
        )
        assert committed["receipt"]["status"] == "accepted"
        assert current["baseline_pool"] == []

        assert runtime.bundle_stage.process_once()
        published = runtime.bundle_stage.query_current()
        assert len(published["baseline_pool"]) == 1
        baseline = published["baseline_pool"][0]
        assert baseline["target_commit_ref"] == committed["commit_ref"]
        assert baseline["evidence_ref"].startswith("evidence_")
        assert baseline["role_receipt"]["status"] == "accepted"
    finally:
        runtime.close()


@pytest.mark.parametrize("disposition", ("nonsignificant", "denied", "uncertain"))
def test_valid_nonpositive_target_outcomes_are_realized_not_failed(
    tmp_path: Path,
    disposition: str,
) -> None:
    runtime = _bundle_runtime(
        tmp_path / f"bundle-disposition-{disposition}",
        experiment_provider=_DispositionExperimentProvider(disposition),
    )
    try:
        _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        _finish_plan_stage(runtime)
        for _step in range(28):
            runtime.bundle_stage.process_once()
            runtime.experiment.process_once()
            current = runtime.bundle_stage.query_current()
            if current["target_commits"]:
                break
        else:
            raise AssertionError(f"Bundle did not realize {disposition}")
        assert current["target_graph"]["targets"][0]["status"] == "committed"
        assert current["target_commits"][0]["result_disposition"] == disposition
        assert current["target_commits"][0]["status"] == "realized"
    finally:
        runtime.close()


def test_all_target_commits_complete_the_root_run_and_bundle_stage(
    tmp_path: Path,
) -> None:
    runtime = _bundle_runtime(tmp_path / "bundle-completed")
    try:
        _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        _finish_plan_stage(runtime)

        for _step in range(32):
            runtime.bundle_stage.process_once()
            runtime.experiment.process_once()
            current = runtime.bundle_stage.query_current()
            if current["stage_commit"] is not None:
                break
        else:
            raise AssertionError("Bundle did not reach StageCommit")

        assert current["run"]["status"] == "completed"
        assert current["disposition"] == {
            "status": "completed",
            "target_count": 1,
            "target_commit_count": 1,
        }
        assert current["stage_commit"]["status"] == "Completed"
        assert current["stage_commit"]["stage"] == "Bundle"
        assert current["stage_commit"]["target_commit_refs"] == [
            current["target_commits"][0]["commit_ref"]
        ]
        assert current["stage_commit"]["run_completion_receipt"]["status"] == (
            "accepted"
        )
        assert current["stage_commit"]["receipt"]["status"] == "accepted"
        assert current["stage_commit"]["next_stage"] == "Reasoning"
    finally:
        runtime.close()


def test_bundle_stage_commit_rejects_an_epoch_changed_during_verification(
    tmp_path: Path,
) -> None:
    runtime = _bundle_runtime(tmp_path / "bundle-epoch-cas")
    try:
        _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        _finish_plan_stage(runtime)
        for _step in range(32):
            runtime.bundle_stage.process_once()
            runtime.experiment.process_once()
            current = runtime.bundle_stage.query_current()
            if (
                current["run"] is not None
                and current["run"]["status"] == "completed"
                and current["stage_commit"] is None
            ):
                break
        else:
            raise AssertionError("Bundle did not reach pre-commit completion")

        request_ref = current["stage_run_request"]["request_ref"]
        run = runtime.owners.agent_runtime.query_bundle_stage_run(request_ref)
        graph = runtime.owners.research_graph.query_target_graph(request_ref)
        assert run is not None and run.completion is not None
        assert graph is not None
        commits = runtime.owners.research_graph.query_target_commits(graph.graph_ref)
        original_verifier = runtime.owners.advancement_engine._target_commit_verifier
        assert original_verifier is not None
        verification_complete = threading.Event()
        continue_commit = threading.Event()

        class _PausingVerifier:
            def verify_target_commit_set(self, **values) -> None:
                original_verifier.verify_target_commit_set(**values)
                verification_complete.set()
                assert continue_commit.wait(timeout=5)

        runtime.owners.advancement_engine._target_commit_verifier = _PausingVerifier()
        failures: list[BaseException] = []

        def commit() -> None:
            try:
                runtime.owners.advancement_engine.commit_bundle_stage(
                    request_ref=request_ref,
                    run_ref=run.run_ref,
                    target_graph_ref=graph.graph_ref,
                    target_commit_receipts=tuple(value.receipt for value in commits),
                    run_completion_receipt=run.completion.receipt,
                    target_graph_receipt=graph.receipt,
                    idempotency_key="bundle-epoch-race-commit",
                )
            except Exception as error:
                failures.append(error)

        worker = threading.Thread(target=commit)
        worker.start()
        assert verification_complete.wait(timeout=5)
        with runtime._database.write() as connection:
            connection.exec_driver_sql(
                "UPDATE ae_stage_run_requests SET epoch = epoch + 1 "
                "WHERE request_ref = ?",
                (request_ref,),
            )
        continue_commit.set()
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert len(failures) == 1
        assert isinstance(failures[0], OwnerConflict)
        assert str(failures[0]) == "bundle_foreground_epoch_stale"
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


def test_target_run_restart_recovers_the_same_target_identity_and_commit(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "bundle-restart"
    runtime = _bundle_runtime(data_root)
    try:
        _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        _finish_plan_stage(runtime)
        for _step in range(12):
            assert runtime.bundle_stage.process_once()
            current = runtime.bundle_stage.query_current()
            targets = current["target_graph"]["targets"]
            if targets and targets[0]["target_run_ref"] is not None:
                break
        else:
            raise AssertionError("Bundle did not bind a TargetRun")
        graph_ref = current["target_graph"]["graph_ref"]
        target_ref = targets[0]["target_ref"]
        target_run_ref = targets[0]["target_run_ref"]
        assert current["target_commits"] == []
    finally:
        runtime.close()

    restarted = _bundle_runtime(data_root)
    try:
        recovered = restarted.bundle_stage.query_current()
        assert recovered["target_graph"]["graph_ref"] == graph_ref
        assert recovered["target_graph"]["targets"][0]["target_ref"] == (target_ref)
        assert recovered["target_graph"]["targets"][0]["target_run_ref"] == (
            target_run_ref
        )
        for _step in range(32):
            restarted.bundle_stage.process_once()
            restarted.experiment.process_once()
            recovered = restarted.bundle_stage.query_current()
            if recovered["stage_commit"] is not None:
                break
        else:
            raise AssertionError("Restarted Bundle did not complete")
        assert len(recovered["target_commits"]) == 1
        assert recovered["target_commits"][0]["target_ref"] == target_ref
        assert recovered["target_commits"][0]["target_run_ref"] == target_run_ref
        assert not restarted.bundle_stage.process_once()
        assert len(restarted.owners.research_graph.query_target_commits(graph_ref)) == 1
    finally:
        restarted.close()


def test_old_fence_is_rejected_and_lost_target_commit_ack_is_idempotent(
    tmp_path: Path,
) -> None:
    runtime = _bundle_runtime(tmp_path / "bundle-fence-and-ack")
    try:
        _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        _finish_plan_stage(runtime)
        for _step in range(28):
            runtime.bundle_stage.process_once()
            runtime.experiment.process_once()
            current = runtime.bundle_stage.query_current()
            targets = current["target_graph"]["targets"]
            if not targets or targets[0]["target_run_ref"] is None:
                continue
            target = targets[0]
            binding = runtime.owners.research_graph.query_target_run_binding(
                target["target_ref"]
            )
            assert binding is not None
            domain = runtime.owners.research_graph.query_experiment(
                binding.evaluation_attempt_ref
            )
            target_run = runtime.owners.agent_runtime.query_experiment_run(
                binding.evaluation_attempt_ref
            )
            if (
                domain is not None
                and domain.formal_measurement_status == "accepted"
                and target_run is not None
                and target_run.execution_receipt is not None
            ):
                break
        else:
            raise AssertionError("Target did not reach Formal Measurement")

        with pytest.raises(OwnerConflict):
            runtime.owners.research_graph.accept_target_commit(
                target_ref=target["target_ref"],
                target_run_ref=target_run.run_ref,
                execution_attempt_ref=target_run.attempt_ref,
                fence_ref=f"{target_run.fence_ref}-stale",
                execution_result_hash=target_run.result_hash,
                execution_receipt=target_run.execution_receipt,
                result_content=_accepted_result_content(
                    runtime, binding.evaluation_attempt_ref
                ),
            )
        assert current["target_commits"] == []

        first = runtime.owners.research_graph.accept_target_commit(
            target_ref=target["target_ref"],
            target_run_ref=target_run.run_ref,
            execution_attempt_ref=target_run.attempt_ref,
            fence_ref=target_run.fence_ref,
            execution_result_hash=target_run.result_hash,
            execution_receipt=target_run.execution_receipt,
            result_content=_accepted_result_content(
                runtime, binding.evaluation_attempt_ref
            ),
        )
        replay = runtime.owners.research_graph.accept_target_commit(
            target_ref=target["target_ref"],
            target_run_ref=target_run.run_ref,
            execution_attempt_ref=target_run.attempt_ref,
            fence_ref=target_run.fence_ref,
            execution_result_hash=target_run.result_hash,
            execution_receipt=target_run.execution_receipt,
            result_content=_accepted_result_content(
                runtime, binding.evaluation_attempt_ref
            ),
        )
        assert replay == first
        graph = runtime.owners.research_graph.query_target_graph(
            current["stage_run_request"]["request_ref"]
        )
        assert graph is not None
        assert runtime.owners.research_graph.query_target_commits(graph.graph_ref) == (
            first,
        )
    finally:
        runtime.close()


def test_partial_failure_keeps_the_realized_target_commit_visible(
    tmp_path: Path,
) -> None:
    provider = _FailAfterFirstTargetProvider()
    runtime = _bundle_runtime(
        tmp_path / "bundle-partial-failure",
        bundle_skill_provider=_TwoTargetBundleSkill(),
        experiment_provider=provider,
    )
    try:
        _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        _finish_plan_stage(runtime)
        for _step in range(64):
            runtime.bundle_stage.process_once()
            runtime.experiment.process_once()
            current = runtime.bundle_stage.query_current()
            if (
                current["disposition"]["status"] == "partial_blocked"
                and current["target_commits"]
            ):
                break
        else:
            raise AssertionError("Bundle did not expose partial failure")

        targets = current["target_graph"]["targets"]
        assert {target["status"] for target in targets} == {
            "committed",
            "blocked",
        }
        assert len(current["target_commits"]) == 1
        assert len(current["baseline_pool"]) == 1
        committed_target = next(
            target for target in targets if target["status"] == "committed"
        )
        blocked_target = next(
            target for target in targets if target["status"] == "blocked"
        )
        assert (
            current["target_commits"][0]["target_ref"]
            == (committed_target["target_ref"])
        )
        assert blocked_target["blocker"] == {"code": "experiment_provider_failed"}
        assert current["disposition"]["blocked_targets"] == [
            {
                "target_ref": blocked_target["target_ref"],
                "reason": blocked_target["blocker"],
            }
        ]
        assert current["stage_commit"] is None
        for _step in range(3):
            runtime.bundle_stage.process_once()
        after_retries = runtime.bundle_stage.query_current()
        assert len(after_retries["target_commits"]) == 1
        assert (
            after_retries["target_commits"][0]["commit_ref"]
            == (current["target_commits"][0]["commit_ref"])
        )
        assert after_retries["stage_commit"] is None
        assert not runtime.bundle_stage.process_once()
    finally:
        runtime.close()
