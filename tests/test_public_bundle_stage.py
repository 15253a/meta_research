from __future__ import annotations

from pathlib import Path

import pytest

from meta_research.bundle_skill import (
    BundleSkillDraft,
    BundleSkillRequest,
    BundleSkillResult,
)
from meta_research.composition import build_production_runtime
from meta_research.experiment_contract import ExperimentProviderUnavailable
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
        assert committed["receipt"]["status"] == "accepted"
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
            )
        assert current["target_commits"] == []

        first = runtime.owners.research_graph.accept_target_commit(
            target_ref=target["target_ref"],
            target_run_ref=target_run.run_ref,
            execution_attempt_ref=target_run.attempt_ref,
            fence_ref=target_run.fence_ref,
            execution_result_hash=target_run.result_hash,
            execution_receipt=target_run.execution_receipt,
        )
        replay = runtime.owners.research_graph.accept_target_commit(
            target_ref=target["target_ref"],
            target_run_ref=target_run.run_ref,
            execution_attempt_ref=target_run.attempt_ref,
            fence_ref=target_run.fence_ref,
            execution_result_hash=target_run.result_hash,
            execution_receipt=target_run.execution_receipt,
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
